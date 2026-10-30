"""Background jobs: the queue, the worker, the lanes and the rules a job
keeps (harvey/modules/jobs.py, harvey/modules/worker.py,
harvey/modules/job_kinds.py).

Every source works on its own lane, none waits on another; a lane runs its
jobs in order; one worker per database; a stopped search resumes from its
page; the spend cap is reserved before a paid job spends; the same work
can't wait twice. Fictional example.com businesses only.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig
from harvey.modules import job_kinds, jobs
from harvey.modules import runs as R
from harvey.modules.worker import (Worker, acquire_lease, lease_holder,
                                   release_lease, wait_for_batch)
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import StateManager


# The real one, before the fixture below stands it in.
REAL_SITE_READS = job_kinds.enqueue_site_reads


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
    async def no_geocode(state, city, radius_km=40, client=None):
        return ""

    monkeypatch.setattr(R, "geocode_city", no_geocode)
    env = EnvConfig(serper_api_key="k", tinyfish_api_key="k")
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: env)
    monkeypatch.setattr(job_kinds, "_config", lambda: None)
    # Site reads aren't what these tests are about.
    monkeypatch.setattr(job_kinds, "enqueue_site_reads", _no_site_reads)


async def _no_site_reads(*a, **k):
    return [], []


def rows(state, sql, *args):
    with closing(sqlite3.connect(state.db_path)) as db:
        return db.execute(sql, args).fetchall()


def routed(monkeypatch, handler):
    """Every httpx client a job opens answers from ``handler``."""
    real = httpx.AsyncClient

    class Routed(real):
        def __init__(self, *a, **kw):
            kw.pop("transport", None)
            super().__init__(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Routed)


class Serper:
    """Serper's search: 10 results a page for pages 1..``pages``, then none.
    Remembers the pages asked for."""

    def __init__(self, pages=3, on_request=None):
        self.pages = pages
        self.asked: list[int] = []
        self.on_request = on_request

    def __call__(self, request):
        page = json.loads(request.content).get("page", 1)
        self.asked.append(page)
        if self.on_request:
            self.on_request(page)
        organic = [] if page > self.pages else [
            {"title": f"Example Clinic {page}-{i}", "position": i,
             "link": f"https://clinic{page}x{i}.example.com/"} for i in range(1, 11)]
        return httpx.Response(200, json={"organic": organic})


async def run_until_idle(state, worker=None, timeout=10.0, **kw):
    """Run a worker until no job is queued or running, then stop it."""
    worker = worker or Worker(state, poll=0.05, **kw)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        paused = {k[len("lanes."):-len(".paused")] for k, v in rows(
            state, "SELECT key, value FROM settings WHERE key LIKE 'lanes.%.paused'") if v}
        left = sum(1 for (lane,) in rows(
            state, "SELECT lane FROM jobs WHERE status IN ('queued', 'running') "
                   "AND (not_before IS NULL OR not_before <= datetime('now'))")
            if lane not in paused)
        if not left and worker.idle():
            break
    stop.set()
    await asyncio.wait_for(task, 10)
    return worker


@pytest.fixture
def kinds(monkeypatch):
    """Register job kinds for one test."""
    def add(name, fn):
        monkeypatch.setitem(jobs.KINDS, name, jobs.Kind(name, fn))
    return add


# ── The table ──


@pytest.mark.asyncio
async def test_the_schema_has_jobs_batches_and_the_lease(state):
    tables = {r[0] for r in rows(state, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"jobs", "job_batches", "worker_lease"} <= tables


@pytest.mark.asyncio
async def test_the_same_work_cant_wait_twice(state):
    first = await jobs.enqueue(state, "k", "lane", {"x": 1}, dedupe_key="same", label="One")
    with pytest.raises(jobs.Duplicate) as dup:
        await jobs.enqueue(state, "k", "lane", {"x": 1}, dedupe_key="same")
    assert dup.value.job.id == first and first in str(dup.value)
    # Once it finished, the same work can be asked for again.
    await jobs.finish(state, first, jobs.DONE)
    second = await jobs.enqueue(state, "k", "lane", {"x": 1}, dedupe_key="same")
    assert second != first
    # A paused one still counts as waiting.
    await jobs.pause(state, second)
    with pytest.raises(jobs.Duplicate):
        await jobs.enqueue(state, "k", "lane", {}, dedupe_key="same")


@pytest.mark.asyncio
async def test_a_second_discovery_run_queues_and_a_duplicate_names_the_job(state, claim_provider):
    claim_provider(_Paged("serper_stub"))
    q1 = [D.DiscoveryQuery(term="dentist", location="Brașov")]
    q2 = [D.DiscoveryQuery(term="dentist", location="Sibiu")]
    batch, ids, dupes = await job_kinds.enqueue_discovery(state, "serper_stub", q1, max_spend=1)
    assert len(ids) == 1 and not dupes
    # Another search while the first waits: queued, not refused.
    _, ids2, _ = await job_kinds.enqueue_discovery(state, "serper_stub", q2, max_spend=1)
    assert len(ids2) == 1
    with pytest.raises(jobs.Duplicate) as dup:
        await job_kinds.enqueue_discovery(state, "serper_stub", q1, max_spend=1)
    assert ids[0] in str(dup.value)
    # A run with one new search and one asked again queues the new one.
    _, ids3, dupes3 = await job_kinds.enqueue_discovery(
        state, "serper_stub", q1 + [D.DiscoveryQuery(term="dentist", location="Cluj")],
        max_spend=1)
    assert len(ids3) == 1 and [d.id for d in dupes3] == ids
    lanes = {r[0] for r in rows(state, "SELECT lane FROM jobs")}
    assert lanes == {"stub_serper_stub"}


# ── Claiming ──


@pytest.mark.asyncio
async def test_two_workers_never_claim_the_same_job(state):
    ids, _ = await jobs.enqueue_many(state, [dict(kind="k", lane="osm") for _ in range(40)])
    taken: list[str] = []

    async def grab(name):
        while True:
            job = await jobs.claim(state, "osm", name)
            if job is None:
                return
            taken.append(job.id)

    await asyncio.gather(grab("a"), grab("b"), grab("c"))
    assert sorted(taken) == sorted(ids)
    assert len(set(taken)) == 40
    assert rows(state, "SELECT COUNT(*) FROM jobs WHERE status = 'running'")[0][0] == 40


@pytest.mark.asyncio
async def test_two_processes_never_claim_the_same_job(state):
    """Separate connections, as two processes would have them."""
    await jobs.enqueue_many(state, [dict(kind="k", lane="osm") for _ in range(30)])
    other = StateManager(state.db_path)
    taken = []

    async def grab(store, name):
        while (job := await jobs.claim(store, "osm", name)) is not None:
            taken.append((job.id, name))

    await asyncio.gather(grab(state, "one"), grab(other, "two"))
    assert len({j for j, _ in taken}) == len(taken) == 30


@pytest.mark.asyncio
async def test_a_lane_runs_its_jobs_in_order(state, kinds):
    order = []

    async def note(ctx):
        order.append(ctx.params["n"])
        return {}

    kinds("note", note)
    await jobs.enqueue_many(state, [dict(kind="note", lane="osm", params={"n": i})
                                    for i in range(5)])
    await run_until_idle(state)
    assert order == [0, 1, 2, 3, 4]


# ── One worker per database ──


@pytest.mark.asyncio
async def test_one_worker_holds_the_lease(state):
    assert await acquire_lease(state, "dashboard", ttl=30)
    assert not await acquire_lease(state, "cli", ttl=30)
    assert (await lease_holder(state))["owner"] == "dashboard"
    assert await acquire_lease(state, "dashboard", ttl=30)       # renewing is fine
    await release_lease(state, "dashboard")
    assert await lease_holder(state) is None
    assert await acquire_lease(state, "cli", ttl=30)


@pytest.mark.asyncio
async def test_a_lease_whose_worker_stopped_beating_is_taken_over(state):
    assert await acquire_lease(state, "gone", ttl=30)
    with closing(sqlite3.connect(state.db_path)) as db:
        db.execute("UPDATE worker_lease SET heartbeat_at = ?", (time.time() - 60,))
        db.commit()
    assert await lease_holder(state, ttl=30) is None
    assert await acquire_lease(state, "new", ttl=30)


@pytest.mark.asyncio
async def test_a_second_worker_waits_while_the_first_runs(state, kinds):
    ran = []

    async def note(ctx):
        ran.append(ctx.worker.name)
        return {}

    kinds("note", note)
    assert await acquire_lease(state, "dashboard")
    await jobs.enqueue(state, "note", "osm")
    other = Worker(state, name="cli", wait_for_lease=False, poll=0.05)
    assert await other.run(asyncio.Event()) is False
    assert ran == [] and rows(state, "SELECT status FROM jobs") == [("queued",)]


@pytest.mark.asyncio
async def test_jobs_a_dead_worker_left_running_go_back_and_run(state, kinds):
    ran = []

    async def note(ctx):
        ran.append(ctx.job.attempts)
        return {}

    kinds("note", note)
    job_id = await jobs.enqueue(state, "note", "osm")
    assert (await jobs.claim(state, "osm", "killed-worker")).id == job_id
    await run_until_idle(state)
    assert rows(state, "SELECT status FROM jobs") == [("done",)]
    assert ran == [0]           # run again; a restart doesn't use up one of its tries


# ── Lanes side by side ──


@pytest.mark.asyncio
async def test_a_slow_tinyfish_job_doesnt_hold_up_an_osm_one(state, kinds):
    release = asyncio.Event()
    finished = []

    async def slow(ctx):
        await release.wait()
        finished.append("tinyfish")
        return {}

    async def quick(ctx):
        finished.append("osm")
        return {}

    kinds("slow", slow)
    kinds("quick", quick)
    await jobs.enqueue(state, "slow", "tinyfish")
    await jobs.enqueue(state, "quick", "osm")
    worker = Worker(state, poll=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if dict(rows(state, "SELECT lane, status FROM jobs")) == {
                    "tinyfish": "running", "osm": "done"}:
                break
        assert finished == ["osm"]
        assert dict(rows(state, "SELECT lane, status FROM jobs")) == {"tinyfish": "running",
                                                                      "osm": "done"}
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(finished) == 2:
                break
    finally:
        release.set()
        stop.set()
        await task
    assert finished == ["osm", "tinyfish"]


@pytest.mark.asyncio
async def test_a_paused_lane_starts_nothing_until_resumed(state, kinds):
    ran = []

    async def note(ctx):
        ran.append(ctx.job.lane)
        return {}

    kinds("note", note)
    await jobs.pause_lane(state, "tinyfish")
    await jobs.enqueue(state, "note", "tinyfish")
    await jobs.enqueue(state, "note", "osm")
    worker = Worker(state, poll=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.5)
    assert ran == ["osm"]
    await jobs.resume_lane(state, "tinyfish")
    for _ in range(60):
        await asyncio.sleep(0.05)
        if len(ran) == 2:
            break
    stop.set()
    await task
    assert ran == ["osm", "tinyfish"]


# ── Retries and stops ──


@pytest.mark.asyncio
async def test_a_passing_problem_is_tried_again_later_then_fails(state, kinds, monkeypatch):
    async def flaky(ctx):
        raise httpx.ConnectError("no route")

    kinds("flaky", flaky)
    monkeypatch.setattr(jobs, "MAX_ATTEMPTS", 2)
    job_id = await jobs.enqueue(state, "flaky", "osm")
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "osm", worker.name), {})
    (status, attempts, error, wait), = rows(
        state, "SELECT status, attempts, error, (julianday(not_before) - julianday('now')) "
               "* 86400 FROM jobs")
    assert (status, attempts, error) == ("queued", 1, "no route")
    assert jobs.backoff_seconds(1) - 2 < wait <= jobs.backoff_seconds(1)
    # Not due yet: nothing to claim.
    assert await jobs.claim(state, "osm", worker.name) is None
    with closing(sqlite3.connect(state.db_path)) as db:
        db.execute("UPDATE jobs SET not_before = NULL")
        db.commit()
    await worker.run_job(await jobs.claim(state, "osm", worker.name), {})
    assert rows(state, "SELECT status, attempts, error FROM jobs") == [
        ("failed", 2, "no route")]
    assert jobs.backoff_seconds(1) < jobs.backoff_seconds(2) < jobs.backoff_seconds(3)
    # A failed job can be retried by hand.
    await jobs.retry(state, job_id)
    assert rows(state, "SELECT status, attempts, error FROM jobs") == [("queued", 0, "")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_a_429_or_a_5xx_is_tried_again(state, kinds, status):
    async def busy(ctx):
        request = httpx.Request("GET", "https://api.example.com/")
        raise httpx.HTTPStatusError("busy", request=request,
                                    response=httpx.Response(status, request=request))

    kinds("busy", busy)
    await jobs.enqueue(state, "busy", "osm")
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "osm", worker.name), {})
    assert rows(state, "SELECT status FROM jobs") == [("queued",)]


@pytest.mark.asyncio
async def test_a_plain_error_fails_at_once_with_its_message(state, kinds):
    async def broken(ctx):
        raise ValueError("the file is not what it should be")

    kinds("broken", broken)
    await jobs.enqueue(state, "broken", "osm")
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "osm", worker.name), {})
    assert rows(state, "SELECT status, error FROM jobs") == [
        ("failed", "the file is not what it should be")]


@pytest.mark.asyncio
async def test_out_of_credit_pauses_the_lane(state, kinds):
    async def spent(ctx):
        raise jobs.LaneExhausted("refused the key (HTTP 402)")

    kinds("spent", spent)
    await jobs.enqueue(state, "spent", "serper")
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "serper", worker.name), {})
    assert await jobs.lane_paused(state, "serper") == "refused the key (HTTP 402)"
    # The job waits in the queue for the lane, ready to carry on.
    assert rows(state, "SELECT status, error FROM jobs") == [
        ("queued", "refused the key (HTTP 402)")]


@pytest.mark.asyncio
async def test_an_hourly_allowance_waits_instead_of_pausing(state, kinds):
    async def hourly(ctx):
        raise jobs.LaneExhausted("used up this hour", retry_after=600)

    kinds("hourly", hourly)
    await jobs.enqueue(state, "hourly", "tinyfish")
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "tinyfish", worker.name), {})
    assert await jobs.lane_paused(state, "tinyfish") == ""
    (status, wait), = rows(state, "SELECT status, (julianday(not_before) - julianday('now')) "
                                  "* 86400 FROM jobs")
    assert status == "queued" and 590 < wait <= 600


@pytest.mark.asyncio
async def test_cancel_pause_and_resume_by_hand(state):
    job_id = await jobs.enqueue(state, "k", "osm")
    assert (await jobs.pause(state, job_id)).status == "paused"
    assert (await jobs.resume(state, job_id)).status == "queued"
    assert (await jobs.cancel(state, job_id)).status == "cancelled"
    with pytest.raises(jobs.Refused):
        await jobs.resume(state, job_id)
    # A running search is asked to stop at its next page.
    other = await jobs.enqueue(state, "discover", "osm")
    await jobs.claim(state, "osm", "w")
    assert (await jobs.cancel(state, other)).stop_requested == jobs.STOP_CANCEL
    assert (await jobs.find(state, other[:6])).id == other


# ── Discovery as jobs: pages, the cap, resume, cancel ──


class _Paged(D.Serper):
    """Serper's code under another key, so a test can claim it."""

    def __init__(self, key):
        self.key = key


