"""The website reader's capability. The code it runs is
harvey/collectors/profile.py."""

from harvey.collectors.profile import site_draft
from harvey.modules.api import Capability


async def _read_site(inp, ctx):
    """One pass over the company's own site: its pages, and every
    confirmed signal they answer, each stamped with the module it belongs
    to (one fetch serves them all)."""
    return await site_draft(inp, ctx)


CAPABILITIES = {"read_site": Capability("website", "read_site", _read_site)}
