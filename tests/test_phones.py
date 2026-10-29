"""Phone numbers read off a business's own pages, stored and exported.

Every number here is fictional: 0268 000 1xx (a Brașov landline range)
and 0700 000 1xx / 0740 000 1xx mobiles, US 555-01xx.
"""

import csv
import io
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.collectors.phones import (
    MAX_PHONES, phone_hit, phone_region, phones_from_html,
)
from harvey.collectors.profile import ProfileCollector
from harvey.export import COMPANY_CSV_COLUMNS, collect_companies, export_companies_csv
from harvey.models.company import Company
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def numbers(html, region="RO"):
    return [h.number for h in phones_from_html(html, region)]


# ── extraction ──

@pytest.mark.parametrize("written,expected", [
    ("0268 000 101", "+40268000101"),
    ("0268-000-101", "+40268000101"),
    ("0740.000.101", "+40740000101"),
    ("0740 000 101", "+40740000101"),
    ("+40 (0) 268 000 101", "+40268000101"),
    ("+40 740 000 101", "+40740000101"),
    ("004 0740 000 101", "+40740000101"),
    ("0040 268 000 101", "+40268000101"),
])
def test_romanian_formats_in_text(written, expected):
    assert numbers(f"<p>Programări: {written}</p>") == [expected]


@pytest.mark.parametrize("href,expected", [
    ("tel:+40268000101", "+40268000101"),
    ("tel:0268000101", "+40268000101"),
    ("tel:+40%20740%20000%20101", "+40740000101"),
])
def test_tel_links(href, expected):
    hits = phones_from_html(f'<a href="{href}">Sună-ne</a>', "RO")
    assert [(h.number, h.source) for h in hits] == [(expected, "tel_link")]


def test_hit_carries_display_and_type():
    land, mobile = phones_from_html("Tel 0268 000 101, mobil 0740 000 101", "RO")
    assert (land.display, land.type, land.source) == ("+40 268 000 101", "landline", "page_text")
    assert (mobile.display, mobile.type) == ("+40 740 000 101", "mobile")


def test_tel_links_come_first_and_dedupe():
    html = ("<p>Sunați la 0740 000 102 sau 0268 000 101</p>"
            '<footer><a href="tel:0268000101">0268 000 101</a></footer>')
    hits = phones_from_html(html, "RO")
    assert [(h.number, h.source) for h in hits] == [
        ("+40268000101", "tel_link"), ("+40740000102", "page_text")]


def test_count_is_capped():
    html = "<br>".join(f"0268 000 1{i:02d}" for i in range(10))
    assert len(numbers(html)) == MAX_PHONES


def test_numbers_in_neighbouring_tags_stay_apart():
    html = "<ul><li>0268 000 101</li><li>0740 000 102</li></ul>"
    assert numbers(html) == ["+40268000101", "+40740000102"]


def test_a_number_split_by_inline_tags_is_whole():
    assert numbers("<p>Tel: <b>0268</b> <span>000 101</span></p>") == ["+40268000101"]


@pytest.mark.parametrize("label", ["Fax: ", "Fax ", "fax. ", "F: ", "FAX - "])
def test_fax_is_left_out(label):
    assert numbers(f"Tel: 0268 000 101 &nbsp; {label}0268 000 102") == ["+40268000101"]


@pytest.mark.parametrize("label", ["Tel/Fax: ", "Tel./Fax: ", "Telefon/fax ", "Tel/Fax\n"])
def test_tel_fax_line_is_a_phone(label):
    assert numbers(f"<p>{label}0268 000 103</p>") == ["+40268000103"]


@pytest.mark.parametrize("html", [
    "<p>Telefax: 0268 000 104</p>",
    "<table><tr><td>Telefax</td><td>0268 000 104</td></tr></table>",
    "<table><tr><td>Fax</td><td>0268 000 104</td></tr></table>",
    "<p>0268 000 104 (fax)</p>",
    "<p>0268 000 104 - fax</p>",
    "<p>Fax (secretariat): 0268 000 104</p>",
    '<p><a href="tel:0268000104">Fax</a></p>',
    '<p>Fax: <a href="tel:0268000104">0268 000 104</a></p>',
])
def test_more_fax_labels(html):
    assert numbers(html) == []