@pytest.mark.asyncio
async def test_a_search_stopped_by_the_cap_pauses_and_resumes_from_its_page(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    server = Serper(pages=3)
    routed(monkeypatch, server)
    query = D.DiscoveryQuery(term="dentist", location="Denver, CO")
    batch, (job_id,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [query], max_spend=0.0025)     # two pages
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "paused" and job.paused_reason == jobs.SPEND_LIMIT
    assert job.progress["cursor"] == {"page": 3} and job.progress["pages"] == 2
    assert server.asked == [1, 2]
    assert job.actual_cost == pytest.approx(0.002)
    # Raised, and resumed: it carries on at page 3, never paying for 1 and 2 again.
    await jobs.set_batch_cap(state, batch, 1.0)
    await jobs.resume(state, job_id)
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "done" and server.asked == [1, 2, 3, 4]
    assert job.result["new_companies"] == 30 and job.progress["pages"] == 4
    assert job.actual_cost == pytest.approx(0.004)
    assert rows(state, "SELECT COUNT(*) FROM companies")[0][0] == 30


@pytest.mark.asyncio
async def test_a_search_cut_off_by_a_restart_resumes_from_its_cursor(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    server = Serper(pages=4)
    routed(monkeypatch, server)
    query = D.DiscoveryQuery(term="dentist", location="Denver, CO")
    _, (job_id,), _ = await job_kinds.enqueue_discovery(state, "serper_stub", [query],
                                                        max_spend=1.0)
    # A worker got it as far as page 3, saved, and was killed.
    await jobs.claim(state, "stub_serper_stub", "killed")
    await jobs.save_progress(state, job_id, {"cursor": {"page": 3}, "pages": 2, "results": 20})
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "done" and server.asked == [3, 4, 5]
    assert job.progress["pages"] == 5 and job.progress["results"] == 40
    # Page 3's first result is ranked 21: positions carry on from the page.
    ranks = rows(state, "SELECT value_num FROM observations WHERE signal_code = 'SERP_RANK' "
                        "ORDER BY value_num LIMIT 1")
    assert ranks == [(21.0,)]
    # A resumed search didn't see its first pages, so it marks nobody "gone".
    assert rows(state, "SELECT COUNT(*) FROM observations WHERE signal_code = 'SERP_RANK' "
                       "AND value_num = 0")[0][0] == 0


@pytest.mark.asyncio
async def test_spend_is_reserved_from_the_run_s_cap_before_a_job_spends(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    server = Serper(pages=5)
    routed(monkeypatch, server)
    queries = [D.DiscoveryQuery(term="dentist", location=city) for city in ("Brașov", "Sibiu")]
    batch, ids, _ = await job_kinds.enqueue_discovery(state, "serper_stub", queries,
                                                      max_spend=0.0015)
    await run_until_idle(state)
    first, second = [await jobs.get(state, i) for i in ids]
    # The first spent what the cap allowed (one page); the second couldn't
    # reserve even its first request, so it asked nothing.
    assert first.status == second.status == "paused"
    assert first.actual_cost == pytest.approx(0.001) and second.actual_cost == 0
    assert server.asked == [1]
    assert second.paused_reason == jobs.SPEND_LIMIT and "spend limit" in second.error
    assert await jobs.batch_spent(state, batch) == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_cancel_mid_search_stops_after_the_page_and_keeps_it(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    holder = {}

    def cancel_on_first(page):
        if page == 1:
            # Asked while page 1 is in flight (a click on Queue).
            with closing(sqlite3.connect(state.db_path)) as db:
                db.execute("UPDATE jobs SET stop_requested = 'cancel' WHERE id = ?",
                           (holder["id"],))
                db.commit()

    server = Serper(pages=5, on_request=cancel_on_first)
    routed(monkeypatch, server)
    _, (job_id,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="dentist")], max_spend=1.0)
    holder["id"] = job_id
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "cancelled" and server.asked == [1]
    assert job.result["new_companies"] == 10            # page 1 is kept
    assert rows(state, "SELECT COUNT(*) FROM companies")[0][0] == 10


@pytest.mark.asyncio
async def test_a_source_out_of_credit_pauses_its_lane_not_the_others(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))

    def refused(request):
        return httpx.Response(200, json={"organic": []})

    class Spent(_Paged):
        async def fetch(self, client, env, query):
            raise D.ProviderExhausted("refused the key (HTTP 402)")

    claim_provider(Spent("spent_stub"))
    routed(monkeypatch, refused)
    await job_kinds.enqueue_discovery(state, "spent_stub", [D.DiscoveryQuery(term="a")],
                                      max_spend=1)
    _, (other,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="b")], max_spend=1)
    await run_until_idle(state)
    assert await jobs.lane_paused(state, "stub_spent_stub") == "refused the key (HTTP 402)"
    assert await jobs.lane_paused(state, "stub_serper_stub") == ""
    assert (await jobs.get(state, other)).status == "done"


