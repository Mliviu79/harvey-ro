"""The one writer of what modules find.

A capability returns a Draft (harvey/modules/api.py) and writes nothing.
Here is where a draft becomes rows, under the rules no module can skip:

- a record is matched by the resolver (harvey/resolver.py) before it is
  saved, so a company found twice is one company, a likely one goes to the
  review list, and a known one only has its gaps filled;
- a fact is written only when its signal is confirmed and its module is on
  (the confirmation gate), and a phone number only when it parses;
- people and contacts accumulate: a second source adds beside the first,
  never over it;
- every observation says which module and capability found it and which
  record it was found for before any merge (``origin_company_id``), which
  is what lets undoing an automatic merge take back exactly its facts.

``store`` is a StateManager, or a CompanyBatch for many records on one
connection (a registry pull). Run bookkeeping (the runs log, a company's
"profiled" mark) stays with the run that does it (harvey/modules/runs.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from datetime import datetime, timezone

from harvey import resolver
from harvey.models import Company
from harvey.modules.api import Budget, Fact, Record

logger = logging.getLogger("harvey.ingest")

@dataclass
class Saved:
    """Where a record landed: ``company_id`` to write its facts to,
    ``origin`` the record it is before any merge, ``created`` whether it is
    new. For a source record also whether a known one changed, and whether
    its declared domain was left off because another company holds it."""

    company_id: str = ""
    origin: str = ""
    created: bool = False
    updated: bool = False
    shared_domain: bool = False
    activities: int = 0


def company_from(record: Record) -> Company:
    """The company row a record describes. Every legal form keeps its
    registered office: a sole trader's or family business's address and
    phone are public registry data like any company's."""
    return Company(
        name=record.name, domain=record.domain, website=record.website,
        phone=record.phone, industry=record.industry, location=record.location,
        address=record.address, description=record.description,
        company_size=record.company_size, cui=record.ids.get("cui", ""),
        external_id=record.external_id,
        source=record.source, source_url=record.source_url,
    )


def excluded(record: Record, do_not_contact) -> bool:
    """Is the record a company the operator has forbidden (do-not-contact)?
    Checked before it is saved, so it never becomes a row."""
    from harvey.gate import do_not_contact_domain_match

    return bool(record.domain) and do_not_contact_domain_match(record.domain, do_not_contact or [])


async def listing(store, record: Record) -> Saved:
    """A company a search found: matched by its listing id, domain, phone
    and name in its town; a known one gets the gaps in its phone, website
    and domain filled, nothing it holds replaced. A booking platform, link
    shortener or directory given as its website is many businesses'
    domain, not this one's: the website is kept, the domain isn't."""
    domain = record.domain if not record.domain or resolver.usable_domain(record.domain) else ""
    company = company_from(record)
    company.domain = domain
    result = await store.save_company(company, extra_ids=tuple(x for x in record.alt_ids if x))
    if not result.created and not result.merged_from:
        # (An automatic merge has filled the gaps already, and logged them
        # so an undo can take them back.)
        await store.fill_company_fields(
            result.company_id, phone=record.phone, website=record.website, domain=domain)
    await place_of(store, result.company_id, record, result.origin,
                   sure=result.created or result.match.same_record)
    return Saved(result.company_id, result.origin, result.created)


async def place_of(store, company_id: str, record: Record, origin: str = "",
                   sure: bool = True) -> int:
    """A listing as one of its company's places, and as one of the sources
    it was found in: a branch that matched a company on file keeps its own
    address and phone there, and its phone is a contact of the company
    through that place (``sure`` False: matched by other evidence than its
    own record, so the phone is only likely the company's). Returns the
    place's id (0 when the listing can't tell one place from another)."""
    from harvey import contacts
    from harvey.collectors.phones import phone_region

    source = record.module or record.source
    await store.add_company_source(company_id, record.source or source, record.external_id,
                                   origin)
    location_id = await store.add_location(
        company_id, name=record.name, address=record.address, location=record.location,
        external_id=record.external_id, phone=record.phone, source=source, origin=origin)
    if record.phone:
        await store.add_contacts(company_id, [contacts.Found(
            "phone", record.phone, source=source, capability=record.capability,
            evidence_url=record.source_url, trust="confirmed" if sure else "likely",
            location_id=location_id,
            region=phone_region(record.location, None, record.domain))], origin)
    return location_id


