"""Runs: core's loops that call capabilities and hand what they find to ingest.

A run is what a person (the CLI, the dashboard) or the heartbeat starts: a
discovery over a list of searches, a registry pull, a profile pass over the
companies due for one. The run owns everything that isn't a module's to
own: the confirmation gate read once at the start, the runs log, the spend
cap and the kill switch checked between calls, the do-not-contact list,
and the order things are written in. Each call to a module goes through
its capability (harvey/modules/api.py) and each thing it found through
harvey/modules/ingest.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import replace

import httpx

from harvey.modules import api, ingest

logger = logging.getLogger("harvey.runs")
# Each run logs as the collector it took over from, so the log reads as before.
_discover_log = logging.getLogger("harvey.collectors.discover")


def _capability_for(field: str, key: str):
    """The capability of the module that claims discovery ``field = key``
    (a provider or a preset)."""
    from harvey.modules import switches

    module_id = switches.capability_owner(field, key)
    manifest = switches.by_id().get(module_id or "")
    if manifest is None:
        raise api.CapabilityMissing(f"no module claims {field} {key!r}")
    cap_id = next(c["id"] for c in manifest.capabilities if c.get(field) == key)
    return api.capability(module_id, cap_id)


async def _enabled(state) -> frozenset[str]:
    """The modules that are on: every capability call says so, and one
    whose module is off is refused (api.Capability)."""
    from harvey.modules import switches

    return frozenset(await switches.enabled_ids(state))


# ── Geocoding (the nominatim module) ──

async def geocode_city(state, city: str, radius_km: int = 40,
                       client: httpx.AsyncClient | None = None) -> str:
    """"Denver, CO" -> "39.7392,-104.9903,40". Cached permanently, a miss
    too (as "-"), so a place is asked once."""
    if not city:
        return ""
    cache_key = f"geo:{city.strip().lower()}"
    cached = await state.get_setting(cache_key)
    if cached:
        return "" if cached == "-" else f"{cached},{radius_km}"

    from harvey.collectors.discover import TIMEOUT

    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        draft = await api.capability("nominatim", "geocode")(
            api.Input(locality=city), api.Context(http=client, enabled=await _enabled(state)))
    finally:
        if owned:
            await client.aclose()
    point = draft.values.get("point")
    if point is None:
        return ""
    await ingest.place(state, cache_key, point or "-")
    return f"{point},{radius_km}" if point else ""


async def resolve_coordinates(state, queries, radius_km: int = 40,
                              client: httpx.AsyncClient | None = None):
    """Fill in coordinates for any query that lacks them.

    Configured ``icp.geo_coordinates`` always wins; this only covers what the
    user did not spell out. Distinct cities are geocoded once each, however
    many queries share them.
    """
    needed = {q.location for q in queries if q.location and not q.coordinate}
    points: dict[str, str] = {}
    for i, city in enumerate(sorted(needed)):
        if i:
            await asyncio.sleep(1.1)     # Nominatim: max 1 request/second
        points[city] = await geocode_city(state, city, radius_km, client)

    for query in queries:
        if not query.coordinate:
            query.coordinate = points.get(query.location, "")
    return queries


# ── Discovery (the search modules and the presets) ──

async def run_discovery(state, config, provider_key: str | None = None, queries=None,
                        max_spend: float = 1.0, dry_run: bool = False, count: bool = False,
                        stop=None, cursor: dict | None = None, on_page=None):
    """Discover businesses and record what was learned about them: every
    result each search has, page by page, until the spend cap or the kill
    switch stops it (read between pages); a stop says what was left.

    ``dry_run`` estimates and returns without collecting anything. With
    ``count``, a source that can count (DataForSEO Business Listings) is
    asked how many results each search has first, a small paid request per
    search, so the estimate is exact; nothing else is called. Run it first,
    every time: the first small trial is what exposes the data-quality
    problems that would otherwise poison every market you scale to.

    A job runs one search through here: ``stop()`` (async) says why it must
    stop at the next page (a cancel, a pause), ``cursor`` resumes a search
    from the page a stop left it at, and ``on_page(pages, results, cost)``
    is told after each page (cost: what the search has spent so far). ``report.cursor`` is where the last search
    stopped (None when it ran to the end).
    """
    from harvey.collectors import discover as D
    from harvey.config import live_do_not_contact, load_env
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    provider_key = provider_key or D.DEFAULT_PROVIDER
    provider = D.PROVIDERS.get(provider_key)
    if provider is None:
        # Only a source a module claims runs: one that no module does is
        # refused, never run with its facts attributed to nobody.
        raise ValueError(f"unknown discovery provider: {provider_key!r} "
                         "(no module claims it; see harvey modules list)")
    # A source or preset whose module is off is refused (ModuleOff), never
    # run or skipped quietly.
    for preset in sorted({q.preset for q in queries or []} - {""}) or [""]:
        await switches.require_discovery(state, provider_key, preset)

    queries = queries or D.build_queries(config)
    if provider.needs_coordinates:
        queries = await resolve_coordinates(state, queries)
    report = D.DiscoveryReport(provider=provider_key, queries=len(queries))
    report.estimated_cost = provider.estimate(queries)
    # Not started yet: a stop before the first search leaves it where it was.
    report.cursor = cursor

    if dry_run:
        if count and provider.can_count:
            await _count(provider_key, queries, report)
        return report

    env = load_env().model_dump()
    missing = provider.missing_keys(env)
    if missing:
        names = ", ".join(k.upper() for k in missing)
        report.errors.append(
            f"{provider.label} is not ready: {names} is empty or still the "
            f"template placeholder in .env")
        report.stopped = "not configured"
        return report

    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    if not confirmed:
        report.errors.append(
            "no signals confirmed — confirm signals first (dashboard → Signals, "
            "or `harvey signals --confirm free`) or discovery has nothing to record")
        report.stopped = "no confirmed signals"
        return report

    report.run_id = await state.start_run(
        "discover", provider=provider_key,
        params={"queries": len(queries), "estimate": report.estimated_cost},
    )

    # Resolved once per run, not per result: the list may live in a file, and
    # re-reading it a few hundred times would turn a config lookup into I/O
    # inside the hot loop.
    do_not_contact = live_do_not_contact(config)
    enabled = await _enabled(state)
    searcher = _capability_for("provider", provider_key)
    presets = {}

    # Each business's base facts are written once per run; its ranks once per
    # (business, keyword), and again only if a repeat search ranks it better.
    seen: set[str] = set()
    best_ranks: dict[tuple[str, str], tuple] = {}
    saved: dict[str, ingest.Saved] = {}
    ranked_in_run: dict[tuple[str, str], set[str]] = {}

    async def paused() -> str:
        if stop is not None:
            why = await stop()
            if why:
                return why
        return "paused" if await state.get_setting("discovery_paused") else ""

    next_query = 0
    try:
        async with httpx.AsyncClient(timeout=D.TIMEOUT, follow_redirects=True) as client:
            for i, query in enumerate(queries):
                next_query = i
                # The kill switch is read between searches and between a
                # search's pages, not once at the top: a long run has to be
                # stoppable while it is running.
                why = await paused()
                if why:
                    report.stopped = why
                    break
                # The cap is checked before spending: a search starts only
                # if its first request fits in what is left.
                if report.actual_cost + provider.first_cost(query) > max_spend + 1e-9:
                    report.stopped = "budget reached"
                    break
                next_query = i + 1
                if i and provider.key == "osm":
                    await asyncio.sleep(D.OpenStreetMap.MIN_INTERVAL)

                cap = searcher
                if query.preset:
                    cap = presets.get(query.preset) or presets.setdefault(
                        query.preset, _capability_for("preset", query.preset))
                budget = api.Budget()
                budget.remaining = max(0.0, max_spend - report.actual_cost)
                ctx = api.Context(http=client, env=env, budget=budget, config=config,
                                  wanted=frozenset(confirmed), enabled=enabled,
                                  settings={"provider": provider_key,
                                            "searcher": (searcher.module, searcher.id),
                                            "paused": paused, "on_page": on_page,
                                            "cursor": cursor if i == 0 else None})
                report.queries_ran += 1
                draft = await cap(api.Input(query=query, area=query.location,
                                            extra={"provider": provider_key}), ctx)
                report.actual_cost += draft.cost
                report.pages += draft.counts.get("pages", 0)
                report.cursor = (draft.values.get("cursor") if draft.failed is None
                                 else (cursor if i == 0 else None))
                report.transient = bool(draft.values.get("transient"))
                report.retry_after = draft.values.get("retry_after")
                if "available" in draft.counts:
                    report.available = (report.available or 0) + draft.counts["available"]
                report.unfetched += draft.counts.get("unfetched", 0)
                report.unfetched_cost += draft.values.get("unfetched_cost", 0.0)
                if draft.failed is not None:
                    if draft.stop:
                        report.stopped = draft.stop
                        next_query = i       # it found nothing: still to run
                        break
                    report.errors.append(f"{query.keyword()}: {draft.failed}")
                    continue
                report.queries_succeeded += 1

                for error in draft.errors:
                    report.errors.append(f"{query.keyword()}: {error}")
                report.found += draft.counts.get("found", 0)
                report.junk += draft.counts.get("junk", 0)
                facts_of: dict[str, list[api.Fact]] = {}
                for fact in draft.facts:
                    facts_of.setdefault(fact.record, []).append(fact)
                batch = []
                for record in draft.records:
                    # Before the save, so a forbidden company never becomes a
                    # row and never reaches profiling. Checking it at send time
                    # alone still lets one through discovery, where it costs
                    # credits to enrich and sits in every cohort until somebody
                    # notices it by eye.
                    if ingest.excluded(record, do_not_contact):
                        report.excluded += 1
                        continue
                    ident = record.domain or record.external_id
                    if not ident:
                        continue
                    first = ident not in seen
                    if first:
                        seen.add(ident)
                        saved[ident] = await ingest.listing(state, record)
                        report.company_ids.append(saved[ident].company_id)
                        if saved[ident].created:
                            report.new_companies += 1
                        else:
                            report.known_companies += 1
                    where = saved[ident]
                    facts = facts_of.get(record.key, [])
                    ranks = tuple(next((f.value_num for f in facts if f.code == code), None)
                                  for code in D.RANK_CODES)
                    phrase = record.found_by or query.keyword()
                    for code, value in zip(D.RANK_CODES, ranks):
                        if value is not None:
                            ranked_in_run.setdefault((code, phrase), set()).add(where.company_id)

                    rank_key = (ident, phrase.lower())
                    previous = best_ranks.get(rank_key)
                    improved = previous is None or any(
                        new is not None and (old is None or new < old)
                        for new, old in zip(ranks, previous))
                    if not first and not improved:
                        continue
                    if improved:
                        best_ranks[rank_key] = tuple(
                            D._best(old, None if new is None else int(new)) for new, old in
                            zip(ranks, previous or (None,) * len(ranks)))
                    if not first:
                        # A repeat of the same search: record only the ranks
                        # it improved, so a worse one never becomes "latest".
                        old = dict(zip(D.RANK_CODES, previous or (None,) * len(ranks)))
                        facts = [f for f in facts if f.code in D.RANK_CODES
                                 and (old[f.code] is None or f.value_num < old[f.code])]
                    batch += ingest.rows([replace(f, record="") for f in facts],
                                         company_id=where.company_id, origin=where.origin,
                                         run_id=report.run_id, confirmed=confirmed)

                # A business this phrase ranked before, and that no search for
                # it this run has found, is no longer ranked (within depth) or
                # no longer in the pack. Say so, so the latest reading is true.
                # (Said by the search module, as the search's ranks are,
                # whether or not a preset ran it.)
                phrase = query.keyword()
                for code in draft.complete_codes:
                    if code not in confirmed:
                        continue
                    found_now = ranked_in_run.setdefault((code, phrase), set())
                    gone = api.Fact(code, value_num=0.0, value_text=phrase,
                                    collector="discover", module=searcher.module,
                                    capability=searcher.id)
                    for company_id in await state.ranked_company_ids(code, phrase) - found_now:
                        batch += ingest.rows([gone], company_id=company_id,
                                             run_id=report.run_id, confirmed=confirmed)

                # Flush every batch. The observations are the asset; buffering
                # them until some threshold means a run that dies loses them.
                report.observations += await ingest.write(state, batch, report.run_id)
                if draft.stop:
                    report.stopped = draft.stop
                    break

        if report.stopped in ("budget reached", "paused", "cancelled"):
            # What the stop left: the searches never started, at their
            # estimate (the results a stopped search had left are counted
            # above, from what the source said it holds).
            left = queries[next_query:]
            report.queries_left = len(left)
            report.unfetched_cost += provider.estimate(left) if left else 0.0
        await state.finish_run(report.run_id, status="completed",
                               records=report.new_companies + report.known_companies,
                               cost_usd=report.actual_cost)
    except Exception as e:
        _discover_log.exception("discover run failed")
        report.errors.append(str(e))
        await state.finish_run(report.run_id, status="failed",
                               records=report.new_companies,
                               cost_usd=report.actual_cost, error=str(e))
    return report


async def _count(provider_key: str, queries, report):
    """Ask the source how many results each search has, so the estimate is
    exact (``report.available``, ``report.count_cost``). Needs the
    provider's keys; without them, or if asking fails, the estimate stays
    the uncounted one and says why."""
    from harvey.collectors import discover as D
    from harvey.config import load_env

    provider = D.PROVIDERS[provider_key]
    env = load_env().model_dump()
    if provider.missing_keys(env):
        report.errors.append(f"{provider.label} has no key yet, so the searches "
                             "weren't counted: the estimate prices a full first "
                             "page for each")
        return
    try:
        async with httpx.AsyncClient(timeout=D.TIMEOUT, follow_redirects=True) as client:
            report.count_cost = await D.count_results(provider_key, queries, client, env)
    except Exception as e:
        logger.warning("discover: counting %s failed: %s", provider_key, e)
        report.errors.append(f"counting the results failed: {e}")
        for q in queries:
            q.total = None
        return
    report.counted = True
    report.available = sum(q.total or 0 for q in queries)
    report.estimated_cost = provider.estimate(queries)


# ── Official sources (a trade registry: the onrc module) ──

# Records saved per transaction (big enough that committing is noise, small
# enough that an interrupted import keeps most of what it saved), and how
# often progress is reported.
SOURCE_SAVE_BATCH = 500
SOURCE_PROGRESS_EVERY = 500
# The counts on a source's report a save adds to as it saves.
_REPORT_COUNTS = ("catch_all_only", "shared_domain", "excluded", "new_companies",
                  "known_companies", "updated_companies", "administrators",
                  "activities", "observations")


async def save_source_draft(state, draft: api.Draft, report, *, module: str,
                            run_params: dict | None = None, dry_run: bool = False,
                            do_not_contact: list[str] | None = None, progress=None,
                            saved_ids: list[str] | None = None, on_saved=None):
    """Save what an official source (``module``) read, SOURCE_SAVE_BATCH records
    per transaction, through ingest's rules for an official source.
    ``report`` is the source's own report (its ``values["report"]``); the
    counts in _REPORT_COUNTS are added to it as records are saved, and
    ``unconfirmed`` lists the signals the read collects
    (``values["codes"]``) that aren't confirmed. ``dry_run`` matches
    everything and writes nothing. A fact is written once per dataset:
    re-reading the same dataset adds nothing, a newer one adds its readings
    beside the old. ``progress(done, total)`` is called every
    SOURCE_PROGRESS_EVERY records. ``saved_ids`` collects the id each saved
    record landed on; ``on_saved(ids)`` is told them as each batch commits."""
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    await switches.require(state, module, "Saving what the source found")
    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    report.unconfirmed = [c for c in draft.values.get("codes", ()) if c not in confirmed]
    report.dry_run = dry_run
    collector = draft.values.get("collector", "")

    if not dry_run:
        report.run_id = await state.start_run("import", provider=module,
                                              params=run_params or {})
    facts_of: dict[str, list[api.Fact]] = {}
    for fact in draft.facts:
        facts_of.setdefault(fact.record, []).append(fact)

    async def save(batch, record: api.Record):
        if ingest.excluded(record, do_not_contact):
            report.excluded += 1
            return
        report.catch_all_only += record.counts.get("catch_all_only", 0)
        found = facts_of.get(record.key, [])
        saved = await ingest.source_record(batch, record, collector=collector,
                                           dry_run=dry_run)
        report.shared_domain += saved.shared_domain
        if saved.created:
            report.new_companies += 1
        else:
            report.known_companies += 1
        report.administrators += record.counts.get("administrators", 0)
        if dry_run:
            return
        if saved_ids is not None:
            saved_ids.append(saved.company_id)
        report.updated_companies += saved.updated
        report.activities += saved.activities
        rows = ingest.rows([replace(f, record="") for f in found],
                           company_id=saved.company_id, origin=saved.origin,
                           run_id=report.run_id, confirmed=confirmed)
        fresh = await ingest.unseen(batch, saved.company_id, record.source_url, rows)
        report.observations += await ingest.write(batch, fresh, report.run_id)

    records = draft.records
    try:
        done = 0
        for start in range(0, len(records), SOURCE_SAVE_BATCH):
            # A batch that fails is rolled back, so are its counts.
            counts = {k: getattr(report, k) for k in _REPORT_COUNTS}
            first = len(saved_ids) if saved_ids is not None else 0
            try:
                async with state.batch() as batch:
                    for record in records[start:start + SOURCE_SAVE_BATCH]:
                        done += 1
                        if progress and done % SOURCE_PROGRESS_EVERY == 0:
                            progress(done, len(records))
                        await save(batch, record)
            except Exception:
                for k, v in counts.items():
                    setattr(report, k, v)
                if saved_ids is not None:
                    del saved_ids[first:]
                raise
            if on_saved is not None and saved_ids is not None:
                on_saved(saved_ids[first:])
        if not dry_run:
            await state.finish_run(report.run_id, status="completed",
                                   records=report.new_companies + report.known_companies)
    except Exception as e:
        logger.exception("%s import failed", module)
        report.errors.append(str(e))
        if not dry_run:
            await state.finish_run(report.run_id, status="failed",
                                   records=report.new_companies, error=str(e))
    return report


async def _source_read(state, cap_id: str, inp: api.Input):
    """Call the onrc module's ``cap_id`` (refused while the module is off)."""
    from harvey.signals import sync_vocabulary

    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    return await api.capability("onrc", cap_id)(
        inp, api.Context(enabled=await _enabled(state), wanted=frozenset(confirmed)))


