"""The ONRC trade registry: its files, the load, and selecting firms from
it (filtering, identity, what is and isn't stored when work starts on them).

The registry files are national and huge, so these tests build tiny ones in
the same shape: '^'-separated, UTF-8 with a BOM, a header row. Every
company, person and number in them is invented.
"""

import sqlite3

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import onrc
from harvey.models import Company
from harvey import launch, registry
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim

DATASET = onrc.Dataset("firme-01-10-2026", "2026-10-01", {})

CAEN = [
    ("COD_INMATRICULARE", "COD_CAEN_AUTORIZAT", "VER_CAEN_AUTORIZAT"),
    ("J08/100/2010", "8623", "2"),      # A: dental, Rev.2, Brașov, operating
    ("J08/100/2010", "8622", "2"),
    ("J2025000200088", "8623", "3"),    # B: dental, Rev.3, Brașov, operating
    ("J08/300/2001", "8623", "2"),      # C: Brașov, struck off
    ("J12/400/2012", "8623", "2"),      # D: Cluj, operating
    ("J08/500/1999", "8623", "1"),      # E: an old-nomenclature code
    ("J08/600/2015", "4773", "2"),      # F: a pharmacy
    ("J08/700/2016", "8623", "2"),      # G: operating but dissolved
    ("F08/800/2018", "8623", "2"),      # H: sole trader, CNP in the CUI field
    ("J08/900/2011", "8690", "2"),      # P: only Rev.2's catch-all health code
    ("J08/910/2021", "8695", "3"),      # Q: physiotherapy, Rev.3
    ("J08/920/2012", "8690", "2"),      # R: the catch-all and physiotherapy
    ("J08/920/2012", "8695", "3"),
]
STATUS = [
    ("COD_INMATRICULARE", "COD", ""),
    ("J08/100/2010", "1048", ""),
    ("J2025000200088", "1048", ""),
    ("J08/300/2001", "1084", ""),
    ("J12/400/2012", "1048", ""),
    ("J08/500/1999", "1048", ""),
    ("J08/600/2015", "1048", ""),
    ("J08/700/2016", "1048", ""),
    ("J08/700/2016", "1049", ""),
    ("F08/800/2018", "1048", ""),
    ("J08/900/2011", "1048", ""),
    ("J08/910/2021", "1048", ""),
    ("J08/920/2012", "1048", ""),
]
FIRM_HEADER = ("DENUMIRE", "CUI", "COD_INMATRICULARE", "DATA_INMATRICULARE", "EUID",
               "FORMA_JURIDICA", "ADR_TARA", "ADR_JUDET", "ADR_LOCALITATE",
               "ADR_DEN_STRADA", "ADR_NR_STRADA", "ADR_BLOC", "ADR_SCARA", "ADR_ETAJ",
               "ADR_APARTAMENT", "ADR_COD_POSTAL", "ADR_SECTOR", "ADR_COMPLETARE",
               "WEB", "TARA_FIRMA_MAMA")
CNP = "1900101123456"        # invented, but shaped like a personal numeric code
BIRTH = "07/07/1977"


def firm(name, cui, reg, form, county, locality, street="", number="", flat="",
         postal="", web=""):
    return (name, cui, reg, "01/02/2010", f"ROONRC.{reg}", form, "România", county,
            locality, street, number, "B1", "A", "2", flat, postal, "", "", web, "")


