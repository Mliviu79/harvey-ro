"""What Harvey's agent may do besides reading: propose a plan, save a set,
and queue the runs a user's button queues, under a plan the user
approved.

Every action goes through the code the dashboard's button goes through
(harvey/launch.py for the request, harvey/modules/job_kinds.py for the
queue): the same selection of companies or contacts, the same estimate,
the same run caps, the same module switches, and the install's day and
month spend limits, which jobs.reserve keeps as for any run. The plan's
budget is a limit on top (plans.reserve: the run's caps are held from it
before anything is queued). No action sends, approves or writes an email,
touches a campaign, the do-not-contact list, a merge, a setting or
anything outside its run: those are never offered as tools.

Actions take a filter (the Contacts and Companies pages' own) or a set the
agent saved (agent_set: a filter, or one read-only SELECT of ids run
through query()'s gate), never a list of rows: a set's ids are resolved
here, at the action, and never returned to the model.

This is the one place the agent's tool server writes Harvey's database
(besides the record of its calls): through its own StateManager, reached
only by these functions. query() stays on its read-only connection.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qsl, urlencode

from harvey import queries
from harvey.agent import plans, readonly, views

SET_KINDS = ("filter", "select")
# What a SELECT set's one column holds: Harvey's firm ids, contact ids, or
# the trade registry's registration numbers (reg.firms.reg).
SET_OF = ("firms", "contacts", "registry")
SET_NAME_CHARS = 80
SETS_LIMIT = 50


def _cap(name: str, value, default: float | None = 0.0) -> float:
    from harvey import launch

    try:
        return launch.money(value, default=default)
    except (TypeError, ValueError):
        raise readonly.Refused(f"{name} is a number of dollars, 0 or more") from None


class Actions:
    """The agent's actions in one chat, on a writable StateManager of their
    own."""

    def __init__(self, db_path: str, chat: str):
        from harvey.state import StateManager

        self.db_path = str(db_path)
        self.chat = str(chat or "")
        self._state = StateManager(self.db_path)
        self._ready = False

    async def state(self):
        if not self._ready:
            await self._state.init_db()
            self._ready = True
        return self._state

    # ── Plans ──

    async def propose_plan(self, turn: str, goal, steps, budget_usd, expiry_hours=None) -> dict:
        state = await self.state()
        try:
            plan = await plans.propose(state, self.chat, turn, goal, steps, budget_usd,
                                       expiry_hours)
        except plans.Refused as e:
            raise readonly.Refused(str(e)) from None
        return {"plan": plan["id"], "status": plan["status"], "budget_usd": plan["budget_usd"],
                "expiry_hours": plan["expiry_hours"],
                "note": "proposed: the user approves or rejects it on the Chat tab (or with "
                        "harvey agent plans); nothing is done under it before that"}

    async def close_plan(self, turn: str, plan) -> dict:
        state = await self.state()
        try:
            done = await plans.close(state, plan, cancel=False, by=plans.BY_AGENT, chat=self.chat)
        except plans.Refused as e:
            await plans.record(state, plan, self.chat, turn, "close_plan", {"plan": plan},
                               plans.REFUSED, str(e))
            raise readonly.Refused(str(e)) from None
        return {"plan": done["id"], "status": done["status"]}

    async def plan_status(self, plan) -> dict:
        """The plan, its money, and each run's report (jobs.batch_report,
        not stored) with its jobs' error classes, never their text."""
        from harvey.modules import jobs

        state = await self.state()
        try:
            out = await plans.status(state, plan, chat=self.chat)
        except plans.Refused as e:
            raise readonly.Refused(str(e)) from None
        for run in out["runs"]:
            if not run["batch_id"] or await jobs.get_batch(state, run["batch_id"]) is None:
                continue
            report = await jobs.batch_report(state, run["batch_id"], keep=False)
            run["report"] = {k: report.get(k) for k in (
                "jobs", "statuses", "seconds", "searches_usd", "checks_made", "checks_usd",
                "lookups_usd", "classify", "total_usd")}
            run["errors"] = await views.error_classes(state, run["batch_id"])
        out["actions"] = [{k: a[k] for k in ("id", "tool", "batch_id", "cap_usd",
                                             "classify_cap_usd", "outcome", "why", "at")}
                          for a in await plans.actions(state, out["id"], limit=20)]
        for k in ("chat", "turn"):
            out.pop(k, None)
        return out

    # ── Sets ──

    async def save_set(self, name, filter=None, sql: str = "", of: str = "firms") -> dict:
        """A set by a filter or by one SELECT of ids: its id and how many it
        holds now, nothing else."""
        state = await self.state()
        name = " ".join(str(name or "").split())[:SET_NAME_CHARS]
        if not name:
            raise readonly.Refused("give the set a name")
        if bool(filter) == bool((sql or "").strip()):
            raise readonly.Refused("a set is a filter or a SELECT, one of the two")
        if filter:
            raw = _filter_args(filter)
            f, args = await views._filter(state, raw)
            n = (await asyncio.to_thread(views._with_reader, self.db_path,
                                         lambda r: r.firm_counts(f)))["firms"]
            kind, of_, definition = "filter", "firms", urlencode(args)
        else:
            of_ = str(of or "firms")
            if of_ not in SET_OF:
                raise readonly.Refused(f"of is one of {', '.join(SET_OF)}")
            values = await asyncio.to_thread(_select_values, self.db_path, sql, of_)
            kind, definition, n = "select", readonly.check_statement(sql), len(values)
        async with state._connect() as db:
            cur = await db.execute(queries.sql("overlay.agent.set_add"),
                                   (self.chat, name, kind, of_, definition, n, int(time.time())))
            set_id = cur.lastrowid
            await db.commit()
        return {"set": set_id, "count": n}

    async def sets(self) -> dict:
        state = await self.state()
        async with state._connect() as db:
            async with db.execute(queries.sql("overlay.agent.sets_chat"),
                                  (self.chat, SETS_LIMIT)) as cur:
                rows = await cur.fetchall()
        return {"sets": [dict(zip(("set", "name", "kind", "of", "count", "created_at"), r))
                         for r in rows]}

    async def _set(self, set_id) -> dict:
        state = await self.state()
        try:
            sid = int(set_id)
        except (TypeError, ValueError):
            raise readonly.Refused(f"no set {set_id!r}: sets() lists them") from None
        async with state._connect() as db:
            async with db.execute(queries.sql("overlay.agent.set"), (sid,)) as cur:
                row = await cur.fetchone()
        if row is None or row[1] != self.chat:
            raise readonly.Refused(f"no set {set_id} in this chat: sets() lists them")
        return dict(zip(("id", "chat", "name", "kind", "of", "definition", "n", "created_at"),
                        row))

    async def _resolve(self, set_id, filter, takes: str, cap: int) -> dict:
        """What an action selects: {"filter": {...}} or {"ids": [...]} from
        a set of this chat (resolved now) or a filter."""
        if set_id not in (None, "") and filter:
            raise readonly.Refused("give a set or a filter, not both")
        if set_id in (None, ""):
            raw = _filter_args(filter or {})
            if not raw:
                raise readonly.Refused("give a set (save_set) or a filter: with neither it "
                                       "would be everything")
            # (The arguments as counts() reads them, a list joined by commas:
            # the run selects what the count counted.)
            _, args = await views._filter(await self.state(), raw)
            return {"filter": args}
        s = await self._set(set_id)
        if s["kind"] == "filter":
            if takes == "registry":
                raise readonly.Refused("a registry pull takes the registry's filters or a "
                                       "set of registration numbers (of='registry'), not a "
                                       "Contacts filter")
            return {"filter": dict(parse_qsl(s["definition"]))}
        if s["of"] != takes:
            raise readonly.Refused(f"this action takes a set of {takes}; set {s['id']} holds "
                                   f"{s['of']}")
        values = await asyncio.to_thread(_select_values, self.db_path, s["definition"],
                                         s["of"], cap)
        return {"ids": values}

    # ── Runs ──

    async def _act(self, turn: str, tool: str, arguments: dict, plan, prepare):
        """One action under ``plan``: ``prepare()`` reads the request the
        way the button's endpoint does and returns (caps, estimate, queue,
        or a dict to answer with when there is nothing to queue); its caps
        are reserved from the plan, then ``queue()`` enqueues the run.
        Every refusal is recorded."""
        from harvey.modules import jobs, switches

        state = await self.state()

        async def refuse(why: str):
            await plans.record(state, plan, self.chat, turn, tool, arguments, plans.REFUSED, why)
            raise readonly.Refused(why)

        try:
            plans.usable(await plans.get(state, plan), plan, self.chat)
            prepared = await prepare(state)
        except (readonly.Refused, plans.Refused, switches.ModuleOff, ValueError,
                LookupError) as e:
            await refuse(str(e).strip("'\""))
        if isinstance(prepared, dict):
            await plans.record(state, plan, self.chat, turn, tool, arguments, plans.NOTHING,
                               prepared.get("note", ""))
            return prepared
        (cap, classify_cap), estimate, queue = prepared

        async def reserve_and_queue():
            try:
                action, _ = await plans.reserve(state, plan, self.chat, turn, tool, arguments,
                                                cap=cap, classify_cap=classify_cap)
            except plans.Refused as e:
                raise readonly.Refused(str(e)) from None
            try:
                batch_id, ids, dupes = await queue(state)
            except jobs.Duplicate as e:
                why = (f"the same work is already waiting (job {e.job.id}, {e.job.status}): "
                       "follow it with plan_status or jobs()")
                await plans.settle(state, action, plans.REFUSED, why=why)
                raise readonly.Refused(why) from None
            except Exception as e:
                await plans.settle(state, action, plans.ERROR, why=type(e).__name__)
                raise
            await plans.settle(state, action, plans.OK if ids else plans.NOTHING, batch=batch_id)
            # (The user may have cancelled the plan while this run was being
            # queued: the cancel couldn't see its batch yet, so it goes now.)
            if ids and (await plans.get(state, plan))["status"] == plans.CANCELLED:
                await plans.cancel_run(state, batch_id)
                raise readonly.Refused(f"plan {plan} was cancelled meanwhile: its run is "
                                       "cancelled too")
            return batch_id, ids, dupes

        # (Shielded: a call stopped mid-way, its answer stopped by the user,
        # still records the run it queued against the plan, so its caps
        # are never forgotten while it runs.)
        task = asyncio.ensure_future(reserve_and_queue())
        # (Its refusal is read even when no one waits for it any more.)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        batch_id, ids, dupes = await asyncio.shield(task)
        after = await plans.status(state, plan)
        return {"plan": int(plan), "run": batch_id, "queued": len(ids),
                "already_waiting": len(dupes),
                "caps": {"max_spend": round(cap, 6), "classify_max_spend": round(classify_cap, 6)},
                "estimate": estimate, "remaining_usd": after["remaining_usd"],
                "note": "the run's caps are what it may spend; follow it with plan_status"}

    async def discover(self, turn: str, plan, sources, phrases=None, cities=None,
                       preset: str = "", depth=None, max_spend=0.0, websites_max_spend=0.0,
                       find_websites: bool = True, classify_max_spend=0.0) -> dict:
        """A Discover run over search sources, as the Discover tab queues
        it: its searches (the settings' or ``phrases`` in ``cities``), its
        spend cap, its website lookups' amount and its classifier cap."""
        from harvey.collectors.discover import PROVIDERS

        keys = [str(k) for k in (sources if isinstance(sources, list) else [sources]) if k]
        body = {"providers": keys, "cities": list(cities or []), "terms": list(phrases or []),
                "preset": preset or "", "depth": depth, "max_spend": max_spend,
                "find_websites": bool(find_websites), "websites_max_spend": websites_max_spend,
                "classify_max_spend": classify_max_spend}
        arguments = {k: v for k, v in body.items() if v not in (None, "", [])}

        async def prepare(state):
            if not keys:
                raise readonly.Refused("name the sources (estimate's discover lists them)")
            for k in keys:
                if k not in PROVIDERS:
                    raise readonly.Refused(f"{k} isn't a search source: the registry is "
                                           "registry_pull")
            return await self._discovery(state, body)
        return await self._act(turn, "discover", arguments, plan, prepare)

    async def registry_pull(self, turn: str, plan, filters=None, set=None,
                            websites_max_spend=0.0, classify_max_spend=0.0) -> dict:
        """A registry pull, as the Registry page and the Discover tab queue
        it: by the registry source's filters, or a set of registration
        numbers (a SELECT set of='registry')."""
        from harvey.modules.job_kinds import REGS_MAX, discover_sources

        arguments = {"filters": filters or {}, "set": set,
                     "websites_max_spend": websites_max_spend,
                     "classify_max_spend": classify_max_spend}

        async def prepare(state):
            source = next((s["key"] for s in discover_sources() if s["job"] == "registry_pull"),
                          None)
            if source is None:
                raise readonly.Refused("no registry source is installed")
            if set not in (None, ""):
                if filters:
                    raise readonly.Refused("give the registry's filters or a set, not both")
                chosen = await self._resolve(set, None, "registry", REGS_MAX)
                f = {"regs": [str(r) for r in chosen["ids"]]}
                if not f["regs"]:
                    return {"plan": int(plan), "queued": 0,
                            "note": "the set holds no registration number now"}
            elif isinstance(filters, dict) and filters:
                f = dict(filters)
                f.pop("regs", None)
            else:
                raise readonly.Refused("give the registry's filters (words, county, "
                                       "locality, ...) or a set of registration numbers")
            body = {"providers": [source], "filters": {source: f}, "max_spend": 0,
                    "find_websites": True, "websites_max_spend": websites_max_spend,
                    "classify_max_spend": classify_max_spend}
            return await self._discovery(state, body)
        return await self._act(turn, "registry_pull", arguments, plan, prepare)

    async def _discovery(self, state, body: dict):
        """The Discover run's request read as /api/discover/run reads it
        (launch: the module switches, the sources' plan, the amounts), with
        the estimate shown before it (never a paid count)."""
        from harvey import launch
        from harvey.modules.job_kinds import _config, enqueue_sources

        refusal = await launch.discovery_refusal(state, body)
        if refusal:
            raise readonly.Refused(str(refusal))
        if await state.get_setting("discovery_paused"):
            raise readonly.Refused("Discover is paused: the user resumes it on the Discover tab")
        config = _config()
        if config is None:
            raise readonly.Refused("Harvey's settings can't be read")
        source_plan, notes = launch.discovery_plan(body, config)
        max_spend = _cap("max_spend", body.get("max_spend"))
        try:
            websites = launch.find_websites(body)
        except (TypeError, ValueError):
            raise readonly.Refused("websites_max_spend is a number of dollars, 0 or more") \
                from None
        classify_cap = _cap("classify_max_spend", body.get("classify_max_spend"))
        estimate = await launch.discovery_estimate(state, config, source_plan, notes, websites,
                                                   count=False)
        estimate = {k: estimate[k] for k in ("query_count", "estimated_cost", "free",
                                             "available", "notes", "errors", "find_websites")}
        estimate["queries"] = [q for p in source_plan for q in (
            [x.keyword() for x in p["queries"]] if p.get("provider") else [p["label"]])][:20]

        async def queue(state):
            return await enqueue_sources(
                state, source_plan, max_spend=max_spend, find_websites=websites,
                classify_max_spend=classify_cap,
                label=" + ".join(p_.get("name") or p_["label"] for p_ in source_plan))
        return (max_spend + (websites or 0.0), classify_cap), estimate, queue

    async def find_contacts(self, turn: str, plan, set=None, filter=None, max_spend=0.0,
                            classify_max_spend=0.0, force: bool = False, limit=None) -> dict:
        """A Find-contacts run, as POST /api/contacts/find queues it."""
        from harvey import launch
        from harvey.modules.job_kinds import enqueue_contact_search

        arguments = {"set": set, "filter": filter or {}, "max_spend": max_spend,
                     "classify_max_spend": classify_max_spend, "force": bool(force),
                     "limit": limit}

        async def prepare(state):
            cap = _cap("max_spend", max_spend)
            classify_cap = _cap("classify_max_spend", classify_max_spend)
            chosen = await self._resolve(set, filter, "firms", launch.FIND_MAX)
            body = {"force": bool(force), "limit": limit}
            if "ids" in chosen:
                body.update(filter={}, company_ids=[str(i) for i in chosen["ids"]])
            else:
                body["filter"] = chosen["filter"]
            selected, error = await launch.find_selection(state, body)
            if error:
                raise readonly.Refused(error)
            if not selected:
                return {"plan": int(plan), "queued": 0, "companies": 0,
                        "note": "no company to search: each has a contact already (force "
                                "searches them again)"}
            estimate = await launch.find_estimate(state, selected, bool(force))

            async def queue(state):
                return await enqueue_contact_search(
                    state, [c["id"] for c in selected], max_spend=cap, force=bool(force),
                    classify_max_spend=classify_cap)
            return (cap, classify_cap), _find_summary(estimate), queue
        return await self._act(turn, "find_contacts", arguments, plan, prepare)

    async def verify_contacts(self, turn: str, plan, set=None, filter=None, max_spend=0.0,
                              limit=None) -> dict:
        """A verification by the classifier, as POST /api/contacts/verify
        queues it: ``max_spend`` is what its calls may cost."""
        from harvey import classify, contact_check, launch
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_contact_checks

        arguments = {"set": set, "filter": filter or {}, "max_spend": max_spend, "limit": limit}

        async def prepare(state):
            cap = _cap("max_spend", max_spend)
            if await classify.available(state, "contacts") in ("off", "task off"):
                raise readonly.Refused("no profile answers the classifier's contacts task: the "
                                       "user sets one in Settings → Classifier")
            chosen = await self._resolve(set, filter, "contacts", contact_check.MAX_LINKS)
            body = {"limit": limit}
            if "ids" in chosen:
                body["contact_ids"] = [int(i) for i in chosen["ids"]]
            else:
                body["filter"] = chosen["filter"]
            links, error = await launch.verify_selection(state, body)
            if error:
                raise readonly.Refused(error)
            if not links:
                return {"plan": int(plan), "queued": 0, "links": 0,
                        "note": "no contact linked to a company in it"}
            estimate = await contact_check.estimate(state, links)

            async def queue(state):
                out = await enqueue_contact_checks(state, links, max_spend=cap)
                jobs.notify()
                return out
            return (0.0, cap), {k: estimate.get(k) for k in (
                "links", "companies", "calls", "usd_typical", "usd_high", "price_known",
                "note")}, queue
        return await self._act(turn, "verify_contacts", arguments, plan, prepare)

    async def cancel_run(self, turn: str, plan, run: str) -> dict:
        """Cancel what is still to finish of one of the plan's runs."""
        state = await self.state()
        arguments = {"plan": plan, "run": run}
        try:
            status = await plans.status(state, plan, chat=self.chat)
        except plans.Refused as e:
            await plans.record(state, plan, self.chat, turn, "cancel_run", arguments,
                               plans.REFUSED, str(e))
            raise readonly.Refused(str(e)) from None
        if not run or run not in {r["batch_id"] for r in status["runs"]}:
            why = f"no run {run!r} under plan {status['id']}"
            await plans.record(state, plan, self.chat, turn, "cancel_run", arguments,
                               plans.REFUSED, why)
            raise readonly.Refused(why)
        n = await plans.cancel_run(state, run)
        await plans.record(state, plan, self.chat, turn, "cancel_run", arguments, plans.OK,
                           f"{n} job(s) cancelled")
        return {"run": run, "cancelled_jobs": n}


