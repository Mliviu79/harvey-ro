"""Harvey's agent woken under an approved plan (harvey/agent/wakes.py) and
what it remembers (harvey/agent/memory.py). The Claude CLI is a stand-in (a
small Python script speaking the stream-json lines the real CLI writes: no
model is called) and nothing else runs: the runs are rows, finished by
hand. A run finishing queues one wake for its approved plan and none for a
closed one; events coalesce into one wake; its answer lands in the chat
with why it woke, or nothing for [SILENT]; a chat in use, the plan's share
used, a logged-out CLI and the turn limit; a live chat told what a wake
wrote; schedule_wake's bounds; the memory's bounds, masking, and the
snapshot every new process is given; the Chat tab's words; the CLI. All
data is fictional."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import harvey.dashboard as dash
from harvey import claude_runtime, registers
from harvey.agent import chat, memory, plans, wakes
from harvey.brain import Brain
from harvey.config import load_config
from harvey.modules import jobs, worker
from harvey.state import StateManager

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "harvey" / "web"
CHAT = "chat-w"

# The CLI's stand-in: each user line is answered (a wake's one, or a chat's
# several) after one tool call recorded the way Harvey's server records it
# (under the chat's turn). FAKE_WAKE: silent answers [SILENT], login a
# logged-out CLI's result, loop tool calls until interrupted, die exits
# with an error, hang never answers. Every message
# is logged (FAKE_CLI_LOG) as one JSON line.
FAKE = r'''
import json, os, select, sys

args = sys.argv[1:]
cfg = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))
srv = cfg["mcpServers"]["harvey"]
sys.path.insert(0, srv["env"]["PYTHONPATH"])
db = srv["args"][srv["args"].index("--db") + 1]
chat = srv["args"][srv["args"].index("--chat") + 1]
mode = os.environ.get("FAKE_WAKE", "")


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
         "usage": {"input_tokens": 900, "output_tokens": 40},
         "modelUsage": {"claude-test": {"inputTokens": 900, "outputTokens": 40,
                                        "cacheReadInputTokens": 0,
                                        "cacheCreationInputTokens": 0, "costUSD": 0.0}}})


def tool_round(n, k):
    from harvey.agent import tools
    mid = f"m{n}-{k}"
    out({"type": "stream_event", "event": {"type": "message_start", "message": {
        "id": mid, "usage": {"input_tokens": 900, "cache_read_input_tokens": 100}}}})
    out({"type": "assistant", "message": {"id": mid, "content": [
        {"type": "tool_use", "id": f"t{n}-{k}", "name": "mcp__harvey__plan_status",
         "input": {"plan": 1}}]}})
    tools.Tools(db, chat).record("plan_status", {"plan": 1}, "runs: 1", 5, "ok")
    out({"type": "user", "message": {"content": [{"type": "tool_result",
                                                   "tool_use_id": f"t{n}-{k}"}]}})


n = 0
while True:
    line = readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("type") != "user":
        continue
    n += 1
    content = msg["message"]["content"]
    with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "n": n, "chat": chat, "content": content}) + "\n")
    if mode == "login":
        result("Not logged in · Please run /login", True)
        continue
    if mode == "die":
        sys.stderr.write("something broke\n")
        sys.exit(3)
    if mode == "hang":
        import time
        time.sleep(3600)
    if mode == "loop":
        k = 0
        while True:
            tool_round(n, k)
            k += 1
            got = readline(0.05)
            if got and json.loads(got).get("type") == "control_request":
                result("", True, "error_during_execution")
                break
        continue
    tool_round(n, 0)
    text = "[SILENT]" if mode == "silent" else f"Rularea s-a terminat: 2 firme noi ({n})."
    out({"type": "assistant", "message": {"id": f"m{n}-text", "content": [
        {"type": "text", "text": text}]}})
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
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.delenv("FAKE_WAKE", raising=False)
    monkeypatch.setattr(claude_runtime, "check_cli", lambda *a, **k: str(script))

    def lines():
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
    return lines


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    asyncio.run(s.init_db())
    con = sqlite3.connect(s.db_path)
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at) "
                "VALUES (?, 'Dentiști din Brașov', 1, 1)", (CHAT,))
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


def _plan(state, *, approve=True, budget=1.0, hours=72, chat_id=CHAT) -> int:
    async def go():
        p = await plans.propose(state, chat_id, "t0", "Contacte pentru dentiștii din Brașov",
                                ["Caut firmele", "Caut contactele"], budget, hours)
        if approve:
            await plans.decide(state, p["id"], True, plans.BY_DASHBOARD, budget_usd=budget)
        return p["id"]
    return _run(go())


def _a_run(state, plan_id, n_jobs=2, tool="find_contacts") -> tuple[str, list[str]]:
    """A run of the plan: a batch with ``n_jobs`` queued jobs, filed as the
    plan's action."""
    async def go():
        batch = await jobs.new_batch(state, tool, "Brașov, CAEN 8623")
        ids = []
        for i in range(n_jobs):
            ids.append(await jobs.enqueue(state, "find_contacts", "contacts",
                                          {"company_id": str(i)}, batch_id=batch,
                                          dedupe_key=f"t:{batch}:{i}"))
        async with state._connect() as db:
            await db.execute("INSERT INTO agent_action (plan, chat, turn, tool, arguments, batch, "
                             "cap_usd, classify_cap_usd, outcome, why, at) "
                             "VALUES (?, ?, 't0', ?, '{}', ?, 0.2, 0, 'ok', '', 1)",
                             (plan_id, CHAT, tool, batch))
            await db.commit()
        return batch, ids
    return _run(go())


