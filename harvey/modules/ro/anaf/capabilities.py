"""ANAF's company check. The code it runs is service.py (the request, the
pace, the kept answers).

``company_check`` is called with up to 100 companies at a time
(``extra["batch"]``: ``[{"key": <record>, "cui": <cui>}]``) and returns, for
each one ANAF knows, its facts and phone numbers keyed to that record. A
kept answer younger than the ``cache_days`` setting is used instead of
asking again; ``extra["missing_only"]`` leaves out every CUI that has a
kept answer at all. ``values`` counts what was asked, kept, unknown to
ANAF, invalid, and the requests made."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harvey.modules.api import Capability, Contact, Draft, Fact
from harvey.modules.ro.anaf import service

COLLECTOR = "anaf"


def _bool(value) -> float | None:
    return float(value) if isinstance(value, bool) else None


def facts_for(item: dict, key: str = "", observed_at: str | None = None) -> tuple[list[Fact], list[Contact]]:
    """What one ANAF answer says about a company, as facts (about the
    record ``key``) and its phone numbers as contacts."""
    general = item.get("date_generale") or {}
    vat = item.get("inregistrare_scop_Tva") or {}
    cash = item.get("inregistrare_RTVAI") or {}
    split = item.get("inregistrare_SplitTVA") or {}
    inactive = item.get("stare_inactiv") or {}
    facts: list[Fact] = []

    def add(code, value_num=None, value_text=""):
        facts.append(Fact(code, value_num=value_num, value_text=value_text,
                          source_url=service.API_URL, record=key, collector=COLLECTOR,
                          observed_at=observed_at))

    for code, raw in (("VAT_REGISTERED", vat.get("scpTVA")),
                      ("VAT_ON_CASH", cash.get("statusTvaIncasare")),
                      ("SPLIT_VAT", split.get("statusSplitTVA")),
                      # An explicit "no": ANAF answered that it isn't inactive.
                      ("FISCALLY_INACTIVE", inactive.get("statusInactivi")),
                      ("EFACTURA_REGISTERED", general.get("statusRO_e_Factura"))):
        value = _bool(raw)
        if value is not None:
            add(code, value)
    if vat.get("scpTVA") is True:
        since = service.vat_since(item)
        if since:
            add("VAT_SINCE", value_text=since)
    status = service.status_words(general.get("stare_inregistrare"))
    if status:
        add("REGISTRATION_STATUS", value_text=status)
    form = " ".join(str(general.get("forma_juridica") or "").split())
    if form:
        add("LEGAL_FORM", value_text=form)
    for line in service.addresses(item):
        add("ADDRESS", value_text=line)
    contacts = []
    for number, display in service.phones(general.get("telefon")):
        add("PHONE_NUMBER", value_text=number)
        contacts.append(Contact("phone", number, display=display, source_url=service.API_URL,
                                record=key))
    return facts, contacts


async def _check(inp, ctx) -> Draft:
    settings = ctx.settings or {}
    path = Path(settings.get("path") or service.CACHE_PATH)
    days = int(settings.get("cache_days") or 30)
    missing_only = bool(inp.extra.get("missing_only"))
    batch = list(inp.extra.get("batch") or [])
    if not batch and inp.ids.get("cui"):
        batch = [{"key": "", "cui": inp.ids["cui"]}]
    keys: dict[str, list[str]] = {}
    invalid = 0
    for entry in batch:
        cui = service.clean_cui(entry.get("cui"))
        if not cui:
            invalid += 1
            continue
        keys.setdefault(cui, []).append(entry.get("key", ""))
    cuis = list(keys)
    if len(cuis) > service.MAX_BATCH:
        return Draft(failed=f"at most {service.MAX_BATCH} companies a call")
    # (Off the event loop: a run saves its last batch meanwhile.)
    kept = await asyncio.to_thread(service.kept, path, cuis)
    answers: dict[str, tuple[str, dict | None]] = {}
    for cui, (fetched_at, answer) in kept.items():
        if missing_only or service.fresh(fetched_at, days):
            answers[cui] = (fetched_at, answer)
    ask = [c for c in cuis if c not in answers]
    values = {"asked": len(ask), "cached": len(answers), "not_found": 0,
              "invalid": invalid, "requests": 0}
    if ask:
        if ctx.http is None:
            return Draft(failed="no connection given", values=values)
        try:
            found, missing, made = await service.ask(ctx.http, ask)
        except service.AnafError as e:
            return Draft(failed=str(e), values=values, stop="busy" if e.busy else "")
        values["requests"] = made
        when = service.now()
        try:
            await asyncio.to_thread(service.keep, path, found, missing, when)
        except OSError as e:
            ctx.log.warning("anaf: answers not kept (%s)", e)
        for cui in ask:
            answers[cui] = (when, found.get(cui))
    draft = Draft(values=values)
    for cui, (fetched_at, answer) in answers.items():
        if answer is None:
            values["not_found"] += 1
            continue
        for key in keys[cui]:
            facts, contacts = facts_for(answer, key, observed_at=_sql_time(fetched_at))
            draft.facts += facts
            draft.contacts += contacts
    return draft


def _sql_time(iso: str) -> str | None:
    """An ISO time as the observations table writes its own (UTC, no T)."""
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M:%S")


CAPABILITIES = {
    "company_check": Capability("anaf", "company_check", _check),
}
