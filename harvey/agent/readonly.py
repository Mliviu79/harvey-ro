"""What Harvey's agent may read, and how.

Every read the agent's tools make is on a connection opened read-only
(``mode=ro``) with ``PRAGMA query_only``, the registers attached read-only
the way the pages attach them (registers.attach_read_only). A query() is
one SELECT (or WITH) statement, run under SQLite's authorizer: it may read
only, and never a table or column below, whatever the statement says (the
authorizer sees each column the compiled statement reads, through views
and subqueries too: no string matching). It stops after QUERY_SECONDS
(a progress handler) and returns at most MAX_ROWS rows.

Left out: Harvey's settings (keys, tokens), the email bodies and threads,
and any text Harvey copied from a website or an inbound email (a page's
excerpt, a reply's preview), so nothing a stranger wrote reaches the
model that holds the tools. The agent gets counts and Harvey's own
fields instead. A table or column added later is readable until it is
named here: tests/test_agent_tools.py lists every readable one, so a new
text column must be decided.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from harvey.state import BUSY_TIMEOUT_SECONDS, StateManager

logger = logging.getLogger("harvey.agent")

# Whole tables the agent never reads, each with why.
EXCLUDED_TABLES = {
    "settings": "Harvey's settings: keys, tokens and mailbox state",
    "conversations": "email threads: what prospects wrote",
    "processed_replies": "inbound message ids",
    "site_undo": "what a site check took off, with the page text it was read from",
    "agent_message": "the chat itself (the agent is given its history)",
    "agent_tool_call": "the record of the agent's own calls",
    "register_set": "compressed bitmaps, not data",
    "local_fts": "the full-text index of local firms",
    "sqlite_master": "the schema: schema() describes it",
    "sqlite_schema": "the schema: schema() describes it",
    "sqlite_temp_master": "the schema",
    "sqlite_temp_schema": "the schema",
    "sqlite_sequence": "SQLite's own counters",
}
# Tables that belong to another one (a full-text index's own tables).
_SHADOW = re.compile(r"^local_fts_")

# Columns the agent never reads, each with why: text a website or an
# inbound email supplied, an email's content, or a secret.
EXCLUDED_COLUMNS = {
    ("outbox", "subject"): "an email's subject (a reply's quotes the sender's)",
    ("outbox", "body"): "an email's body",
    ("outbox", "message_id"): "a message header",
    ("outbox", "thread_ref"): "a message header",
    ("outbox", "in_reply_to"): "a message header",
    ("outbox", "unsub_token"): "the token that unsubscribes the recipient",
    ("outbox", "error"): "a mail server's answer",
    ("actions", "details_json"): "holds previews of replies",
    ("suppression", "note"): "holds the opt-out reply's text",
    ("suppression", "undo_note"): "free text",
    ("suppression", "ref"): "a message id",
    ("obs", "text"): "text values read from websites",
    ("reading", "text"): "text values read from websites",
    ("search", "detail"): "what a search read: page excerpts and reasons",
    ("local_firm", "description"): "a listing's own description",
    ("contact_check", "note"): "the classifier's answer about page text",
    ("contact_check", "payload"): "what a check lowered, with its evidence",
    ("prospects", "personalization_notes"): "notes written from a company's website",
    ("jobs", "progress_json"): "a job's log lines, which quote what sources said",
}

FIRST_WORD = re.compile(r"^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/)|\s)*(\w+)", re.S)
STATEMENTS = ("select", "with", "values")
# What a query may call: any function but these.
DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}

MAX_ROWS = 200
ROWS_CAP = 1000
QUERY_SECONDS = 5.0
CELL_CHARS = 1000
# How often (in SQLite's virtual machine steps) the clock is looked at.
PROGRESS_STEPS = 2000

# What a refused statement would have done, by the authorizer's action code
# (the module's SQLITE_ names also hold result codes that share numbers).
_ACTIONS = ("CREATE_INDEX", "CREATE_TABLE", "CREATE_TEMP_INDEX", "CREATE_TEMP_TABLE",
            "CREATE_TEMP_TRIGGER", "CREATE_TEMP_VIEW", "CREATE_TRIGGER", "CREATE_VIEW",
            "DELETE", "DROP_INDEX", "DROP_TABLE", "DROP_TEMP_INDEX", "DROP_TEMP_TABLE",
            "DROP_TEMP_TRIGGER", "DROP_TEMP_VIEW", "DROP_TRIGGER", "DROP_VIEW", "INSERT",
            "PRAGMA", "TRANSACTION", "UPDATE", "ATTACH", "DETACH", "ALTER_TABLE", "REINDEX",
            "ANALYZE", "CREATE_VTABLE", "DROP_VTABLE", "SAVEPOINT")
_NAMES = {getattr(sqlite3, f"SQLITE_{n}"): n.lower() for n in _ACTIONS
          if isinstance(getattr(sqlite3, f"SQLITE_{n}", None), int)}
# The longest string or blob a query may make, and the most columns it may
# return: so zeroblob(), printf() or a wide SELECT can't fill the memory of
# the machine the dashboard runs on. A result stops (cut) past
# RESULT_BUDGET characters, whatever its row count.
VALUE_BYTES = 1_000_000
COLUMNS_CAP = 100
RESULT_BUDGET = 400_000


class Refused(ValueError):
    """The tool won't do this (a write, an excluded table or column, more
    than one statement, too slow): the message says why."""


def uri(path) -> str:
    return f"file:{quote(Path(path).resolve().as_posix())}?mode=ro"


def connect(db_path, *, data_dir=None) -> tuple[sqlite3.Connection, list]:
    """A read-only connection to harvey.db with every loaded register
    attached read-only (from ``data_dir``, by default the database's
    folder). Returns (connection, the adapters attached)."""
    from harvey import registers

    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    con = sqlite3.connect(uri(path), uri=True, isolation_level=None,
                          timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
    attached = []
    try:
        for adapter in registers.adapters(Path(data_dir) if data_dir else path.parent):
            try:
                if adapter.attach(con):
                    attached.append(adapter)
            except (sqlite3.Error, RuntimeError) as e:
                logger.debug("register %s not attached: %s", adapter.module, e)
    except Exception as e:
        logger.debug("registers not attached: %s", e)
    con.execute("PRAGMA query_only = 1")
    return con, attached


def schemas(con: sqlite3.Connection) -> dict[str, str]:
    """alias -> file of every database on the connection (main first)."""
    return {r[1]: r[2] for r in con.execute("PRAGMA database_list") if r[1] != "temp"}


def tables(con: sqlite3.Connection, alias: str = "main") -> list[str]:
    return [r[0] for r in con.execute(
        f"SELECT name FROM {alias}.sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite!_%' ESCAPE '!' ORDER BY name")]


def columns(con: sqlite3.Connection, table: str, alias: str = "main") -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA {alias}.table_info("{table}")')]


def excluded_table(name: str) -> str:
    """Why a harvey.db table is left out, or ""."""
    name = name.lower()
    if name in EXCLUDED_TABLES:
        return EXCLUDED_TABLES[name]
    if _SHADOW.match(name):
        return EXCLUDED_TABLES["local_fts"]
    return ""


def readable(con: sqlite3.Connection) -> dict[str, list[str]]:
    """Every harvey.db table the agent may read, with its readable columns."""
    out = {}
    for table in tables(con):
        if excluded_table(table):
            continue
        out[table] = [c for c in columns(con, table)
                      if (table, c) not in EXCLUDED_COLUMNS]
    return out


class _Gate:
    """The authorizer of one query: reads of what isn't excluded, and
    nothing else. ``why`` keeps the first refusal."""

    def __init__(self, main_tables: set[str], other_tables: set[str]):
        self.main = main_tables
        self.other = other_tables
        self.why = ""

    def deny(self, why: str) -> int:
        if not self.why:
            self.why = why
        return sqlite3.SQLITE_DENY

    def __call__(self, action, arg1, arg2, db, source) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in DENIED_FUNCTIONS:
                return self.deny(f"the function {arg2}() isn't allowed")
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return self._read((arg1 or "").lower(), (arg2 or "").lower(), db)
        return self.deny(f"only reading is allowed (this statement would "
                         f"{_NAMES.get(action, str(action)).replace('_', ' ')})")

    def _read(self, table: str, column: str, db) -> int:
        if db == "temp":
            return self.deny("temporary tables aren't read")
        if db is None:
            # (A table counted with no column named, or a WITH's own name:
            # no database. An excluded name is refused all the same.)
            why = excluded_table(table)
            if why and table not in self.other:
                return self.deny(f"the table {table} isn't readable: {why}")
            return sqlite3.SQLITE_OK
        if db != "main":
            return sqlite3.SQLITE_OK        # a register: read in full
        why = excluded_table(table)
        if why:
            return self.deny(f"the table {table} isn't readable: {why}")
        if table not in self.main:
            return self.deny(f"no readable table {table}")
        why = EXCLUDED_COLUMNS.get((table, column))
        if why:
            return self.deny(f"the column {table}.{column} isn't readable: {why}")
        return sqlite3.SQLITE_OK


def _cell(value):
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > CELL_CHARS:
        return value[:CELL_CHARS] + "…"
    return value


def check_statement(sql: str) -> str:
    """The statement, stripped, or Refused when it doesn't start as a read."""
    text = (sql or "").strip()
    if not text:
        raise Refused("give one SELECT statement")
    m = FIRST_WORD.match(text)
    if not m or m.group(1).lower() not in STATEMENTS:
        raise Refused("one SELECT (or WITH ... SELECT) statement only")
    return text


