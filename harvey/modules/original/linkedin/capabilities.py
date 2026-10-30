"""LinkedIn's capability. The code it runs is
harvey/integrations/linkedin.py; ``ctx.browser`` is the caller's logged-in
LinkedInAutomation session."""

from harvey.modules.api import Capability, Draft, Person


async def _people_search(inp, ctx) -> Draft:
    """People whose profile matches ``inp.query`` (at most
    ``extra["max_results"]``), each profile as LinkedIn shows it in
    ``fields``."""
    profiles = await ctx.browser.search_people(keywords=inp.query,
                                               max_results=inp.extra.get("max_results", 10))
    return Draft(people=[Person(
        first_name=(p.get("first_name") or "").strip(), last_name=(p.get("last_name") or "").strip(),
        title=(p.get("title") or "").strip(), source_url=(p.get("linkedin_url") or "").strip(),
        fields=dict(p)) for p in profiles or [] if isinstance(p, dict)])


CAPABILITIES = {"people_search": Capability("linkedin", "people_search", _people_search)}