async def registry_pull(state, con, q, *, limit: int | None = None,
                        with_administrators: bool = True, dry_run: bool = False,
                        do_not_contact: list[str] | None = None, progress=None,
                        area: str = "", follow_up: "FollowUp | None" = None):
    """Copy the firms a resolved registry query matches (the first
    ``limit``, best first) into the working database: the onrc module's
    registry_search, then save_source_draft. ACTIVITY_CODE is recorded
    only for the classes the query named. ``follow_up`` (a started
    FollowUp) is handed each batch of saved firms as it commits, so its
    checks run while the rest is saved; the caller finishes it."""
    draft = await _source_read(state, "registry_search", api.Input(
        query=q, area=area, extra={"con": con, "limit": limit,
                                   "with_administrators": with_administrators}))
    saved: list[str] = []
    report = await save_source_draft(
        state, draft, draft.values["report"], module="onrc",
        run_params=draft.values.get("run_params"), dry_run=dry_run,
        do_not_contact=do_not_contact, progress=progress, saved_ids=saved,
        on_saved=follow_up.add if follow_up is not None else None)
    if saved and not report.errors:
        # What other free local sources know about them (their yearly
        # figures), at once: a filter on those is decided before any paid
        # search runs.
        found = await lookup_by_id(state, saved)
        report.observations += found["observations"]
    return report


