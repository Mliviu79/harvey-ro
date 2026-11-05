"""The boundaries module's capabilities. The code they run is store.py (the
local database, data/boundaries.db) and source.py (Geofabrik's extracts).

``boundaries_sync`` downloads the extract of each region when none is on
disk (or ``extra["force"]``: a fresh one) and loads the boundaries;
``find_area`` names the units a place may mean; ``area_unit`` gives one
unit with its outline and its sub-units. The regions are the module's
``regions`` setting, else those the market packs that are on name
(``boundaries`` in a market manifest)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harvey.modules.api import Capability, Draft
from harvey.modules.original.boundaries import source, store


def _paths(ctx) -> tuple[Path, Path]:
    s = ctx.settings or {}
    return (Path(s.get("path") or store.BOUNDARIES_PATH),
            Path(s.get("downloads") or source.DOWNLOADS))


def regions(ctx) -> list[str]:
    """The extracts to load: the setting's, else the market packs' that
    are on."""
    from harvey.modules.loader import load_manifests

    wanted = [r for r in (ctx.settings or {}).get("regions") or [] if str(r).strip()]
    if not wanted:
        for m in load_manifests():
            if m.kind == "market" and m.boundaries and (ctx.enabled is None
                                                        or m.id in ctx.enabled):
                wanted += [r for r in m.boundaries if r not in wanted]
    return [source.check_region(str(r)) for r in wanted]


async def _sync(inp, ctx) -> Draft:
    path, root = _paths(ctx)
    progress = inp.extra.get("progress")
    force = bool(inp.extra.get("force"))
    problem = store.library_problem()
    if problem:
        raise RuntimeError(problem)
    wanted = regions(ctx)
    if not wanted:
        raise RuntimeError("no region to load: no market pack that is on names one; set the "
                           "boundaries module's regions (e.g. europe/romania)")
    loaded = store.status(path)
    have = {r: source.downloaded(r, root) for r in wanted}
    if (loaded and not force and all(have.values())
            and (loaded.get("regions") or "").split() == wanted):
        return Draft(values={"boundaries": loaded, "downloaded": [], "regions": wanted,
                             "unchanged": True})
    fetched = []
    for region in wanted:
        if have[region] is None or force:
            if ctx.http is None:
                raise RuntimeError(f"no connection given, and {region} isn't downloaded")
            have[region] = await source.fetch(ctx.http, region, root, progress=progress)
            fetched.append(have[region].name)
    meta = await asyncio.to_thread(store.build, [have[r] for r in wanted], path,
                                   regions=wanted, progress=progress)
    return Draft(values={"boundaries": meta, "downloaded": fetched, "regions": wanted})


def _open(ctx):
    path, _ = _paths(ctx)
    try:
        return store.connect(path)
    except FileNotFoundError:
        return None


async def _find(inp, ctx) -> Draft:
    """``values["units"]``: the units the place may mean, likeliest first
    (no outlines); ``values["loaded"]``: whether boundaries are loaded."""
    con = _open(ctx)
    if con is None:
        return Draft(values={"loaded": False, "units": []})
    try:
        units = store.find(con, inp.locality or inp.region)
    finally:
        con.close()
    return Draft(values={"loaded": True, "units": units})


async def _unit(inp, ctx) -> Draft:
    """``values["unit"]``: one unit with its outline, parent and children
    (None when there is no such unit or nothing is loaded)."""
    con = _open(ctx)
    if con is None:
        return Draft(values={"loaded": False, "unit": None})
    try:
        raw = str(inp.ids.get("unit") or "")
        found = store.unit(con, int(raw)) if raw.isdigit() else None
    finally:
        con.close()
    return Draft(values={"loaded": True, "unit": found})


CAPABILITIES = {
    "boundaries_sync": Capability("boundaries", "boundaries_sync", _sync),
    "find_area": Capability("boundaries", "find_area", _find),
    "area_unit": Capability("boundaries", "area_unit", _unit),
}
