"""The Companies page: sorts, filters, pages (index and kept orderings),
exports and checks of the filtered list, and the company record read from
the side databases. All data is fictional."""

import asyncio
import sqlite3
import tempfile
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import companies, contacts, registry
from harvey.models import Company
from harvey.modules.ro.anaf import service as anaf
from harvey.modules.ro.mf_financials import store as fin
from harvey.modules.ro.mf_taxpayers import store as tax
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# name, town, county, cui, website, days after BASE it was found
ROWS = [
    ("ALFA EXEMPLU SRL", "Brașov", "Brașov", "1000004", "alfa.example.com", 1),
    ("BETA EXEMPLU SRL", "Cluj-Napoca", "Cluj", "1000012", "", 2),
    ("GAMA EXEMPLU SRL", "Făgăraș", "Brașov", "1000020", "", 3),
    ("DELTA EXEMPLU SRL", "Sibiu", "Sibiu", "1000039", "delta.example.com", 4),
    ("EPSILON EXEMPLU SRL", "Brașov", "Brașov", "1000047", "", 5),
    ("ZETA GOALĂ SRL", "Iași", "Iași", "", "", 6),
]
SECOND_CUI = "1000055"


@pytest.fixture
def env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        monkeypatch.setattr(dash, "DB_PATH", tmp / "harvey.db")
        monkeypatch.setattr(fin, "FINANCIALS_PATH", tmp / "financials.db")
        monkeypatch.setattr(tax, "TAXPAYERS_PATH", tmp / "taxpayers.db")
        monkeypatch.setattr(anaf, "CACHE_PATH", tmp / "anaf.db")
        monkeypatch.setattr(registry, "REGISTRY_PATH", tmp / "registry.db")
        companies.SNAPSHOTS.clear()
        companies.RANKINGS.clear()
        dash.token_store.configure(tmp / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.tmp = tmp
            c.ids = asyncio.run(_seed(tmp / "harvey.db"))
            yield c
        companies.SNAPSHOTS.clear()
        companies.RANKINGS.clear()


async def _seed(db_path) -> dict:
    s = StateManager(str(db_path))
    await s.init_db()
    await sync_vocabulary(s)
    for code in ("PHONE_NUMBER", "CONTACT_FOUND", "TURNOVER", "EMPLOYEES"):
        await s.set_signal_status(code, "confirmed")
    ids = {}
    for i, (name, town, county, cui, site, day) in enumerate(ROWS):
        ids[name.split()[0].lower()] = await s.add_company(Company(
            name=name, cui=cui, domain=site, website=f"https://{site}" if site else "",
            location=f"{town}, {county}, Romania", address=f"Str. Exemplului nr. {i + 1}, {town}",
            source="registry:onrc", external_id=f"onrc:J08/{i + 1}/2015",
            created_at=BASE + timedelta(days=day), updated_at=BASE + timedelta(days=day)))
    a, b, g, d = ids["alfa"], ids["beta"], ids["gama"], ids["delta"]
    await s.add_observations([
        {"company_id": a, "signal_code": "EMPLOYEES", "value_num": 6.0, "value_text": "2023"},
        {"company_id": a, "signal_code": "EMPLOYEES", "value_num": 7.0, "value_text": "2024"},
    ])
    await s.set_company_activities(a, [("8623", 3)], source="onrc")
    await s.set_company_activities(g, [("8623", 3), ("4711", 3)], source="onrc")
    await s.set_company_activities(b, [("4711", 3)], source="onrc")
    async with s._connect() as db:
        # Alfa: a second CUI another source gave, two administrators (one
        # reached by a mobile of their own), an email, and the chain's phone.
        await db.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                         "VALUES (?, 'cui', ?, 'discover:osm')", (a, SECOND_CUI))
        await contacts.add_person(db, a, "Ana Exemplu", "administrator", "onrc")
        await contacts.add_person(db, a, "Ion Exemplu", "asociat", "onrc")
        await contacts.add(db, a, contacts.Found(kind="phone", value="+40722000101", person="Ana Exemplu",
                                                 source="anaf", trust="confirmed"))
        await contacts.add(db, a, contacts.Found(kind="email", value="office@alfa.example.com",
                                                 status="verified", source="website", trust="confirmed"))
        for cid in (a, d):
            await contacts.add(db, cid, contacts.Found(kind="phone", value="+40268000999",
                                                       source="discover:osm", trust="likely"))
        await contacts.add(db, b, contacts.Found(kind="phone", value="+40268000111", source="anaf",
                                                 trust="confirmed"))
        await contacts.add_source(db, d, "discover:osm", "osm:node/1")
        await contacts.record_search(db, "company", a, "website", "site_read",
                                     looked_for="phone,contact_form,email", outcome="found", found=2)
        await contacts.record_search(db, "company", g, "serper", "website_lookup",
                                     looked_for="website", outcome="nothing")
        await db.commit()
    _side_dbs(Path(db_path).parent)
    return ids


