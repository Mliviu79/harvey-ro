"""The taxpayer file's adapter: how the core reads data/taxpayers.db.

Flags and the phone are read from ``current`` (derived.py), keyed by the CUI
as an integer; the address from the file's own row, by its CUI. A firm the
file doesn't list has no row: unknown.
"""

from __future__ import annotations

import json

from harvey import queries
from harvey.registers import Adapter as Base
from harvey.registers import Read, chunks

MODULE = "mf_taxpayers"
PUBLISHED = "(SELECT value FROM {alias}.meta WHERE key = 'published')"

queries.statement(
    "mf_taxpayers.describe",
    "SELECT j.value AS key, t.* FROM json_each(?) j CROSS JOIN {alias}.taxpayers t "
    "WHERE t.cui = CAST(j.value AS TEXT)",
    params=("[1, 2]",), module=MODULE, drives="j")


def _flag(signal: str, column: str) -> Read:
    return Read(signal, "{derived}.current c", "cui", f"c.{column}", date=PUBLISHED, indexed=True)


class Adapter(Base):
    module = MODULE
    marks_sql = ("SELECT group_concat(value, ' ') FROM (SELECT value FROM {alias}.meta "
                 "WHERE key IN ('published', 'loaded_at') ORDER BY key)")
    reads = (
        _flag("HAS_EMPLOYEES_TAX", "employer"),
        _flag("MICRO_TAX_REGIME", "micro"),
        _flag("VAT_REGISTERED", "vat"),
        Read("PHONE_NUMBER", "{derived}.current c", "cui", "c.phone", date=PUBLISHED),
    )

    def describe(self, con, keys) -> dict:
        """{key: the taxpayer row, with ``address`` as one line}."""
        from harvey.modules.ro.mf_taxpayers.store import address

        out = {}
        for chunk in chunks(keys):
            cur = con.execute(self.sql("describe"), (json.dumps(chunk),))
            cols = [d[0] for d in cur.description]
            for row in cur:
                item = dict(zip(cols, row))
                item["address"] = address(item)
                out[item.pop("key")] = item
        return out