def _finish(state, *ids, status="done"):
    for i in ids:
        _x(state, "UPDATE jobs SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
           status, i)


def _wake_jobs(state):
    return _q(state, "SELECT id, status, json_extract(params_json, '$.plan') FROM jobs "
                     "WHERE kind = 'agent_wake' ORDER BY rowid")


def _work_wake(state):
    """Claim the agent lane's next job and run it as the worker does: the
    job as it ended."""
    async def go():
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, wakes.AGENT_LANE, w.name)
        assert job is not None and job.kind == wakes.WAKE_KIND
        await w.run_job(job, {})
        return await jobs.get(state, job.id)
    return _run(go())


# ── What wakes it ──

def test_a_finished_run_queues_exactly_one_wake_for_its_approved_plan(state):
    plan_id = _plan(state)
    batch, ids = _a_run(state, plan_id)
    _finish(state, ids[0])
    assert _run(wakes.run_finished(state, batch)) == 0          # one job still to finish
    _finish(state, ids[1], status="failed")
    assert _run(wakes.run_finished(state, batch)) == 1
    # Seen finished again (the sweep, another job's end): still one event, one wake.
    assert _run(wakes.run_finished(state, batch)) == 0
    assert _run(wakes.sweep(state)) == 0
    [(kind, ref, data)] = _q(state, "SELECT kind, ref, data FROM agent_event")
    assert kind == "run_done" and ref.startswith(batch + "@")
    data = json.loads(data)
    assert data["run"] == batch and data["jobs"] == {"done": 1, "failed": 1}
    assert data["label"] == "Brașov, CAEN 8623" and data["tool"] == "find_contacts"
    [(job_id, status, plan)] = _wake_jobs(state)
    assert (status, plan) == ("queued", plan_id)
    assert _q(state, "SELECT lane, module, dedupe_key FROM jobs WHERE id = ?", job_id) == [
        ("agent", "agent", f"agent_wake:{plan_id}")]


@pytest.mark.parametrize("case", ["proposed", "done", "cancelled", "expired"])
def test_no_wake_for_a_plan_that_isnt_open(state, case):
    plan_id = _plan(state, approve=case != "proposed")
    batch, ids = _a_run(state, plan_id, n_jobs=1)
    if case in ("done", "cancelled"):
        _run(plans.close(state, plan_id, cancel=case == "cancelled", by=plans.BY_CLI))
    if case == "expired":
        _x(state, "UPDATE agent_plan SET expires_at = 1 WHERE id = ?", plan_id)
    _finish(state, *ids, status="cancelled" if case == "cancelled" else "done")
    assert _run(wakes.run_finished(state, batch)) == 0
    assert _run(wakes.sweep(state)) == 0
    assert _wake_jobs(state) == [] and _q(state, "SELECT COUNT(*) FROM agent_event") == [(0,)]


def test_a_run_cancelled_while_queued_is_found_by_the_sweep(state):
    plan_id = _plan(state)
    batch, ids = _a_run(state, plan_id)
    _run(plans.cancel_run(state, batch))
    assert _q(state, "SELECT status FROM jobs WHERE batch_id = ?", batch) == [("cancelled",)] * 2
    assert _run(wakes.sweep(state)) == 1
    assert len(_wake_jobs(state)) == 1


def test_events_coalesce_into_one_wake_that_reads_them_all(state, fake):
    plan_id = _plan(state)
    batch, ids = _a_run(state, plan_id, n_jobs=1)
    _finish(state, *ids)
    _run(wakes.run_finished(state, batch))
    # Another module's event (no schema change) and a second run, while the wake waits.
    _run(wakes.note(state, plan_id, "reply", "r1", {"intent": "interested"}))
    batch2, ids2 = _a_run(state, plan_id, n_jobs=1)
    _finish(state, *ids2)
    _run(wakes.run_finished(state, batch2))
    assert len(_wake_jobs(state)) == 1
    job = _work_wake(state)
    assert job.status == "done" and job.result["events"] == 3
    assert _q(state, "SELECT COUNT(*) FROM agent_event WHERE seen_at IS NULL") == [(0,)]
    sent = fake()[0]["content"]
    assert batch in sent and batch2 in sent and '"interested"' in sent
    # (Done: nothing new, so no next wake.)
    assert [s for _, s, _ in _wake_jobs(state)] == ["done"]


