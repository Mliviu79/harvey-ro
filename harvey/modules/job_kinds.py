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
command) asked for it, because a run it belongs to saved companies (the
contact finder follows every source), or because a person switched a
source to update itself. The one background feed is the contact finder's
backlog (enqueue_backlog): free website reads, and paid searches only
inside the day's budget the user set.
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
DUPLICATES_LANE = "duplicates"  # the resolver's look for duplicates on file
CONTACTS_LANE = "contacts"     # the contact finder, one company per job
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
    cap = next(c for c in manifest.capabilities if c.get("provider") == provider_key)
    if cap.get("op") != "query":
        raise ValueError(lookup_only_message(provider_key))
    return owner, cap["id"]


def lookup_only_message(provider_key: str) -> str:
    return (f"{provider_key} only finds the website of a company already on file (its "
            "terms forbid building a list of companies from its results), so it isn't a "
            "Discover source")


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

    discovery_lane(provider_key)        # a source no module claims is refused
    return await enqueue_sources(state, [{"provider": provider_key, "queries": queries}],
                                 max_spend=max_spend, read_sites=read_sites,
                                 label=label or D.PROVIDERS[provider_key].label)


# A Discover run's two amounts (its batch's ``pools``): what its searches
# may spend, and what looking up websites for the companies they saved with
# none may (``find_websites``). The batch's cap is the two together; each
# job draws from its own pool only (jobs.reserve).
SEARCH_POOL = "search"
WEBSITES_POOL = "websites"
# The website lookups one Discover run queues beyond what its websites
# amount pays for, when a free search can take them: about an hour of
# TinyFish's free searches. The contacts lane runs its jobs in order, so a
# county-wide registry pull must not fill it for days; the companies left
# over wait for the background's walk, which looks up every company with
# no contact inside the free allowances' share.
WEBSITE_LOOKUPS_FREE_PER_RUN = 500


def _search_specs(provider_key: str, queries, read_sites: bool) -> list[dict]:
    from harvey.collectors import discover as D

    lane, cap = discovery_lane(provider_key)
    provider = D.PROVIDERS[provider_key]
    return [dict(kind="discover", lane=lane, module=lane, capability=cap,
                 label=q.keyword() or provider.label,
                 params={"provider": provider_key, "query": asdict(q), "read_sites": read_sites,
                         "pool": SEARCH_POOL},
                 dedupe_key=discovery_dedupe(provider_key, q),
                 est_cost=provider.estimate([q])) for q in queries]


async def enqueue_sources(state, plan: list[dict], *, max_spend: float, read_sites: bool = True,
                          label: str = "", find_websites: float | None = None):
    """One Discover run over several sources: one batch, one spend cap
    shared by all, a job per search (``{"provider", "queries"}``) and per
    registry pull (``{"source": "<module>.<capability>", "job": kind,
    "params": {...}}``, queued on its module's lane). ``find_websites``
    (dollars; None: off) also looks up a website for each company the run
    saves with none, in the same batch: free searches always, paid ones up
    to that amount, kept apart from the searches' ``max_spend`` (the
    batch's cap is the two together). Returns (batch id, the new job ids,
    jobs already waiting); Duplicate when every one is."""
    specs: list[dict] = []
    sources = []
    for part in plan:
        if part.get("provider"):
            specs += _search_specs(part["provider"], part["queries"], read_sites)
            sources.append(part["provider"])
            continue
        module, _, cap = part["source"].partition(".")
        specs.append(dict(kind=part["job"], lane=module, module=module, capability=cap,
                          label=part.get("label") or part["source"],
                          params={**part["params"], "source": part["source"],
                                  "pool": SEARCH_POOL},
                          dedupe_key=f"{part['job']}:{_key(part['params'])}"))
        sources.append(part["source"])
    params = {"sources": sources, "provider": sources[0] if len(sources) == 1 else "",
              "queries": [s["label"] for s in specs], "read_sites": read_sites}
    total = max_spend
    if find_websites is not None and read_sites:
        params["find_websites"] = True
        params["pools"] = {SEARCH_POOL: float(max_spend), WEBSITES_POOL: float(find_websites)}
        total = float(max_spend) + float(find_websites)
    batch_id = await jobs.new_batch(state, "discover", label or ", ".join(sources),
                                    params=params, max_spend=total)
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
        # Every company it saved or matched with a website: its site read
        # (free; the run's cap stays the searches').
        await enqueue_finder_follow_ups(ctx.state, report.company_ids,
                                        batch_id=ctx.job.batch_id, parent_id=ctx.id)
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


