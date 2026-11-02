"""The trade registry's adapter: how the core reads data/registry.db.

A firm is reached by its CUI through ``firm_by_cui`` (the canonical row,
built by the load: derived.py), or by its registration number (``reg``,
the scheme ro.onrc_reg) when it has no CUI. Never through ``firms.cui``
directly: that index is partial (``cui != ''``) and a CUI can be on several
registrations.

Activities are read and ranged over the current revisions of the
nomenclature only (2 and 3: the classes the registry's search index tags),
so a read and a range agree; describe() lists every class on file.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache

from harvey import queries
from harvey.registers import Adapter as Base
from harvey.registers import Read, chunks

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
# The Registry page's list by name with nothing but words, area and status
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


@lru_cache(maxsize=20_000)
def place(locality, county) -> str:
    """The place line a pull gives a firm ("Town, County, Romania")."""
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
        """The registry ids the Registry page's search finds for these
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
        """The keys the Registry page's search finds for these filters, in
        its order (resolve_ids' rows as the core's keys)."""
        ids = self.resolve_ids(filters)
        mapper = sqlite3.connect(":memory:", uri=True)
        try:
            self.attach(mapper)
            return self.keys_of_ids(mapper, ids)
        finally:
            mapper.close()