async def source_record(batch, record: Record, *, collector: str,
                        dry_run: bool = False) -> Saved:
    """A company an official source (a trade registry) describes, on a
    CompanyBatch. Matched by legal identity (CUI, registration number) and
    name in its town; a domain it declares is self-declared, so it is a
    possible match for review, never a reason on its own to hang this
    record on another business, and a domain another company holds is left
    off. A known one has its gaps filled, except the registered office,
    which the source is the authority on and always refreshes. Its
    activities are saved in full. ``dry_run`` matches and writes nothing."""
    company = company_from(record)
    match = await batch.match_company(company, declared_domain=record.declared_domain)
    existing = match.company_id if match.attach else None
    out = Saved(created=existing is None)
    domain = company.domain
    holder = await batch.get_company_by_domain(domain) if domain else None
    if holder and (existing is None or holder.id != existing):
        out.shared_domain = True
        domain = company.domain = ""
    elif holder and not match.same_record:
        # Joining the holder by a merge: the merge brings the domain.
        company.domain = ""
    if dry_run:
        return out

    if existing and match.same_record:
        out.company_id = existing
        out.origin = match.origin or existing
        changed = await batch.fill_company_fields(
            existing, cui=company.cui, name=company.name, website=company.website,
            domain=domain, industry=company.industry, location=company.location,
            address=company.address)
        await batch.register_ids(existing, company, match)
        out.updated = bool(changed)
    else:
        # New, or the same company by other evidence (saved, then merged
        # into it, which can be undone), or a possible one for review.
        result = await batch.save_company(company, match)
        out.company_id, out.origin = result.company_id, result.origin
    # What the company does, all of it: describes the record (like its
    # industry), so it isn't held behind the signal gate.
    out.activities = await batch.set_company_activities(
        out.company_id, record.activities, source=collector)
    await batch.add_company_source(out.company_id, record.source, record.external_id, out.origin)
    return out


def valid_phone(value: str) -> bool:
    """A phone fact's value: a valid number in E.164."""
    from harvey.collectors.phones import phone_hit

    return bool(value) and value.startswith("+") and phone_hit(value) is not None


# (module, capability, code) already reported as undeclared: said once.
_undeclared_seen: set[tuple[str, str, str]] = set()


_declared_cache: list = [None, {}]


def _declared_facts() -> dict[tuple[str, str], frozenset[str]]:
    """The facts each capability declares in its manifest's ``outputs``
    (worked out once per set of manifests: a registry pull asks per firm)."""
    from harvey.modules import api, switches

    mans = switches.manifests()
    if _declared_cache[0] is not mans:
        _declared_cache[:] = [mans, {
            (m.id, cap.get("id")): frozenset((cap.get("outputs") or {}).get("facts") or ())
            for m, cap in api.declared({m.id: m for m in mans})}]
    return _declared_cache[1]


def rows(facts: list[Fact], *, company_id: str = "", origin: str = "",
         records: dict[str, Saved] | None = None, run_id: str = "",
         confirmed: set[str] | None = None, collector: str = "") -> list[dict]:
    """Observation rows for ``facts``: a fact about the record called for
    goes to ``company_id`` (found for ``origin``), one about a record of
    the draft to where that record was saved (``records``, by key). Facts
    not confirmed (``confirmed``; None checks nothing here), about a record
    that wasn't saved, or a phone that doesn't parse are left out, and so
    is one the capability stamped on it doesn't declare in its manifest
    (a module's bug, or a module naming another as the finder: logged)."""
    out = []
    declared = _declared_facts() if any(f.module for f in facts) else {}
    for fact in facts:
        if confirmed is not None and fact.code not in confirmed:
            continue
        if fact.module and fact.code not in declared.get((fact.module, fact.capability), ()):
            seen = (fact.module, fact.capability, fact.code)
            if seen not in _undeclared_seen:
                _undeclared_seen.add(seen)
                logger.warning("ingest: %s.%s returned %s, which its manifest doesn't "
                               "declare: left out", *seen)
            continue
        if fact.record:
            saved = (records or {}).get(fact.record)
            if saved is None or not saved.company_id:
                continue
            target, found_for = saved.company_id, saved.origin or saved.company_id
        else:
            target, found_for = company_id, origin or company_id
        if fact.code == "PHONE_NUMBER" and not valid_phone(fact.value_text):
            logger.debug("ingest: %s left out, not a valid number", fact.value_text)
            continue
        row = {
            "company_id": target, "signal_code": fact.code,
            "collector": fact.collector or collector, "value_num": fact.value_num,
            "value_text": fact.value_text, "confidence": fact.confidence,
            "evidence_url": fact.source_url, "run_id": run_id,
            "module": fact.module, "capability": fact.capability,
            "origin_company_id": found_for,
        }
        if fact.observed_at:
            row["observed_at"] = fact.observed_at
        out.append(row)
    return out


