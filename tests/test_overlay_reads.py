"""Every read page on the overlay (schema 25), on fictional fixtures: the
registers built by their loaders from tiny inputs in the database's own
folder (attached read-only, as on an install), the overlay filled through
its write paths. Each page's numbers are checked against what the fixture
holds, and every statement the pages run is explained with production-sized
statistics (the plan test's rules): a v25 page reads by index. All data is
fictional."""

import sqlite3

import pytest
import pytest_asyncio

from harvey import contacts
from harvey.collectors import discover as D
from harvey.collectors import onrc
from harvey.contacts import ContactFilter
from harvey.modules import ingest, runs, switches
from harvey.overlay import reads, recount, schema
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import test_query_plans as plans

DATASET = onrc.Dataset("firme-01-01-2026", "2026-01-01", {})
QUERY = D.DiscoveryQuery(term="dentist", location="Brașov")
ALFA, BETA = 1000004, 1000012


def _firm(n, name, cui, website="", domain=""):
    # The registry fixture's three firms (plans.build_registry), as a pull
    # reads them.
    return onrc.Firm(reg=f"J08/{n}/2015", name=name, cui=cui, form="SRL", county="Brașov",
                     locality="Brașov", address=f"Str. Exemplului nr. {n}", website=website,
                     domain=domain, registered="2015-02-01")


async def _pull(state, *firms):
    report = onrc.ImportReport(dataset=DATASET.name)
    draft = onrc.firms_draft(list(firms), DATASET, codes={f.reg: [("8623", 3)] for f in firms},
                             classes=[("8623", None)]).stamp("onrc", "registry_search")
    draft.values.update(codes=("ACTIVITY_CODE",), collector=onrc.COLLECTOR)
    saved: list[str] = []
    await runs.save_source_draft(state, draft, report, module="onrc", saved_ids=saved)
    assert not report.errors, report.errors
    return saved


@pytest_asyncio.fixture
async def state(tmp_path):
    plans.build_registry(tmp_path / "registry.db")
    plans.build_financials(tmp_path / "financials.db")
    plans.build_taxpayers(tmp_path / "taxpayers.db")
    plans.build_anaf(tmp_path / "anaf.db")
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path)
    schema.create(con)
    con.close()
    sm = StateManager(str(path))
    await sm.init_db()
    await sync_vocabulary(sm)
    # Web signals' module (off as shipped).
    await switches.set_enabled(sm, "web_marketing", True)
    for code in ("ACTIVITY_CODE", "PHONE_NUMBER", "CONTACT_FOUND", "TECH_STACK",
                 "RUNNING_GOOGLE_ADS", "TURNOVER", "EMPLOYEES", "EFACTURA_REGISTERED",
                 "HAS_EMPLOYEES_TAX"):
        await sm.set_signal_status(code, "confirmed")
    alfa, beta, gama = await _pull(
        sm, _firm(1, "EXEMPLU ALFA SRL", str(ALFA), "https://alfa.example.com",
                  "alfa.example.com"),
        _firm(2, "EXEMPLU BETA SRL", str(BETA)), _firm(3, "EXEMPLU GAMA SRL", ""))
    saved = await ingest.listing(sm, D.business_record(D.Business(
        name="Cabinet Exemplu", phone="+40 268 000 101", location="Brașov, Romania",
        external_id="dataforseo:cid:1", provider="dataforseo_maps", has_website=False), QUERY))
    listing = saved.company_id
    await sm.add_contacts(alfa, [contacts.Found("email", "office@alfa.example.com",
                                                source="website", trust="confirmed"),
                                 contacts.Found("phone", "+40 722 000 001", source="website",
                                                person="Ana Exemplu", trust="likely")])
    await sm.add_contacts(beta, [contacts.Found("phone", "+40 268 000 102", source="anaf")])
    await sm.add_observations([
        {"company_id": alfa, "signal_code": "TECH_STACK", "value_text": "Exemplu CMS",
         "value_num": 1, "module": "web_marketing", "collector": "profile"},
        {"company_id": alfa, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1,
         "module": "web_marketing", "collector": "profile"},
        {"company_id": beta, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 0,
         "module": "web_marketing", "collector": "profile"}])
    await sm.record_search("company", alfa, "contact_finder", "find_contacts", outcome="found",
                           found=2)
    sm.ids = {"alfa": int(alfa), "beta": int(beta), "gama": int(gama),
              "listing": int(listing)}
    reads.ORDERINGS.clear()
    reads.SETS.clear()
    return sm