def test_every_number_in_a_fax_list_is_fax():
    html = "<p>Fax: 0268 000 104 / 0268 000 105</p><p>Tel: 0268 000 106</p>"
    assert numbers(html) == ["+40268000106"]


def test_a_fax_label_after_a_number_marks_only_the_next_one():
    assert numbers("<p>Tel 0268 000 101 Fax 0268 000 102</p>") == ["+40268000101"]
    assert numbers("<p>Fax: 0268 000 102, tel: 0740 000 103</p>") == ["+40740000103"]


def test_a_tel_fax_link_is_a_phone():
    assert numbers('<p><a href="tel:0268000103">Tel/Fax</a></p>') == ["+40268000103"]


@pytest.mark.parametrize("text", [
    "IBAN: RO49AAAA1B31007593840000",
    "IBAN RO49 AAAA 1B31 0075 9384 0000",
    "Nr. Reg. Com. J08/1234/2010",
    "J08/2680/2010",
    "CUI RO12345678",
    "CIF: 26800010",
    "C.U.I. 268000101",
    "Program actualizat 12.03.2024",
    "Consultație 1.250 lei",
    "Ecografie 268 000 101 lei",
    "Cod poștal 500123",
    "office0740000101@example.com",
    "https://example.com/p/0740000101",
    "Comanda 268000101",
])
def test_digits_that_are_not_phones(text):
    assert numbers(f"<p>{text}</p>") == []


@pytest.mark.parametrize("written,expected", [
    ("0268-00-01-01", "+40268000101"),
    ("0268.00.01.01", "+40268000101"),
    ("0740-00-01-02", "+40740000102"),
    ("0740-12-11-20", "+40740121120"),
])
def test_two_digit_groups_are_not_dates(written, expected):
    assert numbers(f"<p>Tel: {written}</p>") == [expected]


@pytest.mark.parametrize("text", [
    "Actualizat 12.03.2024", "Actualizat 1.3.24", "Actualizat 2024-03-12",
    "Autorizație 0268/12.05.2023",
])
def test_dates_are_still_masked(text):
    assert numbers(f"<p>{text}</p>") == []


def test_a_phone_next_to_a_price():
    assert numbers("<p>0268 000 101 250 lei</p>") == ["+40268000101"]
    assert numbers("<p>Tel 0740 000 102 · consult 1.250,00 lei</p>") == ["+40740000102"]


@pytest.mark.parametrize("text", [
    "Comanda nr. 0268000101",
    "Factura 0268 000 101",
    "Order #0740000102",
    "Ref: 0740 000 102",
    "Contract nr. 0268000101",
    "Dosar 0268000101",
])
def test_order_and_reference_numbers_are_not_phones(text):
    assert numbers(f"<p>{text}</p>") == []


@pytest.mark.parametrize("text", [
    "Programări: 0740 000 102",
    "Tel. nr. 0740 000 102",
    "Nr. telefon: 0740 000 102",
    "Comanda 12 · sună 0740 000 102",
])
def test_a_phone_label_beats_a_reference_label(text):
    assert numbers(f"<p>{text}</p>") == ["+40740000102"]


def test_schema_telephone_is_read():
    html = ("""<script type="application/ld+json">{"@graph": [{"@type": "MedicalClinic",
              "address": {"telephone": ["+40 268 000 101", "0740 000 102"]}},
              {"telephone": "0268 000 103", "faxNumber": "0268 000 109"}]}</script>"""
            '<meta itemprop="telephone" content="+40268000104">'
            '<span itemprop="telephone">0268 000 105</span>'
            '<script type="application/ld+json">{not json</script>')
    hits = phones_from_html(html, "RO")
    assert [(h.number, h.source) for h in hits] == [
        ("+40268000101", "schema"), ("+40740000102", "schema"), ("+40268000103", "schema"),
        ("+40268000104", "schema"), ("+40268000105", "schema")]
    assert hits[0].confidence == 1.0


