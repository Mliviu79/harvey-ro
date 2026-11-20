"""The trade registry's adapter: how the core reads data/registry.db.

A firm is reached by its CUI through ``firm_by_cui`` (the canonical row,
built by the load: derived.py), or by its registration number (``reg``,
the scheme ro.onrc_reg) when it has no CUI. Never through ``firms.cui``
directly: that index is partial (``cui != ''``) and a CUI can be on several
registrations. The one read that wants every registration of a CUI (whether
a record in Harvey holds any of a firm's numbers: the records statements)
goes through that index, and says ``cui != ''``.

Activities are read and ranged over the current revisions of the
nomenclature only (2 and 3: the classes the registry's search index tags),
so a read and a range agree; describe() lists every class on file.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache

from harvey import queries
from harvey.modules.ro.onrc.derived import CUI_OK
from harvey.registers import Adapter as Base
from harvey.registers import Read, Refused, Stale, chunks

MODULE = "onrc"
DATASET_DATE = "(SELECT value FROM {alias}.meta WHERE key = 'date')"
REG = "ro.onrc_reg"
# The revisions the search index tags (registry.fill_search_index).
CURRENT_REVISIONS = (2, 3)
_CURRENT = f"k.version IN ({', '.join(map(str, CURRENT_REVISIONS))})"

_FIRM_COLUMNS = ("f.id, f.reg, f.name, f.cui, f.form, f.county, f.county_code, f.locality, "
                 "f.places, f.address, f.website, f.domain, f.registered, f.statuses, f.operating")

queries.statement(
    "onrc.describe.by_cui",
    f"SELECT j.value AS key, {_FIRM_COLUMNS} FROM json_each(?) j "
    "CROSS JOIN {derived}.firm_by_cui b CROSS JOIN {alias}.firms f "
    "WHERE b.cui = j.value AND f.id = b.firm_id",
    params=("[1, 2]",), module=MODULE, drives="j")
queries.statement(
    "onrc.describe.by_reg",
    f"SELECT j.value AS key, {_FIRM_COLUMNS} FROM json_each(?) j "
    "CROSS JOIN {alias}.firms f WHERE f.reg = j.value",
    params=('["J08/1/2015"]',), module=MODULE, drives="j")
queries.statement(
    "onrc.describe.activities",
    "SELECT k.firm_id, k.code, k.version FROM json_each(?) j "
    "CROSS JOIN {alias}.firm_caen k WHERE k.firm_id = j.value ORDER BY k.firm_id, k.code",
    params=("[1, 2]",), module=MODULE, drives="j", bounded=True)
queries.statement(
    "onrc.describe.administrators",
    "SELECT a.firm_id, a.first_name, a.last_name, a.role FROM json_each(?) j "
    "CROSS JOIN {alias}.administrators a WHERE a.firm_id = j.value",
    params=("[1, 2]",), module=MODULE, drives="j", bounded=True)
# The registry ids a search found, as the core's keys: the canonical row of
# a CUI (a struck-off older row of the same CUI is left out), or the
# registration number of a firm with no CUI.
queries.statement(
    "onrc.keys_of_ids",
    "SELECT f.id, f.cui, f.reg, b.firm_id FROM json_each(?) j CROSS JOIN {alias}.firms f "
    "LEFT JOIN {derived}.firm_by_cui b ON b.cui = CAST(f.cui AS INTEGER) AND f.cui != '' "
    "WHERE f.id = j.value",
    params=("[1, 2]",), module=MODULE, drives="j")
# The records in Harvey a search's registry ids stand for, by the key each
# row is reached by (derived firm_key, which holds one of the two): a
# CUI's canonical row by the CUI, a row with no CUI by its registration
# number, each id probed once. Params: the ids as JSON, the CUI scheme's
# id, the registration number scheme's.
queries.statement(
    "onrc.held_of_ids",
    "SELECT fi.firm, k.cui IS NULL, COALESCE(k.cui, k.reg), k.firm_id FROM json_each(?) j "
    "CROSS JOIN {derived}.firm_key k CROSS JOIN firm_ident fi WHERE k.firm_id = j.value "
    "AND fi.scheme = CASE WHEN k.cui IS NULL THEN ? ELSE ? END "
    "AND fi.key = +COALESCE(k.cui, k.reg)",
    params=("[1, 2]", 2, 1), module=MODULE, drives="j")
# Firms holding a class, through the registry's own full-text tags (the
# activity table has no index by class), canonical rows only.
queries.statement(
    "onrc.range.ACTIVITY_CODE",
    "SELECT b.cui FROM {alias}.firm_fts t CROSS JOIN {alias}.firms f "
    "CROSS JOIN {derived}.firm_by_cui b "
    "WHERE t.firm_fts MATCH ? AND f.id = t.rowid AND f.cui != '' "
    "AND b.cui = CAST(f.cui AS INTEGER) AND b.firm_id = f.id",
    params=("tags : c8623r3",), module=MODULE, drives="t")
# The same for firms with no CUI, by registration number.
queries.statement(
    f"onrc.range.ACTIVITY_CODE@{REG}",
    "SELECT f.reg FROM {alias}.firm_fts t CROSS JOIN {alias}.firms f "
    "WHERE t.firm_fts MATCH ? AND f.id = t.rowid AND f.cui = ''",
    params=("tags : c8623r3",), module=MODULE, drives="t")
# The All firms list by name with nothing but words, area and status
# (registry_view._index_page, on the registry's own connection: alias
# ``main``): the full-text index counted, then a page of it in its rowid
# order, which is the name order (the load numbers firms by name, as
# registry_view.matching takes it too).
queries.statement(
    "onrc.index_count", "SELECT COUNT(*) FROM {alias}.firm_fts WHERE firm_fts MATCH ?",
    params=("tags : activ",), module=MODULE,
    scans={"firm_fts": "a page's total: the matches counted in the full-text index"})
for _way in ("ASC", "DESC"):
    queries.statement(
        f"onrc.index_page.{_way.lower()}",
        "SELECT rowid FROM {alias}.firm_fts WHERE firm_fts MATCH ? "
        f"ORDER BY rowid {_way} LIMIT ? OFFSET ?",
        params=("tags : activ", 50, 0), module=MODULE)
# The registry's name for each firm in Harvey with a CUI (its canonical
# row): an upgrade's comparison with the name Harvey shows.
queries.statement(
    "onrc.names.held",
    "SELECT fi.firm, f.name FROM firm_ident fi CROSS JOIN {derived}.firm_by_cui b "
    "CROSS JOIN {alias}.firms f WHERE fi.scheme = ? AND b.cui = fi.key AND f.id = b.firm_id",
    params=(1,), module=MODULE, drives="fi",
    scans={"firm_ident": "an upgrade's walk: every CUI in Harvey, each probed in the "
                         "registry by key"})
# The same for firms in Harvey known by registration number (no CUI).
queries.statement(
    f"onrc.names.held@{REG}",
    "SELECT fi.firm, f.name FROM firm_ident fi CROSS JOIN {alias}.firms f "
    "WHERE fi.scheme = ? AND f.reg = fi.key",
    params=(1,), module=MODULE, drives="fi",
    scans={"firm_ident": "an upgrade's walk: every registration number in Harvey, each "
                         "probed in the registry by key"})
# What a page sorts a set of firms by (the registry id follows the names'
# alphabetical order) and its place, by CUI or registration number.
queries.statement(
    "onrc.sort.by_cui",
    "SELECT j.value, f.id, f.name, f.locality, f.county FROM json_each(?) j "
    "CROSS JOIN {derived}.firm_by_cui b CROSS JOIN {alias}.firms f "
    "WHERE b.cui = j.value AND f.id = b.firm_id",
    params=("[1, 2]",), module=MODULE, drives="j")
queries.statement(
    "onrc.sort.by_reg",
    "SELECT j.value, f.id, f.name, f.locality, f.county FROM json_each(?) j "
    "CROSS JOIN {alias}.firms f WHERE f.reg = j.value",
    params=('["J08/1/2015"]',), module=MODULE, drives="j")
# The people a firm's registration names (its administrators), by CUI or
# registration number: the People to search page.
queries.statement(
    "onrc.people.by_cui",
    "SELECT j.value, a.first_name, a.last_name, a.role FROM json_each(?) j "
    "CROSS JOIN {derived}.firm_by_cui b CROSS JOIN {alias}.administrators a "
    "WHERE b.cui = j.value AND a.firm_id = b.firm_id",
    params=("[1, 2]",), module=MODULE, drives="j", bounded=True)
queries.statement(
    "onrc.people.by_reg",
    "SELECT j.value, a.first_name, a.last_name, a.role FROM json_each(?) j "
    "CROSS JOIN {alias}.firms f CROSS JOIN {alias}.administrators a "
    "WHERE f.reg = j.value AND a.firm_id = f.id",
    params=('["J08/1/2015"]',), module=MODULE, drives="j", bounded=True)
queries.statement(
    "onrc.people_count.by_cui",
    "SELECT COUNT(*) FROM (SELECT DISTINCT j.value, a.first_name, a.last_name FROM json_each(?) j "
    "CROSS JOIN {derived}.firm_by_cui b CROSS JOIN {alias}.administrators a "
    "WHERE b.cui = j.value AND a.firm_id = b.firm_id)",
    params=("[1, 2]",), module=MODULE, drives="j", bounded=True)
queries.statement(
    "onrc.people_count.by_reg",
    "SELECT COUNT(*) FROM (SELECT DISTINCT j.value, a.first_name, a.last_name FROM json_each(?) j "
    "CROSS JOIN {alias}.firms f CROSS JOIN {alias}.administrators a "
    "WHERE f.reg = j.value AND a.firm_id = f.id)",
    params=('["J08/1/2015"]',), module=MODULE, drives="j", bounded=True)
# How many people (each name once per firm) the registry names at the
# firms in Harvey with a CUI, counted up to a bound: whether the People
# page's count passes its cap, read before the register's counts are kept.
queries.statement(
    "onrc.people_held_upto",
    "SELECT COUNT(*) FROM (SELECT DISTINCT fi.firm, a.first_name, a.last_name "
    "FROM firm_ident fi CROSS JOIN {derived}.firm_by_cui b CROSS JOIN {alias}.administrators a "
    "WHERE fi.scheme = ? AND b.cui = fi.key AND a.firm_id = b.firm_id LIMIT ?)",
    params=(1, 100001), module=MODULE, drives="fi",
    scans={"firm_ident": "firms in Harvey walked only until the bound (the page's cap) is met"})
# The registry id alone (its names' order) by CUI or registration number:
# what sorts every firm in Harvey by name without reading the names.
queries.statement(
    "onrc.order.by_cui",
    "SELECT j.value, b.firm_id FROM json_each(?) j CROSS JOIN {derived}.firm_by_cui b "
    "WHERE b.cui = j.value", params=("[1, 2]",), module=MODULE, drives="j")
queries.statement(
    "onrc.order.by_reg",
    "SELECT j.value, f.id FROM json_each(?) j CROSS JOIN {alias}.firms f WHERE f.reg = j.value",
    params=('["J08/1/2015"]',), module=MODULE, drives="j")
# Every firm in Harvey with a CUI and its registry id (the names' order).
queries.statement(
    "onrc.order.held",
    "SELECT fi.firm, b.firm_id FROM firm_ident fi CROSS JOIN {derived}.firm_by_cui b "
    "WHERE fi.scheme = ? AND b.cui = fi.key",
    params=(1,), module=MODULE, drives="fi",
    scans={"firm_ident": "every CUI in Harvey, once per kept ordering of every firm by name"})
queries.statement(
    "onrc.name_of_id", "SELECT name FROM {alias}.firms WHERE id = ?", params=(1,), module=MODULE)
# Each firm's place in the registry's orders (overlay/orders.py): its row's
# id (the names' order) and its place line's rank, for every firm in Harvey
# by CUI or registration number, or for some (a list of firm ids).
_PLACE = ("LEFT JOIN {derived}.place_rank p ON p.locality = COALESCE(f.locality, '') "
          "AND p.county = COALESCE(f.county, '')")
queries.statement(
    "onrc.order.all",
    "SELECT fi.firm, f.id, p.rank FROM firm_ident fi CROSS JOIN {derived}.firm_by_cui b "
    f"CROSS JOIN {{alias}}.firms f {_PLACE} WHERE fi.scheme = ? AND b.cui = fi.key "
    "AND f.id = b.firm_id",
    params=(1,), module=MODULE, drives="fi",
    scans={"firm_ident": "every CUI in Harvey, once per registry load (a job)"})
queries.statement(
    f"onrc.order.all@{REG}",
    f"SELECT fi.firm, f.id, p.rank FROM firm_ident fi CROSS JOIN {{alias}}.firms f {_PLACE} "
    "WHERE fi.scheme = ? AND f.reg = fi.key",
    params=(1,), module=MODULE, drives="fi",
    scans={"firm_ident": "every registration number in Harvey, once per registry load (a job)"})
queries.statement(
    "onrc.order.some",
    "SELECT fi.firm, f.id, p.rank FROM json_each(?) j CROSS JOIN firm_ident fi "
    f"CROSS JOIN {{derived}}.firm_by_cui b CROSS JOIN {{alias}}.firms f {_PLACE} "
    "WHERE fi.firm = j.value AND fi.scheme = ? AND b.cui = fi.key AND f.id = b.firm_id",
    params=("[1, 2]", 1), module=MODULE, drives="j")
queries.statement(
    f"onrc.order.some@{REG}",
    "SELECT fi.firm, f.id, p.rank FROM json_each(?) j CROSS JOIN firm_ident fi "
    f"CROSS JOIN {{alias}}.firms f {_PLACE} WHERE fi.firm = j.value AND fi.scheme = ? "
    "AND f.reg = fi.key",
    params=("[1, 2]", 1), module=MODULE, drives="j")
queries.statement("onrc.order.max_id", "SELECT MAX(id) FROM {alias}.firms", module=MODULE)
queries.statement(
    "onrc.order.name_from",
    "SELECT id, name FROM {alias}.firms WHERE id >= ? ORDER BY id LIMIT 1",
    params=(1,), module=MODULE)
queries.statement(
    "onrc.order.place_from",
    "SELECT rank, line FROM {derived}.place_rank WHERE line >= ? ORDER BY line LIMIT 1",
    params=("Brașov",), module=MODULE)
queries.statement(
    "onrc.order.place_last", "SELECT MAX(rank) FROM {derived}.place_rank", module=MODULE)
queries.statement(
    "onrc.order.ready",
    "SELECT COUNT(*) FROM {derived}.sqlite_master WHERE name = 'place_rank'", module=MODULE)
queries.statement(
    "onrc.name_key.version",
    "SELECT version FROM {derived}.derived_meta WHERE name = 'name_key'", module=MODULE)
queries.statement(
    "onrc.name_key.holders",
    "SELECT n.firm_id FROM {derived}.name_key n WHERE n.key = ? AND n.locality = ?",
    params=("exemplu", "brasov"), module=MODULE)

# ── Records for a selection of firms (overlay.store.Store.
#    ensure_registry_records) ──
# Registration numbers one selection may name (rows ticked on All firms, a
# set of the agent's, the keys statement's one JSON list).
REGS_MAX = 10_000
# A selection goes into temp.registering (overlay/queries.py) as its firms'
# canonical rows (derived firm_key: a CUI's one row, or the row of a firm
# with no CUI), each once, with its CUI, its registration number and the
# record in Harvey that holds each already. A matched or chosen registration
# that isn't its CUI's canonical one stands for the firm: its canonical row
# is taken (through firm_by_cui), once however many of its registrations
# the selection holds. Params: the selection, the CUI scheme's id, the
# registration number scheme's, then the do-not-contact list's domains (a
# JSON list, normalised as gate.do_not_contact_domain_match reads them).
# A row the selection holds but leaves out is written with ``left_out`` set,
# for the caller to count and drop: 1, a registration no key reaches (its
# CUI isn't one the core can key by: derived.CUI_OK; written by its own row
# id, negated, so it never meets a firm's); 2, a firm whose declared
# website is on the do-not-contact list (that domain, or one under it: the
# label boundary of gate.do_not_contact_domain_match), as ingest.excluded
# leaves it out.
# The probes in firm_ident are by (scheme, key), compared with no affinity
# (``+``) so the probe keeps the key. A firm's number is its canonical
# row's, else any other registration of its CUI (read through the index on
# firms.cui, which needs ``cui != ''``): a record holding only an old
# number of the firm is the firm's record.
_BY_CUI = "(SELECT i.firm FROM firm_ident i WHERE i.scheme = ?2 AND i.key = +k.cui)"
_BY_REG = ("COALESCE((SELECT i.firm FROM firm_ident i WHERE i.scheme = ?3 AND i.key = +f.reg), "
           "(SELECT i.firm FROM {alias}.firms o CROSS JOIN firm_ident i "
           "WHERE k.cui IS NOT NULL AND o.cui = f.cui AND o.cui != '' "
           "AND i.scheme = ?3 AND i.key = +o.reg LIMIT 1))")
_LEFT_OUT = ("CASE WHEN k.firm_id IS NULL THEN 1 WHEN f.domain != '' AND EXISTS ("
             "SELECT 1 FROM json_each(?4) d WHERE f.domain = d.value "
             "OR substr(f.domain, -length(d.value) - 1) = '.' || d.value) THEN 2 END")
_SELECTED = ("INSERT OR IGNORE INTO temp.registering "
             "(firm_row, cui, reg, by_cui, by_reg, left_out) "
             f"SELECT COALESCE(k.firm_id, -g.id), k.cui, COALESCE(f.reg, g.reg), {_BY_CUI}, "
             f"{_BY_REG}, {_LEFT_OUT} ")
# From a registration ``g`` to its firm: the canonical row ``k``, read as
# ``f`` (both NULL for a registration no key reaches).
_CANONICAL = ("CROSS JOIN {alias}.firms g LEFT JOIN {derived}.firm_by_cui b "
              f"ON b.cui = CAST(g.cui AS INTEGER) AND g.cui != '' "
              f"AND {CUI_OK.replace('f.cui', 'g.cui')} "
              "LEFT JOIN {derived}.firm_key k ON k.firm_id = COALESCE(b.firm_id, g.id) "
              "LEFT JOIN {alias}.firms f ON f.id = k.firm_id")
# Firms chosen by registration number (a JSON list).
queries.statement(
    "onrc.records.select.keys",
    f"{_SELECTED}FROM json_each(?1) j {_CANONICAL} WHERE g.reg = j.value",
    params=('["J08/1/2015"]', 1, 2, '["exemplu.example.ro"]'), module=MODULE, drives="j")


def _fin_bounds(at: int) -> str:
    """mf_financials.store.Filter.sql("f.cui") with every bound a parameter
    that may be NULL, so one statement serves every combination. Params
    from ``at``: whether a firm with no figure is kept (1 or 0), the least
    and most turnover, the least and most employees."""
    unknown, *bounds = (f"?{at + i}" for i in range(5))

    def column(name, low, high):
        return (f"CASE WHEN {low} IS NULL AND {high} IS NULL THEN 1 "
                f"WHEN l.{name} IS NULL THEN {unknown} "
                f"ELSE ({low} IS NULL OR l.{name} >= {low}) "
                f"AND ({high} IS NULL OR l.{name} <= {high}) END")

    return (f"(({unknown} AND NOT EXISTS (SELECT 1 FROM fin.latest l WHERE l.cui = f.cui)) "
            f"OR EXISTS (SELECT 1 FROM fin.latest l WHERE l.cui = f.cui "
            f"AND {column('turnover', *bounds[:2])} AND {column('employees', *bounds[2:])}))")


def _tax_flags(at: int) -> str:
    """mf_taxpayers.store.Filter.sql("f.cui") the same way. Params from
    ``at``: employer, VAT payer, micro-enterprise tax, each 1, 0 or NULL
    (not asked)."""
    return ("EXISTS (SELECT 1 FROM tax.taxpayers x WHERE x.cui = f.cui AND "
            + " AND ".join(f"(?{at + i} IS NULL OR x.{column} = ?{at + i})"
                           for i, column in enumerate(("employer", "vat", "micro_tax"))) + ")")


# Firms a resolved registry query matches (registry.Query.match, a
# full-text expression), with its financial bounds and its taxpayer
# conditions when it has them (the financials and the taxpayer file
# attached beside the registry, as registry._where reads them; a firm's
# registrations carry the one CUI, so they hold for the canonical row as
# for the matched one).
for _name, _conds, _args in (
        ("", (), ()),
        ("+fin", (_fin_bounds(5),), (0, 1, None, None, None)),
        ("+tax", (_tax_flags(5),), (1, None, None)),
        ("+fin+tax", (_fin_bounds(5), _tax_flags(10)), (0, 1, None, None, None, 1, None, None))):
    queries.statement(
        f"onrc.records.select.match{_name}",
        f"{_SELECTED}FROM {{alias}}.firm_fts t {_CANONICAL} "
        f"WHERE t.firm_fts MATCH ?1 AND g.id = t.rowid"
        + "".join(f" AND {c}" for c in _conds),
        params=("tags : activ", 1, 2, '["exemplu.example.ro"]', *_args), module=MODULE,
        drives="t")
# Inside the write, when identifiers changed since the selection was read:
# the firms about to get a record that have one now (another writer made
# it meanwhile) are taken off, by the same three probes. Params: the CUI
# scheme's id, the registration number scheme's.
queries.statement(
    "onrc.records.recheck",
    "DELETE FROM temp.recording WHERE "
    "EXISTS (SELECT 1 FROM firm_ident i WHERE i.scheme = ?1 AND i.key = +recording.cui) "
    "OR EXISTS (SELECT 1 FROM firm_ident i WHERE i.scheme = ?2 AND i.key = +recording.reg) "
    "OR EXISTS (SELECT 1 FROM {alias}.firms o CROSS JOIN firm_ident i "
    "WHERE recording.cui IS NOT NULL AND o.cui = CAST(recording.cui AS TEXT) AND o.cui != '' "
    "AND i.scheme = ?2 AND i.key = +o.reg)",
    params=(1, 2), module=MODULE)
# The new records themselves: each firm of temp.recording at the id after
# the highest given so far, at its place in the registry's orders
# (overlay/orders.py: its row's id follows the names, its place line's rank
# the places). Params: the highest id given so far, the time.
queries.statement(
    "onrc.records.firms",
    "INSERT INTO firm (id, created, name_order, place_order) "
    "SELECT ?1 + n.n, ?2, n.firm_row, p.rank FROM temp.recording n "
    f"CROSS JOIN {{alias}}.firms f {_PLACE} WHERE f.id = n.firm_row",
    params=(0, 1_700_000_000), module=MODULE, drives="n")
# The selection's firms that have a record and haven't taken their registry
# entry yet (no source on the record or its live firm, no website on file),
# whose registry row declares a website: the newest records first, up to a
# cap (params: 1 for every one or 0 for those no contact reaches, the cap).
# Their search reads that website when it starts, so an estimate counts
# them as firms with a website, never as lookups.
queries.statement(
    "onrc.records.declared",
    "SELECT DISTINCT v.id FROM temp.registering n CROSS JOIN firm f CROSS JOIN firm v "
    "CROSS JOIN {alias}.firms r WHERE f.id = COALESCE(n.by_cui, n.by_reg) "
    "AND v.id = CASE f.live WHEN 0 THEN f.id ELSE f.live END AND r.id = n.firm_row "
    "AND r.website != '' AND v.website IS NULL AND (?1 OR v.n_contacts = 0) "
    "AND NOT EXISTS (SELECT 1 FROM firm_source s WHERE s.firm = f.id) "
    "AND NOT EXISTS (SELECT 1 FROM firm_source s WHERE s.firm = v.id) "
    "ORDER BY v.id DESC LIMIT ?2",
    params=(0, 50_000), bounded=True, module=MODULE, drives="n")
queries.statement(
    "onrc.records.ready",
    "SELECT COUNT(*) FROM {derived}.sqlite_master WHERE type = 'table' "
    "AND name IN ('firm_by_cui', 'firm_key', 'place_rank')", module=MODULE)
# The dataset the registry was loaded from (its name and date): what a
# firm read from it is sourced to (Adapter.pull_record).
queries.statement(
    "onrc.dataset",
    "SELECT key, value FROM {alias}.meta WHERE key IN ('dataset', 'date')", module=MODULE)


@lru_cache(maxsize=20_000)
def place(locality, county) -> str:
    """The place line a firm's registry entry gives it ("Town, County, Romania")."""
    from types import SimpleNamespace

    from harvey.collectors.onrc import _location

    return _location(SimpleNamespace(locality=locality or "", county=county or ""))


