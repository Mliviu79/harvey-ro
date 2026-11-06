"""Harvey Dashboard — local web UI to set up, control, and monitor Harvey."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse,
)

logger = logging.getLogger("harvey.dashboard")

from harvey import dashboard_auth as auth  # noqa: E402
from harvey.paths import PROJECT_ROOT  # noqa: E402
from harvey.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from harvey.textio import read_user_text  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"


def _resolve_config_file() -> Path:
    """harvey.local.yaml wins when present — same resolution as config.py's
    loader, so the dashboard doesn't judge setup complete against the
    tracked harvey.yaml template while the real trained config sits in the
    gitignored local override."""
    from harvey.config import _find_config_file, ConfigFileNotFoundError
    try:
        return Path(_find_config_file())
    except ConfigFileNotFoundError:
        return CONFIG_FILE

# No /docs, /redoc or /openapi.json: the API is for this UI only, and every
# route except the static shell needs the token (see dashboard_auth).
@asynccontextmanager
async def _lifespan(_app):
    # Bring in what modules declare before the first request. A missing
    # database stays missing until something writes; an older schema waits
    # for the upgrade (the pages show its screen until then).
    if DB_PATH.exists():
        from harvey.state import NewerBuild, StateManager, UpgradeNeeded
        try:
            await StateManager(str(DB_PATH)).init_db()
        except NewerBuild as e:
            logger.error("%s", e)
        except UpgradeNeeded:
            logger.warning("%s waits for the upgrade: the dashboard shows the upgrade screen",
                           DB_PATH)
        except Exception:
            logger.exception("could not open %s", DB_PATH)
    await asyncio.to_thread(_warm_up)
    # The job worker runs inside the dashboard: the usual install runs only
    # this process. It waits for the database to exist, and stands aside
    # while another worker (harvey worker, a command) holds the lease.
    stop = asyncio.Event()
    task = None
    if os.environ.get("HARVEY_DASHBOARD_WORKER", "on").strip().lower() not in ("off", "0", "no"):
        task = asyncio.create_task(_run_worker(stop))
    try:
        yield
    finally:
        stop.set()
        await _close_chats()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30)
            except Exception:
                logger.exception("the job worker didn't stop cleanly")


def _warm_up() -> None:
    """Load before the first request what every page's first request would
    load otherwise (a few tenths of a second): the modules' manifests and
    register adapters, the CAEN reference, the settings file."""
    try:
        from harvey import registers
        from harvey.overlay import reads

        registers.adapters(DB_PATH.parent)
        reads._caen_names()
        _load_config_quietly()
    except Exception:
        logger.debug("warm-up skipped", exc_info=True)


async def _run_worker(stop: asyncio.Event):
    from harvey.modules.worker import Worker

    while not stop.is_set():
        if DB_PATH.exists():
            try:
                state = _state()
                # (Waits while the database waits for the upgrade.)
                if not await state.wait_until_upgraded(stop):
                    break
                await state.init_db()
                await Worker(state).run(stop)
            except Exception:
                logger.exception("the job worker stopped; starting it again in a minute")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


app = FastAPI(title="Harvey Dashboard", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)
token_store = auth.TokenStore(TOKEN_FILE)


class _UpgradeGate:
    """While harvey.db waits for the upgrade, every API call but the
    upgrade's own answers 409: no page reads tables the old schema doesn't
    have, and the dashboard shows the upgrade screen instead."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith("/api/") and not path.startswith("/api/upgrade") \
                and await asyncio.to_thread(_upgrade_needed):
            response = JSONResponse({"upgrade_required": True, "message": (
                "harvey.db waits for the upgrade to the slim database: open the dashboard "
                "or run harvey upgrade")}, status_code=409)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _upgrade_needed() -> bool:
    from harvey import upgrade

    return upgrade.waiting(DB_PATH)


# (The guard is outermost: a request without the token never learns more.)
app.add_middleware(_UpgradeGate)
app.add_middleware(auth.DashboardGuard, store=token_store)

# Harvey process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in read_user_text(ENV_FILE).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _error_body(error: Exception, key: str = "error", **extra) -> dict:
    """A refusal's JSON body. A module-off refusal also carries the
    module's id (``off_module``), so the page says it in the viewer's
    language; the English text stays for the API and the CLI."""
    from harvey.modules.switches import ModuleOff

    body = {**extra, key: str(error)}
    if isinstance(error, ModuleOff):
        body["off_module"] = error.module_id
    return body


def _env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.partition("=")[0].strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    return key


def _write_env_file(updates: dict[str, str]):
    """Set keys in .env, keeping every other line (comments, order, other
    keys) as it was.

    Written to a private temporary file in the same directory and swapped
    in atomically, so a crash never leaves a half-written .env and the
    credentials are never readable by other users, even for a moment: the
    result is owner-only (0600 on POSIX) whether .env existed or not. A
    symlinked .env is refused rather than replaced or followed.
    """
    if ENV_FILE.is_symlink():
        raise OSError(f"{ENV_FILE} is a symbolic link; edit it by hand.")
    if ENV_FILE.exists() and not ENV_FILE.is_file():
        raise OSError(f"{ENV_FILE} is not a regular file.")
    lines = (read_user_text(ENV_FILE).splitlines()
             if ENV_FILE.exists() else [])

    out, written = [], set()
    for line in lines:
        key = _env_key(line)
        if key in updates:
            if key in written:
                continue  # a later duplicate would override the new value
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in written]

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(ENV_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Harvey process from a PID file."""
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # pid_alive, never os.kill(pid, 0): on Windows that terminates the loop.
        # And only while a loop holds the lock: after a crash or a reboot the
        # PID can belong to an unrelated program, which Stop must not signal.
        if pid is not None and pid_alive(pid) and loop_lock_held(LOCK_FILE):
            return pid
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return None


# ── Setup Status ──


# Where a container runtime leaves its mark; module-level so tests can point
# it somewhere else.
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def _python_environment() -> str:
    """How Harvey's Python dependencies are provided: "venv" (a .venv in the
    checkout), "docker" (the image carries them), "installed" (pip-installed
    into the running Python), or "" when none of those holds."""
    if (PROJECT_ROOT / ".venv").is_dir():
        return "venv"
    if any(marker.exists() for marker in CONTAINER_MARKERS):
        return "docker"
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        try:
            distribution("harvey")
            return "installed"
        except PackageNotFoundError:
            return ""
    except Exception:
        return ""


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []

    # 1. Python environment: a .venv in the checkout, or none needed at all
    # (the Docker image, or Harvey installed into the running Python).
    env_kind = _python_environment()
    checks.append({
        "id": "venv",
        "label": {"docker": "Python environment (Docker image)",
                  "installed": "Python environment (Harvey installed)"}.get(
                      env_kind, "Python virtual environment"),
        "done": bool(env_kind),
        "env": env_kind,      # stable, for the dashboard's own wording
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
        "scope": "finding",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = ENV_FILE.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
        "scope": "finding",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and \
            (PROJECT_ROOT / "data" / "gmail_token.json").is_file()
        provider_help = ("Set GMAIL_CLIENT_ID/SECRET in Settings, then run "
                         "'harvey gmail auth' in your terminal.")
    elif provider == "microsoft":
        provider_done = _has("M365_CLIENT_ID") and \
            (PROJECT_ROOT / "data" / "m365_token.json").is_file()
        provider_help = ("Set the Microsoft 365 client and tenant IDs in Settings, then run "
                         "'harvey microsoft auth' in your terminal.")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        provider_help = "Enter your SMTP host, username, and password in Settings."
    else:
        # Instantly (removed) or a name Harvey doesn't know: nothing is sent
        # or read until another provider is chosen.
        from harvey.config import provider_notice

        provider_done = False
        provider_help = provider_notice(_load_config_quietly()) or (
            "Set channels.email.provider to gmail, smtp or microsoft.")
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "provider": provider,
        "done": provider_done,
        "required": True,
        "help": provider_help,
        "scope": "email",
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = _has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings — "
                "without one, found emails stay 'guess' and are never sent.",
        "scope": "finding",
    })

    # 5. Config valid
    config_valid = False
    config_path = _resolve_config_file()
    if config_path.exists():
        try:
            cfg = yaml.safe_load(read_user_text(config_path))
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": f"Harvey configured ({config_path.name})",
        "file": config_path.name,
        "done": config_valid,
        "required": True,
        "help": "Train Harvey on your product. Use the trainer or set up manually through Claude.",
        "scope": "email",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: harvey train https://yourwebsite.com (or set up through Claude).",
        "scope": "email",
    })

    # 7. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
        "scope": "email",
    })

    # 8. Warmup service: TrulyInbox connected, or another service the user named
    from harvey.integrations import trulyinbox as ti
    try:
        state = _state()
        await state.init_db()
        choice = await ti.get_choice(state)
    except Exception:
        choice = ""
    if _has("TRULYINBOX_API_KEY"):
        choice = "connected"
    checks.append({
        "id": "warmup_service",
        "label": {"connected": "Warmup: TrulyInbox connected",
                  "other": "Warmup: your own warmup service"}.get(choice, "Warm up your mailbox"),
        "done": bool(choice),
        "choice": choice or "",
        "required": True,
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Harvey shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
        "scope": "email",
    })

    # 9. The model: provider, model, login (brain_auth.status, kept briefly;
    # Codex's account and models only on Settings → Model).
    from harvey import brain_auth
    brain_cfg, brain_error = _brain_config_or_error()
    if brain_cfg is None:
        checks.append({"id": "brain", "label": "Model", "done": False, "required": True,
                       "help": brain_error, "scope": "finding"})
    else:
        try:
            state = _state()
            await state.init_db()
            brain = await brain_auth.status(brain_cfg, state=state)
        except Exception as e:
            brain = {"provider": brain_cfg.provider, "model": brain_cfg.model,
                     "auth": brain_cfg.auth, "linked": None,
                     "problem": f"Couldn't check the model ({type(e).__name__})."}
        name = "Codex" if brain["provider"] == "codex" else "Claude"
        checks.append({
            "id": "brain",
            "label": f"Model: {name}" + (f" ({brain['model']})" if brain.get("model") else ""),
            "provider": brain["provider"], "model": brain.get("model", ""),
            "auth": brain.get("auth", "terminal"), "linked": brain.get("linked"),
            "linked_as": brain.get("linked_as") or {}, "cli": brain.get("cli") or {},
            "last_error": brain.get("last_error", ""),
            "done": brain.get("linked") is not False and bool((brain.get("cli") or {}).get("ok")),
            "required": True,
            "help": brain.get("problem") or "Settings → Model shows how Harvey thinks.",
            "scope": "finding",
        })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


def _current_provider() -> str:
    """Read channels.email.provider from the active config (best-effort)."""
    try:
        cfg = yaml.safe_load(read_user_text(_resolve_config_file())) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "gmail")
    except Exception:
        return "gmail"


# Keys a module reads that Settings never writes: TrulyInbox's are saved by
# its own connect flow, which checks the key against the mailbox first, and
# the brain's Claude API key by Settings → Model's (/api/brain/key).
ENV_OWN_FLOW = ("TRULYINBOX_API_KEY", "TRULYINBOX_WORKSPACE_ID", "HARVEY_BRAIN_ANTHROPIC_KEY")


def _present_env_keys() -> set[str]:
    """Names of the keys that hold a value, in .env or the environment."""
    present = {k for k, v in _read_env_file().items() if (v or "").strip()}
    return present | {k for k, v in os.environ.items() if (v or "").strip()}


async def _env_allowlist() -> list[str]:
    """The .env keys Settings shows and saves: those the enabled modules'
    manifests list, plus any module's key that already holds a value. A
    key that is set can always be changed or cleared, so a password still
    in use (the heartbeat sends whatever the Email switch says) can be
    rotated with its module off."""
    from harvey.modules import switches

    state = _state()
    await state.init_db()
    keys = switches.env_keys(await switches.enabled_ids(state))
    present = _present_env_keys()
    keys += [k for k in switches.env_keys({m.id for m in switches.manifests()})
             if k in present and k not in keys]
    return [k for k in keys if k not in ENV_OWN_FLOW]


@app.get("/api/settings")
async def get_settings():
    """Get current settings — presence flags only for secrets, never raw
    values, and only for the keys of modules that are on."""
    allowed = await _env_allowlist()
    env_vars = {k: v for k, v in _read_env_file().items() if k in allowed}
    for key in allowed:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    # Gmail is authorized once harvey gmail auth has stored a token file.
    gmail_token = (PROJECT_ROOT / "data" / "gmail_token.json").is_file()

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": gmail_token,
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "imap_username": env_vars.get("IMAP_USERNAME", ""),
        "imap_password_set": is_set("IMAP_PASSWORD"),
        # Microsoft 365: ids aren't secret (no client secret: Harvey signs
        # in as a public client with PKCE); the token file means signed in.
        "m365_client_id": env_vars.get("M365_CLIENT_ID", ""),
        "m365_tenant_id": env_vars.get("M365_TENANT_ID", ""),
        "m365_authorized": (PROJECT_ROOT / "data" / "m365_token.json").is_file(),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
        "env_keys": allowed,
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    from harvey.modules import switches

    allowed = await _env_allowlist()
    # A key of a module that is off is refused by name, not dropped.
    for key in data:
        owners = switches.env_key_owners(key) if key not in allowed else []
        if owners and key not in ENV_OWN_FLOW:
            return JSONResponse(_error_body(switches.ModuleOff(owners[0], f"Saving {key}"),
                                            "message", success=False), status_code=409)
    async with _env_lock:
        updates = {}
        for key in allowed:
            if key in data and data[key] is not None:
                value = data[key]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return JSONResponse({"success": False, "message": f"{key} must be text."},
                                        status_code=400)
                value = str(value)
                # A port Harvey can't parse would stop it starting at all.
                if key.endswith("_PORT") and value.strip() and not value.strip().isdigit():
                    return JSONResponse({"success": False, "message": f"{key} must be a number."},
                                        status_code=400)
                if len(value) > 4096 or "\x00" in value:
                    return JSONResponse({"success": False, "message": f"{key} is invalid."},
                                        status_code=400)
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = value.replace("\n", " ").replace("\r", " ").strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


@app.post("/api/trulyinbox/connect")
async def trulyinbox_connect(request: Request):
    """Check a TrulyInbox key against the sending mailbox, then save it to .env."""
    from harvey import deliverability as d
    from harvey.integrations import trulyinbox as ti
    try:
        data = await request.json()
    except Exception:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        return JSONResponse({"success": False, "message": "Paste your TrulyInbox API key."},
                            status_code=400)
    key = key.strip()
    try:
        from harvey.config import load_config
        config = load_config()
        address, warmup = ti.sending_address(config), config.channels.email.warmup
    except Exception:
        address, warmup = "", None
    if not address:
        return JSONResponse({"success": False, "message": ti.NO_ADDRESS}, status_code=400)
    workspace = (_read_env_file().get("TRULYINBOX_WORKSPACE_ID", "")
                 or os.getenv("TRULYINBOX_WORKSPACE_ID", "")).strip()
    ti.forget_rejected(key)
    try:
        client = ti.TrulyInbox(key, workspace)
        health = await client.mailbox_health(address)
    except ti.TrulyInboxError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    updates = {"TRULYINBOX_API_KEY": key}
    if not workspace:
        updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
    async with _env_lock:
        try:
            _write_env_file(updates)
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"success": False, "message": "Could not write .env file."}
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "connected")
    return {"success": True, "message": d.check_warmup_service(health, warmup).detail}


@app.post("/api/trulyinbox/other")
async def trulyinbox_other():
    """The user warms their mailbox with another service: stop recommending one."""
    from harvey.integrations import trulyinbox as ti
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "other")
    return {"success": True}


# ── Companies ──


COMPANY_PAGE_MAX = 200


def _page_args(limit, offset, default: int = 50) -> tuple[int, int]:
    try:
        limit = int(limit) if limit not in (None, "") else default
        offset = int(offset) if offset not in (None, "") else 0
    except (TypeError, ValueError):
        limit, offset = default, 0
    return max(1, min(limit, COMPANY_PAGE_MAX)), max(0, offset)


def _load_config_quietly():
    try:
        from harvey.config import load_config
        return load_config(str(_resolve_config_file()))
    except Exception:
        return None


def _split_person(text: str) -> dict:
    """A CONTACT_FOUND value ("Ana Pop — administrator") as name and title."""
    name, sep, title = (text or "").partition(" — ")
    return {"name": name.strip(), "title": title.strip() if sep else ""}


def _blocked_entries(config, strict: bool = False) -> list[str]:
    """compliance.do_not_contact, as the exports read it. Unreadable: logged
    and treated as empty here, or with ``strict`` (an export) the
    ConfigError is raised: an export never goes out without the list."""
    from harvey.export import _do_not_contact
    try:
        return _do_not_contact(config)
    except Exception as e:
        if strict:
            raise
        logger.warning("do_not_contact couldn't be read: %s", e)
        return []


async def _enrich_companies(rows: list[dict], config=None) -> list[dict]:
    """Add what the phone/email-first views show: the phones (main one in
    international format, its type, the others the site published), the
    email addresses on file (most deliverable first, bounced ones left out),
    the activities in words, and how many contacts and named people each has.
    Additive: every key the row already had stays."""
    if not rows:
        return rows
    # The same suppression as the exports: an address (or a whole domain) on
    # compliance.do_not_contact is never offered here either.
    blocked = _blocked_entries(config)
    # The rows (overlay.reads.company_rows) carry what they need.
    inputs = {r["id"]: r.pop("_inputs") for r in rows}
    # An address the do-not-contact list bars is never offered either.
    suppressed = await _state().suppressed_emails(
        [p.get("email") for v in inputs.values() for p in v["prospects"]])
    for v in inputs.values():
        v["prospects"] = [p for p in v["prospects"] if p.get("email") not in suppressed]
    return _enriched(rows, config, blocked,
                     {i: v["phones"] for i, v in inputs.items()},
                     {i: v["people"] for i, v in inputs.items()},
                     {i: v["prospects"] for i, v in inputs.items()}, {})


def _enriched(rows, config, blocked, site_phones, people, contacts, activities) -> list[dict]:
    """_enrich_companies' columns from what it read."""
    from harvey.collectors.phones import phone_region
    from harvey.export import _EMAIL_STATUS_RANK, _phone_columns
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    for row in rows:
        cid = row.get("id")
        mine = contacts.get(cid, [])
        if "activities" not in row:
            row["activities"] = activities.get(cid, [])
        row["contact_count"] = len(mine)
        row["people"] = people.get(cid, [])
        emails, seen = [], set()
        company_blocked = bool(blocked) and do_not_contact_domain_match(row.get("domain") or "", blocked)
        for p in sorted(mine, key=lambda p: _EMAIL_STATUS_RANK.get(p.get("email_status") or "", 3)):
            email = (p.get("email") or "").strip()
            if (not email or email in seen or p.get("email_status") == "invalid"
                    or p.get("status") == "opted_out" or company_blocked
                    or (blocked and do_not_contact_match(email, blocked))):
                continue
            seen.add(email)
            emails.append({"email": email, "status": p.get("email_status") or "",
                           "name": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
                           "title": p.get("title") or ""})
        row["emails"] = emails
        raw = (row.get("phone") or next((p["phone"] for p in mine if p.get("phone")), "") or "").strip()
        try:
            region = phone_region(row.get("location") or "", config, row.get("domain") or "")
            main, kind, others = _phone_columns(raw, region, site_phones.get(cid, []))
        except Exception:
            main, kind, others = raw, "", ""
        row["phone_display"] = main
        row["phone_type"] = kind
        row["other_phones"] = [x for x in others.split("; ") if x]
    return rows


# Companies still standing on their own: a merged one's facts are its survivor's.
_LIVE = "COALESCE(merged_into, '') = ''"

@app.get("/api/companies")
async def get_companies(request: Request, limit: int | None = None, offset: int | None = None,
                        sort: str = "", dir: str = ""):
    """Companies with contact counts. Without any parameter it is the
    original list (newest 200). Otherwise one page as {total, offset,
    limit, sort, dir, companies, sources, financials}: sorted by ``sort``
    (created, name, place, turnover, employees, contacts, searched;
    ``relevance`` with a search), filtered by the Contacts page's company
    filters (county, locality, website, require/exclude, min/max_turnover,
    min/max_employees, unknown) plus caen (codes or words), contact (any,
    phone, mobile, email, none, or a kind), found_in (a source) and search
    (``q`` too: the full-text search, or a CUI). Each row is enriched for
    the phone/email-first table (see _enrich_companies) and carries its
    latest turnover and headcount, its contact count and last search.
    Paging: harvey/companies.py."""
    if not DB_PATH.exists():
        if not request.query_params:
            return []
        limit, offset = _page_args(limit, offset)
        return {"total": 0, "offset": offset, "limit": limit, "sort": sort, "dir": dir,
                "companies": [], "sources": [], "financials": False}
    return await _overlay_companies(request, limit, offset, sort, dir)


async def _overlay_companies(request: Request, limit, offset, sort: str, dir: str):
    """get_companies on the overlay (overlay.reads: a page by index, or a
    kept ordering of the matching firms)."""
    from harvey import companies

    state = _state()
    await state.init_db()
    plain = not request.query_params
    limit, offset = _page_args(limit, offset, 200 if plain else 50)
    args = dict(request.query_params)
    if args.get("q") and not args.get("search"):
        args["search"] = args["q"]
    args.pop("q", None)
    empty = {"total": 0, "offset": offset, "limit": limit, "sort": sort, "dir": dir,
             "companies": [], "sources": [], "financials": False}
    try:
        f, _ = await _contact_filter(state, args)
    except ValueError as e:
        return JSONResponse({**empty, "error": str(e)}, status_code=400)
    has_fin = bool(await state.read("reads_figures"))
    relevance = sort in ("", "relevance") and bool(f.words or f.search)
    if not relevance and sort not in companies.SORTS:
        sort = "created"
    if sort in companies.FIGURE_SORTS and not has_fin:
        sort = "created"
    if relevance:
        sort = "relevance"
    way = companies.direction(sort, dir)
    started = time.monotonic()
    total, rows, how, sources = await state.read("companies_listing", f, sort, way, limit, offset)
    took = round((time.monotonic() - started) * 1000)
    if plain:
        # The original list: the newest 200, each with its contact count.
        for r in rows:
            r.pop("_inputs", None)
            r["contact_count"] = r.get("contacts", 0)
        return rows
    rows = await _enrich_companies(rows, _load_config_quietly())
    return {**empty, "total": total, "sort": sort, "dir": way, "companies": rows,
            "sources": sources, "financials": has_fin, "paged_by": how, "ms": took}


from harvey.contact_query import CUI_QUERY as _CUI_QUERY  # noqa: E402


async def _company_search(state, q: str, limit: int, offset: int) -> tuple[int, list[dict]]:
    """The local search, plus a CUI typed on its own ("10004070", "RO
    10004070"): when a company has that tax id, it is the answer. A bare
    four-digit number stays a CAEN code unless written with RO."""
    m = _CUI_QUERY.fullmatch(q or "")
    cui = m.group(1) if m and (len(m.group(1)) != 4 or q.strip().upper().startswith("RO")) \
        else ""
    hits = await state.read("company_by_key", cui) if cui else []
    if hits:
        return len(hits), hits[offset:offset + limit]
    return await state.read("search_companies", q, limit, offset)


@app.get("/api/companies/search")
async def search_companies(q: str = "", limit: int = 50, offset: int = 0):
    """Companies on file matching every word of ``q`` (name, activity as a
    CAEN code or in words, town), best first, each with its activities in
    words. Local full-text search: no Claude, no network."""
    q = q[:200]
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    empty = {"query": q, "total": 0, "offset": offset, "limit": limit, "companies": []}
    if not q.strip() or not DB_PATH.exists():
        return empty
    try:
        state = _state()
        await state.init_db()
        total, rows = await _company_search(state, q, limit=limit, offset=offset)
    except Exception as e:
        logger.warning("company search failed: %s", e)
        return JSONResponse({**empty, "error": "search failed"}, status_code=500)
    for r in rows:
        r.pop("score", None)
    rows = await _enrich_companies(rows, _load_config_quietly())
    return {"query": q, "total": total, "offset": offset, "limit": limit,
            "companies": rows}


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    if not DB_PATH.exists():
        return []
    return await _state().read("company_prospects", company_id)


@app.post("/api/companies/{company_id}/remove")
async def remove_company(company_id: str):
    """Remove a company a register pull brought from Harvey (the register
    still has it, and a pull adds it again). One Harvey worked on stays,
    with only its pulls taken off ("removed" is "", "kept" says why).
    Refused (409) for a company in a merge, undone or not, or one outreach
    names; 404 for none."""
    from harvey.overlay.store import RemoveRefused

    state = _state()
    await state.init_db()
    try:
        return await state.remove_company(company_id)
    except RemoveRefused as e:
        return _refused(e)


@app.get("/api/companies/{company_id}")
async def get_company(company_id: str):
    """One company for the detail panel: its record, every phone and email
    on file, the named people, its activities in words, and ``findings``:
    every phone and address any source gave with who gave it and when,
    and the latest official facts (official sources first)."""
    if not DB_PATH.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _overlay_company(company_id)


async def _overlay_company(company_id: str):
    """get_company on the overlay: the register parts read by the firm's
    register keys, the rest from overlay.reads."""
    from harvey import companies

    state = _state()
    await state.init_db()
    found = await state.read("panel", company_id)
    if found is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    (row,) = await _enrich_companies([found["row"]], _load_config_quietly())
    row["findings"], row["reach"] = found["findings"], found["reach"]
    keys = found["keys"]

    def parts():
        cuis = keys["cuis"]
        return (companies.registry_part(cuis, keys["external_id"]),
                companies.financials_part(cuis), companies.anaf_part(cuis),
                companies.taxpayer_part(cuis))

    try:
        reg, fin, anaf, tax = await asyncio.to_thread(parts)
        row["record"] = {"cuis": [{"cui": c, "sources": keys["sources"].get(c, [])}
                                  for c in keys["cuis"]],
                         "registry": reg, "financials": fin, "anaf": anaf, "taxpayer": tax,
                         "signals": found["signals"]}
    except Exception as e:
        logger.warning("company record failed: %s", e)
        row["record"] = None
    row.update(found["counts"])
    return row