def _side_dbs(tmp: Path) -> None:
    f = sqlite3.connect(tmp / "financials.db")
    f.executescript(fin.SCHEMA)
    vals = lambda **kw: [kw.get(x) for x in fin.FIELDS]  # noqa: E731
    rows = [("1000004", 2024, 900_000, 7), ("1000004", 2023, 800_000, 6), ("1000004", 2022, 700_000, 5),
            (SECOND_CUI, 2024, 5_000, 1),
            ("1000012", 2024, 3_000_000, 40), ("1000020", 2024, 50_000, 2), ("1000039", 2024, None, 3)]
    for cui, year, turnover, staff in rows:
        f.execute(f"INSERT INTO accounts VALUES ({','.join('?' * (4 + len(fin.FIELDS)))})",
                  (cui, year, "BL", "8623", *vals(turnover=turnover, employees=staff, net_profit=100,
                                                   net_loss=None, equity=1000, debts=200)))
    f.execute("INSERT INTO files VALUES (2024, 'BL', 'f.txt', 'https://data.gov.example/2024', 'p', "
              "'2025-06-30', 'CC-BY 4.0', 0, 0)")
    f.execute(fin.LATEST)
    f.execute(fin.FILL_LATEST)
    f.commit()
    f.close()
    t = sqlite3.connect(tmp / "taxpayers.db")
    t.executescript(tax.SCHEMA)
    t.execute("INSERT INTO taxpayers (cui, name, county, locality, street, number, phone, vat, "
              "employer, micro_tax, part) VALUES ('1000004', 'ALFA EXEMPLU SRL', 'Brașov', 'Brașov', "
              "'Str. Exemplului', '1', '0268000101', 1, 1, 0, '1')")
    t.execute("INSERT INTO files VALUES ('1', 'f.txt', 'https://data.gov.example/t', 'p', 2026, "
              "'2026-06-30', 'CC-BY 4.0', 0, 0)")
    t.commit()
    t.close()
    anaf.keep(tmp / "anaf.db", {"1000004": {
        "date_generale": {"cui": 1000004, "denumire": "ALFA EXEMPLU SRL", "telefon": "0268000101",
                          "stare_inregistrare": "INREGISTRAT din data 01.03.2015",
                          "forma_juridica": "SOCIETATE CU RASPUNDERE LIMITATA", "statusRO_e_Factura": True},
        "inregistrare_scop_Tva": {"scpTVA": True, "perioade_TVA": [{"data_inceput_ScpTVA": "2016-01-01"}]},
        "stare_inactiv": {"statusInactivi": False},
        "adresa_sediu_social": {"sdenumire_Strada": "Str. Exemplului", "snumar_Strada": "1",
                                "sdenumire_Localitate": "Brașov", "sdenumire_Judet": "BRAȘOV"}}},
              {SECOND_CUI}, when="2026-10-15T00:00:00+00:00")
    g = sqlite3.connect(tmp / "registry.db")
    g.executescript(";\n".join(p for p in registry.SCHEMA.split(";\n") if "VIRTUAL" not in p))
    g.execute("INSERT INTO firms (id, reg, name, cui, form, county, locality, address, registered, "
              "statuses, operating) VALUES (1, 'J08/1/2015', 'ALFA EXEMPLU SRL', '1000004', 'SRL', "
              "'Brașov', 'Brașov', 'Str. Exemplului nr. 1', '2015-03-01', '1048', 1)")
    g.execute("INSERT INTO firm_caen VALUES (1, 8623, 3)")
    g.executemany("INSERT INTO administrators VALUES (1, ?, ?, ?)",
                  [("Ana", "Exemplu", "ADMINISTRATOR"), ("Ion", "Exemplu", "ASOCIAT")])
    g.execute("INSERT INTO meta VALUES ('dataset', 'firme-01-10-2026')")
    g.commit()
    g.close()


