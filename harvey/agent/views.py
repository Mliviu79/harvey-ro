"""What the agent's tools return: the schema it may query, the named views,
the estimates and the jobs.

Every function here reads only, on a readonly.ReadOnlyState (its
connections opened read-only, so a write would fail) or a readonly
connection. The views run Harvey's own named statements
(harvey/overlay/queries.py, ``overlay.agent.*``) and page reads, and
return Harvey's own fields and counts: never a column readonly.py leaves
out. The estimates are the ones the dashboard shows before a run, with
nothing that spends or queues: a Discover estimate never asks a source
to count its results (that is a paid request), and the classifier's price
list is never fetched (classify_prices.OFFLINE).
"""

from __future__ import annotations

import asyncio
import time

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries
from harvey.agent import readonly

# The most firms a view lists, and the most it counts coverage over.
FIRMS_LIMIT = 50
COVERAGE_MAX = 200_000
SEARCH_DAYS = 30
ESTIMATES = ("discover", "registry", "find_contacts", "verify")


def _x(con, name: str, params=()):
    return con.execute(queries.sql(name), params).fetchall()


def _db(state) -> str:
    return state.db_path


# ── schema() ──

def schema(db_path) -> dict:
    """The tables the agent may read with their columns, what each holds,
    the fact catalog (the signal table) and the registers' tables."""
    from harvey.overlay import reads

    con, attached = readonly.connect(db_path)
    try:
        main = readonly.readable(con)
        reader = reads.Reader(con, attached)
        catalog = [{k: r.get(k) for k in ("code", "label", "description", "category",
                                         "value_type", "unit", "cost_note", "module",
                                         "available", "subject")}
                   for r in reader.signal_vocabulary()]
        regs = {}
        for a in attached:
            regs[a.alias] = {"module": a.module,
                             "tables": {t: readonly.columns(con, t, a.alias)
                                        for t in readonly.tables(con, a.alias)}}
    finally:
        con.close()
    return {
        "harvey_db": {t: {"columns": cols, "about": TABLE_NOTES.get(t, "")}
                      for t, cols in main.items()},
        "left_out": {"tables": sorted(readonly.EXCLUDED_TABLES),
                     "columns": sorted(f"{t}.{c}" for t, c in readonly.EXCLUDED_COLUMNS)},
        "registers": regs,
        "facts": catalog,
        "notes": SCHEMA_NOTES,
    }


SCHEMA_NOTES = [
    "Times in firm, contact, link, search, obs and reading are unix seconds; the Email "
    "module's tables (prospects, campaigns, outbox, jobs, usage_events) use SQLite "
    "datetime text.",
    "firm.live = 0 is a firm itself; another value is the firm it was merged into. "
    "Count firms with WHERE live = 0.",
    "A firm's register identifiers are in firm_ident (scheme -> ident_scheme.name, key); "
    "the registers are attached under their aliases and read in place.",
    "trust and best_trust: 0 confirmed, 1 likely, 2 unconfirmed. contact.kind -> "
    "contact_kinds.id. search.outcome: 0 error, 1 found, 2 nothing, 3 skipped.",
    "A fact (signal) a website showed is a reading row (num, positive); a register's is "
    "read from the register's own tables.",
]

TABLE_NOTES = {
    "firm": "every company in Harvey (live = 0), with kept counts of its contacts",
    "firm_ident": "register identifiers of a firm (CUI, registration number, listing ids)",
    "local_firm": "the description of a firm no register describes (a listing)",
    "contact": "one row per distinct email, phone, contact form or social profile",
    "link": "which firm a contact reaches, with its trust",
    "link_evidence": "where each link was found, by source",
    "person": "people Harvey found or acted on at a firm",
    "search": "every search of a firm or person by a module's capability",
    "firm_source": "every source a firm was found in",
    "signal": "the fact catalog: codes, labels, value types, cost notes",
    "reading": "the current value of a web fact per firm",
    "obs": "the history of web facts (numbers only)",
    "prospects": "the Email module's prospect cards",
    "campaigns": "email campaigns",
    "campaign_member": "who a campaign writes to and how far each got",
    "outbox": "outgoing email (status and times only)",
    "suppression": "the do-not-contact list",
    "jobs": "background jobs, one per search, read, check",
    "job_batches": "runs (a click that queued jobs), with their spend caps",
    "usage_events": "Harvey's own model calls",
}


