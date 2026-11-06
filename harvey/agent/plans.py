"""The plans Harvey's agent works under (agent_plan, schema 33).

The agent proposes a plan in a chat: a goal, its steps (short texts), a
budget in USD (0: free work only) and how many hours it stays open once
approved (EXPIRY_HOURS by default, EXPIRY_MAX_HOURS at most). Only the user
approves, rejects or cancels it, on the dashboard's Chat tab or with
``harvey agent plans``: no tool the agent is given can. An approved plan is
cancelled (its live runs with it), expires at its time, or is done when
the user or the agent closes it.

The agent acts only under an approved plan of its own chat, and every
action it takes reserves its caps from the plan's budget first
(``reserve``): the check and the record are one write transaction, so two
actions at once can't both take the last of it. What a plan has committed
is, over its runs: a finished run's actual cost (its searches, address
checks and classifier calls), a live run's caps (its spend cap and its
classifier cap, as they are now), and an action still being queued, the
caps it reserved. The run's own caps then hold it as for any run, and the
install's day and month limits still apply (jobs.reserve). With an API key
(brain.auth: api_key, harvey/brain_spend.py) the agent's own model runs
under the plan count too: a finished wake's or task's model cost (its
job's actual_cost), and a running one's worst case, held when it started
(``hold_run``). In the subscription modes they cost nothing.

Reads and writes go through a writable StateManager: the dashboard's and
the CLI's, or the one inside the agent's tool server (actions.py), never
the read-only connection query() runs on.
"""

from __future__ import annotations

import json
import time

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries

PROPOSED, APPROVED, REJECTED, CANCELLED, EXPIRED, DONE = (
    "proposed", "approved", "rejected", "cancelled", "expired", "done")
STATUSES = (PROPOSED, APPROVED, REJECTED, CANCELLED, EXPIRED, DONE)
EXPIRY_HOURS = 72
EXPIRY_MAX_HOURS = 168
GOAL_CHARS = 500
STEP_CHARS = 200
STEPS_MAX = 12
# What an action row says it ended with (agent_action.outcome).
RESERVED, OK, NOTHING, REFUSED, ERROR = "reserved", "ok", "nothing", "refused", "error"
# Who decided (agent_plan.decided_by / closed_by).
BY_DASHBOARD, BY_CLI, BY_AGENT = "dashboard", "cli", "agent"
EPSILON = 1e-9

_FIELDS = ("id", "chat", "turn", "goal", "steps", "budget_usd", "expiry_hours", "status",
           "created_at", "decided_at", "expires_at", "closed_at", "decided_by", "closed_by")


class Refused(ValueError):
    """The plan can't be proposed, decided or acted under as asked: the
    message says why."""


def _now() -> int:
    return int(time.time())


def _plan(row) -> dict | None:
    if row is None:
        return None
    out = dict(zip(_FIELDS, row))
    try:
        out["steps"] = json.loads(out["steps"] or "[]")
    except ValueError:
        out["steps"] = []
    return out


async def _expire(db) -> None:
    await db.execute(queries.sql("overlay.agent.plan_expire"), (_now(),))


async def _one(db, name: str, args=()):
    async with db.execute(queries.sql(name), args) as cur:
        return await cur.fetchone()


async def _all(db, name: str, args=()):
    async with db.execute(queries.sql(name), args) as cur:
        return await cur.fetchall()


# ── Proposing ──