def run_query(con: sqlite3.Connection, sql: str, *, limit: int = MAX_ROWS,
              seconds: float = QUERY_SECONDS, params=()) -> dict:
    """One read on ``con`` (a connect() connection): {"columns", "rows",
    "truncated"}, at most ``limit`` rows (ROWS_CAP at most). Refused for
    anything else: a write, an ATTACH or a PRAGMA, an excluded table or
    column, two statements, or a statement still running after
    ``seconds``."""
    text = check_statement(sql)
    limit = max(1, min(int(limit or MAX_ROWS), ROWS_CAP))
    names = {a: set(tables(con, a)) for a in schemas(con)}
    gate = _Gate({t.lower() for t in names.pop("main", set())},
                 {t.lower() for ts in names.values() for t in ts})
    deadline = time.monotonic() + float(seconds)
    late = []

    def progress():
        if time.monotonic() > deadline:
            late.append(True)
            return 1
        return 0

    was = (con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), con.getlimit(sqlite3.SQLITE_LIMIT_COLUMN))
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, VALUE_BYTES)
    con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, COLUMNS_CAP)
    con.set_authorizer(gate)
    con.set_progress_handler(progress, PROGRESS_STEPS)
    rows, size, truncated = [], 0, False
    try:
        try:
            cur = con.execute(text, tuple(params))
            # Row by row, each cell cut as it comes: at most ``limit`` rows
            # and RESULT_BUDGET characters held.
            while True:
                row = cur.fetchone()
                if row is None:
                    break
                if len(rows) >= limit or size > RESULT_BUDGET:
                    truncated = True
                    break
                cells = [_cell(v) for v in row]
                size += sum(len(c) if isinstance(c, str) else 8 for c in cells)
                rows.append(cells)
        except (sqlite3.Error, sqlite3.Warning) as e:
            if gate.why:
                raise Refused(gate.why) from None
            if late:
                raise Refused(f"the query ran longer than {seconds:g} s: narrow it "
                              "(a WHERE on an indexed column, a LIMIT)") from None
            if "one statement" in str(e):
                raise Refused("one statement at a time") from None
            if isinstance(e, sqlite3.DataError) or "too big" in str(e) or "too many columns" in str(e):
                raise Refused(f"a value or a row is too large ({e}): at most "
                              f"{VALUE_BYTES:,} bytes a value and {COLUMNS_CAP} columns") from None
            raise Refused(f"SQLite: {e}") from None
        cols = [d[0] for d in cur.description or ()]
    finally:
        con.set_authorizer(None)
        con.set_progress_handler(None, 0)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, was[0])
        con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, was[1])
    return {"columns": cols, "rows": rows, "truncated": truncated}


