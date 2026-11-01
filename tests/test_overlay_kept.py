"""The overlay's kept values (schema 25): random row writes, merges and undo
chains, and bulk writes, each compared with a full recount (0 rows may
differ). All data is fictional."""

import random
import sqlite3

import pytest

from harvey.overlay import merges, recount, schema, triggers
from harvey.overlay.merges import MergeRefused

SITE_READ, FINDER, OTHER = 10, 11, 12
DEEP = 4
SCHEME_LEGAL, SCHEME_LISTING = 1, 2


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    schema.create(con)
    schema.set_role(con, "site_read", SITE_READ)
    schema.set_role(con, "finder", FINDER)
    schema.set_role(con, "deep_bit", DEEP)
    con.execute("INSERT INTO module (id, name) VALUES (1, 'example_register')")
    con.execute("INSERT INTO ident_scheme (id, name, module, numeric, legal) "
                "VALUES (1, 'example.tax_id', 1, 1, 1), (2, 'example.listing', 1, 0, 0)")
    return con


def _clean(con) -> dict:
    out = recount.diff(con)
    return {k: v for k, v in out.items() if v}


class World:
    """Random writes over a small fictional overlay."""

    def __init__(self, con, rng: random.Random):
        self.con, self.rng = con, rng
        self.clock = 1_700_000_000
        self.tax_ids = iter(range(1000, 10**6))

    def tick(self) -> int:
        self.clock += self.rng.randint(-50, 100)     # times arrive out of order too
        return self.clock

    def ids(self, table: str, where: str = "1") -> list[int]:
        return [r[0] for r in self.con.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY id")]

    def pick(self, table: str, where: str = "1"):
        ids = self.ids(table, where)
        return self.rng.choice(ids) if ids else None

    # Writes

    def firm(self):
        r = self.rng
        site = f"https://f{r.randint(1, 10**9)}.example.com" if r.random() < 0.3 else None
        fid = self.con.execute("INSERT INTO firm (created, website) VALUES (?, ?)",
                               (self.tick(), site)).lastrowid
        if r.random() < 0.3:
            self.con.execute("INSERT INTO firm_ident (scheme, key, firm, since) VALUES (?, ?, ?, ?)",
                             (SCHEME_LEGAL, next(self.tax_ids), fid, self.clock))
        return fid

    def contact(self):
        r = self.rng
        kind = r.choice((schema.EMAIL, schema.PHONE, schema.PHONE, 3))
        line = r.choice((None, 0, 1, 1, 2)) if kind == schema.PHONE else None
        self.con.execute("INSERT OR IGNORE INTO contact (kind, value, line_type, first_seen, last_seen) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (kind, f"v{r.randint(1, 400)}", line, self.clock, self.clock))

    def evidence(self):
        firm, contact = self.pick("firm"), self.pick("contact")
        if firm is None or contact is None:
            return
        r = self.rng
        person = r.choice([0] + self.ids("person", f"firm = {firm}"))
        t = self.tick()
        self.con.execute(
            "INSERT INTO link_evidence (firm, contact, source, person, trust, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (firm, contact, source, person) "
            "DO UPDATE SET trust = excluded.trust, last_seen = MAX(last_seen, excluded.last_seen)",
            (firm, contact, r.randint(1, 3), person, r.randint(0, 2), t, t))

    def drop_evidence(self):
        rows = self.con.execute("SELECT firm, contact, source, person FROM link_evidence "
                                "ORDER BY 1, 2, 3, 4").fetchall()
        if rows:
            row = self.rng.choice(rows)
            self.con.execute("DELETE FROM link_evidence WHERE firm = ? AND contact = ? "
                             "AND source = ? AND person = ?", row)

    def contact_kind(self):
        c = self.pick("contact")
        if c is not None:
            r = self.rng
            self.con.execute("UPDATE OR IGNORE contact SET kind = ?, line_type = ? WHERE id = ?",
                             (r.choice((1, 2, 3)), r.choice((None, 0, 1, 2)), c))

    def dnc(self):
        c = self.pick("contact")
        if c is not None:
            self.con.execute("UPDATE contact SET do_not_contact = 1 - do_not_contact WHERE id = ?", (c,))

    def person(self):
        firm = self.pick("firm")
        if firm is not None:
            name = self.rng.choice(("ana pop", "ion ionescu", "maria exemplu", "dan test"))
            self.con.execute("INSERT OR IGNORE INTO person (firm, name, name_norm) VALUES (?, ?, ?)",
                             (firm, name.title(), name))

    def drop_person(self):
        p = self.pick("person")
        if p is not None:
            self.con.execute("DELETE FROM person WHERE id = ?", (p,))

    def search(self):
        firm = self.pick("firm")
        if firm is None:
            return
        r = self.rng
        self.con.execute(
            "INSERT INTO search (target, is_person, module, capability, looked_for, at, outcome) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (firm, int(r.random() < 0.1), r.choice((SITE_READ, FINDER, OTHER)),
             r.choice((0, 1, DEEP, DEEP | 1, 2)), self.tick(), r.choice((0, 1, 2, 2))))

    def drop_search(self):
        s = self.pick("search")
        if s is not None:
            self.con.execute("DELETE FROM search WHERE id = ?", (s,))

    def fix_search(self):
        """A search corrected in place: an error retried, or re-pointed."""
        s = self.pick("search")
        if s is None:
            return
        r = self.rng
        if r.random() < 0.5:
            self.con.execute("UPDATE search SET outcome = ?, looked_for = ? WHERE id = ?",
                             (r.choice((0, 1, 2)), r.choice((0, DEEP, DEEP | 1)), s))
        else:
            self.con.execute("UPDATE search SET target = ?, is_person = ? WHERE id = ?",
                             (self.pick("firm"), int(r.random() < 0.1), s))

    def drop_obs(self):
        o = self.pick("obs")
        if o is not None:
            self.con.execute("DELETE FROM obs WHERE id = ?", (o,))

    def move_person(self):
        p, firm = self.pick("person"), self.pick("firm")
        if p is not None and firm is not None:
            self.con.execute("UPDATE OR IGNORE person SET firm = ? WHERE id = ?", (firm, p))

    def drop_contact(self):
        """Contacts no firm links may go; a linked one is refused."""
        c = self.pick("contact")
        if c is None:
            return
        delete = "DELETE FROM contact WHERE id = ?"
        if self.con.execute("SELECT 1 FROM link WHERE contact = ? UNION ALL "
                            "SELECT 1 FROM link_evidence WHERE contact = ?", (c, c)).fetchone():
            with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
                self.con.execute(delete, (c,))
        elif not triggers.names(self.con) and self.con.execute(
                "SELECT 1 FROM temp.touched_link WHERE contact = ?", (c,)).fetchone():
            # In bulk mode, evidence added and dropped again: refused until
            # the recount (on the safe side).
            try:
                self.con.execute(delete, (c,))
            except sqlite3.IntegrityError:
                pass
        else:
            self.con.execute(delete, (c,))

    def obs(self):
        firm = self.pick("firm")
        if firm is None:
            return
        r = self.rng
        self.con.execute(
            "INSERT INTO obs (firm, signal, key, num, text, confidence, module, at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (firm, r.randint(1, 3), r.choice(("", "", "2024")), r.choice((None, 0.0, 1.0, 5.0)),
             r.choice((None, "x")), r.random(), self.tick()))

    def outbox(self):
        r = self.rng
        if r.random() < 0.5 or not self.con.execute("SELECT 1 FROM outbox").fetchone():
            self.con.execute("INSERT INTO outbox (id, status) VALUES (?, ?)",
                             (f"o{r.randint(1, 10**9)}", r.choice(("pending_review", "approved"))))
        else:
            ids = [x[0] for x in self.con.execute("SELECT id FROM outbox ORDER BY id")]
            self.con.execute("UPDATE outbox SET status = ? WHERE id = ?",
                             (r.choice(("sent", "failed", "approved")), r.choice(ids)))

    def website(self):
        firm = self.pick("firm")
        if firm is not None:
            r = self.rng
            if r.random() < 0.5:
                self.con.execute("UPDATE firm SET website = NULL, domain = NULL WHERE id = ?", (firm,))
            else:
                self.con.execute("UPDATE OR IGNORE firm SET website = ?, domain = ? WHERE id = ?",
                                 (f"https://s{firm}.example.com",
                                  f"s{r.randint(1, 50)}.example.com", firm))

    def merge(self):
        a, b = self.pick("firm", "live = 0"), self.pick("firm", "live = 0")
        if a is None or b is None:
            return
        try:
            merges.merge(self.con, a, b, at=self.tick())
        except MergeRefused as e:
            assert e.reason in ("same_firm", "two_legal_ids")

    def undo(self):
        m = self.pick("merge", "undone_at IS NULL")
        if m is None:
            return
        try:
            merges.undo(self.con, m, at=self.tick())
        except MergeRefused as e:
            assert e.reason == "survivor_merged"

    ROW_WRITES = ("firm", "contact", "evidence", "evidence", "evidence", "drop_evidence",
                  "contact_kind", "dnc", "person", "drop_person", "move_person", "search",
                  "drop_search", "fix_search", "obs", "obs", "drop_obs", "outbox", "website",
                  "drop_contact")

    def step(self, writes=ROW_WRITES):
        getattr(self, self.rng.choice(writes))()


