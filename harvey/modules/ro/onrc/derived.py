"""The tables the registry's load builds for the core to read it by key.

- ``firm_by_cui``: the canonical registry row per CUI. A CUI can be on
  several registrations (an old struck-off one and the current one); the
  firm Harvey means is the operating row, else the newest registration,
  else the highest registration number (stable across reloads, unlike the
  row ids). Keyed by the CUI as an integer.
- ``name_key``: operating firms by the resolver's name key and town key
  (harvey.name_keys, the resolver's keys), for matching a record by name and
  town without reading the whole registry. Built by code, so the version
  of its rules is recorded (``derived_meta``) and a reader refuses a stale
  one.

Built inside the registry's own file by its load (registry.build) and by
``reindex()`` for a file loaded before they existed; ``build()`` can also
write them into another file with the registry attached read-only.
"""

from __future__ import annotations

from pathlib import Path

from harvey.registers import build_derived, mark_built

TABLES = ("firm_by_cui", "name_key")
# DD/MM/YYYY (sometimes with a time) as YYYYMMDD, for "newest registration".
REGISTERED = ("CASE WHEN length(f.registered) >= 10 THEN substr(f.registered, 7, 4) "
              "|| substr(f.registered, 4, 2) || substr(f.registered, 1, 2) ELSE '' END")
# A CUI the core can key by: digits only, no leading zero.
CUI_OK = "f.cui GLOB '[1-9]*' AND f.cui NOT GLOB '*[^0-9]*' AND length(f.cui) <= 12"


def build(con, src: str = "main", dst: str = "main") -> dict:
    from harvey import name_keys

    con.execute(f"DROP TABLE IF EXISTS {dst}.firm_by_cui")
    con.execute(f"CREATE TABLE {dst}.firm_by_cui (cui INTEGER PRIMARY KEY, firm_id INTEGER NOT NULL)")
    con.execute(f"""
        INSERT INTO {dst}.firm_by_cui (cui, firm_id)
        SELECT cui, id FROM (
            SELECT CAST(f.cui AS INTEGER) AS cui, f.id,
                   ROW_NUMBER() OVER (PARTITION BY f.cui
                                      ORDER BY f.operating DESC, {REGISTERED} DESC, f.reg DESC,
                                               f.id DESC) AS rn
            FROM {src}.firms f WHERE f.cui != '' AND {CUI_OK})
        WHERE rn = 1 ORDER BY cui""")
    skipped = con.execute(f"SELECT COUNT(*) FROM {src}.firms f WHERE f.cui != '' "
                          f"AND NOT ({CUI_OK})").fetchone()[0]

    con.create_function("harvey_name_key", 1, name_keys.name_key, deterministic=True)
    con.create_function("harvey_locality_key", 1, name_keys.locality_key, deterministic=True)
    con.execute(f"DROP TABLE IF EXISTS {dst}.name_key")
    con.execute(f"CREATE TABLE {dst}.name_key (key TEXT NOT NULL, locality TEXT NOT NULL, "
                "firm_id INTEGER NOT NULL, PRIMARY KEY (key, locality, firm_id)) WITHOUT ROWID")
    con.execute(f"""
        INSERT OR IGNORE INTO {dst}.name_key (key, locality, firm_id)
        SELECT k, harvey_locality_key(locality), id FROM (
            SELECT harvey_name_key(f.name) AS k, f.locality, f.id
            FROM {src}.firms f WHERE f.operating = 1)
        WHERE k != '' ORDER BY 1, 2, 3""")
    mark_built(con, dst, "name_key", name_keys.version())
    q = con.execute
    return {"firm_by_cui": q(f"SELECT COUNT(*) FROM {dst}.firm_by_cui").fetchone()[0],
            "name_key": q(f"SELECT COUNT(*) FROM {dst}.name_key").fetchone()[0],
            "cui_not_keyable": skipped}


def reindex(path: Path, target: Path | None = None) -> dict:
    """Build the tables into the registry at ``path`` (or into ``target``,
    leaving the registry untouched)."""
    return build_derived(path, build, target, tables=TABLES)
