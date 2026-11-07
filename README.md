# Harvey RO

> **Based on [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers (MIT).**
> Harvey RO is an independent project that is turning it into a contact finder
> for Romanian businesses: ONRC registry import, activity search by CAEN code
> in words, Romanian website reading, and phone extraction. It does not track
> or contribute back to the original project. Credit for the original design
> and code belongs to Harvey's author and contributors; see `LICENSE`.

**An autonomous sales agent that runs on your Claude Code or Codex subscription.**

Harvey finds businesses worth selling to, learns something specific about each one, writes cold email that references it, sends it, reads the replies, and works the conversation toward a meeting. It runs on your machine, in a loop, on its own.

The unusual part: **there is no model API bill.** Harvey uses your Claude Pro or Max subscription through Claude Code, or your ChatGPT subscription through Codex. Model calls use the plan you already pay for, without an API key or per-token API billing. Claude is the default; [Codex is an opt-in provider on macOS and Linux](#using-a-chatgpt-plan-instead-codex). If you'd rather pay per token, an API key is opt-in too, under daily and monthly dollar caps you set ([below](#configuration)).

```
$ harvey run

============================================================
Harvey is online. Always Be Closing.
============================================================
Handler: processed 2 inbound message(s).
  handler   → Elena Park, Harbor Dental Studio     interested, answer drafted
  handler   → Priya Nair, Highline Roofing         objection, answer drafted
Campaigns: 'Phoenix HVAC' took 3 new address(es).
  writer    → 3 first emails: Phoenix HVAC hiring a dispatcher
  outbox    → 6 emails waiting for your approval
Cycle complete. Sleeping for 15 minutes.
```

And the dashboard it keeps, at `localhost:5555`. In this order: the Today screen, a
conversation Harvey handed back because a prospect asked for a contract, approving
a drafted reply from the outbox, the Signals tab of an earlier release (it is
gone: see below), searching contacts, and the same screen in dark mode:

![Harvey's dashboard: the Today screen, a conversation handed back for a human, approving a reply in the outbox, the Signals tab of an earlier release, searching contacts, then dark mode](docs/dashboard.gif)

<sub>The demo data is invented: "Bookwell" selling online booking to roofers, dental practices and HVAC shops, on `.test` domains.</sub>

---

## Table of contents

- [Why the subscription thing matters](#why-the-subscription-thing-matters)
- [What Harvey actually does](#what-harvey-actually-does)
- [Quick start](#quick-start)
- [What it looks for](#what-it-looks-for)
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

Harvey uses the subscription-backed CLI for the provider you choose: `claude` for Claude Code, or an isolated `codex app-server` for Codex. Both return text with model tools disabled (Harvey's agent, in the Chat tab, gets Harvey's own tools and nothing else). Harvey handles the prospecting and mail operations itself.

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

**Harvey also budgets itself.** It reads the active provider's subscription quota and throttles to leave headroom for your own Claude or Codex work. For Claude the numbers are what the Claude CLI itself reports: the rate-limit event in each of Harvey's answers, and, when there is no recent one, the CLI's `/usage` command (no model call, no tokens). Harvey never reads your Claude login. The Usage tab and `harvey usage` say when the numbers were last seen and from which of the two. Set `max_daily_claude_percent: 80` for either provider—the setting's name predates Codex support. If quota cannot be read, Harvey reports it as unavailable and falls back to its daily call budget; while a Claude window is unknown or its reading is over 30 minutes old, that budget holds as well.

Harvey runs locally with your own login. Check the terms and usage limits of the provider you choose.

---

## What Harvey actually does

Harvey runs a heartbeat: wake up, check the budget, read replies, send what's due, work the campaigns, log it, sleep. Every 15 minutes by default.

```
    ┌─────────────────────────────────────────────────────────────────────────────────┐
    ↓                                                                                 │
   Wake  →  Quiet hours?  →  Budget OK?  →  Read  →  Send  →  Campaigns  →  Log  →  Sleep
                  │               │
               (sleep)         (sleep)
```

Nothing is chosen: every cycle does the same bounded steps, in order, so an open conversation or paused sending never stops writing:

```
read replies  →  drain the Outbox  →  each active campaign: roll, check, write, stage  →  idle (analyze) if nothing was done
```

Sending waits when replies couldn't all be read (a reply that wasn't read can't have cancelled its follow-ups). Harvey sends through Gmail, SMTP or Microsoft 365 only: with any other `channels.email.provider` (Instantly, which Harvey no longer supports) the heartbeat sends and reads nothing and says why, in the log, on Today and in `harvey status`, while finding contacts and everything else goes on.

The heartbeat is the Email module's: it reads replies, writes and sends, and with the Email module off it does nothing. Finding companies, reading their sites, finding contacts and loading public data are **jobs** the worker runs (see [The queue](#the-queue-background-jobs)), whatever the Email switch says. The heartbeat never looks for people: the [contact finder](#the-contact-finder) does, on its own.

### The queue (background jobs)

Every long run is a job on the **lane** of the source it calls: a discovery search on its search module's lane (one job per search, so a run over five cities is five jobs), each company's site read on the website reader's lane, a registry load or pull on the registry's, a load or check of the financial statements or the taxpayer file on theirs, and checks at ANAF a hundred companies a job on ANAF's. A lane runs its jobs one at a time, in the order they were asked for; lanes run side by side, so a slow TinyFish search never holds up an OpenStreetMap one. The pace each service allows still holds request by request.

- **Asking twice.** A second Descoperă run, of the same source or another, is queued. The same search (source, phrase, place and settings) asked again while it still waits is refused, naming the job already there.
- **Money.** Each paid job reserves its estimate from its run's spend cap before it spends; the cap and the stop are read between pages, and each page's cost is recorded as it arrives, so the cap holds even when a worker is killed mid-search. A search the cap stops is **paused (spend limit)**: raise the cap (Coadă, or `harvey jobs resume ID --max-spend N`) and it carries on from the page it reached, without paying for the earlier pages again. `spend.per_day_usd` and `spend.per_month_usd` in harvey.local.yaml (UTC, none by default) limit every run together.
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

# 4. Find businesses. Free source, no account needed.
harvey discover

# 5. Watch it work
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

# Claude's terminal login comes from the host's ~/.claude, which must exist:
# Compose refuses a missing folder rather than making one owned by root.
# Sign in to Claude there first, or just create it (mkdir -p ~/.claude) if
# Harvey will use Codex linked from the dashboard or an API key.
# The image ships both CLIs (Codex pinned to 0.159.3).
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

Chromium is optional, for the sites that refuse Obscura
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

**Free space on WSL.** Before a big write (a download, a register load, the
database upgrade) Harvey checks free space on the data's disk and on the drive
under it. Under WSL the Linux disk is a file on a Windows drive, which can fill
while Linux still shows room, and a container can't see that drive. Compose
mounts `HARVEY_HOST_DRIVE_SOURCE` read-only for the check: the checkout by
default (right on native Linux). On WSL, the folder must be an empty one of
its own on the Windows drive that holds your WSL disk, which isn't always C:
(Windows records each distro's disk folder; a moved or imported distro can be
on any drive). Run this once from the WSL shell, in the checkout: it finds the
drive, creates `harvey-disk-check` on it and adds `HARVEY_HOST_DRIVE_SOURCE`
to `.env`. A folder already set there is checked instead: kept when it's an
empty folder on that drive, reported with what to change it to when it's on
another drive (older instructions said C:), missing or not empty.
Then recreate the container. Until then, big writes are refused with a message
saying so. Only the folder's free space is read, which is the drive's. Don't
mount the drive itself: the container reads untrusted websites and would see
every file on it.

```bash
scripts/disk-check-folder.sh
docker compose up -d harvey
```

Outside Docker, Harvey finds the drive the same way by itself
(`HARVEY_HOST_DRIVE` overrides it).

The dashboard listens on 5555. If you change the port in the Compose `command`,
change the Dockerfile health-check port too and rebuild. Stop the service with
`docker compose down`; its files remain in the checkout. Use a host install if
you need the Codex provider.

---

## What it looks for

Harvey knows how to collect **31 signals** about a business. Each module collects the signals it declares while it is on (the dashboard's **Surse** page, or `harvey modules enable|disable <id>`): there is nothing else to switch on. Spend is held where it happens, never by a list of signals: a paid source runs only inside its run's cap, and Harvey's agent only inside the budget of a plan you approved.

```bash
harvey signals                       # the signals collected, each one's cost and counts
```

Every signal shows what it is in plain language, **what it costs**, and how many companies it was checked for.

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

### Then a list is a query, not a file

The Companies and Contacts filters (and Harvey's agent) turn signals into a target set — pick what a good prospect **must have**, what **disqualifies** them, and bounds for the figures (turnover, employees):

```
Has a marketing agency  AND  running Google Ads  AND NOT  already has online booking
→ 23 companies
```

That is a campaign with a reason behind it. You know exactly why each of those 23 is on the list, and so does the email Harvey writes them.

Each condition is true, false or **not checked yet**, and every company lands in one of three tabs: **match** (every condition checked and met), **possible** (nothing rules it out, but something isn't checked yet) and **no match**. "NOT running ads" counts only companies checked and found without them, never ones nobody looked at. A disqualifier alone is a list too. `harvey signals` shows, per signal, how many companies are yes, no and not checked.

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
- **The signals:** collected while the `startups` module is on. They read the homepage and
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
to profile. With the `hiring` module on, profiling reads the board their site links
to and records the role (`HIRING_ROLE`); the search hit alone records nothing.

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
expenses), `TURNOVER_GROWTH`, `ACCOUNTS_FORM` and `FILED_ACCOUNTS`, one
reading per year; cohort ranges compare the latest year on
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

**Areas, not a fixed 40 km.** A place is searched by its own size: its circle
is the one that holds the box Nominatim gives the place (a town a few km, a
county tens), and `icp.geo_coordinates` still overrides it. Each kind of
source covers an area its own way, as its module's manifest says (`area` on
the capability):

- **One circle** for a listings source with no result cap (DataForSEO Business
  Listings): one search a phrase over the whole area, and what comes back
  outside the area is dropped (by its outline when the boundaries are loaded,
  else by its box). For Google data on firms the registry already lists, this
  is the cheap way: one $0.012 request per 1,000 businesses plus $0.00036 each.
  Every business in the circle is billed, those outside the area too, so
  neighbouring areas' circles pay twice for where they overlap (the count
  before a run says how many); an area inside another the same run names
  isn't searched again by a circle or a grid, only by a rank search.
- **A grid** for a map search that serves at most a fixed number of places
  (Scavio Maps 120, DataForSEO Maps 700, `max_results` in the manifest): the
  area starts as a few coarse cells (a town one, a county 2 × 2), each
  searched from its centre at the zoom its size gives; a cell that comes back
  full is split into four, queued as more pages of the same search under the
  same cap, and a cell whose page is mostly places earlier cells found stops
  there. The estimate counts the starting cells and says splits may add more,
  within the cap.
- **A point** for a ranked web search (DataForSEO SERP): rank from the area's
  point.

**Boundaries (optional, off by default).** The `boundaries` module loads
OpenStreetMap's administrative boundaries for the regions the market packs
name (Romania: `europe/romania`, a Geofabrik extract of about 320 MB, ODbL:
© OpenStreetMap contributors) into `data/boundaries.db`: every country,
county, town, commune and city sector with its admin level, ref tags (such as
`ref:siruta`), parent, outline, centre and area in km². Turn it on in
**Surse**, then load it on the public-data page (with its size and licence)
or `harvey boundaries load`; the download is kept in
`data/boundaries/<date>/` and carries on from where it stopped. It needs
pyosmium, in the image since this release (`docker compose build harvey`
when the page says "library missing"). With it an area has its real shape,
a homonymous county and town are told apart, and the agent sees an area's
sub-units.

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

On those pages it finds the people named in team cards and lists (a name in a heading with its title after it, or a name on one line and its title on the next), in schema.org markup (an organisation's `founder`, `employee`, `member`, `owner`), and in sentences: "owned and operated by…", "founded by…", "our founder, …", "Dale Whitlock, owner", and in Romanian "fondată de Dr. …", "fondatorul clinicii, …", "… a fondat clinica". CSS team cards are read only when none of those name anyone. The likeliest owner comes first (an explicit owner or founder title on the business's own card, then the others), twelve people at most; people the site remembers ("the late…", "retired in 2010") and a founder known only from a history sentence when the site lists a current owner are left out. Each owner is a `LIKELY_OWNER` reading, with the sentence or markup that says so. Headings made of ordinary words ("Team of Experts"), customers quoted in testimonials ("Founder at Quillnest") and titles that belong to another organisation are not people. A registry pull adds the registry's administrators. Everyone named, with no email or phone of their own, is a person to search (**People to search** on Contacts), and the [contact finder](#the-contact-finder) checks one address for each, up to three per company per search, owners first.

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

**`sales_methodology.md`** — The operating philosophy: the ABC loop, how a conversation should flow across stages, tone calibration, and the ethical lines Harvey does not cross.

**`offer_strategy.md`** — The offer ladder by engagement level, and the rule that governs it: *never lead with an offer, never pitch in a cold email.* First contact exists to start a conversation. Offers come out only after genuine interest.

**Generated for your product by `harvey train <url>`:**

- **`product_knowledge.md`** — what you sell, the benefits, pricing, use cases, the pain you solve, buying triggers
- **`competitive_intel.md`** — battle cards per competitor, differentiation angles, migration paths

*(Both are gitignored. Your positioning and pricing shouldn't land in a public repo by accident.)*

### Which agent gets which

Defined in `harvey/brain.py` — edit the map to change it.

| Skill | Writer | Handler | Sender |
|---|:-:|:-:|:-:|
| `signal_playbook` | ● | | |
| `email_frameworks` | ● | | ● |
| `sales_methodology` | ● | ● | |
| `offer_strategy` | ● | ● | |
| `objection_handling` | | ● | |
| `product_knowledge` | ● | ● | ● |
| `competitive_intel` | ● | ● | |

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
| When it is approved | When you approve it | When the drain that sends it runs, after its review: never while sending is paused, so resuming sends nothing unreviewed |
| An answer to a reply | As you approved it | With `compliance.ai_disclosure_line` under it ("Acest răspuns a fost scris cu ajutorul unui asistent AI." by default; blank turns it off) |

**Turning autopilot on.** Approve a batch or two by hand first and read what
Harvey writes. Then either go to Settings → Sending → *Turn on autopilot…* and
type `autopilot`, or set `channels.email.require_approval: false` in
`harvey.local.yaml` (see [Configuration](#configuration)). A running
Harvey picks it up from its next cycle, with no restart.

**Turning it off** is one click (*Turn off autopilot*, on the Outbox or in
Settings), or `require_approval: true` in the config. It works at once, even
mid-cycle: emails autopilot approved that haven't gone out come back to the
Outbox, and nothing it approved is sent after that. Emails you approved
yourself stay approved. (Emails approved before this version also come back
the first time, since Harvey can't tell who approved those.)

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
each one gets read before it goes.

**Instantly is no longer supported.** Harvey sends and reads mail through
Gmail, Microsoft 365 or SMTP, where every email passes the Outbox, the
do-not-contact list, the source note and the unsubscribe header. A config
that still says `provider: instantly` loads, but nothing is sent or read: the
log, `harvey status` and the dashboard's Today page say to choose gmail, smtp
or microsoft (Settings → Sending), and finding contacts and everything else
keeps working. Campaigns an earlier Harvey deployed to Instantly keep running
there until you stop them in Instantly; Harvey still counts their people as
contacted, so no campaign here writes to them again within 90 days.

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
- **Today** — four headline figures with 14-day trends (contacts found, emails sent, conversations, waiting on you), then everything that needs a decision, contacts by stage, collector runs, the newest job runs with what each took and cost, and what the pipeline is doing right now. Setup shows here until it's finished, then disappears.
- **Outbox** — the decisions desk. Each email sits next to what Harvey knew when it wrote it: the company, how the address was found and verified, and the signals the email draws on. `J`/`K` to move, `A` approve, `E` edit, `R` reject.
- **Conversations** — every reply in the context of the emails that led to it. Threads Harvey handed to you (a contract request, a reply it had no safe answer for) come first, with **Hand back to Harvey**, **Close** and **Reopen**.

**Pipeline**
- **Discover** — the provider menu with prices side by side, an estimate, then a run; plus reading the sites of companies found but not yet looked at
- **Companies** — everything found, one row per company with its phone, email, activities, latest turnover and headcount, contact count, source, when it was found and last searched. Sort by newest found, name, town and county, turnover, employees, number of contacts or last searched, either way (a search adds best match). Filters: county, town, activity (CAEN codes or words), has a website, has a contact (any, phone, mobile, email, none), source, signals the company has or lacks, turnover and employees when the financial statements are loaded, and the search box (words, or a CUI). Exact total, page numbers, a page-number box and the page size; a jump to page 10,000 is as quick as page 1. A filtered or figure-sorted list is ordered once (a few seconds for a signal filter or a turnover sort over a million companies) and kept, so the pages after it are instant; a company that gains a contact or a search leaves or moves in the list at once. A number no company has as its CUI is searched as a word. The Actions menu (CSV, JSON, ANAF check, Find contacts) takes the selection or the whole filtered list in its order. A company's panel shows everything on file: its CUIs (a second one a source gave too), legal form, status and registered office, every financial year (turnover, employees, profit, equity, debts), the trade registry's classes and administrators, ANAF's kept answer, the taxpayer file, contacts with kind, trust, source and status, each person with their own contacts, places, sources, signals and the search log, with **Find contacts** and **Search again** for that company. A register that isn't loaded, or has nothing on the company, says so.
- **Registry** — the whole trade registry (`data/registry.db`, every firm in Romania, operating or not), each firm beside its latest financial statements. Filters are the registry source's own, the ones Discover shows for it: activity (words or CAEN codes), county, town, status (operating, or every registration), turnover and employee bounds (firms with no figures left out or kept), employer, VAT, micro-enterprise tax. Sort by name, turnover, employees, net profit or registration date, either way. The total is exact, with page numbers and a page box; the first page takes about a second over 4 million firms, a sort or broad filter one to four the first time it is asked (less once the background rankings are ready, a few seconds after the page opens), and every page after it is instant. A search of only excluded words ("-auto") lists every firm but those. Each firm says whether it is already in Harvey (by CUI, or by registration number for a firm with none). Ticks are cleared when the filter changes. Its panel shows every financial year side by side, the registry's record with every CAEN class and the administrators, the taxpayer file's row and ANAF's kept answer. **Pull into Harvey** takes the ticked firms (up to 10,000) or the whole list: counted first, free, and queued as a registry pull, followed by the ANAF checks and the contact finder like any Discover run.
- **Contacts** — every way to reach a company, one row per distinct email or phone (a chain's shared number is one row reaching all its companies; expand it to see them), with its kind, phone line type (mobile, fixed, VoIP), status, trust and sources. Filters: kind (email, phone, mobile), status, trust, has a person, source, county or town, company has a website, signals the company has or lacks (the cohort rules), turnover and employees when the financial statements are loaded, and text. Paged on the server; past 100,000 the count says "over N". **People to search** lists everyone named at a company (a registry's administrators, a team page) with no email or phone of their own yet. **Find contacts** runs the [contact finder](#the-contact-finder) over the current filters, priced first (the searches, and the classifier's questions next to them) and under the caps you type, then shows the run's report; tick **Search again** to also search those already searched. Tick any contacts for **Actions → Verify with the classifier** (or verify one from its **Details**): yes, no or unsure, priced first and under a cap, each answer on its row and a no undone with its run; the filter **Classifier's verdict** lists the unsure ones for review ([verifying contacts](#the-classifier)). Above the list, the finder's backlog. CSV and JSON exports list each contact once and leave do-not-contact ones out. The Email module's outreach list is a third view while that module is on.
- **Campaigns** — the campaigns you made and the ones Harvey made earlier: status, members, emails sent, replies; open one for its members and where each got, its filter (**Open in Contacts**) and its legitimate-interest note; pause, resume, stop, archive. **Publicuri** lists the saved audiences (preview, rename, open in Contacts, archive). **Campanie nouă** is the wizard: an audience (a saved one, or the Contacts page's current filter) with a preview of what each rule leaves out and how many days the first step takes at today's cap; *roll* to take new matches as they appear; the offer, goal and booking link; the sequence, written by you or generated (**Generează secvența**) and edited; the sending account; *always review*; the legitimate-interest note; then **Pornește**. New campaigns need Gmail, SMTP or Microsoft 365. Each sequence, step by step, is listed under the table, with per-campaign feedback for the writer.

**System**
- **Queue** — every background job per lane: running (with progress), waiting, paused, done, failed; pause a lane, cancel, retry or resume a job. A run opened from elsewhere shows its report (time, and cost split into searches, address checks and the classifier) and its caps to raise.
- **Chat** — conversations with Harvey's agent about your data ("how many dental firms in Cluj have a phone but no email?", "what did last week's searches cost?"), kept like on claude.ai: a list of your chats (newest first, search by title, rename, delete), **New chat**, and any chat opened again and continued. It reads through Harvey's own tools, on your Claude plan inside Harvey's share of it (or your ChatGPT plan, with `brain.provider: codex`). To do work for you it proposes a **plan**: its goal, its steps and a budget sized from the estimates, shown as a card under its answer; nothing runs until you **Approve** it (a dialog names the budget) or `harvey agent plans approve ID` (you type the budget back). Under an approved plan it can queue what the Discover, Registry and Contacts pages queue (a Discover run, a registry pull, Find contacts, Verify contacts), through the same code, caps and day and month limits, every run's caps coming out of the plan's budget; it works on filters or sets it saves (by a filter or one SELECT of ids, resolved on Harvey's side, so it never holds your rows), follows its runs and reports in counts. The card shows what the plan's runs committed and spent, each run (to the Queue) and **Cancel**, which stops its runs too; deleting the chat closes its open plans (its runs finish under their caps). It also **prepares campaigns** under a plan: it saves an audience from a Contacts filter (with the preview's counts), makes a draft campaign through the wizard's own code (an offer, a sequence written to `skills/email_frameworks.md` in the install's language and checked against the send check's rules, the legitimate-interest note), and asks you to start it: the plan card shows a start request with the audience counted, the sequence, the note, the sending account and what is still needed, and a button to the draft's **Start** on Campaigns. You start it, as always (the acknowledgement, the note, your confirmation). Then Harvey wakes the agent when the campaign takes new members, when a reply comes in (with only what Harvey's reply reader derived: its intent, stage, whether they asked to stop or it needs you, never their words) and when the campaign finishes or a sending rail holds it; the agent reports in counts, can ask the reply reader to draft an answer with its guidance (it always waits for your own approval in the Outbox, even on autopilot), can put someone who asked to stop on the do-not-contact list (you lift such an entry on the list itself), and can pause a campaign of its plan. It can't send or approve email, start or resume a campaign, turn on autopilot, undo a do-not-contact entry, merge or change settings, and never approves its own plan: no tool does those. Job and provider error text never reaches it (it sees an error's kind), and short text from websites and listings is marked as data, never instructions. The answer is written as it comes, with each tool call shown as it runs; **Stop** ends an answer, **Retry** sends a failed or stopped one again, and the chat's context size says when a new chat would cost less (every answer carries the conversation so far). The agent answers from counts, not records: one call counts what any Contacts or Companies filter selects (with and without a website, with an email or a mobile, ...), rows only ever come back as a small sample, and email addresses, phone numbers and people's names never reach the model in full. Up to three chats answer at once; a chat's process closes after 10 minutes without a message, and the next message picks the conversation up from Harvey's database. While you are away, Harvey **wakes** the agent under an approved plan: when one of the plan's runs finishes, or at a time it scheduled (at least 10 minutes ahead, never past the plan's end). The wake is a job on the agent's own lane: it checks the runs, takes the plan's next step within its budget and writes a short report in the plan's chat, marked "Harvey, after run … finished" and shown as new in the chat list (with nothing to say it writes nothing). At most one wake per plan waits at a time, 12 a day, within Harvey's share of your Claude plan; while you are in the chat it waits for you, and the plan card lists its scheduled wakes with **Cancel**. For a broad research question (many areas, phrases or sources) or the check of a draft campaign it can hand the work to a **task**: a child run under the plan, as a job on the same lane, with only a few read tools (a researcher's: counts, the schema, estimates, areas, phrases; a campaign check's: the writing rules, its own draft to fix, the campaign's counts), no chat history and a few turns; it can't spend, queue a run, send, schedule or remember, and its answer is one JSON object Harvey checks before the agent reads it on its next wake. At most 2 a plan at a time, 6 a day; the plan card lists them with their result and the tokens they used, with **Cancel** (or `harvey agent tasks`). The agent also **remembers**: short notes (2,200 characters in all) given to every new conversation, and named playbooks it reads when it needs one, with emails and phone numbers masked; the Chat tab's **Memory** view (or `harvey agent memory`) shows them for you to edit or delete. It runs on Claude, or on Codex with `brain.provider: codex` ([Codex](#using-a-chatgpt-plan-instead-codex)). Same from a terminal: `harvey agent chats`, `harvey agent chat`, `harvey agent ask "..."`, `harvey agent log`, `harvey agent tasks`, `harvey agent memory`.
- **Activity**, **Usage**, **Controls**, **Settings**, **Help**. Settings → **Model** is how Harvey thinks: Claude or Codex (Codex is greyed out on Windows, with why), the model (Claude's aliases opus, sonnet and haiku, or blank for the CLI's default; for Codex the models your account lists that Harvey can run, read live), Codex's reasoning effort, how the CLI is logged in, and a status line: the CLI's version, linked as what (the login method and the plan, never an email), the class of the last error. Saving writes `brain:` in `harvey.local.yaml` (comments kept); a running `harvey run` takes it up when restarted, new chats at once. It offers the CLI's own **terminal login** (`claude login` / `codex login`, as before) and an **API key**, billed per token: a password box (checked with a free call before it is saved, never shown back), the daily and monthly dollar caps, Codex's token prices, and Unlink; the status line then says "API key set · today $x of $y". Linking an account from the dashboard comes later. Settings → **Classifier** says what the classifier is for and when it is asked, sets each task's profile, lists the profiles side by side with what they measured, and holds its caps ([the classifier](#the-classifier)). Usage shows Claude or Codex quota gauges and per-agent token counts, with no dollar figures: you're on a subscription and aren't billed per token, so showing costs would be made up. With an API key it shows today's and this month's dollars beside the caps instead (`harvey usage` too).

| Outbox | Conversations |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/outbox-dark.png"><img alt="The outbox decisions desk: a drafted reply to an objection, with the company, contact, verification and signals beside it" src="docs/screenshots/outbox-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/conversations-dark.png"><img alt="A conversation Harvey handed back: the two emails sent, the prospect asking for a contract, and the Hand back to Harvey and Close buttons" src="docs/screenshots/conversations-light.png"></picture> |

Colour is kept for meaning. The chrome is neutral and a single blue marks what you can act on. Status (waiting on you, done, failed, running) uses the same small set of tones in every tab, so "waiting on you" looks the same wherever it shows up. Figures are tabular, so columns line up and a changing number doesn't shift the layout.

Appearance has three states: `auto` follows your OS and is the default, or pick light or dark from the button at the bottom of the sidebar. Dark mode uses the same layout with a different set of colour tokens.

---

## How the data is stored

SQLite at `data/harvey.db`, and it holds **Harvey's own work only**: the
companies you pulled in or worked on, their contacts and the evidence for
each, the people Harvey found, searches, sources, merges, web signals, and
the email side (prospects, campaigns, outbox, conversations). The public
registers are not copied into it. Each register module reads its own file
in place (`data/registry.db`, `data/financials.db`, `data/taxpayers.db`,
`data/anaf.db`), through the indexes its load builds there, so a register
fact (turnover, VAT, an activity) is the register's current value, dated by
the register, and reloading a register changes what the pages show without
a write to `harvey.db`. On a full install that is about 1 GB instead of
16 GB, and every page answers in well under a second.

| Table | One row per |
|---|---|
| `firm` | company in Harvey, with an integer id; a merged record points at the one it was merged into (`live`), and kept counts (contacts, people, searches) the triggers maintain |
| `firm_ident` | identifier of a firm: a CUI, a registration number, a listing's id. A firm a register knows is described by the register (name, place, activities, administrators) |
| `local_firm` | description of a firm no register describes (a listing) |
| `obs`, `reading` | web signal history (a reading kept only when the value changes) and the current reading per firm and signal |

**Every fact is a row, never a column.** A signal is a code in the governed
vocabulary (`signal`), and a module declares how it reads it: stored (a web
signal Harvey observed) or read from a register. A new signal needs no
migration, history is kept on change, confidence and provenance travel with
the fact, and a cohort is a set query over readings and registers.

Kept values (counts per firm, per signal, totals) are maintained by
triggers; `harvey doctor --recount` compares every one of them with a full
recount (`--fix` writes them again).

### Upgrading from the old database

A `harvey.db` from before (schema 24, with copies of the registers inside)
is never opened by the new code: the dashboard shows an upgrade screen
instead of the pages, and the worker and `harvey run` wait. The upgrade
(dashboard button, or `harvey upgrade`):

1. builds each register's indexes in its own file (the trade registry
   first: the financial statements' "filed accounts" read it), then
2. rewrites `anaf.db` compressed (every answer kept, each one checked to
   read back the same),
3. moves Harvey's own data into a new `harvey.db.v25`,
4. verifies it against the old file (every kept count, the firm, contact,
   link, search and merge counts, each signal's counts, a few list filters,
   and every ANAF answer by hash), and only if that passes
5. switches: the new files take the live names and the old ones are kept as
   `harvey.db.v24` and `anaf.db.old`.

It takes about half an hour on a full install and needs at least 30 GB
free (the new files take about 3 GB). It can be stopped and resumed: each
step picks up where it stopped, a switch cut short included (`harvey.db` is
never missing meanwhile, and Harvey creates no new one while it waits). If
Harvey wrote to the old file after it was migrated, the switch refuses and
the next run migrates and verifies it again. Nothing is deleted on its own: once you're
happy, **Delete old files** (dashboard, typed confirmation) or
`harvey upgrade --delete-old` frees the space. In Docker on WSL the free
space check must see the Windows drive that holds your WSL disk: run
`scripts/disk-check-folder.sh` once from the WSL shell (it creates an empty
folder on that drive and sets `HARVEY_HOST_DRIVE_SOURCE` in `.env`), then
`docker compose up -d harvey`; never the drive itself. A database older than schema 24 is first opened once by the
Harvey release before this one. `harvey upgrade --data DIR --out OUT` is a
dry run on a copy: it writes only into `OUT`, reads the sources as files
that don't change, and refuses a folder Harvey is running on.

### Contacts, people, places, sources and searches

Harvey's job is finding ways to reach companies, so contacts have tables of their own:

| Table | One row per |
|---|---|
| `contact` | distinct contact: `(kind, value)` is unique, an email lowercased, a phone in E.164 with its line type (mobile, fixed, voip, toll free, unknown, from the numbering plan: an SMS audience is the mobiles), its status (an email's verified / risky / guess / invalid / unchecked, a phone's valid / invalid) and do-not-contact (an opt-out) |
| `link`, `link_evidence` | a contact reached for a company (one link per record found for and contact), and each source that gave it: optionally through a person and at a place, with the source and capability that found it, the page it was on, and its trust (confirmed, likely or unconfirmed). Many to many: a pharmacy chain's hundred companies can share one phone and one email, and that is one contact linked a hundred times, not a duplicate and not a merge. A contact is never thrown away because its match is unproven: it is kept with its trust and source, and you filter on them. |
| `person` | person Harvey found or acted on at a company (a team page's member; a register's administrator once the finder works on them). The register's administrators are read from the register |
| `locations` | place of a company: a listing that matched a company on file (by its website or phone) is one of its branches, with its own address and phone |
| `firm_source` | source a company was found in (one company, many sources), every registry pull included |
| `search` | search of a record (a company, a person) by a module's capability: what it looked for, when, what it found, what it cost |

The kinds of contact are an open list: modules declare them in their manifests (`[[contact_kinds]]`; core declares email, phone, contact_form, linkedin, facebook, instagram and whatsapp) and a trigger refuses any kind nobody declared, so a new channel is a new module. A social profile is only kept when the company's own website links to it: Harvey never reads LinkedIn, Facebook or Instagram.

**A record searched once isn't searched there again.** A company searched by one source (a site read, Serper, TinyFish, the address check) is skipped by that source on the next run, whatever it found, and can still be searched by any other: a search module you set up later goes back over the companies the others found nothing for. A plain **Read sites** read looks for phones and a contact form only, so it doesn't count as the finder's read of the site. The finder's read records what it looked for (`phone,email,contact_form,social,people,owner`), and only a read that looked for the owner closes it: a site the finder read before it followed the menu (its record has no `owner`) is read once more, the deeper way, in the background after the companies with no contact, whatever contacts the first read found. A capability can declare `refresh_days` in its manifest to search again after that long (by default, never). An error isn't a search done: it is tried again. To search again anyway, tick **Search again** next to **Find contacts**, or `harvey contacts --force`. The company panel shows where and when it was searched and with what result.

### The contact finder

Every company with no contact is a record to search, whichever source brought it in (the registry, a listing, a search). The finder (`harvey/finder.py`, the core `contact_finder` module) works on one company per job, on the **contacts** lane, and has nothing to do with the email loop: it runs whatever the Email switch says.

1. **What is on file, free.** Phones from ANAF and the taxpayer file are contacts already. A website the company's key or its registry entry gives is read: its phones (each marked mobile or fixed), the addresses it publishes (one on the company's own domain is trusted, another only maybe), its contact form, the social profiles and WhatsApp links its pages point to (never read on those networks), and the people its team, about and contact pages name (found through its own menu, owner first: see [Enrich](#3-enrich--who-decides)).
2. **A website search, only inside a cap.** A company with no website and no contact is looked up by its name and town through each search module whose capability says `lookup = "website"` in its manifest (TinyFish, Serper, Scavio, DataForSEO, Brave; not OpenStreetMap, which only searches by trade), cheapest first, until one gives a candidate. The results are read once, in memory, and never stored. Every result is weighed, not just the best-named one:
   - **Ruled out by its address, before any visit** (`harvey/site_match.py`): company-information sites, listings, booking platforms, marketplaces, social networks and news (`harvey/reference/directories.csv`), a page whose address carries the firm's CUI or registration number (a profile of it on someone else's site), institutions (`.gov`, city halls, county councils), a profile-shaped path on a host that carries no word of the firm's name.
   - **The classifier keeps at most three of the rest** (its `results` task, when one is set up: a yes or no for each result; else the best name matches). Only those are read, free.
   - **A register fact decides without a model.** The firm's CUI (in a fiscal context: `CUI`, `CIF`, `cod fiscal`, `cod unic de înregistrare`, `RO` glued to the digits or after a dash; never the same digits inside a phone or an IBAN) or its registration number on the pages, with no other firm's CUI beside it: **confirmed**. An official email or phone confirms only next to the firm's name (an email also at the site's own domain), and never one many firms share: a taxpayer file's email is often the accountant's, a PFA's register phone sometimes the city hall's switchboard. Its own CUI beside other firms' (a group's site listing its sister companies) is a question for the classifier. Another firm's CUI as the site's owner, with nothing of this firm's on it, or several firms' and not its own (a listing): not its site. (Another firm's code has five digits at least and isn't a year: "CIF 2020" is an Incoterm.)
   - **The rest goes to the classifier** (its `site` task): one yes/no question per candidate, each asked alone with the register facts and that candidate's evidence card (which facts appear and where, other CUIs, the phones' countries, a short excerpt of the contact, about and footer text, the contacts read there with the line around each). Exactly one yes is **likely**, never confirmed; none links nothing; two or more wait, unsettled. Then its `contacts` task asks, for each phone, email and profile read on the chosen site, whether it is the firm's own (not a web agency's footer phone, an office abroad, a theme's social profile, a public hotline); only the yes ones are linked, and the firm's official ones always (on a site a register fact confirmed, only what it says no to is left out). With no answer to that, nothing beyond what code confirms: a site a register fact confirmed links what code read, one the model chose only the firm's official phones and emails.
   - **With no classifier** (or held, or its day's calls or its USD budget used), a site no register fact confirms is kept unlinked, its trust **unconfirmed**, for `harvey contacts rejudge` to take later; a confirmed one links what code read off it (template placeholders such as `nume@exemplu.ro` left out).

   The search record keeps counts by reason and the domains Harvey read, never the other results. The company panel says why a found website was taken (*Why this website*: the firm's CUI on the site, or the classifier's yes and how sure). Below confirmed, the site says nothing else about the company (no facts, no new name, no people) and its domain doesn't become the company's key.
3. **One checked address per person.** For each person named at a company with a website (a registry administrator, a team page's member) and no contact of their own, one address built from the domain's pattern is checked with your verifier tier (Reoon, ZeroBounce, Hunter). A name can be written either way round, so neither order is assumed: when the company's own site names the same person ("Ion Popescu" on its team page, "POPESCU ION MIHAI" in the registry), the site's order is used; otherwise the likelier order is checked first and, only if that address comes back invalid, the other (a check each, against the month's checks). Which order is likelier for an official register is the market pack's hint (`registry_name_order` in its manifest: the Romanian pack says the family name usually comes first), not core's. The first given name of several is used; a hyphenated one is checked whole ("anamaria") and, only if that address is invalid, by its first part ("ana"). A team page's name confirms a registry person only when it carries the registry's family name and fits that one person alone; one address is checked once per company, for whoever it was read for first. The person keeps the registry's name as written; the search record says which order was used and whether the site confirmed it.

Paid steps (searches and address checks) run only inside a spend cap you set: a Find-contacts run's cap, or for background work `spend.per_day_usd` in harvey.local.yaml (off by default, and then the background never pays for anything). Without one they are skipped, the job says so, and the company stays on the backlog for a run with a cap. A search that costs nothing (a free tier priced at $0) needs no cap. Each verification service an address check asks (ZeroBounce, Reoon, Hunter; the SMTP check is free) is one check at that service, as its free tier and its bill count it, and one of the month's checks, `EMAIL_VERIFY_MONTHLY_CHECKS` in `.env` (600 by default, Reoon's free tier; every service together). Each is in dollars too: $0 inside the free checks its manifest declares (`[cost.verifiers.<id>]`: Reoon 600, ZeroBounce 100, Hunter 50), past them the price you enter for it in **Settings → Email verification** (or `harvey modules set email_verify usd_reoon 0.004`; `usd_zerobounce`, and the hunter module's `usd_hunter`; $0 to $10 a request). Hunter's pattern lookups are counted the same way (25 free a month, `HUNTER_MONTHLY_SEARCHES`, `usd_hunter_search`). A priced request is reserved from the run's cap (the same cap as the searches: it is the run's spend) before it is made, and what it cost is recorded after, so the cap and `spend.per_day_usd` / `per_month_usd` count it. No service declares a paid price, so with none entered a request past a free tier is refused under a USD cap (a cap can't stop what it can't count): the check that needed it is an error, not a check done, and the person is searched again once there is a price or a new month. The estimate names the services that would be refused, and the run's report counts the refused checks. The Email module's campaign checks follow the same rule under `spend.per_day_usd` / `per_month_usd`: a priced one is reserved from them and recorded with every run's spend, and with either limit set one past a free tier with no price is refused and the address waits (the campaign page and Today say why: no price, or the day's or month's limit reached), checked once it can be. With no limit set at all they go ahead and are counted unpriced.

**When it runs.** For every company with a website a Discover run saves or matches, in that run's batch, free: its site is read (a registry pull hands it to the ANAF checks first). A Discover run also looks up a website for every company it saves with none (on by default: **Also find websites, up to $X** on Descoperă, `--websites-max-spend` / `--no-find-websites` on `harvey discover`, `find_websites` / `websites_max_spend` in the API). It is part of the same batch: free searches always run, paid ones draw only from that amount ($0 unless you give one; the Descoperă field starts at $1.00), kept apart from the searches' cap (the batch's cap is the two together, and each job takes from its own share; raising the cap on the Queue page or with `harvey jobs resume ID --max-spend N` adds to the searches' amount, or to the share of the job resumed), and at most what the amount pays for plus 500 free lookups are queued per run, the rest left to the background's walk. The estimate and the run's progress show it. Those lookups read the site they find but check no addresses: that is a Find-contacts run's, priced and capped. On demand: **Find contacts** on the Contacts page (the current filters) and in the Companies menu (the selection or the search's results) price the run first (websites to read are free; companies with no website cost one search each through the cheapest module), then run it under the cap you type. In the background, the worker feeds the backlog a little at a time: companies with a website not read yet, newest first, free; then companies with no website that a search module you set up hasn't searched yet, through the free allowances search modules declare in their manifests (`[[free_allowances]]`, renewed every minute, hour, day or month, a month counted as the last 30 days: TinyFish's 30 a minute and 500 an hour, Brave's monthly credit), and with `spend.per_day_usd` set, through paid searches too. One-time sign-up credits (Serper's 2,500, DataForSEO's $1) are never declared, so they are never spent in the background without a budget. Every request is counted by Harvey itself (in `data/lanes/<module>.uses`, whoever made it), and the background takes only its share of each allowance (`background_share`, half by default), so your own runs always find some left; when its share is used it waits for the next period. **Settings → Background searches** turns it off or changes the share (the contact finder's `background_search` and `background_share` settings). Harvey can't see requests made with the same key outside it. When nothing is left it rests for 15 minutes before looking again. The Contacts page shows the backlog (companies with no contact, those with a website still to read, what the finder did in the last day, and each free allowance's use this period with what the background may still take), and so does `harvey contacts status`.

#### The classifier

A small, fast model that answers yes or no, separate from Harvey's own model (the one that writes and answers emails never uses it). It decides two things a search can't: whether a website found by searching a company's name and town is really that company's, and which phones, emails and profiles on it are the company's own. Every question is a statement about a short text (this result is the firm's site, this site is the firm's, this contact is the firm's). **Settings → Classifier** explains all of this on the card itself; nothing in it has to be set by hand in a file.

**When it is asked.** Only for a website a search found, never for one a register gives the company: Find contacts (the Contacts page, the Companies menu, a company's panel) and the free follow-up of a Discover run; **Search again** on a company whose website a search found; the check of the websites found before (below); and **Verify with the classifier** on Contacts. A register fact on the pages (the firm's CUI, registration number, an official phone or email) settles a site with no question. A model's yes makes a site **likely** at most, never confirmed. A task set to off asks nothing: the site waits unlinked, and nothing is guessed in its place.

| Task | What it answers | When it is asked | Off |
|---|---|---|---|
| Search results (`results`) | which results of a web search could be the firm's own site, before any visit (one request for all of them) | a lookup by name and town that leaves two or more results | the three best name matches are read |
| Website (`site`) | whether one website read is the firm's own (one request per candidate) | candidates no register fact settles; `harvey contacts rejudge` | the site waits unlinked, unconfirmed |
| Contacts on the site (`contacts`) | which phones, emails and profiles read there are the firm's own; and, about one contact, yes / no / unsure | the chosen site, before linking; verifying contacts | only official contacts are linked on a site the model chose |

**Profiles, and which task uses which.** The classifier is set up as profiles: one complete setup each, its kind (`openai_compatible` for any OpenAI-compatible endpoint at a base URL, `anthropic` for a small Claude model, `brain` for Harvey's own model, `decision` for a decision model in the System One format), its endpoint, model, the name of the `.env` key it uses (never the key: it stays in `.env`), extra request fields, JSON mode, pace, timeout, prices, the tasks it answers, and its questions' texts and bars. The card's **Tasks** part sets each task to a profile, or off (`harvey contacts classifier use TASK PROFILE|off`); the tasks can use different profiles. The **Profiles** part lists them side by side, sorted by name, each with its endpoint, model, key name and whether `.env` holds a value for it, price, extra fields, pace, the tasks it answers and is used for, and what it measured. From there: **Use for…** a task, **Test**, **Edit**, **Duplicate**, and **Hide** (a built-in, with **Show again**) or **Remove** (one of yours). **Add a profile** starts an empty one. Editing a built-in saves a copy of your own, based on it; the built-in stays as shipped. The editor holds every field, the questions per task with their texts (a language model's prompt and its yes and no texts; a decision model's questions, their criteria and bars, and the contacts question's options and bar) and the extra request fields as JSON. Code keeps the structure (the statements' ids, the options, how answers combine): only the texts and bars change, and bars stay inside their ranges. A profile is the only place a setup lives: an older install's single setup (the contact finder's `classify_*` settings) is taken once as a profile of its own, `settings`, set for the tasks it answered, and those settings are never read again; edit the profile from then on.

**Built-in profiles** ship with Harvey (`harvey/reference/classifier_profiles.json`, read-only): only setups that tested well in Harvey's own judge test on 200 hand-labelled Romanian firms (the developers' test, not shipped: 39 cases the finder asks the classifier about, 32 of them scored), each with what it measured, as counts. None is put above another. Sorted by name, as the card shows them:

| Profile | Kind, endpoint, model | Website right of 32 | False links | Missed | Contacts right / picked / expected | Median / p90 | Test cost |
|---|---|---|---|---|---|---|---|
| DeepInfra: Gemma 3 27B | `openai_compatible`, `https://api.deepinfra.com/v1/openai`, `google/gemma-3-27b-it` | 29 | 0 | 3 | 39 / 39 / 40 | 899 / 3,864 ms | $0.0032 |
| DeepInfra: Mistral Small 24B (2501) | same endpoint, `mistralai/Mistral-Small-24B-Instruct-2501` | 29 | 0 | 3 | 37 / 37 / 40 | 1,674 / 3,993 ms | $0.0020 |
| DeepInfra: Mistral Small 3.2 24B | same endpoint, `mistralai/Mistral-Small-3.2-24B-Instruct-2506` | 30 | 0 | 2 | 41 / 41 / 45 | 797 / 2,025 ms | $0.0031 |
| DeepInfra: Qwen3.8 27B, thinking off | same endpoint, `Qwen/Qwen3.8-27B`, extra field `{"reasoning_effort": "none"}` (without it the model returns nothing) | 29 | 0 | 3 | 39 / 39 / 40 | 918 / 2,918 ms | $0.0076 |
| TypeSafe: Jev (decision model) | `decision`, `https://api.typesafe.ai/v1/systemone`, `jev-latest`; site and contacts only | 32 | 0 | 0 | 53 / 54 / 54 | 213 / 528 ms | $0.0026 |
| Your own hardware: Ollama, qwen2.5:3b | `openai_compatible`, `http://127.0.0.1:11434/v1` (in Docker the Ollama service's address), `qwen2.5:3b`, no key | 28 | 0 | 4 | not measured with today's questions | about 18 s on a CPU, under a second on a GPU | free (your hardware) |

The DeepInfra profiles use the key `DEEPINFRA_API_KEY` and DeepInfra's price list; each reply carries its own cost (`usage.estimated_cost`), which Harvey records. Jev uses `TYPESAFE_API_KEY` at the price entered (0.042 USD a million input tokens, output free): TypeSafe has no price list and no billing API, so keep the USD caps.

**DeepInfra key scopes.** These aren't the standard set, so make the key with them: a new DeepInfra key has `inference`, `models:read` and `account:read` already; add **`billing:read`** (the card's **Account** box: this month's usage per model, the key's limit and spend, the balance) and **`metrics`** (usage metrics) by hand when you create it. Harvey needs no other scope: leave `compute:*`, `batch`, `lora`, `voice` and `account_scoped_jwt` off. A key without `billing:read` still answers questions; the Account box says what it can't read. The key is read on the server and never reaches the browser.

**Testing a profile.** **Test** on a profile runs Harvey's judge test over the built-in fictional cases (`reference/classify_cases.jsonl`), or a labelled file on the server (one JSON object a line; `harvey contacts judge-test --file F --profile ID`), for the tasks you tick. It is counted and priced first (questions, tokens, USD at the profile's price), then runs as a job on the classifier's lane under the USD cap you type, and keeps its counts on the profile: right, false links, missed, the contacts right, picked and expected, median and p90 time, cost, date and test set (the last ten runs). A profile of your own says **not tested** until a test run records on it, and again after any change to what it asks (endpoint, model, extra fields, JSON mode, questions).

**Caps, and a budget per run.** The card's **Caps and spend** part holds the install's caps for every classifier call, whatever the profile: calls a day, a USD cap a day and a month (UTC), and each task's own day cap (`POST /api/classify/settings` takes only these; a setup is a profile's); it shows today's and this month's calls and spend, the cost per site checked and tokens a call, per task. Reached, the classifier isn't asked until it renews, and the firms wait unlinked; with a profile's price unknown, its calls can't count in USD (the card says so) and only the call caps hold. Each run can have a classifier cap of its own on top: **Find contacts** shows, next to its search cap, how many questions the run may ask and an estimate of what they may cost, and a **Classifier cap ($)** (blank: none of the run's own; 0: no question, a site no register fact settles waits unlinked); the API and CLI take `classify_max_spend` / `--classify-max-spend` (Find contacts, Discover, `harvey contacts find`).

**Estimates, and what is enforced.** A call is billed by its tokens, and its size depends on what it reads, so no figure before a run is a promise. Every estimate (Find contacts, verifying contacts, a profile's test) is a range per task: *typical* (this month's calls of the task on average, by the tokens the endpoint billed; before there are any, a guess) and *high* (this month's largest call; before there are any, the most a request of the task is built to), each with the most the task may answer, at the profile's price. It counts a website a search found as read again (a site question and a contacts one) and leaves out one still waiting for the check. What is enforced is the cap, call by call: before each call its request is built and its tokens counted, with a 25% margin (Harvey counts about 3.3 characters a token; on the fictional test cases Qwen2.5's tokenizer gave 0.89 to 1.07 times that per task, 1.19 at worst for one phone-heavy request) and the chat template's own; the call is refused when that worst case, with the most it may answer, doesn't fit what is left of the run's cap and of the install's day and month limits (`spend.per_day_usd`, `per_month_usd`, which count the classifier's cost too). What it really cost is recorded after. Refused, the site waits unlinked, and a job stopped by the run's own cap pauses until you raise it (the Queue's classifier cap for that run). The classifier's cost is part of the run's spend but never of its search cap.

**What each run took and cost.** A Find contacts run shows its report where it was started, every few seconds until its jobs are done; the Queue shows it for the run opened there (with its search cap and its classifier cap to raise), and Today lists the newest runs with theirs: the jobs by status, the time from the first start to the last finish and the jobs' own time, then the cost split into paid searches, address checks (each service's checks, in USD: $0 inside its free allowance, its price past it, and how many checks were refused), Hunter's pattern lookups, the classifier's calls, tokens and USD (and how many calls had no price), anything else, and the total. Each job row shows its own classifier calls and time. A finished run's report is stored with the run, and made again when one of its jobs is retried or resumed. `harvey jobs report RUN`, and the end of `harvey contacts find` and `rejudge`, print the same; the API is `GET /api/jobs/batches/{id}/report`.

**Verifying contacts.** On **Contacts**, tick any contacts (phones, emails, forms, profiles) and choose **Actions → Verify with the classifier**, or open one contact's **Details** and verify it alone (`harvey contacts verify --contact ID`, or the filters). Each link (a contact at a company) is asked of the contacts task's profile, with the firm's facts and where the contact was found (the company's site read again, free, when the contact is on it). It is counted and priced first, then runs under the cap you type, one job per company. Each answer lands on its row: **yes** is noted and changes nothing else (a model never confirms anything); **no** drops what a website's read gave the link to unconfirmed, while official sources and contacts in a campaign are never lowered, and the run's **undo** (on the row, or `--undo BATCH`) puts it back; **unsure** changes nothing and waits for your review: the Contacts filter **Classifier's verdict → unsure** lists them (`--review`). Nothing is deleted.

**Requests and costs, in detail.** A language model answers each statement `yes` or `no` in JSON. A decision model isn't a chat model: each request (POSTed to the base URL exactly as given, the key as a bearer token) is a state and questions it answers with probabilities, so it gets questions of its own and code combines them. A site is the firm's when the model says it names the firm and presents the firm's own business, each at least its bar (0.5 and 0.4), and neither a directory nor an institution (under 0.7), the bars as measured; each contact on the chosen site is a choice of whose it is, kept when the firm's own reaches the profile's contacts bar (0.7 as measured). The company panel shows the model's probability. Every call is in the usage ledger (model, tokens, time, cost): the reply's own cost when it says one, else its tokens times the price entered (USD a million input and output tokens), else, for `openai_compatible` on a DeepInfra base URL, the model's price on DeepInfra's public price list (read once a day; the editor can list DeepInfra's models with their prices to fill the model field); a decision model has only the price entered. With none of those the call is recorded at $0, unpriced. A website check asks each candidate alone, so three candidates are three calls, and one more for the chosen site's contacts. The prompts are short: text on a page is data, and an answer counts only for the statements asked. The key is the provider's own, named `*_API_KEY` in `.env` (the built-ins use `DEEPINFRA_API_KEY` and `TYPESAFE_API_KEY`), never one of Harvey's own keys; the base URL is http(s). Each profile has its own pace, timeout and pause: a 429 or an unknown model pauses that profile only, so a hosted model's rate limit never stops a local one; a refused key, a payment needed or a spent quota pauses every profile on the same account (the same endpoint host and key name). The card and `harvey contacts classifier` say which profile is paused, why and for how long. The SDKs (`openai`, `anthropic`) come with `pip install -e .`; under Docker, rebuild the image once (`docker compose build harvey`), or the card says the library is missing.

**Checking the websites found before.** Sites a search found before this check existed (or while no classifier was set) wait for it: Today says how many, and **Check them** on the Classifier card (or `harvey contacts rejudge --run`) takes each once, one firm per job on a lane of its own (Website checks), so a Find contacts run never waits behind it. Its stored address first (a directory or a profile page is ruled out with no visit), else its pages read again and decided like a new lookup, with every candidate the lookup left in question when the site was waiting for the classifier. While the classifier is held or its day's calls are used, nothing is read and the lane rests until then. A site that doesn't answer is taken off only after three different days of trying. One the classifier says yes to with another candidate too stays as it is, nothing linked, and isn't asked again by the same classifier (provider, model and threshold). One taken gets its trust (likely or confirmed), why, and the contacts the classifier said yes to raised to match; nothing on a site it takes is removed or made less sure. One that isn't the firm's is taken off with exactly what its read left (its pages' contacts, addresses at its domain, the people it named, their keys, contacts left with nothing), and the searches that relied on it stop counting as done (they stay on file with their cost): the lookup that picked it, so the firm is looked up again, its reads, so a website it declares later is read, and the address checks of the people left, so they are searched at the next site; official contacts, the do-not-contact list and campaign members are never touched, and `harvey contacts rejudge --undo BATCH` puts back what a check took off. It counts first (`--dry-run`, the default) and only runs when you start it.

```bash
harvey contacts status                         # the backlog, and the found websites waiting for a check
harvey contacts rejudge                        # count what the check would do (nothing changes)
harvey contacts rejudge --run                  # check them; --undo BATCH puts back what it took off
harvey contacts judge-test                     # the classifier over the built-in labelled set
harvey contacts find --county Brașov           # companies in Brașov with no contact: websites only (free)
harvey contacts find --website no --max-spend 2.00   # look up companies with no website, up to $2
harvey contacts find --force --limit 50        # search again, even what was searched before
```

**From contacts to the Email module.** The finder knows nothing about email campaigns. A campaign takes its members from an audience: a Contacts filter you saved (Contacts → Actions → *Salvează ca public*, or `harvey audiences save`), read by the same code as the page, so the campaign gets exactly what the page showed. Ticked email rows can be added to a campaign by hand (*Adaugă în campanie…*). Whichever way an address comes in (the start, a roll, by hand) it goes through the same rules: verified, catch-all (with `send_to_risky`) or not yet checked; linked to a live firm with trust confirmed or likely; not on the do-not-contact list; not in another active campaign's sequence, nor sent cold email in the last 90 days (an address marked contacted with no cold email on file, as an earlier release's Instantly campaigns or a hand send leave it, counts as recent); nobody there already answered. The preview counts what each rule left out, and the campaign keeps every address it decided with the rule that left it out. A rolling campaign's hourly roll reads only the contacts that changed since its last one (a status, a do-not-contact flag, a link, a merge) and the addresses that came due again (the campaign that held one ended, the cold email it got turned 90 days old, a do-not-contact entry was undone), and its whole filter once a week. Catch-all addresses go by the campaign's own `send_to_risky`, chosen while it is a draft (the setting's by default) and never past the setting. Each cycle writes at most 8 first emails across all campaigns, and only while the first emails waiting to go are fewer than two days of today's cold cap, so the model isn't called for drafts that would sit in the queue. An address nobody checked is checked before anything is written for it (a credit from the same monthly checks; a rolling campaign's new ones at most 50 a day), and one that comes back a guess or invalid is skipped. What the company's record says (its town and activity, and the latest reading of the signals the site read and other sources collected: tools on the site, open roles, ads, employees) goes into the address's card, and the Writer reads the same facts from the record when it writes its first email.

```bash
harvey audiences save "Stomatologi BV" --county BV --q stomatologie
harvey audiences show 1                         # what a campaign would take, counted
harvey campaigns new --name "Stomatologi BV" --audience 1 --note-file nota.txt --sequence-file secventa.json
harvey campaigns start <id>                     # needs the note (80+ characters), a sequence and harvey ack
harvey campaigns members <id> --status replied
harvey campaigns export <id> --format csv       # its record: note, acknowledgement, members, sends, opt-outs
```

**The record (evidență).** Each campaign keeps your record of the balancing test and of what it did: its legitimate-interest note (edited in the campaign's panel while it is a draft, then kept as it started), the acknowledgement it started under (version and time), every address with how it joined (start, roll, by hand) and why it left (replied, opted out, rejected by you, bounced, cut off mid-send, send failed...), every email sent (the answers in its conversations too), and the opt-outs that reach its addresses (their own, their domain's or their firm's). The campaign's panel has **Evidența (CSV)** and **(JSON)**; `harvey campaigns export ID` writes the same. A first email you reject ends that address's place in the campaign (its follow-ups would never follow it), and so does one whose send was cut off mid-way or failed (it may have arrived, and is never retried), so a campaign that doesn't roll still finishes.

Staged email always waits in the Outbox; on autopilot the drain that sends an email approves it then, after the autopilot review, so a pause never turns into a burst nobody looked at.

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
a company's panel. While the module is on, `harvey registry pull` and
`harvey import-onrc` check each saved firm at ANAF in the background while the
rest is saved, then wait for the last batch, with progress. With the module
off, ANAF is never asked and no phone is filled from it.

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
are free, known from the source itself, and read while their modules are on;
the Companies and Contacts filters use them: turnover and employee
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

**`harvey.local.yaml`** — who Harvey is and who it sells to (a copy of the `harvey.yaml` template, see below):

```yaml
persona:      { name, company, role, email, tone }
product:      { name, description, pricing, key_benefits, objection_responses, offer }
icp:          { industries, titles, company_size, geography, hiring_signals, geo_coordinates }
channels:
  email:      { provider: gmail | microsoft | smtp, max_daily_sends, require_approval,
                send_to_risky, max_bounce_rate, warmup, send_window }
usage:        { max_daily_claude_percent, heartbeat_interval_minutes, quiet_hours }
compliance:   { postal_address, opt_out_line, do_not_contact, privacy_url, ai_disclosure_line,
                precleared }
brain:        { provider: claude | codex, model, reasoning_effort, auth,
                usd_per_day, usd_per_month, codex_usd_per_mtok_in, codex_usd_per_mtok_out }   # optional; claude by default
writer:       { humanizer }   # optional; true adds one editing pass per batch using ~/.claude/skills/humanizer/SKILL.md
```

`max_daily_claude_percent` caps whichever subscription `brain.provider` uses (the name predates Codex support).

`brain.auth` is which login the CLI uses: `terminal` (the default, and what an install without the key does) is the one you made with `claude login` or `codex login`, in the CLI's own folder; `account` is a login kept in a folder of Harvey's own, `data/auth/claude` or `data/auth/codex` (made readable by you only): for Codex, **Link account** on Settings → Model fills it with your ChatGPT login and Harvey keeps it renewed (see [Using a ChatGPT plan instead](#using-a-chatgpt-plan-instead-codex)); for Claude, **Link account** (your subscription) or **Link a Console account** runs the Claude CLI's own `claude auth login` in that folder and relays it: the panel shows the CLI's sign-in page as a link, you sign in there, and you paste the code the page gives you into the panel, which hands it to the CLI's prompt (Harvey never logs or keeps the code, and never reads or keeps the login: the CLI keeps it in its own files there and refreshes it itself). If the CLI says the code is incomplete the panel asks again; a code it refuses, or 15 minutes with none, ends the link and leaves the account already there as it was. When `claude auth status` says the linked account is logged out (signed out or revoked), the panel and Today say "link the account again". **Unlink account** is the CLI's own `claude auth logout`, then the folder is removed. A Console account is billed per token, so it is held by the same USD caps as an API key (below), and the panel won't use it without one. `api_key` bills per token: save the key on **Settings → Model** (API key), which checks it with a free call (the provider's list of models, no model call) before keeping it. Claude's goes in `.env` as `HARVEY_BRAIN_ANTHROPIC_KEY`, a name only Harvey reads, and reaches the Claude CLI as `ANTHROPIC_API_KEY` only in this mode; Codex's is handed to Codex's own login in `data/auth/codex`, where Codex keeps it in its own file (Harvey keeps no copy). The key is never shown again; **Unlink** removes it and goes back to the terminal login. `usd_per_day` and `usd_per_month` (UTC, blank for none) cap what Harvey's own calls cost, checked before each one: reached, no call is made until the day or month turns. At least one of them is required: the panel won't switch to API key without a cap, and a hand-edited config with none makes no call. Claude reports each call's cost; Codex doesn't, so its tokens are priced from `codex_usd_per_mtok_in` / `codex_usd_per_mtok_out` (USD per million; cached input counts as input), and with no price no Codex call is made. The caps are a hard stop, not only a check before a call: while a call runs Harvey counts what it costs from its stream (Claude's tokens at its model's prices, from `harvey/reference/model_prices.json`, dated and sourced, with an unknown model priced at the dearest; Codex's at your prices), and stops it, the way Stop does, when it reaches what was left of the day's or month's cap; what it wrote is kept and what it cost is counted. Under an approved plan, an agent wake or task holds its worst case of the plan's budget (and of `spend.per_day_usd` / `per_month_usd`) before it starts, priced the same way, and is stopped at that hold (or at what is left of the day's or month's cap, if less), saying so in its chat; runs side by side in one Harvey (chats, wakes, tasks, brain calls) share what is left of the caps rather than each spending all of it; its cost counts there after. A plan with a $0 budget gets no wakes or tasks on an API key: its card says so, and so does Today, and the card shows the agent's own model spend on a line of its own. A call whose cost isn't reported (it timed out or was cut off) counts as a run's worst case for the turns it was in, never as $0. `harvey train --provider` and `harvey setup` keep the API key mode when you switch provider only if a key is saved for the new one (else they say so and use the terminal login), and keep the caps and Codex's prices. Whatever the mode, an `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in `.env` or your environment never reaches the Claude CLI Harvey starts, so it can't move your calls to per-token billing without you knowing, and neither do the settings that send calls to another provider (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` and the like); Codex's child never gets `OPENAI_API_KEY` or `CODEX_API_KEY` (in API-key mode the key travels in Codex's file only).

If `harvey.local.yaml` exists it wins. It's gitignored, so a fork can carry real product configuration while the tracked `harvey.yaml` stays a template — nobody publishes their positioning and pricing by accident. Harvey never writes `harvey.yaml`: training, `harvey setup` and the dashboard's settings all save to `harvey.local.yaml`, and the first of them makes it as a copy of the config in use (comments included; setup and the dashboard then change only the settings they save). A fresh clone reads `harvey.yaml` until then. To change a setting by hand, edit `harvey.local.yaml`; if you don't have one yet, copy `harvey.yaml` to it first. It is read instead of `harvey.yaml`, not on top of it, so it must hold every setting, not just the ones you change.

**Updating.** `git pull` updates `harvey.yaml` and leaves `harvey.local.yaml` alone. An older Harvey's setup or dashboard wrote `harvey.yaml` itself; if `git pull` refuses because of local changes to `harvey.yaml`, let Harvey save any setting once (or copy `harvey.yaml` to `harvey.local.yaml` yourself), check that `harvey.local.yaml` has your settings, then run `git checkout -- harvey.yaml` and pull again.

### Commands

```bash
harvey run                   # the heartbeat loop (the Email module's: reply, write, send)
harvey dashboard             # web UI at 127.0.0.1:5555 (--url: print the unlock link); runs the job worker
harvey worker                # run queued jobs without the dashboard (one worker per database)
harvey agent chats           # your chats with Harvey's agent, newest first; --q searches the titles
harvey agent chat            # a new chat in the terminal (Ctrl-C stops an answer); --chat ID goes on with one
harvey agent ask "..."       # one question in a chat of its own, the answer streamed as it is written
harvey agent rename ID TITLE # rename a chat; harvey agent delete ID deletes one (asks first; --yes)
harvey agent log             # the messages; --chat ID: one chat's; --calls: every tool call of each answer
harvey agent plans           # the plans the agent proposed (--chat ID); plans show|approve|reject|cancel|close ID (show lists its campaigns)
harvey agent memory          # the agent's notes; memory add TEXT|edit ID TEXT|forget ID|playbooks|playbook NAME|delete-playbook NAME
harvey jobs                  # the queue per lane; cancel|retry|resume ID, pause|resume-lane LANE
                             # (resume ID --max-spend N raises a paused run's cap)
harvey signals               # the signals collected, each one's cost and counts (read only)
harvey discover              # find businesses; --providers / --estimate; --provider a,b runs several in one batch
harvey profile               # read discovered companies' sites (free); one job per company
harvey contacts [find|status|rejudge|judge-test|verify|classifier]  # the contact finder: find (filters, --max-spend, --classify-max-spend, --force, --background), the backlog, checking found websites (--run, --undo BATCH), the classifier's test (--file, --all, --profile), verifying contacts (--contact ID, --max-spend, --review, --undo BATCH), its profiles (classifier use TASK PROFILE)
                             # (these and the loads and checks below wait for their jobs;
                             #  --background queues them and returns)
harvey registry load         # the whole trade registry into data/registry.db, once per dataset
harvey registry search dentist BV  # every firm in Romania: words, CAEN code, county, town
                             # (--min-turnover 1M, --max-employees 49 ... once statements are loaded;
                             #  --employer yes, --vat yes, --micro no once the taxpayer file is)
harvey financials load       # the Ministry of Finance's yearly statements into data/financials.db
harvey financials status     # what is loaded; check: is a new or revised year out
harvey taxpayers load        # the Ministry of Finance's taxpayer file into data/taxpayers.db
harvey boundaries load       # OpenStreetMap's administrative boundaries into data/boundaries.db (module boundaries on)
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
harvey modules settings <id> # its settings; set <id> KEY VALUE changes one (a verifier's price: usd_<service>)
harvey caps list             # every module's capabilities: inputs, outputs, cost, on/off (--json)
harvey duplicates            # possible duplicates, as groups, and merges;
                             # --merge KEEP A B..., --separate A B... [--from C D...],
                             # --undo MERGED, --undo-group MERGED, --scan
harvey upgrade               # upgrade an old harvey.db to the slim database (--check reports only,
                             # --delete-old deletes the old files it kept)
harvey doctor --recount      # every kept count against a full recount (--fix writes them again)
harvey train <url>           # learn a product from its website (--provider codex [--model ID])
harvey status                # pipeline summary
harvey usage                 # active provider's quota + per-agent tokens
harvey outbox                # review queued email
harvey export                # deliverable prospects → CSV
harvey sending pause|resume  # kill switch
harvey sending accept-gap    # let cold email go after a read that couldn't reach back
harvey warmup [domain]       # is the sending domain ready? (SPF/DKIM/DMARC, age, blocklists, warmup)
harvey trulyinbox connect    # connect TrulyInbox so Harvey reads the mailbox's warmup
harvey campaigns export ID   # a campaign's record (CSV or --format json)
harvey gmail auth            # one-time Gmail OAuth
harvey microsoft auth|test  # one-time Microsoft 365 sign-in (docs/microsoft365.md)
```

Prospect CSV exports exclude opted-out contacts, including when using
`--status` or `--email-status` filters. `harvey export --all` is an unfiltered
raw-data export for inspection: it includes unverified contacts and should not
be imported as an outreach list. Even so, it leaves out every address the
do-not-contact list bars. The dashboard's prospect CSV
download follows the same policy; `?all=true` selects its raw export.


### Modules

Everything beyond the core is a module with a `module.toml` manifest under
`harvey/modules/<set>/<id>/`: the core set (website reader, people, phones,
export, geocoding, English), the original set (US market, the discovery
sources, web-marketing, startup and hiring signals, email verification,
Hunter, the email loop) and the Romanian set (market, language,
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
website-marketing signals and email campaigns
start off. Turning a module on never turns on another: one whose `requires`
are off is refused, naming them, and one that an enabled module requires
can't be turned off. Off means hidden, never deleted: its signals leave
the signal pickers and collection (their observations stay), its
discovery sources and presets are refused with an error that names the
module, the contact finder stops using its searches, Hunter and the verifiers
aren't called, and Settings stops offering
its `.env` keys that are empty (one that holds a value stays editable). The signal vocabulary itself comes from the manifests of the modules
that are on. On an install that predates modules, a module that ships off
stays on if one of its `.env` keys is set (a search or enrichment provider
that was set up), if you had confirmed one of its signals, or if the install
has data from it (tools detected on sites, open roles or job-board
contacts). The Email
switch in Settings is the email campaigns module. Off, the dashboard hides
its tabs and `harvey run` writes, sends and reads no email at all; it only
looks for companies and contacts. On, sending works as your config says.

What a module can do is a list of capabilities in its manifest: a search
over an area, a lookup by name and town, a read of a company's site. Each
says what it takes, what it gives (records, identifiers, signals), what it
costs and whether its module is on: `harvey caps list` (`--json`) prints
them all. A capability returns what it found and writes nothing; one core
step saves it, so every module goes through the same rules: the same
company found twice is one company, only signals of modules that are on
are recorded (and only ones the capability declares), a module that is off does
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

Or skip the terminal: on **Settings → Model**, choose Codex, **Account linked in Harvey** and **Link account** (below). The Docker image ships Codex 0.159.3, so this works in Docker too.

Then choose **Codex** on the dashboard's **Settings → Model** (its model list is your account's own, read live), pick **codex** in `harvey setup` (it lists the models your account offers that Harvey can run, and asks), run `harvey train <url> --provider codex` (it asks the same way, or takes `--model`), or add to your config:

```yaml
brain:
  provider: codex
  model: <a model id>    # one your account's Codex catalog lists (Settings → Model shows them)
  reasoning_effort: ""   # optional: minimal | low | medium | high | xhigh
```

What to know:

- **It's one or the other.** Harvey uses exactly the provider you configure. If Codex fails, the call fails with a clear log message; it never falls back to Claude (or to an API key). A Codex-only machine doesn't need Claude installed.
- **Your Codex setup is left alone.** Each call runs throwaway `codex` processes in a private temporary home and empty workspace, so your `~/.codex/config.toml`, MCP servers, plugins, hooks, skills, `AGENTS.md` and memories are never loaded, and Harvey's model choice never changes your own Codex default.
- **No tools, verified before every prompt.** Prompts contain untrusted text (prospect replies, scraped pages), so the model must have nothing to act with. Harvey disables every tool-providing Codex feature, web search, sub-agents, hooks, skills and project instructions, starts the thread with no execution environment, and then checks Codex's own reports (effective config, account type, thread settings, MCP status, hooks) before sending anything. Anything it can't confirm stops the call. During the turn, any attempted tool call, approval request or non-text item kills the runtime. A read-only sandbox is also set, as extra protection only.
- **Your login only goes to OpenAI.** Before your login is put into the runtime at all, Harvey starts Codex without it and checks that requests would go to the built-in OpenAI provider and the default ChatGPT endpoint. Any custom provider, overridden built-in provider, base URL, telemetry exporter or other endpoint in any Codex config layer (for example `/etc/codex/config.toml` or a managed config) stops the call there.
- **Linking a ChatGPT account in Harvey.** **Link account** on Settings → Model starts a ChatGPT device-code login in a Codex app-server of Harvey's own (the same locked-down config every call runs under, no tools, no model call). The panel shows a page on `auth.openai.com` and a one-time code: open the page, sign in with ChatGPT and type the code there. Device-code login must be allowed first, in ChatGPT's security settings (Settings → Security; in a workspace, its admin may have to allow it). The code lasts 15 minutes; **Cancel** drops it. Once you have typed it, Codex writes the login to its own file in `data/auth/codex` (readable by you only; on the data volume in Docker) and the brain is set to `brain.auth: account`. One link at a time. Harvey renews that login itself: about once a day (sooner if it would run out within six hours), it has Codex refresh it in that folder (`account/read` with a refresh); a failed renewal is tried again within the hour. If it can't be renewed and runs out, Codex calls stop and the panel and Today say "link the account again". Each call still gets a copy without the refresh token, as below. **Unlink account** runs Codex's own logout there and puts the brain back on the terminal login. Your own `~/.codex` is never touched in this mode.
- **Login handling (terminal login).** Harvey supports Codex's file credential storage (`~/.codex/auth.json`, or `$CODEX_HOME/auth.json`, mode `600`): the default unless you've configured an OS keyring. Keyring storage isn't supported. On every call Harvey reads that file (read-only, never writing or moving it) and gives the runtime a private copy *without the refresh token*, deleted after the call. Codex therefore can't change or renew your real login. When the login has expired, or is due for Codex's routine renewal (after about 8 days), Codex calls stop with "Your Codex login has expired or is due for renewal". Use Codex once yourself (it renews the login automatically) or run `codex login`, and Harvey picks up the renewed login on its next call. Harvey never renews your terminal login (only the account linked in Harvey, above).
- **Models.** Set `brain.model` to an exact model ID from your account's catalog. Which ones there are depends on your ChatGPT account and changes over time, so Harvey suggests none by name: Settings → Model, `harvey setup` and `harvey train --provider codex` list the ones your account offers (the app-server's `model/list`) that pass Harvey's text-only check below. Harvey creates a private catalog copy that preserves model capabilities while selecting direct tool mode and removing experimental tools. The model receives no tools under Harvey's lockdown. A blank `model` selects the highest-priority listed model that can use this configuration; hidden models require an explicit ID. An explicit model is used exactly or the call fails. Unknown model IDs and unrecognized tool modes are rejected. Availability depends on your ChatGPT account.
- **Quota.** The budget gate and dashboard read your ChatGPT plan's real rate-limit windows from Codex, for the limit bucket that applies to your model, and compare them to `usage.max_daily_claude_percent`. If Codex reports that usage is blocked, or a call fails on the plan's usage limit, Harvey treats the plan as full (even when no window is reported) and stops Codex calls for an hour. If the limits can't be read, the dashboard and `harvey usage` say "Quota unavailable" and the budget falls back to Harvey's own daily call count. Usage is recorded per provider; Codex calls are logged with tokens but no dollar cost, because the plan isn't billed per token.
- **When Codex can't be used, Harvey pauses rather than guessing.** Before each heartbeat, Harvey checks everything a call needs except the prompt itself: the Codex CLI version, the isolation, your login (expired or due for renewal), your ChatGPT account, and that your configured model can run with no tools. No prompt is sent, so nothing is used from your plan beyond a quick account check. If something is wrong, the whole cycle is skipped: no reply is read or marked handled, nothing is drafted or sent. The log line, `harvey status`, and the dashboard's Actions card say "Codex paused" and why. If Codex stops working part-way through a cycle (for example the plan hits its usage limit), the reply being handled is left exactly as it was for a clean retry, not escalated, half-recorded or lost, and that cycle sends nothing. Fix the cause (usually `codex login`), and Harvey resumes on its next heartbeat: a renewed login or reinstalled CLI is noticed immediately, a usage-limit pause ends after an hour, and other fixes are picked up within 30 minutes. `harvey status --check` runs the same check on demand and exits non-zero if it fails. Brief failures (a crash or timeout) just skip one cycle. Harvey never falls back to Claude or an API key.
- **Requirements.** **macOS or Linux**, Codex CLI **0.159.3** exactly, and Node.js for its npm package. **Windows isn't supported for Codex:** Harvey refuses it there before reading your login; use the Claude provider on Windows. Harvey checks the Codex version before every call and refuses any other version, including newer ones, because a different Codex can add tools Harvey's lockdown doesn't know about. Support for a new version comes with a Harvey update. Live use with a real ChatGPT account has been verified on macOS and ARM64 Linux. The offline suites also passed on x86-64 Linux under CPU emulation; CI covers Linux and macOS. The Docker image ships Codex 0.159.3 too (the build fails on any other version); in Docker, link the account from Settings → Model, since the container has no `~/.codex`. To re-verify the lockdown offline against your installed CLI, with no credentials or network: `HARVEY_CODEX_CLI_TESTS=1 pytest tests/test_codex_cli_surface.py`.
- **Harvey's agent on Codex.** With `brain.provider: codex` the agent (the Chat tab, `harvey agent chat`, its wakes and tasks) runs on Codex as well, in the same private runtime with the same checks, with one difference: Harvey's own tool server (`python -m harvey.agent.tools`, the one Claude gets) is configured in that runtime, and Codex must report it as the only MCP server, with its tools loaded, before anything is sent. The thread still has no shell, file editing, web search or any other tool: the model is offered Harvey's tools directly, plus Codex's own three readers of an MCP server's resources (Harvey's server has none). During an answer, a command, a file change, another server's tool, an approval or input request, or a raw call to anything but Harvey's tools ends the answer and the runtime, and says so. Harvey counts the answer's turns itself (Stop and the turn limit interrupt the turn), streams the text as it is written, records the tokens under provider Codex, and holds it to the same plans, budgets and `usage.max_daily_claude_percent` share of your ChatGPT plan. Starting a chat's runtime takes a few seconds (the checks above, then Harvey's tool server). A chat's runtime holds the same copy of your login with no refresh token, so Harvey starts a new one (given the chat so far) before that copy could run out during an answer, and after Codex refuses it. This was checked against the real Codex 0.159.3 with no credentials or network, a local stand-in for the model recording what it is offered: Harvey's tools only, and one of them called through Harvey's server with no approval asked (`HARVEY_CODEX_CLI_TESTS=1 pytest tests/test_codex_cli_surface.py`). A live answer from a real model on a ChatGPT account hasn't been verified yet.
- **Overhead.** Each call checks the CLI version, starts Codex once without your login to verify its configuration, reads the model catalog, then starts it again to run the prompt; each heartbeat's check does the same without the prompt. That adds a few seconds per call, and one Brain's Codex calls run one at a time.

---

## Legal and deliverability

Harvey automates outreach, but **you are the sender.** Cold email is legal in most places when done right and expensive when done wrong — CAN-SPAM penalties run to $53,088 per email. Harvey ships with compliant defaults. Keep them.

**CAN-SPAM (US).** Truthful subject line and accurate from-address — Harvey's copywriting rules forbid fake "re:" threads and impersonation. A working opt-out, honored fast: every cold email ends with an opt-out line (`compliance.opt_out_line`, default *Nu vă interesează? Răspundeți „stop" și nu vă mai scriu.*), and Harvey treats any opt-out wording as immediate and permanent, wherever in the reply the person wrote it (including a bare "STOP" or "No thanks"). Nothing Harvey does later moves someone off *opted out*: a later message from them is kept for you to read, never answered, and a complaint still goes to you. To see why someone was opted out (the rule that fired, the words, and their message), click **Why?** on their row in Contacts. If it was a mistake, **Undo opt-out** there marks them as replied: Harvey sends nothing on its own, and their next reply gets a draft for your approval. Your physical mailing address in the footer: set `compliance.postal_address` in `harvey.local.yaml` before your first campaign. Harvey adds both lines under every cold email when it sends (replies don't get them), shows them under each draft in the Outbox, and warns on `harvey run`, `harvey status` and the Outbox tab until the address is set. Text in a reply that repeats what Harvey wrote for that person (its emails, campaign templates, and every footer it has used) is set aside as quoted, not read as their words, so quoting Harvey's own footer isn't an opt-out; the model also reads replies without the quoted email. The opt-out line must ask for a reply Harvey recognises (`Răspundeți „stop"`, `Reply "no thanks"`, `Reply STOP`, also "dezabonare", "nu mai doresc", "unsubscribe", "remove me", "opt out") and can't contain a link or email address. Romanian opt-outs ("nu mă mai contactați", "nu mai scrieți", "dezabonare", with or without diacritics) are honoured by these keyword rules, never left to the model.

**Do not contact.** List competitors, existing customers, or anyone else Harvey must never email under `compliance.do_not_contact`, one address or domain per line, in `harvey.local.yaml` (copy `harvey.yaml` to it first if you don't have one yet). A domain covers its subdomains (`acme.example` also blocks `mail.acme.example`, not `notacme.example`), an address covers its plus-tags (`jane@` also blocks `jane+news@`), and a pasted link such as `https://www.acme.example/about` or `Jane Doe <jane@acme.example>` counts as what it names. An entry that is neither an address nor a domain stops Harvey from starting, with the entry named, rather than quietly protecting nobody. A running Harvey picks up an edit at once, and the send gate checks the list as each email leaves, cold or reply, so it holds even for an email you already approved. If someone on the list writes back, Harvey doesn't draft an answer: the conversation comes to you. `harvey export` and the dashboard's Export buttons leave them out too.

**The do-not-contact list.** Everyone who asked not to be contacted, and anything you add by hand, is on one list in `harvey.db` (`harvey suppress list`, `--csv` to keep it beside an export; `harvey suppress add ADDRESS|DOMAIN`, `--firm ID` for a whole firm; `harvey suppress undo ID`). An entry names an address, a domain (its subdomains too; a free-mail domain such as gmail.com is refused, it would bar everyone there), or a firm (by its tax code when it has one, so it holds after the firm is removed and pulled again), never a contact row, so it holds through merges and their undo, and an address found again comes back barred. The send gate, the Writer, the audience, the exports and the reply handler all check it, next to `compliance.do_not_contact`. Undoing an entry keeps its history; undoing one the person asked for (an unsubscribe, an opt-out, an objection) asks you to type `confirm` and say why. The one-click "Undo opt-out" on a contact lifts only an opt-out Harvey read in their reply's words (a keyword or the model can be wrong); an unsubscribe, an objection or a complaint is undone on the list. A firm entry for a firm with no tax code bars its own domain too (never a free-mail one), so it holds after the firm is removed and pulled again. No export ever includes a barred address, `--all` included; an export is what was true when it was made, so send the list with it.

**Unsubscribe.** Every cold email carries a `List-Unsubscribe: <mailto:…?subject=dezabonare%20TOKEN>` header (Gmail, Microsoft 365 and SMTP), so mail clients show their own unsubscribe button. Pressing it sends a short message to your sending mailbox, which Harvey already reads: the token names the email, and that address (and the sender, when it is another one) goes on the list at once, with no reply sent. It works with an empty body, and Harvey also looks in the spam/junk folder for these messages (for the token only). There is no one-click (`List-Unsubscribe-Post`) header: that needs a public HTTPS address, and Harvey runs on your machine. On Microsoft 365 an email with this header is created from its MIME text, because Graph's JSON send takes only `x-` headers; that Exchange keeps the header exactly as written hasn't been checked against a live mailbox yet.

**Reading every reply.** Each read goes back at least to the last complete one: if Harvey was off for longer than the usual week, the next read reaches back that far (IMAP reads by date without marking anything read, so a message you opened first is still read). A mailbox that got more mail since then than one read takes (5,000 IMAP messages, 40 pages on Gmail or Microsoft 365) is read newest first. If a read can't cover the gap, cold email waits, with a card on Today, until one does or you accept the gap (`harvey sending accept-gap`, typing `accept`: an unsubscribe sent in that time may stay unread); replies still go out. A message from someone Harvey has no card for is marked read and never holds sending; if it answers one of Harvey's emails, or comes from a firm Harvey wrote to, it comes to you as a conversation, that firm's cold email stops, and the sender goes on the list when their own words ask to stop (a stranger's newsletter with a "dezabonare" link is left alone). Cold email also waits when the address it goes out from isn't known (no `persona.email`, and Gmail or Microsoft 365 couldn't say), since its unsubscribe header names it.

**Before the first cold email: the acknowledgement.** Cold email waits until you have read what sending it to firms means (Law 506/2004, GDPR legitimate interest, the information a person is owed and their right to object) and confirmed it once: Settings → Sending → *Citește și confirmă* (a dialog with the text; you type `accept`), the same dialog when you start your first campaign, or `harvey ack` (the text is versioned: a new version asks again). Today shows a card while cold email waits for it. It is not legal advice, and it says what Harvey's source line doesn't cover.

**Personal addresses.** An address that belongs to a named person, is on a free-mail domain, or belongs to a natural-person business (PFA, II, IF, from the trade registry's legal form, else ANAF's) gets a fixed line in its first email saying where the address was found (for example *Am găsit adresa pe site-ul dumneavoastră (exemplu.ro).*), added when the email is staged so the draft you approve is what goes. It is decided over every firm the address is linked to. The send gate refuses a first email to a personal address without it; one staged before the address was known to be personal gets the line and waits for your approval again. The line names the source only; GDPR art. 14 also asks for who you are, the purpose, the legal basis and the right to object. Set `compliance.privacy_url` to a page that says it, and the line gets *Cine suntem și cum vă puteți opune: https://…* after it, for personal addresses only. It is a link, so that first email carries no other one (the gate allows one): one that has another (a booking link in the first step, say) waits for you in the Outbox from staging, marked, until you take the other link out.

**GDPR / PECR (EU & UK).** B2B cold email needs a defensible legitimate interest: the pitch must be genuinely relevant to that person's role, you must know where the data came from, and objecting must be effortless. If you can't say why a specific person would care, Harvey shouldn't email them — and its qualification rules say so.

**Per-recipient check (optional).** Those rules turn on facts about each recipient: a UK sole trader needs consent where a limited company doesn't, Germany wants prior consent even for business email, and Canada's CASL depends on whether the address was published. With `compliance.precleared.enabled: true`, Harvey asks [Precleared](https://precleared.dev) about each new recipient before the first cold email. Anything other than `ok_to_email` holds that email back, with the rule and the next step shown in the Outbox, and the signed receipt id is logged with every send. One check per address, cached for 30 days (changing the sender country, address source, product description or the country sent with it triggers a fresh check); replies are never checked; native providers (Gmail, Microsoft 365, SMTP) only. Harvey tells Precleared the recipient's country when it is sure of it from the company's location (a country, a US state, a Canadian province or an Australian state named there, as for the send window), and Precleared takes that as settled; otherwise Precleared works the country out from the address, company records or the company's website. If all your prospects are in one country, also set `prospect_country` (for example `US`), which Precleared uses only when none of those show it; without it, the one country every `icp.geography` entry is in serves as that default. Precleared receives the recipient's address, your sender country, the address source, your product description and the recipient's country when Harvey is sure of it, else your prospects' country if there is one. It also receives the company's name and the person's name when Harvey has them, which it uses to find a UK company on the Companies House register when the domain alone doesn't (it doesn't store the person's name). Without a key it allows 10 checks a day, and a free key (`PRECLEARED_API_KEY`) allows 25. Put the setting in `harvey.local.yaml` (`harvey setup` asks and saves it there). [docs/precleared.md](docs/precleared.md) has the details, verdict by verdict.

**If the check is held up.** When Precleared can't answer (the day's free checks are used up, the key is out of checks, was rejected or is being rate limited, or Precleared can't be reached), cold email to new addresses waits and replies still go out; email to people Precleared has already cleared keeps going. If Precleared can't check one address, that email is tried again six hours later and the rest carry on, unless that happens at two different domains with no answer in between, which holds new addresses as above. The Today page and `harvey status` say how many cold emails are waiting on the check and what fixes it.

**Bot disclosure.** Some jurisdictions require disclosing automation. Harvey is instructed to answer truthfully, always, if a prospect asks whether they're talking to an AI. Never configure it otherwise. An answer autopilot sends with nobody reading it first also carries `compliance.ai_disclosure_line` under it (*Acest răspuns a fost scris cu ajutorul unui asistent AI.* by default; blank turns it off). An answer you approved, and cold email, go as they are.

**Deliverability — warm up or burn out.** Sending cold email from your main domain, or at volume on day one, lands you in spam permanently.

1. Buy a **dedicated sending domain** (`getacme.example`, not `acme.example`).
2. Set up **SPF, DKIM and DMARC** on it. Without all three, Gmail junks you. `harvey warmup` checks them, plus domain age, blocklists, your warmup progress and bounce rate, and ends with READY / NOT READY. It only reads public DNS and Harvey's own records.
3. **Warm up for 2–4 weeks** before real volume, and keep warmup running after. We recommend [TrulyInbox](https://trulyinbox.com/?via=harvey); see [Warmup with TrulyInbox](#warmup-with-trulyinbox).
4. **Ramp slowly** — 10–20/day per inbox, adding ~5/day. The `max_daily_sends: 50` default is a ceiling, not a target. Set `channels.email.warmup.start_date` and Harvey does this for you: no cold email for `min_days`, then a cold-email cap that climbs by `daily_increase` a day. Replies always go out.
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

### SMS and WhatsApp replies through nPathi Chat

People sometimes answer by text instead of email, or write to your number
first. Harvey can read those messages from nPathi Chat
(the SMS routes that receive replies: an Android SIM, a phone bridge or a GSM
modem; the official WhatsApp Business API; a paired WhatsApp number) into the
same Conversations tab, and it honours a STOP there the way it honours an
email opt-out.

**Replies only, on purpose.** Harvey never writes first by SMS or WhatsApp,
and there is no switch for it. Romanian law (Law 506/2004 art. 12, which
covers companies as well as people) wants prior consent before a commercial
SMS or messaging-app message, and WhatsApp's own rules forbid cold messages.
Answering someone who wrote to you is the lawful case, and that is what this
is for: Harvey drafts the answer, you approve it in the Outbox, every time
(autopilot never sends one).

1. **Turn the module on**: Surse → nPathi Chat, or `harvey modules enable npathi`
   (it needs the Email module: the same cycle reads both).
2. **Connect it**: Settings → Channels (or `harvey channels connect`). Enter
   your install's address (`https://...`; Harvey refuses plain http and private
   addresses) and a workspace API key with `account.view`, `sms.read` /
   `waba.read` / `wa.read` for the channels you use, and `unsubscribed.view` and
   `unsubscribed.manage`. Harvey checks the key with nPathi's account call
   before it saves it to `.env`, and never shows it again.
3. **Choose the channels** it reads (SMS and WhatsApp Business are on, a paired
   WhatsApp number off) on the card or with `harvey channels enable|disable
   sms|waba|wa`. The card lists each channel's senders and whether a reply can
   come back to each (an alphanumeric sender ID can't).

Each cycle Harvey reads what came in since its last complete read, newest
first. Then, per message:

- **STOP first.** STOP, "dezabonare", "nu mă mai contactați" and the other
  opt-out phrases put the number on the do-not-contact list and on nPathi's own
  list. When the number belongs to exactly one firm, its person is opted out
  too, their queued email is cancelled and their company's cold email stops.
  An opt-out needs no model, so it is recorded even while Claude is
  unavailable. nPathi's own opt-out list is read into Harvey's once a day.
- **A number Harvey knows** (one firm's contact) joins that person's
  conversation: their queued email is cancelled (an SMS reply stops the email
  sequence) and their company's cold email stops.
- **A number several firms share, or one Harvey doesn't know**, goes to you as
  a conversation of its own, and stops no firm.

A read that couldn't reach back to the last one shows on Today until Harvey has
read the missing part. Reading costs nothing; nPathi bills sends, in credits.

**Answering.** On Settings → Channels, per channel, choose the sender answers
go from (one a reply can come back to: a SIM, a phone bridge or a GSM modem
line, a WhatsApp Business number, a paired WhatsApp account; never a sender
ID) and switch **Send answers** on (the key needs `sms.send` / `waba.send` /
`wa.send`, and `rates.view` for the prices). Then:

- A message from a number that belongs to one firm, asking a question or
  showing interest, gets an answer drafted by the Handler (its usual prompt
  and rules) as plain text, at most 300 characters for SMS. It waits in the
  **Outbox** with its channel and the number masked: approve, edit or reject
  it there, one at a time (Approve all leaves these). You can also write an
  answer yourself on the **Conversations** page; it goes to the Outbox too.
- The first answer to a number starts with your firm's name
  (`persona.company`) and ends with an opt-out line
  (`compliance.message_opt_out_line`; "Răspunde STOP ca să nu mai primești
  mesaje." by default, the English one on an English install).
- It goes only while the window after their own message is open (SMS: 30
  days; WhatsApp: 24 hours; there are no templates yet, so nothing goes
  outside it), never a second time before they write again, never to a number
  (or a person or firm) on the do-not-contact list, only inside the sending
  hours (09:00-20:00 in Harvey's time zone by default; quiet hours always
  apply), at most 20 a day per channel by default, and inside the credits caps
  you set. No curly braces (nPathi would fill them in) and at most one link, to
  your own site.
- If nPathi says the credits ran out, sending pauses (resume the `npathi` lane
  on Coadă once credits are added); reading goes on. A number nPathi reports
  as unsubscribed goes on the do-not-contact list; one it can't reach is
  marked so for that channel. None of it touches email's daily cap, bounce
  rate or kill switch.
- Credits spent show on the card and on the Usage tab (in USD too once you
  enter what a credit is worth). Exports leave out numbers on the
  do-not-contact list, and undoing someone's opt-out lifts their number's
  entry in Harvey too; nPathi's own opt-out list is yours to change there
  (the card says how many numbers you undid are still on it).

---

## Troubleshooting

**`command not found: harvey`** — `source .venv/bin/activate` first.

**`ModuleNotFoundError: No module named 'harvey'` after install (macOS)** — Python 3.13 silently ignores `.pth` files carrying the macOS hidden flag, and some Macs propagate that flag into `.venv`. Run `harvey install` again (it auto-fixes), or: `ln -s "$(pwd)/harvey" .venv/lib/python3.13/site-packages/harvey`

**`externally-managed-environment`** — Use a venv, not system Python.

**`SyntaxError` or odd `ImportError`s right after install (macOS)** — Your venv is Python 3.9. `python3 -m venv` builds from `/usr/bin/python3`, which macOS keeps at 3.9. Check with `.venv/bin/python -V`, then rebuild: `rm -rf .venv && python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .`

**Claude headless mode fails** — `claude login`, and confirm the subscription is active. Test with `claude -p "say hi"`. In Docker, mount `~/.claude` into the container (writable: the CLI refreshes its login there). If the log says the Claude CLI "lacks --safe-mode" (or another flag), run `claude update` (in Docker, rebuild the image); Harvey resumes on its own. If your Claude login depends on `env` or `apiKeyHelper` entries in your Claude settings files, set those variables in Harvey's environment instead: Harvey's calls don't read settings files. `harvey status --check` confirms the CLI without sending a prompt.

**Dashboard says "locked"** — Open the link `harvey dashboard` printed (or `harvey dashboard --url`). If it says the token file can't be used, follow the message: usually delete `data/dashboard-token` and restart the dashboard. Visit it as `127.0.0.1` or `localhost`; other host names are refused.

**Codex calls fail** — Read the log line; every Codex error says what to do. "Not logged in" or "expired or is due for renewal" → use Codex once or run `codex login` with ChatGPT. "Hasn't been verified with Harvey" → install the supported Codex version named in the message. "Isolation could not be verified" → a Codex config layer outside your `~/.codex` (for example a managed or `/etc/codex` config) forces a tool, hook, MCP server, instructions or custom endpoint back on; Harvey refuses to send prompts until it's gone. Model catalog errors → choose an available model ID from the catalog shown in the message, or leave `brain.model` blank to select a listed model. Harvey never switches to Claude on its own.

**Discovery finds nothing** — If you're on the free OpenStreetMap source, it only covers mapped trades and is thin for service-area businesses — `harvey discover --providers` shows the alternatives.

**Overpass is throttling** — It's free volunteer infrastructure. Harvey walks three mirrors before giving up. Wait a few minutes, or use a paid provider for bulk work.

**Emails land in spam** — Almost always the domain, not the copy. Check SPF/DKIM/DMARC, confirm warmup ran ([TrulyInbox](https://trulyinbox.com/?via=harvey) shows the inbox rate; `harvey warmup` reads it once connected), halve your volume.

**Harvey does nothing during the day** — Check `quiet_hours` and whether it hit `max_daily_claude_percent`. `harvey status` shows current state; the `actions` table logs every decision.

**How do I stop it right now?** — `Ctrl+C`, or `harvey sending pause` to stop outbound while leaving the loop running. State is in SQLite, so it resumes cleanly.

**Where does my data live?** — All local: `data/harvey.db`, `.env`, `harvey.local.yaml`. Nothing goes anywhere except the APIs you configured.

---

## Project structure

```
harvey/
├── main.py              # heartbeat loop (email only)
├── brain.py             # Claude/Codex provider selection + skills + usage
├── claude_runtime.py    # Isolated Claude CLI calls (safe mode, no tools, clean dir;
│                        #   the agent's: restricted, Harvey's MCP server only)
├── agent/               # the chat's agent: read-only MCP tools (tools.py), what they
│                        #   may read (readonly.py), the views, the live chat (chat.py),
│                        #   plans, actions, wakes (wakes.py), tasks (tasks.py) and memory (memory.py)
├── codex.py             # Isolated Codex subscription runtime
├── state.py             # the StateManager: Harvey's reads and writes, cohorts
├── overlay/             # harvey.db's schema, kept-count triggers, writes, page reads,
│                        #   and the upgrade from the old database (migrate, verify)
├── upgrade.py           # the upgrade the dashboard and harvey upgrade run
├── registers.py         # register modules read in place (adapters, derived tables)
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

**You decide what a good prospect is.** A prospect list is a query over named facts you can read back, never a black box. A prospect list you can't explain is a prospect list you shouldn't send to.

**If it can be gotten deterministically, don't use a model.** Rank position, tech detection, junk filtering, the priority decision, the pre-send gate — all plain code. Claude or Codex is reserved for the parts that need judgment. That's why this runs on a subscription at all.

**A failure is an observation, not silence.** "No team page found" is a finding. "Overpass is throttling" is a finding. Silent failures look exactly like clean results, which is how a broken run gets mistaken for an empty market.

**The database is the asset, not the agent.** The initial list is worth little. The same signals observed over quarters — who changed agencies, who started spending on ads, who let their content go stale — is worth a great deal.

**Everything is editable.** Prompts, skills, config, the dashboard. All plain text. You don't need to be a developer to change how Harvey sells.

---

## Roadmap

**Done:** heartbeat loop · observation data model with governed vocabulary · signal catalog · cohort engine behind the filters · discovery provider menu with cost estimates · free HTTP profiler with agency detection · pattern-first email finding with honest status · native mail providers (Gmail API, Microsoft 365 via Graph, SMTP+IMAP) · outbox approval ladder · deterministic pre-send gate · bounce detection and kill switch · reply handling with intent classification · subscription quota tracking · website trainer · web dashboard · CSV export

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
