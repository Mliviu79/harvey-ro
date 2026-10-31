"""The keyless web search module's capabilities. The code they run is
harvey/collectors/web_search.py; ``ctx.http`` is the caller's fetch,
``fetch(url, **kw)`` returning a response or None."""

from harvey.collectors import web_search
from harvey.modules.api import Capability, Draft, Record


async def _html_search(inp, ctx) -> Draft:
    """``inp.query`` on DuckDuckGo, then Bing, then Google: the first that
    answers. Each result is a link record (its URL, and its snippet as the
    description), in the engine's order."""
    for engine in web_search.ENGINES:
        try:
            results = await engine(ctx.http, inp.query)
        except Exception as e:
            web_search.logger.debug(f"Search backend raised: {e}")
            results = []
        if results:
            return Draft(records=[Record(key=str(i), source_url=url, description=snippet)
                                  for i, (url, snippet) in enumerate(results)])
    return Draft()


async def _directories(inp, ctx) -> Draft:
    """The companies a directory page (``extra["url"]``) links to, with
    ``extra["domain_of"]`` and ``extra["noise"]`` deciding what a link's
    domain is and which aren't companies."""
    x = inp.extra
    found = await web_search.directory_page(ctx.http, x["url"], x["domain_of"], x["noise"])
    return Draft(records=[Record(key=str(i), name=c["name"], domain=c["domain"],
                                 website=c["website"], source=c["source"],
                                 source_url=c["source_url"]) for i, c in enumerate(found)])


CAPABILITIES = {
    "html_search": Capability("web_search_scrape", "html_search", _html_search),
    "directories": Capability("web_search_scrape", "directories", _directories),
}
