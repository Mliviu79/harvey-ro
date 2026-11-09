"""The Claude account linked in Harvey (``brain.claude.auth: account``): linking it
from Settings → Model by relaying the CLI's own login.

Harvey never signs in to Claude itself, and never reads, keeps or shows a
Claude credential. POST /api/brain/claude/link starts the unmodified
``claude auth login`` (``--claudeai`` for a subscription, ``--console`` for
an Anthropic Console account) under a pseudo-terminal, its
CLAUDE_CONFIG_DIR a fresh staging folder data/auth/claude-link (0700), with
the environment every Claude child gets (claude_runtime.runtime_env: no
credential or provider route of Harvey's own, CLAUDE.md and memory off).
The CLI prints a page to open; the panel shows it once it is checked
(https, a Claude or Anthropic host, else the link fails). The user signs
in there and pastes the code the page gives into the panel, which writes
it to the CLI's prompt (submit_code: one line of printable characters,
never logged or kept). The CLI exchanges it and keeps the login in its own
files in that folder. A malformed code is the CLI's "Invalid code" and the
panel asks again; a code the CLI refuses ends the link (failed, refused).

Whether it worked is ``claude auth status`` in the staging folder, never a
file: logged in, the folder is moved into data/auth/claude by one rename
(the folder there before is put aside first, put back if the rename fails,
and removed once it succeeded), with Harvey's own note of the kind of
account beside it (brain_auth.claude_account_kind: a Console account is
billed per token, so held by the brain's USD caps). The staging folder is
removed whatever happens. One link at a time per install (the lease LEASE,
which an unlink takes too); a link not finished within LINK_SECONDS
expires and its process ends.

There is no renewal: the CLI refreshes its own login when it runs. When
``claude auth status`` says the linked account is logged out, the panel
and Today say "link the account again". Unlinking is the CLI's own
``claude auth logout`` in that folder, then the folder removed.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("harvey.claude_link")

LEASE = "claude_account"
# How long a link may take, from the start to the code's outcome; how long
# the CLI has to print its page; how long it has to answer a pasted code.
LINK_SECONDS = 15 * 60
URL_SECONDS = 60
CODE_SECONDS = 60
# A line saying it signed in, with no exit after it: it is asked how it is
# logged in after this long.
SETTLE_SECONDS = 5
CLI_SECONDS = 30
STOP_GRACE_SECONDS = 3
# How long a finished link's outcome stays readable (the panel's poll).
KEEP_DONE_SECONDS = 600
CODE_MAX_CHARS = 512
OUTPUT_MAX_CHARS = 64 * 1024
# The hosts the CLI's sign-in page may be on (each or a subdomain of it).
LOGIN_HOSTS = ("claude.com", "claude.ai", "anthropic.com")

PENDING, WAITING_CODE, LINKED, EXPIRED, FAILED, CANCELLED, IDLE = (
    "pending", "waiting_code", "linked", "expired", "failed", "cancelled", "idle")
KINDS = {"subscription": "--claudeai", "console": "--console"}

# The CLI's output, read for these only (never logged): terminal escapes
# (an OSC 8 hyperlink carries the page twice), the page, and what it says
# about a pasted code.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b[@-_]?")
_URL = re.compile(r"https?://[^\s<>\"'`\x00-\x1f\x7f]+")
_INVALID = re.compile(r"invalid code", re.I)
_REFUSED = re.compile(r"login failed|wasn't accepted|was not accepted", re.I)
_SIGNED_IN = re.compile(r"login successful|logged in as|successfully logged in", re.I)
_CODE = re.compile(r"[!-~]+")


class LinkError(Exception):
    """A link (or an unlink) that can't start or go on: ``kind`` busy,
    setup (no usable CLI), url, code, state or failed."""

    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind


def account_home() -> Path:
    from harvey import brain_auth

    return brain_auth.account_dir("claude")


def staging_home() -> Path:
    from harvey import brain_auth

    return brain_auth.AUTH_DIR / "claude-link"


def _aside_home() -> Path:
    from harvey import brain_auth

    return brain_auth.AUTH_DIR / "claude-old"


def has_account() -> bool:
    """Whether Harvey linked a Claude account (its own note says so; the
    CLI's files are never looked at)."""
    from harvey import brain_auth

    return bool(brain_auth.claude_account_kind())


def plain(text: str) -> str:
    """The CLI's output without terminal escapes."""
    return _ESC.sub("", _CSI.sub("", _OSC.sub("", text or "")))


def login_url(text: str) -> str | None:
    """The first page the output names, or None. Only a whole one: the
    output comes in pieces, and a page the output ends on (or one still
    inside an unfinished escape) may be cut short, so it waits for what
    follows it."""
    text = plain(text)
    m = _URL.search(text)
    return m.group(0) if m and m.end() < len(text) else None


def url_problem(url: str) -> str:
    """"" when ``url`` is a page the user may be sent to sign in: https, a
    Claude or Anthropic host, no credentials or port in it."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "not a URL"
    if parts.scheme != "https":
        return "not https"
    if parts.username or parts.password or port not in (None, 443):
        return "not a plain https page"
    if not any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS):
        return "not a Claude or Anthropic page"
    return ""


def code_problem(code) -> str:
    """"" when ``code`` can be written to the CLI's prompt: one line of
    printable characters, no spaces, at most CODE_MAX_CHARS."""
    if not isinstance(code, str):
        return "Paste the code the page gave you."
    code = code.strip()
    if not code:
        return "Paste the code the page gave you."
    if len(code) > CODE_MAX_CHARS or not _CODE.fullmatch(code):
        return "That isn't a code from the sign-in page: paste it again, as one line."
    return ""


# ── The CLI under a pseudo-terminal ──

def _become_terminal_owner() -> None:  # pragma: no cover  (runs in the child)
    """The child's own session, its pseudo-terminal the controlling one
    (as a terminal gives it: the CLI only prompts on a terminal)."""
    import fcntl
    import termios

    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@dataclass
class _Cli:
    process: asyncio.subprocess.Process
    master: int
    out: str = ""
    # How many characters the CLI has written in all (``out`` keeps the
    # last OUTPUT_MAX_CHARS): a mark is a count, so since() still finds
    # what followed it once the start of ``out`` was dropped.
    total: int = 0
    eof: bool = False
    closed: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")(
        errors="replace"))

    def on_readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except OSError:
            data = b""
        if not data:
            self.eof = True
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except (RuntimeError, ValueError, OSError):
                pass
        else:
            text = self.decoder.decode(data)
            self.total += len(text)
            self.out = (self.out + text)[-OUTPUT_MAX_CHARS:]
        self.changed.set()

    def since(self, mark: int) -> str:
        """What the CLI wrote after ``mark`` (a total), as much as is kept."""
        n = self.total - mark
        return self.out[-n:] if n > 0 else ""

    async def wait(self, deadline: float) -> bool:
        """Wait for more output, the end, or ``deadline`` (monotonic).
        False once the deadline passed."""
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        self.changed.clear()
        if self.process.returncode is not None:
            return True
        waiter = asyncio.ensure_future(self.changed.wait())
        ended = asyncio.ensure_future(self.process.wait())
        try:
            await asyncio.wait({waiter, ended}, timeout=min(left, 1.0),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not ended.done():
                ended.cancel()
        return time.monotonic() < deadline

    def write(self, text: str) -> None:
        if self.closed:
            raise OSError("the terminal is closed")
        os.write(self.master, text.encode("utf-8"))

    async def stop(self) -> None:
        """End the CLI (its whole session) and close the terminal."""
        p = self.process
        if p.returncode is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        p.send_signal(sig)
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(p.wait(), STOP_GRACE_SECONDS)
                    break
                except asyncio.TimeoutError:
                    continue
        if not self.closed:
            # (Once: a second close could end a descriptor the number was
            # given to since, a database's or a socket's.)
            self.closed = True
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except (RuntimeError, ValueError, OSError):
                pass
            try:
                os.close(self.master)
            except OSError:
                pass
        self.out = ""


def _env(cwd: str, home: Path) -> dict:
    """A Claude child's environment (runtime_env, no safe mode: CLAUDE.md
    and memory off all the same), its config folder ``home``."""
    from harvey import claude_runtime

    env = claude_runtime.runtime_env(cwd, safe=False)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env["TERM"] = "dumb"
    return env


async def _spawn_login(exe: str, kind: str, home: Path, cwd: str) -> _Cli:
    import pty

    master, slave = pty.openpty()
    try:
        process = await asyncio.create_subprocess_exec(
            exe, "auth", "login", KINDS[kind], stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd, env=_env(cwd, home), preexec_fn=_become_terminal_owner)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    cli = _Cli(process=process, master=master)
    asyncio.get_running_loop().add_reader(master, cli.on_readable)
    return cli


async def _run(exe: str, args: list[str], home: Path,
               timeout: float = CLI_SECONDS) -> tuple[int, str]:
    """One short CLI command with ``home`` as its config folder: (exit
    code, stdout). Raises LinkError on a timeout or a CLI that won't run."""
    from harvey import claude_runtime

    with claude_runtime.isolated_cwd() as cwd:
        try:
            p = await asyncio.create_subprocess_exec(
                exe, *args, cwd=cwd, env=_env(cwd, home), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
        except OSError as e:
            raise LinkError(f"The Claude CLI didn't start ({type(e).__name__}).",
                            "setup") from None
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            await p.wait()
            raise LinkError(f"'claude {' '.join(args)}' didn't answer in {int(timeout)} s.",
                            "timeout") from None
    return p.returncode, out.decode("utf-8", errors="replace")


async def logged_in(exe: str, home: Path) -> dict:
    """``claude auth status`` (JSON) with ``home`` as the config folder:
    brain_auth.parse_claude_auth's {logged_in, method, plan, api}."""
    from harvey import brain_auth

    _, out = await _run(exe, ["auth", "status", "--json"], home)
    return brain_auth.parse_claude_auth(out)


def _relay_problem() -> str:
    """"" when this system can run the CLI under a pseudo-terminal (Linux,
    macOS, Docker); else why not (Windows has none)."""
    try:
        import fcntl  # noqa: F401
        import pty  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return ("Linking a Claude account from the dashboard needs Linux or macOS (or Harvey "
                "in Docker): on this system, use the terminal login or an API key.")
    return ""


def _cli() -> str:
    from harvey import claude_runtime

    try:
        return claude_runtime.check_cli()
    except claude_runtime.ClaudeRuntimeError as e:
        raise LinkError(str(e), "setup") from None


# ── The link ──

@dataclass
class _Link:
    owner: str
    kind: str
    state: object = None
    status: str = PENDING
    url: str = ""
    expires_at: float = 0.0
    deadline: float = 0.0
    problem: str = ""
    why: str = ""
    method: str = ""
    plan: str = ""
    ended_at: float = 0.0
    # Where the output stood when the code was written (what follows is
    # the CLI's answer to it), and when.
    mark: int = 0
    code_at: float = 0.0
    # Whether any code was written: an end after one is decided by the
    # CLI's status, whatever its output said last.
    tried: bool = False
    finishing: bool = False
    cwd: str = ""
    cli: _Cli | None = None
    exe: str = ""
    task: asyncio.Task | None = None
    keeper: asyncio.Task | None = None


_link: _Link | None = None


def _public(link: _Link | None) -> dict:
    if link is None or (link.ended_at and time.time() - link.ended_at > KEEP_DONE_SECONDS):
        return {"status": IDLE}
    out = {"status": link.status, "account": link.kind}
    if link.status in (PENDING, WAITING_CODE):
        out["expires_in"] = max(0, int(link.expires_at - time.time()))
        if link.url:
            out["url"] = link.url
    if link.status == WAITING_CODE and link.problem:
        out["problem"] = link.problem
    if link.status == FAILED:
        out["kind"] = link.why or "failed"
    if link.status == LINKED:
        out.update({k: v for k, v in (("method", link.method), ("plan", link.plan)) if v})
    return out


def status() -> dict:
    """The link for the panel: {status (idle, pending, waiting_code,
    linked, expired, failed, cancelled), account (subscription or
    console), url and expires_in (pending, waiting_code), problem
    (waiting_code: invalid_code once the CLI said the last one was), kind
    (failed: its class), method and plan (linked)}."""
    return _public(_link)


def _busy() -> bool:
    return _link is not None and _link.status in (PENDING, WAITING_CODE) and not _link.ended_at


def _clear_staging() -> None:
    shutil.rmtree(staging_home(), ignore_errors=True)


def _fresh_staging() -> Path:
    from harvey import brain_auth

    _clear_staging()
    brain_auth._owner_only(brain_auth.AUTH_DIR)        # (data/auth, owner-only)
    path = staging_home()
    path.mkdir(mode=0o700)
    return path


async def start(state, kind: str = "subscription", on_linked=None) -> dict:
    """Start a link of a ``kind`` account (subscription or console):
    status()'s waiting_code answer with the page to open, once the CLI
    printed it. ``on_linked`` (async, no arguments) runs once the account
    is in place: the dashboard sets brain.claude.auth there. Raises LinkError."""
    global _link
    from harvey import codex_link
    from harvey.modules import worker

    if kind not in KINDS:
        raise LinkError("Say which account: subscription or console.", "state")
    if _busy():
        raise LinkError("A link is already under way: finish or cancel it first.", "busy")
    problem = _relay_problem()
    if problem:
        raise LinkError(problem, "setup")
    exe = _cli()
    owner = f"link-{uuid.uuid4().hex[:12]}"
    if not await worker.acquire_lease(state, owner, codex_link.LEASE_TTL_SECONDS, LEASE):
        raise LinkError("Another link or unlink of the Claude account is running: try again "
                        "in a minute.", "busy")
    keeper = asyncio.create_task(codex_link._keep_lease(state, owner, LEASE))
    link = _Link(owner=owner, kind=kind, state=state, exe=exe, keeper=keeper,
                 expires_at=time.time() + LINK_SECONDS,
                 deadline=time.monotonic() + LINK_SECONDS)
    try:
        home = _fresh_staging()
        # (An empty working folder of its own, kept while the CLI runs.)
        link.cwd = str(Path(tempfile.mkdtemp(prefix="harvey-claude-link-")).resolve())
        link.cli = await _spawn_login(exe, kind, home, link.cwd)
        url = await _read_url(link.cli)
    except BaseException as e:
        link.status = FAILED
        link.why = e.kind if isinstance(e, LinkError) else "setup"
        await _end(link)
        if isinstance(e, LinkError):
            raise
        if isinstance(e, OSError):
            raise LinkError(f"The Claude CLI's login didn't start ({type(e).__name__}).",
                            "setup") from None
        raise
    link.url, link.status = url, WAITING_CODE
    _link = link
    link.task = asyncio.create_task(_follow(link, on_linked))
    logger.info("claude link: the CLI printed its sign-in page (%s account)", kind)
    return _public(link)


async def _read_url(cli: _Cli) -> str:
    deadline = time.monotonic() + URL_SECONDS
    while True:
        url = login_url(cli.out)
        if url:
            problem = url_problem(url)
            if problem:
                # (Only the class is said: the page's address isn't logged.)
                raise LinkError(f"The Claude CLI pointed to a page Harvey won't send you to "
                                f"({problem}).", "url")
            return url
        if cli.eof or cli.process.returncode is not None:
            raise LinkError("The Claude CLI's login ended before it showed a sign-in page.",
                            "setup")
        if not await cli.wait(deadline):
            raise LinkError(f"The Claude CLI showed no sign-in page within {URL_SECONDS} s.",
                            "timeout")


async def submit_code(code) -> dict:
    """Write the pasted ``code`` to the CLI's prompt (it is never logged or
    kept). The status afterwards (pending while the CLI checks it).
    Raises LinkError (kind code: not a code; state: no link waits for
    one)."""
    link = _link
    if link is None or link.status != WAITING_CODE or link.cli is None or link.ended_at:
        raise LinkError("No link is waiting for a code: start one with Link account.", "state")
    problem = code_problem(code)
    if problem:
        raise LinkError(problem, "code")
    link.mark = link.cli.total
    link.code_at = time.monotonic()
    link.tried = True
    link.problem = ""
    link.status = PENDING
    try:
        # (A carriage return, as the Enter key sends.)
        link.cli.write(code.strip() + "\r")
    except OSError:
        # (The CLI is gone: the link's own wait finds it ended.)
        raise LinkError("The Claude CLI's login has ended: start again.", "state") from None
    return _public(link)


async def _follow(link: _Link, on_linked) -> None:
    """Wait for the CLI's answer to each code, then decide: linked when
    ``claude auth status`` in the staging folder says so."""
    cli = link.cli
    linked = False
    try:
        while True:
            answer = plain(cli.since(link.mark)) if link.code_at else ""
            ended = cli.process.returncode is not None or (cli.eof and link.code_at)
            if link.status == PENDING and _INVALID.search(answer) and not ended:
                # (The CLI asks again: the panel says so and takes another.)
                link.status, link.problem = WAITING_CODE, "invalid_code"
                link.mark, link.code_at = cli.total, 0.0
                continue
            if link.status == PENDING and link.code_at and (
                    ended or _REFUSED.search(answer)
                    or (_SIGNED_IN.search(answer)
                        and time.monotonic() - link.code_at >= SETTLE_SECONDS)
                    or time.monotonic() - link.code_at >= CODE_SECONDS):
                refused = bool(_REFUSED.search(answer))
                if not ended:
                    await asyncio.wait({asyncio.ensure_future(cli.process.wait())},
                                       timeout=SETTLE_SECONDS)
                link.finishing = True
                await cli.stop()
                linked = await _decide(link, refused)
                break
            if ended:
                if link.tried:
                    # (It ended after a code, its last words read as asking
                    # again: whether it signed in is its status that says.)
                    link.finishing = True
                    await cli.stop()
                    linked = await _decide(link, False)
                else:
                    link.status, link.why = FAILED, "cli"
                break
            if not await cli.wait(link.deadline):
                link.status = EXPIRED
                break
        if linked and on_linked is not None:
            try:
                await on_linked()
            except Exception as e:
                logger.warning("claude link: saving brain.claude.auth failed (%s)", type(e).__name__)
                link.why = "config"
        if linked:
            # (Said only once brain.auth is saved: the panel reloads on it.)
            link.status = LINKED
    except asyncio.CancelledError:
        link.status = CANCELLED
        raise
    except Exception as e:
        link.status, link.why = FAILED, "failed"
        logger.warning("claude link: the link failed (%s)", type(e).__name__)
    finally:
        await _end(link)


async def _decide(link: _Link, refused: bool) -> bool:
    """After the CLI's answer: ask it how the staging folder is logged in;
    logged in, move the folder in. False (the link failed, with why)
    otherwise."""
    try:
        found = await logged_in(link.exe, staging_home())
    except LinkError as e:
        link.status, link.why = FAILED, e.kind
        return False
    if not found["logged_in"]:
        link.status, link.why = FAILED, "refused" if refused else "not_linked"
        return False
    try:
        _move_in(link.kind)
    except OSError as e:
        link.status, link.why = FAILED, "save"
        logger.warning("claude link: moving the account in failed (%s)", type(e).__name__)
        return False
    link.method, link.plan = found["method"], found["plan"]
    logger.info("claude link: linked (%s account)", link.kind)
    return True


def _write_note(kind: str) -> None:
    from harvey import brain_auth

    note = brain_auth.claude_link_note()
    tmp = note.with_name(note.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"kind": kind, "linked_at": int(time.time())}, f)
    os.replace(tmp, note)


