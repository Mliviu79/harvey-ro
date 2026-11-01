"""Every kept value of the overlay, computed again from base rows.

``diff()`` compares what is kept with a full recount and says, per kept
value, how many rows differ (``harvey doctor --recount``): a trigger bug
shows up as a non-zero line. ``touched()`` is bulk mode's recount
(triggers.bulk): it writes the recounted values for the rows a bulk write
touched, and the tallies in full. ``fix()`` writes a full recount.

The rules (the same the triggers keep, see triggers.py):
- link: one per (firm, contact) that has evidence; trust the best (lowest)
  of its evidence, person the highest person id named, first/last seen the
  evidence's earliest and latest.
- reading: per (firm, signal, key) the newest observation (by time, then
  id); positive when its number is missing or not 0. ``checked_at`` is when
  it was last seen, which only the writes know: it is checked to be no
  earlier than ``at``, never recounted.
- firm (live firms; a merged record keeps zeros): n_contacts / n_email /
  n_phone / n_mobile count distinct contacts linked by any of its records,
  best_trust the best link trust, n_people distinct normalised names of
  its records' people, and the search times the latest search (not an
  error) of its records: any, a website read, a deep website read (the
  ``deep_bit`` of looked_for), the contact finder's.
- contact: n_firms distinct live firms linking it, best_trust its best link.
- firm_signal: per live firm and signal, its records' readings and how
  many are positive; signal_tally: per signal, live firms with a positive
  reading and live firms with any.
- tally: the counts in schema.TALLIES, and outbox rows per status.
- live: a merged record points at the survivor of a merge in force that
  took it, and a live firm was taken by no merge in force.
- triggers: every kept-value trigger and guard is installed.

History is folded only where the trigger path would never have written
it: bulk mode drops an observation it added that repeats the one before
it. ``fix()`` deletes no observation (a late, older reading the trigger
path kept stays history).
"""

from __future__ import annotations

import sqlite3

from harvey.overlay.schema import EMAIL, ERROR, MOBILE, PHONE, TALLIES

FIRM_COLUMNS = ("n_contacts", "n_email", "n_phone", "n_mobile", "best_trust", "n_people",
                "last_search_at", "site_read_at", "site_read_deep", "finder_at")
LINK_COLUMNS = ("trust", "person", "location", "first_seen", "last_seen")
READING_COLUMNS = ("num", "text", "confidence", "module", "at", "obs", "positive")


def _roles(con) -> dict:
    roles = dict(con.execute("SELECT name, id FROM role").fetchall())
    return {"site": roles.get("site_read"), "finder": roles.get("finder"),
            "deep": roles.get("deep_bit")}


def _drop(con, *tables):
    for t in tables:
        con.execute(f"DROP TABLE IF EXISTS temp.{t}")


# ── Expected values into temp tables ──

def _scope_all(con) -> None:
    """Every firm and contact is in scope (a full recount)."""
    _drop(con, "scope_live", "scope_contact")
    con.execute("CREATE TEMP TABLE scope_live (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO scope_live SELECT id FROM firm WHERE live = 0")
    con.execute("CREATE TEMP TABLE scope_contact (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO scope_contact SELECT id FROM contact")


def _groups(con) -> None:
    """grp: every record of the live firms in scope_live, with its live firm;
    plus records named in scope_live that are merged now (they keep zeros)."""
    _drop(con, "grp")
    con.execute("CREATE TEMP TABLE grp (firm INTEGER PRIMARY KEY, l INTEGER NOT NULL)")
    con.execute("INSERT OR IGNORE INTO grp SELECT f.id, f.id FROM scope_live s "
                "JOIN firm f ON f.id = s.id WHERE f.live = 0")
    con.execute("INSERT OR IGNORE INTO grp SELECT f.id, f.live FROM scope_live s "
                "JOIN firm f ON f.live = s.id AND f.live != 0")
    # A record that was live and is merged now (a merge in bulk): its own
    # row is recounted (to zeros) along with its new group.
    con.execute("INSERT OR IGNORE INTO grp SELECT f.id, f.live FROM scope_live s "
                "JOIN firm f ON f.id = s.id WHERE f.live != 0")