FIRMS = [
    FIRM_HEADER,
    firm("ZÂMBET EXEMPLU SRL", "90000017", "J08/100/2010", "SRL", "Braşov",
         "Municipiul Braşov", "Str. Exemplului", "12", "7", "500123",
         "www.zambet-exemplu.example.ro"),
    firm("DINTE TEST SRL", "RO90000025", "J2025000200088", "SRL", "Brașov",
         "Sat Cristian, Comuna Cristian", "Str. Morii", "3"),
    firm("INCHIS EXEMPLU SRL", "90000033", "J08/300/2001", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("CLUJ EXEMPLU SRL", "90000041", "J12/400/2012", "SRL", "Cluj",
         "Municipiul Cluj-Napoca"),
    firm("VECHI EXEMPLU SRL", "90000058", "J08/500/1999", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("FARMA EXEMPLU SRL", "90000066", "J08/600/2015", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("DIZOLVAT EXEMPLU SRL", "90000074", "J08/700/2016", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("TESTESCU ANA PERSOANĂ FIZICĂ AUTORIZATĂ", CNP, "F08/800/2018", "PFA",
         "Braşov", "Municipiul Braşov", "Str. Casei", "9", "4", "500999"),
    firm("SANATATE GENERALA EXEMPLU SRL", "90000082", "J08/900/2011", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("MISCARE EXEMPLU SRL", "90000090", "J08/910/2021", "SRL", "Braşov",
         "Municipiul Braşov"),
    firm("RECUPERARE EXEMPLU SRL", "90000104", "J08/920/2012", "SRL", "Braşov",
         "Municipiul Braşov"),
]
PEOPLE = [
    ("COD_INMATRICULARE", "PERSOANA_IMPUTERNICITA", "CALITATE", "DATA_NASTERE",
     "LOCALITATE_NASTERE", "JUDET_NASTERE", "TARA_NASTERE", "LOCALITATE", "JUDET", "TARA"),
    ("J08/100/2010", "TESTESCU MARIA", "administrator", BIRTH, "Satul Nasterii",
     "Braşov", "România", "Satul Domiciliului", "Braşov", "România"),
    ("J08/100/2010", "DOI ANDREI-MIHAI", "administrator", BIRTH, "", "", "", "", "", ""),
    ("J08/100/2010", "EXEMPLU INSOLV IPURL", "lichidator", "", "", "", "", "", "", ""),
    ("J2025000200088", "LICHIDATORU ION", "lichidator judiciar", "", "", "", "", "", "", ""),
    ("J2025000200088", "PRACTICIAN EXEMPLU", "administrator judiciar", "", "", "", "", "", "", ""),
    ("J2025000200088", f"FALS {CNP}", "administrator", "", "", "", "", "", "", ""),
    ("J12/400/2012", "CLUJEANU VLAD", "administrator", "", "", "", "", "", "", ""),
]


def write(path, rows):
    lines = ["^".join(r) for r in rows]
    path.write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))
    return path


@pytest.fixture
def files(tmp_path):
    return {
        "caen": write(tmp_path / onrc.FILES["caen"], CAEN),
        # Each row ends in an empty field, as the real file's do.
        "status": write(tmp_path / onrc.FILES["status"], STATUS),
        "firms": write(tmp_path / onrc.FILES["firms"], FIRMS),
        "people": write(tmp_path / onrc.FILES["people"], PEOPLE),
    }


@pytest_asyncio.fixture
async def state(tmp_path, monkeypatch):
    # The registry the selection reads: beside the database, where the
    # state attaches it.
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.db")
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    yield sm


async def run(state, files, *, words="dentist", county="BV", locality="", status="",
              barred=(), create=True, take=True):
    """A Find-contacts selection of the registry (launch.registry_selection),
    with the registry loaded from the files beside the database: the firms
    selected get their records by SQL over it and, with ``take``, take
    their registry entries as their work starts (StateManager.
    take_from_registry). ``barred``: the do-not-contact list's domains.
    Returns the selection's counts, with "taken" and "shared_domain"."""
    slim.registry_beside(state, {k: v for k, v in files.items() if v.exists()}, DATASET)
    filters = {"words": words, "county": county, "locality": locality, "status": status}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(launch, "_barred_domains", lambda: list(barred))
        out = await launch.registry_selection(state, filters, force=True, create=create)
    out["taken"] = out["shared_domain"] = 0
    for cid in (out["ids"] if create and take else []):
        took = await state.take_from_registry(cid, run_id="find")
        out["taken"] += bool(took)
        out["shared_domain"] += bool(took.get("shared_domain"))
    return out


def count(state, words="dentist", **kw) -> int:
    """How many firms of the loaded registry a search selects."""
    con = registry.connect(slim.folder(state) / "registry.db")
    try:
        return registry.count(con, registry.resolve(con, words, **kw))
    finally:
        con.close()


async def obs(state, code):
    """What the registry gives ``code`` for the firms saved (read in place:
    schema 25 stores no register fact)."""
    return await slim.register_values(state, code)


# ── Small parts ──

@pytest.mark.parametrize("given", ["Brașov", "Brasov", "Braşov", "BV", "bv",
                                   "Judetul Brasov", "Județul Brașov", " brasov county "])
def test_county_aliases(given):
    assert onrc.county_name(given) == "Brașov"


def test_county_others():
    assert onrc.county_name("Bucharest") == onrc.county_name("B") == "București"
    assert onrc.county_name("bistrita nasaud") == "Bistrița-Năsăud"
    assert onrc.county_name("CJ") == "Cluj"
    assert onrc.county_name("Atlantis") is None
    assert onrc.county_name("") is None


@pytest.mark.parametrize("given, county", [
    ("Municipiul București", "București"), ("Municipiul Bucuresti", "București"),
    ("Mun. București", "București"), ("mun bucuresti", "București"),
    ("Bistrita", "Bistrița-Năsăud"), ("Bistrița", "Bistrița-Năsăud"),
    ("Bistrita-Nasaud", "Bistrița-Năsăud"),
    ("Caras", "Caraș-Severin"), ("Caraș-Severin", "Caraș-Severin"),
    ("Satu", "Satu Mare"), ("satu mare", "Satu Mare"),
])
def test_county_city_prefix_and_first_word(given, county):
    assert onrc.county_name(given) == county


def test_county_second_word_alone_is_not_enough():
    # "Mare" or "Severin" alone could be a town; only the first word counts.
    assert onrc.county_name("Mare") is None
    assert onrc.county_name("Severin") is None


def test_cui_never_keeps_a_personal_code():
    assert onrc.valid_cui("RO90000025") == "90000025"
    assert onrc.valid_cui(" 90000017 ") == "90000017"
    assert onrc.valid_cui(CNP) == ""
    assert onrc.valid_cui("0") == ""


def test_locality_and_names():
    assert onrc.clean_locality("Municipiul Braşov") == "Brașov"
    assert onrc.clean_locality("Sat Cristian, Comuna Cristian") == "Cristian"
    assert onrc.clean_locality("Loc. Râşnov, Oraş Râşnov") == "Râșnov"
    assert onrc.split_person("ŞTEFĂNESCU-TEST ANA MARIA") == ("Ana Maria", "Ștefănescu-Test")
    assert onrc.split_person("SINGURUL") == ("", "")


def test_both_revisions_and_explicit_codes():
    classes = onrc.parse_classes("dentist")
    assert ("8623", 3) in classes and ("8623", 2) in classes
    assert onrc.parse_classes("", "8623, 862") == [("8623", None), ("0862", None)]


def test_a_website_field_that_is_not_a_url():
    assert onrc._website("http://[exemplu") == ("", "")
    assert onrc._website("0700 000 000") == ("", "")
    assert onrc._website("www.exemplu.example.ro") == (
        "https://exemplu.example.ro", "exemplu.example.ro")


def test_malformed_rows_are_skipped(tmp_path):
    path = write(tmp_path / "x.csv", [("A", "B"), ("1", "2"), ("3", "4", "extra")])
    assert list(onrc.read_rows(path, ("B",))) == [("2",)]


# ── Selecting firms, and what their records hold ──

@pytest.mark.asyncio
async def test_a_selection_filters_and_counts(state, files):
    report = await run(state, files)
    # A B C D G H are authorised (E's code is pre-2008, F is a pharmacy) ...
    assert count(state, all_statuses=True) == 6
    # ... A B D H operate (C is struck off, G dissolved) ...
    assert count(state) == 4
    # ... and A B H are in Brașov.
    assert (report["selected"], report["created"], report["existing"]) == (3, 3, 0)

    a = await state.get_company_by_cui("90000017")
    assert a.name == "ZÂMBET EXEMPLU SRL"
    assert a.source == "registry:onrc"
    assert a.external_id == "onrc:J08/100/2010"
    assert a.domain == "zambet-exemplu.example.ro"
    assert a.website == "https://zambet-exemplu.example.ro"
    assert a.location == "Brașov, Romania"
    # The industry its first current class names, read from the registry
    # (which class the selection asked for isn't kept).
    assert a.industry == "Specialist medical practice"
    # Street and number, no block, staircase, floor or flat.
    assert a.address == "Str. Exemplului 12, 500123"

    b = await state.get_company_by_cui("90000025")
    assert b.location == "Cristian, Brașov, Romania"

    h = await state.get_company_by_external_id("onrc:F08/800/2018")
    # A sole trader keeps its registered office (public registry data); the
    # personal numeric code is never stored as its CUI.
    assert h.cui == "" and h.address == "Str. Casei 9, 500999"
    assert CNP not in str(vars(h))


@pytest.mark.asyncio
async def test_administrators_only_the_people_who_run_it(state, files):
    await run(state, files)
    assert await obs(state, "CONTACT_FOUND") == [
        "Andrei-Mihai Doi — Administrator", "Maria Testescu — Administrator"]
    assert await obs(state, "DECISION_MAKER_TITLE") == await obs(state, "CONTACT_FOUND")
    # The class with its CAEN revision: A and H are on Rev.2, B on Rev.3;
    # A's other authorised class is read too (the registry's whole record).
    assert await obs(state, "ACTIVITY_CODE") == ["8622/2", "8623/2", "8623/2", "8623/3"]


@pytest.mark.asyncio
async def test_no_personal_data_is_stored(state, files):
    await run(state, files)
    with sqlite3.connect(state.db_path) as db:
        dump = "\n".join(db.iterdump())
    # A representative's birth data and home are never read; a sole trader's
    # registered office (Str. Casei) is public registry data and is kept.
    for private in (CNP, BIRTH, "Satul Nasterii", "Satul Domiciliului"):
        assert private not in dump


@pytest.mark.asyncio
async def test_the_same_selection_again_makes_no_second_record(state, files):
    first = await run(state, files)
    again = await run(state, files)
    assert (first["created"], first["existing"], first["taken"]) == (3, 0, 3)
    assert (again["created"], again["existing"], again["taken"]) == (0, 3, 0)
    # The registry's facts are read in place: nothing stores any.
    assert slim.one(state, "SELECT COUNT(*) FROM firm") == 3
    assert slim.one(state, "SELECT COUNT(*) FROM obs") == 0
    assert slim.one(state, "SELECT COUNT(*) FROM firm_source") == 3


@pytest.mark.asyncio
async def test_a_shared_website_is_never_a_merge(state, files):
    # The registry's website field is self-declared: the business already on
    # file under that domain keeps its own identity, address and contacts.
    known = await state.add_company(Company(
        name="Clinica Zâmbet", domain="zambet-exemplu.example.ro",
        source="discover:osm", location="Brașov, Romania"))
    report = await run(state, files)
    assert (report["created"], report["existing"]) == (3, 0)
    assert report["shared_domain"] == 1
    registry_firm = await state.get_company_by_cui("90000017")
    assert registry_firm.id != known
    assert registry_firm.domain == ""
    assert registry_firm.website == "https://zambet-exemplu.example.ro"
    other = await state.get_company_by_domain("zambet-exemplu.example.ro")
    assert other.id == known and other.cui == "" and other.address == ""
    assert other.name == "Clinica Zâmbet"
    assert slim.one(state, "SELECT COUNT(*) FROM firm_ident WHERE firm = ?", int(known)) == 0


@pytest.mark.asyncio
async def test_a_firm_on_file_with_the_cui_is_the_same_legal_company(state, files):
    known = await state.add_company(Company(
        name="Clinica Zâmbet", cui="90000017", source="discover:osm",
        location="Brașov, Romania"))
    report = await run(state, files)
    assert (report["created"], report["existing"]) == (2, 1)
    same = await state.get_company_by_cui("90000017")
    assert same.id == known
    # The registry describes it: its name and address are read from there
    # (Harvey's own name for it would be a display name).
    assert same.name == "ZÂMBET EXEMPLU SRL"
    assert same.address == "Str. Exemplului 12, 500123"
    # Another source brought it, so it takes no registry entry: the website
    # the registry declares is left for its search to read.
    assert report["taken"] == 2 and same.domain == ""


@pytest.mark.asyncio
async def test_locality_filter(state, files):
    report = await run(state, files, locality="Cristian")
    assert report["selected"] == 1
    assert await state.get_company_by_cui("90000025")
    assert await state.get_company_by_cui("90000017") is None


@pytest.mark.asyncio
async def test_every_status(state, files):
    report = await run(state, files, status="all")
    assert report["selected"] == 5        # A B C G H


@pytest.mark.asyncio
async def test_old_nomenclature_never_matches(state, files):
    # 8623 in the pre-2008 nomenclature isn't dental; E carries it.
    slim.registry_beside(state, files, DATASET)
    assert count(state, "8623", all_statuses=True) == 6


@pytest.mark.asyncio
async def test_an_old_installs_proposed_signals_dont_hold_the_read(state, files):
    """An older release's "proposed" rows are read by nothing: the read
    and its administrators go on as with any other install."""
    import sqlite3

    con = sqlite3.connect(state.db_path)
    with con:
        con.execute("UPDATE signal SET status = 'proposed' WHERE code IN "
                    "('ACTIVITY_CODE', 'CONTACT_FOUND', 'DECISION_MAKER_TITLE')")
    con.close()
    report = await run(state, files)
    assert report["created"] == 3 and await obs(state, "CONTACT_FOUND")


@pytest.mark.asyncio
async def test_an_estimate_saves_nothing(state, files):
    report = await run(state, files, create=False)
    assert (report["selected"], report["to_create"]) == (3, 3)
    assert slim.one(state, "SELECT COUNT(*) FROM firm") == 0
    assert slim.one(state, "SELECT COUNT(*) FROM runs") == 0


@pytest.mark.asyncio
async def test_do_not_contact_is_skipped(state, files):
    report = await run(state, files, barred=["example.ro"])
    assert report["excluded"] == 1 and report["created"] == 2


@pytest.mark.asyncio
async def test_unknown_county_refused(state, files):
    with pytest.raises(ValueError):
        await run(state, files, county="Atlantis")


# ── Finding and fetching the dataset ──

def _package(name, files=onrc.FILES.values(), host="data.gov.ro"):
    return {"name": name, "resources": [
        {"name": f, "url": f"https://{host}/dl/{name}/{f.lower()}", "size": 10}
        for f in files]}


@pytest.mark.asyncio
async def test_latest_complete_dataset_wins():
    packages = [
        _package("firme-02-09-2026"),
        _package("firme-19-10-2026", files=["OD_FIRME.CSV"]),   # incomplete
        _package("firme-08-07-2026"),
        _package("firme-20-10-2026", host="elsewhere.example"),  # not data.gov.ro
        {"name": "alt-set", "resources": []},
    ]

    def handler(request):
        assert request.url.path.endswith("/package_search")
        return httpx.Response(200, json={"success": True,
                                         "result": {"results": packages}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ds = await onrc.latest_dataset(client)
    assert (ds.name, ds.date) == ("firme-02-09-2026", "2026-09-02")
    assert set(ds.files) == set(onrc.FILES)
    assert ds.page == "https://data.gov.ro/dataset/firme-02-09-2026"


@pytest.mark.asyncio
async def test_fetch_streams_once_and_checks_size(tmp_path):
    calls = []
    body = {"good": b"x" * 10, "short": b"x" * 3}

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=body[request.url.path.strip("/")])

    ds = onrc.Dataset("firme-01-10-2026", "2026-10-01",
                      {"caen": ("https://data.gov.ro/good", 10),
                       "status": ("https://data.gov.ro/short", 10)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await onrc.fetch_files(client, ds, tmp_path, roles=["caen"])
        assert got["caen"].read_bytes() == body["good"]
        assert got["caen"].parent.name == "2026-10-01"
        await onrc.fetch_files(client, ds, tmp_path, roles=["caen"])
        assert calls == ["/good"]                      # cached the second time
        with pytest.raises(RuntimeError):
            await onrc.fetch_files(client, ds, tmp_path, roles=["status"])
    folder = tmp_path / "2026-10-01"
    assert sorted(p.name for p in folder.iterdir()) == [onrc.FILES["caen"]]


# ── Which statuses count as closed ──

def test_closed_statuses_come_from_the_nomenclature():
    names = onrc.status_names()
    assert names["1048"] == "funcțiune" and len(names) > 150
    closed = onrc.CLOSED_STATUSES
    # Insolvency law 85/2014, a dissolution request at court, office struck
    # off, activity suspended: none of them a code the old list knew.
    assert {"1139", "2091", "2071", "1151"} <= closed
    # Everything the hand-kept list had is still there.
    assert {"1049", "1052", "1070", "1074", "1084", "1107", "1109", "1113",
            "1117"} <= closed
    # Operating, an expired office document, a renamed company's old name,
    # a parent company's dissolution, a ban on starting a dissolution.
    assert not {"1048", "2069", "1085", "1101", "1115", "1140"} & closed


def test_closed_statuses_are_read_by_meaning():
    names = {"1": "funcțiune", "2": "Dizolvare judiciară", "3": "sediu expirat",
             "4": "este sub incidența Legii nr. 85/2014", "5": "radiată",
             "6": "suspendare/interzicere activitate", "7": "firma mamă în dizolvare"}
    assert onrc.closed_statuses(names) == {"2", "4", "5", "6"}


def test_any_closing_status_outranks_operating(tmp_path):
    rows = [("COD_INMATRICULARE", "COD", "")]
    for reg, extra in (("J08/1/2020", "1139"), ("J08/2/2020", "2091"),
                       ("J08/3/2020", "2071"), ("J08/4/2020", "1151"),
                       ("J08/5/2020", "2069"), ("J08/6/2020", "")):
        rows.append((reg, "1048", ""))
        if extra:
            rows.append((reg, extra, ""))
    rows.append(("J08/7/2020", "2069", ""))          # no 1048 at all
    regs = sorted({r[0] for r in rows[1:]})
    files = {"status": write(tmp_path / "status.csv", rows),
             "caen": write(tmp_path / "caen.csv", [CAEN[0]] + [(r, "8623", "3") for r in regs]),
             "firms": write(tmp_path / "firms.csv", [FIRM_HEADER] + [
                 firm(f"EXEMPLU {n} SRL", str(90001000 + n), r, "SRL", "Braşov",
                      "Municipiul Braşov") for n, r in enumerate(regs, 1)])}
    path = tmp_path / "registry.db"
    registry.build(files, DATASET, path)
    con = registry.connect(path)
    try:
        operating = {r for (r,) in con.execute("SELECT reg FROM firms WHERE operating")}
    finally:
        con.close()
    assert operating == {"J08/5/2020", "J08/6/2020"}


# ── Labels, revisions and Rev.2's catch-all ──

def test_class_labels_name_each_code_once():
    classes = [("8623", 3), ("8623", 2), ("8695", 3), ("8690", 2), ("4773", None)]
    assert onrc.class_labels(classes) == [
        "8623 Rev.2/3", "8695 Rev.3", "8690 Rev.2", "4773 Rev.2/3"]


@pytest.mark.asyncio
async def test_a_catch_all_only_firm_is_selected_and_named_by_it(state, files):
    report = await run(state, files, words="kinetoterapeut")
    assert report["selected"] == 3                  # P Q R
    p = await state.get_company_by_cui("90000082")
    assert p.industry == "Other human health activities (labs, imaging, physio, psychologists)"
    r = await state.get_company_by_cui("90000104")
    assert r.industry == "Physiotherapy"            # its precise code wins
    # Each firm's classes are read from the registry as filed.
    assert await obs(state, "ACTIVITY_CODE") == ["8690/2", "8690/2", "8695/3", "8695/3"]


@pytest.mark.asyncio
async def test_a_catch_all_asked_for_by_code_selects_only_its_firms(state, files):
    report = await run(state, files, words="8690")
    assert report["selected"] == 2                  # P R


def test_activity_value_reads_back_with_its_revision():
    from harvey.caen import describe
    assert onrc.activity_value("8623", 3) == "8623/3"
    assert describe(onrc.activity_value("8690", 2)).startswith("Other human health")


# ── What stays on disk ──

def _cached(folder, names):
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"x")


def test_prune_deletes_birth_data_and_older_datasets(tmp_path):
    every = list(onrc.FILES.values())
    _cached(tmp_path / "2026-10-01", every)
    _cached(tmp_path / "2026-09-02", every + [onrc.FILES["firms"] + ".part"])
    _cached(tmp_path / "2026-08-01", every + ["notes.txt"])    # not ours: kept
    _cached(tmp_path / "manual", [onrc.FILES["firms"]])        # not a dataset folder

    deleted = onrc.prune_downloads(tmp_path, DATASET)

    assert tmp_path / "2026-10-01" / onrc.FILES["people"] in deleted
    assert sorted(p.name for p in (tmp_path / "2026-10-01").iterdir()) == sorted(
        f for f in every if f != onrc.FILES["people"])
    assert not (tmp_path / "2026-09-02").exists()
    assert [p.name for p in (tmp_path / "2026-08-01").iterdir()] == ["notes.txt"]
    assert (tmp_path / "manual" / onrc.FILES["firms"]).exists()
    assert len(deleted) == 1 + 5 + 4


def test_prune_can_keep_the_download(tmp_path):
    _cached(tmp_path / "2026-10-01", list(onrc.FILES.values()))
    assert onrc.prune_downloads(tmp_path, DATASET, keep_people=True) == []
    assert (tmp_path / "2026-10-01" / onrc.FILES["people"]).exists()
    assert onrc.prune_downloads(tmp_path / "missing", DATASET) == []


def test_each_activity_is_resolved_on_its_own():
    together = onrc.parse_classes(["dentist", "clinica"])
    codes = {code for code, _ in together}
    assert {"8623", "8622"} <= codes
    assert {c for c, _ in onrc.parse_classes("dentist, clinica")} == codes


def test_county_list_and_whole_country():
    assert onrc.counties("") is None
    assert onrc.counties("BV, cj") == ["Brașov", "Cluj"]
    with pytest.raises(ValueError):
        onrc.counties("BV, Atlantis")


@pytest.mark.asyncio
async def test_whole_country_and_several_counties(state, files):
    report = await run(state, files, county="")
    assert report["selected"] == 4    # A B H in Brașov, D in Cluj
    assert await state.get_company_by_cui("90000041")


@pytest.mark.asyncio
async def test_several_counties(state, files):
    report = await run(state, files, county="BV, CJ")
    assert report["selected"] == 4


@pytest.mark.asyncio
async def test_several_localities(state, files):
    report = await run(state, files, locality="Cristian, Cluj-Napoca", county="")
    assert report["selected"] == 2    # B in Cristian, D in Cluj-Napoca


# ── Activities: every class of a kept company, searchable ──

async def activities(state, cid):
    """The classes the registry lists for a company, (code, revision)."""
    company = await state.read("company", cid)
    return sorted((a["code"], a["version"]) for a in company["activities"])


@pytest.mark.asyncio
async def test_every_authorised_class_of_a_kept_company_is_read(state, files):
    # Asked for dentists; A is also authorised for specialist practice.
    await run(state, files)
    a = await state.get_company_by_cui("90000017")
    assert await activities(state, a.id) == [("8622", 2), ("8623", 2)]
    # Read from the registry, never copied.
    assert slim.one(state, "SELECT COUNT(*) FROM obs") == 0
    total, rows = await state.search_companies("8622")
    assert [r["cui"] for r in rows] == ["90000017"] and total == 1


@pytest.mark.asyncio
async def test_activities_are_read_and_searchable(state, files):
    await run(state, files)
    b = await state.get_company_by_cui("90000025")
    assert await activities(state, b.id) == [("8623", 3)]


@pytest.mark.asyncio
async def test_a_class_the_registry_drops_is_dropped(state, files, tmp_path):
    await run(state, files)
    files["caen"] = write(tmp_path / "less.csv", [r for r in CAEN if r[1] != "8622"])
    await run(state, files)
    a = await state.get_company_by_cui("90000017")
    assert await activities(state, a.id) == [("8623", 2)]


@pytest.mark.asyncio
async def test_no_activity_selects_everything_operating_in_the_area(state, files):
    report = await run(state, files, words="")
    # Brașov registrations: A B C E F G H P Q R; C struck off, G dissolved.
    assert (report["selected"], report["created"]) == (8, 8)
    pharmacy = await state.get_company_by_cui("90000066")
    assert pharmacy.industry == "Pharmacies"
    assert await activities(state, pharmacy.id) == [("4773", 2)]
    # E's pre-2008 code is kept as filed, though it never matches a filter
    # (and this one isn't in the nomenclature, so it names no industry).
    old = await state.get_company_by_cui("90000058")
    assert await activities(state, old.id) == [("8623", 1)]
    assert old.industry == ""


@pytest.mark.asyncio
async def test_a_firm_taken_is_the_registry_modules_and_says_so(state, files):
    report = await run(state, files)
    assert report["taken"] == 3
    assert slim.q(state, "SELECT DISTINCT s.name FROM firm_source fs "
                         "JOIN source s ON s.id = fs.source") == [("registry:onrc",)]


@pytest.mark.asyncio
async def test_no_activity_and_no_area_is_refused(state, files):
    with pytest.raises(ValueError, match="needs an activity, an area or a filter"):
        await run(state, files, words="", county="")


@pytest.mark.asyncio
async def test_no_activity_in_one_locality(state, files):
    report = await run(state, files, words="", county="", locality="Cristian")
    assert report["selected"] == 1 and await state.get_company_by_cui("90000025")


@pytest.mark.asyncio
async def test_a_registry_loaded_without_its_people_file_selects_the_same(state, files):
    files["people"].unlink()        # proves the file isn't needed
    report = await run(state, files)
    assert report["created"] == 3
    assert await obs(state, "CONTACT_FOUND") == []

