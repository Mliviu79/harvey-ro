"""The triggers that keep the overlay's counts and current readings, and
bulk mode.

Row-at-a-time writes (the finder, a Discover listing, a person found) keep
every value through these triggers. Bulk writes (a registry pull, a
migration, a merge) run in ``bulk()``: the kept-value triggers are dropped
inside the transaction, lightweight temporary triggers note which rows were
touched, and the touched values are recounted from base rows before the
triggers come back and the transaction commits. The same recount is
``harvey doctor --recount`` (recount.py), so the two paths are checked
against each other.

Every kept value lives on the live firm: a merged record's links, readings
and searches count on the firm it is merged into, and a merged record keeps
zeros. Merges move no rows (merges.py recounts the group instead).

Writes no trigger could keep cheaply are refused outside bulk mode (the
guards among the triggers below): a firm's ``live`` changes only in a merge
or its undo, and history (``obs``) is never edited. In bulk mode those
guards are off with the rest, and the recount follows whatever the bulk
write did. (A contact some firm links is never deleted, bulk mode or not:
schema.GUARDS.)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from harvey.overlay.schema import EMAIL, ERROR, MOBILE, PHONE

PREFIX = "kept_"


def _live(x: str) -> str:
    """The live firm of record ``x`` (itself, or the firm it is merged into)."""
    return f"(SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm WHERE id = {x})"


def _group(live: str) -> str:
    """The records of live firm ``live``: itself and the records merged into it."""
    return f"(SELECT {live} UNION ALL SELECT id FROM firm WHERE live = {live} AND live != 0)"


def _tally(name: str, delta: str) -> str:
    return (f"INSERT INTO tally (name, n) SELECT {name}, {delta} WHERE ({delta}) != 0 "
            f"ON CONFLICT (name) DO UPDATE SET n = n + excluded.n;")


def _role(name: str) -> str:
    return f"(SELECT id FROM role WHERE name = '{name}')"


def _other_member_links(firm: str, contact: str) -> str:
    """Another record of the same live firm links the contact too."""
    return (f"EXISTS (SELECT 1 FROM link l2 WHERE l2.contact = {contact} AND l2.firm != {firm} "
            f"AND l2.firm IN {_group(_live(firm))})")


def _kind_counts(sign: str, contact: str, firm: str) -> str:
    """firm.n_* moved by one contact (``sign`` + or -) on the live firm of
    ``firm``, unless another record of that firm links the contact."""
    return f"""
  UPDATE firm SET
    n_contacts = n_contacts {sign} 1,
    n_email = n_email {sign} (SELECT kind = {EMAIL} FROM contact WHERE id = {contact}),
    n_phone = n_phone {sign} (SELECT kind = {PHONE} FROM contact WHERE id = {contact}),
    n_mobile = n_mobile {sign} (SELECT kind = {PHONE} AND line_type IS {MOBILE}
                                FROM contact WHERE id = {contact})
  WHERE id = {_live(firm)} AND NOT {_other_member_links(firm, contact)};"""


def _best_firm_trust(firm: str) -> str:
    live = _live(firm)
    return (f"UPDATE firm SET best_trust = (SELECT MIN(l.trust) FROM link l WHERE l.firm IN "
            f"{_group(live)}) WHERE id = {live};")


def _search_times(target: str, cond: str = "1") -> str:
    """The live firm's search times, again from every search of its records
    (when ``cond`` holds)."""
    live = _live(target)
    return f"""
  UPDATE firm SET
    last_search_at = (SELECT MAX(s.at) FROM search s WHERE s.is_person = 0
                      AND s.target IN {_group(live)} AND s.outcome != {ERROR}),
    site_read_at = (SELECT MAX(s.at) FROM search s WHERE s.is_person = 0
                    AND s.target IN {_group(live)} AND s.outcome != {ERROR}
                    AND s.capability = {_role('site_read')}),
    site_read_deep = (SELECT MAX(s.at) FROM search s WHERE s.is_person = 0
                      AND s.target IN {_group(live)} AND s.outcome != {ERROR}
                      AND s.capability = {_role('site_read')}
                      AND (s.looked_for & {_role('deep_bit')}) != 0),
    finder_at = (SELECT MAX(s.at) FROM search s WHERE s.is_person = 0
                 AND s.target IN {_group(live)} AND s.outcome != {ERROR}
                 AND s.capability = {_role('finder')})
  WHERE id = {live} AND {cond};"""


_LINK_FROM_EVIDENCE = """
  UPDATE link SET
    trust = (SELECT MIN(trust) FROM link_evidence e WHERE e.firm = {f} AND e.contact = {c}),
    person = (SELECT MAX(person) FROM link_evidence e WHERE e.firm = {f} AND e.contact = {c}),
    first_seen = (SELECT MIN(first_seen) FROM link_evidence e WHERE e.firm = {f} AND e.contact = {c}),
    last_seen = (SELECT MAX(last_seen) FROM link_evidence e WHERE e.firm = {f} AND e.contact = {c})
  WHERE firm = {f} AND contact = {c};"""

_FIRM_TALLIES = (
    ("'firms'", "live = 0"),
    ("'firms_with_contact'", "live = 0 AND n_contacts > 0"),
    ("'firms_with_site'", "live = 0 AND (website IS NOT NULL OR domain IS NOT NULL)"),
    ("'firms_merged'", "live != 0"),
)
_CONTACT_TALLIES = (
    ("'contacts'", "1"),
    ("'contacts_email'", f"kind = {EMAIL}"),
    ("'contacts_phone'", f"kind = {PHONE}"),
    ("'contacts_mobile'", f"kind = {PHONE} AND line_type IS {MOBILE}"),
    ("'contacts_do_not_contact'", "do_not_contact != 0"),
)


def _row_cond(cond: str, row: str) -> str:
    """A condition on a table's columns, rewritten onto NEW or OLD."""
    import re

    words = ("live", "n_contacts", "website", "domain", "kind", "line_type", "do_not_contact")
    return re.sub(r"\b(" + "|".join(words) + r")\b", lambda m: f"{row}.{m.group(1)}", cond)