@pytest.mark.asyncio
async def test_a_source_no_module_claims_is_refused(state, monkeypatch):
    monkeypatch.setitem(D.PROVIDERS, "ghost", _Paged("ghost"))
    assert "ghost" not in D.PROVIDERS
    with pytest.raises(ValueError, match="no module claims"):
        await R.run_discovery(state, None, "ghost", [D.DiscoveryQuery(term="a")])
    with pytest.raises(ValueError, match="No module claims"):
        await job_kinds.enqueue_discovery(state, "ghost", [D.DiscoveryQuery(term="a")],
                                          max_spend=1)


# ── A new search source is one module folder ──

MODULE_TOML = '''
id = "fakesearch"
name = "Fake search"
description = "A fictional search source, for a test."
set = "extra"
kind = "search"
version = "0.1.0"
api = 1
requires = []
default_enabled = true
entry = "harvey.modules.extra.fakesearch.capabilities"
secrets = ["FAKESEARCH_API_KEY"]

[[capabilities]]
id = "web_search"
op = "query"
provider = "fakesearch"
inputs = [["query"]]
outputs = { records = "companies", ids = ["domain"], facts = ["FOUND_IN_SERP"] }
cost = { usd = 0.0, per = "search" }
'''

MODULE_CODE = '''
from harvey.collectors.discover import Business, DiscoveryProvider, provider_capabilities


class FakeSearch(DiscoveryProvider):
    key = "fakesearch"
    label = "Fake search"
    kind = "serp"
    env_keys = ("fakesearch_api_key",)

    async def fetch(self, client, env, query):
        assert env["fakesearch_api_key"] == "fictional-key"
        return [Business(name=f"Example {i}", domain=f"shop{i}.example.com",
                         has_website=True, provider=self.key) for i in range(3)]


PROVIDERS = {"fakesearch": FakeSearch()}
CAPABILITIES = provider_capabilities("fakesearch", {"web_search": "fakesearch"})
'''


