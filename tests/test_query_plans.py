"""Query plans against databases shaped like the real ones.

Every named statement (harvey/queries.py: the overlay's and each register
module's) is explained with every register attached and production-sized
statistics injected (``sqlite_stat1``; a register whose load never analyses
its file is left without, so it is planned blind as it is in production),
and fails on a step that reads more than SMALL rows of a big table, by the
statistics: a scan (a small partial index may be read whole), an index
search whose bound columns still match many rows (every key of a scheme,
a flag half the rows hold), or an index walked in order under a LIMIT that
filters on a column the index doesn't hold. It fails too on a temporary
sort of a set the statement doesn't bound, and on a join order other than
the one it spells. A statement that reads many rows on purpose (a range,
a recount job) says which tables and why (``scans``).

Today's pages (schema 24) are driven through the dashboard with every
statement they run traced and explained the same way; the scans and sorts
they do today are the allow-list below, which later work shrinks to empty
(an entry that no longer happens fails too, so the list only gets shorter).

All data is fictional; the statistics are row counts only.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from harvey import queries, registers
from harvey.modules.loader import load_manifests
import harvey.overlay.queries  # noqa: F401  (registers the overlay's statements)
from harvey.overlay import schema as overlay

SMALL = 50_000

# ── Statistics ──

# Row counts of the real files (sqlite_stat1 as the loads leave it; the
# derived tables' from their build, the rest estimated from their counts).
# An entry named after its table is its primary key's index (a WITHOUT
# ROWID table's, or a text primary key's).
REGISTER_STATS = {
    "reg": {
        "firms": [("firms_cui", "4017370 1"), ("firms_reg", "4224673 1"),
                  ("firms_registered", "4100000 2 1")],
        "firm_caen": [("firm_caen", "19746899 7 1 1")],
        "administrators": [("administrators_firm", "3298321 2")],
        "caen": [("sqlite_autoindex_caen_1", "2343 2 1")],
        "meta": [("sqlite_autoindex_meta_1", "11 1")],
        "firm_by_cui": [(None, "3919161")],
        "name_key": [("name_key", "1784770 2 1 1")],
    },
    "fin": {
        "latest": [("latest", "1122150 1")],
        "accounts": [("accounts", "2831693 3 1")],
        "files": [("sqlite_autoindex_files_1", "9 3 1")],
        "meta": [("sqlite_autoindex_meta_1", "7 1")],
        "filed": [(None, "1700000")], "complete_year": [(None, "3")],
        "current": [(None, "1122150"), ("current_turnover", "1000000 3"),
                    ("current_employees", "1000000 20"), ("current_net_result", "1100000 3"),
                    ("current_debts", "1100000 3"), ("current_equity", "1100000 3"),
                    ("current_total_assets", "1100000 3"), ("current_turnover_growth", "700000 5")],
    },
    "tax": {
        "taxpayers": [("taxpayers", "2647765 1")],
        "files": [("sqlite_autoindex_files_1", "2 1")],
        "meta": [("sqlite_autoindex_meta_1", "9 1")],
        "current": [(None, "2647765"), ("current_vat", "2600000 1300000"),
                    ("current_employer", "2600000 1300000"), ("current_micro", "2600000 1300000")],
    },
    "anaf": {"answers": [(None, "1785000")], "parsed": [(None, "1785000")],
             "dict": [(None, "1")]},
}

# The overlay at the size of today's install (1.79M firms in Harvey).
OVERLAY_STATS = {
    "firm": [(None, "1790000"), ("firm_domain", "6000 1"), ("firm_merged", "60 2"),
             ("firm_new", "1790000 1 1"), ("firm_with_contact", "11500 1 1"),
             ("firm_no_contact", "1778000 1 1"), ("firm_by_contacts", "1790000 100000 1"),
             ("firm_with_site", "11000 50 1 1"), ("firm_profiled", "1790000 1000000"),
             ("firm_searched", "20000 2 1")],
    "firm_ident": [("firm_ident", "3600000 1800000 1"), ("firm_ident_firm", "3600000 2 1")],
    "local_firm": [(None, "400"), ("local_firm_place", "400 20 5")],
    "contact": [(None, "910000"), ("sqlite_autoindex_contact_1", "910000 300000 1"),
                ("contact_new", "910000 2 1"), ("contact_kind", "910000 300000 200000 2 1"),
                ("contact_reach", "910000 50000 1")],
    "link": [("link", "1850000 2 1"), ("link_contact", "1850000 2 1"), ("link_person", "3000 2")],
    "link_evidence": [("link_evidence", "1900000 2 1 1 1"),
                      ("link_evidence_source", "1900000 200000 2")],
    "person": [(None, "20000"), ("sqlite_autoindex_person_1", "20000 2 1")],
    "search": [(None, "21000"), ("search_target", "21000 15000 3 2 2 1"),
               ("search_at", "21000 2"), ("search_batch", "15000 20")],
    "firm_source": [("firm_source", "2900000 2 1 1 1"), ("firm_source_by", "2900000 300000 2 1")],
    "merge": [(None, "50"), ("merge_survivor", "50 2"), ("merge_merged", "50 1"),
              ("merge_grp", "10 2")],
    "obs": [(None, "10000"), ("obs_firm", "10000 3 2 2 1"), ("obs_run", "9000 10")],
    "reading": [("reading", "9000 3 2 1"), ("reading_signal", "9000 300 150 10 1")],
    "firm_signal": [("firm_signal", "8000 300 1"), ("firm_signal_firm", "8000 2")],
    "legacy_id": [("legacy_id", "1790000 1")],
}

# The registers whose loads analyse their file (registry.build and the
# statements' load run ANALYZE); the taxpayer file and ANAF's answers have
# no statistics, so the fixtures leave them without too.
ANALYSED = {"reg", "fin"}

# Today's harvey.db has no statistics (it was never analysed), so its plans
# are made blind: these are its big tables by name.
BIG_V24 = {"companies", "observations", "company_search", "company_activities",
           "company_ids", "company_contacts", "company_sources", "people", "contacts"}


# Rows per index (and per table), as the statistics give them: what the
# plan checker estimates an index search from. Filled as the fixture files
# are built (an analysed file's and an unanalysed one's alike: the checker
# judges by the real sizes even where the planner plans blind).
STAT: dict[str, list[int]] = {}


def _stat_rows(con: sqlite3.Connection, schema: str, stats: dict) -> list[tuple]:
    """(table, index or None, stat) for every table of a database, from
    ``stats`` ({table: [(index, table name for its primary key, or None,
    stat)]}); an index not named gets its table's row count with ten rows a
    key prefix."""
    out = []
    tables = [r[0] for r in con.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'")]
    for table in tables:
        given = dict(stats.get(table, []))
        rows = int((given.get(None) or next(iter(given.values()), "100")).split()[0])
        if None in given:
            out.append((table, None, given[None]))
        for _, idx, unique, origin, _ in con.execute(f"PRAGMA {schema}.index_list('{table}')"):
            if idx in given:
                stat = given[idx]
            elif origin == "pk" and table in given:
                stat = given[table]
            else:
                n = len(con.execute(f"PRAGMA {schema}.index_info('{idx}')").fetchall()) or 1
                stat = " ".join([str(rows)] + ["10"] * (n - 1) + ["1" if unique else "10"])
            out.append((table, idx, stat))
    return out


def _inject(con: sqlite3.Connection, schema: str, stats: dict, *, write: bool = True) -> None:
    """Replace a database's statistics with ``stats`` (``write`` False:
    leave it unanalysed, recording the sizes for the checker only)."""
    rows = _stat_rows(con, schema, stats)
    for table, idx, stat in rows:
        n = [int(x) for x in stat.split()]
        key = idx or table
        if key not in STAT or STAT[key][0] < n[0]:
            STAT[key] = n
    if not write:
        return
    con.execute(f"ANALYZE {schema}")
    con.execute(f"DELETE FROM {schema}.sqlite_stat1")
    con.executemany(f"INSERT INTO {schema}.sqlite_stat1 VALUES (?, ?, ?)", rows)
    con.commit()


def _sizes() -> tuple[dict, dict]:
    """(rows per table, rows per index) from the statistics above."""
    tables, indexes = {}, {}
    for stats in (*REGISTER_STATS.values(), OVERLAY_STATS):
        for table, rows in stats.items():
            for idx, stat in rows:
                n = int(stat.split()[0])
                if idx is None:
                    tables[table] = max(tables.get(table, 0), n)
                else:
                    indexes[idx] = n
                    tables.setdefault(table, n)
                    tables[table] = max(tables[table], n) if idx == table else tables[table]
    return tables, indexes


TABLE_ROWS, INDEX_ROWS = _sizes()
for _name in BIG_V24:
    TABLE_ROWS.setdefault(_name, 10**6)
TABLE_ROWS.update({"firm_fts": 4_224_673, "local_fts": 400, "caen_fts": 2343})


# ── Fixture files (fictional rows, real shapes) ──

def build_registry(path: Path) -> None:
    from harvey import registry
    from harvey.modules.ro.onrc import derived

    con = sqlite3.connect(path)
    con.executescript(registry.SCHEMA)
    con.executemany(
        "INSERT INTO firms (reg, name, cui, form, county, county_code, locality, address, "
        "website, domain, registered, statuses, operating) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(f"J08/{i}/2015", f"EXEMPLU {n} SRL", cui, "SRL", "Brașov", "BV", "Brașov",
          f"Str. Exemplului nr. {i}", "", "", "01/02/2015", "1048", 1)
         for i, (n, cui) in enumerate((("ALFA", "1000004"), ("BETA", "1000012"),
                                       ("GAMA", "")), 1)])
    con.executemany("INSERT INTO firm_caen VALUES (?, ?, ?)",
                    [(1, 8623, 3), (1, 4321, 3), (1, 7112, 1), (3, 4120, 2)])
    con.executemany("INSERT INTO administrators VALUES (1, ?, 'Exemplu', ?)",
                    [("Ana", "administrator"), ("Ion", "asociat")])
    con.executemany("INSERT INTO meta VALUES (?, ?)",
                    [("dataset", "firme-01-01-2026"), ("date", "2026-01-01"),
                     ("loaded_at", "2026-01-02 00:00:00"), ("schema", registry.SCHEMA_VERSION)])
    con.execute("CREATE UNIQUE INDEX firms_reg ON firms(reg)")
    con.execute("CREATE INDEX firms_cui ON firms(cui) WHERE cui != ''")
    registry.index_registered(con)
    con.execute("CREATE INDEX administrators_firm ON administrators(firm_id)")
    registry._load_caen(con)
    registry.fill_search_index(con)
    derived.build(con)
    con.commit()
    _inject(con, "main", REGISTER_STATS["reg"])
    con.close()


def build_financials(path: Path) -> None:
    from harvey.modules.ro.mf_financials import derived, store

    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.execute(store.LATEST)
    cols = ("cui", "year", "form", "turnover", "employees")
    con.executemany(f"INSERT INTO accounts ({', '.join(cols)}) VALUES (?,?,?,?,?)",
                    [("1000004", 2024, "UU", 250000, 4), ("1000012", 2024, "UU", 1500000, 12)])
    con.execute(store.FILL_LATEST)
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("years", "2024"),
                                                      ("loaded_at", "2026-01-02 00:00:00")])
    derived.build(con)
    con.commit()
    _inject(con, "main", REGISTER_STATS["fin"])
    con.close()


def build_taxpayers(path: Path) -> None:
    from harvey.modules.ro.mf_taxpayers import derived, store

    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.executemany("INSERT INTO taxpayers (cui, name, vat, employer, micro_tax, phone) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [("1000004", "ALFA EXEMPLU SRL", 1, 1, 0, "0268000000"),
                     ("1000012", "BETA EXEMPLU SRL", 0, 1, 1, "")])
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("published", "2026-06-30"),
                                                      ("loaded_at", "2026-07-01 00:00:00")])
    derived.build(con)
    con.commit()
    _inject(con, "main", REGISTER_STATS["tax"], write="tax" in ANALYSED)
    con.close()


def build_anaf(path: Path) -> None:
    """The compressed form (store.py), as the database upgrade writes it."""
    from harvey.modules.ro.anaf import store

    con = sqlite3.connect(path)
    store.create(con)
    store.keep(con, [("1000004", "2026-01-01T00:00:00+00:00", 1,
                      '{"date_generale": {"cui": 1000004, "statusRO_e_Factura": true}}')])
    con.commit()
    _inject(con, "main", REGISTER_STATS["anaf"], write="anaf" in ANALYSED)
    con.close()


@pytest.fixture(scope="module")
def files(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("plans")
    build_registry(d / "registry.db")
    build_financials(d / "financials.db")
    build_taxpayers(d / "taxpayers.db")
    build_anaf(d / "anaf.db")
    con = sqlite3.connect(d / "harvey25.db", isolation_level=None)
    overlay.create(con)
    _inject(con, "main", OVERLAY_STATS)
    con.close()
    return d


@pytest.fixture(scope="module")
def overlay_con(files):
    """The overlay with every register module's file attached, as the core
    would open it."""
    con = sqlite3.connect(files / "harvey25.db")
    adapters = registers.adapters(files)
    loaded = registers.attach_all(con, adapters)
    assert all(loaded.values()), loaded
    yield con
    con.close()


# ── Reading plans ──

_KEYWORDS = {"where", "on", "join", "left", "cross", "inner", "group", "order", "limit", "using",
             "natural", "union", "set", "values", "window", "outer", "as", "select", "and",
             "not", "indexed", "full", "right"}


def aliases(sql: str) -> dict[str, str]:
    """alias -> table name (without its schema) for the tables a statement names."""
    out = {}
    for table, alias in re.findall(r"(?i)(?:\bfrom|\bjoin|,)\s+([\w.]+)\s+(?:as\s+)?(\w+)", sql):
        name = table.split(".")[-1]
        out[name] = name
        if alias.lower() not in _KEYWORDS:
            out[alias] = name
    for table in re.findall(r"(?i)(?:\bfrom|\bjoin|,)\s+([\w.]+)", sql):
        out.setdefault(table.split(".")[-1], table.split(".")[-1])
    return out


def explain(con, sql: str, params=()) -> list[str]:
    return [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _schemas(con) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA database_list")]


def _pk_index(con, table: str) -> str | None:
    """The index a table's primary key is (WITHOUT ROWID or a text key)."""
    for schema in _schemas(con):
        for _, idx, _, origin, _ in con.execute(f"PRAGMA {schema}.index_list('{table}')"):
            if origin == "pk":
                return idx
    return None


