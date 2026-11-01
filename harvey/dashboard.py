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
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
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
    # Bring an existing database up to the current schema before the first
    # request: pages that only read (Prezentare) never upgrade it, and the
    # first one opened after an update would read tables that don't exist
    # yet. A missing database stays missing until something writes.
    if DB_PATH.exists():
        from harvey.state import StateManager
        try:
            await StateManager(str(DB_PATH)).init_db()
        except Exception:
            logger.exception("could not upgrade %s", DB_PATH)
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
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30)
            except Exception:
                logger.exception("the job worker didn't stop cleanly")


async def _run_worker(stop: asyncio.Event):
    from harvey.modules.worker import Worker

    while not stop.is_set():
        if DB_PATH.exists():
            try:
                state = _state()
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
    global _harvey_process, _harvey_started_at
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
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
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
        # Instantly warms its own accounts, so there it's a suggestion, not a step.
        "required": provider != "instantly",
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Harvey shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
        "scope": "email",
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
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


# Keys a module reads that Settings never writes: TrulyInbox's are saved by
# its own connect flow, which checks the key against the mailbox first.
ENV_OWN_FLOW = ("TRULYINBOX_API_KEY", "TRULYINBOX_WORKSPACE_ID")


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
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
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


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


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
    from harvey.collectors.phones import phone_region
    from harvey.export import _EMAIL_STATUS_RANK, _phone_columns
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return rows
    # The same suppression as the exports: an address (or a whole domain) on
    # compliance.do_not_contact is never offered here either.
    blocked = _blocked_entries(config)
    marks = _in(ids)
    site_phones: dict[str, list[str]] = {}
    official = _official_modules()
    found = await query_db(
        "SELECT company_id, value_text, module FROM observations WHERE signal_code = 'PHONE_NUMBER' "
        f"AND value_text != '' AND company_id IN ({marks}) ORDER BY observed_at, rowid",
        tuple(ids))
    # An official register's numbers first, then the rest as they were found.
    for r in sorted(found, key=lambda r: (r.get("module") or "") not in official):
        numbers = site_phones.setdefault(r["company_id"], [])
        if r["value_text"] not in numbers:
            numbers.append(r["value_text"])
    # Every number any source gave (a listing's, a merged record's): kept,
    # shown after the main one.
    for r in await query_db(
            "SELECT company_id, value FROM company_ids WHERE kind = 'phone' "
            f"AND company_id IN ({marks}) ORDER BY created_at, rowid", tuple(ids)):
        numbers = site_phones.setdefault(r["company_id"], [])
        if r["value"] not in numbers:
            numbers.append(r["value"])
    people: dict[str, list[dict]] = {}
    for r in await query_db(
            "SELECT company_id, value_text FROM observations WHERE signal_code = 'CONTACT_FOUND' "
            f"AND value_text != '' AND company_id IN ({marks}) ORDER BY observed_at, rowid",
            tuple(ids)):
        found = people.setdefault(r["company_id"], [])
        person = _split_person(r["value_text"])
        if person["name"] and person not in found:
            found.append(person)
    contacts: dict[str, list[dict]] = {}
    for p in await query_db(
            "SELECT company_id, first_name, last_name, title, email, email_status, phone, "
            "score, status FROM prospects "
            f"WHERE company_id IN ({marks}) ORDER BY score DESC, created_at", tuple(ids)):
        contacts.setdefault(p["company_id"], []).append(p)
    activities = {}
    try:
        state = _state()
        await state.init_db()
        activities = await state.get_company_activities(ids)
    except Exception as e:
        logger.warning("company activities failed: %s", e)

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

_COMPANY_COLUMNS = ("c.id, c.name, c.cui, c.domain, c.website, c.industry, c.company_size, "
                    "c.location, c.address, c.phone, c.source, c.source_url, c.description, "
                    "c.external_id, c.created_at, c.updated_at")


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
    from harvey import companies

    if not request.query_params:
        return await query_db("""
            SELECT c.*,
                (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
            FROM companies c WHERE COALESCE(c.merged_into, '') = ''
            ORDER BY c.created_at DESC LIMIT 200
        """)
    limit, offset = _page_args(limit, offset)
    args = dict(request.query_params)
    if args.get("q") and not args.get("search"):
        args["search"] = args["q"]
    args.pop("q", None)
    empty = {"total": 0, "offset": offset, "limit": limit, "sort": sort, "dir": dir,
             "companies": [], "sources": [], "financials": False}
    if not DB_PATH.exists():
        return empty
    state = _state()
    await state.init_db()
    try:
        f, attach = await _contact_filter(state, args)
    except ValueError as e:
        return JSONResponse({**empty, "error": str(e)}, status_code=400)
    from harvey.modules.ro.mf_financials import store as fin_store
    has_fin = Path(fin_store.FINANCIALS_PATH).is_file()
    relevance = sort in ("", "relevance") and bool(f.search)
    if not relevance and sort not in companies.SORTS:
        sort = "created"
    if sort in companies.FIGURE_SORTS and not has_fin:
        sort = "created"
    way = companies.direction(sort, dir)
    async with _contacts_db(attach or has_fin) as db:
        db.row_factory = aiosqlite.Row
        started = time.monotonic()
        if relevance:
            sort = "relevance"
        total, rowids, how = await companies.page(db, f, sort, way, limit, offset,
                                                  figures_mark=_figures_mark())
        rows = []
        if rowids:
            marks = ",".join("?" * len(rowids))
            async with db.execute(f"SELECT c.rowid AS _r, {_COMPANY_COLUMNS} FROM companies c "
                                  f"WHERE c.rowid IN ({marks})", rowids) as cur:
                found = {r["_r"]: dict(r) for r in await cur.fetchall()}
            rows = [found[i] for i in rowids if i in found]
            for r in rows:
                r.pop("_r", None)
        figures = await companies.figures(db, [r["cui"] for r in rows]) if has_fin else {}
        extra = await companies.counts(db, [r["id"] for r in rows])
        sources = await companies.sources(db)
        took = round((time.monotonic() - started) * 1000)
    rows = await _enrich_companies(rows, _load_config_quietly())
    for r in rows:
        r["figures"] = figures.get(r.get("cui") or "")
        r.update(extra.get(r["id"], {}))
    return {**empty, "total": total, "sort": sort, "dir": way, "companies": rows,
            "sources": sources, "financials": has_fin, "paged_by": how, "ms": took}


def _figures_mark():
    """When the financial statements database last changed (a reload)."""
    from harvey.modules.ro.mf_financials import store as fin_store
    try:
        return Path(fin_store.FINANCIALS_PATH).stat().st_mtime_ns
    except OSError:
        return None


_CUI_QUERY = re.compile(r"\s*(?:RO\s*)?(\d{2,10})\s*", re.IGNORECASE)


