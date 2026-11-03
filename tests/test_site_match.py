"""Whether a website a search found is a company's own (harvey/site_match.py,
harvey/finder.py): results ruled out by their address before any visit, the
register facts that decide without a model, the evidence cards a small
model reads, and the finder linking only what the evidence (or the
classifier, a fake here) says. All data is fictional: made-up CUIs, .test
and .example domains, unassignable numbers."""

import asyncio
import json
import sqlite3

import pytest

from harvey import classify, finder, site_match
from harvey.collectors.profile import site_draft  # noqa: F401  (the real read, never called here)
from harvey.models import Company
from harvey.modules import api
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim

ENV = {"serper_api_key": "test-key"}
FACTS = site_match.Facts(name="EXEMPLU CONSTRUCTII SRL", form="SRL", cui="90000017",
                         reg="J08/1234/2010", street="florilor", number="12", locality="Brașov",
                         county="Brașov", phones=["+40268000111"], emails=["office@exemplu.test"])


def run(coro):
    return asyncio.run(coro)


def _draft(url, body, *contacts):
    return api.Draft(pages=[api.Page(url, f"<html><title>T</title><body>{body}</body></html>")],
                     contacts=list(contacts))


def _c(kind, value, url):
    return api.Contact(kind, value, display=value, source_url=url, module="website",
                       capability="read_site")


def _card(body, *contacts, domain="exemplu-constructii.test", facts=FACTS, holders=None):
    url = f"https://{domain}/contact"
    return site_match.card(facts, domain, _draft(url, body, *contacts), url=url, holders=holders)


# ── Ruled out by the address, before any visit ──


@pytest.mark.parametrize("url,why", [
    ("https://www.listafirme.ro/exemplu-constructii-srl-90000017/", "directory"),
    ("https://ro.kompass.com/c/exemplu/ro0001/", "directory"),          # a parent entry
    ("https://firmele-exemplu.test/x", "directory"),                    # a name that lists firms
    ("https://www.facebook.com/exemplu", "social"),
    ("https://anuar-exemplu.test/firma/90000017", "profile"),            # the CUI in the path
    ("https://anuar-exemplu.test/?cui=RO90000017", "profile"),
    ("https://registru-exemplu.test/j08-1234-2010/", "profile"),         # the registration number
    ("https://catalog-local.test/company/some-firm/", "profile"),      # a profile-shaped path
    ("https://primariaexemplu.ro/contact", "institution"),
    ("https://servicii.exemplu.gov.ro/", "institution"),
    ("https://linktr.ee/exemplu", "unusable"),
    ("https://gmail.com/", "unusable"),
])
def test_results_ruled_out_by_their_address(url, why):
    from harvey.collectors.discover import normalize_domain

    assert site_match.screen(url, normalize_domain(url), FACTS) == why


@pytest.mark.parametrize("url", [
    "https://exemplu-constructii.test/",
    "https://exemplu-constructii.test/company/despre/",      # a profile shape on its own name
    "https://hale-brasov.test/contact",
])
def test_a_firms_own_kind_of_address_is_kept(url):
    from harvey.collectors.discover import normalize_domain

    assert site_match.screen(url, normalize_domain(url), FACTS) == ""


# ── What decides without a model ──


@pytest.mark.parametrize("body,expected", [
    ("Exemplu Construcții SRL · CUI: 90000017 · Brașov", ("confirmed", "cui")),
    ("Cod fiscal RO 90000017", ("confirmed", "cui")),
    ("© Exemplu · RO90000017", ("confirmed", "ro+cui")),
    ("Nr. Reg. Com. J08/1234/2010", ("confirmed", "reg")),
    ("Nr. Reg. Com. J08 / 01234 / 2010", ("confirmed", "reg")),
    ("Exemplu Construcții · Scrieți-ne: office@exemplu.test", ("confirmed", "email")),
    ("Cod unic de înregistrare: 90000017", ("confirmed", "cui")),
    ("Cod de înregistrare fiscală RO 90000017", ("confirmed", "cui")),
    ("CIF: RO-90000017", ("confirmed", "cui")),
    ("© Exemplu Construcții · RO-90000017", ("confirmed", "ro+cui")),
    ("Exemplu Construcții, Brașov · Tel 0268 000 111", ("confirmed", "phone")),
])
def test_a_register_fact_on_the_pages_confirms(body, expected):
    assert site_match.hard(_card(body)) == expected


def test_the_registration_number_in_its_long_form():
    facts = site_match.Facts(name="EXEMPLU SRL", cui="90000017", reg="J2010001234083")
    assert site_match.hard(_card("Reg: J2010001234083", facts=facts)) == ("confirmed", "reg")