def _move_in(kind: str) -> None:
    """The staging folder becomes data/auth/claude by one rename, so a call
    never sees half a login; the folder there before is put aside until
    the rename succeeded (put back if it didn't), then removed. Harvey's
    note of the account's kind is written first (a Console account is
    held by the caps from its first call) and put back with the folder."""
    from harvey import brain_auth

    target, aside, note = account_home(), _aside_home(), brain_auth.claude_link_note()
    shutil.rmtree(aside, ignore_errors=True)
    old_note = note.read_bytes() if note.exists() else None
    _write_note(kind)
    had = target.exists()
    try:
        if had:
            os.replace(target, aside)
        try:
            os.replace(staging_home(), target)
        except OSError:
            if had:
                os.replace(aside, target)
            raise
    except OSError:
        if old_note is None:
            note.unlink(missing_ok=True)
        else:
            note.write_bytes(old_note)
        raise
    shutil.rmtree(aside, ignore_errors=True)


async def _end(link: _Link) -> None:
    """Whatever way a link ended: its process ended, the page forgotten,
    the staging folder removed, the lease released."""
    from harvey import brain_auth, codex_link
    from harvey.modules import worker

    if link.status in (PENDING, WAITING_CODE):
        # (Ended some other way: never left waiting for a code nobody can type.)
        link.status, link.why = FAILED, link.why or "failed"
    link.ended_at = time.time()
    link.url = ""
    try:
        if link.cli is not None:
            await link.cli.stop()
    finally:
        _clear_staging()
        if link.cwd:
            shutil.rmtree(link.cwd, ignore_errors=True)
        await codex_link._stop_keeping(link.keeper)
    try:
        await worker.release_lease(link.state, link.owner, LEASE)
    except Exception as e:
        logger.debug("claude link: releasing the lease failed: %s", e)
    brain_auth.forget()