def _expected_links(con, pairs: str | None) -> None:
    _drop(con, "exp_link")
    where = (f" WHERE (firm, contact) IN (SELECT firm, contact FROM {pairs})" if pairs else "")
    con.execute("CREATE TEMP TABLE exp_link (firm INTEGER, contact INTEGER, trust INTEGER, "
                "person INTEGER, location INTEGER, first_seen INTEGER, last_seen INTEGER, "
                "PRIMARY KEY (firm, contact)) WITHOUT ROWID")
    con.execute("INSERT INTO exp_link SELECT firm, contact, MIN(trust), MAX(person), "
                f"MAX(location), MIN(first_seen), MAX(last_seen) FROM link_evidence{where} "
                "GROUP BY firm, contact")


def _expected_readings(con, keys: str | None) -> None:
    _drop(con, "exp_reading")
    where = (f" WHERE (firm, signal, key) IN (SELECT firm, signal, key FROM {keys})"
             if keys else "")
    con.execute("CREATE TEMP TABLE exp_reading (firm INTEGER, signal INTEGER, key TEXT, "
                "num REAL, text TEXT, confidence REAL, module INTEGER, at INTEGER, "
                "obs INTEGER, positive INTEGER, PRIMARY KEY (firm, signal, key)) WITHOUT ROWID")
    con.execute(f"""
        INSERT INTO exp_reading
        SELECT firm, signal, key, num, text, confidence, module, at, id,
               num IS NULL OR num != 0
        FROM (SELECT o.*, ROW_NUMBER() OVER (PARTITION BY firm, signal, key
                                             ORDER BY at DESC, id DESC) AS rn
              FROM obs o{where})
        WHERE rn = 1""")


def _expected_firms(con, readings: str = "reading") -> None:
    """exp_firm and exp_firm_signal for the records in grp."""
    r = _roles(con)
    _drop(con, "exp_firm", "exp_firm_signal", "g_contacts", "g_trust", "g_people", "g_search")
    con.execute(f"CREATE TEMP TABLE exp_firm (id INTEGER PRIMARY KEY, "
                f"{', '.join(c + ' INTEGER' for c in FIRM_COLUMNS)})")
    con.execute("INSERT INTO exp_firm (id, n_contacts, n_email, n_phone, n_mobile, n_people) "
                "SELECT firm, 0, 0, 0, 0, 0 FROM grp")
    con.execute(f"""
        CREATE TEMP TABLE g_contacts AS
        SELECT gl.l AS l, COUNT(*) AS n, SUM(c.kind = {EMAIL}) AS e, SUM(c.kind = {PHONE}) AS p,
               SUM(c.kind = {PHONE} AND c.line_type IS {MOBILE}) AS m
        FROM (SELECT DISTINCT g.l, k.contact FROM grp g JOIN link k ON k.firm = g.firm) gl
        JOIN contact c ON c.id = gl.contact GROUP BY gl.l""")
    con.execute("UPDATE exp_firm SET n_contacts = x.n, n_email = x.e, n_phone = x.p, "
                "n_mobile = x.m FROM g_contacts x WHERE x.l = exp_firm.id")
    con.execute("CREATE TEMP TABLE g_trust AS SELECT g.l AS l, MIN(k.trust) AS t "
                "FROM grp g JOIN link k ON k.firm = g.firm GROUP BY g.l")
    con.execute("UPDATE exp_firm SET best_trust = x.t FROM g_trust x WHERE x.l = exp_firm.id")
    con.execute("CREATE TEMP TABLE g_people AS SELECT g.l AS l, COUNT(DISTINCT p.name_norm) AS n "
                "FROM grp g JOIN person p ON p.firm = g.firm GROUP BY g.l")
    con.execute("UPDATE exp_firm SET n_people = x.n FROM g_people x WHERE x.l = exp_firm.id")
    con.execute(f"""
        CREATE TEMP TABLE g_search AS
        SELECT g.l AS l, MAX(s.at) AS a,
               MAX(CASE WHEN s.capability = :site THEN s.at END) AS site,
               MAX(CASE WHEN s.capability = :site AND (s.looked_for & :deep) != 0
                        THEN s.at END) AS deep,
               MAX(CASE WHEN s.capability = :finder THEN s.at END) AS finder
        FROM grp g JOIN search s ON s.is_person = 0 AND s.target = g.firm
        WHERE s.outcome != {ERROR} GROUP BY g.l""", r)
    con.execute("UPDATE exp_firm SET last_search_at = x.a, site_read_at = x.site, "
                "site_read_deep = x.deep, finder_at = x.finder "
                "FROM g_search x WHERE x.l = exp_firm.id")
    con.execute("CREATE TEMP TABLE exp_firm_signal (firm INTEGER, signal INTEGER, "
                "n_keys INTEGER, n_positive INTEGER, PRIMARY KEY (signal, firm)) WITHOUT ROWID")
    con.execute(f"INSERT INTO exp_firm_signal SELECT g.l, r.signal, COUNT(*), SUM(r.positive) "
                f"FROM grp g JOIN {readings} r ON r.firm = g.firm GROUP BY g.l, r.signal")


