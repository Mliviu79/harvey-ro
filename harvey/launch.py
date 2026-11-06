"""What a button that queues work reads from its request: the Discover
run's sources and searches, the companies a Find-contacts run takes, the
links a verification takes, the money amounts, and the estimates shown
before each; and the two writes the Campaigns page makes before a
campaign exists to start (an audience saved, a draft made).

The dashboard's endpoints (harvey/dashboard.py) and the agent's action
tools (harvey/agent/actions.py) both go through these, then through the
same enqueue functions (harvey/modules/job_kinds.py), so a run the agent
queues is the run the same request on the page would queue: the same
selection, the same caps, the same module switches. Nothing here spends,
queues or starts anything.
"""

from __future__ import annotations

import asyncio
import math

# A Find-contacts run takes at most this many companies (the newest first);
# a wider filter is taken in more runs.
FIND_MAX = 50_000
# What a Discover run may spend looking up websites for the companies it
# saves with none when the request names no amount: nothing, so a caller's
# max_spend is all it spends; free lookups still run while the option is
# on. (The Discover tab sends the amount its field shows.)
WEBSITES_DEFAULT_USD = 0.0
# Search phrases one request may name, and places each runs in.
TERMS_MAX = 50
# What a tiled search's estimate says beside its cells.
CELLS_NOTE = ("a capped map search starts with these cells; a cell that comes back full is "
              "split into four as it runs, which can cost more, always within the cap")


def money(value, default: float | None = None) -> float:
    """A dollar amount from a request: finite and >= 0, else ValueError."""
    if value is None or value == "":
        if default is None:
            raise ValueError("expected a number of dollars")
        return default
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("expected a number of dollars, 0 or more")
    return amount


# ── Discover ──

def source_keys(body: dict) -> list[str]:
    """The sources a Discover request names: ``providers`` (a list), or
    the older single ``provider``."""
    keys = body.get("providers")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    if not isinstance(keys, list) or not keys:
        keys = [body.get("provider") or ""]
    return list(dict.fromkeys(str(k) for k in keys if k))


async def discovery_refusal(state, body: dict):
    """Why a source or the preset asked for can't run (a ModuleOff: its
    module is off), or None."""
    from harvey.modules import switches
    from harvey.modules.job_kinds import discover_sources

    enabled = await switches.enabled_ids(state)
    jobbed = {s["key"]: s["module"] for s in discover_sources()}
    for key in source_keys(body) or [""]:
        if key in jobbed:
            if jobbed[key] not in enabled:
                return switches.ModuleOff(jobbed[key], f"The {key} discovery source")
            continue
        problem = switches.discovery_problem(enabled, key, str(body.get("preset") or ""))
        if problem:
            return problem
    return None


def _unit_id(item) -> int | None:
    """A boundary unit's id from an area a request names (5, "5",
    "unit:5"), or None for a place name."""
    if isinstance(item, int) and not isinstance(item, bool):
        return item
    text = str(item or "").strip().lower()
    text = text[len("unit:"):] if text.startswith("unit:") else text
    return int(text) if text.isdigit() else None


