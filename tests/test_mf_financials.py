"""The Ministry of Finance's annual financial statements (mf_financials).

Every file here is invented and written by hand in the published layout
(comma-separated, a CUI,CAEN,I1... header, empty cells for unreported
lines); every CUI is a fictional one, the same ones tests/test_onrc.py
gives its invented firms, so the registry and the statements join.
"""

import os
import sqlite3
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey import registry
from harvey.collectors import onrc
from harvey.modules import api, runs, switches
from harvey.modules.loader import KEYED_BY, load_manifests
from harvey.modules.ro.mf_financials import capabilities as caps
from harvey.modules.ro.mf_financials import source, store
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, firm, write

FIN_CODES = ("TURNOVER", "EMPLOYEES", "NET_RESULT", "EQUITY", "DEBTS", "TOTAL_ASSETS",
             "TURNOVER_GROWTH", "ACCOUNTS_FORM", "FILED_ACCOUNTS")
HEADER_2016 = "CUI,CAEN," + ",".join(f"I{i}" for i in range(1, 21))
HEADER_2015_BL = "CUI,CAEN," + ",".join(f"I{i}" for i in range(1, 22))


def row(cui, caen, *, fixed="", current="", prepaid="", debts="", equity="", turnover="",
        net_profit="", net_loss="", employees="", width=20, turnover_col=13):
    """One data line: the named cells at their columns, the rest empty."""
    cells = [""] * width
    named = {1: fixed, 2: current, 6: prepaid, 7: debts, 10: equity,
             turnover_col: turnover, turnover_col + 5: net_profit,
             turnover_col + 6: net_loss, width: employees}
    for col, value in named.items():
        cells[col - 1] = str(value)
    return ",".join([str(cui), str(caen), *cells])


def statement_file(path, header, rows):
    path.write_bytes(("\r\n".join([header, *rows]) + "\r\n").encode("ascii"))
    return path


# 90000017: an SRL with both years; 90000025: only 2025, its CAEN without
# the leading zero; 90000041: 2024 only (filed nothing for 2025).
BL_2025 = [row(90000017, 8623, fixed=100, current=400, prepaid=10, debts=200, equity=300,
               turnover=1_500_000, net_profit=120_000, net_loss=0, employees=12)]
UU_2025 = [row(90000025, 111, fixed=5, current=20, equity=-4, turnover=0, net_profit="",
               net_loss=3_000, employees=""),
           row("RO0090000033", 4773, turnover=50_000, employees=1),
           row("1900101123456", 8623, turnover=9, employees=1)]       # a personal code: never a CUI
IR_2025 = [row(90000058, 6419, turnover=9_000_000, employees=40)]
BL_2024 = [row(90000017, 8623, fixed=90, current=300, debts=150, equity=240,
               turnover=1_000_000, net_profit=80_000, net_loss=0, employees=10),
           row(90000041, 8623, turnover=200_000, employees=3)]
UU_2024 = [row(90000025, 111, turnover=0, employees=0)]
IR_2024 = [row(90000058, 6419, turnover=8_000_000, employees=38)]


@pytest.fixture
def files(tmp_path):
    folder = tmp_path / "raw"
    folder.mkdir()
    out = []
    for year, form, rows in ((2025, "BL", BL_2025), (2025, "UU", UU_2025), (2025, "IR", IR_2025),
                             (2024, "BL", BL_2024), (2024, "UU", UU_2024), (2024, "IR", IR_2024)):
        stem = {"BL": "web_bl_bs_sl_an", "UU": "web_uu_an", "IR": "web_ir_an"}[form]
        path = statement_file(folder / f"{stem}{year}.txt", HEADER_2016, rows)
        # 2025's first release (June 2026), and 2024's revision published
        # beside it (its final release, with the late filers).
        out.append(store.Source(year=year, form=form, path=path, name=path.name,
                                url=f"https://data.gov.ro/dataset/x/resource/{form}{year}/download/{path.name}",
                                package=f"situatii_financiare_{year}"
                                        + ("_actualizat" if year == 2024 else ""),
                                published="2026-06-11T10:00:00" if year == 2025
                                          else "2026-06-24T09:00:00", licence="CC-BY-4.0"))
    return out


@pytest.fixture
def fin_path(files, tmp_path):
    path = tmp_path / "financials.db"
    store.build(files, path)
    return path


# ── Reading the published files ──

def test_columns_are_mapped_per_year_and_form():
    assert store.columns_for(2025, "BL")["I13"] == "turnover"
    assert store.columns_for(2025, "UU")["I20"] == "employees"
    # Up to 2015 the long form has public patrimony at I13: one further on.
    assert store.columns_for(2015, "BL")["I14"] == "turnover"
    assert store.columns_for(2015, "BL")["I21"] == "employees"
    # The abridged form up to 2015 has neither patrimony line: one back.
    assert store.columns_for(2013, "UU")["I12"] == "turnover"
    assert store.columns_for(2013, "UU")["I19"] == "employees"
    assert "I12" not in store.columns_for(2025, "BL")      # the regie's patrimony: not loaded