def call_reader(db_path, method: str, *args, **kwargs):
    """``overlay.reads.Reader(...).method(...)`` on a read-only connection,
    in one read transaction."""
    from harvey.overlay import reads

    con, attached = connect(db_path)
    try:
        reader = reads.Reader(con, attached)
        con.execute("BEGIN")
        try:
            return getattr(reader, method)(*args, **kwargs)
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
    finally:
        con.close()


class ReadOnlyState(StateManager):
    """A StateManager that can only read: its connections are opened
    read-only, its page reads go through call_reader, and it never
    creates or steps up a database. Harvey's own read functions (the
    estimates, the jobs' lists) run on it unchanged; a write they would
    make fails instead of happening."""

    @asynccontextmanager
    async def _connect(self, timeout: float = BUSY_TIMEOUT_SECONDS):
        import aiosqlite

        db = await aiosqlite.connect(uri(self.db_path), uri=True, timeout=timeout)
        try:
            await db.execute("PRAGMA query_only = 1")
            yield db
        finally:
            await db.close()

    async def _read(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(call_reader, self.db_path, method, *args, **kwargs)

    async def _on_overlay(self, method: str, *args, **kwargs):
        raise Refused(f"the agent's tools only read ({method} writes)")

    @asynccontextmanager
    async def _overlay_batch(self):
        raise Refused("the agent's tools only read")
        yield   # pragma: no cover

    def _tx(self, fn, *args, write: bool = True):
        """``fn(connection, *args)`` on a read-only connection: a write it
        makes fails (the do-not-contact list's and the acknowledgement's
        own reads come through here)."""
        con = sqlite3.connect(uri(self.db_path), uri=True, isolation_level=None,
                              timeout=BUSY_TIMEOUT_SECONDS)
        try:
            con.execute("PRAGMA query_only = 1")
            con.execute("BEGIN")
            try:
                return fn(con, *args)
            finally:
                if con.in_transaction:
                    con.execute("ROLLBACK")
        finally:
            con.close()

    async def init_db(self):
        """Nothing to create: the database is Harvey's, opened as it is."""
        return None
