"""The contacts model (harvey/contacts.py, migration v21): one row per
distinct email or phone, linked many-to-many to companies; people, places,
sources and searches; the backfill from what an older database holds;
every source writing through ingest; merge and undo; the Contacts page's
API and its list under node. All data is fictional."""

import asyncio
import csv
import io
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import contacts
from harvey.models import Company
from harvey.models.prospect import Prospect
from harvey.modules import api, ingest
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim

WEB = Path(dash.__file__).resolve().parent / "web"


def run(coro):
    return asyncio.run(coro)


async def _state(path) -> StateManager:
    s = StateManager(str(path))
    await s.init_db()
    await sync_vocabulary(s)
    return s


def _rows(path, sql, args=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


# ── Migration and backfill ──


def test_a_merge_moves_no_contact_and_undo_leaves_each_with_its_record(tmp_path):
    """Rows stay with the record they were found for: an undo gives each
    record back exactly what it had."""
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        slim.describe(s, slim.firm("EXEMPLU PATRU SRL", 90000004, "Cluj", county="Cluj"))
        a = await s.add_company(Company(name="Exemplu Patru", domain="patru.example",
                                        source="discover:osm", location="Cluj, Cluj, Romania"))
        b = await s.add_company(Company(name="EXEMPLU PATRU SRL", cui="90000004",
                                        source="registry:onrc", phone="0264 000 105",
                                        location="Cluj, Cluj, Romania"))
        await s.merge_companies(a, b)
        merged = (_rows(db, "SELECT firm FROM link"),
                  [c["value"] for c in (await s.read("of_company", a))["contacts"]])
        await s.undo_merge(b)
        return a, b, merged

    a, b, (links, shown) = run(go())
    assert links == [(int(b),)] and shown == ["+40264000105"]
    assert _rows(db, "SELECT firm FROM link") == [(int(b),)]
    assert {r[0] for r in _rows(db, "SELECT firm FROM firm_source")} == {int(a), int(b)}


def test_contact_kinds_are_declared_by_modules(tmp_path):
    db = tmp_path / "harvey.db"
    s = run(_state(db))
    kinds = dict(_rows(db, "SELECT kind, module FROM contact_kinds"))
    assert kinds["phone"] == "phones" and kinds["email"] == "website"
    assert {"contact_form", "facebook", "instagram", "linkedin", "whatsapp"} <= set(kinds)

    async def undeclared():
        cid = await s.add_company(Company(name="Exemplu Cinci", domain="cinci.example"))
        return await s.add_contacts(cid, [contacts.Found("pigeon", "coop 7"),
                                          contacts.Found("email", "Info@Cinci.example")])

    assert run(undeclared()) == 1                       # the undeclared kind isn't kept
    assert _rows(db, "SELECT k.kind, c.value FROM contact c JOIN contact_kinds k "
                     "ON k.id = c.kind") == [("email", "info@cinci.example")]


# ── Every source writes through ingest ──


def test_a_listing_matching_a_company_by_phone_is_one_of_its_places(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        slim.describe(s, slim.firm("FARMACIA EXEMPLU SRL", 90000106, website="https://farmacie.example",
                                   domain="farmacie.example"))
        cid = await s.add_company(Company(name="Farmacia Exemplu", domain="farmacie.example",
                                          phone="+40268000106", source="registry:onrc",
                                          cui="90000106", location="Brașov, Brașov, Romania"))
        saved = await ingest.listing(s, api.Record(
            name="Farmacia Exemplu", phone="+40268000106", website="https://farmacie.example",
            address="Str. Exemplului 9", location="Brașov, Brașov, Romania",
            external_id="osm:node/77", source="discover:osm", module="osm", capability="places"))
        return cid, saved, await s.read("of_company", cid)

    cid, saved, page = run(go())
    assert saved.company_id == cid and not saved.created
    assert [(p["name"], p["address"], p["external_id"], p["phone"], p["source"])
            for p in page["locations"]] == \
        [("Farmacia Exemplu", "Str. Exemplului 9", "osm:node/77", "+40268000106", "osm")]
    assert ("discover:osm", "osm:node/77") in {(x["source"], x["ref"]) for x in page["sources"]}
    (link,) = _rows(db, f"SELECT location FROM {slim.EVIDENCE} "
                        "WHERE source_name = 'osm'")
    assert link[0] != 0


def test_a_chain_sharing_one_phone_and_email_is_one_contact_each(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        ids = [await s.add_company(Company(name=f"LANT EXEMPLU {i} SRL", cui=f"9100000{i}",
                                           location="Iași, Iași, Romania", source="registry:onrc"))
               for i in range(5)]
        for cid in ids:
            await ingest.add_contacts(s, cid, [
                api.Contact("phone", "+40232000107", module="anaf", capability="check"),
                api.Contact("email", "Contact@Lant.example", module="website")])
        return ids

    run(go())
    assert _rows(db, f"SELECT kind_name, COUNT(*), MAX(n_firms) FROM {slim.CONTACTS} "
                     "GROUP BY kind_name ORDER BY kind_name") == [("email", 1, 5), ("phone", 1, 5)]
    assert _rows(db, "SELECT COUNT(*) FROM link")[0][0] == 10


def test_registry_administrators_anaf_phones_site_reads_and_prospects(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        await s.set_signal_status("CONTACT_FOUND", "confirmed")
        # The registry's administrators are read from it (never copied).
        slim.describe(s, slim.firm("EXEMPLU SASE SRL", 90000006, domain="sase.example"),
                      people=[("J08/90000006/2010", "EXEMPLU MARIA", "administrator")])
        cid = await s.add_company(Company(name="EXEMPLU SASE SRL", cui="90000006",
                                          domain="sase.example", source="registry:onrc",
                                          location="Brașov, Brașov, Romania"))
        # ANAF's phone, on a batch, is the main phone.
        async with s.batch() as batch:
            await ingest.company_phones(batch, cid, [
                api.Contact("phone", "+40268000108", display="0268 000 108", module="anaf")])
        # A site read: its contact form and a social profile it links to.
        await ingest.add_contacts(s, cid, [
            api.Contact("contact_form", "https://sase.example/contact", module="website"),
            api.Contact("facebook", "https://facebook.com/exemplusase", module="website")])
        # A prospect: its email and person mirrored by the triggers.
        await ingest.prospect(s, Prospect(company_id=cid, first_name="Maria", last_name="Exemplu",
                                          email="Maria@sase.example", email_status="verified",
                                          phone="0722 000 109", source="company_website"))
        return s, cid

    s, cid = run(go())
    company = run(s.get_company(cid))
    assert company.phone in ("0268 000 108", "+40268000108")
    kinds = dict(_rows(db, f"SELECT value, kind_name FROM {slim.CONTACTS}"))
    assert kinds == {"+40268000108": "phone", "https://sase.example/contact": "contact_form",
                     "https://facebook.com/exemplusase": "facebook",
                     "maria@sase.example": "email", "+40722000109": "phone"}
    assert run(slim.register_values(s, "CONTACT_FOUND")) == ["Maria Exemplu — Administrator"]
    assert _rows(db, "SELECT name FROM person") == [("Maria Exemplu",)]
    through = _rows(db, "SELECT ct.value, l.trust FROM link l JOIN contact ct "
                        "ON ct.id = l.contact WHERE l.person != 0 ORDER BY 1")
    assert through == [("+40722000109", 1), ("maria@sase.example", 0)]
    assert ("anaf",) in _rows(db, f"SELECT source_name FROM {slim.EVIDENCE}")


def test_an_opt_out_marks_the_contact_do_not_contact_and_its_undo_clears_it(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="Exemplu Sapte", domain="sapte.example"))
        pid = await s.add_prospect(Prospect(company_id=cid, email="ion@sapte.example"))
        await s.update_prospect_status(pid, "opted_out")
        first = _rows(db, "SELECT do_not_contact FROM contact")
        await s.undo_opt_out(pid)
        return first

    assert run(go()) == [(1,)]
    assert _rows(db, "SELECT do_not_contact FROM contact") == [(0,)]


# ── Merge and undo ──


def test_a_merge_shows_each_contact_once_and_undo_gives_each_record_its_own(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        slim.describe(s, slim.firm("EXEMPLU OPT SRL", 90000008, "Arad"))
        a = await s.add_company(Company(name="EXEMPLU OPT SRL", cui="90000008",
                                        source="registry:onrc", location="Arad, Arad, Romania"))
        b = await s.add_company(Company(name="Exemplu Opt", domain="opt.example",
                                        source="discover:osm", location="Arad, Arad, Romania"))
        for cid in (a, b):
            await s.add_contacts(cid, [contacts.Found("phone", "+40257000110", source="x",
                                                      person="Dan Exemplu")])
        await s.add_contacts(b, [contacts.Found("email", "dan@opt.example", source="website",
                                                person="Dan Exemplu")])
        await s.record_search("company", b, "people", "find_people", outcome="nothing")
        before = slim.snapshot(s)
        await s.merge_companies(a, b)
        page = await s.read("of_company", a)
        after = {
            "visible": sorted(c["value"] for c in page["contacts"]),
            "people": [p["name"] for p in page["people"]],
            "searched": [c["id"] for c in await s.companies_needing_prospects(limit=10)],
            "searches": len(page["searches"]),
        }
        await s.undo_merge(b)
        return s, a, b, before, after

    s, a, b, before, after = run(go())
    # The survivor reaches both contacts, each once, and one Dan (his two
    # records' rows stay where they were found).
    assert after["visible"] == ["+40257000110", "dan@opt.example"]
    assert after["people"] == ["Dan Exemplu"]
    assert after["searched"] == [] and after["searches"] == 1
    assert slim.snapshot(s) == before
    assert slim.recount_diff(s) == {}
    # Each person at its own record, each link through its own person.
    assert _rows(db, "SELECT COUNT(*) FROM link l JOIN person p "
                     "ON p.id = l.person WHERE p.firm != l.firm")[0][0] == 0


# ── Searches: once per source, unless forced or stale ──


def test_a_company_searched_in_one_source_is_skipped_there_only(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="Exemplu Noua", domain="noua.example"))
        out = {"fresh": await s.companies_needing_prospects()}
        await s.record_search("company", cid, "people", "find_people", outcome="nothing")
        out["people"] = await s.companies_needing_prospects()
        out["hunter"] = await s.companies_needing_prospects(module="hunter",
                                                            capability="domain_search")
        await s.record_search("company", cid, "hunter", "domain_search", outcome="error")
        out["after_error"] = await s.companies_needing_prospects(module="hunter",
                                                                 capability="domain_search")
        monkeypatch.setattr(contacts, "refresh_days", lambda m, c: 30)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE search SET at = at - 31 * 86400")
        out["stale"] = await s.companies_needing_prospects()
        return cid, out

    cid, out = run(go())
    ids = {k: [c["id"] for c in v] for k, v in out.items()}
    assert ids["fresh"] == [cid] and ids["people"] == []
    assert ids["hunter"] == [cid]                       # another source still searches it
    assert ids["after_error"] == [cid]                  # an error isn't a search done
    assert ids["stale"] == [cid]                        # older than refresh_days


def test_force_searches_again_what_was_searched_before_the_run(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="Exemplu Zece", domain="zece.example"))
        await s.record_search("company", cid, "people", "find_people", outcome="nothing")
        with sqlite3.connect(db) as conn:          # yesterday
            conn.execute("UPDATE search SET at = at - 86400")
        since = contacts.now()
        before = await s.companies_needing_prospects(since=since)
        await s.record_search("company", cid, "people", "find_people", outcome="nothing")
        again = await s.companies_needing_prospects(since=since)
        return cid, before, again

    cid, before, again = run(go())
    assert [c["id"] for c in before] == [cid]           # searched yesterday: again
    assert again == []                                  # searched in this run: not twice


def test_manifest_refresh_days_must_be_whole_days():
    from harvey.modules.loader import _check_capabilities

    problems = []
    _check_capabilities([{"id": "x", "op": "read", "refresh_days": "soon"}], "m", problems)
    assert problems and "refresh_days" in problems[0]
    problems = []
    _check_capabilities([{"id": "x", "op": "read", "refresh_days": 30}], "m", problems)
    assert problems == []


# ── The Contacts page API ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def _seed(db) -> dict:
    async def go():
        s = await _state(db)
        ids = {}
        for i, (town, site) in enumerate([("Brașov", True), ("Brașov", False), ("Sibiu", True)]):
            # Registered firms: the registry beside the database describes them.
            slim.describe(s, slim.firm(f"EXEMPLU {i} SRL", f"9200000{i}", town,
                                       domain=f"ex{i}.example" if site else ""))
            ids[i] = await s.add_company(Company(
                name=f"EXEMPLU {i} SRL", cui=f"9200000{i}",
                domain=f"ex{i}.example" if site else "", location=f"{town}, {town}, Romania",
                source="registry:onrc"))
        # A chain phone shared by all three; a mobile at the first; emails.
        for cid in ids.values():
            await s.add_contacts(cid, [contacts.Found("phone", "+40268000111", source="anaf")])
        await s.add_contacts(ids[0], [
            contacts.Found("phone", "+40722000112", source="website", person="Ana Exemplu"),
            contacts.Found("email", "ana@ex0.example", status="verified", source="website",
                           person="Ana Exemplu")])
        await s.add_contacts(ids[2], [
            contacts.Found("email", "info@ex2.example", source="serper", confidence=0.4)])
        await s.add_people(ids[2], [("Ion Exemplu", "administrator")], source="onrc")
        await s.record_search("company", ids[2], "people", "find_people", outcome="nothing")
        return ids

    return run(go())


def test_one_row_per_contact_with_how_many_companies_it_reaches(client):
    ids = _seed(client.db_path)
    res = client.get("/api/contacts", params={"sort": "companies", "dir": "desc"}).json()
    assert res["total"] == 4 and res["more"] is False
    top = res["rows"][0]
    assert top["value"] == "+40268000111" and top["companies"] == 3
    assert top["line_type"] == "fixed" and top["sources"] == ["anaf"]
    reach = client.get(f"/api/contacts/{top['id']}").json()["links"]
    assert {l["company_id"] for l in reach} == set(ids.values())
    unsure = next(r for r in res["rows"] if r["value"] == "info@ex2.example")
    assert unsure["trust"] == "unconfirmed" and unsure["status"] == "unchecked"


@pytest.mark.parametrize("params,want", [
    ({"kind": "email"}, {"ana@ex0.example", "info@ex2.example"}),
    ({"kind": "mobile"}, {"+40722000112"}),
    ({"status": "verified"}, {"ana@ex0.example"}),
    ({"trust": "unconfirmed"}, {"info@ex2.example"}),
    ({"person": "yes"}, {"+40722000112", "ana@ex0.example"}),
    ({"source": "serper"}, {"info@ex2.example"}),
    ({"locality": "Sibiu"}, {"+40268000111", "info@ex2.example"}),
    ({"county": "Brașov", "kind": "email"}, {"ana@ex0.example"}),
    ({"website": "no"}, {"+40268000111"}),
    ({"q": "0722 000"}, {"+40722000112"}),
    ({"q": "exemplu 0"}, {"+40268000111", "+40722000112", "ana@ex0.example"}),
])
def test_filters(client, params, want):
    _seed(client.db_path)
    rows = client.get("/api/contacts", params=params).json()["rows"]
    assert {r["value"] for r in rows} == want


def test_signal_filters_reuse_the_cohort_rules(client):
    ids = _seed(client.db_path)

    async def tag():
        s = StateManager(str(client.db_path))
        await s.set_signal_status("CONTACT_FORM_URL", "confirmed")
        await s.add_observations([{"company_id": ids[2], "signal_code": "CONTACT_FORM_URL",
                                   "value_text": "https://ex2.example/contact"}])
    run(tag())
    res = client.get("/api/contacts", params={"require": "CONTACT_FORM_URL"}).json()
    assert {r["value"] for r in res["rows"]} == \
        {"+40268000111", "info@ex2.example", "https://ex2.example/contact"}
    # Excluding: only companies the cohort rules call a match (one never
    # checked for the signal is "possible", so it isn't listed).
    res = client.get("/api/contacts", params={"exclude": "CONTACT_FORM_URL"}).json()
    assert "https://ex2.example/contact" not in {r["value"] for r in res["rows"]}
    # A signal of a module that is off is refused, as on the cohort builder.
    assert client.get("/api/contacts", params={"require": "RUNNING_GOOGLE_ADS"}).status_code == 400


def test_sort_paging_and_capped_count(client, monkeypatch):
    _seed(client.db_path)
    page = client.get("/api/contacts", params={"limit": 2, "offset": 2, "sort": "value",
                                               "dir": "asc"}).json()
    assert [r["value"] for r in page["rows"]] == ["ana@ex0.example", "info@ex2.example"]
    # An unknown column sorts by the default, never into the SQL.
    assert client.get("/api/contacts", params={"sort": "value; DROP TABLE contacts"}).json()[
        "total"] == 4
    monkeypatch.setattr(contacts, "COUNT_CAP", 3)
    capped = client.get("/api/contacts").json()
    assert capped["total"] == 3 and capped["more"] is True


def test_people_still_to_search(client):
    ids = _seed(client.db_path)
    res = client.get("/api/contacts", params={"view": "people"}).json()
    assert [(r["name"], r["company_id"]) for r in res["rows"]] == [("Ion Exemplu", ids[2])]
    assert res["rows"][0]["searched_at"] is None


def test_export_once_per_contact_leaving_do_not_contact_out(client):
    ids = _seed(client.db_path)
    with sqlite3.connect(client.db_path) as conn:
        conn.execute("UPDATE contact SET do_not_contact = 1 WHERE value = 'info@ex2.example'")
    r = client.get("/api/export/contacts.csv", params={"kind": "phone"})
    rows = list(csv.DictReader(io.StringIO(r.text.removeprefix("﻿"))))
    assert [x["value"] for x in rows].count("+40268000111") == 1
    chain = next(x for x in rows if x["value"] == "+40268000111")
    assert chain["companies"] == "3"
    data = client.get("/api/export/contacts.json").json()
    assert "info@ex2.example" not in {x["value"] for x in data}


def test_company_drawer_shows_contacts_people_places_sources_and_searches(client):
    ids = _seed(client.db_path)
    reach = client.get(f"/api/companies/{ids[2]}").json()["reach"]
    assert {c["value"] for c in reach["contacts"]} == {"+40268000111", "info@ex2.example"}
    chain = next(c for c in reach["contacts"] if c["value"] == "+40268000111")
    assert chain["shared_with"] == 2
    assert reach["people"][0]["name"] == "Ion Exemplu" and reach["people"][0]["reached"] is False
    assert reach["sources"][0]["source"] == "registry:onrc"
    assert [(x["module"], x["outcome"]) for x in reach["searches"]] == [("people", "nothing")]


def test_find_contacts_queues_one_job_per_company_with_or_without_force(client):
    _seed(client.db_path)
    r = client.post("/api/contacts/find", json={"force": True, "limit": 50})
    assert r.status_code == 200 and r.json()["force"] is True and r.json()["queued"] >= 1
    # Every company is waiting already: nothing new to queue.
    assert client.post("/api/contacts/find", json={"force": True}).status_code == 409
    found = _rows(client.db_path, "SELECT params_json FROM jobs WHERE kind = 'find_contacts'")
    params = [json.loads(p[0]) for p in found]
    assert all(p["company_id"] and p["since"] for p in params)


# ── The list under node ──


def test_contacts_list_renders_a_shared_contact_once_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app = WEB / "app.js"
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: '', value: '', id});
global.document = {getElementById: id => (id === 'cl-rows' || id === 'cl-actions' || id === 'cl-filters'
  || id === 'cl-search' || id === 'ct-body' || id === 'cl-finder') ? el(id) : null};