def _expected_contacts(con, links: str = "link") -> None:
    _drop(con, "exp_contact")
    con.execute("CREATE TEMP TABLE exp_contact (id INTEGER PRIMARY KEY, n_firms INTEGER, "
                "best_trust INTEGER)")
    con.execute("INSERT INTO exp_contact SELECT id, 0, NULL FROM scope_contact")
    con.execute(f"""
        UPDATE exp_contact SET n_firms = x.n, best_trust = x.t FROM (
            SELECT k.contact AS c, COUNT(DISTINCT CASE f.live WHEN 0 THEN f.id ELSE f.live END) AS n,
                   MIN(k.trust) AS t
            FROM scope_contact s JOIN {links} k ON k.contact = s.id JOIN firm f ON f.id = k.firm
            GROUP BY k.contact) x
        WHERE x.c = exp_contact.id""")


def _expected_tallies(con, firms: str, firm_signal: str) -> None:
    """exp_tally and exp_signal_tally, in full. ``firms``: a query giving
    (live, n_contacts, website, domain) per firm."""
    _drop(con, "exp_tally", "exp_signal_tally")
    con.execute("CREATE TEMP TABLE exp_tally (name TEXT PRIMARY KEY, n INTEGER) WITHOUT ROWID")
    con.execute(f"""
        INSERT INTO exp_tally
        SELECT 'firms', COALESCE(SUM(live = 0), 0) FROM ({firms})
        UNION ALL SELECT 'firms_with_contact', COALESCE(SUM(live = 0 AND n_contacts > 0), 0)
                  FROM ({firms})
        UNION ALL SELECT 'firms_with_site', COALESCE(SUM(live = 0 AND (website IS NOT NULL
                                                      OR domain IS NOT NULL)), 0) FROM ({firms})
        UNION ALL SELECT 'firms_merged', COALESCE(SUM(live != 0), 0) FROM ({firms})""")
    con.execute(f"""
        INSERT INTO exp_tally
        SELECT 'contacts', COUNT(*) FROM contact
        UNION ALL SELECT 'contacts_email', COUNT(*) FROM contact WHERE kind = {EMAIL}
        UNION ALL SELECT 'contacts_phone', COUNT(*) FROM contact WHERE kind = {PHONE}
        UNION ALL SELECT 'contacts_mobile', COUNT(*) FROM contact
                  WHERE kind = {PHONE} AND line_type IS {MOBILE}
        UNION ALL SELECT 'contacts_do_not_contact', COUNT(*) FROM contact WHERE do_not_contact != 0
        UNION ALL SELECT 'people', COUNT(*) FROM person""")
    con.execute("INSERT INTO exp_tally SELECT 'outbox:' || COALESCE(status, ''), COUNT(*) "
                "FROM outbox GROUP BY 1")
    con.execute("CREATE TEMP TABLE exp_signal_tally (signal INTEGER PRIMARY KEY, n_true INTEGER, "
                "n_checked INTEGER)")
    con.execute(f"INSERT INTO exp_signal_tally SELECT signal, SUM(n_positive > 0), COUNT(*) "
                f"FROM {firm_signal} GROUP BY signal")


