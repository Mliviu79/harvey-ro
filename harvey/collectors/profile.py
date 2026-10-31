"""PROFILE collector — what a business is, from three HTTP requests.

Cost: $0. No browser, no model. Homepage + robots.txt + sitemap.xml (+ the
team and careers pages when present, and the company's public job-board feed
when its site links one), then regex over HTML we already hold.

This is the highest value-per-effort stage in the pipeline: it produces the
signals that actually make an email specific — who their incumbent agency is,
whether they're spending on ads today, what's missing from their site.
"""

import logging
from html import unescape as html_unescape
import random
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx

from harvey.browser import HTTP, PageReader
from harvey.modules.api import Contact, Draft, Fact, Page
from harvey.public_http import is_public_client, public_http_client
from harvey.startups import funding_text

from harvey.collectors.ats import detect_board, matching_roles, read_board, role_keywords
from harvey.collectors.phones import MAX_PHONES, phone_region, phones_from_html

from harvey.collectors.discover import (
    name_from_domain, name_matches_domain, site_name_from_html,
)

logger = logging.getLogger("harvey.collectors.profile")

TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# ── Agency detection ──────────────────────────────────────────────────
# Links that appear in footers but are never the site's agency. Matched as
# substrings against the link host.
BENIGN_HOSTS = (
    # social
    "facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "youtube.",
    "tiktok.", "pinterest.", "yelp.", "nextdoor.", "threads.",
    # maps / profiles
    "google.com/maps", "g.page", "maps.app.goo.gl", "goo.gl", "wa.me",
    "bing.com/maps", "apple.com/maps",
    # financing / payments / reviews
    "greensky", "wellsfargo", "synchrony", "affirm", "klarna", "paypal",
    "stripe.com", "bbb.org", "angi.com", "homeadvisor", "houzz.",
    "trustpilot", "birdeye", "podium.com",
    # accessibility / compliance widgets
    "accessibe", "userway", "audioeye", "termly", "iubenda", "onetrust",
    # infra / CDNs / platforms
    "cloudflare", "godaddy", "wordpress.org", "wix.com", "squarespace.com",
    "shopify.com", "webflow.com", "duda.co", "gtranslate", "jquery",
    "gstatic", "googleapis", "cloudfront", "akamai", "w3.org", "schema.org",
    # industry bodies / certs
    "gaf.com", "owenscorning", "certainteed", "nrca.net", "iko.com",
    "energystar.gov", "osha.gov", "nahb.org",
)

# Credit wording → confidence. Only >= 0.75 is recorded: a wrong incumbent in
# an email is worse than saying nothing at all.
STRONG_CREDIT = re.compile(
    r"\b(powered|website|site|web\s*design|designed|developed|built|created|"
    r"marketing)\s+(by|&)\b", re.I)
DESCRIPTIVE_CREDIT = re.compile(
    r"\b(web\s*design|website\s*design|digital\s*marketing|seo|marketing\s*"
    r"agency|web\s*development)\b", re.I)
RECORD_FLOOR = 0.75

# ── Tech / platform fingerprints (name -> regexes over raw HTML) ──
TECH_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "WordPress": (r"wp-content/", r"wp-includes/"),
    "Shopify": (r"cdn\.shopify\.com", r"myshopify\.com"),
    "Wix": (r"static\.parastorage\.com",),
    "Squarespace": (r"static1\.squarespace\.com",),
    "Webflow": (r"assets\.website-files\.com", r"data-wf-domain"),
    "Duda": (r"irp\.cdn-website\.com", r"d1\.awsstatic"),
    "GoDaddy Website Builder": (r"img1\.wsimg\.com",),
    "HubSpot": (r"js\.hs-scripts\.com", r"js\.hsforms\.net"),
    "Google Analytics": (r"googletagmanager\.com/gtag", r"google-analytics\.com"),
    "Google Tag Manager": (r"googletagmanager\.com/gtm\.js",),
    "Intercom": (r"widget\.intercom\.io",),
    "Drift": (r"js\.driftt\.com",),
    "Podium": (r"connect\.podium\.com",),
    "Birdeye": (r"birdeye\.com",),
    "Calendly": (r"assets\.calendly\.com",),
    "ServiceTitan": (r"servicetitan",),
    "Jobber": (r"getjobber\.com",),
    "Housecall Pro": (r"housecallpro",),
    "CallRail": (r"cdn\.callrail\.com",),
    "Stripe": (r"js\.stripe\.com",),
}

GOOGLE_ADS = (r"googleadservices\.com", r"gtag/js\?id=AW-", r"google_conversion",
              r"googletagmanager\.com/gtag/js\?id=AW-")
META_PIXEL = (r"connect\.facebook\.net/[^\"']*fbevents\.js", r"fbq\s*\(\s*['\"]init")

# Between Romanian words in raw HTML: a space, &nbsp;, or a URL's - or _.
_RO_GAP = r"(?:[-_\s]|&nbsp;|&#160;)"
BOOKING_HINTS = (r"calendly\.com", r"acuityscheduling", r"schedulicity",
                 r"squareup\.com/appointments", r"setmore", r"book(ing)?[-_]?now",
                 r"schedule[-_]?(an?[-_])?(appointment|estimate|consultation)",
                 r"housecallpro", r"getjobber\.com", r"servicetitan",
                 # Romanian: "programare(a) online", "programări online",
                 # "programează-te (acum) online", "programează o consultație online",
                 # "rezervă online". Each needs "online": "Programează-te telefonic"
                 # and "Fă o programare la 0712 000 000" are phone booking.
                 # A negation ("nu facem programări online") still matches.
                 rf"program[aă]r(?:[ei]|ea){_RO_GAP}?online",
                 rf"programeaz[aă]{_RO_GAP}?te{_RO_GAP}+(?:acum{_RO_GAP}+)?online",
                 rf"programeaz[aă]{_RO_GAP}o{_RO_GAP}consulta[tțţ]ie{_RO_GAP}+online",
                 rf"rezerv[aă]{_RO_GAP}?online")

AI_CRAWLERS = ("gptbot", "google-extended", "ccbot", "anthropic-ai", "claudebot",
               "perplexitybot", "bytespider")

# The pages where a site names its people and says how to reach it are
# found by following the site's own menu (people.menu_links: team,
# meet-the-doctors, about, contact), at most MAX_SITE_PAGES pages counting
# the homepage and MAX_SITE_FETCHES requests counting the ones that found
# the homepage. Only a homepage whose menu links to fewer than two of them
# (a JavaScript menu, a one-page site) has these common paths tried, and a
# guessed page counts only when it is a real page (over 500 characters).
MAX_SITE_PAGES = 6
MAX_SITE_FETCHES = 10
FALLBACK_SITE_PATHS = ("/about", "/about-us", "/team", "/our-team", "/contact", "/contact-us")
# A .ro site's pages are named in Romanian: tried first on .ro domains only.
RO_FALLBACK_SITE_PATHS = ("/echipa", "/despre-noi", "/medici", "/contact")
CAREERS_PATHS = ("/careers", "/jobs", "/join-us", "/employment", "/hiring",
                 "/about/careers", "/careers/")
# A homepage link to the careers page beats guessing paths: careers.acme.com
# and /company/jobs are as common as /careers. Whole path segments and whole
# anchors only: "/scheduling/hiring-marketplaces" is a product page.
_CAREERS_HOST = re.compile(r"^(careers?|jobs|join)\.", re.I)
_CAREERS_SEGMENTS = frozenset("""careers career jobs join join-us joinus work-with-us
open-roles open-positions hiring""".split())
_CAREERS_ANCHOR = re.compile(r"^(careers?|jobs|join us|join (our|the) team|we['’]?re hiring"
                             r"|open (roles|positions)|work with us)$", re.I)
# Roles recorded per company from a board. The first few say it; twenty rows
# of "Account Executive, <city>" don't say more.
MAX_BOARD_ROLES = 5
# The signals a read of the team and about pages answers.
PEOPLE_CODES = ("CONTACT_FOUND", "DECISION_MAKER_TITLE", "LIKELY_OWNER")
# People kept per company from its site, and owners recorded as LIKELY_OWNER.
MAX_TEAM = 12
MAX_OWNERS = 3
_CONTACT_URL = re.compile(r"(?i)contact")


def _variant_number(site: str, domain: str) -> int:
    """How many requests found the homepage at ``site`` (the scheme and www
    variants _resolve_site tries in turn)."""
    variants = [f"https://{domain}", f"https://www.{domain}", f"http://{domain}",
                f"http://www.{domain}"]
    return variants.index(site) + 1 if site in variants else len(variants)


CONTACT_PATHS = ("/contact", "/contact-us", "/get-a-quote", "/free-estimate",
                 "/request-estimate", "/quote")

# Words that are never part of a person's name. Without this, headings like
# "Request Service", "Baker History" and "Commercial Services" pass a
# Firstname-Lastname regex and become contacts — the single most common
# failure mode when scraping team pages.
NON_NAME_WORDS = frozenset("""
service services repair repairs roofing roof roofs contractor contracting
commercial residential industrial storm damage restoration installation
history about contact quote quotes estimate estimates free request get call
learn more read view our their your team staff careers jobs home page site
company companies inc llc ltd corp group solutions systems partners
gallery projects reviews testimonials financing warranty insurance claims
maintenance inspection replacement gutters siding windows doors metal shingle
shingles tile flat emergency 24 hour hours today now new best top quality
privacy policy terms conditions sitemap blog news events why choose us
areas served locations location city county state north south east west
español espanol menu skip navigation search close open toggle
hoa hoas condo condos association associations board boards community
communities management property properties owners residents customer care
scope alignment implementation consistency communication process driven
focused structured
""".split())

# Tokens that are words, not names, whatever the vertical: abstract nouns
# ("Scope Alignment", "Board Communication") and shouted acronyms ("HOA").
_NOUN_SUFFIX = re.compile(r"(tion|tions|sion|sions|ness)$")


def _is_name_token(token: str) -> bool:
    for part in token.split("-"):
        bare = part.strip(".,'")
        low = bare.lower()
        if low in NON_NAME_WORDS or _NOUN_SUFFIX.search(low):
            return False
        if len(bare) >= 3 and bare.isupper():
            return False
    return True


# Block-level tags separate a name, its title, and the bio that follows; the
# first text chunk after the name heading is the title, nothing further.
_BLOCK_BREAK = re.compile(r"(?i)<(?:/?(?:p|div|li|ul|ol|section|article|td|tr)\b[^>]*|br\s*/?)>")
_CONNECTORS = frozenset("of and the for & to in at de în și".split())


