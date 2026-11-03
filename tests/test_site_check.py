"""Checking the websites a search found before (harvey/site_check.py, the
overlay store's reject/accept/undo), the classifier's test over a labelled
set, and their pages: the Settings card, Today, the company panel. Every
site, search and model is a fake; all data is fictional."""

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import classify, contacts, site_check
from harvey.models import Company
from harvey.modules import api, lanes, switches
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim

SITE = "exemplu-tours.test"


@pytest.fixture(autouse=True)
def _fresh_prices(monkeypatch):
    # (The price list and the account box are kept in the process.)
    from harvey import classify_prices

    monkeypatch.setattr(classify_prices, "_kept", (0.0, []))
    monkeypatch.setattr(classify_prices, "_failed_at", 0.0)
    monkeypatch.setattr(classify_prices, "_account", {})
    monkeypatch.setattr(classify_prices, "_lock", None)


def run(coro):
    return asyncio.run(coro)


async def _state(path) -> StateManager:
    s = StateManager(str(path))
    await s.init_db()
    await sync_vocabulary(s)
    return s


def _rows(db, sql, *args):
    with sqlite3.connect(db) as con:
        return con.execute(sql, args).fetchall()


def _found(kind, value, source, *, url=f"https://{SITE}/contact", cap="read_site",
           trust="unconfirmed", person=""):
    return contacts.Found(kind, value, source=source, capability=cap, evidence_url=url,
                          trust=trust, person=person)


async def _firm_with_found_site(s, *, trust="unconfirmed", name="EXEMPLU CONSTRUCTII SRL",
                                cui=90000017, website=f"https://{SITE}/"):
    slim.describe(s, slim.firm(name, cui, "Brașov", address="Str. Florilor nr. 12"))
    cid = await s.add_company(Company(name=name, cui=str(cui), location="Brașov, Brașov, Romania",
                                      source="registry:onrc"))
    await s.set_found_website(cid, website, trust=trust, source="tinyfish")
    await s.record_search("company", cid, "tinyfish", "web_search", looked_for="website",
                          outcome="found", found=1, detail=SITE)
    return cid


async def _wrong_site_left_behind(s, cid):
    """What a read of the wrong site left, beside an official phone."""
    await s.add_contacts(cid, [
        _found("email", "office@exemplu-tours.test", "website"),
        # (Linked at confirmed by the second read that found the first's phone.)
        _found("phone", "+40268000666", "phones", cap="site_phones",
               url=f"https://www.{SITE}/", trust="confirmed"),
        _found("phone", "+40268000111", "phones", cap="site_phones"),
        contacts.Found("phone", "+40268000111", source="mf_taxpayers", trust="confirmed"),
        _found("email", "rezervari@exemplu-tours.test", "website"),
        _found("email", "info@exemplu-tours.test", "website"),
        contacts.Found("facebook", "https://facebook.com/exemplutours", source="website",
                       capability="read_site", trust="unconfirmed"),
    ])
    await s.add_people(cid, [("Ion Exemplu", "Director")], source="people")
    await s.add_people(cid, [("POPESCU ANA", "administrator")], source="onrc")
    with sqlite3.connect(s.db_path) as con:
        # One address on the do-not-contact list, one a campaign named.
        con.execute("INSERT INTO suppression (scope, value, reason, at, by) "
                    "VALUES (1, 'rezervari@exemplu-tours.test', 4, 1, 'test')")
        con.execute("INSERT INTO campaign_member (campaign, email, status, added_by, added_at) "
                    "VALUES ('c1', 'info@exemplu-tours.test', 0, 3, 1)")


def _links(db, cid):
    return sorted((k, v, t) for k, v, _, t, *_ in slim.links(str(db), cid))


# ── Taking a wrong site off, and putting it back ──


