"""The tasks Harvey's agent hands to a child under an approved plan
(harvey/agent/tasks.py). The Claude CLI is a stand-in (a small Python
script speaking the stream-json lines the real CLI writes: no model is
called) and nothing else runs. A child's server offers only its profile's
tools and refuses any other even by name; a campaign_driver only its own
campaign; assign_task's refusals; a child's valid answer ends the task
done and wakes the plan, an invalid one is kept, masked; a failed run
fails the task; closing the plan or deleting its chat cancels its tasks;
the prompt is masked; usage rows and tokens; the Chat tab's words; the
CLI. All data is fictional."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harvey import claude_runtime, registers
from harvey.agent import actions, chat, plans, readonly, tasks, tools, wakes
from harvey.brain import Brain
from harvey.config import load_config
from harvey.modules import jobs, worker
from harvey.state import StateManager

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "harvey" / "web"
CHAT = "chat-t"

RESEARCH = {"areas": [{"area": "Brașov", "why": "1.200 de firme fără site"}],
            "phrases": ["stomatolog", "clinica dentara"], "sources": ["dataforseo_listings"],
            "estimate": {"summary": "o căutare pe expresie", "usd_typical": 0.4, "usd_high": 0.9},
            "notes": "Registrul are deja firmele; căutarea adaugă telefonul și site-ul."}

# The CLI's stand-in for a task: it reads the MCP config (the server's
# --task), logs the message it was given (FAKE_CLI_LOG), records one tool
# call the way the task's server records it, and answers FAKE_ANSWER (a
# file holding the text), or: FAKE_TASK=login a logged-out CLI's result,
# die exits with an error, loop calls tools until it is interrupted.
FAKE = r'''
import json, os, select, sys

args = sys.argv[1:]
cfg = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))
srv = cfg["mcpServers"]["harvey"]
sys.path.insert(0, srv["env"]["PYTHONPATH"])
db = srv["args"][srv["args"].index("--db") + 1]
chat = srv["args"][srv["args"].index("--chat") + 1]
task = int(srv["args"][srv["args"].index("--task") + 1])
mode = os.environ.get("FAKE_TASK", "")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def readline(timeout=None):
    if timeout is not None and not select.select([0], [], [], timeout)[0]:
        return None
    buf = b""
    while True:
        c = os.read(0, 1)
        if not c:
            return buf.decode() if buf else ""
        if c == b"\n":
            return buf.decode()
        buf += c


def result(text, error=False, subtype="success"):
    out({"type": "result", "subtype": subtype, "is_error": error, "result": text,
         "session_id": "s", "duration_ms": 500, "num_turns": 2,
         "usage": {"input_tokens": 700, "output_tokens": 60},
         "modelUsage": {"claude-test": {"inputTokens": 700, "outputTokens": 60,
                                        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                                        "costUSD": float(os.environ.get("FAKE_COST", "0"))}}})


def tool_round(k):
    from harvey.agent import tools
    mid = f"m{k}"
    out({"type": "assistant", "message": {"id": mid, "content": [
        {"type": "tool_use", "id": f"t{k}", "name": "mcp__harvey__counts", "input": {}}]}})
    tools.Tools(db, chat, task).record("counts", {}, "firms=3", 5, "ok")
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{k}"}]}})


line = readline()
msg = json.loads(line)
with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"task": task, "chat": chat, "args": srv["args"],
                        "content": msg["message"]["content"]}) + "\n")
if mode == "login":
    result("Not logged in · Please run /login", True)
    sys.exit(0)
if mode == "die":
    sys.stderr.write("something broke\n")
    sys.exit(3)
if mode == "loop":
    k = 0
    while True:
        tool_round(k)
        k += 1
        got = readline(0.05)
        if got and json.loads(got).get("type") == "control_request":
            result("", True, "error_during_execution")
            break
    sys.exit(0)
tool_round(0)
text = open(os.environ["FAKE_ANSWER"], encoding="utf-8").read()
out({"type": "assistant", "message": {"id": "m-text", "content": [{"type": "text", "text": text}]}})
result(text)
'''


def _config(provider: str = "claude"):
    cfg = load_config(str(ROOT / "harvey.yaml"))
    cfg.brain.provider = provider
    return cfg


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No real CLI, no quota read, no register, the template's settings."""
    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [])
    monkeypatch.setattr(chat, "_config", lambda: _config())

    def refuse(*a, **k):
        raise AssertionError("the real Claude CLI was looked for")
    monkeypatch.setattr(claude_runtime, "check_cli", refuse)

    async def within(self, *a, **k):
        return True
    monkeypatch.setattr(Brain, "is_within_budget", within)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "fake-cli.log"
    log.write_text("", encoding="utf-8")
    answer = tmp_path / "answer.txt"
    answer.write_text(json.dumps(RESEARCH, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("FAKE_ANSWER", str(answer))
    monkeypatch.delenv("FAKE_TASK", raising=False)
    monkeypatch.setattr(claude_runtime, "check_cli", lambda *a, **k: str(script))

    class Fake:
        def lines(self):
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]

        def answer(self, text):
            answer.write_text(text, encoding="utf-8")
    return Fake()


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    asyncio.run(s.init_db())
    con = sqlite3.connect(s.db_path)
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at, turn) "
                "VALUES (?, 'Dentiști din Brașov', 1, 1, 'turn-1')", (CHAT,))
    con.execute("INSERT INTO agent_message (turn, role, text, status, chat, created_at, "
                "updated_at) VALUES ('turn-0', 'user', 'Ce firme sunt la Sibiu?', 'ok', ?, 1, 1)",
                (CHAT,))
    con.commit()
    con.close()
    return s


