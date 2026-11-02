"""A schema 24 database (Harvey before the slim schema 25) for the
upgrade's tests: the frozen empty schema the old migrations made
(fixtures/schema_v24.sql) and the signal vocabulary as the old app wrote it.
Harvey itself no longer writes schema 24: only the upgrade reads it."""

import asyncio
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("fixtures") / "schema_v24.sql"

_UPSERT = """INSERT INTO signal_codes
       (code, label, description, category, value_type, collector, cost_note,
        confidence_floor, status, module, subject, unit, cardinality, keyed_by, absence,
        when_known, available)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)
   ON CONFLICT(code) DO NOTHING"""

_DEFAULTS = dict(label="", description="", category="", value_type="text", collector="",
                 cost_note="", confidence_floor=0.0, module="", subject="company", unit="",
                 cardinality="one", keyed_by="", absence="unknown", when_known="enrich",
                 available=True)


class _Capture:
    """Stands in for the state sync_vocabulary writes to: keeps its rows."""

    def __init__(self):
        self.rows, self.kinds = [], []

    async def write_vocabulary(self, rows_for, settings_prefix):
        self.rows = rows_for({})
        return len(self.rows)

    async def write_contact_kinds(self, rows_for, settings_prefix):
        self.kinds = rows_for({})


def vocabulary() -> _Capture:
    from harvey.signals import sync_vocabulary

    cap = _Capture()
    asyncio.run(sync_vocabulary(cap))
    return cap


def build(path) -> None:
    """An empty schema 24 database at ``path`` with the vocabulary."""
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        cap = vocabulary()
        for k in cap.kinds:
            con.execute("INSERT OR IGNORE INTO contact_kinds (kind, label, channel, module, "
                        "available) VALUES (?, ?, ?, ?, ?)",
                        (k["kind"], k.get("label", k["kind"]), k.get("channel", ""),
                         k["module"], 1 if k["available"] else 0))
        for row in cap.rows:
            r = {**_DEFAULTS, **row}
            con.execute(_UPSERT, (r["code"], r["label"], r["description"], r["category"],
                                  r["value_type"], r["collector"], r["cost_note"],
                                  float(r["confidence_floor"]), r["module"], r["subject"],
                                  r["unit"], r["cardinality"], r["keyed_by"], r["absence"],
                                  r["when_known"], 1 if r["available"] else 0))
        con.commit()
    finally:
        con.close()
