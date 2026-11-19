"""Find contacts that starts from the trade registry: ticked rows or the
Registry page's filters get their records when a run takes them
(launch.registry_selection), the new firms are checked at ANAF first, and
each firm's own job takes its registry entry (its declared website, the
registers' phones) before anything is searched for it (finder._registry_entry,
overlay.store.Store.take_from_registry). Nothing copies the registry up
front.

The registry is the one tests/test_registry.py builds plus five salons of
its own. Every site, search and ANAF answer is a fake; every company,
person, number and address is invented.
"""

import asyncio
import json
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.state as state_mod
from harvey import finder, launch, registry
from harvey.collectors import onrc
from harvey.config import ComplianceConfig, EnvConfig
from harvey.models import Company
from harvey.modules import job_kinds, runs, switches
from harvey.modules.ro.anaf import service as anaf
from harvey.modules.worker import wait_for_batch
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim
from tests.test_anaf import answer, cui
from tests.test_contact_finder import ENV, Fakes, _contact, _site, _spend
from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, firm, write
from tests.test_registry import EXTRA_CAEN, EXTRA_FIRMS, EXTRA_STATUS

# Five salons in Brașov: two declare the same website, one a website on the
# do-not-contact list, one has a phone in the taxpayer file, one has nothing.
# (Their CUIs carry a valid check digit, so ANAF is asked about them.)
DOI_CUI, UNU_CUI, BARRED_CUI, SIMPLU_CUI, TELEFON_CUI = (cui(f"90000{n}") for n in range(20, 25))
SALONS = [
    firm("COMUN DOI EXEMPLU SRL", DOI_CUI, "J08/802/2020", "SRL", "Braşov",
         "Municipiul Braşov", web="comun-exemplu.example.ro"),
    firm("COMUN UNU EXEMPLU SRL", UNU_CUI, "J08/801/2020", "SRL", "Braşov",
         "Municipiul Braşov", web="www.comun-exemplu.example.ro"),
    firm("INTERZIS EXEMPLU SRL", BARRED_CUI, "J08/803/2020", "SRL", "Braşov",
         "Municipiul Braşov", web="www.interzis-exemplu.example.ro"),
    firm("SIMPLU EXEMPLU SRL", SIMPLU_CUI, "J08/805/2020", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("TELEFON EXEMPLU SRL", TELEFON_CUI, "J08/804/2020", "SRL", "Braşov",
         "Municipiul Braşov"),
]
SALON_REGS = [f[2] for f in SALONS]
SALON_CAEN = [(reg, "9602", "2") for reg in SALON_REGS]
SALON_STATUS = [(reg, "1048", "") for reg in SALON_REGS]
SALON_FILTER = {"words": "9602", "county": "BV"}
# The firm of tests/test_onrc.py that declares a website and has administrators.
ZAMBET, ZAMBET_CUI, ZAMBET_SITE = "J08/100/2010", 90000017, "zambet-exemplu.example.ro"
SOLE_TRADER = "F08/800/2018"            # no CUI: ANAF isn't asked about it
DOI, UNU, SIMPLU, TELEFON = "J08/802/2020", "J08/801/2020", "J08/805/2020", "J08/804/2020"
TAXPAYERS = [(int(TELEFON_CUI), "TELEFON EXEMPLU SRL", 1, 1, 0, "0268000104"),
             (ZAMBET_CUI, "ZAMBET EXEMPLU SRL", 1, 1, 0, "0268000103")]
SHARED = "comun-exemplu.example.ro"
BARRED = "interzis-exemplu.example.ro"


def run(coro):
    return asyncio.run(coro)


def build(folder) -> None:
    """registry.db in ``folder``, loaded as `harvey registry load` leaves it."""
    raw = folder / "raw"
    raw.mkdir()
    registry.build({
        "caen": write(raw / onrc.FILES["caen"], CAEN + EXTRA_CAEN + SALON_CAEN),
        "status": write(raw / onrc.FILES["status"], STATUS + EXTRA_STATUS + SALON_STATUS),
        "firms": write(raw / onrc.FILES["firms"], FIRMS + EXTRA_FIRMS + SALONS),
        "people": write(raw / onrc.FILES["people"], PEOPLE),
    }, DATASET, folder / "registry.db")


async def opened(folder) -> StateManager:
    sm = StateManager(str(folder / "harvey.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    return sm


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A folder with the registry loaded and no harvey.db yet: Companies
    would be empty."""
    build(tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(job_kinds, "_config", lambda: None)
    monkeypatch.setattr(finder, "_env", lambda: dict(ENV))
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: EnvConfig(serper_api_key="k"))
    return tmp_path


@pytest_asyncio.fixture
async def state(world):
    yield await opened(world)


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")      # nothing runs: just the queue
    monkeypatch.setattr(dash, "DB_PATH", world / "harvey.db")
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db = world / "harvey.db"
        yield c


def counted(selected: int, existing: int, **rest) -> dict:
    """A registry selection's counts as an answer gives them: the ones
    named, nothing left out unless said."""
    return {"selected": selected, "existing": existing, "excluded": 0, "unkeyable": 0, **rest}


def firms(db) -> int:
    return slim.one(db, "SELECT COUNT(*) FROM firm")


def record_of(db, reg: str) -> str:
    return str(slim.one(db, "SELECT fi.firm FROM firm_ident fi JOIN ident_scheme s "
                            "ON s.id = fi.scheme WHERE s.name = 'ro.onrc_reg' AND fi.key = ?", reg))


def queued(db) -> list[tuple[str, dict]]:
    return [(kind, json.loads(params)) for kind, params in slim.q(
        db, "SELECT kind, params_json FROM jobs ORDER BY rowid")]


def nothing_copied(db) -> bool:
    """Every firm is a bare record: its row and identifiers, nothing of the
    registry's on it, no source, contact, person or description."""
    bare = slim.q(db, "SELECT COUNT(*) FROM firm WHERE website IS NOT NULL OR domain IS NOT NULL "
                      "OR n_contacts != 0 OR display_name IS NOT NULL")[0][0] == 0
    return bare and all(slim.one(db, f"SELECT COUNT(*) FROM {table}") == 0 for table in (
        "firm_source", "contact", "link", "link_evidence", "person", "local_firm", "search",
        "obs"))


def site_columns(db, reg: str) -> tuple:
    return slim.q(db, "SELECT website, domain, website_trust FROM firm WHERE id = ?",
                  int(record_of(db, reg)))[0]


def website_source(db, reg: str) -> str | None:
    return slim.one(db, "SELECT s.name FROM firm f JOIN source s ON s.id = f.website_source "
                        "WHERE f.id = ?", int(record_of(db, reg)))


def sources(db, reg: str) -> list[tuple]:
    return slim.q(db, "SELECT s.name, fs.ref FROM firm_source fs JOIN source s "
                      "ON s.id = fs.source WHERE fs.firm = ?", int(record_of(db, reg)))


def phone_sources(db) -> list[tuple]:
    return sorted(slim.q(db, "SELECT c.value, s.name FROM link_evidence e JOIN contact c "
                             "ON c.id = e.contact JOIN contact_kinds k ON k.id = c.kind "
                             "JOIN source s ON s.id = e.source WHERE k.kind = 'phone'"))


# ── A run on a database with no firm at all ──

def test_ticked_rows_get_records_and_jobs_and_nothing_is_copied(client):
    body = {"registry": {"regs": [ZAMBET, SOLE_TRADER, TELEFON, "J99/1/1900"]},
            "max_spend": 0.5}
    r = client.post("/api/contacts/find", json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    # A number the registry doesn't have selects nothing.
    assert got["registry"] == counted(3, 0, created=3, left=0)
    assert got["companies"] == 3 and got["max_spend"] == 0.5
    assert firms(client.db) == 3 and nothing_copied(client.db)
    # The two with a CUI are checked at ANAF first; the sole trader, whom
    # ANAF isn't asked about, is searched at once.
    checked = sorted([record_of(client.db, ZAMBET), record_of(client.db, TELEFON)])
    assert got["checks_first"] == {"jobs": 1, "companies": 2} and got["queued"] == 1
    (check,) = [p for kind, p in queued(client.db) if kind == "id_check"]
    assert check["then_find"] is True and sorted(check["company_ids"]) == checked
    assert [p for kind, p in queued(client.db) if kind == "find_contacts"] == [
        {"company_id": record_of(client.db, SOLE_TRADER)}]
    assert slim.q(client.db, "SELECT kind, max_spend FROM job_batches") == [
        ("find_contacts", 0.5)]
    # The register signals are counted again over the firms in Harvey, as
    # after a pull that saved firms.
    assert [kind for kind, _ in queued(client.db)] == [
        "id_check", "register_counts", "find_contacts"]

    # The check done, it queues the run's own search of each: under the
    # run's cap, whether or not a website is on file yet.
    async def checked_now():
        s = await opened(client.db.parent)
        return await job_kinds.enqueue_finder_follow_ups(s, checked,
                                                         batch_id=got["batch_id"])

    assert run(checked_now()) == 2
    searches = [p for kind, p in queued(client.db) if kind == "find_contacts"]
    assert sorted(p["company_id"] for p in searches) == sorted(
        [*checked, record_of(client.db, SOLE_TRADER)])
    assert not any(p.get("free_only") or p.get("website") for p in searches)
    assert slim.q(client.db, "SELECT DISTINCT batch_id FROM jobs") == [(got["batch_id"],)]


def test_the_registry_pages_filter_gets_records_and_jobs(client):
    r = client.post("/api/contacts/find", json={"registry": SALON_FILTER})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["registry"] == counted(5, 0, created=5, left=0)
    assert got["companies"] == 5 and firms(client.db) == 5 and nothing_copied(client.db)
    assert got["checks_first"] == {"jobs": 1, "companies": 5} and got["queued"] == 0
    assert sorted(slim.q(client.db, "SELECT key FROM firm_ident fi JOIN ident_scheme s "
                                    "ON s.id = fi.scheme WHERE s.name = 'ro.onrc_reg'")) == \
        sorted((reg,) for reg in SALON_REGS)
    # The same filter again makes nothing, and searches none of them: each
    # one's search already waits behind the first run's check.
    again = client.post("/api/contacts/find", json={"registry": SALON_FILTER})
    assert again.status_code == 409 and again.json()["code"] == "duplicate", again.text
    assert again.json()["registry"] == counted(5, 5, created=0, left=0)
    assert again.json()["job"]["kind"] == "id_check"
    assert firms(client.db) == 5
    assert [kind for kind, _ in queued(client.db) if kind == "find_contacts"] == []


def test_a_later_run_leaves_the_firms_an_earlier_runs_check_waits_for(client):
    """The firms left for a later run: that run takes the earlier run's new
    firms too (the newest records), but their search waits behind the
    earlier run's check, so it is neither queued at once nor twice."""
    first = client.post("/api/contacts/find", json={"registry": SALON_FILTER,
                                                    "limit": 2}).json()
    (earlier,) = [p["company_ids"] for kind, p in queued(client.db) if kind == "id_check"]
    assert first["checks_first"] == {"jobs": 1, "companies": 2} and len(earlier) == 2
    later = client.post("/api/contacts/find", json={"registry": SALON_FILTER}).json()
    assert later["registry"] == counted(5, 2, created=3, left=0)
    assert later["companies"] == 5 and later["checks_first"] == {"jobs": 1, "companies": 3}
    assert later["queued"] == 0 and later["already_waiting"] == 2
    assert [kind for kind, _ in queued(client.db) if kind == "find_contacts"] == []

    async def checked_now():
        s = await opened(client.db.parent)
        for batch, params in slim.q(client.db, "SELECT batch_id, params_json FROM jobs "
                                               "WHERE kind = 'id_check'"):
            await job_kinds.enqueue_finder_follow_ups(s, json.loads(params)["company_ids"],
                                                      batch_id=batch)

    run(checked_now())
    # Each firm is searched once, by the run whose check it waited for.
    searches = slim.q(client.db, "SELECT json_extract(params_json, '$.company_id'), batch_id "
                                 "FROM jobs WHERE kind = 'find_contacts'")
    assert sorted(c for c, _ in searches) == sorted(
        record_of(client.db, reg) for reg in SALON_REGS)
    assert {c for c, b in searches if b == first["batch_id"]} == set(earlier)


def test_a_forced_run_searches_its_checked_firms_since_its_start(client):
    got = client.post("/api/contacts/find", json={"registry": {"regs": [ZAMBET]},
                                                  "force": True}).json()
    assert got["checks_first"] == {"jobs": 1, "companies": 1}
    cid = record_of(client.db, ZAMBET)

    async def checked_now():
        s = await opened(client.db.parent)
        return await job_kinds.enqueue_finder_follow_ups(s, [cid], batch_id=got["batch_id"])

    assert run(checked_now()) == 1
    (search,) = [p for kind, p in queued(client.db) if kind == "find_contacts"]
    assert search["company_id"] == cid and search["since"]


# ── The estimate ──

def test_the_estimate_counts_and_writes_nothing(client):
    body = {"registry": SALON_FILTER}
    est = client.post("/api/contacts/find/estimate", json=body)
    assert est.status_code == 200, est.text
    e = est.json()
    assert e["registry"] == counted(5, 0, to_create=5, left=0, note=launch.REGISTRY_NOTE)
    # Nothing is known of firms with no record: the figures are for none.
    assert (e["companies"], e["with_site"], e["to_look_up"]) == (0, 0, 0)
    assert firms(client.db) == 0
    for table in ("firm_ident", "jobs", "job_batches"):
        assert slim.one(client.db, f"SELECT COUNT(*) FROM {table}") == 0, table
    # Two get a record (a run of ticked rows): the estimate sees those two.
    client.post("/api/contacts/find", json={"registry": {"regs": [SIMPLU, TELEFON]}})
    before = (firms(client.db), slim.one(client.db, "SELECT COUNT(*) FROM jobs"),
              slim.one(client.db, "SELECT n FROM revision WHERE name = 'identity'"))
    # It answers while another writer is in the middle of its write: the
    # estimate takes no write lock.
    writer = sqlite3.connect(client.db, isolation_level=None, timeout=1)
    writer.execute("BEGIN IMMEDIATE")
    try:
        e = client.post("/api/contacts/find/estimate", json=body).json()
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert e["registry"] == counted(5, 2, to_create=3, left=0, note=launch.REGISTRY_NOTE)
    assert (e["companies"], e["to_look_up"]) == (2, 2)
    assert (firms(client.db), slim.one(client.db, "SELECT COUNT(*) FROM jobs"),
            slim.one(client.db, "SELECT n FROM revision WHERE name = 'identity'")) == before


def test_the_estimate_counts_a_records_declared_website_as_a_website(client):
    """Records an earlier run made whose searches haven't started hold no
    website yet: one whose registry entry declares a website is counted
    with it (its search reads it, free), never as a paid lookup."""
    client.post("/api/contacts/find", json={"registry": {"regs": [ZAMBET, SIMPLU]}})
    assert site_columns(client.db, ZAMBET) == (None, None, None)
    e = client.post("/api/contacts/find/estimate",
                    json={"registry": {"regs": [ZAMBET, SIMPLU]}}).json()
    assert e["registry"] == counted(2, 2, to_create=0, left=0, note=launch.REGISTRY_NOTE)
    assert (e["companies"], e["with_site"], e["to_look_up"]) == (2, 1, 1)
    assert "registry entry declares one" in e["registry"]["note"]


def test_one_read_of_the_selection_a_request(client, monkeypatch):
    from harvey.modules.ro.onrc import queries as onrc_queries

    read = onrc_queries.Adapter.select_firms
    reads = []

    def counting(self, *a, **kw):
        reads.append(1)
        return read(self, *a, **kw)

    monkeypatch.setattr(onrc_queries.Adapter, "select_firms", counting)
    client.post("/api/contacts/find/estimate", json={"registry": SALON_FILTER})
    assert len(reads) == 1
    got = client.post("/api/contacts/find", json={"registry": SALON_FILTER}).json()
    assert len(reads) == 2 and got["companies"] == 5


# ── What the selection leaves out ──

def test_a_firm_whose_declared_website_is_on_the_do_not_contact_list_gets_no_record(
        client, monkeypatch):
    # The list as people write it: a page of the site, a parent domain that
    # isn't one of the salons' (no label boundary), an address (it bars a
    # mailbox, not the firm).
    listed = ["https://www.interzis-exemplu.example.ro/contact", "exemplu.example.ro",
              f"ana@{SHARED}"]
    monkeypatch.setattr(job_kinds, "_config", lambda: SimpleNamespace(
        compliance=ComplianceConfig(do_not_contact=listed),
        spend=SimpleNamespace(per_day_usd=None, per_month_usd=None)))
    est = client.post("/api/contacts/find/estimate", json={"registry": SALON_FILTER}).json()
    assert est["registry"] == counted(4, 0, to_create=4, left=0, excluded=1,
                                      note=launch.REGISTRY_NOTE)
    got = client.post("/api/contacts/find", json={"registry": SALON_FILTER}).json()
    assert got["registry"] == counted(4, 0, created=4, left=0, excluded=1)
    assert firms(client.db) == 4 and record_of(client.db, "J08/803/2020") == "None"
    # No ANAF request names it: it is in no check.
    checked = [i for kind, p in queued(client.db) if kind == "id_check"
               for i in p["company_ids"]]
    assert len(checked) == 4
    # Ticked on its own: nothing is made or queued for it.
    alone = client.post("/api/contacts/find", json={"registry": {"regs": ["J08/803/2020"]}})
    assert alone.json()["registry"] == counted(0, 0, created=0, left=0, excluded=1)
    assert alone.json()["queued"] == 0 and firms(client.db) == 4


def test_a_registration_no_key_reaches_is_counted(client, world):
    """A registry row whose CUI isn't one Harvey can key by is never a
    record: the selection says how many such rows it held."""
    reg = sqlite3.connect(world / "registry.db")
    reg.execute("INSERT INTO firms (reg, name, cui, county, locality) VALUES "
                "('J08/806/2020', 'STRAMB EXEMPLU SRL', 'RO 90000x', 'Braşov', "
                "'Municipiul Braşov')")
    reg.commit()
    reg.close()
    body = {"registry": {"regs": [ZAMBET, "J08/806/2020"]}}
    est = client.post("/api/contacts/find/estimate", json=body).json()
    assert est["registry"] == counted(1, 0, to_create=1, left=0, unkeyable=1,
                                      note=launch.REGISTRY_NOTE)
    got = client.post("/api/contacts/find", json=body).json()
    assert got["registry"] == counted(1, 0, created=1, left=0, unkeyable=1)
    assert firms(client.db) == 1


# ── The cap ──

def test_the_cap_leaves_the_rest_for_a_later_run(client):
    body = {"registry": SALON_FILTER, "limit": 2}
    est = client.post("/api/contacts/find/estimate", json=body).json()
    assert est["registry"] == counted(5, 0, to_create=2, left=3, note=launch.REGISTRY_NOTE)
    first = client.post("/api/contacts/find", json=body).json()
    assert first["registry"] == counted(5, 0, created=2, left=3)
    assert first["companies"] == 2 and firms(client.db) == 2
    taken = {p_ for kind, p in queued(client.db) if kind == "id_check" for p_ in p["company_ids"]}
    # The next run makes the next two, and takes those: the newest records.
    second = client.post("/api/contacts/find", json=body).json()
    assert second["registry"] == counted(5, 2, created=2, left=1)
    assert second["companies"] == 2 and firms(client.db) == 4
    now = {p_ for kind, p in queued(client.db) if kind == "id_check" for p_ in p["company_ids"]}
    assert len(now - taken) == 2 and min(map(int, now - taken)) > max(map(int, taken))
    # The estimate leaves room for the one still to make.
    est = client.post("/api/contacts/find/estimate", json=body).json()
    assert est["registry"]["to_create"] == 1 and est["registry"]["left"] == 0
    assert est["companies"] == 1
    third = client.post("/api/contacts/find", json=body).json()
    assert third["registry"] == counted(5, 4, created=1, left=0)
    assert third["companies"] == 2 and third["checks_first"]["companies"] == 1
    assert firms(client.db) == 5 and nothing_copied(client.db)


# ── What is refused ──

@pytest.mark.parametrize("chosen, says", [
    ({}, "needs an activity, an area or a filter"),
    ({"status": "all"}, "needs an activity, an area or a filter"),
    ({"words": "si"}, "needs an activity, an area or a filter"),
    ({"words": "-dentist", "status": "all"}, "only excludes"),
    ({"words": "-dentist"}, "only excludes"),
    ({"words": "-dentist -frizerie si"}, "only excludes"),
    ({"county": "Atlantis"}, "Atlantis"),
    ({"regs": "J08/100/2010"}, "list of registration numbers"),
    ("dentist", "Registry page's filters"),
])
def test_a_selection_that_says_nothing_is_refused(client, chosen, says):
    for path in ("/api/contacts/find/estimate", "/api/contacts/find"):
        r = client.post(path, json={"registry": chosen})
        assert r.status_code == 400, r.text
        assert says in (r.json().get("error") or r.json().get("message")), r.text
    assert firms(client.db) == 0 and queued(client.db) == []


def test_the_estimates_registry_refusals_carry_a_code_for_the_page(client, world,
                                                                   monkeypatch):
    from harvey.modules.ro.mf_taxpayers import store as tax_store

    only = client.post("/api/contacts/find/estimate", json={"registry": {"words": "-dentist"}})
    assert only.status_code == 400 and only.json()["code"] == "registry_only_excludes"
    # Every registration with only a taxpayer condition: the search index has nothing to match.
    slim.taxpayers_beside(run(opened(world)), TAXPAYERS)
    monkeypatch.setattr(tax_store, "TAXPAYERS_PATH", world / "taxpayers.db")
    every = client.post("/api/contacts/find/estimate",
                        json={"registry": {"status": "all", "employer": "yes"}})
    assert every.status_code == 400, every.text
    assert every.json()["code"] == "registry_every_status" and "isn't taken yet" in every.text
    # Any other refusal: the server's words alone.
    other = client.post("/api/contacts/find/estimate", json={"registry": {"county": "Atlantis"}})
    assert other.status_code == 400 and "code" not in other.json()
    assert firms(client.db) == 0


def test_other_refusals(client, world, monkeypatch):
    both = client.post("/api/contacts/find", json={"registry": SALON_FILTER,
                                                   "company_ids": ["1"]})
    assert both.status_code == 400 and "one of the two" in both.json()["message"]
    # The Contacts page's filters read what Harvey holds: a new record holds nothing.
    narrowed = client.post("/api/contacts/find", json={"registry": SALON_FILTER,
                                                       "filter": {"website": "yes"}})
    assert narrowed.status_code == 400 and "registry's own filters" in narrowed.json()["message"]
    # The registry's module off: nothing is selected from it.
    run(_switch(world, "onrc", False))
    off = client.post("/api/contacts/find", json={"registry": SALON_FILTER})
    assert off.status_code == 400 and "which is off" in off.json()["message"]
    run(_switch(world, "onrc", True))
    # No registry loaded where the page's filters are read from.
    monkeypatch.setattr(registry, "REGISTRY_PATH", world / "none.db")
    missing = client.post("/api/contacts/find/estimate", json={"registry": SALON_FILTER})
    assert missing.status_code == 400 and "harvey registry load" in missing.json()["error"]
    assert firms(client.db) == 0


async def _switch(folder, module: str, on: bool) -> None:
    await switches.set_enabled(await opened(folder), module, on)


# ── A firm's own job takes its registry entry ──

async def _pulled(folder, source, regs) -> StateManager:
    """Another install beside a copy of the same registers, the firms
    brought by a registry pull: what the new records are compared with."""
    folder.mkdir()
    for name in ("registry.db", "taxpayers.db"):
        if (source / name).exists():
            shutil.copy(source / name, folder / name)
    other = await opened(folder)
    con = registry.connect(folder / "registry.db")
    try:
        await runs.registry_pull(other, con, registry.resolve(con, regs=regs))
    finally:
        con.close()
    return other


def _footing(sm, reg: str) -> dict:
    """What the finder left on a firm, as far as a pull's firm can be
    compared with a new record's."""
    cid = record_of(sm.db_path, reg)
    return {"site": site_columns(sm.db_path, reg), "sources": sources(sm.db_path, reg),
            "links": slim.links(sm, cid), "phones": phone_sources(sm.db_path),
            "people": sorted(slim.q(sm, "SELECT name, role FROM person")),
            "searches": [s[:5] for s in slim.searches(sm)]}


@pytest.mark.asyncio
async def test_the_finder_takes_the_registry_entry_then_reads_the_declared_site(
        state, world, monkeypatch):
    slim.taxpayers_beside(state, TAXPAYERS)
    site = _site(f"https://{ZAMBET_SITE}/", "<p>Zâmbet, Brașov</p>",
                 _contact("email", f"office@{ZAMBET_SITE}", url=f"https://{ZAMBET_SITE}/"))
    fakes = Fakes(sites={ZAMBET_SITE: site}).install(monkeypatch)
    assert (await state.ensure_registry_records(regs=[ZAMBET]))["created"] == 1
    cid = record_of(state.db_path, ZAMBET)
    assert site_columns(state.db_path, ZAMBET) == (None, None, None)

    found = await finder.find(state, cid, spend=_spend(cap=True), env=ENV)
    # Its declared website was read, free; nothing was looked up by name.
    assert fakes.read == [(ZAMBET_SITE, True)] and fakes.looked == []
    assert found.steps[0] == "site" and found.website == ZAMBET_SITE
    assert site_columns(state.db_path, ZAMBET) == (f"https://{ZAMBET_SITE}", ZAMBET_SITE, None)
    assert sources(state.db_path, ZAMBET) == [("registry:onrc", f"onrc:{ZAMBET}")]
    assert phone_sources(state.db_path) == [("+40268000103", "mf_taxpayers")]
    # The website it took is the registry's, as a pulled firm's is.
    assert website_source(state.db_path, ZAMBET) == "registry:onrc"
    # Its administrators are the registry's, named when the finder works on it.
    assert sorted(slim.q(state, "SELECT name FROM person")) == [
        ("Andrei-Mihai Doi",), ("Maria Testescu",)]

    # From here on it is a pulled firm: the same as one a pull brought,
    # searched the same way.
    other = await _pulled(world / "pulled", world, [ZAMBET])
    theirs = await finder.find(other, record_of(other.db_path, ZAMBET), spend=_spend(cap=True),
                               env=ENV)
    assert _footing(state, ZAMBET) == _footing(other, ZAMBET)
    assert website_source(other.db_path, ZAMBET) == "registry:onrc"
    mine, pulled = found.as_dict(), theirs.as_dict()
    for d in (mine, pulled):
        d.pop("ms"), d.pop("company_id")
    assert mine == pulled
    # A second search takes nothing from the registry again.
    await finder.find(state, cid, spend=_spend(cap=True), env=ENV)
    assert sources(state.db_path, ZAMBET) == [("registry:onrc", f"onrc:{ZAMBET}")]
    assert fakes.looked == []


def _pairs(sm) -> list[tuple]:
    """(the live firms, the review pairs, the merges) on an install, by
    what each firm is: "listing" or the registry firm's number."""
    def what(fid):
        reg = slim.one(sm.db_path, "SELECT fi.key FROM firm_ident fi JOIN ident_scheme s "
                                   "ON s.id = fi.scheme WHERE s.name = 'ro.onrc_reg' "
                                   "AND fi.firm = ?", fid)
        return reg or "listing"

    live = sorted(what(f) for (f,) in slim.q(sm.db_path, "SELECT id FROM firm WHERE live = 0"))
    pairs = sorted(tuple(sorted((what(a), what(b)))) + (status,) for a, b, status in slim.q(
        sm.db_path, "SELECT a, b, status FROM match_candidate"))
    merged = sorted((what(i), what(v)) for i, v in slim.q(
        sm.db_path, "SELECT id, live FROM firm WHERE live != 0"))
    return [live, pairs, merged]


@pytest.mark.asyncio
@pytest.mark.parametrize("listing, expected", [
    # The declared domain only: a question.
    (dict(name="Alt Nume", website=f"https://{ZAMBET_SITE}", domain=ZAMBET_SITE),
     [["J08/100/2010", "listing"], [("J08/100/2010", "listing", "pending")], []]),
    # The same name in the same town only: a question.
    (dict(name="Zâmbet Exemplu", location="Brașov, Romania"),
     [["J08/100/2010", "listing"], [("J08/100/2010", "listing", "pending")], []]),
    # Both: the same firm, merged into the listing.
    (dict(name="Zâmbet Exemplu", location="Brașov, Romania", website=f"https://{ZAMBET_SITE}",
          domain=ZAMBET_SITE),
     [["listing"], [], [("J08/100/2010", "listing")]]),
])
async def test_a_record_taking_its_entry_meets_the_firms_other_sources_brought(
        state, world, monkeypatch, listing, expected):
    """A firm a listing brought first, then a record made for the same
    registry firm: taking its entry, the record is matched with the firms
    on file as a pull's new firm is, a review pair or a merge."""
    Fakes().install(monkeypatch)
    await state.add_company(Company(source="discover:osm", **listing))
    await state.ensure_registry_records(regs=[ZAMBET])
    took = await state.take_from_registry(record_of(state.db_path, ZAMBET))
    assert took and _pairs(state) == expected
    assert took["merged_into"] == ("" if len(expected[0]) == 2 else
                                   str(slim.one(state.db_path, "SELECT id FROM firm "
                                                               "WHERE live = 0")))
    # As a pull of the same firm leaves the two.
    folder = world / "pulled"
    folder.mkdir()
    shutil.copy(world / "registry.db", folder / "registry.db")
    other = await opened(folder)
    await other.add_company(Company(source="discover:osm", **listing))
    con = registry.connect(folder / "registry.db")
    try:
        await runs.registry_pull(other, con, registry.resolve(con, regs=[ZAMBET]))
    finally:
        con.close()
    assert _pairs(other) == expected


@pytest.mark.asyncio
async def test_only_a_record_that_holds_nothing_of_its_own_takes_its_entry(state, world,
                                                                          monkeypatch):
    """A firm another source found, holding the registry firm's CUI, and a
    pulled firm the user took off the registry's pulls while Harvey's own
    work keeps it, are searched as before: no registry source, website or
    register phone is given them."""
    slim.taxpayers_beside(state, TAXPAYERS)
    site = _site(f"https://{ZAMBET_SITE}/", "<p>Zâmbet</p>",
                 _contact("email", f"office@{ZAMBET_SITE}", url=f"https://{ZAMBET_SITE}/"))
    fakes = Fakes(sites={ZAMBET_SITE: site}).install(monkeypatch)
    # Found by a listing, with the CUI the registry knows it by.
    cid = await state.add_company(Company(source="discover:osm", name="Zâmbet Exemplu",
                                          location="Brașov, Romania", cui=str(ZAMBET_CUI)))
    assert await state.registry_entry(cid) is None
    found = await finder.find(state, cid, spend=_spend(cap=True), env=ENV)
    assert sorted(slim.q(state.db_path, "SELECT s.name FROM firm_source fs JOIN source s "
                                        "ON s.id = fs.source")) == [("discover:osm",)]
    assert slim.q(state.db_path, "SELECT website, domain FROM firm") == [(None, None)]
    assert phone_sources(state.db_path) == [] and fakes.read == []
    assert found.as_dict()["why"] != "do_not_contact"
    assert await state.take_from_registry(cid) == {}

    # Pulled, worked on, then taken off the registry's pulls (harvey remove).
    other = await _pulled(world / "pulled", world, [ZAMBET])
    pulled = record_of(other.db_path, ZAMBET)
    await finder.find(other, pulled, spend=_spend(cap=True), env=ENV)
    removed = await other.remove_company(pulled)
    assert removed["kept"] and sources(other.db_path, ZAMBET) == []
    await finder.find(other, pulled, spend=_spend(cap=True), env=ENV)
    assert sources(other.db_path, ZAMBET) == []


@pytest.mark.asyncio
async def test_what_harvey_found_itself_is_kept(state, monkeypatch):
    Fakes().install(monkeypatch)
    await state.ensure_registry_records(regs=[ZAMBET])
    cid = record_of(state.db_path, ZAMBET)
    await state.fill_company_fields(cid, website="https://alta-adresa.example",
                                    domain="alta-adresa.example")
    await finder.find(state, cid, spend=_spend(cap=False), env=ENV)
    assert site_columns(state.db_path, ZAMBET)[:2] == ("https://alta-adresa.example",
                                                       "alta-adresa.example")
    assert sources(state.db_path, ZAMBET) == [("registry:onrc", f"onrc:{ZAMBET}")]


@pytest.mark.asyncio
async def test_a_declared_domain_another_firm_holds_is_left_off(state, world, monkeypatch):
    doi, unu = DOI, UNU
    site = _site(f"https://{SHARED}/", "<p>Salon, Brașov</p>",
                 _contact("phone", "+40268000105", url=f"https://{SHARED}/"))
    fakes = Fakes(sites={SHARED: site}).install(monkeypatch)
    await state.ensure_registry_records(regs=[doi, unu])
    first = await finder.find(state, record_of(state.db_path, doi), spend=_spend(cap=True),
                              env=ENV)
    second = await finder.find(state, record_of(state.db_path, unu), spend=_spend(cap=True),
                               env=ENV)
    # The first to be worked on holds the domain; the other keeps the
    # website it declares, read as its own at most likely, never looked up.
    assert site_columns(state.db_path, doi) == (f"https://{SHARED}", SHARED, None)
    assert site_columns(state.db_path, unu) == (f"https://{SHARED}", None, None)
    assert fakes.read == [(SHARED, True), (SHARED, True)] and fakes.looked == []
    assert (first.trust, second.trust) == ("confirmed", "likely")
    # As a pull leaves the two (it saves them in the same order, by name).
    other = await _pulled(world / "pulled", world, [doi, unu])
    for reg in (doi, unu):
        assert site_columns(other.db_path, reg) == site_columns(state.db_path, reg)


@pytest.mark.asyncio
async def test_a_declared_site_listed_after_its_record_was_made_is_skipped(state, con_free,
                                                                          monkeypatch):
    """A record made before its declared website went on the do-not-contact
    list: its job takes nothing from the registry and reads nothing. The
    skip is recorded by each search that could look it up, so the
    background's walk doesn't offer the firm again round after round."""
    barred_reg = "J08/803/2020"
    fakes = Fakes(sites={BARRED: _site(f"https://{BARRED}/", "<p>Salon</p>")}).install(
        monkeypatch)
    config = SimpleNamespace(compliance=ComplianceConfig(do_not_contact=[BARRED]),
                             spend=SimpleNamespace(per_day_usd=2.0, per_month_usd=None))
    monkeypatch.setattr(job_kinds, "_config", lambda: config)
    await state.ensure_registry_records(regs=[barred_reg])
    cid = record_of(state.db_path, barred_reg)
    found = await finder.find(state, cid, spend=_spend(cap=True), env=ENV, config=config)
    assert found.as_dict()["why"] == "do_not_contact" and found.steps == []
    assert "do-not-contact list" in found.skipped[0]
    assert fakes.read == [] and fakes.looked == []
    assert site_columns(state.db_path, barred_reg) == (None, None, None)
    for table in ("firm_source", "contact", "link", "person", "local_firm", "obs"):
        assert slim.one(state.db_path, f"SELECT COUNT(*) FROM {table}") == 0, table
    assert [(s[0], s[1], s[3], s[4]) for s in slim.searches(state)] == [
        ("company", "serper", "website", "skipped")]
    # The background's walk doesn't offer it again.
    job_kinds._backlog_idle.clear()
    assert await job_kinds.enqueue_backlog(state) == 0
    # A pull left the same firm out.
    report = await runs.registry_pull(state, con_free, registry.resolve(
        con_free, regs=[barred_reg]), do_not_contact=[BARRED], dry_run=True)
    assert report.excluded == 1
    # Off the list, its job takes the entry as any other firm's (the skip
    # is no work of Harvey's on it).
    await finder.find(state, cid, spend=_spend(cap=True), env=ENV,
                      config=SimpleNamespace(compliance=ComplianceConfig()))
    assert fakes.read == [(BARRED, True)]
    assert site_columns(state.db_path, barred_reg)[1] == BARRED


@pytest.fixture
def con_free(world):
    c = registry.connect(world / "registry.db")
    yield c
    c.close()


# ── The background ──

@pytest.mark.asyncio
async def test_the_background_pays_for_no_lookup_before_the_registry_entry(state, monkeypatch):
    """A record a run made has no website on file until its job has taken
    its registry entry, so the background's walk over firms with no website
    offers it once the run's check of it is done. Whichever job reaches it first, the run's or the
    background's, takes the entry before it decides anything: a firm that
    declares a website is read, one a register has a phone for is left, and
    only a firm with neither is looked up by name."""
    slim.taxpayers_beside(state, TAXPAYERS)
    fakes = Fakes(sites={ZAMBET_SITE: _site(f"https://{ZAMBET_SITE}/", "<p>Zâmbet</p>")}).install(
        monkeypatch)
    body = {"registry": {"regs": [ZAMBET, TELEFON, SIMPLU]}}
    selected, error = await launch.find_selection(state, body, create=True)
    assert not error and len(selected) == 3 and len(body["_registry"]["new"]) == 3
    await job_kinds.enqueue_contact_search(state, [c["id"] for c in selected], max_spend=1.0,
                                           check_first=body["_registry"]["new"])
    ids = {reg: record_of(state.db_path, reg) for reg in (ZAMBET, TELEFON, SIMPLU)}
    # With a day's budget the background still leaves them while the run's
    # check is to finish: the check queues their search, after ANAF's phone.
    monkeypatch.setattr(job_kinds, "_config", lambda: SimpleNamespace(
        spend=SimpleNamespace(per_day_usd=2.0, per_month_usd=None)))
    job_kinds._backlog_idle.clear()
    assert await job_kinds.enqueue_backlog(state) == 0
    # The check done (by hand here), the background takes them: to its walk
    # they are firms with no website and no contact.
    db = sqlite3.connect(state.db_path)
    db.execute("UPDATE jobs SET status = 'done' WHERE kind = 'id_check'")
    db.commit()
    db.close()
    job_kinds._backlog_idle.clear()
    assert await job_kinds.enqueue_backlog(state) == 3
    background = {p["company_id"] for kind, p in queued(state.db_path)
                  if kind == "find_contacts" and p.get("background")}
    assert background == set(ids.values())

    declared = await finder.find(state, ids[ZAMBET], spend=_spend(cap=True), env=ENV,
                                 background=True)
    assert fakes.looked == [] and fakes.read == [(ZAMBET_SITE, True)]
    assert declared.website == ZAMBET_SITE
    reached = await finder.find(state, ids[TELEFON], spend=_spend(cap=True), env=ENV,
                                background=True)
    assert fakes.looked == [] and reached.as_dict()["why"] == "has_contact"
    assert ("+40268000104", "mf_taxpayers") in phone_sources(state.db_path)
    await finder.find(state, ids[SIMPLU], spend=_spend(cap=True), env=ENV, background=True)
    assert fakes.looked == [("serper", "SIMPLU EXEMPLU SRL", "Brașov")]
    # Their websites on file, the walk has nothing more to pay for.
    assert site_columns(state.db_path, ZAMBET)[1] == ZAMBET_SITE
    assert sources(state.db_path, SIMPLU) == [("registry:onrc", f"onrc:{SIMPLU}")]


# ── The whole chain: records, the check, then each firm's search ──

@pytest.mark.asyncio
async def test_a_run_checks_its_new_firms_then_searches_them(state, world, monkeypatch):
    monkeypatch.setattr(anaf, "CACHE_PATH", world / "anaf.db")
    asked: list = []
    known = {TELEFON_CUI: answer(TELEFON_CUI, phone="0268 000 104"),
             DOI_CUI: answer(DOI_CUI, phone=""), SIMPLU_CUI: answer(SIMPLU_CUI, phone="")}

    async def ask(client, cuis, **kw):
        # (No search before the check: the run's searches wait for it.)
        assert fakes.read == [] and fakes.looked == []
        asked.append(sorted(cuis))
        return {c: known[c] for c in cuis if c in known}, set(), 1

    monkeypatch.setattr(anaf, "ask", ask)
    fakes = Fakes(sites={SHARED: _site(
        f"https://{SHARED}/", "<p>Salon</p>",
        _contact("phone", "+40268000106", url=f"https://{SHARED}/"))}).install(monkeypatch)
    body = {"registry": {"regs": [DOI, TELEFON, SIMPLU]}}
    selected, error = await launch.find_selection(state, body, create=True)
    assert not error
    checks: dict = {}
    batch, ids, _ = await job_kinds.enqueue_contact_search(
        state, [c["id"] for c in selected], max_spend=1.0,
        check_first=body["_registry"]["new"], checks=checks)
    assert ids == [] and checks == {"jobs": 1, "companies": 3}
    done = await asyncio.wait_for(wait_for_batch(state, batch, say=lambda line: None,
                                                 poll=0.05), timeout=120)
    assert sorted((j.kind, j.status) for j in done) == [
        ("find_contacts", "done")] * 3 + [("id_check", "done"), ("register_counts", "done")]
    assert asked == [sorted([DOI_CUI, SIMPLU_CUI, TELEFON_CUI])]
    # The declared site was read; ANAF's phone reached one firm, so no
    # search was paid for it; the firm with neither was looked up.
    assert fakes.read == [(SHARED, True)]
    assert fakes.looked == [("serper", "SIMPLU EXEMPLU SRL", "Brașov")]
    why = {j.params["company_id"]: j.result.get("why") for j in done
           if j.kind == "find_contacts"}
    assert why[record_of(state.db_path, TELEFON)] == "has_contact"
    assert why[record_of(state.db_path, DOI)] is None
    # What ANAF gave a record first (its phone) is a register's, not the
    # firm's own: each took its registry entry all the same.
    assert ("+40268000104", "anaf") in phone_sources(state.db_path)
    for reg in (DOI, TELEFON, SIMPLU):
        assert sources(state.db_path, reg) == [("registry:onrc", f"onrc:{reg}")], reg


@pytest.mark.parametrize("ended", ["failed", "cancelled"])
def test_a_check_that_ends_without_queueing_its_searches_still_gets_them_queued(client, ended):
    from harvey.modules import jobs, worker

    got = client.post("/api/contacts/find",
                      json={"registry": {"regs": [ZAMBET, TELEFON]}}).json()
    (check,) = [r[0] for r in slim.q(client.db, "SELECT id FROM jobs WHERE kind = 'id_check'")]
    db = sqlite3.connect(client.db)
    db.execute("UPDATE jobs SET status = ? WHERE id = ?", (ended, check))
    db.commit()
    db.close()

    async def ended_now():
        s = await opened(client.db.parent)
        await worker._after_job(s, await jobs.get(s, check))

    run(ended_now())
    searches = slim.q(client.db, "SELECT json_extract(params_json, '$.company_id'), batch_id "
                                 "FROM jobs WHERE kind = 'find_contacts'")
    if ended == "cancelled":
        # The user stopped the run: nothing is queued behind their cancel.
        assert searches == []
        return
    assert sorted(searches) == sorted((record_of(client.db, reg), got["batch_id"])
                                      for reg in (ZAMBET, TELEFON))


# ── The commands ──

def _cli(monkeypatch, *argv) -> None:
    from harvey import cli

    monkeypatch.setattr(sys, "argv", ["harvey", *argv])
    cli.main()


def test_harvey_contacts_find_starts_from_the_registry(world, monkeypatch, capsys):
    db = world / "harvey.db"
    _cli(monkeypatch, "contacts", "find", "9602", "--registry", "--county", "BV", "--dry-run")
    out = capsys.readouterr().out
    assert "Registry: 5 firms match, 0 with a record in Harvey; 5 would get one now" in out
    assert "isn't known before the run" in out and "Dry run: nothing was written" in out
    assert firms(db) == 0 and queued(db) == []

    _cli(monkeypatch, "contacts", "find", "9602", "--registry", "--county", "BV", "--limit",
         "3", "--max-spend", "1.5", "--background")
    out = capsys.readouterr().out
    assert ("Registry: 5 firms match, 0 with a record in Harvey; 3 got one now, 2 left for a "
            "later run (--limit)") in out
    assert "3 new firms are checked at ANAF first" in out and "Queued:" in out
    assert firms(db) == 3 and nothing_copied(db)
    assert slim.q(db, "SELECT kind, max_spend FROM job_batches") == [("find_contacts", 1.5)]
    assert [kind for kind, _ in queued(db)] == ["id_check", "register_counts"]

    # The same refusals as the page's.
    for argv in (["--registry"], ["9602", "--registry", "--website", "yes"],
                 ["--registry", "--county", "Atlantis"]):
        with pytest.raises(SystemExit) as exit_:
            _cli(monkeypatch, "contacts", "find", *argv)
        assert exit_.value.code == 2
    out = capsys.readouterr().out
    assert "needs an activity, an area or a filter" in out and "Atlantis" in out
    assert firms(db) == 3
    # No record for none, nor words that would be dropped without --registry.
    for argv in (["9602", "--registry", "--limit", "0"], ["9602", "--registry", "--limit", "-1"],
                 ["9602", "--county", "BV"]):
        with pytest.raises(SystemExit) as exit_:
            _cli(monkeypatch, "contacts", "find", *argv)
        assert exit_.value.code == 2
    out = capsys.readouterr().out
    assert "--limit must be 1 or more" in out
    assert "add --registry, or use --q" in out
    assert firms(db) == 3 and len(queued(db)) == 2


def test_harvey_contacts_find_estimates_before_it_makes_records(world, monkeypatch, capsys):
    """Every estimate is made before a record is: one that fails leaves
    no record with no work queued for it."""
    async def broken(*a, **kw):
        raise RuntimeError("the estimate failed")

    monkeypatch.setattr(finder, "classify_estimate", broken)
    with pytest.raises(SystemExit):
        _cli(monkeypatch, "contacts", "find", "9602", "--registry", "--county", "BV",
             "--background")
    assert "the estimate failed" in capsys.readouterr().out
    assert firms(world / "harvey.db") == 0 and queued(world / "harvey.db") == []


def test_import_onrc_refuses_the_whole_country_and_counts_it_in_place(world, monkeypatch,
                                                                      capsys):
    for argv in (["--whole-country"], []):
        with pytest.raises(SystemExit) as exit_:
            _cli(monkeypatch, "import-onrc", *argv)
        assert exit_.value.code == 2
        out = capsys.readouterr().out
        assert "Companies page" in out and "harvey contacts find --registry" in out
    _cli(monkeypatch, "import-onrc", "--dry-run")
    out = capsys.readouterr().out
    assert "16 operating firms in all of Romania" in out and DATASET.name in out
    _cli(monkeypatch, "import-onrc", "--dry-run", "--include-inactive", "--whole-country")
    assert "19 firms of every status" in capsys.readouterr().out
    # Counted where the registry is: no database, no job, nothing saved.
    assert not (world / "harvey.db").exists()