def test_a_finished_job_tells_the_plan_through_the_worker(state, monkeypatch):
    plan_id = _plan(state)

    async def step(ctx):
        return {"ok": True}
    # (Registered for this test only: every real kind is named on the Queue.)
    monkeypatch.setitem(jobs.KINDS, "test_step", jobs.Kind("test_step", step))

    async def go():
        batch = await jobs.new_batch(state, "find_contacts", "Brașov")
        job_id = await jobs.enqueue(state, "test_step", "contacts", {}, batch_id=batch)
        async with state._connect() as db:
            await db.execute("INSERT INTO agent_action (plan, chat, tool, arguments, batch, "
                             "cap_usd, classify_cap_usd, outcome, why, at) "
                             "VALUES (?, ?, 'find_contacts', '{}', ?, 0, 0, 'ok', '', 1)",
                             (plan_id, CHAT, batch))
            await db.commit()
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, "contacts", w.name)
        assert job.id == job_id
        await w.run_job(job, {})
    _run(go())
    assert len(_wake_jobs(state)) == 1


# ── A wake ──

def test_the_wakes_answer_lands_in_the_chat_with_why_it_woke(state, fake):
    plan_id = _plan(state)
    batch, ids = _a_run(state, plan_id, n_jobs=1)
    _x(state, "INSERT INTO agent_note (text, author, created_at, updated_at) "
              "VALUES ('Serper a dat puține firme noi în Brașov', 'agent', 1, 1)")
    _finish(state, *ids)
    _run(wakes.run_finished(state, batch))
    job = _work_wake(state)
    assert job.status == "done" and job.result["silent"] is False
    [sent] = fake()
    content = sent["content"]
    # The prompt, the memory, the plan and its events as data.
    assert content.startswith("You are Harvey's agent")
    assert "[1] Serper a dat puține firme noi în Brașov" in content
    assert f"Plan {plan_id} is approved" in content and "[SILENT]" in content
    assert "## Harvey woke you" in content and '"kind": "run_done"' in content
    [(turn, role, text, status, wake, tokens)] = _q(
        state, "SELECT turn, role, text, status, wake, tokens FROM agent_message")
    assert (role, status, text) == ("agent", "ok", "Rularea s-a terminat: 2 firme noi (1).")
    assert json.loads(wake) == {"kinds": ["run_done"], "runs": [batch],
                                "label": f"after run {batch} finished"}
    assert tokens == 940
    # Its tool calls under its turn; its usage as the agent's wake; the chat
    # marked unread, its turn given back.
    assert _q(state, "SELECT tool, outcome FROM agent_tool_call WHERE turn = ?", turn) == [
        ("plan_status", "ok")]
    assert _q(state, "SELECT agent, task, input_tokens FROM usage_events") == [
        ("agent", "wake", 900)]
    assert _q(state, "SELECT unread, turn FROM agent_chat") == [(1, "")]
    assert _q(state, "SELECT COUNT(*) FROM worker_lease WHERE name LIKE 'agent_chat:%'") == [(0,)]
    listed = _run(chat.chats(state))
    assert listed[0]["unread"] == 1 and listed[0]["answering"] is False
    shown = _run(chat.messages(state, CHAT))
    assert shown["messages"][0]["wake"]["runs"] == [batch]
    _run(chat.mark_read(state, CHAT))
    assert _q(state, "SELECT unread FROM agent_chat") == [(0,)]
    # The next new process sees the wake's answer in the conversation.
    primed = _run(chat.prime(state, CHAT, "t9", "Și acum?"))
    assert f"(Harvey woke on its own, after run {batch} finished" in primed
    assert "Harvey: Rularea s-a terminat" in primed and "(tool plan_status(" in primed


def test_silent_writes_no_message_but_keeps_its_tool_calls(state, fake, monkeypatch):
    monkeypatch.setenv("FAKE_WAKE", "silent")
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {"intent": "not_now"}))
    job = _work_wake(state)
    assert job.status == "done" and job.result["silent"] is True
    assert _q(state, "SELECT COUNT(*) FROM agent_message") == [(0,)]
    assert _q(state, "SELECT tool FROM agent_tool_call WHERE turn = ?", job.result["turn"]) == [
        ("plan_status",)]
    assert _q(state, "SELECT seen_at IS NOT NULL, wake FROM agent_event") == [
        (1, job.result["turn"])]
    assert _q(state, "SELECT unread FROM agent_chat") == [(0,)]


def test_a_live_chat_holds_the_wake_off_without_a_failure(state, fake):
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))

    async def hold():
        await worker.acquire_lease(state, "chat-live-owner", chat.LEASE_TTL_SECONDS,
                                   chat._lease(CHAT))
    _run(hold())
    job = _work_wake(state)
    assert job.status == "queued" and job.attempts == 0 and job.not_before
    assert "in use" in job.error
    assert fake() == [] and _q(state, "SELECT seen_at FROM agent_event") == [(None,)]


def test_over_the_plans_share_the_wake_waits_and_the_lane_rests(state, fake, monkeypatch):
    async def over(self, *a, **k):
        return False
    monkeypatch.setattr(Brain, "is_within_budget", over)
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work_wake(state)
    assert job.status == "queued" and job.attempts == 0 and job.not_before
    assert fake() == []
    held = _run(wakes.held(state))
    assert held and "usage.max_daily_claude_percent" in held["why"] and not held["paused"]


