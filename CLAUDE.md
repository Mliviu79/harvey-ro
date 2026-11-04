# Harvey — Autonomous AI Sales Agent

Harvey is an autonomous sales agent powered by Claude Code. It finds prospects, writes cold emails, sends campaigns from your own mailbox (Gmail, Microsoft 365 or SMTP), handles replies, and books meetings — all on its own.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Harvey is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

**Changing Harvey's code or opening a PR?** Read `CONTRIBUTING.md` first. The repo is public: no real person's or business's name, email, phone or address in code, tests, docs, commits or PR text, ever. Use fictional `example.com`-style data, and report live test results as counts only.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `harvey` importable? → If not, dependencies need installing
3. Does `.env` exist with real values? → If not, they need API keys
4. Does the config Harvey reads (`harvey.local.yaml`, else the `harvey.yaml` template) have real values (not "Your Company")? → If not, they need product training
5. Does `data/harvey.db` exist? → If not, Harvey hasn't run yet. If it does, `harvey upgrade --check` says whether it is the old database (schema 24) that waits for the upgrade

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Harvey — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It runs on your Claude Max subscription so there's no extra cost.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Harvey is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Harvey is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Harvey to Users

People will ask "how does this work?" — explain it simply:

- **"What does Harvey do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It finds companies matching your target profile (Discover: OpenStreetMap for free, or a search or listings provider with a key, or the Romanian trade registry), then the contact finder visits their websites: the homepage and the team, about and contact pages its own menu links to (six pages at most), for phones, published emails (including ones hidden from scrapers), the contact form, social links and the people they name, in team cards, in sentences ("owned and operated by…", "fondată de…") or in schema.org, the owner first. A registry pull adds each firm's administrators. A company with no website can be looked up by name and town, only inside a spend cap you set. For each person named with no contact yet, it uses their own address when the site publishes it, or builds one from the company's email pattern (learned from the addresses its site publishes, or before, or a free Hunter domain lookup, else first@ for a small company and first.last@ otherwise) and verifies it through a free verification tier — every address is tagged verified / catch-all / guess, and only deliverable ones get sent. A site found by searching is the company's only on evidence: its CUI or another register fact on the pages, or a small classifier model the user sets up (Settings → Classifier: a profile per task, from the built-in setups with what they measured on labelled firms, or one of their own; off by default, then such a site waits unlinked; it can also verify a contact: yes, no or unsure). No expensive tools needed.

- **"How does it write emails?"** → It uses proven cold email frameworks (like AIDA and PAS) with strict rules — short, personal, no AI-sounding language. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Just your Claude Max subscription (which you already have), plus a mailbox to send from (a Google Workspace mailbox on a secondary domain is about $7 a month; any SMTP mailbox or Microsoft 365 works too) and, recommended, a warmup service (TrulyInbox). Everything else — prospecting, email writing, reply handling — is included; paid lookups only ever run inside a spend cap you set.

- **"Can it use ChatGPT / Codex instead of Claude?"** → Yes, opt-in. Install Codex CLI 0.159.3 (`npm install -g @openai/codex@0.159.3`; other versions are refused), run `codex login` with ChatGPT, and set `brain: { provider: codex, model: gpt-5.5 }` in harvey.local.yaml (or choose codex in `harvey setup`, which suggests gpt-5.5, or `harvey train <url> --provider codex --model gpt-5.5`). gpt-5.5 is the verified text-only model; a blank model fails on current accounts because the live catalog hides it, and code-mode models are refused. Claude stays the default; Harvey never switches providers on its own. Each Codex call runs isolated with no tools — see "Using a ChatGPT plan instead (Codex)" in the README. macOS and Linux only; on Windows, Codex is refused, so use Claude. Don't edit the user's own `~/.codex` config to make it work.

