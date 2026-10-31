"""Companies searched locally by what they do and where (state.search_companies).

Covers: the CAEN nomenclature seeded into the database (every revision,
English names, everyday words) and re-seeded only when it changes; the
full-text index kept in step on every write (insert, rename, industry,
new activity from an observation); the upgrade of a database from before
the index; `harvey search`, `harvey caen` and the dashboard endpoint.
Every company, person and place below is invented.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.state as state_mod
from harvey import caen, cli
from harvey.models import Company
from harvey.signals import sync_vocabulary
from harvey.state import MIGRATIONS, StateManager, search_terms


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        yield sm


async def _dental(state, name="ZÂMBET EXEMPLU SRL", location="Cristian, Brașov, Romania"):
    cid = await state.add_company(Company(name=name, cui="90000017", location=location,
                                          industry="Dental practice", source="registry:onrc"))
    await state.set_company_activities(cid, [("8623", 3), ("8623", 2), ("4778", 2)], "registry")
    return cid


async def _names(state, query):
    _, rows = await state.search_companies(query)
    return [r["name"] for r in rows]


# ── The nomenclature in the database ──


def _db(state, sql, args=()):
    with sqlite3.connect(state.db_path) as db:
        return db.execute(sql, args).fetchall()


@pytest.mark.asyncio
async def test_caen_classes_hold_every_revision_with_words(state):
    assert _db(state, "SELECT COUNT(*) FROM caen_classes")[0][0] == len(caen.reference_rows())
    versions = {v for (v,) in _db(state, "SELECT DISTINCT version FROM caen_classes")}
    assert versions == {0, 1, 2, 3}
    ((name_ro, name_en, words),) = _db(
        state, "SELECT name_ro, name_en, keywords FROM caen_classes "
               "WHERE code = '8623' AND version = 3")
    assert name_ro == "Activități de asistență stomatologică"
    assert name_en == "Dental practice"
    for word in ("dentist", "dental", "stomatolog", "ortodont", "cabinet", "stomatologic"):
        assert word in words.split()


@pytest.mark.asyncio
async def test_seeding_is_idempotent_and_follows_an_updated_nomenclature(state, monkeypatch):
    await _dental(state)
    async with state._connect() as db:
        assert await state._seed_caen(db) is False          # nothing changed
    await state.init_db()
    assert _db(state, "SELECT COUNT(*) FROM caen_classes")[0][0] == len(caen.reference_rows())

    rows = [r if r[:2] != ("8623", 3) else (r[0], r[1], "Asistență dentară nouă", r[3], r[4])
            for r in caen.reference_rows()]
    monkeypatch.setattr(caen, "reference_rows", lambda: rows)
    await state.init_db()
    assert _db(state, "SELECT name_ro FROM caen_classes WHERE code = '8623' AND version = 3") \
        == [("Asistență dentară nouă",)]
    assert await _names(state, "dentara") == ["ZÂMBET EXEMPLU SRL"]   # the index re-read it


# ── Searching ──


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "8623",                       # the code, either revision
    "stomatologica",              # Romanian, no accents
    "stomatologică",              # with them
    "STOMATOLOG",                 # a prefix of the inflected word
    "dental practice",            # English name
    "dentist",                    # an everyday word
    "cristian",                   # the town
    "brasov",                     # the county, accent-free
    "8623 cristian",              # code and town together
    "zambet",                     # the name
])
async def test_search_finds_a_company_every_way(state, query):
    await _dental(state)
    await state.add_company(Company(name="FARMA EXEMPLU SRL", location="Exampleville, Romania",
                                    industry="pharmacy"))
    assert await _names(state, query) == ["ZÂMBET EXEMPLU SRL"]


@pytest.mark.asyncio
async def test_every_word_must_match(state):
    await _dental(state)
    assert await _names(state, "dentist cluj") == []
    assert await _names(state, "8622") == []


@pytest.mark.asyncio
async def test_a_code_matches_rev2_and_rev3_registrations(state):
    for cui, version in (("90000025", 2), ("90000033", 3)):
        cid = await state.add_company(Company(name=f"DINTE {version} SRL", cui=cui))
        await state.set_company_activities(cid, [("8623", version)], "registry")
    assert sorted(await _names(state, "8623")) == ["DINTE 2 SRL", "DINTE 3 SRL"]


@pytest.mark.asyncio
async def test_a_map_category_is_searchable_without_any_caen(state):
    await state.add_company(Company(name="Clinica Exemplu", domain="clinica.example.com",
                                    industry="Dentist", location="Exampleville",
                                    source="discover:osm"))
    assert await _names(state, "dentist") == ["Clinica Exemplu"]
    _, rows = await state.search_companies("dentist")
    assert rows[0]["activities"] == []


@pytest.mark.asyncio
async def test_results_carry_details_and_activities_in_words(state):
    cid = await _dental(state)
    await state.fill_company_phone(cid, "+40 700 000 000")
    total, rows = await state.search_companies("dentist")
    assert total == 1
    row = rows[0]
    assert (row["id"], row["cui"], row["phone"]) == (cid, "90000017", "+40 700 000 000")
    acts = [(a["code"], a["version"], a["name"], a["matched"]) for a in row["activities"]]
    # What the words matched first, then the rest.
    assert acts == [
        ("8623", 3, "Activități de asistență stomatologică", True),
        ("8623", 2, "Activități de asistență stomatologică", True),
        ("4778", 2, "Comerț cu amănuntul al altor bunuri noi, în magazine specializate", False),
    ]


@pytest.mark.asyncio
async def test_limit_offset_and_total(state):
    for i in range(5):
        await state.add_company(Company(name=f"Exemplu {i}", industry="dentist"))
    total, rows = await state.search_companies("dentist", limit=2, offset=4)
    assert total == 5 and len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", "de si", '"', "dent* OR x", "NEAR(a b)"])
async def test_odd_queries_never_raise(state, query):
    await _dental(state)
    await state.search_companies(query)


def test_search_terms():
    assert search_terms("Stomatologică 8623 de gp") == (
        '"stomatologica"* AND "8623" AND "gp"', ["stomatologica", "8623", "gp"])
    assert search_terms("") == ("", [])


@pytest.mark.parametrize("query, match", [
    ("86.23", '"8623"'),
    ("8623/2", '"8623"'),
    ("dentist -pizza", '("dentist"*) NOT "pizza"*'),
    ("s.r.l. dentist", '"dentist"*'),
    ("-pizza", ""),
])
def test_search_terms_read_codes_exclusions_and_letters(query, match):
    assert search_terms(query)[0] == match


@pytest.mark.asyncio
async def test_a_minus_word_excludes(state):
    await _dental(state)
    await _dental(state, name="PIZZA DENTIST EXEMPLU SRL")
    assert await _names(state, "dentist -pizza") == ["ZÂMBET EXEMPLU SRL"]
    assert await _names(state, "86.23") != []


# ── Kept in step with every write ──


@pytest.mark.asyncio
async def test_index_follows_renames_industry_and_new_activities(state):
    await sync_vocabulary(state)
    cid = await state.add_company(Company(name="Vechi Nume SRL", location="Exampleville"))
    await state.rename_company(cid, "Nou Nume SRL")
    assert await _names(state, "nou") == ["Nou Nume SRL"]
    assert await _names(state, "vechi") == []

    await state.fill_company_fields(cid, industry="Veterinary clinic")
    assert await _names(state, "veterinary") == ["Nou Nume SRL"]

    # An ACTIVITY_CODE observation is an activity too, whoever wrote it.
    await state.add_observation(company_id=cid, signal_code="ACTIVITY_CODE",
                                value_text="4773/3", collector="registry")
    assert await _names(state, "farmacie") == ["Nou Nume SRL"]
    assert _db(state, "SELECT code, version, source FROM company_activities") == \
        [("4773", 3, "registry")]

    await state.set_company_activities(cid, [], "registry")
    assert await _names(state, "farmacie") == []


@pytest.mark.asyncio
async def test_many_activities_index_once_and_completely(state):
    cid = await state.add_company(Company(name="Multi Exemplu SRL", cui="90000025"))
    classes = [(f"{n:04d}", 2) for n in range(1000, 1400)] + [("8623", 3)]
    await state.set_company_activities(cid, classes, "registry")
    assert await _names(state, "8623") == ["Multi Exemplu SRL"]
    assert _db(state, "SELECT COUNT(*) FROM company_search_dirty") == [(0,)]
    await state.set_company_activities(cid, classes[:-1], "registry")
    assert await _names(state, "8623") == []


@pytest.mark.asyncio
async def test_registry_address_reaches_the_index(state):
    cid = await state.add_company(Company(name="Exemplu SRL"))
    await state.fill_company_fields(cid, address="Str. Exemplului 12", location="Râșnov, Brașov")
    assert await _names(state, "rasnov exemplului") == ["Exemplu SRL"]


# ── Upgrading a database from before the index ──


@pytest.mark.asyncio
async def test_migration_backfills_existing_companies(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:15]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 15")
    conn.execute("INSERT INTO signal_codes (code) VALUES ('ACTIVITY_CODE')")
    conn.execute("INSERT INTO companies (id, name, location, industry) VALUES "
                 "('c1', 'DINTE VECHI SRL', 'Cristian, Brașov', ''), "
                 "('c2', 'Brutăria Exemplu', 'Exampleville', 'bakery')")
    conn.execute("INSERT INTO observations (id, company_id, signal_code, collector, "
                 "value_text, confidence) VALUES ('o1', 'c1', 'ACTIVITY_CODE', 'registry', "
                 "'8623/2', 1.0)")
    conn.commit()
    conn.close()

    sm = StateManager(str(path))
    await sm.init_db()
    assert await _names(sm, "stomatologie") == ["DINTE VECHI SRL"]
    assert await _names(sm, "8623 cristian") == ["DINTE VECHI SRL"]
    assert await _names(sm, "brutaria") == ["Brutăria Exemplu"]
    assert await _names(sm, "bakery") == ["Brutăria Exemplu"]


# ── harvey search / harvey caen ──


def _cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["harvey", *argv])
    cli.main()
    return capsys.readouterr().out


@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    path = tmp_path / "harvey.db"
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    return StateManager(str(path))


def _seed_cli_db(sm):
    async def go():
        await sm.init_db()
        cid = await _dental(sm)
        await sm.fill_company_phone(cid, "+40 700 000 000")
    asyncio.run(go())


def test_cli_search_prints_activities_in_words(cli_db, monkeypatch, capsys):
    _seed_cli_db(cli_db)
    out = _cli(monkeypatch, capsys, "search", "stomatologie", "brasov")
    assert '1 company match "stomatologie brasov"' in out
    assert "ZÂMBET EXEMPLU SRL  CUI 90000017" in out
    assert "Cristian, Brașov, Romania · +40 700 000 000" in out
    assert "8623 — Activități de asistență stomatologică (Dental practice)  Rev.2/3" in out
    assert "+ 1 other activity" in out
    assert "4778" not in out


def test_cli_search_json_and_no_match(cli_db, monkeypatch, capsys):
    _seed_cli_db(cli_db)
    data = json.loads(_cli(monkeypatch, capsys, "search", "8623", "--json"))
    assert data["total"] == 1 and data["companies"][0]["cui"] == "90000017"
    assert {a["code"] for a in data["companies"][0]["activities"]} == {"8623", "4778"}
    assert "No company on file matches" in _cli(monkeypatch, capsys, "search", "zzzqx")


def test_cli_caen_code_lookup_shows_every_revision(monkeypatch, capsys):
    out = _cli(monkeypatch, capsys, "caen", "8623")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "8623  Rev.3  Activități de asistență stomatologică (Dental practice)"
    assert lines[1] == "8623  Rev.2  Activități de asistență stomatologică (Dental practice)"
    assert "harvey search 8623" in out
    # A pre-2008 code is shown, and labelled as such.
    old = _cli(monkeypatch, capsys, "caen", "8513")
    assert "8513  v1     Activități de asistență stomatologică  (pre-2008, old registrations)" in old


def test_cli_caen_unknown_code(monkeypatch, capsys):
    assert "No CAEN class 9999" in _cli(monkeypatch, capsys, "caen", "9999")


# ── Dashboard ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        c.state = StateManager(str(db))
        yield c


@pytest.mark.asyncio
async def test_endpoint_searches_with_activities(client):
    await client.state.init_db()
    await _dental(client.state)
    body = client.get("/api/companies/search", params={"q": "dentist cristian"}).json()
    assert body["total"] == 1 and body["query"] == "dentist cristian"
    company = body["companies"][0]
    assert company["name"] == "ZÂMBET EXEMPLU SRL" and company["contact_count"] == 0
    assert "score" not in company
    assert company["activities"][0]["code"] == "8623"
    assert company["activities"][0]["name_en"] == "Dental practice"


def test_endpoint_empty_query_and_missing_db(client):
    assert client.get("/api/companies/search").json()["companies"] == []
    assert client.get("/api/companies/search", params={"q": "dentist"}).json()["total"] == 0


def test_endpoint_needs_the_token(client):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as anon:
        assert anon.get("/api/companies/search", params={"q": "x"}).status_code == 401


def test_companies_tab_has_the_search_box():
    js = (Path(dash.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    # Paged on the server: one list endpoint takes the search, filters, sort and page.
    assert "apiRead('/api/companies?' + companyParams())" in js
    assert "if (_co.q.trim()) f.search = _co.q.trim();" in js
    assert "p.set('limit', _co.limit);" in js and "p.set('offset', _co.offset);" in js
    assert 'id="company-search"' in js
