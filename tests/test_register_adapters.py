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
    assert m.identifiers == ({"name": "example.tax_id", "numeric": True, "legal": True,
                              "legacy": ""},)
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
    assert con.execute("SELECT name, numeric, legal FROM ident_scheme WHERE name LIKE 'ro.%' "
                       "ORDER BY name").fetchall() == [("ro.cui", 1, 1), ("ro.onrc_reg", 0, 0)]
    # Listing sources declare how their listings are known again (never legal).
    assert set(ids) == {"ro.cui", "ro.onrc_reg", "osm.element", "dataforseo.listing",
                        "scavio.place"}
    assert con.execute("SELECT COUNT(*) FROM ident_scheme WHERE legal = 1").fetchone()[0] == 1


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


def _registry(path):
    from harvey import registry
    from harvey.modules.ro.onrc import derived

    con = sqlite3.connect(path)
    con.executescript(registry.SCHEMA)
    rows = [  # reg, cui, form, registered, operating
        ("J08/1/2015", "1000004", "SRL", "01/02/2015", 1),    # filed 2024: yes
        ("J08/2/2015", "1000012", "SRL", "01/02/2015", 1),    # last filed 2022: no
        ("J08/3/2015", "1000020", "SRL", "01/02/2015", 1),    # never filed: no
        ("J08/4/2015", "1000039", "PFA", "01/02/2015", 1),    # a sole trader: unknown
        ("J08/5/2025", "1000047", "SRL", "03/04/2025", 1),    # registered after: unknown
        ("J08/6/2010", "1000055", "SRL", "01/02/2010", 0),    # struck off: unknown
        ("J08/7/2010", "1000063", "SRL", "01/02/2010", 0),    # struck off, filed 2021: yes
    ]
    con.executemany("INSERT INTO firms (reg, name, cui, form, registered, operating) "
                    "VALUES (?, 'EXEMPLU SRL', ?, ?, ?, ?)", rows)
    con.execute("INSERT INTO meta VALUES ('dataset', 'firme-01-10-2026')")
    con.commit()
    con.close()
    derived.reindex(path)


def test_filed_accounts_says_no_only_for_a_firm_that_should_have_filed(tmp_path):
    from harvey.modules.ro.mf_financials import derived, store

    path = tmp_path / "financials.db"
    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.executemany("INSERT INTO accounts (cui, year, form, turnover) VALUES (?, ?, 'UU', 1)",
                    [("1000004", 2024), ("1000012", 2022), ("1000063", 2021),
                     ("1000039", 2023)])
    # 2024 loaded in full and final (every form, revised), 2025 not.
    con.executemany("INSERT INTO files (year, form, package, published) VALUES (?, ?, ?, ?)",
                    [(2024, f, "situatii_2024_actualizat", "2026-06-01") for f in store.FORMS]
                    + [(2025, "UU", "situatii_2025", "2026-06-01")])
    con.commit()
    con.close()
    # Without the registry nothing can say no.
    counts = derived.reindex(path)
    assert (counts["filed_yes"], counts["filed_no"], counts["filed_from"]) == (4, 0, "none")
    reg = tmp_path / "registry.db"
    _registry(reg)
    counts = derived.reindex(path, registry=reg)
    con = sqlite3.connect(path)
    assert dict((c, (y, v)) for c, y, v in con.execute("SELECT cui, year, filed FROM filed")) == {
        1000004: (2024, 1), 1000012: (2024, 0), 1000020: (2024, 0), 1000039: (2023, 1),
        1000063: (2021, 1)}
    assert counts["filed_from"].startswith("registry firme-01-10-2026")
    assert con.execute("SELECT version FROM derived_meta WHERE name = 'filed'").fetchone()[0] == \
        counts["filed_from"]
    con.close()