def _q(state, sql, *args):
    con = sqlite3.connect(state.db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _x(state, sql, *args):
    con = sqlite3.connect(state.db_path)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _run(coro):
    return asyncio.run(coro)


def _plan(state, *, approve=True, chat_id=CHAT, budget=0.0) -> int:
    async def go():
        p = await plans.propose(state, chat_id, "t0", "Contacte pentru dentiștii din Brașov",
                                ["Caut zonele", "Caut firmele"], budget, 72)
        if approve:
            await plans.decide(state, p["id"], True, plans.BY_DASHBOARD, budget_usd=budget)
        return p["id"]
    return _run(go())


def _assign(state, plan_id, profile="researcher", prompt="Unde caut dentiști în Brașov?",
            campaign=""):
    return _run(tasks.assign(state, plan_id, CHAT, "turn-1", profile, prompt, campaign))


def _work(state):
    """Claim the agent lane's next job and run it as the worker does: the
    job as it ended."""
    async def go():
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, wakes.AGENT_LANE, w.name)
        assert job is not None and job.kind == tasks.TASK_KIND
        await w.run_job(job, {})
        return await jobs.get(state, job.id)
    return _run(go())


def _task(state, tid):
    return _run(tasks.get(state, tid))


# ── Profiles ──

def test_profiles_hold_real_tools_and_none_a_child_may_never_have():
    from mcp import Client

    async def names():
        async with Client(tools.build(":memory:", "x")) as c:
            return {t.name for t in (await c.list_tools()).tools}
    every = _run(names())
    assert tasks.FORBIDDEN <= every
    for p in tasks.PROFILES.values():
        assert p.tools <= every and not p.tools & tasks.FORBIDDEN, p.name
    for name in ("assign_task", "schedule_wake", "propose_plan", "close_plan", "discover",
                 "registry_pull", "find_contacts", "verify_contacts", "cancel_run",
                 "create_campaign", "request_start", "pause_campaign", "suppress", "draft_reply",
                 "remember", "forget", "save_playbook"):
        assert name in tasks.FORBIDDEN
    assert "edit_campaign" in tasks.PROFILES["campaign_driver"].tools
    assert "edit_campaign" not in tasks.PROFILES["researcher"].tools
    with pytest.raises(ValueError):
        tasks.register(tasks.Profile("spender", frozenset({"counts", "discover"}),
                                     tasks._obj({}), "x"))


