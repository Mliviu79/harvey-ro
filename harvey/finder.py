"""The contact finder: a way to reach every company on file.

Harvey is a lead finder. A company on file with no contact (no email, no
phone, no contact form, no social profile its site links to) is a record to
search, whichever source brought it in. For one company at a time (the
``find_contacts`` job, harvey/modules/job_kinds.py, on the contacts lane):

1. What is on file comes first, and is free: a website the company's own
   key or its registry entry gives is read (the website module: phones,
   emails, the contact form, the social profiles it links to, the people its
   team page names). Phones from official sources (ANAF, the taxpayer file)
   are contacts already: ingest linked them when they arrived.
2. A company with no website and no contact is looked up by its name and
   town through each search module whose capability says ``lookup =
   "website"`` (cheapest first, never a hard-coded list), until one gives a
   candidate. Only inside a spend cap: the run's, or the install's
   ``spend.per_day_usd`` for background work; with neither, it is skipped
   and says so (a search priced at $0 needs no cap). The results are read once, in memory, and never stored: the
   best candidate is read like any website, and only it and what its pages
   publish are kept.
3. A person named at a company with a website, reached by no contact of
   their own, gets one address built from the domain's pattern and checked
   by a verification tier (email_verify), inside the same cap and the
   month's address checks (a credit each: a cap in dollars can't see a
   free tier running out).

Nothing found is thrown away because the match is unproven. A website found
by searching is tagged confirmed (its pages carry the company's CUI, or a
phone or email already on file), likely (the name and the town match) or
unconfirmed, and every contact read off it carries no more than that trust.
Below confirmed, the site says nothing else about the company (no facts, no
new name) and its domain doesn't become the company's key.

Every step is a row in ``searches`` (harvey/contacts.py): a company searched
by one module's capability isn't searched by it again (unless forced, or
past the capability's ``refresh_days``), and can still be searched by any
other. The backlog follows the same rows: a site is to read until a read
looked for its emails (a plain "Read sites" read looks for phones and a
contact form only), and a company with no website is to look up while a
search module that is on and set up hasn't searched it (one added later
searches again what the others found nothing for). A search that failed
isn't a search done.

The finder never depends on the email loop: the Email module's switch
doesn't stop it, and Scout and the heartbeat don't call it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import aiosqlite

from harvey import contacts, resolver
from harvey.modules import api, ingest

logger = logging.getLogger("harvey.finder")

FINDER = ("contact_finder", "find_contacts")
LOOKUP = ("contact_finder", "find_website")
SITE = ("website", "read_site")
# What the finder's site read looks for that a plain read doesn't: only a
# read that looked for emails closes it.
SITE_WANTS = "email"
EMAIL = ("email_verify", "find_email")
# Addresses built and checked per company and job: a registry firm has one
# or two administrators; more is a big company whose site lists its people.
MAX_PEOPLE_PER_COMPANY = 3

LIVE = "COALESCE(c.merged_into, '') = ''"
NO_CONTACT = "NOT EXISTS (SELECT 1 FROM company_contacts cc WHERE cc.company_id = c.id)"
HAS_SITE = "(COALESCE(c.domain, '') != '' OR COALESCE(c.website, '') != '')"


# ── Searches ──

def _not_searched(module: str, capability: str, since: str | None,
                  target_sql: str = "c.id", target: str = "company",
                  looked_for: str = "") -> tuple[str, list]:
    return (contacts.not_searched(target_sql, target, looked_for),
            [module, capability, contacts.search_cutoff(module, capability, since)])


def _site_not_read(since: str | None = None) -> tuple[str, list]:
    return _not_searched(*SITE, since, looked_for=SITE_WANTS)


async def searched(db, target: str, target_id, module: str, capability: str,
                   since: str | None = None, looked_for: str = "") -> bool:
    """Was the record searched by the module's capability (since the
    cutoff: a forced run's start, else the capability's refresh age; one
    that looked for ``looked_for`` when given)?"""
    cond, args = _not_searched(module, capability, since, "?", target, looked_for)
    async with db.execute(f"SELECT {cond}", (str(target_id), *args)) as cur:
        (fresh,) = await cur.fetchone()
    return not fresh


# ── The searches a website lookup can go through ──

@dataclass
class Searcher:
    module: str
    capability: str
    provider: str
    estimate: float
    coordinates: bool = False


def lookup_estimate(provider: str) -> float:
    """What one website lookup through ``provider`` costs."""
    cap = api.capability(*LOOKUP)
    return float(cap.estimate(api.Input(name="x", extra={"provider": provider})))


def searchers(enabled, env: dict) -> list[Searcher]:
    """Every search capability of a module that is on, set up (its keys)
    and able to find a company by name (``lookup = "website"``), cheapest
    first."""
    from harvey.collectors.discover import PROVIDERS
    from harvey.modules import switches

    out = []
    for m, cap in api.declared(switches.by_id()):
        key = cap.get("provider") or ""
        provider = PROVIDERS.get(key)
        if (m.id not in enabled or cap.get("lookup") != "website" or provider is None
                or not provider.configured(env)):
            continue
        out.append(Searcher(m.id, cap["id"], key, lookup_estimate(key),
                            bool(provider.needs_coordinates)))
    return sorted(out, key=lambda s: (s.estimate, s.module, s.capability))


# ── What a run may spend ──

class Spend:
    """What one job's paid steps may spend: ``allow(estimate, first)``
    reserves from the run's cap (and the install's day and month limits),
    and says no, with why, when what is left can't pay. ``cap`` False means
    no cap was set anywhere: paid steps never run then (a free one does)."""

    def __init__(self, reserve=None, record=None, *, cap: bool = True,
                 per_day: float | None = None, per_month: float | None = None):
        self._reserve, self._record = reserve, record
        self.cap = cap
        self.per_day, self.per_month = per_day, per_month
        self.spent = 0.0
        self.refused = ""

    async def allow(self, estimate: float, first: float | None = None) -> float | None:
        """The money a step may spend, or None (``refused`` says why)."""
        from harvey.modules.jobs import SpendLimit

        if not self.cap:
            if estimate <= 0 and (first is None or first <= 0):
                return float("inf")         # free: nothing to cap
            self.refused = ("no spend cap set: a website search runs only inside a run's "
                            "cap, or spend.per_day_usd for background work")
            return None
        if self._reserve is None:
            return float("inf")
        try:
            return await self._reserve(estimate, estimate if first is None else first,
                                       per_day=self.per_day, per_month=self.per_month)
        except SpendLimit as e:
            self.refused = str(e)
            return None

    async def record(self, amount: float) -> None:
        self.spent += float(amount or 0.0)
        if self._record is not None and amount:
            await self._record(amount)


class _Budget(api.Budget):
    def __init__(self, remaining: float):
        self.remaining = remaining


# ── Picking and judging a website ──

def _words(text: str) -> list[str]:
    return [w for w in resolver.fold(text) if len(w) >= 3]


def name_score(company_name: str, record) -> int:
    """How well a search result's name and domain answer a company's name:
    2 the same name (legal forms aside), 1 every word of it in the result's
    name or domain, 0 neither."""
    mine = resolver.name_key(company_name)
    if not mine:
        return 0
    if resolver.name_key(record.name or "") == mine:
        return 2
    words = _words(mine)
    stem = re.sub(r"[^a-z0-9]", "", (record.domain or "").split(".")[0].lower())
    theirs = " ".join(resolver.fold(record.name or ""))
    if words and all(w in stem or w in theirs.split() for w in words):
        return 1
    return 0


def pick(company: dict, records) -> tuple[object, int] | None:
    """The best website candidate among a search's results: one domain
    that is a single business's (not a directory, a booking platform or a
    social network), the best name match first, then the search's order."""
    best = None
    for i, record in enumerate(records):
        domain = (record.domain or "").lower()
        if not domain or not resolver.usable_domain(domain):
            continue
        score = name_score(company.get("name") or "", record)
        if best is None or score > best[1]:
            best = (record, score, i)
    return (best[0], best[1]) if best else None


