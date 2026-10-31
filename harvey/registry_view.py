"""The Registry page: the whole trade registry (data/registry.db) browsed
with each firm's latest financial statements beside it, filtered the way a
registry pull is, sorted, and paged like the Companies page.

The filters are the registry source's own (the onrc module's manifest,
read by job_kinds.registry_params), resolved by the registry's own search
(registry.resolve): the page lists exactly the firms a pull of the same
filters would save, and can pull them.

Paging. The first request for a filter and sort orders every matching
firm once, as an array of registry ids, and keeps it for a few minutes
(companies.Snapshots); every other page, a jump to page 30,000 included, is
a slice of it, and its length is the exact total. Registry ids follow the
firms' names, so the name order is the full-text index's own. A sort by a
figure (turnover, employees, profit: the latest filed year's) or by
registration date reads a ranking of every firm that has one, computed
once per loaded database and kept until the registry or the statements
are loaded again; a filter's list is that ranking less the firms outside
the filter, then the firms with no value, in name order (no value sorts
last both ways).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from array import array
from collections import deque
from itertools import compress, repeat
from pathlib import Path

from harvey import registry
from harvey.companies import Ordered, Snapshots

SORTS = ("name", "turnover", "employees", "profit", "registered")
FIGURE_SORTS = ("turnover", "employees", "profit")
DEFAULT_DIR = {"name": "asc", "turnover": "desc", "employees": "desc", "profit": "desc",
               "registered": "desc"}

# Every firm with a value, highest first; ties in name order.
RANKS = {
    "turnover": "SELECT f.id FROM fin.latest l JOIN firms f ON f.cui = l.cui AND f.cui != '' "
                "WHERE l.turnover IS NOT NULL ORDER BY l.turnover DESC, f.id",
    "employees": "SELECT f.id FROM fin.latest l JOIN firms f ON f.cui = l.cui AND f.cui != '' "
                 "WHERE l.employees IS NOT NULL ORDER BY l.employees DESC, f.id",
    # The latest year's net result: profit less loss, when either is filed.
    "profit": "SELECT f.id FROM fin.latest l "
              "JOIN fin.accounts a ON a.cui = l.cui AND a.year = l.year "
              "JOIN firms f ON f.cui = l.cui AND f.cui != '' "
              "WHERE a.net_profit IS NOT NULL OR a.net_loss IS NOT NULL "
              "ORDER BY COALESCE(a.net_profit, 0) - COALESCE(a.net_loss, 0) DESC, f.id",
    # Read in order from the registry's own index (registry.index_registered).
    "registered": registry.REGISTERED_SORT,
}

# Registry ids (a few million) fit in 32 bits: kept lists take half the
# memory 64-bit ones would.
IDS = "i"
SNAPSHOTS = Snapshots(kept=4)
# Rankings and the list of every operating firm change only when a
# database is loaded again (their mark says when): kept a day at most.
RANKINGS = Snapshots(kept=len(RANKS) + 1, seconds=24 * 3600)
# A registry this big has its operating list and rankings computed in the
# background when the page is first opened (1-2 s each at 4M firms, after
# the registration-date index is added to a registry loaded before it
# existed); a small one computes them when asked, at once.
WARM_FIRMS = 200_000

logger = logging.getLogger(__name__)
_warmed: set[tuple] = set()
# One ranking is computed at a time: two at once each take as long as both
# (they share the interpreter and the disk). A page request goes first: the
# background warm-up starts nothing new while one is being answered.
_computing = threading.Lock()
_asked = threading.Condition()
_asking = 0
_background = threading.local()


@contextmanager
def _answering():
    """A page request is being answered: background warm-up waits."""
    global _asking
    with _asked:
        _asking += 1
    try:
        yield
    finally:
        with _asked:
            _asking -= 1
            _asked.notify_all()


def _kept(key: tuple, mark: tuple, compute):
    """RANKINGS[key], computed once (a second asker waits for the first)."""
    hit = RANKINGS.get(key, mark)
    if hit is not None:
        return hit
    if getattr(_background, "on", False):
        with _asked:
            _asked.wait_for(lambda: _asking == 0)
    with _computing:
        hit = RANKINGS.get(key, mark)
        if hit is None:
            hit = compute()
            RANKINGS.put(key, mark, hit)
    return hit

PAGE_MAX = 200
# Up to this many firms, financial and taxpayer conditions are checked firm
# by firm; above it, from the statements' and the taxpayer file's side
# (every firm they keep, nationwide, as a byte per registry id).
PROBE_MAX = 150_000


def direction(sort: str, raw: str) -> str:
    raw = (raw or "").lower()
    return raw if raw in ("asc", "desc") else DEFAULT_DIR.get(sort, "asc")


def _paths() -> tuple[Path, Path, Path]:
    from harvey.modules.ro.mf_financials import store as fin_store
    from harvey.modules.ro.mf_taxpayers import store as tax_store

    return (Path(registry.REGISTRY_PATH), Path(fin_store.FINANCIALS_PATH),
            Path(tax_store.TAXPAYERS_PATH))


def _stamp(path: Path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def loaded() -> dict:
    """Which of the databases the page reads are loaded."""
    reg, fin, tax = _paths()
    return {"registry": reg.is_file(), "financials": fin.is_file(), "taxpayers": tax.is_file()}


def connect() -> sqlite3.Connection:
    """The registry, read-only, with the statements and the taxpayer file
    attached when they are loaded. FileNotFoundError without a registry."""
    from harvey.modules.ro.mf_financials import store as fin_store
    from harvey.modules.ro.mf_taxpayers import store as tax_store

    reg, fin, tax = _paths()
    con = registry.connect(reg)
    fin_store.attach(con, fin)
    tax_store.attach(con, tax)
    return con


def resolve(con, params: dict) -> registry.Query:
    """A registry pull's parameters (job_kinds.registry_params) as the
    registry's resolved query."""
    from harvey.modules.job_kinds import _filters

    fin, tax = _filters(params)
    return registry.resolve(con, params.get("words") or "",
                            counties=params.get("counties") or None,
                            localities=params.get("localities") or None,
                            all_statuses=bool(params.get("all_statuses")), fin=fin, tax=tax,
                            regs=params.get("regs"))