def test_a_logged_out_cli_pauses_the_agents_lane(state, fake, monkeypatch):
    monkeypatch.setenv("FAKE_WAKE", "login")
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work_wake(state)
    assert job.status == "queued" and job.attempts == 0
    paused = _run(jobs.lane_paused(state, wakes.AGENT_LANE))
    assert "claude login" in paused
    assert _q(state, "SELECT COUNT(*) FROM agent_message") == [(0,)]
    assert _q(state, "SELECT seen_at FROM agent_event") == [(None,)]
    assert _run(wakes.held(state))["paused"] is True


def test_a_failed_wake_keeps_its_events_and_the_next_waits(state, fake, monkeypatch):
    monkeypatch.setenv("FAKE_WAKE", "die")
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work_wake(state)
    assert job.status == "failed" and "exit 3" in job.error and "something broke" in job.error
    assert _q(state, "SELECT seen_at FROM agent_event") == [(None,)]
    # (Counted as a call all the same.)
    assert _q(state, "SELECT agent, task, is_error FROM usage_events") == [("agent", "wake", 1)]
    # Not again at once: the sweep waits FAILED_RETRY_SECONDS.
    assert _run(wakes.sweep(state)) == 0 and len(_wake_jobs(state)) == 1
    _x(state, "UPDATE jobs SET finished_at = datetime('now', '-1 hour') WHERE id = ?", job.id)
    assert _run(wakes.sweep(state)) == 1


def test_the_turn_limit_stops_a_wake_and_says_so(state, fake, monkeypatch):
    monkeypatch.setenv("FAKE_WAKE", "loop")
    monkeypatch.setattr(chat, "MAX_TURNS", 3)
    monkeypatch.setattr(chat, "STOP_GRACE_SECONDS", 3.0)
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work_wake(state)
    assert job.status == "done"
    assert _q(state, "SELECT status, text FROM agent_message") == [
        ("error:max_turns", "stopped after 3 turns without an answer")]
    assert _q(state, "SELECT seen_at IS NOT NULL FROM agent_event") == [(1,)]


def test_codex_and_the_days_wakes_queue_nothing(state, monkeypatch):
    plan_id = _plan(state)
    monkeypatch.setattr(chat, "_config", lambda: _config("codex"))
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    assert _wake_jobs(state) == []
    monkeypatch.setattr(chat, "_config", lambda: _config())
    for i in range(wakes.WAKES_PER_DAY):
        _x(state, "INSERT INTO jobs (id, kind, lane, dedupe_key, status) "
                  "VALUES (?, 'agent_wake', 'agent', ?, 'done')", f"old{i}", f"agent_wake:{plan_id}")
    assert _run(wakes.enqueue(state, plan_id)) is None
    _x(state, "DELETE FROM jobs WHERE id = 'old0'")
    assert _run(wakes.enqueue(state, plan_id))


def test_a_live_chat_is_told_what_a_wake_wrote_before_its_next_message(state, fake):
    async def work():
        h = chat.hub(state)
        try:
            a = await h.ask(CHAT, "Câte firme am găsit?")
            [e async for e in a.follow()]
            # (A wake answered meanwhile: its lease was lost and taken.)
            con = sqlite3.connect(state.db_path)
            con.execute("INSERT INTO agent_message (turn, role, text, status, chat, created_at, "
                        "wake) VALUES ('w1', 'agent', 'Rularea b1 a găsit 4 firme.', 'ok', ?, 2, "
                        "?)", (CHAT, json.dumps({"kinds": ["run_done"], "runs": ["b1"],
                                                 "label": "after run b1 finished"})))
            con.commit()
            con.close()
            b = await h.ask(CHAT, "Și cu email?")
            [e async for e in b.follow()]
            c = await h.ask(CHAT, "Mulțumesc")
            [e async for e in c.follow()]
        finally:
            await chat.close_all()
    _run(work())
    sent = [x["content"] for x in fake()]
    assert len({x["pid"] for x in fake()}) == 1 and len(sent) == 3
    assert sent[1].startswith("## Written in this chat since your last message")
    assert "after run b1 finished" in sent[1] and "Harvey: Rularea b1 a găsit 4 firme." in sent[1]
    assert sent[1].rstrip().endswith("## The user's new message\n\nȘi cu email?")
    # (Told once.)
    assert sent[2] == "Mulțumesc"
    # The user's message is stored as typed.
    assert ("Și cu email?",) in _q(state, "SELECT text FROM agent_message WHERE role = 'user'")


