"""How the brain's CLI is logged in (``brain.auth``), and what Settings →
Model says about it.

- ``terminal`` (the default, and what every install had before the
  setting): the login the user made outside Harvey, which the CLI keeps in
  its own folder (``claude login``: ~/.claude or the CLAUDE_CONFIG_DIR the
  user set; ``codex login``: ~/.codex or CODEX_HOME).
- ``account``: a login kept in a folder of Harvey's own on the data volume,
  data/auth/claude and data/auth/codex (made 0700). Claude's children run
  with CLAUDE_CONFIG_DIR pointed there; Codex takes its login from there
  instead of ~/.codex (an access-only copy per call, as codex.py does with
  any login). Harvey never reads Claude's files there: only the CLI does,
  and whether it is logged in is the CLI's own ``claude auth status``. The
  dashboard's link flow that fills these folders comes later; until then a
  status says the folder isn't linked yet.
- ``api_key`` (config.BRAIN_AUTH_MODES, not yet in BRAIN_AUTH_READY): a key
  billed per token. claude_env() is the one place a mode adds to a Claude
  child's environment.

Whatever the mode, credentials in Harvey's own environment never reach a
child: claude_runtime.runtime_env drops CREDENTIAL_ENV (so a key added to
.env by hand can't silently move Claude to per-token billing), and Codex's
child gets an allowlisted environment (codex.ENV_ALLOWLIST: never
OPENAI_API_KEY or CODEX_API_KEY).

status() is the panel's line and /api/setup-status's ``brain`` entry: the
provider, model and mode, the CLI's version check, linked as what (the
login method and the plan, never an email or an organisation), and the
class of the last error the brain recorded. Each CLI call has a timeout and
the answer is kept briefly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from harvey.paths import PROJECT_ROOT

TERMINAL, ACCOUNT, API_KEY = "terminal", "account", "api_key"
AUTH_DIR = PROJECT_ROOT / "data" / "auth"
# The ways to link the panel offers: the others' flows aren't in this
# release (their buttons stay hidden; the data model already knows them).
OFFERED = (TERMINAL,)

# The Claude CLI's documented model aliases ("" is the CLI's own default);
# any other model name is typed in.
CLAUDE_MODELS = ("opus", "sonnet", "haiku")

CLI_TIMEOUT_SECONDS = 15
CODEX_DESCRIBE_SECONDS = 90
# How long a status is kept: a short one, and a full one (Codex's account
# and models, which start the app-server twice).
KEEP_SECONDS = 60
KEEP_FULL_SECONDS = 300
KEEP_FAILED_SECONDS = 20

_kept: dict[tuple, tuple[float, dict]] = {}


def account_dir(provider: str) -> Path:
    return AUTH_DIR / ("codex" if provider == "codex" else "claude")


def ensure_account_dir(provider: str) -> Path:
    """data/auth/<provider>, made (with data/auth) owner-only."""
    path = account_dir(provider)
    for folder in (AUTH_DIR, path):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
    return path


def claude_env(auth: str) -> dict:
    """What ``auth`` adds to a Claude child's environment (after
    claude_runtime.runtime_env has dropped CREDENTIAL_ENV): nothing for the
    terminal login, CLAUDE_CONFIG_DIR for a linked account."""
    if auth == ACCOUNT:
        return {"CLAUDE_CONFIG_DIR": str(ensure_account_dir("claude"))}
    return {}


def codex_home(auth: str) -> Path | None:
    """Where Codex's login is taken from: Harvey's folder for a linked
    account, else None (codex.native_codex_home(): ~/.codex or CODEX_HOME)."""
    if auth == ACCOUNT:
        return ensure_account_dir("codex")
    return None


def auth_of(brain) -> str:
    return getattr(brain, "auth", "") or TERMINAL


def _clean(value, limit: int = 40) -> str:
    """A field from a CLI's answer, made safe to show and log."""
    return re.sub(r"[^A-Za-z0-9._:+ -]", "?", str(value or ""))[:limit]


