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
   and says so (a search priced at $0 needs no cap). The results are read
   once, in memory, and never stored. Code drops those that can't be the
   company's own site by their address alone (a directory, a listing, a
   profile of the firm on someone else's site, an institution:
   site_match.screen); a small model (classify's ``results`` task, when
   one is set up) keeps at most three of the rest; those are read, free,
   and nothing is written until one is chosen (see below).
3. A person named at a company with a website, reached by no contact of
   their own, gets one address built from the domain's pattern and checked
   by a verification tier (email_verify), inside the same cap and the
   month's checks (each service it asks is one). Each request is $0 inside
   its service's free allowance and its price past it (entered by the user,
   else declared: harvey/verify_costs.py), reserved from the run's cap
   before it is made and recorded after; past a free allowance with no
   price it is refused (the cap can't count it), and a check that needed
   it is an error, not a check done (the report counts it). An address the
   site publishes as the person's own is linked to them as it is, with no
   check, cap or not.

A website found by searching is the company's only on evidence. Code
decides what a register fact settles (site_match.hard): the firm's CUI or
registration number on the pages (no other firm's beside it), an official
email, an official phone next to its name: confirmed; another firm's CUI as
the site's owner, or several firms' (a listing): not its site. The rest
goes to the classifier (classify's ``site`` task): a yes/no question per
candidate, each asked alone with the firm's register facts and that
candidate's evidence card; exactly one yes is likely, never confirmed;
none links nothing; two or more wait, unsettled. On the site chosen, the ``contacts`` task says which
contacts read there are the firm's own (not a web agency's footer phone,
not an office abroad), and only those are linked. With no model set up
(or held, or its day's calls used), a site no register fact confirms is
kept unlinked, its trust unconfirmed and nothing linked from it, waiting
for ``harvey contacts rejudge`` (recheck below). Below confirmed, the site
says nothing else about the company (no facts, no new name, no people) and
its domain doesn't become the company's key. A website the company declares (its own
key, its registry entry) is read as before.

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
import json
import logging
import re
from dataclasses import dataclass, field


from harvey import classify, contacts, resolver, site_match
from harvey.modules import api, ingest

logger = logging.getLogger("harvey.finder")

FINDER = ("contact_finder", "find_contacts")
LOOKUP = ("contact_finder", "find_website")
# The check of a found website (a search row of its own, its outcome and why).
CHECK = ("contact_finder", "judge_site")
# Candidates read for one lookup, at most.
MAX_CANDIDATES = 3
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

# ── Searches ──

async def company_searched(state, company_id, module: str, capability: str,
                           since: str | None = None, looked_for: str = "") -> bool:
    """Was the company searched by the module's capability (any of its
    records; since the cutoff: a forced run's start, else the capability's
    refresh age; one that looked for ``looked_for`` when given)?"""
    return await state.read("finder_searched", company_id, module, capability,
                            contacts.search_cutoff(module, capability, since), looked_for)


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
        # What each kind of paid step spent: "searches" (website lookups),
        # "checks" (address checks), "lookups" (Hunter's pattern lookups),
        # for the run's report.
        self.by: dict[str, float] = {}
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

    async def record(self, amount: float, what: str = "searches") -> None:
        self.spent += float(amount or 0.0)
        self.by[what] = round(self.by.get(what, 0.0) + float(amount or 0.0), 10)
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
    return await state.read("finder_company", company_id)


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
    return await state.read("finder_on_file", company_id)


async def _people_to_search(state, company_id: str, since: str | None,
                            limit: int = MAX_PEOPLE_PER_COMPANY) -> list[dict]:
    # (Schema 25 keeps the people a register names in the register: the
    # ones the finder works on become Harvey's own, so their searches
    # are recorded.)
    await state.name_register_people(company_id)
    return await state.read("finder_people", company_id, *EMAIL,
                            contacts.search_cutoff(*EMAIL, since), limit)


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


# Why the finder found nothing for a company, as a class (Found.why; never
# a page's or a source's words): its site couldn't be reached, answered
# with a bot challenge or failed to read; it was read and gave nothing;
# the site it holds or a search found waits for a check or the classifier,
# or wasn't taken as the firm's; it has no website and none was found, or
# no search could look (none set up, no money for it, every search failed);
# it was skipped (read already, a contact on file).
WHY = ("unreachable", "blocked", "site_error", "no_contacts", "waiting_check",
       "waiting_classifier", "not_linked", "no_website_found", "no_search", "no_budget",
       "search_error", "already_read", "has_contact", "gone", "nothing_found")


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
    # Requests made to verification services (each a check at that
    # service, what its allowance and bill count), address checks refused
    # (a service past its free tier with no price, a month's cap, the
    # run's money), Hunter's pattern lookups made, what the paid steps
    # spent by kind (Spend.by), and how long the company took.
    checks: int = 0
    checks_refused: int = 0
    lookups: int = 0
    services: dict = field(default_factory=dict)
    spend: dict = field(default_factory=dict)
    ms: int = 0
    # Why it found nothing, as a class (WHY): the run's report counts them.
    why: str = ""

    def as_dict(self) -> dict:
        return {"company_id": self.company_id, "steps": self.steps, "contacts": self.contacts,
                "people": self.people, "website": self.website, "trust": self.trust,
                "cost": round(self.cost, 4), "skipped": self.skipped,
                "spend": {"searches": round(self.spend.get("searches", 0.0), 6),
                          "checks": round(self.spend.get("checks", 0.0), 6),
                          "checks_made": self.checks,
                          "checks_refused": self.checks_refused,
                          "lookups": round(self.spend.get("lookups", 0.0), 6),
                          "lookups_made": self.lookups,
                          "services": {k: n for k, n in self.services.items()
                                       if k != "hunter_search"}},
                "ms": self.ms, "why": None if self.contacts else (self.why or "nothing_found")}


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
    import time

    started = time.monotonic()
    out = await _find(state, company_id, spend=spend, since=since, batch_id=batch_id,
                      config=config, env=env, run_id=run_id, background=background,
                      want_website=want_website, addresses=addresses)
    out.spend = dict(spend.by)
    out.ms = int((time.monotonic() - started) * 1000)
    return out


async def _find(state, company_id: str, *, spend: Spend, since, batch_id, config, env,
                run_id, background, want_website, addresses) -> Found:
    from harvey.modules import switches

    out = Found(company_id)
    company = await company_row(state, company_id)
    if company is None:
        out.skipped.append("merged into another company, or gone")
        out.why = "gone"
        return out
    env = env if env is not None else _env()
    enabled = await switches.enabled_ids(state)
    wanted = await state.collected_signal_codes()
    domain, floor = site_of(company)
    site_done = bool(domain) and await company_searched(state, company_id, *SITE, since,
                                                        looked_for=SITE_WANTS)
    had_contact = await state.read("finder_has_contact", company_id)
    # A website a search found that nothing has checked yet: nothing is read
    # off it until the check (recheck) takes it, and it isn't looked up again.
    waiting = bool(domain) and company.get("website_found") and floor == "unconfirmed"

    if waiting:
        out.skipped.append("the website a search found waits for a check "
                           "(harvey contacts rejudge)")
        out.why = "waiting_check"
    elif domain and not site_done and company.get("website_found"):
        # A website a search found and the evidence took, read again
        # (Search again, a refresh): the same evidence and the same
        # contact choice as when it was found, never everything on it.
        async with _reader(state, wanted, config) as read:
            await _reread_found(state, company, domain, read, out, run_id=run_id,
                                config=config)
        out.website = domain
    elif domain and not site_done:
        on_file = await _on_file(state, company_id)
        verdict = judge(company, on_file, 1, floor=floor) if floor else None
        async with _reader(state, wanted, config) as read:
            done = await read(company, domain=domain, judge=verdict, run_id=run_id)
        out.steps.append("site")
        if done is not None:
            out.contacts += done.contacts
            out.people += done.people
            out.trust = verdict(done.draft) if verdict else "confirmed"
        out.why = ("site_error" if done is None
                   else done.draft.values.get("unread") or "no_contacts")
        out.website = domain
    elif not domain and (since or want_website or not had_contact):
        # (Forced, a company with a phone already is looked up too.)
        found = await _look_up(state, company, out, spend=spend, since=since,
                               batch_id=batch_id, enabled=enabled, env=env,
                               wanted=wanted, config=config, run_id=run_id,
                               background=background)
        if found:
            domain = found
        out.why = out.why or _lookup_why(out)
    else:
        out.why = "already_read" if domain else "has_contact"
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


async def _reread_found(state, company: dict, domain: str, read, out: Found, *,
                        run_id: str = "", config=None) -> None:
    """A found website read again: its card decided as a lookup's is
    (decide), and only the contacts that gives linked, at most at the
    trust the site has. Nothing is linked when the evidence no longer
    holds or the classifier gives no answer (the re-check settles it)."""
    from harvey.collectors.discover import normalize_domain

    facts_raw = await state.read("site_facts", company["id"]) or {}
    facts = site_match.Facts.of(facts_raw)
    website = company.get("website") or f"https://{domain}"
    draft = await read.fetch(company, domain=domain)
    out.steps.append("site")
    card = site_match.card(facts, normalize_domain(website) or domain, draft or api.Draft(),
                           url=website, holders=facts_raw.get("holders"))
    d = await decide(state, facts, [card], config=config)
    have = company.get("website_trust") or "likely"
    if d.index is None:
        # Nothing linked off its pages. A site checked before (its basis)
        # whose contacts only wait for the classifier keeps its trust for
        # the addresses built at it; one nothing checked builds none, until
        # the re-check settles it.
        checked = bool((await state.read("website_basis", company["id"])).get("by"))
        out.trust = have if d.waiting and checked else "unconfirmed"
        out.why = ("site_error" if draft is None else draft.values.get("unread")
                   or ("waiting_classifier" if d.waiting else "not_linked"))
        _skip(out, f"nothing linked from the website read again: {d.note}")
        await state.record_search("company", company["id"], *SITE, looked_for=_contact_read(),
                                  outcome="nothing" if draft is not None else "error",
                                  detail=f"not linked: {d.note}"[:500])
        return
    trust = max(d.trust, have, key=contacts.TRUST_LEVELS.index)
    draft.contacts = chosen_contacts(card, d.selection)
    if trust != "confirmed":
        draft.people = []
    done = await read.keep(company, draft, domain=domain, trust=trust, run_id=run_id)
    out.contacts += done.contacts
    out.people += done.people
    out.trust = trust
    out.why = "no_contacts"


def _reader(state, wanted, config):
    from harvey.collectors.ats import role_keywords
    from harvey.modules.runs import site_reader

    return site_reader(state, wanted, config=config, contacts=True,
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
                   enabled, env, wanted, config, run_id, background: bool = False) -> str:
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
            if await company_searched(state, company_id, s.module, s.capability, since):
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
            await spend.record(cost, "searches")
            out.cost += cost
            if draft.failed is None:
                out.steps.append(f"search:{s.module}")
            else:
                out.failed += 1
                out.skipped.append(f"{s.module} search failed: {draft.failed}")
            if draft.failed is not None:
                await state.record_search(
                    "company", company_id, s.module, s.capability, looked_for="website",
                    outcome="error", cost=cost, batch_id=batch_id, detail=draft.failed)
                if draft.stop:
                    # Out of credit or a refused key: the background leaves
                    # it alone for a while (until when it said, when it did).
                    await asyncio.to_thread(lanes.hold, s.module, draft.values.get("retry_after"))
                    out.skipped.append(f"{s.module}: {draft.stop}")
                continue
            chosen = await choose(state, company, draft.records, wanted=wanted,
                                  config=config, run_id=run_id, source=s.module)
            await state.record_search(
                "company", company_id, s.module, s.capability, looked_for="website",
                outcome="found" if chosen.domain else "nothing",
                found=1 if chosen.domain else 0, cost=cost, batch_id=batch_id,
                detail=chosen.detail)
            if chosen.read:
                out.steps.append("site")
            if chosen.waiting:
                _skip(out, f"the website found waits for the classifier ({chosen.waiting})")
            if chosen.domain:
                out.website, out.trust = chosen.domain, chosen.trust
                out.contacts += chosen.contacts
                out.people += chosen.people
                return chosen.domain if chosen.trust != "unconfirmed" else ""
            if draft.stop:
                out.skipped.append(f"{s.module}: {draft.stop}")
    return ""


# ── Choosing a company's website among a search's results ──


@dataclass
class Decision:
    """What the evidence says of a firm's candidate sites: the one taken
    (``index``) at ``trust`` and why (``basis``), the contacts the
    classifier chose there (``selection``: {"contacts": [card indices]};
    None: every one code read), or ``waiting`` (why the classifier gave no
    answer, or "unsettled": it said yes to several) when it had to."""

    index: int | None = None
    trust: str = ""
    basis: dict = field(default_factory=dict)
    selection: dict | None = None
    waiting: str = ""
    note: str = ""
    # The cards still in question (no rule ruled them out, or every one the
    # classifier said yes to), surest first: what waits for the check.
    pool: list[int] = field(default_factory=list)


# The rule that confirmed a site, as the search row says it.
RULE_WORDS = {"cui": "the firm's CUI on its pages", "ro+cui": "the firm's VAT code on its pages",
              "reg": "the firm's registration number on its pages",
              "email": "an official email on its pages",
              "phone": "an official phone next to the firm's name"}


async def facts_of(state, company_id) -> site_match.Facts:
    found = await state.read("site_facts", company_id)
    return site_match.Facts.of(found or {})


def question(cards: list) -> tuple[int | None, list[int], list]:
    """What code alone makes of ``cards`` (site_match.hard): the one a
    register fact settles (or None), else the cards the classifier is
    asked to choose among (several confirmed ones, else those in
    question), and every card's verdict."""
    verdicts = [site_match.hard(c) for c in cards]
    sure = [i for i, (v, _) in enumerate(verdicts) if v == "confirmed"]
    if len({cards[i].domain for i in sure}) == 1:
        return sure[0], [], verdicts
    open_ = [i for i, (v, _) in enumerate(verdicts) if v == "judge"]
    return None, sure if len(sure) > 1 else open_, verdicts


async def ask_sites(state, facts: site_match.Facts, cards: list, pool: list[int], *, s,
                    config=None) -> tuple[list[tuple[int, bool]], list, str]:
    """Classify's ``site`` task for each card of ``pool``, each one alone
    (a call each): ([(card index, its yes or no)], every Answer, why it
    stopped: "" when every card was answered). It stops at the first card
    with no answer: a choice among some of them would be no choice."""
    said, answers = [], []
    for i in pool:
        ans = await classify.ask(state, "site", {"firm": facts.as_dict(), "cards": [cards[i]],
                                                 "region": facts.region}, s=s, config=config)
        answers.append(ans)
        if ans.value is None:
            return said, answers, ans.why or "no answer"
        said.append((i, bool(ans.value["yes"])))
    return said, answers, ""


async def ask_contacts(state, facts: site_match.Facts, card, *, s, config=None):
    """Classify's ``contacts`` task on the chosen ``card``: its Answer, or
    None when the card has no contacts or the task is off."""
    if not card.contacts or not s.task_on("contacts"):
        return None
    return await classify.ask(state, "contacts", {"firm": facts.as_dict(), "cards": [card],
                                                  "region": facts.region}, s=s, config=config)


def official(card, facts: site_match.Facts) -> list[int]:
    """The card's contacts an official source gives the firm (its register
    phones and emails): what code confirms of a site's contacts by itself."""
    phones, emails = set(facts.phones), {e.lower() for e in facts.emails}
    return [j for j, c in enumerate(card.contacts)
            if (c["kind"] == "phone" and c["value"] in phones)
            or (c["kind"] == "email" and str(c["value"]).lower() in emails)]


async def decide(state, facts: site_match.Facts, cards: list, *, s=None,
                 config=None) -> Decision:
    """Which of ``cards`` (site_match.Card, read already) is the firm's:
    by a register fact (site_match.hard) when one settles it, else by the
    classifier's ``site`` task, each candidate asked alone: exactly one
    yes is the firm's (likely, never confirmed by the model); none links
    nothing; two or more is an unsettled question, and the site waits.
    Then the ``contacts`` task says which contacts read on the chosen site
    are the firm's own."""
    import time

    settled, pool, verdicts = question(cards)
    ruled = "; ".join(f"{cards[i].domain}: {r}" for i, (v, r) in enumerate(verdicts)
                      if v == "rejected")
    s = s or await classify.settings(state)
    if settled is not None:
        i = settled
        rule = verdicts[i][1]
        d = Decision(i, "confirmed", {"by": rule, "at": int(time.time())},
                     note=f"{cards[i].domain}: {RULE_WORDS.get(rule, rule)}")
        await _choose_contacts(state, facts, cards[i], d, s=s, config=config)
        return d
    if not pool:
        return Decision(note=f"no candidate is the firm's ({ruled or 'none read'})")
    said, answers, why = await ask_sites(state, facts, cards, pool, s=s, config=config)
    if why:
        return Decision(waiting=why, pool=list(pool), note=f"waits for the classifier ({why})")
    model = next((a.model for a in reversed(answers) if a.model), "")
    yes = [i for i, y in said if y]
    basis = {"by": "judge", "model": model, "at": int(time.time())}
    if not yes:
        return Decision(basis=basis, note="the classifier: none is the firm's")
    if len(yes) > 1:
        # Two sites both the firm's, by the model: a question it can't
        # settle. Nothing linked; the site waits, with those still in it
        # (the re-check doesn't ask the same again: site_check.UNSETTLED).
        return Decision(waiting="unsettled", pool=yes, basis=basis,
                        note=f"waits: the classifier says yes to {len(yes)} sites "
                             f"({', '.join(cards[i].domain for i in yes)})")
    i = yes[0]
    p = (answers[pool.index(i)].value or {}).get("p")
    if p is not None:
        # (A System One classifier's probability, shown on the company panel.)
        basis["p"] = p
    trust = "likely"
    if verdicts[i][0] == "confirmed":
        trust = "confirmed"
        basis["by"] = verdicts[i][1]
    d = Decision(i, trust, basis, note=f"{cards[i].domain}: the classifier said yes")
    await _choose_contacts(state, facts, cards[i], d, s=s, config=config)
    return d


async def _choose_contacts(state, facts, card, d: Decision, *, s, config=None) -> None:
    """``d.selection``: the contacts the ``contacts`` task says are the
    firm's own, and always those an official source gives the firm. On a
    site a register fact confirmed, the task only takes off what it says
    no to: a contact it wasn't shown or didn't answer stays, as code read
    it. With no answer from it (switched off, held, the day's calls used,
    no valid answer), nothing is linked beyond what code confirms: on a
    site a register fact confirmed, every contact code read there
    (selection None); on one the model chose, only the official ones."""
    ans = await ask_contacts(state, facts, card, s=s, config=config)
    if ans is not None and ans.value is not None:
        own = set(official(card, facts))
        if d.basis.get("by") == "judge":
            pick = set(ans.value["contacts"]) | own
        else:
            no = set(ans.value.get("no") or ())
            pick = {j for j in range(len(card.contacts)) if j not in no} | own
        d.selection = {"contacts": sorted(pick)}
        d.basis["model"] = ans.model or d.basis.get("model", "")
    elif d.basis.get("by") == "judge" and card.contacts:
        d.selection = {"contacts": official(card, facts)}


def chosen_contacts(card, selection: dict | None) -> list:
    """The contacts to link from a chosen site: those the classifier chose,
    or with no selection every one code read, placeholders left out
    (site_match.card)."""
    if selection is None:
        return list(card.picks)
    return [card.picks[i] for i in selection.get("contacts") or [] if 0 <= i < len(card.picks)]


@dataclass
class Chosen:
    """What one lookup's results gave: the website taken (``domain``, at
    ``trust``; "unconfirmed" when it waits for a check), what was linked,
    and the search row's detail (counts, never the results themselves)."""

    domain: str = ""
    trust: str = ""
    contacts: int = 0
    people: int = 0
    read: int = 0
    waiting: str = ""
    detail: str = ""


def _results(n: int, reasons: dict) -> str:
    """"10 results: 6 directory, 2 profile": what a search gave, as counts."""
    counts = ", ".join(f"{k} {why}" for why, k in sorted(reasons.items(), key=lambda x: -x[1]))
    return f"{n} result{'' if n == 1 else 's'}" + (f": {counts}" if counts else "")


async def choose(state, company: dict, records, *, wanted=frozenset(), config=None,
                 run_id: str = "", source: str = "") -> Chosen:
    """The company's own site among a search's ``records`` (in memory
    only), chosen and written (site_match.screen, classify's ``results``,
    reads of at most MAX_CANDIDATES, decide). The search row keeps counts
    and the domains Harvey read, never the other results."""
    out = Chosen()
    raw = await state.read("site_facts", company["id"]) or {}
    facts, holders = site_match.Facts.of(raw), raw.get("holders")
    reasons: dict[str, int] = {}
    survivors, seen = [], set()
    for r in records:
        domain = (r.domain or "").lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        why = site_match.screen(r.website or f"https://{domain}", domain, facts)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
        else:
            survivors.append(r)
    head = _results(len(records), reasons)
    if not survivors:
        out.detail = f"{head}; none could be the firm's site"
        return out
    s = await classify.settings(state)
    ranked = sorted(survivors, key=lambda r: -name_score(company.get("name") or "", r))
    picked = ranked[:MAX_CANDIDATES]
    if len(survivors) >= 2:
        ans = await classify.ask(state, "results", {"firm": facts.as_dict(),
                                                     "results": survivors},
                                 s=s, config=config)
        if ans.value is not None:
            picked = [survivors[i] for i in ans.value["keep"]][:MAX_CANDIDATES]
            dropped = len(survivors) - len(picked)
            if dropped:
                reasons["left out by the classifier"] = dropped
            head = _results(len(records), reasons)
            if not picked:
                out.detail = f"{head}; none could be the firm's site"
                return out
    async with _reader(state, wanted, config) as read:
        drafts, cards = [], []
        for r in picked:
            domain = r.domain.lower()
            draft = await read.fetch(company, domain=domain)
            drafts.append(draft or api.Draft())
            cards.append(site_match.card(facts, domain, draft or api.Draft(),
                                         url=r.website or f"https://{domain}", holders=holders))
        out.read = len(cards)
        names = ", ".join(c.domain for c in cards)
        d = await decide(state, facts, cards, s=s, config=config)
        out.detail = f"{head}; read: {names}; {d.note}"
        if d.index is None and not d.waiting:
            return out
        # (Waiting: the first candidate no rule ruled out, never one that
        # didn't answer or is another firm's.)
        i = d.index if d.index is not None else d.pool[0]
        card, draft, record = cards[i], drafts[i], picked[i]
        website = record.website or f"https://{card.domain}"
        if d.waiting:
            # Kept with nothing linked, for the check to take up later with
            # every candidate still in question (the read's detail names
            # them); the read is recorded so the backlog doesn't read it
            # again meanwhile.
            await state.set_found_website(company["id"], website, trust="unconfirmed",
                                          source=source)
            await state.record_search(
                "company", company["id"], *SITE, looked_for=_contact_read(), outcome="nothing",
                detail=site_match.waiting_detail([cards[j].domain for j in d.pool]))
            out.domain, out.trust, out.waiting = card.domain, "unconfirmed", d.waiting
            return out
        await state.set_found_website(company["id"], website,
                                      domain=card.domain if d.trust == "confirmed" else "",
                                      trust=d.trust, source=source, basis=d.basis)
        draft.contacts = chosen_contacts(card, d.selection)
        if d.trust != "confirmed":
            draft.people = []
        done = await read.keep(company, draft, domain=card.domain, trust=d.trust, run_id=run_id)
        out.domain, out.trust = card.domain, d.trust
        out.contacts, out.people = done.contacts, done.people
    return out


def _contact_read() -> str:
    from harvey.modules.runs import CONTACT_READ

    return CONTACT_READ


def _lookup_why(out: Found) -> str:
    """Why a lookup found the company no site, as a class (WHY)."""
    if out.website:
        return "no_contacts"
    said = " ".join(out.skipped)
    if "waits for the classifier" in said:
        return "waiting_classifier"
    if "no search module" in said or "no name to search by" in said:
        return "no_search"
    if "website search skipped" in said or "no free searches left" in said:
        return "no_budget"
    if out.failed and not any(s.startswith("search:") for s in out.steps):
        return "search_error"
    return "no_website_found"


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
    from harvey import verify_costs
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
    meter = await verify_costs.Meter.start(state, spend)
    out.services = meter.made
    configured = ef.configured_verifiers(env, enabled)
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
        draft, tried, same_as, paid, no_check = None, [], "", 0.0, False
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
            # Each service a check asks is one of the month's checks
            # (EMAIL_VERIFY_MONTHLY_CHECKS), priced as it is asked and,
            # past its free allowance, refused with no price (the Meter).
            # None left to ask: the check isn't made. The other order is
            # checked only when the likelier one comes back invalid.
            why = await _no_check(meter, configured)
            if why:
                _skip(out, f"address check skipped: {why}")
                if draft is None:
                    # (An error isn't a search done: searched again later.
                    # The people after this one are still gone through:
                    # one whose own address the site publishes costs nothing.)
                    out.checks_refused += 1
                    await state.record_search(
                        "person", person["id"], *EMAIL, looked_for="email", outcome="error",
                        batch_id=batch_id, origin=company_id, detail=why)
                    no_check = True
                break
            mark, spent, made = meter.mark(), meter.spent(), meter.total()[0]
            looked = meter.total("search")[0]
            ctx = api.Context(env=env, enabled=frozenset(enabled), budget=meter)
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
            # What it cost: each request $0 inside its service's free
            # allowance, its price past it (recorded by the Meter as made).
            usd = round(meter.spent() - spent, 6)
            out.checks += meter.total()[0] - made
            out.lookups += meter.total("search")[0] - looked
            out.cost += usd
            paid += usd
            refused = meter.refused[mark:]
            for service, why in refused:
                if meter.specs.get(service, {}).get("kind") == "search":
                    _skip(out, f"pattern lookup skipped: {why}")
            stopped = "; ".join(dict.fromkeys(
                why for service, why in refused
                if meter.specs.get(service, {}).get("kind") != "search"))
            if stopped and draft.failed is None and not any(
                    c.status and c.status != "guess" for c in draft.contacts):
                # A service the check would have asked next was refused:
                # it isn't a check done, and nothing it guessed is kept.
                _skip(out, f"address check stopped: {stopped}")
                out.checks_refused += 1
                draft = api.Draft(failed=stopped, values=draft.values)
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
        if no_check:
            continue
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
            found=kept, cost=paid, batch_id=batch_id,
            detail="; ".join(x for x in (draft.failed or "", order) if x), origin=company_id)


async def _no_check(meter, configured: list[str]) -> str:
    """Why no address check can be made now, or "": the month's checks
    are used, or every service it may ask would be refused."""
    from harvey import verify_costs

    if await verify_costs.checks_left(meter.state, meter.specs) <= 0:
        return "this month's address checks are used (EMAIL_VERIFY_MONTHLY_CHECKS)"
    whys = [await meter.refusal(v) for v in configured]
    return whys[0] if whys and all(whys) else ""


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
    return await state.read("finder_linked_emails", company_id)


async def _emails_at(state, company_id: str, domain: str) -> list[str]:
    """The addresses a source published for a company at ``domain`` (its
    site, a registry, a listing). An address built here (an address check's
    find_email) is no evidence of how the company spells its addresses,
    nor is one a check found invalid or that stays a guess: learning from
    those would pass a guess off as published, or teach the next check a
    pattern nobody confirmed."""
    return await state.read("finder_emails_at", company_id, domain, EMAIL)


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
    if await state.read("finder_sources", company["id"]) & set(LOCAL_BUSINESS_SOURCES):
        return True
    employees = await state.read("finder_employees", company["id"])
    return employees is not None and employees <= SMALL_COMPANY_MAX


async def _named_at(state, company_id: str) -> list[dict]:
    """Everyone named at a company (to confirm a name's order by)."""
    return [{k: p[k] for k in ("id", "name", "source")}
            for p in await state.read("finder_named", company_id)]


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


async def next_free(state, limit: int) -> list[str]:
    """The free work the background takes next, newest first: companies
    with no contact and a website not read for its emails yet; then, once
    those are done, companies whose site the finder read the shallow way
    (SHALLOW_READ), whatever contacts that read found, so each gets the
    deeper read (menu, people, owner, pattern) once. Walks the companies
    with a domain or a website (their indexes), never the whole table: on
    a registry most companies have neither."""
    return await state.read("next_free", int(limit))


# Where the background's walks over the companies with no website got to:
# one for each set of searches it was given (which searches have some of
# their free allowance left changes by the minute), the newest first.
PAID_WALK_KEY = "finder_paid_walk"
PAID_WALKS = 8


async def next_paid(state, limit: int, available: list[Searcher] | None = None) -> list[str]:
    """Companies with no contact and no website that at least one of the
    search modules able to find them (``available``; every one on and set
    up when None) hasn't searched yet, newest first: the background's paid
    work, when a day's budget is set. A module added later brings back
    the companies the others found nothing for."""
    from harvey.overlay.reads import paid_walk_searchers

    if available is None:
        from harvey.modules import switches

        available = searchers(await switches.enabled_ids(state), _env())
    if not available:
        return []
    # (Where the walk got to: overlay.reads.Reader.next_paid.)
    kept = await state.get_setting(PAID_WALK_KEY)
    try:
        walks = json.loads(kept or "[]")
    except (TypeError, ValueError):
        walks = []
    walks = [w for w in walks if isinstance(w, dict)] if isinstance(walks, list) else []
    sig = paid_walk_searchers(available)
    walk = next((w for w in walks if w.get("searchers") == sig), None)
    ids, after = await state.read("next_paid", int(limit), available, walk)
    # (Forgotten meanwhile, Store.forget_paid_walk: the stretch read from it
    # may have passed a firm searchable again, so the next walk starts over.)
    if after != walk and await state.get_setting(PAID_WALK_KEY) == kept:
        rest = [w for w in walks if w.get("searchers") != after.get("searchers")]
        await state.set_setting(PAID_WALK_KEY, json.dumps([after, *rest][:PAID_WALKS]))
    return ids


# ── A selection (the Contacts and Companies pages' Find contacts) ──

async def select(state, f: contacts.ContactFilter | None = None, *, ids=None,
                 force: bool = False, limit: int = 1000,
                 attach_figures: bool = False) -> list[dict]:
    """The companies a Find-contacts run takes: those matching the filter's
    company conditions (a value not known yet is searched, like a
    "possible" in state.cohort_verdicts) and with no contact (every match with
    ``force``), with whether each has a website, up to ``limit``. Given
    ``ids``, only those, in pieces of IDS_PER_QUERY."""
    f = f or contacts.ContactFilter()
    return await state.read("select", f, None if ids is None else list(ids), force,
                            int(limit))


async def plan(state, selected: list[dict], enabled, env: dict, *,
               force: bool = False) -> list[dict]:
    """``selected`` with what a run over it will do to each company, as
    _find decides it (a forced run counts nothing as searched; otherwise a
    search counts while younger than its capability's refresh_days):
    ``reread``, a website a search found and the evidence took, read again
    (its classifier questions asked again) only when not read since;
    ``look_with``, the searches a company with no website still goes
    through (those that haven't searched it), cheapest first. The
    estimates read both."""
    found = searchers(enabled, env)
    since = contacts.now() if force else None
    again = [c["id"] for c in selected
             if c["site"] and c.get("website_found") and not c.get("website_waiting")]
    no_site = [c["id"] for c in selected if not c["site"]]
    read = set()
    if again and not force:
        read = await state.read("finder_searched_many", again, *SITE,
                                contacts.search_cutoff(*SITE), SITE_WANTS)
    looked = {}
    for x in found:
        looked[(x.module, x.capability)] = set() if force or not no_site else await state.read(
            "finder_searched_many", no_site, x.module, x.capability,
            contacts.search_cutoff(x.module, x.capability, since))
    again_set = set(again)
    return [{**c, "reread": c["id"] in again_set and c["id"] not in read,
             "look_with": [] if c["site"] else
             [x for x in found if c["id"] not in looked[(x.module, x.capability)]]}
            for c in selected]


def estimate(selected: list[dict], enabled, env: dict) -> dict:
    """What a Find-contacts run over ``selected`` would cost: a website
    read is free; a company with no website costs one lookup through the
    cheapest search that can find it and hasn't searched it (``plan``'s
    ``look_with``; every search when the rows weren't planned), more when
    that one finds nothing and the next is tried (the cap stops it)."""
    found = searchers(enabled, env)

    def through(c):
        return c["look_with"] if "look_with" in c else found

    paid = [through(c) for c in selected if not c["site"] and through(c)]
    return {"companies": len(selected), "with_site": sum(1 for c in selected if c["site"]),
            "to_look_up": len(paid),
            "estimated_cost": round(sum(ways[0].estimate for ways in paid), 4),
            "most": round(sum(x.estimate for ways in paid for x in ways), 4),
            "searchers": [{"module": s.module, "capability": s.capability,
                           "provider": s.provider, "cost": s.estimate} for s in found]}


async def checks_estimate(state, selected: list[dict], enabled, env: dict) -> dict:
    """The address checks a Find-contacts run over ``selected`` may make
    (only with a spend cap: MAX_PEOPLE_PER_COMPANY a company at most), each
    asking every verification service at worst, and Hunter's pattern
    lookups (one a company at most), with what they may cost past each
    service's free allowance this month at its price (entered, else
    declared: verify_costs.projection, ``usd_high``). ``refused``: the
    services past their allowance with no price, whose requests the run
    will refuse; ``checks_left``: the month's checks, every service's
    together. A planned company (``plan``) that can't reach a domain the
    run would build addresses at (a found site waiting for its check, no
    website and no search left to find one) is left out. An estimate: the
    run's cap is what is enforced, request by request."""
    from harvey import verify_costs
    from harvey.integrations import email_finder as ef

    def reaches(c) -> bool:
        if c["site"]:
            return not c.get("website_waiting")
        return "look_with" not in c or bool(c["look_with"])

    on = EMAIL[0] in enabled
    configured = ef.configured_verifiers(env, enabled) if on else []
    firms = sum(1 for c in selected if reaches(c)) if on else 0
    checks = firms * MAX_PEOPLE_PER_COMPANY
    wants = {v: checks for v in configured}
    lookups = firms if on and ef._module_on(enabled, "hunter") \
        and ef._hunter_api_key(env) else 0
    if lookups:
        wants[verify_costs.HUNTER_SEARCH] = lookups
    out = await verify_costs.projection(state, wants)
    return {**out, "checks": checks, "lookups": lookups, "verifiers": configured,
            "per_company": MAX_PEOPLE_PER_COMPANY}


async def classify_estimate(state, selected: list[dict]) -> dict:
    """What a Find-contacts run over ``selected`` may ask the classifier
    and what that may cost, before anything is sent: a company looked up
    is a results question, one per candidate read (MAX_CANDIDATES at most)
    and one about the chosen site's contacts; a website a search found
    and read again (``plan``'s ``reread``: not waiting for the re-check,
    and not read since, unless forced) a site question and a contacts one;
    a website the company declares needs none. The cost is a range per task (classify.projection: typical and
    high, at its profile's price), never a limit: the run's classifier cap
    is what is enforced, call by call (``note``). ``price_known`` False: a
    profile has no price, so the USD figures leave its calls out."""
    per = {"results": 0, "site": 0, "contacts": 0}
    for c in selected:
        if not c["site"]:
            if "look_with" in c and not c["look_with"]:
                continue            # (every search that can find it already did)
            per["results"] += 1
            per["site"] += MAX_CANDIDATES
            per["contacts"] += 1
        elif c.get("reread", c.get("website_found") and not c.get("website_waiting")):
            per["site"] += 1
            per["contacts"] += 1
    seen = await classify.sizes(state)
    out = {"calls": 0, "usd_typical": 0.0, "usd_high": 0.0, "price_known": True,
           "per_task": {}, "note": classify.ESTIMATE_NOTE}
    for task, n in per.items():
        s = (await classify.settings(state, task)).for_task(task)
        if not n or not s.task_on(task) or s.provider == "off":
            out["per_task"][task] = {"calls": 0, "usd_typical": 0.0, "usd_high": 0.0,
                                     "profile": s.profile}
            continue
        p = await classify.projection(state, s, task, n, seen)
        out["per_task"][task] = p
        out["calls"] += n
        out["usd_typical"] = round(out["usd_typical"] + p["usd_typical"], 6)
        out["usd_high"] = round(out["usd_high"] + p["usd_high"], 6)
        out["price_known"] = out["price_known"] and p["price_known"]
    return out
