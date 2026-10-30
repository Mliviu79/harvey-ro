"""Romanian practice sites: team cards, booking wording and front-desk inboxes."""

from types import SimpleNamespace

import pytest

from harvey.agents.scout import (
    FALLBACK_SITE_PATHS, INBOX_PREFERRED, MAX_SITE_FETCHES, Scout,
)
from harvey.collectors import people as people_mod
from harvey.collectors.profile import (
    BOOKING_HINTS, RO_TEAM_PATHS, TEAM_PATHS, ProfileCollector, _reads_like_title, title_looks_like_role,
)
from harvey.models.company import Company
from harvey.signals import sync_vocabulary
from tests.test_collectors_profile import state  # noqa: F401  (fixture)
from tests.test_company_inbox import make_scout


def collector():
    return ProfileCollector.__new__(ProfileCollector)


# ── people ──

def test_romanian_team_page():
    html = """<div>
      <h3>Dr. Ștefan Mureșan</h3><p>Medic stomatolog</p>
      <h3>Prof. Dr. Ioana Țărână</h3><p>Medic coordonator</p>
      <h3>Dr. Elena Bălan</h3><p>Medic rezident</p>
      <h3>Programări online</h3><p>Sunați-ne de luni până vineri</p>
    </div>"""
    people = collector().detect_people(html)
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Ștefan", "Mureșan", "Medic stomatolog"),
        ("Ioana", "Țărână", "Medic coordonator"),
        ("Elena", "Bălan", "Medic rezident"),
    ]
    assert people[0]["full_name"] == "Ștefan Mureșan"
    assert people[1]["full_name"] == "Ioana Țărână"
    assert [p["is_decision_maker"] for p in people] == [False, True, False]


SENTENCE_CASE_CARDS = """<div>
  <div><h3>Dr. Ștefan Mureșan</h3><p>Fondator, medic primar stomatolog</p></div>
  <div><h3>Dr. Radu Câmpeanu</h3><p>Medic specialist chirurgie orală</p></div>
  <div><h3>Dr. Ana Pop</h3><p>Medic primar medicină de familie</p></div>
  <div><h3>Dr. Mihai Șerban</h3><p>Medic primar</p></div>
</div>"""
SENTENCE_CASE_PEOPLE = [
    ("Ștefan", "Mureșan", "Fondator, medic primar stomatolog"),
    ("Radu", "Câmpeanu", "Medic specialist chirurgie orală"),
    ("Ana", "Pop", "Medic primar medicină de familie"),
    ("Mihai", "Șerban", "Medic primar"),
]


def test_sentence_case_titles_keep_their_cards():
    people = collector().detect_people(SENTENCE_CASE_CARDS, domain="clinica-exemplu.ro")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == SENTENCE_CASE_PEOPLE


def test_sentence_case_titles_in_find_people():
    people = people_mod.find_people([("https://clinica-exemplu.ro/echipa", SENTENCE_CASE_CARDS)],
                                    "clinica-exemplu.ro", "Clinica Exemplu")
    assert sorted((p["first_name"], p["last_name"], p["title"]) for p in people) \
        == sorted(SENTENCE_CASE_PEOPLE)


@pytest.mark.parametrize("text", [
    # a Romanian role word buys sentence case, nothing else does
    "Specialist in roofing with over twenty years of hands on experience",
    "Director with over twenty years in the industry",
    "Medic primar și chirurg cu peste douăzeci de ani de experiență în implantologie",
])
def test_long_lowercase_text_is_still_not_a_title(text):
    assert not _reads_like_title(text)


@pytest.mark.parametrize("title", [
    "Medic stomatolog", "Medic primar", "Medic specialist ortodonție",
    "Medic specialist în ortodonție", "Director medical", "Administrator",
    "Farmacistă", "Fondatoare", "Proprietar", "Kinetoterapeut", "Fizioterapeută",
    "Psiholog", "Ortodont", "Asistent medical",
])
def test_romanian_titles_read_as_roles(title):
    assert _reads_like_title(title)


@pytest.mark.parametrize("text", [
    "Medicină dentară", "Medicamente", "Asistență rutieră", "Programări online",
])
def test_word_boundaries_hold_with_diacritics(text):
    # \b is Unicode-aware: "medic" is not inside "Medicină", "asistent" not in "Asistență"
    assert not title_looks_like_role(text)


