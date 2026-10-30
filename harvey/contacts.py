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

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
    trimmed, Romanian letters plain, ASCII lower case. Same as sql_name_norm."""
    text = (name or "").strip(" ")
    for a, b in _FOLD_PAIRS:
        text = text.replace(a, b)
    return text.translate(_ASCII_LOWER)


def sql_name_norm(expr: str) -> str:
    """name_norm as an SQL expression over ``expr``."""
    expr = f"TRIM({expr})"
    for a, b in _FOLD_PAIRS:
        expr = f"REPLACE({expr}, '{a}', '{b}')"
    return f"LOWER({expr})"


def split_person(text: str) -> tuple[str, str]:
    """A CONTACT_FOUND value ("Ana Pop — administrator") as (name, role)."""
    name, sep, role = (text or "").partition(" — ")
    return name.strip(), role.strip() if sep else ""


def split_name(name: str, source: str = "") -> tuple[str, str]:
    """(first, last) of a person's name. The trade registry writes the
    family name first ("POPESCU ION MIHAI"); a team page writes it last."""
    words = [w for w in (name or "").split() if w]
    if len(words) < 2:
        return "", ""
    if (source or "").startswith(("onrc", "registry")):
        return words[1].title(), words[0].title()
    return words[0], words[-1]


def trust_for(confidence) -> str:
    """How sure a finding is that a contact is the company's, from the
    confidence its source gave it."""
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        value = 1.0
    return "confirmed" if value >= 0.9 else "likely" if value >= 0.6 else "unconfirmed"


def phone_parts(raw: str, region: str | None = None) -> tuple[str, str, str] | None:
    """(E.164, international display, line type) for a valid number, else None."""
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

_UPSERT_CONTACT = """INSERT INTO contacts
       (kind, value, display, line_type, status, first_seen, last_seen)
   VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
   ON CONFLICT(kind, value) DO UPDATE SET
       display = CASE WHEN contacts.display = '' THEN excluded.display ELSE contacts.display END,
       line_type = CASE WHEN excluded.line_type NOT IN ('', 'unknown')
                        THEN excluded.line_type ELSE contacts.line_type END,
       status = CASE WHEN excluded.status NOT IN ('', 'unchecked')
                     THEN excluded.status ELSE contacts.status END,
       first_seen = MIN(contacts.first_seen, excluded.first_seen),
       last_seen = MAX(contacts.last_seen, excluded.last_seen)
   RETURNING id"""

# The best of two trust levels, in SQL (``a`` and ``b`` are expressions).
_BEST_TRUST = ("CASE WHEN 'confirmed' IN ({a}, {b}) THEN 'confirmed' "
               "WHEN 'likely' IN ({a}, {b}) THEN 'likely' ELSE 'unconfirmed' END")

_UPSERT_LINK = f"""INSERT INTO company_contacts
       (company_id, contact_id, person_id, location_id, source, capability,
        evidence_url, trust, confidence, first_seen, last_seen, origin_company_id)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP),
           COALESCE(?, CURRENT_TIMESTAMP), ?)
   ON CONFLICT(company_id, contact_id, person_id, source) DO UPDATE SET
       location_id = CASE WHEN company_contacts.location_id = 0
                          THEN excluded.location_id ELSE company_contacts.location_id END,
       evidence_url = CASE WHEN company_contacts.evidence_url = ''
                           THEN excluded.evidence_url ELSE company_contacts.evidence_url END,
       trust = {_BEST_TRUST.format(a="company_contacts.trust", b="excluded.trust")},
       confidence = MAX(company_contacts.confidence, excluded.confidence),
       first_seen = MIN(company_contacts.first_seen, excluded.first_seen),
       last_seen = MAX(company_contacts.last_seen, excluded.last_seen)"""

_UPSERT_PERSON = """INSERT INTO people
       (company_id, name, name_norm, role, source, first_seen, last_seen, origin_company_id)
   VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP), ?)
   ON CONFLICT(company_id, name_norm) DO UPDATE SET
       role = CASE WHEN people.role = '' THEN excluded.role ELSE people.role END,
       first_seen = MIN(people.first_seen, excluded.first_seen),
       last_seen = MAX(people.last_seen, excluded.last_seen)"""


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


async def save_contact(db, kind: str, value: str, display: str = "", line_type: str = "",
                       status: str = "", seen: str | None = None) -> int:
    """The contact's id, saved or refreshed (values as ``normalised`` gives)."""
    async with db.execute(_UPSERT_CONTACT, (kind, value, display, line_type, status,
                                            seen, seen)) as cur:
        return (await cur.fetchone())[0]


