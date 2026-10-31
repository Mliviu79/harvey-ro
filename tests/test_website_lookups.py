"""Finding websites for companies with none: Brave Search as a website
finder only (never a Discover source, its results never kept), the search
modules' free allowances counted by Harvey and shared with the background,
a Discover run's "find websites" amount inside its batch, and a person's
name tried in both orders. Every server and verifier is a fake; all data is
fictional."""

import asyncio
import json
import logging
import sqlite3

import httpx
import pytest

from harvey import contacts, finder
from harvey.collectors import discover as D
from harvey.models import Company
from harvey.modules import api, ingest, job_kinds, jobs, lanes, switches
from harvey.modules.original.brave import provider as brave
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

KEY = {"brave_api_key": "test-brave-key"}
# What the fake Brave answers: one result is the company's site, the rest
# aren't; none of it but the chosen site may be kept anywhere.
RESULTS = [
    {"url": "https://www.alfa-dent.example/contact", "title": "Alfa Dent Clinic TITLE-KEPT-NOWHERE",
     "description": "SNIPPET-KEPT-NOWHERE one", "extra_snippets": ["EXTRA-KEPT-NOWHERE"]},
    {"url": "https://zeta-portal.example/listing/42", "title": "Zeta Portal OTHER-TITLE",
     "description": "SNIPPET-KEPT-NOWHERE two"},
]
SECRETS = ("TITLE-KEPT-NOWHERE", "SNIPPET-KEPT-NOWHERE", "EXTRA-KEPT-NOWHERE", "OTHER-TITLE",
           "zeta-portal")


def run(coro):
    return asyncio.run(coro)


async def _state(path) -> StateManager:
    s = StateManager(str(path))
    await s.init_db()
    await sync_vocabulary(s)
    return s