def names(c, **params) -> list[str]:
    params.setdefault("limit", 50)
    r = c.get("/api/companies", params=params)
    assert r.status_code == 200, r.text
    return [x["name"].split()[0] for x in r.json()["companies"]]


# ── Sorts ──


def test_sort_by_index_columns_both_ways(env):
    assert names(env, sort="created") == ["ZETA", "EPSILON", "DELTA", "GAMA", "BETA", "ALFA"]
    assert names(env, sort="created", dir="asc") == ["ALFA", "BETA", "GAMA", "DELTA", "EPSILON", "ZETA"]
    assert names(env, sort="name", dir="asc") == ["ALFA", "BETA", "DELTA", "EPSILON", "GAMA", "ZETA"]
    assert names(env, sort="name", dir="desc")[0] == "ZETA"
    # By place ("Town, County, Romania"): the two in Brașov first, the older one first.
    assert names(env, sort="place", dir="asc")[:2] == ["ALFA", "EPSILON"]
    page = env.get("/api/companies", params={"limit": 2, "sort": "name"}).json()
    assert page["paged_by"] == "index" and page["dir"] == "asc" and page["total"] == 6


def test_sort_by_figures_puts_unknown_last_both_ways(env):
    # Latest turnover: Beta 3M, Alfa 900k, Gama 50k; Delta has none filed, the rest no statement.
    got = names(env, sort="turnover", dir="desc")
    assert got[:3] == ["BETA", "ALFA", "GAMA"] and set(got[3:]) == {"DELTA", "EPSILON", "ZETA"}
    got = names(env, sort="turnover", dir="asc")
    assert got[:3] == ["GAMA", "ALFA", "BETA"] and set(got[3:]) == {"DELTA", "EPSILON", "ZETA"}
    assert names(env, sort="employees", dir="desc")[:4] == ["BETA", "ALFA", "DELTA", "GAMA"]
    page = env.get("/api/companies", params={"sort": "turnover", "limit": 10}).json()
    assert page["paged_by"] == "snapshot" and page["financials"] is True
    alfa = next(r for r in page["companies"] if r["name"].startswith("ALFA"))
    assert alfa["figures"] == {"year": 2024, "turnover": 900_000, "employees": 7}


def test_sort_by_contacts_and_last_searched(env):
    # Alfa: mobile, email, chain phone = 3; Beta 1; Delta 1 (the chain phone).
    got = names(env, sort="contacts", dir="desc")
    assert got[0] == "ALFA" and set(got[1:3]) == {"BETA", "DELTA"}
    got = names(env, sort="contacts", dir="asc")
    assert got[-1] == "ALFA" and set(got[:3]) == {"GAMA", "EPSILON", "ZETA"}  # none = 0 comes first
    got = names(env, sort="searched", dir="desc")
    assert set(got[:2]) == {"ALFA", "GAMA"}
    assert set(names(env, sort="searched", dir="asc")[:2]) == {"ALFA", "GAMA"}  # never searched: last
    row = next(r for r in env.get("/api/companies", params={"limit": 10}).json()["companies"]
               if r["name"].startswith("ALFA"))
    assert row["contacts"] == 3 and row["searched_at"]


def test_unknown_sort_falls_back_and_search_sorts_by_relevance(env):
    page = env.get("/api/companies", params={"sort": "nonsense", "limit": 5}).json()
    assert page["sort"] == "created"
    page = env.get("/api/companies", params={"search": "alfa", "limit": 5}).json()
    assert page["sort"] == "relevance" and [r["name"] for r in page["companies"]] == ["ALFA EXEMPLU SRL"]


# ── Filters ──


def test_place_activity_website_filters(env):
    assert set(names(env, county="Brașov")) == {"ALFA", "GAMA", "EPSILON"}
    assert set(names(env, county="brasov", locality="Făgăraș")) == {"GAMA"}
    assert set(names(env, caen="8623")) == {"ALFA", "GAMA"}
    assert set(names(env, caen="86.23, 4711")) == {"ALFA", "GAMA", "BETA"}
    assert set(names(env, county="Brașov", caen="4711")) == {"GAMA"}
    assert set(names(env, caen="dentist")) >= {"ALFA", "GAMA"}
    assert set(names(env, website="yes")) == {"ALFA", "DELTA"}
    assert set(names(env, website="no")) == {"BETA", "GAMA", "EPSILON", "ZETA"}


