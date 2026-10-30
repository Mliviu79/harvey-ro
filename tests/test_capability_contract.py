"""The module contract: what every capability is, and what no module may do.

- every capability a manifest declares has code behind it (``entry``);
- ``estimate()`` never touches the network;
- a capability returns only what its manifest's ``outputs`` declare (each
  part checked against the capability stamped on it);
- no file a module consists of (its ``entry`` and the code it ``wraps``)
  imports harvey.state, or core's ingest and runs;
- none of those files calls a method that writes Harvey's database: core
  ingest (harvey/modules/ingest.py) is the only writer of what modules find;
- every observation says which module and capability found it, and for
  which record, and undoing an automatic merge takes back exactly the
  facts found for the merged record, the ones found after the merge too.

Every company, person and number here is invented.
"""

import ast
import importlib
import inspect
import os
import re
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig
from harvey.models import Company
from harvey.modules import api, ingest, runs
from harvey.modules.loader import load_manifests
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import CompanyBatch, StateManager
from tests import test_capability_parity as parity

ROOT = Path(__file__).resolve().parents[1]
# Actions act on what core already holds (a selection to export, the outbox
# to send): they read Harvey's database by design, and get capabilities of
# their own when actions run as jobs. Every other module is held to the rules.
ACTIONS = {"export", "email_outreach"}


@pytest.fixture(scope="module")
def manifests():
    return {m.id: m for m in load_manifests(data_dir=None)}


def _modules_with_code(manifests):
    return [m for m in manifests.values() if m.entry and m.id not in ACTIONS]


# ── Every capability has code ──

def test_every_declared_capability_resolves_to_a_callable(manifests):
    missing = []
    for m in _modules_with_code(manifests):
        for cap in m.capabilities:
            try:
                found = api.capability(m.id, cap["id"], manifests)
            except api.CapabilityMissing as e:
                missing.append(str(e))
                continue
            assert (found.module, found.id) == (m.id, cap["id"])
            assert callable(found.run) and callable(found.estimate)
    assert missing == []


def test_a_module_has_no_code_its_manifest_does_not_declare(manifests):
    for m in _modules_with_code(manifests):
        code = importlib.import_module(m.entry).CAPABILITIES
        assert set(code) == {c["id"] for c in m.capabilities}, m.id


def test_every_module_with_capabilities_names_the_code_it_wraps(manifests):
    for m in _modules_with_code(manifests):
        if m.capabilities:
            assert m.entry.startswith(f"harvey.modules.{m.set}.{m.id}."), m.id
            assert m.wraps, m.id
            for path in m.wraps:
                importlib.import_module(path)


def test_a_fact_a_site_read_finds_belongs_to_the_capability_stamped_on_it(manifests):
    from harvey.collectors.profile import READ_BY

    for code, (module, cap) in READ_BY.items():
        assert code in api.outputs(manifests[module], cap).get("facts", []), code


# ── estimate() is free of the network ──