def _reader(state):
    con, attached = reads.connect(state.db_path)
    return con, reads.Reader(con, attached)


def _page(state, f=None, sort="created", way="desc", limit=50, offset=0):
    con, r = _reader(state)
    try:
        return r.companies_page(f or ContactFilter(), sort, way, limit, offset)
    finally:
        con.close()


# ── Companies ──

@pytest.mark.asyncio
async def test_the_list_by_index_and_its_harvey_filters(state):
    i = state.ids
    total, ids, how = _page(state)
    assert (total, how) == (4, "index")
    # Newest first (ids follow creation).
    assert ids == sorted(ids, reverse=True) and set(ids) == set(i.values())
    assert _page(state, way="asc")[1] == sorted(i.values())
    assert _page(state, limit=2, offset=1)[1] == sorted(i.values(), reverse=True)[1:3]
    assert _page(state, ContactFilter(website="yes"))[:2] == (1, [i["alfa"]])
    no_site = _page(state, ContactFilter(website="no"))
    assert no_site[0] == 3 and i["alfa"] not in no_site[1]
    with_contact = _page(state, ContactFilter(contact="any"))
    # The listing's phone is its contact too.
    assert set(with_contact[1]) == {i["alfa"], i["beta"], i["listing"]}
    assert _page(state, ContactFilter(contact="none"))[1] == [i["gama"]]
    assert _page(state, ContactFilter(contact="email"))[1] == [i["alfa"]]
    assert set(_page(state, ContactFilter(contact="mobile"))[1]) == {i["alfa"]}


@pytest.mark.asyncio
async def test_register_filters_read_the_register(state):
    i = state.ids
    county = _page(state, ContactFilter(county="Brașov"))
    # The registry's three firms by its own search, plus the listing by its
    # place line.
    assert county[0] == 4 and county[2] == "snapshot"
    assert set(_page(state, ContactFilter(caen=("8623",)))[1]) == {i["alfa"]}
    assert set(_page(state, ContactFilter(cui=str(BETA)))[1]) == {i["beta"]}
    found = _page(state, ContactFilter(words="exemplu", search='"exemplu"*'))
    assert set(found[1]) == set(i.values())
    con, r = _reader(state)
    try:
        listed = next(x for x in r.company_sources() if x.startswith("discover:"))
    finally:
        con.close()
    assert _page(state, ContactFilter(found_in=listed))[1] == [i["listing"]]
    # Figures: turnover 250k (alfa), 1.5M (beta); none for the others.
    assert _page(state, ContactFilter(bounds={"min_turnover": 1e6, "unknown": "exclude"}))[1] \
        == [i["beta"]]
    unknown = _page(state, ContactFilter(bounds={"min_turnover": 1e6, "unknown": "include"}))
    assert set(unknown[1]) == {i["beta"], i["gama"], i["listing"]}
    both = _page(state, ContactFilter(county="Brașov", contact="email"))
    assert both[1] == [i["alfa"]]
    assert _page(state, ContactFilter(signals=(("TECH_STACK",), ())))[1] == [i["alfa"]]


