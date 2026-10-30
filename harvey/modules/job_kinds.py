"""What each kind of job runs, and how each is queued.

Every long run is a job (harvey/modules/jobs.py) on the lane of the source
it calls: a discovery search on its search module's lane (one job per
search, so a run over five cities is five jobs), each company's site read
on the website reader's lane, a registry load or pull on the registry's,
a load of the financial statements or the taxpayer file on theirs, and
the checks at ANAF a hundred companies a job on ANAF's. Each kind calls the
same run code as before (harvey/modules/runs.py); the job adds the queue,
the progress, the resume and the spend reservation.

Nothing here re-runs on its own: a job runs because a person (a click, a
command) or the Email module asked for it, or because a person switched a
source to update itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path

from harvey.modules import jobs
from harvey.modules.jobs import JobError, LaneExhausted, SpendLimit, Transient

logger = logging.getLogger("harvey.jobs")

SITE_LANE = "website"          # the website reader's module
CONTACTS_LANE = "contacts"     # finding people and emails (the Email module asks)
REGISTRY_LANE = "onrc"


def _config():
    try:
        from harvey.config import load_config
        return load_config()
    except Exception:
        return None


def _key(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


# ── Discovery: one job per search ──

def discovery_lane(provider_key: str) -> tuple[str, str]:
    """(the module that claims ``provider_key``, its capability id).
    ValueError for a source no module claims: it is refused, not run."""
    from harvey.modules import switches

    owner = switches.capability_owner("provider", provider_key)
    manifest = switches.by_id().get(owner or "")
    if manifest is None:
        raise ValueError(f"No module claims the discovery source {provider_key!r}, so it "
                         "can't run. See: harvey modules list")
    cap = next(c["id"] for c in manifest.capabilities if c.get("provider") == provider_key)
    return owner, cap


def discovery_dedupe(provider_key: str, query) -> str:
    q = asdict(query)
    q.pop("total", None)
    return f"discover:{provider_key}:{_key(q)}"


async def enqueue_discovery(state, provider_key: str, queries, *, max_spend: float,
                            read_sites: bool = True, label: str = ""):
    """Queue one job per search of a run (one batch, capped at
    ``max_spend``). Returns (batch id, the new job ids, the jobs already
    waiting for searches asked again). Duplicate when every search is
    already queued or running."""
    from harvey.collectors import discover as D

    lane, cap = discovery_lane(provider_key)
    provider = D.PROVIDERS[provider_key]
    batch_id = await jobs.new_batch(
        state, "discover", label or provider.label,
        params={"provider": provider_key, "queries": [q.keyword() for q in queries],
                "read_sites": read_sites}, max_spend=max_spend)
    specs = [dict(kind="discover", lane=lane, module=lane, capability=cap,
                  label=q.keyword() or provider.label,
                  params={"provider": provider_key, "query": asdict(q), "read_sites": read_sites},
                  dedupe_key=discovery_dedupe(provider_key, q),
                  est_cost=provider.estimate([q])) for q in queries]
    ids, dupes = await jobs.enqueue_many(state, specs, batch_id=batch_id)
    if not ids and dupes:
        await jobs.drop_batch(state, batch_id)
        raise jobs.Duplicate(dupes[0])
    return batch_id, ids, dupes


_SUMMED = ("queries", "queries_ran", "queries_succeeded", "found", "junk", "excluded",
           "new_companies", "known_companies", "observations", "unfetched",
           "queries_left", "pages")
_MONEY = ("estimated_cost", "actual_cost", "unfetched_cost", "count_cost")


def merge_reports(reports) -> dict:
    """Several discovery reports (one per search, or one search's resumes)
    as one."""
    out: dict = {k: 0 for k in _SUMMED}
    out.update({k: 0.0 for k in _MONEY})
    out.update(errors=[], stopped="", left_behind="", provider="")
    for r in reports:
        if not r:
            continue
        for k in _SUMMED:
            out[k] += int(r.get(k) or 0)
        for k in _MONEY:
            out[k] = round(out[k] + float(r.get(k) or 0.0), 4)
        out["errors"] += [e for e in r.get("errors") or [] if e not in out["errors"]][:20]
        out["stopped"] = r.get("stopped") or out["stopped"]
        out["left_behind"] = r.get("left_behind") or out["left_behind"]
        out["provider"] = r.get("provider") or out["provider"]
    return out


# A provider's own stop (its allowance, its key) as opposed to these.
_OWN_STOPS = ("budget reached", "paused", "cancelled")


def _spend_limits(config) -> dict:
    """The install's spend limits for the day and the month (harvey.yaml
    ``spend``), as jobs.reserve takes them."""
    spend = getattr(config, "spend", None)
    return {"per_day": getattr(spend, "per_day_usd", None),
            "per_month": getattr(spend, "per_month_usd", None)}


@jobs.kind("discover", stoppable=True)
async def run_discover(ctx) -> dict:
    from harvey.collectors import discover as D
    from harvey.modules.runs import run_discovery

    key = ctx.params.get("provider", "")
    provider = D.PROVIDERS.get(key)
    if provider is None:
        raise JobError(f"No module claims the discovery source {key!r} (or its module's "
                       "code didn't load), so it can't run.")
    query = D.DiscoveryQuery(**ctx.params["query"])
    cursor = ctx.progress.get("cursor")
    pages_before = int(ctx.progress.get("pages") or 0)
    results_before = int(ctx.progress.get("results") or 0)
    config = _config()
    remaining = await ctx.reserve(provider.estimate([query]), provider.first_cost(query),
                                  **_spend_limits(config))
    # What this try has recorded so far: each page's cost is stored as it
    # arrives, before the next page is paid for, so a worker killed
    # mid-search still has it counted against the caps.
    recorded = {"cost": 0.0}

    async def on_page(pages, results, cost=0.0):
        ctx.actual_cost += float(cost or 0.0) - recorded["cost"]
        recorded["cost"] = float(cost or 0.0)
        await ctx.save(pages=pages_before + pages, results=results_before + results)

    report = await run_discovery(ctx.state, config, key, [query], max_spend=remaining,
                                 stop=ctx.stop_reason, cursor=cursor, on_page=on_page)
    await ctx.spend(report.actual_cost - recorded["cost"])
    total = merge_reports([ctx.progress.get("report"), report.as_dict()])
    await ctx.save(cursor=report.cursor, report=total,
                   pages=pages_before + report.pages,
                   results=results_before + report.found)
    if ctx.params.get("read_sites", True) and report.company_ids:
        await enqueue_site_reads(ctx.state, report.company_ids, batch_id=ctx.job.batch_id,
                                 parent_id=ctx.id)
    stopped = report.stopped
    if stopped in ("not configured", "no confirmed signals"):
        raise JobError("; ".join(report.errors) or stopped)
    if stopped == "budget reached":
        raise SpendLimit("the run's spend limit is reached")
    if stopped == D.PART_WAY:
        # A later page failed with something passing: what the earlier
        # pages found is saved, and the next try starts at the failed page.
        raise Transient(report.errors[-1] if report.errors else stopped)
    if stopped in ("paused", "cancelled"):
        if not ctx.outcome:
            # The old kill switch (discovery_paused): held, not dropped.
            ctx.pause(jobs.PAUSED_BY_HAND)
    elif stopped and stopped not in _OWN_STOPS:
        raise LaneExhausted(stopped, retry_after=report.retry_after)
    elif not report.queries_succeeded and report.errors:
        if report.transient:
            raise Transient(report.errors[-1])
        raise JobError(report.errors[-1])
    return total


# ── Reading companies' sites: one job per company ──

async def enqueue_site_reads(state, company_ids=None, *, batch_id: str = "",
                             parent_id: str = "", stale_days: int | None = None):
    """Queue a site read for each company (all when None) that has a
    website and hasn't been read lately. Returns (new ids, already queued)."""
    from harvey.pipeline import PROFILE_STALE_DAYS

    due = await state.companies_needing_profile(
        limit=-1, stale_days=stale_days or PROFILE_STALE_DAYS,
        ids=None if company_ids is None else list(dict.fromkeys(company_ids)))
    specs = [dict(kind="site_read", lane=SITE_LANE, module=SITE_LANE, capability="read_site",
                  label=c.get("domain") or c.get("name") or c["id"],
                  params={"company_id": c["id"]}, dedupe_key=f"site_read:{c['id']}")
             for c in due]
    return await jobs.enqueue_many(state, specs, batch_id=batch_id, parent_id=parent_id)