async def _company_search(state, q: str, limit: int, offset: int) -> tuple[int, list[dict]]:
    """The local search, plus a CUI typed on its own ("10004070", "RO
    10004070"): when a company has that tax id, it is the answer. A bare
    four-digit number stays a CAEN code unless written with RO."""
    m = _CUI_QUERY.fullmatch(q or "")
    if m and (len(m.group(1)) != 4 or q.strip().upper().startswith("RO")):
        digits = m.group(1)
        hits = await query_db(
            "SELECT id, name, cui, location, address, phone, website, domain, industry, source "
            f"FROM companies WHERE {_LIVE} AND cui != '' "
            "AND REPLACE(UPPER(REPLACE(cui, ' ', '')), 'RO', '') = ? "
            "ORDER BY name", (digits,))
        if hits:
            activities = await state.get_company_activities([h["id"] for h in hits])
            for h in hits:
                h["activities"] = activities.get(h["id"], [])
            return len(hits), hits[offset:offset + limit]
    return await state.search_companies(q, limit=limit, offset=offset)


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
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


@app.get("/api/companies/{company_id}")
async def get_company(company_id: str):
    """One company for the detail panel: its record, every phone and email
    on file, the named people, its activities in words, and ``findings``:
    every phone and address any source gave with who gave it and when,
    and the latest official facts (official sources first)."""
    rows = await query_db(f"SELECT {_COMPANY_COLUMNS} FROM companies c WHERE c.id = ?",
                          (company_id,))
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    (row,) = await _enrich_companies(rows, _load_config_quietly())
    try:
        row["findings"] = await _company_findings(company_id, row)
    except Exception as e:
        logger.warning("company findings failed: %s", e)
        row["findings"] = {"phones": [], "addresses": [], "facts": []}
    try:
        from harvey import contacts
        async with _contacts_db() as db:
            row["reach"] = await contacts.of_company(db, company_id)
    except Exception as e:
        logger.warning("company contacts failed: %s", e)
        row["reach"] = {"contacts": [], "people": [], "locations": [], "sources": []}
    try:
        from harvey import companies
        async with _contacts_db() as db:
            row["record"] = await companies.record(db, row)
            if row["record"]["financials"]["state"] != companies.OK:
                years = await companies.observed_years(db, company_id)
                if years:
                    row["record"]["financials"].update(state=companies.OK, years=years,
                                                       from_observations=True)
            row.update((await companies.counts(db, [company_id]))[company_id])
    except Exception as e:
        logger.warning("company record failed: %s", e)
        row["record"] = None
    return row


def _official_modules() -> set[str]:
    from harvey.modules import switches

    return {m.id for m in switches.manifests() if m.official}


async def _company_findings(company_id: str, company: dict) -> dict:
    """Every phone and address on file for a company, each with every
    source that gave it (module, when, confidence, whether it is an
    official register), official sources first, then the newest; and the
    latest reading of each fact an official source gave (a yearly one's
    latest year)."""
    from harvey.modules import switches

    mans = switches.by_id()
    official = {m for m, man in mans.items() if man.official}

    def who(module: str, collector: str) -> str:
        m = mans.get(module or "")
        return (m.name or m.id) if m else (collector or module or "")

    rows = await query_db(
        "SELECT signal_code, value_num, value_text, module, collector, confidence, "
        "observed_at, evidence_url FROM observations WHERE company_id = ? "
        "ORDER BY observed_at DESC, rowid DESC", (company_id,))
    lists: dict[str, dict[str, dict]] = {"PHONE_NUMBER": {}, "ADDRESS": {}}
    for r in rows:
        code = r["signal_code"]
        if code not in lists or not (r["value_text"] or "").strip():
            continue
        entry = lists[code].setdefault(r["value_text"], {"value": r["value_text"], "sources": []})
        entry["sources"].append({
            "module": r["module"] or "", "name": who(r["module"], r["collector"]),
            "official": (r["module"] or "") in official, "observed_at": r["observed_at"],
            "confidence": r["confidence"], "evidence_url": r["evidence_url"] or ""})
    address = (company.get("address") or "").strip()
    source = (company.get("source") or "")
    if address and address not in lists["ADDRESS"]:
        module = source.split(":", 1)[1] if source.startswith("registry:") else ""
        lists["ADDRESS"][address] = {"value": address, "sources": [{
            "module": module, "name": who(module, source), "official": module in official,
            "observed_at": company.get("updated_at") or company.get("created_at") or "",
            "confidence": 1.0, "evidence_url": company.get("source_url") or ""}]}

    def ordered(entries: dict) -> list[dict]:
        out = list(entries.values())
        for e in out:
            # Official first, then the newest (two stable sorts).
            e["sources"].sort(key=lambda x: x["observed_at"] or "", reverse=True)
            e["sources"].sort(key=lambda x: not x["official"])
            e["official"] = any(x["official"] for x in e["sources"])
            e["latest"] = max((x["observed_at"] or "" for x in e["sources"]), default="")
        out.sort(key=lambda e: e["latest"], reverse=True)
        out.sort(key=lambda e: not e["official"])
        return out

    from harvey.collectors.phones import phone_hit

    phones = ordered(lists["PHONE_NUMBER"])
    for e in phones:
        hit = phone_hit(e["value"])
        e["display"] = hit.display if hit else e["value"]
    codes = {r["code"]: r for r in await query_db(
        "SELECT code, label, value_type, unit, keyed_by FROM signal_codes")}
    facts: dict[str, dict] = {}
    for r in rows:
        code, module = r["signal_code"], r["module"] or ""
        if module not in official or code in lists or code == "ACTIVITY_CODE":
            continue
        meta = codes.get(code) or {}
        year = r["value_text"] if meta.get("keyed_by") == "year" else ""
        old = facts.get(code)
        if old is not None and not (year.isdigit() and old["year"].isdigit()
                                    and int(year) > int(old["year"])):
            continue
        facts[code] = {
            "code": code, "label": meta.get("label") or code,
            "value_type": meta.get("value_type") or "text", "unit": meta.get("unit") or "",
            "value_num": r["value_num"], "value_text": "" if year else r["value_text"],
            "year": year, "module": module, "name": who(module, r["collector"]),
            "observed_at": r["observed_at"], "evidence_url": r["evidence_url"] or ""}
    order = [m.id for m in switches.manifests()]
    fact_list = sorted(facts.values(), key=lambda f: (
        order.index(f["module"]) if f["module"] in order else len(order), f["code"]))
    return {"phones": phones, "addresses": ordered(lists["ADDRESS"]), "facts": fact_list}


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
    out: dict[str, str] = {}
    rows = []
    if cuis:
        marks = _in(cuis)
        rows += await query_db(
            "SELECT i.value AS cui, c.id, COALESCE(c.merged_into, '') AS merged_into "
            f"FROM company_ids i JOIN companies c ON c.id = i.company_id "
            f"WHERE i.kind = 'cui' AND i.value IN ({marks})", tuple(cuis))
        rows += await query_db(
            "SELECT cui, id, COALESCE(merged_into, '') AS merged_into FROM companies "
            f"WHERE cui != '' AND cui IN ({marks})", tuple(cuis))
    if keys:
        rows += await query_db(
            "SELECT external_id AS cui, id, COALESCE(merged_into, '') AS merged_into "
            f"FROM companies WHERE external_id IN ({_in(keys)})", tuple(keys))
    for r in rows:
        if r["cui"] not in out or not r["merged_into"]:
            out[r["cui"]] = r["merged_into"] or r["id"]
    return out


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
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

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


