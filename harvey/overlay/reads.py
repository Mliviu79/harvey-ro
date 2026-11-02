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
from harvey.overlay import schema
from harvey.overlay.idents import Schemes
from harvey.overlay.schema import EMAIL, PHONE, TRUST_NAMES

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

ORDERINGS = Snapshots()
SETS = Snapshots(kept=64, seconds=600)
# How long an ordering of every firm (no filter) is kept at most: it is
# dropped sooner when a firm is added or merged or a register reloaded.
LONG_SECONDS = 3600
# From this many firms a list sorts by name through the registry's own
# order (its ids), not by reading every name.
NAME_ORDER_FROM = 200_000


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
    what the Signals page and the landing page read. A job (a walk of
    every key per read: minutes on a whole registry), never a page.
    Returns how many rows were written."""
    con, attached = connect(path)
    try:
        rows = Reader(con, attached).count_registers(codes)
        con.execute("BEGIN IMMEDIATE")
        con.executemany("INSERT OR REPLACE INTO register_tally (signal, module, mark, loaded, "
                        "n_true, n_false, at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
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
    what it leaves out."""

    __slots__ = ("ids", "neg")

    def __init__(self, ids=(), neg: bool = False):
        self.ids = ids if isinstance(ids, set) else set(ids)
        self.neg = neg

    def __and__(self, other: "FirmSet") -> "FirmSet":
        if not self.neg and not other.neg:
            return FirmSet(self.ids & other.ids)
        if self.neg and other.neg:
            return FirmSet(self.ids | other.ids, True)
        pos, neg = (self, other) if not self.neg else (other, self)
        return FirmSet(pos.ids - neg.ids)

    def __or__(self, other: "FirmSet") -> "FirmSet":
        if not self.neg and not other.neg:
            return FirmSet(self.ids | other.ids)
        if self.neg and other.neg:
            return FirmSet(self.ids & other.ids, True)
        pos, neg = (self, other) if not self.neg else (other, self)
        return FirmSet(neg.ids - pos.ids, True)

    def __invert__(self) -> "FirmSet":
        return FirmSet(self.ids, not self.neg)

    def count(self, total: int) -> int:
        return total - len(self.ids) if self.neg else len(self.ids)


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
        self.key_scheme = self.directory.keyed_by if self.directory else ""
        self._enabled = set(enabled) if enabled is not None else None
        self._signal_ids: dict[str, int] | None = None
        # Each live firm's register key, as far as known (key_of), and the
        # firms known to have none (described locally).
        self.keys: dict[int, tuple[str, object]] = {}
        self._keyless: set[int] = set()
        self._modules: dict[str, int] | None = None

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
            out = self.live_of_keys(self.directory.resolve({"words": words}))
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
    def passes(row: dict, p: dict) -> bool:
        if p["site"] is not None and bool(row["website"] or row["domain"]) != bool(p["site"]):
            return False
        if p["contact"] is not None and (row["n_contacts"] > 0) != bool(p["contact"]):
            return False
        return not ((p["email"] and not row["n_email"]) or (p["phone"] and not row["n_phone"])
                    or (p["mobile"] and not row["n_mobile"]))

    @staticmethod
    def narrowed(p: dict) -> bool:
        return p["site"] is not None or p["contact"] is not None or bool(
            p["email"] or p["phone"] or p["mobile"])

    def firm_set(self, f, possible: bool = False) -> FirmSet | None:
        """The firms the filter's register, source and signal conditions
        allow (None: no such condition, every live firm); Harvey's own
        columns are ``predicates``. ``possible``: a cohort's unknowns pass
        too (the contact finder searches what isn't known yet)."""
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
        return [f for f in self.all_live() if f not in s.ids]

    def matching(self, f, possible: bool = False) -> list[int]:
        """Every live firm the filter selects (its register conditions,
        then Harvey's own columns), in no particular order."""
        s = self.firm_set(f, possible)
        p = self.predicates(f)
        ids = self.resolve(s)
        if not self.narrowed(p):
            return list(ids)
        rows = self.firm_rows(ids)
        return [i for i in ids if i in rows and self.passes(rows[i], p)]

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
                    ids = [i for i in ids if (i in other.ids) != other.neg]
        else:
            ids = list(self.resolve(s))
        if not self.narrowed(p):
            return list(ids)
        rows = self.firm_rows(ids)
        return [i for i in ids if i in rows and self.passes(rows[i], p)]

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
            if (p["site"] == 0 and p["contact"] is None
                    and not (p["email"] or p["phone"] or p["mobile"])):
                total = t.get("firms", 0) - t.get("firms_with_site", 0)
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
        return None

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
            local = next((locals_[m] for m in group if m in locals_), None)
            card = described.get(i, {}).get("card") or {}
            srcs = sorted(s for m in group for s in sources.get(m, []))
            key = dict(own).get(self.key_scheme) if self.key_scheme else None
            external = ""
            for scheme, k in own:
                s = self.schemes.named(scheme)
                if s is not None and s.kind == "external":
                    external = f"{s.prefix}{k}"
                    break
            main_phone = (local[5] if local and local[5] else "") or \
                next((p["value"] for p in phones.get(i, [])), "")
            out.append({
                "id": str(i),
                "name": r["display_name"] or card.get("name") or (local[1] if local else "") or "",
                "cui": str(key) if key is not None else "",
                "domain": r["domain"] or "", "website": r["website"] or "",
                "industry": (local[6] if local and local[6] else "") or card.get("industry", ""),
                "company_size": "",
                "location": (", ".join(p for p in (local[3], local[2]) if p) if local
                             else card.get("location", "")),
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
                                 "first": first or 0, "line_type": LINE_NAMES.get(row[4], "")}
        return {f: sorted(v.values(), key=lambda p: (p["trust"], p["first"]))
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
        if self.key_of(firms):
            found |= {a.module for a in self.registers if a.module in self.enabled}
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

    def prospects_by_id(self, ids) -> dict[str, dict]:
        """{prospect id: the outbox's fields of it, ``firm`` its record}."""
        out = {}
        for chunk in chunks(list(dict.fromkeys(ids))):
            cur = self.x("overlay.read.prospects_by_id", (json.dumps(chunk),))
            names = [c[0] for c in cur.description]
            out.update({r[0]: dict(zip(names, r)) for r in cur.fetchall()})
        return out

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
        for firm, contact, source, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[l[0], l[1]] for l in links], 500):
            evidence.setdefault((contact, person), set()).add(source)
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 list({l[3] for l in links if l[3]}))}
        merged: dict[tuple, dict] = {}
        for firm, contact, trust, person, location, first in links:
            row = rows.get(contact)
            if row is None:
                continue
            key = (contact, person)
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
        people = []
        searched = dict(self._by_ids("overlay.read.person_searched",
                                     [r[1] for r in self._by_ids("overlay.read.people_of", group)]))
        for firm, pid, name, norm, role, source, first in self._by_ids("overlay.read.people_of",
                                                                       group):
            people.append({"id": pid, "name": name, "role": role, "source": source or "",
                           "searched_at": _db_time(searched.get(pid)),
                           "reached": pid in linked_people, "_n": norm})
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
                             else named.get((module, sig), ""),
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
        from harvey.collectors.phones import phone_hit

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
                for number in str(r["value_text"] or "").split():
                    add(code, number, module, r["observed_at"])
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
                      cap: int = contacts.COUNT_CAP) -> tuple[int, bool, list[int]]:
        """(total up to ``cap``, more than that, the page's contact ids):
        the Contacts page on the overlay. A contact matches the company
        conditions when any live firm it reaches does."""
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

    def _contact_ids(self, f, s: FirmSet | None, p: dict) -> list[int]:
        """Every contact the filter keeps."""
        firms = None
        if s is not None or self.narrowed(p):
            firms = set(self.matching(f))
        kind_id = self._kind_id(f.kind)
        status = schema.CONTACT_STATUS.get(f.status) if f.status else None
        trust = contacts.TRUST_LEVELS.index(f.trust) if f.trust in contacts.TRUST_LEVELS else None
        if (f.kind and kind_id is None) or (f.status and status is None):
            return []           # a kind or status no contact has
        links = None
        candidates = None
        if firms is not None:
            groups = self.members(firms)
            live_of = {m: g for g, ms in groups.items() for m in ms}
            links = list(self._by_ids("overlay.read.links_of_firms", list(live_of)))
            candidates = {l[1] for l in links}
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
        for name, arg in narrow:
            hit = {r[0] for r in self.x(name, () if arg is None else (arg,))}
            candidates = hit if candidates is None else candidates & hit
        words = [w for w in (f.q or "")[:200].split()[:8]]
        for word in words:
            hit = self._word_contacts(word)
            candidates = hit if candidates is None else candidates & hit
        if candidates is None:
            candidates = {r[0] for r in self.x(
                "overlay.read.contacts.walk" if kind_id == PHONE else "overlay.contacts.new",
                ((PHONE, schema.MOBILE if f.kind == "mobile" else None,
                  schema.MOBILE if f.kind == "mobile" else None, 1 << 62, 0)
                 if kind_id == PHONE else (1 << 62, 0)))}
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", list(candidates))}
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
        if not (f.source or f.has_person or firms is not None):
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

    def _contacts_with_person(self, ids) -> set[int]:
        return {l[0] for l in self._by_ids("overlay.read.links_of_contacts", list(ids)) if l[3]}

    def _word_contacts(self, word: str) -> set[int]:
        """Contacts a word of the search box finds: in its value (a number
        however it is written), a firm it reaches (by name, through the
        register's search), or a person it reaches."""
        import re

        folded = contacts.fold(word)
        like = contacts.like(folded)
        found = {r[0] for r in self.x("overlay.read.contacts.value_like", (like,))}
        digits = re.sub(r"[\s+\-.()/]", "", word)
        if digits.isdigit() and len(digits) >= 3:
            forms = {digits}
            if digits.startswith("0") and not digits.startswith("00"):
                forms.add("40" + digits[1:])
            elif digits.startswith("00"):
                forms.add(digits[2:])
            for form in forms:
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

    def _contact_order(self, ids, sort: str) -> list[int]:
        """Contacts in the sort's descending order (ties by id)."""
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
                    cap: int = contacts.COUNT_CAP) -> tuple[int, bool, list[dict]]:
        """The people still to search: named at a live firm (a register's
        administrators and the people Harvey found, each name once per firm
        and its merged records) and reached by no contact of their own yet,
        newest firm first. The total stops at ``cap``: with no condition,
        when the register's count says the firms naming someone are more
        than that, only the page is read."""
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
            named = self.register_count("CONTACT_FOUND", self.directory.module)
            many = named is not None and named[0] > cap
        words = [contacts.fold(w) for w in (f.q or "")[:200].split()[:8]]
        counted = None
        if not many and plain and not words and not f.source and firms is not None:
            # Counted, not walked: the register's people of the whole set,
            # with the few firms Harvey found people at worked out one by one.
            counted = self._count_people(firms, linked)
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
        for batch in self._batches(walk, 5000):
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
    def _batches(it, size):
        batch = []
        for x in it:
            batch.append(x)
            if len(batch) >= size:
                yield batch
                batch = []
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

    def signal_vocabulary(self, status: str | None = None) -> list[dict]:
        """get_signal_codes on the overlay: the vocabulary with the module
        that declares each signal and whether a module that is on provides
        it (the manifests say; the database keeps the status)."""
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
            if status and r["status"] != status:
                continue
            module, sig = declared.get(r["code"], ("", {}))
            r.pop("id", None)
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
                        "status": vocab[code]["status"], "companies": st["true"],
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

    def register_count(self, code: str, module: str) -> tuple[int, int] | None:
        """(true, false) over the live firms a register reads ``code`` for,
        counted for its current load mark; None when not counted for it."""
        a = self.regs.get(module)
        sid, mid = self.signal_id(code), self.module_id(module)
        if a is None or sid is None or mid is None:
            return (0, 0) if a is None else None
        row = self.x("overlay.read.register_tally_of", (sid, mid)).fetchone()
        if row is None or row[4] is None or row[0] != (a.marks(self.con) or ""):
            return None
        return row[2] or 0, row[3] or 0

    def count_registers(self, codes=None) -> list[tuple]:
        """Every (signal, module) a loaded register reads, counted true /
        false over the firms in Harvey (one walk of the scheme per read):
        the rows register_tally keeps, per load mark. A job, never a page."""
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
                for (c, scheme), read in a.by_signal.items():
                    if c != code:
                        continue
                    s = self.schemes.named(scheme or a.keyed_by)
                    if s is None:
                        continue
                    for firm, value, date in a.held_values(self.con, code, s.id, scheme):
                        yes = self._truth(read, value)
                        per[firm] = per.get(firm, False) or yes
                lives: dict[int, bool] = {}
                for firm, live in self.live_of(per).items():
                    lives[live] = lives.get(live, False) or per[firm]
                n_true = sum(1 for v in lives.values() if v)
                out.append((sid, mid, mark, 1, n_true, len(lives) - n_true, now))
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
        out = set()
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
                    hit = self._lives(r[0] for r in self.con.execute(sql, (value, s.id)))
                    SETS.put(key, (), hit, LONG_SECONDS)
                out |= hit
        return FirmSet(out)

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
        key = ("unkeyed", scheme, self.marks())
        hit = SETS.get(key, ())
        if hit is None:
            hit = {r[0] for r in self.x("overlay.read.unkeyed", (s.id,))}
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
        return {merged.get(r, r) for r in records}

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
        return true, false

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
        if order is None:
            chosen = reduce(lambda a, b: a | b, (sets[v] for v in wanted))
            ids = list(self.resolve(chosen))
            if len(ids) > NAME_ORDER_FROM:
                # (The registry's own order: its names are upper case.)
                order = array("q", reversed(self._name_order(ids)))
            else:
                names = self.sort_keys(ids, "name")
                order = array("q", sorted(ids, key=lambda i: ((names.get(i) or "").lower(),
                                                               str(i))))
            ORDERINGS.put(key, mark, order)
        page = list(order[offset:] if limit is None else order[offset:offset + int(limit)])
        verdict_of = {}
        for v in wanted:
            s = sets[v]
            for i in page:
                if (i in s.ids) != s.neg:
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
                                          rows[i]["profiled_at"] or 0))[:int(limit)]
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
        people = t.get("people", 0)
        named = self.register_count("CONTACT_FOUND", self.directory.module) \
            if self.directory else None
        activity = self.register_count("ACTIVITY_CODE", self.directory.module) \
            if self.directory else None
        return {"companies": t.get("firms", 0), "with_phone": phones,
                "with_email": len(email), "with_both": with_both,
                "people": people, "people_firms": named[0] if named else None,
                "with_activity": activity[0] if activity else None}

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

    def next_paid(self, limit: int, searchers) -> list[str]:
        """finder.next_paid on the overlay: firms with no contact and no
        site, named, that a search module able to find them hasn't searched
        yet, newest first."""
        if not searchers:
            return []
        out: list[str] = []
        at = 0
        caps = []
        for s in searchers:
            mid, cid = self.module_id(s.module), self.capability_id(s.module, s.capability)
            caps.append((mid, cid, self._cutoff(s.module, s.capability)))
        while len(out) < limit:
            batch = [r[0] for r in self.x("overlay.read.next_paid", (1000, at))]
            if not batch:
                break
            at += len(batch)
            left = set(batch)
            done_by_all = set(batch)
            for mid, cid, cut in caps:
                if mid is None or cid is None:
                    done_by_all = set()
                    break
                done = {r[0] for r in self.x("overlay.read.searched_by",
                                             (json.dumps(batch), mid, cid, cut))}
                done_by_all &= done
            left -= done_by_all
            named = self.sort_keys([b for b in batch if b in left], "name")
            out += [str(b) for b in batch if b in left and named.get(b)][:limit - len(out)]
        return out

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
        rows = self.firm_rows(found)
        if not force:
            found = [i for i in found if i in rows and rows[i]["n_contacts"] == 0]
        words = [contacts.fold(w) for w in (f.q or "")[:200].split()[:8]]
        if words:
            names = self.sort_keys(found, "name")
            found = [i for i in found
                     if all(w in contacts.name_norm(names.get(i, "")) for w in words)]
        found.sort(key=lambda i: (rows[i]["created"], i) if i in rows else (0, i), reverse=True)
        return [{"id": str(i), "site": bool(rows[i]["website"] or rows[i]["domain"])}
                for i in found[:limit] if i in rows]

    def email_audience(self, limit: int = 200) -> list[dict]:
        """StateManager.email_audience on the overlay: email contacts the
        Email module can write to and has no prospect for yet (verified,
        catch-all or not checked; not do-not-contact), each with one link to
        a live firm trusted confirmed or likely (through a person first,
        then the surest, oldest): its company, person and source."""
        ok = {schema.CONTACT_STATUS[s] for s in ("verified", "risky", "unchecked")}
        ids = [r[0] for r in self.x("overlay.read.contacts.of_kind", (EMAIL,))]
        rows = {r[0]: r for r in self._by_ids("overlay.read.contact_rows", ids)}
        cands = [c for c in sorted(ids) if c in rows and not rows[c][6] and rows[c][5] in ok]
        taken = {r[0] for r in self._by_ids("overlay.read.prospected_emails",
                                            [rows[c][2] for c in cands])}
        cands = [c for c in cands if rows[c][2] not in taken]
        cands.sort(key=lambda c: (rows[c][5] == schema.CONTACT_STATUS["unchecked"], c))
        links = list(self._by_ids("overlay.read.links_of_contacts", cands))
        lives = self.live_of([l[1] for l in links])
        best: dict[int, tuple] = {}
        for contact, firm, trust, person, location, first in links:
            if trust > schema.TRUST["likely"] or firm not in lives:
                continue
            key = (person == 0, trust != 0, first or 0, firm)
            if contact not in best or key < best[contact][0]:
                best[contact] = (key, firm, trust, person)
        chosen = [c for c in cands if c in best][:int(limit)]
        cards = {r["id"]: r for r in self.company_rows(
            list(dict.fromkeys(lives[best[c][1]] for c in chosen)))}
        persons = {r[0]: r for r in self._by_ids("overlay.read.persons",
                                                 [best[c][3] for c in chosen if best[c][3]])}
        evidence = {}
        for firm, contact, src, trust, url, first, person, location in self._by_ids(
                "overlay.read.evidence", [[best[c][1], c] for c in chosen], 500):
            evidence.setdefault((firm, contact), (src, url))
        out = []
        for c in chosen:
            _, firm, trust, person = best[c]
            card = cards.get(str(lives[firm]), {})
            src, url = evidence.get((firm, c), ("", ""))
            p = persons.get(person)
            out.append({"contact_id": c, "email": rows[c][2],
                        "status": STATUS_NAMES.get(rows[c][5], ""), "company_id": card.get("id"),
                        "source": src or "", "evidence_url": url or "",
                        "trust": TRUST_NAMES.get(trust, ""), "person": p[2] if p else "",
                        "role": (p[4] or "") if p else "", "person_source": (p[5] or "") if p else "",
                        "company": card.get("name", ""), "industry": card.get("industry", ""),
                        "domain": card.get("domain", "")})
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
