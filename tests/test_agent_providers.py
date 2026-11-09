"""Claude and Codex set up side by side (harvey/config.py's per-provider
blocks, harvey/agent/chat.py's agent_chat.provider): each chat runs on its
own provider whatever the default is, can be moved between messages (never
mid-answer), its plan's wakes and tasks run on it, and what holds one
provider holds only the work on it. The Claude CLI and Codex are the test
suite's stand-ins (tests/test_agent_chat.py's script, tests/fake_codex_agent.py):
no model, no network, no login. All data is fictional."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harvey import brain_spend, claude_runtime, codex, registers
from harvey.agent import chat, plans, tasks, wakes
from harvey.brain import Brain
from harvey.config import load_config
from harvey.modules import jobs, worker
from harvey.state import StateManager
from tests.test_agent_chat import FAKE as CLAUDE_FAKE
from tests.test_agent_codex import FAKE as CODEX_FAKE, RESEARCH, Fake, _jwt

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the Codex provider isn't supported on Windows")

ROOT = Path(__file__).resolve().parents[1]
CHAT = "chat-p"


def _config(default: str = "claude"):
    cfg = load_config(str(ROOT / "harvey.yaml"))
    cfg.brain.provider = default
    # (Each provider's own model: Codex needs one.)
    cfg.brain.codex.model = "gpt-5.5"
    return cfg


@pytest.fixture
def default(monkeypatch):
    """Which provider is the default for this test (claude unless set)."""
    picked = {"p": "claude"}
    monkeypatch.setattr(chat, "_config", lambda: _config(picked["p"]))
    return picked


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch, default):
    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [])

    async def within(self, *a, **k):
        return True
    monkeypatch.setattr(Brain, "is_within_budget", within)
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(runtimes))


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """The Claude CLI's stand-in, and what it was given (one JSON line each)."""
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + CLAUDE_FAKE, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "fake-cli.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)
    monkeypatch.setattr(claude_runtime, "check_cli", lambda *a, **k: str(script))

    def lines():
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
    return lines


@pytest.fixture
def codex_cli(tmp_path, monkeypatch):
    """Codex's stand-in and the ChatGPT login it is given a copy of."""
    fake = Fake(tmp_path)
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{CODEX_FAKE}" '
                       f'"{fake.scenario_file}" "{fake.log}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(codex, "find_codex", lambda: str(wrapper))
    home = tmp_path / "native-codex"
    home.mkdir()
    stamp = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": _jwt({"email": "x@example.com"}),
                   "access_token": _jwt({"exp": time.time() + 86400}),
                   "refresh_token": "native-refresh", "account_id": "acct-1"},
        "last_refresh": stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}))
    (home / "auth.json").chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return fake


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    asyncio.run(s.init_db())
    return s


def _q(state, sql, *args):
    con = sqlite3.connect(state.db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _hub(state, work, timeout=60):
    async def main():
        h = chat.hub(state)
        try:
            return await asyncio.wait_for(work(h), timeout)
        finally:
            await chat.close_all()
    return asyncio.run(main())


async def _say(h, chat_id, text, provider=None):
    answer = await h.ask(chat_id, text, provider=provider)
    return [e async for e in answer.follow()]


# ── A chat on either provider, whatever the default ──

def test_a_chat_on_codex_while_claude_is_the_default_starts_codex(state, claude, codex_cli):
    async def work(h):
        events = await _say(h, None, "Câte firme din Cluj au telefon?", provider="codex")
        return events, await chat.chat_info(state, events[0]["chat"])
    events, info = _hub(state, work)
    assert events[-1]["type"] == "done" and events[-1]["text"] == "Mesajul 1: trei firme."
    assert info["provider"] == "codex"
    assert claude() == []                                  # Claude never started
    assert [s["has_auth"] for s in codex_cli.servers()] == [False, True]
    assert _q(state, "SELECT DISTINCT provider FROM usage_events") == [("codex",)]


def test_a_chat_on_claude_while_codex_is_the_default_starts_claude(state, claude, codex_cli,
                                                                   default):
    default["p"] = "codex"

    async def work(h):
        on_claude = await _say(h, None, "Câte firme?", provider="claude")
        on_default = await _say(h, None, "Și pe Codex?")
        return on_claude, on_default
    on_claude, on_default = _hub(state, work)
    assert on_claude[-1]["type"] == "done" and on_default[-1]["type"] == "done"
    rows = dict(_q(state, "SELECT id, provider FROM agent_chat"))
    assert rows == {on_claude[0]["chat"]: "claude", on_default[0]["chat"]: "codex"}
    assert [x["chat"] for x in claude() if x.get("start")] == [on_claude[0]["chat"]]
    assert len(codex_cli.servers()) == 2                    # one runtime, attested then used


def test_a_provider_that_isnt_one_is_refused(state, claude):
    async def work(h):
        with pytest.raises(chat.Refused) as e:
            await h.ask(None, "Salut", provider="gemini")
        return e.value
    assert _hub(state, work).code == "provider"
    assert _q(state, "SELECT COUNT(*) FROM agent_chat") == [(0,)] and claude() == []


def test_a_chat_made_before_follows_the_default(state, claude, codex_cli, default):
    # (Schema 43 left NULL for the chats on file: the default at the time.)
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at) "
                "VALUES (?, 'Mai demult', 1, 1)", (CHAT,))
    con.commit()
    con.close()
    default["p"] = "codex"

    async def work(h):
        return await _say(h, CHAT, "Câte firme?")
    assert _hub(state, work)[-1]["type"] == "done"
    assert claude() == [] and codex_cli.servers()
    assert _q(state, "SELECT provider FROM agent_chat") == [(None,)]


