"""The chats with Harvey's agent: conversations the user keeps, each
answered by a live process while it is in use: the Claude CLI's, or
Codex's app-server when brain.provider is codex (driver.py, read as the
same stream-json events; what follows says the CLI for either).

A chat (agent_chat, schema 32) is kept until the user deletes it: titled
from its first message (TITLE_CHARS, no model call) or renamed, listed by
its last activity, opened again any time and continued. While a chat is in
use one long-lived process answers it (claude_runtime.build_agent_command:
print mode with stream-json in and out, Harvey's MCP server and
its tools only, an empty working folder), owned by the Harvey process that
serves it: the dashboard, or the terminal for ``harvey agent chat`` and
``ask``. Each message goes in on stdin as a JSON line; the answer comes out
as it is made (its text, each tool call as it starts, each call's outcome
from agent_tool_call) and ends with a result, whose tokens go to
usage_events (agent ``agent``, task ``chat``). Nothing is queued: a message
is sent, or refused at once with why (REFUSALS).

Several chats can be live at once: one process each, at most MAX_LIVE per
Harvey process (a further one closes the least recently used idle process;
with every one answering it is refused). A process closes after
IDLE_SECONDS without a message, when its chat is deleted, or when Harvey
stops; the chat's next message starts a new one primed with the
conversation from Harvey's database (prime(): the messages, the answers, a
line per tool call, within PRIME_TOKENS, the oldest turns condensed to a
note saying so). Each live chat holds a lease (worker_lease
``agent_chat:<id>``) while its process lives, so two Harvey processes
never drive the same chat, while different chats run side by side.

Each answer has at most MAX_TURNS turns, counted here whatever the CLI's
own --max-turns counts; Stop and the turn limit ask the CLI to interrupt (a
control request) and end the process when no result follows within
STOP_GRACE_SECONDS. A failed or stopped answer can be retried: the same
message again in the same chat, the earlier attempt kept on file as
``retried`` (harvey agent log shows it; the Chat tab shows the retry).

Between the user's visits Harvey may wake the agent under an approved plan
(wakes.py: a job, never this process). A wake answers in the plan's chat,
holding the same lease, so it waits while a process here holds it; its
answer counts as unread until the chat is opened, and a process primed
before a wake answer was written is given it before its next message.
Every new process starts with the memory snapshot (memory.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
import uuid
from pathlib import Path

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries

logger = logging.getLogger("harvey.agent")

# The turns (model calls between tool calls) of one answer, counted by
# Harvey; the process's own ceiling (the CLI's --max-turns, whether it
# counts per message or per process), past which a new process is started.
MAX_TURNS = 12
PROCESS_TURNS = MAX_TURNS * 20
MESSAGE_CHARS = 4000
ANSWER_TIMEOUT_SECONDS = 600
# A chat's title: its first message, cut at a word to this many characters;
# a title the user gives, at most RENAME_CHARS.
TITLE_CHARS = 60
RENAME_CHARS = 120
# The conversation a new process is primed with: its newest turns within
# this many tokens (site_match.tokens' estimate), read from at most
# PRIME_ROWS messages; each tool call's arguments and result cut to
# PRIME_CALL_CHARS.
PRIME_TOKENS = 40_000
PRIME_ROWS = 1000
PRIME_CALL_CHARS = 200
# Live processes per Harvey process, and how long one waits for a message.
MAX_LIVE = 3
IDLE_SECONDS = 600
# Stop: how long the CLI has to end the answer after the interrupt.
STOP_GRACE_SECONDS = 5.0
CLOSE_GRACE_SECONDS = 5.0
KILL_REAP_SECONDS = 10.0
# Shutdown: how long every live chat has to close before its process is
# killed.
SHUTDOWN_SECONDS = 5.0
# A live chat's lease (one per chat, whoever serves it).
LEASE_PREFIX = "agent_chat:"
LEASE_TTL_SECONDS = 30.0
# Past this context (the newest answer's input, cache read and cache
# creation tokens) the Chat tab suggests a new chat: every message carries
# the whole conversation.
CONTEXT_HINT_TOKENS = 150_000
# The longest line the CLI may write (a tool result is one line).
LINE_BYTES = 16 * 1024 * 1024
STDERR_KEPT = 4000
TOOL_PREFIX = "mcp__harvey__"

# A user message's status, and an answer's.
RUNNING, ANSWERED, FAILED = "running", "answered", "failed"
# A user message sent again (Retry): its turn stays on file.
RETRIED = "retried"
# Shown (never stored) for a message nobody is answering any more: its
# process ended with Harvey (a restart) before the answer.
STOPPED = "stopped"
OK = "ok"
# An answer that is an error says which (the dashboard says it in its own
# words); its text is the detail, in English.
ERRORS = ("login", "max_turns", "failed", "timeout", "stopped")
# Why a message (or a change to a chat) is refused before anything is
# written.
REFUSALS = ("empty", "too_long", "codex", "budget", "busy", "elsewhere", "cli", "full",
            "missing", "retry", "waking")

_LOGIN_PATTERNS = ("not logged in", "please run `claude login`", "invalid api key",
                   "unauthorized", "run 'codex login'")


class Refused(ValueError):
    """The message can't be sent: ``code`` (REFUSALS) and why."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


def _config():
    try:
        from harvey.config import load_config
        return load_config()
    except Exception:
        return None


def refusal(config) -> str:
    """Why the chat can't run with these settings, or "" (driver.refusal:
    Codex where it isn't supported)."""
    from harvey.agent import driver

    return driver.refusal(config)


def _now() -> int:
    return int(time.time())