def test_contact_and_source_filters(env):
    assert set(names(env, contact="any")) == {"ALFA", "BETA", "DELTA"}
    assert set(names(env, contact="none")) == {"GAMA", "EPSILON", "ZETA"}
    assert set(names(env, contact="email")) == {"ALFA"}
    assert set(names(env, contact="mobile")) == {"ALFA"}
    assert set(names(env, contact="phone")) == {"ALFA", "BETA", "DELTA"}
    assert set(names(env, found_in="discover:osm")) == {"DELTA"}
    assert "registry:onrc" in env.get("/api/companies", params={"limit": 1}).json()["sources"]


def test_figure_and_signal_filters(env):
    assert set(names(env, min_turnover=100_000)) == {"ALFA", "BETA"}
    assert set(names(env, min_turnover=10_000, max_turnover=1_000_000)) == {"ALFA", "GAMA"}
    assert set(names(env, max_employees=5)) == {"GAMA", "DELTA"}
    # unknown=include also keeps the companies with no figure.
    assert set(names(env, min_turnover=100_000, unknown="include")) == \
        {"ALFA", "BETA", "DELTA", "EPSILON", "ZETA"}

    async def observe():
        s = StateManager(str(env.tmp / "harvey.db"))
        await s.add_observations([{"company_id": env.ids["beta"], "signal_code": "PHONE_NUMBER",
                                   "value_text": "+40268000111"}])
    asyncio.run(observe())
    companies.SNAPSHOTS.clear()
    assert set(names(env, require="PHONE_NUMBER")) == {"BETA"}
    assert "BETA" not in names(env, exclude="PHONE_NUMBER")


def test_search_by_words_and_by_either_cui(env):
    assert names(env, search="gama") == ["GAMA"]
    assert names(env, q="gama") == ["GAMA"]                     # the old parameter
    assert names(env, search="1000004") == ["ALFA"]
    assert names(env, search="RO 1000055") == ["ALFA"]          # the second CUI
    assert set(names(env, search="exemplu", county="Brașov")) == {"ALFA", "GAMA", "EPSILON"}


def test_bad_filters_say_why(env):
    r = env.get("/api/companies", params={"caen": "zzqqxx"})
    assert r.status_code == 400 and "CAEN" in r.json()["error"]
    r = env.get("/api/companies", params={"search": "-alfa"})
    assert r.status_code == 400 and r.json()["error"]
    r = env.get("/api/companies", params={"contact": "Robert'); DROP"})
    assert r.status_code == 400


# ── Pages ──


@pytest.mark.parametrize("sort", ["created", "name", "turnover", "contacts"])
@pytest.mark.parametrize("way", ["asc", "desc"])
def test_pages_cover_the_list_once(env, sort, way):
    full = names(env, sort=sort, dir=way)
    paged = []
    for offset in range(0, 6, 4):
        page = env.get("/api/companies", params={"sort": sort, "dir": way, "limit": 4,
                                                  "offset": offset}).json()
        assert page["total"] == 6 and page["offset"] == offset
        paged += [r["name"].split()[0] for r in page["companies"]]
    assert paged == full and len(set(full)) == 6
    beyond = env.get("/api/companies", params={"sort": sort, "dir": way, "offset": 600}).json()
    assert beyond["companies"] == [] and beyond["total"] == 6


def test_filtered_page_total_is_exact_and_kept_ordering_follows_new_companies(env):
    page = env.get("/api/companies", params={"county": "Brașov", "limit": 2, "offset": 2}).json()
    assert page["total"] == 3 and len(page["companies"]) == 1 and page["paged_by"] == "snapshot"

    async def add():
        s = StateManager(str(env.tmp / "harvey.db"))
        await s.add_company(Company(name="ETA EXEMPLU SRL", location="Brașov, Brașov, Romania",
                                    source="registry:onrc", external_id="onrc:J08/99/2015"))
    asyncio.run(add())
    page = env.get("/api/companies", params={"county": "Brașov", "limit": 2}).json()
    assert page["total"] == 4


def test_ordered_slices_both_directions():
    o = companies.Ordered(array("q", [9, 8, 7]), array("q", [1, 2]))
    assert o.slice("desc", 0, 10) == [9, 8, 7, 1, 2]
    assert o.slice("asc", 0, 10) == [7, 8, 9, 1, 2]
    assert o.slice("asc", 2, 2) == [9, 1]
    z = companies.Ordered(array("q", [9, 8, 7]), array("q", [1, 2]), zero_first=True)
    assert z.slice("asc", 0, 10) == [1, 2, 7, 8, 9]
    assert z.slice("asc", 1, 3) == [2, 7, 8] and len(z) == 5


