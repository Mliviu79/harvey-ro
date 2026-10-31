"""The Ministry of Finance's annual financial statements, in a local database.

The Ministry publishes every company's yearly balance sheet and income
statement figures on data.gov.ro, one set of files per year (source.py
finds and downloads them). ``build()`` loads the chosen years into their own
SQLite database, data/financials.db, beside the working one:

    accounts    one row per company (CUI) and year: the form it filed (BL
                long, UU abridged, IR IFRS), the CAEN class it declared, and
                the figures under Harvey's own names (turnover, employees,
                equity, ...). An empty cell is NULL (not reported), never 0.
    latest      per CUI, its latest year's turnover and headcount: what a
                registry search filters on before anything is pulled
    files       which published file each year and form came from (its
                address, publication date and licence)
    meta        when it was loaded, how long it took, counts

Each year's and form's column numbering is mapped to the named fields by
indicators.csv beside this file, so a column shift between years is one
line there and never reaches signal code. A new load is built in a
temporary file and renamed over the old one, so a reader never sees a
half-loaded database.

Only companies are in these files, identified by CUI and CAEN class: no
name, address or person. Sole traders and family businesses (PFA, II, IF)
keep single-entry books and file no balance sheet, so they are never here.
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harvey.modules.ro import cui as ro_cui
from harvey.paths import PROJECT_ROOT

FINANCIALS_PATH = PROJECT_ROOT / "data" / "financials.db"
INDICATORS = Path(__file__).with_name("indicators.csv")
SCHEMA_VERSION = "1"
FORMS = ("BL", "UU", "IR")
LOAD_BATCH = 50_000

# The named fields, in column order.
FIELDS = ("fixed_assets", "current_assets", "inventories", "receivables", "cash",
          "prepaid_expenses", "debts", "deferred_income", "provisions", "equity",
          "paid_capital", "turnover", "total_revenue", "total_expenses", "gross_profit",
          "gross_loss", "net_profit", "net_loss", "employees")

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE files (
    year INTEGER NOT NULL,
    form TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    package TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    licence TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    rows INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (year, form)
);

CREATE TABLE accounts (
    cui TEXT NOT NULL,
    year INTEGER NOT NULL,
    form TEXT NOT NULL,
    caen TEXT NOT NULL DEFAULT '',
    {", ".join(f"{f} INTEGER" for f in FIELDS)},
    PRIMARY KEY (cui, year)
) WITHOUT ROWID;
"""

LATEST = """
CREATE TABLE latest (
    cui TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    form TEXT NOT NULL,
    turnover INTEGER,
    employees INTEGER
) WITHOUT ROWID
"""
# SQLite takes the other columns from the row MAX() picked.
FILL_LATEST = """
INSERT INTO latest (cui, year, form, turnover, employees)
SELECT cui, MAX(year), form, turnover, employees FROM accounts GROUP BY cui
"""


class FinancialsBusy(RuntimeError):
    """Another load of the same database is running."""


class FormatChanged(ValueError):
    """A file's columns don't match what indicators.csv says for its year
    and form: the numbering changed, and the mapping needs a line."""


# ── The indicator mapping ──

_mapping_cache: list = [None, None]


def mapping(path: Path = INDICATORS) -> list[tuple[int, int, str, str, str]]:
    """(from_year, to_year, form, indicator, field) rows of indicators.csv."""
    path = Path(path)
    stamp = (str(path), path.stat().st_mtime_ns)
    if _mapping_cache[0] == stamp:
        return _mapping_cache[1]
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        lines = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
    for rec in csv.DictReader(lines):
        if rec["field"] not in FIELDS:
            raise ValueError(f"{path}: unknown field {rec['field']!r}")
        rows.append((int(rec["from_year"]), int(rec["to_year"]), rec["form"].upper(),
                     rec["indicator"].upper(), rec["field"]))
    _mapping_cache[:] = [stamp, rows]
    return rows


