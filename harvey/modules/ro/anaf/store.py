"""data/anaf.db in its compressed form (file version 2).

Version 1 (service.SCHEMA) keeps one row per CUI with ANAF's raw JSON as
text in a WITHOUT ROWID table: about 4.7 KB a row once the body spills to
overflow pages, and a refresh replaces the row, so the answer before is
lost. Version 2:

- ``answers``: one row per CUI (an integer key), when it was fetched (unix
  seconds), whether ANAF knew it, and the raw body compressed with zlib and
  a preset dictionary (``dict``), small enough to stay inline;
- ``answers_history``: every answer a refresh replaced with a different
  one (raw answers are never lost; two replaced in the same second both
  stay);
- ``answers_text``: an answer kept under a CUI that isn't a plain number
  (never asked by the job, which sends only valid CUIs, but a file may
  hold one), with its key as text;
- ``parsed``: the fields Harvey reads from an answer, so a cohort condition
  on them is an index read instead of decompressing every body;
- ``dict``: the preset dictionaries, by id. A row names the one it was
  compressed with, so a new dictionary never makes an old body unreadable.

The dictionary is the shape of an answer: every field name ANAF's
PlatitorTvaRest v9 writes, in its order, with neutral values (no data from
any real answer). ``rewrite()`` turns a version 1 file into a new version 2
file (the old one is only read), resumably, and marks it version 2 only
once it is whole (until then ``version()`` says 0 and the job refuses
it); ``verify()`` checks that every body decompresses to the exact
original.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

VERSION = 2
# The file's user_version while a rewrite is writing it.
PARTIAL = -VERSION
DICT_ID = 1
# A rewrite refuses to start with less free space than this.
MIN_FREE_BYTES = 30 * 10**9

# The shape of an answer (field names in ANAF's order, neutral values).
_SHAPE = {
    "date_generale": {
        "cui": 0, "data": "", "denumire": "", "adresa": "", "nrRegCom": "", "telefon": "",
        "fax": "", "codPostal": "", "act": "", "stare_inregistrare": "INREGISTRAT din data ",
        "data_inregistrare": "", "cod_CAEN": "", "iban": "", "statusRO_e_Factura": False,
        "organFiscalCompetent": "", "forma_de_proprietate": "", "forma_organizare": "",
        "forma_juridica": "", "data_inreg_Reg_RO_e_Factura": ""},
    "inregistrare_scop_Tva": {"scpTVA": False, "perioade_TVA": [
        {"data_inceput_ScpTVA": "", "data_sfarsit_ScpTVA": "", "data_anul_imp_ScpTVA": "",
         "mesaj_ScpTVA": ""}]},
    "inregistrare_RTVAI": {"dataInceputTvaInc": "", "dataSfarsitTvaInc": "",
                           "dataActualizareTvaInc": "", "dataPublicareTvaInc": "",
                           "tipActTvaInc": "", "statusTvaIncasare": False},
    "stare_inactiv": {"dataInactivare": "", "dataReactivare": "", "dataPublicare": "",
                      "dataRadiere": "", "statusInactivi": False},
    "inregistrare_SplitTVA": {"dataInceputSplitTVA": "", "dataAnulareSplitTVA": "",
                              "statusSplitTVA": False},
    "adresa_sediu_social": {
        "sdenumire_Strada": "", "snumar_Strada": "", "sdenumire_Localitate": "",
        "scod_Localitate": "", "sdenumire_Judet": "", "scod_Judet": "", "scod_JudetAuto": "",
        "stara": "", "sdetalii_Adresa": "", "scod_Postal": ""},
    "adresa_domiciliu_fiscal": {
        "ddenumire_Strada": "", "dnumar_Strada": "", "ddenumire_Localitate": "",
        "dcod_Localitate": "", "ddenumire_Judet": "", "dcod_Judet": "", "dcod_JudetAuto": "",
        "dtara": "", "ddetalii_Adresa": "", "dcod_Postal": ""},
}
# Words most answers carry, then the shape (zlib reaches the end of a
# dictionary most cheaply, so the shape goes last).
_COMMON = ('"false","true",null,"RO","ROMANIA","Municipiul ","Sector ","Str. ","Nr. ",'
           '"SOCIETATE COMERCIALA CU RASPUNDERE LIMITATA","PERSOANA JURIDICA",'
           '"PROPRIETATE PRIVATA-CAPITAL PRIVAT AUTOHTON","INREGISTRAT din data ",'
           '"Inregistrare","Radiere",')
DICTIONARIES = {DICT_ID: (_COMMON + json.dumps(_SHAPE, ensure_ascii=False,
                                               separators=(",", ":"))).encode()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    cui INTEGER PRIMARY KEY, fetched INTEGER NOT NULL, found INTEGER NOT NULL,
    dict INTEGER, body BLOB
);
CREATE TABLE IF NOT EXISTS answers_history (
    cui INTEGER NOT NULL, fetched INTEGER NOT NULL, found INTEGER NOT NULL, dict INTEGER,
    body BLOB
);
CREATE INDEX IF NOT EXISTS answers_history_cui ON answers_history(cui, fetched);
CREATE TABLE IF NOT EXISTS answers_text (
    cui TEXT PRIMARY KEY, fetched INTEGER NOT NULL, found INTEGER NOT NULL, dict INTEGER,
    body BLOB
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS parsed (
    cui INTEGER PRIMARY KEY, vat INTEGER, vat_since TEXT, vat_cash INTEGER, split_vat INTEGER,
    inactive INTEGER, efactura INTEGER, status TEXT, legal_form TEXT, phone TEXT, address TEXT
);
CREATE INDEX IF NOT EXISTS parsed_vat ON parsed(cui) WHERE vat = 1;
CREATE INDEX IF NOT EXISTS parsed_efactura ON parsed(cui) WHERE efactura = 1;
CREATE INDEX IF NOT EXISTS parsed_inactive ON parsed(cui) WHERE inactive = 1;
CREATE TABLE IF NOT EXISTS dict (id INTEGER PRIMARY KEY, zdict BLOB NOT NULL);
"""
PARSED = ("vat", "vat_since", "vat_cash", "split_vat", "inactive", "efactura", "status",
          "legal_form", "phone", "address")