def _official_modules() -> set[str]:
    from harvey.modules import switches

    return {m.id for m in switches.manifests() if m.official}


# ── The Registry page: the whole trade registry, with its financials ──
#
# harvey/registry_view.py lists and orders the firms; the filters are the
# registry source's own (its manifest, read by job_kinds.registry_params),
# so the page shows what a pull of the same filters saves. Pulling goes
# through Discover's estimate and run, with the same filters or the
# registration numbers ticked.


def _registry_source(enabled=None) -> dict | None:
    from harvey.modules.job_kinds import discover_sources

    return next((s for s in discover_sources(enabled) if s["job"] == "registry_pull"), None)


async def _in_harvey(cuis: list[str], regs: list[str] = ()) -> dict[str, str]:
    """CUI, or "onrc:" + registration number, -> the id of the company on
    file with it (its own CUI or one it was also given; a firm with no CUI
    is saved under its registration number; a merged record stands for
    its survivor)."""
    cuis = [c for c in dict.fromkeys(cuis) if c]
    keys = [f"onrc:{r}" for r in dict.fromkeys(regs) if r]
    if not (cuis or keys) or not DB_PATH.exists():
        return {}
    state = _state()
    # Held firms by their identifiers (a merged record's stand for
    # its survivor).
    found = await state.held_ids("ro.cui", cuis)
    found.update({f"onrc:{reg}": cid for reg, cid in (await state.held_ids(
        "ro.onrc_reg", [k[len("onrc:"):] for k in keys])).items()})
    return found


def _on_file(found: dict[str, str], cui: str, reg: str) -> str | None:
    return (found.get(cui) if cui else None) or (found.get(f"onrc:{reg}") if reg else None)


