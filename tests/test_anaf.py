"""ANAF's company check (the anaf module), its pace and the runs around it.

Every answer here is invented and written by hand in the shape ANAF's
PlatitorTvaRest v9 returns; every CUI is fictional (made with a valid check
digit, so it is one ANAF would be asked about) and no test reaches ANAF:
tests/conftest.py refuses the request unless a test marks itself
``live_anaf_client`` and gives a stubbed HTTP client.
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import time

import httpx
import pytest
import pytest_asyncio

from harvey.models import Company
from harvey.modules import api, ingest, job_kinds, lanes, runs, switches
from harvey.modules.ro.anaf import capabilities as caps
from harvey.modules.ro.anaf import service
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

ANAF_CODES = ("VAT_REGISTERED", "VAT_SINCE", "VAT_ON_CASH", "SPLIT_VAT", "FISCALLY_INACTIVE",
              "EFACTURA_REGISTERED", "REGISTRATION_STATUS", "LEGAL_FORM", "ADDRESS",
              "PHONE_NUMBER")


def cui(body: str) -> str:
    """A fictional CUI: ``body`` with its check digit."""
    key = "753217532"[-len(body):]
    total = sum(int(d) * int(k) for d, k in zip(body, key))
    return body + str(total * 10 % 11 % 10)


A, B, C = cui("9100001"), cui("9100002"), cui("9100003")


def answer(number: str, *, vat=True, inactive=False, cash=False, split=False, efactura=True,
           phone="0268 000 210", form="SOCIETATE CU RASPUNDERE LIMITATA",
           status="INREGISTRAT din data 01.03.2015", fiscal_street="Str. Exemplu"):
    """One invented company as ANAF's v9 answers for it."""
    return {
        "date_generale": {
            "cui": int(number), "data": "2026-10-30", "denumire": f"EXEMPLU {number} SRL",
            "adresa": "JUD. BRAŞOV, MUN. BRAŞOV, STR. EXEMPLU, NR.1", "nrRegCom": "J08/1/2015",
            "telefon": phone, "fax": "", "codPostal": "500001", "act": "",
            "stare_inregistrare": status, "data_inregistrare": "2015-03-01", "cod_CAEN": "8623",
            "iban": "", "statusRO_e_Factura": efactura, "organFiscalCompetent": "",
            "forma_de_proprietate": "", "forma_organizare": "PERSOANA JURIDICA",
            "forma_juridica": form},
        "inregistrare_scop_Tva": {"scpTVA": vat, "perioade_TVA": [
            {"data_inceput_ScpTVA": "2016-01-01", "data_sfarsit_ScpTVA": "2017-01-01",
             "data_anul_imp_ScpTVA": "", "mesaj_ScpTVA": ""},
            {"data_inceput_ScpTVA": "2019-05-01", "data_sfarsit_ScpTVA": "",
             "data_anul_imp_ScpTVA": "", "mesaj_ScpTVA": ""}] if vat else []},
        "inregistrare_RTVAI": {"statusTvaIncasare": cash},
        "stare_inactiv": {"dataInactivare": "", "dataReactivare": "", "dataPublicare": "",
                          "dataRadiere": "", "statusInactivi": inactive},
        "inregistrare_SplitTVA": {"statusSplitTVA": split},
        "adresa_sediu_social": {"sdenumire_Strada": "Str. Exemplu", "snumar_Strada": "1",
                                "sdenumire_Localitate": "Mun. Braşov",
                                "sdenumire_Judet": "BRAŞOV", "sdetalii_Adresa": "",
                                "scod_Postal": "500001"},
        "adresa_domiciliu_fiscal": {"ddenumire_Strada": fiscal_street, "dnumar_Strada": "1",
                                    "ddenumire_Localitate": "Mun. Braşov",
                                    "ddenumire_Judet": "BRAŞOV", "ddetalii_Adresa": "",
                                    "dcod_Postal": "500001"},
    }


class Anaf:
    """A stand-in for ANAF's endpoint: answers from ``known``, or the
    statuses queued in ``fail`` first. Records every request body."""

    def __init__(self, known: dict | None = None, fail=()):
        self.known = dict(known or {})
        self.fail = list(fail)
        self.bodies: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.fail:
            status = self.fail.pop(0)
            headers = {"Retry-After": "0"} if status == 429 else {}
            return httpx.Response(status, json={"error": "busy"}, headers=headers)
        if any(not isinstance(item["cui"], int) for item in body):
            return httpx.Response(400, json={"status": 400, "error": "Bad Request"})
        found = [self.known[str(i["cui"])] for i in body if str(i["cui"]) in self.known]
        missing = [i["cui"] for i in body if str(i["cui"]) not in self.known]
        return httpx.Response(200, json={"found": found, "notFound": missing})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# The real request code (tests/conftest.py replaces it in every test).
_REAL_ASK = service.ask


@pytest.fixture
def real_ask(monkeypatch):
    """The real request code, against a stubbed client, without its waits."""
    monkeypatch.setattr(service, "ask", _REAL_ASK)
    monkeypatch.setattr(service, "BACKOFF", (0.0, 0.0, 0.0))
    monkeypatch.setattr(lanes, "interval", lambda lane: 0.0)
    return _REAL_ASK


def ctx(http, tmp_path, **settings):
    return api.Context(http=http, settings={"path": str(tmp_path / "anaf.db"), **settings})


# ── CUIs and answers ──

def test_a_cui_is_digits_with_a_valid_check_digit():
    assert service.clean_cui(f"RO{A}") == A
    assert service.clean_cui(f" 00{A} ") == A
    assert service.clean_cui(A[:-1] + str((int(A[-1]) + 1) % 10)) == ""
    assert service.clean_cui("1900101123456") == ""          # a personal numeric code
    assert service.clean_cui("") == "" and service.clean_cui("RO") == ""