async def import_registry(state, files, dataset, *, county: str = "", classes=None,
                          locality: str = "", active_only: bool = True, dry_run: bool = False,
                          do_not_contact: list[str] | None = None,
                          with_administrators: bool = True, progress=None):
    """Read the registry's files (the onrc module's registry_import) and
    save what matches, without the local registry database. Every class a
    kept company is authorised for is saved as its activities, so it can be
    searched by any of them later. ``with_administrators`` False leaves the
    representatives file unread; so does a run where neither people signal
    is confirmed."""
    draft = await _source_read(state, "registry_import", api.Input(
        area=county, locality=locality, extra={
            "files": files, "dataset": dataset, "classes": list(classes or []),
            "active_only": active_only, "with_administrators": with_administrators}))
    return await save_source_draft(
        state, draft, draft.values["report"], module="onrc",
        run_params=draft.values.get("run_params"), dry_run=dry_run,
        do_not_contact=do_not_contact, progress=progress)


async def ensure_registry(path, data_dir, name: str = "latest", *, with_people: bool = True,
                          force: bool = False, offline_ok: bool = False, say=None) -> dict:
    """The registry at ``path`` holding dataset ``name`` ("latest": the
    newest on data.gov.ro), downloading any missing file into
    data_dir/<date>/ and building it when it isn't loaded yet. With
    ``offline_ok``, a registry already loaded is used as it is when
    data.gov.ro can't be reached; without one, the newest dataset already
    downloaded into data_dir is loaded instead. ``say(line)`` (async) is
    told what happens. Returns its meta; JobError (code 2) when there is
    nothing to load or another load holds the registry."""
    from pathlib import Path

    from harvey import registry
    from harvey.collectors import onrc
    from harvey.modules.jobs import JobError

    async def tell(line: str):
        if say is not None:
            await say(line)

    path, data_dir = Path(path), Path(data_dir)
    roles = [r for r in onrc.FILES if r != "people" or with_people]
    have = registry.status(path)
    if have and have.get("schema") != registry.SCHEMA_VERSION:
        await tell(f"  {path.name} was loaded by an older Harvey; loading it again.")
        have, force = None, True
    local = onrc.local_dataset(data_dir, name, roles) if name != "latest" else None
    if local:
        dataset, files = local
    else:
        timeout = httpx.Timeout(60.0, read=300.0)
        said: list[str] = []

        def progress(role, filename, size):
            mb = f"{size / 1e6:.0f} MB" if size else "size unknown"
            said.append(f"    downloading {filename} ({mb}), once per dataset...")
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                dataset = await (onrc.latest_dataset(client) if name == "latest"
                                 else onrc.dataset_named(client, name))
                if have and have.get("dataset") == dataset.name and not force:
                    return have
                files = await onrc.fetch_files(client, dataset, data_dir, roles=roles,
                                               progress=progress)
        except httpx.HTTPError as e:
            for line in said:
                await tell(line)
            why = f"data.gov.ro unreachable ({e.__class__.__name__})"
            if have and offline_ok:
                await tell(f"  {why}; using the registry already loaded ({have.get('dataset')})")
                return have
            local = newest_local_dataset(data_dir, roles) if name == "latest" else None
            if not local:
                raise JobError(f"{why}, and no complete dataset is downloaded in {data_dir}."
                               + (f" The registry holds {have.get('dataset')}." if have else "")
                               + " Try again when online.", code=2)
            dataset, files = local
            await tell(f"  {why}; using {dataset.name}, already downloaded")
        else:
            for line in said:
                await tell(line)
    if have and have.get("dataset") == dataset.name and not force:
        return have
    await tell(f"  Loading {dataset.name} (registry as of {dataset.date}) into {path.name}; "
               "a few minutes, once per dataset...")
    loop = asyncio.get_running_loop()

    def load_progress(step, rows):
        line = f"    {step}..." if rows is None else f"    {step}: {rows:,} rows read"
        if say is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(say(line)))
    try:
        loaded = await api.capability("onrc", "registry_sync")(
            api.Input(extra={"files": files, "dataset": dataset, "path": path,
                             "progress": load_progress}), api.Context())
        return loaded.values["registry"]
    except registry.RegistryBusy as e:
        if have and offline_ok:
            await tell(f"  Another load is running; using the registry already loaded "
                       f"({have.get('dataset')})")
            return have
        raise JobError(f"{e}.", code=2)


