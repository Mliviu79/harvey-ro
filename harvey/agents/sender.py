"""Sender — sends through the configured mail provider (Gmail, SMTP,
Microsoft 365) by way of the outbox approval ladder.

  1. An active campaign's ready members are STAGED: merge variables
     rendered per member, one outbox row per (member, step) with a
     scheduled send_at, always waiting for review.
  2. Each heartbeat DRAINS due approved rows (on autopilot it first
     approves the due staged ones it may send): kill-switch check, the
     campaign and member, stop-on-reply check, deterministic pre-send gate,
     then provider.send_email with jittered pacing and the daily cap.
"""

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

from harvey.brain import Brain
from harvey.config import (
    HarveyConfig, EnvConfig, cold_daily_cap, compliance_footer_lines, compliance_notice,
    compliance_of, operator_timezone, provider_notice, send_window_of,
)
from harvey import email_rules, unsubscribe
from harvey.gate import autopilot_review, pre_send_check
from harvey.send_window import in_window, next_send_time, recipient_country, recipient_zone
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from harvey.integrations.precleared import PreclearedClient, clear_hold, record_hold
from harvey.sending_mode import sync_sending_mode
from harvey.state import (
    ACK_NEEDED, AUTOPILOT_OFF_REASON, CAMPAIGN_NOT_STARTED, CAMPAIGN_PAUSED, CAMPAIGN_REVIEWS,
    COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW,
    MANUAL_SEND_KEY, MAYBE_SENT_PREFIX, NO_SENDER_ADDRESS, NOTE_ADDED, PRIVACY_LINK_HELD,
    READ_GAP_HELD,
    READ_GAP_KEY, RECHECK_PREFIX, StateManager,
)

# A follow-up waits at least this long after the email it follows went out,
# whatever its schedule says.
MIN_FOLLOW_UP_GAP = timedelta(days=1)