def _cui_on(text: str, cui: str) -> bool:
    digits = re.sub(r"\D", "", cui or "")
    if len(digits) < 2:
        return False
    return re.search(r"(?<!\d)(?:RO\s*)?" + digits + r"(?!\d)", text, re.I) is not None


def judge(company: dict, on_file: set[str], score: int, *, floor: str = "unconfirmed"):
    """``judge(draft)``: how sure Harvey is that a site it read is the
    company's. confirmed when a page carries its CUI, or names a phone or
    email already on file for it; likely when the name matched (``score``)
    and a page names its town; else ``floor``."""
    town = resolver.locality_key(company.get("location") or "")

    def verdict(draft) -> str:
        text = " ".join(p.html for p in draft.pages)
        if company.get("cui") and _cui_on(text, company["cui"]):
            return "confirmed"
        if on_file and any(c.value.lower() in on_file for c in draft.contacts):
            return "confirmed"
        folded = " ".join(resolver.fold(re.sub(r"(?s)<[^>]+>", " ", text)))
        if score and (not town or town in folded):
            return "likely"
        return floor

    return verdict


# ── One company ──

async def company_row(state, company_id: str) -> dict | None:
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT c.id, c.name, c.domain, c.website, c.website_trust, c.location, c.cui, "
                f"c.phone, c.source_url FROM companies c WHERE c.id = ? AND {LIVE}",
                (company_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _on_file(state, company_id: str) -> set[str]:
    """The phones and emails on file for a company (to confirm a site by)."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT ct.value FROM company_contacts l JOIN contacts ct ON ct.id = l.contact_id "
                "WHERE l.company_id = ? AND ct.kind IN ('phone', 'email')", (company_id,)) as cur:
            return {r[0].lower() for r in await cur.fetchall()}


async def _people_to_search(state, company_id: str, since: str | None) -> list[dict]:
    cond, args = _not_searched(*EMAIL, since, "CAST(p.id AS TEXT)", "person")
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT p.id, p.name, p.role, p.source FROM people p WHERE p.company_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM company_contacts l WHERE l.person_id = p.id "
                f"AND l.person_id != 0) AND {cond} ORDER BY p.id LIMIT ?",
                (company_id, *args, MAX_PEOPLE_PER_COMPANY)) as cur:
            return [dict(r) for r in await cur.fetchall()]


split_name = contacts.split_name


def site_of(company: dict) -> tuple[str, str]:
    """(domain to read, trust floor): the company's own key, else the
    website it declares (a registry's field, or one the finder found)."""
    from harvey.collectors.discover import normalize_domain

    if company.get("domain"):
        return company["domain"], ""
    website = normalize_domain(company.get("website") or "")
    if website and resolver.usable_domain(website):
        return website, company.get("website_trust") or "likely"
    return "", ""


@dataclass
class Found:
    """What the finder did for one company."""

    company_id: str
    steps: list[str] = field(default_factory=list)
    contacts: int = 0
    people: int = 0
    website: str = ""
    trust: str = ""
    cost: float = 0.0
    skipped: list[str] = field(default_factory=list)
    failed: int = 0

    def as_dict(self) -> dict:
        return {"company_id": self.company_id, "steps": self.steps, "contacts": self.contacts,
                "people": self.people, "website": self.website, "trust": self.trust,
                "cost": round(self.cost, 4), "skipped": self.skipped}


async def find(state, company_id: str, *, spend: Spend, since: str | None = None,
               batch_id: str = "", config=None, env: dict | None = None,
               run_id: str = "") -> Found:
    """Search one company for every way to reach it (the module docstring's
    three steps), each written through ingest as it is found."""
    from harvey.modules import switches

    out = Found(company_id)
    company = await company_row(state, company_id)
    if company is None:
        out.skipped.append("merged into another company, or gone")
        return out
    env = env if env is not None else _env()
    enabled = await switches.enabled_ids(state)
    confirmed = set(await state.confirmed_signal_codes())
    domain, floor = site_of(company)
    async with state._connect() as db:
        site_done = bool(domain) and await searched(db, "company", company_id, *SITE, since,
                                                    looked_for=SITE_WANTS)
        had_contact = await _has_contact(db, company_id)

    if domain and not site_done:
        on_file = await _on_file(state, company_id)
        verdict = judge(company, on_file, 1, floor=floor) if floor else None
        async with _reader(state, confirmed, config) as read:
            done = await read(company, domain=domain, judge=verdict, run_id=run_id)
        out.steps.append("site")
        if done is not None:
            out.contacts += done.contacts
            out.people += done.people
            out.trust = verdict(done.draft) if verdict else "confirmed"
        out.website = domain
    elif not domain and (since or not had_contact):
        # (Forced, a company with a phone already is looked up too.)
        found = await _look_up(state, company, out, spend=spend, since=since,
                               batch_id=batch_id, enabled=enabled, env=env,
                               confirmed=confirmed, config=config, run_id=run_id)
        if found:
            domain = found
    trust = out.trust or floor or "confirmed"
    if domain and trust != "unconfirmed" and spend.cap:
        # (An address built at a domain that may not be the company's would
        # be a guess about someone else's mailbox.)
        await _addresses(state, company, domain, out, trust, spend=spend, since=since,
                         batch_id=batch_id, enabled=enabled, env=env)
    if out.steps or out.failed:
        # Every search failed: an error, which isn't a search done.
        await state.record_search(
            "company", company_id, *FINDER, looked_for="website,people,email,phone",
            outcome="found" if out.contacts else "nothing" if out.steps else "error",
            found=out.contacts, cost=out.cost, batch_id=batch_id,
            detail="; ".join(out.steps + out.skipped))
    return out


async def _has_contact(db, company_id: str) -> bool:
    async with db.execute("SELECT 1 FROM company_contacts WHERE company_id = ? LIMIT 1",
                          (company_id,)) as cur:
        return await cur.fetchone() is not None


def _reader(state, confirmed, config):
    from harvey.collectors.ats import role_keywords
    from harvey.modules.runs import site_reader

    return site_reader(state, confirmed, config=config, contacts=True,
                       role_keywords=role_keywords(getattr(config, "icp", None)))


def _env() -> dict:
    try:
        from harvey.config import load_env

        return load_env().model_dump()
    except Exception:
        return {}


async def _look_up(state, company: dict, out: Found, *, spend: Spend, since, batch_id,
                   enabled, env, confirmed, config, run_id) -> str:
    """Step 2: the company's name and town through each search that can
    find it, cheapest first, until one gives a website. Returns the domain
    read, or ""."""
    from harvey.collectors.discover import TIMEOUT
    import httpx

    company_id = company["id"]
    name = (company.get("name") or "").strip()
    if not resolver.name_key(name):
        out.skipped.append("no name to search by")
        return ""
    available = searchers(enabled, env)
    if not available:
        out.skipped.append("no search module that can find a company by name is on and set up")
        return ""
    cap = api.capability(*LOOKUP)
    town = (company.get("location") or "").split(",")[0].strip()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for s in available:
            async with state._connect() as db:
                if await searched(db, "company", company_id, s.module, s.capability, since):
                    continue
            room = await spend.allow(s.estimate)
            if room is None:
                out.skipped.append(f"website search skipped: {spend.refused}")
                return ""
            point = ""
            if s.coordinates:
                from harvey.modules.runs import geocode_city

                point = await geocode_city(state, company.get("location") or "")
            inp = api.Input(company_id=company_id, name=name, locality=town, point=point,
                            extra={"provider": s.provider})
            ctx = api.Context(http=client, env=env, enabled=frozenset(enabled),
                              budget=_Budget(room),
                              settings={"provider": s.provider,
                                        "searcher": (s.module, s.capability)})
            try:
                draft = await cap(inp, ctx)
            except Exception as e:
                draft = api.Draft(failed=str(e))
            await spend.record(draft.cost)
            out.cost += draft.cost
            if draft.failed is None:
                out.steps.append(f"search:{s.module}")
            else:
                out.failed += 1
                out.skipped.append(f"{s.module} search failed: {draft.failed}")
            choice = pick(company, draft.records) if draft.failed is None else None
            await state.record_search(
                "company", company_id, s.module, s.capability, looked_for="website",
                outcome="error" if draft.failed is not None else "found" if choice else "nothing",
                found=1 if choice else 0, cost=draft.cost, batch_id=batch_id,
                detail=draft.failed or (choice[0].domain if choice else ""))
            if choice is None:
                if draft.stop:
                    out.skipped.append(f"{s.module}: {draft.stop}")
                continue
            record, score = choice
            domain = record.domain.lower()
            on_file = await _on_file(state, company_id)
            verdict = judge(company, on_file, score)
            async with _reader(state, confirmed, config) as read:
                done = await read(company, domain=domain, judge=verdict, run_id=run_id)
            trust = verdict(done.draft) if done is not None else ("likely" if score else
                                                                  "unconfirmed")
            await state.set_found_website(
                company_id, record.website or f"https://{domain}",
                domain=domain if trust == "confirmed" else "", trust=trust,
                source=s.module)
            out.website, out.trust = domain, trust
            out.steps.append("site")
            if done is not None:
                out.contacts += done.contacts
                out.people += done.people
            return domain
    return ""


async def _addresses(state, company: dict, domain: str, out: Found, trust: str, *,
                     spend: Spend, since, batch_id, enabled, env) -> None:
    """Step 3: one checked address for each person at the company reached
    by no contact of their own (a verification credit each)."""
    if EMAIL[0] not in enabled:
        return
    company_id = company["id"]
    people = await _people_to_search(state, company_id, since)
    if not people:
        return
    try:
        cached = await state.get_email_pattern(domain)
    except Exception:
        cached = None
    cap = api.capability(*EMAIL)
    credits = ingest.Allowance(state)
    for person in people:
        first, last = split_name(person["name"], person.get("source") or "")
        if not (first and last):
            await state.record_search("person", person["id"], *EMAIL, looked_for="email",
                                      outcome="nothing", batch_id=batch_id,
                                      detail="no first and last name", origin=company_id)
            continue
        # A verification credit each, counted against the month's checks
        # (email_verify's monthly_searches): free tiers run out, and a
        # cap in dollars can't see them.
        if await credits.left(EMAIL[0]) <= 0:
            out.skipped.append("address check skipped: this month's address checks are "
                               "used (EMAIL_VERIFY_MONTHLY_CHECKS)")
            return
        await credits.take(EMAIL[0])
        ctx = api.Context(env=env, enabled=frozenset(enabled), budget=credits)
        try:
            draft = await cap(api.Input(domain=domain, facts={"EMAIL_PATTERN": cached}, extra={
                "first_name": first, "last_name": last, "verify": True}), ctx)
        except Exception as e:
            draft = api.Draft(failed=str(e))
        await ingest.learned_pattern(state, domain, draft.values.get("email_pattern"))
        kept = 0
        if draft.failed is None and draft.contacts:
            kept = await ingest.person_contacts(state, company_id, person["name"],
                                                person.get("role") or "", draft.contacts,
                                                trust=trust)
        out.contacts += kept
        out.steps.append("address")
        await state.record_search(
            "person", person["id"], *EMAIL, looked_for="email",
            outcome="error" if draft.failed is not None else "found" if kept else "nothing",
            found=kept, batch_id=batch_id, detail=draft.failed or "", origin=company_id)


# ── The backlog: every company with no contact ──

# backlog()'s counts read every company (seconds on a full registry): kept
# this long per database, so the Contacts page doesn't count on every load.
BACKLOG_CACHE_SECONDS = 120
_counted: dict[str, tuple[float, tuple]] = {}


async def backlog(state, fresh: bool = False) -> dict:
    """How many companies have no contact; of them, how many have a website
    still to read (free) and how many were looked at by the finder; and
    what the finder found in the last day. The counts are kept for
    BACKLOG_CACHE_SECONDS (``fresh`` counts again)."""
    import time

    site_cond, site_args = _site_not_read()
    finder_cond, finder_args = _not_searched(*FINDER, None)
    key = str(state.db_path)
    kept = _counted.get(key)
    async with state._connect() as db:
        if fresh or kept is None or time.monotonic() - kept[0] > BACKLOG_CACHE_SECONDS:
            async with db.execute(
                    f"SELECT COUNT(*), SUM({HAS_SITE} AND {site_cond}), SUM(NOT ({finder_cond})) "
                    f"FROM companies c WHERE {LIVE} AND {NO_CONTACT}",
                    (*site_args, *finder_args)) as cur:
                kept = (time.monotonic(), await cur.fetchone())
            _counted[key] = kept
        total, free, searched_ = kept[1]
        async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(found), 0), COALESCE(SUM(cost), 0) FROM searches "
                "WHERE target = 'company' AND module = ? AND capability = ? "
                "AND searched_at >= datetime('now', '-1 day')", FINDER) as cur:
            day, day_found, day_cost = await cur.fetchone()
        async with db.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE kind = 'find_contacts' "
                "AND status IN ('queued', 'running', 'paused') GROUP BY status") as cur:
            waiting = {s: n for s, n in await cur.fetchall()}
    return {"without_contact": total or 0, "site_to_read": free or 0,
            "searched": searched_ or 0, "last_day": {"companies": day or 0,
                                                     "found": day_found or 0,
                                                     "cost": round(day_cost or 0.0, 4)},
            "jobs": waiting}


