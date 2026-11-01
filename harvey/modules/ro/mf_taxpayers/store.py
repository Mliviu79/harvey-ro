"""The Ministry of Finance's taxpayer identification file, in a local database.

Once a year the Ministry publishes every Romanian taxpayer's identification
data on data.gov.ro (source.py finds and downloads it): name, registered
office, phone, fax, registration status, whether it is registered for VAT,
and which taxes it is registered for. ``build()`` loads the columns Harvey
uses into their own SQLite database, data/taxpayers.db:

    taxpayers   one row per CUI: its name, address parts, phone and fax,
                status, VAT registration, and three tax facts: the
                micro-enterprise income tax, profit tax, and whether it is
                an employer (wage income tax or the labour insurance
                contribution, which only an employer owes)
    files       which published file each part came from
    meta        when it was loaded, how long it took, counts

The file is '^'-separated UTF-8 with a byte-order mark, one header row; the
columns are found by name, so the order can change. Since 2026 the VAT and
tax columns hold DA/NU; earlier files held the date each was registered
(both are read). A row whose tax code isn't a CUI (a 13-digit personal
numeric code) is never kept. Every legal form is kept: an authorised
natural person's (PFA) registry and tax details are public like a
company's. A new load is built in a temporary file and renamed over the
old one, so a reader never sees a half-loaded database.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from harvey.modules.ro import cui as ro_cui
from harvey.paths import PROJECT_ROOT

TAXPAYERS_PATH = PROJECT_ROOT / "data" / "taxpayers.db"
SCHEMA_VERSION = "1"
LOAD_BATCH = 50_000

# Harvey's name for each column it keeps, and the names the file has used
# for it (compared in capitals, without underscores).
COLUMNS = {
    "cui": ("CODFISCAL", "CUI"),
    "name": ("DENUMIRE", "NUME"),
    "parent_cui": ("CODFISCALPARINTE",),
    "unit_type": ("TIPUNITATE",),
    "contributor_type": ("TIPCONTRIB",),
    "county": ("JUDET",),
    "locality": ("LOCALITATE", "LOC"),
    "sector": ("SECTOR", "SECT"),
    "street": ("STRADA", "STR"),
    "number": ("NR",),
    "block": ("BLOC",),
    "staircase": ("SCARA",),
    "floor": ("ETAJ",),
    "apartment": ("AP",),
    "details": ("DETALIIADRESA",),
    "postal_code": ("CODPOSTAL", "CP"),
    "phone": ("TELEFON", "TEL"),
    "fax": ("FAX",),
    "registered": ("DATAINREGISTRARE",),
    "status": ("STARE",),
    "status_date": ("DATASTARE",),
    "struck_off": ("DATARADIERE",),
    "vat": ("TVA",),
    "profit_tax": ("IMP100",),
    "micro_tax": ("IMP120",),
    "labour_contribution": ("CONT480", "IMP480"),
    "wage_tax": ("IMP602",),
}
TEXT = ("name", "parent_cui", "unit_type", "contributor_type", "county", "locality", "sector",
        "street", "number", "block", "staircase", "floor", "apartment", "details",
        "postal_code", "phone", "fax", "registered", "status", "status_date", "struck_off")
FLAGS = ("vat", "profit_tax", "micro_tax", "employer")

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE files (
    part TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    package TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL DEFAULT 0,
    published TEXT NOT NULL DEFAULT '',
    licence TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    rows INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE taxpayers (
    cui TEXT PRIMARY KEY,
    {", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in TEXT)},
    vat INTEGER,
    vat_since TEXT NOT NULL DEFAULT '',
    profit_tax INTEGER,
    micro_tax INTEGER,
    employer INTEGER,
    part TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""


class TaxpayersBusy(RuntimeError):
    """Another load of the same database is running."""


class FormatChanged(ValueError):
    """A file's columns aren't the ones Harvey reads (no tax code column)."""


# ── Reading one published file ──

def clean_cui(raw: str) -> str:
    """The CUI as digits, no RO, no leading zeros; "" when it isn't one (a
    13-digit personal numeric code never is): harvey.modules.ro.cui."""
    return ro_cui.clean(raw)


def iso_date(raw: str) -> str:
    """YYYY-MM-DD from "DD.MM.YYYY[ HH:MM:SS]", "YYYY-MM-DD" or "YYYYMMDD";
    "" for anything else."""
    text = (raw or "").strip()
    for pattern, order in ((r"(\d{1,2})\.(\d{1,2})\.(\d{4})", "dmy"),
                           (r"(\d{4})-(\d{2})-(\d{2})", "ymd"), (r"(\d{4})(\d{2})(\d{2})$", "ymd")):
        m = re.match(pattern, text)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (c, b, a) if order == "dmy" else (a, b, c)
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                return ""
    return ""


def flag(raw: str) -> tuple[int | None, str]:
    """(1/0/None, date) from a tax column: DA/NU since 2026; before, the
    date the tax was registered (a date is a yes, empty a no)."""
    text = (raw or "").strip().upper()
    if text in ("DA", "D", "1", "Y", "YES"):
        return 1, ""
    if text in ("NU", "N", "0", "NO"):
        return 0, ""
    since = iso_date(text)
    if since:
        return 1, since
    return (0, "") if not text else (None, "")


def _key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.strip().lstrip("﻿").upper())


def _open(path: Path):
    """The file as text: UTF-8 (with or without a byte-order mark), else
    the Windows code page Romanian files used before."""
    with open(path, "rb") as fh:
        sample = fh.read(1 << 20)
    try:
        sample.decode("utf-8")
        encoding = "utf-8-sig"
    except UnicodeDecodeError as e:
        # A cut multi-byte character at the end of the sample is fine.
        encoding = "utf-8-sig" if e.start >= len(sample) - 3 else "cp1250"
    return open(path, encoding=encoding, errors="replace", newline="")


def read_file(path: Path):
    """Rows of one published file as dicts of Harvey's columns (``cui``,
    the TEXT columns, the FLAGS as 1/0/None, ``vat_since``). Rows whose tax
    code isn't a CUI are left out."""
    with _open(path) as f:
        header = f.readline().rstrip("\r\n")
        delimiter = max("^;,|\t", key=header.count)
        names = [_key(h) for h in header.split(delimiter)]
        where = {}
        for column, aliases in COLUMNS.items():
            for alias in aliases:
                if alias in names:
                    where[column] = names.index(alias)
                    break
        if "cui" not in where:
            raise FormatChanged(f"{Path(path).name}: no COD_FISCAL column in its header")
        width = len(names)
        for line in f:
            parts = line.rstrip("\r\n").split(delimiter)
            if len(parts) == width + 1 and not parts[-1].strip():
                parts.pop()          # rows end with the separator
            if len(parts) < 2:
                continue

            def get(column):
                i = where.get(column)
                return parts[i].strip() if i is not None and i < len(parts) else ""

            cui = clean_cui(get("cui"))
            if not cui:
                continue
            row = {c: " ".join(get(c).split()) for c in TEXT}
            row["cui"] = cui
            row["parent_cui"] = clean_cui(row["parent_cui"])
            for c in ("registered", "status_date", "struck_off"):
                row[c] = iso_date(row[c]) or row[c]
            row["vat"], row["vat_since"] = flag(get("vat"))
            row["profit_tax"], _ = flag(get("profit_tax"))
            row["micro_tax"], _ = flag(get("micro_tax"))
            labour, _ = flag(get("labour_contribution"))
            wage, _ = flag(get("wage_tax"))
            known = [v for v in (labour, wage) if v is not None]
            row["employer"] = max(known) if known else None
            yield row


