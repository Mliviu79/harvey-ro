"""The startups module's capabilities. The searches are presets of
harvey/collectors/discover.py; what a site says is read by
harvey/collectors/profile.py with harvey/startups.py."""

from harvey.collectors.discover import preset_capabilities

CAPABILITIES = preset_capabilities("startups", {
    "yc_search": "yc",
    "accelerator_search": "accelerator",
    "funding_search": "funding",
})

from harvey.collectors.profile import READ_BY, site_draft  # noqa: E402
from harvey.modules.api import Capability  # noqa: E402

CODES = {code for code, owner in READ_BY.items() if owner[0] == "startups"}


async def _startup_claims(inp, ctx):
    """Only this module's signals, from the site: a run reads them in the
    website reader's one pass instead."""
    draft = await site_draft(inp, ctx, only=CODES)
    draft.facts = [f for f in draft.facts if f.code in CODES]
    draft.pages, draft.contacts, draft.values = [], [], {}
    return draft


CAPABILITIES["startup_claims"] = Capability("startups", "startup_claims", _startup_claims)