async def write(store, batch: list[dict], run_id: str = "") -> int:
    """Write observation rows (from ``rows``) in one go."""
    if not batch:
        return 0
    return await store.add_observations(batch, run_id=run_id)


async def facts(store, found: list[Fact], *, company_id: str, origin: str = "",
                run_id: str = "", confirmed: set[str] | None = None,
                collector: str = "") -> int:
    """Facts about one company, through the gate: ``confirmed`` None reads
    what is confirmed now."""
    if confirmed is None:
        confirmed = await store.confirmed_signal_codes()
    return await write(store, rows(found, company_id=company_id, origin=origin,
                                   run_id=run_id, confirmed=confirmed or set(),
                                   collector=collector), run_id)


async def unseen(batch, company_id: str, evidence_url: str, found: list[dict]) -> list[dict]:
    """The rows a source hasn't given before: one dataset's reading is
    written once, a newer dataset's beside it."""
    if not found:
        return []
    seen = await batch.observed(company_id, evidence_url)
    return [r for r in found if (r["signal_code"], r["value_text"]) not in seen]


def many_codes() -> frozenset[str]:
    """Signals a company can hold several values of at once (phones,
    addresses): a new one is added beside the others."""
    from harvey.modules import switches

    return frozenset(sig["code"] for m in switches.manifests() for sig in m.signals
                     if sig.get("cardinality") == "many")


async def changed(batch, company_id: str, module: str, found: list[dict],
                  many: frozenset[str] | None = None) -> list[dict]:
    """The rows of a repeated check that say something new about the
    company: for a signal with many values, one ``module`` hasn't recorded
    before; for a single-valued one, a value other than the module's
    latest reading (so a status that flips back is recorded again, and an
    unchanged one isn't written twice)."""
    if not found:
        return []
    many = many_codes() if many is None else many
    latest, ever = await batch.module_readings(company_id, module)
    return _new_readings(found, latest, ever, many)


def _new_readings(found: list[dict], latest: dict, ever: set, many: frozenset[str]) -> list[dict]:
    """The rows of ``found`` that ``changed`` keeps, against the module's
    readings so far (``latest``, ``ever``: updated as rows are kept)."""
    out = []
    for row in found:
        code, text = row["signal_code"], row.get("value_text") or ""
        value = (row.get("value_num"), text)
        if code in many:
            if (code, text) in ever:
                continue
            ever.add((code, text))
        else:
            if latest.get(code) == value:
                continue
            latest[code] = value
        out.append(row)
    return out


async def checked(batch, module: str, found: dict[str, tuple[list[Fact], list]], *,
                  run_id: str, confirmed, many: frozenset[str] | None = None) -> dict:
    """What a lookup by identifier answered for many companies at once
    (``found``: {company_id: (facts, contacts)}), saved on one batch the
    way ``changed``, ``write`` and ``company_phones`` save one company's,
    with a statement for the lot instead of several each: each fact through
    the confirmation gate, written only when it says something new; every
    number a contact of its company (with its source and trust), and the
    first one the company's phone when it has none. Returns
    {"observations", "updated", "phones_set"}: rows written, companies with
    a new reading, phones set."""
    many = many_codes() if many is None else many
    rows_of = {cid: _one_each(rows(facts, run_id=run_id, confirmed=confirmed,
                                   records={cid: Saved(cid, cid)}), many)
               for cid, (facts, _) in found.items()}
    readings = await batch.module_readings_of([cid for cid, r in rows_of.items() if r], module)
    fresh: list[dict] = []
    updated = 0
    for cid, found_rows in rows_of.items():
        if found_rows:
            new = _new_readings(found_rows, *readings[cid], many)
            fresh += new
            updated += bool(new)
    written = await write(batch, fresh, run_id)
    items, phones = [], {}
    for cid, (_, contacts_) in found.items():
        items += _contact_items(cid, contacts_)
        first = next((c for c in contacts_ or () if c.kind == "phone" and (c.display or c.value)),
                     None)
        if first is not None:
            phones[cid] = first.display or first.value
    if items:
        await batch.add_contact_list(items)
    set_ = await batch.fill_company_phones(phones) if phones else []
    return {"observations": written, "updated": updated, "phones_set": len(set_)}


