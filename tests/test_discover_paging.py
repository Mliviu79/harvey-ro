"""Discovery takes every result a source has, page by page.

A search for a common business in a big city has thousands of answers, not a
page of 100. These tests drive each paging provider against recorded-shape
responses (fictional example.com businesses only) and check: it pages until
the source is exhausted, it follows DataForSEO's offset_token, the spend cap
and the kill switch stop it between pages and say what was left, the
estimate counts first, and no business is stored twice.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import closing

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig, load_config
from harvey.modules import runs as R
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import StateManager

DENVER = {"location": "Denver, CO", "coordinate": "39.7392,-104.9903,40"}
LISTINGS_URL = "/v3/business_data/business_listings/search/live"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    env = EnvConfig(dataforseo_login="u", dataforseo_password="p", serper_api_key="k",
                    tinyfish_api_key="k", scavio_api_key="k")
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: env)


def routed(monkeypatch, handler):
    """Every httpx client the run opens answers from ``handler``."""
    real = httpx.AsyncClient

    class Routed(real):
        def __init__(self, *a, **kw):
            kw.pop("transport", None)
            super().__init__(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Routed)


def _listing(i: int) -> dict:
    return {"type": "business_listing", "title": f"Example Dental {i}",
            "domain": f"dental{i}.example.com", "url": f"https://dental{i}.example.com/",
            "phone": f"+1303555{i:04d}", "cid": str(9_000_000 + i),
            "place_id": f"place-{i}", "category": "Dentist", "is_claimed": True,
            "rating": {"value": 4.5, "votes_count": 3000 - i},
            "address_info": {"city": "Denver", "region": "CO"}}


class ListingsServer:
    """DataForSEO Business Listings, as its docs describe it: ``limit`` up to
    1,000, ``total_count`` on every page, an ``offset_token`` for the next
    page that must come with an otherwise identical request, and ``offset``
    for a client that doesn't use the token. Billed $0.012 a request plus
    $0.00036 a business returned."""

    def __init__(self, total: int, tokens: bool = True, overlap: int = 0):
        self.total, self.tokens, self.overlap = total, tokens, overlap
        self.requests: list[dict] = []
        self.first: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == LISTINGS_URL
        task = json.loads(request.content)[0]
        self.requests.append(task)
        token = task.get("offset_token")
        if token:
            # A token request repeats the request it follows.
            same = {k: v for k, v in task.items() if k != "offset_token"}
            assert same == self.first, "offset_token needs an otherwise identical request"
            start = int(token.split("-")[1])
        else:
            if self.first is None and task["limit"] > 1:
                self.first = dict(task)
            start = task.get("offset", 0)
        if start:
            # A source that repeats its last few rows on the next page.
            start -= self.overlap
        limit = task["limit"]
        assert 1 <= limit <= 1000
        items = [_listing(i) for i in range(start, min(start + limit, self.total))]
        end = start + len(items)
        result = {"total_count": self.total, "count": len(items), "offset": start,
                  "items": items}
        if self.tokens and end < self.total:
            result["offset_token"] = f"tok-{end + self.overlap}"
        cost = round(0.012 + 0.00036 * len(items), 6)
        return httpx.Response(200, json={
            "status_code": 20000, "cost": cost,
            "tasks": [{"status_code": 20000, "cost": cost, "result": [result]}]})


ENV = {"dataforseo_login": "u", "dataforseo_password": "p"}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def small_pages(monkeypatch):
    """Pages of 10 instead of 1,000, so a whole run stays quick to store."""
    monkeypatch.setattr(D.DataForSEOListings, "PAGE_SIZE", 10)
    return D.PROVIDERS["dataforseo_listings"]


def page(n: int) -> float:
    return 0.012 + 0.00036 * n


# ── DataForSEO Business Listings ──


@pytest.mark.asyncio
async def test_listings_pages_until_exhausted_with_offset_token():
    server = ListingsServer(total=2300)
    provider = D.DataForSEOListings()
    async with _client(server) as client:
        out = await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    assert len(out) == 2300
    assert len({b.external_id for b in out}) == 2300
    assert [r["limit"] for r in server.requests] == [1000, 1000, 1000]
    assert "offset_token" not in server.requests[0]
    assert [r.get("offset_token") for r in server.requests[1:]] == ["tok-1000", "tok-2000"]
    assert provider.available == 2300
    assert provider.last_cost == pytest.approx(3 * 0.012 + 2300 * 0.00036)
    assert provider.cost_of(2300) == pytest.approx(provider.last_cost)
    assert not provider.stop_reason and provider.unfetched is None


@pytest.mark.asyncio
async def test_listings_pages_by_offset_without_a_token():
    server = ListingsServer(total=2050, tokens=False)
    provider = D.DataForSEOListings()
    async with _client(server) as client:
        out = await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    assert len(out) == 2050
    assert [r.get("offset", 0) for r in server.requests] == [0, 1000, 2000]


@pytest.mark.asyncio
async def test_listings_never_returns_a_business_twice():
    # The source repeats its last 5 rows at the top of each next page.
    server = ListingsServer(total=2300, tokens=False, overlap=5)
    provider = D.DataForSEOListings()
    async with _client(server) as client:
        out = await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    ids = [b.external_id for b in out]
    assert len(ids) == len(set(ids))
    assert set(ids) == {f"dataforseo:cid:{9_000_000 + i}" for i in range(2300)}


@pytest.mark.asyncio
async def test_listings_limit_still_takes_fewer_when_asked():
    server = ListingsServer(total=2300)
    provider = D.DataForSEOListings()
    async with _client(server) as client:
        out = await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", limit=1500, **DENVER))
    assert len(out) == 1500
    assert len(server.requests) == 2


@pytest.mark.asyncio
async def test_listings_kill_switch_stops_between_pages():
    server = ListingsServer(total=2300)
    provider = D.DataForSEOListings()
    provider.remaining_budget = float("inf")

    async def paused():
        return len(server.requests) >= 1     # pressed once the first page is in

    provider.paused = paused
    async with _client(server) as client:
        out = await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    provider.paused = None
    assert len(out) == 1000 and len(server.requests) == 1
    assert provider.stop_reason == "paused"
    assert provider.unfetched == 1300
    assert provider.unfetched_cost == pytest.approx(provider.cost_of(1300))


@pytest.mark.asyncio
async def test_spend_cap_stops_a_run_midway_and_says_what_is_left(state, monkeypatch,
                                                                small_pages):
    server = ListingsServer(total=1230)
    routed(monkeypatch, server)
    queries = [D.DiscoveryQuery(term="dentist", **DENVER),
               D.DiscoveryQuery(term="orthodontist", **DENVER)]
    # Each page of 10 is $0.0156; the fourth would pass the $0.05 cap.
    report = await R.run_discovery(state, load_config(), "dataforseo_listings", queries,
                                   max_spend=0.05)
    assert report.stopped == "budget reached"
    assert report.actual_cost == pytest.approx(3 * page(10))
    assert report.actual_cost <= 0.05
    assert report.available == 1230
    assert report.new_companies == 30
    assert report.unfetched == 1200
    assert report.queries_left == 1
    # The 1,200 left, plus the search not run priced at one full page.
    assert report.unfetched_cost == pytest.approx(small_pages.cost_of(1200) + page(10))
    line = report.as_dict()["left_behind"]
    assert "1,200 results left unfetched" in line and "1 search not run" in line
    assert "$1.89" in line


@pytest.mark.asyncio
async def test_first_page_shrinks_to_what_the_cap_buys(state, monkeypatch, small_pages):
    server = ListingsServer(total=23)
    routed(monkeypatch, server)
    report = await R.run_discovery(state, load_config(), "dataforseo_listings",
                                   [D.DiscoveryQuery(term="dentist", **DENVER)],
                                   max_spend=page(4))
    assert server.requests[0]["limit"] == 4
    assert report.new_companies == 4
    assert report.actual_cost <= page(4) + 1e-9
    assert report.stopped == "budget reached"
    assert report.unfetched == 19


@pytest.mark.asyncio
async def test_a_full_run_stores_each_business_once(state, monkeypatch, small_pages):
    server = ListingsServer(total=23, tokens=False, overlap=2)
    routed(monkeypatch, server)
    report = await R.run_discovery(state, load_config(), "dataforseo_listings",
                                   [D.DiscoveryQuery(term="dentist", **DENVER)],
                                   max_spend=5.0)
    assert not report.stopped and report.unfetched == 0
    assert len(server.requests) == 3
    assert report.new_companies == 23 and report.known_companies == 0
    with closing(sqlite3.connect(state.db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 23
        rows, companies = db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT company_id) FROM observations "
            "WHERE signal_code = 'REVIEW_COUNT'").fetchone()
    assert rows == companies == 23


@pytest.mark.asyncio
async def test_estimate_counts_first_then_is_exact(state, monkeypatch):
    server = ListingsServer(total=1840)
    routed(monkeypatch, server)
    queries = [D.DiscoveryQuery(term="dentist", **DENVER)]
    uncounted = await R.run_discovery(state, load_config(), "dataforseo_listings",
                                      queries, dry_run=True)
    assert not server.requests and not uncounted.counted
    assert uncounted.estimated_cost == pytest.approx(0.372)   # one full page

    report = await R.run_discovery(state, load_config(), "dataforseo_listings",
                                   queries, dry_run=True, count=True)
    # One cheap request: a single business, to read total_count.
    assert [r["limit"] for r in server.requests] == [1]
    assert report.counted and report.available == 1840
    assert report.count_cost == pytest.approx(0.012 + 0.00036)
    # Two pages (1,000 + 840) and every business: 2 x $0.012 + 1,840 x $0.00036.
    assert report.estimated_cost == pytest.approx(0.6864)
    assert not await state.get_runs()      # counting isn't a run


# ── DataForSEO SERP and Maps: as deep as the source goes ──


def _serp_envelope(items, cost):
    return {"status_code": 20000, "cost": cost,
            "tasks": [{"status_code": 20000, "result": [{"items": items}]}]}


@pytest.mark.asyncio
async def test_dataforseo_rank_searches_go_to_the_source_maximum():
    tasks = []

    def handler(request):
        tasks.append(json.loads(request.content)[0])
        return httpx.Response(200, json=_serp_envelope([], 0.002))

    async with _client(handler) as client:
        await D.DataForSEOSerp().fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
        await D.DataForSEOMaps().fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    assert [t["depth"] for t in tasks] == [200, 700]
    assert D.estimate_cost("dataforseo_serp", [D.DiscoveryQuery(term="x")]) == pytest.approx(0.04)
    assert D.estimate_cost("dataforseo_maps", [D.DiscoveryQuery(term="x")]) == pytest.approx(0.014)


@pytest.mark.asyncio
async def test_dataforseo_serp_depth_shrinks_to_the_cap():
    tasks = []

    def handler(request):
        tasks.append(json.loads(request.content)[0])
        return httpx.Response(200, json=_serp_envelope([], 0.01))

    provider = D.DataForSEOSerp()
    provider.remaining_budget = 0.011           # five pages of 10
    async with _client(handler) as client:
        await provider.fetch(client, ENV, D.DiscoveryQuery(term="dentist", **DENVER))
    provider.remaining_budget = float("inf")
    assert tasks[0]["depth"] == 50
    assert provider.stop_reason == "budget reached"
    assert not provider.complete       # ranks below 50 weren't looked at
    assert provider.unfetched_cost == pytest.approx(15 * 0.002)


# ── Serper, Scavio, TinyFish, OpenStreetMap ──


@pytest.mark.asyncio
async def test_serper_pages_until_google_has_no_more():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        page = body.get("page", 1)
        organic = [] if page > 3 else [
            {"title": f"Example Clinic {page}-{i}", "position": i,
             "link": f"https://clinic{page}x{i}.example.com/"} for i in range(1, 11)]
        return httpx.Response(200, json={"organic": organic})

    provider = D.Serper()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"serper_api_key": "k"},
                                   D.DiscoveryQuery(term="dentist", location="Denver, CO"))
    assert [b.get("page", 1) for b in bodies] == [1, 2, 3, 4]
    assert all(b["num"] == 10 for b in bodies)
    assert len(out) == 30
    assert [b.rank for b in out][9:12] == [10, 11, 12]      # page 2 starts at 11
    assert provider.last_cost == pytest.approx(4 * 0.001)


@pytest.mark.asyncio
async def test_serper_spend_cap_stops_between_pages():
    def handler(request):
        page = json.loads(request.content).get("page", 1)
        return httpx.Response(200, json={"organic": [
            {"title": "Example", "position": i, "link": f"https://c{page}x{i}.example.com/"}
            for i in range(1, 11)]})

    provider = D.Serper()
    provider.remaining_budget = 0.0025           # two pages
    async with _client(handler) as client:
        out = await provider.fetch(client, {"serper_api_key": "k"},
                                   D.DiscoveryQuery(term="dentist"))
    provider.remaining_budget = float("inf")
    assert len(out) == 20
    assert provider.stop_reason == "budget reached"


@pytest.mark.asyncio
async def test_scavio_search_pages_until_no_next_page():
    starts = []

    def handler(request):
        start = json.loads(request.content)["start"]
        starts.append(start)
        page = start // 10
        return httpx.Response(200, json={
            "organic_results": [{"title": "Example", "position": i,
                                 "link": f"https://s{page}x{i}.example.com/"}
                                for i in range(1, 11)],
            "pagination": {"next": "more"} if page < 6 else {}})

    provider = D.ScavioSearch()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"scavio_api_key": "k"},
                                   D.DiscoveryQuery(term="dentist"))
    assert starts == [0, 10, 20, 30, 40, 50, 60]
    assert len(out) == 70


@pytest.mark.asyncio
async def test_scavio_maps_takes_all_six_pages_google_maps_serves():
    starts = []

    def handler(request):
        start = json.loads(request.content)["start"]
        starts.append(start)
        return httpx.Response(200, json={"local_results": [
            {"title": f"Example {start + i}", "place_id": f"p{start + i}"} for i in range(20)]})

    async with _client(handler) as client:
        out = await D.ScavioMaps().fetch(client, {"scavio_api_key": "k"},
                                         D.DiscoveryQuery(term="dentist", **DENVER))
    assert starts == [0, 20, 40, 60, 80, 100]
    assert len(out) == 120


@pytest.mark.asyncio
async def test_tinyfish_takes_every_page_it_serves():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json={"results": [
            {"title": "Example", "url": f"https://t{page}x{i}.example.com/"} for i in range(10)]})

    async with _client(handler) as client:
        out = await D.TinyFish().fetch(client, {"tinyfish_api_key": "k"},
                                       D.DiscoveryQuery(term="dentist"))
    assert pages == list(range(11))
    assert len(out) == 110


@pytest.mark.asyncio
async def test_osm_asks_for_every_match():
    bodies = []

    def handler(request):
        bodies.append(request.content.decode())
        return httpx.Response(200, json={"elements": []})

    async with _client(handler) as client:
        await D.OpenStreetMap().fetch(client, {}, D.DiscoveryQuery(term="dentist", **DENVER))
        await D.OpenStreetMap().fetch(client, {}, D.DiscoveryQuery(term="dentist", limit=50, **DENVER))
    assert bodies[0].endswith("out center;")
    assert bodies[1].endswith("out center 50;")


def test_no_fixed_limit_by_default():
    q = D.DiscoveryQuery(term="dentist")
    assert q.limit is None and q.depth is None
    config = load_config()
    assert all(x.limit is None and x.depth is None for x in D.build_queries(config))