def newest_local_dataset(data_dir, roles):
    """The newest dataset fully downloaded under data_dir/<date>/, or None."""
    from datetime import date
    from pathlib import Path

    from harvey.collectors import onrc

    folders = sorted((p.name for p in Path(data_dir).glob("????-??-??") if p.is_dir()),
                     reverse=True)
    for folder in folders:
        try:
            when = date.fromisoformat(folder)
        except ValueError:
            continue
        found = onrc.local_dataset(data_dir, f"firme-{when:%d-%m-%Y}", roles)
        if found:
            return found
    return None


# ── Free local lookups by legal identifier (a source's own database) ──

def id_lookups(enabled) -> list[tuple]:
    """(manifest, capability, identifier kind) for every free lookup a
    source module that is on can make from one identifier alone (its CUI):
    what it knows is local, so it is read for every company that has one."""
    from harvey.modules import switches

    out = []
    for m, cap in api.declared(switches.by_id()):
        if m.id not in enabled or m.kind != "source" or cap.get("op") != "lookup":
            continue
        if float((cap.get("cost") or {}).get("usd", 0) or 0) > 0:
            continue
        kinds = [need[0][len("id:"):] for need in cap.get("inputs") or ()
                 if len(need) == 1 and need[0].startswith("id:")]
        if kinds:
            out.append((m, cap, kinds[0]))
    return out