def test_commented_out_numbers_are_not_read():
    html = ('<!-- old footer: <a href="tel:+40268000108">old</a> 0740 000 108 -->'
            '<!-- <script type="application/ld+json">{"telephone": "0268 000 107"}</script> -->'
            '<a href="tel:+40268000101">Sună</a>')
    assert numbers(html) == ["+40268000101"]


def test_script_and_attributes_are_not_read():
    html = ('<script>var id = "0268 000 101";</script>'
            '<img data-id="0740000101" src="/x.png">')
    assert numbers(html) == []


def test_us_page():
    html = ("<p>Call us: (303) 555-0123</p><p>Fax: (303) 555-0199</p>"
            "<p>Suite 200, Denver, CO 80202</p>")
    hits = phones_from_html(html, "US")
    assert [(h.number, h.display, h.type) for h in hits] == [
        ("+13035550123", "+1 303-555-0123", "other")]


def test_without_a_region_only_international_numbers():
    html = "<p>0268 000 101 · +40 268 000 102 · 0040 740 000 103</p>"
    assert numbers(html, None) == ["+40268000102", "+40740000103"]
    assert numbers('<a href="tel:0268000101">x</a>', None) == []
    assert numbers('<a href="tel:+40268000101">x</a>', None) == ["+40268000101"]


# ── which country a business's national numbers are in ──

def _config(prospect_country="", geography=()):
    return SimpleNamespace(
        compliance=SimpleNamespace(precleared=SimpleNamespace(prospect_country=prospect_country)),
        icp=SimpleNamespace(geography=list(geography), geo_coordinates={}),
    )


def test_phone_region():
    assert phone_region("Brașov, Romania") == "RO"
    assert phone_region("Brasov", _config("RO")) == "RO"
    assert phone_region("Brasov", _config(geography=["Brașov, Romania"])) == "RO"
    assert phone_region("Brasov", None, "clinica.ro") == "RO"
    assert phone_region("Denver, CO") == "US"
    assert phone_region("Brasov", None, "clinic.io") is None
    assert phone_region("", None, "") is None


@pytest.mark.parametrize("location", ["Brașov", "Brașov, Județul Brașov", ""])
def test_a_country_domain_beats_the_config(location):
    us = _config(geography=["Denver, CO, United States"])
    assert phone_region(location, us, "clinica.ro") == "RO"
    assert phone_region(location, _config("US"), "clinica.ro") == "RO"


def test_a_certain_location_beats_the_config_but_not_a_country_domain():
    assert phone_region("Brașov, Romania", _config("US"), "clinic.com") == "RO"
    assert phone_region("Denver, Colorado", _config("RO"), "clinic.com") == "US"
    assert phone_region("Denver, Colorado", _config("US"), "clinica.ro") == "RO"


def test_config_then_an_uncertain_location():
    # "CO" is Colorado or Colombia: only a guess, so the config wins it.
    assert phone_region("Denver, CO", _config("RO"), "clinic.com") == "RO"
    assert phone_region("Denver, CO", None, "clinic.com") == "US"


def test_stored_listing_phone_formats():
    assert phone_hit("0268 000 101", "RO").display == "+40 268 000 101"
    assert phone_hit("+40268000101").type == "landline"
    assert phone_hit("call reception", "RO") is None


# ── storage ──

@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "phones.db"))
    await sm.init_db()
    await seed_signal_catalog(sm)
    return sm


def _rows(state, sql):
    with closing(sqlite3.connect(state.db_path)) as db:
        return db.execute(sql).fetchall()


HOME = ('<a href="tel:+40268000101">Sună</a>'
        "<p>Recepție: 0268 000 101, urgențe 0740 000 102. Fax: 0268 000 109</p>")


async def _profile(state, confirmed, companies, monkeypatch):
    async def fake_resolve(self, domain):
        return f"https://{domain}", HOME

    async def fake_first(self, site, paths):
        return "", ""

    async def no_sleep(_):
        return None

    monkeypatch.setattr(ProfileCollector, "_resolve_site", fake_resolve)
    monkeypatch.setattr(ProfileCollector, "_first_present", fake_first)
    monkeypatch.setattr("harvey.collectors.profile.asyncio.sleep", no_sleep)
    async with ProfileCollector(state, confirmed) as c:
        return await c.run(companies)