def title_of(text: str, limit: int = TITLE_CHARS) -> str:
    """A chat's title from a message: its words on one line, cut at a word
    to ``limit`` characters."""
    words = " ".join((text or "").split())
    if len(words) <= limit:
        return words
    cut = words[:limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def _row(r) -> dict:
    keys = ("id", "turn", "role", "text", "status", "chat", "created_at", "updated_at",
            "tokens", "context", "wake")
    out = dict(zip(keys, r))
    # (An answer written on a wake: why it woke, wakes.label; None for the
    # rest.)
    try:
        out["wake"] = json.loads(out["wake"]) if out.get("wake") else None
    except ValueError:
        out["wake"] = {"label": "woken"}
    return out


def _chat_row(r) -> dict | None:
    if r is None:
        return None
    return dict(zip(("id", "title", "created_at", "last_at", "turn", "unread"), r))


async def _one(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchone()


async def _all(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchall()


async def _write(state, *statements) -> None:
    """(name, args) statements in one transaction."""
    async with state._connect() as db:
        for name, args in statements:
            await db.execute(queries.sql(name), args)
        await db.commit()


def _lease(chat_id: str) -> str:
    return LEASE_PREFIX + chat_id


def _held_refusal(holder: dict | None, verb: str = "") -> Refused:
    """Why a chat whose lease someone else holds can't be used now: a wake
    is answering in it (it ends within minutes), or another Harvey
    process has it open."""
    from harvey.agent.wakes import OWNER_PREFIX

    holder = holder or {}
    if str(holder.get("owner") or "").startswith(OWNER_PREFIX):
        return Refused("Harvey is answering a wake in this chat: try again in a minute",
                       "waking")
    return Refused(
        f"the chat is open in another Harvey process (pid {holder.get('pid', '?')} on "
        f"{holder.get('host') or 'this machine'}): close it there" + verb, "elsewhere")


# ── The chats ──

async def chat_info(state, chat_id: str) -> dict | None:
    """One chat ({id, title, created_at, last_at, turn, unread, context,
    hint}), or None. ``unread``: its wake answers the user hasn't opened."""
    info = _chat_row(await _one(state, "overlay.agent.chat", (chat_id,)))
    if info is None:
        return None
    row = await _one(state, "overlay.agent.chat_context", (chat_id,))
    context = int(row[0]) if row else 0
    return {**info, "context": context, "hint": context >= CONTEXT_HINT_TOKENS}


async def _held_elsewhere(state) -> set[str]:
    """The chats another Harvey process has a live process for."""
    mine = _HUBS.get(_key(state))
    owner = mine.owner if mine is not None else ""
    rows = await _all(state, "overlay.agent.chat_leases", (time.time() - LEASE_TTL_SECONDS,))
    return {name[len(LEASE_PREFIX):] for name, who in rows if who != owner}


async def _waking(state) -> set[str]:
    """The chats a wake is answering in now (its lease)."""
    from harvey.agent.wakes import OWNER_PREFIX

    rows = await _all(state, "overlay.agent.chat_leases", (time.time() - LEASE_TTL_SECONDS,))
    return {name[len(LEASE_PREFIX):] for name, who in rows
            if str(who or "").startswith(OWNER_PREFIX)}


async def chats(state, q: str = "", limit: int = 50) -> list[dict]:
    """The chats, newest activity first ({id, title, created_at, last_at,
    unread, answering}), those whose title holds ``q`` when it is given.
    ``answering``: an answer is being made, here, in another Harvey
    process or by a wake; ``unread``: wake answers the user hasn't
    opened."""
    limit = max(1, min(int(limit or 50), 500))
    q = " ".join((q or "").split())
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await _all(state, "overlay.agent.chats_like", (like, limit))
    else:
        rows = await _all(state, "overlay.agent.chats", (limit,))
    here = _HUBS.get(_key(state))
    answering = here.answering() if here is not None else set()
    elsewhere = await _held_elsewhere(state)
    out = []
    for r in rows:
        c = _chat_row(r)
        turn = c.pop("turn")
        busy = c["id"] in answering or (bool(turn) and c["id"] in elsewhere)
        out.append({**c, "answering": busy})
    return out


async def mark_read(state, chat_id: str) -> None:
    """The user opened the chat: its wake answers are read."""
    await _write(state, ("overlay.agent.chat_read", (chat_id,)))


async def rename(state, chat_id: str, title: str) -> dict:
    """Give a chat a title of the user's (its words on one line, at most
    RENAME_CHARS): the chat, or Refused."""
    title = " ".join((title or "").split())
    if not title:
        raise Refused("give the chat a title", "empty")
    if len(title) > RENAME_CHARS:
        raise Refused(f"a title is at most {RENAME_CHARS} characters", "too_long")
    if await chat_info(state, chat_id) is None:
        raise Refused("no such chat", "missing")
    await _write(state, ("overlay.agent.rename_chat", (title, chat_id)))
    return await chat_info(state, chat_id)


# ── What a chat shows ──

async def messages(state, chat: str | None, limit: int = 50, before: int | None = None,
                   retried: bool = False) -> dict:
    """The newest ``limit`` messages, oldest first (``before``: those older
    than that id), of one chat (its id) or of every chat (None); each
    turn's tool calls; the chat (chat_info); ``live``, the message being
    answered in this process, and ``busy`` (one is being answered here or
    elsewhere; a running message nobody answers shows as stopped),
    ``waking`` (a wake answers in it now); ``more``: older messages are on
    file. A wake's answer carries ``wake`` (why it woke). A retried turn is
    left out unless ``retried`` (the log shows every attempt)."""
    limit = max(1, min(int(limit or 50), 200))
    info = await chat_info(state, chat) if chat else None
    if chat is not None and info is None:
        return {"messages": [], "calls": {}, "chat": None, "live": "", "busy": False,
                "more": False}
    async with state._connect() as db:
        if info is not None:
            sql, args = (("overlay.agent.chat_messages_before",
                          (info["id"], int(before), limit + 1))
                         if before else ("overlay.agent.chat_messages", (info["id"], limit + 1)))
        else:
            sql, args = (("overlay.agent.messages_before", (int(before), limit + 1)) if before
                         else ("overlay.agent.messages", (limit + 1,)))
        async with db.execute(queries.sql(sql), args) as cur:
            rows = [_row(r) for r in await cur.fetchall()]
        more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        if not retried:
            gone = {r["turn"] for r in rows if r["role"] == "user" and r["status"] == RETRIED}
            rows = [r for r in rows if r["turn"] not in gone]
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
    hub = _HUBS.get(_key(state))
    live = hub.live_turn(info["id"]) if hub is not None and info is not None else ""
    elsewhere, waking = "", False
    if info is not None and info["turn"] and not live \
            and info["id"] in await _held_elsewhere(state):
        elsewhere = info["turn"]
        waking = info["id"] in await _waking(state)
    for r in rows:
        # (Unfinished and answered by nobody: running when Harvey stopped,
        # or queued or held by the chat's earlier job-based version.)
        if r["role"] == "user" and r["status"] not in (ANSWERED, FAILED, RETRIED) \
                and r["turn"] not in (live, elsewhere):
            r["status"] = STOPPED
    return {"messages": rows, "calls": calls, "chat": info, "live": live,
            "busy": bool(live or elsewhere), "waking": waking, "more": more}


# ── Priming a new process ──

def mcp_config(db_path: str, chat: str, task: int | None = None) -> dict:
    """The MCP config naming Harvey's own server for one chat: this Python,
    the server module, the database and the chat (``task``: a task of the
    chat's, whose profile's tools are all the server offers, tasks.py)."""
    package_root = str(Path(__file__).resolve().parents[2])
    return {"mcpServers": {"harvey": {
        "type": "stdio", "command": sys.executable,
        "args": ["-m", "harvey.agent.tools", "--db", str(Path(db_path).resolve()),
                 "--chat", chat] + ([] if task is None else ["--task", str(int(task))]),
        "env": {"PYTHONPATH": package_root, "PYTHONIOENCODING": "utf-8"}}}}


def _call_line(c: dict) -> str:
    args = json.dumps(c["arguments"], ensure_ascii=False)[:PRIME_CALL_CHARS]
    result = " ".join(str(c["result"] or "").split())[:PRIME_CALL_CHARS]
    return f"  (tool {c['tool']}({args}): {c['outcome']}" + (f", {result})" if result else ")")


def history(rows: list[dict], calls: dict, turn: str, budget: int = PRIME_TOKENS,
            more: bool = False) -> str:
    """The conversation before ``turn``: each answered exchange (the
    message, a line per tool call, the answer), newest kept first within
    ``budget`` tokens; the older ones condensed to a note that says how
    many were left out (``more``: some weren't even read)."""
    from harvey.site_match import tokens

    exchanges: list[str] = []
    for t in dict.fromkeys(r["turn"] for r in rows):
        if t == turn:
            continue
        user = next((r for r in rows if r["turn"] == t and r["role"] == "user"), None)
        agent = next((r for r in rows if r["turn"] == t and r["role"] == "agent"), None)
        if user is None and agent is not None and agent.get("wake") and agent["status"] == OK:
            # (An answer Harvey wrote on a wake, with no message before it.)
            lines = [wake_line(agent)]
            lines += [_call_line(c) for c in calls.get(t, [])]
            lines.append(f"Harvey: {agent['text']}")
            exchanges.append("\n".join(lines))
            continue
        if user is None or agent is None or user["status"] != ANSWERED or agent["status"] != OK:
            continue
        lines = [f"User: {user['text']}"]
        lines += [_call_line(c) for c in calls.get(t, [])]
        lines.append(f"Harvey: {agent['text']}")
        exchanges.append("\n".join(lines))
    kept: list[str] = []
    used = 0
    for text in reversed(exchanges):
        cost = tokens(text)
        if used + cost > budget:
            break
        kept.append(text)
        used += cost
    kept.reverse()
    left = len(exchanges) - len(kept)
    if left or more:
        if more and not left:
            many = "some earlier exchanges"
        else:
            many = (f"{'more than ' if more else ''}{left} earlier "
                    f"exchange{'' if left == 1 and not more else 's'}")
        kept.insert(0, f"(Earlier messages were left out to keep this short: {many} of this "
                       "chat. Say so if the user refers to something you can't see.)")
    return "\n\n".join(kept)


def wake_line(row: dict) -> str:
    """The line before a wake's answer in the history: why Harvey woke."""
    why = (row.get("wake") or {}).get("label") or "woken"
    return f"(Harvey woke on its own, {why}; no message from the user)"


async def prime(state, chat_id: str, turn: str, text: str) -> str:
    """A new process's first message: prompts/agent.md, the memory, the
    chat so far (history(), from Harvey's database), then the message."""
    parts = await opening(state, chat_id, turn)
    parts.append("## The user's new message\n\n" + text)
    return "\n\n".join(parts) + "\n"


async def opening(state, chat_id: str, turn: str) -> list[str]:
    """What every new process (a chat's, a wake's) is given first, as
    parts: prompts/agent.md, the memory snapshot (memory.py: the notes,
    the playbooks' names), the chat so far before ``turn``."""
    from harvey.agent import memory
    from harvey.brain import PROMPTS_DIR
    from harvey.textio import read_user_text

    system = read_user_text(PROMPTS_DIR / "agent.md")
    system = system.replace("{{date}}", time.strftime("%Y-%m-%d")).replace(
        "{{max_turns}}", str(MAX_TURNS))
    async with state._connect() as db:
        async with db.execute(queries.sql("overlay.agent.chat_messages"),
                              (chat_id, PRIME_ROWS + 1)) as cur:
            rows = [_row(r) for r in await cur.fetchall()]
        more = len(rows) > PRIME_ROWS
        rows = rows[:PRIME_ROWS]
        rows.reverse()
        turns = list(dict.fromkeys(r["turn"] for r in rows if r["turn"] != turn))
        calls: dict[str, list] = {}
        if turns:
            async with db.execute(queries.sql("overlay.agent.calls"),
                                  (json.dumps(turns),)) as cur:
                for _, t, tool, args_, result, _ms, outcome, _at in await cur.fetchall():
                    try:
                        parsed = json.loads(args_ or "{}")
                    except ValueError:
                        parsed = {}
                    calls.setdefault(t, []).append({"tool": tool, "arguments": parsed,
                                                    "result": result, "outcome": outcome})
    past = history(rows, calls, turn, more=more)
    parts = [system.strip()]
    remembered = await memory.snapshot(state)
    if remembered:
        parts.append(remembered)
    if past:
        parts.append("## The conversation so far\n\n" + past)
    return parts


# ── The process ──

class Process:
    """One CLI process: JSON lines in and out, its stderr's tail kept, its
    MCP config folder and working folder removed when it ends (the Claude
    CLI's; driver.CodexProcess is Codex's, read the same way)."""

    label = "the Claude CLI"

    def __init__(self, proc, folders: list[str]):
        self.proc = proc
        self.folders = folders
        self.stderr = b""
        self._err = asyncio.ensure_future(self._drain_stderr())

    @classmethod
    async def start(cls, cmd: list[str], cwd: str, env: dict, folders: list[str]) -> "Process":
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LINE_BYTES)
        return cls(proc, folders)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk:
                    return
                self.stderr = (self.stderr + chunk)[-STDERR_KEPT:]
        except Exception:
            return

    async def send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def read(self) -> dict | None:
        """The next JSON event, or None at the end of its output."""
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                logger.warning("agent CLI wrote a line longer than %s bytes", LINE_BYTES)
                continue
            if not line:
                return None
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                return event

    async def interrupt(self) -> None:
        """Ask the CLI to end the answer (the control protocol's interrupt)."""
        try:
            await self.send({"type": "control_request", "request_id": uuid.uuid4().hex,
                             "request": {"subtype": "interrupt"}})
        except (ConnectionError, RuntimeError, OSError):
            pass

    async def close(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """Close its stdin (the CLI ends), kill it if it doesn't within
        ``grace`` seconds, and remove its folders."""
        try:
            if self.alive:
                try:
                    self.proc.stdin.close()
                except (ConnectionError, RuntimeError, OSError):
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    self.kill()
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=KILL_REAP_SECONDS)
                    except Exception as e:
                        logger.warning("agent CLI not reaped: %r", e)
            try:
                await asyncio.wait_for(asyncio.shield(self._err), timeout=1)
            except Exception:
                pass
        finally:
            # (Cancelled mid-wait, by the shutdown's own deadline: ended all
            # the same, never left running with nothing holding it.)
            if self.alive:
                self.kill()
            for folder in self.folders:
                shutil.rmtree(folder, ignore_errors=True)

    def kill(self) -> None:
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass


# ── An answer, as it is made ──

class Answer:
    """One message's answer: every event so far, in order, for whoever
    follows it (the dashboard's stream, again after switching chats; the
    terminal)."""

    def __init__(self, chat: str, turn: str):
        self.chat, self.turn = chat, turn
        self.events: list[dict] = []
        # The usage of the newest model call made for it (its context).
        self.usage: dict | None = None
        self.done = False
        self.stop_reason = ""
        self.stopping = asyncio.Event()
        self._changed = asyncio.Condition()

    async def emit(self, event: dict) -> None:
        async with self._changed:
            self.events.append({**event, "turn": self.turn})
            if event["type"] in ("done", "error"):
                self.done = True
            self._changed.notify_all()

    def stop(self, reason: str = "stopped") -> None:
        if not self.done and not self.stopping.is_set():
            self.stop_reason = reason
            self.stopping.set()

    async def follow(self, start: int = 0):
        """Every event from ``start`` on, as they come, until the answer ends."""
        at = start
        while True:
            async with self._changed:
                while at >= len(self.events) and not self.done:
                    await self._changed.wait()
                fresh = self.events[at:]
                done = self.done
            for event in fresh:
                yield event
            at += len(fresh)
            if done and at >= len(self.events):
                return


_STOP, _TIMEOUT = object(), object()


def _usage_delta(now: dict, before: dict) -> dict:
    """This answer's part of the CLI's modelUsage, which counts the whole
    process (the binary builds the result's modelUsage from its session's
    cost ledger): each counter less the previous result's, or as it is
    when it went down (counted per message after all)."""
    out = {}
    for model, mu in (now or {}).items():
        if not isinstance(mu, dict):
            continue
        prev = before.get(model) or {}
        row = {}
        for k, v in mu.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                p = prev.get(k, 0)
                row[k] = v - p if isinstance(p, (int, float)) and v >= p else v
        out[model] = row
    return out


def _tokens(model_usage: dict) -> int:
    return sum(int(mu.get(k) or 0) for mu in model_usage.values()
               for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                         "cacheCreationInputTokens"))


def _note_usage(answer: Answer, usage) -> None:
    """Keep a model call's usage on the answer when it says what the call
    was given (its input tokens)."""
    if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), (int, float)):
        answer.usage = usage