def _title_chunks(raw_after: str) -> list[str]:
    raw_after = re.sub(r"<[^>]*$", "", raw_after)   # a tag cut off by the window
    chunks = []
    for piece in _BLOCK_BREAK.split(raw_after):
        text = html_unescape(re.sub(r"(?s)<[^>]+>", " ", piece))
        text = re.sub(r"\s+", " ", text).strip(" ,.|-–—·")
        if text:
            chunks.append(text)
    return chunks


# Romanian titles are sentence case: "Medic primar medicină de familie" is a
# title, not copy, when it opens with a role word. "Director" is left out: it
# is English too, and "Director with over twenty years..." is a sentence.
_RO_TITLE_START = re.compile(
    r"(?i)^(?:medic|fondator|fondatoare|farmacist[ăa]?|stomatolog|coordonator|"
    r"coordonatoare|asistent[ăa]?|kinetoterapeut[ăa]?|proprietar[ăa]?|psiholog)\b")


def _reads_like_title(text: str) -> bool:
    """A job title is short and mostly capitalised; a sentence is neither.

    "Desiring consistent oversight and a trusted management partner" contains
    the role word "partner" but is marketing copy, not a title.
    """
    words = text.split()
    if not words or len(words) > 10:
        return False
    lower = [w for w in words[1:] if w[:1].islower() and w.lower() not in _CONNECTORS]
    limit = 6 if _RO_TITLE_START.match(text) else 2
    return len(lower) <= limit and title_looks_like_role(text)

DECISION_MAKER_TITLES = (
    "owner", "founder", "co-founder", "president", "ceo", "principal",
    "partner", "general manager", "operations manager", "practice manager",
    "office manager", "marketing director", "marketing manager",
    "director of marketing", "vice president", "vp ", "managing director",
    "chief", "director",
    # Romanian ("director medical" is covered by "director")
    "fondator", "fondatoare", "proprietar", "medic coordonator", "medic coordonatoare",
)


ROLE_HINT = re.compile(
    r"\b(owner|founder|president|ceo|cfo|coo|cto|principal|partner|manager|"
    r"director|supervisor|superintendent|foreman|estimator|specialist|"
    r"consultant|coordinator|representative|rep|sales|marketing|operations|"
    r"office|project|account|service|technician|installer|inspector|"
    r"vice president|vp|executive|lead|head of|chief|proprietor|attorney|lawyer|"
    r"dentist|orthodontist|periodontist|endodontist|hygienist|dds|dmd|esq|cpa|"
    r"administrator|paralegal|associate|co-?owner|co-?founder|"
    # Romanian; [ăa] takes the feminine form with or without its accent
    r"medic|stomatolog|ortodont|farmacist[ăa]?|fondator|fondatoare|proprietar[ăa]?|"
    r"asistent[ăa]?|kinetoterapeut[ăa]?|fizioterapeut[ăa]?|psiholog|coordonator|"
    r"coordonatoare)\b", re.I)


def title_looks_like_role(text: str) -> bool:
    """True when the text following a name reads like a job title.

    A name with no role nearby is usually a nav link or a page heading, not
    a person — requiring a role is the cheapest high-precision filter.
    """
    return bool(ROLE_HINT.search(text[:160]))


def _strip_code(html: str) -> str:
    """Remove script/style/comments BEFORE any text regex.

    Skipping this is how you end up with contacts named 'return value instan'
    whose title is CEO — minified JS read as prose.
    """
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return html


# Signals that a response is a block / bot-challenge rather than real content.
# Strong markers only ever appear on a challenge page. Weak ones ("captcha")
# also appear on real pages that load reCAPTCHA for a contact form, so they
# count only in the visible text of a short page.
_BLOCK_MARKERS = (
    "unusual traffic",
    "detected unusual activity",
    "/sorry/index",
    "/.well-known/sgcaptcha",       # SiteGround's bot challenge (served as a 202)
    "cf-chl-", "cf_chl_opt",        # Cloudflare's interstitial
    "px-captcha",                   # PerimeterX / HUMAN
    "_incapsula_resource", "incapsula incident id",
    "sucuri website firewall",
)
_BLOCK_TITLES = (
    "just a moment", "attention required", "access denied",
    "access to this page has been denied", "are you a robot", "security check",
    "verify you are human", "human verification", "bot verification",
    "ddos protection", "ddos-guard", "one more step",
)
_WEAK_BLOCK_MARKERS = (
    "captcha", "are you a robot", "verify you are human", "access denied",
    "enable javascript and cookies to continue",
)



def looks_like_challenge(text: str, status: int = 200) -> bool:
    """A bot challenge or block page — not a real page that happens to load
    reCAPTCHA for its contact form (which used to read as "blocked" and
    cost Harvey the whole site). Ordinary words ("access denied", "protected
    by reCAPTCHA") only count on a short page served as an error or 202."""
    if not text:
        return False
    head = text[:6000]
    low = head.lower()
    if any(marker in low for marker in _BLOCK_MARKERS):
        return True
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", head)
    if title and any(t in title.group(1).strip().lower() for t in _BLOCK_TITLES):
        return True
    if status in (202, 403, 429, 503) and len(text) < 15000:
        visible = re.sub(r"(?s)<[^>]+>", " ", _strip_code(text)).lower()
        return any(marker in visible for marker in _WEAK_BLOCK_MARKERS)
    return False


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", _strip_code(html)))


# ── YC_BACKED: what a company says about itself on its own site ──
# Never read from ycombinator.com or YC's other sites, whose terms forbid
# scraping and collecting founders' contacts. What counts is the company's
# own claim: a reference to its own YC page, or a sentence about itself
# ("we", "our", its name) or a badge-short line ("YC W24", "Backed by Y
# Combinator"). Lines about customers and audiences ("trusted by YC-backed
# startups", "for YC founders") and YC programs (Startup School, Demo Day,
# applying) are not.
YC_OWNED_DOMAINS = ("ycombinator.com", "workatastartup.com", "startupschool.org")
_YC = r"(?:YC|Y[\s-]?Combinator)"
_YC_LINK_RE = re.compile(r"ycombinator\.com\\?/companies\\?/([a-z0-9][\w-]*)", re.I)
YC_ABOUT_PATHS = ("/about", "/about-us", "/company", "/our-story")
FUNDING_PATHS = ("/press", "/news", "/newsroom", "/blog")
_YC_SEASONS = {"w": "W", "winter": "W", "s": "S", "summer": "S",
               "f": "F", "fall": "F", "x": "X", "spring": "X"}
_SEASON_WORD = r"(Winter|Summer|Fall|Spring)"
# A batch right next to YC: "YC W24", "(YC S23)", "Y Combinator's Summer 2023
# batch", "Y Combinator alumni (W19)", "the Summer 2023 batch of Y Combinator".
_YC_BATCH_RES = (
    re.compile(rf"\b{_YC}(?:['’]s)?\s*(?:(?:alumni|alums?|company|startup)\s*)?[\(\[]?\s*"
               r"(W|S|F|X)\s*'?(\d{2})\b(?![\w-]*\s*(?:Ultra|Plus|Pro|FE|Max)\b)", re.I),
    re.compile(rf"\b{_YC}(?:['’]s)?\s+{_SEASON_WORD}\s+(?:batch\s+)?(\d{{4}})\b", re.I),
    re.compile(rf"\b{_SEASON_WORD}\s+(\d{{4}})\s+batch\s+(?:of|at)\s+{_YC}\b", re.I),
    re.compile(rf"\b{_YC}\s+(?:in|during)\s+(?:the\s+)?{_SEASON_WORD}\s+(?:of\s+)?(\d{{4}})\b", re.I),
    re.compile(rf"\b{_YC}\s+demo day\s*[\(\[]\s*(W|S|F|X)\s*'?(\d{{2}})\b", re.I),
)
# What a claim looks like. Anchored at the start of a sentence, so the
# company is the one being described, not a customer, a team member or an
# audience ("trusted by YC-backed startups", "we sponsored the YC dinner").
# The same shapes serve every accelerator (ACCELERATORS below): only the
# program's name, its cohort code and the nouns that follow "a <program>"
# change.
_BATCH = r"(?:W|S|F|X)\s*'?\d{2}"
_FILLER = r"(?:\s+(?:proudly|also|now|officially|recently|originally|first|both|all|so|were|was|are|is|been|have|has|had|very))*"
_SELF_SUBJECT = (r"(?:we|we're|we are|we were|we've been|proud(?:ly)? to be|excited to be|honou?red to be"
                 r"|the company|our company)")
_FOUNDED_PREFIX = r"(?:founded in \d{4},?\s+)?"