# ── Duplicates on file (harvey/resolver.py) ──

async def enqueue_duplicates_scan(state, *, batch_id: str = "") -> str:
    """Queue one look for duplicates among every company on file. Raises
    jobs.Duplicate while one is queued or running."""
    return await jobs.enqueue(state, "duplicates_scan", DUPLICATES_LANE, {},
                              dedupe_key="duplicates_scan", label="Look for duplicates",
                              batch_id=batch_id)


@jobs.kind("duplicates_scan")
async def run_duplicates_scan(ctx) -> dict:
    await ctx.say("Looking for duplicates among the companies on file")
    done = await ctx.state.scan_duplicates()
    await ctx.say(f"{done['merged']} merged on their own, {done['review']} to review")
    return {"duplicates": done}


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
    queued = finding = 0
    if saved.ids and not dry_run and not report.errors:
        # The contact finder follows the checks (their phones are on file
        # by then, so no search is paid for a firm they already reach).
        queued = await enqueue_follow_up_checks(ctx.state, saved.ids, batch_id=ctx.job.batch_id,
                                                parent_id=ctx.id, then_find=True)
        if queued:
            await ctx.say(f"  {queued:,} check{'' if queued == 1 else 's'} of the saved firms "
                          "queued (ANAF: one request a second, 100 firms each)")
        else:
            finding = await enqueue_finder_follow_ups(ctx.state, saved.ids,
                                                      batch_id=ctx.job.batch_id, parent_id=ctx.id)
    return {"report": report.as_dict(), "meta": meta, "follow_up_jobs": queued,
            "finder_jobs": finding}


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

# Calls of the capability a check job makes (ANAF: ten requests of 100
# companies, about 11 s): enough that each request goes out while the last
# batch is saved (runs.check_by_id), few enough that a pause or a cancel
# takes effect within seconds and the queue shows progress as it goes.
ID_CHECK_BATCHES = 10


async def enqueue_id_checks(state, module_id: str, capability: str, company_ids=None, *,
                            missing_only: bool = False, batch_id: str = "",
                            parent_id: str = "", label: str = "",
                            then_find: bool = False) -> tuple[list[str], list]:
    """Queue checks of these companies (every one with the identifier when
    None), ID_CHECK_BATCHES times as many per job as the capability asks
    about at once (ANAF: 1,000, ten requests). Returns (new job ids, jobs
    already waiting for the same ones)."""
    from harvey.modules import switches

    manifest = switches.by_id()[module_id]
    cap = next(c for c in manifest.capabilities if c.get("id") == capability)
    kinds = [need[0][len("id:"):] for need in cap.get("inputs") or ()
             if len(need) == 1 and need[0].startswith("id:")]
    if not kinds:
        raise ValueError(f"{module_id}.{capability} doesn't look companies up by an identifier")
    from harvey.modules.runs import _batches

    rows = await state.companies_with_id(kinds[0], company_ids)
    size = max(1, int(cap.get("batch") or 1)) * ID_CHECK_BATCHES
    lane = cap.get("lane") or module_id
    specs = []
    # A company known by two identifiers is in one job, once.
    for part in _batches(rows, size):
        ids = list(dict.fromkeys(r["id"] for r in part))
        specs.append(dict(
            kind="id_check", lane=lane, module=module_id, capability=capability,
            label=label or f"{manifest.name or module_id}: {len(ids)} "
                           f"compan{'y' if len(ids) == 1 else 'ies'}",
            params={"module": module_id, "capability": capability, "company_ids": ids,
                    "missing_only": missing_only, **({"then_find": True} if then_find else {})},
            dedupe_key=f"id_check:{module_id}:{capability}:{_key(sorted(ids), missing_only)}"))
    return await jobs.enqueue_many(state, specs, batch_id=batch_id, parent_id=parent_id)