def test_a_wake_cut_off_ends_its_process_and_counts_the_call(state, fake, monkeypatch):
    import os

    monkeypatch.setenv("FAKE_WAKE", "hang")
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))

    async def go():
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, wakes.AGENT_LANE, w.name)
        task = asyncio.ensure_future(w.run_job(job, {}))
        for _ in range(200):
            if fake():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await jobs.get(state, job.id)
    job = _run(go())
    [sent] = fake()
    # The CLI is gone, its job back in the queue, the chat free again.
    with pytest.raises(ProcessLookupError):
        os.kill(sent["pid"], 0)
    assert job.status == "queued"
    assert _q(state, "SELECT agent, task, is_error FROM usage_events") == [("agent", "wake", 1)]
    assert _q(state, "SELECT COUNT(*) FROM worker_lease WHERE name LIKE 'agent_chat:%'") == [(0,)]
    assert _q(state, "SELECT turn FROM agent_chat") == [("",)]
    assert _q(state, "SELECT seen_at FROM agent_event") == [(None,)]


def test_a_wake_the_user_cancelled_doesnt_come_back(state):
    plan_id = _plan(state)
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    [(job_id, _, _)] = _wake_jobs(state)
    _run(jobs.cancel(state, job_id))
    _x(state, "UPDATE jobs SET finished_at = datetime('now', '+2 seconds') WHERE id = ?", job_id)
    # The sweep, a minute later: the events it would have read stay set aside.
    assert _run(wakes.sweep(state)) == 0 and len(_wake_jobs(state)) == 1
    assert _q(state, "SELECT cancelled_at IS NOT NULL FROM agent_event") == [(1,)]
    # What comes after the cancel wakes the plan again.
    _x(state, "UPDATE jobs SET finished_at = datetime('now', '-1 minute') WHERE id = ?", job_id)
    _run(wakes.note(state, plan_id, "reply", "r2", {}))
    assert [s for _, s, _ in _wake_jobs(state)] == ["cancelled", "queued"]


def test_a_wake_in_the_chat_refuses_a_message_and_a_delete_for_a_moment(state):
    async def go():
        await worker.acquire_lease(state, wakes.OWNER_PREFIX + "j1", chat.LEASE_TTL_SECONDS,
                                   chat._lease(CHAT))
        _x(state, "UPDATE agent_chat SET turn = 'w1' WHERE id = ?", CHAT)
        shown = await chat.messages(state, CHAT)
        with pytest.raises(chat.Refused) as delete:
            await chat.hub(state).delete(CHAT)
        await chat.close_all()
        return shown, delete.value
    shown, delete = _run(go())
    assert shown["busy"] and shown["waking"]
    assert delete.code == "waking" and "wake" in str(delete)


# ── Scheduling and cancelling ──

def test_schedule_wake_is_bounded(state):
    plan_id = _plan(state, hours=5)
    other = _plan(state, approve=False)

    def ask(**kw):
        return _run(wakes.schedule(state, kw.pop("plan", plan_id), kw.pop("chat", CHAT), **kw))
    for kw, why in (({"in_hours": 0.05}, "at least 10 minutes"),
                    ({"in_hours": 6}, "closes in"),
                    ({}, "one of the two"), ({"in_hours": 1, "at": "2030-01-01"}, "one of the two"),
                    ({"at": "soon"}, "ISO date"),
                    ({"plan": other, "in_hours": 1}, "waits for the user's approval"),
                    ({"chat": "chat-z", "in_hours": 1}, "no plan")):
        with pytest.raises(wakes.Refused) as e:
            ask(**kw)
        assert why in str(e.value), kw
    first = ask(in_hours=1, note_text="Verifică rularea; scrie la office@alfa.example.com")
    assert first["due_at"] >= int(time.time()) + 3500
    [(data,)] = _q(state, "SELECT data FROM agent_event WHERE kind = 'scheduled'")
    assert json.loads(data)["note"] == "Verifică rularea; scrie la o***@alfa.example.com"
    ask(at=int(time.time()) + 7200)
    ask(at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 9000)))
    with pytest.raises(wakes.Refused) as e:
        ask(in_hours=2)
    assert "3 wakes waiting" in str(e.value)
    # Not due yet: nothing is queued, the card lists them.
    assert _wake_jobs(state) == []
    pending = _run(wakes.pending(state, plan_id))
    assert len(pending["scheduled"]) == 3 and pending["job"] is None
    cards = _run(plans.for_chat(state, CHAT))
    assert len(next(p for p in cards if p["id"] == plan_id)["wakes"]["scheduled"]) == 3
    # The user cancels one; come due, the next is queued by the sweep.
    _run(wakes.cancel_event(state, pending["scheduled"][0]["id"]))
    assert len(_run(wakes.pending(state, plan_id))["scheduled"]) == 2
    _x(state, "UPDATE agent_event SET due_at = 1 WHERE kind = 'scheduled' AND cancelled_at IS NULL")
    assert _run(wakes.sweep(state)) == 1 and len(_wake_jobs(state)) == 1


def test_closing_a_plan_or_deleting_its_chat_cancels_its_wakes(state):
    plan_id = _plan(state)
    _run(wakes.schedule(state, plan_id, CHAT, in_hours=2))
    _run(wakes.note(state, plan_id, "reply", "r1", {}))
    assert [s for _, s, _ in _wake_jobs(state)] == ["queued"]
    _run(plans.close(state, plan_id, cancel=False, by=plans.BY_CLI))
    assert [s for _, s, _ in _wake_jobs(state)] == ["cancelled"]
    assert _q(state, "SELECT COUNT(*) FROM agent_event WHERE cancelled_at IS NULL") == [(0,)]
    second = _plan(state)
    _run(wakes.schedule(state, second, CHAT, in_hours=2))

    async def delete():
        try:
            await chat.hub(state).delete(CHAT)
        finally:
            await chat.close_all()
    _run(delete())
    assert _q(state, "SELECT COUNT(*) FROM agent_event WHERE cancelled_at IS NULL") == [(0,)]