class _ProgramClaims:
    """The claim patterns for one program. ``prog`` is a regex for its name,
    ``batch`` for a cohort code written right after it, ``nouns`` what may
    follow "a <program>" ("a YC company"), ``programs`` its own events and
    pages that are mentions, not backing (Startup School, Demo Day)."""

    def __init__(self, prog: str, batch: str, nouns: str, programs: str, extra: str = "",
                 strict: bool = False):
        self.mention = re.compile(rf"\b{prog}\b", re.I)
        self.extra = extra          # more shapes after the subject (accelerators only)
        self.strict = strict        # an event is never backing, even with a year ("Startup Weekend 2019")
        # a whole line that is a badge: backing words or a batch, not a bare name
        self.badge = re.compile(
            rf"^\(?\s*(?:(?:backed|funded|supported)\s+by\s+{prog}(?:\s*[\(\[]?\s*{batch}\s*[\)\]]?)?"
            rf"|{prog}[\s-]+(?:backed|funded)(?:\s*[\(\[]?\s*{batch}\s*[\)\]]?)?"
            rf"|{prog}\s*[\(\[]?\s*{batch}\s*[\)\]]?)\s*\)?\.?$", re.I)
        self.body = (
            rf"{_FILLER}\s+(?:backed by|funded by|supported by|part of|a member of|members of|graduated from|"
            rf"alumni of|alums? of|went through|joined|hatched (?:in|at)|incubated (?:in|at|by)|accepted into|"
            rf"participated in|launched at|in)\s+(?:the\s+)?{prog}\b"
            rf"|{_FILLER}\s+in\s+the\s+{_SEASON_WORD}\s+\d{{4}}\s+batch\s+of\s+{prog}\b"
            rf"|{_FILLER}\s+(?:a|an)\s+{prog}(?:\s+{batch})?[\s-]+(?:(?:backed|funded)\s+)?"
            rf"(?:{nouns})\b"
            rf"|{_FILLER}\s+{prog}[\s-]+(?:backed|funded|alumni|alums?|alumnus)\b"
            rf"|\s*[\(\[]\s*{prog}\s*{batch}\s*[\)\]]"
        )
        # "in YC" only after a form of be ("we were in YC"), not "believe in YC".
        self.in_needs_be = re.compile(
            rf"(?<!\bwere)(?<!\bwas)(?<!\bare)(?<!\bis)(?<!\bbeen)\s+in\s+(?:the\s+)?{prog}\b", re.I)
        # "<subject> ... and is backed by YC", but not "...companies that are backed by YC".
        self.later = (rf"[^.]{{0,80}}?(?<!\bthat)(?<!\bwhich)(?<!\bwho)\s+(?:is|are|was|were|has been|have been)"
                      rf"\s+(?:proudly\s+)?(?:backed|funded|supported)\s+by\s+{prog}\b")
        self.investors = re.compile(rf"^(?:our\s+)?investors?\s*(?:include|including|are|:)[^.]{{0,80}}\b{prog}\b", re.I)
        self.leading = re.compile(rf"^(?:backed|funded|supported)\s+by\s+{prog}\b", re.I)
        # One of its programs right after the mention, with no batch: not backing.
        self.programs = re.compile(rf"\b{prog}(?:['’]s)?\s+(?:{programs})\b", re.I)

    def claims(self, html: str, domain: str, cohort) -> list:
        """The cohort found in each sentence that claims the program (None
        when that sentence names none), in page order. ``cohort(sentence)``
        finds the cohort."""
        name = _domain_name(domain) if domain else ""
        # The name as written on the page may have spaces or hyphens ("Acme Corp"
        # for acmecorp.com): any separators between its letters.
        spelled = r"[\s.-]?".join(re.escape(ch) for ch in _alnum(name)) if len(_alnum(name)) >= 3 else ""
        subject = rf"(?:{_SELF_SUBJECT}" + (rf"|{spelled}\b" if spelled else "") + r")"
        claim_re = re.compile(rf"^{_FOUNDED_PREFIX}{subject},?(?:{self.body}|{self.later}{self.extra})", re.I)
        found_in = []
        for sentence in _lines(html):
            if not self.mention.search(sentence):
                continue
            found = cohort(sentence)
            match = claim_re.search(sentence)
            if match and self.in_needs_be.search(match.group(0)) and not re.search(
                    r"\bin\s+the\s+\w+\s+\d{4}\s+batch|hatched in|incubated in|participated in", match.group(0), re.I):
                match = None
            ok = bool(self.badge.match(sentence) or match or self.investors.search(sentence)
                      or self.leading.search(sentence))
            if ok and (not found or self.strict) and self.programs.search(sentence):
                ok = False      # Startup School, Demo Day, a YC blog: a program, not backing
            if ok:
                found_in.append(found)
        return found_in


_YC_CLAIMS = _ProgramClaims(
    _YC, _BATCH, "company|startup|platform|product|business|team|tool",
    "startup school|demo day|application|interview|events?|podcast|blog|news|jobs|deals?|perks?")
# The YC shapes by name, as before the generalisation.
_BADGE_RE, _CLAIM_BODY, _IN_NEEDS_BE = _YC_CLAIMS.badge, _YC_CLAIMS.body, _YC_CLAIMS.in_needs_be
_LATER_BACKING, _INVESTORS_RE = _YC_CLAIMS.later, _YC_CLAIMS.investors
_LEADING_BACKING_RE, _YC_PROGRAM_RE = _YC_CLAIMS.leading, _YC_CLAIMS.programs
_ATTR_TEXT_RE = re.compile(r"""\b(?:alt|title|aria-label)=["']([^"']{0,200})["']""", re.I)
_BLOCK_TAG_RE = re.compile(r"(?i)</?(?:p|div|h[1-6]|li|ul|ol|br|section|footer|header|nav|article|td|tr|table|blockquote|figcaption|dt|dd)\b[^>]*>")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\s*[|•·]\s*|\n")
_PREFIXES = re.compile(r"^(?:get|try|use|join)(?=\w{4})")
_SUFFIXES = re.compile(r"(?<=\w{4})hq$")
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _lines(html: str) -> list[str]:
    """Readable sentences, with block boundaries kept (a badge isn't run into
    the next paragraph) and entities decoded ("Y Combinator&#x27;s W20")."""
    text = _BLOCK_TAG_RE.sub("\n", _strip_code(html))
    text = html_unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text += "\n" + "\n".join(html_unescape(a) for a in _ATTR_TEXT_RE.findall(html))
    out = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        out.extend(p.strip() for p in _SENTENCE_RE.split(line) if p.strip())
    return out


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _domain_name(domain: str) -> str:
    """The registrable name: app.acme.com and acme.co.uk are both "acme"."""
    parts = [p for p in domain.lower().strip(".").split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _is_their_yc_page(slug: str, domain: str) -> bool:
    """A YC page link is theirs when its name matches their domain's: a
    customer logo can link to that customer's YC page."""
    name, label = _alnum(slug), _alnum(_domain_name(domain)) if domain else ""
    if not name or not label:
        return False
    if name == label:
        return True
    # getacme.com / acmehq.com and the slug "acme", nothing looser.
    core = lambda t: _SUFFIXES.sub("", _PREFIXES.sub("", t))
    return min(len(core(name)), len(core(label))) >= 4 and core(name) == core(label)


def _yc_batch(match) -> str:
    season, year = _YC_SEASONS[match.group(1).lower()], match.group(2)[-2:]
    return f"{season}{year}" if 5 <= int(year) <= 40 else ""


def yc_backed(html: str, domain: str = "") -> tuple[str, float] | None:
    """(batch like "W24" or "", confidence) when the page says the company
    itself is YC-backed, else None."""
    if not html:
        return None
    link = any(_is_their_yc_page(m.group(1), domain) for m in _YC_LINK_RE.finditer(html))
    found_in = _YC_CLAIMS.claims(
        html, domain, lambda s: next((m for r in _YC_BATCH_RES for m in [r.search(s)] if m), None))
    batch = ""
    for found in found_in:
        if found and not batch:
            batch = _yc_batch(found)
    if link:
        return batch, 0.95
    if found_in:
        return batch, 0.9 if batch else 0.85
    return None


# ── ACCELERATOR: the other programs, the same way ──
# Programs whose companies say "backed by X" / "a X company" on their own
# sites. YC has its own signal (YC_BACKED) and isn't repeated here. Names
# that are also words or places match only as written: "Antler", not
# "antlers"; "Pioneer" but not "pioneer in" or "Pioneer Square"; "Neo" but
# not "Neo4j" or "Neo Capital"; "AI Grant" but not "an AI grant"; "500
# Startups" but not "500 startups". Their own sites are never prospects
# (discover.JUNK_DOMAIN_PATTERNS).
# Not Neo4j, Neo.js, Neo Capital, Antler Homes, "the Pioneer of", "the
# Pioneer dealer network": only a few words may follow the bare name.
_NOT_NAMED = (r"(?![\w-]|\.\w|\s+(?!(?:and|or|in|as|with|since|from|to|alongside|plus|batch|cohort|company"
              r"|startup|portfolio|alumni|alums?|backed|funded|residency|program)\b)[\w])")
ACCELERATORS = (
    ("Techstars", r"Techstars"),
    ("500 Global", r"(?-i:500\s+(?:Global(?!\s+[a-z]+s\b)|Startups))"),     # not "500 Global retailers"
    ("Antler", rf"(?-i:Antler){_NOT_NAMED}"),
    ("Entrepreneur First", r"(?-i:Entrepreneur\s+First)"),
    ("South Park Commons", r"South\s+Park\s+Commons"),
    ("Neo", rf"(?-i:Neo){_NOT_NAMED}"),
    ("AI Grant", r"(?-i:AI\s+Grant)"),
    ("a16z speedrun", r"(?:a16z(?:['’]s)?\s+speedrun|speedrun\s+(?:by|from)\s+a16z)"),
    ("Pioneer", rf"(?-i:Pioneer){_NOT_NAMED}"),
    ("Plug and Play", r"(?-i:Plug\s+(?:and|&)\s+Play)"),
)
_ACC_BATCH = r"(?:['’]?\d{2}|20\d{2}|(?-i:SR)\s*\d{1,3})"
_ACC_NOUNS = r"company|startup|portfolio company|alumn(?:us|i|a)"
_ACC_EVENTS = (r"startup weekend|startup week|startup digest|summit|expo|events?|conference|podcast|blog"
               r"|news|newsletter|jobs|applications?|mentors?|demo day|office hours")
# "Techstars Boulder", "Techstars Anywhere Fall"; never one of its events.
_CITY = r"(?:\s+(?!(?:Startup|Demo|Summit|Expo|Weekend|Week|Day|Conference|Podcast)\b)(?-i:[A-Z][a-z]+)){0,2}"


def _cohort_finder(prog: str):
    """A function from a sentence to the cohort written next to ``prog``
    ("Techstars '24", "Techstars Boulder 2023", "AI Grant batch 3",
    "speedrun SR003"), normalised, or None."""
    shapes = (
        (re.compile(rf"\b{prog}(?:['’]s)?{_CITY}\s*[\(\[,]?\s*(?:class of\s+)?['’](\d{{2}})\b", re.I),
         lambda m: f"20{m.group(1)}"),
        (re.compile(rf"\b{prog}(?:['’]s)?{_CITY}\s*[\(\[,]?\s*(?:class of\s+)?(20\d{{2}})\b", re.I),
         lambda m: m.group(1)),
        (re.compile(rf"\b{prog}\s+(\d{{2}})\s*[\)\]]", re.I), lambda m: f"20{m.group(1)}"),
        (re.compile(rf"\b{prog}(?:['’]s)?\s+(?:batch|cohort)\s*#?\s*(\d{{1,2}})\b", re.I),
         lambda m: f"batch {int(m.group(1))}"),
        (re.compile(rf"\b{prog}(?:['’]s)?\s*[\(\[]?\s*(?-i:SR)\s*(\d{{1,3}})\b", re.I),
         lambda m: f"SR{int(m.group(1)):03d}"),
    )

    def find(sentence: str) -> str | None:
        for shape, norm in shapes:
            m = shape.search(sentence)
            if m:
                cohort = norm(m)
                if cohort.startswith("20") and not 2005 <= int(cohort) <= datetime.now().year + 1:
                    continue
                return cohort
        return None
    return find


# Two shapes seen on real About pages: founders who are alumni ("Permutive
# was founded in 2014 by A and B, alumni of Entrepreneur First") and a
# timeline line ("DigitalOcean founded, joins the TechStars Accelerator").
_ACC_EXTRA = (
    r"|\s+(?:was\s+)?founded(?:\s+in\s+\d{{4}})?(?:\s+by\s+[^.]{{0,80}}?)?,?\s+(?:(?:both|all)\s+)?"
    r"(?:alumni|alums?|graduates)\s+of\s+(?:the\s+)?{prog}\b"
    r"|(?:\s+founded,?)?\s+(?:joins|enters|graduates from)\s+(?:the\s+)?{prog}\b"
)
_ACC_CLAIMS = tuple((name, _ProgramClaims(prog, _ACC_BATCH, _ACC_NOUNS, _ACC_EVENTS, _ACC_EXTRA.format(prog=prog),
                                           strict=True),
                     _cohort_finder(prog))
                    for name, prog in ACCELERATORS)
# A logo wall: a "Backed by" / "Investors" label, then the program's logo
# (alt text, aria-label, title or a link's text) in the same section: before
# the next heading, and with no more than a few names' worth of text in
# between. Customers' and partners' walls don't count.
_WALL_LABEL_RE = re.compile(
    r"""(?:>|["'])\s*(?:(?:proudly\s+)?(?:backed|funded|supported)\s+by|(?:our\s+)?investors?"""
    r"""(?:\s+(?:and|&)\s+partners?)?(?:\s+logos?)?)\s*:?\s*(?:<|["'])""", re.I)
_WALL_SPAN, _WALL_TEXT = 4000, 300
_SECTION_END_RE = re.compile(r"(?i)<h[1-6]\b|</section>")
_OTHER_WALL_RE = re.compile(r"(?i)\b(?:customers?|clients?|partners?|trusted by|used by|featured in|as seen)\b")


def _on_logo_wall(html: str, prog: str) -> bool:
    logo = re.compile(rf"""(?:\b(?:alt|aria-label|title)=["']|>)\s*{prog}\b[^"'<]{{0,30}}(?:["']|<)""", re.I)
    for label in _WALL_LABEL_RE.finditer(html):
        end = label.end() + _WALL_SPAN
        stop = _SECTION_END_RE.search(html, label.end(), end)
        m = logo.search(html, label.end(), stop.start() if stop else end)
        between = _text(html[label.end():m.start()]).strip() if m else ""
        if m and len(between) <= _WALL_TEXT and not _OTHER_WALL_RE.search(between):
            return True
    return False


def accelerators(html: str, domain: str = "") -> list[tuple[str, str, float]]:
    """[(program, cohort or "", confidence)] for every accelerator the page
    says the company itself went through, in ACCELERATORS order."""
    if not html:
        return []
    out = []
    stripped = _strip_code(html)
    for (name, claims, cohort), (_, prog) in zip(_ACC_CLAIMS, ACCELERATORS):
        found_in = claims.claims(html, domain, cohort)
        if found_in:
            stated = next((c for c in found_in if c), "")
            out.append((name, stated, 0.9 if stated else 0.85))
        elif _on_logo_wall(stripped, prog):
            out.append((name, "", 0.8))
    return out


# ── FUNDING_ROUND: a raise the company announces on its own pages ──
# Read off the homepage, the search hit's own page, About, and a press or
# news page. Only the company's own raise counts: "we raised", "<name>
# raises", "announcing our seed round". Not "we help startups raise",
# a customer's or a portfolio company's round, or a VC's new fund. The date
# matters as much as the round (an old raise is no reason to write now), so
# it's taken from the sentence, else from the page's own publish date.
# "seed" only as a round: "a $1M seed", "our seed round", not "our seed prices".
_ROUND = (r"(pre[\s-]?seed|seed(?=[\s-]+(?:round|funding|financing|investment|extension|capital)\b"
          r"|\s*(?:[,.;:)]|$|\s+(?:led|from|with|in|on|by|to|and)\b))"
          r"|series\s+[a-h](?![\w-])|bridge(?=\s+round)|growth(?=\s+round))")
_AMOUNT = r"([$€£])\s?(\d[\d,]*(?:\.\d+)?)\s?(k|mm|mn|mio|m|bn|b|million|billion|thousand)?(?!\w)"
# "1,2 Mio Euro", "5 million dollars": the currency after the number.
_AMOUNT_WORD = (r"(\d[\d,]*(?:\.\d+)?)\s?(k|mm|mn|mio|m|bn|b|million|billion|thousand)\s+"
                r"(euros?|eur|usd|dollars?|gbp|pounds?)\b")
_CURRENCY = {"eur": "€", "euro": "€", "euros": "€", "usd": "$", "dollar": "$", "dollars": "$",
             "gbp": "£", "pound": "£", "pounds": "£"}
_MULTIPLIER = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "mio": 1e6, "million": 1e6,
               "b": 1e9, "bn": 1e9, "billion": 1e9}
