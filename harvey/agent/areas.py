"""The agent's tools for where and what to search: areas(), plan_cells(),
terms() and phrase_yield().

Every one reads only, and spends nothing: an area comes from the
boundaries file (the boundaries module, when it is on and loaded) or from
Nominatim's point and box (looked up once a place and cached for good,
the same lookup a run makes), the cells and prices from the search
modules' own estimates (harvey/geo.py, the providers), the phrases from
the words Harvey maps to activity classes (harvey/caen.py, the market
pack's), and the yield of each phrase from the discovery jobs' own
reports, counted. Nothing here names a record: places, codes, phrases and
counts only.
"""

from __future__ import annotations

import json
import time

from harvey import queries
from harvey.agent import readonly

# The sub-units an answer names, the candidates it lists, the cells it
# shows, and the phrases phrase_yield gives.
SUB_UNITS_NAMED = 40
CANDIDATES = 8
CELLS_SHOWN = 16
YIELD_ROWS = 50
YIELD_DAYS = 90


async def _boundaries(state) -> dict:
    from harvey.modules import switches
    from harvey.modules.original.boundaries import store

    on = "boundaries" in await switches.enabled_ids(state)
    loaded = store.status() if on else None
    return {"on": on, "loaded": bool(loaded),
            "regions": (loaded or {}).get("regions") or "",
            "units": (loaded or {}).get("units") or 0}


def _unit_summary(unit: dict) -> dict:
    return {k: unit.get(k) for k in ("id", "name", "level", "area_km2", "lat", "lng", "bbox",
                                     "refs", "parent", "parent_name") if k in unit}


async def resolve(state, place, *, writer=None):
    """(the geo.Area a place or unit id names, the boundary unit or None,
    the other units the name may mean). ``writer``: a writable state, so a
    place no gazetteer answered yet is looked up at Nominatim once (and
    cached); without one only what is cached is read."""
    from harvey import launch
    from harvey.modules import runs

    unit_id = launch._unit_id(place)
    if unit_id is not None:
        unit = await runs.boundary_unit(state, unit_id)
        if unit is None:
            raise readonly.Refused(f"no boundary unit {unit_id}: the boundaries module must be "
                                   "on and loaded")
        return runs.unit_area(unit), unit, []
    name = str(place or "").strip()
    if not name:
        raise readonly.Refused("name a place (a town, a county) or a boundary unit's id")
    others = []
    if (await _boundaries(state))["loaded"]:
        from harvey.modules import api

        draft = await api.capability("boundaries", "find_area")(
            api.Input(locality=name), api.Context(enabled=await runs._enabled(state)))
        found = draft.values.get("units") or []
        if found:
            unit = await runs.boundary_unit(state, found[0]["id"])
            others = [_unit_summary(u) | {"sub_units": u.get("children", 0)}
                      for u in found[1:CANDIDATES + 1]]
            return runs.unit_area(unit, name=name), unit, others
    area = await runs.place_area(writer or state, name, network=writer is not None)
    if area is None:
        raise readonly.Refused(f"{name!r}: no such place found"
                               + ("" if writer is not None else " (not looked up yet)"))
    return area, None, others


async def areas(state, place, *, writer=None) -> dict:
    """What an area is: its unit (name, admin level, km², box, refs such
    as ref:siruta, parent), its sub-units (how many by level, the first
    SUB_UNITS_NAMED names), the circle a listings search would take, and
    whether the boundaries are loaded; other units the name may mean."""
    area, unit, others = await resolve(state, place, writer=writer)
    out = {"place": str(place), "boundaries": await _boundaries(state),
           "area": area.as_dict(), "circle": {"lat": round(area.lat, 5),
                                              "lng": round(area.lng, 5),
                                              "radius_km": area.radius_km},
           "shape_from": {"boundaries": "the unit's outline",
                          "nominatim": "Nominatim's point and box (no outline)",
                          "config": "a circle in icp.geo_coordinates"}.get(area.source,
                                                                           area.source)}
    if unit is not None:
        children = unit.get("children") or []
        out["unit"] = _unit_summary(unit)
        out["sub_units"] = {"count": sum((unit.get("children_by_level") or {}).values()),
                            "by_level": unit.get("children_by_level") or {},
                            "names": [{"id": c["id"], "name": c["name"], "level": c["level"],
                                       "area_km2": c.get("area_km2")}
                                      for c in children[:SUB_UNITS_NAMED]],
                            "more": max(0, len(children) - SUB_UNITS_NAMED)}
    elif not out["boundaries"]["loaded"]:
        out["note"] = ("no outline or sub-units: the boundaries aren't loaded (the user loads "
                       "them on the Surse page, or harvey boundaries load); the box is "
                       "Nominatim's")
    if others:
        out["also_named"] = others
    return out


