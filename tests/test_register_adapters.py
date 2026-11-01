"""Registers as modules: manifest declarations, the derived tables each
register's load builds, and the overlay's ``harvey doctor --recount``.
All data is fictional."""

import argparse
import sqlite3
from pathlib import Path

import pytest

from harvey import registers
from harvey.modules.loader import ManifestError, cross_check, load_manifests, read_manifest
from harvey.overlay import schema


def _manifest(tmp_path: Path, mid: str, body: str):
    folder = tmp_path / "example" / mid
    folder.mkdir(parents=True)
    path = folder / "module.toml"
    path.write_text(f'id = "{mid}"\nset = "example"\nkind = "source"\nversion = "0.1.0"\n'
                    f'api = 1\n{body}', encoding="utf-8")
    return read_manifest(path)


REGISTER = '''
[[identifiers]]
name = "example.tax_id"
numeric = true
legal = true

[register]
attach = "exr"
file = "example.db"
keyed_by = "example.tax_id"
adapter = "harvey.modules.ro.mf_financials.queries"

[[register.reads]]
code = "EXAMPLE_SIGNAL"
absent = "false"
precedence = 2

[[signals]]
code = "EXAMPLE_SIGNAL"
value_type = "bool"
'''


def test_a_manifest_declares_identifiers_and_its_register(tmp_path):
    m = _manifest(tmp_path, "example_register", REGISTER)
    assert m.identifiers == ({"name": "example.tax_id", "numeric": True, "legal": True},)
    assert m.register["attach"] == "exr" and m.register["keyed_by"] == "example.tax_id"
    assert m.register["reads"] == ({"code": "EXAMPLE_SIGNAL", "absent": "false",
                                    "precedence": 2},)
    assert cross_check([m]) == []


@pytest.mark.parametrize("bad, said", [
    ('[[identifiers]]\nname = "TaxId"\nnumeric = true\nlegal = true\n', "dotted lower-case"),
    ('[[identifiers]]\nname = "example.id"\nlegal = true\n', "numeric = true or false"),
    ('[register]\nattach = "main"\nfile = "x.db"\nkeyed_by = "example.id"\n'
     'adapter = "harvey.x"\n', "not main or temp"),
    ('[register]\nattach = "x"\nfile = "../x.db"\nkeyed_by = "example.id"\n'
     'adapter = "harvey.x"\n', "ending in .db"),
    ('[register]\nattach = "x"\nfile = "x.db"\nkeyed_by = "example.id"\nadapter = "os.path"\n'
     '[[register.reads]]\ncode = "X"\nabsent = "maybe"\n', "adapter must be"),
])
def test_bad_register_declarations_are_refused(tmp_path, bad, said):
    with pytest.raises(ManifestError) as e:
        _manifest(tmp_path, "example_bad", bad)
    assert said in str(e.value)


def test_cross_check_wants_declared_schemes_signals_and_one_alias(tmp_path):
    a = _manifest(tmp_path, "example_a", REGISTER)
    b = _manifest(tmp_path, "example_b", REGISTER.replace("[[signals]]\ncode = \"EXAMPLE_SIGNAL\"\n"
                                                           "value_type = \"bool\"\n", "")
                  .replace('keyed_by = "example.tax_id"', 'keyed_by = "example.other"')
                  .replace("[[identifiers]]", "[[identifiers_unused]]"))
    problems = cross_check([a, b])
    assert any("alias 'exr' is already used" in p for p in problems)
    assert any("'example.other' is not an identifier" in p for p in problems)


def test_the_romanian_registers_are_declared_by_their_modules():
    mans = {m.id: m for m in load_manifests()}
    assert {i["name"] for i in mans["onrc"].identifiers} == {"ro.cui", "ro.onrc_reg"}
    assert {m.id: m.register["attach"] for m in registers.declared()} == {
        "onrc": "reg", "mf_financials": "fin", "mf_taxpayers": "tax", "anaf": "anaf"}
    con = sqlite3.connect(":memory:")
    schema.create(con, triggers=False)
    ids = schema.sync_identifiers(con)
    assert con.execute("SELECT name, numeric, legal FROM ident_scheme ORDER BY name").fetchall() \
        == [("ro.cui", 1, 1), ("ro.onrc_reg", 0, 0)]
    assert set(ids) == {"ro.cui", "ro.onrc_reg"}


