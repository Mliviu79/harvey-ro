"""The Codex account linked in Harvey (``brain.auth: account``): linking it
from Settings → Model, and keeping it renewed.

Linking. POST /api/brain/codex/link starts a ChatGPT device-code login
(codex.start_device_login) in an app-server of Harvey's own, under the
private config every Codex call runs with, its CODEX_HOME a fresh staging
folder data/auth/codex-link. The panel shows the page and the code the
user types there; the link waits for Codex's ``account/login/completed``
(GET .../link polls it: pending, linked, expired, failed with a class,
cancelled), at most codex.DEVICE_LOGIN_SECONDS. Once linked, the login
file Codex wrote is moved into data/auth/codex, where calls take their
access-only copy from, so a link that fails or is cancelled leaves the
login already there (an API key, an earlier account) as it was. The
staging folder is removed whatever happens. One link at a time per
install: the link holds the lease LEASE, and so does a renewal, so the two
never write the login at once. The code is shown to the user (it is theirs
to type) and never logged.

Renewal. Harvey keeps that login alive itself: the worker calls
renew_if_due every RENEW_CHECK_SECONDS; when the login was last renewed
RENEW_EVERY_SECONDS ago or would stop being usable within
RENEW_AHEAD_SECONDS (codex.login_until), codex.renew_login has Codex
refresh it in that folder. A failed renewal is tried again after
RENEW_RETRY_SECONDS; the outcome is kept in the settings table
(RENEWAL_KEY) for the panel and Today, which say "link again" once the
login on file can't be used (or Codex no longer reports the account).
The terminal login (~/.codex) is never renewed: only a login in Harvey's
own folder is.

Unlinking runs Codex's own ``account/logout`` in that folder (unlink).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("harvey.codex_link")

LEASE = "codex_account"
LEASE_TTL_SECONDS = 30.0
RENEW_CHECK_SECONDS = 600
RENEW_EVERY_SECONDS = 24 * 3600
RENEW_AHEAD_SECONDS = 6 * 3600
RENEW_RETRY_SECONDS = 3600
RENEW_TIMEOUT_SECONDS = 120
RENEWAL_KEY = "codex_login_renewal"
# How long a finished link's outcome stays readable (the panel's poll).
KEEP_DONE_SECONDS = 600

PENDING, LINKED, EXPIRED, FAILED, CANCELLED, IDLE = (
    "pending", "linked", "expired", "failed", "cancelled", "idle")


class LinkError(Exception):
    """A link that can't start: ``kind`` busy (another link or a renewal
    is running) or the CodexError's kind."""

    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind


def account_home() -> Path:
    from harvey import brain_auth

    return brain_auth.ensure_account_dir("codex")


def staging_home() -> Path:
    from harvey import brain_auth

    return brain_auth.AUTH_DIR / "codex-link"


def linked_login() -> dict | None:
    """The ChatGPT login in Harvey's folder (read only, never shown), or
    None when there is none (no file, an API key)."""
    from harvey import brain_auth, codex

    try:
        return codex._load_native_auth(brain_auth.account_dir("codex"))[1]
    except codex.CodexError:
        return None


def is_linked() -> bool:
    return linked_login() is not None


# ── The link ──

@dataclass
class _Link:
    owner: str
    status: str = PENDING
    url: str = ""
    code: str = ""
    expires_at: float = 0.0
    kind: str = ""
    plan: str = ""
    ended_at: float = 0.0
    # Set once the code was typed: the login is being moved in, which a
    # cancel waits for rather than interrupts.
    finishing: bool = False
    login: object = None
    task: asyncio.Task | None = None
    keeper: asyncio.Task | None = None
    state: object = None


_link: _Link | None = None


def _public(link: _Link | None) -> dict:
    if link is None or (link.status != PENDING and link.ended_at
                        and time.time() - link.ended_at > KEEP_DONE_SECONDS):
        return {"status": IDLE}
    out = {"status": link.status}
    if link.status == PENDING:
        out.update({"url": link.url, "code": link.code,
                    "expires_in": max(0, int(link.expires_at - time.time()))})
    if link.kind:
        out["kind"] = link.kind
    if link.plan:
        out["plan"] = link.plan
    return out


def status() -> dict:
    """The link's state for the panel: {status (idle, pending, linked,
    expired, failed, cancelled), url, code, expires_in (pending only),
    kind (failed: the CodexError's class), plan (linked)}."""
    return _public(_link)


async def _keep_lease(state, owner: str, lease: str = LEASE) -> None:
    """Renew ``owner``'s hold on ``lease`` until cancelled (Claude's link,
    harvey/claude_link.py, keeps its own lease the same way)."""
    from harvey.modules import worker

    while True:
        await asyncio.sleep(LEASE_TTL_SECONDS / 3)
        try:
            await worker.acquire_lease(state, owner, LEASE_TTL_SECONDS, lease)
        except Exception as e:
            logger.debug("%s: renewing the lease failed: %s", lease, e)