def _rows(path, sql, args=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def _everything(path) -> str:
    """Every value in every table of the database, as one text."""
    out = []
    with sqlite3.connect(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for t in tables:
            for row in conn.execute(f'SELECT * FROM "{t}"'):
                out.append(" ".join(str(v) for v in row))
    return "\n".join(out)


class FakeBrave:
    """Brave's web search endpoint: answers each request in turn from
    ``answers`` (status, json, headers), the last one again when they run
    out, and remembers each request."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(200, {"web": {"results": RESULTS}}, {})]
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.brave.com"
        assert request.url.path == "/res/v1/web/search"
        self.requests.append(request)
        status, body, headers = self.answers[min(len(self.requests), len(self.answers)) - 1]
        return httpx.Response(status, json=body, headers=headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def install(self, monkeypatch):
        """Every client Harvey opens talks to this fake (the finder opens its own)."""
        real = httpx.AsyncClient

        def make(*a, **kw):
            kw["transport"] = httpx.MockTransport(self.handler)
            return real(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", make)
        return self


@pytest.fixture(autouse=True)
def _no_pace(monkeypatch):
    """Brave's lane is a second between searches: the tests record each
    turn instead of waiting for it."""
    turns = []

    async def wait(lane):
        lanes.interval(lane)        # an undeclared lane is still refused
        turns.append(lane)

    monkeypatch.setattr(lanes, "wait", wait)
    return turns


async def _fetch(fake, query=None, budget=float("inf")):
    p = brave.Brave()
    p.remaining_budget, p.last_cost, p.pages = budget, 0.0, 0
    async with fake.client() as client:
        found = await p.fetch(client, KEY, query or D.DiscoveryQuery(
            term="ALFA DENT SRL", location="Brașov", depth=10, limit=10))
    return p, found


# ── The module and its provider ──


def test_brave_is_a_website_finder_declared_like_any_search_module():
    m = switches.by_id()["brave"]
    assert m.kind == "search" and m.secrets == ("BRAVE_API_KEY",)
    cap = next(c for c in m.capabilities if c["id"] == "website_search")
    assert (cap["op"], cap["provider"], cap["lookup"], cap["lane"]) == (
        "lookup", "brave", "website", "brave")
    assert lanes.interval("brave") == 1.0
    assert [dict(a) for a in m.free_allowances] == [{"per": "month", "requests": 1000}]
    assert D.PROVIDERS["brave"].estimate([D.DiscoveryQuery(term="x")]) == 0.005
    # The contact finder can search through it once its key is set.
    assert "brave" in [s.module for s in finder.searchers({"brave"}, KEY)]
    assert finder.searchers({"brave"}, {}) == []


def test_a_lookup_asks_for_the_name_and_town_with_the_key(_no_pace):
    fake = FakeBrave()
    p, found = run(_fetch(fake))
    (req,) = fake.requests
    assert req.headers["X-Subscription-Token"] == "test-brave-key"
    assert req.headers["Accept"] == "application/json"
    assert req.url.params["q"] == "ALFA DENT SRL Brașov" and req.url.params["count"] == "10"
    assert [b.domain for b in found] == ["alfa-dent.example", "zeta-portal.example"]
    assert p.last_cost == 0.005 and p.pages == 1
    assert _no_pace == ["brave"]            # one turn on its lane per search


def test_a_short_rate_limit_is_waited_out_and_a_spent_window_stops(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(brave.asyncio, "sleep", sleep)
    second = {"X-RateLimit-Remaining": "0, 900", "X-RateLimit-Reset": "1, 86000"}
    fake = FakeBrave((429, {}, second), (200, {"web": {"results": RESULTS}}, {}))
    p, found = run(_fetch(fake))
    assert len(fake.requests) == 2 and len(found) == 2 and slept == [1.0]

    month = {"X-RateLimit-Remaining": "1, 0", "X-RateLimit-Reset": "1, 86400"}
    with pytest.raises(D.ProviderExhausted) as e:
        run(_fetch(FakeBrave((429, {}, month))))
    assert e.value.retry_after == 86400


@pytest.mark.parametrize("status,body,says", [
    (402, {"error": {"code": "OPTION_NOT_IN_PLAN"}}, "no credit left"),
    (401, {}, "refused the key"),
    (422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "status": 422}}, "refused the key"),
])
def test_out_of_credit_or_a_refused_key_stops_the_source(status, body, says):
    with pytest.raises(D.ProviderExhausted, match=says):
        run(_fetch(FakeBrave((status, body, {}))))


def test_a_search_the_budget_cant_pay_for_isnt_made():
    fake = FakeBrave()
    p, found = run(_fetch(fake, budget=0.001))
    assert found == [] and fake.requests == [] and p.stop_reason == "budget reached"


def test_brave_is_never_a_discover_source():
    assert not D.is_discovery_source("brave") and D.is_discovery_source("tinyfish")
    menu = {m["key"]: m for m in D.provider_menu(KEY)}
    assert menu["brave"]["lookup_only"] and menu["brave"]["website_lookup"]
    assert not menu["tinyfish"]["lookup_only"]
    with pytest.raises(ValueError, match="isn't a Discover source"):
        job_kinds.discovery_lane("brave")
    # Nor a preset's search.
    assert D.preset_problem("yc", "brave") is not None


# ── Free allowances: counted by Harvey, shared with the background ──


def test_every_search_counts_against_its_modules_free_allowance():
    fake = FakeBrave()

    async def go():
        async with fake.client() as client:
            return await D.search_draft("brave", D.DiscoveryQuery(term="ALFA DENT SRL", depth=10),
                                        client, KEY)

    draft = run(go())
    assert draft.failed is None and draft.cost == 0.005
    assert lanes.uses("brave") == {"month": 1}
    assert lanes.free_left("brave") == 999
    assert lanes.free_left("brave", 0.5) == 499
    # A module with no free allowance has nothing to count against.
    assert lanes.free_left("serper") is None and lanes.uses("serper") == {}
    lanes.count_uses("serper", 5)
    assert lanes.uses("serper") == {}


def test_tinyfish_shares_its_minute_and_hour_with_the_background():
    lanes.count_uses("tinyfish", 14)
    assert lanes.uses("tinyfish") == {"minute": 14, "hour": 14}
    assert lanes.free_left("tinyfish", 0.5) == 1        # half of 30 a minute
    lanes.count_uses("tinyfish")
    assert lanes.free_left("tinyfish", 0.5) == 0 and lanes.free_left("tinyfish") == 15


def test_a_period_past_starts_from_nothing(monkeypatch):
    lanes.count_uses("tinyfish", 7)
    real = lanes._window
    monkeypatch.setattr(lanes, "_window", lambda per, now: real(per, now) + "-next")
    assert lanes.uses("tinyfish") == {"minute": 0, "hour": 0}


def test_a_month_is_the_last_30_days_not_the_calendar_month(monkeypatch):
    """Brave's monthly credit renews on its own cycle, per key: a calendar
    month would let the background take its share before the 1st and
    again after it, inside one cycle."""
    import calendar

    start = calendar.timegm((2026, 11, 29, 12, 0, 0))      # two days before the 1st
    monkeypatch.setattr(lanes.time, "time", lambda: start)
    lanes.count_uses("brave", 499)
    assert lanes.free_left("brave", 0.5) == 1
    for days, used in ((3, 499), (29, 499), (31, 0)):        # Dec 2, Dec 28, Dec 30
        monkeypatch.setattr(lanes.time, "time", lambda d=days: start + d * 86400)
        assert lanes.uses("brave") == {"month": used}, days
    # A day added later is kept beside those still inside the window.
    monkeypatch.setattr(lanes.time, "time", lambda: start + 10 * 86400)
    lanes.count_uses("brave", 1)
    assert lanes.uses("brave") == {"month": 500} and lanes.free_left("brave", 0.5) == 0


async def _company(s, **kw):
    kw.setdefault("source", "registry:onrc")
    return await s.add_company(Company(**kw))


def _site_reader(monkeypatch, read):
    real = api.capability

    async def read_site(inp, ctx):
        read.append(inp.domain)
        return api.Draft(pages=[api.Page(f"https://{inp.domain}/",
                                         "<p>Alfa Dent, str. Exemplu 1, Brașov</p>")],
                         contacts=[api.Contact("email", f"office@{inp.domain}",
                                               source_url=f"https://{inp.domain}/",
                                               module="website", capability="read_site")])

    def capability(module_id, cap_id, manifests=None):
        if (module_id, cap_id) == ("website", "read_site"):
            return api.Capability(module_id, cap_id, read_site)
        return real(module_id, cap_id, manifests)

    monkeypatch.setattr(api, "capability", capability)


def test_the_background_looks_up_inside_the_free_allowance_and_keeps_only_the_site(
        tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = tmp_path / "harvey.db"
    fake = FakeBrave().install(monkeypatch)
    read = []
    _site_reader(monkeypatch, read)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", location="Brașov, BV")
        # No day's budget: only a free allowance can pay.
        found = await finder.find(s, cid, spend=finder.Spend(cap=False), env=KEY,
                                  background=True)
        return cid, found

    cid, found = run(go())
    assert found.steps[:2] == ["search:brave", "site"] and found.cost == 0.0
    assert found.website == "alfa-dent.example" and read == ["alfa-dent.example"]
    assert len(fake.requests) == 1 and lanes.uses("brave") == {"month": 1}
    # The search is on record, free, naming only the site it chose.
    assert _rows(db, "SELECT module, capability, outcome, cost, detail FROM searches "
                     "WHERE looked_for = 'website'") == [
        ("brave", "website_search", "found", 0.0, "alfa-dent.example")]
    assert _rows(db, "SELECT website_trust FROM companies WHERE id = ?", (cid,)) == [("likely",)]
    # Nothing else from the results is kept: not in the database, not in the log.
    everything = _everything(db)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in SECRETS:
        assert secret not in everything, secret
        assert secret not in logged, secret
    assert "office@alfa-dent.example" in everything


def test_the_background_stops_when_its_share_is_used_or_it_is_off(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fake = FakeBrave().install(monkeypatch)
    _site_reader(monkeypatch, [])

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", location="Brașov")
        lanes.count_uses("brave", 500)          # half the month's 1,000: the background's
        used = await finder.find(s, cid, spend=finder.Spend(cap=False), env=KEY,
                                 background=True)
        await switches.set_module_setting(s, "contact_finder", "background_share", 1.0)
        await switches.set_module_setting(s, "contact_finder", "background_search", False)
        off = await finder.find(s, cid, spend=finder.Spend(cap=False), env=KEY,
                                background=True)
        await switches.set_module_setting(s, "contact_finder", "background_search", True)
        status = await finder.background_status(s)
        return used, off, status

    monkeypatch.setattr(finder, "_env", lambda: KEY)
    used, off, status = run(go())
    assert fake.requests == []
    assert "brave: no free searches left for the background for now" in used.skipped
    assert "brave: no free searches left for the background for now" in off.skipped
    (row,) = status["searchers"]
    assert status["on"] and status["share"] == 1.0
    assert row["module"] == "brave" and row["used"] == {"month": 500} and row["left"] == 500


def test_a_persons_run_pays_brave_from_its_cap(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    FakeBrave().install(monkeypatch)
    _site_reader(monkeypatch, [])
    reserved, recorded = [], []

    async def reserve(estimate, first, **kw):
        reserved.append(estimate)
        return 1.0

    async def record(amount):
        recorded.append(amount)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", location="Brașov")
        return await finder.find(s, cid, spend=finder.Spend(reserve, record), env=KEY)

    found = run(go())
    assert reserved == [0.005] and recorded == [0.005] and found.cost == 0.005


def test_the_backlog_queues_companies_with_no_website_for_a_free_allowance(tmp_path,
                                                                           monkeypatch):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(job_kinds, "_config", lambda: None)       # no day's budget
    monkeypatch.setattr(finder, "_env", lambda: KEY)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", location="Brașov")
        on = await job_kinds.enqueue_backlog(s)
        async with s._connect() as conn:
            await conn.execute("DELETE FROM jobs")
            await conn.commit()
        await switches.set_module_setting(s, "contact_finder", "background_search", False)
        job_kinds._backlog_idle.clear()
        off = await job_kinds.enqueue_backlog(s)
        return cid, on, off

    cid, on, off = run(go())
    assert (on, off) == (1, 0)


# ── A Discover run's "find websites" ──


def test_find_websites_is_its_own_amount_inside_the_runs_batch(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(finder, "_env", lambda: {"serper_api_key": "k"})

    async def go():
        s = await _state(db)
        q = [D.DiscoveryQuery(term="dentist", location="Brașov")]
        batch_id, ids, _ = await job_kinds.enqueue_sources(
            s, [{"provider": "serper", "queries": q}], max_spend=1.0, find_websites=0.5)
        a = await _company(s, name="ALFA DENT SRL", location="Brașov")
        b = await _company(s, name="BETA DENT SRL", location="Brașov", domain="beta.example")
        queued = await job_kinds.enqueue_finder_follow_ups(s, [a, b], batch_id=batch_id)
        search = await jobs.get(s, ids[0])
        website = next(j for j in await jobs.list_jobs(s, batch_id=batch_id)
                       if j.params.get("pool") == "websites")
        # The searches spent their dollar: none of the websites' amount is theirs.
        async with s._connect() as conn:
            await conn.execute("UPDATE jobs SET actual_cost = 1.0, status = 'done' WHERE id = ?",
                               (search.id,))
            await conn.commit()
        more = (await jobs.enqueue(s, "discover", "serper", {"pool": "search"},
                                   batch_id=batch_id))
        try:
            await jobs.reserve(s, await jobs.get(s, more), 0.01, 0.01)
            refused = ""
        except jobs.SpendLimit as e:
            refused = str(e)
        room = await jobs.reserve(s, website, 0.005, 0.005)
        return batch_id, queued, website, refused, room, a

    batch_id, queued, website, refused, room, a = run(go())
    assert _rows(db, "SELECT max_spend, json_extract(params_json, '$.pools') FROM job_batches") \
        == [(1.5, json.dumps({"search": 1.0, "websites": 0.5}, separators=(",", ":")))]
    assert queued == 2      # beta's site read, alfa's website lookup
    assert website.params == {"company_id": a, "website": True, "pool": "websites"}
    assert "search" in refused and room == pytest.approx(0.5)


def test_find_websites_can_be_left_off():
    async def go(db):
        s = await _state(db)
        q = [D.DiscoveryQuery(term="dentist", location="Brașov")]
        batch_id, _, _ = await job_kinds.enqueue_sources(
            s, [{"provider": "serper", "queries": q}], max_spend=1.0)
        a = await _company(s, name="ALFA DENT SRL", location="Brașov")
        return await job_kinds.enqueue_finder_follow_ups(s, [a], batch_id=batch_id)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        assert run(go(Path(d) / "harvey.db")) == 0


def test_a_website_lookup_from_discover_runs_with_a_phone_on_file_and_checks_no_address(
        tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    FakeBrave().install(monkeypatch)
    _site_reader(monkeypatch, [])

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", location="Brașov", phone="+40268000111")
        await s.add_contacts(cid, [contacts.Found("phone", "+40268000111", source="anaf")])
        plain = await finder.find(s, cid, spend=finder.Spend(cap=False), env=KEY)
        wanted = await finder.find(s, cid, spend=finder.Spend(cap=False), env=KEY,
                                   want_website=True, background=True, addresses=False)
        return plain, wanted

    plain, wanted = run(go())
    assert plain.steps == [] and wanted.website == "alfa-dent.example"
    assert "address" not in wanted.steps


# ── The dashboard ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    from harvey.config import EnvConfig

    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    monkeypatch.setattr("harvey.config.load_env",
                        lambda *a, **k: EnvConfig(serper_api_key="k", brave_api_key="k"))
    monkeypatch.setattr(dash, "_discovery_queries",
                        lambda body, config, provider="": [D.DiscoveryQuery(term="dentist",
                                                                             location="Iasi")])
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def test_the_estimate_and_the_run_carry_find_websites(client):
    est = client.post("/api/discover/estimate", json={"providers": ["serper"],
                                                      "websites_max_spend": 0.25}).json()
    w = est["find_websites"]
    assert w["enabled"] and w["max_spend"] == 0.25 and "brave" in w["paid"]
    assert w["per_lookup"] == 0.001 and w["paid_lookups"] == 250     # Serper, the cheapest
    off = client.post("/api/discover/estimate", json={"providers": ["serper"],
                                                      "find_websites": False}).json()
    assert off["find_websites"] == {"enabled": False}
    bad = client.post("/api/discover/run", json={"providers": ["serper"],
                                                 "websites_max_spend": -1})
    assert bad.status_code == 400
    ok = client.post("/api/discover/run", json={"providers": ["serper"], "max_spend": 2,
                                                "websites_max_spend": 0.5})
    assert ok.status_code == 200, ok.text
    assert _rows(client.db_path, "SELECT max_spend FROM job_batches") == [(2.5,)]
    summary = client.get("/api/discover/providers").json()["last_report"]
    assert summary["search_max_spend"] == 2.0
    assert summary["websites"] == {"max_spend": 0.5, "spent": 0.0, "looked_up": 0, "found": 0,
                                   "waiting": 0, "left_to_background": 0}


def test_brave_shows_in_the_menu_but_isnt_runnable_from_discover(client):
    providers = {p["key"]: p for p in client.get("/api/discover/providers").json()["providers"]}
    assert providers["brave"]["lookup_only"] and providers["brave"]["configured"]
    r = client.post("/api/discover/estimate", json={"providers": ["brave"]})
    assert r.status_code == 400 and "isn't a Discover source" in r.json()["error"]
    r = client.post("/api/discover/run", json={"providers": ["brave"]})
    assert r.status_code == 400


def test_the_background_switch_and_share_from_settings(client):
    r = client.post("/api/contacts/finder/settings", json={"background_search": False,
                                                           "background_share": 0.25})
    assert r.status_code == 200 and r.json()["on"] is False and r.json()["share"] == 0.25
    status = client.get("/api/contacts/finder").json()["background_search"]
    assert status["on"] is False and status["share"] == 0.25
    assert client.post("/api/contacts/finder/settings",
                       json={"background_share": 2}).status_code == 400
    assert client.post("/api/contacts/finder/settings", json={"x": 1}).status_code == 400


# ── A person's name, in either order ──


def test_the_market_pack_says_which_order_is_likelier_core_tries_both():
    assert contacts.name_order("onrc") == "family_first"        # market_ro's hint
    assert contacts.name_order("registry") == "family_first"    # its collector
    assert contacts.name_order("website") == "given_first"
    assert contacts.name_orders("POPESCU ION MIHAI", "onrc") == [("Ion", "Popescu"),
                                                                 ("Popescu", "Mihai")]
    assert contacts.name_orders("Ana-Maria Exemplu", "website") == [("Ana-Maria", "Exemplu"),
                                                                    ("Exemplu", "Ana-Maria")]
    assert contacts.name_orders("IONESCU ANA-MARIA", "onrc")[0] == ("Ana-Maria", "Ionescu")
    assert contacts.name_orders("Ana", "onrc") == []


def _verifier(monkeypatch, answers):
    """The address check: the status for each (first, last) asked, in turn."""
    asked = []
    real = api.capability

    async def find_email(inp, ctx):
        first, last = inp.extra["first_name"], inp.extra["last_name"]
        asked.append((first, last))
        address = f"{first}.{last}@alfa-dent.example".lower()
        return api.Draft(contacts=[api.Contact("email", address,
                                               status=answers.get((first, last), "invalid"))])

    def capability(module_id, cap_id, manifests=None):
        if (module_id, cap_id) == ("email_verify", "find_email"):
            return api.Capability(module_id, cap_id, find_email)
        return real(module_id, cap_id, manifests)

    monkeypatch.setattr(api, "capability", capability)
    return asked


async def _addresses(s, cid):
    out = finder.Found(cid)
    company = await finder.company_row(s, cid)
    await finder._addresses(s, company, "alfa-dent.example", out, "confirmed",
                            spend=finder.Spend(cap=True), since=None, batch_id="",
                            enabled={"email_verify"}, env={})
    return out


def test_the_other_order_is_checked_only_when_the_likelier_one_is_invalid(tmp_path,
                                                                         monkeypatch):
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {("Popescu", "Mihai"): "verified"})

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU ION MIHAI", "administrator")], source="onrc")
        out = await _addresses(s, cid)
        left = await ingest.Allowance(s).left("email_verify")
        return out, left

    out, left = run(go())
    assert asked == [("Ion", "Popescu"), ("Popescu", "Mihai")]
    assert out.contacts == 1 and left == ingest.monthly_allowance("email_verify") - 2
    # The person keeps the registry's text; the record says the order used.
    assert _rows(db, "SELECT name FROM people") == [("POPESCU ION MIHAI",)]
    assert _rows(db, "SELECT detail FROM searches WHERE target = 'person'") == [
        ("read as Popescu Mihai",)]
    assert _rows(db, "SELECT value, status FROM contacts WHERE kind = 'email'") == [
        ("popescu.mihai@alfa-dent.example", "verified")]


def test_a_verified_first_order_needs_no_second_check(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {("Ion", "Popescu"): "verified"})

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU ION", "administrator")], source="onrc")
        return await _addresses(s, cid)

    run(go())
    assert asked == [("Ion", "Popescu")]


def test_an_order_the_companys_site_confirms_is_the_only_one_checked(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {})          # every address comes back invalid

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU MIHAI ION", "administrator")], source="onrc")
        await s.add_people(cid, [("Ion Popescu", "medic")], source="website")
        return await _addresses(s, cid)

    run(go())
    # The team page writes "Ion Popescu" (the registry's likelier reading
    # is Mihai Popescu): that order, once, and the page's own mention isn't
    # checked a second time.
    assert asked == [("Ion", "Popescu")]
    details = dict(_rows(db, "SELECT p.name, s.detail FROM searches s JOIN people p "
                             "ON CAST(p.id AS TEXT) = s.target_id WHERE s.target = 'person'"))
    assert details["POPESCU MIHAI ION"] == "read as Ion Popescu (the company's site names them so)"
    assert details["Ion Popescu"] == "searched as POPESCU MIHAI ION"


# ── The pages under node ──


def test_discover_offers_find_websites_and_never_brave_as_a_source():
    import shutil
    import subprocess
    from pathlib import Path

    import harvey.dashboard as dash

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(dash.__file__).resolve().parent / "web"
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
['discover-providers', 'discover-filters', 'discover-runs', 'disc-run', 'discover-estimate',
 'disc-cities', 'disc-depth', 'disc-limit', 'disc-cap', 'disc-websites', 'disc-websites-cap',
 'disc-websites-note'].forEach(id => els[id] = {innerHTML: '', value: '', textContent: '',
                                                 disabled: false, checked: false, style: {}});
els['disc-cap'].value = '2'; els['disc-websites'].checked = true; els['disc-websites-cap'].value = '0.5';
global.document = {getElementById: id => els[id] || null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => JSON.stringify(s);
global.icon = n => '<i>' + n + '</i>';
global.safeHref = v => v;
global.tServer = (k, d) => d;
global.fmtUsd = (n, d) => '$' + Number(n).toFixed(d);
global.markLoadFailed = () => {};
global.offlineState = () => 'offline';
global.emptyState = (i, a, b) => a + b;
global.renderProfileCard = () => {};
global.renderDiscoverResult = () => {};
global.setInterval = () => 1; global.clearInterval = () => {};
global.api = async () => ({
  providers: [{key: 'tinyfish', label: 'TinyFish', configured: true, enabled: true, env_keys: [],
               blurb: 'Free web search', cost_note: 'free', free_tier: 'free', website_lookup: true},
              {key: 'brave', label: 'Brave Search', configured: true, enabled: true, env_keys: [],
               blurb: 'Website lookups only', cost_note: '$5 per 1,000 searches', free_tier: '$5 a month',
               lookup_only: true, website_lookup: true}],
  sources: [], presets: {}, default: 'tinyfish', selected_all: ['tinyfish', 'brave'], running: true,
  active_runs: [{batch_id: 'b1', label: 'TinyFish', jobs: {running: 1}, max_spend: 2.5, spent: 0.01,
                 searches_active: 1, sources: [],
                 websites: {max_spend: 0.5, spent: 0.01, looked_up: 3, found: 2, waiting: 4}}]});
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('// ── Discover: tick sources', '// Companies found but not looked at'));
vm.runInThisContext(pick("// The background's website searches inside free allowances, beside",
                         '// Settings: the background'));
(async () => {
  await loadDiscoverProviders();
  console.log(JSON.stringify({cards: els['discover-providers'].innerHTML, body: discoverBody(),
    note: els['disc-websites-note'].textContent, runs: els['discover-runs'].innerHTML,
    estimate: websitesEstimate({enabled: true, max_spend: 0.5, per_lookup: 0.005, paid_lookups: 100,
                                free: ['tinyfish'], paid: ['brave'], searchers: [{module: 'brave'}]}),
    bg: backgroundSearchLine({on: true, share: 0.5, searchers: [{module: 'brave',
      allowance: {month: 1000}, used: {month: 10}, left: 490, last_day: {searches: 10, found: 7}}]}, 42),
    off: backgroundSearchLine({on: false, searchers: []}, 42)}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    out = subprocess.run([node, "-e", script, str(web / "app.js"), str(web / "i18n.js")],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    data = json.loads(out.stdout)
    # Brave is no source card (nor kept ticked from the last run), only one of
    # the searches "find websites" goes through.
    assert "TinyFish" in data["cards"] and "Brave" not in data["cards"]
    assert data["body"]["providers"] == ["tinyfish"]
    assert data["body"]["find_websites"] is True and data["body"]["websites_max_spend"] == 0.5
    assert "Brave Search ($5 per 1,000 searches)" in data["note"]
    assert "3" in data["runs"] and "$0.50" in data["runs"]
    assert "$0.0050" in data["estimate"] and "tinyfish" in data["estimate"]
    assert "până la 100" in data["estimate"]     # an upper bound: a company may take two
    assert "10" in data["bg"] and "490" in data["bg"] and "50%" in data["bg"]
    assert data["bg"] != data["off"] and "42" not in data["off"]


# ── Regressions from review ──


def test_raising_a_discover_runs_cap_frees_its_paused_search(tmp_path):
    """The Queue page and `harvey jobs resume ID --max-spend N` raise the
    run's cap: the change goes to the searches' amount (or the pool the
    job draws from), so a search the old limit paused carries on."""
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        q = [D.DiscoveryQuery(term="dentist", location="Brasov")]
        batch_id, ids, _ = await job_kinds.enqueue_sources(
            s, [{"provider": "serper", "queries": q}], max_spend=1.0, find_websites=1.0)
        async with s._connect() as conn:
            await conn.execute("UPDATE jobs SET actual_cost = 1.0, status = 'done' WHERE id = ?",
                               (ids[0],))
            await conn.commit()
        more = await jobs.enqueue(s, "discover", "serper", {"pool": "search"},
                                  batch_id=batch_id)
        await jobs.set_batch_cap(s, batch_id, 10.0)
        search = await jobs.reserve(s, await jobs.get(s, more), 0.01, 0.01)
        after_search = (await jobs.get_batch(s, batch_id))["params"]["pools"]
        await jobs.set_batch_cap(s, batch_id, 12.0, pool="websites")
        return search, after_search, (await jobs.get_batch(s, batch_id))["params"]["pools"]

    search, after_search, after_websites = run(go())
    assert search == pytest.approx(8.0)          # $9 for searches, $1 of it spent
    assert after_search == {"search": 9.0, "websites": 1.0}
    assert after_websites == {"search": 9.0, "websites": 3.0}


def test_a_different_person_on_the_site_doesnt_lend_the_admin_their_mailbox(
        tmp_path, monkeypatch):
    """POPESCU ION MIHAI (family name first) and a team page's "Ion Mihai"
    share two words, but not the family name: two people, each searched
    under their own name."""
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {("Ion", "Mihai"): "verified"})

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU ION MIHAI", "administrator")], source="onrc")
        await s.add_people(cid, [("Ion Mihai", "medic")], source="website")
        await _addresses(s, cid)

    run(go())
    assert asked[0] == ("Ion", "Popescu") and ("Ion", "Mihai") in asked
    linked = dict(_rows(db, "SELECT ct.value, p.name FROM company_contacts cc JOIN people p "
                            "ON p.id = cc.person_id JOIN contacts ct ON ct.id = cc.contact_id "
                            "WHERE ct.value = 'ion.mihai@alfa-dent.example'"))
    assert linked == {"ion.mihai@alfa-dent.example": "Ion Mihai"}


def test_two_registry_people_a_site_name_fits_share_no_mailbox(tmp_path, monkeypatch):
    """Father and son POPESCU ION and POPESCU ION MIHAI; the site names
    "Ion Popescu". The match isn't one to one, so it confirms neither; and
    the address ion.popescu@ is checked once, for one person only."""
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {("Ion", "Popescu"): "verified"})

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU ION", "administrator"),
                                 ("POPESCU ION MIHAI", "administrator")], source="onrc")
        await s.add_people(cid, [("Ion Popescu", "medic")], source="website")
        await _addresses(s, cid)

    run(go())
    assert asked.count(("Ion", "Popescu")) == 1
    owners = _rows(db, "SELECT p.name FROM company_contacts cc JOIN people p "
                       "ON p.id = cc.person_id JOIN contacts ct ON ct.id = cc.contact_id "
                       "WHERE ct.value = 'ion.popescu@alfa-dent.example'")
    assert owners == [("POPESCU ION",)]
    details = dict(_rows(db, "SELECT p.name, s.detail FROM searches s JOIN people p "
                             "ON CAST(p.id AS TEXT) = s.target_id WHERE s.target = 'person'"))
    assert "site names them so" not in " ".join(details.values())
    # The site's own reading is the one already checked: it isn't swapped
    # for the other order (popescu.ion@), which is tried only after an
    # invalid answer.
    assert asked == [("Ion", "Popescu")]
    assert details["Ion Popescu"].startswith("read the same as POPESCU ION:")
    assert details["POPESCU ION MIHAI"].startswith("read the same as POPESCU ION:")


@pytest.mark.parametrize("source", ["onrc", "registry", "registry:onrc"])
def test_a_registry_records_source_reads_family_name_first(source):
    """A registry record's own source ("registry:onrc") names its module."""
    assert contacts.name_order(source) == "family_first"
    assert contacts.split_name("POPESCU ION", source) == ("Ion", "Popescu")


def test_a_reading_taken_by_someone_else_is_moved_past_only_when_invalid(tmp_path,
                                                                        monkeypatch):
    """Every address comes back invalid: POPESCU ION MIHAI moves past
    ion.popescu@ (invalid for POPESCU ION) to his own other reading, and no
    reading is checked twice."""
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {})          # every address comes back invalid

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("POPESCU ION", "administrator"),
                                 ("POPESCU ION MIHAI", "administrator")], source="onrc")
        await s.add_people(cid, [("Ion Popescu", "medic")], source="website")
        await _addresses(s, cid)

    run(go())
    assert sorted(asked) == sorted([("Ion", "Popescu"), ("Popescu", "Ion"),
                                    ("Popescu", "Mihai")])
    assert len(asked) == len(set(asked))