def checked(goal, steps, budget_usd, expiry_hours) -> tuple[str, list[str], float, int]:
    """A proposal's fields as stored, or Refused saying what is wrong."""
    import math

    goal = " ".join(str(goal or "").split())
    if not goal:
        raise Refused("say the plan's goal")
    if len(goal) > GOAL_CHARS:
        raise Refused(f"a goal is at most {GOAL_CHARS} characters")
    if not isinstance(steps, list) or not steps:
        raise Refused("give the plan's steps: a list of short texts")
    steps = [" ".join(str(s or "").split()) for s in steps]
    steps = [s for s in steps if s]
    if not steps or len(steps) > STEPS_MAX:
        raise Refused(f"a plan has 1 to {STEPS_MAX} steps")
    if any(len(s) > STEP_CHARS for s in steps):
        raise Refused(f"a step is at most {STEP_CHARS} characters")
    try:
        budget = float(budget_usd)
    except (TypeError, ValueError):
        raise Refused("budget_usd is a number of dollars, 0 or more") from None
    if not math.isfinite(budget) or budget < 0:
        raise Refused("budget_usd is a number of dollars, 0 or more")
    try:
        hours = int(EXPIRY_HOURS if expiry_hours in (None, "") else expiry_hours)
    except (TypeError, ValueError):
        raise Refused("expiry_hours is a whole number of hours") from None
    if not 1 <= hours <= EXPIRY_MAX_HOURS:
        raise Refused(f"a plan stays open 1 to {EXPIRY_MAX_HOURS} hours once approved")
    # (In cents: the budget the user is shown, approves and types back is
    # the budget held.)
    return goal, steps, round(budget, 2), hours


async def propose(state, chat: str, turn: str, goal, steps, budget_usd,
                  expiry_hours=None) -> dict:
    """A new plan in ``chat``, proposed while ``turn`` was answered: it
    waits for the user."""
    if not chat:
        raise Refused("a plan is proposed in a chat")
    goal, steps, budget, hours = checked(goal, steps, budget_usd, expiry_hours)
    async with state._connect() as db:
        cur = await db.execute(queries.sql("overlay.agent.plan_add"),
                               (chat, turn or "", goal, json.dumps(steps, ensure_ascii=False),
                                budget, hours, _now()))
        plan_id = cur.lastrowid
        await db.commit()
    return await get(state, plan_id)


# ── Reading ──

async def get(state, plan_id) -> dict | None:
    """One plan (its status as of now), or None."""
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError):
        return None
    async with state._connect() as db:
        await _expire(db)
        await db.commit()
        return _plan(await _one(db, "overlay.agent.plan", (plan_id,)))


async def listed(state, chat: str | None = None, status: str = "", limit: int = 50) -> list[dict]:
    """The plans, newest first: of one chat, or of every chat (of one
    status when it is given)."""
    limit = max(1, min(int(limit or 50), 500))
    async with state._connect() as db:
        await _expire(db)
        await db.commit()
        if chat and status:
            rows = await _all(db, "overlay.agent.plans_chat_status", (chat, status, limit))
        elif chat:
            rows = await _all(db, "overlay.agent.plans_chat", (chat, limit))
        elif status:
            rows = await _all(db, "overlay.agent.plans_status", (status, limit))
        else:
            rows = await _all(db, "overlay.agent.plans", (limit,))
    return [_plan(r) for r in rows]


def _money(rows) -> tuple[float, float, list[dict]]:
    """(committed, spent, the runs) from a plan's plan_runs rows."""
    committed = spent = 0.0
    runs = []
    for (aid, tool, batch, cap, ccap, outcome, at, exists, b_cap, b_ccap, label,
         active, actual) in rows:
        held = float(cap or 0.0) + float(ccap or 0.0)
        actual = float(actual or 0.0)
        if outcome == RESERVED or not batch:
            live, owes = True, held
        elif not exists:
            live, owes = False, 0.0         # (its jobs were all duplicates: dropped)
        elif active:
            now = (float(cap or 0.0) if b_cap is None else float(b_cap)) + \
                  (float(ccap or 0.0) if b_ccap is None else float(b_ccap))
            live, owes = True, max(now, actual)
        else:
            live, owes = False, actual
        committed += owes
        spent += actual
        runs.append({"action": aid, "tool": tool, "batch_id": batch or "", "label": label or "",
                     "live": live, "cap_usd": round(held, 6), "committed_usd": round(owes, 6),
                     "spent_usd": round(actual, 6), "at": at})
    return committed, spent, runs