@pytest.mark.asyncio
async def test_a_new_search_source_is_one_module_folder(state, monkeypatch, tmp_path):
    import harvey.modules
    from harvey.config import _module_keys
    from harvey.modules import switches
    from harvey.modules.loader import read_manifest

    folder = tmp_path / "extra" / "fakesearch"
    folder.mkdir(parents=True)
    (tmp_path / "extra" / "__init__.py").write_text("")
    (folder / "__init__.py").write_text("")
    (folder / "module.toml").write_text(MODULE_TOML)
    (folder / "capabilities.py").write_text(MODULE_CODE)
    monkeypatch.setattr(harvey.modules, "__path__", [*harvey.modules.__path__, str(tmp_path)])
    for name in [n for n in sys.modules if n.startswith("harvey.modules.extra")]:
        monkeypatch.delitem(sys.modules, name)
    manifest = read_manifest(folder / "module.toml")
    mans = (*switches.manifests(), manifest)
    monkeypatch.setattr(switches, "manifests", lambda: mans)
    # The module's .env key is read because its manifest names it (no edit
    # in config.py), and reaches the provider like a built-in one's.
    monkeypatch.setenv("FAKESEARCH_API_KEY", "fictional-key")
    keys = _module_keys()
    assert keys["fakesearch_api_key"] == "fictional-key"
    env = EnvConfig(**keys)
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: env)

    assert "fakesearch" in D.PROVIDERS
    menu = {p["key"]: p for p in D.provider_menu(env.model_dump())}
    assert menu["fakesearch"]["module"] == "fakesearch" and menu["fakesearch"]["configured"]
    assert "FAKESEARCH_API_KEY" in switches.env_keys(await switches.enabled_ids(state))

    _, (job_id,), _ = await job_kinds.enqueue_discovery(
        state, "fakesearch", [D.DiscoveryQuery(term="shop")], max_spend=0)
    assert (await jobs.get(state, job_id)).lane == "fakesearch"
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "done" and job.result["new_companies"] == 3
    assert rows(state, "SELECT DISTINCT module FROM observations") == [("fakesearch",)]


# ── Commands wait for their jobs ──


@pytest.mark.asyncio
async def test_a_command_runs_its_own_jobs_when_no_worker_is_running(state, kinds):
    async def talk(ctx):
        await ctx.say("  half way")
        return {"ok": True}

    kinds("talk", talk)
    batch = await jobs.new_batch(state, "talk")
    await jobs.enqueue(state, "talk", "osm", batch_id=batch)
    # Somebody else's job, in another run: the command leaves it alone.
    other = await jobs.enqueue(state, "talk", "osm")
    said = []
    found = await wait_for_batch(state, batch, say=said.append, poll=0.05)
    assert [j.status for j in found] == ["done"] and said == ["  half way"]
    assert (await jobs.get(state, other)).status == "queued"
    assert await lease_holder(state) is None              # released when done


@pytest.mark.asyncio
async def test_a_command_follows_the_worker_that_holds_the_lease(state, kinds):
    async def talk(ctx):
        await ctx.say("  from the dashboard's worker")
        return {}

    kinds("talk", talk)
    worker = Worker(state, name="dashboard", poll=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(40):
        await asyncio.sleep(0.05)
        if worker.holding:
            break
    batch = await jobs.new_batch(state, "talk")
    await jobs.enqueue(state, "talk", "osm", batch_id=batch)
    said = []
    found = await asyncio.wait_for(wait_for_batch(state, batch, say=said.append, poll=0.05), 10)
    stop.set()
    await task
    assert found[0].status == "done" and found[0].worker == ""
    assert said[0].startswith("  Queued. The worker running on")
    assert said[-1] == "  from the dashboard's worker"


# ── Site reads: one job per company, no cap ──


@pytest.mark.asyncio
async def test_every_company_found_gets_its_own_site_read(state, monkeypatch):
    from harvey.models.company import Company

    for i in range(260):        # more than the old 200-a-run cap
        await state.add_company(Company(name=f"Example {i}", domain=f"site{i}.example.com"))
    await state.add_company(Company(name="No site", domain=""))
    ids, dupes = await REAL_SITE_READS(state)
    assert len(ids) == 260 and not dupes
    assert rows(state, "SELECT DISTINCT kind, lane FROM jobs") == [("site_read", "website")]
    again, dupes = await REAL_SITE_READS(state)
    assert again == [] and len(dupes) == 260


@pytest.mark.asyncio
async def test_a_site_read_job_reads_one_company(state, monkeypatch):
    from harvey.models.company import Company

    cid = await state.add_company(Company(name="Example", domain="example.com"))
    seen = []

    async def fake_profile(state_, companies, confirmed, run_id="", config=None,
                           role_keywords=None):
        seen.append((companies[0]["domain"], run_id))
        return 3

    monkeypatch.setattr(R, "profile", fake_profile)
    monkeypatch.setattr("harvey.modules.job_kinds._config", lambda: None)
    job_id = await jobs.enqueue(state, "site_read", "website", {"company_id": cid})
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "done" and job.result == {"domain": "example.com", "observations": 3}
    # One run in the runs log for the lane's streak, closed when it went idle.
    (stage, status, records), = rows(state, "SELECT stage, status, records FROM runs")
    assert (stage, status, records) == ("profile", "completed", 1)
    assert seen[0][1]


# ── The heartbeat keeps only email ──


@pytest.mark.asyncio
async def test_the_heartbeat_queues_contact_finding_instead_of_running_it(state):
    from harvey.modules.job_kinds import enqueue_find_contacts

    first = await enqueue_find_contacts(state)
    assert first and await enqueue_find_contacts(state) is None     # one at a time
    assert rows(state, "SELECT kind, lane FROM jobs") == [("find_contacts", "contacts")]


@pytest.mark.asyncio
async def test_with_the_email_module_off_the_heartbeat_does_nothing(state, monkeypatch):
    from types import SimpleNamespace

    import harvey.agents.analyst
    import harvey.agents.handler
    import harvey.agents.sender
    import harvey.agents.writer
    import harvey.main as main_mod
    from harvey.config import load_config
    from harvey.modules import switches

    await switches.set_enabled(state, switches.EMAIL_MODULE, False)
    config = load_config()
    stop = asyncio.Event()
    slept = []

    async def one_cycle(seconds, event):
        slept.append(seconds)
        return True

    class NoBrain:
        provider_label, model = "none", ""

        def __init__(self, *a, **k):
            pass

        async def preflight(self):
            raise AssertionError("the brain was asked with email off")

    monkeypatch.setattr(main_mod, "load_config", lambda: config)
    monkeypatch.setattr(main_mod, "load_env", lambda: SimpleNamespace())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    monkeypatch.setattr(main_mod, "Brain", NoBrain)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c: False)
    monkeypatch.setattr(main_mod, "_interruptible_sleep", one_cycle)

    def agent(*a, **k):
        return SimpleNamespace(is_native=False, run=None)

    for mod, name in ((harvey.agents.writer, "Writer"), (harvey.agents.sender, "Sender"),
                      (harvey.agents.handler, "Handler"), (harvey.agents.analyst, "Analyst")):
        monkeypatch.setattr(mod, name, agent)
    await asyncio.wait_for(main_mod.heartbeat(stop), 30)
    assert slept and rows(state, "SELECT COUNT(*) FROM jobs")[0][0] == 0
    assert rows(state, "SELECT COUNT(*) FROM actions")[0][0] == 0