class Unfinished(RuntimeError):
    """A file a rewrite is still writing (or was cut short writing)."""


def version(con: sqlite3.Connection) -> int:
    """1 for the plain file (or an empty one), 2 for this one, 0 for one a
    rewrite hasn't finished."""
    found = con.execute("PRAGMA user_version").fetchone()[0]
    return {VERSION: 2, PARTIAL: 0}.get(found, 1)


def create(con: sqlite3.Connection, *, finished: bool = True) -> None:
    """Make an empty database version 2 (its tables and dictionaries);
    ``finished=False`` marks it as a rewrite's, not yet whole."""
    con.executescript(SCHEMA)
    con.executemany("INSERT OR IGNORE INTO dict (id, zdict) VALUES (?, ?)",
                    list(DICTIONARIES.items()))
    con.execute(f"PRAGMA user_version = {VERSION if finished else PARTIAL}")
    con.commit()


def _key(cui) -> int | None:
    """A CUI as the integer key of ``answers``, or None (kept in answers_text)."""
    text = str(cui)
    return int(text) if text.isdigit() and text[0] != "0" and len(text) <= 18 else None


# ── Bodies ──

def compress(body: str, dict_id: int = DICT_ID) -> tuple[int | None, bytes | None]:
    """(dictionary id, compressed body); (None, None) for an empty body."""
    if not body:
        return None, None
    z = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zdict=DICTIONARIES[dict_id])
    return dict_id, z.compress(body.encode("utf-8")) + z.flush()


def decompress(dict_id: int | None, blob: bytes | None, zdicts: dict | None = None) -> str:
    if blob is None:
        return ""
    z = zlib.decompressobj(zdict=(zdicts or DICTIONARIES)[dict_id])
    return (z.decompress(blob) + z.flush()).decode("utf-8")