async def _model_money(db, plan_id: int, exclude: str = "") -> tuple[float, float]:
    """(committed, spent) of a plan's own model runs, its wakes and tasks
    (jobs on the agent lane; other than ``exclude``): a finished one's
    actual cost, a running one's hold (its ``reserved``) when that is more.
    0 in the subscription modes, where they hold and spend nothing."""
    committed = spent = 0.0
    for jid, status, actual, reserved in await _all(
            db, "overlay.agent.plan_model_jobs", (f"agent_wake:{int(plan_id)}", int(plan_id))):
        if jid == exclude:
            continue
        actual = float(actual or 0.0)
        spent += actual
        committed += max(actual, float(reserved or 0.0)) if status == "running" else actual
    return committed, spent


async def money(state, plan_id: int) -> dict:
    """What a plan has committed and spent (``agent_usd``: of it, the
    agent's own model runs), and its runs."""
    async with state._connect() as db:
        rows = await _all(db, "overlay.agent.plan_runs", (int(plan_id),))
        model_committed, model_spent = await _model_money(db, int(plan_id))
    committed, spent, runs = _money(rows)
    return {"committed_usd": round(committed + model_committed, 6),
            "spent_usd": round(spent + model_spent, 6), "agent_usd": round(model_spent, 6),
            "runs": runs}


async def status(state, plan_id, chat: str | None = None) -> dict:
    """A plan with its money (committed, spent, remaining) and runs, or
    Refused when there is none (in ``chat``, when given)."""
    plan = await get(state, plan_id)
    if plan is None or (chat is not None and plan["chat"] != chat):
        raise Refused(f"no plan {plan_id} in this chat")
    m = await money(state, plan["id"])
    return {**plan, **m,
            "remaining_usd": round(max(0.0, plan["budget_usd"] - m["committed_usd"]), 6)}


async def for_chat(state, chat: str, billed: bool = False) -> list[dict]:
    """A chat's plans, oldest first, each with its money and runs (the Chat
    tab's cards), its campaigns (outreach.for_plan: their counts, a start
    asked for), its tasks (tasks.for_plan: each with its result line, and
    their tokens) and an approved one's wakes to come (wakes.pending).
    ``billed`` (the brain on an API key): each says so (its ``agent_usd``
    is then money), and an approved plan with no budget is marked
    ``no_runs``: its wakes and tasks can't hold what they may cost."""
    from harvey.agent import outreach, tasks, wakes

    out = []
    for plan in reversed(await listed(state, chat, limit=100)):
        m = await money(state, plan["id"])
        p = {**plan, **m, "remaining_usd": round(
            max(0.0, plan["budget_usd"] - m["committed_usd"]), 6)}
        if billed:
            p["billed"] = True
            p["no_runs"] = plan["status"] == APPROVED and plan["budget_usd"] <= EPSILON
        if plan["status"] == APPROVED:
            p["wakes"] = await wakes.pending(state, plan["id"])
        if plan["status"] not in (PROPOSED, REJECTED):
            p["campaigns"] = await outreach.for_plan(state, plan["id"])
            found = await tasks.for_plan(state, plan["id"])
            p["tasks"], p["task_tokens"] = found["tasks"], found["tokens"]
        out.append(p)
    return out


# ── The user's decisions ──

async def decide(state, plan_id, approve: bool, by: str, budget_usd=None) -> dict:
    """Approve (open for its hours from now) or reject a proposed plan.
    ``budget_usd``: when given, the budget the user saw, which must be the
    plan's (an approval never takes a budget it didn't show)."""
    plan = await get(state, plan_id)
    if plan is None:
        raise Refused(f"no plan {plan_id}")
    if plan["status"] != PROPOSED:
        raise Refused(f"plan {plan['id']} is {plan['status']}: only a proposed plan is "
                      "approved or rejected")
    if approve and budget_usd is not None:
        try:
            same = round(float(budget_usd), 2) == round(plan["budget_usd"], 2)
        except (TypeError, ValueError):
            same = False
        if not same:
            raise Refused(f"plan {plan['id']}'s budget is ${plan['budget_usd']:.2f}: approve "
                          "it with that budget")
    now = _now()
    to = APPROVED if approve else REJECTED
    expires = now + plan["expiry_hours"] * 3600 if approve else None
    async with state._connect() as db:
        cur = await db.execute(queries.sql("overlay.agent.plan_decide"),
                               (to, now, by, expires, plan["id"]))
        await db.commit()
    if not cur.rowcount:
        raise Refused(f"plan {plan['id']} was decided meanwhile")
    return await get(state, plan["id"])


