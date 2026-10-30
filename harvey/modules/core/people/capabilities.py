"""The people module's capability. The code it runs is
harvey/collectors/people.py."""

from harvey.collectors import people as people_mod
from harvey.collectors.discover import site_name_from_html
from harvey.modules.api import Capability, Draft, Person


async def _find_people(inp, ctx) -> Draft:
    """The people a company's own pages (``inp.pages``, the homepage first)
    name, likeliest decision maker first: cards and lists, schema.org, and
    sentences ("owned and operated by..."). Each person's ``fields`` keep
    what the finder knows: owner, rank, where it was read and the evidence."""
    pages = [(p.url, p.html) for p in inp.pages]
    if not pages:
        return Draft()
    site_name = site_name_from_html(pages[0][1], inp.domain)
    found = people_mod.find_people(pages, inp.domain, site_name)
    return Draft(people=[Person(
        first_name=m["first_name"], last_name=m["last_name"], title=m.get("title", ""),
        source_url=m.get("evidence_url", ""), fields=dict(m)) for m in found])


CAPABILITIES = {"find_people": Capability("people", "find_people", _find_people)}