_ROUND_RANK = {"Pre-seed": 1, "Seed": 2, "Bridge": 3}
_CLAUSE = r"(?:[^.!?]|\.(?=\d))"     # the rest of the sentence; "$4.5M" isn't its end
_RAISE_VERB = (r"(?P<verb>raised|raises|closed|closes|secured|secures|landed|lands|completed|completes"
               r"|announced|announces)")
_ANNOUNCE = (r"(?:(?:today,?\s+)?we(?:['’]re|\s+are)\s+(?:\w+\s+)?(?:excited|thrilled|proud|happy|delighted|pleased)"
             r"\s+to\s+(?:announce|share)(?:\s+that)?\s+)")
_LEAD_IN = (r"(?:(?:today|update|news|announcement)\s*[,:]?\s+"
            r"|(?:in|on|back in)\s+(?:[A-Za-z]+\.?\s+)?(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?\d{4},?\s+)?")
# Whose money it was: theirs, not their clients' or a fund's.
_NOT_THEIR_RAISE = re.compile(
    r"\b(?:for|on behalf of|across)\s+(?:our\s+|their\s+|its\s+)?(?:clients?|customers?|founders?|startups?"
    r"|companies|portfolio|charity|charities|nonprofits?|members?|users?|causes?|families)\b"
    r"|(?<!\bto )\bfund\b(?!\s+(?:our|the|its|their)\b)|\bto invest\b|\bthe bar\b"
    # an investor's site: "our Series A investment in Acme", "a seed round for Acme"
    r"|\b(?:investment|round|funding|stake)\s+(?:in|into|for)\s+(?!our\b|us\b)(?-i:[A-Z])"
    r"|\bportfolio compan", re.I)
# With no round named, money is a raise only when the sentence says so
# (funding, investors, led by) or "we raised $XM" says it plainly; never
# loans, verdicts, deals or donations (law firms, brokers and builders
# are Harvey's prospects too), or money raised for someone.
_NOT_FUNDING_MONEY = re.compile(
    r"\b(?:arr|mrr|revenue|sales|bookings|pipeline|contracts?|deals?|orders?|savings|grants?|loans?|mortgages?"
    r"|verdicts?|settlements?|judgments?|transactions?|projects?|volume|worth|donations?|donated|charity"
    r"|charities|gala|auction|fundraiser|scholarships?|claims?|recover(?:ed|ies|y)|properties|homes|listings"
    r"|awards?|for)\b", re.I)
_FUNDING_EVIDENCE = re.compile(
    r"\b(?:funding|investment|financing|led by|co-led by|investors?|venture|backed by)\b", re.I)
# A date in another clause ("..., and in March 2025 we launched") isn't the raise's.
_CLAUSE_END_RE = re.compile(r",\s+(?:and|but|while|then|before|after|since)\b|;|\s+and\s+(?:in|on|then|later)\b",
                            re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_DATE_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?:(\d{1,2})(?:st|nd|rd|th)?,?\s+)?(20\d{2})\b", re.I)
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_YEAR_RE = re.compile(r"(?:\bin\s+|\(\s*)(20\d{2})\b")
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"(20\d{2}-\d{2}-\d{2})')
_META_DATE_RES = (
    re.compile(r"""<meta[^>]+(?:property|name)=["'](?:article:published_time|date|pubdate)["'][^>]*"""
               r"""content=["'](20\d{2}-\d{2}-\d{2})""", re.I),
    re.compile(r"""<meta[^>]+content=["'](20\d{2}-\d{2}-\d{2})[^"']*["'][^>]*"""
               r"""(?:property|name)=["'](?:article:published_time|date|pubdate)["']""", re.I),
)
_TIME_RE = re.compile(r"""<time\b[^>]*datetime=["'](20\d{2}-\d{2}-\d{2})""", re.I)
_URL_DATE_RES = (re.compile(r"/(20\d{2})-(\d{2})-(\d{2})(?:[/-]|$)"), re.compile(r"/(20\d{2})/(\d{2})/"))


def _sentence_date(sentence: str) -> str:
    """The most precise date a sentence states: 2025-03-04, 2025-03, 2025, or ""."""
    m = _ISO_DATE_RE.search(sentence)
    if m:
        return m.group(0)
    m = _MONTH_DATE_RE.search(sentence)
    if m:
        month = _MONTHS[m.group(1).lower()[:3]]
        return f"{m.group(3)}-{month:02d}" + (f"-{int(m.group(2)):02d}" if m.group(2) else "")
    m = _YEAR_RE.search(sentence)
    return m.group(1) if m else ""


_ARTICLE_PATH_RE = re.compile(
    r"/(?:press|news|newsroom|blog|posts?|announc\w*|stories|updates|articles?|insights)(?:[/-]|$)", re.I)
_ARTICLE_TYPE_RE = re.compile(
    r'"@type"\s*:\s*\[?\s*"(?:NewsArticle|BlogPosting|Article|PressRelease|ReportageNewsArticle|TechArticle)"')


def _not_future(when: str) -> str:
    """A date after today isn't when a raise happened ("to expand in 2027")."""
    return "" if when and when > date.today().isoformat()[:len(when)] else when


def _page_date(html: str, url: str) -> str:
    """When the page itself was published: its structured data, its article
    meta tag, a date in its URL, or its one <time> (a listing page full of
    dates says nothing about any one claim). Only for an article: a press,
    news or blog URL, or an article type in its structured data. A homepage,
    About or careers page carries dates that aren't any raise's."""
    path = urlparse(url or "").path
    if not (_ARTICLE_PATH_RE.search(path) or _ARTICLE_TYPE_RE.search(html)
            or any(r.search(path) for r in _URL_DATE_RES)):
        return ""
    m = _JSONLD_DATE_RE.search(html)
    if m:
        return m.group(1)
    for r in _META_DATE_RES:
        m = r.search(html)
        if m:
            return m.group(1)
    for r in _URL_DATE_RES:
        m = r.search(url or "")
        if m:
            return "-".join(m.groups())
    times = set(_TIME_RE.findall(html))
    return times.pop() if len(times) == 1 else ""


