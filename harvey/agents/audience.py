"""Audience — the helpers a campaign uses to turn a contact into someone to write to.

The contact finder (harvey/finder.py) collects every way to reach a company
and knows nothing about email campaigns. A campaign takes its members from
the contacts (harvey/campaigns.py: add_members); these are its two helpers:
the prospect card made the first time an address joins a campaign (the
person's role is its title; state.company_brief the rest), and the check of
an address nobody verified yet by a verification tier (a credit each,
counted against the month's allowance, email_verify's monthly_searches,
by the campaign cycle).
"""

import logging

from harvey import contacts
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

    async def _check(self, row: dict, enabled) -> str:
        """The address graded by a verification tier, and the grade kept on
        the contact ("guess" when the check failed)."""
        domain = row["email"].rpartition("@")[2]
        try:
            graded = await api.capability(*VERIFY)(
                api.Input(domain=domain, extra={"email": row["email"]}),
                api.Context(env=self.env, enabled=enabled))
            status = graded.contacts[0].status or "guess"
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
