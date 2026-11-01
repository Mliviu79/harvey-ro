"""The Companies page: one page of a filtered, sorted company list, and
everything on file about one company.

The list takes the same filters as the Contacts page and the contact
finder (contacts.ContactFilter, its company conditions), so a page, its
export, its ANAF check and its Find contacts run select the same companies.

Paging. With no filter and a sort the database keeps an index for (newest,
name, place), a page is read straight from that index with OFFSET: the
indexes (migration v23) are partial on live companies and cover the sort,
so skipping half a million rows walks the index alone. Any other page
(a filter, or a sort by a figure computed per company: turnover,
employees, contacts, last searched) is cut from an ordered list of the
matching companies' rowids, computed once and kept for a few minutes
(``Snapshots``): the first page pays for the whole ordering, every other
page, a jump to page 10,000 included, is a slice. The kept list also gives
the exact total. It is dropped when a company is added or merged, when a
table the list reads gains a row (a contact for a "no contact" list, a
search for "last searched"), or after SNAPSHOT_SECONDS.

The record (``record()``) reads the side databases (the registry, the
financial statements, the taxpayer file, ANAF's kept answers) for each of
the company's CUIs. A database that isn't loaded, or holds nothing for the
company, is said so in the payload ("not_loaded" / "none"), never an error.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from array import array
from dataclasses import astuple
from pathlib import Path

from harvey import contacts

# Live companies, written exactly as the partial indexes of v23 are, so
# SQLite can use them.
LIVE = "COALESCE(c.merged_into, '') = ''"
# The same, written so SQLite can't use those indexes: an ordering of most
# companies reads the table in order (a scan) faster than it reads it row
# by row in an index's order (measured: 2.0 s against 5.6 s for 1.78M).
LIVE_SCAN = "(COALESCE(c.merged_into, '') || '') = ''"

# The sorts a page offers. The three index sorts are columns; the others
# are a figure per company, ranked from the table that holds it (RANKS).
COLUMNS = {"created": "c.created_at", "name": "c.name", "place": "c.location"}
SORTS = ("created", "name", "place", "turnover", "employees", "contacts", "searched")
INDEXED_SORTS = tuple(COLUMNS)
FIGURE_SORTS = ("turnover", "employees")
# The rowid of every company that has a value, highest first (ties: the
# higher rowid first). A company with none sorts last both ways, except
# contacts: none is 0, first going up.
RANKS = {
    "turnover": "SELECT c.rowid FROM fin.latest l JOIN companies c ON c.cui = l.cui AND c.cui != '' "
                "WHERE l.turnover IS NOT NULL ORDER BY l.turnover DESC, c.rowid DESC",
    "employees": "SELECT c.rowid FROM fin.latest l JOIN companies c ON c.cui = l.cui AND c.cui != '' "
                 "WHERE l.employees IS NOT NULL ORDER BY l.employees DESC, c.rowid DESC",
    "contacts": "SELECT c.rowid FROM company_contacts l JOIN companies c ON c.id = l.company_id "
                "GROUP BY l.company_id ORDER BY COUNT(DISTINCT l.contact_id) DESC, c.rowid DESC",
    "searched": "SELECT c.rowid FROM searches s JOIN companies c ON c.id = s.target_id "
                "WHERE s.target = 'company' GROUP BY s.target_id "
                "ORDER BY MAX(s.searched_at) DESC, c.rowid DESC",
}
ZERO_FIRST = ("contacts",)
DEFAULT_DIR = {"created": "desc", "name": "asc", "place": "asc", "turnover": "desc",
               "employees": "desc", "contacts": "desc", "searched": "desc"}

SNAPSHOT_SECONDS = 300
SNAPSHOTS_KEPT = 3
# How long a ranking by a figure is reused at most (across filters and
# directions); it is dropped sooner when the table it ranks from changes.
RANK_SECONDS = {"turnover": 600, "employees": 600, "contacts": 300, "searched": 300}


def direction(sort: str, raw: str) -> str:
    raw = (raw or "").lower()
    return raw if raw in ("asc", "desc") else DEFAULT_DIR.get(sort, "desc")


def order_sql(sort: str, way: str, scan: bool = False) -> str:
    """ORDER BY for an index sort (ties by rowid, the same way). ``scan``:
    written so SQLite sorts what it found rather than walking the index."""
    column = COLUMNS.get(sort, COLUMNS["created"])
    way = "ASC" if way == "asc" else "DESC"
    return f" ORDER BY {'+' if scan else ''}{column} {way}, c.rowid {way}"


def filtered(f: contacts.ContactFilter) -> bool:
    """Whether the filter narrows the list at all (beyond live companies)."""
    blank = contacts.ContactFilter()
    return any(a != b for a, b in zip(astuple(f), astuple(blank)))


def where(f: contacts.ContactFilter, scan: bool = False) -> tuple[str, str, list]:
    """(WITH prefix, FROM ... WHERE over companies ``c``, parameters)."""
    with_sql, params = (f.cohort if f.cohort is not None else ("", []))
    conds, args = contacts.company_conditions(f)
    if scan and not f.places:
        # (Named places are an index lookup each: keep the form the place
        # index is written for.)
        conds[0] = LIVE_SCAN
    return with_sql, "FROM companies c WHERE " + " AND ".join(conds), [*params, *args]


class Snapshots:
    """Recent orderings, a few at a time, each dropped when its mark (what
    changes when it may have) differs or it is older than its seconds. An
    ordering is an array of rowids: 8 bytes a company, 14 MB for 1.78M.
    Every get and put also drops whatever has expired, so an old list
    doesn't stay in memory until something pushes it out."""

    def __init__(self, kept: int = SNAPSHOTS_KEPT, seconds: float = SNAPSHOT_SECONDS):
        self.kept, self.seconds = kept, seconds
        self._lock = threading.Lock()
        self._items: dict[tuple, tuple[float, float, tuple, object]] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, (made, ttl, _, _) in self._items.items() if now - made > ttl]:
            del self._items[key]

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def get(self, key: tuple, mark: tuple):
        with self._lock:
            self._sweep()
            hit = self._items.get(key)
            if not hit:
                return None
            made, ttl, made_mark, value = hit
            if made_mark != mark:
                del self._items[key]
                return None
            # Most recently used last.
            self._items[key] = self._items.pop(key)
            return value

    def put(self, key: tuple, mark: tuple, value, seconds: float | None = None) -> None:
        with self._lock:
            self._sweep()
            self._items.pop(key, None)
            self._items[key] = (time.monotonic(), seconds or self.seconds, mark, value)
            while len(self._items) > self.kept:
                self._items.pop(next(iter(self._items)))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


