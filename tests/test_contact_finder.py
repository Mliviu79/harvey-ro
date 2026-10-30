"""The contact finder (harvey/finder.py): every company with no contact is a
record to search, its own site first, a search by name and town only inside
a spend cap, each step a row in searches; what a site read collects; the
backlog; the Find-contacts API and command; and Discover running several
sources (the registry among them) in one batch. Every site, search and
verifier is a fake; all data is fictional."""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import contacts, finder
from harvey.collectors.profile import social_links
from harvey.models import Company
from harvey.modules import api, job_kinds, jobs
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

ENV = {"serper_api_key": "test-key"}


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


def _page(url, html):
    return api.Page(url, html)


class Fakes:
    """Stands in for the website read, the website lookup and the address
    check: each answers from a dict and remembers what it was asked."""

    def __init__(self, sites=None, results=None, email=None):
        self.sites = sites or {}            # domain -> Draft
        self.results = results or []        # records a lookup returns
        self.email = email
        self.read, self.looked, self.checked = [], [], []

    def install(self, monkeypatch):
        real = api.capability

        async def read_site(inp, ctx):
            self.read.append((inp.domain, ctx.settings.get("contacts")))
            return self.sites.get(inp.domain) or api.Draft()

        async def find_website(inp, ctx):
            self.looked.append((ctx.settings["provider"], inp.name, inp.locality))
            return api.Draft(records=[api.Record(**r) for r in self.results], cost=0.001)

        async def find_email(inp, ctx):
            self.checked.append((inp.extra["first_name"], inp.extra["last_name"], inp.domain))
            if not self.email:
                return api.Draft()
            return api.Draft(contacts=[api.Contact("email", self.email, status="verified")])

        fakes = {("website", "read_site"): read_site,
                 ("contact_finder", "find_website"): find_website,
                 ("email_verify", "find_email"): find_email}

        def capability(module_id, cap_id, manifests=None):
            run_ = fakes.get((module_id, cap_id))
            if run_ is None:
                return real(module_id, cap_id, manifests)
            return api.Capability(module_id, cap_id, run_, estimate=lambda inp: 0.001)

        monkeypatch.setattr(api, "capability", capability)
        return self


def _site(url, html, *contacts_, people=()):
    return api.Draft(pages=[_page(url, html)], contacts=list(contacts_), people=list(people))


def _contact(kind, value, confidence=1.0, url="https://alfa.example/"):
    return api.Contact(kind, value, confidence=confidence, source_url=url,
                       module="website", capability="read_site")


async def _company(s, **kw):
    kw.setdefault("source", "registry:onrc")
    return await s.add_company(Company(**kw))


def _spend(cap=True):
    return finder.Spend(cap=cap)


# ── What a site read collects for the finder ──


def test_social_links_keep_profiles_and_drop_share_buttons():
    html = """
      <a href="https://www.facebook.com/alfa.example.ro/">fb</a>
      <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
      <a href="https://ro.linkedin.com/company/alfa-example?trk=x">in</a>
      <a href="https://www.instagram.com/alfa_example/p/abc">post on its profile</a>
      <a href="https://wa.me/40722000111">chat</a>
      <a href="https://api.whatsapp.com/send?phone=+40722000222&text=hi">chat</a>
      <a href="https://twitter.com/alfa">x</a>"""
    assert social_links(html) == [
        ("facebook", "https://www.facebook.com/alfa.example.ro"),
        ("linkedin", "https://www.linkedin.com/company/alfa-example"),
        ("instagram", "https://www.instagram.com/alfa_example"),
        ("whatsapp", "https://wa.me/40722000111"),
        ("whatsapp", "https://wa.me/40722000222"),
    ]


def test_the_finders_site_read_keeps_emails_forms_profiles_and_people():
    from harvey.collectors.profile import ProfileCollector, _reach

    c = ProfileCollector(confirmed=set())
    c.contacts = True
    c.forms["c1"] = "https://alfa.example/contact"
    c.team["c1"] = [{"first_name": "Ana", "last_name": "Exemplu", "title": "Administrator",
                     "url": "https://alfa.example/echipa"}]
    draft = api.Draft(pages=[_page("https://alfa.example/", (
        '<a href="mailto:office@alfa.example">o</a> <a href="mailto:dev@agency.example">a</a>'
        '<a href="https://www.facebook.com/alfaexample">f</a>'))])
    _reach(draft, c, api.Input(company_id="c1", domain="alfa.example"))
    got = {(x.kind, x.value, x.confidence) for x in draft.contacts}
    assert got == {("email", "office@alfa.example", 1.0), ("email", "dev@agency.example", 0.5),
                   ("facebook", "https://www.facebook.com/alfaexample", 1.0),
                   ("contact_form", "https://alfa.example/contact", 1.0)}
    assert [(p.first_name, p.module) for p in draft.people] == [("Ana", "people")]
    # Every kind it returns is declared by the website module's manifest.
    assert api.undeclared(draft) == []


# ── One company ──