def test_a_childs_server_offers_only_its_profiles_tools_and_refuses_the_rest_by_name(state):
    from mcp import Client

    plan_id = _plan(state)
    task = _assign(state, plan_id)
    _x(state, "UPDATE agent_task SET status = 'running' WHERE id = ?", task["id"])

    async def run():
        async with Client(tools.build(state.db_path, CHAT, task["id"])) as c:
            listed = {t.name for t in (await c.list_tools()).tools}
            forbidden = await c.call_tool("discover", {"plan": plan_id, "sources": ["osm"]})
            asked = await c.call_tool("counts", {})
            return listed, forbidden, asked
    listed, forbidden, asked = _run(run())
    assert listed == tasks.PROFILES["researcher"].tools
    assert forbidden.is_error and not asked.is_error
    # The check on each call, whatever was listed: a tool by name, another
    # plan, a task no longer running.
    t = tools.Tools(state.db_path, CHAT, task["id"])

    async def call(name, args):
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            await t.call(name, args, lambda: asyncio.sleep(0, {"ok": True}))
            return "ok"
        except ToolError as e:
            return str(e)
    assert "isn't a tool of a researcher task" in _run(call("find_contacts", {"plan": plan_id}))
    assert "isn't a tool of a researcher task" in _run(call("assign_task", {"plan": plan_id}))
    assert "under plan" in _run(call("sets", {"plan": plan_id + 1}))
    assert _run(call("sets", {})) == "ok"
    _x(state, "UPDATE agent_task SET status = 'cancelled' WHERE id = ?", task["id"])
    assert "no more calls" in _run(call("counts", {}))
    # Recorded under the task's own turn, never the chat's message.
    rows = _q(state, "SELECT tool, outcome FROM agent_tool_call WHERE turn = ? ORDER BY id",
              task["turn"])
    assert ("find_contacts", "refused") in rows and ("counts", "ok") in rows
    assert _q(state, "SELECT COUNT(*) FROM agent_tool_call WHERE turn = 'turn-1'") == [(0,)]


def test_a_task_of_another_chat_gets_no_tools(state):
    from mcp import Client

    task = _assign(state, _plan(state))

    async def run():
        async with Client(tools.build(state.db_path, "other-chat", task["id"])) as c:
            return (await c.list_tools()).tools
    assert _run(run()) == []


# ── Assigning ──

def test_assign_task_is_refused_outside_its_bounds(state, monkeypatch):
    proposed = _plan(state, approve=False)
    plan_id = _plan(state)
    a = actions.Actions(state.db_path, CHAT)

    def refused(plan, profile="researcher", prompt="Unde caut?", campaign=""):
        try:
            _run(a.assign_task("turn-1", plan, profile, prompt, campaign))
        except readonly.Refused as e:
            return str(e)
        return ""
    assert "waits for the user's approval" in refused(proposed)
    assert "no plan 999" in refused(999)
    assert "no profile 'spender'" in refused(plan_id, "spender")
    assert "say what the task is" in refused(plan_id, prompt="  ")
    assert f"at most {tasks.PROMPT_CHARS:,}" in refused(plan_id, prompt="x" * 2001)
    assert "takes no campaign" in refused(plan_id, campaign="c1")
    assert "give its id" in refused(plan_id, "campaign_driver")
    assert "isn't one made under plan" in refused(plan_id, "campaign_driver", campaign="c1")
    assert refused(plan_id) == "" and refused(plan_id) == ""
    assert f"{tasks.OPEN_MAX} tasks queued or running" in refused(plan_id)
    _x(state, "UPDATE agent_task SET status = 'done'")
    for _ in range(tasks.PER_DAY - 2):
        assert refused(plan_id) == ""
        _x(state, "UPDATE agent_task SET status = 'done'")
    assert f"{tasks.PER_DAY} tasks in the last 24 hours" in refused(plan_id)
    from harvey import codex
    monkeypatch.setattr(chat, "_config", lambda: _config("codex"))
    monkeypatch.setattr(codex, "_PLATFORM", "win32")
    why = refused(plan_id)
    assert "tasks can't run" in why and "Windows" in why
    outcomes = _q(state, "SELECT outcome, COUNT(*) FROM agent_action WHERE tool = 'assign_task' "
                         "GROUP BY outcome")
    assert dict(outcomes) == {"ok": tasks.PER_DAY, "refused": 11}
    # Each one queued as a job on the agent's lane, labelled for the Queue page.
    assert _q(state, "SELECT COUNT(*), MIN(lane), MIN(label) FROM jobs WHERE kind = ?",
              tasks.TASK_KIND)[0][:2] == (tasks.PER_DAY, "agent")
    assert "task" in _q(state, "SELECT label FROM jobs WHERE kind = ?", tasks.TASK_KIND)[0][0]


def test_the_prompt_is_kept_masked(state):
    task = _assign(state, _plan(state),
                   prompt="Firma cu office@alfa.example.com și 0722 000 102 din Brașov")
    assert "office@" not in task["prompt"] and "000 102" not in task["prompt"]
    assert "o***@alfa.example.com" in task["prompt"] and "Brașov" in task["prompt"]


# ── Running one ──