@pytest.mark.parametrize("body", [
    "Tel 0268 9000 0017",                    # the CUI's digits inside a phone
    "Tel 02689000001788",                    # inside a longer number
    "IBAN RO49EXMP0000090000017000",         # inside an IBAN
    "Comanda nr. 90000017 a fost livrată",   # no fiscal context
])
def test_the_cui_inside_another_number_is_no_match(body):
    c = _card(body)
    assert "cui" not in c.facts and "ro+cui" not in c.facts
    assert site_match.hard(c)[0] != "confirmed"


def test_the_firms_own_code_beside_others_is_a_question_not_a_listing():
    # A group's site lists its sister companies; a listing page lists anyone:
    # the classifier tells them apart, code doesn't.
    c = _card("Grupul Exemplu: Exemplu SRL CUI 90000017; Alfa SRL CUI 90000025; "
              "Beta SRL CUI 90000033")
    assert c.other_cuis == ["90000025", "90000033"]
    assert site_match.hard(c) == ("judge", "cui_and_other_cuis")


def test_several_other_codes_and_not_the_firms_own_make_a_listing():
    c = _card("Firme: Alfa SRL CUI 90000025; Beta SRL CUI 90000033")
    assert site_match.hard(c) == ("rejected", "listing")


def test_one_other_code_beside_the_firms_own_is_a_question():
    c = _card("Exemplu SRL CUI 90000017 · site realizat de Agenție SRL, CUI 90000041")
    assert site_match.hard(c) == ("judge", "cui_and_other_cuis")


def test_another_firms_code_as_the_sites_owner_rules_it_out():
    c = _card("Theta Steel Distribution SRL, CUI 90000155, Ploiești", domain="theta-steel.test")
    assert site_match.hard(c) == ("rejected", "other_firm")


def test_an_official_phone_without_the_firms_name_is_a_question_not_a_verdict():
    # A city hall's switchboard is a PFA's register phone: the judge decides.
    c = _card("Primăria Exemplu · Registratura 0268 000 111", domain="comuna-exemplu.test")
    assert "phone:official" in c.facts
    assert site_match.hard(c) == ("judge", "phone_without_name")


def test_a_phone_many_firms_share_confirms_nothing():
    c = _card("Exemplu Construcții · Tel 0268 000 111", holders={"+40268000111": 7})
    assert "phone:official" not in c.facts and site_match.hard(c)[0] == "judge"


def test_a_site_that_didnt_answer_is_ruled_out():
    c = site_match.card(FACTS, "exemplu.test", api.Draft())
    assert not c.readable and site_match.hard(c) == ("rejected", "unreadable")


def test_tva_rates_and_land_book_numbers_are_not_firms_codes():
    c = _card("Prețuri cu TVA 19% inclus. Teren intabulat, CF nr. 123456, Brașov")
    assert c.other_cuis == []


# ── The evidence card a small model reads ──


def test_the_card_names_where_the_firms_facts_appear():
    url = "https://consultanta-exemplu.test/"
    c = _card("<h1>Consultanță în Brașov</h1><footer>© 2026 Exemplu Construcții · "
              "Str. Florilor nr. 12, Brașov · +31 10 000 0000 · 0268 000 222</footer>",
              _c("phone", "+40268000222", url), _c("email", "nume@exemplu.ro", url),
              _c("email", "contact@consultanta-exemplu.test", url),
              domain="consultanta-exemplu.test")
    # The name is only in the footer, not the domain.
    assert {"name", "street", "street+no", "town", "county"} <= set(c.facts)
    assert "name_in_domain" not in c.facts
    assert c.phone_countries.get("RO") == 1 and len(c.phone_countries) == 2
    # A theme's placeholder never reaches the card.
    assert [k["value"] for k in c.contacts] == ["+40268000222",
                                                 "contact@consultanta-exemplu.test"]
    assert "0268 000 222" in c.contacts[0]["line"]
    text = site_match.card_text(1, c)
    assert text.startswith("[1] consultanta-exemplu.test")
    assert "c1 phone +40268000222" in text and "© 2026 Exemplu Construcții" in text


def test_cards_are_trimmed_to_fit_a_small_models_budget():
    long = "".join(f"<p>Contact linia {i}, str. Exemplu, Brașov</p>" for i in range(200))
    cards = [_card(long, *[_c("phone", f"+4026800{i:04d}", "https://x.test/")
                                        for i in range(20)], domain=f"exemplu{n}.test")
             for n in range(3)]
    full = "\n".join(site_match.card_text(i, c) for i, c in enumerate(cards, 1))
    trimmed = site_match.cards_text(cards, 900)
    assert site_match.tokens(full) > 900 >= site_match.tokens(trimmed)
    assert "[1] exemplu0.test" in trimmed


# ── The finder: every result, read only what may be the firm's ──


async def _state(path) -> StateManager:
    s = StateManager(str(path))
    await s.init_db()
    await sync_vocabulary(s)
    return s