@pytest.mark.parametrize("title, owner", [
    ("Fondator", True), ("Proprietară", True), ("Director medical", True),
    ("Medic coordonatoare", True), ("Administrator", False), ("Medic primar", False),
])
def test_romanian_decision_makers(title, owner):
    html = f"<h3>Dr. Andrei Pop</h3><p>{title}</p>"
    assert collector().detect_people(html)[0]["is_decision_maker"] is owner


@pytest.mark.parametrize("heading", ["Igienizare Profesională", "Ecografie Doppler", "Victoria Dent"])
def test_service_card_with_medical_title_is_nobody(heading):
    html = f"<div><h3>{heading}</h3><p>Medic primar stomatolog</p></div>"
    assert collector().detect_people(html, domain="clinica-exemplu.ro") == []


@pytest.mark.parametrize("html, expected", [
    ("<h3>Dr. Ana Pop</h3><p>Medic primar</p>", ("Ana", "Pop", "Medic primar")),
    ("<h3>Prof. Dr. Ana Pop</h3><p>Medic primar</p>", ("Ana", "Pop", "Medic primar")),
    ("<h3>Andrei Popa</h3><p>Fondator</p>", ("Andrei", "Popa", "Fondator")),
])
def test_medical_title_with_honorific_or_owner_title_is_kept(html, expected):
    people = collector().detect_people(html, domain="clinica-exemplu.ro")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [expected]


def test_english_cards_need_no_honorific():
    html = """<div><h3>Dale Whitlock</h3><p>Owner</p>
      <h3>Maria Lopez</h3><p>Office Manager</p>
      <h3>Dr. Sam Reed</h3><p>Dentist</p></div>"""
    people = collector().detect_people(html, domain="acme.com")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Dale", "Whitlock", "Owner"), ("Maria", "Lopez", "Office Manager"),
        ("Sam", "Reed", "Dentist")]


# ── booking ──

@pytest.mark.parametrize("html", [
    "<a href='/programari-online'>Rezervă</a>", "<button>Programări online</button>",
    "<a>Programare online</a>", "<a>Programarea online</a>", "<a href='/programeaza-te-online'>x</a>",
    "<a>Programează-te online</a>", "<a>Programează-te acum online</a>",
    "<a>Fă o programare online</a>", "<a href='/fa_o_programare_online'>x</a>", "<b>REZERVĂ ONLINE</b>",
    "<a>Programări&nbsp;online</a>", "<a>Programări&#160;online</a>",
    "<a>Fă&nbsp;o&nbsp;programare&nbsp;online</a>",
    "<a>Programează o consultație online</a>", "<a href='/programeaza-o-consultatie-online'>x</a>",
    "<a>Programează&nbsp;o&nbsp;consultaţie&nbsp;online</a>",
])
def test_romanian_booking_wording(html):
    assert ProfileCollector._any(html, BOOKING_HINTS)


@pytest.mark.parametrize("html", [
    "<p>Programul cabinetului: luni-vineri</p>", "<p>Plată online</p>", "<p>Contact</p>",
    # booking by phone is not online booking
    "<p>Programează-te telefonic la 0712 000 000</p>", "<a href='/programeaza-te'>x</a>",
    "<a>Fă o programare</a>", "<p>Fă o programare la 0712 000 000</p>",
    "<p>Programează o consultație la 0712 000 000</p>",
])
def test_no_booking_wording(html):
    assert not ProfileCollector._any(html, BOOKING_HINTS)


# ── inbox ──

@pytest.mark.asyncio
async def test_practice_mailbox_beats_front_desk():
    pages = {"https://acme.com": "programari@acme.com receptie@acme.com contact@acme.com",
             "https://acme.com/contact": "cabinet@acme.com"}
    assert await make_scout(pages)._find_inbox_address("acme.com") == "cabinet@acme.com"


@pytest.mark.asyncio
async def test_front_desk_inbox_is_used():
    pages = {"https://acme.com": "Dr. Ana Pop: ana.pop@acme.com, programari@acme.com"}
    assert await make_scout(pages)._find_inbox_address("acme.com") == "programari@acme.com"


@pytest.mark.asyncio
async def test_pharmacy_mailbox_beats_front_desk():
    pages = {"https://acme.com": "programare@acme.com info@acme.com farmacia@acme.com"}
    assert await make_scout(pages)._find_inbox_address("acme.com") == "farmacia@acme.com"