@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("estimate() opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_estimate_makes_no_network_call(manifests, no_network):
    query = D.DiscoveryQuery(term="roofer", location="Denver, CO",
                             coordinate="39.7392,-104.9903,40")
    for m in _modules_with_code(manifests):
        for cap in m.capabilities:
            price = api.capability(m.id, cap["id"], manifests).estimate(
                api.Input(query=query, domain="example.com", locality="Denver, CO"))
            assert isinstance(price, float) and price >= 0, (m.id, cap["id"])


def test_a_paid_search_estimates_what_its_provider_charges(manifests):
    query = D.DiscoveryQuery(term="roofer", location="Denver, CO", depth=30)
    cap = api.capability("dataforseo", "organic_rank", manifests)
    assert cap.estimate(api.Input(query=query)) == D.PROVIDERS["dataforseo_serp"].estimate([query])


# ── Only declared outputs ──

def _declared(draft, manifests, module, cap_id):
    problems = api.undeclared(draft, manifests)
    out = api.outputs(manifests[module], cap_id)
    if draft.pages and out.get("records") != "pages":
        problems.append(f"{module}.{cap_id} returned pages")
    extra = set(draft.values) - set(out.get("values") or ())
    if extra:
        problems.append(f"{module}.{cap_id} returned values {sorted(extra)}")
    return problems


@pytest.fixture
def recorded(monkeypatch):
    def install(handler):
        parity.recorded(monkeypatch, handler)
    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cap_id,provider", [
    ("osm", "osm_listings", "osm"),
    ("dataforseo", "business_listings", "dataforseo_listings"),
    ("dataforseo", "organic_rank", "dataforseo_serp"),
    ("dataforseo", "maps_rank", "dataforseo_maps"),
    ("serper", "google_search", "serper"),
    ("tinyfish", "web_search", "tinyfish"),
    ("scavio", "maps_listings", "scavio_maps"),
    ("scavio", "google_search", "scavio_search"),
])
async def test_searches_return_only_what_they_declare(manifests, recorded, module, cap_id, provider):
    recorded(parity.provider_handler)
    query = D.DiscoveryQuery(term="roofer", **parity.DENVER)
    env = {"dataforseo_login": "u", "dataforseo_password": "p", "serper_api_key": "k",
           "tinyfish_api_key": "k", "scavio_api_key": "k"}
    async with httpx.AsyncClient() as client:
        draft = await api.capability(module, cap_id, manifests)(
            api.Input(query=query), api.Context(http=client, env=env))
    assert draft.records and draft.failed is None
    assert _declared(draft, manifests, module, cap_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cap_id,preset", [
    ("startups", "yc_search", "yc"), ("startups", "accelerator_search", "accelerator"),
    ("startups", "funding_search", "funding"), ("hiring", "hiring_search", "hiring"),
])
async def test_presets_return_only_what_they_declare(manifests, recorded, module, cap_id, preset):
    recorded(parity.provider_handler)
    query = D.DiscoveryQuery(term="fintech", preset=preset)
    async with httpx.AsyncClient() as client:
        draft = await api.capability(module, cap_id, manifests)(
            api.Input(query=query), api.Context(
                http=client, env={"serper_api_key": "k"},
                settings={"provider": "serper", "searcher": ("serper", "google_search")}))
    assert draft.records
    assert _declared(draft, manifests, module, cap_id) == []
    if preset == "yc":
        assert {f.capability for f in draft.facts if f.code == "YC_BACKED"} == {"yc_search"}


@pytest.mark.asyncio
async def test_geocoding_returns_only_a_point(manifests, recorded):
    recorded(lambda r: httpx.Response(200, json=[{"lat": "45.6", "lon": "25.6"}]))
    async with httpx.AsyncClient() as client:
        draft = await api.capability("nominatim", "geocode", manifests)(
            api.Input(locality="Brașov"), api.Context(http=client))
    assert draft.values == {"point": "45.6000,25.6000"}
    assert _declared(draft, manifests, "nominatim", "geocode") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cap_id", [
    ("website", "read_site"), ("web_marketing", "marketing_profile"),
    ("startups", "startup_claims"), ("hiring", "job_board"),
])
async def test_site_reads_return_only_what_they_declare(manifests, recorded, module, cap_id):
    from harvey.browser import PageReader
    from harvey.collectors.profile import open_http

    recorded(parity.site_handler)
    wanted = frozenset(s["code"] for s in SIGNAL_CATALOG)
    client, pages = open_http(), PageReader()
    try:
        draft = await api.capability(module, cap_id, manifests)(
            api.Input(company_id="c1", name="Dentist Near Me", domain="brightpathdental.example",
                      locality="Denver, CO"),
            api.Context(http=client, browser=pages, wanted=wanted,
                        settings={"role_keywords": ["hygienist"]}))
    finally:
        await client.aclose()
        await pages.aclose()
    assert draft.facts
    assert _declared(draft, manifests, module, cap_id) == []
    if module == "website":
        # One pass, stamped per module: the facts each other module declares.
        assert {f.module for f in draft.facts} >= {"website", "web_marketing", "startups",
                                                   "hiring", "people", "phones"}
        assert draft.pages and draft.values == {"name": "Brightpath Dental"}
    else:
        assert {f.module for f in draft.facts} == {module}


