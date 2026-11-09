"""The messaging channels: SMS and WhatsApp through nPathi Chat.

People send Harvey an SMS or a WhatsApp message; Harvey reads it into a
conversation (harvey/agents/messages.py), and an answer to it is drafted by
the Handler and waits in the Outbox for the user's approval, or is written
by the user on the Conversations page and approved as they write it;
harvey/agents/messenger.py sends it, through every check. Both run in the
channels' own loop, apart from email (harvey/agents/message_loop.py).
Harvey never writes first on these channels:
Law 506/2004 art. 12 wants prior consent for a commercial SMS or app
message, for a company as much as for a person, and WhatsApp's rules forbid
cold messages. So an answer goes only to a number that wrote in on that
channel within its window (SEND_RULES; no templates yet, so nothing outside
it), and autopilot never approves one.

What is kept per install, in the settings table:

- ``msg_read_at:<channel>``: when the channel was last read completely
  (unix seconds), as ``mail_read_at:`` is for a mailbox;
- ``msg_read_gap:<channel>``: while a read hasn't reached back to the one
  before it (JSON: since, covered_from, reason): that channel's sends wait
  and Today says so, as ``mail_read_gap`` does for cold email;
- ``msg_read_problem:<channel>``: why the last read failed (Harvey's own
  sentence and the error's class, never nPathi's text);
- ``npathi:account``: the last account check (plan, the key's abilities,
  credits, the channels and their senders), for the settings card;
- ``npathi:optout_sync_at`` and ``npathi:optout_pending:<number>``: nPathi's
  opt-out list read once a day, and Harvey's own opt-outs still to add to it;
  ``npathi:optout_sync_problem``: why the last read of that list failed (a
  key without unsubscribed.view waits a day, not a pass);
  ``npathi:still_listed``: the numbers the user took off the do-not-contact
  list in Harvey that nPathi's list still holds (theirs to remove there);
- ``npathi:rates``: the plan's prices per channel (GET /rates), a day;
- ``npathi:attempt:<outbox id>``: when a send was first tried, so a retry
  with the same Idempotency-Key stays inside nPathi's 24 hours;
- ``sending_paused:<channel>``: that channel's own kill switch (the global
  ``sending_paused`` stops it too).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from harvey.integrations import npathi

READ_KEY = "msg_read_at:"
GAP_KEY = "msg_read_gap:"
PROBLEM_KEY = "msg_read_problem:"
ACCOUNT_KEY = "npathi:account"
SYNC_KEY = "npathi:optout_sync_at"
PENDING_KEY = "npathi:optout_pending:"
SYNC_EVERY_SECONDS = 86400
CHANNELS = tuple(npathi.CHANNELS)
SETTING = "receive_{}"
SYNC_PROBLEM_KEY = "npathi:optout_sync_problem"
STILL_LISTED_KEY = "npathi:still_listed"
RATES_KEY = "npathi:rates"
RATES_EVERY_SECONDS = 86400
ATTEMPT_KEY = "npathi:attempt:"
PAUSED_KEY = "sending_paused:"
# The module's sending settings, per channel.
SEND_SETTING = "send_{}"
ROUTE_SETTING = "route_{}"
CAP_SETTING = "daily_cap_{}"


@dataclass(frozen=True)
class Rules:
    """What an answer on a channel may be: ``max_chars`` in all (the
    firm's name and the opt-out line included), sent only within
    ``window_hours`` of the number's last message in on that channel, to a
    mobile only when ``needs_mobile`` (where the contact's line type is
    known)."""
    max_chars: int
    window_hours: float
    needs_mobile: bool


# SMS: 300 characters (two or three segments, whatever the alphabet), and
# a month after their message. WhatsApp Business: its free-text window of 24
# hours (a template outside it is a later change); the paired WhatsApp
# number keeps to the same day. 4096 is the API's own limit for a text.
SEND_RULES = {"sms": Rules(300, 30 * 24, True), "waba": Rules(4096, 24, False),
              "wa": Rules(4096, 24, False)}
# The channels an nPathi channel's answers come back on (a message sent
# through a phone bridge is an SMS).
NPATHI_CHANNEL = {sub: ch for ch, subs in npathi.CHANNELS.items() for sub in subs}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


async def answer_problems(state, item: dict, body: str, config) -> list[str]:
    """What the gate would refuse in an SMS or WhatsApp answer's text (an
    Outbox row, ``item``), as it would send it now: a first answer to the
    number needs the firm's name and the opt-out line. The dashboard's
    Approve and Edit and ``harvey outbox --approve`` ask it before an
    answer is approved, while it can still be fixed. [] when none."""
    from harvey import gate

    rules = SEND_RULES.get(item.get("channel") or "")
    if rules is None:
        return ["not a messaging channel"]
    first = not await state.messages_sent_to_since(item["channel"], item.get("to_phone") or "", "")
    return gate.message_problems(body, max_chars=rules.max_chars, config=config, first=first)


def _json(raw) -> dict:
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


async def receiving(state) -> list[str]:
    """The channels whose inbound messages are read (their switches on)."""
    from harvey.modules import switches

    settings = await switches.module_settings(state, npathi.MODULE)
    return [ch for ch in CHANNELS if settings.get(SETTING.format(ch))]


async def set_receiving(state, channel: str, on: bool) -> None:
    from harvey.modules import switches

    if channel not in CHANNELS:
        raise ValueError(f"no channel {channel!r}: one of {', '.join(CHANNELS)}")
    await switches.set_module_setting(state, npathi.MODULE, SETTING.format(channel), bool(on))


async def last_read(state, channel: str) -> float | None:
    try:
        return float(await state.get_setting(READ_KEY + channel) or "") or None
    except ValueError:
        return None


async def read_gap(state, channel: str) -> dict | None:
    raw = await state.get_setting(GAP_KEY + channel)
    return _json(raw) if raw else None


async def gaps(state) -> dict[str, dict]:
    """{channel: gap} for every channel whose read hasn't reached back."""
    return {ch: gap for ch in CHANNELS if (gap := await read_gap(state, ch)) is not None}