def test_a_known_website_is_read_first_free_and_every_contact_kept(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(sites={"alfa.example": _site(
        "https://alfa.example/", "<p>Alfa</p>",
        _contact("email", "office@alfa.example"), _contact("phone", "+40722000111"),
        _contact("linkedin", "https://www.linkedin.com/company/alfa"),
        people=[api.Person(first_name="Ana", last_name="Exemplu", title="Owner",
                           module="people", capability="find_people")])}).install(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ALFA EXAMPLE SRL", domain="alfa.example",
                             location="Brașov, Brașov, Romania")
        assert (await finder.backlog(s))["site_to_read"] == 1
        found = await finder.find(s, cid, spend=_spend(cap=False), env=ENV)
        return s, cid, found

    s, cid, found = run(go())
    assert fakes.read == [("alfa.example", True)] and fakes.looked == []
    assert found.steps == ["site"] and found.contacts == 3 and found.trust == "confirmed"
    kept = _rows(db, "SELECT ct.kind, ct.line_type, l.trust FROM company_contacts l "
                     "JOIN contacts ct ON ct.id = l.contact_id WHERE l.company_id = ? "
                     "ORDER BY ct.kind", (cid,))
    assert kept == [("email", "", "confirmed"), ("linkedin", "", "confirmed"),
                    ("phone", "mobile", "confirmed")]
    assert _rows(db, "SELECT name, role FROM people") == [("Ana Exemplu", "Owner")]
    assert sorted(_rows(db, "SELECT module, capability FROM searches")) == [
        ("contact_finder", "find_contacts"), ("website", "read_site")]
    b = run(finder.backlog(s, fresh=True))
    assert b["without_contact"] == 0 and b["site_to_read"] == 0


def test_with_no_website_and_no_cap_nothing_is_paid_for_and_nothing_marked(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes().install(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="BETA EXAMPLE SRL", location="Sibiu, Sibiu, Romania")
        return await finder.find(s, cid, spend=_spend(cap=False), env=ENV)

    found = run(go())
    assert fakes.looked == [] and found.steps == []
    assert "no spend cap set" in found.skipped[0]
    # Not marked searched: a run with a cap later still looks it up.
    assert _rows(db, "SELECT COUNT(*) FROM searches")[0][0] == 0


def _beta_results():
    return [dict(name="Firme din Sibiu", domain="listafirme.ro", website="https://listafirme.ro/x"),
            dict(name="Beta Example | Sibiu", domain="betaexample.example",
                 website="https://betaexample.example/")]


def test_a_website_search_picks_the_company_and_its_cui_confirms_it(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=_beta_results(), sites={"betaexample.example": _site(
        "https://betaexample.example/", "<footer>CUI RO 92000001, Sibiu</footer>",
        _contact("email", "contact@betaexample.example",
                 url="https://betaexample.example/"))}).install(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="BETA EXAMPLE SRL", cui="92000001",
                             location="Sibiu, Sibiu, Romania")
        return cid, await finder.find(s, cid, spend=_spend(), env=ENV, batch_id="b1")

    cid, found = run(go())
    # The directory in the results is never taken for the company's site.
    assert fakes.looked == [("serper", "BETA EXAMPLE SRL", "Sibiu")]
    assert fakes.read == [("betaexample.example", True)]
    assert (found.website, found.trust) == ("betaexample.example", "confirmed")
    assert _rows(db, "SELECT domain, website_trust, website_source FROM companies WHERE id = ?",
                 (cid,)) == [("betaexample.example", "confirmed", "serper")]
    assert _rows(db, "SELECT trust FROM company_contacts") == [("confirmed",)]
    searched = _rows(db, "SELECT module, capability, looked_for, outcome, cost, batch_id "
                         "FROM searches WHERE module = 'serper'")
    assert searched == [("serper", "google_search", "website", "found", 0.001, "b1")]


def test_an_unproven_website_is_kept_marked_and_says_nothing_else(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=[dict(name="Gamma Tours", domain="gammatours.example",
                                website="https://gammatours.example/")],
                  sites={"gammatours.example": api.Draft(
                      pages=[_page("https://gammatours.example/", "<p>Holidays</p>")],
                      contacts=[_contact("phone", "+40268000111",
                                         url="https://gammatours.example/")],
                      facts=[api.Fact("TECH_STACK", value_text="WordPress",
                                      module="web_marketing",
                                      capability="marketing_profile")],
                      values={"name": "Gamma Tours"})}).install(monkeypatch)
    fakes.email = "ion.popescu@gammatours.example"

    async def go():
        s = await _state(db)
        await s.set_signal_status("TECH_STACK", "confirmed")
        cid = await _company(s, name="DELTA EXAMPLE SRL", location="Cluj-Napoca, Cluj, Romania")
        await s.add_people(cid, [("POPESCU ION", "administrator")], source="onrc")
        return cid, await finder.find(s, cid, spend=_spend(), env=ENV)

    cid, found = run(go())
    assert found.trust == "unconfirmed"
    # Kept, marked, never the company's key, its name, or a fact about it.
    assert _rows(db, "SELECT name, domain, website, website_trust FROM companies WHERE id = ?",
                 (cid,)) == [("DELTA EXAMPLE SRL", "", "https://gammatours.example/",
                              "unconfirmed")]
    assert _rows(db, "SELECT trust FROM company_contacts") == [("unconfirmed",)]
    assert _rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 0
    # No address is built at a domain that may be someone else's.
    assert fakes.checked == []


def test_a_company_searched_in_one_source_is_still_searched_in_another(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=[]).install(monkeypatch)
    env = {**ENV, "tinyfish_api_key": "test-key"}

    async def go():
        s = await _state(db)
        cid = await _company(s, name="EPSILON EXAMPLE SRL", location="Iași, Iași, Romania")
        # Searched by TinyFish before: not again, but Serper still is.
        await s.record_search("company", cid, "tinyfish", "web_search", looked_for="website")
        first = await finder.find(s, cid, spend=_spend(), env=env)
        again = await finder.find(s, cid, spend=_spend(), env=env)
        async with s._connect() as conn:
            await conn.execute("UPDATE searches SET searched_at = '2026-01-01 00:00:00'")
            await conn.commit()
        forced = await finder.find(s, cid, spend=_spend(), env=env, since=contacts.now())
        return first, again, forced

    first, again, forced = run(go())
    assert [p for p, *_ in fakes.looked][:1] == ["serper"]
    assert first.steps == ["search:serper"] and again.steps == []
    # Forced: searched again in every source not searched since the run began.
    assert sorted(forced.steps) == ["search:serper", "search:tinyfish"]


def test_a_spent_cap_skips_the_search_and_says_so(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=_beta_results()).install(monkeypatch)

    async def full(estimate, first, **kw):
        raise jobs.SpendLimit("the run's spend limit ($0.00) is reached")

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ZETA EXAMPLE SRL", location="Arad, Arad, Romania")
        return await finder.find(s, cid, spend=finder.Spend(full), env=ENV)

    found = run(go())
    assert fakes.looked == [] and "spend limit" in found.skipped[0]


def test_a_person_with_no_contact_gets_one_checked_address(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(email="ion.popescu@eta.example").install(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ETA EXAMPLE SRL", domain="eta.example")
        await s.add_people(cid, [("POPESCU ION", "administrator")], source="onrc")
        await s.record_search("company", cid, "website", "read_site", looked_for="phone,email")
        return cid, await finder.find(s, cid, spend=_spend(), env=ENV)

    cid, found = run(go())
    # The registry writes the family name first.
    assert fakes.checked == [("Ion", "Popescu", "eta.example")]
    assert found.steps == ["address"] and found.contacts == 1
    assert _rows(db, "SELECT ct.value, ct.status, p.name FROM company_contacts l "
                     "JOIN contacts ct ON ct.id = l.contact_id JOIN people p ON p.id = l.person_id"
                 ) == [("ion.popescu@eta.example", "verified", "POPESCU ION")]
    assert _rows(db, "SELECT target, module, outcome FROM searches WHERE target = 'person'") == [
        ("person", "email_verify", "found")]


def test_split_name_reads_the_registry_family_name_first():
    assert finder.split_name("POPESCU ION MIHAI", "onrc") == ("Ion", "Popescu")
    assert finder.split_name("Ana Maria Exemplu", "people") == ("Ana", "Exemplu")
    assert finder.split_name("Ana") == ("", "")


def test_only_a_search_that_can_find_a_company_by_name_is_used():
    enabled = {"serper", "tinyfish", "osm", "dataforseo", "scavio"}
    keys = {"serper_api_key": "k", "tinyfish_api_key": "k", "scavio_api_key": "k",
            "dataforseo_login": "k", "dataforseo_password": "k"}
    found = finder.searchers(enabled, keys)
    providers = [s.provider for s in found]
    assert "osm" not in providers           # OpenStreetMap searches by trade only
    assert providers[0] == "tinyfish"       # cheapest (free) first
    assert {"serper", "scavio_search", "dataforseo_serp"} <= set(providers)
    assert finder.searchers({"osm"}, keys) == []


# ── The backlog and the triggers ──


def test_the_background_takes_free_work_and_paid_only_with_a_day_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    db = tmp_path / "harvey.db"
    monkeypatch.setattr(job_kinds, "_config", lambda: None)

    async def go():
        s = await _state(db)
        site = await _company(s, name="SITE EXAMPLE SRL", domain="site.example")
        bare = await _company(s, name="BARE EXAMPLE SRL")
        queued = await job_kinds.enqueue_backlog(s)
        first = _rows(db, "SELECT params_json FROM jobs")
        monkeypatch.setattr(job_kinds, "_config", lambda: SimpleNamespace(
            spend=SimpleNamespace(per_day_usd=2.0, per_month_usd=None)))
        again = await job_kinds.enqueue_backlog(s)
        return site, bare, queued, first, again

    monkeypatch.setattr(finder, "_env", lambda: {"serper_api_key": "k"})
    site, bare, queued, first, again = run(go())
    assert queued == 1 and json.loads(first[0][0]) == {"company_id": site, "background": True}
    assert again == 1
    ids = {json.loads(p)["company_id"] for (p,) in _rows(db, "SELECT params_json FROM jobs")}
    assert ids == {site, bare}


def test_a_discover_batch_feeds_the_finder_what_it_saved(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        site = await _company(s, name="IOTA EXAMPLE SRL", domain="iota.example")
        bare = await _company(s, name="KAPPA EXAMPLE SRL")
        free = await jobs.new_batch(s, "discover", "free", max_spend=0.0)
        n_free = await job_kinds.enqueue_finder_follow_ups(s, [site, bare], batch_id=free)
        capped = await jobs.new_batch(s, "discover", "capped", max_spend=1.0)
        await jobs.drop_batch(s, free)
        async with s._connect() as conn:
            await conn.execute("DELETE FROM jobs")
            await conn.commit()
        n_capped = await job_kinds.enqueue_finder_follow_ups(s, [site, bare], batch_id=capped)
        return n_free, n_capped

    # A capped run's cap stays its searches': the follow-up is the free site
    # read only, never a website search for the company with none.
    assert run(go()) == (1, 1)
    (params,) = _rows(db, "SELECT params_json FROM jobs")[0]
    assert json.loads(params)["free_only"] is True


def test_a_registry_pull_hands_the_finder_to_its_checks(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        for code in ("VAT_REGISTERED", "PHONE_NUMBER"):
            await s.set_signal_status(code, "confirmed")
        cid = await _company(s, name="LAMBDA EXAMPLE SRL", cui="92000009")
        return await job_kinds.enqueue_follow_up_checks(s, [cid], then_find=True)

    assert run(go()) == 1
    (params,) = _rows(db, "SELECT params_json FROM jobs WHERE kind = 'id_check'")[0]
    assert json.loads(params)["then_find"] is True


def test_the_finder_runs_with_the_email_module_off(tmp_path, monkeypatch):
    from harvey.modules import switches

    db = tmp_path / "harvey.db"
    Fakes(sites={"mu.example": _site("https://mu.example/", "",
                                     _contact("email", "office@mu.example"))}).install(monkeypatch)

    async def go():
        s = await _state(db)
        await switches.set_enabled(s, switches.EMAIL_MODULE, False)
        cid = await _company(s, name="MU EXAMPLE SRL", domain="mu.example")
        return await finder.find(s, cid, spend=_spend(cap=False), env=ENV)

    assert run(go()).contacts == 1


# ── The API and the command ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    from harvey.config import EnvConfig
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: EnvConfig(serper_api_key="k"))
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def _seed(db):
    async def go():
        s = await _state(db)
        ids = [await _company(s, name="NU EXAMPLE SRL", domain="nu.example",
                              location="Brașov, Brașov, Romania"),
               await _company(s, name="XI EXAMPLE SRL", location="Brașov, Brașov, Romania"),
               await _company(s, name="OMICRON EXAMPLE SRL", location="Sibiu, Sibiu, Romania")]
        return ids
    return run(go())


def test_find_contacts_estimates_then_queues_a_capped_batch(client):
    _seed(client.db_path)
    est = client.post("/api/contacts/find/estimate", json={"filter": {"county": "Brașov"}})
    assert est.status_code == 200
    e = est.json()
    assert (e["companies"], e["with_site"], e["to_look_up"]) == (2, 1, 1)
    assert e["estimated_cost"] == pytest.approx(0.001) and e["searchers"][0]["provider"] == "serper"
    r = client.post("/api/contacts/find", json={"filter": {"county": "Brașov"}, "max_spend": 0.5})
    assert r.status_code == 200 and r.json()["queued"] == 2
    (cap,) = _rows(client.db_path, "SELECT max_spend FROM job_batches WHERE kind = 'find_contacts'")
    assert cap[0] == 0.5
    status = client.get("/api/contacts/finder").json()
    assert status["without_contact"] == 3 and status["jobs"] == {"queued": 2}


# ── Discover: several sources, one batch ──


def test_discover_estimates_several_sources_as_one_sum(client, monkeypatch):
    from harvey.collectors import discover as D

    monkeypatch.setattr(dash, "_discovery_queries",
                        lambda body, config, provider="": [D.DiscoveryQuery(term="dentist",
                                                                             location="Brașov")])
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)
    r = client.post("/api/discover/estimate", json={"providers": ["osm", "serper"]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert [s["key"] for s in data["sources"]] == ["osm", "serper"]
    assert data["estimated_cost"] == pytest.approx(sum(s["estimated_cost"] for s in data["sources"]))
    assert data["query_count"] == 2


def test_a_preset_one_source_cant_run_leaves_it_out_with_a_note(client, monkeypatch):
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)

    def queries(body, config, provider=""):
        if provider == "osm":
            raise ValueError("OpenStreetMap can't run the yc preset")
        from harvey.collectors import discover as D
        return [D.DiscoveryQuery(term="startup")]

    monkeypatch.setattr(dash, "_discovery_queries", queries)
    r = client.post("/api/discover/estimate", json={"providers": ["osm", "serper"]})
    data = r.json()
    assert [s["key"] for s in data["sources"]] == ["serper"]
    assert "can't run the yc preset" in data["notes"][0]
    # Alone, the same source is refused.
    assert client.post("/api/discover/estimate", json={"provider": "osm"}).status_code == 400


def test_the_registry_is_a_discover_source_with_its_own_filters(client, monkeypatch, tmp_path):
    from harvey import registry

    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "none.db")
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)
    _seed(client.db_path)
    menu = client.get("/api/discover/providers").json()
    (src,) = [s for s in menu["sources"] if s["key"] == "onrc.registry_search"]
    keys = [f["key"] for f in src["filters"]]
    assert keys[:3] == ["words", "county", "locality"] and "min_turnover" in keys
    body = {"providers": ["onrc.registry_search"],
            "filters": {"onrc.registry_search": {"words": "8623", "county": "BV"}}}
    est = client.post("/api/discover/estimate", json=body).json()
    assert est["estimated_cost"] == 0 and est["free"] is True
    assert "no registry loaded" in est["errors"][0]
    # Nothing narrowing it is every firm in Romania: refused.
    bare = client.post("/api/discover/estimate", json={"providers": ["onrc.registry_search"]})
    assert bare.status_code == 400
    r = client.post("/api/discover/run", json={**body, "max_spend": 0.25})
    assert r.status_code == 200, r.text
    ((kind, lane, params, batch),) = _rows(client.db_path,
                                           "SELECT kind, lane, params_json, batch_id FROM jobs")
    p = json.loads(params)
    assert (kind, lane) == ("registry_pull", "onrc")
    assert p["words"] == "8623" and len(p["counties"]) == 1 and p["with_administrators"]
    summary = client.get("/api/discover/providers").json()
    assert summary["active_runs"][0]["batch_id"] == batch
    assert summary["active_runs"][0]["sources"][0]["key"] == "onrc.registry_search"
    stop = client.post("/api/discover/stop", json={"batch_id": batch,
                                                   "provider": "onrc.registry_search"})
    assert stop.json()["cancelled"] == 1


def test_several_sources_share_one_batch_and_one_cap(tmp_path):
    from harvey.collectors import discover as D

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        q = [D.DiscoveryQuery(term="dentist", location="Brașov")]
        return await job_kinds.enqueue_sources(
            s, [{"provider": "serper", "queries": q}, {"provider": "tinyfish", "queries": q}],
            max_spend=2.0)

    batch_id, ids, dupes = run(go())
    assert len(ids) == 2 and not dupes
    assert _rows(db, "SELECT DISTINCT batch_id FROM jobs") == [(batch_id,)]
    assert _rows(db, "SELECT max_spend FROM job_batches") == [(2.0,)]


# ── The Discover page under node ──


def test_discover_page_ticks_sources_shows_the_registry_filters_and_each_run():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(dash.__file__).resolve().parent / "web"
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const ids = ['discover-providers', 'discover-filters', 'discover-runs', 'disc-run', 'discover-estimate',
             'disc-cities', 'disc-depth', 'disc-limit', 'disc-cap'];
ids.forEach(id => els[id] = {innerHTML: '', value: '', textContent: '', disabled: false, style: {}});
els['disc-cap'].value = '2';
global.document = {getElementById: id => els[id] || null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => JSON.stringify(s);
global.icon = n => '<i>' + n + '</i>';
global.safeHref = v => v;
global.tServer = (k, d) => d;
global.fmtN = n => String(n);
global.fmtUsd = (n, d) => '$' + Number(n).toFixed(d);
global.markLoadFailed = () => {};
global.offlineState = () => 'offline';
global.emptyState = (i, a, b) => a + b;
global.renderProfileCard = () => {};
global.renderDiscoverResult = () => {};
global.showToast = () => {};
global.failMsg = (d, f) => f;
global.setInterval = () => 1; global.clearInterval = () => {};
const calls = [];
global.api = async (path, opts) => { calls.push([path, opts && opts.body]);
  return {providers: [{key: 'serper', label: 'Serper', configured: true, enabled: true, env_keys: [],
                       blurb: 'Google search', cost_note: '$1/1k', free_tier: '2,500'}],
          sources: [{key: 'onrc.registry_search', label: 'ONRC trade registry', configured: true,
                     enabled: true, blurb: 'Every firm', filters: [
                       {key: 'words', type: 'text', label: 'Activity'},
                       {key: 'vat', type: 'yesno', label: 'VAT registered'}]}],
          presets: {}, default: 'osm', selected_all: ['serper', 'onrc.registry_search'], running: true,
          active_runs: [{batch_id: 'b1', label: 'Serper + dentist in BV', jobs: {running: 1, queued: 2},
                         max_spend: 2, spent: 0.004, searches_active: 1, contacts_found: 5, finder_waiting: 3,
                         sources: [{key: 'serper', jobs: {done: 1, running: 1}, new_companies: 4,
                                    known_companies: 1, actual_cost: 0.004, active: 1},
                                   {key: 'onrc.registry_search', jobs: {queued: 1}, new_companies: 0,
                                    known_companies: 0, actual_cost: 0, active: 1}]}]}; };
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('// ── Discover: tick sources', '// Companies found but not looked at'));
(async () => {
  await loadDiscoverProviders();
  setSourceFilter('onrc.registry_search', 'words', '8623');
  setSourceFilter('onrc.registry_search', 'vat', 'yes');
  console.log(JSON.stringify({cards: els['discover-providers'].innerHTML,
    filters: els['discover-filters'].innerHTML, runs: els['discover-runs'].innerHTML,
    body: discoverBody(), stop: t('disc.stop_source'), shared: t('disc.cap_shared', {cap: '$2.00'}),
    caps: [discoverCap('0'), discoverCap(''), discoverCap('2.5')]}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    out = subprocess.run([node, "-e", script, str(web / "app.js"), str(web / "i18n.js")], text=True,
                         encoding="utf-8", capture_output=True, check=True, timeout=30)
    data = json.loads(out.stdout)
    # Checkboxes, both ticked from the last run.
    assert data["cards"].count('type="checkbox" checked') == 2
    assert "ONRC trade registry" in data["filters"] and 'id="sf-onrc-registry_search-vat"' in data["filters"]
    assert data["body"]["providers"] == ["serper", "onrc.registry_search"]
    assert data["body"]["filters"] == {"onrc.registry_search": {"words": "8623", "vat": "yes"}}
    assert data["body"]["max_spend"] == 2
    # 0 is a cap of nothing; blank is the $1 default.
    assert data["caps"] == [0, 1, 2.5]
    # Each source's progress in the run, a Stop for the run and for each source.
    assert data["runs"].count(data["stop"]) == 2
    assert "stopDiscovery(\"b1\",\"onrc.registry_search\")" in data["runs"]
    assert "stopDiscovery(\"b1\")" in data["runs"]


def test_harvey_contacts_status_and_find_in_the_background(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    import harvey.state as state_mod
    from harvey import cli
    from harvey.config import EnvConfig

    db = tmp_path / "harvey.db"

    async def seed():
        s = await _state(db)
        await _company(s, name="PI EXAMPLE SRL", domain="pi.example", location="Brașov, Brașov, Romania")
        await _company(s, name="RHO EXAMPLE SRL", location="Brașov, Brașov, Romania")
    run(seed())
    monkeypatch.setattr(state_mod, "DB_PATH", db)
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: EnvConfig(serper_api_key="k"))
    args = SimpleNamespace(action="status", county="", locality="", website="", q="", require="",
                           exclude="", limit=200, max_spend=0.0, force=False, background=True)
    cli.cmd_contacts(args)
    out = capsys.readouterr().out
    assert "Companies with no contact:    2" in out and "with a website to read:     1" in out
    cli.cmd_contacts(SimpleNamespace(**{**vars(args), "action": "find", "county": "Brașov",
                                        "max_spend": 1.5}))
    out = capsys.readouterr().out
    assert "2 companies: 1 with a website to read (free), 1 to look up" in out
    assert "Queued: 2 jobs" in out
    assert _rows(db, "SELECT max_spend FROM job_batches") == [(1.5,)]


# ── What the backlog, force and the caps keep straight ──


def test_a_plain_site_read_leaves_the_finders_read_to_do(tmp_path, monkeypatch):
    """"Read sites" (and the upgrade's old profile marks) look for phones and
    a contact form, not emails: the company stays in the free backlog and
    the finder still reads its site for its emails."""
    from harvey.modules import runs

    db = tmp_path / "harvey.db"
    fakes = Fakes()
    fakes.install(monkeypatch)
    inner = api.capability

    async def read_site(inp, ctx):
        # Like the real reader: emails only in the finder's contact mode.
        fakes.read.append((inp.domain, ctx.settings.get("contacts")))
        if ctx.settings.get("contacts"):
            return _site("https://kappa.example/", "", _contact("email", "office@kappa.example"))
        return _site("https://kappa.example/", "")

    monkeypatch.setattr(api, "capability", lambda m, c, man=None: api.Capability(
        m, c, read_site) if (m, c) == finder.SITE else inner(m, c, man))

    async def go():
        s = await _state(db)
        cid = await _company(s, name="KAPPA EXAMPLE SRL", domain="kappa.example")
        other = await _company(s, name="LAMBDA EXAMPLE SRL", domain="lambda.example")
        await runs.profile(s, [{"id": cid, "domain": "kappa.example", "name": "KAPPA"}], set())
        # What v21 makes of a profile_checked_at mark.
        await s.record_search("company", other, "website", "read_site",
                              looked_for="phone,contact_form")
        free = await finder.next_free(s, 10)
        found = await finder.find(s, cid, spend=_spend(cap=False), env=ENV)
        return cid, other, free, found, await finder.next_free(s, 10)

    cid, other, free, found, after = run(go())
    assert set(free) == {cid, other}
    assert fakes.read == [("kappa.example", False), ("kappa.example", True)]
    assert found.steps == ["site"] and found.contacts == 1
    assert after == [other]
    looked = _rows(db, "SELECT looked_for FROM searches WHERE module = 'website' "
                       "AND target_id = ? ORDER BY id", (cid,))
    assert looked == [(runs.PLAIN_READ,), (runs.CONTACT_READ,)]


def test_a_failed_search_keeps_the_company_in_the_backlog(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    real = api.capability

    async def broken(inp, ctx):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(api, "capability", lambda m, c, man=None: api.Capability(
        m, c, broken, estimate=lambda inp: 0.001) if (m, c) == finder.LOOKUP else real(m, c, man))

    async def go():
        s = await _state(db)
        cid = await _company(s, name="BETA EXAMPLE SRL", location="Sibiu, Sibiu, Romania")
        found = await finder.find(s, cid, spend=finder.Spend(), env=ENV)
        return cid, found, await finder.next_paid(s, 10, finder.searchers({"serper"}, ENV))

    cid, found, after = run(go())
    assert found.steps == [] and found.failed == 1 and "provider 503" in found.skipped[0]
    assert cid in after
    assert sorted(_rows(db, "SELECT module, outcome FROM searches")) == [
        ("contact_finder", "error"), ("serper", "error")]


def test_a_source_added_later_brings_a_company_back(tmp_path, monkeypatch):
    """Searched in one source, a company can still be searched in another:
    the background brings it back when a new search module is set up."""
    db = tmp_path / "harvey.db"
    Fakes(results=[]).install(monkeypatch)
    tinyfish = {"tinyfish_api_key": "k"}
    both = {**tinyfish, "serper_api_key": "k"}

    async def go():
        s = await _state(db)
        cid = await _company(s, name="GAMMA EXAMPLE SRL", location="Iasi, Iasi, Romania")
        await finder.find(s, cid, spend=finder.Spend(), env=tinyfish)
        enabled = {"tinyfish", "serper"}
        return (cid, await finder.next_paid(s, 10, finder.searchers(enabled, tinyfish)),
                await finder.next_paid(s, 10, finder.searchers(enabled, both)))

    cid, only_tinyfish, with_serper = run(go())
    assert only_tinyfish == [] and with_serper == [cid]


def test_force_and_a_persons_run_are_not_swallowed_by_a_background_job(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _company(s, name="DELTA EXAMPLE SRL", domain="delta.example")
        await job_kinds.enqueue_find_contacts(s, [cid], background=True)
        await job_kinds.enqueue_contact_search(s, [cid], max_spend=0.5)
        await job_kinds.enqueue_contact_search(s, [cid], force=True)
        with pytest.raises(jobs.Duplicate):
            await job_kinds.enqueue_contact_search(s, [cid], max_spend=0.5)
        return [json.loads(p) for (p,) in _rows(db, "SELECT params_json FROM jobs "
                                                     "WHERE status = 'queued' ORDER BY rowid")]

    params = run(go())
    assert [bool(p.get("background")) for p in params] == [True, False, False]
    assert not params[1].get("since") and params[2].get("since")


def test_force_looks_up_a_company_that_has_a_phone_but_no_website(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=[]).install(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="THETA EXAMPLE SRL", location="Arad, Arad, Romania")
        await s.add_contacts(cid, [contacts.Found("phone", "+40257000222", source="anaf")])
        plain = await finder.find(s, cid, spend=finder.Spend(), env=ENV)
        forced = await finder.find(s, cid, spend=finder.Spend(), env=ENV, since=contacts.now())
        return plain, forced

    plain, forced = run(go())
    assert plain.steps == [] and forced.steps == ["search:serper"]
    assert len(fakes.looked) == 1


def test_a_free_search_runs_without_a_cap(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(results=[]).install(monkeypatch)
    monkeypatch.setattr(finder, "lookup_estimate", lambda provider: 0.0)

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ETA EXAMPLE SRL", location="Cluj, Cluj, Romania")
        return await finder.find(s, cid, spend=_spend(cap=False), env={"tinyfish_api_key": "k"})

    found = run(go())
    assert [p for p, *_ in fakes.looked] == ["tinyfish"] and found.steps == ["search:tinyfish"]


def test_address_checks_count_against_the_months_checks(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    fakes = Fakes(email=None).install(monkeypatch)
    monkeypatch.setenv("EMAIL_VERIFY_MONTHLY_CHECKS", "2")

    async def go():
        s = await _state(db)
        cid = await _company(s, name="ZETA EXAMPLE SRL", domain="zeta.example")
        await s.record_search("company", cid, "website", "read_site", looked_for="email")
        await s.add_people(cid, [("POPESCU ION", "a"), ("IONESCU ANA", "b"),
                                 ("GEORGESCU DAN", "c")], source="onrc")
        return await finder.find(s, cid, spend=_spend(), env=ENV)

    found = run(go())
    assert len(fakes.checked) == 2
    assert "address checks are used" in found.skipped[-1]


def test_the_free_backlog_walks_companies_with_a_site_newest_first(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        old = await _company(s, name="OLD EXAMPLE SRL", domain="old.example")
        await _company(s, name="NONE EXAMPLE SRL")
        declared = await _company(s, name="DECL EXAMPLE SRL", website="https://decl.example")
        async with s._connect() as conn:
            await conn.execute("UPDATE companies SET created_at = '2020-01-01' WHERE id = ?",
                               (old,))
            await conn.commit()
        return old, declared, await finder.next_free(s, 10)

    old, declared, got = run(go())
    assert got == [declared, old]


def test_the_backlog_counts_are_kept_for_a_while(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        await _company(s, name="PI EXAMPLE SRL", domain="pi.example")
        first = await finder.backlog(s)
        await _company(s, name="RHO EXAMPLE SRL", domain="rho.example")
        return first, await finder.backlog(s), await finder.backlog(s, fresh=True)

    first, kept, fresh = run(go())
    assert first["without_contact"] == kept["without_contact"] == 1
    assert fresh["without_contact"] == 2


def test_a_selection_by_ids_is_never_cut_short(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        ids = [f"c{i:05d}" for i in range(1_234)]
        async with s._connect() as conn:
            await conn.executemany(
                "INSERT INTO companies (id, name, domain, source) VALUES (?, ?, ?, 'registry:onrc')",
                [(i, f"FIRMA {i} SRL", f"{i}.example") for i in ids])
            await conn.commit()
        batch = await jobs.new_batch(s, "discover", "big", max_spend=0.0)
        return await job_kinds.enqueue_finder_follow_ups(s, ids, batch_id=batch)

    assert run(go()) == 1_234


def test_the_background_feed_skips_what_waits_and_rests_when_nothing_is_left(tmp_path,
                                                                             monkeypatch):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(job_kinds, "_config", lambda: None)
    monkeypatch.setattr(job_kinds, "BACKLOG_QUEUED", 2)
    looked = []
    real = finder.next_free

    async def counted(state, limit):
        looked.append(limit)
        return await real(state, limit)

    monkeypatch.setattr(finder, "next_free", counted)

    async def go():
        s = await _state(db)
        ids = [await _company(s, name=f"SIGMA {i} SRL", domain=f"sigma{i}.example")
               for i in range(3)]
        first = await job_kinds.enqueue_backlog(s)
        # The newest one's job finished: the next top-up takes the oldest,
        # not the one still waiting.
        async with s._connect() as conn:
            await conn.execute("UPDATE jobs SET status = 'done' WHERE rowid = 1")
            await conn.commit()
        await s.record_search("company", ids[2], "website", "read_site", looked_for="email")
        second = await job_kinds.enqueue_backlog(s)
        waiting = sorted(json.loads(p)["company_id"] for (p,) in _rows(
            db, "SELECT params_json FROM jobs WHERE status = 'queued'"))
        async with s._connect() as conn:
            await conn.execute("UPDATE jobs SET status = 'done'")
            await conn.commit()
        for cid in ids:
            await s.record_search("company", cid, "website", "read_site", looked_for="email")
        third = await job_kinds.enqueue_backlog(s)
        fourth = await job_kinds.enqueue_backlog(s)
        return ids, first, second, waiting, third, fourth

    ids, *counts, waiting, third, fourth = run(go())
    assert counts == [2, 1] and waiting == sorted([ids[0], ids[1]])
    assert (third, fourth) == (0, 0)
    # The fourth top-up rested: nothing was left, so it didn't look again.
    assert len(looked) == 3


def test_a_discover_cap_of_0_is_0_and_blank_is_the_default(client, monkeypatch):
    from harvey.collectors import discover as D

    monkeypatch.setattr(dash, "_discovery_queries",
                        lambda body, config, provider="": [D.DiscoveryQuery(term=body["term"],
                                                                             location="Iasi")])
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)
    zero = client.post("/api/discover/run", json={"providers": ["serper"], "term": "dentist",
                                                  "max_spend": 0})
    assert zero.status_code == 200, zero.text
    blank = client.post("/api/discover/run", json={"providers": ["serper"], "term": "roofer",
                                                   "max_spend": ""})
    assert blank.status_code == 200, blank.text
    assert _rows(client.db_path, "SELECT max_spend FROM job_batches ORDER BY rowid") == [
        (0.0,), (1.0,)]


def test_discover_runs_only_the_registrys_job_from_its_filters(client, monkeypatch):
    monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: None)
    monkeypatch.setattr(job_kinds, "discover_sources", lambda enabled=None: [
        {"key": "onrc.other_lookup", "module": "onrc", "job": "other_pull", "label": "Other"}])
    r = client.post("/api/discover/run", json={"providers": ["onrc.other_lookup"],
                                               "filters": {"onrc.other_lookup": {"words": "x"}}})
    assert r.status_code == 400 and "can't be run from Discover" in r.json()["message"]