async def lookup_by_id(state, company_ids=None, *, progress=None) -> dict:
    """Read every free local source that knows companies by an identifier
    (the financial statements by CUI) for these companies (all of them when
    None), and record what each knows through the confirmation gate. A
    reading is written once per published file: running it again adds
    nothing, a revised file adds its readings beside the old.

    A capability that declares ``batch`` is called with up to that many
    companies at once (``extra["batch"]``: ``[{"key": <company id>, <kind>:
    <identifier>}]``, its facts and contacts keyed to them), so it opens its
    database once for them; one without is called a company at a time.

    Returns {"observations", "companies", "unconfirmed": {module: [codes]},
    "skipped" (a source with nothing loaded), "errors"}."""
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    enabled = await _enabled(state)
    out = {"observations": 0, "companies": 0, "unconfirmed": {}, "skipped": [], "errors": []}
    for m, cap, kind in id_lookups(enabled):
        codes = list((cap.get("outputs") or {}).get("facts") or ())
        missing = [c for c in codes if c not in confirmed]
        if missing:
            out["unconfirmed"][m.id] = missing
        if not _wanted(cap, confirmed):
            continue
        companies = await state.companies_with_id(kind, company_ids)
        if not companies:
            continue
        run = api.capability(m.id, cap["id"])
        ctx = api.Context(settings=await switches.module_settings(state, m.id),
                          wanted=frozenset(confirmed), enabled=enabled)
        size = int(cap.get("batch") or 0)

        async def read(rows) -> tuple[str | None, dict[str, api.Draft]]:
            """(why it failed or None, company id -> what it knows)."""
            found: dict[str, api.Draft] = {}
            if not size:
                for row in rows:
                    draft = await run(api.Input(company_id=row["id"], name=row["name"] or "",
                                                ids={kind: row["value"]}), ctx)
                    if draft.failed is not None:
                        return draft.failed, found
                    found[row["id"]] = draft
                return None, found
            for at in range(0, len(rows), size):
                part = rows[at:at + size]
                draft = await run(api.Input(extra={"batch": [
                    {"key": r["id"], kind: r["value"]} for r in part]}), ctx)
                if draft.failed is not None:
                    return draft.failed, found
                for r in part:
                    found[r["id"]] = api.Draft()
                for fact in draft.facts:
                    if fact.record in found:
                        found[fact.record].facts.append(replace(fact, record=""))
                for contact in draft.contacts:
                    if contact.record in found:
                        found[contact.record].contacts.append(replace(contact, record=""))
            return None, found

        first_rows = companies[:SOURCE_SAVE_BATCH]
        failed, first = await read(first_rows)
        if failed is not None and not first:
            # Its data isn't there (never loaded): nothing to read, no run.
            out["skipped"].append(f"{m.id}: {failed}")
            continue
        run_id = await state.start_run("lookup", provider=m.id,
                                       params={"capability": cap["id"], "companies": len(companies)})
        written = touched = 0
        try:
            for start in range(0, len(companies), SOURCE_SAVE_BATCH):
                rows_here = companies[start:start + SOURCE_SAVE_BATCH]
                if start:
                    failed, found = await read(rows_here)
                else:
                    found = first
                async with state.batch() as batch:
                    for row in rows_here:
                        draft = found.get(row["id"])
                        if draft is None:
                            continue
                        rows = ingest.rows(draft.facts, company_id=row["id"], run_id=run_id,
                                           confirmed=confirmed)
                        await ingest.company_phones(batch, row["id"], draft.contacts)
                        fresh = []
                        for url in dict.fromkeys(r["evidence_url"] for r in rows):
                            fresh += await ingest.unseen(
                                batch, row["id"], url, [r for r in rows if r["evidence_url"] == url])
                        if fresh:
                            touched += 1
                            written += await ingest.write(batch, fresh, run_id)
                if failed is not None:
                    out["errors"].append(f"{m.id}: {failed}")
                    break
                if progress:
                    progress(min(start + SOURCE_SAVE_BATCH, len(companies)), len(companies))
            await state.finish_run(run_id, status="failed" if out["errors"] else "completed",
                                   records=touched, error="; ".join(out["errors"][:1]))
        except Exception as e:
            logger.exception("%s lookup failed", m.id)
            out["errors"].append(f"{m.id}: {e}")
            await state.finish_run(run_id, status="failed", records=touched, error=str(e))
        out["observations"] += written
        out["companies"] += touched
    return out