@pytest.mark.asyncio
async def test_sorts_by_name_place_figures_contacts_and_search(state, monkeypatch):
    i = state.ids
    by_name = _page(state, sort="name", way="asc")[1]
    # ALFA, BETA, GAMA (the registry's names), then the listing's own name
    # ("Cabinet..." sorts before "EXEMPLU..." in binary order).
    assert by_name == [i["listing"], i["alfa"], i["beta"], i["gama"]]
    reads.ORDERINGS.clear()
    monkeypatch.setattr(reads, "NAME_ORDER_FROM", 0)
    # The registry's own order for a big list: the same here.
    assert _page(state, sort="name", way="asc")[1] == by_name
    county = ContactFilter(county="Brașov")
    assert _page(state, county, sort="name")[1] == list(reversed(by_name))
    # A figure: highest first, then the firms without one, newest first.
    assert _page(state, sort="turnover")[1][:2] == [i["beta"], i["alfa"]]
    assert _page(state, sort="turnover")[2] == "index"
    assert _page(state, county, sort="turnover")[1] == [
        i["beta"], i["alfa"], *sorted((i["gama"], i["listing"]), reverse=True)]
    assert _page(state, sort="turnover", way="asc")[1][:2] == [i["alfa"], i["beta"]]
    assert _page(state, sort="contacts")[1][0] == i["alfa"]
    # Ascending: the firms with none first.
    assert _page(state, sort="contacts", way="asc")[1][0] == i["gama"]
    assert _page(state, sort="searched")[1][0] == i["alfa"]
    rel = _page(state, ContactFilter(words="gama", search='"gama"*'), sort="relevance")
    assert rel[1] == [i["gama"]]


@pytest.mark.asyncio
async def test_rows_describe_firms_from_the_register_and_locally(state):
    i = state.ids
    con, r = _reader(state)
    try:
        rows = {int(x["id"]): x for x in r.company_rows(list(i.values()))}
    finally:
        con.close()
    alfa = rows[i["alfa"]]
    assert (alfa["name"], alfa["cui"], alfa["location"]) == ("EXEMPLU ALFA SRL", str(ALFA),
                                                             "Brașov, Romania")
    assert alfa["external_id"] == "onrc:J08/1/2015" and alfa["domain"] == "alfa.example.com"
    # Every class on its registration (an old revision's too).
    assert {a["code"] for a in alfa["activities"]} == {"8623", "4321", "7112"}
    # Its email and mobile, and the taxpayer file's phone (kept by the pull).
    assert alfa["figures"]["turnover"] == 250000 and alfa["contacts"] == 3
    assert alfa["_inputs"]["people"][0]["name"] == "Ana Exemplu"
    assert rows[i["gama"]]["external_id"] == "onrc:J08/3/2015" and rows[i["gama"]]["cui"] == ""
    listing = rows[i["listing"]]
    # (A country named last is no county: the place line keeps the town.)
    assert (listing["name"], listing["location"]) == ("Cabinet Exemplu", "Brașov")
    assert listing["phone"] and listing["source"].startswith("discover:")


# ── Contacts, people ──

def _contacts(state, f=None, sort="", way="desc", limit=50, offset=0):
    con, r = _reader(state)
    try:
        return r.contacts_page(f or ContactFilter(), sort, way, limit, offset)
    finally:
        con.close()


@pytest.mark.asyncio
async def test_contacts_and_their_filters(state):
    # ALFA's email, mobile and taxpayer-file phone, BETA's and the listing's.
    total, more, ids = _contacts(state)
    assert (total, more, len(ids)) == (5, False, 5)
    assert _contacts(state, ContactFilter(kind="email"))[0] == 1
    assert _contacts(state, ContactFilter(kind="mobile"))[0] == 1
    assert _contacts(state, ContactFilter(kind="phone"))[0] == 4
    assert _contacts(state, ContactFilter(trust="likely"))[0] == 1
    assert _contacts(state, ContactFilter(source="anaf"))[0] == 1
    assert _contacts(state, ContactFilter(has_person="yes"))[0] == 1
    assert _contacts(state, ContactFilter(website="yes"))[0] == 3
    assert _contacts(state, ContactFilter(county="Brașov", kind="phone"))[0] == 4
    assert _contacts(state, ContactFilter(q="0722"))[0] == 1
    assert _contacts(state, ContactFilter(q="alfa"))[0] == 3
    assert _contacts(state, ContactFilter(signals=(("TECH_STACK",), ())))[0] == 3
    con, r = _reader(state)
    try:
        rows = r.contact_rows(ids)
        email = next(x for x in rows if x["kind"] == "email")
        assert email["companies"] == 1 and email["company"]["name"] == "EXEMPLU ALFA SRL"
        assert email["trust"] == "confirmed" and email["sources"] == ["website"]
        reach = r.reach(email["id"])["links"]
        assert [x["company"] for x in reach] == ["EXEMPLU ALFA SRL"]
        panel = r.of_company(state.ids["alfa"])
        assert {c["kind"] for c in panel["contacts"]} == {"email", "phone"}
        assert panel["searches"][0]["outcome"] == "found"
    finally:
        con.close()