els['cl-finder'] = {innerHTML: '', id: 'cl-finder', dataset: {}};
global.fmtN = n => String(n);
global.fmtUsd = (n, d) => '$' + Number(n).toFixed(d);
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => JSON.stringify(s);
global.icon = n => '<i>' + n + '</i>';
global.relTime = s => String(s);
global.badge = s => '<b>' + s + '</b>';
global.initials = () => 'AE';
global.roleText = s => s;
global.telHref = v => 'tel:' + v; global.mailHref = v => 'mailto:' + v; global.safeHref = v => v;
global.menuHtml = (id, items) => items.map(i => i.label || '').join('|');
global.loadPageSize = () => 50;
global.offlineState = () => 'offline';
global.emptyState = (i, a, b) => a + b;
global.tServer = (k, d) => d;
global.searchPh = () => 'cl.search_ph';
global.own = (o, k) => Object.prototype.hasOwnProperty.call(o, k) ? o[k] : undefined;
global.statusMeta = s => ({label: s});
global._modules = {email: false};
let calls = [];
global.api = async (path, opts) => { calls.push([path, opts && opts.body]);
  if (path === '/api/contacts/find/estimate') return {companies: 2, with_site: 1, to_look_up: 1,
    estimated_cost: 0.001, most: 0.002, searchers: [{provider: 'serper'}]};
  if (path === '/api/contacts/find') return {success: true, queued: 2, force: true};
  return path.startsWith('/api/signals') ? {groups: []} : {
  total: 120000, more: true, sources: ['anaf'], kinds: [{kind: 'phone', label: 'Phone'}], financials: false,
  rows: [{id: 7, kind: 'phone', value: '+40268000111', display: '+40 268 000 111', line_type: 'fixed',
          status: 'valid', trust: 'confirmed', companies: 3, people: 0, sources: ['anaf'],
          company: {id: 'c1', name: 'EXEMPLU <0> SRL', location: 'Brașov'}, first_seen: 'x'}]}; };
