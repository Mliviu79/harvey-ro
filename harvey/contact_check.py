"""Verifying contacts with the classifier: yes, no or unsure.

For a link you choose (a contact and a company it is linked to), the
contacts task (harvey/classify.py, through the profile the install sets it
to) is asked about that one contact, with the company's register facts and
where the contact was found as its state: the company's own site read
again (free) when it has one and the contact is on it (the line it is
written on, the page), else what the evidence on file says (its sources,
the page it was read on, how sure each is). The answer may be "unsure"
too: a language model is allowed it in its JSON; a decision model's
probability is yes at the profile's contacts bar or above, no at 1 - bar
or below, unsure between.

- yes: a row saying so (by the classifier, the profile, the model, when).
  Nothing else changes: the model never confirms anything.
- no: the link's evidence from a website's read drops to unconfirmed,
  its trust before kept in the row for the undo (``harvey contacts verify
  --undo BATCH``). Official sources' evidence, and a contact in a
  campaign, are never lowered (the do-not-contact list isn't touched).
- unsure: nothing changes, and the link is listed for a person's review.

Nothing is deleted. A run is counted and priced first, then one job per
company on the classifier's lane, under a USD cap the user types and the
install's spend limits (classify.Meter).
"""

from __future__ import annotations

import dataclasses
import logging

from harvey import classify, finder, site_match

logger = logging.getLogger("harvey.contact_check")

VERDICTS = ("yes", "no", "unsure")
# Links one run takes at most.
MAX_LINKS = 2000


async def links(state, contact_ids, *, limit: int = MAX_LINKS) -> list[dict]:
    """Every live company each contact is linked to, once per (company,
    contact), with what the evidence says: [{"company_id", "contact_id",
    "kind", "value", "evidence": [{"source", "trust", "url"}]}]."""
    out: dict[tuple, dict] = {}
    for cid in dict.fromkeys(int(c) for c in contact_ids):
        reach = await state.read("reach", cid)
        kind_value = await _kind_value(state, cid)
        if kind_value is None:
            continue
        for r in reach.get("links") or []:
            key = (r["company_id"], cid)
            link = out.setdefault(key, {"company_id": r["company_id"], "contact_id": cid,
                                        "company": r.get("company") or "",
                                        "kind": kind_value[0], "value": kind_value[1],
                                        "evidence": []})
            link["evidence"].append({"source": r.get("source") or "",
                                     "trust": r.get("trust") or "",
                                     "url": r.get("evidence_url") or ""})
            if len(out) >= limit:
                return list(out.values())
    return list(out.values())


async def _kind_value(state, contact_id: int) -> tuple[str, str] | None:
    rows = await state.read("contact_kind_value", [contact_id])
    return rows.get(contact_id)