def test_a_valid_answer_ends_the_task_done_and_wakes_its_plan(state, fake):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    job = _work(state)
    assert job.status == "done"
    done = _task(state, task["id"])
    assert done["status"] == "done" and done["attempts"] == 1 and done["problem"] == ""
    assert done["result"] == RESEARCH and done["tokens"] == 760
    # What the child was told: its tools, the plan's goal and its prompt,
    # never the chat so far; the server in task mode.
    [given] = fake.lines()
    assert given["args"][-2:] == ["--task", str(task["id"])]
    assert "Unde caut dentiști în Brașov?" in given["content"]
    assert "Contacte pentru dentiștii din Brașov" in given["content"]
    assert "Ce firme sunt la Sibiu?" not in given["content"]
    assert "phrase_yield" in given["content"] and "discover" not in given["content"].split(
        "## Your task")[0]
    # Its tool call is recorded under its turn; its tokens as task.researcher.
    assert _q(state, "SELECT tool FROM agent_tool_call WHERE turn = ?", task["turn"]) == [
        ("counts",)]
    assert _q(state, "SELECT agent, task, input_tokens, output_tokens FROM usage_events") == [
        ("agent", "task.researcher", 700, 60)]
    # The plan is told, and its wake queued.
    [(kind, ref, data)] = _q(state, "SELECT kind, ref, data FROM agent_event")
    assert (kind, ref) == ("task_done", str(task["id"]))
    assert json.loads(data) == {"task": task["id"], "profile": "researcher", "status": "done"}
    assert _q(state, "SELECT status FROM jobs WHERE kind = 'agent_wake'") == [("queued",)]
    # What the agent and the plan card read.
    card = _run(plans.for_chat(state, CHAT))[-1]
    assert card["task_tokens"] == 760
    [shown] = card["tasks"]
    assert shown["counts"] == {"areas": 1, "phrases": 2, "sources": 1}
    listed = _run(actions.Actions(state.db_path, CHAT).tasks(plan_id))
    assert listed["tasks"][0]["result"] == RESEARCH


def test_an_api_key_tasks_cost_counts_in_its_plan_and_a_short_budget_holds_it(
        state, fake, monkeypatch):
    from harvey import brain_spend

    cfg = _config()
    cfg.brain = cfg.brain.model_copy(update={"auth": "api_key"})
    monkeypatch.setattr(chat, "_config", lambda: cfg)
    monkeypatch.setenv("FAKE_COST", "0.31")
    # A free plan can't pay for a billed task: it waits, the CLI never starts.
    free = _plan(state)
    task = _assign(state, free)
    job = _work(state)
    assert job.status == "queued" and "budget can't cover it" in job.error
    assert _task(state, task["id"])["status"] == "queued" and fake.lines() == []
    _run(plans.close(state, free, cancel=True, by=plans.BY_CLI))
    # One that can: its worst case held, then what it cost counted.
    paid = _plan(state, budget=2.0)
    task = _assign(state, paid)
    job = _work(state)
    assert job.status == "done" and job.actual_cost == pytest.approx(0.31)
    assert tasks.TASK_TURNS * brain_spend.TURN_USD_FLOOR <= 2.0
    money = _run(plans.money(state, paid))
    assert money["committed_usd"] == pytest.approx(0.31) == money["agent_usd"]
    assert _q(state, "SELECT task, source FROM usage_events") == [("task.researcher", "api_key")]


@pytest.mark.parametrize("answer, why", [
    ("Am găsit trei zone: Brașov, Sibiu, Cluj. Scrie la office@alfa.example.com", "not one JSON"),
    (json.dumps({**RESEARCH, "extra": 1}), "no field 'extra'"),
    (json.dumps({k: v for k, v in RESEARCH.items() if k != "notes"}), "answer.notes is missing"),
    (json.dumps({**RESEARCH, "phrases": ["x"] * 13}), "answer.phrases holds at most 12"),
    (json.dumps([RESEARCH]), "not one JSON object"),
])
def test_an_invalid_answer_is_kept_masked_with_its_problem(state, fake, answer, why):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    fake.answer(answer)
    assert _work(state).status == "done"
    kept = _task(state, task["id"])
    assert kept["status"] == "invalid" and why in kept["problem"]
    assert "office@alfa" not in kept["result"]["raw"]
    assert len(kept["result"]["raw"]) <= tasks.RAW_CHARS
    assert _q(state, "SELECT kind FROM agent_event") == [("task_done",)]
    assert json.loads(_q(state, "SELECT data FROM agent_event")[0][0])["status"] == "invalid"