def test_sort_indexes_exist_and_are_used(env):
    con = sqlite3.connect(env.tmp / "harvey.db")
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_companies_live_created", "idx_companies_live_name",
            "idx_companies_live_place"} <= have
    for sort, index in (("name", "idx_companies_live_name"), ("place", "idx_companies_live_place"),
                        ("created", "idx_companies_live_created")):
        _, body, params = companies.where(contacts.ContactFilter())
        plan = " ".join(r[3] for r in con.execute(
            f"EXPLAIN QUERY PLAN SELECT c.rowid {body}{companies.order_sql(sort, 'asc')} "
            "LIMIT 50 OFFSET 500", params))
        assert index in plan and "TEMP B-TREE" not in plan, plan


# ── The company record ──


def test_record_shows_everything_on_file(env):
    a = env.get(f"/api/companies/{env.ids['alfa']}").json()
    rec = a["record"]
    assert [x["cui"] for x in rec["cuis"]] == ["1000004", SECOND_CUI]
    assert "discover:osm" in rec["cuis"][1]["sources"]
    years = rec["financials"]["years"]
    assert [(y["cui"], y["year"]) for y in years] == [("1000004", 2024), ("1000004", 2023),
                                                      ("1000004", 2022), (SECOND_CUI, 2024)]
    assert years[0]["turnover"] == 900_000 and years[0]["employees"] == 7
    assert years[0]["net_result"] == 100 and years[0]["equity"] == 1000 and years[0]["debts"] == 200
    firm = rec["registry"]["firms"][0]
    assert rec["registry"]["state"] == "ok" and firm["reg"] == "J08/1/2015"
    assert [p["name"] for p in firm["administrators"]] == ["Ana Exemplu", "Ion Exemplu"]
    assert firm["activities"][0]["code"] == "8623"
    answers = {x["cui"]: x for x in rec["anaf"]["answers"]}
    assert answers["1000004"]["vat"] is True and answers["1000004"]["efactura"] is True
    assert answers["1000004"]["inactive"] is False and answers["1000004"]["vat_since"] == "2016-01-01"
    assert answers["1000004"]["status"] == "INREGISTRAT"
    assert answers["1000004"]["checked_at"].startswith("2026-10-15")
    assert answers[SECOND_CUI]["found"] is False
    assert rec["taxpayer"]["rows"][0]["employer"] == 1 and "Brașov" in rec["taxpayer"]["rows"][0]["address"]
    reach = a["reach"]
    people = {p["name"]: p for p in reach["people"]}
    assert people["Ana Exemplu"]["reached"] and not people["Ion Exemplu"]["reached"]
    shared = next(x for x in reach["contacts"] if x["value"] == "+40268000999")
    assert shared["shared_with"] == 1 and shared["trust"] == "likely"
    assert {x["kind"] for x in reach["contacts"]} == {"phone", "email"}
    assert [x["module"] for x in reach["searches"]] == ["website"]
    assert [x["source"] for x in reach["sources"]] == ["registry:onrc"]
    assert a["contacts"] == 3 and a["searched_at"]
    employees = next(x for x in rec["signals"] if x["code"] == "EMPLOYEES")
    assert (employees["year"], employees["value_num"]) == ("2024", 7.0)


def test_record_of_a_company_with_nothing_says_so(env):
    z = env.get(f"/api/companies/{env.ids['zeta']}").json()
    rec = z["record"]
    assert rec["cuis"] == [] and rec["signals"] == []
    for part in ("registry", "financials", "anaf", "taxpayer"):
        assert rec[part]["state"] == "none", part
    assert z["reach"]["contacts"] == [] and z["reach"]["people"] == [] and z["contacts"] == 0


def test_record_without_side_databases_says_not_loaded(env):
    for name in ("financials.db", "taxpayers.db", "anaf.db", "registry.db"):
        (env.tmp / name).unlink()
    rec = env.get(f"/api/companies/{env.ids['alfa']}").json()["record"]
    for part in ("registry", "anaf", "taxpayer"):
        assert rec[part]["state"] == "not_loaded", part
    # Alfa's headcount observations stand in for the statements.
    assert rec["financials"]["from_observations"] is True
    rec = env.get(f"/api/companies/{env.ids['gama']}").json()["record"]
    assert rec["financials"]["state"] == "not_loaded"
    page = env.get("/api/companies", params={"sort": "turnover"}).json()
    assert page["financials"] is False and page["sort"] == "created"