def last_error(raw: str, provider: str) -> str:
    """The class (codex.CodexError's kind, ``setup`` for Claude) of the
    pause the brain last recorded for ``provider``, or "" when it isn't
    paused."""
    from harvey.brain import active_brain_status

    stored = active_brain_status(raw, provider)
    return _clean(stored.get("kind") or "setup") if stored.get("status") == "paused" else ""


# ── Claude ──

def parse_claude_auth(text: str) -> dict:
    """``claude auth status``'s JSON as {logged_in, method, plan, api}:
    loggedIn, authMethod, subscriptionType and apiProvider only (its email,
    organisation and folders are never read out)."""
    start = (text or "").find("{")
    try:
        data = json.loads(text[start:]) if start >= 0 else {}
    except ValueError:
        try:
            data = json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {"logged_in": data.get("loggedIn") is True,
            "method": _clean(data.get("authMethod")),
            "plan": _clean(data.get("subscriptionType")),
            "api": _clean(data.get("apiProvider"))}


def parse_version(text: str) -> str:
    m = re.search(r"\b(\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?)\b", text or "")
    return m.group(1) if m else ""


def _run_claude(exe: str, args: list[str], auth: str) -> subprocess.CompletedProcess:
    from harvey import claude_runtime

    with claude_runtime.isolated_cwd() as cwd:
        return subprocess.run(
            [exe, *args], cwd=cwd, env=claude_runtime.runtime_env(cwd, auth=auth),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CLI_TIMEOUT_SECONDS)


def claude_status(auth: str) -> dict:
    """The Claude part of status(): the CLI's isolation check and version,
    then ``claude auth status`` with the environment a call gets."""
    from harvey import claude_runtime

    out = {"cli": {"ok": False, "version": "", "problem": ""}, "linked": None,
           "linked_as": {}, "problem": ""}
    try:
        exe = claude_runtime.check_cli()
    except claude_runtime.ClaudeRuntimeError as e:
        out["cli"]["problem"] = out["problem"] = str(e)
        return out
    out["cli"]["ok"] = True
    try:
        out["cli"]["version"] = parse_version(_run_claude(exe, ["--version"], auth).stdout)
        res = _run_claude(exe, ["auth", "status"], auth)
    except (OSError, subprocess.SubprocessError) as e:
        out["problem"] = f"'claude auth status' didn't answer ({type(e).__name__})."
        return out
    found = parse_claude_auth(res.stdout)
    out["linked"] = found["logged_in"]
    if found["logged_in"]:
        out["linked_as"] = {k: found[k] for k in ("method", "plan", "api") if found[k]}
    elif auth == ACCOUNT:
        out["problem"] = f"Not linked yet: no login in {_shown(account_dir('claude'))}."
    else:
        out["problem"] = "Claude isn't logged in: run 'claude login' in a terminal."
    return out


# ── Codex ──