# ── Derived tables ──

def test_the_canonical_row_is_the_operating_one_then_the_newest(tmp_path):
    from harvey import registry
    from harvey.modules.ro.onrc import derived

    path = tmp_path / "registry.db"
    con = sqlite3.connect(path)
    con.executescript(registry.SCHEMA)
    rows = [  # reg, cui, registered, operating
        ("J01/1/2001", "1000004", "01/01/2001", 0),
        ("J01/2/2019", "1000004", "05/06/2019", 1),     # operating: canonical
        ("J01/3/2010", "1000012", "01/01/2010", 0),
        ("J01/4/2015", "1000012", "01/01/2015", 0),     # newest: canonical
        ("J01/5/2015", "", "01/01/2015", 1),
        ("J01/7/2016", "1000020", "02/02/2016", 0),     # a tie: the higher number
        ("J01/6/2016", "1000020", "02/02/2016", 0),     # (not the later row id)
    ]
    con.executemany("INSERT INTO firms (reg, name, cui, registered, operating, locality) "
                    "VALUES (?, 'EXEMPLU TEST SRL', ?, ?, ?, 'Brașov')", rows)
    con.commit()
    con.close()
    before = path.read_bytes()
    target = tmp_path / "out" / "derived.db"
    target.parent.mkdir()
    counts = derived.reindex(path, target)
    assert path.read_bytes() == before                  # the register was only read
    assert counts["firm_by_cui"] == 3 and counts["bytes"] > 0
    out = sqlite3.connect(target)
    assert out.execute("SELECT cui, firm_id FROM firm_by_cui ORDER BY cui").fetchall() == \
        [(1000004, 2), (1000012, 4), (1000020, 6)]
    assert out.execute("SELECT key, locality, firm_id FROM name_key ORDER BY firm_id").fetchall() \
        == [("exemplu test", "brasov", 2), ("exemplu test", "brasov", 5)]
    from harvey import name_keys

    assert out.execute("SELECT version FROM derived_meta WHERE name = 'name_key'").fetchone() \
        == (name_keys.version(),)
    out.close()
    derived.reindex(path)                               # in place, as a load does
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM firm_by_cui").fetchone() == (3,)