async def enqueue_follow_up_checks(state, company_ids, *, batch_id: str = "",
                                   parent_id: str = "", then_find: bool = False) -> int:
    """The checks a source's new companies get (every enricher capability
    marked ``after_source`` whose module is on and has a confirmed signal
    to record: ANAF's), queued on their own lanes. ``then_find``: each
    check queues the contact finder for its companies when it is done (the
    first module's checks only). Returns how many jobs."""
    from harvey.modules.runs import _enabled, _wanted, id_enrichers

    confirmed = await state.confirmed_signal_codes()
    queued = 0
    for m, cap, _ in id_enrichers(await _enabled(state), after_source=True):
        if not _wanted(cap, confirmed):
            continue
        ids, _dupes = await enqueue_id_checks(state, m.id, cap["id"], company_ids,
                                              batch_id=batch_id, parent_id=parent_id,
                                              then_find=then_find and not queued)
        queued += len(ids)
    return queued


@jobs.kind("id_check", stoppable=True)
async def run_id_check(ctx) -> dict:
    """Check the job's companies a batch at a time, in the order of their
    ids. After each batch is saved the job's progress says how far it got
    (``after``: the last company id done, ``done``, ``total`` and the
    report so far), so a job paused, cancelled part way, stopped with the
    worker or tried again after a failure goes on from there. Progress
    only moves over companies with none left unchecked before them: after
    a batch that failed (or a save that did), the companies after it are
    asked again on the next try (the service's kept answers make that
    cheap) rather than the failed ones skipped. A job with any company
    left unchecked never ends done."""
    from harvey.modules.runs import check_by_id

    p = ctx.params
    ids = sorted(set(p.get("company_ids") or ()))
    after = str(ctx.progress.get("after") or "")
    todo = [i for i in ids if i > after] if after else ids
    if p.get("company_ids") is None:
        todo = None                     # every company with the identifier
    so_far = dict(ctx.progress.get("report") or {})
    done_before = int(ctx.progress.get("done") or 0)
    saved_now = [0]

    async def saved(batch_ids, report_so_far):
        if report_so_far.get("errors"):
            # A batch before this one failed: its companies are still to
            # check, so progress stays in front of them.
            return
        saved_now[0] += len(batch_ids)
        await ctx.save(after=max(batch_ids), done=done_before + saved_now[0], total=len(ids),
                       report=_added(so_far, report_so_far), busy=0)

    async def stop():
        return bool(await ctx.stop_reason())

    report = await check_by_id(ctx.state, p["module"], p["capability"], todo,
                               missing_only=bool(p.get("missing_only")), stop=stop, saved=saved)
    total = _added(so_far, report)
    total["companies"] = len(ids) if p.get("company_ids") is not None else report["companies"]
    if ctx.outcome:
        # Paused, cancelled or handed back: what it did is in its progress.
        return total
    if report["errors"]:
        message = report["errors"][-1]
        if report["stopped"] == "busy":
            # The service is busy (or out of reach): the whole lane waits,
            # longer each time, without using up a try. Its other jobs
            # wait too, rather than each asking a busy service again.
            busy = int(ctx.progress.get("busy") or 0) + 1
            await ctx.save(busy=busy)
            wait = jobs.backoff_seconds(busy)
            await jobs.rest_lane(ctx.state, ctx.job.lane, wait, but=ctx.id)
            raise LaneExhausted(message, retry_after=wait)
        if saved_now[0]:
            # Some went through: the rest later, from where it got,
            # without using up a try (it is getting on).
            raise LaneExhausted(message, retry_after=jobs.backoff_seconds(1))
        # Nothing went through: tried again later, a try used.
        raise Transient(message)
    report = total
    if p.get("then_find"):
        report["finder_jobs"] = await enqueue_finder_follow_ups(
            ctx.state, p.get("company_ids") or [], batch_id=ctx.job.batch_id, parent_id=ctx.id)
    return report


