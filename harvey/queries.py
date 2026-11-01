"""Named statements: every query a page or a register adapter runs, by name.

A statement is registered once, where its code lives (core statements in
harvey/overlay/queries.py, a register module's in its own queries.py), and
read back by name wherever it runs. tests/test_query_plans.py runs EXPLAIN
QUERY PLAN on each against fixture databases shaped like the real ones
(every register attached, production-sized statistics) and fails on a scan
of a big table, a sort that isn't bounded, or a register read that isn't an
index search.

``{alias}`` in a register module's statement is its attach alias, filled
in by ``sql()`` (``{derived}``: where its load's derived tables are, the
same file unless built elsewhere): the core never names a register's
tables itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Statement:
    name: str
    sql: str
    # Example parameters the plan test binds (the plan, not the rows, is
    # what is checked: any value of the right type will do).
    params: tuple = ()
    # The module whose register the statement reads ("" for the core's).
    module: str = ""
    # The statement's ORDER BY sorts a set bounded by its own LIMIT or a
    # key list (a page's 50 rows): a temporary b-tree is fine there.
    bounded: bool = False
    # The table the statement must start from (a forced join order, written
    # with CROSS JOIN): the plan test checks the plan's first table.
    drives: str = ""
    # Big tables this statement may scan on purpose (a full recount, a
    # rebuild), each with why.
    scans: dict = field(default_factory=dict)


REGISTRY: dict[str, Statement] = {}


def statement(name: str, sql: str, *, params: tuple = (), module: str = "",
              bounded: bool = False, drives: str = "", scans: dict | None = None) -> Statement:
    """Register a statement under ``name`` (once: a second registration of
    the same name with other text is a mistake) and return it."""
    st = Statement(name, " ".join(sql.split()), tuple(params), module, bounded, drives,
                   dict(scans or {}))
    old = REGISTRY.get(name)
    if old is not None and old != st:
        raise ValueError(f"statement {name!r} is registered twice with different text")
    REGISTRY[name] = st
    return st


def get(name: str) -> Statement:
    return REGISTRY[name]


def sql(name: str, alias: str = "", derived: str = "") -> str:
    """The statement's text, with a register module's ``{alias}`` filled in,
    and ``{derived}`` (the tables its load builds: in the register's own
    file, so the same alias, unless built elsewhere) too."""
    text = REGISTRY[name].sql
    if alias:
        text = text.replace("{derived}", derived or alias).replace("{alias}", alias)
    return text


def named(prefix: str = "") -> list[Statement]:
    """Every registered statement whose name starts with ``prefix``, by name."""
    return [REGISTRY[n] for n in sorted(REGISTRY) if n.startswith(prefix)]
