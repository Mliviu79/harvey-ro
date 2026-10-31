"""The hiring module's capabilities. The search is a preset of
harvey/collectors/discover.py; job boards are read by
harvey/collectors/ats.py."""

from harvey.collectors.discover import preset_capabilities

CAPABILITIES = preset_capabilities("hiring", {"hiring_search": "hiring"})

from harvey.collectors.profile import READ_BY, site_draft  # noqa: E402
from harvey.modules.api import Capability  # noqa: E402

CODES = {code for code, owner in READ_BY.items() if owner[0] == "hiring"}


async def _job_board(inp, ctx):
    """Only this module's signals, from the site and the job board it
    links to: a run reads them in the website reader's one pass instead."""
    draft = await site_draft(inp, ctx, only=CODES)
    draft.facts = [f for f in draft.facts if f.code in CODES]
    draft.pages, draft.contacts, draft.values = [], [], {}
    return draft


CAPABILITIES["job_board"] = Capability("hiring", "job_board", _job_board)

