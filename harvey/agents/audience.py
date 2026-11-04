"""Audience — the helpers a campaign uses to turn a contact into someone to write to.

The contact finder (harvey/finder.py) collects every way to reach a company
and knows nothing about email campaigns. A campaign takes its members from
the contacts (harvey/campaigns.py: add_members); these are its two helpers:
the prospect card made the first time an address joins a campaign (the
person's role is its title; state.company_brief the rest), and the check of
an address nobody verified yet by a verification tier (each service it
asks counted and priced as it is asked, harvey/verify_costs.py, against
the month's checks, email_verify's monthly_searches, and the install's
day and month spend limits, verify_costs.InstallSpend, as the contact
finder's are: a priced request is reserved from them and recorded, and
with a limit set one past a free tier with no price is refused and the
address waits; with no limit set it is made and counted unpriced).
"""

import logging

from harvey import contacts, verify_costs
from harvey.integrations import email_finder
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.modules import api
from harvey.state import StateManager

logger = logging.getLogger("harvey.audience")

VERIFY = ("email_verify", "verify_address")


class Audience:
    def __init__(self, state: StateManager, env=None):
        self.state = state
        # The .env settings (EnvConfig), or already a dict.
        self.env = (env.model_dump() if hasattr(env, "model_dump")
                    else env if isinstance(env, dict) else {})
        # The install's spend limits (verify_costs.InstallSpend, set by the
        # campaign cycle); None: no USD limit.
        self.spend = None
        # Why the last _check made no check (it returned ""): {"code",
        # "why", "services"}, for campaigns.hold_checks.
        self.wait: dict | None = None

    async def blocked(self, services) -> str:
        """Why none of ``services`` could be asked now, or "" (read only)."""
        meter = await verify_costs.Meter.start(self.state, self.spend)
        whys = [await meter.blocked(v) for v in services]
        return whys[0] if whys and all(whys) else ""

    def configured(self, enabled) -> list[str]:
        return email_finder.configured_verifiers(self.env, enabled)

    async def still_waits(self, wait: dict, enabled) -> bool:
        """Whether what made the checks wait (``wait``, from _check) holds
        still: the same services configured, and those it refused refused
        again (read only)."""
        return (wait.get("configured") == self.configured(enabled)
                and bool(await self.blocked(wait.get("services") or [])))

    def _waits(self, why: str, services, configured) -> str:
        self.wait = {"code": verify_costs.hold_code(why), "why": why,
                     "services": list(dict.fromkeys(services)), "configured": configured}
        logger.info(f"Audience: address checks wait: {why}")
        return ""

    async def _check(self, row: dict, enabled) -> str:
        """The address graded by a verification tier, and the grade kept on
        the contact ("guess" when the check failed). "" when no check was
        made and the address waits (``wait`` says why): every service it
        may ask is refused (past a free tier with no price under a spend
        limit, or the day's or month's limit reached), or one it needed was
        refused and nothing else answered. Nothing is kept on the contact
        then."""
        domain = row["email"].rpartition("@")[2]
        self.wait = None
        try:
            # (Each service asked uses its free allowance: counted, whoever asks.)
            meter = await verify_costs.Meter.start(self.state, self.spend)
            configured = self.configured(enabled)
            why = await self.blocked(configured)
            if why:
                return self._waits(why, configured, configured)
            mark = meter.mark()
            graded = await api.capability(*VERIFY)(
                api.Input(domain=domain, extra={"email": row["email"]}),
                api.Context(env=self.env, enabled=enabled, budget=meter))
            status = graded.contacts[0].status or "guess"
            stopped = [(v, why) for v, why in meter.refused[mark:]
                       if meter.specs.get(v, {}).get("kind") != "search"]
            if stopped and status == "guess":
                # A service the check would have asked was refused: it
                # isn't a check done, and its guess isn't kept.
                return self._waits(stopped[0][1], [v for v, _ in stopped], configured)
            if meter.total()[2]:
                logger.warning(f"Audience: checking {row['email']} asked a service past its "
                               "free tier with no price entered (Settings: Email "
                               "verification): counted, not priced (no spend limit is set)")
        except Exception as e:
            logger.debug(f"Audience: checking {row['email']} failed: {e}")
            status = "guess"
        await self.state.add_contacts(row["company_id"], [contacts.Found(
            "email", row["email"], status=status, source=row["source"],
            evidence_url=row["evidence_url"], trust=row["trust"])])
        return status

    @staticmethod
    def _prospect(row: dict, status: str) -> Prospect:
        first, last = contacts.split_name(row["person"], row["person_source"])
        inbox = not (first and last)
        return Prospect(
            company_id=row["company_id"], first_name="" if inbox else first,
            last_name="" if inbox else last, email=row["email"], email_status=status,
            email_verified=status == "verified",
            title=row["role"] or ("General inbox" if inbox else ""),
            source=COMPANY_INBOX_SOURCE if inbox else (row["source"] or "contacts"),
            source_url=row["evidence_url"], company=row["company"] or "",
            industry=row["industry"])
