"""Merge and undo on the overlay.

A merge moves no rows: the merged firm (and every record already merged
into it) points at the survivor (``firm.live``), and the live firm's
register identity is the union of its records' ``firm_ident`` rows (one key
per legal scheme at most: a merge that would give it two is refused). Its
kept values are recounted for the whole group (bulk mode), since triggers
only follow row writes.

Undo restores ``live`` for the records the merge took that still point at
its survivor; it is refused while the survivor is itself merged (undo the
newer merge first), so a record never comes back through a stale row.
"""

from __future__ import annotations

import sqlite3
import time

from harvey.overlay import triggers


class MergeRefused(ValueError):
    """``reason``: not_live, same_firm, two_legal_ids, survivor_merged,
    already_undone, unknown."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _live(con, firm: int) -> int | None:
    row = con.execute("SELECT live FROM firm WHERE id = ?", (firm,)).fetchone()
    return None if row is None else row[0]


def members(con, firm: int) -> list[int]:
    """A live firm's records: itself first, then those merged into it."""
    return [firm] + [r[0] for r in con.execute(
        "SELECT id FROM firm WHERE live = ? AND live != 0 ORDER BY id", (firm,))]


def legal_conflict(con, a: int, b: int) -> str | None:
    """The legal scheme in which live firms ``a`` and ``b`` hold different
    keys (two firms that can never be one), or None."""
    group_a, group_b = members(con, a), members(con, b)
    marks_a, marks_b = ",".join("?" * len(group_a)), ",".join("?" * len(group_b))
    row = con.execute(f"""
        SELECT s.name FROM firm_ident x JOIN ident_scheme s ON s.id = x.scheme AND s.legal = 1
        WHERE x.firm IN ({marks_a}) AND EXISTS (
            SELECT 1 FROM firm_ident y WHERE y.firm IN ({marks_b}) AND y.scheme = x.scheme
            AND y.key != x.key) LIMIT 1""",
                      (*group_a, *group_b)).fetchone()
    return row[0] if row else None


def merge(con: sqlite3.Connection, survivor: int, merged: int, *, score: float | None = None,
          reasons: str = "", decided_by: str = "", grp: int | None = None,
          location: int | None = None, at: int | None = None) -> int:
    """Merge live firm ``merged`` (with its records) into live firm
    ``survivor``; returns the merge's id. A survivor with no website takes
    one of the merged records' (filled, and cleared again by undo)."""
    at = int(at if at is not None else time.time())
    if survivor == merged:
        raise MergeRefused("same_firm")
    for firm in (survivor, merged):
        live = _live(con, firm)
        if live is None:
            raise MergeRefused("unknown", str(firm))
        if live != 0:
            raise MergeRefused("not_live", f"{firm} is merged into {live}")
    scheme = legal_conflict(con, survivor, merged)
    if scheme:
        raise MergeRefused("two_legal_ids", scheme)
    with triggers.bulk(con) as b:
        mid = con.execute(
            "INSERT INTO merge (survivor, merged, score, reasons, decided_by, grp, at, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (survivor, merged, score, reasons, decided_by, grp, at, location)).lastrowid
        taken = members(con, merged)
        con.executemany("INSERT INTO merge_member (merge, firm) VALUES (?, ?)",
                        [(mid, f) for f in taken])
        con.execute("UPDATE firm SET live = ? WHERE id = ? OR (live = ? AND live != 0)",
                    (survivor, merged, merged))
        _fill_website(con, survivor, taken, mid)
        b.touch(survivor, *taken)
        con.execute("UPDATE register_tally SET at = NULL")
    return mid


def _fill_website(con, survivor: int, taken: list[int], mid: int) -> None:
    """A survivor with no website takes the first merged record's that has
    one (its domain, as a site, when it has only that: the domain itself
    stays the record's key)."""
    row = con.execute("SELECT website, domain FROM firm WHERE id = ?", (survivor,)).fetchone()
    if row[0] is not None or row[1] is not None:
        return
    for firm in taken:
        site = con.execute("SELECT website, domain, website_trust, website_source FROM firm "
                           "WHERE id = ?", (firm,)).fetchone()
        website = site[0] or (f"https://{site[1]}" if site[1] else None)
        if website:
            con.execute("UPDATE firm SET website = ?, website_trust = ?, website_source = ?, "
                        "website_fill = ? WHERE id = ?",
                        (website, site[2], site[3], mid, survivor))
            return


def undo(con: sqlite3.Connection, merge_id: int, *, at: int | None = None) -> list[int]:
    """Undo a merge; returns the records that came back (the merged firm
    first). Refused while its survivor is merged into another firm."""
    at = int(at if at is not None else time.time())
    row = con.execute("SELECT survivor, merged, undone_at, location FROM merge WHERE id = ?",
                      (merge_id,)).fetchone()
    if row is None:
        raise MergeRefused("unknown", f"merge {merge_id}")
    survivor, merged, undone_at, location = row
    if undone_at is not None:
        raise MergeRefused("already_undone")
    if _live(con, survivor) != 0:
        raise MergeRefused("survivor_merged", f"undo the merge of {survivor} first")
    with triggers.bulk(con) as b:
        back = [r[0] for r in con.execute(
            "SELECT m.firm FROM merge_member m JOIN firm f ON f.id = m.firm "
            "WHERE m.merge = ? AND f.live = ? ORDER BY m.firm != ?, m.firm",
            (merge_id, survivor, merged))]
        con.executemany("UPDATE firm SET live = CASE WHEN id = ? THEN 0 ELSE ? END WHERE id = ?",
                        [(merged, merged, f) for f in back])
        con.execute("UPDATE firm SET website = NULL, website_trust = NULL, website_source = NULL, "
                    "website_fill = NULL WHERE id = ? AND website_fill = ?", (survivor, merge_id))
        if location:
            con.execute("DELETE FROM locations WHERE id = ?", (location,))
        con.execute("UPDATE merge SET undone_at = ? WHERE id = ?", (at, merge_id))
        b.touch(survivor, *back)
        con.execute("UPDATE register_tally SET at = NULL")
    return back