def columns_for(year: int, form: str, path: Path = INDICATORS) -> dict[str, str]:
    """Indicator column (I13) -> field (turnover) for one year and form."""
    return {ind: fld for y0, y1, f, ind, fld in mapping(path)
            if f == form and y0 <= year <= y1}


# ── Reading one published file ──

def clean_cui(raw: str) -> str:
    """The CUI as digits, no RO, no leading zeros; "" when it isn't one (a
    13-digit personal numeric code never is): harvey.modules.ro.cui."""
    return ro_cui.clean(raw)


def clean_caen(raw: str) -> str:
    """The CAEN class as 4 digits: the files drop the leading zero (111 is 0111)."""
    raw = (raw or "").strip()
    return raw.zfill(4) if raw.isdigit() and len(raw) <= 4 else ""


def _number(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return round(float(raw.replace(",", ".")))
        except ValueError:
            return None


def read_file(path: Path, year: int, form: str, indicators: Path = INDICATORS):
    """Rows of one published data file as (cui, year, form, caen, *FIELDS).
    Comma-separated, a header row naming CUI, CAEN and the indicator
    columns. FormatChanged when its columns aren't the ones indicators.csv
    maps for this year and form (the last column must be the headcount)."""
    wanted = columns_for(year, form, indicators)
    if not wanted:
        raise FormatChanged(f"indicators.csv has no columns for {form} {year}")
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = [h.strip().upper().lstrip("﻿") for h in next(reader, [])]
        if len(header) < 3 or header[0] != "CUI" or header[1] != "CAEN":
            raise FormatChanged(f"{path.name}: expected a CUI,CAEN,I1,... header")
        indicators_seen = [h for h in header[2:] if re.fullmatch(r"I\d+", h)]
        last = max(indicators_seen, key=lambda h: int(h[1:]), default="")
        missing = sorted(set(wanted) - set(header), key=lambda h: int(h[1:]))
        if missing or wanted.get(last) != "employees":
            raise FormatChanged(
                f"{path.name} ({form} {year}): its columns ({', '.join(header[2:])}) "
                f"don't match indicators.csv for that year and form; add a line for it")
        where = [(header.index(ind), FIELDS.index(fld)) for ind, fld in wanted.items()]
        for row in reader:
            if len(row) < 2:
                continue
            cui = clean_cui(row[0])
            if not cui:
                continue
            values: list = [None] * len(FIELDS)
            for col, slot in where:
                if col < len(row):      # a short row's missing cells stay unknown
                    values[slot] = _number(row[col])
            yield (cui, year, form, clean_caen(row[1]), *values)


# ── Loading ──

@dataclass
class Source:
    """One published file to load: which year and form it holds, where it
    is on disk, and where it came from."""
    year: int
    form: str
    path: Path
    name: str = ""
    url: str = ""
    package: str = ""
    published: str = ""
    licence: str = ""
    extra: dict = field(default_factory=dict)


def _remove(path: Path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def build(sources: list[Source], target: Path | None = None, progress=None,
          indicators: Path = INDICATORS) -> dict:
    """Load these files into a fresh database at ``target``, one load at a
    time (FinancialsBusy when another is running). Built in
    ``<target>.building`` and renamed over ``target`` once complete.
    ``progress(step, rows)`` is called as it goes. Where a company is in two
    forms' files for one year (rare), the first file loaded keeps it: BL,
    then IR, then UU. Returns the meta."""
    from harvey.pidfile import _try_lock, _unlock

    target = Path(target or FINANCIALS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = open(target.with_name(target.name + ".lock"), "a+", encoding="utf-8")
    try:
        if not _try_lock(lock):
            raise FinancialsBusy(f"another load of {target} is running; try again "
                                 "when it has finished")
        try:
            return _build(sources, target, progress, indicators)
        finally:
            _unlock(lock)
    finally:
        lock.close()


def _build(sources, target: Path, progress, indicators: Path) -> dict:
    building = target.with_name(target.name + ".building")
    _remove(building)
    started = time.monotonic()
    order = {f: i for i, f in enumerate(("BL", "IR", "UU"))}
    sources = sorted(sources, key=lambda s: (-s.year, order.get(s.form, 9)))

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
        marks = ",".join("?" * (4 + len(FIELDS)))
        sql = f"INSERT OR IGNORE INTO accounts VALUES ({marks})"
        duplicates = 0
        for src in sources:
            step = f"{src.form} {src.year}"
            before = con.total_changes
            read = 0
            batch = []
            for row in read_file(src.path, src.year, src.form, indicators):
                batch.append(row)
                read += 1
                if len(batch) >= LOAD_BATCH:
                    con.executemany(sql, batch)
                    batch = []
                    say(step, read)
            if batch:
                con.executemany(sql, batch)
            kept = con.total_changes - before
            duplicates += read - kept
            say(step, read)
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                        (src.year, src.form, src.name or src.path.name, src.url,
                         src.package, src.published, src.licence,
                         src.path.stat().st_size, kept))
        say("latest year per company")
        con.execute(LATEST)
        con.execute(FILL_LATEST)
        counts = {
            "rows": con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "companies": con.execute("SELECT COUNT(*) FROM latest").fetchone()[0],
            "duplicates": duplicates,
        }
        years = [r[0] for r in con.execute("SELECT DISTINCT year FROM files ORDER BY year DESC")]
        meta = {
            "schema": SCHEMA_VERSION,
            "years": " ".join(map(str, years)),
            "loaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "load_seconds": f"{time.monotonic() - started:.1f}",
            **{k: str(v) for k, v in counts.items()},
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        con.execute("COMMIT")
        con.execute("ANALYZE")
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
    path = Path(path or FINANCIALS_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"no financial statements at {path}: "
                                "run harvey financials load")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def status(path: Path | None = None) -> dict | None:
    """The meta of the loaded database plus its files and size on disk, or
    None when there is none."""
    path = Path(path or FINANCIALS_PATH)
    if not path.is_file():
        return None
    con = connect(path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        meta["files"] = [dict(r) for r in con.execute(
            "SELECT * FROM files ORDER BY year DESC, form")]
    finally:
        con.close()
    meta["bytes"] = path.stat().st_size
    meta["path"] = str(path)
    return meta


def final_release(year: int, package: str, published: str) -> bool:
    """Whether a year's file is its final release: a revision
    (``_actualizat``) or one published two years after the year or later
    (2023's in 2025). A year's first release (June of the next year) lacks
    the late filers, about one in seven, that the revision a year later
    adds."""
    if "actualizat" in (package or "").lower():
        return True
    published_year = re.match(r"(\d{4})", published or "")
    return bool(published_year) and int(published_year.group(1)) >= year + 2


def complete_years(con) -> set[int]:
    """Years loaded in full and final: the long, abridged and IFRS files all
    in, each its final release (final_release). Only for those does a
    company's absence mean it filed nothing; in a first release a late
    filer is missing still."""
    have: dict[int, set[str]] = {}
    for r in con.execute("SELECT year, form, package, published FROM files"):
        if final_release(r[0], r[2], r[3]):
            have.setdefault(r[0], set()).add(r[1])
    return {y for y, forms in have.items() if set(FORMS) <= forms}


def accounts_for(con, cui: str) -> list[dict]:
    """Every year on file for one CUI, newest first, with the file each came from."""
    rows = con.execute(
        "SELECT a.*, COALESCE(f.url, '') AS source_url, COALESCE(f.licence, '') AS licence "
        "FROM accounts a LEFT JOIN files f ON f.year = a.year AND f.form = a.form "
        "WHERE a.cui = ? ORDER BY a.year DESC", (cui,)).fetchall()
    return [dict(r) for r in rows]


def file_urls(con) -> dict[int, str]:
    """Year -> the address of one of its files (the long form's), the
    evidence a "filed nothing that year" reading points at."""
    out: dict[int, str] = {}
    for r in con.execute("SELECT year, form, url FROM files ORDER BY year, "
                         "CASE form WHEN 'BL' THEN 0 WHEN 'IR' THEN 1 ELSE 2 END DESC"):
        out[r[0]] = r[2]
    return out


# ── Filtering a registry search before anything is pulled ──

@dataclass
class Filter:
    """Bounds on a company's latest filed year. ``unknown``: "include" keeps
    companies with no figure (no statement on file, or the cell empty),
    "exclude" drops them."""
    min_turnover: float | None = None
    max_turnover: float | None = None
    min_employees: float | None = None
    max_employees: float | None = None
    unknown: str = "exclude"

    def active(self) -> bool:
        return any(v is not None for v in (self.min_turnover, self.max_turnover,
                                           self.min_employees, self.max_employees))

    def parts(self) -> tuple[list[str], list]:
        """The bounds on a ``latest`` row aliased ``l``, and their
        arguments (each part also says how a missing value is taken)."""
        return self._parts()

    def _parts(self) -> tuple[list[str], list]:
        """The bounds on a ``latest`` row aliased ``l``, and their arguments."""
        parts, args = [], []
        for column, low, high in (("turnover", self.min_turnover, self.max_turnover),
                                  ("employees", self.min_employees, self.max_employees)):
            if low is None and high is None:
                continue
            inner = []
            if low is not None:
                inner.append(f"l.{column} >= ?")
                args.append(low)
            if high is not None:
                inner.append(f"l.{column} <= ?")
                args.append(high)
            bound = " AND ".join(inner)
            if self.unknown == "include":
                parts.append(f"(l.{column} IS NULL OR ({bound}))")
            else:
                parts.append(f"(l.{column} IS NOT NULL AND {bound})")
        return parts, args

    def sql(self, cui_column: str, alias: str = "fin") -> tuple[str, list]:
        """A WHERE condition on a row holding ``cui_column``, with the
        financials database attached as ``alias``."""
        parts, args = self._parts()
        if not parts:
            return "1", []
        cond = " AND ".join(parts)
        if self.unknown == "include":
            # No row at all (never filed, or no CUI) is unknown too.
            return (f"(NOT EXISTS (SELECT 1 FROM {alias}.latest l WHERE l.cui = {cui_column})"
                    f" OR EXISTS (SELECT 1 FROM {alias}.latest l WHERE l.cui = {cui_column}"
                    f" AND {cond}))", args)
        return (f"EXISTS (SELECT 1 FROM {alias}.latest l WHERE l.cui = {cui_column} "
                f"AND {cond})", args)

    def in_sql(self, cui_column: str, alias: str = "fin") -> tuple[str, list]:
        """The same condition as ``sql()``, written as IN over the matching
        CUIs: SQLite reads them once and looks each company up by its CUI,
        instead of probing the statements for every row it scans."""
        parts, args = self._parts()
        if not parts:
            return "1", []
        matching = f"SELECT l.cui FROM {alias}.latest l WHERE {' AND '.join(parts)}"
        if self.unknown == "include":
            return (f"({cui_column} NOT IN (SELECT l.cui FROM {alias}.latest l) "
                    f"OR {cui_column} IN ({matching}))", args)
        return f"({cui_column} != '' AND {cui_column} IN ({matching}))", args


def attach(con, path: Path | None = None, alias: str = "fin") -> bool:
    """Attach the financials database to another read-only connection (the
    registry's) as ``alias``. False when there is none to attach."""
    path = Path(path or FINANCIALS_PATH)
    if not path.is_file():
        return False
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    if alias not in attached:
        con.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{path}?mode=ro",))
    return True
