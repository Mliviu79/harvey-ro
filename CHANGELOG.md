# Changelog

Notable changes to Harvey. Dates are release dates; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and versions
follow [semver](https://semver.org/), with the caveat that Harvey is pre-1.0 and
minor versions can still change behaviour.

## [Unreleased]

### Added

- **The agent prepares and follows campaigns; you start them.** Under an
  approved plan (free work: no budget needed) the agent can save an
  audience from a Contacts filter (`save_audience`: its id and what a
  campaign would take, counted, never an address), make a draft campaign
  through the wizard's own code (`create_campaign`: the offer, a sequence
  written to the email frameworks in the install's language, checked as
  the send check will check it: only the merge variables the sender fills,
  no banned phrase, the subject, length and link limits; the
  legitimate-interest note, at least 80 characters), edit its own drafts,
  count what one would take (`campaign_preview`), and ask you to start it
  (`request_start`): the plan card shows a start request with the
  audience counted, the sequence, the note, the sending account and what
  is still needed, and a button to the draft's Start on the Campaigns page.
  Starting stays yours. A campaign the agent made belongs to its plan
  (schema 35, `agent_campaign`), and tells it, as wakes with structured
  fields only: new members (the start's snapshot, a roll), a reply (the
  conversation's id, its intent, stage and status as Harvey's reply
  reader set them, whether they asked to stop or it needs you, whether an
  answer waits: never the text, the subject or the address), the campaign
  finishing, sending paused by the bounce rate, address checks held. The
  agent reads `campaign_status`, `campaigns` and `replies` (counts, and
  conversation ids with their fields), has the reply reader draft an
  answer with its short guidance (`draft_reply`: Harvey's own prompt and
  rules; it always waits for your own approval in the Outbox, even on
  autopilot and outside Approve all: autopilot is your consent to
  Harvey's answers, not to one the agent asked for; refused for an opt-out, an escalation, a no, a closed
  conversation, a reply answered already, one with an answer waiting, or
  someone on the do-not-contact list), adds to the
  do-not-contact list (`suppress`: a conversation's person, a contact or a
  firm; never an undo; its entries are by hand, by the agent, so the
  one-click Undo opt-out refuses while one stands and you lift it on the
  do-not-contact list) and pauses a campaign of its plan
  (`pause_campaign`; there is no resume). `writing_rules` gives it the
  frameworks, merge variables and limits. `harvey agent plans show` lists
  a plan's campaigns.
- **A run's report says why companies gave nothing.** The contact
  finder's result keeps a class for a company it found nothing for (its
  site unreachable or behind a bot challenge, read with nothing on it,
  waiting for a check or the classifier, not taken as the firm's, no
  website found, no search set up or no money for it, ...), and the run's
  report counts them (`nothing_why`, in `plan_status`, `jobs report`, and
  `harvey jobs report`): classes only, never a page's words.

- **Harvey wakes its agent to follow a plan while you are away.** When a
  run of an approved plan finishes (every job of it done, failed or
  cancelled), or at a time the agent scheduled with `schedule_wake` (at
  least 10 minutes ahead, never past the plan's end, three waiting at
  most), Harvey wakes the agent as a job on its own lane (`agent_wake` on
  the `agent` lane, with the worker's leases, fencing and retries). The
  wake is one Claude run in the plan's chat: it is given the prompt, its
  memory, the chat so far and what happened as structured data (never a
  page's or a reply's text), checks the plan's runs, carries on with the
  plan's next step within its budget, and writes a short report in the
  chat, marked with why it woke ("Harvey, after run … finished",
  "scheduled") and shown as new in the chat list until you open it; with
  nothing to say it answers `[SILENT]` and writes nothing. A plan's events
  are coalesced (one wake queued or running per plan, reading every event
  not yet seen), at most 12 wakes a plan a day, only while the plan is
  approved and open, within Harvey's share of your Claude plan (over it,
  the wakes wait and the lane rests); Codex refuses wakes. A logged-out CLI
  pauses the agent's lane, a spent plan limit rests it, with the reason on
  the Queue page and Today. While you are in the chat the wake waits for
  you; a chat whose process was given the conversation before a wake
  answered is given that answer before your next message. The plan card
  lists the scheduled wakes with Cancel; a wake you cancel on the Queue page
  doesn't come back for the same events; closing or cancelling a plan, or
  deleting its chat, drops its wakes. Other modules can add their own kind
  of event (a reply, new members) with no schema change. Quiet hours don't
  apply, and no wake can send anything.
- **The agent remembers.** Short notes it keeps with `remember` (300
  characters each, 2,200 together, refused past it) are given to every new
  conversation, chat or wake; playbooks it saves with `save_playbook`
  (named procedures, 4,000 characters, 50 at most) are listed there by
  name and read with `read_playbook`. Email addresses and phone numbers are
  masked as they are written, and memory lives in Harvey's database only.
  A note or playbook you wrote or edited is given to the agent as yours,
  and the agent can't drop or replace it.
  You see, edit and delete both on the Chat tab's **Memory** view and with
  `harvey agent memory` (`add`, `edit`, `forget`, `playbooks`, `playbook`,
  `delete-playbook`). Schema 34 adds `agent_event`, `agent_note`,
  `agent_playbook`, an answer's wake and a chat's unread count.

- **The agent works for you, under a plan you approve.** Asked to do
  something, Harvey's agent looks at the schema and the counts, estimates
  each step and proposes a plan: its goal, its steps, a budget sized from
  the estimates' high figures (0 for free work) and how long it stays open
  (72 hours by default, a week at most). It shows as a card under the
  answer with Approve (a dialog naming the budget) and Reject, and in
  `harvey agent plans` (`show`, `approve` with the budget typed back,
  `reject`, `cancel`, `close`); only you decide it, no tool of the agent's
  can. Under an approved plan of its own chat the agent can queue a Discover
  run, a registry pull, Find contacts or Verify contacts, each through the
  same code as the page's button (`harvey/launch.py`, now shared by the
  dashboard's endpoints and the agent): the same selection, estimate, run
  caps, module switches and day and month limits, the run's caps reserved
  from the plan's budget in one transaction so two actions can't both take
  the last of it. A plan's committed money is its finished runs' actual cost
  and its live runs' caps; the card shows it with what was spent, each run
  (to the Queue) and Cancel, which cancels its live runs (a run being queued
  as you cancel too); deleting the chat closes its open plans. The agent works on
  filters and on sets it saves (a filter, or one SELECT of firm ids, contact
  ids or registration numbers run under query()'s rules), resolved on
  Harvey's side: it gets a set's id and count, never its rows. Every action,
  refused ones too, is recorded (`agent_action`). Sending, approving email,
  campaigns, autopilot, the acknowledgement, the do-not-contact list,
  merges and settings are never tools. Schema 33 adds `agent_plan`,
  `agent_set` and `agent_action`.
- **No error text reaches the agent.** A job's or a run's error and a
  job's result (which quote what providers and pages answered) are left out
  of its queries; it sees each job's error class (spend limit, paused, lane
  paused, retrying, error) instead. Short text from websites and listings
  (names, addresses, roles, page addresses) stays readable and the schema
  marks it as data, never instructions.

- **Chats with Harvey's agent, read only.** A Chat tab (and `harvey agent
  chats`, `chat`, `ask`, `rename`, `delete`, `log`) keeps your conversations
  with a model about Harvey's data, the way claude.ai does: a list of chats
  (newest activity first, search by title, rename, delete), New chat, and
  any chat opened again and continued, its answer still streaming if you
  left it mid-answer. Each chat in use is one Claude CLI process (stream-json
  in and out) answering message after message, the answer written as it
  comes with each tool call shown as it runs; Stop, Retry on a failed or
  stopped answer, and the chat's context size with a hint to start afresh
  past 150,000 tokens. No job and no queue: a message is answered at once or
  refused at once (Harvey's share of the plan used, an answer still coming,
  the chat open in another Harvey process, three chats already answering).
  Up to three chats live at once per Harvey process; a chat's process
  closes after 10 minutes without a message, and the next message primes a
  new one with the conversation from Harvey's database (its oldest turns
  condensed to a note when it is long). Ctrl-C on the dashboard waits 5 s
  at most for an open answer.
  It reads only through Harvey's own MCP server (`python -m
  harvey.agent.tools`, the official `mcp` SDK), and in counts: `counts`
  answers how many firms and contacts any Contacts or Companies filter
  selects (with and without a website, with an email or a mobile, ...) in
  one call, as those pages count them; rows come back only as a sample (10,
  with their total); the schema gives the join rules (the `ro.cui` scheme,
  integer keys, the registry's CUI table); a query that reads a personal
  column (a name, an email, a phone, a profile) is refused, wherever it
  reads it, and what the views return has emails and phones masked. One SELECT runs under SQLite's
  authorizer on a read-only connection (no settings, email text, or text
  copied from websites and replies; 5 s). It can't act yet. Every tool call
  is recorded (`agent_tool_call`) and shown under its answer. Schemas 31 and
  32 add the chats' tables (messages already on file become one chat; the earlier chat jobs still waiting are cancelled). The `mcp` package is new: rebuild the Docker
  image (`docker compose build harvey`). Claude only: with Codex as the
  brain the chat says so.
- **Settings → Classifier explains itself, and everything it does is on
  the page.** The card says what the classifier decides (whether a website
  a search found is the firm's, which contacts on it are the firm's), when
  it is asked (Find contacts and a Discover run's follow-up, Search again,
  the found-websites check, Verify on Contacts), that a model's yes makes a
  site likely at most, that an off task leaves the site unlinked with
  nothing guessed, and that it runs only inside the caps. Each task shows
  what it is for, when it is asked and what off means, with the profile
  answering it, changeable there. The profiles sit side by side, sorted by
  name, each with its endpoint, model, key name and whether `.env` holds
  it, price, extra request fields, pace and its measured counts (or "not
  tested"), with Use for, Test (counted and priced, then run under a cap,
  the counts kept on the profile), Edit (every field, the questions' texts
  and bars per task, the extra fields as JSON, DeepInfra's price list),
  Duplicate, Hide / Show again or Remove, and the DeepInfra account box.
  The caps and spend, and the DeepInfra key's scopes (`billing:read` and
  `metrics` added by hand), are on the card too.
- **Find contacts prices the classifier and reports the run.** Next to the
  search cap: how many classifier questions the run may ask, an estimate
  of what they may cost, and a classifier cap of the run's own. Once started, the
  run's report follows where it was started: its jobs, how long it took,
  and the cost split into searches, address checks and the classifier's
  calls, tokens and USD. The Queue shows an opened run's report and lets
  its classifier cap be raised; each job row shows its classifier calls and
  time; Today lists the newest runs with theirs (`GET /api/jobs/batches`).
- **Verify with the classifier, on Contacts.** Any contacts can be ticked
  (phones, emails, forms, profiles) and verified, or one from its Details:
  priced first, run under a cap, each answer (yes, no, unsure) on its row,
  a no undone with its run. The filter *Classifier's verdict* lists the
  unsure ones for review (`GET /api/contacts/verify/verdicts` gives the
  rows' standing verdicts).
- **Classifier profiles.** Settings → Classifier is now a list of profiles,
  each one complete setup (kind, endpoint, model, the name of the key it
  uses, extra request fields, pace, timeout, prices, the tasks it answers,
  and its questions' texts and bars), and each task (search results,
  website, contacts on the site) is set to a profile or off. The card says
  what each task is for and when it is asked. Built-in profiles ship
  read-only with the counts Harvey's judge test measured on 200
  hand-labelled firms (the developers' test, not shipped), side by side:
  four DeepInfra models (Qwen3.8 27B with thinking off through the extra
  request field `reasoning_effort: none`), TypeSafe's Jev, and a local
  Ollama model for your own hardware. Editing a built-in saves a copy of
  your own; built-ins can be hidden and shown again; your own can be
  added, edited and removed, and say "not tested" until a test run
  records results on them. **Test** runs the judge test on the built-in
  fictional cases or your own labelled file, counted and priced first, as
  a job under a USD cap you type, and keeps its counts on the profile.
  The older single setup becomes a profile of its own. The DeepInfra
  key's scopes are documented: `billing:read` and `metrics` are added by
  hand.
- **A classifier budget per run, and run reports.** A Find contacts run,
  a Discover run's follow-up and a test run take a USD cap for the
  classifier (`classify_max_spend`, `--classify-max-spend`), checked
  before every call against the call's estimate; its cost is the run's
  spend, so `spend.per_day_usd` and `per_month_usd` count it too.
  Reached, a firm's site waits unlinked, as with no classifier. Every run
  reports how long it took and what it cost, split into paid searches,
  address checks and the classifier's calls, tokens and USD
  (`GET /api/jobs/batches/{id}/report`, `harvey jobs report RUN`, at the
  end of `harvey contacts find` and `rejudge`).
- **Verify a contact with the classifier** (`harvey contacts verify`,
  `POST /api/contacts/verify`): yes, no or unsure about one contact at a
  company, priced first and under a cap. Yes notes it, no drops the
  link's website evidence to unconfirmed (official sources' never;
  `--undo BATCH` puts it back), unsure lists it for review. Nothing is
  deleted (schema 30: `contact_check`).

- **A small model that tells a firm's own website from the rest.** The
  contact finder weighs every search result, not just the best-named one.
  Code rules out directories, listings, profiles of the firm on other sites
  and institutions by their address, before any visit; a register fact on
  the pages decides the rest when it can (the firm's CUI or registration
  number with no other firm's beside it, an official email or phone next
  to its name that few firms share); everything else goes to a small, fast
  yes/no classifier you choose (Settings → Classifier: any
  OpenAI-compatible endpoint, local or hosted, a small Claude model, or
  Harvey's own). Each candidate is asked alone whether it is the firm's
  site, and exactly one yes is taken; then each contact read there is
  asked whether it is the firm's own (the firm's official phones and
  emails are always kept). It can only
  make a site likely, never confirmed. `harvey contacts
  judge-test` scores it over a built-in fictional labelled set or your own
  file.
- **Decision models as a classifier provider** (`decision`): any endpoint
  in the System One format (a state and atomic questions, each answered
  with a probability), called over HTTP at the full endpoint URL you enter:
  it is for TypeSafe's Jev (`https://api.typesafe.ai/v1/systemone`, model
  `jev-latest`, key `TYPESAFE_API_KEY`). It gets questions of its own,
  combined in code: for a site, does it name the firm (at least 0.5),
  present the firm's own business (at least 0.4), is it a directory or an
  institution (each under 0.7), the bars as measured; for each contact on
  the chosen site, whose it is, kept at the contacts bar (`classify_yes`,
  0.7, a setting). The company panel shows the probability.