@pytest.mark.parametrize("seed", range(6))
def test_row_writes_keep_every_value(seed):
    con = _db()
    w = World(con, random.Random(seed))
    for _ in range(6):
        w.firm()
        w.contact()
    for i in range(400):
        w.step()
        if i % 50 == 49:
            assert _clean(con) == {}, f"after {i + 1} writes"
    assert _clean(con) == {}


@pytest.mark.parametrize("seed", range(6))
def test_merges_and_undo_chains_keep_every_value(seed):
    con = _db()
    w = World(con, random.Random(100 + seed))
    for _ in range(8):
        w.firm()
        w.contact()
    for i in range(300):
        w.step(World.ROW_WRITES + ("merge", "merge", "undo"))
        if i % 25 == 24:
            assert _clean(con) == {}, f"after {i + 1} steps"
    assert _clean(con) == {}


@pytest.mark.parametrize("seed", range(4))
def test_bulk_writes_recount_what_they_touched(seed):
    con = _db()
    w = World(con, random.Random(200 + seed))
    for _ in range(5):
        w.firm()
        w.contact()
    for _round in range(8):
        for _ in range(30):
            w.step()
        with triggers.bulk(con):
            assert triggers.names(con) == set()
            for _ in range(40):
                w.step()
        assert len(triggers.names(con)) == len(triggers.TRIGGERS)
        assert _clean(con) == {}