SNAPSHOTS = Snapshots()
RANKINGS = Snapshots(kept=len(RANKS) + 1)       # + every live company


async def _main_file(db) -> str:
    async with db.execute("PRAGMA database_list") as cur:
        return next((r[2] for r in await cur.fetchall() if r[1] == "main"), "")


async def _top(db, table: str) -> int:
    async with db.execute(f"SELECT MAX(rowid) FROM {table}") as cur:
        (top,) = await cur.fetchone()
    return top or 0


async def _mark(db) -> tuple:
    """What changes when any list may have: a company added (the highest
    rowid) or merged (how many are), in which database."""
    async with db.execute("SELECT COUNT(*) FROM companies WHERE merged_into != ''") as cur:
        (merged,) = await cur.fetchone()
    return (await _main_file(db), await _top(db, "companies"), merged)


# The tables a filter or a sort reads beyond companies, by what reads them:
# a row added to one (its highest rowid) may change the list.
def _depends(f: contacts.ContactFilter | None, sort: str) -> tuple[str, ...]:
    tables = []
    if f is not None:
        if f.contact:
            tables.append("company_contacts")
        if f.website:
            # A website is set by the contact finder (a search row) or by a
            # source that found the company again (a source row).
            tables += ["searches", "company_sources"]
        if f.found_in:
            tables.append("company_sources")
        if f.caen:
            tables.append("company_activities")
        if f.cohort is not None:
            tables.append("observations")
    if sort == "contacts":
        tables.append("company_contacts")
    if sort == "searched":
        tables.append("searches")
    return tuple(dict.fromkeys(tables))


