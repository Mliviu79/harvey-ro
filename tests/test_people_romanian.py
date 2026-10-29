"""Romanian practice sites: names with diacritics, stacked honorifics,
Romanian menu links and .ro addresses. All names and domains are fictional."""

import re

import pytest

from harvey.collectors import people
from harvey.integrations.email_finder import build_email


# ── names ──

@pytest.mark.parametrize("text, expected", [
    ("Ștefan Mureșan", ("Ștefan", "Mureșan")),            # comma-below ș
    ("Ştefan Mureşan", ("Ştefan", "Mureşan")),            # cedilla ş
    ("Mădălina Pătrașcu", ("Mădălina", "Pătrașcu")),
    ("Ioana Țurcanu", ("Ioana", "Țurcanu")),              # comma-below Ț
    ("Ioana Ţurcanu", ("Ioana", "Ţurcanu")),              # cedilla Ţ
    ("Dr. Andreea Bălănescu-Popa", ("Andreea", "Bălănescu-Popa")),
    ("Prof. Dr. Ion Vasilescu", ("Ion", "Vasilescu")),
    ("Conf. univ. dr. Irina Dobrescu", ("Irina", "Dobrescu")),
    ("Dr Drew Halvorsen", ("Drew", "Halvorsen")),          # "Drew" is not "Dr"
])
def test_romanian_names_parse(text, expected):
    assert people.split_person_name(text) == expected


@pytest.mark.parametrize("text", [
    "ȘTEFAN MUREȘAN",          # all caps
    "Ștefan MUREȘAN",
    "Ștefan Mureșan 2",
    "Echipa Noastră Medicală",
    "Medicii Noștri",
    "Despre Noi",
    "Programează Acum",
    "Meet The Team",
    "Prof. Dr.",
])
def test_rejections_still_hold(text):
    assert not people.is_person_name(text)


def test_name_classes_cover_romanian_letters():
    upper = re.compile(f"^[{people.NAME_UPPER}]+$")
    lower = re.compile(f"^[{people.NAME_LOWER}]+$")
    assert upper.match("AZĂÂÎȘŞȚŢÉ") and not upper.match("ă")
    assert lower.match("azăâîșşțţé") and not lower.match("Ă")
    assert not upper.match("×") and not lower.match("÷")


@pytest.mark.parametrize("text", [
    "Chirurgie Orală", "Parodontologie Laser", "Program Lucru", "Program Luni",
    "Urgențe Stomatologice", "Ortodonție Copii", "Întrebări Frecvente", "Politica Cookies",
    "Termeni Condiții", "Endodonție Microscop", "Galerie Foto", "Contactează Ne",
    "Farmacist Șef", "Valorile Clinicii", "Tehnologie Modernă", "Strada Memorandumului",
    "Bulevardul Unirii", "Prima Consultație", "Radiologie Digitală", "Fațete Ceramice",
    "Proteze Dentare",
])
def test_romanian_headings_are_not_names(text):
    assert not people.is_person_name(text)


@pytest.mark.parametrize("text, expected", [
    ("Ștefan Mureșan", ("Ștefan", "Mureșan")),
    ("Radu Câmpeanu", ("Radu", "Câmpeanu")),
    ("Ana Pop", ("Ana", "Pop")),
    ("Mihai Șerban", ("Mihai", "Șerban")),
    ("Bogdan Ilieș", ("Bogdan", "Ilieș")),
    ("Vlad Stănescu", ("Vlad", "Stănescu")),
    # Words left out of the heading vocabulary because people are named them
    ("Louis Prima", ("Louis", "Prima")),
    ("Joi Hartwell", ("Joi", "Hartwell")),
    ("Marti Noxwell", ("Marti", "Noxwell")),
    ("Dieter Laser", ("Dieter", "Laser")),
])
def test_names_near_the_heading_vocabulary_still_parse(text, expected):
    assert people.split_person_name(text) == expected


def test_heading_cards_yield_no_people():
    html = """<div class="card"><h3>Chirurgie Orală</h3><p>Medic specialist</p></div>
      <div class="card"><h3>Urgențe Stomatologice</h3><p>Medic de gardă</p></div>
      <p>Program Luni<br>Medic de gardă</p>
      <div class="card"><h3>Dr. Ioana Țurcanu</h3><p>Medic coordonator</p></div>"""
    found = people.people_from_lines(html, "clinicaexemplu.test")
    assert [(p["first_name"], p["last_name"]) for p in found] == [("Ioana", "Țurcanu")]


# ── titles ──

@pytest.mark.parametrize("title", ["Fondator", "Fondatoare", "Co-fondator", "Cofondator",
                                   "Proprietar", "Proprietară", "Fondator, medic primar stomatolog"])
def test_romanian_owner_titles(title):
    assert people.is_owner_title(title)
    assert people.owner_rank(title) >= 95
    assert people.title_matches(title, ["Owner"])


def test_proprietar_ranks_with_proprietor():
    assert people.owner_rank("Proprietar") == people.owner_rank("Proprietor") == 100


