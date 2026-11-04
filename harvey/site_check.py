"""Checking the websites a search found before, and testing the classifier.

Before the contact finder chose sites on evidence (harvey/finder.py), a
search's best-scored result was read and everything on it linked, its
trust "unconfirmed" or "likely". The re-check takes each such firm once
(``harvey contacts rejudge``, or the Today page; one firm per job on a
lane of its own, so a person's Find contacts run never waits behind it):
its stored address first (a directory, a profile page, an institution is
ruled out with no visit), else its pages read again (free) and decided as
a new lookup is (finder.decide). A site that doesn't answer is taken off
only after UNREADABLE_DAYS different days of trying.

- Taken: the trust the evidence gives (likely or confirmed), why, and the
  contacts chosen (the classifier's yes, else what code confirms: all its
  read left on a site a register fact confirms, the official ones on one
  the model chose) raised to match. Nothing on a site taken is removed or
  made less sure, so an undo has nothing to put back there.
- Not the firm's: the website and exactly what its read left are taken off
  (overlay.store.reject_found_site, kept for an undo), and the firm is due
  for a new lookup.

Official sources' contacts, the do-not-contact list and campaign members
are never touched. It runs only when started, and counts first.

``judge_test`` runs the configured classifier over a labelled set (the
built-in fictional one, reference/classify_cases.jsonl, or your own file
in the same format) and says how it did: how to compare a local model, a
hosted free one and Claude.
"""

from __future__ import annotations

import json
import statistics
import time
from importlib import resources

from harvey import classify, finder, site_match
from harvey.modules import api, ingest

# ── Counting first ──


def _seconds_to_midnight() -> float:
    now = time.time()
    return 86400 - now % 86400 + 5