@app.get("/api/registry")
async def get_registry(request: Request, limit: int | None = None, offset: int | None = None,
                       sort: str = "", dir: str = ""):
    """One page of the trade registry: {total, offset, limit, sort, dir,
    rows, filters, loaded}. Filters (query string) are the registry
    source's: words (activity, CAEN code or name), county, locality,
    status (operating, or all), min/max_turnover, min/max_employees,
    unknown, employer, vat, micro. With none, every operating firm. Sort:
    name, turnover, employees, profit (the latest filed year's) or
    registered. Each row is the registry's firm with ``figures`` (its
    latest year) and ``company_id`` when it is already on file."""
    from harvey import registry_view
    from harvey.modules import switches
    from harvey.modules.job_kinds import registry_params

    limit, offset = _page_args(limit, offset)
    state = _state()
    await state.init_db()
    source = _registry_source(await switches.enabled_ids(state))
    loaded = registry_view.loaded()
    empty = {"total": 0, "offset": offset, "limit": limit, "sort": sort, "dir": dir,
             "rows": [], "loaded": loaded, "source": source}
    if source is None or not source["enabled"]:
        return {**empty, "off": True}
    if not loaded["registry"]:
        return empty
    args = {k: v for k, v in request.query_params.items()
            if k not in ("limit", "offset", "sort", "dir")}
    registry_view.warm()
    try:
        params = registry_params(args)
        got = await asyncio.to_thread(registry_view.page, params, sort, dir, limit, offset)
    except FileNotFoundError as e:
        return JSONResponse({**empty, "error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({**empty, "error": str(e)}, status_code=400)
    on_file = await _in_harvey([r["cui"] for r in got["rows"]],
                               [r["reg"] for r in got["rows"] if not r["cui"]])
    for r in got["rows"]:
        r["company_id"] = _on_file(on_file, r["cui"], r["reg"])
    return {**empty, **got}


@app.get("/api/registry/firm")
async def get_registry_firm(reg: str = ""):
    """One firm by registration number: the registry's row with its
    activities and administrators, every financial year on file, the
    taxpayer file's row and ANAF's kept answer, and the company on file
    with its CUI (``company_id``)."""
    from harvey import registry_view

    if not reg.strip():
        return JSONResponse({"error": "reg is required"}, status_code=400)
    try:
        found = await asyncio.to_thread(registry_view.firm, reg.strip())
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if found is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    found["company_id"] = _on_file(await _in_harvey([found["cui"]], [found["reg"]]),
                                   found["cui"], found["reg"])
    return found


# ── Possible duplicates ──
#
# The resolver (harvey/resolver.py) merges a company found twice on its own
# only when sure; a possible match waits here for a person. Nothing is
# deleted either way: a merge moves one company's facts onto the other and
# can be undone.


def _refused(e) -> JSONResponse:
    return JSONResponse({"error": e.code}, status_code=409 if e.code != "not_found" else 404)


@app.get("/api/duplicates")
async def get_duplicates(status: str = "pending", limit: int = 100):
    """The review list as groups (``status`` pending: companies that may be
    one, each member with its name, CUI, town, phones, emails, domains and
    sources, the record to keep and why they match), pairs a person kept
    apart (``status`` separate), or merges in force (``status`` merged,
    with the group each belongs to). ``pending`` counts the groups."""
    empty = {"status": status, "pending": 0, "rows": []}
    if status not in ("pending", "merged", "separate") or not DB_PATH.exists():
        return empty
    limit = max(1, min(limit, 500))
    try:
        state = _state()
        await state.init_db()
        if status == "pending":
            pending, rows = await state.duplicate_groups(limit)
        else:
            rows = await (state.merges(limit) if status == "merged"
                          else state.duplicates(status, limit))
            pending = await state.count_duplicates()
    except Exception as e:
        logger.warning("duplicates failed: %s", e)
        return JSONResponse({**empty, "error": "duplicates failed"}, status_code=500)
    return {"status": status, "pending": pending, "rows": rows}


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _ids(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(x).strip() for x in value if str(x or "").strip()))


@app.post("/api/duplicates/merge")
async def merge_duplicates(request: Request):
    """"Same company". A group: ``keep`` keeps the record, every one of
    ``members`` merges into it, and each of ``out`` (taken out of the group)
    is marked a different company. A pair (``a``, ``b``): the one with a CUI
    (else the one holding most) keeps the record."""
    from harvey.resolver import MergeRefused

    data = await _body(request)
    keep = str(data.get("keep") or "").strip()
    members = _ids(data.get("members"))
    a, b = str(data.get("a") or "").strip(), str(data.get("b") or "").strip()
    state = _state()
    await state.init_db()
    try:
        if keep or members:
            if not keep or not members:
                return JSONResponse({"error": "keep and members needed"}, status_code=400)
            return await state.merge_group(keep, members, out=_ids(data.get("out")))
        if not a or not b:
            return JSONResponse({"error": "two companies needed"}, status_code=400)
        return await state.merge_pair(a, b)
    except MergeRefused as e:
        return _refused(e)


@app.post("/api/duplicates/separate")
async def separate_duplicates(request: Request):
    """"Different companies": each of ``out`` is not any of ``rest`` (taken
    out of its group), or with ``rest`` empty, all of ``out`` are different
    from one another (a whole group). ``a`` and ``b``: one pair."""
    from harvey.resolver import MergeRefused

    data = await _body(request)
    out, rest = _ids(data.get("out")), _ids(data.get("rest"))
    a, b = str(data.get("a") or "").strip(), str(data.get("b") or "").strip()
    if not out and a and b:
        out, rest = [a], [b]
    if len(set(out) | set(rest)) < 2:
        return JSONResponse({"error": "two companies needed"}, status_code=400)
    state = _state()
    await state.init_db()
    try:
        await state.separate_group(out, rest)
    except MergeRefused as e:
        return _refused(e)
    return {"ok": True}


@app.post("/api/duplicates/undo")
async def undo_duplicate(request: Request):
    """Undo a merge: ``company_id`` is the company that was merged; with
    ``group`` true, the whole group merge it kept or joined."""
    from harvey.resolver import MergeRefused

    data = await _body(request)
    company_id = str(data.get("company_id") or "").strip()
    if not company_id:
        return JSONResponse({"error": "company_id needed"}, status_code=400)
    state = _state()
    await state.init_db()
    try:
        if data.get("group"):
            return {"undone": await state.undo_group(company_id)}
        return await state.undo_merge(company_id)
    except MergeRefused as e:
        return _refused(e)


@app.post("/api/duplicates/scan")
async def scan_duplicates():
    """Look for duplicates among every company on file: queued as a job on
    the duplicates lane (the Queue page shows it), the panel reloads when it
    is done."""
    from harvey.modules import job_kinds, jobs

    state = _state()
    await state.init_db()
    try:
        job_id = await job_kinds.enqueue_duplicates_scan(state)
    except jobs.Duplicate as e:
        return {"job": e.job.id if getattr(e, "job", None) else "", "queued": False}
    return {"job": job_id, "queued": True}


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Harvey Controls ──


@app.get("/api/harvey/status")
async def get_harvey_status():
    """Check if Harvey is currently running."""
    pid = _check_harvey_pid()
    started = _harvey_started_at.isoformat() if _harvey_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/harvey/start")
async def start_harvey():
    """Start Harvey's heartbeat loop as a subprocess."""
    global _harvey_process, _harvey_started_at

    if _check_harvey_pid():
        return {"success": False, "message": "Harvey is already running."}
    from harvey.main import _needs_setup
    if _needs_setup():
        return {"success": False,
                "message": "Harvey isn't set up yet. Run `harvey setup` in a terminal first."}

    # Ensure data dir exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # The native Claude CLI installer puts claude in ~/.local/bin without
    # always adding it to PATH; the heartbeat can't think without it.
    env = os.environ.copy()
    local_bin = Path.home() / ".local" / "bin"
    if not shutil.which("claude") and local_bin.is_dir():
        env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

    try:
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        try:
            _harvey_process = subprocess.Popen(
                [sys.executable, "-m", "harvey", "run"],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                env=env,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Harvey: %s", e)
        return {"success": False, "message": f"Failed to start Harvey: {e}"}
    _harvey_started_at = datetime.now()
    # The loop writes its own PID file once it holds the loop lock. Writing
    # the child's PID here would overwrite a running loop's record if this
    # child then finds the lock taken and exits.
    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Harvey subprocess."""
    global _harvey_process, _harvey_started_at

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Harvey is not running."}

    own = _harvey_process if _harvey_process and _harvey_process.pid == pid else None

    def alive() -> bool:
        # Our own child is reaped by poll(); left unreaped it is a zombie,
        # which still answers signal 0.
        if own is not None:
            return own.poll() is None
        return pid_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            if not alive():
                break
            await asyncio.sleep(0.5)
        else:
            # Force kill if still running (Windows has no SIGKILL; SIGTERM
            # there is already TerminateProcess)
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            await asyncio.sleep(0.5)
    except OSError as e:
        logger.warning("Couldn't signal Harvey (pid %s): %s", pid, e)

    if alive():
        # Started by another user, or from an elevated terminal on Windows.
        return {"success": False,
                "message": f"Couldn't stop Harvey (pid {pid}). Stop it where it was started."}

    _harvey_process = None
    _harvey_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/harvey/logs")
async def get_harvey_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


def _pipeline_bucket(status: str | None, stage: str | None, intent: str | None,
                     email_status: str | None = None) -> str:
    """One funnel stage per contact, from its status and latest conversation."""
    status = status or "new"
    if status in ("new", "queued"):
        return "to_write"
    if status == "contacted":
        # A bounce leaves the status alone and marks the address invalid.
        return "closed_out" if email_status == "invalid" else "contacted"
    if status == "meeting":
        return "ready_to_meet"
    if status == "replied" and stage != "closed_lost":
        if stage == "closing":
            return "ready_to_meet"
        if intent == "interested" or stage in ("presenting", "negotiating"):
            return "interested"
        return "replied"
    return "closed_out"   # lost, opted out, bounced, unwritable


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        # Where each contact stands. Prospect status alone stops at
        # "replied"; how a reply went lives on its latest conversation.
        rows = await query_db("""
            SELECT p.status, p.email_status, l.stage, l.intent, COUNT(*) AS count FROM prospects p
            LEFT JOIN (SELECT prospect_id, stage, intent, ROW_NUMBER() OVER (
                         PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn
                       FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1
            GROUP BY p.status, p.email_status, l.stage, l.intent
        """)
        pipeline = dict.fromkeys(
            ("to_write", "contacted", "replied", "interested", "ready_to_meet", "closed_out"), 0)
        for r in rows:
            pipeline[_pipeline_bucket(r["status"], r["stage"], r["intent"],
                                      r["email_status"])] += r["count"]

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        # usage_log rows are keyed by the LOCAL date (increment_usage /
        # get_usage_today use date.today()); SQLite's date('now') is UTC and
        # can select a different day's counts on either side of UTC midnight.
        usage = await query_db(
            "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?",
            (date.today().isoformat(),),
        )
        usage_today = (usage[0]["claude_calls"] or 0) if usage else 0
        codex_today = (usage[0]["codex_calls"] or 0) if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "actions_total": action_count,
            "claude_calls_today": usage_today,
            "codex_calls_today": codex_today,
            "brain_provider": _active_provider(),
            "brain_status": await _brain_status(),
        }
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_quota_clients: dict = {}


def _active_brain():
    """The brain config the config file selects (None if unreadable)."""
    try:
        from harvey.config import load_brain_config
        return load_brain_config()
    except Exception:
        return None


async def _brain_status() -> dict:
    """Last recorded brain health transition ({} if none): lets the
    dashboard say "Codex paused: <why>" instead of looking healthy."""
    from harvey.brain import BRAIN_STATUS_KEY, active_brain_status
    rows = await query_db("SELECT value FROM settings WHERE key = ?", (BRAIN_STATUS_KEY,))
    # Only the configured provider's health (a stale Codex pause isn't
    # Claude's).
    return active_brain_status(rows[0]["value"], _active_provider()) if rows else {}


def _brain_config_or_error():
    """(the brain section of the config the dashboard reads, None) or
    (None, why it can't be read)."""
    from harvey.config import ConfigError, load_brain_config
    try:
        return load_brain_config(str(_resolve_config_file())), None
    except ConfigError as e:
        return None, str(e)


@app.get("/api/brain")
async def get_brain(full: bool = False, refresh: bool = False, provider: str = ""):
    """Settings → Model: the provider, model, reasoning effort and login
    (brain.auth) with its status (brain_auth.status: the CLI's version
    check, linked as what, the last error's class; ``full`` with Codex's
    account and its models, ``refresh`` asks the CLIs again). ``provider``
    (claude, codex): that provider's status under the same login, for the
    panel before a switch is saved."""
    from harvey import brain_auth
    from harvey.config import BRAIN_AUTH_READY, BRAIN_PROVIDERS, CODEX_REASONING_EFFORTS, BrainConfig
    brain, error = _brain_config_or_error()
    if brain is None:
        return JSONResponse({"error": error}, status_code=500)
    if provider and provider != brain.provider:
        if provider not in BRAIN_PROVIDERS:
            return JSONResponse({"error": f"'{provider}' is not a brain provider"}, status_code=400)
        brain = BrainConfig(provider=provider, auth=brain.auth)
    state = _state()
    await state.init_db()
    out = await brain_auth.status(brain, state=state, full=full, force=refresh)
    out.update({"efforts": list(CODEX_REASONING_EFFORTS), "auth_ready": list(BRAIN_AUTH_READY),
                "file": _resolve_config_file().name})
    return out


@app.post("/api/brain")
async def set_brain(request: Request):
    """Save Settings → Model: {provider, model, reasoning_effort, auth,
    usd_per_day, usd_per_month, codex_usd_per_mtok_in, codex_usd_per_mtok_out},
    each optional, into harvey.local.yaml (config.set_brain_settings; a
    blank amount clears it). Codex is refused where it can't run (Windows),
    and with no model (a blank one leaves the pick to a catalog that can
    hide it); a login the panel doesn't offer is refused unless it is the
    one already set, and an API key's login until a key is saved
    (/api/brain/key)."""
    from harvey import brain_auth
    from harvey.config import BRAIN_KEYS, ConfigError, set_brain_settings
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body or set(body) - set(BRAIN_KEYS):
        return _refuse("expected {provider, model, reasoning_effort, auth}", 400)
    current, error = _brain_config_or_error()
    if current is None:
        return _refuse(error, 409)
    values = {k: str(v if v is not None else "").strip() for k, v in body.items()}
    if not values.get("provider"):
        values.pop("provider", None)
    provider = values.setdefault("provider", current.provider).lower()
    if provider != current.provider:
        # A model (and an effort) is one provider's: a switch that names
        # none never carries the other one's along.
        values.setdefault("model", "")
        values.setdefault("reasoning_effort", "")
    if provider == "codex":
        why = brain_auth.codex_refusal()
        if why:
            return _refuse(why, 409)
        model = values["model"] if "model" in values else (
            current.model if current.provider == "codex" else "")
        if not model:
            return _refuse("Pick a Codex model: with none, Harvey would take the first one the "
                           "catalog lists, and an account's catalog can hide it, so every call "
                           "would fail.", 400)
    else:
        values["reasoning_effort"] = ""
    auth = (values.get("auth") or current.auth).lower()
    if auth not in brain_auth.OFFERED and auth != current.auth:
        return _refuse(f"Linking with '{auth}' isn't available from the dashboard yet.", 409)
    if auth == brain_auth.API_KEY and not brain_auth.key_set(provider):
        return _refuse(f"Save the {'Codex' if provider == 'codex' else 'Claude'} API key first: "
                       "paste it in the key box.", 409)
    try:
        path = set_brain_settings(values, _resolve_config_file())
    except ConfigError as e:
        return _refuse(str(e), 400)
    brain_auth.forget()
    _quota_clients.clear()
    state = _state()
    await state.init_db()
    saved, _ = _brain_config_or_error()
    await state.log_action("brain_settings", "dashboard", {
        "provider": saved.provider, "model": saved.model, "auth": saved.auth,
        "reasoning_effort": saved.reasoning_effort, "file": path.name})
    return {"success": True, "file": path.name, "loop_running": bool(_check_harvey_pid()),
            "brain": {k: getattr(saved, k) for k in BRAIN_KEYS}}


@app.post("/api/brain/key")
async def save_brain_key(request: Request):
    """Save an API key for the brain (brain.auth: api_key): {provider, key}.
    Checked first with a free call (brain_auth.check_key: the provider's
    list of models, no model call), then kept: Claude's in .env under
    brain_auth.CLAUDE_KEY_ENV, Codex's by Codex itself in data/auth/codex
    (brain_auth.link_codex_key). The key is never returned or logged: the
    answer says only that one is set."""
    from harvey import brain_auth
    from harvey.config import BRAIN_PROVIDERS
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected {provider, key}", 400)
    provider = str(body.get("provider") or "").strip().lower()
    key = body.get("key")
    if provider not in BRAIN_PROVIDERS:
        return _refuse("Say which provider the key is for: claude or codex.", 400)
    if not isinstance(key, str):
        return _refuse("Paste the API key.", 400)
    key = key.strip()
    if provider == "codex":
        why = brain_auth.codex_refusal()
        if why:
            return _refuse(why, 409)
    problem = await brain_auth.check_key(provider, key)
    if problem:
        return _refuse(problem, 400)
    if provider == "codex":
        from harvey import codex
        try:
            await brain_auth.link_codex_key(key)
        except codex.CodexError as e:
            return _refuse(str(e), 409)
    else:
        async with _env_lock:
            try:
                _write_env_file({brain_auth.CLAUDE_KEY_ENV: key})
            except Exception as e:
                logger.warning("Failed to write .env: %s", type(e).__name__)
                return _refuse("Could not write the .env file.", 500)
    brain_auth.forget()
    state = _state()
    await state.init_db()
    await state.log_action("brain_key", "dashboard", {"provider": provider, "saved": True})
    return {"success": True, "provider": provider, "key_set": True}


@app.delete("/api/brain/key")
async def delete_brain_key(provider: str = ""):
    """Unlink an API key: Claude's cleared from .env, Codex's removed from
    data/auth/codex. A brain on that provider's API key goes back to the
    terminal login, so no call is made with a key that isn't there."""
    from harvey import brain_auth
    from harvey.config import BRAIN_PROVIDERS, ConfigError, set_brain_settings
    provider = (provider or "").strip().lower()
    if provider not in BRAIN_PROVIDERS:
        return _refuse("Say which provider's key to remove: claude or codex.", 400)
    if provider == "codex":
        brain_auth.unlink_codex_key()
    else:
        async with _env_lock:
            try:
                _write_env_file({brain_auth.CLAUDE_KEY_ENV: ""})
            except Exception as e:
                logger.warning("Failed to write .env: %s", type(e).__name__)
                return _refuse("Could not write the .env file.", 500)
            os.environ.pop(brain_auth.CLAUDE_KEY_ENV, None)
    current, _ = _brain_config_or_error()
    auth = current.auth if current else ""
    if current is not None and current.provider == provider and auth == brain_auth.API_KEY:
        try:
            set_brain_settings({"auth": brain_auth.TERMINAL}, _resolve_config_file())
            auth = brain_auth.TERMINAL
        except ConfigError as e:
            return _refuse(str(e), 409)
    brain_auth.forget()
    _quota_clients.clear()
    state = _state()
    await state.init_db()
    await state.log_action("brain_key", "dashboard", {"provider": provider, "saved": False})
    return {"success": True, "provider": provider, "key_set": False, "auth": auth}


def _active_provider() -> str:
    brain = _active_brain()
    return brain.provider if brain else ""


def _quota_client_for(provider: str, model: str = "", auth: str = "terminal"):
    """One cached quota reader per provider (and Codex model and login).
    Each reads only its own subscription, so a Claude gauge is never shown
    as Codex or vice versa, and Codex shows the bucket metering the
    configured model."""
    from harvey import brain_auth

    if auth == brain_auth.API_KEY:
        # (An API key has no plan to read: the brain's USD caps hold it.)
        return None
    if provider == "claude":
        # (Claude's readings are kept in the database, so the reader holds
        # nothing of its own.)
        from harvey.integrations.quota import QuotaClient
        return QuotaClient(_state(), auth=auth)
    if provider != "codex":
        return None
    key = (provider, model, auth)
    if key not in _quota_clients:
        from harvey.codex import CodexClient, CodexQuotaClient
        _quota_clients[key] = CodexQuotaClient(
            CodexClient(model=model, native_home=brain_auth.codex_home(auth)))
    return _quota_clients[key]


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota for the Usage tab."""

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = rows[0] if rows else {}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_provider = await query_db(grouped("provider", "provider"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    brain = _active_brain()
    provider = brain.provider if brain else ""
    from harvey import brain_spend
    money = None
    if brain is not None and brain_spend.api_key(brain):
        try:
            state = _state()
            await state.init_db()
            money = await brain_spend.totals(state, brain)
        except Exception as e:
            logger.debug("API-key spend not read: %s", e)
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "",
                                   brain.auth if brain else "terminal")
        if client is not None:
            quota = await client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "provider": provider,
        "quota": quota,
        # Whose quota this is, even when it couldn't be read, so the UI shows
        # "unavailable" for the active provider instead of hiding the gauge.
        "quota_provider": provider,
        "quota_status": ("available" if quota else "unavailable")
        if provider and money is None else "",
        # With an API key (brain.auth: api_key): today's and this month's
        # spend beside the brain's USD caps, in place of the plan's windows.
        "billing": "api_key" if money is not None else "subscription",
        "money": money,
        "by_provider": by_provider,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


# Contacts filter buckets (the Contacts tab's status filter).
_CONTACT_BUCKETS = {
    "todo": "p.status IN ('new', 'queued')",
    "contacted": "p.status = 'contacted'",
    "replied": "p.status IN ('replied', 'interested', 'meeting')",
    "closed": ("COALESCE(p.status, '') NOT IN "
               "('new', 'queued', 'contacted', 'replied', 'interested', 'meeting')"),
}


async def _opt_out_undone_ids() -> set:
    return {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}


# Romanian letters folded to plain ones inside SQLite (its lower() only
# knows ASCII), so "sablon" finds "Șablon" and "brasov" finds "Brașov".
_FOLD_PAIRS = (("ă", "a"), ("â", "a"), ("î", "i"), ("ș", "s"), ("ş", "s"), ("ț", "t"),
               ("ţ", "t"), ("Ă", "a"), ("Â", "a"), ("Î", "i"), ("Ș", "s"), ("Ş", "s"),
               ("Ț", "t"), ("Ţ", "t"))
_PHONE_PUNCT = (" ", "+", "-", ".", "(", ")", "/")


def _sql_fold(expr: str) -> str:
    for a, b in _FOLD_PAIRS:
        expr = f"REPLACE({expr}, '{a}', '{b}')"
    return f"LOWER({expr})"


def _sql_digits(expr: str) -> str:
    for ch in _PHONE_PUNCT:
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


def _fold_text(text: str) -> str:
    import unicodedata
    plain = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()


_CONTACT_TEXT = _sql_fold("(" + " || ' ' || ".join(f"COALESCE({col}, '')" for col in (
    "p.first_name", "p.last_name", "p.company", "p.email", "p.title", "p.phone",
    "c.phone", "c.name", "c.location", "c.cui")) + ")")
_CONTACT_PHONES = _sql_digits("(COALESCE(p.phone, '') || ' ' || COALESCE(c.phone, ''))")


def _contact_search(q: str) -> tuple[list[str], list]:
    """WHERE conditions for the Contacts search: every word must appear in
    the name, company, email, title, place, CUI or phone, accents ignored.
    A number also matches a phone however it is written: "0778 000" finds
    "+40 778 000 …"."""
    where, params = [], []
    for word in (q or "")[:200].split()[:8]:
        folded = _fold_text(word)
        like = "%" + folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds, args = [f"{_CONTACT_TEXT} LIKE ? ESCAPE '\\'"], [like]
        digits = re.sub(r"[\s+\-.()/]", "", word)
        if digits.isdigit() and len(digits) >= 3:
            forms = {digits}
            if digits.startswith("0") and not digits.startswith("00"):
                forms.add("40" + digits[1:])           # national 07… = international +40 7…
            elif digits.startswith("0040"):
                forms.add(digits[2:])
            for form in sorted(forms):
                conds.append(f"{_CONTACT_PHONES} LIKE ?")
                args.append("%" + form + "%")
        where.append("(" + " OR ".join(conds) + ")")
        params += args
    return where, params


@app.get("/api/prospects")
async def get_prospects(limit: int | None = None, offset: int | None = None,
                        q: str = "", filter: str = "all"):
    """Contacts. Without ``limit``/``offset``: the original list (newest
    200). With either: one page as {total, offset, limit, counts,
    prospects}, searched by ``q`` (see _contact_search) and
    narrowed by ``filter`` (all, todo, contacted, replied, closed); counts
    are per filter for the same search. Each row also carries its company's
    phone, place and CUI."""
    if limit is None and offset is None:
        rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
        # Someone whose opt-out was undone keeps a way to see what happened.
        undone = await _opt_out_undone_ids()
        for r in rows:
            r["opt_out_undone"] = r.get("id") in undone
        return rows

    limit, offset = _page_args(limit, offset)
    if not DB_PATH.exists():
        return {"total": 0, "offset": offset, "limit": limit, "counts": {}, "prospects": []}
    return await _overlay_prospects(limit, offset, q, filter)


# The Contacts page's buckets as Python tests of a prospect's status.
_BUCKET_TESTS = {
    "todo": lambda s: s in ("new", "queued"),
    "contacted": lambda s: s == "contacted",
    "replied": lambda s: s in ("replied", "interested", "meeting"),
    "closed": lambda s: s not in ("new", "queued", "contacted", "replied", "interested",
                                  "meeting"),
}


async def _overlay_prospects(limit: int, offset: int, q: str, filter: str) -> dict:
    """get_prospects' page on the overlay: the prospects (a few thousand at
    most) with their company's phone, place, CUI and name from the firm's
    record, searched and counted here as _contact_search and
    _CONTACT_BUCKETS do in SQL."""
    state = _state()
    rows = await state.read("prospect_rows")
    firms = list(dict.fromkeys(str(r["company_id"]) for r in rows
                               if str(r.get("company_id") or "").isdigit()))
    cards = {c["id"]: c for c in await state.read("company_rows", firms)} if firms else {}
    words = [w for w in (q or "")[:200].split()[:8]]
    out, counts = [], dict.fromkeys(["all", *_BUCKET_TESTS], 0)
    for r in rows:
        cid = "" if r.get("company_id") in (None, "") else str(r["company_id"])
        r["company_id"] = cid
        card = cards.get(cid) or {}
        r.update(company_phone=card.get("phone") or "", company_location=card.get("location") or "",
                 company_cui=card.get("cui") or "", company_name=card.get("name") or "")
        text = _fold_text(" ".join(str(r.get(k) or "") for k in (
            "first_name", "last_name", "company", "email", "title", "phone", "company_phone",
            "company_name", "company_location", "company_cui")))
        phones = re.sub(r"[\s+\-.()/]", "", f"{r.get('phone') or ''} {r['company_phone']}")
        if not all(_fold_text(w) in text or _phone_word(w, phones) for w in words):
            continue
        counts["all"] += 1
        status = r.get("status") or ""
        for key, test in _BUCKET_TESTS.items():
            if test(status):
                counts[key] += 1
        out.append(r)
    test = _BUCKET_TESTS.get(filter)
    if test:
        out = [r for r in out if test(r.get("status") or "")]
    page = out[offset:offset + limit]
    undone = await _opt_out_undone_ids()
    for r in page:
        r["opt_out_undone"] = r.get("id") in undone
    return {"total": counts[filter] if test else counts["all"], "offset": offset, "limit": limit,
            "filter": filter if test else "all", "counts": counts, "prospects": page}


def _phone_word(word: str, phones: str) -> bool:
    """Whether a number in the search matches a phone however it is written
    (_contact_search's forms: 07... is also +40 7...)."""
    digits = re.sub(r"[\s+\-.()/]", "", word)
    if not (digits.isdigit() and len(digits) >= 3):
        return False
    forms = {digits}
    if digits.startswith("0") and not digits.startswith("00"):
        forms.add("40" + digits[1:])
    elif digits.startswith("0040"):
        forms.add(digits[2:])
    return any(f in phones for f in forms)


def _state():
    from harvey.state import StateManager
    return StateManager(db_path=str(DB_PATH))


# ── Contacts: every way to reach a company (harvey/contacts.py) ──

CONTACT_KINDS_EXTRA = ("mobile",)
CONTACT_PAGE_MAX = 200
CONTACT_EXPORT_MAX = 500_000


from harvey.contact_query import (  # noqa: E402  (the page's filters, shared)
    filter_from_query as _contact_filter,
)


@asynccontextmanager
async def _contacts_db(attach_figures: bool = False):
    db = await aiosqlite.connect(str(DB_PATH))
    try:
        if attach_figures:
            from harvey.modules.ro.mf_financials import store as fin_store

            await db.execute("ATTACH DATABASE ? AS fin",
                             (f"file:{fin_store.FINANCIALS_PATH}?mode=ro",))
        yield db
    finally:
        await db.close()


@app.get("/api/contacts")
async def get_contacts(request: Request, view: str = "contacts", limit: int = 50,
                       offset: int = 0, sort: str = "", dir: str = "desc"):
    """One page of contacts, one row per distinct contact (a chain's shared
    phone is one row reaching many companies), or with ``view=people`` the
    people still to search (named, with no contact of their own). Filters
    (query string): kind (email, phone, mobile, or any kind), status,
    trust, person (yes/no), source, county, locality, website (yes/no),
    require/exclude (signal codes, the cohort rules), min/max_turnover,
    min/max_employees and unknown (include/exclude), q. ``total`` stops at
    contacts.COUNT_CAP (``more`` says there are more)."""
    limit = max(1, min(int(limit or 50), CONTACT_PAGE_MAX))
    offset = max(0, int(offset or 0))
    people = view == "people"
    empty = {"view": "people" if people else "contacts", "total": 0, "more": False,
             "offset": offset, "limit": limit, "rows": [], "sources": [], "kinds": [],
             "financials": False}
    if not DB_PATH.exists():
        return empty
    state = _state()
    await state.init_db()
    try:
        f, attach = await _contact_filter(state, request.query_params)
    except ValueError as e:
        return JSONResponse({**empty, "error": str(e)}, status_code=400)
    found = await state.read("contacts_listing", f, sort, dir, limit, offset, people)
    rows = found.pop("rows")
    _block_listed(rows, people, set() if people else await state.suppressed_emails(
        [r["value"] for r in rows if r.get("kind") == "email"]))
    return {**empty, **found, "rows": rows}


def _block_listed(rows: list[dict], people: bool, suppressed: set | None = None) -> None:
    """Mark the emails on compliance.do_not_contact, and those the
    do-not-contact list bars (``suppressed``), on the Contacts page."""
    blocked = _blocked_entries(_load_config_quietly()) if not people else []
    if blocked or suppressed:
        from harvey.gate import do_not_contact_match
        for r in rows:
            if r["kind"] == "email" and (r["value"] in (suppressed or ()) or (
                    blocked and do_not_contact_match(r["value"], blocked))):
                r["do_not_contact"] = True


from harvey.launch import FIND_MAX  # noqa: E402  (the queueing paths, shared with the agent)


async def _find_selection(body: dict):
    """(state, the companies a Find-contacts request selects, its filter's
    error or "") from {"filter": {the Contacts page's filters}, "company_ids",
    "force", "limit"} (launch.find_selection)."""
    from harvey import launch

    state = _state()
    await state.init_db()
    selected, error = await launch.find_selection(state, body)
    return state, selected, error


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


@app.post("/api/contacts/find/estimate")
async def estimate_find_contacts(request: Request):
    """What a Find-contacts run over the current filter would do and may
    cost: how many companies (with no contact, or all with ``force``), how
    many have a website to read (free), how many need a website search, and
    its price through the cheapest search module that can find a company;
    ``checks``: the address checks it may make and what they may cost past
    the verifiers' free allowances (finder.checks_estimate); ``classifier``:
    its calls and their cost as a range (finder.classify_estimate). Both
    are estimates: the run's caps are what is enforced, step by step."""
    from harvey import launch

    body = await _json_body(request)
    state, selected, error = await _find_selection(body)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    return await launch.find_estimate(state, selected, bool(body.get("force")))


@app.post("/api/contacts/find")
async def find_contacts(request: Request):
    """Queue the contact finder for the current filter ({"filter", "force",
    "max_spend", "limit"}, or "company_ids"): one batch, one job per
    company. ``max_spend`` caps the website searches and address checks
    (0, the default: websites are read, nothing is paid for);
    ``classify_max_spend`` what the run's classifier calls may cost (absent
    or null: no cap of the run's own; 0: none, a site no register fact
    settles waits unlinked). A company already searched by a module's
    capability is skipped by it, unless ``force`` (searched again, once in
    this run). The run's report: GET /api/jobs/batches/{batch_id}/report."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_contact_search

    body = await _json_body(request)
    try:
        max_spend = _money(body.get("max_spend"), default=0.0)
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "max_spend must be a number of "
                             "dollars, 0 or more", "code": "bad_cap"}, status_code=400)
    try:
        classify_cap = None if body.get("classify_max_spend") in (None, "") \
            else _money(body.get("classify_max_spend"))
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "classify_max_spend must be a "
                             "number of dollars, 0 or more", "code": "bad_cap"},
                            status_code=400)
    state, selected, error = await _find_selection(body)
    if error:
        return JSONResponse({"success": False, "message": error}, status_code=400)
    if not selected:
        return {"success": True, "queued": 0, "companies": 0, "force": bool(body.get("force"))}
    try:
        batch_id, ids, dupes = await enqueue_contact_search(
            state, [c["id"] for c in selected], max_spend=max_spend,
            force=bool(body.get("force")), classify_max_spend=classify_cap)
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    return {"success": True, "batch_id": batch_id, "queued": len(ids),
            "already_waiting": len(dupes), "companies": len(selected),
            "max_spend": max_spend, "classify_max_spend": classify_cap,
            "force": bool(body.get("force"))}


@app.get("/api/contacts/finder")
async def contact_finder_status():
    """The contact finder's backlog: companies with no contact, those with
    a website still to read (worked through in the background, free), those
    it has looked at, what it did in the last day, and its jobs waiting."""
    from harvey import finder

    if not DB_PATH.exists():
        return {"without_contact": 0, "site_to_read": 0, "searched": 0,
                "last_day": {"companies": 0, "found": 0, "cost": 0.0}, "jobs": {}}
    state = _state()
    await state.init_db()
    out = await finder.backlog(state)
    config = _load_config_quietly()
    spend = getattr(config, "spend", None)
    out["day_budget"] = getattr(spend, "per_day_usd", None)
    return out


@app.get("/api/verify/prices")
async def get_verify_prices():
    """Each verification service and Hunter's pattern lookup this month
    (harvey/verify_costs.py status): its free allowance, the requests made
    and how many were past it, and its price (entered, declared); with the
    month's checks against EMAIL_VERIFY_MONTHLY_CHECKS."""
    from harvey import verify_costs

    state = _state()
    await state.init_db()
    return await verify_costs.status(state)


@app.post("/api/verify/prices")
async def set_verify_prices(request: Request):
    """Enter what one request past a service's free allowance costs, in
    USD ({service: amount, or null to clear}). A run with a USD cap refuses
    a request past the allowance with no price."""
    from harvey import verify_costs

    body = await _json_body(request)
    known = verify_costs.verifiers()
    if not body or any(k not in known for k in body):
        return JSONResponse({"success": False, "message": "expected prices for "
                             + ", ".join(sorted(known))}, status_code=400)
    prices = {}
    for key, value in body.items():
        try:
            if isinstance(value, bool):
                raise ValueError(key)
            prices[key] = None if value is None or value == "" else _money(value)
            if prices[key] is not None and prices[key] > verify_costs.MAX_USD:
                raise ValueError(key)
        except (TypeError, ValueError):
            return JSONResponse({"success": False, "message": f"{key}: a number of dollars "
                                 f"from 0 to {verify_costs.MAX_USD:g} (one request), or "
                                 "blank"}, status_code=400)
    state = _state()
    await state.init_db()
    for key, value in prices.items():
        await verify_costs.set_price(state, key, value)
    return {"success": True, **await verify_costs.status(state)}


@app.post("/api/contacts/finder/settings")
async def set_contact_finder_settings(request: Request):
    """{"background_search": bool, "background_share": 0.05-1}: whether the
    backlog's walk looks companies with no website up inside the search
    modules' free allowances, and the share of each it may take (the rest
    is kept for your own runs). Returns the background's status."""
    from harvey import finder
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    allowed = ("background_search", "background_share")
    if not isinstance(body, dict) or not body or any(k not in allowed for k in body):
        return JSONResponse({"success": False,
                             "message": "expected background_search or background_share"},
                            status_code=400)
    if "background_search" in body and not isinstance(body["background_search"], bool):
        return JSONResponse({"success": False, "message": "background_search must be "
                             "true or false"}, status_code=400)
    if "background_share" in body:
        try:
            share = float(body["background_share"])
        except (TypeError, ValueError):
            share = float("nan")
        if not 0 < share <= 1:
            return JSONResponse({"success": False, "message": "background_share must be "
                                 "above 0 and at most 1"}, status_code=400)
    state = _state()
    await state.init_db()
    for key, value in body.items():
        await switches.set_module_setting(state, finder.FINDER[0], key, value)
    return {"success": True, **await finder.background_status(state)}


# ── The classifier (harvey/classify.py) and the found websites' check ──

# The install's caps the Classifier card may change, whatever the profile
# (the setup itself is a profile's: /api/classify/profiles).
CLASSIFY_FIELDS = {"per_day": "classify_per_day", "tasks": "classify_tasks",
                   "usd_per_day": "classify_usd_per_day", "usd_per_month": "classify_usd_per_month"}
# The ones that are USD amounts (blank or null: none).
CLASSIFY_MONEY = ("usd_per_day", "usd_per_month")


async def _classify_holds(state, by_task: dict) -> dict:
    """{profile id: {"seconds", "why", "scope"}} of every profile a task is
    set to that is held now (its own hold, or its account's)."""
    from harvey import classify

    out = {}
    for s in {id(v): v for v in by_task.values()}.values():
        if s.provider == "off" or not s.profile:
            continue
        h = await asyncio.to_thread(classify.held, s)
        if h["seconds"] > 0:
            out[s.profile] = {**h, "seconds": int(h["seconds"]), "name": s.profile_name}
    return out


async def _classify_status(state) -> dict:
    from harvey import classify, site_check
    from harvey import classify_prices as prices

    s = await classify.settings(state)
    holds = await _classify_holds(state, s.by_task or {"site": s})
    key_set = bool(s.key_name) and bool(classify._key(s)) if s.provider in (
        "openai_compatible", "anthropic", "decision") else None
    from harvey import classify_profiles as profiles

    deepinfra = prices.is_deepinfra(s.base_url)
    return {**s.public(), "providers": list(classify.PROVIDERS),
            "use": await profiles.uses(state),
            "calls_today": await classify.calls_today(state),
            # (Each profile's own hold: one held never stops another.)
            "held": holds, "held_seconds": int((holds.get(s.profile) or {}).get("seconds", 0)),
            "key_set": key_set, "sdk_missing": classify.sdk_missing(s.provider),
            "waiting": await site_check.waiting(state),
            "cost": await classify.cost_status(state, s),
            "budget_reached": s.provider != "off"
            and await classify.available(state, "site", s) == "budget",
            "metrics": await classify.metrics(state),
            "deepinfra": deepinfra,
            "price_list": deepinfra and s.provider in prices.KINDS,
            "account": deepinfra and bool(key_set)}


@app.get("/api/classify")
async def get_classify():
    """The classifier as the site task is set (its profile's settings: the
    key's name, never a value; whether the .env holds one), the install's
    caps, today's calls, which profiles are held and until when (``held``:
    {profile: {seconds, why, scope}}), and how many found websites wait
    for a check."""
    if not DB_PATH.exists():
        return {"provider": "off", "waiting": 0, "calls_today": 0, "held": {}}
    state = _state()
    await state.init_db()
    return await _classify_status(state)


@app.post("/api/classify/settings")
async def set_classify(request: Request):
    """Change the install's caps on every profile's calls: per_day (calls a
    day), tasks ({"site": {"per_day": 500}, ...}: each task's day cap),
    usd_per_day and usd_per_month (a number of at least 0, or blank / null
    for none). A classifier's setup is a profile's
    (/api/classify/profiles)."""
    from harvey import classify, finder
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not body or any(k not in CLASSIFY_FIELDS for k in body):
        return JSONResponse({"success": False, "message": "expected any of "
                             + ", ".join(CLASSIFY_FIELDS) + " (a classifier's setup is a "
                             "profile's: /api/classify/profiles)"}, status_code=400)
    for money in CLASSIFY_MONEY:
        if money in body:
            raw = body[money]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                body[money] = ""
                continue
            value = classify.money_setting(raw)
            if value is None or isinstance(raw, bool):
                return JSONResponse({"success": False, "message": f"{money} is an amount in "
                                     "USD of at least 0, or blank for none"}, status_code=400)
            body[money] = f"{value:g}"
    if "tasks" in body:
        if not isinstance(body["tasks"], dict) or any(
                k not in classify.TASKS or not isinstance(v, dict) for k, v in body["tasks"].items()):
            return JSONResponse({"success": False, "message": "tasks: {name: {per_day}} "
                                 "for " + ", ".join(classify.TASKS)}, status_code=400)
        try:
            body["tasks"] = json.dumps({
                k: {"per_day": min(classify.MAX_PER_DAY, max(0, int(v.get("per_day") or 0)))}
                for k, v in body["tasks"].items()})
        except (TypeError, ValueError):
            return JSONResponse({"success": False, "message": "tasks: per_day is a number"},
                                status_code=400)
    state = _state()
    await state.init_db()
    for key, value in body.items():
        await switches.set_module_setting(state, finder.FINDER[0], CLASSIFY_FIELDS[key], value)
    return {"success": True, **await _classify_status(state)}


@app.get("/api/classify/models")
async def get_classify_models(provider: str = ""):
    """DeepInfra's models the classifier can be asked through, with their
    price a million input and output tokens and their context, cheapest
    first (text-generation, for openai_compatible: the current provider's
    when none is named). Fetched here, kept a day; sorted by price and
    nothing else."""
    from harvey import classify
    from harvey import classify_prices as prices

    state = _state()
    await state.init_db()
    s = await classify.settings(state)
    provider = provider or s.provider
    if provider not in prices.KINDS:
        return JSONResponse({"models": [], "error": "a price list is for openai_compatible"},
                            status_code=400)
    return await prices.choices(state, provider)


@app.get("/api/classify/account")
async def get_classify_account(refresh: bool = False, profile: str = ""):
    """The classifier key's DeepInfra account, read here with the key
    (never sent to the browser): this month's usage per model, the key's
    limit and spend, the balance; each part says when it can't be read.
    ``profile``: that profile's key (else the site task's)."""
    from harvey import classify, classify_profiles as profiles
    from harvey import classify_prices as prices

    state = _state()
    await state.init_db()
    s = await profiles.settings_for(state, profile) if profile else await classify.settings(state)
    if s is None:
        return JSONResponse({"error": "no such profile"}, status_code=404)
    return await prices.account(s, refresh=refresh)


@app.post("/api/classify/test")
async def start_classify_test():
    """Run the classifier over the built-in labelled set, as a job: its id
    (GET /api/classify/test/{id} for the summary)."""
    from harvey import classify
    from harvey.modules import jobs
    from harvey.modules.job_kinds import CLASSIFY_LANE

    state = _state()
    await state.init_db()
    if (await classify.settings(state)).provider == "off":
        return JSONResponse({"success": False, "message": "choose a provider first"},
                            status_code=409)
    try:
        job_id = await jobs.enqueue(state, "classify_test", CLASSIFY_LANE, {},
                                    dedupe_key="classify_test", label="Classifier test",
                                    module="contact_finder")
    except jobs.Duplicate as e:
        job_id = e.job.id
    jobs.notify()
    return {"success": True, "job_id": job_id}


@app.get("/api/classify/test/{job_id}")
async def get_classify_test(job_id: str):
    from harvey.modules import jobs

    state = _state()
    await state.init_db()
    job = await jobs.get(state, job_id)
    if job is None or job.kind != "classify_test":
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": job.status, "error": job.error, "result": job.result}


# ── Classifier profiles (harvey/classify_profiles.py) ──

def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"success": False, "message": message}, status_code=status)


async def _profiles_view(state) -> dict:
    from harvey import classify, classify_prices, classify_profiles as profiles

    use = await profiles.uses(state)
    gone = set(await profiles.hidden(state))
    runs = await profiles.tests(state)
    base = await classify.install_caps(state)
    out = []
    for p in (await profiles.every(state, with_hidden=True)).values():
        row = profiles.public(p, use=use, runs=runs.get(p["id"]) or [],
                              key_set=profiles.key_set(p), hidden_=p["id"] in gone)
        # (Its own hold, or its account's: {seconds, why, scope}; 0 not held.)
        h = await asyncio.to_thread(classify.held, profiles.settings_of(p, base))
        row["held"] = {**h, "seconds": int(h["seconds"])}
        out.append(row)
    return {"profiles": out, "use": use, "hidden": sorted(gone),
            "tasks": {t: {**profiles.TASK_DOCS.get(t, {}),
                          "kinds": [k for k in profiles.KINDS if t in profiles.kind_tasks(k)]}
                      for t in classify.TASKS},
            "kinds": list(profiles.KINDS), "test_set": profiles.catalog().get("test_set"),
            "about": profiles.catalog().get("about"),
            "keys": profiles.catalog().get("keys") or {},
            # (The host a base URL must have, exactly, for DeepInfra's
            # price list and account: classify_prices.is_deepinfra.)
            "deepinfra_host": classify_prices.DEEPINFRA_HOST}


@app.get("/api/classify/profiles")
async def get_classify_profiles():
    """Every classifier profile (the built-ins with what they measured, and
    yours), which task is set to which, and what each task is for and
    when it is asked. A key is named, never shown."""
    state = _state()
    await state.init_db()
    return await _profiles_view(state)


@app.post("/api/classify/profiles")
async def add_classify_profile(request: Request):
    """A new profile of your own: {name, kind, base_url, model, key_name,
    request_extra, json_mode, per_minute, timeout_seconds, prices:
    {in_per_m, out_per_m} | {from_list: true}, tasks, questions, notes}.
    It is "not tested" until a test run records results on it."""
    from harvey import classify_profiles as profiles

    state = _state()
    await state.init_db()
    try:
        p = await profiles.save(state, await _json_body(request))
    except ValueError as e:
        return _bad(str(e))
    return {"success": True, "profile": p, **await _profiles_view(state)}


@app.post("/api/classify/profiles/{profile_id}")
async def change_classify_profile(profile_id: str, request: Request):
    """Change a profile (any of its fields): a built-in's change is saved as
    a copy of your own, based on it (the reply's ``profile`` is the copy).
    A change to what it asks marks it not tested."""
    from harvey import classify_profiles as profiles

    state = _state()
    await state.init_db()
    try:
        p = await profiles.save(state, await _json_body(request), profile_id=profile_id)
    except KeyError:
        return _bad("no such profile", 404)
    except ValueError as e:
        return _bad(str(e))
    return {"success": True, "profile": p, "copied_from": profile_id
            if p["id"] != profile_id else "", **await _profiles_view(state)}


@app.post("/api/classify/use")
async def set_classify_use(request: Request):
    """{task: profile id or "off"}: which profile answers each task. A
    profile that can't answer a task (a decision model: search results)
    can't be set for it."""
    from harvey import classify_profiles as profiles

    state = _state()
    await state.init_db()
    try:
        use = await profiles.set_use(state, await _json_body(request))
    except ValueError as e:
        return _bad(str(e))
    return {"success": True, "use": use, **await _classify_status(state)}


def _test_request(body: dict) -> tuple[dict, str]:
    """A test run's {tasks, file, every, limit} checked: ``file`` is a
    labelled JSONL file on the server (the judge-test format)."""
    import os

    tasks = body.get("tasks") or ["site"]
    if tasks == "all":
        tasks = ["site", "results"]
    if not isinstance(tasks, list) or not tasks or any(t not in ("site", "results")
                                                       for t in tasks):
        return {}, "tasks: site and/or results (the contacts task is scored with site)"
    path = body.get("file") or ""
    if path:
        if not isinstance(path, str) or len(path) > 1000:
            return {}, "file is a path on the server"
        # (A labelled .jsonl file only, its link followed: never .env or
        # another file whose lines would be sent to the profile's endpoint.)
        real = os.path.realpath(path)
        if not real.endswith(".jsonl") or not os.path.isfile(real):
            return {}, "no such .jsonl file on the server"
        path = real
        if os.path.getsize(path) > 50_000_000:
            return {}, "the file is over 50 MB"
    try:
        limit = max(0, int(body.get("limit") or 0))
    except (TypeError, ValueError):
        return {}, "limit is a number"
    return {"tasks": tasks, "file": path, "every": bool(body.get("every")),
            "limit": limit}, ""


async def _test_estimate(state, profile_id: str, req: dict) -> dict:
    from harvey import classify_profiles as profiles, site_check

    s = await profiles.settings_for(state, profile_id)
    rows = site_check.read_cases(req["file"]) if req["file"] else site_check.builtin_cases()
    est = await site_check.estimate_test(state, rows, s=s, tasks=req["tasks"],
                                         every=req["every"], limit=req["limit"])
    return {**est, "profile": profile_id, "test_set": "your file" if req["file"] else "built-in"}


@app.post("/api/classify/profiles/{profile_id}/test/estimate")
async def estimate_classify_test(profile_id: str, request: Request):
    """What a test run of a profile would ask and may cost, counted before
    anything is sent: {tasks, file, every, limit} → {calls, tokens_in,
    usd_typical, usd_high, price_known, by_task, note}. An estimate: the
    run's USD cap is what is enforced, call by call."""
    from harvey import classify_profiles as profiles

    req, error = _test_request(await _json_body(request))
    if error:
        return _bad(error)
    state = _state()
    await state.init_db()
    if await profiles.settings_for(state, profile_id) is None:
        return _bad("no such profile", 404)
    try:
        return await _test_estimate(state, profile_id, req)
    except (OSError, ValueError) as e:
        return _bad(f"the file can't be read: {type(e).__name__}")


@app.post("/api/classify/profiles/{profile_id}/test")
async def start_profile_test(profile_id: str, request: Request):
    """Test a profile with Harvey's judge test, as a job on the classifier's
    lane: {tasks, file, every, limit, max_spend} (max_spend: USD the run's
    classifier calls may cost; required, 0 or more). The result is kept on
    the profile (GET /api/classify/profiles shows its tests)."""
    from harvey import classify_profiles as profiles
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_classify_test

    body = await _json_body(request)
    req, error = _test_request(body)
    if error:
        return _bad(error)
    try:
        cap = _money(body.get("max_spend"))
    except (TypeError, ValueError):
        return _bad("max_spend: the USD the test may spend, 0 or more")
    state = _state()
    await state.init_db()
    if await profiles.settings_for(state, profile_id) is None:
        return _bad("no such profile", 404)
    try:
        batch_id, job_id = await enqueue_classify_test(
            state, profile_id, tasks=req["tasks"], max_spend=cap, file=req["file"],
            every=req["every"], limit=req["limit"])
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    jobs.notify()
    return {"success": True, "job_id": job_id, "batch_id": batch_id, "max_spend": cap}


@app.post("/api/classify/profiles/{profile_id}/{action}")
async def act_on_classify_profile(profile_id: str, action: str):
    """remove (one of your own: the tasks set to it are off), hide or
    restore (a built-in)."""
    from harvey import classify_profiles as profiles

    if action not in ("remove", "hide", "restore"):
        return _bad(f"unknown action {action!r}")
    state = _state()
    await state.init_db()
    try:
        if action == "remove":
            done = await profiles.remove(state, profile_id)
        else:
            done = await profiles.hide(state, profile_id, shown=action == "restore")
    except KeyError:
        return _bad("no such profile of yours" if action == "remove" else "no such built-in",
                    404)
    return {"success": True, **done, **await _profiles_view(state)}


# ── Verifying contacts with the classifier (harvey/contact_check.py) ──

async def _verify_selection(state, body: dict) -> tuple[list[dict], str]:
    """The links a verification takes (launch.verify_selection)."""
    from harvey import launch

    return await launch.verify_selection(state, body)


@app.post("/api/contacts/verify/estimate")
async def estimate_contact_verify(request: Request):
    """What verifying the chosen links may cost, before anything is sent:
    {links, companies, calls, tokens_typical, tokens_high, basis,
    usd_typical, usd_high, price_known, profile, model, off, note}. An
    estimate: the run's cap is what is enforced, call by call."""
    from harvey import contact_check

    state = _state()
    await state.init_db()
    chosen, error = await _verify_selection(state, await _json_body(request))
    if error:
        return _bad(error)
    return await contact_check.estimate(state, chosen)


@app.post("/api/contacts/verify")
async def start_contact_verify(request: Request):
    """Verify the chosen links with the classifier's contacts task (yes /
    no / unsure), one job per company: the selection's fields and
    ``max_spend`` (USD the run's classifier calls may cost, required).
    Its batch id undoes what its no's lowered (POST /api/contacts/verify/undo)."""
    from harvey import classify
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_contact_checks

    body = await _json_body(request)
    try:
        cap = _money(body.get("max_spend"))
    except (TypeError, ValueError):
        return _bad("max_spend: the USD the run may spend on the classifier, 0 or more")
    state = _state()
    await state.init_db()
    if await classify.available(state, "contacts") in ("off", "task off"):
        return _bad("set a profile for the contacts task first (Settings → Classifier)", 409)
    chosen, error = await _verify_selection(state, body)
    if error:
        return _bad(error)
    if not chosen:
        return {"success": True, "queued": 0, "links": 0}
    try:
        batch_id, ids, dupes = await enqueue_contact_checks(state, chosen, max_spend=cap)
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    jobs.notify()
    return {"success": True, "batch_id": batch_id, "queued": len(ids),
            "already_waiting": len(dupes), "links": len(chosen), "max_spend": cap}


@app.post("/api/contacts/verify/undo")
async def undo_contact_verify(request: Request):
    """{"batch": id}: put back the trust a verification's no's lowered."""
    body = await _json_body(request)
    if not isinstance(body.get("batch"), str) or not body["batch"]:
        return _bad("batch: the verification run's id")
    state = _state()
    await state.init_db()
    return {"success": True, **await state.undo_contact_checks(body["batch"])}


@app.get("/api/contacts/verify/review")
async def contact_verify_review(verdict: str = "unsure", limit: int = 50):
    """The links the classifier gave ``verdict`` (unsure: for your review),
    newest first, still standing."""
    from harvey import contact_check

    if verdict not in contact_check.VERDICTS:
        return _bad("verdict: yes, no or unsure")
    state = _state()
    await state.init_db()
    return {"verdict": verdict,
            "links": await state.read("contact_checks_open", verdict, max(1, min(limit, 500)))}


@app.get("/api/contacts/verify/verdicts")
async def contact_verify_verdicts(ids: str = ""):
    """The classifier's newest verdict still standing on each of ``ids``
    (comma-separated contact ids, 200 at most): {"verdicts": {id: check}},
    a contact with none left out (the Contacts rows)."""
    try:
        wanted = list(dict.fromkeys(int(i) for i in ids.split(",") if i.strip()))[:200]
    except ValueError:
        return _bad("ids: contact ids, comma-separated")
    state = _state()
    await state.init_db()
    out = {}
    for cid in wanted:
        standing = [c for c in await state.read("contact_checks", cid) if not c["undone"]]
        if standing:
            out[str(cid)] = standing[0]
    return {"verdicts": out}


@app.get("/api/contacts/{contact_id}/checks")
async def get_contact_checks(contact_id: int):
    """The classifier's verdicts on a contact's links, newest first."""
    state = _state()
    await state.init_db()
    return {"checks": await state.read("contact_checks", contact_id)}


@app.get("/api/contacts/site-checks")
async def get_site_checks():
    """What checking the found websites would do (counted, nothing changed)."""
    from harvey import site_check

    if not DB_PATH.exists():
        return {"firms": 0}
    state = _state()
    await state.init_db()
    return await site_check.count(state)


@app.post("/api/contacts/site-checks")
async def start_site_checks():
    """Check every found website nothing checked yet: one batch, one job per
    firm (its id undoes what the check took off: harvey contacts rejudge
    --undo BATCH)."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_site_checks

    state = _state()
    await state.init_db()
    try:
        batch_id, ids, dupes = await enqueue_site_checks(state)
    except jobs.Duplicate as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=409)
    jobs.notify()
    return {"success": True, "batch_id": batch_id, "queued": len(ids), "waiting": len(dupes)}


@app.get("/api/contacts/{contact_id}")
async def get_contact_reach(contact_id: int):
    """Every live company a contact reaches, through whom and where."""
    if not DB_PATH.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _state().read("reach", contact_id)


async def _contact_export_rows(request: Request) -> tuple[list[dict], int]:
    """Every contact the filters match, once each (a shared phone is called
    once, not once per company), for an export: do-not-contact ones left
    out (an opt-out, an address on compliance.do_not_contact, and every
    contact of a company whose domain is on it: its phones too). ConfigError
    when the list can't be read."""
    state = _state()
    await state.init_db()
    f, attach = await _contact_filter(state, request.query_params)
    return await _overlay_contact_export(state, f, request)


async def _overlay_contact_export(state, f, request: Request) -> tuple[list[dict], int]:
    """_contact_export_rows on the overlay (the same suppression)."""
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    blocked = _blocked_entries(_load_config_quietly(), strict=True)
    rows = await state.read("contact_export", f, request.query_params.get("sort", ""),
                            request.query_params.get("dir", "desc"), CONTACT_EXPORT_MAX)
    # The do-not-contact list's entries (address, domain, a firm the address
    # reaches) as well as compliance.do_not_contact.
    suppressed = await state.suppressed_emails(
        [r["value"] for r in rows if r["kind"] == "email"])
    out = []
    for r in rows:
        if r["kind"] == "email" and (r["value"] in suppressed or (
                blocked and do_not_contact_match(r["value"], blocked))):
            continue
        if blocked and any(d and do_not_contact_domain_match(d, blocked) for d in r["domains"]):
            continue
        out.append({
            "kind": r["kind"], "value": r["value"], "display": r["display"] or r["value"],
            "line_type": r["line_type"], "status": r["status"], "trust": r.get("trust", ""),
            "companies": r.get("companies", 0),
            "company_names": "; ".join(r["company_names"][:10]),
            "people": "; ".join(r["people_names"][:10]),
            "sources": "; ".join(r.get("sources", [])),
            "first_seen": r["first_seen"], "last_seen": r["last_seen"]})
    return out, len(out)


CONTACT_EXPORT_COLUMNS = ["kind", "value", "display", "line_type", "status", "trust",
                          "companies", "company_names", "people", "sources", "first_seen",
                          "last_seen"]


@app.get("/api/export/contacts.csv")
async def export_contacts_csv(request: Request):
    """The filtered contacts as CSV (same filters as /api/contacts)."""
    from harvey.export import to_csv

    from harvey.config import ConfigError

    try:
        rows, _ = await _contact_export_rows(request)
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _csv_download(to_csv(rows, CONTACT_EXPORT_COLUMNS),
                         f"contacts-{date.today().isoformat()}.csv")


@app.get("/api/export/contacts.json")
async def export_contacts_json(request: Request):
    """The filtered contacts as JSON (same filters as /api/contacts)."""
    from harvey.config import ConfigError

    try:
        rows, _ = await _contact_export_rows(request)
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(rows, headers={
        "Content-Disposition": f'attachment; filename="contacts-{date.today().isoformat()}.json"'})


# What a reviewer needs next to a draft to check its claims against the facts.
OUTBOX_CONTEXT_SIGNALS = (
    "TECH_STACK", "INCUMBENT_AGENCY", "HIRING_ROLE", "RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING",
)


def _in(ids) -> str:
    return ",".join("?" for _ in ids)


async def _with_context(items: list[dict]) -> list[dict]:
    """Attach the campaign, company, contact and signals behind each draft."""
    if not items:
        return items
    if not DB_PATH.exists():
        return items
    return await _with_context_overlay(items)


async def _campaign_names(items: list[dict]) -> dict[str, str]:
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})
    return {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}


async def _with_context_overlay(items: list[dict]) -> list[dict]:
    """_with_context on the overlay: the company from its firm (the
    registers and Harvey's own), its signals from its current readings."""
    state = _state()
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    people = await state.read("prospects_by_id", pids) if pids else {}
    campaigns = await _campaign_names(items)
    lives = {}
    for p in people.values():
        if p.get("firm") is not None:
            live = await state.read("live", p["firm"])
            if live is not None:
                lives[p["id"]] = live
    companies = {int(r["id"]): r for r in await state.read("company_rows",
                                                            sorted(set(lives.values())))}
    labels = {v["code"]: v.get("label") or v["code"]
              for v in await state.read("signal_vocabulary")}
    signals: dict[int, dict[str, str]] = {}
    for live in companies:
        for r in await state.read("readings_of", str(live)):
            if r["signal_code"] not in OUTBOX_CONTEXT_SIGNALS:
                continue
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            label = labels.get(r["signal_code"], r["signal_code"])
            mine = signals.setdefault(live, {})
            values = [v for v in (mine.get(label) or "").split(", ") if v]
            if value and value not in values:
                mine[label] = ", ".join([*values, value])
    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        c = companies.get(lives.get(p.get("id"))) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": c.get("name", ""),
            "website": c.get("website") or (f"https://{c['domain']}" if c.get("domain") else ""),
            "description": c.get("description", ""),
            "found_by": c.get("industry", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(lives.get(p.get("id")), {}),
        }
    return items


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        # A send cut off mid-way shows up under Didn't send even when Harvey
        # isn't running.
        await state.fail_stale_sending_outbox()
        return {
            **_compliance_view(),
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_context(
                await state.get_outbox(status="pending_review", limit=100)),
            # Mail going out right now leads the list.
            "approved": (await state.get_outbox(status="sending", limit=10)
                         + await state.get_outbox(status="approved", limit=50)),
            "sent": (await query_db(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25")),
            "failed": (await query_db(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25")),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sending-mode")
async def get_sending_mode():
    """Approval (every email waits for you) or autopilot, and what that means here."""
    from harvey.config import load_config
    from harvey.gate import _booking_prefix, allowed_link_domains
    path = _resolve_config_file()
    try:
        config = load_config(str(path))
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read {path.name}: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    return {
        "mode": "approval" if config.channels.email.require_approval else "autopilot",
        "provider": config.channels.email.provider,
        "file": path.name,
        "your_domains": sorted(allowed_link_domains(config)),
        "booking_page": _booking_prefix(config),
        "held": await state.count_held_for_review(),
    }


@app.post("/api/sending-mode")
async def set_sending_mode(request: Request):
    """Switch it. Autopilot needs {"mode": "autopilot", "confirm": "autopilot"};
    {"mode": "approval"} works at once and sends back to the Outbox whatever
    autopilot approved that hasn't gone out."""
    from harvey.config import ConfigError, set_require_approval
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and body.get("confirm") != "autopilot":
        return _refuse('Type "autopilot" to confirm: Harvey will send email without asking you.', 400)
    try:
        path = set_require_approval(mode == "approval", _resolve_config_file())
    except ConfigError as e:
        return _refuse(str(e), 409)
    state = _state()
    await state.init_db()
    requeued = await state.requeue_autopilot_approved() if mode == "approval" else 0
    await state.log_action("sending_mode", "dashboard", {"mode": mode, "file": path.name,
                                                         "requeued": requeued})
    out = {"success": True, "mode": mode}
    if mode == "approval":
        out["requeued"] = requeued
    return out


@app.post("/api/outbox/approve-all")
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox()
        # Held ones (possible planted link or leak) are approved one by one.
        return {"success": True, "approved": n, "held": await state.count_held_for_review()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/edit")
async def outbox_edit(item_id: str, request: Request):
    """Rewrite a draft awaiting review. Never approves it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "").strip()
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") != "pending_review":
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        if item.get("kind") == "reply":
            # Gmail threads a reply by its subject: a new one would land as a
            # fresh, unthreaded email to someone who wrote in. Only the body
            # of a reply is editable; its stored subject is kept exactly.
            subject = item.get("subject") or ""
        if not subject.strip() or not text:
            return JSONResponse({"success": False,
                                 "message": "Subject and body are both required."},
                                status_code=400)
        # The sender re-checks every email before it goes out; an edit it
        # would block is refused now, while the reviewer can still fix it,
        # instead of failing quietly at send time. Content rules only: the
        # recipient isn't something an edit can change.
        from harvey.gate import pre_send_check
        checked = text
        footer = _compliance_view()["footer"]
        if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
            checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
        gate = pre_send_check("reviewer@example.com", subject, checked,
                              acknowledged=True, personal=False)
        if not gate:
            return JSONResponse({"success": False,
                                 "message": "The sender would have blocked this: "
                                            + "; ".join(gate.reasons)},
                                status_code=400)
        if not await state.edit_outbox_draft(item_id, subject, text):
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": item_id, "to_email": item.get("to_email", ""),
            "old_subject": item.get("subject", ""), "old_body": item.get("body", ""),
        })
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# A hand send is only offered to someone Harvey could still email: never to
# someone who replied, opted out, bounced or was already contacted.
HAND_SENDABLE_STATUSES = ("new", "queued")
HAND_SENDABLE_EMAIL = ("verified", "risky")


def _harvey_can_send() -> bool:
    """Can Harvey send email itself (a connected mailbox)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from harvey.config import load_config, load_env
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        email = config.channels.email
        if not email.enabled or (email.provider or "").strip().lower() not in NATIVE_PROVIDERS:
            return False
        provider = get_mail_provider(config, env)
        return bool(provider and provider.is_configured())
    except Exception:
        return True


def _send_as() -> str:
    """The address Harvey sends as, so Gmail opens the right account."""
    try:
        from harvey.config import load_config
        return load_config().persona.email or ""
    except Exception:
        return ""


def _compliance_view() -> dict:
    """What the Sender adds under each cold email, so a reviewer sees the
    email as it will leave, and the warning when the address is missing."""
    try:
        from harvey.config import compliance_footer_lines, compliance_notice, load_config
        config = load_config()
        return {"footer": "\n".join(compliance_footer_lines(config)),
                "compliance_notice": compliance_notice(config)}
    except Exception:
        return {"footer": "", "compliance_notice": ""}


async def _harvey_committed_ids() -> set[str]:
    """Everyone Harvey has approved or sent mail for (the Outbox, or an
    Instantly campaign an earlier release deployed).

    Approved mail can be going out at this moment, so these people are past
    the point where a hand send can be recorded safely. So is anyone whose
    send was cut off mid-way: it may have reached them. A draft that only
    awaits review isn't: marking the person sent cancels it.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    ids = {r["prospect_id"] for r in await query_db(
        "SELECT DISTINCT prospect_id FROM outbox WHERE status IN ('approved', 'sending', 'sent') "
        "OR (status = 'failed' AND substr(error, 1, ?) = ?)",
        (len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX))}
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            found = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(found, list):
            ids.update(i for i in found if isinstance(i, str))
    return ids


async def _hand_sent_ids() -> set[str]:
    from harvey.state import MANUAL_SEND_KEY
    rows = await query_db("SELECT key FROM settings WHERE key LIKE ?", (MANUAL_SEND_KEY + "%",))
    return {r["key"][len(MANUAL_SEND_KEY):] for r in rows}


async def _harvey_sent_to(prospect_id: str) -> bool:
    """Did Harvey itself email this person (the Outbox, or an Instantly
    campaign an earlier release deployed)?

    A send in flight, or cut off mid-way, counts: it may have reached them.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    if await query_db(
            "SELECT 1 FROM outbox WHERE prospect_id = ? AND (status IN ('sending', 'sent') "
            "OR (status = 'failed' AND substr(error, 1, ?) = ?)) LIMIT 1",
            (prospect_id, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX)):
        return True
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            if prospect_id in json.loads(row.get("prospect_ids_json") or "[]"):
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


@app.post("/api/prospects/{prospect_id}/sent")
async def mark_prospect_sent(prospect_id: str, request: Request):
    """The user sent this email by hand (e.g. from Gmail): record it, or undo it."""
    from harvey.state import HAND_SENT_REASON, MANUAL_SEND_KEY
    try:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("sent", True), bool):
            return _refuse('expected {"sent": true|false}', 400)
        sent = body.get("sent", True)
        rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
        if not rows:
            return _refuse("unknown prospect", 404)
        current = rows[0].get("status") or "new"
        state = _state()
        await state.init_db()
        key = MANUAL_SEND_KEY + prospect_id
        marked = bool(await state.get_setting(key))

        if sent:
            if marked:
                return {"success": True, "sent": True, "already": True}
            if current not in HAND_SENDABLE_STATUSES:
                # A reply, opt-out or close is never touched; neither is
                # someone Harvey already emailed.
                return {"success": True, "sent": True, "status": current}
            # Allowed even when Harvey can send: recording "I already emailed
            # them" can only ever stop Harvey sending. Not once Harvey has
            # approved or sent mail to them, though: that may be going out now.
            # The marker goes down FIRST: a drain that checks after this sees
            # it and cancels; one that checked before only holds an approved
            # row, which the check below then sees.
            await state.set_setting(key, current)
            if prospect_id in await _harvey_committed_ids():
                await state.delete_setting(key)
                return _refuse("Harvey has already approved or sent an email to this person. "
                               "Check the Outbox.")
            await state.update_prospect_status(prospect_id, "contacted")
            # Anything Harvey had queued for them must not go out as well.
            changed = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=HAND_SENT_REASON)
        else:
            if not marked:
                if current in HAND_SENDABLE_STATUSES:
                    return {"success": True, "sent": False}
                if current == "contacted":
                    # Setting them back would make Harvey write and send again.
                    return _refuse("This wasn't marked as sent by hand here, so it stays sent.")
                return {"success": True, "sent": True, "status": current}
            if current != "contacted":
                # They replied, opted out or bounced since: that stands.
                return {"success": True, "sent": True, "status": current}
            previous = await state.get_setting(key) or "queued"
            await state.update_prospect_status(
                prospect_id, previous if previous in HAND_SENDABLE_STATUSES else "queued")
            await state.delete_setting(key)
            # What marking sent cancelled comes back for review, never straight out.
            changed = await state.restore_hand_cancelled_outbox(
                prospect_id, reason=HAND_SENT_REASON)
        if sent:
            # Open in Gmail adds today's footer; remember it as Harvey's text.
            footer = _compliance_view()["footer"]
            await state.remember_footer_lines(footer.split("\n"))
        await state.log_action(
            action_type="manual_send" if sent else "manual_send_undone",
            agent="user",
            details={"prospect_id": prospect_id, "outbox_items": changed},
        )
        return {"success": True, "sent": sent}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await state.reject_outbox_item(item_id):
            return _refuse("That email is already sending or sent, so it can't be rejected.")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/sending/status")
async def sending_status():
    """The kill switch, the sending mode and what is queued: read only, so
    the dashboard can show it (and offer the pause) with or without the
    Email module's tabs."""
    return await _sending_summary(_load_config_quietly())


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = "",
                           bom: bool = False):
    """Sequencer-ready CSV download of the prospect list. ``bom`` prefixes a
    UTF-8 byte order mark (the dashboard asks for it, for Excel)."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    state = StateManager(db_path=str(DB_PATH))
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        (CSV_BOM if bom else "") + text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


EXPORT_MAX_IDS = 5000
EXPORT_SEARCH_MAX = 100000


async def _export_request(request: Request) -> tuple[list[str] | None, str]:
    """{"ids": [...]}, {"q": "..."}, {"filter": {the Companies page's
    filters}, "sort", "dir"} (its list, as ids) or none of them (everything)
    from an export POST. Raises ValueError on a malformed body."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    ids = body.get("ids")
    if ids is not None:
        if (not isinstance(ids, list) or len(ids) > EXPORT_MAX_IDS
                or any(not isinstance(i, str) or len(i) > 128 for i in ids)):
            raise ValueError(f"ids must be a list of at most {EXPORT_MAX_IDS} ids")
        ids = list(dict.fromkeys(ids))
    q = body.get("q") or ""
    if not isinstance(q, str):
        raise ValueError("q must be text")
    if ids is None and isinstance(body.get("filter"), dict):
        # The Companies page's filters and sort: every result, in its order
        # (how many matched in all is kept for the export's header).
        ids, request.state.matched = await _company_filter_ids(
            body["filter"], str(body.get("sort") or ""), str(body.get("dir") or ""),
            EXPORT_SEARCH_MAX)
    return ids, q[:200]


async def _company_filter_ids(raw: dict, sort: str, way: str, limit: int) -> tuple[list[str], int]:
    """(the ids of the Companies page's list for these filters in its sort,
    up to ``limit``; how many match in all). ValueError on a bad filter."""
    from harvey import companies

    state = _state()
    await state.init_db()
    f, attach = await _contact_filter(state, {k: str(v) for k, v in raw.items() if v is not None})
    if (f.words or f.search) and sort in ("", "relevance"):
        sort = "relevance"
    elif sort not in companies.SORTS:
        sort = "created"
    return await state.read("ids_for", f, sort, companies.direction(sort, way), limit)


async def _search_all_ids(state, q: str) -> tuple[list[str], int]:
    """Every company id a search matches, best first, and how many matched
    in all (more than returned when the export cap cuts it short)."""
    ids: list[str] = []
    total, offset = None, 0
    while total is None or (offset < total and offset < EXPORT_SEARCH_MAX):
        total, found = await _company_search(state, q, limit=500, offset=offset)
        if not found:
            break
        ids += [r["id"] for r in found]
        offset += len(found)
    return ids[:EXPORT_SEARCH_MAX], total or 0


async def _export_company_rows(request: Request) -> tuple[list[dict], int]:
    """(rows, how many matched): the rows to export, and the full count
    when a search export was cut at EXPORT_SEARCH_MAX."""
    from harvey.export import collect_companies

    ids, q = await _export_request(request)
    state = _state()
    await state.init_db()
    config = _load_config_quietly()
    if ids is None and q.strip():
        # Every match of the search, in the order the search ranks them.
        ids, matched = await _search_all_ids(state, q)
        rank = {cid: i for i, cid in enumerate(ids)}
        rows = await collect_companies(state, config=config, ids=ids)
        rows = sorted(rows, key=lambda r: rank.get(r["id"], len(rank)))
    else:
        rows = await collect_companies(state, config=config, ids=ids)
        if ids:
            # In the order asked for (a filtered page's sort).
            rank = {cid: i for i, cid in enumerate(ids)}
            rows.sort(key=lambda r: rank.get(r["id"], len(rank)))
        matched = getattr(request.state, "matched", None) or len(rows)
    # (Read with the rows; a row without them is asked for on its own.)
    activities = {r["id"]: r.pop("_activities") for r in rows if "_activities" in r}
    missing = [r["id"] for r in rows if r["id"] not in activities]
    if missing:
        activities.update(await state.get_company_activities(missing))
    for r in rows:
        acts = activities.get(r["id"], [])
        seen, listed = set(), []
        for a in acts:                       # each class once, newest revision first
            if a["code"] not in seen:
                seen.add(a["code"])
                listed.append({"code": a["code"], "name": a.get("name") or a.get("name_en") or ""})
        r["activity_list"] = listed
        r["activities"] = "; ".join(f"{a['code']} {a['name']}".strip() for a in listed)
    from harvey.export import latest_year_figures
    codes = await _figure_codes(state)
    figures = await latest_year_figures(state, [r["id"] for r in rows], codes)
    for r in rows:
        year, values = figures.get(r["id"], ("", {}))
        for code in codes:
            value = values.get(code)
            r[code.lower()] = "" if value is None else (
                int(value) if float(value).is_integer() else value)
        r["financial_year"] = year
    return rows, max(matched, len(rows))


async def _figure_codes(state) -> list[str]:
    """The yearly figures the modules that are on record (a financial
    statements module's turnover, headcount, ...): export columns."""
    from harvey.export import yearly_codes
    from harvey.modules import switches

    return yearly_codes(set(await switches.enabled_ids(state)))


# The dashboard's company CSV: the CLI's columns, then the registry identity,
# the activities, and the latest year's figures from the modules that are on
# (all from one year, named in financial_year).
def _dashboard_company_columns(codes=()) -> list[str]:
    from harvey.export import COMPANY_CSV_COLUMNS
    figures = [c.lower() for c in codes]
    return [*COMPANY_CSV_COLUMNS, "cui", "address", "activities", *figures,
            *(["financial_year"] if figures else [])]


# Downloads from the dashboard open in a spreadsheet: a UTF-8 byte order
# mark lets Excel read ș, ț and ă right. The CLI's files are unchanged.
CSV_BOM = "\ufeff"


def _csv_download(text: str, filename: str, extra: dict | None = None) -> PlainTextResponse:
    return PlainTextResponse(
        CSV_BOM + text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', **(extra or {})})


def _truncated_header(rows: list[dict], matched: int) -> dict:
    return {"X-Export-Matched": str(matched), "X-Export-Rows": str(len(rows))}


@app.post("/api/export/companies.csv")
async def export_companies_csv_api(request: Request):
    """Companies as CSV (the `harvey export --companies` columns plus cui,
    address and activities): the ids given, every match of a search ``q``,
    or all of them. Cells go through the export's formula guard."""
    from harvey.export import to_csv
    try:
        rows, matched = await _export_company_rows(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Company export failed: %s", e)
        return JSONResponse({"error": "Company export failed"}, status_code=500)
    columns = _dashboard_company_columns(await _figure_codes(_state()))
    return _csv_download(to_csv(rows, columns), "companies.csv",
                         _truncated_header(rows, matched))


@app.post("/api/export/companies.json")
async def export_companies_json_api(request: Request):
    """The same companies as JSON, with the id, CUI, address and activities."""
    from harvey.export import COMPANY_CSV_COLUMNS
    try:
        rows, matched = await _export_company_rows(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Company export failed: %s", e)
        return JSONResponse({"error": "Company export failed"}, status_code=500)
    from harvey.export import source_notes
    figures = [c.lower() for c in await _figure_codes(_state())]
    keys = ("id", "cui", *COMPANY_CSV_COLUMNS, "address", *figures,
            *(["financial_year"] if figures else []))
    return JSONResponse(
        {"companies": [{**{k: r.get(k, "") for k in keys}, "activities": r.get("activity_list", [])}
                       for r in rows],
         "count": len(rows), "matched": matched,
         # The credit each source's data needs (an open licence's attribution).
         "notes": await source_notes(_state(), [r["id"] for r in rows])},
        headers={"Content-Disposition": 'attachment; filename="companies.json"',
                 **_truncated_header(rows, matched)})


@app.post("/api/export/prospects.csv")
async def export_selected_prospects(request: Request):
    """The contacts picked in the dashboard, as the prospect CSV. Opted-out
    contacts and addresses on compliance.do_not_contact stay out, as in every
    outreach export; no deliverability filter (they were chosen by hand),
    but a bounced address is never handed out: that row goes without it."""
    from harvey.export import CSV_COLUMNS, collect_prospects, to_csv
    try:
        ids, _ = await _export_request(request)
        if ids is None:
            raise ValueError("ids are required")
        state = _state()
        await state.init_db()
        rows = await collect_prospects(state, email_statuses=[], ids=ids,
                                       config=_load_config_quietly())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        return JSONResponse({"error": "Prospect export failed"}, status_code=500)
    for r in rows:
        if r.get("email_status") == "invalid":
            r["email"] = ""
    return _csv_download(to_csv(rows, CSV_COLUMNS), "contacts.csv")


# ── Modules: what Harvey is made of, and which parts are on ──
#
# The Email switch is the email_outreach module's. Off, the dashboard hides
# its tabs and the heartbeat writes, sends and reads no mail at all.

UI_MODULES = {"email": "email_outreach"}


async def _modules() -> dict:
    from harvey.modules import switches

    state = _state()
    await state.init_db()
    on = await switches.enabled_ids(state)
    return {name: module_id in on for name, module_id in UI_MODULES.items()}


@app.get("/api/ui/modules")
async def get_ui_modules():
    """Which optional dashboard modules are shown (all off by default)."""
    try:
        return {"modules": await _modules()}
    except Exception as e:
        logger.warning("modules read failed: %s", e)
        return {"modules": dict.fromkeys(UI_MODULES, False)}


@app.post("/api/ui/modules")
async def set_ui_modules(request: Request):
    """{"email": true|false}: show or hide a module's tabs. Visibility only."""
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    if (not isinstance(body, dict) or not body
            or any(k not in UI_MODULES or not isinstance(v, bool) for k, v in body.items())):
        return JSONResponse({"success": False,
                             "message": 'expected {"email": true} or {"email": false}'},
                            status_code=400)
    state = _state()
    await state.init_db()
    try:
        for name, on in body.items():
            await switches.set_enabled(state, UI_MODULES[name], on)
    except switches.SwitchRefused as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=409)
    return {"success": True, "modules": await _modules()}


def _module_row(m, on: set[str], present: set[str], labels: dict[str, str],
                names: dict[str, str]) -> dict:
    cost = m.cost or {}
    return {
        "id": m.id, "name": m.name or m.id, "description": m.description,
        "set": m.set, "kind": m.kind, "version": m.version,
        "enabled": m.id in on, "locked": m.set == "core", "data_only": m.data_only,
        "requires": [{"id": r, "name": names.get(r, r)} for r in m.requires],
        "signals": [{"code": sig["code"], "label": labels.get(sig["code"], sig.get("label", ""))}
                    for sig in m.signals],
        # Presence only: a value never leaves the box.
        "secrets": [{"key": k, "present": k in present} for k in m.secrets],
        "cost": {"note": str(cost.get("note", "")), "free_tier": str(cost.get("free_tier", "")),
                 "as_of": str(cost.get("as_of", ""))},
        "terms_note": m.terms_note,
    }


async def _modules_page() -> dict:
    from harvey.modules import switches

    state = _state()
    await state.init_db()
    on = await switches.enabled_ids(state)
    present = _present_env_keys()
    labels = {r["code"]: r["label"] for r in await state.get_signal_codes()}
    names = {m.id: m.name or m.id for m in switches.manifests()}
    sets: dict[str, list] = {}
    for m in switches.manifests():
        sets.setdefault(m.set, []).append(_module_row(m, on, present, labels, names))
    return {"sets": [{"key": k, "modules": v} for k, v in sets.items()],
            # Packs in data/modules left out, one line each saying why.
            "pack_problems": switches.pack_problems()}


@app.get("/api/modules")
async def get_modules():
    """Every module, grouped by set: what it is, the signals it adds, the
    .env keys it needs (present or not, never the values), its cost and
    terms notes, and whether it is on."""
    try:
        return await _modules_page()
    except Exception as e:
        logger.exception("modules page failed")
        return {"sets": [], "error": str(e)}


@app.post("/api/modules/{module_id}")
async def set_module(module_id: str, request: Request):
    """{"enabled": true|false}. Refused, with the reason, for a core module,
    for one whose requirements are off, and for one another module needs."""
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return JSONResponse({"success": False, "message": 'expected {"enabled": true|false}'},
                            status_code=400)
    if module_id not in switches.by_id():
        return JSONResponse({"success": False, "message": f"no module {module_id!r}"},
                            status_code=404)
    state = _state()
    await state.init_db()
    try:
        await switches.set_enabled(state, module_id, body["enabled"])
    except switches.SwitchRefused as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=409)
    return {"success": True, **await _modules_page()}


@app.get("/api/overview")
async def get_overview():
    """The landing page's counts: companies, how many have a phone or an
    email on file (counted as the Companies table shows them: no bounced,
    opted-out or do-not-contact address), contacts and named people, plus
    whether email can go out right now (``sending``), so the page can say so
    even while the Email module's tabs are hidden."""
    if not DB_PATH.exists():
        await _state().init_db()       # a new install: the empty database
    return await _overlay_overview()


async def _overlay_overview() -> dict:
    """get_overview on the overlay: the firm counts from overlay.reads (the
    register's when counted for its load mark), the rest from the outreach tables."""
    config = _load_config_quietly()
    state = _state()
    await state.init_db()
    found = await state.read("overview", _blocked_entries(config))
    people = found["people"]
    return {"companies": found["companies"], "with_phone": found["with_phone"],
            "with_email": found["with_email"], "with_both": found["with_both"],
            "contacts": sum((await state.count_prospects_by_status()).values()),
            "people": people, "with_activity": found["with_activity"],
            "counting": found.get("counting", False),
            "sending": await _sending_summary(config)}


async def _sending_summary(config) -> dict:
    """What email will do with nobody watching: the kill switch, the mode
    (autopilot sends without asking), and what is queued. Read only."""
    paused = ""
    try:
        state = _state()
        await state.init_db()
        paused = await state.get_setting("sending_paused") or ""
    except Exception as e:
        logger.warning("sending state couldn't be read: %s", e)
    autopilot = None
    try:
        if config is not None:
            autopilot = not config.channels.email.require_approval
    except Exception:
        autopilot = None
    queued = await _scalar("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('approved', 'sending')")
    waiting = await _scalar("SELECT COUNT(*) AS n FROM outbox WHERE status = 'pending_review'")
    return {"paused": paused, "autopilot": autopilot, "queued": queued, "waiting": waiting,
            # Something can go out without anyone pressing a button.
            "live": not paused and (bool(autopilot) or queued > 0)}


@app.get("/api/campaigns")
async def get_campaigns():
    """Every campaign (the newest 100), as before, plus each one's members
    by status, emails sent, replies, origin, audience and note. Its
    recipients are its members (and, for a campaign from before members,
    its prospect list)."""
    from harvey import campaigns

    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    if rows:
        state = await _campaign_state()
        extra = {c["id"]: c for c in await state.campaigns_call(campaigns.list_campaigns,
                                                                 write=False)}
        member_ids = {}
        for r in await query_db("SELECT campaign, prospect_id FROM campaign_member "
                                "WHERE prospect_id != ''"):
            member_ids.setdefault(r["campaign"], []).append(r["prospect_id"])
    harvey_sends = _harvey_can_send()
    send_as = "" if harvey_sends else _send_as()
    footer = _compliance_view()["footer"]
    committed = await _harvey_committed_ids()
    hand_sent = await _hand_sent_ids()
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
        row["prospect_ids"] = list(dict.fromkeys(row["prospect_ids"]
                                                 + member_ids.get(row["id"], [])))
        info = extra.get(row["id"], {})
        for key in ("members", "member_total", "sent", "replies", "offer", "rolling"):
            row[key] = info.get(key)
        # Who each draft goes to, so the UI can open it in the user's own mail client.
        ids = row["prospect_ids"]
        row["recipients"] = await query_db(
            f"SELECT id, email, first_name, last_name, company, title, status, email_status, "
            f"source, personalization_notes FROM prospects "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND email != ''",
            tuple(ids),
        ) if ids else []
        row["send_as"] = send_as
        # A cold email sent by hand needs the same footer Harvey adds.
        row["footer"] = footer
        for r in row["recipients"]:
            r["sent_by_hand"] = r["id"] in hand_sent
            # "I already emailed them": always recordable before Harvey commits.
            r["can_mark_sent"] = (
                r["id"] not in hand_sent and r["id"] not in committed
                and (r.get("status") or "new") in HAND_SENDABLE_STATUSES
            )
            # The Gmail button: only when Harvey can't send itself.
            r["can_send_by_hand"] = (
                r["can_mark_sent"] and not harvey_sends
                and (r.get("email_status") or "guess") in HAND_SENDABLE_EMAIL
            )
    return rows


@app.get("/api/prospects/{prospect_id}/opt-out")
async def get_opt_out(prospect_id: str):
    """Why this person is opted out: the rule, the words and their message."""
    rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    status = rows[0].get("status") or "new"
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), None)
    out = {
        "opted_out": status == "opted_out", "status": status, "recorded": bool(last),
        "at": None, "rule": None, "matched": "", "why": "", "message": "",
        "conversation_id": "", "history": history,
    }
    if last:
        out.update({k: last.get(k) or out[k] for k in
                    ("rule", "matched", "why", "message", "conversation_id")})
        out["at"] = last["at"]
    elif status == "opted_out" or history:
        # Opted out before Harvey kept reasons (and maybe undone since):
        # show the last thing they wrote.
        for c in await query_db(
                "SELECT id, thread_json, updated_at FROM conversations "
                "WHERE prospect_id = ? ORDER BY updated_at DESC", (prospect_id,)):
            try:
                thread = json.loads(c.get("thread_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            if theirs:
                out.update(message=str(theirs[-1].get("content") or ""),
                           conversation_id=c["id"], at=theirs[-1].get("timestamp") or c.get("updated_at"))
                break
    return out


@app.post("/api/prospects/{prospect_id}/opt-out/undo")
async def undo_opt_out(prospect_id: str, request: Request):
    """The owner says this opt-out was a mistake. Needs {"confirm": true}."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refuse('expected {"confirm": true}', 400)
    rows = await query_db("SELECT email FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), {})
    from harvey import suppression
    try:
        undone = await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")})
    except suppression.Refused as e:
        # An unsubscribe, objection or complaint: undone on the list, with
        # a note (POST /api/suppression/{id}/undo), not with one click.
        return JSONResponse({"success": False, "message": str(e), "code": "optout_listed"},
                            status_code=409)
    if not undone:
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


# ── The cold-email acknowledgement and the do-not-contact list ──

@app.get("/api/acknowledgement")
async def get_acknowledgement():
    """The cold-email acknowledgement's text (Romanian and English), its
    version and whether it was confirmed. Cold email waits until it is."""
    state = _state()
    await state.init_db()
    return await state.acknowledgement_status()


@app.post("/api/acknowledgement")
async def post_acknowledgement(request: Request):
    """Confirm it: {"typed": "accept"}, the word typed by the operator."""
    from harvey import acknowledgement

    try:
        body = await request.json()
    except ValueError:
        body = None
    typed = str((body or {}).get("typed") or "").strip().lower() if isinstance(body, dict) else ""
    if typed != acknowledgement.CONFIRM_WORD:
        return _refuse(f'expected {{"typed": "{acknowledgement.CONFIRM_WORD}"}}', 400)
    state = _state()
    await state.init_db()
    await state.acknowledge("dashboard")
    return {"success": True, **await state.acknowledgement_status()}


@app.post("/api/mail/read-gap/accept")
async def accept_read_gap(request: Request):
    """Let cold email go after a mailbox read that couldn't reach back to
    the last complete one: {"typed": "accept"}. An unsubscribe or reply
    sent in that time may stay unread; the acceptance is logged."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    typed = str((body or {}).get("typed") or "").strip().lower() if isinstance(body, dict) else ""
    if typed != "accept":
        return _refuse('expected {"typed": "accept"}', 400)
    state = _state()
    await state.init_db()
    if not await state.accept_read_gap("dashboard"):
        return _refuse("Cold email isn't waiting for a mailbox read.", 404)
    return {"success": True}


@app.get("/api/suppression")
async def get_suppression(all: bool = False):
    """The do-not-contact list: the entries in force (``all``: undone too)."""
    state = _state()
    await state.init_db()
    rows = await state.suppression_entries(live_only=not all)
    return {"rows": rows, "total": len(rows)}


@app.get("/api/suppression.csv")
async def get_suppression_csv(all: bool = False):
    """The list as CSV, to keep beside an export: an export is what was
    true when it was made, the list is what holds from now on."""
    import csv
    import io

    state = _state()
    await state.init_db()
    rows = await state.suppression_entries(live_only=not all)
    buf = io.StringIO()
    out = csv.DictWriter(buf, fieldnames=["id", "scope", "value", "reason", "at", "by",
                                          "undone_at", "undone_by"], extrasaction="ignore")
    out.writeheader()
    out.writerows(rows)
    return _csv_download(buf.getvalue(), "nu-contacta.csv")


@app.post("/api/suppression")
async def add_suppression(request: Request):
    """Never email an address, a domain or a firm again: {"scope": "address"
    | "domain" | "firm", "value": ..., "note": ...} (a firm's value is its
    company id)."""
    from harvey import suppression

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("scope") not in ("address", "domain", "firm"):
        return _refuse('expected {"scope": "address" | "domain" | "firm", "value": ...}', 400)
    state = _state()
    await state.init_db()
    try:
        made = await state.suppress(body["scope"], body.get("value") or "", "by_hand",
                                    by="you", note=str(body.get("note") or ""))
    except suppression.Refused as e:
        return _refuse(str(e), 400)
    return {"success": True, "added": made is not None, "id": made}


@app.post("/api/suppression/{entry_id}/undo")
async def undo_suppression(entry_id: int, request: Request):
    """Undo an entry (soft: its history stays). One the person asked for
    (an unsubscribe, an opt-out, an objection) needs {"typed": "confirm",
    "note": why}; one you added needs nothing."""
    from harvey import suppression

    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    state = _state()
    await state.init_db()
    entry = next((e for e in await state.suppression_entries() if e["id"] == entry_id), None)
    if entry is None:
        return _refuse("no entry in force with that id", 404)
    asked = {suppression.REASON_NAMES[r] for r in suppression.PERSON_ASKED}
    note = str(body.get("note") or "").strip()
    if entry["reason"] in asked and (str(body.get("typed") or "").strip().lower() != "confirm"
                                     or not note):
        return _refuse('they asked not to be contacted: expected {"typed": "confirm", '
                       '"note": why}', 400)
    await state.undo_suppression(entry_id, by="you", note=note)
    return {"success": True}


# ── Audiences and campaigns made by the user (harvey/campaigns.py) ──

def _campaign_config():
    """The settings a campaign action reads (the provider, send_to_risky,
    the do-not-contact list), or a refusal when they can't be read."""
    config = _load_config_quietly()
    if config is None:
        raise ValueError("harvey.local.yaml can't be read: fix it first")
    return config


def _campaign_refusal(e) -> JSONResponse:
    """A campaigns.Refused (or a bad filter) as the page reads it: 409 with
    ``need`` when something must be done first (the acknowledgement, the
    note, a native provider), else 400."""
    need = getattr(e, "need", "")
    body = {"success": False, "message": str(e)}
    if need:
        body["need"] = need
    return JSONResponse(body, status_code=409 if need else 400)


async def _campaign_state():
    state = _state()
    await state.init_db()
    return state


@app.get("/api/audiences")
async def list_audiences(archived: bool = False):
    """The saved audiences (a Contacts filter each), with how many
    campaigns name each."""
    from harvey import campaigns

    state = await _campaign_state()
    return {"audiences": await state.campaigns_call(campaigns.audiences, archived, write=False)}


@app.post("/api/audiences")
async def save_audience(request: Request):
    """Save the Contacts page's current filter: {"name", "query"} (its query
    string; kind is always email)."""
    from harvey import campaigns, launch

    body = await _json_body(request)
    state = await _campaign_state()
    try:
        audience_id = await launch.save_audience(state, str(body.get("name") or ""),
                                                 str(body.get("query") or ""))
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)
    except Exception as e:
        return _campaign_refusal(ValueError(str(e)))
    return {"success": True, "id": audience_id,
            "audience": await state.campaigns_call(campaigns.get_audience, audience_id,
                                                   write=False)}


@app.patch("/api/audiences/{audience_id}")
async def edit_audience(audience_id: int, request: Request):
    """Rename ({"name"}) or archive ({"archived": true}) an audience."""
    from harvey import campaigns

    body = await _json_body(request)
    state = await _campaign_state()
    try:
        changed = await state.campaigns_call(
            campaigns.update_audience, audience_id, name=body.get("name"),
            archived=body.get("archived") if isinstance(body.get("archived"), bool) else None)
    except campaigns.Refused as e:
        return _campaign_refusal(e)
    if not changed:
        return _refuse("no such audience, or nothing to change", 404)
    return {"success": True}


async def _preview_counts(state, query: str = "", emails=None, campaign_id: str = ""):
    from harvey import campaigns

    return await campaigns.preview(state, _campaign_config(), query=query, emails=emails,
                                   campaign_id=campaign_id)


@app.get("/api/audiences/{audience_id}/preview")
async def preview_audience(audience_id: int):
    """What a campaign would take from this audience, counted: ready,
    catch-all, to check (a verifier credit each), left out (invalid or a
    guess, untrusted, do-not-contact, in another campaign, answered),
    personal addresses by reason, firms, days at today's cap."""
    from harvey import campaigns

    state = await _campaign_state()
    audience = await state.campaigns_call(campaigns.get_audience, audience_id, write=False)
    if audience is None:
        return _refuse("no such audience", 404)
    try:
        return {"audience": audience, "counts": await _preview_counts(state, audience["query"])}
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)


@app.post("/api/audiences/preview")
async def preview_query(request: Request):
    """The same counts for a filter not saved yet: {"query"} (the Contacts
    page's current filter)."""
    from harvey import campaigns

    body = await _json_body(request)
    state = await _campaign_state()
    try:
        return {"counts": await _preview_counts(state, str(body.get("query") or ""))}
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)


@app.get("/api/campaigns/setup")
async def campaign_setup():
    """What the new-campaign wizard needs: whether new campaigns can be made
    (a native provider) and why not, the sending accounts, the saved
    audiences, the acknowledgement's state, the note's template and
    minimum, and the product as the settings describe it (the offer's
    default)."""
    from harvey import campaigns
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS

    config = _load_config_quietly()
    state = await _campaign_state()
    provider = (getattr(getattr(getattr(config, "channels", None), "email", None),
                        "provider", "") or "").lower()
    if config is not None:
        await state.sync_sending_account(config)
    product = getattr(config, "product", None)
    return {
        "native": provider in NATIVE_PROVIDERS, "provider": provider,
        "refusal": "" if provider in NATIVE_PROVIDERS else campaigns.NATIVE_ONLY,
        "accounts": await state.campaigns_call(campaigns.accounts, write=False),
        "audiences": await state.campaigns_call(campaigns.audiences, write=False),
        "acknowledged": await state.acknowledged(),
        "li_note_min": campaigns.LI_NOTE_MIN,
        "li_note_template": campaigns.li_note_template(getattr(product, "name", "") or ""),
        "offer": {"offer": getattr(product, "description", "") or ""},
        "max_daily_sends": getattr(getattr(getattr(config, "channels", None), "email", None),
                                   "max_daily_sends", 0) or 0,
    }


@app.post("/api/campaigns")
async def create_campaign(request: Request):
    """A new draft campaign: {"name", "audience_id" or "query", "rolling",
    "offer": {offer, goal, booking_link}, "sequence": [{subject, body,
    delay_days}], "li_note", "review": "inherit" | "always"}. Refused
    (409, need native_provider) unless the provider is Gmail, SMTP or
    Microsoft 365."""
    from harvey import campaigns, launch

    body = await _json_body(request)
    try:
        config = _campaign_config()
        state = await _campaign_state()
        campaign_id = await launch.new_campaign(state, config, body)
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)
    return {"success": True, "id": campaign_id,
            "campaign": await state.campaign_record(campaign_id)}