def test_financial_years_fall_back_to_observations(env):
    (env.tmp / "financials.db").unlink()

    async def observe():
        s = StateManager(str(env.tmp / "harvey.db"))
        await s.add_observations([
            {"company_id": env.ids["beta"], "signal_code": "TURNOVER", "value_num": 1200.0, "value_text": "2023"},
            {"company_id": env.ids["beta"], "signal_code": "EMPLOYEES", "value_num": 4.0, "value_text": "2023"},
            {"company_id": env.ids["beta"], "signal_code": "TURNOVER", "value_num": 1500.0, "value_text": "2024"},
        ])
    asyncio.run(observe())
    fin_part = env.get(f"/api/companies/{env.ids['beta']}").json()["record"]["financials"]
    assert fin_part["from_observations"] is True
    assert [(y["year"], y["turnover"], y["employees"]) for y in fin_part["years"]] == \
        [(2024, 1500, None), (2023, 1200, 4)]


# ── The list's actions follow its filters and sort ──


def test_export_and_find_take_the_page_filter(env):
    body = {"filter": {"county": "Brașov"}, "sort": "name", "dir": "asc"}
    r = env.post("/api/export/companies.json", json=body)
    assert [c["name"].split()[0] for c in r.json()["companies"]] == ["ALFA", "EPSILON", "GAMA"]
    assert r.json()["matched"] == 3 and r.headers["X-Export-Matched"] == "3"
    r = env.post("/api/export/companies.json", json={"filter": {"search": "gama"}})
    assert [c["name"] for c in r.json()["companies"]] == ["GAMA EXEMPLU SRL"]
    est = env.post("/api/contacts/find/estimate",
                   json={"filter": {"county": "Brașov", "caen": "8623"}, "force": True}).json()
    assert est["companies"] == 2
    est = env.post("/api/contacts/find/estimate", json={"filter": {"contact": "none"}}).json()
    assert est["companies"] == 3
    r = env.post("/api/export/companies.json", json={"filter": {"caen": "zzqqxx"}})
    assert r.status_code == 400


def test_page_strings_exist_in_both_languages():
    src = (Path(dash.__file__).resolve().parent / "web" / "i18n.js").read_text(encoding="utf-8")
    for key in ("co.sort.turnover", "co.f.contact_none", "rec.financials", "rec.not_loaded",
                "page.go", "drawer.search_again"):
        assert src.count(f"'{key}'") == 2, key


# ── Fixes found in review ──


def _add_company(env, **kw) -> str:
    async def go():
        s = StateManager(str(env.tmp / "harvey.db"))
        return await s.add_company(Company(source="registry:onrc", **kw))
    return asyncio.run(go())


def test_a_number_no_company_has_as_cui_is_searched_as_a_word(env):
    _add_company(env, name="AUTO 24 EXEMPLU SRL", location="Sibiu, Sibiu, Romania")
    assert names(env, search="24") == ["AUTO"]                 # as the old search did
    assert names(env, search="1000004") == ["ALFA"]            # a CUI on file is that company
    assert names(env, search="99999999") == []
    body = {"filter": {"search": "24"}}
    got = env.post("/api/export/companies.json", json=body).json()["companies"]
    assert [c["name"] for c in got] == ["AUTO 24 EXEMPLU SRL"]


def test_single_letters_or_common_words_find_nothing_without_an_error(env):
    for typed in ("a", "s r l"):
        r = env.get("/api/companies", params={"search": typed, "limit": 50})
        assert r.status_code == 200 and r.json()["total"] == 0, typed
    r = env.get("/api/companies", params={"search": "-alfa"})   # only minus-words: say why
    assert r.status_code == 400 and r.json()["error"]