@pytest.mark.parametrize("name,source,first", [("Ana-Maria Exemplu", "website", "Ana-Maria"),
                                               ("EXEMPLESCU ANA-MARIA", "onrc", "Ana-Maria")])
def test_the_email_audience_greets_by_the_whole_given_name(name, source, first):
    """agents/audience.py greets by split_name's first name."""
    assert contacts.split_name(name, source)[0] == first


def test_a_hyphenated_given_name_is_checked_whole_then_its_first_part(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    asked = _verifier(monkeypatch, {("Ana", "Exemplu"): "verified"})

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA DENT SRL", domain="alfa-dent.example")
        await s.add_people(cid, [("Ana-Maria Exemplu", "medic")], source="website")
        await _addresses(s, cid)

    run(go())
    assert asked == [("Ana-Maria", "Exemplu"), ("Ana", "Exemplu")]


def test_a_discover_run_queues_only_so_many_website_lookups(tmp_path, monkeypatch):
    """A county-wide registry pull mustn't fill the contacts lane for
    days: what the amount pays for plus a free run's worth, the rest left
    to the background's walk."""
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(finder, "_env", lambda: {"tinyfish_api_key": "k",
                                                 "serper_api_key": "k"})
    monkeypatch.setattr(job_kinds, "WEBSITE_LOOKUPS_FREE_PER_RUN", 2)

    async def go():
        s = await _state(db)
        q = [D.DiscoveryQuery(term="dentist", location="Brasov")]
        batch_id, _, _ = await job_kinds.enqueue_sources(
            s, [{"provider": "serper", "queries": q}], max_spend=1.0, find_websites=0.001)
        firms = [await _company(s, name=f"FIRMA {n} SRL", location="Brasov") for n in range(5)]
        first = await job_kinds.enqueue_finder_follow_ups(s, firms[:4], batch_id=batch_id)
        second = await job_kinds.enqueue_finder_follow_ups(s, firms[4:], batch_id=batch_id)
        return batch_id, first, second, await jobs.get_batch(s, batch_id)

    batch_id, first, second, batch = run(go())
    assert (first, second) == (3, 0)             # 1 paid ($0.001 at Serper) + 2 free
    assert batch["params"]["websites_deferred"] == 2


def test_discover_api_without_find_websites_adds_nothing_to_the_cap(client):
    ok = client.post("/api/discover/run", json={"providers": ["serper"], "max_spend": 2})
    assert ok.status_code == 200, ok.text
    assert _rows(client.db_path, "SELECT max_spend, json_extract(params_json, '$.pools') "
                                 "FROM job_batches") == [
        (2.0, json.dumps({"search": 2.0, "websites": 0.0}, separators=(",", ":")))]


def test_the_cli_looks_websites_up_free_unless_given_an_amount():
    import sys
    from unittest import mock

    from harvey import cli

    seen = {}

    def grab(args):
        seen.update(vars(args))

    with mock.patch.object(sys, "argv", ["harvey", "discover", "--estimate"]), \
            mock.patch.object(cli, "cmd_discover", grab):
        try:
            cli.main()
        except SystemExit:
            pass
    assert seen.get("websites_max_spend") == 0.0, seen


def test_a_search_out_of_credit_is_left_out_of_the_background_for_a_while(tmp_path,
                                                                          monkeypatch):
    db = tmp_path / "harvey.db"
    fake = FakeBrave((402, {"error": {"code": "PAYMENT_REQUIRED"}}, {})).install(monkeypatch)
    _site_reader(monkeypatch, [])

    async def go():
        s = await _state(db)
        a = await _company(s, name="ALFA DENT SRL", location="Brașov")
        b = await _company(s, name="BETA DENT SRL", location="Brașov")
        first = await finder.find(s, a, spend=finder.Spend(cap=False), env=KEY, background=True)
        second = await finder.find(s, b, spend=finder.Spend(cap=False), env=KEY, background=True)
        return first, second

    first, second = run(go())
    assert len(fake.requests) == 1
    assert lanes.held("brave") > 3000 and finder.free_for_background("brave", True, 0.5) == 0
    assert "brave: said it can take no more searches for now" in second.skipped


def test_a_page_counts_against_the_free_allowance_as_it_comes():
    p = brave.Brave()
    p.pages, p.on_page = 0, None
    run(p.page_done(3))
    assert lanes.uses("brave") == {"month": 1}


def test_the_free_allowance_is_read_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    db = tmp_path / "harvey.db"
    threads = []
    real = lanes.free_left

    def free_left(module, share=1.0):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(module, share)

    monkeypatch.setattr(lanes, "free_left", free_left)
    monkeypatch.setattr(finder, "_env", lambda: KEY)

    async def go():
        s = await _state(db)
        return await finder.background_status(s)

    assert run(go())["searchers"] and threads and not any(threads)


# ── Brave: the market's country, and its own count of what is left ──


def test_a_lookup_is_searched_in_the_companys_country_when_known():
    fake = FakeBrave()
    run(_fetch(fake, D.DiscoveryQuery(term="ALFA DENT SRL", location="Brașov", depth=10,
                                      limit=10, country="ro")))
    run(_fetch(fake))
    assert fake.requests[0].url.params["country"] == "RO"
    assert "country" not in fake.requests[1].url.params       # not known: none sent


def test_a_country_brave_refuses_is_dropped_and_not_sent_again(monkeypatch):
    monkeypatch.setattr(brave, "_REFUSED_COUNTRIES", set())
    fake = FakeBrave(_COUNTRY_REFUSED, (200, {"web": {"results": RESULTS}}, {}))
    query = D.DiscoveryQuery(term="ALFA DENT SRL", location="Brașov", depth=10, limit=10,
                             country="XQ")
    p, found = run(_fetch(fake, query))
    assert len(found) == 2 and p.last_cost == 0.005            # one search billed
    assert fake.requests[0].url.params["country"] == "XQ"
    assert "country" not in fake.requests[1].url.params
    run(_fetch(fake, query))
    assert "country" not in fake.requests[2].url.params


_COUNTRY_REFUSED = (422, {"error": {"code": "VALIDATION", "status": 422, "meta": {
    "errors": [{"loc": ["query", "country"], "msg": "not a supported country"}]}}}, {})


def test_a_422_about_something_else_keeps_the_country(monkeypatch):
    monkeypatch.setattr(brave, "_REFUSED_COUNTRIES", set())
    bad_query = (422, {"error": {"code": "VALIDATION", "status": 422, "meta": {
        "errors": [{"loc": ["query", "q"], "msg": "too long"}]}}}, {})
    fake = FakeBrave(bad_query)
    with pytest.raises(RuntimeError):
        run(_fetch(fake, D.DiscoveryQuery(term="X" * 500, location="London", depth=10,
                                          limit=10, country="GB")))
    assert len(fake.requests) == 1 and brave._REFUSED_COUNTRIES == set()
    ok = FakeBrave()
    run(_fetch(ok, D.DiscoveryQuery(term="ALFA", location="London", depth=10, limit=10,
                                    country="GB")))
    assert ok.requests[0].url.params["country"] == "GB"


def test_the_search_without_the_country_still_gets_its_retry(monkeypatch):
    monkeypatch.setattr(brave, "_REFUSED_COUNTRIES", set())
    monkeypatch.setattr(brave.asyncio, "sleep", _no_sleep)
    fake = FakeBrave(_COUNTRY_REFUSED, (503, {}, {}), (200, {"web": {"results": RESULTS}}, {}))
    p, found = run(_fetch(fake, D.DiscoveryQuery(term="ALFA DENT SRL", location="Brașov",
                                                 depth=10, limit=10, country="XQ")))
    assert len(found) == 2 and len(fake.requests) == 3


async def _no_sleep(*_a, **_kw):
    return None


def test_the_market_country_comes_from_the_location_or_the_official_source():
    assert finder.market_country({"location": "Brașov, Brașov, Romania"}) == "RO"
    assert finder.market_country({"location": "Brașov", "source": "registry:onrc"}) == "RO"
    assert finder.market_country({"location": "Springfield", "source": "osm"}) == ""


def test_the_lookup_query_carries_the_country():
    from harvey.modules.core.contact_finder.capabilities import lookup_query

    q = lookup_query(api.Input(name="ALFA DENT SRL", locality="Brașov",
                               extra={"country": "RO"}))
    assert q.country == "RO" and lookup_query(api.Input(name="X")).country == ""


def test_brave_saying_less_is_left_stops_the_background_below_its_share():
    lanes.count_uses("brave", 100)
    assert lanes.free_left("brave", 0.5) == 400               # Harvey's own count
    # Brave says only 600 are left this month (the key is used elsewhere too):
    # the background keeps the 500 it leaves a person, so 100 are its own.
    fake = FakeBrave((200, {"web": {"results": RESULTS}},
                      {"X-RateLimit-Remaining": "0, 600", "X-RateLimit-Reset": "1, 86000"}))
    run(_fetch(fake))
    assert lanes.reported_left("brave") == 600
    assert lanes.free_left("brave", 0.5) == 100
    assert lanes.free_left("brave") == 600
    # Less than a person's part left: nothing for the background.
    lanes.report_left("brave", 450, 86000)
    assert lanes.free_left("brave", 0.5) == 0 and lanes.free_left("brave") == 450
    # Brave saying more is left than Harvey counts (101 used, the search
    # above too) changes nothing.
    lanes.report_left("brave", 5000, 86000)
    assert lanes.free_left("brave", 0.5) == 399
    # Its word holds until its window renews.
    lanes.report_left("brave", 10, 0)
    assert lanes.reported_left("brave") is None and lanes.free_left("brave", 0.5) == 399


def test_counting_a_search_keeps_what_brave_said():
    lanes.report_left("brave", 300, 86000)
    lanes.count_uses("brave", 1)
    assert lanes.reported_left("brave") == 300