class Fakes:
    """The website read and the website lookup: each answers from a dict and
    remembers what it was asked (every page fetch counted)."""

    def __init__(self, results, sites):
        self.results, self.sites = results, sites
        self.read = []

    def install(self, monkeypatch):
        real = api.capability

        async def read_site(inp, ctx):
            self.read.append(inp.domain)
            return self.sites.get(inp.domain) or api.Draft()

        async def find_website(inp, ctx):
            return api.Draft(records=[api.Record(**r) for r in self.results], cost=0.0)

        fakes = {("website", "read_site"): read_site,
                 ("contact_finder", "find_website"): find_website}

        def capability(module_id, cap_id, manifests=None):
            fn = fakes.get((module_id, cap_id))
            if fn is None:
                return real(module_id, cap_id, manifests)
            return api.Capability(module_id, cap_id, fn, estimate=lambda inp: 0.0)

        monkeypatch.setattr(api, "capability", capability)
        return self


class FakeClassifier:
    """classify.ask answering from ``answers`` by task (a dict, a callable
    of the payload, or None for "no answer"), and remembering each payload."""

    def __init__(self, monkeypatch, **answers):
        self.answers, self.asked = answers, []
        monkeypatch.setattr(classify, "ask", self.ask)

    async def ask(self, state, task, payload, **kw):
        self.asked.append((task, payload))
        answer = self.answers.get(task)
        if callable(answer):
            answer = answer(payload)
        if answer is None:
            return classify.Answer(why="off")
        return classify.Answer(value=answer, model="fake-small", tokens_in=500, tokens_out=20)


def _record(domain, path="/", name=""):
    return dict(name=name or domain, domain=domain, website=f"https://{domain}{path}")


OWN = "exemplu-constructii.test"


async def _firm(s, name="EXEMPLU CONSTRUCTII SRL", cui=90000017, address="Str. Florilor nr. 12"):
    slim.describe(s, slim.firm(name, cui, "Brașov", address=address))
    return await s.add_company(Company(name=name, cui=str(cui), location="Brașov, Brașov, Romania",
                                       source="registry:onrc"))


def _links(db, cid):
    return [(k, v, t) for k, v, _, t, *_ in slim.links(str(db), cid)]


def _row(db, sql, *args):
    with sqlite3.connect(db) as con:
        return con.execute(sql, args).fetchall()