async def add_person(db, company_id: str, name: str, role: str = "", source: str = "",
                     origin: str = "", seen: str | None = None) -> int:
    """The person's id at the company, saved or refreshed; 0 for no name."""
    name = (name or "").strip()
    key = name_norm(name)
    if not company_id or not key:
        return 0
    await db.execute(_UPSERT_PERSON, (company_id, name, key, role or "", source or "",
                                      seen, seen, origin or company_id))
    async with db.execute("SELECT id FROM people WHERE company_id = ? AND name_norm = ?",
                          (company_id, key)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def add(db, company_id: str, found: Found, origin: str = "") -> int | None:
    """Save a contact and link it to the company (and to its person, when
    it names one). Returns the contact's id, None when the value isn't one."""
    if not company_id:
        return None
    parts = normalised(found)
    if parts is None:
        return None
    value, display, line, status = parts
    try:
        contact_id = await save_contact(db, found.kind, value, display, line, status, found.seen)
    except sqlite3.IntegrityError:
        # A kind no module declares (contact_kinds' trigger): not kept.
        return None
    person_id = 0
    if found.person:
        person_id = await add_person(db, company_id, found.person, found.role, found.source,
                                     origin, found.seen)
    await db.execute(_UPSERT_LINK, (
        company_id, contact_id, person_id, found.location_id or 0, found.source or "",
        found.capability or "", found.evidence_url or "",
        found.trust if found.trust in TRUST_LEVELS else trust_for(found.confidence),
        float(found.confidence), found.seen, found.seen, origin or company_id))
    return contact_id


async def add_location(db, company_id: str, *, name: str = "", address: str = "",
                       location: str = "", point: str = "", external_id: str = "",
                       phone: str = "", source: str = "", origin: str = "") -> int:
    """One of a company's places (a listing's): kept once per listing id,
    refreshed when seen again. ``location`` ("Town, County, Country") is
    split into its parts. Returns its id (0 without a listing id or an
    address, which can't tell two places apart)."""
    if not company_id or not (external_id or address):
        return 0
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    locality = parts[0] if parts else ""
    county = parts[1] if len(parts) >= 2 and not (len(parts) == 2 and _country(parts[1])) else ""
    country = parts[-1] if len(parts) >= 2 and (len(parts) >= 3 or _country(parts[-1])) else ""
    if external_id:
        async with db.execute("SELECT id FROM locations WHERE company_id = ? AND external_id = ?",
                              (company_id, external_id)) as cur:
            row = await cur.fetchone()
    else:
        async with db.execute("SELECT id FROM locations WHERE company_id = ? AND external_id = '' "
                              "AND address = ?", (company_id, address)) as cur:
            row = await cur.fetchone()
    if row:
        await db.execute(
            "UPDATE locations SET last_seen = CURRENT_TIMESTAMP, "
            "address = CASE WHEN ? != '' THEN ? ELSE address END, "
            "phone = CASE WHEN phone = '' THEN ? ELSE phone END WHERE id = ?",
            (address, address, phone or "", row[0]))
        return row[0]
    cur = await db.execute(
        "INSERT INTO locations (company_id, name, address, locality, county, country, point, "
        "external_id, phone, source, origin_company_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company_id, name or "", address or "", locality, county, country, point or "",
         external_id or "", phone or "", source or "", origin or company_id))
    return cur.lastrowid


def _country(text: str) -> bool:
    return text.strip().lower() in ("romania", "românia", "usa", "united states", "uk",
                                    "united kingdom", "canada", "australia")


