"""Harvey's agent on Codex (harvey/agent/driver.py): with brain.provider
codex, the chat, a wake and a task run on Codex's app-server in the brain's
private runtime, with Harvey's MCP server as its only tools, read as the
same events the Claude CLI gives. The CLI is a stand-in
(tests/fake_codex_agent.py, speaking Codex 0.159.3's app-server protocol:
no model, no network, no login is used): a chat answer streamed with one
tool call, two messages in one process, Stop, the turn limit, anything but
Harvey's tools ending the answer, an unsafe config or an unverified version
refused before anything is sent, a logged-out plan, a wake that has
nothing to say, a task's JSON answer; and Claude, still the default, as it
was. All data is fictional."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harvey import claude_runtime, codex, registers
from harvey.agent import chat, driver, plans, tasks, wakes
from harvey.brain import Brain
from harvey.config import load_config
from harvey.modules import jobs, worker
from harvey.state import StateManager

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the Codex provider isn't supported on Windows")

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_codex_agent.py")
CHAT = "chat-x"
RESEARCH = {"areas": [{"area": "Brașov", "why": "1.200 de firme fără site"}],
            "phrases": ["stomatolog"], "sources": ["dataforseo_listings"],
            "estimate": {"summary": "o căutare", "usd_typical": 0.4, "usd_high": 0.9},
            "notes": "Registrul are deja firmele."}


def _config(provider: str = "codex"):
    cfg = load_config(str(ROOT / "harvey.yaml"))
    cfg.brain.provider = provider
    cfg.brain.model = "gpt-5.5" if provider == "codex" else ""
    return cfg


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No real CLI of either kind, no quota read, no register; Codex as the
    brain; runtimes under this test's own temp folder."""
    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [])
    monkeypatch.setattr(chat, "_config", lambda: _config())

    def refuse(*a, **k):
        raise AssertionError("the real Claude CLI was looked for")
    monkeypatch.setattr(claude_runtime, "check_cli", refuse)
    monkeypatch.setattr(codex, "find_codex", lambda: None)

    async def within(self, *a, **k):
        return True
    monkeypatch.setattr(Brain, "is_within_budget", within)
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(runtimes))