# ── The dashboard: Run queues, Coadă shows it ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    import harvey.state as state_mod

    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")      # nothing runs: just the queue
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_a_second_run_is_queued_and_a_duplicate_points_at_its_job(client):
    first = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"]})
    assert first.status_code == 200 and first.json()["success"], first.json()
    second = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Sibiu"]})
    assert second.status_code == 200 and second.json()["success"]
    again = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"]})
    body = again.json()
    assert again.status_code == 409 and body["duplicate"]
    assert body["job"]["id"] in body["message"] and "already queued" in body["message"]

    queue = client.get("/api/jobs").json()
    [osm] = [lane for lane in queue["lanes"] if lane["lane"] == "osm"]
    assert osm["name"] == "OpenStreetMap" and len(osm["waiting"]) == queue["waiting"]
    assert queue["worker"] is None
    assert client.get("/api/jobs/counts").json() == {"running": 0, "waiting": queue["waiting"]}
    batch = first.json()["batch_id"]
    mine = client.get("/api/jobs", params={"batch": batch}).json()
    assert sum(len(l["waiting"]) for l in mine["lanes"]) == first.json()["queries"]
    providers = client.get("/api/discover/providers").json()
    assert providers["running"] and providers["last_report"]["batch_id"] == second.json()["batch_id"]

    job = osm["waiting"][0]["id"]
    assert client.post(f"/api/jobs/{job}/cancel").json()["job"]["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job}/resume").status_code == 409
    assert client.post(f"/api/jobs/{job}/retry").json()["job"]["status"] == "queued"
    assert client.post("/api/lanes/osm/pause").json()["paused"]
    assert client.get("/api/jobs").json()["lanes"][0]["paused"]
    assert client.post("/api/lanes/osm/resume").json()["paused"] is False
    assert client.post(f"/api/jobs/{job}/explode").status_code == 400


def test_raising_a_run_s_cap_resumes_what_the_cap_paused(client):
    run = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"],
                                                 "max_spend": 0}).json()
    with closing(sqlite3.connect(_db(client))) as db:
        db.execute("UPDATE jobs SET status = 'paused', paused_reason = 'spend limit'")
        db.commit()
    r = client.post(f"/api/jobs/batches/{run['batch_id']}/cap", json={"max_spend": 2.5})
    assert r.json() == {"success": True, "resumed": run["queries"]}
    assert client.post(f"/api/jobs/batches/{run['batch_id']}/cap",
                       json={"max_spend": "x"}).status_code == 400


def _db(client):
    import harvey.dashboard as dash
    return str(dash.DB_PATH)


# ── What the review of the queue found, kept from coming back ──


class SlowSerper(Serper):
    """Serper's fake with a delay per page, as a real request has."""

    def __init__(self, pages, delay, on_request=None, fail=None):
        super().__init__(pages=pages, on_request=on_request)
        self.delay = delay
        self.fail = dict(fail or {})        # page -> HTTP status, once each
        self.answered: list[int] = []

    async def __call__(self, request):
        page = json.loads(request.content).get("page", 1)
        self.asked.append(page)
        if self.on_request:
            self.on_request(page)
        await asyncio.sleep(self.delay)
        if page in self.fail:
            return httpx.Response(self.fail.pop(page), json={})
        self.answered.append(page)
        organic = [] if page > self.pages else [
            {"title": f"Example Clinic {page}-{i}", "position": i,
             "link": f"https://clinic{page}x{i}.example.com/"} for i in range(1, 11)]
        return httpx.Response(200, json={"organic": organic})