async def _stop_keeping(keeper: asyncio.Task | None) -> None:
    """Stop renewing a lease (before it is released, so no renewal lands
    after the release)."""
    if keeper is None:
        return
    keeper.cancel()
    await asyncio.gather(keeper, return_exceptions=True)


def _clear_staging() -> None:
    shutil.rmtree(staging_home(), ignore_errors=True)


def _fresh_staging() -> Path:
    from harvey import brain_auth

    _clear_staging()
    brain_auth.ensure_account_dir("codex")       # (data/auth, owner-only)
    path = staging_home()
    path.mkdir(mode=0o700)
    return path


async def start(state, on_linked=None) -> dict:
    """Start a link (status()'s pending answer, with the url and code).
    ``on_linked`` (async, no arguments) runs once the login is in place:
    the dashboard sets brain.auth there. Raises LinkError."""
    global _link
    from harvey import codex
    from harvey.modules import worker

    if _link is not None and _link.status == PENDING:
        raise LinkError("A link is already waiting for its code: finish or cancel it first.",
                        "busy")
    owner = f"link-{uuid.uuid4().hex[:12]}"
    if not await worker.acquire_lease(state, owner, LEASE_TTL_SECONDS, LEASE):
        raise LinkError("Another link or a renewal of the Codex login is running: try again "
                        "in a minute.", "busy")
    # (Kept alive from now: starting the login can outlast the lease's life.)
    keeper = asyncio.create_task(_keep_lease(state, owner))
    try:
        login = await codex.start_device_login(_fresh_staging())
    except BaseException as e:
        _clear_staging()
        await _stop_keeping(keeper)
        await worker.release_lease(state, owner, LEASE)
        if isinstance(e, codex.CodexError):
            raise LinkError(str(e), e.kind) from None
        if isinstance(e, OSError):
            raise LinkError(f"Couldn't prepare the link folder ({type(e).__name__}).",
                            "runtime") from None
        raise
    link = _Link(owner=owner, url=login.url, code=login.code, login=login, state=state,
                 expires_at=time.time() + codex.DEVICE_LOGIN_SECONDS, keeper=keeper)
    _link = link
    link.task = asyncio.create_task(_follow(state, link, on_linked))
    return _public(link)


async def _follow(state, link: _Link, on_linked) -> None:
    from harvey import codex

    deadline = time.monotonic() + max(0.0, link.expires_at - time.time())
    linked = False
    try:
        try:
            found = await link.login.wait(deadline)
            link.finishing = True
            await link.login.close()
            _move_in()
            link.plan = found.get("plan") or ""
            linked = True
            try:
                await _record(state, ok=True, kind="")
            except Exception as e:
                logger.warning("codex link: recording the link failed (%s)", type(e).__name__)
        except codex.CodexError as e:
            link.status = EXPIRED if e.kind == "timeout" else FAILED
            link.kind = "" if e.kind == "timeout" else e.kind
        except OSError as e:
            link.status, link.kind = FAILED, "save"
            logger.warning("codex link: moving the login in failed (%s)", type(e).__name__)
        except Exception as e:
            link.status, link.kind = FAILED, "failed"
            logger.warning("codex link: the link failed (%s)", type(e).__name__)
        if linked and on_linked is not None:
            try:
                await on_linked()
            except Exception as e:
                logger.warning("codex link: saving brain.auth failed: %s", e)
                link.kind = "config"
        if linked:
            # (Said only once brain.auth is saved: the panel reloads on it.)
            link.status = LINKED
    except asyncio.CancelledError:
        link.status = CANCELLED
        # (Codex is asked to drop the login before its process ends.)
        try:
            await link.login.cancel()
        except BaseException:
            pass
        raise
    finally:
        await _end(link)


async def _end(link: _Link) -> None:
    """Whatever way a link ended: its code forgotten, its process ended,
    the staging folder removed, the lease released."""
    from harvey import brain_auth
    from harvey.modules import worker

    if link.status == PENDING:
        # (Ended some other way: never left waiting for a code nobody can type.)
        link.status, link.kind = FAILED, link.kind or "failed"
    link.code = ""
    link.ended_at = time.time()
    try:
        await link.login.close()
    finally:
        link.login.code = ""
        _clear_staging()
        await _stop_keeping(link.keeper)
    try:
        await worker.release_lease(link.state, link.owner, LEASE)
    except Exception as e:
        logger.debug("codex link: releasing the lease failed: %s", e)
    brain_auth.forget()


def _move_in() -> None:
    """The login Codex wrote in the staging folder, moved (one rename, so
    calls never see half a file) into data/auth/codex; it must be a
    ChatGPT login Codex wrote owner-only."""
    from harvey import codex

    staged = staging_home() / "auth.json"
    codex._load_native_auth(staging_home())          # (raises when it isn't one)
    os.replace(staged, account_home() / "auth.json")


