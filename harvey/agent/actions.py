"""What Harvey's agent may do besides reading: propose a plan, save a set,
and queue the runs a user's button queues, under a plan the user
approved.

Every action goes through the code the dashboard's button goes through
(harvey/launch.py for the request, harvey/modules/job_kinds.py for the
queue): the same selection of companies or contacts, the same estimate,
the same run caps, the same module switches, and the install's day and
month spend limits, which jobs.reserve keeps as for any run. The plan's
budget is a limit on top (plans.reserve: the run's caps are held from it
before anything is queued).

The campaign actions (outreach.py) are free: an audience saved, a draft
campaign made or edited, a start asked of the user, a campaign paused,
an answer drafted by the Handler and queued for review, an entry added
to the do-not-contact list. No action sends or approves an email,
starts or resumes a campaign, undoes a do-not-contact entry, merges, or
changes a setting: those are never offered as tools.

Actions take a filter (the Contacts and Companies pages' own) or a set the
agent saved (agent_set: a filter, or one read-only SELECT of ids run
through query()'s gate), never a list of rows. A set's ids are resolved
once, when it is saved (refresh_set resolves them again), and kept in
agent_set_member: an action reads those, following a merged firm to its
survivor and skipping what was removed since, and never runs the filter
or the SELECT again. The ids never reach the model: only counts.

This is the one place the agent's tool server writes Harvey's database
(besides the record of its calls): through its own StateManager, reached
only by these functions. query() stays on its read-only connection.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qsl, urlencode

from harvey import queries
from harvey.agent import plans, readonly, views, wakes

SET_KINDS = ("filter", "select")
# What a SELECT set's one column holds: Harvey's firm ids, contact ids, or
# the trade registry's registration numbers (reg.firms.reg).
SET_OF = ("firms", "contacts", "registry")
# What a filter set holds: the firms it selects (as counts() counts them),
# or the contacts it keeps (as the Contacts page lists them).
FILTER_OF = ("firms", "contacts")
SET_NAME_CHARS = 80
SETS_LIMIT = 50
# The most ids a set holds (as many as a Find-contacts run takes): a save or
# a refresh past it is refused with the count, and nothing of it is kept.
SET_MAX = 50_000
# The ids followed (firms) or checked (contacts) a statement, one JSON
# array each: a set's size never meets SQLite's limit on variables.
SET_CHUNK = 5_000


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
                "lookups_usd", "classify", "total_usd", "nothing_why")}
            run["errors"] = await views.error_classes(state, run["batch_id"])
        out["actions"] = [{k: a[k] for k in ("id", "tool", "batch_id", "cap_usd",
                                             "classify_cap_usd", "outcome", "why", "at")}
                          for a in await plans.actions(state, out["id"], limit=20)]
        # (Its wakes to come, and the events not read yet by kind: counts
        # only.)
        pending = await wakes.pending(state, out["id"])
        out["wakes"] = {"scheduled": [{k: w[k] for k in ("id", "due_at", "note")}
                                      for w in pending["scheduled"]],
                        "unseen": pending["unseen"],
                        "queued": bool(pending["job"])}
        for k in ("chat", "turn"):
            out.pop(k, None)
        return out

    # ── Sets ──

    async def save_set(self, name, filter=None, sql: str = "", of: str = "firms") -> dict:
        """A set by a filter or by one SELECT of ids, resolved now and kept
        as ids: its id and how many it holds, nothing else."""
        state = await self.state()
        name = " ".join(str(name or "").split())[:SET_NAME_CHARS]
        if not name:
            raise readonly.Refused("give the set a name")
        if bool(filter) == bool((sql or "").strip()):
            raise readonly.Refused("a set is a filter or a SELECT, one of the two")
        of_ = str(of or "firms")
        if filter:
            if of_ not in FILTER_OF:
                raise readonly.Refused(f"a filter set holds {' or '.join(FILTER_OF)}")
            _, args = await views._filter(state, _filter_args(filter))
            kind, definition = "filter", urlencode(args)
        else:
            if of_ not in SET_OF:
                raise readonly.Refused(f"of is one of {', '.join(SET_OF)}")
            kind, definition = "select", readonly.check_statement(sql)
        ids = await self._members_now(kind, of_, definition)
        now = int(time.time())
        async with state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(queries.sql("overlay.agent.set_add"),
                                       (self.chat, name, kind, of_, definition, len(ids), now,
                                        now))
                set_id = cur.lastrowid
                await db.executemany(queries.sql("overlay.agent.set_member_add"),
                                     [(set_id, v) for v in ids])
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {"set": set_id, "count": len(ids)}

    async def sets(self) -> dict:
        state = await self.state()
        async with state._connect() as db:
            async with db.execute(queries.sql("overlay.agent.sets_chat"),
                                  (self.chat, SETS_LIMIT)) as cur:
                rows = await cur.fetchall()
        return {"sets": [dict(zip(("set", "name", "kind", "of", "count", "created_at",
                                   "frozen_at", "refreshed_at"), r)) for r in rows]}

    async def refresh_set(self, turn: str, plan, set_id) -> dict:
        """A set of this chat resolved again from its filter or SELECT (the
        same bounds as a save), its ids replaced in one transaction, under
        an approved plan of the chat: free, recorded as an action, refusals
        too. Counts only."""
        state = await self.state()
        arguments = {"plan": plan, "set": set_id}
        try:
            plans.usable(await plans.get(state, plan), plan, self.chat)
            s = await self._set(set_id)
            ids = await self._members_now(s["kind"], s["of"], s["definition"])
        except (readonly.Refused, plans.Refused, ValueError, LookupError) as e:
            why = str(e).strip("'\"")
            await plans.record(state, plan, self.chat, turn, "refresh_set", arguments,
                               plans.REFUSED, why)
            raise readonly.Refused(why) from None
        try:
            old = await self._keep(s["id"], ids, refreshed=True)
        except Exception as e:
            await plans.record(state, plan, self.chat, turn, "refresh_set", arguments,
                               plans.ERROR, type(e).__name__)
            raise
        before, after = frozenset(old), frozenset(ids)
        out = {"set": s["id"], "old_count": len(before), "count": len(after),
               "added": len(after - before), "removed": len(before - after)}
        await plans.record(state, plan, self.chat, turn, "refresh_set", arguments, plans.OK,
                           f"{out['old_count']} -> {out['count']}")
        return {"plan": int(plan), **out}

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
        return dict(zip(("id", "chat", "name", "kind", "of", "definition", "n", "created_at",
                         "frozen_at", "refreshed_at"), row))

    async def _members_now(self, kind: str, of: str, definition: str) -> list:
        """The ids a set's filter or SELECT gives now, each once, at most
        SET_MAX (refused past it, with how many)."""
        if kind == "filter":
            f, _ = await views._filter(await self.state(), dict(parse_qsl(definition)))
            ids = await asyncio.to_thread(
                views._with_reader, self.db_path,
                lambda r: r.matching(f) if of == "firms" else r.contact_ids(f))
            ids = list(dict.fromkeys(ids))
            if len(ids) > SET_MAX:
                raise readonly.Refused(f"the set would hold {len(ids):,}, more than "
                                       f"{SET_MAX:,}: narrow it")
            return ids
        return await asyncio.to_thread(_select_values, self.db_path, definition, of, SET_MAX)

    async def _keep(self, set_id: int, ids: list, *, refreshed: bool = False,
                    unfrozen_only: bool = False) -> list:
        """The set's ids replaced by ``ids`` and its count with them, in one
        write transaction; returns the ids it held before. ``unfrozen_only``:
        only while the set is still unfrozen (a set saved before schema 39,
        frozen by its first use; two first uses keep the first's)."""
        state = await self.state()
        now = int(time.time())
        async with state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if unfrozen_only:
                    async with db.execute(queries.sql("overlay.agent.set"), (set_id,)) as cur:
                        row = await cur.fetchone()
                    if row is None or row[8] is not None:
                        await db.rollback()
                        return []
                async with db.execute(queries.sql("overlay.agent.set_members"),
                                      (set_id,)) as cur:
                    old = [r[0] for r in await cur.fetchall()]
                await db.execute(queries.sql("overlay.agent.set_members_clear"), (set_id,))
                await db.executemany(queries.sql("overlay.agent.set_member_add"),
                                     [(set_id, v) for v in ids])
                await db.execute(queries.sql("overlay.agent.set_frozen"),
                                 (len(ids), now, now if refreshed else None, set_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return old

    async def _members(self, s: dict) -> list:
        """The ids set ``s`` holds; an unfrozen one (saved before schema 39)
        is resolved and kept first."""
        if s["frozen_at"] is None:
            ids = await self._members_now(s["kind"], s["of"], s["definition"])
            await self._keep(s["id"], ids, unfrozen_only=True)
        state = await self.state()
        async with state._connect() as db:
            async with db.execute(queries.sql("overlay.agent.set_members"), (s["id"],)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def _follow(self, of: str, members: list) -> tuple[list, int, int]:
        """(the ids an action takes, how many were followed to the firm they
        were merged into, how many were skipped): a firm id to its live
        firm (each once), a contact id while it is on file, a registration
        number as kept (the registry pull resolves it). In pieces of
        SET_CHUNK, each one JSON array."""
        if of == "registry":
            return list(members), 0, 0
        state = await self.state()
        name = "overlay.read.live_of" if of == "firms" else "overlay.agent.contacts_on_file"
        out: dict = {}
        followed = skipped = 0
        async with state._connect() as db:
            for at in range(0, len(members), SET_CHUNK):
                chunk = members[at:at + SET_CHUNK]
                async with db.execute(queries.sql(name), (json.dumps(chunk),)) as cur:
                    rows = await cur.fetchall()
                found = {r[0]: r[-1] for r in rows}
                for m in chunk:
                    live = found.get(m)
                    if live is None:
                        skipped += 1
                        continue
                    followed += live != m
                    out.setdefault(live, None)
        return list(out), followed, skipped

    async def _resolve(self, set_id, filter, takes: str, cap: int) -> dict:
        """What an action selects: {"filter": {...}}, or {"ids": [...], "set":
        its counts} from a set of this chat (its kept ids, followed), at
        most ``cap`` of them."""
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
        if s["kind"] == "filter" and takes == "registry":
            raise readonly.Refused("a registry pull takes the registry's filters or a "
                                   "set of registration numbers (of='registry'), not a "
                                   "Contacts filter")
        if s["of"] != takes:
            hint = (" (save_set with the filter and of='contacts' keeps the contacts it "
                    "selects)" if s["kind"] == "filter" else "")
            raise readonly.Refused(f"this action takes a set of {takes}; set {s['id']} holds "
                                   f"{s['of']}{hint}")
        members = await self._members(s)
        ids, followed, skipped = await self._follow(s["of"], members)
        if len(ids) > cap:
            raise readonly.Refused(f"set {s['id']} holds {len(ids):,}: this action takes at "
                                   f"most {cap:,} (save a narrower set)")
        return {"ids": ids, "set": {"set": s["id"], "held": len(members), "taken": len(ids),
                                    "followed_merge": followed, "skipped_gone": skipped}}

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
                       find_websites: bool = True, classify_max_spend=0.0,
                       areas=None) -> dict:
        """A Discover run over search sources, as the Discover tab queues
        it: its searches (the settings' or ``phrases`` in ``cities``, or in
        ``areas``: boundary unit ids or place names, each searched by its
        shape), its spend cap, its website lookups' amount and its
        classifier cap."""
        from harvey.collectors.discover import PROVIDERS

        keys = [str(k) for k in (sources if isinstance(sources, list) else [sources]) if k]
        body = {"providers": keys, "cities": list(cities or []), "terms": list(phrases or []),
                "areas": list(areas or []),
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
        taken: dict = {}

        async def prepare(state):
            source = next((s["key"] for s in discover_sources() if s["job"] == "registry_pull"),
                          None)
            if source is None:
                raise readonly.Refused("no registry source is installed")
            if set not in (None, ""):
                if filters:
                    raise readonly.Refused("give the registry's filters or a set, not both")
                chosen = await self._resolve(set, None, "registry", REGS_MAX)
                taken.update(chosen["set"])
                f = {"regs": [str(r) for r in chosen["ids"]]}
                if not f["regs"]:
                    return {"plan": int(plan), "queued": 0, "set": chosen["set"],
                            "note": "the set holds no registration number"}
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
        return _with_set(await self._act(turn, "registry_pull", arguments, plan, prepare), taken)

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
        if body.get("areas"):
            try:
                await launch.discovery_areas(state, body)
            except ValueError as e:
                raise readonly.Refused(str(e)) from None
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
        estimate_parts = estimate["sources"]
        estimate = {k: estimate[k] for k in ("query_count", "estimated_cost", "free",
                                             "available", "notes", "errors", "find_websites",
                                             "cells_note")}
        estimate["shapes"] = {p["key"]: {k: p.get(k) for k in ("shape", "cells", "areas")}
                              for p in estimate_parts}
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
        taken: dict = {}

        async def prepare(state):
            cap = _cap("max_spend", max_spend)
            classify_cap = _cap("classify_max_spend", classify_max_spend)
            chosen = await self._resolve(set, filter, "firms", launch.FIND_MAX)
            body = {"force": bool(force), "limit": limit}
            if "ids" in chosen:
                taken.update(chosen["set"])
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
        return _with_set(await self._act(turn, "find_contacts", arguments, plan, prepare), taken)

    async def verify_contacts(self, turn: str, plan, set=None, filter=None, max_spend=0.0,
                              limit=None) -> dict:
        """A verification by the classifier, as POST /api/contacts/verify
        queues it: ``max_spend`` is what its calls may cost, over the whole
        run however many pieces it is queued in."""
        from harvey import classify, contact_check, launch
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_contact_checks

        arguments = {"set": set, "filter": filter or {}, "max_spend": max_spend, "limit": limit}
        taken: dict = {}

        async def prepare(state):
            cap = _cap("max_spend", max_spend)
            if await classify.available(state, "contacts") in ("off", "task off"):
                raise readonly.Refused("no profile answers the classifier's contacts task: the "
                                       "user sets one in Settings → Classifier")
            chosen = await self._resolve(set, filter, "contacts", contact_check.MAX_RUN)
            body = {"limit": limit}
            if "ids" in chosen:
                taken.update(chosen["set"])
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
                "contacts", "links", "companies", "pieces", "more", "calls", "usd_typical",
                "usd_high", "price_known", "note")}, queue
        return _with_set(await self._act(turn, "verify_contacts", arguments, plan, prepare),
                         taken)

    # ── Campaigns (outreach.py): free, through the Campaigns page's code ──

    async def _campaign_act(self, turn: str, tool: str, arguments: dict, plan, work) -> dict:
        """One campaign action under ``plan`` (an approved plan of this
        chat): ``work(state, config)``'s answer, recorded as an action with
        no money held, refusals too."""
        from harvey.agent import outreach
        from harvey.modules.job_kinds import _config

        state = await self.state()
        try:
            plans.usable(await plans.get(state, plan), plan, self.chat)
            config = _config()
            if config is None:
                raise readonly.Refused("Harvey's settings can't be read")
            out = await work(state, config)
        except (outreach.Refused, plans.Refused, readonly.Refused) as e:
            why = str(e).strip("'\"")
            await plans.record(state, plan, self.chat, turn, tool, arguments, plans.REFUSED, why)
            raise readonly.Refused(why) from None
        done = ", ".join(f"{k} {out[k]}" for k in ("audience", "campaign", "conversation",
                                                   "contact", "firm", "outbox", "status")
                         if out.get(k) not in (None, ""))
        await plans.record(state, plan, self.chat, turn, tool, arguments, plans.OK, done)
        return {"plan": int(plan), **out}

    async def save_audience(self, turn: str, plan, name, filter=None, set=None) -> dict:
        """An audience by a Contacts filter, or a filter set, saved as
        "Salvează ca public" saves it, with its preview's counts."""
        from harvey.agent import outreach

        arguments = {"plan": plan, "name": name, "filter": filter or {}, "set": set}

        async def work(state, config):
            if set not in (None, ""):
                if filter:
                    raise readonly.Refused("give a filter or a set, not both")
                s = await self._set(set)
                if s["kind"] != "filter":
                    raise readonly.Refused("an audience is a Contacts filter: save_set with a "
                                           "filter, not a SELECT")
                query = s["definition"]
            else:
                raw = _filter_args(filter or {})
                if not raw:
                    raise readonly.Refused("give a filter (the Contacts page's) or a filter set")
                _, args = await views._filter(state, raw)
                query = urlencode(args)
            return await outreach.save_audience(state, config, name, query)
        return await self._campaign_act(turn, "save_audience", arguments, plan, work)

    async def create_campaign(self, turn: str, plan, name, audience, offer, sequence, note,
                              rolling: bool = False, always_review: bool = False,
                              send_to_risky: bool | None = None) -> dict:
        from harvey.agent import outreach

        arguments = {"plan": plan, "name": name, "audience": audience, "offer": offer,
                     "steps": len(sequence) if isinstance(sequence, list) else 0,
                     "rolling": bool(rolling), "always_review": bool(always_review),
                     "send_to_risky": send_to_risky}
        return await self._campaign_act(turn, "create_campaign", arguments, plan,
                                        lambda state, config: outreach.create(
                                            state, config, int(plan), self.chat, name=name,
                                            audience=audience, offer=offer, sequence=sequence,
                                            note=note, rolling=rolling,
                                            always_review=always_review,
                                            send_to_risky=send_to_risky))

    async def edit_campaign(self, turn: str, plan, campaign, **fields) -> dict:
        from harvey.agent import outreach

        arguments = {"plan": plan, "campaign": campaign,
                     "fields": sorted(k for k, v in fields.items() if v is not None)}
        return await self._campaign_act(turn, "edit_campaign", arguments, plan,
                                        lambda state, config: outreach.edit(
                                            state, int(plan), self.chat, campaign, **fields))

    async def request_start(self, turn: str, plan, campaign) -> dict:
        from harvey.agent import outreach

        return await self._campaign_act(turn, "request_start",
                                        {"plan": plan, "campaign": campaign}, plan,
                                        lambda state, config: outreach.request_start(
                                            state, config, int(plan), self.chat, campaign))

    async def pause_campaign(self, turn: str, plan, campaign) -> dict:
        from harvey.agent import outreach

        return await self._campaign_act(turn, "pause_campaign",
                                        {"plan": plan, "campaign": campaign}, plan,
                                        lambda state, config: outreach.pause(
                                            state, int(plan), self.chat, campaign))

    async def draft_reply(self, turn: str, plan, conversation, guidance) -> dict:
        from harvey.agent import outreach

        arguments = {"plan": plan, "conversation": conversation,
                     "guidance": outreach.guidance_text(guidance)}
        return await self._campaign_act(turn, "draft_reply", arguments, plan,
                                        lambda state, config: outreach.draft_reply(
                                            state, config, int(plan), self.chat, conversation,
                                            guidance))

    async def suppress(self, turn: str, plan, conversation=None, contact=None, firm=None,
                       reason: str = "") -> dict:
        from harvey.agent import outreach

        arguments = {"plan": plan, "conversation": conversation, "contact": contact,
                     "firm": firm, "reason": reason}
        return await self._campaign_act(turn, "suppress", arguments, plan,
                                        lambda state, config: outreach.suppress(
                                            state, config, int(plan), self.chat,
                                            conversation=conversation, contact=contact,
                                            firm=firm, reason=reason))

    async def campaign_read(self, verb: str, campaign=None, plan=None) -> dict:
        """The campaign reads: preview, status, replies (of a campaign, or
        of a plan of this chat's campaigns)."""
        from harvey.agent import outreach
        from harvey.modules.job_kinds import _config

        state = await self.state()
        try:
            if verb == "preview":
                config = _config()
                if config is None:
                    raise readonly.Refused("Harvey's settings can't be read")
                return await outreach.preview(state, config, campaign)
            if verb == "status":
                return await outreach.status(state, campaign)
            if verb == "replies":
                if (campaign in (None, "")) == (plan in (None, "")):
                    raise readonly.Refused("give a campaign or a plan, one of the two")
                # (Whether each could be answered is draft_reply's own
                # checks, which read the settings.)
                config = _config()
                if config is None:
                    raise readonly.Refused("Harvey's settings can't be read")
                if campaign not in (None, ""):
                    await outreach.status(state, campaign)
                    return {"campaign": str(campaign),
                            **await outreach.replies(state, config, [str(campaign)],
                                                     chat=self.chat)}
                p = await plans.status(state, plan, chat=self.chat)
                ids = [link["campaign"] for link in await outreach.of_plan(state, p["id"])]
                return {"plan": p["id"], "campaigns": ids,
                        **await outreach.replies(state, config, ids, chat=self.chat)}
        except (outreach.Refused, plans.Refused) as e:
            raise readonly.Refused(str(e)) from None
        raise ValueError(verb)

    # ── Wakes and memory ──

    async def schedule_wake(self, turn: str, plan, in_hours=None, at=None, note: str = "") -> dict:
        """A wake under an approved plan of this chat (wakes.schedule's
        bounds), recorded as an action, refusals too."""
        state = await self.state()
        arguments = {"plan": plan, "in_hours": in_hours, "at": at, "note": note}
        try:
            out = await wakes.schedule(state, plan, self.chat, in_hours=in_hours, at=at,
                                       note_text=note)
        except wakes.Refused as e:
            await plans.record(state, plan, self.chat, turn, "schedule_wake", arguments,
                               plans.REFUSED, str(e))
            raise readonly.Refused(str(e)) from None
        await plans.record(state, plan, self.chat, turn, "schedule_wake", arguments, plans.OK,
                           f"wake {out['wake']} at {out['due']}")
        return {**out, "note": "the wake comes as a job at its time; the user sees it on the "
                               "plan's card and can cancel it"}

    # ── Tasks (tasks.py) ──

    async def assign_task(self, turn: str, plan, profile, prompt, campaign="") -> dict:
        """A task under an approved plan of this chat (tasks.assign's
        bounds), recorded as an action with no money held, refusals too."""
        from harvey.agent import tasks

        state = await self.state()
        arguments = {"plan": plan, "profile": profile, "prompt": prompt, "campaign": campaign}
        try:
            task = await tasks.assign(state, plan, self.chat, turn, profile, prompt, campaign)
        except tasks.Refused as e:
            await plans.record(state, plan, self.chat, turn, "assign_task", arguments,
                               plans.REFUSED, str(e))
            raise readonly.Refused(str(e)) from None
        await plans.record(state, plan, self.chat, turn, "assign_task", arguments, plans.OK,
                           f"task {task['id']} ({task['profile']})")
        return {"task": task["id"], "plan": task["plan"], "profile": task["profile"],
                "status": task["status"],
                "note": "queued as a job on the agent's lane: when it ends Harvey wakes you "
                        "(a task_done event), and tasks() gives its result"}

    async def tasks(self, plan) -> dict:
        """A plan of this chat's tasks, with their checked results."""
        from harvey.agent import tasks

        state = await self.state()
        try:
            p = await plans.status(state, plan, chat=self.chat)
        except plans.Refused as e:
            raise readonly.Refused(str(e)) from None
        return {"plan": p["id"], **await tasks.for_plan(state, p["id"])}

    async def cancel_task(self, turn: str, plan, task) -> dict:
        from harvey.agent import tasks

        state = await self.state()
        arguments = {"plan": plan, "task": task}
        try:
            plans.usable(await plans.get(state, plan), plan, self.chat)
            done = await tasks.cancel(state, task, plan=plan, chat=self.chat,
                                      why="cancelled by the agent")
        except (plans.Refused, tasks.Refused) as e:
            await plans.record(state, plan, self.chat, turn, "cancel_task", arguments,
                               plans.REFUSED, str(e))
            raise readonly.Refused(str(e)) from None
        await plans.record(state, plan, self.chat, turn, "cancel_task", arguments, plans.OK,
                           f"task {done['id']} cancelled")
        return {"task": done["id"], "status": done["status"]}

    async def memory(self, verb: str, *args) -> dict:
        """The memory tools (memory.py): its refusals said as the tools'."""
        from harvey.agent import memory

        state = await self.state()
        try:
            if verb == "remember":
                n = await memory.remember(state, args[0])
                return {"note": n["id"], **await memory.usage(state)}
            if verb == "forget":
                return {**await memory.forget(state, args[0], by=memory.BY_AGENT),
                        **await memory.usage(state)}
            if verb == "notes":
                return {"notes": [{k: n[k] for k in ("id", "text", "author", "updated_at")}
                                  for n in await memory.notes(state)],
                        **await memory.usage(state)}
            if verb == "save_playbook":
                p = await memory.save_playbook(state, args[0], args[1])
                return {"playbook": p["name"], "chars": len(p["body"]),
                        "limit": memory.PLAYBOOK_CHARS}
            if verb == "read_playbook":
                p = await memory.read_playbook(state, args[0])
                return {k: p[k] for k in ("name", "body", "author", "updated_at")}
            if verb == "playbooks":
                return {"playbooks": [{k: p[k] for k in ("name", "chars", "author", "updated_at")}
                                      for p in await memory.playbooks(state)]}
        except memory.Refused as e:
            raise readonly.Refused(str(e)) from None
        raise ValueError(verb)

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


def _with_set(out: dict, taken: dict) -> dict:
    """An action's answer with the counts of the set it took (held, taken,
    followed to a merge's survivor, skipped as gone), when it took one."""
    if taken and "set" not in out:
        out = {**out, "set": dict(taken)}
    return out


def _filter_args(raw) -> dict:
    """A filter as the Contacts page's arguments: a dict, or its query
    string."""
    if isinstance(raw, str):
        return dict(parse_qsl(raw.lstrip("?")))
    if not isinstance(raw, dict):
        raise readonly.Refused("a filter is the Contacts page's filters, as an object")
    return {k: v for k, v in raw.items() if v is not None and v != ""}


def _select_values(db_path: str, sql: str, of: str, cap: int) -> list:
    """The ids a SELECT set gives now, run through query()'s gate on a
    read-only connection, each once, at most ``cap``: firm or contact ids
    (whole numbers) or registration numbers (text)."""
    con, _ = readonly.connect(db_path)
    try:
        values = readonly.column_values(con, sql, cap=cap)
    finally:
        con.close()
    out: dict = {}
    for v in values:
        if of == "registry":
            if not isinstance(v, str) or not v.strip():
                raise readonly.Refused("a set of='registry' selects registration numbers "
                                       "(reg.firms.reg, text)")
            out.setdefault(v.strip(), None)
            continue
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if not isinstance(v, int) or isinstance(v, bool):
            raise readonly.Refused(f"a set of='{of}' selects {of[:-1]} ids (whole numbers)")
        out.setdefault(v, None)
    return list(out)


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