def test_a_file_is_read_into_named_fields(files):
    uu = next(f for f in files if (f.year, f.form) == (2025, "UU"))
    rows = list(store.read_file(uu.path, 2025, "UU"))
    by_cui = {r[0]: dict(zip(("cui", "year", "form", "caen", *store.FIELDS), r)) for r in rows}
    assert set(by_cui) == {"90000025", "90000033"}         # the personal code is dropped
    a = by_cui["90000025"]
    assert a["caen"] == "0111"                             # the leading zero put back
    assert a["turnover"] == 0 and a["net_loss"] == 3_000 and a["equity"] == -4
    assert a["employees"] is None and a["net_profit"] is None   # empty is unknown, not 0


def test_an_old_long_form_reads_turnover_one_column_on(tmp_path):
    path = statement_file(tmp_path / "webblbsslan2015.txt", HEADER_2015_BL,
                          [row(90000017, 8623, turnover=700, employees=5, width=21,
                               turnover_col=14)])
    (r,) = store.read_file(path, 2015, "BL")
    got = dict(zip(("cui", "year", "form", "caen", *store.FIELDS), r))
    assert (got["turnover"], got["employees"]) == (700, 5)


def test_a_shifted_layout_is_refused_not_misread(tmp_path):
    # A future year with one more column: the headcount isn't last any more.
    path = statement_file(tmp_path / "web_bl_bs_sl_an2026.txt",
                          "CUI,CAEN," + ",".join(f"I{i}" for i in range(1, 22)),
                          [row(90000017, 8623, width=21)])
    with pytest.raises(store.FormatChanged, match="indicators.csv"):
        list(store.read_file(path, 2026, "BL"))


# ── The local database ──

def test_build_keeps_years_files_and_the_latest_year(fin_path):
    meta = store.status(fin_path)
    assert meta["years"] == "2025 2024"
    assert int(meta["rows"]) == 8 and int(meta["companies"]) == 5
    assert {(f["year"], f["form"]) for f in meta["files"]} == {
        (y, f) for y in (2025, 2024) for f in ("BL", "UU", "IR")}
    con = store.connect(fin_path)
    try:
        latest = {r["cui"]: dict(r) for r in con.execute("SELECT * FROM latest")}
        assert latest["90000041"]["year"] == 2024          # filed nothing for 2025
        assert latest["90000017"]["turnover"] == 1_500_000
        assert store.complete_years(con) == {2024}            # 2025's is a first release
        assert [r["year"] for r in store.accounts_for(con, "90000017")] == [2025, 2024]
    finally:
        con.close()


def test_a_reader_never_sees_a_half_built_database(files, fin_path, tmp_path):
    bad = statement_file(tmp_path / "broken.txt", "NOT,A,HEADER", [])
    with pytest.raises(store.FormatChanged):
        store.build([*files, store.Source(year=2023, form="BL", path=bad)], fin_path)
    assert store.status(fin_path)["years"] == "2025 2024"    # the old one stays
    assert not fin_path.with_name(fin_path.name + ".building").exists()


# ── Finding the files on data.gov.ro ──

def _res(name, created, size=2000, dataset="d1"):
    return {"url": f"https://data.gov.ro/dataset/{dataset}/resource/r/download/{name}",
            "created": created, "size": size, "format": "TXT"}


PACKAGES = [
    {"name": "situatii_financiare2023", "license_id": "CC-BY-4.0", "resources": [
        _res("web_bl_bs_sl_an2023.txt", "2024-07-12T13:25:34"),
        _res("web_bl_bs_sl_an2023.txt", "2025-07-27T14:11:28", dataset="d2"),   # revised, same name
        _res("web_uu_an2023.txt", "2024-07-12T13:42:09"),
        _res("web_uu_an2023.csv", "2024-07-12T13:41:16"),       # the column spec, not data
        _res("web_ong_an2023.txt", "2024-07-12T13:37:43"),      # another form
        _res("web_ifn2023.txt", "2024-07-12T13:30:37"),
    ]},
    {"name": "situatii_financiare_2024", "license_id": "CC-BY-4.0", "resources": [
        _res("web_bl_bs_sl_an2024.txt", "2025-06-23T10:00:00"),
        _res("web_uu_an2024.txt", "2025-06-23T10:00:00"),
        _res("web_ir_an2024.txt", "2025-06-23T10:00:00"),
    ]},
    {"name": "situatii_financiare_2024_actualizat", "license_id": "CC-BY-4.0", "resources": [
        _res("web_uu_an2024.txt", "2026-06-24T09:00:00", dataset="d3"),
    ]},
    {"name": "situatii_financiare_2010", "license_id": "uk-ogl", "resources": [
        _res("webblbsslan2010.txt", "2018-08-09T10:00:00"),
        {"url": "http://elsewhere.example/webblbsslan2010.txt", "created": "2030-01-01"},
    ]},
    {"name": "date_identificare_062025", "license_id": "CC-BY-4.0", "resources": [
        _res("web_bl_bs_sl_an2025.txt", "2026-01-01T00:00:00")]},   # not a statements package
]