def _amount(symbol: str, number: str, suffix: str) -> str:
    """"$2,500,000" → "$2.5M", "€8m" → "€8M", "€1,2M" → "€1.2M"."""
    suffix = (suffix or "").lower()
    if suffix and re.fullmatch(r"\d{1,3},\d{1,2}", number):
        number = number.replace(",", ".")       # a decimal comma before a unit
    try:
        value = float(number.replace(",", "")) * _MULTIPLIER.get(suffix, 1)
    except ValueError:
        return ""
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= div:
            return f"{symbol}{round(value / div, 2):g}{unit}"
    return f"{symbol}{value:g}"


def _amount_in(text: str) -> str:
    m = re.search(_AMOUNT, text, re.I)
    if m:
        return _amount(m.group(1), m.group(2), m.group(3))
    m = re.search(_AMOUNT_WORD, text, re.I)
    return _amount(_CURRENCY[m.group(3).lower()], m.group(1), m.group(2)) if m else ""


def _round_name(text: str) -> str:
    text = re.sub(r"\s+", " ", text.lower())
    if text.startswith("pre"):
        return "Pre-seed"
    if text.startswith("series"):
        return "Series " + text[-1].upper()
    return text.capitalize()


def funding_round(html: str, domain: str = "", url: str = "") -> dict | None:
    """{"round", "amount", "date", "conf"} for the newest raise the page
    says the company itself made, else None. Any part it doesn't state is
    ""; the date is the sentence's, else the page's."""
    if not html:
        return None
    name = _alnum(_domain_name(domain)) if domain else ""
    spelled = r"[\s.-]?".join(re.escape(ch) for ch in name) if len(name) >= 3 else ""
    spelled = spelled and rf"{spelled}(?:\.[a-z]{{2,6}})?"     # "Context.dev has raised"
    subject = r"(?:we(?:['’]ve)?|the company|our company" + (rf"|{spelled}\b" if spelled else "") + r")"
    raised = re.compile(
        rf"^{_LEAD_IN}{_ANNOUNCE}?{subject},?(?:\s+(?:has|have|had|just|now|recently|officially|successfully"
        rf"|today|also|finally))*\s+{_RAISE_VERB}\b(?P<rest>{_CLAUSE}{{0,160}})", re.I)
    owner = r"(?:our" + (rf"|{spelled}['’]s" if spelled else "") + r")"
    announced = re.compile(
        rf"^{_LEAD_IN}(?:(?:announcing|introducing|celebrating)\s+|{_ANNOUNCE})(?:that\s+)?{owner}\s+"
        rf"(?P<rest>(?:{_AMOUNT}\s+)?{_ROUND}{_CLAUSE}{{0,160}})", re.I)
    led = re.compile(
        rf"^{owner}\s+(?P<rest>(?:{_AMOUNT}\s+)?{_ROUND}(?:\s+(?:round|funding|financing))?"
        rf"(?:\s+(?:was|is|has been))?\s+(?:led|co-led|closed|backed)\b{_CLAUSE}{{0,160}})", re.I)
    claims = []
    for sentence in _lines(html):
        m = raised.search(sentence) or announced.search(sentence) or led.search(sentence)
        if not m:
            continue
        rest = m.group("rest")
        end = _CLAUSE_END_RE.search(rest)
        rest = rest[:end.start()] if end else rest
        if _NOT_THEIR_RAISE.search(rest):
            continue
        rnd = re.search(rf"\b{_ROUND}", rest, re.I)
        amount = _amount_in(rest)
        if not rnd:
            plain = (m.groupdict().get("verb") or "").lower().startswith("rais") and amount[-1:] in ("M", "B")
            if not amount or _NOT_FUNDING_MONEY.search(rest) or not (plain or _FUNDING_EVIDENCE.search(rest)):
                continue
        when = _not_future(_sentence_date(sentence[:m.start("rest")] + rest))
        claims.append({"round": _round_name(rnd.group(1)) if rnd else "", "amount": amount, "date": when})
    if not claims:
        return None
    # The page's own date belongs to what the page announces: its only raise,
    # or the one its title names. A seed post that links to the later Series
    # A doesn't date the Series A.
    page_date = _not_future(_page_date(html, url))
    if page_date:
        undated = [c for c in claims if not c["date"]]
        title = _page_title(html)
        for c in undated:
            if (len({(u["round"], u["amount"]) for u in undated}) == 1
                    or (c["round"] and c["round"].lower() in title)
                    or (c["amount"] and _alnum(c["amount"]) in _alnum(title))):
                c["date"] = page_date
    for c in claims:
        c["conf"] = 0.9 if c["date"] else 0.85

    def rank(c):
        r = c["round"]
        return (c["date"], _ROUND_RANK.get(r, 10 + ord(r[-1]) if r.startswith("Series") else 0), bool(c["amount"]))
    return max(claims, key=rank)


def _page_title(html: str) -> str:
    """The page's <title>, og:title and first <h1>, lowercased, for telling
    which raise the page itself is about."""
    parts = re.findall(r"(?is)<title[^>]*>(.*?)</title>", html)[:1]
    parts += re.findall(r"""(?i)<meta[^>]+property=["']og:title["'][^>]*content=["']([^"']*)""", html)[:1]
    parts += re.findall(r"(?is)<h1\b[^>]*>(.*?)</h1>", html)[:1]
    return html_unescape(re.sub(r"(?s)<[^>]+>", " ", " ".join(parts))).lower()


