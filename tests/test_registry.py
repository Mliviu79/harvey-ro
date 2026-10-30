"""The whole registry in its own database (harvey/registry.py): load, search, pull.

Built from the same tiny invented files as tests/test_onrc.py, plus a few
rows of its own. Every company, person and number here is invented.
"""

import os
import sqlite3
import sys
import tempfile
import time

import pytest
import pytest_asyncio

from harvey import registry
from harvey.collectors import onrc
from harvey.models import Company
from harvey.modules import runs
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import StateManager
from tests.test_onrc import BIRTH, CAEN, CNP, DATASET, FIRMS, PEOPLE, STATUS, firm, write

# A firm whose name says dentist but which isn't authorised for dentistry,
# one listed twice in the firms file, and one with no status row at all.
EXTRA_FIRMS = [
    firm("DENTIST DOAR NUME SRL", "90000112", "J08/990/2019", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("DUBLURA EXEMPLU SRL", "90000120", "J08/991/2019", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("DUBLURA ALTA SRL", "90000139", "J08/991/2019", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("FARA STARE SRL", "90000147", "J08/992/2019", "SRL", "Braşov",
         "Municipiul Braşov"),
]
EXTRA_CAEN = [("J08/990/2019", "4778", "2"), ("J08/991/2019", "4778", "2")]
EXTRA_STATUS = [("J08/990/2019", "1048", ""), ("J08/991/2019", "1048", "")]


@pytest.fixture
def raw(tmp_path):
    folder = tmp_path / "raw"
    folder.mkdir()
    return {
        "caen": write(folder / onrc.FILES["caen"], CAEN + EXTRA_CAEN),
        "status": write(folder / onrc.FILES["status"], STATUS + EXTRA_STATUS),
        "firms": write(folder / onrc.FILES["firms"], FIRMS + EXTRA_FIRMS),
        "people": write(folder / onrc.FILES["people"], PEOPLE),
    }


@pytest.fixture
def db_path(raw, tmp_path):
    path = tmp_path / "registry.db"
    registry.build(raw, DATASET, path)
    return path


@pytest.fixture
def con(db_path):
    c = registry.connect(db_path)
    yield c
    c.close()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


def names(found):
    return [r["name"] for r in found["rows"]]


def obs(state, code):
    with sqlite3.connect(state.db_path) as db:
        return sorted(r[0] for r in db.execute(
            "SELECT value_text FROM observations WHERE signal_code = ?", (code,)))


# ── Loading ──

def test_load_keeps_every_firm_with_its_status(db_path, tmp_path):
    meta = registry.status(db_path)
    assert meta["dataset"] == DATASET.name and meta["date"] == DATASET.date
    assert meta["page"] == DATASET.page
    # 11 + 3 distinct registrations (the duplicate keeps its first row).
    assert meta["firms"] == "14"
    # Operating: A B D E F H P Q R + the two extra with 1048; not C, G, nor
    # the one with no status row.
    assert meta["operating"] == "11"
    assert meta["with_administrators"] == "1"
    # Built aside and renamed: nothing half-built is left next to it (the
    # empty lock file that keeps loads one at a time stays).
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "raw", "registry.db", "registry.db.lock"]
    with sqlite3.connect(db_path) as db:
        dup = db.execute("SELECT name FROM firms WHERE reg = 'J08/991/2019'").fetchall()
        assert dup == [("DUBLURA EXEMPLU SRL",)]
        g = db.execute("SELECT operating, statuses FROM firms "
                       "WHERE reg = 'J08/700/2016'").fetchone()
        assert g == (0, "1048 1049")


def test_load_stores_nothing_private(db_path):
    with sqlite3.connect(db_path) as db:
        dump = "\n".join(db.iterdump())
        admins = sorted(db.execute(
            "SELECT first_name, last_name, role FROM administrators").fetchall())
    # A representative's birth data and home are never read; a sole trader's
    # registered office (Str. Casei) is public registry data and is kept.
    for private in (CNP, BIRTH, "Satul Nasterii", "Satul Domiciliului"):
        assert private not in dump
    # Insolvency practitioners and companies acting as representatives are left out.
    assert admins == [("Andrei-Mihai", "Doi", "Administrator"),
                      ("Maria", "Testescu", "Administrator"),
                      ("Vlad", "Clujeanu", "Administrator")]


def test_load_without_representatives(raw, tmp_path):
    del raw["people"]
    path = tmp_path / "noadmins.db"
    meta = registry.build(raw, DATASET, path)
    assert meta["with_administrators"] == "0" and meta["administrators"] == "0"


def test_a_new_load_replaces_the_old_one(raw, db_path):
    newer = onrc.Dataset("firme-02-11-2026", "2026-11-02", {})
    c = registry.connect(db_path)          # a reader open during the swap
    registry.build(raw, newer, db_path)
    assert c.execute("SELECT value FROM meta WHERE key = 'dataset'").fetchone()[0] \
        == DATASET.name
    c.close()
    assert registry.status(db_path)["dataset"] == "firme-02-11-2026"


def test_progress_is_reported(raw, tmp_path):
    seen = []
    registry.build(raw, DATASET, tmp_path / "p.db", progress=lambda s, n: seen.append(s))
    assert {"firms", "statuses", "activities", "administrators", "search index"} <= set(seen)


# ── Searching ──

def test_activity_word_finds_firms_by_class_then_by_name(con):
    found = registry.search(con, "dentist BV")
    # Authorised for dental practice first, alphabetically; then the one
    # that only says dentist in its name.
    assert names(found) == ["DINTE TEST SRL", "TESTESCU ANA PERSOANĂ FIZICĂ AUTORIZATĂ",
                            "ZÂMBET EXEMPLU SRL", "DENTIST DOAR NUME SRL"]
    assert found["total"] == 4
    page = registry.search(con, "dentist BV", limit=1, offset=3)
    assert names(page) == ["DENTIST DOAR NUME SRL"] and page["total"] == 4


def test_the_same_with_county_names_codes_and_accents(con):
    expected = names(registry.search(con, "dentist BV"))
    for query in ("dentist brasov", "Dentist Brașov", "DENTIST Bv"):
        assert names(registry.search(con, query)) == expected, query
    # An everyday word for the class, not in any firm's name.
    assert names(registry.search(con, "stomatologie bv")) == expected[:3]
    assert names(registry.search(con, "dentist CJ")) == ["CLUJ EXEMPLU SRL"]
    assert names(registry.search(con, "dentist cluj")) == ["CLUJ EXEMPLU SRL"]
    assert registry.search(con, "dentist B")["total"] == 0


def test_codes_in_every_spelling(con):
    both = ["CLUJ EXEMPLU SRL", "DINTE TEST SRL", "TESTESCU ANA PERSOANĂ FIZICĂ AUTORIZATĂ",
            "ZÂMBET EXEMPLU SRL"]
    assert names(registry.search(con, "8623")) == both
    assert names(registry.search(con, "86.23")) == both
    assert names(registry.search(con, "8623/3")) == ["DINTE TEST SRL"]
    # E's 8623 is pre-2008 (another activity then): never matched.
    assert "VECHI EXEMPLU SRL" not in names(registry.search(con, "8623", all_statuses=True))


def test_operating_by_default(con):
    assert registry.search(con, "8623 BV")["total"] == 3
    every = names(registry.search(con, "8623 BV", all_statuses=True))
    assert "INCHIS EXEMPLU SRL" in every and "DIZOLVAT EXEMPLU SRL" in every
    row = registry.search(con, "inchis", all_statuses=True)["rows"][0]
    assert row["operating"] is False and row["status"]


def test_exclusions_and_dropped_letters(con):
    assert "ZÂMBET EXEMPLU SRL" not in names(registry.search(con, "dentist BV -zambet"))
    assert registry.search(con, "dentist BV -bv")["total"] == 4      # -bv: a county
    assert registry.search(con, "dentist -BV")["total"] == 1         # Cluj only
    # "S.R.L." is single letters: dropped rather than matching everything.
    assert registry.search(con, "dinte S.R.L.")["total"] == 1


def test_name_and_town_words(con):
    assert names(registry.search(con, "cristian")) == ["DINTE TEST SRL"]
    assert names(registry.search(con, "zambet")) == ["ZÂMBET EXEMPLU SRL"]
    assert registry.search(con, "")["total"] == 0
    assert registry.search(con, "nimicdeloc")["total"] == 0


def test_rows_carry_activities_in_words_matched_first(con):
    row = registry.search(con, "dentist zambet")["rows"][0]
    assert row["cui"] == "90000017" and row["reg"] == "J08/100/2010"
    assert row["county"] == "Brașov" and row["county_code"] == "BV"
    assert row["locality"] == "Brașov"
    assert row["address"] == "Str. Exemplului 12, 500123"
    assert row["operating"] is True and row["status"] == "funcțiune"
    assert [(a["code"], a["matched"]) for a in row["activities"]] == [
        ("8623", True), ("8622", False)]
    assert row["activities"][0]["name_en"] == "Dental practice"
    pfa = registry.search(con, "testescu")["rows"][0]
    # Its registered office is kept; the personal numeric code never is.
    assert pfa["address"] == "Str. Casei 9, 500999" and pfa["cui"] == ""
    assert CNP not in str(pfa)


def test_explicit_filters_as_import_onrc_gives_them(con):
    q = registry.resolve(con, classes=onrc.parse_classes("kinetoterapeut"),
                         counties=["Brașov"])
    rows = registry.firm_rows(con, registry.matching_ids(con, q), q)
    assert sorted(r["cui"] for r in rows) == ["90000082", "90000090", "90000104"]
    q = registry.resolve(con, classes=onrc.parse_classes("dentist"), localities=["Cristian"])
    assert [r["cui"] for r in registry.firm_rows(con, registry.matching_ids(con, q))] \
        == ["90000025"]
    q = registry.resolve(con, counties=["Cluj"])
    assert len(registry.matching_ids(con, q)) == 1


# ── Pulling into the working database ──

@pytest.mark.asyncio
async def test_pull_saves_like_import_onrc(state, con):
    q = registry.resolve(con, "dentist BV")
    report = await runs.registry_pull(state, con, q)
    assert not report.errors
    assert (report.new_companies, report.known_companies) == (4, 0)
    a = await state.get_company_by_cui("90000017")
    assert a.external_id == "onrc:J08/100/2010" and a.source == "registry:onrc"
    assert a.industry == "Dental practice" and a.location == "Brașov, Romania"
    assert a.address == "Str. Exemplului 12, 500123" and a.source_url == DATASET.page
    # ACTIVITY_CODE only for the classes the words named; the firm matched
    # by its name alone gets none.
    assert obs(state, "ACTIVITY_CODE") == ["8623/2", "8623/2", "8623/3"]
    assert obs(state, "CONTACT_FOUND") == [
        "Andrei-Mihai Doi — Administrator", "Maria Testescu — Administrator"]
    total, rows = await state.search_companies("8622")
    assert [r["cui"] for r in rows] == ["90000017"]

    again = await runs.registry_pull(state, con, q)
    assert (again.new_companies, again.known_companies) == (0, 4)
    assert again.observations == 0


@pytest.mark.asyncio
async def test_pull_merges_on_legal_identity_only(state, con):
    known = await state.add_company(Company(name="Clinica Zâmbet", cui="90000017",
                                            source="discover:osm"))
    other = await state.add_company(Company(name="Alt Zâmbet",
                                            domain="zambet-exemplu.example.ro"))
    report = await runs.registry_pull(state, con, registry.resolve(con, "8623 BV"))
    assert report.known_companies == 1 and report.new_companies == 2
    merged = await state.get_company_by_cui("90000017")
    assert merged.id == known and merged.name == "Clinica Zâmbet"
    assert merged.domain == ""                       # another business holds it
    assert report.shared_domain == 1
    assert (await state.get_company_by_domain("zambet-exemplu.example.ro")).id == other


@pytest.mark.asyncio
async def test_pull_limit_dry_run_and_gate(state, con):
    q = registry.resolve(con, "dentist BV")
    dry = await runs.registry_pull(state, con, q, dry_run=True)
    assert dry.new_companies == 4
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    for code in ("ACTIVITY_CODE", "CONTACT_FOUND", "DECISION_MAKER_TITLE"):
        await state.set_signal_status(code, "proposed")
    report = await runs.registry_pull(state, con, q, limit=2, with_administrators=False)
    assert report.new_companies == 2 and report.observations == 0
    assert report.unconfirmed == ["ACTIVITY_CODE"]


@pytest.mark.asyncio
async def test_catch_all_through_the_registry(state, con):
    q = registry.resolve(con, classes=onrc.parse_classes("kinetoterapeut"),
                         counties=["Brașov"])
    report = await runs.registry_pull(state, con, q)
    assert report.catch_all_only == 1
    assert (await state.get_company_by_cui("90000104")).industry == "Physiotherapy"


@pytest.mark.asyncio
async def test_pull_is_quick_per_company(state, tmp_path):
    # 600 invented firms: one batch-sized transaction each SAVE_BATCH, not a
    # connection and a commit per write.
    rows = [CAEN[0]] + [(f"J08/{i}/2020", "8623", "3") for i in range(600)]
    status = [STATUS[0]] + [(f"J08/{i}/2020", "1048", "") for i in range(600)]
    firms = [FIRMS[0]] + [firm(f"EXEMPLU {i} SRL", str(91000000 + i), f"J08/{i}/2020",
                               "SRL", "Braşov", "Municipiul Braşov") for i in range(600)]
    folder = tmp_path / "many"
    folder.mkdir()
    raw = {"caen": write(folder / "c.csv", rows), "status": write(folder / "s.csv", status),
           "firms": write(folder / "f.csv", firms)}
    path = tmp_path / "many.db"
    registry.build(raw, DATASET, path)
    con = registry.connect(path)
    started = time.monotonic()
    report = await runs.registry_pull(state, con, registry.resolve(con, "8623"))
    per_company = (time.monotonic() - started) / 600
    assert report.new_companies == 600
    assert per_company < 0.010, per_company


# ── The command line ──

def run_cli(monkeypatch, capsys, *argv):
    from harvey import cli
    monkeypatch.setattr(sys, "argv", ["harvey", *argv])
    cli.main()
    return capsys.readouterr().out


def test_cli_status_and_search(monkeypatch, capsys, db_path):
    import json

    out = run_cli(monkeypatch, capsys, "registry", "status", "--registry", str(db_path))
    assert DATASET.name in out and "11 operating" in out
    out = run_cli(monkeypatch, capsys, "registry", "search", "dentist", "BV",
                  "--registry", str(db_path))
    assert "4 firms match" in out and "ZÂMBET EXEMPLU SRL" in out
    out = run_cli(monkeypatch, capsys, "registry", "search", "8623/3", "--json",
                  "--registry", str(db_path))
    data = json.loads(out)
    assert data["total"] == 1 and data["firms"][0]["cui"] == "90000025"


def test_cli_without_a_registry(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exit_:
        run_cli(monkeypatch, capsys, "registry", "search", "dentist",
                "--registry", str(tmp_path / "none.db"))
    assert exit_.value.code == 2


def test_cli_load_uses_files_already_downloaded(monkeypatch, capsys, raw, tmp_path):
    # A named dataset whose files are on disk loads without data.gov.ro.
    folder = tmp_path / "onrc" / DATASET.date
    folder.mkdir(parents=True)
    for role, path in raw.items():
        (folder / onrc.FILES[role]).write_bytes(path.read_bytes())
    monkeypatch.setattr(onrc, "MIN_BYTES", {})
    out = run_cli(monkeypatch, capsys, "registry", "load", "--dataset", DATASET.name,
                  "--data-dir", str(tmp_path / "onrc"),
                  "--registry", str(tmp_path / "loaded.db"))
    assert "14 registrations" in out
    # Every downloaded file is still there.
    assert len(list(folder.iterdir())) == 4


def test_cli_import_onrc_is_a_shortcut(monkeypatch, capsys, raw, db_path, tmp_path):
    import harvey.state as state_mod

    async def latest(client):
        return DATASET
    monkeypatch.setattr(onrc, "latest_dataset", latest)
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    out = run_cli(monkeypatch, capsys, "import-onrc", "dentist", "--county", "BV",
                  "--dry-run", "--registry", str(db_path))
    assert "3 match" in out and "3 would be new" in out


def test_counting_stops_at_the_cap(con, monkeypatch):
    monkeypatch.setattr(registry, "COUNT_CAP", 2)
    found = registry.search(con, "8623")
    assert found["total"] == 2 and found["more"] is True
    assert len(found["rows"]) == 4                  # the page isn't capped
    exact = registry.search(con, "8623/3")
    assert exact["total"] == 1 and exact["more"] is False


def test_an_everyday_phrase_resolves_as_one(con):
    units = registry._phrase_units(["service", "auto", "brasov"])
    assert units[0][0] == ["service", "auto"] and units[1] == "brasov"
    assert ("4520", 2) in units[0][1] and ("9531", 3) in units[0][1]
    q = registry.resolve(con, "service auto BV")
    # The phrase's own classes, not every class with "service" or "auto".
    assert set(q.keyword_classes) == set(units[0][1]) and q.name_classes == []
    q = registry.resolve(con, "medic de familie")
    assert ("8621", 3) in q.keyword_classes and len(q.keyword_classes) <= 4


# ── Villages, places first, one load at a time, guards ──

# A dental practice in a village (found by its commune too), and one in
# Cluj whose name holds a town's name.
PLACE_FIRMS = [
    firm("SAT EXEMPLU SRL", "90000155", "J08/993/2019", "SRL", "Braşov",
         "Sat Purcăreni, Comuna Tărlungeni"),
    firm("CRISTIAN EXEMPLU SRL", "90000163", "J12/994/2019", "SRL", "Cluj",
         "Municipiul Cluj-Napoca"),
]
PLACE_CAEN = [("J08/993/2019", "8623", "3"), ("J12/994/2019", "8623", "3")]
PLACE_STATUS = [("J08/993/2019", "1048", ""), ("J12/994/2019", "1048", "")]


@pytest.fixture
def places_con(tmp_path):
    folder = tmp_path / "places"
    folder.mkdir()
    raw = {"caen": write(folder / "c.csv", CAEN + PLACE_CAEN),
           "status": write(folder / "s.csv", STATUS + PLACE_STATUS),
           "firms": write(folder / "f.csv", FIRMS + PLACE_FIRMS)}
    path = tmp_path / "places.db"
    registry.build(raw, DATASET, path)
    c = registry.connect(path)
    yield c
    c.close()


def test_a_village_is_found_by_its_commune(places_con):
    assert registry.places("Sat Purcăreni, Comuna Tărlungeni") == "Purcăreni Tărlungeni"
    assert registry.places("Municipiul Braşov") == "Brașov"
    for where in ("Tărlungeni", "Purcăreni", "Comuna Tărlungeni"):
        q = registry.resolve(places_con, counties=["Brașov"], localities=[where])
        rows = registry.firm_rows(places_con, registry.matching_ids(places_con, q))
        assert [r["cui"] for r in rows] == ["90000155"], where
        # Shown as the village itself.
        assert rows[0]["locality"] == "Purcăreni"
    assert names(registry.search(places_con, "dentist tarlungeni")) == ["SAT EXEMPLU SRL"]


def test_firms_in_the_named_place_come_first(places_con):
    # Alphabetically CRISTIAN EXEMPLU (Cluj) comes before DINTE TEST (in
    # Cristian); the town ranks first.
    assert names(registry.search(places_con, "8623 cristian")) == [
        "DINTE TEST SRL", "CRISTIAN EXEMPLU SRL"]
    page = registry.search(places_con, "8623 cristian", limit=1, offset=1)
    assert names(page) == ["CRISTIAN EXEMPLU SRL"] and page["total"] == 2


def test_one_load_at_a_time(raw, db_path):
    from harvey.pidfile import _try_lock, _unlock

    newer = onrc.Dataset("firme-02-11-2026", "2026-11-02", {})
    with open(str(db_path) + ".lock", "a+", encoding="utf-8") as held:
        assert _try_lock(held)
        try:
            with pytest.raises(registry.RegistryBusy):
                registry.build(raw, newer, db_path)
        finally:
            _unlock(held)
    # The loaded registry is untouched, and a later load goes through.
    assert registry.status(db_path)["dataset"] == DATASET.name
    registry.build(raw, newer, db_path)
    assert registry.status(db_path)["dataset"] == newer.name


@pytest.mark.asyncio
async def test_a_failed_batch_takes_its_counts_with_it(state, con, monkeypatch):
    from harvey.state import CompanyBatch

    async def broken(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(CompanyBatch, "set_company_activities", broken)
    report = await runs.registry_pull(state, con, registry.resolve(con, "dentist BV"))
    assert report.errors
    assert (report.new_companies, report.administrators, report.observations) == (0, 0, 0)
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0


def test_cli_pull_asks_for_a_limit_on_big_matches(monkeypatch, capsys, db_path, tmp_path):
    import harvey.state as state_mod
    from harvey import cli

    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(cli, "PULL_WITHOUT_LIMIT", 2)
    with pytest.raises(SystemExit) as exit_:
        run_cli(monkeypatch, capsys, "registry", "pull", "8623", "--registry", str(db_path))
    assert exit_.value.code == 2
    assert "--limit" in capsys.readouterr().out
    out = run_cli(monkeypatch, capsys, "registry", "pull", "8623", "--dry-run",
                  "--registry", str(db_path))
    assert "4 would be new" in out and "saved 4" not in out
    out = run_cli(monkeypatch, capsys, "registry", "pull", "8623", "--limit", "2",
                  "--registry", str(db_path))
    assert "2 are new" in out


def test_cli_offline_uses_the_newest_download(monkeypatch, capsys, raw, tmp_path):
    import httpx

    import harvey.state as state_mod

    async def offline(client):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(onrc, "latest_dataset", offline)
    monkeypatch.setattr(onrc, "MIN_BYTES", {})
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    folder = tmp_path / "onrc" / DATASET.date
    folder.mkdir(parents=True)
    for role, path in raw.items():
        (folder / onrc.FILES[role]).write_bytes(path.read_bytes())
    out = run_cli(monkeypatch, capsys, "import-onrc", "dentist", "--county", "BV",
                  "--dry-run", "--data-dir", str(tmp_path / "onrc"),
                  "--registry", str(tmp_path / "offline.db"))
    assert f"using {DATASET.name}, already downloaded" in out and "3 match" in out
    # Nothing downloaded at all: a plain message, not a network traceback.
    with pytest.raises(SystemExit) as exit_:
        run_cli(monkeypatch, capsys, "import-onrc", "dentist", "--county", "BV",
                "--data-dir", str(tmp_path / "empty"),
                "--registry", str(tmp_path / "none.db"))
    assert exit_.value.code == 2
    assert "no complete dataset is downloaded" in capsys.readouterr().out