async def discovery_areas(state, body: dict, *, network: bool = True) -> list:
    """The areas a Discover request names (``areas``: boundary unit ids or
    place names, at most TERMS_MAX), each as a geo.Area: a unit from the
    boundaries file (the boundaries module on and loaded), a place from
    the boundaries or Nominatim (``network`` False: only what is cached).
    ValueError for one that can't be found. They are kept on the request
    as ``_areas`` for discovery_queries, one inside another marked
    ``inside`` (the listings circle or map grid of the outer one covers
    it)."""
    from harvey.modules import runs

    raw = body.get("areas") or []
    if isinstance(raw, (str, int)):
        raw = [raw]
    out = []
    for item in [i for i in raw if str(i).strip()][:TERMS_MAX]:
        unit = _unit_id(item)
        if unit is not None:
            found = await runs.boundary_unit(state, unit)
            if found is None:
                raise ValueError(f"no boundary unit {unit}: the boundaries module must be on "
                                 "and loaded (areas() names the units)")
            area = runs.unit_area(found)
        else:
            area = await runs.place_area(state, str(item).strip(), network=network)
            if area is None:
                raise ValueError(f"{str(item).strip()!r}: no such place found"
                                 + ("" if network else " (not looked up yet: give a "
                                    "boundary unit, or a place a run looked up before)"))
        out.append(area)
    kept = [a.as_dict() for a in out]
    for i, inner in enumerate(out):
        for j, outer in enumerate(out):
            if j != i and "inside" not in kept[j] and await _within(state, inner, outer):
                # A listings circle or a map grid over the outer one already
                # covers it (and pays for what it finds once): only a
                # source that ranks from each place searches it.
                kept[i]["inside"] = outer.name
                break
    body["_areas"] = kept
    return out


async def _within(state, inner, outer) -> bool:
    """Whether all of ``inner``'s box is in ``outer`` (by its outline when
    it has one, else its box): what a search over ``outer`` keeps covers
    it."""
    from harvey.modules import runs

    if inner.bbox is None or not outer.shaped or outer.name == inner.name:
        return False
    if outer.unit is not None and not outer.rings:
        unit = await runs.boundary_unit(state, outer.unit)
        outer.rings = (unit or {}).get("rings") or []
    s, w, n, e = inner.bbox
    return all(outer.contains(la, lo) is True
               for la, lo in ((s, w), (s, e), (n, w), (n, e), ((s + n) / 2, (w + e) / 2)))


def discovery_queries(body: dict, config, provider: str = ""):
    """The searches a Discover request makes at ``provider``: from Harvey's
    settings (the target industries and places, or a preset's phrases),
    or the request's own ``terms`` in each of its ``cities`` (the settings'
    places when it names none), or in each of its areas (``_areas``, from
    discovery_areas: each search then carries its circle and area).
    ValueError for a preset the source can't search with."""
    from harvey import geo
    from harvey.collectors.discover import (DiscoveryQuery, area_shape, build_queries,
                                            preset_problem)

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, provider or str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [str(c).strip() for c in (body.get("cities") or []) if str(c).strip()] or None
    areas = {a["name"]: a for a in body.get("_areas") or [] if a.get("name")}
    if areas:
        cities = list(areas)
    # No depth or limit: every result the source has (the cap stops the run).
    depth = int(body.get("depth") or 0) or None
    limit = int(body.get("limit") or 0) or None
    terms = [str(t).strip() for t in (body.get("terms") or []) if str(t).strip()]
    if terms:
        places = cities or list(config.icp.geography) or [""]
        coords = getattr(config.icp, "geo_coordinates", {}) or {}
        language = getattr(config.icp, "search_language", "") or "en"
        out = [DiscoveryQuery(term=t, location=c, depth=depth, limit=limit,
                              coordinate=coords.get(c, ""), language=language)
               for t in terms[:TERMS_MAX] for c in places[:TERMS_MAX]]
    else:
        out = build_queries(config, cities=cities, depth=depth, limit=limit, preset=preset)
    covers = bool(provider) and area_shape(provider)[0] in ("circle", "grid")
    for q in out:
        if q.location in areas:
            q.area = dict(areas[q.location])
            q.coordinate = geo.Area.from_dict(q.area).coordinate
    # An area inside another the request names is searched only by a source
    # that ranks from its point: a circle or grid over the outer one covers it.
    return [q for q in out if not (covers and q.area.get("inside"))]


