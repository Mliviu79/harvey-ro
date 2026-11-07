"""SQLite state manager. All of Harvey's memory lives here.

Concurrency: the DB runs in WAL mode (set persistently at init) so the
dashboard can read while the agent writes. Every connection gets a busy
timeout so concurrent writers wait instead of raising "database is locked".

Migrations: schema changes are applied via a linear, idempotent migration
list tracked with SQLite's ``PRAGMA user_version`` so existing user DBs
upgrade cleanly in place.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import aiosqlite

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep  # noqa: F401 (EmailStep re-exported)
from harvey.models.conversation import Conversation, Message  # noqa: F401

from harvey import contacts, resolver
from harvey.paths import PROJECT_ROOT
from harvey.startups import parse_funding
from harvey.words import SEARCH_STOPWORDS, fold_words

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "harvey.db"

# How long (seconds) a connection waits on a locked database before failing.
BUSY_TIMEOUT_SECONDS = 30.0
# How long a start waits for another process's upgrade of the schema: a
# migration over a full registry holds the database for a minute or more.
MIGRATION_TIMEOUT_SECONDS = 900.0

# Email statuses that can be written to (`harvey status` counts the
# prospects ready to write for by them).
WRITABLE_EMAIL_STATUSES = ("verified", "risky")
# Prospects scored below this when they were found are kept, ranked last, but
# not written to. A score of 0 means never scored (the contact finder's
# contacts, imports): still written.
LOW_SCORE = 30


# The signals whose latest reading the Writer is told about (company_brief).
BRIEF_CODES = ("TECH_STACK", "HIRING_ROLE", "OPEN_ROLES", "RUNNING_GOOGLE_ADS",
               "RUNNING_META_ADS", "NO_ONLINE_BOOKING", "INCUMBENT_AGENCY", "EMPLOYEES",
               "REVIEW_RATING", "REVIEW_COUNT")


def company_brief_lines(location: str, activity: str, rows: list[dict]) -> list[str]:
    """The Writer's facts from a company's location, activity and
    observations (``rows``: signal_code, value_num, value_text, observed_at,
    newest first). Only each signal's latest reading counts, and a "no"
    (0) says nothing worth writing about, except no online booking."""
    latest: dict[str, list[dict]] = {}
    for r in rows:
        first = latest.setdefault(r["signal_code"], [r])
        if first[0] is not r and r["observed_at"] == first[0]["observed_at"]:
            first.append(r)         # several readings of one pass (open roles)
    out = []
    town = (location or "").split(",")[0].strip()
    if town:
        out.append(f"Based in {town}")
    if activity:
        out.append(f"Registered activity: {activity}")

    def one(code):
        return (latest.get(code) or [None])[0]

    def num(code):
        r = one(code)
        return r["value_num"] if r is not None and r["value_num"] is not None else None

    tech = one("TECH_STACK")
    if tech is not None and (tech["value_text"] or "").strip():
        out.append(f"Tools on their website: {tech['value_text'].strip()}")
    roles = [r["value_text"].strip() for r in latest.get("HIRING_ROLE", [])
             if (r["value_text"] or "").strip() and r["value_num"] != 0]
    if roles:
        out.append("Hiring: " + "; ".join(roles[:2]))
    if (num("OPEN_ROLES") or 0) > 0:
        out.append(f"{int(num('OPEN_ROLES'))} open roles on their job board")
    if num("RUNNING_GOOGLE_ADS") == 1:
        out.append("Running Google Ads")
    if num("RUNNING_META_ADS") == 1:
        out.append("Running Facebook/Instagram ads")
    if num("NO_ONLINE_BOOKING") == 1:
        out.append("No online booking on their website")
    agency = one("INCUMBENT_AGENCY")
    if agency is not None and (agency["value_text"] or "").strip():
        out.append(f"Website credited to: {agency['value_text'].strip()}")
    # (One reading per financial year, the year in value_text: the newest year.)
    staff = max((r for r in rows if r["signal_code"] == "EMPLOYEES" and r["value_num"] is not None),
                key=lambda r: r["value_text"] or "", default=None)
    if staff is not None:
        year = (staff["value_text"] or "").strip()
        out.append(f"{int(staff['value_num'])} employees" + (f" ({year} accounts)" if year else ""))
    if num("REVIEW_RATING") is not None:
        count = num("REVIEW_COUNT")
        out.append(f"Rated {num('REVIEW_RATING'):g}"
                   + (f" from {int(count)} reviews" if count else ""))
    return out


def brief_note(lines: list[str]) -> str:
    """company_brief's facts as one prospect note (personalization_notes)."""
    return ". ".join(lines)[:400]


def poorly_scored(score) -> bool:
    """Scored, and below LOW_SCORE: kept on file, left out of campaigns."""
    return 0 < (score or 0) < LOW_SCORE
# Settings key, per prospect: the user emailed them by hand from the
# dashboard. Its value is the status they had before, so undo is exact; its
# presence tells the Sender never to email them as well.
MANUAL_SEND_KEY = "manual_send:"
# Outbox cancel reason for mail the user sent by hand instead; undo restores it.
HAND_SENT_REASON = "sent_by_hand"
# Set when you undo someone's opt-out: Harvey's first reply to them after
# that waits for your approval, even on autopilot. Cleared once it has.
REVIEW_NEXT_REPLY_KEY = "review_next_reply:"
# Outbox error prefix for an email autopilot held for a human (gate.
# autopilot_review): approve-all skips these, they're approved one by one.
HELD_FOR_REVIEW = "held for your review: "
# Set while the heartbeat holds sending because replies couldn't all be read
# (JSON: since, last_seen, reason), so Today and `harvey status` can say so.
SENDS_HELD_KEY = "sends_held"
# Outbox error on a sequence email cancelled because someone else at the
# company replied: one answer from a company stops the cold email to all of it.
COLLEAGUE_REPLIED = "a colleague replied"
# Setting set (to their name) when someone's reply stops their company.
# An out-of-office or a "wrong person" doesn't set it.
COMPANY_STOP_KEY = "company_stop:"
# Shared mailbox providers: two people on one aren't colleagues, a firm is
# never keyed on one, and an address on one is personal.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "web.de",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "qq.com", "163.com",
    "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net",
    "cox.net", "charter.net", "hey.com", "fastmail.com", "pm.me", "rr.com",
    "earthlink.net", "frontier.com", "frontiernet.net", "windstream.net",
    "centurylink.net", "optonline.net", "roadrunner.com", "juno.com",
    "netzero.net", "mindspring.com", "embarqmail.com", "suddenlink.net",
    "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "hotmail.co.uk", "hotmail.ca",
    "live.co.uk", "live.ca", "btinternet.com", "sky.com", "virginmedia.com",
    "talktalk.net", "ntlworld.com", "shaw.ca", "rogers.com", "sympatico.ca",
    "telus.net", "bigpond.com", "optusnet.com.au", "orange.fr", "free.fr",
    "wanadoo.fr", "laposte.net", "t-online.de", "libero.it", "mail.ru",
    "naver.com", "seznam.cz", "wp.pl", "o2.pl", "tutanota.com", "tuta.io",
    "yahoo.ro", "yahoo.fr", "yahoo.it", "yahoo.de", "yahoo.es", "hotmail.fr",
    "hotmail.it", "hotmail.de", "hotmail.es", "outlook.fr", "outlook.de",
    # Romanian webmail and home-ISP mailboxes (a person's, never a firm's
    # domain): the resolver never joins two firms on them, and an address
    # there is personal (email_rules).
    "rdslink.ro", "clicknet.ro", "email.ro", "k.ro", "home.ro", "gmx.net",
})


def _email_domain(alias: str) -> str:
    return f"lower(substr({alias}.email, instr({alias}.email, '@') + 1))"


def _colleagues(p: str, c: str) -> str:
    """SQL: prospect ``c`` works with prospect ``p``. The same company (a
    merged record's prospects are its survivor's); or, when either has no
    company, the same email domain, never a shared one and never one that
    isn't the known side's own company domain."""
    domain = _email_domain(p)
    free = ", ".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))
    own = "(SELECT lower(co.domain) FROM firm co WHERE co.id = {x}.company_id)"
    live = ("(SELECT CASE f.live WHEN 0 THEN f.id ELSE f.live END FROM firm f "
            "WHERE f.id = {x}.company_id)")
    return (
        f"{c}.id != {p}.id AND ("
        f"({p}.company_id != '' AND {live.format(x=c)} = {live.format(x=p)}) OR "
        f"(({p}.company_id = '' OR {c}.company_id = '') "
        f"AND instr({p}.email, '@') > 0 AND {domain} != '' AND {domain} NOT IN ({free}) "
        f"AND {_email_domain(c)} = {domain} "
        f"AND ({p}.company_id = '' OR {domain} = {own.format(x=p)}) "
        f"AND ({c}.company_id = '' OR {domain} = {own.format(x=c)})))"
    )


# SQL: outbox row ``o`` belongs to a campaign that is paused or a draft.
_HELD_CAMPAIGN = ("o.campaign_id IN (SELECT id FROM campaigns "
                  "WHERE status IN ('paused', 'draft'))")


def _company_answered(p: str) -> str:
    """SQL: someone at prospect ``p``'s company has replied."""
    return (f"EXISTS (SELECT 1 FROM prospects c, settings s "
            f"WHERE s.key = '{COMPANY_STOP_KEY}' || c.id AND {_colleagues(p, 'c')})")
# Outbox error on what autopilot had approved when it was turned off.
AUTOPILOT_OFF_REASON = "waiting for you: autopilot was turned off before it went out"
# Outbox error on the first answer since an opt-out was undone: it waits for
# you even on autopilot (the undo panel promises it).
REVIEW_AFTER_UNDO = "waiting for you: the first answer since you undid their opt-out"
# Outbox error on an answer Harvey's agent asked the Handler to draft: it
# waits for you even on autopilot (promote_for_autopilot takes no row with a
# note), and Approve all leaves it for you to read one at a time. Your
# autopilot is consent to Harvey's own answers, not to one the agent asked for.
AGENT_DRAFT_HELD = "waiting for you: an answer Harvey's agent asked for"
# Outbox error on what an undone hand send brought back: a human decides
# again, so autopilot never approves it (promote_for_autopilot).
RESTORED_FOR_REVIEW = "waiting for you: back after you undid a hand send"
# Outbox errors on cold email whose campaign is paused or not started yet
# (it waits, approved), or asks for every email to be reviewed.
CAMPAIGN_PAUSED = "waiting: its campaign is paused"
CAMPAIGN_NOT_STARTED = "waiting: its campaign hasn't started"
CAMPAIGN_REVIEWS = "waiting for you: its campaign asks you to review every email"
# Outbox error on cold email that waits, approved, for the operator's
# cold-email acknowledgement (harvey ack).
ACK_NEEDED = "waiting for the cold-email acknowledgement: run harvey ack"
# Outbox error on cold email that waits, approved, because the address it
# goes out from (which its unsubscribe header names) isn't known.
NO_SENDER_ADDRESS = ("waiting: the sending address isn't known, so the unsubscribe header "
                     "can't be built; set persona.email")
# Outbox error on a first email to a personal address that lacked its source
# note: the note was added, and it waits for you to approve it as it is now.
NOTE_ADDED = ("waiting for you: the address is personal, so the line saying where it was "
              "found was added")
# Outbox error on a first email to a personal address that carries the
# privacy page and another link: one link is all a cold email may have.
PRIVACY_LINK_HELD = ("waiting for you: the privacy page is this email's one link; take the "
                     "other link out")
