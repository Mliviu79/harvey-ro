"""The contact finder's site read on local fixture sites (an English dental
practice and a Romanian clinic, tests/fixtures/sites): it follows the site's
own menu to the team, about and contact pages (six pages at most, ten
requests), finds the people named in team cards, sentences and schema.org
with the owner first, records LIKELY_OWNER, decodes obfuscated addresses and
learns how the company spells them; the address step uses what was learned
(a person's own published address is linked as it is, the others are built
the same way, a small company's first@ when nothing says otherwise); and the
Writer gets what the record says about the company. Every site, MX lookup
and verifier is a fake; all data is fictional."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import harvey.integrations.email_finder as ef
from harvey import finder, public_http
from harvey.agents.audience import Audience
from harvey.agents.writer import Writer
from harvey.browser import PageReader, Renderer
from harvey.collectors import people as people_mod
from harvey.collectors.profile import MAX_SITE_FETCHES, MAX_SITE_PAGES, site_draft
from harvey.models import Company
from harvey.modules import api, runs
from harvey.signals import sync_vocabulary
from harvey.state import StateManager, company_brief_lines

FIXTURES = Path(__file__).parent / "fixtures" / "sites"
SITES = {"harborlinedental.example": "harborline", "clinica-exemplu.ro": "clinica"}
EN, RO = "harborlinedental.example", "clinica-exemplu.ro"


def run(coro):
    return asyncio.run(coro)


def _rows(path, sql, args=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


class Web:
    """The fixture sites over a mock transport, plus any extra pages a test
    gives (url -> html or an httpx.Response); remembers every request."""

    def __init__(self, extra=None):
        self.extra = extra or {}
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.asked.append(url)
        if url in self.extra:
            page = self.extra[url]
            return page if isinstance(page, httpx.Response) else httpx.Response(
                200, text=page, headers={"content-type": "text/html"})
        host = request.url.host.removeprefix("www.")
        if request.url.scheme != "https" or host not in SITES:
            raise httpx.ConnectError("no such host")
        f = FIXTURES / SITES[host] / f"{request.url.path.strip('/') or 'index'}.html"
        if not f.exists():
            return httpx.Response(404, text="not here", headers={"content-type": "text/html"})
        return httpx.Response(200, text=f.read_text(encoding="utf-8"),
                              headers={"content-type": "text/html"})

    def paths(self, domain):
        return [u.split(domain, 1)[1] or "/" for u in self.asked if domain in u]


def _read(web, domain, wanted=(), location=""):
    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(web), follow_redirects=True)
        client.public_only = True
        ctx = api.Context(http=client, browser=PageReader(Renderer([])), wanted=frozenset(wanted),
                          settings={"contacts": True, "role_keywords": []})
        try:
            return await site_draft(api.Input(company_id="c1", name="x", domain=domain,
                                              locality=location), ctx)
        finally:
            await client.aclose()

    return run(go())


PEOPLE_SIGNALS = {"CONTACT_FOUND", "DECISION_MAKER_TITLE", "LIKELY_OWNER"}


# ── Following the menu ──


def test_the_english_site_is_read_through_its_own_menu():
    web = Web()
    draft = _read(web, EN, PEOPLE_SIGNALS)
    # Team, about and contact, as the menu names them; never services, the
    # blog or a guessed path.
    assert web.paths(EN) == ["/", "/meet-the-dentists/", "/about-us/", "/contact/"]
    assert [p.url for p in draft.pages] == [
        "https://harborlinedental.example", "https://harborlinedental.example/meet-the-dentists/",
        "https://harborlinedental.example/about-us/", "https://harborlinedental.example/contact/"]
    # The owner first: named in schema.org and "owned and operated by".
    people = [(p.first_name, p.last_name, p.fields.get("owner")) for p in draft.people]
    assert people[0] == ("Maren", "Calloway", True)
    assert {p[:2] for p in people} == {("Maren", "Calloway"), ("Lena", "Brightwater"),
                                        ("Theo", "Ashgrove")}
    [owner] = [f for f in draft.facts if f.code == "LIKELY_OWNER"]
    assert owner.value_num == 1 and owner.value_text.startswith("Maren Calloway — ")
    assert "owned and operated by" in owner.value_text
    assert (owner.module, owner.capability) == ("people", "find_people")
    assert api.undeclared(draft) == []


def test_obfuscated_and_plain_addresses_teach_the_pattern():
    draft = _read(Web(), EN)
    emails = {c.value for c in draft.contacts if c.kind == "email"}
    # maren@ is behind Cloudflare's email protection.
    assert emails == {"maren@harborlinedental.example", "lena@harborlinedental.example"}
    assert draft.values["email_pattern"] == {"pattern": "{f}", "confidence": 0.95,
                                             "source": "site_address"}
    # Nothing is recorded that wasn't confirmed.
    assert not any(f.code == "LIKELY_OWNER" for f in draft.facts)


def test_the_romanian_site_founder_comes_first():
    web = Web()
    draft = _read(web, RO, PEOPLE_SIGNALS, location="Brașov, Brașov, Romania")
    assert web.paths(RO) == ["/", "/echipa-noastra", "/despre-noi", "/contact"]
    assert [(p.first_name, p.last_name) for p in draft.people] == [
        ("Ioana", "Vrânceanu"), ("Mihai", "Olteanu")]
    assert draft.people[0].fields["owner"] is True
    emails = {c.value for c in draft.contacts if c.kind == "email"}
    # "ioana.vranceanu [at] clinica-exemplu [dot] ro"
    assert emails == {"ioana.vranceanu@clinica-exemplu.ro", "contact@clinica-exemplu.ro"}
    assert draft.values["email_pattern"]["pattern"] == "{f}.{l}"
    assert any(f.code == "LIKELY_OWNER" and "Ioana Vrânceanu" in f.value_text
               for f in draft.facts)
    kinds = {c.kind for c in draft.contacts}
    assert {"phone", "contact_form"} <= kinds


def test_no_more_than_six_pages_and_ten_requests():
    home = "".join(f'<a href="/team/member-{i}/">Team member {i}</a>' for i in range(15))
    extra = {"https://acme.example": home}
    extra.update({f"https://acme.example/team/member-{i}/": f"<p>member {i}</p>" * 80
                  for i in range(15)})
    web = Web(extra)
    draft = _read(web, "acme.example")
    assert len(draft.pages) <= MAX_SITE_PAGES
    assert len(web.asked) <= MAX_SITE_FETCHES


def test_a_link_off_site_or_back_home_is_not_read():
    home = '<a href="/team/">Team</a><a href="/about/">About</a><a href="/contact/">Contact</a>'
    extra = {
        "https://acme.example": home,
        # A franchisor's team page...
        "https://acme.example/team/": httpx.Response(
            302, headers={"location": "https://bigfranchise.example/team/"}),
        "https://bigfranchise.example/team/": "<h3>Franchise Boss</h3><p>CEO</p>",
        # ...and an About link that is really the homepage.
        "https://acme.example/about/": home,
        "https://acme.example/contact/": "<p>Call us</p>" * 60,
    }
    draft = _read(Web(extra), "acme.example")
    assert [p.url for p in draft.pages] == ["https://acme.example",
                                            "https://acme.example/contact/"]
    assert draft.people == []


SG_CHALLENGE = ('<html><head><meta http-equiv="refresh" '
                'content="0;/.well-known/sgcaptcha/?r=%2F"></meta></head></html>')


def test_a_bot_check_on_a_menu_page_stops_the_read():
    home = '<a href="/team/">Team</a><a href="/about/">About</a><a href="/contact/">Contact</a>'
    extra = {"https://acme.example": home,
             "https://acme.example/team/": httpx.Response(
                 202, text=SG_CHALLENGE, headers={"content-type": "text/html"})}
    web = Web(extra)
    draft = _read(web, "acme.example")
    # It blocks automated reading: nothing more is asked of it.
    assert web.paths("acme.example") == ["/", "/team/"]
    assert [p.url for p in draft.pages] == ["https://acme.example"]


def test_css_cards_are_only_a_fallback_and_reject_common_words():
    html = """<div class="team-member"><h3>Team of Experts</h3><p>Owner, CEO</p></div>
              <div class="team-member"><h4>Ana Ruiz</h4><span class="role">Owner</span></div>"""
    members = people_mod.people_from_cards([("https://acme.example/", html)])
    assert [(m["first_name"], m["last_name"], m["owner"]) for m in members] == [
        ("Ana", "Ruiz", True)]


# ── The finder end to end ──


@pytest.fixture
def no_network(monkeypatch):
    """Sites from the fixtures; a Google MX for every domain; no verifier
    or Hunter key (every built address stays a guess)."""
    web = Web()
    monkeypatch.setattr(public_http, "PublicHTTPTransport", lambda: httpx.MockTransport(web))
    monkeypatch.setenv("BROWSER_CDP_URL", "off")

    async def mx(domain):
        return "aspmx.l.google.com"

    async def boom(*a, **k):
        raise AssertionError("no paid or verification call in tests")

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "")
    for name in ("verify_reoon", "verify_zerobounce", "verify_hunter", "verify_email_smtp",
                 "_hunter_domain_pattern"):
        monkeypatch.setattr(ef, name, boom)
    return web


async def _state(path) -> StateManager:
    s = StateManager(str(path))
    await s.init_db()
    await sync_vocabulary(s)
    return s


def _addresses(db, cid):
    return sorted(_rows(db, "SELECT p.name, ct.value, ct.status, l.trust FROM company_contacts l "
                            "JOIN contacts ct ON ct.id = l.contact_id "
                            "JOIN people p ON p.id = l.person_id "
                            "WHERE l.company_id = ? AND ct.kind = 'email'", (cid,)))


def test_the_finder_links_published_addresses_and_builds_the_rest_the_same_way(
        tmp_path, no_network):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="HARBORLINE DENTAL", domain=EN,
                                          location="Port Ellery", source="discover:serper"))
        await s.set_signal_status("LIKELY_OWNER", "confirmed")
        found = await finder.find(s, cid, spend=finder.Spend(), env={})
        return cid, found

    cid, found = run(go())
    assert found.steps[0] == "site" and found.people == 3
    # The owner was saved first: the address step takes people in that order.
    assert _rows(db, "SELECT name, role FROM people ORDER BY id")[0] == (
        "Maren Calloway", "Owner")
    assert _addresses(db, cid) == [
        # Published on the site: theirs as it is, no check spent.
        ("Lena Brightwater", "lena@harborlinedental.example", "unchecked", "confirmed"),
        ("Maren Calloway", "maren@harborlinedental.example", "unchecked", "confirmed"),
        # Built the way the site spells addresses; no verifier confirmed
        # it, so a guess, and no surer than likely.
        ("Theo Ashgrove", "theo@harborlinedental.example", "guess", "likely"),
    ]
    assert _rows(db, "SELECT pattern, source FROM email_patterns WHERE domain = ?", (EN,)) == [
        ("{f}", "site_address")]
    owner = _rows(db, "SELECT value_text FROM observations WHERE signal_code = 'LIKELY_OWNER'")
    assert len(owner) == 1 and owner[0][0].startswith("Maren Calloway")
    # The read is a deeper one: it looked for the owner.
    assert _rows(db, "SELECT looked_for FROM searches WHERE module = 'website'") == [
        (runs.CONTACT_READ,)]
    assert "owner" in runs.CONTACT_READ.split(",") and finder.SITE_WANTS == "owner"


def test_the_romanian_clinic_gets_its_doctors_addresses(tmp_path, no_network):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="CLINICA EXEMPLU SRL", domain=RO,
                                          location="Brașov, Brașov, Romania",
                                          source="registry:onrc"))
        await finder.find(s, cid, spend=finder.Spend(), env={})
        return cid

    cid = run(go())
    assert _addresses(db, cid) == [
        ("Ioana Vrânceanu", "ioana.vranceanu@clinica-exemplu.ro", "unchecked", "confirmed"),
        ("Mihai Olteanu", "mihai.olteanu@clinica-exemplu.ro", "guess", "likely"),
    ]


def test_the_registrys_administrator_gets_the_address_the_site_publishes_for_them(
        tmp_path, no_network):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="CLINICA EXEMPLU SRL", domain=RO,
                                          location="Brașov, Brașov, Romania",
                                          source="registry:onrc"))
        await s.add_people(cid, [("VRÂNCEANU IOANA", "administrator")], source="onrc")
        await finder.find(s, cid, spend=finder.Spend(), env={})
        return cid

    cid = run(go())
    # The team page names her too: one person, the registry's entry linked.
    assert _addresses(db, cid) == [
        ("Mihai Olteanu", "mihai.olteanu@clinica-exemplu.ro", "guess", "likely"),
        ("VRÂNCEANU IOANA", "ioana.vranceanu@clinica-exemplu.ro", "unchecked", "confirmed"),
    ]


def test_a_site_read_before_the_menu_was_followed_is_read_once_more(tmp_path, no_network):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="HARBORLINE DENTAL", domain=EN))
        # What a finder read recorded before it followed the menu.
        await s.record_search("company", cid, "website", "read_site",
                              looked_for="phone,email,contact_form,social,people")
        before = await finder.next_free(s, 10)
        first = await finder.find(s, cid, spend=finder.Spend(cap=False), env={})
        # A company with a contact now: and a forced run's read of it
        # aside, the deeper read is never repeated.
        again = await finder.find(s, cid, spend=finder.Spend(cap=False), env={})
        return cid, before, first, again, await finder.next_free(s, 10)

    cid, before, first, again, after = run(go())
    assert before == [cid] and "site" in first.steps
    assert "site" not in again.steps and after == []


def _site(domain, html):
    return {f"https://{domain}": html}


@pytest.mark.parametrize("source,size,employees,want", [
    ("discover:serper", "", None, "john.doe@"),       # no size signal: first.last@
    ("discover:osm", "", None, "john@"),              # a local-business listing
    ("registry:onrc", "2-10", None, "john@"),         # its size on file
    ("registry:onrc", "", 7, "john@"),                # its financial statements
    ("registry:onrc", "", 140, "john.doe@"),
])
def test_a_small_company_is_guessed_first_at(tmp_path, no_network, source, size, employees,
                                             want):
    db = tmp_path / "harvey.db"
    no_network.extra.update(_site("acme-roofing.example",
                                  "<h3>John Doe</h3><p>Owner</p>" + "<p>Roofs.</p>" * 60))

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="ACME ROOFING", domain="acme-roofing.example",
                                          source=source, company_size=size))
        if employees is not None:
            await s.add_observations([{"company_id": cid, "signal_code": "EMPLOYEES",
                                       "value_num": employees, "value_text": "2025",
                                       "collector": "financials"}])
        await finder.find(s, cid, spend=finder.Spend(), env={})
        return cid

    cid = run(go())
    [(name, email, status, trust)] = _addresses(db, cid)
    assert email == f"{want}acme-roofing.example" and status == "guess" and trust == "likely"


def test_a_small_icp_allows_first_at(tmp_path, no_network):
    db = tmp_path / "harvey.db"
    no_network.extra.update(_site("acme-roofing.example",
                                  "<h3>John Doe</h3><p>Owner</p>" + "<p>Roofs.</p>" * 60))
    config = SimpleNamespace(icp=SimpleNamespace(company_size="1-10 employees", titles=[],
                                                 hiring_signals=[]))

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="ACME ROOFING", domain="acme-roofing.example"))
        await finder.find(s, cid, spend=finder.Spend(), env={}, config=config)
        return cid

    [(_, email, _, _)] = _addresses(db, run(go()))
    assert email == "john@acme-roofing.example"


def test_two_people_who_could_share_an_address_get_no_guess(tmp_path, no_network):
    db = tmp_path / "harvey.db"
    no_network.extra.update(_site("acme-roofing.example", (
        "<h3>John Smith</h3><p>Owner</p><h3>Jane Smith</h3><p>Owner</p>"
        '<a href="mailto:jsmith@acme-roofing.example">Email</a>' + "<p>Roofs.</p>" * 60)))

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="ACME ROOFING", domain="acme-roofing.example"))
        await finder.find(s, cid, spend=finder.Spend(), env={})
        return cid

    assert _addresses(db, run(go())) == []


# ── What reaches the Writer ──


def test_the_brief_says_what_the_record_knows():
    rows = [
        {"signal_code": "HIRING_ROLE", "value_num": None, "value_text": "Dental Hygienist",
         "observed_at": "2026-10-02"},
        {"signal_code": "HIRING_ROLE", "value_num": None, "value_text": "Receptionist",
         "observed_at": "2026-10-02"},
        {"signal_code": "TECH_STACK", "value_num": None, "value_text": "WordPress, Calendly",
         "observed_at": "2026-10-02"},
        {"signal_code": "RUNNING_GOOGLE_ADS", "value_num": 0, "value_text": "",
         "observed_at": "2026-10-02"},
        {"signal_code": "HIRING_ROLE", "value_num": None, "value_text": "Closed last month",
         "observed_at": "2026-09-01"},
        {"signal_code": "EMPLOYEES", "value_num": 9, "value_text": "2024",
         "observed_at": "2026-09-01"},
        {"signal_code": "EMPLOYEES", "value_num": 11, "value_text": "2025",
         "observed_at": "2026-09-01"},
    ]
    assert company_brief_lines("Brașov, Brașov, Romania", "Dental practice activities", rows) == [
        "Based in Brașov", "Registered activity: Dental practice activities",
        "Tools on their website: WordPress, Calendly",
        "Hiring: Dental Hygienist; Receptionist", "11 employees (2025 accounts)"]


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "quick one", "body": "Hi"}


def test_the_audience_and_the_writer_get_the_companys_facts(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="Clinica Exemplu", domain=RO,
                                          location="Brașov, Brașov, Romania",
                                          industry="Dental practice"))
        await s.add_observations([
            {"company_id": cid, "signal_code": "TECH_STACK", "value_text": "WordPress",
             "collector": "profile"},
            {"company_id": cid, "signal_code": "HIRING_ROLE", "value_text": "Asistentă medicală",
             "collector": "profile"},
        ])
        from harvey import contacts
        await s.add_contacts(cid, [contacts.Found(
            "email", "ioana.vranceanu@clinica-exemplu.ro", status="verified",
            person="Ioana Vrânceanu", role="Medic primar stomatolog, fondator",
            source="website", trust="confirmed")])
        made = await Audience(s, env={}).run()
        [p] = await s.get_prospects_by_status("new")
        brain = RecordingBrain()
        config = SimpleNamespace(
            product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
            persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
            channels=SimpleNamespace(email=SimpleNamespace(provider="smtp")),
            humanizer=SimpleNamespace(enabled=False))
        w = Writer(brain, s, config)
        w.skills = ""
        await w._write_personal_email(p)
        return made, p, brain.prompts[0]

    made, p, prompt = run(go())
    assert made == 1
    assert p.title == "Medic primar stomatolog, fondator"
    assert p.personalization_notes == ("Based in Brașov. Tools on their website: WordPress. "
                                       "Hiring: Asistentă medicală")
    for line in ("- Title: Medic primar stomatolog, fondator", "- Industry: Dental practice",
                 "- Based in Brașov", "- Tools on their website: WordPress",
                 "- Hiring: Asistentă medicală"):
        assert line in prompt
    # Said once, not again as research notes.
    assert "Research notes" not in prompt


# ── Regressions ──

FILLER = "<p>Roofs and gutters.</p>" * 60


def test_a_built_guess_is_never_passed_off_as_a_published_address(tmp_path, no_network):
    """A guess built for the registry's administrator is on file at the
    domain; a forced run (Search again) must not take it for an address the
    site published for the team page's reading of the same person, nor
    learn a pattern from it."""
    from datetime import datetime, timezone

    db = tmp_path / "harvey.db"
    no_network.extra.update(_site("acme-roofing.example",
                                  "<h3>Mihai Olteanu</h3><p>Owner</p>" + FILLER))

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="ACME ROOFING SRL", domain="acme-roofing.example",
                                          source="registry:onrc"))
        await s.add_people(cid, [("OLTEANU MIHAI", "administrator")], source="onrc")
        await finder.find(s, cid, spend=finder.Spend(), env={})
        first = _addresses(db, cid)
        published = await finder._emails_at(s, cid, "acme-roofing.example")
        await asyncio.sleep(1.1)
        since = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await finder.find(s, cid, spend=finder.Spend(), env={}, since=since)
        return cid, first, published

    cid, first, published = run(go())
    assert first == [("OLTEANU MIHAI", "mihai.olteanu@acme-roofing.example", "guess", "likely")]
    assert published == []
    links = _rows(db, "SELECT ct.status, l.trust, l.source FROM company_contacts l "
                      "JOIN contacts ct ON ct.id = l.contact_id WHERE l.company_id = ?", (cid,))
    assert not any(st == "guess" and t == "confirmed" for st, t, _ in links), links
    assert not any(st == "guess" and src == "website" for st, _, src in links), links


def test_published_addresses_exclude_invalid_ones(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await s.add_company(Company(name="ACME", domain="acme-roofing.example"))
        from harvey.modules import ingest
        await ingest.person_contacts(s, cid, "Ana Pop", "", [
            api.Contact("email", "ana.pop@acme-roofing.example", status="invalid",
                        module="website", capability="read_site")], trust="confirmed")
        await ingest.person_contacts(s, cid, "Dan Pop", "", [
            api.Contact("email", "dan@acme-roofing.example", status="unchecked",
                        module="website", capability="read_site")], trust="confirmed")
        return await finder._emails_at(s, cid, "acme-roofing.example")

    assert run(go()) == ["dan@acme-roofing.example"]


def test_relative_menu_links_resolve_against_the_page_the_homepage_landed_on():
    """A homepage that redirects to /ro/ with relative menu links: the
    team is at /ro/echipa/, not /echipa/."""
    home = ('<nav><a href="echipa/">Echipa</a> <a href="despre-noi/">Despre noi</a>'
            '<a href="contact/">Contact</a></nav>' + FILLER)
    team = "<h3>Dr. Ioana Vrânceanu</h3><p>Medic primar stomatolog, fondator</p>" + FILLER
    asked = []

    def handler(request):
        url = str(request.url)
        asked.append(url)
        if url in ("https://zambet.test", "https://zambet.test/"):
            return httpx.Response(301, headers={"location": "https://zambet.test/ro/"})
        if url == "https://zambet.test/ro/":
            return httpx.Response(200, text=home, headers={"content-type": "text/html"})
        if url == "https://zambet.test/ro/echipa/":
            return httpx.Response(200, text=team, headers={"content-type": "text/html"})
        return httpx.Response(404, text="nu", headers={"content-type": "text/html"})

    draft = _read(handler, "zambet.test", PEOPLE_SIGNALS)
    assert "https://zambet.test/ro/echipa/" in asked
    assert "https://zambet.test/echipa/" not in asked
    assert asked.count("https://zambet.test/ro/") == 1      # the homepage is read once
    assert [(p.first_name, p.last_name) for p in draft.people] == [("Ioana", "Vrânceanu")]


def test_the_same_page_in_another_language_is_read_once():
    html = ('<a href="/en/team">Team</a><a href="/de/team">Team</a><a href="/fr/team">Team</a>'
            '<a href="/en/contact">Contact</a><a href="/de/contact">Kontakt</a>')
    assert people_mod.menu_links(html, "https://acme.example/", limit=5) == [
        "https://acme.example/en/team", "https://acme.example/en/contact"]
    # The homepage's own language first.
    assert people_mod.menu_links(html, "https://acme.example/de/", limit=5) == [
        "https://acme.example/de/team", "https://acme.example/de/contact"]


def test_a_huge_menu_and_a_language_switcher_stay_in_budget():
    links = "".join(f'<a href="/en/team-{i}">Team {i}</a><a href="/ro/echipa-{i}">Echipa</a>'
                    f'<a href="/de/team-{i}">Team</a>' for i in range(200))
    links += ('<a href="mailto:a@acme.example">Mail</a><a href="tel:+40">Tel</a>'
              '<a href="javascript:void(0)">Team</a><a href="https://other.example/team">Team</a>')
    home = links + FILLER
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, text=home + f"<p>{len(asked)}</p>",
                              headers={"content-type": "text/html"})

    draft = _read(handler, "big.example", PEOPLE_SIGNALS)
    assert len(asked) <= MAX_SITE_FETCHES and len(draft.pages) <= MAX_SITE_PAGES
    assert not any("other.example" in u or "mailto" in u or "javascript" in u for u in asked)
    assert not any("/de/team-0" in u for u in asked)        # /en/team-0 read instead


def test_a_shallow_read_site_with_a_contact_gets_the_deeper_read_once(tmp_path, no_network):
    """A site the finder read the shallow way found a phone; the background
    still reads it the deeper way once, after the companies with no contact."""
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="HARBORLINE DENTAL", domain=EN))
        other = await s.add_company(Company(name="ACME ROOFING", domain="acme-roofing.example"))
        plain = await s.add_company(Company(name="PLAIN READ", domain="plain.example"))
        from harvey import contacts
        for c, phone in ((cid, "+40268000111"), (plain, "+40268000222")):
            await s.add_contacts(c, [contacts.Found("phone", phone, source="website",
                                                    capability="read_site",
                                                    trust="confirmed")])
        await s.record_search("company", cid, "website", "read_site",
                              looked_for="phone,email,contact_form,social,people")
        # A plain "Read sites" read never looked for emails: not the finder's.
        await s.record_search("company", plain, "website", "read_site",
                              looked_for="phone,contact_form")
        before = await finder.next_free(s, 10)
        first = await finder.find(s, cid, spend=finder.Spend(cap=False), env={})
        return cid, other, before, first, await finder.next_free(s, 10)

    cid, other, before, first, after = run(go())
    assert before == [other, cid]
    assert "site" in first.steps
    assert cid not in after


def test_a_catch_all_address_is_no_surer_than_likely():
    risky = [api.Contact("email", "a@acme.example", status="risky")]
    verified = [api.Contact("email", "a@acme.example", status="verified")]
    assert finder._guess_trust(risky, "confirmed") == "likely"
    assert finder._guess_trust(verified, "confirmed") == "confirmed"
    assert finder._guess_trust(risky, "unconfirmed") == "unconfirmed"