def _read_ids(con, sql: str, args=()) -> array:
    out = array(IDS)
    cur = con.cursor()
    cur.row_factory = None          # plain tuples: Row objects cost a third more
    cur.execute(sql, args)
    while True:
        rows = cur.fetchmany(100_000)
        if not rows:
            return out
        out.extend(r[0] for r in rows)


def _top(con) -> int:
    return con.execute("SELECT MAX(id) FROM firms").fetchone()[0] or 0


def _every_firm(con) -> array:
    """Every firm's id, in name order. A loaded registry numbers its firms
    1..N, so that is a range, not 4 million rows read."""
    low, high, n = con.execute("SELECT MIN(id), MAX(id), COUNT(*) FROM firms").fetchone()
    if n and high - low + 1 == n:
        return array(IDS, range(low, high + 1))
    return _read_ids(con, "SELECT id FROM firms ORDER BY id")


def _mark_ids(flags: bytearray, ids, value: int = 1) -> None:
    """flags[i] = value for each id (looped in C: a third faster)."""
    deque(map(flags.__setitem__, ids, repeat(value)), maxlen=0)


def _keep(ids: array, flags: bytearray) -> array:
    """The ids whose flag is set, in their order."""
    return array(IDS, compress(ids, map(flags.__getitem__, ids)))


def _passing(con, q: registry.Query) -> bytearray | None:
    """A byte per registry id, 1 for the firms the query's financial
    bounds and taxpayer conditions keep (registry._where's conditions,
    read from the statements' and the taxpayer file's side: the firms they
    name, looked up by CUI, instead of a probe per firm); None when there
    are none. FileNotFoundError when a database they need isn't loaded."""
    from harvey.modules.ro.mf_financials import store as fin_store
    from harvey.modules.ro.mf_taxpayers import store as tax_store

    fin, tax = q.fin, q.tax
    fin_on = fin is not None and fin.active()
    tax_on = tax is not None and tax.active()
    if not (fin_on or tax_on):
        return None
    _, fin_path, tax_path = _paths()
    size = _top(con) + 1
    keep = None
    if fin_on:
        if not fin_store.attach(con, q.fin_path or fin_path):
            raise FileNotFoundError("no financial statements loaded: harvey financials load")
        parts, args = fin.parts()
        join = "FROM fin.latest l JOIN firms f ON f.cui = l.cui AND f.cui != '' WHERE "
        if fin.unknown == "include":
            # Kept unless a statement on file fails a bound (no statement,
            # or no figure, is unknown: kept).
            keep = bytearray(b"\x01") * size
            _mark_ids(keep, _read_ids(con, f"SELECT f.id {join} NOT ({' AND '.join(parts)})",
                                      args), 0)
        else:
            keep = bytearray(size)
            _mark_ids(keep, _read_ids(con, f"SELECT f.id {join} {' AND '.join(parts)}", args))
    if tax_on:
        if not tax_store.attach(con, q.tax_path or tax_path):
            raise FileNotFoundError("no taxpayer file loaded: harvey taxpayers load")
        parts, args = tax.parts()
        found = bytearray(size)
        _mark_ids(found, _read_ids(
            con, "SELECT f.id FROM tax.taxpayers t JOIN firms f ON f.cui = t.cui "
                 f"AND f.cui != '' WHERE {' AND '.join(parts)}", args))
        if keep is None:
            keep = found
        else:
            both = int.from_bytes(keep, "little") & int.from_bytes(found, "little")
            keep = bytearray(both.to_bytes(size, "little"))
    return keep