def open_http() -> httpx.AsyncClient:
    """The client a profile pass reads sites with (one per run)."""
    return public_http_client(
        timeout=TIMEOUT,
        headers={"User-Agent": random.choice(USER_AGENTS),
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    )


class Known:
    """What core already knows about the company being read, handed in as
    values (no database): whether a preset search found it (by signal
    code), and the page that search found it on. The collector asks it
    the way it would ask the database."""

    def __init__(self, search_hits: dict[str, bool] | None = None, source_url: str = ""):
        self.search_hits = dict(search_hits or {})
        self.source_url = source_url

    async def search_hit(self, company_id: str, code: str) -> bool:
        return bool(self.search_hits.get(code))

    async def get_company(self, company_id: str):
        return self


class ProfileCollector:
    """Fetches a business's public pages and emits observations.

    Only signals the user CONFIRMED are collected; everything else is skipped
    even if the data is sitting right there in the HTML. ``known`` is what
    core knows about the company already (Known); it is only read.
    """

    def __init__(self, known=None, confirmed: set[str] | None = None, timeout: float = 12.0,
                 role_keywords: list[str] | None = None, config=None):
        self.known = known
        # Where a business's national phone numbers are read when its
        # location doesn't name a country (prospect_country, icp.geography).
        self.config = config
        self.confirmed = confirmed if confirmed is not None else set()
        self.timeout = timeout
        # icp.hiring_signals (or icp.titles): which open roles are worth a row.
        self.role_keywords = list(role_keywords or [])
        self._client: httpx.AsyncClient | None = None
        self.site_names: dict[str, str] = {}
        # The first phone each company's pages publish: the company's own
        # phone, when it has none (site_draft returns it as a contact).
        self.phones: dict[str, str] = {}
        # Hosts that answered with a bot challenge (SiteGround's 202,
        # Cloudflare's "Just a moment...") rather than a page.
        self.challenged: set[str] = set()
        # Read for the ways a company can be reached whatever is confirmed
        # (the contact finder's site read): the contact page and the team
        # page are fetched for them, and kept here per company.
        self.contacts = False
        self.forms: dict[str, str] = {}
        self.team: dict[str, list[dict]] = {}

    def wants(self, code: str) -> bool:
        return code in self.confirmed

    async def __aenter__(self):
        self._client = open_http()
        self._pages = PageReader()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()
        if getattr(self, "_pages", None) is not None:
            await self._pages.aclose()

    def _method(self, url: str) -> str:
        """What produced the page at ``url``: "http", or the browser."""
        pages = getattr(self, "_pages", None)
        return pages.method(url) if pages is not None else HTTP

    async def _get(self, url: str) -> str:
        """The page's HTML over HTTP, or rendered by a browser when HTTP got
        a JavaScript shell or a block (harvey/browser.py); "" when neither
        produced a page. Where it landed after any redirect is kept in
        ``landed_at`` (the last few URLs asked)."""
        final, html = await self._fetch(url)
        if html:
            landed = self.__dict__.setdefault("landed_at", {})
            if len(landed) > 16:
                landed.clear()
            landed[url] = final
        return html

    async def _fetch(self, url: str) -> tuple[str, str]:
        """(the URL the page was read at after any redirect, its HTML), as
        ``_get`` reads it; (url, "") when nothing produced a page."""
        pages = getattr(self, "_pages", None)
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if pages is not None and pages.browser_first(url):
            page = await pages.render(url)
            if page is not None:
                return page.url or url, page.text
        try:
            resp = await self._client.get(url)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug(f"fetch failed {url}: {e}")
            return url, ""
        final = str(getattr(resp, "url", "") or url)
        blocked = resp.status_code in (200, 202, 403, 429, 503) and looks_like_challenge(
            resp.text or "", resp.status_code)
        ctype = resp.headers.get("content-type", "")
        html_ok = ("html" in ctype or "xml" in ctype or "text" in ctype)
        if pages is not None and (blocked or resp.status_code != 200 or html_ok):
            page = await pages.after_http(url, html=resp.text if html_ok else "",
                                          status=resp.status_code, blocked=blocked)
            if page is not None:
                self.challenged.discard(host)
                return page.url or final, page.text
        if blocked:
            self.challenged.add(host)
            return final, ""
        if resp.status_code != 200:
            return final, ""
        if not html_ok:
            return final, ""
        return final, resp.text

    async def _resolve_site(self, domain: str) -> tuple[str, str]:
        """Return (working base url, homepage html), or ("", "") if none work.

        Tries https/http and bare/www variants — a plain https guess marks
        plenty of live, real small-business sites "unreachable" simply
        because they never got a TLS cert, or only answer on www.
        """
        for base in (
            f"https://{domain}", f"https://www.{domain}",
            f"http://{domain}", f"http://www.{domain}",
        ):
            html = await self._get(base)
            if html:
                # Where the homepage landed (a redirect to /ro/): its menu's
                # relative links are relative to that page, not the root.
                self.__dict__.setdefault("landed", {})[domain] = \
                    self.__dict__.get("landed_at", {}).get(base, base)
                return base, html
            if domain.lower().removeprefix("www.") in self.challenged:
                break       # every variant lands on the same challenge
        return "", ""

    async def _first_present(self, site: str, paths) -> tuple[str, str]:
        """Return (url, html) for the first path that returns content."""
        for path in paths:
            url = f"{site}{path}"
            html = await self._get(url)
            if html and len(html) > 500:
                return url, html
        return "", ""

    async def _site_pages(self, site: str, home: str, domain: str,
                          fetched: int = 1) -> list[tuple[str, str]]:
        """The pages the homepage's own menu links to where a business names
        its people and says how to reach it (team, meet-the-doctors, about,
        contact), as (url, html), at most MAX_SITE_PAGES - 1 of them and
        MAX_SITE_FETCHES requests in all (``fetched``: the ones that found
        the homepage). A link that redirects to another site (a franchisor's
        team page), or lands back on the homepage, isn't read; a host that
        answers with a bot check stops the read (it is never got around)."""
        from harvey.collectors.people import _norm_url, menu_links

        out: list[tuple[str, str]] = []
        seen = {_norm_url(site)}
        base = getattr(self, "landed", {}).get(domain) or site
        if (urlparse(base).hostname or "").lower().removeprefix("www.") != domain:
            base = site     # landed on another site: its menu isn't this one's
        seen.add(_norm_url(base))
        if base == site:
            base = site + "/"
        links = [(url, False) for url in menu_links(home, base, limit=MAX_SITE_PAGES - 1)]
        if len(links) < 2:
            paths = FALLBACK_SITE_PATHS
            if domain.rstrip(".").endswith(".ro"):
                paths = tuple(dict.fromkeys(RO_FALLBACK_SITE_PATHS + paths))
            links += [(f"{site}{path}", True) for path in paths]
        for url, guessed in links:
            if len(out) >= MAX_SITE_PAGES - 1 or fetched >= MAX_SITE_FETCHES:
                break
            if _norm_url(url) in seen:
                continue
            seen.add(_norm_url(url))
            fetched += 1
            final, html = await self._fetch(url)
            if not html:
                if domain in getattr(self, "challenged", ()):
                    break
                continue
            host = (urlparse(final).hostname or "").lower().removeprefix("www.")
            if host != domain:
                continue        # redirected to someone else's site
            if html == home or (_norm_url(final) != _norm_url(url) and _norm_url(final) in seen):
                continue        # a menu link that is really the homepage, or a page read already
            if guessed and len(html) <= 500:
                continue        # a guessed path that answered with next to nothing
            seen.add(_norm_url(final))
            out.append((final, html))
        return out

    async def _careers_page(self, site: str, home: str, domain: str) -> tuple[str, str]:
        """(url, html) of the careers page: the homepage's own link to it
        (same domain or a subdomain), else the first common path that exists."""
        for link in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                                _strip_code(home)):
            url = urljoin(site + "/", link.group(1).strip())
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme not in ("http", "https") or not (
                    host == domain or host.endswith("." + domain)):
                continue
            anchor = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", link.group(2))).strip()
            segments = {seg.lower() for seg in parsed.path.split("/") if seg}
            if (segments or host != domain) and (
                    _CAREERS_HOST.match(host) or segments & _CAREERS_SEGMENTS
                    or _CAREERS_ANCHOR.match(html_unescape(anchor))):
                html = await self._get(url)
                if html and len(html) > 500:
                    return url, html
                break
        return await self._first_present(site, CAREERS_PATHS)

    async def _hiring(self, site: str, home: str, domain: str, add) -> None:
        curl, chtml = "", ""
        board = detect_board(home, domain)
        if board is None:
            curl, chtml = await self._careers_page(site, home, domain)
            board = detect_board(chtml, domain)
        postings = await read_board(self._client, board) if board else None
        if postings is not None:
            add("OPEN_ROLES", num=len(postings), text=board.label, url=board.page_url)
            keywords = getattr(self, "role_keywords", None) or []
            matched = matching_roles(postings, keywords)[:MAX_BOARD_ROLES]
            for post in matched:
                add("HIRING_ROLE", text=post.summary(), url=post.url or board.page_url)
            if not matched:
                # The board answered and lists nothing relevant: say so, so
                # last month's (now closed) roles stop being the current view.
                add("HIRING_ROLE", num=0, url=board.page_url)
            return
        if board and not chtml:
            curl, chtml = await self._careers_page(site, home, domain)
        if chtml and self.wants("HIRING_ROLE"):
            roles = []
            for m in re.finditer(
                r"(?is)<(h[2-5]|a|li)[^>]*>\s*([^<]{6,70})\s*</\1>", _strip_code(chtml)
            ):
                t = re.sub(r"\s+", " ", m.group(2)).strip()
                if re.search(r"\b(hiring|apply|position|opening|wanted|"
                             r"technician|installer|sales|manager|foreman|"
                             r"estimator|crew|representative)\b", t, re.I):
                    if t.lower() not in [r.lower() for r in roles]:
                        roles.append(t)
                if len(roles) >= 3:
                    break
            for r in roles:
                add("HIRING_ROLE", text=r, url=curl)
    async def _search_hit(self, company_id: str, code: str) -> bool:
        """Is the latest reading a preset search's hit? (see state.search_hit)"""
        if getattr(self, "known", None) is None:
            return False
        try:
            return await self.known.search_hit(company_id, code)
        except Exception:
            return False

    async def _own_hit_page(self, company_id: str, domain: str, site: str) -> str:
        """The page a search found the company on, when it's on their own
        site and isn't the homepage; "" otherwise. Never someone else's site."""
        try:
            company = await self.known.get_company(company_id)
        except Exception:
            return ""
        url = (getattr(company, "source_url", "") or "").strip()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in ("http", "https") or not (host == domain or host.endswith("." + domain)):
            return ""
        if parsed.path.strip("/") == "" or url.rstrip("/") == site.rstrip("/"):
            return ""
        return url

    # ── individual detectors ──

    def detect_agency(self, html: str, domain: str) -> tuple[str, float, str] | None:
        """Find the agency credited in the footer. (name, confidence, url)."""
        cleaned = _strip_code(html)
        m = re.search(r"(?is)<footer\b.*?</footer>", cleaned)
        region = m.group(0) if m else cleaned[-12000:]

        best = None
        for link in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', region):
            href, anchor = link.group(1), re.sub(r"(?s)<[^>]+>", " ", link.group(2))
            anchor = re.sub(r"\s+", " ", anchor).strip()
            if not href.startswith("http"):
                continue
            host = (urlparse(href).netloc or "").lower().replace("www.", "")
            if not host or host.endswith(domain) or domain.endswith(host):
                continue
            if any(b in href.lower() or b in host for b in BENIGN_HOSTS):
                continue
            if not anchor or len(anchor) > 80:
                continue

            # Context = the ~120 chars before the link, where credits live.
            start = max(0, link.start() - 120)
            context = re.sub(r"(?s)<[^>]+>", " ", region[start:link.start()])
            blob = f"{context} {anchor}"

            if STRONG_CREDIT.search(blob):
                conf = 0.9
            elif DESCRIPTIVE_CREDIT.search(anchor):
                conf = 0.75
            else:
                conf = 0.35  # a bare link — do not record
            if conf >= RECORD_FLOOR and (best is None or conf > best[1]):
                best = (anchor[:80], conf, href)
        return best

    def detect_tech(self, html: str) -> list[str]:
        found = []
        for name, patterns in TECH_FINGERPRINTS.items():
            if any(re.search(p, html, re.I) for p in patterns):
                found.append(name)
        return sorted(found)

    @staticmethod
    def _any(html: str, patterns) -> bool:
        return any(re.search(p, html, re.I) for p in patterns)

    def detect_people(self, html: str, domain: str = "", site_name: str = "") -> list[dict]:
        """Names + titles from a team page. Conservative on purpose.

        Two passes: a name in a heading with its title in the text after
        it, and a name on one line with its title on the next ("Dale
        Whitlock<br>Owner and CEO" in a span). Names made of ordinary words
        ("Team of Experts") and titles that belong to another company (a
        customer quoted as "Founder at Quillnest") are rejected.
        """
        from harvey.collectors.people import (
            NAME_LOWER, NAME_UPPER, drop_quoted, people_from_lines,
            romanian_title_needs_honorific, split_person_name, title_names_other_org,
        )

        text_html = _strip_code(drop_quoted(html))
        people, seen = [], set()

        def add(first, last, title, full_name=""):
            key = (first.lower(), last.lower())
            if key in seen or len(people) >= 12:
                return
            if title_names_other_org(title, domain, site_name):
                return
            seen.add(key)
            people.append({
                "first_name": first, "last_name": last,
                "full_name": full_name or f"{first} {last}",
                "title": title[:80],
                "is_decision_maker": any(t in title.lower() for t in DECISION_MAKER_TITLES),
            })

        # Name in a heading, title in the nearby text.
        # Tag names match any case; the NAME must not — under a blanket (?i)
        # "Customer care" passes the Firstname Lastname capitalisation test.
        for m in re.finditer(
            rf"(?s)<((?i:h[1-5]|strong|b))[^>]*>\s*((?:(?:Prof\.?\s+)?Dr\.?\s+)?"
            rf"[{NAME_UPPER}][{NAME_LOWER}'\-]+(?:\s+[{NAME_UPPER}]\.?)?"
            rf"(?:\s+(?:[{NAME_UPPER}][{NAME_UPPER}{NAME_LOWER}'\-]+|van|von|de|der|del|la|le|di|da)){{1,3}}"
            r"(?:,?\s+(?:DDS|DMD|MD|Esq\.?|CPA|Jr\.?|Sr\.?|II|III))?)\s*</(?i:\1)>(?=(.{0,220}))",
            text_html
        ):
            # NOTE the lookahead above: capturing the trailing context normally
            # would CONSUME it, so finditer would skip every person whose entry
            # fell inside the previous person's 220-char window.
            name = re.sub(r"\s+", " ", m.group(2)).strip()
            # Bound the context at the NEXT heading, in raw HTML, before
            # stripping tags — otherwise one member's role bleeds into the
            # next member's entry.
            raw_after = m.group(3)
            nxt = re.search(r"(?i)<(h[1-5]|strong|b)\b", raw_after)
            if nxt:
                raw_after = raw_after[: nxt.start()]
            if name.lower() in seen or len(name) > 45:
                continue
            # Reject obvious non-names
            if re.search(r"\d|@|\bthe\b|\bour\b|\bwe\b", name, re.I):
                continue
            # Reject headings that merely LOOK like names. Every token must be
            # plausibly a name part; one business word disqualifies the whole
            # match ("Request Service", "Baker History", "HOA Boards").
            if not all(_is_name_token(t) for t in name.split()):
                continue
            split = split_person_name(name)
            if not split:
                continue
            # A real contact needs a title that reads like a role; a bare
            # heading with no role text is almost always navigation, and a
            # heading followed by a sentence is a feature list.
            title = next((c for c in _title_chunks(raw_after)[:2]
                          if _reads_like_title(c)), "")
            if not title:
                continue
            # "Igienizare Profesională" over "Medic primar stomatolog" is a
            # service card; a Romanian medical title needs a Dr./Prof. name.
            full_name = re.sub(r"(?i)^(?:prof\.?\s+)?dr\.?\s+", "", name)
            if full_name == name and romanian_title_needs_honorific(title):
                continue
            add(split[0], split[1], title, full_name)
        # Name and title on consecutive lines, or "Name, Title" on one.
        for p in people_from_lines(html, domain, site_name):
            if all(_is_name_token(t) for t in p["first_name"].split() + p["last_name"].split()):
                add(p["first_name"], p["last_name"], p["title"], p["full_name"])
        return people

    # ── the stage itself ──

    async def profile_company(self, company_id: str, domain: str,
                              location: str = "") -> list[dict]:
        """Collect every confirmed profile signal for one business."""
        obs: list[dict] = []
        # Pages fetched for other signals, read again for phone numbers.
        pages: list[tuple[str, str]] = []
        domain = domain.lower().replace("www.", "").strip("/")
        if any(domain == d or domain.endswith("." + d) for d in YC_OWNED_DOMAINS):
            # YC's own sites forbid scraping; they're never a prospect anyway.
            return obs

        def add(code, *, num=None, text="", conf=1.0, url=""):
            if not self.wants(code):
                return
            method = self._method(url or site)
            obs.append({
                "signal_code": code, "company_id": company_id,
                # Provenance: a rendered page says which browser read it.
                "collector": "profile" if method == HTTP else f"profile:{method}",
                "value_num": num, "value_text": text, "confidence": conf,
                "evidence_url": url or site,
            })

        site, home = await self._resolve_site(domain)
        if not home:
            # A failure IS an observation — silent failures look like clean results.
            if domain in self.challenged:
                logger.info(f"profile: {domain} answered with a bot challenge (challenged, not unreachable)")
            else:
                logger.info(f"profile: {domain} unreachable")
            return obs
        self.site_names[company_id] = site_name_from_html(home, domain)
        pages.append((site, home))
        self.__dict__.setdefault("pages_read", {})[company_id] = pages

        # Ads (they are spending TODAY — the strongest budget signal here)
        add("RUNNING_GOOGLE_ADS", num=1 if self._any(home, GOOGLE_ADS) else 0)
        add("RUNNING_META_ADS", num=1 if self._any(home, META_PIXEL) else 0)

        # Platform + tooling
        tech = self.detect_tech(home)
        if tech:
            add("TECH_STACK", text=", ".join(tech))

        # Structured data gap
        has_schema = bool(re.search(r'application/ld\+json', home, re.I)
                          and re.search(r'"@type"', home))
        add("NO_SCHEMA_MARKUP", num=0 if has_schema else 1)

        # Booking gap
        add("NO_ONLINE_BOOKING", num=0 if self._any(home, BOOKING_HINTS) else 1)

        # The About page, fetched once for the startup signals below.
        about_page: list[tuple[str, str]] = []

        async def about() -> tuple[str, str]:
            if not about_page:
                about_page.append(await self._first_present(site, YC_ABOUT_PATHS))
            return about_page[0]

        # YC-backed, as the company says on its own site: the homepage, or
        # its About page (where most say it, if anywhere).
        if self.wants("YC_BACKED"):
            yc, where = yc_backed(home, domain), site
            if yc is None:
                about_url, about_html = await about()
                if about_html:
                    yc, where = yc_backed(about_html, domain), about_url
            # Most YC companies never say it on these pages, so silence
            # doesn't erase a YC-preset search hit (collector "discover").
            # It does correct an earlier profile "yes", so a wrong tag or a
            # dropped claim doesn't last forever.
            searched = yc is None and await self._search_hit(company_id, "YC_BACKED")
            if yc is not None or not searched:
                add("YC_BACKED", num=1 if yc else 0, text=yc[0] if yc else "",
                    conf=yc[1] if yc else 1.0, url=where if yc else site)

        # Another accelerator, the same way. Unlike YC, a preset's search hit
        # doesn't outlast a profile that finds nothing: the search phrases
        # name programs that are also ordinary words ("backed by Neo" matches
        # "backed by Neo Capital"), so the hit alone is weaker evidence.
        if self.wants("ACCELERATOR"):
            programs, where = accelerators(home, domain), site
            if not programs:
                about_url, about_html = await about()
                if about_html:
                    programs, where = accelerators(about_html, domain), about_url
            add("ACCELERATOR", num=1 if programs else 0,
                text="; ".join(f"{p} {c}".strip() for p, c, _ in programs),
                conf=max(c for _, _, c in programs) if programs else 1.0,
                url=where if programs else site)

        # A raise they announced: the homepage, the page a search found on
        # their own site (a funding-preset hit is usually the announcement),
        # About, then a press or news page. The newest wins.
        if self.wants("FUNDING_ROUND"):
            # (Its own list: ``pages`` is what the read returns.)
            funding_pages = [(site, home)]
            hit = await self._own_hit_page(company_id, domain, site)
            if hit:
                funding_pages.append((hit, await self._get(hit)))
            funding_pages.append(await about())
            found = [(f, url) for url, html in funding_pages if html
                     for f in [funding_round(html, domain, url)] if f]
            if not found:
                url, html = await self._first_present(site, FUNDING_PATHS)
                f = funding_round(html, domain, url) if html else None
                found = [(f, url)] if f else []
            if found:
                best, where = max(found, key=lambda fu: fu[0]["date"])
                add("FUNDING_ROUND", num=1, text=funding_text(best), conf=best["conf"], url=where)
            else:
                add("FUNDING_ROUND", num=0, url=site)

        # Incumbent agency — the compounding signal
        agency = self.detect_agency(home, domain)
        if agency:
            add("INCUMBENT_AGENCY", text=agency[0], conf=agency[1], url=agency[2])

        # robots.txt → AI crawler policy
        if self.wants("BLOCKS_AI_CRAWLERS"):
            robots = (await self._get(f"{site}/robots.txt")).lower()
            blocked = any(
                re.search(rf"user-agent:\s*{re.escape(bot)}[\s\S]{{0,200}}?disallow:\s*/",
                          robots)
                for bot in AI_CRAWLERS
            )
            add("BLOCKS_AI_CRAWLERS", num=1 if blocked else 0,
                url=f"{site}/robots.txt")

        # sitemap.xml → size + content freshness
        if self.wants("SITE_PAGE_COUNT") or self.wants("BLOG_STALE"):
            sm = await self._get(f"{site}/sitemap.xml")
            if sm:
                locs = re.findall(r"<loc>\s*([^<]+)\s*</loc>", sm)
                if self.wants("SITE_PAGE_COUNT") and locs:
                    add("SITE_PAGE_COUNT", num=len(locs), url=f"{site}/sitemap.xml")
                if self.wants("BLOG_STALE"):
                    dates = re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", sm)
                    if dates:
                        newest = max(dates)
                        try:
                            age = (datetime.now(timezone.utc).date()
                                   - datetime.strptime(newest, "%Y-%m-%d").date()).days
                            add("BLOG_STALE", num=age, text=f"newest content {newest}",
                                url=f"{site}/sitemap.xml")
                        except ValueError:
                            pass

        # The pages the site's own menu links to where it names its people
        # and says how to reach it (team, about, contact): read when anyone
        # is wanted, and always for the contact finder (``contacts``).
        reach = getattr(self, "contacts", False)
        people_wanted = reach or any(self.wants(code) for code in PEOPLE_CODES)
        menu: list[tuple[str, str]] = []
        if people_wanted:
            menu = await self._site_pages(site, home, domain, fetched=_variant_number(site, domain))
            pages.extend(menu)

        # contact form — reaches the business without needing an email at all
        if self.wants("CONTACT_FORM_URL") or reach:
            read = [(site, home)] + sorted(menu, key=lambda p: not _CONTACT_URL.search(p[0]))
            form = next((url for url, html in read
                         if re.search(r"(?is)<form\b", _strip_code(html))), "")
            if not form and not people_wanted:
                # A plain read (no menu read): the common contact paths.
                curl, chtml = await self._first_present(site, CONTACT_PATHS)
                if chtml:
                    pages.append((curl, chtml))
                if chtml and re.search(r"(?is)<form\b", _strip_code(chtml)):
                    form = curl
            if form:
                add("CONTACT_FORM_URL", text=form, url=form)
                self.__dict__.setdefault("forms", {})[company_id] = form

        # open roles → hiring intent. The job-board feed when their site
        # links one (titles, departments and dates for every role), else
        # whatever the careers page's own HTML lists. Any failure here costs
        # only these two signals, never the rest of the profile.
        if self.wants("HIRING_ROLE") or self.wants("OPEN_ROLES"):
            try:
                await self._hiring(site, home, domain, add)
            except Exception as e:
                logger.info(f"profile: {domain} open roles unread: {type(e).__name__}: {e}")

        # the people the homepage and its menu pages name, owners first
        if people_wanted:
            self._people(company_id, [(site, home)] + menu, domain, add)

        self._phones(company_id, pages, phone_region(location, getattr(self, "config", None), domain), add)
        return obs

    def _people(self, company_id: str, read: list[tuple[str, str]], domain: str, add) -> None:
        """Everyone the pages read name (people.find_people: team cards and
        lists, schema.org founder and employee markup, sentences such as
        "owned and operated by" or "fondată de"), the likeliest owner
        first; CSS team cards only when none of those name anyone. Kept for
        the contact finder (``team``), with CONTACT_FOUND for each,
        DECISION_MAKER_TITLE for an owner or a decision maker's title, and
        LIKELY_OWNER for the owners."""
        from harvey.collectors.people import find_people, is_owner_title, people_from_cards

        site_name = self.site_names.get(company_id, "")
        found = find_people(read, domain, site_name) or people_from_cards(read)
        team = self.__dict__.setdefault("team", {}).setdefault(company_id, [])
        owners = 0
        for person in found[:MAX_TEAM]:
            url = person.get("evidence_url") or read[0][0]
            team.append({**person, "url": url})
            label = f"{person['first_name']} {person['last_name']}"
            title = person.get("title") or ""
            text = f"{label} — {title}" if title else label
            add("CONTACT_FOUND", text=text, url=url)
            if person.get("owner") or any(t in title.lower() for t in DECISION_MAKER_TITLES):
                add("DECISION_MAKER_TITLE", text=text, url=url)
            if person.get("owner") and owners < MAX_OWNERS:
                owners += 1
                said = f"{label} — {title or 'owner'}"
                evidence = (person.get("evidence") or "").strip()
                if evidence and evidence != text:
                    said += f": {evidence}"     # the sentence or markup that says so
                add("LIKELY_OWNER", num=1, text=said[:300],
                    conf=0.9 if is_owner_title(title) else 0.7, url=url)

    def _phones(self, company_id: str, pages: list[tuple[str, str]], region, add) -> None:
        """PHONE_NUMBER for each number the fetched pages publish (a tel:
        link or schema.org markup at full confidence), and the first one
        kept as the company's phone (returned by site_draft)."""
        seen: set[str] = set()
        for url, html in pages:
            for hit in phones_from_html(html, region):
                if hit.number in seen or len(seen) >= MAX_PHONES:
                    continue
                seen.add(hit.number)
                self.__dict__.setdefault("phones", {}).setdefault(company_id, hit.display)
                self.__dict__.setdefault("phone_hits", {}).setdefault(company_id, []).append(
                    (url, hit))
                add("PHONE_NUMBER", text=hit.number, conf=hit.confidence, url=url)

    def better_name(self, company_id: str, current: str, domain: str) -> str:
        """The name the homepage gives itself, when it should replace
        ``current``: one the domain doesn't back up ("Denver, CO | HOA
        Management Firm") or one merely derived from the domain ("Grow With
        Us"); "" to keep it. Only a distinctive brand match is kept; generic
        industry and navigation text does not establish identity."""
        better = self.site_names.pop(company_id, "")
        current = current or ""
        fallback = current == name_from_domain(domain)
        if not better or better == current or (
                name_matches_domain(current, domain) and not fallback):
            return ""
        return better


