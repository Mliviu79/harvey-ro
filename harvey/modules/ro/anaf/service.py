"""ANAF's public web service for a company's tax registration, and the
answers kept from it.

``POST https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva`` takes up to
100 CUIs as ``[{"cui": <int>, "data": "YYYY-MM-DD"}]`` and answers
``{"found": [...], "notFound": [...]}``: per company its general data (name,
address, phone, legal form, registration status, e-Factura), its VAT
registration and periods, VAT on cash basis, split VAT, and whether it is
fiscally inactive. ANAF allows one request a second; every request here
waits its turn in the ``anaf`` lane (harvey/modules/lanes.py), shared by
everything Harvey runs. A CUI must be digits: one sent as "RO123" makes ANAF
refuse the whole request (HTTP 400), so each is checked first.

Answers are kept in data/anaf.db, one row per CUI (a CUI ANAF doesn't know
too), so a company is asked about again only once its answer is older than
the module's ``cache_days``. The file is read and written in the form it
has: the plain one (version 1, below), or the compressed one with every
replaced answer kept (version 2, store.py), which the database upgrade
writes.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from harvey.paths import PROJECT_ROOT

API_URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
CACHE_PATH = PROJECT_ROOT / "data" / "anaf.db"
LANE = "anaf"
MAX_BATCH = 100
# Busy answers worth asking again, and how long to wait before each retry.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF = (2.0, 5.0, 10.0)
MAX_RETRY_AFTER = 60.0
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

class AnafError(RuntimeError):
    """ANAF couldn't be asked, or refused the request. ``busy`` when it
    stayed busy or couldn't be reached: asking again at once won't help,
    so a run stops instead of moving on to its next batch."""

    def __init__(self, message: str, *, busy: bool = False):
        super().__init__(message)
        self.busy = busy


# ── CUIs ──

def clean_cui(raw) -> str:
    """The CUI as ANAF takes it: digits, no RO, no leading zeros, with a
    valid check digit; "" otherwise (a 13-digit personal numeric code, a
    typo, a placeholder). harvey.modules.ro.cui.valid."""
    from harvey.modules.ro import cui as ro_cui

    return ro_cui.valid(raw)


# ── The request ──

async def ask(client, cuis: list[str], *, on: date | None = None,
              sleep=asyncio.sleep) -> tuple[dict[str, dict], set[str], int]:
    """Ask ANAF about up to MAX_BATCH clean CUIs in one request, waiting
    for the lane first. Returns ({cui: answer}, {cuis ANAF doesn't know},
    requests made). Retries with backoff only when ANAF is busy (429, 5xx),
    at most len(BACKOFF) times; anything else raises AnafError."""
    from harvey.modules import lanes

    if not cuis:
        return {}, set(), 0
    if len(cuis) > MAX_BATCH:
        raise ValueError(f"ANAF takes at most {MAX_BATCH} CUIs a request")
    when = (on or date.today()).isoformat()
    body = [{"cui": int(c), "data": when} for c in cuis]
    made = 0
    for attempt in range(len(BACKOFF) + 1):
        await lanes.wait(LANE)
        made += 1
        try:
            resp = await client.post(API_URL, json=body, headers=HEADERS)
        except Exception as e:     # httpx's transport errors, a timeout
            raise AnafError(f"ANAF unreachable ({e.__class__.__name__})", busy=True) from e
        if resp.status_code in RETRY_STATUSES and attempt < len(BACKOFF):
            wait = BACKOFF[attempt]
            after = resp.headers.get("retry-after", "")
            if after.strip().isdigit():
                wait = min(max(wait, float(after)), MAX_RETRY_AFTER)
            await sleep(wait)
            continue
        if resp.status_code in RETRY_STATUSES:
            raise AnafError(f"ANAF stayed busy (HTTP {resp.status_code}) after "
                            f"{made} requests", busy=True)
        if resp.status_code != 200:
            raise AnafError(f"ANAF refused the request (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError as e:
            raise AnafError("ANAF's answer isn't JSON") from e
        found, missing = parse(data)
        # A CUI asked about and in neither list is unknown to ANAF too.
        missing |= {c for c in cuis if c not in found}
        return found, missing, made
    raise AnafError("ANAF stayed busy", busy=True)    # not reached: the last attempt raises


def parse(data) -> tuple[dict[str, dict], set[str]]:
    """({cui: answer}, {cuis not found}) from ANAF's answer."""
    if not isinstance(data, dict):
        raise AnafError("ANAF's answer has no found/notFound lists")
    found: dict[str, dict] = {}
    for item in data.get("found") or []:
        general = (item or {}).get("date_generale") or {}
        cui = re.sub(r"\D", "", str(general.get("cui") or ""))
        if cui:
            found[cui] = item
    missing = set()
    for item in data.get("notFound") or []:
        raw = item.get("cui") if isinstance(item, dict) else item
        cui = re.sub(r"\D", "", str(raw or ""))
        if cui:
            missing.add(cui)
    return found, missing


# ── What an answer says ──

def iso_date(raw) -> str:
    """YYYY-MM-DD from the forms ANAF writes dates in, "" for none."""
    text = str(raw or "").strip()
    for pattern, order in ((r"(\d{4})-(\d{2})-(\d{2})", "ymd"), (r"(\d{2})\.(\d{2})\.(\d{4})", "dmy"),
                           (r"(\d{4})(\d{2})(\d{2})$", "ymd")):
        m = re.match(pattern, text)
        if m:
            y, mo, d = (m.group(1), m.group(2), m.group(3)) if order == "ymd" else (
                m.group(3), m.group(2), m.group(1))
            try:
                return date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                return ""
    return ""


def vat_since(item: dict) -> str:
    """When the company's current VAT registration started: the open
    period's start (no end date), else the latest start."""
    periods = ((item.get("inregistrare_scop_Tva") or {}).get("perioade_TVA")) or []
    starts = []
    for p in periods if isinstance(periods, list) else []:
        start = iso_date((p or {}).get("data_inceput_ScpTVA"))
        if not start:
            continue
        if not iso_date((p or {}).get("data_sfarsit_ScpTVA")):
            return start
        starts.append(start)
    return max(starts) if starts else ""


def status_words(raw) -> str:
    """The registration status without the date ANAF appends ("INREGISTRAT
    din data 01.02.2010" is "INREGISTRAT")."""
    text = re.split(r"\s+din\s+data\b", str(raw or ""), maxsplit=1, flags=re.I)[0]
    return " ".join(text.split())


def address(parts: dict, prefix: str) -> str:
    """One line from ANAF's address parts (``s`` registered office, ``d``
    fiscal domicile): street and number, details, town, county, postal code."""
    def get(name):
        return " ".join(str(parts.get(prefix + name) or "").split())

    street = " ".join(x for x in (get("denumire_Strada"), get("numar_Strada")) if x)
    pieces = [street, get("detalii_Adresa"), get("denumire_Localitate"),
              get("denumire_Judet"), get("cod_Postal")]
    seen, out = set(), []
    for piece in pieces:
        if piece and piece.lower() not in seen:
            seen.add(piece.lower())
            out.append(piece)
    return ", ".join(out)


def addresses(item: dict) -> list[str]:
    """The company's registered office, and its fiscal domicile when it is
    elsewhere; ANAF's one-line address when the parts are missing."""
    out = []
    for key, prefix in (("adresa_sediu_social", "s"), ("adresa_domiciliu_fiscal", "d")):
        line = address(item.get(key) or {}, prefix)
        if line and line.lower() not in {x.lower() for x in out}:
            out.append(line)
    if not out:
        line = " ".join(str(((item.get("date_generale") or {}).get("adresa")) or "").split())
        if line:
            out.append(line)
    return out


def phones(raw) -> list[tuple[str, str]]:
    """(E.164, display) for each number in ANAF's phone field."""
    from harvey.collectors.phones import field_phones

    return [(hit.number, hit.display) for hit in field_phones(raw, "RO")]


# ── Kept answers ──

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    cui TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    found INTEGER NOT NULL,
    body TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID
"""


def _connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    try:
        # A write-ahead log: a batch's answers kept without waiting for the
        # disk each time (an answer lost to a power cut is only asked again),
        # and readers in other processes never wait for the writer.
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass            # another process is switching it at this moment
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(SCHEMA)
    return con


def kept(path: Path, cuis: list[str], *,
         read_only: bool = False) -> dict[str, tuple[str, dict | None]]:
    """{cui: (fetched_at, answer or None when ANAF didn't know it)} for the
    CUIs with a kept answer. ``read_only``: open the file only to read it
    (a page showing a company), never to set it up."""
    if not cuis or not Path(path).is_file():
        return {}
    out = {}
    con = (sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
           if read_only else _connect(path))
    try:
        from harvey.modules.ro.anaf import store

        form = store.version(con)
        if form == 0:
            raise store.Unfinished(f"{path} is a rewrite that hasn't finished")
        if form == store.VERSION:
            for cui, (fetched_at, found, body) in store.kept(con, cuis).items():
                try:
                    out[cui] = (fetched_at, json.loads(body) if found else None)
                except ValueError:
                    continue
            return out
        for start in range(0, len(cuis), 900):
            chunk = cuis[start:start + 900]
            rows = con.execute(f"SELECT cui, fetched_at, found, body FROM answers WHERE cui IN "
                               f"({','.join('?' * len(chunk))})", chunk).fetchall()
            for cui, fetched_at, found, body in rows:
                try:
                    answer = json.loads(body) if found else None
                except ValueError:
                    continue
                out[cui] = (fetched_at, answer)
    finally:
        con.close()
    return out


def keep(path: Path, found: dict[str, dict], missing: set[str], when: str | None = None) -> None:
    """Keep ANAF's answers (and the CUIs it didn't know) as of ``when``."""
    when = when or now()
    rows = [(cui, when, 1, json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            for cui, item in found.items()]
    rows += [(cui, when, 0, "") for cui in missing if cui not in found]
    if not rows:
        return
    con = _connect(path)
    try:
        from harvey.modules.ro.anaf import store

        form = store.version(con)
        if form == 0:
            raise store.Unfinished(f"{path} is a rewrite that hasn't finished")
        if form == store.VERSION:
            # The answer replaced goes to history when it said something else.
            store.keep(con, rows)
        else:
            con.executemany("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()


def fresh(fetched_at: str, days: int, at: datetime | None = None) -> bool:
    try:
        then = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (at or datetime.now(timezone.utc)) - then < timedelta(days=days)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status(path: Path | None = None) -> dict:
    """How many answers are kept, how many found, and the newest."""
    path = Path(path or CACHE_PATH)
    if not path.is_file():
        return {"answers": 0, "found": 0, "latest": ""}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        from harvey.modules.ro.anaf import store

        if store.version(con) == store.VERSION:
            n, found, latest = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(found), 0), MAX(fetched) FROM answers").fetchone()
            latest = store.to_iso(latest) if latest is not None else ""
        else:
            n, found, latest = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(found), 0), COALESCE(MAX(fetched_at), '') "
                "FROM answers").fetchone()
    except sqlite3.Error:
        return {"answers": 0, "found": 0, "latest": ""}
    finally:
        con.close()
    return {"answers": n, "found": found, "latest": latest}