def _active_provider() -> str:
    brain = _active_brain()
    return brain.provider if brain else ""


def _quota_client_for(provider: str, model: str = ""):
    """One cached quota reader per provider (and Codex model). Each reads
    only its own subscription, so a Claude gauge is never shown as Codex or
    vice versa, and Codex shows the bucket metering the configured model."""
    key = (provider, model if provider == "codex" else "")
    if key not in _quota_clients:
        if provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            _quota_clients[key] = CodexQuotaClient(CodexClient(model=model))
        elif provider == "claude":
            from harvey.integrations.quota import QuotaClient
            _quota_clients[key] = QuotaClient()
        else:
            return None
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
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "")
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
        "quota_status": ("available" if quota else "unavailable") if provider else "",
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
    where, params = _contact_search(q)
    base = "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id"
    searched = (" WHERE " + " AND ".join(where)) if where else ""
    counts = {"all": await _scalar(f"SELECT COUNT(*) AS n {base}{searched}", tuple(params))}
    for key, cond in _CONTACT_BUCKETS.items():
        counts[key] = await _scalar(
            f"SELECT COUNT(*) AS n {base} WHERE {cond}" + (" AND " + " AND ".join(where) if where else ""),
            tuple(params))
    bucket = _CONTACT_BUCKETS.get(filter)
    conds = where + ([bucket] if bucket else [])
    clause = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = await query_db(
        "SELECT p.*, COALESCE(c.phone, '') AS company_phone, COALESCE(c.location, '') AS "
        "company_location, COALESCE(c.cui, '') AS company_cui, COALESCE(c.name, '') AS company_name "
        f"{base}{clause} ORDER BY p.created_at DESC, p.rowid DESC LIMIT ? OFFSET ?",
        (*params, limit, offset))
    undone = await _opt_out_undone_ids()
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return {"total": counts[filter] if bucket else counts["all"],
            "offset": offset, "limit": limit, "filter": filter if bucket else "all",
            "counts": counts, "prospects": rows}


def _state():
    from harvey.state import StateManager
    return StateManager(db_path=str(DB_PATH))


# ── Contacts: every way to reach a company (harvey/contacts.py) ──

CONTACT_KINDS_EXTRA = ("mobile",)
CONTACT_PAGE_MAX = 200
CONTACT_EXPORT_MAX = 500_000


def _number_arg(raw):
    """A bound typed in a filter box: a number, or None when empty."""
    if raw is None or str(raw).strip() == "":
        return None
    return _range_bound(str(raw).strip())


def _caen_arg(raw: str) -> tuple[str, ...]:
    """CAEN codes from a filter box: 4-digit codes (also written 86.23 or
    8623/3) and words, turned into the classes they name. ValueError when
    words name no class."""
    if not raw:
        return ()
    from harvey import caen

    text = re.sub(r"\b(\d{2})\.(\d{2})\b", r"\1\2", raw)
    text = re.sub(r"\b(\d{4})/\d+\b", r"\1", text)
    codes, words = [], []
    for token in re.split(r"[\s,;]+", text):
        if re.fullmatch(r"\d{4}", token):
            codes.append(token)
        elif re.fullmatch(r"\d{3}", token):
            # A CAEN group (47.1): every class in it.
            group = sorted({code for code, _ in caen._classes() if code.startswith(token)})
            if not group:
                raise ValueError(f"No CAEN class is in group {token}")
            codes += group
        elif token:
            words.append(token)
    if words:
        named = sorted(caen.codes_for(" ".join(words)))
        if not named:
            raise ValueError(f"No CAEN class matches \"{' '.join(words)}\"")
        codes += named
    return tuple(dict.fromkeys(codes))


def _contact_arg(raw: str) -> str:
    """has-contact filter: any, none, mobile or a contact kind."""
    if raw and not re.fullmatch(r"[a-z_]{1,32}", raw):
        raise ValueError("contact must be any, none, mobile or a contact kind")
    return raw


def _search_arg(raw: str) -> tuple[str, str, bool]:
    """(CUI digits, full-text expression, nothing) from the Companies search
    box. A number typed on its own ("10004070", "RO 10004070"; a bare
    four-digit number stays a CAEN code) gives both: the CUI when a company
    has it, else the words (the caller decides, _contact_filter). Only
    single letters or common words: ``nothing`` (no company matches).
    ValueError when only -words are typed (nothing to look for)."""
    if not raw.strip():
        return "", "", False
    from harvey.state import search_terms

    match, _ = search_terms(raw)
    m = _CUI_QUERY.fullmatch(raw)
    if m and (len(m.group(1)) != 4 or raw.strip().upper().startswith("RO")):
        return m.group(1), match, False
    if not match:
        if all(t.startswith("-") for t in raw.split()):
            raise ValueError("Type a word to look for: a word with a minus only leaves things out.")
        return "", "", True
    return "", match, False


async def _cui_on_file(state, cui: str) -> bool:
    """Whether a live company has this CUI (the search box's number)."""
    from harvey import contacts

    conds, params = contacts.company_conditions(contacts.ContactFilter(cui=cui))
    async with state._connect() as db:
        async with db.execute(f"SELECT 1 FROM companies c WHERE {' AND '.join(conds)} LIMIT 1",
                              params) as cur:
            return await cur.fetchone() is not None


