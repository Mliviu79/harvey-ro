"""The Ministry of Finance's taxpayer identification file (mf_taxpayers).

Every file here is invented and written by hand in the published layout
('^'-separated, UTF-8 with a byte-order mark, a header row, each data row
ending with the separator); every CUI, name, address and phone is
fictional.
"""

import os
import sqlite3
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.models import Company
from harvey.modules import api, runs, switches
from harvey.modules.ro.mf_taxpayers import capabilities as caps
from harvey.modules.ro.mf_taxpayers import source, store
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

COLUMNS = ("COD_FISCAL^DENUMIRE^COD_FISCAL_PARINTE^TIP_UNITATE^TIP_CONTRIB^LOCALITATE^STRADA^NR^"
           "DATA_INREGISTRARE^DATA_PRELUCRARE^FAX^SECTOR^TELEFON^JUDET_COMERT^NR_COMERT^AN_COMERT^"
           "ACT_AUTORIZARE^TVA^DATA_RADIERE^COD_POSTAL^DATA_STARE^STARE^JUDET^IMP100^IMP120^"
           "CONT480^IMP602^DETALII_ADRESA^BLOC^SCARA^ETAJ^AP").split("^")


def line(**cells) -> str:
    """One data row: the named cells, the rest empty, ending with '^'."""
    return "^".join(cells.get(c.lower(), "") for c in COLUMNS) + "^"


ROWS = [
    # An SRL: VAT, employer, micro-enterprise tax, a landline.
    line(cod_fiscal="90000014", denumire="EXEMPLU ALFA SRL", tip_unitate="Sediu central",
         tip_contrib="PJ", localitate="Mun. Braşov", strada="Str. Exemplu", nr="1",
         data_inregistrare="01.03.2015", telefon="0268100200", tva="DA", cod_postal="500001",
         data_stare="01.03.2015 10:00:00", stare="INREGISTRAT", judet="BRAŞOV", imp100="NU ",
         imp120="DA ", cont480="DA ", imp602="DA ", bloc="A1", ap="3"),
    # A PFA: kept like any other legal form, its address and phone included.
    line(cod_fiscal="90000022", denumire="POPESCU EXEMPLU PFA", tip_unitate="Sediu central",
         tip_contrib="PF", localitate="Sat Exemplu", strada="Str. Câmpului", nr="9",
         telefon="0722100200 / 0268 100 300", tva="NU", stare="INREGISTRAT", judet="BRAŞOV",
         imp100="NU ", imp120="NU ", cont480="NU ", imp602="NU "),
    # A tax code that is a personal numeric code: never kept.
    line(cod_fiscal="1900101123456", denumire="NUME PERSOANA", tip_contrib="PF",
         localitate="Sat Exemplu", tva="NU", stare="INREGISTRAT"),
    # Struck off, no phone, profit tax.
    line(cod_fiscal="90000030", denumire="EXEMPLU GAMA SRL", tip_contrib="PJ",
         localitate="Mun. Cluj-Napoca", strada="Str. Exemplu", nr="3", telefon="0",
         tva="NU", data_radiere="15.06.2025", stare="DIZOLVARE FARA LICHIDARE(FUZIUNE)",
         judet="CLUJ", imp100="DA ", imp120="NU ", cont480="NU ", imp602="NU "),
]
ROWS_B = [
    line(cod_fiscal="90000049", denumire="EXEMPLU DELTA SRL", tip_contrib="PJ",
         localitate="Mun. Braşov", tva="DA", stare="INREGISTRAT", judet="BRAŞOV",
         imp100="DA ", imp120="NU ", cont480="DA ", imp602="NU "),
    # In both parts: the first part loaded keeps it.
    line(cod_fiscal="90000014", denumire="EXEMPLU ALFA DUPLICAT", tva="NU"),
]


def write(path, rows, header=None):
    header = header or "^".join(COLUMNS)
    path.write_bytes(b"\xef\xbb\xbf" + ("\r\n".join([header, *rows]) + "\r\n").encode("utf-8"))
    return path


