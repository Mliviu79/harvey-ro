"""Every write path on the overlay (schema 25): a registry pull in bulk,
discovery saves, the contact finder's contacts, evidence, people and
searches, web signals, merges and their undo, and removing a firm. Each
test ends with a full recount finding 0 rows differing from what the
triggers (or a bulk write's recount) kept. All data is fictional."""

import sqlite3

import pytest
import pytest_asyncio

from harvey import contacts, resolver
from harvey.collectors import discover as D
from harvey.collectors import onrc
from harvey.models import Company
from harvey.modules import ingest, runs
from harvey.overlay import recount, schema
from harvey.overlay.store import RemoveRefused
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

DATASET = onrc.Dataset("firme-01-01-2026", "2026-01-01", {})
QUERY = D.DiscoveryQuery(term="dentist", location="Brașov")


@pytest_asyncio.fixture
async def state(tmp_path):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path)
    schema.create(con)
    con.close()
    sm = StateManager(str(path))
    await sm.init_db()
    await sync_vocabulary(sm)
    for code in ("ACTIVITY_CODE", "PHONE_NUMBER", "CONTACT_FOUND", "TECH_STACK",
                 "RUNNING_GOOGLE_ADS"):
        await sm.set_signal_status(code, "confirmed")
    return sm


def _con(state) -> sqlite3.Connection:
    return sqlite3.connect(state.db_path, isolation_level=None)


def _q(state, sql, *args):
    con = _con(state)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _one(state, sql, *args):
    rows = _q(state, sql, *args)
    return rows[0][0] if rows else None


def _clean(state) -> dict:
    con = _con(state)
    try:
        return {k: v for k, v in recount.diff(con).items() if v}
    finally:
        con.close()


def _firm(n=1, name="EXEMPLU SRL", locality="Brașov", cui=None, website="", domain=""):
    return onrc.Firm(reg=f"J08/{n}/2010", name=name, cui=cui if cui is not None else
                     str(90000000 + n), form="SRL", county="Brașov", locality=locality,
                     address=f"Str. Exemplu nr. {n}", website=website, domain=domain,
                     registered="2010-01-01")


async def _pull(state, *firms):
    report = onrc.ImportReport(dataset=DATASET.name)
    draft = onrc.firms_draft(list(firms), DATASET, codes={f.reg: [("8623", 3)] for f in firms},
                             classes=[("8623", None)]).stamp("onrc", "registry_search")
    draft.values.update(codes=("ACTIVITY_CODE",), collector=onrc.COLLECTOR)
    saved: list[str] = []
    await runs.save_source_draft(state, draft, report, module="onrc", saved_ids=saved)
    assert not report.errors, report.errors
    return report, saved


def _hit(name="Exemplu", phone="", location="Brașov, Romania", n=1, domain=""):
    return D.Business(name=name, phone=phone, location=location, domain=domain,
                      website=f"https://{domain}" if domain else "",
                      external_id=f"dataforseo:cid:{n}", provider="dataforseo_maps",
                      has_website=bool(domain))


async def _listing(state, biz):
    saved = await ingest.listing(state, D.business_record(biz, QUERY))
    return saved.company_id, saved.created


def _cui_firm(state, cui: int):
    return _one(state, "SELECT fi.firm FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme "
                       "WHERE s.name = 'ro.cui' AND fi.key = ?", cui)


# ── The registry pull, in bulk ──