def _added(before: dict, report: dict) -> dict:
    """A check's report with the counts of one before it added in."""
    from harvey.modules.runs import _new_check_report

    out = _new_check_report(report.get("module") or before.get("module") or "")
    for part in (before, report):
        for key, value in part.items():
            if isinstance(value, int) and not isinstance(value, bool) and key in out:
                out[key] += value
            elif key in ("unconfirmed", "stopped") and value:
                out[key] = value
            elif key == "errors" and value:
                out["errors"] = (out["errors"] + list(value))[:20]
    return out


# ── Finding contacts: one company per job (harvey/finder.py) ──

def _finder_spec(company_id: str, *, force_since: str = "", background: bool = False,
                 free_only: bool = False, website: bool = False) -> dict:
    params = {"company_id": company_id}
    if force_since:
        params["since"] = force_since
    if background:
        params["background"] = True
    if free_only:
        params["free_only"] = True
    if website:
        # A Discover run's "find websites": looked up even with a phone on
        # file, paid searches from the run's websites amount.
        params["website"] = True
        params["pool"] = WEBSITES_POOL
    # One waiting job per company and kind of request: a person's run (or
    # a forced one) isn't swallowed by a background job already waiting,
    # which would run without its cap (or without force).
    flavour = "force" if force_since else "background" if background else "run"
    return dict(kind="find_contacts", lane=CONTACTS_LANE, module="contact_finder",
                capability="find_contacts", label=company_id, params=params,
                dedupe_key=f"find_contacts:{company_id}:{flavour}")


async def _labels(state, company_ids) -> dict[str, str]:
    """Each company's name (or domain), for its job's label."""
    out: dict[str, str] = {}
    ids = list(company_ids)
    async with state._connect() as db:
        for at in range(0, len(ids), 500):
            part = ids[at:at + 500]
            async with db.execute(
                    "SELECT id, COALESCE(NULLIF(name, ''), domain, id) FROM companies "
                    f"WHERE id IN ({','.join('?' * len(part))})", part) as cur:
                out.update({r[0]: r[1] for r in await cur.fetchall()})
    return out


async def enqueue_find_contacts(state, company_ids, *, batch_id: str = "", parent_id: str = "",
                                force_since: str = "", background: bool = False,
                                free_only: bool = False, website: bool = False):
    """Queue the contact finder for each company (one job each, on the
    contacts lane; a company already waiting isn't queued twice). Returns
    (new job ids, jobs already waiting)."""
    ids = list(dict.fromkeys(i for i in company_ids or () if i))
    if not ids:
        return [], []
    labels = await _labels(state, ids)
    specs = []
    for company_id in ids:
        spec = _finder_spec(company_id, force_since=force_since, background=background,
                            free_only=free_only, website=website)
        spec["label"] = labels.get(company_id) or company_id
        specs.append(spec)
    return await jobs.enqueue_many(state, specs, batch_id=batch_id, parent_id=parent_id)


