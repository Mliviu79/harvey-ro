"""Harvey's reads on the overlay (schema 25): every page the dashboard,
the CLI and the agents read, in the shapes schema 24's reads give them.

The registers describe every firm (name, place, activities, figures,
flags), read in place through their adapters (harvey/registers.py); the
overlay holds what Harvey added (websites, contacts and links, people,
searches, sources, merges, web signals, outreach). A page composes the
two: a register filter resolves to register keys, mapped to firms in
Harvey through ``firm_ident``; Harvey's own filters are the firm's kept
columns; the intersection is done here, in Python, as sets of firm ids.

Every statement is named (overlay/queries.py and each register module's
queries.py) and checked by the plan test. A page reads by index: an
unfiltered list walks a partial index with LIMIT / OFFSET and counts
from a tally; a filtered or re-sorted one is an ordering of the matching
firms kept for a few minutes (companies.Snapshots, keyed by what changes
when it may have), so page numbers and the page box keep working and
every page after the first is a slice.

Ids. Company ids travel as text digits, as the store gives them; an old
text id still named somewhere is read through ``legacy_id``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from array import array
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path
from urllib.parse import quote

from harvey import contacts, queries
from harvey import registers as regs_mod
from harvey.overlay import queries as _statements  # noqa: F401  (registers them)
from harvey.overlay import bitmaps, schema
from harvey.overlay.idents import Schemes
from harvey.overlay.schema import EMAIL, PHONE, TRUST, TRUST_NAMES

logger = logging.getLogger("harvey.overlay")

CHUNK = 10_000
# Line types and contact statuses by their stored integers.
LINE_NAMES = {v: k for k, v in schema.LINE_TYPE.items()}
STATUS_NAMES = {v: k for k, v in schema.CONTACT_STATUS.items()}
OUTCOME_NAMES = {v: k for k, v in schema.OUTCOME.items()}
# Contact filters a firm's kept counts answer (the rest read their links).
COUNTED_CONTACTS = ("any", "none", "email", "phone", "mobile")

# Kept orderings (filter + sort -> firm ids), as the schema 24 pages keep
# theirs; and the register-condition sets a cohort reads, per load mark.
from harvey.companies import Ordered, Snapshots  # noqa: E402

# The sources a read of a website writes contacts under (its pages, its
# phones, the people it names, an address built at its domain): never
# evidence that a site is the company's.
SITE_SOURCES = frozenset({"website", "phones", "people", "email_verify", "profile",
                          "profile:browser"})

ORDERINGS = Snapshots()
SETS = Snapshots(kept=64, seconds=600)
# How long an ordering of every firm (no filter) is kept at most: it is
# dropped sooner when a firm is added or merged or a register reloaded.
LONG_SECONDS = 3600
# From this many firms a list sorts by name through the registry's own
# order (its ids), not by reading every name.
NAME_ORDER_FROM = 200_000
# A cohort this big or smaller is sorted by name at once and kept; a bigger
# one is read off the name index a page at a time.
COHORT_SORTED_MAX = 20_000
# finder.next_paid's keyset walk: its page, the keys above and below every
# firm, and how long a walk's done stretch is trusted before it starts over.
PAID_WALK_PAGE = 1000
PAID_TOP = (2**62, 2**62)
PAID_BOTTOM = (-2**62, -2**62)
PAID_WALK_SECONDS = 86400
# Past this many identifiers given since the counts job kept the register
# sets (a big pull), every first visit would read them all again from the
# registers: the worker runs the job again instead (bitmaps.py).
PATCH_MAX = 10_000


def _iso(ts) -> str | None:
    """Unix seconds as schema 24 wrote times (naive UTC, ISO)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _db_time(ts) -> str | None:
    """Unix seconds as SQLite's CURRENT_TIMESTAMP writes them."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_CAEN: dict | None = None


def _caen_names() -> dict:
    """(code, version) -> (Romanian name, English name, everyday words)."""
    global _CAEN
    if _CAEN is None:
        from harvey.caen import reference_rows

        _CAEN = {(c, v): (ro, en, kw) for c, v, ro, en, kw in reference_rows()}
    return _CAEN


def chunks(items, size: int = CHUNK):
    items = list(items)
    for at in range(0, len(items), size):
        yield items[at:at + size]


# ── Connections ──

# Where the registers are read from: None, the database's own folder (as
# the store attaches them); else a callable(path) giving the adapters (a
# set of files kept elsewhere: a comparison, a test).
REGISTERS = None


def connect(path, *, data_dir=None) -> tuple[sqlite3.Connection, list]:
    """The overlay at ``path`` with every register that is loaded attached
    read-only (overlay.store.connect's, unless REGISTERS says where)."""
    from harvey.overlay import store

    if REGISTERS is None:
        return store.connect(path, data_dir=data_dir)
    con = sqlite3.connect(f"file:{quote(str(Path(path).resolve()))}", uri=True,
                          isolation_level=None, timeout=30, check_same_thread=False)
    attached = []
    for adapter in REGISTERS(path):
        try:
            if adapter.attach(con):
                attached.append(adapter)
        except (sqlite3.Error, RuntimeError) as e:
            logger.debug("register %s not attached: %s", adapter.module, e)
    return con, attached


def recount_register_tally(path, codes=None) -> int:
    """Count every register signal over the firms in Harvey for the
    registers' current load marks and keep the counts (register_tally):
    what the signal listing and the landing page read. A job (a walk of
    every key per read: minutes on a whole registry), never a page.
    Returns how many rows were written."""
    con, attached = connect(path)
    try:
        # (Taken before the walk, with a second to spare: a record given an
        # identifier meanwhile is read again from the register by a page,
        # bitmaps.py.)
        at = int(time.time()) - 1
        top, records = con.execute(queries.sql("overlay.read.kept_start")).fetchone()
        sets: dict = {}
        rows = Reader(con, attached).count_registers(codes, sets)
        con.execute("BEGIN IMMEDIATE")
        con.executemany("INSERT OR REPLACE INTO register_tally (signal, module, mark, loaded, "
                        "n_true, n_false, at, n_values) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        # (A signal's sets replaced whole: a year no longer loaded goes.)
        con.executemany("DELETE FROM register_set WHERE signal = ? AND module = ?",
                        sorted({(sid, mid) for sid, mid, _ in sets}))
        con.executemany(
            "INSERT INTO register_set (signal, module, part, mark, top, records, at, bits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(sid, mid, part, mark, top or 0, records, at, bitmaps.pack(bits))
             for (sid, mid, part), (mark, bits) in sets.items()])
        con.execute("COMMIT")
        return len(rows)
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()


def call(path, method: str, *args, **kwargs):
    """``Reader(...).method(*args, **kwargs)`` in one read transaction (one
    snapshot: a page's count and rows agree)."""
    con, attached = connect(path)
    try:
        reader = Reader(con, attached)
        con.execute("BEGIN")
        try:
            return getattr(reader, method)(*args, **kwargs)
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
    finally:
        con.close()


class FirmSet:
    """A set of live firm ids, or every live firm but those (``neg``): a
    filter that keeps most firms (no turnover outside a range) is held by
    what it leaves out. A big set worked out from a register is held as a
    bitmap (``bits``, overlay/bitmaps.py), and combining it with another set
    makes a bitmap too: a million ids are never a Python set."""

    __slots__ = ("_ids", "neg", "_bits", "_lookup")

    def __init__(self, ids=(), neg: bool = False, bits: int | None = None):
        self._bits = bits
        self._ids = None if bits is not None else (ids if isinstance(ids, set) else set(ids))
        self.neg = neg
        self._lookup = None

    @property
    def ids(self) -> set[int]:
        """The ids held (left out, for a negative set)."""
        if self._ids is None:
            self._ids = set(bitmaps.ids_of(self._bits))
        return self._ids

    @property
    def bits(self) -> int:
        if self._bits is None:
            self._bits = bitmaps.bits_of(self._ids)
        return self._bits

    def _both_sets(self, other: "FirmSet") -> bool:
        return self._bits is None and other._bits is None

    def __and__(self, other: "FirmSet") -> "FirmSet":
        if self._both_sets(other):
            if not self.neg and not other.neg:
                return FirmSet(self.ids & other.ids)
            if self.neg and other.neg:
                return FirmSet(self.ids | other.ids, True)
            pos, neg = (self, other) if not self.neg else (other, self)
            return FirmSet(pos.ids - neg.ids)
        if not self.neg and not other.neg:
            return FirmSet(bits=self.bits & other.bits)
        if self.neg and other.neg:
            return FirmSet(neg=True, bits=self.bits | other.bits)
        pos, neg = (self, other) if not self.neg else (other, self)
        return FirmSet(bits=pos.bits & ~neg.bits)

    def __or__(self, other: "FirmSet") -> "FirmSet":
        if self._both_sets(other):
            if not self.neg and not other.neg:
                return FirmSet(self.ids | other.ids)
            if self.neg and other.neg:
                return FirmSet(self.ids & other.ids, True)
            pos, neg = (self, other) if not self.neg else (other, self)
            return FirmSet(neg.ids - pos.ids, True)
        if not self.neg and not other.neg:
            return FirmSet(bits=self.bits | other.bits)
        if self.neg and other.neg:
            return FirmSet(neg=True, bits=self.bits & other.bits)
        pos, neg = (self, other) if not self.neg else (other, self)
        return FirmSet(neg=True, bits=neg.bits & ~pos.bits)

    def __invert__(self) -> "FirmSet":
        if self._bits is not None:
            return FirmSet(neg=not self.neg, bits=self._bits)
        return FirmSet(self._ids, not self.neg)

    def __contains__(self, i: int) -> bool:
        if self._bits is None:
            return (i in self._ids) != self.neg
        if self._lookup is None:
            self._lookup = bitmaps.Lookup(self._bits)
        return (i in self._lookup) != self.neg

    def size(self) -> int:
        """How many ids are held (left out, for a negative set)."""
        return self._bits.bit_count() if self._bits is not None else len(self._ids)

    def count(self, total: int) -> int:
        return total - self.size() if self.neg else self.size()


def paid_walk_searchers(searchers) -> list[list[str]]:
    """The set of searches a next_paid walk was made for, as it keeps it."""
    return sorted([s.module, s.capability] for s in searchers or ())


class Reader:
    def __init__(self, con: sqlite3.Connection, registers=(), *, manifests=None,
                 enabled=None):
        from harvey.modules import switches

        self.con = con
        self.manifests = list(manifests if manifests is not None else switches.manifests())
        self.schemes = Schemes.load(con, self.manifests)
        self.registers = list(registers)
        self.regs = {a.module: a for a in self.registers}
        # The register that describes firms (names, places, the filters a
        # registry search answers): the one whose adapter resolves filters.
        self.directory = next((a for a in self.registers
                               if type(a).resolve is not regs_mod.Adapter.resolve), None)
        # The firm's key (a tax code): the directory's, else the one legal
        # scheme, so a key on file still shows with no register loaded.
        legal = [sc.name for sc in self.schemes.by_id.values() if sc.legal]
        self.key_scheme = self.directory.keyed_by if self.directory else \
            (legal[0] if len(legal) == 1 else "")
        self._enabled = set(enabled) if enabled is not None else None
        self._signal_ids: dict[str, int] | None = None
        # Each live firm's register key, as far as known (key_of), and the
        # firms known to have none (described locally).
        self.keys: dict[int, tuple[str, object]] = {}
        self._keyless: set[int] = set()
        self._modules: dict[str, int] | None = None
        # The firm sets worked out in this read (firm_set), by filter: a
        # read that asks twice (the Contacts page, the agent's counts)
        # works each out once.
        self._firm_sets: dict[tuple, FirmSet | None] = {}

    # Plumbing

    def x(self, name_or_sql: str, params=()) -> sqlite3.Cursor:
        sql = queries.REGISTRY[name_or_sql].sql if name_or_sql in queries.REGISTRY else name_or_sql
        return self.con.execute(sql, params)

    def one(self, name_or_sql: str, params=()):
        row = self.x(name_or_sql, params).fetchone()
        return row[0] if row else None

    def _by_ids(self, name: str, ids, size: int = CHUNK):
        """A statement over ``json_each(?)`` run a chunk at a time."""
        for chunk in chunks(ids, size):
            yield from self.x(name, (json.dumps(chunk),))

    @property
    def enabled(self) -> set[str]:
        if self._enabled is None:
            from harvey.modules import switches

            stored = dict(self.con.execute("SELECT key, value FROM settings WHERE key LIKE ?",
                                           (switches.PREFIX + "%",)).fetchall())
            self._enabled = set(switches.resolve(stored, self.manifests))
        return self._enabled

    def module_id(self, name: str) -> int | None:
        if self._modules is None:
            self._modules = dict(self.con.execute("SELECT name, id FROM module").fetchall())
        return self._modules.get(name)

    def capability_id(self, module: str, name: str) -> int | None:
        return self.one("SELECT c.id FROM capability c JOIN module m ON m.id = c.module "
                        "WHERE m.name = ? AND c.name = ?", (module, name))

    def signal_id(self, code: str) -> int | None:
        if self._signal_ids is None:
            self._signal_ids = dict(self.con.execute("SELECT code, id FROM signal").fetchall())
        return self._signal_ids.get(code)

    def tally(self) -> dict[str, int]:
        return dict(self.x("overlay.tally").fetchall())

    def total(self) -> int:
        return self.tally().get("firms", 0)

    def marks(self) -> tuple:
        """What changes when a page may have: a firm added or merged, a
        contact, search, person or web signal written, a register reloaded."""
        t = self.tally()
        return (*self.x("overlay.read.marks").fetchone(), t.get("firms"), t.get("firms_merged"),
                t.get("firms_with_contact"), t.get("firms_with_site"), t.get("contacts"),
                self.register_marks(), self._main_file(), self.identity())

    def identity(self) -> int:
        """Moves whenever which firm holds which identifier changes (a firm
        added, removed, merged or unmerged, an identifier added)."""
        return self.one("overlay.read.identity") or 0

    def held_mark(self) -> tuple:
        """What a set worked out from register keys for the firms in
        Harvey is kept by: the registers' load marks, and which firms hold
        which keys in this database."""
        return self.register_marks(), self._main_file(), self.identity()

    def _main_file(self) -> str:
        return next((r[2] for r in self.con.execute("PRAGMA database_list") if r[1] == "main"), "")

    def register_marks(self) -> tuple:
        out = []
        for a in self.registers:
            try:
                out.append((a.module, a.marks(self.con)))
            except sqlite3.Error:
                out.append((a.module, None))
        return tuple(out)

    # Ids

    def fid(self, cid) -> int | None:
        """The firm a company id names (its digits, or an old text id)."""
        if cid is None or cid == "":
            return None
        text = str(cid).strip()
        # (More digits than an integer column holds names no firm.)
        if text.isdigit() and len(text) <= 18:
            found = self.one("SELECT id FROM firm WHERE id = ?", (int(text),))
            if found is not None or len(text) != 12:
                return found
        return self.one("SELECT l.firm FROM legacy_id l JOIN firm f ON f.id = l.firm "
                        "WHERE l.uuid = ?", (text,))

    def live(self, cid) -> int | None:
        fid = self.fid(cid)
        if fid is None:
            return None
        return self.one("SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm WHERE id = ?",
                        (fid,))

    def live_of(self, ids) -> dict[int, int]:
        """{record id: its live firm} (a merged record's survivor)."""
        return dict(self._by_ids("overlay.read.live_of", list(dict.fromkeys(ids))))

    def live_set(self, ids) -> set[int]:
        return set(self.live_of(ids).values())

    def members(self, live_ids) -> dict[int, list[int]]:
        """{live firm: [itself, every record merged into it]}."""
        out = {f: [f] for f in live_ids}
        for live, member in self._by_ids("overlay.read.members", list(live_ids)):
            out[live].append(member)
        return out

    def live_of_keys(self, keys) -> list[int]:
        """The live firms holding these register keys ((scheme, key), in
        order), each once, in the keys' order; keys Harvey doesn't hold are
        left out."""
        by_scheme: dict[str, list[tuple[int, object]]] = {}
        for i, (scheme, key) in enumerate(keys):
            by_scheme.setdefault(scheme, []).append((i, key))
        found: dict[int, int] = {}
        for scheme, items in by_scheme.items():
            s = self.schemes.named(scheme)
            if s is None:
                continue
            for chunk in chunks(items):
                at = [i for i, _ in chunk]
                keys_ = [k for _, k in chunk]
                for pos, live, record in self.x("overlay.read.live_of_keys",
                                                (json.dumps(keys_), s.id)):
                    found[at[pos]] = live
                    if record == live:
                        # The firm's own key: what describes it, sorts it.
                        self.keys.setdefault(live, (scheme, keys_[pos]))
        return list(dict.fromkeys(found[i] for i in sorted(found)))

    def key_of(self, ids) -> dict[int, tuple[str, object]]:
        """{live firm: the register key that describes it} (register_key),
        from the keys a set was resolved from, else its records' idents."""
        missing = [i for i in ids if i not in self.keys and i not in self._keyless]
        if missing:
            for f, idents in self.group_idents(missing).items():
                k = self.register_key(idents)
                if k is None:
                    self._keyless.add(f)
                else:
                    self.keys[f] = k
        return {i: self.keys[i] for i in ids if i in self.keys}

    def idents(self, ids) -> dict[int, list[tuple[str, object]]]:
        """{record: [(scheme name, key)]}."""
        out: dict[int, list] = {}
        for firm, scheme, key in self._by_ids("overlay.firms.idents", list(ids)):
            s = self.schemes.by_id.get(scheme)
            out.setdefault(firm, []).append((s.name if s else str(scheme), key))
        return out

    def group_idents(self, live_ids) -> dict[int, list[tuple[str, object]]]:
        """{live firm: [(scheme, key)] of its own record first, then its
        members'}."""
        groups = self.members(live_ids)
        found = self.idents([m for g in groups.values() for m in g])
        return {f: [k for m in g for k in found.get(m, [])] for f, g in groups.items()}

    def register_key(self, idents) -> tuple[str, object] | None:
        """The key a register describes a firm by: its directory's own
        scheme first (a tax code), else another register scheme (a
        registration number)."""
        own = [(s, k) for s, k in idents if s == self.key_scheme]
        if own:
            return own[0]
        reg = [(s, k) for s, k in idents if (self.schemes.named(s) or None)
               and self.schemes.named(s).register]
        return reg[0] if reg else None

    # ── Firm sets: the Companies and Contacts pages' company conditions ──

    def place_set(self, county: str, locality: str) -> FirmSet:
        """Firms in the county and locality: the register's own search (its
        canonical rows), plus firms no register describes, matched on their
        place line as schema 24 did (contacts.place_matches)."""
        county, locality = (county or "").strip(), (locality or "").strip()
        out: set[int] = set()
        if self.directory is not None:
            out |= self._resolved(("place", county, locality),
                                  {"county": county, "locality": locality})
        local = [f for f, loc, cty in self.x("overlay.read.local_places")
                 if contacts.place_matches(", ".join(p for p in (loc, cty) if p), county, locality)]
        return FirmSet(out | self.live_set(local))

    def _resolved(self, name: tuple, filters: dict) -> set[int]:
        """The live firms a register search finds (kept per load mark, with
        the key each was found by)."""
        key = (*name, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is None:
            hit = self._held_by_ids(filters, ordered=False)
            if hit is not None:
                SETS.put(key, (), hit)
                return set(hit)
        if hit is None:
            before = dict(self.keys)
            self.keys = {}
            found = self.live_of_keys(self.directory.resolve(filters))
            hit = {f: self.keys.get(f) for f in found}
            self.keys = {**hit, **before}
            self.keys = {f: k for f, k in self.keys.items() if k is not None}
            SETS.put(key, (), hit)
        else:
            for f, k in hit.items():
                if k is not None:
                    self.keys.setdefault(f, k)
        return set(hit)

    def _held_by_ids(self, filters: dict, ordered: bool = True):
        """(live firm, the key that describes it or None, its place in the
        register search's order) for what the directory's search finds:
        the registry ids matched to records in one statement (the register's
        key table), never a key list built in Python. Not ``ordered``:
        {live firm: its key or None} instead. None when the register can't
        (an older load's derived tables)."""
        if not hasattr(self.directory, "held_of_ids"):
            return None
        ids = self.directory.resolve_ids(filters)
        held = self.directory.held_of_ids(self.con, ids,
                                          {s.name: s.id for s in self.schemes.by_id.values()})
        if held is None:
            return None
        merged = self._merged()
        if not ordered and not any(r[0] in merged for r in held):
            # (Every record its own live firm, as nearly always: built in C.)
            hit = {r[0]: (r[1], r[2]) for r in held}
            self.keys = {**hit, **self.keys}
            return hit
        at = {rid: n for n, rid in enumerate(ids)}
        out: dict[int, list] = {}
        for record, scheme, k, rid in held:
            live = merged.get(record, record)
            have = out.get(live)
            pos = at.get(rid, len(at))
            if have is None:
                out[live] = [(scheme, k) if record == live else None, pos]
            else:
                if record == live:
                    have[0] = (scheme, k)
                have[1] = min(have[1], pos)
        for f, (k, _) in out.items():
            if k is not None:
                self.keys.setdefault(f, k)
        if not ordered:
            return {f: k for f, (k, _) in out.items()}
        return [(f, k, pos) for f, (k, pos) in out.items()]

    def words_set(self, words: str) -> FirmSet:
        """Firms matching every word typed: the register's search (name,
        activities, place) plus firms no register describes (their own
        full-text index)."""
        from harvey.state import search_terms

        out: set[int] = set()
        if self.directory is not None:
            out |= self._resolved(("words", words), {"words": words})
        match, _ = search_terms(words)
        if match:
            out |= self.live_set(r[0] for r in self.x("overlay.firms.local_search", (match,)))
        return FirmSet(out)

    def words_order(self, words: str) -> list[int]:
        """A search's matches best first: the register's tiers (in their
        order), then firms no register describes."""
        from harvey.state import search_terms

        out: list[int] = []
        if self.directory is not None:
            found = self._held_by_ids({"words": words})
            out = [f for f, _, _ in sorted(found, key=lambda x: x[2])] if found is not None \
                else self.live_of_keys(self.directory.resolve({"words": words}))
        match, _ = search_terms(words)
        if match:
            seen = set(out)
            out += [f for f in dict.fromkeys(self.live_of(
                [r[0] for r in self.x("overlay.firms.local_search", (match,))]).values())
                if f not in seen]
        return out

    def caen_set(self, codes) -> FirmSet:
        """Firms holding any of these classes (4-digit codes, current
        revisions), through the register's activity index."""
        codes = [c for c in codes if str(c).isdigit() and len(str(c)) == 4][:200]
        reader = self._reader_of("ACTIVITY_CODE")
        if not codes or reader is None:
            return FirmSet()
        from harvey.caen import CURRENT_VERSIONS

        keys = []
        for code in codes:
            for version in CURRENT_VERSIONS:
                for scheme in self._schemes_read(reader, "ACTIVITY_CODE"):
                    try:
                        found = reader.range(self.con, "ACTIVITY_CODE", "=", f"{code}/{version}",
                                             scheme="" if scheme == reader.keyed_by else scheme)
                    except ValueError:
                        continue
                    keys += [(scheme, k) for k in found]
        return FirmSet(self.live_of_keys(keys))

    def cui_set(self, digits: str) -> FirmSet:
        s = self.schemes.named(self.key_scheme) if self.key_scheme else None
        if s is None or not str(digits).isdigit() or len(str(digits)) > 18:
            return FirmSet()
        return FirmSet(self.live_of_keys([(s.name, int(digits))]))

    def source_set(self, source: str) -> FirmSet:
        sid = self.one("SELECT id FROM source WHERE name = ?", (source,))
        if sid is None:
            return FirmSet()
        return FirmSet(self.live_set(r[0] for r in self.x("overlay.read.source_firms", (sid,))))

    def kind_set(self, kind: str) -> FirmSet:
        kid = self.one("SELECT id FROM contact_kinds WHERE kind = ?", (kind,))
        if kid is None:
            return FirmSet()
        return FirmSet(self.live_set(r[0] for r in self.x("overlay.read.kind_firms", (kid,))))

    def site_set(self) -> FirmSet:
        return FirmSet(r[0] for r in self.x("overlay.read.with_site"))

    def figures_set(self, bounds: dict) -> FirmSet:
        """Firms whose turnover and headcount are inside the bounds
        ({min,max}_{turnover,employees}, unknown include|exclude): a firm with
        no figure on file passes only with ``unknown`` include."""
        out = FirmSet(neg=True)
        include = bounds.get("unknown") == "include"
        for code, low, high in (("TURNOVER", bounds.get("min_turnover"), bounds.get("max_turnover")),
                                ("EMPLOYEES", bounds.get("min_employees"),
                                 bounds.get("max_employees"))):
            if low is None and high is None:
                continue
            if include:
                outside = FirmSet()
                if low is not None:
                    outside |= self._held(code, "<", low)
                if high is not None:
                    outside |= self._held(code, ">", high)
                out &= ~outside
            else:
                inside = FirmSet(neg=True)
                if low is not None:
                    inside &= self._held(code, ">=", low)
                if high is not None:
                    inside &= self._held(code, "<=", high)
                if inside.neg:
                    inside &= self._held(code, ">=", -1e300)
                out &= inside
        return out

    def predicates(self, f) -> dict:
        """Harvey's own columns a filter asks about: site (1 / 0), contact
        (1 / 0), email, phone, mobile (1 to require)."""
        p = {"site": None, "contact": None, "email": 0, "phone": 0, "mobile": 0}
        if f.website == "no":
            p["site"] = 0
        if f.contact == "any":
            p["contact"] = 1
        elif f.contact == "none":
            p["contact"] = 0
        elif f.contact in ("email", "phone", "mobile"):
            p[f.contact] = 1
        return p

    @staticmethod
    def narrowed(p: dict) -> bool:
        return p["site"] is not None or p["contact"] is not None or bool(
            p["email"] or p["phone"] or p["mobile"])

    def firm_set(self, f, possible: bool = False) -> FirmSet | None:
        """The firms the filter's register, source and signal conditions
        allow (None: no such condition, every live firm); Harvey's own
        columns are ``predicates``. ``possible``: a cohort's unknowns pass
        too (the contact finder searches what isn't known yet)."""
        from dataclasses import astuple

        key = (repr(astuple(f)), possible)
        if key not in self._firm_sets:
            self._firm_sets[key] = self._firm_set(f, possible)
        return self._firm_sets[key]

    def _firm_set(self, f, possible: bool) -> FirmSet | None:
        if f.nothing:
            return FirmSet()
        parts: list[FirmSet] = []
        if (f.county or "").strip() or (f.locality or "").strip():
            parts.append(self.place_set(f.county, f.locality))
        if f.caen:
            parts.append(self.caen_set(f.caen))
        if f.cui:
            parts.append(self.cui_set(f.cui))
        elif getattr(f, "words", "") or f.search:
            parts.append(self.words_set(getattr(f, "words", "") or f.search))
        if f.found_in:
            parts.append(self.source_set(f.found_in))
        if f.contact and f.contact not in COUNTED_CONTACTS:
            parts.append(self.kind_set(f.contact))
        if f.website == "yes":
            parts.append(self.site_set())
        signals = getattr(f, "signals", None)
        if signals:
            require, exclude = signals
            verdicts = ("match", "possible") if possible else ("match",)
            parts.append(self.cohort_set(list(require), list(exclude), [], verdicts))
        bounds = getattr(f, "bounds", None)
        if bounds:
            parts.append(self.figures_set(bounds))
        if not parts:
            return None
        return reduce(lambda a, b: a & b, parts)

    def firm_rows(self, ids) -> dict[int, dict]:
        cols = ("id", "live", "created", "display_name", "website", "domain", "website_trust",
                "n_contacts", "n_email", "n_phone", "n_mobile", "best_trust", "n_people",
                "last_search_at", "profiled_at", "site_read_at", "site_read_deep", "finder_at")
        return {r[0]: dict(zip(cols, r)) for r in self._by_ids("overlay.read.firm_rows", list(ids))}

    def all_live(self) -> list[int]:
        key = ("all", self._main_file())
        mark = self.marks()
        hit = SETS.get(key, mark)
        if hit is None:
            hit = array("q", (r[0] for r in self.x("overlay.read.all_live")))
            SETS.put(key, mark, hit)
        return hit

    def resolve(self, s: FirmSet | None) -> list[int] | set[int]:
        if s is None:
            return self.all_live()
        if not s.neg:
            return s.ids
        return [f for f in self.all_live() if f in s]

    def matching(self, f, possible: bool = False) -> list[int]:
        """Every live firm the filter selects (its register conditions,
        then Harvey's own columns), in no particular order."""
        s = self.firm_set(f, possible)
        p = self.predicates(f)
        ids = self.resolve(s)
        if not self.narrowed(p):
            return list(ids)
        return self.passing(list(ids), p)

    # ── Companies ──

    SORTS = ("created", "name", "place", "turnover", "employees", "contacts", "searched")

    def companies_page(self, f, sort: str, way: str, limit: int, offset: int
                       ) -> tuple[int, list[int], str]:
        """(total, the page's live firm ids in order, how it was read:
        "index" or "snapshot"): companies.page on the overlay."""
        relevance = sort == "relevance" and bool(getattr(f, "words", "") or f.search)
        if sort not in self.SORTS and not relevance:
            sort = "created"
        s = self.firm_set(f)
        p = self.predicates(f)
        if s is None and not relevance:
            page = self._index_page(p, sort, way, limit, offset)
            if page is not None:
                return page
        from dataclasses import astuple

        key = ("companies", sort, repr(astuple(f)), self._main_file())
        mark = self.marks()
        found = ORDERINGS.get(key, mark)
        if found is None:
            if relevance:
                order = self.words_order(getattr(f, "words", "") or f.search)
                found = Ordered(array("q", self._keep(order, s, p, f)))
            else:
                found = self.ordering(self._keep(None, s, p, f), sort)
            # An ordering of every firm (no filter) only changes with a
            # firm added or merged or a register reloaded: kept longer.
            ORDERINGS.put(key, mark, found, LONG_SECONDS if s is None else None)
        return len(found), found.slice(way, offset, limit), "snapshot"

    def _keep(self, order, s, p, f) -> list[int]:
        """The firms a filter keeps (``order``: a search's, else none)."""
        if order is not None:
            ids = order
            if s is not None:
                other = self._without_words(f)
                if other is not None:
                    ids = [i for i in ids if i in other]
        else:
            ids = list(self.resolve(s))
        if not self.narrowed(p):
            return list(ids)
        return self.passing(ids, p)

    def passing(self, ids, p: dict) -> list[int]:
        """The firms of ``ids`` (in their order) that pass Harvey's own
        columns: filtered in SQL, only the passing ids read back."""
        args = (p["site"], p["site"], p["contact"], p["contact"], p["email"], p["phone"],
                p["mobile"])
        keep = set()
        for chunk in chunks(ids):
            keep.update(r[0] for r in self.x("overlay.read.passing", (json.dumps(chunk), *args)))
        return [i for i in ids if i in keep]

    def _without_words(self, f) -> FirmSet | None:
        from dataclasses import replace

        return self.firm_set(replace(f, search="", words=""))

    def _index_page(self, p, sort, way, limit, offset):
        """A page straight from an index (no register condition), or None."""
        asc = ".asc" if way == "asc" else ""
        t = self.tally()
        plain = not self.narrowed(p)
        only_contact = (p["site"] is None and not (p["email"] or p["phone"] or p["mobile"])
                        and p["contact"] is not None)
        if sort == "created":
            if plain:
                ids = [r[0] for r in self.x(f"overlay.firms.new{asc}", (limit, offset))]
                return t.get("firms", 0), ids, "index"
            if only_contact:
                name = "with_contact" if p["contact"] else "no_contact"
                ids = [r[0] for r in self.x(f"overlay.firms.{name}{asc}", (limit, offset))]
                n = t.get("firms_with_contact", 0)
                return (n if p["contact"] else t.get("firms", 0) - n), ids, "index"
            args = (p["site"], p["site"], p["contact"], p["contact"], p["email"], p["phone"],
                    p["mobile"])
            ids = [r[0] for r in self.x(f"overlay.read.walk{asc}", (*args, limit, offset))]
            kinds = [k for k in ("email", "phone", "mobile") if p[k]]
            if (p["site"] == 0 and p["contact"] is None
                    and not (p["email"] or p["phone"] or p["mobile"])):
                total = t.get("firms", 0) - t.get("firms_with_site", 0)
            elif p["site"] is None and p["contact"] is None and len(kinds) == 1 \
                    and f"firms_with_{kinds[0]}" in t:
                # (A contact of that kind is a contact: counted by its tally.)
                total = t[f"firms_with_{kinds[0]}"]
            else:
                total = self._walk_count(args)
            return total, ids, "index"
        if not plain:
            return None
        total = t.get("firms", 0)
        if sort == "contacts":
            # Descending: most contacts first, the firms with none after
            # them, newest first. Ascending: those with none first (newest
            # first, as the list's tie order), then the rest, fewest first.
            if way != "asc":
                return total, [r[0] for r in self.x("overlay.firms.by_contacts",
                                                    (limit, offset))], "index"
            zero = total - t.get("firms_with_contact", 0)
            return total, self._two_part(
                lambda n, at: self.x("overlay.firms.no_contact", (n, at)), zero,
                lambda n, at: self.x("overlay.read.by_contacts.some.asc", (n, at)),
                limit, offset), "index"
        if sort == "searched":
            have = self.one("overlay.firms.searched.count")
            first = "overlay.firms.searched" + asc
            return total, self._two_part(
                lambda n, at: self.x(first, (n, at)), have,
                lambda n, at: self.x("overlay.read.walk_unsearched", (n, at)),
                limit, offset), "index"
        if sort in ("turnover", "employees"):
            code = {"turnover": "TURNOVER", "employees": "EMPLOYEES"}[sort]
            page = self._figure_page(code, way, limit, offset)
            if page is not None:
                return total, page, "index"
        if sort in ("name", "place") and self.orders_current():
            return total, [r[0] for r in self.x(f"overlay.firms.by_{sort}{asc}",
                                                (limit, offset))], "index"
        return None

    def orders_current(self) -> bool:
        """Whether the firms' kept name and place positions belong to the
        directory register's current load (overlay/orders.py)."""
        if not hasattr(self, "_orders_ok"):
            from harvey.overlay import orders

            self._orders_ok = orders.current(self.con, self.registers)
        return self._orders_ok

    def order_values(self, ids, sort: str) -> dict[int, float | None]:
        """{firm: its kept name or place position}."""
        col = 1 if sort == "name" else 2
        return {r[0]: r[col] for r in self._by_ids("overlay.read.orders", list(ids))}

    def by_order(self, ids, sort: str, asc: bool = False) -> list[int]:
        """``ids`` in the kept name or place order (highest first unless
        ``asc``; a firm with no position lowest, ties by id): a few
        thousand sorted by their positions, more read off the index whole."""
        if len(ids) > NAME_ORDER_FROM:
            wanted = ids if isinstance(ids, (set, frozenset)) else set(ids)
            name = f"overlay.read.walk_by_{sort}" + (".asc" if asc else "")
            return [r[0] for r in self.x(name) if r[0] in wanted]
        values = self.order_values(ids, sort)
        return sorted(ids, key=lambda i: (values.get(i) is not None, values.get(i) or 0, i),
                      reverse=not asc)

    def walk_order(self, s: FirmSet, sort: str, offset: int, limit: int | None) -> list[int]:
        """A page of the firms in ``s`` (any set, a negative one too) in the
        kept order, lowest first, read off the index until it is full."""
        out, skipped = [], 0
        for (i,) in self.x(f"overlay.read.walk_by_{sort}.asc"):
            if i not in s:
                continue
            if skipped < offset:
                skipped += 1
                continue
            out.append(i)
            if limit is not None and len(out) >= limit:
                break
        return out

    def _walk_count(self, args) -> int:
        key = ("walk_count", args, self._main_file())
        mark = self.marks()
        hit = SETS.get(key, mark)
        if hit is None:
            hit = self.one("overlay.read.walk.count", args)
            SETS.put(key, mark, hit)
        return hit

    @staticmethod
    def _two_part(first, n_first: int, rest, limit: int, offset: int) -> list[int]:
        """A page of a list made of two index walks one after the other
        (``first`` holds ``n_first`` firms)."""
        out: list[int] = []
        if offset < n_first:
            out = [r[0] for r in first(limit, offset)]
        if len(out) < limit:
            out += [r[0] for r in rest(limit - len(out), max(0, offset - n_first))]
        return out

    def _figure_page(self, code: str, way: str, limit: int, offset: int) -> list[int] | None:
        """A page of every firm by a register figure: the firms with one in
        the register index's order (highest first; ascending, lowest first),
        then the firms with none, newest first. None when no register reads
        the figure through an index."""
        reader = self._reader_of(code)
        if reader is None or reader.page_sql(code) is None:
            return None
        s = self.schemes.named(reader.keyed_by)
        if s is None:
            return None
        have = self._figure_count(reader, code, s.id)
        out: list[int] = []
        if offset < have:
            records = [r[0] for r in self.con.execute(
                reader.page_sql(code, way), (s.id, limit, offset))]
            lives = self.live_of(records)
            out = list(dict.fromkeys(lives[r] for r in records if r in lives))
        if len(out) < limit:
            out += [r[0] for r in self.con.execute(reader.lacking_sql(code), (
                s.id, limit - len(out), max(0, offset - have)))]
        return out

    def _figure_count(self, reader, code: str, scheme_id: int) -> int:
        """How many records in Harvey have a value of the figure: the
        register's count for its load mark when counted, else counted once
        per mark (an index range of the figure, each probed in firm_ident)."""
        got = self.register_count(code, reader.module)
        if got is not None:
            return got[0] + got[1]
        key = ("figure_count", reader.module, code, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is None:
            hit = self.con.execute(reader.page_count_sql(code), (scheme_id,)).fetchone()[0]
            SETS.put(key, (), hit, LONG_SECONDS)
        return hit

    def ordering(self, ids, sort: str) -> Ordered:
        """The firms in the sort's descending order, kept for both
        directions as companies.Ordered: an index sort (created, name,
        place) ties by id; a figure (turnover, employees, contacts, last
        searched) has the firms with one first, the rest after, newest
        first. Ids follow creation (a firm's id is given in the order firms
        are made), so newest first is highest id first."""
        if sort in ("name", "place") and self.orders_current():
            return Ordered(array("q", self.by_order(ids, sort)))
        newest = sorted(ids, reverse=True)
        if sort == "created":
            return Ordered(array("q", newest))
        if sort == "name" and len(newest) > NAME_ORDER_FROM:
            return Ordered(array("q", self._name_order(newest)))
        if sort in ("name", "place"):
            if len(newest) > NAME_ORDER_FROM:
                self._bulk_keys()
            keys = self.sort_keys(newest, sort)
            have = sorted(newest, key=lambda i: (keys.get(i, ""), i), reverse=True)
            return Ordered(array("q", have))
        if sort in ("contacts", "searched"):
            rows = self.firm_rows(newest)
            col = "n_contacts" if sort == "contacts" else "last_search_at"
            valued = [i for i in newest if i in rows and rows[i][col]]
            have = sorted(valued, key=lambda i: (rows[i][col], i), reverse=True)
            rest = [i for i in newest if not (i in rows and rows[i][col])]
            return Ordered(array("q", have), array("q", rest), zero_first=sort == "contacts")
        # A figure from the register that reads it.
        code = {"turnover": "TURNOVER", "employees": "EMPLOYEES"}[sort]
        values = self.values(newest, code)
        have = sorted((i for i in newest if values.get(i) is not None),
                      key=lambda i: (values[i], i), reverse=True)
        rest = [i for i in newest if values.get(i) is None]
        return Ordered(array("q", have), array("q", rest))

    def _bulk_keys(self) -> None:
        """Every firm's register key at once (a sort of most firms): each
        register scheme's index range, the directory's own scheme first."""
        if getattr(self, "_bulk", False):
            return
        merged = self._merged()
        s = self.schemes.named(self.key_scheme) if self.key_scheme else None
        if s is not None:
            # (A firm with no key of the directory's own scheme is looked up
            # one by one by key_of: a few.)
            for firm, key in self.x("overlay.read.idents_of_scheme", (s.id,)):
                if firm not in merged:
                    self.keys.setdefault(firm, (s.name, key))
        self._bulk = True

    def _name_order(self, ids) -> list[int]:
        """A big list by name, highest first: the order of every firm by
        name (kept), less the firms not in the list."""
        every = self._all_by_name()
        if len(ids) >= len(every):
            return list(reversed(every))
        wanted = set(ids)
        return [f for f in reversed(every) if f in wanted]

    def _all_by_name(self) -> list[int]:
        """Every live firm by name, lowest first. The registry's ids follow
        its names' order, so a firm with a tax code sorts by its registry id
        (one walk, no name read); the few others (a registration number
        only, a firm no register describes, a firm with its own name) are
        put in place by comparing names."""
        key = ("by_name", self._main_file())
        mark = self.marks()
        hit = SETS.get(key, mark)
        if hit is not None:
            return hit
        merged = self._merged()
        renamed = dict(self.x("overlay.read.all_display_names"))
        own = self.schemes.named(self.key_scheme) if self.key_scheme else None
        order: list[int] = []
        rids: list[int] = []
        if self.directory is not None and own is not None and \
                hasattr(self.directory, "held_name_order"):
            seen = set()
            for record, rid in self.directory.held_name_order(self.con, own.id):
                live = merged.get(record, record)
                if live not in seen and live not in renamed:
                    seen.add(live)
                    order.append(live)
                    rids.append(rid)
        placed = set(order)
        others = [f for f in self.all_live() if f not in placed]
        names = self.sort_keys(others, "name") if others else {}
        cache: dict[int, str] = {}

        def name_at(pos: int) -> str:
            if pos not in cache:
                cache[pos] = self.directory.name_of_id(self.con, rids[pos])
            return cache[pos]

        inserts: dict[int, list] = {}
        for f in others:
            name, lo, hi = names.get(f, ""), 0, len(order)
            while lo < hi:
                mid = (lo + hi) // 2
                if (name_at(mid), order[mid]) < (name, f):
                    lo = mid + 1
                else:
                    hi = mid
            inserts.setdefault(lo, []).append((name, f))
        out: list[int] = []
        at = 0
        for pos in sorted(inserts):
            out += order[at:pos]
            out += [f for _, f in sorted(inserts[pos])]
            at = pos
        out += order[at:]
        hit = array("q", out)
        SETS.put(key, mark, hit, LONG_SECONDS)
        return hit

    def sort_keys(self, ids, sort: str) -> dict[int, str]:
        """{firm: its name or place line}: Harvey's own name first (a
        rename), the register's, else the firm's local description."""
        keys: dict[int, str] = {}
        by_scheme: dict[str, list] = {}
        firm_of: dict[tuple, int] = {}
        for i, k in self.key_of(ids).items():
            by_scheme.setdefault(k[0], []).append(k[1])
            firm_of[k] = i
        if self.directory is not None and hasattr(self.directory, "sort_keys"):
            for scheme, keylist in by_scheme.items():
                for key, (_, name, place) in self.directory.sort_keys(
                        self.con, keylist, scheme, places=sort == "place").items():
                    keys[firm_of[(scheme, key)]] = name if sort == "name" else place
        local = [i for i in ids if i not in keys]
        for firm, name, county, locality, *_ in self._by_ids("overlay.read.locals", local):
            keys[firm] = (name or "") if sort == "name" else \
                ", ".join(p for p in (locality, county) if p)
        if sort == "name":
            keys.update(self._by_ids("overlay.read.display_names", ids))
        return keys

    def values(self, ids, code: str) -> dict[int, float]:
        """{firm: its newest value of a register figure} for the firms that
        have one (through each firm's register key)."""
        reader = self._reader_of(code)
        if reader is None:
            return {}
        firm_of = {}
        for i, (scheme, key) in self.key_of(ids).items():
            if scheme == reader.keyed_by:
                firm_of.setdefault(key, i)
        out: dict[int, float] = {}
        for key, found in reader.read(self.con, code, list(firm_of)).items():
            value = found[0][0] if found else None
            if value is not None and firm_of[key] not in out:
                out[firm_of[key]] = value
        return out

    def company_rows(self, ids) -> list[dict]:
        """The page's companies in order, each as schema 24's company row
        (id, name, cui, domain, website, industry, company_size, location,
        address, phone, source, source_url, description, external_id,
        created_at, updated_at) plus ``activities``, ``figures``,
        ``contacts``, ``searched_at`` and ``_inputs`` (what the page's
        enrichment reads: phones, people, prospects)."""
        ids = [int(i) for i in ids]
        if not ids:
            return []
        rows = self.firm_rows(ids)
        groups = self.members(ids)
        records = [m for g in groups.values() for m in g]
        idents = self.idents(records)
        locals_ = {r[0]: r for r in self._by_ids("overlay.read.locals", records)}
        sources: dict[int, list] = {}
        for firm, name, ref, first, last in self._by_ids("overlay.read.sources_of", records):
            sources.setdefault(firm, []).append((first or 0, name, ref))
        described = self.describe({i: [k for m in groups[i] for k in idents.get(m, [])]
                                   for i in ids})
        figures = self.figures(ids, idents_of={i: [k for m in groups[i] for k in idents.get(m, [])]
                                               for i in ids})
        phones = self.phones(groups)
        people = self.people_named(groups, described)
        prospects = self.prospects_of(groups)
        out = []
        for i in ids:
            if i not in rows:
                continue
            r = rows[i]
            group = groups[i]
            own = [k for m in group for k in idents.get(m, [])]
            card = described.get(i, {}).get("card") or {}
            # Its own description; a merged record's only for a firm no
            # register describes (a branch merged into a registered firm
            # never lends it its town or address).
            local = locals_.get(i)
            if local is None and not card:
                local = next((locals_[m] for m in group if m in locals_), None)
            srcs = sorted(s for m in group for s in sources.get(m, []))
            key = dict(own).get(self.key_scheme) if self.key_scheme else None
            external = ""
            for scheme, k in own:
                s = self.schemes.named(scheme)
                if s is not None and s.kind == "external":
                    external = f"{s.prefix}{k}"
                    break
            main_phone = (local[5] if local and local[5] else "") or \
                next((p["display"] for p in phones.get(i, [])), "")
            out.append({
                "id": str(i),
                "name": r["display_name"] or card.get("name") or (local[1] if local else "") or "",
                "cui": str(key) if key is not None else "",
                "domain": r["domain"] or "", "website": r["website"] or "",
                "industry": (local[6] if local and local[6] else "") or card.get("industry", ""),
                "company_size": "",
                "location": (", ".join(p for p in (local[3], local[2], local[10]) if p)
                             if local else card.get("location", "")),
                "address": (local[4] if local and local[4] else "") or card.get("address", ""),
                "phone": main_phone,
                "source": srcs[0][1] if srcs else "",
                "source_url": (local[8] if local and local[8] else ""),
                "description": (local[7] if local and local[7] else ""),
                "external_id": external,
                "created_at": _iso(r["created"]),
                "updated_at": _iso(r["created"]),
                "activities": described.get(i, {}).get("activities", []),
                "figures": figures.get(i),
                "contacts": r["n_contacts"],
                "searched_at": _iso(r["last_search_at"]),
                "_inputs": {"phones": [p["value"] for p in phones.get(i, [])],
                            "people": people.get(i, []), "prospects": prospects.get(i, [])},
            })
        return out

    def describe(self, idents_of: dict[int, list]) -> dict[int, dict]:
        """{firm: {"card": the directory's card, "activities": [...], "row":
        the register row}} for firms a register describes."""
        out: dict[int, dict] = {}
        if self.directory is None:
            return out
        by_scheme: dict[str, dict] = {}
        for firm, idents in idents_of.items():
            k = self.register_key(idents)
            if k is not None:
                by_scheme.setdefault(k[0], {})[k[1]] = firm
        for scheme, firms in by_scheme.items():
            try:
                found = self.directory.describe(self.con, list(firms), scheme=scheme)
            except TypeError:
                found = self.directory.describe(self.con, list(firms))
            for key, row in found.items():
                card = self.directory.card(row) if hasattr(self.directory, "card") else row
                out[firms[key]] = {"card": card, "row": row,
                                   "activities": self.activities(row.get("activities") or [])}
        return out

    @staticmethod
    def activities(pairs) -> list[dict]:
        """A register's (code, version) classes as get_company_activities'
        rows: by code, newest revision first, with their names."""
        from harvey.caen import revision_label

        names = _caen_names()
        out = []
        for code, version in sorted({(f"{int(c):04d}", int(v)) for c, v in pairs},
                                    key=lambda p: (p[0], -p[1])):
            ro, en, keywords = names.get((code, version), ("", "", ""))
            out.append({"code": code, "version": version, "confidence": 1.0, "name": ro,
                        "name_en": en, "keywords": keywords,
                        "revision": revision_label(version)})
        return out

    def figures(self, ids, idents_of=None) -> dict[int, dict]:
        """{firm: {year, turnover, employees}} from the register that reads
        the figures (its newest values, each with its year)."""
        reader = self._reader_of("TURNOVER")
        if reader is None or not hasattr(reader, "describe"):
            return {}
        idents_of = idents_of or self.group_idents(ids)
        firm_of = {}
        for i in ids:
            for scheme, key in idents_of.get(i, []):
                if scheme == reader.keyed_by:
                    firm_of.setdefault(key, i)
        out = {}
        for key, row in reader.describe(self.con, list(firm_of)).items():
            # (Each figure is its newest one: the year shown is the turnover's.)
            out.setdefault(firm_of[key], {"year": row.get("turnover_year") or row.get("year"),
                                          "turnover": row.get("turnover"),
                                          "employees": row.get("employees")})
        return out

    def phones(self, groups: dict[int, list[int]]) -> dict[int, list[dict]]:
        """{live firm: its phone contacts, surest and oldest first}."""
        live_of = {m: f for f, g in groups.items() for m in g}
        found: dict[int, dict[int, dict]] = {}
        contact_ids = set()
        links = list(self._by_ids("overlay.read.links_of_firms", list(live_of)))
        for firm, contact, trust, person, location, first in links:
            contact_ids.add(contact)
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(contact_ids))}
        for firm, contact, trust, person, location, first in links:
            row = rows.get(contact)
            if row is None or row[11] != PHONE:
                continue
            best = found.setdefault(live_of[firm], {})
            old = best.get(contact)
            if old is None or (trust, first) < (old["trust"], old["first"]):
                best[contact] = {"value": row[2], "display": row[3] or row[2], "trust": trust,
                                 "first": first or 0, "line_type": LINE_NAMES.get(row[4], ""),
                                 "_id": contact}
        # (Ties, within a second, by the contact on file first.)
        return {f: [{k: v for k, v in p.items() if k != "_id"} for p in sorted(
                    v.values(), key=lambda p: (p["trust"], p["first"], p["_id"]))]
                for f, v in found.items()}

    def people_named(self, groups: dict[int, list[int]], described: dict) -> dict[int, list[dict]]:
        """{live firm: [{name, title}]}: the register's administrators, then
        the people Harvey found, each name once."""
        out: dict[int, list[dict]] = {}
        for f in groups:
            row = described.get(f, {}).get("row") or {}
            for first, last, role in row.get("administrators") or []:
                person = {"name": f"{first} {last}".strip(), "title": role or ""}
                if person["name"] and person not in out.setdefault(f, []):
                    out[f].append(person)
        live_of = {m: f for f, g in groups.items() for m in g}
        for firm, pid, name, norm, role, source, first in self._by_ids("overlay.read.people_of",
                                                                       list(live_of)):
            f = live_of[firm]
            names = {contacts.name_norm(p["name"]) for p in out.get(f, [])}
            if norm not in names:
                out.setdefault(f, []).append({"name": name, "title": role or ""})
        return out

    def prospects_of(self, groups: dict[int, list[int]]) -> dict[int, list[dict]]:
        live_of = {m: f for f, g in groups.items() for m in g}
        cols = ("first_name", "last_name", "title", "email", "email_status", "phone", "score",
                "status", "created_at")
        out: dict[int, list[dict]] = {}
        for r in self._by_ids("overlay.read.prospects_of", list(live_of)):
            out.setdefault(live_of[r[0]], []).append(dict(zip(cols, r[1:])))
        for v in out.values():
            v.sort(key=lambda p: (-(p["score"] or 0), p["created_at"] or ""))
        return out

    def company_sources(self) -> list[str]:
        return [r[0] for r in self.x("overlay.read.firm_source_names") if r[0]][:500]

    def counts(self, ids) -> dict[str, dict]:
        rows = self.firm_rows([int(i) for i in ids])
        return {str(i): {"contacts": rows[int(i)]["n_contacts"] if int(i) in rows else 0,
                         "searched_at": _iso(rows[int(i)]["last_search_at"])
                         if int(i) in rows else None} for i in ids}

    def companies_listing(self, f, sort: str, way: str, limit: int, offset: int
                          ) -> tuple[int, list[dict], str, list[str]]:
        """The Companies page: (total, the page's rows, how it was read,
        every source firms were found in)."""
        total, ids, how = self.companies_page(f, sort, way, limit, offset)
        return total, self.company_rows(ids), how, self.company_sources()

    def reads_figures(self) -> bool:
        """Whether a loaded register reads the yearly figures (the
        turnover and headcount sorts and filters)."""
        return self._reader_of("TURNOVER") is not None

    def cui_set_size(self, digits: str) -> int:
        return len(self.cui_set(digits).ids)

    def ids_for(self, f, sort: str, way: str, limit: int) -> tuple[list[str], int]:
        """(up to ``limit`` company ids of the list in its order, how many
        it has): companies.ids_for on the overlay."""
        total, ids, _ = self.companies_page(f, sort, way, limit, 0)
        return [str(i) for i in ids], total

    def year_figures(self, ids, codes) -> dict[str, tuple[str, dict]]:
        """export.latest_year_figures on the overlay: company -> (year,
        {code: value}) from the registers that read the yearly figures: the
        latest year any of them has, and only the figures of that year."""
        firms = [int(i) for i in ids if str(i).isdigit() and len(str(i)) <= 18]
        keys = self.key_of(firms)
        years: dict[int, dict[str, dict]] = {}
        for code in codes:
            reader = self._reader_of(code)
            if reader is None:
                continue
            firm_of = {k: f for f, (scheme, k) in keys.items() if scheme == reader.keyed_by}
            for key, values in reader.read(self.con, code, list(firm_of)).items():
                for value, year in values:
                    if value is not None and str(year or "").isdigit():
                        years.setdefault(firm_of[key], {}).setdefault(str(year), {})[code] = value
        return {str(f): (max(by, key=int), by[max(by, key=int)]) for f, by in years.items()}

    def modules_of(self, ids) -> list[str]:
        """The modules whose data these companies carry (the sources that
        found them, the registers that describe them, the web signals'
        modules): their attribution travels with an export."""
        firms = [int(i) for i in ids if str(i).isdigit() and len(str(i)) <= 18]
        found: set[str] = set()
        groups = self.members(firms)
        records = [m for g in groups.values() for m in g]
        for firm, name, *_ in self._by_ids("overlay.read.sources_of", records):
            if name and name.startswith("registry:"):
                found.add(name.split(":", 1)[1])
        for firm, code, key, num, text, conf, module, at in self._by_ids(
                "overlay.read.readings_of", records):
            if module:
                found.add(module)
        keys = self.key_of(firms)
        for a in self.registers:
            if a.module not in self.enabled or a.module in found:
                continue
            # A register is credited when it has a value of any signal it
            # reads for one of them.
            mine = [k for scheme, k in keys.values() if scheme == a.keyed_by]
            for code in dict.fromkeys(c for c, scheme in a.by_signal if not scheme):
                if not mine:
                    break
                try:
                    if a.read(self.con, code, mine):
                        found.add(a.module)
                        break
                except sqlite3.Error:
                    break
        return sorted(found)

    # ── One company ──

    def company(self, cid) -> dict | None:
        """The panel's company row (company_rows' shape), or None."""
        fid = self.fid(cid)
        if fid is None:
            return None
        rows = self.company_rows([fid])
        if not rows:
            return None
        row = rows[0]
        live = self.live(fid)
        if live != fid:
            row["merged_into"] = str(live)
        return row

    def company_by(self, column: str, value: str) -> dict | None:
        """StateManager.get_company_by_{cui,domain,external_id} on the
        overlay: the live firm holding that key, as company() gives it."""
        value = (value or "").strip()
        if not value:
            return None
        found = None
        if column == "domain":
            value = value.lower()
            found = self.one("overlay.resolve.domain", (value,)) or self.one(
                "overlay.resolve.key", (schema.FIRM_KEY["domain"], value))
        elif column in ("cui", "external_id"):
            from harvey import resolver

            key = (self.schemes.key("cui", resolver.cui_key(value)) if column == "cui"
                   else self.schemes.key("external", value))
            if key is not None:
                found = self.one("overlay.resolve.ident", key)
        if found is None:
            return None
        return self.company(self.live(found))

    def company_prospects(self, cid) -> list[dict]:
        """The company's prospects (its records'), best scored first, each
        with ``company_id`` the live firm as schema 24's rows carry it."""
        fid = self.live(cid)
        if fid is None:
            return []
        cur = self.x("overlay.read.company_prospects", (fid, fid))
        names = [c[0] for c in cur.description]
        rows = [{**dict(zip(names, r)), "company_id": str(fid)} for r in cur.fetchall()]
        return sorted(rows, key=lambda r: -(r.get("score") or 0))

    def prospect_rows(self) -> list[dict]:
        """Every prospect as its row, newest first (the Contacts page)."""
        cur = self.x("overlay.read.prospect_rows")
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def prospects_by_id(self, ids) -> dict[str, dict]:
        """{prospect id: the outbox's fields of it, ``firm`` its record}."""
        out = {}
        for chunk in chunks(list(dict.fromkeys(ids))):
            cur = self.x("overlay.read.prospects_by_id", (json.dumps(chunk),))
            names = [c[0] for c in cur.description]
            out.update({r[0]: dict(zip(names, r)) for r in cur.fetchall()})
        return out

    def ranked(self, code: str, phrase: str) -> list[str]:
        """The records currently ranked (or in the pack) for a search phrase
        by a rank signal (keyed by the phrase)."""
        return [str(f) for f, in self.x("overlay.read.ranked", (code, phrase))]

    def run_last_at(self, run_id: str) -> int | None:
        """When a run last recorded a reading (unix seconds), or None."""
        return self.one("overlay.read.run_last_at", (run_id,))

    def company_keys(self, cid) -> dict:
        """The register keys of a company's records: {"cuis": [...],
        "external_id": a registration number's external id, "sources":
        {cui: [the sources that gave it]}}."""
        fid = self.live(cid)
        if fid is None:
            return {"cuis": [], "external_id": "", "sources": {}}
        idents = self.group_idents([fid]).get(fid, [])
        cuis = [str(k) for s, k in idents if s == self.key_scheme]
        external = ""
        for scheme, k in idents:
            s = self.schemes.named(scheme)
            if s is not None and s.kind == "external" and s.register:
                external = f"{s.prefix}{k}"
                break
        return {"cuis": list(dict.fromkeys(cuis)), "external_id": external,
                "sources": {c: [] for c in cuis}}

    def of_company(self, cid) -> dict:
        """contacts.of_company on the overlay: the company's contacts (one
        row per contact and person, with its best trust, its sources and
        how many other firms it reaches), people, places, sources and the
        latest searches."""
        fid = self.live(cid)
        empty = {"contacts": [], "people": [], "locations": [], "sources": [], "searches": []}
        if fid is None:
            return empty
        group = self.members([fid])[fid]
        links = list(self._by_ids("overlay.read.links_of_firms", group))
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows",
                                              list({l[1] for l in links}))}
        evidence = {}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 list({l[3] for l in links if l[3]}))}
        for firm, contact, source, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[l[0], l[1]] for l in links], 500):
            key = (contact, persons[person][3] if person in persons else person)
            evidence.setdefault(key, set()).add(source)
        # A person by name across the records (each record keeps its own
        # row of someone named at two of them): one row per contact and name.
        norm_of = {pid: r[3] for pid, r in persons.items()}
        merged: dict[tuple, dict] = {}
        for firm, contact, trust, person, location, first in links:
            row = rows.get(contact)
            if row is None:
                continue
            key = (contact, norm_of.get(person, person))
            old = merged.get(key)
            if old is not None and old["rank"] <= trust:
                continue
            merged[key] = {"id": contact, "kind": row[1], "value": row[2],
                           "display": row[3] or row[2], "line_type": LINE_NAMES.get(row[4], ""),
                           "status": STATUS_NAMES.get(row[5], ""),
                           "do_not_contact": bool(row[6]), "rank": trust,
                           "person": persons[person][2] if person in persons else "",
                           "shared_with": max(0, row[9] - 1), "kind_id": row[11]}
        out = []
        for (contact, person), link in merged.items():
            sources = sorted(evidence.get((contact, person), set()))
            out.append({**{k: v for k, v in link.items() if k not in ("rank", "kind_id")},
                        "trust": TRUST_NAMES.get(link["rank"], "unconfirmed"),
                        "sources": sources, "source": sources[0] if sources else "",
                        "_order": (link["kind"] or "", link["rank"], link["value"])})
        out.sort(key=lambda c: c.pop("_order"))
        linked_people = {l[3] for l in links if l[3]}
        reached = {norm_of[p] for p in linked_people if p in norm_of}
        people, named = [], {}
        searched = dict(self._by_ids("overlay.read.person_searched",
                                     [r[1] for r in self._by_ids("overlay.read.people_of", group)]))
        for firm, pid, name, norm, role, source, first in self._by_ids("overlay.read.people_of",
                                                                       group):
            # Once by name across the records (the survivor's row first).
            at = searched.get(pid)
            if norm in named:
                seen = named[norm]
                seen["role"] = seen["role"] or role
                if at and (seen["_at"] is None or at > seen["_at"]):
                    seen["_at"], seen["searched_at"] = at, _db_time(at)
                continue
            named[norm] = {"id": pid, "name": name, "role": role, "source": source or "",
                           "searched_at": _db_time(at), "_at": at,
                           "reached": pid in linked_people or norm in reached, "_n": norm}
            people.append(named[norm])
        for p in people:
            p.pop("_at")
        people.sort(key=lambda p: p.pop("_n"))
        places = [dict(zip(("id", "name", "address", "locality", "county", "phone", "source",
                            "external_id"), r)) for r in self.con.execute(
            f"SELECT id, name, address, locality, county, phone, source, external_id FROM "
            f"locations WHERE firm IN ({','.join('?' * len(group))}) ORDER BY id", group)]
        srcs = []
        for firm, name, ref, first, last in self._by_ids("overlay.read.sources_of", group):
            srcs.append({"source": name, "ref": ref, "first_seen": _db_time(first),
                         "last_seen": _db_time(last), "_t": first or 0})
        srcs.sort(key=lambda s: (s.pop("_t"), s["source"]))
        searches = []
        for member in group:
            for row in self.x("overlay.read.searches_of", (0, member)):
                searches.append(row)
        for firm, pid, name, *_ in self._by_ids("overlay.read.people_of", group):
            for row in self.x("overlay.read.searches_of", (1, pid)):
                searches.append((*row[:2], name, *row[3:]))
        found = []
        for row in sorted(searches, key=lambda r: (r[6] or 0, r[0]), reverse=True)[:50]:
            sid, is_person, target, module, capability, looked, at, outcome, n, cost = row
            found.append({"target": "person" if is_person else "company",
                          "person": target if is_person else "", "module": module,
                          "capability": capability, "looked_for": self.looked_names(looked),
                          "searched_at": _db_time(at), "outcome": OUTCOME_NAMES.get(outcome, ""),
                          "found": n, "cost": cost})
        return {"contacts": out, "people": people, "locations": places, "sources": srcs,
                "searches": found}

    def looked_names(self, mask) -> str:
        if not mask:
            return ""
        bits = dict(self.con.execute("SELECT bit, name FROM looked_for").fetchall())
        return ",".join(n for b, n in sorted(bits.items()) if mask & b)

    def readings_of(self, cid) -> list[dict]:
        """The company's current web-signal readings (its records'),
        newest first: {signal_code, value_num, value_text, module,
        observed_at}."""
        fid = self.live(cid)
        if fid is None:
            return []
        group = self.members([fid])[fid]
        out = []
        for firm, code, key, num, text, conf, module, at in self._by_ids(
                "overlay.read.readings_of", group):
            out.append({"signal_code": code, "value_num": num,
                        "value_text": text if text is not None else (key or None),
                        "module": module or "", "observed_at": _iso(at), "key": key,
                        "confidence": conf})
        out.sort(key=lambda r: r["observed_at"] or "", reverse=True)
        return out

    def observations(self, cid, code: str = "", latest_only: bool = False,
                     limit: int = 500) -> list[dict]:
        """StateManager.get_observations on the overlay, for one company
        (its records' web-signal history), newest first, in schema 24's
        row shape. ``collector`` is "discover" for a reading a search's hit
        gave (a capability that queries), else the collector the module's
        manifest names for the signal."""
        fid = self.live(cid)
        if fid is None:
            return []
        group = json.dumps(self.members([fid])[fid])
        if code:
            sid = self.signal_id(code)
            if sid is None:
                return []
            rows = self.x("overlay.read.obs_of_signal", (group, sid)).fetchall()
        else:
            rows = self.x("overlay.read.obs_of", (group,)).fetchall()
        ops = {(m.id, cap.get("id")): cap.get("op") for m in self.manifests
               for cap in m.capabilities}
        named = {(m.id, sig["code"]): sig.get("collector") or "" for m in self.manifests
                 for sig in m.signals}
        out = []
        for oid, sig, num, text, conf, module, cap, url, at, run in sorted(
                rows, key=lambda r: (r[8], r[0]), reverse=True):
            out.append({
                "id": oid, "company_id": str(fid), "signal_code": sig, "value_num": num,
                "value_text": text or "", "confidence": conf,
                "collector": "discover" if ops.get((module, cap)) == "query"
                             or cap == schema.SEARCH_HIT else named.get((module, sig), ""),
                "module": module or "", "capability": cap or "", "evidence_url": url or "",
                "run_id": run or "", "observed_at": _db_time(at)})
        if latest_only:
            seen = set()
            out = [r for r in out if not (r["signal_code"] in seen or seen.add(r["signal_code"]))]
        return out[:int(limit)]

    def register_readings(self, cid, codes=None) -> list[dict]:
        """Every value the loaded registers give the company (its register
        keys), newest first: {signal_code, value_num, value_text, module,
        observed_at (the register's own date)}."""
        fid = self.live(cid)
        if fid is None:
            return []
        idents = self.group_idents([fid]).get(fid, [])
        out = []
        for a in self.registers:
            if a.module not in self.enabled:
                continue
            for (code, scheme), read in a.by_signal.items():
                if codes is not None and code not in codes:
                    continue
                want = scheme or a.keyed_by
                keys = [k for s, k in idents if s == want]
                if not keys:
                    continue
                try:
                    found = a.read(self.con, code, keys, scheme=scheme)
                except sqlite3.Error:
                    continue
                for key, values in found.items():
                    for value, date in values:
                        num = value if isinstance(value, (int, float)) else None
                        text = value if isinstance(value, str) else (
                            str(date) if a.by_signal[(code, scheme)].date.endswith("_year")
                            else None)
                        out.append({"signal_code": code, "value_num": num, "value_text": text,
                                    "module": a.module, "observed_at": str(date or "")})
        out.sort(key=lambda r: r["observed_at"], reverse=True)
        return out

    def findings(self, cid) -> dict:
        """The panel's findings on the overlay: every phone and address on
        file, each with every source that gave it (a contact's evidence, a
        register's value: official sources first, then the newest), and
        the latest value of each fact an official register gives."""
        from harvey.collectors.phones import field_phones, phone_hit

        fid = self.live(cid)
        if fid is None:
            return {"phones": [], "addresses": [], "facts": []}
        mans = {m.id: m for m in self.manifests}
        official = {m for m, man in mans.items() if man.official}

        def who(module: str) -> str:
            m = mans.get(module or "")
            return (m.name or m.id) if m else (module or "")

        lists: dict[str, dict[str, dict]] = {"PHONE_NUMBER": {}, "ADDRESS": {}}

        def add(code, value, module, seen, confidence=1.0, url=""):
            value = (value or "").strip()
            if not value:
                return
            entry = lists[code].setdefault(value, {"value": value, "sources": []})
            entry["sources"].append({"module": module or "", "name": who(module),
                                     "official": (module or "") in official,
                                     "observed_at": seen or "", "confidence": confidence,
                                     "evidence_url": url or ""})

        group = self.members([fid])[fid]
        links = list(self._by_ids("overlay.read.links_of_firms", group))
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows",
                                              list({l[1] for l in links}))}
        for firm, contact, src, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[l[0], l[1]] for l in links], 500):
            row = rows.get(contact)
            if row is not None and row[11] == PHONE:
                add("PHONE_NUMBER", row[2], src, _iso(first), 1.0, url)
        readings = self.register_readings(fid)
        facts: dict[str, dict] = {}
        vocab = {v["code"]: v for v in self.signal_vocabulary()}
        for r in readings:
            code, module = r["signal_code"], r["module"]
            if code == "PHONE_NUMBER":
                # A register's field as it writes it (E.164 numbers, or a
                # national number as filed): each number once, in E.164,
                # so two sources of one number are one finding.
                raw = str(r["value_text"] or "")
                if raw.startswith("+"):
                    numbers = [(phone_hit(n), n) for n in raw.split()]
                else:
                    markets = getattr(mans.get(module), "markets", ()) or ()
                    numbers = [(h, h.number) for h in field_phones(
                        raw, markets[0] if markets else None)]
                for hit, number in numbers:
                    add(code, hit.number if hit else number, module, r["observed_at"])
                continue
            if code == "ADDRESS":
                for line in str(r["value_text"] or "").split("\n"):
                    add(code, line, module, r["observed_at"])
                continue
            if module not in official or code in ("ACTIVITY_CODE", "CONTACT_FOUND",
                                                  "DECISION_MAKER_TITLE") or code in facts:
                continue
            meta = vocab.get(code) or {}
            year = r["observed_at"] if meta.get("keyed_by") == "year" else ""
            facts[code] = {"code": code, "label": meta.get("label") or code,
                           "value_type": meta.get("value_type") or "text",
                           "unit": meta.get("unit") or "", "value_num": r["value_num"],
                           "value_text": "" if year else (r["value_text"] or ""),
                           "year": str(year), "module": module, "name": who(module),
                           "observed_at": r["observed_at"], "evidence_url": ""}
        row = (self.company_rows([fid]) or [{}])[0]
        if row.get("address") and row["address"] not in lists["ADDRESS"]:
            module = self.directory.module if self.directory and row.get("cui") else ""
            add("ADDRESS", row["address"], module, row.get("created_at"), 1.0,
                row.get("source_url", ""))

        def ordered(entries: dict) -> list[dict]:
            out = list(entries.values())
            for e in out:
                e["sources"].sort(key=lambda x: x["observed_at"] or "", reverse=True)
                e["sources"].sort(key=lambda x: not x["official"])
                e["official"] = any(x["official"] for x in e["sources"])
                e["latest"] = max((x["observed_at"] or "" for x in e["sources"]), default="")
            out.sort(key=lambda e: e["latest"], reverse=True)
            out.sort(key=lambda e: not e["official"])
            return out

        phones = ordered(lists["PHONE_NUMBER"])
        for e in phones:
            hit = phone_hit(e["value"])
            e["display"] = hit.display if hit else e["value"]
        order = [m.id for m in self.manifests]
        fact_list = sorted(facts.values(), key=lambda f: (
            order.index(f["module"]) if f["module"] in order else len(order), f["code"]))
        return {"phones": phones, "addresses": ordered(lists["ADDRESS"]), "facts": fact_list}

    # Shown in their own sections of the panel, not again among the signals.
    OWN_SECTIONS = ("PHONE_NUMBER", "ADDRESS", "CONTACT_FOUND", "ACTIVITY_CODE")

    def panel_signals(self, cid) -> list[dict]:
        """companies.signals on the overlay: the latest value of each signal
        the firm has (a web signal's reading, a register's value)."""
        vocab = {v["code"]: v for v in self.signal_vocabulary()}
        latest: dict[str, dict] = {}
        for r in [*self.register_readings(cid), *self.readings_of(cid)]:
            code = r["signal_code"]
            if code in self.OWN_SECTIONS or code in latest:
                continue
            meta = vocab.get(code) or {}
            yearly = meta.get("keyed_by") == "year"
            latest[code] = {"code": code, "label": meta.get("label") or code,
                            "value_type": meta.get("value_type") or "text",
                            "unit": meta.get("unit") or "", "value_num": r["value_num"],
                            "value_text": "" if yearly else (r["value_text"] or ""),
                            "year": str(r.get("key") or r["observed_at"] or "") if yearly else "",
                            "module": r["module"], "observed_at": r["observed_at"]}
        return sorted(latest.values(), key=lambda x: x["code"])

    def panel(self, cid) -> dict | None:
        """Everything the company panel shows from the overlay: its row,
        findings, contacts (reach), register keys and signals."""
        row = self.company(cid)
        if row is None:
            return None
        row["website_basis"] = self.website_basis(cid)
        return {"row": row, "findings": self.findings(cid), "reach": self.of_company(cid),
                "keys": self.company_keys(cid), "signals": self.panel_signals(cid),
                "counts": self.counts([row["id"]])[row["id"]]}

    # ── Search (harvey search, the Companies search box) ──

    def search_companies(self, query: str, limit: int = 50, offset: int = 0
                         ) -> tuple[int, list[dict]]:
        """StateManager.search_companies on the overlay: firms in Harvey
        matching every word, best first (the register's tiers, then firms
        no register describes), each with its activities, the ones the
        query named first."""
        from harvey.state import _activity_matches, search_terms

        match, terms = search_terms(query)
        if not match:
            return 0, []
        key = ("search", query, self._main_file())
        mark = self.marks()
        order = ORDERINGS.get(key, mark)
        if order is None:
            order = array("q", self.words_order(query))
            ORDERINGS.put(key, mark, order)
        page = list(order[offset:offset + max(1, min(int(limit), 500))])
        rows = []
        for r in self.company_rows(page):
            acts = r["activities"]
            for a in acts:
                a["matched"] = _activity_matches(a, terms)
            acts.sort(key=lambda a: not a["matched"])
            rows.append({k: r[k] for k in ("id", "name", "cui", "location", "address", "phone",
                                           "website", "domain", "industry", "source")}
                        | {"activities": acts, "_inputs": r["_inputs"]})
        return len(order), rows

    def company_by_key(self, digits: str) -> list[dict]:
        """A company holding this tax code (the search box's number)."""
        found = self.cui_set(digits)
        rows = self.company_rows(sorted(found.ids))
        return rows

    # ── Contacts ──

    def contacts_page(self, f, sort: str, way: str, limit: int, offset: int,
                      cap: int | None = None) -> tuple[int, bool, list[int]]:
        """(total up to ``cap``, more than that, the page's contact ids):
        the Contacts page on the overlay. A contact matches the company
        conditions when any live firm it reaches does. ``cap``: by default
        contacts.COUNT_CAP, read when called."""
        cap = contacts.COUNT_CAP if cap is None else cap
        sort = sort if sort in contacts.CONTACT_SORTS else "first_seen"
        s = self.firm_set(f)
        p = self.predicates(f)
        plain_contact = not (f.status or f.trust in contacts.TRUST_LEVELS or f.has_person
                             or f.source or (f.q or "").strip())
        t = self.tally()
        asc = ".asc" if (way or "").lower() == "asc" else ""
        if s is None and not self.narrowed(p) and plain_contact:
            kind_id = self._kind_id(f.kind)
            if not f.kind and sort in ("first_seen", "companies"):
                name = "overlay.contacts.new" if sort == "first_seen" else "overlay.contacts.by_reach"
                ids = [r[0] for r in self.x(name + asc, (limit, offset))]
                n = t.get("contacts", 0)
                return min(n, cap), n > cap, ids
            if f.kind and kind_id == PHONE and sort == "first_seen":
                line = schema.MOBILE if f.kind == "mobile" else None
                ids = [r[0] for r in self.x("overlay.read.contacts.walk" + asc,
                                            (PHONE, line, line, limit, offset))]
                n = t.get("contacts_mobile" if line else "contacts_phone", 0)
                return min(n, cap), n > cap, ids
        from dataclasses import astuple

        key = ("contacts", sort, repr(astuple(f)), self._main_file())
        mark = self.marks()
        found = ORDERINGS.get(key, mark)
        if found is None:
            found = Ordered(array("q", self._contact_order(self._contact_ids(f, s, p), sort)))
            ORDERINGS.put(key, mark, found)
        n = len(found)
        return min(n, cap), n > cap, found.slice("asc" if asc else "desc", offset, limit)

    def _kind_id(self, kind: str) -> int | None:
        if kind == "mobile":
            return PHONE
        if not kind:
            return None
        return self.one("SELECT id FROM contact_kinds WHERE kind = ?", (kind,))

    def contact_ids(self, f) -> list[int]:
        """Every contact the Contacts page's filter keeps (as contact_counts
        counts them), in no particular order: an agent's set, frozen."""
        return self._contact_ids(f, self.firm_set(f), self.predicates(f))

    def _contact_ids(self, f, s: FirmSet | None, p: dict, only=None) -> list[int]:
        """Every contact the filter keeps; ``only`` (contact ids): of those,
        the ones it keeps, read by their ids and their firms' (a rolling
        campaign's changed contacts), never the filter's whole set."""
        firms = None
        if s is not None or self.narrowed(p):
            firms = set(self.matching(f)) if only is None else self._matching_of(s, p, only)
        kind_id = self._kind_id(f.kind)
        status = schema.CONTACT_STATUS.get(f.status) if f.status else None
        trust = contacts.TRUST_LEVELS.index(f.trust) if f.trust in contacts.TRUST_LEVELS else None
        if (f.kind and kind_id is None) or (f.status and status is None):
            return []           # a kind or status no contact has
        links = None
        candidates = None
        if firms is not None:
            # Each firm and the records merged into it (a few: the merged
            # map, not a probe per firm).
            live_of = {f: f for f in firms}
            live_of.update({m: g for m, g in self._merged().items() if g in live_of})
            if f.source or f.has_person:
                links = list(self._by_ids("overlay.read.links_of_firms", list(live_of)))
                candidates = {l[1] for l in links}
            else:
                # Only which contacts they reach (of the kind asked for).
                line = schema.MOBILE if f.kind == "mobile" else None
                candidates = {r[0] for chunk in chunks(list(live_of)) for r in self.x(
                    "overlay.read.contacts_of_firms",
                    (json.dumps(chunk), kind_id, kind_id, line, line))}
        if only is not None:
            # (The rows below check kind, status and trust; the links,
            # source and person: no index read of every match needed.)
            candidates = set(only) if candidates is None else candidates & set(only)
        # The narrowest condition a table answers by index (or a read of the
        # contact table returning the few that match) gives the candidates.
        narrow = []
        if kind_id is not None and kind_id != PHONE:
            narrow.append(("overlay.read.contacts.of_kind", kind_id))
        if f.source:
            sid = self.one("SELECT id FROM source WHERE name = ?", (f.source,))
            narrow.append(("overlay.read.contacts.by_source", sid if sid is not None else -1))
        if status is not None and status != schema.CONTACT_STATUS["valid"]:
            narrow.append(("overlay.read.contacts.by_status", status))
        if trust is not None and trust != schema.TRUST["confirmed"]:
            narrow.append(("overlay.read.contacts.by_trust", trust))
        if f.has_person == "yes":
            narrow.append(("overlay.read.contacts.with_person", None))
        for name, arg in narrow if only is None else ():
            hit = {r[0] for r in self.x(name, () if arg is None else (arg,))}
            candidates = hit if candidates is None else candidates & hit
        words = [w for w in (f.q or "")[:200].split()[:8]]
        for word in words:
            hit = self._word_contacts(word, candidates if only is not None else None)
            candidates = hit if candidates is None else candidates & hit
        if candidates is None:
            candidates = {r[0] for r in self.x(
                "overlay.read.contacts.walk" if kind_id == PHONE else "overlay.contacts.new",
                ((PHONE, schema.MOBILE if f.kind == "mobile" else None,
                  schema.MOBILE if f.kind == "mobile" else None, 1 << 62, 0)
                 if kind_id == PHONE else (1 << 62, 0)))}
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(candidates))}
        # (The sort reads the same rows: contacts_page.)
        self._contact_rows = rows
        keep = []
        for cid, r in rows.items():
            if kind_id is not None and r[11] != kind_id:
                continue
            if f.kind == "mobile" and r[4] != schema.MOBILE:
                continue
            if status is not None and r[5] != status:
                continue
            if trust is not None and (r[10] if r[10] is not None else 2) != trust:
                continue
            keep.append(cid)
        if not (f.source or f.has_person) and links is None:
            # (A firm condition's candidates are its firms' contacts already.)
            return keep
        # Link conditions: the source and the person go with a link of a
        # firm that matches (any live firm when there is no firm condition).
        keep_set = set(keep)
        if links is None:
            links = list(self._by_ids("overlay.read.links_of_contacts", keep))
            lives = self.live_of([l[1] for l in links])
            links = [(l[1], l[0], *l[2:]) for l in links]
        else:
            lives = {m: live_of[m] for m in live_of}
        source_ok: set[tuple] | None = None
        if f.source:
            source_ok = set()
            for firm, contact, src, *_ in self._by_ids(
                    "overlay.read.evidence", [[l[0], l[1]] for l in links if l[1] in keep_set],
                    500):
                if src == f.source:
                    source_ok.add((firm, contact))
        out = set()
        with_person = set()
        for firm, contact, trust_, person, location, first in links:
            if contact not in keep_set:
                continue
            if person:
                with_person.add(contact)
            if firms is not None and lives.get(firm) not in firms:
                continue
            if source_ok is not None and (firm, contact) not in source_ok:
                continue
            if f.has_person == "yes" and not person:
                continue
            out.add(contact)
        if f.has_person == "no":
            out = {c for c in out if c not in with_person} if firms is not None or f.source \
                else {c for c in keep_set if c not in self._contacts_with_person(keep)}
        return [c for c in keep if c in out]

    # ── Counts over a filter (the agent's counts view) ──

    FIRM_COUNTS = ("firms", "with_website", "without_contact", "with_email", "with_phone",
                   "with_mobile", "with_email_or_mobile", "site_read", "finder_searched",
                   "searched", "with_people")

    def firm_counts(self, f) -> dict:
        """What the Companies page's filter selects, counted: the firms,
        with and without a website, without a contact, with an email, a
        phone, a mobile (email or mobile), their site read, searched by
        the contact finder or by anything, people named. No firm condition:
        the tallies (the reads a tally doesn't keep: None)."""
        s = self.firm_set(f)
        p = self.predicates(f)
        if s is None and not self.narrowed(p):
            t = self.tally()
            out = dict.fromkeys(self.FIRM_COUNTS)
            out.update(firms=t.get("firms", 0), with_website=t.get("firms_with_site", 0),
                       without_contact=t.get("firms", 0) - t.get("firms_with_contact", 0),
                       with_email=t.get("firms_with_email", 0),
                       with_phone=t.get("firms_with_phone", 0),
                       with_mobile=t.get("firms_with_mobile", 0),
                       with_email_or_mobile=self.one("overlay.agent.firms_reached"))
        else:
            ids = self.matching(f)
            sums = [0] * len(self.FIRM_COUNTS)
            for chunk in chunks(ids, 5000):
                row = self.x("overlay.agent.firm_counts", (json.dumps(chunk),)).fetchone()
                sums = [a + (b or 0) for a, b in zip(sums, row)]
            out = dict(zip(self.FIRM_COUNTS, sums))
        if out["firms"] is not None and out["with_website"] is not None:
            out["without_website"] = out["firms"] - out["with_website"]
        return out

    def contact_counts(self, f) -> dict:
        """What the Contacts page's filter selects, counted: contacts by
        kind, status and trust, the mobiles, the emails that can be sent
        to (verified or risky, not on the do-not-contact list) and those on
        it. No condition at all: the contact table counted whole."""
        s = self.firm_set(f)
        p = self.predicates(f)
        plain_contact = not (f.status or f.trust in contacts.TRUST_LEVELS or f.has_person
                             or f.source or (f.q or "").strip())
        statuses = {v: k for k, v in schema.CONTACT_STATUS.items()}
        out = {"contacts": 0, "by_kind": {}, "by_status": {}, "by_trust": {}, "mobile": 0,
               "deliverable_email": 0, "do_not_contact": 0}

        def add(kind, line, trust, status, n, dnc):
            if f.kind == "mobile" and not (kind == "phone" and line == schema.MOBILE):
                return
            if f.kind and f.kind != "mobile" and kind != f.kind:
                return
            out["contacts"] += n
            out["by_kind"][kind] = out["by_kind"].get(kind, 0) + n
            name = statuses.get(status, "unknown") if status is not None else "unknown"
            out["by_status"][name] = out["by_status"].get(name, 0) + n
            level = TRUST_NAMES.get(trust, "unlinked") if trust is not None else "unlinked"
            out["by_trust"][level] = out["by_trust"].get(level, 0) + n
            if kind == "phone" and line == schema.MOBILE:
                out["mobile"] += n
            out["do_not_contact"] += dnc
            if kind == "email" and status in (schema.CONTACT_STATUS["verified"],
                                              schema.CONTACT_STATUS["risky"]):
                out["deliverable_email"] += n - dnc

        if s is None and not self.narrowed(p) and plain_contact:
            for kind, line, trust, status, n, dnc in self.x("overlay.agent.contacts"):
                add(kind, line, trust, status, n, dnc or 0)
            return out
        for cid in self._contact_ids(f, s, p):
            r = self._contact_rows[cid]
            add(r[1], r[4], r[10], r[5], 1, 1 if r[6] else 0)
        return out

    def _contacts_with_person(self, ids) -> set[int]:
        return {l[0] for l in self._by_ids("overlay.read.links_of_contacts", list(ids)) if l[3]}

    def _word_contacts(self, word: str, among=None) -> set[int]:
        """Contacts a word of the search box finds: in its value (a number
        however it is written), a firm it reaches (by name, through the
        register's search), or a person it reaches. ``among`` (contact
        ids): only which of those it finds, their values read by id."""
        folded = contacts.fold(word)
        like = contacts.like(folded)
        if among is not None:
            return self._word_contacts_among(word, folded, like, set(among))
        found = {r[0] for r in self.x("overlay.read.contacts.value_like", (like,))}
        for form in self._digit_forms(word):
            found |= {r[0] for r in self.x("overlay.read.contacts.value_like",
                                           ("%" + form + "%",))}
        persons = {r[0] for r in self.x("overlay.read.persons_like", (like,))}
        firms = self.words_set(word).ids if len(folded) >= 2 else set()
        if firms:
            groups = self.members(firms)
            found |= {l[1] for l in self._by_ids("overlay.read.links_of_firms",
                                                 [m for g in groups.values() for m in g])}
        if persons:
            for l in self._by_ids("overlay.read.links_of_firms",
                                  list({r[1] for r in self.x("overlay.read.persons_like",
                                                             (like,))})):
                if l[3] in persons:
                    found.add(l[1])
        return found

    @staticmethod
    def _digit_forms(word: str) -> set[str]:
        """A number of the search box as a phone may be written on file."""
        import re

        digits = re.sub(r"[\s+\-.()/]", "", word)
        if not (digits.isdigit() and len(digits) >= 3):
            return set()
        forms = {digits}
        if digits.startswith("0") and not digits.startswith("00"):
            forms.add("40" + digits[1:])
        elif digits.startswith("00"):
            forms.add(digits[2:])
        return forms

    def _word_contacts_among(self, word: str, folded: str, like: str, among: set[int]
                             ) -> set[int]:
        """_word_contacts for a few contacts: their values by id, their
        firms (live, through their links) against the word's firms, their
        people against the word's."""
        if not among:
            return set()
        ids = sorted(among)
        found = {r[0] for r in self._by_ids_with("overlay.read.contacts.value_like_of", ids,
                                                 (like,))}
        for form in self._digit_forms(word):
            found |= {r[0] for r in self._by_ids_with("overlay.read.contacts.value_like_of",
                                                      ids, ("%" + form + "%",))}
        links = list(self._by_ids("overlay.read.links_of_contacts", ids))
        lives = self.live_of([l[1] for l in links])
        firms = self.words_set(word).ids if len(folded) >= 2 else set()
        persons = {r[0] for r in self.x("overlay.read.persons_like", (like,))}
        for contact, firm, _, person, *_ in links:
            if lives.get(firm) in firms or (person and person in persons):
                found.add(contact)
        return found

    def _by_ids_with(self, name: str, ids, params=(), size: int = CHUNK):
        """_by_ids for a statement taking more after its list."""
        for chunk in chunks(ids, size):
            yield from self.x(name, (json.dumps(chunk), *params))

    def _matching_of(self, s: FirmSet | None, p: dict, contacts_: set[int]) -> set[int]:
        """The live firms of these contacts' links that the filter's firm
        conditions keep (``matching`` for a few contacts)."""
        links = list(self._by_ids("overlay.read.links_of_contacts", sorted(contacts_)))
        lives = list(dict.fromkeys(self.live_of([l[1] for l in links]).values()))
        if s is not None:
            lives = [f for f in lives if f in s]
        if self.narrowed(p):
            lives = self.passing(lives, p)
        return set(lives)

    def _contact_order(self, ids, sort: str) -> list[int]:
        """Contacts in the sort's descending order (ties by id)."""
        rows = getattr(self, "_contact_rows", None) or {}
        if any(c not in rows for c in ids):
            rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(ids))}
        col = {"value": 2, "kind": 1, "line_type": 4, "status": 5, "first_seen": 7,
               "last_seen": 8, "companies": 9}.get(sort)
        if sort == "trust":
            key = lambda c: (-(rows[c][10] if rows[c][10] is not None else 2), c)  # noqa: E731
        elif sort in ("line_type", "status"):
            names = LINE_NAMES if sort == "line_type" else STATUS_NAMES
            key = lambda c: (names.get(rows[c][col], "") or "", c)  # noqa: E731
        else:
            key = lambda c: (rows[c][col] if rows[c][col] is not None else "", c)  # noqa: E731
        return sorted((c for c in ids if c in rows), key=key, reverse=True)

    def contacts_listing(self, f, sort: str, way: str, limit: int, offset: int,
                         people: bool = False) -> dict:
        """The Contacts page (or, with ``people``, the people to search):
        {total, more, rows, sources, kinds, financials}."""
        if people:
            total, more, rows = self.people_page(f, sort, way, limit, offset)
        else:
            total, more, ids = self.contacts_page(f, sort, way, limit, offset)
            rows = self.contact_rows(ids)
        return {"total": total, "more": more, "rows": rows,
                "sources": self.contact_sources(people), "kinds": self.contact_kinds(),
                "financials": self.reads_figures()}

    def contact_rows(self, ids) -> list[dict]:
        """The page's contacts with what contacts.summaries adds."""
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(ids))}
        found = self.summaries(ids)
        out = []
        for i in ids:
            r = rows.get(i)
            if r is None:
                continue
            out.append({"id": i, "kind": r[1], "value": r[2], "display": r[3],
                        "line_type": LINE_NAMES.get(r[4], "") if r[11] == PHONE else "",
                        "status": STATUS_NAMES.get(r[5], "") if r[5] is not None else "",
                        "do_not_contact": bool(r[6]), "first_seen": _db_time(r[7]),
                        "last_seen": _db_time(r[8]), **found.get(i, {})})
        return out

    def summaries(self, ids) -> dict[int, dict]:
        """contacts.summaries on the overlay: live firms and people each
        contact reaches, its best trust, its sources and the first company
        (its surest, oldest link)."""
        out = {i: {"companies": 0, "people": 0, "trust": "unconfirmed", "sources": [],
                   "company": None} for i in ids}
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(ids))}
        for cid, r in rows.items():
            out[cid].update(companies=r[9] or 0,
                            trust=TRUST_NAMES.get(r[10] if r[10] is not None else 2,
                                                  "unconfirmed"))
        for contact, people, sources in self._by_ids("overlay.read.contact_summary", list(ids)):
            out[contact].update(people=people or 0,
                                sources=sorted(set((sources or "").split(","))) if sources
                                else [])
        first = {}
        for cid in ids:
            row = self.x("overlay.read.contact_first", (cid,)).fetchone()
            if row:
                first[cid] = row
        lives = self.live_of([f for f, _ in first.values()])
        cards = {r["id"]: r for r in self.company_rows(list(dict.fromkeys(lives.values())))}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 [p for _, p in first.values() if p])}
        for cid, (firm, person) in first.items():
            card = cards.get(str(lives.get(firm, firm)), {})
            out[cid]["company"] = {"id": card.get("id", str(firm)), "name": card.get("name", ""),
                                   "location": card.get("location", ""),
                                   "person": persons[person][2] if person in persons else ""}
        return out

    def reach(self, contact_id: int, limit: int = 500) -> dict:
        """contacts.reach on the overlay."""
        links = list(self.x("overlay.read.links_of_contacts", (json.dumps([int(contact_id)]),)))
        lives = self.live_of([l[1] for l in links])
        cards = {r["id"]: r for r in self.company_rows(list(dict.fromkeys(lives.values())))}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 list({l[3] for l in links if l[3]}))}
        evidence = {}
        for firm, contact, src, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[l[1], l[0]] for l in links], 500):
            evidence.setdefault((firm, person), []).append((src, trust, url, first))
        out = []
        for contact, firm, trust, person, location, first in links:
            card = cards.get(str(lives.get(firm)), {})
            for src, etrust, url, efirst in evidence.get((firm, person), [(None, trust, "", first)]):
                out.append({"company_id": card.get("id", str(firm)),
                            "company": card.get("name", ""), "location": card.get("location", ""),
                            "cui": card.get("cui", ""), "source": src or "",
                            "trust": TRUST_NAMES.get(etrust, ""), "evidence_url": url or "",
                            "first_seen": _db_time(efirst),
                            "person": persons[person][2] if person in persons else "",
                            "role": (persons[person][4] or "") if person in persons else "",
                            "place": "", "address": ""})
        out.sort(key=lambda r: r["company"])
        return {"links": out[:limit]}

    def contact_sources(self, people: bool = False) -> list[str]:
        name = "overlay.read.person_source_names" if people else "overlay.read.evidence_source_names"
        return [r[0] for r in self.x(name) if r[0]]

    def contact_kinds(self) -> list[dict]:
        return [{"kind": k, "label": label, "channel": channel}
                for k, label, channel in self.x("overlay.read.contact_kinds")]

    def contact_export(self, f, sort: str, way: str, limit: int) -> list[dict]:
        """Every contact the filters keep (do-not-contact ones left out),
        with the companies and people each reaches: the contacts export."""
        _, _, ids = self.contacts_page(f, sort, way, limit, 0, cap=limit)
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", ids)}
        found = {}
        for contact, people, sources in self._by_ids("overlay.read.contact_summary", ids):
            found[contact] = sorted(set((sources or "").split(","))) if sources else []
        keep = [i for i in ids if i in rows and not rows[i][6]]
        links = list(self._by_ids("overlay.read.links_of_contacts", keep))
        lives = self.live_of([l[1] for l in links])
        firms = list(dict.fromkeys(lives.values()))
        names = self.sort_keys(firms, "name")
        domains = {i: r["domain"] or "" for i, r in self.firm_rows(firms).items()}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 list({l[3] for l in links if l[3]}))}
        per: dict[int, list] = {}
        for contact, firm, trust, person, location, first in links:
            per.setdefault(contact, []).append((first or 0, firm, person))
        out = []
        for i in keep:
            r = rows[i]
            company_names, people, doms = [], [], []
            for _, firm, person in sorted(per.get(i, [])):
                live = lives.get(firm)
                if live is None:
                    continue
                name = names.get(live, "")
                if name and name not in company_names:
                    company_names.append(name)
                doms.append(domains.get(live, ""))
                pname = persons[person][2] if person in persons else ""
                if pname and pname not in people:
                    people.append(pname)
            out.append({"id": i, "kind": r[1], "value": r[2], "display": r[3],
                        "line_type": LINE_NAMES.get(r[4], "") if r[11] == PHONE else "",
                        "status": STATUS_NAMES.get(r[5], "") if r[5] is not None else "",
                        "trust": TRUST_NAMES.get(r[10] if r[10] is not None else 2,
                                                 "unconfirmed"),
                        "companies": r[9] or 0, "sources": found.get(i, []),
                        "first_seen": _db_time(r[7]), "last_seen": _db_time(r[8]),
                        "company_names": company_names, "people_names": people,
                        "domains": doms})
        return out

    # ── People to search ──

    def people_page(self, f, sort: str, way: str, limit: int, offset: int,
                    cap: int | None = None) -> tuple[int, bool, list[dict]]:
        """The people still to search: named at a live firm (a register's
        administrators and the people Harvey found, each name once per firm
        and its merged records) and reached by no contact of their own yet,
        newest firm first. The total stops at ``cap``: with no condition,
        when the register's count says the firms naming someone are more
        than that, only the page is read."""
        cap = contacts.COUNT_CAP if cap is None else cap
        s = self.firm_set(f)
        p = self.predicates(f)
        unfiltered = s is None and not self.narrowed(p) and not f.source and not (f.q or "").strip()
        firms = None
        if s is None and not self.narrowed(p):
            walk = self._newest_firms()
        else:
            firms = self.matching(f)
            walk = iter(sorted(firms, reverse=True))
        linked = {r[0] for r in self.x("overlay.read.linked_persons")}
        plain = sort in ("", "first_seen")
        many = False
        if unfiltered and self.directory is not None:
            named = self.register_count("CONTACT_FOUND", self.directory.module, values=True)
            if named is not None:
                many = named[1] > cap + len(linked)
            elif hasattr(self.directory, "people_held_upto"):
                # Not counted for this load yet: counted up to the cap (and
                # the few a linked person or a merge may take away), which
                # stops early when there are more.
                own = self.schemes.named(self.directory.keyed_by)
                if own is not None:
                    bound = cap + 1 + len(linked) + 10 * len(self._merged()) + 100
                    many = self.directory.people_held_upto(self.con, own.id, bound) >= bound
        words = [contacts.fold(w) for w in (f.q or "")[:200].split()[:8]]
        counted = None
        if not many and plain and not words and not f.source and firms is not None:
            # Counted, not walked: the register's people of the whole set,
            # with the few firms Harvey found people at worked out one by one
            # (kept until a page may have changed, as the Contacts list is).
            from dataclasses import astuple

            key = ("people_count", repr(astuple(f)), self._main_file())
            mark = (self.marks(), len(linked))
            counted = SETS.get(key, mark)
            if counted is None:
                counted = self._count_people(firms, linked)
                SETS.put(key, mark, counted)
        need = offset + limit if ((many or counted is not None) and plain) \
            else max(offset + limit, cap + 1)
        # The people Harvey found (a few thousand) are read whole; the
        # register's administrators a batch of firms at a time, newest
        # first, until the page (or the count) is filled. In time order, as
        # schema 24 listed people: an administrator dates from its firm's
        # pull, a person from when it was found.
        merged = self._merged()
        own_firms = {merged.get(r[0], r[0]) for r in self.x("overlay.read.person_firms")}
        if firms is not None:
            own_firms &= set(firms)
        found = self._people_of(sorted(own_firms, reverse=True), linked, f.source, words,
                                part="persons")
        admins = 0
        # (Small batches first: a page usually fills from the newest few
        # hundred firms.)
        for batch in self._batches(walk, 5000, first=min(5000, max(500, 4 * need))):
            more_ = self._people_of(batch, linked, f.source, words, part="admins")
            found += more_
            admins += len(more_)
            if admins >= need:
                break
        found.sort(key=lambda r: r["_order"], reverse=True)
        if many:
            total, more = cap, True
        elif counted is not None:
            total, more = min(counted, cap), counted > cap
        else:
            total, more = min(len(found), cap), len(found) > cap
        if not plain:
            key = {"name": "name_norm", "role": "role", "company": "company_name",
                   "searched_at": "searched_at"}.get(sort, "first_seen")
            if key == "company_name":
                self._describe_people(found)
            found.sort(key=lambda r: (r.get(key) or "", r["_order"]),
                       reverse=(way or "desc") != "asc")
        elif (way or "desc") == "asc":
            found.reverse()
        page = found[offset:offset + limit]
        self._describe_people(page)
        for r in page:
            r.pop("_order", None)
            r.pop("name_norm", None)
        return total, more, page

    def _count_people(self, firms, linked: set) -> int:
        """How many people to search these firms have: the register's count,
        corrected exactly at the firms Harvey found people at."""
        if self.directory is None or not hasattr(self.directory, "count_people"):
            return sum(1 for _ in self._people_of(list(firms), linked, "", []))
        firms = set(firms)
        merged = self._merged()
        own = {merged.get(r[0], r[0]) for r in self.x("overlay.read.person_firms")} & firms
        rest = [f for f in firms if f not in own]
        by_scheme: dict[str, list] = {}
        for f, (scheme, key) in self.key_of(rest).items():
            by_scheme.setdefault(scheme, []).append(key)
        n = sum(self.directory.count_people(self.con, keys, scheme)
                for scheme, keys in by_scheme.items())
        return n + len(self._people_of(sorted(own), linked, "", []))

    def _describe_people(self, rows: list[dict]) -> None:
        """Each People row's company name, place and tax code."""
        firms = list(dict.fromkeys(int(r["company_id"]) for r in rows if "company_name" not in r))
        if not firms:
            return
        cards = {r["id"]: r for r in self.company_rows(firms)}
        for r in rows:
            card = cards.get(r["company_id"], {})
            r.setdefault("company_name", card.get("name", ""))
            r.setdefault("location", card.get("location", ""))
            r.setdefault("cui", card.get("cui", ""))

    def _newest_firms(self):
        at = 0
        while True:
            ids = [r[0] for r in self.x("overlay.firms.new", (5000, at))]
            if not ids:
                return
            yield from ids
            at += len(ids)

    @staticmethod
    def _batches(it, size, first=None):
        """``it`` in lists of ``size``; with ``first``, the first list that
        long and each next one four times longer, up to ``size``."""
        batch, now = [], first or size
        for x in it:
            batch.append(x)
            if len(batch) >= now:
                yield batch
                batch, now = [], min(size, now * 4)
        if batch:
            yield batch

    def _people_of(self, firms: list[int], linked: set, source: str, words,
                   part: str = "all") -> list[dict]:
        """The people to search at these firms (in their order), each a
        People row: Harvey's own person row when it has one, else the
        register's administrator (``part``: "persons" or "admins" only)."""
        rows = self.firm_rows(firms)
        groups = self.members(firms)
        live_of = {m: f for f, g in groups.items() for m in g}
        persons: dict[int, list] = {}
        for firm, pid, name, norm, role, src, first in self._by_ids("overlay.read.people_of",
                                                                   list(live_of)):
            persons.setdefault(live_of[firm], []).append((pid, name, norm, role, src, first))
        named: dict[int, list] = {}
        directory = self.directory.module if self.directory else ""
        if self.directory is not None and hasattr(self.directory, "people") and part != "persons":
            by_scheme: dict[str, dict] = {}
            for f, (scheme, key) in self.key_of(firms).items():
                by_scheme.setdefault(scheme, {})[key] = f
            for scheme, of in by_scheme.items():
                for key, people in self.directory.people(self.con, list(of), scheme).items():
                    named[of[key]] = people
        need_names = bool(words)
        names = self.sort_keys(firms, "name") if need_names else {}
        out = []
        for f in firms:
            if f not in rows:
                continue
            created = rows[f]["created"]
            seen: set[str] = set()
            mine = persons.get(f, [])
            reached = {norm for pid, name, norm, *_ in mine if pid in linked}
            entries = []
            for pid, name, norm, role, src, first in mine:
                seen.add(norm)
                if pid in linked or part == "admins":
                    continue
                entries.append({"id": pid, "name": name, "role": role, "source": src or "",
                                "first_seen": _db_time(first), "name_norm": norm,
                                "_t": first or created})
            for name, role in ([] if part == "persons" else named.get(f, [])):
                norm = contacts.name_norm(name)
                if not name or norm in seen or norm in reached:
                    continue
                seen.add(norm)
                entries.append({"id": None, "name": name, "role": role, "source": directory,
                                "first_seen": _db_time(created), "name_norm": norm,
                                "_t": created})
            company = contacts.name_norm(names.get(f, "")) if need_names else ""
            for e in entries:
                if source and e["source"] != source:
                    continue
                if words and not all(w in e["name_norm"] or w in company
                                     or w in contacts.fold(e["role"] or "") for w in words):
                    continue
                t = e.pop("_t")
                e.update(searched_at=None, company_id=str(f), domain=rows[f]["domain"] or "",
                         _order=(t, f))
                out.append(e)
        return out

    # ── Signals ──

    def signal_vocabulary(self) -> list[dict]:
        """get_signal_codes on the overlay: the vocabulary with the module
        that declares each signal and whether a module that is on provides
        it (the manifests say). The stored ``status`` column, a confirmation
        older releases asked for, isn't read: a signal of a module that is
        on is collected."""
        from harvey.signals import available_codes

        declared = {sig["code"]: (m.id, sig) for m in self.manifests for sig in m.signals}
        stored = {k: v for k, v in self.con.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'modules.%'").fetchall()}
        available = available_codes(stored, self.manifests)
        cur = self.x("overlay.read.vocabulary")
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            module, sig = declared.get(r["code"], ("", {}))
            r.pop("id", None)
            r.pop("status", None)
            r.update(module=module, available=int(r["code"] in available),
                     subject=sig.get("subject", "company"), absence=sig.get("absence", "unknown"),
                     collector=sig.get("collector", ""))
            out.append(r)
        return out

    def sources_of_signal(self) -> dict[str, list[tuple]]:
        """{code: [(module, how, precedence, absent)]}: who provides each
        signal (stored by a module, or read from its register)."""
        out: dict[str, list] = {}
        for code, module, how, prec, absent in self.x("overlay.read.signal_sources"):
            out.setdefault(code, []).append((module, how, prec, absent))
        return out

    def signal_counts(self) -> list[dict]:
        """StateManager.signal_counts on the overlay: firms carrying each
        signal now (a stored signal's kept tally; a register signal's count
        for the register's load mark, when counted)."""
        vocab = {v["code"]: v for v in self.signal_vocabulary()}
        states = self.signal_states(list(vocab))
        out = []
        for code, st in states.items():
            if not st["true"]:
                continue
            out.append({"signal_code": code, "label": vocab[code]["label"] or code,
                        "companies": st["true"],
                        "observations": st["true"]})
        out.sort(key=lambda r: -r["companies"])
        return out

    def signal_states(self, codes) -> dict[str, dict]:
        """Per signal, how many live firms read true, false and unknown: a
        stored signal from its tally, a register signal from the register's
        count for its current load mark (``counting``: not counted for this
        mark yet; then its register part reads unknown)."""
        total = self.total()
        sources = self.sources_of_signal()
        absent_false = self._absence()
        out = {}
        for code in codes:
            true = false = 0
            counting = stored = False
            sid = self.signal_id(code)
            for module, how, prec, absent in sources.get(code, []):
                if module not in self.enabled:
                    continue
                if how == 0 and sid is not None and not stored:
                    # (One tally for every module storing the signal.)
                    stored = True
                    row = self.x("overlay.read.tally_signal", (sid,)).fetchone()
                    if row:
                        t, checked = row
                        true += t
                        false += 0 if code in absent_false else checked - t
                elif how == 1:
                    got = self.register_count(code, module)
                    if got is None:
                        counting = True
                    else:
                        true, false = max(true, got[0]), max(false, got[1])
            if code in absent_false:
                false = total - true
            false = min(false, total - true)
            out[code] = {"true": true, "false": false, "unknown": total - true - false,
                         **({"counting": True} if counting else {})}
        return out

    def _absence(self) -> set[str]:
        return {sig["code"] for m in self.manifests for sig in m.signals
                if sig.get("absence") == "false"}

    def register_count(self, code: str, module: str, values: bool = False
                       ) -> tuple[int, int] | None:
        """(true, false) over the live firms a register reads ``code`` for,
        counted for its current load mark; None when not counted for it.
        ``values``: (true, how many values those firms hold) instead."""
        a = self.regs.get(module)
        sid, mid = self.signal_id(code), self.module_id(module)
        if a is None or sid is None or mid is None:
            return (0, 0) if a is None else None
        row = self.x("overlay.read.register_tally_of", (sid, mid)).fetchone()
        if row is None or row[4] is None or row[0] != (a.marks(self.con) or ""):
            return None
        if values:
            return (row[2] or 0, row[5]) if row[5] is not None else None
        return row[2] or 0, row[3] or 0

    def register_tally_stale(self) -> bool:
        """Whether a loaded register's signals aren't counted for its
        current load mark (just after the upgrade, a reload or a merge):
        the worker queues the count then."""
        for a in self.registers:
            mid = self.module_id(a.module)
            if mid is None or not a.by_signal:
                continue
            mark = a.marks(self.con) or ""
            if not self.one("SELECT 1 FROM register_tally WHERE module = ? AND mark = ? "
                            "AND at IS NOT NULL LIMIT 1", (mid, mark)):
                return True
            # (The sets a cohort reads, kept by the same job: none for this
            # load, counting other firms than there are, or so many records
            # given an identifier since that patching them costs more.)
            row = self.x("overlay.read.kept_of_module", (mid,)).fetchone()
            if row is None or row[0] != mark or self.one("overlay.read.kept_records",
                                                         (row[1],)) != row[2]:
                return True
            if self.one("overlay.read.idents_since_count", (row[3], PATCH_MAX + 1)) > PATCH_MAX:
                return True
        return False

    def count_registers(self, codes=None, sets: dict | None = None) -> list[tuple]:
        """Every (signal, module) a loaded register reads, counted true /
        false over the firms in Harvey (one walk of the scheme per read):
        the rows register_tally keeps, per load mark. A job, never a page.
        ``sets``: filled with {(signal id, module id, part): (mark, bitmap)}
        (bitmaps.py): the records with a value, those whose value reads yes
        as a cohort reads it (any value of a text signal), and for a
        register that keeps each year's figures, per year the records whose
        statement that year has the signal's figure."""
        out = []
        now = int(time.time())
        for a in self.registers:
            mid = self.module_id(a.module)
            mark = a.marks(self.con) or ""
            for code in dict.fromkeys(c for c, _ in a.by_signal):
                if codes is not None and code not in codes:
                    continue
                sid = self.signal_id(code)
                if sid is None or mid is None:
                    continue
                per: dict[int, bool] = {}
                text = (self._value_types().get(code) or "text") == "text"
                reads_yes: set[int] = set()
                values = 0
                for (c, scheme), read in a.by_signal.items():
                    if c != code:
                        continue
                    s = self.schemes.named(scheme or a.keyed_by)
                    if s is None:
                        continue
                    # (A record's rows come together: its values counted once each.)
                    record, seen = None, set()
                    for firm, value, date in a.held_values(self.con, code, s.id, scheme):
                        yes = self._truth(read, value)
                        per[firm] = per.get(firm, False) or yes
                        if yes or text:
                            reads_yes.add(firm)
                        if firm != record:
                            record, seen = firm, set()
                        if yes and value not in seen:
                            seen.add(value)
                            values += 1
                if sets is not None:
                    sets[(sid, mid, bitmaps.VALUED)] = (mark, bitmaps.bits_of(per))
                    sets[(sid, mid, bitmaps.YES)] = (mark, bitmaps.bits_of(reads_yes))
                lives: dict[int, bool] = {}
                for firm, live in self.live_of(per).items():
                    lives[live] = lives.get(live, False) or per[firm]
                n_true = sum(1 for v in lives.values() if v)
                out.append((sid, mid, mark, 1, n_true, len(lives) - n_true, now, values))
            s = self.schemes.named(a.keyed_by)
            if sets is None or mid is None or s is None or not hasattr(a, "year_valued"):
                continue
            for year in a.years(self.con):
                for code, records in a.year_valued(self.con, year, s.id).items():
                    sid = self.signal_id(code)
                    if sid is not None and (codes is None or code in codes):
                        sets[(sid, mid, year)] = (mark, bitmaps.bits_of(records))
        return out

    @staticmethod
    def _truth(read, value) -> bool:
        if value is None or value == "":
            return False
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip() not in ("", "0")

    # Cohorts

    VERDICTS = ("match", "possible", "no_match")

    def _readers_of(self, code: str) -> list:
        """The register adapters that read ``code``, of modules that are on."""
        return [a for a in self.registers if a.module in self.enabled
                and any(c == code for c, _ in a.by_signal)]

    def _reader_of(self, code: str):
        found = self._readers_of(code)
        return found[0] if found else None

    def _schemes_read(self, a, code: str) -> list[str]:
        return [scheme or a.keyed_by for c, scheme in a.by_signal if c == code]

    def _held(self, code: str, op: str, value) -> FirmSet:
        """Live firms whose register value of ``code`` satisfies ``op value``
        (an index search in the register), kept per load mark."""
        out = 0
        for a in self._readers_of(code):
            for scheme in self._schemes_read(a, code):
                s = self.schemes.named(scheme)
                own = "" if scheme == a.keyed_by else scheme
                sql = a.held_sql(code, op, own)
                if s is None or sql is None:
                    continue
                key = ("held", a.module, code, scheme, op, value, *self.held_mark())
                hit = SETS.get(key, ())
                if hit is None:
                    hit = self._live_bits(r[0] for r in self.con.execute(sql, (value, s.id)))
                    SETS.put(key, (), hit, LONG_SECONDS)
                out |= hit
        return FirmSet(bits=out)

    def _register_sets(self, code: str, low=None, high=None) -> tuple[FirmSet, FirmSet]:
        """(true, false) for "has ``code`` [in range]" from the registers
        that read it: newest source first (by its precedence), a firm takes
        the first source that has a value for it."""
        key = ("register", code, low, high, tuple(sorted(self.enabled)),
               *self.held_mark())
        hit = SETS.get(key, ())
        if hit is not None:
            return hit
        sources = {m: (prec, absent) for m, how, prec, absent in
                   self.sources_of_signal().get(code, []) if how == 1}
        readers = sorted(self._readers_of(code), key=lambda a: sources.get(a.module, (9, 0))[0])
        true, false, checked = FirmSet(), FirmSet(), FirmSet()
        for a in readers:
            t, f, c = self._source_sets(a, code, low, high, sources.get(a.module, (0, 0))[1])
            true |= t & ~checked
            false |= f & ~checked
            checked |= c
        SETS.put(key, (), (true, false))
        return true, false

    def _source_sets(self, a, code, low, high, absent_false) -> tuple[FirmSet, FirmSet, FirmSet]:
        """(true, false, checked) from one register. A value it keeps an
        index of is ranged through that index; the firms it has no value for
        are found once per load mark; the rest by complement, so a flag most
        firms hold is never a set of a million ids. A register whose missing
        row reads no (absent = false) needs its yes set only."""
        ranged = low is not None or high is not None
        own = a.by_signal.get((code, ""))
        indexed = own is not None and own.indexed
        found = None if (ranged and not indexed) else self._kept(a, code)
        if found is not None:
            # The counts job's sets for this load (bitmaps.py): no walk.
            valued, yes = found
            if ranged:
                true = FirmSet(neg=True)
                if low is not None:
                    true &= self._held(code, ">=", low)
                if high is not None:
                    true &= self._held(code, "<=", high)
                if true.neg:
                    true &= valued
            else:
                true = yes
            if absent_false:
                return true, ~true, FirmSet(neg=True)
            return true, valued & ~true, valued
        text = (self._value_types().get(code) or "text") == "text"
        yes_sql = a.held_yes_sql(code)
        unvalued = None if (absent_false and (indexed or yes_sql)) else self._unvalued(a, code)
        if ranged:
            if indexed:
                true = FirmSet(neg=True)
                if low is not None:
                    true &= self._held(code, ">=", low)
                if high is not None:
                    true &= self._held(code, "<=", high)
                if true.neg:
                    true &= ~(unvalued or FirmSet())
            else:
                values = self._held_values(a, code)
                true = FirmSet(f for f, v in values.items()
                               if self._in_range(v, low, high, True, a, code))
        elif yes_sql is not None:
            s = self.schemes.named(a.keyed_by)
            key = ("yes", a.module, code, *self.held_mark())
            hit = SETS.get(key, ())
            if hit is None:
                hit = self._lives(r[0] for r in self.con.execute(yes_sql, (s.id,)))
                SETS.put(key, (), hit, LONG_SECONDS)
            true = FirmSet(hit)
        elif indexed:
            if absent_false:
                true = self._held(code, ">", 0) | self._held(code, "<", 0)
            else:
                true = ~(unvalued | self._held(code, "=", 0))
        elif text:
            # Any value is a yes (a name, a form, an address).
            true = ~unvalued
        else:
            true = ~(unvalued | self._held_false(a, code))
        if absent_false:
            return true, ~true, FirmSet(neg=True)
        return true, ~(unvalued | true), ~unvalued

    def _kept(self, a, code: str) -> tuple[FirmSet, FirmSet] | None:
        """(live firms register ``a`` has a value of ``code`` for, those of
        them whose value reads yes) as bitmaps: the sets the counts job
        kept for the register's current load, patched for the records
        given an identifier since and for merges. None when none are kept
        for this load (bitmaps.py)."""
        sid, mid = self.signal_id(code), self.module_id(a.module)
        if sid is None or mid is None:
            return None
        key = ("kept", a.module, code, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is not None:
            return hit or None
        hit = self._patched(a, code, sid, mid)
        SETS.put(key, (), hit or (), LONG_SECONDS)
        return hit

    def _kept_bits(self, a, sid: int, mid: int, parts) -> tuple[list[int], list[int]] | None:
        """The bitmaps register_set keeps for ``parts`` of a signal, when
        they were kept by one walk for the register's current load and
        count the firms there are (a removal clears its bit and takes one
        off), and the records given an identifier since (to read again);
        else None."""
        rows = {r[0]: r[1:] for r in self.x("overlay.read.kept", (sid, mid))}
        if any(p not in rows for p in parts):
            return None
        heads = {tuple(rows[p][:4]) for p in parts}
        if len(heads) != 1:
            return None
        mark, top, records, at = heads.pop()
        if mark != (a.marks(self.con) or ""):
            return None
        if self.one("overlay.read.kept_records", (top,)) != records:
            return None
        changed = [r[0] for r in self.x("overlay.read.idents_since", (at,))]
        return [bitmaps.unpack(rows[p][4]) for p in parts], changed

    def _patched(self, a, code: str, sid: int, mid: int) -> tuple[FirmSet, FirmSet] | None:
        """_kept's sets read from register_set and patched (not kept)."""
        got = self._kept_bits(a, sid, mid, (bitmaps.VALUED, bitmaps.YES))
        if got is None:
            return None
        (valued, yes), changed = got
        if changed:
            gone = ~bitmaps.bits_of(changed)
            valued, yes = valued & gone, yes & gone
            have, says_yes = self._read_records(a, code, changed)
            valued |= bitmaps.bits_of(have)
            yes |= bitmaps.bits_of(says_yes)
        return FirmSet(bits=self._onto_survivors(valued)), FirmSet(bits=self._onto_survivors(yes))

    def _read_records(self, a, code: str, records) -> tuple[set[int], set[int]]:
        """(the records register ``a`` has a value of ``code`` for, those
        whose value reads yes), read from the register by their keys."""
        text = (self._value_types().get(code) or "text") == "text"
        idents = self.idents(records)
        have, says_yes = set(), set()
        for (c, scheme), read in a.by_signal.items():
            if c != code:
                continue
            name = scheme or a.keyed_by
            holder = {k: f for f, ks in idents.items() for s, k in ks if s == name}
            if not holder:
                continue
            for k, values in a.read(self.con, code, list(holder), scheme=scheme).items():
                have.add(holder[k])
                if text or any(self._truth(read, v) for v, _ in values):
                    says_yes.add(holder[k])
        return have, says_yes

    def _onto_survivors(self, bits: int) -> int:
        """A bitmap over records as one over live firms: a merged record's
        bit moves to the firm it is merged into."""
        merged = self._merged()
        if not merged or not bits:
            return bits
        look = bitmaps.Lookup(bits)
        moved = [m for m in merged if m in look]
        if not moved:
            return bits
        return (bits & ~bitmaps.bits_of(moved)) | bitmaps.bits_of({merged[m] for m in moved})

    def _live_bits(self, records) -> int:
        """The bitmap of the live firms of records (a register statement's
        firm_ident rows)."""
        return self._onto_survivors(bitmaps.bits_of(array("q", records)))

    def _value_types(self) -> dict[str, str]:
        if not hasattr(self, "_vtypes"):
            self._vtypes = {s["code"]: s.get("value_type") or "text"
                            for m in self.manifests for s in m.signals}
            self._vtypes.update({r[0]: r[1] for r in self.con.execute(
                "SELECT code, value_type FROM signal") if r[1]})
        return self._vtypes

    def _unvalued(self, a, code: str) -> FirmSet:
        """Live firms the register has no value of ``code`` for (kept per
        load mark): those whose key of its own scheme it has no value for,
        and those with no such key, unless a read by another of their keys
        (a firm with no tax code, by its registration number) has one."""
        key = ("unvalued", a.module, code, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is None:
            own = self.schemes.named(a.keyed_by)
            sql = a.unvalued_sql(code)
            hit = set()
            if own is not None and sql is not None:
                hit = self._lives(r[0] for r in self.con.execute(sql, (own.id,)))
            keyless = self.unkeyed(a.keyed_by)
            others = [s for c, s in a.by_signal if c == code and s]
            if others and keyless:
                valued = set()
                idents = self.group_idents(keyless)
                for scheme in others:
                    firm_of = {k: f for f, ks in idents.items() for s, k in ks if s == scheme}
                    if firm_of:
                        valued |= {firm_of[k] for k in a.read(self.con, code, list(firm_of),
                                                              scheme=scheme)}
                keyless = keyless - valued
            hit |= keyless
            SETS.put(key, (), hit, LONG_SECONDS)
        return FirmSet(hit)

    def unkeyed(self, scheme: str) -> set[int]:
        """Live firms (their records merged in included) with no key of a
        scheme: a register's firms are nearly all keyed, so these are few."""
        s = self.schemes.named(scheme)
        if s is None:
            return set(self.all_live())
        # (Which firms hold which keys only changes with the identity mark.)
        key = ("unkeyed", scheme, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is None:
            hit = {r[0] for r in self.x("overlay.read.unkeyed", (s.id,))}
            # (A survivor whose merged record holds one is keyed: a few.)
            merged = self._merged()
            survivors = hit & set(merged.values())
            if survivors:
                keyed = {merged[m] for m, sc, _ in self._by_ids(
                    "overlay.firms.idents", [m for m in merged if merged[m] in survivors])
                         if sc == s.id}
                hit -= keyed
            SETS.put(key, (), hit, LONG_SECONDS)
        return hit

    def _held_false(self, a, code: str) -> FirmSet:
        """Live firms whose register value of ``code`` reads no (kept per
        load mark)."""
        key = ("false", a.module, code, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is None:
            hit = set()
            for scheme in self._schemes_read(a, code):
                s = self.schemes.named(scheme)
                sql = a.held_false_sql(code, "" if scheme == a.keyed_by else scheme)
                if s is not None and sql is not None:
                    hit |= self._lives(r[0] for r in self.con.execute(sql, (s.id,)))
            SETS.put(key, (), hit, LONG_SECONDS)
        return FirmSet(hit)

    def _merged(self) -> dict[int, int]:
        """{record merged away: its live firm} (a few)."""
        if not hasattr(self, "_merged_map"):
            self._merged_map = dict(self.con.execute(
                "SELECT id, live FROM firm WHERE live != 0").fetchall())
        return self._merged_map

    def _lives(self, records) -> set[int]:
        """The live firms of records that exist (a register statement's
        firm_ident rows): a merged record stands for its survivor."""
        merged = self._merged()
        out = set(records)
        gone = out & merged.keys()
        if gone:
            out -= gone
            out |= {merged[r] for r in gone}
        return out

    def _in_range(self, value, low, high, ranged, a, code) -> bool:
        if not ranged:
            read = a.by_signal.get((code, ""))
            return self._truth(read, value)
        if not isinstance(value, (int, float)):
            return False
        return (low is None or value >= low) and (high is None or value <= high)

    def _held_values(self, a, code: str) -> dict[int, object]:
        """{live firm: its value} from a register read with no index (a walk
        of the scheme's keys: kept per load mark)."""
        key = ("values", a.module, code, *self.held_mark())
        hit = SETS.get(key, ())
        if hit is not None:
            return hit
        merged = self._merged()
        out: dict[int, object] = {}
        for (c, scheme), read in a.by_signal.items():
            if c != code:
                continue
            s = self.schemes.named(scheme or a.keyed_by)
            if s is None:
                continue
            for firm, value, date in a.held_values(self.con, code, s.id, scheme):
                live = merged.get(firm, firm)
                if live not in out or self._truth(read, value):
                    out[live] = value
        SETS.put(key, (), out, LONG_SECONDS)
        return out

    def _stored_sets(self, code: str, low=None, high=None, key=None, floor=None
                     ) -> tuple[FirmSet, FirmSet, FirmSet]:
        """(true, false-or-checked parts) for a stored signal: (found at the
        confidence floor, found at all, checked) over live firms."""
        sid = self.signal_id(code)
        if sid is None:
            return FirmSet(), FirmSet(), FirmSet()
        ranged = low is not None or high is not None
        year_codes = {s["code"] for m in self.manifests for s in m.signals
                      if s.get("keyed_by") == "year"}
        found, any_found, checked = set(), set(), set()
        for firm, k, num, text, conf, positive, at in self.x("overlay.read.readings", (sid,)):
            if key is not None and (text if text is not None else k) != str(key) and k != str(key):
                continue
            checked.add(firm)
            if ranged:
                ok = num is not None and (num != 0 or code in year_codes) and \
                    (low is None or num >= low) and (high is None or num <= high)
            elif key is not None:
                ok = num is None or num != 0
            else:
                ok = bool(positive)
            if ok:
                any_found.add(firm)
                if floor is None or (conf or 0) >= floor:
                    found.add(firm)
        lives = self.live_of(checked)
        return (FirmSet(lives[f] for f in found), FirmSet(lives[f] for f in any_found),
                FirmSet(lives.values()))

    def _leaf(self, code: str, low=None, high=None, key=None, floor: float = 0.0
              ) -> tuple[FirmSet, FirmSet]:
        """(true, false) for "has a positive reading of code [in range]", over
        every source of the signal that is on: a stored reading and a
        register value alike."""
        sources = self.sources_of_signal().get(code, [])
        stored = any(how == 0 and m in self.enabled for m, how, *_ in sources) or not sources
        true, false = FirmSet(), FirmSet()
        if stored:
            found, any_found, checked = self._stored_sets(code, low, high, key,
                                                          floor if floor else None)
            if code in self._absence():
                t, f = found, ~any_found
            else:
                t, f = found, FirmSet(checked.ids - any_found.ids)
            true, false = t, f
        if key is None and self._readers_of(code):
            rt, rf = self._register_sets(code, low, high)
            # A firm a register answers takes the register's answer where
            # no stored reading says yes.
            true = true | rt
            false = (false | rf) & ~true
        elif key is not None:
            rt, rf = self._year_sets(code, low, high, key)
            true = true | rt
            false = (false | rf) & ~true
        return true, false

    def _year_sets(self, code: str, low, high, year) -> tuple[FirmSet, FirmSet]:
        """(true, false) for ``code`` [in range] in a named year, from the
        registers that keep each year's figures: a firm with no figure that
        year is unknown. Kept per load mark."""
        if not str(year).isdigit():
            return FirmSet(), FirmSet()
        key = ("year", code, low, high, str(year), tuple(sorted(self.enabled)),
               *self.held_mark())
        hit = SETS.get(key, ())
        if hit is not None:
            return hit
        true, false = FirmSet(), FirmSet()
        for a in self._readers_of(code):
            s = self.schemes.named(a.keyed_by)
            if s is None or code not in getattr(a, "year_codes", ()):
                continue
            try:
                if int(year) not in a.years(self.con):
                    continue
                valued = self._kept_year(a, code, int(year))
                if valued is None:
                    valued = FirmSet(bits=self._live_bits(
                        a.year_valued(self.con, year, s.id)[code]))
                held = FirmSet(bits=self._live_bits(
                    a.year_held(self.con, code, year, low, high, s.id)))
            except sqlite3.Error:
                continue
            true |= held
            false |= valued & ~held
        out = (true, false & ~true)
        SETS.put(key, (), out, LONG_SECONDS)
        return out

    def _kept_year(self, a, code: str, year: int) -> FirmSet | None:
        """Live firms whose ``year`` statement in register ``a`` has
        ``code``'s figure: the counts job's set for this load, patched as
        _patched patches (not kept); None when none is kept for it."""
        sid, mid = self.signal_id(code), self.module_id(a.module)
        got = None if sid is None or mid is None else self._kept_bits(a, sid, mid, (year,))
        if got is None:
            return None
        (valued,), changed = got
        if changed:
            valued &= ~bitmaps.bits_of(changed)
            holder = {k: f for f, ks in self.idents(changed).items() for s, k in ks
                      if s == a.keyed_by}
            if holder:
                valued |= bitmaps.bits_of(
                    [holder[k] for k in a.year_read(self.con, code, year, list(holder))])
        return FirmSet(bits=self._onto_survivors(valued))

    def cohort_sets(self, require, exclude, ranges, min_confidence: float = 0.0
                    ) -> tuple[FirmSet, FirmSet]:
        """(match, no_match) as sets; possible is everything else."""
        trues, falses = [], []
        for code in require:
            t, f = self._leaf(code, floor=min_confidence)
            trues.append(t)
            falses.append(f)
        for code in exclude:
            t, f = self._leaf(code, floor=min_confidence)
            trues.append(f)
            falses.append(t)
        for r in ranges:
            t, f = self._leaf(r["code"], r.get("min"), r.get("max"), r.get("key"),
                              floor=min_confidence)
            trues.append(t)
            falses.append(f)
        no = reduce(lambda a, b: a | b, falses, FirmSet())
        match = reduce(lambda a, b: a & b, trues, FirmSet(neg=True)) & ~no
        return match, no

    def cohort_set(self, require, exclude, ranges, verdicts=("match",)) -> FirmSet:
        match, no = self.cohort_sets(require, exclude, ranges)
        out = FirmSet()
        if "match" in verdicts:
            out |= match
        if "no_match" in verdicts:
            out |= no
        if "possible" in verdicts:
            out |= ~(match | no)
        return out

    def cohort_verdicts(self, require, exclude, ranges=None, verdicts=("match",),
                        offset: int = 0, limit: int | None = None,
                        min_confidence: float = 0.0) -> tuple[dict, list[dict]]:
        """StateManager.cohort_verdicts on the overlay: every live firm's
        verdict counted, and a page of those in ``verdicts``, by name."""
        counts = dict.fromkeys(self.VERDICTS, 0)
        require = list(dict.fromkeys(require or []))
        exclude = list(dict.fromkeys(exclude or []))
        ranges = list(ranges or [])
        if not require and not exclude and not ranges:
            return counts, []
        total = self.total()
        match, no = self.cohort_sets(require, exclude, ranges, min_confidence)
        counts["match"] = match.count(total)
        counts["no_match"] = no.count(total)
        counts["possible"] = total - counts["match"] - counts["no_match"]
        wanted = [v for v in dict.fromkeys(verdicts) if v in self.VERDICTS]
        if not wanted:
            return counts, []
        sets = {"match": match, "no_match": no, "possible": ~(match | no)}
        key = ("cohort", repr((require, exclude, ranges, min_confidence, tuple(wanted))),
               self._main_file())
        mark = self.marks()
        order = ORDERINGS.get(key, mark)
        chosen = reduce(lambda a, b: a | b, (sets[v] for v in wanted))
        if order is None and self.orders_current():
            # By name through the kept positions: a set of a few thousand
            # sorted by them, a bigger one (or one held by what it leaves
            # out) read off the index a page at a time.
            if chosen.neg or chosen.size() > COHORT_SORTED_MAX:
                page = self.walk_order(chosen, "name", int(offset),
                                       None if limit is None else int(limit))
                order = None
            else:
                order = array("q", self.by_order(chosen.ids, "name", asc=True))
                ORDERINGS.put(key, mark, order)
        if order is None and not self.orders_current():
            ids = list(self.resolve(chosen))
            if len(ids) > NAME_ORDER_FROM:
                # (The registry's own order: its names are upper case.)
                order = array("q", reversed(self._name_order(ids)))
            else:
                names = self.sort_keys(ids, "name")
                order = array("q", sorted(ids, key=lambda i: ((names.get(i) or "").lower(), i)))
            ORDERINGS.put(key, mark, order)
        if order is not None:
            page = list(order[offset:] if limit is None else order[offset:offset + int(limit)])
        verdict_of = {}
        for v in wanted:
            s = sets[v]
            for i in page:
                if i in s:
                    verdict_of.setdefault(i, v)
        rows = []
        for r in self.company_rows(page):
            rows.append({"id": r["id"], "name": r["name"], "domain": r["domain"],
                         "website": r["website"], "industry": r["industry"],
                         "location": r["location"], "verdict": verdict_of.get(int(r["id"]))})
        return counts, rows

    # ── Today, status, overview, the finder's backlog ──

    def needs_profile(self, stale_days: int = 90) -> int:
        return self.one("overlay.read.needs_profile", (int(time.time()) - stale_days * 86400,))

    def needing_profile(self, limit: int = 100, stale_days: int = 90, ids=None) -> list[dict]:
        cutoff = int(time.time()) - stale_days * 86400
        if ids is None:
            found = [r[0] for r in self.x("overlay.read.needing_profile", (cutoff, int(limit)))]
        else:
            rows = self.firm_rows(self.live_set(self.fid(i) for i in ids if self.fid(i)))
            found = sorted((i for i, r in rows.items() if r["domain"] and r["live"] == 0
                            and (r["profiled_at"] is None or r["profiled_at"] < cutoff)),
                           key=lambda i: (rows[i]["profiled_at"] is not None,
                                          rows[i]["profiled_at"] or 0))
            if int(limit) >= 0:         # (a negative limit is none, as in SQL)
                found = found[:int(limit)]
        out = []
        for r in self.company_rows(found):
            out.append({"id": r["id"], "name": r["name"], "domain": r["domain"],
                        "location": r["location"], "source_url": r["source_url"]})
        return out

    def status_counts(self, since=None) -> dict:
        """What live-status counts about firms: all, with a domain, read,
        searched by the finder (of the read), read since ``since``, and the
        finder's last search."""
        t = self.tally()
        profiled, checked = self.x("overlay.read.profiled").fetchone()
        mid = self.module_id("contact_finder")
        cap = self.capability_id("contact_finder", "find_contacts")
        last = self.one("overlay.read.last_search", (mid, cap)) if mid and cap else None
        with_site = self.one("overlay.read.with_domain") + self.one("overlay.read.domain_by_member")
        return {"companies": t.get("firms", 0), "with_site": with_site,
                "profiled": profiled or 0, "checked": checked or 0,
                "profiled_since": (self.one("overlay.read.profiled_since", (int(since),))
                                   if since is not None else None),
                "last_contact": _db_time(last)}

    def overview(self, blocked=()) -> dict:
        """The landing page's firm counts: all, with a phone (a phone
        contact, a listing's or a prospect's), with an email (a prospect's
        deliverable address), people and firms with an activity on file
        (the registers' counts, when counted for their load mark)."""
        from harvey.gate import do_not_contact_domain_match, do_not_contact_match

        t = self.tally()
        phones = self.one("overlay.read.with_phone") or 0
        extra = set(self.live_of([r[0] for r in self.x("overlay.read.local_phones")]).values())
        extra |= set(self.live_of([r[0] for r in self.x("overlay.read.prospect_phones")
                                   if r[0] is not None]).values())
        if extra:
            rows = self.firm_rows(extra)
            phones += sum(1 for i in extra if i in rows and not rows[i]["n_phone"])
        email = set()
        for firm, address, domain in self.x("overlay.read.prospect_emails"):
            if firm is None or firm in email:
                continue
            if blocked and (do_not_contact_match(address, blocked)
                            or do_not_contact_domain_match(domain or "", blocked)):
                continue
            email.add(firm)
        email = set(self.live_of(email).values())
        with_both = 0
        if email:
            rows = self.firm_rows(email)
            with_both = sum(1 for i in email if i in rows and (rows[i]["n_phone"] or i in extra))
        # People: Harvey's own, and the people the register names at the
        # firms in Harvey (counted per load mark), the ones Harvey took up
        # from it once.
        people = t.get("people", 0)
        named = self.register_count("CONTACT_FOUND", self.directory.module, values=True) \
            if self.directory else None
        if named is not None:
            src = self.one("SELECT id FROM source WHERE name = ?", (self.directory.module,))
            taken = self.one("SELECT COUNT(*) FROM person WHERE source = ?", (src,)) if src else 0
            people += named[1] - (taken or 0)
        activity = self.register_count("ACTIVITY_CODE", self.directory.module) \
            if self.directory else None
        return {"companies": t.get("firms", 0), "with_phone": phones,
                "with_email": len(email), "with_both": with_both,
                "people": people, "people_firms": named[0] if named else None,
                "with_activity": activity[0] if activity else None,
                # The register's counts wait for their job (shown as such, never 0).
                "counting": self.directory is not None and (named is None or activity is None)}

    def backlog(self, finder_capability=("contact_finder", "find_contacts"),
                site=("website", "read_site"), since=None) -> dict:
        """finder.backlog's counts on the overlay: firms with no contact, of
        them those with a site still to read, those the finder searched, and
        those with no site and a name; the finder's last day."""
        from harvey import contacts as c

        t = self.tally()
        no_contact = t.get("firms", 0) - t.get("firms_with_contact", 0)
        site_cut = self._cutoff(*site)
        with_site, to_read = self.x("overlay.read.backlog.sites", (site_cut,)).fetchone()
        searched = self.one("overlay.read.backlog.searched", (self._cutoff(*finder_capability),))
        day = int(time.time()) - 86400
        mid = self.module_id(finder_capability[0])
        cap = self.capability_id(*finder_capability)
        n, found, cost = (self.x("overlay.read.searches_since", (day, mid, cap)).fetchone()
                          if mid and cap else (0, 0, 0.0))
        bit = self.one("SELECT bit FROM looked_for WHERE name = 'website'") or 0
        looked = {m: (k, fnd) for m, k, fnd in self.x("overlay.read.lookups_since", (day, bit))} \
            if bit else {}
        del c
        return {"without_contact": no_contact, "site_to_read": to_read or 0,
                "searched": searched or 0,
                "last_day": {"companies": n or 0, "found": found or 0,
                             "cost": round(cost or 0.0, 4)},
                "no_website": no_contact - (with_site or 0), "looked": looked}

    def _cutoff(self, module: str, capability: str, since=None) -> int:
        """A search counts as done from this time on (unix seconds): a
        forced run's start, else the capability's refresh age (0: ever)."""
        from harvey.overlay.store import unix

        cut = contacts.search_cutoff(module, capability, since)
        return unix(cut) or 0

    def next_free(self, limit: int) -> list[str]:
        """finder.next_free on the overlay: firms with no contact and a site
        not read the deep way yet, newest first; then firms whose site was
        read the shallow way, whatever contacts that read found."""
        cut = self._cutoff("website", "read_site")
        out = [r[0] for r in self.x("overlay.read.next_free", (cut, int(limit)))]
        if len(out) < limit:
            out += [r[0] for r in self.x("overlay.read.next_shallow", (int(limit) - len(out),))]
        return [str(i) for i in out]

    def next_paid(self, limit: int, searchers,
                  walk: dict | None = None) -> tuple[list[str], dict]:
        """finder.next_paid on the overlay: firms with no contact and no
        site, named, that a search module able to find them hasn't searched
        yet, newest first; and the walk to keep for the next call.

        ``walk`` (finder.PAID_WALK_KEY) remembers a stretch of the walk,
        from ``hi`` down to ``lo`` (keys (created, id)), whose firms were
        all searched by every one of these searchers (or are unnamed) when
        it was walked: the next call reads the firms above it (those added
        since), then goes on below it, so the same firms come back in the
        same order without the stretch being read again. It starts over
        when the searchers change, a pass reaches the end with nothing
        left, or it is PAID_WALK_SECONDS old (a search cleared, or aged
        past its refresh_days, is seen again)."""
        sig = paid_walk_searchers(searchers)
        now = int(time.time())
        if (not isinstance(walk, dict) or walk.get("searchers") != sig
                or now - int(walk.get("since") or 0) >= PAID_WALK_SECONDS):
            walk = {"searchers": sig, "since": now, "hi": None, "lo": None}
        else:
            walk = dict(walk)
        if not searchers:
            return [], walk
        caps = []
        for s in searchers:
            mid, cid = self.module_id(s.module), self.capability_id(s.module, s.capability)
            caps.append((mid, cid, self._cutoff(s.module, s.capability)))

        def left(batch: list[int]) -> list[int]:
            # (The batch's firms some searcher hasn't searched, and named.)
            done_by_all = set(batch)
            for mid, cid, cut in caps:
                if mid is None or cid is None:
                    done_by_all = set()
                    break
                done_by_all &= {r[0] for r in self.x("overlay.read.searched_by",
                                                     (json.dumps(batch), mid, cid, cut))}
            rest = [b for b in batch if b not in done_by_all]
            named = self.sort_keys(rest, "name")
            return [b for b in rest if named.get(b)]

        out: list[str] = []
        page = PAID_WALK_PAGE
        hi, lo = walk["hi"], walk["lo"]
        if hi is not None:
            # The firms above the stretch (added since): when all of them
            # are done, the stretch reaches up to the newest.
            upper, newest, clean = PAID_TOP, None, True
            while len(out) < limit:
                rows = self.x("overlay.read.next_paid", (*upper, *hi, page)).fetchall()
                if not rows:
                    break
                newest = newest or [rows[0][1], rows[0][0]]
                todo = left([r[0] for r in rows])
                clean = clean and not todo
                out += [str(b) for b in todo][:limit - len(out)]
                upper = (rows[-1][1], rows[-1][0])
                if len(rows) < page:
                    break
            else:
                clean = False
            if clean and newest:
                walk["hi"] = newest
        if len(out) < limit:
            # Below the stretch (from the top when there is none): it grows
            # over the firms done, up to the first one that isn't.
            upper = tuple(lo) if lo is not None else PAID_TOP
            growing = True
            while len(out) < limit:
                rows = self.x("overlay.read.next_paid", (*upper, *PAID_BOTTOM, page)).fetchall()
                todo = left([r[0] for r in rows]) if rows else []
                if growing:
                    waiting = set(todo)
                    for fid, created in rows:
                        if fid in waiting:
                            growing = False
                            break
                        if walk["hi"] is None:
                            walk["hi"] = [created, fid]
                        walk["lo"] = [created, fid]
                out += [str(b) for b in todo][:limit - len(out)]
                if len(rows) < page:
                    if growing:
                        # The end with nothing left: the next call starts over.
                        walk = {"searchers": sig, "since": now, "hi": None, "lo": None}
                    break
                upper = (rows[-1][1], rows[-1][0])
        return out, walk

    def select(self, f, ids=None, force: bool = False, limit: int = 1000) -> list[dict]:
        """finder.select on the overlay: the firms a Find-contacts run takes
        (the filter's conditions, unknowns included; with no contact unless
        ``force``), newest first, with whether each has a site."""
        if ids is not None:
            found = list(dict.fromkeys(i for i in (self.live(x) for x in ids if x)
                                       if i is not None))
            allowed = set(self.matching(f, possible=True)) \
                if (self.firm_set(f, True) is not None or self.narrowed(self.predicates(f))) \
                else None
            if allowed is not None:
                found = [i for i in found if i in allowed]
        else:
            s = self.firm_set(f, possible=True)
            p = self.predicates(f)
            if s is None and not (f.q or "").strip():
                if not force:
                    p = {**p, "contact": 0} if p["contact"] in (None, 0) else p
                args = (p["site"], p["site"], p["contact"], p["contact"], p["email"], p["phone"],
                        p["mobile"])
                found = [r[0] for r in self.x("overlay.read.walk", (*args, int(limit), 0))]
            else:
                found = self.matching(f, possible=True)
        # (Each firm's time and site only, those with no contact unless forced:
        # read in SQL, a big area's firms never read whole.)
        rows = {}
        for chunk in chunks(found):
            rows.update((r[0], r[1:]) for r in self.x("overlay.read.finder_rows",
                                                      (json.dumps(chunk), 0 if force else 1)))
        found = [i for i in found if i in rows]
        words = [contacts.fold(w) for w in (f.q or "")[:200].split()[:8]]
        if words:
            names = self.sort_keys(found, "name")
            found = [i for i in found
                     if all(w in contacts.name_norm(names.get(i, "")) for w in words)]
        found.sort(key=lambda i: (rows[i][0], i), reverse=True)
        # (``website_found``: a search found its site; ``website_waiting``:
        # unconfirmed, nothing is read off it until the re-check.)
        return [{"id": str(i), "site": bool(rows[i][1]), "website_found": bool(rows[i][2]),
                 "website_waiting": bool(rows[i][2]) and rows[i][3] == TRUST["unconfirmed"]}
                for i in found[:limit]]

    def address_facts(self, email: str) -> dict:
        """What email_rules decides an address by, from every firm it is
        linked to (any link, not one chosen): whether a link names a person,
        the names of the people of those firms (Harvey's and the register's
        administrators), each firm's legal forms (``firm_forms``: per firm,
        the register's form and ANAF's LEGAL_FORM readings, so each is
        decided on its own), the evidence sources and pages of the address,
        and the firms' domains."""
        address = (email or "").strip().lower()
        out: dict = {"email": address, "person_link": False, "people": [], "firm_forms": [],
                     "sources": [], "evidence_urls": [], "domains": []}
        links = list(self.x("overlay.read.links_of_address", (EMAIL, address)))
        if not links:
            return out
        out["person_link"] = any(person for _, person, _, _ in links)
        lives = self.live_of([firm for firm, *_ in links])
        groups = self.members(list(dict.fromkeys(lives.values())))
        described = self.describe(self.group_idents(list(groups)))
        for f, people in self.people_named(groups, described).items():
            out["people"] += [p["name"] for p in people if p["name"] not in out["people"]]
        for f in groups:
            form = (described.get(f, {}).get("card") or {}).get("legal_form") or ""
            anaf = list(dict.fromkeys(r["value_text"] for r in self.register_readings(
                f, {"LEGAL_FORM"}) if r["value_text"]))
            out["firm_forms"].append([form, anaf])
        for row in self.company_rows(list(groups)):
            if row.get("domain") and row["domain"] not in out["domains"]:
                out["domains"].append(row["domain"])
        for firm, contact, src, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[firm, contact] for firm, _, _, contact in links], 500):
            if src and src not in out["sources"]:
                out["sources"].append(src)
            if url and url not in out["evidence_urls"]:
                out["evidence_urls"].append(url)
        return out

    def audience_rows(self, f=None, emails=None, only=None) -> list[dict]:
        """The email contacts a campaign could take (campaigns.add_members
        decides): those the Contacts filter ``f`` keeps (its kind forced to
        email), or the addresses ``emails`` (ticked rows, typed ones). Each
        with its status, do-not-contact flag, and the one link it is read
        through: a live firm, through a person first, then the surest, then
        the oldest (trust None when it reaches no live firm). Its company,
        person and evidence come with it. ``only`` (contact ids, with
        ``f``): of those, the ones ``f`` keeps (_contact_ids)."""
        ids: list[int] = []
        if emails is not None:
            for e in dict.fromkeys((e or "").strip().lower() for e in emails):
                cid = self.one("SELECT id FROM contact WHERE kind = ? AND value = ?", (EMAIL, e))
                if cid is not None:
                    ids.append(cid)
        elif f is not None:
            from dataclasses import replace

            f = replace(f, kind="email")
            if not f.nothing:
                ids = self._contact_ids(f, self.firm_set(f), self.predicates(f), only=only)
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", ids)}
        ids = [c for c in ids if c in rows and rows[c][11] == EMAIL]
        links = list(self._by_ids("overlay.read.links_of_contacts", ids))
        lives = self.live_of([l[1] for l in links])
        best: dict[int, tuple] = {}
        for contact, firm, trust, person, location, first in links:
            if firm not in lives:
                continue
            key = (person == 0, trust, first or 0, firm)
            if contact not in best or key < best[contact][0]:
                best[contact] = (key, firm, trust, person)
        cards = {r["id"]: r for r in self.company_rows(
            list(dict.fromkeys(lives[b[1]] for b in best.values())))}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 [b[3] for b in best.values() if b[3]])}
        evidence = {}
        for firm, contact, src, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[b[1], c] for c, b in best.items()], 500):
            evidence.setdefault((firm, contact), (src, url))
        out = []
        for c in ids:
            r = rows[c]
            row = {"contact_id": c, "email": r[2], "status": STATUS_NAMES.get(r[5], "") or "",
                   "do_not_contact": bool(r[6]), "trust": None, "firm": None,
                   "company_id": None, "person_id": 0, "person": "", "role": "",
                   "person_source": "", "source": "", "evidence_url": "", "company": "",
                   "industry": "", "domain": ""}
            if c in best:
                _, firm, trust, person = best[c]
                card = cards.get(str(lives[firm]), {})
                src, url = evidence.get((firm, c), ("", ""))
                p = persons.get(person)
                row.update(trust=TRUST_NAMES.get(trust, ""), firm=firm,
                           company_id=card.get("id"), person_id=person or 0,
                           person=p[2] if p else "", role=(p[4] or "") if p else "",
                           person_source=(p[5] or "") if p else "", source=src or "",
                           evidence_url=url or "", company=card.get("name", ""),
                           industry=card.get("industry", ""), domain=card.get("domain", ""))
            out.append(row)
        return out

    # ── The Writer's brief ──

    def company_brief(self, cid, codes) -> tuple[str, str, list[dict]] | None:
        """(location, activity, readings newest first) for
        state.company_brief_lines; None for a firm not on file."""
        fid = self.live(cid)
        if fid is None:
            return None
        row = (self.company_rows([fid]) or [None])[0]
        if row is None:
            return None
        readings = [r for r in self.readings_of(fid) if r["signal_code"] in codes]
        readings += [{"signal_code": r["signal_code"], "value_num": r["value_num"],
                      "value_text": r["value_text"], "observed_at": r["observed_at"]}
                     for r in self.register_readings(fid, set(codes))]
        activity = ""
        if not (row["industry"] or "").strip():
            activity = next((a["name_en"] or a["name"] for a in row["activities"]
                             if a["name_en"] or a["name"]), "")
        return row["location"], activity, readings


    # ── The contact finder's reads of one company (finder.py) ──

    def finder_company(self, cid) -> dict | None:
        """finder.company_row on the overlay: the live firm's id, name,
        domain, website and its trust ("" for a site a register declares),
        place, CUI, main phone and where it was found; None for a record
        merged into another, or none."""
        fid = self.fid(cid)
        if fid is None or self.live(fid) != fid:
            return None
        rows = self.company_rows([fid])
        if not rows:
            return None
        r = rows[0]
        trust, found = self.x("SELECT website_trust, website_source FROM firm WHERE id = ?",
                              (fid,)).fetchone()
        out = {k: r.get(k) or "" for k in ("id", "name", "domain", "website", "location", "cui",
                                           "phone", "source_url", "source", "company_size")}
        out["website_trust"] = TRUST_NAMES.get(trust, "") if trust is not None else ""
        # (A website a search found, as opposed to one the firm declares,
        # which has a source and no trust.)
        out["website_found"] = found is not None and trust is not None and bool(r.get("website"))
        return out

    def _group_links(self, cid) -> tuple[list[int], list, dict]:
        """(the live firm's records, their links, the linked contacts' rows)."""
        fid = self.live(cid)
        if fid is None:
            return [], [], {}
        group = self.members([fid])[fid]
        links = list(self._by_ids("overlay.read.links_of_firms", group))
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows",
                                              list({l[1] for l in links}))}
        return group, links, rows

    def finder_on_file(self, cid) -> set[str]:
        """The phones and emails on file for a company that a source other
        than a read of a website gave (a register, ANAF, the taxpayer file,
        a listing), lower case: what confirms a site the company's. One a
        site's own read linked never does, or a second read of the same
        site would confirm it by what the first left."""
        return {c["value"].lower() for c in self._official_contacts(cid)
                if c["kind"] in (PHONE, EMAIL)}

    def _official_contacts(self, cid) -> list[dict]:
        """The company's contacts with evidence from a source that isn't a
        website's read (SITE_SOURCES): {"kind", "value", "n_firms"}."""
        group, links, rows = self._group_links(cid)
        pairs = [[l[0], l[1]] for l in links if l[1] in rows]
        official = set()
        for contact, source, _ in self._by_ids("overlay.read.evidence_caps", pairs, 500):
            if (source or "") not in SITE_SOURCES:
                official.add(contact)
        return [{"kind": rows[c][11], "value": rows[c][2], "n_firms": rows[c][9] or 0}
                for c in sorted(official)]

    def site_facts(self, cid) -> dict | None:
        """What the registers say of a company, as the contact finder's
        evidence cards compare a website with it (site_match.Facts): name,
        legal form, CUI, registration number, street and number, town,
        county, and the phones (E.164) and emails official sources give,
        each with how many firms hold it (``holders``: an accountant's
        address or a group's switchboard says nothing of whose site it is)."""
        from harvey import site_match

        fid = self.live(cid)
        if fid is None:
            return None
        rows = self.company_rows([fid])
        if not rows:
            return None
        row = rows[0]
        group = self.members([fid])[fid]
        idents = self.idents(group)
        own = [k for m in group for k in idents.get(m, [])]
        card = self.describe({fid: own}).get(fid, {}).get("card") or {}
        reg = card.get("reg") or ""
        if not reg and (row.get("external_id") or "").count(":") >= 2:
            reg = row["external_id"].rsplit(":", 1)[-1]
        place = [p.strip() for p in (row.get("location") or "").split(",")]
        street, number = site_match.split_street(row.get("address") or "")
        official = self._official_contacts(fid)
        return {"name": row.get("name") or "", "form": card.get("legal_form") or "",
                "cui": row.get("cui") or "", "reg": reg, "street": street, "number": number,
                "locality": place[0] if place else "",
                "county": place[1] if len(place) > 2 else "",
                "phones": [c["value"] for c in official if c["kind"] == PHONE],
                "holders": {c["value"] if c["kind"] == PHONE else c["value"].lower(): c["n_firms"]
                            for c in official if c["kind"] in (PHONE, EMAIL)},
                "emails": [c["value"].lower() for c in official if c["kind"] == EMAIL],
                "region": "RO" if (row.get("location") or "").strip().endswith("Romania")
                or row.get("cui") else ""}

    def classify_calls_today(self, task: str = "") -> int:
        """Calls the classifier made since midnight UTC (one task's, when given)."""
        import datetime as _dt

        day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d 00:00:00")
        like = f"classify.{task}" if task else "classify.%"
        return int(self.one("overlay.classify.calls_today", (day, like)) or 0)

    def classify_spend_since(self, since: str) -> float:
        """USD the classifier's own calls cost since ``since`` (UTC)."""
        return float(self.one("overlay.classify.spend_since", (since,)) or 0.0)

    def classify_by_task(self, since: str) -> dict:
        """{task: {"calls", "errors", "tokens_in", "tokens_out", "usd",
        "unpriced"}} of the classifier's calls since ``since`` (UTC)."""
        out = {}
        for task, calls, errors, tin, tout, usd, unpriced in self.x(
                "overlay.classify.by_task", (since,)):
            out[str(task).split(".", 1)[-1]] = {
                "calls": int(calls or 0), "errors": int(errors or 0),
                "tokens_in": int(tin or 0), "tokens_out": int(tout or 0),
                "usd": round(float(usd or 0.0), 6), "unpriced": int(unpriced or 0)}
        return out

    def classify_sizes(self, since: str) -> dict:
        """{task: {"answered", "avg_in", "max_in", "avg_out", "max_out",
        "models": {model: the same, of its calls only}}} of the
        classifier's answered calls since ``since`` (UTC): the tokens the
        endpoints billed."""
        out: dict = {}
        for task, model, n, ain, xin, aout, xout in self.x("overlay.classify.sizes", (since,)):
            n = int(n or 0)
            one = {"answered": n, "avg_in": round(float(ain or 0)), "max_in": int(xin or 0),
                   "avg_out": round(float(aout or 0)), "max_out": int(xout or 0)}
            t = out.setdefault(str(task).split(".", 1)[-1],
                               {"answered": 0, "sum_in": 0.0, "max_in": 0, "sum_out": 0.0,
                                "max_out": 0, "models": {}})
            t["models"][str(model or "")] = one
            t["answered"] += n
            t["sum_in"] += float(ain or 0) * n
            t["sum_out"] += float(aout or 0) * n
            t["max_in"] = max(t["max_in"], one["max_in"])
            t["max_out"] = max(t["max_out"], one["max_out"])
        for t in out.values():
            n = t["answered"] or 1
            t["avg_in"] = round(t.pop("sum_in") / n)
            t["avg_out"] = round(t.pop("sum_out") / n)
        return out

    def classify_last_source(self, model: str, since: str) -> str:
        """How the last answered call of ``model`` since ``since`` was
        priced (its usage row's source), "" when there is none."""
        return str(self.one("overlay.classify.last_source", (since, model)) or "")

    def site_check_ids(self, limit: int = -1) -> list[int]:
        """Live firms whose website a search found and nothing checked yet
        (unconfirmed, or likely with no basis): the re-check's selection,
        oldest first."""
        return [r[0] for r in self.x("overlay.site_check.ids", (int(limit),))]

    def site_check_count(self) -> dict:
        """{"unconfirmed": n, "likely": n} of site_check_ids."""
        out = {"unconfirmed": 0, "likely": 0}
        for trust, n in self.x("overlay.site_check.count"):
            out[TRUST_NAMES.get(trust, "unconfirmed")] = n
        return out

    def site_check_rows(self, ids) -> list[dict]:
        """For each firm: id, website, trust, and the facts screen() reads
        from a URL alone (name, CUI, registration number)."""
        out = []
        for chunk in chunks(list(ids), 500):
            rows = {r["id"]: r for r in self.company_rows(chunk)}
            for fid, website, trust in self._by_ids("overlay.site_check.rows", chunk):
                r = rows.get(str(fid)) or {}
                out.append({"id": fid, "website": website or "",
                            "trust": TRUST_NAMES.get(trust, ""), "name": r.get("name") or "",
                            "cui": r.get("cui") or "", "external_id": r.get("external_id") or ""})
        return out

    def site_candidates(self, cid) -> list[str]:
        """The candidates a waiting found website's lookup left in question
        (site_match.waiting_detail on the newest read of it), in order; [] for
        a site found before the finder kept them."""
        from harvey.site_match import WAITING, waiting_candidates

        fid = self.live(cid)
        cap = self.capability_id("website", "read_site")
        if fid is None or cap is None:
            return []
        group = self.members([fid])[fid]
        marks = ",".join("?" * len(group))
        row = self.x(f"SELECT detail FROM search WHERE is_person = 0 AND target IN ({marks}) "
                     f"AND capability = ? AND detail LIKE ? ORDER BY at DESC, id DESC LIMIT 1",
                     (*group, cap, WAITING + "%")).fetchone()
        return waiting_candidates(row[0]) if row else []

    def site_check_failures(self, cid, prefix: str) -> int:
        """On how many days the re-check found a firm's website not
        answering (its judge_site errors whose detail starts ``prefix``)."""
        fid = self.live(cid)
        mid = self.module_id("contact_finder")
        cap = self.capability_id("contact_finder", "judge_site")
        if fid is None or mid is None or cap is None:
            return 0
        group = self.members([fid])[fid]
        marks = ",".join("?" * len(group))
        return int(self.one(
            f"SELECT COUNT(DISTINCT at / 86400) FROM search WHERE is_person = 0 "
            f"AND target IN ({marks}) AND module = ? AND capability = ? AND outcome = 0 "
            f"AND detail LIKE ?", (*group, mid, cap, prefix + "%")) or 0)

    def contact_checks(self, contact_id: int) -> list[dict]:
        """The classifier's verdicts on a contact's links, newest first
        (harvey/contact_check.py)."""
        from harvey.overlay.schema import CHECK_VERDICT

        names = {v: k for k, v in CHECK_VERDICT.items()}
        return [{"company_id": str(firm), "verdict": names.get(verdict, ""), "at": at,
                 "profile": profile, "model": model, "p": p, "note": note or "",
                 "batch": batch, "undone": undone is not None}
                for firm, verdict, at, profile, model, p, note, batch, undone in self.x(
                    "overlay.contact_check.of_contact", (int(contact_id),))]

    def contact_checks_open(self, verdict: str = "unsure", limit: int = 50) -> list[dict]:
        """Links the classifier gave ``verdict`` that still stand, newest
        first: the unsure ones are a person's to review."""
        from harvey.overlay.schema import CHECK_VERDICT

        rows = list(self.x("overlay.contact_check.open",
                           (CHECK_VERDICT[verdict], int(limit))))
        values = {r[0]: r for r in self._by_ids("overlay.read.contact_rows",
                                                 list({r[1] for r in rows}))} if rows else {}
        cards = {r["id"]: r for r in self.company_rows(list({r[0] for r in rows}))} \
            if rows else {}
        return [{"company_id": str(firm), "company": (cards.get(str(firm)) or {}).get("name", ""),
                 "contact_id": contact,
                 "kind": values[contact][1] or "" if contact in values else "",
                 "value": values[contact][2] if contact in values else "",
                 "verdict": verdict, "at": at, "profile": profile, "model": model, "p": p,
                 "note": note or "", "batch": batch}
                for firm, contact, _, at, profile, model, p, note, batch in rows]

    def contact_check_done(self, batch: str, cid) -> set[int]:
        """The contacts ``batch`` asked about at a company already."""
        fid = self.live(cid)
        if fid is None:
            return set()
        return {r[0] for r in self.x("overlay.contact_check.done", (batch, fid))}

    def contact_links(self, ids) -> dict[int, dict]:
        """{contact id: {"kind", "value", "links": [{"company_id", "company",
        "source", "trust", "evidence_url"}]}} for many contacts in one read:
        what ``reach`` gives one contact (each link's evidence rows, the
        link's own trust when it has none, by company name), with the
        firm's name only. Contacts not on file are left out."""
        ids = [int(i) for i in ids]
        kinds = self.contact_kind_value(ids)
        links = list(self._by_ids("overlay.read.links_of_contacts", [i for i in ids
                                                                      if i in kinds]))
        lives = self.live_of([l[1] for l in links])
        names = self.sort_keys(list(dict.fromkeys(lives.values())), "name")
        evidence: dict[tuple, list] = {}
        for firm, contact, src, trust, url, _first, person, _loc in self._by_ids(
                "overlay.read.evidence", [[l[1], l[0]] for l in links], 500):
            evidence.setdefault((firm, contact, person), []).append((src, trust, url))
        per: dict[int, list] = {}
        for contact, firm, trust, person, _loc, _first in links:
            live = lives.get(firm, firm)
            for src, etrust, url in evidence.get((firm, contact, person), [(None, trust, "")]):
                per.setdefault(contact, []).append({
                    "company_id": str(live), "company": names.get(live, ""),
                    "source": src or "", "trust": TRUST_NAMES.get(etrust, ""),
                    "evidence_url": url or ""})
        out = {}
        for i in ids:
            if i in kinds:
                rows = sorted(per.get(i, []), key=lambda r: r["company"])
                out[i] = {"kind": kinds[i][0], "value": kinds[i][1], "links": rows}
        return out

    def contact_kind_value(self, ids) -> dict[int, tuple[str, str]]:
        """{contact id: (its kind, its value)}."""
        return {r[0]: (r[1] or "", r[2] or "")
                for r in self._by_ids("overlay.read.contact_rows", [int(i) for i in ids])}

    def contact_check_batch(self, batch: str) -> dict:
        """{verdict: n} of a verification run, and how many were undone."""
        from harvey.overlay.schema import CHECK_VERDICT

        names = {v: k for k, v in CHECK_VERDICT.items()}
        out = {"yes": 0, "no": 0, "unsure": 0, "undone": 0}
        for verdict, n, undone in self.x("overlay.contact_check.batch", (batch,)):
            out[names.get(verdict, "unsure")] = int(n or 0)
            out["undone"] += int(undone or 0)
        return out

    def site_check_last(self, cid) -> str:
        """The detail of the re-check's newest search of a firm ("" when
        none): what its last check said."""
        fid = self.live(cid)
        mid = self.module_id("contact_finder")
        cap = self.capability_id("contact_finder", "judge_site")
        if fid is None or mid is None or cap is None:
            return ""
        group = self.members([fid])[fid]
        marks = ",".join("?" * len(group))
        return self.one(
            f"SELECT detail FROM search WHERE is_person = 0 AND target IN ({marks}) "
            f"AND module = ? AND capability = ? ORDER BY at DESC, id DESC LIMIT 1",
            (*group, mid, cap)) or ""

    def website_basis(self, cid) -> dict:
        """Why a found website was taken (firm.website_basis), with its trust
        and the module whose search found it; {} for a site no search found."""
        fid = self.live(cid)
        if fid is None:
            return {}
        row = self.x("SELECT website_trust, website_source, website_basis FROM firm WHERE id = ?",
                     (fid,)).fetchone()
        # (A website the firm's own source declared has a source and no trust.)
        if row is None or row[1] is None or row[0] is None:
            return {}
        try:
            basis = json.loads(row[2]) if row[2] else {}
        except ValueError:
            basis = {}
        source = self.one("SELECT name FROM source WHERE id = ?", (row[1],)) or ""
        return {**(basis if isinstance(basis, dict) else {}),
                "trust": TRUST_NAMES.get(row[0], "") if row[0] is not None else "",
                "found_by": source}

    def finder_has_contact(self, cid) -> bool:
        fid = self.live(cid)
        return bool(fid is not None and self.one(
            "SELECT n_contacts FROM firm WHERE id = ?", (fid,)))

    def searched_targets(self, ids, *, person: bool, module: str, capability: str,
                         cutoff=None, looked_for: str = "") -> set[int]:
        """Which of these records (``person``: persons) the module's
        capability searched since ``cutoff`` (an error is no search; with
        ``looked_for``, only a search that looked for that word)."""
        from harvey.overlay.store import unix

        mid, cap = self.module_id(module), self.capability_id(module, capability)
        if mid is None or cap is None or not ids:
            return set()
        bit = 0
        if looked_for:
            bit = self.one("SELECT bit FROM looked_for WHERE name = ?", (looked_for,))
            if bit is None:
                return set()
        at = unix(cutoff) or 0
        out = set()
        for chunk in chunks(ids):
            out |= {r[0] for r in self.x("overlay.read.searched_targets", (
                json.dumps(chunk), int(person), mid, cap, at, bit, bit))}
        return out

    def finder_searched(self, cid, module: str, capability: str, cutoff=None,
                        looked_for: str = "") -> bool:
        """finder.searched for a company: one of its records searched."""
        fid = self.live(cid)
        if fid is None:
            return False
        return bool(self.searched_targets(self.members([fid])[fid], person=False, module=module,
                                          capability=capability, cutoff=cutoff,
                                          looked_for=looked_for))

    def finder_searched_many(self, cids, module: str, capability: str, cutoff=None,
                             looked_for: str = "") -> set[str]:
        """finder_searched for many companies at once (an estimate): the
        ones (as given) one of whose records was searched."""
        lives = {c: self.live(c) for c in cids}
        groups = self.members(list(dict.fromkeys(f for f in lives.values() if f is not None)))
        done = self.searched_targets([m for ms in groups.values() for m in ms], person=False,
                                     module=module, capability=capability, cutoff=cutoff,
                                     looked_for=looked_for)
        return {c for c, f in lives.items()
                if f is not None and any(m in done for m in groups[f])}

    def finder_named(self, cid) -> list[dict]:
        """Everyone named at a company (its records' people), in id order."""
        fid = self.live(cid)
        if fid is None:
            return []
        rows = self._by_ids("overlay.read.people_of", self.members([fid])[fid])
        return sorted(({"id": pid, "name": name, "role": role or "", "source": source or ""}
                       for firm, pid, name, norm, role, source, first in rows),
                      key=lambda p: p["id"])

    def finder_people(self, cid, module: str, capability: str, cutoff=None,
                      limit: int = 3) -> list[dict]:
        """The people at a company reached by no contact of their own and
        not searched by the module's capability since ``cutoff``, in id
        order, at most ``limit`` (-1: all)."""
        group, links, _ = self._group_links(cid)
        named = self.finder_named(cid)
        reached = {l[3] for l in links if l[3]}
        searched = self.searched_targets([p["id"] for p in named], person=True, module=module,
                                         capability=capability, cutoff=cutoff)
        out = [p for p in named if p["id"] not in reached and p["id"] not in searched]
        return out if limit < 0 else out[:limit]

    def finder_linked_emails(self, cid) -> set[tuple[int, str]]:
        """(person, address) for every email linked to a person at the company."""
        _, links, rows = self._group_links(cid)
        return {(l[3], rows[l[1]][2].lower()) for l in links
                if l[3] and l[1] in rows and rows[l[1]][11] == EMAIL}

    def finder_emails_at(self, cid, domain: str, built=("", "")) -> list[str]:
        """The addresses a source published for a company at ``domain``: not
        a guess, not invalid, and found by something other than the address
        check that builds them (``built``: its (module, capability))."""
        _, links, rows = self._group_links(cid)
        bad = {schema.CONTACT_STATUS["guess"], schema.CONTACT_STATUS["invalid"]}
        wanted = [(l[0], l[1]) for l in links if l[1] in rows and rows[l[1]][11] == EMAIL
                  and rows[l[1]][2].lower().endswith("@" + domain.lower())
                  and rows[l[1]][5] not in bad]
        published = set()
        for contact, source, capability in self._by_ids("overlay.read.evidence_caps",
                                                        [list(w) for w in wanted], 500):
            if (source or "", capability or "") != tuple(built):
                published.add(contact)
        return list(dict.fromkeys(rows[c][2] for _, c in wanted if c in published))

    def finder_sources(self, cid) -> set[str]:
        """Every source that found the company's records."""
        fid = self.live(cid)
        if fid is None:
            return set()
        return {r[1] for r in self._by_ids("overlay.read.sources_of", self.members([fid])[fid])}

    def finder_employees(self, cid):
        """The company's latest employee count from the register that reads
        it, or None."""
        fid = self.live(cid)
        if fid is None:
            return None
        return (self.figures([fid]).get(fid) or {}).get("employees")

    def companies_needing_prospects(self, limit: int, module: str, capability: str,
                                    cutoff=None) -> list[dict]:
        """StateManager.companies_needing_prospects on the overlay: live
        firms with a domain, no prospect on any of their records, and not
        searched by the module's capability since ``cutoff``; never
        searched first, then the longest ago."""
        ids = [r[0] for r in self.x("overlay.read.with_domain_ids")]
        if not ids:
            return []
        groups = self.members(ids)
        live_of = {m: f for f, g in groups.items() for m in g}
        with_prospect = {live_of[r[0]] for r in self._by_ids("overlay.read.prospects_of",
                                                             list(live_of))}
        done = {live_of[r] for r in self.searched_targets(list(live_of), person=False,
                                                          module=module, capability=capability,
                                                          cutoff=cutoff)}
        last: dict[int, int] = {}
        mid, cap = self.module_id(module), self.capability_id(module, capability)
        if mid is not None and cap is not None:
            for chunk in chunks(list(live_of)):
                for target, at in self.x("overlay.read.last_searched",
                                         (json.dumps(chunk), mid, cap)):
                    f = live_of[target]
                    last[f] = max(last.get(f, 0), at or 0)
        wanted = [f for f in ids if f not in with_prospect and f not in done]
        wanted.sort(key=lambda f: (f in last, last.get(f, 0), f))
        cards = {int(r["id"]): r for r in self.company_rows(wanted[:int(limit)])}
        return [{"id": str(f), "name": cards[f]["name"], "domain": cards[f]["domain"],
                 "industry": cards[f]["industry"]} for f in wanted[:int(limit)] if f in cards]

    def labels(self, ids) -> dict[str, str]:
        """{company id: its name, else its domain, else its id} (job labels),
        read for the names alone (no enrichment)."""
        fids = {str(i): self.fid(i) for i in dict.fromkeys(ids)}
        found = [f for f in fids.values() if f is not None]
        rows = self.firm_rows(found)
        names = self.sort_keys(found, "name") if found else {}
        out = {}
        for cid, fid in fids.items():
            if fid is None or fid not in rows:
                continue
            out[cid] = names.get(fid) or rows[fid]["domain"] or cid
        return out

    # ── Duplicates: the review list and the merges in force ──

    def duplicate_sides(self, ids) -> dict[str, dict]:
        """{company id: the side the duplicates panel shows}: its record
        (name, CUI, place, address, phone, domain, website, first source,
        the firm it is merged into, when it was added), its phones, the
        domains, listing ids and emails it is known by, its sources and how
        many places it has."""
        ids = [int(i) for i in dict.fromkeys(ids) if str(i).isdigit()]
        if not ids:
            return {}
        cards = {int(r["id"]): r for r in self.company_rows(ids)}
        groups = self.members(ids)
        records = [m for g in groups.values() for m in g]
        lives = self.live_of(ids)
        idents = self.idents(records)
        keys: dict[int, list] = {}
        for firm, kind, value in self._by_ids("overlay.read.keys_of", records):
            keys.setdefault(firm, []).append((kind, value))
        places = dict(self._by_ids("overlay.read.places_count", records))
        sources: dict[int, list] = {}
        for firm, name, ref, first, last in self._by_ids("overlay.read.sources_of", records):
            sources.setdefault(firm, []).append((first or 0, name))
        phones = self.phones(groups)
        out = {}
        for i in ids:
            card = cards.get(i)
            if card is None:
                continue
            group = groups[i]
            domains = [card["domain"]] if card["domain"] else []
            emails, listings = [], []
            for m in group:
                for kind, value in keys.get(m, []):
                    if kind == schema.FIRM_KEY["domain"] and value not in domains:
                        domains.append(value)
                    elif kind == schema.FIRM_KEY["email"] and value not in emails:
                        emails.append(value)
                for scheme, key in idents.get(m, []):
                    s = self.schemes.named(scheme)
                    if s is not None and s.kind == "external" and f"{s.prefix}{key}" not in listings:
                        listings.append(f"{s.prefix}{key}")
            srcs = [n for _, n in sorted(s for m in group for s in sources.get(m, []))]
            live = lives.get(i, i)
            out[str(i)] = {
                "id": str(i), "name": card["name"], "cui": card["cui"],
                "location": card["location"], "address": card["address"],
                "phone": card["phone"], "domain": card["domain"], "website": card["website"],
                "source": card["source"], "merged_into": str(live) if live != i else "",
                "created_at": card["created_at"],
                "phones": [p["value"] for p in phones.get(i, [])], "domains": domains,
                "listings": listings, "emails": emails,
                "sources": list(dict.fromkeys(srcs)),
                "locations": sum(places.get(m, 0) for m in group)}
        return out

    @staticmethod
    def _reasons(text) -> list:
        try:
            out = json.loads(text or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return out if isinstance(out, list) else []

    def duplicates(self, status: str = "pending", limit: int = 100) -> list[dict]:
        """StateManager.duplicates on the overlay: pairs on the review list
        with ``status``, most likely first, each side as the panel shows it."""
        pairs = [dict(zip(("company_a", "company_b", "score", "reasons", "status", "created_at",
                           "decided_at"), r))
                 for r in self.x("overlay.read.candidates", (status, int(limit)))]
        sides = self.duplicate_sides({x for p in pairs for x in (p["company_a"], p["company_b"])})
        out = []
        for p in pairs:
            a, b = str(p["company_a"]), str(p["company_b"])
            out.append({**p, "company_a": a, "company_b": b, "reasons": self._reasons(p["reasons"]),
                        "created_at": _db_time(p["created_at"]),
                        "decided_at": _db_time(p["decided_at"]),
                        "a": sides.get(a), "b": sides.get(b)})
        return out

    def _firm_identity(self, live_ids) -> dict[str, tuple | None]:
        """{live firm: the firm it legally is (resolver.identity over its
        records' legal keys), or None}."""
        from harvey import resolver

        legal = set(self.schemes.legal_kinds())
        out = {}
        for f, idents in self.group_idents(list(live_ids)).items():
            ids: dict[str, set[str]] = {}
            for scheme, key in idents:
                s = self.schemes.named(scheme)
                if s is None:
                    continue
                for kind, value in self.schemes.kinds_of(s.id, key):
                    if kind in legal:
                        ids.setdefault(kind, set()).add(value)
            out[str(f)] = resolver.identity(ids) if ids else None
        return out

    def duplicate_groups_found(self) -> list[dict]:
        """resolver.groups on the overlay: every pending pair followed to the
        live firms it stands for now (gone when they are one since a merge,
        or a person said they differ, or they are two firms), joined into
        connected groups, most likely first, then largest."""
        rows = self.x("overlay.read.candidates_open").fetchall()
        if not rows:
            return []
        live = {str(k): str(v) for k, v in self.live_of({x for r in rows for x in r[:2]}).items()}
        apart = {frozenset((live[str(a)], live[str(b)])) for a, b, _, _, status in rows
                 if status == "separate" and str(a) in live and str(b) in live}
        edges: dict[frozenset, dict] = {}
        for a, b, score, reasons, status in rows:
            if status != "pending":
                continue
            a, b = live.get(str(a), ""), live.get(str(b), "")
            key = frozenset((a, b))
            if not a or not b or a == b or key in apart:
                continue
            lo, hi = sorted((a, b), key=int)
            edge = edges.setdefault(key, {"a": lo, "b": hi, "score": 0.0, "reasons": []})
            if (score or 0) > edge["score"]:
                edge["score"] = round(score or 0, 2)
            for r in self._reasons(reasons):
                if r not in edge["reasons"]:
                    edge["reasons"].append(r)
        if not edges:
            return []
        firm = self._firm_identity({int(x) for key in edges for x in key})
        parent: dict[str, str] = {}

        def root(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        kept = []
        for edge in edges.values():
            fa, fb = firm.get(edge["a"]), firm.get(edge["b"])
            if fa and fb and fa != fb:
                continue
            kept.append(edge)
            parent[root(edge["a"])] = root(edge["b"])
        out: dict[str, dict] = {}
        for edge in kept:
            group = out.setdefault(root(edge["a"]), {"members": set(), "pairs": [], "score": 0.0})
            group["members"].update((edge["a"], edge["b"]))
            group["pairs"].append(edge)
            group["score"] = max(group["score"], edge["score"])
        result = []
        for group in out.values():
            group["members"] = sorted(group["members"], key=int)
            group["pairs"].sort(key=lambda e: (-e["score"], int(e["a"]), int(e["b"])))
            result.append(group)
        result.sort(key=lambda g: (-g["score"], -len(g["members"]), int(g["members"][0])))
        return result

    def count_duplicates(self) -> int:
        return len(self.duplicate_groups_found())

    def duplicate_groups(self, limit: int = 100) -> tuple[int, list[dict]]:
        """StateManager.duplicate_groups on the overlay: (how many groups,
        the first ``limit``), each member as the panel shows it, the record
        to keep and, per member, whether it is another firm than the
        group's (``conflict``)."""
        from harvey.overlay.store import Store

        found = self.duplicate_groups_found()
        shown = found[:max(1, limit)]
        ids = [cid for g in shown for cid in g["members"]]
        sides = self.duplicate_sides(ids)
        firm = self._firm_identity({int(i) for i in ids})
        store = Store(self.con, manifests=self.manifests, registers=self.registers)
        for group in shown:
            order = store.keeper_order(group["members"])
            keeper = order[0] if order else group["members"][0]
            group["keeper"] = keeper
            reasons: list[dict] = []
            for pair in group["pairs"]:
                for r in pair["reasons"]:
                    if r.get("code") not in {x.get("code") for x in reasons}:
                        reasons.append(r)
            group["reasons"] = reasons
            group["key"] = ",".join(group["members"])
            members = order + [m for m in group["members"] if m not in order]
            anchor = firm.get(keeper) or next(
                (firm[cid] for cid in members if firm.get(cid)), None)
            group["members"] = [
                {**(sides.get(cid) or {"id": cid}), "keeper": cid == keeper,
                 "conflict": bool(firm.get(cid) and anchor and firm[cid] != anchor)}
                for cid in members]
        return len(found), shown

    def merges(self, limit: int = 50) -> list[dict]:
        """StateManager.merges on the overlay: merges in force, newest first,
        both sides as the panel shows them."""
        rows = [dict(zip(("id", "survivor", "merged", "score", "reasons", "decided_by",
                          "created_at", "group_id"), r))
                for r in self.x("overlay.read.merges", (int(limit),))]
        sides = self.duplicate_sides({x for r in rows for x in (r["survivor"], r["merged"])})
        for r in rows:
            r["survivor"], r["merged"] = str(r["survivor"]), str(r["merged"])
            r["reasons"] = self._reasons(r["reasons"])
            r["created_at"] = _db_time(r["created_at"])
            r["group_id"] = r["group_id"] or 0
            r["a"], r["b"] = sides.get(r["survivor"]), sides.get(r["merged"])
        return rows

def version(path) -> int:
    """The schema version of the database at ``path`` (0: none there)."""
    path = Path(path)
    if not path.is_file():
        return 0
    con = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