def test_an_in_place_reindex_that_fails_leaves_the_register_as_it_was(tmp_path):
    """Journalled, one transaction: a build bigger than the cache that
    fails part-way rolls back, and only the built tables are analysed."""
    from harvey.registers import build_derived

    path = tmp_path / "register.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, v)")
    con.execute("CREATE INDEX rows_v ON rows(v)")
    con.execute("CREATE TABLE built (id INTEGER PRIMARY KEY, v INTEGER NOT NULL)")
    con.executemany("INSERT INTO rows VALUES (?, ?)", [(i, i % 7) for i in range(1, 2001)])
    con.executemany("INSERT INTO built VALUES (?, ?)", [(i, i) for i in range(1, 1001)])
    con.commit()
    con.close()

    def failing(con, src, dst):
        con.execute(f"DROP TABLE {dst}.built")
        con.execute(f"CREATE TABLE {dst}.built (id INTEGER PRIMARY KEY, v INTEGER NOT NULL)")
        con.execute("PRAGMA cache_size=50")             # a build bigger than the cache
        con.execute(f"WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n "
                    f"WHERE x < 100000) INSERT INTO {dst}.built SELECT x, x FROM n")
        raise RuntimeError("stopped part-way")

    with pytest.raises(RuntimeError, match="part-way"):
        build_derived(path, failing, tables=("built",))
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert con.execute("SELECT COUNT(*) FROM built").fetchone() == (1000,)

    def ok(con, src, dst):
        con.execute(f"DROP TABLE {dst}.built")
        con.execute(f"CREATE TABLE {dst}.built AS SELECT id, v FROM {src}.rows")
        return {}

    con.close()
    build_derived(path, ok, tables=("built",))
    con = sqlite3.connect(path)
    assert {r[0] for r in con.execute("SELECT tbl FROM sqlite_stat1")} == {"built"}
    assert con.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_a_register_attaches_read_only(tmp_path):
    from harvey.registers import attach_read_only

    path = tmp_path / "register.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.commit()
    con = sqlite3.connect(":memory:", uri=True)
    attach_read_only(con, "r", path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("INSERT INTO r.t VALUES (1)")


def test_current_figures_take_each_figure_from_its_newest_year():
    from harvey.modules.ro.mf_financials.derived import current_row

    def year(y, **kw):
        base = dict(cui="1000004", year=y, form="UU", turnover=None, employees=None,
                    net_profit=None, net_loss=None, debts=None, equity=None,
                    fixed_assets=None, current_assets=None, prepaid_expenses=None)
        return {**base, **kw}

    row, mixed = current_row([
        year(2022, turnover=100, employees=3),
        year(2023, turnover=150, employees=4, net_profit=10, fixed_assets=5, current_assets=7),
        year(2024, form="BL", employees=5, net_loss=2, debts=9),
    ])
    from harvey.modules.ro.mf_financials.derived import COLUMNS

    v = dict(zip(COLUMNS, row))
    assert (v["cui"], v["year"], v["form"]) == (1000004, 2024, "BL")
    assert (v["turnover"], v["employees"], v["net_result"], v["debts"], v["equity"],
            v["total_assets"]) == (150, 5, -2, 9, None, 12)
    assert v["turnover_growth"] == 50.0 and mixed
    # Each figure says which year it comes from.
    assert (v["turnover_year"], v["employees_year"], v["net_result_year"], v["debts_year"],
            v["equity_year"], v["total_assets_year"], v["turnover_growth_year"]) == \
        (2023, 2024, 2024, 2024, None, 2023, 2023)
    # Growth over the year just before only; an older pair is dated by its year.
    row, _ = current_row([year(2020, turnover=100), year(2021, turnover=300),
                          year(2023, turnover=50)])
    v = dict(zip(COLUMNS, row))
    assert (v["turnover"], v["turnover_year"]) == (50, 2023)
    assert (v["turnover_growth"], v["turnover_growth_year"]) == (200.0, 2021)


def test_taxpayers_current_keeps_unknown_flags_unknown(tmp_path):
    from harvey.modules.ro.mf_taxpayers import derived, store

    path = tmp_path / "taxpayers.db"
    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.executemany("INSERT INTO taxpayers (cui, vat, employer, micro_tax, phone) VALUES (?,?,?,?,?)",
                    [("1000004", 1, None, 0, "0268000000"), ("1000012", 0, 1, None, ""),
                     ("0123", 1, 1, 1, "")])
    con.commit()
    con.close()
    counts = derived.reindex(path)
    assert counts["current"] == 2 and counts["cui_not_keyable"] == 1
    con = sqlite3.connect(path)
    assert con.execute("SELECT * FROM current ORDER BY cui").fetchall() == [
        (1000004, 1, None, 0, "0268000000"), (1000012, 0, 1, None, None)]


# ── harvey doctor --recount ──

def _doctor(path, **kw):
    from harvey import cli

    args = argparse.Namespace(recount=True, fix=False, db=str(path), **kw)
    cli.cmd_doctor(args)


def test_doctor_recount_says_when_kept_values_match_and_fixes_them(tmp_path, capsys):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path, isolation_level=None)
    schema.create(con)
    f = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    c = con.execute("INSERT INTO contact (kind, value, first_seen, last_seen) "
                    "VALUES (1, 'ana@example.com', 1, 1)").lastrowid
    con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, last_seen) "
                "VALUES (?, ?, 1, 0, 1, 1)", (f, c))
    con.close()
    _doctor(path)
    assert "Every kept value matches" in capsys.readouterr().out
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("UPDATE firm SET n_contacts = 5")
    con.close()
    with pytest.raises(SystemExit) as e:
        _doctor(path)
    assert e.value.code == 1
    assert "rows differ" in capsys.readouterr().out
    from harvey import cli

    cli.cmd_doctor(argparse.Namespace(recount=True, fix=True, db=str(path)))
    assert "Fixed" in capsys.readouterr().out
    _doctor(path)


def test_doctor_leaves_a_schema_24_database_alone(tmp_path, capsys):
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute("PRAGMA user_version = 24")
    with pytest.raises(SystemExit) as e:
        _doctor(path)
    assert e.value.code == 2
    assert "schema 24" in capsys.readouterr().out