@pytest.mark.parametrize("title", ["Proprietary Software Lead", "Proprietary Trading"])
def test_proprietary_is_not_an_owner(title):
    assert not people.is_owner_title(title)


@pytest.mark.parametrize("line, expected", [
    ("Dr. Radu Câmpeanu — Medic primar stomatolog", ("Radu", "Câmpeanu", "Medic primar stomatolog")),
    ("Dr. Mihai Șerban, Medic specialist chirurgie orală",
     ("Mihai", "Șerban", "Medic specialist chirurgie orală")),
    ("Vlad Stănescu | Fondator", ("Vlad", "Stănescu", "Fondator")),
    # Hyphenated names, and "Dr." kept out of the full name
    ("Dr. Ana-Maria Bălănescu — Medic primar", ("Ana-Maria", "Bălănescu", "Medic primar")),
    ("Ioan-Cristian Țăranu, Fondator", ("Ioan-Cristian", "Țăranu", "Fondator")),
    ("Prof. Dr. Raluca Ștefănescu-Pop | Medic primar", ("Raluca", "Ștefănescu-Pop", "Medic primar")),
    ("Mary-Kate Olsen — Office Manager", ("Mary-Kate", "Olsen", "Office Manager")),
    ("Mary-Kate Olsen - Office Manager", ("Mary-Kate", "Olsen", "Office Manager")),
])
def test_romanian_roles_on_one_line(line, expected):
    found = people.people_from_lines(f"<ul><li>{line}</li></ul>", "clinicaexemplu.test")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in found] == [expected]
    assert found[0]["full_name"] == f"{expected[0]} {expected[1]}"


# ── a Romanian-only title needs a Dr./Prof./Conf. name ──

_SERVICE_HEADINGS = ["Igienizare Profesională", "Tratamente Endodontice", "Sedare Conștientă",
                     "Medicină Internă", "Ecografie Doppler", "Victoria Dent"]


@pytest.mark.parametrize("heading", _SERVICE_HEADINGS)
@pytest.mark.parametrize("title", ["Medic primar stomatolog", "Medic specialist ATI"])
def test_service_heading_over_romanian_title_is_nobody(heading, title):
    html = f'<div class="card"><h3>{heading}</h3><p>{title}</p></div>' \
           f"<ul><li>{heading} — {title}</li><li>{heading}, {title}</li></ul>"
    assert people.people_from_lines(html, "clinicaexemplu.test") == []


@pytest.mark.parametrize("html, expected", [
    ('<div class="card"><h3>Dr. Ana Pop</h3><p>Medic primar</p></div>', ("Ana", "Pop", "Ana Pop")),
    ('<div class="card"><h3>Prof. Dr. Ion Vasilescu</h3><p>Medic primar</p></div>',
     ("Ion", "Vasilescu", "Ion Vasilescu")),
    ('<div class="card"><h3>Conf. univ. dr. Irina Dobrescu</h3><p>Medic primar</p></div>',
     ("Irina", "Dobrescu", "Irina Dobrescu")),
    # An owner title needs no honorific
    ('<div class="card"><h3>Andrei Popa</h3><p>Fondator</p></div>', ("Andrei", "Popa", "Andrei Popa")),
    ('<p>Elena Dumitru<br>Proprietară</p>', ("Elena", "Dumitru", "Elena Dumitru")),
    # Nor does an English role word
    ('<p>Calvin Marsh<br>Office Manager</p>', ("Calvin", "Marsh", "Calvin Marsh")),
    ('<div class="card"><h3>Dr. Emily Stone</h3><p>Dentist</p></div>', ("Emily", "Stone", "Emily Stone")),
])
def test_cards_that_are_people(html, expected):
    found = people.people_from_lines(html, "clinicaexemplu.test")
    assert [(p["first_name"], p["last_name"], p["full_name"]) for p in found] == [expected]


def test_romanian_only_title_without_honorific_is_dropped():
    # A pharmacist or nurse rarely carries "Dr.": the cost of the rule.
    html = "<p>Ana Pop<br>Farmacist șef</p><ul><li>Ana Pop, Farmacist șef</li></ul>"
    assert people.people_from_lines(html, "clinicaexemplu.test") == []


@pytest.mark.parametrize("title, needs", [
    ("Medic primar", True), ("Medic specialist ATI", True), ("Medic coordonator", True),
    ("Asistentă medicală", True), ("Farmacistă", True), ("Kinetoterapeut", True),
    ("Psiholog", True), ("Coordonatoare", True), ("Ortodont", True),
    ("Fondator", False), ("Cofondator, medic primar", False), ("Proprietară", False),
    ("Medic primar, DDS", False), ("Office Manager", False), ("Specialist", False),
    ("Dentist", False), ("", False),
])
def test_romanian_title_needs_honorific(title, needs):
    assert people.romanian_title_needs_honorific(title) is needs


@pytest.mark.parametrize("text", ["Sfântul Andrei", "Sfânta Maria", "Sf Andrei"])
def test_saints_are_not_names(text):
    assert not people.is_person_name(text)
    html = f'<div class="card"><h3>{text}</h3><p>Fondator</p></div>'
    assert people.people_from_lines(html, "clinicaexemplu.test") == []