@pytest.mark.asyncio
async def test_a_search_cut_off_mid_page_never_spends_past_its_cap(
        state, monkeypatch, claim_provider):
    """Killed (or cut off by the shutdown grace) while a page is in
    flight: the pages it paid for are on record, so its next try can't
    spend past the cap."""
    claim_provider(_Paged("serper_stub"))
    holder: dict = {}

    def cut_off(page):
        if page == 3 and not holder.get("cut"):
            holder["cut"] = True
            for task in holder["worker"]._tasks.values():
                task.cancel()

    server = SlowSerper(pages=6, delay=0.05, on_request=cut_off)
    routed(monkeypatch, server)
    cap = 0.004                     # four pages at $0.001
    _, (job_id,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="dentist", location="Springfield")],
        max_spend=cap)
    first = Worker(state, poll=0.05)
    holder["worker"] = first
    stop = asyncio.Event()
    task = asyncio.create_task(first.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if holder.get("cut") and first.idle():
            break
    stop.set()
    await asyncio.wait_for(task, 10)
    cut = await jobs.get(state, job_id)
    assert cut.status == "queued" and cut.actual_cost == pytest.approx(0.002)
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    paid = len(server.answered) * D.Serper.PAGE_COST
    assert paid <= cap + 1e-9, f"spent ${paid:.3f} on a ${cap:.3f} cap"
    assert job.actual_cost == pytest.approx(paid)
    assert job.status == "paused" and job.paused_reason == jobs.SPEND_LIMIT


@pytest.mark.asyncio
async def test_the_cost_so_far_is_recorded_page_by_page(state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    seen: list[float] = []

    def watch(page):
        if page > 1:
            seen.append(rows(state, "SELECT actual_cost FROM jobs")[0][0])

    routed(monkeypatch, Serper(pages=3, on_request=watch))
    await job_kinds.enqueue_discovery(state, "serper_stub", [D.DiscoveryQuery(term="dentist")],
                                      max_spend=1.0)
    await run_until_idle(state)
    # Before each further page is asked for, the pages before it are on record.
    assert seen == [pytest.approx(0.001), pytest.approx(0.002), pytest.approx(0.003)]


@pytest.mark.asyncio
async def test_a_later_page_that_fails_for_a_moment_resumes_from_that_page(
        state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    server = SlowSerper(pages=4, delay=0, fail={3: 503})
    routed(monkeypatch, server)
    _, (job_id,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="dentist")], max_spend=1.0)
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    # Pages 1-2 are kept, and the next try is to start at page 3.
    assert job.status == "queued" and job.attempts == 1 and "503" in job.error
    assert job.progress["cursor"] == {"page": 3}
    assert rows(state, "SELECT COUNT(*) FROM companies")[0][0] == 20
    with closing(sqlite3.connect(state.db_path)) as db:
        db.execute("UPDATE jobs SET not_before = NULL")
        db.commit()
    await run_until_idle(state)
    job = await jobs.get(state, job_id)
    assert job.status == "done" and server.asked == [1, 2, 3, 3, 4, 5]
    assert server.answered == [1, 2, 3, 4, 5]           # no page paid for twice
    assert job.actual_cost == pytest.approx(0.005)
    assert rows(state, "SELECT COUNT(*) FROM companies")[0][0] == 40


@pytest.mark.asyncio
async def test_a_job_never_runs_twice_when_its_workers_loop_stalls(state, kinds):
    """Synchronous work holding up the event loop past the lease's life:
    the lease is renewed from a thread, so no second worker takes over."""
    import threading

    runs: list[str] = []

    async def stalls(ctx):
        runs.append(ctx.worker.name)
        if len(runs) == 1:
            time.sleep(2.5)
        return {}

    kinds("stalls", stalls)
    await jobs.enqueue(state, "stalls", "onrc")

    def other_worker():
        async def go():
            w = Worker(StateManager(state.db_path), name="harvey-worker", lease_ttl=1.0,
                       poll=0.05)
            stop = asyncio.Event()
            t = asyncio.create_task(w.run(stop))
            await asyncio.sleep(4.0)
            stop.set()
            await t
        asyncio.run(go())

    assert await acquire_lease(state, "dashboard", ttl=1.0)
    other = threading.Thread(target=other_worker)
    other.start()
    dashboard = Worker(state, name="dashboard", lease_ttl=1.0, poll=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(dashboard.run(stop))
    await asyncio.sleep(4.5)
    stop.set()
    await asyncio.wait_for(task, 15)
    other.join(10)
    assert runs == ["dashboard"]
    assert rows(state, "SELECT status FROM jobs") == [("done",)]


@pytest.mark.asyncio
async def test_a_worker_that_lost_its_job_writes_nothing(state, kinds):
    """Fenced writes: once a job is somebody else's, the old worker's
    progress and ending are not recorded."""
    async def quiet(ctx):
        return {}

    kinds("quiet", quiet)
    job_id = await jobs.enqueue(state, "quiet", "osm")
    await jobs.claim(state, "osm", "old")
    await jobs.sweep_stale(state, "osm")
    taken = await jobs.claim(state, "osm", "new")
    assert taken.id == job_id
    assert not await jobs.save_progress(state, job_id, {"x": 1}, worker="old")
    assert not await jobs.finish(state, job_id, jobs.DONE, worker="old")
    assert await jobs.stop_requested(state, job_id, worker="old") == jobs.LOST
    assert rows(state, "SELECT status, worker FROM jobs") == [("running", "new")]


@pytest.mark.asyncio
async def test_a_registry_pull_job_doesnt_freeze_the_event_loop(state, tmp_path):
    from harvey import registry
    from tests.test_onrc import CAEN, DATASET, FIRMS, STATUS, firm, write

    n = 20000
    caen = [CAEN[0]] + [(f"J08/{i}/2020", "8623", "3") for i in range(n)]
    status = [STATUS[0]] + [(f"J08/{i}/2020", "1048", "") for i in range(n)]
    firms = [FIRMS[0]] + [firm(f"EXEMPLU {i} SRL", str(91000000 + i), f"J08/{i}/2020",
                               "SRL", "Braşov", "Municipiul Braşov") for i in range(n)]
    folder = tmp_path / "many"
    folder.mkdir()
    raw = {"caen": write(folder / "c.csv", caen), "status": write(folder / "s.csv", status),
           "firms": write(folder / "f.csv", firms)}
    path = tmp_path / "many.db"
    registry.build(raw, DATASET, path)
    await jobs.enqueue(state, "registry_pull", "onrc", {
        "path": str(path), "words": "8623", "with_administrators": False, "dry_run": True})
    gaps: list[float] = []

    async def ticker(stop):
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    stop = asyncio.Event()
    tick = asyncio.create_task(ticker(stop))
    await run_until_idle(state, timeout=300)
    stop.set()
    await tick
    assert rows(state, "SELECT status FROM jobs") == [("done",)]
    assert max(gaps) < 0.5, f"the event loop stalled {max(gaps):.2f}s"


@pytest.mark.asyncio
async def test_a_running_job_that_cant_stop_part_way_refuses_cancel_and_pause(state):
    job_id = await jobs.enqueue(state, "id_check", "anaf", {"company_ids": ["x"]})
    await jobs.claim(state, "anaf", "w")
    for act in (jobs.cancel, jobs.pause):
        with pytest.raises(jobs.Refused) as refused:
            await act(state, job_id)
        assert refused.value.code == "not_stoppable"
    job = await jobs.get(state, job_id)
    assert job.stop_requested == "" and job.as_dict()["stoppable"] is False
    # Waiting, it can still be cancelled; a search can be asked to stop running.
    waiting = await jobs.enqueue(state, "id_check", "anaf", {"company_ids": ["y"]},
                                 dedupe_key="other")
    assert (await jobs.cancel(state, waiting)).status == "cancelled"
    assert jobs.stoppable("discover") and not jobs.stoppable("registry_load")


@pytest.mark.asyncio
async def test_cancel_lands_when_a_worker_claims_the_job_meanwhile(state, monkeypatch):
    job_id = await jobs.enqueue(state, "discover", "osm")
    real_get = jobs.get
    seen = {"n": 0}

    async def stale_then_real(st, jid):
        job = await real_get(st, jid)
        seen["n"] += 1
        if seen["n"] == 1:
            # Read as waiting, then claimed before the update.
            await jobs.claim(state, "osm", "w")
        return job

    monkeypatch.setattr(jobs, "get", stale_then_real)
    job = await jobs.cancel(state, job_id)
    assert job.status == "running" and job.stop_requested == jobs.STOP_CANCEL


@pytest.mark.asyncio
async def test_a_commands_worker_doesnt_hold_back_other_lanes(state, kinds):
    ran: list[str] = []
    release = asyncio.Event()

    async def long_pull(ctx):
        await release.wait()
        ran.append("pull")
        return {}

    async def site(ctx):
        ran.append("site")
        return {}

    kinds("long_pull", long_pull)
    kinds("site", site)
    batch = await jobs.new_batch(state, "registry_pull")
    await jobs.enqueue(state, "long_pull", "onrc", batch_id=batch)
    await jobs.enqueue(state, "site", "website")          # queued from the dashboard
    cli = Worker(state, name="cli", batch_ids={batch}, wait_for_lease=False, poll=0.05)
    dash = Worker(state, name="dashboard", poll=0.05)
    s1, s2 = asyncio.Event(), asyncio.Event()
    t1 = asyncio.create_task(cli.run(s1))
    await asyncio.sleep(0.2)
    t2 = asyncio.create_task(dash.run(s2))
    for _ in range(60):
        await asyncio.sleep(0.05)
        if "site" in ran:
            break
    assert ran == ["site"], "the website lane waited for an unrelated registry pull"
    release.set()
    for _ in range(60):
        await asyncio.sleep(0.05)
        if "pull" in ran:
            break
    s1.set()
    s2.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), 15)
    assert ran == ["site", "pull"]
    assert rows(state, "SELECT status, worker FROM jobs") == [("done", ""), ("done", "")]


