"""ANAF's kept answers' adapter: how the core reads data/anaf.db.

The core reads the compressed file (store.py, version 2), which the
database upgrade writes: every signal is a column of ``parsed``, keyed by
the CUI as an integer, dated by when the answer was fetched. A CUI ANAF
didn't know has ``found = 0``, a checked answer: its yes/no signals read
no, the rest unknown. A CUI never asked has no row: unknown. A plain file
(version 1, the job's until the upgrade) is not read here: the register
counts as not loaded until it is rewritten.
"""

from __future__ import annotations

import json
import sqlite3

from harvey import queries
from harvey.registers import Adapter as Base
from harvey.registers import Read, chunks

MODULE = "anaf"
# When the answer was fetched, as the job writes the time (ISO, UTC).
FETCHED = "strftime('%Y-%m-%dT%H:%M:%S+00:00', a.fetched, 'unixepoch')"
TABLE = "{alias}.parsed c CROSS JOIN {alias}.answers a"

queries.statement(
    "anaf.describe",
    "SELECT j.value AS key, a.fetched, a.found, a.dict, a.body FROM json_each(?) j "
    "CROSS JOIN {alias}.answers a WHERE a.cui = j.value",
    params=("[1, 2]",), module=MODULE, drives="j")
queries.statement("anaf.dicts", "SELECT id, zdict FROM {alias}.dict", module=MODULE,
                  scans={"dict": "the preset dictionaries: one row each"})


def _read(signal: str, column: str) -> Read:
    return Read(signal, TABLE, "cui", f"c.{column}", date=FETCHED, where="a.cui = c.cui")


class Adapter(Base):
    module = MODULE
    # Answers are added a hundred at a time: the file's size and last write
    # mark it (no statement reads all of it).
    marks_sql = ""
    reads = (
        _read("VAT_REGISTERED", "vat"),
        _read("VAT_ON_CASH", "vat_cash"),
        _read("SPLIT_VAT", "split_vat"),
        _read("FISCALLY_INACTIVE", "inactive"),
        _read("EFACTURA_REGISTERED", "efactura"),
        _read("LEGAL_FORM", "legal_form"),
        _read("VAT_SINCE", "vat_since"),
        _read("REGISTRATION_STATUS", "status"),
        # Every number ANAF lists (E.164, space-separated) and the address
        # lines (registered office, then fiscal domicile when elsewhere).
        _read("PHONE_NUMBER", "phone"),
        _read("ADDRESS", "address"),
    )

    def attach(self, con: sqlite3.Connection) -> bool:
        """Attach the file when it is the compressed form; a plain or a
        half-rewritten one counts as not loaded."""
        from harvey.modules.ro.anaf import store

        if self.path is None or not self.path.is_file():
            return False
        if self.alias not in {r[1] for r in con.execute("PRAGMA database_list")}:
            probe = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True)
            try:
                if store.version(probe) != store.VERSION:
                    return False
            finally:
                probe.close()
        return super().attach(con)

    def describe(self, con, keys) -> dict:
        """{key: {"fetched_at", "found", "answer"}} (the raw answer parsed)."""
        from harvey.modules.ro.anaf import store

        zdicts = {**store.DICTIONARIES, **dict(con.execute(self.sql("dicts")).fetchall())}
        out = {}
        for chunk in chunks(keys):
            for key, fetched, found, dict_id, blob in con.execute(self.sql("describe"),
                                                                  (json.dumps(chunk),)):
                try:
                    answer = json.loads(store.decompress(dict_id, blob, zdicts)) if found else None
                except ValueError:
                    answer = None
                out[key] = {"fetched_at": store.to_iso(fetched), "found": bool(found),
                            "answer": answer}
        return out
