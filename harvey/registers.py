"""Registers as modules: the core's side.

A register module (a public register Harvey reads in place: a trade
registry, a ministry's yearly file, a tax authority's answers) declares in
its manifest the identifier schemes firms are known by ([[identifiers]]),
its file and attach alias ([register]) and the signals it reads
([[register.reads]]). Its own ``queries.py`` provides an ``Adapter``: how to
describe a firm, which keys a filter resolves to, and a ``Read`` per signal
(the table, key and value expression its load built for it). The core
composes those with harvey.db's own sets and never names a register's
tables, columns or identifiers itself.

Every statement an adapter runs is registered by name (harvey/queries.py)
and checked by the plan test: a firm reaches a register only by an integer
or text primary key (``firm_ident`` -> the register's key), and a range on a
register value only through an index its load built.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from harvey import queries
from harvey.paths import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
# A range is an index search for these (not for !=, which reads the whole index).
OPS = (">=", ">", "<=", "<", "=")


@dataclass(frozen=True)
class Read:
    """How a register reads one signal. SQL fragments on alias ``c``:
    ``table`` is the FROM part that names ``c`` (and anything it joins),
    with ``{alias}`` / ``{derived}`` for the register's file. ``key`` is
    the column of ``c`` the scheme's key is in, ``key_text`` when
    that column holds the key as text (an integer key is compared as its
    text then). ``true`` / ``false`` say when a row reads yes or no
    (default: value not 0 / value 0); a row that is neither, or no row at
    all, is unknown unless the manifest says absent = "false". ``indexed``:
    the load built an index on ``value``, so a range is an index search.
    ``scheme``: the identifier scheme ``key`` holds when it isn't the
    register's own (``keyed_by``): a register that reaches some firms by
    another key (a firm with no tax code, by its registration number)."""
    signal: str
    table: str
    key: str
    value: str
    date: str = "NULL"
    true: str = ""
    false: str = ""
    indexed: bool = False
    key_text: bool = False
    # More conditions on the rows ``table`` joins (its join condition).
    where: str = "1"
    scheme: str = ""

    @property
    def suffix(self) -> str:
        """What a statement name adds for a read keyed by another scheme."""
        return f"@{self.scheme}" if self.scheme else ""

    @property
    def base_table(self) -> str:
        """The register table ``c`` is (what a range statement reads)."""
        m = re.match(r"\s*(?:\{\w+\}\.)?(\w+)", self.table)
        return m.group(1) if m else ""


class Stale(RuntimeError):
    """A derived table was built by other rules than the code's: rebuild
    it (the register's reindex) before reading it."""


class Adapter:
    """A register module's adapter. Subclasses set ``module`` and
    ``reads`` and may add describe() and resolve()."""

    module = ""
    reads: tuple[Read, ...] = ()

    def __init__(self, alias: str, path: Path | None = None, derived: str = ""):
        self.alias = alias
        self.path = Path(path) if path else None
        self.derived = derived or alias
        # (signal, scheme) -> Read; scheme "" is the register's own key.
        self.by_signal = {(r.signal, r.scheme): r for r in self.reads}
        register_reads(self)

    # The register's file

    def attach(self, con: sqlite3.Connection) -> bool:
        """Attach the file read-only under the alias; False when it isn't loaded."""
        if self.path is None or not self.path.is_file():
            return False
        if self.alias not in {r[1] for r in con.execute("PRAGMA database_list")}:
            attach_read_only(con, self.alias, self.path)
        return True

    def sql(self, name: str) -> str:
        return queries.sql(f"{self.module}.{name}", self.alias, self.derived)

    def marks(self, con: sqlite3.Connection) -> str | None:
        """The load mark of the attached file (what a kept count is keyed
        by), or None when it isn't attached."""
        if self.alias not in {r[1] for r in con.execute("PRAGMA database_list")}:
            return None
        if not self.marks_sql:
            # A file written a little at a time: its size and last write.
            st = os.stat(self.path)
            return f"{st.st_size} {st.st_mtime_ns}"
        row = con.execute(self.sql("marks")).fetchone()
        return row[0] if row else ""

    # Signals

    marks_sql = ""

    def read(self, con: sqlite3.Connection, signal: str, keys, scheme: str = "") -> dict:
        """{key: [(value, date), ...]} for the keys the register has a value
        for (several for a signal with many values, like activities).
        ``scheme``: keys of another scheme than the register's own, for a
        signal it also reads by that scheme."""
        read = self.by_signal.get((signal, scheme))
        if read is None:
            raise ValueError(f"{self.module} doesn't read {signal} by {scheme or 'its key'}")
        out: dict = {}
        for chunk in chunks(keys):
            for key, value, date in con.execute(self.sql(f"read.{signal}{read.suffix}"),
                                                (json.dumps(chunk),)):
                out.setdefault(key, []).append((value, date))
        return out

    def range_sql(self, signal: str, op: str, scheme: str = "") -> str | None:
        """A statement selecting the keys whose value satisfies ``op ?``
        (an index search), or None when the register can't answer it that way."""
        read = self.by_signal.get((signal, scheme))
        if read is None or not read.indexed or op not in OPS:
            return None
        return self.sql(f"range.{signal}{read.suffix}.{OPS.index(op)}")

    def held_sql(self, signal: str, op: str, scheme: str = "") -> str | None:
        """The records in Harvey whose register value satisfies ``op ?``
        (params: the value, the scheme's id), or None."""
        read = self.by_signal.get((signal, scheme))
        if read is None or not read.indexed or op not in OPS:
            return None
        return self.sql(f"held.{signal}{read.suffix}.{OPS.index(op)}")

    def range(self, con: sqlite3.Connection, signal: str, op: str, value,
              scheme: str = "") -> list:
        sql = self.range_sql(signal, op, scheme)
        if sql is None:
            raise ValueError(f"{self.module} can't range over {signal} with {op}")
        return [r[0] for r in con.execute(sql, (value,))]

    # What a subclass adds

    def describe(self, con: sqlite3.Connection, keys) -> dict:
        return {}

    def resolve(self, filters: dict) -> list[tuple[str, object]]:
        raise NotImplementedError(f"{self.module} doesn't resolve filters")


def register_reads(adapter: Adapter) -> None:
    """Register the statements every Read gives: read by keys, and a range
    per operator when the value is indexed."""
    mod = adapter.module
    if adapter.marks_sql:
        queries.statement(f"{mod}.marks", adapter.marks_sql, module=mod)
    for r in adapter.reads:
        name = f"{r.signal}{r.suffix}"
        keyed = "CAST(j.value AS TEXT)" if r.key_text else "j.value"
        queries.statement(
            f"{mod}.read.{name}",
            f"SELECT j.value, {r.value}, {r.date} FROM json_each(?) j CROSS JOIN {r.table} "
            f"WHERE c.{r.key} = {keyed} AND {r.where} AND {r.value} IS NOT NULL",
            params=("[1, 2]",), module=mod, drives="j")
        # The firms in Harvey a register value holds for, counted yes / no,
        # once per firm (register_tally: a recount job per load mark, so it
        # walks every key of the scheme, probing the register by key). A
        # firm reads yes when any of its rows does (an activity among
        # several), no when one of its rows reads no and none yes.
        true = r.true or f"{r.value} != 0"
        false = r.false or f"{r.value} = 0"
        probe = (f"c.{r.key} = {'CAST(fi.key AS TEXT)' if r.key_text else 'fi.key'} "
                 f"AND {r.where}")
        queries.statement(
            f"{mod}.held_count.{name}",
            f"SELECT COALESCE(SUM(t), 0), COALESCE(SUM(f AND NOT t), 0) FROM ("
            f"SELECT EXISTS (SELECT 1 FROM {r.table} WHERE {probe} AND {true}) AS t, "
            f"EXISTS (SELECT 1 FROM {r.table} WHERE {probe} AND {false}) AS f "
            f"FROM firm_ident fi WHERE fi.scheme = ?)",
            params=(1,), module=mod, drives="fi",
            scans={"firm_ident": "a recount job per load mark: every key of the scheme, "
                                 "each probed in the register by key"})
        if r.indexed:
            matches = {r.base_table: "a range reads the index entries it matches (as many as "
                                     "the firms in range): an index search, never the table"}
            for i, op in enumerate(OPS):
                queries.statement(
                    f"{mod}.range.{name}.{i}",
                    f"SELECT c.{r.key} FROM {r.table} WHERE {r.where} AND {r.value} {op} ?",
                    params=(1,), module=mod, scans=matches)
                # The records in Harvey in that range: the register's index
                # drives, each key probed in firm_ident by (scheme, key). The
                # key is compared with no affinity (``+``): the register's
                # integer key column would otherwise convert firm_ident's
                # key, and the probe could use only the scheme (every key
                # of it, once per register row).
                queries.statement(
                    f"{mod}.held.{name}.{i}",
                    f"SELECT fi.firm FROM {r.table} CROSS JOIN firm_ident fi "
                    f"WHERE {r.where} AND {r.value} {op} ? AND fi.scheme = ? "
                    f"AND fi.key = +c.{r.key}",
                    params=(1, 1), module=mod, drives="c", scans=matches)


def chunks(keys, size: int = 500):
    keys = list(keys)
    for start in range(0, len(keys), size):
        yield keys[start:start + size]


# ── Declarations from the manifests ──

def declared(manifests=None) -> list:
    """The manifests that declare a register, by module id."""
    from harvey.modules.loader import load_manifests

    return [m for m in (manifests if manifests is not None else load_manifests()) if m.register]


def adapter_for(manifest, data_dir: Path = DATA_DIR, derived: str = "") -> Adapter:
    """The module's adapter, over its file in ``data_dir``."""
    reg = manifest.register
    cls = import_module(reg["adapter"]).Adapter
    return cls(reg["attach"], Path(data_dir) / reg["file"], derived=derived)


def adapters(data_dir: Path = DATA_DIR, manifests=None, enabled=None) -> list[Adapter]:
    """An adapter for every register module (that is on, when ``enabled``
    is given: the module ids switched on)."""
    return [adapter_for(m, data_dir) for m in declared(manifests)
            if enabled is None or m.id in enabled]


def attach_all(con: sqlite3.Connection, regs: list[Adapter]) -> dict[str, bool]:
    """Attach every register's file read-only: alias -> loaded."""
    return {a.alias: a.attach(con) for a in regs}


def attach_read_only(con: sqlite3.Connection, alias: str, path: Path) -> None:
    """ATTACH ``path`` read-only as ``alias``. Read-only is a URI parameter,
    which SQLite honours in ATTACH only when the connection was opened
    with ``uri=True`` or the library was built to accept URIs everywhere;
    otherwise the URI would be taken for a file name (a new, empty, writable
    database in the working directory). That is checked and refused, never
    attached writable."""
    path = Path(path).resolve()
    uri = f"file:{path.as_posix()}?mode=ro"
    stray = Path(uri)
    existed = stray.exists()
    con.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
    opened = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == alias), "")
    if opened and Path(opened).resolve() == path:
        return
    con.execute(f"DETACH DATABASE {alias}")
    if not existed and stray.is_file() and stray.stat().st_size == 0:
        stray.unlink()
    raise RuntimeError(f"can't attach {path.name} read-only on this connection: open it "
                       "with sqlite3.connect(..., uri=True)")


