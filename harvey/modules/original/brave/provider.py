"""Brave Search's web search API, as a website lookup.

GET https://api.search.brave.com/res/v1/web/search with the key in the
``X-Subscription-Token`` header; ``q`` is the search, ``count`` up to 20
results (one page is all a lookup needs). An answer's ``web.results`` each
carry a ``url`` and a ``title``. Every successful search is billed (the
Search plan: $5 per 1,000, with $5 of credit every month); a failed one
isn't. Over the rate limit Brave answers 429 with ``X-RateLimit-Remaining``
and ``X-RateLimit-Reset`` (seconds) per window, the shortest window first:
"1, 15000" is a second's and a month's.

Results are read once, in memory: never stored, cached or logged here."""

from __future__ import annotations

import asyncio

import httpx

from harvey.collectors.discover import (Business, DiscoveryProvider, DiscoveryQuery,
                                        ProviderExhausted, business_name, normalize_domain)
from harvey.transient import Transient

LANE = "brave"
MODULE = "brave"
# Countries Brave refused as a search's ``country`` (HTTP 422) in this
# process: searched without one from then on.
_REFUSED_COUNTRIES: set[str] = set()
# A wait longer than this is a window running out (the month's), not the
# per-second pace: the search stops and says when Brave takes more.
SHORT_WAIT_SECONDS = 5.0


def _numbers(header: str) -> list[float]:
    out = []
    for part in (header or "").split(","):
        try:
            out.append(float(part.strip()))
        except ValueError:
            out.append(-1.0)
    return out


def long_window(headers) -> tuple[float, float | None] | None:
    """(searches left, seconds until it renews) in Brave's longest window
    (the month's, the last of ``X-RateLimit-Remaining``), None when the
    headers don't say."""
    remaining = _numbers(headers.get("X-RateLimit-Remaining", ""))
    if not remaining or remaining[-1] < 0:
        return None
    reset = _numbers(headers.get("X-RateLimit-Reset", ""))
    renews = reset[len(remaining) - 1] if len(reset) >= len(remaining) else None
    return remaining[-1], (renews if renews is not None and renews >= 0 else None)


def retry_after(headers) -> float | None:
    """Seconds until Brave takes another search: the reset of the window
    with nothing left (the longest, when several are), None when the
    headers don't say."""
    remaining = _numbers(headers.get("X-RateLimit-Remaining", ""))
    reset = _numbers(headers.get("X-RateLimit-Reset", ""))
    if not reset:
        return None
    spent = [reset[i] for i, left in enumerate(remaining) if left == 0 and i < len(reset)]
    wait = max(spent) if spent else reset[0]
    return wait if wait >= 0 else None