async def place_queries(state, plan: list[dict], *, network: bool = True) -> None:
    """Give each search of a source that searches an area (a listings
    circle, a map grid) the area of its place, as the run will: what the
    estimate counts cells over. ``network`` False: only places already
    looked up (or in the boundaries)."""
    from harvey import geo
    from harvey.collectors.discover import area_shape
    from harvey.modules import runs

    found: dict[str, object] = {}
    for part in plan:
        if not part.get("provider") or area_shape(part["provider"])[0] not in ("circle", "grid"):
            continue
        for q in part["queries"]:
            if q.area or not q.location:
                if q.coordinate and not q.area:
                    area = geo.Area.from_coordinate(q.coordinate, name=q.location)
                    q.area = area.as_dict() if area else {}
                continue
            if q.location not in found:
                found[q.location] = await runs.place_area(state, q.location, network=network)
            area = found[q.location]
            if area is not None:
                q.area = area.as_dict()
                q.coordinate = q.coordinate or area.coordinate


def discovery_plan(body: dict, config, queries=None) -> tuple[list[dict], list[str]]:
    """One part per source the request ticked: a search source's queries,
    or a registry pull's parameters from its filters. A source that can't
    take this run (a preset it can't search with, filters that say
    nothing) is skipped with a note, unless it is the only one: then the
    run is refused (ValueError). Unknown sources are refused (LookupError).
    ``queries``: what makes a source's searches (discovery_queries)."""
    from harvey.collectors.discover import PROVIDERS, is_discovery_source
    from harvey.modules.job_kinds import discover_sources, lookup_only_message, registry_plan

    queries = queries or discovery_queries
    keys = source_keys(body)
    if not keys:
        raise ValueError("pick at least one source")
    jobbed = {s["key"]: s for s in discover_sources()}
    filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    plan, notes = [], []
    for key in keys:
        try:
            if key in PROVIDERS and not is_discovery_source(key):
                raise LookupError(lookup_only_message(key))
            if key in PROVIDERS:
                plan.append({"provider": key, "label": PROVIDERS[key].label,
                             "queries": queries(body, config, key)})
            elif key in jobbed:
                src = jobbed[key]
                if src["job"] != "registry_pull":
                    # Only the registry's filters are read here: another
                    # source run as a job needs its own planner first.
                    raise ValueError(f"{key} can't be run from Discover yet")
                params = registry_plan(filters.get(key) or {})
                plan.append({"source": key, "job": src["job"], "label": params.pop("label"),
                             "params": params, "name": src["label"]})
            else:
                raise LookupError(f"unknown provider {key!r}")
        except LookupError:
            raise
        except ValueError as e:
            if len(keys) == 1:
                raise
            notes.append(f"{key}: {e}")
    if not plan:
        raise ValueError("; ".join(notes) or "no source can take this run")
    return plan, notes


def find_websites(body: dict) -> float | None:
    """A Discover request's "also find websites, up to $X": the amount
    (``websites_max_spend``, $0 when absent: free lookups only), or None
    when ``find_websites`` is false. On unless turned off. ValueError for
    an amount that isn't one."""
    if body.get("find_websites") is False:
        return None
    return money(body.get("websites_max_spend"), default=WEBSITES_DEFAULT_USD)


async def websites_estimate(state, amount: float | None) -> dict:
    """What "find websites" would use: the searches that can find a
    company by name (free ones first), the price of a paid lookup, and how
    many paid lookups the amount pays for. How many companies will need
    one isn't known before the run."""
    from harvey import finder
    from harvey.config import load_env
    from harvey.modules import switches

    if amount is None:
        return {"enabled": False}
    found = finder.searchers(await switches.enabled_ids(state), load_env().model_dump())
    paid = [s for s in found if s.estimate > 0]
    cheapest = min((s.estimate for s in paid), default=0.0)
    return {"enabled": True, "max_spend": amount,
            "free": sorted({s.module for s in found if s.estimate <= 0}),
            "paid": sorted({s.module for s in paid}),
            "per_lookup": round(cheapest, 4),
            "paid_lookups": int(amount / cheapest + 1e-9) if cheapest else 0,
            "searchers": [{"module": s.module, "provider": s.provider, "cost": s.estimate}
                          for s in found]}