@pytest.mark.asyncio
async def test_page_readers_return_only_what_they_declare(manifests):
    pages = [api.Page("https://brightpathdental.example/team", parity.TEAM),
             api.Page("https://brightpathdental.example/contact", parity.CONTACT)]
    inp = api.Input(domain="brightpathdental.example", locality="Denver, CO", pages=pages)
    people = await api.capability("people", "find_people", manifests)(inp, api.Context())
    assert [p.first_name for p in people.people] == ["Nora", "Ivo"]
    phones = await api.capability("phones", "site_phones", manifests)(
        inp, api.Context(settings={"collector": "people"}))
    assert [c.value for c in phones.contacts] == ["+13035550177"]
    assert [f.collector for f in phones.facts] == ["people"]
    for draft, module, cap in ((people, "people", "find_people"), (phones, "phones", "site_phones")):
        assert _declared(draft, manifests, module, cap) == []


@pytest.mark.asyncio
async def test_email_lookups_return_only_what_they_declare(manifests, monkeypatch):
    from harvey.integrations import email_finder as ef

    async def mx(domain):
        return "mx.example.com"

    async def reoon(email, key):
        return {"status": "valid", "catch_all": True}

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "verify_reoon", reoon)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "")
    ctx = api.Context(env=EnvConfig(reoon_api_key="k"))
    found = await api.capability("email_verify", "find_email", manifests)(
        api.Input(domain="example.com", extra={"first_name": "Ana", "last_name": "Pop"}), ctx)
    assert [(c.value, c.status) for c in found.contacts] == [("ana.pop@example.com", "risky")]
    assert found.values["email_pattern"]["is_catch_all"] == 1
    graded = await api.capability("email_verify", "verify_address", manifests)(
        api.Input(domain="example.com", extra={"email": "office@example.com"}), ctx)
    assert graded.contacts[0].status == "risky"
    for draft, cap in ((found, "find_email"), (graded, "verify_address")):
        assert _declared(draft, manifests, "email_verify", cap) == []


@pytest.mark.asyncio
async def test_people_sources_return_only_what_they_declare(manifests, monkeypatch):
    from harvey.integrations import email_finder as ef

    async def hunter(domain, key, limit=10):
        return [{"first_name": "Ana", "last_name": "Pop", "position": "Owner",
                 "value": f"ana@{domain}"}]

    class Session:
        async def search_people(self, keywords, max_results=10):
            return [{"first_name": "Ion", "last_name": "Pop", "title": "Owner",
                     "linkedin_url": "https://linkedin.example/in/ionpop"}]

    monkeypatch.setattr(ef, "hunter_domain_people", hunter)
    found = await api.capability("hunter", "domain_search", manifests)(
        api.Input(domain="example.com"), api.Context(env=EnvConfig(hunter_api_key="k")))
    assert [p.email for p in found.people] == ["ana@example.com"]
    li = await api.capability("linkedin", "people_search", manifests)(
        api.Input(query="Owner Dental"), api.Context(browser=Session()))
    assert [p.last_name for p in li.people] == ["Pop"]
    assert _declared(found, manifests, "hunter", "domain_search") == []
    assert _declared(li, manifests, "linkedin", "people_search") == []


@pytest.mark.asyncio
async def test_a_spent_allowance_stops_a_people_search(manifests):
    class Spent(api.Budget):
        async def left(self, what):
            return 0

    draft = await api.capability("hunter", "domain_search", manifests)(
        api.Input(domain="example.com"), api.Context(budget=Spent()))
    assert draft.stop and not draft.people