# ── Derived tables: built by each register's own load ──

def mark_built(con: sqlite3.Connection, dst: str, table: str, version: str) -> None:
    """Record which version of its rules built a derived table (a table
    built with code, like a name key, goes stale when the code changes)."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {dst}.derived_meta "
                "(name TEXT PRIMARY KEY, version TEXT NOT NULL)")
    con.execute(f"INSERT OR REPLACE INTO {dst}.derived_meta VALUES (?, ?)", (table, version))


def build_derived(source: Path, build, target: Path | None = None,
                  tables: tuple[str, ...] = ()) -> dict:
    """Run a register's derived-table ``build(con, src, dst)`` and analyse
    the ``tables`` it builds. Into ``target``, a fresh file (the register
    attached read-only, never written), or, with no target, into the
    register's own file: one journalled transaction, so a build that fails
    part-way leaves the file as it was, and only the built tables are
    analysed (the register's own statistics, and so today's plans, stay as
    its load left them). Returns {"seconds", "bytes", ...build's counts}."""
    source = Path(source)
    started = time.monotonic()
    if target is None:
        con = sqlite3.connect(source, isolation_level=None)
        src = dst = "main"
    else:
        target = Path(target)
        for suffix in ("", "-journal", "-wal"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        con = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True,
                              isolation_level=None)
        con.execute("ATTACH DATABASE ? AS out", (str(target),))
        src, dst = "main", "out"
    try:
        if target is not None:
            # A scratch file nobody reads until the build is done.
            con.execute(f"PRAGMA {dst}.journal_mode=OFF")
            con.execute(f"PRAGMA {dst}.synchronous=OFF")
        con.execute("PRAGMA cache_size=-200000")
        con.execute("BEGIN IMMEDIATE")
        counts = build(con, src, dst) or {}
        for table in tables:
            con.execute(f"ANALYZE {dst}.{table}")
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    size = os.path.getsize(target if target is not None else source)
    return {"seconds": round(time.monotonic() - started, 1), "bytes": size, **counts}
