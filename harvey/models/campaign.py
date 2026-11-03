"""Campaign data model."""

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmailStep(BaseModel):
    step: int
    subject: str
    body: str
    delay_days: int = 0  # days after previous step


class Campaign(BaseModel):
    id: str
    name: str = ""
    channel: str = "email"  # Harvey sends email only
    # Set on a campaign an earlier release deployed to Instantly: it goes on
    # there, never through Harvey's own mailbox.
    instantly_campaign_id: str = ""
    sequence: list[EmailStep] = Field(default_factory=list)
    status: str = "draft"  # draft/active/paused/completed
    prospect_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    # 'user': made on the Campaigns page; 'auto': made by the Writer before
    # campaigns were made by hand (kept readable, never made any more).
    origin: str = "auto"
    account_id: int | None = None
    profile_id: str = ""
    offer_json: str = "{}"
    review: str = "inherit"

    def sequence_json(self) -> str:
        return json.dumps([s.model_dump() for s in self.sequence])

    @classmethod
    def sequence_from_json(cls, data: str | None) -> list[EmailStep]:
        """Parse a stored sequence; tolerates NULL/empty/corrupt JSON."""
        if not data:
            return []
        try:
            return [EmailStep(**s) for s in json.loads(data)]
        except (json.JSONDecodeError, TypeError):
            return []
