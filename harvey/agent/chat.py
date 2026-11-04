"""The chat with Harvey's agent: its messages, and the job that answers one.

A message the user sends (the dashboard's Chat tab, ``harvey agent ask``)
is a row of agent_message and an ``agent_turn`` job on the agent lane: the
answer comes when the job has run, not while the user waits. The job runs
the Claude CLI once (claude_runtime.build_agent_command: print mode, with
Harvey's read-only MCP server and its tools only, an empty working
folder, at most MAX_TURNS turns), the prompt on stdin: prompts/agent.md,
the conversation so far (bounded), then the message. The turn's tool calls
are recorded by the server (agent_tool_call); the call's tokens go to
usage_events (agent ``agent``, task ``chat``). Over the plan's quota
(Brain.is_within_budget, ``usage.max_daily_claude_percent``) the job
waits and the lane rests, like the heartbeat. Codex is refused: it needs
a mode of its own to load Harvey's tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import uuid
from pathlib import Path

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries
from harvey.modules import jobs

logger = logging.getLogger("harvey.agent")

AGENT_LANE = "agent"
KIND = "agent_turn"
# The CLI's turns (model calls between tool calls) for one answer.
MAX_TURNS = 12
# The conversation the prompt carries: the newest messages, at most this many
# and this long in all.
HISTORY_MESSAGES = 20
HISTORY_CHARS = 16_000
MESSAGE_CHARS = 4000
TURN_TIMEOUT_SECONDS = 600
# Over the plan's quota: the turn waits this long, then asks again.
QUOTA_WAIT_SECONDS = 3600
KILL_REAP_SECONDS = 10.0

# A user message's status, and an answer's.
QUEUED, RUNNING, HELD, ANSWERED, FAILED = "queued", "running", "held", "answered", "failed"
# Shown (never stored) for a message whose job ended without an answer: it
# was cancelled in the Queue, or failed outside the turn.
STOPPED = "stopped"
WAITING = (QUEUED, RUNNING, HELD)
OK = "ok"
# An answer that is an error says which (the dashboard says it in its own
# words); its text is the detail, in English.
ERRORS = ("codex", "cli", "login", "max_turns", "failed", "timeout")

CODEX_REFUSED = ("The chat runs on Claude only for now: Codex needs a mode of its own to load "
                 "Harvey's tools. Set brain.provider: claude in harvey.local.yaml to use it.")
_LOGIN_PATTERNS = ("not logged in", "please run `claude login`", "invalid api key",
                   "unauthorized")


class Refused(ValueError):
    """The message can't be queued: the message says why."""


def _config():
    try:
        from harvey.config import load_config
        return load_config()
    except Exception:
        return None


def refusal(config) -> str:
    """Why the chat can't run with these settings, or ""."""
    brain = getattr(config, "brain", None)
    if getattr(brain, "provider", "claude") == "codex":
        return CODEX_REFUSED
    return ""


def _now() -> int:
    return int(time.time())


def _row(r) -> dict:
    keys = ("id", "turn", "role", "text", "status", "job", "created_at", "updated_at")
    return dict(zip(keys, r))


async def _add(db, turn: str, role: str, text: str, status: str, job: str = "") -> None:
    now = _now()
    await db.execute(queries.sql("overlay.agent.add"), (turn, role, text, status, job, now, now))


async def _set_status(state, turn: str, status: str, job: str = "") -> None:
    async with state._connect() as db:
        await db.execute(queries.sql("overlay.agent.set_status"), (status, job, _now(), turn))
        await db.commit()


async def _turn(state, turn: str) -> list[dict]:
    async with state._connect() as db:
        async with db.execute(queries.sql("overlay.agent.turn"), (turn,)) as cur:
            return [_row(r) for r in await cur.fetchall()]


async def ask(state, text: str, config=None) -> dict:
    """Queue a message: {"turn", "job_id", "batch_id"}. Refused for an empty
    or too long one, or while Codex is the brain."""
    text = (text or "").strip()
    if not text:
        raise Refused("write a message first")
    if len(text) > MESSAGE_CHARS:
        raise Refused(f"a message is at most {MESSAGE_CHARS:,} characters")
    why = refusal(config if config is not None else _config())
    if why:
        raise Refused(why)
    turn = uuid.uuid4().hex[:12]
    async with state._connect() as db:
        await _add(db, turn, "user", text, QUEUED)
        await db.commit()
    batch_id = await jobs.new_batch(state, KIND, "Chat", params={"turn": turn})
    job_id = await jobs.enqueue(state, KIND, AGENT_LANE, {"turn": turn},
                                dedupe_key=f"{KIND}:{turn}", module="agent",
                                label=text[:60], batch_id=batch_id)
    await _set_status(state, turn, QUEUED, job_id)
    return {"turn": turn, "job_id": job_id, "batch_id": batch_id}


