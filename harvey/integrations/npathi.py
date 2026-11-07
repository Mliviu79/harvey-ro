"""nPathi Chat: the SMS and WhatsApp messages people send in.

nPathi Chat runs per customer, so its address is the install's own
(``modules.npathi.base_url``, https only), and the workspace key is
NPATHI_API_KEY in .env, saved only after the account call accepted it.
Harvey reads, and writes one thing: the people who asked it to stop go on
nPathi's opt-out list too (POST /api/v1/unsubscribed, scope all).

- the account (GET /api/v1/me): plan, the key's abilities, credits;
- the channels this key may use (GET /api/v1/channels/available);
- inbound messages since a mark (GET /api/v1/messages?direction=in), newest
  first, page by page through meta.next_cursor;
- the opt-out list (GET /api/v1/unsubscribed), and adding to it.

Every request waits its turn on the ``npathi`` lane, and the lane is held
for as long as nPathi says (Retry-After on a 429, X-RateLimit-Reset when
X-RateLimit-Remaining reaches 0). Every POST carries an Idempotency-Key, the
same for the same request, so a retry within a day is never done twice.

The key goes in the Authorization header and nowhere else: never in a log
line, an error or a repr. An error says what happened in Harvey's own words
and nPathi's machine code (``NpathiError.code``), never nPathi's message
text, so nothing a server wrote reaches the agent or the log.

Endpoints follow nPathi Chat's public API v1 (Zender), read 2026-11-06.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("harvey.npathi")

MODULE = "npathi"
LANE = "npathi"
KEY_ENV = "NPATHI_API_KEY"
API = "/api/v1"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
PAGE = 100
# Pages one read walks at most (newest first); a read that stops here
# hasn't reached back to its mark (Read.complete False).
MAX_PAGES = 20
# A 429 with no Retry-After: wait this long. And the longest hold nPathi's
# headers can set: a wrong or hostile value never stops reading (a STOP
# among it) for longer than this.
RATE_HOLD_SECONDS = 60.0
MAX_HOLD_SECONDS = 3600.0
# For development against a local nPathi Chat: http and a private address
# are accepted with this set to 1 (never a link-local one).
DEV_URL_ENV = "HARVEY_NPATHI_DEV_URL"

# Harvey's channels, and the nPathi channels each reads. SMS arrives
# through an Android SIM (sms), a phone bridge or a GSM modem; waba is the
# official WhatsApp Business API; wa a WhatsApp number paired to the account.
CHANNELS = {"sms": ("sms", "phone_bridge", "gsm_modem"), "waba": ("waba",), "wa": ("wa",)}
# The ability a key needs to read each one (x-ability of GET /messages).
READ_ABILITY = {"sms": "sms.read", "waba": "waba.read", "wa": "wa.read"}
LABELS = {"sms": "SMS", "waba": "WhatsApp Business", "wa": "WhatsApp"}
# SMS connections that can receive a reply on the number they send from;
# an alphanumeric sender id can't.
RECEIVING_CONNECTIONS = ("phone_bridge", "gsm_modem", "android", "sim")

KEY_REJECTED = ("nPathi Chat refused the API key (missing, invalid, revoked or expired). "
                "Create a workspace key in nPathi Chat (API Keys) and connect it again.")
FORBIDDEN = ("nPathi Chat refused this request: the key lacks the ability, or the plan "
             "the feature.")
NO_CREDITS = "nPathi Chat says the workspace is out of credits."
RATE_LIMITED = "nPathi Chat's request limit is reached; Harvey waits as long as it asks."
NO_ANSWER = "nPathi Chat didn't answer. Try again in a minute."
SERVER_ERROR = "nPathi Chat had an error of its own. Try again later."
REFUSED = "nPathi Chat refused the request."
UNREADABLE = "nPathi Chat sent a reply Harvey couldn't read."
NOT_FOUND = "That isn't an nPathi Chat address: the API wasn't found there."
NO_KEY = "No nPathi Chat key is saved: connect it on Settings → Channels."
NO_URL = "No nPathi Chat address is set: enter your install's https:// address."

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class NpathiError(RuntimeError):
    """A request to nPathi Chat failed. ``kind``: key (401), forbidden
    (403), credits (402), rate (429), unreachable, server, refused (another
    4xx), unreadable, not_found or setup. ``code``: nPathi's machine code
    when it gave a well-formed one, else "". ``retry_after``: seconds, on
    a rate limit. The message is Harvey's own sentence."""

    def __init__(self, kind: str, message: str, code: str = "",
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retry_after = retry_after


def base_url_problem(url: str) -> str:
    """Why ``url`` can't be the install's address ("" when it can): https
    with a host, never a private, loopback or link-local address (http and
    a private address only with HARVEY_NPATHI_DEV_URL=1; link-local never:
    a cloud's metadata service lives there)."""
    url = (url or "").strip()
    if not url:
        return NO_URL
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "That isn't a URL."
    dev = os.getenv(DEV_URL_ENV, "").strip() == "1"
    if parts.scheme not in (("https", "http") if dev else ("https",)) or not host:
        return "The nPathi Chat address must start with https:// and name a host."
    if parts.username or parts.password or parts.query or parts.fragment:
        return "The nPathi Chat address is just https:// and the host (and a path, if any)."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            return "" if dev else "The nPathi Chat address can't be this computer."
        return ""
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return "The nPathi Chat address can't be a link-local or unspecified address."
    if not ip.is_global and not dev:
        return "The nPathi Chat address can't be a private or local network address."
    return ""


def normalize_base_url(url: str) -> str:
    """The address as kept: no trailing slash and no /api/v1 (added per request)."""
    url = (url or "").strip().rstrip("/")
    if url.lower().endswith(API):
        url = url[: -len(API)].rstrip("/")
    return url


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _int(x) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def _number(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def parse_time(x) -> float | None:
    """An ISO-8601 time with an offset as unix seconds, else None."""
    if not isinstance(x, str) or not x:
        return None
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def iso(ts: float) -> str:
    """Unix seconds as the ISO-8601 time with an offset nPathi's filters take."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _seconds(value) -> float | None:
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n >= 0 else None


@dataclass(frozen=True)
class Account:
    workspace: str
    plan: str
    features: tuple[str, ...]
    abilities: tuple[str, ...]
    credits: float | None
    key_expires_at: str

    def can_read(self, channel: str) -> bool:
        return READ_ABILITY.get(channel, "") in self.abilities


@dataclass(frozen=True)
class Inbound:
    """One message someone sent in. ``body`` is their text: untrusted, kept
    where reply bodies are, never shown to the agent."""
    id: int
    channel: str            # nPathi's channel (sms, phone_bridge, waba, wa...)
    sender: str             # their number or handle, as nPathi gave it
    to: str                 # the workspace's number it came to
    body: str
    created_at: float | None
    reference: str = ""


@dataclass
class Read:
    """What one read returned: the messages (newest first), whether it
    walked every page back to ``since`` (``complete``), and the oldest
    message time it reached (``oldest``)."""
    messages: list[Inbound] = field(default_factory=list)
    complete: bool = True
    oldest: float | None = None


def _inbound(row: dict) -> Inbound | None:
    mid = _int(row.get("id"))
    if mid is None or row.get("direction") != "in":
        return None
    sender = row.get("from")
    if isinstance(sender, dict):
        sender = sender.get("address")
    if not _str(sender):
        sender = _dict(row.get("sender")).get("address") if isinstance(row.get("sender"), dict) \
            else row.get("sender")
    return Inbound(id=mid, channel=_str(row.get("channel")), sender=_str(sender).strip()[:191],
                   to=_str(row.get("to")).strip()[:191], body=_str(row.get("body")),
                   created_at=parse_time(row.get("created_at")),
                   reference=_str(row.get("reference"))[:191])


class Npathi:
    def __init__(self, base_url: str, api_key: str, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        problem = base_url_problem(base_url)
        if problem:
            raise NpathiError("setup", problem)
        if not isinstance(api_key, str) or not api_key.strip() \
                or not (api_key.isascii() and api_key.isprintable()) or len(api_key) > 512:
            raise NpathiError("setup", NO_KEY)
        self.base_url = normalize_base_url(base_url)
        self._key = api_key.strip()
        self._transport = transport

    def __repr__(self) -> str:
        return f"Npathi(base_url={self.base_url!r})"

    # ── One request ──

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None):
        from harvey.modules import lanes

        held, _ = lanes.hold_info(LANE)
        if held > 0:
            raise NpathiError("rate", RATE_LIMITED, retry_after=held)
        await lanes.wait(LANE)
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        content = None
        if body is not None:
            content = json.dumps(body, sort_keys=True, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
            # The same request, the same key: a retry within 24 hours gets
            # the first answer instead of acting twice.
            headers["Idempotency-Key"] = "harvey-" + hashlib.sha256(
                f"{method} {path} {content}".encode()).hexdigest()[:40]
        try:
            async with httpx.AsyncClient(base_url=self.base_url + API, transport=self._transport,
                                         timeout=TIMEOUT, follow_redirects=False,
                                         trust_env=False) as client:
                resp = await client.request(method, path, params=params, content=content,
                                            headers=headers)
        except httpx.HTTPError:
            raise NpathiError("unreachable", NO_ANSWER) from None
        self._pace(resp)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = None
        if resp.is_success:
            if not isinstance(data, dict):
                raise NpathiError("unreadable", UNREADABLE)
            return data
        error = _dict(_dict(data).get("error"))
        code = _str(error.get("code"))
        code = code if _CODE.match(code) else ""
        status = resp.status_code
        if status == 401:
            raise NpathiError("key", KEY_REJECTED, code)
        if status == 402 or code == "insufficient_credits":
            raise NpathiError("credits", NO_CREDITS, code)
        if status == 429 or code == "rate_limited":
            wait = (_seconds(resp.headers.get("Retry-After"))
                    or _seconds(_dict(error.get("details")).get("retry_after"))
                    or RATE_HOLD_SECONDS)
            wait = min(wait, MAX_HOLD_SECONDS)
            lanes.hold(LANE, wait, why="nPathi Chat asked Harvey to wait")
            raise NpathiError("rate", RATE_LIMITED, code, retry_after=wait)
        if status == 403:
            raise NpathiError("forbidden", FORBIDDEN, code)
        if status == 404 and not code:
            raise NpathiError("not_found", NOT_FOUND)
        if status >= 500:
            raise NpathiError("server", SERVER_ERROR, code)
        raise NpathiError("refused", REFUSED + (f" ({code})" if code else ""), code)

    @staticmethod
    def _pace(resp: httpx.Response) -> None:
        """A window whose requests are used up holds the lane until it resets."""
        from harvey.modules import lanes

        for prefix in ("X-RateLimit-", "X-RateLimit-Workspace-"):
            left = _seconds(resp.headers.get(prefix + "Remaining"))
            reset = _seconds(resp.headers.get(prefix + "Reset"))
            if left is not None and left < 1 and reset is not None and reset > time.time():
                lanes.hold(LANE, min(reset - time.time(), MAX_HOLD_SECONDS),
                           why="nPathi Chat's request window is used up")

    async def _pages(self, path: str, params: dict, max_pages: int):
        """Each page's rows, through meta.next_cursor; (rows, complete)."""
        params = {**params, "limit": PAGE}
        for _ in range(max_pages):
            data = await self._request("GET", path, params=params)
            yield _list(data.get("data")), None
            cursor = _dict(data.get("meta")).get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                return
            params = {**params, "cursor": cursor}
        yield [], "stopped"

    # ── What Harvey asks ──

    async def account(self) -> Account:
        data = _dict((await self._request("GET", "/me")).get("data"))
        plan, token = _dict(data.get("plan")), _dict(data.get("token"))
        features = _dict(plan.get("features"))
        return Account(
            workspace=_str(_dict(data.get("workspace")).get("name"))[:120],
            plan=_str(plan.get("name"))[:120],
            features=tuple(sorted(k for k, v in features.items()
                                  if isinstance(k, str) and v not in (False, None, 0)))[:100],
            abilities=tuple(sorted(a for a in _list(token.get("abilities"))
                                   if isinstance(a, str) and len(a) <= 64))[:200],
            credits=_number(_dict(data.get("credits")).get("balance")),
            key_expires_at=_str(token.get("expires_at"))[:40])

    async def channels(self) -> dict:
        """Per channel, the senders this key may use, each with whether a
        reply can come back to it (``receives``): {sms: [...], waba: [...],
        wa: [...]}; a channel the key may not send on is left out."""
        data = _dict((await self._request("GET", "/channels/available")).get("data"))
        out: dict[str, list[dict]] = {}
        sms = data.get("sms")
        if isinstance(sms, dict):
            rows = [{"id": _int(c.get("id")), "name": _str(c.get("name"))[:120],
                     "type": _str(c.get("type"))[:40],
                     "receives": _str(c.get("type")).lower() in RECEIVING_CONNECTIONS}
                    for c in _list(sms.get("connections")) if isinstance(c, dict)]
            rows += [{"id": _int(s.get("id")), "name": _str(s.get("name"))[:120],
                      "type": "sender_id", "receives": False}
                     for s in _list(sms.get("sender_ids")) if isinstance(s, dict)]
            if sms.get("default_route") is True:
                rows.insert(0, {"id": None, "name": "", "type": "default", "receives": None})
            out["sms"] = rows
        waba = data.get("waba")
        if isinstance(waba, dict):
            out["waba"] = [{"id": _int(p.get("id")), "name": _str(p.get("display"))[:120],
                            "type": "waba_number", "receives": True}
                           for p in _list(waba.get("phone_numbers")) if isinstance(p, dict)]
        wa = data.get("wa")
        if isinstance(wa, dict):
            out["wa"] = [{"id": _int(a.get("id")), "name": _str(a.get("name"))[:120],
                          "type": "wa_account", "receives": True}
                         for a in _list(wa.get("accounts")) if isinstance(a, dict)]
        return out

    async def inbound(self, channel: str, since: float, until: float | None = None,
                      max_pages: int | None = None) -> Read:
        """The messages that came in on one of nPathi's channels from
        ``since`` (to ``until``), newest first."""
        params = {"direction": "in", "channel": channel, "since": iso(since)}
        if until is not None:
            params["until"] = iso(until)
        out = Read()
        async for rows, stopped in self._pages("/messages", params, max_pages or MAX_PAGES):
            if stopped:
                out.complete = False
                break
            for row in rows:
                msg = _inbound(_dict(row))
                if msg is None:
                    continue
                out.messages.append(msg)
                if msg.created_at is not None:
                    out.oldest = msg.created_at if out.oldest is None \
                        else min(out.oldest, msg.created_at)
        return out

    async def unsubscribed(self, max_pages: int = 200) -> tuple[list[dict], bool]:
        """nPathi's opt-out list: ([{phone, scope, at}], whether it was read
        to the end). Email entries and voice-only ones are left out."""
        out, complete = [], True
        async for rows, stopped in self._pages("/unsubscribed", {}, max_pages):
            if stopped:
                complete = False
                break
            for row in rows:
                row = _dict(row)
                phone = _str(row.get("phone")).strip()
                if phone and row.get("scope", "all") == "all":
                    out.append({"phone": phone[:32], "at": parse_time(row.get("unsubscribed_at"))})
        return out, complete

    async def add_unsubscribed(self, phone: str, reason: str = "") -> None:
        """Put a number on nPathi's opt-out list (texts and automated calls)."""
        body = {"phone": phone, "scope": "all"}
        if reason:
            body["reason"] = reason[:120]
        await self._request("POST", "/unsubscribed", body=body)


# ── This install's connection ──

def key_from_env() -> str:
    return os.getenv(KEY_ENV, "").strip()


async def base_url(state) -> str:
    from harvey.modules import switches

    return normalize_base_url(str((await switches.module_settings(state, MODULE)).get(
        "base_url") or ""))


async def client(state, *, transport: httpx.AsyncBaseTransport | None = None) -> Npathi | None:
    """The install's client, or None while the module is off or the
    address or the key is missing (nothing to read: never an error)."""
    from harvey.modules import switches

    if not await switches.is_enabled(state, MODULE):
        return None
    url, key = await base_url(state), key_from_env()
    if not url or not key or base_url_problem(url):
        return None
    return Npathi(url, key, transport=transport)