def test_a_bulk_write_that_fails_leaves_everything_as_it_was():
    con = _db()
    w = World(con, random.Random(7))
    for _ in range(5):
        w.firm()
        w.contact()
        w.evidence()
    before = con.execute("SELECT COUNT(*) FROM link_evidence").fetchone()[0]
    with pytest.raises(RuntimeError):
        with triggers.bulk(con):
            w.evidence()
            w.firm()
            raise RuntimeError("stop")
    assert con.execute("SELECT COUNT(*) FROM link_evidence").fetchone()[0] == before
    assert len(triggers.names(con)) == len(triggers.TRIGGERS)
    assert _clean(con) == {}


def test_fix_writes_a_full_recount():
    con = _db()
    w = World(con, random.Random(9))
    for _ in range(5):
        w.firm()
        w.contact()
    for _ in range(100):
        w.step()
    con.execute("UPDATE firm SET n_contacts = n_contacts + 3, best_trust = 2")
    con.execute("UPDATE contact SET n_firms = 9")
    con.execute("UPDATE tally SET n = n + 1")
    assert _clean(con)
    recount.fix(con)
    assert _clean(con) == {}


def test_repeated_reading_moves_only_checked_at():
    con = _db()
    f = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    ins = "INSERT INTO obs (firm, signal, key, num, at) VALUES (?, 1, '', ?, ?)"
    con.execute(ins, (f, 1.0, 100))
    con.execute(ins, (f, 1.0, 200))
    con.execute(ins, (f, 0.0, 50))           # older and different: history only
    assert con.execute("SELECT COUNT(*) FROM obs").fetchone()[0] == 2
    assert con.execute("SELECT num, at, checked_at, positive FROM reading").fetchone() == (1.0, 100, 200, 1)
    assert con.execute("SELECT n_keys, n_positive FROM firm_signal").fetchone() == (1, 1)
    assert con.execute("SELECT n_true, n_checked FROM signal_tally").fetchone() == (1, 1)
    con.execute(ins, (f, 0.0, 300))
    assert con.execute("SELECT num, at, checked_at, positive FROM reading").fetchone() == (0.0, 300, 300, 0)
    assert con.execute("SELECT n_true, n_checked FROM signal_tally").fetchone() == (0, 1)