def test_a_fenced_answer_counts_and_a_failed_run_fails_the_task(state, fake, monkeypatch):
    plan_id = _plan(state)
    first = _assign(state, plan_id)
    fake.answer("```json\n" + json.dumps(RESEARCH) + "\n```")
    _work(state)
    assert _task(state, first["id"])["status"] == "done"
    # (The plan's wake it queued is someone else's test.)
    _x(state, "DELETE FROM jobs WHERE kind = 'agent_wake'")
    second = _assign(state, plan_id)
    monkeypatch.setenv("FAKE_TASK", "die")
    job = _work(state)
    assert job.status == "failed"
    failed = _task(state, second["id"])
    assert failed["status"] == "failed" and "something broke" not in failed["problem"]
    assert ("task_done", str(second["id"])) in _q(state, "SELECT kind, ref FROM agent_event")


def test_a_logged_out_cli_pauses_the_lane_and_the_task_waits(state, fake, monkeypatch):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    monkeypatch.setenv("FAKE_TASK", "login")
    job = _work(state)
    assert job.status == "queued"
    assert _run(jobs.lane_paused(state, "agent"))
    assert _task(state, task["id"])["status"] == "queued"
    assert _q(state, "SELECT COUNT(*) FROM agent_event") == [(0,)]


def test_a_child_is_held_to_its_turns(state, fake, monkeypatch):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    monkeypatch.setenv("FAKE_TASK", "loop")
    _work(state)
    ended = _task(state, task["id"])
    assert ended["status"] == "failed" and f"{tasks.TASK_TURNS} turns" in ended["problem"]
    assert _q(state, "SELECT COUNT(*) FROM agent_tool_call WHERE turn = ?",
              task["turn"])[0][0] >= tasks.TASK_TURNS


def test_codex_where_it_cant_run_refuses_a_task_already_queued(state, fake, monkeypatch):
    from harvey import codex
    task = _assign(state, _plan(state))
    monkeypatch.setattr(chat, "_config", lambda: _config("codex"))
    monkeypatch.setattr(codex, "_PLATFORM", "win32")
    assert _work(state).status == "failed"
    assert _task(state, task["id"])["status"] == "failed" and fake.lines() == []


# ── Cancelling ──

def test_closing_the_plan_or_deleting_its_chat_cancels_its_tasks(state, fake):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    _run(plans.close(state, plan_id, cancel=False, by=plans.BY_CLI))
    assert _task(state, task["id"])["status"] == "cancelled"
    assert _q(state, "SELECT status FROM jobs WHERE id = ?", task["job"]) == [("cancelled",)]
    second = _assign(state, _plan(state))
    # A child's tool calls go with the chat.
    _x(state, "INSERT INTO agent_tool_call (turn, tool, arguments, result, ms, outcome, at) "
              "VALUES (?, 'counts', '{}', '', 1, 'ok', 1)", second["turn"])

    async def delete():
        try:
            await chat.hub(state).delete(CHAT)
        finally:
            await chat.close_all()
    _run(delete())
    assert _task(state, second["id"])["status"] == "cancelled"
    assert _q(state, "SELECT COUNT(*) FROM agent_tool_call") == [(0,)]
    assert _q(state, "SELECT COUNT(*) FROM agent_event WHERE kind = 'task_done'") == [(0,)]


def test_the_agent_and_the_user_cancel_a_task(state, fake):
    plan_id = _plan(state)
    a = actions.Actions(state.db_path, CHAT)
    task = _assign(state, plan_id)
    out = _run(a.cancel_task("turn-1", plan_id, task["id"]))
    assert out["status"] == "cancelled"
    with pytest.raises(readonly.Refused):
        _run(a.cancel_task("turn-1", plan_id, task["id"]))
    other = _assign(state, plan_id)
    # Cancelled on the Queue page while queued: the task ends with its job.
    _run(jobs.cancel(state, other["job"]))
    [shown] = [t for t in _run(tasks.listed(state, plan_id)) if t["id"] == other["id"]]
    assert shown["status"] == "cancelled"