@app.get("/api/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: str, status: str = "", limit: int = 200,
                          offset: int = 0):
    """One campaign: its record, members by status, a page of members
    (``status`` filters), its note and frozen filter."""
    from harvey import campaigns

    state = await _campaign_state()
    c = next((x for x in await state.campaigns_call(campaigns.list_campaigns, write=False)
              if x["id"] == campaign_id), None)
    if c is None:
        return _refuse("no such campaign", 404)
    try:
        rows = await state.campaigns_call(campaigns.members, campaign_id, status,
                                          max(1, min(int(limit), 500)), max(0, int(offset)),
                                          write=False)
    except campaigns.Refused as e:
        return _campaign_refusal(e)
    c["query"] = await state.campaigns_call(campaigns.frozen_query, c, write=False)
    # Why its waiting addresses aren't being checked, when that is so.
    held = await campaigns.checks_held(state) if (c.get("members") or {}).get("waiting") \
        else None
    return {"campaign": c, "members": rows, "checks_held": held}


@app.patch("/api/campaigns/{campaign_id}")
async def edit_campaign(campaign_id: str, request: Request):
    """Edit a campaign (campaigns.update_campaign)."""
    from harvey import campaigns

    body = await _json_body(request)
    allowed = ("name", "offer", "sequence", "li_note", "review", "rolling", "audience_id", "query")
    state = await _campaign_state()
    try:
        c = await state.campaigns_call(campaigns.update_campaign, campaign_id,
                                       **{k: body[k] for k in allowed if k in body})
    except campaigns.Refused as e:
        return _campaign_refusal(e)
    return {"success": True, "campaign": c}