def _one_each(found: list[dict], many: frozenset[str]) -> list[dict]:
    """A company's rows with one reading of each single-valued signal, the
    first (its identifiers come in order, so always the same one): a
    company known by two identifiers that answer differently would
    otherwise write both on every check, each one "new" against the other."""
    seen: set[str] = set()
    out = []
    for row in found:
        code = row["signal_code"]
        if code not in many:
            if code in seen:
                continue
            seen.add(code)
        out.append(row)
    return out


async def company_phones(batch, company_id: str, contacts) -> bool:
    """An official record's phone as the company's phone when it has none
    (on a batch); every number is a contact of the company, and is also
    kept as a reading when PHONE_NUMBER is confirmed. True when the
    company's phone was set."""
    await add_contacts(batch, company_id, contacts)
    phones = [c for c in contacts if c.kind == "phone" and (c.display or c.value)]
    if not phones:
        return False
    changed_ = await batch.fill_company_fields(company_id, phone=phones[0].display or phones[0].value)
    return "phone" in (changed_ or [])


async def add_contacts(store, company_id: str, found, origin: str = "",
                       trust: str = "", region: str | None = None) -> int:
    """Every contact a capability found for a company (api.Contact), linked
    to it with its source and evidence. Contact data, like a published
    email: it doesn't wait for a signal to be confirmed. ``trust`` is the
    most a contact can be trusted (found on a website whose match to the
    company is only likely, or unconfirmed): never thrown away, only marked.
    How many were kept (a number that doesn't parse isn't)."""
    items = [f for _, f, _ in _contact_items(company_id, found, origin, trust, region)]
    if not items or not company_id:
        return 0
    return await store.add_contacts(company_id, items, origin)


def _contact_items(company_id: str, found, origin: str = "", trust: str = "",
                   region: str | None = None) -> list[tuple]:
    """(company_id, contacts.Found, origin) for each contact a capability
    found (api.Contact), trusted at most ``trust`` (see add_contacts)."""
    from harvey import contacts

    def capped(c) -> str:
        own = contacts.trust_for(c.confidence)
        if trust not in contacts.TRUST_LEVELS:
            return own
        return max(own, trust, key=contacts.TRUST_LEVELS.index)

    return [(company_id, contacts.Found(c.kind, c.value, display=c.display, status=c.status,
                                        source=c.module, capability=c.capability,
                                        evidence_url=c.source_url, confidence=c.confidence,
                                        trust=capped(c), region=region), origin)
            for c in found or () if c.value]


async def people(store, company_id: str, found, origin: str = "") -> int:
    """The people a capability named at a company (api.Person): kept once
    each by name, to be searched for a contact of their own. How many were
    new."""
    named = [(f"{p.first_name} {p.last_name}".strip(), p.title or "") for p in found or ()]
    named = [(name, role) for name, role in named if name]
    if not named or not company_id:
        return 0
    source = next((p.module for p in found if p.module), "")
    return await store.add_people(company_id, named, source=source, origin=origin)


async def named_people(batch, company_id: str, found: list[Fact], *, origin: str = "",
                       source: str = "") -> None:
    """The people a source's CONTACT_FOUND facts name ("Ana Pop —
    administrator"), as people of the company (on a batch): records to
    search for a contact, kept whether or not the signal is confirmed."""
    from harvey import contacts

    named = [contacts.split_person(f.value_text or "") for f in found
             if f.code == "CONTACT_FOUND"]
    named = [(name, role) for name, role in named if name]
    if named and company_id:
        await batch.add_people(company_id, named, source=source, origin=origin)


# ── People, contacts and what a company is called ──


async def company_phone(store, company_id: str, display: str) -> bool:
    """A phone the company's own pages publish, as its phone only when it
    has none: a listing's number is never replaced."""
    return await store.fill_company_phone(company_id, display)


async def company_name(store, company_id: str, name: str) -> None:
    """The name a company's own site gives itself, over one the domain
    doesn't back up (the module decides that; this only writes it)."""
    await store.rename_company(company_id, name)