async def discovery_estimate(state, config, plan: list[dict], notes: list[str],
                             websites: float | None, *, count: bool = True) -> dict:
    """Projected spend and the exact query list of a Discover run's
    ``plan``: the sum, and each source's own. ``count``: a source that can
    count (DataForSEO Business Listings) is asked how many results each
    search has first, a small paid request each, so the price is exact;
    the answer says what counting cost. Without it nothing is asked of a
    source (the agent's estimate). The registry's part is its free count
    of the firms that match."""
    from harvey.collectors.discover import PROVIDERS, area_cells, area_shape, estimate_cost
    from harvey.modules.job_kinds import registry_count
    from harvey.modules.runs import run_discovery

    await place_queries(state, plan, network=count)
    parts = []
    for part in plan:
        if part.get("provider"):
            key, queries = part["provider"], part["queries"]
            shape, most = area_shape(key)
            areas = {"shape": shape, "max_results": most,
                     "areas": len({q.location for q in queries if q.area}),
                     "cells": sum(area_cells(key, q) for q in queries),
                     "radius_km": sorted({q.area["radius_km"] for q in queries
                                          if q.area.get("radius_km")})[:10]}
            counted = {"counted": False, "available": None, "count_cost": 0.0, "errors": []}
            if count and PROVIDERS[key].can_count:
                report = await run_discovery(state, config, key, queries,
                                             dry_run=True, count=True)
                counted = {"counted": report.counted, "available": report.available,
                           "count_cost": round(report.count_cost, 4),
                           "errors": report.errors}
            parts.append({"key": key, "label": part["label"],
                          "queries": [q.keyword() for q in queries],
                          "query_count": len(queries),
                          "estimated_cost": round(estimate_cost(key, queries), 4),
                          "free": PROVIDERS[key].estimate(queries) == 0, **areas, **counted})
        else:
            try:
                n = await asyncio.to_thread(registry_count, part["params"])
                errors = []
            except FileNotFoundError as e:
                n, errors = None, [f"{e} (the filter needs it)"]
            parts.append({"key": part["source"], "label": part["label"],
                          "queries": [part["label"]], "query_count": 1,
                          "estimated_cost": 0.0, "free": True, "counted": n is not None,
                          "available": n, "count_cost": 0.0,
                          "errors": errors or ([] if n is not None else [
                              "no registry loaded yet: the run loads it first (about "
                              "1.5 GB, once per dataset)"])})
    queries = [q for p_ in parts for q in p_["queries"]]
    counted_all = all(p_["counted"] for p_ in parts)
    return {
        "find_websites": await websites_estimate(state, websites),
        "provider": parts[0]["key"],
        "sources": parts, "notes": notes,
        "queries": queries, "query_count": len(queries),
        "estimated_cost": round(sum(p_["estimated_cost"] for p_ in parts), 4),
        "free": all(p_["free"] for p_ in parts),
        "counted": counted_all if len(parts) > 1 else parts[0]["counted"],
        "available": (sum(p_["available"] or 0 for p_ in parts)
                      if any(p_["available"] is not None for p_ in parts) else None),
        "count_cost": round(sum(p_["count_cost"] for p_ in parts), 4),
        "errors": [e for p_ in parts for e in p_["errors"]],
        "cells_note": CELLS_NOTE if any(p_.get("shape") == "grid" and p_.get("areas")
                                        for p_ in parts) else "",
    }


# ── Find contacts ──

async def find_selection(state, body: dict) -> tuple[list[dict], str]:
    """(the companies a Find-contacts request selects, its filter's error
    or "") from {"filter": {the Contacts page's filters}, "company_ids",
    "force", "limit"}."""
    from harvey import finder
    from harvey.contact_query import filter_from_query

    raw = body.get("filter") if isinstance(body.get("filter"), dict) else {}
    try:
        f, attach = await filter_from_query(state, {k: str(v) for k, v in raw.items()
                                                    if v is not None})
    except ValueError as e:
        return [], str(e)
    try:
        limit = max(1, min(int(body.get("limit") or FIND_MAX), FIND_MAX))
    except (TypeError, ValueError):
        return [], "limit must be a number"
    ids = body.get("company_ids") if isinstance(body.get("company_ids"), list) else None
    selected = await finder.select(state, f, ids=ids, force=bool(body.get("force")),
                                   limit=limit, attach_figures=attach)
    return selected, ""