@app.post("/api/campaigns/{campaign_id}/templates")
async def generate_templates(campaign_id: str):
    """"Generează secvența": the model writes the campaign's steps from its
    offer and a few of its addresses; returned for editing, saved with PUT
    (or PATCH sequence)."""
    from harvey import campaigns
    from harvey.agents.audience import Audience
    from harvey.models.campaign import Campaign

    state = await _campaign_state()
    c = await state.campaign_record(campaign_id)
    if c is None:
        return _refuse("no such campaign", 404)
    try:
        config = _campaign_config()
        query = await state.campaigns_call(campaigns.frozen_query, c, write=False)
        rows = [r for r in await campaigns.candidate_rows(state, query=query)
                if r["trust"] in ("confirmed", "likely")][:5] if query else []
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)
    sample = [Audience._prospect(r, r["status"]) for r in rows]
    model = Campaign(id=campaign_id, name=c["name"], profile_id=c["profile_id"] or "",
                     offer_json=json.dumps(c["offer"], ensure_ascii=False))
    steps = await _campaign_writer(state, config).write_templates(model, sample)
    if not steps:
        return _refuse("The model gave no usable sequence; try again or write it yourself.", 502)
    return {"success": True, "sequence": [s.model_dump() for s in steps]}


@app.put("/api/campaigns/{campaign_id}/templates")
async def save_templates(campaign_id: str, request: Request):
    """Save the edited steps: {"sequence": [...]}."""
    from harvey import campaigns

    body = await _json_body(request)
    state = await _campaign_state()
    try:
        c = await state.campaigns_call(campaigns.update_campaign, campaign_id,
                                       sequence=body.get("sequence"))
    except campaigns.Refused as e:
        return _campaign_refusal(e)
    return {"success": True, "campaign": c}


