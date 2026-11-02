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


# A cohort range that names a year reads that year's statements in
# ``accounts`` (each figure as SQL on ``c``, as derived._year_values works it
# out): one read of the year's rows, each probed in firm_ident by its CUI
# (as an integer, and back as text: only a plain CUI matches, as in
# ``current``). The counts job keeps which firms have each figure each year
# (bitmaps.py); a page asks only for the firms in range.
YEAR_FIGURES = {
    "TURNOVER": "c.turnover", "EMPLOYEES": "c.employees",
    "NET_RESULT": "(CASE WHEN c.net_profit IS NULL AND c.net_loss IS NULL THEN NULL "
                  "ELSE COALESCE(c.net_profit, 0) - COALESCE(c.net_loss, 0) END)",
    "DEBTS": "c.debts", "EQUITY": "c.equity",
    "TOTAL_ASSETS": "(CASE WHEN c.fixed_assets IS NULL OR c.current_assets IS NULL THEN NULL "
                    "ELSE c.fixed_assets + c.current_assets + COALESCE(c.prepaid_expenses, 0) "
                    "END)",
}
_BY_CUI = "fi.scheme = ? AND fi.key = +CAST(c.cui AS INTEGER) AND c.cui = CAST(fi.key AS TEXT)"
_YEAR_READ = {"accounts": "a cohort range on a named year: that year's statements read once "
                          "per load mark (kept), each probed in firm_ident"}
queries.statement("mf_financials.years", "SELECT value FROM {alias}.meta WHERE key = 'years'",
                  module=MODULE)
queries.statement(
    "mf_financials.year_valued",
    f"SELECT fi.firm, {', '.join(f'{e} IS NOT NULL' for e in YEAR_FIGURES.values())} "
    f"FROM {{alias}}.accounts c CROSS JOIN firm_ident fi WHERE c.year = ? AND {_BY_CUI}",
    params=(2024, 1), module=MODULE, drives="c",
    scans={"accounts": "the counts job, per load mark: one year's statements read whole, "
                       "each probed in firm_ident"})
for _code, _e in YEAR_FIGURES.items():
    queries.statement(
        f"mf_financials.year_held.{_code}",
        f"SELECT fi.firm FROM {{alias}}.accounts c CROSS JOIN firm_ident fi WHERE c.year = ? "
        f"AND {_e} >= COALESCE(?, {_e}) AND {_e} <= COALESCE(?, {_e}) AND {_BY_CUI}",
        params=(2024, 1, 2, 1), module=MODULE, drives="c", scans=_YEAR_READ)
    queries.statement(
        f"mf_financials.year_read.{_code}",
        f"SELECT j.value, {_e} FROM json_each(?) j CROSS JOIN {{alias}}.accounts c "
        f"WHERE c.cui = CAST(j.value AS TEXT) AND c.year = ? AND {_e} IS NOT NULL",
        params=("[1, 2]", 2024), module=MODULE, drives="j")


def _figure(signal: str, column: str) -> Read:
    # Dated by the year the figure comes from (an older one when the newest
    # statement leaves it empty).
    return Read(signal, "{derived}.current c", "cui", f"c.{column}", date=f"c.{column}_year",
                indexed=True)


class Adapter(Base):
    module = MODULE
    # The years loaded and when, and the registry load ``filed`` was built
    # from (a registry reload builds it again: derived.refile).
    marks_sql = ("SELECT group_concat(value, ' ') FROM (SELECT key, value FROM {alias}.meta "
                 "WHERE key IN ('years', 'loaded_at') UNION ALL SELECT name, version "
                 "FROM {derived}.derived_meta WHERE name = 'filed' ORDER BY 1)")
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

    # Named years (reads._year_sets): the signals a single year's statement
    # gives, the years loaded, and per year the records with each figure,
    # those in a range, and some records' figures by their keys.
    year_codes = tuple(YEAR_FIGURES)

    def years(self, con) -> list[int]:
        row = con.execute(self.sql("years")).fetchone()
        return sorted({int(y) for y in (row[0] if row else "").split() if y.isdigit()},
                      reverse=True)

    def year_valued(self, con, year, scheme_id: int) -> dict[str, list[int]]:
        """{signal: the records in Harvey whose ``year`` statement has its
        figure}: the year's statements read whole (a job, never a page)."""
        out: dict[str, list[int]] = {code: [] for code in YEAR_FIGURES}
        lists = list(out.values())
        for firm, *has in con.execute(self.sql("year_valued"), (int(year), scheme_id)):
            for got, into in zip(has, lists):
                if got:
                    into.append(firm)
        return out

    def year_held(self, con, code: str, year, low, high, scheme_id: int):
        """The records in Harvey whose ``year`` figure is in [low, high]
        (None: open)."""
        return (r[0] for r in con.execute(self.sql(f"year_held.{code}"),
                                          (int(year), low, high, scheme_id)))

    def year_read(self, con, code: str, year, keys) -> dict:
        """{key: its ``year`` figure} for the keys whose statement has it."""
        out = {}
        for chunk in chunks(keys, 10_000):
            out.update(con.execute(self.sql(f"year_read.{code}"),
                                   (json.dumps(chunk), int(year))).fetchall())
        return out

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