@pytest.mark.asyncio
async def test_people_to_search_are_administrators_and_found_people(state):
    con, r = _reader(state)
    try:
        total, more, rows = r.people_page(ContactFilter(), "", "desc", 50, 0)
    finally:
        con.close()
    names = {(x["company_id"], x["name"]) for x in rows}
    # ALFA's two administrators (the register's) and no one else: Ana is
    # reached by a contact of her own (she is linked), so only Ion is left;
    # the others name no one.
    assert names == {(str(state.ids["alfa"]), "Ion Exemplu")} and total == 1


# ── Signals, cohorts ──

@pytest.mark.asyncio
async def test_signal_states_and_cohorts_over_stored_and_register_signals(state):
    assert await state.recount_register_tally() > 0
    states = await state.signal_states(["TECH_STACK", "RUNNING_GOOGLE_ADS", "TURNOVER",
                                        "EFACTURA_REGISTERED"])
    assert states["TECH_STACK"] == {"true": 1, "false": 0, "unknown": 3}
    assert states["RUNNING_GOOGLE_ADS"] == {"true": 1, "false": 1, "unknown": 2}
    # The registers' counts for their load marks: two firms with a turnover
    # (both above 0), one ANAF answer (e-invoicing: yes).
    assert states["TURNOVER"] == {"true": 2, "false": 0, "unknown": 2}
    assert states["EFACTURA_REGISTERED"]["true"] == 1
    i = state.ids
    counts, rows = await state.cohort_verdicts(["TECH_STACK"], [])
    assert counts == {"match": 1, "possible": 3, "no_match": 0}
    assert [int(x["id"]) for x in rows] == [i["alfa"]]
    counts, _ = await state.cohort_verdicts([], ["RUNNING_GOOGLE_ADS"])
    assert counts == {"match": 1, "possible": 2, "no_match": 1}
    counts, rows = await state.cohort_verdicts([], [], [{"code": "TURNOVER", "min": 1e6}])
    assert counts == {"match": 1, "possible": 2, "no_match": 1}
    assert [int(x["id"]) for x in rows] == [i["beta"]]
    counts, _ = await state.cohort_verdicts(["EFACTURA_REGISTERED"], [])
    assert counts["match"] == 1
    counts, rows = await state.cohort_verdicts(["TECH_STACK"], [], verdicts=("possible",))
    assert len(rows) == 3
    codes = {c["code"]: c for c in await state.get_signal_codes(available_only=True)}
    assert codes["TECH_STACK"]["status"] == "confirmed" and codes["TECH_STACK"]["module"]


# ── The dashboard on the overlay ──