# ── The diff ──

def _count(con, sql: str) -> int:
    return con.execute(sql).fetchone()[0]


def _diff_table(con, actual: str, expected: str, keys: tuple, cols: tuple,
                actual_where: str = "") -> int:
    on = " AND ".join(f"a.{k} = e.{k}" for k in keys)
    differ = " OR ".join(f"a.{c} IS NOT e.{c}" for c in cols)
    missing = _count(con, f"SELECT COUNT(*) FROM {expected} e WHERE NOT EXISTS "
                          f"(SELECT 1 FROM {actual} a WHERE {on})")
    extra = _count(con, f"SELECT COUNT(*) FROM {actual} a WHERE "
                        f"{actual_where + ' AND ' if actual_where else ''}NOT EXISTS "
                        f"(SELECT 1 FROM {expected} e WHERE {on})")
    wrong = _count(con, f"SELECT COUNT(*) FROM {expected} e JOIN {actual} a ON {on} "
                        f"WHERE {differ}") if cols else 0
    return missing + extra + wrong


def diff(con: sqlite3.Connection) -> dict[str, int]:
    """Per kept value, how many rows differ from a full recount. Reads
    only (temporary tables aside)."""
    _scope_all(con)
    _groups(con)
    _expected_links(con, None)
    _expected_readings(con, None)
    _expected_firms(con, readings="exp_reading")
    _expected_contacts(con, links="exp_link")
    _expected_tallies(
        con, "SELECT f.live, COALESCE(e.n_contacts, 0) AS n_contacts, f.website, f.domain "
             "FROM firm f LEFT JOIN exp_firm e ON e.id = f.id", "exp_firm_signal")
    out = {
        "link": _diff_table(con, "link", "exp_link", ("firm", "contact"), LINK_COLUMNS),
        "reading": _diff_table(con, "reading", "exp_reading", ("firm", "signal", "key"),
                               READING_COLUMNS),
        "reading.checked_at": _count(con, "SELECT COUNT(*) FROM reading WHERE checked_at < at"),
    }
    for col in FIRM_COLUMNS:
        out[f"firm.{col}"] = _count(con, f"SELECT COUNT(*) FROM exp_firm e JOIN firm f "
                                         f"ON f.id = e.id WHERE f.{col} IS NOT e.{col}")
    out["firm.live"] = _count(con, "SELECT COUNT(*) FROM firm f WHERE f.live != 0 AND NOT EXISTS "
                                   "(SELECT 1 FROM firm s WHERE s.id = f.live AND s.live = 0)")
    # live agrees with the merges in force: a merged record was taken by a
    # merge into its survivor, a live one by none.
    out["firm.live_merges"] = _count(con, """
        SELECT COUNT(*) FROM firm f WHERE CASE WHEN f.live != 0
          THEN NOT EXISTS (SELECT 1 FROM merge_member mm JOIN merge m ON m.id = mm.merge
                           WHERE mm.firm = f.id AND m.undone_at IS NULL AND m.survivor = f.live)
          ELSE EXISTS (SELECT 1 FROM merge_member mm JOIN merge m ON m.id = mm.merge
                       WHERE mm.firm = f.id AND m.undone_at IS NULL) END""")
    for col in ("n_firms", "best_trust"):
        out[f"contact.{col}"] = _count(con, f"SELECT COUNT(*) FROM exp_contact e JOIN contact c "
                                            f"ON c.id = e.id WHERE c.{col} IS NOT e.{col}")
    out["firm_signal"] = _diff_table(con, "firm_signal", "exp_firm_signal", ("signal", "firm"),
                                     ("n_keys", "n_positive"))
    out["signal_tally"] = _diff_table(con, "signal_tally", "exp_signal_tally", ("signal",),
                                      ("n_true", "n_checked"),
                                      actual_where="(a.n_checked != 0 OR a.n_true != 0)")
    # A tally row that is 0 counts the same as no row.
    out["tally"] = _count(con, """
        SELECT COUNT(*) FROM (
            SELECT name, n FROM exp_tally WHERE n != 0
            EXCEPT SELECT name, n FROM tally WHERE n != 0)""") + _count(con, """
        SELECT COUNT(*) FROM (
            SELECT name, n FROM tally WHERE n != 0
            EXCEPT SELECT name, n FROM exp_tally WHERE n != 0)""")
    from harvey.overlay.triggers import missing

    out["triggers.missing"] = len(missing(con))
    _cleanup(con)
    return out