def test_the_tools_schedule_a_wake_and_keep_memory(state):
    from mcp import Client

    from harvey.agent import tools

    plan_id = _plan(state)
    _x(state, "UPDATE agent_chat SET turn = 't5' WHERE id = ?", CHAT)

    async def run():
        out = []
        async with Client(tools.build(state.db_path, CHAT)) as c:
            for name, args in (("schedule_wake", {"plan": plan_id, "in_hours": 2,
                                                  "note": "Verifică rularea"}),
                               ("schedule_wake", {"plan": plan_id, "in_hours": 0}),
                               ("remember", {"text": "Telefonul +40722000102 nu răspunde"}),
                               ("notes", {}), ("save_playbook", {"name": "Dentiști",
                                                                 "body": "1. registry_pull"}),
                               ("playbooks", {}), ("read_playbook", {"name": "Dentiști"}),
                               ("forget", {"id": 1}), ("forget", {"id": 1}),
                               ("plan_status", {"plan": plan_id})):
                r = await c.call_tool(name, args)
                out.append((name, r.is_error, r.content[0].text))
        return out
    answers = _run(run())
    errors = [(n, e) for n, e, _ in answers]
    assert errors == [("schedule_wake", False), ("schedule_wake", True), ("remember", False),
                      ("notes", False), ("save_playbook", False), ("playbooks", False),
                      ("read_playbook", False), ("forget", False), ("forget", True),
                      ("plan_status", False)]
    got = {n: t for n, e, t in answers if not e}
    assert json.loads(got["notes"])["notes"][0]["text"] == "Telefonul +4072**** nu răspunde"
    assert json.loads(got["playbooks"])["playbooks"][0]["name"] == "Dentiști"
    assert json.loads(got["read_playbook"])["body"] == "1. registry_pull"
    status = json.loads(got["plan_status"])
    assert len(status["wakes"]["scheduled"]) == 1 and status["wakes"]["unseen"] == {}
    assert _q(state, "SELECT tool, outcome FROM agent_action ORDER BY id") == [
        ("schedule_wake", "ok"), ("schedule_wake", "refused")]
    assert ("remember", "ok") in _q(state, "SELECT tool, outcome FROM agent_tool_call "
                                           "WHERE turn = 't5'")


# ── Memory ──

def test_the_agent_keeps_to_what_the_user_wrote_in_its_memory(state):
    from mcp import Client

    from harvey.agent import tools

    mine = _run(memory.remember(state, "Brașov: OSM a dat 12 firme noi"))
    theirs = _run(memory.remember(state, "Nu folosi surse plătite fără să întrebi",
                                  author=memory.BY_DASHBOARD))
    _run(memory.save_playbook(state, "Dentiști", "pașii utilizatorului",
                              author=memory.BY_DASHBOARD))

    async def run():
        out = []
        async with Client(tools.build(state.db_path, CHAT)) as c:
            for name, args in (("forget", {"id": theirs["id"]}),
                               ("save_playbook", {"name": "Dentiști", "body": "alți pași"}),
                               ("forget", {"id": mine["id"]})):
                r = await c.call_tool(name, args)
                out.append((name, r.is_error, r.content[0].text))
        return out
    answers = _run(run())
    assert [e for _, e, _ in answers] == [True, True, False]
    assert "the user's" in answers[0][2] and "the user's" in answers[1][2]
    assert [n["id"] for n in _run(memory.notes(state))] == [theirs["id"]]
    assert _run(memory.read_playbook(state, "Dentiști"))["body"] == "pașii utilizatorului"
    # The user's note is given to a new process as theirs.
    primed = _run(chat.prime(state, CHAT, "t1", "Salut"))
    assert f"- [{theirs['id']}] (the user's) Nu folosi surse plătite" in primed
    # The user removes their own.
    _run(memory.forget(state, theirs["id"]))


def test_a_playbooks_name_and_a_wakes_note_are_masked_too(state):
    p = _run(memory.save_playbook(state, "Sună la 0722 000 102", "pași"))
    assert p["name"] == "Sună la 0722****" and "000 102" not in _run(memory.snapshot(state))
    plan_id = _plan(state)
    _run(wakes.schedule(state, plan_id, CHAT, in_hours=1,
                        note_text="Sună la 0722 000 102 după rulare"))
    [(data,)] = _q(state, "SELECT data FROM agent_event WHERE kind = 'scheduled'")
    assert json.loads(data)["note"] == "Sună la 0722**** după rulare"

