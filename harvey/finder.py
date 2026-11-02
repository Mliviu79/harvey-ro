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
   free tier running out). An address the site publishes as the person's
   own is linked to them as it is, with no check, cap or not.

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
doesn't stop it, and the heartbeat doesn't call it.
"""

from __future__ import annotations

import asyncio
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
# read that looked for the owner closes it (runs.CONTACT_READ). A read that
# looked for emails only on the homepage and the first team or contact path
# that answered (before the finder followed the site's menu) doesn't: those
# sites are read once more, the deeper way.
SITE_WANTS = "owner"
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
                "c.phone, c.source_url, c.source, c.company_size "
                f"FROM companies c WHERE c.id = ? AND {LIVE}",
                (company_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


def market_country(company: dict) -> str:
    """The company's country as two letters, when something says: the one
    its location certainly names, else the market of the official source
    it came from (that module's one market: a trade registry's record is
    in the registry's country). "" otherwise."""
    from harvey.modules import switches
    from harvey.send_window import country_for

    country, certain = country_for(company.get("location") or "")
    if country and certain:
        return country.upper()
    src = (company.get("source") or "").strip()
    src = src.split(":", 1)[1] if src.startswith("registry:") else src
    module = next((m for m in switches.manifests() if src and m.id == src), None)
    if module is not None and module.official and len(module.markets) == 1:
        return str(module.markets[0]).upper()
    return ""


async def _on_file(state, company_id: str) -> set[str]:
    """The phones and emails on file for a company (to confirm a site by)."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT ct.value FROM company_contacts l JOIN contacts ct ON ct.id = l.contact_id "
                "WHERE l.company_id = ? AND ct.kind IN ('phone', 'email')", (company_id,)) as cur:
            return {r[0].lower() for r in await cur.fetchall()}


async def _people_to_search(state, company_id: str, since: str | None,
                            limit: int = MAX_PEOPLE_PER_COMPANY) -> list[dict]:
    cond, args = _not_searched(*EMAIL, since, "CAST(p.id AS TEXT)", "person")
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT p.id, p.name, p.role, p.source FROM people p WHERE p.company_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM company_contacts l WHERE l.person_id = p.id "
                f"AND l.person_id != 0) AND {cond} ORDER BY p.id LIMIT ?",
                (company_id, *args, limit)) as cur:
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
               run_id: str = "", background: bool = False,
               want_website: bool = False, addresses: bool = True) -> Found:
    """Search one company for every way to reach it (the module docstring's
    three steps), each written through ingest as it is found.
    ``background``: the backlog's walk, which may also search inside the
    search modules' free allowances (its share of them). ``want_website``:
    look a company with no website up even when it has a phone (a Discover
    run's "find websites"). ``addresses`` False: no address is built and
    checked (a verification credit each), only sites read and searched and
    the addresses a site publishes as a person's own linked to them."""
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
    elif not domain and (since or want_website or not had_contact):
        # (Forced, a company with a phone already is looked up too.)
        found = await _look_up(state, company, out, spend=spend, since=since,
                               batch_id=batch_id, enabled=enabled, env=env,
                               confirmed=confirmed, config=config, run_id=run_id,
                               background=background)
        if found:
            domain = found
    trust = out.trust or floor or "confirmed"
    if domain and trust != "unconfirmed":
        # (An address built at a domain that may not be the company's would
        # be a guess about someone else's mailbox.) With no cap, or no
        # address checks wanted, only the addresses the site publishes as a
        # person's own are linked to them: that costs nothing.
        await _addresses(state, company, domain, out, trust, spend=spend, since=since,
                         batch_id=batch_id, enabled=enabled, env=env, config=config,
                         verify=bool(spend.cap and addresses))
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


# ── The background's share of the search modules' free allowances ──

async def background_settings(state) -> tuple[bool, float]:
    """(whether the backlog's walk searches inside free allowances, the
    share of each allowance it may take): the contact finder's settings
    background_search and background_share."""
    from harvey.modules import switches

    try:
        s = await switches.module_settings(state, FINDER[0])
    except Exception:
        s = {}
    return bool(s.get("background_search", True)), float(s.get("background_share", 0.5) or 0.0)


def free_for_background(module: str, on: bool, share: float) -> int | None:
    """Searches a module may still make free for the background this
    period (lanes.free_left over its share), None for a module that
    declares no free allowance; 0 when the switch is off."""
    from harvey.modules import lanes

    if not lanes.free_allowances(module):
        return None
    return lanes.free_left(module, share) if on and not lanes.held(module) else 0


async def free_for_background_now(module: str, on: bool, share: float) -> int | None:
    """free_for_background off the event loop (its counters sit behind an
    OS file lock)."""
    return await asyncio.to_thread(free_for_background, module, on, share)


async def _look_up(state, company: dict, out: Found, *, spend: Spend, since, batch_id,
                   enabled, env, confirmed, config, run_id, background: bool = False) -> str:
    """Step 2: the company's name and town through each search that can
    find it, cheapest first, until one gives a website. In the background,
    a search with some of its free allowance left for the background comes
    first, and costs nothing (its own counters keep the share); one whose
    free allowance is used runs only when a day's budget can pay for it.
    Returns the domain read, or ""."""
    from harvey.collectors.discover import TIMEOUT
    from harvey.modules import lanes
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
    on, share = False, 0.0
    if background:
        on, share = await background_settings(state)
        free = {s.module: await free_for_background_now(s.module, on, share)
                for s in available}
        available = sorted(available, key=lambda s: (not free.get(s.module), s.estimate,
                                                     s.module, s.capability))
    cap = api.capability(*LOOKUP)
    town = (company.get("location") or "").split(",")[0].strip()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for s in available:
            async with state._connect() as db:
                if await searched(db, "company", company_id, s.module, s.capability, since):
                    continue
            if background and await asyncio.to_thread(lanes.held, s.module):
                # (It said it can take no more for now: out of credit, a
                # spent quota, a refused key.)
                _skip(out, f"{s.module}: said it can take no more searches for now")
                continue
            # (Counted again here: the lookup before this one may have used it up.)
            left = await free_for_background_now(s.module, on, share) if background else None
            gratis = bool(left)
            if left is not None and not gratis and not (spend.cap and s.estimate > 0):
                _skip(out, f"{s.module}: no free searches left for the background for now")
                continue
            room = float("inf") if gratis else await spend.allow(s.estimate)
            if room is None:
                _skip(out, f"website search skipped: {spend.refused}")
                continue
            point = ""
            if s.coordinates:
                from harvey.modules.runs import geocode_city

                point = await geocode_city(state, company.get("location") or "")
            inp = api.Input(company_id=company_id, name=name, locality=town, point=point,
                            extra={"provider": s.provider, "country": market_country(company)})
            ctx = api.Context(http=client, env=env, enabled=frozenset(enabled),
                              budget=_Budget(room),
                              settings={"provider": s.provider,
                                        "searcher": (s.module, s.capability)})
            try:
                draft = await cap(inp, ctx)
            except Exception as e:
                draft = api.Draft(failed=str(e))
            # Inside the free allowance a search costs nothing.
            cost = 0.0 if gratis else draft.cost
            await spend.record(cost)
            out.cost += cost
            if draft.failed is None:
                out.steps.append(f"search:{s.module}")
            else:
                out.failed += 1
                out.skipped.append(f"{s.module} search failed: {draft.failed}")
            choice = pick(company, draft.records) if draft.failed is None else None
            await state.record_search(
                "company", company_id, s.module, s.capability, looked_for="website",
                outcome="error" if draft.failed is not None else "found" if choice else "nothing",
                found=1 if choice else 0, cost=cost, batch_id=batch_id,
                detail=draft.failed or (choice[0].domain if choice else ""))
            if draft.failed is not None and draft.stop:
                # Out of credit or a refused key: the background leaves it
                # alone for a while (until when it said, when it did).
                await asyncio.to_thread(lanes.hold, s.module, draft.values.get("retry_after"))
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


def _skip(out: Found, why: str) -> None:
    if why not in out.skipped:
        out.skipped.append(why)


async def _addresses(state, company: dict, domain: str, out: Found, trust: str, *,
                     spend: Spend, since, batch_id, enabled, env, config=None,
                     verify: bool = True) -> None:
    """Step 3: one checked address for each person at the company reached
    by no contact of their own (a verification credit each). What the
    addresses on file at the domain say comes first (email_finder's
    site_email_evidence): a person's own published address is linked to
    them as it is, with no check spent; the others are built the way those
    addresses are spelled, and never as one another named there could own.
    With no evidence and no pattern learned, a small company's address is
    built as first@ (SMALL_BUSINESS_PATTERN, see small_company), any
    other's as first.last@. A built address no verifier confirmed stays a
    guess, kept no surer than "likely". ``verify`` False (no cap): only the
    published addresses are linked, for everyone named there, and nobody
    else is marked searched."""
    from harvey.integrations import email_finder as ef

    if EMAIL[0] not in enabled:
        return
    company_id = company["id"]
    # (Linking costs nothing: everyone named, not the few a check is spent on.)
    people = await _people_to_search(state, company_id, since,
                                     MAX_PEOPLE_PER_COMPANY if verify else -1)
    if not people:
        return
    try:
        cached = await state.get_email_pattern(domain)
    except Exception:
        cached = None
    cap = api.capability(*EMAIL)
    credits = ingest.Allowance(state)
    named = await _named_at(state, company_id)
    twins = _twins(named)
    searching = {p["id"] for p in people}
    # Each name once: a registry entry and a team page naming the same
    # person read the same, and an address is no less theirs for it.
    names = list({_reading_key(*n): n for n in (split_name(p["name"], p["source"] or "")
                                                for p in named) if all(n)}.values())
    known, _, _ = ef.site_email_evidence(await _emails_at(state, company_id, domain), names,
                                         domain)
    default = (ef.SMALL_BUSINESS_PATTERN if await small_company(state, company, config)
               else ef.DEFAULT_PATTERN)
    # Each name read checked once at the company, for whoever it was read
    # for first: father and son both "Ion Popescu" can't be told apart by
    # an address, and one mailbox isn't two people's. Kept with whether it
    # came back invalid: only then may the next reading be tried.
    checked: dict[str, tuple[str, bool]] = {}
    linked = await _linked_emails(state, company_id)
    for other in named:
        # Linked to their own address on an earlier pass: still theirs alone.
        # (Only linked: someone searched with nothing found doesn't own it.)
        if other["id"] not in searching:
            own, key = _own_address(known, contacts.name_orders(other["name"], other["source"] or ""))
            if own and (other["id"], own.lower()) in linked:
                checked.setdefault(key, (other["name"], False))
    for person in people:
        source = person.get("source") or ""
        twin = twins.get(person["id"])
        if twin is not None and twin["id"] in searching and _official(twin["source"]):
            # A team page naming the registry's administrator: the registry
            # entry is searched, in the order the page confirms.
            if not verify:
                continue
            await state.record_search("person", person["id"], *EMAIL, looked_for="email",
                                      outcome="nothing", batch_id=batch_id,
                                      detail=f"searched as {twin['name']}", origin=company_id)
            continue
        if twin is not None and not _official(twin["source"]) and _official(source):
            orders, confirmed = [contacts.name_orders(twin["name"], twin["source"])[0]], True
        else:
            orders, confirmed = contacts.name_orders(person["name"], source), False
        if not orders:
            if not verify:
                continue
            await state.record_search("person", person["id"], *EMAIL, looked_for="email",
                                      outcome="nothing", batch_id=batch_id,
                                      detail="no first and last name", origin=company_id)
            continue
        own, key = _own_address(known, orders)
        if own:
            if key in checked:
                # Someone else here reads the same: one mailbox isn't two people's.
                if not verify:
                    continue
                await state.record_search(
                    "person", person["id"], *EMAIL, looked_for="email", outcome="nothing",
                    batch_id=batch_id, origin=company_id,
                    detail=f"read the same as {checked[key][0]}: an address can't tell "
                           "them apart")
                continue
            checked[key] = (person["name"], False)
            # Their own address, published on the company's site: theirs
            # as it is, graded later like any published address.
            kept = await ingest.person_contacts(
                state, company_id, person["name"], person.get("role") or "",
                [api.Contact("email", own, module="website", capability="read_site")],
                trust=trust)
            out.contacts += kept
            await state.record_search(
                "person", person["id"], *EMAIL, looked_for="email", outcome="found",
                found=kept, batch_id=batch_id, origin=company_id,
                detail="their own address, published on the company's site")
            continue
        if not verify:
            continue
        # A hyphenated given name whole first ("anamaria"), then its first
        # part ("ana") if that comes back invalid.
        readings = list(dict.fromkeys(
            r for first, last in orders
            for r in ((first, last), (contacts.short_given(first), last))))
        draft, tried, same_as = None, [], ""
        for first, last in readings:
            key = _reading_key(first, last)
            if key in checked:
                # Read so for someone else already: an invalid answer lets
                # the next reading be tried, any other ends here (the
                # other order is never tried in place of the likelier one).
                who, invalid = checked[key]
                if invalid:
                    continue
                same_as = who
                break
            # A verification credit each, counted against the month's checks
            # (email_verify's monthly_searches): free tiers run out, and a
            # cap in dollars can't see them. The other order is checked only
            # when the likelier one comes back invalid.
            if await credits.left(EMAIL[0]) <= 0:
                _skip(out, "address check skipped: this month's address checks are "
                           "used (EMAIL_VERIFY_MONTHLY_CHECKS)")
                if draft is None:
                    return
                break
            await credits.take(EMAIL[0])
            ctx = api.Context(env=env, enabled=frozenset(enabled), budget=credits)
            try:
                draft = await cap(api.Input(domain=domain, facts={"EMAIL_PATTERN": cached},
                                            extra={"first_name": first, "last_name": last,
                                                   "verify": True,
                                                   "known_emails": known or None,
                                                   "other_people": names or None,
                                                   "default_pattern": default}), ctx)
            except Exception as e:
                draft = api.Draft(failed=str(e))
            tried.append(f"{first} {last}")
            await ingest.learned_pattern(state, domain, draft.values.get("email_pattern"))
            if draft.values.get("email_pattern"):
                try:
                    cached = await state.get_email_pattern(domain)
                except Exception:
                    pass
            invalid = draft.failed is None and any(c.status == "invalid"
                                                   for c in draft.contacts)
            checked[key] = (person["name"], invalid)
            if not invalid:
                break
        if draft is None:
            await state.record_search(
                "person", person["id"], *EMAIL, looked_for="email", outcome="nothing",
                batch_id=batch_id, origin=company_id,
                detail=f"read the same as {same_as or checked[_reading_key(*readings[0])][0]}: "
                       "an address can't tell them apart")
            continue
        kept = 0
        if draft.failed is None and draft.contacts:
            kept = await ingest.person_contacts(state, company_id, person["name"],
                                                person.get("role") or "", draft.contacts,
                                                trust=_guess_trust(draft.contacts, trust))
        out.contacts += kept
        out.steps.append("address")
        # The registry text stays the person's name as written; the order
        # used is said here, and that the company's own site confirmed it.
        order = f"read as {tried[-1]}" + (" (the company's site names them so)"
                                          if confirmed else "")
        await state.record_search(
            "person", person["id"], *EMAIL, looked_for="email",
            outcome="error" if draft.failed is not None else "found" if kept else "nothing",
            found=kept, batch_id=batch_id,
            detail="; ".join(x for x in (draft.failed or "", order) if x), origin=company_id)


def _guess_trust(found, trust: str) -> str:
    """A built address no verifier confirmed (a guess, or "risky": a
    catch-all domain accepts every address) is no surer than "likely",
    whatever the site's trust: the domain may be the company's, the
    mailbox is unproven."""
    if trust == "confirmed" and any(c.status in ("guess", "risky") for c in found):
        return "likely"
    return trust


def _own_address(known, orders) -> tuple[str, str]:
    """(the address published as this person's, site_email_evidence's
    ``known``, read in any of their name's orders; that reading's key), or
    ("", "") when none is."""
    for first, last in orders:
        mine = _reading_key(first, last)
        for addr, k_first, k_last in known or ():
            if _reading_key(k_first, k_last) == mine:
                return addr, mine
    return "", ""


async def _linked_emails(state, company_id: str) -> set[tuple[int, str]]:
    """(person id, address) for every email linked to a person at the company."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT l.person_id, LOWER(ct.value) FROM company_contacts l "
                "JOIN contacts ct ON ct.id = l.contact_id "
                "WHERE l.company_id = ? AND l.person_id != 0 AND ct.kind = 'email'",
                (company_id,)) as cur:
            return {(r[0], r[1]) for r in await cur.fetchall()}


async def _emails_at(state, company_id: str, domain: str) -> list[str]:
    """The addresses a source published for a company at ``domain`` (its
    site, a registry, a listing). An address built here (an address check's
    find_email) is no evidence of how the company spells its addresses,
    nor is one a check found invalid or that stays a guess: learning from
    those would pass a guess off as published, or teach the next check a
    pattern nobody confirmed."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT DISTINCT ct.value FROM company_contacts l "
                "JOIN contacts ct ON ct.id = l.contact_id "
                "WHERE l.company_id = ? AND ct.kind = 'email' AND ct.value LIKE ? "
                "AND COALESCE(ct.status, '') NOT IN ('guess', 'invalid') "
                "AND NOT (COALESCE(l.capability, '') = ? AND COALESCE(l.source, '') = ?)",
                (company_id, f"%@{domain}", EMAIL[1], EMAIL[0])) as cur:
            return [r[0] for r in await cur.fetchall()]


# A company this size or smaller is "small" (first@ before first.last@).
SMALL_COMPANY_MAX = 20
# Sources that list local businesses (a shop, a practice, a trade): a
# company found there is small.
LOCAL_BUSINESS_SOURCES = ("discover:osm", "discover:dataforseo_listings")


def _upper(text) -> int | None:
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(text or ""))]
    return max(nums) if nums else None