async def cancel() -> dict:
    """Cancel a pending link: Codex is asked to drop it and its process
    ends. The status afterwards."""
    link = _link
    if link is None or link.task is None:
        return status()
    if link.finishing:
        # (The code was typed: the login is being moved in; let it land.)
        await asyncio.gather(link.task, return_exceptions=True)
        return status()
    if link.status != PENDING:
        return status()
    link.task.cancel()
    try:
        await link.task
    except (asyncio.CancelledError, Exception):
        pass
    if link.status == PENDING:
        # (Cancelled before its wait began: nothing of it has run.)
        link.status = CANCELLED
        await link.login.cancel()
        await _end(link)
    return status()


async def close() -> None:
    """Stop a pending link (the dashboard shutting down)."""
    await cancel()


async def unlink(state) -> bool:
    """Remove the linked account: Codex's own logout in data/auth/codex
    (codex.logout). True when there was one. Waits for no link or renewal
    (takes the lease; raises LinkError busy while one runs)."""
    from harvey import brain_auth, codex
    from harvey.modules import worker

    if not is_linked():
        return False
    owner = f"unlink-{uuid.uuid4().hex[:12]}"
    if not await worker.acquire_lease(state, owner, LEASE_TTL_SECONDS, LEASE):
        raise LinkError("A link or a renewal of the Codex login is running: try again in a "
                        "minute.", "busy")
    keeper = asyncio.create_task(_keep_lease(state, owner))
    try:
        await codex.logout(brain_auth.account_dir("codex"))
        await state.set_setting(RENEWAL_KEY, "")
    finally:
        await _stop_keeping(keeper)
        await worker.release_lease(state, owner, LEASE)
        brain_auth.forget()
    return True


# ── Renewal ──

async def _record(state, *, ok: bool, kind: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    entry = {"at": int(now), "ok": ok, "kind": kind,
             "next_at": int(now + (RENEW_EVERY_SECONDS if ok else RENEW_RETRY_SECONDS))}
    await state.set_setting(RENEWAL_KEY, json.dumps(entry))
    return entry


async def _recorded(state) -> dict:
    try:
        raw = await state.get_setting(RENEWAL_KEY) or ""
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def due(native: dict, recorded: dict, now: float) -> bool:
    """Whether the linked login wants renewing: usable for less than
    RENEW_AHEAD_SECONDS, or renewed a day ago or more (and not tried
    within the day); a failure waits RENEW_RETRY_SECONDS before the next
    try."""
    from harvey import codex

    next_at = float(recorded.get("next_at") or 0)
    if recorded.get("ok") is False and now < next_at:
        return False
    if codex.login_until(native) - now < RENEW_AHEAD_SECONDS:
        return True
    if now < next_at:
        return False
    refreshed = codex.last_refreshed(native)
    return refreshed is None or now - refreshed >= RENEW_EVERY_SECONDS


async def renew_if_due(state, now: float | None = None, force: bool = False) -> dict | None:
    """Renew the linked login when it is due (``force``: now). The
    outcome recorded ({at, ok, kind, next_at}), or None when nothing was
    done (no linked login, not due, a link or another renewal running)."""
    from harvey import brain_auth, codex
    from harvey.modules import worker

    native = linked_login()
    if native is None:
        return None
    now = time.time() if now is None else now
    if not force and not due(native, await _recorded(state), now):
        return None
    owner = f"renew-{uuid.uuid4().hex[:12]}"
    if not await worker.acquire_lease(state, owner, LEASE_TTL_SECONDS, LEASE):
        return None
    # (Kept alive while Codex runs: a renewal can outlast the lease's life.)
    keeper = asyncio.create_task(_keep_lease(state, owner))
    try:
        kind = ""
        try:
            await codex.renew_login(brain_auth.account_dir("codex"),
                                    timeout=RENEW_TIMEOUT_SECONDS)
        except codex.CodexError as e:
            kind = e.kind or "failed"
        after = linked_login()
        if not kind and (after is None
                         or codex.login_until(after) - time.time() < RENEW_AHEAD_SECONDS):
            # (Codex answered, but the login on file still runs out soon.)
            kind = "not_renewed"
        if kind:
            logger.warning("The linked Codex login couldn't be renewed (%s); trying again "
                           "in %d min", kind, RENEW_RETRY_SECONDS // 60)
        return await _record(state, ok=not kind, kind=kind)
    finally:
        await _stop_keeping(keeper)
        await worker.release_lease(state, owner, LEASE)
        brain_auth.forget()


async def renewal_status(state, now: float | None = None) -> dict:
    """The linked login for the panel and Today: {linked, usable_until,
    expired (link again), renewed_at, last_try {at, ok, kind}}. ``expired``
    when the login on file can't be given to a call any more, or the last
    renewal found Codex no longer reports the account."""
    from harvey import codex

    now = time.time() if now is None else now
    native = linked_login()
    recorded = await _recorded(state) if state is not None else {}
    if native is None:
        return {"linked": False, "expired": False}
    until = codex.login_until(native)
    refreshed = codex.last_refreshed(native)
    return {"linked": True, "usable_until": int(until), "renewed_at": int(refreshed or 0),
            "expired": until <= now or (recorded.get("ok") is False
                                        and recorded.get("kind") == "auth"),
            "last_try": {k: recorded[k] for k in ("at", "ok", "kind") if k in recorded}}