async def _marks(db, f: contacts.ContactFilter | None, sort: str) -> tuple:
    """_mark plus the highest rowid of each table the list reads, so a
    company that gains a contact leaves a "no contact" list at once."""
    return (*await _mark(db), *[await _top(db, t) for t in _depends(f, sort)])


async def _rowids(db, sql: str, params) -> array:
    out = array("q")
    async with db.execute(sql, params) as cur:
        while True:
            rows = await cur.fetchmany(50_000)
            if not rows:
                return out
            out.extend(r[0] for r in rows)


async def _ranking(db, sort: str, figures_mark) -> array:
    """Rowids of every company with a value for ``sort``, highest first
    (ties: the higher rowid first), merged ones included. Kept across
    filters and directions until the table it ranks from changes."""
    key = (await _main_file(db), sort)
    mark = (figures_mark,) if sort in FIGURE_SORTS else \
        tuple([await _top(db, t) for t in _depends(None, sort)])
    hit = RANKINGS.get(key, mark)
    if hit is not None:
        return hit
    hit = await _rowids(db, RANKS[sort], ())
    RANKINGS.put(key, mark, hit, RANK_SECONDS[sort])
    return hit


class Ordered:
    """A list's order, kept once for both directions: ``have`` (highest
    first) and ``rest`` (companies with no value, in the list's tie order).
    Descending is have + rest; ascending is have reversed, then rest (no
    value sorts last both ways), or rest first when no value means 0."""

    def __init__(self, have: array, rest: array | None = None, zero_first: bool = False):
        self.have, self.rest, self.zero_first = have, rest or array("q"), zero_first

    def __len__(self) -> int:
        return len(self.have) + len(self.rest)

    def segments(self, way: str) -> list[tuple[array, bool]]:
        if way != "asc":
            return [(self.have, False), (self.rest, False)]
        if self.zero_first:
            return [(self.rest, False), (self.have, True)]
        return [(self.have, True), (self.rest, False)]

    def slice(self, way: str, offset: int, limit: int) -> list[int]:
        out: list[int] = []
        for seq, backwards in self.segments(way):
            n = len(seq)
            if offset >= n:
                offset -= n
                continue
            take = min(limit - len(out), n - offset)
            if backwards:
                start = n - offset
                out += reversed(seq[start - take:start])
            else:
                out += seq[offset:offset + take]
            offset = 0
            if len(out) >= limit:
                break
        return out


async def _live_base(db, mark) -> array:
    """Every live company's rowid, newest first, from the index (it holds
    the rowid: no table read). Kept while nothing is added or merged."""
    mark = mark[:3]
    hit = RANKINGS.get(("live", mark[0]), mark)
    if hit is None:
        _, body, params = where(contacts.ContactFilter())
        hit = await _rowids(db, f"SELECT c.rowid {body}{order_sql('created', 'desc')}", params)
        RANKINGS.put(("live", mark[0]), mark, hit)
    return hit


async def ordering(db, f: contacts.ContactFilter, sort: str, mark=None,
                   figures_mark=None) -> Ordered:
    """Every matching company's rowid, in the sort's descending order (an
    index sort: by that column, ties by rowid; a figure: highest first, the
    companies without one after them, newest first)."""
    if sort == "relevance":
        return Ordered(await _relevance(db, f))
    tie = sort if sort in COLUMNS else "created"
    if filtered(f):
        with_sql, body, params = where(f, scan=True)
        base = await _rowids(db, f"{with_sql}SELECT c.rowid {body}"
                                 f"{order_sql(tie, 'desc', scan=True)}", params)
    elif tie == "created":
        base = await _live_base(db, mark if mark is not None else await _mark(db))
    else:
        _, body, params = where(f)
        base = await _rowids(db, f"SELECT c.rowid {body}{order_sql(tie, 'desc')}", params)
    if sort in COLUMNS:
        return Ordered(base)
    ranked = await _ranking(db, sort, figures_mark)
    merged: set[int] = set()
    narrowed = filtered(f)
    if not narrowed:
        # Every live company: the ranking less the few merged ones.
        async with db.execute("SELECT rowid FROM companies WHERE merged_into != ''") as cur:
            merged = {r[0] for r in await cur.fetchall()}
    # Off the event loop: sets of up to 1.78M rowids take a second, and the
    # job worker and every other request share the loop.
    have, rest = await asyncio.to_thread(_split, ranked, base, narrowed, merged)
    return Ordered(have, rest, zero_first=sort in ZERO_FIRST)