async def small_company(state, company: dict, config=None) -> bool:
    """Is there a size signal independent of the website? The ICP's
    company size, the company's size on file, its latest employee count
    from the financial statements (EMPLOYEES), or a local-business listing
    (OpenStreetMap, DataForSEO Business Listings) among its sources. Three
    executives on a homepage say nothing about headcount."""
    icp = getattr(config, "icp", None)
    bound = _upper(getattr(icp, "company_size", "") or "")
    if bound is not None and bound <= SMALL_COMPANY_MAX:
        return True
    size = _upper(company.get("company_size"))
    if size is not None and size <= SMALL_COMPANY_MAX:
        return True
    if (company.get("source") or "") in LOCAL_BUSINESS_SOURCES:
        return True
    marks = ",".join("?" * len(LOCAL_BUSINESS_SOURCES))
    async with state._connect() as db:
        async with db.execute(
                f"SELECT 1 FROM company_sources WHERE company_id = ? AND source IN ({marks}) "
                "LIMIT 1", (company["id"], *LOCAL_BUSINESS_SOURCES)) as cur:
            if await cur.fetchone():
                return True
        async with db.execute(
                "SELECT value_num FROM observations WHERE company_id = ? "
                "AND signal_code = 'EMPLOYEES' AND value_num IS NOT NULL "
                # (One reading per financial year, the year in value_text.)
                "ORDER BY value_text DESC, observed_at DESC LIMIT 1", (company["id"],)) as cur:
            row = await cur.fetchone()
    return row is not None and row[0] <= SMALL_COMPANY_MAX