# ── Checks by legal identifier at an outside service (an enricher) ──

def id_enrichers(enabled, *, after_source: bool = False) -> list[tuple]:
    """(manifest, capability, identifier kind) for every free lookup an
    enricher module that is on makes from one identifier (a CUI), asked
    of an outside service a batch at a time. ``after_source`` keeps only
    the ones that run for the companies a source brings in."""
    from harvey.modules import switches

    out = []
    for m, cap in api.declared(switches.by_id()):
        if m.id not in enabled or m.kind != "enricher" or cap.get("op") != "lookup":
            continue
        if float((cap.get("cost") or {}).get("usd", 0) or 0) > 0:
            continue
        if after_source and not cap.get("after_source"):
            continue
        kinds = [need[0][len("id:"):] for need in cap.get("inputs") or ()
                 if len(need) == 1 and need[0].startswith("id:")]
        if kinds:
            out.append((m, cap, kinds[0]))
    return out


# A check by identifier stops after this many failed batches in a row.
CHECK_FAILURES_IN_A_ROW = 2

# Why a check by identifier asked nothing: none of its signals is confirmed.
NOTHING_CONFIRMED = "unconfirmed"


def _wanted(cap: dict, confirmed) -> bool:
    """Whether any signal a capability answers with is confirmed: a free
    lookup by identifier is made only then (Harvey proposes, the user
    confirms)."""
    return bool(set((cap.get("outputs") or {}).get("facts") or ()) & set(confirmed))


def _new_check_report(module: str) -> dict:
    return {"module": module, "companies": 0, "checked": 0, "cached": 0, "not_found": 0,
            "invalid": 0, "requests": 0, "observations": 0, "updated": 0, "phones_set": 0,
            "unconfirmed": [], "errors": [], "stopped": ""}


async def check_by_id(state, module_id: str, cap_id: str, company_ids=None, *,
                      missing_only: bool = False, progress=None, http=None,
                      stop=None, run_id: str = "") -> dict:
    """Ask an enricher's lookup-by-identifier capability about these
    companies (every company with that identifier when None), a batch at
    a time (the capability's ``batch``), and record what it answers: each
    reading through the confirmation gate, written only when it says
    something new (ingest.changed), and a phone as the company's phone when
    it has none. ``missing_only`` asks only about companies the module has
    never answered for. ``progress(done, total)`` after each batch;
    ``stop()`` returning True ends it between batches. ``run_id`` adds to a
    run already open (a follow-up's) instead of logging one of its own.
    Refused while the module is off.

    Returns {"companies", "checked", "cached", "not_found", "invalid",
    "requests", "observations", "updated", "phones_set", "unconfirmed",
    "errors", "stopped"}."""
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    await switches.require(state, module_id)
    manifest = switches.by_id()[module_id]
    cap = next(c for c in manifest.capabilities if c.get("id") == cap_id)
    kinds = [need[0][len("id:"):] for need in cap.get("inputs") or ()
             if len(need) == 1 and need[0].startswith("id:")]
    if not kinds:
        raise ValueError(f"{module_id}.{cap_id} doesn't look companies up by an identifier")
    kind = kinds[0]
    size = max(1, int(cap.get("batch") or 1))
    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    enabled = await _enabled(state)
    out = _new_check_report(module_id)
    codes = list((cap.get("outputs") or {}).get("facts") or ())
    out["unconfirmed"] = [c for c in codes if c not in confirmed]
    companies = await state.companies_with_id(kind, company_ids)
    out["companies"] = len(companies)
    if not companies:
        return out
    if not _wanted(cap, confirmed):
        # Nothing it answers is confirmed: no request, no phone, no run.
        out["stopped"] = NOTHING_CONFIRMED
        return out
    run = api.capability(module_id, cap_id)
    many = ingest.many_codes()
    owned = http is None
    if owned:
        http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True)
    own_run = not run_id
    if own_run:
        run_id = await state.start_run("lookup", provider=module_id,
                                       params={"capability": cap_id, "companies": len(companies),
                                               "missing_only": missing_only})
    touched = 0
    if progress:
        progress(0, len(companies))
    try:
        ctx = api.Context(http=http, settings=await switches.module_settings(state, module_id),
                          wanted=frozenset(confirmed), enabled=enabled)
        done = failed_in_a_row = 0
        for start in range(0, len(companies), size):
            if stop is not None and stop():
                out["stopped"] = "stopped"
                break
            chunk = companies[start:start + size]
            draft = await run(api.Input(extra={
                "batch": [{"key": r["id"], kind: r["value"]} for r in chunk],
                "missing_only": missing_only}), ctx)
            values = draft.values or {}
            for key in ("cached", "not_found", "invalid", "requests"):
                out[key] += int(values.get(key) or 0)
            out["checked"] += int(values.get("asked") or 0)
            done += len(chunk)
            if draft.failed is not None:
                out["errors"].append(draft.failed)
                failed_in_a_row += 1
                if draft.stop or failed_in_a_row >= CHECK_FAILURES_IN_A_ROW:
                    # The service is down or busy: stop asking it.
                    out["stopped"] = draft.stop or "failed"
                    break
                if progress:
                    progress(done, len(companies))
                continue
            failed_in_a_row = 0
            facts_of: dict[str, list[api.Fact]] = {}
            for fact in draft.facts:
                facts_of.setdefault(fact.record, []).append(fact)
            contacts_of: dict[str, list[api.Contact]] = {}
            for contact in draft.contacts:
                contacts_of.setdefault(contact.record, []).append(contact)
            async with state.batch() as batch:
                for row in chunk:
                    cid = row["id"]
                    rows = ingest.rows(facts_of.get(cid, []), run_id=run_id, confirmed=confirmed,
                                       records={cid: ingest.Saved(cid, cid)})
                    fresh = await ingest.changed(batch, cid, module_id, rows, many)
                    written = await ingest.write(batch, fresh, run_id)
                    out["observations"] += written
                    out["phones_set"] += await ingest.company_phones(
                        batch, cid, contacts_of.get(cid, []))
                    touched += bool(written)
            if progress:
                progress(done, len(companies))
        out["updated"] = touched
        if own_run:
            await state.finish_run(run_id, status="failed" if out["errors"] and not touched
                                   else "completed", records=touched,
                                   error="; ".join(out["errors"][:1]))
    except Exception as e:
        logger.exception("%s check failed", module_id)
        out["errors"].append(str(e))
        out["updated"] = touched
        if own_run:
            await state.finish_run(run_id, status="failed", records=touched, error=str(e))
    finally:
        if owned:
            await http.aclose()
    return out