def search_rows(con, step: str, table: str) -> int | None:
    """How many rows an index search reads by the statistics: the rows a
    value of its bound equality prefix matches. None when they can't say:
    a search with a range bound reads its matches, which depends on the
    data (the statistics hold no histogram), and today's unanalysed file
    has none."""
    m = re.match(r"SEARCH \S+ USING (?:(INTEGER PRIMARY KEY)|(?:COVERING )?INDEX (\w+)|"
                 r"(PRIMARY KEY)|(AUTOMATIC))[^(]*\((.*)\)\s*$", step)
    if not m:
        return None
    rowid, idx, pk, automatic, bound = m.groups()
    terms = [t.strip() for t in bound.split(" AND ")]
    eq = sum(bool(re.fullmatch(r"\w+=\?", t)) for t in terms)
    ranges = sum(bool(re.fullmatch(r"\w+[<>]\?", t)) for t in terms)
    if automatic:
        return TABLE_ROWS.get(table)        # built from the whole table first
    if ranges:
        return None
    if rowid:
        return 1
    name = idx or (_pk_index(con, table) if pk else None)
    stat = STAT.get(name or "")
    if not stat:
        return None
    return stat[eq] if eq < len(stat) else 1


def _columns(con, table: str) -> set[str]:
    out = set()
    for schema in _schemas(con):
        out |= {r[1] for r in con.execute(f"PRAGMA {schema}.table_info('{table}')")}
    return out