def test_a_rejected_site_takes_off_exactly_what_its_read_left(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await _wrong_site_left_behind(s, cid)
        before = (_links(db, cid),)
        done = await s.reject_found_site(cid, batch="check-1", detail="not exemplu-tours.test")
        after = _links(db, cid)
        diff = slim.recount_diff(s)
        gone = {
            "values": {r[0] for r in _rows(db, "SELECT value FROM contact")},
            "people": [r[0] for r in _rows(db, "SELECT name FROM person")],
            "keys": _rows(db, "SELECT value FROM firm_key WHERE value LIKE '%exemplu-tours%'"),
            "firm": _rows(db, "SELECT website, website_trust, website_source FROM firm "
                              "WHERE id = ?", int(cid)),
            "searches": [(m, c, o) for m, c, o in _rows(
                db, "SELECT m.name, c.name, s.outcome FROM search s JOIN module m "
                    "ON m.id = s.module JOIN capability c ON c.id = s.capability")]}
        undone = await s.undo_site_checks("check-1")
        return cid, before, done, after, diff, gone, undone, _links(db, cid), \
            slim.recount_diff(s)

    cid, before, done, after, diff, gone, undone, back, diff_back = run(go())
    # The official phone stays, at its own trust; everything the site gave goes.
    assert after == [("phone", "+40268000111", "confirmed")]
    assert diff == {}                                    # the kept counts followed
    values = gone["values"]
    # Left with nothing: deleted; on the do-not-contact list or in a campaign: kept.
    assert "office@exemplu-tours.test" not in values and "+40268000666" not in values
    assert "https://facebook.com/exemplutours" not in values
    assert {"rezervari@exemplu-tours.test", "info@exemplu-tours.test"} <= values
    assert done["contacts_kept"] == 2
    assert _rows(db, "SELECT value FROM suppression") == [("rezervari@exemplu-tours.test",)]
    # The site's people go; the register's stay.
    assert gone["people"] == ["POPESCU ANA"] and gone["keys"] == []
    # The firm is due for a new lookup: its website and the lookup's search are gone.
    assert gone["firm"] == [(None, None, None)]
    names = gone["searches"]
    assert ("tinyfish", "web_search", 1) not in names
    assert ("contact_finder", "judge_site", 2) in names
    # Undone: as it was.
    assert undone == {"restored": 1, "refused": 0}
    assert back == before[0] and diff_back == {}
    assert {r[0] for r in _rows(db, "SELECT name FROM person")} == {"POPESCU ANA", "Ion Exemplu"}
    assert ("tinyfish", "web_search", 1) in [(m, c, o) for m, c, o in _rows(
        db, "SELECT m.name, c.name, s.outcome FROM search s JOIN module m ON m.id = s.module "
            "JOIN capability c ON c.id = s.capability")]
    assert _rows(db, "SELECT website FROM firm WHERE id = ?", int(cid)) == [(f"https://{SITE}/",)]


def test_an_undo_leaves_a_firm_that_got_another_website_since(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.reject_found_site(cid, batch="b", detail="")
        await s.set_found_website(cid, "https://exemplu-nou.test/", trust="likely",
                                  source="serper", basis={"by": "judge"})
        return await s.undo_site_checks("b")

    assert run(go()) == {"restored": 0, "refused": 1}


# ── The check, one firm at a time ──


class Reads:
    def __init__(self, monkeypatch, sites):
        self.sites, self.read = sites, []
        real = api.capability

        async def read_site(inp, ctx):
            self.read.append(inp.domain)
            return self.sites.get(inp.domain) or api.Draft()

        def capability(module_id, cap_id, manifests=None):
            if (module_id, cap_id) == ("website", "read_site"):
                return api.Capability(module_id, cap_id, read_site)
            return real(module_id, cap_id, manifests)

        monkeypatch.setattr(api, "capability", capability)


def _page(body, *contacts_):
    url = f"https://{SITE}/contact"
    return api.Draft(pages=[api.Page(url, f"<html><body>{body}</body></html>")],
                     contacts=[api.Contact(k, v, display=v, source_url=url, module=m,
                                           capability="read_site" if m == "website"
                                           else "site_phones") for k, v, m in contacts_])


def test_a_profile_address_is_ruled_out_without_a_visit(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    reads = Reads(monkeypatch, {})

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(
            s, website="https://www.listafirme.ro/exemplu-constructii-srl-90000017/")
        return cid, await site_check.recheck_one(s, cid, batch="b")

    cid, done = run(go())
    assert done["outcome"] == "rejected" and done["why"] == "directory" and reads.read == []


def test_a_site_the_judge_takes_raises_its_chosen_contacts_and_lowers_nothing(
        tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {SITE: _page("Exemplu Construcții · Brașov · Tel 0268 000 666 · "
                                    "office@exemplu-tours.test · Site de Agenție, 021 000 9990",
                                    ("phone", "+40268000666", "phones"),
                                    ("email", "office@exemplu-tours.test", "website"),
                                    ("phone", "+40210009990", "phones"))})
    asked = []

    async def ask(state, task, payload, **kw):
        asked.append(task)
        if task == "site":
            return classify.Answer(value={"yes": True}, model="fake-small")
        return classify.Answer(value={"contacts": [0, 1], "no": [2]},
                               model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s, trust="likely")
        await s.add_contacts(cid, [
            _found("phone", "+40268000666", "phones", cap="site_phones", trust="confirmed"),
            _found("email", "office@exemplu-tours.test", "website", trust="likely"),
            _found("phone", "+40210009990", "phones", cap="site_phones", trust="likely")])
        done = await site_check.recheck_one(s, cid, batch="b")
        return cid, done, await s.read("website_basis", cid), slim.recount_diff(s)

    cid, done, basis, diff = run(go())
    assert asked == ["site", "contacts"]
    assert done["outcome"] == "taken" and done["trust"] == "likely"
    # Nothing is lowered or taken off: a "no" for the agency's phone only
    # leaves it out of what is raised.
    assert _links(db, cid) == [("email", "office@exemplu-tours.test", "likely"),
                               ("phone", "+40210009990", "likely"),
                               ("phone", "+40268000666", "confirmed")]
    assert basis["by"] == "judge" and basis["model"] == "fake-small"
    assert basis["trust"] == "likely" and basis["found_by"] == "tinyfish" and diff == {}
    assert run(_waiting(db)) == 0


OTHER = "exemplu-alt.test"


def test_a_check_run_takes_off_only_what_its_undo_puts_back(tmp_path, monkeypatch):
    """One batch: a site taken whose classifier left the firm's own email
    out, and a site that isn't the firm's. The one taken keeps every
    contact it had (the left-out email at its own trust) and raises what
    was chosen; the undo puts the other back and leaves it so."""
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {
        SITE: _page("Exemplu Construcții · Brașov · Tel 0268 000 666 · "
                    "office@exemplu-tours.test · vanzari@exemplu-tours.test · "
                    "Site de Agenție, 021 000 9990",
                    ("phone", "+40268000666", "phones"),
                    ("email", "office@exemplu-tours.test", "website"),
                    ("phone", "+40210009990", "phones")),
        OTHER: api.Draft(pages=[api.Page(f"https://{OTHER}/", "<p>Vacanțe la mare</p>")])})

    async def ask(state, task, payload, **kw):
        if task == "contacts":
            return classify.Answer(value={"contacts": [0], "no": [1, 2]},
                                   model="fake-small")
        if payload["firm"]["name"].startswith("ALFA"):
            return classify.Answer(value={"yes": False}, model="fake-small")
        return classify.Answer(value={"yes": True}, model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        taken = await _firm_with_found_site(s)
        await s.add_contacts(taken, [
            _found("phone", "+40268000666", "phones", cap="site_phones"),
            _found("email", "office@exemplu-tours.test", "website"),
            _found("phone", "+40210009990", "phones", cap="site_phones")])
        wrong = await _firm_with_found_site(s, name="ALFA EXEMPLU SRL", cui=90000025,
                                            website=f"https://{OTHER}/")
        await s.add_contacts(wrong, [_found("email", f"contact@{OTHER}", "website",
                                            url=f"https://{OTHER}/contact")])
        before = _links(db, wrong)
        done = [await site_check.recheck_one(s, c, batch="b") for c in (taken, wrong)]
        after = (_links(db, taken), _links(db, wrong), slim.recount_diff(s))
        undone = await s.undo_site_checks("b")
        back = (_links(db, taken), _links(db, wrong), slim.recount_diff(s))
        stored = _rows(db, "SELECT firm FROM site_undo WHERE batch = 'b'")
        return wrong, before, done, after, undone, back, stored

    wrong, before, done, after, undone, back, stored = run(go())
    assert [d["outcome"] for d in done] == ["taken", "rejected"]
    kept = [("email", "office@exemplu-tours.test", "unconfirmed"),
            ("phone", "+40210009990", "unconfirmed"),
            ("phone", "+40268000666", "likely")]
    assert after == (kept, [], {})
    # Only the rejected site is in the batch's undo; the one taken lost nothing.
    assert stored == [(int(wrong),)] and undone == {"restored": 1, "refused": 0}
    assert back == (kept, before, {})


def test_a_confirmed_site_keeps_every_contact_the_classifier_didnt_pick(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {SITE: _page("Exemplu Construcții SRL · CUI RO90000017 · "
                                    "Tel 0268 000 666 · office@exemplu-tours.test",
                                    ("phone", "+40268000666", "phones"),
                                    ("email", "office@exemplu-tours.test", "website"))})

    async def ask(state, task, payload, **kw):
        assert task == "contacts"
        return classify.Answer(value={"contacts": [], "no": [0, 1]}, model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.add_contacts(cid, [
            _found("phone", "+40268000666", "phones", cap="site_phones", trust="confirmed"),
            _found("email", "office@exemplu-tours.test", "website")])
        done = await site_check.recheck_one(s, cid, batch="b")
        return cid, done, slim.recount_diff(s)

    cid, done, diff = run(go())
    assert done["outcome"] == "taken" and done["trust"] == "confirmed" and diff == {}
    assert _links(db, cid) == [("email", "office@exemplu-tours.test", "unconfirmed"),
                               ("phone", "+40268000666", "confirmed")]


async def _waiting(db):
    s = StateManager(str(db))
    return await site_check.waiting(s)


def test_a_site_with_the_firms_cui_is_confirmed_and_its_domain_its_key(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {SITE: _page("Exemplu Construcții SRL · CUI RO90000017",
                                    ("phone", "+40268000666", "phones"))})

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.add_contacts(cid, [_found("phone", "+40268000666", "phones", cap="site_phones")])
        return cid, await site_check.recheck_one(s, cid, batch="b")

    cid, done = run(go())
    assert done["outcome"] == "taken" and done["trust"] == "confirmed"
    assert _links(db, cid) == [("phone", "+40268000666", "confirmed")]
    assert _rows(db, "SELECT domain, website_trust FROM firm WHERE id = ?", int(cid)) == [
        (SITE, 0)]


def test_with_no_classifier_a_question_waits_and_nothing_changes(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {SITE: _page("Exemplu · Brașov", ("phone", "+40268000666", "phones"))})

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.add_contacts(cid, [_found("phone", "+40268000666", "phones", cap="site_phones")])
        return cid, await site_check.recheck_one(s, cid, batch="b")

    cid, done = run(go())
    assert done == {"outcome": "waiting", "why": "off"}
    assert _links(db, cid) == [("phone", "+40268000666", "unconfirmed")]


def test_the_count_comes_first_and_changes_nothing(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        await _firm_with_found_site(
            s, website="https://www.listafirme.ro/exemplu-constructii-srl-90000017/")
        await _firm_with_found_site(s, name="ALFA EXEMPLU SRL", cui=90000025,
                                    website="https://anuar-local.test/firma/90000025")
        await _firm_with_found_site(s, name="BETA EXEMPLU SRL", cui=90000033, trust="likely")
        checked = await _firm_with_found_site(s, name="GAMA EXEMPLU SRL", cui=90000041,
                                              trust="likely")
        with sqlite3.connect(db) as con:
            con.execute("UPDATE firm SET website_basis = '{\"by\": \"judge\"}' WHERE id = ?",
                        (int(checked),))
        return await site_check.count(s)

    c = run(go())
    assert (c["firms"], c["unconfirmed"], c["likely"]) == (3, 2, 1)
    assert c["ruled_out_by_address"] == 2 and c["to_read"] == 1
    assert c["by_reason"] == {"directory": 1, "profile": 1} and c["provider"] == "off"
    assert _rows(db, "SELECT COUNT(*) FROM firm WHERE website IS NOT NULL") == [(4,)]


def test_harvey_contacts_rejudge_counts_by_default(tmp_path, monkeypatch, capsys):
    import harvey.state as state_mod
    from harvey import cli

    db = tmp_path / "harvey.db"

    async def seed():
        s = await _state(db)
        await _firm_with_found_site(
            s, website="https://www.listafirme.ro/exemplu-constructii-srl-90000017/")
    run(seed())
    monkeypatch.setattr(state_mod, "DB_PATH", db)
    args = SimpleNamespace(action="rejudge", run=False, dry_run=True, undo="", limit_checks=0,
                           background=True, file="", all=False, task="site")
    cli.cmd_contacts(args)
    out = capsys.readouterr().out
    assert "Found websites waiting for a check: 1 (1 unconfirmed, 0 likely)" in out
    assert "ruled out by their address:  1" in out and "Nothing changed" in out
    # (The flag that limits a run is --checks.)
    assert "--checks N for the first N" in out and "--limit" not in out
    assert _rows(db, "SELECT COUNT(*) FROM jobs") == [(0,)]
    cli.cmd_contacts(SimpleNamespace(**{**vars(args), "run": True}))
    out = capsys.readouterr().out
    assert "1 queued" in out
    assert _rows(db, "SELECT kind, lane FROM jobs") == [("recheck_site", "site_check")]


# ── The classifier's test over a labelled set ──


class AlwaysOpenAI:
    """An openai module whose model always gives the same reply."""

    def __init__(self, reply):
        self.requests = []
        fake = self

        class AsyncOpenAI:
            def __init__(self, **kw):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            async def create(self, **kw):
                fake.requests.append(kw)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                    usage=SimpleNamespace(prompt_tokens=600, completion_tokens=20),
                    model=kw["model"])

        self.module = types.ModuleType("openai")
        self.module.AsyncOpenAI = AsyncOpenAI


@pytest.mark.parametrize("reply", ['{"site": "no"}', '{"site": "yes", "c1": "yes"}'])
def test_judge_test_scores_the_built_in_set(tmp_path, monkeypatch, reply):
    from harvey import finder, site_match

    fake = AlwaysOpenAI(reply)
    monkeypatch.setitem(sys.modules, "openai", fake.module)
    cases = site_check.builtin_cases()
    # What today's code asks: each candidate in question alone.
    pools = {}
    for row in cases:
        facts = site_match.Facts.of(row["firm"])
        kept, _ = site_check._cards(row, facts)
        settled, pool, _ = finder.question([c for _, c in kept])
        if settled is None and pool:
            pools[row["id"]] = [kept[i][0] for i in pool]

    async def go():
        s = await _state(tmp_path / "harvey.db")
        await switches.set_module_setting(s, "contact_finder", "classify_provider",
                                          "openai_compatible")
        await switches.set_module_setting(s, "contact_finder", "classify_model", "fake-3b")
        return await site_check.judge_test(s, cases)

    r = run(go())
    asked = [c for c in cases if c["id"] in pools]
    site_calls = sum(len(p) for p in pools.values())
    assert r["rows"] == len(cases) and r["asked"] == len(asked) and r["invalid"] == 0
    expect_none = sum(c["expect"]["candidate"] is None for c in asked)
    if '"no"' in reply:
        # Never a link: right about every site that isn't the firm's, a miss for each that is.
        assert (r["correct"], r["false_links"], r["misses"]) == (
            expect_none, 0, len(asked) - expect_none)
        assert len(fake.requests) == site_calls and r["tokens_in"] == 600 * site_calls
    else:
        # Yes to every candidate: one alone is chosen, two or more link nothing.
        got = {c["id"]: pools[c["id"]][0] if len(pools[c["id"]]) == 1 else None for c in asked}
        right = sum(got[c["id"]] == c["expect"]["candidate"] for c in asked)
        assert r["correct"] == right
        assert r["false_links"] == sum(got[c["id"]] is not None and got[c["id"]] !=
                                       c["expect"]["candidate"] for c in asked)
        # (A site rightly chosen with expected contacts: its contacts asked too.)
        contacts_calls = sum(got[c["id"]] is not None and got[c["id"]] == c["expect"]["candidate"]
                             and c["expect"].get("contacts") is not None for c in asked)
        assert len(fake.requests) == site_calls + contacts_calls
        assert r["contacts_picked"] >= contacts_calls > 0
    lines = site_check.summary_lines(r)
    assert lines[0] == "Classifier: openai_compatible fake-3b"
    assert "False links" in lines[3]


def test_the_built_in_set_is_fictional_and_well_formed():
    cases = site_check.builtin_cases()
    assert len(cases) >= 15 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert set(c) >= {"id", "firm", "results", "candidates", "expect", "code"}
        for cand in c["candidates"]:
            assert cand["domain"].endswith(".test")
    # The cases the brief names are all there.
    ids = {c["id"] for c in cases}
    assert {"own-cui-footer", "pfa-city-hall-phone", "listing-page", "foreign-same-word",
            "news-article", "second-is-right", "injection-in-page", "contacts-footer-noise",
            "obfuscated-email"} <= ids


def _labelled(id_, candidates, *, results=None, hard="judge", candidate=0):
    return {"id": id_, "code": {"hard": hard},
            "firm": {"name": "EXEMPLU CONSTRUCTII SRL", "cui": "90000017", "county": "Brașov",
                     "locality": "Brașov"},
            "results": results or [{"url": f"https://{c['domain']}/", "title": ""}
                                   for c in candidates],
            "candidates": candidates, "expect": {"candidate": candidate}}


def test_judge_test_asks_only_what_todays_code_would_ask(tmp_path, monkeypatch):
    """Which rows are asked comes from the code, not the file's stored
    verdict: pages are read again, a directory's result is screened out,
    and a stored card with no pages is used as it is, and counted."""
    asked = []

    async def ask(state, task, payload, **kw):
        asked.append([c.domain for c in payload["cards"]])
        return classify.Answer(value={"yes": True}, model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)
    stale = {"domain": "exemplu-constructii.test", "facts": ["name"], "readable": True,
             "contacts": []}
    rows = [
        # Stored as a question, but its page carries the firm's CUI: settled by code.
        _labelled("pages-confirm", [{**stale, "pages": [
            {"url": "https://exemplu-constructii.test/",
             "html": "<p>Exemplu Construcții SRL · CUI RO90000017</p>"}]}]),
        # Stored as confirmed, but the page no longer names it: a question now.
        _labelled("pages-question", [{**stale, "facts": ["cui"], "pages": [
            {"url": "https://exemplu-constructii.test/",
             "html": "<p>Exemplu Construcții · Brașov</p>"}]}], hard="confirmed"),
        # Its only result is the firm's profile on another site: never read, never asked.
        _labelled("profile", [{**stale, "domain": "anuar-local.test"}],
                  results=[{"url": "https://anuar-local.test/firma/90000017", "title": ""}]),
        # No pages: its stored card is used as it is.
        _labelled("stored", [stale]),
    ]

    async def go():
        s = await _state(tmp_path / "harvey.db")
        await switches.set_module_setting(s, "contact_finder", "classify_provider",
                                          "openai_compatible")
        return await site_check.judge_test(s, rows)

    r = run(go())
    assert asked == [["exemplu-constructii.test"], ["exemplu-constructii.test"]]
    assert (r["asked"], r["not_asked"], r["stored_cards"], r["correct"]) == (2, 2, 1, 2)
    assert any("1 row(s) asked with their cards as stored" in line
               for line in site_check.summary_lines(r))


def test_judge_test_runs_with_a_decision_model_and_shows_its_bar_and_spend(tmp_path,
                                                                         monkeypatch):
    # (A decision model: probabilities combined in code; fake HTTP answers.)
    from tests.test_classify import DECISION_URL, OWN_SITE, FakeHTTP, said

    FakeHTTP(default=said(OWN_SITE, tokens=(1000, 0))).install(monkeypatch)

    async def wait_gap(lane, seconds):
        pass

    monkeypatch.setattr(lanes, "wait_gap", wait_gap)
    monkeypatch.setenv("CLASSIFY_DECISION_KEY", "test-decision-value")
    cases = site_check.builtin_cases()

    async def go():
        s = await _state(tmp_path / "harvey.db")
        for key, value in (("provider", "decision"), ("key_name", "CLASSIFY_DECISION_KEY"),
                           ("base_url", DECISION_URL), ("model", "decision-test-1"),
                           ("yes", 0.8), ("price_in_per_m", "1"),
                           ("price_out_per_m", "0")):
            await switches.set_module_setting(s, "contact_finder", f"classify_{key}", value)
        return await site_check.judge_test(s, cases)

    r = run(go())
    assert r["asked"] > 0 and r["invalid"] == 0 and r["yes"] == 0.8
    assert "bars" not in r
    lines = site_check.summary_lines(r)
    assert lines[0] == "Classifier: decision decision-test-1 (contacts kept at 0.80)"
    # Each call 1000 tokens at $1 a million.
    assert r["cost_known"] and r["cost_usd"] == pytest.approx(r["tokens_in"] / 1e6)
    assert lines[-1] == f"Spend: {classify.usd(r['cost_usd'])}"


def test_judge_test_refuses_with_the_classifier_off(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        return await site_check.judge_test(s, site_check.builtin_cases())

    with pytest.raises(ValueError, match="classifier is off"):
        run(go())


# ── The pages ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def test_the_classifier_settings_name_the_key_never_its_value(client, monkeypatch):
    monkeypatch.setenv("CLASSIFY_API_KEY", "secret-classifier-value")
    run(_state(client.db_path))
    assert client.get("/api/classify").json()["provider"] == "off"
    assert client.post("/api/classify/test").status_code == 409
    bad = client.post("/api/classify/settings", json={"key_name": "sk-secret-value"})
    assert bad.status_code == 400
    assert client.post("/api/classify/settings", json={"provider": "gpt"}).status_code == 400
    assert client.post("/api/classify/settings", json={"color": "red"}).status_code == 400
    r = client.post("/api/classify/settings", json={
        "provider": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
        "model": "small-3b", "per_minute": 10, "tasks": {"results": {"on": False, "per_day": 9}}})
    data = r.json()
    assert r.status_code == 200 and data["success"]
    assert (data["provider"], data["model"], data["per_minute"]) == (
        "openai_compatible", "small-3b", 10)
    assert data["key_name"] == "CLASSIFY_API_KEY" and data["key_set"] is True
    assert data["tasks"]["results"] == {"on": False, "per_day": 9}
    assert data["tasks"]["site"]["on"] is True
    assert "secret-classifier-value" not in r.text
    assert "secret-classifier-value" not in client.get("/api/classify").text
    test = client.post("/api/classify/test").json()
    assert test["success"]
    assert _rows(client.db_path, "SELECT kind, lane FROM jobs") == [("classify_test", "classify")]
    assert client.get(f"/api/classify/test/{test['job_id']}").json()["status"] == "queued"


def test_today_offers_the_check_and_the_panel_says_why_a_site_was_taken(client):
    async def go():
        s = await _state(client.db_path)
        waiting = await _firm_with_found_site(s)
        taken = await s.add_company(Company(name="ALFA EXEMPLU SRL",
                                            location="Brașov, Brașov, Romania",
                                            source="registry:onrc"))
        await s.set_found_website(taken, "https://alfa-exemplu.test/", trust="likely",
                                  source="tinyfish",
                                  basis={"by": "judge", "model": "fake-small"})
        return waiting, taken

    waiting, taken = run(go())
    today = client.get("/api/today").json()
    item = next(i for i in today["items"] if i["key"] == "site-check")
    assert today["stats"]["sites_to_check"] == 1 and item["tab"] == "settings"
    panel = client.get(f"/api/companies/{taken}").json()
    assert panel["website_basis"]["by"] == "judge"
    assert panel["website_basis"]["model"] == "fake-small"
    assert panel["website_basis"]["found_by"] == "tinyfish"
    assert client.get(f"/api/companies/{waiting}").json()["website_basis"]["trust"] == \
        "unconfirmed"
    counts = client.get("/api/contacts/site-checks").json()
    assert counts["firms"] == 1 and counts["to_read"] == 1
    started = client.post("/api/contacts/site-checks").json()
    assert started["success"] and started["queued"] == 1


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_the_classifier_card_and_why_a_site_was_taken_read_in_both_languages():
    node = _node()
    web = Path(dash.__file__).resolve().parent / "web"
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => process.argv[3]};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
['classify-card', 'rejudge-box', 'cls-test-result'].forEach(id => els[id] = {innerHTML: ''});
global.document = {getElementById: id => els[id] || null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.failMsg = (d, f) => f;
global.tServer = (k, d) => d;
let mode = '';
const metrics = {today: {calls: 3, usd: 0.0012, unpriced: 0, avg_tokens: 610}, month: {calls: 40, usd: 0.02, unpriced: 0, avg_tokens: 620},
                 per_task: {site: {calls: 30, usd: 0.015, avg_tokens: 615}}, sites: 30, cost_per_site: 0.0005};
global.api = async (path) => path === '/api/classify' && mode === 'dm'
  ? {provider: 'decision', providers: ['off', 'openai_compatible', 'anthropic', 'brain', 'decision'],
     base_url: 'https://decisions.example.com/v1/systemone', host: 'decisions.example.com', model: 'decision-test-1',
     key_name: 'CLASSIFY_DECISION_KEY', key_set: true, deepinfra: false, price_list: false, account: false,
     json_mode: true, per_day: 2000, per_minute: 20, timeout_seconds: 30, yes: 0.75,
     price_in_per_m: 0.06, price_out_per_m: 0, usd_per_day: 1.5, usd_per_month: null, budget_reached: true,
     cost: {known: true, source: 'entered', in_per_m: 0.06, out_per_m: 0}, metrics: metrics,
     tasks: {site: {on: true, per_day: 1500}}, calls_today: 0, held_seconds: 0, waiting: 0}
  : path === '/api/classify' && mode === 'di'
  ? {provider: 'openai_compatible', providers: ['off', 'openai_compatible', 'anthropic', 'brain', 'decision'],
     base_url: 'https://api.deepinfra.com/v1/openai', host: 'api.deepinfra.com', model: 'vendor/chat-1',
     key_name: 'CLASSIFY_API_KEY', key_set: true, deepinfra: true, price_list: true, account: true,
     json_mode: true, per_day: 2000, per_minute: 20, timeout_seconds: 30,
     price_in_per_m: null, price_out_per_m: null, usd_per_day: 1.5, usd_per_month: null, budget_reached: true,
     cost: {known: true, source: 'deepinfra', in_per_m: 0.06, out_per_m: 0}, metrics: metrics,
     tasks: {site: {on: true, per_day: 1500}}, calls_today: 0, held_seconds: 0, waiting: 0}
  : path === '/api/classify'
  ? {provider: 'openai_compatible', providers: ['off', 'openai_compatible', 'anthropic', 'brain'],
     base_url: 'http://127.0.0.1:11434/v1', host: '127.0.0.1', model: 'small-3b', key_name: 'CLASSIFY_API_KEY',
     key_set: false, json_mode: true, per_day: 2000, per_minute: 20, timeout_seconds: 30,
     tasks: {results: {on: true, per_day: 1500}, site: {on: true, per_day: 1500},
             contacts: {on: false, per_day: 500}}, usd_per_day: 2, usd_per_month: null,
     cost: {known: false, source: '', why: 'cost unknown: the USD cap can\'t be checked'},
     metrics: {today: {calls: 12, usd: 0, unpriced: 12, avg_tokens: 400}, month: {calls: 12, usd: 0, unpriced: 12, avg_tokens: 400},
               per_task: {}, sites: 12, cost_per_site: null},
     calls_today: 12, held_seconds: 0, waiting: 3}
  : {firms: 3, unconfirmed: 2, likely: 1, ruled_out_by_address: 2, to_read: 1,
     provider: 'openai_compatible', days: 1};
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('// Settings: the classifier (a small language model)', 'let _finder = null;'));
(async () => {
  await loadClassifyCard();
  await new Promise(r => setTimeout(r, 0));
  const card = els['classify-card'].innerHTML, box = els['rejudge-box'].innerHTML;
  mode = 'dm';
  await loadClassifyCard();
  const dmCard = els['classify-card'].innerHTML;
  mode = 'di';
  await loadClassifyCard();
  console.log(JSON.stringify({card: card, box: box, dm_card: dmCard, di_card: els['classify-card'].innerHTML,
    dm: siteBasisText({by: 'judge', model: 'dm-1', p: 0.88, found_by: 'tinyfish', trust: 'likely'}),
    plain: siteBasisText({by: 'judge', model: 'small-3b', found_by: 'tinyfish', trust: 'likely'}),
    cui: siteBasisText({by: 'cui', found_by: 'tinyfish', trust: 'confirmed'}),
    open: siteBasisText({found_by: 'tinyfish', trust: 'unconfirmed'}),
    declared: siteBasisText({})}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    out = {}
    for lang in ("ro", "en"):
        res = subprocess.run([node, "-e", script, str(web / "app.js"), str(web / "i18n.js"), lang],
                             text=True, encoding="utf-8", capture_output=True, check=True,
                             timeout=30)
        out[lang] = json.loads(res.stdout)
    ro, en = out["ro"], out["en"]
    assert "Clasificator" in ro["card"] and "Classifier" in en["card"]
    assert 'value="CLASSIFY_API_KEY"' in en["card"] and "holds no value" in en["card"]
    # Where the questions and the key go, plainly.
    assert "the key CLASSIFY_API_KEY go to: 127.0.0.1" in en["card"]
    assert 'id="cls-task-contacts">' in en["card"] and 'id="cls-task-site" checked' in en["card"]
    assert "12" in en["card"] and "2,000" in en["card"]
    assert "2 ruled out by their address, 1 to read again" in en["box"]
    assert "Check them" in en["box"] and "Verifică-le" in ro["box"]
    assert en["plain"] == ("found by a search (tinyfish) · likely: the classifier said yes "
                           "(small-3b)")
    assert ro["plain"].endswith("probabil: clasificatorul a spus da (small-3b)")
    assert 'id="cls-yes"' not in en["card"]
    assert en["dm"] == "found by a search (tinyfish) · likely: the classifier said yes, 88% (dm-1)"
    assert ro["dm"].endswith("probabil: clasificatorul a spus da, 88% (dm-1)")
    assert 'id="cls-yes"' in en["dm_card"] and 'value="0.75"' in en["dm_card"]
    assert "cls-site_" not in en["dm_card"]       # the site's bars are fixed, not fields
    assert "the key CLASSIFY_DECISION_KEY go to: decisions.example.com" in en["dm_card"]
    assert "Decision model (TypeSafe System One format)" in en["dm_card"]
    assert "Model de decizie" in ro["dm_card"]
    assert "https://api.typesafe.ai/v1/systemone" in en["dm_card"] and "jev-latest" in ro["dm_card"]
    # A decision model: only the price entered, no price list, no account box.
    assert "Price entered: $0.0600 in" in en["dm_card"]
    assert "loadClassifyModels()" not in en["dm_card"] and "loadClassifyAccount()" not in en["dm_card"]
    # The price, where it comes from, the budget, the spend, DeepInfra's list and account.
    assert "Price from DeepInfra&#39;s list" in en["di_card"] or \
        "Price from DeepInfra's list" in en["di_card"]
    assert "$0.0600 in" in en["di_card"] and 'id="cls-usd_day"' in en["di_card"]
    assert 'value="1.5"' in en["di_card"] and 'id="cls-budget"' in en["di_card"]
    assert "this month: 40 calls, $0.0200" in en["di_card"] and "per site checked: $0.0005" in en["di_card"]
    assert "loadClassifyModels()" in en["di_card"] and "loadClassifyAccount()" in en["di_card"]
    assert 'id="cls-yes"' not in en["di_card"]
    # An unknown price says the USD cap can't count, in both languages.
    assert "cost unknown: the USD cap can&#39;t be checked" in en["card"] or \
        "cost unknown: the USD cap can't be checked" in en["card"]
    assert "Until the price is known, only the call caps count." in en["card"]
    assert "per site checked: –" in en["card"]
    assert "cost necunoscut: plafonul în USD nu poate fi verificat" in ro["card"]
    assert "loadClassifyModels()" not in en["card"] and "loadClassifyAccount()" not in en["card"]
    assert ro["cui"] == "găsit prin căutare (tinyfish) · confirmat: CUI-ul firmei e pe site"
    assert en["open"].endswith("not checked yet") and en["declared"] == ""


def test_every_new_string_is_in_both_languages():
    node = _node()
    web = Path(dash.__file__).resolve().parent / "web"
    script = r'''
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const pre = ['cls.', 'rejudge.', 'site.basis.', 'today.item.site_check'];
const pick = (L) => Object.keys(I18N[L]).filter(k => pre.some(p => k.startsWith(p))).sort();
console.log(JSON.stringify({ro: pick('ro'), en: pick('en')}));
'''
    res = subprocess.run([node, "-e", script, str(web / "i18n.js")], text=True,
                         encoding="utf-8", capture_output=True, check=True, timeout=30)
    keys = json.loads(res.stdout)
    assert keys["ro"] == keys["en"] and len(keys["ro"]) > 40


# ── What a rejection leaves, and what waits ──


def _searches(db, cid=None):
    return _rows(db, "SELECT m.name, c.name, s.outcome, s.cost FROM search s JOIN module m "
                     "ON m.id = s.module JOIN capability c ON c.id = s.capability "
                     "WHERE s.is_person = 0 ORDER BY s.id")


def test_a_rejected_site_is_looked_up_again_whatever_its_lookups_detail(tmp_path):
    """The lookup row of a site the finder chose carries a long detail (counts,
    the domains read, why): a rejection now finds it by what it is, and keeps
    it on file with its cost, no longer counted as done; the reads of the
    site and the register people's address checks at it stop counting too."""
    from harvey import finder
    from harvey.modules.runs import CONTACT_READ

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        slim.describe(s, slim.firm("EXEMPLU CONSTRUCTII SRL", 90000017, "Brașov"))
        cid = await s.add_company(Company(name="EXEMPLU CONSTRUCTII SRL", cui="90000017",
                                          location="Brașov, Brașov, Romania",
                                          source="registry:onrc"))
        await s.set_found_website(cid, f"https://{SITE}/", trust="unconfirmed",
                                  source="tinyfish")
        await s.record_search("company", cid, "tinyfish", "web_search", looked_for="website",
                              outcome="found", found=1, cost=0.004,
                              detail=f"3 results: 1 directory; read: {SITE}, alt.test; "
                                     "waits for the classifier (off)")
        await s.record_search("company", cid, "website", "read_site", looked_for=CONTACT_READ,
                              outcome="found", found=2)
        await s.add_people(cid, [("POPESCU ANA", "administrator")], source="onrc")
        (pid,) = _rows(db, "SELECT id FROM person")[0]
        await s.record_search("person", pid, "email_verify", "find_email", looked_for="email",
                              outcome="nothing", origin=cid)
        before = await s.read("finder_people", cid, "email_verify", "find_email", None, -1)
        done = await s.reject_found_site(cid, batch="b2", detail="not it")
        searched = await finder.company_searched(s, cid, "tinyfish", "web_search", None,
                                                 looked_for="website")
        after = await s.read("finder_people", cid, "email_verify", "find_email", None, -1)
        deep = _rows(db, "SELECT site_read_deep FROM firm WHERE id = ?", int(cid))
        rows = _searches(db)
        undone = await s.undo_site_checks("b2")
        again = await finder.company_searched(s, cid, "tinyfish", "web_search", None,
                                              looked_for="website")
        return done, searched, before, after, deep, rows, undone, again, _searches(db)

    done, searched, before, after, deep, rows, undone, again, back = run(go())
    assert done["searches"] == 3 and searched is False
    # Kept on file with its cost, counted as not done.
    assert ("tinyfish", "web_search", 0, 0.004) in rows
    assert ("website", "read_site", 0, 0.0) in rows and deep == [(None,)]
    # The register's administrator is searched again at the next site.
    assert before == [] and [p["name"] for p in after] == ["POPESCU ANA"]
    # Undone: as it was.
    assert undone == {"restored": 1, "refused": 0} and again is True
    assert ("tinyfish", "web_search", 1, 0.004) in back
    assert ("website", "read_site", 1, 0.0) in back


def test_a_site_that_doesnt_answer_is_taken_off_only_after_three_days(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    reads = Reads(monkeypatch, {})                    # every read: no page

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        out = []
        for _ in range(3):
            out.append(await site_check.recheck_one(s, cid, batch="b"))
            with sqlite3.connect(db) as con:      # (the next try, a day later)
                con.execute("UPDATE search SET at = at - 86400 WHERE detail LIKE ?",
                            (site_check.UNREADABLE + "%",))
        return cid, out

    cid, out = run(go())
    assert [o["outcome"] for o in out] == ["waiting", "waiting", "rejected"]
    assert out[0]["why"] == "unreadable" and out[2]["why"] == "unreadable"
    assert reads.read == [SITE] * 3
    assert _rows(db, "SELECT website FROM firm WHERE id = ?", int(cid)) == [(None,)]


def test_one_unanswered_read_changes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    Reads(monkeypatch, {})

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.add_contacts(cid, [_found("phone", "+40268000666", "phones", cap="site_phones")])
        return cid, await site_check.recheck_one(s, cid, batch="b")

    cid, done = run(go())
    assert done["outcome"] == "waiting" and done["why"] == "unreadable"
    assert _rows(db, "SELECT website FROM firm WHERE id = ?", int(cid)) == [(f"https://{SITE}/",)]
    assert _links(db, cid) == [("phone", "+40268000666", "unconfirmed")]


@pytest.mark.parametrize("why", ["held", "day cap", "budget"])
def test_while_the_classifier_cant_answer_nothing_is_read(tmp_path, monkeypatch, why):
    db = tmp_path / "harvey.db"
    reads = Reads(monkeypatch, {SITE: _page("Exemplu · Brașov")})

    async def available(state, task, s=None):
        return why

    monkeypatch.setattr(classify, "available", available)

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        return await site_check.recheck_one(s, cid, batch="b")

    assert run(go()) == {"outcome": "waiting", "why": why} and reads.read == []


@pytest.mark.parametrize("why", ["day cap", "budget"])
def test_a_held_classifier_rests_the_whole_check_lane(tmp_path, monkeypatch, why):
    from harvey.modules import jobs
    from harvey.modules.job_kinds import SITE_CHECK_LANE, run_recheck_site

    rested = []

    async def recheck_one(state, company_id, **kw):
        return {"outcome": "waiting", "why": why}

    async def rest_lane(state, lane, seconds, *, but=""):
        rested.append((lane, but, seconds >= 60))
        return 5

    monkeypatch.setattr(site_check, "recheck_one", recheck_one)
    monkeypatch.setattr(jobs, "rest_lane", rest_lane)
    ctx = SimpleNamespace(params={"company_id": "1"}, lane={"config": None}, state=None,
                          job=SimpleNamespace(batch_id="b", lane=SITE_CHECK_LANE, id="j1"))
    with pytest.raises(jobs.LaneExhausted):
        run(run_recheck_site(ctx))
    assert rested == [(SITE_CHECK_LANE, "j1", True)]


def test_a_waiting_site_is_judged_with_every_candidate_and_moved_to_the_one_taken(
        tmp_path, monkeypatch):
    from harvey import site_match
    from harvey.modules.runs import CONTACT_READ

    db = tmp_path / "harvey.db"
    other = "exemplu-constructii.test"
    url = f"https://{other}/contact"
    reads = Reads(monkeypatch, {
        SITE: _page("Vacanțe la mare · rezervări"),
        other: api.Draft(pages=[api.Page(url, "<p>Exemplu Construcții · Brașov · "
                                              "Tel 0268 000 555</p>")],
                         contacts=[api.Contact("phone", "+40268000555", display="0268 000 555",
                                               source_url=url, module="phones",
                                               capability="site_phones")])})
    asked = []

    async def ask(state, task, payload, **kw):
        asked.append((task, [c.domain for c in payload["cards"]]))
        if task == "contacts":
            return classify.Answer(value={"contacts": [0], "no": []}, model="fake-small")
        yes = payload["cards"][0].domain == other
        return classify.Answer(value={"yes": yes}, model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.record_search("company", cid, "website", "read_site", looked_for=CONTACT_READ,
                              outcome="nothing", detail=site_match.waiting_detail([SITE, other]))
        return cid, await site_check.recheck_one(s, cid, batch="b")

    cid, done = run(go())
    # Each candidate asked alone, then the contacts of the one taken.
    assert asked == [("site", [SITE]), ("site", [other]), ("contacts", [other])]
    assert reads.read == [SITE, other]
    assert done["outcome"] == "taken" and done["website"] == other
    assert _rows(db, "SELECT website, website_trust FROM firm WHERE id = ?", int(cid)) == [
        (f"https://{other}/", 1)]
    # The contact the classifier chose is linked now (a waiting site had none).
    assert _links(db, cid) == [("phone", "+40268000555", "likely")]


def test_the_endpoint_and_key_settings_refuse_what_could_leak_a_secret(client):
    run(_state(client.db_path))
    for body in ({"key_name": "SMTP_PASSWORD"}, {"key_name": "TINYFISH_API_KEY"},
                 {"key_name": "GMAIL_CLIENT_SECRET"}, {"base_url": "file:///etc/passwd"},
                 {"base_url": "http://169.254.169.254/latest/"},
                 {"tasks": {"site": {"per_day": "many"}}}):
        assert client.post("/api/classify/settings", json=body).status_code == 400, body
    r = client.post("/api/classify/settings", json={
        "provider": "openai_compatible", "key_name": "GROQ_API_KEY",
        "base_url": "https://api.exemplu.test/openai/v1",
        "tasks": {"site": {"on": True, "per_day": 10 ** 9}}}).json()
    assert r["success"] and r["key_name"] == "GROQ_API_KEY" and r["host"] == "api.exemplu.test"
    assert r["tasks"]["site"]["per_day"] == classify.MAX_PER_DAY
    # A decision model's contacts bar: a number, clamped; the site's bars aren't settings.
    assert client.post("/api/classify/settings", json={"yes": "high"}).status_code == 400
    assert client.post("/api/classify/settings", json={"site_own": 0.5}).status_code == 400
    r = client.post("/api/classify/settings", json={
        "provider": "decision", "yes": 2, "key_name": "CLASSIFY_DECISION_KEY",
        "base_url": "https://decisions.exemplu.test/v1/decide"}).json()
    assert r["success"] and r["yes"] == 0.99 and r["host"] == "decisions.exemplu.test"
    assert "site_own" not in r and "site_names" not in r
    assert "decision" in r["providers"] and "typesafe" not in r["providers"]
    # No endpoint or model of its own: blank stays blank.
    assert r["model"] == "" and client.post("/api/classify/settings", json={
        "base_url": ""}).json()["host"] == ""


def test_the_check_asks_before_it_starts_and_says_what_happens_with_no_classifier():
    node = _node()
    web = Path(dash.__file__).resolve().parent / "web"
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => 'en'};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
global.document = {getElementById: () => null};
global.escHtml = s => String(s == null ? '' : s);
global.failMsg = (d, f) => f;
global.showToast = () => {};
const asked = [], posted = [];
global.confirm = (text) => { asked.push(text); return false; };
global.api = async (path, opts) => { if (opts && opts.method === 'POST') posted.push(path); return {}; };
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('// Settings: the classifier (a small language model)', 'let _finder = null;'));
(async () => {
  vm.runInThisContext("_classify = {provider: 'off'}");
  await startRejudge(12);
  vm.runInThisContext("_classify = {provider: 'openai_compatible'}");
  await startRejudge(3);
  console.log(JSON.stringify({asked, posted}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    res = subprocess.run([node, "-e", script, str(web / "app.js"), str(web / "i18n.js")],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    out = json.loads(res.stdout)
    assert out["posted"] == []                       # declined: nothing started
    assert out["asked"][0].startswith("The classifier is off") and "12" in out["asked"][0]
    assert out["asked"][1].startswith("Check 3 found websites?")


def test_every_translation_key_has_a_line_of_its_own():
    import re

    web = Path(dash.__file__).resolve().parent / "web"
    glued = [n for n, line in enumerate((web / "i18n.js").read_text(encoding="utf-8")
                                        .splitlines(), 1)
             if re.search(r'^\s*"[^"]+":\s*"(?:[^"\\]|\\.)*",\s+"[^"]+":', line)]
    assert glued == []


def test_the_day_cap_counts_a_call_for_each_candidate_asked(tmp_path, monkeypatch):
    from harvey import finder, site_match

    fake = AlwaysOpenAI('{"site": "no"}')
    monkeypatch.setitem(sys.modules, "openai", fake.module)
    facts = site_match.Facts(name="EXEMPLU CONSTRUCTII SRL", cui="90000017", locality="Brașov")
    cards = [site_match.Card(domain=f"exemplu{i}.test", title="Exemplu", facts=["name"],
                             excerpt=["Exemplu Construcții"], text="Exemplu Construcții")
             for i in range(3)]

    async def go():
        s = await _state(tmp_path / "harvey.db")
        for key, value in (("provider", "openai_compatible"), ("model", "fake-3b"),
                           ("per_day", 2)):
            await switches.set_module_setting(s, "contact_finder", f"classify_{key}", value)
        d = await finder.decide(s, facts, cards)
        return d, await classify.calls_today(s), await classify.calls_today(s, "site")

    d, today, site = run(go())
    # Three candidates are three calls: two fit the day, the third waits.
    assert len(fake.requests) == 2 and (today, site) == (2, 2)
    assert d.index is None and d.waiting == "day cap" and d.pool == [0, 1, 2]


def test_two_yes_leave_the_site_as_it_is_and_the_same_classifier_isnt_asked_again(
        tmp_path, monkeypatch):
    from harvey import site_match
    from harvey.modules.runs import CONTACT_READ

    db = tmp_path / "harvey.db"
    other = "exemplu-constructii.test"
    reads = Reads(monkeypatch, {SITE: _page("Exemplu Construcții · Brașov"),
                                other: _page("Exemplu Construcții · Brașov · hale")})
    asked = []

    async def ask(state, task, payload, **kw):
        asked.append(task)
        return classify.Answer(value={"yes": True}, model="fake-small")

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        cid = await _firm_with_found_site(s)
        await s.record_search("company", cid, "website", "read_site", looked_for=CONTACT_READ,
                              outcome="nothing", detail=site_match.waiting_detail([SITE, other]))
        first = await site_check.recheck_one(s, cid, batch="b")
        second = await site_check.recheck_one(s, cid, batch="b")
        await switches.set_module_setting(s, "contact_finder", "classify_model", "fake-other")
        third = await site_check.recheck_one(s, cid, batch="b")
        return cid, first, second, third

    cid, first, second, third = run(go())
    assert first == second == third == {"outcome": "waiting", "why": "unsettled"}
    # Two calls the first time, none the second; another model asks again.
    assert asked == ["site", "site", "site", "site"] and reads.read == [SITE, other, SITE, other]
    assert _rows(db, "SELECT website_trust, website_basis FROM firm WHERE id = ?", int(cid)) == [
        (2, None)]
    assert _links(db, cid) == []


def test_the_count_says_at_most_how_many_days_at_the_tighter_cap(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        for i, cui in enumerate((90000017, 90000025, 90000033)):
            await _firm_with_found_site(s, name=f"EXEMPLU {i} SRL", cui=cui,
                                        website=f"https://exemplu-{i}.test/")
        for key, value in (("provider", "openai_compatible"), ("model", "fake-3b"),
                           ("per_day", 8)):
            await switches.set_module_setting(s, "contact_finder", f"classify_{key}", value)
        return await site_check.count(s)

    c = run(go())
    # A firm is at most three candidates and its contacts: 8 calls a day are 2 firms.
    assert c["to_read"] == 3 and c["days"] == 2


# ── The classifier's cost, budget, price list and account (Settings) ──


def test_the_prices_and_usd_caps_are_amounts_or_blank(client):
    run(_state(client.db_path))
    for body in ({"usd_per_day": -1}, {"usd_per_month": "lots"}, {"price_in_per_m": True},
                 {"price_out_per_m": "-0.5"}):
        assert client.post("/api/classify/settings", json=body).status_code == 400, body
    r = client.post("/api/classify/settings", json={
        "provider": "decision", "price_in_per_m": "0,06", "price_out_per_m": 0,
        "usd_per_day": 1.5, "usd_per_month": "20"}).json()
    assert r["success"] and (r["price_in_per_m"], r["price_out_per_m"]) == (0.06, 0.0)
    assert (r["usd_per_day"], r["usd_per_month"]) == (1.5, 20.0)
    assert r["cost"]["known"] and r["cost"]["source"] == "entered"
    assert r["budget_reached"] is False and r["metrics"]["month"]["calls"] == 0
    r = client.post("/api/classify/settings", json={"price_in_per_m": "", "usd_per_day": None,
                                                    "usd_per_month": ""}).json()
    assert r["price_in_per_m"] is None and r["usd_per_day"] is None
    assert r["usd_per_month"] is None and not r["cost"]["known"]
    assert r["cost"]["why"] == classify.COST_UNKNOWN
    assert r["deepinfra"] is False and r["price_list"] is False and r["account"] is False


def test_harvey_contacts_status_says_the_spend_and_the_budget(tmp_path, monkeypatch, capsys):
    from harvey import cli

    async def go():
        s = await _state(tmp_path / "harvey.db")
        for key, value in (("provider", "decision"), ("model", "decision-test-1"),
                           ("price_in_per_m", "1"), ("price_out_per_m", "0"),
                           ("usd_per_day", "0.001")):
            await switches.set_module_setting(s, "contact_finder", f"classify_{key}", value)
        await s.record_usage_event(agent="classify", task="classify.site", model="decision-test-1",
                                   input_tokens=1000, cost_usd=0.001, source="classify",
                                   provider="decision")
        await cli._print_site_checks(s)

    run(go())
    out = capsys.readouterr().out
    assert "today 1 calls $0.0010, this month 1 calls $0.0010; per site $0.0010" in out
    assert "USD cap: $0.0010 a day; reached: the firms wait unlinked" in out


def _models_reply():
    from tests.test_classify import _listed

    return [_listed("vendor/chat-max", kind="text-generation", cents_in=1.2e-04,
                    cents_out=4e-04, discount=0.5),
            _listed("vendor/decide-1", kind="decision", cents_in=4e-06),
            _listed("vendor/chat-large", kind="text-generation", cents_in=9e-05,
                    cents_out=4e-04),
            _listed("vendor/chat-small", kind="text-generation", cents_in=5e-06,
                    cents_out=1.5e-05),
            {**_listed("vendor/painter", kind="text-to-image")}]


def test_the_price_list_is_fetched_here_filtered_by_kind_sorted_and_kept(client, monkeypatch):
    import httpx
    from tests.test_classify import FakeHTTP

    fake = FakeHTTP(default=httpx.Response(200, json=_models_reply())).install(monkeypatch)
    run(_state(client.db_path))
    client.post("/api/classify/settings", json={
        "provider": "openai_compatible", "base_url": "https://api.deepinfra.com/v1/openai"})
    status = client.get("/api/classify").json()
    assert status["deepinfra"] and status["price_list"]
    r = client.get("/api/classify/models").json()
    # Cheapest first, text-generation models only, the discount applied; no badge.
    assert [m["model"] for m in r["models"]] == ["vendor/chat-small", "vendor/chat-max",
                                                 "vendor/chat-large"]
    assert r["models"][1]["in_per_m"] == pytest.approx(0.6) and r["error"] == ""
    assert set(r["models"][0]) == {"model", "type", "in_per_m", "out_per_m", "context", "discount"}
    # No price list for a decision model (its price is the one entered), nor for anthropic.
    assert client.get("/api/classify/models?provider=decision").status_code == 400
    assert client.get("/api/classify/models?provider=anthropic").status_code == 400
    # Fetched once (for the card's status and the list), kept a day.
    assert [q.url.path for q in fake.requests] == ["/models/list"]


def test_a_price_list_that_doesnt_answer_says_so(client, monkeypatch):
    import httpx
    from tests.test_classify import FakeHTTP

    FakeHTTP(default=httpx.ConnectError("refused")).install(monkeypatch)
    run(_state(client.db_path))
    r = client.get("/api/classify/models?provider=openai_compatible").json()
    assert r["models"] == [] and r["error"]


def _account_replies(usage=200, limits=200, balance=200):
    import httpx

    def reply(request):
        path = request.url.path
        if path == "/payment/usage":
            assert request.url.params["from"] == "current" and request.url.params["to"] == "current"
            if usage == "down":
                raise httpx.ConnectError("refused")
            return httpx.Response(usage, json={"initial_month": "2026.01", "months": [{
                "period": classify.since("month")[:7].replace("-", "."),
                "interval": {"fr": 0, "to": 1}, "total_cost": 7, "invoice_id": "NOT_FINAL",
                "items": [{"model": {"provider": "deepinfra", "model_name": "vendor/chat-1",
                                     "task": "text-generation"}, "units": 61303, "rate": 4e-06,
                           "cost": 5, "pricing_type": "input_tokens", "interval": {}},
                          {"model": {"provider": "deepinfra", "model_name": "vendor/chat-1",
                                     "task": "text-generation"}, "units": 100, "rate": 0.0,
                           "cost": 2, "pricing_type": "output_tokens", "interval": {}}]}]})
        if path == "/payment/key-limits":
            return httpx.Response(limits, json=[{"token_id": "t1", "name": "harvey",
                                                 "limit": 5.0, "spend": 0.07}])
        if path == "/payment/checklist":
            return httpx.Response(balance, json={
                "stripe_balance": -5.0, "recent": 0.07, "limit": None, "suspend_reason": None,
                "suspended": False, "billing_address_info": {"line1": "Str. Exemplu 1"}})
        return httpx.Response(404)

    return reply


def _deepinfra_account(client, monkeypatch, **replies):
    from tests.test_classify import FakeHTTP

    monkeypatch.setenv("CLASSIFY_DEEPINFRA_KEY", "secret-deepinfra-value")
    fake = FakeHTTP(default=_account_replies(**replies)).install(monkeypatch)
    run(_state(client.db_path))
    client.post("/api/classify/settings", json={
        "provider": "openai_compatible", "base_url": "https://api.deepinfra.com/v1/openai",
        "key_name": "CLASSIFY_DEEPINFRA_KEY", "price_in_per_m": "1", "price_out_per_m": "1"})
    return fake


def test_the_account_box_reads_deepinfra_with_the_key_on_the_server(client, monkeypatch):
    fake = _deepinfra_account(client, monkeypatch)
    assert client.get("/api/classify").json()["account"] is True
    res = client.get("/api/classify/account")
    a = res.json()
    assert a["available"] and a["usage"]["ok"] and a["limits"]["ok"] and a["balance"]["ok"]
    assert a["usage"]["total_usd"] == 0.07
    assert a["usage"]["models"] == [{"model": "vendor/chat-1", "usd": 0.07}]
    assert a["limits"]["keys"] == [{"name": "harvey", "limit_usd": 5.0, "spend_usd": 0.07}]
    assert a["balance"] == {"ok": True, "credit_usd": 5.0, "owed_usd": 0.0,
                            "since_invoice_usd": 0.07, "suspended": False}
    # The key went to DeepInfra, never to the browser; nor the billing address.
    assert {q.headers["authorization"] for q in fake.requests} == {"Bearer secret-deepinfra-value"}
    assert "secret-deepinfra-value" not in res.text and "Exemplu 1" not in res.text
    # Kept a few minutes: no second read.
    client.get("/api/classify/account")
    assert len(fake.requests) == 3


@pytest.mark.parametrize("which", ["usage", "limits", "balance"])
def test_each_account_part_says_why_it_cant_be_read(client, monkeypatch, which):
    _deepinfra_account(client, monkeypatch, **{which: 403})
    a = client.get("/api/classify/account").json()
    assert a[which] == {"ok": False, "code": "scope", "scope": "billing:read",
                        "error": "the key can't read this: it needs the billing:read scope"}
    assert all(a[p]["ok"] for p in ("usage", "limits", "balance") if p != which)


def test_an_account_part_that_fails_leaves_the_others(client, monkeypatch):
    _deepinfra_account(client, monkeypatch, usage="down", balance=500)
    a = client.get("/api/classify/account?refresh=true").json()
    assert a["usage"]["code"] == "no_answer" and a["balance"]["code"] == "status"
    assert a["balance"]["status"] == 500 and a["limits"]["ok"]


def test_the_account_box_needs_deepinfra_and_a_key(client, monkeypatch):
    from tests.test_classify import FakeHTTP

    fake = FakeHTTP().install(monkeypatch)
    run(_state(client.db_path))
    client.post("/api/classify/settings", json={"provider": "openai_compatible",
                                                "base_url": "https://llm.exemplu.test/v1"})
    assert client.get("/api/classify/account").json()["code"] == "not_deepinfra"
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))
    monkeypatch.delenv("CLASSIFY_API_KEY", raising=False)
    client.post("/api/classify/settings", json={"base_url": "https://api.deepinfra.com/v1/openai"})
    assert client.get("/api/classify/account").json()["code"] == "no_key"
    assert fake.requests == []


def _classifier_texts() -> dict:
    """Every text a person reads about the classifier: the docs' classifier
    parts, the manifest's notes, the card's strings in both languages, the
    CLI's help."""
    import re

    root = Path(dash.__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    toml = (root / "harvey/modules/core/contact_finder/module.toml").read_text(encoding="utf-8")
    i18n = (root / "harvey/web/i18n.js").read_text(encoding="utf-8")
    cli_src = (root / "harvey/cli.py").read_text(encoding="utf-8")
    return {
        "README": "\n".join(l for l in readme.splitlines() if "classif" in l.lower()),
        "CLAUDE.md": "\n".join(l for l in claude.splitlines() if "classif" in l.lower()),
        "CHANGELOG": "\n".join(p for p in unreleased.split("\n- ") if "classif" in p.lower()),
        "module.toml": "\n".join(l for l in toml.splitlines()
                                 if l.startswith("#") and "classif" in toml.lower()),
        "i18n": "\n".join(l for l in i18n.splitlines()
                          if re.match(r'\s*"(cls|rejudge|site\.basis)\.', l)),
        "cli": "\n".join(l for l in cli_src.splitlines()
                         if "judge-test" in l or "classif" in l.lower()),
    }


def test_nothing_about_the_classifier_recommends_a_provider_or_model():
    texts = _classifier_texts()
    assert all(texts.values()), [k for k, v in texts.items() if not v]
    for where, text in texts.items():
        low = text.lower()
        assert "recommend" not in low and "recomand" not in low, where
        assert "the cheapest" not in low and "cel mai bun" not in low, where
        for word in ("suggest", "sugera", "sugest", "default model", "model implicit"):
            assert word not in low, (where, word)