async def save_account(state, account: npathi.Account | None, senders: dict | None,
                       error: npathi.NpathiError | None = None) -> dict:
    """Keep the outcome of an account check for the card (never the key)."""
    if account is None:
        kept = _json(await state.get_setting(ACCOUNT_KEY))
        kept.update({"ok": False, "checked_at": int(time.time()),
                     "error": str(error) if error else "", "error_kind": error.kind if error else ""})
    else:
        kept = {"ok": True, "checked_at": int(time.time()), "error": "", "error_kind": "",
                "workspace": account.workspace, "plan": account.plan,
                "features": list(account.features), "abilities": list(account.abilities),
                "credits": account.credits, "key_expires_at": account.key_expires_at,
                "senders": senders or {}}
    await state.set_setting(ACCOUNT_KEY, json.dumps(kept))
    return kept


async def check(state, client: npathi.Npathi) -> dict:
    """Ask nPathi who the key is and what it may use; keep the answer.
    Raises NpathiError (kept as the card's error too)."""
    try:
        account = await client.account()
        senders = await client.channels()
    except npathi.NpathiError as e:
        await save_account(state, None, None, e)
        raise
    return await save_account(state, account, senders)


async def status(state) -> dict:
    """What the settings card and ``harvey channels status`` show: whether
    the module is on, the address, whether a key is saved, the last account
    check, the channels a credits cap holds for want of a price, and per channel its switches, whether the key may read and send
    on it, its senders (each with whether a reply can come back to it) and
    the one chosen for answers, its daily cap, when it was last read
    completely, any gap and the last failure; the sending hours, the
    credits spent and their caps, and the opt-out list's sync. No request
    is made."""
    from harvey.modules import switches

    url = await npathi.base_url(state)
    account = _json(await state.get_setting(ACCOUNT_KEY))
    on = set(await receiving(state))
    send_on = set(await sending(state))
    abilities = set(account.get("abilities") or ())
    listed = account.get("senders") or {}
    settings = await module_settings(state)
    rows = []
    for ch in CHANNELS:
        problem = _json(await state.get_setting(PROBLEM_KEY + ch))
        chosen = await route(state, ch)
        gap = await read_gap(state, ch)
        rows.append({
            "id": ch, "label": npathi.LABELS[ch], "receive": ch in on,
            "can_read": (npathi.READ_ABILITY[ch] in abilities) if account.get("ok") else None,
            "can_send": (npathi.SEND_ABILITY[ch] in abilities) if account.get("ok") else None,
            "senders": [{**x, "key": route_key(x)} for x in listed.get(ch) or []],
            "last_read_at": await last_read(state, ch),
            # (When it is, the page says it in its own words: since, until.)
            "gap": {k: gap.get(k) for k in ("since", "covered_from")} if gap else None,
            "problem": problem.get("why", ""), "problem_kind": problem.get("kind", ""),
            "send": ch in send_on,
            "route": route_key(chosen) if chosen else "",
            "route_chosen": str(settings.get(ROUTE_SETTING.format(ch)) or ""),
            "daily_cap": int(settings.get(CAP_SETTING.format(ch)) or 0),
            "window_hours": SEND_RULES[ch].window_hours,
            "max_chars": SEND_RULES[ch].max_chars,
            "paused": bool(await state.get_setting(PAUSED_KEY + ch)),
        })
    start, end = await send_hours(state)
    sync_problem = _json(await state.get_setting(SYNC_PROBLEM_KEY))
    still = _json(await state.get_setting(STILL_LISTED_KEY))
    return {
        "module_on": await switches.is_enabled(state, npathi.MODULE),
        "base_url": url, "base_url_problem": npathi.base_url_problem(url) if url else "",
        "key_set": bool(npathi.key_from_env()),
        "connected": bool(account.get("ok")) and bool(url) and bool(npathi.key_from_env()),
        "account": {k: account.get(k) for k in
                    ("workspace", "plan", "features", "abilities", "credits", "key_expires_at",
                     "checked_at", "error", "error_kind", "ok")} if account else {},
        "channels": rows,
        "send_hours": {"start": f"{start // 60:02d}:{start % 60:02d}",
                       "end": f"{end // 60:02d}:{end % 60:02d}"},
        "spend": await spend(state),
        # The channels answers are sent on whose price isn't known while a
        # credits cap is set: their answers wait (credits_room's UNPRICED).
        "unpriced_cap": await unpriced_under_cap(state),
        "credits_out": await _lane_paused(state),
        "optout_sync": {"at": float(await state.get_setting(SYNC_KEY) or 0) or None,
                        "problem_kind": sync_problem.get("kind", ""),
                        "retry_at": sync_problem.get("retry_at")},
        "still_listed": int(still.get("count") or 0),
    }