def _index_covers(con, idx: str, sql: str, table: str, names: dict) -> bool:
    """Every column the statement filters ``table`` on is in the index (or
    in its partial-index condition), so a walk in its order stops after the
    LIMIT's rows."""
    for schema in _schemas(con):
        row = con.execute(f"SELECT sql FROM {schema}.sqlite_master WHERE name = ?",
                          (idx,)).fetchone()
        if row:
            held = {r[2] for r in con.execute(f"PRAGMA {schema}.index_info('{idx}')")}
            partial = (row[0] or "").partition(" WHERE ")[2]
            break
    else:
        return False
    cols = _columns(con, table)
    held |= {w for w in re.findall(r"\b\w+\b", partial) if w in cols}
    where = re.split(r"(?i)\b(?:ORDER\s+BY|GROUP\s+BY|LIMIT)\b",
                     re.split(r"(?i)\bWHERE\b", sql, maxsplit=1)[-1])[0] \
        if re.search(r"(?i)\bWHERE\b", sql) else ""
    used = set()
    for alias, col in re.findall(r"\b(?:(\w+)\.)?(\w+)\b", where):
        if col in cols and (not alias or names.get(alias) == table):
            used.add(col)
    return used <= held


def findings(sql: str, plan: list[str], *, bounded: bool = False, drives: str = "",
             allowed: dict | None = None, con: sqlite3.Connection) -> set[str]:
    """What the plan does that the rules forbid: "scan:<table>" for a step
    that reads more than SMALL rows of a table (a scan, a big full-text
    index read without a match, an index search whose bound columns match
    that many, an index walked under a LIMIT on a filter it doesn't hold),
    "sort" for a temporary ORDER BY sort the statement doesn't bound (in a
    statement that reads a big table), "drives:<alias>" when it starts
    from another table than it spells. ``con``: the connection the plan
    came from (what an index holds, its primary key's index)."""
    names = aliases(sql)
    out = set()
    walk = bool(re.search(r"(?i)\bLIMIT\b", sql)) and \
        not any(p.startswith("USE TEMP B-TREE FOR ORDER BY") for p in plan)
    first = None
    for step in plan:
        m = re.match(r"(SCAN|SEARCH) ([\w.]+)", step)
        if m and first is None:
            first = m.group(2).split(".")[-1]
        if step.startswith("USE TEMP B-TREE FOR ORDER BY"):
            if not bounded and any(TABLE_ROWS.get(t, 0) > SMALL for t in set(names.values())):
                out.add("sort")
            continue
        if not m:
            continue
        alias = m.group(2).split(".")[-1]
        table = names.get(alias, alias)
        if "VIRTUAL TABLE" in step:
            if step.rstrip().endswith(":") and TABLE_ROWS.get(table, 0) > SMALL:
                out.add(f"scan:{table}")
            continue
        via = re.search(r"USING (?:COVERING )?INDEX (\w+)", step)
        if m.group(1) == "SEARCH":
            rows = search_rows(con, step, table)
        else:
            rows = INDEX_ROWS.get(via.group(1)) if via else None
            if rows is None:
                rows = TABLE_ROWS.get(table, 0)
        if rows is None or rows <= SMALL or table in (allowed or {}):
            continue
        if via and walk and _index_covers(con, via.group(1), sql, table, names):
            continue            # a page walked in index order, stopped by its LIMIT
        out.add(f"scan:{table}")
    if drives and first != drives:
        out.add(f"drives:{first}")
    return out