class FollowUp:
    """Checks that run, in the background, for the companies a source
    brings in: every enricher capability marked ``after_source`` whose
    module is on (ANAF's company check). Saved companies are queued with
    ``add(ids)`` as the source saves them and checked a batch at a time
    while it goes on; ``finish()`` waits for the queue to empty and
    returns one report per capability. ``progress(module, done, total)``
    says how far each has got."""

    def __init__(self, state, progress=None):
        self.state = state
        self.progress = progress
        self.checks: list[tuple] = []
        self.queue: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.reports: dict[str, dict] = {}
        self.runs: dict[str, str] = {}
        self.queued = 0
        self.done = 0
        self._stop = False
        self._failures: dict[str, int] = {}
        self._given_up: set[str] = set()

    async def start(self) -> bool:
        """Find the checks to run; False (and nothing started) when none is
        on, or none has a confirmed signal to record."""
        from harvey.signals import sync_vocabulary

        await sync_vocabulary(self.state)
        confirmed = await self.state.confirmed_signal_codes()
        self.checks = [c for c in id_enrichers(await _enabled(self.state), after_source=True)
                       if _wanted(c[1], confirmed)]
        if not self.checks:
            return False
        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self._work())
        return True

    def add(self, company_ids) -> None:
        if self.queue is None:
            return
        ids = [i for i in company_ids if i]
        if ids:
            self.queued += len(ids)
            self.queue.put_nowait(ids)

    def stop(self) -> None:
        self._stop = True

    async def _work(self):
        size = max([int(cap.get("batch") or 1) for _, cap, _ in self.checks] or [1])
        pending: list[str] = []
        closing = False
        while True:
            if not closing and len(pending) < size:
                item = await self.queue.get()
                if item is None:
                    closing = True
                else:
                    pending += item
                    continue
            if not pending:
                break
            take, pending = pending[:size], pending[size:]
            for m, cap, _ in self.checks:
                if m.id in self._given_up:
                    continue
                if m.id not in self.runs:
                    self.runs[m.id] = await self.state.start_run(
                        "lookup", provider=m.id, params={"capability": cap["id"],
                                                         "after": "source"})
                report = await check_by_id(self.state, m.id, cap["id"], take,
                                           stop=lambda: self._stop, run_id=self.runs[m.id])
                self._merge(m.id, report)
                # A service that is down or busy isn't asked about the
                # rest of what the source brings in (check_by_id --missing
                # finishes later).
                failed = bool(report["errors"])
                self._failures[m.id] = self._failures.get(m.id, 0) + 1 if failed else 0
                if (report["stopped"] and report["stopped"] != "stopped") or \
                        self._failures[m.id] >= CHECK_FAILURES_IN_A_ROW:
                    self._given_up.add(m.id)
            self.done += len(take)
            if self.progress:
                self.progress(self.done, self.queued)
            if self._stop:
                break

    def _merge(self, module: str, report: dict) -> None:
        total = self.reports.setdefault(module, _new_check_report(module))
        for key, value in report.items():
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] += value
        total["unconfirmed"] = report["unconfirmed"] or total["unconfirmed"]
        total["errors"] += [e for e in report["errors"] if e not in total["errors"]][:3]
        total["stopped"] = report["stopped"] or total["stopped"]

    async def finish(self) -> dict[str, dict]:
        if self.task is None:
            return {}
        self.queue.put_nowait(None)
        try:
            await self.task
        except Exception as e:
            logger.exception("follow-up checks failed")
            for m, _, _ in self.checks:
                self.reports.setdefault(m.id, _new_check_report(m.id))["errors"].append(str(e))
        for module, run_id in self.runs.items():
            report = self.reports.get(module) or _new_check_report(module)
            await self.state.finish_run(
                run_id, status="failed" if report["errors"] and not report["updated"]
                else "completed", records=report["updated"],
                error="; ".join(report["errors"][:1]))
        return self.reports


async def sync_source(state, module_id: str, cap_id: str, *, extra: dict | None = None,
                      http=None) -> api.Draft:
    """Run a source module's sync capability (a check of what is published,
    or a download and load) with the module's settings; refused while the
    module is off. Opens an HTTP client when none is given."""
    from harvey.modules import switches

    await switches.require(state, module_id)
    settings = await switches.module_settings(state, module_id)
    owned = http is None
    if owned:
        http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0),
                                 follow_redirects=True)
    try:
        return await api.capability(module_id, cap_id)(
            api.Input(extra=dict(extra or {})),
            api.Context(http=http, settings=settings, enabled=await _enabled(state)))
    finally:
        if owned:
            await http.aclose()


# The settings a source module declares to be kept up to date on its own.
AUTO_SETTING, CHECK_DAYS_SETTING = "auto_update", "check_days"
CHECKED_KEY = "modules.{}.checked_at"


