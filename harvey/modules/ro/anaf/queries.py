"""ANAF's kept answers' adapter: how the core reads data/anaf.db.

Answers are kept by CUI (as text) with ANAF's JSON body; a CUI ANAF didn't
know has ``found = 0``, a checked answer: its yes/no signals read no. A CUI
never asked has no row: unknown. Values are read from the body by key;
there is no range over them until the file keeps the parsed fields beside
the body (its rewrite), so a cohort condition on them is a count job.
"""

from __future__ import annotations

import json

from harvey import queries
from harvey.registers import Adapter as Base
from harvey.registers import Read, chunks

MODULE = "anaf"

queries.statement(
    "anaf.describe",
    "SELECT j.value AS key, a.fetched_at, a.found, a.body FROM json_each(?) j "
    "CROSS JOIN {alias}.answers a WHERE a.cui = CAST(j.value AS TEXT)",
    params=("[1, 2]",), module=MODULE, drives="j")


def _flag(signal: str, path: str) -> Read:
    value = f"CASE WHEN c.found = 0 THEN 0 ELSE json_extract(c.body, '{path}') END"
    return Read(signal, "{alias}.answers c", "cui", value, date="c.fetched_at", key_text=True)


def _text(signal: str, path: str) -> Read:
    return Read(signal, "{alias}.answers c", "cui", f"NULLIF(json_extract(c.body, '{path}'), '')",
                date="c.fetched_at", key_text=True)


class Adapter(Base):
    module = MODULE
    # Answers are added a hundred at a time: the file's size and last write
    # mark it (no statement reads all of it).
    marks_sql = ""
    reads = (
        _flag("VAT_REGISTERED", "$.inregistrare_scop_Tva.scpTVA"),
        _flag("VAT_ON_CASH", "$.inregistrare_RTVAI.statusTvaIncasare"),
        _flag("SPLIT_VAT", "$.inregistrare_SplitTVA.statusSplitTVA"),
        _flag("FISCALLY_INACTIVE", "$.stare_inactiv.statusInactivi"),
        _flag("EFACTURA_REGISTERED", "$.date_generale.statusRO_e_Factura"),
        _text("LEGAL_FORM", "$.date_generale.forma_juridica"),
    )

    def describe(self, con, keys) -> dict:
        """{key: {"fetched_at", "found", "answer"}} (the answer parsed)."""
        out = {}
        for chunk in chunks(keys):
            for key, fetched_at, found, body in con.execute(self.sql("describe"),
                                                            (json.dumps(chunk),)):
                try:
                    answer = json.loads(body) if found else None
                except ValueError:
                    answer = None
                out[key] = {"fetched_at": fetched_at, "found": bool(found), "answer": answer}
        return out