class Brave(DiscoveryProvider):
    """Brave Search: a company's website by its name and town, for the
    contact finder only (``kind = "lookup"``: never a Discover source or a
    preset's search)."""

    key = "brave"
    label = "Brave Search"
    kind = "lookup"
    rank_codes = ()
    blurb = ("Finds the website of a company already on file, by its name and town. "
             "Website lookups only: Brave's terms forbid building a list of companies "
             "from its results.")
    cost_note = "$5 per 1,000 searches"
    free_tier = "$5 of searches free every month (about 1,000); a card on file is needed"
    signup_url = "https://api-dashboard.search.brave.com/"
    env_keys = ("brave_api_key",)
    caveat = ("Needs BRAVE_API_KEY in .env. Used by the contact finder only, one search "
              "per company with no website; never a Discover source. Its results are "
              "read once and never stored.")

    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
    PRICE = 0.005               # $5 per 1,000 searches
    MAX_COUNT = 20

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        # One search a query: a lookup reads the first page only.
        return round(self.PRICE * len(queries), 6)

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        api_key = (env.get("brave_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("Brave Search needs BRAVE_API_KEY in .env")
        if self.remaining_budget + 1e-9 < self.PRICE:
            self._stopped("budget reached")
            return []
        count = min(self.MAX_COUNT, query.limit or query.depth or self.MAX_COUNT)
        params = {"q": query.keyword(), "count": count}
        country = (query.country or "").strip().upper()
        if len(country) == 2 and country.isalpha() and country not in _REFUSED_COUNTRIES:
            params["country"] = country
        headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
        r = await self._get(client, headers, params)
        self.last_cost += self.PRICE
        items = self._items(r)
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            domain = normalize_domain(url) if isinstance(url, str) else ""
            if not domain:
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            out.append(Business(name=business_name(title, domain), raw_title=title,
                                domain=domain, website=url, location=query.location,
                                has_website=True, source_url=url, provider=self.key,
                                keyword=query.keyword()))
        await self.page_done(len(out))
        return out

    async def _get(self, client: httpx.AsyncClient, headers: dict, params: dict):
        from harvey.modules import lanes

        r = None
        attempt = 0
        while attempt < 2:
            await lanes.wait(LANE)
            try:
                r = await client.get(self.ENDPOINT, headers=headers, params=params)
            except httpx.HTTPError as e:
                if attempt == 0:
                    attempt += 1
                    await asyncio.sleep(1.0)
                    continue
                raise Transient(f"Brave Search unreachable: {e}") from e
            await _note_left(r.headers)
            if r.status_code == 422 and "country" in params and self._refuses_country(r):
                # A country Brave doesn't search in: once more without it
                # (not one of the two tries), never sent again by this process.
                _REFUSED_COUNTRIES.add(params.pop("country"))
                continue
            if r.status_code == 429:
                wait = retry_after(r.headers)
                if attempt == 0 and (wait is None or wait <= SHORT_WAIT_SECONDS):
                    attempt += 1
                    await asyncio.sleep(max(1.0, wait or 1.0))
                    continue
                raise ProviderExhausted(
                    "Brave Search: rate limited, or this month's searches on the plan are "
                    "used up.", retry_after=max(60.0, wait or 60.0))
            if r.status_code >= 500:
                if attempt == 0:
                    attempt += 1
                    await asyncio.sleep(1.0)
                    continue
                raise Transient(f"Brave Search answered HTTP {r.status_code}")
            break
        if r is None:
            raise RuntimeError("Brave Search: request failed")
        code = self._error_code(r) if r.status_code >= 400 else ""
        if r.status_code in (401, 403) or code == "SUBSCRIPTION_TOKEN_INVALID":
            raise ProviderExhausted(
                f"Brave Search refused the key (HTTP {r.status_code}). Check BRAVE_API_KEY "
                "in .env, or get one at https://api-dashboard.search.brave.com/")
        if r.status_code == 402:
            raise ProviderExhausted(
                "Brave Search refused the search (HTTP 402): the plan has no credit left "
                "or doesn't include web search.")
        if r.status_code >= 400:
            raise RuntimeError(f"Brave Search refused the search (HTTP {r.status_code}"
                               + (f", {code}" if code else "") + ")")
        return r

    @staticmethod
    def _refuses_country(r) -> bool:
        """A 422 that says the ``country`` parameter is what Brave refused
        (its validation errors name the parameter in ``loc``)."""
        try:
            err = (r.json() or {}).get("error") or {}
            errors = ((err.get("meta") or {}).get("errors")) or []
        except Exception:
            return False
        return any(isinstance(e, dict) and "country" in (e.get("loc") or ())
                   for e in errors if isinstance(errors, list))

    @staticmethod
    def _error_code(r) -> str:
        try:
            err = (r.json() or {}).get("error") or {}
        except Exception:
            return ""
        return str(err.get("code") or "") if isinstance(err, dict) else ""

    @staticmethod
    def _items(r) -> list:
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError("Brave Search sent an unexpected answer (not JSON)") from e
        web = data.get("web") if isinstance(data, dict) else None
        items = web.get("results") if isinstance(web, dict) else None
        return items if isinstance(items, list) else []


async def _note_left(headers) -> None:
    """What Brave says is left in its longest window, for the background's
    share (lanes.report_left): its own count can be lower than Harvey's (a
    key used elsewhere too). Never fails a search."""
    from harvey.modules import lanes

    try:
        left = long_window(headers)
        if left is not None:
            await asyncio.to_thread(lanes.report_left, MODULE, *left)
    except Exception:
        pass