@pytest.mark.asyncio
async def test_singular_booking_inbox_is_used():
    pages = {"https://acme.com": "Dr. Ana Pop: ana.pop@acme.com, programare@acme.com"}
    assert await make_scout(pages)._find_inbox_address("acme.com") == "programare@acme.com"


def test_english_inbox_order_is_unchanged():
    english = [a for a in INBOX_PREFERRED if a in (
        "owner", "office", "info", "contact", "contactus", "hello", "team", "service",
        "sales", "estimates", "quotes", "inquiries", "enquiries", "admin", "support")]
    assert english == ["owner", "office", "info", "contact", "contactus", "hello", "team",
                       "service", "sales", "estimates", "quotes", "inquiries", "enquiries",
                       "admin", "support"]


# ── fallback pages ──

def fetch_recorder(domain):
    """A Scout whose homepage links nowhere, recording every page it requests."""
    scout = Scout.__new__(Scout)
    base, requested = f"https://{domain}", []

    async def resolve(d, retries=1):
        return base, SimpleNamespace(text="<html><body>Bine ați venit</body></html>", url=base)

    async def fetch(url, **kw):
        requested.append(url)
        return None

    scout._resolve_base_url = resolve
    scout._fetch = fetch
    return scout, base, requested


@pytest.mark.asyncio
async def test_english_site_fallback_requests_are_unchanged():
    scout, base, requested = fetch_recorder("acme.com")
    await scout._read_site("acme.com")
    assert requested == [f"{base}{p}" for p in FALLBACK_SITE_PATHS]


@pytest.mark.asyncio
async def test_romanian_site_tries_romanian_pages_first():
    scout, base, requested = fetch_recorder("clinica-exemplu.ro")
    await scout._read_site("clinica-exemplu.ro")
    assert requested == [f"{base}{p}" for p in (
        "/despre-noi", "/echipa", "/contact",
        "/about", "/about-us", "/team", "/our-team", "/contact-us")]
    assert len(requested) + 1 <= MAX_SITE_FETCHES     # + the homepage


async def team_requests(state, domain):  # noqa: F811
    await sync_vocabulary(state)
    await state.set_signal_status("CONTACT_FOUND", "confirmed")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.known = {"CONTACT_FOUND"}, {}, state
    requested = []

    async def fake_get(url):
        requested.append(url)
        return "<html>home</html>"
    c._get = fake_get
    cid = await state.add_company(Company(name="Exemplu", domain=domain))
    await c.profile_company(cid, domain)
    team = set(TEAM_PATHS) | set(RO_TEAM_PATHS) | {"/contact"}
    return [u.split(domain, 1)[1] for u in requested if u.split(domain, 1)[1] in team]


@pytest.mark.asyncio
async def test_english_team_paths_are_unchanged(state):  # noqa: F811
    assert await team_requests(state, "acme.com") == list(TEAM_PATHS)


@pytest.mark.asyncio
async def test_romanian_team_paths_go_first(state):  # noqa: F811
    assert await team_requests(state, "clinica-exemplu.ro") \
        == ["/echipa", "/echipa-noastra", "/medici", "/despre-noi", *TEAM_PATHS, "/contact"]


@pytest.mark.asyncio
async def test_romanian_team_page_is_read_before_contact(state):  # noqa: F811
    await sync_vocabulary(state)
    await state.set_signal_status("CONTACT_FOUND", "confirmed")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.known = {"CONTACT_FOUND"}, {}, state
    pad = "<p>" + "Bine ați venit. " * 40 + "</p>"
    pages = {"": "<html>home</html>",
             "/contact": f"<html>{pad}<p>Telefon 0712 000 000</p></html>",
             "/medici": f"<html>{pad}<h3>Dr. Ana Pop</h3><p>Medic primar</p></html>"}

    async def fake_get(url):
        return pages.get(url.split("clinica-exemplu.ro", 1)[1].rstrip("/"), "")
    c._get = fake_get
    cid = await state.add_company(Company(name="Exemplu", domain="clinica-exemplu.ro"))
    obs = await c.profile_company(cid, "clinica-exemplu.ro")
    assert [(o["value_text"], o["evidence_url"]) for o in obs if o["signal_code"] == "CONTACT_FOUND"] \
        == [("Ana Pop — Medic primar", "https://clinica-exemplu.ro/medici")]