@pytest.mark.asyncio
async def test_a_busy_database_never_leaves_a_job_marked_running(state, kinds, monkeypatch):
    async def quick(ctx):
        return {"ok": True}

    kinds("quick", quick)
    await jobs.enqueue(state, "quick", "osm")
    real = jobs.finish
    calls = {"n": 0}

    async def locked_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(*a, **k)

    monkeypatch.setattr(jobs, "finish", locked_once)
    worker = Worker(state)
    await worker.run_job(await jobs.claim(state, "osm", worker.name), {})
    assert rows(state, "SELECT status FROM jobs") == [("done",)]
    assert jobs.is_transient(sqlite3.OperationalError("database is locked"))
    assert not jobs.is_transient(sqlite3.OperationalError("no such table: jobs"))


@pytest.mark.asyncio
async def test_waiting_for_an_allowance_doesnt_use_up_the_retries(state, kinds):
    calls = []

    async def hourly_then_flaky(ctx):
        calls.append(ctx.job.attempts)
        if len(calls) <= 4:
            raise jobs.LaneExhausted("used up this hour", retry_after=600)
        raise httpx.ConnectError("no route")

    kinds("hf", hourly_then_flaky)
    await jobs.enqueue(state, "hf", "tinyfish")
    worker = Worker(state)
    for _ in range(5):
        with closing(sqlite3.connect(state.db_path)) as db:
            db.execute("UPDATE jobs SET not_before = NULL")
            db.commit()
        await worker.run_job(await jobs.claim(state, "tinyfish", worker.name), {})
    assert calls == [0, 0, 0, 0, 0]
    assert rows(state, "SELECT status, attempts FROM jobs") == [("queued", 1)]


@pytest.mark.asyncio
async def test_resuming_clears_the_old_reason(state):
    job_id = await jobs.enqueue(state, "discover", "osm")
    with closing(sqlite3.connect(state.db_path)) as db:
        db.execute("UPDATE jobs SET status = 'paused', paused_reason = 'spend limit', "
                   "error = 'the run''s spend limit is reached'")
        db.commit()
    job = await jobs.resume(state, job_id)
    assert job.status == "queued" and job.error == "" and job.paused_reason == ""


@pytest.mark.asyncio
async def test_the_day_s_spend_limit_holds_across_runs(state, monkeypatch, claim_provider):
    claim_provider(_Paged("serper_stub"))
    server = Serper(pages=5)
    routed(monkeypatch, server)
    monkeypatch.setattr(job_kinds, "_config", lambda: SimpleNamespace(
        spend=SimpleNamespace(per_day_usd=0.003, per_month_usd=None)))
    # Two runs, each with plenty of room under its own cap.
    _, (a,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="dentist", location="Brașov")],
        max_spend=1.0)
    _, (b,), _ = await job_kinds.enqueue_discovery(
        state, "serper_stub", [D.DiscoveryQuery(term="dentist", location="Sibiu")],
        max_spend=1.0)
    await run_until_idle(state)
    first, second = await jobs.get(state, a), await jobs.get(state, b)
    assert first.actual_cost + second.actual_cost == pytest.approx(0.003)
    assert len(server.asked) == 3
    assert second.status == "paused" and "for the day" in second.error


def test_spend_limits_must_be_dollars():
    from pydantic import ValidationError

    from harvey.config import SpendConfig

    assert SpendConfig(per_day_usd=5).per_day_usd == 5
    for bad in (float("inf"), float("nan"), -1):
        with pytest.raises(ValidationError):
            SpendConfig(per_month_usd=bad)


@pytest.mark.asyncio
async def test_site_reads_look_up_only_the_companies_a_search_found(state):
    with closing(sqlite3.connect(state.db_path)) as db:
        for i in range(5):
            db.execute("INSERT INTO companies (id, name, domain) VALUES (?, ?, ?)",
                       (f"c{i}", f"Example {i}", f"example{i}.example.com"))
        db.commit()
    found = await state.companies_needing_profile(limit=-1, ids=["c1", "c3", "nope"])
    assert sorted(c["id"] for c in found) == ["c1", "c3"]
    assert await state.companies_needing_profile(limit=-1, ids=[]) == []
    assert len(await state.companies_needing_profile(limit=-1)) == 5
    ids, _ = await REAL_SITE_READS(state, ["c3"])
    assert rows(state, "SELECT params_json FROM jobs") == [('{"company_id": "c3"}',)]


@pytest.mark.asyncio
async def test_harvey_run_hosts_a_worker_for_the_jobs_it_queues(state, kinds, monkeypatch):
    import harvey.main as main_mod

    async def note(ctx):
        return {"ok": True}

    kinds("note", note)
    await jobs.enqueue(state, "note", "contacts")
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    stop = asyncio.Event()
    task = asyncio.create_task(main_mod._run_worker(stop))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if rows(state, "SELECT status FROM jobs") == [("done",)]:
            break
    stop.set()
    await asyncio.wait_for(task, 15)
    assert rows(state, "SELECT status FROM jobs") == [("done",)]