def _tallies_for(pairs, sign_new: bool, sign_old: bool) -> str:
    out = []
    for name, cond in pairs:
        parts = []
        if sign_new:
            parts.append(f"({_row_cond(cond, 'NEW')})")
        if sign_old:
            parts.append(f"- ({_row_cond(cond, 'OLD')})")
        out.append(_tally(name, " ".join(parts) if sign_new else f"0 {' '.join(parts)}"))
    return "\n  ".join(out)


TRIGGERS = {
    # ── Current readings ──
    # A reading equal to the current one changes nothing but when it was
    # last seen: it is not history.
    "obs_same": f"""
CREATE TRIGGER {PREFIX}obs_same BEFORE INSERT ON obs
WHEN EXISTS (SELECT 1 FROM reading r WHERE r.firm = NEW.firm AND r.signal = NEW.signal
             AND r.key = NEW.key AND r.num IS NEW.num AND r.text IS NEW.text)
BEGIN
  UPDATE reading SET checked_at = MAX(checked_at, NEW.at)
  WHERE firm = NEW.firm AND signal = NEW.signal AND key = NEW.key;
  SELECT RAISE(IGNORE);
END""",
    # The newest reading is current (an older one arriving late is history).
    "obs_ins": f"""
CREATE TRIGGER {PREFIX}obs_ins AFTER INSERT ON obs BEGIN
  INSERT INTO reading (firm, signal, key, num, text, confidence, module, at, checked_at, obs, positive)
  VALUES (NEW.firm, NEW.signal, NEW.key, NEW.num, NEW.text, NEW.confidence, NEW.module,
          NEW.at, NEW.at, NEW.id, NEW.num IS NULL OR NEW.num != 0)
  ON CONFLICT (firm, signal, key) DO UPDATE SET
    num = excluded.num, text = excluded.text, confidence = excluded.confidence,
    module = excluded.module, at = excluded.at,
    checked_at = MAX(reading.checked_at, excluded.at), obs = excluded.obs,
    positive = excluded.positive
  WHERE excluded.at >= reading.at;
END""",
    # The current reading deleted: the newest observation left is current.
    "obs_del": f"""
CREATE TRIGGER {PREFIX}obs_del AFTER DELETE ON obs
WHEN EXISTS (SELECT 1 FROM reading r WHERE r.firm = OLD.firm AND r.signal = OLD.signal
             AND r.key = OLD.key AND r.obs = OLD.id)
BEGIN
  DELETE FROM reading WHERE firm = OLD.firm AND signal = OLD.signal AND key = OLD.key;
  INSERT INTO reading (firm, signal, key, num, text, confidence, module, at, checked_at, obs, positive)
  SELECT firm, signal, key, num, text, confidence, module, at, at, id, num IS NULL OR num != 0
  FROM obs WHERE firm = OLD.firm AND signal = OLD.signal AND key = OLD.key
  ORDER BY at DESC, id DESC LIMIT 1;
END""",
    "obs_edit": f"""
CREATE TRIGGER {PREFIX}obs_edit BEFORE UPDATE OF firm, signal, key, num, text, confidence, module, at
ON obs BEGIN
  SELECT RAISE(ABORT, 'history is never edited: delete the observation and add another');
END""",
    "reading_ins": f"""
CREATE TRIGGER {PREFIX}reading_ins AFTER INSERT ON reading BEGIN
  INSERT INTO firm_signal (firm, signal, n_keys, n_positive)
  VALUES ({_live('NEW.firm')}, NEW.signal, 1, NEW.positive)
  ON CONFLICT (signal, firm) DO UPDATE SET n_keys = n_keys + 1,
    n_positive = n_positive + excluded.n_positive;
END""",
    "reading_upd": f"""
CREATE TRIGGER {PREFIX}reading_upd AFTER UPDATE OF positive ON reading
WHEN NEW.positive != OLD.positive BEGIN
  UPDATE firm_signal SET n_positive = n_positive + NEW.positive - OLD.positive
  WHERE signal = NEW.signal AND firm = {_live('NEW.firm')};
END""",
    "reading_del": f"""
CREATE TRIGGER {PREFIX}reading_del AFTER DELETE ON reading BEGIN
  UPDATE firm_signal SET n_keys = n_keys - 1, n_positive = n_positive - OLD.positive
  WHERE signal = OLD.signal AND firm = {_live('OLD.firm')};
  DELETE FROM firm_signal WHERE signal = OLD.signal AND firm = {_live('OLD.firm')} AND n_keys <= 0;
END""",
    "firm_signal_ins": f"""
CREATE TRIGGER {PREFIX}firm_signal_ins AFTER INSERT ON firm_signal BEGIN
  INSERT INTO signal_tally (signal, n_true, n_checked) VALUES (NEW.signal, NEW.n_positive > 0, 1)
  ON CONFLICT (signal) DO UPDATE SET n_true = n_true + excluded.n_true, n_checked = n_checked + 1;
END""",
    "firm_signal_upd": f"""
CREATE TRIGGER {PREFIX}firm_signal_upd AFTER UPDATE ON firm_signal BEGIN
  UPDATE signal_tally SET n_true = n_true + (NEW.n_positive > 0) - (OLD.n_positive > 0)
  WHERE signal = NEW.signal;
END""",
    "firm_signal_del": f"""
CREATE TRIGGER {PREFIX}firm_signal_del AFTER DELETE ON firm_signal BEGIN
  UPDATE signal_tally SET n_true = n_true - (OLD.n_positive > 0), n_checked = n_checked - 1
  WHERE signal = OLD.signal;
  DELETE FROM signal_tally WHERE signal = OLD.signal AND n_checked <= 0;
END""",

    # ── Links from their evidence ──
    "evidence_ins": f"""
CREATE TRIGGER {PREFIX}evidence_ins AFTER INSERT ON link_evidence BEGIN
  INSERT INTO link (firm, contact, trust, person, first_seen, last_seen)
  VALUES (NEW.firm, NEW.contact, NEW.trust, NEW.person, NEW.first_seen, NEW.last_seen)
  ON CONFLICT (firm, contact) DO UPDATE SET
    trust = MIN(link.trust, excluded.trust), person = MAX(link.person, excluded.person),
    first_seen = MIN(link.first_seen, excluded.first_seen),
    last_seen = MAX(link.last_seen, excluded.last_seen);
END""",
    "evidence_upd": f"""
CREATE TRIGGER {PREFIX}evidence_upd AFTER UPDATE ON link_evidence BEGIN
  {_LINK_FROM_EVIDENCE.format(f='NEW.firm', c='NEW.contact')}
END""",
    "evidence_del": f"""
CREATE TRIGGER {PREFIX}evidence_del AFTER DELETE ON link_evidence BEGIN
  DELETE FROM link WHERE firm = OLD.firm AND contact = OLD.contact AND NOT EXISTS (
    SELECT 1 FROM link_evidence e WHERE e.firm = OLD.firm AND e.contact = OLD.contact);
  {_LINK_FROM_EVIDENCE.format(f='OLD.firm', c='OLD.contact')}
END""",
    "evidence_keys": f"""
CREATE TRIGGER {PREFIX}evidence_keys BEFORE UPDATE OF firm, contact ON link_evidence
WHEN NEW.firm != OLD.firm OR NEW.contact != OLD.contact BEGIN
  SELECT RAISE(ABORT, 'evidence never moves: delete it and add another');
END""",
    "link_keys": f"""
CREATE TRIGGER {PREFIX}link_keys BEFORE UPDATE OF firm, contact ON link
WHEN NEW.firm != OLD.firm OR NEW.contact != OLD.contact BEGIN
  SELECT RAISE(ABORT, 'a link never moves: delete it and add another');
END""",

    # ── Firm and contact counts from links ──
    "link_ins": f"""
CREATE TRIGGER {PREFIX}link_ins AFTER INSERT ON link BEGIN
  UPDATE contact SET
    n_firms = n_firms + NOT {_other_member_links('NEW.firm', 'NEW.contact')},
    best_trust = MIN(COALESCE(best_trust, NEW.trust), NEW.trust)
  WHERE id = NEW.contact;
  {_kind_counts('+', 'NEW.contact', 'NEW.firm')}
  UPDATE firm SET best_trust = MIN(COALESCE(best_trust, NEW.trust), NEW.trust)
  WHERE id = {_live('NEW.firm')};
END""",
    "link_del": f"""
CREATE TRIGGER {PREFIX}link_del AFTER DELETE ON link BEGIN
  UPDATE contact SET
    n_firms = n_firms - NOT {_other_member_links('OLD.firm', 'OLD.contact')},
    best_trust = (SELECT MIN(trust) FROM link WHERE contact = OLD.contact)
  WHERE id = OLD.contact;
  {_kind_counts('-', 'OLD.contact', 'OLD.firm')}
  {_best_firm_trust('OLD.firm')}
END""",
    "link_trust": f"""
CREATE TRIGGER {PREFIX}link_trust AFTER UPDATE OF trust ON link
WHEN NEW.trust != OLD.trust BEGIN
  UPDATE contact SET best_trust = (SELECT MIN(trust) FROM link WHERE contact = NEW.contact)
  WHERE id = NEW.contact;
  {_best_firm_trust('NEW.firm')}
END""",
    "contact_kind": f"""
CREATE TRIGGER {PREFIX}contact_kind AFTER UPDATE OF kind, line_type ON contact
WHEN NEW.kind != OLD.kind OR NEW.line_type IS NOT OLD.line_type BEGIN
  UPDATE firm SET
    n_email = n_email - (OLD.kind = {EMAIL}) + (NEW.kind = {EMAIL}),
    n_phone = n_phone - (OLD.kind = {PHONE}) + (NEW.kind = {PHONE}),
    n_mobile = n_mobile - (OLD.kind = {PHONE} AND OLD.line_type IS {MOBILE})
                        + (NEW.kind = {PHONE} AND NEW.line_type IS {MOBILE})
  WHERE id IN (SELECT CASE g.live WHEN 0 THEN g.id ELSE g.live END
               FROM link l JOIN firm g ON g.id = l.firm WHERE l.contact = NEW.id);
END""",

    # ── People ──
    "person_ins": f"""
CREATE TRIGGER {PREFIX}person_ins AFTER INSERT ON person BEGIN
  UPDATE firm SET n_people = n_people + 1
  WHERE id = {_live('NEW.firm')} AND NOT EXISTS (
    SELECT 1 FROM person p WHERE p.name_norm = NEW.name_norm AND p.id != NEW.id
    AND p.firm IN {_group(_live('NEW.firm'))});
  {_tally("'people'", "1")}
END""",
    "person_del": f"""
CREATE TRIGGER {PREFIX}person_del AFTER DELETE ON person BEGIN
  UPDATE firm SET n_people = n_people - 1
  WHERE id = {_live('OLD.firm')} AND NOT EXISTS (
    SELECT 1 FROM person p WHERE p.name_norm = OLD.name_norm
    AND p.firm IN {_group(_live('OLD.firm'))});
  {_tally("'people'", "-1")}
END""",
    # (A person moved between two records of one live firm, same name:
    # nothing changes.)
    "person_upd": f"""
CREATE TRIGGER {PREFIX}person_upd AFTER UPDATE OF firm, name_norm ON person
WHEN NEW.name_norm != OLD.name_norm OR {_live('NEW.firm')} IS NOT {_live('OLD.firm')} BEGIN
  UPDATE firm SET n_people = n_people - 1
  WHERE id = {_live('OLD.firm')} AND NOT EXISTS (
    SELECT 1 FROM person p WHERE p.name_norm = OLD.name_norm
    AND p.firm IN {_group(_live('OLD.firm'))});
  UPDATE firm SET n_people = n_people + 1
  WHERE id = {_live('NEW.firm')} AND NOT EXISTS (
    SELECT 1 FROM person p WHERE p.name_norm = NEW.name_norm AND p.id != NEW.id
    AND p.firm IN {_group(_live('NEW.firm'))});
END""",

    # ── Search times ──
    "search_ins": f"""
CREATE TRIGGER {PREFIX}search_ins AFTER INSERT ON search
WHEN NEW.is_person = 0 AND NEW.outcome != {ERROR} BEGIN
  UPDATE firm SET
    last_search_at = MAX(COALESCE(last_search_at, NEW.at), NEW.at),
    site_read_at = CASE WHEN NEW.capability = {_role('site_read')}
                        THEN MAX(COALESCE(site_read_at, NEW.at), NEW.at) ELSE site_read_at END,
    site_read_deep = CASE WHEN NEW.capability = {_role('site_read')}
                           AND (NEW.looked_for & {_role('deep_bit')}) != 0
                          THEN MAX(COALESCE(site_read_deep, NEW.at), NEW.at)
                          ELSE site_read_deep END,
    finder_at = CASE WHEN NEW.capability = {_role('finder')}
                     THEN MAX(COALESCE(finder_at, NEW.at), NEW.at) ELSE finder_at END
  WHERE id = {_live('NEW.target')};
END""",
    "search_del": f"""
CREATE TRIGGER {PREFIX}search_del AFTER DELETE ON search
WHEN OLD.is_person = 0 BEGIN
  {_search_times('OLD.target')}
END""",
    # A search corrected in place (an error retried, a target re-pointed):
    # both firms' times again.
    "search_upd": f"""
CREATE TRIGGER {PREFIX}search_upd AFTER UPDATE OF target, is_person, capability, looked_for, at,
outcome ON search BEGIN
  {_search_times('OLD.target', 'OLD.is_person = 0')}
  {_search_times('NEW.target', 'NEW.is_person = 0')}
END""",

    # ── Guards (off in bulk mode, where the recount follows) ──
    "firm_live": f"""
CREATE TRIGGER {PREFIX}firm_live BEFORE UPDATE OF live ON firm
WHEN NEW.live IS NOT OLD.live BEGIN
  SELECT RAISE(ABORT, 'live changes only in a merge or its undo (harvey.overlay.merges)');
END""",
    "firm_live_new": f"""
CREATE TRIGGER {PREFIX}firm_live_new BEFORE INSERT ON firm
WHEN NEW.live != 0 BEGIN
  SELECT RAISE(ABORT, 'a firm is added live; merges point it elsewhere');
END""",


    # ── Tallies (counts with no clock) ──
    "firm_ins": f"""
CREATE TRIGGER {PREFIX}firm_ins AFTER INSERT ON firm BEGIN
  {_tallies_for(_FIRM_TALLIES, True, False)}
END""",
    "firm_del": f"""
CREATE TRIGGER {PREFIX}firm_del AFTER DELETE ON firm BEGIN
  {_tallies_for(_FIRM_TALLIES, False, True)}
END""",
    "firm_upd": f"""
CREATE TRIGGER {PREFIX}firm_upd AFTER UPDATE OF live, n_contacts, website, domain ON firm BEGIN
  {_tallies_for(_FIRM_TALLIES, True, True)}
END""",
    "contact_ins": f"""
CREATE TRIGGER {PREFIX}contact_ins AFTER INSERT ON contact BEGIN
  {_tallies_for(_CONTACT_TALLIES, True, False)}
END""",
    "contact_del": f"""
CREATE TRIGGER {PREFIX}contact_del AFTER DELETE ON contact BEGIN
  {_tallies_for(_CONTACT_TALLIES, False, True)}
END""",
    "contact_upd": f"""
CREATE TRIGGER {PREFIX}contact_upd AFTER UPDATE OF kind, line_type, do_not_contact ON contact BEGIN
  {_tallies_for(_CONTACT_TALLIES, True, True)}
END""",
    "outbox_ins": f"""
CREATE TRIGGER {PREFIX}outbox_ins AFTER INSERT ON outbox BEGIN
  {_tally("'outbox:' || COALESCE(NEW.status, '')", "1")}
END""",
    "outbox_del": f"""
CREATE TRIGGER {PREFIX}outbox_del AFTER DELETE ON outbox BEGIN
  {_tally("'outbox:' || COALESCE(OLD.status, '')", "-1")}
END""",
    "outbox_upd": f"""
CREATE TRIGGER {PREFIX}outbox_upd AFTER UPDATE OF status ON outbox
WHEN NEW.status IS NOT OLD.status BEGIN
  {_tally("'outbox:' || COALESCE(OLD.status, '')", "-1")}
  {_tally("'outbox:' || COALESCE(NEW.status, '')", "1")}
END""",
}