def context_of(payload: dict | None, call: dict | None = None) -> int:
    """The context an answer was given: the input, cache read and cache
    creation tokens of its last model call (``call``, its usage as the
    stream gave it), else of its result (the message's own usage, not the
    process's; with tool calls the result may add every call's input up,
    so the last call's is preferred)."""
    usage = call if isinstance(call, dict) else (payload or {}).get("usage")
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens")
               if isinstance(usage.get(k), (int, float)))


# ── The chats this process serves ──

def _key(state) -> str:
    return str(Path(state.db_path).resolve())


_HUBS: dict[str, "Hub"] = {}


def hub(state) -> "Hub":
    """This process's chats over ``state``'s database (one Hub per database
    and event loop)."""
    loop = asyncio.get_running_loop()
    key = _key(state)
    h = _HUBS.get(key)
    if h is None or h.loop is not loop:
        h = Hub(state, loop)
        _HUBS[key] = h
    return h


async def close_all() -> None:
    """Close every chat process this event loop serves (shutdown): each
    answer stopped (its stream ends with an error event), within
    SHUTDOWN_SECONDS, then killed."""
    loop = asyncio.get_running_loop()
    for key, h in list(_HUBS.items()):
        if h.loop is loop:
            await h.close_all()
            _HUBS.pop(key, None)