def test_an_answer_becomes_facts_and_phones():
    facts, contacts = caps.facts_for(answer(A), "c1")
    got = {(f.code, f.value_num, f.value_text) for f in facts}
    assert ("VAT_REGISTERED", 1.0, "") in got
    assert ("VAT_SINCE", None, "2019-05-01") in got            # the open period's start
    assert ("VAT_ON_CASH", 0.0, "") in got and ("SPLIT_VAT", 0.0, "") in got
    assert ("FISCALLY_INACTIVE", 0.0, "") in got               # an explicit "not inactive"
    assert ("EFACTURA_REGISTERED", 1.0, "") in got
    assert ("REGISTRATION_STATUS", None, "INREGISTRAT") in got  # the date left out
    assert ("LEGAL_FORM", None, "SOCIETATE CU RASPUNDERE LIMITATA") in got
    assert ("PHONE_NUMBER", None, "+40268000210") in got
    addresses = [f.value_text for f in facts if f.code == "ADDRESS"]
    assert addresses == ["Str. Exemplu 1, Mun. Braşov, BRAŞOV, 500001"]   # one: the same place
    assert all(f.record == "c1" and f.collector == "anaf" and f.source_url == service.API_URL
               for f in facts)
    assert [(c.kind, c.value, c.display) for c in contacts] == [
        ("phone", "+40268000210", "+40 268 000 210")]


def test_an_inactive_company_and_a_fiscal_domicile_elsewhere():
    facts, _ = caps.facts_for(answer(A, vat=False, inactive=True, phone="",
                                     fiscal_street="Str. Altundeva"))
    got = {(f.code, f.value_num, f.value_text) for f in facts}
    assert ("FISCALLY_INACTIVE", 1.0, "") in got and ("VAT_REGISTERED", 0.0, "") in got
    assert not any(f.code in ("VAT_SINCE", "PHONE_NUMBER") for f in facts)
    assert len([f for f in facts if f.code == "ADDRESS"]) == 2


@pytest.mark.parametrize("raw,expected", [
    ("0268 000 210 / 0722 000 210", ["+40268000210", "+40722000210"]),
    ("268000210", ["+40268000210"]),           # the trunk 0 left out
    ("0", []), ("", []), ("12345", []),
])
def test_a_register_phone_field(raw, expected):
    assert [n for n, _ in service.phones(raw)] == expected


# ── The request ──

@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_a_batch_is_one_request_of_clean_cuis(tmp_path, real_ask):
    anaf = Anaf({A: answer(A), B: answer(B)})
    async with anaf.client() as http:
        draft = await caps.CAPABILITIES["company_check"](api.Input(extra={"batch": [
            {"key": "a", "cui": f"RO{A}"}, {"key": "b", "cui": B}, {"key": "c", "cui": C},
            {"key": "x", "cui": "RO123"}]}), ctx(http, tmp_path))
    assert draft.failed is None
    assert len(anaf.bodies) == 1
    assert [i["cui"] for i in anaf.bodies[0]] == [int(A), int(B), int(C)]   # no RO, no bad one
    assert all(isinstance(i["cui"], int) and len(i["data"]) == 10 for i in anaf.bodies[0])
    assert draft.values == {"asked": 3, "cached": 0, "not_found": 1, "invalid": 1, "requests": 1}
    assert {f.record for f in draft.facts} == {"a", "b"}


@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_answers_are_kept_and_reused_until_they_are_old(tmp_path, real_ask):
    anaf = Anaf({A: answer(A)})
    async with anaf.client() as http:
        first = await caps.CAPABILITIES["company_check"](
            api.Input(extra={"batch": [{"key": "a", "cui": A}, {"key": "c", "cui": C}]}),
            ctx(http, tmp_path))
        again = await caps.CAPABILITIES["company_check"](
            api.Input(extra={"batch": [{"key": "a", "cui": A}, {"key": "c", "cui": C}]}),
            ctx(http, tmp_path))
        assert len(anaf.bodies) == 1                               # nothing asked twice
        assert again.values["cached"] == 2 and again.values["requests"] == 0
        assert {(f.code, f.value_num) for f in again.facts} == {
            (f.code, f.value_num) for f in first.facts}
        # An answer older than cache_days is asked again; --missing never asks it.
        service.keep(tmp_path / "anaf.db", {A: answer(A)}, set(), "2020-01-01T00:00:00+00:00")
        missing = await caps.CAPABILITIES["company_check"](
            api.Input(extra={"batch": [{"key": "a", "cui": A}], "missing_only": True}),
            ctx(http, tmp_path))
        assert missing.values["requests"] == 0 and len(anaf.bodies) == 1
        stale = await caps.CAPABILITIES["company_check"](
            api.Input(extra={"batch": [{"key": "a", "cui": A}]}), ctx(http, tmp_path))
        assert stale.values["requests"] == 1 and len(anaf.bodies) == 2
    kept = service.status(tmp_path / "anaf.db")
    assert kept["answers"] == 2 and kept["found"] == 1


