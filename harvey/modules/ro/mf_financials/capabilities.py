"""The Ministry of Finance financial statements' capabilities. The code they
run is store.py (the local database, data/financials.db) and source.py
(data.gov.ro's listing and the downloads).

``statements_check`` asks data.gov.ro what is published and says what is
new or revised against what is loaded; ``statements_sync`` downloads what
is missing for the years wanted and loads them; ``statements_lookup``
reads one company's years by its CUI and returns them as facts, one per
year for the yearly signals (the year in ``value_text``)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

from harvey.collectors.onrc import NATURAL_PERSON_FORMS
from harvey.modules.api import Capability, Draft, Fact
from harvey.modules.ro.mf_financials import source, store

COLLECTOR = "financials"


def _paths(ctx) -> tuple[Path, Path]:
    s = ctx.settings or {}
    return (Path(s.get("path") or store.FINANCIALS_PATH),
            Path(s.get("downloads") or source.DOWNLOADS))


def _years_setting(ctx, extra: dict) -> int:
    raw = extra.get("years")
    if raw is None:
        raw = (ctx.settings or {}).get("years", 3)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def updates(found: dict, loaded: dict | None, years: int) -> list[dict]:
    """What a load would change for the latest ``years`` published years:
    {"year", "form", "why"} with why "new" (not loaded), "revised" (a newer
    file than the one loaded) or "dropped" (loaded, no longer wanted)."""
    wanted = source.published_years(found)[:years]
    have = {(f["year"], f["form"]): f for f in (loaded or {}).get("files") or []}
    out = []
    for year in wanted:
        for form in store.FORMS:
            res = found.get((year, form))
            if res is None:
                continue
            old = have.get((year, form))
            if old is None:
                out.append({"year": year, "form": form, "why": "new"})
            elif res.url != old["url"] or res.published > old["published"]:
                out.append({"year": year, "form": form, "why": "revised"})
    for year, form in sorted({k for k in have if k[0] not in wanted}):
        out.append({"year": year, "form": form, "why": "dropped"})
    return out


async def _check(inp, ctx) -> Draft:
    """Ask data.gov.ro (``ctx.http``) what is published, keep the listing,
    and say what a load would change."""
    path, root = _paths(ctx)
    found = await source.catalog(ctx.http)
    source.save_catalog(found, root)
    return Draft(values={
        "catalog": {"years": source.published_years(found), "files": len(found)},
        "updates": updates(found, store.status(path), _years_setting(ctx, inp.extra))})


async def _sync(inp, ctx) -> Draft:
    """Download what is missing for the latest ``years`` published years
    (``extra["years"]``, else the module's setting) and load them, unless
    nothing changed (``extra["force"]`` loads anyway). With no
    ``ctx.http``, or data.gov.ro unreachable, the files already downloaded
    are loaded instead (``values["offline"]``)."""
    import httpx

    path, root = _paths(ctx)
    x = inp.extra
    years = _years_setting(ctx, x)
    progress = x.get("progress")
    offline = ""
    found: dict = {}
    if ctx.http is not None:
        try:
            found = await source.catalog(ctx.http)
            source.save_catalog(found, root)
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            offline = f"data.gov.ro unreachable ({e.__class__.__name__})"
    else:
        offline = "no connection given"
    if offline:
        local = source.downloaded(root)
        found = {k: res for k, (res, _) in local.items()}
        if not found:
            raise RuntimeError(f"{offline}, and nothing is downloaded in {root}")
    loaded = store.status(path)
    changes = updates(found, loaded, years)
    wanted = source.published_years(found)[:years]
    if loaded and not changes and not x.get("force"):
        return Draft(values={"financials": loaded, "updates": [], "downloaded": [],
                             "catalog": {"years": source.published_years(found)},
                             "offline": offline, "unchanged": True})
    fetched, sources = [], []
    for year in wanted:
        for form in store.FORMS:
            res = found.get((year, form))
            if res is None:
                continue
            target = source.local_file(res, root)
            if offline:
                file = target
            else:
                had = target.exists()
                file = await source.fetch(ctx.http, res, root, progress=progress)
                if not had:
                    fetched.append(res.filename)
            sources.append(store.Source(year=res.year, form=res.form, path=file,
                                        name=res.name, url=res.url, package=res.package,
                                        published=res.published, licence=res.licence))
    # Off the event loop: a load takes a minute, and the dashboard and the
    # heartbeat keep answering meanwhile.
    meta = await asyncio.to_thread(store.build, sources, path, progress)
    return Draft(values={"financials": store.status(path) or meta, "updates": changes,
                         "downloaded": fetched,
                         "catalog": {"years": source.published_years(found)},
                         "offline": offline})


def _registered_year(raw: str) -> int | None:
    years = re.findall(r"\b(\d{4})\b", raw or "")
    return int(years[-1]) if years else None


def registry_connection(registry_path) -> sqlite3.Connection | None:
    """The trade registry, read-only, when it is loaded (harvey registry load)."""
    if not registry_path or not Path(registry_path).is_file():
        return None
    return sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True)


def registry_firm(registry, cui: str) -> dict | None:
    """The trade registry's legal form, registration date and status for a
    CUI. ``registry`` is an open connection (registry_connection) or the
    database's path."""
    con = registry
    if not isinstance(registry, sqlite3.Connection):
        con = registry_connection(registry)
        if con is None:
            return None
    try:
        # "cui != ''" lets SQLite use the registry's CUI index, which
        # leaves out firms without one: without it, every lookup reads
        # the whole registry.
        row = con.execute("SELECT form, registered, operating FROM firms "
                          "WHERE cui = ? AND cui != '' "
                          "ORDER BY operating DESC LIMIT 1", (cui,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        if con is not registry:
            con.close()
    if row is None:
        return None
    return {"form": (row[0] or "").upper(), "registered": _registered_year(row[1]),
            "operating": bool(row[2])}


def facts_for(rows: list[dict], *, complete: set[int] = frozenset(),
              evidence: dict[int, str] | None = None, firm: dict | None = None,
              record: str = "") -> list[Fact]:
    """The facts one company's years give (``rows``, newest first, from
    store.accounts_for), about the record ``record``. FILED_ACCOUNTS is 1
    for each year on file, and 0 for a final year (``complete``:
    store.complete_years) without one, but only for a company the registry
    (``firm``) lists as operating, of a form that files, and registered
    before that year: a sole trader's or family business's absence is not
    a "no", nor is a struck-off firm's."""
    out: list[Fact] = []
    by_year = {r["year"]: r for r in rows}

    def add(code, value, year, url):
        out.append(Fact(code, value_num=float(value), value_text=str(year),
                        source_url=url, collector=COLLECTOR, record=record))

    for r in rows:
        year, url = r["year"], r.get("source_url") or ""
        add("FILED_ACCOUNTS", 1, year, url)
        if r.get("turnover") is not None:
            add("TURNOVER", r["turnover"], year, url)
        if r.get("employees") is not None:
            add("EMPLOYEES", r["employees"], year, url)
        if r.get("net_profit") is not None or r.get("net_loss") is not None:
            # One of the two is 0 by construction: an empty one counts as 0.
            add("NET_RESULT", (r.get("net_profit") or 0) - (r.get("net_loss") or 0), year, url)
        if r.get("equity") is not None:
            add("EQUITY", r["equity"], year, url)
        if r.get("debts") is not None:
            add("DEBTS", r["debts"], year, url)
        if r.get("fixed_assets") is not None and r.get("current_assets") is not None:
            add("TOTAL_ASSETS", r["fixed_assets"] + r["current_assets"]
                + (r.get("prepaid_expenses") or 0), year, url)
        before = by_year.get(year - 1)
        if (r.get("turnover") is not None and before and before.get("turnover") is not None
                and before["turnover"] > 0):
            growth = (r["turnover"] - before["turnover"]) / before["turnover"] * 100
            add("TURNOVER_GROWTH", round(growth, 1), year, url)
    if rows:
        latest = rows[0]
        out.append(Fact("ACCOUNTS_FORM", value_text=latest["form"],
                        source_url=latest.get("source_url") or "", collector=COLLECTOR,
                        record=record))
    if (firm and firm.get("operating") and firm.get("form")
            and firm["form"] not in NATURAL_PERSON_FORMS):
        since = firm.get("registered")
        for year in sorted(complete, reverse=True):
            if year in by_year or since is None or since >= year:
                continue
            add("FILED_ACCOUNTS", 0, year, (evidence or {}).get(year, ""))
    return out


async def _lookup(inp, ctx) -> Draft:
    """Companies' years as facts: one (``ids["cui"]``), or up to the
    manifest's ``batch`` at once (``extra["batch"]``: ``[{"key": <record>,
    "cui": <cui>}]``, the facts keyed to each record). The database, the
    trade registry (``extra["registry_path"]``, else harvey registry's) and
    which years are final are opened and read once a call, not once a
    company; the registry says whether a missing year is a "no"."""
    from harvey import registry

    batch = list(inp.extra.get("batch") or [])
    if not batch and inp.ids.get("cui"):
        batch = [{"key": "", "cui": inp.ids["cui"]}]
    wanted = [(e.get("key", ""), store.clean_cui(e.get("cui", ""))) for e in batch]
    wanted = [(key, cui) for key, cui in wanted if cui]
    if not wanted:
        return Draft()
    path, _ = _paths(ctx)
    try:
        con = store.connect(path)
    except FileNotFoundError:
        return Draft(failed="no financial statements loaded: harvey financials load")
    reg = registry_connection(Path(inp.extra.get("registry_path") or registry.REGISTRY_PATH))
    facts: list[Fact] = []
    try:
        complete = store.complete_years(con)
        evidence = store.file_urls(con)
        for key, cui in wanted:
            firm = registry_firm(reg, cui) if reg is not None else None
            facts += facts_for(store.accounts_for(con, cui), complete=complete,
                               evidence=evidence, firm=firm, record=key)
    finally:
        con.close()
        if reg is not None:
            reg.close()
    return Draft(facts=facts)


CAPABILITIES = {
    "statements_check": Capability("mf_financials", "statements_check", _check),
    "statements_sync": Capability("mf_financials", "statements_sync", _sync),
    "statements_lookup": Capability("mf_financials", "statements_lookup", _lookup),
}