def test_the_newest_file_per_year_and_form_wins_wherever_published():
    found = source.resources(PACKAGES)
    assert sorted(found) == [(2010, "BL"), (2023, "BL"), (2023, "UU"),
                             (2024, "BL"), (2024, "IR"), (2024, "UU")]
    assert "/d2/" in found[(2023, "BL")].url                    # the revised copy
    assert found[(2024, "UU")].package == "situatii_financiare_2024_actualizat"
    assert found[(2010, "BL")].licence == "OGL-ROU-1.0"
    assert found[(2010, "BL")].url.startswith("https://data.gov.ro/")
    assert source.published_years(found) == [2024, 2023, 2010]


@pytest.mark.parametrize("name,expected", [
    ("web_bl_bs_sl_an2025.txt", (2025, "BL")), ("webblbsslan2016.txt", (2016, "BL")),
    ("web2008.txt", (2008, "BL")), ("web_uu_2011.txt", (2011, "UU")),
    ("webuuan2016.txt", (2016, "UU")), ("webiran2012.txt", (2012, "IR")),
    ("web_ir_2012.txt", (2012, "IR")), ("web_ir_an2025.txt", (2025, "IR")),
    ("web_ong_an2025.txt", None), ("web_ifn2025.txt", None), ("webasig2023.txt", None),
    ("web_uu_an2025.csv", None), ("descriere-fisiere-web.txt", None),
])
def test_file_names_say_their_year_and_form(name, expected):
    assert source.classify(name) == expected


def _ckan_transport(packages, bodies):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/package_search"):
            assert request.url.params["fq"] == "organization:mfp"
            return httpx.Response(200, json={"result": {"results": packages}})
        name = request.url.path.rsplit("/", 1)[-1]
        if name in bodies:
            return httpx.Response(200, content=bodies[name])
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _published(files):
    """The fixture's files as data.gov.ro would list them, and their bytes."""
    packages, bodies = {}, {}
    for f in files:
        data = f.path.read_bytes()
        bodies[f.path.name] = data
        packages.setdefault(f.year, {"name": f"situatii_financiare_{f.year}",
                                     "license_id": "CC-BY-4.0", "resources": []})
        packages[f.year]["resources"].append({
            "url": f"https://data.gov.ro/dataset/p{f.year}/resource/{f.form}/download/{f.path.name}",
            "created": f"{f.year + 1}-06-11T10:00:00", "size": len(data)})
    return list(packages.values()), bodies


@pytest.mark.asyncio
async def test_sync_downloads_once_loads_and_then_has_nothing_to_do(files, tmp_path, monkeypatch):
    monkeypatch.setattr(source, "MIN_BYTES", 10)
    packages, bodies = _published(files)
    root, path = tmp_path / "downloads", tmp_path / "fin.db"
    settings = {"path": str(path), "downloads": str(root), "years": 3}
    sync = api.capability("mf_financials", "statements_sync")
    async with httpx.AsyncClient(transport=_ckan_transport(packages, bodies)) as http:
        first = await sync(api.Input(), api.Context(http=http, settings=settings))
        assert len(first.values["downloaded"]) == 6
        assert store.status(path)["years"] == "2025 2024"
        # Kept by publication date and name, with its listing beside it.
        kept = sorted(p.name for p in (root / "2025").iterdir())
        assert "20260611-web_bl_bs_sl_an2025.txt" in kept
        assert "20260611-web_bl_bs_sl_an2025.txt.json" in kept
        again = await sync(api.Input(), api.Context(http=http, settings=settings))
        assert again.values["unchanged"] and again.values["downloaded"] == []
        check = await api.capability("mf_financials", "statements_check")(
            api.Input(), api.Context(http=http, settings=settings))
        assert check.values["updates"] == []
    # data.gov.ro unreachable: what is downloaded is loaded again.
    path.unlink()
    offline = await sync(api.Input(), api.Context(settings=settings))
    assert offline.values["offline"] and store.status(path)["years"] == "2025 2024"


@pytest.mark.asyncio
async def test_one_year_wanted_loads_only_the_latest(files, tmp_path, monkeypatch):
    monkeypatch.setattr(source, "MIN_BYTES", 10)
    packages, bodies = _published(files)
    path = tmp_path / "fin.db"
    settings = {"path": str(path), "downloads": str(tmp_path / "dl"), "years": 1}
    async with httpx.AsyncClient(transport=_ckan_transport(packages, bodies)) as http:
        await api.capability("mf_financials", "statements_sync")(
            api.Input(), api.Context(http=http, settings=settings))
    assert store.status(path)["years"] == "2025"


def test_updates_say_new_revised_and_dropped():
    found = source.resources(PACKAGES)
    loaded = {"files": [
        {"year": 2023, "form": "BL", "url": "https://data.gov.ro/dataset/d1/x", "published": "2024-07-12"},
        {"year": 2010, "form": "BL", "url": found[(2010, "BL")].url, "published": "2018-08-09T10:00:00"},
    ]}
    got = {(u["year"], u["form"]): u["why"] for u in caps.updates(found, loaded, 2)}
    assert got[(2023, "BL")] == "revised" and got[(2024, "UU")] == "new"
    assert got[(2010, "BL")] == "dropped"


# ── Facts ──

def _accounts(fin_path, cui):
    con = store.connect(fin_path)
    try:
        return store.accounts_for(con, cui), store.complete_years(con), store.file_urls(con)
    finally:
        con.close()