async def find_estimate(state, selected: list[dict], force: bool) -> dict:
    """What a Find-contacts run over ``selected`` would do and may cost
    (finder.plan, finder.estimate), with ``checks`` (the address checks:
    finder.checks_estimate) and ``classifier`` (its calls as a range:
    finder.classify_estimate). Estimates: the run's caps are what is
    enforced, step by step."""
    from harvey import finder
    from harvey.config import load_env
    from harvey.modules import switches

    enabled, env = await switches.enabled_ids(state), load_env().model_dump()
    # (What the run will do to each: searched already counts, unless forced.)
    selected = await finder.plan(state, selected, enabled, env, force=force)
    out = finder.estimate(selected, enabled, env)
    return {**out, "more": len(selected) >= FIND_MAX,
            "checks": await finder.checks_estimate(state, selected, enabled, env),
            "classifier": await finder.classify_estimate(state, selected)}


# ── Verify contacts ──

async def verify_selection(state, body: dict) -> tuple[list[dict], str]:
    """The links a verification takes: {"contact_ids": [...]} or {"filter":
    {the Contacts page's filters}, "limit"}; at most contact_check.MAX_LINKS."""
    from harvey import contact_check
    from harvey.contact_query import filter_from_query

    try:
        limit = max(1, min(int(body.get("limit") or 200), contact_check.MAX_LINKS))
    except (TypeError, ValueError):
        return [], "limit must be a number"
    ids = body.get("contact_ids")
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return [], "contact_ids is a list of contact ids"
    else:
        raw = body.get("filter") if isinstance(body.get("filter"), dict) else {}
        try:
            f, _ = await filter_from_query(state, {k: str(v) for k, v in raw.items()
                                                   if v is not None})
        except ValueError as e:
            return [], str(e)
        _, _, ids = await state.read("contacts_page", f, "", "desc", limit, 0, cap=limit)
    return await contact_check.links(state, ids[:limit], limit=limit), ""


# ── Campaigns: an audience saved, a draft made ──

async def save_audience(state, name: str, query: str) -> int:
    """A Contacts filter (the page's query string) saved as an audience, as
    "Salvează ca public" saves it: a filter the page can't read isn't
    saved (ValueError), nor one with no name (campaigns.Refused)."""
    from harvey import campaigns
    from harvey.contact_query import filter_from_query

    await filter_from_query(state, query or "")
    return await state.campaigns_call(campaigns.save_audience, str(name or ""), query or "")


async def new_campaign(state, config, body: dict) -> str:
    """A draft campaign from the wizard's request ({"name", "audience_id"
    or "query", "rolling", "offer", "sequence", "li_note", "review",
    "send_to_risky"}), sending from the settings' account: its id.
    send_to_risky is the setting's unless the request says. campaigns.Refused
    when it can't be made (no native provider, no name, no such audience, a
    bad sequence)."""
    from harvey import campaigns

    address = getattr(getattr(config, "persona", None), "email", "") or ""
    risky = body.get("send_to_risky")
    if risky is None:
        risky = bool(getattr(config.channels.email, "send_to_risky", False))
    elif not isinstance(risky, bool):
        raise campaigns.Refused("send_to_risky is true or false")
    return await state.campaigns_call(
        campaigns.create_campaign, provider=config.channels.email.provider,
        address=address, name=str(body.get("name") or ""),
        audience_id=body.get("audience_id"), query=str(body.get("query") or ""),
        rolling=bool(body.get("rolling")), offer=body.get("offer"),
        sequence=body.get("sequence") or None, li_note=str(body.get("li_note") or ""),
        review=str(body.get("review") or "inherit"), send_to_risky=bool(risky))