# ── Named views ──

def pipeline(db_path) -> dict:
    """The whole pipeline in counts: firms, contacts, the backlog, the
    Email module's prospects, members, outbox and conversations."""
    from harvey.overlay import reads

    con, attached = readonly.connect(db_path)
    try:
        r = reads.Reader(con, attached)
        con.execute("BEGIN")
        out = {"firms": r.tally(), "overview": r.overview(), "backlog": r.backlog()}
        out["prospects"] = dict(_x(con, "overlay.agent.prospects"))
        members: dict = {}
        for campaign, status, n in _x(con, "overlay.agent.members"):
            members[status] = members.get(status, 0) + n
        out["campaign_members"] = _member_names(members)
        outbox: dict = {}
        for _, kind, status, n in _x(con, "overlay.agent.outbox"):
            by = outbox.setdefault(kind or "", {})
            by[status] = by.get(status, 0) + n
        out["outbox"] = outbox
        out["conversations"] = [{"status": s, "stage": g, "n": n}
                                for s, g, n in _x(con, "overlay.agent.conversations")]
        con.execute("ROLLBACK")
    finally:
        con.close()
    return out


def _member_names(by_status: dict) -> dict:
    from harvey.overlay.schema import MEMBER_STATUS

    names = {v: k for k, v in MEMBER_STATUS.items()}
    return {names.get(k, str(k)): n for k, n in by_status.items()}


def _place_filter(county: str = "", locality: str = "", activity: str = ""):
    from harvey import contacts
    from harvey.contact_query import caen_arg

    try:
        caen = caen_arg((activity or "").strip()[:200])
    except ValueError as e:
        raise readonly.Refused(str(e))
    return contacts.ContactFilter(county=(county or "").strip()[:200],
                                  locality=(locality or "").strip()[:200], caen=caen)


FIRM_FIELDS = ("id", "name", "cui", "location", "website", "industry", "contacts",
               "searched_at", "created_at", "source")


def firms(db_path, county: str = "", locality: str = "", activity: str = "",
          limit: int = 20) -> dict:
    """Firms in Harvey by place (county, town) and activity (CAEN codes or
    words): how many match, and the newest ``limit`` of them."""
    f = _place_filter(county, locality, activity)
    limit = max(1, min(int(limit or 20), FIRMS_LIMIT))

    def read(reader):
        total, ids, _ = reader.companies_page(f, "created", "desc", limit, 0)
        rows = reader.company_rows(ids)
        return total, rows

    total, rows = _with_reader(db_path, read)
    out = []
    for r in rows:
        row = {k: r.get(k) for k in FIRM_FIELDS}
        row["activities"] = [a.get("code") for a in r.get("activities") or [] if a.get("code")]
        figures = r.get("figures") or {}
        row["figures"] = {k: figures.get(k) for k in ("year", "turnover", "employees")
                          if k in figures}
        out.append(row)
    return {"filter": {"county": f.county, "locality": f.locality, "caen": list(f.caen)},
            "total": total, "shown": len(out), "firms": out}


def _with_reader(db_path, fn):
    from harvey.overlay import reads

    con, attached = readonly.connect(db_path)
    try:
        reader = reads.Reader(con, attached)
        con.execute("BEGIN")
        try:
            return fn(reader)
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
    finally:
        con.close()