# ── prose ──

@pytest.mark.parametrize("html, expected", [
    ("<p>Clinica a fost fondată de Dr. Ioana Țurcanu în 2009.</p>", ("Ioana", "Țurcanu")),
    ("<p>Ioana Țurcanu a fondat clinica în 2009.</p>", ("Ioana", "Țurcanu")),
    ("<p>Fondatorul clinicii, Dr. Vlad Stănescu, este medic primar.</p>", ("Vlad", "Stănescu")),
    ("<p>Fondatoarea cabinetului, Prof. Dr. Ana Pop, are 30 de ani de experiență.</p>", ("Ana", "Pop")),
    ("<p>Cabinetul a fost înființat de Conf. univ. dr. Bogdan Ilieș.</p>", ("Bogdan", "Ilieș")),
    ("<p>Clinica a fost infiintata in 2001 de Dr. Radu Câmpeanu.</p>", ("Radu", "Câmpeanu")),
])
def test_romanian_founder_in_prose(html, expected):
    found = people.people_from_prose(html, "clinicaexemplu.test")
    assert [(p["first_name"], p["last_name"], p["title"], p["owner"]) for p in found] \
        == [(*expected, "Founder", True)]


def test_romanian_prose_patterns_stay_linear():
    import time
    text = "<p>" + "Fondatorul clinicii, " + " ".join(["Abcde"] * 3000) + " a fondat</p>"
    start = time.perf_counter()
    people.people_from_prose(text, "clinicaexemplu.test")
    assert time.perf_counter() - start < 2


def test_administrator_is_not_an_owner():
    html = "<p>Administrator: Radu Ilinescu.</p>"
    assert all(not p["owner"] for p in people.people_from_prose(html, "clinicaexemplu.test"))


def test_surname_on_the_door_folds_accents():
    assert people.surname_on_the_door("Mureșan", "muresandental.test")


# ── which pages to read ──

def test_romanian_menu_links():
    html = """
      <a href="/echipa-noastra/">Echipa noastră</a>
      <a href="/echip%C4%83/">x</a>
      <a href="/despre-noi">Despre noi</a>
      <a href="/contacteaza-ne">Contactează-ne</a>
      <a href="/servicii/implant">Implant dentar</a>
      <a href="/pagina-7">Medicii noștri</a>
    """
    links = people.menu_links(html, "https://clinicaexemplu.test/", limit=10)
    assert set(links[:3]) == {"https://clinicaexemplu.test/echipa-noastra/",
                              "https://clinicaexemplu.test/echip%C4%83/",
                              "https://clinicaexemplu.test/pagina-7"}
    assert links[3:] == ["https://clinicaexemplu.test/despre-noi",
                         "https://clinicaexemplu.test/contacteaza-ne"]


def test_personal_is_not_personal_injury():
    html = """
      <a href="/personal-injury/">Personal Injury</a>
      <a href="/practice-areas/personal-injury-claims">Personal Injury Claims</a>
      <a href="/personal/">Personal</a>
      <a href="/banking/personal">Personal</a>
      <a href="/staff-medical">Personal medical</a>
      <a href="/personalul">Personalul</a>
      <a href="/personal-medical">Medici</a>
    """
    links = people.menu_links(html, "https://exemplu.test/", limit=10)
    assert sorted(links) == ["https://exemplu.test/personal-medical",
                             "https://exemplu.test/personalul",
                             "https://exemplu.test/staff-medical"]


def test_doctorii_and_specialistii_nostri_labels():
    html = """<a href="/p1">Doctorii noștri</a><a href="/p2">Specialiștii noștri</a>
      <a href="/doctorii-nostri/">x</a>"""
    links = people.menu_links(html, "https://clinicaexemplu.test/", limit=10)
    assert sorted(links) == ["https://clinicaexemplu.test/doctorii-nostri/",
                             "https://clinicaexemplu.test/p1", "https://clinicaexemplu.test/p2"]


def test_cabinet_label_on_english_site_scores_only_whole():
    html = '<a href="/custom-cabinets">Custom Cabinets</a>'
    assert people.menu_links(html, "https://exemplu.test/") == []


# ── addresses ──

def test_obfuscated_ro_and_eu_addresses_decode():
    html = "<p>Scrieți-ne: receptie [at] clinicaexemplu [dot] ro sau info (at) exemplu (dot) eu</p>"
    assert people.emails_on_page(html) == ["receptie@clinicaexemplu.ro", "info@exemplu.eu"]


@pytest.mark.parametrize("first, last, expected", [
    ("Ștefan", "Mureșan", "stefan.muresan@example.ro"),
    ("Ştefan", "Mureşan", "stefan.muresan@example.ro"),
    ("Mădălina", "Pătrașcu", "madalina.patrascu@example.ro"),
    ("Ioana", "Țurcanu", "ioana.turcanu@example.ro"),
    ("Ana", "Bălănescu-Popa", "ana.balanescupopa@example.ro"),
])
def test_build_email_folds_accents(first, last, expected):
    assert build_email("{f}.{l}", first, last, "example.ro") == expected