def matching(con, q: registry.Query) -> array:
    """Every firm the query matches, in name order. With no words, area or
    selection: every operating firm (every firm, with all statuses)."""
    if q.firm_ids is not None:
        return array(IDS, sorted(q.firm_ids))
    match = q.match
    if not match and not q.all_statuses:
        match = f"tags : {registry.OPERATING_TAG}"
    if match == f"tags : {registry.OPERATING_TAG}":
        base = _operating(con)
    elif match and match != registry.EVERY:
        base = _read_ids(con, "SELECT rowid FROM firm_fts WHERE firm_fts MATCH ? ORDER BY rowid",
                         (match,))
    else:
        base = _every_firm(con)
        if q.excluded:
            left = bytearray(b"\x01") * (_top(con) + 1)
            _mark_ids(left, _read_ids(con, "SELECT rowid FROM firm_fts WHERE firm_fts MATCH ?",
                                      (q.excluded,)), 0)
            base = _keep(base, left)
    cond, args = registry._where(con, q, exclusions=False)
    if not cond:
        return base
    if len(base) <= PROBE_MAX:
        # A narrow list: each firm's figures looked up by its CUI.
        out = array(IDS)
        cur = con.cursor()
        cur.row_factory = None
        for at in range(0, len(base), 500):
            chunk = base[at:at + 500]
            cur.execute(f"SELECT f.id FROM firms f WHERE f.id IN ({','.join('?' * len(chunk))}) "
                        f"AND {cond} ORDER BY f.id", (*chunk, *args))
            out.extend(r[0] for r in cur.fetchall())
        return out
    return _keep(base, _passing(con, q))


def _mark() -> tuple:
    reg, fin, tax = _paths()
    return (_stamp(reg), _stamp(fin), _stamp(tax))


def _operating(con) -> array:
    """Every operating firm, in name order (the list most filters start
    from), kept until the registry is loaded again."""
    reg, _, _ = _paths()
    return _kept((str(reg), "operating"), (_stamp(reg),), lambda: _read_ids(
        con, "SELECT rowid FROM firm_fts WHERE firm_fts MATCH ? ORDER BY rowid",
        (f"tags : {registry.OPERATING_TAG}",)))


def ranking(con, sort: str) -> array:
    """Every firm with a value for ``sort``, highest first, kept until the
    registry or the statements are loaded again."""
    reg, fin, _ = _paths()
    mark = (_stamp(reg), _stamp(fin) if sort in FIGURE_SORTS else None)
    return _kept((str(reg), sort), mark, lambda: _read_ids(con, RANKS[sort]))


def warm() -> bool:
    """Compute the operating list and the rankings in a background thread
    (adding the registration-date index first when the registry lacks it),
    once per loaded registry, when it is big enough to need it. True when
    a thread was started."""
    reg, fin, _ = _paths()
    mark = (str(reg), _stamp(reg), _stamp(fin))
    if mark in _warmed or mark[1] is None:
        return False
    _warmed.add(mark)
    try:
        con = registry.connect(reg)
        try:
            firms = int(dict(con.execute("SELECT key, value FROM meta").fetchall())
                        .get("firms") or 0)
        finally:
            con.close()
    except (sqlite3.Error, ValueError, FileNotFoundError):
        return False
    if firms < WARM_FIRMS:
        return False
    threading.Thread(target=_warm, name="registry-rankings", daemon=True).start()
    return True


def _warm() -> None:
    _background.on = True
    started = time.monotonic()
    reg, _, _ = _paths()
    try:
        if registry.ensure_registered_index(reg):
            logger.info("registry: registration-date index added in %.1fs",
                        time.monotonic() - started)
    except (sqlite3.Error, OSError) as e:     # read-only, busy: the sort still works, slower
        logger.warning("registry: registration-date index not added: %s", e)
    try:
        con = connect()
        try:
            _operating(con)
            fin_loaded = "fin" in {r[1] for r in con.execute("PRAGMA database_list")}
            for sort in RANKS:
                if sort in FIGURE_SORTS and not fin_loaded:
                    continue
                ranking(con, sort)
        finally:
            con.close()
        logger.info("registry rankings ready in %.1fs", time.monotonic() - started)
    except Exception as e:      # a reload under it, a database gone: asked for again later
        logger.warning("registry rankings not computed: %s", e)


