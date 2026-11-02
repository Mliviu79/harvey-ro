"""Harvey's writes on the overlay (schema 25).

The same writes the StateManager and CompanyBatch make on schema 24, by
the same names (ingest, the contact finder and the runs call them without
knowing the schema): a firm found or pulled, the gaps of one on file, its
contacts and the evidence for each, people, searches, sources, places, web
signals, merges and their undo, and removing a firm from Harvey. They run
on one connection inside the caller's transaction (``call`` opens one per
call; ``Batch`` keeps one for many), so a row-at-a-time write keeps every
count through the triggers, and a bulk one (a registry pull, a merge, a
removal) recounts what it touched (triggers.bulk).

Ids. A firm is Harvey's own integer id; every caller passes and gets back
company ids as text, so a firm id travels as its digits. An old text id
(still named by a queued job, an action or a setting) is read through
``legacy_id``.

Where a row goes. A fact, a contact's evidence, a person, a place and a
source belong to the record they were found for (``origin``, else the
company given): a merge moves no rows, and undoing it takes back exactly
what came with the record. Kept values count on the live firm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from harvey import contacts, resolver
from harvey.overlay import merges, schema, triggers
from harvey.overlay.idents import Schemes
from harvey.overlay.schema import CONTACT_STATUS, FIRM_KEY, LINE_TYPE, PHONE, TRUST

logger = logging.getLogger("harvey.overlay")

# The company columns a fill may write (StateManager.fill_company_fields).
FILL_COLUMNS = frozenset({"name", "domain", "website", "industry", "location", "phone", "cui",
                          "address"})
# Of those, the ones only a firm no register describes keeps (a register is
# the authority on its own firms' name, place and activity).
LOCAL_COLUMNS = ("name", "industry", "address")
# Registry pull: records per bulk write. The records are matched first, in
# a read transaction (matching is most of a pull's time), so the write
# lock is held only for a chunk's writes and its recount; small enough that
# the finder's and the pages' writes wait seconds, not minutes. A pull cut
# short keeps the chunks already committed.
PULL_CHUNK = 5_000


class RemoveRefused(ValueError):
    """Removing a firm from Harvey can't be done. ``code``: not_found,
    merged (it is part of a merge, undone or not: its history needs it),
    has_prospects (outreach names it)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def unix(value) -> int | None:
    """A timestamp as unix seconds: a number as it is, text as SQLite or
    ISO writes it (UTC when it says no zone); None for none."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def place_parts(location: str) -> tuple[str, str, str]:
    """(locality, county, country) of a place line as a source writes it:
    "Town, County, Country", "Town, Country" (a country named last is
    never a county), "Town, County" or a town alone."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    locality = parts[0] if parts else ""
    country_last = len(parts) >= 2 and (len(parts) >= 3 or contacts._country(parts[-1]))
    county = parts[1] if len(parts) >= 2 and not (len(parts) == 2 and
                                                   contacts._country(parts[1])) else ""
    return locality, county, parts[-1] if country_last else ""


# ── Connections ──

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


def connect(path, *, data_dir=None) -> tuple[sqlite3.Connection, list]:
    """A connection to the overlay at ``path`` (autocommit: the caller
    opens its transactions), with every register that is loaded attached
    read-only (from ``data_dir``, by default the database's folder).
    Returns (connection, the adapters attached)."""
    from harvey import registers

    path = Path(path).resolve()
    con = sqlite3.connect(f"file:{quote(str(path))}", uri=True, isolation_level=None,
                          timeout=30, check_same_thread=False)
    con.execute("PRAGMA synchronous=NORMAL")
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
    return con, attached


# Store methods that only read: run without taking the write lock.
READS = frozenset({"match_company", "companies_with_id", "held", "signal_codes",
                   "keeper_order", "observed", "module_readings", "module_readings_of",
                   "dictionaries_stale", "match_records"})


def call(path, method: str, *args, **kwargs):
    """``Store(...).method(*args, **kwargs)`` in one transaction of its own
    (a write transaction unless it only reads; committed, or rolled back
    when it raises)."""
    con, attached = connect(path)
    try:
        store = Store(con, registers=attached)
        con.execute("BEGIN" if method in READS else "BEGIN IMMEDIATE")
        try:
            out = getattr(store, method)(*args, **kwargs)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        return out
    finally:
        con.close()


class Batch:
    """A Store on one connection and one transaction (StateManager.batch()
    on the overlay), its methods awaited like CompanyBatch's: each runs in
    a worker thread, one at a time."""

    def __init__(self, store: "Store"):
        self._store = store

    def __getattr__(self, name):
        method = getattr(self._store, name)

        async def run(*args, **kwargs):
            return await asyncio.to_thread(method, *args, **kwargs)
        return run


# ── The writer ──