async def count(state) -> dict:
    """What a re-check would do: the firms it takes (found websites
    nothing checked: unconfirmed, likely), how many their stored address
    alone rules out (no visit), how many need a read and maybe a
    classifier call, and the classifier's calls today against its cap."""
    from harvey.collectors.discover import normalize_domain

    by_trust = await state.read("site_check_count")
    ids = await state.read("site_check_ids", -1)
    rows = await state.read("site_check_rows", ids)
    ruled: dict[str, int] = {}
    for r in rows:
        reg = (r.get("external_id") or "").rsplit(":", 1)[-1] if r.get("external_id") else ""
        facts = site_match.Facts(name=r["name"], cui=str(r["cui"] or ""), reg=reg)
        why = site_match.screen(r["website"], normalize_domain(r["website"]), facts)
        if why:
            ruled[why] = ruled.get(why, 0) + 1
    s = await classify.settings(state, "site")
    to_read = len(rows) - sum(ruled.values())
    calls = await classify.calls_today(state)
    per_day = s.task_cap("site") if s.provider != "off" else 0
    # (At most: a firm is a call per candidate, and one for the chosen
    # site's contacts, under the tighter of the two caps.)
    per_firm = finder.MAX_CANDIDATES + (1 if s.task_on("contacts") else 0)
    firms_a_day = min(s.per_day, per_day) // per_firm
    return {"firms": len(rows), "unconfirmed": by_trust.get("unconfirmed", 0),
            "likely": by_trust.get("likely", 0), "ruled_out_by_address": sum(ruled.values()),
            "by_reason": ruled, "to_read": to_read, "provider": s.provider,
            "calls_today": calls, "per_day": per_day,
            "days": (-(-to_read // firms_a_day) if firms_a_day else None)}


async def waiting(state) -> int:
    """How many found websites wait for a check (a cheap count, for Today)."""
    c = await state.read("site_check_count")
    return int(c.get("unconfirmed", 0) + c.get("likely", 0))


# ── One firm ──

# A re-read that got no page counts as a failure for the day; a site is
# taken off only after it didn't answer on this many different days (one
# timeout is no verdict).
UNREADABLE = "the site didn't answer"
UNREADABLE_DAYS = 3
# A check where the classifier said yes to two or more candidates: the
# search row's detail starts with this and the classifier it asked, which
# isn't asked the same again (another provider or model is).
UNSETTLED = "unsettled"
# Why the classifier can't be asked for a while: nothing is read meanwhile.
STOPS = ("held", "day cap", "budget", classify.RUN_BUDGET)


def _asked(s) -> str:
    return f"{UNSETTLED} by {s.provider} {s.model}"


async def recheck_one(state, company_id: str, *, batch: str = "", config=None) -> dict:
    """Check one firm's found website again (the module docstring).
    {"outcome": "taken" | "rejected" | "waiting" | "skipped", ...}; a
    "waiting" result says why nothing was decided ("off", "held", "day
    cap", "unreadable", ...), and nothing was changed. While the
    classifier is held or its day's calls are used, nothing is read: only
    what the address alone settles is decided. A site waiting since its
    lookup is judged with every candidate that lookup left in question."""
    from harvey.collectors.discover import normalize_domain

    company = await finder.company_row(state, company_id)
    if company is None or not company.get("website_found"):
        return {"outcome": "skipped", "why": "no website a search found"}
    basis = await state.read("website_basis", company_id)
    if basis.get("by") or company.get("website_trust") not in ("unconfirmed", "likely"):
        return {"outcome": "skipped", "why": "checked already"}
    raw = await state.read("site_facts", company_id) or {}
    facts = site_match.Facts.of(raw)
    website = company.get("website") or ""
    domain = normalize_domain(website)
    why = site_match.screen(website, domain, facts)
    if why:
        done = await state.reject_found_site(company_id, batch=batch,
                                             detail=f"not {domain}: {why}")
        return {"outcome": "rejected", "why": why, **done}
    s = await classify.settings(state)
    busy = await classify.available(state, "site", s)
    if busy in STOPS:
        return {"outcome": "waiting", "why": busy}
    if (await state.read("site_check_last", company_id)).startswith(_asked(s) + ":"):
        return {"outcome": "waiting", "why": UNSETTLED}
    others = [d for d in await state.read("site_candidates", company_id) or []
              if d != domain and not site_match.screen(f"https://{d}/", d, facts)]
    urls = [(domain, website)] + [(d, f"https://{d}/") for d in others]
    urls = urls[:finder.MAX_CANDIDATES]
    cards = []
    async with finder._reader(state, set(await state.confirmed_signal_codes()), config) as read:
        for dom, url in urls:
            draft = await read.fetch(company, domain=dom)
            cards.append(site_match.card(facts, dom, draft or api.Draft(), url=url,
                                         holders=raw.get("holders")))
    if not any(c.readable for c in cards):
        await state.record_search("company", company_id, *finder.CHECK, looked_for="website",
                                  outcome="error", detail=f"{UNREADABLE}: {domain}")
        days = await state.read("site_check_failures", company_id, UNREADABLE)
        if days < UNREADABLE_DAYS:
            return {"outcome": "waiting", "why": "unreadable", "days": days}
        done = await state.reject_found_site(
            company_id, batch=batch, detail=f"not {domain}: {UNREADABLE} on {days} days")
        return {"outcome": "rejected", "why": "unreadable", **done}
    d = await finder.decide(state, facts, cards, s=s, config=config)
    if d.waiting == UNSETTLED:
        # (Kept as it is, nothing linked; a person or another classifier decides.)
        await state.record_search(
            "company", company_id, *finder.CHECK, looked_for="website", outcome="nothing",
            detail=f"{_asked(s)}: {', '.join(cards[i].domain for i in d.pool)}"[:500])
    if d.waiting:
        return {"outcome": "waiting", "why": d.waiting}
    if d.index is None:
        done = await state.reject_found_site(company_id, batch=batch, detail=d.note)
        return {"outcome": "rejected", "why": d.note, **done}
    card = cards[d.index]
    if card.domain != domain and not await state.move_found_website(company_id, card.url):
        return {"outcome": "skipped", "why": "its website changed meanwhile"}
    chosen = finder.chosen_contacts(card, d.selection)
    values = [c.value for c in chosen] if d.selection is not None else None
    done = await state.accept_found_site(company_id, trust=d.trust, basis=d.basis,
                                         chosen=values, detail=d.note)
    # What the evidence chose, linked at its trust: a site that waited had
    # nothing linked yet (a contact linked already keeps its surer trust).
    if chosen:
        done["linked"] = await ingest.add_contacts(state, company_id, chosen, trust=d.trust)
    return {"outcome": "taken", "trust": d.trust, "website": card.domain, **done}


# ── Testing the classifier over a labelled set ──

BUILTIN = "reference/classify_cases.jsonl"


def builtin_cases() -> list[dict]:
    text = resources.files("harvey").joinpath(BUILTIN).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def read_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _url(r: dict) -> str:
    return r.get("url") or ""


def _cards(row: dict, facts: site_match.Facts, *,
           screen: bool = True) -> tuple[list[tuple[int, site_match.Card]], bool]:
    """The candidates the finder would read for ``row``, as today's code
    sees them: each one's address screened (site_match.screen; its result's
    URL when the row has one), at most MAX_CANDIDATES, and its card rebuilt
    from the row's pages when the file carries them (a candidate's
    ``pages``: [{"url", "html"}], with its stored contacts as code's read),
    else the card as stored. [(index in the row, card)], and whether any
    card was used as stored."""
    from harvey.collectors.discover import normalize_domain

    urls = {normalize_domain(_url(r)): _url(r) for r in row.get("results") or [] if _url(r)}
    firm = row.get("firm") or {}
    out, stored = [], False
    for i, c in enumerate(row.get("candidates") or []):
        domain = (c.get("domain") or "").lower()
        url = c.get("url") or urls.get(normalize_domain(domain)) or f"https://{domain}/"
        if screen and site_match.screen(url, domain, facts):
            continue
        pages = [api.Page(p.get("url") or url, p.get("html") or "") for p in c.get("pages") or []]
        if pages:
            draft = api.Draft(pages=pages, contacts=[
                api.Contact(k.get("kind") or "", k.get("value") or "",
                            display=k.get("value") or "", source_url=url)
                for k in c.get("contacts") or []])
            card = site_match.card(facts, domain, draft, url=url, holders=firm.get("holders"))
        else:
            card, stored = site_match.Card.of(c), True
        out.append((i, card))
    return (out[:finder.MAX_CANDIDATES] if screen else out), stored


def _values(card: site_match.Card, indices) -> set[str]:
    return {card.contacts[i]["value"].lower() for i in indices or []
            if 0 <= i < len(card.contacts)}


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


async def judge_test(state, rows: list[dict], *, every: bool = False, task: str = "site",
                     limit: int = 0, config=None, s=None) -> dict:
    """The configured classifier over ``rows`` (the judge-test format: a
    firm, its search results, its candidates' cards, what is expected):
    accuracy, false links (a site chosen where none is the firm's, or the
    wrong one: the costly error), misses, contact precision and recall
    (the ``contacts`` task, on a site rightly chosen), invalid answers,
    latency and tokens. Which rows are asked, and with which candidates,
    is what the finder would do with today's code (_cards,
    finder.question, finder.ask_sites: each candidate alone, exactly one
    yes chosen): results ruled out by their address, a site a register
    fact settles and one every rule rules out are never asked. ``every``
    asks every row about all its candidates. Every call counts against the
    day's cap. ``s``: the settings to test (a profile's,
    classify_profiles.settings_for), else those the install sets the task
    to. ValueError with the classifier off for the task."""
    from harvey.collectors.discover import normalize_domain

    s = (s or await classify.settings(state, task)).for_task(task)
    if s.provider == "off":
        raise ValueError(f"the classifier is off for the {task} task: set a profile for it "
                         "first (Settings → Classifier)")
    started = time.monotonic()
    out = {"provider": s.provider, "model": s.model_name, "profile": s.profile,
           "task": task, "rows": len(rows),
           "asked": 0, "correct": 0, "false_links": 0, "misses": 0,
           "invalid": 0, "unsure": 0, "not_asked": 0, "stored_cards": 0, "contacts_right": 0,
           "contacts_picked": 0, "contacts_expected": 0, "tokens_in": 0, "tokens_out": 0,
           "cost_usd": 0.0, "calls": 0, "failures": []}
    if s.provider in classify.SYSTEM_ONE:
        out["yes"] = s.yes
    latency = []

    def spent(answers, *, same_row: bool = False) -> None:
        out["calls"] += sum(a.tries for a in answers)
        out["tokens_in"] += sum(a.tokens_in for a in answers)
        out["tokens_out"] += sum(a.tokens_out for a in answers)
        out["cost_usd"] = round(out["cost_usd"] + sum(a.cost for a in answers), 10)
        ms = sum(a.ms for a in answers)
        if ms and same_row and latency:
            latency[-1] += ms
        elif ms:
            latency.append(ms)

    for row in rows:
        if limit and out["asked"] >= limit:
            break
        facts = site_match.Facts.of(row.get("firm") or {})
        expect = row.get("expect") or {}
        if task == "results":
            # The finder asks only when two or more results pass the screen.
            survivors = [(i, r) for i, r in enumerate(row.get("results") or [])
                         if every or not site_match.screen(_url(r), normalize_domain(_url(r)),
                                                           facts)]
            if len(survivors) < 2 or expect.get("results_keep") is None:
                out["not_asked"] += 1
                continue
            results = [api.Record(website=_url(r), name=r.get("title") or "",
                                  snippet=r.get("snippet") or "") for _, r in survivors]
            ans = await classify.ask(state, "results", {"firm": facts.as_dict(),
                                                         "results": results},
                                     s=s, config=config)
            if ans.why in STOPS + ("off", "task off"):
                out["stopped"] = ans.why
                break
            out["asked"] += 1
            spent([ans])
            if ans.value is None:
                out["invalid"] += 1
                out["failures"].append({"id": row.get("id"), "why": ans.why})
                continue
            if expect.get("unsure"):
                out["unsure"] += 1
                continue
            want = expect.get("results_keep") or []
            got = [survivors[k][0] for k in ans.value["keep"]]
            right = (not want and not got) or (bool(want) and bool(got) and got[0] in want)
            out["correct"] += right
            if got and not want:
                out["false_links"] += 1
            if want and not got:
                out["misses"] += 1
            if not right:
                out["failures"].append({"id": row.get("id"), "expected": want, "got": got})
            continue
        kept, stored = _cards(row, facts, screen=not every)
        cards = [c for _, c in kept]
        settled, pool, _ = finder.question(cards)
        if every:
            settled, pool = None, list(range(len(kept)))
        if settled is not None or not pool:
            out["not_asked"] += 1
            continue
        out["stored_cards"] += stored
        said, answers, why = await finder.ask_sites(state, facts, cards, pool, s=s,
                                                     config=config)
        if why in STOPS + ("off", "task off"):
            out["stopped"] = why
            break
        out["asked"] += 1
        spent(answers)
        if why:
            out["invalid"] += 1
            out["failures"].append({"id": row.get("id"), "why": why})
            continue
        if expect.get("unsure"):
            out["unsure"] += 1
            continue
        yes = [i for i, y in said if y]
        # (Two or more yes: the finder links nothing, the site waits.)
        got = kept[yes[0]][0] if len(yes) == 1 else None
        want = expect.get("candidate")
        if got == want:
            out["correct"] += 1
            if got is not None and expect.get("contacts") is not None:
                card = kept[yes[0]][1]
                ans = await finder.ask_contacts(state, facts, card, s=s, config=config)
                if ans is not None:
                    spent([ans], same_row=bool(sum(a.ms for a in answers)))
                if ans is not None and ans.value is not None:
                    stored_card = site_match.Card.of((row.get("candidates") or [])[want])
                    chosen = _values(card, ans.value["contacts"])
                    expected = _values(stored_card, expect["contacts"])
                    out["contacts_right"] += len(chosen & expected)
                    out["contacts_picked"] += len(chosen)
                    out["contacts_expected"] += len(expected)
        else:
            if got is not None:
                out["false_links"] += 1
            else:
                out["misses"] += 1
            out["failures"].append({"id": row.get("id"), "expected": want, "got": got,
                                    "said": {kept[i][0]: y for i, y in said}})
    scored = out["asked"] - out["invalid"] - out["unsure"]
    out["scored"] = scored
    out["accuracy"] = _pct(out["correct"], scored)
    out["contact_precision"] = _pct(out["contacts_right"], out["contacts_picked"])
    out["contact_recall"] = _pct(out["contacts_right"], out["contacts_expected"])
    latency.sort()
    out["latency_ms"] = {"median": int(statistics.median(latency)) if latency else None,
                         "p90": latency[min(len(latency) - 1, int(len(latency) * 0.9))]
                         if latency else None}
    out["failures"] = out["failures"][:20]
    out["seconds"] = round(time.monotonic() - started, 1)
    # (After the calls: a reply that carries its cost makes it known.)
    out["cost_known"] = (await classify.cost_status(state, s))["known"]
    return out


async def estimate_test(state, rows: list[dict], *, s, tasks=("site",), every: bool = False,
                        limit: int = 0) -> dict:
    """What judge_test would ask over ``rows`` through ``s`` (a profile's
    settings), counted before anything is sent: the calls per task (a
    row's contacts question only when its site is chosen right), the
    tokens their requests hold (built as they would be, never sent) and
    what they may cost at the profile's price, as a range: typical (the
    tokens counted, and the chat template's) and high (classify.
    worst_tokens), each with the most the task may answer. An estimate:
    the run's cap is what is enforced, call by call (``note``).
    ``price_known`` False: no price, nothing can be counted in USD."""
    from harvey.collectors.discover import normalize_domain

    out = {"rows": len(rows), "calls": 0, "tokens_in": 0, "usd_typical": 0.0, "usd_high": 0.0,
           "price_known": True, "by_task": {}, "note": classify.ESTIMATE_NOTE}
    for task in tasks:
        st = s.for_task(task)
        if st.provider == "off" or not st.task_on(task):
            out["by_task"][task] = {"calls": 0, "tokens_in": 0, "usd_typical": 0.0,
                                    "usd_high": 0.0, "off": True}
            continue
        calls, size, low, high, asked = 0, 0, 0.0, 0.0, 0
        sizes: list[tuple[str, int]] = []
        for row in rows:
            if limit and asked >= limit:
                break
            facts = site_match.Facts.of(row.get("firm") or {})
            expect = row.get("expect") or {}
            if task == "results":
                survivors = [r for r in row.get("results") or []
                             if every or not site_match.screen(_url(r), normalize_domain(_url(r)),
                                                               facts)]
                if len(survivors) < 2 or expect.get("results_keep") is None:
                    continue
                results = [api.Record(website=_url(r), name=r.get("title") or "",
                                      snippet=r.get("snippet") or "") for r in survivors]
                sizes.append(("results", classify.request(
                    st, "results", {"firm": facts.as_dict(), "results": results})["tokens"]))
                asked += 1
                continue
            kept, _ = _cards(row, facts, screen=not every)
            cards = [c for _, c in kept]
            settled, pool, _ = finder.question(cards)
            if every:
                settled, pool = None, list(range(len(kept)))
            if settled is not None or not pool:
                continue
            asked += 1
            for i in pool:
                sizes.append(("site", classify.request(
                    st, "site", {"firm": facts.as_dict(), "cards": [cards[i]],
                                 "region": facts.region})["tokens"]))
            want = expect.get("candidate")
            card = next((c for j, c in kept if j == want), None)
            if card is not None and card.contacts and expect.get("contacts") is not None \
                    and st.task_on("contacts"):
                sizes.append(("contacts", classify.request(
                    st.for_task("contacts"), "contacts",
                    {"firm": facts.as_dict(), "cards": [card], "region": facts.region})["tokens"]))
        for name, tokens_in in sizes:
            sx = st.for_task(name)
            cost, known = await classify.call_cost(state, sx, name,
                                                   tokens_in + classify.TEMPLATE_TOKENS)
            worst, _ = await classify.call_cost(state, sx, name, classify.worst_tokens(tokens_in))
            calls += 1
            size += tokens_in
            low += cost
            high += worst
            out["price_known"] = out["price_known"] and known
        out["by_task"][task] = {"calls": calls, "tokens_in": size, "usd_typical": round(low, 6),
                                "usd_high": round(high, 6), "rows_asked": asked}
        out["calls"] += calls
        out["tokens_in"] += size
        out["usd_typical"] = round(out["usd_typical"] + low, 6)
        out["usd_high"] = round(out["usd_high"] + high, 6)
    return out


def summary_lines(r: dict) -> list[str]:
    """judge_test's result as a few lines for a terminal."""
    lat = r.get("latency_ms") or {}
    lines = [f"Classifier: {r['provider']} {r.get('model') or ''}".rstrip()
             + (f" (contacts kept at {r['yes']:.2f})" if r.get("yes") is not None else ""),
             f"Rows: {r['rows']}, asked {r['asked']} ({r['task']} task), "
             f"{r['not_asked']} the finder wouldn't ask"
             + (f"; stopped: {r['stopped']}" if r.get("stopped") else ""),
             f"Accuracy: {r['accuracy'] if r['accuracy'] is not None else '-'}% of "
             f"{r['scored']} scored ({r['unsure']} unsure left out)",
             f"False links: {r['false_links']}   Misses: {r['misses']}   "
             f"Invalid answers: {r['invalid']}"]
    if r["task"] == "site":
        lines.append(f"Contacts: precision {r['contact_precision'] if r['contact_precision'] is not None else '-'}%, "
                     f"recall {r['contact_recall'] if r['contact_recall'] is not None else '-'}%")
    if r.get("stored_cards"):
        lines.append(f"{r['stored_cards']} row(s) asked with their cards as stored (no pages "
                     "in the file to read them again with today's code)")
    lines.append(f"Latency: median {lat.get('median') if lat.get('median') is not None else '-'} ms, "
                 f"p90 {lat.get('p90') if lat.get('p90') is not None else '-'} ms   "
                 f"Tokens: {r['tokens_in']:,} in, {r['tokens_out']:,} out")
    if "cost_usd" in r:
        lines.append(f"Spend: {classify.usd(r['cost_usd'])}" + (
            "" if r.get("cost_known", True) else f" ({classify.COST_UNKNOWN})"))
    return lines