@pytest.mark.asyncio
async def test_state_fills_an_empty_phone_only(state):
    empty = await state.add_company(Company(name="Alfa", domain="alfa.example"))
    listed = await state.add_company(Company(name="Beta", domain="beta.example",
                                             phone="0268 000 150"))
    assert await state.fill_company_phone(empty, "+40 268 000 101") is True
    assert await state.fill_company_phone(empty, "+40 268 000 102") is False
    assert await state.fill_company_phone(listed, "+40 268 000 101") is False
    phones = dict(_rows(state, "SELECT name, phone FROM companies"))
    assert phones == {"Alfa": "+40 268 000 101", "Beta": "0268 000 150"}


@pytest.mark.asyncio
async def test_profile_stores_site_phones(state, monkeypatch):
    empty = await state.add_company(Company(name="Alfa", domain="alfa.ro",
                                            location="Brașov"))
    listed = await state.add_company(Company(name="Beta", domain="beta.ro",
                                             phone="0268 000 150", location="Brașov"))
    await state.set_signal_status("PHONE_NUMBER", "confirmed")
    n = await _profile(state, {"PHONE_NUMBER"}, [
        {"id": empty, "domain": "alfa.ro", "location": "Brașov"},
        {"id": listed, "domain": "beta.ro", "location": "Brașov"},
    ], monkeypatch)
    assert n == 4
    phones = dict(_rows(state, "SELECT name, phone FROM companies"))
    assert phones == {"Alfa": "+40 268 000 101", "Beta": "0268 000 150"}
    obs = _rows(state, f"SELECT value_text, confidence, evidence_url, collector "
                       f"FROM observations WHERE company_id = '{empty}' "
                       f"AND signal_code = 'PHONE_NUMBER' ORDER BY confidence DESC")
    assert obs == [("+40268000101", 1.0, "https://alfa.ro", "profile"),
                   ("+40740000102", 0.9, "https://alfa.ro", "profile")]


@pytest.mark.asyncio
async def test_unconfirmed_phone_signal_still_fills_the_company_phone(state, monkeypatch):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.ro"))
    await _profile(state, {"TECH_STACK"}, [{"id": cid, "domain": "alfa.ro"}], monkeypatch)
    assert _rows(state, "SELECT phone FROM companies") == [("+40 268 000 101",)]
    assert _rows(state, "SELECT * FROM observations WHERE signal_code = 'PHONE_NUMBER'") == []


def _scout(state, pages, extra=()):
    scout = Scout.__new__(Scout)
    scout.state = state
    scout.config = _config("RO")
    scout._site_reads = {"alfa.example": {"pages": list(pages), "extra_pages": list(extra)}}
    return scout


@pytest.mark.asyncio
async def test_scout_reads_phones_from_pages_it_already_has(state):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.example"))
    await state.set_signal_status("PHONE_NUMBER", "confirmed")
    scout = _scout(state, [("https://alfa.example", "<p>Clinica Alfa</p>")],
                   extra=[("https://alfa.example/contact", "<p>Telefon: 0268 000 120</p>")])
    await scout._observe_phones(cid, "alfa.example")
    assert _rows(state, "SELECT phone FROM companies") == [("+40 268 000 120",)]
    assert _rows(state, "SELECT value_text, evidence_url FROM observations "
                        "WHERE signal_code = 'PHONE_NUMBER'") == [
        ("+40268000120", "https://alfa.example/contact")]


@pytest.mark.asyncio
async def test_scout_phone_rows_do_not_count_as_profiled(state):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.example"))
    await state.set_signal_status("PHONE_NUMBER", "confirmed")
    scout = _scout(state, [("https://alfa.example", "<p>Telefon: 0268 000 120</p>")])
    await scout._observe_phones(cid, "alfa.example")
    assert _rows(state, "SELECT collector FROM observations "
                        "WHERE signal_code = 'PHONE_NUMBER'") == [("people",)]
    assert [c["id"] for c in await state.companies_needing_profile()] == [cid]