async def _named_at(state, company_id: str) -> list[dict]:
    """Everyone named at a company (to confirm a name's order by)."""
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, source FROM people WHERE company_id = ?",
                              (company_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _official(source: str) -> bool:
    """Is a name from an official source whose market says how it writes
    names (so its order is a guess a site can settle)?"""
    return contacts.name_order(source or "") != "given_first"


def _reading_key(first: str, last: str) -> str:
    """A name read as (first, last), as an address would spell it."""
    return "".join(resolver.fold(first)) + "|" + "".join(resolver.fold(last))


def _same_person(official: dict, other: dict) -> bool:
    """Could a name from elsewhere (a team page) be the official one's
    person? Two words at least each, every word of the shorter name in the
    longer one, and the family name the official source most likely wrote
    (its first word, family name first) among them: "Ion Mihai" isn't
    POPESCU ION MIHAI."""
    mine, theirs = set(resolver.fold(official["name"])), set(resolver.fold(other["name"]))
    words = (official["name"] or "").split()
    if len(mine) < 2 or len(theirs) < 2 or not words:
        return False
    family = words[0] if contacts.name_order(official["source"] or "") == "family_first" \
        else words[-1]
    family = set(resolver.fold(family))
    return bool(family) and family <= theirs and (theirs <= mine or mine <= theirs)


def _twins(named: list[dict]) -> dict:
    """Person id -> the other mention of the same person at the company,
    one official and one from elsewhere, only where the match is one to
    one: a site name that fits two registry people (or the reverse)
    confirms neither."""
    officials = [p for p in named if _official(p["source"])]
    others = [p for p in named if not _official(p["source"])]
    pairs = [(o, x) for o in officials for x in others if _same_person(o, x)]
    count: dict = {}
    for o, x in pairs:
        count[o["id"]] = count.get(o["id"], 0) + 1
        count[x["id"]] = count.get(x["id"], 0) + 1
    out = {}
    for o, x in pairs:
        if count[o["id"]] == 1 and count[x["id"]] == 1:
            out[o["id"]], out[x["id"]] = x, o
    return out


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

    if await state.is_overlay():
        key = str(state.db_path)
        kept = _counted.get(key)
        if fresh or kept is None or time.monotonic() - kept[0] > BACKLOG_CACHE_SECONDS:
            kept = (time.monotonic(), await state.read("backlog", FINDER, SITE))
            _counted[key] = kept
        out = dict(kept[1])
        looked = out.pop("looked")
        out["jobs"] = await state.jobs_waiting("find_contacts")
        out["background_search"] = await background_status(state, looked)
        return out
    site_cond, site_args = _site_not_read()
    finder_cond, finder_args = _not_searched(*FINDER, None)
    key = str(state.db_path)
    kept = _counted.get(key)
    async with state._connect() as db:
        if fresh or kept is None or time.monotonic() - kept[0] > BACKLOG_CACHE_SECONDS:
            async with db.execute(
                    f"SELECT COUNT(*), SUM({HAS_SITE} AND {site_cond}), SUM(NOT ({finder_cond})), "
                    f"SUM(NOT {HAS_SITE} AND COALESCE(c.name, '') != '') "
                    f"FROM companies c WHERE {LIVE} AND {NO_CONTACT}",
                    (*site_args, *finder_args)) as cur:
                kept = (time.monotonic(), await cur.fetchone())
            _counted[key] = kept
        total, free, searched_, no_site = kept[1]
        async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(found), 0), COALESCE(SUM(cost), 0) FROM searches "
                "WHERE target = 'company' AND module = ? AND capability = ? "
                "AND searched_at >= datetime('now', '-1 day')", FINDER) as cur:
            day, day_found, day_cost = await cur.fetchone()
        async with db.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE kind = 'find_contacts' "
                "AND status IN ('queued', 'running', 'paused') GROUP BY status") as cur:
            waiting = {s: n for s, n in await cur.fetchall()}
        async with db.execute(
                "SELECT module, COUNT(*), COALESCE(SUM(found), 0) FROM searches "
                "WHERE target = 'company' AND looked_for = 'website' "
                "AND searched_at >= datetime('now', '-1 day') GROUP BY module") as cur:
            looked = {m: (n, f) for m, n, f in await cur.fetchall()}
    return {"without_contact": total or 0, "site_to_read": free or 0,
            "searched": searched_ or 0, "last_day": {"companies": day or 0,
                                                     "found": day_found or 0,
                                                     "cost": round(day_cost or 0.0, 4)},
            "jobs": waiting, "no_website": no_site or 0,
            "background_search": await background_status(state, looked)}