@pytest.fixture
def files(tmp_path):
    folder = tmp_path / "raw"
    folder.mkdir()
    out = []
    for part, rows in (("date_identificare_platitori_2026_a", ROWS),
                       ("date_identificare_platitori_2026_b", ROWS_B)):
        path = write(folder / f"{part}.txt", rows)
        out.append(store.Source(part=part, path=path, name=path.name,
                                url=f"https://data.gov.ro/dataset/x/resource/{part}/download/{path.name}",
                                package="date_de_identificare_platitori_actualizate_iunie_2026",
                                year=2026, published="2026-07-23T06:46:00", licence="CC-BY-4.0"))
    return out


@pytest.fixture
def db(files, tmp_path):
    path = tmp_path / "taxpayers.db"
    store.build(files, path)
    return path


# ── Reading and loading ──

def test_a_file_is_read_by_column_name(files):
    rows = {r["cui"]: r for r in store.read_file(files[0].path)}
    assert set(rows) == {"90000014", "90000022", "90000030"}       # the personal code left out
    alfa = rows["90000014"]
    assert (alfa["vat"], alfa["employer"], alfa["micro_tax"], alfa["profit_tax"]) == (1, 1, 1, 0)
    assert alfa["registered"] == "2015-03-01" and alfa["status_date"] == "2015-03-01"
    pfa = rows["90000022"]
    assert pfa["contributor_type"] == "PF" and pfa["street"] == "Str. Câmpului"
    assert pfa["phone"] == "0722100200 / 0268 100 300"
    assert rows["90000030"]["struck_off"] == "2025-06-15"


def test_an_older_layout_with_dates_and_other_names_is_read(tmp_path):
    header = "cod_fiscal,nume,loc,str,nr,tel,cp,judet,tva,stare,imp_120,imp_480,imp_602"
    path = tmp_path / "old.csv"
    path.write_bytes((header + "\r\n90000014,EXEMPLU ALFA SRL,Mun. Braşov,Str. Exemplu,1,"
                      "0268100200,500001,BRAŞOV,01.02.2019,INREGISTRAT,01.01.2018,,01.05.2016\r\n")
                     .encode("cp1250"))
    (row,) = store.read_file(path)
    assert row["vat"] == 1 and row["vat_since"] == "2019-02-01"
    assert row["micro_tax"] == 1 and row["employer"] == 1 and row["locality"] == "Mun. Braşov"


def test_a_file_without_a_tax_code_column_is_refused(tmp_path):
    path = write(tmp_path / "x.txt", ["1^2"], header="A^B")
    with pytest.raises(store.FormatChanged):
        list(store.read_file(path))


def test_build_keeps_the_first_part_of_a_duplicate_and_counts(db):
    meta = store.status(db)
    assert meta["year"] == "2026" and meta["rows"] == "4" and meta["duplicates"] == "1"
    assert (meta["vat"], meta["employers"], meta["micro"], meta["with_phone"]) == ("2", "2", "1", "3")
    assert [f["part"] for f in meta["files"]] == ["date_identificare_platitori_2026_a",
                                                 "date_identificare_platitori_2026_b"]
    con = store.connect(db)
    try:
        assert store.taxpayer(con, "90000014")["name"] == "EXEMPLU ALFA SRL"
        assert store.taxpayer(con, "1900101123456") is None
    finally:
        con.close()


def test_an_address_is_one_line():
    row = {"street": "Str. Exemplu", "number": "1", "block": "A1", "apartment": "3",
           "sector": "", "locality": "Mun. Braşov", "county": "BRAŞOV", "postal_code": "500001"}
    assert store.address(row) == "Str. Exemplu nr. 1, bl. A1, ap. 3, Mun. Braşov, BRAŞOV, 500001"


# ── What data.gov.ro has ──

def _res(url, created, size=430_000_000):
    return {"url": url, "created": created, "size": size}


