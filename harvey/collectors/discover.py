"""DISCOVER — who exists, and how visible they are.

The only stage that costs money, so it is the one with a provider menu, a
mandatory cost estimate, and a budget check inside the loop.

Two shapes of discovery, and the difference matters:

* **Listings** ask "which businesses of this type are in this place?" and come
  back with a phone, a category, a rating and — crucially — whether there is a
  website at all. For local SMB verticals this is the primary path.
* **SERP** asks "who ranks for this search?" and comes back with a rank
  position. You are buying the *signal*, not the list: positions 11-30 are
  businesses visible enough to be trying and not winning, which is the most
  useful prospecting signal there is.

Cost discipline lives in the run (harvey/modules/runs.py), not the database.
A table can only record spend after the fact; only the run can decline to
spend it. Every run estimates first, checks a budget between batches, reads
the kill switch, and flushes observations per batch — a long run that dies
must not lose what it already learned. This file is the search modules'
code: each provider answers one search as a Draft, and writes nothing.
"""

import asyncio
import time
import base64
import logging
import math
import re
import unicodedata
from collections.abc import MutableMapping
from html import unescape
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx

from harvey.modules.api import Draft, Fact, Record
from harvey.transient import Transient

logger = logging.getLogger("harvey.collectors.discover")

TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)


# ── Junk filtering ────────────────────────────────────────────────────
# Discovery fills a database with directories, hospital systems, media and
# lead-gen middlemen unless you filter hard. Match PATTERNS, not a literal
# list: a literal list is exactly how `whatclinic.com` and `ufhealth.org` get
# through on the first run.

# Anchored on a dot boundary, not the string start: SERP results arrive as
# `en.wikipedia.org` and `m.facebook.com` at least as often as bare domains,
# and `^(www\.)?` catches neither.
JUNK_DOMAIN_PATTERNS = tuple(re.compile(p, re.I) for p in (
    # directories & marketplaces
    r"(^|\.)(yelp|yellowpages|superpages|manta|bbb|angi|angieslist|homeadvisor)\.",
    r"(^|\.)(thumbtack|houzz|porch|buildzoom|networx|modernize|nextdoor)\.",
    r"(^|\.)(facebook|instagram|twitter|x|linkedin|youtube|tiktok|pinterest)\.",
    r"(^|\.)(meetup|eventbrite)\.",
    r"(^|\.)(indeed|glassdoor|ziprecruiter|craigslist|zillow|realtor)\.",
    r"(^|\.)(mapquest|foursquare|tripadvisor|trustpilot|birdeye|expertise)\.",
    r"(directory|listings?|find-?a-|top\d+|best-?of)\.",
    # Job boards, directories, and marketplaces.
    r"(^|\.)(naukri|jobrapido|jobsora|empleos|shine|foundit|timesjobs|freelancer|upwork)\.",
    # Indian directories & marketplaces
    r"(^|\.)(indiamart|justdial|tradeindia|exportersindia|sulekha|urbanpro|yet5)\.",
    r"(^|\.)(olx|quikr|flipkart|snapdeal|myntra|shopclues|etsy|ebay|alibaba|aliexpress)\.",
    # hobby communities & model repositories — never a prospect
    r"(^|\.)(boardgamegeek|team-bhp|hackaday|instructables|thingiverse|printables|cults3d|thangs|grabcad)\.",
    # media & reference
    r"(^|\.)(wikipedia|reddit|quora|medium|substack|forbes|inc|entrepreneur)\.",
    r"(news|magazine|journal|gazette|tribune|herald)\.(com|org|net)$",
    # institutional
    r"\.(edu|gov|mil)$",
    r"(hospital|healthsystem|medicalcenter)\.",
    r"health\.org$",
    # startup databases, launch sites and tech press (the YC preset's noise)
    r"(^|\.)(ycombinator|workatastartup|startupschool|yclist|ycdb|seedtable)\.",
    r"(^|\.)(crunchbase|techcrunch|wellfound|angel|tracxn|pitchbook)\.",
    r"(^|\.)(cbinsights|producthunt|f6s|dealroom|growjo|owler|zoominfo)\.",
    r"(^|\.)github\.com$",
    # hosted job boards: the company is whoever's board it is, not the host
    r"(^|\.)(job-)?boards\.(eu\.)?greenhouse\.io$",
    r"(^|\.)jobs\.(eu\.)?lever\.co$",
    r"(^|\.)jobs\.ashbyhq\.com$",
    r"(^|\.)(apply|jobs)\.workable\.com$",
    r"(^|\.)(jobs|careers)\.smartrecruiters\.com$",
    r"(^|\.)jobs\.jobvite\.com$",
    r"(^|\.)ats\.rippling\.com$",
    r"(^|\.)(myworkdayjobs|applytojob)\.com$",
    # a company's board on these lives on a subdomain; the bare domain is
    # the vendor, which may be someone's prospect
    r"\.(icims|bamboohr|recruitee|teamtailor)\.com$",
    r"\.breezy\.hr$",
    r"\.jobs\.personio\.(de|com)$",
    # job sites named for the job: jobs.com, careers.co.uk
    r"^(jobs|careers)\.((co|com|org|net|ac)\.)?[a-z]{2,}$",
    # accelerators' own sites (the accelerator preset's noise)
    r"(^|\.)(techstars|antler|joinef|southparkcommons|aigrant|a16z|pioneer|plugandplaytechcenter|plugandplay)\.",
    r"(^|\.)(500\.co|neo\.com)$",
    # funding news, wires and round trackers (the funding preset's noise)
    r"(^|\.)(prnewswire|businesswire|globenewswire|accesswire|einpresswire|newswire|finsmes|axios)\.",
    r"(^|\.)(venturebeat|siliconangle|eu-startups|sifted|geekwire|thesaasnews|fortune|bloomberg)\.",
    r"(^|\.)(reuters|wsj|cnbc|businessinsider|privco|recentfunding|pulse2|startupfundraising)\.",
    r"(^|\.)(startupintros|openvc|siliconcanals|startupdaily|e27|similarweb|vcbacked|roundos)\.",
    r"(^|\.)(techfundingnews|startups\.gallery)",
    r"(^|\.)tech\.eu$",
    # infrastructure that is never a prospect
    r"(^|\.)(google|bing|yahoo|duckduckgo|amazon|apple|microsoft)\.",
    r"(wordpress|wix|squarespace|shopify|godaddy|weebly|webflow)\.com$",
))

# Names that mean the row is a category page or an aggregator, not a business.
JUNK_NAME_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"^\s*(top|best|\d+)\s+\d*\s*(best\s+)?\w+.*\b(in|near|of)\b",
    r"\b(directory|listings?|reviews? of|compare|find a)\b",
    # "Ohio HOA Management Company List", "List of Plumbers in Austin"
    r"\b(compan(y|ies)|firms?|contractors?|businesses|providers?)\s+list\b",
    r"\blist of\b",
))


def normalize_domain(url_or_domain: str) -> str:
    """A bare, lowercase, www-less hostname — the entity-resolution key.

    Entity resolution decides whether any of this is real. Get it wrong and
    every trend built on top is garbage.
    """
    if not url_or_domain:
        return ""
    raw = url_or_domain.strip().lower()
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


# ── Business names ────────────────────────────────────────────────────
# SERP providers return a page TITLE, not a business name: "Denver, CO | HOA
# Management Firm", "Home Page - NVCM | Community Association Management".
# Stored as-is it lands in an email as "...at Denver, CO | HOA Management
# Firm". The business is the title segment that shares a DISTINCTIVE word with
# the domain — "management" matches managementharbor.example but says nothing.