- **What the classifier costs, and a USD budget.** Every call records its
  cost: the reply's own when it says one, else tokens times the price you
  enter, else, on an OpenAI-compatible DeepInfra endpoint, the model's
  price on DeepInfra's public price list (a decision model has only the
  price you enter); unknown is recorded at $0 and said so. USD caps a day and a month
  (Settings → Classifier) sit next to the call caps: reached, the firms
  wait unlinked; with the price unknown, only the call caps hold. The card
  shows calls and spend today and this month, cost per site checked and
  tokens a call; on DeepInfra it lists the models with their prices to
  choose from, and an Account box reads the key's usage, limit and balance
  on the server. `harvey contacts status` and the judge test print spend.
  You choose provider, model, prices and budget: none is filled in for you.
- **A check of the websites found before** (`harvey contacts rejudge`,
  Today, Settings → Classifier): counted first, run when you start it (on
  a lane of its own, behind no Find contacts run), one that isn't the
  firm's taken off with exactly what its read left, and `--undo BATCH` to
  put it back. One it takes only gains: the contacts chosen are made surer,
  and nothing linked is removed or made less sure. A site that doesn't answer is taken off only after three
  days of trying. The company panel says why a found website was taken.

- **The cold-email acknowledgement on the dashboard.** Settings → Sending
  shows whether it was given (version, when, from where) and opens a dialog
  with the whole text, in the page's language, where you type `accept`;
  starting your first campaign opens the same dialog instead of a browser
  prompt, and starts it once confirmed. Today's card sends you there. The
  text says that the line naming an address's source isn't all GDPR
  art. 14 asks for.
- **A campaign's record (evidență).** Its legitimate-interest note, the
  acknowledgement it started under (version and time), every address with
  how it joined (start, roll, by hand) and why it left, every email sent
  (the answers in its conversations too) and the opt-outs that reach its
  addresses (their own, their domain's or their firm's), as CSV or JSON: **Evidența** in the
  campaign's panel, `harvey campaigns export ID`, or
  `/api/campaigns/{id}/record.csv|json`. The note can be edited in the
  panel while the campaign is a draft; once it has started the note stays
  the one it started under, whatever asks to change it.
- **A line under answers autopilot sent.** An answer that went out with
  nobody reading it carries `compliance.ai_disclosure_line` (*Acest
  răspuns a fost scris cu ajutorul unui asistent AI.* by default; blank
  turns it off). Cold email and answers you approved go as they are.
- **Your privacy page after the source line.** `compliance.privacy_url` is
  added after the line saying where a personal address was found (*Cine
  suntem și cum vă puteți opune: …*), in its first email only. It counts as
  that email's one link: a first email that has another (a booking link,
  say) waits for you in the Outbox, from staging, instead of failing at
  send.