@pytest.mark.asyncio
async def test_keyless_searches_return_only_what_they_declare(manifests, monkeypatch):
    results = ('<div class="result"><a class="result__a" href="https://acme-roofing.example/">'
               'Acme</a><div class="result__snippet">Roofers</div></div>')
    directory = ('<a href="https://acme-roofing.example/">Acme Roofing Co</a>'
                 '<a href="https://www.linkedin.com/x">LinkedIn page</a>')

    async def fetch(url, **kw):
        return SimpleNamespace(text=directory if "list" in url else results)

    from harvey.agents.scout import Scout

    html = await api.capability("web_search_scrape", "html_search", manifests)(
        api.Input(query="roofers"), api.Context(http=fetch))
    assert [(r.source_url, r.description) for r in html.records] == [
        ("https://acme-roofing.example/", "Roofers")]
    scout = Scout.__new__(Scout)
    found = await api.capability("web_search_scrape", "directories", manifests)(
        api.Input(extra={"url": "https://list.example/roofers",
                         "domain_of": scout._extract_domain, "noise": scout._is_noise_domain}),
        api.Context(http=fetch))
    assert [r.domain for r in found.records] == ["acme-roofing.example"]

    class Posts:
        empty = False

        def to_dict(self, orient):
            return [{"company": "Acme Roofing", "title": "Estimator", "job_url": "https://jobs.example/1"}]

    monkeypatch.setitem(sys.modules, "jobspy", SimpleNamespace(scrape_jobs=lambda **k: Posts()))
    jobs = await api.capability("hiring", "job_board_search", manifests)(
        api.Input(query="estimator", area="Denver, CO"), api.Context())
    assert [(r.name, r.found_by) for r in jobs.records] == [("Acme Roofing", "Estimator")]
    for draft, module, cap in ((html, "web_search_scrape", "html_search"),
                               (found, "web_search_scrape", "directories"),
                               (jobs, "hiring", "job_board_search")):
        assert _declared(draft, manifests, module, cap) == []


@pytest.mark.asyncio
async def test_the_registry_returns_only_what_it_declares(manifests, tmp_path):
    from harvey import registry
    from harvey.collectors import onrc
    from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, write

    files = {"caen": write(tmp_path / onrc.FILES["caen"], CAEN),
             "status": write(tmp_path / onrc.FILES["status"], STATUS),
             "firms": write(tmp_path / onrc.FILES["firms"], FIRMS),
             "people": write(tmp_path / onrc.FILES["people"], PEOPLE)}
    loaded = await api.capability("onrc", "registry_sync", manifests)(
        api.Input(extra={"files": files, "dataset": DATASET, "path": tmp_path / "r.db"}),
        api.Context())
    assert loaded.values["registry"]["dataset"] == DATASET.name
    con = registry.connect(tmp_path / "r.db")
    try:
        draft = await api.capability("onrc", "registry_search", manifests)(
            api.Input(query=registry.resolve(con, "dentist BV"), extra={"con": con}),
            api.Context())
    finally:
        con.close()
    assert draft.records and draft.counts["matched"] == len(draft.records)
    # A sole trader's office is a home: never kept.
    assert all(not r.address for r in draft.records if r.legal_form == "PFA")
    for name, cap in ((loaded, "registry_sync"), (draft, "registry_search")):
        assert _declared(name, manifests, "onrc", cap) == []


# ── What a module's files may not do ──

def _files(m) -> list[Path]:
    paths = [m.entry, *m.wraps]
    return [Path(importlib.import_module(p).__file__) for p in paths]


