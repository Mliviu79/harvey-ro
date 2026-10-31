"""Audience — the Email module's people to write to, picked from the contacts.

The contact finder (harvey/finder.py) collects every way to reach a company
and knows nothing about email campaigns. This is the email campaign's side:
each cycle it takes the email contacts it could write to (not
do-not-contact, reaching a company with trust confirmed or likely) that
have no prospect yet, and makes each a prospect for the Writer, its notes
what the company's record says that makes an email specific (the person's
role is its title; state.company_brief the rest). An address
a site published and nobody checked yet is checked first (a
verification credit each, counted against the
month's allowance, email_verify's monthly_searches); one that comes back a
guess or invalid stays a contact and never becomes a prospect.
"""

import logging

from harvey import contacts
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.modules import api, ingest
from harvey.state import WRITABLE_EMAIL_STATUSES, StateManager, brief_note

logger = logging.getLogger("harvey.audience")

# Contacts taken per cycle, and how many of them may be checked first.
AUDIENCE_PER_CYCLE = 200
CHECKS_PER_CYCLE = 20
VERIFY = ("email_verify", "verify_address")


class Audience:
    def __init__(self, state: StateManager, env=None):
        self.state = state
        # The .env settings (EnvConfig), or already a dict.
        self.env = (env.model_dump() if hasattr(env, "model_dump")
                    else env if isinstance(env, dict) else {})

    async def run(self) -> int:
        """Make prospects of the email contacts the Writer can draft for.
        Returns how many were made."""
        from harvey.modules import switches

        enabled = frozenset(await switches.enabled_ids(self.state))
        allowance = ingest.Allowance(self.state)
        checks = 0
        made = 0
        for row in await self.state.email_audience(AUDIENCE_PER_CYCLE):
            status = row["status"]
            if status == "unchecked":
                if (VERIFY[0] not in enabled or checks >= CHECKS_PER_CYCLE
                        or await allowance.left(VERIFY[0]) <= 0):
                    continue
                checks += 1
                await allowance.take(VERIFY[0])
                status = await self._check(row, enabled)
                if status not in WRITABLE_EMAIL_STATUSES:
                    continue
            prospect = self._prospect(row, status)
            prospect.personalization_notes = await self._notes(row["company_id"])
            await ingest.prospect(self.state, prospect)
            made += 1
        if made:
            logger.info(f"Audience: {made} email contact(s) ready for the Writer.")
        return made

    async def _notes(self, company_id: str) -> str:
        """What the company's record says that makes an email specific (its
        town, what it does, the tools on its site, the roles it is hiring
        for...: state.company_brief), as the prospect's notes, for the
        Writer and the {{personalization}} merge field."""
        try:
            return brief_note(await self.state.company_brief(company_id))
        except Exception as e:
            logger.debug(f"Audience: no notes for company {company_id}: {e}")
            return ""

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