def _campaign_writer(state, config):
    """The Writer that generates a campaign's templates (tests replace it)."""
    from harvey.agents.writer import Writer
    from harvey.brain import Brain

    return Writer(Brain(state, config.brain), state, config)


@app.get("/api/campaigns/{campaign_id}/members")
async def campaign_members(campaign_id: str, status: str = "", limit: int = 200,
                           offset: int = 0):
    from harvey import campaigns

    state = await _campaign_state()
    try:
        return {"members": await state.campaigns_call(
            campaigns.members, campaign_id, status, max(1, min(int(limit), 500)),
            max(0, int(offset)), write=False)}
    except campaigns.Refused as e:
        return _campaign_refusal(e)


@app.post("/api/campaigns/{campaign_id}/members")
async def add_campaign_members(campaign_id: str, request: Request):
    """"Adaugă în campanie…": {"emails": [...]} (the ticked Contacts rows),
    each through every rule; the counts say what was taken and why not."""
    from harvey import campaigns

    body = await _json_body(request)
    emails = body.get("emails")
    if not isinstance(emails, list) or not emails or len(emails) > 5000 \
            or any(not isinstance(e, str) for e in emails):
        return _refuse('expected {"emails": [...]} (at most 5000)', 400)
    state = await _campaign_state()
    try:
        counts = await campaigns.add_by_hand(state, _campaign_config(), campaign_id, emails)
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)
    return {"success": True, "counts": counts}


@app.delete("/api/campaigns/{campaign_id}/members")
async def remove_campaign_members(campaign_id: str, request: Request):
    """"Scoate din campanie": {"emails": [...]}."""
    from harvey import campaigns

    body = await _json_body(request)
    emails = body.get("emails")
    if not isinstance(emails, list) or not emails or any(not isinstance(e, str) for e in emails):
        return _refuse('expected {"emails": [...]}', 400)
    state = await _campaign_state()
    return {"success": True, "removed": await campaigns.remove_by_hand(state, campaign_id, emails)}


@app.get("/api/campaigns/{campaign_id}/record.{fmt}")
async def campaign_record_export(campaign_id: str, fmt: str):
    """The campaign's record (evidență) as JSON or CSV: its legitimate-
    interest note, the acknowledgement it started under, every member with
    how it was added and why it ended, the emails sent and the opt-outs."""
    from harvey import campaigns

    if fmt not in ("json", "csv"):
        return _refuse("record.json or record.csv", 404)
    state = await _campaign_state()
    try:
        rec = await state.campaigns_call(campaigns.record, campaign_id, write=False)
    except campaigns.Refused as e:
        return _refuse(str(e), 404)
    name = f"evidenta-{campaign_id}-{date.today().isoformat()}.{fmt}"
    if fmt == "csv":
        return _csv_download(campaigns.record_csv(rec), name)
    return JSONResponse(rec, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/campaigns/{campaign_id}/{action}")
async def campaign_action(campaign_id: str, action: str):
    """start (the note, the acknowledgement, a sequence first; then the
    audience is frozen and taken), pause, resume, stop, archive."""
    from harvey import campaigns

    if action not in ("start", "pause", "resume", "stop", "archive"):
        return _refuse("unknown action", 404)
    state = await _campaign_state()
    try:
        if action == "start":
            counts = await campaigns.start(state, _campaign_config(), campaign_id)
            return {"success": True, "counts": counts,
                    "campaign": await state.campaign_record(campaign_id)}
        c = await state.campaigns_call(campaigns.set_status, campaign_id, action)
    except (campaigns.Refused, ValueError) as e:
        return _campaign_refusal(e)
    return {"success": True, "campaign": c}

@app.get("/api/conversations")
async def get_conversations():
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company,
               p.status as prospect_status
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    # The stored thread holds replies and Harvey's answers; the cold emails
    # a reply answers live in the Outbox. Attach those (sent ones only) and
    # any answer still waiting for approval, so the page shows the exchange.
    pids = sorted({r["prospect_id"] for r in rows if r.get("prospect_id")})
    emails: dict[str, list] = {}
    if pids:
        for e in await query_db(
                "SELECT prospect_id, campaign_id, step, subject, body, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND status = 'sent' AND prospect_id IN ({_in(pids)}) "
                "ORDER BY sent_at", tuple(pids)):
            emails.setdefault(e.pop("prospect_id"), []).append(e)
    cids = [r["id"] for r in rows]
    drafts = {d.pop("conversation_id"): d for d in (await query_db(
        "SELECT id, conversation_id, subject, body, send_at FROM outbox "
        f"WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id IN ({_in(cids)}) "
        "ORDER BY created_at", tuple(cids)) if cids else [])}
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
        # A prospect in two campaigns: show the sequence this reply answers.
        camp = row.get("campaign_id") or ""
        row["emails"] = [{k: v for k, v in e.items() if k != "campaign_id"}
                         for e in emails.get(row.get("prospect_id"), [])
                         if not camp or e.get("campaign_id") == camp]
        row["draft"] = drafts.get(row["id"])
    return rows


@app.post("/api/conversations/{convo_id}/status")
async def set_conversation_status(convo_id: str, request: Request):
    """Close a conversation, or hand one Harvey escalated back to it.

    Harvey answers new replies only on open conversations, so "open" means
    "Harvey, carry on" and "closed" means it's done."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("open", "closed"):
        return JSONResponse({"success": False, "message": "status must be open or closed"},
                            status_code=400)
    rows = await query_db("SELECT status FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such conversation."},
                            status_code=404)
    state = _state()
    await state.update_conversation(convo_id, status=status)
    await state.log_action("conversation_status", "dashboard",
                           {"conversation_id": convo_id, "from": rows[0]["status"], "to": status})
    return {"success": True, "status": status}


@app.get("/api/trend")
async def get_trend(days: int = 14, tz: int = 0):
    """Daily counts for the last `days` days in the viewer's time zone
    (`tz`: minutes east of UTC; stored times are UTC): contacts found,
    emails sent, first replies. Days with nothing are 0."""
    from datetime import datetime, timedelta, timezone
    days = max(1, min(90, days))
    tz = max(-840, min(840, tz))
    shift = f"{tz:+d} minutes"
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    async def per_day(table: str, column: str, where: str = "") -> list[int]:
        rows = await query_db(
            f"SELECT date({column}, ?) AS d, COUNT(*) AS n FROM {table} "
            f"WHERE date({column}, ?) >= ? {where} GROUP BY d", (shift, shift, labels[0]))
        counts = {r["d"]: r["n"] for r in rows}
        return [counts.get(day, 0) for day in labels]

    return {
        "days": labels,
        "found": await per_day("prospects", "created_at"),
        "sent": await per_day("outbox", "sent_at", "AND status = 'sent'"),
        "replies": await per_day("conversations", "created_at"),
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.


async def _batch_summary(state, batch_id: str) -> dict | None:
    """What a Descoperă run (one batch of jobs) did so far: its searches'
    reports merged, each source's own progress, the registry pulls, the
    sites read and contacts found, and how many of its jobs are where."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import merge_reports

    batch = await jobs.get_batch(state, batch_id)
    if batch is None:
        return None
    found = await jobs.list_jobs(state, batch_id=batch_id, limit=100000)
    searches = [j for j in found if j.kind == "discover"]
    pulls = [j for j in found if j.kind == "registry_pull"]
    sites = [j for j in found if j.kind == "site_read"]
    finder = [j for j in found if j.kind == "find_contacts"]
    report = merge_reports([j.progress.get("report") or j.result for j in searches])
    report["errors"] = (report["errors"] + [f"{j.label}: {j.error}" for j in found
                                             if j.status == jobs.FAILED and j.error])[:20]
    statuses: dict[str, int] = {}
    for j in found:
        statuses[j.status] = statuses.get(j.status, 0) + 1
    paused = [j for j in found if j.status == jobs.PAUSED]
    active = (jobs.QUEUED, jobs.RUNNING)
    # Each source of the run on its own: its jobs' states and what they found.
    sources: dict[str, dict] = {}
    for j in searches + pulls:
        key = j.params.get("provider") or j.params.get("source") or j.kind
        s = sources.setdefault(key, {"key": key, "jobs": {}, "found": 0, "new_companies": 0,
                                     "known_companies": 0, "actual_cost": 0.0, "active": 0})
        s["jobs"][j.status] = s["jobs"].get(j.status, 0) + 1
        s["active"] += j.status in active
        s["actual_cost"] = round(s["actual_cost"] + float(j.actual_cost or 0.0), 4)
        r = ((j.result or {}).get("report") or {}) if j.kind == "registry_pull" else (
            j.progress.get("report") or j.result or {})
        s["found"] += int(r.get("in_area" if j.kind == "registry_pull" else "found") or 0)
        for k in ("new_companies", "known_companies"):
            s[k] += int(r.get(k) or 0)
            if j.kind == "registry_pull":
                # (A search's are in the merged report already.)
                report[k] += int(r.get(k) or 0)
    pools = (batch.get("params") or {}).get("pools") or {}
    looking = [j for j in finder if j.params.get("pool") == "websites"]
    websites = None
    if (batch.get("params") or {}).get("find_websites"):
        websites = {"max_spend": pools.get("websites"),
                    "spent": round(sum(float(j.actual_cost or 0.0) for j in looking), 4),
                    "looked_up": sum(1 for j in looking if j.status == jobs.DONE),
                    "found": sum(1 for j in looking if j.status == jobs.DONE
                                 and (j.result or {}).get("website")),
                    "waiting": sum(1 for j in looking if j.status in active),
                    # Past the run's limit: left to the background's walk.
                    "left_to_background": int((batch.get("params") or {})
                                              .get("websites_deferred") or 0)}
    return {
        **report,
        "batch_id": batch_id, "label": batch.get("label") or "",
        "max_spend": batch.get("max_spend"),
        # A run that also finds websites: the searches' amount, and the
        # website lookups' own progress and spend (``websites``).
        "search_max_spend": pools.get("search"),
        "websites": websites,
        "spent": round(sum(float(j.actual_cost or 0.0) for j in found), 4),
        "sources": list(sources.values()),
        "profiled_companies": sum(1 for j in sites if j.status == jobs.DONE)
                              + sum(1 for j in finder if j.status == jobs.DONE
                                    and "site" in (j.result.get("steps") or [])),
        "profile_observations": sum(int(j.result.get("observations") or 0) for j in sites),
        "contacts_found": sum(int(j.result.get("contacts") or 0) for j in finder),
        "finder_waiting": sum(1 for j in finder if j.status in active),
        "sites_waiting": sum(1 for j in sites if j.status in active),
        "jobs": statuses,
        "spend_limited": sum(1 for j in paused if j.paused_reason == jobs.SPEND_LIMIT),
        "running": any(j.status in active for j in found),
        # Searches of this run still to finish: what its Stop stops.
        "searches_active": sum(1 for j in searches + pulls if j.status in active),
    }


def _source_keys(body: dict) -> list[str]:
    from harvey import launch

    return launch.source_keys(body)


async def _discovery_refusal(body: dict):
    """Why a source or the preset asked for can't run (a ModuleOff: its
    module is off), or None (launch.discovery_refusal)."""
    from harvey import launch

    state = _state()
    await state.init_db()
    return await launch.discovery_refusal(state, body)


def _discovery_queries(body: dict, config, provider: str = ""):
    from harvey import launch

    return launch.discovery_queries(body, config, provider)


async def _discovery_areas(body: dict) -> None:
    """The areas a request names, resolved onto it (launch.discovery_areas;
    ValueError for one that can't be found)."""
    from harvey import launch

    if body.get("areas"):
        state = _state()
        await state.init_db()
        await launch.discovery_areas(state, body)


def _discovery_plan(body: dict, config) -> tuple[list[dict], list[str]]:
    """One part per source the request ticked (launch.discovery_plan)."""
    from harvey import launch

    # (Through this module's _discovery_queries, read when called.)
    return launch.discovery_plan(body, config,
                                 queries=lambda b, c, k: _discovery_queries(b, c, k))


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use:
    the search modules' providers and every source Discover runs as a job
    (the registry, with the filters its manifest declares); the runs still
    working, and the newest one."""
    try:
        from harvey.collectors.discover import DEFAULT_PROVIDER, PRESETS, provider_menu
        from harvey.config import load_env
        from harvey.modules import switches
        from harvey.modules.job_kinds import discover_sources

        state = _state()
        await state.init_db()
        enabled = await switches.enabled_ids(state)
        batches = await query_db(
            "SELECT id FROM job_batches WHERE kind IN ('discover', 'site_read') "
            "ORDER BY rowid DESC LIMIT 1")
        last = await _batch_summary(state, batches[0]["id"]) if batches else None
        working = await query_db(
            "SELECT DISTINCT b.id FROM job_batches b JOIN jobs j ON j.batch_id = b.id "
            "WHERE b.kind IN ('discover', 'site_read') AND j.status IN ('queued', 'running') "
            "ORDER BY b.rowid DESC LIMIT 10")
        runs = [s for s in [await _batch_summary(state, r["id"]) for r in working] if s]
        active = await query_db(
            "SELECT kind, COUNT(*) AS n FROM jobs WHERE kind IN ('discover', 'site_read', "
            "'registry_pull') AND status IN ('queued', 'running') GROUP BY kind")
        kinds = {r["kind"]: r["n"] for r in active}
        selected = await state.get_setting("discovery_provider") or DEFAULT_PROVIDER
        return {
            "providers": provider_menu(load_env().model_dump(), enabled),
            "sources": discover_sources(enabled),
            # Presets whose module is off are hidden from the picker.
            "presets": {key: switches.capability_owner("preset", key) in enabled
                        for key in PRESETS},
            "default": DEFAULT_PROVIDER,
            "selected": selected,
            "selected_all": [k for k in selected.split(",") if k],
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(kinds),
            "running_kind": "discover" if kinds.get("discover") or kinds.get("registry_pull")
                            else "profile",
            "searches_waiting": kinds.get("discover", 0) + kinds.get("registry_pull", 0),
            "sites_waiting": kinds.get("site_read", 0),
            "unprofiled": await state.count_companies_needing_profile(),
            "last_report": last,
            "active_runs": runs,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is
    collected, for every source ticked (``providers``; ``provider`` for
    one): the sum, and each source's own. A source that can count
    (DataForSEO Business Listings) is asked how many results each search
    has first, a small paid request each, so the price is exact; the
    answer says what counting cost. The registry's estimate is its free
    count of the firms that match."""
    try:
        from harvey import launch
        from harvey.config import load_config

        body = await request.json()
        refusal = await _discovery_refusal(body)
        if refusal:
            return JSONResponse(_error_body(refusal), status_code=409)
        config = load_config()
        try:
            await _discovery_areas(body)
            plan, notes = _discovery_plan(body, config)
        except LookupError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:
            websites = _find_websites(body)
        except (TypeError, ValueError):
            return JSONResponse({"error": "websites_max_spend must be a number of dollars, "
                                 "0 or more"}, status_code=400)
        state = _state()
        await state.init_db()
        return await launch.discovery_estimate(state, config, plan, notes, websites)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _duplicate_body(e) -> dict:
    """A 409 for work already waiting: the message, plus the job (its kind,
    label and status) for the page to say it in the reader's language."""
    return {"success": False, "message": str(e), "duplicate": True, "code": "duplicate",
            "job": e.job.as_dict()}


from harvey.launch import WEBSITES_DEFAULT_USD  # noqa: E402,F401


def _find_websites(body: dict) -> float | None:
    """A Discover request's "also find websites, up to $X"
    (launch.find_websites)."""
    from harvey import launch

    return launch.find_websites(body)


async def _websites_estimate(state, amount: float | None) -> dict:
    from harvey import launch

    return await launch.websites_estimate(state, amount)


def _money(value, default: float | None = None) -> float:
    """A dollar amount from a request: finite and >= 0, else ValueError."""
    from harvey import launch

    return launch.money(value, default)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Queue the run and hand back at once: every source ticked in one
    batch under one spend cap, a job per search on the source's own lane
    and a registry pull on the registry's. Another run, of these sources or
    any other, queues behind or beside it; the same search asked again
    while it still waits is refused, naming the job already there. What
    every source saves feeds the contact finder."""
    try:
        from harvey.config import load_config
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_sources

        body = await request.json()
        refusal = await _discovery_refusal(body)
        if refusal:
            return JSONResponse(_error_body(refusal, "message", success=False),
                                status_code=409)

        config = load_config()
        try:
            await _discovery_areas(body)
            plan, notes = _discovery_plan(body, config)
        except LookupError as e:
            return JSONResponse({"success": False, "message": str(e).strip("'\"")},
                                status_code=400)
        except ValueError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        try:
            # 0 is a cap of nothing (free sources only); blank, the default.
            max_spend = _money(body.get("max_spend"), default=1.0)
        except (TypeError, ValueError):
            return JSONResponse({"success": False, "message": "max_spend must be a number "
                                 "of dollars, 0 or more", "code": "bad_cap"}, status_code=400)
        try:
            find_websites = _find_websites(body)
        except (TypeError, ValueError):
            return JSONResponse({"success": False, "message": "websites_max_spend must be a "
                                 "number of dollars, 0 or more", "code": "bad_cap"},
                                status_code=400)
        try:
            # (What the classifier may cost on the companies the run saves:
            # absent, no cap of the run's own.)
            classify_cap = None if body.get("classify_max_spend") in (None, "") \
                else _money(body.get("classify_max_spend"))
        except (TypeError, ValueError):
            return JSONResponse({"success": False, "message": "classify_max_spend must be a "
                                 "number of dollars, 0 or more", "code": "bad_cap"},
                                status_code=400)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider",
                                ",".join(p_.get("provider") or p_["source"] for p_ in plan))
        await state.set_setting("discovery_paused", "")
        try:
            batch_id, ids, dupes = await enqueue_sources(
                state, plan, max_spend=max_spend, find_websites=find_websites,
                classify_max_spend=classify_cap,
                label=" + ".join(p_.get("name") or p_["label"] for p_ in plan))
        except jobs.Duplicate as e:
            return JSONResponse(_duplicate_body(e), status_code=409)
        return {"success": True, "queries": len(ids), "batch_id": batch_id, "notes": notes,
                "duplicates": [d.as_dict() for d in dupes]}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Queue a read of the website of everything discovered but not yet
    looked at: one job per company on the website reader's lane.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    try:
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_site_reads

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}
        batch_id = await jobs.new_batch(state, "site_read", "Read sites")
        ids, dupes = await enqueue_site_reads(state, batch_id=batch_id)
        return {"success": True, "pending": len(ids), "already_queued": len(dupes),
                "batch_id": batch_id}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery(request: Request):
    """Cancel what is still to finish of one run ({"batch_id"}), of one
    source within it ({"batch_id", "provider"}), or of one source in every
    run ({"provider"}): its searches and registry pulls. A running search
    stops after its page, keeping what it found; sites and contacts already
    queued are still looked at. Other runs (another source's, a command's)
    and searches paused at a spend limit or by hand are left alone."""
    try:
        from harvey.modules import jobs
        from harvey.modules.job_kinds import discover_sources, discovery_lane

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        batch_id = str(body.get("batch_id") or "")
        provider = str(body.get("provider") or "")
        if not batch_id and not provider:
            return JSONResponse({"success": False, "message": "say which run to stop "
                                 "(batch_id) or which source (provider)"}, status_code=400)
        jobbed = {s["key"]: s["module"] for s in discover_sources()}
        lane = None
        if not batch_id:
            if provider in jobbed:
                lane = jobbed[provider]
            else:
                try:
                    lane = discovery_lane(provider)[0]
                except ValueError as e:
                    return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        state = _state()
        await state.init_db()
        stopped = 0
        for job in await jobs.list_jobs(state, statuses=(jobs.QUEUED, jobs.RUNNING),
                                        batch_id=batch_id or None, lane=lane, limit=100000):
            if job.kind not in ("discover", "registry_pull"):
                continue
            key = job.params.get("provider") or job.params.get("source") or ""
            if provider and key != provider:
                continue
            try:
                await jobs.cancel(state, job.id)
            except jobs.Refused:
                continue        # it finished meanwhile
            stopped += 1
        return {"success": True, "cancelled": stopped}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# ── The queue (Coadă): every job, per lane ──

# Finished jobs shown per lane.
QUEUE_DONE_SHOWN = 10


def _lane_name(lane: str) -> str:
    """The lane's name as its module's manifest gives it (English); the page
    shows its own words for the lanes it knows (q.lane.<id>)."""
    from harvey.modules import switches

    m = switches.by_id().get(lane)
    if m is not None:
        return m.name or m.id
    return {"contacts": "Contacts", "duplicates": "Duplicates", "counts": "Counts",
            "classify": "Classifier", "site_check": "Website checks"}.get(lane, lane)


@app.get("/api/jobs")
async def get_jobs(batch: str = ""):
    """Every lane: what is running (with its progress), waiting, paused,
    done and failed, and whether the lane is paused; the worker running
    them. ``batch`` narrows it to one run's jobs."""
    try:
        from harvey.modules import jobs
        from harvey.modules.worker import lease_holder

        state = _state()
        await state.init_db()
        paused = await jobs.paused_lanes(state)
        counts = await jobs.counts(state)
        lanes: dict[str, dict] = {}
        for lane in sorted(set(counts["lanes"]) | set(paused)):
            lanes[lane] = {"lane": lane, "name": _lane_name(lane), "paused": paused.get(lane, ""),
                           "counts": counts["lanes"].get(lane, {}), "running": [],
                           "waiting": [], "paused_jobs": [], "done": [], "failed": []}
        shown = await jobs.list_jobs(state, statuses=(jobs.RUNNING, jobs.PAUSED),
                                     batch_id=batch or None, limit=2000)
        # Waiting: per lane, the first 50 in the order the worker runs them.
        for lane_id in sorted(set(lanes) | {
                r["lane"] for r in await query_db(
                    "SELECT DISTINCT lane FROM jobs WHERE status = 'queued'"
                    + (" AND batch_id = ?" if batch else ""), (batch,) if batch else ())}):
            shown += await jobs.list_jobs(state, lane=lane_id, statuses=(jobs.QUEUED,),
                                          batch_id=batch or None, limit=50, oldest_first=True)
        finished = await query_db(
            "SELECT id FROM (SELECT id, lane, status, ROW_NUMBER() OVER (PARTITION BY lane, "
            "status ORDER BY rowid DESC) AS n FROM jobs WHERE status IN "
            "('done', 'failed', 'cancelled')" + (" AND batch_id = ?" if batch else "") +
            ") WHERE n <= ?", (*((batch,) if batch else ()), QUEUE_DONE_SHOWN))
        for row in finished:
            job = await jobs.get(state, row["id"])
            if job is not None:
                shown.append(job)
        where = {jobs.RUNNING: "running", jobs.QUEUED: "waiting", jobs.PAUSED: "paused_jobs",
                 jobs.DONE: "done", jobs.CANCELLED: "done", jobs.FAILED: "failed"}
        for job in shown:
            lane = lanes.setdefault(job.lane, {
                "lane": job.lane, "name": _lane_name(job.lane), "paused": paused.get(job.lane, ""),
                "counts": counts["lanes"].get(job.lane, {}), "running": [], "waiting": [],
                "paused_jobs": [], "done": [], "failed": []})
            lane[where[job.status]].append(job.as_dict())
        for lane in lanes.values():
            lane["waiting_more"] = max(0, lane["counts"].get(jobs.QUEUED, 0) - len(lane["waiting"]))
            for key in ("done", "failed"):
                lane[key].sort(key=lambda j: j["finished_at"] or j["created_at"] or "", reverse=True)
                lane[key] = lane[key][:QUEUE_DONE_SHOWN]
        holder = await lease_holder(state)
        return {
            "lanes": sorted(lanes.values(), key=lambda l: (
                -len(l["running"]), -l["counts"].get(jobs.QUEUED, 0), l["name"].lower())),
            "running": counts.get(jobs.RUNNING, 0), "waiting": counts.get(jobs.QUEUED, 0),
            "paused": counts.get(jobs.PAUSED, 0), "failed": counts.get(jobs.FAILED, 0),
            "worker": {"pid": holder.get("pid"), "host": holder.get("host")} if holder else None,
            "batch": await jobs.get_batch(state, batch) if batch else None,
            "report": await jobs.batch_report(state, batch) if batch else None,
        }
    except Exception as e:
        logger.exception("jobs failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/jobs/counts")
async def get_job_counts():
    """The nav badge: jobs running and waiting."""
    rows = await query_db("SELECT status, COUNT(*) AS n FROM jobs "
                          "WHERE status IN ('running', 'queued') GROUP BY status")
    n = {r["status"]: r["n"] for r in rows}
    return {"running": n.get("running", 0), "waiting": n.get("queued", 0)}


@app.post("/api/jobs/{job_id}/{action}")
async def act_on_job(job_id: str, action: str):
    """cancel, retry, resume or pause one job."""
    from harvey.modules import jobs

    do = {"cancel": jobs.cancel, "retry": jobs.retry, "resume": jobs.resume,
          "pause": jobs.pause}.get(action)
    if do is None:
        return JSONResponse({"success": False, "message": f"unknown action {action!r}"},
                            status_code=400)
    state = _state()
    await state.init_db()
    try:
        job = await do(state, job_id)
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    except jobs.Refused as e:
        return JSONResponse({"success": False, "message": str(e), "code": e.code},
                            status_code=409)
    return {"success": True, "job": job.as_dict()}


@app.post("/api/lanes/{lane}/{action}")
async def act_on_lane(lane: str, action: str):
    """pause or resume a lane."""
    from harvey.modules import jobs

    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": f"unknown action {action!r}"},
                            status_code=400)
    state = _state()
    await state.init_db()
    if not await jobs.known_lane(state, lane):
        return JSONResponse({"success": False, "message": f"no lane {lane!r}"}, status_code=404)
    if action == "pause":
        await jobs.pause_lane(state, lane)
    else:
        await jobs.resume_lane(state, lane)
    return {"success": True, "lane": lane, "paused": action == "pause"}


