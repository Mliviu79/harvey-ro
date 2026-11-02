"""The Companies page's shared pieces: its sorts and their default
directions, kept orderings (``Snapshots`` of ``Ordered`` lists, which the
overlay's page reads and the Registry page cut pages from), and one
company's record from the side databases.

The record reads the side databases (the registry, the financial
statements, the taxpayer file, ANAF's kept answers) for each of the
company's CUIs. A database that isn't loaded, or holds nothing for the
company, is said so in the payload ("not_loaded" / "none"), never an error.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from array import array
from pathlib import Path

SORTS = ("created", "name", "place", "turnover", "employees", "contacts", "searched")
FIGURE_SORTS = ("turnover", "employees")
DEFAULT_DIR = {"created": "desc", "name": "asc", "place": "asc", "turnover": "desc",
               "employees": "desc", "contacts": "desc", "searched": "desc"}

SNAPSHOT_SECONDS = 300
SNAPSHOTS_KEPT = 3


def direction(sort: str, raw: str) -> str:
    raw = (raw or "").lower()
    return raw if raw in ("asc", "desc") else DEFAULT_DIR.get(sort, "desc")


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


# ── One company's record, from the side databases ──

NOT_LOADED, NONE, OK = "not_loaded", "none", "ok"


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