def test_place_filter_reads_the_distinct_places_and_matches_the_sql_fold(env):
    tricky = ["Brașov, Brașov, Romania", "BRAŞOV, Brasov, Romania", "brasov, BRAȘOV, Romania",
              "Satu Mare, Satu Mare, Romania", "Brașov Nord, Brașov, Romania", " Făgăraș , Brașov, Romania",
              "Codlea, Brașov", "Brașov", "", "Cluj-Napoca, Cluj, Romania", "Zărnești,Brașov,Romania"]
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE companies (location TEXT, merged_into TEXT DEFAULT '')")
    con.executemany("INSERT INTO companies (location) VALUES (?)", [(t,) for t in tricky])
    for county, locality in [("Brașov", ""), ("brasov", ""), ("", "Brașov"), ("satu mare", ""),
                             ("", "Satu Mare"), ("Brașov", "Făgăraș"), ("", "Brașov Nord"), ("Cluj", "")]:
        conds, params = contacts.company_conditions(contacts.ContactFilter(county=county, locality=locality))
        sql = [r[0] for r in con.execute(
            f"SELECT location FROM companies c WHERE {' AND '.join(conds)}", params)]
        py = [t for t in tricky if contacts.place_matches(t, county, locality)]
        assert py == sql, (county, locality)
    page = env.get("/api/companies", params={"county": "brasov", "limit": 50}).json()
    assert page["total"] == 3
    assert set(names(env, county="Iași", locality="Iasi")) == {"ZETA"}
    assert names(env, county="Nicăieri") == []


def test_place_filter_finds_a_company_added_in_a_new_place(env):
    assert names(env, locality="Codlea") == []
    _add_company(env, name="ETA EXEMPLU SRL", location="Codlea, Brașov, Romania")
    assert names(env, locality="Codlea") == ["ETA"]


def test_signal_matches_are_the_same_without_judging_every_company():
    from harvey.state import _current_cte

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            s = StateManager(str(Path(tmp) / "harvey.db"))
            await s.init_db()
            await sync_vocabulary(s)
            for code in ("VAT_REGISTERED", "TURNOVER", "SERP_RANK"):
                await s.set_signal_status(code, "confirmed")
            ids = [await s.add_company(Company(name=f"FIRMA {i} EXEMPLU SRL", source="registry:onrc",
                                               external_id=f"onrc:J08/{i}/2015")) for i in range(8)]
            obs = []
            for i, cid in enumerate(ids):
                if i % 2 == 0:
                    obs.append({"company_id": cid, "signal_code": "VAT_REGISTERED", "value_num": 1.0})
                elif i % 3 == 0:
                    obs.append({"company_id": cid, "signal_code": "VAT_REGISTERED", "value_num": 0.0})
                if i % 3 == 1:
                    obs.append({"company_id": cid, "signal_code": "TURNOVER", "value_num": 1000.0 * i,
                                "value_text": "2024"})
                if i % 4 == 1:
                    obs.append({"company_id": cid, "signal_code": "SERP_RANK", "value_num": 12.0,
                                "value_text": "dentist exemplu"})
            await s.add_observations(obs)
            cases = [(["VAT_REGISTERED"], [], []), ([], ["VAT_REGISTERED"], []),
                     (["TURNOVER"], ["VAT_REGISTERED"], []), (["SERP_RANK"], [], []),
                     ([], [], [{"code": "TURNOVER", "min": 3000}]),
                     (["VAT_REGISTERED"], [], [{"code": "SERP_RANK", "min": 11, "max": 30}])]
            seen = 0
            async with s._connect() as db:
                for require, exclude, ranges in cases:
                    sql, params = s._verdict_sql(require, exclude, ranges, 0.0)
                    codes = len(dict.fromkeys([*require, *exclude, *(r["code"] for r in ranges)]))
                    async with db.execute(sql + "SELECT id FROM matched ORDER BY id", params) as cur:
                        matched = [r[0] for r in await cur.fetchall()]
                    async with db.execute(sql + "SELECT id FROM judged WHERE verdict = 'match' "
                                          "ORDER BY id", params) as cur:
                        judged = [r[0] for r in await cur.fetchall()]
                    # The same judgement over every observation on file.
                    whole = sql.replace(_current_cte(codes).rstrip(), _current_cte().rstrip())
                    async with db.execute(whole + "SELECT id, verdict FROM judged ORDER BY id",
                                          params[codes * 4:]) as cur:
                        every = await cur.fetchall()
                    async with db.execute(sql + "SELECT id, verdict FROM judged ORDER BY id",
                                          params) as cur:
                        restricted = await cur.fetchall()
                    assert matched == judged, (require, exclude, ranges)
                    assert restricted == every, (require, exclude, ranges)
                    seen += bool(matched)
            assert seen >= 4
    asyncio.run(go())