async def messages(state, limit: int = 50, before: int | None = None) -> dict:
    """The newest ``limit`` messages, oldest first (``before``: those older
    than that id), and each turn's tool calls: {"messages", "calls": {turn:
    [...]}, "busy": a turn waiting or running}."""
    limit = max(1, min(int(limit or 50), 200))
    async with state._connect() as db:
        if before:
            sql, args = queries.sql("overlay.agent.messages_before"), (int(before), limit)
        else:
            sql, args = queries.sql("overlay.agent.messages"), (limit,)
        async with db.execute(sql, args) as cur:
            rows = [_row(r) for r in await cur.fetchall()]
        rows.reverse()
        turns = list(dict.fromkeys(r["turn"] for r in rows))
        calls: dict[str, list] = {}
        if turns:
            async with db.execute(queries.sql("overlay.agent.calls"),
                                  (json.dumps(turns),)) as cur:
                for _, turn, tool, args_, result, ms, outcome, at in await cur.fetchall():
                    try:
                        parsed = json.loads(args_ or "{}")
                    except ValueError:
                        parsed = {}
                    calls.setdefault(turn, []).append(
                        {"tool": tool, "arguments": parsed, "result": result, "ms": ms,
                         "outcome": outcome, "at": at})
    answered = {r["turn"] for r in rows if r["role"] == "agent"}
    for r in rows:
        if r["role"] == "user" and r["status"] in WAITING and r["turn"] not in answered:
            job = await jobs.get(state, r["job"]) if r["job"] else None
            if job is None or job.status in (jobs.DONE, jobs.FAILED, jobs.CANCELLED):
                r["status"] = STOPPED
    busy = any(r["role"] == "user" and r["status"] in WAITING for r in rows)
    return {"messages": rows, "calls": calls, "busy": busy}


# ── One turn ──

def mcp_config(db_path: str, turn: str) -> dict:
    """The MCP config naming Harvey's own server for one turn: this
    Python, the server module, the database and the turn."""
    package_root = str(Path(__file__).resolve().parents[2])
    return {"mcpServers": {"harvey": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "harvey.agent.tools", "--db", str(Path(db_path).resolve()),
                 "--turn", turn],
        "env": {"PYTHONPATH": package_root, "PYTHONIOENCODING": "utf-8"}}}}


def _history(rows: list[dict], turn: str) -> str:
    """The conversation before ``turn``: the newest answered exchanges,
    within HISTORY_MESSAGES and HISTORY_CHARS."""
    lines: list[str] = []
    used = 0
    for r in reversed(rows):
        if r["turn"] == turn:
            continue
        if r["role"] == "user" and r["status"] != ANSWERED:
            continue
        if r["role"] == "agent" and r["status"] != OK:
            continue
        line = f"{'User' if r['role'] == 'user' else 'Harvey'}: {r['text']}"
        if len(lines) >= HISTORY_MESSAGES or used + len(line) > HISTORY_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(reversed(lines))


async def prompt(state, turn: str, text: str) -> str:
    """What the CLI is sent on stdin for ``turn``."""
    from harvey.brain import PROMPTS_DIR
    from harvey.textio import read_user_text

    system = read_user_text(PROMPTS_DIR / "agent.md")
    system = system.replace("{{date}}", time.strftime("%Y-%m-%d")).replace(
        "{{max_turns}}", str(MAX_TURNS))
    async with state._connect() as db:
        async with db.execute(queries.sql("overlay.agent.messages"), (200,)) as cur:
            rows = [_row(r) for r in await cur.fetchall()]
    rows.reverse()
    past = _history(rows, turn)
    parts = [system.strip()]
    if past:
        parts.append("## The conversation so far\n\n" + past)
    parts.append("## The user's new message\n\n" + text)
    return "\n\n".join(parts) + "\n"


async def _run_cli(cmd: list[str], prompt_text: str, cwd: str, env: dict,
                   timeout: float) -> tuple[int | None, bytes, bytes]:
    """Run the CLI with the prompt on stdin: (exit code or None on a
    timeout, stdout, stderr). A cancelled turn kills it."""
    process = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(prompt_text.encode("utf-8")),
                                          timeout=timeout)
    except asyncio.TimeoutError:
        _kill(process)
        await process.wait()
        return None, b"", b""
    except BaseException:
        _kill(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_REAP_SECONDS)
        except Exception as e:
            logger.warning("agent CLI not reaped: %r", e)
        raise
    return process.returncode, out or b"", err or b""