@pytest.mark.asyncio
async def test_a_pull_marks_firms_with_their_keys_and_declared_site(state):
    report, saved = await _pull(state, _firm(1, website="https://exemplu-unu.example.com",
                                             domain="exemplu-unu.example.com"),
                                _firm(2, name="ALTA EXEMPLU SRL"))
    assert (report.new_companies, report.known_companies) == (2, 0)
    assert len(saved) == 2 and all(s.isdigit() for s in saved)
    one = _cui_firm(state, 90000001)
    assert str(one) in saved
    # Both identifiers, the declared site (trust unset: never above likely),
    # no local description (the register describes it).
    assert _one(state, "SELECT COUNT(*) FROM firm_ident WHERE firm = ?", one) == 2
    assert _q(state, "SELECT website, domain, website_trust FROM firm WHERE id = ?", one) == [
        ("https://exemplu-unu.example.com", "exemplu-unu.example.com", None)]
    assert _one(state, "SELECT COUNT(*) FROM local_firm") == 0
    # Sourced to this pull (its run as the batch); the register's facts
    # (activities, administrators) are read, never stored.
    assert _one(state, "SELECT COUNT(*) FROM firm_source WHERE batch != 0") == 2
    assert _one(state, "SELECT COUNT(*) FROM obs") == 0
    assert _one(state, "SELECT n FROM tally WHERE name = 'firms'") == 2
    assert _clean(state) == {}

    # Pulled again: known, nothing new; the trigger-kept counts still agree.
    report, saved2 = await _pull(state, _firm(1), _firm(3, name="TREIA EXEMPLU SRL"))
    assert (report.new_companies, report.known_companies) == (1, 1)
    assert str(one) in saved2
    assert _one(state, "SELECT COUNT(*) FROM firm") == 3
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_pull_keeps_the_phones_loaded_registers_know(state, tmp_path):
    tax = sqlite3.connect(tmp_path / "taxpayers.db")
    tax.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('published', '2026-01-01'), ('loaded_at', '2026-01-02');
        CREATE TABLE taxpayers (cui TEXT PRIMARY KEY, phone TEXT);
        CREATE TABLE current (cui INTEGER PRIMARY KEY, vat INTEGER, employer INTEGER,
                              micro INTEGER, phone TEXT);
        INSERT INTO current VALUES (90000001, 1, 1, 0, '0268 000 001');""")
    tax.commit()
    tax.close()
    await _pull(state, _firm(1), _firm(2))
    one = _cui_firm(state, 90000001)
    assert _q(state, "SELECT c.value, s.name, e.trust FROM link_evidence e "
                     "JOIN contact c ON c.id = e.contact JOIN source s ON s.id = e.source "
                     "WHERE e.firm = ?", one) == [("+40268000001", "mf_taxpayers", 0)]
    assert _one(state, "SELECT n_phone FROM firm WHERE id = ?", one) == 1
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_dry_run_pull_writes_nothing(state):
    report = onrc.ImportReport(dataset=DATASET.name)
    draft = onrc.firms_draft([_firm(1)], DATASET, codes={}, classes=[]).stamp(
        "onrc", "registry_search")
    await runs.save_source_draft(state, draft, report, module="onrc", dry_run=True)
    assert report.new_companies == 1
    assert _one(state, "SELECT COUNT(*) FROM firm") == 0


# ── Discovery saves and the resolver on firm_ident ──

@pytest.mark.asyncio
async def test_a_listing_is_a_local_firm_with_its_place_phone_and_source(state):
    cid, created = await _listing(state, _hit(phone="+40700000001", n=7))
    assert created
    fid = int(cid)
    assert _q(state, "SELECT s.name, fi.key FROM firm_ident fi JOIN ident_scheme s "
                     "ON s.id = fi.scheme WHERE fi.firm = ?", fid) == [
        ("dataforseo.listing", "cid:7")]
    assert _one(state, "SELECT name FROM local_firm WHERE firm = ?", fid) == "Exemplu"
    assert _one(state, "SELECT COUNT(*) FROM firm_key WHERE firm = ? AND kind = ?",
                fid, schema.FIRM_KEY["name"]) == 1
    assert _one(state, "SELECT COUNT(*) FROM locations WHERE firm = ?", fid) == 1
    assert _one(state, "SELECT n_phone FROM firm WHERE id = ?", fid) == 1
    # The same listing again is the same firm.
    again, created = await _listing(state, _hit(phone="+40700000001", n=7))
    assert (again, created) == (cid, False)
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_listing_with_a_registered_firms_phone_joins_it_and_undo_splits(state):
    await _pull(state, _firm(1))
    firm = _cui_firm(state, 90000001)
    await state.add_contacts(str(firm), [contacts.Found("phone", "+40700000002", source="anaf",
                                                         trust="confirmed")])
    cid, created = await _listing(state, _hit(name="Clinica Exemplu", phone="+40700000002", n=9))
    # One firm's phone on a record without a CUI: its branch, merged.
    assert cid == str(firm) and not created
    listing = _one(state, "SELECT firm FROM firm_ident WHERE key = 'cid:9'")
    assert _one(state, "SELECT live FROM firm WHERE id = ?", listing) == firm
    assert _clean(state) == {}
    done = await state.undo_merge(str(listing))
    assert done["survivor"] == str(firm)
    assert _one(state, "SELECT live FROM firm WHERE id = ?", listing) == 0
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_two_cuis_never_merge_and_a_registration_number_gaining_its_cui_joins(state):
    # A firm pulled while it had no CUI (only its registration number).
    await _pull(state, _firm(5, cui=""))
    reg_only = _one(state, "SELECT firm FROM firm_ident WHERE key = 'J08/5/2010'")
    assert _one(state, "SELECT COUNT(*) FROM firm_ident WHERE firm = ?", reg_only) == 1
    # Its next pull carries the CUI: the same firm gains it, id unchanged.
    await _pull(state, _firm(5))
    assert _cui_firm(state, 90000005) == reg_only
    # Two firms with different CUIs, whatever else they share, stay two.
    await _pull(state, _firm(6, name="DOI EXEMPLU SRL"), _firm(7, name="DOI EXEMPLU SRL"))
    a, b = _cui_firm(state, 90000006), _cui_firm(state, 90000007)
    with pytest.raises(resolver.MergeRefused) as e:
        await state.merge_group(str(a), [str(b)])
    assert e.value.code == "different_cui"
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_the_registration_number_is_not_a_legal_scheme(state):
    """Only a legal scheme keeps two firms apart: a record whose
    registration number differs is matched on its other keys."""
    con = _con(state)
    legal = dict(con.execute("SELECT name, legal FROM ident_scheme").fetchall())
    con.close()
    assert legal["ro.cui"] == 1 and legal["ro.onrc_reg"] == 0
    m = await state.match_company(Company(name="Exemplu", cui="90000001"))
    assert m.company_id is None


# ── The contact finder: contacts, evidence, people, searches, websites ──

@pytest.mark.asyncio
async def test_site_read_writes_contacts_people_searches_and_its_website(state):
    cid, _ = await _listing(state, _hit(n=11))
    fid = int(cid)
    assert await state.set_found_website(cid, "https://exemplu-site.example.com",
                                         domain="exemplu-site.example.com", trust="likely",
                                         source="tinyfish")
    assert not await state.set_found_website(cid, "https://alt.example.com")
    kept = await state.add_contacts(cid, [
        contacts.Found("email", "office@exemplu-site.example.com", source="website",
                       capability="read_site", trust="likely"),
        contacts.Found("email", "ana@exemplu-site.example.com", source="website",
                       capability="read_site", person="Ana Exemplu", role="owner",
                       trust="likely"),
        contacts.Found("phone", "0700 000 003", region="RO", source="website",
                       capability="read_site", trust="likely"),
        contacts.Found("phone", "not a number", source="website")])
    assert kept == 3
    assert await state.add_people(cid, [("Ana Exemplu", "owner"), ("Dan Test", "")],
                                  source="website") == 1
    await state.record_search("company", cid, "website", "read_site",
                              looked_for="phone,email,contact_form,social,people,owner",
                              outcome="found", found=3)
    person = _one(state, "SELECT id FROM person WHERE name = 'Dan Test'")
    await state.record_search("person", person, "reoon", "verify_email", outcome="nothing",
                              origin=cid)
    await state.mark_company_profiled(cid)
    await state.rename_company(cid, "Exemplu Clinica")
    row = _q(state, "SELECT n_contacts, n_email, n_phone, n_mobile, best_trust, n_people, "
                    "site_read_at IS NOT NULL, site_read_deep IS NOT NULL, profiled_at IS NOT NULL, "
                    "website_trust FROM firm WHERE id = ?", fid)
    assert row == [(3, 2, 1, 1, 1, 2, 1, 1, 1, 1)]
    assert _one(state, "SELECT name FROM local_firm WHERE firm = ?", fid) == "Exemplu Clinica"
    assert _one(state, "SELECT COUNT(*) FROM link WHERE person != 0") == 1
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_registered_firms_name_from_its_site_is_harveys_own(state):
    _, saved = await _pull(state, _firm(1))
    await state.rename_company(saved[0], "Exemplu Dental")
    assert _one(state, "SELECT display_name FROM firm WHERE id = ?", int(saved[0])) == \
        "Exemplu Dental"
    filled = await state.fill_company_fields(saved[0], website="https://exemplu.example.com",
                                             domain="exemplu.example.com", name="Ignored",
                                             phone="+40700000004")
    assert set(filled) == {"website", "domain", "phone"}
    assert _clean(state) == {}


# ── Web signals ──

@pytest.mark.asyncio
async def test_web_signals_keep_history_on_change_and_register_facts_are_refused(state):
    cid, _ = await _listing(state, _hit(n=21))
    fid = int(cid)

    def row(code, num=None, text="", at="2026-03-01 10:00:00", module="web_marketing"):
        return {"company_id": cid, "signal_code": code, "value_num": num, "value_text": text,
                "module": module, "capability": "profile", "observed_at": at,
                "origin_company_id": cid}

    assert await state.add_observations([row("RUNNING_GOOGLE_ADS", 1)]) == 1
    await state.add_observations([row("RUNNING_GOOGLE_ADS", 1, at="2026-03-02 10:00:00")])
    await state.add_observations([row("RUNNING_GOOGLE_ADS", 0, at="2026-03-03 10:00:00")])
    await state.add_observations([row("TECH_STACK", text="WordPress"),
                                  row("TECH_STACK", text="HubSpot")])
    # A register's fact is read from the register: refused here.
    assert await state.add_observations([row("ACTIVITY_CODE", text="8623/3",
                                             module="onrc")]) == 0
    # A phone fact is a contact whatever is stored.
    await state.add_observations([row("PHONE_NUMBER", text="+40700000005", module="phones")])
    ads = _one(state, "SELECT id FROM signal WHERE code = 'RUNNING_GOOGLE_ADS'")
    tech = _one(state, "SELECT id FROM signal WHERE code = 'TECH_STACK'")
    assert _one(state, "SELECT COUNT(*) FROM obs WHERE signal = ?", ads) == 2   # on change only
    assert _q(state, "SELECT num, checked_at >= at FROM reading WHERE firm = ? AND signal = ?",
              fid, ads) == [(0.0, 1)]
    assert _q(state, "SELECT n_keys, n_positive FROM firm_signal WHERE firm = ? AND signal = ?",
              fid, tech) == [(2, 2)]
    assert _one(state, "SELECT n_phone FROM firm WHERE id = ?", fid) == 1
    assert _clean(state) == {}


# ── Merges, undo, the review list ──

@pytest.mark.asyncio
async def test_merge_group_undo_and_separate_keep_counts(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", phone="+40700000011", n=31,
                                      domain="unu.example.com"))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", phone="+40700000012", n=32,
                                      location="Sibiu, Romania"))
    c, _ = await _listing(state, _hit(name="Trei Exemplu", n=33))
    done = await state.merge_group(b, [a, c])
    assert done["survivor"] == b and sorted(done["merged"]) == sorted([a, c])
    # The survivor had no website: it takes the merged one's (filled, undone with it).
    assert _one(state, "SELECT website FROM firm WHERE id = ?", int(b)) == "https://unu.example.com"
    # A branch somewhere else is one of the survivor's places.
    assert _one(state, "SELECT COUNT(*) FROM locations WHERE firm = ? AND source = 'merge'",
                int(b)) >= 1
    assert _one(state, "SELECT n_phone FROM firm WHERE id = ?", int(b)) == 2
    assert _clean(state) == {}
    with pytest.raises(resolver.MergeRefused) as e:
        await state.merge_group(a, [b])
    assert e.value.code == "already_merged"
    undone = await state.undo_group(a)
    assert len(undone) == 2
    assert _one(state, "SELECT COUNT(*) FROM firm WHERE live != 0") == 0
    assert _one(state, "SELECT website FROM firm WHERE id = ?", int(b)) is None
    assert _one(state, "SELECT COUNT(*) FROM locations WHERE source = 'merge'") == 0
    assert _clean(state) == {}
    await state.separate_group([a], [b, c])
    assert _one(state, "SELECT COUNT(*) FROM match_candidate WHERE status = 'separate'") == 2
    pair = await state.merge_pair(a, c)
    assert pair["merged"] in (a, c)
    with pytest.raises(resolver.MergeRefused) as e:
        await state.undo_merge(b)
    assert e.value.code == "not_merged"
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_an_undo_is_refused_while_the_survivor_is_merged(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=41))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=42))
    c, _ = await _listing(state, _hit(name="Trei Exemplu", n=43))
    await state.merge_companies(b, a)
    await state.merge_companies(c, b)
    with pytest.raises(resolver.MergeRefused) as e:
        await state.undo_merge(a)
    assert e.value.code == "survivor_merged"
    await state.undo_merge(b)
    await state.undo_merge(a)
    assert _clean(state) == {}


# ── Removing a firm from Harvey ──

@pytest.mark.asyncio
async def test_remove_deletes_a_pulled_firm_and_what_only_it_reaches(state):
    _, saved = await _pull(state, _firm(51), _firm(52, name="ALTA EXEMPLU SRL"))
    a, b = saved
    # What registers give a pulled firm (their phones) is not Harvey's work.
    own = contacts.Found("phone", "+40700000021", source="anaf", trust="confirmed")
    shared = contacts.Found("phone", "+40700000022", source="anaf", trust="confirmed")
    blocked = contacts.Found("phone", "+40700000023", source="anaf", trust="confirmed")
    await state.add_contacts(a, [own, shared, blocked])
    await state.add_contacts(b, [shared])
    con = _con(state)
    con.execute("UPDATE contact SET do_not_contact = 1 WHERE value = '+40700000023'")
    con.close()
    done = await state.remove_company(a)
    assert done == {"removed": a, "kept": "", "pull_rows_removed": 1, "contacts_removed": 1}
    assert _one(state, "SELECT COUNT(*) FROM firm WHERE id = ?", int(a)) == 0
    assert _one(state, "SELECT COUNT(*) FROM contact WHERE value = '+40700000023'") == 1
    assert _one(state, "SELECT n_firms FROM contact WHERE value = '+40700000022'") == 1
    for table in ("firm_ident", "firm_source", "local_firm", "link_evidence"):
        assert _one(state, f"SELECT COUNT(*) FROM {table} WHERE firm = ?", int(a)) == 0
    assert _clean(state) == {}
    with pytest.raises(RemoveRefused) as e:
        await state.remove_company(a)
    assert e.value.code == "not_found"


@pytest.mark.asyncio
async def test_remove_keeps_a_firm_harvey_worked_on_and_takes_only_its_pulls(state):
    _, saved = await _pull(state, _firm(53))
    a = saved[0]
    checked = contacts.Found("email", "ana@exemplu.example.com", person="Ana Exemplu",
                             role="owner", source="website")
    await state.add_contacts(a, [checked])
    await state.record_search("company", a, "website", "read_site", outcome="found", found=1,
                              cost=0.01)
    con = _con(state)
    con.execute("UPDATE contact SET status = 1 WHERE value = 'ana@exemplu.example.com'")
    con.close()
    done = await state.remove_company(a)
    assert done["removed"] == "" and done["kept"] and done["pull_rows_removed"] == 1
    # The firm, its keys, what was paid for and what was checked stay.
    assert _one(state, "SELECT COUNT(*) FROM firm WHERE id = ?", int(a)) == 1
    assert _one(state, "SELECT COUNT(*) FROM firm_ident WHERE firm = ?", int(a)) == 2
    assert _one(state, "SELECT COUNT(*) FROM search WHERE target = ?", int(a)) == 1
    assert _one(state, "SELECT COUNT(*) FROM person WHERE firm = ?", int(a)) == 1
    assert _one(state, "SELECT status FROM contact WHERE value = 'ana@exemplu.example.com'") == 1
    assert _one(state, "SELECT COUNT(*) FROM firm_source WHERE firm = ?", int(a)) == 0
    # A listing Harvey found is its own work too: it stays.
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=54))
    assert (await state.remove_company(b))["kept"]
    assert _one(state, "SELECT COUNT(*) FROM firm WHERE id = ?", int(b)) == 1
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_remove_is_refused_for_a_firm_in_a_merge_or_named_by_outreach(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=61))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=62))
    c, _ = await _listing(state, _hit(name="Trei Exemplu", n=63))
    await state.merge_companies(a, b)
    await state.undo_merge(b)
    for cid in (a, b):              # an undone merge still names them
        with pytest.raises(RemoveRefused) as e:
            await state.remove_company(cid)
        assert e.value.code == "merged"
    con = _con(state)
    con.execute("INSERT INTO prospects (id, firm, email) VALUES ('p1', ?, 'ana@example.com')",
                (int(c),))
    con.close()
    with pytest.raises(RemoveRefused) as e:
        await state.remove_company(c)
    assert e.value.code == "has_prospects"
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_old_text_ids_still_name_their_firm(state):
    cid, _ = await _listing(state, _hit(n=71))
    con = _con(state)
    con.execute("INSERT INTO legacy_id (uuid, firm) VALUES ('abcdef012345', ?)", (int(cid),))
    con.close()
    await state.mark_company_profiled("abcdef012345")
    assert _one(state, "SELECT profiled_at IS NOT NULL FROM firm WHERE id = ?", int(cid)) == 1


@pytest.mark.asyncio
async def test_a_lookup_by_identifier_keeps_phones_and_reads_no_register_facts(state):
    from harvey.modules import api

    _, saved = await _pull(state, _firm(1), _firm(2))
    rows = await state.companies_with_id("cui")
    assert sorted(r["value"] for r in rows) == ["90000001", "90000002"]
    assert {r["id"] for r in rows} == set(saved)
    one = next(r["id"] for r in rows if r["value"] == "90000001")
    async with state.batch() as batch:
        done = await ingest.checked(batch, "anaf", {one: (
            [api.Fact("VAT_REGISTERED", value_num=1, module="anaf", capability="company_check")],
            [api.Contact("phone", "+40700000031", module="anaf", capability="company_check")])},
            run_id="run-1", confirmed={"VAT_REGISTERED"})
    assert done["observations"] == 0          # ANAF's answer is read from anaf.db
    assert _one(state, "SELECT n_phone FROM firm WHERE id = ?", int(one)) == 1
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_pull_merges_a_listing_it_surely_is_inside_its_bulk_write(state):
    listing, _ = await _listing(state, _hit(name="Exemplu", n=81, domain="exemplu.example.com"))
    report, saved = await _pull(state, _firm(1, website="https://exemplu.example.com",
                                             domain="exemplu.example.com"))
    # Same name in the same town and the site the registry declares: one firm.
    assert saved == [listing] and report.known_companies == 1
    pulled = _cui_firm(state, 90000001)
    assert _one(state, "SELECT live FROM firm WHERE id = ?", pulled) == int(listing)
    assert _one(state, "SELECT n FROM tally WHERE name = 'firms_merged'") == 1
    assert _clean(state) == {}


# ── Regressions: what a review of these write paths found ──

@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["osm", "serper", "tinyfish", "scavio", "dataforseo"])
async def test_search_facts_of_every_provider_are_kept(state, module):
    # A search module that returns a fact (its capability's outputs) stores
    # it, whichever module declares the signal.
    for code in ("FOUND_IN_SERP", "NO_WEBSITE"):
        await state.set_signal_status(code, "confirmed")
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=91))
    kept = await state.add_observations([
        {"company_id": a, "signal_code": "NO_WEBSITE", "value_num": 1, "module": module,
         "collector": "discover"},
        {"company_id": a, "signal_code": "FOUND_IN_SERP", "value_num": 1,
         "value_text": f"dentist {module}", "module": module, "collector": "discover"}])
    assert kept == 2
    # A register's facts are still refused, and the stored tally counts a
    # firm once however many modules store the signal.
    assert await state.add_observations([{"company_id": a, "signal_code": "NO_WEBSITE",
                                          "value_num": 1, "module": "onrc"}]) == 0
    states = await state.read("signal_states", ["NO_WEBSITE"])
    assert states["NO_WEBSITE"]["true"] == 1
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_firm_never_gains_a_second_tax_code(state):
    _, saved = await _pull(state, _firm(1))
    fid = int(saved[0])
    assert await state.fill_company_fields(saved[0], cui="90000099") == []
    cuis = _q(state, "SELECT fi.key FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme "
                     "WHERE s.name = 'ro.cui' AND fi.firm = ?", fid)
    assert cuis == [(90000001,)]
    # Nor through a record merged into it, and the schema refuses it too.
    listing, _ = await _listing(state, _hit(name="Exemplu Doi", n=92))
    await state.merge_companies(saved[0], listing)
    assert await state.fill_company_fields(listing, cui="90000098") == []
    con = _con(state)
    try:
        scheme = con.execute("SELECT id FROM ident_scheme WHERE name = 'ro.cui'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="legal identifier"):
            con.execute("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, 0)",
                        (scheme, 90000097, fid))
    finally:
        con.close()
    # A firm with none takes its first.
    other, _ = await _listing(state, _hit(name="Exemplu Trei", n=93))
    assert await state.fill_company_fields(other, cui="90000096") == ["cui"]
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_removed_firms_id_is_never_given_again(state):
    _, saved = await _pull(state, _firm(1))
    await state.remove_company(saved[0])
    b, created = await _listing(state, _hit(name="Alta Firma", n=94))
    assert created and int(b) > int(saved[0])


@pytest.mark.asyncio
async def test_a_listings_county_is_never_its_country(state):
    a, _ = await _listing(state, _hit(n=95, location="Brașov, Romania"))
    b, _ = await _listing(state, _hit(name="Alta Exemplu", n=96, location="Sibiu, Sibiu, Romania"))
    assert _q(state, "SELECT locality, county FROM local_firm WHERE firm = ?", int(a)) == [
        ("Brașov", None)]
    assert _q(state, "SELECT locality, county FROM local_firm WHERE firm = ?", int(b)) == [
        ("Sibiu", "Sibiu")]


@pytest.mark.asyncio
async def test_two_registered_firms_without_a_tax_code_stay_apart(state):
    # The registration number is matched on, not a legal scheme (two numbers
    # can be one firm's old and current registration): two CUI-less firms
    # with the same name and phone are still not merged on their own.
    await _pull(state, _firm(1, cui=""))
    a = _one(state, "SELECT firm FROM firm_ident WHERE key = 'J08/1/2010'")
    await state.add_contacts(str(a), [contacts.Found("phone", "+40700000077", source="anaf",
                                                     trust="confirmed")])
    other = Company(name="EXEMPLU SRL", location="Brașov, Brașov", phone="+40700000077",
                    external_id="onrc:J08/2/2010", source="onrc")
    assert not (await state.match_company(other)).attach
    await state.save_company(other)
    assert _one(state, "SELECT COUNT(*) FROM firm WHERE live = 0") == 2
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_refused_merge_leaves_no_place_behind(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=97, location="Brașov, Romania"))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=98, location="Sibiu, Romania"))
    await state.fill_company_fields(a, cui="90000011")
    await state.fill_company_fields(b, cui="90000012")
    before = _one(state, "SELECT COUNT(*) FROM locations WHERE source = 'merge'")
    with pytest.raises(resolver.MergeRefused):
        await state.merge_companies(a, b)
    assert _one(state, "SELECT COUNT(*) FROM locations WHERE source = 'merge'") == before == 0
    # A merge that is made keeps the merged record's place, and undo takes it.
    c, _ = await _listing(state, _hit(name="Trei Exemplu", n=99, location="Cluj, Romania"))
    await state.merge_companies(a, c)
    assert _q(state, "SELECT firm, locality FROM locations WHERE source = 'merge'") == [
        (int(a), "Cluj")]
    await state.undo_merge(c)
    assert _one(state, "SELECT COUNT(*) FROM locations WHERE source = 'merge'") == 0
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_ids_too_long_for_an_integer_name_no_firm(state):
    with pytest.raises(RemoveRefused) as e:
        await state.remove_company("9" * 25)
    assert e.value.code == "not_found"
    assert await state.read("company", "9" * 25) is None
    assert await state.get_company("9" * 25) is None


@pytest.mark.asyncio
async def test_a_contact_many_firms_share_is_not_gathered(state, monkeypatch):
    # Five registered firms answer the same switchboard: a listing with that
    # number is none of them, and their keys aren't read to find that out.
    _, saved = await _pull(state, *[_firm(n, name=f"EXEMPLU {n} SRL") for n in range(1, 6)])
    for cid in saved:
        await state.add_contacts(cid, [contacts.Found("phone", "+40700000088", source="anaf",
                                                      trust="confirmed")])
    gathered = []
    real = resolver._overlay_keys
    monkeypatch.setattr(resolver, "_overlay_keys",
                        lambda con, schemes, live: gathered.append(live) or real(con, schemes,
                                                                                 live))
    m = await state.match_company(Company(name="Cabinet Exemplu", location="Brașov, Romania",
                                          phone="+40700000088"))
    assert not m.attach and not m.review and not m.others
    assert gathered == []


def test_a_stale_name_index_is_reported_once(caplog):
    from harvey.registers import Stale

    class StaleNames:
        module, alias = "onrc", "main"

        def name_holders(self, con, name, location):
            raise Stale("built for another load")

    con = sqlite3.connect(":memory:")
    resolver._STALE_NAMES_LOGGED.discard("onrc")
    with caplog.at_level("WARNING", logger="harvey.resolver"):
        for _ in range(3):
            assert resolver._register_name_holders(
                con, None, Company(name="Exemplu", location="Brașov"), [StaleNames()]) == set()
    assert sum("name index" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_a_pull_matches_in_a_read_and_matches_again_when_firms_changed(state):
    from harvey.overlay import store

    # Matched before a firm with the same CUI arrived: the write sees the
    # identity moved and matches again (one firm, not two).
    records = onrc.firms_draft([_firm(1)], DATASET, codes={}, classes=[]).stamp(
        "onrc", "registry_search").records
    matched_at, matches = await state.match_records(records)
    assert not matches[0].attach
    await state.save_company(Company(name="EXEMPLU SRL", cui="90000001", source="website"))
    out = await state.pull_records(records, matches=matches, matched_at=matched_at,
                                   run_id="run-x")
    assert out[0][1] is False
    assert _one(state, "SELECT COUNT(*) FROM firm") == 1
    # Two records of one chunk that share a key: the second sees the first.
    twins = onrc.firms_draft([_firm(2, website="https://doi.example.com",
                                    domain="doi.example.com"),
                              _firm(3, name="ALT EXEMPLU SRL", website="https://doi.example.com",
                                    domain="doi.example.com")],
                             DATASET, codes={}, classes=[]).stamp(
        "onrc", "registry_search").records
    matched_at, matches = await state.match_records(twins)
    out = await state.pull_records(twins, matches=matches, matched_at=matched_at, run_id="run-y")
    assert [o[1] for o in out] == [True, True] and out[1][3] is True     # a shared domain
    assert store.PULL_CHUNK <= 10_000
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_opening_the_database_takes_no_write_lock_when_nothing_changed(state):
    # A registry pull holds the write lock; a page opening the database
    # (init_db) must not wait on it when the dictionaries are current.
    import time

    from harvey.overlay import store

    assert not store.call(state.db_path, "dictionaries_stale")
    holder = _con(state)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        await StateManager(state.db_path).init_db()
        assert time.monotonic() - started < 5
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.mark.asyncio
async def test_company_and_its_history_read_on_the_overlay(state):
    for code in ("YC_BACKED",):
        await state.set_signal_status(code, "confirmed")
    a, _ = await _listing(state, _hit(name="Exemplu Startup", phone="+40700000041", n=101,
                                      domain="startup.example.com"))
    company = await state.get_company(a)
    assert (company.id, company.name, company.domain) == (a, "Exemplu Startup",
                                                         "startup.example.com")
    assert (await state.get_company_by_domain("startup.example.com")).id == a
    assert (await state.get_company_by_external_id("dataforseo:cid:101")).id == a
    # A preset search's hit (a capability that queries) stays standing.
    await state.add_observations([{"company_id": a, "signal_code": "YC_BACKED", "value_num": 1,
                                   "module": "startups", "capability": "yc_search",
                                   "collector": "discover"}])
    assert await state.search_hit(a, "YC_BACKED") is True
    assert await state.yc_batch(a) == ""
    rows = await state.get_observations(company_id=a, signal_code="YC_BACKED", latest_only=True)
    assert [(r["signal_code"], r["collector"], r["module"]) for r in rows] == [
        ("YC_BACKED", "discover", "startups")]
    # The site's own reading saying no is the latest now.
    await state.add_observations([{"company_id": a, "signal_code": "YC_BACKED", "value_num": 0,
                                   "module": "startups", "capability": "startup_claims",
                                   "collector": "profile"}])
    assert await state.search_hit(a, "YC_BACKED") is False
    # Reads schema 25 has no version of yet say so.
    from harvey.state import NotOnOverlay

    with pytest.raises(NotOnOverlay):
        await state.duplicate_groups()