def test_counts_follow_the_live_firm_and_merges_refuse_two_legal_ids():
    con = _db()
    a, b, c = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(3))
    con.execute("INSERT INTO firm_ident VALUES (1, 111, ?, 1), (1, 222, ?, 1)", (a, b))
    phone = con.execute("INSERT INTO contact (kind, value, line_type, first_seen, last_seen) "
                        "VALUES (2, '+40700000001', 1, 1, 1)").lastrowid
    for firm, trust in ((a, 1), (c, 2)):
        con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, last_seen) "
                    "VALUES (?, ?, 1, ?, 1, 1)", (firm, phone, trust))
    assert con.execute("SELECT n_firms FROM contact").fetchone() == (2,)
    with pytest.raises(MergeRefused) as e:
        merges.merge(con, a, b)
    assert e.value.reason == "two_legal_ids"
    m = merges.merge(con, a, c)
    assert con.execute("SELECT n_firms, best_trust FROM contact").fetchone() == (1, 1)
    assert con.execute("SELECT n_contacts, n_phone, n_mobile FROM firm WHERE id = ?",
                       (a,)).fetchone() == (1, 1, 1)
    assert con.execute("SELECT n_contacts FROM firm WHERE id = ?", (c,)).fetchone() == (0,)
    # A chain: a into a new firm d, then the older merge can't be undone first.
    d = con.execute("INSERT INTO firm (created) VALUES (2)").lastrowid
    m3 = merges.merge(con, d, a)
    with pytest.raises(MergeRefused) as e:
        merges.undo(con, m)
    assert e.value.reason == "survivor_merged"
    assert merges.undo(con, m3) == [a, c]
    assert con.execute("SELECT live FROM firm WHERE id = ?", (c,)).fetchone() == (a,)
    assert merges.undo(con, m) == [c]
    assert con.execute("SELECT n_firms FROM contact").fetchone() == (2,)
    assert _clean(con) == {}


def test_a_merge_fills_a_missing_website_and_undo_clears_it():
    con = _db()
    a = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    b = con.execute("INSERT INTO firm (created, website, website_trust) "
                    "VALUES (1, 'https://b.example.com', 0)").lastrowid
    m = merges.merge(con, a, b)
    assert con.execute("SELECT website, website_trust, website_fill FROM firm WHERE id = ?",
                       (a,)).fetchone() == ("https://b.example.com", 0, m)
    merges.undo(con, m)
    assert con.execute("SELECT website, website_fill FROM firm WHERE id = ?",
                       (a,)).fetchone() == (None, None)
    assert _clean(con) == {}


# ── Writes the triggers refuse, follow or recount ──

def test_a_person_moved_within_one_firm_counts_once():
    con = _db()
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    merges.merge(con, a, b)
    con.execute("INSERT INTO person (firm, name, name_norm) VALUES (?, 'Ana Pop', 'ana pop')", (b,))
    con.execute("UPDATE person SET firm = ? WHERE firm = ?", (a, b))
    assert con.execute("SELECT n_people FROM firm WHERE id = ?", (a,)).fetchone() == (1,)
    assert _clean(con) == {}


def test_a_search_corrected_in_place_moves_the_search_times():
    con = _db()
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    con.execute("INSERT INTO search (target, module, capability, looked_for, at, outcome) "
                "VALUES (?, 1, ?, ?, 100, 0)", (a, SITE_READ, DEEP))
    assert con.execute("SELECT site_read_at FROM firm WHERE id = ?", (a,)).fetchone() == (None,)
    con.execute("UPDATE search SET outcome = 1")                 # the error, retried: found
    assert con.execute("SELECT site_read_at, site_read_deep FROM firm WHERE id = ?",
                       (a,)).fetchone() == (100, 100)
    con.execute("UPDATE search SET target = ?", (b,))            # re-pointed
    assert con.execute("SELECT last_search_at FROM firm WHERE id = ?", (a,)).fetchone() == (None,)
    assert con.execute("SELECT last_search_at FROM firm WHERE id = ?", (b,)).fetchone() == (100,)
    assert _clean(con) == {}
    with triggers.bulk(con):
        con.execute("UPDATE search SET target = ?, capability = ?", (a, FINDER))
    assert con.execute("SELECT finder_at FROM firm WHERE id = ?", (a,)).fetchone() == (100,)
    assert _clean(con) == {}