def coverage(db_path, county: str = "", locality: str = "", activity: str = "") -> dict:
    """What was done where: of the firms a place and activity select, how
    many have a website known and read, were searched by the contact
    finder, and have contacts; the backlog; the searches of the last
    SEARCH_DAYS days by module and outcome."""
    from harvey.overlay.schema import OUTCOME

    f = _place_filter(county, locality, activity)
    narrowed = bool(f.county or f.locality or f.caen)
    outcomes = {v: k for k, v in OUTCOME.items()}

    def read(reader):
        out = {"filter": {"county": f.county, "locality": f.locality, "caen": list(f.caen)}}
        if narrowed:
            ids = reader.matching(f)
            out["firms"] = len(ids)
            if len(ids) > COVERAGE_MAX:
                out["note"] = (f"{len(ids):,} firms: counted for at most {COVERAGE_MAX:,}; "
                               "narrow the place or the activity")
            else:
                out["of_them"] = _coverage_of(reader.con, ids)
        else:
            t = reader.tally()
            out["firms"] = t.get("firms", 0)
            out["of_them"] = {"with_contact": t.get("firms_with_contact", 0),
                              "with_site": t.get("firms_with_site", 0)}
        out["backlog"] = reader.backlog()
        since = int(time.time()) - SEARCH_DAYS * 86400
        out["searches_since_days"] = SEARCH_DAYS
        out["searches"] = [{"module": m, "capability": c, "outcome": outcomes.get(o, o),
                            "n": n, "cost_usd": round(cost or 0.0, 4),
                            "last_at": last}
                           for m, c, o, n, cost, last in
                           _x(reader.con, "overlay.agent.searches", (since,))]
        return out

    return _with_reader(db_path, read)


def _coverage_of(con, ids) -> dict:
    import json

    keys = ("firms", "with_site", "site_read", "finder_searched", "searched", "with_contact",
            "with_email", "with_phone", "with_people")
    sums = [0] * len(keys)
    ids = list(ids)
    for start in range(0, len(ids), 5000):
        row = con.execute(queries.sql("overlay.agent.coverage"),
                          (json.dumps(ids[start:start + 5000]),)).fetchone()
        sums = [a + (b or 0) for a, b in zip(sums, row)]
    out = dict(zip(keys, sums))
    out.pop("firms")
    return out


def contacts_summary(db_path) -> dict:
    """Contacts by kind, line type, trust and status, with how many are on
    the do-not-contact list."""
    from harvey.overlay.schema import CONTACT_STATUS, LINE_TYPE, TRUST_NAMES

    lines = {v: k for k, v in LINE_TYPE.items()}
    statuses = {v: k for k, v in CONTACT_STATUS.items()}
    con, _ = readonly.connect(db_path)
    try:
        rows = _x(con, "overlay.agent.contacts")
    finally:
        con.close()
    out, totals = [], {}
    for kind, line, trust, status, n, dnc in rows:
        out.append({"kind": kind, "line_type": lines.get(line) if line is not None else None,
                    "trust": TRUST_NAMES.get(trust) if trust is not None else "unlinked",
                    "status": statuses.get(status) if status is not None else None,
                    "n": n, "do_not_contact": dnc or 0})
        totals[kind] = totals.get(kind, 0) + n
    return {"by_kind": totals, "rows": out}


def campaigns(db_path) -> dict:
    """Every campaign with its members by status and its outbox by status."""
    con, _ = readonly.connect(db_path)
    try:
        con.execute("BEGIN")
        rows = _x(con, "overlay.agent.campaigns", (50,))
        members: dict = {}
        for campaign, status, n in _x(con, "overlay.agent.members"):
            members.setdefault(campaign, {})[status] = n
        outbox: dict = {}
        for campaign, kind, status, n in _x(con, "overlay.agent.outbox"):
            outbox.setdefault(campaign or "", {})[f"{kind}:{status}"] = n
        con.execute("ROLLBACK")
    finally:
        con.close()
    out = []
    for cid, name, status, origin, rolling, audience, started, ended, updated in rows:
        out.append({"id": cid, "name": name, "status": status, "origin": origin,
                    "rolling": bool(rolling), "audience_id": audience, "started_at": started,
                    "ended_at": ended, "updated_at": updated,
                    "members": _member_names(members.get(cid, {})),
                    "outbox": outbox.get(cid, {})})
    return {"campaigns": out}


