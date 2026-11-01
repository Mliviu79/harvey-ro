"""The tables the statements' load builds for the core: ``current``, each
company's latest figures keyed by its CUI as an integer, with an index on
every figure (a cohort range or a sort by turnover is an index search),
``complete_year``, the years loaded in full and final, and ``filed``, the
FILED_ACCOUNTS reading per company.

``filed`` says yes at the newest year a company has a statement for, and
no at the newest final year for a company that should have filed for it
and didn't: one the trade registry lists as operating, of a legal form
that files (not a PFA, II or IF: they keep no balance sheet) and
registered before that year. Any other company with no statement has no
row (unknown). Telling those apart needs the registry, read-only, when it
is loaded (``reindex(..., registry=...)``); without it every row says yes.
Which registry load it was built from is recorded (``derived_meta``), so a
registry reload calls for a reindex.

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

from harvey.registers import build_derived, mark_built

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


def build(con, src: str = "main", dst: str = "main",
          registry: tuple[str, str] | None = None) -> dict:
    """``registry``: (the attached registry's alias, the alias of the file
    holding its firm_by_cui), or None when it isn't loaded."""
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
    complete = build_complete_years(con, src, dst)
    return {"current": rows, "mixed_years": mixed, "cui_not_keyable": skipped,
            "complete_years": complete, **build_filed(con, dst, registry)}


def build_complete_years(con, src: str = "main", dst: str = "main") -> int:
    """``complete_year``: the years loaded in full and final (store.complete_years),
    the ones a company's absence means it filed nothing for."""
    from harvey.modules.ro.mf_financials.store import FORMS, final_release

    have: dict[int, set[str]] = {}
    for year, form, package, published in con.execute(
            f"SELECT year, form, package, published FROM {src}.files"):
        if final_release(year, package, published):
            have.setdefault(year, set()).add(form)
    years = sorted(y for y, forms in have.items() if set(FORMS) <= forms)
    con.execute(f"DROP TABLE IF EXISTS {dst}.complete_year")
    con.execute(f"CREATE TABLE {dst}.complete_year (year INTEGER PRIMARY KEY)")
    con.executemany(f"INSERT INTO {dst}.complete_year VALUES (?)", [(y,) for y in years])
    return len(years)


# A registration date (DD/MM/YYYY, sometimes with a time) as its year.
REGISTERED_YEAR = ("CASE WHEN length(f.registered) >= 10 AND substr(f.registered, 7, 4) "
                   "GLOB '[12][0-9][0-9][0-9]' THEN CAST(substr(f.registered, 7, 4) AS INTEGER) END")


def build_filed(con, dst: str = "main", registry: tuple[str, str] | None = None) -> dict:
    """``filed`` (see the module's docstring) from ``current`` and
    ``complete_year``, and the registry when given."""
    from harvey.collectors.onrc import NATURAL_PERSON_FORMS

    con.execute(f"DROP TABLE IF EXISTS {dst}.filed")
    con.execute(f"CREATE TABLE {dst}.filed (cui INTEGER PRIMARY KEY, year INTEGER NOT NULL, "
                "filed INTEGER NOT NULL)")
    con.execute(f"INSERT INTO {dst}.filed (cui, year, filed) "
                f"SELECT cui, year, 1 FROM {dst}.current ORDER BY cui")
    final = con.execute(f"SELECT MAX(year) FROM {dst}.complete_year").fetchone()[0]
    mark = "none"
    if registry is not None and final is not None:
        reg, regx = registry
        forms = ", ".join(f"'{f}'" for f in sorted(NATURAL_PERSON_FORMS))
        con.execute(f"""
            INSERT INTO {dst}.filed (cui, year, filed)
            SELECT b.cui, ?, 0 FROM {regx}.firm_by_cui b CROSS JOIN {reg}.firms f
            WHERE f.id = b.firm_id AND f.operating = 1 AND f.form != ''
              AND upper(f.form) NOT IN ({forms}) AND {REGISTERED_YEAR} < ?
            ON CONFLICT (cui) DO UPDATE SET year = excluded.year, filed = 0
            WHERE filed.year < excluded.year""", (final, final))
        row = con.execute(f"SELECT group_concat(value, ' ') FROM (SELECT value FROM {reg}.meta "
                          "WHERE key IN ('dataset', 'loaded_at') ORDER BY key)").fetchone()
        mark = f"registry {row[0] if row and row[0] else 'unknown'}"
    mark_built(con, dst, "filed", mark)
    yes, no = con.execute(f"SELECT COALESCE(SUM(filed), 0), COALESCE(SUM(filed = 0), 0) "
                          f"FROM {dst}.filed").fetchone()
    return {"filed_yes": yes, "filed_no": no, "filed_from": mark}


def reindex(path: Path, target: Path | None = None, registry: Path | None = None,
            registry_derived: Path | None = None) -> dict:
    """Build ``current`` into financials.db at ``path`` (or into ``target``,
    leaving it untouched). ``registry``: the trade registry's file, read
    only (its firm_by_cui from ``registry_derived`` when built elsewhere),
    so ``filed`` can say no."""
    attach, reg = {}, None
    if registry is not None and Path(registry).is_file():
        attach["regf"] = Path(registry)
        if registry_derived is not None:
            attach["regd"] = Path(registry_derived)
        reg = ("regf", "regd" if registry_derived is not None else "regf")
    return build_derived(path, lambda con, src, dst: build(con, src, dst, registry=reg), target,
                         tables=("current", "complete_year", "filed"), attach=attach)
