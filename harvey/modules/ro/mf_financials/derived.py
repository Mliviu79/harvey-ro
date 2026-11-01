"""The table the statements' load builds for the core: ``current``, each
company's latest figures keyed by its CUI as an integer, with an index on
every figure (a cohort range or a sort by turnover is an index search).

Each figure is the newest year that has it (a company whose latest
statement leaves turnover empty shows the year before's turnover), the way
the yearly readings were compared until now, and ``<figure>_year`` says
which year that is; ``year`` and ``form`` are the newest statement's.
``turnover_growth`` is the growth of the newest year that has one: its
turnover over the year just before's, when both were filed and the earlier
turnover was above 0 (capabilities.facts_for). ``mixed_years`` counts
companies whose figures come from more than one year.

Built inside financials.db by its load (store.build) and by ``reindex()``
for a file loaded before; ``build()`` can write into another file with
financials.db attached read-only.
"""

from __future__ import annotations

from pathlib import Path

from harvey.registers import build_derived

FIGURES = ("turnover", "employees", "net_result", "debts", "equity", "total_assets",
           "turnover_growth")
YEARS = tuple(f"{f}_year" for f in FIGURES)
COLUMNS = ("cui", "year", "form", *FIGURES, *YEARS)
BATCH = 50_000


def _year_values(r) -> dict:
    """One statement's figures (None when not filed)."""
    np_, nl = r["net_profit"], r["net_loss"]
    fixed, cur = r["fixed_assets"], r["current_assets"]
    return {
        "turnover": r["turnover"], "employees": r["employees"],
        "net_result": None if np_ is None and nl is None else (np_ or 0) - (nl or 0),
        "debts": r["debts"], "equity": r["equity"],
        "total_assets": None if fixed is None or cur is None
        else fixed + cur + (r["prepaid_expenses"] or 0),
    }


def current_row(years: list) -> tuple[tuple, bool]:
    """(the ``current`` row for one company, whether its figures come from
    several years) from its statements, oldest first."""
    newest = years[-1]
    values = {k: None for k in FIGURES}
    picked = {k: None for k in FIGURES}
    by_year = {y["year"]: y for y in years}
    for y in reversed(years):
        v = _year_values(y)
        for k, value in v.items():
            if values[k] is None and value is not None:
                values[k], picked[k] = value, y["year"]
        if values["turnover_growth"] is None and y["turnover"] is not None:
            before = by_year.get(y["year"] - 1)
            if before and before["turnover"] is not None and before["turnover"] > 0:
                values["turnover_growth"] = round(
                    (y["turnover"] - before["turnover"]) / before["turnover"] * 100, 1)
                picked["turnover_growth"] = y["year"]
    row = (int(newest["cui"]), newest["year"], newest["form"], *(values[k] for k in FIGURES),
           *(picked[k] for k in FIGURES))
    return row, len({y for y in picked.values() if y is not None}) > 1


def build(con, src: str = "main", dst: str = "main") -> dict:
    con.execute(f"DROP TABLE IF EXISTS {dst}.current")
    figures = ", ".join(f + (" REAL" if f == "turnover_growth" else " INTEGER") for f in FIGURES)
    con.execute(f"CREATE TABLE {dst}.current (cui INTEGER PRIMARY KEY, year INTEGER NOT NULL, "
                f"form TEXT NOT NULL, {figures}, {', '.join(y + ' INTEGER' for y in YEARS)})")
    cur = con.execute(
        f"SELECT cui, year, form, turnover, employees, net_profit, net_loss, debts, equity, "
        f"fixed_assets, current_assets, prepaid_expenses FROM {src}.accounts ORDER BY cui, year")
    cols = [d[0] for d in cur.description]
    sql = f"INSERT INTO {dst}.current VALUES ({','.join('?' * len(COLUMNS))})"
    rows, batch, group, last = 0, [], [], None
    mixed = skipped = 0

    def flush_group():
        nonlocal mixed, skipped
        if not group:
            return
        cui = group[-1]["cui"]
        if not (cui.isdigit() and cui[0] != "0"):
            skipped += 1
            return
        row, several = current_row(group)
        mixed += several
        batch.append(row)

    for raw in cur:
        r = dict(zip(cols, raw))
        if r["cui"] != last:
            flush_group()
            group, last = [], r["cui"]
        group.append(r)
        if len(batch) >= BATCH:
            batch.sort()
            con.executemany(sql, batch)
            rows += len(batch)
            batch = []
    flush_group()
    batch.sort()
    con.executemany(sql, batch)
    rows += len(batch)
    for f in FIGURES:
        con.execute(f"CREATE INDEX {dst}.current_{f} ON current({f}) WHERE {f} IS NOT NULL")
    return {"current": rows, "mixed_years": mixed, "cui_not_keyable": skipped}


def reindex(path: Path, target: Path | None = None) -> dict:
    """Build ``current`` into financials.db at ``path`` (or into ``target``,
    leaving it untouched)."""
    return build_derived(path, build, target, tables=("current",))