# ── Named statements ──

def _all_registered(files):
    # Every register adapter registers its statements when it is made.
    registers.adapters(files)
    return queries.named()


def test_every_named_statement_reads_by_index(files, overlay_con):
    bad = {}
    for st in _all_registered(files):
        alias = next((m.register["attach"] for m in load_manifests()
                      if m.id == st.module and m.register), "")
        sql = queries.sql(st.name, alias)
        try:
            plan = explain(overlay_con, sql, st.params)
        except sqlite3.Error as e:
            bad[st.name] = f"error: {e}"
            continue
        found = findings(sql, plan, bounded=st.bounded, drives=st.drives, allowed=st.scans,
                         con=overlay_con)
        if found:
            bad[st.name] = sorted(found) + plan
    assert bad == {}, "\n".join(f"{k}: {v}" for k, v in bad.items())


def test_register_values_reach_firms_only_through_keys(files):
    """The cross-file rule: a register is read by its key or an index its
    load built, and every read of the registry's firms by CUI goes through
    firm_by_cui, never firms.cui (a partial index a probe misses without
    ``cui != ''``)."""
    for st in _all_registered(files):
        if st.module == "onrc":
            assert not re.search(r"\bf\.cui\s*=(?!\s*'')", st.sql), st.name
    names = {st.name for st in queries.named()}
    for m in registers.declared():
        assert f"{m.id}.marks" in names or m.id == "anaf"
        for r in m.register["reads"]:
            assert f"{m.id}.read.{r['code']}" in names, (m.id, r["code"])


