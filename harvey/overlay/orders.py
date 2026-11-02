"""Where each firm falls in the name and place orders, kept on the firm.

Sorting every firm in Harvey by name or place would read every firm's
register row on each first visit. Instead each firm keeps its position in
the directory register's own orders (``firm.name_order``,
``firm.place_order``, both indexed over live firms): a firm the register
describes takes its row's id (the registry's ids follow its names' order)
and its place line's rank (the register's derived ``place_rank``); a firm
described locally, or named by Harvey (a display name), takes the position
its own name or place line would have there, half a step before the first
one after it. A firm with neither sorts first going up.

Positions belong to one load of the register: a reload refreshes them all
(``refresh`` with no firms: a job, about a minute over 1.8M firms), and
until then the pages sort the slow way (``current`` says which). A merged
record's position is its survivor's when the survivor has none of its own.
"""

from __future__ import annotations

import sqlite3

MARK_KEY = "overlay.orders_mark"


def directory(adapters):
    """The register that places firms in its orders, or None."""
    return next((a for a in adapters if hasattr(a, "order_sql")), None)


def _attached(con, adapter) -> bool:
    return adapter.alias in {r[1] for r in con.execute("PRAGMA database_list")}


def mark(con, adapters) -> str | None:
    """The mark positions are taken against now: the directory's load mark,
    or None when there is none (or it can't place firms)."""
    d = directory(adapters)
    if d is None or not _attached(con, d) or not d.can_order(con):
        return None
    return d.marks(con) or None


def current(con, adapters) -> bool:
    """Whether the kept positions belong to the directory's current load."""
    now = mark(con, adapters)
    if now is None:
        return False
    row = con.execute("SELECT value FROM settings WHERE key = ?", (MARK_KEY,)).fetchone()
    return row is not None and row[0] == now


def refresh(con, adapters, schemes, firms=None) -> int:
    """Take the positions of ``firms`` (records, any of a group; None: every
    firm, and the mark is recorded) against the directory's current load,
    in the caller's transaction. Returns how many firms were placed."""
    d = directory(adapters)
    now = mark(con, adapters)
    if d is None or now is None:
        return 0
    con.execute("DROP TABLE IF EXISTS temp.order_firms")
    con.execute("DROP TABLE IF EXISTS temp.order_pos")
    con.execute("CREATE TEMP TABLE order_pos (firm INTEGER PRIMARY KEY, n REAL, p REAL)")
    subset = firms is not None
    if subset:
        groups = set()
        for f in firms:
            row = con.execute("SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm "
                              "WHERE id = ?", (int(f),)).fetchone()
            if row:
                groups.add(row[0])
        if not groups:
            return 0
        con.execute("CREATE TEMP TABLE order_firms (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT OR IGNORE INTO temp.order_firms VALUES (?)", [(g,) for g in groups])
        # (Their records merged in too: a survivor may take a record's.)
        con.execute("INSERT OR IGNORE INTO temp.order_firms SELECT f.id FROM temp.order_firms t "
                    "CROSS JOIN firm f WHERE f.live = t.id AND f.live != 0")
    # A firm known only by a registration number first, then by tax code
    # (which wins when a firm has both).
    ids = None
    if subset:
        import json

        ids = json.dumps([r[0] for r in con.execute("SELECT id FROM temp.order_firms")])
    for scheme in d.order_schemes():
        s = schemes.named(scheme)
        if s is not None:
            con.execute("INSERT OR REPLACE INTO temp.order_pos (firm, n, p) "
                        + d.order_sql(scheme, subset), (ids, s.id) if subset else (s.id,))
    # A survivor with no position of its own takes its first record's.
    con.execute("INSERT OR IGNORE INTO temp.order_pos (firm, n, p) "
                "SELECT f.live, o.n, o.p FROM firm f INDEXED BY firm_merged "
                "CROSS JOIN temp.order_pos o WHERE f.live != 0 AND o.firm = f.id ORDER BY f.id")
    # Firms the register doesn't place, and firms named by Harvey: their own
    # name (and a local firm's own place) put among the register's.
    within = "f.id IN (SELECT id FROM temp.order_firms) AND " if subset else ""
    rows = con.execute(
        f"SELECT f.id, f.display_name, l.name, l.locality, l.county, o.firm IS NOT NULL, o.p "
        f"FROM firm f LEFT JOIN local_firm l ON l.firm = f.id "
        f"LEFT JOIN temp.order_pos o ON o.firm = f.id "
        f"WHERE {within}f.live = 0 AND (f.display_name IS NOT NULL OR o.firm IS NULL)").fetchall()
    for fid, display, name, locality, county, placed, place in rows:
        n = d.name_position(con, display or name or "") if (display or name) else None
        if not placed:
            place = d.place_position(con, locality or "", county or "") \
                if (locality or county) else None
        con.execute("INSERT OR REPLACE INTO temp.order_pos (firm, n, p) VALUES (?, ?, ?)",
                    (fid, n, place))
    if subset:
        con.execute("UPDATE firm SET name_order = NULL, place_order = NULL "
                    "WHERE id IN (SELECT id FROM temp.order_firms)")
    else:
        con.execute("UPDATE firm SET name_order = NULL, place_order = NULL "
                    "WHERE name_order IS NOT NULL OR place_order IS NOT NULL")
    placed = con.execute("UPDATE firm SET name_order = o.n, place_order = o.p "
                         "FROM temp.order_pos o WHERE firm.id = o.firm").rowcount
    con.execute("DROP TABLE temp.order_pos")
    if subset:
        con.execute("DROP TABLE temp.order_firms")
    else:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (MARK_KEY, now))
    return placed


def refresh_file(path, adapters=None) -> int:
    """refresh() of every firm in the database at ``path``, in a write
    transaction of its own, unless the kept places are current already (a
    register reload's follow-up)."""
    from harvey.overlay import store
    from harvey.overlay.idents import Schemes

    con, attached = store.connect(path)
    try:
        if current(con, adapters if adapters is not None else attached):
            return 0
        con.execute("BEGIN IMMEDIATE")
        try:
            n = refresh(con, adapters if adapters is not None else attached,
                        Schemes.load(con))
            con.execute("COMMIT")
        except sqlite3.Error:
            con.execute("ROLLBACK")
            raise
        return n
    finally:
        con.close()