def _by(facts):
    return {(f.code, f.value_text): f.value_num for f in facts}


def test_facts_one_per_year_with_derived_figures(fin_path):
    rows, complete, urls = _accounts(fin_path, "90000017")
    got = _by(caps.facts_for(rows, complete=complete, evidence=urls))
    assert got[("TURNOVER", "2025")] == 1_500_000 and got[("TURNOVER", "2024")] == 1_000_000
    assert got[("EMPLOYEES", "2025")] == 12
    assert got[("NET_RESULT", "2025")] == 120_000
    assert got[("TOTAL_ASSETS", "2025")] == 510 and got[("TOTAL_ASSETS", "2024")] == 390
    assert got[("TURNOVER_GROWTH", "2025")] == 50.0
    assert ("TURNOVER_GROWTH", "2024") not in got           # no 2023 on file
    assert got[("FILED_ACCOUNTS", "2025")] == 1
    assert got[("ACCOUNTS_FORM", "BL")] is None
    assert all(f.source_url.startswith("https://data.gov.ro/") for f in
               caps.facts_for(rows, complete=complete, evidence=urls))


def test_no_growth_from_zero_and_a_loss_is_negative(fin_path):
    rows, complete, urls = _accounts(fin_path, "90000025")
    got = _by(caps.facts_for(rows, complete=complete, evidence=urls))
    assert got[("TURNOVER", "2025")] == 0                  # a real 0, kept
    assert ("TURNOVER_GROWTH", "2025") not in got          # previous year was 0
    assert got[("NET_RESULT", "2025")] == -3_000
    assert ("EMPLOYEES", "2025") not in got                # empty: unknown


def test_a_missing_year_is_no_only_for_a_firm_that_files(fin_path):
    # 90000033 filed for 2025 only: 2024, a final release, says it didn't.
    rows, complete, urls = _accounts(fin_path, "90000033")
    srl = {"form": "SRL", "registered": 2010, "operating": True}
    got = _by(caps.facts_for(rows, complete=complete, evidence=urls, firm=srl))
    assert got[("FILED_ACCOUNTS", "2024")] == 0 and got[("FILED_ACCOUNTS", "2025")] == 1
    for form in ("PFA", "II", "IF"):
        sole = _by(caps.facts_for(rows, complete=complete, evidence=urls,
                                  firm={"form": form, "registered": 2010, "operating": True}))
        assert ("FILED_ACCOUNTS", "2024") not in sole       # not applicable, never "no"
    young = _by(caps.facts_for(rows, complete=complete, evidence=urls,
                               firm={"form": "SRL", "registered": 2024, "operating": True}))
    assert ("FILED_ACCOUNTS", "2024") not in young
    unknown = _by(caps.facts_for(rows, complete=complete, evidence=urls, firm=None))
    assert ("FILED_ACCOUNTS", "2024") not in unknown        # no registry: unknown


def test_a_first_release_never_says_did_not_file(fin_path):
    # 90000041 is missing from 2025's first release: a late filer is still
    # to come in the revision a year later, so it stays unknown.
    rows, complete, urls = _accounts(fin_path, "90000041")
    srl = {"form": "SRL", "registered": 2010, "operating": True}
    got = _by(caps.facts_for(rows, complete=complete, evidence=urls, firm=srl))
    assert ("FILED_ACCOUNTS", "2025") not in got and got[("FILED_ACCOUNTS", "2024")] == 1


@pytest.mark.parametrize("year,package,published,final", [
    (2025, "situatii_financiare_2025", "2026-06-11T11:28:50", False),          # first release
    (2024, "situatii_financiare_2024_actualizat", "2026-06-24T13:32:09", True),
    (2023, "situatii_financiare2023", "2025-07-27T14:11:28", True),            # re-uploaded later
    (2022, "situatii_financiare_2022", "2023-08-28T10:00:00", False),
    (2022, "situatii_financiare_2022", "", False),
])
def test_which_releases_are_final(year, package, published, final):
    assert store.final_release(year, package, published) is final


def test_a_struck_off_firm_never_gets_did_not_file(fin_path):
    rows, complete, urls = _accounts(fin_path, "90000033")
    gone = {"form": "SRL", "registered": 2005, "operating": False}
    got = _by(caps.facts_for(rows, complete=complete, evidence=urls, firm=gone))
    assert ("FILED_ACCOUNTS", "2024") not in got and got[("FILED_ACCOUNTS", "2025")] == 1


def test_the_registry_lookup_by_cui_uses_its_index(reg, reg_path):
    plan = " ".join(r[3] for r in reg.execute(
        "EXPLAIN QUERY PLAN SELECT form, registered, operating FROM firms "
        "WHERE cui = ? AND cui != '' ORDER BY operating DESC LIMIT 1", ("90000017",)))
    assert "firms_cui" in plan and "SCAN firms" not in plan
    import inspect
    assert "cui != ''" in inspect.getsource(caps.registry_firm)
    assert caps.registry_firm(reg_path, "90000017")["form"] == "SRL"
    assert caps.registry_firm(reg, "90000155")["form"] == "PFA"       # an open connection


