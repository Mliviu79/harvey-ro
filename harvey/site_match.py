"""Is a website a company's own? The evidence, gathered in code.

The contact finder looks a company up by its name and town (harvey/finder.py)
and gets a page of search results back. Most are not the company's own
site: company-information sites that keep a profile of every firm, local
listings, city halls, other firms sharing a word of its name. This module
does the part code can do without a model:

- ``screen``: drops a result before anything is visited, by its address
  alone (a directory or a listing, a page whose path carries the firm's
  CUI or registration number, an institution, a profile-shaped path).
- ``card``: what a candidate's pages, once read, say about the firm: which
  of its register facts appear (CUI, registration number, an official
  phone or email, its name without the legal form, street, town, county),
  other firms' CUIs, the phones' countries, a short excerpt of the
  contact, about and footer text, and the ways to reach it, each with the
  line of text around it.
- ``hard``: what decides without a model: the firm's CUI or registration
  number (no other firm's beside it), an official email, an official
  phone next to the firm's name; or another firm's CUI as the site's
  owner, or several firms' (a listing page).

Everything else is a question for the small model (harvey/classify.py),
which gets the cards as text (``card_text``). Page text is data: it is
quoted, never followed.
"""

from __future__ import annotations

import csv
import html as _html
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from urllib.parse import urlsplit

from harvey import resolver
from harvey.name_keys import fold, locality_key, name_key

# ── Directories, listings, institutions ──


@lru_cache(maxsize=1)
def directories() -> dict[str, str]:
    """host -> kind, from reference/directories.csv."""
    text = resources.files("harvey").joinpath("reference/directories.csv") \
        .read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    return {r["host"].strip().lower(): r["kind"].strip() for r in rows if r.get("host")}


# A host whose first label names company listings ("firme", "lista-firme",
# "topcompanii"): the word on its own in the label, or with a listing's
# prefix or suffix; never inside a firm's own name ("smithcompanies").
_DIRECTORY_LABEL = re.compile(
    r"(?:^|[-_])(?:lista|top|toate|cauta|info)?(?:firme|firmele|companii|companiile|companies|"
    r"firmen)(?:ro|romania|info|online|list|index|net)?(?:$|[-_])")
_INSTITUTION = re.compile(
    r"(\.gov(\.[a-z]{2})?$|\.edu(\.[a-z]{2})?$|\.ac\.[a-z]{2}$|\.mil(\.[a-z]{2})?$"
    r"|(^|\.)primaria|(^|\.)cj[a-z]{1,3}\.ro$|city\.ro$|(^|\.)(anaf|onrc|just|mfinante|anpc|"
    r"apia\.org|ins|cnas|itm|isu[a-z]*)\.ro$)")
_PROFILE_PATH = re.compile(
    r"/(firma|firme|company|companies|companie|companii|profil|profile|cui|cif|detalii|"
    r"biz|business|listing|listings|place|places|o)/([^/?#]*)", re.I)
# What follows a profile-shaped word on a firm's own site ("/company/despre"):
# a page of its own, not another firm's profile.
_OWN_PAGE = re.compile(
    r"(?i)^(?:despre|about|about-us|despre-noi|contact|contacte|echipa|team|istoric|history|"
    r"cariere|careers|servicii|services|misiune|mission|valori|values|noutati|news|"
    r"parteneri|partners|certificari|certifications|politica[-\w]*|termeni[-\w]*|gdpr)?$")