def test_the_plan_checker_catches_what_it_should(overlay_con):
    def check(sql, params=()):
        return findings(sql, explain(overlay_con, sql, params), con=overlay_con)

    assert check("SELECT * FROM firm f WHERE f.display_name = ?", ("x",)) == {"scan:firm"}
    assert "sort" in check("SELECT id FROM firm ORDER BY display_name LIMIT 5")
    assert check("SELECT f.id FROM reg.firms f WHERE f.cui = ?", ("1000004",)) == {"scan:firms"}
    assert check("SELECT f.id FROM reg.firms f WHERE f.cui = ? AND f.cui != ''",
                 ("1000004",)) == set()
    assert check("SELECT rowid FROM reg.firm_fts") == {"scan:firm_fts"}
    # A small partial index; a page walked in order.
    assert check("SELECT id FROM firm WHERE live = 0 AND n_contacts > 0 AND display_name > ''") \
        == set()
    assert check("SELECT id FROM firm WHERE live = 0 ORDER BY created DESC, id DESC LIMIT 50") \
        == set()
    # Index searches that still read most of a big table.
    assert check("SELECT fi.firm FROM firm_ident fi WHERE fi.scheme = ?", (1,)) \
        == {"scan:firm_ident"}
    assert check("SELECT c.cui FROM tax.current c WHERE c.vat = ?", (0,)) == {"scan:current"}
    assert check("SELECT fi.firm FROM firm_ident fi WHERE fi.scheme = ? AND fi.key = ?",
                 (1, 1)) == set()
    # A register range probing firm_ident by its scheme alone, once per row.
    assert check("SELECT fi.firm FROM fin.current c CROSS JOIN firm_ident fi "
                 "WHERE c.turnover >= ? AND fi.scheme = ? AND fi.key = c.cui", (1, 1)) \
        >= {"scan:firm_ident"}
    # A walk under LIMIT filtering on a column its index doesn't hold.
    assert check("SELECT id FROM firm WHERE live = 0 AND display_name = ? "
                 "ORDER BY created DESC, id DESC LIMIT 50", ("x",)) == {"scan:firm"}


