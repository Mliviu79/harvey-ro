"""Areas a search covers (harvey/geo.py), the boundaries module
(harvey/modules/original/boundaries), the tiled map search and the area
filter (harvey/collectors/discover.py), and the agent's area and phrase
tools (harvey/agent/areas.py). No network: every request goes to a mock
transport. All places are invented, at made-up coordinates in the open
sea; no real place's coordinates are used."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from harvey import caen, geo
from harvey.collectors import discover as D
from harvey.modules import runs as R
from harvey.state import StateManager

# An invented country in the open sea: a 0.4 x 0.4 degree square, a county
# filling its west half, two towns in the county.
LAT0, LNG0 = 0.30, -25.00


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        yield sm


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── geo ──

def test_the_radius_comes_from_the_box_not_a_fixed_40_km():
    box = (LAT0 - 0.1, LNG0 - 0.1, LAT0 + 0.1, LNG0 + 0.1)
    r = geo.radius_from_bbox(LAT0, LNG0, box)
    # The corner of a 0.2-degree box near the equator: about 15.7 km.
    assert 15 < r < 16.5
    area = geo.Area.from_bbox(LAT0, LNG0, box, name="Portu Inventat")
    assert area.coordinate == f"{LAT0:.4f},{LNG0:.4f},{r:g}"
    # A point with no box gets the default; a tiny box the floor.
    assert geo.radius_from_bbox(LAT0, LNG0, (LAT0, LNG0, LAT0, LNG0)) == geo.MIN_RADIUS_KM


def test_the_zoom_comes_from_the_radius():
    assert geo.zoom_for_radius(40) == 10
    assert geo.zoom_for_radius(5) == 13
    assert geo.zoom_for_radius(0.2, 3, 21) == 16
    # DataForSEO Maps: a cell's radius gives its zoom; no radius keeps 12z.
    # A point a gazetteer gave without a box keeps the city zoom.
    maps = D.DataForSEOMaps()
    assert maps.zoom(D.DiscoveryQuery(term="x", coordinate="0.3,-25,5",
                                      area=dict(D.CELL_AREA))) == "13z"
    assert maps.zoom(D.DiscoveryQuery(term="x", coordinate="0.3,-25,40",
                                      area={"source": "point"})) == "12z"
    assert maps.zoom(D.DiscoveryQuery(term="x", coordinate="0.3,-25")) == "12z"
    assert D.ScavioMaps.map_center("0.3,-25,5") == "@0.3,-25,13z"


def test_area_contains_by_outline_then_box():
    ring = [(LNG0, LAT0), (LNG0 + 0.1, LAT0), (LNG0 + 0.1, LAT0 + 0.1), (LNG0, LAT0 + 0.1)]
    area = geo.Area(lat=LAT0 + 0.05, lng=LNG0 + 0.05, bbox=geo.bbox_of([ring]), rings=[ring])
    assert area.contains(LAT0 + 0.05, LNG0 + 0.05) is True
    assert area.contains(LAT0 + 0.2, LNG0 + 0.05) is False
    assert area.contains(None, None) is None
    # About 11.1 km a side.
    assert 120 < geo.area_km2([ring]) < 126


def test_start_cells_and_their_splits():
    county = (LAT0 - 0.4, LNG0 - 0.4, LAT0 + 0.4, LNG0 + 0.4)     # ~90 km a side
    area = geo.Area.from_bbox(LAT0, LNG0, county)
    cells = geo.start_cells(area)
    assert len(cells) == geo.START_CELLS ** 2
    town = geo.Area.from_bbox(LAT0, LNG0, (LAT0 - 0.04, LNG0 - 0.04, LAT0 + 0.04, LNG0 + 0.04))
    assert len(geo.start_cells(town)) == 1
    quarters = geo.split(town, geo.start_cells(town)[0])
    assert len(quarters) == 4
    assert all(geo.cell_side_km(q) < geo.cell_side_km(geo.start_cells(town)[0]) for q in quarters)
    assert geo.mostly_seen(["a", "b", "c", "d", "e"], {"a", "b", "c", "d"})
    assert not geo.mostly_seen(["a", "b", "c", "d", "e"], {"a"})
    assert not geo.mostly_seen(["a", "b"], {"a", "b"})      # too few to say


# ── Nominatim's box, cached ──

@pytest.mark.asyncio
async def test_nominatim_box_gives_the_radius_and_is_cached(state):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[{"lat": str(LAT0), "lon": str(LNG0), "boundingbox": [
            str(LAT0 - 0.1), str(LAT0 + 0.1), str(LNG0 - 0.1), str(LNG0 + 0.1)]}])

    async with _client(handler) as client:
        first = await R.geocode_city(state, "Portu Inventat", client=client)
        again = await R.geocode_city(state, "portu inventat", client=client)
    assert len(calls) == 1
    lat, lng, radius = first.split(",")
    assert float(lat) == LAT0 and 15 < float(radius) < 16.5
    assert again == first
    area = await R.place_area(state, "Portu Inventat", network=False)
    assert area.bbox == pytest.approx((LAT0 - 0.1, LNG0 - 0.1, LAT0 + 0.1, LNG0 + 0.1))
    # The point stays where the email module's send window reads it.
    assert await state.get_setting("geo:portu inventat") == f"{LAT0:.4f},{LNG0:.4f}"


@pytest.mark.asyncio
async def test_a_point_cached_before_boxes_were_kept_is_asked_once_more(state):
    await state.set_setting("geo:satu vechi", f"{LAT0},{LNG0}")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[{"lat": str(LAT0), "lon": str(LNG0)}])

    async with _client(handler) as client:
        assert await R.geocode_city(state, "Satu Vechi", client=client) \
            == f"{LAT0:.4f},{LNG0:.4f},40"
        assert await R.geocode_city(state, "Satu Vechi", client=client) \
            == f"{LAT0:.4f},{LNG0:.4f},40"
    assert len(calls) == 1
    assert await state.get_setting("geobox:satu vechi") == "-"
    # A point with no box has no shape: a capped map search isn't tiled on it.
    q = D.DiscoveryQuery(term="dentist", location="Satu Vechi")
    await R.resolve_coordinates(state, [q])
    assert q.area["source"] == "point" and "bbox" not in q.area
    assert D.area_cells("scavio_maps", q) == 1


@pytest.mark.asyncio
async def test_configured_coordinates_become_the_area(state):
    q = D.DiscoveryQuery(term="dentist", location="Portu Inventat", coordinate=f"{LAT0},{LNG0},7")
    await R.resolve_coordinates(state, [q])
    assert q.coordinate == f"{LAT0},{LNG0},7"
    assert q.area["radius_km"] == 7 and len(q.area["bbox"]) == 4


# ── One listings circle, results outside the area dropped ──

@pytest.mark.asyncio
async def test_listings_take_one_circle_and_drop_what_is_outside():
    box = (LAT0 - 0.05, LNG0 - 0.05, LAT0 + 0.05, LNG0 + 0.05)
    area = geo.Area.from_bbox(LAT0, LNG0, box, name="Portu Inventat")
    q = D.DiscoveryQuery(term="dentist", location="Portu Inventat", coordinate=area.coordinate,
                         area=area.as_dict())
    posted = []

    def handler(request):
        posted.append(json.loads(request.content)[0])
        items = [{"title": f"Clinica {i}", "cid": str(i), "url": f"https://c{i}.example",
                  "domain": f"c{i}.example", "latitude": lat, "longitude": LNG0}
                 for i, lat in enumerate((LAT0, LAT0 + 0.01, LAT0 + 0.3))]
        return httpx.Response(200, json={"cost": 0.013, "tasks": [{"status_code": 20000,
                                                                     "result": [{"total_count": 3,
                                                                                 "items": items}]}]})

    async with _client(handler) as client:
        draft = await D.search_draft("dataforseo_listings", q, client,
                                     {"dataforseo_login": "l", "dataforseo_password": "p"})
    assert draft.failed is None, draft.failed
    assert len(posted) == 1
    assert posted[0]["location_coordinate"] == area.coordinate
    assert draft.counts["outside_area"] == 1
    assert sorted(r.domain for r in draft.records) == ["c0.example", "c1.example"]
    assert D.area_shape("dataforseo_listings") == ("circle", None)
    assert D.area_cells("dataforseo_listings", q) == 1


# ── The tiled map search ──

class FakeMaps(D.DiscoveryProvider):
    """A map search serving at most CAP places within its circle, $0.01 a
    search: the places are fixed points."""

    key = "fake_maps"
    label = "Fake Maps"
    kind = "listings"
    needs_coordinates = True
    COST = 0.01

    def __init__(self, points, cap, domain=""):
        self.points = points
        self.cap = cap
        # Every place's website (a chain's branches share one), when given.
        self.domain = domain
        self.calls: list[str] = []
        self.seen_at: list[set] = []

    def configured(self, env):
        return True

    def estimate(self, queries):
        return sum(self.COST * D.area_cells(self.key, q) for q in queries)

    def first_cost(self, query):
        return self.COST

    async def fetch(self, client, env, query):
        if self.COST > self.remaining_budget + 1e-9:
            raise D.ProviderExhausted("budget reached")
        self.calls.append(query.coordinate)
        self.seen_at.append(set(self.seen or ()))
        self.last_cost = self.COST
        lat, lng, radius = geo.parse_coordinate(query.coordinate)
        # (A map's view reaches a little past the circle that holds a cell.)
        near = [(i, la, lo) for i, (la, lo) in enumerate(self.points)
                if geo.distance_km(lat, lng, la, lo) <= radius * 1.5]
        near.sort(key=lambda p: geo.distance_km(lat, lng, p[1], p[2]))
        await self.page_done(len(near[:self.cap]))
        return [D.Business(name=f"Loc {i}", external_id=f"fake:{i}", lat=la, lng=lo,
                           domain=self.domain, provider=self.key)
                for i, la, lo in near[:self.cap]]


@pytest.fixture
def maps(claim_provider, monkeypatch):
    def make(points, cap, domain=""):
        provider = claim_provider(FakeMaps(points, cap, domain))
        real = D.area_shape
        monkeypatch.setattr(D, "area_shape",
                            lambda key: ("grid", cap) if key == FakeMaps.key else real(key))
        return provider
    return make


def _town(side_deg=0.09):
    h = side_deg / 2
    return geo.Area.from_bbox(LAT0, LNG0, (LAT0 - h, LNG0 - h, LAT0 + h, LNG0 + h), name="Valea")


@pytest.mark.asyncio
async def test_a_full_cell_is_split_and_every_place_is_found_within_the_cap(maps):
    # Eight places packed in the south-west quarter, one in the north-east.
    pts = [(LAT0 - 0.035 + 0.01 * (i // 4), LNG0 - 0.04 + 0.01 * (i % 4)) for i in range(8)]
    pts.append((LAT0 + 0.03, LNG0 + 0.03))
    provider = maps(pts, cap=4)
    area = _town()
    q = D.DiscoveryQuery(term="dentist", location="Valea", coordinate=area.coordinate,
                         area=area.as_dict())
    assert D.area_cells(FakeMaps.key, q) == 1 and provider.estimate([q]) == pytest.approx(0.01)
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=1.0)
    assert draft.failed is None
    assert sorted(r.external_id for r in draft.records) == sorted(f"fake:{i}" for i in range(9))
    assert draft.counts["split"] >= 1 and draft.counts["cells"] == len(provider.calls) > 1
    assert draft.cost == pytest.approx(0.01 * len(provider.calls))
    # Each cell searched from its own centre, at a smaller radius than the town.
    radii = [geo.parse_coordinate(c)[2] for c in provider.calls]
    assert radii[0] > max(radii[1:])

    # Under a smaller cap it stops between cells, with the cells left as its cursor.
    provider.calls.clear()
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=0.025)
    assert draft.stop == "budget reached" and len(provider.calls) == 2
    assert draft.cost <= 0.025 + 1e-9
    left = draft.values["cursor"]["cells"]
    assert left and draft.values["unfetched_cost"] == pytest.approx(0.01 * len(left))
    # Carried on from there, it searches only the cells left.
    provider.calls.clear()
    more = await D.search_draft(FakeMaps.key, q, None, {}, remaining=1.0,
                                cursor=draft.values["cursor"])
    assert more.failed is None and len(provider.calls) >= len(left)


@pytest.mark.asyncio
async def test_a_cell_of_places_already_found_stops_and_is_not_split(maps):
    # Five places around the centre, in every cell's circle.
    pts = [(LAT0 + 0.002 * i, LNG0) for i in range(5)]
    provider = maps(pts, cap=5)
    area = _town()
    q = D.DiscoveryQuery(term="dentist", location="Valea", coordinate=area.coordinate,
                         area=area.as_dict())
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=1.0)
    assert len(draft.records) == 5
    # The first cell came back full and split; its quarters gave only known
    # places, so none of them split again.
    assert draft.counts["split"] == 1
    assert draft.counts["seen_stops"] == len(provider.calls) - 1 == 4


@pytest.mark.asyncio
async def test_a_limited_or_shallow_search_is_never_split(maps):
    pts = [(LAT0 + 0.001 * i, LNG0) for i in range(6)]
    provider = maps(pts, cap=4)
    area = _town()
    q = D.DiscoveryQuery(term="dentist", location="Valea", coordinate=area.coordinate,
                         area=area.as_dict(), depth=2)
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=1.0)
    assert draft.counts["split"] == 0 and len(provider.calls) == 1


def test_the_capped_map_sources_declare_their_cap():
    assert D.area_shape("scavio_maps") == ("grid", D.ScavioMaps.MAX_PAGES * D.ScavioMaps.PAGE_SIZE)
    assert D.area_shape("dataforseo_maps") == ("grid", D.DataForSEOMaps.MAX_DEPTH)
    assert D.area_shape("dataforseo_serp")[0] == "point"


def test_a_tiled_estimate_counts_the_first_cells():
    county = geo.Area.from_bbox(LAT0, LNG0, (LAT0 - 0.4, LNG0 - 0.4, LAT0 + 0.4, LNG0 + 0.4))
    q = D.DiscoveryQuery(term="dentist", location="Ținutul", coordinate=county.coordinate,
                         area=county.as_dict())
    cells = D.area_cells("scavio_maps", q)
    assert cells == geo.START_CELLS ** 2
    assert D.ScavioMaps().estimate([q]) == pytest.approx(0.06 * cells)
    assert D.DataForSEOMaps().estimate([q]) == pytest.approx(0.014 * cells)
    assert D.DataForSEOListings().estimate([q]) == D.DataForSEOListings().estimate(
        [D.DiscoveryQuery(term="dentist")])


# ── Terms ──

def test_terms_read_the_activity_words_backwards():
    dental = caen.phrases("8623")
    assert {c["code"] for c in dental["classes"]} == {"8623"}
    got = {p["phrase"]: p for p in dental["phrases"]}
    assert "stomatolog" in got and got["stomatolog"]["from"] == "synonym"
    assert "cabinet stomatologic" in got
    assert any(p["from"] == "class name" and p["lang"] == "en" for p in dental["phrases"])
    bakery = caen.phrases("brutărie")
    assert any(p["phrase"] == "brutărie" and p["from"] == "everyday" and p["lang"] == "ro"
               for p in bakery["phrases"])
    assert caen.phrases("")["phrases"] == []


# ── phrase_yield ──


@pytest.mark.asyncio
async def test_the_cap_between_cells_counts_what_was_spent_once(maps):
    # Places everywhere, four a cell at most: every cell splits, so there
    # is always another cell to pay for.
    pts = [(LAT0 - 0.04 + 0.01 * (i // 9), LNG0 - 0.04 + 0.01 * (i % 9)) for i in range(81)]
    provider = maps(pts, cap=4)
    area = _town()
    q = D.DiscoveryQuery(term="dentist", location="Valea", coordinate=area.coordinate,
                         area=area.as_dict())
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=0.05)
    # $0.05 buys five cells of $0.01, not three.
    assert draft.stop == "budget reached"
    assert len(provider.calls) == 5 and draft.cost == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_a_chains_branches_in_different_cells_are_all_kept(maps):
    # Five branches of one chain (one website), four a cell at most.
    pts = [(LAT0 - 0.035 + 0.017 * i, LNG0 - 0.035 + 0.017 * i) for i in range(5)]
    provider = maps(pts, cap=4, domain="lant.example")
    area = _town()
    q = D.DiscoveryQuery(term="farmacie", location="Valea", coordinate=area.coordinate,
                         area=area.as_dict())
    draft = await D.search_draft(FakeMaps.key, q, None, {}, remaining=1.0)
    assert draft.failed is None
    assert sorted(r.external_id for r in draft.records) == sorted(f"fake:{i}" for i in range(5))
    # What a cell is told it has seen is the places' listing ids (what a
    # page's own check compares), not their shared website.
    assert any(seen and all(i.startswith("fake:") for i in seen) for seen in provider.seen_at)


def test_a_cells_circle_holds_all_its_corners_far_from_the_equator():
    cell = (69.0, 18.0, 70.0, 20.0)
    lat, lng = geo.cell_centre(cell)
    r = geo.cell_radius_km(cell)
    for la in (69.0, 70.0):
        for lo in (18.0, 20.0):
            assert geo.distance_km(lat, lng, la, lo) <= r + 0.01


def test_a_cell_an_outline_edge_crosses_is_kept():
    # A thin band crossing the cell: no vertex of it in the cell, no corner
    # or centre of the cell in it.
    band = [(-1.0, 0.45), (2.0, 0.45), (2.0, 0.46), (-1.0, 0.46), (-1.0, 0.45)]
    area = geo.Area(name="Banda", lat=0.455, lng=0.5, bbox=(0.45, -1.0, 0.46, 2.0),
                    rings=[band])
    assert geo._touches(area, (0.0, 0.0, 1.0, 1.0))
    assert not geo._touches(area, (0.6, 0.0, 1.0, 1.0))


@pytest.mark.asyncio
async def test_every_nominatim_request_takes_its_turn_on_the_lane(state, monkeypatch):
    from harvey.modules import lanes

    waited = []

    async def wait(lane):
        waited.append(lane)
    monkeypatch.setattr(lanes, "wait", wait)

    def handler(request):
        return httpx.Response(200, json=[{"lat": str(LAT0), "lon": str(LNG0)}])

    async with _client(handler) as client:
        await R.place_area(state, "Portu Inventat", client)
        await R.place_area(state, "Satu Inventat", client)
    assert waited == ["nominatim", "nominatim"]


@pytest.mark.asyncio
async def test_an_area_inside_another_is_covered_by_the_outer_ones_search(state):
    from types import SimpleNamespace

    from harvey import launch

    boxes = {"ținut inventat": (0.0, -25.4, 0.4, -25.0),
             "valea inventata": (0.1, -25.3, 0.2, -25.2),
             "dealu departe": (5.0, -20.1, 5.1, -20.0)}
    for place, (s, w, n, e) in boxes.items():
        await state.set_setting(f"geo:{place}", f"{(s + n) / 2},{(w + e) / 2}")
        await state.set_setting(f"geobox:{place}", f"{s},{w},{n},{e}")
    body = {"areas": ["Ținut Inventat", "Valea Inventata", "Dealu Departe"],
            "terms": ["dentist"]}
    await launch.discovery_areas(state, body, network=False)
    config = SimpleNamespace(icp=SimpleNamespace(geography=[], geo_coordinates={},
                                                 search_language="ro"))
    listings = launch.discovery_queries(body, config, "dataforseo_listings")
    grid = launch.discovery_queries(body, config, "scavio_maps")
    ranks = launch.discovery_queries(body, config, "dataforseo_serp")
    # One circle (or grid) for the county pays for its town's places once.
    assert sorted(q.location for q in listings) == ["Dealu Departe", "Ținut Inventat"]
    assert sorted(q.location for q in grid) == ["Dealu Departe", "Ținut Inventat"]
    # A rank search still searches from the town's own point.
    assert len(ranks) == 3


def test_phrase_yield_of_an_area_leaves_out_searches_with_no_place(tmp_path):
    from harvey.agent import areas

    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    con = sqlite3.connect(db)
    _job(con, "osm", "dentist", "Valea", "osm", "done", 3, 3, 0.0, 1)
    _job(con, "osm", "dentist", "", "osm", "done", 9, 9, 0.0, 2)
    _job(con, "osm", "dentist", "Valeaplana", "osm", "done", 4, 4, 0.0, 3)
    con.commit()
    con.close()
    out = areas.phrase_yield(str(db), area="valea")
    assert [(r["searches"], r["found"]) for r in out["phrases"]] == [(1, 3)]

def _job(con, lane, term, place, provider, status, found, new, cost, n):
    report = {"found": found, "new_companies": new}
    con.execute(
        "INSERT INTO jobs (id, kind, lane, params_json, status, result_json, progress_json, "
        "actual_cost, error) VALUES (?, 'discover', ?, ?, ?, ?, ?, ?, 'planted error text')",
        (f"j{n}", lane, json.dumps({"provider": provider, "query": {"term": term,
                                                                     "location": place}}),
         status, json.dumps(report) if status == "done" else "{}",
         json.dumps({"report": report}) if status != "done" else "{}", cost))


def test_phrase_yield_counts_new_firms_per_dollar(tmp_path):
    from harvey.agent import areas

    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    con = sqlite3.connect(db)
    _job(con, "dataforseo", "stomatolog", "Valea", "dataforseo_listings", "done", 40, 30, 0.02, 1)
    _job(con, "dataforseo", "stomatolog", "Dealu", "dataforseo_listings", "done", 10, 2, 0.02, 2)
    _job(con, "dataforseo", "dentist", "Valea", "dataforseo_listings", "cancelled", 5, 1, 0.01, 3)
    _job(con, "osm", "dentist", "Valea", "osm", "done", 3, 3, 0.0, 4)
    con.commit()
    con.close()
    out = areas.phrase_yield(str(db))
    rows = {(r["phrase"], r["source"]): r for r in out["phrases"]}
    paid = rows[("stomatolog", "dataforseo_listings")]
    assert (paid["searches"], paid["places"], paid["found"], paid["new_firms"]) == (2, 2, 50, 32)
    assert paid["cost_usd"] == 0.04 and paid["new_per_usd"] == 800.0
    assert rows[("dentist", "dataforseo_listings")]["unfinished"] == 1
    assert rows[("dentist", "osm")]["new_per_usd"] == "free"
    assert "planted" not in json.dumps(out)
    only = areas.phrase_yield(str(db), area="valea", source="dataforseo_listings")
    assert {(r["phrase"], r["searches"]) for r in only["phrases"]} == {("stomatolog", 1),
                                                                        ("dentist", 1)}


# ── The boundaries module ──

def _fixture_pbf(path: Path) -> None:
    """An invented country (level 2) with a county (level 4) holding two
    towns (level 8), one named like a second county, as an .osm.pbf."""
    osmium = pytest.importorskip("osmium", reason="pyosmium isn't installed in this image "
                                                  "(docker compose build harvey)")
    from osmium.osm.mutable import Node, Relation, Way

    squares = {  # name: (level, south, west, north, east, tags)
        "Țara Exemplu": (2, 0.0, -25.4, 0.4, -25.0, {"ISO3166-1": "XE"}),
        "Ținutul Exemplu": (4, 0.0, -25.4, 0.4, -25.2, {"ref": "TE"}),
        "Valea Verde": (8, 0.05, -25.38, 0.15, -25.28, {"ref:siruta": "90001"}),
        "Dealu Mic": (8, 0.25, -25.38, 0.35, -25.28, {"ref:siruta": "90002"}),
        "Ținutul Valea Verde": (4, 0.0, -25.2, 0.4, -25.0, {"ref": "TV"}),
    }
    w = osmium.SimpleWriter(str(path))
    try:
        nid, ways, rels = 1, [], []
        for i, (name, (level, s, west, n, e, tags)) in enumerate(squares.items(), start=1):
            first = nid
            for lon, lat in ((west, s), (e, s), (e, n), (west, n)):
                w.add_node(Node(id=nid, location=(lon, lat), version=1))
                nid += 1
            ways.append(Way(id=100 + i, nodes=[first, first + 1, first + 2, first + 3, first],
                            version=1))
            rels.append(Relation(id=1000 + i, members=[("w", 100 + i, "outer")], version=1,
                                 tags={"type": "boundary", "boundary": "administrative",
                                       "admin_level": str(level), "name": name, **tags}))
        for way in ways:
            w.add_way(way)
        for rel in rels:
            w.add_relation(rel)
    finally:
        w.close()


def test_the_boundaries_load_keeps_levels_parents_refs_and_outlines(tmp_path):
    from harvey.modules.original.boundaries import store

    pbf = tmp_path / "exemplu-latest.osm.pbf"
    _fixture_pbf(pbf)
    target = tmp_path / "boundaries.db"
    # The full outlines aren't held while reading: each unit is measured on
    # them and kept simplified.
    assert all("rings" not in u and u["simple"] for u in store._read([pbf]))
    meta = store.build([pbf], target, regions=["europe/exemplu"])
    assert meta["units"] == 5 and meta["levels"] == {"2": 1, "4": 2, "8": 2}
    con = store.connect(target)
    try:
        found = store.find(con, "Valea Verde")
        # The town by its exact name first, the county ending with it after.
        assert [u["level"] for u in found] == [8, 4]
        town = store.unit(con, found[0]["id"])
        assert town["refs"] == {"ref:siruta": "90001"}
        assert town["parent_name"] == "Ținutul Exemplu"
        assert 110 < town["area_km2"] < 135
        county = store.unit(con, town["parent"])
        assert sorted(c["name"] for c in county["children"]) == ["Dealu Mic", "Valea Verde"]
        assert county["children_by_level"] == {"8": 2}
        assert store.find(con, "valea verde, tinutul exemplu")[0]["level"] == 8
    finally:
        con.close()
    # The area from the outline: its circle holds it, and a point outside is out.
    area = R.unit_area(town)
    assert area.contains(0.10, -25.33) is True and area.contains(0.30, -25.33) is False
    assert area.radius_km >= geo.radius_from_bbox(area.lat, area.lng, town["bbox"]) - 0.2


@pytest.mark.asyncio
async def test_the_boundaries_sync_downloads_resumes_checks_and_loads(tmp_path, monkeypatch):
    pytest.importorskip("osmium", reason="pyosmium isn't installed in this image")
    from harvey.modules import api
    from harvey.modules.original.boundaries import source

    monkeypatch.setattr(source, "MIN_BYTES", 100)     # the fixture is a few hundred bytes

    fixture = tmp_path / "fixture.osm.pbf"
    _fixture_pbf(fixture)
    body = fixture.read_bytes()
    md5 = hashlib.md5(body).hexdigest()
    root, path = tmp_path / "dl", tmp_path / "boundaries.db"
    asked = []

    def handler(request):
        asked.append((request.url.host, request.url.path, request.headers.get("range")))
        assert request.url.host == source.HOST
        if request.url.path.endswith(".md5"):
            return httpx.Response(200, text=f"{md5}  exemplu-latest.osm.pbf\n")
        start = int((request.headers.get("range") or "bytes=0-")[6:-1] or 0)
        status = 206 if start else 200
        return httpx.Response(status, content=body[start:])

    # A download stopped part-way: its .part carries on with a Range request.
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    part = root / day / "europe_exemplu-latest.osm.pbf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(body[:100])
    ctx = api.Context(http=_client(handler), enabled=frozenset({"boundaries"}),
                      settings={"regions": ["europe/exemplu"], "path": str(path),
                                "downloads": str(root)})
    cap = api.capability("boundaries", "boundaries_sync")
    draft = await cap(api.Input(), ctx)
    assert draft.values["regions"] == ["europe/exemplu"]
    assert draft.values["downloaded"] == ["europe_exemplu-latest.osm.pbf"]
    assert draft.values["boundaries"]["units"] == 5
    assert ("download.geofabrik.de", "/europe/exemplu-latest.osm.pbf", "bytes=100-") in asked
    assert (root / day / "europe_exemplu-latest.osm.pbf").read_bytes() == body
    # Asked again: nothing downloaded, nothing loaded.
    asked.clear()
    again = await cap(api.Input(), ctx)
    assert again.values.get("unchanged") and asked == []
    # The find and unit capabilities read it.
    found = await api.capability("boundaries", "find_area")(api.Input(locality="Dealu Mic"), ctx)
    assert found.values["loaded"] and found.values["units"][0]["refs"] == {"ref:siruta": "90002"}
    await ctx.http.aclose()


@pytest.mark.asyncio
async def test_a_download_that_does_not_match_its_md5_is_dropped(tmp_path):
    from harvey.modules.original.boundaries import source

    def handler(request):
        if request.url.path.endswith(".md5"):
            return httpx.Response(200, text="0" * 32 + "  x\n")
        return httpx.Response(200, content=b"x" * 20_000)

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="MD5"):
            await source.fetch(client, "europe/exemplu", tmp_path)
    assert source.downloaded("europe/exemplu", tmp_path) is None
    with pytest.raises(ValueError):
        source.url("../etc")
    assert not source.trusted("https://example.com/europe/exemplu-latest.osm.pbf")


def test_the_market_pack_names_its_extract_and_the_module_ships_off():
    from harvey.modules.loader import load_manifests

    mans = {m.id: m for m in load_manifests()}
    assert mans["market_ro"].boundaries == ("europe/romania",)
    assert mans["boundaries"].default_enabled is False
    assert "OpenStreetMap" in mans["boundaries"].attribution


def test_without_pyosmium_the_load_says_what_to_do(monkeypatch):
    from harvey.modules.original.boundaries import store

    monkeypatch.setattr(store, "_LIBRARY", [store.MISSING])
    assert "docker compose build harvey" in store.library_problem()


@pytest.mark.modules_on("boundaries")
def test_the_public_data_page_offers_the_boundaries_load(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    from harvey.modules.original.boundaries import source, store

    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")     # queued, never run
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(store, "BOUNDARIES_PATH", tmp_path / "boundaries.db")
    monkeypatch.setattr(source, "DOWNLOADS", tmp_path / "boundaries")
    monkeypatch.setattr(store, "_LIBRARY", [""])
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        b = c.get("/api/public-data").json()["boundaries"]
        assert b["on"] and b["loaded"] is None and b["regions"] == ["europe/romania"]
        assert "320 MB" in b["size_note"] and "OpenStreetMap" in b["attribution"]
        r = c.post("/api/public-data/boundaries/load", json={})
        assert r.status_code == 200 and r.json()["boundaries"]["running"]
        assert c.post("/api/public-data/boundaries/load", json={}).status_code == 409
        monkeypatch.setattr(store, "_LIBRARY", [store.MISSING])
        r = c.post("/api/public-data/boundaries/load", json={})
        assert r.status_code == 409 and "library missing" in r.json()["message"]