async def background_status(state, looked: dict | None = None) -> dict:
    """The background's website searches inside free allowances: whether
    they are on, its share, and for each search module that is on and set
    up and declares a free allowance, what each period allows, what was
    used (by every run) and what the background may still take; with the
    lookups each made in the last day (``looked``: module -> (searches,
    websites found))."""
    from harvey.modules import lanes, switches

    on, share = await background_settings(state)
    out = []
    for s in searchers(await switches.enabled_ids(state), _env()):
        specs = lanes.free_allowances(s.module)
        if not specs or any(o["module"] == s.module for o in out):
            continue
        n, f = (looked or {}).get(s.module, (0, 0))
        out.append({"module": s.module, "allowance": {a["per"]: a["requests"] for a in specs},
                    "used": await asyncio.to_thread(lanes.uses, s.module),
                    "left": await free_for_background_now(s.module, on, share) or 0,
                    "last_day": {"searches": n, "found": f}})
    return {"on": on, "share": share, "searchers": out}


# A site the finder read before it followed the site's menu: its read
# looked for emails (on the homepage and the first team or contact path
# that answered) but not for the owner (SITE_WANTS).
SHALLOW_READ = ("EXISTS (SELECT 1 FROM searches s WHERE s.target = 'company' "
                "AND s.target_id = c.id AND s.module = 'website' AND s.capability = 'read_site' "
                "AND s.outcome != 'error' AND (',' || s.looked_for || ',') LIKE '%,email,%')")