async def prospect(store, person) -> str:
    """A person at a company, as a contact. A contact already on file (same
    email or LinkedIn address) is kept as it is: a second source adds
    people, it doesn't rewrite them. Its email and its person are mirrored
    into the contacts by the prospect triggers (migration v21); its own
    phone, which needs parsing, here."""
    prospect_id = await store.add_prospect(person)
    phone = (getattr(person, "phone", "") or "").strip()
    if phone and person.company_id:
        from harvey import contacts
        from harvey.collectors.phones import phone_region

        try:
            company = await store.get_company(person.company_id)
            await store.add_contacts(person.company_id, [contacts.Found(
                "phone", phone, person=person.full_name(), role=person.title or "",
                source=person.source or "", evidence_url=person.source_url or "",
                trust="likely", region=phone_region(
                    getattr(company, "location", "") or "", None,
                    getattr(company, "domain", "") or (person.email or "").rpartition("@")[2]))])
        except Exception as e:
            logger.debug(f"prospect phone not kept as a contact: {e}")
    return prospect_id


async def email_pattern(store, domain: str, **found) -> None:
    """What was learned about a domain's addresses: the pattern only over
    one of lower confidence; the mail host and catch-all always refresh."""
    await store.save_email_pattern(domain, **found)


async def place(store, key: str, point: str) -> None:
    """Where a place is ("-" for a place that can't be found), kept for
    good so it is asked once."""
    await store.set_setting(key, point)


# The month's count of a module's allowance, in Harvey's settings. (The
# name is from when Hunter's domain searches were the only allowance; it is
# kept so a month's count carries over.)
ALLOWANCE_KEY = "{module}_domain_search_count:{month}"


def monthly_allowance(module_id: str) -> int:
    """A module's allowance this month: its manifest's ``monthly_searches``
    setting, from the .env key the setting names when that is set, else
    the setting's default. 0 for a module that declares none."""
    import os

    from harvey.modules import switches

    manifest = switches.by_id().get(module_id)
    spec = (manifest.settings if manifest else {}).get("monthly_searches") or {}
    default = int(spec.get("default", 0) or 0)
    raw = os.getenv(spec["env"], "").strip() if spec.get("env") else ""
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


class Allowance(Budget):
    """The counted allowances a capability draws on, kept in Harvey's
    settings per module and calendar month (UTC): Hunter's domain searches,
    shared by every Hunter call (the pattern lookup and the named-people
    search). A unit is drawn before the request, so a call that fails
    mid-flight is still counted."""

    def __init__(self, store, remaining: float = float("inf")):
        self.store = store
        self.remaining = remaining

    @staticmethod
    def _key(what: str) -> str:
        return ALLOWANCE_KEY.format(module=what, month=f"{datetime.now(timezone.utc):%Y-%m}")

    async def left(self, what: str) -> int:
        cap = monthly_allowance(what)
        if self.store is None:
            return cap
        try:
            used = int(await self.store.get_setting(self._key(what), "0") or "0")
        except (ValueError, TypeError):
            used = 0
        except Exception as e:
            logger.debug(f"{what} allowance read failed: {e}")
            return 0
        return max(cap - used, 0)

    async def take(self, what: str) -> bool:
        if self.store is None:
            return True
        try:
            await self.store.increment_setting(self._key(what))
        except Exception as e:
            logger.debug(f"{what} allowance write failed: {e}")
        return True


_ef_log = logging.getLogger("harvey.email_finder")


async def learned_pattern(store, domain: str, learned: dict) -> None:
    """What an email lookup learned about a domain (EmailResult.learned):
    its pattern, then that it is a catch-all when a verifier said so."""
    if not learned:
        return
    try:
        await email_pattern(store, domain, pattern=learned.get("pattern", ""),
                            source=learned.get("source", ""),
                            confidence=learned.get("confidence", 0.0),
                            mx_type=learned.get("mx_type", ""))
    except Exception as e:
        _ef_log.debug(f"Pattern cache write failed for {domain}: {e}")
    if learned.get("is_catch_all"):
        try:
            await email_pattern(store, domain, mx_type=learned.get("mx_type", ""),
                                is_catch_all=1)
        except Exception:
            pass


async def person_contacts(store, company_id: str, person: str, role: str, found,
                          trust: str = "likely", origin: str = "") -> int:
    """A contact a lookup found for one person at a company (an address a
    verifier graded), linked to that person. How many were kept."""
    from harvey import contacts

    items = [contacts.Found(c.kind, c.value, display=c.display, status=c.status,
                            person=person, role=role, source=c.module,
                            capability=c.capability, evidence_url=c.source_url,
                            confidence=c.confidence, trust=trust)
             for c in found or () if c.value]
    if not items or not company_id:
        return 0
    return await store.add_contacts(company_id, items, origin)
