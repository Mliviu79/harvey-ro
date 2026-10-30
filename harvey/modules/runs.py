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
                        max_spend: float = 1.0, dry_run: bool = False, count: bool = False):
    """Discover businesses and record what was learned about them: every
    result each search has, page by page, until the spend cap or the kill
    switch stops it (read between pages); a stop says what was left.

    ``dry_run`` estimates and returns without collecting anything. With
    ``count``, a source that can count (DataForSEO Business Listings) is
    asked how many results each search has first, a small paid request per
    search, so the estimate is exact; nothing else is called. Run it first,
    every time: the first small trial is what exposes the data-quality
    problems that would otherwise poison every market you scale to.
    """
    from harvey.collectors import discover as D
    from harvey.config import live_do_not_contact, load_env
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    provider_key = provider_key or D.DEFAULT_PROVIDER
    provider = D.PROVIDERS.get(provider_key)
    if provider is None:
        raise ValueError(f"unknown discovery provider: {provider_key!r}")
    # A source or preset whose module is off is refused (ModuleOff), never
    # run or skipped quietly.
    for preset in sorted({q.preset for q in queries or []} - {""}) or [""]:
        await switches.require_discovery(state, provider_key, preset)

    queries = queries or D.build_queries(config)
    if provider.needs_coordinates:
        queries = await resolve_coordinates(state, queries)
    report = D.DiscoveryReport(provider=provider_key, queries=len(queries))
    report.estimated_cost = provider.estimate(queries)

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
    try:
        searcher = _capability_for("provider", provider_key)
    except api.CapabilityMissing:
        # A provider no module claims (one added in code only): it runs as
        # it is, its facts attributed to no module.
        searcher = D.provider_capabilities("", {provider_key: provider_key})[provider_key]
    presets = {}

    # Each business's base facts are written once per run; its ranks once per
    # (business, keyword), and again only if a repeat search ranks it better.
    seen: set[str] = set()
    best_ranks: dict[tuple[str, str], tuple] = {}
    saved: dict[str, ingest.Saved] = {}
    ranked_in_run: dict[tuple[str, str], set[str]] = {}

    async def paused() -> bool:
        return bool(await state.get_setting("discovery_paused"))

    next_query = 0
    try:
        async with httpx.AsyncClient(timeout=D.TIMEOUT, follow_redirects=True) as client:
            for i, query in enumerate(queries):
                next_query = i
                # The kill switch is read between searches and between a
                # search's pages, not once at the top: a long run has to be
                # stoppable while it is running.
                if await paused():
                    report.stopped = "paused"
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
                                            "paused": paused})
                report.queries_ran += 1
                draft = await cap(api.Input(query=query, area=query.location,
                                            extra={"provider": provider_key}), ctx)
                report.actual_cost += draft.cost
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

        if report.stopped in ("budget reached", "paused"):
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
                            do_not_contact: list[str] | None = None, progress=None):
    """Save what an official source (``module``) read, SOURCE_SAVE_BATCH records
    per transaction, through ingest's rules for an official source.
    ``report`` is the source's own report (its ``values["report"]``); the
    counts in _REPORT_COUNTS are added to it as records are saved, and
    ``unconfirmed`` lists the signals the read collects
    (``values["codes"]``) that aren't confirmed. ``dry_run`` matches
    everything and writes nothing. A fact is written once per dataset:
    re-reading the same dataset adds nothing, a newer one adds its readings
    beside the old. ``progress(done, total)`` is called every
    SOURCE_PROGRESS_EVERY records."""
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
                raise
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
                        area: str = ""):
    """Copy the firms a resolved registry query matches (the first
    ``limit``, best first) into the working database: the onrc module's
    registry_search, then save_source_draft. ACTIVITY_CODE is recorded
    only for the classes the query named."""
    draft = await _source_read(state, "registry_search", api.Input(
        query=q, area=area, extra={"con": con, "limit": limit,
                                   "with_administrators": with_administrators}))
    return await save_source_draft(
        state, draft, draft.values["report"], module="onrc",
        run_params=draft.values.get("run_params"), dry_run=dry_run,
        do_not_contact=do_not_contact, progress=progress)


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
