"""The signal vocabulary Harvey proposes, and the user confirms.

Harvey never decides on its own what makes a good prospect. It proposes a
catalog of signals it knows how to collect — each with a plain-language
description, what it costs, and why it might matter — and the user confirms,
edits, or rejects them in the dashboard before any collection runs.

Only ``confirmed`` signals get collected. That keeps spend intentional and
keeps the resulting cohorts explainable: a prospect list is a query over
signals a human chose, not a black box.

Design note: every signal is recorded as an OBSERVATION (a row), never a
column. A new signal needs no migration, history comes free, and confidence
and provenance travel with the fact.
"""

# category: discovery | profile | people | verification
# value_type: num | text | bool
# cost_note: what collecting it actually costs, in plain language
# absence: what a company with no current observation of the signal reads as
#   in a cohort. "unknown" (the default): nobody looked, or the collector
#   only writes what it finds. "false": the collector looks at every company
#   and writes only positives, so no row means no. A collector that looks at
#   one company and finds nothing should write an explicit negative row
#   (value_num 0) instead, which reads as false whatever this says.
#
# How each signal records "no" today. None is "false": every collector here
# visits only some companies, so no row can mean "not looked at yet".
#   SERP_RANK, IN_LOCAL_PACK, MAPS_RANK  0 for a phrase that no longer finds a
#       business it found before; never searched, or never found, has no row
#   FOUND_IN_SERP, REVIEW_RATING, REVIEW_COUNT  positives only
#   NO_WEBSITE, UNCLAIMED_LISTING  1 or 0 from the listing (0 when it names a
#       site or says claimed); a listing that doesn't say writes nothing
#   ACTIVITY_CODE  positives only (one row per CAEN class the import kept)
#   RUNNING_GOOGLE_ADS, RUNNING_META_ADS, NO_SCHEMA_MARKUP, NO_ONLINE_BOOKING,
#   BLOCKS_AI_CRAWLERS, ACCELERATOR, FUNDING_ROUND  1 or 0 whenever the
#       homepage was read; an unreachable site writes nothing
#   YC_BACKED  1 or 0, except that silence leaves a search hit standing
#   HIRING_ROLE  0 when a job board answered with no matching role; a careers
#       page that lists none writes nothing
#   OPEN_ROLES  the count, 0 included, when a job board answered
#   INCUMBENT_AGENCY, TECH_STACK, SITE_PAGE_COUNT, BLOG_STALE  positives only:
#       an uncredited agency or a missing sitemap is not a "no"
#   CONTACT_FOUND, DECISION_MAKER_TITLE, REGISTRY_VERIFIED, LIKELY_OWNER,
#   EMAIL_PATTERN, EMAIL_STATUS, CONTACT_FORM_URL, PHONE_NUMBER  positives
#       only: a page Harvey didn't read can still have one
SIGNAL_CATALOG: list[dict] = [
    # ── Discovery: who exists, and how visible are they ──
    {
        "code": "SERP_RANK",
        "label": "Search rank position",
        "description": (
            "Where the business ranks organically for a '<service> <city>' "
            "search (the search phrase is stored with it). The most useful "
            "prospecting signal there is: positions 11-30 are the sweet spot — "
            "visible enough to be trying, not winning yet. Top 10 are already "
            "winning (harder sell); 31+ are cheaper but less motivated. Rank "
            "depends on where the searcher stands: this is the view from the "
            "city centre."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "~$0.006 per search at depth 30 (paid SERP API, Live endpoint)",
        "confidence_floor": 0.0,
    },
    {
        "code": "IN_LOCAL_PACK",
        "label": "In the Google local 3-pack",
        "description": (
            "Holds one of the three map slots above the organic results for a "
            "'<service> <city>' search (value = slot 1-3; ads excluded). These "
            "businesses are already winning local search — exclude them to "
            "find the ones who aren't, or target them with something else."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "included in the organic rank search above",
        "confidence_floor": 0.0,
    },
    {
        "code": "MAPS_RANK",
        "label": "Google Maps rank position",
        "description": (
            "Where the business appears in Google Maps for a '<service> <city>' "
            "search from the city centre (the search phrase is stored with it). "
            "Outside the top 3 on the map means outside the local pack most "
            "searchers see. Also finds businesses with no website at all."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "~$0.002 per Maps search of up to 100 results (paid SERP API)",
        "confidence_floor": 0.0,
    },
    {
        "code": "FOUND_IN_SERP",
        "label": "Discovered via search",
        "description": "The business appeared in a target-market search — how Harvey found it at all.",
        "category": "discovery",
        "value_type": "text",
        "collector": "discover",
        "cost_note": "included in the search above",
        "confidence_floor": 0.0,
    },

    {
        "code": "NO_WEBSITE",
        "label": "No website at all",
        "description": (
            "The business has a listing and a phone number but no site. If you "
            "sell websites this is the highest-intent signal that exists — and "
            "it is a filter you can query for directly, not something you infer."
        ),
        "category": "discovery",
        "value_type": "bool",
        "collector": "discover",
        "cost_note": "included in the business listing lookup",
        "confidence_floor": 0.0,
    },
    {
        "code": "UNCLAIMED_LISTING",
        "label": "Unclaimed Google listing",
        "description": (
            "Nobody has claimed their Google Business Profile, so they are not "
            "managing how they show up locally. Strong signal for anyone "
            "selling local marketing."
        ),
        "category": "discovery",
        "value_type": "bool",
        "collector": "discover",
        "cost_note": "included in the business listing lookup",
        "confidence_floor": 0.0,
    },
    {
        "code": "REVIEW_RATING",
        "label": "Review rating",
        "description": (
            "Their average star rating. Useful in both directions: a low "
            "rating is a reputation problem to solve, a high one with few "
            "reviews is proof they are good but invisible."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "included in the business listing lookup",
        "confidence_floor": 0.0,
    },
    {
        "code": "REVIEW_COUNT",
        "label": "Number of reviews",
        "description": (
            "How many reviews they have. A rough proxy for size and for how "
            "much attention they pay to their online presence."
        ),
        "category": "discovery",
        "value_type": "num",
        "collector": "discover",
        "cost_note": "included in the business listing lookup",
        "confidence_floor": 0.0,
    },

    # ── Profile: what they are (free — HTML Harvey already fetches) ──
    {
        "code": "ACTIVITY_CODE",
        "label": "Registered activity (CAEN)",
        "description": (
            "An activity the company is authorised for in Romania's trade "
            "registry (ONRC), as a 4-digit CAEN class such as 8623 for dental "
            "practice: one row per class that matched your search words "
            "(every class, when the import named no activity). "
            "What the business is legally set up to do, as filed, not what "
            "its website says. The dataset and its date are kept with it."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "registry",
        "cost_note": "free (ONRC open data, data.gov.ro)",
        "confidence_floor": 0.0,
    },
    {
        "code": "INCUMBENT_AGENCY",
        "label": "Has a marketing/web agency",
        "description": (
            "An agency credited in the site footer ('Website by X'). High-value: "
            "it tells you who you're displacing, and every detection builds a "
            "roster you can target as a whole book of business. Only recorded "
            "when the credit wording is explicit — a wrong incumbent is worse "
            "than none."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.75,
    },
    {
        "code": "RUNNING_GOOGLE_ADS",
        "label": "Running Google Ads",
        "description": "Google Ads tag on the site — they are spending on acquisition TODAY. Strong budget signal.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "RUNNING_META_ADS",
        "label": "Running Meta/Facebook Ads",
        "description": "Meta Pixel on the site — actively spending on paid social.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "TECH_STACK",
        "label": "Website platform / tools",
        "description": "What the site is built on and which tools it runs (WordPress, Shopify, HubSpot, chat widgets, booking tools).",
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_SCHEMA_MARKUP",
        "label": "Missing structured data",
        "description": "No JSON-LD schema markup — a concrete, checkable SEO gap you can name in an email.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "NO_ONLINE_BOOKING",
        "label": "No online booking/scheduling",
        "description": "No booking or scheduling widget detected — leads have to phone in, so after-hours demand is lost.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "SITE_PAGE_COUNT",
        "label": "Site size",
        "description": "Page count from sitemap.xml. A very thin site suggests little investment; a large stale one suggests neglect.",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "free (sitemap.xml)",
        "confidence_floor": 0.0,
    },
    {
        "code": "BLOG_STALE",
        "label": "Content gone stale",
        "description": "No new content in a long time — content marketing started and abandoned.",
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "free (sitemap.xml)",
        "confidence_floor": 0.0,
    },
    {
        "code": "BLOCKS_AI_CRAWLERS",
        "label": "Blocks AI crawlers",
        "description": "robots.txt blocks AI crawlers — they'll be invisible in AI search answers.",
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (robots.txt)",
        "confidence_floor": 0.0,
    },
    {
        "code": "YC_BACKED",
        "label": "Backed by Y Combinator",
        "description": ("The company says on its own site that it's YC-backed (a batch like "
                        "W24, a YC badge, or a link to its YC page). Pairs with the YC-startups "
                        "discovery preset and the startup-founder writing playbook."),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "ACCELERATOR",
        "label": "Went through an accelerator",
        "description": ("The company says on its own site that an accelerator backed it: Techstars, "
                        "500 Global, Antler, Entrepreneur First, South Park Commons, Neo, AI Grant, "
                        "a16z speedrun, Pioneer or Plug and Play, with the cohort when stated. YC is "
                        "YC_BACKED. Pairs with the accelerator discovery preset and the startup-founder "
                        "writing playbook."),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage and About page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "FUNDING_ROUND",
        "label": "Announced a funding round",
        "description": ("The company announces its own raise on its site (\"we raised\", \"Acme raises "
                        "$12M Series A\"): round, amount and date when stated. Raises over 18 months old "
                        "aren't written to as news. Pairs with the recently-funded discovery preset."),
        "category": "profile",
        "value_type": "bool",
        "collector": "profile",
        "cost_note": "free (homepage, About, press page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "HIRING_ROLE",
        "label": "Hiring for a relevant role",
        "description": (
            "An open role that implies they're buying in your category right now: one "
            "of your hiring_signals roles (or ICP titles) on the Greenhouse, Lever, "
            "Ashby or Workable board their site links to, with department, location "
            "and posting date, or a role listed on their careers page."
        ),
        "category": "profile",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (careers page or public job-board feed)",
        "confidence_floor": 0.0,
    },
    {
        "code": "OPEN_ROLES",
        "label": "Open roles on their job board",
        "description": (
            "How many roles are open on the Greenhouse, Lever, Ashby or Workable board "
            "their site links to. A growing team is a budget signal; zero is a quiet one."
        ),
        "category": "profile",
        "value_type": "num",
        "collector": "profile",
        "cost_note": "free (public job-board feed, one request)",
        "confidence_floor": 0.0,
    },

    # ── People: who decides ──
    {
        "code": "CONTACT_FOUND",
        "label": "Named contact found",
        "description": ("A named person scraped from the team/about page, with their title, "
                        "or an administrator named in Romania's trade registry (ONRC)."),
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (team page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "DECISION_MAKER_TITLE",
        "label": "Decision-maker title",
        "description": ("Title matches an owner/decision-maker pattern (Owner, President, Practice "
                        "Manager, Marketing Director), or the company's administrator in the registry."),
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (team page)",
        "confidence_floor": 0.0,
    },
    {
        "code": "REGISTRY_VERIFIED",
        "label": "Confirmed in a public registry",
        "description": (
            "The person was confirmed in an authoritative public registry "
            "(licensing board, professional registry). Also filters out "
            "scraping artifacts — a name that matches nothing usually wasn't a person."
        ),
        "category": "people",
        "value_type": "text",
        "collector": "people",
        "cost_note": "free (public registry API)",
        "confidence_floor": 0.0,
    },
    {
        "code": "LIKELY_OWNER",
        "label": "Likely the owner",
        "description": (
            "The company's own website says this person owns or founded it: an "
            "Owner/Founder/President title, a sentence like 'owned and operated by' "
            "or a schema.org founder. Former, retired and late owners don't count. "
            "The evidence is kept with the fact."
        ),
        "category": "people",
        "value_type": "bool",
        "collector": "people",
        "cost_note": "free (company website)",
        "confidence_floor": 0.0,
    },

    # ── Verification ──
    {
        "code": "EMAIL_PATTERN",
        "label": "Company email pattern",
        "description": "The domain's address format (first.last@, flast@). One real address cracks the whole company.",
        "category": "verification",
        "value_type": "text",
        "collector": "verify",
        "cost_note": "free, or ~1 lookup credit",
        "confidence_floor": 0.0,
    },
    {
        "code": "EMAIL_STATUS",
        "label": "Email deliverability",
        "description": "Whether the address is verified, catch-all/risky, an unverified guess, or invalid.",
        "category": "verification",
        "value_type": "text",
        "collector": "verify",
        "cost_note": "1 verification credit (free tiers available)",
        "confidence_floor": 0.0,
    },
    {
        "code": "CONTACT_FORM_URL",
        "label": "Contact form available",
        "description": (
            "A contact form on the site. Worth knowing because it reaches the "
            "business without needing an email address at all — and roughly "
            "half of domains can't be email-verified."
        ),
        "category": "verification",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (site HTML)",
        "confidence_floor": 0.0,
    },
    {
        "code": "PHONE_NUMBER",
        "label": "Phone number on the site",
        "description": (
            "A phone number the business publishes on its own pages (a tap-to-call "
            "link, its schema.org markup, or one written out), one row per number "
            "in +40… form. Fax numbers are left out; a tap-to-call link or the "
            "markup counts with full confidence, a number in the text with a "
            "little less. The company's phone is "
            "filled from its site either way when no listing gave one; this "
            "keeps every number found, for the export's other_phones."
        ),
        "category": "verification",
        "value_type": "text",
        "collector": "profile",
        "cost_note": "free (site HTML)",
        "confidence_floor": 0.0,
    },
]


ABSENCE_VALUES = ("unknown", "false")


def absence(code: str) -> str:
    """How a company with no current observation of ``code`` reads."""
    for sig in SIGNAL_CATALOG:
        if sig["code"] == code:
            return sig.get("absence", "unknown")
    return "unknown"


async def seed_signal_catalog(state) -> int:
    """Register the catalog as PROPOSED signals awaiting user confirmation.

    Idempotent, and never overrides a decision the user already made: an
    existing signal keeps its confirmed/rejected status.
    """
    for sig in SIGNAL_CATALOG:
        await state.upsert_signal_code(
            sig["code"],
            label=sig["label"],
            description=sig["description"],
            category=sig["category"],
            value_type=sig["value_type"],
            collector=sig["collector"],
            cost_note=sig["cost_note"],
            confidence_floor=sig.get("confidence_floor", 0.0),
        )
    return len(SIGNAL_CATALOG)
