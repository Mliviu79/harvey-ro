"""Every way to reach a company, and the people at it.

Harvey finds contacts: an email address, a phone number, a contact form, a
social profile the company's own site links to. Six tables hold them
(migration v21 in harvey/state.py):

    contacts          one row per distinct value: (kind, value) is unique, an
                      email lowercased, a phone in E.164 with its line type
    company_contacts  which companies a contact reaches, through whom and
                      where, found by which source, and how sure Harvey is
                      that it belongs to them (trust)
    people            someone named at a company (a registry's administrator,
                      a team page's member), searched for a contact of their own
    locations         a company's places (a listing that is a branch of a
                      company on file keeps its own address and phone)
    company_sources   every source a company was found in
    searches          where each record (a company, a person) was already
                      searched, by which module's capability, and with what
                      result: a record searched in one source is not searched
                      there again (unless forced, or older than the
                      capability's ``refresh_days``), and can still be
                      searched in any other

Contacts are many-to-many with companies: a chain's hundred units can share
one phone and one email, and that is one contact linked a hundred times,
not an error and not a merge. A found contact is never thrown away because
the match to its company is unproven: it is kept with its trust level
(confirmed / likely / unconfirmed) and its source.

The kinds are an open vocabulary: modules declare them in their manifests
(``[[contact_kinds]]``) and ``contact_kinds`` holds them, so a new channel is
a new module. A trigger refuses a kind nobody declared.

Every row says which company it was found for before any merge
(``origin_company_id``): a merge moves rows to the survivor, a row the
survivor already holds stays parked on the merged record (its provenance
kept), and undoing the merge sends back exactly the rows found for the
merged record.

Functions here take an open connection and never commit, like the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache

TRUST_LEVELS = ("confirmed", "likely", "unconfirmed")
EMAIL_STATUSES = ("verified", "risky", "guess", "invalid", "unchecked")
LINE_TYPES = ("mobile", "fixed", "voip", "toll_free", "unknown")

# Romanian letters folded inside SQLite (its lower() only knows ASCII). A
# person's name_norm is this fold of the name, computed the same way in SQL
# (the migration's backfill, the prospect triggers) and in Python.
_FOLD_PAIRS = (("ă", "a"), ("â", "a"), ("î", "i"), ("ș", "s"), ("ş", "s"), ("ț", "t"),
               ("ţ", "t"), ("Ă", "a"), ("Â", "a"), ("Î", "i"), ("Ș", "s"), ("Ş", "s"),
               ("Ț", "t"), ("Ţ", "t"))
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def name_norm(name: str) -> str:
    """The key that tells two mentions of one person at a company apart:
    trimmed, Romanian letters plain, ASCII lower case."""
    text = (name or "").strip(" ")
    for a, b in _FOLD_PAIRS:
        text = text.replace(a, b)
    return text.translate(_ASCII_LOWER)


def split_person(text: str) -> tuple[str, str]:
    """A CONTACT_FOUND value ("Ana Pop — administrator") as (name, role)."""
    name, sep, role = (text or "").partition(" — ")
    return name.strip(), role.strip() if sep else ""


def name_order(source: str = "") -> str:
    """The order a name from ``source`` (a module id, or a collector one of
    its signals names) is most likely written in: what the market pack of
    an official source's market says (``registry_name_order``: Romania's
    trade registry writes the family name first), else given name first.
    A hint only: where it matters, both orders are tried."""
    from harvey.modules import switches

    mans = switches.manifests()
    src = (source or "").strip()
    # A registry record's source names its module ("registry:onrc").
    src = src.split(":", 1)[1] if src.startswith("registry:") else src
    module = next((m for m in mans if m.id == src), None) or next(
        (m for m in mans if src and any(sig.get("collector") == src for sig in m.signals)), None)
    if module is None or not module.official or not module.markets:
        return "given_first"
    for pack in mans:
        if (pack.kind == "market" and pack.registry_name_order
                and set(pack.markets) & set(module.markets)):
            return pack.registry_name_order
    return "given_first"


def short_given(word: str) -> str:
    """The first part of a hyphenated given name ("Ana-Maria": Ana), for an
    address tried after the whole name's ("anamaria") comes back invalid."""
    return word.split("-")[0] or word


def name_orders(name: str, source: str = "") -> list[tuple[str, str]]:
    """Every (first, last) a person's name can be read as, the likelier
    first: given name first ("Ion Popescu") and family name first ("POPESCU
    ION MIHAI"), the first given name of several, kept whole when
    hyphenated ("Ana-Maria"). Two words read the same either way give one;
    a single word none."""
    words = [w for w in (name or "").split() if w]
    if len(words) < 2:
        return []
    upper = name == name.upper()

    def word(w: str) -> str:
        return w.title() if upper else w

    given_first = (word(words[0]), word(words[-1]))
    family_first = (word(words[1]), word(words[0]))
    orders = [family_first, given_first] if name_order(source) == "family_first" \
        else [given_first, family_first]
    return list(dict.fromkeys(orders))


def split_name(name: str, source: str = "") -> tuple[str, str]:
    """(first, last) of a person's name in its likelier order (name_orders):
    the market pack says how an official source's names are usually
    written; a team page writes the given name first."""
    orders = name_orders(name, source)
    return orders[0] if orders else ("", "")


def trust_for(confidence) -> str:
    """How sure a finding is that a contact is the company's, from the
    confidence its source gave it."""
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        value = 1.0
    return "confirmed" if value >= 0.9 else "likely" if value >= 0.6 else "unconfirmed"