@pytest.mark.asyncio
async def test_a_lookup_pass_opens_each_database_once_a_batch(state, monkeypatch):
    for i, cui in enumerate(("90000017", "90000025", "RO90000033", "90000041", "90000058")):
        await _company(state, f"EXEMPLU {i} SRL", cui)
    await state.set_signal_status("TURNOVER", "confirmed")
    opened, calls = [], []
    real_connect, real_run = store.connect, caps._lookup

    def connect(path=None):
        opened.append(path)
        return real_connect(path)

    async def lookup(inp, ctx):
        calls.append(len(inp.extra.get("batch") or []))
        return await real_run(inp, ctx)

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setitem(caps.CAPABILITIES, "statements_lookup",
                        api.Capability("mf_financials", "statements_lookup", lookup))
    monkeypatch.setattr(runs, "SOURCE_SAVE_BATCH", 3)
    report = await runs.lookup_by_id(state)
    assert report["companies"] == 5
    assert calls == [3, 2] and len(opened) == 2           # one call, one open, per batch
    assert ("2025", 50_000.0) in _values(state, "TURNOVER")      # facts land on the right company


def test_a_sole_trader_with_no_statement_gets_no_facts(fin_path):
    rows, complete, urls = _accounts(fin_path, "90000155")
    assert caps.facts_for(rows, complete=complete, evidence=urls,
                          firm={"form": "PFA", "registered": 2015}) == []


# ── Into Harvey: the gate, once per published file, cohorts by the latest year ──