FORBIDDEN_IMPORTS = ("harvey.state", "harvey.modules.ingest", "harvey.modules.runs")


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def test_no_module_file_imports_the_database_or_core_writers(manifests):
    found = {}
    for m in _modules_with_code(manifests):
        for path in _files(m):
            bad = sorted(i for i in _imports(path)
                         if any(i == f or i.startswith(f + ".") for f in FORBIDDEN_IMPORTS))
            if bad:
                found[str(path.relative_to(ROOT))] = bad
    assert found == {}


def _path_of(module: str) -> Path | None:
    for path in (ROOT / (module.replace(".", "/") + ".py"),
                 ROOT / module.replace(".", "/") / "__init__.py"):
        if path.exists():
            return path
    return None


def _reached(m) -> dict[str, str]:
    """Every harvey module a module's files import, all the way down (an
    import inside a function too): module -> the one that imports it."""
    seen: dict[str, str] = {}
    stack = [(p, "") for p in (m.entry, *m.wraps)]
    while stack:
        name, parent = stack.pop()
        if name in seen:
            continue
        seen[name] = parent
        for imported in _imports(_path_of(name)):
            if imported.startswith("harvey") and _path_of(imported) and imported not in seen:
                stack.append((imported, name))
    return seen


def test_nothing_a_module_imports_reaches_the_database_or_core_writers(manifests):
    found = {}
    for m in _modules_with_code(manifests):
        reached = _reached(m)
        for bad in FORBIDDEN_IMPORTS:
            if bad in reached:
                chain = [bad]
                while reached.get(chain[-1]):
                    chain.append(reached[chain[-1]])
                found[m.id] = " <- ".join(chain)
    assert found == {}


def test_no_module_file_reads_the_environment(manifests):
    """A module's keys and settings come in its Context (env, settings),
    never from the process environment."""
    reads = re.compile(r"\bos\.(getenv|environ)\b")
    found = {str(path.relative_to(ROOT)) for m in _modules_with_code(manifests)
             for path in _files(m) if reads.search(path.read_text(encoding="utf-8"))}
    assert found == set()


def _writers() -> set[str]:
    """Every StateManager and CompanyBatch method that writes Harvey's
    database (its body commits, or runs an INSERT, UPDATE or DELETE, or
    calls one that does)."""
    write_sql = re.compile(r"commit\(\)|\b(INSERT|UPDATE|DELETE|REPLACE)\b")
    sources = {name: inspect.getsource(fn)
               for cls in (StateManager, CompanyBatch)
               for name, fn in inspect.getmembers(cls, inspect.isfunction)
               if not name.startswith("__")}
    names = {name for name, src in sources.items() if write_sql.search(src)}
    while True:
        more = {name for name, src in sources.items() if name not in names
                and any(f"self.{w}(" in src for w in names)}
        if not more:
            break
        names |= more
    # A batch's writes go through module-level helpers; its methods named.
    names |= {"save_company", "register_ids", "fill_company_fields", "set_company_activities",
              "add_observations", "add_company", "batch"}
    return names


def test_the_writer_list_is_real():
    names = _writers()
    for expected in ("save_company", "add_observations", "add_prospect", "save_email_pattern",
                     "fill_company_phone", "rename_company", "mark_company_profiled",
                     "set_setting", "increment_setting", "update_company_signals"):
        assert expected in names


def test_ingest_is_the_only_writer_of_what_modules_find(manifests):
    call = re.compile(r"\.(" + "|".join(sorted(_writers())) + r")\(")
    found = {}
    for m in _modules_with_code(manifests):
        for path in _files(m):
            text = path.read_text(encoding="utf-8")
            hits = sorted({h.group(1) for h in call.finditer(text)})
            if hits:
                found[str(path.relative_to(ROOT))] = hits
    assert found == {}


def test_the_privacy_rule_names_the_registrys_personal_forms():
    from harvey.collectors import onrc

    assert ingest.PERSONAL_FORMS == onrc.PERSONAL_FORMS
    record = api.Record(name="TESTESCU ANA PFA", legal_form="PFA", address="Str. Casei 9")
    assert ingest.company_from(record).address == ""
    assert ingest.company_from(api.Record(legal_form="SRL", address="Str. X 1")).address