def test_stop_on_descopera_stops_only_its_own_run(client):
    mine = client.post("/api/discover/run", json={"provider": "osm",
                                                  "cities": ["Brașov", "Sibiu"]}).json()
    other = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Cluj"]}).json()
    with closing(sqlite3.connect(_db(client))) as db:
        # One of this run's searches paused at its cap: Stop leaves it be.
        db.execute("UPDATE jobs SET status = 'paused', paused_reason = 'spend limit' "
                   "WHERE rowid = (SELECT MIN(rowid) FROM jobs WHERE batch_id = ?)",
                   (mine["batch_id"],))
        db.commit()
    assert client.post("/api/discover/stop", json={}).status_code == 400
    r = client.post("/api/discover/stop", json={"batch_id": mine["batch_id"]}).json()
    assert r["success"] and r["cancelled"] == mine["queries"] - 1
    with closing(sqlite3.connect(_db(client))) as db:
        got = dict(db.execute("SELECT batch_id || ':' || status, COUNT(*) FROM jobs "
                              "GROUP BY 1").fetchall())
    assert got == {f"{mine['batch_id']}:paused": 1,
                   f"{mine['batch_id']}:cancelled": mine["queries"] - 1,
                   f"{other['batch_id']}:queued": other["queries"]}


def test_the_queue_page_refuses_odd_lanes_and_caps(client):
    run = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"]}).json()
    assert client.post("/api/lanes/" + "x" * 300 + "/pause").status_code == 404
    assert client.post("/api/lanes/website/pause").json()["paused"]      # a module's lane
    for bad in ("inf", "nan", -1):
        assert client.post(f"/api/jobs/batches/{run['batch_id']}/cap",
                           json={"max_spend": bad}).status_code == 400
    assert client.post("/api/discover/run", json={"provider": "osm", "cities": ["Sibiu"],
                                                  "max_spend": "inf"}).status_code == 400


def test_the_queue_lists_waiting_jobs_in_the_order_they_run(client):
    client.post("/api/discover/run", json={"provider": "osm",
                                           "cities": [f"Town {i}" for i in range(6)]})
    with closing(sqlite3.connect(_db(client))) as db:
        # Queued in the same second: the order is the queue's, not the clock's.
        db.execute("UPDATE jobs SET created_at = '2026-10-01 10:00:00'")
        db.commit()
        order = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY rowid")]
    [osm] = [l for l in client.get("/api/jobs").json()["lanes"] if l["lane"] == "osm"]
    assert [j["id"] for j in osm["waiting"]] == order[:50]
    assert osm["waiting_more"] == max(0, len(order) - 50)


def test_a_duplicate_says_which_job_in_the_reader_s_language(client):
    client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"]})
    body = client.post("/api/discover/run", json={"provider": "osm", "cities": ["Brașov"]}).json()
    assert body["code"] == "duplicate" and body["job"]["kind"] == "discover"


def test_the_queue_page_under_node():
    """Coadă: a running load offers no Cancel or Pause, a running search
    does; lane names and a duplicate are said in Romanian; the 5-second
    refresh leaves a cap someone is typing alone."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parent.parent / "harvey" / "web"
    app = (web / "app.js").read_text(encoding="utf-8")

    def fn(name):
        start = app.index("function " + name + "(")
        if app[max(0, start - 6):start] == "async ":
            start -= 6
        depth, i = 0, app.index("{", app.index(")", start))
        while True:
            depth += {"{": 1, "}": -1}.get(app[i], 0)
            i += 1
            if depth == 0:
                return app[start:i]

    tone = re.search(r"const JOB_TONE = \{.*?\};", app, re.S).group(0)
    parts = [fn(n) for n in ("escHtml", "jsq", "icon", "own", "tServer", "moduleName", "failMsg",
                             "jobBadge", "jobKind", "jobRow", "laneName", "laneCard", "loadQueue")]
    lanes = [{"lane": "website", "name": "Website reader", "paused": "", "counts": {},
              "running": [{"id": "j1", "kind": "site_read", "status": "running",
                           "stoppable": False, "progress": {}, "label": "a.example.com"}],
              "waiting": [], "paused_jobs": [], "done": [], "failed": []},
             {"lane": "osm", "name": "OpenStreetMap", "paused": "", "counts": {},
              "running": [{"id": "j2", "kind": "discover", "status": "running",
                           "stoppable": True, "progress": {}, "label": "dentist"}],
              "waiting": [], "paused_jobs": [], "done": [], "failed": []}]
    script = (
        f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(web / 'i18n.js'))}, 'utf8'));"
        f"require('vm').runInThisContext({json.dumps(tone + chr(10) + chr(10).join(parts))});"
        "setLang('ro');"
        "var _queueBatch = 'b1';"
        "var els = {'queue-list': {innerHTML: ''}, 'queue-batch': {innerHTML: 'TYPED'},"
        "           'queue-cap': {dataset: {edited: '1'}}};"
        "var document = {getElementById: id => els[id] || null, activeElement: null};"
        "function navCount() {} function emptyState() { return 'E'; } function offlineState() { return 'O'; }"
        f"async function api() {{ return {{lanes: {json.dumps(lanes)}, running: 2, waiting: 0,"
        "  worker: {pid: 1}, batch: {label: 'x', max_spend: 1}}; }"
        "(async () => {"
        "  await loadQueue(); const out = {list: els['queue-list'].innerHTML, head: els['queue-batch'].innerHTML};"
        "  delete els['queue-cap'].dataset.edited; await loadQueue(); out.head2 = els['queue-batch'].innerHTML;"
        "  out.dup = failMsg({code: 'duplicate', job: {kind: 'discover', label: 'dentist', status: 'queued'}}, 'x');"
        "  process.stdout.write(JSON.stringify(out)); })();")
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    encoding="utf-8", check=True, timeout=30).stdout)
    website, osm = out["list"].split('class="card lane-card"')[1:]
    assert "Cititorul de site" in website and "jobAction(" not in website
    assert "jobAction(&quot;j2&quot;,&quot;cancel&quot;)" in osm
    assert "jobAction(&quot;j2&quot;,&quot;pause&quot;)" in osm
    assert out["head"] == "TYPED"                       # left alone while edited
    assert 'id="queue-cap"' in out["head2"]               # refreshed once it isn't
    assert out["dup"].startswith("Aceeași lucrare așteaptă deja") and "dentist" in out["dup"]


def test_harvey_jobs_refuses_an_odd_lane_and_says_when_nothing_runs_a_job(
        monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    import harvey.state as state_mod
    from harvey.cli import cmd_jobs

    db = str(tmp_path / "harvey.db")
    store = StateManager(db)
    asyncio.run(store.init_db())
    job_id = asyncio.run(jobs.enqueue(store, "discover", "osm"))
    asyncio.run(jobs.pause(store, job_id))
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: StateManager(db))

    def run(action, target, **kw):
        cmd_jobs(Namespace(jobs_action=action, target=target, max_spend=None, limit=50, **kw))
        return capsys.readouterr().out

    with pytest.raises(SystemExit):
        run("pause", "no-such-lane")
    assert "No lane 'no-such-lane'" in capsys.readouterr().out
    said = run("resume", job_id)
    assert "resumed" in said and "No worker is running" in said