def test_deleting_the_current_observation_makes_the_one_before_current():
    con = _db()
    f = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    ins = "INSERT INTO obs (firm, signal, key, num, at) VALUES (?, 1, '', ?, ?)"
    con.execute(ins, (f, 1.0, 100))
    con.execute(ins, (f, 0.0, 200))
    con.execute("DELETE FROM obs WHERE at = 200")
    assert con.execute("SELECT num, at, positive FROM reading").fetchone() == (1.0, 100, 1)
    assert con.execute("SELECT n_true, n_checked FROM signal_tally").fetchone() == (1, 1)
    assert _clean(con) == {}
    con.execute("DELETE FROM obs")
    assert con.execute("SELECT COUNT(*) FROM reading").fetchone() == (0,)
    assert _clean(con) == {}


def test_guarded_writes_are_refused_outside_bulk_mode():
    con = _db()
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    con.execute("INSERT INTO obs (firm, signal, key, num, at) VALUES (?, 1, '', 1, 100)", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="never edited"):
        con.execute("UPDATE obs SET num = 0")
    con.execute("UPDATE obs SET evidence_url = 'https://a.example.com/'")   # not history
    with pytest.raises(sqlite3.IntegrityError, match="merge"):
        con.execute("UPDATE firm SET live = ? WHERE id = ?", (a, b))
    with pytest.raises(sqlite3.IntegrityError, match="added live"):
        con.execute("INSERT INTO firm (created, live) VALUES (1, ?)", (a,))
    c = con.execute("INSERT INTO contact (kind, value, first_seen, last_seen) "
                    "VALUES (2, '+15550000009', 1, 1)").lastrowid
    con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, last_seen) "
                "VALUES (?, ?, 1, 0, 1, 1)", (a, c))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        con.execute("DELETE FROM contact WHERE id = ?", (c,))
    merges.merge(con, a, b)
    with pytest.raises(sqlite3.IntegrityError, match="undo those merges"):
        con.execute("DELETE FROM firm WHERE id = ?", (a,))
    assert _clean(con) == {}
    # Contacts are never discarded, bulk mode or not (there, also one whose
    # evidence the bulk write added, before the recount links it).
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        with triggers.bulk(con):
            con.execute("DELETE FROM contact WHERE id = ?", (c,))
    d = con.execute("INSERT INTO contact (kind, value, first_seen, last_seen) "
                    "VALUES (1, 'ana@example.com', 1, 1)").lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        with triggers.bulk(con):
            con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, "
                        "last_seen) VALUES (?, ?, 1, 0, 1, 1)", (a, d))
            con.execute("DELETE FROM contact WHERE id = ?", (d,))
    assert triggers.missing(con) == []
    assert _clean(con) == {}


def test_a_numeric_scheme_keeps_its_keys_as_integers():
    con = _db()
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    con.execute("INSERT INTO firm_ident VALUES (1, 1000004, ?, 1)", (a,))
    with pytest.raises(sqlite3.IntegrityError, match="integers"):
        con.execute("INSERT INTO firm_ident VALUES (1, '1000004', ?, 1)", (b,))
    with pytest.raises(sqlite3.IntegrityError, match="integers"):
        con.execute("UPDATE firm_ident SET key = '1000004'")
    con.execute("INSERT INTO firm_ident VALUES (2, 'L-1', ?, 1)", (b,))     # a text scheme
    merges.merge(con, a, b)
    assert _clean(con) == {}