def _split(ranked: array, base: array, top: int) -> tuple[array, array]:
    """(the ranking's firms in ``base``, in the ranking's order; ``base``'s
    firms with no value, in name order). Membership is a byte per registry
    id (``top`` the highest): a few MB, and no sets of millions of ints."""
    if len(base) == top and (not base or base[0] == 1):
        have = ranked                   # every firm: the whole ranking
    else:
        inside = bytearray(top + 1)
        _mark_ids(inside, base)
        have = _keep(ranked, inside)
    none = bytearray(b"\x01") * (top + 1)
    _mark_ids(none, have, 0)
    return have, _keep(base, none)


def ordering(con, q: registry.Query, sort: str) -> Ordered:
    """The query's firms in the sort's descending order (Ordered: both
    directions from one list)."""
    base = matching(con, q)
    if sort == "name":
        return Ordered(base[::-1])
    have, rest = _split(ranking(con, sort), base, _top(con))
    return Ordered(have, rest)


def _key(params: dict, sort: str) -> tuple:
    reg, _, _ = _paths()
    shown = {k: v for k, v in params.items()
             if k not in ("path", "data_dir", "label", "area", "ensure_latest", "dry_run")}
    return (str(reg), sort, repr(sorted(shown.items(), key=lambda kv: kv[0])))


def ordered(con, params: dict, sort: str) -> tuple[Ordered, registry.Query, str]:
    """(the kept ordering for these filters and sort, the resolved query,
    how it was read: "kept" when it was already computed, else "computed")."""
    q = resolve(con, params)
    key, mark = _key(params, sort), _mark()
    found = SNAPSHOTS.get(key, mark)
    how = "kept"
    if found is None:
        found = ordering(con, q, sort)
        SNAPSHOTS.put(key, mark, found)
        how = "computed"
    return found, q, how


def figures(con, cuis: list[str]) -> dict[str, dict]:
    """CUI -> its latest filed year {year, form, turnover, employees,
    net_result}, when the statements are attached."""
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    cuis = [c for c in dict.fromkeys(cuis) if c]
    if "fin" not in attached or not cuis:
        return {}
    marks = ",".join("?" * len(cuis))
    out = {}
    for r in con.execute(
            "SELECT a.cui, a.year, a.form, a.turnover, a.employees, a.net_profit, a.net_loss "
            "FROM fin.latest l JOIN fin.accounts a ON a.cui = l.cui AND a.year = l.year "
            f"WHERE l.cui IN ({marks})", cuis):
        net = None
        if r[5] is not None or r[6] is not None:
            net = (r[5] or 0) - (r[6] or 0)
        out[r[0]] = {"year": r[1], "form": r[2], "turnover": r[3], "employees": r[4],
                     "net_result": net}
    return out


def page(params: dict, sort: str, way: str, limit: int, offset: int) -> dict:
    """One page: {total, rows, sort, dir, paged_by, ms}. Each row is the
    registry's firm (registry.firm_rows: identity, status, activities)
    with ``figures`` (its latest filed year). FileNotFoundError when the
    registry, or a database a filter needs, isn't loaded."""
    started = time.monotonic()
    if sort not in SORTS:
        sort = "name"
    if sort in FIGURE_SORTS and not loaded()["financials"]:
        sort = "name"
    way = direction(sort, way)
    limit = max(1, min(int(limit), PAGE_MAX))
    offset = max(0, int(offset))
    con = connect()
    try:
        with _answering():
            found, q, how = ordered(con, params, sort)
        ids = found.slice(way, offset, limit)
        rows = registry.firm_rows(con, ids, q)
        figs = figures(con, [r["cui"] for r in rows])
    finally:
        con.close()
    for r in rows:
        r["figures"] = figs.get(r["cui"]) if r["cui"] else None
        r.pop("financials", None)
        r.pop("id", None)
    return {"total": len(found), "rows": rows, "sort": sort, "dir": way, "paged_by": how,
            "ms": round((time.monotonic() - started) * 1000)}


def firm(reg: str) -> dict | None:
    """One firm by its registration number: the registry's row (status,
    activities, administrators), every financial year on file, the
    taxpayer file's row and ANAF's kept answer, each part with its state
    (companies.record's parts). None when the registry has no such firm."""
    from harvey import companies
    from harvey.modules.ro import cui as ro_cui

    reg_path, fin_path, tax_path = _paths()
    con = registry.connect(reg_path)
    try:
        row = con.execute("SELECT cui FROM firms WHERE reg = ?", (reg,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    cui = ro_cui.clean(row[0] or "")
    cuis = [cui] if cui else []
    part = companies.registry_part(cuis, f"onrc:{reg}", reg_path)
    # The firm asked for first, then any other registration with its CUI.
    part["firms"].sort(key=lambda f: f.get("reg") != reg)
    for f in part["firms"]:
        f.pop("financials", None)
    return {"reg": reg, "cui": cui, "registry": part,
            "financials": companies.financials_part(cuis, fin_path),
            "taxpayer": companies.taxpayer_part(cuis, tax_path),
            "anaf": companies.anaf_part(cuis)}