- **"What's the dashboard?"** → Have the user run `harvey dashboard` in their own terminal and open the private link it prints (`http://127.0.0.1:5555/#token=…`; `harvey dashboard --url` prints it again). Don't read, print or repeat the token yourself: it unlocks the dashboard. It opens on **Today** — anything waiting on a decision, then the pipeline. Tabs for Signals, Companies, Contacts, Campaigns, Outbox, Conversations, Activity, Usage, Settings. **Contacts** lists one row per distinct email or phone (a chain's shared phone once, with how many companies it reaches), filterable by kind (mobile for SMS), status, trust, source, place, website, signals and turnover (Actions → *Salvează ca public* keeps the filters as an audience; ticked email rows go *Adaugă în campanie…*); **Campaigns** lists the campaigns, their **Publicuri** (audiences) and a **Campanie nouă** wizard (audience with a preview of what each rule leaves out, offer and sequence, sending account, always-review, the legitimate-interest note, start); **People to search** lists people named with no contact yet; **Find contacts** (with **Search again** to force) runs the contact finder over the current filters, priced first (the searches, the address checks, and the classifier's questions as an estimated range: the caps are what is enforced), under a cap the user types (0: websites only) and a classifier cap of its own (blank: none of the run's own; 0: no question), then shows the run's report (time; searches, address checks, classifier calls/tokens/USD); the backlog shows above the list. Ticked contacts of any kind (or one contact's **Details**) go **Actions → Verify with the classifier**: priced, capped, each yes / no / unsure on its row, a no undone with its run; the filter *Verdictul clasificatorului* lists the unsure ones (`/api/contacts/verify/review`, `/api/contacts/verify/verdicts`). **Settings → Classifier** explains itself (what it decides, when it is asked, that a model's yes is likely at most and an off task leaves the site unlinked), sets each task's profile, lists the profiles side by side by name with endpoint, model, key name and whether it is set, price and measured counts ("not tested" for an edited or new one; no ranking), with use for, test (priced, capped), edit, duplicate, hide/restore or remove, the caps and spend, and the DeepInfra key's scopes. **Coadă** shows an opened run's report and its classifier cap; Today lists the newest runs with theirs (`/api/jobs/batches`). The Companies menu has it too. **Companies** sorts (newest, name, place, turnover, employees, contacts, last searched) and filters (the Contacts page's company filters plus activity/CAEN, has-contact and source) on the server, with page numbers and a page box; its panel shows every CUI, financial year, registry row, ANAF answer and taxpayer row on file (`harvey/companies.py`), "not loaded" when a register isn't. **Registry** browses the whole trade registry joined with its latest financials (`harvey/registry_view.py`, `/api/registry`): the registry source's manifest filters (plus status operating/all), sorts by name, turnover, employees, profit or registration date from a kept list, marks firms already in Harvey by CUI (or `onrc:<reg>` for a firm with none), shows a firm's financial years side by side with its registry, taxpayer and ANAF rows, and pulls the ticked firms (`regs`) or the filter through Discover's registry pull, counted first.

- **"What are signals?"** → Signals are the facts Harvey collects about a business: who its current agency is, whether it's running ads, whether it has online booking, how it ranks. **Harvey proposes; you confirm.** Nothing is collected until the user says yes on the Signals tab (or `harvey signals --confirm ...`). That keeps spend intentional and makes every prospect list explainable — a cohort is a query over signals a human chose, not a black box.

---

## Setup Flow

**Where settings go:** `harvey.yaml` is the tracked template; Harvey never writes it, and neither should you. Every change (training, `harvey setup`, the dashboard, your own edits) goes in `harvey.local.yaml`, which wins when present. Harvey makes it as a copy of `harvey.yaml` on its first save; before editing it by hand on an install that has none, copy `harvey.yaml` to `harvey.local.yaml` first (it is read instead of `harvey.yaml`, not on top of it, so a file with only the changed keys won't load). If `git pull` refuses over local changes to `harvey.yaml` (an older Harvey wrote it), make sure `harvey.local.yaml` holds those settings, then the user runs `git checkout -- harvey.yaml`.

Walk through these steps conversationally. Ask one thing at a time. Don't overwhelm.

### Step 1: Install Dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

No browser to install: Harvey reads sites over HTTP, and a browser only
renders JavaScript-only websites. Docker Compose runs an `obscura` service
for those, used by default (`BROWSER_CDP_URL=off` turns it off; setup writes
its `BROWSER_CDP_TOKEN` to `.env`), and an optional Chromium under
`--profile chromium`, behind an egress proxy that refuses private addresses,
for sites that block Obscura, `BROWSER_CDP_FALLBACK_URL`. Outside Docker,
leave both blank to stay HTTP-only.

### Step 2: API Keys (`.env`)

Harvey needs an **email provider** (to send + read replies: Gmail, SMTP or Microsoft 365) and, strongly recommended, an **email verifier**. Pick one provider:

```
# --- Email provider: choose ONE, set channels.email.provider to match ---
GMAIL_CLIENT_ID=            # Recommended: Gmail/Workspace. Then run 'harvey gmail auth'
GMAIL_CLIENT_SECRET=
# --- or SMTP+IMAP (AgentMail, Fastmail, Workspace app password) ---
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# --- or Microsoft 365: then run 'harvey microsoft auth' (docs/microsoft365.md) ---
M365_CLIENT_ID=  M365_TENANT_ID=

# --- Email verification (add at least one; else emails stay 'guess') ---
REOON_API_KEY=              # 600 free/mo — best free tier
ZEROBOUNCE_API_KEY=         # 100 free/mo — best for M365/Workspace catch-alls
HUNTER_API_KEY=             # 50 free/mo + pattern lookup

# --- Discovery (optional — the free OpenStreetMap source needs no key) ---
TINYFISH_API_KEY=                         # free web search (30/min, 500/hr); no rank signals
BRAVE_API_KEY=                            # website lookups only ($5/1k, $5 free a month); never a Discover source
DATAFORSEO_LOGIN=  DATAFORSEO_PASSWORD=   # cheapest local-business + SERP data
DATAFORSEO_SANDBOX=                       # any value routes to the free sandbox

# --- Optional ---
CLOUDFLARE_ACCOUNT_ID=  CLOUDFLARE_API_TOKEN=   # deep JS crawling for training
SERPER_API_KEY=            # web search + discovery (2,500 free, then a $50 prepaid pack)
PRECLEARED_API_KEY=        # cold-email check before first sends (see Step 7)
TRULYINBOX_API_KEY=        # warmup: set by 'harvey trulyinbox connect' (see Step 2b)
```

**Recommended provider — Gmail:** for <50 cold emails/day, a real Google Workspace mailbox on a *dedicated secondary domain* (never the main one) is the most deliverable, cheapest (~$7/mo) option. Set `channels.email.provider: gmail` in harvey.local.yaml, put the OAuth client id/secret in `.env`, then run `harvey gmail auth` (one-time browser login). SMTP works with any mailbox (AgentMail, Fastmail). Instantly is no longer supported: a config still saying `provider: instantly` sends and reads nothing and says to pick gmail, smtp or microsoft (the log, `harvey status`, Today), while finding contacts and the rest keep working.

**Email verification matters:** Harvey learns each company's email *pattern* and verifies ONE candidate rather than guessing (raw SMTP probing is not viable from a locally-running agent — outbound port 25 is usually blocked, and Google Workspace / Microsoft 365 accept everything from an unknown IP). Without a verifier key, found emails are marked `guess` and are **never sent**.

**Approval by default:** every outgoing email waits in the **Outbox** for your approval (dashboard Outbox tab, or `harvey outbox`). **Autopilot** (`channels.email.require_approval: false`) sends on its own, still inside the daily limit, quiet hours and opt-out rules, but holds any email that links to a domain that isn't the user's or quotes Harvey's own instructions (text from websites and replies can carry planted instructions). Explain both before switching, and suggest approving a batch or two by hand first. Cold email also waits, whatever the mode, until the user has confirmed the cold-email acknowledgement once: dashboard → Settings → Sending (a dialog with the text, where they type `accept`), or `harvey ack` in their own terminal (don't type `accept` for them). On autopilot, an email (cold or an answer) is approved by the drain that sends it, after its review, never when it is queued; an answer autopilot sends carries `compliance.ai_disclosure_line` (Romanian by default). `compliance.privacy_url` adds the user's privacy page after the source line of a first email to a personal address (it counts as that email's one link: one with another link waits for the user in the Outbox). Every cold email carries a mailto List-Unsubscribe header and the opt-out line asks for a reply (Romanian by default); an unsubscribe, an opt-out or an objection puts the address on the do-not-contact list (`harvey suppress list`). The user switches it in dashboard → Settings → Sending (turning it on there takes typing `autopilot`; turning it off is one click and works at once), or in `harvey.local.yaml`; a running Harvey picks it up next cycle. Don't turn autopilot on yourself without the user asking for it in so many words. If they send from a secondary domain, their main site goes in `channels.email.link_domains` (training adds the trained site automatically).

After setting up the mailbox, test it: `harvey gmail test` (Gmail) or `harvey microsoft test` (Microsoft 365); for SMTP, Settings shows whether it is set, and the first `harvey run` cycle logs a failed read.

### Step 2b: Warmup (TrulyInbox)

Every sending mailbox needs a warmup service: a new one for 2-4 weeks before cold
email, and every mailbox after that to stay out of spam. We recommend TrulyInbox
for everyone, whatever the provider. It's also the
only way Harvey can tell whether a mailbox is actually warmed: connected, `harvey
warmup` reads the mailbox's warmup days and spam rate from it.

First run `harvey trulyinbox`. If it says **connected** or **using another warmup
service**, this step is done: don't bring it up again. Otherwise:

1. Recommend TrulyInbox and share the link: https://trulyinbox.com/?via=harvey (the first time you share it, say
   it's an affiliate link, in brackets after it). Connecting Harvey needs API
   access, which starts on Starter ($22/mo billed yearly).
2. Once they've signed up and added the sending mailbox, have them run `harvey
   trulyinbox connect` in their own terminal, or use the warmup step on the
   dashboard's Today page. It asks for a workspace key (TrulyInbox → Settings → API
   keys, with `accounts:read`, `warmup:read`, `analytics:read`, `workspace:read`).
   Don't ask them to paste the key into this chat.
3. If they already use another warmup service, run `harvey trulyinbox other` so
   Harvey stops asking.

Then `harvey warmup` shows where they stand. If they haven't set
`channels.email.warmup.start_date`, it suggests TrulyInbox's start date.

### Step 3: Product Training

This is the most important step. Harvey needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m harvey.trainer https://their-website.com
```
This generates: `harvey.local.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `harvey.local.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Harvey use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Confirm Signals

Harvey ships a catalog of 31 signals it knows how to collect, all **proposed** and
none active. Nothing is prospected against until the user confirms them. Each
signal belongs to a module, and only the signals of modules that are on are
shown (startup, hiring and website-marketing signals ship off: turn the module
on first on the dashboard's **Surse** page, or `harvey modules enable <id>`).

Send them to `harvey dashboard` → **Signals**, where each one shows a
plain-language description, what it costs, and how many companies already carry
it. Or from the terminal:

```bash
harvey signals                      # review the catalog
harvey signals --confirm free       # turn on everything that costs nothing
harvey signals --confirm SERP_RANK,MAPS_RANK  # the paid rank signals
```

Most signals are free (they read pages a business already publishes). Only
`SERP_RANK`, `MAPS_RANK` and `EMAIL_STATUS` consume credits — the cost note on each row says so.

Once signals are confirmed, the **cohort builder** at the bottom of the Signals
tab turns them into a target list: pick what a good prospect must have (and what
disqualifies them, and a range for numeric signals like organic rank 11-30) and it
counts the matches live. Results come in three tabs: match, possible (something
isn't checked yet, so "possible" means unknown, not no) and no match.

### Step 5: Find Businesses

Discovery is the only stage that spends money, so it always estimates first.

```bash
harvey discover --providers                        # the menu, with real prices
harvey discover --estimate                         # projected spend, then exits
harvey discover                                    # free OpenStreetMap source
harvey discover --provider dataforseo_listings --max-spend 2.00
harvey discover --preset yc --provider serper   # startups that say they're YC-backed
harvey discover --preset hiring --provider serper   # companies whose careers pages list your roles (the hiring module must be on)
harvey discover --preset accelerator --provider serper   # ...Techstars, Antler, EF and others
harvey discover --preset funding --provider serper       # companies announcing a raise
```

A run also looks up a website for every company it saves with none (on by
default; free searches always, paid ones up to their own amount on top of the
cap: **Also find websites, up to $X**, or `--websites-max-spend N` /
`--no-find-websites`). Brave Search is a website finder only, never a source
here: its terms forbid building a database of its results, and Harvey never
stores them.

Every search takes every result the source has, page by page; the only stop
is the spend cap (`--max-spend` or the tab's cap, checked before each page) and
the stop button, and a stopped run says how many results were left and what
they would cost. For DataForSEO Business Listings, the estimate first counts the
businesses (about a cent a search), so its price is exact.

The dashboard's **Discover** tab is the same thing with the tradeoffs laid out
side by side: what each source does, what it costs, its free tier, and which
`.env` keys it needs. Nothing is called until the user presses Run. Sources are
checkboxes: several run as one batch under one cap (estimate = sum; a preset a
source can't search with leaves it out with a note). The ONRC registry is a
source too (its manifest's `discover = "registry_pull"` and `filters`): its
panel takes activity/CAEN, county, town, turnover, employees, employer, VAT and
micro, its estimate is the free count, and it queues a registry pull in the
batch. Whatever any source saves feeds the contact finder.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** (default) | free | unlimited | Trying the whole pipeline with no account. Thin coverage for service-area trades — OSM maps premises, so it has under 2,000 roofers in the entire US. A trial source, not a complete list. |
| **DataForSEO Business Listings** | $0.372/1k businesses ($0.012 a page of up to 1,000 + $0.00036 each) | $1 credit | Local trades, clinics, contractors. Every business the search matches, no ceiling. Phone + domain + rating + whether the listing is claimed, and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.002 per 10 results (Live): up to $0.04 a search | $1 credit + free sandbox | Rank as the buying signal. Positions 11-30 are the sweet spot. Also records who holds the local 3-pack (`IN_LOCAL_PACK`). DataForSEO stops at 200 results. |
| **DataForSEO Google Maps** | $0.002 per 100 results (Live): up to $0.014 a search | $1 credit + free sandbox | Map rank (`MAPS_RANK`), rating, category, claimed status; keeps businesses with no website. DataForSEO stops at 700 results. |
| **Serper** | ~$0.30-$1.00/1k searches, one per page of 10 | 2,500 free, no card | The easiest paid one to try. Pages until Google has no more. |

Cities are geocoded automatically (cached forever, one lookup per city), or set
`icp.geo_coordinates` in harvey.local.yaml to control the radius:
`"Denver, CO": "39.7392,-104.9903,50"`. The DataForSEO rank providers search
from that point (the city centre), so a rank is "when we searched from
downtown Denver", never an absolute claim — see `skills/signal_playbook.md`.
Add more search phrases per industry with `icp.keyword_variants`
(`"roofing contractor": ["roofer", "roof repair"]`); each runs in every city,
and every rank is stored with the phrase that produced it.

**Selling to startups:** three presets (above, or *Look for* on Discover), once
the `startups` module is on (`harvey modules enable startups`; `hiring` for the
hiring preset):
`yc` finds companies whose own site says they're backed by Y Combinator,
`accelerator` the same for Techstars, 500 Global, Antler, Entrepreneur First,
South Park Commons, Neo, AI Grant, a16z speedrun, Pioneer and Plug and Play,
and `funding` companies announcing their own raise in the last 18 months.
Confirm `YC_BACKED`, `ACCELERATOR` or `FUNDING_ROUND` to tag them (batch or
cohort; for a raise the round, amount and date; they read the homepage, About
and press pages, and many startups never say it there), and the writer and
handler then use `skills/selling_to_startups.md` for them. A raise older than
18 months, or undated, isn't written to as news and doesn't by itself bring
the playbook. Harvey never reads ycombinator.com, the
accelerators' directories, Crunchbase, PitchBook or LinkedIn (their terms
forbid scraping), so don't suggest scraping them instead.

**Every page is billed.** Google removed 100-results-per-page in September 2025,
so a deep search is billed as ten pages per 100 results at nearly every provider,
and below rank 30 it is mostly directories. Harvey goes as deep as the source
serves; `--depth 30` keeps a rank search to three pages.

### Step 6: Behavior Settings

These go in `harvey.local.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_claude_percent`: 80 (how much of daily Claude quota to use)
- `heartbeat_interval_minutes`: 15 (how often Harvey checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

### Step 7: Cold-Email Check (Optional)

Offer this once, briefly, and take no for an answer. With `compliance.precleared.enabled: true`, Harvey asks [Precleared](https://precleared.dev) which cold-email rules apply to each new address before the first email goes out. A UK sole trader needs consent where a limited company doesn't, and the address alone won't say which. Anything that isn't `ok_to_email` waits in the Outbox with the reason. It checks cold email sent through Gmail, Microsoft 365 or SMTP. Precleared receives each new recipient's address, the sender country, the address source, the product description and the recipient's country when Harvey is sure of it from the company's location (a country or state named there), else the prospects' country if there is one (`prospect_country`, or the one country every `icp.geography` entry is in), plus the company's name and the person's name when Harvey has them, to find a UK company on the register. `docs/precleared.md` explains the check in full.

If they want it, ask one thing at a time:
- Where is the business based? `sender_country` must be `GB` or `US`. If it's neither, leave the check off.
- Are all their prospects in one country? If so, set `prospect_country` to its two-letter code, quoted (for example `"US"`). Otherwise leave it empty.
- Would they like a free key? Without one Precleared allows 10 checks a day; a free key from https://precleared.dev/get-key allows 25. It goes in `.env` as `PRECLEARED_API_KEY`.

Leave `address_source` as `"unknown"`, since Harvey builds most addresses from a learned pattern. Save the settings under `compliance.precleared` in `harvey.local.yaml`. `harvey setup` asks the same questions.

### Step 8: Start Harvey

```bash
source .venv/bin/activate && harvey run
```

Or open the dashboard:
```bash
harvey dashboard
```

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Harvey has done"** → Run `harvey status` or `harvey dashboard`
- **"Am I warmed up?" / "Why am I landing in spam?"** → Run `harvey warmup`. Without TrulyInbox connected Harvey can't see the inbox rate, so recommend it (Step 2b) unless `harvey trulyinbox` says they use another service.
- **"The emails aren't good"** → Edit `skills/email_frameworks.md` and `prompts/writer.md`. Show them the current rules and help them adjust.
- **"Harvey isn't finding the right people"** → Check the ICP config in `harvey.local.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Harvey says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Harvey on a different product"** → `harvey train <new-url>`
- **"How do I see the database?"** → It's at `data/harvey.db` (Harvey's own work: firms it worked on, contacts, searches, emails); the registers stay in their own files beside it (`registry.db`, `financials.db`, `taxpayers.db`, `anaf.db`). They can open any of them with an SQLite tool, or ask you to query them.
- **"The dashboard shows a database upgrade" / "harvey says to run harvey upgrade"** → Their `harvey.db` is the old one (schema 24, with copies of the registers inside). Explain it: the upgrade builds the registers' indexes in their own files, compresses `anaf.db`, moves Harvey's own data into a new slim `harvey.db` (about 1 GB instead of 16), verifies it, then switches, keeping the old files as `harvey.db.v24` / `anaf.db.old`. About half an hour on a full install, at least 30 GB free; it resumes after a stop. They start it from the dashboard's screen or with `harvey upgrade` (stop `harvey run` / `harvey worker` first; the dashboard's own worker waits by itself). If the free space can't be checked in Docker on WSL, they run `scripts/disk-check-folder.sh` once from the WSL shell in the checkout (it finds the Windows drive that holds their WSL disk, which isn't always C:, creates an empty `harvey-disk-check` folder on it and adds `HARVEY_HOST_DRIVE_SOURCE` to `.env` without printing anything else from `.env`; a folder already set there is checked, and one on another drive, such as C: from older instructions, is reported with what to change it to), then `docker compose up -d harvey`: never the drive itself, which would show the whole Windows drive to a container that reads untrusted websites. Never delete the old files yourself: they use **Delete old files** or `harvey upgrade --delete-old` once happy.
- **"I just want the prospect list"** → `harvey export` writes a sequencer-ready CSV (verified/risky emails only; `--all` for everything). Also available as Export buttons on the dashboard's Contacts tab. Harvey is valuable purely as a list-builder even if the user never lets it send.

---

## How Harvey Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): the Email module's only. Every cycle → check quiet hours → check budget → read replies → drain the Outbox (unless replies couldn't all be read, or sending is paused) → the campaign cycle (`agents/campaigner.py`) → log → sleep. With the Email module off it does nothing; with a provider it can't send through (`config.provider_notice`: Instantly, removed) it logs why and sleeps, sending and reading nothing. It never looks for people: the contact finder does.
- **Campaigns** (`campaigns.py`, schema 27, conversations' campaign backfilled by 28): the user makes them (dashboard Campanii, `harvey campaigns`); new ones need Gmail, SMTP or Microsoft 365. An **audience** is a saved Contacts query string, read by `contact_query.filter_from_query` (the same parser as `/api/contacts`). Start (the note of 80+ characters, a sequence and the acknowledgement first) freezes the filter and takes its matches; a rolling campaign re-reads it at most hourly. Every way in (snapshot, roll, by hand) is `campaigns.add_members`: verified, catch-all (with `send_to_risky`) or unchecked; trust confirmed or likely through a live firm; not suppressed; not open in another active or paused campaign nor sent cold email in the last 90 days (nor `contacted` with no cold email on file: an earlier release's Instantly, or a hand send); nobody at the firm answered; the preview counts each. `campaign_member` (keyed campaign + address) holds each address's progress; `prospects` stays its card (status a summary). Each cycle, per active user campaign: roll, check waiting addresses (20 a cycle; a roll's 50 a campaign a day), draft first emails for ready members that can be sent to (8 a cycle across campaigns, only while queued first emails are under two days of the cold cap: `campaigner.QUEUE_DAYS`), stage them. Staging is always `pending_review`, and so is an answer the Handler queues; on autopilot the drain approves due rows at send time (`state.promote_for_autopilot`, cold email up to what the cold cap leaves, answers apart and never in quiet hours; never a row with a note, such as the first answer after an undone opt-out). Approving a row of a paused campaign gives it the paused note. A rejected email, or one cut off mid-send (`fail_stale_sending_outbox`), ends its member (`campaigns.stop_after`: reason `rejected` / `cut off mid-send`, its later steps cancelled). `campaigns.record` is the campaign's record (note, the acknowledgement it started under, members with `added_by` and `end_reason`, sends, opt-outs): `/api/campaigns/{id}/record.json|csv`, `harvey campaigns export ID`. A paused or draft campaign's rows are left out of the drain's pages (`get_outbox(skip_held_campaigns=True)`). Once started, the note can't drop under 80 characters; a non-rolling campaign with no open member (or none at all) finishes. `sending_account` row 1 mirrors the settings; `brain.for_profile` returns today's brain. A campaign the Writer made earlier is `origin = auto`: its active ones are still staged by the Sender, its drafts never; the Writer makes none any more.
- **Contact finder** (`finder.py`, core module `contact_finder`, job `find_contacts` on the `contacts` lane, one company per job): every company with no contact is a record to search. Free first (on-file phones are contacts already; a known or registry-declared website is read with `site_reader(contacts=True)`: the homepage and up to five pages its own menu links to (team, about, contact; the usual paths only when the menu links to fewer than two), at most ten requests (the read's other fetches: About/press pages, robots, sitemap, careers, a board feed, have their own 14, `MAX_EXTRA_FETCHES`; a page is fetched once per read); phones, emails (obfuscated ones decoded), contact form, social links, and the people named in team cards, schema.org and sentences ("owned and operated by", "fondată de"), owner first, with `LIKELY_OWNER` when confirmed; the addresses' spelling is kept as the domain's email pattern). No website and no contact: a lookup by name and town through each search capability with `lookup = "website"` in its manifest, cheapest first, only inside a spend cap (the batch's, or `spend.per_day_usd` for background work; never without one, unless the search costs $0). Results are used once in memory, never stored (the search row keeps counts by reason and the domains read). Every result is weighed (`finder.choose`, `site_match.py`): `screen` rules out by the address alone (`reference/directories.csv`, the firm's CUI or registration number in the path, institutions, a profile-shaped path with no name word in the host); the classifier's `results` task (a yes/no statement per result) keeps three at most (else the best name matches); those are read with `read.fetch` (nothing written) into evidence cards; `site_match.hard` decides what a register fact settles (the firm's CUI in a fiscal context or registration number with no other firm's CUI, an official email or phone next to the name (an email also at the site's own domain) that few firms share (`site_facts` holders): confirmed; its own CUI beside others: a question; one other CUI as owner with nothing of this firm's, or two or more and not its own: ruled out); `finder.decide` asks the classifier's `site` task for the rest, each candidate alone (`finder.ask_sites`, a call each): exactly one yes is likely, never confirmed; none links nothing; two or more wait, unsettled. On the site chosen the `contacts` task (a yes/no per contact read there, social profiles included) picks what is linked through `read.keep`, the official ones always (a site a register fact confirmed loses only what it says no to); with no answer from it, nothing beyond what code confirms (a site a register fact confirmed: everything code read; one the model chose: only the official ones). With no answer (off, held, day cap) the first candidate still in question is kept at unconfirmed with nothing linked, the others named in its read's search row, waiting for `harvey contacts rejudge`; `find` never reads such a site. A found site read again (Search again, a refresh) goes through `finder._reread_found`: the same card and `decide`, only the chosen contacts linked, at most at its trust. A firm's own declared website keeps the old read. Why a found site was taken is `firm.website_basis` (schema 29), shown on the company panel. Only contacts an official source gave count as "on file" (`reads.finder_on_file`: `SITE_SOURCES` never confirm a site); below confirmed no facts, rename, people or domain key. Then one verified address per person with no contact (`email_verify.find_email`, inside the cap; `verify_costs.Meter` is the capability's budget: email_finder calls `budget.ask(service)` before each verification request and `budget.take("hunter")` before a pattern lookup, each counted per service and month as it goes (one request is one check at that service; `EMAIL_VERIFY_MONTHLY_CHECKS`, email_verify's `monthly_searches`, caps every service's checks together, so it is the sum of those counters plus the old per-address counter of the month; Hunter's lookups share the `HUNTER_MONTHLY_SEARCHES` counter), $0 inside its manifest's `[cost.verifiers.<id>]` `free_per_month` (Reoon 600, ZeroBounce 100, Hunter 50, `hunter_search` 25 with `kind = "search"`), past it the user's entered price (module setting `usd_<id>`, $0 to `MAX_USD` $10, Settings → Email verification `/api/verify/prices`, `harvey modules set`; none declared in manifests), reserved from the run's cap and recorded as `spend.checks` / `spend.lookups`; past it with no price: refused under a USD cap (`no price for <id> past its free tier`), and a check whose answer needed a refused service is a search error (not done, nothing kept, `checks_refused`), a refused lookup falls back to the default pattern; with no USD cap (the audience) made and counted unpriced; `finder.checks_estimate` in the find estimate: entered prices, `refused`, `checks_left`): a person's own published address is linked with no check, also with no cap (the free path links those only, and marks nobody else searched); others are built with the pattern the addresses on file show, else `first@` for a small company (ICP size, size on file, `EMPLOYEES` or a local listing source, 20 or fewer) and `first.last@` for others; a guess or a catch-all's `risky` is kept no surer than likely, and an address `find_email` built (or one found invalid) is never evidence of the pattern. Each step is a `searches` row. The backlog reads them per source: a site is to read until a read with `looked_for` containing `owner` (the finder's read records `phone,email,contact_form,social,people,owner`; one recorded before it followed the menu has no `owner` and is read once more, by the background after the companies with no contact, whatever contacts it found; a plain "Read sites" read records `phone,contact_form`), and a company with no website is to look up while a configured search module hasn't searched it. Triggers: every Discover batch's saved/matched companies with a website, free only (`enqueue_finder_follow_ups`; a registry pull chains it after its ANAF checks; lookups are a Find-contacts run with its own cap), Find contacts (`POST /api/contacts/find`, `/api/contacts/find/estimate`, `harvey contacts find`), and the worker's background feed (`enqueue_backlog`: free website reads, newest first; paid lookups only with `spend.per_day_usd`; rests 15 min when nothing is left). Runs whatever the Email switch says.
- **Classifier** (`classify.py`, profiles `classify_profiles.py`, prices `classify_prices.py`, the decision provider and its site/contacts questions `classify_decision.py`): a small fast yes/no classifier, separate from the brain (the brain never uses it), with named tasks (`results`, `site`, `contacts`: each a request of a state text and statements with ids, each answered yes or no; a render, a check, its own day cap; a new task is one `register(Task(...))`). **Profiles**: one complete setup each (kind openai_compatible|anthropic|decision|brain, `base_url`, `model`, `key_name` naming the `.env` key, `request_extra` merged into the request body (plain keys and values only, never `model`/`messages`/`max_tokens`/`response_format` and the rest of `classify_profiles.RESERVED`: `extra_problem`), `json_mode`, `per_minute`, `timeout_seconds`, `prices` (in/out per million, or `from_list` for DeepInfra chat), the tasks it answers, and its questions per task as data: a language model's prompt/yes/no texts, a decision model's site question texts with `needs`/`vetoes` bars and its contacts `instructions` (`{n}`)/`options`/`bar`; code keeps the ids, the options' keys and the combining rules, bars clamped). Built-ins in `reference/classifier_profiles.json` (read-only, only setups that tested well, each with its measured counts on the developers' 200 hand-labelled firms, not shipped: DeepInfra gemma-3-27b, Mistral-Small-3.2, Mistral-Small-24B-2501, Qwen3.8-27B with `{"reasoning_effort": "none"}`, TypeSafe Jev, a local Ollama qwen2.5:3b; the DeepInfra key's scopes, `billing:read` and `metrics` added by hand). Yours in the settings table (`classify:profiles`); editing a built-in saves a copy `based_on` it; built-ins hide/restore (`classify:hidden`); a profile of yours is "not tested" until a test run records on it and again after any change to what it asks (`asks_hash`). Each task is set to a profile that answers it, or off (`classify:use`; a decision model can't take `results`). The install's caps stay the contact finder's settings (`classify_per_day`, `classify_tasks` per_day, `classify_usd_per_day`/`_per_month`: `classify.install_caps`; `POST /api/classify/settings` takes only these); a profile is the only source of a setup: an install whose older `classify_*` settings name a provider gets them once as the `settings` profile for the tasks they were on for (`classify_profiles.migrate_legacy`, read raw from `modules.contact_finder.classify_*`, marked `classify:legacy`; no profile of yours is given the `settings` id), never read again. `classify.settings(state, task)` resolves a task's profile (`Settings.for_task`, `by_task`). Measured numbers are shown as facts side by side: nothing ranked, badged or put forward anywhere (docs, UI, CLI; a test checks the strings). A run's money: a job sets `classify.METER` (`JobContext.classify_meter`, `job_kinds.metered`): every attempt counted in the job's `progress.classify` (calls, tokens, USD, ms) and charged to its `actual_cost` (so `spend.per_day_usd`/`per_month_usd` count it), and before every attempt `jobs.classify_room` checks the call's worst case (`classify.worst_tokens`: the request's counted tokens x `TOKEN_MARGIN` 1.25 + `TEMPLATE_TOKENS`, with the task's most output tokens, x price) against the run's `classify_max_spend` (batch params; None no cap, 0 no calls) and the day/month limits: refused is "run budget" (`site_check.STOPS`; the site waits unlinked; a run's own cap pauses the job with the spend limit). The run's search cap and pools leave classifier spend out (`jobs.CLASSIFY_USD`). Estimates before a run (`finder.classify_estimate`, `contact_check.estimate`, `site_check.estimate_test`, the find/verify/test estimate APIs, the CLI) are never limits: a range `usd_typical`/`usd_high` per task (`classify.projection`: this month's average and largest billed input tokens per task, the profile's model's when it has any (`classify_sizes` per task and model), else `TOKENS_GUESS` and `worst_tokens(max_input_tokens)`, each with the most output) and `note` = `classify.ESTIMATE_NOTE` saying the cap is what is enforced; `finder.plan` marks what the run will do as `_find` decides it (forced or not, `refresh_days`): `reread` (a found website, not `website_waiting`, not read since: a site and a contacts call) and `look_with` (the searches a company with no website hasn't had: its lookup's cost and classifier calls). `site_match.tokens` (3.3 chars a token) vs Qwen2.5's and Mistral-Small's (24B-2501 `tokenizer.json`, 3.2 `tekken.json`) tokenizers on the fictional cases: 0.89-1.07 per task, 1.19 worst request (hence the margin; Gemma-3's is gated, not compared). `jobs.batch_report` / `GET /api/jobs/batches/{id}/report` / `harvey jobs report RUN`: time and cost split (searches, address checks with `checks_usd`, `checks_refused` and per-service `services`, Hunter's `lookups_*`, classifier); a finished run's report is stored on its batch (`job_batches.report_json`, schema 30) with its jobs' statuses and last finish, read from there while they are the same, cleared by `jobs.retry`/`resume`; a running run's is computed each time. A language model answers `{"<id>": "yes"|"no"}` (a missing id no answer) through the tolerant parser (`first_json`) with one retry carrying the error. `decision`: a decision model over httpx in the System One format (POST `{"state", "model", "questions"}` to the profile's `base_url` exactly as given, the full endpoint: it is for TypeSafe's Jev, `https://api.typesafe.ai/v1/systemone`, model `jev-latest`, key `TYPESAFE_API_KEY`, none filled in; `Authorization: Bearer`; reads `answers[id].noul` / `.probabilities`, `usage.input_tokens`/`output_tokens`). A task declares its own state, questions and combining rule for it (`Task.decision`, `Task.decision_check`; `results` has none, so it is "task off"): site = Nouls names >= 0.5 and own >= 0.4 (`classify_decision.SITE_NEEDS`), directory and institution < 0.7 (`classify_decision.SITE_VETOES`), fixed as measured (`p` = own goes into `website_basis`, the panel shows it); contacts = a Choice per contact (`classify_decision.OWNERS`, company_own first: order matters), kept at P(company_own) >= the profile's contacts `bar` (0.7, clamped 0.5..0.99). No key, endpoint or model: held, nothing sent; 401/402/403/404/429/quota held (a 429 for its retry-after-ms or retry-after), timeouts/connection errors/5xx retried; no second ask of an invalid reply. Each profile has its own pace (`lanes.wait_gap("classify", gap, key=classify.pace_key(s))`; the classify lane declares no pace of its own) and held state (`classify.hold`/`held`, `lanes.hold_info`): a 429, an unknown model or a setup that can't be sent holds the profile (`classify.<profile>`); a refused key, a payment or a spent quota (`held_scope`) holds its account (`classify.account.<hash of host and key name>`), every profile on it; `/api/classify` `held` {profile: seconds, why, scope}, each profile's `held` in `/api/classify/profiles`, `harvey contacts classifier`, the lanes' rests (`classify.held_seconds`; a held contacts profile rests only `verify_contacts` jobs, `jobs.rest_lane(kind=)`); a retry-after of 0 holds a second. A language model's second ask (after an invalid reply) is allowed by its own worst case, the error it carries counted. Retries after timeouts and 5xx, and a `usage_events` row per call, a failed one too (`task = classify.<name>`, which the day caps count, checked before every attempt, retries included; a request is one call; `classify_per_day` 0 means no calls). Each row carries `cost_usd`: the reply's own (`usage.estimated_cost`, DeepInfra's), else tokens x the entered price, else, for openai_compatible on DeepInfra only, tokens x DeepInfra's public list price (`GET /models/list`, `cents_per_*_token` x (1 - discount), kept a day in the settings table; a failed fetch keeps the last list however old, else unknown for 10 min; never an error), else 0 with `source = classify:unpriced`. The USD caps (sum of the classifier's own rows since UTC midnight / the month's start) are checked with the call caps before every attempt, with the call's worst case (what is spent plus it must fit): reached is "budget", handled as the day cap (`site_check.STOPS`: the lane rests, firms wait unlinked); with the price unknown (`cost_status`) they can't count, the card says so and only the call caps hold. Settings → Classifier shows the metrics (`classify.metrics`: calls and spend today/this month, cost per site checked, tokens a call, per task; one line in `harvey contacts status` and judge-test), on a DeepInfra base URL a model picker from the price list (`/api/classify/models`, text-generation, for openai_compatible only, cheapest first; a decision model is priced only by the prices entered) and an Account box (`/api/classify/account`: `/payment/usage`, `/payment/key-limits`, `/payment/checklist` read server side with the classifier's key, each part optional, 401/403 = the key lacks `billing:read`, kept 5 min; the key never reaches the browser). The answer counts only for the ids asked, of what the request showed (`site_match.cards_shown`). A profile's `key_name` is the provider's own `*_API_KEY` (the built-ins: `DEEPINFRA_API_KEY`, `TYPESAFE_API_KEY`; `CLASSIFY_*` is taken for a migrated older setup) and never one of Harvey's own keys (`key_name_problem`); its `base_url` is http(s), never link-local (`base_url_problem`). `/api/classify/profiles` gives each profile `is_deepinfra` (the exact host `classify_prices.is_deepinfra` matches) and `deepinfra_host`, for the UI. The SDKs (openai, anthropic) are imported only inside a call; a missing one (an image built before them: `docker compose build harvey`) is "sdk missing", shown on the card. `ask` never raises: an Answer with no value says why. `site_check.py`: the re-check of found websites (`recheck_site` jobs on their own `site_check` lane, `harvey contacts rejudge`, Today's `site-check` item, Settings → Classifier; held or out of calls: nothing read, the lane rests; a waiting site judged with every candidate its lookup kept (`site_match.waiting_detail` on its read's search row); unreadable taken off only after `UNREADABLE_DAYS` days; two or more yes: a `judge_site` search row `unsettled by <provider> <model>`, not asked again by the same classifier; `store.reject_found_site` takes off exactly what the site's read left and keeps it in `site_undo`, and marks the searches that relied on it (the newest lookup that found one, its reads, the people's address checks) as errors, cost kept, so they count as not done; `accept_found_site` re-trusts, `undo_site_checks`), and `judge_test` over `reference/classify_cases.jsonl` (fictional) or a file, for a profile (`s=classify_profiles.settings_for`), counted and priced first (`estimate_test`), as a `classify_test` job on the `classify` lane under a typed USD cap, its counts kept on the profile (`record_test`, ten runs). `contact_check.py`: a contact verified by the contacts task about that one contact (the firm's facts, its site read again when the contact is on it, else the evidence on file), "unsure" allowed (a language model's JSON; a decision model's P >= bar yes, <= 1 - bar no); `store.check_link` keeps it in `contact_check` (schema 30): yes and unsure only a row (unsure listed for review), no lowers the link's website-read evidence to unconfirmed with the old trust in the row (`undo_contact_checks`); official evidence and campaign members never lowered, nothing deleted; `verify_contacts` jobs, one per firm on the `classify` lane, a resumed one asks only the rest.
- **Background website searches** (`finder.py`, `modules/lanes.py`): the backlog's walk also looks companies with no website up, but only inside the recurring free allowances search modules declare (`[[free_allowances]]`: TinyFish a minute and an hour, Brave a month; never a one-time sign-up credit), taking `background_share` of each (half by default) so the user's runs keep the rest; paid searches only with `spend.per_day_usd`. Every request is counted in `data/lanes/<module>.uses`. Settings → Background searches turns it off. Registry administrators' names are tried in both orders (the site's when its team page names the same person; else the market pack's `registry_name_order` first, the other only if that address is invalid).
- **Jobs and the worker** (`modules/jobs.py`, `modules/worker.py`, `modules/job_kinds.py`): every long run is a job on its source's lane (a discovery search per job, a site read per company, registry load/pull, public-data load/check, ANAF checks 100 a job). One loop per lane: a lane runs in order, lanes run side by side. One worker per database (a lease row): the dashboard runs it in-process, `harvey worker` and `harvey run` without the dashboard; CLI commands queue and wait (`--background` returns at once), running only their own lanes when no worker is up. Each lane has its own lease too, renewed from a thread, and a job's writes are fenced to the worker running it. Paid jobs reserve from their run's cap (and `spend.per_day_usd` / `per_month_usd`), and each page's cost is recorded before the next; a cap stop pauses the job and it resumes from its page. A hard kill mid-search re-fetches that search's pages since its last stop (within the cap). Transient errors retry with backoff (a later page's failure resumes from that page); out of credit pauses the lane. Only searches stop part-way on cancel/pause. The dashboard's Coadă page shows it all. Quiet hours don't apply to the worker.
- **Brain** (`brain.py`): Wraps `claude -p --output-format json --dangerously-skip-permissions` for headless Claude calls; records exact per-call token usage into `usage_events`
- **State** (`state.py`, `overlay/`): SQLite at `data/harvey.db`, schema 30, holding Harvey's own work only: `firm` (integer ids; `live` points a merged record at its survivor; kept counts), `firm_ident` (CUI, registration number, listing ids), `local_firm` (a firm no register describes), contacts, searches, sources, merges, web signals, and the Email module's prospects, campaigns, outbox, conversations, actions, usage. Registers are read in place (`registers.py`: each register module's adapter over its own file and the derived tables its load builds there: `firm_by_cui`, `firm_key`, `name_key`, `place_rank`, `current`, `filed`); a cohort's register conditions read the bitmaps the register counts job keeps per load (`register_set`, `overlay/bitmaps.py`), patched for firms added or merged since; every statement is named (`queries.py`, `overlay/queries.py`) and checked by `tests/test_query_plans.py`. Writes are `overlay/store.py`, page reads `overlay/reads.py`; kept counts are trigger-maintained (`overlay/triggers.py`) and `harvey doctor --recount` checks them. A new install creates schema 30; a schema 25 file (what the upgrade writes, frozen as released: `schema.UPGRADE_TARGET`, `PARTS`) is taken through `schema.STEPS` in place when opened, once, in one `BEGIN IMMEDIATE` transaction; an older file is never opened (`UpgradeNeeded`): the upgrade (`upgrade.py`: register indexes, ANAF rewrite, `overlay/migrate.py`, `overlay/verify.py`, switch) reads it; a file a newer Harvey made is refused by name (`NewerBuild`). A later schema change is a new step, never an edit of `PARTS`. Reloading the registry rebuilds the financials' `filed` from it.
- **Do-not-contact list** (`suppression.py`, table `suppression`): one list, keyed on an address, a domain (free mail refused) or a firm (`ro.cui:<n>`, else `firm:<live id>`), never a row id, so it survives merges, undo, re-finds and a firm removed and pulled again; soft undo. `suppression.check()` is the one reader (send gate, Writer, audience, exports, Handler), with `compliance.do_not_contact` read next to it, never copied. Triggers mirror address entries onto `contact.do_not_contact` and refuse clearing the flag while an entry stands; `undo_opt_out` lifts the person's own entries in the same write; `overlay/verify.py` compares the list when a source has one.
- **Cold-email rules at the gate** (`gate.pre_send_check`, `acknowledgement.py`, `email_rules.py`): cold email waits for the versioned acknowledgement (`harvey ack`, `POST /api/acknowledgement`); a first email to a personal address (any link names a person or reaches a natural-person business, a free-mail domain, or a local part that names someone) must carry the fixed source note, computed at staging and kept on `outbox.source_note`.
- **Unsubscribe and read coverage** (`unsubscribe.py`, Handler `_run_native`): the token in a cold email's mailto header (`outbox.unsub_token`) is matched first on every inbound message, empty bodies included, and in the spam/junk folder (`get_spam_unsubscribes`); each read goes back to the last complete one (`mail_read_at:<mailbox>`), and cold email waits (`mail_read_gap`, a Today card) while a read hasn't covered the gap. A stranger's message is always marked processed; one answering Harvey's thread or from a firm it wrote to becomes a needs-human conversation and stops that firm.
- **Usage tracking** (`usage.py`, `integrations/quota.py`): per-call attribution (agent/task/model/tokens) for Harvey's OWN Claude calls only — it never scans your other Claude Code sessions — plus a live subscription-quota gauge read the same way Claude Code's `/usage` does. The budget check throttles Harvey against real quota utilization so it always leaves headroom for your own interactive Claude use (`usage.max_daily_claude_percent`). Dashboard has a Usage tab; CLI has `harvey usage`.
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts
- **Contacts** (`contacts.py`, `overlay/schema.py`): every way to reach a company. `contact` holds one row per distinct email/phone/contact form/social profile (`(kind, value)` unique; phones in E.164 with `line_type` mobile|fixed|voip|toll_free|unknown; `status`; `do_not_contact`), `link` / `link_evidence` link them to firms many-to-many (a chain's shared phone is one contact linked N times, never a merge), through a `person` row and a `locations` row when known, with source, capability, evidence and `trust` confirmed|likely|unconfirmed (an unproven match is kept, never dropped). `firm_source`: every source a company was found in, every pull included. `search`: every search of a company or person by a module's capability; the finder skips a (record, module, capability) already searched unless forced (`harvey contacts --force`, the Search again box, `POST /api/contacts/find {"force": true}`) or older than the capability's manifest `refresh_days` (default never); errors don't count. Contact kinds are declared by modules (`[[contact_kinds]]`), a trigger refuses others. Every source writes through `modules/ingest.py` (listings become locations of a company they match, a register's administrators are read from the register and become Harvey's people once the finder works on them, ANAF/taxpayer/site phones and emails); `prospects` stays the Email module's list. Merge/undo re-point all of it through the record each row was found for.
- **Signals** (`signals.py`, `state.py`): every fact is a row, never a column. A web signal Harvey observes is an `obs` row (history kept on change) with its current `reading`; a register signal is read from the register in place (its `signal_source` says how), true/false/unknown by the register's own date. A new signal needs no migration, and confidence + provenance travel with the fact. The vocabulary in `signal` is governed (a write naming a code not in it is refused) and gated: only `status = 'confirmed'` signals are collected, and only a human sets that. `state.cohort(require, exclude)` does the set intersection in SQL.
- **Dashboard** (`dashboard.py` + `harvey/web/`): FastAPI JSON API plus plain HTML/CSS/JS served from disk — no build step. Edit `harvey/web/app.css` or `app.js` and reload the page.
- **DISCOVER** (`collectors/discover.py` for the providers, `modules/runs.py` for the run): the only stage that spends money, so cost discipline lives in the run, not the database — every run estimates first, checks a spend cap between batches, reads a kill switch, and flushes observations per batch. Providers are adapters behind one interface (`DiscoveryProvider`); the list is built from the manifests (`discover.PROVIDERS`), so a new source is one module folder (its `PROVIDERS` and `CAPABILITIES`, its `.env` keys in `secrets`), and a source no module claims is refused. Each search is a job; a stopped one resumes from its page cursor. Junk filtering matches *patterns*, not a literal blocklist. Entity resolution is the normalised domain, falling back to the provider's `external_id` for businesses with no website.

### Sub-Agents
- **Audience** (`agents/audience.py`): the verification and prospect-card helpers campaigns use. No agent searches for people: the contact finder does, as jobs.
- **Site reads** (`collectors/profile.py`, the `site_read` job on the website lane; `harvey profile`): collect the confirmed **buying signals** a company's own site answers, among them the tech stack (HubSpot, Shopify, Intercom, ~35 tools — zero extra requests) and hiring signals from careers pages. Hiring (`HIRING_ROLE` / `OPEN_ROLES`) goes further: when a company's own site links or embeds a Greenhouse, Lever, Ashby or Workable board, it reads that board's public JSON feed (one request, size-capped, never a guessed token) and records roles matching `icp.hiring_signals` (falls back to `icp.titles`) with department, location, posting date and URL, plus the open-roles count (`harvey/collectors/ats.py`). A board with no matching role records `HIRING_ROLE` = 0, so closed roles stop being current. The `titles` fallback is weak (it lists who you email, not roles that signal buying), so suggest users set `hiring_signals`.
- **Writer**: Generates 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40). Strict ban list on AI patterns. For a campaign it writes the templates on request (the wizard's *Generează secvența*) and a grounded first email per member that can be sent to, with the campaign's offer.
- **Sender**: Gmail, SMTP or Microsoft 365 (`mail_provider.NATIVE_PROVIDERS`; any other provider sends nothing): renders merge vars per member and STAGE each email into the `outbox` (pending_review → approved → sent) with a scheduled send time; each heartbeat drains due, approved items with human-like pacing, the daily cap, the deterministic pre-send gate, and stop-on-reply (a reply from anyone at a company stops the sequence to everyone there, in every campaign). `_blocked` checks, in order: the campaign is active and the member open; the person hasn't answered; no hand send; a first email isn't a second one; nobody at the firm answered; the do-not-contact list; the acknowledgement, address and source note; the gate; autopilot (and the campaign's always-review); the autopilot review; then the follow-up order, Precleared, and all of it again after the claim. Enforces daily send limits.
- **Handler**: Polls the provider for replies, dedups them, classifies intent, advances conversation stage, and queues auto-responses through the same outbox approval ladder (always `pending_review`; autopilot approves at the drain). It also detects bounces → marks the address invalid, cancels the prospect's queued sends, and trips a global kill switch past a bounce-rate threshold.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Order in a cycle
Read replies → drain the Outbox → the campaign cycle → the analyst when nothing was done. Finding people is never a heartbeat action: the contact finder runs as jobs.

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
harvey run                   # Start the heartbeat loop (email: replies, writing, sending)
harvey worker                # Run queued jobs without the dashboard (the dashboard runs its own)
harvey jobs                  # The queue per lane; cancel|retry|resume ID (--max-spend N), pause|resume-lane LANE
harvey dashboard             # Web UI at http://127.0.0.1:5555 (open the printed link)
harvey setup                 # Re-run setup wizard
harvey train <url>           # Train on a product website
harvey status                # Pipeline summary
harvey usage                 # Claude quota gauges + per-agent token usage (no dollar costs — subscription plans aren't billed per token)
harvey export                # Deliverable prospects → sequencer-ready CSV (prospects.csv)
harvey export --all          # Full raw list, no filters (the do-not-contact list is still left out)
harvey contacts [find|status] # The contact finder: find over filters (--county --locality --website --q --require --exclude --limit --max-spend --classify-max-spend --force --background; prints the run's time and cost at the end), or the backlog
harvey contacts rejudge      # Found websites nothing checked: counts (default); --run checks them (--checks N, --background); --undo BATCH puts back what a check took off
harvey contacts judge-test   # The classifier over the built-in fictional labelled set (--file F, --all, --task site|results, --profile ID keeps the result on it): accuracy, false links, misses, latency, tokens
harvey contacts verify       # Ask the classifier about contacts' links: yes / no / unsure (--contact ID or the filters, --max-spend N for its calls, --dry-run prices only, --review the unsure ones, --undo BATCH)
harvey contacts classifier   # The classifier's profiles and which task uses which; classifier use TASK PROFILE|off
harvey jobs report RUN       # How long a run took and what it cost: searches, address checks, classifier calls/tokens/USD
harvey gmail auth            # One-time Gmail OAuth (when provider: gmail)
harvey gmail test            # Verify the Gmail connection
harvey outbox                # Review queued emails; --approve <id> / --approve-all / --reject <id>
harvey signals               # The signal vocabulary; --confirm / --reject CODES (or 'free' / 'all')
harvey discover              # Find businesses; --providers / --estimate / --provider <key>[,<key>...] (several: one batch, one cap)
harvey registry load          # The whole ONRC trade registry into data/registry.db, once per dataset (downloads only missing files into data/onrc/<date>/ and keeps them; --dataset firme-DD-MM-YYYY; status shows what is loaded)
harvey registry search dentist BV  # Every firm in Romania: name, activity words, CAEN code, county (BV, Brașov), town; operating only unless --all-statuses; --json
harvey import-onrc dentist --county BV  # Shortcut: loads the latest dataset if needed, then pulls by area + activity (--dry-run counts). No activity = every operating company in the area (county or --locality required); --administrators all|none. Every authorised CAEN class is saved per company
harvey financials load       # The Ministry of Finance's yearly statements (turnover, employees, profit, equity, debts per CUI) into data/financials.db: the latest 3 years by default (--years N|all), downloads only what is missing into data/mf_financials/<year>/; status / check say whether data.gov.ro has a newer or revised year. Registry search/pull and import-onrc then take --min/--max-turnover, --min/--max-employees, --unknown include|exclude (turnover and headcount alike)
harvey taxpayers load        # The Ministry of Finance's yearly taxpayer file (address, phone, VAT, employer, micro-enterprise tax per CUI; ~870 MB) into data/taxpayers.db, download kept in data/mf_taxpayers/<year>/; status / check say whether a newer file is out. Registry search/pull and import-onrc then take --employer, --vat, --micro yes|no
harvey anaf check --missing  # ANAF's public web service for companies on file (VAT, e-Factura, inactive, status, legal form, address, phone): --missing / --all-on-file / --cui N; 100 CUIs a request, one request every 1.1 s across Harvey, answers kept 30 days in data/anaf.db; registry pull and import-onrc run it on their own for the firms they save while the module is on. status counts the answers kept
harvey search <words|code>   # Search companies on file locally: CAEN code (8623), activity in RO/EN words (stomatologie, dentist), name, town — all words must match, -word excludes; --json
harvey caen <code|words>     # CAEN reference: a code's name in every revision, or the codes for some words (not a filter)
harvey modules list          # Every module's manifest (harvey/modules/<set>/<id>/module.toml): id, set, kind, version, signals, default and current on/off; --json. Imports no module code
harvey modules enable|disable <id>  # Turn a module on or off for this install (modules.<id>.enabled; dashboard: Surse). Core can't go off; enabling needs its requires on, disabling is refused while an enabled module requires it. Off hides its signals, refuses its sources and presets, keeps the contact finder from using its searches and drops its unset .env keys from Settings (a key that holds a value stays editable); nothing is deleted
harvey modules settings|set <id> [KEY VALUE]  # A module's settings (modules.<id>.<key>); a verifier's price past its free tier is usd_<service> ("" clears it)
harvey caps list             # Every capability the manifests declare: module, capability, op, inputs, outputs, cost, lane, on/off by its module's switch; --json. A capability returns a draft and writes nothing: harvey/modules/ingest.py saves it (resolver, signal gate, provenance)
harvey duplicates            # Companies that may be on file twice (resolver.py), listed as groups (pending pairs followed to the live company, joined): --merge KEEP A B... (a whole group into one; a different address becomes a location of KEEP) / --separate A B... [--from C...] / --undo MERGED / --undo-group MERGED / --scan (a job on the duplicates lane). Merges at >= 0.85 are automatic, 0.6-0.85 wait for review, two CUIs never merge (a group holding two is refused), every merge can be undone. Phone, email (never free-mail) and website count by how many firms hold them: one firm's makes CUI-less holders its branches (auto for a website or up to 3 listings), 2-3 firms' is a question only, more is ignored; sure pairs chaining to two firms are held for review as one group
harvey remove ID...           # Take companies a register pull brought out of Harvey (the register keeps them; a pull adds them again). One Harvey worked on (searches, contacts or people it found, web signals, its own sources) stays, with only its pulls taken off; refused for one in a merge or named by outreach
harvey upgrade               # Old harvey.db (schema 24) to the slim database: register indexes, ANAF rewrite, migration, verification, switch (old files kept as harvey.db.v24 / anaf.db.old); resumable; --check reports only, --delete-old deletes the old files (typed confirmation), --data/--out for a dry run on a copy that writes nothing in the data folder (refused on a folder Harvey is running on)
harvey doctor --recount [--fix]  # Count every kept value again from its rows and list (or write) what differs; --registers counts the register signals over the firms in Harvey for the registers' current loads
harvey sending pause|resume  # Kill switch for all outbound; accept-gap lets cold email go after a mailbox read that couldn't reach back to the last one (the user types accept)
harvey ack                   # Read and confirm the cold-email acknowledgement (cold email waits for it); --status
harvey audiences [list|show|save|archive]  # Saved Contacts filters (save NAME --q/--county/... or --query "<qs>"); show counts what a campaign would take
harvey campaigns [list|show|new|start|pause|resume|stop|archive|members|add|remove|export]  # Campaigns you make (new --name --audience ID --note ... --sequence-file F; export ID: its record, --format csv|json)
harvey suppress [list|add|undo]  # The do-not-contact list: list (--csv, --all), add ADDRESS|DOMAIN (--firm ID), undo ID (--note)
harvey warmup [domain]       # Sending-domain readiness: SPF/DKIM/DMARC, age, blocklists, ramp, TrulyInbox warmup
harvey trulyinbox [connect|other]  # Warmup service: connect TrulyInbox, or record another service
```

### Common Issues
- **"command not found: harvey"**: Activate venv first: `source .venv/bin/activate`
- **"ModuleNotFoundError: No module named 'harvey'" after install (macOS)**: Python 3.13 silently ignores `.pth` files that carry the macOS hidden file flag, and some Macs propagate that flag into `.venv`. Run `harvey install` again (it auto-fixes by linking the package), or manually: `ln -s "$(pwd)/harvey" .venv/lib/python3.13/site-packages/harvey`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs `claude login` and an active Max subscription
- **Codex calls fail** (`brain.provider: codex`): the log line says why — `codex login` with ChatGPT (also when the login "has expired or is due for renewal": Harvey never renews it), install the supported Codex version (`harvey status --check` shows what's wrong without a model call; while Codex is unusable Harvey pauses whole cycles on purpose), pick a `brain.model` without code mode, or remove whatever Codex config layer is forcing tools/hooks/MCP back on. Never weaken Harvey's isolation checks to get past it.
- **"Instantly nu mai e suportat" / "Instantly is no longer supported"**: Harvey sends through Gmail, SMTP or Microsoft 365 only. Help them set one up (the API keys section above) and change `channels.email.provider` in harvey.local.yaml; campaigns already running in Instantly keep going there until they stop them in Instantly.