@pytest_asyncio.fixture
async def state(fin_path, monkeypatch):
    monkeypatch.setattr(store, "FINANCIALS_PATH", fin_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", fin_path.with_name("no-registry.db"))
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        yield sm


async def _company(state, name, cui):
    from harvey.models import Company

    return await state.add_company(Company(name=name, cui=cui, source="registry:onrc"))


def _values(state, code):
    with sqlite3.connect(state.db_path) as db:
        return sorted((r[0], r[1]) for r in db.execute(
            "SELECT value_text, value_num FROM observations WHERE signal_code = ?", (code,)))


@pytest.mark.asyncio
async def test_lookups_write_only_confirmed_facts_once_per_file(state):
    a = await _company(state, "ZÂMBET EXEMPLU SRL", "90000017")
    await _company(state, "CLUJ EXEMPLU SRL", "90000041")
    nothing = await runs.lookup_by_id(state)
    assert nothing["observations"] == 0
    assert set(nothing["unconfirmed"]["mf_financials"]) == set(FIN_CODES)

    for code in ("TURNOVER", "EMPLOYEES"):
        await state.set_signal_status(code, "confirmed")
    first = await runs.lookup_by_id(state)
    assert first["companies"] == 2 and first["observations"] == 6
    assert _values(state, "TURNOVER") == [("2024", 200_000.0), ("2024", 1_000_000.0),
                                          ("2025", 1_500_000.0)]
    assert _values(state, "NET_RESULT") == []                     # not confirmed
    again = await runs.lookup_by_id(state)
    assert again["observations"] == 0                             # same files: nothing new
    with sqlite3.connect(state.db_path) as db:
        who = db.execute("SELECT DISTINCT module, capability, collector FROM observations "
                         "WHERE company_id = ?", (a,)).fetchall()
    assert who == [("mf_financials", "statements_lookup", "financials")]


@pytest.mark.asyncio
async def test_a_range_compares_the_latest_year_or_the_year_named(state):
    big = await _company(state, "ZÂMBET EXEMPLU SRL", "90000017")
    dormant = await _company(state, "DINTE TEST SRL", "90000025")
    old = await _company(state, "CLUJ EXEMPLU SRL", "90000041")
    unseen = await _company(state, "FARA CIFRE SRL", "90000163")
    await state.set_signal_status("TURNOVER", "confirmed")
    await runs.lookup_by_id(state)

    def verdicts(ranges):
        async def go():
            _, rows = await state.cohort_verdicts([], [], ranges,
                                                  verdicts=("match", "possible", "no_match"))
            return {r["id"]: r["verdict"] for r in rows}
        return go()

    over = await verdicts([{"code": "TURNOVER", "min": 1_200_000}])
    assert over[big] == "match"                     # 2025's 1.5M, not 2024's 1M
    assert over[old] == "no_match" and over[unseen] == "possible"
    in_2024 = await verdicts([{"code": "TURNOVER", "min": 1_200_000, "key": "2024"}])
    assert in_2024[big] == "no_match"
    zero = await verdicts([{"code": "TURNOVER", "max": 10}])
    assert zero[dormant] == "match"                 # 0 is a figure, not "not found"


# ── Filtering the registry before anything is pulled ──

PFA_WITH_CUI = firm("POPESCU EXEMPLU PFA", "90000155", "F08/810/2019", "PFA", "Braşov",
                    "Municipiul Braşov", "Str. Exemplului", "4")


@pytest.fixture
def reg(tmp_path):
    folder = tmp_path / "onrc"
    folder.mkdir()
    raw = {"caen": write(folder / onrc.FILES["caen"], CAEN + [("F08/810/2019", "8623", "2")]),
           "status": write(folder / onrc.FILES["status"], STATUS + [("F08/810/2019", "1048", "")]),
           "firms": write(folder / onrc.FILES["firms"], FIRMS + [PFA_WITH_CUI]),
           "people": write(folder / onrc.FILES["people"], PEOPLE)}
    path = tmp_path / "registry.db"
    registry.build(raw, DATASET, path)
    con = registry.connect(path)
    yield con
    con.close()


@pytest.fixture
def reg_path(reg, tmp_path):
    return tmp_path / "registry.db"


def _names(found):
    return sorted(r["name"] for r in found["rows"])


def test_turnover_and_headcount_filter_the_registry(reg, fin_path):
    every = _names(registry.search(reg, "dentist BV"))
    assert "ZÂMBET EXEMPLU SRL" in every and "DINTE TEST SRL" in every
    fin = store.Filter(min_turnover=1_000_000)
    found = registry.search(reg, "dentist BV", fin=fin, fin_path=fin_path)
    assert _names(found) == ["ZÂMBET EXEMPLU SRL"] and found["total"] == 1
    assert found["rows"][0]["financials"] == {"year": 2025, "form": "BL",
                                              "turnover": 1_500_000, "employees": 12}
    small = store.Filter(max_turnover=100, unknown="include")
    kept = _names(registry.search(reg, "dentist BV", fin=small, fin_path=fin_path))
    assert "DINTE TEST SRL" in kept                        # turnover 0
    assert "POPESCU EXEMPLU PFA" in kept                   # no statement: unknown, included
    assert "ZÂMBET EXEMPLU SRL" not in kept
    strict = store.Filter(max_turnover=100)
    assert "POPESCU EXEMPLU PFA" not in _names(
        registry.search(reg, "dentist BV", fin=strict, fin_path=fin_path))
    staff = store.Filter(min_employees=5, max_employees=20)
    assert _names(registry.search(reg, "8623", fin=staff, fin_path=fin_path)) == [
        "ZÂMBET EXEMPLU SRL"]


def test_bounds_without_loaded_statements_are_refused(reg, tmp_path):
    with pytest.raises(FileNotFoundError, match="harvey financials load"):
        registry.search(reg, "dentist BV", fin=store.Filter(min_turnover=1),
                        fin_path=tmp_path / "none.db")


@pytest.mark.asyncio
async def test_a_pull_with_bounds_saves_only_the_matches_with_their_figures(reg, reg_path, state,
                                                                           monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", reg_path)
    await state.set_signal_status("TURNOVER", "confirmed")
    await state.set_signal_status("FILED_ACCOUNTS", "confirmed")
    q = registry.resolve(reg, "dentist BV", fin=store.Filter(min_turnover=1_000_000),
                         fin_path=store.FINANCIALS_PATH)
    report = await runs.registry_pull(state, reg, q, with_administrators=False)
    assert report.new_companies == 1
    company = await state.get_company_by_cui("90000017")
    assert company is not None
    # Figures attached straight away, so a filter on them is decided now.
    assert ("2025", 1_500_000.0) in _values(state, "TURNOVER")


@pytest.mark.asyncio
async def test_a_sole_traders_missing_statement_is_never_a_no(reg, reg_path, state, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", reg_path)
    await state.set_signal_status("FILED_ACCOUNTS", "confirmed")
    await runs.registry_pull(state, reg, registry.resolve(reg, "dentist BV"),
                             with_administrators=False)
    sole = await state.get_company_by_cui("90000155")
    srl = await state.get_company_by_cui("90000025")
    with sqlite3.connect(state.db_path) as db:
        def filed(cid):
            return sorted(db.execute("SELECT value_text, value_num FROM observations WHERE "
                                     "company_id = ? AND signal_code = 'FILED_ACCOUNTS'",
                                     (cid,)).fetchall())
        assert filed(sole.id) == []
        assert filed(srl.id) == [("2024", 1.0), ("2025", 1.0)]
    # Its registered office is kept, like any firm's.
    assert sole.address == "Str. Exemplului 4"


# ── The module itself ──

def test_the_manifest_declares_yearly_signals():
    (m,) = [m for m in load_manifests(data_dir=None) if m.id == "mf_financials"]
    yearly = {s["code"] for s in m.signals if s["keyed_by"] == "year"}
    assert yearly == set(FIN_CODES) - {"ACCOUNTS_FORM"}
    assert all(s["when_known"] == "source" and s["cost_note"].startswith("free")
               for s in m.signals)
    assert "year" in KEYED_BY
    assert "Ministerul Finanțelor" in m.attribution


@pytest.mark.asyncio
async def test_settings_are_typed_and_clamped(state):
    assert (await switches.module_settings(state, "mf_financials")) == {
        "years": 3, "check_days": 30, "auto_update": False}
    assert await switches.set_module_setting(state, "mf_financials", "years", "99") == 30
    assert await switches.set_module_setting(state, "mf_financials", "auto_update", True) is True
    got = await switches.module_settings(state, "mf_financials")
    assert got["years"] == 30 and got["auto_update"] is True
    with pytest.raises(ValueError):
        await switches.set_module_setting(state, "mf_financials", "enabled", "on")


@pytest.mark.asyncio
async def test_nothing_is_downloaded_unless_auto_update_is_on(state, monkeypatch):
    called = []

    async def fake_sync(*a, **k):
        called.append(a)
        return api.Draft(values={"unchanged": True})

    monkeypatch.setattr(runs, "sync_source", fake_sync)
    assert await runs.sync_due_sources(state) == []
    await switches.set_module_setting(state, "mf_financials", "auto_update", True)
    assert await runs.sync_due_sources(state) == ["mf_financials"]
    assert await runs.sync_due_sources(state) == []       # not due again for check_days
    assert len(called) == 1


@pytest.mark.asyncio
async def test_a_lookup_with_nothing_loaded_is_skipped_quietly(state, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "FINANCIALS_PATH", tmp_path / "missing.db")
    await _company(state, "ZÂMBET EXEMPLU SRL", "90000017")
    await state.set_signal_status("TURNOVER", "confirmed")
    got = await runs.lookup_by_id(state)
    assert got["errors"] == [] and got["skipped"] and got["observations"] == 0
    with sqlite3.connect(state.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM runs WHERE stage = 'lookup'").fetchone() == (0,)


# ── The dashboard and the CLI ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(store, "FINANCIALS_PATH", tmp_path / "financials.db")
    monkeypatch.setattr(source, "DOWNLOADS", tmp_path / "mf")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_public_data_says_what_is_loaded_and_takes_settings(client):
    data = client.get("/api/public-data").json()
    fin = data["financials"]
    assert data["registry"] is None and fin["on"] and fin["loaded"] is None
    assert fin["settings"] == {"years": 3, "check_days": 30, "auto_update": False}
    assert "Ministerul Finanțelor" in fin["attribution"] and not fin["running"]
    assert "CC-BY 4.0" in fin["attribution"]               # the licence reaches the panel
    assert "CC-BY 4.0" in data["taxpayers"]["attribution"] and "ANAF" in data["anaf"]["attribution"]
    r = client.post("/api/public-data/financials/settings", json={"years": 5, "auto_update": True})
    assert r.json()["financials"]["settings"]["years"] == 5
    assert r.json()["financials"]["settings"]["auto_update"] is True
    assert client.post("/api/public-data/financials/settings",
                       json={"path": "/etc"}).status_code == 400
    client.post("/api/modules/mf_financials", json={"enabled": False})
    refused = client.post("/api/public-data/financials/load", json={})
    assert refused.status_code == 409 and refused.json()["off_module"] == "mf_financials"


def test_a_cohort_range_can_name_a_year():
    import harvey.dashboard as dash

    assert dash._ranges("TURNOVER:1000000::2024") == [
        {"code": "TURNOVER", "min": 1_000_000.0, "max": None, "key": "2024"}]
    assert dash._ranges([{"code": "TURNOVER", "min": 1}]) == [
        {"code": "TURNOVER", "min": 1.0, "max": None}]
    with pytest.raises(ValueError, match="4 digits"):
        dash._ranges([{"code": "TURNOVER", "min": 1, "key": "last"}])


@pytest.mark.asyncio
async def test_an_export_credits_the_sources_its_data_came_from(state):
    from harvey.export import latest_year_figures, source_notes

    a = await _company(state, "ZÂMBET EXEMPLU SRL", "90000017")
    assert await source_notes(state, [a]) == ["ONRC open data, data.gov.ro (CC-BY 4.0)"]
    await state.set_signal_status("TURNOVER", "confirmed")
    await runs.lookup_by_id(state)
    notes = await source_notes(state, [a])
    assert any("Ministerul Finanțelor" in n for n in notes)
    assert (await latest_year_figures(state, [a], ("TURNOVER",)))[a] == (
        "2025", {"TURNOVER": 1_500_000.0})


def test_cli_status_prints_json(tmp_path, monkeypatch, capsys):
    import sys

    import harvey.state
    from harvey.cli import main

    monkeypatch.setattr(harvey.state, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(store, "FINANCIALS_PATH", tmp_path / "financials.db")
    monkeypatch.setattr(source, "DOWNLOADS", tmp_path / "mf")
    monkeypatch.setattr(sys, "argv", ["harvey", "financials", "status", "--json"])
    main()
    import json

    out = json.loads(capsys.readouterr().out)
    assert out["loaded"] is None and out["settings"]["years"] == 3


# ── What the listing says is not trusted with the disk or the network ──

def _listed(created, name="web_uu_an2025.txt"):
    return [{"name": "situatii_financiare_2025", "license_id": "cc-by-4.0", "resources": [
        {"url": f"https://data.gov.ro/dataset/x/resource/y/download/{name}",
         "created": created, "size": "2000"}]}]


@pytest.mark.parametrize("created", ["/tmp/elsewhere", "../../../x", "2026-06-11/../../x"])
def test_a_publication_date_never_leaves_the_downloads_folder(tmp_path, created):
    root = tmp_path / "data" / "mf_financials"
    res = source.resources(_listed(created))[(2025, "UU")]
    target = source.local_file(res, root)
    assert target.resolve().is_relative_to(root.resolve())
    assert target.name == "undated-web_uu_an2025.txt" and res.published == ""


def test_an_undated_file_never_wins_over_a_dated_one():
    found = source.resources([{"name": "situatii_financiare_2025", "license_id": "cc-by-4.0",
                               "resources": [_res("web_uu_an2025.txt", None),
                                             _res("web_uu_an2025.txt", "2026-06-11T10:00:00",
                                                  dataset="dated")]}])
    assert "/dated/" in found[(2025, "UU")].url


def test_a_resource_named_with_odd_characters_is_skipped_and_never_a_path(tmp_path):
    assert source.resources(_listed("2026-06-11", name="web_uu_an2025%2F..txt")) == {}
    bad = source.Resource(year=2025, form="UU", url="https://data.gov.ro/x", name="../x.txt",
                          package="p", published="2026-06-11", licence="")
    with pytest.raises(ValueError):
        source.local_file(bad, tmp_path)


def _body():
    return b"CUI,CAEN,I1\n" + b"1,2,3\n" * 400


@pytest.mark.asyncio
async def test_a_redirect_off_data_gov_ro_is_refused(tmp_path):
    res = source.resources(_listed("2026-06-11T10:00:00"))[(2025, "UU")]
    res.size = len(_body())
    hosts = []

    def handler(request):
        hosts.append(f"{request.url.scheme}://{request.url.host}")
        if request.url.host == "data.gov.ro":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/internal.txt"})
        return httpx.Response(200, content=_body())

    # The client sync_source opens follows redirects; the download doesn't.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 follow_redirects=True) as client:
        with pytest.raises(RuntimeError, match="only https://data.gov.ro"):
            await source.fetch(client, res, tmp_path / "dl")
    assert hosts == ["https://data.gov.ro"]


@pytest.mark.asyncio
async def test_a_redirect_within_data_gov_ro_is_followed(tmp_path):
    res = source.resources(_listed("2026-06-11T10:00:00"))[(2025, "UU")]
    res.size = len(_body())

    def handler(request):
        if request.url.path.endswith("/y/download/web_uu_an2025.txt"):
            return httpx.Response(302, headers={"Location": "/storage/web_uu_an2025.txt"})
        return httpx.Response(200, content=_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await source.fetch(client, res, tmp_path / "dl")
    assert got.read_bytes() == _body()


@pytest.mark.asyncio
async def test_a_download_is_refused_when_the_disk_is_short(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    res = source.resources(_listed("2026-06-11T10:00:00"))[(2025, "UU")]
    res.size = 80_000_000
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10**12, 10**12, 50_000_000))
    asked = []
    transport = httpx.MockTransport(lambda r: asked.append(r) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="not enough free disk space"):
            await source.fetch(client, res, tmp_path / "dl")
    assert asked == []


# ── An export row's figures are all one year's ──

@pytest.mark.asyncio
async def test_an_export_never_mixes_two_years_figures(state):
    from harvey.export import latest_year_figures, yearly_codes

    a = await _company(state, "EXEMPLU A SRL", "90000017")
    await state.add_observations([
        {"company_id": a, "signal_code": "TURNOVER", "value_num": 1_000_000.0,
         "value_text": "2024", "module": "mf_financials", "collector": "financials"},
        {"company_id": a, "signal_code": "EMPLOYEES", "value_num": 12.0,
         "value_text": "2025", "module": "mf_financials", "collector": "financials"},
        {"company_id": a, "signal_code": "FILED_ACCOUNTS", "value_num": 0.0,
         "value_text": "2026", "module": "mf_financials", "collector": "financials"}])
    codes = yearly_codes({"mf_financials"})
    assert codes[:2] == ["TURNOVER", "EMPLOYEES"] and "FILED_ACCOUNTS" not in codes
    assert yearly_codes({"onrc"}) == []                    # no such columns without the module
    # 2025's headcount alone, not 2024's turnover labelled 2025.
    assert (await latest_year_figures(state, [a], codes))[a] == ("2025", {"EMPLOYEES": 12.0})


def test_the_unknown_switch_covers_turnover_and_headcount():
    import argparse

    from harvey.cli import _fin_filter_args

    p = argparse.ArgumentParser()
    _fin_filter_args(p)
    args = p.parse_args(["--max-employees", "9", "--unknown", "include"])
    assert args.unknown_figures == "include"
    assert "headcount" in p.format_help()
    with pytest.raises(SystemExit):
        p.parse_args(["--unknown-revenue", "include"])


@pytest.mark.parametrize("raw,clean,valid", [
    ("RO 0091000012", "91000012", "91000012"),  # fictional, its check digit right
    ("91000013", "91000013", ""),                # wrong check digit
    ("1900101123456", "", ""),                   # a personal numeric code (CNP)
    ("0", "", ""), ("", "", ""),
])
def test_one_cui_rule_for_every_romanian_module(raw, clean, valid):
    from harvey.modules.ro import cui
    from harvey.modules.ro.anaf import service
    from harvey.modules.ro.mf_taxpayers import store as taxpayers

    assert cui.clean(raw) == onrc.valid_cui(raw) == store.clean_cui(raw) == \
        taxpayers.clean_cui(raw) == clean
    assert cui.valid(raw) == service.clean_cui(raw) == valid