@lru_cache(maxsize=4096)
def phone_parts(raw: str, region: str | None = None) -> tuple[str, str, str] | None:
    """(E.164, international display, line type) for a valid number, else None
    (remembered: one number is read several times as a batch is saved)."""
    from phonenumbers import PhoneNumberFormat, format_number

    from harvey.collectors.phones import line_type, parse_phone

    number = parse_phone(raw, region)
    if number is None:
        return None
    return (format_number(number, PhoneNumberFormat.E164),
            format_number(number, PhoneNumberFormat.INTERNATIONAL), line_type(number))


def email_status(status: str) -> str:
    return status if status in EMAIL_STATUSES else "unchecked"


# ── Writes ──

@dataclass
class Found:
    """One contact as a source gave it, before it is normalised and saved.
    ``value`` is the address, number or URL as written (a phone in any form
    ``region`` reads); ``person`` the name of whom it reaches, if anyone's."""

    kind: str
    value: str
    display: str = ""
    status: str = ""
    person: str = ""
    role: str = ""
    source: str = ""
    capability: str = ""
    evidence_url: str = ""
    confidence: float = 1.0
    trust: str = ""
    location_id: int = 0
    region: str | None = None
    seen: str | None = None
    extra: dict = field(default_factory=dict)


def normalised(found: Found) -> tuple[str, str, str, str] | None:
    """(value, display, line_type, status) as stored, or None when the value
    isn't one (a phone that doesn't parse, an email without an @)."""
    raw = (found.value or "").strip()
    if not raw:
        return None
    if found.kind == "phone":
        parts = phone_parts(raw, found.region)
        if parts is None:
            return None
        number, display, line = parts
        return number, found.display or display, line, "valid"
    if found.kind == "email":
        value = raw.lower()
        if "@" not in value.strip("@"):
            return None
        return value, value, "", email_status(found.status)
    return raw, found.display or raw, "", found.status or ""


def _country(text: str) -> bool:
    return text.strip().lower() in ("romania", "românia", "usa", "united states", "uk",
                                    "united kingdom", "canada", "australia")


# ── Searches: where a record was already searched ──

SEARCH_OUTCOMES = ("found", "nothing", "error")


def _utc(moment: datetime) -> str:
    """A moment as SQLite's CURRENT_TIMESTAMP writes it (UTC)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now() -> str:
    """The current moment, as a search's ``since`` (a forced run's start)."""
    return _utc(datetime.now(timezone.utc))


def refresh_days(module: str, capability: str) -> int:
    """How many days a capability's search holds before the record is
    searched again: its manifest's ``refresh_days``, 0 for never."""
    from harvey.modules import switches

    manifest = switches.by_id().get(module)
    for cap in (manifest.capabilities if manifest else ()):
        if cap.get("id") == capability:
            return int(cap.get("refresh_days") or 0)
    return 0


def search_cutoff(module: str, capability: str, since: str | None = None) -> str:
    """Searches from this moment on count as done. ``since`` (a forced
    run's start) counts only the ones this run did; otherwise the
    capability's refresh age, and with none every search counts ('')."""
    if since:
        return since
    days = refresh_days(module, capability)
    return _utc(datetime.now(timezone.utc) - timedelta(days=days)) if days else ""


# ── Reading: the Contacts page, its exports, and campaign audiences ──

# Counting stops here: past it the page says "N+" (like the registry search).
COUNT_CAP = 100_000

# The columns a list may be sorted by (overlay.reads sorts on each).
CONTACT_SORTS = ("value", "kind", "line_type", "status", "first_seen", "last_seen", "trust",
                 "companies")


@dataclass
class ContactFilter:
    """What the Contacts page (and an export of it) narrows by. A contact
    matches the company conditions when ANY company it reaches does."""

    kind: str = ""              # a kind, or "mobile" (phones on a mobile line)
    status: str = ""
    trust: str = ""
    has_person: str = ""        # "yes" | "no"
    source: str = ""
    county: str = ""
    locality: str = ""
    website: str = ""           # "yes" | "no": the company has a website
    q: str = ""
    # The company does one of these CAEN classes (4-digit codes; words are
    # turned into codes by the caller, harvey.caen.codes_for).
    caen: tuple[str, ...] = ()
    # The company is reached by a contact: "any", a kind ("phone", "email",
    # ...), "mobile" (a phone on a mobile line), or "none" (no contact).
    contact: str = ""
    # The company was found in this source (company_sources).
    found_in: str = ""
    # The company matches this full-text expression (the Companies page's
    # search box: state.search_terms), or has this CUI (digits).
    search: str = ""
    cui: str = ""
    # Nothing can match (a search of single letters or common words only).
    nothing: bool = False
    # The words typed in the search box, the cohort's (require, exclude)
    # signal codes, and the figures' bounds ({min,max}_{turnover,employees},
    # unknown).
    words: str = ""
    signals: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    bounds: dict | None = None


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fold(text: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()


def place_matches(location: str, county: str = "", locality: str = "") -> bool:
    """Whether a place ("Town, County, Country") is in the county and
    locality (the place folded as name_norm, the typed words as _fold). A
    county is matched by its name or its code, whichever the place and the
    filter use ("Cluj" and "CJ" alike)."""
    folded = name_norm(location or "")
    locality, county = (locality or "").strip(), (county or "").strip()
    if locality and not (folded + ",").startswith(_fold(locality) + ","):
        return False
    if county and not _county_spellings(county) & {p.strip() for p in folded.split(",")}:
        return False
    return True


def _county_spellings(county: str) -> set[str]:
    """The county's folded name and code, or only what was typed when it
    names no county."""
    from harvey.collectors import onrc

    out = {_fold(county)}
    name = onrc.county_name(county)
    if name:
        out |= {_fold(name)} | {code.lower() for code, n in onrc.COUNTIES.items() if n == name}
    return out


like = _like
fold = _fold