def test_a_record_with_another_registration_number_merges():
    """A registration-style scheme that isn't legal (one tax id can have
    several registrations): the record gains the tax id by a merge."""
    con = _db()
    con.execute("INSERT INTO ident_scheme (id, name, module, numeric, legal) "
                "VALUES (3, 'example.reg_no', 1, 0, 0)")
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    con.execute("INSERT INTO firm_ident VALUES (1, 1000004, ?, 1), (3, 'R/1/2015', ?, 1)", (a, a))
    con.execute("INSERT INTO firm_ident VALUES (3, 'R/9/2020', ?, 1)", (b,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO firm_ident VALUES (1, 1000004, ?, 2)", (b,))
    merges.merge(con, a, b)
    assert con.execute("SELECT live FROM firm WHERE id = ?", (b,)).fetchone() == (a,)
    assert _clean(con) == {}


def test_a_bulk_body_that_commits_never_loses_the_triggers():
    con = sqlite3.connect(":memory:")                # default transaction control
    schema.create(con)
    con.commit()
    with pytest.raises(RuntimeError, match="later failure"):
        with triggers.bulk(con):
            con.execute("INSERT INTO firm (created) VALUES (1)")
            con.commit()                             # a helper's own commit
            con.execute("INSERT INTO firm (created) VALUES (2)")
            raise RuntimeError("later failure")
    assert triggers.missing(con) == []
    assert con.execute("SELECT COUNT(*) FROM firm").fetchone() == (1,)   # the committed part
    assert _clean(con) == {}
    # With no transaction control, a commit ends bulk()'s transaction: said so.
    con = sqlite3.connect(":memory:", isolation_level=None)
    schema.create(con)
    with pytest.raises(triggers.BulkCommitted):
        with triggers.bulk(con):
            con.execute("INSERT INTO firm (created) VALUES (1)")
            con.execute("COMMIT")
            con.execute("INSERT INTO firm (created) VALUES (2)")
    assert triggers.missing(con) == []
    assert _clean(con) == {}


def test_fix_keeps_history_the_trigger_path_kept():
    con = _db()
    f = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    for num, at in ((1, 10), (0, 20), (1, 15)):          # 15 arrives last: history
        con.execute("INSERT INTO obs (firm, signal, key, num, at) VALUES (?, 1, '', ?, ?)",
                    (f, num, at))
    assert _clean(con) == {}
    recount.fix(con)
    assert con.execute("SELECT COUNT(*) FROM obs").fetchone() == (3,)
    assert _clean(con) == {}


def test_bulk_folds_only_the_repeats_it_added():
    con = _db()
    f = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    ins = "INSERT INTO obs (firm, signal, key, num, at) VALUES (?, 1, '', ?, ?)"
    for num, at in ((1, 10), (0, 20), (1, 15)):
        con.execute(ins, (f, num, at))
    with triggers.bulk(con):
        con.execute(ins, (f, 0.0, 30))                  # repeats the current value: folded
        con.execute(ins, (f, 1.0, 5))                   # older: history
    assert sorted(r[0] for r in con.execute("SELECT at FROM obs")) == [5, 10, 15, 20]
    assert con.execute("SELECT num, at, checked_at FROM reading").fetchone() == (0.0, 20, 30)
    assert _clean(con) == {}


def test_undo_leaves_a_website_written_after_the_merge():
    con = _db()
    a = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    b = con.execute("INSERT INTO firm (created, website) VALUES (1, 'https://b.example.com')").lastrowid
    m = merges.merge(con, a, b)
    con.execute("UPDATE firm SET website = 'https://a.example.com' WHERE id = ?", (a,))
    merges.undo(con, m)
    assert con.execute("SELECT website, website_fill FROM firm WHERE id = ?",
                       (a,)).fetchone() == ("https://a.example.com", None)
    assert _clean(con) == {}


def test_a_role_set_after_the_searches_counts_them():
    con = sqlite3.connect(":memory:", isolation_level=None)
    schema.create(con)
    a = con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid
    con.execute("INSERT INTO search (target, module, capability, looked_for, at, outcome) "
                "VALUES (?, 1, ?, ?, 100, 1)", (a, SITE_READ, DEEP))
    schema.set_role(con, "site_read", SITE_READ)
    schema.set_role(con, "deep_bit", DEEP)
    assert con.execute("SELECT site_read_at, site_read_deep FROM firm").fetchone() == (100, 100)
    schema.set_role(con, "site_read", OTHER)
    assert con.execute("SELECT site_read_at FROM firm").fetchone() == (None,)
    assert _clean(con) == {}


def test_the_diff_sees_missing_triggers_and_live_against_merges():
    con = _db()
    a, b = (con.execute("INSERT INTO firm (created) VALUES (1)").lastrowid for _ in range(2))
    m = merges.merge(con, a, b)
    con.execute("DROP TRIGGER kept_firm_live")
    con.execute("UPDATE merge SET undone_at = 1 WHERE id = ?", (m,))     # undone behind its back
    found = _clean(con)
    assert found == {"triggers.missing": 1, "firm.live_merges": 1}
    con.execute("UPDATE merge SET undone_at = NULL")
    con.execute("DROP TRIGGER guard_ident_numeric")
    recount.fix(con)
    assert _clean(con) == {}