def _cleanup(con) -> None:
    _drop(con, "scope_live", "scope_contact", "grp", "exp_link", "exp_reading", "exp_firm",
          "exp_firm_signal", "exp_contact", "exp_tally", "exp_signal_tally", "g_contacts",
          "g_trust", "g_people", "g_search", "ord", "fresh", "pairs", "keys")


# ── Writing recounted values ──

def _write_links(con, pairs: str | None) -> None:
    """Links for these (firm, contact) pairs (all, when None) from their evidence."""
    _expected_links(con, pairs)
    scope = f"(firm, contact) IN (SELECT firm, contact FROM {pairs})" if pairs else "1"
    con.execute(f"DELETE FROM link WHERE {scope} AND NOT EXISTS "
                "(SELECT 1 FROM exp_link e WHERE e.firm = link.firm AND e.contact = link.contact)")
    con.execute("""
        INSERT INTO link (firm, contact, trust, person, location, first_seen, last_seen)
        SELECT firm, contact, trust, person, location, first_seen, last_seen FROM exp_link
        WHERE true
        ON CONFLICT (firm, contact) DO UPDATE SET trust = excluded.trust,
            person = excluded.person, location = excluded.location,
            first_seen = excluded.first_seen,
            last_seen = excluded.last_seen""")


def _write_readings(con, keys: str | None, fold: str | None = None) -> None:
    """Readings for these keys (all, when None): the newest observation,
    with the latest time its value was seen (its run of equal values) as
    checked_at. ``fold``: a table of observation ids a bulk write added;
    those that repeat the observation before them are not history (the
    trigger path never writes them) and are deleted."""
    _drop(con, "ord", "fresh")
    where = (f" WHERE (firm, signal, key) IN (SELECT firm, signal, key FROM {keys})"
             if keys else "")
    con.execute(f"""
        CREATE TEMP TABLE ord AS
        SELECT id, firm, signal, key, at, new_run,
               SUM(new_run) OVER (PARTITION BY firm, signal, key ORDER BY at, id) AS run
        FROM (SELECT o.id, o.firm, o.signal, o.key, o.at,
                     CASE WHEN LAG(o.id) OVER w IS NULL OR o.num IS NOT LAG(o.num) OVER w
                               OR o.text IS NOT LAG(o.text) OVER w THEN 1 ELSE 0 END AS new_run
              FROM obs o{where}
              WINDOW w AS (PARTITION BY o.firm, o.signal, o.key ORDER BY o.at, o.id))""")
    # Per key, the last time the current value was seen (its last run).
    con.execute("""
        CREATE TEMP TABLE fresh AS
        SELECT o.firm, o.signal, o.key, MAX(o.at) AS seen
        FROM ord o JOIN (SELECT firm, signal, key, MAX(run) AS run FROM ord
                         GROUP BY firm, signal, key) l
          ON l.firm = o.firm AND l.signal = o.signal AND l.key = o.key AND l.run = o.run
        GROUP BY o.firm, o.signal, o.key""")
    if fold:
        con.execute(f"DELETE FROM obs WHERE id IN (SELECT o.id FROM ord o JOIN {fold} t "
                    "ON t.id = o.id WHERE o.new_run = 0)")
    _expected_readings(con, keys)
    scope = f"(firm, signal, key) IN (SELECT firm, signal, key FROM {keys})" if keys else "1"
    con.execute(f"DELETE FROM reading WHERE {scope} AND NOT EXISTS (SELECT 1 FROM exp_reading e "
                "WHERE e.firm = reading.firm AND e.signal = reading.signal AND e.key = reading.key)")
    con.execute("""
        INSERT INTO reading (firm, signal, key, num, text, confidence, module, at, checked_at,
                             obs, positive)
        SELECT e.firm, e.signal, e.key, e.num, e.text, e.confidence, e.module, e.at,
               MAX(e.at, COALESCE(f.seen, e.at)), e.obs, e.positive
        FROM exp_reading e LEFT JOIN fresh f
          ON f.firm = e.firm AND f.signal = e.signal AND f.key = e.key WHERE true
        ON CONFLICT (firm, signal, key) DO UPDATE SET
            checked_at = CASE WHEN reading.num IS excluded.num AND reading.text IS excluded.text
                              THEN MAX(reading.checked_at, excluded.checked_at)
                              ELSE excluded.checked_at END,
            num = excluded.num, text = excluded.text, confidence = excluded.confidence,
            module = excluded.module, at = excluded.at, obs = excluded.obs,
            positive = excluded.positive""")