logger = logging.getLogger("harvey.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
# How many due emails quiet hours look through for one that can go now.
QUIET_HOURS_SCAN = 200
SEND_JITTER_SECONDS = (4, 15)
# Optional Precleared check: an email whose address Precleared couldn't
# check is moved back this far, behind other email, rather than asked about
# again every cycle. Failures for this many different domains with no answer
# in between (across cycles) mean Precleared itself is in trouble, so checks
# pause. And no new check starts after this many seconds of checking in one
# cycle.
ADDRESS_RETRY_AFTER = timedelta(hours=6)
DOMAINS_FAILING_TO_PAUSE = 2
CHECK_SECONDS_PER_CYCLE = 90

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


def _utcnow() -> datetime:
    """Naive UTC now, as the outbox stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.provider = get_mail_provider(config, env)
        # Optional per-recipient compliance check; None unless enabled.
        self.precleared = PreclearedClient.from_config(config, env)
        # Domains whose check failed since Precleared last answered. Kept
        # across cycles: one new address a cycle failing is still a pattern.
        self._failing_domains: set[str] = set()
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        # Set by the heartbeat in quiet hours (send windows in recipients'
        # zones): only cold email goes, inside its window; replies wait for
        # the operator's own hours, as they always have.
        self.cold_only = False
        # The last drain couldn't ask Precleared: quiet hours wake only for
        # cold email it has already cleared until a drain outside them has run.
        self.compliance_held = False

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def _send_zone(self, prospect, country: str = "", given: str = "",
                         names: tuple[str, str] = ("", "")) -> str:
        """The time zone this recipient's send window is in: their own when
        it can be told (with recipient_timezone on), otherwise the zone in
        usage.quiet_hours, as before. ``country`` is the recipient's country
        from Precleared, ``given`` the one Harvey gave the check (see
        _recipient_country) and ``names`` the company and person names it
        gave (see _recipient_names), which a cached verdict is keyed on."""
        window = send_window_of(self.config)
        if window is not None and window.recipient_timezone:
            if not country and self.precleared is not None and prospect is not None:
                # A country Precleared already found counts on every check,
                # not only on the one that asked it.
                company, name = names
                country = await self.precleared.known_country(
                    prospect.email, self.state, given, company=company, name=name)
            zone = await recipient_zone(self.state, prospect, self.config, country)
            if zone:
                return zone
        return operator_timezone(self.config)

    async def _zone_or_default(self, prospect, country: str = "", given: str = "",
                               names: tuple[str, str] = ("", "")) -> str:
        """_send_zone, but a failure to work the zone out falls back to
        usage.quiet_hours.timezone for this one email instead of stopping
        the whole drain (replies included)."""
        try:
            return await self._send_zone(prospect, country, given, names)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the time zone for "
                f"{getattr(prospect, 'email', '?')}, using quiet_hours.timezone: {e}")
            return operator_timezone(self.config)

    async def _recipient_country(self, prospect) -> str:
        """The recipient's country when Harvey is sure of it, for the
        compliance check (see send_window.recipient_country). A failure to
        work it out means none for this one email, which Precleared then
        works out itself, instead of stopping the drain."""
        try:
            return await recipient_country(self.state, prospect)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the country for "
                f"{getattr(prospect, 'email', '?')}, leaving it to Precleared: {e}")
            return ""

    async def _recipient_names(self, prospect) -> tuple[str, str]:
        """The company's name and the person's full name, for the compliance
        check: the company record's name, else the prospect's own company
        field, and the prospect's first and last name. A company inbox has no
        person, so no name. A failure to load the company record falls back
        to the prospect's own field for this one email instead of stopping
        the drain."""
        if prospect is None:
            return "", ""
        company = ""
        if getattr(prospect, "company_id", ""):
            try:
                record = await self.state.get_company(prospect.company_id)
                company = (getattr(record, "name", "") or "") if record else ""
            except Exception as e:
                logger.warning(
                    f"Sender: couldn't load the company for "
                    f"{getattr(prospect, 'email', '?')}, using the prospect's own: {e}")
        company = company.strip() or (getattr(prospect, "company", "") or "").strip()
        name = ""
        if not getattr(prospect, "is_company_inbox", False):
            first = (getattr(prospect, "first_name", "") or "").strip()
            last = (getattr(prospect, "last_name", "") or "").strip()
            name = f"{first} {last}".strip()
        return company, name

    async def _has_cached_verdict(self, item: dict) -> bool:
        """Use the send loop's recipient facts when counting held email."""
        prospect = await self.state.get_prospect(item["prospect_id"])
        country = await self._recipient_country(prospect)
        company, name = await self._recipient_names(prospect)
        return await self.precleared.cached(
            item["to_email"], self.state, country, company=company, name=name) is not None

    async def cold_due_in_window(self) -> bool:
        """Quiet hours: whether a cold email is due, inside its recipient's
        send window and free to go now (sending not paused, under the daily
        and warmup caps, and after a compliance hold only to someone
        Precleared has already cleared). Only then is it worth waking to read
        replies and send."""
        window = send_window_of(self.config)
        if window is None or not self.is_native:
            return False
        if not self.provider.is_configured() or await self.state.get_setting(KILL_SWITCH_KEY):
            return False
        email = self.config.channels.email
        if email.max_daily_sends - await self.state.count_outbox_sent_today() <= 0:
            return False
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        if cold_daily_cap(email) - await self.state.count_outbox_sent_today(
                kind="sequence",
                since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()) <= 0:
            return False
        now = _utcnow()
        due_before = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        await self._promote(due_before, QUIET_HOURS_SCAN)
        due = await self.state.get_outbox(
            status="approved", due_before=due_before,
            limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, skip_held_campaigns=True,
            kind="sequence",
        )
        if self.compliance_held and self.precleared is not None:
            # Match the drain's separate follow-up scan: waiting first emails
            # must not hide a cleared follow-up and prevent the loop waking.
            seen = {item["id"] for item in due}
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=due_before,
                limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, skip_held_campaigns=True,
                follow_ups_only=True,
            )
            due += [item for item in follow_ups if item["id"] not in seen]
        for item in due:
            if item["kind"] != "sequence":
                continue
            prospect = await self.state.get_prospect(item["prospect_id"])
            country = await self._recipient_country(prospect) if self.precleared is not None else ""
            names = await self._recipient_names(prospect) if self.precleared is not None else ("", "")
            if self.compliance_held and self.precleared is not None:
                # New addresses wait for a drain outside quiet hours; a
                # stored verdict still lets email go, as in the drain.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=names[0], name=names[1])
                if clearance is None or not clearance.allowed:
                    continue
            if prospect is not None and in_window(
                    now, await self._zone_or_default(prospect, given=country, names=names), window):
                return True
        return False

    async def _outside_window(self, item: dict, zone: str, window) -> bool:
        """Move a cold email that isn't in its send window to the window's
        next opening. True when it was moved and must wait."""
        now = _utcnow()
        if in_window(now, zone, window):
            return False
        later = next_send_time(now, zone, window)
        await self.state.update_outbox_item(item["id"], send_at=later.isoformat())
        return True

    async def run(self):
        """Drain the outbox through the mail provider (nothing with a
        provider Harvey doesn't support: config.provider_notice)."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return
        if not self.is_native:
            logger.warning(f"Sender: {provider_notice(self.config)}")
            return
        await self._run_native()

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    # ── The outbox ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        # Before any early return: a send cut off mid-way must show up as
        # failed even while sending is paused or no mailbox is connected.
        stale = await self.state.fail_stale_sending_outbox()
        if stale:
            logger.warning(
                f"Sender: {stale} email(s) were interrupted mid-send and won't be "
                "retried. Check your Sent folder; they're under Didn't send."
            )
        # The Writer's campaigns ('auto') had their first emails drafted when
        # they were made: their members are staged here. A campaign made by
        # the user is staged by the campaign cycle, after its drafts are
        # written (agents/campaigner.py). Staging goes on while sending is
        # paused: it only fills the review queue (nothing is approved until
        # a drain that sends it, promote_for_autopilot). One an earlier
        # release deployed to Instantly goes on there, never from here.
        for campaign in await self.state.get_campaigns_by_status("active"):
            if campaign.origin != "auto" or campaign.instantly_campaign_id:
                continue
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `harvey sending resume` once the cause is fixed."
            )
            return

        await self._drain_due()

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            # A company inbox has no name; "there" keeps "Hi {{first_name}}," readable.
            "first_name": prospect.first_name or ("there" if prospect.is_company_inbox else ""),
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        # "{{company}}'s" for "Parkes Builders" should read "Builders'".
        company = prospect.company or ""
        if company.endswith(("s", "S")):
            for tag in ("{{company}}", "{{ company }}"):
                text = text.replace(tag + "'s", company + "'")
                text = text.replace(tag + "’s", company + "’")
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def stageable(self, campaign_id: str, member: dict, prospect,
                        sendable_email) -> tuple[str, str] | None:
        """Why a ready member can't be staged now, as (its new member
        status, why), or None when it can: the checks staging always made
        (a usable address that may be sent to, nobody at the company has
        answered, not on the do-not-contact list), plus their own answer, a
        hand-sent email, and another campaign holding the address."""
        from harvey import campaigns

        if prospect is None or not prospect.email or not EMAIL_RE.match(prospect.email.strip()):
            return "skipped", "no usable address"
        if prospect.status in campaigns.ANSWERED:
            return "replied", f"prospect status '{prospect.status}'"
        if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
            return "stopped", HAND_SENT_REASON
        estatus = prospect.email_status or "guess"
        if estatus not in sendable_email:
            if estatus in ("", "unchecked"):
                return "waiting", "not checked yet"
            return "skipped", f"email status '{estatus}'"
        if colleague := await self.state.colleague_who_replied(prospect.id):
            return "stopped", f"{COLLEAGUE_REPLIED} ({colleague})"
        if hit := await self.state.suppression_check(prospect.email, firm=prospect.company_id,
                                                     config=self.config):
            return "suppressed", hit.text
        held = await self.state.campaigns_call(campaigns.overlap, prospect.email, campaign_id,
                                               write=False)
        if held:
            return "stopped", f"overlap: {held}"
        return None

    async def _stage_campaign_native(self, campaign, emails=None):
        """Render + schedule an active campaign's ready members into the
        outbox (``emails``: only those). Always waiting for review: autopilot
        approves when a drain is about to send it (promote_for_autopilot)."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        held: dict[str, int] = {}
        now = _utcnow()
        window = send_window_of(self.config)
        wanted = {e.strip().lower() for e in emails} if emails is not None else None

        for member in await self.state.campaign_members(campaign.id, status="ready", limit=10_000):
            email = member["email"]
            if wanted is not None and email not in wanted:
                continue
            prospect = await self.state.get_prospect(member["prospect_id"]) \
                if member["prospect_id"] else await self.state.get_prospect_by_email(email)
            why = await self.stageable(campaign.id, member, prospect, sendable_email)
            if why is not None:
                await self.state.set_campaign_member(campaign.id, email, why[0], why[1])
                held[why[0]] = held.get(why[0], 0) + 1
                continue
            # A first email to a personal address carries where the address
            # was found (email_rules), decided now and kept on the row.
            _, note = await self.state.personal_address(
                email, privacy_url=compliance_of(self.config).privacy_url)

            # With a send window, each email is planned for the recipient's
            # office hours; follow-ups keep their delay, then move into it.
            zone = await self._zone_or_default(prospect) if window is not None else ""
            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                if window is not None:
                    send_at = next_send_time(send_at, zone, window)
                body = self._render(step.body, prospect)
                first = int(step.step) <= 1
                if first:
                    body = email_rules.with_note(body, note)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=body,
                    send_at=send_at.isoformat(),
                    status="pending_review",
                    provider=self.provider.name,
                    source_note=note if first else "",
                    account_id=campaign.account_id or 1,
                    error=PRIVACY_LINK_HELD if first and note and email_rules.privacy_crowded(
                        body, compliance_of(self.config).privacy_url) else "",
                )
                if item_id:
                    staged += 1
            await self.state.set_campaign_member(campaign.id, email, "staged")
            # Conditional: never overwrite a status set while staging
            # (marked sent by hand, a reply).
            await self.state.queue_prospect_if_unsent(prospect.id)

        for status, n in held.items():
            logger.info(f"Sender: {n} member(s) of '{campaign.name}' not staged ({status}).")
        if staged:
            mode = "awaiting your approval"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )
        return staged

    def _with_footer(self, body: str) -> str:
        """A cold email as it leaves: the postal address and opt-out line
        underneath (CAN-SPAM). Added at send time, so a changed address
        applies to mail already queued; not added again when the body already
        ends with it."""
        footer = "\n".join(compliance_footer_lines(self.config))
        if not footer or body.rstrip().endswith(footer):
            return body
        return body.rstrip() + "\n\n" + footer

    def _disclosure(self) -> str:
        return compliance_of(self.config).ai_disclosure_line.strip()

    def _outgoing_body(self, item: dict) -> str:
        """The exact body that goes out: cold email gets the footer; an
        answer autopilot approved (nobody read it) gets the AI disclosure
        line; an answer you approved goes as you approved it."""
        if item["kind"] == "sequence":
            return self._with_footer(item["body"])
        line = self._disclosure()
        if (item["kind"] == "reply" and item.get("approved_by") == "autopilot" and line
                and not item["body"].rstrip().endswith(line)):
            return item["body"].rstrip() + "\n\n" + line
        return item["body"]

    async def _follow_up_order(self, item: dict, prospect=None) -> str | tuple[str, str] | None:
        """For a follow-up: "wait" until the email before it went out and its
        gap has passed, (status, error) when that email never will, else None.

        Without this, a step held for any reason (approval, a pause, a
        compliance hold, the warmup ramp) lets its follow-ups' scheduled
        times arrive first: a late "Approve all" sent the whole sequence in
        one go, or step 2 before step 1.
        """
        if item["kind"] != "sequence" or int(item["step"]) <= 1:
            return None
        prev = await self.state.get_previous_step(item)
        if prev is None:
            return None
        if prev["status"] in ("pending_review", "approved", "sending"):
            return "wait"
        if prev["status"] != "sent":
            return "cancelled", f"step {prev['step']} wasn't sent ({prev['status']})"

        def _at(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None
        sent_at = _at(prev["sent_at"])
        if sent_at is None:
            return None
        delay = await self.state.get_step_delay_days(item.get("campaign_id"), item["step"])
        if delay is None:  # campaign unknown: fall back to the planned spacing
            planned, prev_planned = _at(item["send_at"]), _at(prev["send_at"])
            gap = (planned - prev_planned) if planned and prev_planned else MIN_FOLLOW_UP_GAP
        else:
            gap = timedelta(days=delay)
        ready_at = sent_at + max(MIN_FOLLOW_UP_GAP, gap)
        window = send_window_of(self.config)
        if window is not None and prospect is not None:
            ready_at = next_send_time(ready_at, await self._zone_or_default(prospect), window)
        if _utcnow() >= ready_at:
            return None
        # Reschedule it for then, so it leaves the due queue until it's ready.
        await self.state.update_outbox_item(item["id"], send_at=ready_at.isoformat())
        return "wait"

    async def _retry_later(self, item: dict, reason: str) -> datetime:
        """Move a cold email back by ADDRESS_RETRY_AFTER, saying why; return
        the new time. Sending it clears the note."""
        retry_at = _utcnow() + ADDRESS_RETRY_AFTER
        await self.state.update_outbox_item(
            item["id"], send_at=retry_at.isoformat(),
            error=f"{RECHECK_PREFIX}{retry_at:%Y-%m-%d %H:%M} UTC: {reason}"[:300])
        return retry_at

    async def _campaign_hold(self, item: dict) -> tuple[str, str] | None:
        """A cold email's campaign and its member, checked first: the
        campaign must be active (paused: the email waits, approved; ended:
        cancelled) and the member still open there. None for an email of no
        campaign Harvey keeps (or a member it never kept)."""
        from harvey import campaigns

        campaign = await self.state.campaign_record(item.get("campaign_id") or "")
        if campaign is None:
            return None
        if campaign["status"] == "paused":
            return "approved", CAMPAIGN_PAUSED
        if campaign["status"] == "draft":
            return "approved", CAMPAIGN_NOT_STARTED
        if campaign["status"] != "active":
            return "cancelled", f"campaign {campaign['status']}"
        member = await self.state.campaign_member(campaign["id"], item["to_email"])
        if member is not None and member["end_reason"] == HAND_SENT_REASON:
            return "cancelled", HAND_SENT_REASON     # what undoing a hand send restores
        if member is not None and member["status"] not in campaigns.OPEN_MEMBER:
            return "cancelled", (f"member {member['status_name']}"
                                 + (f": {member['end_reason']}" if member["end_reason"] else ""))
        return None

    async def _blocked(self, item: dict, prospect, allow_risky: bool) -> tuple[str, str] | None:
        """Why this email must not go out, as (status, error), or None.

        In this order, for cold email: its campaign is active and the
        member still open there (_campaign_hold); the person hasn't answered
        or opted out; nobody emailed them by hand (MANUAL_SEND_KEY); a first
        email isn't a second one (this campaign sent one already, another
        campaign holds the address, or the prospect was contacted outside
        any campaign); nobody at their company answered (COMPANY_STOP_KEY,
        any campaign). Then, for every email: the do-not-contact list; the
        acknowledgement, sending address and source note for cold email; the
        gate; autopilot still on (and the campaign not asking for review);
        the autopilot review. The drain also checks a follow-up's order, the
        compliance check, and all of this again once it has claimed the row."""
        from harvey import campaigns

        await sync_sending_mode(self.config, self.state)   # "Turn off autopilot" counts at once
        if prospect is None:
            return "failed", "prospect missing"
        if item["kind"] == "sequence":
            held = await self._campaign_hold(item)
            if held is not None:
                return held
            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if prospect.status in STOP_STATUSES:
                return "cancelled", f"prospect status '{prospect.status}'"
            # The user already emailed them by hand: whatever Harvey staged
            # (before, during or after that) would be a second email.
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                return "cancelled", HAND_SENT_REASON
            # A first email to someone already contacted is always a duplicate.
            if int(item["step"]) <= 1:
                member = await self.state.campaign_member(item.get("campaign_id") or "",
                                                          item["to_email"])
                if member is None and prospect.status == "contacted":
                    return "cancelled", "already contacted"
                if member is not None:
                    why = await self.state.campaigns_call(
                        campaigns.already_contacted, item["to_email"], item["campaign_id"],
                        item["id"], write=False)
                    if why:
                        return "cancelled", why
            # Someone else at their company answered: the rest of the
            # company gets no more cold email.
            colleague = await self.state.colleague_who_replied(prospect.id)
            if colleague:
                return "cancelled", f"{COLLEAGUE_REPLIED} ({colleague})"
        # The do-not-contact list (an entry or compliance.do_not_contact),
        # replies included: "never contact" means never.
        hit = await self.state.suppression_check(item["to_email"], firm=prospect.company_id,
                                                 config=self.config)
        personal, note = 0, ""
        acknowledged = item["kind"] != "sequence" or await self.state.acknowledged()
        if item["kind"] == "sequence" and hit is None:
            # Cold email waits, approved, for the operator's acknowledgement.
            if not acknowledged:
                return "approved", ACK_NEEDED
            # Its unsubscribe header names the sending address: unknown
            # (no persona.email, and the provider couldn't say), it waits.
            if getattr(self.provider, "supports_headers", False) \
                    and not await self._sending_address():
                return "approved", NO_SENDER_ADDRESS
            if int(item["step"]) <= 1:
                personal, note = await self.state.personal_address(
                    item["to_email"], privacy_url=compliance_of(self.config).privacy_url)
                note = item.get("source_note") or note
                if personal and not (item.get("source_note")
                                     and item["source_note"] in (item["body"] or "")):
                    # Staged before the address was known to be personal (or
                    # the line was edited out): it gets the line, and waits
                    # for you to approve it as it now is.
                    await self.state.update_outbox_item(
                        item["id"], body=email_rules.with_note(item["body"], note),
                        source_note=note)
                    return "pending_review", NOTE_ADDED
                if personal and email_rules.privacy_crowded(
                        item["body"], compliance_of(self.config).privacy_url):
                    # The gate would fail it for good: it waits for you.
                    return "pending_review", PRIVACY_LINK_HELD
        gate = pre_send_check(
            item["to_email"], item["subject"], self._outgoing_body(item),
            prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            postal_address=compliance_of(self.config).postal_address,
            suppressed=hit.text if hit is not None else "",
            acknowledged=acknowledged,
            step=int(item["step"] or 1), personal=bool(personal), source_note=note,
        )
        if not gate:
            logger.warning(
                f"Sender: gate blocked email to {item['to_email']}: "
                f"{gate.reasons}"
            )
            return "failed", "gate: " + "; ".join(gate.reasons)[:300]
        if (item.get("approved_by") == "autopilot"
                and getattr(self.config.channels.email, "require_approval", True)):
            # Autopilot approved it, and autopilot has since been turned off.
            return "pending_review", AUTOPILOT_OFF_REASON
        if (item.get("approved_by") == "autopilot" and item["kind"] == "sequence"
                and await self.state.campaigns_call(campaigns.review_always,
                                                    item.get("campaign_id") or "", write=False)):
            # The campaign asks for every email to be reviewed, autopilot or not.
            return "pending_review", CAMPAIGN_REVIEWS
        if item.get("approved_by") != "you":
            # Nobody approved this one (autopilot): what a planted
            # instruction would add sends it back to you instead.
            # (The source note is Harvey's fixed line, naming the site the
            # address was found on: not a link a model added.)
            # (Nor is the disclosure line under an answer: the operator's.)
            body = self._outgoing_body(item)
            if item.get("source_note"):
                body = body.replace(item["source_note"], "")
            if item["kind"] == "reply" and self._disclosure():
                body = body.replace(self._disclosure(), "")
            held = autopilot_review(item["subject"], body, self.config,
                                    recipient=item["to_email"])
            if held:
                logger.warning(f"Sender: holding email to {item['to_email']} for review: {held}")
                return "pending_review", (HELD_FOR_REVIEW + "; ".join(held))[:300]
        return None

    async def _sending_address(self) -> str:
        """The address cold email goes out from (its unsubscribe comes back
        there), or "" when neither persona.email nor the provider says."""
        try:
            address = (await self.provider.sender_address() or "").strip()
        except Exception as e:
            logger.warning(f"Sender: couldn't ask {self.provider.name} for its address: {e}")
            address = ""
        return address or (getattr(getattr(self.config, "persona", None), "email", "")
                           or "").strip()

    async def _unsubscribe_headers(self, item: dict) -> dict:
        """The unsubscribe header a cold email goes out with, as send_email
        keyword arguments ({} for a reply, or a provider that can't carry
        headers). Its token is stored on the row before it goes, so the
        unsubscribe it brings back names this email."""
        if item["kind"] != "sequence" or not getattr(self.provider, "supports_headers", False):
            return {}
        address = await self._sending_address()
        if not address:
            return {}           # never reached: _blocked holds it first
        token = item.get("unsub_token") or unsubscribe.new_token()
        if token != item.get("unsub_token"):
            await self.state.update_outbox_item(item["id"], unsub_token=token)
        return {"headers": unsubscribe.headers(address, token)}

    async def _record_block(self, item: dict, blocked: tuple[str, str]) -> None:
        """Keep why an email didn't go; a cancelled or failed cold email
        also ends its member's place in the campaign, with the same reason
        (undoing a hand send finds it by that reason). A failed one is never
        retried, so its member would otherwise stay open for good."""
        await self.state.update_outbox_item(item["id"], status=blocked[0], error=blocked[1])
        if blocked[0] in ("cancelled", "failed") and item["kind"] == "sequence" \
                and item.get("campaign_id"):
            member = await self.state.campaign_member(item["campaign_id"], item["to_email"])
            if member is not None and member["status_name"] in ("staged", "in_sequence"):
                await self.state.set_campaign_member(item["campaign_id"], item["to_email"],
                                                     "stopped", blocked[1])

    async def _end_after_failed_send(self, item: dict, *, maybe_sent: bool) -> None:
        """A failed send is never retried, so a cold email's member ends
        there (campaigns.stop_after), as when a send is cut off: nothing
        follows an email that may have arrived, or one that never went, and
        a campaign that doesn't roll can finish."""
        if item["kind"] != "sequence" or not item.get("campaign_id"):
            return
        from harvey import campaigns

        await self.state.campaigns_call(
            campaigns.stop_after, item["campaign_id"], item["to_email"],
            int(item.get("step") or 1),
            campaigns.CUT_OFF if maybe_sent else campaigns.SEND_FAILED)

    async def _promote(self, due_before: str, limit: int, kind: str = "sequence") -> None:
        """On autopilot, approve the staged email of ``kind`` this drain may
        send (state.promote_for_autopilot): approval happens at send time,
        after the autopilot review, never when the email was staged or the
        Handler queued its answer."""
        await sync_sending_mode(self.config, self.state)
        if getattr(self.config.channels.email, "require_approval", True):
            return
        promoted = await self.state.promote_for_autopilot(due_before, limit, kind=kind)
        if promoted:
            what = "answer(s)" if kind == "reply" else "staged email(s)"
            logger.info(f"Sender: autopilot approved {promoted} {what} to send now.")

    async def _drain_due(self):
        """Send due, approved outbox items through the provider."""
        if self.precleared is None:
            # Check switched off: cold email no longer waits on it, so a
            # hold recorded while it was on must not stay on Today.
            await clear_hold(self.state)
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        max_daily = self.config.channels.email.max_daily_sends
        sent_today = await self.state.count_outbox_sent_today()
        budget = min(MAX_SENDS_PER_CYCLE, max_daily - sent_today)
        if budget <= 0:
            logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}).")
            return
        # Cold email has its own cap under the warmup ramp; replies don't.
        # Counted from local midnight, the day the ramp counts in.
        cold_cap = cold_daily_cap(self.config.channels.email)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        cold_left = cold_cap - await self.state.count_outbox_sent_today(
            kind="sequence",
            since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat())

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # No more than the cold cap leaves for today: what it would hold
        # stays waiting for review, not shown approved.
        await self._promote(now, max(min(budget * 3, cold_left), 0))
        if not self.cold_only:
            await self._promote(now, budget * 3, kind="reply")
        # In quiet hours only cold email can go, so only cold email is
        # fetched: replies due ahead of it can't fill the page.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            skip_unready_follow_ups=True, skip_held_campaigns=True,
            kind="sequence" if self.cold_only else None,
        )
        # Cold emails held (by an unavailable compliance check, the cold
        # cap, a follow-up's spacing) stay approved and could fill the
        # page above, so replies (and any other kind) are fetched on
        # their own too and never wait behind them. Bounded: one extra
        # query of the same size.
        seen = {item["id"] for item in due}
        unchecked = [] if self.cold_only else await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            exclude_kind="sequence",
        )
        extra = [item for item in unchecked if item["id"] not in seen]
        if self.precleared is not None:
            # First emails waiting on Precleared keep their place and could
            # fill the page too, hiding follow-ups to people already cleared,
            # which a stored verdict lets go out. Fetched on their own as well.
            seen.update(item["id"] for item in extra)
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=now, limit=budget * 3,
                skip_unready_follow_ups=True, skip_held_campaigns=True,
                follow_ups_only=True,
            )
            extra += [item for item in follow_ups if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        window = send_window_of(self.config)
        # The mailbox read hasn't covered the time since the last complete
        # one (Handler: Harvey was off longer than the provider reads back):
        # an unsubscribe may be unread, so cold email waits. Replies go.
        read_gap = bool(await self.state.get_setting(READ_GAP_KEY))
        held_gap = 0
        sent = 0
        sent_cold = 0
        outside_window = 0
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        held_cold = 0
        # For the Today page and `harvey status`: what held cold email this
        # cycle, how many waited behind it, and whether a fresh call was
        # answered (which clears an earlier hold).
        hold = None
        waiting_on_check = 0
        answered = False
        # Addresses Precleared couldn't check this cycle: their other emails
        # aren't asked about again. (self._failing_domains: failures across
        # several domains pause checks.)
        failed_addresses: set[str] = set()
        check_seconds = 0.0
        for item in due:
            if sent >= budget:
                break
            if self.cold_only and item["kind"] != "sequence":
                continue  # quiet hours: replies wait for the operator's hours
            if item["kind"] == "sequence" and read_gap:
                held_gap += 1
                if item.get("error") != READ_GAP_HELD:
                    await self.state.update_outbox_item(item["id"], error=READ_GAP_HELD)
                continue
            if item["kind"] == "sequence" and sent_cold >= cold_left:
                held_cold += 1
                continue
            needs_check = self.precleared is not None and item["kind"] == "sequence"

            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self._record_block(item, blocked)
                continue
            order = await self._follow_up_order(item, prospect)
            if order == "wait":
                continue
            if order:
                await self.state.update_outbox_item(item["id"], status=order[0], error=order[1])
                continue
            # The recipient's country when Harvey is sure of it, and the
            # company's and person's names: they go with the compliance
            # check, and a cached verdict is keyed on them.
            country = await self._recipient_country(prospect) if needs_check else ""
            names = await self._recipient_names(prospect) if needs_check else ("", "")
            # Cold email waits for the recipient's send window, however
            # late it was approved; replies don't.
            if (window is not None and item["kind"] == "sequence"
                    and await self._outside_window(
                        item, await self._zone_or_default(prospect, given=country, names=names),
                        window)):
                outside_window += 1
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                company, name = names
                # A stored verdict still counts once checks are paused, so
                # people already cleared keep getting their follow-ups; only
                # new addresses wait for the next cycle.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=company, name=name)
                if clearance is None and (compliance_paused
                                          or check_seconds > CHECK_SECONDS_PER_CYCLE):
                    waiting_on_check += 1
                    continue
                address = (item["to_email"] or "").strip().lower()
                if clearance is None and address in failed_addresses:
                    await self._retry_later(item, "the check failed for this address earlier this cycle")
                    continue
                if clearance is None:
                    started = time.monotonic()
                    clearance = await self.precleared.check(
                        item["to_email"], self.state, country, company=company, name=name)
                    check_seconds += time.monotonic() - started
                if clearance.held and not clearance.pause_checks:
                    # Possibly about this address alone: move its email back,
                    # behind other email, and carry on with the rest. Only a
                    # first failure counts towards pausing: an address failing
                    # again says nothing new about Precleared.
                    again = (item.get("error") or "").startswith(RECHECK_PREFIX)
                    failed_addresses.add(address)
                    if not again:
                        self._failing_domains.add(address.rpartition("@")[2])
                    waiting_on_check += 1
                    retry_at = await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: Precleared couldn't check {item['to_email']}, trying "
                        f"again after {retry_at:%Y-%m-%d %H:%M} UTC: {clearance.reason}"
                    )
                    if again or len(self._failing_domains) < DOMAINS_FAILING_TO_PAUSE:
                        continue
                if clearance.held:
                    # About Precleared, not just this address: the others
                    # would likely fail too, so no more fresh checks this cycle.
                    compliance_paused = True
                    hold = clearance
                    if clearance.pause_checks:  # else counted just above
                        waiting_on_check += 1
                        if clearance.retry_address:
                            # It may still be this address: don't let it be
                            # first in line, and asked first, every cycle.
                            await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.cached:
                    answered = True
                    self._failing_domains.clear()
                if not clearance.allowed:
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {clearance.verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared held back email to {item['to_email']}: "
                        f"{clearance.verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id
                # Precleared found where the recipient is, and nothing else
                # had: their own window applies from now on.
                if (window is not None and window.recipient_timezone and clearance.country
                        and not prospect.timezone):
                    zone = await self._zone_or_default(prospect, clearance.country)
                    if zone and await self._outside_window(item, zone, window):
                        outside_window += 1
                        continue

            # Claim it. A reply, bounce, hand send or rejection may have
            # cancelled it since the queue was read; then it stays cancelled.
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            # Send the email as it is now, not as the queue read it: it may
            # have been edited and approved again since.
            item = await self.state.get_outbox_item(item["id"]) or item
            # Whatever landed between the checks above and the claim (a reply
            # sets the status before it cancels) is caught here, with fresh
            # data. From now on nothing else can cancel this email.
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self._record_block(item, blocked)
                continue

            body = self._outgoing_body(item)
            try:
                result = await self.provider.send_email(
                    item["to_email"], item["subject"], body,
                    thread_ref=item.get("thread_ref", ""),
                    in_reply_to=item.get("in_reply_to", ""),
                    **await self._unsubscribe_headers(item),
                )
            except Exception as e:
                # It may have gone out before the error: never retry it.
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"{MAYBE_SENT_PREFIX}send error: {e}"[:300],
                )
                await self._end_after_failed_send(item, maybe_sent=True)
                logger.warning(f"Sender: send to {item['to_email']} raised: {e}")
                continue
            if not result.ok:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=result.error[:300]
                )
                await self._end_after_failed_send(item, maybe_sent=False)
                continue

            sent += 1
            if item["kind"] == "sequence":
                sent_cold += 1
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            # A note that the check moved it back no longer applies.
            # Nor one that it waited for a complete mailbox read.
            # Nor one that its campaign was paused or not started yet.
            note = {"error": ""} if ((item.get("error") or "").startswith(RECHECK_PREFIX)
                                     or item.get("error") in (
                                         READ_GAP_HELD, CAMPAIGN_PAUSED,
                                         CAMPAIGN_NOT_STARTED)) else {}
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body,
                message_id=result.message_id, thread_ref=result.thread_ref, **note,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")
            if item["kind"] == "sequence" and item.get("campaign_id"):
                await self.state.member_sent(item["campaign_id"], item["to_email"], item["step"])

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from harvey.models.conversation import Message
                        convo.thread.append(
                            Message(sender="harvey", content=body)
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            details = {
                "to": item["to_email"], "step": item["step"],
                "kind": item["kind"], "provider": self.provider.name,
            }
            if receipt_id:
                details["precleared_receipt"] = receipt_id
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details=details,
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {self.provider.name}."
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        self.compliance_held = compliance_paused
        if held_gap:
            logger.warning(
                f"Sender: {held_gap} cold email(s) wait until the mailbox is read back to "
                "its last complete read (an unsubscribe may be unread). Replies still go out.")
        if outside_window:
            logger.info(
                f"Sender: {outside_window} cold email(s) wait for the recipient's "
                f"send window ({window.start}-{window.end} their time).")
        if self.precleared is not None:
            if hold is not None:
                # The page read above stops at a few times the cycle's
                # budget; count every cold email still waiting on the check:
                # due with no stored verdict to go on, or moved back by it.
                waiting_on_check = max(waiting_on_check, await self.state.count_due_cold_outbox(
                    now, has_cached_verdict=self._has_cached_verdict,
                    moved_back_prefix=RECHECK_PREFIX))
                await record_hold(self.state, hold, waiting_on_check)
            elif answered:
                await clear_hold(self.state)
        if held_cold:
            warmup = getattr(self.config.channels.email, "warmup", None)
            days = warmup.days_in() if warmup is not None else None
            if days is not None and cold_cap < max_daily:
                logger.info(
                    f"Sender: warmup day {days}: {held_cold} cold email(s) wait; today's "
                    f"cold cap is {cold_cap}. Replies still go out. See `harvey warmup`.")
            else:
                logger.info(f"Sender: cold email cap reached for today ({cold_cap}).")
        if sent_cold:
            await self.state.remember_footer_lines(compliance_footer_lines(self.config))
            notice = compliance_notice(self.config)
            if notice:
                logger.warning(f"Sender: {notice}")
        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{max_daily} today).")