def _kill(process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _answer(state, turn: str, text: str, status: str, user_status: str) -> None:
    async with state._connect() as db:
        await _add(db, turn, "agent", text, status)
        await db.execute(queries.sql("overlay.agent.set_status"),
                         (user_status, "", _now(), turn))
        await db.commit()


async def _fail(state, turn: str, code: str, detail: str) -> dict:
    await _answer(state, turn, detail, f"error:{code}", FAILED)
    return {"turn": turn, "outcome": "error", "error": code, "detail": detail[:300]}


async def run_turn(ctx) -> dict:
    """One turn: the CLI asked once, with Harvey's tools; the answer (or
    what went wrong) written as the agent's message."""
    from harvey import claude_runtime
    from harvey.brain import Brain

    state = ctx.state
    turn = str(ctx.params.get("turn") or "")
    rows = await _turn(state, turn)
    user = next((r for r in rows if r["role"] == "user"), None)
    if user is None:
        raise jobs.JobError(f"no message for turn {turn!r}")
    if any(r["role"] == "agent" for r in rows):
        return {"turn": turn, "outcome": "answered already"}
    config = _config()
    why = refusal(config)
    if why:
        return await _fail(state, turn, "codex", why)
    brain_config = getattr(config, "brain", None)
    brain = Brain(state, brain_config)
    percent = getattr(getattr(config, "usage", None), "max_daily_claude_percent", None)
    if not await brain.is_within_budget(max_percent=percent):
        await _set_status(state, turn, HELD)
        await jobs.rest_lane(state, ctx.job.lane, QUOTA_WAIT_SECONDS, but=ctx.job.id)
        raise jobs.LaneExhausted(
            f"{brain.provider_label} usage limit reached for Harvey's share of the plan; "
            "the chat waits", retry_after=QUOTA_WAIT_SECONDS)
    try:
        exe = await asyncio.to_thread(claude_runtime.check_cli, None, claude_runtime.AGENT_FLAGS)
    except claude_runtime.ClaudeRuntimeError as e:
        return await _fail(state, turn, "cli", str(e))
    await _set_status(state, turn, RUNNING)
    await ctx.say(f"  turn {turn}: asking Claude")
    text = await prompt(state, turn, user["text"])
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="harvey-agent-mcp-",
                                         ignore_cleanup_errors=True) as folder:
            config_path = Path(folder) / "mcp.json"
            config_path.write_text(json.dumps(mcp_config(state.db_path, turn)),
                                   encoding="utf-8")
            cmd = claude_runtime.build_agent_command(
                exe, str(config_path), max_turns=MAX_TURNS,
                model=getattr(brain_config, "model", "") or "")
            with claude_runtime.isolated_cwd() as cwd:
                code, out, err = await _run_cli(cmd, text, cwd,
                                                claude_runtime.runtime_env(cwd, safe=False),
                                                TURN_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        await asyncio.shield(_set_status(state, turn, QUEUED))
        raise
    except OSError as e:
        return await _fail(state, turn, "cli", f"the Claude CLI couldn't be started: {e}")
    ms = int((time.monotonic() - started) * 1000)
    if code is None:
        # The call ran (and used the plan) until it was stopped: counted,
        # with no tokens to record.
        try:
            await state.increment_usage()
        except Exception as e:
            logger.warning("agent usage not recorded: %s", e)
        return await _fail(state, turn, "timeout",
                           f"no answer after {TURN_TIMEOUT_SECONDS} s")
    response, payload = Brain._parse_result_payload(out.decode("utf-8", errors="replace").strip())
    try:
        await state.increment_usage()
        await brain._record_usage(payload, agent="agent", task="chat", label=turn)
    except Exception as e:
        logger.warning("agent usage not recorded: %s", e)
    if code != 0 and not payload:
        error = err.decode("utf-8", errors="replace").strip()
        if any(p in error.lower() for p in _LOGIN_PATTERNS):
            return await _fail(state, turn, "login", "Claude isn't logged in: run claude login")
        return await _fail(state, turn, "failed", f"the Claude CLI exited with {code}: "
                                                  f"{error[:300]}")
    subtype = str((payload or {}).get("subtype") or "")
    if subtype == "error_max_turns":
        return await _fail(state, turn, "max_turns",
                           f"stopped after {MAX_TURNS} turns without an answer")
    if (payload or {}).get("is_error") or not response.strip():
        # A logged-out CLI says so in its JSON result, not on stderr.
        if any(p in response.lower() for p in _LOGIN_PATTERNS):
            return await _fail(state, turn, "login", "Claude isn't logged in: run claude login")
        return await _fail(state, turn, "failed", response.strip()[:300] or "no answer")
    await _answer(state, turn, response.strip(), OK, ANSWERED)
    await ctx.say(f"  answered in {ms / 1000:.1f} s")
    return {"turn": turn, "outcome": "answered", "ms": ms,
            "turns": int((payload or {}).get("num_turns") or 0)}
