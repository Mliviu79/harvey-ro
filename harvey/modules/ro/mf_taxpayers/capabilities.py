"""The Ministry of Finance taxpayer file's capabilities. The code they run is
store.py (the local database, data/taxpayers.db) and source.py
(data.gov.ro's listing and the downloads).

``taxpayers_check`` asks data.gov.ro what is published and says whether it
is newer than what is loaded; ``taxpayers_sync`` downloads what is missing
and loads it; ``taxpayer_lookup`` reads one company by its CUI and returns
what the file says as facts, dated the day the file was published (so a
newer reading, ANAF's, stays the current one)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harvey.modules.api import Capability, Contact, Draft, Fact
from harvey.modules.ro.mf_taxpayers import source, store

COLLECTOR = "taxpayers"


def _paths(ctx) -> tuple[Path, Path]:
    s = ctx.settings or {}
    return (Path(s.get("path") or store.TAXPAYERS_PATH),
            Path(s.get("downloads") or source.DOWNLOADS))


def update(found: list, loaded: dict | None) -> dict | None:
    """What a load would change: {"year", "why"} with why "new" (nothing
    loaded, or a newer year) or "revised" (a newer file for a part), or
    None when what is loaded is the newest."""
    if not found:
        return None
    year = found[0].year
    if not loaded:
        return {"year": year, "why": "new"}
    have = {f["part"]: f for f in loaded.get("files") or []}
    if int(loaded.get("year") or 0) < year:
        return {"year": year, "why": "new"}
    for res in found:
        old = have.get(res.part)
        if old is None or res.url != old["url"] or res.published > old["published"]:
            return {"year": year, "why": "revised"}
    return None


async def _check(inp, ctx) -> Draft:
    """Ask data.gov.ro (``ctx.http``) what is published, keep the listing,
    and say whether a load would change anything."""
    path, root = _paths(ctx)
    found = await source.catalog(ctx.http)
    source.save_catalog(found, root)
    return Draft(values={"catalog": {"year": found[0].year, "files": len(found)},
                         "update": update(found, store.status(path))})


async def _sync(inp, ctx) -> Draft:
    """Download the newest year's files when they are missing, and load
    them, unless nothing changed (``extra["force"]`` loads anyway). With no
    ``ctx.http``, or data.gov.ro unreachable, the files already downloaded
    are loaded instead (``values["offline"]``)."""
    import httpx

    path, root = _paths(ctx)
    x = inp.extra
    progress = x.get("progress")
    offline = ""
    found: list = []
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
        found = [res for res, _ in local]
        if not found:
            raise RuntimeError(f"{offline}, and nothing is downloaded in {root}")
    loaded = store.status(path)
    change = update(found, loaded)
    if loaded and change is None and not x.get("force"):
        return Draft(values={"taxpayers": loaded, "update": None, "downloaded": [],
                             "catalog": {"year": found[0].year}, "offline": offline,
                             "unchanged": True})
    fetched, sources = [], []
    for res in found:
        target = source.local_file(res, root)
        if offline:
            file = target
        else:
            had = target.exists()
            file = await source.fetch(ctx.http, res, root, progress=progress)
            if not had:
                fetched.append(res.filename)
        sources.append(store.Source(part=res.part, path=file, name=res.name, url=res.url,
                                    package=res.package, year=res.year,
                                    published=res.published, licence=res.licence))
    # Off the event loop: a load takes a minute or two, and the dashboard
    # and the heartbeat keep answering meanwhile.
    meta = await asyncio.to_thread(store.build, sources, path, progress)
    return Draft(values={"taxpayers": store.status(path) or meta, "update": change,
                         "downloaded": fetched, "catalog": {"year": found[0].year},
                         "offline": offline})


def _published(row: dict) -> str | None:
    """The file's publication day as an observation time."""
    day = (row.get("published") or "")[:10]
    return f"{day} 00:00:00" if len(day) == 10 else None


def facts_for(row: dict, record: str = "") -> tuple[list[Fact], list[Contact]]:
    """What one taxpayer row says, as facts about the record ``record``
    (the company looked up), and its phone numbers as contacts."""
    from harvey.collectors.phones import field_phones

    url, when = row.get("source_url") or "", _published(row)
    facts: list[Fact] = []

    def add(code, value_num=None, value_text=""):
        facts.append(Fact(code, value_num=value_num, value_text=value_text, source_url=url,
                          collector=COLLECTOR, observed_at=when, record=record))

    for code, column in (("VAT_REGISTERED", "vat"), ("HAS_EMPLOYEES_TAX", "employer"),
                         ("MICRO_TAX_REGIME", "micro_tax")):
        if row.get(column) is not None:
            add(code, float(row[column]))
    if row.get("vat") and row.get("vat_since"):
        add("VAT_SINCE", value_text=row["vat_since"])
    # The file's status (STARE) is the last event on the taxpayer's record
    # (a transfer, a resumption, a change), not its state: it is kept in the
    # file but not recorded as REGISTRATION_STATUS, which is ANAF's state.
    line = store.address(row)
    if line:
        add("ADDRESS", value_text=line)
    contacts = []
    for hit in field_phones(row.get("phone"), "RO"):
        add("PHONE_NUMBER", value_text=hit.number)
        contacts.append(Contact("phone", hit.number, display=hit.display, source_url=url,
                                record=record))
    return facts, contacts


async def _lookup(inp, ctx) -> Draft:
    """Companies as facts: one (``ids["cui"]``), or up to the manifest's
    ``batch`` at once (``extra["batch"]``: ``[{"key": <record>, "cui":
    <cui>}]``, the facts and phones keyed to each record). Reads the
    database at ``extra["con"]`` (an open connection) or opens the
    module's own, once a call."""
    batch = list(inp.extra.get("batch") or [])
    if not batch and inp.ids.get("cui"):
        batch = [{"key": "", "cui": inp.ids["cui"]}]
    wanted = [(e.get("key", ""), store.clean_cui(e.get("cui", ""))) for e in batch]
    wanted = [(key, cui) for key, cui in wanted if cui]
    if not wanted:
        return Draft()
    con = inp.extra.get("con")
    owned = con is None
    if owned:
        path, _ = _paths(ctx)
        try:
            con = store.connect(path)
        except FileNotFoundError:
            return Draft(failed="no taxpayer file loaded: harvey taxpayers load")
    draft = Draft()
    try:
        for key, cui in wanted:
            row = store.taxpayer(con, cui)
            if row is None:
                continue
            facts, contacts = facts_for(row, key)
            draft.facts += facts
            draft.contacts += contacts
    finally:
        if owned:
            con.close()
    return draft


CAPABILITIES = {
    "taxpayers_check": Capability("mf_taxpayers", "taxpayers_check", _check),
    "taxpayers_sync": Capability("mf_taxpayers", "taxpayers_sync", _sync),
    "taxpayer_lookup": Capability("mf_taxpayers", "taxpayer_lookup", _lookup),
}
