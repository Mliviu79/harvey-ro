"""The campaign cycle: each heartbeat's work for the campaigns the user made.

After the heartbeat has read replies and drained the outbox, every active
campaign, in turn from where the last cycle stopped:

  1. rolls its audience, when it is rolling and an hour has passed
     (campaigns.roll: the frozen filter, every rule of add_members);
  2. has its waiting members checked by a verification tier, oldest first
     across campaigns, CHECKS_PER_CYCLE in all, inside email_verify's
     monthly allowance (and, for addresses a roll added, ROLL_CHECKS_PER_DAY
     a campaign);
  3. gets a grounded first email drafted for ready members that can be
     sent to (Sender.stageable checks them first, so no model call is
     spent on an address that would be refused): WRITE_PER_CYCLE in all,
     and only while the first emails queued and not yet sent are fewer
     than QUEUE_DAYS of today's cold cap, so drafts don't run ahead of
     sending (each is a model call) or go stale in the queue;
  4. has those members staged into the outbox, for review (autopilot
     approves at send time).

A campaign that doesn't roll finishes once no member is open.
"""

from __future__ import annotations

import logging

from harvey import campaigns
from harvey.agents.audience import VERIFY, Audience
from harvey.agents.sender import SENDABLE_EMAIL_STATUSES, SENDABLE_EMAIL_STATUSES_WITH_RISKY
from harvey.modules import ingest

logger = logging.getLogger("harvey.campaigner")

CHECKS_PER_CYCLE = 20
WRITE_PER_CYCLE = 8
QUEUE_DAYS = 2
ROUND_ROBIN_KEY = "campaign_cycle_next"


class CampaignCycle:
    def __init__(self, state, config, env, writer, sender):
        self.state = state
        self.config = config
        self.writer = writer
        self.sender = sender
        self.audience = Audience(state, env)

    async def run(self) -> bool:
        """One cycle over the active campaigns made by the user. True when
        anything was done (rolled, checked, written or staged)."""
        await self.state.sync_sending_account(self.config)
        active = [c for c in await self.state.campaigns_call(
            campaigns.list_campaigns, ["active"], write=False) if c["origin"] == "user"]
        if not active:
            return False
        active.sort(key=lambda c: c["created_at"] or "")
        start = await self.state.get_setting(ROUND_ROBIN_KEY)
        ids = [c["id"] for c in active]
        if start in ids:
            i = ids.index(start)
            active = active[i:] + active[:i]
        did = False
        for c in active:
            try:
                rolled = await campaigns.roll(self.state, self.config, c)
                did = did or bool(rolled and rolled["added"])
            except Exception as e:
                logger.warning(f"Campaigns: rolling '{c['name']}' failed: {e}")
        did = await self._verify(active) or did
        room = await self._write_room()
        for c in active:
            try:
                if room > 0:
                    staged = await self._write_and_stage(c, room)
                    room -= staged
                    did = did or staged > 0
            except Exception as e:
                logger.error(f"Campaigns: '{c['name']}' failed this cycle: {e}")
            await self.state.campaigns_call(campaigns.finish_if_done, c["id"])
        if len(active) > 1:
            await self.state.set_setting(ROUND_ROBIN_KEY, active[1]["id"])
        return did

    async def _verify(self, active: list[dict]) -> bool:
        """Check waiting members' addresses, CHECKS_PER_CYCLE in all."""
        from harvey.modules import switches

        enabled = frozenset(await switches.enabled_ids(self.state))
        if VERIFY[0] not in enabled:
            return False
        allowance = ingest.Allowance(self.state)
        allow_risky = bool(getattr(self.config.channels.email, "send_to_risky", False))
        checks = 0
        for c in active:
            roll_left = await campaigns.roll_checks_left(self.state, c["id"])
            for m in await self.state.campaign_members(c["id"], status="waiting"):
                if checks >= CHECKS_PER_CYCLE or await allowance.left(VERIFY[0]) <= 0:
                    return checks > 0
                rolled = m["added_by"] == campaigns.MEMBER_ADDED_BY["rolling"]
                if rolled and roll_left <= 0:
                    continue
                rows = await self.state.read("audience_rows", None, [m["email"]])
                if not rows or rows[0]["trust"] not in ("confirmed", "likely"):
                    await self.state.set_campaign_member(c["id"], m["email"], "skipped",
                                                         "no trusted link any more")
                    continue
                checks += 1
                await allowance.take(VERIFY[0])
                if rolled:
                    roll_left -= 1
                    await campaigns.count_roll_check(self.state, c["id"])
                status = await self.audience._check(rows[0], enabled)
                if m["prospect_id"]:
                    await self.state.update_prospect_email(m["prospect_id"], m["email"], status)
                if status == "verified" or (status == "risky" and allow_risky):
                    await self.state.set_campaign_member(c["id"], m["email"], "ready")
                else:
                    await self.state.set_campaign_member(c["id"], m["email"], "skipped",
                                                         f"email status '{status}'")
        return checks > 0

    async def _write_room(self) -> int:
        """How many first emails this cycle may write: WRITE_PER_CYCLE, and
        no more than QUEUE_DAYS of today's cold cap minus what is queued."""
        from harvey.config import cold_daily_cap

        cap = cold_daily_cap(self.config.channels.email) * QUEUE_DAYS
        return max(min(WRITE_PER_CYCLE, cap - await self.state.count_queued_first_emails()), 0)

    async def _write_and_stage(self, c: dict, room: int) -> int:
        """Draft and stage up to ``room`` ready members that can be sent to;
        members that can't be are moved on with the reason. Returns how
        many members were staged."""
        campaign = next((x for x in await self.state.get_campaigns_by_status("active")
                         if x.id == c["id"]), None)
        if campaign is None or not campaign.sequence:
            return 0
        allow_risky = bool(getattr(self.config.channels.email, "send_to_risky", False))
        sendable = SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky else SENDABLE_EMAIL_STATUSES
        picked = []
        for m in await self.state.campaign_members(c["id"], status="ready", limit=200):
            if len(picked) >= room:
                break
            prospect = await self.state.get_prospect(m["prospect_id"]) if m["prospect_id"] \
                else None
            why = await self.sender.stageable(c["id"], m, prospect, sendable)
            if why is not None:
                await self.state.set_campaign_member(c["id"], m["email"], why[0], why[1])
                continue
            picked.append(prospect)
        if not picked:
            return 0
        await self.writer.write_for_members(campaign, picked)
        await self.sender._stage_campaign_native(campaign, emails=[p.email for p in picked])
        return len(picked)