def test_a_running_child_is_stopped_when_its_task_is_cancelled(state, fake, monkeypatch):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    monkeypatch.setenv("FAKE_TASK", "loop")
    monkeypatch.setattr(wakes, "STOP_POLL_SECONDS", 0.1)
    monkeypatch.setattr(tasks, "TASK_TURNS", 10_000)

    async def go():
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, wakes.AGENT_LANE, w.name)
        running = asyncio.ensure_future(w.run_job(job, {}))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if (await tasks.get(state, task["id"]))["status"] == "running":
                break
        await tasks.cancel(state, task["id"], why="cancelled by the user")
        await asyncio.wait_for(running, 30)
        return await jobs.get(state, job.id)
    job = _run(go())
    assert job.status == "cancelled"
    assert _task(state, task["id"])["status"] == "cancelled"
    assert _q(state, "SELECT task FROM usage_events") == [("task.researcher",)]


# ── A campaign_driver keeps to its campaign ──

def test_a_campaign_driver_works_on_its_own_campaign_only(tmp_path, monkeypatch):
    from tests.test_agent_campaigns import NOTE_LONG
    from tests.test_campaigns import SEQUENCE, config, contact

    cfg = config()
    monkeypatch.setattr("harvey.modules.job_kinds._config", lambda: cfg)
    path = tmp_path / "harvey.db"
    s = StateManager(str(path))

    async def seed():
        await s.init_db()
        await contact(s, "office@alfa.example.com")
    _run(seed())
    _x(s, "INSERT INTO agent_chat (id, title, created_at, last_at, turn) "
          "VALUES (?, 'Campanie', 1, 1, 'turn-1')", CHAT)
    plan_id = _plan(s)
    a = actions.Actions(str(path), CHAT)

    async def drafts():
        aud = await a.save_audience("turn-1", plan_id, "Public", {"q": "alfa"})
        out = []
        for name in ("Stomatologi", "Clinici"):
            made = await a.create_campaign("turn-1", plan_id, name, aud["audience"],
                                           "programări online", SEQUENCE, NOTE_LONG)
            out.append(made["campaign"])
        return out
    mine, other = _run(drafts())
    task = _run(tasks.assign(s, plan_id, CHAT, "turn-1", "campaign_driver", "Verifică ciorna",
                             mine))
    _x(s, "UPDATE agent_task SET status = 'running' WHERE id = ?", task["id"])
    t = tools.Tools(str(path), CHAT, task["id"])
    assert t.refusal("edit_campaign", {"plan": plan_id, "campaign": mine}) == ""
    assert "campaign " + mine in t.refusal("edit_campaign", {"plan": plan_id, "campaign": other})
    assert "campaign " + mine in t.refusal("replies", {"plan": plan_id})
    assert t.refusal("campaign_status", {"campaign": mine}) == ""
    # Only the emails it was given: never the audience, offer, note or the rest.
    assert t.refusal("edit_campaign", {"plan": plan_id, "campaign": mine, "sequence": SEQUENCE,
                                       "audience": None, "note": None}) == ""
    for field, value in (("audience", 7), ("name", "Alt nume"), ("offer", "altceva"),
                         ("note", NOTE_LONG), ("rolling", True), ("always_review", True)):
        why = t.refusal("edit_campaign", {"plan": plan_id, "campaign": mine, field: value})
        assert "changes only the sequence, not " + field in why
    assert "isn't a tool" in t.refusal("request_start", {"plan": plan_id, "campaign": mine})
    assert "isn't a tool" in t.refusal("pause_campaign", {"plan": plan_id, "campaign": mine})
    assert "isn't a tool" in t.refusal("discover", {"plan": plan_id})


# ── The output check ──

def test_the_output_check_takes_each_profiles_shape():
    ok, why = tasks.check("researcher", json.dumps(RESEARCH))
    assert ok == RESEARCH and why == ""
    drive = {"check": {"ok": False, "problems": ["subiect prea lung"]},
             "edits": ["subiect scurtat"], "to_do": ["confirmarea"]}
    assert tasks.check("campaign_driver", json.dumps(drive)) == (drive, "")
    assert "answer.check.ok is true or false" in tasks.check(
        "campaign_driver", json.dumps({**drive, "check": {"ok": "da", "problems": []}}))[1]
    assert "is a number" in tasks.check("researcher", json.dumps(
        {**RESEARCH, "estimate": {"summary": "x", "usd_high": float("nan")}}))[1]
    assert "is a number" in tasks.check("researcher", json.dumps(
        {**RESEARCH, "estimate": {"summary": "x", "usd_high": "1"}}))[1]
    assert tasks.check("researcher", "{} {}")[1].startswith("not one JSON object")


# ── The dashboard's words, the CLI ──

