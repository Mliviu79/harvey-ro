"""The upgrade of harvey.db from schema 24 to schema 25 (the overlay).

Reads a schema 24 database (never writes it: attached read-only) and
writes a new schema 25 file beside it. Only what Harvey added moves over:
firms with new integer ids and their identifiers, websites with their
trust, contacts and every piece of link evidence, the people Harvey found
or acted on, searches, sources, web signals (history only on change),
merges with their members and candidates, and the outreach tables. Copies
of a register (its facts, administrators, activities, the search index,
its phone and name keys) are left behind: the register modules read them
in place.

Every step is chunked, and each chunk commits together with its
checkpoint (``migrate_progress``), so a run that is killed resumes where it
stopped and ends with the same file as one run. Everything is written with
the kept-value triggers off and recounted once at the end (bulk mode,
recount.py); ``harvey doctor --recount`` then finds 0 rows differing.

What an upgrade keeps aside instead of deciding for the user goes into
``migration_note``: a held firm's name that differs from its register's
(the display name stays the register's until the user says otherwise), an
identifier no module declares, a value with no column in schema 25.

Firm ids are assigned in creation order. A scratch file (``<dst>.work``)
holds the old-to-new id map while the run lasts; it is deleted at the end.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from harvey.overlay import recount, schema, triggers

# The upgrade refuses to start with less free space than this on the disk
# it writes to (a full disk can remount it read-only).
MIN_FREE_BYTES = 30 * 10**9
CHUNK = 50_000
OBS_CHUNK = 250_000

# Schema 24 keys a firm's identifiers by these company_ids kinds; the
# company column that holds a record's own key of the kind.
OWN_COLUMN = {"cui": "cui", "external": "external_id"}
TRUST_CASE = ("CASE {x} WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 "
              "WHEN 'unconfirmed' THEN 2 ELSE NULL END")
LINE_CASE = ("CASE {x} " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in schema.LINE_TYPE.items())
             + " ELSE NULL END")
STATUS_CASE = ("CASE {x} " + " ".join(f"WHEN '{k}' THEN {v}"
                                      for k, v in schema.CONTACT_STATUS.items())
               + " ELSE NULL END")
_HEX_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")

# Tables copied as they are (columns both schemas have), and the text
# columns of each that may name an old company id (legacy_id).
COPIED = ("campaigns", "outbox", "conversations", "processed_replies", "actions", "runs",
          "usage_log", "usage_events", "settings", "email_patterns", "jobs", "job_batches",
          "feedback")
NAMES_IDS = {"jobs": ("params_json", "progress_json", "result_json"),
             "job_batches": ("params_json",), "actions": ("details_json",),
             "settings": ("value",), "runs": ("params_json",), "feedback": ("entity_id",)}

PROGRESS = """
CREATE TABLE IF NOT EXISTS migrate_progress (
  step TEXT PRIMARY KEY, done INTEGER NOT NULL DEFAULT 0, last INTEGER NOT NULL DEFAULT 0,
  rows INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0, detail TEXT
)
"""


# The schema 24 tables the upgrade reads (besides COPIED): a fingerprint of
# them is kept on the first run and checked on every resume and once all of
# them are read, so a file written to meanwhile is never half carried over.
READ_TABLES = ("companies", "company_ids", "company_merges", "company_merge_moves",
               "match_candidates", "contacts", "contact_kinds", "company_contacts", "people",
               "searches", "company_sources", "observations", "locations", "prospects",
               "signal_codes")
# Columns a write to an existing row changes, where a table has one.
CHANGED_COLUMNS = ("updated_at", "last_seen", "undone_at")


class Refused(RuntimeError):
    """The upgrade won't start (or resume): too little disk, a file that
    isn't a schema 24 database, an output that is something else."""


def unix(x: str) -> str:
    """SQL: a schema 24 timestamp (text) as unix seconds, NULL for none."""
    return f"CAST(strftime('%s', NULLIF({x}, '')) AS INTEGER)"


def free_bytes(path: Path) -> int:
    """Free space for a file written at ``path``: the least of its own disk
    and, on a virtual disk, the drive that disk lives on (disk.free_bytes)."""
    from harvey import disk

    return disk.free_bytes(path)


def fingerprint(con: sqlite3.Connection, db: str = "old") -> dict:
    """Per table the upgrade reads: rows, the highest rowid and the latest
    change time (where it has one). Equal fingerprints: nothing was added,
    removed or (by those columns) changed."""
    out = {}
    for table in (*READ_TABLES, *COPIED):
        cols = {r[1] for r in con.execute(f"PRAGMA {db}.table_info({table})")}
        if not cols:
            continue
        parts = ["COUNT(*)", "MAX(rowid)"] + [f"MAX({c})" for c in CHANGED_COLUMNS if c in cols]
        out[table] = list(con.execute(f"SELECT {', '.join(parts)} FROM {db}.{table}").fetchone())
    return out


def live_worker(con: sqlite3.Connection, db: str = "old") -> bool:
    """Whether a worker holds a fresh lease on the schema 24 file (it would
    keep writing to it while the upgrade reads it)."""
    from harvey.modules.worker import LEASE_TTL_SECONDS

    if not con.execute(f"SELECT 1 FROM {db}.sqlite_master WHERE name = 'worker_lease'").fetchone():
        return False
    return con.execute(f"SELECT 1 FROM {db}.worker_lease WHERE heartbeat_at >= ?",
                       (time.time() - LEASE_TTL_SECONDS,)).fetchone() is not None


def _uri(path: Path, *, read_only: bool = False, immutable: bool = False) -> str:
    """A file's URI (a ``file:`` URI given is used as it is)."""
    if str(path).startswith("file:"):
        return str(path)
    uri = "file:" + quote(str(Path(path).resolve()))
    if read_only:
        uri += "?mode=ro" + ("&immutable=1" if immutable else "")
    return uri


# ── Reading the place line of a firm no register describes ──