def _write_firms_and_contacts(con) -> None:
    """Firm values and firm_signal for the records in grp, then contact
    values for scope_contact."""
    _expected_firms(con)
    sets = ", ".join(f"{c} = e.{c}" for c in FIRM_COLUMNS)
    con.execute(f"UPDATE firm SET {sets} FROM exp_firm e WHERE e.id = firm.id")
    con.execute("DELETE FROM firm_signal WHERE firm IN (SELECT firm FROM grp)")
    con.execute("INSERT INTO firm_signal SELECT firm, signal, n_keys, n_positive "
                "FROM exp_firm_signal")
    _expected_contacts(con)
    con.execute("UPDATE contact SET n_firms = e.n_firms, best_trust = e.best_trust "
                "FROM exp_contact e WHERE e.id = contact.id")


def _write_tallies(con) -> None:
    _expected_tallies(con, "SELECT live, n_contacts, website, domain FROM firm", "firm_signal")
    con.execute("DELETE FROM tally")
    con.execute("INSERT INTO tally SELECT name, n FROM exp_tally")
    for name in TALLIES:
        con.execute("INSERT OR IGNORE INTO tally (name, n) VALUES (?, 0)", (name,))
    con.execute("DELETE FROM signal_tally")
    con.execute("INSERT INTO signal_tally SELECT signal, n_true, n_checked FROM exp_signal_tally")