RENDER = r'''
const fs = require('fs'), vm = require('vm');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const src = fs.readFileSync(process.argv[1], 'utf8');
const pick = name => { const i = src.indexOf('function ' + name + '('); let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) { if (src[k] === '{') d++; else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1); } };
for (const n of ['escHtml', 'renderPlanCard', 'planBadge', 'planWakes', 'planCampaigns', 'planTasks',
                 'taskLine', 'startRequestCard', 'wakeLabel'])
  vm.runInThisContext(pick(n));
for (const c of ['PLAN_TONE', 'CAMPAIGN_TONE', 'TASK_TONE'])
  vm.runInThisContext(src.match(new RegExp('const ' + c + ' = \\{[^}]*\\};'))[0]);
global.fmtUsd = (n, d) => '$' + Number(n).toFixed(d);
global.fmtN = n => String(n);
global.intlLocale = () => 'en';
const plan = {id: 7, goal: 'Dentiști', steps: ['a'], budget_usd: 1, status: 'approved', expires_at: 2000000000,
  committed_usd: 0, spent_usd: 0, remaining_usd: 1, runs: [], campaigns: [], task_tokens: 760,
  tasks: [{task: 4, profile: 'researcher', status: 'done', counts: {areas: 1, phrases: 2, sources: 1}, problem: ''},
          {task: 5, profile: 'campaign_driver', campaign: '<b>c1</b>', status: 'running', counts: {}, problem: ''},
          {task: 6, profile: 'campaign_driver', campaign: 'c2', status: 'done',
           counts: {ok: false, problems: 1, edits: 2, to_do: 1}, problem: ''},
          {task: 8, profile: 'researcher', status: 'invalid', counts: {}, problem: 'answer.notes is missing'},
          {task: 9, profile: 'researcher', status: 'cancelled', counts: {}, problem: 'cancelled by the user'}]};
process.stdout.write(JSON.stringify({html: renderPlanCard(plan),
  wake: wakeLabel({kinds: ['task_done'], runs: [], label: 'after task done'})}));
'''


@pytest.mark.parametrize("lang, words", [
    ("ro", ["Sarcini date mai departe", "760 de tokeni", "Cercetare", "Verificarea campaniei",
            "propune zone: 1, expresii: 2, surse: 1", "lucrează", "1 problemă găsită",
            "modificări: 2, de făcut înainte de pornire: 1", "răspuns nevalid", "Anulează",
            "după ce o sarcină s-a încheiat"]),
    ("en", ["Tasks handed on", "760 tokens", "Research", "Campaign check",
            "proposes areas: 1, phrases: 2, sources: 1", "working", "1 problem found",
            "edits: 2, to do before the start: 1", "invalid answer", "Cancel",
            "after a task ended"]),
])
def test_the_plan_card_lists_its_tasks_in_both_languages(lang, words):
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    res = subprocess.run([node, "-e", RENDER, str(WEB / "app.js"), str(WEB / "i18n.js"), lang],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-2000:]
    out = json.loads(res.stdout)
    for w in words:
        assert w in out["html"] + out["wake"], w
    html = out["html"]
    assert "answer.notes is missing" in html and "&lt;b&gt;c1&lt;/b&gt;" in html
    # (A cancelled task's badge says it: no English reason beside it.)
    assert "cancelled by the user" not in html
    # Cancel only on the running one.
    assert html.count('onclick="cancelTask(this.dataset.task)"') == 1 and 'data-task="5"' in html


def test_the_cli_lists_shows_and_cancels_tasks(state, monkeypatch, capsys):
    from harvey import cli
    import harvey.state as state_mod

    plan_id = _plan(state)
    task = _assign(state, plan_id)
    monkeypatch.setattr(state_mod, "DB_PATH", state.db_path)
    for argv in (["--plan", str(plan_id)], ["show", str(task["id"])],
                 ["cancel", str(task["id"])]):
        monkeypatch.setattr(sys, "argv", ["harvey", "agent", "tasks", *argv])
        cli.main()
    out = capsys.readouterr().out
    assert "researcher" in out and "Unde caut dentiști" in out
    assert f"Task {task['id']} is cancelled." in out
    monkeypatch.setattr(sys, "argv", ["harvey", "agent", "tasks", "cancel", str(task["id"])])
    with pytest.raises(SystemExit):
        cli.main()


# ── A task's end once its plan closed, and its calls once it ended ──

