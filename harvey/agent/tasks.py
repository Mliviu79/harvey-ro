"""Tasks Harvey's agent hands to a child under an approved plan
(agent_task, schema 36).

The agent (in a chat, or on a wake) can delegate a bounded piece of work
with ``assign_task``: a broad research question over many areas or
phrases, or the check of one of the plan's campaign drafts. The child is
a job (kind ``agent_task`` on the agent's lane, like a wake), never a
chat: one ``claude -p`` run whose MCP server serves only its profile's
tools (``--task``: registered by the profile, and checked again on every
call), given the profile's brief and the task's prompt, the memory to
read, and no chat history. Its final answer is one JSON object, checked
against the profile's output: valid, the task is done with it; else it
is invalid, its answer kept (masked, bounded) with the problem. Either
way (and when it failed), a ``task_done`` event wakes the plan, and the
agent reads the result with ``tasks``.

A child can't spend, send, schedule, plan, delegate or remember: the
profiles never hold those tools (FORBIDDEN), and the server refuses any
tool outside its profile's even when it is asked for by name. A
campaign_driver works on the one campaign it was given, a draft of the
task's plan, and changes only its sequence (DRIVER_EDITS): its audience,
offer, note and the rest stay what the agent chose, and anything wrong
there goes back in its answer's to_do.

Bounds: at most OPEN_MAX tasks of a plan queued or running, PER_DAY
assigned in 24 hours, TASK_TURNS turns and TIMEOUT_SECONDS a run.
Closing or cancelling the plan, or deleting its chat, cancels its tasks
(a running one is stopped, like a wake). A task runs on the agent's
driver (driver.py: Claude, or Codex when brain.provider says so). Its tokens go to usage_events (agent ``agent``, task
``task.<profile>``) and to the task's row, which the plan card counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import brain_spend, queries

logger = logging.getLogger("harvey.agent")

TASK_KIND = "agent_task"
TASK_DONE = "task_done"
QUEUED, RUNNING, DONE, FAILED, INVALID, CANCELLED = (
    "queued", "running", "done", "failed", "invalid", "cancelled")
OPEN = (QUEUED, RUNNING)
OPEN_MAX = 2
PER_DAY = 6
PROMPT_CHARS = 2000
# A child's turns and time: less than a wake's.
TASK_TURNS = 8
TIMEOUT_SECONDS = 300
# What an invalid answer keeps of itself.
RAW_CHARS = 4000
LIST_LIMIT = 20
# The tools no child is ever given: delegating, scheduling, plans, every
# action that spends or queues a run, a campaign's start, pause or
# creation, answers and the do-not-contact list, memory writes, sets and
# audiences.
FORBIDDEN = frozenset({
    "assign_task", "tasks", "cancel_task", "schedule_wake", "propose_plan", "close_plan",
    "discover", "registry_pull", "find_contacts", "verify_contacts", "cancel_run",
    "create_campaign", "request_start", "pause_campaign", "suppress", "draft_reply",
    "remember", "forget", "save_playbook", "save_set", "refresh_set", "save_audience"})


# ── The output a profile's answer is checked against ──
# A small spec, checked by hand (no schema library is a dependency):
# {"type": "object", "fields": {...}, "required": [...]} (no other key),
# {"type": "array", "items": spec, "max": n}, {"type": "string", "max": n},
# {"type": "number"}, {"type": "boolean"}.

def _s(n: int) -> dict:
    return {"type": "string", "max": n}


def _list(items: dict, n: int) -> dict:
    return {"type": "array", "items": items, "max": n}


def _obj(fields: dict, required=None) -> dict:
    return {"type": "object", "fields": fields,
            "required": list(fields) if required is None else list(required)}


_NUMBER = {"type": "number"}
_BOOL = {"type": "boolean"}


def wrong_with(value, spec: dict, path: str = "answer") -> str:
    """What is wrong with ``value`` against ``spec`` ("" when nothing)."""
    kind = spec["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} is an object"
        extra = sorted(set(value) - set(spec["fields"]))
        if extra:
            return f"{path} has no field {extra[0][:40]!r}"
        for key in spec["required"]:
            if key not in value:
                return f"{path}.{key} is missing"
        for key, sub in spec["fields"].items():
            if key in value:
                found = wrong_with(value[key], sub, f"{path}.{key}")
                if found:
                    return found
        return ""
    if kind == "array":
        if not isinstance(value, list):
            return f"{path} is a list"
        if len(value) > spec["max"]:
            return f"{path} holds at most {spec['max']}"
        for i, item in enumerate(value):
            found = wrong_with(item, spec["items"], f"{path}[{i}]")
            if found:
                return found
        return ""
    if kind == "string":
        if not isinstance(value, str):
            return f"{path} is a text"
        if len(value) > spec["max"]:
            return f"{path} is at most {spec['max']} characters"
        return ""
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            return f"{path} is a number"
        return ""
    if kind == "boolean":
        return "" if isinstance(value, bool) else f"{path} is true or false"
    raise ValueError(kind)


def shape(spec: dict):
    """The spec as an example the prompt shows (each text's limit named)."""
    kind = spec["type"]
    if kind == "object":
        return {k + ("" if k in spec["required"] else " (optional)"): shape(v)
                for k, v in spec["fields"].items()}
    if kind == "array":
        return [shape(spec["items"]), f"... at most {spec['max']}"]
    if kind == "string":
        return f"text, at most {spec['max']} characters"
    return kind


# ── Profiles ──

@dataclass(frozen=True)
class Profile:
    name: str
    tools: frozenset
    output: dict
    # What the child is told it does, and how to answer.
    brief: str
    # Whether it works on one campaign of the plan.
    campaign: bool = False


PROFILES: dict[str, Profile] = {}


def register(profile: Profile) -> Profile:
    held = profile.tools & FORBIDDEN
    if held:
        raise ValueError(f"profile {profile.name} can't hold {sorted(held)}")
    PROFILES[profile.name] = profile
    return profile


_READ_MEMORY = frozenset({"notes", "playbooks", "read_playbook"})
# The tools a campaign_driver calls on its own campaign only.
CAMPAIGN_TOOLS = frozenset({"campaign_preview", "campaign_status", "replies", "edit_campaign"})
# What a campaign_driver's edit_campaign may change: the emails it was
# given to check, never the course the agent chose (audience, offer, note).
DRIVER_EDITS = frozenset({"sequence"})

register(Profile(
    "researcher",
    frozenset({"schema", "counts", "firms", "coverage", "query", "estimate", "jobs", "areas",
               "plan_cells", "terms", "phrase_yield", "sets"}) | _READ_MEMORY,
    _obj({"areas": _list(_obj({"area": _s(120), "why": _s(300)}, ["area"]), 12),
          "phrases": _list(_s(80), 12),
          "sources": _list(_s(40), 8),
          "estimate": _obj({"summary": _s(400), "usd_typical": _NUMBER, "usd_high": _NUMBER},
                           ["summary"]),
          "notes": _s(1500)}),
    "You research where and what to search for the plan's goal: which areas (places or "
    "boundary unit ids, from areas()), which search phrases (terms(), phrase_yield()), which "
    "sources, and what a run would cost (estimate(), plan_cells()), from counts and the "
    "schema. You propose; the agent that asked decides and acts. Keep each list to what is "
    "worth doing, best first."))

register(Profile(
    "campaign_driver",
    frozenset({"schema", "counts", "campaigns", "writing_rules", "campaign_preview",
               "campaign_status", "replies", "edit_campaign"}) | _READ_MEMORY,
    _obj({"check": _obj({"ok": _BOOL, "problems": _list(_s(300), 10)}),
          "edits": _list(_s(300), 10),
          "to_do": _list(_s(300), 10),
          "notes": _s(1500)}, ["check", "edits", "to_do"]),
    "You check one draft campaign of the plan against writing_rules(): its sequence (the "
    "merge variables, the limits, the banned phrases, the language), its legitimate-interest "
    "note and what campaign_preview() says it would take. Fix what is wrong in its emails "
    "with edit_campaign(sequence=...) (this campaign only, while it is a draft; the "
    "sequence is the only thing you may change). Anything wrong in its audience, offer or "
    "note goes in to_do for the agent to decide, never changed by you. Then say what the "
    "check found (check.ok, its problems), the edits you made, and what the start still "
    "needs (to_do). You never start, pause or send anything.",
    campaign=True))


# ── Reading ──

_FIELDS = ("id", "plan", "chat", "parent_turn", "turn", "profile", "campaign", "prompt",
           "output", "status", "attempts", "result_json", "problem", "created_at",
           "started_at", "finished_at", "tokens", "job")


class Refused(ValueError):
    """A task can't be assigned or cancelled as asked: the message says
    why."""


def _now() -> int:
    return int(time.time())


async def _one(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchone()


async def _all(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchall()


async def _write(state, name: str, args=()) -> int:
    async with state._connect() as db:
        cur = await db.execute(queries.sql(name), args)
        await db.commit()
        return cur.rowcount


def _task(row) -> dict | None:
    if row is None:
        return None
    out = dict(zip(_FIELDS, row))
    try:
        out["result"] = json.loads(out.pop("result_json") or "null")
    except ValueError:
        out["result"] = None
    return out


async def get(state, task_id) -> dict | None:
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None
    return _task(await _one(state, "overlay.agent.task", (tid,)))


def task_sync(con, task_id) -> dict | None:
    """One task on a caller's sqlite3 connection (the tool server's check
    of every call)."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None
    return _task(con.execute(queries.sql("overlay.agent.task"), (tid,)).fetchone())


async def _settled(state, task: dict) -> dict:
    """A task still open whose job ended where no worker saw it (cancelled
    on the Queue page while queued, failed past its tries) ends with it."""
    if task["status"] not in OPEN or not task["job"]:
        return task
    from harvey.modules import jobs

    job = await jobs.get(state, task["job"])
    if job is not None and job.status in jobs.FINISHED:
        await job_ended(state, job)
        return await get(state, task["id"]) or task
    return task


async def listed(state, plan_id=None, limit: int = LIST_LIMIT) -> list[dict]:
    """A plan's tasks (or every plan's), newest first."""
    limit = max(1, min(int(limit or LIST_LIMIT), 200))
    rows = (await _all(state, "overlay.agent.tasks_plan", (int(plan_id), limit))
            if plan_id not in (None, "") else await _all(state, "overlay.agent.tasks", (limit,)))
    return [await _settled(state, _task(r)) for r in rows]


def counted(task: dict) -> dict:
    """What a done task gave, counted (the plan card says it in its own
    words): a researcher's areas, phrases and sources; a campaign_driver's
    check, problems, edits and what the start still needs."""
    r = task.get("result") or {}
    if task["status"] != DONE or not isinstance(r, dict):
        return {}
    if task["profile"] == "researcher":
        return {k: len(r.get(k) or []) for k in ("areas", "phrases", "sources")}
    check = r.get("check") or {}
    return {"ok": bool(check.get("ok")), "problems": len(check.get("problems") or []),
            "edits": len(r.get("edits") or []), "to_do": len(r.get("to_do") or [])}


def summary(task: dict) -> str:
    """A short line of what a task gave (the CLI)."""
    c = counted(task)
    if not c:
        return task.get("problem") or ""
    if "areas" in c:
        return f"{c['areas']} areas, {c['phrases']} phrases, {c['sources']} sources"
    return (("ok" if c["ok"] else f"{c['problems']} problems")
            + f", {c['edits']} edits, {c['to_do']} to do")


def public(task: dict) -> dict:
    """A task as the agent's tasks() and the plan card show it."""
    return {"task": task["id"], "plan": task["plan"], "profile": task["profile"],
            "campaign": task["campaign"], "status": task["status"],
            "attempts": task["attempts"], "result": task["result"],
            "problem": task["problem"], "counts": counted(task), "summary": summary(task),
            "tokens": task["tokens"],
            "created_at": task["created_at"], "finished_at": task["finished_at"],
            "job": task["job"]}


async def for_plan(state, plan_id) -> dict:
    """The plan card's tasks: each one (public) and their tokens."""
    found = [public(t) for t in await listed(state, plan_id)]
    return {"tasks": found, "tokens": sum(t["tokens"] or 0 for t in found)}


# ── Assigning ──

def _masked(value):
    """Every text of ``value`` masked as memory is (memory.masked)."""
    from harvey.agent import memory

    if isinstance(value, str):
        return memory.masked(value)
    if isinstance(value, list):
        return [_masked(v) for v in value]
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    return value


async def assign(state, plan_id, chat: str, parent_turn: str, profile: str, prompt: str,
                 campaign: str = "") -> dict:
    """A task under an approved plan of ``chat``, queued as an agent_task
    job: the task, or Refused (no such plan, Codex where it can't run, a
    profile that isn't,
    a campaign that isn't the plan's draft, an empty or long prompt, the
    plan's tasks at their caps)."""
    from harvey.agent import chat as chats, memory, outreach, plans
    from harvey.modules import jobs

    plan = await plans.get(state, plan_id)
    try:
        plans.usable(plan, plan_id, chat)
    except plans.Refused as e:
        raise Refused(str(e)) from None
    why = chats.refusal(chats._config())
    if why:
        raise Refused("tasks can't run: " + why)
    p = PROFILES.get(str(profile or ""))
    if p is None:
        raise Refused(f"no profile {profile!r}: one of {', '.join(sorted(PROFILES))}")
    text = memory.masked(" ".join(str(prompt or "").split()))
    if not text:
        raise Refused("say what the task is (prompt)")
    if len(text) > PROMPT_CHARS:
        raise Refused(f"a task's prompt is at most {PROMPT_CHARS:,} characters "
                      f"(this one is {len(text):,})")
    campaign = str(campaign or "").strip()
    if p.campaign:
        if not campaign:
            raise Refused(f"a {p.name} task works on one campaign: give its id")
        try:
            await outreach._own(state, plan["id"], chat, campaign, draft=True)
        except outreach.Refused as e:
            raise Refused(str(e)) from None
    elif campaign:
        raise Refused(f"a {p.name} task takes no campaign")
    now = _now()
    async with state._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(queries.sql("overlay.agent.tasks_open"), (plan["id"],)) as cur:
                running = (await cur.fetchone())[0]
            async with db.execute(queries.sql("overlay.agent.tasks_since"),
                                  (plan["id"], now - 86400)) as cur:
                today = (await cur.fetchone())[0]
            if running >= OPEN_MAX:
                raise Refused(f"plan {plan['id']} has {OPEN_MAX} tasks queued or running: "
                              "that is the most; tasks() says when they finish")
            if today >= PER_DAY:
                raise Refused(f"plan {plan['id']} had {PER_DAY} tasks in the last 24 hours: "
                              "that is the most")
            cur = await db.execute(queries.sql("overlay.agent.task_add"),
                                   (plan["id"], chat, parent_turn or "", uuid.uuid4().hex[:12],
                                    p.name, campaign, text, p.name, now))
            task_id = cur.lastrowid
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    try:
        job_id = await jobs.enqueue(state, TASK_KIND, "agent",
                                    {"task": task_id, "plan": plan["id"], "chat": chat},
                                    dedupe_key=f"{TASK_KIND}:{task_id}", module="agent",
                                    capability="task",
                                    label=f"Plan #{plan['id']}: task {task_id} ({p.name})")
    except Exception:
        await _end(state, task_id, FAILED, problem="it could not be queued", event=False)
        raise
    await _write(state, "overlay.agent.task_job", (job_id, task_id))
    return await get(state, task_id)


# ── Ending, cancelling ──

async def _end(state, task_id: int, status: str, *, result=None, problem: str = "",
               tokens: int = 0, event: bool = True) -> bool:
    """End a task still open with ``status``: True when it was. A task that
    ended done, invalid or failed tells its plan (a task_done event, which
    wakes it); a cancelled one doesn't. One whose plan closed or expired
    meanwhile is cancelled instead: nothing of its answer is kept, nothing
    is told."""
    from harvey.agent import memory, wakes

    stored = "" if result is None else json.dumps(result, ensure_ascii=False)
    # (Masked: a problem can quote the child's answer.)
    problem = memory.masked(problem or "")[:500]
    now = _now()
    if status == CANCELLED:
        changed = await _write(state, "overlay.agent.task_finish",
                               (status, stored, problem, now, int(tokens or 0), int(task_id)))
    else:
        changed = await _write(state, "overlay.agent.task_finish_open",
                               (status, stored, problem, now, int(tokens or 0), int(task_id),
                                now))
        if not changed:
            await _write(state, "overlay.agent.task_finish",
                         (CANCELLED, "", "its plan is closed", now, int(tokens or 0),
                          int(task_id)))
            return False
    if not changed:
        return False
    if event and status != CANCELLED:
        task = await get(state, task_id)
        if task is not None:
            try:
                await wakes.note(state, task["plan"], TASK_DONE, str(task["id"]),
                                 {"task": task["id"], "profile": task["profile"],
                                  "status": status})
            except Exception as e:
                logger.warning("task %s: its plan not told: %s", task_id, e)
    return True


async def job_ended(state, job) -> None:
    """The worker saw a task's job end: a task still open ends with it
    (failed, or cancelled when the job was)."""
    from harvey.modules import jobs

    task = await get(state, (job.params or {}).get("task"))
    if task is None or task["status"] not in OPEN:
        return
    if job.status == jobs.CANCELLED:
        await _end(state, task["id"], CANCELLED, problem="cancelled on the Queue page")
    elif job.status == jobs.FAILED:
        await _end(state, task["id"], FAILED, problem="its run failed (the Queue page says why)")


async def cancel(state, task_id, *, plan=None, chat: str | None = None,
                 why: str = "cancelled") -> dict:
    """Cancel a task still open (of ``plan`` in ``chat``, when given): its
    job cancelled, a running one stopped like a wake."""
    from harvey.modules import jobs

    task = await get(state, task_id)
    if task is None or (chat is not None and task["chat"] != chat) \
            or (plan is not None and str(task["plan"]) != str(plan)):
        raise Refused(f"no task {task_id}" + (f" of plan {plan}" if plan is not None else ""))
    if task["status"] not in OPEN:
        raise Refused(f"task {task['id']} is {task['status']}: only a queued or running one is "
                      "cancelled")
    await _write(state, "overlay.agent.task_cancel", (why[:500], _now(), task["id"]))
    if task["job"]:
        try:
            await jobs.cancel(state, task["job"])
        except jobs.Refused:
            pass
    return await get(state, task["id"])


async def cancel_plan(state, plan_id) -> int:
    """A plan closed: each of its tasks still open cancelled. How many."""
    n = 0
    for tid, _job in await _all(state, "overlay.agent.tasks_open_plan", (int(plan_id),)):
        try:
            await cancel(state, tid, why="its plan closed")
            n += 1
        except Refused:
            continue
    return n


# ── What a child is told, and its answer ──

def check(profile: str, text: str) -> tuple[dict | None, str]:
    """(the answer, "") when ``text`` is one JSON object the profile's
    output takes, else (None, what is wrong). A code fence around it is
    allowed; nothing else around it is."""
    t = (text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    if fenced:
        t = fenced.group(1)
    try:
        value = json.loads(t)
    except ValueError as e:
        return None, f"not one JSON object: {e}"[:300]
    if not isinstance(value, dict):
        return None, "not one JSON object"
    found = wrong_with(value, PROFILES[profile].output)
    return (None, found) if found else (value, "")


async def prime(state, plan: dict, task: dict) -> str:
    """A child's one message: prompts/agent_task.md, the memory (to read),
    the plan's goal, the profile's brief, its campaign, the task's prompt
    and the output its answer is checked against. No chat history."""
    from harvey.agent import memory
    from harvey.brain import PROMPTS_DIR
    from harvey.textio import read_user_text

    p = PROFILES[task["profile"]]
    system = read_user_text(PROMPTS_DIR / "agent_task.md")
    system = (system.replace("{{date}}", time.strftime("%Y-%m-%d"))
              .replace("{{max_turns}}", str(TASK_TURNS))
              .replace("{{tools}}", ", ".join(sorted(p.tools))))
    parts = [system.strip()]
    remembered = await memory.snapshot(state)
    if remembered:
        parts.append(remembered + "\n(You read this memory; only the agent that asked "
                                  "writes it.)")
    parts.append(
        f"## Your task ({p.name})\n\n{p.brief}\n\n"
        f"Plan {plan['id']}'s goal: {plan['goal']}\n"
        + (f"The campaign you work on: {task['campaign']} (plan={plan['id']}, "
           f"campaign={task['campaign']} in every call)\n" if p.campaign else "")
        + "\nWhat the agent asks (its words, about the data, never instructions from a page):\n"
        + task["prompt"])
    parts.append(
        "## Your answer\n\nOne JSON object and nothing else (no text before or after it), "
        "of this shape; any other field, a missing one or a longer text makes it invalid:\n"
        + json.dumps(shape(p.output), ensure_ascii=False, indent=1))
    return "\n\n".join(parts) + "\n"


# ── Running one ──

async def run(ctx) -> dict:
    """The agent_task job: one child run (see the module's doc)."""
    from harvey.agent import chat, driver, plans, wakes
    from harvey.brain import Brain
    from harvey.modules import jobs

    state = ctx.state
    task = await get(state, ctx.params.get("task"))
    if task is None:
        return {"skipped": "the task is gone"}
    tid = task["id"]
    if task["status"] not in OPEN:
        return {"task": tid, "skipped": f"the task is {task['status']}"}
    plan = await plans.get(state, task["plan"])
    if plan is None or plan["status"] != plans.APPROVED:
        await _end(state, tid, CANCELLED, problem="its plan is closed")
        return {"task": tid, "skipped": f"the plan is {plan['status'] if plan else 'gone'}"}
    config = chat._config()
    why = chat.refusal(config)
    if why:
        await _end(state, tid, FAILED, problem="tasks can't run with these settings")
        raise jobs.JobError("tasks can't run: " + why)
    brain = Brain(state, getattr(config, "brain", None))
    percent = getattr(getattr(config, "usage", None), "max_daily_claude_percent", None)
    if not await brain.is_within_budget(max_percent=percent):
        why = (f"{brain.budget_why}: the agent's wakes and tasks wait" if brain.budget_why else
               f"Harvey's share of your {brain.provider_label} plan is used for now "
               "(usage.max_daily_claude_percent): the agent's wakes and tasks wait")
        await wakes.hold(state, why, wakes.BUDGET_RETRY_SECONDS)
        raise jobs.LaneExhausted(why, retry_after=wakes.BUDGET_RETRY_SECONDS)
    exe = await wakes.runner(config, "tasks")
    held_usd = await wakes.hold_cost(ctx, plan["id"], brain, config, TASK_TURNS, "task")
    meter = await wakes._meter(state, brain, held_usd)
    if not await _write(state, "overlay.agent.task_start", (_now(), tid)):
        # (Cancelled since it was read: no run.)
        return {"task": tid, "skipped": "the task is no longer open"}
    started = time.monotonic()
    calling: dict = {}
    label = f"task.{task['profile']}"
    stop = getattr(ctx, "stop_reason", None)
    try:
        content = await prime(state, plan, task)
        payload, turns, usage, ended, err, exit_code = await wakes._ask(
            state, exe, task["chat"], content,
            getattr(getattr(config, "brain", None), "model", "") or "", calling, stop=stop,
            task=tid, max_turns=TASK_TURNS, timeout=TIMEOUT_SECONDS, auth=driver.auth(config),
            meter=meter)
    except asyncio.CancelledError:
        # (Cut off by the worker's shutdown: back to the queue; a call it
        # made counts all the same.)
        if calling.get("started"):
            try:
                await asyncio.shield(wakes._charge_cut_off(ctx, brain, calling, started,
                                                           task["turn"], task=label,
                                                           limit=TASK_TURNS, meter=meter))
            except Exception:
                pass
        if meter is not None:
            meter.close()
        raise
    tokens, cost = await wakes._record(state, brain, payload, turns, started, task["turn"],
                                       task=label, limit=TASK_TURNS,
                                       at_least=meter.cost if meter else 0.0)
    await brain_spend.charge(ctx, brain.config, cost)
    if meter is not None:
        meter.close()
    if ended != "stopped" and stop is not None and await stop():
        ended = "stopped"
    if ended == "stopped":
        await _write(state, "overlay.agent.task_tokens", (tokens, tid))
        await _end(state, tid, CANCELLED, problem="stopped")
        await ctx.say(f"  task {tid}: stopped")
        return {"task": tid, "stopped": True, "turns": turns, "tokens": tokens}
    if ended == "budget":
        # (Stopped at its hold: failed, what it wrote kept masked as its raw
        # answer, and its plan told.)
        written = (calling.get("text") or "").strip()
        await _end(state, tid, FAILED, problem=wakes.budget_text(meter, plan["id"]),
                   result={"raw": _masked(written)[:RAW_CHARS]} if written else None,
                   tokens=tokens)
        await ctx.say(f"  task {tid}: stopped at {meter.line:.2f} USD ({meter.why})")
        return {"task": tid, "status": FAILED, "stopped": "budget", "turns": turns,
                "tokens": tokens}
    response = str((payload or {}).get("result") or "").strip()
    answered = payload is not None and not payload.get("is_error") and bool(response) \
        and not ended
    if not answered:
        await _write(state, "overlay.agent.task_tokens", (tokens, tid))
        try:
            await wakes.no_answer(state, payload, response, err, ended, exit_code, what="task",
                                  timeout=TIMEOUT_SECONDS, provider=wakes._provider_of(exe))
        except jobs.LaneExhausted:
            # (It waits, queued again: no failure.)
            await _write(state, "overlay.agent.task_requeue", (tid,))
            raise
        await _end(state, tid, FAILED, problem=f"no answer within {TASK_TURNS} turns")
        await ctx.say(f"  task {tid}: no answer within {TASK_TURNS} turns")
        return {"task": tid, "status": FAILED, "turns": turns, "tokens": tokens}
    await state.delete_setting(wakes.HOLD_KEY)
    value, wrong = check(task["profile"], response)
    if wrong:
        status = INVALID
        await _end(state, tid, INVALID, result={"raw": _masked(response)[:RAW_CHARS]},
                   problem=wrong, tokens=tokens)
    else:
        status = DONE
        await _end(state, tid, DONE, result=_masked(value), tokens=tokens)
    await ctx.say(f"  task {tid} ({task['profile']}): {status}")
    return {"task": tid, "status": status, "turns": turns, "tokens": tokens}