class Fake:
    def __init__(self, tmp_path: Path):
        self.scenario_file = tmp_path / "scenario"
        self.log = tmp_path / "fake-codex.log"
        self.log.write_text("", encoding="utf-8")
        self.use("answer")

    def use(self, scenario: str, answer: str = ""):
        self.scenario_file.write_text(scenario, encoding="utf-8")
        if answer:
            Path(str(self.scenario_file) + ".answer").write_text(answer, encoding="utf-8")
        return self

    def entries(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x]

    def methods(self):
        return [e["method"] for e in self.entries() if "method" in e]

    def servers(self):
        return [e for e in self.entries() if e.get("cmd") == "app-server"]

    def prompts(self):
        return [e["prompt"] for e in self.entries() if "prompt" in e]

    def params(self, method):
        return [e["params"] for e in self.entries() if e.get("method") == method]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The stand-in Codex CLI (a wrapper on PATH's place), and the user's
    ChatGPT login it is given an access-only copy of."""
    fake = Fake(tmp_path)
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" '
                       f'"{fake.scenario_file}" "{fake.log}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(codex, "find_codex", lambda: str(wrapper))
    home = tmp_path / "native-codex"
    home.mkdir()
    auth = home / "auth.json"
    stamp = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
    auth.write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": _jwt({"email": "x@example.com"}),
                   "access_token": _jwt({"exp": time.time() + 86400}),
                   "refresh_token": "native-refresh", "account_id": "acct-1"},
        "last_refresh": stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}))
    auth.chmod(0o600)
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


def _x(state, sql, *args):
    con = sqlite3.connect(state.db_path)
    try:
        con.execute(sql, args)
        con.commit()
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


async def _say(h, chat_id, text):
    answer = await h.ask(chat_id, text)
    return [e async for e in answer.follow()]


def _leftover():
    return [d for d in os.listdir(tempfile.gettempdir()) if d.startswith(codex.RUNTIME_PREFIX)]


# ── The chat ──

def test_a_chat_answers_on_codex_with_harveys_tools_only(state, fake):
    async def work(h):
        first = await _say(h, None, "Câte firme din Cluj au telefon?")
        cid = first[0]["chat"]
        second = await _say(h, cid, "Și cu email?")
        return cid, first, second
    cid, first, second = _hub(state, work)
    kinds = [e["type"] for e in first]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert [e for e in first if e["type"] == "tool_use"] == [
        {"type": "tool_use", "id": "call-1-0", "tool": "counts",
         "arguments": {"filter": {"county": "Cluj"}}, "turn": first[0]["turn"]}]
    [called] = [e for e in first if e["type"] == "tool_result"]
    assert (called["tool"], called["outcome"]) == ("counts", "ok")
    streamed = "".join(e["text"] for e in first if e["type"] == "text")
    assert streamed == "Mă uit în date.Mesajul 1: trei firme."
    # The answer is the final one, never the commentary.
    assert first[-1]["text"] == "Mesajul 1: trei firme."
    assert second[-1]["text"] == "Mesajul 2: trei firme."
    # One runtime for both messages (two launches: attested without the
    # login, then with it), Harvey's MCP server the only one, as written.
    servers = fake.servers()
    assert [s["has_auth"] for s in servers] == [False, True]
    for s in servers:
        assert list(s["mcp_servers"]) == ["harvey"]
        entry = s["mcp_servers"]["harvey"]
        assert entry["args"][:2] == ["-m", "harvey.agent.tools"]
        assert entry["args"][entry["args"].index("--chat") + 1] == cid
        assert entry["default_tools_approval_mode"] == "approve"
        assert "OPENAI_API_KEY" not in s["env_keys"]
    [thread] = fake.params("thread/start")
    assert thread["environments"] == [] and thread["dynamicTools"] == []
    assert (thread["sandbox"], thread["approvalPolicy"]) == ("read-only", "never")
    assert thread["developerInstructions"] == driver.AGENT_INSTRUCTIONS
    assert thread["model"] == "gpt-5.5" and "mcp_servers" not in thread["config"]
    assert thread["config"]["features.shell_tool"] is False
    # The first message carries the prompt and the conversation; the second
    # only what was typed.
    prompts = fake.prompts()
    assert prompts[0].startswith("You are Harvey's agent") and prompts[1] == "Și cu email?"
    # Its tool calls recorded under each message; the answers and their size.
    assert _q(state, "SELECT COUNT(*) FROM agent_tool_call") == [(2,)]
    rows = _q(state, "SELECT text, status, tokens, context FROM agent_message "
                     "WHERE role = 'agent' ORDER BY id")
    assert [r[:2] for r in rows] == [("Mesajul 1: trei firme.", "ok"),
                                     ("Mesajul 2: trei firme.", "ok")]
    # Each message's own tokens (the thread's total less the one before);
    # its context, its last model call's input.
    assert [r[2] for r in rows] == [2160, 2160] and [r[3] for r in rows] == [1200, 1200]
    usage = _q(state, "SELECT provider, model, agent, task, input_tokens, cache_read_tokens, "
                      "output_tokens, source FROM usage_events ORDER BY id")
    assert usage == [("codex", "gpt-5.5", "agent", "chat", 800, 1300, 60, "codex_app_server")] * 2
    assert _q(state, "SELECT SUM(codex_calls), SUM(claude_calls) FROM usage_log") == [(2, 0)]
    assert _leftover() == []


def test_stop_interrupts_the_codex_turn(state, fake):
    fake.use("slow")

    async def work(h):
        answer = await h.ask(None, "Scrie mult")
        async for e in answer.follow():
            if e["type"] == "text":
                break
        assert await h.stop(answer.chat)
        return [e async for e in answer.follow()]
    events = _hub(state, work)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "stopped"
    [interrupt] = fake.params("turn/interrupt")
    assert interrupt == {"threadId": "thr-agent", "turnId": "turn-1"}
    assert _q(state, "SELECT status FROM agent_message WHERE role = 'agent'") == [
        ("error:stopped",)]


def test_the_turn_limit_is_counted_by_harvey_on_codex_too(state, fake):
    fake.use("loop")

    async def work(h):
        return await _say(h, None, "Caută la nesfârșit")
    events = _hub(state, work)
    assert events[-1]["code"] == "max_turns"
    assert len([e for e in events if e["type"] == "tool_use"]) >= chat.MAX_TURNS
    assert fake.params("turn/interrupt")


@pytest.mark.parametrize("scenario, what", [
    ("shell", "commandExecution"),
    ("other_server", "another MCP server"),
    ("raw_exec", "function_call exec_command"),
    ("approval", "a request to the client"),
])
def test_anything_but_harveys_tools_ends_the_answer(state, fake, scenario, what):
    fake.use(scenario)

    async def work(h):
        events = await _say(h, None, "Salut")
        live = h.chats[events[0]["chat"]]
        return events, live.proc
    events, proc = _hub(state, work)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "failed"
    assert "something other than Harvey's tools" in events[-1]["detail"]
    assert what in events[-1]["detail"]
    assert proc is None or not proc.alive
    assert _leftover() == []


@pytest.mark.parametrize("scenario, words", [
    ("shell_on", "feature 'shell_tool' is not disabled"),
    ("extra_mcp", "an MCP server other than Harvey's"),
    ("old_version", "hasn't been verified"),
])
def test_an_unsafe_codex_is_refused_before_anything_is_sent(state, fake, scenario, words):
    fake.use(scenario)

    async def work(h):
        with pytest.raises(chat.Refused) as e:
            await h.ask(None, "Salut")
        return e.value
    refused = _hub(state, work)
    assert refused.code == "cli" and words in str(refused)
    assert "turn/start" not in fake.methods() and fake.prompts() == []
    # Nothing written, no lease left, no runtime left behind.
    assert _q(state, "SELECT COUNT(*) FROM agent_chat") == [(0,)]
    assert _q(state, "SELECT COUNT(*) FROM worker_lease WHERE name LIKE 'agent_chat:%'") == [(0,)]
    assert _leftover() == []
    if scenario == "old_version":
        assert fake.servers() == []
    elif scenario == "shell_on":
        # (Caught while the runtime held no login.)
        assert [s["has_auth"] for s in fake.servers()] == [False]


def test_a_logged_out_chatgpt_plan_says_codex_login(state, fake):
    fake.use("logout")

    async def work(h):
        return await _say(h, None, "Salut")
    events = _hub(state, work)
    assert events[-1]["code"] == "login" and "codex login" in events[-1]["detail"]


# ── A wake and a task ──

def _plan(state) -> int:
    _x(state, "INSERT INTO agent_chat (id, title, created_at, last_at) "
              "VALUES (?, 'Dentiști din Brașov', 1, 1)", CHAT)

    async def go():
        p = await plans.propose(state, CHAT, "t0", "Contacte pentru dentiștii din Brașov",
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


def test_a_wake_on_codex_that_has_nothing_to_say_writes_nothing(state, fake):
    fake.use("silent")
    plan_id = _plan(state)
    asyncio.run(wakes.note(state, plan_id, "reply", "r1", {"intent": "not_now"}))
    job = _work(state, wakes.WAKE_KIND)
    assert job.status == "done" and job.result["silent"] is True
    assert _q(state, "SELECT COUNT(*) FROM agent_message") == [(0,)]
    assert _q(state, "SELECT tool FROM agent_tool_call WHERE turn = ?", job.result["turn"]) == [
        ("counts",)]
    assert _q(state, "SELECT provider, task FROM usage_events") == [("codex", "wake")]
    [prompt] = fake.prompts()
    assert "## Harvey woke you" in prompt and "[SILENT]" in prompt
    assert _q(state, "SELECT COUNT(*) FROM worker_lease WHERE name LIKE 'agent_chat:%'") == [(0,)]
    assert _leftover() == []


def test_a_task_on_codex_gives_its_json_answer(state, fake):
    fake.use("task", json.dumps(RESEARCH, ensure_ascii=False))
    plan_id = _plan(state)
    task = asyncio.run(tasks.assign(state, plan_id, CHAT, "turn-1", "researcher",
                                    "Unde caut dentiști în Brașov?"))
    job = _work(state, tasks.TASK_KIND)
    assert job.status == "done"
    done = asyncio.run(tasks.get(state, task["id"]))
    assert done["status"] == "done" and done["result"] == RESEARCH
    # The server in task mode: only the task's tools.
    args = fake.servers()[-1]["mcp_servers"]["harvey"]["args"]
    assert args[-2:] == ["--task", str(task["id"])]
    assert _q(state, "SELECT tool FROM agent_tool_call WHERE turn = ?", task["turn"]) == [
        ("counts",)]
    assert _q(state, "SELECT provider, task FROM usage_events") == [("codex", "task.researcher")]


def test_a_wake_on_a_logged_out_codex_pauses_the_lane(state, fake):
    fake.use("logout")
    plan_id = _plan(state)
    asyncio.run(wakes.note(state, plan_id, "reply", "r1", {}))
    job = _work(state, wakes.WAKE_KIND)
    assert job.status in ("queued", "paused") and "codex login" in (job.error or "")


# ── Claude stays the default ──

def test_claude_is_the_default_and_codex_is_refused_only_where_it_cant_run(monkeypatch):
    seen = []

    def check_cli(env=None, flags=()):
        seen.append(flags)
        return "/bin/claude"
    monkeypatch.setattr(claude_runtime, "check_cli", check_cli)
    assert asyncio.run(driver.check(_config("claude"))) == "/bin/claude"
    assert seen == [claude_runtime.AGENT_FLAGS]
    assert driver.refusal(_config("claude")) == "" and driver.refusal(_config("codex")) == ""
    assert chat.refusal(load_config(str(ROOT / "harvey.yaml"))) == ""
    monkeypatch.setattr(codex, "_PLATFORM", "win32")
    assert "Windows" in driver.refusal(_config("codex"))
    assert driver.refusal(_config("claude")) == ""
    with pytest.raises(driver.Unusable) as e:
        asyncio.run(driver.check(_config("codex")))
    assert e.value.provider == driver.CODEX


def test_the_codex_config_names_harveys_server_as_chat_mcp_config_does():
    entry = chat.mcp_config("/data/harvey.db", "c1", task=4)["mcpServers"]["harvey"]
    toml = driver.codex_mcp_toml(entry)
    import tomllib
    parsed = tomllib.loads(toml)["mcp_servers"]["harvey"]
    assert parsed["command"] == entry["command"] and parsed["args"] == entry["args"]
    assert parsed["env"] == entry["env"] and parsed["enabled"] is True
    assert parsed["default_tools_approval_mode"] == "approve"


# ── What the review tightened ──

def _entry():
    return chat.mcp_config("/data/harvey.db", "c1")["mcpServers"]["harvey"]


def _read_back(entry, **extra):
    """config/read's report of the private config, as 0.159.3 gives it (its
    default environment_id added, the timeouts as floats)."""
    written = {k: entry[k] for k in ("command", "args", "env")}
    reported = {**written, "enabled": True, "environment_id": "local",
                "startup_timeout_sec": 30.0, "tool_timeout_sec": 120.0,
                "default_tools_approval_mode": "approve", **extra}
    return {"config": {"mcp_servers": {"harvey": reported}}}


def test_harveys_server_must_read_back_exactly_as_harvey_wrote_it():
    entry = _entry()
    driver.attest_agent_config(_read_back(entry), entry)
    # Another config layer adding to Harvey's entry changes what runs as
    # Harvey's server: an environment variable, a working folder, a URL,
    # approvals asked again.
    for extra in ({"env": {**entry["env"], "PYTHONSTARTUP": "/srv/x.py"}},
                  {"cwd": "/srv"}, {"url": "https://mcp.example.com"},
                  {"default_tools_approval_mode": "prompt"},
                  {"environment_id": "remote"}):
        with pytest.raises(codex.CodexError) as e:
            driver.attest_agent_config(_read_back(entry, **extra), entry)
        assert e.value.kind == "isolation", extra


def test_a_raw_call_counts_as_harveys_only_in_harveys_namespace():
    harveys = driver._harvey_raw_call
    assert harveys({"name": "counts", "namespace": "mcp__harvey"})
    assert harveys({"name": "counts", "namespace": "mcp__harvey__"})
    assert harveys({"name": "mcp__harvey__counts"})
    assert harveys({"name": "read_mcp_resource"})
    # (Whatever its name says, a call in another namespace isn't to a tool
    # Harvey gave.)
    assert not harveys({"name": "mcp__harvey__counts", "namespace": "web"})
    assert not harveys({"name": "read_mcp_resource", "namespace": "files"})
    assert not harveys({"name": "", "namespace": "mcp__harvey"})
    assert not harveys({"name": "exec_command"})


class _Server:
    """Enough of codex._AppServer for CodexProcess to translate
    notifications handed to it directly."""
    proc = None
    _next_id = 1

    def __init__(self):
        self.sent = []
        self.stopped = False

    async def read_message(self, deadline):
        await asyncio.Event().wait()

    async def _send(self, message):
        self.sent.append(message)

    async def stop(self):
        self.stopped = True

    async def close(self):
        self.stopped = True


def _drain(proc) -> list:
    out = []
    while not proc._events.empty():
        out.append(proc._events.get_nowait())
    return out


def test_an_ended_turns_late_notifications_dont_end_the_next_answer():
    async def go():
        proc = driver.CodexProcess(_Server(), "thr", "gpt-5.5")
        try:
            note = lambda method, **p: proc._on(method, {"threadId": "thr", **p})
            proc._turn_id = "turn-1"
            # The turn fails (an error Codex won't retry), its result given...
            assert note("error", turnId="turn-1", willRetry=False,
                        error={"message": "x", "codexErrorInfo": "badRequest"}) == ""
            [failed] = _drain(proc)
            assert failed["type"] == "result" and failed["is_error"]
            # ...the next message is sent, then the first turn's own end
            # arrives late, with a delta of it: not the second answer's.
            proc._new_turn()
            assert note("turn/completed", turn={"id": "turn-1", "status": "failed",
                                                "items": []}) == ""
            assert note("item/agentMessage/delta", turnId="turn-1", itemId="a",
                        delta="vechi") == ""
            assert _drain(proc) == []
            note("turn/started", turn={"id": "turn-2", "status": "inProgress"})
            note("item/completed", turnId="turn-2",
                 item={"type": "agentMessage", "id": "m2", "text": "Gata.",
                       "phase": "final_answer"})
            note("turn/completed", turn={"id": "turn-2", "status": "completed", "items": []})
            assert _drain(proc)[-1]["result"] == "Gata."
            # An ended turn is still checked: anything but Harvey's tools.
            assert note("item/started", turnId="turn-1",
                        item={"type": "commandExecution", "id": "c"}) == "commandExecution"
        finally:
            proc._reader.cancel()
    asyncio.run(go())


def test_a_turns_end_listing_something_but_harveys_tools_ends_the_answer():
    async def go():
        proc = driver.CodexProcess(_Server(), "thr", "gpt-5.5")
        try:
            proc._turn_id = "turn-1"
            why = proc._on("turn/completed", {"threadId": "thr", "turn": {
                "id": "turn-1", "status": "completed",
                "items": [{"type": "webSearch", "id": "w", "query": "x"},
                          {"type": "agentMessage", "id": "m", "text": "Gata.",
                           "phase": "final_answer"}]}})
            assert why == "webSearch"
            assert [e for e in _drain(proc) if e["type"] == "result"] == []
        finally:
            proc._reader.cancel()
    asyncio.run(go())


def _login(tmp_path, seconds_left: float):
    auth = tmp_path / "native-codex" / "auth.json"
    data = json.loads(auth.read_text())
    data["tokens"]["access_token"] = _jwt({"exp": time.time() + seconds_left})
    auth.write_text(json.dumps(data))


def test_a_chat_starts_a_new_codex_process_before_its_login_copy_runs_out(state, fake,
                                                                          tmp_path):
    # (The runtime's copy has no refresh token: one that could run out
    # within an answer's time isn't given another message.)
    _login(tmp_path, chat.ANSWER_TIMEOUT_SECONDS + 100)

    async def work(h):
        first = await _say(h, None, "Câte firme?")
        second = await _say(h, first[0]["chat"], "Și cu email?")
        return first, second
    first, second = _hub(state, work)
    assert first[-1]["type"] == "done" and second[-1]["type"] == "done"
    # Two runtimes (each started twice: without the login, then with it),
    # the second given the chat so far.
    assert [s["has_auth"] for s in fake.servers()] == [False, True, False, True]
    assert fake.prompts()[1].startswith("You are Harvey's agent")
    assert _leftover() == []


def test_a_login_codex_refused_isnt_used_for_the_next_message(state, fake):
    fake.use("logout")

    async def work(h):
        first = await _say(h, None, "Salut")
        fake.use("answer")
        return first, await _say(h, first[0]["chat"], "Acum?")
    first, second = _hub(state, work)
    assert first[-1]["code"] == "login" and second[-1]["type"] == "done"
    assert [s["has_auth"] for s in fake.servers()] == [False, True, False, True]


def test_the_hub_keeps_a_brain_only_on_codex(state):
    async def go():
        h = chat.Hub(state, asyncio.get_running_loop())
        claude = _config("claude")
        assert h.brain(claude) is not h.brain(claude)
        on_codex = _config("codex")
        assert h.brain(on_codex) is h.brain(on_codex)
    asyncio.run(go())
