"""The contact finder's capability: a company's name and town searched
through one search module, the results handed back as candidates for its
website. Core (harvey/finder.py) picks one, reads it and decides how sure
it is; nothing here keeps a result."""

from harvey.collectors.discover import PROVIDERS, DiscoveryQuery, search_draft
from harvey.modules.api import Capability, Draft

# One page is enough to find a company by its own name.
LOOKUP_DEPTH = 10


def lookup_query(inp) -> DiscoveryQuery:
    """The search a website lookup makes: the company's name in its town,
    one page, in its country when known (``extra["country"]``)."""
    return DiscoveryQuery(term=inp.name, location=inp.locality, depth=LOOKUP_DEPTH,
                          limit=LOOKUP_DEPTH, coordinate=inp.point,
                          country=str(inp.extra.get("country") or ""))


async def _find_website(inp, ctx) -> Draft:
    key = ctx.settings["provider"]
    draft = await search_draft(key, lookup_query(inp), ctx.http, ctx.env,
                               remaining=ctx.budget.remaining)
    # The search module found these: it is the source, and its terms apply.
    draft.stamp(*ctx.settings["searcher"])
    draft.facts = []
    return draft


def _estimate(inp) -> float:
    provider = PROVIDERS.get(inp.extra.get("provider", ""))
    return provider.estimate([lookup_query(inp)]) if provider is not None else 0.0


CAPABILITIES = {"find_website": Capability("contact_finder", "find_website", _find_website,
                                           estimate=_estimate)}
