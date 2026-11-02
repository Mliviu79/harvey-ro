"""Whether an address is personal, and the source note a first email to it
carries.

An address is personal when it belongs to a named person, is on a free-mail
domain, or belongs to a natural-person business (PFA, II, IF). That is
decided over every firm the address is linked to, not one chosen link: if
any link names a person or reaches a natural-person business, the address
is personal. Erring towards "personal" only adds a line to the email, which
is the safe direction.

A first email to a personal address carries a fixed line saying where the
address was found (GDPR art. 14 asks for the source at the first contact at
the latest). It is chosen here from the address's evidence, computed when
the first email is staged and stored on its outbox row, so what the operator
approves is exactly what is sent. The model never writes or paraphrases it.

Pure functions over StateManager.personal_address's facts
(overlay.reads.Reader.address_facts).
"""

from __future__ import annotations

import re
import unicodedata

NAMED, FREE_MAIL, NATURAL_PERSON = 1, 2, 3
REASON_NAMES = {NAMED: "named person", FREE_MAIL: "free-mail address",
                NATURAL_PERSON: "natural-person business"}

# A mailbox for a role, not a person (ROLE_LOCALS is English; these are the
# Romanian ones seen on firms' sites).
ROLE_LOCALS_RO = frozenset("""
office birou contact contabilitate financiar secretariat vanzari comenzi receptie programari
programare rezervari rezervare clienti suport tehnic service oferte ofertare achizitii
resurse hr juridic marketing relatii public administratie admin cabinet clinica
""".split())

# ANAF's LEGAL_FORM text for a natural-person business (its forma_juridica
# spelt out), compared without diacritics.
_NATURAL_PERSON_WORDS = ("persoana fizica", "intreprindere individuala",
                         "intreprindere familiala")

# The line for each kind of evidence source.
NOTE_SITE = "Am găsit adresa pe site-ul dumneavoastră ({domain})."
NOTE_REGISTER = "Am găsit adresa în datele publice ale firmei."
NOTE_LISTING = "Am găsit adresa în {label}."
NOTE_DERIVED = "Am dedus adresa din formatul adreselor publice ale firmei ({domain})."
NOTE_OTHER = "Am găsit adresa într-o sursă publică."
# Sources that build an address from the firm's pattern rather than find it.
DERIVED_SOURCES = frozenset({"email_verify", "hunter", "contact_finder"})
SITE_SOURCES = frozenset({"website"})


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z]+", _plain(text)) if len(t) >= 2]


def _role_local(local: str) -> bool:
    from harvey.integrations.email_finder import ROLE_LOCALS

    base = re.sub(r"\d+$", "", local)
    return base in ROLE_LOCALS or base in ROLE_LOCALS_RO


def names_person(local: str, people) -> bool:
    """The local part names a person: a name of the firm's people in it
    (ion, ion.popescu, ipopescu, popescu.i), or the first.last shape of
    any two names, unless it is a role mailbox."""
    local = _plain(local.split("+", 1)[0])
    if not local or _role_local(local):
        return False
    parts = [p for p in re.split(r"[._-]+", re.sub(r"\d+", "", local)) if p]
    names = {t for name in people or () for t in _tokens(name) if len(t) >= 3}
    if any(p in names for p in parts):
        return True
    joined = "".join(parts)
    if any(joined[1:] == n or joined[:-1] == n for n in names):
        return True       # an initial and a family name
    return (len(parts) == 2 and all(p.isalpha() and len(p) >= 2 for p in parts)
            and not any(_role_local(p) for p in parts))


def natural_person_form(firm_forms) -> bool:
    """Whether any of the firms is a natural-person business. Each firm
    (``firm_forms``: its register form and ANAF's LEGAL_FORM texts) is
    decided on its own: its register form (PFA, II, IF, PF, AF) when it has
    one, else ANAF's text. One firm's register form never hides another
    firm's ANAF reading."""
    from harvey.collectors.onrc import NATURAL_PERSON_FORMS

    for form, anaf_forms in firm_forms or ():
        if str(form or "").strip():
            if str(form).strip().upper() in NATURAL_PERSON_FORMS:
                return True
            continue
        for text in anaf_forms or ():
            plain = _plain(str(text)).strip()
            if plain.upper() in NATURAL_PERSON_FORMS or any(
                    w in plain for w in _NATURAL_PERSON_WORDS):
                return True
    return False


def personal_reason(email: str, facts: dict) -> int:
    """Why ``email`` is personal (NAMED, FREE_MAIL, NATURAL_PERSON), or 0
    for a role or company mailbox at a company."""
    from harvey.state import FREE_MAIL_DOMAINS

    address = (email or "").strip().lower()
    local, _, domain = address.rpartition("@")
    if not local:
        return 0
    if facts.get("person_link") or names_person(local, facts.get("people") or ()):
        return NAMED
    if domain in FREE_MAIL_DOMAINS:
        return FREE_MAIL
    if natural_person_form(facts.get("firm_forms") or ()):
        return NATURAL_PERSON
    return 0


def _host(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:]+)", (url or "").strip().lower())
    host = m.group(1) if m else ""
    return host[4:] if host.startswith("www.") else host


def _manifest(source: str):
    try:
        from harvey.modules import switches

        return switches.by_id().get(source)
    except Exception:
        return None


def source_note(facts: dict) -> str:
    """The fixed line a first email to a personal address carries, chosen
    from the address's evidence: its firm's website first, then a public
    register, a listing source, an address built from the firm's pattern,
    else a public source. Never empty."""
    sources = list(facts.get("sources") or ())
    email_domain = (facts.get("email") or "").rpartition("@")[2]
    domains = list(facts.get("domains") or ())
    site = next((h for h in map(_host, facts.get("evidence_urls") or ()) if h), "")
    domain = site or (domains[0] if domains else "") or email_domain
    if any(s in SITE_SOURCES for s in sources) and domain:
        return NOTE_SITE.format(domain=domain)
    for source in sources:
        m = _manifest(source)
        if m is not None and getattr(m, "register", None):
            return NOTE_REGISTER
    for source in sources:
        m = _manifest(source)
        if m is not None and getattr(m, "kind", "") == "search":
            return NOTE_LISTING.format(label=m.name or source)
    if any(s in DERIVED_SOURCES for s in sources) and (domains or email_domain):
        return NOTE_DERIVED.format(domain=domains[0] if domains else email_domain)
    return NOTE_OTHER


def with_note(body: str, note: str) -> str:
    """A first email's body with its source note as the last paragraph
    (the footer goes under it at send time); unchanged when it has it."""
    body = (body or "").rstrip()
    if not note or note in body:
        return body
    return body + "\n\n" + note