def test_the_newest_year_and_the_newest_file_per_part_win():
    base = "https://data.gov.ro/dataset/x/resource/{}/download/{}"
    packages = [
        {"name": "date_de_identificare_platitori_actualizate_iunie_2026", "license_id": "cc-by-4.0",
         "resources": [
             _res(base.format(1, "date_identificare_platitori_2026_a.csv"), "2026-06-12T10:36:00"),
             _res(base.format(2, "date_identificare_platitori_2026_a.txt"), "2026-06-12T10:37:00"),
             _res(base.format(3, "date_identificare_platitori_2026_b.txt"), "2026-06-12T10:38:00"),
             _res(base.format(4, "date_identificare_platitori_2026_a_actualizat_iulie.txt"),
                  "2026-07-23T06:46:00"),
             _res(base.format(5, "fisier_test.txt"), "2026-07-24T00:00:00", size=9),
             _res("https://elsewhere.example/date_identificare_platitori_2026_b_x.txt",
                  "2026-08-01T00:00:00")]},
        {"name": "date_identificare_062025", "license_id": "cc-by-4.0",
         "resources": [_res(base.format(6, "dateidentificare2025.csv"), "2025-07-25T09:50:00")]},
        {"name": "date_de_identificare_platitori_actualizate_iunie_2027", "license_id": "cc-by-4.0",
         "resources": [_res(base.format(7, "date_identificare_platitori_2027_csv.zip"),
                            "2027-06-01T00:00:00")]},          # only an archive: not usable
        {"name": "situatii_financiare_2025", "resources": []},
    ]
    found = source.resources(packages)
    assert [(r.year, r.part, r.name) for r in found] == [
        (2026, "date_identificare_platitori_2026_a",
         "date_identificare_platitori_2026_a_actualizat_iulie.txt"),
        (2026, "date_identificare_platitori_2026_b", "date_identificare_platitori_2026_b.txt")]
    assert found[0].licence == "CC-BY-4.0"


@pytest.mark.parametrize("created", ["/tmp/elsewhere", "../../../x", "2026-06-11/../../x"])
def test_a_publication_date_never_leaves_the_downloads_folder(tmp_path, created):
    root = tmp_path / "data" / "mf_taxpayers"
    url = "https://data.gov.ro/dataset/x/resource/1/download/date_identificare_platitori_2026_a.txt"
    [res] = source.resources([{"name": "date_de_identificare_platitori_2026",
                               "resources": [_res(url, created)]}])
    assert res.published == ""
    assert source.local_file(res, root).resolve().is_relative_to(root.resolve())


def test_a_file_named_with_odd_characters_is_skipped():
    url = "https://data.gov.ro/dataset/x/resource/1/download/date_2026_a%2F..txt"
    assert source.resources([{"name": "date_de_identificare_platitori_2026",
                              "resources": [_res(url, "2026-06-11T10:00:00")]}]) == []


@pytest.mark.asyncio
async def test_a_redirect_off_data_gov_ro_is_refused(tmp_path):
    url = "https://data.gov.ro/dataset/x/resource/1/download/date_identificare_platitori_2026_a.txt"
    [res] = source.resources([{"name": "date_de_identificare_platitori_2026",
                               "resources": [_res(url, "2026-06-11T10:00:00", size=None)]}])
    hosts = []

    def handler(request):
        hosts.append(f"{request.url.scheme}://{request.url.host}")
        if request.url.host == "data.gov.ro":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/internal.txt"})
        return httpx.Response(200, content=b"COD_FISCAL^DENUMIRE\n" * 100_000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 follow_redirects=True) as client:
        with pytest.raises(RuntimeError, match="only https://data.gov.ro"):
            await source.fetch(client, res, tmp_path / "dl")
    assert hosts == ["https://data.gov.ro"]


def test_update_says_new_or_revised():
    res = [source.Resource(2026, "p_a", "https://data.gov.ro/a", "a.txt", "pkg",
                           "2026-07-23T06:46:00", "CC-BY-4.0")]
    assert caps.update(res, None) == {"year": 2026, "why": "new"}
    loaded = {"year": "2026", "files": [{"part": "p_a", "url": "https://data.gov.ro/a",
                                         "published": "2026-07-23T06:46:00"}]}
    assert caps.update(res, loaded) is None
    loaded["files"][0]["published"] = "2026-06-12T10:37:00"
    assert caps.update(res, loaded) == {"year": 2026, "why": "revised"}
    assert caps.update(res, {"year": "2025", "files": []}) == {"year": 2026, "why": "new"}