@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_busy_answers_are_retried_with_backoff_and_nothing_else(tmp_path, real_ask,
                                                                      monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(service, "BACKOFF", (2.0, 5.0, 10.0))
    for fail, requests, ok in (([503], 2, True), ([429, 502], 3, True),
                               ([500, 500, 500, 500], 4, False), ([400], 1, False)):
        anaf = Anaf({A: answer(A)}, fail=fail)
        waits.clear()
        async with anaf.client() as http:
            if ok:
                found, missing, made = await service.ask(http, [A], sleep=sleep)
                assert A in found and made == requests
            else:
                with pytest.raises(service.AnafError, match=r"HTTP (500|400)") as err:
                    await service.ask(http, [A], sleep=sleep)
                # Busy to the end: a run stops; refused: only that batch fails.
                assert err.value.busy is (fail != [400])
        assert len(anaf.bodies) == requests
        assert waits == ([] if fail == [400] else [2.0, 5.0, 10.0][:requests - 1])


@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_a_refused_request_fails_the_call_and_nothing_is_kept(tmp_path, real_ask):
    anaf = Anaf(fail=[400])
    async with anaf.client() as http:
        draft = await caps.CAPABILITIES["company_check"](
            api.Input(extra={"batch": [{"key": "a", "cui": A}]}), ctx(http, tmp_path))
    assert draft.failed == "ANAF refused the request (HTTP 400)" and not draft.facts
    assert service.status(tmp_path / "anaf.db")["answers"] == 0


@pytest.mark.asyncio
async def test_more_than_a_hundred_is_refused_before_asking(tmp_path):
    batch = [{"key": str(i), "cui": cui(str(9200000 + i))} for i in range(101)]
    draft = await caps.CAPABILITIES["company_check"](api.Input(extra={"batch": batch}),
                                                     ctx(None, tmp_path))
    assert draft.failed == "at most 100 companies a call"


# ── The pace ──

def test_the_lane_paces_anaf_at_one_request_every_1_1_seconds():
    assert lanes.interval("anaf") == 1.1
    assert lanes.interval("nominatim") == 1.0         # max_per_second = 1


@pytest.mark.asyncio
async def test_a_lane_no_manifest_declares_is_refused_not_unpaced():
    # A typo ("anaff") must not mean requests as fast as they go.
    with pytest.raises(lanes.UnknownLane):
        lanes.interval("anaff")
    with pytest.raises(lanes.UnknownLane):
        await lanes.wait("anaff")


def test_a_capability_naming_an_undeclared_lane_fails_to_load(tmp_path):
    from harvey.modules import loader

    folder = tmp_path / "x" / "paced"
    folder.mkdir(parents=True)
    (folder / "module.toml").write_text(
        'id = "paced"\nset = "x"\nkind = "enricher"\nversion = "0.1.0"\napi = 1\n'
        'entry = "harvey.modules.x.paced"\n'
        '[[capabilities]]\nid = "ask"\nop = "lookup"\nlane = "anaff"\n', encoding="utf-8")
    with pytest.raises(loader.ManifestError, match="lane 'anaff'"):
        loader.load_manifests(repo_dir=tmp_path, data_dir=None)


@pytest.mark.asyncio
async def test_requests_in_one_lane_take_turns(monkeypatch):
    monkeypatch.setattr(lanes, "interval", lambda lane: 0.2)
    times = []

    async def one():
        await lanes.wait("test")
        times.append(time.monotonic())

    await asyncio.gather(one(), one(), one())
    times.sort()
    assert all(b - a >= 0.19 for a, b in zip(times, times[1:]))
    stamp = float((lanes.LANE_DIR / "test.last").read_text())
    assert abs(stamp - time.time()) < 5


def test_another_process_s_last_request_is_waited_for(monkeypatch):
    # The shared file says a request went out just now (another process).
    lanes.LANE_DIR.mkdir(parents=True, exist_ok=True)
    (lanes.LANE_DIR / "shared.last").write_text(f"{time.time():.3f}")
    started = time.time()
    lanes._turn("shared", 0.3)
    assert time.time() - started >= 0.28


def _three_threads(lane):
    """Three callers, each on its own thread and event loop."""
    import threading

    times = []

    def worker():
        async def go():
            await lanes.wait(lane)
            times.append(time.time())
        asyncio.run(go())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times.sort()
    return [b - a for a, b in zip(times, times[1:])]


def test_the_pace_holds_across_threads_without_the_lane_file(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(lanes, "LANE_DIR", blocker / "lanes")     # can't be created
    monkeypatch.setattr(lanes, "interval", lambda lane: 0.3)
    assert all(gap >= 0.29 for gap in _three_threads("no-file"))


def test_the_pace_holds_across_threads_with_the_lane_file(monkeypatch, tmp_path):
    monkeypatch.setattr(lanes, "LANE_DIR", tmp_path / "lanes")
    monkeypatch.setattr(lanes, "interval", lambda lane: 0.3)
    assert all(gap >= 0.29 for gap in _three_threads("with-file"))


# ── Recording what ANAF says ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        yield sm


def stub_anaf(monkeypatch, known: dict, asked: list | None = None):
    async def ask(client, cuis, **kw):
        if asked is not None:
            asked.append(list(cuis))
        found = {c: known[c] for c in cuis if c in known}
        return found, {c for c in cuis if c not in known}, 1
    monkeypatch.setattr(service, "ask", ask)


def readings(state, code):
    with sqlite3.connect(state.db_path) as db:
        return [tuple(r) for r in db.execute(
            "SELECT company_id, value_num, value_text, module FROM observations "
            "WHERE signal_code = ? ORDER BY rowid", (code,))]


@pytest.mark.asyncio
async def test_a_check_records_confirmed_facts_only_when_they_change(state, monkeypatch):
    a = await state.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
    b = await state.add_company(Company(name="EXEMPLU B SRL", cui=B, phone="0268 000 990",
                                        source="registry:onrc"))
    asked = []
    stub_anaf(monkeypatch, {A: answer(A), B: answer(B, phone="0722 000 210")}, asked)
    for code in ("FISCALLY_INACTIVE", "VAT_REGISTERED", "PHONE_NUMBER", "ADDRESS"):
        await state.set_signal_status(code, "confirmed")

    first = await runs.check_by_id(state, "anaf", "company_check")
    assert [sorted(b) for b in asked] == [[A, B]]             # one batch, one request
    assert first["companies"] == 2 and first["updated"] == 2
    assert "LEGAL_FORM" in first["unconfirmed"] and not readings(state, "LEGAL_FORM")
    assert sorted(readings(state, "FISCALLY_INACTIVE")) == sorted([(a, 0.0, "", "anaf"),
                                                                   (b, 0.0, "", "anaf")])
    # A phone fills only a company that has none.
    assert (await state.get_company(a)).phone == "+40 268 000 210"
    assert (await state.get_company(b)).phone == "0268 000 990"
    assert first["phones_set"] == 1
    assert {r[2] for r in readings(state, "PHONE_NUMBER")} == {"+40268000210", "+40722000210"}

    # Asked again with nothing changed: nothing new is written.
    service.keep(service.CACHE_PATH, {}, set())
    again = await runs.check_by_id(state, "anaf", "company_check", [a])
    assert again["observations"] == 0 and again["cached"] == 1

    # It turns inactive, then active again: each change is recorded.
    for inactive in (True, False):
        service.keep(service.CACHE_PATH, {A: answer(A, inactive=inactive)}, set(),
                     "2020-01-01T00:00:00+00:00")
        stub_anaf(monkeypatch, {A: answer(A, inactive=inactive)})
        await runs.check_by_id(state, "anaf", "company_check", [a])
    assert [r[1] for r in readings(state, "FISCALLY_INACTIVE") if r[0] == a] == [0.0, 1.0, 0.0]


@pytest.mark.asyncio
async def test_missing_only_asks_about_companies_never_answered(state, monkeypatch):
    a = await state.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
    await state.add_company(Company(name="EXEMPLU B SRL", cui=B, source="registry:onrc"))
    service.keep(service.CACHE_PATH, {A: answer(A)}, set(), "2020-01-01T00:00:00+00:00")
    asked = []
    stub_anaf(monkeypatch, {B: answer(B)}, asked)
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    report = await runs.check_by_id(state, "anaf", "company_check", missing_only=True)
    assert asked == [[B]] and report["cached"] == 1
    assert report["companies"] == 2 and a


@pytest.mark.asyncio
async def test_nothing_is_asked_or_written_until_one_of_its_signals_is_confirmed(state,
                                                                                monkeypatch):
    a = await state.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
    asked = []
    stub_anaf(monkeypatch, {A: answer(A)}, asked)
    report = await runs.check_by_id(state, "anaf", "company_check")
    assert await job_kinds.enqueue_follow_up_checks(state, [a]) == 0   # nor after a pull
    assert asked == [] and report["requests"] == 0 and report["phones_set"] == 0
    assert report["stopped"] == runs.NOTHING_CONFIRMED and "VAT_REGISTERED" in report["unconfirmed"]
    assert not (await state.get_company(a)).phone
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


async def _companies(state, n):
    return [await state.add_company(Company(name=f"EXEMPLU {i} SRL", cui=cui(str(9200000 + i)),
                                            source="registry:onrc")) for i in range(n)]


@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_a_busy_anaf_stops_the_run_instead_of_moving_on(state, real_ask):
    await _companies(state, 250)                          # three batches
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    anaf = Anaf(fail=[503] * 1000)
    async with anaf.client() as http:
        report = await runs.check_by_id(state, "anaf", "company_check", http=http)
    assert len(anaf.bodies) == 4                          # one batch, its retries, then stop
    assert report["stopped"] == "busy" and len(report["errors"]) == 1


@pytest.mark.asyncio
@pytest.mark.live_anaf_client
async def test_two_refused_batches_in_a_row_stop_the_run(state, real_ask):
    await _companies(state, 350)                          # four batches
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    anaf = Anaf(fail=[400] * 1000)
    async with anaf.client() as http:
        report = await runs.check_by_id(state, "anaf", "company_check", http=http)
    assert len(anaf.bodies) == 2 and report["stopped"] == "failed"


@pytest.mark.asyncio
async def test_a_check_is_refused_while_the_module_is_off(state):
    await switches.set_enabled(state, "anaf", False)
    with pytest.raises(switches.ModuleOff, match="anaf"):
        await runs.check_by_id(state, "anaf", "company_check")


@pytest.mark.asyncio
async def test_firms_a_source_brings_in_are_queued_for_a_check(state):
    ids = [await state.add_company(Company(name=f"EXEMPLU {c} SRL", cui=c,
                                           source="registry:onrc")) for c in (A, B, C)]
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    assert await job_kinds.enqueue_follow_up_checks(state, ids) == 1
    with sqlite3.connect(state.db_path) as db:
        queued = db.execute("SELECT kind, module, capability FROM jobs").fetchall()
    assert queued == [("id_check", "anaf", "company_check")]


@pytest.mark.asyncio
async def test_no_follow_up_while_the_module_is_off(state):
    a = await state.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    await switches.set_enabled(state, "anaf", False)
    assert await job_kinds.enqueue_follow_up_checks(state, [a]) == 0


@pytest.mark.asyncio
async def test_a_registry_pull_hands_each_saved_batch_to_the_follow_up(state, monkeypatch):
    from harvey.collectors.onrc import ImportReport

    draft = api.Draft(records=[api.Record(key=str(i), name=f"EXEMPLU {i} SRL",
                                          ids={"cui": c}, source="registry:onrc",
                                          module="onrc", capability="registry_search")
                               for i, c in enumerate((A, B, C))],
                      values={"codes": (), "collector": "registry"})

    handed = []
    monkeypatch.setattr(runs, "SOURCE_SAVE_BATCH", 2)
    saved = []
    await runs.save_source_draft(state, draft, ImportReport(), module="onrc", saved_ids=saved,
                                 on_saved=lambda ids: handed.append(list(ids)))
    assert [len(h) for h in handed] == [2, 1] and sum(handed, []) == saved


# ── Saving a batch at a time, the next request going out meanwhile ──

def _fixture_answer(i: int, number: str):
    """A varied invented answer: inactive or not, VAT or not, a fiscal
    domicile elsewhere, no phone, or a phone several companies share."""
    phone = "" if i % 6 == 0 else "0268 000 210" if i % 13 == 0 else f"0722 000 {i:03d}"
    return answer(number, vat=i % 3 != 0, inactive=i % 5 == 0, cash=i % 4 == 0,
                  phone=phone, fiscal_street="Str. Fiscală" if i % 4 == 1 else "Str. Exemplu")


async def _fixture(sm, n=230):
    """``n`` invented companies, some with a phone of their own, some with
    readings from an earlier check, one known by two CUIs; ANAF knows all
    but every eleventh. Returns {cui: answer}."""
    known = {}
    for i in range(n):
        number = cui(str(9300000 + i))
        await sm.add_company(Company(name=f"EXEMPLU {i} SRL", cui=number,
                                     phone="0268 000 900" if i % 7 == 0 else "",
                                     location="Brașov, Brașov, Romania",
                                     source="registry:onrc"))
        if i % 11:
            known[number] = _fixture_answer(i, number)
    with sqlite3.connect(sm.db_path) as db:
        ids = {c: i for i, c in db.execute("SELECT id, cui FROM companies")}
        # One company known by a second CUI too (an earlier merge).
        second = cui("9399999")
        db.execute("INSERT INTO company_ids (company_id, kind, value) VALUES (?, 'cui', ?)",
                   (ids[cui("9300001")], second))
    known[second] = _fixture_answer(1, second)
    earlier = []
    for i in range(0, n, 9):
        cid = ids[cui(str(9300000 + i))]
        earlier += [dict(company_id=cid, signal_code="VAT_REGISTERED", value_num=0.0,
                         module="anaf", capability="company_check", collector="anaf",
                         observed_at="2026-01-01 00:00:00"),
                    dict(company_id=cid, signal_code="ADDRESS", value_text=service.address(
                        known.get(cui(str(9300000 + i)), answer("1"))["adresa_sediu_social"], "s"),
                         module="anaf", capability="company_check", collector="anaf",
                         observed_at="2026-01-01 00:00:00")]
    await sm.add_observations(earlier)
    for code in ANAF_CODES:
        if code != "LEGAL_FORM":
            await sm.set_signal_status(code, "confirmed")
    return known


async def _old_check(state):
    """The check as it saved before, a company at a time (kept here to
    compare with): its rows are what the batch save must write."""
    from harvey.modules.ingest import Saved

    confirmed = await state.confirmed_signal_codes()
    companies = await state.companies_with_id("cui")
    run = api.capability("anaf", "company_check")
    many = ingest.many_codes()
    ctx = api.Context(http=httpx.AsyncClient(), settings={}, wanted=frozenset(confirmed),
                      enabled=await runs._enabled(state))
    for start in range(0, len(companies), 100):
        chunk = companies[start:start + 100]
        draft = await run(api.Input(extra={
            "batch": [{"key": r["id"], "cui": r["value"]} for r in chunk]}), ctx)
        facts_of, contacts_of = {}, {}
        for fact in draft.facts:
            facts_of.setdefault(fact.record, []).append(fact)
        for contact in draft.contacts:
            contacts_of.setdefault(contact.record, []).append(contact)
        async with state.batch() as batch:
            for row in chunk:
                cid = row["id"]
                rows = ingest.rows(facts_of.get(cid, []), run_id="r", confirmed=confirmed,
                                   records={cid: Saved(cid, cid)})
                fresh = await ingest.changed(batch, cid, "anaf", rows, many)
                await ingest.write(batch, fresh, "r")
                await ingest.company_phones(batch, cid, contacts_of.get(cid, []))
    await ctx.http.aclose()


def _snapshot(path) -> dict:
    """Everything a check writes, keyed by CUI (ids and times differ
    between two databases): readings in the order written, contacts and
    their links, phones, keys."""
    with sqlite3.connect(path) as db:
        key = dict(db.execute("SELECT id, cui FROM companies"))
        key[""] = ""
        readings: dict = {}
        for row in db.execute(
                "SELECT company_id, signal_code, value_num, value_text, collector, confidence, "
                "evidence_url, module, capability, origin_company_id FROM observations "
                "ORDER BY rowid"):
            readings.setdefault(key[row[0]], []).append((*row[1:9], key[row[9]]))
        return {
            "readings": readings,
            "contacts": sorted(db.execute(
                "SELECT kind, value, display, line_type, status, do_not_contact FROM contacts")),
            "links": sorted((key[r[0]], *r[1:-1], key[r[-1]]) for r in db.execute(
                "SELECT l.company_id, k.kind, k.value, l.person_id, l.location_id, l.source, "
                "l.capability, l.evidence_url, l.trust, l.confidence, l.origin_company_id "
                "FROM company_contacts l JOIN contacts k ON k.id = l.contact_id")),
            "phones": sorted(db.execute("SELECT cui, phone FROM companies")),
            "keys": sorted((key[r[0]], *r[1:]) for r in db.execute(
                "SELECT company_id, kind, value, source, confidence FROM company_ids")),
            "people": sorted(db.execute("SELECT name, role, source FROM people")),
        }


@pytest.mark.asyncio
async def test_the_batch_save_writes_what_the_company_at_a_time_save_wrote(monkeypatch, tmp_path):
    snaps = []
    for label in ("before", "now"):
        sm = StateManager(str(tmp_path / f"{label}.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        known = await _fixture(sm)
        monkeypatch.setattr(service, "CACHE_PATH", tmp_path / f"{label}-anaf.db")
        asked = []
        stub_anaf(monkeypatch, known, asked)
        if label == "before":
            await _old_check(sm)
        else:
            report = await runs.check_by_id(sm, "anaf", "company_check")
            assert report["errors"] == [] and report["requests"] == 3
            assert report["observations"] == sum(len(v) for v in _snapshot(sm.db_path)[
                "readings"].values()) - 2 * len(range(0, 230, 9))
        # Asked again with nothing new: nothing more is written, either way.
        if label == "before":
            await _old_check(sm)
        else:
            again = await runs.check_by_id(sm, "anaf", "company_check")
            assert again["observations"] == 0 and again["phones_set"] == 0
        snaps.append(_snapshot(sm.db_path))
    before, now = snaps
    for part in before:
        assert now[part] == before[part], part
    # The fixture exercised what it is there for.
    assert any(len(v) > 6 for v in now["readings"].values())
    assert any(r[1] == "phone" and r[5] == "registry:onrc" for r in now["links"])
    assert not any(r[0] == "LEGAL_FORM" for v in now["readings"].values() for r in v)


def _paced_anaf(monkeypatch, known, log):
    """ANAF's request, timed: each waits its turn in the lane (the real
    lanes.wait), and the log notes when each starts and ends."""
    async def ask(client, cuis, **kw):
        await lanes.wait(service.LANE)
        log.append(("ask", time.monotonic(), len(cuis)))
        await asyncio.sleep(0.01)
        log.append(("answered", time.monotonic(), len(cuis)))
        found = {c: known[c] for c in cuis if c in known}
        return found, {c for c in cuis if c not in known}, 1
    monkeypatch.setattr(service, "ask", ask)


def _slow_save(monkeypatch, log, seconds=0.25):
    real = ingest.checked

    async def checked(batch, module, found, **kw):
        log.append(("save", time.monotonic(), len(found)))
        await asyncio.sleep(seconds)
        out = await real(batch, module, found, **kw)
        log.append(("saved", time.monotonic(), len(found)))
        return out
    monkeypatch.setattr(ingest, "checked", checked)


@pytest.mark.asyncio
async def test_the_next_request_goes_out_while_a_batch_is_saved_never_faster_than_the_lane(
        state, monkeypatch):
    ids = await _companies(state, 550)                    # six batches
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    monkeypatch.setattr(lanes, "interval", lambda lane: 0.1)
    log = []
    _paced_anaf(monkeypatch, known, log)
    _slow_save(monkeypatch, log)
    report = await runs.check_by_id(state, "anaf", "company_check", ids)
    assert report["requests"] == 6 and report["updated"] == 550 and not report["errors"]
    asks = [t for kind, t, _ in log if kind == "ask"]
    assert all(b - a >= 0.099 for a, b in zip(asks, asks[1:]))        # the lane's pace
    in_flight = saving = most_requests = most_ahead = asked = saved = 0
    for kind, _, _ in log:
        in_flight += {"ask": 1, "answered": -1}.get(kind, 0)
        asked += kind == "ask"
        saved += kind == "saved"
        most_requests = max(most_requests, in_flight)
        most_ahead = max(most_ahead, asked - saved)
    assert most_requests == 1                  # one request at a time
    assert most_ahead == 2                     # this batch saving, the next asked: no more
    # Each request after the first went out while the batch before it was saved.
    saves = [(t, u) for (k, t, _), (k2, u, _) in zip(
        [e for e in log if e[0] == "save"], [e for e in log if e[0] == "saved"])]
    overlapped = sum(any(s < t < u for s, u in saves) for t in asks)
    assert overlapped == 5


@pytest.mark.asyncio
async def test_a_stop_ends_it_after_the_batch_already_asked_for_and_loses_nothing(
        state, monkeypatch):
    ids = await _companies(state, 550)
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    log = []
    _paced_anaf(monkeypatch, known, log)
    saved_batches = []

    async def saved(batch_ids, report):
        saved_batches.append(list(batch_ids))

    report = await runs.check_by_id(state, "anaf", "company_check", ids, saved=saved,
                                    stop=lambda: len(saved_batches) >= 1)
    asked = [n for kind, _, n in log if kind == "ask"]
    assert report["stopped"] == "stopped" and asked == [100, 100]
    # Every batch asked about was saved: none answered and dropped.
    assert [len(b) for b in saved_batches] == [100, 100]
    assert len(readings(state, "VAT_REGISTERED")) == 200 == report["updated"]


class _JobCtx:
    """What run_id_check is handed by the worker, enough of it: stops (a
    pause) once ``stop_after`` batches are saved."""

    def __init__(self, state, params, progress=None, stop_after=None):
        from types import SimpleNamespace

        self.state, self.params = state, params
        self.progress = dict(progress or {})
        self.outcome = ""
        self.saves = 0
        self.stop_after = stop_after
        self.id = "job1"
        self.job = SimpleNamespace(id="job1", batch_id="", lane="anaf")

    async def save(self, **changes):
        self.progress.update(changes)
        self.saves += 1

    async def stop_reason(self):
        if self.stop_after is not None and self.saves >= self.stop_after:
            self.outcome = "paused"
            return "paused"
        return ""


@pytest.mark.asyncio
async def test_a_paused_check_job_goes_on_from_where_it_got(state, monkeypatch):
    from harvey.modules import job_kinds

    ids = await _companies(state, 350)
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    asked = []
    stub_anaf(monkeypatch, known, asked)
    params = {"module": "anaf", "capability": "company_check", "company_ids": ids}
    first = _JobCtx(state, params, stop_after=1)
    await job_kinds.run_id_check(first)
    assert first.outcome == "paused" and first.progress["done"] == 200   # one more was asked
    assert first.progress["after"] == sorted(ids)[199]
    second = _JobCtx(state, params, progress=first.progress)
    result = await job_kinds.run_id_check(second)
    every = [c for batch in asked for c in batch]
    assert sorted(every) == sorted(known) and len(every) == len(set(every))   # each once
    assert result["updated"] == 350 and result["requests"] == 4 and result["companies"] == 350
    assert second.progress["done"] == 350
    assert len(readings(state, "VAT_REGISTERED")) == 350


@pytest.mark.asyncio
async def test_a_busy_anaf_hands_the_job_back_and_rests_the_lane(state, monkeypatch):
    from harvey.modules import job_kinds, jobs
    from harvey.modules.jobs import LaneExhausted

    ids = await _companies(state, 2250)
    queued, _ = await job_kinds.enqueue_id_checks(state, "anaf", "company_check", ids)
    first = await jobs.get(state, queued[0])
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    calls = []

    async def ask(client, cuis, **kw):
        calls.append(list(cuis))
        if len(calls) > 1:
            raise service.AnafError("ANAF stayed busy (HTTP 503) after 4 requests", busy=True)
        return {c: known[c] for c in cuis}, set(), 1

    monkeypatch.setattr(service, "ask", ask)
    ctx = _JobCtx(state, first.params)
    ctx.id = ctx.job.id = first.id
    with pytest.raises(LaneExhausted) as e:               # waits, without using up a try
        await job_kinds.run_id_check(ctx)
    assert e.value.retry_after == jobs.backoff_seconds(1)
    assert ctx.progress["done"] == 100 and ctx.progress["busy"] == 1 and len(calls) == 2
    # The lane's other jobs wait as long, rather than each asking a busy ANAF.
    with sqlite3.connect(state.db_path) as db:
        waiting = dict(db.execute(
            "SELECT id, not_before > datetime('now', '+20 seconds') FROM jobs"))
    assert waiting == {queued[0]: None, queued[1]: 1, queued[2]: 1}
    # Busy from its first batch on: still not a try used, and a longer wait.
    again = _JobCtx(state, first.params, progress=ctx.progress)
    with pytest.raises(LaneExhausted) as e:
        await job_kinds.run_id_check(again)
    assert e.value.retry_after == jobs.backoff_seconds(2) and again.progress["busy"] == 2
    assert calls[-1][0] not in calls[0]                   # it went on from where it got


@pytest.mark.asyncio
async def test_a_save_that_fails_part_way_never_ends_the_job_done(state, monkeypatch):
    from harvey.modules import job_kinds
    from harvey.modules.jobs import LaneExhausted, Transient

    ids = await _companies(state, 350)
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    stub_anaf(monkeypatch, known, [])
    real = ingest.checked
    fail_on = {2}
    calls = [0]

    async def checked(batch, module, found, **kw):
        calls[0] += 1
        if calls[0] in fail_on:
            raise sqlite3.OperationalError("database is locked")
        return await real(batch, module, found, **kw)

    monkeypatch.setattr(ingest, "checked", checked)
    params = {"module": "anaf", "capability": "company_check", "company_ids": ids}
    ctx = _JobCtx(state, params)
    with pytest.raises(LaneExhausted):                    # some saved: goes on later
        await job_kinds.run_id_check(ctx)
    assert ctx.progress["done"] == 100 and len(readings(state, "VAT_REGISTERED")) == 100
    # The first save fails: nothing went through, so a try is used.
    calls[0], fail_on = 0, {1}
    with pytest.raises(Transient):
        await job_kinds.run_id_check(_JobCtx(state, params, progress=ctx.progress))
    calls[0], fail_on = 0, set()
    done = _JobCtx(state, params, progress=ctx.progress)
    result = await job_kinds.run_id_check(done)
    assert done.outcome == "" and result["errors"] == [] and done.progress["done"] == 350
    assert len(readings(state, "VAT_REGISTERED")) == 350


@pytest.mark.asyncio
async def test_a_failed_batch_then_a_good_one_is_asked_again_not_skipped(state, monkeypatch):
    from harvey.modules import job_kinds
    from harvey.modules.jobs import LaneExhausted

    ids = await _companies(state, 350)
    known = {r["value"]: answer(r["value"]) for r in await state.companies_with_id("cui")}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    calls = []

    async def ask(client, cuis, **kw):
        calls.append(list(cuis))
        if len(calls) == 2:
            raise service.AnafError("ANAF refused the request (HTTP 400)")
        return {c: known[c] for c in cuis}, set(), 1

    monkeypatch.setattr(service, "ask", ask)
    params = {"module": "anaf", "capability": "company_check", "company_ids": ids}
    ctx = _JobCtx(state, params)
    with pytest.raises(LaneExhausted):
        await job_kinds.run_id_check(ctx)
    # Batches 3 and 4 were saved, but progress stays in front of batch 2.
    assert len(calls) == 4 and ctx.progress["done"] == 100
    assert ctx.progress["after"] == sorted(ids)[99]
    done = _JobCtx(state, params, progress=ctx.progress)
    result = await job_kinds.run_id_check(done)
    assert result["errors"] == [] and done.progress["done"] == 350
    assert calls[4] == calls[1]                           # the failed batch, asked again
    # Every company read once: the ones asked twice said nothing new.
    assert len(readings(state, "VAT_REGISTERED")) == 350


@pytest.mark.asyncio
async def test_a_companys_two_cuis_are_never_split_between_batches_or_jobs(state, monkeypatch):
    from harvey.modules import job_kinds, jobs

    ids = await _companies(state, 300)
    straddler = sorted(ids)[99]           # its two rows would be rows 99 and 100
    second = cui("9299999")
    with sqlite3.connect(state.db_path) as db:
        db.execute("INSERT INTO company_ids (company_id, kind, value) VALUES (?, 'cui', ?)",
                   (straddler, second))
    rows = await state.companies_with_id("cui", ids)
    assert [len(b) for b in runs._batches(rows, 100)] == [99, 100, 100, 2]
    known = {r["value"]: answer(r["value"]) for r in rows}
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    asked = []
    stub_anaf(monkeypatch, known, asked)
    params = {"module": "anaf", "capability": "company_check", "company_ids": ids}
    first = _JobCtx(state, params, stop_after=1)          # paused after its first batch
    await job_kinds.run_id_check(first)
    # Its first two batches saved: rows 0-98, then 99-198 (the second
    # starts with the company's two rows, so it ends at company 197).
    assert first.progress["after"] == sorted(ids)[197]
    await job_kinds.run_id_check(_JobCtx(state, params, progress=first.progress))
    every = [c for b in asked for c in b]
    assert second in every and sorted(set(every)) == sorted(known)
    # Jobs: the company in one job, once (its rows straddle 1,000).
    more = await _companies_from(state, 9210000, 800)
    everyone = ids + more
    order = sorted(everyone)
    with sqlite3.connect(state.db_path) as db:
        db.execute("INSERT INTO company_ids (company_id, kind, value) VALUES (?, 'cui', ?)",
                   (order[999], cui("9298888")))
        db.execute("DELETE FROM company_ids WHERE company_id = ? AND value = ?",
                   (straddler, second))
    queued, _ = await job_kinds.enqueue_id_checks(state, "anaf", "company_check", everyone)
    held = [(await jobs.get(state, j)).params["company_ids"] for j in queued]
    assert [len(h) for h in held] == [999, 101]
    assert sum(h.count(order[999]) for h in held) == 1


@pytest.mark.asyncio
async def test_companies_with_an_id_come_in_id_order_whatever_order_they_are_asked_in(state):
    # More than one read of 900, asked for in reverse: the reads used to
    # follow the order asked, each sorted on its own, so where a company
    # fell (and which job of 1,000 it went in) depended on its random id.
    ids = await _companies(state, 1000)
    rows = await state.companies_with_id("cui", sorted(ids, reverse=True))
    assert [r["id"] for r in rows] == sorted(ids)


async def _companies_from(state, start, n):
    return [await state.add_company(Company(name=f"EXEMPLU {start + i} SRL",
                                            cui=cui(str(start + i)), source="registry:onrc"))
            for i in range(n)]


@pytest.mark.asyncio
async def test_two_cuis_answering_differently_write_nothing_on_a_second_check(state, monkeypatch):
    a = await state.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
    with sqlite3.connect(state.db_path) as db:
        db.execute("INSERT INTO company_ids (company_id, kind, value) VALUES (?, 'cui', ?)",
                   (a, B))
    stub_anaf(monkeypatch, {A: answer(A, vat=True), B: answer(B, vat=False)})
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    first = await runs.check_by_id(state, "anaf", "company_check", [a])
    assert len(readings(state, "VAT_REGISTERED")) == 1    # one reading, not one per CUI
    again = await runs.check_by_id(state, "anaf", "company_check", [a])
    assert again["observations"] == 0 and len(readings(state, "VAT_REGISTERED")) == 1


@pytest.mark.asyncio
async def test_check_jobs_take_ten_requests_each(state):
    from harvey.modules import job_kinds, jobs

    ids = await _companies(state, 2050)
    queued, _ = await job_kinds.enqueue_id_checks(state, "anaf", "company_check", ids)
    found = [await jobs.get(state, j) for j in queued]
    assert [len(j.params["company_ids"]) for j in found] == [1000, 1000, 50]
    assert jobs.stoppable("id_check")


# ── The dashboard ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    import harvey.state as state_mod
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_a_company_panel_shows_findings_official_first(client, monkeypatch, tmp_path):
    import harvey.dashboard as dash

    async def setup():
        sm = StateManager(str(tmp_path / "harvey.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        cid = await sm.add_company(Company(name="EXEMPLU A SRL", cui=A, address="Str. Veche 2",
                                           source="registry:onrc"))
        for code in ("PHONE_NUMBER", "ADDRESS", "VAT_REGISTERED"):
            await sm.set_signal_status(code, "confirmed")
        await sm.add_observations([
            {"company_id": cid, "signal_code": "PHONE_NUMBER", "value_text": "+40722000210",
             "module": "phones", "capability": "site_phones", "collector": "profile",
             "observed_at": "2026-10-01 10:00:00"},
            {"company_id": cid, "signal_code": "PHONE_NUMBER", "value_text": "+40268000210",
             "module": "anaf", "capability": "company_check", "collector": "anaf",
             "observed_at": "2026-09-01 10:00:00"},
            {"company_id": cid, "signal_code": "ADDRESS", "value_text": "Str. Exemplu 1",
             "module": "anaf", "capability": "company_check", "collector": "anaf"},
            {"company_id": cid, "signal_code": "VAT_REGISTERED", "value_num": 1.0,
             "module": "anaf", "capability": "company_check", "collector": "anaf"},
        ])
        return cid

    cid = asyncio.run(setup())
    data = client.get(f"/api/companies/{cid}").json()
    phones = data["findings"]["phones"]
    assert [p["value"] for p in phones] == ["+40268000210", "+40722000210"]   # official first
    assert phones[0]["official"] and phones[0]["sources"][0]["name"] == "ANAF – date firmă"
    assert phones[0]["display"] == "+40 268 000 210"
    assert data["other_phones"][0] in ("+40 268 000 210", "+40268000210")
    addresses = [a["value"] for a in data["findings"]["addresses"]]
    assert set(addresses) == {"Str. Exemplu 1", "Str. Veche 2"}     # ANAF's and the registry's
    assert all(a["official"] for a in data["findings"]["addresses"])
    facts = {f["code"]: f for f in data["findings"]["facts"]}
    assert facts["VAT_REGISTERED"]["value_num"] == 1.0 and facts["VAT_REGISTERED"]["module"] == "anaf"
    assert dash._official_modules() >= {"anaf", "onrc", "mf_financials", "mf_taxpayers"}


def test_a_finding_lists_its_official_sources_newest_first(client, tmp_path):
    async def setup():
        sm = StateManager(str(tmp_path / "harvey.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        cid = await sm.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))
        await sm.set_signal_status("PHONE_NUMBER", "confirmed")
        await sm.add_observations([
            {"company_id": cid, "signal_code": "PHONE_NUMBER", "value_text": "+40268000210",
             "module": module, "capability": "x", "collector": module, "observed_at": when}
            for module, when in (("phones", "2026-10-20 10:00:00"),
                                 ("anaf", "2026-09-01 10:00:00"),
                                 ("mf_taxpayers", "2026-07-23 00:00:00"),
                                 ("anaf", "2026-10-01 10:00:00"))])
        return cid

    cid = asyncio.run(setup())
    sources = client.get(f"/api/companies/{cid}").json()["findings"]["phones"][0]["sources"]
    assert [(x["module"], x["observed_at"][:10]) for x in sources] == [
        ("anaf", "2026-10-01"), ("anaf", "2026-09-01"), ("mf_taxpayers", "2026-07-23"),
        ("phones", "2026-10-20")]


def test_the_dashboard_checks_a_selection_at_anaf(client, monkeypatch, tmp_path):
    async def setup():
        sm = StateManager(str(tmp_path / "harvey.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        await sm.set_signal_status("VAT_REGISTERED", "confirmed")
        return await sm.add_company(Company(name="EXEMPLU A SRL", cui=A, source="registry:onrc"))

    cid = asyncio.run(setup())
    stub_anaf(monkeypatch, {A: answer(A)})
    assert client.post("/api/anaf/check", json={"ids": [cid]}).json()["success"]
    for _ in range(50):
        data = client.get("/api/public-data").json()
        if not data["anaf"]["running"]:
            break
        time.sleep(0.1)
    job = data["anaf"]["job"]
    assert job["report"]["updated"] == 1 and job["done"] == job["total"] == 1
    assert data["anaf"]["on"] and data["anaf"]["kept"]["answers"] == 1
    assert data["taxpayers"]["on"] and data["taxpayers"]["loaded"] is None
    assert client.post("/api/anaf/check", json={"ids": "x"}).status_code == 400
    client.post("/api/modules/anaf", json={"enabled": False})
    refused = client.post("/api/anaf/check", json={})
    assert refused.status_code == 409 and refused.json()["off_module"] == "anaf"


# ── The command line ──

def test_cli_check_needs_to_be_told_which_companies(tmp_path, monkeypatch, capsys):
    import sys

    import harvey.state
    from harvey.cli import main

    monkeypatch.setattr(harvey.state, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(sys, "argv", ["harvey", "anaf", "check"])
    with pytest.raises(SystemExit) as stop:
        main()
    assert stop.value.code == 2 and "--missing" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["harvey", "anaf", "check", "--cui", A])
    with pytest.raises(SystemExit):
        main()
    assert "no company on file has this CUI" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["harvey", "anaf", "status"])
    main()
    assert "ANAF answers kept: 0" in capsys.readouterr().out


def test_the_manifest_says_what_the_check_declares():
    m = switches.by_id()["anaf"]
    cap = m.capabilities[0]
    assert cap["batch"] == 100 and cap["after_source"] and cap["lane"] == "anaf"
    assert set(cap["outputs"]["facts"]) == set(ANAF_CODES)
    assert m.official and m.default_enabled and m.requires == ("market_ro",)
    declared = {s["code"]: s for s in m.signals}
    assert declared["FISCALLY_INACTIVE"]["absence"] == "unknown"
    assert all(s["when_known"] == "source" for s in m.signals)
    assert m.settings["cache_days"]["default"] == 30