async def _contact_filter(state, args) -> tuple[object, bool]:
    """The ContactFilter the query string asks for, and whether the
    financials database must be attached. Raises ValueError (or ModuleOff
    for a hidden signal) on a bad argument."""
    from harvey import contacts

    get = lambda k: (args.get(k) or "").strip()[:200]  # noqa: E731
    f = contacts.ContactFilter(
        kind=get("kind"), status=get("status"), trust=get("trust"),
        has_person=get("person"), source=get("source"), county=get("county"),
        locality=get("locality"), website=get("website"), q=get("q"),
        caen=_caen_arg(get("caen")), contact=_contact_arg(get("contact")),
        found_in=get("found_in"))
    f.cui, f.search, f.nothing = _search_arg(get("search"))
    if f.county or f.locality:
        from harvey import companies

        async with state._connect() as db:
            f.places = await companies.places(db, f.county, f.locality)
    if f.cui:
        # The company with that CUI, else the number as a word ("24").
        if await _cui_on_file(state, f.cui):
            f.search = ""
        else:
            f.cui = ""
            f.nothing = not f.search
    require, exclude = _codes(args.get("require") or ""), _codes(args.get("exclude") or "")
    if require or exclude:
        await _refuse_hidden_signals(state, [*require, *exclude])
        f.cohort = state._verdict_sql(require, exclude, [], 0.0)
    bounds = {k: _number_arg(args.get(k)) for k in
              ("min_turnover", "max_turnover", "min_employees", "max_employees")}
    attach = False
    if any(v is not None for v in bounds.values()):
        from harvey.modules.ro.mf_financials import store as fin_store

        figures = fin_store.Filter(**bounds, unknown="include" if get("unknown") == "include"
                                   else "exclude")
        if Path(fin_store.FINANCIALS_PATH).is_file():
            f.figures = figures.in_sql("c.cui")
            attach = True
        elif figures.unknown != "include":
            # No statements on file: no company has a known figure.
            f.figures = ("0", [])
    return f, attach


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