- **Campaigns you make.** The Campaigns page has a new-campaign wizard: an
  audience (a saved Contacts filter, or the page's current one), the offer
  and its sequence (write it, or have it generated and edit it), the
  sending account, "always review", and the legitimate-interest note (80
  characters at least, never sent; your record). Starting it needs the
  note, a sequence and the cold-email acknowledgement, and takes the
  audience as it is then; a rolling campaign takes new matches at most
  hourly. Each campaign has its members and their progress, pause, resume,
  stop and archive; once started, its note stays as it was. A campaign that doesn't roll finishes when no member
  is left open, also when its audience matched no one. Each cycle writes
  at most 8 first emails across all campaigns, and only while the first
  emails waiting to go are fewer than two days of today's cold cap, so
  drafting (a model call each) never runs ahead of sending. A paused
  campaign's approved emails wait without holding up other campaigns'.
  `harvey campaigns` does the same from the terminal. Campaigns need
  Gmail, SMTP or Microsoft 365.
- **Audiences.** Contacts → Actions → *Salvează ca public* keeps the page's
  filters under a name (Campaigns → Publicuri, `harvey audiences`), read by
  the same code as the page, so a campaign gets exactly what the page
  showed. Ticked email addresses can be added to a campaign (*Adaugă în
  campanie…*).
- **One set of rules for every way into a campaign.** The start, a roll
  and adding by hand all take an address only when it is verified,
  catch-all (with `send_to_risky`) or not yet checked, linked to a live
  firm with trust confirmed or likely, not on the do-not-contact list, not
  in another active campaign's sequence or sent cold email in the last 90
  days (or marked contacted with no cold email on file, as an earlier
  release's Instantly campaigns or a hand send leave it), and neither its person nor anyone at its firm has answered;
  the preview counts what each rule
  left out. Unchecked addresses are checked by a verifier before anything
  is written for them (a roll's, at most 50 a campaign a day).
- **Sending accounts and AI profiles, the hook.** A campaign names the
  mailbox it sends from and the AI profile that writes for it (one per
  mailbox). Today there is one of each, from your settings; caps, warmup
  and reading replies still come from the settings.
- **Replies land in their campaign.** A reply is attributed through the
  email it answers (else the last cold email to that address), and the
  campaign shows its replies. A reply anywhere ends that address's places
  in every campaign, and its colleagues' at the same firm.
- **One do-not-contact list.** Unsubscribes, opt-outs, objections and the
  entries you add are kept in one list keyed on the address, the domain or
  the firm's tax code, so an entry holds through merges, their undo, an
  address found again and a firm removed and pulled again. The send gate,
  the Writer, the audience, the exports and the reply handler all check it
  (`harvey suppress`, `/api/suppression`, a CSV to keep beside an export).
  What was opted out before is carried over. Undoing is soft and keeps the
  history.
- **Unsubscribe header.** Every cold email carries a mailto
  `List-Unsubscribe` header with a token (Gmail, SMTP, Microsoft 365, the
  latter created from MIME); the unsubscribe it brings back is honoured at
  once, even with an empty body or in the spam folder. No one-click header:
  that needs a public HTTPS address.
- **The cold-email acknowledgement.** Cold email waits until you confirm,
  once per version of the text, what sending it to firms means
  (`harvey ack`). Today says when cold email is waiting for it.
- **The source line for personal addresses.** A first email to a named
  person's address, a free-mail address or a PFA/II/IF gets a fixed line
  saying where the address was found, added at staging; the gate refuses
  one without it.

- **The contact finder reads a site the way the old prospecting did.** It
  follows the homepage's own menu to the team, about and contact pages
  (meet-the-doctors, echipa, medici, despre noi...), six pages at most and
  ten requests, instead of the first of a fixed list of paths that answered
  (and which missed pages under 500 characters). Only a homepage whose menu
  links to fewer than two such pages has the usual paths tried. On those
  pages it finds people in team cards, schema.org markup and sentences
  ("owned and operated by", "fondată de", "fondatorul clinicii"), the
  likeliest owner first, CSS team cards only as a fallback, and records
  `LIKELY_OWNER` when it is confirmed. The addresses the site publishes,
  obfuscated ones included, teach the domain's email pattern: a person's
  own published address is linked to them with no check spent, everyone
  else's is built the same way, and with no evidence a small company's
  address is built as `first@` (your ICP's size, its size on file, its
  employee count from the financial statements, or a local listing).
  A built address no verifier confirmed (a guess, or a catch-all's
  "risky") is kept no surer than "likely", and an address built earlier
  is never taken for one the site published, nor teaches the pattern.
  Relative menu links follow the page the homepage landed on (a redirect
  to `/ro/`), and the same page in another language is read once. A site
  the finder read before this gets the deeper read once in the
  background, after the companies with no contact, whatever contacts the
  first read found (its read didn't look for the owner).
- **A site read's other requests have a budget of their own.** The About
  and press pages the startup signals look for, the page a search found,
  `robots.txt`, `sitemap.xml`, the careers page and its job board's feed
  share fourteen requests per read, apart from the menu read's ten, so they
  can never starve the pages contacts come from. A signal whose page the
  budget couldn't pay for stays unanswered (never a "no"), and a page is
  fetched once per read: the About page the startup signals found is the
  menu's About page too.
- **Published addresses are linked with no spend cap.** A person's own
  address on the company's site is linked to them by every finder run,
  including the background walk with no `spend.per_day_usd`; addresses are
  still built and checked only inside a cap.
- **The Writer gets what the record knows.** Prospects made from contacts
  carry the company's town, its activity, and the latest tools on its site,
  open roles, ads, booking, website agency, employees and rating as notes,
  and the Writer reads the same facts from the record when it writes to one
  person (they used to come only from the removed prospecting stage).

- **A Registry page.** The whole trade registry, every firm in Romania
  operating or not, browsed with each firm's latest financial statements:
  the registry source's own filters (activity, county, town, turnover and
  employee bounds, employer, VAT, micro, and a new status filter for every
  registration), sorted by name, turnover, employees, net profit or
  registration date, with an exact total and page numbers. The first page
  of a filter is ordered once and kept, so later pages and jumps are
  instant; the rankings are computed in the background when the page is
  first opened, one at a time and after any page being asked for, and a
  registry gets an index for the registration-date sort (built with each
  load, or added once to one loaded before). A search of only excluded
  words ("-auto") lists every firm but those, and a pull of it takes the
  same firms; only joining words ("si") count as no words. Each firm says
  whether it is already in Harvey, by CUI or, for a firm with none, by its
  registration number. Its panel
  shows every financial year side by side, the registry's record and
  administrators, the taxpayer file and ANAF's answer. The ticked firms or
  the whole list are pulled into Harvey as a registry pull, counted first.
- **Possible duplicates as groups.** The review list joins every pair still
  to decide into groups, each side followed to the company it stands for now:
  a pair made stale by a later merge is gone, and four or six records of one
  business are one decision. Pick the record to keep, untick any that aren't
  the same, and merge the rest in one go (or take some out, or call them all
  different). A group holding two CUIs shows the other firm but never merges
  it. Each merge in a group can be undone alone, or the whole group at once
  (`harvey duplicates --merge KEEP A B...`, `--separate A B... --from C...`,
  `--undo-group MERGED`). **Look again** / `harvey duplicates --scan` scores every
  company on file again, as a job on the duplicates lane.
- **Branches become locations.** Merging companies at different addresses or
  towns keeps each merged one's place as a location of the company kept, its
  contacts reached through it; an undo takes it away again. A listing without
  a CUI whose phone, email or website only one registered firm has is that
  firm's branch: merged on its own for a website or up to three listings,
  offered as one group beyond that. One that two or three firms share puts a
  listing up for review against each, never merged; more firms than that, a
  chain's switchboard, counts for nothing.
- **Email as a matching key.** An email linked to two companies counts like a
  shared phone (0.8); an address on a free-mail provider (gmail.com, yahoo.ro
  and the like) never counts. Existing email links become keys on upgrade.

- **Companies page: sort, filters, pages and the whole record.** Sort by
  newest found, name, town and county, turnover, employees, contacts or last
  searched, either way; filter by county, town, activity (CAEN codes or
  words), website, has a contact (any, phone, mobile, email, none), source,
  signals, turnover and employees, and the search box (words or a CUI): the
  Contacts page's filter language, so the Actions menu's export, ANAF check
  and Find contacts take the same list. Exact totals, page numbers, a page
  box and the page size. Unfiltered pages read partial indexes on live
  companies (migration v23: newest, name, place), so page 10,000 takes
  about 20 ms of SQL; any other list is ordered once and kept up to five
  minutes (dropped at once when a company in it gains a contact, a search or
  a source the list depends on), so every page after the first is a slice.
  A number no company has as its CUI is searched as a word, as before. The company panel adds
  every CUI, every financial year, the trade registry's row (classes,
  administrators), ANAF's kept answer, the taxpayer file, each person's own
  contacts, and Find contacts / Search again for that company.
- **Discover finds websites for what it saves with none.** On by default
  ("Also find websites, up to $X"; `--websites-max-spend`,
  `--no-find-websites`; `find_websites` / `websites_max_spend` in the API):
  free searches always run, paid ones draw only from that amount ($0 when
  the API or CLI names none; the Discover tab's field starts at $1.00),
  kept apart from the searches' cap inside the same batch, and the estimate
  and the run's progress show it. A run queues at most what its amount
  pays for plus 500 free lookups; the rest wait for the background.
- **Background website searches inside free allowances.** Search modules
  declare the free allowance they renew each minute, hour, day or month
  (`[[free_allowances]]`: TinyFish 30 a minute and 500 an hour, Brave 1,000
  a month, counted over the last 30 days). Harvey counts every request itself, and the backlog's walk takes
  only its share (half by default) for companies with no website; paid
  searches still need `spend.per_day_usd`. Settings → Background searches
  turns it off or changes the share; the Contacts page shows each
  allowance's use.
- **Brave Search, as a website finder.** `BRAVE_API_KEY`: the contact
  finder looks up a company's website by its name and town ($5 per 1,000
  searches, $5 free a month). Never a Discover source (its terms forbid
  building a database of its results), and its results are never stored.
- **Names in either order.** A person's email address no longer assumes a
  name order: the order the company's own site confirms, else the likelier
  one (the market pack's `registry_name_order`: family name first for the
  Romanian registry) and, only if that address is invalid, the other. A
  hyphenated given name is kept whole in greetings and checked whole first.

- **Discovery takes every result.** A search is no longer one page of 100:
  each source is paged until it has nothing more (DataForSEO Business
  Listings 1,000 a page with its `offset_token`, Serper and Scavio until
  Google has no next page, DataForSEO SERP and Maps at their deepest, 200
  and 700). The only stop is the spend cap, checked before each page, and
  the stop button; a stopped run says how many results were left and what
  they would cost. For Business Listings the estimate counts the businesses
  first (about a cent a search), so its price is exact. A run whose
  estimate is over the cap now runs up to the cap instead of refusing.
  `--limit` and `--depth` default to everything.
- **Romanian companies' annual financial statements.** `harvey financials load`
  downloads the Ministry of Finance's yearly statement files from data.gov.ro
  (the latest three published years by default; only what is missing) into a
  local `data/financials.db`. Companies on file get turnover, employees, net
  result, equity, debts, total assets, turnover growth, the form filed and
  whether they filed, one reading per year, once those signals are confirmed.
  Registry search, pull and `import-onrc` filter on turnover and employees
  before anything is pulled (`--unknown include` keeps firms whose figure is
  missing). "Did not file" is said only for a year whose final, revised
  release is loaded, and never for a struck-off firm. Downloads are checked:
  only https://data.gov.ro, redirects included, never outside
  `data/mf_financials/`, and refused when the disk hasn't room. The company
  export's figures all come from one year, named in `financial_year`. The dashboard's Sources page shows what is
  loaded and loads or checks for a newer year; automatic updates are opt-in.
- **ANAF company data.** The new `anaf` module asks ANAF's free public web
  service about companies on file by CUI: VAT registration and since when,
  VAT on cash basis, split VAT, fiscal inactivity (an explicit "no" when ANAF
  says so), e-Factura, registration status, legal form, registered office and
  fiscal domicile, and phone. Up to 100 CUIs a request, one request every
  1.1 s through one lane shared across Harvey (and across processes on one
  machine), retries only on HTTP 429 or 5xx, answers kept 30 days.
  `harvey anaf check --missing|--all-on-file|--cui N`, a Check at ANAF action
  on the Companies tab and the company panel, and a background check of every
  firm a registry pull saves while the module is on and at least one of its
  signals is confirmed (nothing is asked, and no phone filled, before that).
  A busy or unreachable ANAF stops the run instead of being asked batch
  after batch.
- **The Ministry of Finance's taxpayer file.** `harvey taxpayers load` loads
  the yearly identification file (about 870 MB) into `data/taxpayers.db`:
  address, phone, VAT, whether a company is an employer (`HAS_EMPLOYEES_TAX`)
  or on the micro-enterprise tax (`MICRO_TAX_REGIME`). Companies with a CUI
  get its readings right after a registry pull, dated the day it was
  published so ANAF's newer answer stays current.
- **Findings accumulate, official first.** Every phone and address a source
  gives is kept with its module, date and confidence; the company panel shows
  each with who gave it, official registers first, and the latest official
  facts. Modules declare `official = true`, and `[[lanes]]` paces
  (`min_interval_seconds`) are now enforced (`harvey/modules/lanes.py`).
- **Yearly signals.** A signal can be keyed by year (`keyed_by = "year"`):
  its current reading is the latest year on file, and a cohort range can name
  another year.
- **Scavio discovery providers** (`scavio_maps`, `scavio_search`). Google Maps
  listings around each city, with phone, website, category, rating and review
  count, and Google organic results with rank, both from one
  `SCAVIO_API_KEY`. Maps is 1 credit per 20 businesses and Search 1 credit per
  10 results; estimates use the $0.01 pay-as-you-go credit price. Off unless
  you pick one in `harvey discover` or the dashboard. Closes #28.
- **TrulyInbox for warmup.** Harvey now recommends TrulyInbox for warming and
  keeping sending mailboxes healthy, and reads it: `harvey trulyinbox connect`
  (or the new warmup step on the dashboard's setup checklist) checks a key and
  saves it, and `harvey warmup` then says whether the sending mailbox is
  actually warmed: enrolled, signed in, warmed for `min_days`, and under 5%
  spam in its warmup mail. Read-only; it never changes anything in TrulyInbox.
  `harvey trulyinbox other` records that you use another warmup service.
- **A do-not-contact list, enforced at send time.** `compliance.do_not_contact`
  takes addresses and domains Harvey must never email: competitors, your
  existing customers, anyone under a legal hold, a partner you agreed not to
  approach. A domain covers its subdomains, matched on a label boundary, so
  `acme.example` blocks `mail.acme.example` and leaves `notacme.example`
  alone — a plain suffix test would catch the wrong company. The check runs
  in `pre_send_check`, so it is the last thing before an email leaves and
  holds for one a human already approved in the Outbox. It applies to replies as well as cold mail: a list that lets
  automated replies through is not a do-not-contact list, and the operator
  can still answer by hand. Entries are normalised when the config loads, so
  ` @Acme.EXAMPLE `, a pasted `https://www.acme.example/about`,
  `Jane Doe <jane@acme.example>` (that address) and `acme.example` behave
  alike; an entry that names nothing is a config error, not a silent no-op.
  An edit counts at once in a running Harvey. A listed person who writes
  back goes to you without a draft, and `harvey export` leaves them out.

- **Discovery skips do-not-contact companies too.** The same
  `compliance.do_not_contact` list is now applied while discovering, before a
  result becomes a company, so a forbidden business never enters the pool: it
  costs nothing to enrich, never has its site read, and stops turning up in
  cohorts for someone to spot by eye. The send-time gate is unchanged and
  still the last word — only it can stop a send — but on its own it let a
  competitor through discovery and charged you for the enrichment first.
  Discovery has a domain, not an address, so only domain entries apply here:
  `jane@acme.example` bars one mailbox, and acme.example stays discoverable
  because a colleague there may still be worth reaching. Skipped companies
  are counted separately from junk, and `harvey discover` prints the count
  only when it isn't zero — junk asks whether a result is a business at all,
  this asks whether you are allowed to approach one, and burying the second
  in the first is how a list that has grown too broad goes unnoticed.

- **TinyFish as a free discovery source** (from #95). `harvey discover
  --provider tinyfish` (or *TinyFish* on the Discover tab) finds businesses
  by phrase with a free `TINYFISH_API_KEY`, no card, and runs the `yc`,
  `hiring`, `accelerator` and `funding` presets. Harvey paces it to its
  published limits, 30 searches a minute and 500 an hour, and a run that
  uses up the hour (or is refused the key) stops and says why instead of
  failing every remaining phrase. It records no
  `SERP_RANK`: TinyFish's ordering isn't Google's and its search takes no
  location, so rank signals still come from Serper or DataForSEO SERP, and
  a TinyFish run never marks a business they ranked as gone. Scout also
  searches through TinyFish when the key is set, after Serper.

- **Open roles from the job board a company actually uses.** When a
  company's own site links or embeds a Greenhouse, Lever, Ashby or Workable
  board, profiling reads that board's public JSON feed (one request, capped
  at 8 MB, never a guessed token) instead of guessing from careers-page
  HTML, which on most startup sites has no roles in it at all because a
  script loads them. `HIRING_ROLE` now records the roles matching
  `icp.hiring_signals` (else `icp.titles`) with department, location,
  posting date and the posting's URL; the new `OPEN_ROLES` signal counts the
  board's open roles. When the board lists no matching role, `HIRING_ROLE`
  is recorded as 0, so a role that has closed stops counting in cohorts and
  shows as "no" to the reviewer. Matching folds VP/Vice President, CMO/Chief
  Marketing Officer and similar, and "Assistant to the CEO" is not a CEO
  role. Pages linking three or more boards, none named like the site (an
  agency or a VC portfolio page), are attributed to nobody. Sites without a
  board still get the careers-page read.
  The new `hiring` discovery preset (`harvey discover --preset hiring`, or
  *Look for: Companies hiring for your roles*) searches through Serper or
  DataForSEO SERP for companies whose own careers pages list your roles.
  `skills/signal_playbook.md` has a short section on using a posting as the
  hook: cite the role as posted, never guess salary or headcount, don't
  imply inside knowledge, mind the posting date.

- **Find companies by where they rank in a city.** DataForSEO SERP now also
  records who holds the local 3-pack (`IN_LOCAL_PACK`, slot 1-3, ads
  excluded), and a new **DataForSEO Google Maps** provider records each
  business's map position (`MAPS_RANK`) with its rating, category and
  claimed status, keeping businesses with no website by their place_id
  (`NO_WEBSITE`). Both search from the geocoded city centre. Every rank is
  stored with the search phrase that produced it ("#14 for 'roofer Denver,
  CO'"), one per business per phrase, keeping the best. When a later search
  for that phrase no longer finds a business, Harvey records that, so an old
  rank or a lost pack slot doesn't keep matching. Websiteless businesses are
  keyed on Google's cid, so the pack and Maps resolve to one company. `icp.keyword_variants`
  adds search phrases per industry. The cohort builder takes numeric ranges
  (Signals tab, `/api/cohort` `ranges`, `/api/cohort.csv?ranges=SERP_RANK:11:30`),
  so "organic rank 11-30, not in the local pack" is one query. The signal
  playbook has rank-based angles, worded as "when we searched from downtown
  Denver", never as an absolute position.

- **Cold email in each recipient's office hours** (from #59 by
  gabrielaoimevbore-beep; `channels.email.send_window`, #53). Cold email goes out only between `start` and `end` (08:00 to 17:00 on
  weekdays by default) in the recipient's own time zone. The zone is worked
  out once and stored on the prospect, from the company's location (US
  states, Canadian provinces, Australian states, countries with one zone or
  one main zone, and the coordinates discovery already cached), the country
  Precleared returns when that check is on, or `prospect_country`. A code
  that is a state and a country too ("Mumbai, IN", "Adelaide, SA") is
  settled by those; with nothing else it reads as a US state. Anyone Harvey
  can't place gets the window in `usage.quiet_hours.timezone`. Follow-ups
  keep their `delay_days` and move into the window, an email approved after
  hours waits for the next one, and replies are never held by the window.
  With `recipient_timezone` on, quiet hours still pause prospecting,
  writing and replies, but wake for cold email that can go now in its
  recipient's window (not paused, under the caps): the heartbeat reads
  replies, then sends it. `weekdays_only` means Monday to Friday. Off unless
  set; Gmail, Microsoft and SMTP only.

- **A built-in skill for selling to YC startups.** The `yc` discovery preset
  (`harvey discover --preset yc`, or *Look for: YC startups* on Discover)
  searches your industries, through Serper or DataForSEO SERP, for companies
  whose own site says they're backed by Y Combinator. The new `YC_BACKED`
  signal tags them from their homepage or About page (a reference to their
  own YC page, or a line where they say it, never customer or audience
  lines), with the batch when it's stated (W24, S23). Many YC companies
  don't say it on their site, so it finds the ones that do. For those prospects the writer and reply handler use the new
  `skills/selling_to_startups.md` playbook: who reads, timing around the
  batch and Demo Day, what not to do. Harvey never reads ycombinator.com,
  whose terms forbid scraping it or collecting founders' contact details.
- **Startups beyond YC: accelerators and recent raises.** Two more presets:
  `accelerator` (*Accelerator startups*) finds companies whose own site says
  Techstars, 500 Global, Antler, Entrepreneur First, South Park Commons, Neo,
  AI Grant, a16z speedrun, Pioneer or Plug and Play backed them, and
  `funding` (*Recently funded*) finds companies announcing their own raise in
  the last 18 months, with news sites, press wires and funding databases
  excluded and filtered as junk. Two new signals read it off their own
  pages: `ACCELERATOR` (program and cohort, from a sentence where the company
  says it or a "Backed by" logo wall) and `FUNDING_ROUND` (round, amount and
  date, from the homepage, the search hit's own page, About or the press
  page; the date from the sentence or the page's publish date). Both are
  anchored like `YC_BACKED`: "we help startups raise", customer or
  portfolio lines, an investor's round in another company, and money that
  isn't a raise (loans closed, verdicts won, money raised for a charity)
  don't count. The founder playbook now applies to YC- and
  accelerator-backed companies and to raises dated within 18 months, and has
  a section on writing to recently funded companies; an older or undated
  raise is never written to as news.
- **Approval or autopilot, visible and switchable.** The Outbox now says
  which sending mode you're in, and Settings → Sending explains both,
  lists the domains counted as yours and switches them. Turning autopilot
  on takes typing `autopilot`; turning it off is one click and works at
  once, mid-cycle too (every step that queues or sends re-reads the mode):
  emails autopilot approved that haven't gone out come back to the Outbox,
  and the sender won't send one while approval is on. Only the
  `require_approval` line of your config changes (comments kept), and a
  running Harvey picks it up next cycle. Each outbox row records who
  approved it (`approved_by`: you, autopilot, or earlier for rows from
  before this). The README has a plain table of what each mode does.
- **`harvey warmup [domain]`**: checks whether a sending domain is ready for
  cold email. It covers MX, SPF (`+all`, duplicates, top-level lookups), DKIM,
  DMARC, domain age, domain and SMTP-IP blocklists (a list that refuses the
  query is reported as such, not as a listing), warmup progress and bounce
  rate, then gives a READY / NOT READY verdict. It's read-only, and a DNS
  timeout is reported as unknown, never as a missing record.
- **A cold-email ramp for new domains** (`channels.email.warmup`). From
  `start_date`, no cold email goes out for `min_days`; then the cold cap starts
  at `start_sends` and climbs by `daily_increase` a day up to
  `max_daily_sends`, counted per local day. Replies are never
  held. Off unless `start_date` is set. Both based on #18 by Tariz Shahid.
- **A postal address and opt-out line under every cold email** (CAN-SPAM).
  Set `compliance.postal_address` in `harvey.yaml`; `compliance.opt_out_line`
  defaults to *Not interested? Reply "no thanks" and I won't email again.*
  Both are added when the email sends, never
  to replies, and the Outbox shows them under each draft; "Open in Gmail"
  includes them too. Without an address (or with the opt-out line turned
  off), cold email still sends, and `harvey run`, `harvey status` and the
  Outbox tab warn until it's set. The opt-out line must ask for a reply
  Harvey recognises and can't contain a link. Based on #13 by Camilo Cruz.
- **The Today page and `harvey status` say when Precleared is holding cold
  email.** When the check can't be made (its free checks for the day are used
  up, the key is out of checks, was rejected or is being rate limited, or
  Precleared can't be reached), cold email to new addresses waits and replies
  still go out, but this used to reach only the log, so cold email just went
  out slower. Harvey now records the hold, and the Today page and
  `harvey status` say how many cold emails are waiting and what fixes it: a
  free key, a pack for that key (the checkout link from Precleared's answer),
  or nothing but time. The next fresh answer clears it, a hold not seen for a
  day is ignored, and switching the check off clears it. (from #57 by
  gabrielaoimevbore-beep)

- **Send by hand from the dashboard, and a live status panel.** With no
  mailbox connected (or email turned off), each campaign's first email has an
  **Open in Gmail** button that fills in a compose window in the account
  Harvey sends as, and a **Mark as sent** box. Harvey never sends to someone
  marked sent by hand, even if a mailbox is connected later, and only offers
  it for people it could still email (not opted out, replied, bounced, or
  already queued by Harvey). The Today tab gains a panel showing each pipeline
  step, what's running, and what to do next.

- **Outbox review: see what a draft is based on, and edit it.** Each pending
  draft now shows its campaign, the company (name, clickable website,
  description), the contact and how they were found, email status and score,
  tech / agency / hiring / ads / booking signals, and the Writer's notes, so
  every claim can be checked against what Harvey actually found. **Edit** (or
  `E`) turns subject and body into fields; Save rewrites the draft only while
  it is still `pending_review` and never approves it. An edit the sender
  would block (merge tags, HTML, banned phrases) is refused on save with the
  reason; a reply keeps its subject so it stays in the thread; every edit is
  logged with what it replaced. While editing, the shortcuts and Approve all
  can't act on the unedited draft. `POST /api/outbox/{id}/edit` backs it.

- **Microsoft 365 mail provider.** Set `channels.email.provider: microsoft` to
  send and read through a Microsoft 365 / Outlook mailbox via Microsoft Graph,
  for domains whose mail lives in Exchange Online (which no longer accepts
  passwords over IMAP). Signs in once with OAuth + PKCE (`harvey microsoft
  auth`; no client secret, no stored password), drafts then sends so the
  Message-ID is known, threads replies with `createReply`, reads the last 7
  days of the inbox without marking anything read, refuses to act as any
  mailbox other than `persona.email`, and holds sending when the inbox can't
  be read. See [docs/microsoft365.md](docs/microsoft365.md).
- **Optional per-recipient compliance check (Precleared).** Set
  `compliance.precleared.enabled: true` with `sender_country: GB` or `US` to
  have each new recipient checked before the first cold email against UK
  PECR, US CAN-SPAM, Canada's CASL, Germany's UWG and France's rules. Any
  verdict but `ok_to_email` fails that outbox item with the rule and the next
  step. If the check can't be made because of Precleared (daily allowance or
  rate limit, a bad key, network down, an unreadable answer), new addresses
  wait for a later cycle while email to people already cleared, and replies,
  still go out; if it fails for one address, only that email is moved back
  six hours (failures at two different domains with no answer in between
  hold new addresses too), and an address whose check timed out or got an
  answer Harvey can't read is moved back as well, so it can't keep the
  others waiting. One check per address, cached for 30 days; changing the
  sender country, address source or product description triggers a fresh
  check. The receipt id is logged with each send. Native providers only. Off
  by default.
- **`harvey setup` offers the Precleared check** (from #46 by
  gabrielaoimevbore-beep). A new optional last step
  asks whether to turn the check on, where your business is based (GB or US)
  and, if your prospects are all in one country, which one (answering "no"
  there asks before saving it as Norway's code). It then offers a
  free `PRECLEARED_API_KEY` (hidden input, saved to `.env`; 25 checks a day
  instead of 10). Off unless you say yes. Re-running setup offers your saved
  answers as the defaults, so pressing Enter never switches the check off. The
  Claude-guided setup in CLAUDE.md offers the same step. Setup now reads and
  saves `harvey.local.yaml` when it exists (it's what `harvey train` writes,
  and the file Harvey reads), not the `harvey.yaml` it shadows, so its answers
  take effect; if it can't save the file it says so instead of ticking the
  summary.
- **Cohort builder pagination and CSV export.** The cohort table on the
  Signals tab used to stop at the first 50 matches with no way to see the
  rest. It now pages with Prev / Next ("Showing 51–100 of 147"), and an
  Export CSV button downloads the whole cohort. `POST /api/cohort` accepts
  `offset` and `limit` (default 50, max 200) and orders by name so pages never
  overlap; `GET /api/cohort.csv?require=...&exclude=...` returns every match.
  Counts and exports include every match; pages use a stable name/id SQL order.
  CSV downloads carry dashboard authorization and escape spreadsheet formulas.

### Changed

- **Merge variables have one list.** The send check's template check and
  the Sender read the same five (`first_name`, `last_name`, `company`,
  `title`, `personalization`), and the Sender now fills them with any
  spacing inside the braces (`{{ company }}`, `{{company  }}`), as the
  check accepts them, instead of leaving one for the gate to refuse.
- **Every agent tool says its own bounds.** Each tool's description now
  states the limits that enforce it (a plan's 1 to 12 steps of at most 200
  characters, its budget to the cent and 1 to 168 hours, a note's 300
  characters, a wake's 10 minutes ahead and 12 a day, a playbook's name
  and size, a query's sample), taken from the constants themselves, so the
  model keeps to them instead of learning them from a refusal.
- **Closing or cancelling a plan stops its running wake.** A wake job is
  now stoppable: its plan closed or cancelled, or the job cancelled or
  paused on the Queue page, ends its Claude run within two seconds and
  nothing it answered goes into the chat (the call still counts), even
  while it streams; the agent closing its own plan from a wake lets that
  wake finish its report. The
  minute's sweep logs a plan at its day's wakes once a day, not every
  minute.
- **Campaign address checks count against the spend limits.** The Email
  module's checks of waiting addresses follow the contact finder's rule: a
  priced request is reserved from `spend.per_day_usd` / `per_month_usd` and
  recorded where those limits read every job's spend (a day ledger in the
  settings table for spend no job makes), so the finder's, the
  classifier's and the campaigns' add up once. With either limit set, a
  request past a free tier with no price is refused and the address stays
  waiting, checked once there is a price, a new day or month, or room; the
  campaign page, Today and `harvey campaigns show` say why. Free checks are
  never refused. With no limit set, they are made and counted unpriced, as
  before.
- **Each classifier profile has its own pace and its own pause.** The
  pace is the profile's requests a minute; the classify lane adds no
  one-second floor under it (it named none of the services). A 429 or
  an unknown model pauses only the profile it came from, so a hosted
  model's rate limit never stops a local one; a refused key, a payment
  needed or a spent quota pauses every profile on that account (the same
  endpoint host and key name). Settings → Classifier, `GET /api/classify`
  (`held`: each paused profile, why and for how long) and `harvey contacts
  classifier` say which profile is paused and until when (on the card,
  each profile's own and the card's status line). The caps (calls a day,
  USD a day and a month) stay the install's. A paused contacts profile
  rests only the other verifications on the classifier's lane, never
  another profile's test; a server's `retry-after: 0` is a second's pause.
- **Classifier estimates are a range, and say what is enforced.** Find
  contacts, verifying contacts and a profile's test give each task's cost
  as typical (this month's calls on average, by the tokens billed) to high
  (this month's largest call), with the most a call may answer, instead of
  one "at most" figure (`usd_typical` / `usd_high` and a `note` in place
  of `calls_most` / `usd_most`). A website a search found is counted as
  read again, and this month's calls are the profile's own model's when it
  has any. The run's classifier cap is what holds: each call's request
  is built and counted, with a margin for how a tokenizer counts digits
  and addresses, and refused when its worst case doesn't fit; the same
  for a second ask after a reply that isn't valid (counted with the error
  it carries), and for the install's classifier USD caps a day and a
  month. The dashboard says so in one line under each estimate.
- **A classifier profile is the only place its setup lives.** The contact
  finder's older `classify_provider`, `classify_model` and the rest are
  taken once into the `settings` profile and never read again; they are no
  longer settings, and `POST /api/classify/settings` takes the install's
  caps only. A profile names the provider's own key (`DEEPINFRA_API_KEY`,
  `TYPESAFE_API_KEY`).
- **Address checks are counted in dollars, as each service bills them.**
  Each verification service a check asks (Reoon, ZeroBounce, Hunter) is a
  check at that service, counted per month: $0 inside the free checks its
  manifest declares (Reoon 600, ZeroBounce 100, Hunter 50), past them the
  price you enter for it in Settings → Email verification (or `harvey
  modules set email_verify usd_reoon 0.004`; $0 to $10 a request, more is
  refused as a typo). Hunter's pattern lookups are
  counted the same way (25 free a month, `usd_hunter_search`). Under a USD
  cap (a run's, or `spend.per_day_usd` / `per_month_usd` in the
  background) a request past a free tier with no price is refused: a cap
  can't stop what it can't count. A check that needed it is an error, not
  a check done, so the person is searched again once there is a price or a
  new month; the Email module's audience, with no USD cap, still checks and
  counts it unpriced. A priced request is reserved from the run's cap before
  it is made and recorded after. `EMAIL_VERIFY_MONTHLY_CHECKS` now counts
  each service asked (every service together) rather than each address,
  so it agrees with the services' own counts. The find estimate prices the
  checks and lookups at the entered prices and names the services that
  would be refused; the run's report gives each service's checks, the
  refused ones and the pattern lookups. Each service's month is counted in
  one statement, so two checks at once never both take its last free one.
- **A finished run's report is stored.** It is kept on the run (schema
  30), read from there by Today, the Queue, the API and `harvey jobs
  report`, and made again when a job of the run is retried or resumed.
- **The find estimate counts what the run will do.** A website a search
  found and that was read since isn't counted as read again (nor its
  classifier questions) unless the run searches again, and a company every
  search already looked up isn't counted as a lookup.
- **The address-checks line in Find contacts follows the cap.** With a cap
  of 0, one short line says checks need a cap; above 0, what they may
  cost.
- **DeepInfra is recognised by the server's rule.** The price list and
  account box show for a base URL on `api.deepinfra.com` exactly (a saved
  profile's `is_deepinfra`), never any `*.deepinfra.com` host.
- **A website a search found is no longer linked on its name alone.** With
  no classifier set up, a site no register fact confirms is kept with
  nothing linked from it, waiting for a check, instead of linking every
  phone and email on it as "unconfirmed".

- **Autopilot approves an answer when it goes, as it does cold email.** The
  Handler's answers always wait in the Outbox; on autopilot the drain that
  sends one approves it then, after the autopilot review, so an answer
  queued while sending was paused isn't sent on resume without that
  review. The first answer after you undo an opt-out still waits for you.
- **A rejected cold email ends that address's place in its campaign**, and
  so does one whose send was cut off mid-way (it may have arrived): what it
  had queued after it is cancelled, the reason shows on the campaign
  (*respins de tine*, *trimitere întreruptă*), and a campaign that doesn't
  roll can finish.
- **An email you approve while its campaign is paused says why it waits**,
  like the ones the pause found approved; resuming clears it.
- **`harvey setup` asks which mailbox you send from** (Gmail, SMTP or
  Microsoft 365) and its keys, instead of Instantly; training writes
  `provider: gmail`, and a config with no provider means Gmail.
- **The opt-out line is Romanian by default** (*Nu vă interesează?
  Răspundeți „stop" și nu vă mai scriu.*), and Romanian opt-out words
  (*dezabonare*, *nu mai doresc să primesc*, *nu ne mai trimiteți*,
  *scoateți-mă din listă*, *Stop, mulțumesc*, *Nu, mulțumim*, singular or
  plural, with or without diacritics) are honoured by the keyword rules;
  *nu mai doresc să aștept* and *nu ștergeți datele* are not opt-outs. An
  objection (*de unde aveți adresa?*, *ștergeți-mi datele*) is recorded as
  one and handed to you. An opt-out is recorded before anything else about
  the reply, and one that couldn't be recorded (a locked database) is read
  again next cycle, up to five times, then handed to you.
- **Undoing an opt-out with one click** lifts only one Harvey read in the
  reply's words. An unsubscribe, an objection or a complaint is undone on
  the do-not-contact list, with a note saying why.
- **`harvey export --all` leaves the do-not-contact list out** too: a full
  dump can be fed to another sequencer just the same.
- **Every reply is read.** IMAP reads by date in read-only mode (nothing is
  marked read), and a read reaches back to the last complete one when
  Harvey was off longer than a week; cold email waits while a read hasn't
  covered the gap. A mailbox with more mail since then than a read takes
  (5,000 IMAP messages, 40 pages on Gmail or Microsoft 365) is read newest
  first instead of failing every read; cold email then waits until a read
  covers the gap or you accept it (`harvey sending accept-gap`,
  `POST /api/mail/read-gap/accept`). A message from a stranger never holds
  sending: it is marked read, and handed to you, and put on the list when
  their own words ask to stop, only when it answers Harvey's thread or
  comes from a firm Harvey wrote to; a newsletter's "dezabonare" link bars
  nobody. An IMAP read that fails now holds sending instead of looking
  like an empty inbox.
- **Cold email waits when the sending address isn't known** (no
  `persona.email` and Gmail or Microsoft 365 couldn't say which mailbox is
  signed in), rather than going without its unsubscribe header.
- **Schema 26, in place.** The upgrade still writes schema 25; Harvey takes
  the file to 26 when it opens it, in one transaction. A file a newer
  Harvey made is refused with a message that says so. Going back to an
  earlier release after this one has opened the database doesn't work:
  earlier releases don't know schema 26 and ask for `harvey upgrade`,
  which can't help. Keep a copy of `data/harvey.db` from before updating
  if you may need to go back.
- **Romanian webmail domains** (rdslink.ro, clicknet.ro, email.ro, k.ro,
  home.ro) and gmx.net count as free mail: never a firm's key, and an
  address there is personal.
- **Cold email is staged for review, and autopilot approves it when it is
  about to go.** Staging (and the Writer's first emails) always waits in
  the Outbox; on autopilot the drain that sends an email approves it then,
  after the autopilot review, never when it was staged. So staging goes on
  while sending is paused, and resuming never sends a backlog nobody
  looked at without that review.
- **Each heartbeat reads replies, drains the Outbox, then works the
  campaigns**: no action is chosen any more, so an open conversation or
  paused sending no longer stops writing. The Writer no longer makes
  campaigns of its own, and the audience no longer fills a global pool:
  campaigns take their members.
- **Schema 27, in place.** Sending accounts, audiences, campaign members
  and the campaigns' new columns are added when Harvey opens the file. A
  campaign the Writer made earlier stays as it was ('made by Harvey'), its
  prospects become its members (done when every step went out already),
  and nothing is staged or sent from a draft:
  you start it (or archive it) on the Campaigns page.
- **Bulk mail isn't a reply.** A message with its own `List-Unsubscribe`,
  `List-Id` or `Precedence: bulk` that doesn't answer one of Harvey's
  emails, from an address Harvey never sent cold email to, is read and left
  alone: its "dezabonare" or "stop" is not an opt-out, and no model call or
  answer is spent on it.
- **A slim database: harvey.db keeps Harvey's own work, the registers are
  read in place.** The firms you pulled or worked on, their contacts and
  evidence, people, searches, sources, merges, web signals and the email
  side stay in `harvey.db`; the trade registry, the financial statements,
  the taxpayer file and ANAF's answers are read from their own files
  through indexes each load builds there. On a full install `harvey.db`
  goes from about 16 GB to about 1 GB, `anaf.db` from about 8 GB to about
  1.2 GB (compressed; replaced answers are now kept as history), and pages
  that took seconds to minutes answer in well under a second. Register
  facts show the register's own date and current value, and a confirmed
  register signal shows its data at once. Reloading the trade registry
  rebuilds the financial statements' "filed accounts" from it.
- **The upgrade.** An old `harvey.db` (schema 24) is never opened by the
  new code: the dashboard shows an upgrade screen (what it does, expected
  time, the disk it needs and the free space now, live progress per step,
  the verification at the end) and the worker and `harvey run` wait. The
  upgrade, from the screen or `harvey upgrade` (`--check` to only report),
  builds the registers' indexes, rewrites `anaf.db` (every answer hashed
  against the original), moves Harvey's data into a new file, verifies it,
  and only then switches, keeping the old files as `harvey.db.v24` and
  `anaf.db.old`. It resumes after a stop or a crash, refuses to switch when
  verification fails, and deletes nothing: **Delete old files** (typed
  confirmation) or `harvey upgrade --delete-old` does that.
  `harvey upgrade --data DIR --out DIR` runs it on a copy and writes
  nothing in the data folder. A database older than schema 24 is opened
  once by the previous release first. A new install starts on the slim
  database.
- **What the slim database shows differently.** A registered firm's name,
  place, address and activity are the register's current ones: its
  industry is the first current class it is authorised for (not the class a
  pull happened to ask for), and a name Harvey found first gives way to the
  register's. A merge moves nothing: each record keeps its contacts, people,
  readings and places, and the company shows them all (once each); undo
  gives each record back exactly what it had. A firm holds one tax code at
  most. The register's facts aren't copied, so a pull or a lookup reports
  no "observations" for them, and the financial statements are never looked
  up by company: they are read where they are. ANAF's whole answer is kept
  whatever is confirmed (confirming decides what is shown), with earlier
  answers as history. Every site with no deeper read yet (one only "Read
  sites" visited included) is read once by the background, the companies
  with no contact first.
- **First visits are much faster on a full install.** The register
  counts job, which walks every firm's register values once per load
  anyway, now also keeps which firms each register signal holds for, as
  compressed bitmaps (`register_set`): a cohort's register conditions (VAT,
  e-Factura, filed accounts, a turnover range) are read from them and
  patched for the firms added or merged since, instead of every key being
  walked again on the first visit (5-7 s before on 1.8M firms). A register
  search (a county, a locality, words) reaches the firms in Harvey through
  a key table the registry's load builds (`firm_key`), the Companies
  page's email, phone and mobile filters count from kept tallies, the
  Registry page's plain list reads its first pages straight from the
  search index, and the Find-contacts estimate reads only each firm's time
  and site. A cohort range that names a year reads that year's statements
  once, with the firms that have each figure kept by the same job. Until
  the job has run after a register load (an ANAF check or a registry reload
  included), the pages work the sets out as before; removing a firm keeps
  them in use, and the job runs again once many firms were pulled since.
- **Docker on WSL: the free-space check mounts one empty folder, never
  the Windows drive.** `HARVEY_HOST_DRIVE_SOURCE` is now documented as a
  dedicated empty folder on the Windows drive that holds your WSL disk,
  whose free space is the drive's, instead of the drive's root, which
  showed every file on it to a container that reads untrusted websites.
  `scripts/disk-check-folder.sh`, run once from the WSL shell, finds that
  drive, creates `harvey-disk-check` on it and adds the setting to `.env`.
  If you set it to a drive's root, change it and recreate the container:
  until then the free-space check refuses a folder that holds a drive's
  `Windows`, `Users` or `Program Files`, so loads and the upgrade say what
  to set.
- **WSL: the free-space check measures the drive that really holds the
  disk.** It assumed C:, but a moved or imported distro's disk can be on
  any drive. Outside Docker, Harvey now asks Windows which folder holds
  this distro's disk (the registry, through `reg.exe`) and measures that
  drive; when it can't tell, big writes are refused with what to set.
  `scripts/disk-check-folder.sh` checks a `HARVEY_HOST_DRIVE_SOURCE` that
  `.env` already sets: one on another drive (C:, as earlier instructions
  said), missing or not empty is reported with what to change it to.

- `persona.linkedin` is optional and nothing reads it; training and setup
  no longer write one (training used to invent a profile URL from your
  domain). The reply handler's qualifying stage says what to find out
  instead of naming BANT.
- The Today page's "Find emails" step counts the companies the contact
  finder searched and shows it at work while a contact job runs; it read a
  column nothing has written since the prospecting stage went.
- Removed `update_company_signals`: what a site read learns is an
  observation, and nothing wrote those columns any more.

- **ANAF checks run at ANAF's pace.** Saving a batch of answers no longer
  holds up the next request: while one batch is saved the next is asked,
  one request at a time, still one every 1.1 s. On a database of
  1.78 million companies the check went from about 1,000 to about 5,400
  firms a minute. Looking companies up by CUI reads by company instead of
  walking every CUI on file, the search index flush returns at once when
  nothing changed, and a batch's readings, contacts, phones and keys are
  saved with set-based statements in one transaction. Jobs hold 1,000
  firms, can be paused and cancelled, and resume from the last saved
  batch. A failed save or request hands the job back instead of ending it
  done; a busy ANAF rests the whole lane with a growing wait (up to 15
  min) and uses no try. A company's CUIs are never split between batches
  or jobs, and a company with two CUIs that answer differently keeps its
  lowest CUI's single-valued readings, so a re-check writes nothing new.
- **Brave lookups send the company's country** (worked out from its
  location or the market of the official source it came from); a country
  Brave refuses is dropped for the process. Brave's own count of what is
  left is read from each response, and the background stops at the lower
  of it and Harvey's count.
- Sole traders and family businesses (PFA, II, IF) keep their registered
  office and phone like any firm: their registry details are public. A
  personal numeric code is still never stored as a CUI. Reload the registry
  (`harvey registry load --force`, or the next `import-onrc` does it) to fill
  in the addresses left out before.
- **Precleared is told the company's and person's names Harvey already has.**
  For a UK address, cold email may go to a limited company's staff without
  consent but not to a sole trader, so Precleared has to find the company
  behind the domain on the Companies House register. When the domain alone
  didn't find it, the answer was `consent_needed`, even for a company Harvey
  knew the name of. Harvey now sends `company_name` (the company record's
  name, else the prospect's company field) and `recipient_name` (the
  prospect's first and last name; none for a company inbox), which
  Precleared uses to search the register and match the company's officers.
  It doesn't store the person's name. The names join the cache fingerprint
  only when set: a cached `ok_to_email` stands when they first arrive, and
  any other cached verdict gets one fresh check with them.
- **Precleared is told the recipient's country Harvey already knows.** The
  send window works out where a company is (a country, US state, Canadian
  province or Australian state in its location, or a code that is a state
  and a country too once the cached coordinates settle it); the compliance
  check used to ask Precleared about the address with none of it, so
  Precleared had to find the country again and, when it couldn't, answered
  `unknown` and the email failed. Harvey now sends that country as
  `recipient_country` when it is sure of it, which Precleared takes as
  settled. Nothing it is not sure of is sent: "Berlin, DE" with no
  coordinates reads as Delaware by convention and goes without a country,
  as do a bare city and coordinates alone. Without a certain country,
  `prospect_country` goes as the default, as before, and with that unset
  the one country every `icp.geography` entry is in now serves as that
  default; Precleared uses a default only when its own lookups show
  nothing. The country joins the cache fingerprint only when it is set, so
  cached verdicts are kept, and an address Harvey later becomes sure of is
  re-checked once unless Precleared had found the same country itself.
  `docs/precleared.md` describes the check in full.
- **Setup comes first on Today.** Until setup is finished, the checklist sits at
  the top of the Today page as a "Finish setting up Harvey" card, with the next
  step highlighted, instead of at the bottom of the side column.
- **Scout reads a company's site the way a person would.** It starts at the
  homepage and follows the site's own menu to its team, about, our-story,
  meet-the-doctors or attorneys pages (six pages at most), instead of
  guessing nine fixed paths and stopping at the first that answered. Owners
  are read from team cards, sentences ("owned and operated by…"),
  and schema.org founders, and rank first (a surname in the business name
  only breaks a tie between owners);
  at most two people per company become prospects. ICP titles match by
  whole word, with owner synonyms (Proprietor, Principal, Managing Partner,
  Founder, CEO). With `LIKELY_OWNER` confirmed, each owner is recorded with
  its evidence.
- **Email patterns come from the addresses a site publishes**, including
  ones hidden behind Cloudflare email protection or a mail link's `data-`
  attributes, matched to the people the site names. A lone ambiguous
  address (`kmorrow@`) teaches nothing, and an address another named person
  could own (`jsmith@` with both John and Jane Smith there) is never guessed
  for anyone. A named person's own published address is used as is. With no
  evidence, a company known to be small (ICP company size, or a
  local-business listing) is guessed as `first@`, others as `first.last@`. Every address is still graded
  verified / risky / guess / invalid, and only verified or risky ones are
  ever sent.
- **Gmail reads only what's new.** After the first complete read of the
  week, each cycle asks Gmail's history for messages added to the inbox
  since the last check, so a busy mailbox costs what a quiet one does.
  Harvey moves past them only once every reply has been handled, so one
  it couldn't handle is read again. It reads the full week again when
  Gmail's history marker has expired, and once a day as a backstop.

### Fixed

- **CLAUDE.md described the brain's isolation backwards.** It said brain calls
  pass `--dangerously-skip-permissions`; they run with no tools at all, in safe
  mode with no settings files, an empty working folder and the prompt on stdin.
  The doc now says so.
- **Today's run reports are cheap.** A finished run's report is kept and
  given again while none of its jobs changed, instead of being added up
  again for five runs every refresh.
- **Verifying a contact checks campaigns by an index.** Whether a contact
  is in a campaign is a named statement over a new index on campaign
  members by contact.
- **Start writes its log where its log is.** The dashboard's Start made
  the checkout's `data/` folder rather than its log file's.
- **Tests never load a `.env` they didn't write.** `load_dotenv()` with no
  path found the checkout's own file and put a developer's keys in the
  test process; a test fixture now blocks it and clears the classifier's
  keys, so no test can reach a live endpoint.
- **A found website no longer confirms itself.** A second read of a site a
  search found counted the phones and emails its first read had linked as
  "on file" and marked them confirmed (campaign audiences take confirmed
  links). Only a phone or email an official source gave confirms a site
  now; the check of found websites re-trusts what was linked that way.

- **Conversations from before replies were attributed land in their
  campaign** (schema 28, in place): the campaign of the cold email each one
  answers, sent before it began (one older than every cold email stays
  without a campaign).
- **A send that failed ends its member.** A send that raised (it may have
  gone out) or that the mailbox refused is never retried, so its member
  ends there and what it had queued after it is cancelled, as for a send
  cut off mid-way: a campaign that doesn't roll can finish.
- **What waits for you stays waiting.** Approve all leaves the first answer
  since you undid an opt-out for a look of its own; what an undone hand
  send brings back waits for you even on autopilot; an answer an earlier
  release queued after an undo gets the same note on upgrade.
- **`git pull` no longer refuses because Harvey changed `harvey.yaml`.**
  Every config change now goes to `harvey.local.yaml`: `harvey setup`
  (including the training it runs), the dashboard's sending switch and
  `harvey train` with `harvey.yaml` as the output. On an install with no
  `harvey.local.yaml` yet, the first change makes it as a copy of the
  config in use, comments and all, so a `harvey.yaml` an older setup
  edited carries over. A change that fails leaves no copy behind.
  `harvey setup` now rewrites only the settings it asks about, so the
  file's comments stay (it used to rewrite the whole file without them),
  keeps usage settings it doesn't ask about, and swaps the file in whole. A running Harvey that loaded `harvey.yaml` reads
  the switch and the do-not-contact list from the new file at once. The
  tracked `harvey.yaml` stays the template a fresh clone reads until then.
  If `git pull` still refuses over `harvey.yaml`, check that
  `harvey.local.yaml` has your settings, then run
  `git checkout -- harvey.yaml` once and pull again. Messages that said to
  edit `harvey.yaml` now name `harvey.local.yaml`, and say to copy
  `harvey.yaml` to it first on an install that has none: it is read instead
  of `harvey.yaml`, not on top of it, and a `harvey.local.yaml` missing
  required settings now says so when Harvey refuses to start.

- **A combined title on a team card is a role, not another company.**
  "Owner and Lead Dentist", "Founder, Head of Growth" or "Fondator și
  Director Medical" no longer drops the person as someone quoted from
  another organisation; "Founder at Quillnest", "Partner, Example Law LLP",
  "Owner and Lead Dentist, Bluefinch Dental" and any title's "at Another
  Clinic" still do. A branch is not another organisation: "Associate
  Dentist at Port Ellery" or "at Our Vellmora Office" keeps the person.
- A site read that looked for a raise (`FUNDING_ROUND`) no longer drops the
  contact and team pages it read afterwards from what it returns.

- **A registry pull filtered only by figures works.** One filtered only by turnover, headcount or taxpayer flags
  (no activity or area) found no firm; it now takes every operating firm
  (or every firm, with all statuses) that meets them.
- **Checks by CUI batch companies in id order.** A check of companies by CUI cut more than 900 companies into batches and
  jobs in the order they were asked for, not by id, so where a company with
  two CUIs fell depended on its random id (a test failed one run in five).
- **Looking for duplicates on file no longer drops pairs.** When an earlier
  pair of the same pass had merged one side into a third company, the pair was
  skipped, so a record matching two others that merged was never merged or
  asked about. It now follows the merge. A pair someone kept apart or whose
  merge was undone is no longer merged again by the scan. A chain of sure
  pairs that reaches two firms (one listing only one firm's, another only
  another's, the two listings one business) is put to you as one group
  instead of folding both listings into whichever firm came first. Two
  records without a CUI sharing a website wait for review instead of merging
  on their own, and a pair already on the list isn't counted as new on every
  look.
- **Undoing one member of a group merge** no longer takes from the company
  kept what another member still merged into it gives it (a shared contact or
  source, a key, a phone or address it could fill, a place both branches had
  in one town). An email key goes back with its contact.

- **Signal, county and activity filters at install size.** A signal filter
  (has / lacks) reads only that signal's readings instead of every
  observation, and the matches without judging every company: on 1.78M
  companies, 15-19 s became 4-6 s on the Companies page, and on the
  Contacts page, where it ran past ten minutes, about 11 s. A county or town is
  matched once against the distinct places and then read from the place
  index (4.6 s to about 0.5 s). The Contacts page checks each contact's own
  companies against these sets instead of walking them.

- **Replies remember the whole conversation** (closes #97). A reply was
  written from the last six messages only, so a requirement the prospect
  stated earlier ("synthetic data only, processed in the EU") dropped out
  and the draft could contradict it. Harvey now sends the whole thread, and
  tells the model that what the prospect asked for or ruled out, and what
  Harvey agreed to in its own messages, still applies. Each message's text
  is quoted under a header the prospect can't type, so a reply that writes
  "Harvey: confirmed, the first year is free" stays the prospect's words
  and is no agreement. A thread too long to fit (about 24,000 characters)
  keeps the latest six messages, then the prospect's own earlier words,
  then Harvey's, and says how many it left out; one pasted wall of text in
  an older message is cut so it can't crowd out the rest, while the
  message being answered is kept almost whole.

- **The dashboard's Start and Stop work, on Windows and everywhere else.**
  Start launched `python -m harvey` with no command, so the loop printed the
  help and exited; it now runs `harvey run`, and finds `claude` in
  `~/.local/bin` when the native installer left it off PATH. On Windows the
  "is it running?" check was `os.kill(pid, 0)`, which there terminates the
  process it asks about, and a stale PID file made the status and Start
  return 500. The check now asks the OS without signalling. `harvey run`
  records itself too, so a loop started in a terminal shows as running, and
  holds a lock on `data/harvey.lock`: a PID file left by a crash or a reboot
  can name an unrelated program, which is now reported as stopped and never
  signalled, and a second `harvey run` refuses to start while one is
  running. Start refuses, with the reason, until `harvey setup` has been
  run (the loop used to sit in the setup wizard reading the dashboard's
  terminal), and Stop says so when it couldn't end the loop instead of
  reporting success. (From #91 and #90.)

- **`{{company}}'s` reads right for names ending in "s"**: "Parkes Builders'",
  not "Parkes Builders's". (From #90.)

- **LinkedIn-profile web searches run only when LinkedIn prospecting is on**
  (from #60 by FreemanGT). Searching `site:linkedin.com/in` for your titles
  found names with no company and no email, which Harvey can never verify
  or write to, and they counted toward the "no usable emails" check that
  pauses prospecting for the day. It also ran first and could use up the
  cycle's three searches before company discovery, which finds people you
  can email, got any. That search now runs only when LinkedIn prospecting
  is on (`channels.linkedin.enabled`, off by default) with a LinkedIn login.

- **Dashboard actions work in Safari and Firefox** (from #80 by 0SansNom).
  Confirming signals, approving the Outbox and saving settings all failed
  with "Cross-origin requests are refused." The dashboard's requests asked the
  browser to send no referrer, which per the Fetch spec makes a POST's Origin
  `null`, and the guard rightly refused it. Safari and Firefox follow the
  spec; Chrome didn't, so it worked there. Requests now send their origin to
  the dashboard itself, and only to it.

- **A reply stops the cold email to the whole company.** Stop-on-reply was
  per person, so when an owner wrote back "not interested" the office
  manager still got the follow-up. Now a reply (not a bounce, an
  out-of-office or a "wrong person, ask Bob") cancels the sequence emails
  queued for everyone else at that company, and Harvey won't write, stage
  or send cold email to them later; the Outbox and log say which colleague
  replied. Colleagues are the same company; or, when Harvey has no company
  for one of them, the same email domain, and only if it's that company's
  own domain, never a shared one like gmail.com or rr.com.

- **DataForSEO SERP sends a location.** It sent only a keyword, but
  DataForSEO requires a location on every task, so searches failed or came
  back un-localised. It now sends the geocoded city point, or a
  "Denver,Colorado,United States" location name, or the United States for
  searches with no city. "No search results" is now an empty answer rather
  than an error; other task errors still stop that query and are reported.
- **DataForSEO SERP estimates were 3.3x low.** They used the standard-queue
  price ($0.0006 per 10 results); Harvey calls the Live endpoint ($0.002),
  so a depth-30 search is $0.006, not $0.0018. Harvey stays on Live: it's
  synchronous, so the spend cap checked between searches sees real spend.

- **Scout finds the actual decision maker.** On a test set of 19 small
  business sites it found the right person on 3; it now finds 17. It no
  longer turns headings like "Team of Experts" into prospects, reads a
  bio paragraph as a job title, lets "Vice President" pass for "President",
  or takes a customer quoted in a testimonial for the owner.
- **A cached default email pattern no longer blocks better evidence.** A
  first.last@ guess cached for a domain used to be read first forever, so
  neither a real address nor a Hunter lookup could replace it. Hunter is
  asked once per domain, and `j.smith@` is no longer read as first.last@.
- **The company-inbox fallback finds published inboxes it used to miss**: it
  reads whole pages (an `info@` 140k characters into a page counted for
  nothing), schema.org, and an address on the business's own second domain
  (`admin@phroofing.test` for prairieheritageroofing.test). It is also used
  when the named owner's address is only a guess, which is never sent.
- **reCAPTCHA no longer makes a site look blocked.** Any page mentioning
  "captcha" in its first 4,000 characters counted as a block, which cost
  Harvey every site with a protected contact form. Only real challenge
  pages count now, and a SiteGround or Cloudflare challenge is reported as
  challenged (not unreachable) without retrying every URL variant.
- **Gmail reads replies in the charset they were written in** (from #58 by
  xiaoheiai4719-collab). Every reply was decoded as UTF-8, so a reply sent
  as ISO-8859-1 or Windows-1252 ("Je préfère mardi") reached the model
  garbled. Each part now uses its declared charset; an unknown or missing
  one, or odd headers, falls back to UTF-8 without losing the text. SMTP
  and Microsoft 365 already decoded correctly.
- **You see it when Harvey isn't sending because it couldn't read your
  inbox.** Sending waits until replies are read, so an opt-out stops its
  follow-up. That hold only showed in the log, so a lapsed Gmail sign-in
  could stall sending for days unnoticed. Today now shows it first, with
  the reason, how long, how many emails are waiting and the fix (`harvey
  gmail auth`, `harvey microsoft auth`, or a mailbox over the read limit),
  and so does `harvey status`. It clears itself once sending runs again.
- **Gmail: nothing sends until the whole inbox has been read** (from #62 by
  xiaoheiai4719-collab). A failed or partial inbox read used to look like
  an empty inbox, so a follow-up could go out before an opt-out on page two
  was handled. Gmail now reads every page of the last week, and raises on
  any failure so the heartbeat holds sending, the same as Microsoft 365. A
  send whose outcome is unknown (a dropped connection, a 5xx, an unreadable
  answer) is recorded as maybe-sent and never retried, so nobody gets it
  twice. Messages already read aren't downloaded again on later cycles.
- **Autopilot can't be steered by text from outside** (#66, reported by
  Yaaaannnn435). Websites Harvey researches and the replies people send
  reach the model, and either can carry planted instructions. That text is
  now fenced as data in every prompt (writer, reply handler, scout scoring,
  Humanizer), in a block it can't close itself. Prompts only lower the odds,
  so with `require_approval: false` an email nobody approved goes back to
  the Outbox, marked Held with the reason, when it links to or gives an
  address at a domain that isn't yours (sending domain, booking link, or
  `channels.email.link_domains`), or quotes Harvey's own instructions.
  Bare domains like `example.com/x` count as links. Approving it yourself
  sends it. The model never had tools, files or network to act with, so this was
  never more than what it wrote.
- **Opted-out contacts are no longer in the prospect CSV.** Someone who
  replied "unsubscribe" stayed in `harvey export` and the dashboard's Export
  button, so taking the list to another sequencer could email them again.
  They're now left out even when `--status` or `--email-status` is given;
  `harvey export --all` (and `?all=true`) is still the raw, unfiltered dump.
  From #64 by hifzabuildsai.
- **An opt-out now stays an opt-out.** Any later message from someone who
  opted out (an out-of-office, a "thanks") set them back to *replied* or
  *lost*, which put them back in the export, and a question from them got a
  drafted answer in the Outbox. Now their message is kept in a closed
  conversation for you to read, nothing is drafted, and no code path can
  move a prospect off *opted out*. A complaint from them still comes to you.
- **See why someone opted out, and undo it.** Each opt-out now records the
  rule that fired (an opt-out phrase, "no thanks", a one-word reply, or the
  model's judgement), the words it matched and their message. **Why?** on
  their row in Contacts (or on the conversation) shows it, and **Undo
  opt-out** reverses a mistaken one after a confirm: they become *replied*,
  so nothing is sent on its own, and Harvey's answer to their next reply
  waits for your approval even with `require_approval: false`. Opt-outs from before this show their last message instead, and
  every undo is logged.
- **`aiosmtplib` is now 5.1.3 or later** (was `>=3.0.1,<4`). Older versions
  wrote a CR/LF inside an address straight to the SMTP server, so an address
  scraped from a website could add commands of its own
  (GHSA-v3q9-hj7j-63hq), and kept plaintext bytes across STARTTLS
  (GHSA-vxj7-4xrp-5vr4). Harvey passes scraped addresses to it when sending
  over SMTP and when probing a mailbox. Reported in #66.
- A follow-up no longer goes out before, or right after, the email it
  follows. Each step now waits until the previous one has actually been sent
  and its gap (at least a day) has passed. Before, approving a batch of drafts
  late, or resuming after a pause or a compliance hold, could send a whole
  sequence within minutes, or step 2 before step 1. A follow-up whose earlier
  email was rejected or failed is cancelled instead of arriving as a first
  email.
- **Windows:** text files (harvey.yaml, .env, prompts, skills, trained
  output, tokens, exports) are read and written as UTF-8 instead of the
  locale code page, so `harvey train` no longer crashes on characters like
  "★" and prompts aren't garbled; command output is UTF-8 too, so the
  dashboard's log of `harvey run` can't crash on "⚠". An npm install's
  `claude.cmd` shim is resolved to the native `claude.exe` it wraps and is
  never run through cmd.exe, which would re-parse Harvey's isolation flags;
  if the executable is missing, Harvey says how to fix it. Files an older
  Harvey saved in the Windows code page (trained skills, harvey.yaml) still
  load, with a warning to re-save them as UTF-8. `harvey export` CSVs are now
  UTF-8 on Windows too, which sequencers expect (Excel needs Data > From
  Text/CSV to show accents). Based on #18 by Tariz Shahid.
- Replies are read without the email they quote. The model, the stored thread
  and the answer get the reply without the quoted email, and any line that
  repeats what Harvey sent that person is set aside, so Harvey's own words
  (its footer, or an escalation word like "attorney" in its pitch) don't count
  as theirs. Opt-out and escalation words the person wrote are still found
  anywhere, including between or under quoted lines. "No thanks" / "no thank
  you" as a phrase, and a reply of just "STOP", now count as opt-outs.

- The Sender claims each email right before sending it. A reply, bounce, hand
  send or rejection that cancels an approved email while the Sender is working
  through its queue now stops it, instead of the Sender sending its earlier
  copy and recording it as sent. Two Harvey processes running at once can no
  longer send the same email twice. An email cut off mid-send (Harvey stopped,
  or the mail library crashed mid-call) is marked failed and never retried, since
  it may have gone out; it shows under "Didn't send" with a note to check your
  Sent folder, and the dashboard won't offer to email that person by hand.
  Rejecting an email that is already sending or sent is refused instead of
  relabelling it.
- Keep local website requests out of private networks, including redirects and
  DNS rebinding. Preserve the original hostname for TLS verification. Website
  fetching (search, company sites, profiling, the training crawler) now ignores
  `HTTP_PROXY`/`HTTPS_PROXY`, so it can't be routed around that boundary; mail
  and API integrations are unchanged. `harvey train` refuses local or intranet
  URLs and says so.
- Disable LinkedIn by default in the model, sample config, and product trainer;
  save the setup wizard's explicit LinkedIn choice when updating a config.
  Answering "no" to LinkedIn when re-running `harvey setup` now turns it off
  (saved credentials stay in `.env`).
- Protect the local dashboard with an access token, restrict requests to trusted
  local origins, and save settings with owner-only permissions. Update and restart
  Harvey, then open the dashboard link printed in your terminal.
- Isolate Claude calls from personal instructions and hooks, and fix Claude CLI
  installation in Docker. Thanks to @EICCIO-Advisors for the private report.

### Removed

- **Instantly.** Harvey sends and reads mail through Gmail, SMTP or
  Microsoft 365 only, where every email passes the Outbox, the
  do-not-contact list, the source note and the unsubscribe header. Gone:
  the Instantly client, deploying campaigns and reading replies there,
  `INSTANTLY_API_KEY` in setup and Settings (a key left in `.env` is kept
  and ignored), and the Instantly test. A config that still says
  `provider: instantly` loads; nothing is sent or read, and the log,
  `harvey status`, Today and the setup checklist say, in Romanian and
  English, to choose gmail, smtp or microsoft. Finding contacts and
  everything outside email keep working. Campaigns an earlier Harvey
  deployed to Instantly keep running there until you stop them in
  Instantly, and are never staged through your own mailbox: the people
  it never wrote to leave it (*deployed to Instantly*); its others still
  count as contacted. A prospect it couldn't write for is new again.
- **The Writer's automatic campaigns.** The heartbeat's one-action-per-cycle
  choice, the Writer grouping new prospects into a campaign per industry,
  the Audience turning every email contact into a prospect each cycle, and
  `harvey retry-writes` (only that flow gave up on a prospect). Every cold
  email now comes from a campaign you made; one the Writer made earlier
  stays readable, and an active one is still sent.
- **The old prospecting code the contact finder replaced.** Nothing called it
  any more: the Scout agent (`harvey/agents/scout.py`: its search-engine,
  Hunter, LinkedIn, job-board and company-website strategies and its contact
  scoring), the DISCOVER → PROFILE chain in `harvey/pipeline.py`, the
  in-process ANAF follow-up of a registry pull (the pull queues ANAF check
  jobs instead) and the in-process automatic update of public datasets (the
  worker queues them). The module switches, `.env` keys and signals those
  strategies used are all still there; what Harvey does is unchanged. Two
  messages that said Scout would find people now say how contacts are found.
- **Three options that did nothing since the old prospecting went.**
  LinkedIn prospecting (the `linkedin` module and its people search, the
  browser login and its `data/linkedin_login_hold.json` hold, the setup
  wizard's LinkedIn step, the LinkedIn row in Settings and on the Today
  checklist, `channels.linkedin` and `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD`,
  and the `linkedin_outreach` skill); the hiring module's job-board search
  (python-jobspy, Indeed); and the keyless web search module
  (`web_search_scrape`: DuckDuckGo, Bing and Google result pages, G2 and
  Clutch). Nothing called any of them. A config that still has
  `channels.linkedin`, a `.env` with the LinkedIn keys, or a saved
  `modules.linkedin.enabled` or `modules.web_search_scrape.enabled` loads as
  before and is ignored. The hiring module keeps its careers-page and job
  board feed signals and its preset search. `harvey install` no longer
  downloads a Chromium: only LinkedIn launched one, and the browser for
  JavaScript-only sites is reached over CDP (`BROWSER_CDP_URL`, and the
  Docker Chromium at `BROWSER_CDP_FALLBACK_URL`, which stays). LinkedIn
  addresses a company's own site links to are still kept as contacts.
- **The Scout agent's prompt and skills, and real-looking names in tests.**
  `prompts/scout.md` and the three skills only Scout loaded
  (`prospecting_tactics`, `lead_qualification`, `account_navigation`) are
  gone, with Scout's row in the skill map. `sales_methodology` no longer
  plans LinkedIn touches between emails: Harvey writes email only, and the
  skill now says so. The Hunter module's description says what it does (a
  domain's address pattern, and a verification fallback). Test and
  documentation data that could belong to real businesses now uses
  reserved example names (`acme.example`, `.test`, `<name>.example.ro`),
  phone numbers no subscriber can hold (`0268 000 1xx`, US 555-01xx) and
  `exemplu.fictiv.*` free-mail addresses; a test now fails on any other.

## [0.3.0] — 2026-10-13

### Added

- **Opt-in Codex brain (ChatGPT plan).** Set `brain.provider: codex` (or pick
  it in `harvey setup`, or `harvey train --provider codex`) to think with the
  Codex CLI on a ChatGPT subscription instead of Claude. Claude remains the
  default and there is no automatic fallback between providers. Each call
  runs a throwaway, isolated `codex app-server` with no tools; Harvey verifies
  the lockdown from Codex's own reports before sending any prompt, aborts on
  any attempted tool use, refuses API-key logins and custom endpoints
  (checked before your login is exposed to Codex), and never changes your own
  Codex settings or login: each call gets a private copy of the login without
  its refresh token, so an expired login must be renewed with Codex itself or
  `codex login`. Requires Codex CLI 0.159.3; other versions are refused. See
  the README section "Using a ChatGPT plan instead (Codex)".
- `brain.model` picks the model for either provider, for Harvey only. For
  Codex use `gpt-5.5` (suggested by `harvey setup`): the verified text-only
  model. Blank auto-selection only picks listed models without code-execution
  tools, and on current ChatGPT accounts there are none; code-mode models are
  refused.
- If Codex can't be used (expired or renewal-due login, unsupported CLI,
  unsafe Codex config, rejected model, plan usage limit), Harvey pauses the
  heartbeat instead of running agents that would fail: replies stay unread
  and unescalated, queued emails wait, and the reason shows in the log,
  `harvey status` and the dashboard. It resumes on its own once the cause is
  fixed. `harvey status --check` runs the same check (no prompt sent) on demand
  and exits non-zero when it fails. The Codex provider supports macOS and
  Linux; it is refused on Windows (use Claude there).

### Changed

- Each heartbeat now sends email after the agents that read and answer
  replies have finished, and native mail providers (Gmail/SMTP) read the
  mailbox every cycle, so a reply always cancels its queued follow-ups before
  that cycle's sends (previously the two could run at the same time, and a
  reply arriving with no open conversation waited for a later cycle). This
  applies to Claude and Codex alike.
- A reply is only recorded once its reply draft (if it needs one) is
  written, so a brain outage part-way never half-records it. If a working
  brain returns no usable draft, the conversation is flagged for a human
  instead of being left open with no answer.
- Usage is tracked per provider. The budget gate, `harvey usage`, `harvey
  status` and the dashboard read and label the active provider's own quota;
  existing usage history is kept and attributed to Claude.

### Fixed

- Dashboard HTML and text assets now use UTF-8 explicitly, fixing Windows
  decoding failures. Regression tests cover HTML, CSS, JavaScript and SVG.

## [0.2.1] — 2026-10-13

A security release. Update if you run Harvey.

### Security

- **Claude now runs with no tools.** Every Claude call used to run with
  `--dangerously-skip-permissions`, and prompts include text Harvey doesn't
  control: prospect email replies and scraped web pages. Instructions hidden
  in that text could make Claude run commands, read or write files, or use
  the operator's connected MCP servers. Calls now run with `--tools ""` and
  `--strict-mcp-config`; Harvey only ever needed text back, so nothing else
  changes. Reported privately by [@hackchang](https://github.com/hackchang).
  If you ran an earlier version with live replies coming in, check the
  account that ran Harvey for unexpected files or activity.

### Fixed

- `harvey run` no longer drops into the setup wizard when the trained config
  lives in `harvey.local.yaml`; the dashboard reads it too.
- A heartbeat cycle is capped at 10 minutes, so one hung agent can't freeze
  Harvey, and quiet hours are re-checked every 5 minutes so a Mac waking from
  sleep resumes on time.
- Sites that only answer on plain HTTP or `www.` are no longer marked
  unreachable.
- Profile and contact backfills rotate through every company instead of
  retrying the same few forever.
- The trainer writes full place names ("Solon, Ohio") and real job titles, so
  discovery doesn't geocode to the wrong city and found contacts aren't
  silently discarded.

### Added

- Contacts are backfilled for companies found without any, from their own team
  pages and, if a key is set, Hunter's named-people search.
- `HUNTER_MONTHLY_SEARCHES` (default 25) sets one Hunter budget shared by every
  domain search.

Thanks to [@sshaw0729-dotcom](https://github.com/sshaw0729-dotcom) for the
fixes above (#2).

## [0.2.0] — 2026-09-13

The release that made Harvey worth running. The previous version could write
and send email but could not reliably find anyone to send it to, and it needed
a paid third-party platform to send at all. Both are fixed.

### Added

- **A prospecting pipeline that works.** Four stages, each independently
  runnable and re-runnable: DISCOVER (who exists) → PROFILE (what they are) →
  ENRICH (who decides) → VERIFY (can you reach them).
- **DISCOVER, with a provider menu.** Four sources behind one adapter
  interface, shown side by side in the dashboard with what each does, what it
  costs, its free tier and which keys it needs. The default — OpenStreetMap —
  needs no account, no card and no key, so the whole pipeline can be run
  before paying anyone. `harvey discover`.
- **Cost discipline in the collector.** Every discovery run estimates before it
  spends, refuses to start over your cap, re-checks the cap between batches,
  reads a kill switch, and flushes observations per batch so a run that dies
  keeps what it learned. `harvey discover --estimate` prints the projection and
  exits.
- **PROFILE.** Free — three HTTP requests per business, no browser, no model
  call. Reads the incumbent agency out of the site footer, ad pixels, missing
  schema, abandoned blogs, open roles and named people. Chained onto DISCOVER
  and run on every heartbeat. `harvey profile`.
- **A signal catalog you confirm.** Harvey knows how to collect 23 signals and
  collects none of them until a human says so. Each shows a plain-language
  description, what it costs, and how many companies already carry it.
  `harvey signals`.
- **The cohort builder.** Pick what a prospect must have and what disqualifies
  them; see the matching companies live. A prospect list is a query you can
  explain, not a file.
- **Native mail providers.** Gmail (REST + OAuth) and any SMTP+IMAP mailbox.
- **The outbox approval ladder.** Every outgoing email waits for approval by
  default (`pending_review → approved → sent`), reviewed one at a time in a
  decisions desk with `A`/`R`/`J`/`K`. A deterministic pre-send gate — no model
  involved — rejects unrendered merge tags, banned phrases, over-length bodies,
  too many links, HTML, undeliverable addresses and recipient mismatches.
- **Bounce handling and a kill switch.** A bounce marks the address invalid,
  cancels that prospect's queued sends, and trips a global pause past a
  configured bounce rate. `harvey sending pause|resume`.
- **Claude usage tracking.** Per-call attribution by agent, task and model, plus
  live subscription-quota gauges read the same way `/usage` does, so Harvey
  throttles itself and leaves headroom for your own interactive work.
  `harvey usage`.
- **CSV export.** `harvey export` writes a sequencer-ready list. Harvey is worth
  running as a list-builder even if you never let it send.
- **A dashboard worth looking at.** Opens on Today — what needs a human — with
  an activity feed and a rail of pipeline figures, collector runs and setup.
  Light and dark are the same design re-tokenised; appearance follows your OS by
  default. Fonts are vendored so it renders with the network off.
- **A Python version guard.** `python3 -m venv` on macOS builds from the system
  3.9, which cannot run Harvey. It now fails immediately with the fix instead of
  surfacing later as an unrelated-looking ImportError.

### Changed

- **The data model.** Every fact Harvey learns is now an observation — a row in
  `observations`, never a column. A new signal needs no migration, re-observing
  over time is a free time series, and confidence and provenance travel with the
  fact. The vocabulary is governed: a database trigger rejects any signal code
  not in `signal_codes`.
- **Email finding is pattern-first.** Harvey derives the domain's address format
  and verifies one candidate rather than brute-forcing name variations, and
  grades every address honestly as `verified` / `risky` / `guess` / `invalid`.
  Only deliverable addresses are ever sent to.
- **The default mail provider** is now `gmail`. Instantly is still supported as
  a legacy option.
- **Discovery depth defaults to 30, not 100.** Google removed
  100-results-per-page in September 2025, so depth 100 is now billed as ten
  pages nearly everywhere — and below rank 30 it is mostly directories.
- **The dashboard's HTML, CSS and JS** are real files in `harvey/web/` instead
  of a 1,500-line string literal in `dashboard.py`. Still no build step.

### Fixed

- **Guessed emails were being reported as verified** whenever the domain merely
  had an MX record. The migration downgrades every legacy `email_verified=1` row
  to `guess`.
- **Cohorts counted the wrong companies.** PROFILE records a false boolean when
  it checks and finds nothing — which is correct — but `cohort()` matched on the
  row existing, so "companies running Google Ads" silently meant "companies we
  checked for Google Ads", which was all of them. Cohorts now read the newest
  observation per company and signal, and require it to be true.
- **Usage tracking counted every Claude session on the machine**, not just
  Harvey's. The transcript reconciler is gone; the ledger contains only rows
  Harvey's own Brain wrote.
- **Page titles were being extracted as people** ("Request Service", "Commercial
  Services"), and titles bled between team members.
- **Confirming a signal before anything had loaded the catalog** reported success
  while changing nothing.
- **The free OSM provider answered unmapped search terms with every registered
  office in the area** — coffee roasters and churches for a roofing query. It now
  refuses the query and names the trades it can search.

### Security

- Test fixtures no longer carry real businesses' phone numbers or street
  addresses.
- `harvey.local.yaml` (gitignored) overrides the tracked `harvey.yaml`, so a
  fork can hold real product configuration without it reaching a public repo.

## [0.1.0]

Initial release: the heartbeat loop, five sub-agents, the skills library, the
website trainer, Instantly-based sending, and the first dashboard.
