"""Messenger: sends the SMS and WhatsApp answers the user approved, through
nPathi Chat, and reads back where each one is.

Only an answer: a number gets a message only while the window after its own
message in on that channel is open (channels.SEND_RULES: 30 days for SMS,
24 hours on WhatsApp; there are no templates yet, so nothing goes outside
it), and never a second one before they write again. Harvey never writes
first on these channels.

Every row was approved by the user (approved_by "you"): the Handler queues
its drafts (and the agent's) waiting for review, an answer the user writes
on the Conversations page is approved as they write it, autopilot never
approves one (state.promote_for_autopilot is email only), and a row approved
any other way goes back to the Outbox. It runs in the messaging channels'
own loop (harvey/agents/message_loop.py), apart from email, and an answer
the user wrote is tried at once. Before each send (blocked; the channel
switched off, paused or with no sender is said first, then 1 (refusal(),
which the Conversations page asks before it queues anything), then the rest
of 2):

1. refused, for good (the row fails with why): an unknown channel; a number
   that isn't E.164, or for SMS one whose contact is known not to be a
   mobile; anything but an answer to a conversation whose number wrote in
   on this channel within the window; a number nPathi said the channel
   can't reach; the do-not-contact list (the number, the person's address,
   the firm, compliance.do_not_contact) or a person who opted out; what
   gate.message_problems refuses in the text (length, a curly brace, a
   link that isn't yours, a banned phrase, and on a first answer the
   firm's name and the opt-out line); a second message before they answered;
2. waiting, approved (the row says why and is tried each pass): the channel
   switched off or its sender not chosen (or one a reply can't come back
   to); sending paused (the global kill switch, the channel's own, or the
   npathi lane, paused when nPathi said the credits ran out); the channel's
   last read failed or didn't reach back (a STOP may be unread); outside
   the sending hours or in quiet hours; the channel's daily cap; the
   credits caps, and with a cap set an answer whose price isn't known (no
   rate, no price paid yet on the channel).

A send carries the row's own Idempotency-Key, so a send cut off (Harvey
stopped between claiming the row and recording the answer) is sent again
under the same key and nPathi answers with the first one instead of sending
twice; past nPathi's 24 hours it is failed as possibly sent instead. A 402
pauses the npathi lane (Coadă resumes it) and the row waits. Delivery is read
back for a week: a number on nPathi's opt-out list goes on the do-not-contact
list, an invalid number or one with no WhatsApp is marked unreachable for
that channel. None of it touches email's daily caps, bounce rate or kill
switch, and nothing here ever sends email.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from harvey import channels, gate, suppression
from harvey.agent.readonly import mask
from harvey.config import operator_timezone
from harvey.integrations import npathi
from harvey.models.conversation import Message
from harvey.state import MAYBE_SENT_PREFIX

logger = logging.getLogger("harvey.messenger")

# Answers sent per channel in one pass, at most.
MAX_SENDS_PER_CYCLE = 5
# A claimed answer not finished after this long was cut off mid-send.
STALE_AFTER = timedelta(minutes=10)
# nPathi keeps an Idempotency-Key 24 hours: a send is tried again under
# its key only inside this.
IDEMPOTENT_SECONDS = 23 * 3600
# Delivery is read back this long after the send.
READ_BACK_DAYS = 7
# nPathi's answers about an Idempotency-Key: the first send under it still
# running (tried again next pass), or what became of it unknown (failed as
# possibly sent: never sent again).
IDEMPOTENT_BUSY = frozenset({"idempotency_in_progress"})
IDEMPOTENT_UNKNOWN = frozenset({"idempotency_conflict", "idempotency_response_unavailable",
                                "idempotency_expired"})

NEEDS_YOU = "waiting for you: an SMS or WhatsApp answer is approved by you, one at a time"
WAIT_OFF = "waiting: answers aren't sent on this channel (Settings → Channels)"
WAIT_ROUTE = ("waiting: choose the sender answers go from on this channel, one a reply can "
              "come back to (Settings → Channels)")
WAIT_PAUSED = "waiting: sending is paused"
WAIT_READ = "waiting: this channel's messages weren't all read, so a STOP may be unread"
WAIT_HOURS = "waiting: outside the sending hours"
WAIT_CAP = "waiting: this channel's daily cap is reached"
WAIT_CREDITS = "waiting: the credits cap is reached"
WAIT_UNPRICED = ("waiting: a credits cap is set and the price of an answer here isn't known "
                 "(give the nPathi key access to the rates, or lift the cap)")
WAIT_RETRY = "waiting: nPathi Chat didn't take it this time; it is tried again under the same key"
WAIT_NO_CREDITS = "waiting: nPathi Chat says the workspace is out of credits"
OUTSIDE_WINDOW = ("they haven't written on this channel within its window, and Harvey only "
                  "answers (no templates yet)")


# The waits a row keeps (its error) by the name the page says them with.
WAIT_CODES = {NEEDS_YOU: "needs_you", WAIT_OFF: "off", WAIT_ROUTE: "route",
              WAIT_PAUSED: "paused", WAIT_READ: "read", WAIT_HOURS: "hours", WAIT_CAP: "cap",
              WAIT_CREDITS: "credits", WAIT_UNPRICED: "unpriced", WAIT_RETRY: "retry",
              WAIT_NO_CREDITS: "no_credits"}


class Hold(tuple):
    """Why an answer can't go now: (status, why), as blocked() always gave
    it, with ``code`` naming the reason for the page, which says it in the
    viewer's language (the sentence is the row's, the log's and the CLI's)."""

    code: str

    def __new__(cls, status: str, why: str, code: str = ""):
        hold = super().__new__(cls, (status, why))
        hold.code = code
        return hold


def wait_code(error: str) -> str:
    """The name of the wait an approved row's error says ("" for none)."""
    return WAIT_CODES.get(error or "", "")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