def place_parts(location: str) -> tuple[str, str]:
    """(locality, county) from a schema 24 place line: "Town, County,
    Country" (the country, a third part, is left out), "Town, Region", or
    a town alone. A free text line: the split is what the line says, no
    more."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) >= 3:
        return parts[0], ", ".join(parts[1:-1])
    return parts[0], parts[1] if len(parts) == 2 else ""


# ── The run ──

class Run:
    def __init__(self, con: sqlite3.Connection, *, chunk: int, obs_chunk: int, manifests,
                 registers, on_chunk):
        self.con, self.chunk, self.obs_chunk = con, chunk, obs_chunk
        self.manifests, self.registers, self.on_chunk = manifests, registers or [], on_chunk
        self.register_modules = {m.id for m in manifests if m.register}
        self.keyed_by = {m.register["keyed_by"] for m in manifests if m.register}
        # Every scheme a register module declares: a firm keyed by one is
        # described by its register (by tax code, or by registration number
        # for a firm with none), the others locally. Per module, its own
        # key first.
        self.register_schemes: dict[str, list[str]] = {
            m.id: [m.register["keyed_by"]] + sorted(
                i["name"] for i in m.identifiers if i["name"] != m.register["keyed_by"])
            for m in manifests if m.register}
        self.report: dict = {}

    # Checkpoints

    def state(self, step: str) -> tuple[int, int]:
        row = self.con.execute("SELECT done, last FROM migrate_progress WHERE step = ?",
                               (step,)).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def _save(self, step: str, *, last: int, rows: int, seconds: float, done: int,
              detail: dict | None = None) -> None:
        self.con.execute(
            "INSERT INTO migrate_progress (step, done, last, rows, seconds, detail) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (step) DO UPDATE SET done = excluded.done, "
            "last = excluded.last, rows = rows + excluded.rows, seconds = seconds + excluded.seconds, "
            "detail = COALESCE(excluded.detail, detail)",
            (step, done, last, rows, seconds, json.dumps(detail) if detail else None))

    def atomic(self, step: str, body) -> None:
        """A step that runs in one transaction with its checkpoint."""
        if self.state(step)[0]:
            return
        started, before = time.monotonic(), self.con.total_changes
        self.con.execute("BEGIN IMMEDIATE")
        try:
            detail = body()
            self._save(step, last=0, rows=self.con.total_changes - before,
                       seconds=time.monotonic() - started, done=1, detail=detail)
            self.con.execute("COMMIT")
        except BaseException:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            raise
        if self.on_chunk:
            self.on_chunk(step, 0)

    def chunked(self, step: str, keys: str, body, size: int | None = None) -> None:
        """A step over integer keys (``keys``: a query selecting ``k``,
        each once), ``size`` at a time: ``body(lo, hi)`` writes the rows
        for lo < k <= hi, committed with the checkpoint."""
        done, last = self.state(step)
        if done:
            return
        size = size or self.chunk
        while True:
            hi = self.con.execute(f"SELECT MAX(k) FROM (SELECT k FROM ({keys}) WHERE k > ? "
                                  f"ORDER BY k LIMIT ?)", (last, size)).fetchone()[0]
            started, before = time.monotonic(), self.con.total_changes
            self.con.execute("BEGIN IMMEDIATE")
            try:
                if hi is not None:
                    body(last, hi)
                self._save(step, last=hi if hi is not None else last,
                           rows=self.con.total_changes - before,
                           seconds=time.monotonic() - started, done=int(hi is None))
                self.con.execute("COMMIT")
            except BaseException:
                if self.con.in_transaction:
                    self.con.execute("ROLLBACK")
                raise
            if hi is None:
                return
            last = hi
            if self.on_chunk:
                self.on_chunk(step, hi)

    # Helpers

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.con.execute(sql, params)

    def one(self, sql: str, params=()):
        return self.con.execute(sql, params).fetchone()[0]

    def names_into(self, table: str, select: str, params=()) -> None:
        """Add the names ``select`` gives to a name dictionary (module, source)."""
        self.x(f"INSERT OR IGNORE INTO {table} (name) SELECT DISTINCT n FROM ({select}) "
               f"WHERE n IS NOT NULL AND n != ''", params)

    def batches_into(self, select: str, params=()) -> None:
        self.x("INSERT OR IGNORE INTO batch (ref) SELECT DISTINCT n FROM (" + select + ") "
               "WHERE n IS NOT NULL AND n != ''", params)

    def firm_of(self, uuid: str, origin: str | None = None, moved: str | None = None) -> str:
        """SQL: the new id of the record a row belongs to: a move logged by
        a merge (the record it came from), else its origin, else its company."""
        parts = []
        if moved:
            parts.append(moved)
        if origin:
            parts.append(f"(SELECT firm FROM work.map WHERE uuid = NULLIF({origin}, ''))")
        parts.append(f"(SELECT firm FROM work.map WHERE uuid = {uuid})")
        return f"COALESCE({', '.join(parts)})" if len(parts) > 1 else parts[0]


def migrate(src, dst, *, chunk: int = CHUNK, obs_chunk: int = OBS_CHUNK,
            min_free: int = MIN_FREE_BYTES, immutable: bool = False, manifests=None,
            registers=None, attach: dict | None = None, on_chunk=None) -> dict:
    """Write schema 25 from the schema 24 database ``src`` into ``dst``
    (resuming a run that was cut short). ``registers``: register adapters
    to compare held firms' names with (their files attached read-only, with
    ``attach``: alias -> path of further read-only files, such as derived
    tables built elsewhere). ``on_chunk(step, last)`` is called after each
    committed chunk. Returns the per-step report."""
    from harvey.modules.loader import load_manifests

    src, dst = Path(src), Path(dst)
    work = Path(str(dst) + ".work")
    if not src.is_file():
        raise Refused(f"no database at {src}")
    if min_free:
        from harvey.disk import HostDriveUnknown

        try:
            free = free_bytes(dst)
        except HostDriveUnknown as e:
            raise Refused(str(e)) from e
        if free < min_free:
            raise Refused(f"{free / 1e9:.1f} GB free where {dst.name} would be written; "
                          f"the upgrade needs at least {min_free / 1e9:.0f} GB")
    manifests = list(manifests if manifests is not None else load_manifests())
    old = sqlite3.connect(_uri(src, read_only=True, immutable=immutable), uri=True)
    try:
        found = old.execute("PRAGMA user_version").fetchone()[0]
    finally:
        old.close()
    if found != 24:
        raise Refused(f"{src.name} is schema {found}; this upgrade reads schema 24")
    fresh = not dst.exists()
    if not fresh:
        probe = sqlite3.connect(_uri(dst, read_only=True), uri=True)
        try:
            has = probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = "
                                "'migrate_progress'").fetchone()[0]
            done = has and probe.execute("SELECT done FROM migrate_progress WHERE step = 'finish'"
                                         ).fetchone()
        finally:
            probe.close()
        if not has:
            raise Refused(f"{dst} exists and isn't an upgrade's output: move it away first")
        if done and done[0]:
            return _report(dst)
    if fresh:
        _create(dst)
        work.unlink(missing_ok=True)
    con = sqlite3.connect(_uri(dst), uri=True, isolation_level=None)
    try:
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA cache_size=-400000")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("ATTACH DATABASE ? AS old", (_uri(src, read_only=True, immutable=immutable),))
        con.execute("ATTACH DATABASE ? AS work", (_uri(work),))
        con.execute("PRAGMA work.synchronous=NORMAL")
        for alias, path in (attach or {}).items():
            con.execute(f"ATTACH DATABASE ? AS {alias}", (_uri(path, read_only=True),))
        for adapter in registers or []:
            adapter.attach(con)
        con.create_function("harvey_place", 2, lambda loc, i: place_parts(loc)[i],
                            deterministic=True)
        if live_worker(con):
            raise Refused("a worker is running jobs on this database: stop Harvey (the "
                          "dashboard and harvey worker) before upgrading it")
        run = Run(con, chunk=chunk, obs_chunk=obs_chunk, manifests=manifests,
                  registers=registers, on_chunk=on_chunk)
        _source_unchanged(run)
        for step in STEPS:
            step(run)
    finally:
        con.close()
    work.unlink(missing_ok=True)
    Path(str(work) + "-journal").unlink(missing_ok=True)
    return _report(dst)


def _create(dst: Path) -> None:
    """The empty schema 25 file, written aside and renamed into place (a
    run killed while creating it leaves no half-made output)."""
    part = Path(str(dst) + ".part")
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(part) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(part, isolation_level=None)
    try:
        schema.create(con, triggers=False)
        con.execute(PROGRESS)
    finally:
        con.close()
    os.replace(part, dst)


def _report(dst: Path) -> dict:
    con = sqlite3.connect(_uri(dst, read_only=True), uri=True)
    try:
        rows = con.execute("SELECT step, done, rows, seconds, detail FROM migrate_progress "
                           "ORDER BY rowid").fetchall()
    finally:
        con.close()
    return {step: {"done": bool(done), "rows": n, "seconds": round(s, 1),
                   **({"detail": json.loads(d)} if d else {})}
            for step, done, n, s, d in rows}


# ── Steps ──

def _source_unchanged(run: Run, step: str = "source") -> None:
    """Keep the source's fingerprint on the first run; on a resume (and
    with ``step`` ``source_read``, once every table was read) refuse when it
    changed: rows read before and after a write would not fit together."""
    now = fingerprint(run.con)
    row = run.con.execute("SELECT detail FROM migrate_progress WHERE step = 'source'").fetchone()
    if row is None:
        run.x("BEGIN IMMEDIATE")
        run._save("source", last=0, rows=0, seconds=0, done=1, detail=now)
        run.x("COMMIT")
        return
    kept = json.loads(row[0])
    if kept != now:
        changed = sorted(t for t in set(kept) | set(now) if kept.get(t) != now.get(t))
        raise Refused(f"the database was written to after this upgrade started reading it "
                      f"({', '.join(changed)}): delete the unfinished output and upgrade again "
                      f"with Harvey stopped")
    if step != "source" and not run.state(step)[0]:
        run.x("BEGIN IMMEDIATE")
        run._save(step, last=0, rows=0, seconds=0, done=1)
        run.x("COMMIT")


def _source_read(run: Run) -> None:
    _source_unchanged(run, "source_read")


def _vocabulary(run: Run) -> None:
    def body():
        con = run.con
        for m in run.manifests:
            schema.dict_id(con, "module", m.id)
            for cap in m.capabilities:
                schema.capability_id(con, m.id, cap["id"])
        schemes = schema.sync_identifiers(con, run.manifests)
        rows = [dict(zip([d[0] for d in cur.description], r))
                for cur in [con.execute("SELECT * FROM old.signal_codes")] for r in cur.fetchall()]
        schema.sync_signals(con, rows, run.manifests)
        # Contact kinds: the core two keep their fixed ids.
        core = {k: i for i, k, *_ in schema.CORE_KINDS}
        for kind, label, channel, module, available in con.execute(
                "SELECT kind, label, channel, module, available FROM old.contact_kinds ORDER BY rowid"
        ).fetchall():
            if kind in core:
                con.execute("UPDATE contact_kinds SET label = ?, channel = ?, module = ?, "
                            "available = ? WHERE id = ?", (label, channel, module, available,
                                                           core[kind]))
            else:
                con.execute("INSERT OR IGNORE INTO contact_kinds (kind, label, channel, module, "
                            "available) VALUES (?, ?, ?, ?, ?)",
                            (kind, label, channel, module, available))
        con.execute("INSERT OR IGNORE INTO contact_kinds (kind) SELECT DISTINCT kind "
                    "FROM old.contacts")
        # Roles the kept values recognise: a website read, the contact
        # finder's search, and the looked_for bit of a deep read (one that
        # looked for the owner).
        schema.set_role(con, "site_read", schema.capability_id(con, "website", "read_site"))
        schema.set_role(con, "finder", schema.capability_id(con, "contact_finder",
                                                            "find_contacts"))
        schema.set_role(con, "deep_bit", schema.looked_for_bit(con, "owner"))
        # A bare capability name (schema 24 kept no module beside it) ->
        # the capability a manifest declares under that name.
        con.execute("CREATE TABLE work.capname (name TEXT PRIMARY KEY, id INTEGER NOT NULL)")
        for m in run.manifests:
            for cap in m.capabilities:
                con.execute("INSERT OR IGNORE INTO work.capname VALUES (?, ?)",
                            (cap["id"], schema.capability_id(con, m.id, cap["id"])))
        # Moves schema 24 merges logged: the record a moved row came from.
        con.execute("CREATE TABLE work.moved (tbl TEXT NOT NULL, key TEXT NOT NULL, "
                    "uuid TEXT NOT NULL, PRIMARY KEY (tbl, key)) WITHOUT ROWID")
        con.execute("""
            INSERT OR IGNORE INTO work.moved
            SELECT CASE mm.tbl WHEN 'company_ids' THEN 'ids:' || mm.key1 ELSE mm.tbl END,
                   CASE mm.tbl WHEN 'company_ids' THEN mm.key2 ELSE mm.key1 END, m.merged
            FROM old.company_merge_moves mm JOIN old.company_merges m ON m.id = mm.merge_id
            WHERE m.undone_at IS NULL AND mm.tbl IN ('company_ids', 'observations', 'prospects')
            ORDER BY m.id""")
        for merged, moved in con.execute("SELECT merged, moved_json FROM old.company_merges "
                                         "WHERE undone_at IS NULL ORDER BY id").fetchall():
            try:
                items = json.loads(moved or "{}")
            except ValueError:
                items = {}
            for column, value in items.items():
                if isinstance(value, str) and value:
                    con.execute("INSERT OR IGNORE INTO work.moved VALUES (?, ?, ?)",
                                ("col:" + column, value, merged))
                    if column in OWN_COLUMN.values():
                        kind = next(k for k, c in OWN_COLUMN.items() if c == column)
                        con.execute("INSERT OR IGNORE INTO work.moved VALUES (?, ?, ?)",
                                    ("ids:" + kind, value, merged))
        # People some link names (kept whatever their source).
        con.execute("CREATE TABLE work.refperson (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO work.refperson SELECT DISTINCT person_id FROM old.company_contacts "
                    "WHERE person_id != 0")
        return {"schemes": len(schemes)}

    run.atomic("vocabulary", body)


def _map(run: Run) -> None:
    def body():
        run.x("CREATE TABLE work.map (firm INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, "
              "crow INTEGER NOT NULL)")
        run.x(f"""
            INSERT INTO work.map (firm, uuid, crow)
            SELECT ROW_NUMBER() OVER (ORDER BY COALESCE({unix('created_at')}, 0), id), id, rowid
            FROM old.companies""")

    run.atomic("map", body)


def _firms(run: Run) -> None:
    def body(lo, hi):
        sel = "SELECT c.website_source AS n FROM work.map m JOIN old.companies c ON c.rowid = m.crow " \
              "WHERE m.firm > ? AND m.firm <= ?"
        run.names_into("source", sel, (lo, hi))
        run.x(f"""
            INSERT INTO firm (id, live, created, website, domain, website_trust, website_source,
                              profiled_at)
            SELECT m.firm, 0, COALESCE({unix('c.created_at')}, 0), NULLIF(c.website, ''),
                   CASE WHEN COALESCE(mv.uuid, '') = '' THEN NULLIF(c.domain, '') END,
                   {TRUST_CASE.format(x='c.website_trust')},
                   (SELECT id FROM source WHERE name = NULLIF(c.website_source, '')),
                   {unix('c.profile_checked_at')}
            FROM work.map m JOIN old.companies c ON c.rowid = m.crow
            LEFT JOIN work.moved mv ON mv.tbl = 'col:domain' AND mv.key = c.domain
                                   AND mv.uuid != c.id AND c.domain != ''
            WHERE m.firm > ? AND m.firm <= ?""", (lo, hi))
        # Values schema 25 has no column for, kept aside.
        run.x(f"""
            INSERT OR IGNORE INTO migration_note (firm, kind, value)
            SELECT m.firm, 'prospects_checked_at', c.prospects_checked_at
            FROM work.map m JOIN old.companies c ON c.rowid = m.crow
            WHERE m.firm > ? AND m.firm <= ? AND c.prospects_checked_at IS NOT NULL""", (lo, hi))

    run.chunked("firms", "SELECT firm AS k FROM work.map", body)


def _schemes(run: Run) -> list[tuple[int, str, str, int]]:
    """(scheme id, legacy kind, value prefix, numeric) per declared scheme
    with a schema 24 place."""
    out = []
    for m in run.manifests:
        for ident in m.identifiers:
            if ident.get("legacy"):
                kind, _, prefix = ident["legacy"].partition(":")
                sid = run.one("SELECT id FROM ident_scheme WHERE name = ?", (ident["name"],))
                out.append((sid, kind, prefix, int(ident["numeric"])))
    return out


def _idents(run: Run) -> None:
    """firm_ident from schema 24's keys, read in the order they are stored
    (a walk of company_ids, not a lookup per firm), then every record's own
    key from its company row in case company_ids lacks it."""
    schemes = _schemes(run)
    numeric_ok = "{v} GLOB '[1-9]*' AND {v} NOT GLOB '*[^0-9]*' AND length({v}) <= 18"

    def key_sql(value: str, prefix: str, numeric: int) -> tuple[str, str]:
        raw = f"substr({value}, {len(prefix) + 1})"
        if numeric:
            return f"CAST({raw} AS INTEGER)", numeric_ok.format(v=raw)
        return raw, f"{raw} != ''"

    def moved(kind: str, value: str) -> str:
        return (f"(SELECT w.firm FROM work.moved mv JOIN work.map w ON w.uuid = mv.uuid "
                f"WHERE mv.tbl = 'ids:{kind}' AND mv.key = {value})")

    # (+i.kind: the walk goes by rowid, never by the key index's kind.)
    def from_ids(lo, hi):
        for sid, kind, prefix, numeric in schemes:
            column = OWN_COLUMN.get(kind)
            match = f"substr(i.value, 1, {len(prefix)}) = '{prefix}'" if prefix else "1"
            key, ok = key_sql("i.value", prefix, numeric)
            # The record that carries a key: the one a merge moved it from,
            # else the one whose own column holds it, else its holder.
            own = (f"(SELECT w.firm FROM old.companies oc JOIN work.map w ON w.uuid = oc.id "
                   f"WHERE oc.{column} = i.value AND oc.{column} != '')") if column else "NULL"
            run.x(f"""
                INSERT OR IGNORE INTO firm_ident (scheme, key, firm, since)
                SELECT ?, {key}, COALESCE({moved(kind, 'i.value')}, {own}, m.firm),
                       COALESCE({unix('i.created_at')}, f.created)
                FROM old.company_ids i JOIN work.map m ON m.uuid = i.company_id
                JOIN firm f ON f.id = m.firm
                WHERE i.rowid > ? AND i.rowid <= ? AND +i.kind = ? AND {match} AND {ok}""",
                  (sid, lo, hi, kind))
        # Identifiers of a kind a scheme takes, that no scheme's prefix (or
        # key rule) fits: kept aside, never lost.
        for kind in sorted({k for _, k, _, _ in schemes}):
            fits = " OR ".join(
                f"(substr(i.value, 1, {len(p)}) = '{p}' AND {key_sql('i.value', p, n)[1]})"
                for _, k, p, n in schemes if k == kind) or "0"
            run.x(f"""
                INSERT OR IGNORE INTO migration_note (firm, kind, value)
                SELECT m.firm, 'ident:' || i.kind, i.value
                FROM old.company_ids i JOIN work.map m ON m.uuid = i.company_id
                WHERE i.rowid > ? AND i.rowid <= ? AND +i.kind = ? AND NOT ({fits})""",
                  (lo, hi, kind))

    def from_columns(lo, hi):
        for sid, kind, prefix, numeric in schemes:
            column = OWN_COLUMN.get(kind)
            if not column:
                continue
            key, ok = key_sql(f"c.{column}", prefix, numeric)
            pmatch = (f"substr(c.{column}, 1, {len(prefix)}) = '{prefix}'" if prefix
                      else f"c.{column} != ''")
            run.x(f"""
                INSERT OR IGNORE INTO firm_ident (scheme, key, firm, since)
                SELECT ?, {key}, COALESCE({moved(kind, f'c.{column}')}, m.firm), f.created
                FROM work.map m JOIN old.companies c ON c.rowid = m.crow
                JOIN firm f ON f.id = m.firm
                WHERE m.firm > ? AND m.firm <= ? AND {pmatch} AND {ok}""", (sid, lo, hi))

    run.chunked("idents", "SELECT rowid AS k FROM old.company_ids", from_ids, size=run.obs_chunk)
    run.chunked("own_idents", "SELECT firm AS k FROM work.map", from_columns)


def _keys(run: Run) -> None:
    kinds = ", ".join(f"'{k}'" for k in schema.FIRM_KEY)
    case = "CASE i.kind " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in schema.FIRM_KEY.items()) \
        + " END"

    def body(lo, hi):
        sel = (f"SELECT i.source AS n FROM old.company_ids i "
               f"WHERE i.rowid > ? AND i.rowid <= ? AND +i.kind IN ({kinds})")
        run.names_into("source", sel, (lo, hi))
        run.x(f"""
            INSERT OR IGNORE INTO firm_key (kind, value, firm, source)
            SELECT {case}, i.value,
                   COALESCE((SELECT w.firm FROM work.moved mv JOIN work.map w ON w.uuid = mv.uuid
                             WHERE mv.tbl = 'ids:' || i.kind AND mv.key = i.value), m.firm),
                   (SELECT id FROM source WHERE name = NULLIF(i.source, ''))
            FROM old.company_ids i JOIN work.map m ON m.uuid = i.company_id
            WHERE i.rowid > ? AND i.rowid <= ? AND +i.kind IN ({kinds})""", (lo, hi))

    run.chunked("keys", "SELECT rowid AS k FROM old.company_ids", body, size=run.obs_chunk)


def _scheme_ids(run: Run, names) -> list[int]:
    names = sorted(set(names))
    if not names:
        return []
    return [r[0] for r in run.con.execute(
        f"SELECT id FROM ident_scheme WHERE name IN ({','.join('?' * len(names))})", names)]


def _local(run: Run) -> None:
    keyed = _scheme_ids(run, [s for schemes in run.register_schemes.values() for s in schemes])
    described = (f"EXISTS (SELECT 1 FROM firm_ident fi WHERE fi.firm = m.firm "
                 f"AND fi.scheme IN ({','.join(map(str, keyed))}))") if keyed else "0"

    def body(lo, hi):
        run.x(f"""
            INSERT INTO local_firm (firm, name, county, locality, address, phone, industry,
                                    description, source_url)
            SELECT m.firm, NULLIF(c.name, ''), NULLIF(harvey_place(c.location, 1), ''),
                   NULLIF(harvey_place(c.location, 0), ''), NULLIF(c.address, ''),
                   NULLIF(c.phone, ''), NULLIF(c.industry, ''), NULLIF(c.description, ''),
                   NULLIF(c.source_url, '')
            FROM work.map m JOIN old.companies c ON c.rowid = m.crow
            WHERE m.firm > ? AND m.firm <= ? AND NOT {described}""", (lo, hi))

    run.chunked("local", "SELECT firm AS k FROM work.map", body)


def _names(run: Run) -> None:
    """A held firm's name that differs from its register's goes into the
    migration note (it may be a rename or an older dataset: it becomes the
    display name only on the user's say-so). The register's name is read by
    every scheme a register declares that a loaded register names firms by
    (each module's own key first). A firm the register no longer has under
    any of them keeps its last name and place, described locally and marked
    gone. A firm keyed by a scheme no loaded register names by keeps its
    name in the note too, so nothing is lost."""
    def body():
        attached = {r[1] for r in run.con.execute("PRAGMA database_list")}
        schemes: list[str] = []
        for module in sorted(run.register_schemes):
            schemes += [x for x in run.register_schemes[module] if x not in schemes]
        statements = {}
        for scheme in schemes:
            for adapter in run.registers:
                sql = adapter.names_sql(scheme) if adapter.alias in attached else None
                if sql:
                    statements[scheme] = sql
                    break
        sids = {x: run.one("SELECT id FROM ident_scheme WHERE name = ?", (x,)) for x in schemes}
        run.x("CREATE TEMP TABLE regname (firm INTEGER PRIMARY KEY, name TEXT)")
        for scheme in schemes:              # its own key first: the first name found wins
            if scheme in statements:
                run.x(f"INSERT OR IGNORE INTO temp.regname {statements[scheme]}", (sids[scheme],))
            else:
                run.x("""
                    INSERT OR IGNORE INTO migration_note (firm, kind, value)
                    SELECT fi.firm, 'name', c.name FROM firm_ident fi
                    JOIN work.map m ON m.firm = fi.firm JOIN old.companies c ON c.rowid = m.crow
                    WHERE fi.scheme = ? AND c.name != ''""", (sids[scheme],))
        run.x("""
            INSERT OR IGNORE INTO migration_note (firm, kind, value)
            SELECT r.firm, 'name', c.name FROM temp.regname r
            JOIN work.map m ON m.firm = r.firm JOIN old.companies c ON c.rowid = m.crow
            WHERE c.name != '' AND c.name IS NOT r.name""")
        gone = 0
        if statements:
            marks = ",".join(str(sids[x]) for x in statements)
            gone = run.x(f"""
                INSERT OR IGNORE INTO local_firm (firm, name, county, locality, address, phone,
                                                  industry, description, source_url, gone)
                SELECT DISTINCT fi.firm, NULLIF(c.name, ''),
                       NULLIF(harvey_place(c.location, 1), ''),
                       NULLIF(harvey_place(c.location, 0), ''), NULLIF(c.address, ''),
                       NULLIF(c.phone, ''), NULLIF(c.industry, ''), NULLIF(c.description, ''),
                       NULLIF(c.source_url, ''), 1
                FROM firm_ident fi JOIN work.map m ON m.firm = fi.firm
                JOIN old.companies c ON c.rowid = m.crow
                WHERE fi.scheme IN ({marks}) AND fi.firm NOT IN (SELECT firm FROM temp.regname)
                  AND fi.firm NOT IN (SELECT firm FROM local_firm)""").rowcount
        detail = {"named_by": sorted(statements),
                  "not_named": sorted(x for x in schemes if x not in statements),
                  "compared": run.one("SELECT COUNT(*) FROM temp.regname"), "gone": gone,
                  "noted": run.one("SELECT COUNT(*) FROM migration_note WHERE kind = 'name'")}
        run.x("DROP TABLE temp.regname")
        return detail

    run.atomic("names", body)


def _contacts(run: Run) -> None:
    def body(lo, hi):
        run.x(f"""
            INSERT INTO contact (id, kind, value, display, line_type, status, do_not_contact,
                                 first_seen, last_seen)
            SELECT t.id, (SELECT id FROM contact_kinds WHERE kind = t.kind), t.value,
                   NULLIF(t.display, ''), {LINE_CASE.format(x='t.line_type')},
                   {STATUS_CASE.format(x='t.status')}, COALESCE(t.do_not_contact, 0),
                   COALESCE({unix('t.first_seen')}, 0),
                   COALESCE({unix('t.last_seen')}, {unix('t.first_seen')}, 0)
            FROM old.contacts t WHERE t.id > ? AND t.id <= ?""", (lo, hi))

    run.chunked("contacts", "SELECT id AS k FROM old.contacts", body)


def _person_sources(run: Run) -> str:
    """SQL list of the people sources that are a register's own (its
    administrators, read from the register instead)."""
    return ", ".join(f"'{m}'" for m in sorted(run.register_modules)) or "''"


def _people(run: Run) -> None:
    register = _person_sources(run)

    def body(lo, hi):
        sel = "SELECT p.source AS n FROM old.people p WHERE p.id > ? AND p.id <= ?"
        run.names_into("source", sel, (lo, hi))
        run.x(f"""
            INSERT OR IGNORE INTO person (id, firm, name, name_norm, role, source, first_seen,
                                          last_seen)
            SELECT p.id, {run.firm_of('p.company_id', origin='p.origin_company_id')}, p.name,
                   p.name_norm, NULLIF(p.role, ''),
                   (SELECT id FROM source WHERE name = NULLIF(p.source, '')),
                   {unix('p.first_seen')}, {unix('p.last_seen')}
            FROM old.people p
            WHERE p.id > ? AND p.id <= ?
              AND (p.source NOT IN ({register}) OR p.id IN (SELECT id FROM work.refperson))""",
              (lo, hi))

    run.chunked("people", "SELECT id AS k FROM old.people", body)


def _capabilities(run: Run, select: str, params) -> None:
    """Capability names a chunk uses that no manifest declares: added
    under the module (or source) they were recorded with."""
    for module, name in run.con.execute(
            f"SELECT DISTINCT mo, n FROM ({select}) WHERE n != '' AND n NOT IN "
            f"(SELECT name FROM work.capname)", params).fetchall():
        run.x("INSERT OR IGNORE INTO work.capname VALUES (?, ?)",
              (name, schema.capability_id(run.con, module or "unknown", name)))


def _evidence(run: Run) -> None:
    def body(lo, hi):
        sel = "SELECT k.source AS n FROM old.company_contacts k WHERE k.id > ? AND k.id <= ?"
        run.names_into("source", sel, (lo, hi))
        _capabilities(run, "SELECT k.source AS mo, k.capability AS n FROM old.company_contacts k "
                           "WHERE k.id > ? AND k.id <= ?", (lo, hi))
        run.x(f"""
            INSERT INTO link_evidence (firm, contact, source, person, location, capability,
                                       trust, confidence, evidence_url, first_seen, last_seen)
            SELECT {run.firm_of('k.company_id', origin='k.origin_company_id')}, k.contact_id,
                   (SELECT id FROM source WHERE name = k.source), k.person_id,
                   CASE WHEN k.location_id != 0 AND EXISTS (SELECT 1 FROM old.locations l
                                                            WHERE l.id = k.location_id)
                        THEN k.location_id ELSE 0 END,
                   (SELECT id FROM work.capname WHERE name = NULLIF(k.capability, '')),
                   COALESCE({TRUST_CASE.format(x='k.trust')}, 2), k.confidence,
                   NULLIF(k.evidence_url, ''), COALESCE({unix('k.first_seen')}, 0),
                   COALESCE({unix('k.last_seen')}, {unix('k.first_seen')}, 0)
            FROM old.company_contacts k WHERE k.id > ? AND k.id <= ? AND true
            ON CONFLICT (firm, contact, source, person) DO UPDATE SET
                trust = MIN(trust, excluded.trust), location = MAX(location, excluded.location),
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)""", (lo, hi))

    run.chunked("evidence", "SELECT id AS k FROM old.company_contacts", body)


def _looked_for(run: Run, lo: int, hi: int) -> None:
    for (text,) in run.con.execute("SELECT DISTINCT looked_for FROM old.searches WHERE id > ? "
                                   "AND id <= ? AND looked_for != ''", (lo, hi)).fetchall():
        mask = schema.looked_for_mask(run.con, text.split(","))
        run.x("INSERT OR IGNORE INTO work.lf (text, mask) VALUES (?, ?)", (text, mask))


def _searches(run: Run) -> None:
    outcome = ("CASE s.outcome " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in schema.OUTCOME.items())
               + f" ELSE CASE WHEN s.found > 0 THEN {schema.OUTCOME['found']} "
                 f"ELSE {schema.OUTCOME['nothing']} END END")
    run.x("CREATE TABLE IF NOT EXISTS work.lf (text TEXT PRIMARY KEY, mask INTEGER NOT NULL)")

    def body(lo, hi):
        run.names_into("module", "SELECT module AS n FROM old.searches WHERE id > ? AND id <= ?",
                       (lo, hi))
        run.batches_into("SELECT batch_id AS n FROM old.searches WHERE id > ? AND id <= ?",
                         (lo, hi))
        for module, cap in run.con.execute(
                "SELECT DISTINCT module, capability FROM old.searches WHERE id > ? AND id <= ?",
                (lo, hi)).fetchall():
            schema.capability_id(run.con, module, cap or "")
        _looked_for(run, lo, hi)
        target = (f"CASE WHEN s.target = 'company' THEN "
                  f"(SELECT firm FROM work.map WHERE uuid = s.target_id) "
                  f"ELSE (SELECT id FROM person WHERE id = CAST(s.target_id AS INTEGER)) END")
        origin = ("CASE WHEN s.origin_company_id != '' AND s.origin_company_id != s.target_id "
                  "THEN (SELECT firm FROM work.map WHERE uuid = s.origin_company_id) END")
        run.x(f"""
            INSERT INTO search (id, target, is_person, origin, module, capability, looked_for, at,
                                outcome, found, cost, batch, detail)
            SELECT s.id, t, s.target != 'company', {origin}, mo.id,
                   (SELECT c.id FROM capability c WHERE c.module = mo.id
                    AND c.name = COALESCE(s.capability, '')),
                   COALESCE((SELECT mask FROM work.lf WHERE text = s.looked_for), 0),
                   COALESCE({unix('s.searched_at')}, 0), {outcome}, s.found, s.cost,
                   (SELECT id FROM batch WHERE ref = NULLIF(s.batch_id, '')),
                   NULLIF(s.detail, '')
            FROM (SELECT s.*, {target} AS t FROM old.searches s WHERE s.id > ? AND s.id <= ?) s
            JOIN module mo ON mo.name = s.module
            WHERE t IS NOT NULL""", (lo, hi))

    run.chunked("searches", "SELECT id AS k FROM old.searches", body)


def _sources(run: Run) -> None:
    def body(lo, hi):
        sel = "SELECT s.source AS n FROM old.company_sources s WHERE s.rowid > ? AND s.rowid <= ?"
        run.names_into("source", sel, (lo, hi))
        run.x(f"""
            INSERT INTO firm_source (firm, source, batch, ref, first_seen, last_seen)
            SELECT {run.firm_of('s.company_id', origin='s.origin_company_id')},
                   (SELECT id FROM source WHERE name = s.source), 0, COALESCE(s.ref, ''),
                   {unix('s.first_seen')}, {unix('s.last_seen')}
            FROM old.company_sources s WHERE s.rowid > ? AND s.rowid <= ? AND true
            ON CONFLICT (firm, source, batch, ref) DO UPDATE SET
                first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen),
                last_seen = MAX(COALESCE(last_seen, excluded.last_seen), excluded.last_seen)""",
              (lo, hi))

    run.chunked("sources", "SELECT rowid AS k FROM old.company_sources", body)


def _obs_rules(run: Run) -> None:
    """work.obs_rule: per (signal, module, collector) seen, whether its
    rows move over (a register's copy doesn't), with the module, signal
    and key rule they move with."""
    run.x("""CREATE TABLE IF NOT EXISTS work.obs_rule (
               code TEXT NOT NULL, module TEXT NOT NULL, collector TEXT NOT NULL,
               keep INTEGER, module_id INTEGER, signal_id INTEGER, keyed INTEGER,
               PRIMARY KEY (code, module, collector)) WITHOUT ROWID""")


def collector_modules(manifests) -> dict[str, str]:
    """A collector name that only one module's signals declare -> that module."""
    seen: dict[str, set] = {}
    for m in manifests:
        for sig in m.signals:
            if sig.get("collector"):
                seen.setdefault(sig["collector"], set()).add(m.id)
    return {c: next(iter(ms)) for c, ms in seen.items() if len(ms) == 1}