@pytest.mark.parametrize("closing", [
    "UPDATE agent_plan SET status = 'done', closed_at = 1 WHERE id = ?",
    "UPDATE agent_plan SET expires_at = 1 WHERE id = ?",
])
def test_a_task_ending_after_its_plan_closed_keeps_nothing_and_tells_nobody(state, closing):
    # (The plan closed, or ran past its time, while the child ran, before
    # its tasks were cancelled: its answer is not kept and nothing wakes.)
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    _x(state, "UPDATE agent_task SET status = 'running' WHERE id = ?", task["id"])
    _x(state, closing, plan_id)
    assert _run(tasks._end(state, task["id"], tasks.DONE, result=RESEARCH, tokens=9)) is False
    ended = _task(state, task["id"])
    assert ended["status"] == "cancelled" and ended["result"] is None
    assert ended["problem"] == "its plan is closed" and ended["tokens"] == 9
    assert _q(state, "SELECT COUNT(*) FROM agent_event") == [(0,)]


def test_a_task_cancelled_before_its_run_starts_calls_no_cli(state, fake, monkeypatch):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    real = plans.get

    async def cancelled_meanwhile(st, pid, *a, **k):
        found = await real(st, pid, *a, **k)
        _x(state, "UPDATE agent_task SET status = 'cancelled' WHERE id = ?", task["id"])
        return found
    monkeypatch.setattr(plans, "get", cancelled_meanwhile)
    _work(state)
    assert fake.lines() == []
    assert _q(state, "SELECT COUNT(*) FROM usage_events") == [(0,)]


def test_a_childs_calls_after_its_task_ended_or_its_chat_went_are_not_kept(state):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    _x(state, "UPDATE agent_task SET status = 'running' WHERE id = ?", task["id"])
    t = tools.Tools(state.db_path, CHAT, task["id"])
    t.record("counts", {}, "firms=3", 5, "ok")
    _x(state, "DELETE FROM agent_chat WHERE id = ?", CHAT)
    t.record("counts", {}, "firms=3", 5, "ok")
    _x(state, "INSERT INTO agent_chat (id, title, created_at, last_at, turn) "
              "VALUES (?, 'Din nou', 1, 1, 'turn-2')", CHAT)
    _x(state, "UPDATE agent_task SET status = 'cancelled' WHERE id = ?", task["id"])
    t.record("schema", {}, "", 5, "refused")
    assert _q(state, "SELECT tool FROM agent_tool_call WHERE turn = ?", task["turn"]) == [
        ("counts",)]


def test_a_problem_quoting_the_childs_answer_is_masked_and_bounded(state, fake):
    plan_id = _plan(state)
    task = _assign(state, plan_id)
    fake.answer(json.dumps({**RESEARCH, "office@alfa.example.com" + "x" * 600: 1}))
    _work(state)
    ended = _task(state, task["id"])
    assert ended["status"] == "invalid"
    assert "office@alfa.example.com" not in ended["problem"]
    assert len(ended["problem"]) < 120


def test_each_profiles_tools_are_exactly_these():
    # (A tool added to a profile is a decision: this list changes with it.)
    read = {"schema", "counts", "notes", "playbooks", "read_playbook"}
    assert tasks.PROFILES["researcher"].tools == read | {
        "firms", "coverage", "query", "estimate", "jobs", "areas", "plan_cells", "terms",
        "phrase_yield", "sets"}
    assert tasks.PROFILES["campaign_driver"].tools == read | {
        "campaigns", "writing_rules", "campaign_preview", "campaign_status", "replies",
        "edit_campaign"}
    assert set(tasks.PROFILES) == {"researcher", "campaign_driver"}


def test_a_task_runs_under_the_configured_login_without_inherited_credentials(
        state, fake, monkeypatch):
    from harvey import brain_auth

    for key in claude_runtime.CREDENTIAL_ENV:
        monkeypatch.setenv(key, "fake-credential-for-test")
    cfg = _config()
    cfg.brain.auth = "account"
    monkeypatch.setattr(chat, "_config", lambda: cfg)
    seen = []
    real_start = chat.Process.start

    async def start(cmd, cwd, env, folders):
        seen.append(dict(env))
        return await real_start(cmd, cwd, env, folders)
    monkeypatch.setattr(chat.Process, "start", start)
    plan_id = _plan(state)
    _assign(state, plan_id)
    assert _work(state).status == "done"
    [env] = seen
    assert not set(claude_runtime.CREDENTIAL_ENV) & set(env)
    assert env["CLAUDE_CONFIG_DIR"] == str(brain_auth.AUTH_DIR / "claude")
