"""Search engines' and directories' result pages, read without an API.

DuckDuckGo, Bing and Google answer a query with an HTML page of results;
a directory (G2, Clutch, a "company list") links the companies it lists.
Both are read here into (url, snippet) results or company links. Their
terms forbid automated access, so the web_search_scrape module that runs
this ships off. Fetching is the caller's (``fetch(url, **kw)`` returns a
response with ``.text``, or None): Scout's, with its retries, back-off and
bot-challenge tracking.
"""

import logging
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("harvey.scout")

MAX_RESULTS_PER_QUERY = 20
SCRAPE_TIMEOUT = 12.0


def _soup(text: str) -> BeautifulSoup | None:
    """Parse HTML without ever raising."""
    try:
        return BeautifulSoup(text, "html.parser")
    except Exception as e:
        logger.debug(f"HTML parse failed: {e}")
        return None


def _attr(value) -> str:
    """BS4 attributes can be a str or a list; coerce to a clean string."""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value) if value else ""


async def duckduckgo(fetch, query: str) -> list[tuple[str, str]]:
    """DuckDuckGo's HTML results: friendly to scraping, the first tried."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    resp = await fetch(url)
    if resp is None:
        return []

    soup = _soup(resp.text)
    if soup is None:
        return []

    results = []
    for result in soup.select(".result"):
        link = result.select_one(".result__a")
        snippet_el = result.select_one(".result__snippet")
        if not (link and link.get("href")):
            continue
        href = _attr(link.get("href"))
        # DDG wraps URLs in a redirect — extract the real URL.
        if "uddg=" in href:
            try:
                params = parse_qs(urlparse(href).query)
                real = params.get("uddg", [])
                if real:
                    href = real[0]
            except Exception:
                pass
        snippet = snippet_el.get_text().strip() if snippet_el else ""
        if href.startswith("http"):
            results.append((href, snippet))

    if results:
        logger.debug(f"DuckDuckGo returned {len(results)} results")
    return results[:MAX_RESULTS_PER_QUERY]


async def bing(fetch, query: str) -> list[tuple[str, str]]:
    """Bing's HTML results — less aggressive than Google."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count={MAX_RESULTS_PER_QUERY}"
    resp = await fetch(url)
    if resp is None:
        return []

    soup = _soup(resp.text)
    if soup is None:
        return []

    results = []
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a")
        snippet_el = item.select_one(".b_caption p")
        if not (link and link.get("href")):
            continue
        href = _attr(link.get("href"))
        snippet = snippet_el.get_text().strip() if snippet_el else ""
        if href.startswith("http"):
            results.append((href, snippet))

    if results:
        logger.debug(f"Bing returned {len(results)} results")
    return results[:MAX_RESULTS_PER_QUERY]


async def google(fetch, query: str) -> list[tuple[str, str]]:
    """Google's HTML results — aggressive rate limiting, last resort."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&num={MAX_RESULTS_PER_QUERY}"
    # Google throttles hard; a single quick try avoids burning the cycle.
    resp = await fetch(url, retries=1)
    if resp is None:
        return []

    soup = _soup(resp.text)
    if soup is None:
        return []

    results = []
    for div in soup.select("div.g"):
        link = div.select_one("a")
        snippet_el = div.select_one("div.VwiC3b")
        if not (link and link.get("href")):
            continue
        href = _attr(link.get("href"))
        snippet = snippet_el.get_text() if snippet_el else ""
        if href.startswith("http"):
            results.append((href, snippet))

    if results:
        logger.debug(f"Google returned {len(results)} results")
    return results[:MAX_RESULTS_PER_QUERY]


ENGINES = (duckduckgo, bing, google)


async def directory_page(fetch, url: str, domain_of, noise) -> list[dict]:
    """The companies a directory or list page links to: link text as the
    name, at most ten, none of its own links or ``noise(domain)`` ones."""
    companies = []
    resp = await fetch(url, timeout=SCRAPE_TIMEOUT, retries=2)
    if resp is None:
        return []

    soup = _soup(resp.text)
    if soup is None:
        return []

    page_domain = domain_of(url)
    seen_here = set()

    try:
        for link in soup.find_all("a", href=True):
            href = _attr(link.get("href"))
            if not href.startswith("http"):
                continue

            domain = domain_of(href)
            if not domain or noise(domain):
                continue
            if domain == page_domain or domain in seen_here:
                continue

            link_text = link.get_text().strip()
            if 3 < len(link_text) < 60:
                seen_here.add(domain)
                companies.append({
                    "name": link_text,
                    "domain": domain,
                    "website": f"https://{domain}",
                    "source": "directory",
                    "source_url": url,
                })

            if len(companies) >= 10:
                break
    except Exception as e:
        logger.debug(f"Failed to parse directory page {url}: {e}")

    return companies