# ── Chat: the chats with Harvey's agent (harvey/agent/chat.py) ──

def _chat_refused(e) -> JSONResponse:
    status = {"empty": 400, "too_long": 400, "retry": 400, "missing": 404,
              "budget": 429}.get(e.code, 409)
    return JSONResponse({"success": False, "code": e.code, "message": str(e)},
                        status_code=status)


@app.get("/api/agent/chats")
async def get_agent_chats(q: str = "", limit: int = 100):
    """The chats, newest activity first ({id, title, created_at, last_at,
    answering}), those whose title holds ``q``; why the chat can't run with
    these settings (``refused``: Codex where it isn't supported); who
    answers (``provider``, claude or codex, and ``model``, "" for its
    default)."""
    from harvey.agent import chat

    state = _state()
    await state.init_db()
    config = _load_config_quietly()
    brain = getattr(config, "brain", None)
    return {"chats": await chat.chats(state, q, limit),
            "refused": chat.refusal(config),
            "provider": getattr(brain, "provider", "claude") or "claude",
            "model": getattr(brain, "model", "") or ""}


@app.post("/api/agent/chats")
async def create_agent_chat(request: Request):
    """A new chat with its first message, {"text"} (its title is made from
    it): {"success", "chat", "turn"}, the answer started at once (follow it
    with /api/agent/chats/{chat}/stream?turn=). Refused before anything is
    written, with ``code`` (chat.REFUSALS)."""
    from harvey.agent import chat

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        answer = await chat.hub(state).ask(None, str(body.get("text") or ""),
                                           config=_load_config_quietly())
    except chat.Refused as e:
        return _chat_refused(e)
    return {"success": True, "chat": answer.chat, "turn": answer.turn}


@app.patch("/api/agent/chats/{chat_id}")
async def rename_agent_chat(chat_id: str, request: Request):
    """Rename a chat: {"title"}. {"success", "chat"}."""
    from harvey.agent import chat

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        return {"success": True, "chat": await chat.rename(state, chat_id,
                                                           str(body.get("title") or ""))}
    except chat.Refused as e:
        return _chat_refused(e)


@app.delete("/api/agent/chats/{chat_id}")
async def delete_agent_chat(chat_id: str):
    """Delete a chat, its messages and their tool calls (an answer being
    made is stopped first; the usage rows stay; its open plans close:
    chat.Hub.delete). {"success"}."""
    from harvey.agent import chat

    state = _state()
    await state.init_db()
    try:
        await chat.hub(state).delete(chat_id)
    except chat.Refused as e:
        return _chat_refused(e)
    return {"success": True}


@app.get("/api/agent/chats/{chat_id}/messages")
async def get_agent_chat_messages(chat_id: str, limit: int = 50, before: int = 0):
    """A chat's newest messages, oldest first ({id, turn, role, text,
    status, created_at, tokens, context}; ``before``: older than that id,
    ``more``: older ones are on file), each turn's tool calls (``calls``:
    {turn: [{tool, arguments, result, ms, outcome}]}), the chat ({id,
    title, context, hint: past chat.CONTEXT_HINT_TOKENS}), the message
    being answered (``live``: follow it with the stream), ``busy``, and
    ``refused`` (Codex as the brain), and the chat's plans (``plans``,
    oldest first, each with its committed, spent and runs, an approved
    one's wakes to come). A wake's answer carries ``wake`` (why it woke);
    reading the newest page marks them read. 404 for no such chat."""
    from harvey.agent import chat

    state = _state()
    await state.init_db()
    from harvey.agent import plans

    found = await chat.messages(state, chat_id, limit=limit, before=before or None)
    if found["chat"] is None:
        return _bad("no such chat", 404)
    if not before:
        # (The newest messages shown: its wake answers are read.)
        await chat.mark_read(state, chat_id)
    return {**found, "plans": await plans.for_chat(state, chat_id),
            "refused": chat.refusal(_load_config_quietly())}


@app.post("/api/agent/chats/{chat_id}/messages")
async def post_agent_chat_message(chat_id: str, request: Request):
    """Send a chat a message: {"text"}. {"success", "chat", "turn"}, or
    refused with ``code`` as for a new chat (missing: 404; full: every live
    chat is answering)."""
    from harvey.agent import chat

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        answer = await chat.hub(state).ask(chat_id, str(body.get("text") or ""),
                                           config=_load_config_quietly())
    except chat.Refused as e:
        return _chat_refused(e)
    return {"success": True, "chat": answer.chat, "turn": answer.turn}


@app.post("/api/agent/chats/{chat_id}/retry")
async def retry_agent_chat_message(chat_id: str, request: Request):
    """Send the chat's newest message again when its answer failed or was
    stopped: {"turn"}. {"success", "chat", "turn": the new one}; the
    earlier attempt stays on file, marked retried."""
    from harvey.agent import chat

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        answer = await chat.hub(state).retry(chat_id, str(body.get("turn") or ""),
                                             config=_load_config_quietly())
    except chat.Refused as e:
        return _chat_refused(e)
    return {"success": True, "chat": answer.chat, "turn": answer.turn}


@app.get("/api/agent/chats/{chat_id}/stream")
async def stream_agent_answer(chat_id: str, turn: str = "", after: int = 0):
    """The answer to ``turn`` as it is made, as server-sent events (one
    ``data:`` JSON line each, from event ``after`` on: from the start when
    the page comes back to the chat): start, text (as it is written),
    tool_use (a tool call as it starts), tool_result (its outcome, from
    agent_tool_call), then done (with the tokens and the chat's context)
    or error (with its code). A turn this process isn't answering gets a
    single gone event: its message's status says how it ended."""
    from harvey.agent import chat

    answer = chat.hub(_state()).answer(chat_id)

    async def events():
        if answer is None or answer.turn != turn:
            yield "data: " + json.dumps({"type": "gone", "turn": turn}) + "\n\n"
            return
        async for event in answer.follow(max(0, int(after))):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/agent/chats/{chat_id}/stop")
async def stop_agent_answer(chat_id: str):
    """Stop the chat's answer being made: {"success", "stopped": whether
    there was one}."""
    from harvey.agent import chat

    return {"success": True, "stopped": await chat.hub(_state()).stop(chat_id)}


# ── The agent's plans (harvey/agent/plans.py): only the user decides them ──

def _plan_refused(e) -> JSONResponse:
    return JSONResponse({"success": False, "message": str(e)},
                        status_code=404 if str(e).startswith("no plan") else 409)


@app.get("/api/agent/plans")
async def get_agent_plans(chat: str = "", status: str = "", limit: int = 50):
    """The agent's plans, newest first (of one chat, of one status), each
    with what its runs committed and spent and what remains."""
    from harvey.agent import plans

    state = _state()
    await state.init_db()
    out = []
    for p in await plans.listed(state, chat or None, status, limit):
        m = await plans.money(state, p["id"])
        out.append({**p, **m,
                    "remaining_usd": round(max(0.0, p["budget_usd"] - m["committed_usd"]), 6)})
    return {"plans": out}


@app.get("/api/agent/plans/{plan_id}")
async def get_agent_plan(plan_id: int):
    """One plan with its money, its runs and its actions (refusals too)."""
    from harvey.agent import plans

    state = _state()
    await state.init_db()
    try:
        out = await plans.status(state, plan_id)
    except plans.Refused as e:
        return _plan_refused(e)
    return {**out, "actions": await plans.actions(state, plan_id)}


@app.post("/api/agent/plans/{plan_id}/{action}")
async def decide_agent_plan(plan_id: int, action: str, request: Request):
    """approve ({"budget_usd": the budget the user confirmed, which must be
    the plan's}), reject, cancel (its live runs are cancelled too) or close
    (done: its runs go on to their end). The user's alone: the agent's
    tools can't."""
    from harvey.agent import plans

    if action not in ("approve", "reject", "cancel", "close"):
        return _bad("approve, reject, cancel or close", 404)
    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        if action in ("approve", "reject"):
            if action == "approve" and body.get("budget_usd") is None:
                return _bad("budget_usd: the plan's budget, as the user confirmed it")
            plan = await plans.decide(state, plan_id, action == "approve", plans.BY_DASHBOARD,
                                      budget_usd=body.get("budget_usd"))
        else:
            plan = await plans.close(state, plan_id, cancel=action == "cancel",
                                     by=plans.BY_DASHBOARD)
    except plans.Refused as e:
        return _plan_refused(e)
    return {"success": True, "plan": plan}


@app.post("/api/agent/wakes/{event_id}/cancel")
async def cancel_agent_wake(event_id: int):
    """Cancel a wake the agent scheduled (one not read yet): {"success",
    "wake"}."""
    from harvey.agent import wakes

    state = _state()
    await state.init_db()
    try:
        return {"success": True, "wake": await wakes.cancel_event(state, event_id)}
    except wakes.Refused as e:
        return _bad(str(e), 404)


# ── The agent's tasks (harvey/agent/tasks.py): the user sees and cancels them ──

@app.get("/api/agent/tasks")
async def get_agent_tasks(plan: int | None = None, limit: int = 20):
    """The agent's tasks, newest first (of one plan): profile, status, the
    checked result, the problem, tokens."""
    from harvey.agent import tasks

    state = _state()
    await state.init_db()
    return {"tasks": [tasks.public(t) for t in await tasks.listed(state, plan, limit)]}


@app.get("/api/agent/tasks/{task_id}")
async def get_agent_task(task_id: int):
    from harvey.agent import tasks

    state = _state()
    await state.init_db()
    found = await tasks.get(state, task_id)
    if found is None:
        return _bad(f"no task {task_id}", 404)
    return {**tasks.public(found), "prompt": found["prompt"]}


@app.post("/api/agent/tasks/{task_id}/cancel")
async def cancel_agent_task(task_id: int):
    """Cancel a task still queued or running (a running one is stopped)."""
    from harvey.agent import tasks

    state = _state()
    await state.init_db()
    try:
        return {"success": True, "task": tasks.public(
            await tasks.cancel(state, task_id, why="cancelled by the user"))}
    except tasks.Refused as e:
        return _bad(str(e), 404 if str(e).startswith("no task") else 409)


# ── The agent's memory (harvey/agent/memory.py): the user sees and edits it ──

@app.get("/api/agent/memory")
async def get_agent_memory():
    """The agent's notes ({id, text, author, created_at, updated_at}) with
    what they use of their room, and its playbooks (their text too)."""
    from harvey.agent import memory

    state = _state()
    await state.init_db()
    return {"notes": await memory.notes(state), "usage": await memory.usage(state),
            "playbooks": await memory.playbooks(state, bodies=True),
            "playbook_limit": memory.PLAYBOOK_CHARS}


def _memory_refused(e) -> JSONResponse:
    return JSONResponse({"success": False, "message": str(e)},
                        status_code=404 if str(e).startswith("no ") else 400)


@app.post("/api/agent/memory/notes")
async def add_agent_note(request: Request):
    """A note of the user's: {"text"}."""
    from harvey.agent import memory

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        note = await memory.remember(state, body.get("text"), author=memory.BY_DASHBOARD)
    except memory.Refused as e:
        return _memory_refused(e)
    return {"success": True, "note": note}


@app.patch("/api/agent/memory/notes/{note_id}")
async def edit_agent_note(note_id: int, request: Request):
    """A note's text changed: {"text"}."""
    from harvey.agent import memory

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        note = await memory.edit_note(state, note_id, body.get("text"))
    except memory.Refused as e:
        return _memory_refused(e)
    return {"success": True, "note": note}


@app.delete("/api/agent/memory/notes/{note_id}")
async def delete_agent_note(note_id: int):
    from harvey.agent import memory

    state = _state()
    await state.init_db()
    try:
        await memory.forget(state, note_id)
    except memory.Refused as e:
        return _memory_refused(e)
    return {"success": True}


@app.patch("/api/agent/memory/playbooks/{playbook_id}")
async def edit_agent_playbook(playbook_id: int, request: Request):
    """A playbook changed: {"name"?, "body"?}."""
    from harvey.agent import memory

    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        p = await memory.edit_playbook(state, playbook_id, name=body.get("name"),
                                       body=body.get("body"))
    except memory.Refused as e:
        return _memory_refused(e)
    return {"success": True, "playbook": p}


@app.delete("/api/agent/memory/playbooks/{playbook_id}")
async def delete_agent_playbook(playbook_id: int):
    from harvey.agent import memory

    state = _state()
    await state.init_db()
    try:
        await memory.delete_playbook(state, playbook_id)
    except memory.Refused as e:
        return _memory_refused(e)
    return {"success": True}


@app.get("/api/jobs/batches")
async def get_recent_batches(limit: int = 5):
    """The newest runs, each with its report (Today: what each run took
    and cost)."""
    from harvey.modules import jobs

    state = _state()
    await state.init_db()
    return {"runs": [{**b, "report": await jobs.batch_report(state, b["id"])}
                     for b in await jobs.recent_batches(state, max(1, min(limit, 20)))]}


@app.get("/api/jobs/batches/{batch_id}/report")
async def get_batch_report(batch_id: str):
    """How long a run took and what it cost, split: {jobs, statuses,
    seconds (first start to last finish), job_seconds, searches_usd,
    checks_made, checks_usd, classify: {calls, errors, tokens_in,
    tokens_out, usd, unpriced, refused}, other_usd, total_usd}; each job's
    own figures are in its result (``spend``, ``ms``, ``classify``)."""
    from harvey.modules import jobs

    state = _state()
    await state.init_db()
    batch = await jobs.get_batch(state, batch_id)
    if batch is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    return {**await jobs.batch_report(state, batch_id), "batch": batch}


@app.post("/api/jobs/batches/{batch_id}/cap")
async def raise_batch_cap(batch_id: str, request: Request):
    """{"max_spend": n} and/or {"classify_max_spend": n}: a new spend limit
    for a run (its searches', its classifier's); its jobs the old limit
    paused go back to the queue and carry on from where they stopped. A
    run with amounts of its own (a Discover run's searches and website
    lookups) gives a max_spend change to ``pool`` ("websites"), else its
    searches."""
    from harvey.modules import jobs

    try:
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        cap = _money(body.get("max_spend")) if "max_spend" in body else None
        classify_cap = _money(body.get("classify_max_spend")) \
            if "classify_max_spend" in body else None
        if cap is None and classify_cap is None:
            raise ValueError("nothing to change")
        pool = body.get("pool") or ""
        if not isinstance(pool, str):
            raise ValueError(pool)
    except Exception:
        return JSONResponse({"success": False, "message": "expected max_spend or "
                             "classify_max_spend, a number >= 0"}, status_code=400)
    state = _state()
    await state.init_db()
    if await jobs.get_batch(state, batch_id) is None:
        return JSONResponse({"success": False, "message": "no such run"}, status_code=404)
    if cap is not None:
        await jobs.set_batch_cap(state, batch_id, cap, pool=pool)
    if classify_cap is not None:
        await jobs.set_classify_cap(state, batch_id, classify_cap)
    resumed = 0
    for job in await jobs.list_jobs(state, statuses=(jobs.PAUSED,), batch_id=batch_id,
                                    limit=100000):
        if job.paused_reason == jobs.SPEND_LIMIT:
            await jobs.resume(state, job.id)
            resumed += 1
    return {"success": True, "resumed": resumed}


def _parse_ts(value) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP / isoformat strings -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
    except ValueError:
        return None


async def _scalar(sql: str, params: tuple = ()) -> int:
    rows = await query_db(sql, params)
    return int(next(iter(rows[0].values())) or 0) if rows else 0


async def _first(sql: str, params: tuple = (), key: str = "t"):
    """First row's `key`, or None — query_db returns [] on any DB problem."""
    rows = await query_db(sql, params)
    return rows[0].get(key) if rows else None


# A run row still says 'running' after its process was killed (closed window,
# reboot). No new facts for this long means it is not coming back.
STALE_RUN_SECONDS = 900
# The contact finder has no run row: a company it searched this recently, or
# a find_contacts job running, means it is at work.
CONTACT_ACTIVE_SECONDS = 180
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


# ── The database upgrade (harvey/upgrade.py) ──

def _upgrade_layout():
    from harvey import upgrade

    return upgrade.Layout(DB_PATH.parent)


@app.get("/api/upgrade")
async def get_upgrade():
    """Whether harvey.db needs the upgrade, its stages, verification,
    estimate (sizes, time, disk) and the old files the switch kept."""
    from harvey import upgrade

    return await asyncio.to_thread(upgrade.status, _upgrade_layout())


def _spawn_upgrade() -> None:
    """Run harvey upgrade in its own process (it outlives a closed tab, and
    a crash there never takes the dashboard down); its output goes to
    data/upgrade.log."""
    log = open(DB_PATH.parent / "upgrade.log", "ab")
    try:
        subprocess.Popen([sys.executable, "-m", "harvey", "upgrade", "--yes", "--data",
                          str(DB_PATH.parent)], cwd=str(PROJECT_ROOT), stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=sys.platform != "win32")
    finally:
        log.close()


@app.post("/api/upgrade/start")
async def start_upgrade():
    """Start (or resume) the upgrade; refused when none is needed, one is
    running, or the free space is too little or can't be known."""
    from harvey import upgrade

    s = await asyncio.to_thread(upgrade.status, _upgrade_layout())
    if not s["needed"]:
        return JSONResponse({"success": False, "message": "harvey.db is upgraded already"},
                            status_code=409)
    if s["running"]:
        return JSONResponse({"success": False, "message": "the upgrade is running"},
                            status_code=409)
    # (Finishing a switch writes nothing new: no space to check.)
    est = {} if s.get("switch_pending") else s.get("estimate") or {}
    if est.get("free_error"):
        return JSONResponse({"success": False, "message": est["free_error"]}, status_code=409)
    if est.get("free_bytes") is not None and est["free_bytes"] < est["floor_bytes"]:
        return JSONResponse({"success": False, "message": (
            f"{est['free_bytes'] / 1e9:.1f} GB free; the upgrade needs at least "
            f"{est['floor_bytes'] / 1e9:.0f} GB")}, status_code=409)
    await asyncio.to_thread(_spawn_upgrade)
    return {"success": True}


# What the user types to delete the old files (either language's word).
DELETE_WORDS = ("șterge", "sterge", "delete")


@app.post("/api/upgrade/delete-old")
async def delete_old_files(request: Request):
    """Delete the files the switch kept (harvey.db.v24, anaf.db.old), once
    the typed confirmation says so."""
    from harvey import upgrade

    body = await request.json() if await request.body() else {}
    if str(body.get("confirm", "")).strip().lower() not in DELETE_WORDS:
        return JSONResponse({"success": False, "message": "type the word to confirm"},
                            status_code=400)
    try:
        freed = await asyncio.to_thread(upgrade.delete_old, _upgrade_layout())
    except upgrade.Refused as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=409)
    return {"success": True, "freed": freed}