def test_a_phone_fact_that_does_not_parse_is_left_out():
    facts = [api.Fact("PHONE_NUMBER", value_text="+13035550142"),
             api.Fact("PHONE_NUMBER", value_text="555-0100"),
             api.Fact("TECH_STACK", value_text="HubSpot")]
    rows = ingest.rows(facts, company_id="c1", confirmed={"PHONE_NUMBER"})
    assert [r["value_text"] for r in rows] == ["+13035550142"]


# ── Provenance, and undoing a merge with it ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "contract.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


def _rows(state, sql, *args):
    with sqlite3.connect(state.db_path) as db:
        return db.execute(sql, args).fetchall()


class Listing(D.DiscoveryProvider):
    """A map listing the same business is found under, run after run."""

    key = "fake_maps"
    label = "Fake maps"

    def __init__(self, rating):
        self.rating = rating

    def configured(self, env):
        return True

    async def fetch(self, client, env, query):
        return [D.Business(name="Northvale Roofing Company", phone="+13035550199",
                           location="Denver, Colorado", rating=self.rating,
                           external_id="fake_maps:1", source_url="https://maps.example/1",
                           provider=self.key)]


@pytest.mark.asyncio
async def test_facts_found_after_an_automatic_merge_go_back_on_undo(state, monkeypatch):
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: EnvConfig())
    survivor = (await state.save_company(Company(
        name="Northvale Roofing Company", phone="+13035550199", location="Denver, Colorado",
        source="manual", source_url="https://maps.example/1"))).company_id
    await state.add_observation("REVIEW_COUNT", company_id=survivor, value_num=3)
    query = [D.DiscoveryQuery(term="roofer", location="Denver, CO")]

    monkeypatch.setitem(D.PROVIDERS, "fake_maps", Listing(4.1))
    await runs.run_discovery(state, None, "fake_maps", query)
    [(merged, into)] = _rows(state, "SELECT id, merged_into FROM companies WHERE merged_into != ''")
    assert into == survivor
    # Found again later: the listing's own id is the merged record's, and
    # its facts land on the company it stands for, saying whose they are.
    monkeypatch.setitem(D.PROVIDERS, "fake_maps", Listing(4.6))
    await runs.run_discovery(state, None, "fake_maps", query)
    found = _rows(state, "SELECT signal_code, value_num, company_id, origin_company_id, module, "
                         "capability FROM observations WHERE signal_code = 'REVIEW_RATING' "
                         "ORDER BY rowid")
    assert found == [("REVIEW_RATING", 4.1, survivor, merged, "", "fake_maps"),
                     ("REVIEW_RATING", 4.6, survivor, merged, "", "fake_maps")]

    await state.undo_merge(merged)
    back = _rows(state, "SELECT company_id, COUNT(*) FROM observations WHERE origin_company_id = ? "
                        "GROUP BY company_id", merged)
    assert back == [(merged, 4)]          # FOUND_IN_SERP and REVIEW_RATING, both runs
    # The survivor keeps its own, even one sharing the listing's address.
    assert _rows(state, "SELECT signal_code FROM observations WHERE company_id = ?",
                 survivor) == [("REVIEW_COUNT",)]