def _observations(run: Run) -> None:
    _obs_rules(run)
    collectors = collector_modules(run.manifests)
    declared = dict(run.con.execute("SELECT code, module FROM old.signal_codes").fetchall())
    shape = {code: (keyed_by or "", cardinality or "") for code, keyed_by, cardinality in
             run.con.execute("SELECT code, keyed_by, cardinality FROM signal").fetchall()}
    register = sorted(run.register_modules)
    skip = (f"COALESCE(o.module, '') NOT IN ({','.join('?' * len(register))})"
            if register else "1")

    def decide(lo, hi):
        new = run.con.execute(
            f"SELECT DISTINCT o.signal_code, COALESCE(o.module, ''), COALESCE(o.collector, '') "
            f"FROM old.observations o WHERE o.rowid > ? AND o.rowid <= ? AND NOT EXISTS ("
            f"SELECT 1 FROM work.obs_rule r WHERE r.code = o.signal_code "
            f"AND r.module = COALESCE(o.module, '') AND r.collector = COALESCE(o.collector, ''))",
            (lo, hi)).fetchall()
        for code, module, collector in new:
            owner = module or collectors.get(collector or "") or declared.get(code) or ""
            keep = owner not in run.register_modules and code in shape
            keyed_by, cardinality = shape.get(code, ("", ""))
            run.x("INSERT INTO work.obs_rule VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (code, module or "", collector or "", int(keep),
                   schema.dict_id(run.con, "module", owner) if keep and owner else None,
                   run.one("SELECT id FROM signal WHERE code = ?", (code,)) if keep else None,
                   int(bool(keyed_by) or cardinality == "many")))

    def body(lo, hi):
        decide(lo, hi)
        run.batches_into(f"SELECT o.run_id AS n FROM old.observations o "
                         f"WHERE o.rowid > ? AND o.rowid <= ? AND {skip}", (lo, hi, *register))
        _capabilities(run, f"SELECT r.module AS mo, o.capability AS n FROM old.observations o "
                           f"JOIN work.obs_rule r ON r.code = o.signal_code "
                           f"AND r.module = COALESCE(o.module, '') "
                           f"AND r.collector = COALESCE(o.collector, '') AND r.keep = 1 "
                           f"WHERE o.rowid > ? AND o.rowid <= ? AND {skip}", (lo, hi, *register))
        moved = ("(SELECT w.firm FROM work.moved mv JOIN work.map w ON w.uuid = mv.uuid "
                 "WHERE mv.tbl = 'observations' AND mv.key = o.id)")
        run.x(f"""
            INSERT INTO obs (firm, signal, key, num, text, confidence, module, capability, run,
                             evidence_url, at)
            SELECT {run.firm_of('o.company_id', origin='o.origin_company_id', moved=moved)},
                   r.signal_id, CASE WHEN r.keyed THEN COALESCE(o.value_text, '') ELSE '' END,
                   o.value_num, NULLIF(o.value_text, ''), o.confidence, r.module_id,
                   (SELECT id FROM work.capname WHERE name = NULLIF(o.capability, '')),
                   (SELECT id FROM batch WHERE ref = NULLIF(o.run_id, '')),
                   NULLIF(o.evidence_url, ''), COALESCE({unix('o.observed_at')}, 0)
            FROM old.observations o
            JOIN work.obs_rule r ON r.code = o.signal_code AND r.module = COALESCE(o.module, '')
                                AND r.collector = COALESCE(o.collector, '') AND r.keep = 1
            WHERE o.rowid > ? AND o.rowid <= ? AND {skip} AND o.company_id != ''
            ORDER BY o.rowid""", (lo, hi, *register))

    run.chunked("observations", "SELECT rowid AS k FROM old.observations", body,
                size=run.obs_chunk)