def test_held_statements_probe_firm_ident_by_scheme_and_key(files, overlay_con):
    """A range's records in Harvey: each register key probed in firm_ident
    by (scheme, key), never by the scheme alone."""
    regs = {a.module: a for a in registers.adapters(files)}
    held = [st for st in queries.named() if ".held." in st.name]
    assert held
    for st in held:
        plan = explain(overlay_con, queries.sql(st.name, regs[st.module].alias), st.params)
        probe = [p for p in plan if p.startswith("SEARCH fi ")]
        assert probe and all("(scheme=? AND key=?)" in p for p in probe), (st.name, plan)


def test_adapters_answer_on_fixture_files(files, overlay_con):
    from harvey.modules.ro.mf_financials.queries import Adapter as Fin
    from harvey.modules.ro.mf_taxpayers.queries import Adapter as Tax
    from harvey.modules.ro.onrc.queries import Adapter as Reg

    reg, fin, tax = Reg("reg", files / "registry.db"), Fin("fin"), Tax("tax")
    firms = reg.describe(overlay_con, [1000004, 1000012, 99])
    assert sorted(firms) == [1000004, 1000012]
    assert firms[1000004]["activities"] == [("4321", 3), ("7112", 1), ("8623", 3)]
    assert firms[1000004]["administrators"] == [("Ana", "Exemplu", "administrator"),
                                                ("Ion", "Exemplu", "asociat")]
    assert reg.describe(overlay_con, ["J08/3/2015"], scheme="ro.onrc_reg")["J08/3/2015"]["name"] \
        == "EXEMPLU GAMA SRL"
    assert reg.range(overlay_con, "ACTIVITY_CODE", "=", "8623/3") == [1000004]
    # Current revisions only, in a read as in a range.
    assert reg.read(overlay_con, "ACTIVITY_CODE", [1000004]) == {
        1000004: [("4321/3", "2026-01-01"), ("8623/3", "2026-01-01")]}
    with pytest.raises(ValueError, match="revisions"):
        reg.range(overlay_con, "ACTIVITY_CODE", "=", "7112/1")
    # A firm with no CUI, by its registration number.
    assert reg.range(overlay_con, "ACTIVITY_CODE", "=", "4120/2", scheme="ro.onrc_reg") \
        == ["J08/3/2015"]
    assert reg.read(overlay_con, "ACTIVITY_CODE", ["J08/3/2015", "J08/1/2015"],
                    scheme="ro.onrc_reg") == {"J08/3/2015": [("4120/2", "2026-01-01")]}
    assert reg.keys_of_ids(overlay_con, [3, 1, 1]) == [("ro.onrc_reg", "J08/3/2015"),
                                                       ("ro.cui", 1000004)]
    assert reg.name_holders(overlay_con, "Exemplu Alfa S.R.L.", "Brașov, Brașov, Romania") == [1]
    assert fin.range(overlay_con, "TURNOVER", ">=", 1_000_000) == [1000012]
    assert fin.read(overlay_con, "EMPLOYEES", [1000004, 1000012]) == {
        1000004: [(4, 2024)], 1000012: [(12, 2024)]}
    assert sorted(tax.range(overlay_con, "HAS_EMPLOYEES_TAX", "=", 1)) == [1000004, 1000012]
    assert tax.describe(overlay_con, [1000004])[1000004]["name"] == "ALFA EXEMPLU SRL"
    assert fin.marks(overlay_con) and tax.marks(overlay_con) and reg.marks(overlay_con)
    con = sqlite3.connect(":memory:")
    assert fin.marks(con) is None
    # The records in Harvey in a range: through firm_ident, with the scheme's id.
    overlay_con.execute("INSERT INTO firm (id, created) VALUES (7, 1)")
    overlay_con.execute("INSERT INTO ident_scheme (id, name, module, numeric, legal) "
                        "VALUES (1, 'ro.cui', 1, 1, 1)")
    overlay_con.execute("INSERT INTO firm_ident VALUES (1, 1000012, 7, 1)")
    assert [r[0] for r in overlay_con.execute(fin.held_sql("TURNOVER", ">="), (10**6, 1))] == [7]
    assert overlay_con.execute(queries.sql("mf_financials.held_count.TURNOVER", "fin"),
                               (1,)).fetchone() == (1, 0)
    # Counted per firm: one firm with two activities and two administrators.
    overlay_con.execute("INSERT INTO firm (id, created) VALUES (8, 1)")
    overlay_con.execute("INSERT INTO firm_ident VALUES (1, 1000004, 8, 1)")
    for signal, expected in (("ACTIVITY_CODE", (1, 0)), ("CONTACT_FOUND", (1, 0)),
                             ("DECISION_MAKER_TITLE", (1, 0))):
        assert overlay_con.execute(queries.sql(f"onrc.held_count.{signal}", "reg"),
                                   (1,)).fetchone() == expected, signal
    overlay_con.rollback()