async def add_source(db, company_id: str, source: str, ref: str = "", origin: str = "") -> None:
    """A source the company was found in (one company, many sources)."""
    if not company_id or not source:
        return
    await db.execute(
        "INSERT INTO company_sources (company_id, source, ref, origin_company_id) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(company_id, source, ref) DO UPDATE SET "
        "last_seen = CURRENT_TIMESTAMP", (company_id, source, ref or "", origin or company_id))


async def from_observations(db, rows: list[dict]) -> None:
    """What observation rows say about reaching the company: a PHONE_NUMBER
    is a phone contact, a CONTACT_FOUND a person, a CONTACT_FORM_URL a
    contact form. Called by the one observation writer, so every path that
    writes those facts keeps the contacts in step."""
    people = []
    for r in rows:
        company_id, code = r.get("company_id") or "", r.get("signal_code")
        text = (r.get("value_text") or "").strip()
        if not company_id or not text or code not in ("PHONE_NUMBER", "CONTACT_FOUND",
                                                        "CONTACT_FORM_URL"):
            continue
        source = r.get("module") or r.get("collector") or ""
        origin = r.get("origin_company_id") or company_id
        if code == "CONTACT_FOUND":
            name, role = split_person(text)
            if name_norm(name):
                people.append((company_id, name, name_norm(name), role, source,
                               r.get("observed_at"), r.get("observed_at"), origin))
            continue
        await add(db, company_id, Found(
            "phone" if code == "PHONE_NUMBER" else "contact_form", text,
            source=source, capability=r.get("capability") or "",
            evidence_url=r.get("evidence_url") or "", confidence=r.get("confidence", 1.0),
            seen=r.get("observed_at")), origin)
    if people:
        await db.executemany(_UPSERT_PERSON, people)


async def sync_kinds(db, kinds: list[dict]) -> None:
    """Bring contact_kinds in line with the manifests: every declared kind,
    ``available`` while its module is on. A kind is never deleted (its
    contacts stay)."""
    await db.executemany(
        "INSERT INTO contact_kinds (kind, label, channel, module, available) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind) DO UPDATE SET label = excluded.label, "
        "channel = excluded.channel, module = excluded.module, available = excluded.available",
        [(k["kind"], k.get("label", k["kind"]), k.get("channel", ""), k.get("module", ""),
          1 if k.get("available", True) else 0) for k in kinds])


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


def not_searched(target_id_sql: str, target: str = "company", looked_for: str = "") -> str:
    """An SQL condition: the record ``target_id_sql`` has no search done
    by a module's capability since a cutoff. Takes three parameters:
    module, capability, cutoff (search_cutoff). An error isn't a search
    done. ``looked_for`` (one word, code's own) counts only a search that
    looked for it: a site read for its phones doesn't close the read for
    its emails."""
    assert looked_for.isalpha() or not looked_for
    wanted = f"AND (',' || s.looked_for || ',') LIKE '%,{looked_for},%' " if looked_for else ""
    return ("NOT EXISTS (SELECT 1 FROM searches s WHERE s.target = '" + target + "' "
            f"AND s.target_id = {target_id_sql} AND s.module = ? AND s.capability = ? "
            f"AND s.searched_at >= ? AND s.outcome != 'error' {wanted})")


async def record_search(db, target: str, target_id, module: str, capability: str, *,
                        looked_for: str = "", outcome: str = "nothing", found: int = 0,
                        cost: float = 0.0, batch_id: str = "", detail: str = "",
                        origin: str = "") -> None:
    """One search done for a record (a company, a person), whatever it
    found: the next run skips the record for this capability."""
    if outcome not in SEARCH_OUTCOMES:
        outcome = "found" if found else "nothing"
    await db.execute(
        "INSERT INTO searches (target, target_id, module, capability, looked_for, outcome, "
        "found, cost, batch_id, detail, origin_company_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target, str(target_id), module, capability or "", looked_for or "", outcome,
         int(found or 0), float(cost or 0.0), batch_id or "", (detail or "")[:500],
         origin or (str(target_id) if target == "company" else "")))


# ── Merge and undo (called by harvey/resolver.py, inside its transaction) ──