async def close(state, plan_id, *, cancel: bool, by: str, chat: str | None = None) -> dict:
    """End an approved plan: cancelled (``cancel``: its live runs are
    cancelled too, a search stopping after its page) or done (its runs go
    on to their end under their caps). Either way no action is taken under
    it any more. ``chat``: only a plan of that chat (the agent's own)."""
    plan = await get(state, plan_id)
    if plan is None or (chat is not None and plan["chat"] != chat):
        raise Refused(f"no plan {plan_id}" + (" in this chat" if chat is not None else ""))
    if plan["status"] != APPROVED:
        raise Refused(f"plan {plan['id']} is {plan['status']}: only an approved plan is "
                      + ("cancelled" if cancel else "closed"))
    async with state._connect() as db:
        cur = await db.execute(queries.sql("overlay.agent.plan_close"),
                               (CANCELLED if cancel else DONE, _now(), by, plan["id"]))
        await db.commit()
    if not cur.rowcount:
        raise Refused(f"plan {plan['id']} changed meanwhile")
    from harvey.agent import wakes

    # (Its wakes still to come go with it, and one running is stopped,
    # unless the agent closed the plan itself: from a wake, that wake's
    # last answer still reaches the chat.)
    await wakes.cancel_plan(state, plan["id"], running=by != BY_AGENT)
    out = await get(state, plan["id"])
    out["runs_cancelled"] = 0
    if cancel:
        for run in (await money(state, plan["id"]))["runs"]:
            if run["live"] and run["batch_id"]:
                out["runs_cancelled"] += await cancel_run(state, run["batch_id"])
    return out


async def cancel_run(state, batch_id: str) -> int:
    """Cancel what is still to finish of a run (jobs.cancel each: a running
    search stops after its page, one that can't stop part-way runs to its
    end). How many jobs were cancelled."""
    from harvey.modules import jobs

    n = 0
    for job in await jobs.list_jobs(state, statuses=jobs.ACTIVE, batch_id=batch_id,
                                    limit=100_000):
        try:
            await jobs.cancel(state, job.id)
        except jobs.Refused:
            continue            # finished meanwhile, or can't stop part-way
        n += 1
    return n


# ── Acting under a plan ──

def usable(plan: dict | None, plan_id, chat: str) -> dict:
    """The plan when an action may be taken under it now, else Refused."""
    if plan is None or plan["chat"] != chat:
        raise Refused(f"no plan {plan_id} in this chat: propose one with propose_plan and "
                      "wait for the user to approve it")
    if plan["status"] == PROPOSED:
        raise Refused(f"plan {plan['id']} waits for the user's approval")
    if plan["status"] != APPROVED:
        raise Refused(f"plan {plan['id']} is {plan['status']}: nothing more is done under it; "
                      "propose a new plan if more is needed")
    return plan


async def record(state, plan_id, chat: str, turn: str, tool: str, arguments: dict,
                 outcome: str, why: str = "") -> int:
    """An action row with no money held (a refusal, nothing to do)."""
    try:
        plan_id = int(plan_id) if plan_id not in (None, "") else None
    except (TypeError, ValueError):
        plan_id = None
    async with state._connect() as db:
        cur = await db.execute(queries.sql("overlay.agent.action_add"),
                               (plan_id, chat, turn or "", tool, _args(arguments), None, 0.0,
                                0.0, outcome, why[:1000], _now()))
        await db.commit()
        return cur.lastrowid