async def enqueue_finder_follow_ups(state, company_ids, *, batch_id: str = "",
                                    parent_id: str = "") -> int:
    """The finder for the companies a run saved or matched, in that run's
    batch: those it has free work for (a website to read), free only. When
    the run asked to find websites (its batch's ``find_websites``), those
    with no website are looked up too, drawing paid searches from the
    run's websites amount only. Anything else (an address check for what
    it saved) is a Find-contacts run of its own, with its own estimate and
    cap. Returns how many were queued."""
    from harvey import finder

    if not company_ids:
        return 0
    chosen = await finder.select(state, ids=company_ids, force=True, limit=len(company_ids))
    ids, _ = await enqueue_find_contacts(state, [c["id"] for c in chosen if c["site"]],
                                         batch_id=batch_id, parent_id=parent_id, free_only=True)
    batch = await jobs.get_batch(state, batch_id) if batch_id else None
    if batch and (batch.get("params") or {}).get("find_websites"):
        wanted = [c["id"] for c in chosen if not c["site"]]
        room = max(0, await website_lookups_limit(state, batch)
                   - await _website_jobs(state, batch_id))
        more, _ = await enqueue_find_contacts(state, wanted[:room], batch_id=batch_id,
                                              parent_id=parent_id, website=True)
        ids += more
        if len(wanted) > room:
            await _note_deferred(state, batch_id, len(wanted) - room)
    return len(ids)


async def website_lookups_limit(state, batch: dict) -> int:
    """How many website lookups a Discover run queues: the paid lookups
    its websites amount pays for, at the cheapest paid search that is on,
    plus WEBSITE_LOOKUPS_FREE_PER_RUN when a free search is on."""
    from harvey import finder
    from harvey.modules import switches

    pools = (batch.get("params") or {}).get("pools") or {}
    amount = float(pools.get(WEBSITES_POOL) or 0.0)
    found = finder.searchers(await switches.enabled_ids(state), finder._env())
    paid = [s.estimate for s in found if s.estimate > 0]
    limit = int(amount / min(paid) + 1e-9) if paid else 0
    if any(s.estimate <= 0 for s in found):
        limit += WEBSITE_LOOKUPS_FREE_PER_RUN
    return limit


async def _website_jobs(state, batch_id: str) -> int:
    async with state._connect() as db:
        async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND kind = 'find_contacts' "
                "AND json_extract(params_json, '$.pool') = ?", (batch_id, WEBSITES_POOL)) as cur:
            (n,) = await cur.fetchone()
    return int(n or 0)


async def _note_deferred(state, batch_id: str, n: int) -> None:
    """Count the companies a run left to the background's walk (its
    batch's ``websites_deferred``)."""
    async with state._connect() as db:
        await db.execute(
            "UPDATE job_batches SET params_json = json_set(params_json, '$.websites_deferred', "
            "COALESCE(json_extract(params_json, '$.websites_deferred'), 0) + ?) WHERE id = ?",
            (int(n), batch_id))
        await db.commit()


async def _spend_for(ctx, config):
    """What a finder job may spend: its run's cap, or for background work
    the install's day budget (spend.per_day_usd); with neither, nothing
    paid runs."""
    from harvey import finder

    limits = _spend_limits(config)
    if ctx.params.get("free_only"):
        return finder.Spend(cap=False)
    batch = await jobs.get_batch(ctx.state, ctx.job.batch_id) if ctx.job.batch_id else None
    cap = batch.get("max_spend") if batch else None
    if cap is not None and float(cap) > 0:
        return finder.Spend(ctx.reserve, ctx.spend, **limits)
    if ctx.params.get("background") and limits["per_day"]:
        return finder.Spend(ctx.reserve, ctx.spend, **limits)
    return finder.Spend(cap=False)


@jobs.kind("find_contacts")
async def run_find_contacts(ctx) -> dict:
    """The contact finder for one company (harvey/finder.py): what is on
    file and its own site first, then, inside a spend cap, a website search
    by name and town and one checked address per person. Runs whatever the
    Email module's switch says."""
    from harvey import finder

    company_id = ctx.params.get("company_id", "")
    if not company_id:
        raise JobError("find_contacts needs a company_id")
    lane = ctx.lane
    if "config" not in lane:
        from harvey.signals import sync_vocabulary

        await sync_vocabulary(ctx.state)
        lane["config"] = _config()
    config = lane["config"]
    spend = await _spend_for(ctx, config)
    run_id = await ctx.lane_run("contacts", "finder")
    found = await finder.find(ctx.state, company_id, spend=spend,
                              since=ctx.params.get("since") or None,
                              batch_id=ctx.job.batch_id, config=config, run_id=run_id,
                              background=bool(ctx.params.get("background")),
                              want_website=bool(ctx.params.get("website")),
                              # (A Discover run's website lookups check no
                              # addresses: that is a Find-contacts run's.)
                              addresses=not ctx.params.get("website"))
    ctx.count(1)
    for why in found.skipped:
        await ctx.say(f"  {why}")
    return found.as_dict()