def install(con: sqlite3.Connection) -> None:
    """Create every kept-value trigger (those missing)."""
    have = names(con)
    for name, sql in TRIGGERS.items():
        if PREFIX + name not in have:
            con.execute(sql)


def drop(con: sqlite3.Connection) -> None:
    for name in names(con):
        con.execute(f"DROP TRIGGER {name}")


def names(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'trigger' AND name LIKE ?",
        (PREFIX + "%",))}


def missing(con: sqlite3.Connection) -> list[str]:
    """The kept-value triggers and guards the schema should have and hasn't
    (a bulk write cut short outside its transaction would leave them out)."""
    from harvey.overlay.schema import GUARD_NAMES

    have = {r[0] for r in con.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'trigger'")}
    want = [PREFIX + n for n in TRIGGERS] + list(GUARD_NAMES)
    return [n for n in want if n not in have]


# ── Bulk mode ──

# While the kept-value triggers are off, these note what a bulk write
# touched; the recount at the end reads them.
_TOUCH = """
CREATE TEMP TABLE IF NOT EXISTS touched_firm (id INTEGER PRIMARY KEY);
CREATE TEMP TABLE IF NOT EXISTS touched_contact (id INTEGER PRIMARY KEY);
CREATE TEMP TABLE IF NOT EXISTS touched_link (firm INTEGER, contact INTEGER,
                                              PRIMARY KEY (firm, contact)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS temp.touched_link_contact ON touched_link(contact);
CREATE TEMP TABLE IF NOT EXISTS touched_reading (firm INTEGER, signal INTEGER, key TEXT,
                                                 PRIMARY KEY (firm, signal, key)) WITHOUT ROWID;
CREATE TEMP TABLE IF NOT EXISTS touched_obs (id INTEGER PRIMARY KEY);
CREATE TEMP TRIGGER bulk_firm_ins AFTER INSERT ON main.firm BEGIN
  INSERT INTO touched_firm VALUES (NEW.id) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_firm_upd AFTER UPDATE ON main.firm BEGIN
  INSERT INTO touched_firm VALUES (NEW.id) ON CONFLICT DO NOTHING;
  INSERT INTO touched_firm SELECT OLD.live WHERE OLD.live != 0 ON CONFLICT DO NOTHING;
  INSERT INTO touched_firm SELECT NEW.live WHERE NEW.live != 0 ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_firm_del AFTER DELETE ON main.firm BEGIN
  INSERT INTO touched_firm SELECT OLD.live WHERE OLD.live != 0 ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_evidence_ins AFTER INSERT ON main.link_evidence BEGIN
  INSERT INTO touched_link VALUES (NEW.firm, NEW.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_evidence_upd AFTER UPDATE ON main.link_evidence BEGIN
  INSERT INTO touched_link VALUES (NEW.firm, NEW.contact) ON CONFLICT DO NOTHING;
  INSERT INTO touched_link VALUES (OLD.firm, OLD.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_evidence_del AFTER DELETE ON main.link_evidence BEGIN
  INSERT INTO touched_link VALUES (OLD.firm, OLD.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_link_ins AFTER INSERT ON main.link BEGIN
  INSERT INTO touched_link VALUES (NEW.firm, NEW.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_link_upd AFTER UPDATE ON main.link BEGIN
  INSERT INTO touched_link VALUES (NEW.firm, NEW.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_link_del AFTER DELETE ON main.link BEGIN
  INSERT INTO touched_link VALUES (OLD.firm, OLD.contact) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_contact_ins AFTER INSERT ON main.contact BEGIN
  INSERT INTO touched_contact VALUES (NEW.id) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_contact_upd AFTER UPDATE ON main.contact BEGIN
  INSERT INTO touched_contact VALUES (NEW.id) ON CONFLICT DO NOTHING;
END;
-- Links lag their evidence until the recount: a contact with evidence
-- added here is linked already (schema.GUARDS refuses the rest).
CREATE TEMP TRIGGER bulk_contact_linked BEFORE DELETE ON main.contact
WHEN EXISTS (SELECT 1 FROM touched_link WHERE contact = OLD.id) BEGIN
  SELECT RAISE(ABORT, 'a contact some firm links is never deleted');
END;

CREATE TEMP TRIGGER bulk_person_ins AFTER INSERT ON main.person BEGIN
  INSERT INTO touched_firm VALUES (NEW.firm) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_person_upd AFTER UPDATE ON main.person BEGIN
  INSERT INTO touched_firm VALUES (NEW.firm) ON CONFLICT DO NOTHING;
  INSERT INTO touched_firm VALUES (OLD.firm) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_person_del AFTER DELETE ON main.person BEGIN
  INSERT INTO touched_firm VALUES (OLD.firm) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_search_ins AFTER INSERT ON main.search WHEN NEW.is_person = 0 BEGIN
  INSERT INTO touched_firm VALUES (NEW.target) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_search_del AFTER DELETE ON main.search WHEN OLD.is_person = 0 BEGIN
  INSERT INTO touched_firm VALUES (OLD.target) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_search_upd AFTER UPDATE ON main.search BEGIN
  INSERT INTO touched_firm SELECT OLD.target WHERE OLD.is_person = 0 ON CONFLICT DO NOTHING;
  INSERT INTO touched_firm SELECT NEW.target WHERE NEW.is_person = 0 ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_obs_ins AFTER INSERT ON main.obs BEGIN
  INSERT INTO touched_reading VALUES (NEW.firm, NEW.signal, NEW.key) ON CONFLICT DO NOTHING;
  INSERT INTO touched_obs VALUES (NEW.id) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_obs_upd AFTER UPDATE ON main.obs BEGIN
  INSERT INTO touched_reading VALUES (OLD.firm, OLD.signal, OLD.key) ON CONFLICT DO NOTHING;
  INSERT INTO touched_reading VALUES (NEW.firm, NEW.signal, NEW.key) ON CONFLICT DO NOTHING;
  INSERT INTO touched_obs VALUES (NEW.id) ON CONFLICT DO NOTHING;
END;
CREATE TEMP TRIGGER bulk_obs_del AFTER DELETE ON main.obs BEGIN
  INSERT INTO touched_reading VALUES (OLD.firm, OLD.signal, OLD.key) ON CONFLICT DO NOTHING;
END;
"""

_TOUCH_TRIGGERS = ("bulk_firm_ins", "bulk_firm_upd", "bulk_firm_del", "bulk_evidence_ins",
                   "bulk_evidence_upd", "bulk_evidence_del", "bulk_link_ins", "bulk_link_upd",
                   "bulk_link_del", "bulk_contact_ins", "bulk_contact_upd", "bulk_contact_linked",
                   "bulk_person_ins", "bulk_person_upd", "bulk_person_del", "bulk_search_ins",
                   "bulk_search_del", "bulk_search_upd", "bulk_obs_ins", "bulk_obs_upd",
                   "bulk_obs_del")
TOUCH_TABLES = ("touched_firm", "touched_contact", "touched_link", "touched_reading",
                "touched_obs")


class Bulk:
    """What a bulk write can say beyond the rows it writes: firms whose
    group changed in a way the temporary triggers can't see (none so far),
    added with ``touch()``."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def touch(self, *firms: int) -> None:
        self.con.executemany("INSERT INTO temp.touched_firm VALUES (?) ON CONFLICT DO NOTHING",
                             [(f,) for f in firms])


class BulkCommitted(RuntimeError):
    """The body of a bulk write committed on its own: what it wrote before
    that is kept (and recounted), but it was not one transaction."""


def _close_bulk(con: sqlite3.Connection) -> None:
    """Recount what the bulk write touched and put the triggers back."""
    from harvey.overlay import recount

    for name in _TOUCH_TRIGGERS:
        con.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
    recount.touched(con)
    install(con)


@contextmanager
def bulk(con: sqlite3.Connection):
    """One transaction with the kept-value triggers off: yields a Bulk;
    at the end the touched rows are recounted from base rows (recount.py),
    the triggers come back, and the transaction commits. On an error
    everything is rolled back, the triggers included.

    The body must not commit. If it does (and its transaction is gone at
    the end, or an error follows), the part already committed is
    recounted and the triggers are put back in a transaction of their own
    before the error is raised (BulkCommitted when the body raised none),
    so the triggers are never left out."""
    if con.in_transaction:
        raise RuntimeError("bulk() starts its own transaction: commit first")
    con.execute("BEGIN IMMEDIATE")
    try:
        drop(con)
        from harvey.overlay.schema import _script

        _script(con, _TOUCH)
        for table in TOUCH_TABLES:
            con.execute(f"DELETE FROM temp.{table}")
        yield Bulk(con)
        committed = not con.in_transaction
        if committed:
            con.execute("BEGIN IMMEDIATE")
        _close_bulk(con)
        con.execute("COMMIT")
        if committed:
            raise BulkCommitted("the bulk write committed part-way: its writes are kept and "
                                "recounted, but they were not one transaction")
    except BulkCommitted:
        raise
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        if names(con) != {PREFIX + n for n in TRIGGERS}:
            # The triggers' drop was committed with part of the body.
            con.execute("BEGIN IMMEDIATE")
            try:
                _close_bulk(con)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        raise
    finally:
        for name in _TOUCH_TRIGGERS:
            try:
                con.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
            except sqlite3.Error:
                pass