@pytest.mark.asyncio
async def test_scout_phone_rows_wait_for_confirmation(state):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.example",
                                          phone="0268 000 150"))
    scout = _scout(state, [("https://alfa.example", "<p>Telefon: 0268 000 120</p>")])
    await scout._observe_phones(cid, "alfa.example")
    assert _rows(state, "SELECT phone FROM companies") == [("0268 000 150",)]
    assert _rows(state, "SELECT * FROM observations WHERE signal_code = 'PHONE_NUMBER'") == []


@pytest.mark.asyncio
async def test_scout_never_fetches_for_phones(state):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.example"))
    scout = Scout.__new__(Scout)
    scout.state = state
    scout._site_reads = {}

    async def no_fetch(*a, **kw):
        raise AssertionError("phones must come from pages already read")

    scout._fetch = no_fetch
    scout._resolve_base_url = no_fetch
    await scout._observe_phones(cid, "alfa.example")
    assert _rows(state, "SELECT phone FROM companies") == [("",)]


# ── export ──

@pytest.mark.asyncio
async def test_company_export_phone_columns(state):
    assert COMPANY_CSV_COLUMNS[-2:] == ["phone_type", "other_phones"]
    cid = await state.add_company(Company(name="Alfa", domain="alfa.example",
                                          phone="0268 000 101", location="Brașov, Romania"))
    await state.add_company(Company(name="Beta", domain="beta.example",
                                    phone="Reception desk"))
    await state.add_company(Company(name="Gama", domain="gama.example"))
    await state.set_signal_status("PHONE_NUMBER", "confirmed")
    await state.add_observations([
        {"signal_code": "PHONE_NUMBER", "company_id": cid, "value_text": v,
         "observed_at": f"2026-10-0{i + 1} 00:00:00"}
        for i, v in enumerate(["+40268000101", "+40740000102", "+40268000103",
                               "+40740000102"])
    ])
    rows = {r["name"]: r for r in await collect_companies(state)}
    assert (rows["Alfa"]["phone"], rows["Alfa"]["phone_type"]) == ("+40 268 000 101", "landline")
    assert rows["Alfa"]["other_phones"] == "+40 740 000 102; +40 268 000 103"
    assert (rows["Beta"]["phone"], rows["Beta"]["phone_type"]) == ("Reception desk", "")
    assert rows["Gama"]["phone"] == rows["Gama"]["other_phones"] == ""

    _, text = await export_companies_csv(state)
    alfa = next(r for r in csv.DictReader(io.StringIO(text)) if r["name"] == "Alfa")
    assert alfa["phone"] == "+40 268 000 101"
    assert alfa["other_phones"] == "+40 740 000 102; +40 268 000 103"     # not quoted


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [
    "+40 268 000 101;+40 740 000 102",
    "0268 000 101 / 0740 000 102",
    "0268000101, 0740 000 102",
])
async def test_company_export_splits_a_listing_with_several_numbers(state, listed):
    cid = await state.add_company(Company(name="Alfa", domain="alfa.ro", phone=listed))
    await state.set_signal_status("PHONE_NUMBER", "confirmed")
    await state.add_observations([
        {"signal_code": "PHONE_NUMBER", "company_id": cid, "value_text": v}
        for v in ["+40740000102", "+40268000103"]])
    (row,) = await collect_companies(state)
    assert (row["phone"], row["phone_type"]) == ("+40 268 000 101", "landline")
    assert row["other_phones"] == "+40 740 000 102; +40 268 000 103"


@pytest.mark.asyncio
async def test_company_export_keeps_a_slash_inside_one_number(state):
    await state.add_company(Company(name="Alfa", domain="alfa.ro", phone="0268/000.101"))
    (row,) = await collect_companies(state)
    assert (row["phone"], row["other_phones"]) == ("+40 268 000 101", "")


def test_trailing_fax_note_does_not_label_the_next_number():
    for html in ("<p>0268 000 134 (fax), 0740 000 135 (mobil)</p>",
                 "<p>0268 000 134 (fax)</p><p>0740 000 135</p>"):
        assert [h.number for h in phones_from_html(html, "RO")] == ["+40740000135"]


def test_country_tld_beats_a_county_code_location():
    assert phone_region("Sibiu, SB", None, "clinica-exemplu.ro") == "RO"
    assert phone_region("Constanța, CT", None, "clinica-exemplu.ro") == "RO"