@app.get("/api/live-status")
async def get_live_status():
    """Plain-language progress: each pipeline step, what is running, how long is left.

    Built for someone who would otherwise keep asking "what's the status?".
    Everything is derived from the database, so it is true whatever started
    the work — `harvey run`, a CLI command, or a one-off script. Every total
    counts only what the step can act on (a business with no website is never
    read), so a finished step always reaches 100% instead of sticking.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    if DB_PATH.exists():
        firm_counts = await _state().read("status_counts")
        companies, with_site, profiled, checked = (
            firm_counts[k] for k in ("companies", "with_site", "profiled", "checked"))
    else:
        companies = with_site = profiled = checked = 0
    no_site = companies - with_site

    prospects = await query_db("SELECT id, email, email_status, status FROM prospects")
    with_email = [p for p in prospects if p.get("email")]
    by_status: dict[str, int] = {}
    for p in with_email:
        k = (p.get("email_status") or "guess").lower()
        by_status[k] = by_status.get(k, 0) + 1
    deliverable = {p["id"] for p in with_email
                   if (p.get("email_status") or "") in ("verified", "risky")}
    unconfirmed = sum(n for k, n in by_status.items()
                      if k not in ("verified", "risky", "invalid"))

    # Drafts are counted per person, not per campaign: one campaign can cover many.
    drafted: set[str] = set()
    for row in await query_db("SELECT prospect_ids_json FROM campaigns"):
        try:
            ids = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            ids = []
        if isinstance(ids, list):
            drafted.update(i for i in ids if isinstance(i, str))
    sent = {p["id"] for p in prospects if p.get("status") in SENT_STATUSES}
    # Who is left for the user to send by hand: only when Harvey can't send
    # itself, and never someone Harvey has queued, sent to, or can't reach.
    harvey_sends = _harvey_can_send()
    awaiting_review = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
    by_hand: set[str] = set()
    if not harvey_sends:
        skip = await _harvey_committed_ids() | await _hand_sent_ids()
        by_hand = {p["id"] for p in prospects
                   if p["id"] in drafted and p["id"] not in skip
                   and (p.get("status") or "new") in HAND_SENDABLE_STATUSES
                   and (p.get("email_status") or "guess") in HAND_SENDABLE_EMAIL}
    replies = sum(1 for p in prospects if p.get("status") in REPLY_STATUSES)
    cost = float(await _first(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs", key="c") or 0)

    running, stopped = [], []
    for run in await query_db(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at"):
        started = _parse_ts(run.get("started_at"))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        last = started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = (await _state().read(
                "status_counts", since=started.replace(tzinfo=timezone.utc).timestamp())
            )["profiled_since"]
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    # (The newest searches only: the table holds every search ever made.)
    last_contact = _parse_ts(firm_counts["last_contact"]) if DB_PATH.exists() else None
    # Work is jobs now: one running on its lane counts as the step running.
    busy = {r["kind"] for r in await query_db(
        "SELECT DISTINCT kind FROM jobs WHERE status = 'running'")}
    finding_emails = "find_contacts" in busy or bool(
        last_contact and (now - last_contact).total_seconds() < CONTACT_ACTIVE_SECONDS)
    reading = any(r["stage"] == "profile" for r in running) or "site_read" in busy
    finding = any(r["stage"] == "discover" for r in running) or "discover" in busy

    def step(label, done, total, active, detail, info=False, key="", vars=None):
        if active:
            state_ = "running"
        elif info:
            state_ = "done" if done else "waiting"
        elif total and done >= total:
            state_ = "done"
        elif done:
            state_ = "partial"
        else:
            state_ = "waiting"
        # key + vars: stable, so the dashboard can say it in its own language.
        return {"label": label, "done": done, "total": total,
                "state": state_, "detail": detail, "key": key, "vars": vars or {}}

    profile_eta = next((r["eta_min"] for r in running if r["stage"] == "profile"), None)
    read_detail = f"{profiled} of {with_site} read"
    if reading and profile_eta is not None:
        read_detail += " · " + _eta_text(profile_eta)
    if no_site:
        read_detail += f" · {no_site} have no website (skipped)"

    steps = [
        step("Find businesses", companies, 0, finding,
             f"{companies} businesses found", info=True, key="find",
             vars={"companies": companies}),
        step("Read their websites", profiled, with_site, reading, read_detail, key="read",
             vars={"profiled": profiled, "with_site": with_site, "no_site": no_site,
                   "eta_min": profile_eta if reading else None}),
        step("Find emails", checked, profiled, finding_emails,
             f"{checked} of {profiled} checked · {len(with_email)} emails found",
             key="emails", vars={"checked": checked, "profiled": profiled,
                                 "emails": len(with_email)}),
        step("Check emails are real", len(with_email) - unconfirmed, len(with_email), False,
             f"{by_status.get('verified', 0)} verified · {by_status.get('risky', 0)} risky · "
             f"{unconfirmed} not confirmed"
             + (f" · {by_status['invalid']} bad" if by_status.get("invalid") else ""),
             key="verify", vars={"verified": by_status.get("verified", 0),
                                 "risky": by_status.get("risky", 0),
                                 "unconfirmed": unconfirmed,
                                 "invalid": by_status.get("invalid", 0)}),
        step("Write first emails", len(drafted), len(deliverable | drafted), False,
             f"{len(drafted)} written · see Campaigns", key="write",
             vars={"written": len(drafted)}),
        step("Sent", len(sent & drafted), len(drafted), False,
             f"{len(sent & drafted)} of {len(drafted)} sent", key="sent",
             vars={"sent": len(sent & drafted), "written": len(drafted)}),
        step("Replies", replies, 0, False,
             f"{replies} replies" if replies else "No replies yet", info=True,
             key="replies", vars={"replies": replies}),
    ]

    next_tab = ""   # where the next step is done, when it's done in the dashboard
    next_vars: dict = {}
    if reading or finding:
        next_key = "working"
        next_up = "Wait — Harvey is working. This page updates by itself."
    elif finding_emails:
        next_key = "finding_emails"
        next_up = "Wait — emails are being found."
    elif stopped:
        next_key = "stopped"
        next_up, next_tab = "A job stopped before finishing. Run it again.", "discover"
    elif harvey_sends and awaiting_review:
        next_key, next_vars = "review", {"n": awaiting_review}
        next_up, next_tab = f"Review the {awaiting_review} draft(s) waiting in the Outbox.", "outbox"
    elif by_hand:
        next_key, next_vars = "by_hand", {"n": len(by_hand)}
        next_up, next_tab = f"Open Campaigns and send the {len(by_hand)} unsent email(s).", "campaigns"
    elif profiled > checked:
        next_key, next_vars = "find_emails", {"n": profiled - checked}
        next_up, next_tab = f"Find emails for the {profiled - checked} businesses not checked yet.", "controls"
    elif deliverable - drafted:
        next_key, next_vars = "write", {"n": len(deliverable - drafted)}
        next_up, next_tab = f"Write first emails for {len(deliverable - drafted)} more contact(s).", "controls"
    elif not companies:
        next_key = "start"
        next_up, next_tab = "Search for businesses to get started.", "discover"
    else:
        next_key = "more"
        next_up, next_tab = "Search more cities or industries to get more leads.", "discover"

    return {
        "steps": steps,
        "running": [dict(r, eta_text=_eta_text(r["eta_min"])) for r in running],
        "stopped": stopped,
        "next": next_up,
        "next_key": next_key,
        "next_vars": next_vars,
        "next_tab": next_tab,
        "cost_usd": round(cost, 4),
        "updated_at": now.isoformat() + "Z",
    }


def _require_approval_now() -> bool:
    try:
        from harvey.config import load_config
        return bool(load_config(str(_resolve_config_file())).channels.email.require_approval)
    except Exception:
        return True


# Why campaign addresses wait for a check (campaigns.checks_held's code).
_CHECKS_HELD = {
    "no_price": ("A verification service is past its free tier and has no price entered, "
                 "and a spend limit is set, so the check isn't made. Enter its price in "
                 "Settings → Email verification; the addresses are checked then."),
    "day": "Today's spend limit (spend.per_day_usd) is reached. Checks go on tomorrow (UTC).",
    "month": ("This month's spend limit (spend.per_month_usd) is reached. Checks go on next "
              "month, or when the limit is raised."),
}


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from harvey.signals import sync_vocabulary

        state = _state()
        await state.init_db()
        await sync_vocabulary(state)

        codes = await state.get_signal_codes(available_only=True)
        pending = await state.get_outbox(status="pending_review", limit=200)
        approved = await state.get_outbox(status="approved", limit=200)
        paused = await state.get_setting("sending_paused")
        counts = await state.count_prospects_by_status()

        convos = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'"
        )
        open_convos = convos[0]["n"] if convos else 0
        handed = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'needs_human'"
        )
        needs_human = handed[0]["n"] if handed else 0
        n_companies = (await state.read("tally")).get("firms", 0)

        unprofiled_count = await state.count_companies_needing_profile()
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_on": len(codes),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "cold_waiting": len([i for i in pending + approved if i.get("kind") == "sequence"]),
            "email_provider": _current_provider(),
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Harvey from doing anything at all.
        brain = await _brain_status()
        if brain.get("status") == "paused":
            who = "Codex" if brain.get("provider") == "codex" else "Harvey's model"
            items.append({
                "key": "brain-paused", "tone": "bad",
                "title": f"{who} is paused, so Harvey is waiting",
                "detail": (str(brain.get("reason") or "It can't be used right now.")[:300] +
                           " Replies and queued emails are untouched; Harvey resumes "
                           "once it's fixed. harvey status --check says what's wrong."),
                "action": "Open the log", "tab": "controls",
            })

        # A provider Harvey can't send or read through (Instantly, removed).
        from harvey.config import provider_notice
        provider_problem = provider_notice(_load_config_quietly())
        if provider_problem:
            items.append({
                "key": "provider", "tone": "bad",
                "title": "Choose Gmail, SMTP or Microsoft 365 to send email",
                "detail": provider_problem, "provider": _current_provider(),
                "action": "Open settings", "tab": "settings",
            })

        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
            })

        # Cold email waits until the mailbox was read back to its last
        # complete read (an unsubscribe sent while Harvey was off).
        gap = await state.read_gap()
        if gap is not None:
            items.append({
                "key": "read-gap", "tone": "bad",
                "title": "Cold email waits for a complete mailbox read",
                "detail": ("The last read didn't reach back to the one before it, so an "
                           "unsubscribe in between may be unread. Replies still go out; "
                           "cold email goes once a read covers the gap, or once you accept "
                           "it (harvey sending accept-gap)."),
                "reason": gap.get("reason") or "",
                "action": "Open the log", "tab": "controls",
            })

        # Cold email waits for the operator's acknowledgement.
        cold_waiting = [i for i in pending + approved if i.get("kind") == "sequence"]
        if cold_waiting and not await state.acknowledged():
            items.append({
                "key": "ack", "tone": "bad",
                "title": "Cold email waits for your acknowledgement",
                "detail": ("Read what sending cold email to firms means and confirm it once: "
                           "Settings → Sending, or harvey ack in a terminal."),
                "action": "Read and confirm", "tab": "settings",
            })

        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        # Cold email waiting on the optional Precleared check: its free
        # checks ran out, the key was rejected, or it couldn't be reached.
        from harvey.integrations.precleared import current_hold, hold_message
        try:
            from harvey.config import load_config
            loaded = load_config(str(_resolve_config_file()))
        except Exception:
            loaded = None  # can't judge: show the hold rather than hide it
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            items.append({
                "key": "precleared-hold", "tone": message["tone"],
                "title": message["title"], "detail": message["detail"],
                "action": "Open the log", "tab": "controls",
                "url": message["url"], "url_label": message["url_label"],
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Harvey up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if not n_companies:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Nothing has been collected yet. "
                           "Discovery is free to try — no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific — who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        # Websites a search found that nothing has checked yet (found before
        # the finder chose sites on evidence, or waiting for the classifier).
        from harvey import site_check
        to_check = await site_check.waiting(state)
        stats["sites_to_check"] = to_check
        if to_check:
            items.append({
                "key": "site-check", "tone": "warn",
                "title": f"{to_check} found websites wait for a check",
                "detail": ("A search found them, but nothing proved they are the firms' own. "
                           "The check rules out directories by their address, reads the "
                           "rest again and asks the classifier; nothing changes until you "
                           "start it."),
                "action": "Review the check", "tab": "settings",
            })

        # Harvey's agent: its wakes wait (logged out, the plan's share or
        # limit used).
        from harvey.agent import wakes
        agent_held = await wakes.held(state)
        if agent_held:
            items.append({
                "key": "agent-wakes", "tone": "warn", "paused": agent_held["paused"],
                "title": "Harvey's agent waits to look at its plans",
                "detail": agent_held["why"] + ("" if not agent_held["paused"] else
                                               " (the agent's lane is paused)"),
                "action": "Open the Queue", "tab": "queue",
            })

        # Campaign addresses waiting for a check the spend rules refuse.
        from harvey import campaigns
        held = await campaigns.checks_held(state)
        if held:
            n = await _scalar(
                "SELECT COUNT(*) FROM campaign_member m JOIN campaigns c ON c.id = m.campaign "
                "WHERE c.status = 'active' AND m.status = ?", (campaigns.M["waiting"],))
            if n:
                stats["campaign_checks_waiting"], stats["campaign_checks_held"] = n, held["code"]
                items.append({
                    "key": "campaign-checks", "tone": "warn", "code": held["code"],
                    "title": f"{n} campaign address(es) wait for a check",
                    "detail": _CHECKS_HELD.get(held["code"], held.get("why") or ""),
                    "action": "Open Settings", "tab": "settings",
                })

        if needs_human:
            items.append({
                "key": "needs-human", "tone": "bad",
                "title": (f"{needs_human} conversation needs you" if needs_human == 1
                          else f"{needs_human} conversations need you"),
                "detail": ("Harvey stopped and handed these over: an escalation, or a "
                           "reply it had no safe answer for. Reply yourself, then close "
                           "it or hand it back."),
                "action": "Read them", "tab": "conversations",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": ("Nothing sends until you approve it. Read them one at a time."
                           if _require_approval_now() else
                           "Autopilot sends the rest on its own; these wait for you. "
                           "Read them one at a time."),
                "action": "Open the decisions desk", "tab": "outbox",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Harvey is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── CAEN: Romanian activity codes, found by words ──


# ── Public data: the free official datasets kept locally ──

FIN_MODULE = "mf_financials"
async def _source_jobs(state, module_id: str) -> tuple[bool, dict | None]:
    """(whether a load or check of ``module_id``'s local copy is queued or
    running, the last load's report: unchanged, offline, downloaded,
    companies, observations, errors)."""
    from harvey.modules import jobs

    active = await query_db(
        "SELECT COUNT(*) AS n FROM jobs WHERE module = ? AND kind IN ('source_sync', "
        "'source_check') AND status IN ('queued', 'running')", (module_id,))
    last = await query_db(
        "SELECT id FROM jobs WHERE module = ? AND kind = 'source_sync' AND status IN "
        "('done', 'failed') ORDER BY rowid DESC LIMIT 1", (module_id,))
    report = None
    if last:
        job = await jobs.get(state, last[0]["id"])
        if job is not None and job.status == jobs.FAILED:
            report = {"errors": [job.error or "failed"]}
        elif job is not None:
            r = job.result
            report = {"unchanged": bool(r.get("unchanged")), "offline": r.get("offline") or "",
                      "downloaded": r.get("downloaded") or [],
                      "companies": r.get("companies", 0), "observations": r.get("observations", 0),
                      "errors": r.get("errors") or []}
    return bool(active and active[0]["n"]), report


async def _queue_source(module_id: str, kind_: str, capability: str, force: bool = False):
    """Queue a load or a check of a source's local copy: the answer, or a
    409 naming the job already waiting."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_source

    state = _state()
    await state.init_db()
    try:
        batch_id, job_id = await enqueue_source(state, kind_, module_id, capability, force=force)
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    return {"success": True, "job": job_id, "batch_id": batch_id, **await _public_data()}


def _attribution(module_id: str) -> str:
    """The credit a module's data asks for (its manifest's attribution,
    licence included): one source of truth for the panels and the CLI."""
    from harvey.modules import switches

    m = switches.by_id().get(module_id)
    return (m.attribution or m.name or m.id) if m else ""


async def _public_data() -> dict:
    """The trade registry's and the financial statements' local copies: what
    is loaded, what data.gov.ro had at the last check, the module's
    settings, and whether a load is running."""
    from harvey import registry
    from harvey.modules import switches
    from harvey.modules.ro.mf_financials import source, store
    from harvey.modules.ro.mf_financials.capabilities import updates

    state = _state()
    await state.init_db()
    on = await switches.enabled_ids(state)
    settings = await switches.module_settings(state, FIN_MODULE) if FIN_MODULE in on else {}
    loaded = await asyncio.to_thread(store.status)
    checked_at, found = source.load_catalog()
    reg = await asyncio.to_thread(registry.status)
    fin_running, fin_report = await _source_jobs(state, FIN_MODULE)
    return {
        "registry": None if reg is None else {
            k: reg.get(k) for k in ("dataset", "date", "loaded_at", "firms", "operating")},
        "registry_on": "onrc" in on,
        "taxpayers": await _taxpayers_status(state, on),
        "boundaries": await _boundaries_status(state, on),
        "anaf": await _anaf_status(state, on),
        "financials": {
            "on": FIN_MODULE in on,
            "loaded": None if loaded is None else {
                **{k: loaded.get(k) for k in ("years", "loaded_at", "load_seconds", "rows",
                                              "companies", "bytes")},
                "files": loaded.get("files") or []},
            "checked_at": checked_at,
            "published": source.published_years(found)[:8] if found else [],
            "updates": updates(found, loaded, int(settings.get("years") or 3)) if found else [],
            "settings": settings,
            "running": fin_running,
            "report": fin_report,
            "attribution": _attribution(FIN_MODULE),
        },
    }


@app.get("/api/public-data")
async def get_public_data():
    try:
        return await _public_data()
    except Exception as e:
        logger.exception("public data status failed")
        return {"error": str(e)}


async def _financials_refused() -> JSONResponse | None:
    from harvey.modules import switches

    state = _state()
    await state.init_db()
    try:
        await switches.require(state, FIN_MODULE, "Loading the financial statements")
    except switches.ModuleOff as e:
        return JSONResponse(_error_body(e, "message", success=False), status_code=409)
    return None


@app.post("/api/public-data/financials/check")
async def check_financials():
    """Queue a check of data.gov.ro for a new or revised year."""
    refused = await _financials_refused()
    if refused:
        return refused
    return await _queue_source(FIN_MODULE, "source_check", "statements_check")


@app.post("/api/public-data/financials/load")
async def load_financials(request: Request):
    """Queue a download of what is missing for the years wanted and its
    load; then the companies on file get their figures. {"force": true}
    loads again when nothing new is published."""
    refused = await _financials_refused()
    if refused:
        return refused
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(isinstance(body, dict) and body.get("force"))
    return await _queue_source(FIN_MODULE, "source_sync", "statements_sync", force=force)


@app.post("/api/public-data/financials/settings")
async def set_financials_settings(request: Request):
    """{"years": n, "auto_update": bool, "check_days": n}: any of them."""
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    allowed = ("years", "auto_update", "check_days")
    if not isinstance(body, dict) or not body or any(k not in allowed for k in body):
        return JSONResponse({"success": False,
                             "message": "expected years, auto_update or check_days"},
                            status_code=400)
    refused = await _financials_refused()
    if refused:
        return refused
    state = _state()
    for key, value in body.items():
        await switches.set_module_setting(state, FIN_MODULE, key, value)
    return {"success": True, **await _public_data()}


TAX_MODULE = "mf_taxpayers"
ANAF_MODULE = "anaf"
BOUNDARIES_MODULE = "boundaries"


async def _boundaries_status(state, on: set[str]) -> dict:
    """The boundaries' local copy: what is loaded (units by admin level,
    the regions, when), the regions a load takes and what is downloaded
    of them, whether pyosmium is there, whether a load is running."""
    from harvey.modules import switches
    from harvey.modules.api import Context
    from harvey.modules.original.boundaries import capabilities as bcap
    from harvey.modules.original.boundaries import source, store

    m = switches.by_id().get(BOUNDARIES_MODULE)
    if m is None:
        return {"on": False}
    settings = await switches.module_settings(state, BOUNDARIES_MODULE) \
        if BOUNDARIES_MODULE in on else {}
    loaded = await asyncio.to_thread(store.status)
    try:
        regions = bcap.regions(Context(settings=settings, enabled=frozenset(on)))
    except ValueError:
        regions = []
    files = []
    for region in regions:
        path = source.downloaded(region)
        files.append({"region": region, "file": path.name if path else "",
                      "day": path.parent.name if path else "",
                      "bytes": path.stat().st_size if path else None})
    running, report = await _source_jobs(state, BOUNDARIES_MODULE)
    return {
        "on": BOUNDARIES_MODULE in on,
        "loaded": None if loaded is None else {
            k: loaded.get(k) for k in ("regions", "loaded_at", "load_seconds", "units",
                                       "levels", "bytes")},
        "regions": regions,
        "downloads": files,
        "size_note": (m.cost or {}).get("note", ""),
        "library": store.library_problem(),
        "running": running,
        "report": report,
        "attribution": _attribution(BOUNDARIES_MODULE),
    }


@app.post("/api/public-data/boundaries/load")
async def load_boundaries(request: Request):
    """Queue a download of each region's OpenStreetMap extract when none is
    on disk, and the load of its boundaries. {"force": true}: a fresh
    download and load."""
    from harvey.modules.original.boundaries import store

    refused = await _module_refused(BOUNDARIES_MODULE, "Loading the boundaries")
    if refused:
        return refused
    if store.library_problem():
        return JSONResponse({"success": False, "message": store.library_problem(),
                             "code": "library_missing"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(isinstance(body, dict) and body.get("force"))
    return await _queue_source(BOUNDARIES_MODULE, "source_sync", "boundaries_sync", force=force)



async def _taxpayers_status(state, on: set[str]) -> dict:
    """The taxpayer file's local copy: what is loaded, what data.gov.ro had
    at the last check, the module's settings, whether a load is running."""
    from harvey.modules import switches
    from harvey.modules.ro.mf_taxpayers import source, store
    from harvey.modules.ro.mf_taxpayers.capabilities import update

    loaded = await asyncio.to_thread(store.status)
    checked_at, found = source.load_catalog()
    running, report = await _source_jobs(state, TAX_MODULE)
    return {
        "on": TAX_MODULE in on,
        "loaded": None if loaded is None else {
            k: loaded.get(k) for k in ("year", "published", "loaded_at", "load_seconds", "rows",
                                       "with_phone", "vat", "employers", "micro", "bytes")},
        "checked_at": checked_at,
        "published": found[0].year if found else None,
        "update": update(found, loaded) if found else None,
        "settings": await switches.module_settings(state, TAX_MODULE) if TAX_MODULE in on else {},
        "running": running,
        "report": report,
        "attribution": _attribution(TAX_MODULE),
    }


async def _anaf_status(state, on: set[str]) -> dict:
    """ANAF's company check: the answers kept, whether a check is running
    and how far it got, the last check's report."""
    from harvey.modules import switches
    from harvey.modules.ro.anaf import service

    running, job = await _anaf_batch(state)
    return {
        "on": ANAF_MODULE in on,
        "kept": await asyncio.to_thread(service.status),
        "settings": await switches.module_settings(state, ANAF_MODULE) if ANAF_MODULE in on else {},
        "running": running,
        "job": job,
        "attribution": _attribution(ANAF_MODULE),
    }


async def _anaf_batch(state) -> tuple[bool, dict | None]:
    """Whether checks at ANAF are queued or running, and the latest
    check's progress ({done, total, report, started_at}: companies, its
    jobs' reports added up)."""
    from harvey.modules import jobs
    from harvey.modules.runs import _new_check_report

    active = await query_db("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'id_check' AND "
                            "module = ? AND status IN ('queued', 'running')", (ANAF_MODULE,))
    last = await query_db("SELECT batch_id FROM jobs WHERE kind = 'id_check' AND module = ? "
                          "ORDER BY rowid DESC LIMIT 1", (ANAF_MODULE,))
    if not last:
        return bool(active and active[0]["n"]), None
    found = [j for j in await jobs.list_jobs(state, batch_id=last[0]["batch_id"], limit=100000)
             if j.kind == "id_check" and j.module == ANAF_MODULE]
    report = _new_check_report(ANAF_MODULE)
    done = total = 0
    for j in found:
        n = len(j.params.get("company_ids") or [])
        total += n
        if j.status == jobs.DONE:
            done += n
            for key, value in j.result.items():
                if isinstance(value, int) and not isinstance(value, bool) and key in report:
                    report[key] += value
            report["stopped"] = j.result.get("stopped") or report["stopped"]
        elif j.status == jobs.FAILED:
            done += n
            report["errors"].append(j.error)
        else:
            # A job part way (ten requests each): the companies it has saved.
            done += min(n, int((j.progress or {}).get("done") or 0))
    report["errors"] = report["errors"][:3]
    started = min((j.created_at or "" for j in found), default="")
    return bool(active and active[0]["n"]), {"done": done, "total": total, "report": report,
                                             "started_at": started}


async def _module_refused(module_id: str, what: str) -> JSONResponse | None:
    from harvey.modules import switches

    state = _state()
    await state.init_db()
    try:
        await switches.require(state, module_id, what)
    except switches.ModuleOff as e:
        return JSONResponse(_error_body(e, "message", success=False), status_code=409)
    return None


@app.post("/api/public-data/taxpayers/check")
async def check_taxpayers():
    """Queue a check of data.gov.ro for a newer taxpayer file."""
    refused = await _module_refused(TAX_MODULE, "Checking the taxpayer file")
    if refused:
        return refused
    return await _queue_source(TAX_MODULE, "source_check", "taxpayers_check")


@app.post("/api/public-data/taxpayers/load")
async def load_taxpayers(request: Request):
    """Queue a download of the newest taxpayer file when missing and its
    load; then the companies on file get its readings. {"force": true}
    loads again when nothing new is published."""
    refused = await _module_refused(TAX_MODULE, "Loading the taxpayer file")
    if refused:
        return refused
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(isinstance(body, dict) and body.get("force"))
    return await _queue_source(TAX_MODULE, "source_sync", "taxpayers_sync", force=force)


@app.post("/api/public-data/taxpayers/settings")
async def set_taxpayers_settings(request: Request):
    """{"auto_update": bool, "check_days": n}: either of them."""
    from harvey.modules import switches

    try:
        body = await request.json()
    except Exception:
        body = None
    allowed = ("auto_update", "check_days")
    if not isinstance(body, dict) or not body or any(k not in allowed for k in body):
        return JSONResponse({"success": False, "message": "expected auto_update or check_days"},
                            status_code=400)
    refused = await _module_refused(TAX_MODULE, "Changing the taxpayer file's settings")
    if refused:
        return refused
    state = _state()
    for key, value in body.items():
        await switches.set_module_setting(state, TAX_MODULE, key, value)
    return {"success": True, **await _public_data()}


@app.post("/api/anaf/check")
async def check_at_anaf(request: Request):
    """Queue checks of companies at ANAF: {"ids": [...]} (a selection, or
    one company), {"q": "..."} (every match of a search), {"missing": true}
    (every company never checked) or {} (every company with a CUI, kept
    answers reused). A hundred companies a job on ANAF's lane, one request
    a second; the progress is in /api/public-data's ``anaf.job``."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_id_checks

    refused = await _module_refused(ANAF_MODULE, "Checking companies at ANAF")
    if refused:
        return refused
    try:
        ids, q = await _export_request(request)
    except ValueError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    missing = bool(isinstance(body, dict) and body.get("missing"))
    state = _state()
    await state.init_db()
    if ids is None and q.strip():
        ids, request.state.matched = await _search_all_ids(state, q)
    batch_id = await jobs.new_batch(state, "id_check", "ANAF", params={"missing": missing})
    queued, dupes = await enqueue_id_checks(state, ANAF_MODULE, "company_check", ids,
                                            missing_only=missing, batch_id=batch_id)
    if not queued:
        # Nothing new queued: no empty run left behind.
        await jobs.drop_batch(state, batch_id)
    if not queued and dupes:
        return JSONResponse(_duplicate_body(jobs.Duplicate(dupes[0])), status_code=409)
    out = {"success": True, "batch_id": batch_id, "jobs": len(queued),
           "already_queued": len(dupes)}
    matched = getattr(request.state, "matched", None)
    if ids is not None and matched and matched > len(ids):
        # A filtered list longer than the cap: only its first ids were queued.
        out.update(matched=matched, capped=len(ids))
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@app.get("/api/caen/search")
async def search_caen(q: str = "", limit: int = 50):
    """CAEN classes for some words (offline, no Claude), plus the codes a
    registry filter should use for them."""
    from harvey.caen import codes_for, revision_label, search

    q = q[:200]
    matches = search(q, limit=max(1, min(limit, 200)))
    return {
        "query": q,
        "matches": [{**m._asdict(), "revision": revision_label(m.version)}
                    for m in matches],
        "codes": sorted(codes_for(q)),
    }


# ── Signals: the facts Harvey collects ──
#
# Read only. A module's switch says what is collected; a paid source spends
# only inside its run's cap. The Contacts and Companies filters list these
# for their require/exclude choices.

@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, one group per module that declares signals,
    with how many companies carry each and how many were checked either
    way. Signals of a module that is off are left out (their observations
    stay); ``hidden`` counts them."""
    try:
        from harvey.modules import switches
        from harvey.signals import absence, sync_vocabulary

        state = _state()
        await state.init_db()
        # Idempotent, so safe on every load: signals shipped in an upgrade
        # show up without any migration step.
        await sync_vocabulary(state)

        every = await state.get_signal_codes()
        codes = [c for c in every if c.get("available", 1)]
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        # True / false / unknown per company: "unknown" is not checked yet.
        states = await state.signal_states([sig["code"] for sig in codes])

        groups = []
        for m in switches.manifests():
            rows = []
            for sig in codes:
                if sig.get("module") != m.id:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                    "states": states.get(sig["code"], {}),
                    "absence": absence(sig["code"]),
                })
            if rows:
                groups.append({"key": m.id, "label": m.name or m.id,
                               "blurb": m.description, "signals": rows})
        return {"groups": groups, "total": len(codes), "hidden": len(every) - len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": []}


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        runs = await state.get_runs(limit=25)
        # Same rule as the live panel: a "running" run that has recorded
        # nothing for STALE_RUN_SECONDS has stopped, whatever its row says.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for run in runs:
            if run.get("status") != "running":
                continue
            # (Unix seconds: the obs time, by the run's batch.)
            at = await state.read("run_last_at", run["id"])
            last = (datetime.fromtimestamp(at, timezone.utc).replace(tzinfo=None)
                    if at else None)
            last = last or _parse_ts(run.get("started_at"))
            if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
                run["status"] = "stalled"
        return runs
    except Exception as e:
        return {"error": str(e)}


WEB_DIR = (Path(__file__).resolve().parent / "web")


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN — this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


class DashboardSetupError(Exception):
    """The dashboard can't start safely; the message says what to do."""


def _check_loopback_bind(host: str) -> str:
    """The host to bind, which must be a loopback address. The dashboard has
    no remote-access mode: from another machine, use an SSH tunnel."""
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not auth.is_loopback(name):
        raise DashboardSetupError(
            f"Refusing to listen on {host!r}: the dashboard only listens on this machine "
            "(127.0.0.1, ::1 or localhost). To reach it from elsewhere, tunnel instead: "
            "ssh -L 5555:127.0.0.1:5555 <this-host>")
    return name


def dashboard_url(host: str = "127.0.0.1", port: int = 5555) -> str:
    """The unlock link. The token rides in the #fragment, which browsers
    never send to the server (so it stays out of logs and Referer); the
    page moves it to sessionStorage and strips it from the address bar."""
    name = _check_loopback_bind(host)
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    return f"http://{name}:{port}/#token={token_store.get()}"


# How long Ctrl-C waits for open requests (a chat's stream among them)
# before cancelling them.
GRACEFUL_SHUTDOWN_SECONDS = 5


def _server_class():
    import uvicorn

    class Server(uvicorn.Server):
        """uvicorn's server, the chats closed first on shutdown: each answer
        being made ends (its stream with an error event), so an open stream
        doesn't hold the graceful wait."""

        async def shutdown(self, sockets=None):
            await _close_chats()
            await super().shutdown(sockets)

    return Server


async def _close_chats() -> None:
    try:
        from harvey.agent import chat

        await chat.close_all()
    except Exception:
        logger.exception("the chats' processes didn't close cleanly")


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server (loopback only, token required)."""
    import uvicorn

    try:
        bind = _check_loopback_bind(host)
        url = dashboard_url(host, port)
    except (DashboardSetupError, auth.TokenError) as e:
        raise DashboardSetupError(str(e)) from e

    print(f"\n  Harvey Dashboard running. Open this link to unlock it:\n\n    {url}\n")
    print(f"  The token is stored in {token_store.path} (owner-only). Keep the link")
    print("  private; anyone with it can use the dashboard. Press Ctrl+C to stop.\n")
    if os.name == "nt":
        print("  Windows: the token file is protected by your folder's permissions,")
        print("  not POSIX file modes.\n")
    config = uvicorn.Config(app, host=bind, port=port, log_level="warning",
                            proxy_headers=False, server_header=False, date_header=False,
                            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
    _server_class()(config).run()