async def next_free(state, limit: int) -> list[str]:
    """The free work the background takes next, newest first: companies
    with no contact and a website not read for its emails yet; then, once
    those are done, companies whose site the finder read the shallow way
    (SHALLOW_READ), whatever contacts that read found, so each gets the
    deeper read (menu, people, owner, pattern) once. Walks the companies
    with a domain or a website (their indexes), never the whole table: on
    a registry most companies have neither."""
    if await state.is_overlay():
        return await state.read("next_free", int(limit))
    site_cond, site_args = _site_not_read()
    out: list[str] = []
    async with state._connect() as db:
        for extra in (NO_CONTACT, f"NOT {NO_CONTACT} AND {SHALLOW_READ}"):
            if len(out) >= limit:
                break
            rest = f"{LIVE} AND {extra} AND {site_cond}"
            async with db.execute(
                    "SELECT id FROM ("
                    f"SELECT c.id, c.created_at, c.rowid AS r FROM companies c "
                    f"WHERE c.domain > '' AND {rest} "
                    "UNION ALL "
                    f"SELECT c.id, c.created_at, c.rowid FROM companies c "
                    f"WHERE c.website != '' AND COALESCE(c.domain, '') = '' AND {rest}) "
                    "ORDER BY created_at DESC, r DESC LIMIT ?",
                    (*site_args, *site_args, int(limit) - len(out))) as cur:
                out += [r[0] for r in await cur.fetchall()]
    return out


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
    if await state.is_overlay():
        return await state.read("next_paid", int(limit), available)
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
    if await state.is_overlay():
        return await state.read("select", f, None if ids is None else list(ids), force,
                                int(limit))
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