async def _lane_paused(state) -> str:
    from harvey.modules import jobs

    return await jobs.lane_paused(state, npathi.LANE)


async def set_base_url(state, url: str) -> str:
    """Keep the install's address (checked: https, a public host)."""
    from harvey.modules import switches

    url = npathi.normalize_base_url(url)
    problem = npathi.base_url_problem(url)
    if problem:
        raise ValueError(problem)
    await switches.set_module_setting(state, npathi.MODULE, "base_url", url)
    return url


# ── Sending answers (harvey/agents/messenger.py) ──

async def module_settings(state) -> dict:
    from harvey.modules import switches

    return await switches.module_settings(state, npathi.MODULE)


async def sending(state) -> list[str]:
    """The channels answers may be sent on (the module on, their send
    switches on). Off by default: the user turns a channel on once its
    route is chosen."""
    from harvey.modules import switches

    if not await switches.is_enabled(state, npathi.MODULE):
        return []
    settings = await module_settings(state)
    return [ch for ch in CHANNELS if settings.get(SEND_SETTING.format(ch))]


async def set_sending(state, channel: str, on: bool) -> None:
    from harvey.modules import switches

    if channel not in CHANNELS:
        raise ValueError(f"no channel {channel!r}: one of {', '.join(CHANNELS)}")
    await switches.set_module_setting(state, npathi.MODULE, SEND_SETTING.format(channel), bool(on))


def route_key(sender: dict) -> str:
    """How a sender is named in the settings: "<type>:<id>"."""
    return f"{sender.get('type') or ''}:{'' if sender.get('id') is None else sender.get('id')}"


async def senders(state, channel: str) -> list[dict]:
    """The senders the last account check listed for a channel."""
    account = _json(await state.get_setting(ACCOUNT_KEY))
    return [x for x in ((account.get("senders") or {}).get(channel) or []) if isinstance(x, dict)]


async def route(state, channel: str) -> dict | None:
    """The sender chosen for a channel's answers, as the last account check
    listed it, when a reply can come back to it; None otherwise (not
    chosen, gone from the account, or one that can't receive: an
    alphanumeric sender ID, or a default route nobody can say of)."""
    chosen = str((await module_settings(state)).get(ROUTE_SETTING.format(channel)) or "")
    for sender in await senders(state, channel):
        if chosen and route_key(sender) == chosen and sender.get("receives") is True:
            return sender
    return None


