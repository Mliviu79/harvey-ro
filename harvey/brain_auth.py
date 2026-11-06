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
- ``api_key``: a key billed per token, saved once from Settings → Model
  after a free check (check_key: the provider's model list, no model
  call). Claude's is kept in .env under CLAUDE_KEY_ENV, a name only Harvey
  reads, and passed to a Claude child as ANTHROPIC_API_KEY in this mode
  only, with CLAUDE_CONFIG_DIR a folder of Harvey's own (data/auth/
  claude-key), so no other login there can be used instead. Codex's is
  given to Codex itself (``account/login/start``, codex.login_api_key) in
  data/auth/codex, where Codex keeps it in its own file: Harvey keeps no
  copy. The calls' cost is held by brain.usd_per_day / usd_per_month
  (harvey/brain_spend.py). claude_env() is the one place a mode adds to a
  Claude child's environment.

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
# Harvey's .env, where the Claude key is kept (read afresh, so a key
# removed from Settings stops being used without a restart).
ENV_FILE = PROJECT_ROOT / ".env"
# The ways to link the panel offers: the account link's flow isn't in this
# release (its button stays hidden; the data model already knows it).
OFFERED = (TERMINAL, API_KEY)
# The .env name of the Claude API key Settings → Model saves: no CLI reads
# it (claude_runtime.CREDENTIAL_ENV drops it from every child), so a key
# saved here never moves a terminal or account login to per-token billing.
CLAUDE_KEY_ENV = "HARVEY_BRAIN_ANTHROPIC_KEY"
# The free checks of a key: the provider's list of models (no model call).
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
KEY_CHECK_SECONDS = 15
KEY_MAX_CHARS = 512

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


def key_dir() -> Path:
    """Claude's config folder in API-key mode (no login of any kind in it)."""
    return AUTH_DIR / "claude-key"


def ensure_account_dir(provider: str) -> Path:
    """data/auth/<provider>, made (with data/auth) owner-only."""
    return _owner_only(account_dir(provider))


def _owner_only(path: Path) -> Path:
    for folder in (AUTH_DIR, path):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
    return path


def claude_key() -> str:
    """The Claude API key Settings → Model saved ("" when none): read from
    .env each time (the environment only when there is no .env). Never
    logged, shown or returned by any endpoint."""
    try:
        if ENV_FILE.is_file():
            from dotenv import dotenv_values

            return (dotenv_values(ENV_FILE).get(CLAUDE_KEY_ENV) or "").strip()
    except Exception:
        return ""
    return (os.environ.get(CLAUDE_KEY_ENV) or "").strip()


def claude_env(auth: str) -> dict:
    """What ``auth`` adds to a Claude child's environment (after
    claude_runtime.runtime_env has dropped CREDENTIAL_ENV): nothing for the
    terminal login, CLAUDE_CONFIG_DIR for a linked account, and for an API
    key the saved key as ANTHROPIC_API_KEY with a config folder of its own
    (no key saved: the folder alone, so the call fails as not logged in
    rather than using another login)."""
    if auth == ACCOUNT:
        return {"CLAUDE_CONFIG_DIR": str(ensure_account_dir("claude"))}
    if auth == API_KEY:
        env = {"CLAUDE_CONFIG_DIR": str(_owner_only(key_dir()))}
        key = claude_key()
        if key:
            env["ANTHROPIC_API_KEY"] = key
        return env
    return {}


def codex_home(auth: str) -> Path | None:
    """Where Codex's login is taken from: Harvey's folder for a linked
    account or an API key, else None (codex.native_codex_home(): ~/.codex
    or CODEX_HOME)."""
    if auth in (ACCOUNT, API_KEY):
        return ensure_account_dir("codex")
    return None


def key_set(provider: str) -> bool:
    """Whether an API key is saved for ``provider`` (Claude's in .env,
    Codex's in its own file in data/auth/codex)."""
    if provider == "codex":
        from harvey import codex

        return codex.has_api_key_login(account_dir("codex"))
    return bool(claude_key())