@pytest.mark.asyncio
async def test_facts_found_after_a_chain_of_merges_go_back_to_the_first_owner(state, monkeypatch):
    """The listing merged into a company, that company merged by hand into
    another, then the listing found again: its facts are the listing's."""
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: EnvConfig())
    middle = (await state.save_company(Company(
        name="Northvale Roofing Company", phone="+13035550199", location="Denver, Colorado",
        source="manual"))).company_id
    top = (await state.save_company(Company(
        name="Westridge Holdings", domain="westridge.example", location="Boulder, Colorado",
        source="manual"))).company_id
    query = [D.DiscoveryQuery(term="roofer", location="Denver, CO")]
    monkeypatch.setitem(D.PROVIDERS, "fake_maps", Listing(4.1))
    await runs.run_discovery(state, None, "fake_maps", query)
    [(listing, into)] = _rows(state, "SELECT id, merged_into FROM companies WHERE merged_into != ''")
    assert into == middle
    await state.merge_companies(top, middle)

    monkeypatch.setitem(D.PROVIDERS, "fake_maps", Listing(4.6))
    await runs.run_discovery(state, None, "fake_maps", query)
    assert _rows(state, "SELECT DISTINCT company_id, origin_company_id FROM observations "
                        "WHERE value_num = 4.6") == [(top, listing)]

    await state.undo_merge(middle)
    await state.undo_merge(listing)
    assert _rows(state, "SELECT company_id, COUNT(*) FROM observations GROUP BY company_id") \
        == [(listing, 4)]


@pytest.mark.asyncio
async def test_every_observation_says_who_found_it(state, monkeypatch):
    parity.recorded(monkeypatch, parity.provider_handler)
    monkeypatch.setattr("harvey.config.load_env",
                        lambda *a, **k: EnvConfig(dataforseo_login="u", dataforseo_password="p"))
    await runs.run_discovery(state, None, "dataforseo_serp",
                             [D.DiscoveryQuery(term="roofer", **parity.DENVER)], max_spend=5.0)
    rows = _rows(state, "SELECT DISTINCT module, capability FROM observations")
    assert rows == [("dataforseo", "organic_rank")]
    assert not _rows(state, "SELECT 1 FROM observations WHERE origin_company_id = ''")


# ── What core enforces whoever calls a capability ──

def test_a_fact_its_capability_does_not_declare_is_left_out(caplog):
    facts = [api.Fact("EMAIL_PATTERN", value_text="{first}", module="hunter",
                      capability="domain_search"),
             # Not Hunter's to find, nor a site's to name startups as the finder of.
             api.Fact("REVIEW_RATING", value_num=4.0, module="hunter", capability="domain_search"),
             api.Fact("TECH_STACK", value_text="HubSpot", module="startups",
                      capability="startup_claims"),
             # A site read stamps what it reads for another module that declares it.
             api.Fact("TECH_STACK", value_text="HubSpot", module="web_marketing",
                      capability="marketing_profile")]
    rows = ingest.rows(facts, company_id="c1", confirmed=None)
    assert [(r["signal_code"], r["module"]) for r in rows] == [
        ("EMAIL_PATTERN", "hunter"), ("TECH_STACK", "web_marketing")]
    assert "hunter.domain_search returned REVIEW_RATING" in caplog.text


@pytest.mark.asyncio
async def test_a_capability_of_a_module_that_is_off_is_refused(manifests, no_network):
    from harvey.modules.switches import ModuleOff

    cap = api.capability("hiring", "job_board_search", manifests)
    with pytest.raises(ModuleOff, match="hiring"):
        await cap(api.Input(query="Dental Assistant"), api.Context(enabled=frozenset({"website"})))


@pytest.mark.asyncio
async def test_a_site_is_read_only_through_a_client_that_refuses_private_addresses(monkeypatch):
    from harvey.collectors import profile as P

    plain_asked, opened = [], []

    def plain(request):
        plain_asked.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    def guarded():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html><title>Acme</title></html>")))
        client.public_only = True
        opened.append(client)
        return client

    monkeypatch.setattr(P, "open_http", guarded)
    async with httpx.AsyncClient(transport=httpx.MockTransport(plain)) as client:
        await P.site_draft(api.Input(company_id="c1", domain="acme.example"),
                           api.Context(http=client, wanted=frozenset({"TECH_STACK"})))
    assert plain_asked == [] and opened and opened[0].is_closed