def test_anaf_reads_the_compressed_file_and_counts_a_plain_one_not_loaded(tmp_path):
    import json

    from harvey.modules.ro.anaf import queries as anaf_queries
    from harvey.modules.ro.anaf import service, store

    item = {"date_generale": {"cui": 1000004, "telefon": "0268 000 210",
                              "stare_inregistrare": "INREGISTRAT din data 01.03.2015",
                              "statusRO_e_Factura": True},
            "inregistrare_scop_Tva": {"scpTVA": True, "perioade_TVA": [
                {"data_inceput_ScpTVA": "2019-05-01", "data_sfarsit_ScpTVA": ""}]},
            "adresa_sediu_social": {"sdenumire_Strada": "Str. Exemplu", "snumar_Strada": "1",
                                    "sdenumire_Localitate": "Mun. Braşov"}}
    plain = tmp_path / "plain" / "anaf.db"
    service.keep(plain, {"1000004": item}, set(), "2026-10-01T08:00:00+00:00")
    new = tmp_path / "anaf.db"
    con = sqlite3.connect(new)
    store.create(con)
    store.keep(con, [("1000004", "2026-10-01T08:00:00+00:00", 1, json.dumps(item)),
                     ("1000012", "2026-10-01T08:00:00+00:00", 0, "")])
    con.commit()
    con.close()
    overlay = sqlite3.connect(":memory:", uri=True)
    schema.create(overlay, triggers=False)
    ids = schema.sync_identifiers(overlay)
    overlay.executemany("INSERT INTO firm (id, created) VALUES (?, 1)", [(1,), (2,)])
    overlay.executemany("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, 1)",
                        [(ids["ro.cui"], 1000004, 1), (ids["ro.cui"], 1000012, 2)])
    assert anaf_queries.Adapter("anaf", plain, keyed_by="ro.cui").attach(overlay) is False
    a = anaf_queries.Adapter("anaf", new, keyed_by="ro.cui")
    assert a.attach(overlay) is True
    got = {code: {f: v for f, v, _ in a.held_values(overlay, code, ids["ro.cui"])}
           for code in ("PHONE_NUMBER", "ADDRESS", "REGISTRATION_STATUS", "VAT_SINCE",
                        "VAT_REGISTERED", "EFACTURA_REGISTERED")}
    assert got["PHONE_NUMBER"] == {1: "+40268000210"}
    assert got["ADDRESS"][1].startswith("Str. Exemplu 1")
    assert got["REGISTRATION_STATUS"] == {1: "INREGISTRAT"}
    assert got["VAT_SINCE"] == {1: "2019-05-01"}
    assert got["VAT_REGISTERED"] == {1: 1, 2: 0}               # ANAF didn't know 2: no
    assert got["EFACTURA_REGISTERED"] == {1: 1, 2: 0}
    _, _, date = next(iter(a.held_values(overlay, "VAT_REGISTERED", ids["ro.cui"])))
    assert date == "2026-10-01T08:00:00+00:00"
    assert a.describe(overlay, [1000004])[1000004]["answer"] == item


def test_the_taxpayer_file_reads_the_address_as_one_line(tmp_path):
    from harvey.modules.ro.mf_taxpayers import derived, store
    from harvey.modules.ro.mf_taxpayers import queries as tax_queries

    path = tmp_path / "taxpayers.db"
    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    row = {"cui": "1000004", "street": "Str. Exemplu", "number": "7", "block": "A",
           "apartment": "3", "locality": "Brașov", "county": "Brașov", "postal_code": "500001"}
    con.execute(f"INSERT INTO taxpayers ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()))
    con.execute("INSERT INTO taxpayers (cui, name) VALUES ('1000012', 'EXEMPLU DOI SRL')")
    con.execute("INSERT INTO meta VALUES ('published', '2026-07-01')")
    con.commit()
    con.close()
    derived.reindex(path)
    overlay = sqlite3.connect(":memory:", uri=True)
    schema.create(overlay, triggers=False)
    ids = schema.sync_identifiers(overlay)
    overlay.executemany("INSERT INTO firm (id, created) VALUES (?, 1)", [(1,), (2,)])
    overlay.executemany("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, 1)",
                        [(ids["ro.cui"], 1000004, 1), (ids["ro.cui"], 1000012, 2)])
    a = tax_queries.Adapter("tax", path, keyed_by="ro.cui")
    assert a.attach(overlay)
    got = {f: v for f, v, _ in a.held_values(overlay, "ADDRESS", ids["ro.cui"])}
    # The same parts store.address() joins (it also leaves out a repeated
    # one, like a county named as its town).
    assert got == {1: "Str. Exemplu nr. 7, bl. A, ap. 3, Brașov, Brașov, 500001"}
    assert store.address({**row, "number": "7"}) == "Str. Exemplu nr. 7, bl. A, ap. 3, Brașov, 500001"


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