# Which module's capability each fact a profile pass reads belongs to: one
# pass over a site serves them all (the pages are fetched once).
READ_BY = {
    "CONTACT_FORM_URL": ("website", "read_site"),
    **{code: ("web_marketing", "marketing_profile") for code in (
        "RUNNING_GOOGLE_ADS", "RUNNING_META_ADS", "TECH_STACK", "NO_SCHEMA_MARKUP",
        "NO_ONLINE_BOOKING", "INCUMBENT_AGENCY", "BLOCKS_AI_CRAWLERS", "SITE_PAGE_COUNT",
        "BLOG_STALE")},
    **{code: ("startups", "startup_claims") for code in (
        "YC_BACKED", "ACCELERATOR", "FUNDING_ROUND")},
    **{code: ("hiring", "job_board") for code in ("HIRING_ROLE", "OPEN_ROLES")},
    **{code: ("people", "find_people") for code in PEOPLE_CODES},
    "PHONE_NUMBER": ("phones", "site_phones"),
}


async def site_draft(inp, ctx, only: set[str] | None = None) -> Draft:
    """Read one company's site (``inp.domain``, located at ``inp.locality``)
    as a Draft: the facts every confirmed signal (``ctx.wanted``, narrowed
    to ``only``) asks for, each stamped with the capability it belongs to
    (READ_BY); the pages read; the phones they publish, the first of which
    is the company's when it has none; and in ``values["name"]`` the name
    the homepage gives itself when it should replace ``inp.name``.

    ``inp.facts["search_hits"]`` says which signals a preset search found
    it by, ``inp.extra["source_url"]`` the page; ``ctx.http`` and
    ``ctx.browser`` are the run's client and page reader. A site is read
    only through a client that refuses private addresses: given any other
    (or none), the read opens its own. A fact of a module that is off
    isn't read for."""
    wanted = set(ctx.wanted if ctx.wanted is not None else READ_BY)
    if only is not None:
        wanted &= only
    wanted = {code for code in wanted if ctx.on(READ_BY.get(code, ("website",))[0])}
    config = ctx.config
    keywords = ctx.settings.get("role_keywords")
    if keywords is None:
        keywords = role_keywords(getattr(config, "icp", None))
    c = ProfileCollector(Known(inp.facts.get("search_hits"), inp.extra.get("source_url", "")),
                         wanted, role_keywords=keywords, config=config)
    c.contacts = bool(ctx.settings.get("contacts"))
    own_http = None if is_public_client(ctx.http) else open_http()
    own_pages = PageReader() if ctx.browser is None else None
    c._client, c._pages = own_http or ctx.http, own_pages or ctx.browser
    try:
        obs = await c.profile_company(inp.company_id, inp.domain, inp.locality)
    finally:
        if own_http is not None:
            await own_http.aclose()
        if own_pages is not None:
            await own_pages.aclose()
    draft = Draft()
    for row in obs:
        module, cap = READ_BY.get(row["signal_code"], ("website", "read_site"))
        draft.facts.append(Fact(
            row["signal_code"], value_num=row["value_num"], value_text=row["value_text"],
            confidence=row["confidence"], source_url=row["evidence_url"],
            collector=row["collector"], module=module, capability=cap))
    draft.pages = [Page(url, html, c._method(url))
                   for url, html in c.__dict__.get("pages_read", {}).get(inp.company_id, [])]
    draft.contacts = [Contact("phone", hit.number, display=hit.display,
                              confidence=hit.confidence, source_url=url,
                              module="phones", capability="site_phones")
                      for url, hit in c.__dict__.get("phone_hits", {}).get(inp.company_id, [])]
    if c.contacts:
        _reach(draft, c, inp)
    better = c.better_name(inp.company_id, inp.name, inp.domain)
    if better:
        draft.values["name"] = better
    return draft