def _queries(area, terms: list[str], provider_key: str, config):
    from harvey.collectors.discover import DiscoveryQuery

    language = getattr(getattr(config, "icp", None), "search_language", "") or "en"
    return [DiscoveryQuery(term=t, location=area.name, coordinate=area.coordinate,
                           area=area.as_dict(), language=language) for t in terms]


async def plan_cells(state, place, sources, terms, *, writer=None) -> dict:
    """Per search source, how it would cover the area and what that would
    cost: a listings circle (one search per phrase, results outside the
    area dropped), a map grid (its first cells, their size and zoom; a
    full cell splits in four as the run goes, within its cap), or a point
    (a ranked web search). Nothing is queued, spent or asked of a source."""
    from harvey import geo
    from harvey.collectors.discover import PROVIDERS, area_shape, is_discovery_source
    from harvey.modules.job_kinds import _config

    keys = [str(k) for k in (sources if isinstance(sources, list) else [sources]) if k]
    if not keys:
        raise readonly.Refused("name the sources (estimate's discover lists them)")
    phrases = [str(t).strip() for t in (terms if isinstance(terms, list) else [terms] if terms
                                        else []) if str(t).strip()][:50]
    if not phrases:
        raise readonly.Refused("name the search phrases (terms() suggests them)")
    area, unit, _ = await resolve(state, place, writer=writer)
    if unit is not None and unit.get("rings"):
        area.rings = unit["rings"]
    config = _config()
    out = []
    for key in keys:
        if key not in PROVIDERS or not is_discovery_source(key):
            raise readonly.Refused(f"{key} isn't a search source")
        provider = PROVIDERS[key]
        shape, most = area_shape(key)
        qs = _queries(area, phrases, key, config)
        part = {"source": key, "label": provider.label, "shape": shape or "none",
                "max_results": most, "searches": len(qs),
                "estimated_cost_usd": round(provider.estimate(qs), 4),
                "free": provider.estimate(qs) == 0}
        if shape == "grid":
            cells = geo.start_cells(area)
            # (Priced over the outline's cells, as the run starts them.)
            per_cell = provider.estimate([_queries(area, phrases[:1], key, config)[0]]) / max(
                1, len(geo.start_cells(geo.Area.from_dict(area.as_dict()))))
            part["estimated_cost_usd"] = round(per_cell * len(cells) * len(qs), 4)
            part["free"] = per_cell == 0
            part["cells_per_phrase"] = len(cells)
            part["cell_side_km"] = round(geo.cell_side_km(cells[0]), 1) if cells else None
            part["zoom"] = geo.zoom_for_radius(geo.cell_radius_km(cells[0])) if cells else None
            part["cells"] = [{"lat": round(geo.cell_centre(c)[0], 5),
                              "lng": round(geo.cell_centre(c)[1], 5),
                              "radius_km": geo.cell_radius_km(c)} for c in cells[:CELLS_SHOWN]]
            part["note"] = ("a cell that comes back full is split into four as the run goes "
                            "(more searches, within the run's cap); a cell whose page is mostly "
                            "places already found stops there")
        elif shape == "circle":
            part["circle"] = {"lat": round(area.lat, 5), "lng": round(area.lng, 5),
                              "radius_km": area.radius_km}
            part["note"] = ("one circle per phrase covers the area; what comes back outside "
                            + ("the outline" if area.rings else "the box") + " is dropped")
        elif shape == "point":
            part["note"] = "ranks from the area's point; the radius means nothing to it"
        else:
            part["note"] = "a web search: the place rides in the phrase"
        out.append(part)
    return {"area": area.as_dict(), "terms": phrases, "sources": out,
            "estimated_cost_usd": round(sum(p["estimated_cost_usd"] for p in out), 4),
            "note": "estimates: the run's cap is what is enforced"}


