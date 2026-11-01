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
queries.statement(
    "onrc.name_key.version",
    "SELECT version FROM {derived}.derived_meta WHERE name = 'name_key'", module=MODULE)
queries.statement(
    "onrc.name_key.holders",
    "SELECT n.firm_id FROM {derived}.name_key n WHERE n.key = ? AND n.locality = ?",
    params=("exemplu", "brasov"), module=MODULE)


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
        key, town = name_keys.name_key(name), name_keys.locality_key(location)
        if not key:
            return []
        return [r[0] for r in con.execute(self.sql("name_key.holders"), (key, town))]

    def resolve(self, filters: dict) -> list[tuple[str, object]]:
        """The keys the Registry page's search finds for these filters
        (words, county, locality, status "all"), in its order: read on its
        own connection to the registry file."""
        from harvey import registry

        con = registry.connect(self.path)
        try:
            q = registry.resolve(
                con, filters.get("words", ""),
                counties=[filters["county"]] if filters.get("county") else None,
                localities=[filters["locality"]] if filters.get("locality") else None,
                all_statuses=filters.get("status") == "all")
            ids = registry.matching_ids(con, q) if (q.match or q.firm_ids) else []
        finally:
            con.close()
        mapper = sqlite3.connect(":memory:", uri=True)
        try:
            self.attach(mapper)
            return self.keys_of_ids(mapper, ids)
        finally:
            mapper.close()