def _split(ranked: array, base: array, narrowed: bool, merged: set) -> tuple[array, array]:
    """(the ranking's companies in the list, the list's companies without
    a value, in the list's order)."""
    valued = set(ranked)
    if narrowed:
        inside = set(base)
        have = array("q", (r for r in ranked if r in inside))
    else:
        have = array("q", (r for r in ranked if r not in merged)) if merged else ranked
    return have, array("q", (r for r in base if r not in valued))


async def _relevance(db, f: contacts.ContactFilter) -> array:
    """A search's matches under the other filters, best first (bm25: the
    name weighs most, then the activity, then the place), then by name."""
    match = f.search
    f.search = ""
    try:
        with_sql, body, params = where(f, scan=True)
    finally:
        f.search = match
    body = body.replace("FROM companies c WHERE ",
                        "FROM company_search JOIN companies c ON c.id = company_search.company_id "
                        "WHERE company_search MATCH ? AND ", 1)
    return await _rowids(db, f"{with_sql}SELECT c.rowid {body} ORDER BY "
                             "bm25(company_search, 0.0, 4.0, 2.0, 1.0), c.name", [match, *params])


async def page(db, f: contacts.ContactFilter, sort: str, way: str, limit: int,
               offset: int, figures_mark=None) -> tuple[int, list[int], str]:
    """(total, the page's company rowids in order, how it was read:
    "index" or "snapshot")."""
    if sort not in SORTS and not (sort == "relevance" and f.search):
        sort = "created"
    SNAPSHOTS.sweep()
    RANKINGS.sweep()
    if sort in INDEXED_SORTS and not filtered(f):
        _, body, params = where(f)
        async with db.execute(f"SELECT c.rowid {body}{order_sql(sort, way)} LIMIT ? OFFSET ?",
                              (*params, limit, offset)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        async with db.execute(f"SELECT COUNT(*) {body}", params) as cur:
            (total,) = await cur.fetchone()
        return total, ids, "index"
    key = (sort, repr(astuple(f)))
    mark = await _marks(db, f, sort)
    found = SNAPSHOTS.get(key, (mark, figures_mark))
    if found is None:
        found = await ordering(db, f, sort, mark, figures_mark)
        SNAPSHOTS.put(key, (mark, figures_mark), found)
    return len(found), found.slice(way, offset, limit), "snapshot"


async def ids_for(db, f: contacts.ContactFilter, sort: str, way: str,
                  limit: int, figures_mark=None) -> tuple[list[str], int]:
    """(up to ``limit`` company ids of the list in its order, how many it
    has): an export or a check of "every result"."""
    if sort not in SORTS and not (sort == "relevance" and f.search):
        sort = "created"
    found = await ordering(db, f, sort, figures_mark=figures_mark)
    rowids = found.slice(way, 0, limit)
    out: dict[int, str] = {}
    for at in range(0, len(rowids), 500):
        chunk = rowids[at:at + 500]
        async with db.execute(f"SELECT rowid, id FROM companies WHERE rowid IN "
                              f"({','.join('?' * len(chunk))})", chunk) as cur:
            out.update(await cur.fetchall())
    return [out[r] for r in rowids if r in out], len(found)


# Above this many places, the conditions fold every company's place instead
# (a bound parameter each; SQLite allows 32,766).
PLACES_MAX = 5000
PLACES = Snapshots(kept=2)


async def places(db, county: str, locality: str) -> tuple[str, ...] | None:
    """The distinct places (companies.location) in the county and locality,
    read one value at a time from the place index (an index seek each: a
    few thousand places, not 1.78M rows) and matched in Python exactly as
    the SQL fold would (contacts.place_matches). Kept until a company is
    added. None when there are more than PLACES_MAX."""
    key = (await _main_file(db),)
    mark = (await _top(db, "companies"),)
    every = PLACES.get(key, mark)
    if every is None:
        async with db.execute(
                "WITH RECURSIVE s(v) AS (SELECT MIN(c.location) FROM companies c WHERE "
                f"{LIVE} UNION ALL SELECT (SELECT MIN(c.location) FROM companies c WHERE "
                f"{LIVE} AND c.location > s.v) FROM s WHERE s.v IS NOT NULL) "
                "SELECT v FROM s WHERE v IS NOT NULL") as cur:
            every = [r[0] for r in await cur.fetchall()]
        PLACES.put(key, mark, every)
    found = tuple(p for p in every if contacts.place_matches(p, county, locality))
    return found if len(found) <= PLACES_MAX else None


async def sources(db) -> list[str]:
    """Every source companies were found in, read from the index one value
    at a time (a DISTINCT would walk all of it)."""
    async with db.execute(
            "WITH RECURSIVE s(v) AS (SELECT MIN(source) FROM company_sources "
            "UNION ALL SELECT (SELECT MIN(source) FROM company_sources WHERE source > s.v) "
            "FROM s WHERE s.v IS NOT NULL) SELECT v FROM s WHERE v IS NOT NULL LIMIT 500") as cur:
        return [r[0] for r in await cur.fetchall() if r[0]]


async def figures(db, cuis: list[str]) -> dict[str, dict]:
    """CUI -> its latest filed year {year, turnover, employees}, with the
    financial statements attached as ``fin``."""
    cuis = [c for c in dict.fromkeys(cuis) if c]
    if not cuis:
        return {}
    marks = ",".join("?" * len(cuis))
    async with db.execute(f"SELECT cui, year, turnover, employees FROM fin.latest "
                          f"WHERE cui IN ({marks})", cuis) as cur:
        return {r[0]: {"year": r[1], "turnover": r[2], "employees": r[3]}
                for r in await cur.fetchall()}


async def counts(db, ids: list[str]) -> dict[str, dict]:
    """Company id -> {contacts: distinct contacts, searched_at: last search}."""
    out = {i: {"contacts": 0, "searched_at": None} for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    async with db.execute(f"SELECT company_id, COUNT(DISTINCT contact_id) FROM company_contacts "
                          f"WHERE company_id IN ({marks}) GROUP BY company_id", ids) as cur:
        for cid, n in await cur.fetchall():
            out[cid]["contacts"] = n
    async with db.execute(f"SELECT target_id, MAX(searched_at) FROM searches WHERE target = 'company' "
                          f"AND target_id IN ({marks}) GROUP BY target_id", ids) as cur:
        for cid, when in await cur.fetchall():
            out[cid]["searched_at"] = when
    return out


# ── One company's record, from the side databases ──

NOT_LOADED, NONE, OK = "not_loaded", "none", "ok"


def _cuis(company: dict, ids: list[dict]) -> list[str]:
    from harvey.modules.ro import cui as ro_cui

    out = []
    for raw in [company.get("cui") or "", *(i["value"] for i in ids)]:
        clean = ro_cui.clean(raw)
        if clean and clean not in out:
            out.append(clean)
    return out


def _open(path: Path) -> sqlite3.Connection | None:
    path = Path(path)
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def registry_part(cuis: list[str], external_id: str, path: Path | None = None) -> dict:
    """The trade registry's rows for the company (by registration number,
    else CUI): identity, status, every CAEN class, administrators."""
    from harvey import registry

    con = _open(path or registry.REGISTRY_PATH)
    if con is None:
        return {"state": NOT_LOADED, "firms": []}
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        ids: list[int] = []
        reg = external_id[5:] if external_id.startswith("onrc:") else ""
        if reg:
            ids += [r[0] for r in con.execute("SELECT id FROM firms WHERE reg = ?", (reg,))]
        for cui in cuis:
            ids += [r[0] for r in con.execute(
                "SELECT id FROM firms WHERE cui = ? AND cui != ''", (cui,))]
        ids = list(dict.fromkeys(ids))[:10]
        firms = registry.firm_rows(con, ids)
        for firm in firms:
            firm["administrators"] = [
                {"name": f"{r[0]} {r[1]}".strip(), "role": r[2]} for r in con.execute(
                    "SELECT first_name, last_name, role FROM administrators WHERE firm_id = ? "
                    "ORDER BY rowid", (firm["id"],))]
        return {"state": OK if firms else NONE, "firms": firms,
                "dataset": meta.get("dataset", ""), "loaded_at": meta.get("loaded_at", "")}
    finally:
        con.close()


def financials_part(cuis: list[str], path: Path | None = None) -> dict:
    """Every year of financial statements on file for each CUI, newest first."""
    from harvey.modules.ro.mf_financials import store

    con = _open(path or store.FINANCIALS_PATH)
    if con is None:
        return {"state": NOT_LOADED, "years": []}
    try:
        years = []
        for cui in cuis:
            for r in store.accounts_for(con, cui):
                profit = None
                if r.get("net_profit") is not None or r.get("net_loss") is not None:
                    profit = (r.get("net_profit") or 0) - (r.get("net_loss") or 0)
                years.append({"cui": cui, "year": r["year"], "form": r["form"], "caen": r["caen"],
                              "turnover": r["turnover"], "employees": r["employees"],
                              "net_result": profit, "equity": r["equity"], "debts": r["debts"],
                              "total_revenue": r["total_revenue"],
                              "source_url": r.get("source_url") or ""})
        loaded = [r[0] for r in con.execute("SELECT DISTINCT year FROM files ORDER BY year DESC")]
        return {"state": OK if years else NONE, "years": years, "loaded_years": loaded}
    finally:
        con.close()


def taxpayer_part(cuis: list[str], path: Path | None = None) -> dict:
    """The taxpayer file's row for each CUI."""
    from harvey.modules.ro.mf_taxpayers import store

    con = _open(path or store.TAXPAYERS_PATH)
    if con is None:
        return {"state": NOT_LOADED, "rows": []}
    try:
        rows = []
        for cui in cuis:
            row = store.taxpayer(con, cui)
            if row:
                row["address"] = store.address(row)
                rows.append(row)
        return {"state": OK if rows else NONE, "rows": rows}
    finally:
        con.close()


def anaf_part(cuis: list[str], path: Path | None = None) -> dict:
    """ANAF's kept answer for each CUI, in plain fields."""
    from harvey.modules.ro.anaf import service

    path = Path(path or service.CACHE_PATH)
    if not path.is_file():
        return {"state": NOT_LOADED, "answers": []}
    try:
        # Either form of the file (service.kept reads both).
        kept = service.kept(path, list(cuis), read_only=True)
    except sqlite3.Error:
        kept = {}
    answers = []
    for cui in cuis:
        if cui not in kept:
            continue
        fetched_at, item = kept[cui]
        if item is None:
            answers.append({"cui": cui, "checked_at": fetched_at, "found": False})
            continue
        general = item.get("date_generale") or {}
        vat = item.get("inregistrare_scop_Tva") or {}
        inactive = item.get("stare_inactiv") or {}
        answers.append({
            "cui": cui, "checked_at": fetched_at, "found": True,
            "name": general.get("denumire") or "",
            "vat": vat.get("scpTVA"), "vat_since": service.vat_since(item),
            "efactura": general.get("statusRO_e_Factura"),
            "inactive": inactive.get("statusInactivi"),
            "status": service.status_words(general.get("stare_inregistrare")),
            "legal_form": general.get("forma_juridica") or "",
            "addresses": service.addresses(item),
            "phones": [display for _, display in service.phones(general.get("telefon"))],
        })
    return {"state": OK if answers else NONE, "answers": answers}


async def record(db, company: dict) -> dict:
    """Everything the side databases hold on the company, per CUI: the
    registry, financial years, ANAF's answer, the taxpayer file. Each part
    has a ``state``: ok, none (loaded, nothing on this company) or
    not_loaded."""
    async with db.execute("SELECT value, source, created_at FROM company_ids WHERE company_id = ? "
                          "AND kind = 'cui' ORDER BY created_at", (company["id"],)) as cur:
        ids = [{"value": r[0], "source": r[1] or "", "created_at": r[2]}
               for r in await cur.fetchall()]
    cuis = _cuis(company, ids)
    sources: dict[str, list[str]] = {c: [] for c in cuis}
    from harvey.modules.ro import cui as ro_cui
    for i in ids:
        clean = ro_cui.clean(i["value"])
        if clean in sources and i["source"] and i["source"] not in sources[clean]:
            sources[clean].append(i["source"])

    def read():
        return (registry_part(cuis, company.get("external_id") or ""), financials_part(cuis),
                anaf_part(cuis), taxpayer_part(cuis))

    reg, fin, anaf, tax = await asyncio.to_thread(read)
    return {"cuis": [{"cui": c, "sources": sources.get(c, [])} for c in cuis],
            "registry": reg, "financials": fin, "anaf": anaf, "taxpayer": tax,
            "signals": await signals(db, company["id"])}


# Shown in their own sections of the panel, not again among the signals.
OWN_SECTIONS = ("PHONE_NUMBER", "ADDRESS", "CONTACT_FOUND", "ACTIVITY_CODE")


async def signals(db, company_id: str) -> list[dict]:
    """The latest reading of each signal the company has (a yearly one's
    latest year), with its label, who observed it and when."""
    async with db.execute(
            "SELECT o.signal_code, o.value_num, o.value_text, o.module, o.observed_at, "
            "COALESCE(s.label, ''), COALESCE(s.value_type, 'text'), COALESCE(s.unit, ''), "
            "COALESCE(s.keyed_by, '') FROM observations o "
            "LEFT JOIN signal_codes s ON s.code = o.signal_code WHERE o.company_id = ? "
            "ORDER BY o.observed_at, o.rowid", (company_id,)) as cur:
        rows = await cur.fetchall()
    latest: dict[str, dict] = {}
    for code, num, text, module, seen, label, kind, unit, keyed in rows:
        if code in OWN_SECTIONS:
            continue
        year = text if keyed == "year" else ""
        old = latest.get(code)
        if old and year and old["year"] and not (year.isdigit() and old["year"].isdigit()
                                                 and int(year) >= int(old["year"])):
            continue
        latest[code] = {"code": code, "label": label or code, "value_type": kind, "unit": unit,
                        "value_num": num, "value_text": "" if year else (text or ""),
                        "year": year, "module": module or "", "observed_at": seen}
    return sorted(latest.values(), key=lambda x: x["code"])


# The yearly figures as observations name them, and the field each fills.
OBSERVED = {"TURNOVER": "turnover", "EMPLOYEES": "employees", "NET_RESULT": "net_result",
            "EQUITY": "equity", "DEBTS": "debts"}


async def observed_years(db, company_id: str) -> list[dict]:
    """Financial years from the company's observations (when the financial
    statements database isn't loaded, or doesn't have the company): one
    row per year, newest first, the last reading of each figure."""
    codes = list(OBSERVED)
    async with db.execute(
            f"SELECT signal_code, value_num, value_text, evidence_url FROM observations "
            f"WHERE company_id = ? AND signal_code IN ({','.join('?' * len(codes))}) "
            "AND value_num IS NOT NULL ORDER BY observed_at, rowid", (company_id, *codes)) as cur:
        rows = await cur.fetchall()
    years: dict[str, dict] = {}
    for code, value, year, url in rows:
        if not str(year or "").isdigit():
            continue
        entry = years.setdefault(year, {"cui": "", "year": int(year), "form": "", "caen": "",
                                        **{f: None for f in OBSERVED.values()},
                                        "total_revenue": None, "source_url": ""})
        entry[OBSERVED[code]] = int(value) if float(value).is_integer() else value
        entry["source_url"] = url or entry["source_url"]
    return sorted(years.values(), key=lambda e: -e["year"])