async def check_key(provider: str, key: str, *, transport=None) -> str:
    """"" when ``key`` is accepted by its provider, else why not, checked
    with a free call: the list of models (Anthropic's for Claude, OpenAI's
    for Codex), never a model call. The key is never part of the answer."""
    import httpx

    key = (key or "").strip()
    if not key or len(key) > KEY_MAX_CHARS or any(c.isspace() for c in key):
        return "Paste the whole API key (one line, no spaces)."
    if provider == "codex":
        url, host = OPENAI_MODELS_URL, "api.openai.com"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url, headers, host = ANTHROPIC_MODELS_URL, {
            "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, "api.anthropic.com"
    try:
        async with httpx.AsyncClient(timeout=KEY_CHECK_SECONDS, follow_redirects=False,
                                     transport=transport) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return f"Couldn't reach {host} to check the key ({type(e).__name__}): try again."
    if res.status_code == 200:
        return ""
    if res.status_code in (401, 403):
        return f"{host} refused this key (HTTP {res.status_code}): check it and paste it again."
    return f"Couldn't check the key: {host} answered HTTP {res.status_code}. Try again."


async def link_codex_key(key: str) -> None:
    """Give Codex the API key: its app-server's own login into data/auth/
    codex (codex.login_api_key), which Codex keeps in its own file there.
    Raises codex.CodexError."""
    from harvey import codex

    await codex.login_api_key(ensure_account_dir("codex"), key)


def unlink_codex_key() -> bool:
    """Remove the API key Codex keeps in data/auth/codex (a ChatGPT login
    there is left alone). True when one was removed."""
    from harvey import codex

    home = account_dir("codex")
    if not codex.has_api_key_login(home):
        return False
    try:
        (home / "auth.json").unlink()
    except FileNotFoundError:
        return False
    return True


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
    if auth == API_KEY:
        # (The key is Harvey's to know about: the CLI isn't asked whether
        # it is logged in, only whether it runs.)
        try:
            out["cli"]["version"] = parse_version(_run_claude(exe, ["--version"], auth).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        out["linked"] = key_set("claude")
        if out["linked"]:
            out["linked_as"] = {"method": API_KEY}
        else:
            out["problem"] = "No API key saved: paste one in Settings → Model."
        return out
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
    api = auth == API_KEY
    if api and not key_set("codex"):
        out["linked"] = False
        out["problem"] = "No API key saved: paste one in Settings → Model."
        return out
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
    client = codex.CodexClient(model=getattr(brain, "model", "") or "", native_home=home,
                               api_key=api)
    try:
        found = asyncio.run(client.describe(timeout=CODEX_DESCRIBE_SECONDS))
    except codex.CodexError as e:
        out["linked"] = False if e.kind in ("auth", "setup") else None
        out["problem"] = str(e)
        return out
    account = found.get("account") or {}
    out["linked"] = account.get("type") == ("apiKey" if api else "chatgpt")
    out["linked_as"] = {k: v for k, v in (("method", account.get("type")),
                                           ("plan", account.get("plan"))) if v}
    if api:
        if not out["linked"]:
            out["problem"] = "Codex doesn't report the API key: save it again in Settings → Model."
    elif account.get("type") and account.get("type") != "chatgpt":
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

    auth = auth_of(brain)
    client = codex.CodexClient(model="", native_home=codex_home(auth), api_key=auth == API_KEY)
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
    checked_at, key_set {claude, codex} (whether a key is saved, never the
    key), the brain's USD caps and Codex's prices, and in API-key mode
    ``spend`` (brain_spend.totals)}. Kept KEEP_SECONDS (``full``: KEEP_FULL_SECONDS), a failed
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
    spend = None
    if auth == API_KEY and state is not None:
        from harvey import brain_spend
        try:
            spend = await brain_spend.totals(state, brain)
        except Exception:
            spend = None
    return {"provider": provider, "model": brain.model,
            "reasoning_effort": brain.reasoning_effort, "auth": auth,
            "offered": list(OFFERED), "spend": spend,
            "key_set": {p: key_set(p) for p in ("claude", "codex")},
            "usd_per_day": brain.usd_per_day, "usd_per_month": brain.usd_per_month,
            "codex_usd_per_mtok_in": brain.codex_usd_per_mtok_in,
            "codex_usd_per_mtok_out": brain.codex_usd_per_mtok_out,
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