async def sync_due_sources(state) -> list[str]:
    """For each source module that is on and whose ``auto_update`` setting
    the user switched on: when its ``check_days`` have passed since the last
    check, download and load what is new (its sync capability marked
    ``auto = true``), then
    record what it knows about the companies on file. Nothing happens for a
    module whose auto_update is off (the default). Returns the modules synced."""
    from datetime import datetime, timedelta, timezone

    from harvey.modules import switches

    done = []
    enabled = await _enabled(state)
    for m in switches.manifests():
        if m.id not in enabled or m.kind != "source" or AUTO_SETTING not in m.settings:
            continue
        settings = await switches.module_settings(state, m.id)
        if not settings.get(AUTO_SETTING):
            continue
        # The sync its manifest marks as the one to run unattended.
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
            draft = await sync_source(state, m.id, sync[0])
        except Exception as e:
            logger.warning("%s: automatic update failed: %s", m.id, e)
            continue
        if not draft.values.get("unchanged"):
            await lookup_by_id(state)
        done.append(m.id)
    return done


# ── Reading companies' sites (the website module and what reads its pages) ──

_profile_log = logging.getLogger("harvey.collectors.profile")


def _hit_codes(enabled, confirmed) -> tuple[str, ...]:
    """The signals a site read is told a preset search found the company
    by: each ``fact:<CODE>`` input a read capability of a module that is on
    declares, when the code is confirmed (a page saying nothing doesn't
    overwrite a hit)."""
    from harvey.modules import switches

    codes = {key[len("fact:"):] for m, cap in api.declared(switches.by_id())
             if m.id in enabled and cap.get("op") == "read"
             for need in cap.get("inputs") or () for key in need if key.startswith("fact:")}
    return tuple(sorted(codes & set(confirmed)))


async def _known(state, company: dict, codes) -> tuple[dict, str]:
    """What a site read is told about the company: which of ``codes`` a
    preset search found it by, and the page that search found it on."""
    company_id = company["id"]
    hits = {}
    for code in codes:
        try:
            hits[code] = await state.search_hit(company_id, code)
        except Exception:
            hits[code] = False
    source_url = company.get("source_url")
    if source_url is None:
        try:
            source_url = getattr(await state.get_company(company_id), "source_url", "") or ""
        except Exception:
            source_url = ""
    return hits, source_url


async def profile(state, companies: list[dict], confirmed: set[str], run_id: str = "",
                  config=None, role_keywords: list[str] | None = None) -> int:
    """Read many companies' sites (the website module's read_site), and
    write what each read found before the next: the observations are the
    asset, and a long run that dies must not take them with it. Returns
    the observations written."""
    from harvey.browser import PageReader
    from harvey.collectors import profile as P

    cap = api.capability("website", "read_site")
    total = 0
    enabled = await _enabled(state)
    hit_codes = _hit_codes(enabled, confirmed)
    client, pages = P.open_http(), PageReader()
    try:
        ctx = api.Context(http=client, browser=pages, config=config,
                          wanted=frozenset(confirmed), enabled=enabled,
                          settings={"role_keywords": list(role_keywords or [])})
        for company in companies:
            domain = (company.get("domain") or "").strip()
            if not domain:
                continue
            company_id = company["id"]
            hits, source_url = await _known(state, company, hit_codes)
            inp = api.Input(company_id=company_id, name=company.get("name") or "",
                            domain=domain, locality=company.get("location") or "",
                            facts={"search_hits": hits}, extra={"source_url": source_url})
            try:
                draft = await cap(inp, ctx)
            except Exception as e:
                _profile_log.warning(f"profile failed for {domain}: {e}")
                await _mark_profiled(state, company_id)
                continue
            # Marked regardless of outcome — an unreachable site still counts
            # as checked, or the same handful gets retried every cycle
            # forever instead of rotating through the rest of the backlog.
            await _mark_profiled(state, company_id)
            better = draft.values.get("name")
            if better:
                try:
                    await ingest.company_name(state, company_id, better)
                    _profile_log.info(f"profile: {domain} named '{better}' "
                                      f"(was '{company.get('name') or ''}')")
                except Exception as e:
                    _profile_log.debug(f"rename_company failed for {domain}: {e}")
            phones = [c for c in draft.contacts if c.kind == "phone"]
            if phones:
                # Contact data like a published email: it doesn't wait for
                # PHONE_NUMBER to be confirmed.
                try:
                    if await ingest.company_phone(state, company_id, phones[0].display):
                        _profile_log.info(f"profile: {company.get('domain')} phone "
                                          f"{phones[0].display} from its site")
                except Exception as e:
                    _profile_log.debug(f"fill_company_phone failed for {company.get('domain')}: {e}")
            if draft.facts:
                total += await ingest.write(state, ingest.rows(
                    draft.facts, company_id=company_id, run_id=run_id,
                    confirmed=confirmed), run_id)
                _profile_log.info(f"profile: {domain} → {len(draft.facts)} observation(s)")
            await asyncio.sleep(random.uniform(0.4, 1.2))
    finally:
        await client.aclose()
        await pages.aclose()
    return total


async def _mark_profiled(state, company_id: str):
    try:
        await state.mark_company_profiled(company_id)
    except Exception as e:
        _profile_log.debug(f"mark_company_profiled failed for {company_id}: {e}")


async def profile_companies(state, companies: list[dict], config=None) -> tuple[int, str]:
    """Opens a run, reads the companies' sites, closes the run.

    ``config`` supplies the roles that count as hiring intent
    (``icp.hiring_signals``, else ``icp.titles``); without it a job board
    still gives OPEN_ROLES, but no role is singled out.
    """
    from harvey.collectors.ats import role_keywords
    from harvey.signals import sync_vocabulary

    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    if not confirmed:
        _profile_log.warning(
            "profile: no signals confirmed yet — confirm signals first "
            "(nothing is collected until you choose what matters)."
        )
        return 0, ""
    run_id = await state.start_run("profile", provider="http")
    try:
        keywords = role_keywords(getattr(config, "icp", None))
        n = await profile(state, companies, confirmed, run_id=run_id, config=config,
                          role_keywords=keywords)
        await state.finish_run(run_id, records=n, cost_usd=0.0)
        return n, run_id
    except Exception as e:
        await state.finish_run(run_id, status="failed", error=str(e))
        raise