# Social profiles and messaging links a company's own pages point to, as
# (kind, pattern over an href). Harvey never reads these networks: a link on
# the company's own site is the only evidence it keeps.
SOCIAL_LINKS = (
    ("linkedin", re.compile(r"(?i)^https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school|in)/([^/?#\s]+)")),
    ("facebook", re.compile(r"(?i)^https?://(?:www\.|m\.|web\.)?(?:facebook|fb)\.com/([^?#\s]+)")),
    ("instagram", re.compile(r"(?i)^https?://(?:www\.)?instagram\.com/([^/?#\s]+)")),
    ("whatsapp", re.compile(r"(?i)^https?://(?:wa\.me/|(?:api|web)\.whatsapp\.com/send/?\?(?:[^#\s]*&)?phone=)\+?(\d{6,15})")),
)
# Paths of those networks that are a share button, a login or a post, not
# the company's own profile.
_NOT_A_PROFILE = frozenset({
    "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login",
    "l.php", "events", "watch", "groups", "hashtag", "p", "reel", "reels", "explore",
    "accounts", "stories", "people", "pages", "profile.php", "home.php", "intent",
})
_HREF = re.compile(r"""(?i)href\s*=\s*["']([^"'>\s]+)["']""")


def social_links(html: str) -> list[tuple[str, str]]:
    """(kind, profile URL) for each social profile or messaging link a page
    links to, once each, normalised (no query, no trailing slash)."""
    out: list[tuple[str, str]] = []
    for href in _HREF.findall(html or ""):
        href = html_unescape(href).strip()
        for kind, pattern in SOCIAL_LINKS:
            m = pattern.match(href)
            if not m:
                continue
            if kind == "linkedin":
                url = f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2).strip('/')}"
            elif kind == "whatsapp":
                url = f"https://wa.me/{m.group(1)}"
            else:
                path = m.group(1).strip("/")
                if not path or path.split("/")[0].lower() in _NOT_A_PROFILE:
                    break
                host = "www.facebook.com" if kind == "facebook" else "www.instagram.com"
                url = f"https://{host}/{path.split('/')[0] if kind == 'instagram' else path}"
            if (kind, url) not in out:
                out.append((kind, url))
            break
    return out


def _reach(draft: Draft, c: "ProfileCollector", inp) -> None:
    """The ways the pages just read say to reach the company, beside the
    phones: the addresses they publish (an address on the company's own
    domain is surely its own, any other only maybe), its contact form, the
    social profiles it links to, and the people its team page names."""
    from harvey.collectors.people import emails_on_page
    from harvey.modules.api import Person

    own = (inp.domain or "").lower().removeprefix("www.")
    seen: set[tuple[str, str]] = set()

    def add(kind, value, url, confidence=1.0, display=""):
        if (kind, value) in seen:
            return
        seen.add((kind, value))
        draft.contacts.append(Contact(kind, value, display=display, confidence=confidence,
                                      source_url=url, module="website", capability="read_site"))

    for page in draft.pages:
        for addr in emails_on_page(page.html):
            host = addr.rpartition("@")[2].lower()
            mine = bool(own) and (host == own or host.endswith("." + own))
            add("email", addr.lower(), page.url, 1.0 if mine else 0.5)
        for kind, url in social_links(page.html):
            add(kind, url, page.url)
    form = c.__dict__.get("forms", {}).get(inp.company_id)
    if form:
        add("contact_form", form, form)
    team = c.__dict__.get("team", {}).get(inp.company_id, [])
    for person in team:
        draft.people.append(Person(
            first_name=person["first_name"], last_name=person["last_name"],
            title=person.get("title") or "", source_url=person.get("url") or "",
            fields={k: person[k] for k in ("owner", "rank", "source", "evidence") if k in person},
            module="people", capability="find_people"))
    learned = site_pattern([x.value for x in draft.contacts if x.kind == "email"],
                           [(p["first_name"], p["last_name"]) for p in team], own)
    if learned:
        draft.values["email_pattern"] = learned


def site_pattern(addresses, named, domain: str) -> dict:
    """What the addresses a company's own pages publish (obfuscated ones
    decoded) say about how it spells its people's addresses, in the shape
    an email lookup's ``learned`` has (ingest.learned_pattern keeps it):
    the pattern, how sure, and whether an address was tied to someone named
    on the site ("site_address") or only shows a format
    ("site_address_guess", which Hunter still gets one look past). {} when
    they say nothing (role mailboxes such as office@ never do)."""
    from harvey.integrations.email_finder import site_email_evidence

    if not domain:
        return {}
    known, pattern, confidence = site_email_evidence(list(addresses), list(named), domain)
    if not pattern:
        return {}
    return {"pattern": pattern, "confidence": confidence,
            "source": "site_address" if known else "site_address_guess"}