def touched(con: sqlite3.Connection) -> None:
    """Bulk mode's recount: links and readings for the pairs and keys a
    bulk write touched, then every value of the live firms they belong to
    (and of the contacts those firms link), then the tallies in full.
    Runs inside bulk()'s transaction, triggers off."""
    _write_links(con, "temp.touched_link")
    _write_readings(con, "temp.touched_reading", fold="temp.touched_obs")
    _drop(con, "scope_live", "scope_contact")
    con.execute("CREATE TEMP TABLE scope_live (id INTEGER PRIMARY KEY)")
    for sql in ("SELECT id FROM temp.touched_firm",
                "SELECT firm FROM temp.touched_link",
                "SELECT firm FROM temp.touched_reading",
                # A contact's kind or line type counts on every firm linking it.
                "SELECT k.firm FROM temp.touched_contact t JOIN link k ON k.contact = t.id"):
        # Each touched record, and the live firm it belongs to.
        con.execute(f"INSERT OR IGNORE INTO scope_live SELECT f.id FROM firm f "
                    f"WHERE f.id IN ({sql})")
        con.execute(f"INSERT OR IGNORE INTO scope_live SELECT f.live FROM firm f "
                    f"WHERE f.id IN ({sql}) AND f.live != 0")
    _groups(con)
    con.execute("CREATE TEMP TABLE scope_contact (id INTEGER PRIMARY KEY)")
    con.execute("INSERT OR IGNORE INTO scope_contact SELECT id FROM temp.touched_contact")
    con.execute("INSERT OR IGNORE INTO scope_contact SELECT contact FROM temp.touched_link")
    con.execute("INSERT OR IGNORE INTO scope_contact SELECT k.contact FROM grp g "
                "JOIN link k ON k.firm = g.firm")
    _write_firms_and_contacts(con)
    _write_tallies(con)
    _cleanup(con)


def fix(con: sqlite3.Connection, *, fold_history: bool = False) -> None:
    """Write a full recount of every kept value (in bulk mode: one
    transaction, triggers off), and put back any trigger or guard that is
    missing. No observation is deleted, unless ``fold_history``: then one
    that repeats the observation before it is (a bulk load of history that
    was kept on every check, not only on change), after the readings'
    ``checked_at`` has taken the time it was last seen."""
    from harvey.overlay import schema
    from harvey.overlay.triggers import TOUCH_TABLES, bulk

    with bulk(con):
        schema.install_guards(con)
        _write_links(con, None)
        _write_readings(con, None, fold="main.obs" if fold_history else None)
        _scope_all(con)
        _groups(con)
        _write_firms_and_contacts(con)
        _write_tallies(con)
        _cleanup(con)
        # Nothing left for bulk()'s own recount to do.
        for table in TOUCH_TABLES:
            con.execute(f"DELETE FROM temp.{table}")


def search_times(con: sqlite3.Connection) -> None:
    """The search times of every live firm whose records have searches,
    counted again (after a role changes what a website read or the
    finder's search is)."""
    r = _roles(con)
    _drop(con, "st_scope", "st")
    con.execute("CREATE TEMP TABLE st_scope (firm INTEGER PRIMARY KEY, l INTEGER NOT NULL)")
    # The live firms whose records were searched, and each of their records.
    con.execute("""
        INSERT OR IGNORE INTO st_scope
        SELECT DISTINCT CASE f.live WHEN 0 THEN f.id ELSE f.live END,
                        CASE f.live WHEN 0 THEN f.id ELSE f.live END
        FROM search s JOIN firm f ON f.id = s.target WHERE s.is_person = 0""")
    con.execute("INSERT OR IGNORE INTO st_scope SELECT m.id, m.live FROM st_scope g "
                "JOIN firm m ON m.live = g.firm AND m.live != 0")
    con.execute(f"""
        CREATE TEMP TABLE st AS
        SELECT l, MAX(CASE WHEN s.outcome != {ERROR} THEN s.at END) AS a,
               MAX(CASE WHEN s.outcome != {ERROR} AND s.capability = :site THEN s.at END) AS site,
               MAX(CASE WHEN s.outcome != {ERROR} AND s.capability = :site
                        AND (s.looked_for & :deep) != 0 THEN s.at END) AS deep,
               MAX(CASE WHEN s.outcome != {ERROR} AND s.capability = :finder
                        THEN s.at END) AS finder
        FROM st_scope g LEFT JOIN search s ON s.is_person = 0 AND s.target = g.firm
        GROUP BY l""", r)
    con.execute("UPDATE firm SET last_search_at = st.a, site_read_at = st.site, "
                "site_read_deep = st.deep, finder_at = st.finder FROM st WHERE st.l = firm.id")
    _drop(con, "st_scope", "st")