async def _capped_count(db, with_sql: str, body: str, params: list) -> tuple[int, bool]:
    from harvey.contacts import COUNT_CAP

    async with db.execute(f"{with_sql}SELECT COUNT(*) FROM (SELECT 1 {body} LIMIT ?)",
                          (*params, COUNT_CAP + 1)) as cur:
        (n,) = await cur.fetchone()
    return min(n, COUNT_CAP), n > COUNT_CAP


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
    from harvey import contacts

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
    from harvey.modules.ro.mf_financials import store as fin_store
    empty["financials"] = Path(fin_store.FINANCIALS_PATH).is_file()
    if people:
        with_sql, body, params = contacts.people_query(f)
        order = contacts.order_by(contacts.PEOPLE_SORTS, sort, dir, "first_seen", "p.id")
        select = ("SELECT p.id, p.name, p.role, p.source, p.first_seen, "
                  f"{contacts.PERSON_SEARCHED} AS searched_at, "
                  "c.id AS company_id, c.name AS company_name, c.location, c.cui, c.domain ")
    else:
        with_sql, body, params = contacts.contact_query(f)
        order = contacts.order_by(contacts.CONTACT_SORTS, sort, dir, "first_seen", "ct.id")
        select = ("SELECT ct.id, ct.kind, ct.value, ct.display, ct.line_type, ct.status, "
                  "ct.do_not_contact, ct.first_seen, ct.last_seen ")
    async with _contacts_db(attach) as db:
        db.row_factory = aiosqlite.Row
        total, more = await _capped_count(db, with_sql, body, params)
        async with db.execute(f"{with_sql}{select}{body}{order} LIMIT ? OFFSET ?",
                              (*params, limit, offset)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not people:
            found = await contacts.summaries(db, [r["id"] for r in rows])
            for r in rows:
                r.update(found.get(r["id"], {}))
                r["do_not_contact"] = bool(r["do_not_contact"])
        async with db.execute(
                "SELECT DISTINCT source FROM people WHERE source != '' ORDER BY source" if people
                else "SELECT DISTINCT source FROM company_contacts WHERE source != '' "
                     "ORDER BY source") as cur:
            sources = [r[0] for r in await cur.fetchall()]
        async with db.execute("SELECT kind, label, channel FROM contact_kinds "
                              "WHERE available = 1 OR kind IN (SELECT DISTINCT kind FROM contacts) "
                              "ORDER BY rowid") as cur:
            kinds = [dict(r) for r in await cur.fetchall()]
    blocked = _blocked_entries(_load_config_quietly()) if not people else []
    if blocked:
        from harvey.gate import do_not_contact_match
        for r in rows:
            if r["kind"] == "email" and do_not_contact_match(r["value"], blocked):
                r["do_not_contact"] = True
    return {**empty, "total": total, "more": more, "rows": rows, "sources": sources,
            "kinds": kinds}


# A Find-contacts run takes at most this many companies (the newest first);
# a wider filter is taken in more runs.
FIND_MAX = 50_000


async def _find_selection(body: dict):
    """(state, the companies a Find-contacts request selects, its filter's
    error or "") from {"filter": {the Contacts page's filters}, "company_ids",
    "force", "limit"}."""
    from harvey import finder

    state = _state()
    await state.init_db()
    raw = body.get("filter") if isinstance(body.get("filter"), dict) else {}
    try:
        f, attach = await _contact_filter(state, {k: str(v) for k, v in raw.items()
                                                  if v is not None})
    except ValueError as e:
        return state, [], str(e)
    try:
        limit = max(1, min(int(body.get("limit") or FIND_MAX), FIND_MAX))
    except (TypeError, ValueError):
        return state, [], "limit must be a number"
    ids = body.get("company_ids") if isinstance(body.get("company_ids"), list) else None
    selected = await finder.select(state, f, ids=ids, force=bool(body.get("force")),
                                   limit=limit, attach_figures=attach)
    return state, selected, ""


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


@app.post("/api/contacts/find/estimate")
async def estimate_find_contacts(request: Request):
    """What a Find-contacts run over the current filter would do and cost:
    how many companies (with no contact, or all with ``force``), how many
    have a website to read (free), how many need a website search, and its
    price through the cheapest search module that can find a company."""
    from harvey import finder
    from harvey.modules import switches

    state, selected, error = await _find_selection(await _json_body(request))
    if error:
        return JSONResponse({"error": error}, status_code=400)
    from harvey.config import load_env

    out = finder.estimate(selected, await switches.enabled_ids(state), load_env().model_dump())
    return {**out, "more": len(selected) >= FIND_MAX}


@app.post("/api/contacts/find")
async def find_contacts(request: Request):
    """Queue the contact finder for the current filter ({"filter", "force",
    "max_spend", "limit"}, or "company_ids"): one batch, one job per
    company. ``max_spend`` caps the website searches and address checks
    (0, the default: websites are read, nothing is paid for). A company
    already searched by a module's capability is skipped by it, unless
    ``force`` (searched again, once in this run)."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_contact_search

    body = await _json_body(request)
    try:
        max_spend = _money(body.get("max_spend"), default=0.0)
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "message": "max_spend must be a number of "
                             "dollars, 0 or more", "code": "bad_cap"}, status_code=400)
    state, selected, error = await _find_selection(body)
    if error:
        return JSONResponse({"success": False, "message": error}, status_code=400)
    if not selected:
        return {"success": True, "queued": 0, "companies": 0, "force": bool(body.get("force"))}
    try:
        batch_id, ids, dupes = await enqueue_contact_search(
            state, [c["id"] for c in selected], max_spend=max_spend,
            force=bool(body.get("force")))
    except jobs.Duplicate as e:
        return JSONResponse(_duplicate_body(e), status_code=409)
    return {"success": True, "batch_id": batch_id, "queued": len(ids),
            "already_waiting": len(dupes), "companies": len(selected),
            "max_spend": max_spend, "force": bool(body.get("force"))}


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


@app.get("/api/contacts/{contact_id}")
async def get_contact_reach(contact_id: int):
    """Every live company a contact reaches, through whom and where."""
    from harvey import contacts

    if not DB_PATH.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    async with _contacts_db() as db:
        return await contacts.reach(db, contact_id)


async def _contact_export_rows(request: Request) -> tuple[list[dict], int]:
    """Every contact the filters match, once each (a shared phone is called
    once, not once per company), for an export: do-not-contact ones left
    out (an opt-out, an address on compliance.do_not_contact, and every
    contact of a company whose domain is on it: its phones too). ConfigError
    when the list can't be read."""
    from harvey import contacts
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    state = _state()
    await state.init_db()
    f, attach = await _contact_filter(state, request.query_params)
    with_sql, body, params = contacts.contact_query(f)
    order = contacts.order_by(contacts.CONTACT_SORTS, request.query_params.get("sort", ""),
                              request.query_params.get("dir", "desc"), "first_seen", "ct.id")
    blocked = _blocked_entries(_load_config_quietly(), strict=True)
    out: list[dict] = []
    async with _contacts_db(attach) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                f"{with_sql}SELECT ct.id, ct.kind, ct.value, ct.display, ct.line_type, ct.status, "
                f"ct.first_seen, ct.last_seen {body} AND ct.do_not_contact = 0{order} LIMIT ?",
                (*params, CONTACT_EXPORT_MAX)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        rows = [r for r in rows if not (r["kind"] == "email" and blocked
                                        and do_not_contact_match(r["value"], blocked))]
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            found = await contacts.summaries(db, [r["id"] for r in chunk])
            marks = ",".join("?" * len(chunk))
            names: dict[int, list[str]] = {}
            people: dict[int, list[str]] = {}
            barred: set[int] = set()
            async with db.execute(
                    "SELECT l.contact_id, c.name, COALESCE(p.name, ''), COALESCE(c.domain, '') "
                    "FROM company_contacts l JOIN companies c ON c.id = l.company_id "
                    "LEFT JOIN people p ON p.id = l.person_id "
                    f"WHERE l.contact_id IN ({marks}) AND COALESCE(c.merged_into, '') = '' "
                    "ORDER BY l.contact_id, l.id", [r["id"] for r in chunk]) as cur:
                for cid, company, person, domain in await cur.fetchall():
                    if blocked and domain and do_not_contact_domain_match(domain, blocked):
                        barred.add(cid)
                    if company and company not in names.setdefault(cid, []):
                        names[cid].append(company)
                    if person and person not in people.setdefault(cid, []):
                        people[cid].append(person)
            for r in chunk:
                if r["id"] in barred:
                    continue
                info = found.get(r["id"], {})
                out.append({
                    "kind": r["kind"], "value": r["value"], "display": r["display"] or r["value"],
                    "line_type": r["line_type"], "status": r["status"],
                    "trust": info.get("trust", ""), "companies": info.get("companies", 0),
                    "company_names": "; ".join(names.get(r["id"], [])[:10]),
                    "people": "; ".join(people.get(r["id"], [])[:10]),
                    "sources": "; ".join(info.get("sources", [])),
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
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})

    people = {r["id"]: r for r in (await query_db(
        "SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, "
        "p.score, p.personalization_notes, p.company_id, c.name AS company_name, "
        "c.domain, c.website, c.description, c.industry AS found_by "
        "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
        f"WHERE p.id IN ({_in(pids)})", tuple(pids)) if pids else [])}
    campaigns = {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}

    company_ids = sorted({p["company_id"] for p in people.values() if p.get("company_id")})
    signals: dict[str, dict[str, str]] = {}
    if company_ids:
        rows = await query_db(
            "SELECT o.company_id, o.signal_code, o.value_num, o.value_text, "
            "o.observed_at, o.run_id, "
            "COALESCE(NULLIF(sc.label, ''), o.signal_code) AS label FROM observations o "
            "LEFT JOIN signal_codes sc ON sc.code = o.signal_code "
            f"WHERE o.company_id IN ({_in(company_ids)}) "
            f"AND o.signal_code IN ({_in(OUTBOX_CONTEXT_SIGNALS)}) "
            "ORDER BY o.observed_at, o.id",
            (*company_ids, *OUTBOX_CONTEXT_SIGNALS),
        )
        # Oldest first, so the latest observation wins. A signal can be seen
        # several times at once (one HIRING_ROLE row per open role): every
        # value from the latest sighting shows, not just one of them.
        latest: dict[tuple, tuple] = {}
        for r in rows:
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            key = (r["company_id"], r["label"])
            seen = (r["run_id"] or r["observed_at"])
            prev_seen, values = latest.get(key, (None, []))
            if seen != prev_seen:
                values = []
            if value not in values:
                values = values + [value]
            latest[key] = (seen, values)
        for (company_id, label), (_, values) in latest.items():
            signals.setdefault(company_id, {})[label] = ", ".join(v for v in values if v)

    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": p.get("company_name", ""),
            "website": p.get("website") or (f"https://{p['domain']}" if p.get("domain") else ""),
            "description": p.get("description", ""),
            "found_by": p.get("found_by", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(p.get("company_id"), {}),
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
        gate = pre_send_check("reviewer@example.com", subject, checked)
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
    """Can Harvey send email itself (a connected mailbox, or Instantly)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from harvey.config import load_config, load_env
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        from harvey.config import legacy_sending_held
        email = config.channels.email
        if not email.enabled or legacy_sending_held(config):
            return False
        if (email.provider or "").strip().lower() in NATIVE_PROVIDERS:
            provider = get_mail_provider(config, env)
            return bool(provider and provider.is_configured())
        return bool(env.instantly_api_key)
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
    """Everyone Harvey has approved or sent mail for (Outbox or Instantly).

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
    """Did Harvey itself email this person (native send, or an Instantly campaign)?

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
    from harvey.modules.ro.mf_financials import store as fin_store
    has_fin = Path(fin_store.FINANCIALS_PATH).is_file()
    if f.search and sort in ("", "relevance"):
        sort = "relevance"
    elif sort not in companies.SORTS or (sort in companies.FIGURE_SORTS and not has_fin):
        sort = "created"
    async with _contacts_db(attach or (has_fin and sort in companies.FIGURE_SORTS)) as db:
        return await companies.ids_for(db, f, sort, companies.direction(sort, way), limit,
                                       figures_mark=_figures_mark())


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
    activities = await state.get_company_activities([r["id"] for r in rows])
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
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    has_phone = ("(COALESCE(c.phone, '') != '' OR EXISTS (SELECT 1 FROM observations o "
                 "WHERE o.company_id = c.id AND o.signal_code = 'PHONE_NUMBER' AND o.value_text != '') "
                 "OR EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id AND p.phone != ''))")
    phone_ids = {r["id"] for r in await query_db(
        f"SELECT c.id FROM companies c WHERE COALESCE(c.merged_into, '') = '' AND {has_phone}")}
    config = _load_config_quietly()
    blocked = _blocked_entries(config)
    email_ids = set()
    for r in await query_db(
            "SELECT p.company_id, p.email, COALESCE(c.domain, '') AS domain FROM prospects p "
            "JOIN companies c ON c.id = p.company_id WHERE p.email != '' "
            "AND COALESCE(p.email_status, '') != 'invalid' AND p.status IS NOT 'opted_out'"):
        if r["company_id"] in email_ids:
            continue
        if blocked and (do_not_contact_match(r["email"], blocked)
                        or do_not_contact_domain_match(r["domain"], blocked)):
            continue
        email_ids.add(r["company_id"])
    return {
        "companies": await _scalar(f"SELECT COUNT(*) AS n FROM companies WHERE {_LIVE}"),
        "with_phone": len(phone_ids),
        "with_email": len(email_ids),
        "with_both": len(phone_ids & email_ids),
        "contacts": await _scalar("SELECT COUNT(*) AS n FROM prospects"),
        "people": await _scalar(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT company_id, value_text FROM observations "
            "WHERE signal_code = 'CONTACT_FOUND' AND value_text != '')"),
        "with_activity": await _scalar(
            "SELECT COUNT(DISTINCT company_id) AS n FROM company_activities"),
        "signals_proposed": await _scalar(
            "SELECT COUNT(*) AS n FROM signal_codes WHERE status = 'proposed' AND available = 1"),
        "signals_confirmed": await _scalar(
            "SELECT COUNT(*) AS n FROM signal_codes WHERE status = 'confirmed' AND available = 1"),
        "sending": await _sending_summary(config),
    }


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
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
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
    if not await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")}):
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


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
    """The sources a Discover request names: ``providers`` (a list), or
    the older single ``provider``."""
    keys = body.get("providers")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    if not isinstance(keys, list) or not keys:
        keys = [body.get("provider") or ""]
    return list(dict.fromkeys(str(k) for k in keys if k))


async def _discovery_refusal(body: dict):
    """Why a source or the preset asked for can't run (a ModuleOff: its
    module is off), or None."""
    from harvey.modules import switches
    from harvey.modules.job_kinds import discover_sources

    state = _state()
    await state.init_db()
    enabled = await switches.enabled_ids(state)
    jobbed = {s["key"]: s["module"] for s in discover_sources()}
    for key in _source_keys(body) or [""]:
        if key in jobbed:
            if jobbed[key] not in enabled:
                return switches.ModuleOff(jobbed[key], f"The {key} discovery source")
            continue
        problem = switches.discovery_problem(enabled, key, str(body.get("preset") or ""))
        if problem:
            return problem
    return None


def _discovery_queries(body: dict, config, provider: str = ""):
    from harvey.collectors.discover import build_queries, preset_problem

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, provider or str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    # No depth or limit: every result the source has (the cap stops the run).
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 0) or None,
        limit=int(body.get("limit") or 0) or None,
        preset=preset,
    )


def _discovery_plan(body: dict, config) -> tuple[list[dict], list[str]]:
    """One part per source the request ticked: a search source's queries,
    or a registry pull's parameters from its filters. A source that can't
    take this run (a preset it can't search with, filters that say
    nothing) is skipped with a note, unless it is the only one: then the
    run is refused (ValueError). Unknown sources are refused."""
    from harvey.collectors.discover import PROVIDERS, is_discovery_source
    from harvey.modules.job_kinds import discover_sources, lookup_only_message, registry_plan

    keys = _source_keys(body)
    if not keys:
        raise ValueError("pick at least one source")
    jobbed = {s["key"]: s for s in discover_sources()}
    filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    plan, notes = [], []
    for key in keys:
        try:
            if key in PROVIDERS and not is_discovery_source(key):
                raise LookupError(lookup_only_message(key))
            if key in PROVIDERS:
                plan.append({"provider": key, "label": PROVIDERS[key].label,
                             "queries": _discovery_queries(body, config, key)})
            elif key in jobbed:
                src = jobbed[key]
                if src["job"] != "registry_pull":
                    # Only the registry's filters are read here: another
                    # source run as a job needs its own planner first.
                    raise ValueError(f"{key} can't be run from Discover yet")
                params = registry_plan(filters.get(key) or {})
                plan.append({"source": key, "job": src["job"], "label": params.pop("label"),
                             "params": params, "name": src["label"]})
            else:
                raise LookupError(f"unknown provider {key!r}")
        except LookupError:
            raise
        except ValueError as e:
            if len(keys) == 1:
                raise
            notes.append(f"{key}: {e}")
    if not plan:
        raise ValueError("; ".join(notes) or "no source can take this run")
    return plan, notes


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
        import asyncio

        from harvey.collectors.discover import PROVIDERS, estimate_cost
        from harvey.config import load_config
        from harvey.modules.job_kinds import registry_count
        from harvey.modules.runs import run_discovery

        body = await request.json()
        refusal = await _discovery_refusal(body)
        if refusal:
            return JSONResponse(_error_body(refusal), status_code=409)
        config = load_config()
        try:
            plan, notes = _discovery_plan(body, config)
        except LookupError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        state = _state()
        await state.init_db()
        parts = []
        for part in plan:
            if part.get("provider"):
                key, queries = part["provider"], part["queries"]
                counted = {"counted": False, "available": None, "count_cost": 0.0, "errors": []}
                if PROVIDERS[key].can_count:
                    report = await run_discovery(state, config, key, queries,
                                                 dry_run=True, count=True)
                    counted = {"counted": report.counted, "available": report.available,
                               "count_cost": round(report.count_cost, 4),
                               "errors": report.errors}
                parts.append({"key": key, "label": part["label"],
                              "queries": [q.keyword() for q in queries],
                              "query_count": len(queries),
                              "estimated_cost": round(estimate_cost(key, queries), 4),
                              "free": PROVIDERS[key].estimate(queries) == 0, **counted})
            else:
                try:
                    n = await asyncio.to_thread(registry_count, part["params"])
                    errors = []
                except FileNotFoundError as e:
                    n, errors = None, [f"{e} (the filter needs it)"]
                parts.append({"key": part["source"], "label": part["label"],
                              "queries": [part["label"]], "query_count": 1,
                              "estimated_cost": 0.0, "free": True, "counted": n is not None,
                              "available": n, "count_cost": 0.0,
                              "errors": errors or ([] if n is not None else [
                                  "no registry loaded yet: the run loads it first (about "
                                  "1.5 GB, once per dataset)"])})
        queries = [q for p_ in parts for q in p_["queries"]]
        counted_all = all(p_["counted"] for p_ in parts)
        try:
            websites = await _websites_estimate(state, _find_websites(body))
        except (TypeError, ValueError):
            return JSONResponse({"error": "websites_max_spend must be a number of dollars, "
                                 "0 or more"}, status_code=400)
        return {
            "find_websites": websites,
            "provider": parts[0]["key"],
            "sources": parts, "notes": notes,
            "queries": queries, "query_count": len(queries),
            "estimated_cost": round(sum(p_["estimated_cost"] for p_ in parts), 4),
            "free": all(p_["free"] for p_ in parts),
            "counted": counted_all if len(parts) > 1 else parts[0]["counted"],
            "available": (sum(p_["available"] or 0 for p_ in parts)
                          if any(p_["available"] is not None for p_ in parts) else None),
            "count_cost": round(sum(p_["count_cost"] for p_ in parts), 4),
            "errors": [e for p_ in parts for e in p_["errors"]],
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _duplicate_body(e) -> dict:
    """A 409 for work already waiting: the message, plus the job (its kind,
    label and status) for the page to say it in the reader's language."""
    return {"success": False, "message": str(e), "duplicate": True, "code": "duplicate",
            "job": e.job.as_dict()}


# What a Discover run may spend looking up websites for the companies it
# saves with none when the request names no amount: nothing, so a caller's
# max_spend is all it spends; free lookups still run while the option is
# on. (The Discover tab sends the amount its field shows.)
WEBSITES_DEFAULT_USD = 0.0


def _find_websites(body: dict) -> float | None:
    """A Discover request's "also find websites, up to $X": the amount
    (``websites_max_spend``, $0 when absent: free lookups only), or None
    when ``find_websites`` is false. On unless turned off. ValueError for
    an amount that isn't one."""
    if body.get("find_websites") is False:
        return None
    return _money(body.get("websites_max_spend"), default=WEBSITES_DEFAULT_USD)


async def _websites_estimate(state, amount: float | None) -> dict:
    """What "find websites" would use: the searches that can find a
    company by name (free ones first), the price of a paid lookup, and how
    many paid lookups the amount pays for. How many companies will need
    one isn't known before the run."""
    from harvey import finder
    from harvey.config import load_env
    from harvey.modules import switches

    if amount is None:
        return {"enabled": False}
    found = finder.searchers(await switches.enabled_ids(state), load_env().model_dump())
    paid = [s for s in found if s.estimate > 0]
    cheapest = min((s.estimate for s in paid), default=0.0)
    return {"enabled": True, "max_spend": amount,
            "free": sorted({s.module for s in found if s.estimate <= 0}),
            "paid": sorted({s.module for s in paid}),
            "per_lookup": round(cheapest, 4),
            "paid_lookups": int(amount / cheapest + 1e-9) if cheapest else 0,
            "searchers": [{"module": s.module, "provider": s.provider, "cost": s.estimate}
                          for s in found]}


def _money(value, default: float | None = None) -> float:
    """A dollar amount from a request: finite and >= 0, else ValueError."""
    import math

    if value is None or value == "":
        if default is None:
            raise ValueError("expected a number of dollars")
        return default
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("expected a number of dollars, 0 or more")
    return amount


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

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider",
                                ",".join(p_.get("provider") or p_["source"] for p_ in plan))
        await state.set_setting("discovery_paused", "")
        try:
            batch_id, ids, dupes = await enqueue_sources(
                state, plan, max_spend=max_spend, find_websites=find_websites,
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
    return {"contacts": "Contacts", "duplicates": "Duplicates"}.get(lane, lane)


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


@app.post("/api/jobs/batches/{batch_id}/cap")
async def raise_batch_cap(batch_id: str, request: Request):
    """{"max_spend": n}: a new spend limit for a run; its jobs the old
    limit paused go back to the queue and carry on from where they stopped.
    A run with amounts of its own (a Discover run's searches and website
    lookups) gives the change to ``pool`` ("websites"), else its searches."""
    from harvey.modules import jobs

    try:
        body = await request.json()
        cap = _money((body or {}).get("max_spend"))
        pool = (body or {}).get("pool") or ""
        if not isinstance(pool, str):
            raise ValueError(pool)
    except Exception:
        return JSONResponse({"success": False, "message": "expected max_spend, a number >= 0"},
                            status_code=400)
    state = _state()
    await state.init_db()
    if await jobs.get_batch(state, batch_id) is None:
        return JSONResponse({"success": False, "message": "no such run"}, status_code=404)
    await jobs.set_batch_cap(state, batch_id, cap, pool=pool)
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
# A company the contact finder searched (finder.FINDER's searches row).
_FINDER_SEARCHED = ("EXISTS (SELECT 1 FROM searches s WHERE s.target = 'company' "
                    "AND s.target_id = companies.id AND s.module = 'contact_finder' "
                    "AND s.capability = 'find_contacts' AND s.outcome != 'error')")
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


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
    has_site = f"COALESCE(domain, '') != '' AND {_LIVE}"
    companies = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {_LIVE}")
    with_site = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {has_site}")
    no_site = companies - with_site
    profiled = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at IS NOT NULL")
    checked = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} "
        f"AND profile_checked_at IS NOT NULL AND {_FINDER_SEARCHED}")

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
        last = _parse_ts(await _first(
            "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
            (run["id"],))) or started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = await _scalar(
                f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at >= ?",
                (run["started_at"],))
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    # (The newest searches only: the table holds every search ever made.)
    last_contact = _parse_ts(await _first(
        "SELECT MAX(searched_at) AS t FROM (SELECT target, module, capability, searched_at "
        "FROM searches ORDER BY id DESC LIMIT 500) WHERE target = 'company' "
        "AND module = 'contact_finder' AND capability = 'find_contacts'"))
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
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
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
        companies = await query_db(f"SELECT COUNT(*) AS n FROM companies WHERE {_LIVE}")
        n_companies = companies[0]["n"] if companies else 0

        unprofiled_count = await state.count_companies_needing_profile()
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "signals_proposed": len(proposed),
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

        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
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

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Harvey won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
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
    companies, observations, unconfirmed, errors)."""
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
                      "unconfirmed": (r.get("unconfirmed") or {}).get(module_id, []),
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
            report["unconfirmed"] = j.result.get("unconfirmed") or report["unconfirmed"]
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


# ── Signals: Harvey proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, one group per module that declares signals,
    with live cohort sizes. Signals of a module that is off are left out
    (their observations stay); ``hidden`` counts them."""
    try:
        from harvey.modules import switches
        from harvey.signals import absence, sync_vocabulary

        state = _state()
        await state.init_db()
        # Syncing is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await sync_vocabulary(state)

        every = await state.get_signal_codes()
        codes = [c for c in every if c.get("available", 1)]
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        # True / false / unknown per company: "unknown" is not checked yet.
        states = await state.signal_states([sig["code"] for sig in codes])

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
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
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes),
                "hidden": len(every) - len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from harvey.signals import sync_vocabulary

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await sync_vocabulary(state)

        # A hidden signal (its module is off) is not decided on here.
        hidden = {r["code"] for r in await state.get_signal_codes() if not r.get("available", 1)}
        changed, unknown, off = 0, [], []
        for code in codes:
            if code in hidden:
                off.append(code)
            elif await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown, "module_off": off,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


