"""The table the taxpayer file's load builds for the core: ``current``, each
taxpayer's flags and phone keyed by its CUI as an integer, with an index on
each flag (a cohort condition on VAT, employer or micro-enterprise tax is an
index search). A flag the file leaves empty stays NULL: unknown.

Built inside taxpayers.db by its load (store.build) and by ``reindex()`` for
a file loaded before; ``build()`` can write into another file with
taxpayers.db attached read-only.
"""

from __future__ import annotations

from pathlib import Path

from harvey.registers import build_derived

FLAGS = ("vat", "employer", "micro")
CUI_OK = "t.cui GLOB '[1-9]*' AND t.cui NOT GLOB '*[^0-9]*' AND length(t.cui) <= 12"


def build(con, src: str = "main", dst: str = "main") -> dict:
    con.execute(f"DROP TABLE IF EXISTS {dst}.current")
    con.execute(f"CREATE TABLE {dst}.current (cui INTEGER PRIMARY KEY, vat INTEGER, "
                "employer INTEGER, micro INTEGER, phone TEXT)")
    con.execute(f"""
        INSERT INTO {dst}.current (cui, vat, employer, micro, phone)
        SELECT CAST(t.cui AS INTEGER), t.vat, t.employer, t.micro_tax, NULLIF(t.phone, '')
        FROM {src}.taxpayers t WHERE {CUI_OK} ORDER BY 1""")
    for f in FLAGS:
        con.execute(f"CREATE INDEX {dst}.current_{f} ON current({f}) WHERE {f} IS NOT NULL")
    q = con.execute
    return {"current": q(f"SELECT COUNT(*) FROM {dst}.current").fetchone()[0],
            "cui_not_keyable": q(f"SELECT COUNT(*) FROM {src}.taxpayers t "
                                 f"WHERE NOT ({CUI_OK})").fetchone()[0]}


def reindex(path: Path, target: Path | None = None) -> dict:
    """Build ``current`` into taxpayers.db at ``path`` (or into ``target``,
    leaving it untouched)."""
    return build_derived(path, build, target, tables=("current",))