def test_notes_are_bounded_masked_and_given_to_every_new_process(state):
    n = _run(memory.remember(state, "Scrie la ana.pop@beta.example.com sau la 0722 000 102"))
    assert n["text"] == "Scrie la a***@beta.example.com sau la 0722****"
    with pytest.raises(memory.Refused) as e:
        _run(memory.remember(state, "x" * (memory.NOTE_CHARS + 1)))
    assert "at most 300" in str(e.value)
    with pytest.raises(memory.Refused):
        _run(memory.remember(state, "   "))
    while True:
        try:
            _run(memory.remember(state, "y" * 290))
        except memory.Refused as e:
            assert "2,200 characters together" in str(e) and "forget one" in str(e)
            break
    assert _run(memory.usage(state))["chars"] <= memory.NOTES_CHARS
    # An edit within the room left by the note itself.
    edited = _run(memory.edit_note(state, n["id"], "Brașov: OSM a dat 12 firme noi"))
    assert edited["author"] == "dashboard"
    _run(memory.save_playbook(state, "Dentiști pe județ", "1. registry_pull pe CAEN 8623\n2. ..."))
    primed = _run(chat.prime(state, CHAT, "t1", "Salut"))
    assert f"- [{n['id']}] (the user's) Brașov: OSM a dat 12 firme noi" in primed
    assert "Your playbooks (read_playbook(name) reads one): Dentiști pe județ" in primed
    assert "registry_pull pe CAEN 8623" not in primed
    assert primed.index("## Your memory") < primed.index("## The user's new message")
    _run(memory.forget(state, n["id"]))
    with pytest.raises(memory.Refused):
        _run(memory.forget(state, n["id"]))


def test_playbooks_are_bounded_and_replaced_by_name(state):
    _run(memory.save_playbook(state, "Dentiști", "unu"))
    p = _run(memory.save_playbook(state, " Dentiști ", "doi"))
    assert p["body"] == "doi" and len(_run(memory.playbooks(state))) == 1
    with pytest.raises(memory.Refused) as e:
        _run(memory.save_playbook(state, "Lung", "z" * (memory.PLAYBOOK_CHARS + 1)))
    assert "4,000" in str(e.value)
    with pytest.raises(memory.Refused):
        _run(memory.read_playbook(state, "Nu există"))
    other = _run(memory.save_playbook(state, "Clinici", "trei"))
    with pytest.raises(memory.Refused) as e:
        _run(memory.edit_playbook(state, other["id"], name="Dentiști"))
    assert "another playbook" in str(e.value)
    _run(memory.delete_playbook(state, other["id"]))
    assert [x["name"] for x in _run(memory.playbooks(state))] == ["Dentiști"]


def test_memory_endpoints(state, monkeypatch):
    monkeypatch.setattr(dash, "_state", lambda: state)

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    async def go():
        added = await dash.add_agent_note(Req({"text": "Clienții preferă emailul"}))
        nid = added["note"]["id"]
        bad = await dash.add_agent_note(Req({"text": ""}))
        await dash.edit_agent_note(nid, Req({"text": "Clienții preferă telefonul"}))
        await memory.save_playbook(state, "Cluj", "pași")
        got = await dash.get_agent_memory()
        pid = got["playbooks"][0]["id"]
        await dash.edit_agent_playbook(pid, Req({"body": "pași noi"}))
        after = await dash.get_agent_memory()
        await dash.delete_agent_playbook(pid)
        await dash.delete_agent_note(nid)
        return added, bad, got, after, await dash.get_agent_memory()
    added, bad, got, after, gone = _run(go())
    assert added["success"] and bad.status_code == 400
    assert got["notes"][0]["text"] == "Clienții preferă telefonul"
    assert got["usage"]["limit"] == memory.NOTES_CHARS
    assert after["playbooks"][0]["body"] == "pași noi"
    assert gone["notes"] == [] and gone["playbooks"] == []


# ── The Chat tab and the CLI ──

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