def to_unix(fetched_at: str) -> int:
    when = datetime.fromisoformat(fetched_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def to_iso(fetched: int) -> str:
    """The time as service.now() writes it."""
    return datetime.fromtimestamp(fetched, timezone.utc).isoformat(timespec="seconds")


# ── What an answer says, as columns ──

def parsed_fields(item: dict | None) -> tuple:
    """The ``parsed`` columns (in PARSED order) for an answer; for a CUI
    ANAF didn't know (``item`` None) every yes/no reads no, the rest none
    (a checked answer, as the adapter reads it)."""
    from harvey.modules.ro.anaf import service

    if item is None:
        return (0, None, 0, 0, 0, 0, None, None, None, None)

    def flag(raw):
        return int(raw) if isinstance(raw, bool) else None

    general = item.get("date_generale") or {}
    vat = item.get("inregistrare_scop_Tva") or {}
    since = service.vat_since(item) if vat.get("scpTVA") is True else ""
    form = " ".join(str(general.get("forma_juridica") or "").split())
    phones = " ".join(number for number, _ in service.phones(general.get("telefon")))
    return (flag(vat.get("scpTVA")), since or None,
            flag((item.get("inregistrare_RTVAI") or {}).get("statusTvaIncasare")),
            flag((item.get("inregistrare_SplitTVA") or {}).get("statusSplitTVA")),
            flag((item.get("stare_inactiv") or {}).get("statusInactivi")),
            flag(general.get("statusRO_e_Factura")),
            service.status_words(general.get("stare_inregistrare")) or None, form or None,
            phones or None, "\n".join(service.addresses(item)) or None)


def _item(body: str, found: int) -> dict | None:
    if not found:
        return None
    try:
        item = json.loads(body)
    except ValueError:
        return {}
    return item if isinstance(item, dict) else {}


# ── Writing answers (the ANAF job, on a version 2 file) ──

def keep(con: sqlite3.Connection, rows) -> int:
    """Keep answers ``(cui, fetched_at ISO, found, body)``: a CUI's answer
    replaces the one kept, which moves to answers_history when its body
    differs (a re-check that says the same only moves ``fetched``).
    Returns how many answers went to history. The caller commits."""
    moved = 0
    zdicts = dicts(con)
    for cui, fetched_at, found, body in rows:
        key, fetched = _key(cui), to_unix(fetched_at)
        table, cui = ("answers", key) if key is not None else ("answers_text", str(cui))
        old = con.execute(f"SELECT fetched, found, dict, body FROM {table} WHERE cui = ?",
                          (cui,)).fetchone()
        if old is not None:
            if old[1] == found and decompress(old[2], old[3], zdicts) == body:
                con.execute(f"UPDATE {table} SET fetched = MAX(fetched, ?) WHERE cui = ?",
                            (fetched, cui))
                continue
            if key is not None:
                moved += con.execute("INSERT INTO answers_history (cui, fetched, found, dict, "
                                     "body) VALUES (?, ?, ?, ?, ?)", (cui, *old)).rowcount
        dict_id, blob = compress(body)
        if key is None:
            # A key that isn't a number: kept whole, never parsed (nothing
            # reaches it by CUI). Its earlier answer is replaced in place.
            con.execute("INSERT OR REPLACE INTO answers_text (cui, fetched, found, dict, body) "
                        "VALUES (?, ?, ?, ?, ?)", (cui, fetched, found, dict_id, blob))
            continue
        con.execute("INSERT INTO answers (cui, fetched, found, dict, body) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (cui) DO UPDATE SET fetched = excluded.fetched, "
                    "found = excluded.found, dict = excluded.dict, body = excluded.body",
                    (cui, fetched, found, dict_id, blob))
        con.execute(f"INSERT OR REPLACE INTO parsed (cui, {', '.join(PARSED)}) "
                    f"VALUES (?{', ?' * len(PARSED)})", (cui, *parsed_fields(_item(body, found))))
    return moved


def dicts(con: sqlite3.Connection) -> dict:
    return {**DICTIONARIES, **dict(con.execute("SELECT id, zdict FROM dict").fetchall())}


def kept(con: sqlite3.Connection, cuis: list[str]) -> dict[str, tuple[str, int, str]]:
    """{cui: (fetched_at ISO, found, raw body)} for the CUIs kept."""
    zdicts = dicts(con)
    out = {}
    keys = [k for k in (_key(c) for c in cuis) if k is not None]
    texts = [str(c) for c in cuis if _key(c) is None]
    for table, wanted in (("answers", keys), ("answers_text", texts)):
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            for cui, fetched, found, dict_id, blob in con.execute(
                    f"SELECT cui, fetched, found, dict, body FROM {table} WHERE cui IN "
                    f"({','.join('?' * len(chunk))})", chunk):
                out[str(cui)] = (to_iso(fetched), found, decompress(dict_id, blob, zdicts))
    return out


# ── Rewriting a version 1 file ──

def _uri(path: Path, read_only: bool, immutable: bool = False) -> str:
    uri = "file:" + quote(str(Path(path).resolve()))
    return uri + ("?mode=ro" + ("&immutable=1" if immutable else "") if read_only else "")


def _digest(cui: str, fetched_at: str, found: int, body: str) -> int:
    h = hashlib.sha256(f"{cui}\x1f{fetched_at}\x1f{int(found)}\x1f".encode() + body.encode())
    return int.from_bytes(h.digest(), "big")


class Refused(RuntimeError):
    """A rewrite won't start: too little disk, or an output that is something else."""


def rewrite(src, dst, *, chunk: int = 20_000, immutable: bool = False, on_chunk=None,
            min_free: int = MIN_FREE_BYTES) -> dict:
    """Write a version 2 copy of the version 1 file ``src`` into ``dst``
    (``src`` is only read), resuming a run that was cut short. Every body
    is decompressed again before its chunk commits and compared with the
    original. The output says version 2 only once it is whole. Refuses to
    start with less than ``min_free`` bytes free (disk.free_bytes: on a
    virtual disk, the drive it lives on counts). Returns counts and the
    order-free digest of the originals."""
    from harvey import disk

    src, dst = Path(src), Path(dst)
    if min_free:
        try:
            free = disk.free_bytes(dst)
        except disk.HostDriveUnknown as e:
            raise Refused(str(e)) from e
        if free < min_free:
            raise Refused(f"{free / 1e9:.1f} GB free where {dst.name} would be written; the "
                          f"rewrite needs at least {min_free / 1e9:.0f} GB")
    con = sqlite3.connect(_uri(dst, False), uri=True, isolation_level=None)
    try:
        state = version(con)
        if state == 2 and not con.execute("SELECT 1 FROM sqlite_master "
                                          "WHERE name = 'rewrite_progress'").fetchone():
            raise Refused(f"{dst} is a version 2 file the job wrote, not a rewrite's output")
        if state == 1:
            if con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
                raise Refused(f"{dst} exists and isn't a rewrite's output: move it away first")
            create(con, finished=False)
        con.execute("CREATE TABLE IF NOT EXISTS rewrite_progress (id INTEGER PRIMARY KEY "
                    "CHECK (id = 1), last TEXT NOT NULL, rows INTEGER NOT NULL, "
                    "digest TEXT NOT NULL, unkeyable INTEGER NOT NULL, mismatched INTEGER "
                    "NOT NULL, time_text INTEGER NOT NULL, done INTEGER NOT NULL, "
                    "seconds REAL NOT NULL)")
        con.execute("INSERT OR IGNORE INTO rewrite_progress VALUES (1, '', 0, '0', 0, 0, 0, 0, 0)")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("ATTACH DATABASE ? AS old", (_uri(src, True, immutable),))
        last, rows, digest, unkeyable, mismatched, time_text, done, seconds = con.execute(
            "SELECT last, rows, digest, unkeyable, mismatched, time_text, done, seconds "
            "FROM rewrite_progress").fetchone()
        digest = int(digest)
        while not done:
            started = time.monotonic()
            batch = con.execute("SELECT cui, fetched_at, found, body FROM old.answers "
                                "WHERE cui > ? ORDER BY cui LIMIT ?", (last, chunk)).fetchall()
            out, parsed, texts = [], [], []
            for cui, fetched_at, found, body in batch:
                body = body or ""
                digest = (digest + _digest(cui, fetched_at, found, body)) % (1 << 256)
                fetched = to_unix(fetched_at)
                if to_iso(fetched) != fetched_at:
                    time_text += 1
                dict_id, blob = compress(body)
                if decompress(dict_id, blob) != body:
                    mismatched += 1
                key = _key(cui)
                if key is None:
                    # Kept whole under its text key, never left behind.
                    unkeyable += 1
                    texts.append((cui, fetched, found, dict_id, blob))
                    continue
                out.append((key, fetched, found, dict_id, blob))
                parsed.append((key, *parsed_fields(_item(body, found))))
            con.execute("BEGIN IMMEDIATE")
            try:
                con.executemany("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?)", out)
                con.executemany("INSERT OR REPLACE INTO answers_text VALUES (?, ?, ?, ?, ?)",
                                texts)
                con.executemany(f"INSERT OR REPLACE INTO parsed VALUES "
                                f"(?{', ?' * len(PARSED)})", parsed)
                done = int(len(batch) < chunk)
                last = batch[-1][0] if batch else last
                rows += len(batch)
                seconds += time.monotonic() - started
                con.execute("UPDATE rewrite_progress SET last = ?, rows = ?, digest = ?, "
                            "unkeyable = ?, mismatched = ?, time_text = ?, done = ?, seconds = ?",
                            (last, rows, str(digest), unkeyable, mismatched, time_text, done,
                             seconds))
                if done:
                    con.execute(f"PRAGMA user_version = {VERSION}")      # whole now
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            if on_chunk:
                on_chunk(rows)
        con.execute("DETACH DATABASE old")
        con.execute("ANALYZE")
    finally:
        con.close()
    return {"rows": rows, "text_keys": unkeyable, "mismatched": mismatched,
            "time_not_round_trip": time_text, "seconds": round(seconds, 1),
            "digest": f"{digest:064x}", "bytes": dst.stat().st_size}


def verify(src, dst, *, immutable: bool = False) -> dict:
    """Read both files in full: every answer of the version 1 file
    ``src`` and of the version 2 file ``dst`` (bodies decompressed, times
    written back as ISO) hashed per row and summed, order-free. Equal sums
    and counts: every raw answer came back exactly."""
    sums, counts = {}, {}
    old = sqlite3.connect(_uri(src, True, immutable), uri=True)
    try:
        total = 0
        for n, (cui, fetched_at, found, body) in enumerate(
                old.execute("SELECT cui, fetched_at, found, body FROM answers"), 1):
            total = (total + _digest(cui, fetched_at, found, body or "")) % (1 << 256)
            counts["original"] = n
        sums["original"] = total
    finally:
        old.close()
    new = sqlite3.connect(_uri(dst, True), uri=True)
    try:
        zdicts = dicts(new)
        total = 0
        for n, (cui, fetched, found, dict_id, blob) in enumerate(new.execute(
                "SELECT cui, fetched, found, dict, body FROM answers UNION ALL "
                "SELECT cui, fetched, found, dict, body FROM answers_text"), 1):
            body = decompress(dict_id, blob, zdicts)
            total = (total + _digest(str(cui), to_iso(fetched), found, body)) % (1 << 256)
            counts["rewritten"] = n
        sums["rewritten"] = total
        whole = version(new) == VERSION
        parsed = new.execute("SELECT COUNT(*) FROM parsed").fetchone()[0]
    finally:
        new.close()
    return {"original": counts.get("original", 0), "rewritten": counts.get("rewritten", 0),
            "parsed": parsed, "finished": whole, "equal": whole
            and sums["original"] == sums["rewritten"]
            and counts.get("original", 0) == counts.get("rewritten", 0)}