@pytest.mark.asyncio
async def test_sync_downloads_once_and_loads_then_has_nothing_to_do(files, tmp_path,
                                                                     monkeypatch):
    data = {f.name: f.path.read_bytes() for f in files}
    listing = {"result": {"results": [{
        "name": "date_de_identificare_platitori_actualizate_iunie_2026", "license_id": "cc-by-4.0",
        "resources": [{"url": f.url, "created": f.published, "size": len(data[f.name])}
                      for f in files]}]}}
    fetched = []

    def handler(request):
        if request.url.path.endswith("package_search"):
            return httpx.Response(200, json=listing)
        name = request.url.path.rsplit("/", 1)[-1]
        fetched.append(name)
        return httpx.Response(200, content=data[name])

    monkey = {"path": str(tmp_path / "t.db"), "downloads": str(tmp_path / "dl")}
    monkeypatch.setattr(source, "MIN_BYTES", 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = api.Context(http=http, settings=monkey)
        first = await caps.CAPABILITIES["taxpayers_sync"](api.Input(), ctx)
        again = await caps.CAPABILITIES["taxpayers_sync"](api.Input(), ctx)
    offline = await caps.CAPABILITIES["taxpayers_sync"](
        api.Input(extra={"force": True}), api.Context(settings=monkey))
    assert sorted(fetched) == sorted(data) and len(first.values["downloaded"]) == 2
    assert first.values["taxpayers"]["rows"] == "4"
    assert again.values["unchanged"] and len(fetched) == 2          # nothing downloaded again
    assert offline.values["offline"] and offline.values["taxpayers"]["rows"] == "4"
    assert (tmp_path / "dl" / "2026").is_dir()


# ── Lookups ──

def test_a_lookup_gives_facts_dated_the_day_the_file_was_published(db):
    con = store.connect(db)
    try:
        facts, contacts = caps.facts_for(store.taxpayer(con, "90000014"))
        pfa, pfa_phones = caps.facts_for(store.taxpayer(con, "90000022"))
    finally:
        con.close()
    got = {(f.code, f.value_num, f.value_text) for f in facts}
    assert {("VAT_REGISTERED", 1.0, ""), ("HAS_EMPLOYEES_TAX", 1.0, ""),
            ("MICRO_TAX_REGIME", 1.0, ""), ("PHONE_NUMBER", None, "+40268100200")} <= got
    # The file's STARE is the record's last event, not a state: ANAF alone
    # writes REGISTRATION_STATUS, so a status filter means one thing.
    assert not any(f.code == "REGISTRATION_STATUS" for f in facts)
    assert {f.observed_at for f in facts} == {"2026-07-23 00:00:00"}
    assert all(f.source_url.startswith("https://data.gov.ro/") for f in facts)
    assert [c.display for c in contacts] == ["+40 268 100 200"]
    # A sole trader's address and phones are kept.
    assert any(f.code == "ADDRESS" and "Str. Câmpului" in f.value_text for f in pfa)
    assert [c.value for c in pfa_phones] == ["+40722100200", "+40268100300"]
    assert ("HAS_EMPLOYEES_TAX", 0.0, "") in {(f.code, f.value_num, f.value_text) for f in pfa}


@pytest_asyncio.fixture
async def state(db, monkeypatch):
    monkeypatch.setattr(store, "TAXPAYERS_PATH", db)
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        yield sm


@pytest.mark.asyncio
async def test_companies_on_file_get_readings_once_and_a_phone(state):
    a = await state.add_company(Company(name="EXEMPLU ALFA SRL", cui="90000014",
                                        source="registry:onrc"))
    pfa = await state.add_company(Company(name="POPESCU EXEMPLU PFA", cui="90000022",
                                          source="registry:onrc"))
    for code in ("HAS_EMPLOYEES_TAX", "VAT_REGISTERED", "PHONE_NUMBER", "ADDRESS"):
        await state.set_signal_status(code, "confirmed")
    first = await runs.lookup_by_id(state)
    assert first["companies"] == 2
    assert "MICRO_TAX_REGIME" in first["unconfirmed"]["mf_taxpayers"]
    assert (await state.get_company(a)).phone == "+40 268 100 200"
    assert (await state.get_company(pfa)).phone == "+40 722 100 200"
    with sqlite3.connect(state.db_path) as con:
        rows = con.execute("SELECT signal_code, module, observed_at FROM observations "
                           "WHERE company_id = ? ORDER BY signal_code", (pfa,)).fetchall()
    assert ("ADDRESS", "mf_taxpayers", "2026-07-23 00:00:00") in rows
    assert sum(1 for r in rows if r[0] == "PHONE_NUMBER") == 2
    again = await runs.lookup_by_id(state)
    assert again["observations"] == 0                                # same file: nothing new


@pytest.mark.asyncio
async def test_anaf_s_newer_reading_stays_current(state):
    a = await state.add_company(Company(name="EXEMPLU ALFA SRL", cui="90000014",
                                        source="registry:onrc"))
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    await state.add_observations([{"company_id": a, "signal_code": "VAT_REGISTERED",
                                   "value_num": 0.0, "module": "anaf",
                                   "capability": "company_check", "collector": "anaf"}])
    await runs.lookup_by_id(state)                     # the file (July) says yes
    with sqlite3.connect(state.db_path) as con:
        current = con.execute(
            "SELECT value_num, module FROM observations WHERE company_id = ? AND "
            "signal_code = 'VAT_REGISTERED' ORDER BY observed_at DESC, rowid DESC LIMIT 1",
            (a,)).fetchone()
    assert current == (0.0, "anaf")


@pytest.mark.asyncio
async def test_nothing_loaded_is_skipped_quietly(state, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "TAXPAYERS_PATH", tmp_path / "none.db")
    await state.add_company(Company(name="EXEMPLU ALFA SRL", cui="90000014"))
    await state.set_signal_status("VAT_REGISTERED", "confirmed")
    found = await runs.lookup_by_id(state)
    assert any("harvey taxpayers load" in s for s in found["skipped"])


def test_the_manifest():
    m = switches.by_id()["mf_taxpayers"]
    assert m.kind == "source" and m.official and m.default_enabled
    assert {s["code"] for s in m.signals} == {"HAS_EMPLOYEES_TAX", "MICRO_TAX_REGIME"}
    lookup = next(c for c in m.capabilities if c["id"] == "taxpayer_lookup")
    assert lookup["inputs"] == [["id:cui"]]
    assert "PFA" in m.terms_note and "CC-BY" in m.attribution
    assert m.settings["auto_update"]["default"] is False


def test_cli_status_says_nothing_is_loaded(tmp_path, monkeypatch, capsys):
    import sys

    import harvey.state
    from harvey.cli import main

    monkeypatch.setattr(harvey.state, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(source, "DOWNLOADS", tmp_path / "mf")
    monkeypatch.setattr(sys, "argv", ["harvey", "taxpayers", "status"])
    main()
    assert "No taxpayer file loaded" in capsys.readouterr().out


# ── Filtering the registry before anything is pulled ──

from tests.test_mf_financials import reg  # noqa: E402,F401  (the registry fixture)


def test_employer_and_vat_filter_the_registry(reg, tmp_path):
    from harvey import registry

    path = write(tmp_path / "t.txt", [
        line(cod_fiscal="90000017", denumire="ZÂMBET EXEMPLU SRL", tva="DA", cont480="DA ",
             imp602="DA ", imp120="DA "),
        line(cod_fiscal="90000025", denumire="DINTE TEST SRL", tva="NU", cont480="NU ",
             imp602="NU ", imp120="DA ")])
    db = tmp_path / "taxpayers.db"
    store.build([store.Source(part="p", path=path, year=2026)], db)

    def names(**flags):
        found = registry.search(reg, "dentist BV", tax=store.Filter(**flags), tax_path=db)
        return sorted(r["name"] for r in found["rows"])

    assert names(employer=True) == ["ZÂMBET EXEMPLU SRL"]
    assert names(employer=False) == ["DINTE TEST SRL"]        # on file and explicitly not
    assert names(vat=True, micro=True) == ["ZÂMBET EXEMPLU SRL"]
    assert names(micro=True) == ["DINTE TEST SRL", "ZÂMBET EXEMPLU SRL"]
    with pytest.raises(FileNotFoundError, match="harvey taxpayers load"):
        registry.search(reg, "dentist BV", tax=store.Filter(vat=True),
                        tax_path=tmp_path / "none.db")