# The tables a merge moves, each with its own key per company.
MOVED_TABLES = ("people", "company_contacts", "locations", "company_sources")


async def _ready(db) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                          "AND name = 'company_contacts'") as cur:
        return await cur.fetchone() is not None


async def merge(db, survivor: str, merged: str, merge_id: int) -> None:
    """Move the merged company's people, links, places and sources to the
    survivor. A row the survivor already holds (the same person, the same
    contact through the same person) stays parked on the merged record, so
    its provenance is kept and the survivor shows it once. A link through a
    person who is parked follows the survivor's same-named person; that
    re-point is logged so an undo restores it."""
    if not await _ready(db):
        return
    await db.execute(
        "INSERT INTO company_merge_moves (merge_id, tbl, key1, key2, key3) "
        "SELECT ?, 'company_contacts.person', CAST(cc.id AS TEXT), CAST(cc.person_id AS TEXT), "
        "CAST(s.id AS TEXT) FROM company_contacts cc "
        "JOIN people m ON m.id = cc.person_id AND m.company_id = ? "
        "JOIN people s ON s.company_id = ? AND s.name_norm = m.name_norm "
        "WHERE cc.company_id = ?", (merge_id, merged, survivor, merged))
    await db.execute(
        "UPDATE OR IGNORE company_contacts SET person_id = (SELECT CAST(key3 AS INTEGER) "
        "FROM company_merge_moves WHERE merge_id = ? AND tbl = 'company_contacts.person' "
        "AND key1 = CAST(company_contacts.id AS TEXT)) "
        "WHERE company_id = ? AND CAST(id AS TEXT) IN (SELECT key1 FROM company_merge_moves "
        "WHERE merge_id = ? AND tbl = 'company_contacts.person')",
        (merge_id, merged, merge_id))
    for table in MOVED_TABLES:
        await db.execute(f"UPDATE OR IGNORE {table} SET company_id = ? WHERE company_id = ?",
                         (survivor, merged))
    # A search of either record is a search of the company they are.
    await db.execute("UPDATE searches SET target_id = ? WHERE target = 'company' "
                     "AND target_id = ?", (survivor, merged))


async def undo(db, survivor: str, merged: str, merge_id: int) -> None:
    """Send back what was found for the merged record (its own rows, and
    those of companies merged into it before) and restore the persons its
    links pointed at. Rows the survivor holds for itself stay."""
    if not await _ready(db):
        return
    origins = ("(SELECT ? UNION SELECT key1 FROM company_merge_moves "
               "WHERE merge_id = ? AND tbl = 'companies')")
    for table in MOVED_TABLES:
        await db.execute(
            f"UPDATE OR IGNORE {table} SET company_id = ? WHERE company_id = ? "
            f"AND origin_company_id IN {origins}", (merged, survivor, merged, merge_id))
    await db.execute(
        "UPDATE searches SET target_id = ? WHERE target = 'company' AND target_id = ? "
        f"AND origin_company_id IN {origins}", (merged, survivor, merged, merge_id))
    await db.execute(
        "UPDATE OR IGNORE company_contacts SET person_id = (SELECT CAST(key2 AS INTEGER) "
        "FROM company_merge_moves WHERE merge_id = ? AND tbl = 'company_contacts.person' "
        "AND key1 = CAST(company_contacts.id AS TEXT)) "
        "WHERE CAST(id AS TEXT) IN (SELECT key1 FROM company_merge_moves "
        "WHERE merge_id = ? AND tbl = 'company_contacts.person')", (merge_id, merge_id))


# ── v21's Python step ──