async def terms(state, query: str) -> dict:
    """Candidate search phrases for an activity (class codes or words):
    the market's classification read backwards (Romania: CAEN, from
    harvey/caen.py and reference/caen_cuvinte.csv) and the settings' own
    phrases (icp.industries, icp.keyword_variants)."""
    from harvey.modules import switches
    from harvey.modules.job_kinds import _config

    text = str(query or "").strip()
    if not text:
        raise readonly.Refused("give class codes (8623) or words (dentist)")
    enabled = await switches.enabled_ids(state)
    out: dict = {"query": text, "classes": [], "phrases": []}
    if "market_ro" in enabled:
        from harvey import caen

        found = caen.phrases(text)
        out.update(classes=found["classes"], phrases=found["phrases"], more=found["more"],
                   classification="CAEN (Romania)")
    else:
        out["classification"] = "none: no market pack with an activity classification is on"
    config = _config()
    icp = getattr(config, "icp", None)
    configured = []
    for industry in list(getattr(icp, "industries", []) or []):
        variants = (getattr(icp, "keyword_variants", {}) or {}).get(industry, [])
        configured += [industry, *variants]
    out["configured"] = list(dict.fromkeys(p for p in configured if p))[:30]
    out["language"] = getattr(icp, "search_language", "") or "en"
    out["categories"] = []
    out["note"] = ("candidates: trim them to the few people type, try them, and keep what "
                   "phrase_yield() shows found new firms cheaply. No Google category is known "
                   "for these classes, so a listings search matches the phrase in the title.")
    return out


def _fold(text: str) -> str:
    from harvey.modules.original.boundaries.store import fold

    return fold(text)


def phrase_yield(state_or_db, days: int = YIELD_DAYS, area: str = "", source: str = "") -> dict:
    """Per search phrase and source over the last ``days``: searches,
    places found, new firms (not on file before), what it cost, and new
    firms per dollar, from the discovery jobs' own reports. ``area``
    keeps the searches whose place matches it (folded, either way);
    ``source`` one source. Counts only."""
    from harvey.modules import switches

    db = getattr(state_or_db, "db_path", state_or_db)
    try:
        days = max(1, min(int(days or YIELD_DAYS), 3650))
    except (TypeError, ValueError):
        raise readonly.Refused("days is a whole number") from None
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - days * 86400))
    lanes = sorted({m.id for m in switches.manifests() for cap in m.capabilities
                    if cap.get("provider") and cap.get("op") == "query"})
    want_area = _fold(area)
    rows: dict[tuple, dict] = {}
    con, _ = readonly.connect(db)
    try:
        for lane in lanes:
            for status, params, result, progress, cost in con.execute(
                    queries.sql("overlay.agent.discover_jobs"), (lane, since)):
                try:
                    p = json.loads(params or "{}")
                    report = json.loads(result or "{}") if status == "done" else \
                        (json.loads(progress or "{}").get("report") or {})
                except ValueError:
                    continue
                q = p.get("query") or {}
                key = str(p.get("provider") or lane)
                if source and key != source:
                    continue
                place = str(q.get("location") or (q.get("area") or {}).get("name") or "")
                # (Whole words either way: "Alba" is not "Albac".)
                folded = f" {_fold(place)} "
                if want_area and not (folded.strip() and (f" {want_area} " in folded
                                                          or folded in f" {want_area} ")):
                    continue
                term = str(q.get("term") or "")
                row = rows.setdefault((term, key), {"phrase": term, "source": key, "places": set(),
                                                    "searches": 0, "found": 0, "new_firms": 0,
                                                    "cost_usd": 0.0, "unfinished": 0})
                row["searches"] += 1
                row["places"].add(place)
                row["found"] += int(report.get("found") or 0)
                row["new_firms"] += int(report.get("new_companies") or 0)
                row["cost_usd"] += float(cost or 0.0)
                row["unfinished"] += status != "done"
    finally:
        con.close()
    out = []
    for row in rows.values():
        row["places"] = len(row["places"])
        row["cost_usd"] = round(row["cost_usd"], 4)
        row["new_per_usd"] = (round(row["new_firms"] / row["cost_usd"], 1)
                              if row["cost_usd"] > 0 else None)
        out.append(row)
    out.sort(key=lambda r: (r["cost_usd"] > 0, -(r["new_per_usd"] or 0), -r["new_firms"]))
    for r in out:
        if r["cost_usd"] == 0:
            r["new_per_usd"] = "free"
    return {"days": days, "area": area, "source": source, "phrases": out[:YIELD_ROWS],
            "more": max(0, len(out) - YIELD_ROWS),
            "note": "new_firms: firms not on file before that search; found counts every place "
                    "it returned. A search not finished (stopped, failed) counts what it did."}