def _filter_args(raw) -> dict:
    """A filter as the Contacts page's arguments: a dict, or its query
    string."""
    if isinstance(raw, str):
        return dict(parse_qsl(raw.lstrip("?")))
    if not isinstance(raw, dict):
        raise readonly.Refused("a filter is the Contacts page's filters, as an object")
    return {k: v for k, v in raw.items() if v is not None and v != ""}


def _select_values(db_path: str, sql: str, of: str, cap: int | None = None) -> list:
    """The ids a SELECT set holds now, run through query()'s gate on a
    read-only connection: firm or contact ids (whole numbers) or
    registration numbers (text)."""
    from harvey import contact_check, launch
    from harvey.modules.job_kinds import REGS_MAX

    cap = cap or {"firms": launch.FIND_MAX, "contacts": contact_check.MAX_LINKS,
                  "registry": REGS_MAX}[of]
    con, _ = readonly.connect(db_path)
    try:
        values = readonly.column_values(con, sql, cap=cap)
    finally:
        con.close()
    out = []
    for v in values:
        if of == "registry":
            if not isinstance(v, str) or not v.strip():
                raise readonly.Refused("a set of='registry' selects registration numbers "
                                       "(reg.firms.reg, text)")
            out.append(v.strip())
            continue
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if not isinstance(v, int) or isinstance(v, bool):
            raise readonly.Refused(f"a set of='{of}' selects {of[:-1]} ids (whole numbers)")
        out.append(v)
    return out


def _find_summary(e: dict) -> dict:
    """A Find-contacts estimate, short: its counts, the searches' cost and
    most, the address checks' and the classifier's high."""
    checks = e.get("checks") or {}
    cls = e.get("classifier") or {}
    return {"companies": e.get("companies"), "with_site": e.get("with_site"),
            "to_look_up": e.get("to_look_up"), "searches_usd": e.get("estimated_cost"),
            "searches_most_usd": e.get("most"), "more": e.get("more"),
            "checks": {k: checks.get(k) for k in ("checks", "lookups", "usd_high", "refused",
                                                   "checks_left") if k in checks},
            "classifier": {k: cls.get(k) for k in ("calls", "usd_typical", "usd_high",
                                                   "price_known", "note") if k in cls}}