_TITLE_SEPARATORS = re.compile(r"\s+[|\-–—·•]\s+|\s*[|•·]\s*|:\s+")
_GENERIC_NAME_WORDS = frozenset("""
home page welcome official site website hoa hoas management manager managers
mgmt mgt company companies co inc llc ltd corp firm group services service
property properties community communities association associations condo
condominium residential homeowners owners real estate the and of in for best
top leading trusted local full arizona phoenix az texas tx florida fl
california ca nevada nv colorado co georgia ga carolina nc usa us america
roofing roof roofers roofer plumbing plumber plumbers contact about
""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w]


def _domain_stem(domain: str) -> str:
    host = normalize_domain(domain)
    return re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0]) if host else ""


def _segment_matches_domain(segment: str, stem: str) -> bool:
    # Navigation and service/location labels can share an incidental word
    # with a domain without identifying the business.
    if re.fullmatch(r"(?i)(contact(?: us)?|about(?: us)?|home(?: page)?)", segment.strip()):
        return False
    if re.search(r"(?i)\bservices?\s+(?:in|near)\s+\S", segment):
        return False
    words = _words(segment)
    distinctive = [w for w in words if w not in _GENERIC_NAME_WORDS]
    if any(len(w) >= 3 and w in stem for w in distinctive):
        return True
    # "Northvale Association of Community Managers" ↔ nacm.org
    initials = "".join(w[0] for w in words if w not in ("of", "the", "and", "&"))
    return len(initials) >= 3 and stem.startswith(initials)


def _segment_domain_evidence(segment: str, stem: str) -> int:
    """Amount of distinctive domain evidence, shared by SERP and homepage names."""
    if not _segment_matches_domain(segment, stem):
        return 0
    return sum(len(w) for w in set(_words(segment))
               if w not in _GENERIC_NAME_WORDS and len(w) >= 3 and w in stem)


_SPLIT_WORDS = sorted((w for w in _GENERIC_NAME_WORDS | {"with", "your"}
                       if len(w) >= 4), key=len, reverse=True)


def _split_stem(stem: str) -> list[str]:
    """managementharbor → [management, harbor]; kqresidential → [kq, residential]."""
    parts, buf, i = [], "", 0
    while i < len(stem):
        word = next((w for w in _SPLIT_WORDS if stem.startswith(w, i)), "")
        if word:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(word)
            i += len(word)
        else:
            buf += stem[i]
            i += 1
    if buf:
        parts.append(buf)
    return parts


def name_from_domain(domain: str) -> str:
    stem = normalize_domain(domain).rsplit(".", 1)[0]
    words = [w for chunk in re.split(r"[-_.]+", stem) if chunk for w in _split_stem(chunk)]
    # Short vowel-less leftovers are initials: "kq" → "KQ".
    return " ".join(w.upper() if len(w) <= 3 and not re.search(r"[aeiouy]", w)
                    else w.capitalize() for w in words)


def name_matches_domain(name: str, domain: str) -> bool:
    """True when the stored name demonstrably belongs to this domain."""
    stem = _domain_stem(domain)
    return bool(stem and name and _segment_matches_domain(name, stem))


def site_name_from_html(html: str, domain: str) -> str:
    """The business name a homepage gives itself (og:site_name, then <title>),
    or "" when neither names it in a way the domain backs up."""
    stem = _domain_stem(domain)
    candidates = []
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:site_name',
                r"(?is)<title[^>]*>(.*?)</title>"):
        m = re.search(pat, html or "", re.I)
        if m:
            candidates.append(unescape(re.sub(r"\s+", " ", m.group(1))).strip())
    # Distinctive brand evidence wins; equally backed names prefer the fullest
    # form: "The Management Harbor" over og's "Management Harbor".
    matches = [seg for text in candidates
               for seg in (x.strip(" ,.-") for x in _TITLE_SEPARATORS.split(text))
               if seg and len(seg) <= 60 and _segment_matches_domain(seg, stem)]
    return max(matches, key=lambda seg: (_segment_domain_evidence(seg, stem), len(seg)),
               default="")


def business_name(title: str, domain: str) -> str:
    """The business's name from a SERP title, or one derived from its domain."""
    stem = _domain_stem(domain)
    title = re.sub(r"\s*(\.\.\.|…)\s*$", "", (title or "").strip())
    if stem and title:
        segments = [segment.strip(" ,.-") for segment in _TITLE_SEPARATORS.split(title)]
        matches = [seg for seg in segments
                   if seg and len(seg) <= 60 and _segment_matches_domain(seg, stem)]
        # Prefer the segment with the most distinctive domain evidence rather
        # than the first incidental substring. Stable ties preserve title order.
        if matches:
            return max(matches, key=lambda seg: _segment_domain_evidence(seg, stem))
    return name_from_domain(domain) if stem else title


def is_junk(domain: str, name: str = "") -> bool:
    """True when this row would pollute the database rather than fill it."""
    if domain:
        if any(p.search(domain) for p in JUNK_DOMAIN_PATTERNS):
            return True
        # A bare TLD or an IP address is never a business we can profile.
        if "." not in domain or re.fullmatch(r"[\d.]+", domain):
            return True
    if name and any(p.search(name) for p in JUNK_NAME_PATTERNS):
        return True
    return False


# ── Geocoding ─────────────────────────────────────────────────────────
# Listings providers search a radius, not a place name, so "Denver, CO" has to
# become coordinates. Nominatim allows exactly this (single place lookups at
# <=1 req/s) and forbids bulk enumeration, so every result is cached forever in
# the settings table: a given city is looked up once, ever.

NOMINATIM = "https://nominatim.openstreetmap.org/search"
GEOCODE_UA = "Harvey/0.1 (open-source sales agent; github.com/ethanplusai/harvey)"


async def nominatim_point(city: str, client: httpx.AsyncClient) -> str | None:
    """"Denver, CO" -> "39.7392,-104.9903"; "" when Nominatim knows no such
    place, None when it couldn't be asked. Caching the answer is the
    caller's (it is kept for good: harvey/modules/runs.py)."""
    try:
        r = await client.get(
            NOMINATIM,
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": GEOCODE_UA},
        )
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        logger.warning("geocode failed for %r: %s", city, e)
        return None
    if not hits:
        logger.warning("geocode: no match for %r", city)
        return ""
    return f"{float(hits[0]['lat']):.4f},{float(hits[0]['lon']):.4f}"


# ── The record every provider returns ─────────────────────────────────


@dataclass
class Business:
    """One discovered business, normalised across providers."""

    name: str = ""
    domain: str = ""
    website: str = ""
    phone: str = ""
    address: str = ""
    location: str = ""
    category: str = ""
    rating: float | None = None
    review_count: int | None = None
    is_claimed: bool | None = None
    has_website: bool | None = None
    rank: int | None = None          # organic rank, when the provider gives one
    local_pack_rank: int | None = None   # slot in Google's local 3-pack (ads excluded)
    maps_rank: int | None = None     # position in Google Maps results
    keyword: str = ""                # the search that produced the ranks above
    external_id: str = ""            # "<provider>:<stable id>"
    alt_ids: tuple[str, ...] = ()    # other ids the same business was stored under
    source_url: str = ""
    provider: str = ""
    raw_title: str = ""             # SERP evidence for junk filtering before normalization

    def identity(self) -> str:
        return self.domain or self.external_id


@dataclass
class DiscoveryQuery:
    """What to look for, and where."""

    term: str                        # "roofing contractor"
    location: str = ""               # "Denver, CO"
    category: str = ""               # provider-native category slug, optional
    coordinate: str = ""             # "lat,lng,radius_km" for listings providers
    # How deep a ranked search goes. None: as deep as the source serves.
    depth: int | None = None
    # The most records to take from one query. None: every result the
    # source has; the spend cap is what stops a run.
    limit: int | None = None
    preset: str = ""                 # PRESETS key that built it, if any
    language: str = "en"             # search language code for SERP providers
    # How many results the source holds for it, once counted (a listings
    # provider's count request); the estimate is exact when every query has one.
    total: int | None = None
    # The country to search in (two letters, "RO"), when known: a search
    # that can be told one (Brave) gets results from there first.
    country: str = ""

    def keyword(self) -> str:
        return f"{self.term} {self.location}".strip()


# DataForSEO names places "Denver,Colorado,United States" — full state name,
# no spaces after commas. Harvey's geography is written the way people write
# it ("Denver, CO"), so US state abbreviations are expanded here. Anything
# else passes through with its commas tightened; a name DataForSEO doesn't
# know comes back as a task error, which the run reports.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_US_STATE_NAMES = {v.lower(): v for v in US_STATES.values()}

# DataForSEO requires a location on every SERP task. A search with no place
# at all (the non-local YC preset) is a United States search.
DEFAULT_LOCATION_CODE = 2840


def dataforseo_location_name(place: str) -> str:
    """"Denver, CO" -> "Denver,Colorado,United States"."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if len(parts) == 2:
        state = US_STATES.get(parts[1].upper()) or _US_STATE_NAMES.get(parts[1].lower())
        if state:
            return f"{parts[0]},{state},United States"
    return ",".join(parts)


def _dfs_location(query: "DiscoveryQuery", point_suffix: str) -> dict:
    """The location fields for a DataForSEO SERP task.

    A geocoded point wins: rank depends on where the searcher stands, and a
    city-centre point is the reproducible version of "searching from Denver".
    The stored coordinate's third part is the listings radius in km, which
    means nothing here, so only lat,lng are kept.
    """
    if query.coordinate:
        lat, lng = (query.coordinate.split(",") + [""])[:2]
        if lat.strip() and lng.strip():
            return {"location_coordinate": f"{lat.strip()},{lng.strip()},{point_suffix}"}
    if query.location:
        return {"location_name": dataforseo_location_name(query.location)}
    return {"location_code": DEFAULT_LOCATION_CODE}


# ── Provider adapters ─────────────────────────────────────────────────


# A web search can't say beforehand how many results Google will serve. An
# estimate for "every result" prices this many pages of 10: Google seldom
# serves more than about 200 for a phrase, and paging stops where it ends.
SEARCH_PAGES_ESTIMATE = 20


def search_pages(depth: int | None, page_size: int) -> int:
    """Pages an estimate prices: enough for ``depth``, or for every result."""
    if not depth:
        return SEARCH_PAGES_ESTIMATE
    return max(1, -(-depth // page_size))


# Match whole placeholder values, never substrings of opaque credentials.
_PLACEHOLDER_RE = re.compile(
    r"(?i)(placeholder|change[-_]?me|your[_-](?:api[_-])?key(?:[_-]here)?"
    r"|paste[_-](?:api[_-])?key(?:[_-]here)?|x{4,}|<[a-z_ -]+>"
    r"|todo|fixme|example[_-](?:api[_-])?key)"
)


# A search whose later page failed with something worth trying again (the
# network, a 5xx, a 429): it keeps what the earlier pages found and stops
# where it failed, so trying again carries on from that page.
PART_WAY = "failed part-way"


class ProviderExhausted(RuntimeError):
    """This provider can take no more searches in this run: its allowance or
    rate limit is used up, or it refused the key. The run stops and says why,
    rather than failing every remaining phrase the same way. ``retry_after``
    (seconds), when known, is when it will take searches again (an hourly
    allowance); without it a person has to act (a key, credit)."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class DiscoveryProvider:
    """One way of answering "who exists".

    Subclasses implement ``fetch``. Everything else — the menu metadata the
    dashboard renders, cost estimation, configuration checks — lives here so a
    new provider is a small class rather than a wiring exercise.
    """

    key: str = ""
    label: str = ""
    kind: str = "listings"           # listings | serp
    blurb: str = ""
    cost_note: str = ""
    free_tier: str = ""
    signup_url: str = ""
    env_keys: tuple[str, ...] = ()
    caveat: str = ""
    # Geocode cities before the run (listings search a radius; the
    # DataForSEO rank providers search from a point).
    needs_coordinates: bool = False
    # Rank signals this provider measures. For each one, a search that no
    # longer finds a business it found before records that, per phrase.
    rank_codes: tuple[str, ...] = ()

    def missing_keys(self, env: dict) -> list[str]:
        """Env keys that are absent, empty, or still the template placeholder.

        An empty ``SERPER_API_KEY=`` line used to pass the truthiness check,
        the run then "succeeded" with zero queries, and the process exited 0
        — a silent no-op that reads as success. Name the bad keys here,
        before any money or time is spent.
        """
        def _missing(value) -> bool:
            if not isinstance(value, str):
                return not value
            value = value.strip()
            return (not value) or bool(_PLACEHOLDER_RE.fullmatch(value))

        return [k for k in self.env_keys if _missing(env.get(k))]

    def configured(self, env: dict) -> bool:
        return not self.missing_keys(env)

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        """Projected spend, in dollars, before anything is called: every
        page a query can take, not just the first."""
        return 0.0

    def first_cost(self, query: DiscoveryQuery) -> float:
        """The least a query can cost: what the remaining budget must cover
        before the run starts it. A paging provider checks each further
        page itself (``halt``)."""
        return self.estimate([query])

    # A provider that can say how many results a query has before paying for
    # them (``count``) makes the estimate exact.
    can_count: bool = False

    async def count(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> tuple[int, float]:
        """(results the source holds for ``query``, what asking cost)."""
        raise NotImplementedError

    async def halt(self, page_cost: float) -> str:
        """Why the next page must not be fetched ("paused", "cancelled",
        "budget reached"), or "": the kill switch (or the job's own stop)
        and the spend cap, read between pages."""
        if self.paused is not None:
            why = await self.paused()
            if why:
                return why if isinstance(why, str) else "paused"
        if self.last_cost + page_cost > self.remaining_budget + 1e-9:
            return "budget reached"
        return ""

    def _stopped(self, why: str, unfetched: int | None = None,
                 unfetched_cost: float = 0.0, cursor: dict | None = None):
        """Record that this query ended early with results left behind,
        and (``cursor``) where to carry on from."""
        self.stop_reason = why
        self.complete = False
        self.unfetched = unfetched
        self.unfetched_cost = unfetched_cost
        self.next_cursor = cursor

    def start_page(self, default: int = 0) -> int:
        """The page a search resumed from a cursor starts at."""
        try:
            return int((self.cursor or {}).get("page", default))
        except (TypeError, ValueError):
            return default

    async def page_done(self, results: int) -> None:
        """Count a fetched page and tell the run (a job's progress): pages
        so far, results so far, and what the search has cost so far, so the
        money is recorded before the next page is paid for."""
        self.pages += 1
        # Counted against the module's free allowances as each page comes,
        # not when the search ends: a long search on another lane would
        # otherwise leave the background's share unseen meanwhile.
        await _count_uses(self.key, 1)
        if self.on_page is not None:
            await self.on_page(self.pages, results, self.last_cost)

    def _failed_part_way(self, error: Exception, page_label, cursor: dict) -> bool:
        """A page after the first failed. True when it is worth trying
        again: the search then stops there (PART_WAY), keeping what the
        earlier pages found, to carry on from ``cursor``."""
        from harvey.transient import is_transient

        if not is_transient(error):
            return False
        self._stopped(PART_WAY, cursor=cursor)
        self.last_error = f"page {page_label} failed: {error}"
        return True

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        raise NotImplementedError

    # Shared helper: providers report their own per-query spend so the run log
    # records what actually happened, not what was projected.
    last_cost: float = 0.0
    last_error: str = ""
    stop_reason: str = ""
    complete: bool = True
    remaining_budget: float = math.inf
    # Set per search by search_draft: reads the kill switch.
    paused = None
    # Set per search by search_draft: where a stopped search left off
    # (``cursor``, None: the first page), told after each page
    # (``on_page(pages, results, cost so far)``), and where to carry on (``next_cursor``,
    # set when it stops part-way; None when it went to the end).
    cursor: dict | None = None
    next_cursor: dict | None = None
    on_page = None
    pages: int = 0
    # Results the source said it holds for the last query, when it says.
    available: int | None = None
    # Left behind when the cap or the kill switch stopped the last query
    # part-way: how many (None: unknown) and what fetching them would cost.
    unfetched: int | None = None
    unfetched_cost: float = 0.0


class DataForSEOListings(DiscoveryProvider):
    """Google Business Profile data as a queryable database, not a scrape.

    The cheapest way to enumerate local businesses anywhere, and the only one
    with a filter for "has no website" — which for anyone selling websites is
    the highest-intent query available.
    """

    key = "dataforseo_listings"
    label = "DataForSEO Business Listings"
    kind = "listings"
    needs_coordinates = True
    blurb = ("Enumerate every business of a type within a radius, with phone, "
             "domain, rating and whether the listing is claimed. Best choice "
             "for local trades, clinics and contractors.")
    cost_note = ("$0.012 a request (up to 1,000 businesses) + $0.00036 a business: "
                 "$0.372 per 1,000. Every result is fetched, 1,000 a page")
    free_tier = "$1 signup credit; $50 minimum deposit to go live"
    signup_url = "https://app.dataforseo.com/api-access"
    env_keys = ("dataforseo_login", "dataforseo_password")
    caveat = ("No email addresses — Harvey resolves those in a later stage. "
              "Fetches every business the search matches, 1,000 a page; the "
              "estimate counts them first (about a cent a search) so the price "
              "is exact before the rest is paid for.")
    can_count = True

    TASK_COST = 0.012
    ITEM_COST = 0.00036
    PAGE_SIZE = 1000          # the API's maximum ``limit``

    def cost_of(self, n: int) -> float:
        """What fetching ``n`` businesses costs: a request per 1,000, plus
        each business."""
        pages = max(1, -(-n // self.PAGE_SIZE))
        return self.TASK_COST * pages + self.ITEM_COST * n

    def _wanted(self, query: DiscoveryQuery) -> int | None:
        """How many businesses the query will take, when known."""
        n = query.total
        if query.limit is not None:
            n = query.limit if n is None else min(n, query.limit)
        return n

    def estimate(self, queries):
        # Uncounted, a query is priced at one full page: the count (the
        # estimate's own request, or the first page) makes it exact.
        return sum(self.cost_of(self.PAGE_SIZE if (n := self._wanted(q)) is None else n)
                   for q in queries)

    def first_cost(self, query):
        return self.TASK_COST + self.ITEM_COST

    def _auth(self, env: dict) -> str:
        raw = f"{env['dataforseo_login']}:{env['dataforseo_password']}"
        return base64.b64encode(raw.encode()).decode()

    def base_url(self, env: dict) -> str:
        # The sandbox mirrors the response schema exactly and is never
        # charged — the right target for a first run and for the test suite.
        return ("https://sandbox.dataforseo.com/v3"
                if env.get("dataforseo_sandbox") else
                "https://api.dataforseo.com/v3")

    def _payload(self, query: DiscoveryQuery, limit: int) -> dict:
        payload = {
            "limit": limit,
            # A stable order, so pages neither overlap nor skip.
            "order_by": ["rating.votes_count,desc"],
        }
        if query.category:
            payload["categories"] = [query.category]
        else:
            payload["title"] = query.term
        if query.coordinate:
            payload["location_coordinate"] = query.coordinate
        elif query.location:
            # Without coordinates the API can only match on the title, so say
            # so rather than silently returning nationwide results.
            payload["description"] = query.location
        return payload

    async def _search(self, client, env, payload: dict) -> tuple[dict, float]:
        r = await client.post(
            f"{self.base_url(env)}/business_data/business_listings/search/live",
            headers={"Authorization": f"Basic {self._auth(env)}",
                     "Content-Type": "application/json"},
            json=[payload],
        )
        r.raise_for_status()
        data = r.json()
        return data, float(data.get("cost") or 0.0)

    async def count(self, client, env, query):
        # One business is the cheapest request that carries total_count.
        data, cost = await self._search(client, env, self._payload(query, 1))
        total = _dfs_result(data).get("total_count")
        return (int(total) if isinstance(total, int) else 0), cost

    async def fetch(self, client, env, query):
        """Every business the search matches, page by page.

        The first page says how many there are (``total_count``). After
        it, each page is asked for with the ``offset_token`` the previous
        one returned and otherwise the same request, as DataForSEO asks;
        a response without a token pages by ``offset`` instead. Before each
        further page the kill switch and the spend cap are read; a stop
        says how many were left and what fetching them would cost.
        """
        self.last_cost = 0.0
        want = query.limit
        resumed = self.cursor or {}
        page_size = min(self.PAGE_SIZE, want) if want is not None else self.PAGE_SIZE
        if resumed.get("page_size"):
            # Resumed: the pages keep the size the search started with (a
            # token repeats the request it follows).
            page_size = int(resumed["page_size"])
        elif math.isfinite(self.remaining_budget):
            # A first page the cap can't pay for in full is asked for
            # smaller, so the cap buys what it can. Later pages keep the
            # first one's size: an offset_token request must repeat the
            # request it follows.
            affordable = int((self.remaining_budget - self.TASK_COST) / self.ITEM_COST + 1e-6)
            if affordable < 1:
                raise ProviderExhausted("budget reached")
            page_size = min(page_size, affordable)
        base = self._payload(query, page_size)

        out: list[Business] = []
        seen: set[str] = set()
        token = resumed.get("token") or None
        offset = int(resumed.get("offset") or 0)
        total = resumed.get("total")
        fetched_here = False
        while True:
            left = None if total is None else max(0, total - offset)
            if want is not None:
                left = want - offset if left is None else min(left, want - offset)
            if left is not None and left <= 0:
                break
            if offset:
                page = self.TASK_COST + self.ITEM_COST * min(page_size, left or page_size)
                if not fetched_here and page > self.remaining_budget + 1e-9:
                    raise ProviderExhausted("budget reached")
                why = await self.halt(page)
                if why:
                    self._stopped(why, left, self.cost_of(left) if left else 0.0,
                                  cursor={"offset": offset, "token": token, "total": total,
                                          "page_size": page_size})
                    logger.info("%s: %s after %d of %s for %r", self.key, why, offset,
                                total if total is not None else "?", query.keyword())
                    break
            payload = dict(base)
            if want is not None and left is not None and left < page_size:
                # The last page of a limited query: a smaller page, so it
                # isn't billed for businesses it would drop. A token must
                # repeat the previous request, so this one goes by offset.
                payload.update(limit=left, offset=offset)
            elif token:
                payload["offset_token"] = token
            elif offset:
                payload["offset"] = offset
            try:
                data, cost = await self._search(client, env, payload)
            except Exception as e:
                if fetched_here and self._failed_part_way(
                        e, offset // max(1, page_size) + 1,
                        {"offset": offset, "token": token, "total": total,
                         "page_size": page_size}):
                    logger.info("%s: page at %d of %r failed, keeping %d: %s",
                                self.key, offset, query.keyword(), len(out), e)
                    break
                raise
            self.last_cost += cost
            result = _dfs_result(data)
            items = result.get("items") or []
            if total is None:
                count = result.get("total_count")
                total = count if isinstance(count, int) else None
                self.available = total
            new = 0
            for item in items:
                biz = self._to_business(item)
                ident = biz.external_id or biz.domain or f"{biz.name}|{biz.address}"
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(biz)
                new += 1
            offset += len(items)
            token = result.get("offset_token") or None
            fetched_here = True
            await self.page_done(len(out))
            # The end: a short page, or one with nothing new in it (a source
            # repeating itself must never become an endless loop).
            if len(items) < page_size or not new:
                break
        return out

    def _to_business(self, item: dict) -> Business:
        url = item.get("url") or ""
        rating = (item.get("rating") or {})
        addr = item.get("address_info") or {}
        city = ", ".join(x for x in (addr.get("city"), addr.get("region")) if x)
        return Business(
            name=item.get("title") or "",
            domain=normalize_domain(item.get("domain") or url),
            website=url,
            phone=item.get("phone") or "",
            address=item.get("address") or "",
            location=city,
            category=item.get("category") or "",
            rating=rating.get("value"),
            review_count=rating.get("votes_count"),
            is_claimed=item.get("is_claimed"),
            has_website=bool(url),
            external_id=_gbp_ids(item)[0],
            alt_ids=_gbp_ids(item)[1],
            source_url=item.get("check_url") or "",
            provider=self.key,
        )


class DataForSEOSerp(DiscoveryProvider):
    """Organic rank — and the local 3-pack above it — as the buying signal.

    Buy the rank, not the list. A business sitting at 11-30 for its own core
    search is visible enough to have tried and is losing — the single most
    useful thing you can know before writing to them. The same response
    carries the local pack, so who holds those three slots comes free.
    """

    key = "dataforseo_serp"
    label = "DataForSEO SERP (organic rank)"
    kind = "serp"
    needs_coordinates = True
    blurb = ("Search '<service> <city>' from the city centre and record where "
             "each business ranks, plus who holds the local 3-pack. Discovers "
             "businesses you didn't have AND scores the ones you did.")
    cost_note = ("$0.002 per 10 results (Live), billed for the results Google "
                 "serves: at most $0.04 a search (200 results)")
    free_tier = "$1 signup credit; free sandbox; $50 minimum deposit to go live"
    signup_url = "https://app.dataforseo.com/api-access"
    env_keys = ("dataforseo_login", "dataforseo_password")
    caveat = ("Searches as deep as DataForSEO goes: 200 results, its maximum for "
              "Google organic, so a search never finds more than 200 businesses. "
              "Below rank 30 it is mostly directories. Rank depends on where the "
              "searcher stands — this is the view from the city centre.")

    # Live mode. The standard queue is $0.0006 but asynchronous (post a task,
    # poll for it minutes later); the run loop checks the spend cap between
    # synchronous calls, so Live keeps that check honest.
    PAGE_COST = 0.002
    # What is billed as one unit (a SERP of 10 results), and the deepest
    # DataForSEO searches Google organic.
    UNIT_COST = PAGE_COST
    UNIT_RESULTS = 10
    MAX_DEPTH = 200
    # The radius of the search point. DataForSEO documents it in mm
    # (199-199999), so 2000 is 2 m: a pinpoint at the city centre, which is
    # what "when we searched from downtown Denver" means. Chosen so that if
    # the unit were metres it would still be 2 km, still downtown.
    POINT_RADIUS = "2000"
    rank_codes = ("SERP_RANK", "IN_LOCAL_PACK")

    def _depth(self, query: DiscoveryQuery) -> int:
        """The depth asked for: the query's, or the deepest the source goes."""
        return min(query.depth or self.MAX_DEPTH, self.MAX_DEPTH)

    def _units(self, depth: int) -> int:
        return max(1, -(-depth // self.UNIT_RESULTS))

    def estimate(self, queries):
        # Billing is the unit price x the units served; priced at the full
        # depth, since a search can't say beforehand how many Google serves.
        return sum(self.UNIT_COST * self._units(self._depth(q)) for q in queries)

    def first_cost(self, query):
        return self.UNIT_COST

    def _affordable_depth(self, query: DiscoveryQuery) -> int:
        """The depth to ask for: as deep as asked, or as deep as the spend
        cap still pays for (the run then stops, saying what is left)."""
        depth = self._depth(query)
        if not math.isfinite(self.remaining_budget):
            return depth
        units = int(self.remaining_budget / self.UNIT_COST + 1e-6)
        if units < 1:
            raise ProviderExhausted("budget reached")
        if units < self._units(depth):
            self._stopped("budget reached", None,
                          self.UNIT_COST * (self._units(depth) - units))
            return units * self.UNIT_RESULTS
        return depth

    _auth = DataForSEOListings._auth
    base_url = DataForSEOListings.base_url

    async def _post(self, client, env, endpoint, task):
        r = await client.post(
            f"{self.base_url(env)}/{endpoint}",
            headers={"Authorization": f"Basic {self._auth(env)}",
                     "Content-Type": "application/json"},
            json=[task],
        )
        r.raise_for_status()
        data = r.json()
        self.last_cost = float(data.get("cost") or 0.0)
        return _dfs_items(data)

    async def fetch(self, client, env, query):
        keyword = query.keyword()
        items = await self._post(client, env, "serp/google/organic/live/advanced", {
            "keyword": keyword, "language_code": query.language,
            "depth": self._affordable_depth(query),
            "device": "desktop", **_dfs_location(query, self.POINT_RADIUS)})

        # One Business per identity: a company in the pack AND at organic 14
        # is one prospect with two facts, not two prospects.
        found: dict[str, Business] = {}
        for item in items:
            kind = item.get("type")
            if kind == "organic":
                domain = normalize_domain(item.get("domain") or item.get("url") or "")
                if not domain:
                    continue
                biz = found.setdefault(domain, Business(
                    name=business_name(item.get("title") or "", domain),
                    raw_title=item.get("title") or "",
                    domain=domain, website=item.get("url") or "",
                    location=query.location, has_website=True,
                    source_url=item.get("url") or "",
                    provider=self.key, keyword=keyword))
                # rank_group is position within organic results.
                # `position` in this payload means left/right column, NOT rank.
                biz.rank = _best(biz.rank, item.get("rank_group"))
            elif kind == "local_pack" and not item.get("is_paid"):
                domain = normalize_domain(item.get("domain") or item.get("url") or "")
                cid = item.get("cid") or ""
                ident = domain or (f"dataforseo:cid:{cid}" if cid else "")
                if not ident:
                    continue
                rating = item.get("rating") or {}
                biz = found.get(ident)
                if biz is None:
                    biz = found[ident] = Business(
                        name=item.get("title") or "", raw_title=item.get("title") or "",
                        domain=domain, website=item.get("url") or "",
                        location=query.location,
                        # No website button in the pack is not proof there
                        # is no website, so this stays unknown.
                        has_website=True if domain else None,
                        external_id="" if domain else ident,
                        source_url=item.get("url") or "",
                        provider=self.key, keyword=keyword)
                biz.phone = biz.phone or item.get("phone") or ""
                if rating.get("value") is not None:
                    biz.rating = rating.get("value")
                    biz.review_count = rating.get("votes_count")
                biz.local_pack_rank = _best(biz.local_pack_rank, item.get("rank_group"))
        return list(found.values())


class DataForSEOMaps(DataForSEOSerp):
    """Google Maps rank — the map results a local searcher actually scrolls.

    Every result is a Google Business Profile, so it carries a phone, a
    category, a rating, whether the listing is claimed and whether there is a
    website at all. Businesses with no site are kept by their place_id.
    """

    key = "dataforseo_maps"
    label = "DataForSEO Google Maps (map rank)"
    kind = "serp"
    blurb = ("Search Google Maps for '<service> <city>' and record each "
             "business's map position, rating, category and claimed status — "
             "including businesses with no website at all.")
    cost_note = ("$0.002 per 100 results (Live), billed for the results served: "
                 "at most $0.014 a search (700 results)")
    caveat = ("Searches as deep as DataForSEO goes: 700 results, its maximum for "
              "Google Maps, so one search never finds more than 700 places. Map "
              "rank depends heavily on where the searcher stands; Harvey searches "
              "from the city centre at city zoom. Treat it as 'when we searched "
              "from downtown', not an absolute position.")

    SERP_COST = 0.002     # per SERP of up to 100 results
    UNIT_COST = SERP_COST
    UNIT_RESULTS = 100
    MAX_DEPTH = 700
    rank_codes = ("MAPS_RANK",)
    # City-wide zoom; DataForSEO accepts 3z-21z and defaults to 17z (a few
    # streets), which would rank only whoever is closest to the pin.
    ZOOM = "12z"

    async def fetch(self, client, env, query):
        keyword = query.keyword()
        items = await self._post(client, env, "serp/google/maps/live/advanced", {
            "keyword": keyword, "language_code": query.language,
            "depth": self._affordable_depth(query),
            **_dfs_location(query, self.ZOOM)})
        out = []
        for item in items:
            # maps_paid_item is an ad; only organic map results have a rank.
            if item.get("type") != "maps_search":
                continue
            url = item.get("url") or ""
            domain = normalize_domain(item.get("domain") or url)
            external, alt_ids = _gbp_ids(item)
            if not (domain or external):
                continue
            rating = item.get("rating") or {}
            addr = item.get("address_info") or {}
            out.append(Business(
                name=item.get("title") or "", raw_title=item.get("title") or "",
                domain=domain, website=url, phone=item.get("phone") or "",
                address=item.get("address") or "",
                location=", ".join(x for x in (addr.get("city"), addr.get("region")) if x)
                         or query.location,
                category=item.get("category") or "",
                rating=rating.get("value"), review_count=rating.get("votes_count"),
                is_claimed=item.get("is_claimed"),
                has_website=bool(url or domain),
                maps_rank=item.get("rank_group"),
                external_id=external, alt_ids=alt_ids, source_url=url,
                provider=self.key, keyword=keyword,
            ))
        return out


def _gbp_ids(item: dict) -> tuple[str, tuple[str, ...]]:
    """(external_id, aliases) for a Google Business Profile result.

    Keyed on cid: it is the one id the organic local pack carries too, so a
    websiteless business seen in the pack and then on Maps is one company.
    place_id stays an alias, because companies were stored by it before.
    """
    place = item.get("place_id") or ""
    cid = str(item.get("cid") or "")
    if cid:
        return f"dataforseo:cid:{cid}", ((f"dataforseo:{place}",) if place else ())
    return (f"dataforseo:{place}" if place else ""), ()


def _best(current: int | None, new) -> int | None:
    """The better (lower) of two ranks, ignoring missing ones."""
    if not isinstance(new, int):
        return current
    return new if current is None else min(current, new)


class Serper(DiscoveryProvider):
    """Google search with the friendliest free tier and the best depth pricing.

    The one provider that did not go to 10x for depth 100 when Google removed
    100-results-per-page, and 2,500 free queries with no card.
    """

    key = "serper"
    label = "Serper"
    kind = "serp"
    rank_codes = ("SERP_RANK",)
    blurb = ("Fast Google search with rank positions. The easiest one to try: "
             "2,500 free queries, no credit card, one API key.")
    cost_note = "~$0.30-$1.00 per 1,000 searches depending on plan"
    free_tier = "2,500 queries free, no card required"
    signup_url = "https://serper.dev"
    env_keys = ("serper_api_key",)
    caveat = ("Pages through every result Google serves for the phrase, 10 a "
              "page (one search each); Google itself seldom serves more than a "
              "few hundred.")

    PAGE_COST = 0.001
    PAGE_SIZE = 10

    def estimate(self, queries):
        return sum(self.PAGE_COST * search_pages(q.depth, self.PAGE_SIZE) for q in queries)

    def first_cost(self, query):
        return self.PAGE_COST

    async def fetch(self, client, env, query):
        """Page 1, 2, 3... until Google has no more, a page brings nothing
        new, or the query's depth is reached. The kill switch and the spend
        cap are read before each page after the first."""
        self.last_cost = 0.0
        out: list[Business] = []
        seen: set[str] = set()
        pages = (search_pages(query.depth, self.PAGE_SIZE) if query.depth
                 else None)
        page = first = max(1, self.start_page(1))
        page -= 1
        while pages is None or page < pages:
            page += 1
            if page > first:
                why = await self.halt(self.PAGE_COST)
                if why:
                    self._stopped(why, cursor={"page": page})
                    break
            body = {"q": query.keyword(),
                    # Free accounts reject num > 10 (HTTP 400 "Query pattern
                    # not allowed for free accounts"), verified 2026-10-26.
                    "num": self.PAGE_SIZE}
            if page > 1:
                body["page"] = page
            try:
                r = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": env["serper_api_key"],
                             "Content-Type": "application/json"},
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                if page == first:
                    raise
                # A later page that fails keeps what the earlier ones found;
                # one worth trying again is carried on from where it failed.
                if not self._failed_part_way(e, page, {"page": page}):
                    self.complete = False
                    self.last_error = f"page {page} failed: {e}"
                break
            self.last_cost += self.PAGE_COST
            items = data.get("organic") or []
            new = 0
            for item in items:
                domain = normalize_domain(item.get("link") or "")
                if not domain or domain in seen:
                    continue
                seen.add(domain)
                new += 1
                position = item.get("position")
                if isinstance(position, int) and 0 < position <= self.PAGE_SIZE:
                    # Positions may restart on each page: page 2's first is 11.
                    position += (page - 1) * self.PAGE_SIZE
                out.append(Business(
                    name=business_name(item.get("title") or "", domain),
                    raw_title=item.get("title") or "",
                    domain=domain,
                    website=item.get("link") or "",
                    location=query.location,
                    rank=position if isinstance(position, int) else None,
                    has_website=True,
                    source_url=item.get("link") or "",
                    provider=self.key,
                ))
            await self.page_done(len(out))
            if not items or not new:
                break
        return out


class TinyFish(DiscoveryProvider):
    """TinyFish web search: free with a key, at 30 searches a minute and
    500 an hour. Results are TinyFish's own ordering for the phrase, from
    no particular place, so they find businesses but are not recorded as a
    Google rank (SERP_RANK means a Google position searched from a point).
    """
    key = "tinyfish"
    label = "TinyFish"
    kind = "serp"
    rank_codes = ()
    blurb = ("Free web search with a key. Finds businesses by phrase; it "
             "records no search rank, since its ordering isn't Google's.")
    cost_note = "free"
    free_tier = "free with a key, no card: 30 searches a minute, 500 an hour"
    signup_url = "https://agent.tinyfish.ai/api-keys"
    env_keys = ("tinyfish_api_key",)
    caveat = ("Needs TINYFISH_API_KEY in .env. TinyFish serves at most 11 pages "
              "of 10 (about 110 results) for a phrase, and Harvey takes them all. "
              "It paces itself to 30 searches a minute; a phrase is up to 11 "
              "searches, so about 45 phrases an hour fit in the 500. No SERP_RANK: "
              "use Serper or DataForSEO SERP for rank signals.")

    ENDPOINT = "https://api.search.tinyfish.ai"
    MAX_PAGES = 11      # page index 0..10 is all it serves
    # 30 a minute, with a little room, and a little under 500 an hour.
    MIN_INTERVAL = 2.1
    HOURLY_LIMIT = 490

    def __init__(self):
        self._last_call = 0.0
        self._calls: list[float] = []      # this hour's, oldest first
        self._lock: tuple | None = None     # (event loop, its lock)

    async def _pace(self):
        """Wait for the next free slot, or stop before the hour's allowance
        is spent. Per process: a CLI run and a dashboard run at the same
        time each keep their own count."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock[0] is not loop:
            # One shared instance serves every run; a lock belongs to one loop.
            self._lock = (loop, asyncio.Lock())
        async with self._lock[1]:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 3600]
            if len(self._calls) >= self.HOURLY_LIMIT:
                minutes = int((3600 - (now - self._calls[0])) // 60) + 1
                raise ProviderExhausted(
                    f"TinyFish allows 500 searches an hour and this hour's are used up. "
                    f"Run again in about {minutes} minutes.",
                    retry_after=minutes * 60)
            wait = self._last_call + self.MIN_INTERVAL - now
            if self._last_call and wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            self._calls.append(self._last_call)

    def estimate(self, queries: list[DiscoveryQuery]) -> float:
        return 0.0

    async def fetch(self, client: httpx.AsyncClient, env: dict,
                    query: DiscoveryQuery) -> list[Business]:
        api_key = env.get("tinyfish_api_key", "").strip()
        if not api_key:
            raise RuntimeError("TinyFish needs TINYFISH_API_KEY in .env")

        headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
        }

        # TinyFish returns up to 10 results per page, with page index 0..10:
        # every page it serves, unless the query asks for less depth.
        pages_needed = min(self.MAX_PAGES, -(-query.depth // 10)) if query.depth else self.MAX_PAGES
        out: list[Business] = []

        first = self.start_page(0)
        for page in range(first, pages_needed):
            if page > first:
                why = await self.halt(0.0)
                if why:
                    self._stopped(why, cursor={"page": page})
                    break

            params = {
                "query": query.keyword(),
                "page": page,
            }

            try:
                r = await self._get_page(client, headers, params)
            except Exception as e:
                if page > first and self._failed_part_way(e, page, {"page": page}):
                    break
                raise
            items = self._items(r)
            if not items:
                break

            keyword = query.keyword()
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or ""
                domain = normalize_domain(url) if isinstance(url, str) else ""
                if not domain:
                    continue
                title = item.get("title") or ""
                out.append(Business(
                    name=business_name(title, domain),
                    raw_title=title,
                    domain=domain,
                    website=url,
                    location=query.location,
                    rank=None,
                    has_website=True,
                    source_url=url,
                    provider=self.key,
                    keyword=keyword,
                ))

            await self.page_done(len(out))
            # If this page returned fewer than 10 results, no more pages exist.
            if len(items) < 10:
                break

        return out

    async def _get_page(self, client: httpx.AsyncClient, headers: dict, params: dict):
        """One page of results, once more after a moment when the network
        or the rate limit is in the way; an answer worth no retry raises
        what it means (a refused key pauses the source)."""
        r = None
        for attempt in range(2):
            await self._pace()
            try:
                r = await client.get(self.ENDPOINT, headers=headers, params=params)
            except httpx.HTTPError as e:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise Transient(f"TinyFish search unreachable: {e}") from e

            if r.status_code == 429:
                retry_after = 2.0
                try:
                    retry_after = float(r.headers.get("Retry-After", "2"))
                except (ValueError, TypeError):
                    pass
                if attempt == 0:
                    await asyncio.sleep(min(60.0, max(1.0, retry_after)))
                    continue
                raise ProviderExhausted(
                    "TinyFish: rate limited (it allows 30 searches a minute and 500 an "
                    "hour). Try again later, or with fewer phrases.",
                    retry_after=max(60.0, min(3600.0, retry_after)))
            break

        if r is None:
            raise RuntimeError("TinyFish: Request failed.")

        if r.status_code == 401:
            raise ProviderExhausted(
                "TinyFish: Invalid or missing API key (HTTP 401). "
                "Check TINYFISH_API_KEY in .env or get a free key at https://agent.tinyfish.ai/api-keys")
        if r.status_code == 402:
            err_msg = ""
            try:
                err_data = r.json()
                err = err_data.get("error")
                if isinstance(err, dict):
                    err_msg = err.get("message", "")
                elif isinstance(err, str):
                    err_msg = err
            except Exception:
                pass
            raise ProviderExhausted(
                f"TinyFish refused the search (HTTP 402). {err_msg}".strip())
        r.raise_for_status()
        return r

    @staticmethod
    def _items(r) -> list:
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError("TinyFish sent an unexpected answer (not JSON)") from e
        if not isinstance(data, dict):
            raise RuntimeError("TinyFish sent an unexpected answer (not an object)")
        items = data.get("results") or []
        return items if isinstance(items, list) else []


class OpenStreetMap(DiscoveryProvider):
    """Genuinely free, no account, no key. Honest about what that costs you.

    Good enough to try the whole pipeline end to end without signing up for
    anything, and a useful cross-reference layer afterwards. Not good enough
    to be your only source: OSM maps physical premises, so service-area
    businesses (roofers, HVAC, plumbers) are heavily under-represented.
    """

    key = "osm"
    label = "OpenStreetMap (Overpass)"
    kind = "listings"
    needs_coordinates = True
    blurb = ("Free public map data. No signup, no key, no card. Try the whole "
             "pipeline for nothing before you pay anyone.")
    cost_note = "free"
    free_tier = "unlimited, within community rate etiquette"
    signup_url = "https://www.openstreetmap.org"
    env_keys = ()
    caveat = ("Coverage is thin for any trade without a storefront — OSM maps "
              "premises, so it holds under 2,000 roofers for the entire US. "
              "Great for a free trial run or as a cross-reference; not a "
              "complete list.")

    # Several public Overpass instances run the same API. The main one
    # throttles readily, so try the mirrors before giving up — a free source
    # that fails half the time is not usable as a default.
    # lz4. and z. are the main instance's sister servers; private.coffee
    # (which kumi.systems now points at) is IPv6-only, so it comes last.
    ENDPOINTS = (
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    )
    # Overpass etiquette for a shipped tool: modest volume, no parallelism.
    MIN_INTERVAL = 2.0
    BACKOFF = 5.0
    # The server may take QUERY_TIMEOUT seconds; wait a little longer than
    # that for its answer, since a loaded instance is slow but still right.
    QUERY_TIMEOUT = 90
    READ_TIMEOUT = httpx.Timeout(connect=10.0, read=QUERY_TIMEOUT + 30.0,
                                 write=10.0, pool=10.0)

    # A friendly term maps to the OSM tags that actually carry those businesses.
    TAG_MAP = {
        "roof": ['craft=roofer', 'shop=roofing'],
        "plumb": ['craft=plumber'],
        "hvac": ['craft=hvac'],
        "electric": ['craft=electrician'],
        "dentist": ['amenity=dentist'],
        "lawyer": ['office=lawyer'],
        "attorney": ['office=lawyer'],
        "contractor": ['craft=builder', 'office=construction_company'],
        "landscap": ['craft=gardener', 'shop=garden_centre'],
    }
    # Medical practices, English and Romanian. A term qualifies only when
    # EVERY word of the accent-folded term is in MEDICAL_WORDS (below), so
    # "lawn doctor", "tax clinic" or "pharmacy software" never borrow a medical
    # category. It then takes the first key here that starts a word of it, so
    # the specific ones come first: "clinica stomatologica" is a dentist,
    # "laborator analize medicale" a lab, and "medic" (which also catches
    # "cabinet medical") is the last resort.
    MEDICAL_TAG_MAP = {
        "dental": ['amenity=dentist'],
        "dentar": ['amenity=dentist'],
        "stomatolog": ['amenity=dentist'],
        "ortodon": ['amenity=dentist'],
        "pharmac": ['amenity=pharmacy'],
        "farmaci": ['amenity=pharmacy'],
        "laborator": ['healthcare=laboratory'],
        "medical lab": ['healthcare=laboratory'],
        "physio": ['healthcare=physiotherapist'],
        "kinetoterap": ['healthcare=physiotherapist'],
        "fizioterap": ['healthcare=physiotherapist'],
        "recuperare": ['healthcare=physiotherapist'],
        "clinic": ['amenity=clinic', 'healthcare=clinic'],
        "policlinic": ['amenity=clinic', 'healthcare=clinic'],
        "doctor": ['amenity=doctors', 'healthcare=doctor'],
        "medic": ['amenity=doctors', 'healthcare=doctor'],
    }
    # The words a medical practice term may be made of: the stems above plus
    # the generic words that sit around them ("cabinet", "family", "general
    # practice", "specialist"). Word prefixes, so inflections match
    # ("medicala", "familiei", "privata"). Anything else ("lawn", "spa",
    # "billing", "veterinara", "equipment") means it is not a practice we
    # have a category for, and the term raises like any unmapped one.
    MEDICAL_WORDS = (
        "dentist", "dental", "dentar", "stomatolog", "ortodon", "pharmac",
        "farmac", "laborator", "analize", "physio", "kinetoterap",
        "fizioterap", "recuperare", "clinic", "policlinic", "doctor", "medic",
        "cabinet", "family", "familie", "general", "practice", "office",
        "privat", "specialist", "primar", "urgent",
    )
    # Short words match whole only: as prefixes "de" would let in "design",
    # "si" "sistem" and "lab" "label". "s" is the one in "doctor's office".
    MEDICAL_JOINERS = frozenset({"de", "si", "and", "s", "lab", "labs"})
    # Medical-vocabulary words that are still not a practice: a drug maker,
    # a public insurer, a doctorate, clinical research, a dental lab.
    MEDICAL_REFUSE = re.compile(
        r"\b(?:pharmaceutic|farmaceutic|medicare|medicaid|doctora|clinical\b"
        r"|dental lab|laborator\w* dentar)")

    @staticmethod
    def _fold(text: str) -> str:
        """Lowercase without diacritics, so "clinică" and "clinica" match alike."""
        return "".join(ch for ch in unicodedata.normalize("NFKD", text.lower())
                       if not unicodedata.combining(ch))

    def _tags(self, term: str) -> list[str]:
        """The OSM tags that actually carry this kind of business, or none.

        There is deliberately no catch-all fallback. An earlier version fell
        back to `office=company`, which answers "roofers in Denver" with every
        registered office in Denver — coffee roasters, churches, photography
        studios. A search this source cannot answer has to say so; filling the
        database with plausible-looking junk is far worse than returning
        nothing.
        """
        low = term.lower()
        for needle, tags in self.TAG_MAP.items():
            if needle in low:
                return tags
        folded = self._fold(term)
        words = re.findall(r"[a-z]+", folded)
        if not words or self.MEDICAL_REFUSE.search(folded) or not all(
                w in self.MEDICAL_JOINERS or w.startswith(self.MEDICAL_WORDS)
                for w in words):
            return []
        for needle, tags in self.MEDICAL_TAG_MAP.items():
            if re.search(r"\b" + re.escape(needle), folded):
                return tags
        return []

    async def fetch(self, client, env, query):
        if not query.coordinate:
            logger.warning("osm: no coordinate for %r — Overpass needs a "
                           "lat,lng,radius to search", query.keyword())
            return []
        tags = self._tags(query.term)
        if not tags:
            known = sorted({**self.TAG_MAP, **self.MEDICAL_TAG_MAP})
            raise RuntimeError(
                f"OpenStreetMap has no category for {query.term!r}. It can only "
                f"search mapped trades ({', '.join(known)}). Use a "
                f"listings or SERP provider for this one.")
        lat, lng, radius_km = (query.coordinate.split(",") + ["", "", ""])[:3]
        radius_m = int(float(radius_km or 25) * 1000)
        clauses = "".join(
            f'node[{tag.split("=")[0]}={tag.split("=")[1]}](around:{radius_m},{lat},{lng});'
            f'way[{tag.split("=")[0]}={tag.split("=")[1]}](around:{radius_m},{lat},{lng});'
            for tag in tags
        )
        # Every match, unless the query asks for fewer.
        cap = f" {query.limit}" if query.limit else ""
        body = f"[out:json][timeout:{self.QUERY_TIMEOUT}];({clauses});out center{cap};"

        # Overpass is a free volunteer service and 429s/504s readily under
        # load. Walk the mirrors, then RAISE: returning [] would make a
        # throttled run look exactly like a market with no businesses in it.
        headers = {"User-Agent": "Harvey/0.1 (open-source sales agent; "
                                 "github.com/ethanplusai/harvey)"}
        r = None
        for i, endpoint in enumerate(self.ENDPOINTS):
            if i:
                await asyncio.sleep(self.BACKOFF)
            try:
                r = await client.post(endpoint, content=body.encode(),
                                      headers=headers, timeout=self.READ_TIMEOUT)
            except httpx.HTTPError as e:
                logger.warning("osm: %s unreachable (%s)", endpoint, e)
                r = None
                continue
            # 429 and every 5xx (502 and 503 come from an overloaded
            # instance's proxy, not from the query) mean "try another one".
            if r.status_code != 429 and r.status_code < 500:
                # An instance that ran out of time or memory still answers
                # 200, with what it had so far and a remark: a partial list
                # must not pass for the whole market.
                if r.status_code == 200 and _overpass_gave_up(r):
                    logger.warning("osm: %s stopped part-way (%s) — trying the "
                                   "next mirror", endpoint, _overpass_gave_up(r))
                    r = None
                    continue
                break
            logger.warning("osm: %s is overloaded (%s) — trying the next mirror",
                           endpoint, r.status_code)
        else:
            raise RuntimeError(
                "Every public Overpass mirror is overloaded right now. This is "
                "free volunteer infrastructure — wait a few minutes and re-run, "
                "or use a paid provider for bulk work.")
        if r is None:
            raise RuntimeError("Could not reach any Overpass mirror.")
        r.raise_for_status()

        out = []
        for el in r.json().get("elements") or []:
            tags = el.get("tags") or {}
            name = tags.get("name") or ""
            if not name:
                continue
            site = tags.get("website") or tags.get("contact:website") or ""
            city = ", ".join(x for x in (tags.get("addr:city"),
                                         tags.get("addr:state")) if x)
            out.append(Business(
                name=name,
                domain=normalize_domain(site),
                website=site,
                phone=tags.get("phone") or tags.get("contact:phone") or "",
                address=" ".join(x for x in (tags.get("addr:housenumber"),
                                             tags.get("addr:street")) if x),
                location=city or query.location,
                category=(tags.get("craft") or tags.get("shop") or tags.get("office")
                          or tags.get("healthcare") or tags.get("amenity") or ""),
                has_website=bool(site),
                external_id=f"osm:{el.get('type')}/{el.get('id')}",
                source_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
                provider=self.key,
            ))
        return out


def _overpass_gave_up(r: httpx.Response) -> str:
    """Overpass's remark when it stopped a query part-way, else ''."""
    try:
        remark = (r.json() or {}).get("remark") or ""
    except ValueError:
        return ""
    return remark if "error" in remark.lower() else ""


class ScavioProvider(DiscoveryProvider):
    """Shared plumbing for the two Scavio adapters: one key, one credit price.

    Scavio bills 1 credit per page. A page that fails after earlier pages
    succeeded ends the query with what it already has, the same rule the run
    loop applies between queries. A failure on the first page raises:
    returning [] would make an expired key look like an empty market.
    """

    signup_url = "https://scavio.dev"
    env_keys = ("scavio_api_key",)
    free_tier = "50 credits on signup, no card"

    API = "https://api.scavio.dev/api/v2/google"
    # Conservative pay-as-you-go price; subscription credits can cost less.
    CREDIT_PRICE = 0.01
    MIN_INTERVAL = 1.05  # free accounts allow one request per second

    def __init__(self):
        self._last_call = 0.0

    async def _post(self, client, env, path: str, body: dict) -> dict:
        if self.last_cost + self.CREDIT_PRICE > self.remaining_budget + 1e-12:
            raise ProviderExhausted("Scavio: budget reached before the next page")
        wait = self._last_call + self.MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()
        try:
            r = await client.post(
                f"{self.API}{path}",
                headers={"Authorization": f"Bearer {env['scavio_api_key']}",
                         "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError:
            raise RuntimeError("Scavio: request failed; try again later") from None
        # Never put response bodies in logs: upstream errors may echo secrets.
        if r.status_code in (401, 402, 403, 429):
            reasons = {401: "invalid API key", 402: "insufficient credits",
                       403: "access refused", 429: "rate limit reached"}
            raise ProviderExhausted(f"Scavio {r.status_code}: {reasons[r.status_code]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Scavio {r.status_code}: request failed")
        # A successful response is billable even if its body is malformed.
        self.last_cost += self.CREDIT_PRICE
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("Scavio: invalid JSON response") from None
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError("Scavio: invalid response")
        credits = data.get("credits_used", 1)
        if (isinstance(credits, (int, float)) and not isinstance(credits, bool)
                and math.isfinite(credits) and credits >= 0):
            self.last_cost += (credits - 1) * self.CREDIT_PRICE
        return data

    async def _pages(self, client, env, query, path: str, body: dict,
                     results_key: str, page_size: int, pages: int | None
                     ) -> list[tuple[int, dict]]:
        """Every result as (offset of its page, item), page by page: up to
        ``pages``, or (None) until the source has no more. The kill switch
        and the spend cap are read before each page after the first."""
        self.last_cost = 0.0
        self.last_error = self.stop_reason = ""
        self.complete = True
        rows: list[tuple[int, dict]] = []
        first = self.start_page(0)
        page = first - 1
        while pages is None or page + 1 < pages:
            page += 1
            start = page * page_size
            if page > first:
                why = await self.halt(self.CREDIT_PRICE)
                if why:
                    self._stopped(why, cursor={"page": page})
                    break
            try:
                data = await self._post(client, env, path, dict(body, start=start))
                items = data.get(results_key)
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise RuntimeError("Scavio: invalid results list")
                for item in items:
                    for field in ("title", "website", "link", "phone", "address", "type", "place_id"):
                        if item.get(field) is not None and not isinstance(item[field], str):
                            raise RuntimeError("Scavio: invalid result field")
                    for field in ("rating", "reviews", "position"):
                        value = item.get(field)
                        if value is not None and (isinstance(value, bool)
                                or not isinstance(value, (int, float))
                                or not math.isfinite(value) or value < 0):
                            raise RuntimeError("Scavio: invalid numeric result field")
                last_page = self._last_page(data, items, page_size)
            except Exception as e:
                if rows and self._failed_part_way(e, page + 1, {"page": page}):
                    logger.warning("%s: page %d of %r failed, keeping %d results and "
                                   "carrying on from it later: %s",
                                   self.key, page + 1, query.keyword(), len(rows), e)
                    break
                self.complete = False
                self.last_error = str(e)
                if isinstance(e, ProviderExhausted):
                    self.stop_reason = str(e)
                if not rows:
                    raise
                logger.warning("%s: page %d of %r failed, keeping %d results: %s",
                               self.key, page + 1, query.keyword(), len(rows), e)
                break
            rows.extend((start, item) for item in items)
            await self.page_done(len(rows))
            if last_page:
                break
        return rows

    def _last_page(self, data: dict, items: list, page_size: int) -> bool:
        return len(items) < page_size

    def first_cost(self, query):
        return self.CREDIT_PRICE


class ScavioMaps(ScavioProvider):
    """Google Maps listings, the source issue #28 asked for.

    Every place Google Maps shows for a search around a point, with phone,
    website, category, rating and review count. Businesses with no website
    are kept, like the other listings providers.
    """

    key = "scavio_maps"
    needs_coordinates = True
    label = "Scavio Google Maps"
    kind = "listings"
    blurb = ("Google Maps results around each city: phone, website, category, "
             "rating and review count for every place. Up to 120 businesses "
             "per search: all Google Maps serves.")
    cost_note = "1 credit per page (up to 20 listings); estimates use $0.01/credit"
    caveat = ("Google Maps serves at most 120 places for one search (6 pages of "
              "20), and Harvey takes them all. It does not say whether a listing "
              "is claimed, so the unclaimed-listing signal stays empty. Same key "
              "as Scavio Google Search.")

    PAGE_SIZE = 20
    MAX_PAGES = 6        # the API accepts start up to 100

    def _page_count(self, query: DiscoveryQuery) -> int:
        if not query.limit:
            return self.MAX_PAGES
        return max(1, min(self.MAX_PAGES, -(-query.limit // self.PAGE_SIZE)))

    def estimate(self, queries):
        return sum(self.CREDIT_PRICE * self._page_count(q) for q in queries)

    @staticmethod
    def map_center(coordinate: str) -> str:
        """"39.7392,-104.9903,40" -> "@39.7392,-104.9903,10z".

        Google Maps takes a center and a zoom, not a radius. A zoom-z view
        spans roughly 40,000 km / 2^z, so a 40 km radius is zoom 10 (one
        metro area) and 5 km is zoom 13.
        """
        parts = [p.strip() for p in coordinate.split(",")]
        if len(parts) < 2 or not all(parts[:2]):
            return ""
        try:
            lat, lng = float(parts[0]), float(parts[1])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return ""
        except ValueError:
            return ""
        try:
            radius_km = float(parts[2]) if len(parts) > 2 and parts[2] else 40.0
        except ValueError:
            radius_km = 40.0
        if not math.isfinite(radius_km) or radius_km <= 0:
            radius_km = 40.0
        zoom = round(math.log2(40000 / max(radius_km, 0.5)))
        return f"@{parts[0]},{parts[1]},{max(3, min(zoom, 16))}z"

    async def fetch(self, client, env, query):
        # The map center picks the area, as it does in the Maps app. Without
        # one the API falls back to a default city for the country, so the
        # place name has to ride in the query instead.
        center = self.map_center(query.coordinate) if query.coordinate else ""
        body = {"query": query.term, "ll": center} if center else {"query": query.keyword()}

        out = []
        for _, item in await self._pages(
                client, env, query, "/maps/search", body, "local_results",
                self.PAGE_SIZE, self._page_count(query)):
            name = item.get("title") or ""
            if not name:
                continue
            site = item.get("website") or ""
            place_id = item.get("place_id") or ""
            out.append(Business(
                name=name,
                domain=normalize_domain(site),
                website=site,
                phone=item.get("phone") or "",
                address=item.get("address") or "",
                location=query.location,
                category=item.get("type") or "",
                rating=item.get("rating"),
                review_count=item.get("reviews"),
                has_website=bool(site),
                # A Maps list position is not an organic rank, so rank stays
                # None and no SERP_RANK is recorded for it.
                external_id=f"gmaps:{place_id}" if place_id else "",
                source_url=(f"https://www.google.com/maps/place/?q=place_id:{place_id}"
                            if place_id else site),
                provider=self.key,
            ))
        return out


class ScavioSearch(ScavioProvider):
    """Google organic results with rank, from the same key as Scavio Maps."""

    key = "scavio_search"
    rank_codes = ("SERP_RANK",)
    label = "Scavio Google Search"
    kind = "serp"
    blurb = ("Google organic results with rank positions. One Scavio key "
             "covers this and Google Maps listings.")
    cost_note = ("1 credit per page (up to 10 results), every page Google serves; "
                 "estimated at 20 pages ($0.20) a search")
    caveat = ("Results come from google.com in English (US) unless the search "
              "names another place. Pages until Google has no next page.")

    PAGE_SIZE = 10

    def estimate(self, queries):
        return sum(self.CREDIT_PRICE * search_pages(q.depth, self.PAGE_SIZE)
                   for q in queries)

    def _last_page(self, data, items, page_size):
        # Google often shows fewer than 10 organic results on a page (a local
        # pack or videos take the space), so a short page is not the end.
        # The end is when Google offers no next page.
        pagination = data.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise RuntimeError("Scavio: invalid pagination")
        return not items or not pagination.get("next")

    async def fetch(self, client, env, query):
        out = []
        for start, item in await self._pages(
                client, env, query, "", {"query": query.keyword()},
                "organic_results", self.PAGE_SIZE,
                -(-query.depth // self.PAGE_SIZE) if query.depth else None):
            domain = normalize_domain(item.get("link") or "")
            if not domain:
                continue
            position = item.get("position")
            out.append(Business(
                name=business_name(item.get("title") or "", domain),
                raw_title=item.get("title") or "",
                domain=domain,
                website=item.get("link") or "",
                location=query.location,
                # `position` restarts at 1 on every page, so page 2's first
                # result is rank 11, not rank 1.
                rank=start + position if isinstance(position, int) and position > 0 else None,
                has_website=True,
                source_url=item.get("link") or "",
                provider=self.key,
            ))
        return out


# The providers whose code lives in this file. Which of them exist for
# Harvey is decided by the manifests (ProviderRegistry), not by this list.
BUILT_IN: dict[str, DiscoveryProvider] = {
    p.key: p for p in (
        OpenStreetMap(),
        TinyFish(),
        DataForSEOListings(),
        DataForSEOSerp(),
        DataForSEOMaps(),
        Serper(),
        ScavioMaps(),
        ScavioSearch(),
    )
}


class ProviderRegistry(MutableMapping):
    """The discovery sources, built from the module manifests: a key is
    here only when a module's capability claims it (``provider = "..."``).
    Its code is the module's own (``PROVIDERS`` in the module's entry
    file) or one of the built-in ones above. So a new search source is a
    module folder, with no change here, and a source no module claims
    doesn't exist: it is refused by name, never run unattributed.

    Assigning a key installs an implementation (a test's stand-in); it is
    used only while a module claims that key."""

    def __init__(self, built_in: dict[str, DiscoveryProvider]):
        self._code = dict(built_in)

    def _claims(self) -> dict[str, DiscoveryProvider]:
        import importlib

        from harvey.modules import switches

        out: dict[str, DiscoveryProvider] = {}
        for m in switches.manifests():
            for cap in m.capabilities:
                key = cap.get("provider")
                if not key or key in out:
                    continue
                provider = self._code.get(key)
                if provider is None and m.entry:
                    try:
                        provider = (getattr(importlib.import_module(m.entry), "PROVIDERS", None)
                                    or {}).get(key)
                    except Exception as e:
                        logger.warning("discover: %s's code didn't load: %s", m.id, e)
                if provider is not None:
                    out[key] = provider
        # The built-in ones in their usual order, then the rest.
        order = list(self._code)
        return dict(sorted(out.items(), key=lambda kv: (
            order.index(kv[0]) if kv[0] in order else len(order))))

    def __getitem__(self, key):
        return self._claims()[key]

    def __setitem__(self, key, provider):
        self._code[key] = provider

    def __delitem__(self, key):
        del self._code[key]

    def __iter__(self):
        return iter(self._claims())

    def __len__(self):
        return len(self._claims())

    def __contains__(self, key):
        return key in self._claims()


PROVIDERS: ProviderRegistry = ProviderRegistry(BUILT_IN)

DEFAULT_PROVIDER = "osm"


NO_SEARCH_RESULTS = 40102


def _dfs_items(payload: dict) -> list[dict]:
    """Pull result items out of a DataForSEO envelope, surfacing errors."""
    return _dfs_result(payload).get("items") or []


def _dfs_result(payload: dict) -> dict:
    """The first result of a DataForSEO envelope (its items, total_count,
    offset_token), surfacing errors; {} when there is none.

    A bare status code costs three wrong guesses; the message in the body
    usually makes the problem obvious immediately.
    """
    if payload.get("status_code") not in (20000, None):
        raise RuntimeError(
            f"DataForSEO {payload.get('status_code')}: "
            f"{payload.get('status_message')}")
    tasks = payload.get("tasks") or []
    if not tasks:
        return {}
    task = tasks[0]
    if task.get("status_code") == NO_SEARCH_RESULTS:
        # A real answer — nobody ranks for this search here — not a failure.
        return {}
    if task.get("status_code") not in (20000, None):
        raise RuntimeError(
            f"DataForSEO task {task.get('status_code')}: "
            f"{task.get('status_message')}")
    results = task.get("result") or []
    if not results or not isinstance(results[0], dict):
        return {}
    return results[0]


# ── The run ───────────────────────────────────────────────────────────


@dataclass
class DiscoveryReport:
    """What a run did — the honest version, including what it skipped."""

    provider: str = ""
    queries: int = 0
    queries_ran: int = 0     # attempted provider requests
    queries_succeeded: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    found: int = 0
    junk: int = 0
    # Counted apart from `junk` on purpose: junk is a judgement about whether
    # a result is a business at all, this is a company the operator has
    # forbidden. Folding them together would hide a do-not-contact list that
    # has grown too broad inside a number nobody questions.
    excluded: int = 0
    new_companies: int = 0
    known_companies: int = 0
    observations: int = 0
    run_id: str = ""
    errors: list[str] = field(default_factory=list)
    stopped: str = ""
    # What the source said it holds for the searches that said (a listings
    # count), and what the estimate's count requests cost.
    available: int | None = None
    counted: bool = False
    count_cost: float = 0.0
    # Left behind when the spend cap or the kill switch stopped the run:
    # results a search had left (as far as the source said), searches not
    # run at all, and what fetching all of it would cost.
    unfetched: int = 0
    queries_left: int = 0
    unfetched_cost: float = 0.0
    # For a job: pages fetched, where the last search stopped (to resume
    # from), whether its failure is worth trying again and when the source
    # takes more, and the companies it saved (their sites are read next).
    pages: int = 0
    cursor: dict | None = None
    transient: bool = False
    retry_after: float | None = None
    company_ids: list[str] = field(default_factory=list)

    def left_behind(self) -> str:
        """One line on what a stopped run left unfetched, or ""."""
        if not (self.unfetched or self.queries_left or self.unfetched_cost):
            return ""
        parts = []
        if self.unfetched:
            parts.append(f"{self.unfetched:,} results left unfetched")
        if self.queries_left:
            parts.append(f"{self.queries_left:,} "
                         f"{'search' if self.queries_left == 1 else 'searches'} not run")
        if not parts:
            parts.append("more results left unfetched")
        return (", ".join(parts)
                + f": fetching them would cost about ${self.unfetched_cost:.2f} more")

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "queries": self.queries,
            "queries_ran": self.queries_ran,
            "queries_succeeded": self.queries_succeeded,
            "estimated_cost": round(self.estimated_cost, 4),
            "actual_cost": round(self.actual_cost, 4),
            "found": self.found, "junk": self.junk,
            "excluded": self.excluded,
            "new_companies": self.new_companies,
            "known_companies": self.known_companies,
            "observations": self.observations,
            "run_id": self.run_id, "errors": self.errors,
            "stopped": self.stopped,
            "available": self.available, "counted": self.counted,
            "count_cost": round(self.count_cost, 4),
            "unfetched": self.unfetched, "queries_left": self.queries_left,
            "unfetched_cost": round(self.unfetched_cost, 4),
            "left_behind": self.left_behind(),
            "pages": self.pages,
        }


# Built-in searches beyond "your ICP". Each says how to turn an industry into
# a search, and which kind of provider can run it.
PRESETS = {
    # Startups that say on their own site they're YC-backed. Never
    # ycombinator.com itself, whose terms forbid scraping and harvesting
    # contacts; its pages and the startup databases are excluded too.
    "yc": {
        "label": "YC startups",
        "kinds": ("serp",),
        "term": ('"backed by Y Combinator" {industry} -site:ycombinator.com '
                 "-site:workatastartup.com -site:startupschool.org "
                 "-site:linkedin.com -site:crunchbase.com"),
        "local": False,
    },
    # Companies hiring for one of your hiring_signals roles (else ICP titles),
    # found by their OWN careers pages. The hosted boards are excluded: a
    # boards.greenhouse.io hit names a board, not a website to profile.
    # Profiling then reads the board their site links to and records the
    # role (HIRING_ROLE); the search hit alone records nothing.
    "hiring": {
        "label": "Companies hiring for your roles",
        "kinds": ("serp",),
        "term": ('"{role}" careers {industry} -site:greenhouse.io -site:lever.co '
                 "-site:ashbyhq.com -site:workable.com -site:smartrecruiters.com "
                 "-site:jobvite.com -site:icims.com -site:bamboohr.com -site:recruitee.com "
                 "-site:teamtailor.com -site:personio.de -site:breezy.hr -site:applytojob.com "
                 "-site:myworkdayjobs.com -site:ats.rippling.com -site:linkedin.com "
                 "-site:indeed.com -site:glassdoor.com -site:ziprecruiter.com"),
        "local": False,
    },
    # Startups that say on their own site an accelerator backed them. Two
    # searches per industry, because Google reads only the first 32 words.
    "accelerator": {
        "label": "Accelerator startups",
        "kinds": ("serp",),
        "terms": (
            '{industry} ("backed by Techstars" OR "backed by Antler" OR "backed by Entrepreneur First" '
            'OR "backed by South Park Commons" OR "backed by 500 Global") '
            "-site:linkedin.com -site:crunchbase.com",
            # Not "backed by Pioneer": it finds Pioneer Square Labs and Pioneer
            # Fund (the profile still reads a Pioneer claim on the page).
            '{industry} ("backed by Neo" OR "backed by AI Grant" OR "a16z speedrun" '
            'OR "backed by Plug and Play") '
            "-site:linkedin.com -site:crunchbase.com -site:a16z.com -site:neo.com",
        ),
        "local": False,
    },
    # Companies announcing their own raise, from the last ~18 months. The
    # news sites and databases are excluded here and filtered as junk.
    "funding": {
        "label": "Recently funded",
        "kinds": ("serp",),
        "term": ('{industry} ("we raised" OR "we\'ve raised" OR announces OR raises) '
                 '("seed round" OR "Series A" OR "Series B") after:{since} '
                 "-site:techcrunch.com -site:crunchbase.com -site:pitchbook.com -site:linkedin.com "
                 "-site:prnewswire.com -site:businesswire.com -site:globenewswire.com"),
        "local": False,
    },
}
FUNDING_LOOKBACK_DAYS = 548       # ~18 months: older raises aren't news


def preset_templates(spec: dict) -> tuple[str, ...]:
    """A preset's search templates: one ``term``, or several ``terms``."""
    return tuple(spec.get("terms") or (spec["term"],))

# A hit on careers.acme.com is acme.com's careers page.
_CAREERS_HOST = re.compile(r"^(careers|jobs|join|work|apply|hiring)\.", re.I)
_SECOND_LEVEL_SUFFIX = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"}


def company_host(domain: str) -> str:
    """careers.acme.com → acme.com. Never down to a public suffix: jobs.co.uk
    stays jobs.co.uk (and is junk), not co.uk."""
    stripped = _CAREERS_HOST.sub("", domain)
    labels = stripped.split(".")
    if stripped == domain or len(labels) < 2:
        return domain
    if len(labels) == 2 and labels[0] in _SECOND_LEVEL_SUFFIX and len(labels[1]) == 2:
        return domain
    return stripped


def preset_problem(preset: str, provider_key: str, config=None) -> str | None:
    """Why ``preset`` can't run on ``provider_key`` (with ``config``), or None."""
    if not preset:
        return None
    spec = PRESETS.get(preset)
    if spec is None:
        return f"Unknown preset {preset!r}. Presets: {', '.join(PRESETS)}."
    provider = PROVIDERS.get(provider_key)
    if provider is None or provider.kind not in spec["kinds"]:
        names = ", ".join(k for k, p in PROVIDERS.items() if p.kind in spec["kinds"])
        return (f"The {spec['label']} preset is a web search, so it needs a search "
                f"provider ({names}), not {provider_key}.")
    if any("{role}" in tpl for tpl in preset_templates(spec)) and config is not None:
        from harvey.collectors.ats import role_keywords
        if not role_keywords(getattr(config, "icp", None)):
            return (f"The {spec['label']} preset searches for roles, and harvey.yaml has "
                    "none: set icp.hiring_signals (or icp.titles).")
    return None


def build_queries(config, cities: list[str] | None = None,
                  depth: int | None = None, limit: int | None = None,
                  preset: str = "") -> list[DiscoveryQuery]:
    """Turn the configured ICP into a concrete query list.

    One query per (industry or keyword variant x city). Coordinates come from
    ``icp.geo_coordinates`` when present, because listings providers need a
    radius, not a place name. A ``preset`` (PRESETS) rewrites each industry
    into its search; a non-local one only uses cities you pass. ``depth``
    and ``limit`` left None take every result the source has.
    """
    icp = config.icp
    terms = list(icp.industries) or [config.product.name]
    spec = PRESETS.get(preset) if preset else None
    if spec:
        templates = preset_templates(spec)
        since = (date.today() - timedelta(days=FUNDING_LOOKBACK_DAYS)).isoformat()
        from harvey.collectors.ats import role_keywords
        roles = role_keywords(icp) if any("{role}" in tpl for tpl in templates) else [""]
        terms = [tpl.format(industry=t, role=r, since=since)
                 for r in roles for t in terms for tpl in templates]
        places = cities if cities is not None else ([""] if not spec["local"] else list(icp.geography) or [""])
    else:
        # "roofing contractor" is one way to search for a roofer. Each
        # configured variant is another search in every city — ranks differ
        # by phrasing, and the email should quote the phrase that was used.
        variants = getattr(icp, "keyword_variants", {}) or {}
        terms = list(dict.fromkeys(
            v.strip() for t in terms for v in [t, *variants.get(t, [])] if v.strip()))
        places = cities if cities is not None else list(icp.geography) or [""]
    coords = getattr(icp, "geo_coordinates", {}) or {}
    language = getattr(icp, "search_language", "") or "en"

    return [
        DiscoveryQuery(term=term, location=place, depth=depth, limit=limit,
                       coordinate=coords.get(place, ""), preset=preset if spec else "",
                       language=language)
        for term in terms
        for place in places
    ]


def estimate_cost(provider_key: str, queries: list[DiscoveryQuery]) -> float:
    """Projected spend. Always call this before a run — and print it."""
    provider = PROVIDERS.get(provider_key)
    return provider.estimate(queries) if provider else 0.0


async def count_results(provider_key: str, queries: list[DiscoveryQuery],
                        client: httpx.AsyncClient, env: dict) -> float:
    """Ask the source how many results each query has (``query.total``), so
    the estimate is exact. Returns what the asking cost. A provider that
    can't count leaves the queries as they were."""
    provider = PROVIDERS[provider_key]
    if not provider.can_count:
        return 0.0
    spent = 0.0
    for query in queries:
        total, cost = await provider.count(client, env, query)
        query.total = total
        spent += cost
    return spent


RANK_FIELDS = ("rank", "local_pack_rank", "maps_rank")
RANK_CODES = ("SERP_RANK", "IN_LOCAL_PACK", "MAPS_RANK")      # same order


def _rank_tuple(biz: Business) -> tuple:
    return tuple(getattr(biz, name) for name in RANK_FIELDS)


def business_record(biz: Business, query: DiscoveryQuery | None, key: str = "") -> Record:
    """The company a result describes. The listing's street address isn't
    kept (a search result's is often the map pin's, not the office's)."""
    return Record(
        key=key or biz.identity(), name=biz.name, domain=biz.domain, website=biz.website,
        phone=biz.phone, industry=biz.category or (query.term if query else ""),
        location=biz.location or (query.location if query else ""),
        source=f"discover:{biz.provider}", source_url=biz.source_url,
        external_id=biz.external_id, alt_ids=tuple(biz.alt_ids),
    )


def business_facts(biz: Business, query: DiscoveryQuery | None = None,
                   ranks_only: bool = False, record: str = "") -> list[Fact]:
    """Every fact a result gives about the business, in the order they are
    recorded. Rank facts carry the search phrase in ``value_text``: "#14
    for 'roofer Denver, CO'" is a fact; "#14" on its own isn't.
    ``ranks_only`` is for a business already recorded this run, found again
    by another search."""
    facts: list[Fact] = []

    def add(code, *, num=None, text="", conf=1.0):
        facts.append(Fact(code, value_num=num, value_text=text, confidence=conf,
                          source_url=biz.source_url, record=record, collector="discover"))

    keyword = biz.keyword or (query.keyword() if query is not None else "")
    if biz.rank is not None:
        add("SERP_RANK", num=float(biz.rank), text=keyword)
    if biz.local_pack_rank is not None:
        add("IN_LOCAL_PACK", num=float(biz.local_pack_rank), text=keyword)
    if biz.maps_rank is not None:
        add("MAPS_RANK", num=float(biz.maps_rank), text=keyword)
    if ranks_only:
        return facts

    add("FOUND_IN_SERP", text=biz.provider)
    # Both ways when the listing says: a cohort that excludes "no website"
    # must tell "the listing names a site" from "never looked".
    if biz.has_website is not None:
        add("NO_WEBSITE", num=0.0 if biz.has_website else 1.0)
    if biz.is_claimed is not None:
        add("UNCLAIMED_LISTING", num=0.0 if biz.is_claimed else 1.0)
    if biz.rating is not None:
        add("REVIEW_RATING", num=float(biz.rating))
    if biz.review_count is not None:
        add("REVIEW_COUNT", num=float(biz.review_count))
    return facts


def preset_facts(query: DiscoveryQuery | None, source_url: str, record: str = "") -> list[Fact]:
    """What a preset search's hit (found at ``source_url``) says beyond the
    search itself."""
    facts: list[Fact] = []
    preset = query.preset if query is not None else ""
    if preset == "yc":
        # The search matched "backed by Y Combinator" on a page of their own
        # site (third-party sites are filtered out above). Short of reading
        # that page ourselves, so below the homepage's own claim.
        facts.append(Fact("YC_BACKED", value_num=1.0, confidence=0.7,
                          source_url=source_url, record=record, collector="discover"))
    if preset == "accelerator":
        # Same, for the other programs; which one isn't known until the
        # profile reads their pages. A funding hit isn't recorded: the
        # raise's date matters, so the profile reads the hit page itself.
        facts.append(Fact("ACCELERATOR", value_num=1.0, confidence=0.6,
                          source_url=source_url, record=record, collector="discover"))
    return facts


def _observations(biz: Business, company_id: str, confirmed: set[str],
                  run_id: str, query: DiscoveryQuery | None = None,
                  ranks_only: bool = False) -> list[dict]:
    """Every fact this business gave us, as observation rows, and only the
    confirmed ones (what a run writes for its first sighting)."""
    facts = business_facts(biz, query, ranks_only)
    if not ranks_only:
        facts += preset_facts(query, biz.source_url)
    return [{"company_id": company_id, "signal_code": f.code, "collector": f.collector,
             "value_num": f.value_num, "value_text": f.value_text, "confidence": f.confidence,
             "evidence_url": f.source_url, "run_id": run_id}
            for f in facts if f.code in confirmed]


async def search_draft(provider_key: str, query: DiscoveryQuery, client: httpx.AsyncClient,
                       env: dict, remaining: float = math.inf, host=None,
                       paused=None, cursor: dict | None = None, on_page=None) -> Draft:
    """One search through one provider, as a Draft: a record per result
    (junk dropped and counted in ``counts``, each keyed by its position),
    the facts each result gives, and what the search really cost.
    ``host`` rewrites a result's domain before the junk check (a preset's
    careers.acme.com is acme.com). A failure is the draft's ``failed``,
    with ``stop`` set when the provider can take no more this run.
    ``paused`` (async, true when the kill switch is on) is read between
    pages; ``counts["available"]`` is what the source said it holds, and
    ``counts["unfetched"]`` / ``values["unfetched_cost"]`` what a stop
    left behind. ``cursor`` resumes a search a stop left part-way (from
    the page it reached); ``values["cursor"]`` is where this one stopped
    (None when it went to the end), ``counts["pages"]`` how many pages it
    fetched, and ``on_page(pages, results, cost)`` is awaited after each
    page. A failure worth trying again (the network, a 5xx, a 429) sets
    ``values["transient"]``: on the first page the draft fails, on a later
    one it keeps the earlier pages and stops PART_WAY with its cursor. A
    provider that can take more later says when (``values["retry_after"]``,
    seconds)."""
    provider = PROVIDERS[provider_key]
    draft = Draft()
    provider.last_cost = 0.0
    provider.last_error = provider.stop_reason = ""
    provider.complete = True
    provider.remaining_budget = remaining
    provider.paused = paused
    provider.available = provider.unfetched = None
    provider.unfetched_cost = 0.0
    provider.cursor = dict(cursor) if cursor else None
    provider.next_cursor = None
    provider.on_page = on_page
    provider.pages = 0
    try:
        found = await provider.fetch(client, env, query)
    except ProviderExhausted as e:
        logger.warning("discover: %s stopped: %s", provider_key, e)
        draft.failed = draft.stop = str(e)
        draft.cost = provider.last_cost
        draft.counts["pages"] = provider.pages
        if e.retry_after:
            draft.values["retry_after"] = e.retry_after
        return draft
    except Exception as e:
        # A failure is an observation about the run, not a reason to lose
        # the queries that already succeeded.
        logger.warning("discover: %s failed on %r: %s", provider_key, query.keyword(), e)
        draft.failed = str(e)
        draft.cost = provider.last_cost
        draft.counts["pages"] = provider.pages
        from harvey.transient import is_transient
        if is_transient(e):
            draft.values["transient"] = True
        return draft
    finally:
        provider.cursor = provider.on_page = None
    draft.cost = provider.last_cost
    draft.counts["pages"] = provider.pages
    if provider.last_error:
        draft.errors.append(provider.last_error)
    draft.stop = provider.stop_reason
    if draft.stop == PART_WAY:
        draft.values["transient"] = True
    if provider.next_cursor:
        draft.values["cursor"] = provider.next_cursor
    if provider.available is not None:
        draft.counts["available"] = provider.available
    if provider.unfetched:
        draft.counts["unfetched"] = provider.unfetched
    if provider.unfetched_cost:
        draft.values["unfetched_cost"] = provider.unfetched_cost
    # A search resumed part-way didn't see its first pages this time, so it
    # can't say a business is no longer ranked.
    draft.complete_codes = (tuple(provider.rank_codes)
                            if provider.complete and not cursor else ())
    draft.counts["found"] = len(found)
    for i, biz in enumerate(found):
        if host is not None and biz.domain and not is_junk(biz.domain):
            # Judged on the host as found first: jobs.lever.co stripped
            # would be lever.co, a vendor, not junk.
            biz.domain = host(biz.domain)
        if is_junk(biz.domain, biz.raw_title or biz.name):
            draft.counts["junk"] = draft.counts.get("junk", 0) + 1
            continue
        key = str(i)
        record = business_record(biz, query, key)
        record.found_by = biz.keyword or query.keyword()
        draft.records.append(record)
        draft.facts += business_facts(biz, query, record=key)
    return draft


async def _count_uses(provider_key: str, requests: int) -> None:
    """Count a search's requests against its module's free allowances
    (lanes.count_uses), whoever ran it, so the background knows what is
    left. Never fails a search."""
    if not requests:
        return
    try:
        from harvey.modules import lanes
        from harvey.modules.switches import capability_owner

        module = capability_owner("provider", provider_key)
        if module and lanes.free_allowances(module):
            await asyncio.to_thread(lanes.count_uses, module, requests)
    except Exception as e:
        logger.debug("discover: %s requests not counted: %s", provider_key, e)


def is_discovery_source(provider_key: str) -> bool:
    """Can Discover run ``provider_key`` to find companies? Only a
    provider whose module claims it with a query capability: one claimed
    by a lookup capability (a search whose terms forbid building a list of
    its results) only finds a known company's website."""
    from harvey.modules import switches

    for m in switches.manifests():
        for cap in m.capabilities:
            if cap.get("provider") == provider_key:
                return cap.get("op") == "query"
    return False


def _finds_websites(provider_key: str) -> bool:
    from harvey.modules import switches

    return any(cap.get("provider") == provider_key and cap.get("lookup") == "website"
               for m in switches.manifests() for cap in m.capabilities)


# ── Capabilities (the search modules' and the presets' code) ──


def provider_capabilities(module_id: str, caps: dict[str, str]) -> dict:
    """A search module's capabilities: capability id -> provider key. Each
    takes a DiscoveryQuery (``input.query``) and searches once."""
    from harvey.modules.api import Capability

    def make(cap_id: str, key: str) -> Capability:
        async def run(inp, ctx) -> Draft:
            draft = await search_draft(key, inp.query, ctx.http, ctx.env,
                                       remaining=ctx.budget.remaining,
                                       paused=ctx.settings.get("paused"),
                                       cursor=ctx.settings.get("cursor"),
                                       on_page=ctx.settings.get("on_page"))
            return draft.stamp(module_id, cap_id)

        return Capability(module_id, cap_id, run,
                          estimate=lambda inp: PROVIDERS[key].estimate([inp.query]))

    return {cap_id: make(cap_id, key) for cap_id, key in caps.items()}


def preset_capabilities(module_id: str, caps: dict[str, str]) -> dict:
    """A preset's capabilities: capability id -> preset key. Each runs the
    preset's search through the search module the run chose
    (``settings["provider"]``, its capability ``settings["searcher"]`` as
    (module, capability)), and adds what a hit says beyond the search."""
    from harvey.modules.api import Capability

    def make(cap_id: str, preset: str) -> Capability:
        async def run(inp, ctx) -> Draft:
            key = ctx.settings["provider"]
            draft = await search_draft(key, inp.query, ctx.http, ctx.env,
                                       remaining=ctx.budget.remaining,
                                       host=company_host if preset == "hiring" else None,
                                       paused=ctx.settings.get("paused"),
                                       cursor=ctx.settings.get("cursor"),
                                       on_page=ctx.settings.get("on_page"))
            draft.stamp(*ctx.settings["searcher"])
            extra = [(r.key, f) for r in draft.records
                     for f in preset_facts(inp.query, r.source_url, r.key)]
            # Each hit's own facts stay together, the preset's after the search's.
            order = {r.key: i for i, r in enumerate(draft.records)}
            facts = [(f.record, f) for f in draft.facts] + extra
            facts.sort(key=lambda kf: order.get(kf[0], -1))
            draft.facts = [f for _, f in facts]
            return draft.stamp(module_id, cap_id)

        return Capability(module_id, cap_id, run, estimate=preset_estimate)

    return {cap_id: make(cap_id, preset) for cap_id, preset in caps.items()}


def preset_estimate(inp) -> float:
    """A preset search costs what the search module it runs through charges
    (``inp.extra["provider"]``); not told which, the dearest one, so an
    estimate never understates."""
    provider = PROVIDERS.get(inp.extra.get("provider", ""))
    if provider is not None:
        return provider.estimate([inp.query])
    return max((p.estimate([inp.query]) for p in PROVIDERS.values()), default=0.0)


def provider_menu(env: dict | None = None, enabled: set[str] | None = None) -> list[dict]:
    """The menu the dashboard renders: what each provider does, and its cost,
    with the module it belongs to and whether that module is on (all on
    when ``enabled`` is None).

    The CLI calls this bare; without a load, every keyed provider would show
    "needs setup" even with a working key in .env.
    """
    from harvey.modules.switches import capability_owner

    if env is None:
        from harvey.config import load_env
        env = load_env().model_dump()
    return [
        {
            "key": p.key, "label": p.label, "kind": p.kind, "blurb": p.blurb,
            "cost_note": p.cost_note, "free_tier": p.free_tier,
            "signup_url": p.signup_url, "caveat": p.caveat,
            "env_keys": [k.upper() for k in p.env_keys],
            "configured": p.configured(env),
            "needs_key": bool(p.env_keys),
            "module": capability_owner("provider", p.key) or "",
            "enabled": enabled is None or capability_owner("provider", p.key) in enabled,
            # Finds a known company's website only: never a Discover source.
            "lookup_only": not is_discovery_source(p.key),
            # Finds a company on file by its name (the contact finder's lookup).
            "website_lookup": _finds_websites(p.key),
        }
        for p in PROVIDERS.values()
    ]