def test_kept_lists_follow_new_contacts_and_searches(env):
    gama = env.ids["gama"]
    first = env.get("/api/companies", params={"contact": "none", "limit": 50}).json()
    assert gama in [r["id"] for r in first["companies"]]
    assert names(env, sort="contacts", dir="asc")[0] in {"GAMA", "EPSILON", "ZETA"}

    async def give():
        s = StateManager(str(env.tmp / "harvey.db"))
        async with s._connect() as db:
            await contacts.add(db, gama, contacts.Found(kind="phone", value="+40268000777",
                                                        source="anaf", trust="confirmed"))
            await contacts.record_search(db, "company", env.ids["zeta"], "website", "site_read",
                                         looked_for="phone", outcome="nothing")
            await db.commit()
    asyncio.run(give())
    again = env.get("/api/companies", params={"contact": "none", "limit": 50}).json()
    assert gama not in [r["id"] for r in again["companies"]]
    assert again["total"] == first["total"] - 1
    assert "GAMA" not in names(env, sort="contacts", dir="asc")[:2]
    assert names(env, sort="searched")[0] == "ZETA"


def test_figure_orderings_do_their_set_work_off_the_event_loop(env, monkeypatch):
    calls = []
    real = companies.asyncio.to_thread

    async def spy(fn, *args, **kw):
        calls.append(fn.__name__)
        return await real(fn, *args, **kw)
    monkeypatch.setattr(companies.asyncio, "to_thread", spy)
    assert names(env, sort="turnover")[:3] == ["BETA", "ALFA", "GAMA"]
    assert "_split" in calls


def test_expired_orderings_leave_memory_on_the_next_request(env, monkeypatch):
    env.get("/api/companies", params={"website": "no", "limit": 50})
    env.get("/api/companies", params={"sort": "contacts", "limit": 50})
    assert companies.SNAPSHOTS._items and companies.RANKINGS._items
    real = companies.time.monotonic
    monkeypatch.setattr(companies.time, "monotonic", lambda: real() + 10_000)
    env.get("/api/companies", params={"limit": 50})             # an index page
    assert not companies.SNAPSHOTS._items and not companies.RANKINGS._items


def test_a_caen_group_means_every_class_in_it(env):
    assert set(names(env, caen="862")) == {"ALFA", "GAMA"}      # 8623 is in group 86.2
    r = env.get("/api/companies", params={"caen": "999"})
    assert r.status_code == 400 and "999" in r.json()["error"]


def test_an_anaf_check_of_a_long_filtered_list_says_it_was_capped(env, monkeypatch):
    from harvey.modules import job_kinds

    queued = []

    async def fake(state, module, kind, ids, **kw):
        queued.append(list(ids))
        return [1], []
    monkeypatch.setattr(job_kinds, "enqueue_id_checks", fake)
    monkeypatch.setattr(dash, "EXPORT_SEARCH_MAX", 2)
    body = env.post("/api/anaf/check", json={"filter": {"county": "Brașov"}, "sort": "name"}).json()
    assert body["success"] and body["capped"] == 2 and body["matched"] == 3
    assert len(queued[0]) == 2
    body = env.post("/api/anaf/check", json={"ids": [env.ids["alfa"]]}).json()
    assert "capped" not in body


def test_contacts_page_checks_the_company_sets_per_link_instead_of_walking_them(env):
    # One contact's links are few; walking a county's, a CAEN class's or a
    # signal's companies for every contact took longer than ten minutes at
    # 1.78M companies.
    f = contacts.ContactFilter(county="Brașov", caen=("8623",), found_in="registry:onrc",
                               contact="any", places=("Brașov, Brașov, Romania",))
    async def plan():
        s = StateManager(str(env.tmp / "harvey.db"))
        f.cohort = s._verdict_sql(["PHONE_NUMBER"], [], [], 0.0)
        with_sql, body, params = contacts.contact_query(f)
        async with s._connect() as db:
            async with db.execute(f"EXPLAIN QUERY PLAN {with_sql}SELECT ct.id {body}", params) as cur:
                return " | ".join(r[3] for r in await cur.fetchall())
    got = asyncio.run(plan())
    assert "(company_id=? AND contact_id=?)" not in got and "(contact_id=?)" in got
    r = env.get("/api/contacts", params={"county": "Brașov", "limit": 50})
    assert r.status_code == 200
    values = {c["value"] for c in r.json()["rows"]}
    assert "+40722000101" in values and "+40268000111" not in values