async def enqueue_contact_search(state, selected: list[str], *, max_spend: float = 0.0,
                                 force: bool = False, label: str = "") -> tuple[str, list, list]:
    """A Find-contacts run over chosen companies: one batch (capped at
    ``max_spend``; 0 reads websites only), one job per company. ``force``
    searches again what was searched before this run started. Returns
    (batch id, new job ids, jobs already waiting); Duplicate when every
    one is already waiting."""
    from harvey import contacts

    since = contacts.now() if force else ""
    batch_id = await jobs.new_batch(
        state, "find_contacts", label or ("Find contacts (search again)" if force
                                          else "Find contacts"),
        params={"companies": len(selected), "force": force}, max_spend=max_spend)
    ids, dupes = await enqueue_find_contacts(state, selected, batch_id=batch_id,
                                             force_since=since)
    if not ids and dupes:
        await jobs.drop_batch(state, batch_id)
        raise jobs.Duplicate(dupes[0])
    return batch_id, ids, dupes


# How many background finder jobs wait at most: the backlog is fed a little
# at a time, so a person's run never queues behind a million of them.
BACKLOG_QUEUED = 50
# After a top-up that found nothing to do, the backlog isn't looked through
# again for this long: on a full registry that look takes seconds.
BACKLOG_IDLE_SECONDS = 900
_backlog_idle: dict[str, float] = {}


async def enqueue_backlog(state) -> int:
    """Feed the finder's backlog to the contacts lane in the background:
    companies with no contact and a website to read (free), newest first;
    then those with no website a search module that is set up hasn't
    searched, through a search with some of its free allowance left for
    the background (the contact finder's background_search and
    background_share), or, only when the install has a day's budget
    (spend.per_day_usd), a paid one drawn from that budget. Keeps at most
    BACKLOG_QUEUED waiting. Returns how many were queued."""
    import time

    from harvey import finder
    from harvey.modules import switches

    enabled = await switches.enabled_ids(state)
    if "contact_finder" not in enabled:
        return 0
    key = str(state.db_path)
    if time.monotonic() < _backlog_idle.get(key, 0.0):
        return 0
    async with state._connect() as db:
        async with db.execute(
                "SELECT json_extract(params_json, '$.company_id') FROM jobs "
                "WHERE kind = 'find_contacts' AND status IN ('queued', 'running') "
                "AND json_extract(params_json, '$.background')") as cur:
            waiting = {r[0] for r in await cur.fetchall()}
    room = BACKLOG_QUEUED - len(waiting)
    if room <= 0:
        return 0
    # (Asked for more than there is room for: the newest are those waiting.)
    ids = [i for i in await finder.next_free(state, BACKLOG_QUEUED) if i not in waiting][:room]
    per_day = _spend_limits(_config())["per_day"]
    if len(ids) < room:
        # Companies with no website: through the searches the day's budget
        # can pay for, and those with some of their free allowance left for
        # the background (its share; never a paid search without a budget).
        everyone = finder.searchers(enabled, finder._env())
        paid = bool(per_day) and await jobs.spent_since(state, "start of day") < per_day
        on, share = await finder.background_settings(state)
        free = {s.module: await finder.free_for_background_now(s.module, on, share)
                for s in everyone}
        available = [s for s in everyone
                     if free[s.module] or (paid and (s.estimate > 0 or free[s.module] is None))]
        ids += [i for i in await finder.next_paid(state, BACKLOG_QUEUED, available)
                if i not in waiting and i not in ids][:room - len(ids)]
    if not ids and not waiting:
        _backlog_idle[key] = time.monotonic() + BACKLOG_IDLE_SECONDS
    new, _ = await enqueue_find_contacts(state, ids, background=True)
    return len(new)