async def reserve(state, plan_id, chat: str, turn: str, tool: str, arguments: dict, *,
                  cap: float, classify_cap: float) -> tuple[int, dict]:
    """Hold ``cap`` + ``classify_cap`` of an approved plan's budget for an
    action about to queue its run: (the action's id, the plan with its
    money after it). Refused (and recorded) when the plan can't take it:
    none of this chat's, not approved, or over what remains. A free action
    (both caps 0) fits any approved plan. The check and the record are one
    write transaction."""
    want = float(cap) + float(classify_cap)
    now = _now()
    why = ""
    async with state._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _expire(db)
            try:
                pid = int(plan_id)
            except (TypeError, ValueError):
                pid = None
            plan = _plan(await _one(db, "overlay.agent.plan", (pid,))) if pid else None
            try:
                usable(plan, plan_id, chat)
                committed, _, _ = _money(await _all(db, "overlay.agent.plan_runs", (pid,)))
                committed += (await _model_money(db, pid))[0]
                left = plan["budget_usd"] - committed
                if want > 0 and want > left + EPSILON:
                    raise Refused(f"over plan {pid}'s budget: this asks for ${want:.2f} and "
                                  f"${max(0.0, left):.2f} of ${plan['budget_usd']:.2f} remains")
            except Refused as e:
                why = str(e)
            cur = await db.execute(queries.sql("overlay.agent.action_add"),
                                   (pid if plan else None, chat, turn or "", tool,
                                    _args(arguments), None, 0.0 if why else float(cap),
                                    0.0 if why else float(classify_cap),
                                    REFUSED if why else RESERVED, why, now))
            action = cur.lastrowid
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    if why:
        raise Refused(why)
    return action, await status(state, pid)


async def hold_run(state, plan_id, job_id: str, usd: float, spent: float = 0.0) -> None:
    """Hold ``usd`` of an approved plan's budget for one of its own model
    runs (a wake, a task: job ``job_id``, which spent ``spent`` before, in
    an earlier try) about to start, as its job's ``reserved`` (counted
    while it runs: _model_money). Refused when the
    plan is no longer approved, or what remains of its budget (its runs,
    actions and other runs' holds) is less. The check and the hold are one
    write transaction, as an action's reserve is."""
    async with state._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _expire(db)
            pid = int(plan_id)
            plan = _plan(await _one(db, "overlay.agent.plan", (pid,)))
            if plan is None or plan["status"] != APPROVED:
                raise Refused(f"plan {pid} is {plan['status'] if plan else 'gone'}")
            committed, _, _ = _money(await _all(db, "overlay.agent.plan_runs", (pid,)))
            committed += (await _model_money(db, pid, exclude=job_id))[0] + float(spent)
            left = plan["budget_usd"] - committed
            if float(usd) > left + EPSILON:
                raise Refused(f"plan {pid}'s budget can't cover it: a run may cost up to "
                              f"${float(usd):.2f} and ${max(0.0, left):.2f} of "
                              f"${plan['budget_usd']:.2f} remains")
            await db.execute(queries.sql("overlay.agent.run_hold"),
                             (float(spent) + float(usd), job_id))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def settle(state, action: int, outcome: str, *, batch: str = "", why: str = "") -> None:
    """How a reserved action ended: ok with its run, or refused / error /
    nothing (its caps held no more)."""
    async with state._connect() as db:
        await db.execute(queries.sql("overlay.agent.action_settle"),
                         (batch or None, outcome, why[:1000], int(action)))
        await db.commit()


async def actions(state, plan_id, limit: int = 50) -> list[dict]:
    """A plan's actions, newest first, refusals included."""
    keys = ("id", "tool", "arguments", "batch_id", "cap_usd", "classify_cap_usd", "outcome",
            "why", "at")
    async with state._connect() as db:
        rows = await _all(db, "overlay.agent.actions_plan", (int(plan_id), int(limit)))
    out = []
    for r in rows:
        a = dict(zip(keys, r))
        try:
            a["arguments"] = json.loads(a["arguments"] or "{}")
        except ValueError:
            a["arguments"] = {}
        out.append(a)
    return out


def _args(arguments: dict) -> str:
    from harvey.agent import readonly

    return json.dumps(readonly.mask_result(arguments or {}), ensure_ascii=False,
                      default=str)[:4000]