global.showToast = (m, kind) => calls.push(['toast', m, kind]);
global.failMsg = (d, f) => f;
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('const SOURCE_LABELS', 'const AGENT_ICONS'));
vm.runInThisContext(pick('const PAGE_SIZES', 'function savePageSize('));
vm.runInThisContext(pick('let _cv = ', '// ── Contacts: same pattern as Companies'));
(async () => {
  _cv.view = 'contacts';
  await loadContactList();
  const rows = els['cl-rows'].innerHTML, actions = els['cl-actions'].innerHTML;
  _cv.force = true;
  await findContacts();
  const card = els['cl-finder'].innerHTML;
  await runFinder({disabled: false});
  console.log(JSON.stringify({rows, actions, calls, card,
    want: {companies: t('kpi.fact_companies', {n: 3}), more: t('page.range_more', {from: 1, to: 50, total: 120000}),
           find: t('cl.find'), force: t('cl.find_force'), queued: t('cl.find_queued_force'),
           summary: t('finder.summary', {n: '2', site: '1', look: '1'})}}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    out = subprocess.run([node, "-e", script, str(app), str(WEB / "i18n.js")], text=True,
                         encoding="utf-8", capture_output=True, check=True, timeout=30)
    data = json.loads(out.stdout)
    rows, want = data["rows"], data["want"]
    assert rows.count("+40 268 000 111") == 1           # one row for the shared phone
    assert want["companies"] in rows and "toggleContactReach(0)" in rows
    assert "EXEMPLU &lt;0> SRL" in rows                 # values are escaped
    assert want["more"] in rows and "pageContactList('page'" in rows
    assert want["find"] in data["actions"] and want["force"] in data["actions"]
    # Find contacts prices the page's filter first, then runs it under the cap.
    priced = [c for c in data["calls"] if c[0] == "/api/contacts/find/estimate"]
    assert priced and json.loads(priced[0][1]) == {"filter": {}, "force": True}
    assert want["summary"] in data["card"] and "finder-cap" in data["card"]
    posted = [c for c in data["calls"] if c[0] == "/api/contacts/find"]
    assert posted and json.loads(posted[0][1]) == {"filter": {}, "force": True, "max_spend": 0}
    assert any(c[0] == "toast" and c[2] == "success" and c[1].startswith(want["queued"])
               for c in data["calls"])


# ── Each source of a contact, and what an export never lets out ──


def test_a_second_source_of_the_same_contact_is_kept(client):
    async def go():
        s = await _state(client.db_path)
        cid = await s.add_company(Company(name="EPS EXAMPLE SRL", cui="92000077",
                                          location="Arad, Arad, Romania", source="registry:onrc"))
        await s.add_contacts(cid, [contacts.Found("phone", "+40257000111", source="website",
                                                  trust="likely")])
        await s.add_contacts(cid, [contacts.Found("phone", "+40257000111", source="anaf")])
        return cid

    cid = run(go())
    assert _rows(client.db_path, f"SELECT source_name, trust_name FROM {slim.EVIDENCE} "
                                 "ORDER BY source_name") == [("anaf", "confirmed"), ("website", "likely")]
    rows = client.get("/api/contacts", params={"source": "anaf"}).json()["rows"]
    assert [(r["value"], r["trust"], r["sources"]) for r in rows] == [
        ("+40257000111", "confirmed", ["anaf", "website"])]
    # The company's panel shows it once, with both sources.
    (phone,) = client.get(f"/api/companies/{cid}").json()["reach"]["contacts"]
    assert phone["sources"] == ["anaf", "website"] and phone["trust"] == "confirmed"


def test_a_do_not_contact_company_has_no_contact_in_the_export(client, monkeypatch):
    _seed(client.db_path)
    import harvey.export as export
    monkeypatch.setattr(export, "_do_not_contact", lambda config: ["ex0.example"])
    r = client.get("/api/export/contacts.csv")
    values = {x["value"] for x in csv.DictReader(io.StringIO(r.text.removeprefix("﻿")))}
    # The mobile at ex0.example and the chain phone it shares are left out;
    # the other companies' contacts stay.
    assert values == {"info@ex2.example"}


def test_an_unreadable_do_not_contact_list_refuses_the_export(client, monkeypatch):
    _seed(client.db_path)
    import harvey.export as export
    from harvey.config import ConfigError

    def broken(config):
        raise ConfigError("compliance.do_not_contact couldn't be read")

    monkeypatch.setattr(export, "_do_not_contact", broken)
    for path in ("/api/export/contacts.csv", "/api/export/contacts.json"):
        r = client.get(path)
        assert r.status_code == 409 and "do_not_contact" in r.json()["error"]
    # The page itself still lists them (it marks, it doesn't send).
    assert client.get("/api/contacts").status_code == 200


def test_registry_administrators_are_people_to_search_whatever_is_confirmed(tmp_path):
    from harvey.collectors import onrc

    db = tmp_path / "harvey.db"
    dataset = onrc.Dataset("firme-01-01-2026", "2026-01-01", {})
    firm = onrc.Firm(reg="J08/1/2010", name="EXEMPLU SRL", cui="90000001", form="SRL",
                     county="Brașov", locality="Brașov", address="Str. Exemplu nr. 1",
                     website="", domain="", registered="2010-01-01")

    async def go():
        s = await _state(db)
        await slim.pull(s, firm, dataset=dataset,
                        people=[(firm.reg, "POPESCU ION", "administrator")])
        return s, await s.read("people_page", contacts.ContactFilter(), "first_seen", "desc",
                               50, 0)

    s, (total, more, rows) = run(go())
    # Named by the registry, read in place (nothing copied): a person to
    # search whether or not CONTACT_FOUND is confirmed.
    assert [(r["name"], r["role"]) for r in rows] == [("Ion Popescu", "Administrator")]
    assert _rows(db, "SELECT COUNT(*) FROM person") == [(0,)]
    assert _rows(db, "SELECT COUNT(*) FROM obs") == [(0,)]


def test_a_start_waits_for_another_process_creating_the_database(tmp_path):
    import threading

    import harvey.state as state_mod
    from harvey.overlay import schema

    path = tmp_path / "harvey.db"
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("BEGIN IMMEDIATE")        # another process is creating it

    def finish():
        schema.create(other)
        other.execute("COMMIT")
        other.close()

    timer = threading.Timer(0.3, finish)
    timer.start()
    try:
        assert state_mod._open_or_create(str(path), schema) == schema.VERSION
    finally:
        timer.join()
    assert _rows(path, "SELECT COUNT(*) FROM tally") == [(len(schema.TALLIES),)]
    assert state_mod.MIGRATION_TIMEOUT_SECONDS > state_mod.BUSY_TIMEOUT_SECONDS


def test_a_failed_creation_raises_at_once_and_leaves_the_file_empty(tmp_path, monkeypatch):
    import harvey.state as state_mod
    from harvey.overlay import schema

    path = tmp_path / "harvey.db"
    broken = schema.STEPS[:-1] + ((schema.STEPS[-1][0], lambda con: con.execute(
        "CREATE TABLE firm (id INTEGER)")),)     # fails part-way: firm exists
    monkeypatch.setattr(schema, "STEPS", broken)
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: pytest.fail("waited"))
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        state_mod._open_or_create(str(path), schema)
    assert time.monotonic() - started < state_mod.BUSY_TIMEOUT_SECONDS
    # Nothing half-made is left behind: the next start creates the schema.
    assert _rows(path, "SELECT COUNT(*) FROM sqlite_master") == [(0,)]
    monkeypatch.undo()
    assert state_mod._open_or_create(str(path), schema) == schema.VERSION


# ── The Email module's campaigns: contacts the Writer can write to ──


def _audience_db(db, monkeypatch, status="verified"):
    """A registry firm whose administrator the finder reached with an
    address, a second firm's unconfirmed address, and an opted-out one."""
    checked = []

    async def verify(inp, ctx):
        checked.append(inp.extra["email"])
        return api.Draft(contacts=[api.Contact("email", inp.extra["email"], status=status)])

    real = api.capability
    monkeypatch.setattr(api, "capability", lambda m, c, man=None: api.Capability(
        m, c, verify) if (m, c) == ("email_verify", "verify_address") else real(m, c, man))

    async def go():
        s = await _state(db)
        alfa = await s.add_company(Company(name="ALFA EXEMPLU SRL", domain="alfa.example",
                                           source="registry:onrc"))
        beta = await s.add_company(Company(name="BETA EXEMPLU SRL", source="registry:onrc"))
        await s.add_people(alfa, [("POPESCU ION", "administrator")], source="onrc")
        await s.add_contacts(alfa, [
            contacts.Found("email", "ion.popescu@alfa.example", status="verified",
                           person="POPESCU ION", source="email_verify", trust="confirmed"),
            contacts.Found("email", "office@alfa.example", source="website", trust="confirmed"),
            contacts.Found("email", "gone@alfa.example", status="verified", source="website")])
        await s.add_contacts(beta, [contacts.Found("email", "info@beta.example", status="verified",
                                                   source="serper", trust="unconfirmed")])
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE contact SET do_not_contact = 1 WHERE value = 'gone@alfa.example'")
        return s, alfa

    s, alfa = run(go())
    return s, alfa, checked


async def _campaign_takes(s):
    """A campaign over every email contact, started: its members and their
    prospect cards, then a cycle's checks of the addresses nobody verified."""
    from harvey import campaigns
    from harvey.agents.campaigner import CampaignCycle
    from tests.test_campaigns import config, new_campaign

    cfg = config()
    cid, counts = await new_campaign(s, cfg, query="kind=email")
    active = await s.campaigns_call(campaigns.list_campaigns, ["active"], write=False)
    cyc = CampaignCycle(s, cfg, {}, None, None)
    await cyc._verify(active)
    await cyc._verify(active)            # checked once: no longer waiting
    return cid, counts


def test_a_campaign_makes_prospect_cards_of_the_contacts_it_can_write_to(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    s, alfa, checked = _audience_db(db, monkeypatch)

    async def go():
        cid, counts = await _campaign_takes(s)
        members = {m["email"]: m["status_name"] for m in await s.campaign_members(cid)}
        return counts, members, await s.get_prospects_by_status("new")

    counts, members, new = run(go())
    # The unconfirmed and the opted-out addresses are left out.
    assert counts["added"] == 2 and (counts["untrusted"], counts["suppressed"]) == (1, 1)
    assert checked == ["office@alfa.example"]
    assert members == {"ion.popescu@alfa.example": "ready", "office@alfa.example": "ready"}
    by_email = {p.email: p for p in new}
    assert set(by_email) == {"ion.popescu@alfa.example", "office@alfa.example"}
    ion = by_email["ion.popescu@alfa.example"]
    assert (ion.first_name, ion.last_name, ion.title, ion.company_id) == (
        "Ion", "Popescu", "administrator", alfa)
    inbox = by_email["office@alfa.example"]
    assert inbox.is_company_inbox and inbox.email_status == "verified"
    # The prospect is the registry's person, not a second one.
    assert _rows(db, "SELECT name FROM person") == [("POPESCU ION",)]


def test_an_address_that_checks_out_bad_is_never_written_to(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    s, alfa, checked = _audience_db(db, monkeypatch, status="invalid")

    async def go():
        cid, _ = await _campaign_takes(s)
        return await s.campaign_member(cid, "office@alfa.example")

    member = run(go())
    assert (member["status_name"], member["end_reason"]) == ("skipped", "email status 'invalid'")
    # Checked once: graded invalid, it stays a contact and isn't asked again.
    assert checked == ["office@alfa.example"]
    from harvey.overlay import schema

    assert _rows(db, "SELECT status FROM contact WHERE value = 'office@alfa.example'") == [
        (schema.CONTACT_STATUS["invalid"],)]
