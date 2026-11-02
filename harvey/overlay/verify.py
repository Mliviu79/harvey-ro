"""The upgrade's verification: a schema 25 file checked against the schema
24 database it was written from. Counts only (nothing a report carries
names a firm, a person or a value).

- ``doctor``: every kept value against a full recount (0 rows differ).
- ``counts``: firms, contacts, evidence and links, searches, sources,
  merges, people, outreach rows, old and new side by side.
- ``stored``: per stored signal (web signals), firms with a positive
  current reading and firms with any, as schema 24 read them and as the
  overlay keeps them.
- ``registers``: per register signal, true / false / unknown over the
  firms in Harvey: schema 24's observations (its current reading) against
  what the register modules read now, as a 3 x 3 table (old state ->
  new state), and the cause of every difference worked out per firm
  (never checked in schema 24; no register key; the register has no value
  for it now; the values differ; no loaded module reads it).
- ``filters``: the totals of a few list filters, old and new, with what
  each side has that the other doesn't.

Each check says whether it passed, and the report lists every failure:
kept values that differ from a recount, a count that differs (unless its
rule says why it may), a stored signal whose counts differ, a register
signal with a difference no allowed cause explains (or a confirmed one
that reads unknown everywhere), a filter on Harvey's own data that gives
other firms.

Schema 24's firm ids map to the new ones by the same rule the upgrade
assigns them (creation order), rebuilt here in a temporary table.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from pathlib import Path

from harvey.overlay import recount, schema
from harvey.overlay.migrate import _uri, unix

STATES = ("true", "false", "unknown")


def _q(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]


def connect(new: Path, old: Path, *, immutable: bool = False, attach: dict | None = None,
            registers=None) -> sqlite3.Connection:
    """The schema 25 file read-only with the schema 24 one attached as
    ``old`` (read-only), further read-only files, and the registers."""
    con = sqlite3.connect(_uri(new, read_only=True), uri=True)
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-400000")
    con.execute("ATTACH DATABASE ? AS old", (_uri(old, read_only=True, immutable=immutable),))
    for alias, path in (attach or {}).items():
        con.execute(f"ATTACH DATABASE ? AS {alias}", (_uri(path, read_only=True),))
    for adapter in registers or []:
        adapter.attach(con)
    con.execute("CREATE TEMP TABLE map (uuid TEXT PRIMARY KEY, firm INTEGER NOT NULL) "
                "WITHOUT ROWID")
    con.execute(f"INSERT INTO temp.map SELECT id, ROW_NUMBER() OVER (ORDER BY "
                f"COALESCE({unix('created_at')}, 0), id) FROM old.companies")
    # Observations a merge moved onto its survivor: the record they came from.
    con.execute("CREATE TEMP TABLE moved_obs (id TEXT PRIMARY KEY, uuid TEXT NOT NULL) "
                "WITHOUT ROWID")
    con.execute("INSERT OR IGNORE INTO temp.moved_obs SELECT mm.key1, m.merged "
                "FROM old.company_merge_moves mm JOIN old.company_merges m ON m.id = mm.merge_id "
                "WHERE m.undone_at IS NULL AND mm.tbl = 'observations' ORDER BY m.id")
    # Each old company's live company (merged_into), as a new live firm.
    con.execute("CREATE TEMP TABLE live_of (uuid TEXT PRIMARY KEY, live INTEGER NOT NULL) "
                "WITHOUT ROWID")
    con.execute("INSERT INTO temp.live_of SELECT m.uuid, CASE f.live WHEN 0 THEN f.id "
                "ELSE f.live END FROM temp.map m JOIN main.firm f ON f.id = m.firm")
    return con


# ── Counts ──

def counts(con, register_modules) -> dict:
    reg = ", ".join(f"'{m}'" for m in sorted(register_modules)) or "''"
    pairs_origin = ("SELECT COUNT(*) FROM (SELECT DISTINCT COALESCE(NULLIF(origin_company_id, ''), "
                    "company_id), contact_id FROM old.company_contacts)")
    rows = {
        "firms (live)": ("SELECT COUNT(*) FROM old.companies WHERE COALESCE(merged_into, '') = ''",
                         "SELECT COUNT(*) FROM firm WHERE live = 0"),
        "firms (all records)": ("SELECT COUNT(*) FROM old.companies", "SELECT COUNT(*) FROM firm"),
        "contacts": ("SELECT COUNT(*) FROM old.contacts", "SELECT COUNT(*) FROM contact"),
        "link evidence": ("SELECT COUNT(*) FROM old.company_contacts",
                          "SELECT COUNT(*) FROM link_evidence"),
        "links (record found for, contact)": (pairs_origin, "SELECT COUNT(*) FROM link"),
        "links (live firm, contact)": (
            "SELECT COUNT(*) FROM (SELECT DISTINCT lo.live, k.contact_id FROM old.company_contacts k "
            "JOIN temp.live_of lo ON lo.uuid = k.company_id)",
            "SELECT COUNT(*) FROM (SELECT DISTINCT CASE f.live WHEN 0 THEN f.id ELSE f.live END, "
            "l.contact FROM link l JOIN firm f ON f.id = l.firm)"),
        "links with a place": (
            "SELECT COUNT(*) FROM (SELECT DISTINCT COALESCE(mo.firm, mc.firm), k.contact_id "
            "FROM old.company_contacts k JOIN old.locations l ON l.id = k.location_id "
            "JOIN temp.map mc ON mc.uuid = k.company_id "
            "LEFT JOIN temp.map mo ON mo.uuid = NULLIF(k.origin_company_id, ''))",
            "SELECT COUNT(*) FROM link WHERE location != 0"),
        "searches": ("SELECT COUNT(*) FROM old.searches", "SELECT COUNT(*) FROM search"),
        "searches with an origin": (
            "SELECT COUNT(*) FROM old.searches WHERE origin_company_id != '' "
            "AND origin_company_id != target_id",
            "SELECT COUNT(*) FROM search WHERE origin IS NOT NULL"),
        "sources (record found for, source, reference)": (
            "SELECT COUNT(*) FROM (SELECT DISTINCT COALESCE(mo.firm, mc.firm), s.source, "
            "COALESCE(s.ref, '') FROM old.company_sources s JOIN temp.map mc ON mc.uuid = s.company_id "
            "LEFT JOIN temp.map mo ON mo.uuid = NULLIF(s.origin_company_id, ''))",
            "SELECT COUNT(*) FROM firm_source"),
        "sources (rows)": ("SELECT COUNT(*) FROM old.company_sources",
                           "SELECT COUNT(*) FROM firm_source"),
        "merges": ("SELECT COUNT(*) FROM old.company_merges", "SELECT COUNT(*) FROM merge"),
        "merges in force": ("SELECT COUNT(*) FROM old.company_merges WHERE undone_at IS NULL",
                            "SELECT COUNT(*) FROM merge WHERE undone_at IS NULL"),
        "merged records": ("SELECT COUNT(*) FROM old.companies WHERE COALESCE(merged_into, '') != ''",
                           "SELECT COUNT(*) FROM firm WHERE live != 0"),
        "match candidates": ("SELECT COUNT(*) FROM old.match_candidates",
                             "SELECT COUNT(*) FROM match_candidate"),
        "people Harvey found or a link names": (
            f"SELECT COUNT(*) FROM old.people WHERE source NOT IN ({reg}) OR id IN "
            f"(SELECT person_id FROM old.company_contacts WHERE person_id != 0)",
            "SELECT COUNT(*) FROM person"),
        "people (a register's administrators, read in place now)": (
            f"SELECT COUNT(*) FROM old.people WHERE source IN ({reg}) AND id NOT IN "
            f"(SELECT person_id FROM old.company_contacts WHERE person_id != 0)", "SELECT 0"),
        "prospects": ("SELECT COUNT(*) FROM old.prospects", "SELECT COUNT(*) FROM prospects"),
        "locations": ("SELECT COUNT(*) FROM old.locations", "SELECT COUNT(*) FROM locations"),
        "jobs": ("SELECT COUNT(*) FROM old.jobs", "SELECT COUNT(*) FROM jobs"),
        "settings": ("SELECT COUNT(*) FROM old.settings", "SELECT COUNT(*) FROM settings"),
        "Harvey-found email and domain keys": (
            "SELECT COUNT(*) FROM old.company_ids WHERE kind IN ('email', 'domain')",
            f"SELECT COUNT(*) FROM firm_key WHERE kind IN ({schema.FIRM_KEY['email']}, "
            f"{schema.FIRM_KEY['domain']})"),
        "websites (live)": (
            "SELECT COUNT(*) FROM old.companies WHERE COALESCE(merged_into, '') = '' "
            "AND (website != '' OR domain != '')",
            "SELECT COUNT(*) FROM firm WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL)"),
    }
    out = {}
    for name, (a, b) in rows.items():
        old, new = _q(con, a), _q(con, b)
        rule = COUNT_RULES.get(name, "equal")
        out[name] = {"old": old, "new": new, "rule": rule,
                     "passed": old == new if rule == "equal" else True}
    out["identifiers by scheme"] = dict(con.execute(
        "SELECT s.name, COUNT(*) FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme "
        "GROUP BY s.name").fetchall())
    out["firms described locally (no register key)"] = _q(con, "SELECT COUNT(*) FROM local_firm")
    out["migration notes by kind"] = dict(con.execute(
        "SELECT kind, COUNT(*) FROM migration_note GROUP BY kind").fetchall())
    out["name keys of firms described locally"] = _q(
        con, f"SELECT COUNT(*) FROM firm_key WHERE kind = {schema.FIRM_KEY['name']}")
    out["legacy ids"] = _q(con, "SELECT COUNT(*) FROM legacy_id")
    out["websites by trust (live)"] = {
        str(k): v for k, v in con.execute(
            "SELECT COALESCE(website_trust, 'declared'), COUNT(*) FROM firm WHERE live = 0 "
            "AND website IS NOT NULL GROUP BY 1").fetchall()}
    return out


# A count that may differ, and why (every other count must be equal).
COUNT_RULES = {
    "sources (rows)": "rows schema 24 held twice (on a survivor and on the record merged into "
                      "it) are one row now: the count by record found for must be equal",
    "people (a register's administrators, read in place now)": "left behind by design: the "
                                                                "register module reads them",
}


# ── Schema 24's current readings ──

def _current(rows, keyed_by: str) -> dict:
    """{company: (any current row, any positive)} from one signal's rows
    (company, num, text, observed_at, rowid), as schema 24's current-reading
    CTE reads them: the newest rows (a yearly figure: its latest year; a
    rank: each phrase's newest, a phraseless row only with no phrased one)."""
    best: dict = {}
    if keyed_by == "phrase":
        per: dict = {}
        for c, num, text, at, rid in rows:
            k = (c, text or "")
            if k not in per or (at, rid) > per[k][0]:
                per[k] = ((at, rid), num)
        phrased = {c for (c, p) in per if p}
        for (c, p), (_, num) in per.items():
            if not p and c in phrased:
                continue
            had = best.get(c, (False, False))
            best[c] = (True, had[1] or (num is not None and num != 0))
        return best
    if keyed_by == "year":
        top: dict = {}
        for c, num, text, at, rid in rows:
            try:
                year = int(text)
            except (TypeError, ValueError):
                year = 0
            key = (year, at, rid)
            if c not in top or key > top[c][0]:
                top[c] = (key, num)
        return {c: (True, num is None or num != 0) for c, (_, num) in top.items()}
    newest: dict = {}
    for c, num, text, at, rid in rows:
        positive = num is None or num != 0
        if c not in newest or at > newest[c][0]:
            newest[c] = (at, positive)
        elif at == newest[c][0]:
            newest[c] = (at, newest[c][1] or positive)
    return {c: (True, p) for c, (_, p) in newest.items()}


def _old_rows(con, code: str, keep) -> list:
    """One signal's schema 24 rows that ``keep(module, collector)`` takes,
    as (record, num, text, observed_at, rowid): the record each belongs to
    (the one it was observed for, before a merge moved it), as the upgrade
    keys it. Each record has its own current reading, and a live firm reads
    yes when any of its records does (schema 24 took the newest row of the
    merged group instead)."""
    out = []
    for c, module, collector, num, text, at, rid in con.execute(
            "SELECT COALESCE(mv.uuid, NULLIF(o.origin_company_id, ''), o.company_id), "
            "COALESCE(o.module, ''), COALESCE(o.collector, ''), o.value_num, o.value_text, "
            "o.observed_at, o.rowid FROM old.observations o "
            "LEFT JOIN temp.moved_obs mv ON mv.id = o.id "
            "WHERE o.signal_code = ? AND o.company_id != ''", (code,)):
        if keep(module, collector):
            out.append((c, num, text, at, rid))
    return out


def _live_states(con, current: dict) -> dict:
    """{live firm: "true" | "false"} from schema 24's per-company readings
    (a merged record's rows count on its live firm)."""
    out: dict = {}
    live = dict(con.execute("SELECT uuid, live FROM temp.live_of").fetchall())
    for c, (_, positive) in current.items():
        f = live.get(c)
        if f is None:
            continue
        if positive:
            out[f] = "true"
        else:
            out.setdefault(f, "false")
    return out


def _owner_rule(manifests, declared: dict, register_modules: set):
    from harvey.overlay.migrate import collector_modules

    collectors = collector_modules(manifests)

    def owner(code: str, module: str, collector: str) -> str:
        return module or collectors.get(collector) or declared.get(code) or ""
    return owner


def stored(con, manifests, register_modules: set) -> dict:
    """Per stored signal: firms with a positive current reading, firms with
    any, schema 24 (its rows that moved) against the overlay."""
    declared = dict(con.execute("SELECT code, module FROM old.signal_codes").fetchall())
    owner = _owner_rule(manifests, declared, register_modules)
    shape = {code: kb or "" for code, kb in con.execute("SELECT code, keyed_by FROM signal")}
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT s.code FROM obs o JOIN signal s ON s.id = o.signal ORDER BY 1")]
    out = {}
    for code in codes:
        rows = _old_rows(con, code, lambda m, c, code=code: owner(code, m, c)
                         not in register_modules)
        states = Counter(_live_states(con, _current(rows, shape.get(code, ""))).values())
        sid = _q(con, "SELECT id FROM signal WHERE code = ?", (code,))
        new_true, new_checked = con.execute(
            "SELECT COALESCE(SUM(n_positive > 0), 0), COUNT(*) FROM firm_signal f "
            "JOIN firm r ON r.id = f.firm AND r.live = 0 WHERE f.signal = ?", (sid,)).fetchone()
        row = {"old_true": states["true"], "old_checked": states["true"] + states["false"],
               "new_true": new_true, "new_checked": new_checked}
        row["passed"] = (row["old_true"], row["old_checked"]) == (new_true, new_checked)
        out[code] = row
    return out


# ── Register signals ──

def _date_key(date) -> str:
    if date is None:
        return ""
    if isinstance(date, (int, float)):
        return f"{int(date):04d}-12-31"
    return str(date)


def _truth(value) -> bool:
    """A register value read as yes / no, as SQLite's ``value != 0`` reads it
    (text is never 0)."""
    if isinstance(value, (int, float)):
        return value != 0
    return True


def register_states(con, adapters, signal: str, *, absent_false: dict | None = None,
                    modules: dict | None = None) -> dict:
    """{live firm: "true" | "false"} for a register signal: every register
    module reading it, by each scheme it reads by; per firm the newest
    dated reading wins, then the module's precedence. ``absent_false``:
    module -> True when a firm keyed by its scheme with no row reads no.
    ``modules``, when given, gets {live firm: the module whose reading won}."""
    precedence = dict(con.execute(
        "SELECT m.name, ss.precedence FROM signal_source ss JOIN module m ON m.id = ss.module "
        "JOIN signal s ON s.id = ss.signal WHERE s.code = ? AND ss.how = 1", (signal,)).fetchall())
    live = dict(con.execute("SELECT id, CASE live WHEN 0 THEN id ELSE live END FROM firm"))
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    picked: dict = {}
    for adapter in adapters:
        if adapter.module not in precedence or adapter.alias not in attached:
            continue
        rank = precedence[adapter.module]
        for (code, scheme), read in adapter.by_signal.items():
            if code != signal:
                continue
            scheme_name = scheme or adapter.keyed_by
            row = con.execute("SELECT id FROM ident_scheme WHERE name = ?", (scheme_name,)).fetchone()
            if row is None:
                continue
            per_firm: dict = {}
            for firm, value, date in adapter.held_values(con, signal, row[0], scheme):
                t = _truth(value)
                had = per_firm.get(firm)
                # A firm with several rows (activities) reads yes when any does.
                per_firm[firm] = (max(_date_key(date), had[0]) if had else _date_key(date),
                                  t or (had[1] if had else False))
            if (absent_false or {}).get(adapter.module):
                for (firm,) in con.execute("SELECT firm FROM firm_ident WHERE scheme = ?",
                                           (row[0],)):
                    per_firm.setdefault(firm, ("", False))
            for firm, (date, t) in per_firm.items():
                lf = live.get(firm)
                if lf is None:
                    continue
                key = (date, -rank)
                if lf not in picked or key > picked[lf][0]:
                    picked[lf] = (key, t, adapter.module)
    if modules is not None:
        modules.update({f: m for f, (_, _, m) in picked.items()})
    return {f: "true" if t else "false" for f, (_, t, _) in picked.items()}


def _matrix(old: dict, new: dict, total: int) -> dict:
    cells = Counter()
    for f in set(old) | set(new):
        cells[(old.get(f, "unknown"), new.get(f, "unknown"))] += 1
    both_unknown = total - sum(cells.values())
    cells[("unknown", "unknown")] += both_unknown
    return {f"{a}->{b}": cells[(a, b)] for a in STATES for b in STATES if cells[(a, b)]}


# Why a firm's state differs, each worked out per firm. ALLOWED causes are
# what a register read in place does differently from a stored copy; any
# other is a failure of the upgrade (or of a module's adapter).
CAUSES = {
    "not observed in schema 24": "it was never checked then; the register answers now",
    "the register has no value for it now": "the firm's key is in the register's scope but "
                                            "the register (as loaded now) has no value for it",
    "no register key": "the firm has no key of the schemes the register reads by",
    "values differ now": "the register (or a newer source among several) says otherwise now; "
                         "counted by the module whose reading wins",
    "no loaded module reads it": "a manifest declares the read but no loaded adapter provides it",
}
ALLOWED = {"not observed in schema 24", "the register has no value for it now",
           "no register key", "values differ now"}


def _keyed_firms(con, adapters, code: str) -> tuple[set, bool]:
    """(live firms with a key of a scheme some loaded register reads
    ``code`` by, whether any loaded register reads it at all)."""
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    schemes = {scheme or a.keyed_by for a in adapters if a.alias in attached
               for (c, scheme) in a.by_signal if c == code}
    if not schemes:
        return set(), False
    marks = ",".join("?" * len(schemes))
    return {f for f, in con.execute(
        f"SELECT DISTINCT CASE f.live WHEN 0 THEN f.id ELSE f.live END FROM firm_ident fi "
        f"JOIN firm f ON f.id = fi.firm JOIN ident_scheme s ON s.id = fi.scheme "
        f"WHERE s.name IN ({marks})", sorted(schemes))}, True


def _causes(old: dict, new: dict, keyed: set, read: bool, won: dict) -> dict:
    """{cause: count} over the firms whose state differs, and the value
    changes by the winning module."""
    out: Counter = Counter()
    by_module: Counter = Counter()
    for f in set(old) | set(new):
        a, b = old.get(f, "unknown"), new.get(f, "unknown")
        if a == b:
            continue
        if a == "unknown":
            out["not observed in schema 24"] += 1
        elif b == "unknown":
            if not read:
                out["no loaded module reads it"] += 1
            elif f not in keyed:
                out["no register key"] += 1
            else:
                out["the register has no value for it now"] += 1
        else:
            out["values differ now"] += 1
            by_module[won.get(f, "")] += 1
    return {"by cause": dict(out), "values differ now, by module": dict(by_module)}


def registers(con, adapters, manifests, register_modules: set, *, checked: dict | None = None,
              variants: dict | None = None) -> dict:
    """Per register signal, true / false / unknown over the firms in Harvey,
    schema 24's observations against the register reads, with the 3 x 3
    table, each difference's cause worked out per firm, and whether it
    passes: every difference has an allowed cause, and a confirmed signal
    that had readings doesn't read unknown everywhere. ``variants``: signal
    -> {name: absent_false map}, each counted too."""
    declared = dict(con.execute("SELECT code, module FROM old.signal_codes").fetchall())
    status = dict(con.execute("SELECT code, status FROM signal").fetchall())
    owner = _owner_rule(manifests, declared, register_modules)
    shape = {code: kb or "" for code, kb in con.execute("SELECT code, keyed_by FROM signal")}
    total = _q(con, "SELECT COUNT(*) FROM firm WHERE live = 0")
    absent = {(m, s): a for s, m, a in con.execute(
        "SELECT s.code, m.name, ss.absent FROM signal_source ss JOIN module m ON m.id = ss.module "
        "JOIN signal s ON s.id = ss.signal WHERE ss.how = 1")}
    codes = sorted({s for s, in con.execute(
        "SELECT DISTINCT s.code FROM signal_source ss JOIN signal s ON s.id = ss.signal "
        "WHERE ss.how = 1")})
    out = {}
    for code in codes:
        started = time.monotonic()
        rows = _old_rows(con, code, lambda m, c, code=code: owner(code, m, c) in register_modules)
        old = _live_states(con, _current(rows, shape.get(code, "")))
        del rows
        base = {m: bool(a) for (m, s), a in absent.items() if s == code}
        won: dict = {}
        new = register_states(con, adapters, code, absent_false=base, modules=won)
        keyed, read = _keyed_firms(con, adapters, code)
        old_c, new_c = Counter(old.values()), Counter(new.values())
        entry = {
            "status": status.get(code, ""),
            "read by": sorted({a.module for a in adapters for (c, _) in a.by_signal
                               if c == code}),
            "old": {s: old_c[s] if s != "unknown" else total - len(old) for s in STATES},
            "new": {s: new_c[s] if s != "unknown" else total - len(new) for s in STATES},
            "old->new": _matrix(old, new, total),
            **_causes(old, new, keyed, read, won),
        }
        failures = [c for c in entry["by cause"] if c not in ALLOWED]
        if status.get(code) == "confirmed" and old and not new:
            failures.append("a confirmed signal reads unknown for every firm")
        if old_c["true"] and not new_c["true"]:
            failures.append("schema 24 had firms reading yes; none does now")
        lost = entry["by cause"].get("the register has no value for it now", 0)
        if old and lost * 2 > len(old):
            failures.append("the register has no value now for most firms schema 24 knew")
        entry["failures"] = failures
        entry["passed"] = not failures
        for name, flags in ((variants or {}).get(code) or {}).items():
            alt = register_states(con, adapters, code, absent_false=flags)
            alt_c = Counter(alt.values())
            entry[f"variant {name}"] = {
                "new": {s: alt_c[s] if s != "unknown" else total - len(alt) for s in STATES},
                "old->new": _matrix(old, alt, total)}
            if checked and code in checked:
                entry[f"variant {name}"]["among firms checked in schema 24"] = _among(
                    old, alt, checked[code])
        if checked and code in checked:
            entry["among firms checked in schema 24"] = _among(old, new, checked[code])
        entry["seconds"] = round(time.monotonic() - started, 1)
        out[code] = entry
    out["causes"] = CAUSES
    return out


def _among(old: dict, new: dict, firms: set) -> dict:
    cells = Counter((old.get(f, "unknown"), new.get(f, "unknown")) for f in firms)
    agree = sum(n for (a, b), n in cells.items() if a == b)
    return {"firms": len(firms), "agree": agree,
            **{f"{a}->{b}": n for (a, b), n in sorted(cells.items()) if a != b}}


def checked_firms(con, module: str, code: str | None = None) -> set:
    """Live firms schema 24 had checked with a module: a source row of that
    module, or an observation of ``code`` from it."""
    rows = set()
    for (uuid,) in con.execute("SELECT company_id FROM old.company_sources WHERE source = ?",
                               (module,)):
        rows.add(uuid)
    if code:
        for (uuid,) in con.execute("SELECT DISTINCT company_id FROM old.observations "
                                   "WHERE signal_code = ? AND module = ?", (code, module)):
            rows.add(uuid)
    live = dict(con.execute("SELECT uuid, live FROM temp.live_of").fetchall())
    return {live[u] for u in rows if u in live}


# ── Filters ──

def filters(con, adapters, *, county: str = "", county_name: str = "", words: str = "",
            activity: str = "", old_search: str = "") -> dict:
    """Totals of list filters, old and new. Old: schema 24's company
    conditions (contacts.company_conditions: the place line, the activity
    table, the search index). New: the overlay's kept columns, and for
    register filters the register's own search (its canonical rows) mapped
    through firm_ident, plus firms described locally."""
    from harvey import contacts

    out: dict = {}
    by_module = {a.module: a for a in adapters}
    live = dict(con.execute("SELECT id, CASE live WHEN 0 THEN id ELSE live END FROM firm"))
    old_live = dict(con.execute("SELECT uuid, live FROM temp.live_of").fetchall())
    schemes = dict(con.execute("SELECT name, id FROM ident_scheme").fetchall())

    def firms_of_keys(keys) -> set:
        found = set()
        for scheme, key in keys:
            row = con.execute("SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?",
                              (schemes.get(scheme), key)).fetchone()
            if row:
                found.add(live[row[0]])
        return found

    def compare(name, old: set, new: set, note: str = ""):
        # A filter Harvey's own data answers must give the same firms; one a
        # register's search answers (another engine) is listed, never failed.
        out[name] = {"old": len(old), "new": len(new), "only_old": len(old - new),
                     "only_new": len(new - old), **({"note": note} if note else {}),
                     "passed": old == new if not note else True}

    con.create_function("harvey_fold", 1, contacts.name_norm, deterministic=True)
    old_ids = lambda sql, params=(): {old_live[u] for u, in con.execute(sql, params)  # noqa: E731
                                      if u in old_live}
    live_old = "COALESCE(c.merged_into, '') = ''"
    # No contact / website.
    compare("no contact",
            old_ids(f"SELECT c.id FROM old.companies c WHERE {live_old} AND c.rowid NOT IN "
                    "(SELECT x.rowid FROM old.company_contacts l JOIN old.companies x "
                    "ON x.id = l.company_id)"),
            {f for f, in con.execute("SELECT id FROM firm WHERE live = 0 AND n_contacts = 0")})
    compare("website yes",
            old_ids(f"SELECT c.id FROM old.companies c WHERE {live_old} "
                    "AND (COALESCE(c.domain, '') != '' OR COALESCE(c.website, '') != '')"),
            {f for f, in con.execute("SELECT id FROM firm WHERE live = 0 AND (website IS NOT NULL "
                                     "OR domain IS NOT NULL)")})
    onrc = next((a for a in adapters if hasattr(a, "resolve") and a.keyed_by), None)
    county_old = county_new = None
    if county_name:
        like = contacts.like(", " + contacts.fold(county_name) + ",")
        county_old = old_ids(f"SELECT c.id FROM old.companies c WHERE {live_old} AND "
                             f"(', ' || harvey_fold(c.location) || ',') LIKE ? ESCAPE '\\'",
                             (like,))
    if county and onrc is not None and "onrc" in by_module:
        # The register's search takes the county as its registry spelling.
        county_new = firms_of_keys(onrc.resolve({"county": county_name or county}))
        local = {live[f] for f, loc, cty in con.execute(
            "SELECT firm, locality, county FROM local_firm")
            if contacts.place_matches(", ".join(x for x in (loc, cty) if x), county_name)}
        county_new |= local
    if county_old is not None and county_new is not None:
        compare(f"county {county}", county_old, county_new,
                "old: the place line names the county; new: the register's canonical row "
                "(an older struck-off row elsewhere no longer counts), plus firms described locally")
    if activity and "onrc" in by_module:
        code = activity.split("/")[0]
        act_old = old_ids(f"SELECT c.id FROM old.companies c WHERE {live_old} AND c.id IN "
                          "(SELECT a.company_id FROM old.company_activities a WHERE a.code = ?)",
                          (code,))
        reg = by_module["onrc"]
        keys = []
        for version in (2, 3):
            keys += [("ro.cui", k) for k in reg.range(con, "ACTIVITY_CODE", "=", f"{code}/{version}")]
            keys += [("ro.onrc_reg", k) for k in reg.range(con, "ACTIVITY_CODE", "=",
                                                           f"{code}/{version}",
                                                           scheme="ro.onrc_reg")]
        compare(f"activity {code}", act_old, firms_of_keys(keys),
                "old: every class the pull saved; new: the canonical row's current revisions")
    if county_old is not None and county_new is not None:
        mob = ("SELECT l.contact_id, l.company_id FROM old.company_contacts l "
               "JOIN old.contacts k ON k.id = l.contact_id WHERE k.kind = 'phone' "
               "AND k.line_type = 'mobile'")
        old_mob = {c for c, u in con.execute(mob) if old_live.get(u) in county_old}
        new_mob = {c for c, f in con.execute(
            "SELECT l.contact, l.firm FROM link l JOIN contact c ON c.id = l.contact "
            "WHERE c.kind = 2 AND c.line_type = 1") if live.get(f) in county_new}
        compare(f"mobile contacts, county {county}", old_mob, new_mob,
                "the county as above: the firms the register places there now")
    if words and onrc is not None:
        s_old = old_ids(f"SELECT c.id FROM old.companies c WHERE {live_old} AND c.id IN "
                        "(SELECT company_id FROM old.company_search WHERE company_search MATCH ?)",
                        (old_search or words + "*",))
        s_new = firms_of_keys(onrc.resolve({"words": words}))
        s_new |= {live[r] for r, in con.execute(
            "SELECT rowid FROM local_fts WHERE local_fts MATCH ?", (old_search or words + "*",))}
        compare(f"search {words}", s_old, s_new,
                "old: the full-text index of name, activities and place; new: the register's "
                "search (its tiers), plus firms described locally")
    return out


def report(new: Path, old: Path, *, manifests=None, adapters=None, attach: dict | None = None,
           immutable: bool = False, county: str = "", county_name: str = "", words: str = "",
           activity: str = "", sections=("doctor", "counts", "stored", "registers", "filters"),
           variants: dict | None = None) -> dict:
    from harvey.modules.loader import load_manifests

    manifests = list(manifests if manifests is not None else load_manifests())
    register_modules = {m.id for m in manifests if m.register}
    con = connect(new, old, immutable=immutable, attach=attach, registers=adapters)
    out: dict = {}
    timing = {}
    try:
        for section in sections:
            started = time.monotonic()
            try:
                _section(out, section, con, manifests, register_modules, adapters, county,
                         county_name, words, activity, variants)
            except Exception as e:     # one failed section never hides the others
                out[section] = {"error": f"{type(e).__name__}: {e}"}
            timing[section] = round(time.monotonic() - started, 1)
    finally:
        con.close()
    out["seconds"] = timing
    out["failures"] = failures(out)
    out["passed"] = not out["failures"]
    return out


def failures(rep: dict) -> list[str]:
    """What fails in a report: a section that errored, a kept value that
    differs from a recount, a count or stored signal that differs, a
    register signal's difference with no allowed cause, a filter on
    Harvey's own data that gives other firms."""
    out = []
    for section in ("doctor", "counts", "stored", "registers", "filters"):
        part = rep.get(section)
        if not isinstance(part, dict):
            continue
        if "error" in part and isinstance(part["error"], str):
            out.append(f"{section}: {part['error']}")
            continue
        if section == "doctor":
            out += [f"doctor: {k} differs ({v})" for k, v in part.items() if v]
            continue
        for name, row in part.items():
            if isinstance(row, dict) and row.get("passed") is False:
                why = "; ".join(row.get("failures", [])) or "differs"
                out.append(f"{section}: {name}: {why}")
    return out


def _section(out, section, con, manifests, register_modules, adapters, county, county_name,
             words, activity, variants) -> None:
    if section == "doctor":
        out["doctor"] = recount.diff(con)
    elif section == "counts":
        out["counts"] = counts(con, register_modules)
    elif section == "stored":
        out["stored"] = stored(con, manifests, register_modules)
    elif section == "registers":
        checked = {"FILED_ACCOUNTS": checked_firms(con, "mf_financials", "FILED_ACCOUNTS")} \
            if "FILED_ACCOUNTS" in {r[0] for r in con.execute("SELECT code FROM signal")} \
            else {}
        out["registers"] = registers(con, adapters or [], manifests, register_modules,
                                     checked=checked, variants=variants)
    elif section == "filters":
        out["filters"] = filters(con, adapters or [], county=county,
                                 county_name=county_name, words=words, activity=activity)
