"""Hunter's capability. The code it runs is
harvey/integrations/email_finder.py."""

from harvey.integrations import email_finder as ef
from harvey.modules.api import Capability, Draft, Person


async def _domain_search(inp, ctx) -> Draft:
    """The people Hunter has on file for ``inp.domain`` (name, title,
    address, its own verdict, in ``fields``). One of the month's domain
    searches is drawn first: none left stops the run (``stop``)."""
    if await ctx.budget.left("hunter") <= 0:
        return Draft(stop="Hunter's domain searches for this month are used up")
    await ctx.budget.take("hunter")
    key = ef._hunter_api_key(ctx.env)
    found = await ef.hunter_domain_people(inp.domain, key)
    return Draft(people=[Person(
        first_name=p.get("first_name") or "", last_name=p.get("last_name") or "",
        title=p.get("position") or "", email=p.get("value") or "", fields=dict(p))
        for p in found or [] if isinstance(p, dict)])


CAPABILITIES = {"domain_search": Capability("hunter", "domain_search", _domain_search)}