async def next_free(state, limit: int) -> list[str]:
    """Companies with no contact and a website not read for its emails
    yet, newest first: the free work the background takes next. Walks the
    companies with a domain or a website (their indexes), never the whole
    table: on a registry most companies have neither."""
    site_cond, site_args = _site_not_read()
    rest = f"{LIVE} AND {NO_CONTACT} AND {site_cond}"
    async with state._connect() as db:
        async with db.execute(
                "SELECT id FROM ("
                f"SELECT c.id, c.created_at, c.rowid AS r FROM companies c "
                f"WHERE c.domain > '' AND {rest} "
                "UNION ALL "
                f"SELECT c.id, c.created_at, c.rowid FROM companies c "
                f"WHERE c.website != '' AND COALESCE(c.domain, '') = '' AND {rest}) "
                "ORDER BY created_at DESC, r DESC LIMIT ?",
                (*site_args, *site_args, int(limit))) as cur:
            return [r[0] for r in await cur.fetchall()]


async def next_paid(state, limit: int, available: list[Searcher] | None = None) -> list[str]:
    """Companies with no contact and no website that at least one of the
    search modules able to find them (``available``; every one on and set
    up when None) hasn't searched yet, newest first: the background's paid
    work, when a day's budget is set. A module added later brings back
    the companies the others found nothing for."""
    if available is None:
        from harvey.modules import switches

        available = searchers(await switches.enabled_ids(state), _env())
    if not available:
        return []
    conds, args = [], []
    for s in available:
        cond, cond_args = _not_searched(s.module, s.capability, None)
        conds.append(cond)
        args += cond_args
    async with state._connect() as db:
        async with db.execute(
                f"SELECT c.id FROM companies c WHERE {LIVE} AND NOT {HAS_SITE} AND {NO_CONTACT} "
                f"AND COALESCE(c.name, '') != '' AND ({' OR '.join(conds)}) "
                "ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
                (*args, int(limit))) as cur:
            return [r[0] for r in await cur.fetchall()]