class MessageSender:
    def __init__(self, state, config, transport=None):
        self.state = state
        self.config = config
        # The HTTP transport (None: the network; tests give a fake nPathi Chat).
        self.transport = transport

    async def run(self) -> int:
        """Read back what was sent, send what is approved and may go; how
        many were sent. Never raises: a messaging failure never stops the
        loop it runs in, and nothing here waits on email."""
        try:
            client = await npathi.client(self.state, transport=self.transport)
            if client is None:
                return 0
            await self.read_back(client)
            on = await channels.sending(self.state)
            if not on:
                return 0
            await self._cut_off(client)
            sent = 0
            for channel in on:
                sent += await self._drain(client, channel)
            return sent
        except Exception as e:
            logger.error(f"Messenger: sending answers failed ({type(e).__name__}).")
            return 0

    # ── The rails ──

    async def blocked(self, item: dict, client=None) -> Hold | None:
        """Why this answer can't go now, as (status, why) with its ``code``:
        "failed" for a refusal, "approved" for a wait, "pending_review" for
        one you haven't approved; None when it may go."""
        state = self.state
        channel = item.get("channel") or ""
        if channel not in channels.SEND_RULES:
            return Hold("failed", f"no messaging channel {channel!r}", "channel")
        if item.get("approved_by") != "you":
            return Hold("pending_review", NEEDS_YOU, "needs_you")
        if channel not in await channels.sending(state):
            return Hold("approved", WAIT_OFF, "off")
        if await channels.paused(state, channel):
            return Hold("approved", WAIT_PAUSED, "paused")
        if await channels.route(state, channel) is None:
            return Hold("approved", WAIT_ROUTE, "route")
        if refused := await self.refusal(item):
            return refused
        if await channels.read_gap(state, channel) or await state.get_setting(
                channels.PROBLEM_KEY + channel):
            return Hold("approved", WAIT_READ, "read")
        if not await self._in_hours():
            return Hold("approved", WAIT_HOURS, "hours")
        if await state.messages_sent_today(channel) >= await channels.daily_cap(state, channel):
            return Hold("approved", WAIT_CAP, "cap")
        worst = await channels.worst_credits(state, channel, item.get("body") or "", client)
        room = await channels.credits_room(state, worst)
        if room == channels.UNPRICED:
            return Hold("approved", WAIT_UNPRICED, "unpriced")
        if room:
            return Hold("approved", WAIT_CREDITS, "credits")
        return None

    async def refusal(self, item: dict) -> Hold | None:
        """What refuses this answer for good (a "failed" Hold), whatever the
        channel's switches and the hours say; None when nothing does. The
        Conversations page asks it first, so an answer you write that could
        never go is refused there and never queued."""
        state = self.state
        channel, phone = item.get("channel") or "", item.get("to_phone") or ""
        rules = channels.SEND_RULES.get(channel)
        if rules is None:
            return Hold("failed", f"no messaging channel {channel!r}", "channel")
        if not phone or suppression.normalize_phone(phone) != phone:
            return Hold("failed", "gate: the recipient isn't a number in international form "
                        "(E.164)", "e164")
        if rules.needs_mobile and (await state.phone_line_type(phone)) not in (
                "", "unknown", "mobile"):
            return Hold("failed", "gate: SMS goes to a mobile only, and this number isn't one",
                        "not_mobile")
        handle = await state.conversation_reply_handle(item.get("conversation_id") or "") \
            if item.get("kind") == "reply" else None
        if handle is None or handle["channel"] != channel or handle["value"] != phone:
            return Hold("failed", "gate: only an answer to a message they sent on this channel",
                        "not_answer")
        last = await state.last_inbound(channel, phone)
        if last is None or time.time() - last > rules.window_hours * 3600:
            return Hold("failed", "gate: " + OUTSIDE_WINDOW, "window")
        if why := await state.unreachable(channel, phone):
            return Hold("failed", f"gate: nPathi Chat can't reach this number here ({why})",
                        "unreachable")
        prospect = await state.get_prospect(item["prospect_id"]) if item.get("prospect_id") \
            else None
        if prospect is not None and prospect.status == "opted_out":
            return Hold("failed", "gate: they asked not to be contacted", "opted_out")
        hit = await state.suppression_check(
            prospect.email if prospect is not None else "",
            firm=prospect.company_id if prospect is not None else handle.get("firm"),
            config=self.config, phone=phone)
        if hit is not None:
            # (Named by its kind, not its value: the number stays masked.)
            return Hold("failed", (f"gate: on the do-not-contact list ({hit.scope}, "
                                   f"{hit.reason.replace('_', ' ')})"), "dnc")
        mine = 1 if item.get("status") == "sending" else 0
        first = await state.messages_sent_to_since(channel, phone, "") - mine <= 0
        problems = gate.message_problems(item.get("body") or "", max_chars=rules.max_chars,
                                         config=self.config, first=first)
        if problems:
            return Hold("failed", ("gate: " + "; ".join(problems))[:300], "text")
        # (One that may have gone out counts: it is never followed by another.)
        if await state.messages_sent_to_since(channel, phone, _iso(last)) - mine > 0 or \
                await state.messages_maybe_sent_to_since(channel, phone, _iso(last)):
            return Hold("failed", "gate: a second message before they answered the first",
                        "second")
        return None

    async def _in_hours(self, at: datetime | None = None) -> bool:
        """Inside the sending hours (Harvey's own time zone) and outside
        quiet hours, which always apply here; now, or at ``at``."""
        return self._hours_open(await channels.send_hours(self.state),
                                at or datetime.now(timezone.utc))

    def _hours_open(self, hours: tuple[int, int], at: datetime) -> bool:
        from zoneinfo import ZoneInfo

        quiet = getattr(getattr(self.config, "usage", None), "quiet_hours", None)
        if quiet is not None:
            from harvey.main import in_quiet_hours

            if in_quiet_hours(self.config, at):
                return False
        try:
            zone = ZoneInfo(operator_timezone(self.config))
        except Exception:
            zone = timezone.utc
        now = at.astimezone(zone)
        minute = now.hour * 60 + now.minute
        start, end = hours
        return start <= minute < end if start <= end else (minute >= start or minute < end)

    async def goes_at(self, hold: "Hold") -> float | None:
        """The earliest time an answer held by the sending hours or the
        day's cap could go (epoch seconds), or None when that can't be told
        (a pause, a missing route...). Looked up in 5-minute steps, two days
        ahead at most."""
        if hold.code not in ("hours", "cap"):
            return None
        start = datetime.now(timezone.utc)
        if hold.code == "cap":
            # (The day's count starts again at UTC midnight.)
            start = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                        microsecond=0)
        hours = await channels.send_hours(self.state)
        for step in range(0, 2 * 24 * 12):
            at = start + timedelta(minutes=5 * step)
            if self._hours_open(hours, at):
                return at.timestamp()
        return None

    async def _hold(self, item: dict, blocked: tuple[str, str]) -> None:
        status, why = blocked
        if (item.get("status"), item.get("error") or "") != (status, why):
            # Only while it is as it was read: an answer you removed or
            # rejected in the meantime stays so (a wait would send it later).
            if not await self.state.update_outbox_item(item["id"], status=status,
                                                       error=why[:300],
                                                       if_status=item.get("status") or ""):
                return
        if status == "failed":
            await self.state.delete_setting(channels.ATTEMPT_KEY + item["id"])
            logger.warning(f"Messenger: an answer to {mask(item.get('to_phone') or '')} "
                           f"won't go: {why}")

    # ── Sending ──

    async def _drain(self, client, channel: str) -> int:
        sent = 0
        for item in await self.state.get_outbox(status="approved", due_before=_now_iso(),
                                                limit=MAX_SENDS_PER_CYCLE * 4, channel=channel):
            if sent >= MAX_SENDS_PER_CYCLE:
                break
            sent += await self._try(client, item)
        return sent

    async def send_one(self, client, item_id: str) -> int:
        """Try one approved answer now (the one you just wrote): sent (1),
        or held or refused with why on its row as the drain would (0)."""
        item = await self.state.get_outbox_item(item_id)
        if item is None or item.get("status") != "approved" or \
                (item.get("channel") or "email") == "email":
            return 0
        return await self._try(client, item)

    async def _try(self, client, item: dict) -> int:
        blocked = await self.blocked(item, client)
        if blocked:
            await self._hold(item, blocked)
            return 0
        # Claim it (a new message from them, a rejection or a STOP may
        # have cancelled it since), then check again on the row as it is.
        if not await self.state.claim_outbox_item(item["id"]):
            return 0
        item = await self.state.get_outbox_item(item["id"]) or item
        blocked = await self.blocked(item, client)
        if blocked:
            await self._hold(item, blocked)
            return 0
        return await self._send(client, item)

    async def _cut_off(self, client) -> None:
        """Answers claimed and never finished: back in line, to go again
        under the same Idempotency-Key (refused now: failed, as possibly sent)."""
        cutoff = (datetime.now(timezone.utc) - STALE_AFTER).replace(tzinfo=None).isoformat()
        for item_id in await self.state.stale_message_sends(cutoff):
            item = await self.state.get_outbox_item(item_id)
            if item is None:
                continue
            blocked = await self.blocked(item, client)
            if blocked and blocked[0] == "failed":
                await self._hold(item, ("failed", MAYBE_SENT_PREFIX + blocked[1]))
                continue
            await self.state.update_outbox_item(item_id, status="approved", error=WAIT_RETRY)

    async def _send(self, client, item: dict) -> int:
        state, channel, phone = self.state, item["channel"], item["to_phone"]
        attempt = channels.ATTEMPT_KEY + item["id"]
        try:
            first_try = float(await state.get_setting(attempt) or 0)
        except ValueError:
            first_try = 0.0
        if first_try and time.time() - first_try > IDEMPOTENT_SECONDS:
            # (Its key is gone at nPathi: sending again could send it twice.)
            await self._hold(item, ("failed", MAYBE_SENT_PREFIX + "Harvey couldn't finish "
                                    "sending it within a day; check nPathi Chat before "
                                    "answering again"))
            return 0
        if not first_try:
            await state.set_setting(attempt, str(int(time.time())))
        route = await channels.route(state, channel) or {}
        try:
            out = await client.send(channel, phone, item["body"], route,
                                    key=f"harvey-outbox-{item['id']}",
                                    reference=f"harvey:{item['id']}")
        except npathi.NpathiError as e:
            return await self._refused(item, e)
        await state.update_outbox_item(
            item["id"], status="sent", sent_at=_now_iso(), error="", message_id=str(out.id),
            provider_status=out.status or "queued", price_credits=out.price)
        await state.delete_setting(attempt)
        if out.status == "failed":
            await self._failed({**item, "message_id": str(out.id)}, out.failure)
        await self._thread(item)
        await state.log_action("message_sent", "messenger", {
            "outbox_id": item["id"], "channel": channel, "to": mask(phone),
            "conversation_id": item.get("conversation_id") or "", "credits": out.price})
        logger.info(f"Messenger: answer sent to {mask(phone)} by {npathi.LABELS[channel]}.")
        return 1

    async def _refused(self, item: dict, e: npathi.NpathiError) -> int:
        """nPathi didn't take a send: wait and go again under the same key
        (it may not have been sent), pause the lane when the credits ran
        out, or fail it when nPathi refused it for good."""
        if e.kind == "credits":
            from harvey.modules import jobs

            await jobs.pause_lane(self.state, npathi.LANE, npathi.NO_CREDITS)
            await self.state.update_outbox_item(item["id"], status="approved",
                                                error=WAIT_NO_CREDITS)
            logger.warning("Messenger: nPathi Chat is out of credits; the npathi lane is paused "
                           "(resume it on Coadă once credits are added).")
            return 0
        if e.code in IDEMPOTENT_BUSY:
            # (nPathi is still working on the first send under this key: the
            # next pass's retry gets its answer.)
            await self.state.update_outbox_item(item["id"], status="approved", error=WAIT_RETRY)
            return 0
        if e.code in IDEMPOTENT_UNKNOWN:
            # nPathi can't say what became of the first send under this key:
            # it may have gone, so it is never sent again.
            await self._hold(item, ("failed", MAYBE_SENT_PREFIX + "nPathi Chat couldn't say "
                                    f"whether the first try went ({e.code}); check nPathi "
                                    "Chat before answering again"))
            return 0
        if e.kind in ("refused", "forbidden", "not_found"):
            if e.code == "contact_unsubscribed":
                await self._suppress(item["to_phone"], f"npathi:refused:{item['id']}")
            await self._hold(item, ("failed", f"nPathi Chat refused it ({e.kind}"
                                    + (f": {e.code}" if e.code else "") + ")"))
            return 0
        # rate, unreachable, server, unreadable, key, setup: maybe not sent.
        await self.state.update_outbox_item(item["id"], status="approved", error=WAIT_RETRY)
        logger.info(f"Messenger: an answer waits for the next pass ({e.kind}).")
        return 0

    async def _thread(self, item: dict) -> None:
        """A sent answer belongs in its conversation's thread."""
        convo = await self.state.get_conversation(item.get("conversation_id") or "")
        if convo is None:
            return
        convo.thread.append(Message(sender="harvey", content=item["body"],
                                    channel=item["channel"]))
        await self.state.update_conversation(convo.id, thread_json=convo.thread_json())

    # ── Delivery ──

    async def read_back(self, client) -> None:
        """Where each answer sent in the last week is now, until nPathi is
        done with it; a failure is acted on (_failed)."""
        since = (datetime.now(timezone.utc) - timedelta(days=READ_BACK_DAYS)).replace(
            tzinfo=None).isoformat()
        for row in await self.state.messages_in_flight(since):
            try:
                out = await client.message(int(row["message_id"]))
            except (TypeError, ValueError):
                continue
            except npathi.NpathiError as e:
                if e.kind in ("not_found", "refused"):
                    continue
                logger.info(f"Messenger: delivery not read back ({e.kind}); next pass.")
                return
            fields = {"provider_status": out.status or "queued"}
            if out.price is not None:
                fields["price_credits"] = out.price
            await self.state.update_outbox_item(row["id"], **fields)
            if out.status == "failed":
                await self._failed(row, out.failure)

    async def _failed(self, row: dict, failure: str) -> None:
        """An answer nPathi couldn't deliver: on its opt-out list, the
        number goes on the do-not-contact list; an invalid number (or one
        with no WhatsApp) is marked unreachable there; out of credits pauses
        the lane. Email's bounce rate and kill switch are never touched."""
        state, phone, channel = self.state, row["to_phone"], row["channel"]
        await state.update_outbox_item(
            row["id"], status="failed",
            error=f"nPathi Chat couldn't deliver it ({failure or 'other'})")
        if failure == "unsubscribed":
            await self._suppress(phone, f"npathi:{row.get('message_id') or ''}")
        elif failure == "invalid_number":
            await state.mark_unreachable([channel], phone, failure)
        elif failure == "no_whatsapp":
            await state.mark_unreachable(("waba", "wa"), phone, failure)
        elif failure == "insufficient_credits":
            from harvey.modules import jobs

            await jobs.pause_lane(state, npathi.LANE, npathi.NO_CREDITS)
        await state.log_action("message_failed", "messenger", {
            "outbox_id": row["id"], "channel": channel, "to": mask(phone),
            "failure": failure or "other"})

    async def _suppress(self, phone: str, ref: str) -> None:
        """nPathi says the number is on its opt-out list: onto Harvey's,
        unless the user took it off Harvey's (nPathi's own list is theirs to
        change there, as the daily sync keeps it: suppress_phones)."""
        await self.state.suppress_phones([phone], "by_hand", by=npathi.MODULE,
                                         note="on nPathi Chat's opt-out list",
                                         listed_at={phone: None})