def industry(activities) -> str:
    """What a firm does, in words: its first current class (collectors.onrc)."""
    from harvey.caen import describe
    from harvey.collectors.onrc import _industry_class

    first = _industry_class([(str(c), int(v)) for c, v in activities])
    return (describe(*first) or "") if first else ""


class Adapter(Base):
    module = MODULE
    marks_sql = ("SELECT group_concat(value, ' ') FROM (SELECT value FROM {alias}.meta "
                 "WHERE key IN ('dataset', 'loaded_at') ORDER BY key)")
    _person = "a.first_name || ' ' || a.last_name || ' — ' || a.role"
    _class = "printf('%04d/%d', k.code, k.version)"
    # Only a decision maker's role (collectors.onrc: administrator,
    # director, president).
    _decides = ("(a.role LIKE 'administrator%' OR a.role LIKE 'director%' "
                "OR a.role LIKE 'pre_edinte%')")
    # Each signal twice: by CUI through the canonical row, and by
    # registration number for a firm with no CUI.
    reads = (
        Read("ACTIVITY_CODE", "{derived}.firm_by_cui c CROSS JOIN {alias}.firm_caen k",
             "cui", _class, date=DATASET_DATE, where=f"k.firm_id = c.firm_id AND {_CURRENT}"),
        Read("CONTACT_FOUND", "{derived}.firm_by_cui c CROSS JOIN {alias}.administrators a",
             "cui", _person, date=DATASET_DATE, where="a.firm_id = c.firm_id"),
        Read("DECISION_MAKER_TITLE",
             "{derived}.firm_by_cui c CROSS JOIN {alias}.administrators a",
             "cui", _person, date=DATASET_DATE, where=f"a.firm_id = c.firm_id AND {_decides}"),
        Read("ACTIVITY_CODE", "{alias}.firms c CROSS JOIN {alias}.firm_caen k", "reg", _class,
             date=DATASET_DATE, where=f"c.cui = '' AND k.firm_id = c.id AND {_CURRENT}",
             scheme=REG),
        Read("CONTACT_FOUND", "{alias}.firms c CROSS JOIN {alias}.administrators a", "reg",
             _person, date=DATASET_DATE, where="c.cui = '' AND a.firm_id = c.id", scheme=REG),
        Read("DECISION_MAKER_TITLE", "{alias}.firms c CROSS JOIN {alias}.administrators a",
             "reg", _person, date=DATASET_DATE,
             where=f"c.cui = '' AND a.firm_id = c.id AND {_decides}", scheme=REG),
    )

    def describe(self, con, keys, scheme: str = "ro.cui") -> dict:
        """{key: the firm's registry row, with ``activities`` [(code,
        version)] and ``administrators`` [(first, last, role)]}."""
        name = "describe.by_cui" if scheme == "ro.cui" else "describe.by_reg"
        out, by_id = {}, {}
        for chunk in chunks(keys):
            cur = con.execute(self.sql(name), (json.dumps(chunk),))
            cols = [d[0] for d in cur.description]
            for row in cur:
                firm = dict(zip(cols, row))
                firm["activities"], firm["administrators"] = [], []
                out[firm["key"]] = firm
                by_id[firm["id"]] = firm
        for chunk in chunks(list(by_id)):
            arg = (json.dumps(chunk),)
            for firm_id, code, version in con.execute(self.sql("describe.activities"), arg):
                by_id[firm_id]["activities"].append((f"{code:04d}", version))
            for firm_id, first, last, role in con.execute(self.sql("describe.administrators"), arg):
                by_id[firm_id]["administrators"].append((first, last, role))
        return out

    def card(self, firm: dict) -> dict:
        """A described firm as the core shows a company: its name, place
        line, address, legal form, declared site, what it does (the
        industry its first current class names) and whether it operates."""
        return {"name": firm.get("name") or "", "location": place(firm.get("locality"),
                                                                   firm.get("county")),
                "address": firm.get("address") or "", "legal_form": firm.get("form") or "",
                "website": firm.get("website") or "", "domain": firm.get("domain") or "",
                "industry": industry(firm.get("activities") or []),
                "registered": firm.get("registered") or "",
                "operating": bool(firm.get("operating")), "reg": firm.get("reg") or ""}

    def sort_keys(self, con, keys, scheme: str = "ro.cui", places: bool = True) -> dict:
        """{key: (registry id, name, place line)} for sorting a set of firms
        (the registry id follows the names in alphabetical order); the place
        line is "" unless ``places``."""
        name = "sort.by_cui" if scheme == "ro.cui" else "sort.by_reg"
        out = {}
        for chunk in chunks(keys, 10_000):
            for key, rid, firm_name, locality, county in con.execute(self.sql(name),
                                                                    (json.dumps(chunk),)):
                out[key] = (rid, firm_name or "", place(locality, county) if places else "")
        return out

    def people(self, con, keys, scheme: str = "ro.cui") -> dict:
        """{key: [(name, role)]}: the people each firm's registration names."""
        name = "people.by_cui" if scheme == "ro.cui" else "people.by_reg"
        out: dict = {}
        for chunk in chunks(keys, 10_000):
            for key, first, last, role in con.execute(self.sql(name), (json.dumps(chunk),)):
                out.setdefault(key, []).append((f"{first} {last}".strip(), role or ""))
        return out

    def count_people(self, con, keys, scheme: str = "ro.cui") -> int:
        """How many people (each name once per firm) the firms' registrations
        name."""
        name = "people_count.by_cui" if scheme == "ro.cui" else "people_count.by_reg"
        return sum(con.execute(self.sql(name), (json.dumps(chunk),)).fetchone()[0]
                   for chunk in chunks(keys, 50_000))

    def people_held_upto(self, con, scheme_id: int, bound: int) -> int:
        """How many people the registry names at the firms in Harvey with a
        CUI (``scheme_id``: the scheme's id), counted only up to ``bound``."""
        return con.execute(self.sql("people_held_upto"), (scheme_id, int(bound))).fetchone()[0]

    def name_order(self, con, keys, scheme: str = "ro.cui") -> dict:
        """{key: its registry id}: the registry's ids follow its names'
        alphabetical order, so sorting by id sorts by name."""
        name = "order.by_cui" if scheme == "ro.cui" else "order.by_reg"
        out = {}
        for chunk in chunks(keys, 50_000):
            out.update(con.execute(self.sql(name), (json.dumps(chunk),)).fetchall())
        return out

    def held_name_order(self, con, scheme_id: int) -> list[tuple[int, int]]:
        """(record, registry id) for every record in Harvey with a CUI the
        registry has, in its names' order (the CUI scheme's id given)."""
        return sorted(con.execute(self.sql("order.held"), (scheme_id,)), key=lambda r: r[1])

    # Places in the registry's orders (overlay/orders.py)

    def order_schemes(self) -> tuple[str, ...]:
        """The schemes positions are read by, the one that wins last."""
        return (REG, "ro.cui")

    def order_sql(self, scheme: str, some: bool = False) -> str:
        """(record, registry id, place rank) for every record in Harvey keyed
        by ``scheme`` (param: its id), or (``some``) for a list of firm ids
        (params: the list as JSON, the scheme's id)."""
        name = "order.some" if some else "order.all"
        return self.sql(name if scheme == "ro.cui" else f"{name}@{REG}")

    def can_order(self, con) -> bool:
        try:
            return bool(con.execute(self.sql("order.ready")).fetchone()[0])
        except sqlite3.Error:
            return False

    def name_position(self, con, name: str) -> float:
        """Where ``name`` falls among the registry's names: half a step
        before the first registry id whose name sorts at or after it (the
        load sorts names by onrc._fold)."""
        from harvey.collectors.onrc import _fold

        key = _fold(name or "")
        top = con.execute(self.sql("order.max_id")).fetchone()[0] or 0
        lo, hi = 1, top + 1
        while lo < hi:
            mid = (lo + hi) // 2
            row = con.execute(self.sql("order.name_from"), (mid,)).fetchone()
            if row is None:
                hi = mid
            elif _fold(row[1] or "") < key:
                lo = row[0] + 1
            else:
                hi = mid
        return lo - 0.5

    def place_position(self, con, locality: str, county: str) -> float:
        """Where a place line falls among the registry's: its rank when the
        registry has it, else half a step before the next."""
        line = place(locality, county)
        row = con.execute(self.sql("order.place_from"), (line,)).fetchone()
        if row is None:
            return (con.execute(self.sql("order.place_last")).fetchone()[0] or 0) + 0.5
        return float(row[0]) if row[1] == line else row[0] - 0.5

    # Records for a selection of its firms (overlay.store.Store.
    # ensure_registry_records)

    def record_schemes(self) -> tuple[str, str]:
        """The schemes a record of a registry firm holds its identifiers
        under: its CUI's, its registration number's."""
        return ("ro.cui", REG)

    def select_firms(self, con, schemes: tuple[int, int], *, regs=None, query=None,
                     barred=()) -> None:
        """Put a selection of firms into temp.registering (see the
        statements): the firms of the registration numbers ``regs`` (at
        most REGS_MAX), or those a resolved ``query`` (registry.Query)
        matches. ``schemes``: the ids of record_schemes() in the database
        on ``con``. ``barred``: the do-not-contact list's domains; a firm
        whose declared website is one of them, or under one, is written
        left out (as is a registration no key reaches), for the caller to
        count. It reads the registry and the identifiers in Harvey and
        writes the temporary table only: no lock on the database.

        A query is taken when its match is a full-text expression, however
        many firms that is ("-word" over operating firms is nearly all of
        them), with or without financial bounds and taxpayer conditions
        (read from the financials and the taxpayer file attached to
        ``con``, not from the query's own paths). Refused (ValueError): a
        query whose match is empty (it names nothing), one whose match is
        every firm (registry.EVERY: only exclusions or conditions over
        every status, so no full-text expression to read it by), and one
        made from registration numbers (pass ``regs``).
        FileNotFoundError when a condition's file isn't loaded; Stale when
        the registry's key tables predate this code."""
        from harvey import registry

        if regs is not None:
            regs = list(regs)
            if len(regs) > REGS_MAX:
                raise ValueError(f"at most {REGS_MAX:,} firms can be chosen one by one")
        else:
            if query.firm_ids is not None:
                raise ValueError("a registry query made from registration numbers isn't taken "
                                 "here: pass the numbers themselves")
            if not query.match:
                raise ValueError("the registry query names no activity, area or filter: "
                                 "nothing to select by")
            if query.match == registry.EVERY:
                raise Refused("a registry query with nothing for the search index to match "
                              "(only exclusions, financial or taxpayer conditions, over "
                              "every status) isn't taken yet: name an activity, an area or "
                              "a word, or keep to operating firms", "registry_every_status")
        if con.execute(self.sql("records.ready")).fetchone()[0] != 3:
            raise Stale("the registry's key tables (firm_by_cui, firm_key, place_rank) aren't "
                        "all built: run harvey registry load --force")
        domains = json.dumps(list(barred or ()))
        if regs is not None:
            con.execute(self.sql("records.select.keys"), (json.dumps(regs), *schemes, domains))
            return
        attached = {r[1] for r in con.execute("PRAGMA database_list")}
        name, params = "records.select.match", [query.match, *schemes, domains]
        fin = query.fin if query.fin is not None and query.fin.active() else None
        if fin is not None:
            if "fin" not in attached:
                raise FileNotFoundError("no financial statements loaded: harvey financials load")
            name += "+fin"
            params += [int(fin.unknown == "include"), fin.min_turnover, fin.max_turnover,
                       fin.min_employees, fin.max_employees]
        tax = query.tax if query.tax is not None and query.tax.active() else None
        if tax is not None:
            if "tax" not in attached:
                raise FileNotFoundError("no taxpayer file loaded: harvey taxpayers load")
            name += "+tax"
            params += [None if wanted is None else int(bool(wanted))
                       for wanted in (tax.employer, tax.vat, tax.micro)]
        con.execute(self.sql(name), params)

    def recheck_sql(self) -> str:
        """The DELETE that takes off temp.recording the firms a record
        holds now (params: the ids of record_schemes())."""
        return self.sql("records.recheck")

    def declared_sql(self) -> str:
        """The SELECT of the selection's records that haven't taken their
        registry entry and whose registry row declares a website (params:
        1 for every one or 0 for those no contact reaches, a cap)."""
        return self.sql("records.declared")

    def record_firms_sql(self) -> str:
        """The INSERT that writes the firms of temp.recording as records,
        each at its place in the registry's orders (params: the highest
        firm id given so far, the time they are created)."""
        return self.sql("records.firms")

    def pull_record(self, con, key, scheme: str = "ro.cui"):
        """One firm as the record its registry entry is read as (collectors.onrc.
        firms_draft: its identifiers, place, declared website and domain,
        its source), by CUI or registration number; None when the registry
        has no such firm. Its administrators are left in the registry: the
        contact finder names them when it works on the firm."""
        from harvey.collectors import onrc

        firm = self.describe(con, [key], scheme).get(key)
        if firm is None:
            return None
        meta = dict(con.execute(self.sql("dataset")).fetchall())
        entry = onrc.Firm(**{f: firm.get(f) or "" for f in onrc.Firm.__dataclass_fields__})
        draft = onrc.firms_draft([entry], onrc.Dataset(meta.get("dataset", ""),
                                                       meta.get("date", ""), {}),
                                 codes={entry.reg: firm["activities"]},
                                 with_administrators=False)
        return draft.records[0]

    def name_of_id(self, con, registry_id: int) -> str:
        row = con.execute(self.sql("name_of_id"), (registry_id,)).fetchone()
        return row[0] if row else ""

    def names_sql(self, scheme: str = "") -> str | None:
        if scheme in ("", "ro.cui"):
            return self.sql("names.held")
        if scheme == REG:
            return self.sql(f"names.held@{REG}")
        return None

    def keys_of_ids(self, con, ids) -> list[tuple[str, object]]:
        """Registry ids (in order) as the core's (scheme, key), canonical
        rows only, each once."""
        found = {}
        for chunk in chunks(ids):
            for firm_id, cui, reg, canonical in con.execute(self.sql("keys_of_ids"),
                                                            (json.dumps(chunk),)):
                if not cui:
                    found[firm_id] = ("ro.onrc_reg", reg)
                elif canonical == firm_id:
                    found[firm_id] = ("ro.cui", int(cui))
        out, seen = [], set()
        for firm_id in ids:
            key = found.get(firm_id)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def range_sql(self, signal: str, op: str, scheme: str = "") -> str | None:
        if signal == "ACTIVITY_CODE" and op == "=" and scheme in ("", REG):
            return self.sql(f"range.ACTIVITY_CODE{'@' + scheme if scheme else ''}")
        return None

    def range(self, con, signal: str, op: str, value, scheme: str = "") -> list:
        """ACTIVITY_CODE = "8623/3" (a class and its revision, a current
        one): CUIs, or with ``scheme`` ro.onrc_reg the registration numbers
        of the firms with no CUI."""
        sql = self.range_sql(signal, op, scheme)
        if sql is None:
            raise ValueError(f"onrc can't range over {signal} with {op}")
        code, _, version = str(value).partition("/")
        version = int(version or CURRENT_REVISIONS[-1])
        if version not in CURRENT_REVISIONS:
            raise ValueError(f"onrc ranges over activities of revisions "
                             f"{', '.join(map(str, CURRENT_REVISIONS))} only, not {version}")
        tag = f"tags : c{int(code):04d}r{version}"
        return [r[0] for r in con.execute(sql, (tag,))]

    def name_holders(self, con, name: str, location: str) -> list[int]:
        """Registry ids of operating firms with this name key in this town.
        Raises registers.Stale when the index was built by other rules."""
        from harvey import name_keys

        return self.key_holders(con, name_keys.name_key(name), name_keys.locality_key(location))

    def key_holders(self, con, key: str, town: str) -> list[int]:
        """Registry ids of operating firms with this name key (already a
        key: resolver.name_key) in this town key. Raises registers.Stale as
        name_holders does."""
        from harvey import name_keys
        from harvey.registers import Stale

        row = None
        try:
            row = con.execute(self.sql("name_key.version")).fetchone()
        except sqlite3.OperationalError:     # built before the mark existed
            pass
        if row is None or row[0] != name_keys.version():
            raise Stale(f"the registry's name index was built by rules "
                        f"{row[0] if row else 'unknown'}, the code's are {name_keys.version()}: "
                        "reindex the registry")
        if not key:
            return []
        return [r[0] for r in con.execute(self.sql("name_key.holders"), (key, town))]

    def resolve_ids(self, filters: dict) -> list[int]:
        """The registry ids the All firms search finds for these
        filters (words, county, locality, status "all"), in its order: read
        on its own connection to the registry file."""
        from harvey import registry

        con = registry.connect(self.path)
        try:
            q = registry.resolve(
                con, filters.get("words", ""),
                counties=[filters["county"]] if filters.get("county") else None,
                localities=[filters["locality"]] if filters.get("locality") else None,
                all_statuses=filters.get("status") == "all")
            return registry.matching_ids(con, q) if (q.match or q.firm_ids) else []
        finally:
            con.close()

    def held_of_ids(self, con, ids, scheme_ids: dict) -> list[tuple[int, str, object, int]] | None:
        """(record in Harvey, scheme, key, registry id) for the registry ids
        (a search's) that firms in Harvey hold, in no order: each id's key
        (derived firm_key) probed in firm_ident. ``scheme_ids``: scheme
        name -> id. None when the registry's derived tables predate
        firm_key (its next load or reindex builds it)."""
        out = []
        # (A scheme Harvey doesn't know matches no identifier: id -1.)
        sids = (scheme_ids.get(REG, -1), scheme_ids.get("ro.cui", -1))
        schemes = ("ro.cui", REG)
        try:
            sql = self.sql("held_of_ids")
            for chunk in chunks(ids, 50_000):
                out += [(firm, schemes[by_reg], key, rid) for firm, by_reg, key, rid in
                        con.execute(sql, (json.dumps(chunk), *sids))]
        except sqlite3.OperationalError:
            return None
        return out

    def resolve(self, filters: dict) -> list[tuple[str, object]]:
        """The keys the All firms search finds for these filters, in
        its order (resolve_ids' rows as the core's keys)."""
        ids = self.resolve_ids(filters)
        mapper = sqlite3.connect(":memory:", uri=True)
        try:
            self.attach(mapper)
            return self.keys_of_ids(mapper, ids)
        finally:
            mapper.close()
