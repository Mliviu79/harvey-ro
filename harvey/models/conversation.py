"""Conversation data model."""

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Message(BaseModel):
    sender: str  # "harvey" or "prospect"
    content: str
    timestamp: datetime = Field(default_factory=_utcnow)
    # The channel it came on when not the conversation's own (an SMS in an
    # email thread): "", or sms / waba / wa.
    channel: str = ""


# Sales stages: tracks where the deal is in the pipeline
STAGES = [
    "initial_outreach",  # first emails sent, no reply yet
    "engaged",           # prospect replied positively
    "qualifying",        # assessing fit (budget, authority, need, timing)
    "presenting",        # sharing product details, case studies
    "negotiating",       # discussing terms, pricing, objections
    "closing",           # moving toward a meeting or deal
    "closed_won",        # meeting booked or deal closed
    "closed_lost",       # prospect said no or went dark
]


class Conversation(BaseModel):
    id: str
    prospect_id: str
    campaign_id: str = ""
    channel: str = "email"
    thread: list[Message] = Field(default_factory=list)
    intent: str = ""  # interested/objection/not_interested/ooo/wrong_person
    stage: str = "initial_outreach"  # sales pipeline stage
    status: str = "open"  # open/replied/meeting_booked/closed
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def thread_json(self) -> str:
        # (An email message keeps the shape it always had: no channel key.)
        return json.dumps([{k: v for k, v in m.model_dump(mode="json").items()
                            if k != "channel" or v} for m in self.thread])

    @classmethod
    def thread_from_json(cls, data: str | None) -> list[Message]:
        """Parse a stored thread; tolerates NULL/empty/corrupt JSON."""
        if not data:
            return []
        try:
            return [Message(**m) for m in json.loads(data)]
        except (json.JSONDecodeError, TypeError):
            return []