async def cancel() -> dict:
    """Cancel a link under way: its process ends, nothing moves in. The
    status afterwards."""
    link = _link
    if link is None or link.task is None or link.ended_at:
        return status()
    if link.finishing:
        # (The CLI answered: the account is being moved in; let it land.)
        await asyncio.gather(link.task, return_exceptions=True)
        return status()
    link.task.cancel()
    await asyncio.gather(link.task, return_exceptions=True)
    if not link.ended_at:
        # (Cancelled before its wait began: nothing of it has run.)
        link.status = CANCELLED
        await _end(link)
    return status()


async def close() -> None:
    """Stop a link under way (the dashboard shutting down)."""
    await cancel()


async def unlink(state) -> bool:
    """Remove the linked account: the CLI's own ``claude auth logout`` in
    data/auth/claude, then the folder and Harvey's note removed (either
    way). True when there was one. Raises LinkError busy while a link
    runs."""
    from harvey import brain_auth, codex_link
    from harvey.modules import worker

    folder = account_home()
    if not folder.exists() and not has_account():
        return False
    if _busy():
        raise LinkError("A link is under way: finish or cancel it first.", "busy")
    owner = f"unlink-{uuid.uuid4().hex[:12]}"
    if not await worker.acquire_lease(state, owner, codex_link.LEASE_TTL_SECONDS, LEASE):
        raise LinkError("A link of the Claude account is running: try again in a minute.",
                        "busy")
    keeper = asyncio.create_task(codex_link._keep_lease(state, owner, LEASE))
    try:
        if folder.exists():
            try:
                code, _ = await _run(_cli(), ["auth", "logout"], folder)
                if code != 0:
                    logger.warning("claude link: 'claude auth logout' exited %s; the folder is "
                                   "removed all the same", code)
            except LinkError as e:
                logger.warning("claude link: 'claude auth logout' didn't run (%s); the folder "
                               "is removed all the same", e.kind)
        shutil.rmtree(folder, ignore_errors=True)
        brain_auth.claude_link_note().unlink(missing_ok=True)
    finally:
        await codex_link._stop_keeping(keeper)
        await worker.release_lease(state, owner, LEASE)
        brain_auth.forget()
    return True