def test_a_preset_estimates_what_its_search_charges(manifests):
    query = D.DiscoveryQuery(term="seed round", location="Denver, CO", depth=30)
    cap = api.capability("startups", "yc_search", manifests)
    serp = D.PROVIDERS["dataforseo_serp"].estimate([query])
    assert serp > 0
    assert cap.estimate(api.Input(query=query, extra={"provider": "dataforseo_serp"})) == serp
    # Not told which search: never less than the dearest.
    assert cap.estimate(api.Input(query=query)) >= serp


@pytest.mark.asyncio
async def test_hunter_takes_its_key_from_the_context(manifests, monkeypatch):
    from harvey.agents.scout import Scout
    from harvey.integrations import email_finder as ef

    keys = []

    async def hunter(domain, key, limit=10):
        keys.append(key)
        return []

    monkeypatch.setattr(ef, "hunter_domain_people", hunter)
    monkeypatch.setenv("HUNTER_API_KEY", "from-the-process")
    cap = api.capability("hunter", "domain_search", manifests)
    await cap(api.Input(domain="example.com"), api.Context(env={"hunter_api_key": "k1"}))
    # Scout hands every capability the same shape: a dict of the .env keys.
    ctx = await Scout(None, None, None, EnvConfig(hunter_api_key="k2"))._context()
    assert isinstance(ctx.env, dict)
    await cap(api.Input(domain="example.com"), ctx)
    assert keys == ["k1", "k2"]


@pytest.mark.asyncio
async def test_an_allowance_comes_from_the_modules_manifest(state, monkeypatch):
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "3")
    allowance = ingest.Allowance(state)
    assert await allowance.left("hunter") == 3
    # A module that declares no allowance has none (and isn't an error).
    assert await allowance.left("osm") == 0


def test_a_site_read_is_told_only_the_preset_hits_a_read_declares():
    assert runs._hit_codes({"startups", "website"}, {"YC_BACKED", "TECH_STACK"}) == ("YC_BACKED",)
    assert runs._hit_codes({"website"}, {"YC_BACKED"}) == ()
    assert runs._hit_codes({"startups"}, {"TECH_STACK"}) == ()


@pytest.mark.asyncio
async def test_a_company_scout_finds_gives_its_later_facts_the_record_found(state, monkeypatch):
    survivor = (await state.save_company(Company(
        name="Northvale Roofing Company", phone="+13035550199", location="Denver, Colorado",
        source="manual"))).company_id
    saved = await ingest.found_company(state, api.Record(
        name="Northvale Roofing Company", phone="+13035550199", location="Denver, Colorado",
        domain="northvale.example", source="website"))
    assert saved.company_id == survivor and saved.origin not in ("", survivor)

    from harvey.agents.scout import Scout

    async def found(store, record):
        return saved

    monkeypatch.setattr(ingest, "found_company", found)
    scout = Scout(None, state, None, EnvConfig())
    assert await scout._ensure_company("Northvale Roofing Company", "northvale-roofing.example") \
        == survivor
    await scout._observe_likely_owner(survivor, {"first_name": "Ana", "last_name": "Pop",
                                                 "title": "Owner"})
    assert _rows(state, "SELECT company_id, origin_company_id FROM observations "
                        "WHERE signal_code = 'LIKELY_OWNER'") == [(survivor, saved.origin)]
    await state.undo_merge(saved.origin)
    assert _rows(state, "SELECT company_id FROM observations WHERE signal_code = 'LIKELY_OWNER'") \
        == [(saved.origin,)]


@pytest.mark.asyncio
async def test_a_single_observation_says_who_found_it(state):
    company = (await state.save_company(Company(name="Acme", domain="acme.example"))).company_id
    await state.add_observation("TECH_STACK", company_id=company, value_text="HubSpot",
                                module="web_marketing", capability="marketing_profile",
                                origin_company_id=company)
    assert _rows(state, "SELECT module, capability, origin_company_id FROM observations") == [
        ("web_marketing", "marketing_profile", company)]