RENDER = r'''
const fs = require('fs'), vm = require('vm');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
global.document = {getElementById: () => null, querySelectorAll: () => [], addEventListener() {},
                   documentElement: {dataset: {}, setAttribute() {}}, body: {}};
global.window = {addEventListener() {}, location: {hash: ''}};
global.sessionStorage = {getItem: () => null, setItem() {}};
const src = fs.readFileSync(process.argv[1], 'utf8');
const pick = name => { const i = src.indexOf('function ' + name + '('); let d = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) { if (src[k] === '{') d++; else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1); } };
for (const n of ['escHtml', 'icon', 'own', 'emptyState', 'chatWhen', 'renderChat', 'chatPending',
                 'chatCalls', 'chatLiveHtml', 'chatRetry', 'renderChatList', 'wakeLabel',
                 'renderPlanCard', 'planBadge', 'planWakes', 'renderMemory'])
  vm.runInThisContext(pick(n));
vm.runInThisContext(src.match(/const CHAT_ERRORS = \{[^}]*\};/)[0]);
vm.runInThisContext(src.match(/const PLAN_TONE = \{[^}]*\};/)[0]);
global.relTime = () => 'now';
global.fmtUsd = (n, d) => '$' + Number(n).toFixed(d);
global.fmtN = n => String(n);
global.intlLocale = () => 'en';
const data = {messages: [
  {id: 1, turn: 'w1', role: 'agent', text: 'Rularea a găsit <b>4</b> firme.', status: 'ok', created_at: 1,
   wake: {kinds: ['run_done', 'scheduled'], runs: ['b1'], label: 'x'}},
  {id: 2, turn: 'w2', role: 'agent', text: 'Un răspuns nou.', status: 'ok', created_at: 2,
   wake: {kinds: ['reply'], runs: [], label: 'after reply'}}], calls: {},
  plans: [{id: 7, turn: 'w1', goal: 'Dentiști', steps: ['a'], budget_usd: 1, status: 'approved',
           expires_at: 2000000000, committed_usd: 0, spent_usd: 0, remaining_usd: 1, runs: [],
           wakes: {scheduled: [{id: 3, due_at: 1900000000, note: 'Verifică <i>rularea</i>'}],
                   unseen: {}, job: {id: 'j', status: 'queued'}}}], waking: true};
const list = renderChatList([{id: 'a', title: 'Cluj', last_at: 5, unread: 2, answering: false}], '', '');
const mem = renderMemory({usage: {chars: 30, limit: 2200, note_limit: 300},
  notes: [{id: 1, text: '<s>OSM</s> a mers', author: 'agent', updated_at: 1}],
  playbooks: [{id: 2, name: 'Dentiști', body: 'pași <u>', author: 'dashboard', updated_at: 1}],
  playbook_limit: 4000});
process.stdout.write(JSON.stringify({html: renderChat(data, null), list: list, mem: mem,
  empty: renderMemory({usage: {}, notes: [], playbooks: []})}));
'''


@pytest.mark.parametrize("lang, words", [
    ("ro", {"run": "Harvey, după ce rularea b1 s-a terminat; la ora pe care și-a programat-o",
            "reply": "Harvey, după reply", "unread": "nou", "waking": "Harvey se uită acum",
            "wakes": "Treziri programate", "queued": "O trezire e în coadă", "cancel": "Anulează",
            "notes": "Note", "by_agent": "scrisă de agent", "none": "Nicio notă încă."}),
    ("en", {"run": "Harvey, after run b1 finished; at the time it scheduled",
            "reply": "Harvey, after reply", "unread": "new", "waking": "Harvey is looking at",
            "wakes": "Scheduled wakes", "queued": "A wake is queued", "cancel": "Cancel",
            "notes": "Notes", "by_agent": "written by the agent", "none": "No notes yet."}),
])
def test_the_chat_tab_shows_wakes_and_memory_in_both_languages(lang, words):
    res = subprocess.run([_node(), "-e", RENDER, str(WEB / "app.js"), str(WEB / "i18n.js"), lang],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-2000:]
    out = json.loads(res.stdout)
    html = out["html"]
    assert words["run"] in html and words["reply"] in html and "chat-msg agent wake" in html
    assert "&lt;b&gt;4&lt;/b&gt;" in html and words["waking"] in html
    assert words["wakes"] in html and words["queued"] in html and words["cancel"] in html
    assert 'onclick="cancelWake(this.dataset.wake)"' in html and 'data-wake="3"' in html
    assert "&lt;i&gt;rularea&lt;/i&gt;" in html
    assert words["unread"] in out["list"]
    mem = out["mem"]
    assert words["notes"] in mem and words["by_agent"] in mem
    assert "&lt;s&gt;OSM&lt;/s&gt;" in mem and "pași &lt;u&gt;" in mem
    assert "saveNote(" in mem and "deletePlaybook(" in mem and words["none"] in out["empty"]


def test_the_cli_shows_and_edits_the_memory_and_labels_a_wakes_answer(state, monkeypatch,
                                                                      capsys):
    from harvey import cli
    import harvey.state as state_mod

    monkeypatch.setattr(state_mod, "DB_PATH", state.db_path)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["harvey", "agent", *argv])
        cli.main()
    run("memory", "add", "Brașov:", "OSM", "a", "dat", "12", "firme")
    run("memory")
    out = capsys.readouterr().out
    assert "Note 1 kept." in out and "Brașov: OSM a dat 12 firme" in out and "of 2,200" in out
    _run(memory.save_playbook(state, "Dentiști", "1. registry_pull"))
    run("memory", "playbooks")
    run("memory", "playbook", "Dentiști")
    out = capsys.readouterr().out
    assert "Dentiști" in out and "1. registry_pull" in out
    run("memory", "forget", "1")
    run("memory", "delete-playbook", "Dentiști")
    assert "Forgotten." in capsys.readouterr().out
    assert _run(memory.notes(state)) == [] and _run(memory.playbooks(state)) == []
    _x(state, "INSERT INTO agent_message (turn, role, text, status, chat, created_at, wake) "
              "VALUES ('w1', 'agent', 'Gata.', 'ok', ?, 1, ?)",
       CHAT, json.dumps({"kinds": ["scheduled"], "runs": [], "label": "scheduled"}))
    run("log", "--chat", CHAT)
    assert "Harvey (woke scheduled)" in capsys.readouterr().out