def directory_kind(host: str) -> str:
    """The kind of a directory, listing or platform ``host`` (or a parent
    of it) is, or ""."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    known = directories()
    parts = host.split(".")
    for i in range(len(parts) - 1):
        kind = known.get(".".join(parts[i:]))
        if kind:
            return kind
    if parts and _DIRECTORY_LABEL.search(parts[0]):
        return "directory"
    return ""


def institution(host: str) -> bool:
    return bool(_INSTITUTION.search((host or "").lower()))


# ── The firm's facts ──


@dataclass
class Facts:
    """What the registers say of a firm, as the cards compare it: its name
    (and legal form), CUI, registration number, street and number, town,
    county, and the phones (E.164) and emails official sources give."""

    name: str = ""
    form: str = ""
    cui: str = ""
    reg: str = ""
    street: str = ""
    number: str = ""
    locality: str = ""
    county: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    region: str = "RO"

    @classmethod
    def of(cls, d: dict) -> "Facts":
        street, number = d.get("street") or "", d.get("number") or ""
        if street and not number:
            street, number = split_street(street)
        return cls(name=d.get("name") or "", form=d.get("form") or "",
                   cui=re.sub(r"\D", "", str(d.get("cui") or "")).lstrip("0"),
                   reg=d.get("reg") or "", street=street, number=number,
                   locality=d.get("locality") or "", county=d.get("county") or "",
                   phones=[p for p in d.get("phones") or [] if p],
                   emails=[e.lower() for e in d.get("emails") or [] if e],
                   region=d.get("region") or "RO")

    def as_dict(self) -> dict:
        return {"name": self.name, "form": self.form, "cui": self.cui, "reg": self.reg,
                "street": " ".join(x for x in (self.street, self.number) if x),
                "locality": self.locality, "county": self.county,
                "phones": list(self.phones), "emails": list(self.emails)}

    @property
    def key(self) -> str:
        return name_key(self.name)

    @property
    def words(self) -> list[str]:
        return [w for w in self.key.split() if len(w) >= 3]


_STREET_TYPE = {"str", "strada", "bd", "bdul", "b", "bulevardul", "calea", "cal", "sos",
                "soseaua", "aleea", "al", "piata", "splaiul", "intrarea", "drumul", "nr"}


def split_street(address: str) -> tuple[str, str]:
    """(street words, number) of an address line ("Str. Florilor nr. 12,
    Brașov" -> ("florilor", "12")); ("", "") when it names no street."""
    first = (address or "").split(",")[0]
    m = re.search(r"(?i)\bnr\.?\s*(\d+[a-z]?)", first)
    number = m.group(1).lower() if m else ""
    words = fold(first[:m.start()] if m else first)
    words = [w for w in words if w not in _STREET_TYPE and not w.isdigit()]
    return " ".join(words), number


# ── Reading a result's address before any visit ──


def screen(url: str, domain: str, facts: Facts) -> str:
    """Why a search result can't be the firm's own site, read from its
    address alone ("" when it may be): unusable (a free mailbox, a site
    builder, a short link), a directory or listing's kind, profile (the
    firm's CUI or registration number in the path, or a profile-shaped
    path on a host that carries no word of its name), institution."""
    domain = (domain or "").strip().lower()
    kind = directory_kind(domain)
    if kind:
        return kind
    if not domain or not resolver.usable_domain(domain):
        return "unusable"
    if institution(domain):
        return "institution"
    parts = urlsplit(url or "")
    tail = f"{parts.path}?{parts.query}" if parts.query else parts.path or ""
    if facts.cui and len(facts.cui) >= 5 and re.search(
            r"(?<!\d)(?:ro)?0*" + facts.cui + r"(?!\d)", tail, re.I):
        return "profile"
    reg = _reg_slug(facts.reg)
    if reg and reg in re.sub(r"[^a-z0-9]", "", tail.lower()):
        return "profile"
    m = _PROFILE_PATH.search(tail)
    if m and not _OWN_PAGE.match(m.group(2)):
        stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        if not any(w in stem for w in facts.words):
            return "profile"
    return ""


def _reg_slug(reg: str) -> str:
    """A registration number as letters and digits ("J40/123/2010" ->
    "j401232010"), "" when too short to mean one firm."""
    slug = re.sub(r"[^a-z0-9]", "", (reg or "").lower())
    return slug if len(slug) >= 8 and slug[0] in "jfc" else ""


# ── A candidate's pages ──

_DROP = re.compile(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>")
_BREAK = re.compile(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|footer|header|section|address|td|dd|dt)>")


def text_of(html: str) -> str:
    """A page's visible text, one line per block."""
    html = _BREAK.sub("\n", _DROP.sub(" ", html or ""))
    text = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def title_of(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()[:120] if m else ""


# A CUI in a fiscal context ("CUI: RO 12345678", "Cod fiscal 12345678",
# "Cod unic de înregistrare: 12345678", "CIF: RO-12345678", "VAT
# RO12345678"), or RO glued to the digits. The digit run is bounded by
# non-digits both ends: a CUI inside a phone, an IBAN or a longer number
# isn't one.
_FISCAL = re.compile(
    r"(?:\bC\.?\s?U\.?\s?I\.?|\bC\.?\s?I\.?\s?F\.?"
    r"|\bcod\s+(?:unic|fiscal)(?:\s+de\s+[iî]nregistrare(?:\s+fiscal[aă])?)?"
    r"|\bcod\s+de\s+[iî]nregistrare\s+fiscal[aă]|\bcod\s+de\s+identificare\s+fiscal[aă]"
    r"|\bVAT(?:\s*(?:ID|no\.?|number))?|\bcod\s+TVA"
    r"|\bnr\.?\s*fiscal)[\s:.#\-]*(?:nr\.?\s*)?(?:RO[\s\-]?)?(\d{2,10})(?![\d,.]?\d)(?!\s*%)",
    re.I)
_RO_CUI = re.compile(r"(?<![A-Za-z0-9])RO\s?(\d{4,10})(?![\d\w])")
_REG = re.compile(r"(?i)\b([JFC])\s?(\d{1,2})\s?/\s?(\d{1,7})\s?/\s?(\d{4})\b|\b([JFC]\d{12,14})\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# An address written to stop scrapers: "office [at] exemplu.ro".
_EMAIL_AT = re.compile(r"(?i)([a-z0-9._%+-]+)\s*[\[(]\s*(?:at|@)\s*[\])]\s*([a-z0-9.-]+\.[a-z]{2,})")
# Another firm's code needs five digits at least, and isn't a year: "TVA
# 19" is a rate, "CIF 2020" an Incoterm's edition, "VAT 2025" a year.
_YEAR = re.compile(r"(?:19|20)\d\d")


def _other_cui(digits: str) -> bool:
    return len(digits) >= 5 and not _YEAR.fullmatch(digits)


def cuis_in(text: str) -> tuple[set[str], set[str]]:
    """(CUIs in a fiscal context, CUIs written RO+digits), as clean digits."""
    fiscal = {m.group(1).lstrip("0") for m in _FISCAL.finditer(text or "")}
    glued = {m.group(1).lstrip("0") for m in _RO_CUI.finditer(text or "")}
    keep = lambda s: {d for d in s if len(d) >= 2}  # noqa: E731
    return keep(fiscal), keep(glued)


def reg_key(raw: str) -> str:
    """A registration number compared ("J40/0123/2010" and "J40/123/2010"
    alike; the 13-character form as written)."""
    raw = re.sub(r"\s", "", (raw or "").upper())
    m = re.fullmatch(r"([JFC])(\d{1,2})/(\d{1,7})/(\d{4})", raw)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}/{int(m.group(3))}/{m.group(4)}"
    return raw


def regs_in(text: str) -> set[str]:
    out = set()
    for m in _REG.finditer(text or ""):
        out.add(reg_key(m.group(0)))
    return out


def emails_in(text: str) -> set[str]:
    found = {e.lower().rstrip(".") for e in _EMAIL.findall(text or "")}
    found |= {f"{a}@{b}".lower().rstrip(".") for a, b in _EMAIL_AT.findall(text or "")}
    return found


def phones_in(text: str, region: str = "RO") -> dict[str, tuple[str, int]]:
    """E.164 -> (its country, where in the text) for each valid number."""
    import phonenumbers

    out: dict[str, tuple[str, int]] = {}
    try:
        matches = list(phonenumbers.PhoneNumberMatcher(text or "", region or "RO"))
    except Exception:
        return out
    for m in matches:
        if not phonenumbers.is_valid_number(m.number):
            continue
        e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        out.setdefault(e164, (phonenumbers.region_code_for_number(m.number) or "?", m.start))
    return out


def phone_e164(raw: str, region: str = "RO") -> str:
    """A phone as E.164, "" when it isn't a valid number."""
    import phonenumbers

    try:
        n = phonenumbers.parse(str(raw or ""), region or "RO")
    except Exception:
        return ""
    if not phonenumbers.is_valid_number(n):
        return ""
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


# Template addresses a theme ships with, never anyone's mailbox.
PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "example.ro", "exemplu.ro", "domeniu.ro",
    "domeniul.ro", "domeniultau.ro", "firma.ro", "firmata.ro", "companie.ro", "compania.ro",
    "numefirma.ro", "yourdomain.com", "domain.com", "email.com", "site.ro", "website.com",
    "mysite.com", "company.com"})
PLACEHOLDER_LOCALS = frozenset({
    "nume", "prenume", "numele", "nume.prenume", "prenume.nume", "yourname", "your.name",
    "name", "username", "email", "youremail", "adresa", "adresata"})


def placeholder(kind: str, value: str) -> bool:
    """A template value a site's theme left in (``nume@exemplu.ro``)."""
    if kind != "email":
        return False
    local, _, domain = (value or "").lower().rpartition("@")
    return domain in PLACEHOLDER_DOMAINS or local in PLACEHOLDER_LOCALS


_EXCERPT = re.compile(
    r"(?i)(contact|adres|tel\b|telefon|phone|e-?mail|\bcui\b|\bcif\b|cod fiscal|\bsrl\b|"
    r"s\.r\.l|\bs\.?a\.?\b|©|copyright|despre|about|sediu|nr\.? ?reg|\bj\d{1,2}/|"
    r"registrul|program|str\.|strada)")


def _page_kind(url: str) -> str:
    path = urlsplit(url or "").path.lower()
    for kind, words in (("contact", ("contact", "kontakt")),
                        ("about", ("despre", "about", "echipa", "team", "firma", "companie")),
                        ("legal", ("termeni", "terms", "privacy", "confidential", "gdpr",
                                   "cookie", "legal"))):
        if any(w in path for w in words):
            return kind
    return "home" if path in ("", "/") else "page"


@dataclass
class Card:
    """One candidate site, as the judge sees it, and what code read off it."""

    domain: str
    url: str = ""
    title: str = ""
    facts: list[str] = field(default_factory=list)
    other_cuis: list[str] = field(default_factory=list)
    phone_countries: dict[str, int] = field(default_factory=dict)
    excerpt: list[str] = field(default_factory=list)
    readable: bool = True
    # {"kind", "value", "line", "page"} for each way to reach it, numbered
    # by position; ``picks`` maps each back to the draft's contact.
    contacts: list[dict] = field(default_factory=list)
    picks: list = field(default_factory=list)
    # Every page's text, for checking a value the judge says it read there.
    text: str = ""

    def as_dict(self) -> dict:
        return {"domain": self.domain, "title": self.title, "facts": list(self.facts),
                "other_cuis": list(self.other_cuis), "phone_countries": dict(self.phone_countries),
                "excerpt": list(self.excerpt), "readable": self.readable,
                "contacts": [dict(c) for c in self.contacts]}

    @classmethod
    def of(cls, d: dict) -> "Card":
        """A card as a labelled file writes it (judge-test)."""
        contacts = [dict(c) for c in d.get("contacts") or []]
        return cls(domain=d.get("domain") or "", title=d.get("title") or "",
                   facts=list(d.get("facts") or []), other_cuis=list(d.get("other_cuis") or []),
                   phone_countries=dict(d.get("phone_countries") or {}),
                   excerpt=list(d.get("excerpt") or []),
                   readable=bool(d.get("readable", True)), contacts=contacts,
                   picks=list(range(len(contacts))),
                   text="\n".join([*(d.get("excerpt") or []),
                                   *(c.get("line") or "" for c in contacts)]))


def _window(text: str, at: int, size: int = 90) -> str:
    a, b = max(0, at - size // 2), min(len(text), at + size // 2)
    return re.sub(r"\s+", " ", text[a:b].replace("\n", " | ")).strip()


def _find(text: str, kind: str, value: str, display: str = "") -> int:
    """Where a contact's value is written in a page's text (-1: nowhere)."""
    if kind == "phone":
        digits = re.sub(r"\D", "", display or value)[-6:]
        if len(digits) < 6:
            return -1
        squeezed, at = [], []
        for i, ch in enumerate(text):
            if ch.isdigit():
                squeezed.append(ch)
                at.append(i)
        j = "".join(squeezed).find(digits)
        return at[j] if j >= 0 else -1
    needle = (value or "").lower()
    if kind != "email":
        needle = urlsplit(needle).path.strip("/").split("/")[-1] or needle
    return text.lower().find(needle) if needle else -1


def card(facts: Facts, domain: str, draft, *, url: str = "", holders=None) -> Card:
    """The evidence a candidate's read (an api.Draft: its pages and the
    contacts code found on them) gives about the firm. ``holders``
    (E.164 -> how many firms hold it) leaves out of the match an official
    phone many firms share (an accountant's, a group's switchboard)."""
    pages = [(p.url, p.html) for p in getattr(draft, "pages", None) or [] if p.html]
    out = Card(domain=domain, url=url, readable=bool(pages))
    if not pages:
        return out
    out.title = title_of(pages[0][1])
    texts = [(u, text_of(h)) for u, h in pages]
    whole = "\n".join(t for _, t in texts)
    out.text = whole
    folded = " ".join(fold(whole))
    words = set(folded.split())
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    hits: list[str] = []
    fiscal, glued = cuis_in(whole)
    if facts.cui and facts.cui in fiscal:
        hits.append("cui")
    # ("RO-12345678" counts for the firm's own code only: a dash after RO
    # also starts a postcode, "RO-500123".)
    if facts.cui and (facts.cui in glued or re.search(
            r"(?<![A-Za-z0-9])RO-\s?0*" + facts.cui + r"(?!\d)", whole)):
        hits.append("ro+cui")
    out.other_cuis = sorted((d for d in fiscal | glued if _other_cui(d) and d != facts.cui),
                            key=lambda d: (-len(d), d))[:3]
    if facts.reg and reg_key(facts.reg) in regs_in(whole):
        hits.append("reg")
    found_phones = phones_in(whole, facts.region)
    for c in getattr(draft, "contacts", None) or []:
        if c.kind == "phone" and c.value and c.value not in found_phones:
            try:
                import phonenumbers

                n = phonenumbers.parse(c.value, None)
                found_phones[c.value] = (phonenumbers.region_code_for_number(n) or "?", -1)
            except Exception:
                pass
    shared = lambda v: holders is not None and holders.get(v, 0) > resolver.PHONE_HOLDERS_MAX  # noqa: E731
    official = {p for p in facts.phones if not shared(p)}
    if official & set(found_phones):
        hits.append("phone:official")
    page_emails = emails_in(whole) | {c.value.lower() for c in getattr(draft, "contacts", ())
                                      if c.kind == "email" and c.value}
    # (An email many firms share, an accountant's, says nothing of whose
    # site this is.)
    matched = {e for e in facts.emails if resolver.usable_email(e) and not shared(e)} \
        & page_emails
    if matched:
        hits.append("email:official")
        host = domain.lower().removeprefix("www.")
        if any(e.rpartition("@")[2] == host or e.rpartition("@")[2].endswith("." + host)
               or host.endswith("." + e.rpartition("@")[2]) for e in matched):
            hits.append("email:own_domain")
    key = facts.key
    if key and f" {key} " in f" {folded} ":
        hits.append("name")
    elif facts.words and all(w in words for w in facts.words):
        hits.append("name_words")
    if facts.words and all(w in stem for w in facts.words):
        hits.append("name_in_domain")
    elif any(len(w) >= 4 and w in stem for w in facts.words):
        hits.append("name_part_in_domain")
    if facts.street and f" {facts.street} " in f" {folded} ":
        hits.append("street")
        if facts.number and re.search(re.escape(facts.street) + r"\D{0,24}\b"
                                      + re.escape(facts.number) + r"\b", folded):
            hits.append("street+no")
    town = locality_key(facts.locality)
    if town and f" {town} " in f" {folded} ":
        hits.append("town")
    county = " ".join(fold(facts.county))
    if len(county) > 2 and f" {county} " in f" {folded} ":
        hits.append("county")
    out.facts = hits
    countries: dict[str, int] = {}
    for country, _ in found_phones.values():
        countries[country] = countries.get(country, 0) + 1
    out.phone_countries = countries
    # The contact, about and footer lines first, then the homepage's last.
    lines: list[str] = []
    for i, (u, t) in enumerate(texts):
        rows = t.split("\n")
        lines += [r[:160] for r in rows if _EXCERPT.search(r)]
        if i == 0:
            lines += [r[:160] for r in rows[-6:]]
    out.excerpt = list(dict.fromkeys(lines))[:40]
    for c in getattr(draft, "contacts", None) or []:
        if not c.value or placeholder(c.kind, c.value):
            continue
        line, page = "", ""
        for u, t in texts:
            at = _find(t, c.kind, c.value, getattr(c, "display", ""))
            if at >= 0:
                line, page = _window(t, at), _page_kind(u)
                break
        if not page:
            page = _page_kind(getattr(c, "source_url", "") or "")
        out.contacts.append({"kind": c.kind, "value": c.value, "line": line, "page": page})
        out.picks.append(c)
    return out


# ── What decides without a model ──


def hard(c: Card) -> tuple[str, str]:
    """(verdict, rule) from code alone: ("confirmed", "cui" | "ro+cui" |
    "reg" | "email" | "phone"), ("rejected", "unreadable" | "other_firm" |
    "listing"), or ("judge", why it is a question).

    The firm's own code with other firms' beside it is a question (a
    group's site lists its sister companies; a listing page lists anyone):
    so is an official email or phone on a page that doesn't name the firm
    (an accountant's address, a city hall's switchboard), unless the email
    is at the candidate's own domain. Several other firms' codes and none
    of its own: a listing."""
    if not c.readable:
        return "rejected", "unreadable"
    h = set(c.facts)
    own = [r for r in ("cui", "ro+cui", "reg") if r in h]
    named = bool(h & {"name", "name_words", "name_in_domain"})
    if own and not c.other_cuis:
        return "confirmed", own[0]
    if own:
        return "judge", "cui_and_other_cuis"
    if len(c.other_cuis) >= 2:
        # Several firms' codes and not its own: a listing.
        return "rejected", "listing"
    email, phone = "email:official" in h, "phone:official" in h
    if email and not c.other_cuis and (named or "email:own_domain" in h):
        return "confirmed", "email"
    if phone and not c.other_cuis and named:
        return "confirmed", "phone"
    if len(c.other_cuis) == 1 and not named and not phone and not email:
        # Another firm's code, and nothing of this one's: that firm's site.
        return "rejected", "other_firm"
    if email:
        return "judge", "email_without_name"
    return "judge", "phone_without_name" if phone else ""


# ── A site waiting for the classifier ──

# The detail of a waiting site's read (harvey/finder.py): the candidates
# still in question, which the re-check judges together.
WAITING = "not linked: waits for a check"


def waiting_detail(domains: list[str]) -> str:
    return f"{WAITING}; candidates: {' '.join(domains)}"[:500]


def waiting_candidates(detail: str) -> list[str]:
    """The candidates a waiting site's read named (waiting_detail), in order."""
    head, _, rest = (detail or "").partition("; candidates: ")
    if head != WAITING:
        return []
    return [d for d in rest.split() if d]


# ── As text, for a small model ──

# About 3.3 characters a token for Romanian text on the common vocabularies:
# a budget, not a bill.
CHARS_PER_TOKEN = 3.3


def tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def firm_line(f: Facts) -> str:
    place = ", ".join(x for x in (" ".join(x for x in (f.street, f.number) if x),
                                  f.locality, f.county) if x)
    parts = [f.name or "-", f"form {f.form}" if f.form else "",
             f"CUI {f.cui}" if f.cui else "", f"reg {f.reg}" if f.reg else "", place,
             "phones " + ", ".join(f.phones[:4]) if f.phones else "",
             "emails " + ", ".join(f.emails[:3]) if f.emails else ""]
    return "COMPANY: " + " | ".join(p for p in parts if p)


def card_text(n: int, c: Card, *, excerpt_chars: int = 700, max_contacts: int = 12,
              line_chars: int = 90) -> str:
    """Candidate ``n`` (numbered from 1) as a few lines of text."""
    lines = [f"[{n}] {c.domain} | {(c.title or '')[:80]}",
             f"found: {' '.join(c.facts) or 'nothing'}",
             f"other CUIs: {', '.join(c.other_cuis[:3]) or 'none'} | phones: "
             + (", ".join(f"{k}x{v}" for k, v in sorted(c.phone_countries.items())) or "none")]
    if not c.readable:
        lines.append("text: (the site didn't answer)")
        return "\n".join(lines)
    kept, size = [], 0
    for line in c.excerpt:
        if size + len(line) > excerpt_chars:
            break
        kept.append(line)
        size += len(line) + 3
    lines.append("text: " + " | ".join(kept))
    if c.contacts:
        lines.append("contacts:")
        for j, k in enumerate(c.contacts[:max_contacts], 1):
            line = (k.get("line") or "")[:line_chars]
            lines.append(f"  c{j} {k['kind']} {str(k['value'])[:70]} :: {line} ({k.get('page') or ''})")
    return "\n".join(lines)


def cards_text(cards: list[Card], budget_tokens: int) -> str:
    return cards_shown(cards, budget_tokens)[0]


def cards_shown(cards: list[Card], budget_tokens: int) -> tuple[str, int, int]:
    """Every card, trimmed until the whole fits ``budget_tokens``: excerpts
    to 300 characters, contact lines to 50, contacts beyond 8, then the
    last card. (The text, how many cards it shows, how many contacts of
    each at most): an answer may name only what the model was shown."""
    steps = [dict(), dict(excerpt_chars=300), dict(excerpt_chars=300, line_chars=50),
             dict(excerpt_chars=300, line_chars=50, max_contacts=8),
             dict(excerpt_chars=150, line_chars=40, max_contacts=6)]
    shown = list(cards)
    while True:
        for kw in steps:
            text = "\n".join(card_text(i, c, **kw) for i, c in enumerate(shown, 1))
            if tokens(text) <= budget_tokens:
                return text, len(shown), kw.get("max_contacts", 12)
        if len(shown) <= 1:
            return text, len(shown), kw.get("max_contacts", 12)
        shown = shown[:-1]


def result_line(n: int, record) -> str:
    """Search result ``n`` (from 1) as one line: url | title | snippet,
    and the phone or address a listing provider gives."""
    url = getattr(record, "website", "") or getattr(record, "domain", "")
    title = (getattr(record, "name", "") or "")[:90]
    snippet = (getattr(record, "snippet", "") or "")[:160]
    extra = " | ".join(x for x in (getattr(record, "phone", ""), getattr(record, "address", ""))
                       if x)
    return " | ".join(x for x in (f"[{n}] {url[:120]}", title, snippet, extra) if x)