class Store:
    def __init__(self, con: sqlite3.Connection, *, manifests=None, registers=()):
        from harvey.modules import switches

        self.con = con
        self.manifests = list(manifests if manifests is not None else switches.manifests())
        self.schemes = Schemes.load(con, self.manifests)
        self.registers = list(registers)
        self._signals: dict[str, tuple[int, bool]] | None = None
        self._stored: dict[int, set[int]] | None = None
        self._capnames: dict[str, str] | None = None

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.con.execute(sql, params)

    def one(self, sql: str, params=()):
        row = self.con.execute(sql, params).fetchone()
        return row[0] if row else None

    # Ids

    def firm(self, cid) -> int | None:
        """The firm a company id names (its digits, or an old text id), or
        None when there is no such firm."""
        if cid is None or cid == "":
            return None
        text = str(cid).strip()
        # (More digits than an integer column holds names no firm.)
        if text.isdigit() and len(text) <= 18:
            found = self.one("SELECT id FROM firm WHERE id = ?", (int(text),))
            if found is not None or len(text) != 12:
                return found
            # (An old id can be all digits too.)
        return self.one("SELECT l.firm FROM legacy_id l JOIN firm f ON f.id = l.firm "
                        "WHERE l.uuid = ?", (text,))

    def live(self, cid) -> int | None:
        """The live firm a company id stands for now."""
        fid = self.firm(cid)
        if fid is None:
            return None
        return self.one("SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm WHERE id = ?",
                        (fid,))

    def _record(self, company_id, origin="") -> int | None:
        """The record a finding belongs to: the one it was found for, else
        the company given."""
        return (self.firm(origin) if origin else None) or self.firm(company_id)

    def _group(self, live: int) -> list[int]:
        return merges.members(self.con, live)

    def _described(self, fid: int) -> bool:
        """Whether a register describes the firm (it holds a register's key)."""
        ids = self.schemes.register_ids()
        if not ids:
            return False
        marks = ",".join(str(i) for i in ids)
        return self.one(f"SELECT 1 FROM firm_ident WHERE firm = ? AND scheme IN ({marks}) "
                        f"LIMIT 1", (fid,)) is not None

    # Dictionaries

    def source(self, name: str) -> int:
        return schema.dict_id(self.con, "source", name or "unknown")

    def capability(self, module: str, name: str) -> int | None:
        """A capability's id by its name: the one a manifest declares under
        that name, else one of ``module`` (a source that isn't a module
        keeps its own)."""
        if not name:
            return None
        if self._capnames is None:
            self._capnames = {}
            for m in self.manifests:
                for cap in m.capabilities:
                    self._capnames.setdefault(cap["id"], m.id)
        return schema.capability_id(self.con, self._capnames.get(name) or module or "unknown",
                                    name)

    def dictionaries_stale(self) -> bool:
        """Whether sync_dictionaries has anything to write (read first: a
        start, and every page that opens the database, takes no write lock
        when nothing changed)."""
        modules = {r[0] for r in self.x("SELECT name FROM module")}
        caps = set(self.x("SELECT m.name, c.name FROM capability c JOIN module m "
                          "ON m.id = c.module").fetchall())
        want_caps = {(m.id, cap["id"]) for m in self.manifests for cap in m.capabilities}
        want_caps |= {("website", "read_site"), ("contact_finder", "find_contacts")}
        if {m.id for m in self.manifests} - modules or want_caps - caps:
            return True
        schemes = {name: (mod, numeric, legal) for name, mod, numeric, legal in self.x(
            "SELECT s.name, m.name, s.numeric, s.legal FROM ident_scheme s JOIN module m "
            "ON m.id = s.module")}
        # (As sync_identifiers writes them: a later manifest's wins.)
        want = {ident["name"]: (m.id, int(ident["numeric"]), int(ident["legal"]))
                for m in self.manifests for ident in m.identifiers}
        if any(schemes.get(name) != value for name, value in want.items()):
            return True
        kinds = {r[0] for r in self.x("SELECT kind FROM contact_kinds")}
        if any(kind not in kinds for _, kind, _, _ in schema.CORE_KINDS):
            return True
        if self.one("SELECT bit FROM looked_for WHERE name = 'owner'") is None:
            return True
        roles = dict(self.x("SELECT name, id FROM role").fetchall())
        cap_id = lambda mod, name: self.one(  # noqa: E731
            "SELECT c.id FROM capability c JOIN module m ON m.id = c.module "
            "WHERE m.name = ? AND c.name = ?", (mod, name))
        return (roles.get("site_read") != cap_id("website", "read_site")
                or roles.get("finder") != cap_id("contact_finder", "find_contacts")
                or roles.get("deep_bit") != self.one("SELECT bit FROM looked_for "
                                                     "WHERE name = 'owner'"))

    def sync_dictionaries(self) -> None:
        """Modules, their capabilities and identifier schemes, the core
        contact kinds and the roles the kept values recognise (a fresh
        database, or one a module was added to since)."""
        for m in self.manifests:
            schema.dict_id(self.con, "module", m.id)
            for cap in m.capabilities:
                schema.capability_id(self.con, m.id, cap["id"])
        schema.sync_identifiers(self.con, self.manifests)
        for kid, kind, label, channel in schema.CORE_KINDS:
            self.x("INSERT OR IGNORE INTO contact_kinds (id, kind, label, channel) "
                   "VALUES (?, ?, ?, ?)", (kid, kind, label, channel))
        roles = dict(self.x("SELECT name, id FROM role").fetchall())
        want = {"site_read": schema.capability_id(self.con, "website", "read_site"),
                "finder": schema.capability_id(self.con, "contact_finder", "find_contacts"),
                "deep_bit": schema.looked_for_bit(self.con, "owner")}
        for name, value in want.items():
            if roles.get(name) != value:
                schema.set_role(self.con, name, value)
        self.schemes = Schemes.load(self.con, self.manifests)

    # ── The vocabulary ──

    def write_vocabulary(self, rows_for, settings_prefix: str) -> int:
        """StateManager.write_vocabulary on the overlay: every declared
        signal upserted, its confirmation (``status``) kept unless a row
        names one, then who provides each (signal_source)."""
        settings = dict(self.x("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                               (len(settings_prefix), settings_prefix)).fetchall())
        rows = rows_for(settings)
        cols = ("label", "description", "category", "value_type", "unit", "cardinality",
                "keyed_by", "when_known", "cost_note", "confidence_floor")
        for row in rows:
            values = [row.get(c) for c in cols]
            self.x(f"INSERT INTO signal (code, {', '.join(cols)}, status) "
                   f"VALUES (?, {', '.join('?' * len(cols))}, COALESCE(?, 'proposed')) "
                   f"ON CONFLICT (code) DO UPDATE SET "
                   f"{', '.join(f'{c} = excluded.{c}' for c in cols)}, "
                   f"status = COALESCE(?, signal.status)",
                   (row["code"], *values, row.get("status"), row.get("status")))
        schema.sync_signals(self.con, (), self.manifests)
        self._signals = self._stored = None
        return len(rows)

    def write_contact_kinds(self, rows_for, settings_prefix: str) -> None:
        settings = dict(self.x("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                               (len(settings_prefix), settings_prefix)).fetchall())
        self.con.executemany(
            "INSERT INTO contact_kinds (kind, label, channel, module, available) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind) DO UPDATE SET label = excluded.label, "
            "channel = excluded.channel, module = excluded.module, "
            "available = excluded.available",
            [(k["kind"], k.get("label", k["kind"]), k.get("channel", ""), k.get("module", ""),
              1 if k.get("available", True) else 0) for k in rows_for(settings)])

    def signal_codes(self, status: str | None = None) -> list[str]:
        if status:
            return [r[0] for r in self.x("SELECT code FROM signal WHERE status = ?", (status,))]
        return [r[0] for r in self.x("SELECT code FROM signal")]

    def set_signal_status(self, code: str, status: str) -> bool:
        return self.x("UPDATE signal SET status = ? WHERE code = ?", (status, code)).rowcount > 0

    # ── Matching and saving a firm ──

    def match_company(self, company, extra_ids=(), declared_domain: bool = False):
        return resolver.match_firm(self.con, company, extra_ids, declared_domain,
                                   schemes=self.schemes, registers=self.registers)

    def save_company(self, company, match=None, extra_ids=(), batch: str = ""):
        """_save_company on the overlay: the same record gains the keys it
        lacked; a new firm is created (and merged into the one it surely
        is, or put up for review against a possible one). ``batch``: the
        run its source is recorded under."""
        from harvey.state import Resolution

        if match is None:
            match = self.match_company(company, extra_ids)
        if match.attach and match.same_record:
            record = self.firm(match.origin) or self.firm(match.company_id)
            self._add_keys(record, self._own_keys(company, match), company.source, linked=True)
            return Resolution(match.company_id, False, match)
        fid, created = self._new_firm(company, extra_ids, batch)
        if not created:
            # A key the match didn't see: that record it is.
            return Resolution(str(self.live(fid)), False, match)
        if match.attach:
            try:
                merges.merge(self.con, int(match.company_id), fid, score=match.score,
                             reasons=json.dumps(match.reasons), decided_by="auto")
                return Resolution(match.company_id, False, match, merged_from=str(fid))
            except merges.MergeRefused:
                return Resolution(str(fid), True, match)
        if match.review:
            self._candidate(int(match.company_id), fid, match.score, match.reasons)
        for other, score, reasons in match.others:
            self._candidate(int(other), fid, score, reasons)
        return Resolution(str(fid), True, match)

    def add_company(self, company) -> str:
        """A firm saved as it is, no resolver (StateManager.add_company): a
        new one, or the one already holding one of its keys."""
        return str(self.live(self._new_firm(company)[0]))

    @staticmethod
    def _own_keys(company, match) -> list[tuple[str, str]]:
        return [(kind, value) for kind, value in match.new_ids
                if kind != "domain" or value == _norm(company.domain)]

    def register_ids(self, company_id, company, match) -> None:
        keys = self._own_keys(company, match)
        if keys:
            self._add_keys(self.firm(match.origin) or self.firm(company_id), keys,
                           company.source, linked=True)

    def _new_firm(self, company, extra_ids=(), batch: str = "") -> tuple[int, bool]:
        """(firm id, True) for a new firm, or (the firm holding one of its
        own keys, False)."""
        now = int(time.time())
        keys = []
        for kind, value in resolver.identifiers(company, extra_ids):
            key = self.schemes.key(kind, value) if kind in ("cui", "external") else None
            if key is not None and key not in keys:
                keys.append(key)
        domain = _norm(company.domain) or None
        for scheme, key in keys:
            holder = self.one("SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?",
                              (scheme, key))
            if holder is not None:
                return holder, False
        if domain:
            holder = self.one("SELECT id FROM firm WHERE domain = ?", (domain,))
            if holder is not None:
                return holder, False
        source = self.source(company.source) if company.source else None
        fid = self.x("INSERT INTO firm (created, website, domain, website_source) "
                     "VALUES (?, ?, ?, ?)",
                     (unix(getattr(company, "created_at", None)) or now,
                      company.website or None, domain,
                      source if company.website else None)).lastrowid
        self.con.executemany("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, ?)",
                             [(s, k, fid, now) for s, k in keys])
        described = bool({s for s, _ in keys} & self.schemes.register_ids())
        if not described:
            locality, county, _ = place_parts(company.location or "")
            self.x("INSERT INTO local_firm (firm, name, county, locality, address, phone, industry, "
                   "description, source_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (fid, company.name or None, county or None, locality or None,
                    company.address or None, company.phone or None, company.industry or None,
                    company.description or None, company.source_url or None))
            self._name_key(fid, company.name, company.location, source)
        if company.source:
            self.add_company_source(str(fid), company.source, company.external_id or "",
                                    batch=batch)
        if company.phone:
            from harvey.collectors.phones import phone_region

            self._contact(fid, contacts.Found(
                "phone", company.phone, source=company.source or "", trust="confirmed",
                region=phone_region(company.location or "", None, domain or "")))
        return fid, True

    def _name_key(self, fid: int, name: str, location: str, source: int | None) -> None:
        key, town = resolver.name_key(name or ""), resolver.locality_key(location or "")
        if key and town:
            self.x("INSERT OR IGNORE INTO firm_key (kind, value, firm, source) VALUES (?, ?, ?, ?)",
                   (FIRM_KEY["name"], f"{key}@{town}", fid, source))

    def _add_keys(self, record: int | None, keys, source: str = "", *,
                  linked: bool = False) -> list[str]:
        """Keys a record on file gains. An identifier another firm holds
        stays where it is: for a legal one, the two are merged first when
        the source links them (``linked``: the same register record by
        another of its keys), else put up for review. A legal key nobody
        holds is refused when the record's firm has another key of that
        scheme already (a second tax code is another firm, not a gap).
        Returns the kinds added to the record."""
        if record is None:
            return []
        added = []
        now = int(time.time())
        src = self.source(source) if source else None
        legal = self.schemes.legal_ids()
        for kind, value in keys:
            if kind in ("cui", "external", "reg"):
                key = self.schemes.key(kind, value)
                if key is None:
                    continue
                holder = self.one("SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?", key)
                if holder is None:
                    if key[0] in legal and self._holds_other(record, *key):
                        logger.info("firm %s already holds another %s: %s %s not added",
                                    record, self.schemes.by_id[key[0]].name, kind, value)
                        continue
                    self.x("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, ?)",
                           (*key, record, now))
                    added.append(kind)
                    continue
                mine, theirs = self.live(record), self.live(holder)
                if key[0] in legal and mine != theirs:
                    if linked:
                        try:
                            merges.merge(self.con, theirs, mine, reasons=json.dumps(
                                [{"code": kind, "value": value}]), decided_by="auto")
                            continue
                        except merges.MergeRefused:
                            pass
                    self._candidate(theirs, mine, resolver.REVIEW_CAP,
                                    [{"code": kind, "value": value}])
            elif kind == "domain":
                taken = self.one("SELECT id FROM firm WHERE domain = ?", (value,))
                if taken is None and self.x("UPDATE firm SET domain = ? WHERE id = ? "
                                            "AND domain IS NULL", (value, record)).rowcount:
                    added.append(kind)
                elif taken != record:
                    self.x("INSERT OR IGNORE INTO firm_key (kind, value, firm, source) "
                           "VALUES (?, ?, ?, ?)", (FIRM_KEY["domain"], value, record, src))
            elif kind == "email":
                self.x("INSERT OR IGNORE INTO firm_key (kind, value, firm, source) "
                       "VALUES (?, ?, ?, ?)", (FIRM_KEY["email"], value, record, src))
            elif kind == "name" and not self._described(record):
                self.x("INSERT OR IGNORE INTO firm_key (kind, value, firm, source) "
                       "VALUES (?, ?, ?, ?)", (FIRM_KEY["name"], value, record, src))
        return added

    def _holds_other(self, record: int, scheme: int, key) -> bool:
        """Whether the firm ``record`` belongs to holds a key of ``scheme``
        other than ``key`` (on any of its records)."""
        group = self._group(self.live(record))
        return self.one(f"SELECT 1 FROM firm_ident WHERE firm IN ({','.join('?' * len(group))}) "
                        f"AND scheme = ? AND key != ? LIMIT 1", (*group, scheme, key)) is not None

    def _candidate(self, a: int, b: int, score: float, reasons) -> bool:
        """"Is B the same company as A?", once per pair either way round."""
        if a == b or self.one("SELECT 1 FROM match_candidate WHERE (a = ? AND b = ?) "
                              "OR (a = ? AND b = ?)", (a, b, b, a)):
            return False
        self.x("INSERT INTO match_candidate (a, b, score, reasons, status, at) "
               "VALUES (?, ?, ?, ?, 'pending', ?)",
               (a, b, score, json.dumps(reasons or []), int(time.time())))
        return True

    def _decide(self, a: int, b: int, status: str) -> None:
        now = int(time.time())
        if not self.x("UPDATE match_candidate SET status = ?, decided_at = ? "
                      "WHERE (a = ? AND b = ?) OR (a = ? AND b = ?)",
                      (status, now, a, b, b, a)).rowcount:
            self.x("INSERT OR IGNORE INTO match_candidate (a, b, status, at, decided_at) "
                   "VALUES (?, ?, ?, ?, ?)", (a, b, status, now, now))

    # ── Gaps of a firm on file ──

    def fill_company_fields(self, company_id, **fields) -> list[str]:
        """fill_company_fields on the overlay: its website, domain (a key
        no other firm holds), phone (a contact), CUI (an identifier), and
        for a firm no register describes its own name, activity, place and
        address, each only where it has none (the address always
        refreshes). The columns that changed."""
        fid = self.live(company_id)
        if fid is None:
            return []
        changed = []
        local = self.one("SELECT 1 FROM local_firm WHERE firm = ?", (fid,)) is not None
        for column, value in fields.items():
            if column not in FILL_COLUMNS:
                raise ValueError(f"not a fillable company column: {column}")
            if not value:
                continue
            if column == "website":
                if self.x("UPDATE firm SET website = ? WHERE id = ? AND website IS NULL",
                          (value, fid)).rowcount:
                    changed.append(column)
            elif column == "domain":
                value = _norm(value)
                if self.one("SELECT 1 FROM firm WHERE domain = ? AND id != ?", (value, fid)):
                    continue
                if self.x("UPDATE firm SET domain = ? WHERE id = ? AND domain IS NULL",
                          (value, fid)).rowcount:
                    changed.append(column)
            elif column == "phone":
                if self.fill_company_phone(str(fid), value):
                    changed.append(column)
            elif column == "cui":
                if "cui" in self._add_keys(fid, [("cui", resolver.cui_key(value))]):
                    changed.append(column)
            elif column == "location" and local:
                locality, county, _ = place_parts(value)
                if self.x("UPDATE local_firm SET locality = ?, county = ? WHERE firm = ? "
                          "AND locality IS NULL AND county IS NULL",
                          (locality or None, county or None, fid)).rowcount:
                    changed.append(column)
            elif column in LOCAL_COLUMNS and local:
                only_empty = "" if column == "address" else f" AND {column} IS NULL"
                if self.x(f"UPDATE local_firm SET {column} = ? WHERE firm = ? "
                          f"AND {column} IS NOT ?{only_empty}", (value, fid, value)).rowcount:
                    changed.append(column)
        if local and {"name", "location"} & set(changed):
            name, locality, county = self.x("SELECT name, locality, county FROM local_firm "
                                            "WHERE firm = ?", (fid,)).fetchone()
            self._name_key(fid, name, ", ".join(p for p in (locality, county) if p), None)
        return changed

    def _place(self, fid: int) -> str:
        row = self.x("SELECT locality, county FROM local_firm WHERE firm = ?", (fid,)).fetchone()
        return ", ".join(p for p in (row or ()) if p)

    def _has_phone(self, live: int) -> bool:
        if not triggers.active(self.con):
            return self.one("SELECT n_phone FROM firm WHERE id = ?", (live,)) > 0
        # In a bulk write the counts wait for its recount: the evidence.
        group = self._group(live)
        return self.one(f"SELECT 1 FROM link_evidence e JOIN contact c ON c.id = e.contact "
                        f"WHERE e.firm IN ({','.join('?' * len(group))}) AND c.kind = ? LIMIT 1",
                        (*group, PHONE)) is not None

    def fill_company_phone(self, company_id, phone: str) -> bool:
        """A phone as the firm's own when it has none yet: kept as its
        contact (trust confirmed). True when it was its first phone."""
        fid = self.live(company_id)
        if fid is None or not phone:
            return False
        if self._has_phone(fid):
            return False
        from harvey.collectors.phones import phone_region

        domain = self.one("SELECT domain FROM firm WHERE id = ?", (fid,)) or ""
        source = self.one("SELECT s.name FROM firm_source fs JOIN source s ON s.id = fs.source "
                          "WHERE fs.firm = ? ORDER BY fs.first_seen LIMIT 1", (fid,)) or ""
        return self._contact(fid, contacts.Found(
            "phone", phone, source=source, trust="confirmed",
            region=phone_region(self._place(fid), None, domain))) is not None

    def fill_company_phones(self, phones: dict) -> list[str]:
        """fill_company_phone for many firms; the ids whose first phone it was."""
        return [cid for cid, phone in phones.items() if self.fill_company_phone(cid, phone)]

    def set_company_activities(self, company_id, classes, source: str = "",
                               confidence: float = 1.0) -> int:
        """Nothing: what a registered firm does is read from its register."""
        return 0

    def set_found_website(self, company_id, website: str, *, domain: str = "",
                          trust: str = "unconfirmed", source: str = "") -> bool:
        fid = self.live(company_id)
        if fid is None:
            return False
        domain = _norm(domain)
        if domain and self.one("SELECT 1 FROM firm WHERE domain = ? AND id != ?", (domain, fid)):
            domain = ""
        return self.x(
            "UPDATE firm SET website = ?, website_trust = ?, website_source = ?, "
            "domain = COALESCE(domain, ?) WHERE id = ? AND website IS NULL AND domain IS NULL",
            (website, TRUST.get(trust, TRUST["unconfirmed"]), self.source(source),
             domain or None, fid)).rowcount > 0

    def mark_company_profiled(self, company_id) -> None:
        fid = self.live(company_id)
        if fid is not None:
            self.x("UPDATE firm SET profiled_at = ? WHERE id = ?", (int(time.time()), fid))

    def rename_company(self, company_id, name: str) -> None:
        """The name a firm's own site gives it: its local name, or for a
        firm a register describes Harvey's own name for it (the register's
        stays as it is). Its prospects carry the copy."""
        fid = self.live(company_id)
        if fid is None:
            return
        if not self.x("UPDATE local_firm SET name = ? WHERE firm = ?", (name, fid)).rowcount:
            self.x("UPDATE firm SET display_name = ? WHERE id = ?", (name, fid))
        group = self._group(fid)
        self.x(f"UPDATE prospects SET company = ?, updated_at = CURRENT_TIMESTAMP "
               f"WHERE firm IN ({','.join('?' * len(group))})", (name, *group))

    # ── Contacts, people, places, sources ──

    def add_contacts(self, company_id, found, origin: str = "") -> int:
        record = self._record(company_id, origin)
        if record is None:
            return 0
        return sum(self._contact(record, f) is not None for f in found or ())

    def add_contact_list(self, items) -> int:
        """Contacts of many firms: (company_id, contacts.Found, origin) each."""
        return sum(self.add_contacts(cid, [found], origin) for cid, found, origin in items)

    def _kind(self, kind: str) -> int | None:
        return self.one("SELECT id FROM contact_kinds WHERE kind = ?", (kind,))

    def _contact(self, record: int, found) -> int | None:
        """Save a contact and the evidence that ``record`` has it (its link
        follows, kept by the triggers). None when the value isn't one, or
        its kind isn't declared."""
        parts = contacts.normalised(found)
        if parts is None:
            return None
        kind = self._kind(found.kind)
        if kind is None:
            return None
        value, display, line, status = parts
        seen = unix(found.seen) or int(time.time())
        line_type = LINE_TYPE.get(line or "unknown", 0) if found.kind == "phone" else None
        contact = self.one(
            "INSERT INTO contact (kind, value, display, line_type, status, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (kind, value) DO UPDATE SET "
            "display = COALESCE(contact.display, excluded.display), "
            "line_type = CASE WHEN COALESCE(excluded.line_type, 0) != 0 THEN excluded.line_type "
            "ELSE contact.line_type END, "
            "status = CASE WHEN COALESCE(excluded.status, 0) != 0 THEN excluded.status "
            "ELSE contact.status END, "
            "first_seen = MIN(contact.first_seen, excluded.first_seen), "
            "last_seen = MAX(contact.last_seen, excluded.last_seen) RETURNING id",
            (kind, value, display or None, line_type, CONTACT_STATUS.get(status), seen, seen))
        person = self._person(record, found.person, found.role, found.source, seen) \
            if found.person else 0
        trust = found.trust if found.trust in contacts.TRUST_LEVELS \
            else contacts.trust_for(found.confidence)
        self.x("""
            INSERT INTO link_evidence (firm, contact, source, person, location, capability, trust,
                                       confidence, evidence_url, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (firm, contact, source, person) DO UPDATE SET
                location = CASE WHEN location = 0 THEN excluded.location ELSE location END,
                capability = COALESCE(capability, excluded.capability),
                evidence_url = COALESCE(evidence_url, excluded.evidence_url),
                trust = MIN(trust, excluded.trust),
                confidence = MAX(COALESCE(confidence, 0), COALESCE(excluded.confidence, 0)),
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)""",
               (record, contact, self.source(found.source), person, found.location_id or 0,
                self.capability(found.source, found.capability), TRUST[trust],
                float(found.confidence), found.evidence_url or None, seen, seen))
        return contact

    def _person(self, record: int, name: str, role: str = "", source: str = "",
                seen: int | None = None) -> int:
        name = (name or "").strip()
        key = contacts.name_norm(name)
        if not key:
            return 0
        seen = seen or int(time.time())
        self.x("INSERT INTO person (firm, name, name_norm, role, source, first_seen, last_seen) "
               "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (firm, name_norm) DO UPDATE SET "
               "role = COALESCE(person.role, excluded.role), "
               "first_seen = MIN(COALESCE(person.first_seen, excluded.first_seen), "
               "excluded.first_seen), "
               "last_seen = MAX(COALESCE(person.last_seen, excluded.last_seen), "
               "excluded.last_seen)",
               (record, name, key, role or None, self.source(source) if source else None,
                seen, seen))
        return self.one("SELECT id FROM person WHERE firm = ? AND name_norm = ?", (record, key))

    def add_people(self, company_id, people, *, source: str = "", origin: str = "") -> int:
        """People named at a firm ((name, role) pairs), each once by name.
        How many were new."""
        record = self._record(company_id, origin)
        if record is None:
            return 0
        before = self.one("SELECT COUNT(*) FROM person WHERE firm = ?", (record,))
        for name, role in people:
            self._person(record, name, role, source)
        return self.one("SELECT COUNT(*) FROM person WHERE firm = ?", (record,)) - before

    def add_location(self, company_id, *, name: str = "", address: str = "", location: str = "",
                     point: str = "", external_id: str = "", phone: str = "", source: str = "",
                     origin: str = "") -> int:
        """One of a firm's places (contacts.add_location's rules)."""
        record = self._record(company_id, origin)
        if record is None or not (external_id or address):
            return 0
        locality, county, country = place_parts(location)
        now = int(time.time())
        if external_id:
            row = self.one("SELECT id FROM locations WHERE firm = ? AND external_id = ?",
                           (record, external_id))
        else:
            row = self.one("SELECT id FROM locations WHERE firm = ? AND external_id = '' "
                           "AND address = ? AND (address != '' OR locality = ?)",
                           (record, address, locality))
        if row:
            self.x("UPDATE locations SET last_seen = ?, "
                   "address = CASE WHEN ? != '' THEN ? ELSE address END, "
                   "phone = CASE WHEN phone = '' THEN ? ELSE phone END WHERE id = ?",
                   (now, address, address, phone or "", row))
            return row
        return self.x("INSERT INTO locations (firm, name, address, locality, county, country, "
                      "point, external_id, phone, source, first_seen, last_seen) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (record, name or "", address or "", locality, county, country, point or "",
                       external_id or "", phone or "", source or "", now, now)).lastrowid

    def add_company_source(self, company_id, source: str, ref: str = "", origin: str = "",
                           batch: str = "") -> None:
        record = self._record(company_id, origin)
        if record is None or not source:
            return
        now = int(time.time())
        self.x("INSERT INTO firm_source (firm, source, batch, ref, first_seen, last_seen) "
               "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (firm, source, batch, ref) DO UPDATE SET "
               "last_seen = excluded.last_seen",
               (record, self.source(source), schema.batch_id(self.con, batch) or 0, ref or "",
                now, now))

    # ── Searches ──

    def record_search(self, target: str, target_id, module: str, capability: str, *,
                      looked_for: str = "", outcome: str = "nothing", found: int = 0,
                      cost: float = 0.0, batch_id: str = "", detail: str = "",
                      origin: str = "") -> None:
        if outcome not in contacts.SEARCH_OUTCOMES:
            outcome = "found" if found else "nothing"
        if target == "company":
            record, is_person = self.firm(target_id), 0
            run_for = self.firm(origin) if origin and str(origin) != str(target_id) else None
        else:
            record, is_person = int(target_id), 1
            run_for = self.firm(origin) if origin else None
        if record is None:
            return
        self.x("INSERT INTO search (target, is_person, origin, module, capability, looked_for, at, "
               "outcome, found, cost, batch, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (record, is_person, run_for, schema.dict_id(self.con, "module", module),
                schema.capability_id(self.con, module, capability or ""),
                schema.looked_for_mask(self.con, (looked_for or "").split(",")),
                int(time.time()), schema.OUTCOME[outcome], int(found or 0), float(cost or 0.0),
                schema.batch_id(self.con, batch_id), (detail or "")[:500] or None))

    # ── Web signals ──

    def _vocabulary(self) -> None:
        if self._signals is None:
            self._signals = {code: (sid, bool(keyed) or cardinality == "many")
                             for sid, code, keyed, cardinality in self.x(
                                 "SELECT id, code, keyed_by, cardinality FROM signal")}
            self._stored = {}
            self._declaring: dict[int, set[int]] = {}
            for sig, mod, precedence in self.x(
                    "SELECT signal, module, precedence FROM signal_source WHERE how = 0"):
                self._stored.setdefault(sig, set()).add(mod)
                if precedence == 0:
                    self._declaring.setdefault(sig, set()).add(mod)

    def add_observations(self, rows, run_id: str = "") -> int:
        """Observation rows (ingest.rows) as web-signal history: a reading
        only where its module stores the signal (declares it, or returns it
        from a capability; a register's facts are read from the register,
        never stored), on the record it was found for. A row naming no
        module goes to the module declaring the signal. A phone, a named person or a contact form is also a contact
        record, whatever is stored. How many readings were taken (a
        repeat of the current value moves only when it was last seen)."""
        from harvey.overlay.migrate import collector_modules

        self._vocabulary()
        collectors = collector_modules(self.manifests)
        kept = 0
        for r in rows or ():
            found = self._signals.get(r.get("signal_code") or "")
            record = self._record(r.get("company_id"), r.get("origin_company_id"))
            if found is None or record is None:
                continue
            sig, keyed = found
            stored = self._stored.get(sig, set())
            declaring = self._declaring.get(sig, set())
            name = r.get("module") or collectors.get(r.get("collector") or "") or ""
            module = self.one("SELECT id FROM module WHERE name = ?", (name,)) if name else (
                next(iter(declaring)) if len(declaring) == 1 else None)
            if module not in stored:
                continue
            text = r.get("value_text") or ""
            cap = r.get("capability") or ""
            mod_name = name or self.one("SELECT name FROM module WHERE id = ?", (module,))
            self.x("INSERT INTO obs (firm, signal, key, num, text, confidence, module, capability, "
                   "run, evidence_url, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (record, sig, text if keyed else "", r.get("value_num"), text or None,
                    float(r.get("confidence", 1.0)), module,
                    self.capability(mod_name, cap) if cap else None,
                    schema.batch_id(self.con, r.get("run_id") or run_id),
                    r.get("evidence_url") or None,
                    unix(r.get("observed_at")) or int(time.time())))
            kept += 1
        self._contacts_from_observations(rows or ())
        return kept

    def _contacts_from_observations(self, rows) -> None:
        """contacts.from_observations on the overlay."""
        for r in rows:
            code, text = r.get("signal_code"), (r.get("value_text") or "").strip()
            if not text or code not in ("PHONE_NUMBER", "CONTACT_FOUND", "CONTACT_FORM_URL"):
                continue
            record = self._record(r.get("company_id"), r.get("origin_company_id"))
            if record is None:
                continue
            source = r.get("module") or r.get("collector") or ""
            if code == "CONTACT_FOUND":
                name, role = contacts.split_person(text)
                self._person(record, name, role, source, unix(r.get("observed_at")))
                continue
            self._contact(record, contacts.Found(
                "phone" if code == "PHONE_NUMBER" else "contact_form", text, source=source,
                capability=r.get("capability") or "", evidence_url=r.get("evidence_url") or "",
                confidence=r.get("confidence", 1.0), seen=r.get("observed_at")))

    def observed(self, company_id, evidence_url: str) -> set[tuple[str, str]]:
        record = self.firm(company_id)
        return {(code, text or "") for code, text in self.x(
            "SELECT s.code, o.text FROM obs o JOIN signal s ON s.id = o.signal "
            "WHERE o.firm = ? AND o.evidence_url = ?", (record, evidence_url))}

    def module_readings_of(self, company_ids, module: str) -> dict:
        out = {cid: ({}, set()) for cid in company_ids}
        mod = self.one("SELECT id FROM module WHERE name = ?", (module,))
        for cid in out:
            record = self.firm(cid)
            if record is None or mod is None:
                continue
            latest, ever = out[cid]
            for code, num, text in self.x(
                    "SELECT s.code, o.num, o.text FROM obs o JOIN signal s ON s.id = o.signal "
                    "WHERE o.firm = ? AND o.module = ? ORDER BY o.at, o.id", (record, mod)):
                latest[code] = (num, text or "")
                ever.add((code, text or ""))
        return out

    def module_readings(self, company_id, module: str):
        return self.module_readings_of([company_id], module)[company_id]

    # ── A registry pull ──

    def identity(self) -> int:
        """Revision ``identity``: moves whenever which firm holds which
        identifier changes."""
        return self.one("SELECT n FROM revision WHERE name = 'identity'") or 0

    def match_records(self, records) -> tuple[int, list]:
        """A pull's records matched (a read: no write lock), with the
        identity revision they were matched at, for pull()."""
        from harvey.modules import ingest

        return self.identity(), [self.match_company(ingest.company_from(r),
                                                    declared_domain=r.declared_domain)
                                 for r in records]

    def pull(self, records, *, facts_of=None, confirmed=frozenset(), run_id: str = "",
             dry_run: bool = False, matches=None, matched_at: int | None = None
             ) -> list[tuple[str, bool, bool, bool]]:
        """Mark the firms a register pull read as Harvey's, in bulk (the
        kept-value triggers off, the touched rows recounted at the end):
        each record matched (a firm on file gains what it lacked: its
        declared website, its keys), new ones created with their declared
        website (trust unset: never above likely) and identifiers, every
        one sourced to this pull, and the phones the loaded registers know
        for them kept as contacts. A fact is stored only where its module
        stores the signal (a register's are read, not copied). Returns
        (company id, created, updated, shared domain) per record; with
        ``dry_run`` only matches ("" for an id, created for a new one).

        ``matches``: match_records' for these records, made at identity
        revision ``matched_at``. They are used when no firm or identifier
        changed since; else every record is matched again here. A record
        sharing a key with one before it in the chunk is matched again
        too (it may be the firm that one just created). What a row-at-a-
        time writer changed meanwhile beyond identities (a domain filled,
        a phone linked) is read live for domains; a phone or a name found
        meanwhile can be missed, which the duplicate scan finds later."""
        from dataclasses import replace

        from harvey.collectors.phones import phone_region
        from harvey.modules import ingest

        if dry_run:
            return [("", not self.match_company(ingest.company_from(r),
                                                declared_domain=r.declared_domain).attach,
                     False, False) for r in records]
        if matches is None or len(matches) != len(records) or matched_at != self.identity():
            matches = [None] * len(records)
        out = []
        regions: dict[int, str | None] = {}
        keys_seen: set = set()
        with triggers.bulk(self.con):
            for record, match in zip(records, matches):
                keys = resolver.identifiers(ingest.company_from(record))
                if match is not None and keys_seen.intersection(keys):
                    match = None
                keys_seen.update(keys)
                saved = self._pulled(record, run_id, match)
                out.append(saved)
                fid = self.firm(saved[0])
                if fid is None:
                    continue
                regions[fid] = phone_region(record.location or "", None, record.domain or "")
                found = (facts_of or {}).get(record.key) or []
                if found:
                    self.add_observations(ingest.rows(
                        [replace(f, record="") for f in found], company_id=saved[0],
                        origin=saved[0], run_id=run_id, confirmed=set(confirmed)), run_id)
            self._register_phones(regions)
        return out

    def _pulled(self, record, run_id: str, match=None) -> tuple[str, bool, bool, bool]:
        """ingest.source_record on the overlay (``match``: the record's,
        made before; None matches it here)."""
        from harvey.modules import ingest

        company = ingest.company_from(record)
        if match is None:
            match = self.match_company(company, declared_domain=record.declared_domain)
        existing = self.live(match.company_id) if match.attach else None
        domain = _norm(company.domain)
        holder = self.live(self.one("SELECT id FROM firm WHERE domain = ?", (domain,))) \
            if domain else None
        shared = False
        if holder and (existing is None or holder != existing):
            shared, company.domain = True, ""
        elif holder and not match.same_record:
            company.domain = ""         # joining the holder by a merge: it brings the domain
        if existing and match.same_record:
            own = self.firm(match.origin) or existing
            changed = self.fill_company_fields(str(existing), website=company.website,
                                               domain=company.domain)
            self._add_keys(own, self._own_keys(company, match), company.source, linked=True)
            self.add_company_source(str(own), record.source, record.external_id, batch=run_id)
            return str(existing), False, bool(changed), shared
        result = self.save_company(company, match, batch=run_id)
        self.add_company_source(result.company_id, record.source, record.external_id,
                                result.origin, batch=run_id)
        return result.company_id, result.created, False, shared

    def _register_phones(self, regions: dict[int, str | None]) -> int:
        """The phones every loaded register knows for these firms (by the
        key it reads them by), as contacts with the register as source."""
        kept = 0
        attached = {r[1] for r in self.x("PRAGMA database_list")}
        for adapter in self.registers:
            if adapter.alias not in attached or ("PHONE_NUMBER", "") not in adapter.by_signal:
                continue
            scheme = self.schemes.named(adapter.keyed_by)
            if scheme is None:
                continue
            keys: dict = {}
            for fid in regions:
                for (key,) in self.x("SELECT key FROM firm_ident WHERE firm = ? AND scheme = ?",
                                     (fid, scheme.id)):
                    keys[key] = fid
            if not keys:
                continue
            try:
                found = adapter.read(self.con, "PHONE_NUMBER", list(keys))
            except sqlite3.Error as e:
                logger.debug("%s phones not read: %s", adapter.module, e)
                continue
            for key, values in found.items():
                fid = keys.get(key)
                for value, _ in values:
                    kept += self._contact(fid, contacts.Found(
                        "phone", str(value), source=adapter.module, trust="confirmed",
                        region=regions.get(fid))) is not None
        return kept

    def companies_with_id(self, kind: str, company_ids=None) -> list[dict]:
        """StateManager.companies_with_id on the overlay: live firms holding
        an identifier that arrives as ``kind`` ("cui"), one row per key,
        {"id", "name", "value"}, in id order as text (the order callers
        resume a check in)."""
        schemes = [s.id for s in self.schemes.by_id.values() if s.kind == kind and not s.prefix]
        if not schemes:
            return []
        marks = ",".join(str(i) for i in schemes)
        sql = (f"SELECT CASE f.live WHEN 0 THEN f.id ELSE f.live END AS l, fi.key "
               f"FROM firm_ident fi JOIN firm f ON f.id = fi.firm WHERE fi.scheme IN ({marks})")
        rows = []
        if company_ids is None:
            rows = self.x(sql).fetchall()
        else:
            for cid in dict.fromkeys(company_ids):
                live = self.live(cid)
                if live is None:
                    continue
                group = self._group(live)
                rows += self.x(sql + f" AND fi.firm IN ({','.join('?' * len(group))})",
                               group).fetchall()
        names = {}
        out = []
        for live, key in sorted(set(rows), key=lambda r: (str(r[0]), str(r[1]))):
            if live not in names:
                names[live] = self.one("SELECT COALESCE(f.display_name, l.name, '') FROM firm f "
                                       "LEFT JOIN local_firm l ON l.firm = f.id WHERE f.id = ?",
                                       (live,)) or ""
            out.append({"id": str(live), "name": names[live], "value": str(key)})
        return out

    def held(self, scheme: str, keys) -> dict:
        """{key: the live firm (as text) holding it} for the keys of a
        scheme Harvey holds."""
        s = self.schemes.named(scheme)
        if s is None:
            return {}
        out = {}
        for key in keys:
            probe = int(key) if s.numeric and str(key).isdigit() and len(str(key)) <= 18 else key
            fid = self.one("SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?",
                           (s.id, probe))
            if fid is not None:
                out[key] = str(self.live(fid))
        return out

    # ── Merges, undo, the review list ──

    @staticmethod
    def _refused(e: merges.MergeRefused) -> resolver.MergeRefused:
        code = {"not_live": "already_merged", "same_firm": "same_company",
                "two_legal_ids": "different_cui", "survivor_merged": "survivor_merged",
                "already_undone": "not_merged", "unknown": "not_found"}.get(e.reason, e.reason)
        return resolver.MergeRefused(code, str(e))

    def _need_live(self, cid) -> int:
        fid = self.firm(cid)
        if fid is None:
            raise resolver.MergeRefused("not_found")
        if self.one("SELECT live FROM firm WHERE id = ?", (fid,)) != 0:
            raise resolver.MergeRefused("already_merged")
        return fid

    def _merge(self, keep: int, merged: int, decided_by: str, grp: int | None = None) -> int:
        row = self.x("SELECT score, reasons FROM match_candidate WHERE (a = ? AND b = ?) "
                     "OR (a = ? AND b = ?)", (keep, merged, merged, keep)).fetchone()
        place = self._branch(keep, merged)
        try:
            mid = merges.merge(self.con, keep, merged, score=row[0] if row else None,
                               reasons=(row[1] if row else None) or "[]", decided_by=decided_by,
                               grp=grp)
        except merges.MergeRefused as e:
            raise self._refused(e) from e
        if place is not None:
            # (Only once the merge is made: a refused one leaves no place.)
            now = int(time.time())
            location = self.x("INSERT INTO locations (firm, name, address, locality, county, "
                              "source, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, 'merge', ?, ?)",
                              (keep, *place, now, now)).lastrowid
            self.x("UPDATE merge SET location = ? WHERE id = ?", (location, mid))
        self._decide(keep, merged, "merged")
        return mid

    def _branch(self, keep: int, merged: int) -> tuple[str, str, str, str] | None:
        """A merged record's own place, somewhere else than the survivor's:
        (name, address, locality, county) for one of the survivor's places
        (removed again by undo), or None."""
        gone = self.x("SELECT name, address, locality, county FROM local_firm WHERE firm = ?",
                      (merged,)).fetchone()
        if gone is None:
            return None
        mine = self.x("SELECT address, locality, county FROM local_firm WHERE firm = ?",
                      (keep,)).fetchone() or (None, None, None)
        place = lambda loc, cty: ", ".join(p for p in (loc, cty) if p)  # noqa: E731
        if not resolver.branch_place({"address": mine[0], "location": place(mine[1], mine[2])},
                                     {"address": gone[1], "location": place(gone[2], gone[3])}):
            return None
        return gone[0] or "", gone[1] or "", gone[2] or "", gone[3] or ""

    def merge_companies(self, keep, merged, decided_by: str = "you") -> int:
        return self._merge(self._need_live(keep), self._need_live(merged), decided_by)

    def keeper_order(self, ids) -> list[str]:
        """Live firms among ``ids`` in the order one keeps the record: one
        with a legal identifier first, then the one holding most (links and
        readings), then the older."""
        legal = ",".join(str(i) for i in self.schemes.legal_ids()) or "0"
        rows = []
        for cid in dict.fromkeys(ids):
            fid = self.firm(cid)
            if fid is None or self.one("SELECT live FROM firm WHERE id = ?", (fid,)) != 0:
                continue
            group = self._group(fid)
            marks = ",".join("?" * len(group))
            has_legal = self.one(f"SELECT 1 FROM firm_ident WHERE firm IN ({marks}) "
                                 f"AND scheme IN ({legal}) LIMIT 1", group) is not None
            held = self.one(f"SELECT (SELECT COUNT(*) FROM link WHERE firm IN ({marks})) + "
                            f"(SELECT COUNT(*) FROM obs WHERE firm IN ({marks}))", group * 2)
            created = self.one("SELECT created FROM firm WHERE id = ?", (fid,))
            rows.append((not has_legal, -held, created, fid))
        return [str(r[3]) for r in sorted(rows)]

    def merge_pair(self, a, b) -> dict:
        if str(a) == str(b):
            raise resolver.MergeRefused("same_company")
        if self.firm(a) is None or self.firm(b) is None:
            raise resolver.MergeRefused("not_found")
        order = self.keeper_order((a, b))
        if len(order) != 2:
            raise resolver.MergeRefused("already_merged")
        keep, merged = order
        return {"survivor": keep, "merged": merged,
                "merge_id": self._merge(int(keep), int(merged), "you")}

    def merge_group(self, keep, members, out=(), decided_by: str = "you") -> dict:
        members = list(dict.fromkeys(str(m) for m in members if m and str(m) != str(keep)))
        if not members:
            raise resolver.MergeRefused("same_company")
        everyone = [keep, *members]
        if any(self.firm(c) is None for c in everyone):
            raise resolver.MergeRefused("not_found")
        ids = [self._need_live(c) for c in everyone]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if merges.legal_conflict(self.con, a, b):
                    raise resolver.MergeRefused("different_cui",
                                                f"{a} and {b} hold different legal identifiers")
        group_id = 0
        for member in ids[1:]:
            mid = self._merge(ids[0], member, decided_by, grp=group_id or None)
            if not group_id:
                group_id = mid
                self.x("UPDATE merge SET grp = ? WHERE id = ?", (mid, mid))
        if out:
            self.separate_group(list(out), [keep])
        return {"survivor": str(ids[0]), "merged": [str(m) for m in ids[1:]],
                "group_id": group_id}

    def separate_group(self, out, rest=()) -> int:
        out = list(dict.fromkeys(str(x) for x in out if x))
        rest = [str(x) for x in dict.fromkeys(rest or ()) if x and str(x) not in out]
        firms = {c: self.firm(c) for c in [*out, *rest]}
        if len(firms) < 2:
            raise resolver.MergeRefused("same_company")
        if any(f is None for f in firms.values()):
            raise resolver.MergeRefused("not_found")
        pairs = [(a, b) for a in out for b in rest] if rest else \
            [(a, b) for i, a in enumerate(out) for b in out[i + 1:]]
        for a, b in pairs:
            self._decide(firms[a], firms[b], "separate")
        return len(pairs)

    def separate_companies(self, a, b) -> bool:
        self.separate_group([a], [b])
        return True

    def undo_merge(self, merged) -> dict:
        fid = self.firm(merged)
        if fid is None:
            raise resolver.MergeRefused("not_merged")
        row = self.x("SELECT id, survivor FROM merge WHERE merged = ? AND undone_at IS NULL "
                     "ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
        if row is None:
            raise resolver.MergeRefused("not_merged")
        try:
            merges.undo(self.con, row[0])
        except merges.MergeRefused as e:
            raise self._refused(e) from e
        # An undo says the merge was wrong, not that they are two
        # companies: the pair goes back on the list.
        self._decide(row[1], fid, "pending")
        return {"survivor": str(row[1]), "merged": str(fid), "merge_id": row[0]}

    def undo_group(self, company_id) -> list[dict]:
        fid = self.firm(company_id)
        if fid is None:
            raise resolver.MergeRefused("not_merged")
        grp = self.one("SELECT grp FROM merge WHERE merged = ? AND undone_at IS NULL "
                       "AND grp IS NOT NULL ORDER BY id DESC LIMIT 1", (fid,))
        if grp is None:
            kept = [r[0] for r in self.x("SELECT DISTINCT grp FROM merge WHERE survivor = ? "
                                         "AND undone_at IS NULL AND grp IS NOT NULL LIMIT 2",
                                         (fid,))]
            if len(kept) > 1:
                raise resolver.MergeRefused("several_groups")
            grp = kept[0] if kept else None
        if grp is None:
            raise resolver.MergeRefused("not_merged")
        return [self.undo_merge(m) for (m,) in self.x(
            "SELECT merged FROM merge WHERE grp = ? AND undone_at IS NULL ORDER BY id DESC",
            (grp,)).fetchall()]

    # ── Removing a firm from Harvey ──

    def _register_sources(self) -> list[int]:
        """The source ids a register writes under: its module's name (its
        phones) and ``registry:<module>`` (its pulls)."""
        names = [n for m in self.manifests if m.register for n in (m.id, f"registry:{m.id}")]
        if not names:
            return []
        return [r[0] for r in self.x(f"SELECT id FROM source WHERE name IN "
                                     f"({','.join('?' * len(names))})", names)]

    def _harvey_work(self, fid: int, registers: list[int]) -> str:
        """What Harvey holds of its own on a firm (beyond what a register
        pull wrote), as the first such thing found, or ""."""
        marks = ",".join(str(i) for i in registers) or "NULL"
        checks = (
            ("found by a search of Harvey's", f"SELECT 1 FROM firm_source WHERE firm = ? "
                                              f"AND source NOT IN ({marks})"),
            ("contacts Harvey found", f"SELECT 1 FROM link_evidence WHERE firm = ? "
                                      f"AND source NOT IN ({marks})"),
            ("people Harvey found", "SELECT 1 FROM person WHERE firm = ?"),
            ("searches", "SELECT 1 FROM search WHERE is_person = 0 AND target = ?1 UNION ALL "
                         "SELECT 1 FROM search WHERE origin = ?1"),
            ("web signals", "SELECT 1 FROM obs WHERE firm = ?"),
            ("keys Harvey found", "SELECT 1 FROM firm_key WHERE firm = ?"),
            ("places", "SELECT 1 FROM locations WHERE firm = ?"),
            ("a website Harvey found, a name or a profile",
             "SELECT 1 FROM firm WHERE id = ? AND (website_trust IS NOT NULL "
             "OR display_name IS NOT NULL OR profiled_at IS NOT NULL)"),
            ("a duplicate decision", "SELECT 1 FROM match_candidate WHERE (a = ?1 OR b = ?1) "
                                     "AND status != 'pending'"),
        )
        for what, sql in checks:
            if self.one(sql + " LIMIT 1", (fid,)) is not None:
                return what
        return ""

    def remove_company(self, company_id) -> dict:
        """Remove a firm from Harvey: a firm a register pull brought and
        that holds nothing of Harvey's own goes, with what the pull wrote
        (identifiers, sources, the register's phones, review pairs). The
        register still has it; a pull adds it again. A firm Harvey worked
        on (searches, contacts or people it found, web signals, its own
        sources, a found website...) stays, and only its register pulls'
        source rows go: what was paid for or checked is never thrown away.
        Refused for a firm in a merge (in force or undone: the merge's
        history names it) and for one outreach names. A contact no firm
        links any more goes too, unless it is marked do-not-contact.
        Returns {"removed": its id, or "" when it stays, "kept": why it
        stays, or "", "pull_rows_removed", "contacts_removed"}."""
        fid = self.firm(company_id)
        if fid is None:
            raise RemoveRefused("not_found")
        if self.one("SELECT 1 FROM firm WHERE id = ? AND live != 0 UNION ALL "
                    "SELECT 1 FROM firm WHERE live = ? AND live != 0 UNION ALL "
                    "SELECT 1 FROM merge WHERE survivor = ? OR merged = ? UNION ALL "
                    "SELECT 1 FROM merge_member WHERE firm = ? LIMIT 1",
                    (fid, fid, fid, fid, fid)):
            raise RemoveRefused("merged")
        if self.one("SELECT 1 FROM prospects WHERE firm = ? LIMIT 1", (fid,)):
            raise RemoveRefused("has_prospects")
        registers = self._register_sources()
        kept = self._harvey_work(fid, registers)
        marks = ",".join(str(i) for i in registers) or "NULL"
        if kept:
            pulled = self.x(f"DELETE FROM firm_source WHERE firm = ? AND source IN ({marks})",
                            (fid,)).rowcount
            return {"removed": "", "kept": kept, "pull_rows_removed": pulled,
                    "contacts_removed": 0}
        pulled = self.one(f"SELECT COUNT(*) FROM firm_source WHERE firm = ? "
                          f"AND source IN ({marks})", (fid,))
        linked = [r[0] for r in self.x("SELECT DISTINCT contact FROM link_evidence WHERE firm = ?",
                                       (fid,))]
        with triggers.bulk(self.con):
            for sql in ("DELETE FROM link_evidence WHERE firm = ?", "DELETE FROM link WHERE firm = ?",
                        "DELETE FROM reading WHERE firm = ?",
                        "DELETE FROM firm_signal WHERE firm = ?",
                        "DELETE FROM firm_source WHERE firm = ?",
                        "DELETE FROM firm_ident WHERE firm = ?",
                        "DELETE FROM local_firm WHERE firm = ?",
                        "DELETE FROM legacy_id WHERE firm = ?",
                        "DELETE FROM migration_note WHERE firm = ?",
                        "DELETE FROM firm WHERE id = ?"):
                self.x(sql, (fid,))
            self.x("DELETE FROM match_candidate WHERE a = ? OR b = ?", (fid, fid))
        gone = 0
        for contact in linked:
            gone += self.x("DELETE FROM contact WHERE id = ? AND do_not_contact = 0 AND NOT EXISTS "
                           "(SELECT 1 FROM link WHERE contact = ?)",
                           (contact, contact)).rowcount
        return {"removed": str(fid), "kept": "", "pull_rows_removed": pulled,
                "contacts_removed": gone}