# Outbox error on cold email that waits, approved, while the mailbox read
# hasn't covered the time Harvey was off (READ_GAP_KEY).
READ_GAP_HELD = "waiting: the mailbox hasn't been read back to the last complete read"
# Settings: when each mailbox was last read completely (unix seconds), and,
# while a read hasn't covered the gap since, why cold email waits (JSON:
# since, reason).
MAIL_READ_KEY = "mail_read_at:"
READ_GAP_KEY = "mail_read_gap"
# Outbox error on an approved cold email the compliance check couldn't answer
# for: it was moved back and is tried again then (sender: _retry_later).
RECHECK_PREFIX = "Precleared couldn't check this address; trying again after "
# Outbox errors starting with this: the send was cut off and the email may
# have reached them. Never retried, and counted as Harvey's mail, so the
# dashboard never offers a second one by hand.
MAYBE_SENT_PREFIX = "may have gone out: "
STALE_SENDING_ERROR = (
    MAYBE_SENT_PREFIX + "Harvey stopped in the middle of sending it; check "
    "your Sent folder before emailing them again"
)
# A claimed email still not finished after this long was cut off mid-send (a
# send itself takes seconds, and a whole cycle is stopped after 10 minutes).
# Long enough never to catch another sender's email in flight.
STALE_SENDING_AFTER = timedelta(minutes=15)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    """Naive UTC now (matches how timestamps are stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(value: str | None) -> str:
    """Normalize an identity key (email/domain) for dedup: strip + lowercase."""
    return (value or "").strip().lower()


# ── Company search: words typed -> an FTS5 query ──

# (harvey/words.py, shared with the registry's search)
_SEARCH_STOPWORDS = SEARCH_STOPWORDS
_fold_words = fold_words


def _search_term(word: str) -> str:
    if word.isdigit() and len(word) == 4:
        return f'"{word}"'
    return f'"{word}"*' if len(word) >= 3 else f'"{word}"'


def search_terms(query: str) -> tuple[str, list[str]]:
    """(FTS5 MATCH expression, the folded words) for what someone typed.

    Every word must match (AND); a word written with a leading minus
    ("-pizza") must not. A 4-digit word is a CAEN code and matches exactly,
    also typed as "86.23" or "8623/2"; any other word of 3+ letters matches
    as a prefix. Single letters are dropped ("S.R.L." would otherwise match
    every company). Each term is quoted, so nothing typed is read as FTS
    syntax."""
    query = re.sub(r"\b(\d{2})\.(\d{2})\b", r"\1\2", query or "")
    query = re.sub(r"\b(\d{4})/\d+\b", r"\1", query)
    wanted: list[str] = []
    unwanted: list[str] = []
    for token in query.split()[:20]:
        bucket = unwanted if token.startswith("-") and len(token) > 1 else wanted
        bucket += [w for w in _fold_words(token)
                   if len(w) > 1 and w not in _SEARCH_STOPWORDS]
    wanted = list(dict.fromkeys(wanted))
    unwanted = [w for w in dict.fromkeys(unwanted) if w not in wanted]
    if not wanted:
        return "", []
    match = " AND ".join(_search_term(w) for w in wanted)
    if unwanted:
        match = f"({match})" + "".join(f" NOT {_search_term(w)}" for w in unwanted)
    return match, wanted


def _activity_matches(activity: dict, words: list[str]) -> bool:
    """Whether a query word names this activity (its code, or a word of its
    names or everyday words)."""
    text = _fold_words(" ".join((activity.get("code", ""), activity.get("name", ""),
                                 activity.get("name_en", ""), activity.get("keywords", ""))))
    for w in words:
        if w.isdigit() and len(w) == 4:
            if w == activity.get("code"):
                return True
        elif any(t == w or (len(w) >= 3 and t.startswith(w)) for t in text):
            return True
    return False


# ── Schema migrations ─────────────────────────────────────────────────
# Each entry is an idempotent SQL script. The index into this list + 1 is
# the schema version stored in PRAGMA user_version. Never edit or reorder
# released migrations — append new ones.


# Pairs a look for duplicates merges or queues per write transaction.
SCAN_WRITE_BATCH = 200

# Live companies: not merged into another (SQL, on companies aliased "c").
LIVE_COMPANY = "COALESCE(c.merged_into, '') = ''"

# (openai_compatible, anthropic and decision: the classifier's providers,
# harvey/classify.py; Harvey's own model is claude or codex.)
BRAIN_PROVIDERS = ("claude", "codex", "openai_compatible", "anthropic", "decision")
_USAGE_LOG_COLUMN = {"claude": "claude_calls", "codex": "codex_calls"}

_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel",
    "sequence_json", "prospect_ids_json", "status",
})
_CONVERSATION_COLUMNS = frozenset({
    "prospect_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


# ── Company writes ──

class Resolution:
    """What saving a company did: ``company_id`` is the record to write its
    facts to; ``created`` whether that is a new company; ``match`` how it
    was matched; ``merged_from`` the row an automatic merge folded in;
    ``origin`` the record before any merge."""

    __slots__ = ("company_id", "created", "match", "merged_from")

    def __init__(self, company_id: str, created: bool, match: "resolver.Match",
                 merged_from: str = ""):
        self.company_id, self.created, self.match = company_id, created, match
        self.merged_from = merged_from

    @property
    def origin(self) -> str:
        """The record this one is before any merge: the row an automatic
        merge folded in, the merged record whose own key matched, or the
        company itself. Facts found for it carry it, so undoing that merge
        takes them back (resolver.undo)."""
        return self.merged_from or self.match.origin or self.company_id


class UpgradeNeeded(RuntimeError):
    """The database is an older schema: harvey upgrade (or the dashboard's
    upgrade screen) brings it to schema 25 first (the steps after it run
    when it is opened)."""

    def __init__(self, version: int):
        self.version = version
        if version:
            super().__init__(f"harvey.db is schema {version}: run harvey upgrade (or open the "
                             "dashboard) to upgrade it to the slim database first")
        else:
            super().__init__("the upgrade's switch to the slim database is unfinished: run "
                             "harvey upgrade (or open the dashboard) to finish it")


class NewerBuild(UpgradeNeeded):
    """The database was made by a newer Harvey: this one never opens it."""

    def __init__(self, version: int, latest: int):
        RuntimeError.__init__(self, newer_build_message(version, latest))
        self.version = version


def newer_build_message(version: int, latest: int) -> str:
    return (f"harvey.db was made by a newer Harvey (schema {version}; this one reads up to "
            f"schema {latest}): update Harvey (git pull, then pip install -e .) to open it")


def _open_or_create(path: str, overlay_schema) -> int:
    """The file's schema version, creating the latest schema in a new empty
    file (WAL, so readers never block the writer), and taking a file the
    upgrade wrote (schema 25) or an earlier release made through the steps
    it lacks, in place. Two processes starting on a new file at once: one
    creates it under the write lock, the other waits for that lock (up to
    MIGRATION_TIMEOUT_SECONDS) and finds it made. A creation that fails
    rolls back and raises its own error; one whose process dies releases
    the lock and leaves the file empty for the next."""
    con = sqlite3.connect(path, isolation_level=None, timeout=MIGRATION_TIMEOUT_SECONDS)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        first = getattr(overlay_schema, "UPGRADE_TARGET", overlay_schema.VERSION)
        if version and not first <= version <= overlay_schema.VERSION:
            return version      # older (the upgrade's) or newer: never touched here
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass    # another process is setting the new file up right now
        con.execute("PRAGMA synchronous=NORMAL")
        if not version:
            con.execute("BEGIN IMMEDIATE")
            try:
                # Read again under the lock: another process may have made it.
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if not version:
                    overlay_schema.create(con)
                    version = overlay_schema.VERSION
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        if first <= version < overlay_schema.VERSION:
            return overlay_schema.step_up(con)
        return version
    finally:
        con.close()


class EarlierBuild(RuntimeError):
    """A file of this release's schema made by an earlier build of it: it
    lacks tables or columns this release reads."""


# The files whose shape was checked in this process: (path, inode, size of
# the schema). A file is checked once, again when it is replaced.
_SHAPES_CHECKED: set[tuple] = set()


def _check_shape(path: str, overlay_schema) -> None:
    try:
        st = os.stat(path)
    except OSError:
        return
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        key = (str(Path(path).resolve()), st.st_ino,
               con.execute("PRAGMA schema_version").fetchone()[0])
        if key in _SHAPES_CHECKED:
            return
        lacks = overlay_schema.missing(con)
    finally:
        con.close()
    if lacks:
        raise EarlierBuild(f"{Path(path).name} was made by an earlier build of the slim database "
                           f"and lacks {', '.join(lacks[:8])}"
                           + (f" and {len(lacks) - 8} more" if len(lacks) > 8 else "")
                           + ": move it away, rename harvey.db.v24 back to harvey.db and run "
                             "harvey upgrade again")
    _SHAPES_CHECKED.add(key)


class NotOnOverlay(RuntimeError):
    """A read the slim database (schema 25) has no version of: raised,
    never answered from tables it doesn't have."""


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)
        # The open database's schema version, read once (25: the overlay,
        # whose writes harvey/overlay/store.py makes).
        self._schema: int | None = None

    async def _on_overlay(self, method: str, *args, **kwargs):
        """overlay.store's ``method`` in a write transaction of its own, in
        a worker thread."""
        from harvey.overlay import store

        return await asyncio.to_thread(store.call, self.db_path, method, *args, **kwargs)

    async def _read(self, method: str, *args, **kwargs):
        """overlay.reads' ``method`` in a read transaction of its own, in a
        worker thread."""
        from harvey.overlay import reads

        return await asyncio.to_thread(reads.call, self.db_path, method, *args, **kwargs)

    async def recount_register_tally(self, codes=None) -> int:
        """Count the register signals over the firms in Harvey for the
        registers' current load marks (overlay.reads.recount_register_tally).
        Returns how many rows were written."""
        from harvey.overlay import reads

        return await asyncio.to_thread(reads.recount_register_tally, self.db_path, codes)

    async def jobs_waiting(self, kind: str) -> dict[str, int]:
        """{status: how many} of a job kind's queued, running and paused jobs."""
        async with self._connect() as db:
            async with db.execute(
                    "SELECT status, COUNT(*) FROM jobs WHERE kind = ? "
                    "AND status IN ('queued', 'running', 'paused') GROUP BY status",
                    (kind,)) as cur:
                return {s: n for s, n in await cur.fetchall()}

    async def read(self, method: str, *args, **kwargs):
        """A page read on the overlay (overlay.reads.Reader's ``method``):
        the dashboard's schema 25 pages."""
        return await self._read(method, *args, **kwargs)

    @asynccontextmanager
    async def _connect(self, timeout: float = BUSY_TIMEOUT_SECONDS):
        """Open a connection with sane concurrency settings.

        `timeout` maps to SQLite's busy handler, so writers wait for locks
        (e.g. while the dashboard holds a read) instead of erroring.
        """
        db = await aiosqlite.connect(self.db_path, timeout=timeout)
        try:
            yield db
        finally:
            await db.close()

    @asynccontextmanager
    async def batch(self):
        """A store.Batch: many company writes on one connection, committed
        once at the end (rolled back if the block raises). For importers
        that save thousands of companies, where opening a connection and
        committing per call is most of the time."""
        async with self._overlay_batch() as batch:
            yield batch

    @asynccontextmanager
    async def _overlay_batch(self):
        from harvey.overlay import store

        con, attached = await asyncio.to_thread(store.connect, self.db_path)
        try:
            await asyncio.to_thread(con.execute, "BEGIN IMMEDIATE")
            try:
                yield store.Batch(store.Store(con, registers=attached))
                await asyncio.to_thread(con.execute, "COMMIT")
            except BaseException:
                if con.in_transaction:
                    await asyncio.to_thread(con.execute, "ROLLBACK")
                raise
        finally:
            await asyncio.to_thread(con.close)

    async def init_db(self):
        """Create the database (the latest schema) when there is none, take
        one of schema 25 or later through the steps it lacks, and bring in
        what modules declare. Safe to call on every startup. An older
        schema is never changed here: it waits for the upgrade
        (harvey/upgrade.py), and this raises UpgradeNeeded (NewerBuild for
        a file a newer Harvey made)."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        from harvey import upgrade
        from harvey.overlay import schema as overlay_schema
        from harvey.overlay import store

        # No new empty database in place of one the upgrade is switching.
        if not Path(self.db_path).exists() and \
                await asyncio.to_thread(upgrade.switch_pending, Path(self.db_path)):
            raise UpgradeNeeded(0)
        version = await asyncio.to_thread(_open_or_create, self.db_path, overlay_schema)
        self._schema = version
        if version > overlay_schema.VERSION:
            raise NewerBuild(version, overlay_schema.VERSION)
        if version != overlay_schema.VERSION:
            raise UpgradeNeeded(version)
        await asyncio.to_thread(_check_shape, self.db_path, overlay_schema)
        # Only what modules declare is brought in (looked at first: most
        # starts have nothing to write, and a page must not wait on a
        # registry pull's write lock).
        if await asyncio.to_thread(store.call, self.db_path, "dictionaries_stale"):
            await self._on_overlay("sync_dictionaries")

    async def needs_upgrade(self) -> bool:
        """Whether the file is an older schema waiting for the upgrade, or
        the upgrade's switch is unfinished beside it (read again each time:
        the upgrade replaces the file)."""
        from harvey import upgrade

        return await asyncio.to_thread(upgrade.waiting, Path(self.db_path))

    async def wait_until_upgraded(self, stop: asyncio.Event, poll: float = 10.0) -> bool:
        """Wait while the database waits for the upgrade (a worker or the
        heartbeat never runs on an older schema). False when ``stop`` was
        set first."""
        said = False
        while await self.needs_upgrade():
            if not said:
                logger.warning("harvey.db needs the upgrade to the slim database (the "
                               "dashboard's upgrade screen, or harvey upgrade): waiting for it.")
                said = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
                return False
            except asyncio.TimeoutError:
                pass
        self._schema = None
        return not stop.is_set()

    # ── Companies ──

    async def add_company(self, company: Company) -> str:
        """Insert a company, or return the id of the one already recorded.

        Identity is the normalised domain when there is one, and the
        provider's ``external_id`` when there isn't — a business with no
        website still has to dedup across re-runs, and for anyone selling
        websites those are the best prospects on the list.
        """
        return await self._on_overlay("add_company", company)

    async def match_company(self, company: Company, extra_ids=(),
                            declared_domain: bool = False) -> "resolver.Match":
        """The company on file ``company`` is, if any (resolver.match)."""
        return await self._on_overlay("match_company", company, extra_ids, declared_domain)

    async def save_company(self, company: Company, extra_ids=()) -> Resolution:
        """Save a company from an outside source through the resolver: the
        same company on file gets its keys (or is merged with it), a
        possible one goes to the review list (_save_company)."""
        return await self._on_overlay("save_company", company, None, extra_ids)

    async def duplicates(self, status: str = "pending", limit: int = 100) -> list[dict]:
        """Pairs on the review list with ``status`` (pending | merged |
        separate), most likely first, each side as the panel shows it. The
        pending ones as the panel decides them are duplicate_groups."""
        return await self._read("duplicates", status, limit)

    async def duplicate_groups(self, limit: int = 100) -> tuple[int, list[dict]]:
        """(how many groups there are, the first ``limit``): the review list
        as resolver.groups reads it, each member as the panel shows it, the
        record to keep (``keeper``: the registry's, else the one holding
        most) and, per member, whether it is another firm than the group's
        (``conflict``: a CUI other than the keeper's, or than the first
        member's when the keeper has none, so never merged with it)."""
        return await self._read("duplicate_groups", limit)

    async def count_duplicates(self) -> int:
        """How many groups wait on the review list."""
        return await self._read("count_duplicates")

    async def merge_companies(self, keep: str, merged: str, decided_by: str = "you") -> int:
        """Merge ``merged`` into ``keep`` (resolver.merge). Raises
        resolver.MergeRefused when it can't be done."""
        return await self._on_overlay("merge_companies", keep, merged, decided_by)

    async def merge_pair(self, a: str, b: str) -> dict:
        """"Same company" on a review pair: the one with a CUI (the
        registry's record) survives; otherwise the older one."""
        return await self._on_overlay("merge_pair", a, b)

    async def merge_group(self, keep: str, members, out=(), decided_by: str = "you") -> dict:
        """"Same company" on a group: every one of ``members`` merges into
        ``keep`` (resolver.merge_group), and each of ``out`` (taken out of
        the group by the person) is marked a different company from them,
        in one transaction. Raises resolver.MergeRefused."""
        return await self._on_overlay("merge_group", keep, members, out, decided_by)

    async def separate_group(self, out, rest=()) -> int:
        """Take ``out`` out of their group: each is a different company from
        every one of ``rest`` (or, with no ``rest``, they are all different
        from one another). Raises resolver.MergeRefused for an unknown company."""
        return await self._on_overlay("separate_group", out, rest)

    async def undo_group(self, company_id: str) -> list[dict]:
        """Undo the whole group merge ``company_id`` kept or joined."""
        return await self._on_overlay("undo_group", company_id)

    async def scan_duplicates(self) -> dict:
        """Look for duplicates among every company on file (resolver.scan):
        the pairs are found with a read, and only the merges and the review
        list are written under the write lock."""
        import time

        started = time.monotonic()
        from harvey.overlay import store

        scored = await asyncio.to_thread(store.call, self.db_path, "scan_candidates")
        fresh = await asyncio.to_thread(store.call, self.db_path, "unknown_pairs", scored)
        found = time.monotonic()
        done = {"merged": 0, "review": 0}
        for at in range(0, len(fresh), SCAN_WRITE_BATCH):
            part = await self._on_overlay("apply_scan", fresh[at:at + SCAN_WRITE_BATCH])
            done = {k: done[k] + part[k] for k in done}
        return {**done, "pairs": len(scored), "read_s": round(found - started, 1),
                "write_s": round(time.monotonic() - found, 1)}

    async def separate_companies(self, a: str, b: str) -> bool:
        """"Different companies": the pair leaves the review list for good.
        Raises resolver.MergeRefused for an unknown company or a pair of one."""
        return await self._on_overlay("separate_companies", a, b)

    async def undo_merge(self, merged: str) -> dict:
        """Undo the merge that took ``merged`` (resolver.undo)."""
        return await self._on_overlay("undo_merge", merged)

    async def remove_company(self, company_id: str) -> dict:
        """Remove a firm from Harvey (overlay.store.Store.remove_company):
        refused (store.RemoveRefused) for one in a merge or named by
        outreach."""
        return await self._on_overlay("remove_company", company_id)

    async def pull_records(self, records, **kwargs) -> list[tuple[str, bool, bool, bool]]:
        """A register pull's records marked as Harvey's in one bulk write
        (overlay.store.Store.pull)."""
        return await self._on_overlay("pull", records, **kwargs)

    async def match_records(self, records) -> tuple[int, list]:
        """A register pull's records matched in a read transaction, before
        pull_records writes them (overlay.store.Store.match_records)."""
        from harvey.overlay import store

        return await asyncio.to_thread(store.call, self.db_path, "match_records", records)

    async def held_ids(self, scheme: str, keys) -> dict:
        """{key: the company holding it} for keys of an identifier scheme
        (overlay.store.Store.held)."""
        return await self._on_overlay("held", scheme, list(keys))

    async def refresh_orders(self) -> int:
        """Every firm's place in the register's name and place orders, taken
        again for its current load (overlay/orders.py)."""
        from harvey.overlay import orders

        return await asyncio.to_thread(orders.refresh_file, self.db_path)

    async def name_register_people(self, company_id: str) -> int:
        """The people a register names at a company kept as Harvey's own
        (overlay.store.Store.name_register_people)."""
        return await self._on_overlay("name_register_people", company_id)

    async def merges(self, limit: int = 50) -> list[dict]:
        """Merges still in force, newest first, both sides as the panel shows them."""
        return await self._read("merges", limit)

    async def company_brief(self, company_id: str) -> list[str]:
        """Short true facts about a company for the Writer, from its record:
        where it is, what it does (its activity when no industry is on
        file), and the latest reading of the signals that make an email
        specific (company_brief_lines). [] for a company not on file."""
        found = await self._read("company_brief", company_id, list(BRIEF_CODES))
        return company_brief_lines(*found) if found else []

    async def _overlay_company(self, method: str, *args) -> Company | None:
        """A company as the overlay's reads describe it (reads.Reader's
        ``method``: company, company_by), as a Company."""
        row = await self._read(method, *args)
        if not row:
            return None
        fields = {k: v for k, v in row.items() if k in Company.model_fields and v is not None}
        return Company(**fields)

    async def get_company(self, company_id: str) -> Company | None:
        return await self._overlay_company("company", company_id)

    async def get_company_by_domain(self, domain: str) -> Company | None:
        return await self._overlay_company("company_by", "domain", _norm(domain))

    async def get_company_by_external_id(self, external_id: str) -> Company | None:
        """Look a company up by its provider-stable id.

        The identity path for businesses with no website — which is exactly
        the cohort worth the most to anyone selling one.
        """
        if not external_id:
            return None
        return await self._overlay_company("company_by", "external_id", external_id)

    async def get_company_by_cui(self, cui: str) -> Company | None:
        """Look a company up by its registry tax code (CUI)."""
        cui = (cui or "").strip()
        if not cui:
            return None
        return await self._overlay_company("company_by", "cui", cui)

    async def fill_company_fields(self, company_id: str, **fields) -> list[str]:
        """Set the given fields where the company has none yet, and always
        set ``address``.

        For a second source describing a company already on file: what the
        first source said is kept, the gaps are filled. ``address`` is the
        exception, overwritten whenever a non-empty one is given: it is the
        registered office, the registry is the authority on it, and a
        registry re-import is how a moved office reaches the record. Returns
        the columns changed.
        """
        return await self._on_overlay("fill_company_fields", company_id, **fields)

    # ── Company activities (CAEN) and local search ──

    async def set_company_activities(
        self, company_id: str, classes, source: str, confidence: float = 1.0,
    ) -> int:
        """Make ``classes`` [(code, version)] the company's activities from
        ``source``: new ones added, ones that source no longer lists
        removed (a re-import of a newer registry dataset drops a class the
        company gave up). Other sources' rows are left alone. Returns how
        many were added."""
        return 0

    async def get_company_activities(self, company_ids) -> dict[str, list[dict]]:
        """company id -> [{code, version, revision, name, name_en, keywords,
        confidence}], by code, newest revision first. Each class once,
        whichever sources reported it."""
        from harvey.caen import revision_label

        ids = list(dict.fromkeys(company_ids))
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = await self._read("company_rows", [i for i in ids if str(i).isdigit()])
        out.update({r["id"]: r["activities"] for r in rows})
        return out

    async def search_companies(
        self, query: str, limit: int = 50, offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Companies on file matching every word of ``query``, best first,
        and how many match in all.

        Searched locally: the directory register's word index (name,
        activities, place) for the firms it describes, and Harvey's own
        index (SQLite FTS5 over the name, the industry or map category a
        source gave, and the place) for the firms no register describes. Accents
        don't matter, words of 3+ letters match as prefixes ("stomatolog"
        finds "stomatologică"), and a 4-digit code matches that class in
        every revision. Each company comes with its activities in words,
        the ones the query matched first (``matched``)."""
        match, terms = search_terms(query)
        if not match:
            return 0, []
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        total, rows = await self._read("search_companies", query, limit, offset)
        for r in rows:
            r.pop("_inputs", None)
        return total, rows

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        """The company's prospects (every record merged into it too), best
        scored first."""
        return [self._prospect_from_row(r)
                for r in await self._read("company_prospects", company_id)]

    # ── Prospects (Contacts) ──

    @staticmethod
    def _prospect_from_row(row) -> Prospect:
        d = dict(row)
        d["email_verified"] = bool(d.get("email_verified", 0))
        d["phone_verified"] = bool(d.get("phone_verified", 0))
        d["email_status"] = d.get("email_status") or ""
        d["timezone"] = d.get("timezone") or ""
        # (On schema 25 the company is the firm's integer id.)
        d["company_id"] = "" if d.get("company_id") is None else str(d["company_id"])
        return Prospect(**d)

    async def add_prospect(self, prospect: Prospect) -> str:
        """Insert a prospect. Duplicates (same email or LinkedIn URL) are not
        re-inserted; the existing record's id is returned instead."""
        if not prospect.id:
            prospect.id = _new_id()
        prospect.email = _norm(prospect.email)
        prospect.linkedin_url = (prospect.linkedin_url or "").strip()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, company_id, first_name, last_name, email, email_verified,
                    email_status, phone, phone_verified, linkedin_url, title,
                    seniority, department, source, source_url, status, score,
                    personalization_notes, company, industry, company_size,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.company_id,
                    prospect.first_name, prospect.last_name,
                    prospect.email, int(prospect.email_verified),
                    prospect.email_status,
                    prospect.phone, int(prospect.phone_verified),
                    prospect.linkedin_url, prospect.title,
                    prospect.seniority, prospect.department,
                    prospect.source, prospect.source_url,
                    prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.company, prospect.industry, prospect.company_size,
                    prospect.created_at.isoformat(),
                    prospect.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 1:
                await self._prospect_contact(prospect)
            if cursor.rowcount == 0:
                # Unique-constraint conflict: resolve to the existing record.
                for column, value in (
                    ("email", prospect.email),
                    ("linkedin_url", prospect.linkedin_url),
                    ("id", prospect.id),
                ):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM prospects WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            prospect.id = row[0]
                            break
        return prospect.id

    async def _prospect_contact(self, prospect: Prospect) -> None:
        """Mirror a prospect's address into its company's contacts (and its
        person), as schema 24's prospect triggers did."""
        if not prospect.company_id or not prospect.email:
            return
        await self._on_overlay(
            "prospect_contact", prospect.company_id, prospect.email,
            first=prospect.first_name or "", last=prospect.last_name or "",
            status=prospect.email_status or "",
            role=prospect.title or "", source=prospect.source or "",
            evidence_url=prospect.source_url or "", opted_out=prospect.status == "opted_out")

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def set_prospect_timezone(self, prospect_id: str, zone: str):
        """Remember a recipient's time zone. Leaves updated_at alone: it
        isn't a change anyone made to the prospect."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET timezone = ? WHERE id = ?", (zone, prospect_id)
            )
            await db.commit()

    async def update_prospect_status(self, prospect_id: str, status: str, *,
                                     reason: str = "opt_out", by: str = "reply",
                                     note: str = "", ref: str = ""):
        # An opt-out is permanent: nothing Harvey does later (a reply, a
        # send, a retry) may move someone off it and back into outreach.
        if status == "opted_out":
            # Their address goes on the do-not-contact list in the same
            # write (suppression.py), so it is never written to again, from
            # any list, whatever happens to this prospect card.
            await self._in_tx(self._opt_out, prospect_id, reason, by, note, ref)
            return
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? "
                "WHERE id = ? AND status IS NOT 'opted_out'",
                (status, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    @staticmethod
    def _opt_out(con, prospect_id: str, reason: str, by: str, note: str, ref: str) -> None:
        from harvey import suppression

        row = con.execute("SELECT email FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        con.execute("UPDATE prospects SET status = 'opted_out', updated_at = ? "
                    "WHERE id = ? AND status IS NOT 'opted_out'",
                    (_utcnow().isoformat(), prospect_id))
        if row and suppression.normalize_address(row[0]):
            suppression.add(con, "address", row[0], reason, by=by, note=note, ref=ref)
            # Every campaign they are in ends for them (campaign_member).
            from harvey import campaigns

            campaigns.end_memberships(con, row[0], "suppressed", reason)

    async def undo_opt_out(self, prospect_id: str, details: dict | None = None) -> bool:
        """A human's undo of an opt-out: the one way off 'opted_out'.

        They go to 'replied', not back into the sequence: Harvey sends
        nothing new on its own (the sender never stages 'replied'), and what
        the opt-out cancelled stays cancelled. Harvey's first reply to them
        after this waits for approval even on autopilot (REVIEW_NEXT_REPLY_KEY).
        The status, that flag, the opt_out_undone log entry and the soft
        undo of their address's do-not-contact entries are one write. Only
        an opt-out Harvey read in their words (opt_out, migrated: a keyword
        or the model can be wrong) is lifted here, never one you added by
        hand, and none while an entry Harvey's agent made stands (it is
        undone on the list). False, and nothing written, when they weren't opted out.
        Raises suppression.Refused, and writes nothing, while an
        unsubscribe, objection or complaint of theirs stands: that is
        undone on the do-not-contact list, with a note saying why."""
        return await self._in_tx(self._undo_opt_out, prospect_id, details or {})

    @staticmethod
    def _undo_opt_out(con, prospect_id: str, details: dict) -> bool:
        from harvey import suppression

        email, phone = (con.execute("SELECT email, phone FROM prospects WHERE id = ?",
                                    (prospect_id,)).fetchone() or ["", ""])
        # Their numbers: their card's, and the ones their SMS or WhatsApp
        # messages came from (an opt-out by STOP is a phone entry).
        phones = {suppression.normalize_phone(p) for p in [phone] + [r[0] for r in con.execute(
            "SELECT h.value FROM conversation_handle h JOIN conversations c "
            "ON c.id = h.conversation WHERE c.prospect_id = ?", (prospect_id,))]}
        phones.discard("")
        asked = suppression.standing_request(con, email) or next(
            (why for p in sorted(phones) if (why := suppression.standing_request(con, p))), "")
        if asked:
            raise suppression.Refused(
                f"they asked for it themselves ({asked}): undo it on the do-not-contact list, "
                "with a note saying why")
        if suppression.agent_entry(con, email):
            raise suppression.Refused(
                "Harvey's agent put them on the do-not-contact list under a plan: undo it on "
                "the do-not-contact list, with a note saying why")
        if con.execute("UPDATE prospects SET status = 'replied', updated_at = ? "
                       "WHERE id = ? AND status = 'opted_out'",
                       (_utcnow().isoformat(), prospect_id)).rowcount != 1:
            return False
        con.execute("""INSERT INTO settings (key, value, updated_at)
                       VALUES (?, '1', CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                           value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                    (REVIEW_NEXT_REPLY_KEY + prospect_id,))
        con.execute("INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                    (_new_id(), "opt_out_undone", "dashboard",
                     json.dumps({"prospect_id": prospect_id, **details})))
        note = str(details.get("reason") or details.get("note") or "")
        suppression.undo_address(con, email, by="you", note=note,
                                 reasons=suppression.READ_IN_THEIR_WORDS)
        for p in sorted(phones):
            suppression.undo_address(con, p, by="you", note=note,
                                     reasons=suppression.READ_IN_THEIR_WORDS)
        return True

    # ── The do-not-contact list (harvey/suppression.py) ──

    def _tx(self, fn, *args, write: bool = True):
        """``fn(connection, *args)`` in one transaction of its own (a write
        one unless ``write`` is False), in the calling thread."""
        con = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                out = fn(con, *args)
                con.execute("COMMIT")
                return out
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    async def _in_tx(self, fn, *args, write: bool = True):
        return await asyncio.to_thread(self._tx, fn, *args, write=write)

    async def suppress(self, scope: str, value, reason, *, by: str, campaign: str = "",
                       ref: str = "", note: str = "") -> int | None:
        """Add a do-not-contact entry (suppression.add): its id, or None
        when one already stands. Raises suppression.Refused."""
        from harvey import suppression

        return await self._in_tx(lambda con: suppression.add(
            con, scope, value, reason, by=by, campaign=campaign, ref=ref, note=note))

    async def suppress_phones(self, phones, reason, *, by: str, note: str = "",
                              listed_at: dict | None = None) -> tuple[int, list[str]]:
        """Numbers onto the do-not-contact list in one write (nPathi's
        opt-out list): (how many entries were new, the numbers left off
        because the user took them off the list in Harvey after
        ``listed_at`` says the list named them: {number: unix seconds})."""
        from harvey import suppression

        def run(con):
            made, kept = 0, []
            for phone in dict.fromkeys(phones):
                if listed_at is not None and suppression.undone_after(
                        con, phone, listed_at.get(phone)):
                    kept.append(phone)
                    continue
                try:
                    made += suppression.add(con, "phone", phone, reason, by=by,
                                            note=note) is not None
                except suppression.Refused:
                    continue
            return made, kept

        return await self._in_tx(run)

    async def undo_suppression(self, entry_id: int, *, by: str, note: str = "") -> bool:
        from harvey import suppression

        return await self._in_tx(lambda con: suppression.undo(con, entry_id, by=by, note=note))

    async def suppression_check(self, email: str, firm=None, config=None, *, phone: str = ""):
        """suppression.check for one address (or number: ``phone``): the
        entry (or the line of compliance.do_not_contact, read live from
        ``config``) that bars it, or None."""
        from harvey import suppression

        listed = ()
        if config is not None:
            from harvey.config import live_do_not_contact

            listed = live_do_not_contact(config)
        firms = [firm] if firm not in (None, "") else None
        return await self._in_tx(
            lambda con: suppression.check(con, email, firms, listed, phone=phone), write=False)

    async def suppressed_emails(self, emails) -> set[str]:
        """Of these addresses, the ones an entry in force bars (address,
        domain, or a firm they are linked to). The config list is the
        caller's (the exports read it already)."""
        from harvey import suppression

        emails = [e for e in dict.fromkeys(emails) if e]
        if not emails or not Path(self.db_path).exists():
            return set()

        def run(con):
            index = suppression.Index(con)
            return {e for e in emails if index and index.hit(e) is not None}

        return await self._in_tx(run, write=False)

    async def suppression_entries(self, live_only: bool = True) -> list[dict]:
        from harvey import suppression

        return await self._in_tx(lambda con: suppression.entries(con, live_only), write=False)

    # ── The cold-email acknowledgement (harvey/acknowledgement.py) ──

    async def acknowledged(self) -> bool:
        from harvey import acknowledgement

        return await self._in_tx(acknowledgement.given, write=False)

    async def acknowledge(self, by: str) -> bool:
        from harvey import acknowledgement

        return await self._in_tx(lambda con: acknowledgement.record(con, by))

    async def acknowledgement_status(self) -> dict:
        from harvey import acknowledgement

        return await self._in_tx(acknowledgement.status, write=False)

    async def personal_address(self, email: str, privacy_url: str = "") -> tuple[int, str]:
        """(email_rules' reason the address is personal, 0 when it isn't;
        the source note a first email to it carries, "" when none is
        needed, with ``privacy_url`` after it when given). Read from every
        firm the address is linked to."""
        from harvey import email_rules

        facts = await self._read("address_facts", email)
        reason = email_rules.personal_reason(email, facts)
        note = email_rules.with_privacy(email_rules.source_note(facts), privacy_url)
        return reason, note if reason else ""

    async def opt_out_history(self, prospect_id: str) -> list[dict]:
        """Every opt-out and undo recorded for this person, oldest first."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT action_type, details_json, created_at FROM actions "
                "WHERE action_type IN ('opt_out', 'opt_out_undone') "
                "AND json_valid(details_json) "
                "AND json_extract(details_json, '$.prospect_id') = ? "
                "ORDER BY created_at, rowid",
                (prospect_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [{**json.loads(d), "action": a, "at": str(at or "")} for a, d, at in rows]

    async def queue_prospect_if_unsent(self, prospect_id: str) -> bool:
        """Mark a prospect 'queued' unless something moved them on meanwhile.

        Conditional, so a status set while a campaign was being staged (the
        user marking them sent by hand, a reply) is never overwritten.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE prospects SET status = 'queued', updated_at = ? "
                "WHERE id = ? AND status IN ('new', 'queued')",
                (_utcnow().isoformat(), prospect_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_prospect_by_email(self, email: str) -> Prospect | None:
        """Look up a prospect by email address (indexed, case-insensitive)."""
        email = _norm(email)
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def update_prospect_email(
        self, prospect_id: str, email: str, email_status: str
    ):
        """Set a prospect's email + honesty status (verified/risky/guess/invalid)."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE prospects
                   SET email = ?, email_status = ?, email_verified = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    _norm(email), email_status,
                    1 if email_status == "verified" else 0,
                    _utcnow().isoformat(), prospect_id,
                ),
            )
            await db.commit()
        prospect = await self.get_prospect(prospect_id)
        if prospect is not None:
            await self._prospect_contact(prospect)

    # ── Email pattern cache (per-domain) ──

    async def get_email_pattern(self, domain: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM email_patterns WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def save_email_pattern(
        self,
        domain: str,
        pattern: str = "",
        source: str = "",
        confidence: float = 0.0,
        mx_type: str = "",
        is_catch_all: int | None = None,
    ):
        """Upsert what we've learned about a domain's email conventions.

        Only overwrites the stored pattern when the new one has equal or
        higher confidence; mx_type/is_catch_all always refresh.
        """
        domain = _norm(domain)
        if not domain:
            return
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO email_patterns
                       (domain, pattern, source, confidence, mx_type, is_catch_all, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(domain) DO UPDATE SET
                       pattern = CASE WHEN excluded.confidence >= email_patterns.confidence
                                       AND excluded.pattern != ''
                                      THEN excluded.pattern ELSE email_patterns.pattern END,
                       source = CASE WHEN excluded.confidence >= email_patterns.confidence
                                      AND excluded.pattern != ''
                                     THEN excluded.source ELSE email_patterns.source END,
                       confidence = MAX(email_patterns.confidence, excluded.confidence),
                       mx_type = CASE WHEN excluded.mx_type != ''
                                      THEN excluded.mx_type ELSE email_patterns.mx_type END,
                       is_catch_all = CASE WHEN excluded.is_catch_all != -1
                                           THEN excluded.is_catch_all
                                           ELSE email_patterns.is_catch_all END,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    domain, pattern, source, float(confidence), mx_type,
                    -1 if is_catch_all is None else int(is_catch_all),
                ),
            )
            await db.commit()

    # ── Outbox (approval ladder + native sending) ──

    async def add_outbox_item(
        self,
        *,
        prospect_id: str,
        to_email: str,
        subject: str,
        body: str,
        send_at: str,
        status: str = "pending_review",
        campaign_id: str = "",
        conversation_id: str = "",
        step: int = 1,
        kind: str = "sequence",
        provider: str = "",
        thread_ref: str = "",
        in_reply_to: str = "",
        approved_by: str = "",
        source_note: str = "",
        account_id: int | None = 1,
        error: str = "",
        channel: str = "email",
        to_phone: str = "",
    ) -> str | None:
        """Queue one outgoing email. Returns its id, or None when the
        (campaign, prospect, step) slot already exists — the double-send guard.
        ``source_note``: the line a first email to a personal address was
        staged with (email_rules; it is in the body too). ``error``: why it
        waits for you (autopilot never approves a row with one).
        ``channel``/``to_phone``: an answer on a messaging channel (sms,
        waba, wa) to that number (E.164), sent by the messenger, never by
        the email drain."""
        item_id = _new_id()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO outbox
                   (id, campaign_id, prospect_id, conversation_id, step, kind,
                    to_email, subject, body, status, send_at, provider,
                    thread_ref, in_reply_to, account_id, channel, to_phone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, campaign_id, prospect_id, conversation_id,
                    int(step), kind, _norm(to_email), subject, body,
                    status, send_at, provider, thread_ref, in_reply_to, account_id,
                    channel or "email", to_phone or "",
                ),
            )
            if cursor.rowcount > 0 and approved_by:
                await db.execute("UPDATE outbox SET approved_by = ? WHERE id = ?", (approved_by, item_id))
            if cursor.rowcount > 0 and source_note:
                await db.execute("UPDATE outbox SET source_note = ? WHERE id = ?",
                                 (source_note, item_id))
            if cursor.rowcount > 0 and error:
                await db.execute("UPDATE outbox SET error = ? WHERE id = ?", (error[:300], item_id))
            await db.commit()
            return item_id if cursor.rowcount > 0 else None

    async def get_outbox(
        self,
        status: str | None = None,
        due_before: str | None = None,
        limit: int = 200,
        exclude_kind: str | None = None,
        skip_unready_follow_ups: bool = False,
        kind: str | None = None,
        follow_ups_only: bool = False,
        skip_held_campaigns: bool = False,
        channel: str | None = None,
    ) -> list[dict]:
        """skip_unready_follow_ups: leave out sequence follow-ups whose
        previous step hasn't gone out yet, so they can't fill the page.
        kind: only that kind ("sequence" for cold email).
        follow_ups_only: only sequence emails after the first step.
        skip_held_campaigns: leave out cold email of a paused or draft
        campaign, which waits, approved, and would fill the page too.
        channel: only that channel's rows ("email" for the email drain,
        which never sends an SMS or WhatsApp answer)."""
        where, params = [], []
        if channel:
            where.append("o.channel = ?")
            params.append(channel)
        if skip_held_campaigns:
            where.append(f"NOT (o.kind = 'sequence' AND {_HELD_CAMPAIGN})")
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        if due_before:
            where.append("(send_at IS NULL OR send_at <= ?)")
            params.append(due_before)
        if exclude_kind:
            where.append("(kind IS NULL OR kind != ?)")
            params.append(exclude_kind)
        if follow_ups_only:
            where.append("o.kind = 'sequence' AND o.step > 1")
        if skip_unready_follow_ups:
            where.append(
                "NOT (o.kind = 'sequence' AND o.step > 1 AND EXISTS ("
                "SELECT 1 FROM outbox p WHERE p.prospect_id = o.prospect_id "
                "AND p.campaign_id = o.campaign_id AND p.kind = 'sequence' "
                "AND p.step = o.step - 1 "
                "AND p.status IN ('pending_review', 'approved', 'sending')))")
        sql = "SELECT * FROM outbox o"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY send_at ASC, created_at ASC LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_texts_for_prospect(self, prospect_id: str, limit: int = 50) -> list[str]:
        """Everything Harvey wrote for this person, which a reply may quote
        back: their Outbox emails (a sent one with its footer, whatever it
        said then) and the templates of campaigns they're in (hand sends,
        and what an earlier release sent through Instantly, never passed
        through the Outbox; merge tags unfilled)."""
        texts: list[str] = []
        async with self._connect() as db:
            async with db.execute(
                "SELECT body FROM outbox WHERE prospect_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (prospect_id, int(limit)),
            ) as cursor:
                texts += [r[0] or "" for r in await cursor.fetchall()]
            async with db.execute(
                "SELECT sequence_json, prospect_ids_json, id IN (SELECT campaign FROM "
                "campaign_member WHERE prospect_id = ?) FROM campaigns "
                "WHERE prospect_ids_json LIKE ? OR id IN (SELECT campaign FROM campaign_member "
                "WHERE prospect_id = ?) ORDER BY created_at DESC LIMIT ?",
                (prospect_id, "%" + prospect_id + "%", prospect_id, int(limit)),
            ) as cursor:
                rows = await cursor.fetchall()
        for sequence_json, ids_json, member in rows:
            try:
                if not member and prospect_id not in json.loads(ids_json or "[]"):
                    continue
                texts += [str(step.get("body") or "") for step in json.loads(sequence_json or "[]")
                          if isinstance(step, dict)]
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return texts

    async def count_due_cold_outbox(
        self,
        due_before: str,
        stored_verdicts: tuple[str, str, str] | None = None,
        moved_back_prefix: str = "",
        has_cached_verdict: Callable[[dict], Awaitable[bool]] | None = None,
    ) -> int:
        """Approved cold email due by ``due_before``, leaving out follow-ups
        whose previous step hasn't gone out yet (as get_outbox's
        skip_unready_follow_ups does).

        stored_verdicts: (settings key prefix, inputs, checked after) of the
        compliance check's stored verdicts; email to an address with one
        still usable doesn't wait on the check, so it's left out.
        moved_back_prefix: also count approved cold email whose error starts
        with it (moved back by the check), due or not.
        has_cached_verdict: optional per-recipient cache check, for facts
        such as country that differ between recipients. Never calls an API."""
        due = "(o.send_at IS NULL OR o.send_at <= ?)"
        params: list = [due_before]
        if moved_back_prefix:
            due = f"({due} OR substr(COALESCE(o.error, ''), 1, ?) = ?)"
            params += [len(moved_back_prefix), moved_back_prefix]
        sql = ("SELECT COUNT(*) FROM outbox o WHERE o.status = 'approved' "
               f"AND o.kind = 'sequence' AND {due} AND NOT {_HELD_CAMPAIGN} "
               "AND NOT (o.step > 1 AND EXISTS ("
               "SELECT 1 FROM outbox p WHERE p.prospect_id = o.prospect_id "
               "AND p.campaign_id = o.campaign_id AND p.kind = 'sequence' "
               "AND p.step = o.step - 1 "
               "AND p.status IN ('pending_review', 'approved', 'sending')))")
        if stored_verdicts:
            # A verdict is stored as JSON under prefix + the lower-cased
            # address, with the facts it was decided on and when.
            sql += (" AND NOT EXISTS (SELECT 1 FROM settings s "
                    "WHERE s.key = ? || lower(trim(o.to_email)) "
                    "AND CASE WHEN json_valid(s.value) "
                    "THEN json_extract(s.value, '$.inputs') END = ? "
                    "AND CASE WHEN json_valid(s.value) "
                    "THEN json_extract(s.value, '$.verdict') END "
                    "IN ('ok_to_email', 'consent_needed', 'blocked') "
                    "AND CASE WHEN json_valid(s.value) "
                    "THEN julianday(json_extract(s.value, '$.checked_at')) END >= julianday(?))")
            params += list(stored_verdicts)
        async with self._connect() as db:
            if has_cached_verdict is not None:
                db.row_factory = aiosqlite.Row
                sql = sql.replace("SELECT COUNT(*)", "SELECT o.prospect_id, o.to_email", 1)
                async with db.execute(sql, params) as cursor:
                    items = [dict(row) for row in await cursor.fetchall()]
            else:
                async with db.execute(sql, params) as cursor:
                    return (await cursor.fetchone())[0]
        # Close the query connection before the callback reads recipient facts.
        waiting = 0
        for item in items:
            if not await has_cached_verdict(item):
                waiting += 1
        return waiting

    async def get_outbox_item(self, item_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE id = ?", (item_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    _OUTBOX_COLUMNS = frozenset({
        "status", "error", "message_id", "thread_ref", "sent_at",
        "subject", "body", "send_at", "provider", "unsub_token", "source_note",
        "price_credits", "provider_status", "approved_by",
    })

    async def update_outbox_item(self, item_id: str, **kwargs):
        fields = {k: v for k, v in kwargs.items() if k in self._OUTBOX_COLUMNS}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE outbox SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _utcnow().isoformat(), item_id),
            )
            await db.commit()

    async def claim_outbox_item(self, item_id: str) -> bool:
        """Take an approved item for sending: approved → sending, atomically.

        False when it is no longer approved and due (cancelled by a reply,
        bounce or hand send, rejected, rescheduled, or taken by another
        sender), so it must not go out now. Cancels never touch a claimed item: it is already on its way.
        """
        now = _utcnow().isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'sending', updated_at = ? "
                "WHERE id = ? AND status = 'approved' "
                "AND (send_at IS NULL OR send_at <= ?)",
                (now, item_id, now),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def fail_stale_sending_outbox(
        self, older_than: timedelta = STALE_SENDING_AFTER
    ) -> int:
        """Items claimed long ago and never finished (Harvey stopped mid-send).

        Whether they went out is unknown, so they are never retried: they
        become failed with a note to check the Sent folder. A cold email's
        member ends there too (campaigns.stop_after): what it had queued
        after it is cancelled, so nothing follows an email that may or may
        not have arrived, and its campaign can finish.
        """
        from harvey import campaigns

        cutoff = (_utcnow() - older_than).isoformat()

        def run(con):
            # (Email only: an SMS or WhatsApp answer cut off is sent again
            # under its Idempotency-Key, harvey/agents/messenger.py.)
            rows = con.execute(
                "SELECT id, kind, campaign_id, to_email, step FROM outbox "
                "WHERE status = 'sending' AND channel = 'email' "
                "AND datetime(updated_at) < datetime(?)",
                (cutoff,)).fetchall()
            now = _utcnow().isoformat()
            for item_id, kind, campaign_id, to_email, step in rows:
                con.execute("UPDATE outbox SET status = 'failed', error = ?, updated_at = ? "
                            "WHERE id = ? AND status = 'sending'",
                            (STALE_SENDING_ERROR, now, item_id))
                if kind == "sequence" and campaign_id:
                    campaigns.stop_after(con, campaign_id, to_email, int(step or 1),
                                         campaigns.CUT_OFF)
            return len(rows)

        return await self._in_tx(run)

    async def edit_outbox_draft(self, item_id: str, subject: str, body: str) -> int:
        """Rewrite a draft's subject and body — only while it awaits review.

        Atomic on status, like approve: an item already approved, sent or
        rejected is never changed. Editing never approves; that stays a
        separate, explicit step. Returns the number of rows changed (0/1).
        """
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET subject = ?, body = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending_review'",
                (subject, body, _utcnow().isoformat(), item_id),
            )
            await db.commit()
            return cursor.rowcount

    async def restore_hand_cancelled_outbox(self, prospect_id: str, reason: str) -> int:
        """Undo a cancel made with `reason`: those items go back for review.

        Always to pending_review, even if they were approved before, with a
        note (RESTORED_FOR_REVIEW) that keeps autopilot from approving them:
        a human decides again. They're rescheduled from now with their original gaps,
        so a late undo never releases a whole sequence at once. Items
        cancelled for any other reason stay cancelled.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, send_at FROM outbox WHERE prospect_id = ? "
                "AND status = 'cancelled' AND error = ? ORDER BY send_at",
                (prospect_id, reason),
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
            # Their places in campaigns that stopped for the same reason
            # open again: staged where an email comes back, else ready.
            from harvey import campaigns

            await db.execute(
                "UPDATE campaign_member SET status = CASE WHEN EXISTS (SELECT 1 FROM outbox o "
                "WHERE o.campaign_id = campaign_member.campaign "
                "AND o.prospect_id = campaign_member.prospect_id AND o.status = 'cancelled' "
                "AND o.error = ?) THEN ? ELSE ? END, ended_at = NULL, end_reason = '' "
                "WHERE prospect_id = ? AND status = ? AND end_reason = ?",
                (reason, campaigns.M["staged"], campaigns.M["ready"], prospect_id,
                 campaigns.M["stopped"], reason))
            if not rows:
                await db.commit()
                return 0
            now = _utcnow()

            def _at(value):
                try:
                    return datetime.fromisoformat(str(value))
                except (TypeError, ValueError):
                    return now
            first = _at(rows[0]["send_at"])
            for r in rows:
                send_at = now + max(timedelta(0), _at(r["send_at"]) - first)
                await db.execute(
                    "UPDATE outbox SET status = 'pending_review', error = ?, approved_by = '', "
                    "send_at = ?, updated_at = ? WHERE id = ?",
                    (RESTORED_FOR_REVIEW, send_at.isoformat(), now.isoformat(), r["id"]),
                )
            await db.commit()
            return len(rows)

    async def reject_outbox_item(self, item_id: str) -> int:
        """Reject an item still waiting to go out. Mail already on its way
        or sent is never relabelled. A rejected cold email ends its member
        in the campaign (campaigns.stop_after: its later steps would never
        go after it anyway), so a campaign that doesn't roll can finish.
        Returns rows changed (0/1)."""
        from harvey import campaigns

        def run(con):
            # A note that the check will try it again no longer applies.
            n = con.execute(
                "UPDATE outbox SET status = 'rejected', updated_at = ?, "
                "error = CASE WHEN substr(COALESCE(error, ''), 1, ?) = ? THEN '' ELSE error END "
                "WHERE id = ? AND status IN ('pending_review', 'approved')",
                (_utcnow().isoformat(), len(RECHECK_PREFIX), RECHECK_PREFIX, item_id),
            ).rowcount
            row = con.execute("SELECT kind, campaign_id, to_email, step FROM outbox "
                              "WHERE id = ?", (item_id,)).fetchone() if n else None
            if row and row[0] == "sequence" and row[1]:
                campaigns.stop_after(con, row[1], row[2], int(row[3] or 1), campaigns.REJECTED)
            return n

        return await self._in_tx(run)

    async def approve_outbox(self, item_id: str | None = None) -> int:
        """Approve one pending item, or ALL pending when item_id is None.

        A human's approval (dashboard or CLI): marked approved_by 'you', so
        the autopilot review (gate.autopilot_review) doesn't hold it again,
        and any reason it was held is cleared. Cold email whose campaign is
        paused gets the note saying why it waits, as the pause gives the
        rows it finds approved (a draft has no emails to approve)."""
        async with self._connect() as db:
            now = _utcnow().isoformat()
            if item_id:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', approved_by = 'you', error = '', "
                    "updated_at = ? WHERE id = ? AND status = 'pending_review'",
                    (now, item_id),
                )
            else:
                # Not the ones autopilot held for a look (possible planted
                # links or a leak), the first answer since an opt-out was
                # undone, an answer Harvey's agent asked for, a first
                # email the privacy page leaves no room for its other link,
                # nor an SMS or WhatsApp answer (each goes to someone's
                # phone and costs credits): those are looked at one at a time.
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', approved_by = 'you', error = '', "
                    "updated_at = ? WHERE status = 'pending_review' AND channel = 'email' "
                    "AND COALESCE(error, '') NOT LIKE ? AND COALESCE(error, '') NOT IN (?, ?, ?)",
                    (now, HELD_FOR_REVIEW + "%", REVIEW_AFTER_UNDO, PRIVACY_LINK_HELD,
                     AGENT_DRAFT_HELD),
                )
            approved = cursor.rowcount
            if approved:
                await db.execute(
                    "UPDATE outbox SET error = ? "
                    "WHERE status = 'approved' AND approved_by = 'you' AND kind = 'sequence' "
                    "AND updated_at = ? AND COALESCE(error, '') = '' AND campaign_id IN "
                    "(SELECT id FROM campaigns WHERE status = 'paused')"
                    + (" AND id = ?" if item_id else ""),
                    (CAMPAIGN_PAUSED, now, *((item_id,) if item_id else ())),
                )
            await db.commit()
            return approved

    async def note_sends_held(self, reason: str):
        """The heartbeat held sending this cycle. Keeps when it started."""
        now = _utcnow().isoformat()
        # A hold that went stale (Harvey stopped) doesn't date a new one.
        since = ((await self.current_sends_held()) or {}).get("since") or now
        await self.set_setting(SENDS_HELD_KEY, json.dumps(
            {"since": since, "last_seen": now, "reason": str(reason)[:500]}))

    async def clear_sends_held(self):
        if await self.get_setting(SENDS_HELD_KEY):
            await self.delete_setting(SENDS_HELD_KEY)

    async def current_sends_held(self, max_age_hours: float = 12) -> dict | None:
        """The hold, unless Harvey hasn't seen it for ``max_age_hours`` (it
        stopped, or the hold lifted without a cycle saying so)."""
        try:
            hold = json.loads(await self.get_setting(SENDS_HELD_KEY) or "null")
            seen = datetime.fromisoformat(hold["last_seen"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return None
        if _utcnow() - seen > timedelta(hours=max_age_hours):
            return None
        return hold

    async def requeue_autopilot_approved(self) -> int:
        """Autopilot was turned off: everything it approved that hasn't gone
        out waits for you instead, and so does what was approved before
        Harvey recorded who approved ('earlier'; it can't tell). Yours stays
        approved. Returns how many."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'pending_review', approved_by = '', "
                "error = ?, updated_at = ? WHERE status = 'approved' "
                "AND approved_by IN ('autopilot', 'earlier')",
                (AUTOPILOT_OFF_REASON, _utcnow().isoformat()),
            )
            await db.commit()
            return cursor.rowcount

    async def promote_for_autopilot(self, due_before: str, limit: int,
                                    kind: str = "sequence") -> int:
        """Autopilot approves staged email when it is about to go, not when
        it was staged: due, waiting with no note (nothing held it), and for
        cold email (``kind`` sequence) in an active campaign that doesn't
        ask to review everything, a follow-up only once the step before it
        went out. So what was staged while sending was paused, or before
        autopilot was turned on, is approved by the drain that sends it
        (after the autopilot review), never by the act of staging. The same
        holds for the Handler's answers (``kind`` reply). Email only: an
        SMS or WhatsApp answer is never approved by autopilot. Returns how many."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'approved', approved_by = 'autopilot', updated_at = ? "
                "WHERE id IN (SELECT o.id FROM outbox o LEFT JOIN campaigns c "
                "ON c.id = o.campaign_id WHERE o.status = 'pending_review' AND o.channel = 'email' "
                "AND o.kind = ? AND COALESCE(o.approved_by, '') = '' "
                "AND COALESCE(o.error, '') = '' AND (o.send_at IS NULL OR o.send_at <= ?) "
                "AND (c.id IS NULL OR (c.status = 'active' "
                "AND COALESCE(c.review, 'inherit') != 'always')) "
                "AND NOT (o.kind = 'sequence' AND o.step > 1 AND EXISTS (SELECT 1 FROM outbox p "
                "WHERE p.prospect_id = o.prospect_id AND p.campaign_id = o.campaign_id "
                "AND p.kind = 'sequence' AND p.step = o.step - 1 AND p.status != 'sent')) "
                "ORDER BY o.send_at, o.created_at LIMIT ?)",
                (_utcnow().isoformat(), kind, due_before, int(limit)),
            )
            await db.commit()
            return cursor.rowcount

    async def count_queued_first_emails(self) -> int:
        """First cold emails staged and not yet sent (waiting for review or
        approved) in active campaigns: what the campaign cycle has already
        written ahead of the send cap."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox o JOIN campaigns c ON c.id = o.campaign_id "
                "WHERE o.kind = 'sequence' AND o.step <= 1 "
                "AND o.status IN ('pending_review', 'approved') AND c.status = 'active'",
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_held_for_review(self) -> int:
        """Emails autopilot sent back for a human, still waiting."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending_review' AND error LIKE ?",
                (HELD_FOR_REVIEW + "%",),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def cancel_pending_outbox_for_prospect(
        self, prospect_id: str, reason: str = "stop_on_reply"
    ) -> int:
        """Stop-on-reply: cancel everything queued for a prospect who replied."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE prospect_id = ? AND status IN ('pending_review', 'approved')",
                (reason, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()
            return cursor.rowcount

    async def stop_company(self, prospect_id: str) -> int:
        """Someone replied: mark their company as answered and cancel the
        sequence emails queued for everyone else there. Replies Harvey owes
        a colleague who wrote in themselves are not touched."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT first_name, last_name, email FROM prospects WHERE id = ?",
                (prospect_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return 0
            # (A card made for a firm that wrote in by SMS has neither.)
            who = (" ".join(x for x in (row["first_name"], row["last_name"]) if x)
                   or row["email"] or "someone who wrote in")
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (COMPANY_STOP_KEY + prospect_id, who, _utcnow().isoformat()),
            )
            cursor = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE kind = 'sequence' AND status IN ('pending_review', 'approved') "
                "AND prospect_id IN (SELECT c.id FROM prospects c, prospects p "
                f"WHERE p.id = ? AND {_colleagues('p', 'c')})",
                (f"{COLLEAGUE_REPLIED} ({who})", _utcnow().isoformat(), prospect_id),
            )
            # Their colleagues' places in every campaign stop too.
            from harvey import campaigns

            await db.execute(
                "UPDATE campaign_member SET status = ?, ended_at = ?, end_reason = ? "
                "WHERE prospect_id IN (SELECT c.id FROM prospects c, prospects p "
                f"WHERE p.id = ? AND {_colleagues('p', 'c')}) "
                f"AND status IN ({','.join('?' * len(campaigns.OPEN_MEMBER))})",
                (campaigns.M["stopped"], int(time.time()), f"{COLLEAGUE_REPLIED} ({who})"[:200],
                 prospect_id, *campaigns.OPEN_MEMBER))
            await db.commit()
            return cursor.rowcount

    async def colleague_who_replied(self, prospect_id: str) -> str | None:
        """The name of someone at this prospect's company who has replied,
        or None. A cold email to them is no longer cold: the company answered."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT s.value FROM prospects p, prospects c, settings s "
                f"WHERE p.id = ? AND s.key = '{COMPANY_STOP_KEY}' || c.id "
                f"AND {_colleagues('p', 'c')} LIMIT 1",
                (prospect_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def count_outbox_sent(self) -> int:
        """Emails sent, ever: the bounce rate's denominator (email only, so
        messages on another channel never dilute it)."""
        import harvey.overlay.queries  # noqa: F401  (registers the named statements)
        from harvey import queries

        async with self._connect() as db:
            async with db.execute(queries.sql("overlay.outbox.email_sent")) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent_today(self, kind: str | None = None,
                                      since: str | None = None) -> int:
        """Emails sent today (the UTC day, or since `since`, a naive UTC
        ISO time); only one kind ("sequence", "reply") if given. Email
        only: the daily caps are the mailbox's."""
        import harvey.overlay.queries  # noqa: F401  (registers the named statements)
        from harvey import queries

        if since:
            name, params = "overlay.outbox.email_sent_since", (since,)
        else:
            name, params = "overlay.outbox.email_sent_today", ()
        if kind:
            name, params = name + "_kind", params + (kind,)
        async with self._connect() as db:
            async with db.execute(queries.sql(name), params) as cursor:
                return (await cursor.fetchone())[0]

    async def get_step_delay_days(self, campaign_id: str, step: int) -> int | None:
        """The campaign's planned wait before `step` (days after the step
        before it), or None when the campaign or step isn't known."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT sequence_json FROM campaigns WHERE id = ?", (campaign_id or "",)
            ) as cursor:
                row = await cursor.fetchone()
        try:
            for s in json.loads((row[0] if row else None) or "[]"):
                if isinstance(s, dict) and int(s.get("step", 0)) == int(step):
                    return max(0, int(s.get("delay_days") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return None

    async def get_previous_step(self, item: dict) -> dict | None:
        """The email a sequence follow-up follows (same campaign and person,
        one step earlier), or None."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE prospect_id = ? AND campaign_id = ? "
                "AND kind = 'sequence' AND step = ? ORDER BY created_at DESC LIMIT 1",
                (item["prospect_id"], item.get("campaign_id") or "", int(item["step"]) - 1),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_outbox_by_message_id(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def sent_cold_email_to(self, email: str) -> bool:
        """Whether Harvey's outbox has sent cold email to this address."""
        if not email:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM outbox WHERE lower(to_email) = ? AND kind = 'sequence' "
                "AND status = 'sent' LIMIT 1", (email.strip().lower(),)
            ) as cursor:
                return await cursor.fetchone() is not None

    async def find_outbox_by_unsub_token(self, token: str) -> dict | None:
        """The sent cold email whose unsubscribe header carried ``token``."""
        if not token:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE unsub_token = ? ORDER BY rowid DESC LIMIT 1",
                (token.upper(),),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def prospect_written_at_domain(self, domain: str) -> Prospect | None:
        """The prospect Harvey last emailed at this domain (their address's,
        or their company's), or None: who a message from a stranger there
        is plausibly about."""
        domain = (domain or "").strip().lower()
        if not domain or domain in FREE_MAIL_DOMAINS:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT p.* FROM prospects p LEFT JOIN firm f ON f.id = p.company_id "
                "WHERE (lower(substr(p.email, instr(p.email, '@') + 1)) = ? "
                "OR lower(COALESCE(f.domain, '')) = ?) "
                "AND EXISTS (SELECT 1 FROM outbox o WHERE o.prospect_id = p.id "
                "AND o.status = 'sent') "
                "ORDER BY (SELECT MAX(o.sent_at) FROM outbox o WHERE o.prospect_id = p.id "
                "AND o.status = 'sent') DESC LIMIT 1", (domain, domain),
            ) as cursor:
                row = await cursor.fetchone()
        return self._prospect_from_row(row) if row else None

    async def last_mail_read(self, mailbox: str) -> float | None:
        """When ``mailbox`` was last read completely (unix seconds), or None."""
        try:
            return float(await self.get_setting(MAIL_READ_KEY + mailbox) or "") or None
        except ValueError:
            return None

    async def read_gap(self) -> dict | None:
        """Why cold email waits for a complete mailbox read (READ_GAP_KEY:
        since, covered_from, reason, mailbox), or None."""
        raw = await self.get_setting(READ_GAP_KEY)
        if not raw:
            return None
        try:
            gap = json.loads(raw)
        except (TypeError, ValueError):
            gap = {}
        return gap if isinstance(gap, dict) else {}

    async def accept_read_gap(self, by: str) -> bool:
        """The operator accepts that the time between the last complete read
        and what the mailbox read reached stays unread (a mailbox too big to
        read back that far): the last complete read becomes now, so the
        next read is the usual one, and cold email goes again. Logged, with
        the gap. False when there was none."""
        gap = await self.read_gap()
        if gap is None:
            return False
        if gap.get("mailbox"):
            await self.set_setting(MAIL_READ_KEY + str(gap["mailbox"]), str(int(time.time())))
        await self.delete_setting(READ_GAP_KEY)
        await self.log_action("read_gap_accepted", by, {k: gap.get(k) for k in
                                                        ("since", "covered_from", "mailbox")})
        return True

    # ── Signal vocabulary (governed: a code no module declares is refused) ──

    async def write_vocabulary(self, rows_for, settings_prefix: str) -> int:
        """Upsert the whole vocabulary in one write transaction.

        ``rows_for(settings)`` gets the settings under ``settings_prefix``
        (the module switches), read inside the transaction, and returns one
        dict per signal: its ``code``, the vocabulary columns
        (overlay.store.write_vocabulary) and, optionally, a ``status`` to
        set. A switch flipped by another process lands before this reads or
        after it commits, never in between, so the availability written
        always matches the switches. Returns how many rows were written."""
        return await self._on_overlay("write_vocabulary", rows_for, settings_prefix)

    async def write_contact_kinds(self, rows_for, settings_prefix: str) -> None:
        """The contact kinds the manifests declare (contacts.sync_kinds), with
        the switches read in the same transaction (see write_vocabulary)."""
        return await self._on_overlay("write_contact_kinds", rows_for,
                                      settings_prefix)

    async def get_signal_codes(self, available_only: bool = False) -> list[dict]:
        """The vocabulary. ``available_only`` leaves out the signals of
        modules that are off (hidden, never collected)."""
        rows = await self._read("signal_vocabulary")
        rows = [r for r in rows if r["available"]] if available_only else rows
        return sorted(rows, key=lambda r: (r.get("category") or "", r["code"]))

    async def collected_signal_codes(self) -> set[str]:
        """What collectors collect: every signal a module that is on
        declares (or one of its capabilities returns). A module's switch is
        the only on/off; what a paid source may spend is its run's cap."""
        from harvey.modules import switches
        from harvey.signals import available_codes

        return available_codes(await self.settings_with_prefix(switches.PREFIX))

    # ── Observations (every fact is a row, never a column) ──

    async def add_observation(
        self,
        signal_code: str,
        *,
        company_id: str = "",
        prospect_id: str = "",
        collector: str = "",
        value_num: float | None = None,
        value_text: str = "",
        confidence: float = 1.0,
        evidence_url: str = "",
        run_id: str = "",
        observed_at: str | None = None,
        module: str = "",
        capability: str = "",
        origin_company_id: str = "",
    ) -> str:
        """Record one fact. Raises if signal_code isn't in the vocabulary —
        a typo must fail loudly rather than create a junk signal.
        ``module``, ``capability`` and ``origin_company_id`` say who found
        it and for which record, as ingest's rows do."""
        await self.add_observations([{
            "company_id": company_id, "signal_code": signal_code, "collector": collector,
            "value_num": value_num, "value_text": value_text, "confidence": confidence,
            "evidence_url": evidence_url, "run_id": run_id, "observed_at": observed_at,
            "module": module, "capability": capability,
            "origin_company_id": origin_company_id}], run_id)
        return ""

    async def add_contacts(self, company_id: str, found: list, origin: str = "") -> int:
        """Save contacts (contacts.Found) found for a company, linked to it."""
        return await self._on_overlay("add_contacts", company_id, found, origin)

    async def add_location(self, company_id: str, **place) -> int:
        """One of a company's places (contacts.add_location)."""
        return await self._on_overlay("add_location", company_id, **place)

    async def add_company_source(self, company_id: str, source: str, ref: str = "",
                                 origin: str = "") -> None:
        return await self._on_overlay("add_company_source", company_id, source, ref,
                                      origin)

    async def add_observations(self, rows: list[dict], run_id: str = "") -> int:
        """Batch insert. Flushed per batch by callers — a long run that dies
        must not lose everything it observed."""
        return await self._on_overlay("add_observations", rows, run_id)

    async def yc_batch(self, company_id: str) -> str | None:
        """The YC batch the company says it's in ("" when it doesn't say
        which), or None when it isn't YC-backed or nobody checked. The
        latest reading wins; profiling never writes "no" over a YC-preset
        search hit, but does over its own earlier "yes"."""
        if not company_id:
            return None
        rows = await self.get_observations(company_id=company_id, signal_code="YC_BACKED",
                                           latest_only=True, limit=1)
        if rows and rows[0].get("value_num") == 1:
            return rows[0].get("value_text") or ""
        return None

    async def yc_search_hit(self, company_id: str) -> bool:
        """Did a YC-preset search find this company (its latest YC reading
        is a discovery hit)?"""
        return await self.search_hit(company_id, "YC_BACKED")

    async def search_hit(self, company_id: str, signal_code: str) -> bool:
        """Is the company's latest reading of ``signal_code`` a preset
        search's hit (collector "discover")? Profiling doesn't write "not
        stated" over one."""
        rows = await self.get_observations(company_id=company_id, signal_code=signal_code,
                                           latest_only=True, limit=1)
        return bool(rows and rows[0].get("collector") == "discover" and rows[0].get("value_num") == 1)

    async def _latest_yes(self, company_id: str, signal_code: str) -> dict | None:
        if not company_id:
            return None
        rows = await self.get_observations(company_id=company_id, signal_code=signal_code,
                                           latest_only=True, limit=1)
        return rows[0] if rows and rows[0].get("value_num") == 1 else None

    async def accelerator(self, company_id: str) -> str | None:
        """The accelerator(s) the company says it went through ("Techstars
        2024; Antler", or "" for a search hit that didn't say which), or
        None. YC is yc_batch's."""
        row = await self._latest_yes(company_id, "ACCELERATOR")
        return (row.get("value_text") or "") if row else None

    async def funding_round(self, company_id: str) -> dict | None:
        """The latest raise the company announced on its own site, as
        {"round", "amount", "date"} (any may be ""), or None."""
        row = await self._latest_yes(company_id, "FUNDING_ROUND")
        return parse_funding(row.get("value_text") or "") if row else None

    async def get_observations(
        self,
        company_id: str = "",
        signal_code: str = "",
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        if not company_id:
            raise NotOnOverlay("observations of every company aren't available on the "
                               "upgraded database: ask for one company's")
        return await self._read("observations", company_id, signal_code, latest_only, limit)

    async def signal_counts(self) -> list[dict]:
        """How many entities actually carry each signal — the cohort sizes."""
        return await self._read("signal_counts")

    async def signal_states(self, codes: list[str]) -> dict[str, dict]:
        """Per signal, how many companies read true, false and unknown — the
        same reading a cohort condition "has it" makes (see cohort_verdicts)."""
        from harvey.signals import absence

        return await self._read("signal_states", list(codes))

    async def cohort(
        self,
        require: list[str],
        exclude: list[str] | None = None,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[str]:
        """IDs of the companies that match: every required signal found, and
        every excluded one checked and not found (see cohort_verdicts)."""
        if not require:
            return []
        _, rows = await self.cohort_verdicts(
            require, exclude or [], verdicts=("match",), limit=limit,
            min_confidence=min_confidence)
        return [r["id"] for r in rows]

    async def companies_with_id(self, kind: str, company_ids=None) -> list[dict]:
        """Companies on file (not merged away) holding an identifier of
        ``kind`` (a CUI): ``{"id", "name", "value"}``, one row per
        identifier, in company id order (then value), a company's rows
        together. Only ``company_ids`` when given."""
        return await self._on_overlay("companies_with_id", kind, company_ids)

    async def ranked_company_ids(self, code: str, phrase: str) -> set[str]:
        """Companies currently ranked (or in the pack) for this search phrase."""
        return set(await self._read("ranked", code, phrase))

    async def cohort_companies(self, require: list[str], exclude: list[str],
                               offset: int = 0, limit: int | None = None,
                               ranges: list[dict] | None = None) -> tuple[int, list[dict]]:
        """The matches only, counted and paged: (match count, page)."""
        counts, rows = await self.cohort_verdicts(
            require, exclude, ranges, verdicts=("match",), offset=offset, limit=limit)
        return counts["match"], rows

    # A cohort condition is true, false or unknown for each company, never
    # just "has a row or not". "Has X" is true on a current positive reading,
    # false on a current reading that is negative (value 0: checked, none),
    # and unknown with no reading at all: nobody has looked. Excluding X is
    # the mirror image, so "no ads" means checked and none, not never
    # checked. A positive below ``min_confidence`` is unknown either way.
    # A signal whose absence is "false" in the catalog (harvey/signals.py)
    # reads a missing row as false.
    #
    # A company is a MATCH when every condition is true, NO_MATCH when any is
    # false, and POSSIBLE otherwise: nothing rules it out, but something is
    # not checked yet. Every company in the database gets a verdict; nothing
    # is left out of the three counts.
    VERDICTS = ("match", "possible", "no_match")

    async def cohort_verdicts(self, require: list[str], exclude: list[str],
                              ranges: list[dict] | None = None,
                              verdicts=("match",), offset: int = 0,
                              limit: int | None = None,
                              min_confidence: float = 0.0) -> tuple[dict, list[dict]]:
        """Every company's verdict counted, and a page of those in ``verdicts``.

        Returns ``({"match": n, "possible": n, "no_match": n}, rows)``; each
        row carries its ``verdict``. ``ranges`` are numeric filters,
        ``[{"code", "min", "max"}]`` with either bound optional: organic rank
        11-30 is ``{"code": "SERP_RANK", "min": 11, "max": 30}``. A yearly
        signal compares its latest year, or the year a ``key`` names
        (``{"code": "TURNOVER", "min": 1e6, "key": "2024"}``). Any one
        condition makes a cohort, an exclusion alone included; none at all is
        an empty cohort, never every company.
        """
        counts = dict.fromkeys(self.VERDICTS, 0)
        require = list(dict.fromkeys(require or []))
        exclude = list(dict.fromkeys(exclude or []))
        ranges = list(ranges or [])
        if not require and not exclude and not ranges:
            return counts, []
        return await self._read("cohort_verdicts", require, exclude, ranges,
                                verdicts=tuple(verdicts), offset=offset, limit=limit,
                                min_confidence=min_confidence)

    async def companies_needing_profile(
        self, limit: int = 100, stale_days: int = 90, ids=None
    ) -> list[dict]:
        """Companies a profile run should visit next.

        Never checked, or last checked longer ago than ``stale_days`` —
        ordered nulls-first so it rotates through the whole backlog instead
        of retrying the same top-N-by-created_at companies every cycle.
        ``profile_checked_at`` is set on every attempt regardless of outcome
        (see ProfileCollector.run), so an unreachable site still counts as
        checked and rotates out instead of being retried forever. A profile
        observation written any other way still counts too, whichever is newer.

        Businesses with no domain are excluded — there is no site to read.
        They are not a failure, they are the NO_WEBSITE cohort.

        ``ids`` looks only at those companies (a search's results), so a
        run's handful isn't found by reading the whole backlog.
        """
        return await self._read("needing_profile", limit, stale_days,
                                None if ids is None else list(ids))

    async def count_companies_needing_profile(self, stale_days: int = 90) -> int:
        return await self._read("needs_profile", stale_days)

    async def mark_company_profiled(self, company_id: str):
        """Record that a profile pass looked at this company, whether or
        not it was reachable — advances the rotation regardless."""
        return await self._on_overlay("mark_company_profiled", company_id)

    async def rename_company(self, company_id: str, name: str):
        """Set a company's name, and the copy each of its prospects carries."""
        return await self._on_overlay("rename_company", company_id, name)

    async def fill_company_phone(self, company_id: str, phone: str) -> bool:
        """Set a company's phone only when it has none: a listing's number
        is never overwritten by one read off the site. True when set."""
        return await self._on_overlay("fill_company_phone", company_id, phone)

    async def companies_needing_prospects(
        self, limit: int = 10, module: str = "people", capability: str = "find_people",
        since: str | None = None,
    ) -> list[dict]:
        """Companies with a domain, zero prospects, and not yet searched by
        ``module``'s ``capability`` (the searches table).

        `harvey discover` (and other company-only sources) can add companies
        without ever finding a named contact at them. This is what lets a
        prospecting pass go back and fill that in, instead of only ever
        looking at companies it stumbles into fresh in the same cycle.

        A company searched once isn't searched by the same capability
        again: not until the capability's ``refresh_days`` (its manifest)
        have passed, never when it declares none. It can still be searched
        by any other. ``since`` (a forced run's start) searches again every
        company not searched since then. Never-searched companies first,
        then the longest ago, so the whole backlog rotates.
        """
        cutoff = contacts.search_cutoff(module, capability, since)
        return await self._read("companies_needing_prospects", limit, module, capability,
                                cutoff)

    async def record_search(self, target: str, target_id, module: str, capability: str,
                            **done) -> None:
        """A search done for a record (contacts.record_search), whatever it
        found."""
        return await self._on_overlay("record_search", target, target_id, module,
                                      capability, **done)

    async def set_found_website(self, company_id: str, website: str, *, domain: str = "",
                                trust: str = "unconfirmed", source: str = "",
                                basis: dict | None = None) -> bool:
        """A website the contact finder found for a company with none, how
        sure it is (companies.website_trust) and why (``basis``: what
        confirmed it, or the classifier's yes; None while nothing checked
        it). Its domain is the company's key only when ``domain`` is given
        (a confirmed match) and no other company holds it. False when the
        company has a website already: a found one never replaces it."""
        return await self._on_overlay("set_found_website", company_id, website,
                                      domain=domain, trust=trust, source=source, basis=basis)

    async def reject_found_site(self, company_id: str, *, batch: str, detail: str = "") -> dict:
        """A found website checked and found not to be the company's, taken
        off with what its read left (overlay.store.reject_found_site)."""
        return await self._on_overlay("reject_found_site", company_id, batch=batch,
                                      detail=detail)

    async def accept_found_site(self, company_id: str, *, trust: str, basis: dict,
                                chosen: list[str] | None = None, detail: str = "") -> dict:
        """A found website checked and taken (overlay.store.accept_found_site)."""
        return await self._on_overlay("accept_found_site", company_id, trust=trust, basis=basis,
                                      chosen=chosen, detail=detail)

    async def move_found_website(self, company_id: str, website: str) -> bool:
        """A waiting found website changed to another of its lookup's
        candidates (overlay.store.move_found_website)."""
        return await self._on_overlay("move_found_website", company_id, website)

    async def undo_site_checks(self, batch: str) -> dict:
        """Put back what a re-check's rejections took off (overlay.store)."""
        return await self._on_overlay("undo_site_checks", batch)

    async def check_link(self, company_id: str, contact_id: int, *, verdict: str, batch: str,
                         profile: str = "", model: str = "", p: float | None = None,
                         note: str = "") -> dict:
        """The classifier's verdict on one link, kept for an undo
        (overlay.store.check_link)."""
        return await self._on_overlay("check_link", company_id, int(contact_id),
                                      verdict=verdict, batch=batch, profile=profile, model=model,
                                      p=p, note=note)

    async def undo_contact_checks(self, batch: str) -> dict:
        """Put back what a verification's no took (overlay.store)."""
        return await self._on_overlay("undo_contact_checks", batch)

    async def add_people(self, company_id: str, people: list[tuple[str, str]], *,
                         source: str = "", origin: str = "") -> int:
        """People named at a company ((name, role) pairs), each kept once
        by name; a second mention fills a missing role. How many were new."""
        return await self._on_overlay("add_people", company_id, people, source=source,
                                      origin=origin)

    # ── Run log (what ran, when, what it produced and cost) ──

    async def start_run(
        self, stage: str, provider: str = "", params: dict | None = None
    ) -> str:
        run_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO runs (id, stage, provider, params_json, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (run_id, stage, provider, json.dumps(params or {})),
            )
            await db.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        status: str = "completed",
        records: int = 0,
        cost_usd: float = 0.0,
        error: str = "",
    ):
        async with self._connect() as db:
            await db.execute(
                "UPDATE runs SET status = ?, records = ?, cost_usd = ?, "
                "error = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, int(records), float(cost_usd), error[:500], run_id),
            )
            await db.commit()

    async def sweep_stale_runs(self, older_than_hours: int = 6) -> int:
        """A killed collector can't close its own run — never trust it to."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE runs SET status = 'stale', ended_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running' "
                "AND started_at < datetime('now', ?)",
                (f"-{int(older_than_hours)} hours",),
            )
            await db.commit()
            return cursor.rowcount

    async def get_runs(self, limit: int = 25) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── Settings (operational flags: kill switch, counters) ──

    FOOTER_HISTORY_KEY = "compliance_footer_history"

    async def remember_footer_lines(self, lines) -> None:
        """Keep every footer line Harvey has sent under a cold email, so a
        reply quoting an old one (since reworded) is still known as ours."""
        lines = [l.strip() for l in lines if l and l.strip()]
        if not lines:
            return
        try:
            known = json.loads(await self.get_setting(self.FOOTER_HISTORY_KEY) or "[]")
        except (json.JSONDecodeError, TypeError):
            known = []
        if not isinstance(known, list):
            known = []
        merged = [l for l in known if isinstance(l, str)]
        merged += [l for l in lines if l not in merged]
        if merged != known:
            await self.set_setting(self.FOOTER_HISTORY_KEY, json.dumps(merged[-50:]))

    async def get_footer_history(self) -> list[str]:
        try:
            known = json.loads(await self.get_setting(self.FOOTER_HISTORY_KEY) or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [l for l in known if isinstance(l, str)] if isinstance(known, list) else []

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        """Every setting whose key starts with ``prefix``."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                (len(prefix), prefix),
            ) as cursor:
                return {k: v for k, v in await cursor.fetchall()}

    async def delete_setting(self, key: str):
        async with self._connect() as db:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
            await db.commit()

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (key, str(value)),
            )
            await db.commit()

    async def increment_setting(self, key: str, by: int = 1) -> int:
        """Add ``by`` to a counting setting (a value that isn't a number
        counts as 0) and return the new count: one statement, so two
        callers at once never both read the same count."""
        async with self._connect() as db:
            async with db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = CAST(CASE WHEN CAST(value AS INTEGER) || '' = TRIM(value)
                                         THEN CAST(value AS INTEGER) ELSE 0 END + ? AS TEXT),
                       updated_at = CURRENT_TIMESTAMP
                   RETURNING value""",
                (key, str(int(by)), int(by)),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        return int(row[0])

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
        async with self._connect() as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (_norm(email),)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?",
                    (linkedin_url.strip(),),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            # Name + company dedup (case-insensitive, expression-indexed)
            if first_name and last_name and company:
                async with db.execute(
                    """SELECT 1 FROM prospects
                       WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                         AND LOWER(company) = ?""",
                    (first_name.lower(), last_name.lower(), company.lower()),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_writable_prospects(self) -> int:
        """New prospects the Writer can draft for (a deliverable email)."""
        marks = ",".join("?" for _ in WRITABLE_EMAIL_STATUSES)
        async with self._connect() as db:
            async with db.execute(
                f"""SELECT COUNT(*) FROM prospects p
                    WHERE status = 'new' AND email != ''
                      AND email_status IN ({marks}) AND NOT {_company_answered('p')}
                      AND NOT (COALESCE(score, 0) > 0 AND score < ?)""",
                (*WRITABLE_EMAIL_STATUSES, LOW_SCORE),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_recent_unusable_prospects(self, hours: int = 24) -> int:
        """New prospects found in the last ``hours`` that the Writer can't use."""
        since = (_utcnow() - timedelta(hours=hours)).isoformat()
        marks = ",".join("?" for _ in WRITABLE_EMAIL_STATUSES)
        async with self._connect() as db:
            async with db.execute(
                f"""SELECT COUNT(*) FROM prospects
                    WHERE status = 'new'
                      AND NOT (COALESCE(email, '') != ''
                               AND COALESCE(email_status, '') IN ({marks}))
                      AND datetime(created_at) >= datetime(?)""",
                (*WRITABLE_EMAIL_STATUSES, since),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_writing_feedback(
        self, prospect_ids: list[str] | tuple = (), limit: int = 10
    ) -> list[dict]:
        """What the owner said about past campaigns (newest first), plus
        anything said about these contacts: the Writer follows it.

        Each row: {"about": "campaign 'Denver roofers'" | "Dana Ruiz",
        "comment": ...}."""
        out: list[dict] = []
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT f.comment, f.created_at, c.name FROM feedback f
                   LEFT JOIN campaigns c ON c.id = f.entity_id
                   WHERE f.entity_type = 'campaign' AND TRIM(f.comment) != ''
                   ORDER BY f.created_at DESC LIMIT ?""",
                (int(limit),),
            ) as cursor:
                for r in await cursor.fetchall():
                    about = f"campaign '{r['name']}'" if r["name"] else "a past campaign"
                    out.append({"about": about, "comment": r["comment"],
                                "created_at": r["created_at"]})
            ids = [i for i in prospect_ids if i][:50]
            if ids:
                marks = ",".join("?" * len(ids))
                async with db.execute(
                    f"""SELECT f.comment, f.created_at, p.first_name, p.last_name FROM feedback f
                        JOIN prospects p ON p.id = f.entity_id
                        WHERE f.entity_type = 'contact' AND f.entity_id IN ({marks})
                          AND TRIM(f.comment) != ''
                        ORDER BY f.created_at DESC LIMIT ?""",
                    (*ids, int(limit)),
                ) as cursor:
                    for r in await cursor.fetchall():
                        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x)
                        out.append({"about": name or "this contact", "comment": r["comment"],
                                    "created_at": r["created_at"]})
        out.sort(key=lambda n: str(n.get("created_at") or ""), reverse=True)  # newest first
        return out

    # ── Messages people send in: SMS, WhatsApp (harvey/agents/messages.py) ──

    async def _named(self, name: str, params: tuple = (), *, write: bool = False):
        import harvey.overlay.queries  # noqa: F401  (registers the named statements)
        from harvey import queries

        async with self._connect() as db:
            async with db.execute(queries.sql(name), params) as cursor:
                rows = await cursor.fetchall()
            if write:
                await db.commit()
        return rows

    async def phone_firms(self, phone: str) -> tuple[int | None, list[int]]:
        """(the phone contact's id, the live firms it is linked to), for a
        number in E.164; (None, []) when it isn't a contact."""
        from harvey.overlay.schema import PHONE

        rows = await self._named("overlay.messages.phone_firms", (PHONE, phone))
        contact = rows[0][0] if rows else None
        return contact, sorted({r[1] for r in rows})

    async def firm_prospect(self, firm: int, phone: str = "") -> Prospect | None:
        """A firm's prospect card for a message from it: the one with this
        number first, else the one Harvey last wrote to; None when it has none."""
        rows = await self._named("overlay.messages.firm_prospects", (int(firm), phone or ""))
        return await self.get_prospect(rows[0][0]) if rows else None

    async def handle_conversation(self, channel: str, value: str) -> Conversation | None:
        """The conversation a number's (or handle's) messages on a channel go
        to: its newest one that isn't closed, or None."""
        rows = await self._named("overlay.messages.handle_conversation", (channel, value))
        return await self.get_conversation(rows[0][0]) if rows else None

    async def put_handle(self, conversation: str, channel: str, value: str, *,
                         contact: int | None = None, firm: int | None = None,
                         at: float | None = None, ref: str = "") -> None:
        await self._named("overlay.messages.handle_put",
                          (conversation, channel, value, contact, firm,
                           int(at if at is not None else time.time()), ref), write=True)

    async def conversation_handles(self, conversation_ids) -> dict[str, dict]:
        """{conversation: {channel, value}} for those with a number or handle
        (the newest-added one when a conversation has several)."""
        ids = [c for c in dict.fromkeys(conversation_ids) if c]
        if not ids:
            return {}
        rows = await self._named("overlay.messages.handles", (json.dumps(ids),))
        return {conv: {"channel": channel, "value": value} for conv, channel, value in rows}

    # ── Answers on the messaging channels (harvey/agents/messenger.py) ──

    async def messages_waiting(self, conversation_ids) -> dict[str, dict]:
        """{conversation: {id, body, status, error, send_at}}: its SMS or
        WhatsApp answer approved and not sent yet (any one of them when
        there are several: the page offers no second while one waits)."""
        ids = [c for c in dict.fromkeys(conversation_ids) if c]
        if not ids:
            return {}
        rows = await self._named("overlay.messages.waiting", (json.dumps(ids),))
        out: dict[str, dict] = {}
        for item_id, convo, body, status, error, send_at in rows:
            out[convo] = {"id": item_id, "body": body, "status": status, "error": error or "",
                          "send_at": send_at}
        return out

    async def conversation_reply_handle(self, conversation: str) -> dict | None:
        """The number an answer to a conversation goes to: its handle that
        wrote in last ({channel, value, last_inbound_at, contact, firm}),
        or None when it has none (an email conversation)."""
        rows = await self._named("overlay.messages.reply_handle", (conversation or "",))
        if not rows:
            return None
        channel, value, at, contact, firm = rows[0]
        return {"channel": channel, "value": value, "last_inbound_at": at,
                "contact": contact, "firm": firm}

    async def last_inbound(self, channel: str, value: str) -> int | None:
        """When a number last wrote in on a channel (unix seconds), any conversation."""
        rows = await self._named("overlay.messages.last_inbound", (channel, value))
        return rows[0][0] if rows and rows[0][0] is not None else None

    async def messages_sent_to_since(self, channel: str, phone: str, since_iso: str) -> int:
        rows = await self._named("overlay.messages.sent_to_since", (phone, channel, since_iso))
        return int(rows[0][0]) if rows else 0

    async def messages_maybe_sent_to_since(self, channel: str, phone: str,
                                           since_iso: str) -> int:
        """Answers to a number failed as possibly sent (MAYBE_SENT_PREFIX) since a time."""
        rows = await self._named("overlay.messages.maybe_sent_to_since", (
            phone, channel, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX, since_iso))
        return int(rows[0][0]) if rows else 0

    async def messages_sent_today(self, channel: str) -> int:
        rows = await self._named("overlay.messages.sent_today", (channel,))
        return int(rows[0][0]) if rows else 0

    async def message_credits_since(self, since_iso: str) -> float:
        rows = await self._named("overlay.messages.credits_since", (since_iso,))
        return float(rows[0][0] or 0) if rows else 0.0

    async def unpriced_messages_since(self, since_iso: str) -> int:
        rows = await self._named("overlay.messages.credits_since", (since_iso,))
        return int(rows[0][1] or 0) if rows else 0

    async def last_message_price(self, channel: str) -> float | None:
        rows = await self._named("overlay.messages.last_price", (channel,))
        return float(rows[0][0]) if rows else None

    async def phone_line_type(self, phone: str) -> str:
        """A phone contact's line type ("" when it isn't a contact or isn't known)."""
        from harvey.overlay.schema import LINE_TYPE, PHONE

        rows = await self._named("overlay.messages.line_type", (PHONE, phone))
        names = {v: k for k, v in LINE_TYPE.items()}
        return names.get(rows[0][0], "") if rows and rows[0][0] is not None else ""

    async def unreachable(self, channel: str, phone: str) -> str:
        """nPathi's reason a channel can't reach this number, or ""."""
        rows = await self._named("overlay.messages.unreachable", (channel, phone))
        return rows[0][0] or "unreachable" if rows else ""

    async def mark_unreachable(self, channels, phone: str, reason: str) -> None:
        for channel in channels:
            await self._named("overlay.messages.mark_unreachable",
                              (channel, phone, reason, int(time.time())), write=True)

    async def stale_message_sends(self, cutoff_iso: str) -> list[str]:
        return [r[0] for r in await self._named("overlay.messages.stale_sends", (cutoff_iso,))]

    async def messages_in_flight(self, since_iso: str, limit: int = 50) -> list[dict]:
        cols = ("id", "channel", "message_id", "to_phone", "conversation_id", "prospect_id")
        return [dict(zip(cols, r)) for r in await self._named(
            "overlay.messages.in_flight", (since_iso, int(limit)))]

    async def suppressed_phones(self, phones, firms=None) -> set[str]:
        """Of these numbers (E.164), the ones an entry in force bars: the
        number itself, or a firm it reaches (``firms`` too)."""
        from harvey import suppression

        phones = [p for p in dict.fromkeys(phones) if p]
        if not phones or not Path(self.db_path).exists():
            return set()

        def run(con):
            index = suppression.Index(con)
            return {p for p in phones if index and index.phone_hit(p, firms) is not None}

        return await self._in_tx(run, write=False)

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        if not reply_id:
            return
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        """A campaign in the shape the Writer made them before campaigns
        were made by hand ('auto', still read): its prospects become its
        members (campaign_member), in the same write."""
        from harvey.overlay import schema

        if not campaign.id:
            campaign.id = _new_id()

        def run(con):
            con.execute(
                """INSERT INTO campaigns
                   (id, name, channel, sequence_json,
                    prospect_ids_json, status, created_at, origin, account_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(), campaign.origin, int(time.time()),
                ),
            )
            schema.members_from_prospects(con, campaign.id, campaign.prospect_ids,
                                          schema.MEMBER_ADDED_BY["writer"])

        await self._in_tx(run)
        return campaign.id

    # ── Audiences, campaigns made by the user, members (harvey/campaigns.py) ──

    async def campaigns_call(self, fn, *args, write: bool = True, **kwargs):
        """harvey.campaigns' ``fn(connection, *args, **kwargs)`` in one
        transaction of its own."""
        return await self._in_tx(lambda con: fn(con, *args, **kwargs), write=write)

    async def campaign_members(self, campaign_id: str, status: str = "",
                               limit: int = 500) -> list[dict]:
        from harvey import campaigns

        return await self.campaigns_call(campaigns.members, campaign_id, status, limit,
                                         write=False)

    async def campaign_member(self, campaign_id: str, email: str) -> dict | None:
        from harvey import campaigns

        return await self.campaigns_call(campaigns.member, campaign_id or "", email, write=False)

    async def set_campaign_member(self, campaign_id: str, email: str, status: str,
                                  reason: str = "", **fields) -> bool:
        from harvey import campaigns

        return await self.campaigns_call(campaigns.set_member, campaign_id, email, status,
                                         reason, **fields)

    async def end_memberships(self, email: str, status: str, reason: str) -> int:
        """Every open membership of an address ends (replied, suppressed,
        bounced), in every campaign."""
        from harvey import campaigns

        return await self.campaigns_call(campaigns.end_memberships, email, status, reason)

    async def member_sent(self, campaign_id: str, email: str, step: int) -> None:
        """A member's email went out: in sequence, or done after its last step."""
        from harvey import campaigns

        def run(con):
            m = campaigns.member(con, campaign_id or "", email)
            if m is None or m["status"] not in (campaigns.M["staged"], campaigns.M["in_sequence"]):
                return
            c = campaigns.get_campaign(con, campaign_id)
            last = max([int(s.get("step") or 0) for s in (c or {}).get("sequence") or []] or [0])
            status = "done" if last and int(step) >= last else "in_sequence"
            campaigns.set_member(con, campaign_id, email, status, "every step sent",
                                 last_step=max(int(step), m["last_step"]),
                                 last_sent_at=int(time.time()))
            if status == "done":
                campaigns.finish_if_done(con, campaign_id)

        await self._in_tx(run)

    async def campaign_record(self, campaign_id: str) -> dict | None:
        from harvey import campaigns

        return await self.campaigns_call(campaigns.get_campaign, campaign_id or "", write=False)

    async def sync_sending_account(self, config) -> int:
        """Sending account 1 as the settings say (campaigns.sync_account)."""
        from harvey import campaigns

        email = config.channels.email
        address = getattr(getattr(config, "persona", None), "email", "") or ""
        return await self.campaigns_call(campaigns.sync_account, email.provider or "", address)

    async def find_campaign_for_reply(self, email: str, in_reply_to: str = "",
                                      references: str = "") -> dict | None:
        """The sent cold email a reply answers: its In-Reply-To or
        References name it (outbox.message_id), else the latest one sent to
        that address (a provider that couldn't read the Message-ID kept its
        own id instead). None when Harvey never wrote to them."""
        for ref in [in_reply_to or ""] + (references or "").split():
            item = await self.find_outbox_by_message_id(ref.strip())
            if item and item.get("campaign_id"):
                return item
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE to_email = ? AND kind = 'sequence' "
                "AND status = 'sent' AND campaign_id != '' ORDER BY sent_at DESC LIMIT 1",
                (_norm(email),),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def reply_thread(self, prospect_id: str, convo_id: str, campaign_id: str) -> dict:
        """The thread an answer to this conversation goes into, as the
        Handler's reply_meta keeps it: the newest email sent in it (an
        answer, else the campaign's cold email to them), its subject,
        thread and message id. {} when nothing was sent."""
        import harvey.overlay.queries  # noqa: F401  (registers the named statements)
        from harvey import queries

        async with self._connect() as db:
            async with db.execute(queries.sql("overlay.agent.reply_thread"),
                                  (prospect_id, convo_id or "", campaign_id or "")) as cur:
                row = await cur.fetchone()
        if row is None:
            return {}
        return {"subject": row[0] or "", "thread_ref": row[1] or "", "message_id": row[2] or ""}

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                campaigns = []
                for r in rows:
                    d = dict(r)
                    d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                    d["prospect_ids"] = json.loads(d.pop("prospect_ids_json") or "[]")
                    campaigns.append(Campaign(**d))
                return campaigns

    async def update_campaign(self, campaign_id: str, **kwargs):
        """Update whitelisted campaign columns in a single atomic statement."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CAMPAIGN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid campaign column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*kwargs.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversation(self, convo_id: str) -> Conversation | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE id = ?", (convo_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                return Conversation(**d)

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        """Update whitelisted conversation columns atomically (bumps updated_at)."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CONVERSATION_COLUMNS
        if invalid:
            raise ValueError(f"Invalid conversation column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*kwargs.values(), _utcnow().isoformat(), convo_id),
            )
            await db.commit()

    # ── Actions Log ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {})),
            )
            await db.commit()

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign (one indexed pass over conversations)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          COUNT(v.id) as reply_count,
                          SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) as interested_count,
                          SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) as objection_count,
                          SUM(CASE WHEN v.intent = 'not_interested' THEN 1 ELSE 0 END) as not_interested_count
                   FROM campaigns c
                   LEFT JOIN conversations v ON v.campaign_id = c.id
                   WHERE c.status IN ('active', 'completed')
                   GROUP BY c.id
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    for key in ("interested_count", "objection_count", "not_interested_count"):
                        d[key] = d[key] or 0
                    prospect_ids = json.loads(d.get("prospect_ids_json") or "[]")
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Usage Tracking ──

    async def get_usage_today(self, provider: str = "claude") -> int:
        """Brain calls made today on one provider's subscription."""
        column = _USAGE_LOG_COLUMN[provider]
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                f"SELECT {column} FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0] or 0) if row else 0

    async def increment_usage(self, provider: str = "claude"):
        column = _USAGE_LOG_COLUMN[provider]
        today = date.today().isoformat()
        async with self._connect() as db:
            await db.execute(
                f"""INSERT INTO usage_log (id, date, {column})
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET {column} = {column} + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Per-call usage accounting (usage_events) ──

    async def record_usage_event(
        self,
        *,
        agent: str = "",
        task: str = "",
        session_id: str = "",
        request_key: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        num_turns: int = 0,
        is_error: bool = False,
        source: str = "result_json",
        created_at: str | None = None,
        provider: str = "claude",
    ) -> bool:
        """Insert one usage row (one model within one brain call).

        Returns False when the row was skipped as a duplicate (request_key
        uniqueness makes transcript reconciliation idempotent).
        """
        if provider not in BRAIN_PROVIDERS:
            raise ValueError(f"unknown brain provider: {provider!r}")
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO usage_events
                   (id, created_at, agent, task, session_id, request_key, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, duration_ms, num_turns,
                    is_error, source, provider)
                   VALUES (?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(), created_at, agent, task, session_id, request_key,
                    model, int(input_tokens or 0), int(output_tokens or 0),
                    int(cache_read_tokens or 0), int(cache_creation_tokens or 0),
                    float(cost_usd or 0.0), int(duration_ms or 0),
                    int(num_turns or 0), 1 if is_error else 0, source, provider,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    _USAGE_SUM = (
        "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
        "SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(cost_usd) AS cost_usd"
    )

    @staticmethod
    def _usage_row_to_dict(row: aiosqlite.Row) -> dict:
        d = dict(row)
        for key, value in d.items():
            if value is None and key != "period":
                d[key] = 0
        if "cost_usd" in d:
            d["cost_usd"] = round(float(d["cost_usd"] or 0.0), 6)
        return d

    async def _usage_grouped(self, group_expr: str, alias: str, days: int) -> list[dict]:
        sql = (
            f"SELECT {group_expr} AS {alias}, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY {alias} ORDER BY cost_usd DESC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_by_agent(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent", days
        )

    async def usage_by_task(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN task = '' THEN 'other' ELSE task END", "task", days
        )

    async def usage_by_model(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN model = '' THEN 'unknown' ELSE model END", "model", days
        )

    async def usage_by_provider(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped("provider", "provider", days)

    async def usage_by_day(self, days: int = 30) -> list[dict]:
        sql = (
            f"SELECT date(created_at) AS day, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY day ORDER BY day ASC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_totals(self) -> dict:
        """Rollups for today / last 7 days / last 30 days (UTC)."""
        totals = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for label, where in (
                ("today", "date(created_at) = date('now')"),
                ("week", "created_at >= datetime('now', '-7 days')"),
                ("month", "created_at >= datetime('now', '-30 days')"),
            ):
                async with db.execute(
                    f"SELECT {self._USAGE_SUM} FROM usage_events WHERE {where}"
                ) as cursor:
                    row = await cursor.fetchone()
                    totals[label] = self._usage_row_to_dict(row) if row else {}
        return totals

    # ── Summary for Decision Making ──

    async def get_state_summary(self) -> dict:
        prospect_counts = await self.count_prospects_by_status()
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                """SELECT
                     SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)
                   FROM campaigns"""
            ) as cursor:
                row = await cursor.fetchone()
                draft_campaigns = row[0] or 0
                active_campaigns = row[1] or 0
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                usage_row = await cursor.fetchone()
                usage_today = (usage_row[0] or 0) if usage_row else 0
                codex_today = (usage_row[1] or 0) if usage_row else 0

        return {
            "prospects": prospect_counts,
            # The 'new' prospects with an address that can be written to (a
            # guessed or invalid one stays 'new' for good).
            "writable_prospects": await self.count_writable_prospects(),
            # Unusable ones found in the last day: `harvey status` says why
            # nothing can be written when these alone fill the backlog.
            "recent_unusable_prospects": await self.count_recent_unusable_prospects(),
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "open_conversations": open_conversations,
            "usage_today": usage_today,  # Claude calls (kept for compatibility)
            "usage_today_by_provider": {"claude": usage_today, "codex": codex_today},
            # Companies discovered but not yet read. The heartbeat profiles
            # these every cycle — it is free, so it never competes with the
            # Claude budget the rest of the loop is rationing.
            "unprofiled": await self.count_companies_needing_profile(),
        }

