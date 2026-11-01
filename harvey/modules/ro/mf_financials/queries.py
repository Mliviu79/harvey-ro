"""The financial statements' adapter: how the core reads data/financials.db.

Every figure is read from ``current`` (derived.py), keyed by the CUI as an
integer, and every range is an index search on it; FILED_ACCOUNTS from
``filed``. A company with no
statement on file has no row: its figures are unknown, never 0.
"""

from __future__ import annotations

import json

from harvey import queries
from harvey.registers import Adapter as Base
from harvey.registers import Read, chunks

MODULE = "mf_financials"

queries.statement(
    "mf_financials.describe",
    "SELECT j.value AS key, c.* FROM json_each(?) j CROSS JOIN {derived}.current c "
    "WHERE c.cui = j.value",
    params=("[1, 2]",), module=MODULE, drives="j")
# A page of companies by a figure, highest first: the index walks in order.
queries.statement(
    "mf_financials.by_turnover",
    "SELECT c.cui FROM {derived}.current c WHERE c.turnover IS NOT NULL "
    "ORDER BY c.turnover DESC LIMIT ? OFFSET ?",
    params=(50, 0), module=MODULE)


def _figure(signal: str, column: str) -> Read:
    # Dated by the year the figure comes from (an older one when the newest
    # statement leaves it empty).
    return Read(signal, "{derived}.current c", "cui", f"c.{column}", date=f"c.{column}_year",
                indexed=True)


class Adapter(Base):
    module = MODULE
    marks_sql = ("SELECT group_concat(value, ' ') FROM (SELECT value FROM {alias}.meta "
                 "WHERE key IN ('years', 'loaded_at') ORDER BY key)")
    reads = (
        _figure("TURNOVER", "turnover"),
        _figure("EMPLOYEES", "employees"),
        _figure("NET_RESULT", "net_result"),
        _figure("DEBTS", "debts"),
        _figure("EQUITY", "equity"),
        _figure("TOTAL_ASSETS", "total_assets"),
        _figure("TURNOVER_GROWTH", "turnover_growth"),
        Read("ACCOUNTS_FORM", "{derived}.current c", "cui", "c.form", date="c.year"),
        # derived.build_filed: yes at the newest year filed, no at the newest
        # final year for a firm that should have filed for it; no row
        # (a sole trader, a firm struck off or too new): unknown.
        Read("FILED_ACCOUNTS", "{derived}.filed c", "cui", "c.filed", date="c.year"),
    )

    def describe(self, con, keys) -> dict:
        """{key: the company's latest figures}."""
        out = {}
        for chunk in chunks(keys):
            cur = con.execute(self.sql("describe"), (json.dumps(chunk),))
            cols = [d[0] for d in cur.description]
            for row in cur:
                item = dict(zip(cols, row))
                out[item.pop("key")] = item
        return out