async def set_route(state, channel: str, key: str) -> None:
    """Choose a channel's sender ("" clears it). ValueError for one the
    account doesn't list, or one a reply can't come back to."""
    from harvey.modules import switches

    if channel not in CHANNELS:
        raise ValueError(f"no channel {channel!r}: one of {', '.join(CHANNELS)}")
    key = str(key or "").strip()
    if key:
        found = next((x for x in await senders(state, channel) if route_key(x) == key), None)
        if found is None:
            raise ValueError("That sender isn't on the account: check the connection again.")
        if found.get("receives") is not True:
            raise ValueError("Answers go only from a sender a reply can come back to "
                             "(a SIM, a phone bridge or a GSM modem line; not a sender ID).")
    await switches.set_module_setting(state, npathi.MODULE, ROUTE_SETTING.format(channel), key)


async def daily_cap(state, channel: str) -> int:
    return int((await module_settings(state)).get(CAP_SETTING.format(channel)) or 0)


def _minutes(text: str, default: int) -> int:
    m = _TIME_RE.match(str(text or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else default


async def send_hours(state) -> tuple[int, int]:
    """The sending window (minutes after local midnight, start and end):
    09:00-20:00 unless the module's settings say otherwise."""
    settings = await module_settings(state)
    return (_minutes(settings.get("send_start"), 9 * 60),
            _minutes(settings.get("send_end"), 20 * 60))


async def paused(state, channel: str) -> str:
    """Why sends on a channel are paused ("" when they aren't): the global
    kill switch (the Outbox's Pause, the bounce rate), the channel's own,
    or the npathi lane (paused when nPathi said the credits ran out)."""
    from harvey.modules import jobs

    return (await state.get_setting("sending_paused") or await state.get_setting(
        PAUSED_KEY + channel) or await jobs.lane_paused(state, npathi.LANE))


def first_message(body: str, config) -> str:
    """The first answer to a number as it goes: the firm's name before it
    (unless the text names it already) and the opt-out line under it."""
    from harvey.config import message_opt_out_line

    body = (body or "").strip()
    company = str(getattr(getattr(config, "persona", None), "company", "") or "").strip()
    if company and company.lower() not in body.lower():
        body = f"{company}: {body}"
    line = message_opt_out_line(config)
    if line and line not in body:
        body = f"{body}\n{line}"
    return body


# The GSM 7-bit alphabet (and its extension, two characters each): any
# other character (ș, ț, ă, î, â among them) sends the SMS as UCS-2.
_GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRS"
           "TUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXT = set("^{}\\[~]|€")


def segments(text: str) -> int:
    """How many SMS segments a text takes (160/153 in GSM 7-bit, 70/67 in
    UCS-2)."""
    text = text or ""
    if all(c in _GSM or c in _GSM_EXT for c in text):
        n = sum(2 if c in _GSM_EXT else 1 for c in text)
        return 1 if n <= 160 else -(-n // 153)
    n = len(text.encode("utf-16-le")) // 2
    return 1 if n <= 70 else -(-n // 67)


async def rates(state, client: npathi.Npathi | None) -> dict[str, float]:
    """The plan's credits per message per nPathi channel, read once a day
    (kept even when the key may not read them: then {} for a day)."""
    kept = _json(await state.get_setting(RATES_KEY))
    if client is not None and time.time() - float(kept.get("at") or 0) >= RATES_EVERY_SECONDS:
        try:
            got = await client.rates()
        except npathi.NpathiError as e:
            if e.kind not in ("forbidden", "refused", "not_found"):
                return kept.get("rates") or {}
            got = {}
        kept = {"at": int(time.time()), "rates": got}
        await state.set_setting(RATES_KEY, json.dumps(kept))
    rates = kept.get("rates") or {}
    return {k: float(v) for k, v in rates.items() if isinstance(v, (int, float))}


async def worst_credits(state, channel: str, body: str,
                        client: npathi.Npathi | None) -> float | None:
    """The most an answer can cost before it is sent: the plan's price for
    the channel (each SMS segment a message), else the last price an answer
    on the channel was charged; None when neither is known."""
    found = await rates(state, client)
    prices = [found[sub] for sub in npathi.CHANNELS[channel] if sub in found]
    per = max(prices) if prices else await state.last_message_price(channel)
    if per is None:
        return None
    return per * (segments(body) if channel == "sms" else 1)


async def credit_caps(state) -> dict:
    """The credits caps (None: none) and what a credit is worth in USD
    (None: not entered)."""
    settings = await module_settings(state)
    return {k: settings.get(k) for k in ("credits_per_day", "credits_per_month", "usd_per_credit")}


def _utc_day_start() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0,
                                              tzinfo=None).isoformat()


def _utc_month_start() -> str:
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0,
                                              microsecond=0, tzinfo=None).isoformat()


async def spend(state) -> dict:
    """The answers sent on every channel today, and the credits they cost
    today and this month (UTC), with the caps and their USD when a credit's
    worth is entered; for the Channels card and the Usage tab."""
    caps = await credit_caps(state)
    today = await state.message_credits_since(_utc_day_start())
    month = await state.message_credits_since(_utc_month_start())
    usd = caps.get("usd_per_credit")
    return {
        "sent_today": {ch: await state.messages_sent_today(ch) for ch in CHANNELS},
        "credits_today": today, "credits_month": month,
        "credits_per_day": caps.get("credits_per_day"),
        "credits_per_month": caps.get("credits_per_month"),
        "usd_per_credit": usd,
        "usd_today": round(today * usd, 4) if usd is not None else None,
        "usd_month": round(month * usd, 4) if usd is not None else None,
        "unpriced": await state.unpriced_messages_since(_utc_month_start()),
    }


# What credits_room says when a cap is set and the price isn't known.
UNPRICED = "unpriced"


async def price_known(state, channel: str) -> bool:
    """Whether the most an answer on ``channel`` can cost is known: a rate
    kept from the rates endpoint, or the last price an answer there was
    charged. No request is made."""
    kept = await rates(state, None)
    return any(sub in kept for sub in npathi.CHANNELS[channel]) or \
        await state.last_message_price(channel) is not None


async def unpriced_under_cap(state) -> list[str]:
    """The channels answers are sent on whose price isn't known, while a
    credits cap is set ([] with no cap)."""
    caps = await credit_caps(state)
    if caps.get("credits_per_day") is None and caps.get("credits_per_month") is None:
        return []
    return [ch for ch in await sending(state) if not await price_known(state, ch)]


async def credits_room(state, worst: float | None) -> str:
    """Why an answer costing at most ``worst`` credits can't go now under
    the caps ("" when it can). With a cap set and the price unknown (no
    rate, no price paid yet) it is UNPRICED: nothing goes until the key may
    read the rates or the cap is lifted; with no cap the price doesn't
    matter. An answer sent and not priced yet counts at this one's price,
    so a cap isn't passed by answers nobody priced."""
    caps = await credit_caps(state)
    for key, since, what in (("credits_per_day", _utc_day_start(), "today's"),
                             ("credits_per_month", _utc_month_start(), "this month's")):
        cap = caps.get(key)
        if cap is None:
            continue
        if worst is None:
            return UNPRICED
        spent = await state.message_credits_since(since)
        unpriced = await state.unpriced_messages_since(since)
        if spent + unpriced * worst + worst > float(cap) + 1e-9:
            return f"{what} credits cap ({spent:g} of {float(cap):g} used)"
    return ""


async def queue_answer(state, config, *, conversation: str, prospect_id: str, channel: str,
                       phone: str, body: str, note: str = "", approved: bool = False) -> str | None:
    """Queue an SMS or WhatsApp answer to a conversation, to ``phone`` on
    ``channel``. Harvey's own drafts and the agent's wait for review
    (autopilot never approves one: state.promote_for_autopilot is email
    only); ``approved``: one the user wrote on the Conversations page,
    approved by them as they write it (approved_by "you"), the messenger's
    every check still ahead of it, ``note`` then why it waits. The first
    answer ever to that number there gets the firm's name and the opt-out
    line (first_message). What the gate would refuse in it (too long, a
    brace, a link) is said on the row, so the Outbox shows it held until it
    is edited; ``note``: why else it waits (an answer the agent asked for).
    Its id, or None."""
    from harvey import gate
    from harvey.state import HELD_FOR_REVIEW

    if channel not in SEND_RULES:
        raise ValueError(f"no channel {channel!r}: one of {', '.join(SEND_RULES)}")
    first = not await state.messages_sent_to_since(channel, phone, "")
    text = first_message(body, config) if first else (body or "").strip()
    problems = gate.message_problems(text, max_chars=SEND_RULES[channel].max_chars,
                                     config=config, first=first)
    if approved and problems:
        return None
    return await state.add_outbox_item(
        prospect_id=prospect_id or "", conversation_id=conversation, kind="reply",
        to_email="", subject="", body=text,
        status="approved" if approved else "pending_review", provider=npathi.MODULE,
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        channel=channel, to_phone=phone, approved_by="you" if approved else "",
        error=note or ((HELD_FOR_REVIEW + "; ".join(problems))[:300] if problems else ""))