class Hub:
    """The chats of one database as this process serves them: a Live per
    chat used here, at most MAX_LIVE of them with a process."""

    def __init__(self, state, loop):
        self.state = state
        self.loop = loop
        # (The leases' owner: every chat this process serves.)
        self.owner = f"chat-{uuid.uuid4().hex[:12]}"
        self.chats: dict[str, Live] = {}
        self._room = asyncio.Lock()
        self._brain = None

    def brain(self, config):
        """The Brain for these settings. On Codex it is kept while they stay
        the same, so its quota reading is cached (reading it starts a
        runtime); on Claude a new one each message, as before."""
        from harvey.brain import Brain

        settings = getattr(config, "brain", None)
        if getattr(settings, "provider", "claude") != "codex":
            return Brain(self.state, settings)
        if self._brain is None or self._brain.config != settings:
            self._brain = Brain(self.state, settings)
        return self._brain

    def _live(self, chat_id: str) -> "Live":
        live = self.chats.get(chat_id)
        if live is None:
            live = self.chats[chat_id] = Live(self, chat_id)
        return live

    def answer(self, chat_id: str) -> Answer | None:
        live = self.chats.get(chat_id)
        return live.answer if live is not None else None

    def live_turn(self, chat_id: str) -> str:
        """The message this process is answering in that chat now, or ""."""
        a = self.answer(chat_id)
        return a.turn if a is not None and not a.done else ""

    def answering(self) -> set[str]:
        return {cid for cid, live in self.chats.items() if live.answering}

    async def ask(self, chat_id: str | None, text: str, config=None) -> Answer:
        """Send a message to a chat (None: a new one, titled from it): the
        Answer it starts (follow it for the events), or Refused before
        anything is written."""
        text, config = _checked(text, config)
        if chat_id and await chat_info(self.state, chat_id) is None:
            raise Refused("no such chat", "missing")
        if chat_id:
            return await self._live(chat_id).ask(text, config)
        new = uuid.uuid4().hex[:12]
        try:
            return await self._live(new).ask(text, config, new=True)
        except BaseException:
            self.chats.pop(new, None)
            raise

    async def retry(self, chat_id: str, turn: str, config=None) -> Answer:
        """Send a failed or stopped message again, in the same chat: its
        newest. The earlier attempt stays on file, marked retried."""
        rows = [_row(r) for r in await _all(self.state, "overlay.agent.turn", (turn,))]
        user = next((r for r in rows if r["role"] == "user" and r["chat"] == chat_id), None)
        if user is None:
            raise Refused("no such message in this chat", "missing")
        newest = await _one(self.state, "overlay.agent.chat_messages", (chat_id, 1))
        if newest is None or _row(newest)["turn"] != turn:
            raise Refused("only the chat's newest message can be sent again", "retry")
        if user["status"] in (ANSWERED, RETRIED) or self.live_turn(chat_id) == turn:
            raise Refused("only a message that failed or was stopped can be sent again", "retry")
        text, config = _checked(user["text"], config)
        return await self._live(chat_id).ask(text, config, retry_of=turn)

    async def stop(self, chat_id: str) -> bool:
        """Stop the answer being made in a chat (True when there was one)."""
        live = self.chats.get(chat_id)
        return await live.stop() if live is not None else False

    async def delete(self, chat_id: str) -> None:
        """Delete a chat: its answer stopped and its process closed, then
        its messages and their tool calls, and its tasks' tool calls
        (usage_events stay: the plan's accounting). Its open plans close (a proposed one rejected, an
        approved one done: its runs finish under their caps; the plans and
        their actions stay on file). Refused while another Harvey process
        serves it."""
        if await chat_info(self.state, chat_id) is None:
            raise Refused("no such chat", "missing")
        if chat_id in await _held_elsewhere(self.state):
            from harvey.modules.worker import lease_holder

            raise _held_refusal(await lease_holder(self.state, LEASE_TTL_SECONDS,
                                                   _lease(chat_id)), " first")
        live = self._live(chat_id)
        await live.stop()
        async with live.lock:
            # (No message can start meanwhile: it would wait for the lock,
            # then find the chat gone.)
            await live.close()
            now = int(time.time())
            await _write(self.state, ("overlay.agent.delete_chat_calls", (chat_id,)),
                         ("overlay.agent.delete_chat_task_calls", (chat_id,)),
                         ("overlay.agent.delete_chat_messages", (chat_id,)),
                         ("overlay.agent.plans_chat_deleted", (now, now, chat_id)),
                         ("overlay.agent.delete_chat", (chat_id,)))
            self.chats.pop(chat_id, None)
        # (Its plans' wakes still to come go with it.)
        from harvey.agent import wakes

        await wakes.cancel_chat(self.state, chat_id)

    async def close_all(self) -> None:
        lives = list(self.chats.values())
        for live in lives:
            if live.answer is not None:
                live.answer.stop("stopped")
        try:
            await asyncio.wait_for(asyncio.gather(*(live.close() for live in lives),
                                                  return_exceptions=True),
                                   timeout=SHUTDOWN_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("the chats didn't close within %s s: their processes are ended",
                           SHUTDOWN_SECONDS)
            await asyncio.gather(*(live.close(kill=True) for live in lives),
                                 return_exceptions=True)

    async def make_room(self, starting: "Live") -> None:
        """Under MAX_LIVE live processes (the caller holds _room): the
        least recently used idle one closed, or Refused when every one is
        answering."""
        live = [x for x in self.chats.values()
                if x is not starting and x.proc is not None and x.proc.alive]
        if len(live) < MAX_LIVE:
            return
        idle = [x for x in live if not x.answering and not x.lock.locked()]
        if not idle:
            raise Refused(f"{MAX_LIVE} chats are answering at once: wait for one to finish, "
                          "or stop one", "full")
        await min(idle, key=lambda x: x.used).close_process()


def _checked(text: str, config):
    """The message as it is sent and the settings, or Refused."""
    text = (text or "").strip()
    if not text:
        raise Refused("write a message first", "empty")
    if len(text) > MESSAGE_CHARS:
        raise Refused(f"a message is at most {MESSAGE_CHARS:,} characters", "too_long")
    config = config if config is not None else _config()
    why = refusal(config)
    if why:
        raise Refused(why, "codex")
    return text, config


class Live:
    """One chat as this process serves it: its CLI process (started on a
    message, closed when idle), the answer being made, the lease that
    keeps any other Harvey process off it."""

    def __init__(self, hub: Hub, chat_id: str):
        self.hub = hub
        self.state = hub.state
        self.chat_id = chat_id
        self.proc: Process | None = None
        self.turns_used = 0
        self.answer: Answer | None = None
        self.last_usage: dict = {}
        # The newest message of the chat its process has been given: a wake
        # answer written after it (its lease lost and taken meanwhile) is
        # put before the next message.
        self.seen_id = 0
        self.used = time.monotonic()
        self.lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._idle: asyncio.Task | None = None
        self._keeper: asyncio.Task | None = None
        self._pending: asyncio.Future | None = None

    @property
    def answering(self) -> bool:
        return self.answer is not None and not self.answer.done

    # The process

    async def _start(self, config) -> None:
        from harvey.agent import driver
        from harvey.modules.worker import acquire_lease, lease_holder

        await self.close_process()
        try:
            runner = await driver.check(config)
        except driver.Unusable as e:
            raise Refused(str(e), "cli") from None
        async with self.hub._room:
            name = _lease(self.chat_id)
            if not await acquire_lease(self.state, self.hub.owner, LEASE_TTL_SECONDS, name):
                raise _held_refusal(await lease_holder(self.state, LEASE_TTL_SECONDS, name),
                                    f", or wait until it has had no message for "
                                    f"{IDLE_SECONDS // 60} minutes")
            try:
                await self.hub.make_room(self)
            except Refused:
                await self._release()
                raise
            try:
                self.proc = await driver.start(
                    runner, self.state.db_path, self.chat_id,
                    model=getattr(getattr(config, "brain", None), "model", "") or "")
            except driver.Unusable as e:
                await self._release()
                raise Refused(str(e), "cli") from None
            except OSError as e:
                await self._release()
                raise Refused(f"{driver.label(config)} couldn't be started: {e}", "cli") from None
        self.turns_used = 0
        self.last_usage = {}
        self._pending = None
        self._keeper = asyncio.ensure_future(self._keep_lease())

    async def _keep_lease(self) -> None:
        """Renew the lease while the process lives; lost, the process ends."""
        from harvey.modules.worker import acquire_lease

        proc = self.proc
        while proc is not None and proc.alive and self.proc is proc:
            await asyncio.sleep(LEASE_TTL_SECONDS / 3)
            if self.proc is not proc:
                return
            try:
                held = await acquire_lease(self.state, self.hub.owner, LEASE_TTL_SECONDS,
                                           _lease(self.chat_id))
            except Exception as e:
                logger.debug("chat lease not renewed: %s", e)
                continue
            if self.proc is not proc:
                return
            if not held:
                logger.warning("a chat's lease was taken by another Harvey process")
                if self.answer is not None:
                    self.answer.stop("failed")
                # (The process this keeper was started for: the chat may
                # have closed it, or started another, meanwhile.)
                proc.kill()
                return

    async def _release(self) -> None:
        from harvey.modules.worker import release_lease

        try:
            await release_lease(self.state, self.hub.owner, _lease(self.chat_id))
        except Exception as e:
            logger.debug("chat lease not released: %s", e)

    async def close_process(self, kill: bool = False) -> None:
        proc, self.proc = self.proc, None
        if self._keeper is not None:
            self._keeper.cancel()
            self._keeper = None
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if proc is not None:
            try:
                if kill:
                    proc.kill()
                await proc.close()
            finally:
                await self._release()

    # Messages

    async def ask(self, text: str, config, new: bool = False, retry_of: str = "") -> Answer:
        """Send a checked message (Hub.ask, Hub.retry): ``new``, the chat's
        first; ``retry_of``, the turn it sends again."""
        async with self.lock:
            if self.answering:
                raise Refused("an answer is still coming: wait for it, or stop it", "busy")
            brain = self.hub.brain(config)
            percent = getattr(getattr(config, "usage", None), "max_daily_claude_percent", None)
            if not await brain.is_within_budget(max_percent=percent):
                raise Refused(f"Harvey's share of your {brain.provider_label} plan is used for "
                              "now (usage.max_daily_claude_percent): ask again later", "budget")
            if not new and await chat_info(self.state, self.chat_id) is None:
                raise Refused("no such chat", "missing")
            fresh = (self.proc is None or not self.proc.alive
                     or self.turns_used + MAX_TURNS > PROCESS_TURNS
                     or getattr(self.proc, "stale", False))
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            if fresh:
                await self._start(config)
            self.used = time.monotonic()
            turn = uuid.uuid4().hex[:12]
            now = _now()
            writes = []
            if new:
                # (Only once its process runs: a refused start leaves no chat.)
                writes.append(("overlay.agent.new_chat",
                               (self.chat_id, title_of(text), now, now)))
            if retry_of:
                writes.append(("overlay.agent.set_status", (RETRIED, now, retry_of)))
            writes += [("overlay.agent.add", (turn, "user", text, RUNNING, self.chat_id,
                                              now, now, 0, 0, self.chat_id)),
                       ("overlay.agent.set_chat_turn", (turn, now, self.chat_id))]
            try:
                missed = "" if fresh else await self._missed()
                await _write(self.state, *writes)
                last = await _one(self.state, "overlay.agent.chat_last_id", (self.chat_id,))
                self.seen_id = int(last[0]) if last else 0
            except BaseException:
                # (Nothing written: a process started for a chat that doesn't
                # exist, or for a message that isn't on file, is closed now,
                # not left running until Harvey stops.)
                if new or fresh:
                    await self.close_process(kill=True)
                raise
            answer = Answer(self.chat_id, turn)
            self.answer = answer
            await answer.emit({"type": "start", "chat": self.chat_id})
            self._task = asyncio.ensure_future(self._answer(answer, missed + text, brain, fresh))
            return answer

    async def _missed(self) -> str:
        """The wake answers written in the chat since its process was last
        given a message, compactly, to put before the user's text ("" when
        none)."""
        rows = [_row(r) for r in await _all(self.state, "overlay.agent.chat_wakes_after",
                                            (self.chat_id, self.seen_id, 20))]
        rows = [r for r in rows if r["status"] == OK]
        if not rows:
            return ""
        lines = ["## Written in this chat since your last message"]
        for r in rows:
            lines.append(wake_line(r) + "\nHarvey: " + " ".join(r["text"].split())[:2000])
        return "\n\n".join(lines) + "\n\n## The user's new message\n\n"

    async def stop(self) -> bool:
        """Stop the answer being made (True when there was one)."""
        answer = self.answer
        if answer is None or answer.done:
            return False
        answer.stop("stopped")
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task),
                                       timeout=STOP_GRACE_SECONDS + CLOSE_GRACE_SECONDS + 5)
            except Exception:
                pass
        return True

    async def close(self, kill: bool = False) -> None:
        """Stop any answer and close the process (the chat stays)."""
        if not kill:
            await self.stop()
        elif self.answer is not None:
            self.answer.stop("stopped")
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        await self.close_process(kill=kill)
        if self.answer is not None and not self.answer.done:
            await self._end_stream(self.answer)

    async def _close_when_idle(self) -> None:
        try:
            await asyncio.sleep(IDLE_SECONDS)
        except asyncio.CancelledError:
            return
        async with self.lock:
            if not self.answering:
                await self.close_process()

    # One answer

    async def _next(self, answer: Answer, deadline: float):
        """The next event, _STOP once a stop is asked for, _TIMEOUT past
        ``deadline``; the read in progress is kept for the next call."""
        if self.proc is None:
            return None
        if answer.stopping.is_set():
            return _STOP
        if self._pending is None:
            self._pending = asyncio.ensure_future(self.proc.read())
        waits = {self._pending}
        stop = asyncio.ensure_future(answer.stopping.wait())
        waits.add(stop)
        try:
            done, _ = await asyncio.wait(waits, timeout=max(0.0, deadline - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if self._pending in done:
            read, self._pending = self._pending, None
            return read.result()
        if answer.stopping.is_set():
            return _STOP
        return _TIMEOUT

    async def _answer(self, answer: Answer, text: str, brain, fresh: bool) -> None:
        try:
            await self._make(answer, text, brain, fresh)
        finally:
            await self._end_stream(answer)

    async def _make(self, answer: Answer, text: str, brain, fresh: bool) -> None:
        state = self.state
        started = time.monotonic()
        deadline = started + ANSWER_TIMEOUT_SECONDS
        turns, ids, streamed = 0, set(), set()
        delta_pending = False
        current = ""
        last_call = 0
        result = None
        ended = ""             # stopped / max_turns / timeout / gone
        try:
            # (A new process is given the prompt, and the chat so far when
            # it has one; a live one only what was typed.)
            content = await prime(state, answer.chat, answer.turn, text) if fresh else text
            await self.proc.send({"type": "user", "message": {"role": "user",
                                                              "content": content}})
            while True:
                event = await self._next(answer, deadline)
                if event is _STOP or event is _TIMEOUT:
                    ended = (answer.stop_reason or "stopped") if event is _STOP else "timeout"
                    result = await self._interrupt(deadline_extra=STOP_GRACE_SECONDS)
                    break
                if event is None:
                    ended = "gone"
                    break
                kind = event.get("type")
                if kind == "stream_event":
                    e = event.get("event") or {}
                    if e.get("type") == "message_start":
                        current = str((e.get("message") or {}).get("id") or "")
                        _note_usage(answer, (e.get("message") or {}).get("usage"))
                    delta = e.get("delta") or {}
                    if e.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        if delta.get("text"):
                            await answer.emit({"type": "text", "text": delta["text"]})
                            delta_pending = True
                            if current:
                                streamed.add(current)
                elif kind == "assistant":
                    message = event.get("message") or {}
                    _note_usage(answer, message.get("usage"))
                    mid = str(message.get("id") or "")
                    if not mid or mid not in ids:
                        turns += 1
                        if mid:
                            ids.add(mid)
                    skip_text = (mid and mid in streamed) or (not mid and delta_pending)
                    delta_pending = False
                    for block in message.get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and block.get("text") and not skip_text:
                            await answer.emit({"type": "text", "text": block["text"]})
                        elif block.get("type") == "tool_use":
                            from harvey.agent.readonly import mask_result

                            name = str(block.get("name") or "")
                            await answer.emit({
                                "type": "tool_use", "id": str(block.get("id") or ""),
                                "tool": name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX)
                                else name,
                                "arguments": mask_result(block.get("input") or {})})
                elif kind == "user":
                    last_call = await self._emit_calls(answer, last_call)
                    if turns >= MAX_TURNS:
                        answer.stop("max_turns")
                elif kind == "rate_limit_event":
                    # (The plan's usage, as the CLI reports it: the gauge
                    # the budget check reads.)
                    from harvey.integrations import quota

                    await quota.note(state, event)
                elif kind == "result":
                    result = event
                    break
            await self._emit_calls(answer, last_call)
        except asyncio.CancelledError:
            ended = ended or "stopped"
            raise
        except Exception as e:
            logger.exception("a chat's answer failed")
            ended = ended or "gone"
            await self._finish(answer, brain, None, "failed",
                               f"{type(e).__name__}: {e}"[:300], turns, started)
            await self.close_process(kill=True)
            return
        finally:
            self.turns_used += turns
        await self._settle(answer, brain, result, ended, turns, started)

    async def _end_stream(self, answer: Answer) -> None:
        """Whatever happened, whoever follows the answer sees it end."""
        if not answer.done:
            try:
                await answer.emit({"type": "error", "code": "failed",
                                   "detail": "the answer couldn't be written", "tokens": 0,
                                   "context": 0, "hint": False})
            except Exception:
                answer.done = True

    async def _interrupt(self, deadline_extra: float) -> dict | None:
        """Interrupt the answer: the result the CLI ends it with, or None
        (the process killed) when none comes within ``deadline_extra``."""
        if self.proc is None:
            return None
        await self.proc.interrupt()
        until = time.monotonic() + deadline_extra
        while time.monotonic() < until:
            if self._pending is None:
                self._pending = asyncio.ensure_future(self.proc.read())
            try:
                event = await asyncio.wait_for(asyncio.shield(self._pending),
                                               timeout=max(0.0, until - time.monotonic()))
            except asyncio.TimeoutError:
                break
            self._pending = None
            if event is None:
                return None
            if event.get("type") == "result":
                return event
        await self.close_process(kill=True)
        return None

    async def _emit_calls(self, answer: Answer, after: int) -> int:
        """The turn's tool calls recorded since ``after`` (by Harvey's
        server), as tool_result events; the last id."""
        rows = await _all(self.state, "overlay.agent.calls_after", (answer.turn, after))
        for cid, tool, _args, summary, ms, outcome in rows:
            await answer.emit({"type": "tool_result", "tool": tool, "ms": ms,
                               "outcome": outcome, "result": summary})
            after = cid
        return after

    async def _settle(self, answer, brain, result, ended, turns, started) -> None:
        """Write how the answer ended, from the result (or its absence)."""
        from harvey.agent import driver

        if result is None:
            # (The call ran, and used the plan, until it ended: counted.)
            detail = {"stopped": "stopped before the answer was finished",
                      "max_turns": f"stopped after {MAX_TURNS} turns without an answer",
                      "timeout": f"no answer after {ANSWER_TIMEOUT_SECONDS} s",
                      "failed": "the chat's process was taken over"}.get(ended, "")
            code = ended if ended in ("stopped", "max_turns", "timeout") else "failed"
            # (No result: the process is gone, was ended, or is no longer to
            # be trusted mid-answer. Reaped, its folders removed and the
            # lease let go now, its stderr read to the end; the next message
            # starts a new one.)
            proc = self.proc
            await self.close_process(kill=ended != "gone")
            if ended == "gone":
                err = (proc.stderr if proc is not None else b"").decode(
                    "utf-8", errors="replace").strip()
                exit_code = proc.proc.returncode if proc is not None else None
                if any(p in err.lower() for p in _LOGIN_PATTERNS):
                    code, detail = "login", driver.login_detail(brain.provider)
                else:
                    who = proc.label if proc is not None else "the CLI"
                    detail = f"{who} ended (exit {exit_code}): {err[:300]}".strip()
            await self._finish(answer, brain, None, code, detail or "no answer", turns, started)
            return
        payload = result
        subtype = str(payload.get("subtype") or "")
        response = str(payload.get("result") or "").strip()
        if ended in ("stopped", "max_turns", "timeout"):
            detail = {"stopped": "stopped before the answer was finished",
                      "max_turns": f"stopped after {MAX_TURNS} turns without an answer",
                      "timeout": f"no answer after {ANSWER_TIMEOUT_SECONDS} s"}[ended]
            await self._finish(answer, brain, payload, ended, detail, turns, started)
        elif subtype == "error_max_turns":
            await self._finish(answer, brain, payload, "max_turns",
                               f"stopped after {MAX_TURNS} turns without an answer",
                               turns, started)
        elif payload.get("is_error") or not response:
            # A logged-out CLI says so in its result, not on stderr.
            if payload.get("error_kind") == "auth" \
                    or any(p in response.lower() for p in _LOGIN_PATTERNS):
                await self._finish(answer, brain, payload, "login",
                                   driver.login_detail(brain.provider), turns, started)
            else:
                await self._finish(answer, brain, payload, "failed",
                                   response[:300] or "no answer", turns, started)
        else:
            await self._finish(answer, brain, payload, OK, response, turns, started)
        self._idle = asyncio.ensure_future(self._close_when_idle())

    async def _finish(self, answer, brain, payload, code: str, text: str, turns: int,
                      started: float) -> None:
        """Record the call's usage, write the answer, end the stream."""
        state = self.state
        tokens = 0
        context = context_of(payload, answer.usage)
        try:
            await state.increment_usage(provider=brain.provider)
            if payload is None:
                # (Ended with no result: the call ran and used the plan, its
                # tokens unknown. A row all the same, so the call is counted
                # in usage_events too.)
                await brain._record_usage(
                    {"usage": {}, "num_turns": turns, "is_error": True,
                     "duration_ms": int((time.monotonic() - started) * 1000)},
                    agent="agent", task="chat", label=answer.turn)
            else:
                delta = _usage_delta(payload.get("modelUsage") or {}, self.last_usage)
                if isinstance(payload.get("modelUsage"), dict):
                    self.last_usage = payload["modelUsage"]
                tokens = _tokens(delta)
                await brain._record_usage(
                    {"modelUsage": delta, "num_turns": turns,
                     "duration_ms": int((time.monotonic() - started) * 1000),
                     "session_id": payload.get("session_id") or "",
                     "is_error": code != OK}, agent="agent", task="chat", label=answer.turn)
        except Exception as e:
            logger.warning("agent usage not recorded: %s", e)
        status = OK if code == OK else f"error:{code}"
        now = _now()
        try:
            await _write(state,
                         ("overlay.agent.add", (answer.turn, "agent", text, status, answer.chat,
                                                now, now, tokens, context, answer.chat)),
                         ("overlay.agent.set_status",
                          (ANSWERED if code == OK else FAILED, now, answer.turn)),
                         ("overlay.agent.set_chat_turn", ("", now, answer.chat)))
        except Exception:
            logger.exception("a chat's answer couldn't be written")
        try:
            info = await chat_info(state, answer.chat) or {"context": context}
        except Exception:
            info = {"context": context}
        size = {"tokens": tokens, "context": info["context"],
                "hint": info["context"] >= CONTEXT_HINT_TOKENS}
        if code == OK:
            await answer.emit({"type": "done", "status": OK, "text": text, **size})
        else:
            await answer.emit({"type": "error", "code": code, "detail": text, **size})