def test_a_stale_name_index_is_refused(files, overlay_con, monkeypatch):
    from harvey import name_keys
    from harvey.modules.ro.onrc.queries import Adapter as Reg
    from harvey.registers import Stale

    reg = Reg("reg", files / "registry.db")
    assert reg.name_holders(overlay_con, "Exemplu Alfa S.R.L.", "Brașov") == [1]
    monkeypatch.setattr(name_keys, "RULES", name_keys.RULES + 1)
    name_keys.version.cache_clear()
    try:
        with pytest.raises(Stale):
            reg.name_holders(overlay_con, "Exemplu Alfa S.R.L.", "Brașov")
    finally:
        monkeypatch.undo()
        name_keys.version.cache_clear()


def test_registry_resolve_gives_canonical_keys(files):
    from harvey.modules.ro.onrc.queries import Adapter as Reg

    reg = Reg("reg", files / "registry.db")
    assert reg.resolve({"words": "exemplu"}) == [("ro.cui", 1000004), ("ro.cui", 1000012),
                                                ("ro.onrc_reg", "J08/3/2015")]


# ── Today's pages (schema 24) ──

PAGES = {
    "today": ("GET", "/api/today", None),
    "live-status": ("GET", "/api/live-status", None),
    "overview": ("GET", "/api/overview", None),
    "signals": ("GET", "/api/signals", None),
    "cohort.efactura": ("POST", "/api/cohort", {"require": ["EFACTURA_REGISTERED"]}),
    "cohort.turnover": ("POST", "/api/cohort",
                        {"ranges": [{"code": "TURNOVER", "min": 1000000}]}),
    "companies": ("GET", "/api/companies?limit=50", None),
    "companies.offset": ("GET", "/api/companies?limit=50&offset=2", None),
    "companies.county": ("GET", "/api/companies?county=Bra%C8%99ov&limit=50", None),
    "companies.no_contact": ("GET", "/api/companies?contact=none&limit=50", None),
    "companies.search": ("GET", "/api/companies?search=exemplu&limit=50", None),
    "companies.turnover": ("GET", "/api/companies?sort=turnover&limit=50", None),
    "companies.panel": ("GET", "/api/companies/{firm}", None),
    "contacts": ("GET", "/api/contacts?limit=50", None),
    "contacts.by_companies": ("GET", "/api/contacts?sort=companies&limit=50", None),
    "contacts.mobile_county": ("GET", "/api/contacts?kind=phone&contact=mobile&county=Bra%C8%99ov",
                               None),
    "people": ("GET", "/api/contacts?view=people&limit=50", None),
    "finder.backlog": ("GET", "/api/contacts/finder", None),
    "registry": ("GET", "/api/registry?limit=50", None),
    "duplicates": ("GET", "/api/duplicates", None),
    "jobs": ("GET", "/api/jobs", None),
    "activity": ("GET", "/api/activity", None),
    "outbox": ("GET", "/api/outbox", None),
}

# What today's pages scan or sort that the rules forbid, page by page:
# the list later work shrinks to empty (on the real database these are the
# pages that take seconds).
KNOWN_V24: dict[str, set[str]] = {
    "today": {"scan:companies", "scan:observations"},
    "live-status": {"scan:companies"},
    "overview": {"scan:companies", "scan:company_activities"},
    "signals": {"scan:companies", "scan:observations", "sort"},
    "cohort.efactura": {"scan:companies", "sort"},
    "cohort.turnover": {"scan:companies", "sort"},
    "companies": {"scan:companies", "sort"},
    "companies.offset": {"scan:companies"},
    "companies.county": {"scan:companies", "sort"},
    "companies.no_contact": {"scan:companies", "scan:company_contacts", "sort"},
    "companies.search": {"scan:companies", "sort"},
    "companies.turnover": {"scan:companies", "sort"},
    "companies.panel": {"sort"},
    "contacts": {"scan:company_contacts", "scan:contacts"},
    "contacts.by_companies": {"scan:company_contacts", "scan:contacts", "sort"},
    "contacts.mobile_county": {"scan:company_contacts", "scan:contacts", "sort"},
    "people": {"scan:contacts", "scan:people"},
    "finder.backlog": {"scan:companies"},
    "registry": {"scan:companies"},
}


class Tracer:
    """Every SELECT any connection runs (with its parameters filled in),
    with the files that connection had attached."""

    def __init__(self):
        self.lock = threading.Lock()
        self.page = ""
        self.seen: dict[str, list] = {}
        self.real = sqlite3.connect

    def connect(self, *args, **kwargs):
        con = self.real(*args, **kwargs)
        attached: list[str] = []
        opened = (args, {k: v for k, v in kwargs.items() if k in ("uri",)})

        def trace(sql: str):
            text = sql.strip()
            if text.upper().startswith("ATTACH"):
                attached.append(text)
            elif re.match(r"(?is)^(select|with)\b", text) and self.page:
                with self.lock:
                    self.seen.setdefault(self.page, []).append((opened, tuple(attached), text))

        con.set_trace_callback(trace)
        return con


