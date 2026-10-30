"""What a module's capability takes and gives back. Plain data, no framework.

A capability is one operation a module can do (``[[capabilities]]`` in its
manifest): look a company up, read its site, search an area. Core calls it
with an ``Input`` (the identity of the record it is called for, or a query)
and a ``Context`` (the HTTP client, the module's settings and keys, a
budget hook, a logger) and gets a ``Draft`` back: the records, facts,
people, contacts and pages it found, and what it actually spent. A
capability never writes Harvey's database and never imports harvey.state:
core ingest (harvey/modules/ingest.py) decides what of a draft is kept.

``estimate(input)`` says what a call would cost without making it: no
network, no database, just the manifest's prices and the input's size.

The code behind each module is named by its manifest's ``entry``: a module
(``harvey.modules.<set>.<id>.capabilities``) with a ``CAPABILITIES`` dict,
capability id to ``Capability``. ``capability(module_id, cap_id)`` finds
one; ``declared()`` lists every capability the manifests declare.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

# ── What a capability is called with ──


@dataclass
class Page:
    """A page of a company's own site, as read (``method``: "http" or the
    browser that rendered it)."""

    url: str
    html: str
    method: str = "http"


@dataclass
class Input:
    """The record a capability is called for, or what a query looks for.

    ``company_id`` is core's id of that record: echoed in what core writes,
    never used by a module to write. ``ids`` holds identifiers by kind
    (cui, reg, external, domain, phone). ``facts`` holds what is known
    already, by signal code (an email pattern on file, a search hit).
    ``query`` and ``area`` are for query and seed operations.
    """

    company_id: str = ""
    ids: dict[str, str] = field(default_factory=dict)
    name: str = ""
    locality: str = ""
    region: str = ""
    address: str = ""
    point: str = ""
    domain: str = ""
    phone: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)
    query: Any = None
    area: str = ""
    # Anything else the capability's own code takes (a person's name for an
    # email lookup, a registry query).
    extra: dict[str, Any] = field(default_factory=dict)


class Budget:
    """What core lets one call spend. ``remaining`` is money (US dollars);
    ``left(what)`` reads a counted allowance (Hunter's monthly domain
    searches), and a caller checks it first; ``take(what)`` then draws one
    unit before the request, so a call that fails mid-flight is still
    counted. Core gives a real one; this default allows everything and counts
    nothing (a one-off call outside an install)."""

    remaining: float = float("inf")

    async def take(self, what: str) -> bool:
        return True

    async def left(self, what: str) -> int:
        return 1 << 30


@dataclass
class Context:
    """Everything a capability may use besides its input."""

    module: str = ""
    http: Any = None                    # an httpx.AsyncClient, when core opened one
    browser: Any = None                 # a harvey.browser.PageReader
    settings: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    config: Any = None                  # HarveyConfig, for what an ICP says
    # Signal codes worth collecting (confirmed, from a module that is on);
    # None means every code. A capability skips work only these need.
    wanted: frozenset[str] | None = None
    # Modules that are on; None means all (a one-off call).
    enabled: frozenset[str] | None = None
    budget: Budget = field(default_factory=Budget)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("harvey.modules"))

    def wants(self, code: str) -> bool:
        return self.wanted is None or code in self.wanted

    def on(self, module_id: str) -> bool:
        # Code no module claims ("") has no switch.
        return self.enabled is None or not module_id or module_id in self.enabled


# ── What a capability gives back ──


@dataclass
class Record:
    """A company (or a link to one) a capability found. ``key`` names it
    within the draft, so facts and people can point at it."""

    key: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    phone: str = ""
    industry: str = ""
    location: str = ""
    address: str = ""
    description: str = ""
    company_size: str = ""
    # Legal identifiers by kind, as a manifest names them ({"cui": ...}).
    ids: dict[str, str] = field(default_factory=dict)
    legal_form: str = ""
    external_id: str = ""
    alt_ids: tuple[str, ...] = ()
    source: str = ""
    source_url: str = ""
    # The domain is what the record says of itself (a registry's website
    # field): worth a review, never a match on its own.
    declared_domain: bool = False
    # Every activity class the record is authorised for: (code, version).
    activities: list[tuple[str, int]] = field(default_factory=list)
    # The search phrase that found it.
    found_by: str = ""
    # What the source counted for it: {"administrators": 2}.
    counts: dict[str, int] = field(default_factory=dict)
    module: str = ""
    capability: str = ""


@dataclass
class Fact:
    """One observation. ``record`` is the key of the Record it is about,
    "" for the record the capability was called for. ``collector`` is the
    label the row carries (discover, profile, onrc, people)."""

    code: str
    value_num: float | None = None
    value_text: str = ""
    confidence: float = 1.0
    source_url: str = ""
    observed_at: str | None = None
    record: str = ""
    collector: str = ""
    module: str = ""
    capability: str = ""


@dataclass
class Person:
    """Someone named at a company. ``fields`` carries what a contact record
    holds beyond the name (seniority, the linkedin address, a score)."""

    first_name: str = ""
    last_name: str = ""
    title: str = ""
    email: str = ""
    email_status: str = ""
    source: str = ""
    source_url: str = ""
    record: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    module: str = ""
    capability: str = ""


@dataclass
class Contact:
    """A way to reach a company or a person: ``kind`` "phone" (``value`` in
    E.164, ``display`` as written for people), "email" (``status``
    verified, risky, guess or invalid), or any other kind a manifest
    declares (``[[contact_kinds]]``: a contact form's URL, a social
    profile's). A capability lists the kinds it returns in its
    ``outputs.contacts``."""

    kind: str
    value: str
    display: str = ""
    status: str = ""
    confidence: float = 1.0
    source_url: str = ""
    record: str = ""
    module: str = ""
    capability: str = ""


@dataclass
class Draft:
    """What one call found. ``cost`` is what it really spent (US dollars);
    ``values`` holds plain outputs (a point, an email pattern) by the names
    the manifest's ``outputs.values`` gives; ``stop`` is the source saying
    no more calls this run (an allowance spent); ``errors`` what went
    wrong without stopping it."""

    records: list[Record] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    stop: str = ""
    errors: list[str] = field(default_factory=list)
    # The call failed and found nothing: why (None when it didn't fail).
    failed: str | None = None
    # Signal codes this answer is complete for (a ranked search that ran to
    # its depth): a record that held one before and isn't in it no longer does.
    complete_codes: tuple[str, ...] = ()
    # What the capability counted: {"found": 9, "junk": 3}.
    counts: dict[str, int] = field(default_factory=dict)

    def stamp(self, module: str, capability: str) -> "Draft":
        """Say who found each part that doesn't say so yet."""
        for part in (*self.records, *self.facts, *self.people, *self.contacts):
            if not part.module:
                part.module, part.capability = module, capability
        return self


# ── Capabilities ──


def free(_input: Input) -> float:
    return 0.0


@dataclass(frozen=True)
class Capability:
    """One operation of a module: ``run(input, context)`` returns a Draft,
    ``estimate(input)`` its cost without making it. Called with a context
    whose ``enabled`` leaves its module out, it raises ModuleOff."""

    module: str
    id: str
    run: Callable[[Input, Context], Awaitable[Draft]]
    estimate: Callable[[Input], float] = free

    async def __call__(self, inp: Input, ctx: Context) -> Draft:
        if not ctx.on(self.module):
            # A module that is off does nothing, whoever calls it.
            from harvey.modules.switches import ModuleOff

            raise ModuleOff(self.module, f"{self.module}.{self.id}")
        if not ctx.module:
            ctx = replace(ctx, module=self.module)
        draft = await self.run(inp, ctx)
        return draft.stamp(self.module, self.id)


class CapabilityMissing(LookupError):
    """A capability a manifest declares has no code behind it."""


def _manifests():
    from harvey.modules import switches

    return switches.by_id()


def capability(module_id: str, cap_id: str, manifests=None) -> Capability:
    """The code behind ``module_id``'s capability ``cap_id``."""
    mans = manifests if manifests is not None else _manifests()
    manifest = mans.get(module_id)
    if manifest is None or not manifest.entry:
        raise CapabilityMissing(f"{module_id}.{cap_id}: no such module with code")
    if not any(c.get("id") == cap_id for c in manifest.capabilities):
        raise CapabilityMissing(f"{module_id}.{cap_id}: not declared in {manifest.path}")
    found = getattr(importlib.import_module(manifest.entry), "CAPABILITIES", {}).get(cap_id)
    if found is None:
        raise CapabilityMissing(f"{module_id}.{cap_id}: {manifest.entry} has no code for it")
    return found


def declared(manifests=None) -> list[tuple[Any, dict]]:
    """(manifest, capability declaration) for every declared capability."""
    mans = manifests if manifests is not None else _manifests()
    return [(m, cap) for m in mans.values() for cap in m.capabilities]


def outputs(manifest, cap_id: str) -> dict:
    for cap in manifest.capabilities:
        if cap.get("id") == cap_id:
            return dict(cap.get("outputs") or {})
    return {}


def undeclared(draft: Draft, manifests=None) -> list[str]:
    """What a draft holds that the capability that made each part doesn't
    declare in its manifest's ``outputs``: one line each."""
    mans = manifests if manifests is not None else _manifests()
    problems = []

    def out(module: str, cap: str) -> dict | None:
        m = mans.get(module)
        return outputs(m, cap) if m is not None else None

    for fact in draft.facts:
        o = out(fact.module, fact.capability)
        if o is None or fact.code not in (o.get("facts") or ()):
            problems.append(f"{fact.module}.{fact.capability} returned fact {fact.code}")
    for rec in draft.records:
        o = out(rec.module, rec.capability)
        if o is None or o.get("records") not in ("companies", "links", "registry"):
            problems.append(f"{rec.module}.{rec.capability} returned a company record")
    for person in draft.people:
        o = out(person.module, person.capability)
        if o is None or o.get("records") != "people":
            problems.append(f"{person.module}.{person.capability} returned a person")
    for contact in draft.contacts:
        o = out(contact.module, contact.capability) or {}
        ok = contact.kind in (o.get("contacts") or ()) or (
            "phone" in (o.get("ids") or ()) if contact.kind == "phone" else (
                "EMAIL_STATUS" in (o.get("facts") or ()) or o.get("records") == "people"))
        if not ok:
            problems.append(f"{contact.module}.{contact.capability} returned a "
                            f"{contact.kind} contact")
    return problems
