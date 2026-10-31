# Harvey RO

> **Based on [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers (MIT).**
> Harvey RO is an independent project that is turning it into a contact finder
> for Romanian businesses: ONRC registry import, activity search by CAEN code
> in words, Romanian website reading, and phone extraction. It does not track
> or contribute back to the original project. Credit for the original design
> and code belongs to Harvey's author and contributors; see `LICENSE`.

**An autonomous sales agent that runs on your Claude Code or Codex subscription.**

Harvey finds businesses worth selling to, learns something specific about each one, writes cold email that references it, sends it, reads the replies, and works the conversation toward a meeting. It runs on your machine, in a loop, on its own.

The unusual part: **there is no model API bill.** Harvey uses your Claude Pro or Max subscription through Claude Code, or your ChatGPT subscription through Codex. Model calls use the plan you already pay for, without an API key or per-token API billing. Claude is the default; [Codex is an opt-in provider on macOS and Linux](#using-a-chatgpt-plan-instead-codex).

```
$ harvey run

============================================================
Harvey is online. Always Be Closing.
============================================================
Checking pipeline state...
Decision: handle_replies (2 new replies)
  handler   → Elena Park, Harbor Dental Studio     interested, answer drafted
  handler   → Priya Nair, Highline Roofing         objection, answer drafted
Decision: write_campaign (3 new prospects with no drafts)
  writer    → 2-email sequence: Phoenix HVAC hiring a dispatcher
  outbox    → 5 emails waiting for your approval
Cycle complete. Sleeping for 15 minutes.
```

And the dashboard it keeps, at `localhost:5555`. In this order: the Today screen, a
conversation Harvey handed back because a prospect asked for a contract, approving
a drafted reply from the outbox, confirming a signal, building a cohort from
confirmed signals, searching contacts, and the same screen in dark mode:

![Harvey's dashboard: the Today screen, a conversation handed back for a human, approving a reply in the outbox, confirming a signal and building a cohort, searching contacts, then dark mode](docs/dashboard.gif)

<sub>The demo data is invented: "Bookwell" selling online booking to roofers, dental practices and HVAC shops, on `.test` domains.</sub>

---

## Table of contents

- [Why the subscription thing matters](#why-the-subscription-thing-matters)
- [What Harvey actually does](#what-harvey-actually-does)
- [Quick start](#quick-start)
- [You confirm what it looks for](#you-confirm-what-it-looks-for)
- [The prospecting pipeline](#the-prospecting-pipeline)
- [The skills library](#the-skills-library)
- [The sub-agents](#the-sub-agents)
- [Nothing sends without you](#nothing-sends-without-you)
- [The dashboard](#the-dashboard)
- [How the data is stored](#how-the-data-is-stored)
- [Romanian public data](#romanian-public-data)
- [Configuration](#configuration)
- [Using a ChatGPT plan instead (Codex)](#using-a-chatgpt-plan-instead-codex)
- [Legal and deliverability](#legal-and-deliverability)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [Philosophy](#philosophy)
- [Roadmap](#roadmap)

---

## Why the subscription thing matters

Every other autonomous sales agent bills you per token. That is the whole reason they cost what they cost: an agent that thinks in a loop, all day, is an agent that burns API credits in a loop, all day. Vendors solve this by thinking less — shorter prompts, cheaper models, fewer passes.

Harvey uses the subscription-backed CLI for the provider you choose: `claude` for Claude Code, or an isolated `codex app-server` for Codex. Both return text with model tools disabled. Harvey handles the prospecting and mail operations itself.

Claude calls run with none of your own Claude Code setup: no `CLAUDE.md` (yours, the project's or any parent folder's), no hooks, skills, plugins, MCP servers or settings files, no tools, in an empty temporary folder, and nothing saved to your Claude transcripts. Your login (subscription or `claude setup-token`) and any admin-managed policy still apply. This needs a Claude CLI with `--safe-mode`; with an older one Harvey pauses instead of sending prompts unisolated and says to run `claude update`. To check this offline against your installed CLI, with no credentials or network: `HARVEY_CLAUDE_CLI_TESTS=1 pytest tests/test_claude_cli_isolation.py`.

Your selected subscription covers model calls, subject to its usage limits. That lets Harvey use its skills library for personalization and reply handling without a separate model API bill.

**What this costs you:**

| | |
|---|---|
| Claude Pro/Max or a ChatGPT plan with Codex access | your existing subscription |
| Finding businesses | **$0** on the default source, or ~$0.37 per 1,000 on the cheapest paid one |
| Reading their websites | **$0** — three HTTP requests each, no model call |
| Writing and sending | your subscription + a mailbox (~$7/mo Google Workspace) |
| Prospecting tools | **none.** No Apollo, no ZoomInfo, no Clearbit, no Clay. |

For comparison, the tools Harvey replaces start at $250–500/month — and they are weakest precisely where Harvey is strongest: small local businesses with 5–50 people, where single-provider contact coverage caps out around 30% and micro-businesses are frequently absent entirely.

**Harvey also budgets itself.** It reads the active provider's subscription quota and throttles to leave headroom for your own Claude or Codex work. Set `max_daily_claude_percent: 80` for either provider—the setting's name predates Codex support. If quota cannot be read, Harvey reports it as unavailable and falls back to its daily call budget.

Harvey runs locally with your own login. Check the terms and usage limits of the provider you choose.

---

## What Harvey actually does

Harvey runs a heartbeat: wake up, check the budget, decide what most needs doing, do it, log it, sleep. Every 15 minutes by default.

```
                 ┌──────────────────────────────────────┐
                 ↓                                      │
   Wake  →  Quiet hours?  →  Budget OK?  →  Decide  →  Act  →  Log  →  Sleep
                 │               │
              (sleep)         (sleep)
```

It decides deterministically, not by asking a model what to do — that would burn a call on something derivable from four counts:

```
handle replies  >  send campaigns  >  write campaigns  >  prospect  >  idle (analyze)
```

Outbox draining rides along on **every** cycle regardless, because it has to happen on schedule.

The heartbeat is the Email module's: it reads replies, writes and sends, and with the Email module off it does nothing. Finding companies, reading their sites, finding contacts and loading public data are **jobs** the worker runs (see [The queue](#the-queue-background-jobs)), whatever the Email switch says. The heartbeat never looks for people: the [contact finder](#the-contact-finder) does, on its own.

### The queue (background jobs)

Every long run is a job on the **lane** of the source it calls: a discovery search on its search module's lane (one job per search, so a run over five cities is five jobs), each company's site read on the website reader's lane, a registry load or pull on the registry's, a load or check of the financial statements or the taxpayer file on theirs, and checks at ANAF a hundred companies a job on ANAF's. A lane runs its jobs one at a time, in the order they were asked for; lanes run side by side, so a slow TinyFish search never holds up an OpenStreetMap one. The pace each service allows still holds request by request.

- **Asking twice.** A second Descoperă run, of the same source or another, is queued. The same search (source, phrase, place and settings) asked again while it still waits is refused, naming the job already there.
- **Money.** Each paid job reserves its estimate from its run's spend cap before it spends; the cap and the stop are read between pages, and each page's cost is recorded as it arrives, so the cap holds even when a worker is killed mid-search. A search the cap stops is **paused (spend limit)**: raise the cap (Coadă, or `harvey jobs resume ID --max-spend N`) and it carries on from the page it reached, without paying for the earlier pages again. `spend.per_day_usd` and `spend.per_month_usd` in harvey.yaml (UTC, none by default) limit every run together.
- **Restarts.** A worker that is stopped (Ctrl-C, `docker stop`) lets a running search finish its page, keeps what it found and saves where it got to; the search resumes from there. A worker killed outright (no time to stop) loses the pages that search fetched since it last stopped: they are fetched again when it resumes, and paid for again, within its cap. Jobs left running on a lane nobody runs go back to the queue.
- **Failures.** A passing problem (the network, a 5xx, a 429) is tried again later, waiting longer each time, then the job fails with the message; a search whose later page failed that way keeps the pages it had and carries on from the failed one. Only failed tries count: waiting for an allowance or a restart doesn't use them up. A source out of credit, or refusing its key, pauses its lane until you resume it; an hourly allowance used up just waits for the next hour.
- **Who runs them.** One worker per database: the dashboard runs one inside its own process (the usual install runs only the dashboard), `harvey worker` and `harvey run` run one without it, and a lease decides which of them works. CLI commands queue their work and then wait and print its progress, running it themselves when no worker is running (and then only the lanes their own jobs are on, so a dashboard started meanwhile runs the rest); `--background` queues it and returns.
- **Nothing re-runs on its own**, except a public-data source you set to update itself. Quiet hours are the Email module's only: the worker runs at any hour.

The dashboard's **Coadă** (Queue) page shows each lane: what is running (pages, results, cost so far), waiting, paused, done and failed, with pause/resume per lane and cancel/retry/resume per job. A running search can be cancelled or paused (it stops after its page); a running load or check runs to its end. The badge beside it in the menu counts what is running and waiting, and Descoperă's result card links to its run's jobs.

---

## Quick start

### Prerequisites

- **Python 3.11+** — check with `python3 --version`. macOS ships 3.9; if that's what you have, `brew install python@3.13` first.
- An active **Claude Pro or Max** subscription, with the CLI logged in (`claude login`) — or, opt-in, a **ChatGPT plan with the Codex CLI** ([see below](#using-a-chatgpt-plan-instead-codex))
- A mailbox to send from — Gmail/Workspace recommended, on a *dedicated secondary domain*. Microsoft 365 works too ([setup](docs/microsoft365.md))
- A warmup service on that mailbox — we recommend [TrulyInbox](https://trulyinbox.com/?via=harvey). It warms a new mailbox before cold email, keeps it out of spam after, and Harvey reads its inbox rate to tell you when you're ready ([Warmup](#warmup-with-trulyinbox))

### Easiest: let Claude set it up

```bash
git clone https://github.com/Mliviu79/harvey-ro.git
cd harvey-ro
claude
```

Then say: **"set up Harvey for me"**. The repo ships a `CLAUDE.md` that turns Claude Code into the setup wizard — it checks what state you're in, installs what's missing, asks what it needs, and trains Harvey on your product.

### Or do it yourself

```bash
# 1. Install — note python3.13, not python3. On macOS bare `python3` is the
#    system 3.9, and a venv built from it can't run Harvey.
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .

# 2. Configure — every variable is documented inline with where to get it
cp .env.example .env && $EDITOR .env

# 3. Learn your product from your website (writes harvey.local.yaml; re-running
#    it updates the product, targeting and company and keeps your other settings)
harvey train https://your-company.com

# 4. Choose what makes a good prospect (see below — this one matters)
harvey signals --confirm free

# 5. Find businesses. Free source, no account needed.
harvey discover

# 6. Watch it work
harvey dashboard          # prints a private link to http://127.0.0.1:5555
harvey run
```

### Or deploy with Docker (Linux)

Docker Compose runs the dashboard; start and stop the heartbeat from its
Actions card. Host networking preserves the dashboard's loopback-only bind at
`127.0.0.1:5555`. This Compose setup supports native Linux; use the pip install
above on macOS or Windows. On a remote Linux host, reach the dashboard through
an SSH tunnel rather than exposing its port to the network.

```bash
git clone https://github.com/Mliviu79/harvey-ro.git
cd harvey-ro

# Create the credentials file only if you do not already have one.
test -e .env || cp .env.example .env

# Run as your Linux user, never root. Set these again when rebuilding.
export HARVEY_UID=$(id -u) HARVEY_GID=$(id -g)

# Sign in to Claude on the host first; ~/.claude must already exist.
# The image ships Claude only; Codex is not included.
docker compose build
# Setup also writes BROWSER_CDP_TOKEN, the obscura browser's password.
docker compose run --rm harvey setup

docker compose up -d
docker compose logs harvey
# Open the private unlock link printed in the logs.
```

**Browsers.** The Harvey image ships no browser. Compose also starts
`obscura`, a light headless browser that renders sites whose content
arrives by JavaScript (plain HTTP is still tried first; only JavaScript
shells and blocked pages go to the browser). Harvey uses it by default;
`BROWSER_CDP_URL=off` in `.env` keeps every site on plain HTTP. Obscura
needs `BROWSER_CDP_TOKEN` in `.env`, which setup writes. On an install set
up before that, add one (until then the obscura container keeps restarting
and Harvey reads over HTTP), then `docker compose up -d`:

```bash
grep -q '^BROWSER_CDP_TOKEN=.' .env || printf '\nBROWSER_CDP_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
```

Chromium is optional, for LinkedIn and for the sites that refuse Obscura
(Cloudflare and others that check the TLS fingerprint): start it with
`docker compose --profile chromium up -d` and set
`BROWSER_CDP_FALLBACK_URL=http://127.0.0.1:9231` in `.env`. Both listen on
the host's loopback only (9230, 9231). Without either, Harvey reads sites
over HTTP only and says so once in the log.

Obscura refuses private and local addresses on its own; Chromium doesn't,
and a public page can redirect or connect it to services on your machine
(the dashboard, databases). So the profile also starts `chromium-egress`,
a proxy that every Chromium request goes through and that refuses private,
local and metadata addresses (`docker/browser-egress.conf`). If you point
`BROWSER_CDP_FALLBACK_URL` at a Chromium of your own instead, give it a
proxy like that one.

Compose mounts the checkout directory at `/app`. Setup credentials, both YAML
config files, training output, knowledge files, and `data/` remain in that
checkout when containers are recreated. Directory mounting also permits atomic
configuration saves; do not replace it with individual config-file mounts.
Harvey loads `.env` directly. Restart the container after editing credentials.
The checkout and Claude auth directory must be writable by the selected user.

To train later, use `docker compose run --rm harvey train <product-url>`;
`harvey.local.yaml` is saved directly to the checkout. Back up this file, `.env`,
`skills/`, and `data/` before moving installations. The running code also comes
from the checkout: restart after updating it, and rebuild when dependencies or
the Dockerfile change. The build context excludes local data and credentials.

The dashboard listens on 5555. If you change the port in the Compose `command`,
change the Dockerfile health-check port too and rebuild. Stop the service with
`docker compose down`; its files remain in the checkout. Use a host install if
you need the Codex provider.

---

## You confirm what it looks for

This is the part that makes Harvey different from a black box, and it is deliberately not skippable.

Harvey knows how to collect **31 signals** about a business. It does not collect any of them until you say so. On first run it *proposes* the catalog; you confirm, skip, or reject each one in the dashboard or from the terminal:

```bash
harvey signals                       # review the catalog
harvey signals --confirm free        # everything that costs nothing (26 of them)
harvey signals --confirm SERP_RANK   # a paid one, opted into explicitly
```

Every signal shows what it is in plain language, **what it costs**, and how many companies already carry it.

| | Signal | What it tells you |
|---|---|---|
| **Discovery** | `SERP_RANK` | Where they rank for their own core search, stored with the search phrase. Positions 11–30 are the sweet spot: visible enough to be trying, and losing. |
| | `IN_LOCAL_PACK` / `MAPS_RANK` | Whether they hold one of the three map slots above the results, and where they sit in Google Maps. |
| | `NO_WEBSITE` | They have a listing and a phone and no site. If you sell websites, the highest-intent signal that exists. |
| | `UNCLAIMED_LISTING` | Nobody has claimed their Google Business Profile. |
| | `REVIEW_RATING` / `REVIEW_COUNT` | Good but invisible, or visible but struggling. |
| **Profile** | `INCUMBENT_AGENCY` | Who currently has the account, credited in their own footer. Every detection builds a roster you can target as a whole book of business. |
| | `RUNNING_GOOGLE_ADS` / `RUNNING_META_ADS` | They are spending on acquisition *today*. The strongest budget signal available for free. |
| | `NO_ONLINE_BOOKING` | Leads have to phone in, so after-hours demand is lost. |
| | `NO_SCHEMA_MARKUP` | A concrete, checkable SEO gap you can name in an email. |
| | `BLOG_STALE` | Content marketing started and abandoned. |
| | `SITE_PAGE_COUNT`, `TECH_STACK`, `BLOCKS_AI_CRAWLERS` | Size, platform, AI-search visibility. |
| | `HIRING_ROLE` / `OPEN_ROLES` | What they're hiring for, and how many roles are open. When their site links a Greenhouse, Lever, Ashby or Workable board, Harvey reads that board's public feed (one request): roles matching `icp.hiring_signals` (else `icp.titles`), with department, location, posting date and the posting's URL; when the board lists none, it records "not hiring" so a closed role stops counting. Otherwise it reads the careers page. Set `hiring_signals` to the roles that mean they're buying: the `titles` fallback names who you email, so "CEO" only matches a CEO opening (never "Assistant to the CEO"), which is rarely the signal you want. |
| | `YC_BACKED`, `ACCELERATOR`, `FUNDING_ROUND` | A startup, as its own site says: YC or another accelerator (with the batch or cohort), or a raise it announced (round, amount, date). |
| **People** | `CONTACT_FOUND`, `DECISION_MAKER_TITLE`, `LIKELY_OWNER`, `REGISTRY_VERIFIED` | Who decides, and whether they own the place. |
| **Contact** | `EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL` | Whether you can actually reach them. |

### Then a cohort is a query, not a list

Once signals are confirmed, the dashboard's cohort builder turns them into a target set — pick what a good prospect **must have**, what **disqualifies** them, and a **range** for numeric signals (organic rank 11 to 30), and it counts the matches live:

```
Has a marketing agency  AND  running Google Ads  AND NOT  already has online booking
→ 23 companies
```

That is a campaign with a reason behind it. You know exactly why each of those 23 is on the list, and so does the email Harvey writes them.

Each condition is true, false or **not checked yet**, and every company lands in one of three tabs: **match** (every condition checked and met), **possible** (nothing rules it out, but something isn't checked yet) and **no match**. "NOT running ads" counts only companies checked and found without them, never ones nobody looked at. A disqualifier alone is a cohort too. The Signals tab shows, per signal, how many companies are yes, no and not checked, and the CSV export has a `verdict` column.

---

## The prospecting pipeline

Four stages. Each one runs independently, re-runs safely, and states its cost.

### 1. DISCOVER — who exists

The only stage that spends money, so it always estimates first and never calls anything until you press go.

```bash
harvey discover --providers   # the menu, with real prices
harvey discover --estimate    # projected spend, then exits
harvey discover               # go
harvey discover --provider serper,tinyfish --max-spend 2.00   # several sources, one run, one cap
```

On the dashboard's **Descoperă** tab the sources are checkboxes: tick several and they run as one batch under one spend cap, the estimate is their sum, and a preset a source can't search with leaves that source out with a note instead of refusing the run. The ONRC trade registry is one of the sources (every query capability that records companies and says in its manifest how Discover runs it, `discover = "registry_pull"`): ticked, it shows the filters its manifest declares (activity or CAEN code, county, town, turnover, employees, employer, VAT, micro-enterprise tax), its estimate is the free count of matching firms, and it queues a registry pull in the same batch. Each run still working shows each source's progress, with a Stop for the run and for each source. What every source saves feeds the [contact finder](#the-contact-finder).

Every search takes every result the source has, page by page: "dentist
București" is not a page of 100, it is all of them. The one stop is your spend
cap (`--max-spend`, or the cap on the Discover tab), checked before each page,
and the stop button. A run the cap stops says how many results were left
unfetched and what fetching them would cost. For DataForSEO Business Listings,
`--estimate` (and the Discover tab's estimate) first asks how many businesses
each search matches, about a cent a search, so the price is exact ("1,840
businesses, $0.68") before the rest is paid for. Where the source itself stops
serving, the table says so. `--limit` and `--depth` still take fewer when you
want fewer.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** *(default)* | free | unlimited, no account | Trying the whole pipeline before paying anyone. Coverage is thin for businesses without a storefront: OSM maps premises, so whole trades are barely present (under 2,000 roofers and under 2,300 plumbers in the entire US). Harvey says so in the UI rather than quietly under-delivering. |
| **TinyFish** | free | 30 searches a minute, 500 an hour, no card | Finding businesses by phrase (including the startup and hiring presets) at no cost. TinyFish serves at most 11 pages (about 110 results) a phrase. Records no search rank: its ordering isn't Google's and it takes no location, so use Serper or DataForSEO SERP for rank signals. |
| **DataForSEO Business Listings** | $0.372 / 1,000 ($0.012 a page of up to 1,000 + $0.00036 a business) | $1 credit | Local trades, clinics, contractors. Every business the search matches, with no ceiling. Phone, domain, rating, claimed status — and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.002 per 10 results (Live): up to $0.04 a search | $1 credit + free sandbox | Organic rank as the buying signal, plus who holds the local 3-pack, searched from the city centre. DataForSEO stops at 200 results a search. |
| **DataForSEO Google Maps** | $0.002 per 100 results (Live): up to $0.014 a search | $1 credit + free sandbox | Map rank, rating, category and claimed status, including businesses with no website. DataForSEO stops at 700 results a search. |
| **Serper** | ~$0.30–1.00 / 1,000 searches, a search per page of 10 | 2,500 free, no card | The easiest paid one to try. Pages until Google has no more. |
| **Scavio Google Maps** | $0.01 / page (up to 20 listings) | 50 credits, no card | Google Maps listings around each city: phone, website, category, rating and reviews, including businesses with no website. Google Maps serves at most 120 places a search. |
| **Scavio Google Search** | $0.01 / page of 10, every page Google serves | same 50 credits | Rank as the signal, on the same key as Scavio Google Maps. |
| **Brave Search** *(website lookups only)* | $5 / 1,000 searches | $5 of searches every month (about 1,000), card on file | Not a Discover source: Brave's terms forbid building a database of its results. The contact finder uses it to find the website of a company already on file by its name and town; the results are read once and never stored. |

**Selling to startups?** Harvey has a built-in startup skill, made of three
parts:

- **The searches** (each a web search, so Serper or DataForSEO SERP):

  | Preset | Finds | Signal |
  |---|---|---|
  | `yc` (*YC startups*) | Companies whose own site says they're backed by Y Combinator | `YC_BACKED`, with the batch (W24, S23) |
  | `accelerator` (*Accelerator startups*) | Ones backed by Techstars, 500 Global, Antler, Entrepreneur First, South Park Commons, Neo, AI Grant, a16z speedrun or Plug and Play (two searches per industry; the signal also reads Pioneer) | `ACCELERATOR`, with the program and cohort ("Techstars 2024") |
  | `funding` (*Recently funded*) | Companies announcing their own raise ("we raised", "announces $X Series A") in the last 18 months, news sites and funding databases excluded | `FUNDING_ROUND`: round, amount and date |

  `harvey discover --preset accelerator --provider serper`, or the *Look for*
  menu on the Discover tab.
- **The signals:** turn them on in the Signals tab. They read the homepage and
  About page (and for a raise, the page the search found on their own site and
  their press or news page) for a line where the company says it about itself
  ("we went through Y Combinator", "Acme (Techstars '24)", "we raised a $4M
  seed round"), a "Backed by" logo wall, or a link to its own YC page. Customer
  and audience lines ("trusted by YC-backed startups", "we help founders raise
  their seed round") don't count. Many startups never say it on their site, so
  these find the ones that do; a YC or accelerator preset hit counts as a yes too.
  A raise's date comes from its own clause, or from the publish date of a
  press, news or blog post. Money that isn't a raise (loans closed, verdicts
  won, money raised for a charity, an investor's round in another company)
  doesn't count.
- **The writing:** the writer and reply handler use
  `skills/selling_to_startups.md`, a playbook for writing to founders with a
  section on companies that just raised, for YC- and accelerator-backed
  companies and for raises dated within the last 18 months. An older or
  undated raise alone doesn't bring the playbook, and is never written to as
  news.

Harvey never reads ycombinator.com, the accelerators' directories, Crunchbase,
PitchBook or LinkedIn, whose terms forbid scraping them; everything comes from
the companies' own sites and your search provider.

**Looking for companies hiring a role?** `harvey discover --preset hiring
--provider serper` (or *Look for: Companies hiring for your roles*) searches
your industries for companies whose own careers pages list one of your
`icp.hiring_signals` roles (else `icp.titles`). Hosted boards and job sites
are left out of the search, since a board page names a board, not a website
to profile. Confirm `HIRING_ROLE` and profiling reads the board their site links
to and records the role; the search hit alone records nothing.

**Romanian companies, by what they do.** `harvey import-onrc --county BV`
reads ONRC's open data (free) and saves every operating company in the area
with every CAEN class it is authorised for; add words or `--codes` to keep
only some activities. The registry lists codes, so Harvey keeps the
nomenclature next to them: `harvey search` (and the search box on the
Companies tab) finds companies by the code (`8623`, Rev.2 or Rev.3), by the
activity in Romanian or English with or without accents (`stomatologie`,
`dental practice`, `dentist`), by name and by town, all at once
(`harvey search 8623 cristian`). Official class names are bureaucratic, so
each class also carries the everyday Romanian words people type
(`brutărie`, `instalator`, `service auto`, `avocat`, `IT`), kept in
`harvey/reference/caen_cuvinte.csv`: add a word there and search,
`harvey caen` and `import-onrc` all understand it. Companies from maps
carry no CAEN, but their category is searched the same way.

**The whole registry, loaded once.** ONRC publishes one national snapshot per
dataset, so Harvey loads all of it once: `harvey registry load` downloads the
files into `data/onrc/<date>/` (only the ones missing; they are kept) and
builds `data/registry.db`, every firm in Romania with its status, CAEN classes
and administrators' names and roles, in a few minutes. Then
`harvey registry search dentist BV` answers in well under a second, and
`harvey import-onrc dentist --county BV` brings the matching firms into
Harvey to look for their contacts. The dashboard's **Registry** page browses
it with each firm's financials and pulls the firms you pick. A newer dataset is built aside and swapped in whole, one load at a
time; offline, the newest dataset already downloaded is used.

**Every company's yearly figures, also loaded once.** The Ministry of Finance
publishes each company's annual statements on data.gov.ro, a year at a time
(year N around June of N+1, revised a year later with late filers).
`harvey financials load` downloads the latest three published years (about
90 MB a year; only what is missing, kept in `data/mf_financials/<year>/` and
never deleted on its own) and loads them into `data/financials.db`: turnover,
average employees, net profit or loss, equity, debts and assets per CUI and
year, under the same names whatever the year's column numbering (the mapping
is `harvey/modules/ro/mf_financials/indicators.csv`). An empty cell stays
unknown, never 0. `--years 5` (or `all`) keeps more years, `harvey financials
status` shows what is loaded and whether data.gov.ro has something newer, and
`harvey financials check` asks now. The dashboard shows the same under
**Sources → Public data**, with a Load button and an opt-in switch to update on
its own when a new year is out (off by default: nothing downloads unless you
ask). Companies on file with a CUI get `TURNOVER`, `EMPLOYEES`, `NET_RESULT`,
`EQUITY`, `DEBTS`, `TOTAL_ASSETS` (derived: fixed + current assets + prepaid
expenses), `TURNOVER_GROWTH`, `ACCOUNTS_FORM` and `FILED_ACCOUNTS` once you
confirm them, one reading per year; cohort ranges compare the latest year on
file, or a year you name. Registry search, pull and `import-onrc` take
`--min-turnover 1M`, `--max-turnover`, `--min-employees`, `--max-employees`
and `--unknown include|exclude (turnover and headcount alike)`, so the list is narrowed before
anything is pulled or paid for. Sole traders and family businesses (PFA, II,
IF) file no balance sheet: for them a missing statement is "not applicable",
never "didn't file". Nor is a struck-off firm's, or a year's first release:
about one filer in seven only appears in the revised files a year later, so
"didn't file" waits for a year's final release. Credit: Ministerul Finanțelor, data.gov.ro (CC-BY 4.0;
OGL-ROU 1.0 before 2019); the companies JSON export carries it in `notes`.

**One company, however many sources found it.** Every key a company is known by
(CUI, registration number, a map listing's id, its domain, its phones, its name
in its town) is kept, and each new record is scored against them: the same
CUI, registration number or listing is the same record; the same domain counts
0.9 (free mailboxes, directories, social networks, booking platforms, link
shorteners and a site two companies already share never count), the same
phone 0.8 and the same email 0.8 (never one on a free-mail provider such as
gmail.com or yahoo.ro), each counted by how many firms (CUIs) hold it: shared by
two or three firms it only puts a listing up for review, by more it is a chain's
switchboard and counts for nothing, and with no firm behind it more than two
records sharing it count for nothing either. The same name without its
legal form (`SRL`, `S.R.L.`, `PFA`, `LLC`... listed in
`harvey/reference/legal_forms.csv`) in the same town 0.7, and kinds of evidence
add up. At 0.85 or more the records are merged; from 0.6 they stay apart and the
pair waits under **Possible duplicates** on the Companies tab (or
`harvey duplicates`). The list shows groups, not pairs: every pair still to
decide, each side followed to the company it stands for now, joined with the
pairs it shares a company with, so four listings of one business are one
decision. Pick the record to keep (the registry's, or the one holding most, is
chosen for you), untick any that aren't the same company, and merge the rest in
one go, or take some out of the group, or say they are all different.
A contact counts by who holds it, because a contact is many to many: a phone,
email or website only one registered firm has makes the listings without a CUI
holding it that firm's branches (merged on their own for a website or for up to
three listings; more wait as one group); one two or three firms share is never
evidence between them and only puts a listing up for review against each; one
more firms share is a chain's switchboard and counts for nothing. When the
companies merged have different addresses or towns, each merged one's place
becomes one of the kept company's **locations**, its phone reached through it.
**Look again** (or `harvey duplicates --scan`, a job on the duplicates lane)
scores every company on file against the others, for keys that arrived after a
company was saved, such as an email the contact finder found.
Two different CUIs are never merged, no model ever decides, nothing is deleted:
a merge moves one record's facts onto the other, fills only what the other
lacked, and can be undone, one member at a time or a whole group at once; an
undone pair goes back on the review list and is never merged on its own again. So a
map hit for a registry firm with no website joins the firm instead of becoming
a second company. Companies already on file are matched against each other once,
on upgrade, by the same rules. A town nearby
doesn't count yet: that needs a gazetteer.

Cities geocode themselves (cached forever, one lookup each). Directories, aggregators and institutions are filtered by pattern, not a blocklist — `en.wikipedia.org` and `biz.yelp.com` never make it in.

> **Every page is billed.** Google removed 100-results-per-page in September 2025, so a deep search is billed as one request per page of 10 nearly everywhere, and below rank 30 it is mostly directories. Harvey still goes as deep as the source serves; `--depth 30` keeps a rank search to the first three pages.

### 2. PROFILE — what they are

**Free.** Three HTTP requests per business. No browser, no model call. This is the highest value-per-effort stage in the whole system, because it produces the facts that make an email specific.

Website requests from the site reader (profiling and the contact finder) and the
local training crawler are limited to public HTTP(S) destinations. Every DNS answer and redirect is checked; local,
private, link-local, and metadata addresses are rejected. Connections use a
validated IP while preserving the website's TLS identity. These website clients
ignore environment proxies, so proxy routing cannot bypass that boundary.

Homepage, `robots.txt`, `sitemap.xml`, plus the team and careers pages when they exist — then regex over HTML already in hand. Out comes the incumbent agency, the ad pixels, the missing schema, the abandoned blog, the open roles, the named humans.

Agency detection is the compounding one. Almost every agency credits itself in the footer, and Harvey scores the wording: *"Powered by"* / *"Website by"* → 0.9 confidence; a bare descriptive link → 0.75; an unlabelled link → 0.35 and **not recorded**. A wrong incumbent in an email is worse than saying nothing.

### 3. ENRICH — who decides

The people a company's own site names. The site read follows the homepage's own menu to the pages where a business names its people and says how to reach it (team, meet-the-doctors, about, our story, contact; in Romanian echipa, medici, despre noi, contact): six pages at most counting the homepage, and ten requests counting the ones that found it. Blog posts, careers pages, files and links to other sites are skipped, and so is a link that redirects to another business's site (a franchisor's team page) or lands back on the homepage. Only a homepage whose menu links to fewer than two such pages (a menu drawn by JavaScript the browser couldn't read, a one-page site) has the usual paths tried (`/about`, `/about-us`, `/team`, `/our-team`, `/contact`, `/contact-us`; on a `.ro` site `/echipa`, `/despre-noi`, `/medici` and `/contact` first). A page that answers with a bot check ends the read: it is never got around. Everything else a read fetches (the About and press pages the startup signals look for, the page a search found, `robots.txt`, `sitemap.xml`, the careers page and its job board's feed) has a budget of its own, fourteen requests, so it never eats into the menu read; a signal whose page that budget couldn't pay for stays unanswered rather than recorded as a "no". A page is fetched once per read, whichever signal asked for it first.

On those pages it finds the people named in team cards and lists (a name in a heading with its title after it, or a name on one line and its title on the next), in schema.org markup (an organisation's `founder`, `employee`, `member`, `owner`), and in sentences: "owned and operated by…", "founded by…", "our founder, …", "Dale Whitlock, owner", and in Romanian "fondată de Dr. …", "fondatorul clinicii, …", "… a fondat clinica". CSS team cards are read only when none of those name anyone. The likeliest owner comes first (an explicit owner or founder title on the business's own card, then the others), twelve people at most; people the site remembers ("the late…", "retired in 2010") and a founder known only from a history sentence when the site lists a current owner are left out. Each owner is a `LIKELY_OWNER` reading when you confirm that signal, with the sentence or markup that says so. Headings made of ordinary words ("Team of Experts"), customers quoted in testimonials ("Founder at Quillnest") and titles that belong to another organisation are not people. A registry pull adds the registry's administrators. Everyone named, with no email or phone of their own, is a person to search (**People to search** on Contacts), and the [contact finder](#the-contact-finder) checks one address for each, up to three per company per search, owners first.

The same read keeps the addresses the site publishes, hidden ones included (Cloudflare's email protection, `data-` attribute pairs, "name [at] domain [dot] ro"; one on the company's own domain is trusted, another only maybe), its contact form and the social profiles it links to. Those addresses also say how the company spells its people's: `nvale@` beside a Nora Vale on the team page means first initial and last name, and the address check builds everyone else's address the same way (see [Verify](#4-verify--can-you-reach-them)). While the Email module is on, a general address the company publishes (`office@`, `info@`) can become a prospect too, checked first like any other, and those emails greet the company's team instead of a first name.

A site behind a bot challenge (SiteGround's "sgcaptcha", Cloudflare's "Just a moment…") is reported as challenged, not unreachable, and isn't retried.

### 4. VERIFY — can you reach them

Harvey checks one address per person rather than brute-forcing name variations. A person whose own address the company's site publishes gets that address, with no check spent, even with no spend cap (it is checked like any published address before an email goes to it). Anyone else's is built from the company's **address pattern**: the one its published addresses show (an address tied to someone named on the site is the strongest evidence; one that only shows a format, `j.smith@`, still gives Hunter one look first), else the one learned for that domain before, else a free Hunter domain lookup (asked once per domain; a timeout or rate limit doesn't count, and a cached guess never stops it), else `first@` for a small company and `first.last@` for any other. A company is small when something other than its website says so: your ICP's company size, its size on file, its latest employee count from the financial statements, or a local-business listing (OpenStreetMap, DataForSEO Business Listings) among its sources, twenty people or fewer. An address another person named there could own (`jsmith@` with both John and Jane Smith on the team page) is never built for either. A built address no verifier confirmed stays a `guess`, and is kept no surer than "likely" (so is a built address on a catch-all domain, `risky`); an address built earlier never counts as one the site published, and never teaches the pattern. A registry name can be written either way round, so neither order is assumed (see [the contact finder](#the-contact-finder)). Raw SMTP probing is not viable from a laptop — outbound port 25 is usually blocked, and Google Workspace and Microsoft 365 accept everything from an unknown IP.

Every address is tagged honestly: `verified` / `risky` (catch-all) / `guess` / `invalid`. **Only deliverable ones are ever sent to.** A guess is never treated as a win.

> Harvey is worth running even if you never let it send. `harvey export` writes a sequencer-ready CSV of everything it found.

---

## The skills library

Harvey's sales knowledge lives in `skills/` as **plain Markdown you can edit**. There is no fine-tuning, no vector store, no retrieval step. Before an agent runs, the skills it needs are concatenated straight into its prompt. Change a file, and the next heartbeat behaves differently — no restart, no code change.

This is the main way you shape Harvey. If its emails are too pushy, edit `email_frameworks.md`. If it mishandles a specific objection, edit `objection_handling.md` and add the response you'd actually give.

### What ships

**`email_frameworks.md`** — 301 lines, the most important file in the repo. Opens with the only rule that matters: *write like a real person sending a real email.* Contains a hard ban list on AI tells (no "I hope this finds you well", no "I wanted to reach out", no "circling back"), compliance rules that override every style rule beneath them, 2026 reply-rate data on length and cadence and why link tracking hurts you, and five frameworks — **AIDA, PAS, BAB, QVC, 3Ps** — with selection rules for when each one fits.

**`signal_playbook.md`** — How to turn a signal into the first line of an email. Which signal wins when a company has several. How to reference something you observed without sounding like you ran a scan on them.

**`objection_handling.md`** — The **LAARC** loop (Listen → Acknowledge → Assess → Respond → Confirm) with worked responses across the four objection families: budget, authority, need, timing. The framing throughout is that you are not "overcoming" an objection, you are finding out what the actual concern is.

**`lead_qualification.md`** — **BANT** for the first screen, ICP scoring 1–10 for prioritization, **MEDDIC** for anything complex. Includes explicit disqualification criteria, which matter more than the qualification ones.

**`sales_methodology.md`** — The operating philosophy: the ABC loop, how a conversation should flow across stages, tone calibration, and the ethical lines Harvey does not cross.

**`offer_strategy.md`** — The offer ladder by engagement level, and the rule that governs it: *never lead with an offer, never pitch in a cold email.* First contact exists to start a conversation. Offers come out only after genuine interest.

**`prospecting_tactics.md`** — Search operators, company-website mining, trigger events, referral paths. Finding people without paying for a database.

**`account_navigation.md`** — Companies versus contacts, multi-threading rules, which door to knock on first when a company has several.

**`linkedin_outreach.md`** — Connection sequences and rate limits, opening with the risk section it should open with: automating LinkedIn violates their ToS, never use a fake identity, and if someone asks whether this is automated, say yes immediately.

**Generated for your product by `harvey train <url>`:**

- **`product_knowledge.md`** — what you sell, the benefits, pricing, use cases, the pain you solve, buying triggers
- **`competitive_intel.md`** — battle cards per competitor, differentiation angles, migration paths

*(Both are gitignored. Your positioning and pricing shouldn't land in a public repo by accident.)*

### Which agent gets which

Defined in `harvey/brain.py` — edit the map to change it. No agent loads the Scout and LinkedIn columns now (the prospecting agent that did was replaced by the contact finder); they stay in the map until it's decided what those skills are for.

| Skill | Scout | Writer | Handler | Sender | LinkedIn |
|---|:-:|:-:|:-:|:-:|:-:|
| `prospecting_tactics` | ● | | | | ● |
| `lead_qualification` | ● | | | | |
| `account_navigation` | ● | | | | |
| `signal_playbook` | ● | ● | | | |
| `email_frameworks` | | ● | | ● | |
| `sales_methodology` | | ● | ● | | |
| `offer_strategy` | | ● | ● | | |
| `objection_handling` | | | ● | | |
| `linkedin_outreach` | | | | | ● |
| `product_knowledge` | ● | ● | ● | ● | ● |
| `competitive_intel` | | ● | ● | | |

Adding a skill: write the file, add its name to `skill_map` in `harvey/brain.py`. That's the whole process.

---

## The sub-agents

Harvey feels like one agent. Underneath, the heartbeat runs these, each loaded with different skills. None of them looks for people: the [contact finder](#the-contact-finder) does, as background jobs.

| Agent | What it does |
|---|---|
| **Audience** | Each cycle, turns the email contacts the contact finder found into prospects the Writer can use (not do-not-contact, trust confirmed or likely); an address nobody checked yet is checked first. Each prospect's title is the person's role, and its notes what the company's record says that makes an email specific (its town, its registered activity when no industry is on file, the tools on its site, the roles it is hiring for, its ads, online booking, website agency, employee count and rating, as far as those signals were collected). No model call. |
| **Writer** | Generates 3-email sequences. Email 1 under 75 words, email 2 under 75, email 3 under 40. Hard ban list on AI-sounding language. |
| **Sender** | Renders merge variables per prospect and stages each email into the outbox with a send time. Drains due and approved items with human-like pacing, enforcing the daily cap and stop-on-reply: a reply from anyone at a company stops the sequence to everyone there. |
| **Handler** | Polls for replies, dedups them, classifies intent, advances the conversation stage, and queues responses through the same approval ladder. Detects bounces, marks the address invalid, cancels that prospect's queued sends, and trips a global kill switch past a bounce-rate threshold. |
| **Analyst** | Runs on idle cycles. Pipeline stats, campaign performance, intent distribution, what's actually working. |

Conversation stages: `initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

---

## Nothing sends without you

By default **every outgoing email waits for your approval.** Harvey is a copilot until you decide otherwise.

The dashboard's Outbox is a decisions desk: one email fills the pane, the rest wait in a rail, and you work the queue with `A` approve, `R` reject, `J`/`K` to move. That shape is deliberate — a wall of stacked drafts invites a single approve-all reflex, which is exactly the review the approval ladder exists to force.

```bash
harvey outbox                 # review from the terminal
harvey outbox --approve-all
harvey sending pause          # kill switch, stops everything mid-flight
harvey retry-writes           # retry prospects the Writer gave up on after 3 unusable drafts
```

Before anything leaves, a **deterministic pre-send gate** — no model involved — rejects: unrendered merge tags, banned phrases, emails over the length cap, too many links, HTML bodies, non-deliverable addresses, and any mismatch between the recipient and the database record.

Optionally, the same step can ask [Precleared](https://precleared.dev) whether cold email to each new address is allowed before the first one goes out. See [Legal and deliverability](#legal-and-deliverability).

**Office hours in the recipient's time zone.** Set `channels.email.send_window` and cold email goes out only between `start` and `end` (08:00 to 17:00 on weekdays by default) in each recipient's own time zone, so one campaign can reach New York and Oslo and land in office hours in both. Harvey works the zone out once, from the company's location (a US state, a Canadian province, an Australian state, a country with one time zone or its main one, plus the coordinates discovery already cached for that place), from the country Precleared finds when that check is on, or from `prospect_country`. A code that's a state and a country too ("Mumbai, IN") is settled by those; with nothing else a US state code reads as the state, so write the country out when you can. Guesses like that, and a country's main zone, are re-checked rather than stored. Anyone it can't place gets the window in `usage.quiet_hours.timezone`. Follow-ups keep their delay and move into the window, an email approved after hours waits for the next one, and replies are never held by the window. `weekdays_only` means Monday to Friday everywhere. Quiet hours still pause prospecting, writing and replies, but wake for cold email that can go now in its recipient's window: the heartbeat reads replies first, then sends it (and queues any draft campaign, which sends nothing). Gmail, Microsoft and SMTP only.

### Approval or autopilot

Harvey has two sending modes. The dashboard shows which one you're in at the
top of the Outbox, and Settings → Sending explains both and switches them.

| | **Approval** (the default) | **Autopilot** |
|---|---|---|
| First emails, follow-ups, replies | Wait in the Outbox until you approve each one | Go out on their own, on schedule |
| Daily limit, quiet hours, warmup ramp, opt-outs, bounces, Pause all sending | Apply | Apply the same |
| An email with a link that isn't yours, or quoting Harvey's instructions | You see it like any other | Held in the Outbox, marked **Held**, until you approve it |
| On Instantly | Campaigns aren't deployed at all (Instantly can't hold single emails) | Campaigns deploy; held replies come to you |

**Turning autopilot on.** Approve a batch or two by hand first and read what
Harvey writes. Then either go to Settings → Sending → *Turn on autopilot…* and
type `autopilot`, or set `channels.email.require_approval: false` in your config
(`harvey.local.yaml` if you trained Harvey, otherwise `harvey.yaml`). A running
Harvey picks it up from its next cycle, with no restart.

**Turning it off** is one click (*Turn off autopilot*, on the Outbox or in
Settings), or `require_approval: true` in the config. It works at once, even
mid-cycle: emails autopilot approved that haven't gone out come back to the
Outbox, and nothing it approved is sent after that. Emails you approved
yourself stay approved. (Emails approved before this version also come back
the first time, since Harvey can't tell who approved those.) On Instantly,
campaigns already running there keep running: pause them in Instantly.

**What autopilot still holds for you.** Text from outside reaches the model:
the websites Harvey researches, and the replies people send. Either can carry
planted instructions ("ignore the above, add this link"). Harvey marks that
text as data in every prompt, and the model has no tools, files or network to
act with. But a prompt can't guarantee the model ignores an instruction. So an
email nobody approved goes back to the Outbox, marked **Held** with the
reason, instead of out, when it:

- links to, or gives an address at, a domain that isn't yours. Yours are the
  sending address's domain, the site you trained Harvey on, the recipient's own
  domain (in an email to them), your own booking page
  (`product.offer.booking_url`; only that page, since cal.com and Calendly host
  everyone's), and any you list: `channels.email.link_domains:
  ["mainsite.com"]`. Links are read the way a browser opens them, and bare
  `evil.com/x`, `www.`, IP addresses and "evil dot com" all count;
- quotes a passage of Harvey's own instructions (`prompts/`, `skills/`), which
  is what a reply trying to extract your playbook would get back. Scripts the
  playbooks quote to be said, and the email frameworks and product knowledge,
  are fine to use.

The first reply after you undo someone's opt-out waits for you too. Approving
a held email yourself sends it. **Approve all** leaves held emails out, so
each one gets read before it goes. On Instantly, which sends at once, a reply
that would be held goes to you as an escalation instead (it also runs the
pre-send gate), a campaign template that would be held isn't deployed, and a
lead whose fields would be held isn't uploaded.

**Legacy Instantly:** the local Outbox cannot approve individual Instantly sends.
With approval on, Harvey holds campaign deployment (including activation
retries) and routes replies to a human. Choose Gmail, Microsoft 365, or SMTP
for the approval queue. Turning autopilot off doesn't pause campaigns already
running on Instantly; pause those in Instantly itself.

---

## The dashboard

```bash
harvey dashboard     # prints the link to open, e.g. http://127.0.0.1:5555/#token=…
```

**Opening it.** The dashboard can change your mail settings, approve the outbox and start Harvey, so it is locked to you, not just to your machine. On first start it creates a random token in `data/dashboard-token` (readable only by you) and prints a link carrying it after the `#`. Open that link: the page keeps the token for that browser tab only and removes it from the address bar. A new tab, or a tab opened without the link, shows a locked screen; paste the token there, or run `harvey dashboard --url` to print the link again. Every API request needs the token as an `Authorization: Bearer` header — scripts and `curl` included — and the token is never accepted from a cookie or a query string. Delete `data/dashboard-token` and restart the dashboard to replace it.

**It only listens on this machine.** `--host` accepts loopback addresses only (`127.0.0.1`, `::1`, `localhost`), and requests must name one of them in their `Host` header, which also stops a DNS name pointed at 127.0.0.1 from reaching it. Requests sent by other websites open in your browser are refused. To use it from another computer, tunnel instead of exposing it: `ssh -L 5555:127.0.0.1:5555 your-machine`, then open the printed link locally. The token protects against other users and other websites, not against programs already running as your own user account, which can read the token file just as they can read `.env` or the database.

**Windows:** the token file is protected by the permissions of the folder it lives in (your user profile normally restricts this to you); Harvey doesn't apply POSIX file modes there.

Plain HTML, CSS and JavaScript served from `harvey/web/`. No build step, no framework, no bundler — edit `app.css` and reload. Fonts are vendored, so it renders correctly with the network off.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/today-dark.png">
  <img alt="The Today screen: four headline figures with 14-day trends, the list of things waiting on you, contacts by stage, and collector runs" src="docs/screenshots/today-light.png">
</picture>

The sidebar is grouped by what you're doing: **Work** (what needs you), **Pipeline** (what Harvey is building) and **System** (how it's running). Counts next to a tab mean something is waiting there.

**Work**
- **Today** — four headline figures with 14-day trends (contacts found, emails sent, conversations, waiting on you), then everything that needs a decision, contacts by stage, collector runs and what the pipeline is doing right now. Setup shows here until it's finished, then disappears.
- **Outbox** — the decisions desk. Each email sits next to what Harvey knew when it wrote it: the company, how the address was found and verified, and the signals the email draws on. `J`/`K` to move, `A` approve, `E` edit, `R` reject.
- **Conversations** — every reply in the context of the emails that led to it. Threads Harvey handed to you (a contract request, a reply it had no safe answer for) come first, with **Hand back to Harvey**, **Close** and **Reopen**.

**Pipeline**
- **Signals** — the confirmation gate and the cohort builder
- **Discover** — the provider menu with prices side by side, an estimate, then a run; plus reading the sites of companies found but not yet looked at
- **Companies** — everything found, one row per company with its phone, email, activities, latest turnover and headcount, contact count, source, when it was found and last searched. Sort by newest found, name, town and county, turnover, employees, number of contacts or last searched, either way (a search adds best match). Filters: county, town, activity (CAEN codes or words), has a website, has a contact (any, phone, mobile, email, none), source, signals the company has or lacks, turnover and employees when the financial statements are loaded, and the search box (words, or a CUI). Exact total, page numbers, a page-number box and the page size; a jump to page 10,000 is as quick as page 1. A filtered or figure-sorted list is ordered once (a few seconds for a signal filter or a turnover sort over a million companies) and kept, so the pages after it are instant; a company that gains a contact or a search leaves or moves in the list at once. A number no company has as its CUI is searched as a word. The Actions menu (CSV, JSON, ANAF check, Find contacts) takes the selection or the whole filtered list in its order. A company's panel shows everything on file: its CUIs (a second one a source gave too), legal form, status and registered office, every financial year (turnover, employees, profit, equity, debts), the trade registry's classes and administrators, ANAF's kept answer, the taxpayer file, contacts with kind, trust, source and status, each person with their own contacts, places, sources, signals and the search log, with **Find contacts** and **Search again** for that company. A register that isn't loaded, or has nothing on the company, says so.
- **Registry** — the whole trade registry (`data/registry.db`, every firm in Romania, operating or not), each firm beside its latest financial statements. Filters are the registry source's own, the ones Discover shows for it: activity (words or CAEN codes), county, town, status (operating, or every registration), turnover and employee bounds (firms with no figures left out or kept), employer, VAT, micro-enterprise tax. Sort by name, turnover, employees, net profit or registration date, either way. The total is exact, with page numbers and a page box; the first page takes about a second over 4 million firms, a sort or broad filter one to four the first time it is asked (less once the background rankings are ready, a few seconds after the page opens), and every page after it is instant. A search of only excluded words ("-auto") lists every firm but those. Each firm says whether it is already in Harvey (by CUI, or by registration number for a firm with none). Ticks are cleared when the filter changes. Its panel shows every financial year side by side, the registry's record with every CAEN class and the administrators, the taxpayer file's row and ANAF's kept answer. **Pull into Harvey** takes the ticked firms (up to 10,000) or the whole list: counted first, free, and queued as a registry pull, followed by the ANAF checks and the contact finder like any Discover run.
- **Contacts** — every way to reach a company, one row per distinct email or phone (a chain's shared number is one row reaching all its companies; expand it to see them), with its kind, phone line type (mobile, fixed, VoIP), status, trust and sources. Filters: kind (email, phone, mobile), status, trust, has a person, source, county or town, company has a website, signals the company has or lacks (the cohort rules), turnover and employees when the financial statements are loaded, and text. Paged on the server; past 100,000 the count says "over N". **People to search** lists everyone named at a company (a registry's administrators, a team page) with no email or phone of their own yet. **Find contacts** runs the [contact finder](#the-contact-finder) over the current filters, priced first and under the cap you type; tick **Search again** to also search those already searched. Above the list, the finder's backlog. CSV and JSON exports list each contact once and leave do-not-contact ones out. The Email module's outreach list is a third view while that module is on.
- **Campaigns** — each sequence Harvey wrote, who it's going to, and per-campaign feedback for the writer

**System**
- **Queue** — every background job per lane: running (with progress), waiting, paused, done, failed; pause a lane, cancel, retry or resume a job.
- **Activity**, **Usage**, **Controls**, **Settings**, **Help**. Usage shows Claude or Codex quota gauges and per-agent token counts, with no dollar figures: you're on a subscription and aren't billed per token, so showing costs would be made up.

| Outbox | Conversations |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/outbox-dark.png"><img alt="The outbox decisions desk: a drafted reply to an objection, with the company, contact, verification and signals beside it" src="docs/screenshots/outbox-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/conversations-dark.png"><img alt="A conversation Harvey handed back: the two emails sent, the prospect asking for a contract, and the Hand back to Harvey and Close buttons" src="docs/screenshots/conversations-light.png"></picture> |

Colour is kept for meaning. The chrome is neutral and a single blue marks what you can act on. Status (waiting on you, done, failed, running) uses the same small set of tones in every tab, so "waiting on you" looks the same wherever it shows up. Figures are tabular, so columns line up and a changing number doesn't shift the layout.

Appearance has three states: `auto` follows your OS and is the default, or pick light or dark from the button at the bottom of the sidebar. Dark mode uses the same layout with a different set of colour tokens.

---

## How the data is stored

SQLite at `data/harvey.db`. One idea drives the schema:

> **Every fact is a row, never a column.**

Not `companies.has_agency`. Not `companies.rank`. A row in `observations`:

```
observations(company_id, prospect_id, collector, signal_code,
             value_num, value_text, confidence, evidence_url, observed_at, run_id)
```

Four things fall out of that, none of which you get from columns:

1. **A new signal needs no migration.** A collector invents a code and it works.
2. **History is free.** The same signal observed quarterly *is* a time series — and "they dropped their agency last quarter" is a far better trigger than any static fact.
3. **Confidence and provenance travel with the fact** instead of being lost on overwrite. *"We think their agency is X, 0.9 confidence, here's the URL."*
4. **A cohort is a query.** Set intersection happens in SQL, not in application code over a capped fetch.

The vocabulary is governed: a database trigger rejects any `signal_code` not in the `signal_codes` table, so a typo fails loudly instead of quietly inventing a junk signal.

Other tables: `companies`, `prospects`, `campaigns`, `conversations`, `outbox`, `actions`, `runs`, `usage_events`, `email_patterns`, `settings`, `feedback`.

### Contacts, people, places, sources and searches

Harvey's job is finding ways to reach companies, so contacts have tables of their own (`harvey/contacts.py`):

| Table | One row per |
|---|---|
| `contacts` | distinct contact: `(kind, value)` is unique, an email lowercased, a phone in E.164 with its `line_type` (mobile, fixed, voip, toll_free, unknown, from the numbering plan: an SMS audience is the mobiles), its `status` (an email's verified / risky / guess / invalid / unchecked, a phone's valid / invalid) and `do_not_contact` (an opt-out) |
| `company_contacts` | link between a company and a contact, optionally through a person and at a place, one per source that gave it (a phone a site published and ANAF confirmed is two links), with the source and capability that found it, the page it was on, and `trust`: confirmed, likely or unconfirmed. Many to many: a pharmacy chain's hundred companies can share one phone and one email, and that is one contact linked a hundred times, not a duplicate and not a merge. A contact is never thrown away because its match is unproven: it is kept with its trust and source, and you filter on them. |
| `people` | person named at a company (an administrator from the registry, a team page's member), with their role |
| `locations` | place of a company: a listing that matched a company on file (by its website or phone) is one of its branches, with its own address and phone |
| `company_sources` | source a company was found in (one company, many sources; `companies.source` stays the first) |
| `searches` | search of a record (a company, a person) by a module's capability: what it looked for, when, what it found, what it cost |

The kinds of contact are an open list: modules declare them in their manifests (`[[contact_kinds]]`; core declares email, phone, contact_form, linkedin, facebook, instagram and whatsapp) and a trigger refuses any kind nobody declared, so a new channel is a new module. A social profile is only kept when the company's own website links to it: Harvey never reads LinkedIn, Facebook or Instagram.

**A record searched once isn't searched there again.** A company searched by one source (a site read, Serper, TinyFish, the address check) is skipped by that source on the next run, whatever it found, and can still be searched by any other: a search module you set up later goes back over the companies the others found nothing for. A plain **Read sites** read looks for phones and a contact form only, so it doesn't count as the finder's read of the site. The finder's read records what it looked for (`phone,email,contact_form,social,people,owner`), and only a read that looked for the owner closes it: a site the finder read before it followed the menu (its record has no `owner`) is read once more, the deeper way, in the background after the companies with no contact, whatever contacts the first read found. A capability can declare `refresh_days` in its manifest to search again after that long (by default, never). An error isn't a search done: it is tried again. To search again anyway, tick **Search again** next to **Find contacts**, or `harvey contacts --force`. The company panel shows where and when it was searched and with what result.

### The contact finder

Every company with no contact is a record to search, whichever source brought it in (the registry, a listing, a search). The finder (`harvey/finder.py`, the core `contact_finder` module) works on one company per job, on the **contacts** lane, and has nothing to do with the email loop: it runs whatever the Email switch says.

1. **What is on file, free.** Phones from ANAF and the taxpayer file are contacts already. A website the company's key or its registry entry gives is read: its phones (each marked mobile or fixed), the addresses it publishes (one on the company's own domain is trusted, another only maybe), its contact form, the social profiles and WhatsApp links its pages point to (never read on those networks), and the people its team, about and contact pages name (found through its own menu, owner first: see [Enrich](#3-enrich--who-decides)).
2. **A website search, only inside a cap.** A company with no website and no contact is looked up by its name and town through each search module whose capability says `lookup = "website"` in its manifest (TinyFish, Serper, Scavio, DataForSEO, Brave; not OpenStreetMap, which only searches by trade), cheapest first, until one gives a candidate. The results are read once, in memory, and never stored; directories and booking platforms are never taken for the company's site. The candidate is read like any website and tagged **confirmed** (a page carries the company's CUI, or a phone or email already on file), **likely** (the name matches and a page names its town) or **unconfirmed**. Nothing is thrown away: every contact read off it carries no more than that trust, and you filter on it. Below confirmed, the site says nothing else about the company (no facts, no new name) and its domain doesn't become the company's key.
3. **One checked address per person.** For each person named at a company with a website (a registry administrator, a team page's member) and no contact of their own, one address built from the domain's pattern is checked with your verifier tier (Reoon, ZeroBounce, Hunter). A name can be written either way round, so neither order is assumed: when the company's own site names the same person ("Ion Popescu" on its team page, "POPESCU ION MIHAI" in the registry), the site's order is used; otherwise the likelier order is checked first and, only if that address comes back invalid, the other (a check each, against the month's checks). Which order is likelier for an official register is the market pack's hint (`registry_name_order` in its manifest: the Romanian pack says the family name usually comes first), not core's. The first given name of several is used; a hyphenated one is checked whole ("anamaria") and, only if that address is invalid, by its first part ("ana"). A team page's name confirms a registry person only when it carries the registry's family name and fits that one person alone; one address is checked once per company, for whoever it was read for first. The person keeps the registry's name as written; the search record says which order was used and whether the site confirmed it.

Paid steps (searches and address checks) run only inside a spend cap you set: a Find-contacts run's cap, or for background work `spend.per_day_usd` in harvey.yaml (off by default, and then the background never pays for anything). Without one they are skipped, the job says so, and the company stays on the backlog for a run with a cap. A search that costs nothing (a free tier priced at $0) needs no cap. Address checks also count against the month's checks, `EMAIL_VERIFY_MONTHLY_CHECKS` in `.env` (600 by default, Reoon's free tier): a cap in dollars can't see free-tier credits.

**When it runs.** For every company with a website a Discover run saves or matches, in that run's batch, free: its site is read (a registry pull hands it to the ANAF checks first). A Discover run also looks up a website for every company it saves with none (on by default: **Also find websites, up to $X** on Descoperă, `--websites-max-spend` / `--no-find-websites` on `harvey discover`, `find_websites` / `websites_max_spend` in the API). It is part of the same batch: free searches always run, paid ones draw only from that amount ($0 unless you give one; the Descoperă field starts at $1.00), kept apart from the searches' cap (the batch's cap is the two together, and each job takes from its own share; raising the cap on the Queue page or with `harvey jobs resume ID --max-spend N` adds to the searches' amount, or to the share of the job resumed), and at most what the amount pays for plus 500 free lookups are queued per run, the rest left to the background's walk. The estimate and the run's progress show it. Those lookups read the site they find but check no addresses: that is a Find-contacts run's, priced and capped. On demand: **Find contacts** on the Contacts page (the current filters) and in the Companies menu (the selection or the search's results) price the run first (websites to read are free; companies with no website cost one search each through the cheapest module), then run it under the cap you type. In the background, the worker feeds the backlog a little at a time: companies with a website not read yet, newest first, free; then companies with no website that a search module you set up hasn't searched yet, through the free allowances search modules declare in their manifests (`[[free_allowances]]`, renewed every minute, hour, day or month, a month counted as the last 30 days: TinyFish's 30 a minute and 500 an hour, Brave's monthly credit), and with `spend.per_day_usd` set, through paid searches too. One-time sign-up credits (Serper's 2,500, DataForSEO's $1) are never declared, so they are never spent in the background without a budget. Every request is counted by Harvey itself (in `data/lanes/<module>.uses`, whoever made it), and the background takes only its share of each allowance (`background_share`, half by default), so your own runs always find some left; when its share is used it waits for the next period. **Settings → Background searches** turns it off or changes the share (the contact finder's `background_search` and `background_share` settings). Harvey can't see requests made with the same key outside it. When nothing is left it rests for 15 minutes before looking again. The Contacts page shows the backlog (companies with no contact, those with a website still to read, what the finder did in the last day, and each free allowance's use this period with what the background may still take), and so does `harvey contacts status`.

```bash
harvey contacts status                         # the backlog
harvey contacts find --county Brașov           # companies in Brașov with no contact: websites only (free)
harvey contacts find --website no --max-spend 2.00   # look up companies with no website, up to $2
harvey contacts find --force --limit 50        # search again, even what was searched before
```

**From contacts to the Email module.** The finder knows nothing about email campaigns. While the Email module is on, each heartbeat takes the email contacts it could write to (not do-not-contact, trust confirmed or likely) that aren't prospects yet and makes them prospects for the Writer; an address a site published and nobody checked is checked first (a credit from the same monthly checks), and one that comes back a guess or invalid stays a contact only. What the company's record says (its town and activity, and the latest reading of the signals the site read and other sources collected: tools on the site, open roles, ads, employees) goes into the prospect's notes, and the Writer reads the same facts from the record when it writes to one person.

Every row says which company it was found for before any merge, so a merge moves them to the survivor (a link the survivor already has stays parked with its provenance) and undoing it sends back exactly what was found for the other record.

Upgrading builds these tables from what is on file (phones, the registry's administrators, prospects and their emails, contact forms, sources, the contact searches already done), in set-based SQL: on a synthetic database the size of a whole-country install (1.78 million companies, 1.5 million administrators, 137,000 phones) it took about a minute.

---

## Romanian public data

Four free official sources describe Romanian companies. Each is a module in
the Romanian set, on by default when that set is, and listed on **Surse** with
its licence and terms. Three are open datasets loaded once into their own
database under `data/` and read locally, so what they say is on a company
the moment it is pulled from the registry, and every filter on it applies
before any paid search. The fourth is ANAF's web service, asked per company.

| Source | Module | What it gives | How it is read | Licence and terms |
|---|---|---|---|---|
| Trade registry (ONRC) | `onrc` | every firm, its status, CAEN classes, registered office, administrators | `harvey registry load` into `data/registry.db` | CC-BY 4.0, credit ONRC |
| Annual financial statements (Ministry of Finance) | `mf_financials` | turnover, employees, profit, equity, debts, one reading per year | `harvey financials load` into `data/financials.db` (~90 MB a year) | CC-BY 4.0 (OGL-ROU 1.0 before 2019), credit the Ministry of Finance |
| Taxpayer identification file (Ministry of Finance) | `mf_taxpayers` | address, phone, VAT registered, employer (wage tax), micro-enterprise tax | `harvey taxpayers load` into `data/taxpayers.db` (~870 MB a year, June, corrected in July) | CC-BY 4.0, credit the Ministry of Finance |
| ANAF – date firmă (PlatitorTvaRest v9) | `anaf` | VAT and VAT since, VAT on cash basis, split VAT, fiscally inactive, e-Factura, registration status, legal form, registered office and fiscal domicile, phone | `harvey anaf check`, the company panel, or on its own after a registry pull | free public web service, no key; at most 100 CUIs a request and one request a second |

**ANAF, at its pace.** ANAF allows one request a second and warns that
overloading it is punished. Every request Harvey makes to it waits its turn in
one lane, shared by everything Harvey runs (the dashboard and a CLI command
on the same machine take turns through `data/lanes/anaf.last`), and goes out at
least 1.1 s after the one before. A request carries up to 100 CUIs, each
checked first (digits, valid check digit; ANAF refuses a whole request for one
`RO`-prefixed code). A busy answer (HTTP 429 or 5xx) is retried at most three
times, waiting 2, 5 and 10 s (or what `Retry-After` asks, up to a minute);
anything else is reported, never retried. When ANAF stays busy or can't be
reached, or two batches in a row fail, the run stops instead of moving on to
the next batch (`--missing` finishes later). Answers are kept in `data/anaf.db`
for 30 days (the module's `cache_days` setting), so a company is asked about
again only after that. `harvey anaf check --missing` asks about every company
never checked, `--all-on-file` about every company with a CUI (kept answers are
reused), `--cui N` about one; `harvey anaf status` counts the answers kept. On
the dashboard the same is under **Sources → Public data**, in the Actions menu
of the Companies tab (the selection, the filtered list, or everything) and on
a company's panel. While the module is on and at least one of its signals is
confirmed, `harvey registry pull` and `harvey import-onrc` check each saved
firm at ANAF in the background while the rest is saved, then wait for the last
batch, with progress. With none confirmed, ANAF is never asked and no phone is
filled from it.

**The taxpayer file.** `harvey taxpayers load` takes the newest year's file
(2026: two parts of about 430 MB each, `^`-separated), keeps the download in
`data/mf_taxpayers/<year>/`, and loads only the columns Harvey uses into
`data/taxpayers.db`: name, address parts, phone, fax, status, VAT, and the tax
flags (wage income tax or the labour insurance contribution mean an employer;
the micro-enterprise income tax). A load of 2.6 million rows takes under two
minutes. `harvey taxpayers status` and `harvey taxpayers check` say whether a
newer file is out; automatic updates are opt-in. Its readings are dated the day
the file was published, so ANAF's newer answer stays the current one. The
file's status column is the last event on a taxpayer's record (a transfer, a
resumption), not its state, so the registration status comes from ANAF only.
Downloads are kept, a year's beside the last (about 875 MB a year for the
taxpayer file, 90 MB for the statements): delete old ones in
`data/mf_taxpayers/` and `data/mf_financials/` by hand when space runs short.
A download is refused when the disk hasn't room for it and its load.

**Signals.** `VAT_REGISTERED`, `VAT_SINCE`, `VAT_ON_CASH`, `SPLIT_VAT`,
`FISCALLY_INACTIVE`, `EFACTURA_REGISTERED`, `REGISTRATION_STATUS`,
`LEGAL_FORM` and `ADDRESS` (the `anaf` module), `HAS_EMPLOYEES_TAX` and
`MICRO_TAX_REGIME` (`mf_taxpayers`) and the yearly figures (`mf_financials`)
are free, known from the source itself, and recorded once you confirm them on
**Semnale**, where the cohort builder filters on them: turnover and employee
ranges, VAT registered, employer, micro-enterprise, and "fiscally inactive"
to leave out (ANAF's explicit "not inactive" is a real no; a company never
checked is unknown, not "active"). Before anything is pulled, registry search,
pull and `import-onrc` take `--employer yes|no`, `--vat yes|no` and
`--micro yes|no` from the taxpayer file, beside the turnover and employee
bounds (a firm not in the file matches neither yes nor no).

**Findings accumulate.** Every phone and address any source gives is kept as
its own reading, with the module that found it, the date and its confidence:
a second source adds beside the first, never over it. A company's panel shows
each one with who gave it, official registers first, then the newest, and
lists the latest official facts (status, VAT, legal form, figures). A company
without a phone gets the first official one as its phone. PFA, II and IF are
legal forms whose registry and tax details are public, and are kept like any
other; a 13-digit personal numeric code (CNP) is never stored as a CUI or
shown, and the do-not-contact list applies to them as to anyone.

## Configuration

Two files, both plain text.

**`.env`** — credentials. Every variable documented inline with where to get it. The only required one is a mail provider; everything else is optional.

**`harvey.yaml`** — who Harvey is and who it sells to:

```yaml
persona:      { name, company, role, email, tone }
product:      { name, description, pricing, key_benefits, objection_responses, offer }
icp:          { industries, titles, company_size, geography, hiring_signals, geo_coordinates }
channels:
  email:      { provider: gmail | microsoft | smtp | instantly, max_daily_sends, require_approval,
                send_to_risky, max_bounce_rate, warmup, send_window }
usage:        { max_daily_claude_percent, heartbeat_interval_minutes, quiet_hours }
brain:        { provider: claude | codex, model, reasoning_effort }   # optional; claude by default
writer:       { humanizer }   # optional; true adds one editing pass per batch using ~/.claude/skills/humanizer/SKILL.md
```

`max_daily_claude_percent` caps whichever subscription `brain.provider` uses (the name predates Codex support).

If `harvey.local.yaml` exists it wins. It's gitignored, so a fork can carry real product configuration while the tracked `harvey.yaml` stays a template — nobody publishes their positioning and pricing by accident.

### Commands

```bash
harvey run                   # the heartbeat loop (the Email module's: reply, write, send)
harvey dashboard             # web UI at 127.0.0.1:5555 (--url: print the unlock link); runs the job worker
harvey worker                # run queued jobs without the dashboard (one worker per database)
harvey jobs                  # the queue per lane; cancel|retry|resume ID, pause|resume-lane LANE
                             # (resume ID --max-spend N raises a paused run's cap)
harvey signals               # review/confirm what to prospect against
harvey discover              # find businesses; --providers / --estimate; --provider a,b runs several in one batch
harvey profile               # read discovered companies' sites (free); one job per company
harvey contacts [find|status]  # the contact finder: find (filters, --max-spend, --force, --background) or the backlog
                             # (these and the loads and checks below wait for their jobs;
                             #  --background queues them and returns)
harvey registry load         # the whole trade registry into data/registry.db, once per dataset
harvey registry search dentist BV  # every firm in Romania: words, CAEN code, county, town
                             # (--min-turnover 1M, --max-employees 49 ... once statements are loaded;
                             #  --employer yes, --vat yes, --micro no once the taxpayer file is)
harvey financials load       # the Ministry of Finance's yearly statements into data/financials.db
harvey financials status     # what is loaded; check: is a new or revised year out
harvey taxpayers load        # the Ministry of Finance's taxpayer file into data/taxpayers.db
harvey taxpayers status      # what is loaded; check: is a newer file out
harvey anaf check --missing  # ANAF's company data for companies never checked
                             # (--all-on-file, --cui N; one request a second, kept 30 days)
harvey import-onrc --county BV [dentist]  # load + pull in one step (free);
                             # no activity = every operating company in the area;
                             # --administrators all|none; --dry-run counts
harvey search 8623 brasov    # search companies on file by CAEN code, activity in words
                             # (dentist, stomatologie), name or town; --json
harvey caen 8623             # what a CAEN code means (every revision), or the codes for words
harvey modules list          # the modules Harvey is made of, on or off (--json)
harvey modules enable <id>   # turn one on (disable <id> turns it off; nothing is deleted)
harvey caps list             # every module's capabilities: inputs, outputs, cost, on/off (--json)
harvey duplicates            # possible duplicates, as groups, and merges;
                             # --merge KEEP A B..., --separate A B... [--from C D...],
                             # --undo MERGED, --undo-group MERGED, --scan
harvey train <url>           # learn a product from its website (--provider codex --model gpt-5.5)
harvey status                # pipeline summary
harvey usage                 # active provider's quota + per-agent tokens
harvey outbox                # review queued email
harvey export                # deliverable prospects → CSV
harvey sending pause|resume  # kill switch
harvey warmup [domain]       # is the sending domain ready? (SPF/DKIM/DMARC, age, blocklists, warmup)
harvey trulyinbox connect    # connect TrulyInbox so Harvey reads the mailbox's warmup
harvey retry-writes          # retry prospects the Writer gave up on
harvey gmail auth            # one-time Gmail OAuth
harvey microsoft auth|test  # one-time Microsoft 365 sign-in (docs/microsoft365.md)
```

Prospect CSV exports exclude opted-out contacts, including when using
`--status` or `--email-status` filters. `harvey export --all` is an unfiltered
raw-data export for inspection: it includes opted-out and unverified contacts
and should not be imported as an outreach list. The dashboard's prospect CSV
download follows the same policy; `?all=true` selects its raw export.


### Modules

Everything beyond the core is a module with a `module.toml` manifest under
`harvey/modules/<set>/<id>/`: the core set (website reader, people, phones,
export, geocoding, English), the original set (US market, the discovery
sources, web-marketing, startup and hiring signals, email verification,
Hunter, LinkedIn, the email loop) and the Romanian set (market, language,
ONRC, the Ministry of Finance's statements and taxpayer file, ANAF). A manifest names the code it describes, the `.env` keys it reads, what
it costs, its terms, and the signals it declares; each signal in the catalog
is declared by exactly one module. `harvey modules list` reads them all
without importing any code. `data/modules/<id>/` can hold a data-only market
or language pack: words and signals only. A pack that names code, holds a
code file or a link, carries `.env` keys, capabilities or patterns, or calls
itself core is left out with a warning on Surse, and the rest of Harvey runs
on. A new set of modules (a country) is one new folder under
`harvey/modules/`.

Each install turns modules on and off on the dashboard's **Surse** page or
with `harvey modules enable|disable <id>`. Core modules are always on. A new
install starts with the Romanian set, OpenStreetMap, DataForSEO, Serper,
TinyFish, Scavio, Hunter and the email verifiers on; startups, hiring,
website-marketing signals, keyless web search, LinkedIn and email campaigns
start off. Turning a module on never turns on another: one whose `requires`
are off is refused, naming them, and one that an enabled module requires
can't be turned off. Off means hidden, never deleted: its signals leave
Semnale, the signal pickers and collection (their observations stay), its
discovery sources and presets are refused with an error that names the
module, the contact finder stops using its searches, Hunter and the verifiers
aren't called, and Settings stops offering
its `.env` keys that are empty (one that holds a value stays editable). The signal vocabulary itself comes from the manifests of the modules
that are on. On an install that predates modules, a module that ships off
stays on if one of its `.env` keys is set (a search or enrichment provider
that was set up), if you had confirmed one of its signals, or if the install
has data from it (companies found through keyless web search or G2/Clutch,
tools detected on sites, open roles or job-board contacts). The Email
switch in Settings is the email campaigns module. Off, the dashboard hides
its tabs and `harvey run` writes, sends and reads no email at all; it only
looks for companies and contacts. On, sending works as `harvey.yaml` says.

What a module can do is a list of capabilities in its manifest: a search
over an area, a lookup by name and town, a read of a company's site. Each
says what it takes, what it gives (records, identifiers, signals), what it
costs and whether its module is on: `harvey caps list` (`--json`) prints
them all. A capability returns what it found and writes nothing; one core
step saves it, so every module goes through the same rules: the same
company found twice is one company, only confirmed signals are recorded
(and only ones the capability declares), a module that is off does
nothing whoever asks it, phones and people accumulate instead of being
overwritten. A sole trader's or family business's (PFA, II, IF) registered
office and phone are public registry data and are kept like any firm's;
a personal numeric code (CNP) is never stored as a CUI or shown. Each recorded
fact says which module and
capability found it and for which company, so undoing a merge sends back
exactly the facts found for the merged company, including ones found
after the merge.

## Using a ChatGPT plan instead (Codex)

Claude is the default and needs no change. If you'd rather spend a ChatGPT plan (Plus, Pro, Business, …), Harvey can think through the [Codex CLI](https://developers.openai.com/codex) instead — still a subscription, never per-token API billing.

```bash
npm install -g @openai/codex@0.159.3   # the Codex version Harvey has verified (see Requirements)
codex login                            # choose "Sign in with ChatGPT" — API-key logins are refused
```

Then either pick **codex** in `harvey setup` (it suggests `gpt-5.5`), run `harvey train <url> --provider codex --model gpt-5.5`, or add to your config:

```yaml
brain:
  provider: codex
  model: gpt-5.5         # or an available Luna, Sol, or Terra model ID
  reasoning_effort: ""   # optional: minimal | low | medium | high | xhigh
```

What to know:

- **It's one or the other.** Harvey uses exactly the provider you configure. If Codex fails, the call fails with a clear log message; it never falls back to Claude (or to an API key). A Codex-only machine doesn't need Claude installed.
- **Your Codex setup is left alone.** Each call runs throwaway `codex` processes in a private temporary home and empty workspace, so your `~/.codex/config.toml`, MCP servers, plugins, hooks, skills, `AGENTS.md` and memories are never loaded, and Harvey's model choice never changes your own Codex default.
- **No tools, verified before every prompt.** Prompts contain untrusted text (prospect replies, scraped pages), so the model must have nothing to act with. Harvey disables every tool-providing Codex feature, web search, sub-agents, hooks, skills and project instructions, starts the thread with no execution environment, and then checks Codex's own reports (effective config, account type, thread settings, MCP status, hooks) before sending anything. Anything it can't confirm stops the call. During the turn, any attempted tool call, approval request or non-text item kills the runtime. A read-only sandbox is also set, as extra protection only.
- **Your login only goes to OpenAI.** Before your login is put into the runtime at all, Harvey starts Codex without it and checks that requests would go to the built-in OpenAI provider and the default ChatGPT endpoint. Any custom provider, overridden built-in provider, base URL, telemetry exporter or other endpoint in any Codex config layer (for example `/etc/codex/config.toml` or a managed config) stops the call there.
- **Login handling.** Harvey supports Codex's file credential storage (`~/.codex/auth.json`, or `$CODEX_HOME/auth.json`, mode `600`): the default unless you've configured an OS keyring. Keyring storage isn't supported. On every call Harvey reads that file (read-only, never writing or moving it) and gives the runtime a private copy *without the refresh token*, deleted after the call. Codex therefore can't change or renew your real login. When the login has expired, or is due for Codex's routine renewal (after about 8 days), Codex calls stop with "Your Codex login has expired or is due for renewal". Use Codex once yourself (it renews the login automatically) or run `codex login`, and Harvey picks up the renewed login on its next call. Harvey never renews it for you.
- **Models.** Set `brain.model` to an exact model ID from your account's catalog, including Luna, Sol, and Terra models such as `gpt-5.6-luna`, `gpt-6.1-sol`, and `gpt-5.6-terra`. `gpt-5.5` remains supported when present in your account's catalog and is the setup suggestion. Harvey creates a private catalog copy that preserves model capabilities while selecting direct tool mode and removing experimental tools. The model receives no tools under Harvey's lockdown. A blank `model` selects the highest-priority listed model that can use this configuration; hidden models require an explicit ID. An explicit model is used exactly or the call fails. Unknown model IDs and unrecognized tool modes are rejected. Availability depends on your ChatGPT account.
- **Quota.** The budget gate and dashboard read your ChatGPT plan's real rate-limit windows from Codex, for the limit bucket that applies to your model, and compare them to `usage.max_daily_claude_percent`. If Codex reports that usage is blocked, or a call fails on the plan's usage limit, Harvey treats the plan as full (even when no window is reported) and stops Codex calls for an hour. If the limits can't be read, the dashboard and `harvey usage` say "Quota unavailable" and the budget falls back to Harvey's own daily call count. Usage is recorded per provider; Codex calls are logged with tokens but no dollar cost, because the plan isn't billed per token.
- **When Codex can't be used, Harvey pauses rather than guessing.** Before each heartbeat, Harvey checks everything a call needs except the prompt itself: the Codex CLI version, the isolation, your login (expired or due for renewal), your ChatGPT account, and that your configured model can run with no tools. No prompt is sent, so nothing is used from your plan beyond a quick account check. If something is wrong, the whole cycle is skipped: no reply is read or marked handled, nothing is drafted or sent. The log line, `harvey status`, and the dashboard's Actions card say "Codex paused" and why. If Codex stops working part-way through a cycle (for example the plan hits its usage limit), the reply being handled is left exactly as it was for a clean retry, not escalated, half-recorded or lost, and that cycle sends nothing. Fix the cause (usually `codex login`), and Harvey resumes on its next heartbeat: a renewed login or reinstalled CLI is noticed immediately, a usage-limit pause ends after an hour, and other fixes are picked up within 30 minutes. `harvey status --check` runs the same check on demand and exits non-zero if it fails. Brief failures (a crash or timeout) just skip one cycle. Harvey never falls back to Claude or an API key.
- **Requirements.** **macOS or Linux**, Codex CLI **0.159.3** exactly, and Node.js for its npm package. **Windows isn't supported for Codex:** Harvey refuses it there before reading your login; use the Claude provider on Windows. Harvey checks the Codex version before every call and refuses any other version, including newer ones, because a different Codex can add tools Harvey's lockdown doesn't know about. Support for a new version comes with a Harvey update. Live use with a real ChatGPT account has been verified on macOS and ARM64 Linux. The offline suites also passed on x86-64 Linux under CPU emulation; CI covers Linux and macOS. The Docker image ships the Claude CLI only, so Codex isn't supported in Docker. To re-verify the lockdown offline against your installed CLI, with no credentials or network: `HARVEY_CODEX_CLI_TESTS=1 pytest tests/test_codex_cli_surface.py`.
- **Overhead.** Each call checks the CLI version, starts Codex once without your login to verify its configuration, reads the model catalog, then starts it again to run the prompt; each heartbeat's check does the same without the prompt. That adds a few seconds per call, and one Brain's Codex calls run one at a time.

---

## Legal and deliverability

Harvey automates outreach, but **you are the sender.** Cold email is legal in most places when done right and expensive when done wrong — CAN-SPAM penalties run to $53,088 per email. Harvey ships with compliant defaults. Keep them.

**CAN-SPAM (US).** Truthful subject line and accurate from-address — Harvey's copywriting rules forbid fake "re:" threads and impersonation. A working opt-out, honored fast: every cold email ends with an opt-out line (`compliance.opt_out_line`, default *Not interested? Reply "no thanks" and I won't email again.*), and Harvey treats any opt-out wording as immediate and permanent, wherever in the reply the person wrote it (including a bare "STOP" or "No thanks"). Nothing Harvey does later moves someone off *opted out*: a later message from them is kept for you to read, never answered, and a complaint still goes to you. To see why someone was opted out (the rule that fired, the words, and their message), click **Why?** on their row in Contacts. If it was a mistake, **Undo opt-out** there marks them as replied: Harvey sends nothing on its own, and their next reply gets a draft for your approval. Your physical mailing address in the footer: set `compliance.postal_address` in `harvey.yaml` before your first campaign. Harvey adds both lines under every cold email when it sends (replies don't get them), shows them under each draft in the Outbox, and warns on `harvey run`, `harvey status` and the Outbox tab until the address is set. Text in a reply that repeats what Harvey wrote for that person (its emails, campaign templates, and every footer it has used) is set aside as quoted, not read as their words, so quoting Harvey's own footer isn't an opt-out; the model also reads replies without the quoted email. The opt-out line must ask for a reply Harvey recognises (`Reply "no thanks"`, `Reply STOP`, also "unsubscribe", "remove me", "opt out") and can't contain a link or email address.

**Do not contact.** List competitors, existing customers, or anyone else Harvey must never email under `compliance.do_not_contact`, one address or domain per line, in the config file Harvey reads: `harvey.local.yaml` if you trained Harvey (that file wins), otherwise `harvey.yaml`. A domain covers its subdomains (`acme.example` also blocks `mail.acme.example`, not `notacme.example`), an address covers its plus-tags (`jane@` also blocks `jane+news@`), and a pasted link such as `https://www.acme.example/about` or `Jane Doe <jane@acme.example>` counts as what it names. An entry that is neither an address nor a domain stops Harvey from starting, with the entry named, rather than quietly protecting nobody. A running Harvey picks up an edit at once, and the send gate checks the list as each email leaves, cold or reply, so it holds even for an email you already approved. If someone on the list writes back, Harvey doesn't draft an answer: the conversation comes to you. `harvey export` and the dashboard's Export buttons leave them out too. On Instantly, listed leads are left out of the upload and a reply to one comes to you, but Harvey can't take back a lead Instantly already has: remove it there.

**GDPR / PECR (EU & UK).** B2B cold email needs a defensible legitimate interest: the pitch must be genuinely relevant to that person's role, you must know where the data came from, and objecting must be effortless. If you can't say why a specific person would care, Harvey shouldn't email them — and its qualification rules say so.

**Per-recipient check (optional).** Those rules turn on facts about each recipient: a UK sole trader needs consent where a limited company doesn't, Germany wants prior consent even for business email, and Canada's CASL depends on whether the address was published. With `compliance.precleared.enabled: true`, Harvey asks [Precleared](https://precleared.dev) about each new recipient before the first cold email. Anything other than `ok_to_email` holds that email back, with the rule and the next step shown in the Outbox, and the signed receipt id is logged with every send. One check per address, cached for 30 days (changing the sender country, address source, product description or the country sent with it triggers a fresh check); replies are never checked; native providers (Gmail, Microsoft 365, SMTP) only. Harvey tells Precleared the recipient's country when it is sure of it from the company's location (a country, a US state, a Canadian province or an Australian state named there, as for the send window), and Precleared takes that as settled; otherwise Precleared works the country out from the address, company records or the company's website. If all your prospects are in one country, also set `prospect_country` (for example `US`), which Precleared uses only when none of those show it; without it, the one country every `icp.geography` entry is in serves as that default. Precleared receives the recipient's address, your sender country, the address source, your product description and the recipient's country when Harvey is sure of it, else your prospects' country if there is one. It also receives the company's name and the person's name when Harvey has them, which it uses to find a UK company on the Companies House register when the domain alone doesn't (it doesn't store the person's name). Without a key it allows 10 checks a day, and a free key (`PRECLEARED_API_KEY`) allows 25. Put the setting in `harvey.yaml`, or in `harvey.local.yaml` if you trained Harvey, since that file wins. [docs/precleared.md](docs/precleared.md) has the details, verdict by verdict.

**If the check is held up.** When Precleared can't answer (the day's free checks are used up, the key is out of checks, was rejected or is being rate limited, or Precleared can't be reached), cold email to new addresses waits and replies still go out; email to people Precleared has already cleared keeps going. If Precleared can't check one address, that email is tried again six hours later and the rest carry on, unless that happens at two different domains with no answer in between, which holds new addresses as above. The Today page and `harvey status` say how many cold emails are waiting on the check and what fixes it.

**Bot disclosure.** Some jurisdictions require disclosing automation. Harvey is instructed to answer truthfully, always, if a prospect asks whether they're talking to an AI. Never configure it otherwise.

**LinkedIn.** Browser automation violates LinkedIn's ToS and can get the account restricted. Off by default. If you turn it on, use an account you can afford to lose.

LinkedIn requires both `channels.linkedin.enabled: true` and configured
credentials. Training a product does not enable it; the setup wizard records
your explicit choice.

**Deliverability — warm up or burn out.** Sending cold email from your main domain, or at volume on day one, lands you in spam permanently.

1. Buy a **dedicated sending domain** (`getacme.com`, not `acme.com`).
2. Set up **SPF, DKIM and DMARC** on it. Without all three, Gmail junks you. `harvey warmup` checks them, plus domain age, blocklists, your warmup progress and bounce rate, and ends with READY / NOT READY. It only reads public DNS and Harvey's own records.
3. **Warm up for 2–4 weeks** before real volume, and keep warmup running after. We recommend [TrulyInbox](https://trulyinbox.com/?via=harvey); see [Warmup with TrulyInbox](#warmup-with-trulyinbox).
4. **Ramp slowly** — 10–20/day per inbox, adding ~5/day. The `max_daily_sends: 50` default is a ceiling, not a target. Set `channels.email.warmup.start_date` and Harvey does this for you (Gmail, Microsoft, SMTP; Instantly has its own warmup): no cold email for `min_days`, then a cold-email cap that climbs by `daily_increase` a day. Replies always go out.
5. **Watch bounces.** Above ~3%, stop and fix list quality. Harvey trips its own kill switch past your configured threshold.

*None of this is legal advice. Sending at scale or into regulated industries? Talk to a lawyer.*

### Warmup with TrulyInbox

Whether a mailbox is warmed isn't something DNS can tell you: only the warmup
service sees where its mail lands. We recommend [TrulyInbox](https://trulyinbox.com/?via=harvey), and Harvey
connects to it so `harvey warmup` can say whether your mailbox is actually ready:

1. **Get TrulyInbox** at [trulyinbox.com](https://trulyinbox.com/?via=harvey) and add your sending mailbox.
   Connecting Harvey needs API access, which starts on the Starter plan.
2. **Create a key.** In TrulyInbox, open Settings > API keys and create a
   workspace key with `accounts:read`, `warmup:read`, `analytics:read` and
   `workspace:read`.
3. **Connect it**: `harvey trulyinbox connect`, or paste it into the warmup step
   on the dashboard's Today page. Harvey checks the key before saving it to `.env`.

`harvey warmup` then reads the mailbox from TrulyInbox and calls it ready only
when it's enrolled, signed in, has warmed for `min_days` (14 by default), and
less than 5% of its warmup mail landed in spam. Harvey only reads: it never
enrolls, pauses or changes anything in your TrulyInbox account.

Already warming with another service? `harvey trulyinbox other` (or *I use
another service* on the dashboard) and Harvey stops suggesting it.

---

## Troubleshooting

**`command not found: harvey`** — `source .venv/bin/activate` first.

**`ModuleNotFoundError: No module named 'harvey'` after install (macOS)** — Python 3.13 silently ignores `.pth` files carrying the macOS hidden flag, and some Macs propagate that flag into `.venv`. Run `harvey install` again (it auto-fixes), or: `ln -s "$(pwd)/harvey" .venv/lib/python3.13/site-packages/harvey`

**`externally-managed-environment`** — Use a venv, not system Python.

**`SyntaxError` or odd `ImportError`s right after install (macOS)** — Your venv is Python 3.9. `python3 -m venv` builds from `/usr/bin/python3`, which macOS keeps at 3.9. Check with `.venv/bin/python -V`, then rebuild: `rm -rf .venv && python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .`

**Claude headless mode fails** — `claude login`, and confirm the subscription is active. Test with `claude -p "say hi"`. In Docker, mount `~/.claude` into the container (writable: the CLI refreshes its login there). If the log says the Claude CLI "lacks --safe-mode" (or another flag), run `claude update` (in Docker, rebuild the image); Harvey resumes on its own. If your Claude login depends on `env` or `apiKeyHelper` entries in your Claude settings files, set those variables in Harvey's environment instead: Harvey's calls don't read settings files. `harvey status --check` confirms the CLI without sending a prompt.

**Dashboard says "locked"** — Open the link `harvey dashboard` printed (or `harvey dashboard --url`). If it says the token file can't be used, follow the message: usually delete `data/dashboard-token` and restart the dashboard. Visit it as `127.0.0.1` or `localhost`; other host names are refused.

**Codex calls fail** — Read the log line; every Codex error says what to do. "Not logged in" or "expired or is due for renewal" → use Codex once or run `codex login` with ChatGPT. "Hasn't been verified with Harvey" → install the supported Codex version named in the message. "Isolation could not be verified" → a Codex config layer outside your `~/.codex` (for example a managed or `/etc/codex` config) forces a tool, hook, MCP server, instructions or custom endpoint back on; Harvey refuses to send prompts until it's gone. Model catalog errors → choose an available model ID from the catalog shown in the message, or leave `brain.model` blank to select a listed model. Harvey never switches to Claude on its own.

**Discovery finds nothing** — Confirm signals first (`harvey signals --confirm free`); nothing is collected until you do. If you're on the free OpenStreetMap source, it only covers mapped trades and is thin for service-area businesses — `harvey discover --providers` shows the alternatives.

**Overpass is throttling** — It's free volunteer infrastructure. Harvey walks three mirrors before giving up. Wait a few minutes, or use a paid provider for bulk work.

**Emails land in spam** — Almost always the domain, not the copy. Check SPF/DKIM/DMARC, confirm warmup ran ([TrulyInbox](https://trulyinbox.com/?via=harvey) shows the inbox rate; `harvey warmup` reads it once connected), halve your volume.

**Harvey does nothing during the day** — Check `quiet_hours` and whether it hit `max_daily_claude_percent`. `harvey status` shows current state; the `actions` table logs every decision.

**How do I stop it right now?** — `Ctrl+C`, or `harvey sending pause` to stop outbound while leaving the loop running. State is in SQLite, so it resumes cleanly.

**Where does my data live?** — All local: `data/harvey.db`, `.env`, `harvey.yaml`. Nothing goes anywhere except the APIs you configured.

---

## Project structure

```
harvey/
├── main.py              # heartbeat loop (email only)
├── brain.py             # Claude/Codex provider selection + skills + usage
├── claude_runtime.py    # Isolated Claude CLI calls (safe mode, no tools, clean dir)
├── codex.py             # Isolated Codex subscription runtime
├── state.py             # SQLite schema, migrations, observations, cohorts
├── signals.py           # the 23-signal catalog Harvey proposes
├── finder.py            # the contact finder (one company per job)
├── gate.py              # deterministic pre-send checks
├── dashboard.py         # FastAPI JSON API
├── dashboard_auth.py    # dashboard token, Host/Origin checks
├── collectors/
│   ├── discover.py      # provider adapters, junk filtering, entity resolution
│   └── profile.py       # the free HTTP profiler
├── agents/              # audience, writer, sender, handler, analyst
├── modules/             # module manifests and capabilities, the loader, core ingest and runs,
│                        #   jobs (the queue), worker (lanes, lease), job_kinds
├── integrations/        # mail providers, email finder, quota
└── web/                 # dashboard HTML/CSS/JS — no build step

skills/                  # ← the sales knowledge. Editable Markdown.
prompts/                 # ← agent system prompts. Also editable Markdown.
tests/                   # 242 tests
```

---

## Philosophy

**Autonomous doesn't mean unsupervised.** Quiet hours, spend caps, send limits, an approval queue, a deterministic pre-send gate, and a kill switch. Harvey asks before it spends and before it sends, until you tell it not to.

**You decide what a good prospect is.** Harvey proposes signals; you confirm them. A prospect list you can't explain is a prospect list you shouldn't send to.

**If it can be gotten deterministically, don't use a model.** Rank position, tech detection, junk filtering, the priority decision, the pre-send gate — all plain code. Claude or Codex is reserved for the parts that need judgment. That's why this runs on a subscription at all.

**A failure is an observation, not silence.** "No team page found" is a finding. "Overpass is throttling" is a finding. Silent failures look exactly like clean results, which is how a broken run gets mistaken for an empty market.

**The database is the asset, not the agent.** The initial list is worth little. The same signals observed over quarters — who changed agencies, who started spending on ads, who let their content go stale — is worth a great deal.

**Everything is editable.** Prompts, skills, config, the dashboard. All plain text. You don't need to be a developer to change how Harvey sells.

---

## Roadmap

**Done:** heartbeat loop · observation data model with governed vocabulary · user-confirmed signal catalog · cohort builder · discovery provider menu with cost estimates · free HTTP profiler with agency detection · pattern-first email finding with honest status · native mail providers (Gmail API, Microsoft 365 via Graph, SMTP+IMAP) · outbox approval ladder · deterministic pre-send gate · bounce detection and kill switch · reply handling with intent classification · subscription quota tracking · website trainer · web dashboard · CSV export

**Next:** people enrichment from public registries · consent-gated voice callbacks · calendar integration for auto-booking · scheduled re-observation so signal *changes* trigger outreach · multi-product support

> **On voice:** a fully autonomous AI cold dialer is not on this roadmap, and won't be. Under FCC 24-17 an AI-generated voice is an "artificial voice" under the TCPA, and 47 CFR 64.1200(a)(1) has no B2B exemption — penalties run $500–1,500 per call, uncapped, and some states ban it outright. Voice here will be **consent-gated**: inbound and opt-in-triggered callbacks only.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Current release: **0.2.1**.

---

## License

MIT

TrulyInbox links in this project are affiliate links: the maintainer may earn a commission, at no extra cost to you.

---

*"Put that coffee down. Coffee is for closers."* — Blake, *Glengarry Glen Ross*
