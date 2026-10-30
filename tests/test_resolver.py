"""One way to tell whether two records are the same company.

A map hit for a registry firm with no website used to become a second
company: discovery matched on domain and listing id only, the registry on
CUI only. The resolver scores every incoming record against every key on
file, merges when sure, asks when not, never merges two CUIs, and every
merge can be undone exactly.
"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey import resolver
from harvey.collectors import discover as D
from harvey.collectors import onrc
from harvey.models import Company, Prospect
from harvey.modules import ingest, runs
from harvey.signals import sync_vocabulary
from harvey.state import MIGRATIONS, StateManager

DATASET = onrc.Dataset("firme-01-01-2026", "2026-01-01", {})


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "r.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    for code in ("ACTIVITY_CODE", "PHONE_NUMBER", "FOUND_IN_SERP", "CONTACT_FOUND"):
        await sm.set_signal_status(code, "confirmed")
    return sm


def _firm(n=1, name="EXEMPLU SRL", locality="Brașov", cui=None, website="", domain=""):
    return onrc.Firm(reg=f"J08/{n}/2010", name=name, cui=cui or str(90000000 + n), form="SRL",
                     county="Brașov", locality=locality, address=f"Str. Exemplu nr. {n}",
                     website=website, domain=domain, registered="2010-01-01")


async def _pull(state, *firms):
    report = onrc.ImportReport(dataset=DATASET.name)
    draft = onrc.firms_draft(list(firms), DATASET, codes={f.reg: [("8623", 3)] for f in firms},
                             classes=[("8623", None)]).stamp("onrc", "registry_search")
    draft.values.update(codes=("ACTIVITY_CODE",), collector=onrc.COLLECTOR)
    await runs.save_source_draft(state, draft, report, module="onrc")
    assert not report.errors, report.errors
    return report


async def _upsert(state, biz, query):
    """A listing saved as discovery saves it: (company id, was it new)."""
    saved = await ingest.listing(state, D.business_record(biz, query))
    return saved.company_id, saved.created


def _maps_hit(name="Exemplu", phone="", location="Brașov, Romania", n=1, domain=""):
    return D.Business(name=name, phone=phone, location=location, domain=domain,
                      website=f"https://{domain}" if domain else "",
                      external_id=f"dataforseo:cid:{n}", provider="dataforseo_maps",
                      has_website=bool(domain))


QUERY = D.DiscoveryQuery(term="dentist", location="Brașov")


def _rows(path, sql, *args):
    with sqlite3.connect(path) as con:
        return con.execute(sql, args).fetchall()


# ── Keys ──


@pytest.mark.parametrize("raw, key", [
    ("EXEMPLU SRL", "exemplu"),
    ("S.C. Exemplu S.R.L.", "exemplu"),
    ("Exemplu", "exemplu"),
    ("EXEMPLU DENT SRL-D", "exemplu dent"),
    ("CMI Dr. Exemplu Ana", "dr exemplu ana"),
    ("Exemplu Roofing, LLC", "exemplu roofing"),
    ("Exemplu Roofing Co.", "exemplu roofing"),
    ("AB SRL", ""),                         # too little left to tell apart
    ("SRL", "srl"),                         # a form alone is never stripped to nothing
])
def test_name_key_strips_legal_forms_from_either_end(raw, key):
    assert resolver.name_key(raw) == key


def test_a_short_form_counts_only_at_the_end_where_it_is_one():
    # "SA" at the start is part of a name, not a legal form.
    assert resolver.name_key("SA EXEMPLU SRL") == "sa exemplu"


@pytest.mark.parametrize("location, town", [
    ("Brașov, Brașov, Romania", "brasov"),
    ("Brasov", "brasov"),
    ("Municipiul Brașov", "brasov"),
    ("București Sectorul 3, București, Romania", "bucuresti"),
    ("Sector 1, București", "bucuresti"),
    ("Mun. Bucuresti, Sector 2", "bucuresti"),
    ("Denver, CO", "denver"),
    ("", ""),
])
def test_locality_key_is_the_town_without_labels(location, town):
    assert resolver.locality_key(location) == town


def test_legal_forms_are_data_with_a_country():
    text = (Path(resolver.__file__).parent / "reference" / "legal_forms.csv").read_text("utf-8")
    header = next(line for line in text.splitlines() if not line.startswith("#"))
    assert header == "country,form,where"
    assert {"RO", "US"} <= {line.split(",")[0] for line in text.splitlines()[1:]
                            if line and not line.startswith("#")}


@pytest.mark.parametrize("domain", ["gmail.com", "facebook.com", "yelp.com", "wixsite.wix.com",
                                    "clicksanitas.ro", "linktr.ee", "bit.ly", "maps.app.goo.gl",
                                    "wa.me", "booking.com", "listafirme.ro"])
def test_aggregator_and_free_mail_domains_are_not_keys(domain):
    kinds = {k for k, _ in resolver.identifiers(Company(name="Exemplu", domain=domain))}
    assert "domain" not in kinds


def test_a_phone_key_is_a_valid_number():
    assert resolver.phone_key("+400000000") == ""
    assert resolver.phone_key("+40700000000") == "+40700000000"
    assert resolver.phone_key("0268 000 000", "Brașov, Romania") == "+40268000000"


@pytest.mark.asyncio
async def test_a_shared_booking_site_is_not_one_company(state):
    one = await _upsert(state, _maps_hit(name="Exemplu Unu", n=1, domain="clicksanitas.ro"), QUERY)
    two = await _upsert(state, _maps_hit(name="Exemplu Doi", n=2, domain="clicksanitas.ro"), QUERY)
    assert one[0] != two[0] and two[1]


@pytest.mark.asyncio
async def test_a_domain_two_companies_hold_says_nothing(state):
    a = await state.add_company(Company(name="Exemplu Unu", location="Cluj"))
    b = await state.add_company(Company(name="Exemplu Doi", location="Iași"))
    with sqlite3.connect(state.db_path) as con:      # a site both declare
        con.executemany("INSERT INTO company_ids (company_id, kind, value) "
                        "VALUES (?, 'domain', 'grup.example.com')", [(a,), (b,)])
    found = await state.match_company(Company(name="Exemplu Trei", location="Sibiu",
                                              domain="grup.example.com"))
    assert found.company_id is None


# ── The bug: a map hit for a registry firm without a website ──


@pytest.mark.asyncio
async def test_map_hit_with_same_name_and_town_goes_to_review(state):
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")

    company_id, is_new = await _upsert(state, _maps_hit(), QUERY)

    assert is_new and company_id != firm.id         # kept, not merged on a name alone
    [pair] = await state.duplicates()
    assert (pair["company_a"], pair["company_b"]) == (firm.id, company_id)
    assert pair["score"] == 0.7
    assert pair["reasons"] == [{"code": "name", "value": "exemplu@brasov"}]
    assert pair["a"]["cui"] == "90000001" and pair["b"]["listings"] == ["dataforseo:cid:1"]


@pytest.mark.asyncio
async def test_map_hit_with_same_name_town_and_phone_merges(state):
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")
    # The registry carries no phone; the firm's own site gave one.
    await state.add_observation(company_id=firm.id, signal_code="PHONE_NUMBER",
                                collector="profile", value_text="+40700000001")

    company_id, is_new = await _upsert(state, _maps_hit(phone="0700 000 001"), QUERY)

    assert company_id == firm.id and not is_new
    [merge] = await state.merges()
    assert merge["survivor"] == firm.id and merge["decided_by"] == "auto"
    assert merge["score"] == 0.94                   # 1 - (1 - 0.8) * (1 - 0.7)
    assert {r["code"] for r in merge["reasons"]} == {"phone", "name"}
    assert await state.duplicates() == []
    # The listing's phone filled the gap; its listing id now finds the firm.
    assert (await state.get_company(firm.id)).phone == "0700 000 001"
    assert (await state.get_company_by_external_id("dataforseo:cid:1")).id == firm.id
    # One company on the Firme page, not two.
    counts = _rows(state.db_path, "SELECT COUNT(*) FROM companies WHERE merged_into = ''")
    assert counts == [(1,)]


@pytest.mark.asyncio
async def test_the_same_listing_again_is_the_same_record(state):
    first, new = await _upsert(state, _maps_hit(phone="0700 000 002"), QUERY)
    again, new_again = await _upsert(state, _maps_hit(phone="0700 000 002"), QUERY)
    assert new and not new_again and again == first
    assert await state.merges() == [] and await state.duplicates() == []


@pytest.mark.asyncio
async def test_a_phone_alone_is_a_question_not_an_answer(state):
    a, _ = await _upsert(state, _maps_hit(name="Exemplu Unu", phone="0700 000 003", n=1), QUERY)
    b, new = await _upsert(state, _maps_hit(name="Altceva Doi", phone="0700 000 003", n=2), QUERY)
    assert new and a != b
    [pair] = await state.duplicates()
    assert pair["score"] == 0.8 and pair["reasons"][0]["code"] == "phone"


@pytest.mark.asyncio
async def test_a_phone_many_companies_share_says_nothing(state):
    for n in range(1, 4):
        await _upsert(state, _maps_hit(name=f"Firma {n} Exemplu", phone="0700 000 004", n=n), QUERY)
    match = await state.match_company(Company(name="Alta Exemplu", phone="+40700000004",
                                              location="Brașov"))
    assert match.company_id is None and match.score == 0


@pytest.mark.asyncio
async def test_same_name_with_different_websites_is_not_evidence(state):
    await _upsert(state, _maps_hit(domain="exemplu-unu.example.com", n=1), QUERY)
    _, new = await _upsert(state, _maps_hit(domain="exemplu-doi.example.com", n=2), QUERY)
    assert new and await state.duplicates() == []


@pytest.mark.asyncio
async def test_aggregator_domains_never_match(state):
    a = await state.save_company(Company(name="Exemplu Unu", domain="facebook.com",
                                         location="Cluj"))
    b = await state.save_company(Company(name="Altceva Doi", domain="facebook.com",
                                         location="Iași"))
    assert b.match.score == 0
    assert _rows(state.db_path, "SELECT COUNT(*) FROM company_ids WHERE kind = 'domain'") == [(0,)]
    assert a.company_id and a.created


# ── Two CUIs never merge ──


@pytest.mark.asyncio
async def test_two_cuis_with_the_same_name_town_and_phone_stay_two(state):
    await _pull(state, _firm(1), _firm(2))
    one = await state.get_company_by_cui("90000001")
    two = await state.get_company_by_cui("90000002")
    assert one.id != two.id
    assert await state.duplicates() == []               # not even a question
    for cid in (one.id, two.id):
        await state.add_observation(company_id=cid, signal_code="PHONE_NUMBER",
                                    value_text="+40700000005")
    match = await state.match_company(Company(name="EXEMPLU SRL", cui="90000003",
                                              phone="+40700000005", location="Brașov"))
    assert match.company_id is None
    with pytest.raises(resolver.MergeRefused) as refused:
        await state.merge_companies(one.id, two.id)
    assert refused.value.code == "different_cui"


@pytest.mark.asyncio
async def test_registry_still_matches_on_cui_and_fills_gaps(state):
    await _pull(state, _firm())
    report = await _pull(state, _firm(website="https://exemplu.example.com",
                                      domain="exemplu.example.com"))
    assert (report.known_companies, report.new_companies) == (1, 0)
    firm = await state.get_company_by_cui("90000001")
    assert firm.domain == "exemplu.example.com"


@pytest.mark.asyncio
async def test_a_declared_website_alone_is_only_a_question(state):
    site = await state.save_company(Company(name="Zâmbet Exemplu", domain="zambet.example.com",
                                            location="Cluj"))
    report = await _pull(state, _firm(name="ALTCEVA SRL", website="https://zambet.example.com",
                                      domain="zambet.example.com"))
    assert report.new_companies == 1 and report.shared_domain == 1
    [pair] = await state.duplicates()
    assert pair["company_a"] == site.company_id
    assert pair["reasons"] == [{"code": "declared_domain", "value": "zambet.example.com"}]


# ── Merge and undo ──


def _snapshot(path):
    tables = {
        "companies": "SELECT id, name, cui, external_id, domain, website, phone, address, "
                     "location, industry, merged_into FROM companies ORDER BY id",
        "observations": "SELECT id, company_id FROM observations ORDER BY id",
        "prospects": "SELECT id, company_id FROM prospects ORDER BY id",
        "company_ids": "SELECT company_id, kind, value FROM company_ids ORDER BY 1, 2, 3",
        "company_activities": "SELECT company_id, code, version, source "
                              "FROM company_activities ORDER BY 1, 2, 3, 4",
        "search": "SELECT company_id, name, place FROM company_search ORDER BY company_id",
    }
    return {name: _rows(path, sql) for name, sql in tables.items()}


@pytest.mark.asyncio
async def test_undo_restores_exactly(state):
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")
    other = (await state.save_company(Company(
        name="Exemplu", phone="+40700000006", location="Brașov", source="discover:osm",
        external_id="osm:node/6", website="https://exemplu.example.com",
        domain="exemplu.example.com"))).company_id
    await state.add_observation(company_id=other, signal_code="FOUND_IN_SERP", value_text="osm")
    await state.add_prospect(Prospect(first_name="Ana", last_name="Exemplu",
                                      email="ana@exemplu.example.com", company_id=other))
    await state.set_company_activities(other, [("8623", 3), ("8690", 2)], source="profile")
    before = _snapshot(state.db_path)

    merge_id = await state.merge_companies(firm.id, other)
    assert merge_id
    after = _snapshot(state.db_path)
    assert {r[1] for r in after["observations"]} == {firm.id}
    assert {r[1] for r in after["prospects"]} == {firm.id}
    assert other not in {r[0] for r in after["search"]}
    [kept] = [c for c in after["companies"] if c[0] == firm.id]
    assert kept[4] == "exemplu.example.com" and kept[6] == "+40700000006"   # gaps filled
    assert kept[1] == "EXEMPLU SRL"                                       # nothing replaced
    total, hits = await state.search_companies("exemplu")
    assert [h["id"] for h in hits] == [firm.id]

    await state.undo_merge(other)
    assert _snapshot(state.db_path) == before
    # Back on the review list: an undo says the merge was wrong, and a
    # person still answers whether they are one company.
    [pair] = await state.duplicates("pending")
    assert {pair["company_a"], pair["company_b"]} == {firm.id, other}


@pytest.mark.asyncio
async def test_an_automatic_merge_can_be_undone(state):
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")
    await state.add_observation(company_id=firm.id, signal_code="PHONE_NUMBER",
                                value_text="+40700000007")
    await _upsert(state, _maps_hit(phone="+40700000007", n=7), QUERY)
    [merge] = await state.merges()

    await state.undo_merge(merge["merged"])
    split = await state.get_company(merge["merged"])
    assert split.merged_into == "" and split.external_id == "dataforseo:cid:7"
    assert (await state.get_company_by_external_id("dataforseo:cid:7")).id == split.id
    assert await state.merges() == []


@pytest.mark.asyncio
async def test_undo_takes_back_what_the_listing_brought_after_the_merge(state):
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")
    await state.add_observation(company_id=firm.id, signal_code="PHONE_NUMBER",
                                value_text="+40700000007")
    hit = _maps_hit(phone="+40700000007", n=7)
    hit.source_url = "https://maps.example.com/?cid=7"
    survivor, created = await _upsert(state, hit, QUERY)
    assert survivor == firm.id and not created
    # The run goes on to write the listing's facts to the company it joined.
    await state.add_observation(company_id=survivor, signal_code="REVIEW_RATING",
                                collector="discover", value_num=4.5,
                                evidence_url=hit.source_url)
    [merge] = await state.merges()
    await state.undo_merge(merge["merged"])
    assert _rows(state.db_path, "SELECT company_id FROM observations "
                                "WHERE signal_code = 'REVIEW_RATING'") == [(merge["merged"],)]
    # The survivor keeps the phone its own site gave.
    assert (firm.id,) in _rows(state.db_path, "SELECT company_id FROM company_ids "
                                              "WHERE kind = 'phone' AND value = '+40700000007'")


@pytest.mark.asyncio
async def test_two_merges_at_once_leave_one_company(state):
    import asyncio

    a = (await state.save_company(Company(name="Unu Exemplu", location="Cluj"))).company_id
    b = (await state.save_company(Company(name="Doi Exemplu", location="Iași"))).company_id
    done = await asyncio.gather(state.merge_companies(a, b), state.merge_companies(b, a),
                                return_exceptions=True)
    refused = [r for r in done if isinstance(r, resolver.MergeRefused)]
    assert len(refused) == 1 and refused[0].code == "already_merged"
    live = _rows(state.db_path, "SELECT id FROM companies WHERE merged_into = ''")
    assert len(live) == 1


@pytest.mark.asyncio
async def test_a_double_click_merges_once(state):
    import asyncio

    a = (await state.save_company(Company(name="Unu Exemplu", location="Cluj"))).company_id
    b = (await state.save_company(Company(name="Doi Exemplu", location="Iași"))).company_id
    done = await asyncio.gather(state.merge_pair(a, b), state.merge_pair(a, b),
                                return_exceptions=True)
    assert sum(isinstance(r, dict) for r in done) == 1
    assert len(await state.merges()) == 1
    await state.undo_merge(b)
    assert _rows(state.db_path, "SELECT COUNT(*) FROM companies WHERE merged_into = ''") == [(2,)]
    assert await state.merges() == []


@pytest.mark.asyncio
async def test_a_company_is_not_merged_with_itself(state):
    a = (await state.save_company(Company(name="Unu Exemplu", location="Cluj"))).company_id
    for call in (state.merge_pair, state.separate_companies):
        with pytest.raises(resolver.MergeRefused) as refused:
            await call(a, a)
        assert refused.value.code == "same_company"
    with pytest.raises(resolver.MergeRefused) as refused:
        await state.separate_companies(a, "no-such-company")
    assert refused.value.code == "not_found"


@pytest.mark.asyncio
async def test_undo_waits_for_a_later_merge_of_the_survivor(state):
    a = (await state.save_company(Company(name="Unu Exemplu", location="Cluj"))).company_id
    b = (await state.save_company(Company(name="Doi Exemplu", location="Iași"))).company_id
    c = (await state.save_company(Company(name="Trei Exemplu", location="Sibiu"))).company_id
    await state.merge_companies(b, c)
    await state.merge_companies(a, b)
    assert (await state.get_company(c)).merged_into == a      # stands for the survivor now
    with pytest.raises(resolver.MergeRefused) as refused:
        await state.undo_merge(c)
    assert refused.value.code == "survivor_merged"
    await state.undo_merge(b)
    assert (await state.get_company(c)).merged_into == b
    await state.undo_merge(c)
    assert (await state.get_company(c)).merged_into == ""


@pytest.mark.asyncio
async def test_merge_pair_keeps_the_registry_record(state):
    site = (await state.save_company(Company(name="Exemplu", location="Brașov",
                                             source="discover:osm"))).company_id
    await _pull(state, _firm())
    firm = await state.get_company_by_cui("90000001")
    [pair] = await state.duplicates()
    done = await state.merge_pair(pair["company_a"], pair["company_b"])
    assert (done["survivor"], done["merged"]) == (firm.id, site)


@pytest.mark.asyncio
async def test_separate_takes_the_pair_off_the_list(state):
    await _pull(state, _firm())
    await _upsert(state, _maps_hit(), QUERY)
    [pair] = await state.duplicates()
    await state.separate_companies(pair["company_a"], pair["company_b"])
    assert await state.duplicates() == [] and await state.count_duplicates() == 0


@pytest.mark.asyncio
async def test_merged_companies_leave_cohorts_and_work_lists(state):
    a = (await state.save_company(Company(name="Unu Exemplu", domain="unu.example.com",
                                          location="Cluj"))).company_id
    b = (await state.save_company(Company(name="Doi Exemplu", domain="doi.example.com",
                                          location="Cluj"))).company_id
    await state.merge_companies(a, b)
    assert [c["id"] for c in await state.companies_needing_profile()] == [a]
    assert [c["id"] for c in await state.companies_needing_prospects()] == [a]


# ── Keys on file ──


@pytest.mark.asyncio
async def test_existing_companies_get_their_keys_on_upgrade(tmp_path):
    path = str(tmp_path / "old.db")
    sm = StateManager(path)
    await sm.init_db()
    cid = await sm.add_company(Company(name="EXEMPLU SRL", cui="90000008", phone="0268 000 000",
                                       location="Brașov, Brașov, Romania",
                                       external_id="onrc:J08/8/2010", domain="exemplu.example.com"))
    with sqlite3.connect(path) as con:          # as a v16 database would have it
        con.execute("DELETE FROM company_ids")
    async with sm._connect() as db:
        from harvey.state import _backfill_company_ids
        await _backfill_company_ids(db)
        await db.commit()
    assert set(_rows(path, "SELECT kind, value FROM company_ids WHERE company_id = ?", cid)) == {
        ("cui", "90000008"), ("external", "onrc:J08/8/2010"), ("reg", "J08/8/2010"),
        ("domain", "exemplu.example.com"), ("phone", "+40268000000"), ("name", "exemplu@brasov")}


@pytest.mark.asyncio
async def test_duplicates_already_on_file_are_found_on_upgrade(tmp_path):
    path = str(tmp_path / "old.db")
    sm = StateManager(path)
    await sm.init_db()

    async def add(**kw):
        return await sm.add_company(Company(**kw))
    firm = await add(name="EXEMPLU SRL", cui="90000008", location="Brașov, Brașov, Romania")
    osm = await add(name="Exemplu", location="Brașov, Romania", external_id="osm:node/1")
    listing = await add(name="Exemplu Dent", phone="+40700000011", location="Cluj",
                        external_id="osm:node/2")
    clinic = await add(name="EXEMPLU DENT SRL", phone="+40 700 000 011", cui="90000009",
                       location="Turda, Cluj")
    site = await add(name="Exemplu Trei", phone="+40700000012", location="Sibiu",
                     domain="trei.example.com")
    registry = await add(name="EXEMPLU TREI SRL", phone="+40 700 000 012", cui="90000010",
                         location="Sibiu, Sibiu")
    with sqlite3.connect(path) as con:          # a v16 database: no keys, no pairs
        con.execute("DELETE FROM company_ids")
        con.execute("DELETE FROM match_candidates")
    async with sm._connect() as db:
        from harvey.state import _resolve_on_file
        await _resolve_on_file(db)
        await db.commit()
    pending = {frozenset((p["company_a"], p["company_b"])): p["score"]
               for p in await sm.duplicates("pending")}
    assert pending == {frozenset((firm, osm)): 0.7,          # same name, same town
                       frozenset((listing, clinic)): 0.8}    # same phone, another town
    # Phone and name together: sure enough to merge, the registry's record kept.
    [merge] = await sm.merges()
    assert (merge["survivor"], merge["merged"], merge["decided_by"]) == (registry, site, "auto")


@pytest.mark.asyncio
async def test_an_upgrade_stopped_halfway_starts_again(tmp_path, monkeypatch):
    import harvey.state as S

    path = str(tmp_path / "half.db")

    async def boom(db):
        raise RuntimeError("stopped")
    monkeypatch.setitem(S._AFTER_MIGRATION, 17, boom)
    with pytest.raises(RuntimeError):
        await StateManager(path).init_db()
    assert _rows(path, "PRAGMA user_version") == [(16,)]
    assert "merged_into" not in {r[1] for r in _rows(path, "PRAGMA table_info(companies)")}
    monkeypatch.setitem(S._AFTER_MIGRATION, 17, S._resolve_on_file)
    await StateManager(path).init_db()
    assert _rows(path, "PRAGMA user_version") == [(len(S.MIGRATIONS),)]


@pytest.mark.asyncio
async def test_two_processes_upgrading_at_once_both_start(tmp_path):
    import asyncio

    path = str(tmp_path / "both.db")
    done = await asyncio.gather(StateManager(path).init_db(), StateManager(path).init_db(),
                                return_exceptions=True)
    assert not [r for r in done if isinstance(r, Exception)], done
    assert _rows(path, "PRAGMA user_version") == [(len(MIGRATIONS),)]


@pytest.mark.asyncio
async def test_a_site_phone_becomes_a_key(state):
    cid = await state.add_company(Company(name="Exemplu", location="Cluj"))
    await state.add_observation(company_id=cid, signal_code="PHONE_NUMBER",
                                collector="profile", value_text="+40700000009")
    assert _rows(state.db_path, "SELECT company_id FROM company_ids WHERE kind = 'phone' "
                                "AND value = '+40700000009'") == [(cid,)]


@pytest.mark.asyncio
async def test_lookups_use_the_key_index(state):
    plan = " ".join(r[-1] for r in _rows(
        state.db_path, "EXPLAIN QUERY PLAN SELECT company_id FROM company_ids "
                       "WHERE kind = ? AND value = ?", "phone", "+40700000000"))
    assert "USING" in plan and "INDEX" in plan and "SCAN" not in plan


def test_nothing_here_asks_a_model():
    source = Path(resolver.__file__).read_text("utf-8")
    assert "brain" not in source and "claude" not in source.lower()


# ── Dashboard and CLI ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import harvey.dashboard as dash

    db = tmp_path / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


async def _seed_pair(path):
    sm = StateManager(str(path))
    await sm.init_db()
    await _pull(sm, _firm())
    site = (await sm.save_company(Company(name="Exemplu", location="Brașov",
                                          phone="+40700000010", source="discover:osm"))).company_id
    return sm, (await sm.get_company_by_cui("90000001")).id, site


def test_review_list_merge_and_undo_over_the_api(client):
    import asyncio
    _, firm, site = asyncio.run(_seed_pair(client.db_path))

    data = client.get("/api/duplicates").json()
    assert data["pending"] == 1
    [row] = data["rows"]
    assert row["a"]["name"] == "EXEMPLU SRL" and row["b"]["phones"] == ["+40700000010"]
    assert client.get("/api/companies?limit=50").json()["total"] == 2

    done = client.post("/api/duplicates/merge", json={"a": site, "b": firm}).json()
    assert done["survivor"] == firm
    listed = client.get("/api/companies?limit=50").json()
    assert listed["total"] == 1 and listed["companies"][0]["id"] == firm
    assert "+40 700 000 010" in (listed["companies"][0]["phone_display"],
                                 *listed["companies"][0]["other_phones"])
    [merged] = client.get("/api/duplicates?status=merged").json()["rows"]
    assert merged["merged"] == site

    assert client.post("/api/duplicates/undo", json={"company_id": site}).status_code == 200
    assert client.get("/api/companies?limit=50").json()["total"] == 2
    again = client.post("/api/duplicates/undo", json={"company_id": site})
    assert again.status_code == 409 and again.json()["error"] == "not_merged"


def test_the_api_refuses_two_cuis(client):
    import asyncio

    async def seed():
        sm = StateManager(str(client.db_path))
        await sm.init_db()
        await _pull(sm, _firm(1), _firm(2))
        return [(await sm.get_company_by_cui(c)).id for c in ("90000001", "90000002")]
    one, two = asyncio.run(seed())
    r = client.post("/api/duplicates/merge", json={"a": one, "b": two})
    assert r.status_code == 409 and r.json()["error"] == "different_cui"
    assert client.post("/api/duplicates/separate", json={"a": one, "b": two}).json() == {"ok": True}
    gone = client.post("/api/duplicates/separate", json={"a": one, "b": "no-such-company"})
    assert gone.status_code == 404 and gone.json()["error"] == "not_found"


def test_the_api_needs_the_token(client):
    from fastapi.testclient import TestClient
    import harvey.dashboard as dash
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as anon:
        assert anon.get("/api/duplicates").status_code == 401
        assert anon.post("/api/duplicates/merge", json={"a": "x", "b": "y"}).status_code == 401


def test_cli_lists_merges_and_undoes(tmp_path, monkeypatch, capsys):
    import asyncio
    import harvey.state as state_mod
    from harvey import cli

    sm, firm, site = asyncio.run(_seed_pair(tmp_path / "cli.db"))
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "cli.db")
    run = lambda **kw: cli.cmd_duplicates(SimpleNamespace(  # noqa: E731
        **{"merge": None, "separate": None, "undo": None, "limit": 50, **kw}))

    run()
    out = capsys.readouterr().out
    assert "1 to decide" in out and "same name in the same town" in out
    assert f"--merge {firm} {site}" in out

    run(merge=[firm, site])
    assert f"harvey duplicates --undo {site}" in capsys.readouterr().out
    run()
    assert "Merged — 1" in capsys.readouterr().out
    run(undo=site)
    assert "is its own company again" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run(undo=site)
    assert "isn't merged" in capsys.readouterr().out


# ── The Firme page ──


def _node(script):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    app = Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js"
    out = subprocess.run([node, "-e", script, str(app), str(app.parent / "i18n.js")],
                         text=True, encoding="utf-8", capture_output=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("lang", ["ro", "en"])
def test_review_panel_shows_both_sides_and_the_choices(lang):
    data = _node(r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
vm.runInThisContext("LANG = '%s';");
const els = {'company-dupes': {innerHTML: ''}, 'dupes-panel': {innerHTML: ''}};
global.document = {getElementById: id => els[id] || null};
global.escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
global.jsq = s => JSON.stringify(s);
global.sourceLabel = s => escHtml(s);
const side = (id, name, extra) => Object.assign({id, name, location: 'Brașov, Romania', phones: [],
  domains: [], sources: []}, extra);
let calls = [];
global.api = async (path) => { calls.push(path); return path.includes('merged')
  ? {pending: 1, rows: [{merged: 'b1', survivor: 'a1', decided_by: 'auto', score: 0.94,
       reasons: [{code: 'phone', value: '+40700000000'}], a: side('a1', 'EXEMPLU SRL', {cui: '90000001'}),
       b: side('b1', 'Exemplu')}]}
  : {pending: 1, rows: [{company_a: 'a1', company_b: 'b1', score: 0.7,
       reasons: [{code: 'name', value: 'exemplu@brasov'}],
       a: side('a1', 'EXEMPLU SRL', {cui: '90000001', sources: ['registry:onrc']}),
       b: side('b1', '<b>Exemplu</b>', {phones: ['+40700000000'], sources: ['discover:osm']})}]}; };
vm.runInThisContext(app.slice(app.indexOf('let _dup = '), app.indexOf('function companyIdLine(')));
(async () => {
  await loadDuplicates();
  const button = els['company-dupes'].innerHTML;
  toggleDuplicates(); await new Promise(r => setTimeout(r, 0));
  const pending = els['dupes-panel'].innerHTML;
  setDuplicatesView('merged'); await new Promise(r => setTimeout(r, 0));
  console.log(JSON.stringify({button, pending, merged: els['dupes-panel'].innerHTML, calls,
    want: {button: t('dup.button', {n: 1}), same: t('dup.same'), different: t('dup.different'),
           undo: t('dup.undo'), name: t('dup.reason.name'), score: t('dup.score', {n: 70}),
           auto: t('dup.by_auto')}}));
})().catch(e => {console.error(e); process.exit(1);});
''' % lang)
    want = data["want"]
    assert want["button"] in data["button"]
    pending = data["pending"]
    for key in ("same", "different", "name", "score"):
        assert want[key] in pending, key
    assert "EXEMPLU SRL" in pending and "90000001" in pending and "+40700000000" in pending
    assert "&lt;b>Exemplu" in pending                    # values are escaped
    assert "decideDuplicate('merge', \"a1\", \"b1\")" in pending
    assert "decideDuplicate('separate', \"a1\", \"b1\")" in pending
    assert want["undo"] in data["merged"] and want["auto"] in data["merged"]
    assert 'undoMerge("b1")' in data["merged"]
    assert data["calls"][-1] == "/api/duplicates?status=merged"


def test_every_duplicate_string_is_in_both_languages():
    data = _node(r'''
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const app = fs.readFileSync(process.argv[1], 'utf8');
const used = [...new Set([...app.matchAll(/t\('(dup\.[a-z_.]*[a-z_])'/g)].map(m => m[1]))];
console.log(JSON.stringify({used,
  ro: Object.keys(I18N.ro).filter(k => k.startsWith('dup.')),
  en: Object.keys(I18N.en).filter(k => k.startsWith('dup.'))}));
''')
    assert sorted(data["ro"]) == sorted(data["en"])
    assert set(data["used"]) <= set(data["en"])
    codes = set(resolver.WEIGHTS) | {"different_websites"}
    assert {f"dup.reason.{c}" for c in codes} <= set(data["en"])