def codex_status(brain, full: bool) -> dict:
    """The Codex part of status(): the platform, the CLI's version and
    whether there is a login (``full``: the app-server's account type and
    plan, and the models Harvey can run, codex.CodexClient.describe)."""
    from harvey import codex

    auth = auth_of(brain)
    out = {"cli": {"ok": False, "version": "", "problem": ""}, "linked": None,
           "linked_as": {}, "problem": "", "models": []}
    try:
        codex.require_supported_platform()
        out["cli"]["version"] = codex.check_cli_version(timeout=CLI_TIMEOUT_SECONDS)
        out["cli"]["ok"] = True
    except codex.CodexError as e:
        out["cli"]["problem"] = out["problem"] = str(e)
        return out
    home = codex_home(auth)
    if home is not None and not (home / "auth.json").exists():
        out["linked"] = False
        out["problem"] = f"Not linked yet: no login in {_shown(home)}."
        return out
    if not full:
        if home is None:
            try:
                codex.check_native_auth()
            except codex.CodexError as e:
                out["linked"] = False
                out["problem"] = str(e)
                return out
        return out
    client = codex.CodexClient(model=getattr(brain, "model", "") or "", native_home=home)
    try:
        found = asyncio.run(client.describe(timeout=CODEX_DESCRIBE_SECONDS))
    except codex.CodexError as e:
        out["linked"] = False if e.kind in ("auth", "setup") else None
        out["problem"] = str(e)
        return out
    account = found.get("account") or {}
    out["linked"] = account.get("type") == "chatgpt"
    out["linked_as"] = {k: v for k, v in (("method", account.get("type")),
                                           ("plan", account.get("plan"))) if v}
    if account.get("type") and account.get("type") != "chatgpt":
        out["problem"] = ("Codex is logged in with an API key or another provider: Harvey "
                          "uses a ChatGPT plan only. Run 'codex login' with ChatGPT.")
    elif not account.get("type"):
        out["problem"] = "Codex isn't logged in: run 'codex login' with ChatGPT."
    out["models"] = found.get("models") or []
    return out


async def codex_models(brain, timeout: float = CODEX_DESCRIBE_SECONDS) -> list[dict]:
    """The Codex models Harvey can run on this account, live (the panel's,
    setup's and training's list): [{id, name, efforts}]. Raises
    codex.CodexError."""
    from harvey import codex

    client = codex.CodexClient(model="", native_home=codex_home(auth_of(brain)))
    return (await client.describe(timeout=timeout)).get("models") or []


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


# ── The status ──

async def status(brain, *, state=None, full: bool = False, force: bool = False) -> dict:
    """Settings → Model's status line (and setup-status's brain entry):
    {provider, model, reasoning_effort, auth, offered, folder, cli {ok,
    version, problem}, linked (True, False, None: unknown), linked_as
    {method, plan[, api]}, problem, last_error, models, codex_refused,
    checked_at}. Kept KEEP_SECONDS (``full``: KEEP_FULL_SECONDS), a failed
    one KEEP_FAILED_SECONDS; ``force`` asks again."""
    from harvey.config import BrainConfig

    brain = brain or BrainConfig()
    provider, auth = brain.provider, auth_of(brain)
    key = (provider, brain.model, auth, full)
    now = time.monotonic()
    kept = _kept.get(key)
    if kept and not force and now < kept[0]:
        found = dict(kept[1])
    else:
        if provider == "codex":
            found = await asyncio.to_thread(codex_status, brain, full)
        else:
            found = await asyncio.to_thread(claude_status, auth)
            found["models"] = [{"id": m, "name": m, "efforts": []} for m in CLAUDE_MODELS]
        found["checked_at"] = int(time.time())
        ttl = (KEEP_FULL_SECONDS if full else KEEP_SECONDS) if found.get("linked") \
            else KEEP_FAILED_SECONDS
        _kept[key] = (now + ttl, dict(found))
    raw = ""
    if state is not None:
        from harvey.brain import BRAIN_STATUS_KEY
        try:
            raw = await state.get_setting(BRAIN_STATUS_KEY) or ""
        except Exception:
            raw = ""
    return {"provider": provider, "model": brain.model,
            "reasoning_effort": brain.reasoning_effort, "auth": auth,
            "offered": list(OFFERED),
            "folder": _shown(account_dir(provider)) if auth == ACCOUNT else "",
            "codex_refused": codex_refusal(), "last_error": last_error(raw, provider),
            **found}


def codex_refusal() -> str:
    """Why Codex can't be chosen on this platform (Windows), or ""."""
    from harvey import codex

    try:
        codex.require_supported_platform()
    except codex.CodexError as e:
        return str(e)
    return ""


def forget() -> None:
    """Drop every kept status (after a change of settings)."""
    _kept.clear()