@pytest.fixture
def client(state, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    from harvey import registry, registry_view
    from harvey.modules.ro.anaf import service as anaf
    from harvey.modules.ro.mf_financials import store as fin
    from harvey.modules.ro.mf_taxpayers import store as tax

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(fin, "FINANCIALS_PATH", tmp_path / "financials.db")
    monkeypatch.setattr(tax, "TAXPAYERS_PATH", tmp_path / "taxpayers.db")
    monkeypatch.setattr(anaf, "CACHE_PATH", tmp_path / "anaf.db")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(registry_view, "WARM_FIRMS", 10**9)
    token = dash.token_store.get()
    return TestClient(dash.app, base_url="http://127.0.0.1:5555",
                      headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_dashboard_pages_on_the_overlay(state, client):
    i = state.ids
    r = client.get("/api/companies?limit=50&county=Bra%C8%99ov&sort=name&dir=asc").json()
    assert r["total"] == 4 and r["paged_by"] == "snapshot"
    assert [c["name"] for c in r["companies"]][:2] == ["Cabinet Exemplu", "EXEMPLU ALFA SRL"]
    alfa = next(c for c in r["companies"] if c["id"] == str(i["alfa"]))
    # (Emails shown are the Email module's prospects': none yet.)
    assert alfa["emails"] == [] and alfa["people"][0]["name"] == "Ana Exemplu"
    assert alfa["phone_display"] and alfa["figures"]["turnover"] == 250000
    assert client.get("/api/companies").json()[0]["contact_count"] is not None
    panel = client.get(f"/api/companies/{i['alfa']}").json()
    assert panel["name"] == "EXEMPLU ALFA SRL" and panel["reach"]["contacts"]
    assert panel["record"]["registry"]["state"] == "ok"
    assert panel["record"]["financials"]["years"][0]["turnover"] == 250000
    assert client.get("/api/companies/999999").status_code == 404
    found = client.get("/api/companies/search?q=gama").json()
    assert [c["id"] for c in found["companies"]] == [str(i["gama"])]
    assert client.get(f"/api/companies/search?q=RO{BETA}").json()["companies"][0]["id"] == \
        str(i["beta"])
    c = client.get("/api/contacts?kind=phone").json()
    assert c["total"] == 4 and {"email", "phone"} <= {k["kind"] for k in c["kinds"]}
    p = client.get("/api/contacts?view=people").json()
    assert p["total"] == 1 and p["rows"][0]["name"] == "Ion Exemplu"
    assert client.get("/api/contacts/finder").json()["without_contact"] == 1
    est = client.post("/api/contacts/find/estimate", json={"filter": {"county": "Brașov"}}).json()
    assert est["companies"] == 1 and est["with_site"] == 0
    sig = client.get("/api/signals").json()
    assert sig["total"] and not sig.get("error")
    co = client.post("/api/cohort", json={"require": ["TECH_STACK"]}).json()
    assert co["size"] == 1 and co["companies"][0]["id"] == str(i["alfa"])
    ov = client.get("/api/overview").json()
    assert ov["companies"] == 4 and ov["with_phone"] == 3
    live = client.get("/api/live-status").json()
    assert live["steps"][0]["vars"]["companies"] == 4
    today = client.get("/api/today").json()
    assert today["stats"]["companies"] == 4
    exp = client.post("/api/export/companies.json",
                      json={"filter": {"county": "Brașov", "website": "yes"}}).json()
    assert [x["id"] for x in exp["companies"]] == [str(i["alfa"])]
    assert exp["companies"][0]["turnover"] == 250000
    rows = client.get("/api/export/contacts.json?kind=email").json()
    assert [x["value"] for x in rows] == ["office@alfa.example.com"]
    reg = client.get("/api/registry?limit=50").json()
    assert sum(1 for x in reg["rows"] if x.get("company_id")) == 3
    lines = await state.company_brief(str(i["alfa"]))
    assert "Based in Brașov" in lines and any("Exemplu CMS" in x for x in lines)


# ── Every statement the pages run reads by index ──

V25_PAGES = {
    "today": ("GET", "/api/today", None),
    "live-status": ("GET", "/api/live-status", None),
    "overview": ("GET", "/api/overview", None),
    "signals": ("GET", "/api/signals", None),
    "cohort.efactura": ("POST", "/api/cohort", {"require": ["EFACTURA_REGISTERED"]}),
    "cohort.turnover": ("POST", "/api/cohort",
                        {"ranges": [{"code": "TURNOVER", "min": 1000000}]}),
    "cohort.tech": ("POST", "/api/cohort", {"require": ["TECH_STACK"]}),
    "companies": ("GET", "/api/companies?limit=50", None),
    "companies.offset": ("GET", "/api/companies?limit=50&offset=2", None),
    "companies.county": ("GET", "/api/companies?county=Bra%C8%99ov&limit=50", None),
    "companies.no_contact": ("GET", "/api/companies?contact=none&limit=50", None),
    "companies.mobile": ("GET", "/api/companies?contact=mobile&limit=50", None),
    "companies.search": ("GET", "/api/companies?search=exemplu&limit=50", None),
    "companies.turnover": ("GET", "/api/companies?sort=turnover&limit=50", None),
    "companies.searched": ("GET", "/api/companies?sort=searched&limit=50", None),
    "companies.contacts.asc": ("GET", "/api/companies?sort=contacts&dir=asc&limit=50", None),
    "companies.name": ("GET", "/api/companies?sort=name&limit=50", None),
    "companies.panel": ("GET", "/api/companies/{alfa}", None),
    "contacts": ("GET", "/api/contacts?limit=50", None),
    "contacts.by_companies": ("GET", "/api/contacts?sort=companies&limit=50", None),
    "contacts.mobile_county": ("GET", "/api/contacts?kind=mobile&county=Bra%C8%99ov", None),
    "contacts.mobile": ("GET", "/api/contacts?kind=mobile", None),
    "people": ("GET", "/api/contacts?view=people&limit=50", None),
    "finder.backlog": ("GET", "/api/contacts/finder", None),
    "registry": ("GET", "/api/registry?limit=50", None),
    "search": ("GET", "/api/companies/search?q=exemplu", None),
}


# A bound parameter as the trace shows it (filled in).
_LITERAL = r"(?:-?\d+(?:\.\d+)?(?:e[+-]?\d+)?|'(?:[^']|'')*'|NULL|x'[0-9a-fA-F]*')"


def _explain_named(tracer, files) -> dict[str, set[str]]:
    """The plan test's findings for every SELECT the pages ran, each with
    its named statement's allowances (a statement may scan what it says).
    The trace shows statements with their parameters filled in: each is
    matched to its named statement with the parameters as any value."""
    import re

    from harvey import queries, registers

    regs = {a.module: a for a in registers.adapters(files)}
    patterns = []
    for st in queries.named():
        alias = regs[st.module].alias if st.module in regs else ""
        text = queries.sql(st.name, alias)
        patterns.append((re.compile(re.escape(text).replace(r"\?", _LITERAL)), st))

    def named(sql: str):
        text = " ".join(sql.split())
        return next((st for rx, st in patterns if rx.fullmatch(text)), None)
    con = sqlite3.connect(files / "harvey25.db")
    registers.attach_all(con, list(regs.values()))
    out: dict[str, set[str]] = {}
    for page, rows in tracer.seen.items():
        for _, _, sql in rows:
            st = named(sql)
            try:
                plan = plans.explain(con, sql)
            except sqlite3.Error:
                continue        # a table the production-sized fixture doesn't have
            found = plans.findings(sql, plan, bounded=st.bounded if st else False,
                                   drives=st.drives if st else "",
                                   allowed=st.scans if st else None, con=con)
            if found:
                out.setdefault(page, set()).update(f"{f} [{(st.name if st else sql[:80])}]"
                                                   for f in found)
    con.close()
    return out


@pytest.mark.asyncio
async def test_v25_pages_read_only_by_index(state, client, monkeypatch, tmp_path_factory):
    """Each page is driven on the overlay with every statement traced; each
    is explained against the plan test's production-sized fixtures."""
    files = tmp_path_factory.mktemp("plans25")
    plans.build_registry(files / "registry.db")
    plans.build_financials(files / "financials.db")
    plans.build_taxpayers(files / "taxpayers.db")
    plans.build_anaf(files / "anaf.db")
    con = sqlite3.connect(files / "harvey25.db", isolation_level=None)
    schema.create(con)
    plans._inject(con, "main", plans.OVERLAY_STATS)
    con.close()
    tracer = plans.Tracer()
    monkeypatch.setattr(sqlite3, "connect", tracer.connect)
    for page, (method, url, body) in V25_PAGES.items():
        tracer.page = page
        r = client.request(method, url.format(**state.ids), json=body)
        assert r.status_code < 500, (page, r.status_code, r.text[:300])
    tracer.page = ""
    monkeypatch.setattr(sqlite3, "connect", tracer.real)
    assert set(V25_PAGES) <= set(tracer.seen), sorted(set(V25_PAGES) - set(tracer.seen))
    assert _explain_named(tracer, files) == {}


@pytest.mark.asyncio
async def test_reads_leave_the_kept_values_alone(state):
    con = sqlite3.connect(state.db_path)
    try:
        assert {k: v for k, v in recount.diff(con).items() if v} == {}
    finally:
        con.close()


# ── Regressions: what a review of these reads found ──

@pytest.mark.asyncio
async def test_register_filters_follow_firms_added_merged_and_removed(state):
    # A register condition's set of firms in Harvey is kept per register
    # load; it is worked out again once a firm is added, merged or removed.
    i = state.ids
    over = ContactFilter(bounds={"min_turnover": 1e6, "unknown": "exclude"})
    unknown = ContactFilter(bounds={"min_turnover": 1e6, "unknown": "include"})
    assert _page(state, over)[1] == [i["beta"]]
    assert set(_page(state, unknown)[1]) == {i["beta"], i["gama"], i["listing"]}
    # Gama (pulled, nothing of Harvey's on it) leaves Harvey.
    assert (await state.remove_company(str(i["gama"])))["removed"]
    assert set(_page(state, unknown)[1]) == {i["beta"], i["listing"]}
    # A firm pulled now has no figure on file: unknown.
    (delta,) = await _pull(state, _firm(4, "EXEMPLU DELTA SRL", "1000020"))
    assert int(delta) in _page(state, unknown)[1]
    # Beta merged into the listing: the listing (its survivor) is listed.
    await state.merge_companies(str(i["listing"]), str(i["beta"]))
    assert _page(state, over)[1] == [i["listing"]]


@pytest.mark.asyncio
async def test_a_survivor_without_a_tax_code_sorts_by_its_merged_records_figure(state):
    i = state.ids
    await state.merge_companies(str(i["listing"]), str(i["beta"]))
    page = _page(state, sort="turnover")[1]
    # The listing holds beta's figure through its record: listed once, first;
    # alfa next; then gama, the one with none.
    assert page == [i["listing"], i["alfa"], i["gama"]]
    assert _page(state, sort="turnover", limit=1, offset=1)[1] == [i["alfa"]]
    assert _page(state, sort="turnover", limit=1, offset=2)[1] == [i["gama"]]
    con, r = _reader(state)
    try:
        reader = r._reader_of("TURNOVER")
        s = r.schemes.named(reader.keyed_by)
        assert con.execute(reader.page_count_sql("TURNOVER"), (s.id,)).fetchone()[0] == 2
        assert [x[0] for x in con.execute(reader.lacking_sql("TURNOVER"), (s.id, 50, 0))] == [
            i["gama"]]
    finally:
        con.close()


@pytest.mark.asyncio
async def test_outreach_reads_on_the_overlay(state, client):
    i = state.ids
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO prospects (id, firm, first_name, email, score) "
                "VALUES ('p1', ?, 'Ana', 'ana@alfa.example.com', 7)", (i["alfa"],))
    con.execute("INSERT INTO campaigns (id, name) VALUES ('c1', 'Exemplu')")
    con.commit()
    con.close()
    rows = client.get(f"/api/companies/{i['alfa']}/contacts").json()
    assert [(r["id"], r["company_id"]) for r in rows] == [("p1", str(i["alfa"]))]
    import harvey.dashboard as dash

    items = await dash._with_context([{"prospect_id": "p1", "campaign_id": "c1"}])
    context = items[0]["context"]
    assert context["company"] == "EXEMPLU ALFA SRL" and context["campaign"] == "Exemplu"
    assert context["contact"] == "Ana" and context["website"] == "https://alfa.example.com"
    assert context["signals"]
    # Reads it has no version of yet say so, and an id too long for an
    # integer is no company (not an error).
    assert client.get("/api/duplicates").status_code == 501
    assert client.get("/api/companies/" + "9" * 25).status_code == 404
    assert client.post("/api/companies/" + "9" * 25 + "/remove").status_code == 404
    assert "error" not in client.get("/api/runs").json()