async def spend(state) -> dict:
    """Jobs by lane and status, the paused lanes, what was spent today and
    this month against the install's limits, the newest runs, and Harvey's
    own model calls today."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import _config, _spend_limits

    limits = _spend_limits(_config())
    counts = await jobs.counts(state)
    batches = await jobs.recent_batches(state, 10)

    def usage():
        con, _ = readonly.connect(_db(state))
        try:
            day = time.strftime("%Y-%m-%d 00:00:00", time.gmtime())
            return [{"agent": a, "task": t, "calls": n, "tokens_in": i or 0,
                     "tokens_out": o or 0, "turns": turns or 0}
                    for a, t, n, i, o, turns in _x(con, "overlay.agent.usage", (day,))]
        finally:
            con.close()

    return {"jobs": counts, "paused_lanes": await jobs.paused_lanes(state),
            "spent_today_usd": round(await jobs.spent_since(state, "start of day"), 4),
            "spent_this_month_usd": round(await jobs.spent_since(state, "start of month"), 4),
            "limits_usd": limits, "recent_runs": batches,
            "model_calls_today": await asyncio.to_thread(usage)}


# ── jobs() ──

async def job_info(state, action: str = "list", ref: str = "", lane: str = "",
                   status: str = "", limit: int = 20) -> dict:
    """list (newest first; by lane and status), get (one job by id), or
    report (a run's time and cost)."""
    from harvey.modules import jobs

    action = (action or "list").strip().lower()
    if action == "list":
        statuses = [s for s in (status or "").split(",") if s.strip() in jobs.STATUSES] or None
        found = await jobs.list_jobs(state, lane=lane or None, statuses=statuses,
                                     limit=max(1, min(int(limit or 20), 200)))
        return {"jobs": [_job(j) for j in found]}
    if action == "get":
        job = await jobs.find(state, ref) if ref else None
        if job is None:
            raise readonly.Refused(f"no job {ref!r}")
        return {"job": _job(job)}
    if action == "report":
        if not ref or await jobs.get_batch(state, ref) is None:
            raise readonly.Refused(f"no run {ref!r}")
        return {"report": await jobs.batch_report(state, ref, keep=False)}
    raise readonly.Refused("action is list, get or report")


def _job(job) -> dict:
    d = job.as_dict()
    d.pop("last_line", None)
    return d


# ── estimate() ──

async def estimate(state, action: str, params: dict | None) -> dict:
    """The estimate the dashboard shows before a run: ``discover``
    (provider, cities, terms, preset, depth, limit), ``registry`` (the
    registry's filters: words, county, locality, status, turnover and
    headcount bounds, employer, vat, micro), ``find_contacts`` (a Contacts
    filter: county, locality, caen, website, kind, ...; force, limit) or
    ``verify`` (contact_ids, or a Contacts filter and limit). Nothing is
    queued, spent or asked of a source."""
    p = params if isinstance(params, dict) else {}
    action = (action or "").strip().lower()
    if action == "discover":
        return _discover_estimate(state, p)
    if action == "registry":
        return await _registry_estimate(p)
    if action == "find_contacts":
        return await _find_estimate(state, p)
    if action == "verify":
        return await _verify_estimate(state, p)
    raise readonly.Refused(f"action is one of {', '.join(ESTIMATES)}")


def _discover_estimate(state, p: dict) -> dict:
    from harvey.collectors.discover import (PROVIDERS, DiscoveryQuery, build_queries,
                                            estimate_cost, is_discovery_source,
                                            preset_problem)
    from harvey.modules.job_kinds import _config

    key = str(p.get("provider") or "").strip()
    if key not in PROVIDERS or not is_discovery_source(key):
        raise readonly.Refused(f"provider is one of {', '.join(sorted(k for k in PROVIDERS if is_discovery_source(k)))}")
    config = _config()
    if config is None:
        raise readonly.Refused("Harvey's settings can't be read")
    preset = str(p.get("preset") or "")
    problem = preset_problem(preset, key, config=config)
    if problem:
        raise readonly.Refused(problem)
    cities = [str(c).strip() for c in (p.get("cities") or []) if str(c).strip()] or None
    depth = int(p.get("depth") or 0) or None
    limit = int(p.get("limit") or 0) or None
    terms = [str(t).strip() for t in (p.get("terms") or []) if str(t).strip()]
    if terms:
        places = cities or list(config.icp.geography) or [""]
        coords = getattr(config.icp, "geo_coordinates", {}) or {}
        language = getattr(config.icp, "search_language", "") or "en"
        qs = [DiscoveryQuery(term=t, location=c, depth=depth, limit=limit,
                             coordinate=coords.get(c, ""), language=language)
              for t in terms[:50] for c in places[:50]]
    else:
        qs = build_queries(config, cities=cities, depth=depth, limit=limit, preset=preset)
    return {"provider": key, "label": PROVIDERS[key].label,
            "queries": [q.keyword() for q in qs][:200], "query_count": len(qs),
            "estimated_cost_usd": round(estimate_cost(key, qs), 4),
            "free": PROVIDERS[key].estimate(qs) == 0,
            "note": "the price of every page each search may take; the run's cap stops it. "
                    "Not counted at the source (that is a paid request)."}


async def _registry_estimate(p: dict) -> dict:
    from harvey.modules.job_kinds import registry_count, registry_plan

    try:
        params = registry_plan(p)
    except ValueError as e:
        raise readonly.Refused(str(e))
    label = params.pop("label")
    try:
        n = await asyncio.to_thread(registry_count, params)
    except FileNotFoundError as e:
        return {"label": label, "firms": None, "note": f"{e} (the filter needs it)"}
    return {"label": label, "firms": n, "cost_usd": 0.0,
            "note": "free: counted in the registry on file" if n is not None
                    else "no registry loaded yet"}


async def _contact_filter(state, p: dict):
    from harvey.contact_query import filter_from_query

    raw = p.get("filter") if isinstance(p.get("filter"), dict) else {
        k: v for k, v in p.items() if k not in ("force", "limit", "contact_ids")}
    try:
        f, _ = await filter_from_query(state, {k: str(v) for k, v in raw.items()
                                               if v is not None})
    except ValueError as e:
        raise readonly.Refused(str(e))
    return f


async def _find_estimate(state, p: dict) -> dict:
    from harvey import finder
    from harvey.config import load_env
    from harvey.modules import switches

    f = await _contact_filter(state, p)
    limit = max(1, min(int(p.get("limit") or 50_000), 50_000))
    force = bool(p.get("force"))
    selected = await finder.select(state, f, force=force, limit=limit)
    enabled, env = await switches.enabled_ids(state), load_env().model_dump()
    selected = await finder.plan(state, selected, enabled, env, force=force)
    out = finder.estimate(selected, enabled, env)
    return {**out, "more": len(selected) >= limit,
            "checks": await finder.checks_estimate(state, selected, enabled, env),
            "classifier": await finder.classify_estimate(state, selected)}


async def _verify_estimate(state, p: dict) -> dict:
    from harvey import contact_check

    limit = max(1, min(int(p.get("limit") or 200), contact_check.MAX_LINKS))
    ids = p.get("contact_ids")
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise readonly.Refused("contact_ids is a list of contact ids")
    else:
        f = await _contact_filter(state, p)
        _, _, ids = await state.read("contacts_page", f, "", "desc", limit, 0, cap=limit)
    chosen = await contact_check.links(state, ids[:limit], limit=limit)
    return await contact_check.estimate(state, chosen)