def by_company(chosen: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for link in chosen:
        out.setdefault(link["company_id"], []).append(link)
    return out


async def estimate(state, chosen: list[dict]) -> dict:
    """What verifying ``chosen`` may cost before anything is sent: a call
    per link, as a range (classify.projection: typical and high, at the
    contacts profile's price), never a limit: the run's cap is what is
    enforced, call by call (``note``)."""
    s = (await classify.settings(state, "contacts")).for_task("contacts")
    p = await classify.projection(state, s, "contacts", len(chosen), await classify.sizes(state))
    return {"links": len(chosen), "companies": len(by_company(chosen)), "calls": len(chosen),
            "tokens_typical": p["tokens_typical"], "tokens_high": p["tokens_high"],
            "basis": p["basis"], "usd_typical": p["usd_typical"], "usd_high": p["usd_high"],
            "price_known": p["price_known"], "profile": s.profile,
            "profile_name": s.profile_name, "provider": s.provider, "model": s.model_name,
            "off": await classify.available(state, "contacts", s) in ("off", "task off"),
            "note": classify.ESTIMATE_NOTE}


def _evidence_card(link: dict, read: site_match.Card | None) -> site_match.Card:
    """One contact as a card of its own: the line the company's site
    writes it on when the read found it there, else where the evidence
    on file says it was found."""
    value = str(link["value"]).lower()
    if read is not None:
        for j, c in enumerate(read.contacts):
            if str(c.get("value") or "").lower() == value:
                return dataclasses.replace(read, contacts=[dict(c)], picks=[read.picks[j]
                                                                           if j < len(read.picks)
                                                                           else None])
    where = "; ".join(f"{e['source'] or 'unknown source'} ({e['trust']})"
                      + (f" on {e['url'][:120]}" if e["url"] else "")
                      for e in link["evidence"][:4])
    # (Not on the site, or no site read: the card says what is on file,
    # so the contact is shown with it.)
    base = read or site_match.Card(domain="-", excerpt=["no website read: what is on file"])
    return dataclasses.replace(base, contacts=[{"kind": link["kind"], "value": link["value"],
                                                "line": f"on file from {where}"[:200],
                                                "page": (link["evidence"][0]["url"]
                                                         if link["evidence"] else "")}],
                               picks=[None], readable=True)


def verdict_of(value: dict | None) -> tuple[str, float | None]:
    """The yes, no or unsure of a one-contact answer (and a decision
    model's probability)."""
    if not value:
        return "unsure", None
    p = (value.get("p") or {}).get(0)
    if 0 in (value.get("contacts") or ()):
        return "yes", p
    if 0 in (value.get("no") or ()):
        return "no", p
    return "unsure", p


async def verify_company(state, company_id: str, chosen: list[dict], *, batch: str,
                         config=None) -> dict:
    """Ask about each of ``chosen`` (one company's links) and keep the
    verdicts (store.check_link). Stops at the first question with no
    answer (``why``: held, the day's calls, the run's budget...): the
    links not asked are left as they are."""
    from harvey.collectors.discover import normalize_domain

    out = {"company_id": company_id, "asked": 0, "yes": 0, "no": 0, "unsure": 0,
           "lowered": 0, "kept": [], "why": ""}
    s = await classify.settings(state, "contacts")
    why = await classify.available(state, "contacts", s)
    if why:
        out["why"] = why
        return out
    company = await finder.company_row(state, company_id)
    if company is None:
        out["why"] = "merged into another company, or gone"
        return out
    raw = await state.read("site_facts", company_id) or {}
    facts = site_match.Facts.of(raw)
    read = None
    website = company.get("website") or ""
    domain = normalize_domain(website) if website else ""
    if domain:
        async with finder._reader(state, set(await state.confirmed_signal_codes()),
                                  config) as reader:
            draft = await reader.fetch(company, domain=domain)
        if draft is not None:
            read = site_match.card(facts, domain, draft, url=website,
                                   holders=raw.get("holders"))
    done_before = await state.read("contact_check_done", batch, company_id) if batch else set()
    for link in chosen:
        if link["contact_id"] in done_before:
            continue            # (a job resumed: asked already)
        card = _evidence_card(link, read)
        ans = await classify.ask(state, "contacts", {"firm": facts.as_dict(), "cards": [card],
                                                     "region": facts.region, "unsure": True},
                                 s=s, config=config)
        if ans.value is None:
            out["why"] = ans.why or "no answer"
            break
        out["asked"] += 1
        verdict, p = verdict_of(ans.value)
        done = await state.check_link(company_id, link["contact_id"], verdict=verdict,
                                      batch=batch, profile=s.profile, model=ans.model, p=p,
                                      note=("read on its site" if read is not None and
                                            card.picks[0] is not None else "from the evidence"))
        out[verdict] += 1
        out["lowered"] += done.get("lowered", 0)
        if done.get("kept"):
            out["kept"].append(f"{link['value']}: {done['kept']}")
    return out


def summary(results: list[dict]) -> dict:
    total = {"companies": len(results), "asked": 0, "yes": 0, "no": 0, "unsure": 0,
             "lowered": 0, "kept": 0}
    for r in results:
        for k in ("asked", "yes", "no", "unsure", "lowered"):
            total[k] += int(r.get(k) or 0)
        total["kept"] += len(r.get("kept") or [])
    return total