COHORT_PAGE_SIZE = 50
COHORT_MAX_PAGE_SIZE = 200


async def _cohort_companies(require: list[str], exclude: list[str],
                            offset: int = 0, limit: int | None = None,
                            ranges: list[dict] | None = None,
                            verdicts=("match",)):
    """(counts per verdict, the page of companies in ``verdicts``)."""
    state = _state()
    await state.init_db()
    await _refuse_hidden_signals(state, [*require, *exclude, *(r["code"] for r in ranges or [])])
    return await state.cohort_verdicts(require, exclude, ranges, verdicts=verdicts,
                                       offset=offset, limit=limit)


async def _refuse_hidden_signals(state, codes) -> None:
    """A signal of a module that is off can't be filtered on: it is hidden,
    and its data stops at the day the module went off."""
    hidden = {r["code"]: r for r in await state.get_signal_codes() if not r.get("available", 1)}
    for code in codes:
        if code in hidden:
            from harvey.modules.switches import ModuleOff
            raise ModuleOff(hidden[code].get("module") or "", f"The signal {code}")


COHORT_VERDICTS = ("match", "possible", "no_match")


def _verdicts(raw) -> list[str]:
    """Which result tabs: one verdict, a list, CSV, or "all"."""
    if raw is None or raw == "" or raw == []:
        return ["match"]
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise ValueError("verdict must be match, possible, no_match or all")
    out = []
    for v in (v.strip() for v in raw):
        if v == "all":
            return list(COHORT_VERDICTS)
        if v not in COHORT_VERDICTS:
            raise ValueError("verdict must be match, possible, no_match or all")
        out.append(v)
    return list(dict.fromkeys(out))