# ── A selection (the Contacts and Companies pages' Find contacts) ──

# Company ids per query when a selection is given as ids (well under any
# SQLite build's limit on parameters).
IDS_PER_QUERY = 500


async def select(state, f: contacts.ContactFilter | None = None, *, ids=None,
                 force: bool = False, limit: int = 1000,
                 attach_figures: bool = False) -> list[dict]:
    """The companies a Find-contacts run takes: those matching the filter's
    company conditions (a value not known yet is searched, like a
    "possible" in the cohort builder) and with no contact (every match with
    ``force``), with whether each has a website, up to ``limit``. Given
    ``ids``, only those, in pieces of IDS_PER_QUERY."""
    f = f or contacts.ContactFilter()
    with_sql, params = (f.cohort if f.cohort is not None else ("", []))
    conds, args = contacts.company_conditions(f, possible=True)
    if not force:
        conds.append(NO_CONTACT)
    for word in (f.q or "")[:200].split()[:8]:
        conds.append(f"{contacts.sql_name_norm('c.name')} LIKE ? ESCAPE '\\'")
        args.append(contacts.like(contacts.fold(word)))
    pieces: list[list[str] | None] = [None]
    if ids is not None:
        ids = list(dict.fromkeys(i for i in ids if i))
        pieces = [ids[at:at + IDS_PER_QUERY] for at in range(0, len(ids), IDS_PER_QUERY)]
    out: list[dict] = []
    async with state._connect() as db:
        if attach_figures:
            from harvey.modules.ro.mf_financials import store as fin_store

            await db.execute("ATTACH DATABASE ? AS fin",
                             (f"file:{fin_store.FINANCIALS_PATH}?mode=ro",))
        for piece in pieces:
            if len(out) >= limit:
                break
            where, where_args = list(conds), list(args)
            if piece is not None:
                where.append(f"c.id IN ({','.join('?' * len(piece))})")
                where_args += piece
            sql = (f"{with_sql}SELECT c.id, {HAS_SITE} AS site FROM companies c WHERE "
                   + " AND ".join(where) + " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?")
            async with db.execute(sql, (*params, *where_args, int(limit) - len(out))) as cur:
                out += [{"id": r[0], "site": bool(r[1])} for r in await cur.fetchall()]
    return out


def estimate(selected: list[dict], enabled, env: dict) -> dict:
    """What a Find-contacts run over ``selected`` would cost: a website
    read is free; a company with no website costs one lookup through the
    cheapest search that can find it (more when that one finds nothing and
    the next is tried: the cap stops it)."""
    found = searchers(enabled, env)
    paid = sum(1 for c in selected if not c["site"])
    cheapest = found[0].estimate if found else 0.0
    return {"companies": len(selected), "with_site": len(selected) - paid,
            "to_look_up": paid, "estimated_cost": round(paid * cheapest, 4),
            "most": round(paid * sum(s.estimate for s in found), 4),
            "searchers": [{"module": s.module, "capability": s.capability,
                           "provider": s.provider, "cost": s.estimate} for s in found]}