# ── Moving a chat between messages ──

def test_moving_a_chat_primes_the_new_provider_from_its_history(state, claude, codex_cli):
    async def work(h):
        first = await _say(h, None, "Câte firme din Brașov au telefon?")
        cid = first[0]["chat"]
        moved = await h.set_provider(cid, "codex")
        second = await _say(h, cid, "Și cu email?")
        back = await h.set_provider(cid, "claude")
        third = await _say(h, cid, "Și fără site?")
        return cid, first, moved, second, back, third
    cid, first, moved, second, back, third = _hub(state, work)
    assert first[-1]["type"] == second[-1]["type"] == third[-1]["type"] == "done"
    assert moved["provider"] == "codex" and back["provider"] == "claude"
    # Codex's process was given the prompt and the chat so far.
    [prompt] = codex_cli.prompts()
    assert prompt.startswith("You are Harvey's agent")
    assert "Câte firme din Brașov au telefon?" in prompt and "Și cu email?" in prompt
    # Back on Claude: a new process, primed with both earlier exchanges.
    started = [x for x in claude() if x.get("start")]
    assert len(started) == 2
    primed = [x for x in claude() if x.get("content")][-1]["content"]
    assert "Câte firme din Brașov" in primed and "Și cu email?" in primed
    assert sorted(p for (p,) in _q(state, "SELECT provider FROM usage_events")) == [
        "claude", "claude", "codex"]


def test_moving_is_refused_mid_answer_and_for_a_provider_that_cant_run(state, claude,
                                                                       monkeypatch):
    async def work(h):
        answer = await h.ask(None, "SLOW")
        cid = answer.chat
        with pytest.raises(chat.Refused) as busy:
            await h.set_provider(cid, "codex")
        await h.stop(cid)
        [e async for e in answer.follow()]
        monkeypatch.setattr(codex, "_PLATFORM", "win32")
        with pytest.raises(chat.Refused) as where:
            await h.set_provider(cid, "codex")
        return cid, busy.value, where.value
    cid, busy, where = _hub(state, work)
    assert busy.code == "busy" and where.code == "codex" and "Windows" in str(where)
    assert _q(state, "SELECT provider FROM agent_chat WHERE id = ?", cid) == [("claude",)]


def test_moving_to_codex_with_no_codex_cli_is_refused_with_why(state, claude, monkeypatch):
    monkeypatch.setattr(codex, "find_codex", lambda: None)

    async def work(h):
        first = await _say(h, None, "Câte firme?")
        with pytest.raises(chat.Refused) as e:
            await h.set_provider(first[0]["chat"], "codex")
        return first[0]["chat"], e.value
    cid, refused = _hub(state, work)
    assert refused.code == "cli" and "Codex CLI not found" in str(refused)
    assert _q(state, "SELECT provider FROM agent_chat WHERE id = ?", cid) == [("claude",)]


# ── A plan's wakes and tasks run on its chat's provider ──