# ── Sources Discover runs as a job of their own (the registry) ──

def discover_sources(enabled=None) -> list[dict]:
    """Every query capability that records companies and says how Discover
    runs it as a job (``discover = "<job kind>"`` in its manifest), with
    the filters it declares and whether its module is on: the registry
    today, read from the manifests, never a list here."""
    from harvey.modules import api, switches

    out = []
    for m, cap in api.declared(switches.by_id()):
        if (cap.get("op") != "query" or (cap.get("outputs") or {}).get("records") != "companies"
                or not cap.get("discover") or cap.get("provider")):
            continue
        filters = [dict(f) for f in cap.get("filters") or ()
                   if enabled is None or not f.get("module") or f["module"] in enabled]
        out.append({"key": f"{m.id}.{cap['id']}", "module": m.id, "capability": cap["id"],
                    "job": cap["discover"], "label": cap.get("label") or m.name or m.id,
                    "blurb": m.description, "attribution": getattr(m, "attribution", "") or "",
                    "filters": filters, "kind": "registry", "configured": True,
                    "enabled": enabled is None or m.id in enabled})
    return out


def _number(value):
    if value in (None, ""):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def registry_plan(filters: dict) -> dict:
    """A registry pull's parameters from the filters its Discover panel
    sent. ValueError when they say nothing (that would be every firm in
    Romania) or name an unknown county."""
    from harvey.collectors import onrc
    from harvey.modules.ro.mf_financials.store import Filter as FinFilter
    from harvey.modules.ro.mf_taxpayers.store import Filter as TaxFilter
    from harvey.paths import PROJECT_ROOT
    from harvey.registry import REGISTRY_PATH

    f = filters or {}
    words = str(f.get("words") or "").strip()
    counties = onrc.counties(str(f.get("county") or "")) or []
    localities = onrc._localities(str(f.get("locality") or ""))
    fin = FinFilter(min_turnover=_number(f.get("min_turnover")),
                    max_turnover=_number(f.get("max_turnover")),
                    min_employees=_number(f.get("min_employees")),
                    max_employees=_number(f.get("max_employees")),
                    unknown="include" if f.get("unknown") == "include" else "exclude")

    def yes_no(key):
        value = f.get(key)
        return None if value in (None, "") else value in (True, "yes", "1", 1)

    tax = TaxFilter(employer=yes_no("employer"), vat=yes_no("vat"), micro=yes_no("micro"))
    if not (words or counties or localities or fin.active() or tax.active()):
        raise ValueError("the registry needs an activity, an area or a filter: with none it "
                         "would be every firm in Romania")
    where = ", ".join(localities + counties) or "Romania"
    return {"path": str(REGISTRY_PATH), "data_dir": str(PROJECT_ROOT / "data" / "onrc"),
            "ensure_latest": True, "words": words, "classes": [], "counties": counties,
            "localities": localities, "all_statuses": False,
            "fin": asdict(fin) if fin.active() else None,
            "tax": asdict(tax) if tax.active() else None,
            "with_administrators": True, "dry_run": False, "area": where,
            "label": f"{words or 'every activity'} in {where}"}


def registry_count(params: dict) -> int | None:
    """How many firms a registry pull would save (free, read locally);
    None when no registry is loaded yet (the pull loads it first)."""
    from harvey import registry

    try:
        con = registry.connect(Path(params["path"]))
    except FileNotFoundError:
        return None
    try:
        fin, tax = _filters(params)
        q = registry.resolve(con, params.get("words") or "",
                             counties=params.get("counties") or None,
                             localities=params.get("localities") or None,
                             all_statuses=bool(params.get("all_statuses")), fin=fin, tax=tax)
        return registry.count(con, q)
    finally:
        con.close()