async def backfill_phones(db, batch: int = 5000) -> int:
    """Display form and line type for the phones v21's SQL backfilled (it
    can't parse a number). Returns how many were read."""
    from harvey.collectors.phones import line_type, parse_phone
    from phonenumbers import PhoneNumberFormat, format_number

    done = 0
    last = 0
    while True:
        async with db.execute(
            "SELECT id, value FROM contacts WHERE kind = 'phone' AND line_type = '' AND id > ? "
            "ORDER BY id LIMIT ?", (last, batch)
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return done
        out = []
        for contact_id, value in rows:
            number = parse_phone(value)
            if number is None:
                out.append(("", "unknown", "invalid", contact_id))
            else:
                out.append((format_number(number, PhoneNumberFormat.INTERNATIONAL),
                            line_type(number), "valid", contact_id))
        await db.executemany(
            "UPDATE contacts SET display = CASE WHEN ? != '' THEN ? ELSE display END, "
            "line_type = ?, status = ? WHERE id = ?",
            [(d, d, lt, st, cid) for d, lt, st, cid in out])
        done += len(rows)
        last = rows[-1][0]


async def backfill_prospect_phones(db) -> int:
    """A prospect's own phone, as a phone contact of its person at its
    company (read in the company's country). Returns how many were kept."""
    from harvey.collectors.phones import phone_region

    async with db.execute(
        "SELECT p.company_id, p.phone, TRIM(COALESCE(p.first_name, '') || ' ' || "
        "COALESCE(p.last_name, '')), COALESCE(p.title, ''), COALESCE(p.source, ''), "
        "COALESCE(p.source_url, ''), COALESCE(c.location, ''), COALESCE(c.domain, '') "
        "FROM prospects p JOIN companies c ON c.id = p.company_id "
        "WHERE TRIM(COALESCE(p.phone, '')) != ''"
    ) as cur:
        rows = await cur.fetchall()
    kept = 0
    for company_id, phone, name, role, source, url, location, domain in rows:
        kept += await add(db, company_id, Found(
            "phone", phone, person=name, role=role, source=source, evidence_url=url,
            trust="likely", region=phone_region(location, None, domain))) is not None
    return kept


# ── Reading: the Contacts page, its exports, and campaign audiences ──

# Counting stops here: past it the page says "N+" (like the registry search).
COUNT_CAP = 100_000

# The best trust a contact has across its links, as a rank (0 = confirmed).
TRUST_RANK = ("(SELECT MIN(CASE l.trust WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 "
              "ELSE 2 END) FROM company_contacts l WHERE l.contact_id = ct.id)")
LIVE_LINKED = ("(SELECT COUNT(DISTINCT l.company_id) FROM company_contacts l "
               "JOIN companies lc ON lc.id = l.company_id "
               "WHERE l.contact_id = ct.id AND COALESCE(lc.merged_into, '') = '')")

# The columns a list may be sorted by, and the SQL each one sorts on.
CONTACT_SORTS = {
    "value": "ct.value", "kind": "ct.kind", "line_type": "ct.line_type",
    "status": "ct.status", "first_seen": "ct.first_seen", "last_seen": "ct.last_seen",
    "trust": TRUST_RANK, "companies": LIVE_LINKED,
}
# When a person was last searched for a contact of their own.
PERSON_SEARCHED = ("(SELECT MAX(s.searched_at) FROM searches s WHERE s.target = 'person' "
                   "AND s.target_id = CAST(p.id AS TEXT))")
PEOPLE_SORTS = {
    "name": "p.name_norm", "role": "p.role", "company": "c.name",
    "first_seen": "p.first_seen", "searched_at": PERSON_SEARCHED,
}


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
    # The cohort condition (a WITH clause ending in ``judged`` and its
    # parameters, from StateManager._verdict_sql): a company must match it.
    cohort: tuple[str, list] | None = None
    # A financials condition on the company's CUI (mf_financials Filter.sql,
    # with the financials database attached as ``fin``).
    figures: tuple[str, list] | None = None


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fold(text: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()


def _company_conditions(f: ContactFilter, possible: bool = False) -> tuple[list[str], list]:
    """Conditions on a company aliased ``c``. ``possible`` lets a company
    the cohort can't judge yet (a signal not checked) through, as the
    contact finder does: unknown is searched."""
    conds, params = ["COALESCE(c.merged_into, '') = ''"], []
    for value, part in ((f.locality, "locality"), (f.county, "county")):
        value = (value or "").strip()
        if not value:
            continue
        # The place is "Town, County, Country": a locality is its first part,
        # a county any part.
        folded = sql_name_norm("c.location")
        if part == "locality":
            conds.append(f"({folded} || ',') LIKE ? ESCAPE '\\'")
            params.append(_like(_fold(value) + ",")[1:])
        else:
            conds.append(f"(', ' || {folded} || ',') LIKE ? ESCAPE '\\'")
            params.append(_like(", " + _fold(value) + ","))
    if f.website == "yes":
        conds.append("(COALESCE(c.domain, '') != '' OR COALESCE(c.website, '') != '')")
    elif f.website == "no":
        conds.append("COALESCE(c.domain, '') = '' AND COALESCE(c.website, '') = ''")
    if f.cohort is not None:
        verdicts = "('match', 'possible')" if possible else "('match')"
        conds.append(f"c.id IN (SELECT id FROM judged WHERE verdict IN {verdicts})")
    if f.figures is not None:
        cond, args = f.figures
        conds.append(cond)
        params += list(args)
    return conds, params


company_conditions = _company_conditions
like = _like
fold = _fold


def _text_conditions(q: str) -> tuple[list[str], list]:
    """Every word must appear in the contact, a company it reaches or a
    person it reaches (accents ignored); a number also finds a phone however
    it is written ("0722 000" finds +40 722 000 ...)."""
    import re

    conds, params = [], []
    for word in (q or "")[:200].split()[:8]:
        folded = _fold(word)
        like = _like(folded)
        alts = ["LOWER(ct.value) LIKE ? ESCAPE '\\'",
                "EXISTS (SELECT 1 FROM company_contacts l JOIN companies lc ON lc.id = l.company_id "
                f"WHERE l.contact_id = ct.id AND {sql_name_norm('lc.name')} LIKE ? ESCAPE '\\')",
                "EXISTS (SELECT 1 FROM company_contacts l JOIN people lp ON lp.id = l.person_id "
                "WHERE l.contact_id = ct.id AND lp.name_norm LIKE ? ESCAPE '\\')"]
        args = [like, like, like]
        digits = re.sub(r"[\s+\-.()/]", "", word)
        if digits.isdigit() and len(digits) >= 3:
            forms = {digits}
            if digits.startswith("0") and not digits.startswith("00"):
                forms.add("40" + digits[1:])
            elif digits.startswith("00"):
                forms.add(digits[2:])
            for form in sorted(forms):
                alts.append("ct.value LIKE ?")
                args.append("%" + form + "%")
        conds.append("(" + " OR ".join(alts) + ")")
        params += args
    return conds, params


def contact_query(f: ContactFilter) -> tuple[str, str, list]:
    """(WITH prefix, FROM ... WHERE ... over contacts ``ct``, parameters)."""
    with_sql, params = (f.cohort if f.cohort is not None else ("", []))
    params = list(params)
    conds: list[str] = []
    if f.kind == "mobile":
        conds.append("ct.kind = 'phone' AND ct.line_type = 'mobile'")
    elif f.kind:
        conds.append("ct.kind = ?")
        params.append(f.kind)
    if f.status:
        conds.append("ct.status = ?")
        params.append(f.status)
    if f.trust in TRUST_LEVELS:
        conds.append(f"{TRUST_RANK} = ?")
        params.append(TRUST_LEVELS.index(f.trust))
    company, company_args = _company_conditions(f)
    link = ["l.contact_id = ct.id", *company]
    if f.source:
        link.append("l.source = ?")
        company_args = [f.source, *company_args]
    if f.has_person == "yes":
        link.append("l.person_id != 0")
    conds.append("EXISTS (SELECT 1 FROM company_contacts l JOIN companies c ON c.id = l.company_id "
                 "WHERE " + " AND ".join(link) + ")")
    params += company_args
    if f.has_person == "no":
        conds.append("NOT EXISTS (SELECT 1 FROM company_contacts l WHERE l.contact_id = ct.id "
                     "AND l.person_id != 0)")
    text, text_args = _text_conditions(f.q)
    conds += text
    params += text_args
    return with_sql, "FROM contacts ct WHERE " + " AND ".join(conds), params


def people_query(f: ContactFilter) -> tuple[str, str, list]:
    """The people still to search: named at a live company, reached by no
    contact of their own yet. (WITH prefix, FROM ... WHERE, parameters)."""
    with_sql, params = (f.cohort if f.cohort is not None else ("", []))
    params = list(params)
    company, company_args = _company_conditions(f)
    # (person_id != 0 spelled out: it lets SQLite use the partial index.)
    conds = ["NOT EXISTS (SELECT 1 FROM company_contacts l WHERE l.person_id = p.id "
             "AND l.person_id != 0)", *company]
    params += company_args
    if f.source:
        conds.append("p.source = ?")
        params.append(f.source)
    for word in (f.q or "")[:200].split()[:8]:
        like = _like(_fold(word))
        conds.append(f"(p.name_norm LIKE ? ESCAPE '\\' OR {sql_name_norm('c.name')} "
                     "LIKE ? ESCAPE '\\' OR LOWER(p.role) LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    return (with_sql, "FROM people p JOIN companies c ON c.id = p.company_id WHERE "
            + " AND ".join(conds), params)


def order_by(sorts: dict[str, str], sort: str, direction: str, default: str,
             tie: str) -> str:
    """An ORDER BY from the whitelist (an unknown column sorts by ``default``)."""
    column = sorts.get(sort) or sorts[default]
    way = "ASC" if (direction or "").lower() == "asc" else "DESC"
    return f" ORDER BY {column} {way}, {tie} {way}"


async def summaries(db, ids: list[int]) -> dict[int, dict]:
    """For each contact on a page: how many live companies and people it
    reaches, its best trust, its sources, and the first company (to show)."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, dict] = {i: {"companies": 0, "people": 0, "trust": "unconfirmed",
                                "sources": [], "company": None} for i in ids}
    async with db.execute(
        "SELECT l.contact_id, COUNT(DISTINCT CASE WHEN COALESCE(c.merged_into, '') = '' "
        "THEN l.company_id END), COUNT(DISTINCT NULLIF(l.person_id, 0)), "
        "MIN(CASE l.trust WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END), "
        "group_concat(DISTINCT NULLIF(l.source, '')) "
        f"FROM company_contacts l JOIN companies c ON c.id = l.company_id "
        f"WHERE l.contact_id IN ({marks}) GROUP BY l.contact_id", ids
    ) as cur:
        for cid, companies, people, rank, sources in await cur.fetchall():
            out[cid].update(companies=companies, people=people,
                            trust=TRUST_LEVELS[rank if rank is not None else 2],
                            sources=sorted((sources or "").split(",")) if sources else [])
    async with db.execute(
        "SELECT l.contact_id, c.id, c.name, c.location, COALESCE(p.name, '') "
        "FROM company_contacts l JOIN companies c ON c.id = l.company_id "
        "LEFT JOIN people p ON p.id = l.person_id "
        f"WHERE l.contact_id IN ({marks}) AND COALESCE(c.merged_into, '') = '' "
        "ORDER BY l.contact_id, CASE l.trust WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 "
        "ELSE 2 END, l.id", ids
    ) as cur:
        for cid, company_id, name, location, person in await cur.fetchall():
            if out[cid]["company"] is None:
                out[cid]["company"] = {"id": company_id, "name": name or "",
                                       "location": location or "", "person": person}
    return out


async def reach(db, contact_id: int, limit: int = 500) -> dict:
    """Everything a contact reaches: each live company (through whom, at
    which place, found by which source, how sure), up to ``limit``."""
    async with db.execute(
        "SELECT c.id, c.name, c.location, c.cui, l.source, l.trust, l.evidence_url, "
        "l.first_seen, COALESCE(p.name, ''), COALESCE(p.role, ''), "
        "COALESCE(lo.name, ''), COALESCE(lo.address, '') "
        "FROM company_contacts l JOIN companies c ON c.id = l.company_id "
        "LEFT JOIN people p ON p.id = l.person_id LEFT JOIN locations lo ON lo.id = l.location_id "
        "WHERE l.contact_id = ? AND COALESCE(c.merged_into, '') = '' "
        "ORDER BY c.name, l.id LIMIT ?", (contact_id, limit)
    ) as cur:
        rows = await cur.fetchall()
    return {"links": [{"company_id": r[0], "company": r[1] or "", "location": r[2] or "",
                       "cui": r[3] or "", "source": r[4] or "", "trust": r[5] or "",
                       "evidence_url": r[6] or "", "first_seen": r[7], "person": r[8],
                       "role": r[9], "place": r[10], "address": r[11]} for r in rows]}


async def of_company(db, company_id: str) -> dict:
    """A company's contacts (with how many other companies each reaches),
    people, places and sources: what its panel shows. A contact two sources
    gave is one row, with its best trust and both sources."""
    async with db.execute(
        "SELECT ct.id, ct.kind, ct.value, ct.display, ct.line_type, ct.status, "
        "ct.do_not_contact, MIN(CASE l.trust WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 "
        "ELSE 2 END) AS rank, group_concat(DISTINCT NULLIF(l.source, '')), "
        "COALESCE(p.name, ''), "
        "(SELECT COUNT(DISTINCT o.company_id) FROM company_contacts o "
        " WHERE o.contact_id = ct.id AND o.company_id != l.company_id) "
        "FROM company_contacts l JOIN contacts ct ON ct.id = l.contact_id "
        "LEFT JOIN people p ON p.id = l.person_id WHERE l.company_id = ? "
        "GROUP BY ct.id, l.person_id ORDER BY ct.kind, rank, ct.value", (company_id,)
    ) as cur:
        links = [{"id": r[0], "kind": r[1], "value": r[2], "display": r[3] or r[2],
                  "line_type": r[4], "status": r[5], "do_not_contact": bool(r[6]),
                  "trust": TRUST_LEVELS[r[7]], "sources": sorted(r[8].split(",")) if r[8] else [],
                  "person": r[9], "shared_with": r[10]}
                 for r in await cur.fetchall()]
    for link in links:
        link["source"] = link["sources"][0] if link["sources"] else ""
    async with db.execute(
        f"SELECT p.id, p.name, p.role, p.source, {PERSON_SEARCHED}, "
        "EXISTS (SELECT 1 FROM company_contacts l WHERE l.person_id = p.id "
        "AND l.person_id != 0) "
        "FROM people p WHERE p.company_id = ? ORDER BY p.name_norm", (company_id,)
    ) as cur:
        people = [{"id": r[0], "name": r[1], "role": r[2], "source": r[3],
                   "searched_at": r[4], "reached": bool(r[5])} for r in await cur.fetchall()]
    async with db.execute(
        "SELECT id, name, address, locality, county, phone, source, external_id "
        "FROM locations WHERE company_id = ? ORDER BY id", (company_id,)
    ) as cur:
        places = [dict(zip(("id", "name", "address", "locality", "county", "phone", "source",
                            "external_id"), r)) for r in await cur.fetchall()]
    async with db.execute(
        "SELECT source, ref, first_seen, last_seen FROM company_sources WHERE company_id = ? "
        "ORDER BY first_seen, source", (company_id,)
    ) as cur:
        sources = [dict(zip(("source", "ref", "first_seen", "last_seen"), r))
                   for r in await cur.fetchall()]
    async with db.execute(
        "SELECT * FROM ("
        "SELECT s.id, s.target, '' AS person, s.module, s.capability, s.looked_for, "
        "s.searched_at, s.outcome, s.found, s.cost FROM searches s "
        "WHERE s.target = 'company' AND s.target_id = ? "
        "UNION ALL "
        "SELECT s.id, s.target, p.name, s.module, s.capability, s.looked_for, s.searched_at, "
        "s.outcome, s.found, s.cost FROM people p JOIN searches s ON s.target = 'person' "
        "AND s.target_id = CAST(p.id AS TEXT) WHERE p.company_id = ?) "
        "ORDER BY searched_at DESC, id DESC LIMIT 50", (company_id, company_id)
    ) as cur:
        searches = [dict(zip(("target", "person", "module", "capability", "looked_for",
                              "searched_at", "outcome", "found", "cost"), r[1:]))
                    for r in await cur.fetchall()]
    return {"contacts": links, "people": people, "locations": places, "sources": sources,
            "searches": searches}