def _plan(state, chat_id: str = CHAT, provider: str | None = "codex") -> int:
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at, provider) "
                "VALUES (?, 'Dentiști din Brașov', 1, 1, ?)", (chat_id, provider))
    con.commit()
    con.close()

    async def go():
        p = await plans.propose(state, chat_id, "t0", "Contacte pentru dentiștii din Brașov",
                                ["Caut firmele"], 1.0, 72)
        await plans.decide(state, p["id"], True, plans.BY_DASHBOARD, budget_usd=1.0)
        return p["id"]
    return asyncio.run(go())


def _work(state, kind):
    async def go():
        w = worker.Worker(state, name="w-test")
        job = await jobs.claim(state, wakes.AGENT_LANE, w.name)
        assert job is not None and job.kind == kind
        await w.run_job(job, {})
        return await jobs.get(state, job.id)
    return asyncio.run(go())


def test_a_wake_runs_on_its_chats_provider_not_the_default(state, codex_cli, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the Claude CLI was looked for")
    monkeypatch.setattr(claude_runtime, "check_cli", refuse)
    codex_cli.use("silent")
    plan_id = _plan(state)
    asyncio.run(wakes.note(state, plan_id, "reply", "r1", {"intent": "not_now"}))
    job = _work(state, wakes.WAKE_KIND)
    assert job.status == "done" and job.result["silent"] is True
    assert _q(state, "SELECT provider, task FROM usage_events") == [("codex", "wake")]


def test_a_task_runs_on_its_chats_provider_not_the_default(state, codex_cli, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the Claude CLI was looked for")
    monkeypatch.setattr(claude_runtime, "check_cli", refuse)
    codex_cli.use("task", json.dumps(RESEARCH, ensure_ascii=False))
    plan_id = _plan(state)
    task = asyncio.run(tasks.assign(state, plan_id, CHAT, "turn-1", "researcher",
                                    "Unde caut dentiști în Brașov?"))
    job = _work(state, tasks.TASK_KIND)
    assert job.status == "done"
    assert asyncio.run(tasks.get(state, task["id"]))["status"] == "done"
    assert _q(state, "SELECT provider, task FROM usage_events") == [("codex", "task.researcher")]


# ── What holds one provider holds only the work on it ──

def _claude_spent(monkeypatch):
    """Claude's share of its plan used; Codex's not."""
    async def within(self, *a, **k):
        return self.provider != "claude"
    monkeypatch.setattr(Brain, "is_within_budget", within)


def test_claudes_share_used_holds_claude_chats_only(state, claude, codex_cli, monkeypatch):
    _claude_spent(monkeypatch)

    async def work(h):
        with pytest.raises(chat.Refused) as held:
            await h.ask(None, "Câte firme?")
        on_codex = await _say(h, None, "Câte firme?", provider="codex")
        return held.value, on_codex
    held, on_codex = _hub(state, work)
    assert held.code == "budget" and "Claude" in str(held)
    assert on_codex[-1]["type"] == "done"
    assert claude() == []


def test_claudes_share_used_holds_its_wakes_and_not_codexs(state, codex_cli, monkeypatch):
    _claude_spent(monkeypatch)
    codex_cli.use("silent")
    on_claude = _plan(state, "chat-c", provider="claude")
    on_codex = _plan(state, "chat-x", provider="codex")
    asyncio.run(wakes.note(state, on_claude, "reply", "r1", {}))
    asyncio.run(wakes.note(state, on_codex, "reply", "r2", {}))
    first = _work(state, wakes.WAKE_KIND)
    # (The Claude wake waits, no try used, the lane not rested: the Codex
    # wake runs at once.)
    assert first.status == "queued" and first.attempts == 0
    assert asyncio.run(jobs.lane_paused(state, wakes.AGENT_LANE)) in (None, "")
    held = asyncio.run(wakes.held(state))
    assert held["provider"] == "claude" and not held["paused"]
    second = _work(state, wakes.WAKE_KIND)
    assert second.status == "done" and second.params["plan"] == on_codex
    assert _q(state, "SELECT provider, task FROM usage_events") == [("codex", "wake")]


def test_a_provider_that_cant_be_started_holds_its_runs_not_the_lane(state, codex_cli,
                                                                       monkeypatch):
    # (Codex found and checked, then its process can't be started: only the
    # runs on Codex wait, the agent's lane isn't paused for the user to
    # resume, so the chats on Claude keep their wakes.)
    from harvey.agent import driver

    async def broken(*a, **k):
        raise PermissionError("not executable")
    monkeypatch.setattr(driver, "start", broken)
    plan_id = _plan(state)
    asyncio.run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work(state, wakes.WAKE_KIND)
    assert job.status == "queued" and job.attempts == 0 and job.not_before
    assert asyncio.run(jobs.lane_paused(state, wakes.AGENT_LANE)) in (None, "")
    held = asyncio.run(wakes.held(state))
    assert held["provider"] == "codex" and "couldn't be started" in held["why"]


def test_a_task_whose_provider_cant_be_started_waits_queued(state, codex_cli, monkeypatch):
    from harvey.agent import driver

    async def broken(*a, **k):
        raise PermissionError("not executable")
    monkeypatch.setattr(driver, "start", broken)
    plan_id = _plan(state)
    task = asyncio.run(tasks.assign(state, plan_id, CHAT, "turn-1", "researcher",
                                    "Unde caut dentiști în Brașov?"))
    job = _work(state, tasks.TASK_KIND)
    assert job.status == "queued" and job.not_before
    assert asyncio.run(jobs.lane_paused(state, wakes.AGENT_LANE)) in (None, "")
    assert asyncio.run(tasks.get(state, task["id"]))["status"] == "queued"


def test_a_chat_moved_while_its_wake_starts_isnt_answered_on_the_old_provider(
        state, codex_cli, monkeypatch):
    # (The user moves the chat to Claude after the wake read Codex and
    # before it took the chat: it doesn't run on Codex; it waits, and runs
    # on Claude when it starts again.)
    from harvey.modules import worker as worker_mod

    plan_id = _plan(state)
    asyncio.run(wakes.note(state, plan_id, "reply", "r1", {}))
    real = worker_mod.acquire_lease

    async def moved_first(st, owner, ttl, name=None, *a, **k):
        if str(owner).startswith(wakes.OWNER_PREFIX):
            con = sqlite3.connect(state.db_path)
            con.execute("UPDATE agent_chat SET provider = 'claude' WHERE id = ?", (CHAT,))
            con.commit()
            con.close()
        return await real(st, owner, ttl, name, *a, **k)
    monkeypatch.setattr(worker_mod, "acquire_lease", moved_first)
    job = _work(state, wakes.WAKE_KIND)
    assert job.status == "queued" and job.attempts == 0
    assert _q(state, "SELECT COUNT(*) FROM usage_events") == [(0,)]
    assert _q(state, "SELECT COUNT(*) FROM worker_lease WHERE name = ?",
              chat._lease(CHAT)) == [(0,)]


def test_each_providers_api_key_caps_count_its_own_calls_only(state):
    from harvey.config import BrainConfig

    brain = BrainConfig(provider="claude", claude={"auth": "api_key", "usd_per_day": 1.0},
                        codex={"auth": "api_key", "usd_per_day": 1.0, "model": "gpt-5.5",
                               "codex_usd_per_mtok_in": 1.0, "codex_usd_per_mtok_out": 2.0})
    asyncio.run(state.record_usage_event(agent="writer", task="t", source="api_key",
                                         provider="codex", cost_usd=1.5))

    async def go():
        return (await brain_spend.refusal(state, brain.for_provider("claude")),
                await brain_spend.refusal(state, brain.for_provider("codex")),
                await brain_spend.spent(state, "day", brain.for_provider("claude")))
    on_claude, on_codex, claude_spent = asyncio.run(go())
    assert on_claude == "" and claude_spent == 0.0
    assert "brain.codex.usd_per_day" in on_codex and "$1.50 spent" in on_codex


# ── Schema 43 ──

def test_schema_43_adds_each_chats_provider_from_42_and_on_a_new_file(tmp_path):
    from harvey.overlay import schema

    old = tmp_path / "old.db"
    con = sqlite3.connect(old, isolation_level=None)
    schema.create(con, version=42)
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at) "
                "VALUES ('c1', 'Mai demult', 1, 1)")
    assert schema.step_up(con) == schema.LATEST == 43
    assert schema.missing(con) == []
    # (A chat on file keeps NULL: the default provider at the time.)
    assert con.execute("SELECT provider FROM agent_chat").fetchall() == [(None,)]
    assert schema.step_up(con) == 43                       # again: nothing to do
    con.close()
    new = sqlite3.connect(tmp_path / "new.db", isolation_level=None)
    schema.create(new)
    assert "provider" in {r[1] for r in new.execute("PRAGMA table_info(agent_chat)")}
    new.close()