def _seed_v24(path: Path) -> dict:
    from harvey.models import Company
    from harvey.signals import sync_vocabulary
    from harvey.state import StateManager

    async def go():
        s = StateManager(str(path))
        await s.init_db()
        await sync_vocabulary(s)
        for code in ("EFACTURA_REGISTERED", "TURNOVER", "PHONE_NUMBER"):
            await s.set_signal_status(code, "confirmed")
        ids = []
        for i, (name, cui) in enumerate((("ALFA EXEMPLU SRL", "1000004"),
                                         ("BETA EXEMPLU SRL", "1000012"))):
            ids.append(await s.add_company(Company(
                name=name, cui=cui, location="Brașov, Brașov, Romania",
                address=f"Str. Exemplului nr. {i + 1}", source="registry:onrc",
                external_id=f"onrc:J08/{i + 1}/2015",
                website="https://alfa.example.com" if i == 0 else "")))
        await s.add_observations([{"company_id": ids[0], "signal_code": "TURNOVER",
                                   "value_num": 250000.0, "value_text": "2024"}])
        return {"firm": ids[0]}

    return asyncio.run(go())


def _explain_traced(tracer: Tracer) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    cons: dict = {}
    for page, rows in tracer.seen.items():
        found = out.setdefault(page, set())
        for opened, attached, sql in rows:
            key = (repr(opened), attached)
            if key not in cons:
                args, kwargs = opened
                con = tracer.real(*args, **kwargs)
                for stmt in attached:
                    try:
                        con.execute(stmt)
                    except sqlite3.Error:
                        pass
                cons[key] = con
            try:
                plan = explain(cons[key], sql)
            except sqlite3.Error:
                continue            # a statement of a temporary table gone since
            found |= findings(sql, plan, con=cons[key])
    for con in cons.values():
        con.close()
    return out


def test_todays_pages_scan_only_what_is_known(files, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    from harvey import companies, registry, registry_view
    from harvey.modules.ro.anaf import service as anaf
    from harvey.modules.ro.mf_financials import store as fin
    from harvey.modules.ro.mf_taxpayers import store as tax

    db = tmp_path / "harvey.db"
    ids = _seed_v24(db)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(fin, "FINANCIALS_PATH", files / "financials.db")
    monkeypatch.setattr(tax, "TAXPAYERS_PATH", files / "taxpayers.db")
    monkeypatch.setattr(anaf, "CACHE_PATH", files / "anaf.db")
    monkeypatch.setattr(registry, "REGISTRY_PATH", files / "registry.db")
    monkeypatch.setattr(registry_view, "WARM_FIRMS", 10**9)    # no background warm-up
    companies.SNAPSHOTS.clear()
    companies.RANKINGS.clear()
    tracer = Tracer()
    monkeypatch.setattr(sqlite3, "connect", tracer.connect)
    token = dash.token_store.get()
    client = TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"})
    for page, (method, url, body) in PAGES.items():
        tracer.page = page
        r = client.request(method, url.format(**ids), json=body)
        assert r.status_code < 500, (page, r.status_code, r.text[:300])
    tracer.page = ""
    monkeypatch.setattr(sqlite3, "connect", tracer.real)
    companies.SNAPSHOTS.clear()
    companies.RANKINGS.clear()
    found = {p: f for p, f in _explain_traced(tracer).items() if f}
    assert set(PAGES) <= set(tracer.seen) | {"jobs", "outbox"}, sorted(set(PAGES) - set(tracer.seen))
    new = {p: sorted(f - KNOWN_V24.get(p, set())) for p, f in found.items()
           if f - KNOWN_V24.get(p, set())}
    gone = {p: sorted(k - found.get(p, set())) for p, k in KNOWN_V24.items()
            if k - found.get(p, set())}
    assert new == {}, f"new scans or sorts (fix them; never add here): {new}"
    assert gone == {}, f"no longer happen: remove from KNOWN_V24: {gone}"


# ── No SQL in the dashboard outside named statements ──

# Statements still written in dashboard.py: moved into named statements as
# the pages move over to the overlay, until none is left.
DASHBOARD_SQL_LINES = 109


def test_dashboard_sql_only_shrinks():
    text = (Path(__file__).resolve().parent.parent / "harvey" / "dashboard.py").read_text(
        encoding="utf-8")
    n = len(re.findall(r"\b(?:SELECT|INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", text))
    assert n <= DASHBOARD_SQL_LINES, f"dashboard.py has {n} statements (was {DASHBOARD_SQL_LINES})"