def _merges(run: Run) -> None:
    def body():
        con = run.con
        f = run.firm_of
        merges = con.execute(f"""
            SELECT m.id, {f('m.survivor')}, {f('m.merged')}, m.score, m.reasons_json,
                   m.decided_by, NULLIF(m.group_id, 0), {unix('m.created_at')},
                   {unix('m.undone_at')}, m.filled_json,
                   (SELECT CAST(key1 AS INTEGER) FROM old.company_merge_moves mm
                    WHERE mm.merge_id = m.id AND mm.tbl = 'locations.created')
            FROM old.company_merges m ORDER BY m.id""").fetchall()
        unmapped = cleared = 0
        for (mid, survivor, merged, score, reasons, by, grp, at, undone, filled,
             location) in merges:
            if survivor is None or merged is None:
                unmapped += 1
                continue
            con.execute("INSERT INTO merge (id, survivor, merged, score, reasons, decided_by, grp, "
                        "at, undone_at, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (mid, survivor, merged, score, reasons, by, grp, at, undone, location))
            if undone is not None:
                con.execute("INSERT INTO merge_member VALUES (?, ?)", (mid, merged))
                continue
            # The records the merge takes: the merged firm and every record
            # merged into it before; all now point at the survivor (chains
            # flattened to their final survivor as later merges follow).
            taken = [merged] + [r[0] for r in con.execute(
                "SELECT id FROM firm WHERE live = ? AND live != 0", (merged,))]
            con.executemany("INSERT INTO merge_member VALUES (?, ?)", [(mid, t) for t in taken])
            con.execute("UPDATE firm SET live = ? WHERE id = ? OR (live = ? AND live != 0)",
                        (survivor, merged, merged))
            try:
                items = json.loads(filled or "{}")
            except ValueError:
                items = {}
            site = items.get("website")
            if isinstance(site, str) and site:
                con.execute("UPDATE firm SET website_fill = ? WHERE id = ? AND website = ?",
                            (mid, survivor, site))
            cleared += _unfill(con, survivor, items)
        # A domain a merge moved onto its survivor is the merged record's
        # key again: rows don't move in schema 25.
        con.execute("""
            UPDATE firm SET domain = (SELECT mv.key FROM work.moved mv JOIN work.map w
                                      ON w.uuid = mv.uuid WHERE mv.tbl = 'col:domain'
                                      AND w.firm = firm.id)
            WHERE domain IS NULL AND id IN (SELECT w.firm FROM work.moved mv JOIN work.map w
                                            ON w.uuid = mv.uuid WHERE mv.tbl = 'col:domain')
              AND NOT EXISTS (SELECT 1 FROM firm x WHERE x.domain = (
                  SELECT mv.key FROM work.moved mv JOIN work.map w ON w.uuid = mv.uuid
                  WHERE mv.tbl = 'col:domain' AND w.firm = firm.id))""")
        con.execute(f"""
            INSERT OR IGNORE INTO match_candidate (a, b, score, reasons, status, at, decided_at)
            SELECT {f('c.company_a')}, {f('c.company_b')}, c.score, c.reasons_json, c.status,
                   {unix('c.created_at')}, {unix('c.decided_at')}
            FROM old.match_candidates c
            WHERE {f('c.company_a')} IS NOT NULL AND {f('c.company_b')} IS NOT NULL""")
        return {"merges": len(merges) - unmapped, "unmapped": unmapped,
                "fills_cleared": cleared}

    run.atomic("merges", body)


# Schema 24 merges copied these columns onto a survivor that had none; in
# schema 25 a merge fills only the website (``website_fill``, undone with
# it), the merged record keeps its own description.
FILLED_COLUMNS = {"name": "name", "address": "address", "phone": "phone",
                  "industry": "industry", "description": "description",
                  "source_url": "source_url"}


def _unfill(con, survivor: int, items: dict) -> int:
    """Clear what a schema 24 merge copied into a survivor's local
    description (its value before the merge was empty), so undoing the merge
    leaves nothing of the merged record behind. Returns the columns cleared."""
    cleared = 0
    for key, value in items.items():
        if not isinstance(value, str) or not value:
            continue
        if key in FILLED_COLUMNS:
            col = FILLED_COLUMNS[key]
            cleared += con.execute(f"UPDATE local_firm SET {col} = NULL WHERE firm = ? "
                                   f"AND {col} = ?", (survivor, value)).rowcount
        elif key == "location":
            locality, county = place_parts(value)
            cleared += con.execute("UPDATE local_firm SET locality = NULL, county = NULL "
                                   "WHERE firm = ? AND locality IS ? AND county IS ?",
                                   (survivor, locality or None, county or None)).rowcount
    return cleared


def _columns(con, table: str, db: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {db}.table_info({table})")]


def _outreach(run: Run) -> None:
    def body():
        con = run.con
        for table in COPIED:
            cols = [c for c in _columns(con, table, "main") if c in _columns(con, table, "old")]
            names = ", ".join(cols)
            con.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM old.{table}")
        cols = [c for c in _columns(con, "prospects", "main") if c != "firm"]
        names = ", ".join(cols)
        moved = ("(SELECT w.firm FROM work.moved mv JOIN work.map w ON w.uuid = mv.uuid "
                 "WHERE mv.tbl = 'prospects' AND mv.key = p.id)")
        con.execute(f"INSERT INTO prospects (firm, {names}) "
                    f"SELECT {run.firm_of('NULLIF(p.company_id, %s)' % repr(''), moved=moved)}, "
                    f"{', '.join('p.' + c for c in cols)} FROM old.prospects p")
        con.execute(f"""
            INSERT INTO locations (id, firm, name, address, locality, county, country, point,
                                   external_id, phone, source, first_seen, last_seen)
            SELECT l.id, {run.firm_of('l.company_id', origin='l.origin_company_id')}, l.name,
                   l.address, l.locality, l.county, l.country, l.point, l.external_id, l.phone,
                   l.source, {unix('l.first_seen')}, {unix('l.last_seen')}
            FROM old.locations l""")
        return {"worker_lease": "not copied (a running worker's lease, not data)"}

    run.atomic("outreach", body)


def _legacy(run: Run) -> None:
    """legacy_id: the old company ids still named in job payloads, actions,
    settings, runs and feedback, so a paused job resumes and the Activity
    page reads."""
    def body():
        con = run.con
        found: set[str] = set()
        for table, columns in NAMES_IDS.items():
            for (text,) in con.execute(f"SELECT {' || char(10) || '.join(columns)} FROM {table}"):
                found.update(_HEX_ID.findall(text or ""))
        found = sorted(found)
        for start in range(0, len(found), 500):
            chunk = found[start:start + 500]
            con.execute("INSERT OR IGNORE INTO legacy_id (uuid, firm) SELECT uuid, firm "
                        "FROM work.map WHERE uuid IN (SELECT value FROM json_each(?))",
                        (json.dumps(chunk),))
        return {"tokens": len(found)}

    run.atomic("legacy", body)


def _recount(run: Run) -> None:
    """One recount of every kept value, with history folded (an observation
    that repeats the one before it is not history), then the triggers."""
    done, _ = run.state("recount")
    if done:
        return
    started = time.monotonic()
    recount.fix(run.con, fold_history=True)
    run.x("BEGIN IMMEDIATE")
    run._save("recount", last=0, rows=0, seconds=time.monotonic() - started, done=1,
              detail={"obs_kept": run.one("SELECT COUNT(*) FROM obs")})
    run.x("COMMIT")
    if run.on_chunk:
        run.on_chunk("recount", 0)


def _finish(run: Run) -> None:
    def body():
        run.x("ANALYZE main")

    run.atomic("finish", body)


STEPS = (_vocabulary, _map, _firms, _idents, _keys, _local, _names, _contacts, _people,
         _evidence, _searches, _sources, _observations, _merges, _outreach, _legacy, _source_read,
         _recount, _finish)