def test_directories_and_profiles_are_never_read(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    url = f"https://{OWN}/"
    fakes = Fakes([_record("listafirme.ro", "/exemplu-constructii-srl-90000017/"),
                   _record("anuar-exemplu.test", "/firma/90000017"),
                   _record("primariabrasov-exemplu.ro"),
                   _record(OWN)],
                  {OWN: _draft(url, "Exemplu Construcții SRL · CUI 90000017 · Brașov",
                               _c("phone", "+40268000555", url))}).install(monkeypatch)
    ai = FakeClassifier(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    # Three ruled out by their address: one page read, no model asked.
    # (Confirmed by its CUI: the classifier is asked only which contacts are
    # the firm's, and with none set up code's reading stands.)
    assert fakes.read == [OWN] and [t for t, _ in ai.asked] == ["contacts"]
    assert (found.website, found.trust) == (OWN, "confirmed")
    assert _links(db, cid) == [("phone", "+40268000555", "confirmed")]
    basis = _row(db, "SELECT website_basis FROM firm WHERE id = ?", int(cid))[0][0]
    assert json.loads(basis)["by"] == "cui"
    (detail,) = [r[0] for r in _row(db, "SELECT detail FROM search WHERE detail LIKE '4 results%'")]
    assert detail.startswith("4 results: ") and "read: exemplu-constructii.test" in detail
    # The results that weren't read are kept nowhere.
    assert "listafirme" not in detail and "anuar-exemplu" not in detail


def test_the_results_task_cuts_the_reads_before_any_page_is_fetched(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    url = f"https://{OWN}/"
    results = [_record("constructii-romania.test"), _record("exemplu-steel.test"),
               _record(OWN), _record("hale-exemplu.test"), _record("stiri-exemplu.test")]
    fakes = Fakes(results, {OWN: _draft(url, "Exemplu Construcții · Str. Florilor nr. 12, Brașov",
                                        _c("phone", "+40268000555", url))}).install(monkeypatch)

    def results_answer(p):
        assert [r.domain for r in p["results"]] == [r["domain"] for r in results]
        return {"keep": [2]}

    ai = FakeClassifier(monkeypatch, results=results_answer, site={"yes": True},
                        contacts={"contacts": [0], "no": []})

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert fakes.read == [OWN]                              # one fetch, not five
    assert [t for t, _ in ai.asked] == ["results", "site", "contacts"]
    assert (found.website, found.trust) == (OWN, "likely")
    assert _links(db, cid) == [("phone", "+40268000555", "likely")]
    basis = json.loads(_row(db, "SELECT website_basis FROM firm")[0][0])
    # (A language model's yes: no probability or confidence claimed.)
    assert basis["by"] == "judge" and "p" not in basis and "confidence" not in basis


def test_with_no_classifier_the_first_three_are_read_by_name(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    results = [_record(f"alt{i}.test") for i in range(4)] + [_record(OWN)]
    fakes = Fakes(results, {}).install(monkeypatch)
    FakeClassifier(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    # The name's best match first, then the search's order; three at most.
    assert fakes.read == [OWN, "alt0.test", "alt1.test"]
    # None answered: ruled out, nothing kept.
    assert found.website == "" and _links(db, cid) == []


def test_the_name_only_in_the_page_reaches_the_judge(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    dom = "hale-brasov.test"
    url = f"https://{dom}/contact"
    Fakes([_record(dom)], {dom: _draft(
        url, "<h1>Hale metalice</h1><footer>© Exemplu Construcții · Brașov · 0268 000 555"
             "</footer>", _c("phone", "+40268000555", url))}).install(monkeypatch)
    ai = FakeClassifier(monkeypatch, site={"yes": True},
                        contacts={"contacts": [0], "no": []})

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    (task, payload), (then, _) = ai.asked
    (card,) = payload["cards"]
    assert (task, then) == ("site", "contacts")
    assert "name" in card.facts and "name_in_domain" not in card.facts
    prompt, _ = classify.TASKS["site"].render(payload, 2300)
    assert "COMPANY: EXEMPLU CONSTRUCTII SRL" in prompt and "© Exemplu Construcții" in prompt
    # The model says likely at most.
    assert found.trust == "likely" and _links(db, cid) == [("phone", "+40268000555", "likely")]


def test_only_the_contacts_the_judge_chose_are_linked(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    dom = "sigmatextil.test"
    url = f"https://{dom}/contact"
    page = ("<h1>Sigma Textil</h1><p>Tel 0234 000 777, vanzari@sigmatextil.test</p>"
            "<p>Biroul din Milano: +39 02 0000 0000</p><p>Mobil 0722 000 333</p>"
            "<footer>Site realizat de Agenție Exemplu · 021 000 9990</footer>")
    Fakes([_record(dom)], {dom: _draft(url, page,
                                       _c("phone", "+40234000777", url),
                                       _c("email", "vanzari@sigmatextil.test", url),
                                       _c("phone", "+390200000000", url),
                                       _c("phone", "+40210009990", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch, site={"yes": True},
                   contacts={"contacts": [0, 1], "no": [2, 3]})

    async def go():
        s = await _state(db)
        cid = await _firm(s, "SIGMA TEXTIL SRL", 90000112)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert sorted(_links(db, cid)) == [("email", "vanzari@sigmatextil.test", "likely"),
                                       ("phone", "+40234000777", "likely")]


def test_a_no_links_nothing(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    dom = "exemplu-tours.test"
    url = f"https://{dom}/"
    Fakes([_record(dom)], {dom: _draft(url, "Exemplu Tours · vacanțe", _c(
        "phone", "+40268000666", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch, site={"yes": False})

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert found.website == "" and _links(db, cid) == []
    assert _row(db, "SELECT website, website_trust FROM firm WHERE id = ?", int(cid)) == [
        (None, None)]
    (row,) = _row(db, "SELECT outcome, detail FROM search WHERE detail LIKE '1 result;%'")
    assert row[0] == 2 and "the classifier: none is the firm's" in row[1]


def test_a_classifiers_probability_is_kept_with_the_yes(tmp_path, monkeypatch):
    # (A decision model answers with a probability: the company panel shows it.)
    db = tmp_path / "harvey.db"
    dom = "exemplu-tours.test"
    url = f"https://{dom}/"
    Fakes([_record(dom)], {dom: _draft(url, "Exemplu Tours · Brașov", _c(
        "phone", "+40268000666", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch, site={"yes": True, "p": 0.88},
                   contacts={"contacts": [0], "no": []})

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert found.website and found.trust == "likely"
    ((basis,),) = _row(db, "SELECT website_basis FROM firm WHERE id = ?", int(cid))
    assert json.loads(basis)["by"] == "judge"
    assert json.loads(basis)["p"] == 0.88


@pytest.mark.parametrize("why", ["off", "held", "day cap"])
def test_with_no_answer_a_site_waits_unlinked(tmp_path, monkeypatch, why):
    db = tmp_path / "harvey.db"
    dom = "exemplu-tours.test"
    url = f"https://{dom}/"
    Fakes([_record(dom)], {dom: _draft(url, "Exemplu · Brașov", _c(
        "phone", "+40268000666", url))}).install(monkeypatch)

    async def ask(state, task, payload, **kw):
        return classify.Answer(why=why)

    monkeypatch.setattr(classify, "ask", ask)

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        found = await finder.find(s, cid, spend=finder.Spend(), env=ENV)
        again = await finder.find(s, cid, spend=finder.Spend(), env=ENV, since="9999-01-01")
        return cid, found, again, await s.read("site_check_ids", -1)

    cid, found, again, waiting = run(go())
    assert found.trust == "unconfirmed" and _links(db, cid) == []
    assert any(f"waits for the classifier ({why})" in w for w in found.skipped)
    # Kept for the check, and neither read nor looked up again meanwhile.
    assert waiting == [int(cid)] and again.steps == []
    assert any("waits for a check" in w for w in again.skipped)


def test_with_no_classifier_a_confirmed_site_links_what_code_read(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    url = f"https://{OWN}/"
    Fakes([_record(OWN)], {OWN: _draft(
        url, "Exemplu Construcții SRL · CUI 90000017",
        _c("phone", "+40268000555", url), _c("email", "nume@exemplu.ro", url),
        _c("email", "office@exemplu-constructii.test", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert found.trust == "confirmed"
    # Everything code read, but the theme's placeholder.
    assert sorted(_links(db, cid)) == [("email", "office@exemplu-constructii.test", "confirmed"),
                                       ("phone", "+40268000555", "confirmed")]
    assert _row(db, "SELECT domain FROM firm WHERE id = ?", int(cid)) == [(OWN,)]


def test_a_second_read_of_a_found_site_never_confirms_it_by_what_the_first_left(tmp_path):
    """The read of a found site links its phone at the site's trust; the next
    read used to find that phone "on file" and call the site confirmed."""
    from harvey import contacts

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        await s.set_found_website(cid, "https://exemplu-tours.test/", trust="likely",
                                  source="tinyfish")
        await s.add_contacts(cid, [
            contacts.Found("phone", "+40268000666", source="phones", capability="site_phones",
                           evidence_url="https://exemplu-tours.test/", trust="likely"),
            contacts.Found("phone", "+40268000111", source="mf_taxpayers", trust="confirmed")])
        on_file = await s.read("finder_on_file", cid)
        company = await finder.company_row(s, cid)
        draft = _draft("https://exemplu-tours.test/", "Tel 0268 000 666",
                       _c("phone", "+40268000666", "https://exemplu-tours.test/"))
        return on_file, finder.judge(company, on_file, 1, floor="likely")(draft)

    on_file, verdict = run(go())
    assert on_file == {"+40268000111"}
    assert verdict == "likely"


def test_a_site_waiting_for_a_check_isnt_fed_to_the_free_walk(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        waiting = await _firm(s)
        await s.set_found_website(waiting, "https://exemplu-tours.test/", trust="unconfirmed",
                                  source="tinyfish")
        declared = await s.add_company(Company(name="ALFA EXEMPLU SRL", domain="alfa-exemplu.test",
                                               location="Brașov, Brașov, Romania"))
        return waiting, declared, await finder.next_free(s, 50)

    waiting, declared, free = run(go())
    assert free == [str(declared)]


def test_a_website_a_listing_declared_is_no_found_one(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="ALFA EXEMPLU SRL", website="https://alfa.test/",
                                          location="Brașov, Brașov, Romania",
                                          source="discover:osm"))
        return (await finder.company_row(s, cid))["website_found"], \
            await s.read("website_basis", cid), await s.read("site_check_ids", -1)

    assert run(go()) == (False, {}, [])


# ── Official emails, codes and addresses that used to mislead ──


def test_an_official_email_on_a_listing_page_confirms_nothing():
    # An accountant's site lists its clients beside the address the taxpayer
    # file holds for the firm: a listing, not the firm's site.
    facts = site_match.Facts(name="ALFA CONSTRUCT SRL", cui="90000017",
                             emails=["contabil@birou-exemplu.test"])
    c = _card("<p>Clienți</p><p>BETA SRL CUI 90000025</p><p>GAMA SRL CUI 90000033</p>"
              "<p>Contact: contabil@birou-exemplu.test</p>", domain="birou-exemplu.test",
              facts=facts)
    assert "email:official" in c.facts
    assert site_match.hard(c) == ("rejected", "listing")


def test_an_official_email_without_the_firms_name_is_a_question():
    facts = site_match.Facts(name="ALFA CONSTRUCT SRL", cui="90000017",
                             emails=["contabil@birou-exemplu.test"])
    c = _card("Hale metalice · contabil@birou-exemplu.test", domain="hale-brasov.test",
              facts=facts)
    assert site_match.hard(c) == ("judge", "email_without_name")
    # The accountant's own site, with its own code: never confirmed by the email.
    c = _card("Birou de contabilitate SRL, CUI 90000041 · contabil@birou-exemplu.test",
              domain="birou-exemplu.test", facts=facts)
    assert site_match.hard(c) == ("judge", "email_without_name")
    # At the candidate's own domain it is the firm's site, name or not.
    own = site_match.Facts(name="ALFA CONSTRUCT SRL", cui="90000017",
                           emails=["office@alfa-construct.test"])
    c = _card("Scrieți-ne: office@alfa-construct.test", domain="www.alfa-construct.test",
              facts=own)
    assert "email:own_domain" in c.facts and site_match.hard(c) == ("confirmed", "email")


def test_an_official_email_many_firms_share_confirms_nothing():
    facts = site_match.Facts(name="EXEMPLU CONSTRUCTII SRL", cui="90000017",
                             emails=["contabil@birou-exemplu.test"])
    c = _card("Exemplu Construcții · contabil@birou-exemplu.test", facts=facts,
              holders={"contabil@birou-exemplu.test": 40})
    assert "email:official" not in c.facts and site_match.hard(c)[0] == "judge"


def test_site_facts_count_how_many_firms_share_an_official_email(tmp_path):
    from harvey import contacts

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        ids = [await _firm(s, f"FIRMA {n} EXEMPLU SRL", 90000017 + 8 * n) for n in range(3)]
        for cid in ids:
            await s.add_contacts(cid, [contacts.Found("email", "contabil@birou-exemplu.test",
                                                      source="mf_taxpayers", trust="confirmed")])
        return await s.read("site_facts", ids[0])

    facts = run(go())
    assert facts["emails"] == ["contabil@birou-exemplu.test"]
    assert facts["holders"]["contabil@birou-exemplu.test"] == 3


@pytest.mark.parametrize("body", [
    "Prețuri fără VAT 2025 · livrare CIF 2020",      # a year, an Incoterm's edition
    "Sediu: RO-500123 Brașov",                        # a postcode after RO-
])
def test_years_and_postcodes_are_not_another_firms_code(body):
    c = _card(body, domain="hale-brasov.test")
    assert c.other_cuis == [] and site_match.hard(c)[0] == "judge"


def test_another_firms_code_beside_this_firms_name_is_a_question():
    # (A web agency's code in the footer of a site that names the firm.)
    c = _card("Exemplu Construcții · Brașov · realizat de Agenție SRL, CUI 90000041",
              domain="hale-brasov.test")
    assert c.other_cuis == ["90000041"] and site_match.hard(c) == ("judge", "")


@pytest.mark.parametrize("url,why", [
    ("https://www.waze.com/live-map/directions/exemplu", "listing"),
    ("https://ro.trustpilot.com/review/exemplu.test", "listing"),
    ("https://www.glassdoor.com/Overview/exemplu", "jobs"),
    ("https://www.kununu.com/ro/exemplu", "jobs"),
    ("https://mapcarta.com/W123", "listing"),
    ("https://2gis.com/firm/1", "listing"),
    ("https://www.paginigalbene.ro/exemplu", "listing"),
    ("https://www.zf.ro/companii/exemplu-123", "news"),
    ("https://lista-firme-brasov.test/x", "directory"),
    ("https://topfirme.test/x", "directory"),
])
def test_more_listings_and_news_are_ruled_out(url, why):
    from harvey.collectors.discover import normalize_domain

    assert site_match.screen(url, normalize_domain(url), FACTS) == why


@pytest.mark.parametrize("url", [
    "https://smithcompanies.test/",                  # "companies" inside a firm's own name
    "https://brandx.test/company/despre/",           # its own page under /company/, another brand
    "https://brandx.test/company/",
])
def test_a_firms_own_site_with_a_listing_word_is_kept(url):
    from harvey.collectors.discover import normalize_domain

    assert site_match.screen(url, normalize_domain(url), FACTS) == ""


# ── The finder: what waits, and a found site read again ──


def test_a_site_that_waits_is_a_readable_candidate_and_names_the_others(tmp_path, monkeypatch):
    """With no answer from the classifier, the site kept to wait was the
    first candidate even when it didn't answer; now the first one still in
    question is kept, and its read names every one for the check."""
    db = tmp_path / "harvey.db"
    dead, live, other = "alfa-dent-vechi.test", "alfa-dent.test", "alfa-dent-brasov.test"
    Fakes([_record(dead), _record(live), _record(other)],
          {live: _draft(f"https://{live}/", "Alfa Dent · Brașov",
                        _c("email", f"office@{live}", f"https://{live}/")),
           other: _draft(f"https://{other}/", "Alfa Dent Brașov · programări",
                         _c("phone", "+40268000666", f"https://{other}/"))}).install(monkeypatch)
    FakeClassifier(monkeypatch)                       # no answer: off

    async def go():
        s = await _state(db)
        cid = await _firm(s, "ALFA DENT SRL", 90000017)
        found = await finder.find(s, cid, spend=finder.Spend(), env=ENV)
        return cid, found, await s.read("site_candidates", cid)

    cid, found, candidates = run(go())
    assert _row(db, "SELECT website, website_trust FROM firm WHERE id = ?", int(cid)) == [
        (f"https://{live}/", 2)]
    assert candidates == [live, other] and _links(db, cid) == []
    assert found.trust == "unconfirmed"


def test_search_again_on_a_found_site_links_only_what_the_evidence_chooses(tmp_path, monkeypatch):
    """Search again on a found site at likely used to read it the old way and
    link everything on it (the agency's phone, an office abroad)."""
    db = tmp_path / "harvey.db"
    dom = "sigmatextil.test"
    url = f"https://{dom}/contact"
    page = ("<h1>Sigma Textil</h1><p>Tel 0234 000 777</p><p>Biroul din Milano: +39 02 0000 0000"
            "</p><footer>Site realizat de Agenție Exemplu · 021 000 9990</footer>")
    fakes = Fakes([], {dom: _draft(url, page, _c("phone", "+40234000777", url),
                                   _c("phone", "+390200000000", url),
                                   _c("phone", "+40210009990", url))}).install(monkeypatch)
    ai = FakeClassifier(monkeypatch, site={"yes": True},
                        contacts={"contacts": [0], "no": [1, 2]})

    async def go():
        s = await _state(db)
        cid = await _firm(s, "SIGMA TEXTIL SRL", 90000112)
        await s.set_found_website(cid, f"https://{dom}/", trust="likely", source="tinyfish",
                                  basis={"by": "judge", "reasons": ["name"]})
        found = await finder.find(s, cid, spend=finder.Spend(), env=ENV, since="9999-01-01")
        return cid, found

    cid, found = run(go())
    assert fakes.read == [dom] and [t for t, _ in ai.asked] == ["site", "contacts"]
    assert _links(db, cid) == [("phone", "+40234000777", "likely")]
    assert found.trust == "likely"


@pytest.mark.parametrize("basis,trust", [({"by": "judge"}, "likely"), (None, "unconfirmed")])
def test_search_again_on_a_found_site_links_nothing_without_an_answer(tmp_path, monkeypatch,
                                                                      basis, trust):
    # (A site checked before keeps its trust for the addresses built at it;
    # one nothing checked builds none.)
    db = tmp_path / "harvey.db"
    dom = "sigmatextil.test"
    url = f"https://{dom}/"
    Fakes([], {dom: _draft(url, "Sigma Textil · 0234 000 777",
                           _c("phone", "+40234000777", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch)

    async def go():
        s = await _state(db)
        cid = await _firm(s, "SIGMA TEXTIL SRL", 90000112)
        await s.set_found_website(cid, url, trust="likely", source="tinyfish", basis=basis)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV,
                                      since="9999-01-01")

    cid, found = run(go())
    assert _links(db, cid) == [] and found.trust == trust
    assert any("nothing linked from the website read again" in w for w in found.skipped)


def test_search_again_on_a_likely_found_site_adds_no_people(tmp_path, monkeypatch):
    # (Below confirmed a site says nothing else about the firm: no people.)
    db = tmp_path / "harvey.db"
    dom = "sigmatextil.test"
    url = f"https://{dom}/"
    draft = _draft(url, "Sigma Textil · 0234 000 777 · Administrator: Ion Exemplu",
                   _c("phone", "+40234000777", url))
    draft.people = [api.Person(first_name="Ion", last_name="Exemplu", title="Administrator")]
    Fakes([], {dom: draft}).install(monkeypatch)
    FakeClassifier(monkeypatch, site={"yes": True},
                   contacts={"contacts": [0], "no": []})

    async def go():
        s = await _state(db)
        cid = await _firm(s, "SIGMA TEXTIL SRL", 90000112)
        await s.set_found_website(cid, url, trust="likely", source="tinyfish",
                                  basis={"by": "judge"})
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV,
                                      since="9999-01-01")

    cid, found = run(go())
    assert _links(db, cid) == [("phone", "+40234000777", "likely")]
    assert found.people == 0 and _row(db, "SELECT COUNT(*) FROM person") == [(0,)]


def _contacts_answer(said):
    async def ask(state, task, payload, **kw):
        assert task == "contacts"
        return classify.Answer(value={"contacts": [j for j, y in said.items() if y],
                                      "no": [j for j, y in said.items() if not y]},
                               model="fake-small")
    return ask


def test_on_a_register_confirmed_site_the_contacts_task_only_takes_off_its_no(monkeypatch):
    # Four contacts; the model was shown and answered two: a "no" to the
    # agency's phone takes it off, the firm's official phone stays whatever
    # it says, and the one it never answered stays as code read it.
    card = _card("Exemplu Construcții SRL · CUI RO90000017 · Str. Florilor nr. 12, Brașov",
                 _c("phone", "+40268000111", "https://exemplu-constructii.test/contact"),
                 _c("phone", "+40210009990", "https://exemplu-constructii.test/contact"),
                 _c("email", "info@exemplu-constructii.test",
                    "https://exemplu-constructii.test/contact"))
    assert site_match.hard(card)[0] == "confirmed"
    monkeypatch.setattr(classify, "ask", _contacts_answer({0: False, 1: False}))
    d = run(finder.decide(None, FACTS, [card], s=classify.Settings(provider="anthropic")))
    assert d.trust == "confirmed" and d.selection == {"contacts": [0, 2]}


def test_on_a_site_the_model_chose_the_official_contacts_are_kept(monkeypatch):
    card = _card("Exemplu Construcții · Brașov",
                 _c("phone", "+40268000111", "https://exemplu-constructii.test/contact"),
                 _c("phone", "+40210009990", "https://exemplu-constructii.test/contact"))
    d = finder.Decision(0, "likely", {"by": "judge"})
    monkeypatch.setattr(classify, "ask", _contacts_answer({0: False, 1: False}))
    run(finder._choose_contacts(None, FACTS, card, d, s=classify.Settings(provider="anthropic")))
    assert d.selection == {"contacts": [0]}


# ── Each candidate asked alone: exactly one yes is chosen ──


def _three(monkeypatch, answers: dict):
    """Three readable candidates no register fact settles, and a classifier
    answering each one by its domain (``answers``: domain -> yes or no)."""
    doms = ["alfa-dent.test", "alfa-dent-brasov.test", "alfa-dent-centru.test"]
    Fakes([_record(d) for d in doms],
          {d: _draft(f"https://{d}/", f"Alfa Dent · Brașov · {d}",
                     _c("phone", f"+4026800066{i}", f"https://{d}/"))
           for i, d in enumerate(doms)}).install(monkeypatch)

    def site(p):
        (card,) = p["cards"]
        return {"yes": answers[card.domain]}

    return doms, FakeClassifier(monkeypatch, site=site, contacts={"contacts": [0], "no": []})


async def _alfa(s):
    return await _firm(s, "ALFA DENT SRL", 90000017)


def test_exactly_one_yes_is_chosen_and_its_contacts_asked_after(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    doms, ai = _three(monkeypatch, {"alfa-dent.test": False, "alfa-dent-brasov.test": True,
                                    "alfa-dent-centru.test": False})

    async def go():
        s = await _state(db)
        cid = await _alfa(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    # One request a candidate, each with that candidate alone; then the contacts.
    asked = [(t, p) for t, p in ai.asked if t != "results"]
    assert [t for t, _ in asked] == ["site", "site", "site", "contacts"]
    assert all(len(p["cards"]) == 1 for _, p in asked)
    assert {p["cards"][0].domain for t, p in asked if t == "site"} == set(doms)
    assert asked[3][1]["cards"][0].domain == "alfa-dent-brasov.test"
    assert (found.website, found.trust) == ("alfa-dent-brasov.test", "likely")
    assert _links(db, cid) == [("phone", "+40268000661", "likely")]
    basis = json.loads(_row(db, "SELECT website_basis FROM firm WHERE id = ?", int(cid))[0][0])
    assert basis["by"] == "judge" and basis["model"] == "fake-small"


def test_two_yes_link_nothing_and_the_site_waits(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    doms, ai = _three(monkeypatch, {"alfa-dent.test": False, "alfa-dent-brasov.test": True,
                                    "alfa-dent-centru.test": True})

    async def go():
        s = await _state(db)
        cid = await _alfa(s)
        found = await finder.find(s, cid, spend=finder.Spend(), env=ENV)
        return cid, found, await s.read("site_candidates", cid)

    cid, found, candidates = run(go())
    assert "contacts" not in [t for t, _ in ai.asked]
    assert found.trust == "unconfirmed" and _links(db, cid) == []
    assert any("(unsettled)" in w for w in found.skipped)
    # Kept for the check, with the two it said yes to named (and only those).
    assert set(candidates) == {"alfa-dent-brasov.test", "alfa-dent-centru.test"}


def test_no_yes_links_nothing(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    _three(monkeypatch, {"alfa-dent.test": False, "alfa-dent-brasov.test": False,
                         "alfa-dent-centru.test": False})

    async def go():
        s = await _state(db)
        cid = await _alfa(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    assert found.website == "" and _links(db, cid) == []


def test_with_no_contacts_answer_a_chosen_site_links_only_official_contacts(tmp_path,
                                                                             monkeypatch):
    db = tmp_path / "harvey.db"
    dom = "hale-brasov.test"
    url = f"https://{dom}/"
    Fakes([_record(dom)], {dom: _draft(url, "Exemplu Construcții · Brașov · 0268 000 555",
                                       _c("phone", "+40268000555", url))}).install(monkeypatch)
    FakeClassifier(monkeypatch, site={"yes": True})      # contacts: no answer

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        return cid, await finder.find(s, cid, spend=finder.Spend(), env=ENV)

    cid, found = run(go())
    # The site is taken; its phone is no official one: nothing linked from it.
    assert (found.website, found.trust) == (dom, "likely") and _links(db, cid) == []