# ── Loading ──

@dataclass
class Source:
    """One published file to load: the part of the year's file it holds,
    where it is on disk, and where it came from."""
    part: str
    path: Path
    name: str = ""
    url: str = ""
    package: str = ""
    year: int = 0
    published: str = ""
    licence: str = ""


def _remove(path: Path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def build(sources: list[Source], target: Path | None = None, progress=None) -> dict:
    """Load these files into a fresh database at ``target``, one load at a
    time (TaxpayersBusy when another is running). Built in
    ``<target>.building`` and renamed over ``target`` once complete.
    ``progress(step, rows)`` is called as it goes. A CUI in two parts keeps
    the first one loaded. Returns the meta."""
    from harvey.pidfile import _try_lock, _unlock

    target = Path(target or TAXPAYERS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = open(target.with_name(target.name + ".lock"), "a+", encoding="utf-8")
    try:
        if not _try_lock(lock):
            raise TaxpayersBusy(f"another load of {target} is running; try again "
                                "when it has finished")
        try:
            return _build(sources, target, progress)
        finally:
            _unlock(lock)
    finally:
        lock.close()


COLUMN_ORDER = ("cui", *TEXT, "vat", "vat_since", "profit_tax", "micro_tax", "employer", "part")


def _build(sources, target: Path, progress) -> dict:
    building = target.with_name(target.name + ".building")
    _remove(building)
    started = time.monotonic()

    def say(step, rows=None):
        if progress:
            progress(step, rows)

    con = sqlite3.connect(building, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-200000")
        con.executescript(SCHEMA)
        con.execute("BEGIN")
        sql = (f"INSERT OR IGNORE INTO taxpayers ({', '.join(COLUMN_ORDER)}) "
               f"VALUES ({','.join('?' * len(COLUMN_ORDER))})")
        duplicates = 0
        for src in sorted(sources, key=lambda s: s.part):
            before = con.total_changes
            read = 0
            batch = []
            for row in read_file(src.path):
                row["part"] = src.part
                batch.append(tuple(row[c] for c in COLUMN_ORDER))
                read += 1
                if len(batch) >= LOAD_BATCH:
                    con.executemany(sql, batch)
                    batch = []
                    say(src.part, read)
            if batch:
                con.executemany(sql, batch)
            kept = con.total_changes - before
            duplicates += read - kept
            say(src.part, read)
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                        (src.part, src.name or src.path.name, src.url, src.package, src.year,
                         src.published, src.licence, src.path.stat().st_size, kept))
        say("current", None)
        from harvey.modules.ro.mf_taxpayers import derived

        derived.build(con)
        q = con.execute
        counts = {
            "rows": q("SELECT COUNT(*) FROM taxpayers").fetchone()[0],
            "with_phone": q("SELECT COUNT(*) FROM taxpayers WHERE phone != ''").fetchone()[0],
            "vat": q("SELECT COUNT(*) FROM taxpayers WHERE vat = 1").fetchone()[0],
            "employers": q("SELECT COUNT(*) FROM taxpayers WHERE employer = 1").fetchone()[0],
            "micro": q("SELECT COUNT(*) FROM taxpayers WHERE micro_tax = 1").fetchone()[0],
            "duplicates": duplicates,
        }
        years = sorted({s.year for s in sources if s.year}, reverse=True)
        published = max((s.published for s in sources), default="")
        meta = {
            "schema": SCHEMA_VERSION,
            "year": str(years[0]) if years else "",
            "published": published,
            "loaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "load_seconds": f"{time.monotonic() - started:.1f}",
            **{k: str(v) for k, v in counts.items()},
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        con.execute("COMMIT")
    except BaseException:
        con.close()
        _remove(building)
        raise
    finally:
        con.close()
    os.replace(building, target)
    return meta


# ── Reading ──

def connect(path: Path | None = None) -> sqlite3.Connection:
    """The database, read-only. FileNotFoundError when it isn't loaded."""
    path = Path(path or TAXPAYERS_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"no taxpayer file at {path}: run harvey taxpayers load")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def status(path: Path | None = None) -> dict | None:
    """The meta of the loaded database plus its files and size on disk, or
    None when there is none."""
    path = Path(path or TAXPAYERS_PATH)
    if not path.is_file():
        return None
    con = connect(path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        meta["files"] = [dict(r) for r in con.execute("SELECT * FROM files ORDER BY part")]
    finally:
        con.close()
    meta["bytes"] = path.stat().st_size
    meta["path"] = str(path)
    return meta


def taxpayer(con, cui: str) -> dict | None:
    """One CUI's row, with the file it came from (``source_url``,
    ``published``), or None."""
    row = con.execute(
        "SELECT t.*, COALESCE(f.url, '') AS source_url, COALESCE(f.published, '') AS published "
        "FROM taxpayers t LEFT JOIN files f ON f.part = t.part WHERE t.cui = ?",
        (cui,)).fetchone()
    return dict(row) if row else None


def address(row: dict) -> str:
    """One line from the address parts: street and number, block,
    staircase, floor, flat, details, sector, town, county, postal code."""
    street = " ".join(x for x in (row.get("street"), row.get("number") and
                                  f"nr. {row['number']}") if x)
    labelled = [(row.get("block"), "bl."), (row.get("staircase"), "sc."),
                (row.get("floor"), "et."), (row.get("apartment"), "ap.")]
    pieces = [street, *(f"{label} {v}" for v, label in labelled if v), row.get("details") or "",
              f"sector {row['sector']}" if row.get("sector") else "",
              row.get("locality") or "", row.get("county") or "", row.get("postal_code") or ""]
    seen, out = set(), []
    for piece in pieces:
        if piece and piece.lower() not in seen:
            seen.add(piece.lower())
            out.append(piece)
    return ", ".join(out)


# ── Filtering a registry search before anything is pulled ──

@dataclass
class Filter:
    """Yes/no conditions from the file: True wants the flag set, False
    wants it explicitly not set (on file and not registered), None doesn't
    ask. A firm not in the file matches neither."""
    employer: bool | None = None
    vat: bool | None = None
    micro: bool | None = None

    def active(self) -> bool:
        return any(v is not None for v in (self.employer, self.vat, self.micro))

    def parts(self) -> tuple[list[str], list]:
        """The conditions on a ``taxpayers`` row aliased ``t``, and their
        arguments."""
        parts, args = [], []
        for column, wanted in (("employer", self.employer), ("vat", self.vat),
                               ("micro_tax", self.micro)):
            if wanted is not None:
                parts.append(f"t.{column} = ?")
                args.append(1 if wanted else 0)
        return parts, args

    def sql(self, cui_column: str, alias: str = "tax") -> tuple[str, list]:
        """A WHERE condition on a row holding ``cui_column``, with the
        taxpayers database attached as ``alias``."""
        parts, args = self.parts()
        if not parts:
            return "1", []
        return (f"EXISTS (SELECT 1 FROM {alias}.taxpayers t WHERE t.cui = {cui_column} "
                f"AND {' AND '.join(parts)})", args)


def attach(con, path: Path | None = None, alias: str = "tax") -> bool:
    """Attach the taxpayers database to another read-only connection (the
    registry's) as ``alias``. False when there is none to attach."""
    path = Path(path or TAXPAYERS_PATH)
    if not path.is_file():
        return False
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    if alias not in attached:
        con.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{path}?mode=ro",))
    return True