async def _company_to_read(state, company_id: str) -> dict | None:
    import aiosqlite

    from harvey.state import LIVE_COMPANY

    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT c.id, c.name, c.domain, c.location, c.source_url FROM companies c "
                f"WHERE c.id = ? AND COALESCE(c.domain, '') != '' AND {LIVE_COMPANY}",
                (company_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


@jobs.kind("site_read")
async def run_site_read(ctx) -> dict:
    from harvey.collectors.ats import role_keywords
    from harvey.modules.runs import profile
    from harvey.signals import sync_vocabulary

    company = await _company_to_read(ctx.state, ctx.params.get("company_id", ""))
    if company is None:
        return {"skipped": "no website (or merged into another company)"}
    lane = ctx.lane
    if "confirmed" not in lane:
        await sync_vocabulary(ctx.state)
        lane["config"] = _config()
        lane["keywords"] = role_keywords(getattr(lane["config"], "icp", None))
    lane["confirmed"] = await ctx.state.confirmed_signal_codes()
    if not lane["confirmed"]:
        raise JobError("No signals confirmed yet: confirm signals first (Semnale, or "
                       "harvey signals --confirm free); nothing is collected until you do.")
    run_id = await ctx.lane_run("profile", "http")
    found = await profile(ctx.state, [company], lane["confirmed"], run_id=run_id,
                          config=lane["config"], role_keywords=lane["keywords"])
    ctx.count(1)
    return {"domain": company["domain"], "observations": found}


# ── The trade registry (the onrc module) ──

@jobs.kind("registry_load")
async def run_registry_load(ctx) -> dict:
    from harvey.modules.runs import ensure_registry

    p = ctx.params
    meta = await ensure_registry(Path(p["path"]), Path(p["data_dir"]), p.get("dataset") or "latest",
                                 with_people=p.get("with_people", True),
                                 force=bool(p.get("force")), say=ctx.say)
    return {"registry": meta}


def _filters(p: dict):
    fin = tax = None
    if p.get("fin"):
        from harvey.modules.ro.mf_financials.store import Filter
        fin = Filter(**p["fin"])
    if p.get("tax"):
        from harvey.modules.ro.mf_taxpayers.store import Filter as TaxFilter
        tax = TaxFilter(**p["tax"])
    return fin, tax


class _Saved:
    """Collects the companies a pull saved (registry_pull's follow_up)."""

    def __init__(self):
        self.ids: list[str] = []

    def add(self, ids) -> None:
        self.ids += [i for i in ids if i]


@jobs.kind("registry_pull")
async def run_registry_pull(ctx) -> dict:
    from harvey import registry
    from harvey.config import live_do_not_contact
    from harvey.modules.runs import ensure_registry, registry_pull

    p = ctx.params
    path = Path(p["path"])
    meta = None
    if p.get("ensure_latest"):
        meta = await ensure_registry(path, Path(p["data_dir"]), "latest",
                                     with_people=p.get("with_administrators", True),
                                     offline_ok=True, say=ctx.say)
    try:
        con = await asyncio.to_thread(registry.connect, path)
    except FileNotFoundError:
        raise JobError(f"No registry loaded at {path}. Run harvey registry load first "
                       "(downloads ~1.5 GB once per dataset).", code=2)
    meta = meta or await asyncio.to_thread(registry.status, path) or {}
    fin, tax = _filters(p)

    def resolve():
        # Reading the registry is synchronous: in a thread, off the loop.
        q = registry.resolve(con, p.get("words") or "",
                             classes=[tuple(c) for c in p.get("classes") or []] or None,
                             counties=p.get("counties") or None,
                             localities=p.get("localities") or None,
                             all_statuses=bool(p.get("all_statuses")), fin=fin, tax=tax)
        registry.count(con, q, 1)
        return q

    try:
        q = await asyncio.to_thread(resolve)
    except FileNotFoundError as e:
        raise JobError(f"{e} (the filter needs it).", code=2)
    dry_run = bool(p.get("dry_run"))
    verb = "checked" if dry_run else "saved"

    def progress(done, total):
        ctx.say_soon(f"  {verb} {done:,} of {total:,}...")

    saved = _Saved()
    config = _config()
    report = await registry_pull(
        ctx.state, con, q, limit=p.get("limit"),
        with_administrators=bool(p.get("with_administrators", True)), dry_run=dry_run,
        do_not_contact=live_do_not_contact(config) if config is not None else None,
        progress=progress, area=p.get("area") or "", follow_up=saved)
    queued = 0
    if saved.ids and not dry_run and not report.errors:
        queued = await enqueue_follow_up_checks(ctx.state, saved.ids, batch_id=ctx.job.batch_id,
                                                parent_id=ctx.id)
        if queued:
            await ctx.say(f"  {queued:,} check{'' if queued == 1 else 's'} of the saved firms "
                          "queued (ANAF: one request a second, 100 firms each)")
    return {"report": report.as_dict(), "meta": meta, "follow_up_jobs": queued}


# ── A source's local copy: check what is published, load it ──

def _sync_progress(ctx):
    def progress(step, rows):
        if isinstance(rows, int) and str(step).startswith("downloading"):
            line = f"    {step} ({rows / 1e6:.0f} MB)..."
        elif rows is None:
            line = f"    {step}..."
        elif isinstance(rows, int) and rows and rows % 500_000 == 0:
            line = f"    {step}: {rows:,} rows..."
        else:
            return
        ctx.say_soon(line)
    return progress


def _source_failure(e: Exception) -> JobError | None:
    """A source's own refusal as a job failure (a load already running is
    "busy", a format it can't read, the site down): None for anything else."""
    if type(e).__name__.endswith("Busy"):
        return JobError(f"{e}.", code=2)
    if type(e).__name__ == "FormatChanged" or isinstance(e, RuntimeError):
        return JobError(str(e), code=1)
    return None


@jobs.kind("source_sync")
async def run_source_sync(ctx) -> dict:
    """Download what is missing and load it (a source module's sync
    capability), then record what it knows about the companies on file."""
    from harvey.modules.runs import lookup_by_id, sync_source

    p = ctx.params
    module = p["module"]
    extra = {"progress": _sync_progress(ctx)}
    if "force" in p:
        extra["force"] = bool(p["force"])
    try:
        draft = await sync_source(ctx.state, module, p["capability"], extra=extra)
    except Exception as e:
        failure = _source_failure(e)
        if failure is not None and not jobs.is_transient(e):
            raise failure
        raise
    values = draft.values or {}
    out = {"module": module, "unchanged": bool(values.get("unchanged")),
           "offline": values.get("offline") or "", "downloaded": values.get("downloaded") or [],
           "values": {k: v for k, v in values.items() if k != "progress"}}
    if p.get("lookup", True) and not (p.get("auto") and values.get("unchanged")):
        found = await lookup_by_id(ctx.state, progress=lambda done, total: ctx.say_soon(
            f"    companies on file: {done:,} of {total:,}..."))
        out.update(companies=found["companies"], observations=found["observations"],
                   unconfirmed=found["unconfirmed"], errors=found["errors"][:3])
    return out


@jobs.kind("source_check")
async def run_source_check(ctx) -> dict:
    """Ask the publisher whether something new is out (a source module's
    check capability). Downloads nothing."""
    from harvey.modules.runs import sync_source

    p = ctx.params
    try:
        draft = await sync_source(ctx.state, p["module"], p["capability"])
    except Exception as e:
        failure = _source_failure(e)
        if failure is not None and not jobs.is_transient(e):
            raise failure
        raise
    return {"module": p["module"], "values": dict(draft.values or {})}


def source_lane(module_id: str) -> str:
    return module_id


async def enqueue_source(state, kind_: str, module_id: str, capability: str, *,
                         force: bool = False, auto: bool = False, label: str = "") -> tuple[str, str]:
    """Queue a load (``source_sync``) or a check (``source_check``) of a
    source's local copy. Returns (batch id, job id); Duplicate when one is
    already waiting."""
    batch_id = await jobs.new_batch(state, kind_, label or module_id, params={"module": module_id})
    params = {"module": module_id, "capability": capability}
    if kind_ == "source_sync":
        params.update(force=force, auto=auto)
    try:
        job_id = await jobs.enqueue(state, kind_, source_lane(module_id), params,
                                    dedupe_key=f"{kind_}:{module_id}", module=module_id,
                                    capability=capability, label=label or module_id,
                                    batch_id=batch_id)
    except jobs.Duplicate:
        await jobs.drop_batch(state, batch_id)
        raise
    return batch_id, job_id


async def enqueue_due_sources(state) -> list[str]:
    """For each source module that is on and whose ``auto_update`` the
    user switched on: when its ``check_days`` have passed since the last
    check, queue a load of what is new. Off by default, so this usually
    only reads settings. Returns the modules queued."""
    from datetime import datetime, timedelta, timezone

    from harvey.modules import switches
    from harvey.modules.runs import AUTO_SETTING, CHECK_DAYS_SETTING, CHECKED_KEY, _enabled

    queued = []
    enabled = await _enabled(state)
    for m in switches.manifests():
        if m.id not in enabled or m.kind != "source" or AUTO_SETTING not in m.settings:
            continue
        settings = await switches.module_settings(state, m.id)
        if not settings.get(AUTO_SETTING):
            continue
        sync = [c["id"] for c in m.capabilities if c.get("op") == "sync" and c.get("auto")]
        if not sync:
            continue
        key = CHECKED_KEY.format(m.id)
        last = await state.get_setting(key)
        days = int(settings.get(CHECK_DAYS_SETTING) or 30)
        now = datetime.now(timezone.utc)
        try:
            if last and now - datetime.fromisoformat(last) < timedelta(days=days):
                continue
        except ValueError:
            pass
        await state.set_setting(key, now.isoformat(timespec="seconds"))
        try:
            await enqueue_source(state, "source_sync", m.id, sync[0], auto=True,
                                 label=f"{m.name or m.id} (update)")
        except jobs.Duplicate:
            continue
        queued.append(m.id)
    return queued


# ── Checks by legal identifier at an outside service (ANAF) ──

async def enqueue_id_checks(state, module_id: str, capability: str, company_ids=None, *,
                            missing_only: bool = False, batch_id: str = "",
                            parent_id: str = "", label: str = "") -> tuple[list[str], list]:
    """Queue checks of these companies (every one with the identifier when
    None), as many per job as the capability asks about at once (ANAF:
    100). Returns (new job ids, jobs already waiting for the same ones)."""
    from harvey.modules import switches

    manifest = switches.by_id()[module_id]
    cap = next(c for c in manifest.capabilities if c.get("id") == capability)
    kinds = [need[0][len("id:"):] for need in cap.get("inputs") or ()
             if len(need) == 1 and need[0].startswith("id:")]
    if not kinds:
        raise ValueError(f"{module_id}.{capability} doesn't look companies up by an identifier")
    rows = await state.companies_with_id(kinds[0], company_ids)
    size = max(1, int(cap.get("batch") or 1))
    lane = cap.get("lane") or module_id
    specs = []
    for at in range(0, len(rows), size):
        ids = [r["id"] for r in rows[at:at + size]]
        specs.append(dict(
            kind="id_check", lane=lane, module=module_id, capability=capability,
            label=label or f"{manifest.name or module_id}: {len(ids)} "
                           f"compan{'y' if len(ids) == 1 else 'ies'}",
            params={"module": module_id, "capability": capability, "company_ids": ids,
                    "missing_only": missing_only},
            dedupe_key=f"id_check:{module_id}:{capability}:{_key(sorted(ids), missing_only)}"))
    return await jobs.enqueue_many(state, specs, batch_id=batch_id, parent_id=parent_id)


async def enqueue_follow_up_checks(state, company_ids, *, batch_id: str = "",
                                   parent_id: str = "") -> int:
    """The checks a source's new companies get (every enricher capability
    marked ``after_source`` whose module is on and has a confirmed signal
    to record: ANAF's), queued on their own lanes. Returns how many jobs."""
    from harvey.modules.runs import _enabled, _wanted, id_enrichers

    confirmed = await state.confirmed_signal_codes()
    queued = 0
    for m, cap, _ in id_enrichers(await _enabled(state), after_source=True):
        if not _wanted(cap, confirmed):
            continue
        ids, _dupes = await enqueue_id_checks(state, m.id, cap["id"], company_ids,
                                              batch_id=batch_id, parent_id=parent_id)
        queued += len(ids)
    return queued


@jobs.kind("id_check")
async def run_id_check(ctx) -> dict:
    from harvey.modules.runs import check_by_id

    p = ctx.params
    report = await check_by_id(ctx.state, p["module"], p["capability"], p.get("company_ids"),
                               missing_only=bool(p.get("missing_only")))
    asked = report["checked"] + report["cached"]
    if report["errors"] and not asked and not report["updated"]:
        # The service was down or busy for this batch: tried again later.
        raise Transient(report["errors"][0])
    return report


# ── Finding people and emails (the Email module asks for it) ──

@jobs.kind("find_contacts")
async def run_find_contacts(ctx) -> dict:
    from harvey.agents.scout import Scout
    from harvey.brain import Brain
    from harvey.config import load_config, load_env

    config = load_config()
    brain = Brain(ctx.state, config.brain)
    scout = Scout(brain, ctx.state, config, load_env())
    try:
        found = await scout.run()
    finally:
        close = getattr(scout, "close_browser", None)
        if close is not None:
            await close()
    return {"found": found if isinstance(found, int) else 0}


async def enqueue_find_contacts(state) -> str | None:
    """Queue one contact search, unless one is already waiting."""
    try:
        return await jobs.enqueue(state, "find_contacts", CONTACTS_LANE, {},
                                  dedupe_key="find_contacts", label="Find contacts")
    except jobs.Duplicate:
        return None