def _codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    codes = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(codes) > 200 or any(len(c) > 128 for c in codes):
        raise ValueError("too many or oversized signal codes")
    return codes


COHORT_MAX_RANGES = 20


def _range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def _ranges(raw) -> list[dict]:
    """Numeric cohort filters: a list of {code, min, max} (and, for a yearly
    signal, an optional ``key``: the year to compare instead of the latest),
    or the CSV query form "SERP_RANK:11:30,MAPS_RANK::3,TURNOVER:1000000::2024".
    At least one bound per range."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            pieces = part.split(":")
            if len(pieces) not in (3, 4):
                raise ValueError("ranges look like CODE:min:max (or CODE:min:max:year)")
            item = {"code": pieces[0], "min": pieces[1], "max": pieces[2]}
            if len(pieces) == 4 and pieces[3].strip():
                item["key"] = pieces[3]
            items.append(item)
        raw = items
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError("ranges must be a list of {code, min, max}")
    if len(raw) > COHORT_MAX_RANGES:
        raise ValueError("too many ranges")
    out = []
    for r in raw:
        code = r.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("each range needs a signal code")
        low, high = _range_bound(r.get("min")), _range_bound(r.get("max"))
        if low is None and high is None:
            raise ValueError(f"range on {code} needs a min or a max")
        if low is not None and high is not None and low > high:
            raise ValueError(f"range on {code}: min is above max")
        item = {"code": code.strip(), "min": low, "max": high}
        key = r.get("key")
        if key is not None and str(key).strip():
            key = str(key).strip()
            if not key.isdigit() or len(key) != 4:
                raise ValueError(f"range on {code}: the year must be 4 digits")
            item["key"] = key
        out.append(item)
    return out


def _cohort_integer(raw, default):
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("offset and limit must be integers")
    value = int(raw)
    if abs(value) > 9223372036854775807:
        raise ValueError("offset or limit is too large")
    return value


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL — intersecting in JS over a capped fetch
    silently returns the wrong answer. ``ranges`` adds numeric filters —
    ``[{"code": "SERP_RANK", "min": 11, "max": 30}]``. An exclusion alone is
    a cohort too.

    Every company gets a verdict: ``match`` (every condition checked and
    met), ``possible`` (nothing rules it out, something isn't checked yet)
    or ``no_match``. ``counts`` has all three and ``size`` is the matches.
    ``verdict`` picks the tab ``companies`` pages through with
    ``offset``/``limit`` (default ``match``); each row carries its verdict.
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        require = _codes(body.get("require"))
        exclude = _codes(body.get("exclude"))
        ranges = _ranges(body.get("ranges"))
        offset = max(0, _cohort_integer(body.get("offset"), 0))
        limit = min(max(1, _cohort_integer(body.get("limit"), COHORT_PAGE_SIZE)),
                    COHORT_MAX_PAGE_SIZE)
        verdicts = _verdicts(body.get("verdict"))
        if not require and not exclude and not ranges:
            # No condition is an empty cohort, never every company.
            offset = 0
        counts, rows = await _cohort_companies(require, exclude, offset, limit,
                                               ranges=ranges, verdicts=verdicts)
        return {"size": counts["match"], "counts": counts,
                "verdict": verdicts[0] if len(verdicts) == 1 else "all",
                "total": sum(counts[v] for v in verdicts),
                "offset": offset, "limit": limit, "companies": rows}
    except (ValueError, TypeError) as e:
        return JSONResponse(_error_body(e), status_code=400)
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


def _csv_cell(value):
    from harvey.export import csv_cell
    return csv_cell(value or "")


@app.get("/api/cohort.csv")
async def export_cohort(require: str = "", exclude: str = "", ranges: str = "",
                        verdict: str = ""):
    """The whole cohort as a CSV download — every company in the chosen
    verdicts (matches by default), not just one page, each with its verdict."""
    import csv
    import io

    columns = ("name", "domain", "website", "industry", "location", "verdict")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    try:
        codes = _codes(require)
        excluded = _codes(exclude)
        bounds = _ranges(ranges)
        verdicts = _verdicts(verdict)
        if codes or excluded or bounds:
            _, rows = await _cohort_companies(codes, excluded, ranges=bounds,
                                              verdicts=verdicts)
            for r in rows:
                writer.writerow([_csv_cell(r.get(k)) for k in columns])
    except ValueError as e:
        return JSONResponse(_error_body(e), status_code=400)
    except Exception as e:
        logger.warning("Cohort export failed: %s", e)
        return JSONResponse({"error": "Cohort export failed"}, status_code=500)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cohort.csv"'},
    )


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
            last = _parse_ts(await _first(
                "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
                (run["id"],))) or _parse_ts(run.get("started_at"))
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
    uvicorn.run(app, host=bind, port=port, log_level="warning",
                proxy_headers=False, server_header=False, date_header=False)
