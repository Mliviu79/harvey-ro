"""The website marketing module's capability. The code it runs is
harvey/collectors/profile.py (its detectors, with
harvey/integrations/tech_detect.py's)."""

from harvey.collectors.profile import READ_BY, site_draft
from harvey.modules.api import Capability

CODES = {code for code, owner in READ_BY.items() if owner[0] == "web_marketing"}


async def _marketing_profile(inp, ctx):
    """Only this module's signals, from the site: a run reads them in the
    website reader's one pass instead."""
    draft = await site_draft(inp, ctx, only=CODES)
    draft.facts = [f for f in draft.facts if f.code in CODES]
    draft.pages, draft.contacts, draft.values = [], [], {}
    return draft


CAPABILITIES = {"marketing_profile": Capability("web_marketing", "marketing_profile",
                                                _marketing_profile)}
