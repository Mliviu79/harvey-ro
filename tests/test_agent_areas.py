"""The agent's area and phrase tools (harvey/agent/areas.py) through its
MCP server: areas(), plan_cells(), terms(), phrase_yield(), and discover()
on an area. No model is called, nothing runs and no request leaves: the
place's point and box are in the geocode cache, as a run leaves them. All
places and data are invented."""

from __future__ import annotations

import json

from harvey.state import StateManager
from tests.test_agent_actions import (_approve, _offline, _one, _propose, _q,  # noqa: F401
                                      _run, db)

# The place's point and box, as Nominatim's answer is cached (in the open sea).
LAT, LNG = 0.30, -25.00
BOX = (0.20, -25.12, 0.40, -24.88)


def _cache(db, place="Brașov"):
    state = StateManager(str(db))
    key = place.lower()
    _run(state.set_setting(f"geo:{key}", f"{LAT:.4f},{LNG:.4f}"))
    _run(state.set_setting(f"geobox:{key}", ",".join(str(v) for v in BOX)))


def test_areas_says_what_an_area_is_from_the_cache(db):
    _cache(db)
    error, out = _one(db, "areas", {"place": "Brașov"})
    assert not error, out
    assert out["area"]["source"] == "nominatim" and out["area"]["bbox"] == list(BOX)
    assert 15 < out["circle"]["radius_km"] < 20
    assert out["boundaries"]["loaded"] is False and "boundaries aren't loaded" in out["note"]
    # A place nobody looked up: Nominatim is asked (here the request fails).
    error, text = _one(db, "areas", {"place": "Atlantis Inventat"})
    assert error and "no such place" in text
    error, text = _one(db, "areas", {"place": "7"})
    assert error and "boundaries module" in text


def test_plan_cells_shows_each_sources_shape_and_price(db):
    _cache(db)
    error, out = _one(db, "plan_cells", {"area": "Brașov", "terms": ["stomatolog", "dentist"],
                                         "sources": ["dataforseo_listings", "scavio_maps",
                                                     "dataforseo_serp", "serper"]})
    assert not error, out
    by = {p["source"]: p for p in out["sources"]}
    assert by["dataforseo_listings"]["shape"] == "circle"
    assert by["dataforseo_listings"]["circle"]["radius_km"] == out["area"]["radius_km"]
    grid = by["scavio_maps"]
    assert grid["shape"] == "grid" and grid["max_results"] == 120
    assert grid["estimated_cost_usd"] == round(0.06 * grid["cells_per_phrase"] * 2, 4)
    assert grid["zoom"] and len(grid["cells"]) == grid["cells_per_phrase"]
    assert by["dataforseo_serp"]["shape"] == "point" and by["serper"]["shape"] == "none"
    error, text = _one(db, "plan_cells", {"area": "Brașov", "terms": [], "sources": ["osm"]})
    assert error and "phrases" in text


def test_terms_and_phrase_yield_are_counts(db):
    error, out = _one(db, "terms", {"query": "8623"})
    assert not error, out
    assert out["classification"] == "CAEN (Romania)"
    assert {c["code"] for c in out["classes"]} == {"8623"}
    assert any(p["phrase"] == "stomatolog" for p in out["phrases"])
    assert "dentist" in out["configured"]
    error, out = _one(db, "phrase_yield", {})
    assert not error and out["phrases"] == [] and out["days"] == 90


def test_discover_takes_an_area_and_queues_its_shape(db):
    _cache(db)
    plan_id = _propose(db, budget=1.0)
    _approve(db, plan_id)
    error, out = _one(db, "discover", {"plan": plan_id, "sources": ["scavio_maps"],
                                       "phrases": ["stomatolog"], "areas": ["Brașov"],
                                       "max_spend": 0.5, "find_websites": False})
    assert not error, out
    assert out["estimate"]["shapes"]["scavio_maps"]["shape"] == "grid"
    cells = out["estimate"]["shapes"]["scavio_maps"]["cells"]
    assert cells >= 1 and "split into four" in out["estimate"]["cells_note"]
    (params, est), = _q(db, "SELECT params_json, est_cost FROM jobs WHERE batch_id = ?",
                        out["run"])
    query = json.loads(params)["query"]
    assert query["location"] == "Brașov" and query["area"]["bbox"] == list(BOX)
    assert est == round(0.06 * cells, 6) or abs(est - 0.06 * cells) < 1e-9
    error, text = _one(db, "discover", {"plan": plan_id, "sources": ["scavio_maps"],
                                        "phrases": ["stomatolog"], "areas": ["unit:12"]})
    assert error and "boundaries module" in text
