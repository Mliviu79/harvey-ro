"""The do-not-contact list: one table, one check.

An entry names what must never be emailed, keyed on what it names and never
on a contact, prospect or firm row, so it survives merges, their undo, a
contact deleted and found again, and a firm removed and pulled again:

- an address (normalised: lowercase, an international domain in its ASCII
  form); a plus-tagged variant of it is covered too;
- a domain, with its subdomains (a free-mail domain is refused: it would
  bar every address there);
- a firm: "<legal scheme>:<key>" when its records hold a legal identifier
  (a tax code outlives every merge and re-pull), else "firm:<live id>",
  matched through firm.live (a record merged into it later is covered),
  with a domain entry for its own domain beside it (unless free mail), so
  it outlives the firm's removal and a pull again too.

``check()`` is the one reader: the send gate, the audience, the Writer, the
exports and the Handler all ask it. The operator's tracked
compliance.do_not_contact list is read next to the table, never copied into
it, so removing a line from the YAML really removes it. Undoing an entry is
soft (undone_at): what was asked, and by whom, is never lost.

contact.do_not_contact mirrors the live address entries through triggers
(overlay/schema.py SUPPRESSION_GUARDS), so the Contacts page's filter and
counts agree with the list, and nothing can clear the flag while an entry
stands.

Every function here runs on a caller's sqlite3 connection, inside the
caller's transaction.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from harvey.overlay.schema import EMAIL, SUPPRESSION_REASON, SUPPRESSION_SCOPE

ADDRESS, DOMAIN, FIRM = (SUPPRESSION_SCOPE[k] for k in ("address", "domain", "firm"))
SCOPE_NAMES = {v: k for k, v in SUPPRESSION_SCOPE.items()}
REASON_NAMES = {v: k for k, v in SUPPRESSION_REASON.items()}
# Reasons that are the person's own request: undoing one asks the operator
# to confirm and say why (the dashboard and CLI), never an entry the
# operator made by hand.
PERSON_ASKED = frozenset(SUPPRESSION_REASON[k] for k in
                         ("unsubscribe", "opt_out", "objection", "complaint", "migrated"))
# Of those, the ones Harvey read in a reply's words (a keyword, the model)
# or carried over: the one-click "undo opt-out" lifts these. An unsubscribe,
# an objection or a complaint is undone on the list itself, with a note.
READ_IN_THEIR_WORDS = frozenset(SUPPRESSION_REASON[k] for k in ("opt_out", "migrated"))
NOTE_MAX = 300


class Refused(ValueError):
    """An entry that can't be made (not an address or domain, a free-mail
    domain, a firm not on file)."""


@dataclass(frozen=True)
class Hit:
    """Why an address must not be emailed: the entry it matched."""
    scope: str          # address | domain | firm
    value: str
    reason: str         # a SUPPRESSION_REASON name, or "config"
    id: int = 0         # the entry's id (0: the config list)

    @property
    def text(self) -> str:
        if self.reason == "config":
            return f"'{self.value}' is on the do-not-contact list"
        return f"'{self.value}' is on the do-not-contact list ({self.reason.replace('_', ' ')})"


# ── Normalising ──

def normalize_address(raw) -> str:
    """An address as entries and contacts keep it, or "" when it isn't one."""
    from harvey.gate import EMAIL_RE, do_not_contact_entry

    entry = do_not_contact_entry(raw)
    return entry if "@" in entry and EMAIL_RE.match(entry) else ""


def normalize_domain(raw) -> str:
    """A bare domain ("https://www.exemplu.ro/x" -> "exemplu.ro"), or "" when
    the text names none (an address names a mailbox, not a domain)."""
    from harvey.gate import do_not_contact_entry

    entry = do_not_contact_entry(raw)
    return entry if entry and "@" not in entry else ""


def free_mail(domain: str) -> bool:
    from harvey.state import FREE_MAIL_DOMAINS

    return (domain or "").strip().lower() in FREE_MAIL_DOMAINS


def _parents(host: str) -> list[str]:
    """host and every parent domain of it, on label boundaries."""
    parts = host.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


# ── Firms ──

def _record(con: sqlite3.Connection, company_id) -> int | None:
    text = str(company_id if company_id is not None else "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) <= 18:
        row = con.execute("SELECT id FROM firm WHERE id = ?", (int(text),)).fetchone()
        if row or len(text) != 12:
            return row[0] if row else None
    row = con.execute("SELECT l.firm FROM legacy_id l JOIN firm f ON f.id = l.firm "
                      "WHERE l.uuid = ?", (text,)).fetchone()
    return row[0] if row else None


def _group(con: sqlite3.Connection, record: int) -> list[int]:
    """The records of ``record``'s live firm: the live firm first."""
    live = con.execute("SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm WHERE id = ?",
                       (record,)).fetchone()
    if live is None:
        return []
    return [live[0]] + [r[0] for r in con.execute(
        "SELECT id FROM firm WHERE live = ? AND live != 0", (live[0],))]


def _legal_keys(con: sqlite3.Connection, records) -> list[str]:
    if not records:
        return []
    marks = ",".join("?" * len(records))
    return [f"{name}:{key}" for name, key in con.execute(
        f"SELECT s.name, fi.key FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme "
        f"WHERE s.legal = 1 AND fi.firm IN ({marks}) ORDER BY fi.firm = ? DESC, s.name",
        (*records, records[0]))]


def firm_values(con: sqlite3.Connection, company_ids) -> set[str]:
    """Every value a firm entry could hold for these firms: their live
    groups' record ids and legal identifiers."""
    out: set[str] = set()
    for cid in company_ids:
        record = _record(con, cid)
        group = _group(con, record) if record is not None else []
        out.update(f"firm:{r}" for r in group)
        out.update(_legal_keys(con, group))
    return out


def firm_anchor(con: sqlite3.Connection, company_id) -> str:
    """What a firm entry for this firm holds: its legal identifier, else
    its live record's id. Refused for a firm not on file."""
    record = _record(con, company_id)
    group = _group(con, record) if record is not None else []
    if not group:
        raise Refused("that firm isn't on file")
    keys = _legal_keys(con, group)
    return keys[0] if keys else f"firm:{group[0]}"


def firms_of_address(con: sqlite3.Connection, email: str) -> list[int]:
    """Every record an email contact is linked to (any link, not one chosen)."""
    return [r[0] for r in con.execute(
        "SELECT l.firm FROM link l JOIN contact c ON c.id = l.contact "
        "WHERE c.kind = ? AND c.value = ?", (EMAIL, email))]


# ── Writing ──

def _reason(reason) -> int:
    code = SUPPRESSION_REASON.get(reason, reason) if isinstance(reason, str) else reason
    if code not in REASON_NAMES:
        raise Refused(f"unknown reason {reason!r}")
    return code


def add(con: sqlite3.Connection, scope: str, value, reason, *, by: str, campaign: str = "",
        ref: str = "", note: str = "") -> int | None:
    """Add an entry; its id, or None when an entry for the same thing
    already stands. ``value``: an address, a domain, or a company id (scope
    firm). Raises Refused for something that can't be an entry."""
    if scope == "address":
        key = normalize_address(value)
        if not key:
            raise Refused(f"'{value}' is not an email address")
    elif scope == "domain":
        key = normalize_domain(value)
        if not key:
            raise Refused(f"'{value}' is not a domain")
        if free_mail(key):
            raise Refused(f"{key} is a free-mail domain: it would bar every address there "
                          "(add the address instead)")
    elif scope == "firm":
        key = firm_anchor(con, value)
    else:
        raise Refused(f"unknown scope {scope!r}")
    cur = con.execute(
        "INSERT INTO suppression (scope, value, reason, campaign, ref, note, at, by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (SUPPRESSION_SCOPE[scope], key, _reason(reason), campaign or "", ref or "",
         (note or "")[:NOTE_MAX], int(time.time()), by or ""))
    made = cur.lastrowid if cur.rowcount else None
    if scope == "firm" and key.startswith("firm:"):
        # No tax code to outlive the firm's removal and a pull again: its
        # own domain (never a free-mail one) is barred too, which does.
        row = con.execute("SELECT domain FROM firm WHERE id = ?", (int(key[5:]),)).fetchone()
        domain = normalize_domain(row[0] if row else "")
        if domain and not free_mail(domain):
            con.execute(
                "INSERT INTO suppression (scope, value, reason, campaign, ref, note, at, by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (DOMAIN, domain, _reason(reason), campaign or "", ref or "",
                 (_companion_note(key) + (note or ""))[:NOTE_MAX], int(time.time()),
                 by or ""))
    return made


def _companion_note(key: str) -> str:
    return f"the domain of {key}: "


def undo(con: sqlite3.Connection, entry_id: int, *, by: str, note: str = "") -> bool:
    """Soft-undo one entry (the trigger clears the contact's flag unless
    another entry still covers it), and a firm entry's domain beside it.
    False when it isn't in force."""
    row = con.execute("SELECT scope, value FROM suppression WHERE id = ? AND undone_at IS NULL",
                      (int(entry_id),)).fetchone()
    if row is None:
        return False
    stamp = (int(time.time()), by or "", (note or "")[:NOTE_MAX])
    con.execute("UPDATE suppression SET undone_at = ?, undone_by = ?, undo_note = ? "
                "WHERE id = ?", (*stamp, int(entry_id)))
    if row[0] == FIRM and str(row[1]).startswith("firm:"):
        con.execute("UPDATE suppression SET undone_at = ?, undone_by = ?, undo_note = ? "
                    "WHERE scope = ? AND undone_at IS NULL AND substr(note, 1, ?) = ?",
                    (*stamp, DOMAIN, len(_companion_note(row[1])), _companion_note(row[1])))
    return True


def undo_address(con: sqlite3.Connection, email: str, *, by: str, note: str = "",
                 reasons=PERSON_ASKED) -> int:
    """Soft-undo the address's entries of these reasons; how many."""
    key = normalize_address(email)
    if not key:
        return 0
    marks = ",".join("?" * len(reasons))
    return con.execute(
        f"UPDATE suppression SET undone_at = ?, undone_by = ?, undo_note = ? "
        f"WHERE scope = ? AND value = ? AND undone_at IS NULL AND reason IN ({marks})",
        (int(time.time()), by or "", (note or "")[:NOTE_MAX], ADDRESS, key,
         *sorted(reasons))).rowcount


def standing_request(con: sqlite3.Connection, email: str) -> str:
    """The reason name of an entry in force for this address that the
    person asked for and that the one-click undo doesn't lift (an
    unsubscribe, an objection, a complaint), or ""."""
    key = normalize_address(email)
    reasons = sorted(PERSON_ASKED - READ_IN_THEIR_WORDS)
    if not key or not reasons:
        return ""
    row = con.execute(
        f"SELECT reason FROM suppression WHERE scope = ? AND value = ? AND undone_at IS NULL "
        f"AND reason IN ({','.join('?' * len(reasons))}) ORDER BY id LIMIT 1",
        (ADDRESS, key, *reasons)).fetchone()
    return REASON_NAMES.get(row[0], "") if row else ""


# ── Reading ──

class Index:
    """The entries in force, read once (the list is small): many checks on
    one read, for a page or an export."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.by_scope: dict[int, dict[str, tuple[int, int]]] = {ADDRESS: {}, DOMAIN: {}, FIRM: {}}
        for entry_id, scope, value, reason in con.execute(
                "SELECT id, scope, value, reason FROM suppression WHERE undone_at IS NULL "
                "ORDER BY id"):
            self.by_scope.setdefault(scope, {}).setdefault(value, (entry_id, reason))

    def __bool__(self) -> bool:
        return any(self.by_scope.values())

    def _hit(self, scope: int, value: str) -> Hit | None:
        found = self.by_scope.get(scope, {}).get(value)
        if found is None:
            return None
        return Hit(SCOPE_NAMES[scope], value, REASON_NAMES.get(found[1], "by_hand"), found[0])

    def hit(self, email: str, firms=None) -> Hit | None:
        """The entry ``email`` matches: the address (or the address without
        its plus-tag), its domain or a parent of it, then a firm it reaches
        (``firms``, company ids, and every firm the address is linked to)."""
        address = normalize_address(email)
        if not address:
            return None
        local, _, host = address.rpartition("@")
        for value in dict.fromkeys((address, f"{local.split('+', 1)[0]}@{host}")):
            if found := self._hit(ADDRESS, value):
                return found
        for value in _parents(host):
            if found := self._hit(DOMAIN, value):
                return found
        if self.by_scope.get(FIRM):
            ids = [f for f in (firms or ()) if f not in (None, "")]
            ids += firms_of_address(self.con, address)
            for value in sorted(firm_values(self.con, ids)):
                if found := self._hit(FIRM, value):
                    return found
        return None

    def firm_hit(self, company_id) -> Hit | None:
        """The firm entry (or a domain entry for its domain) that covers a firm."""
        values = firm_values(self.con, [company_id])
        for value in sorted(values):
            if found := self._hit(FIRM, value):
                return found
        record = _record(self.con, company_id)
        if record is not None and self.by_scope.get(DOMAIN):
            row = self.con.execute("SELECT domain FROM firm WHERE id = ?", (record,)).fetchone()
            for value in _parents((row[0] or "") if row else ""):
                if found := self._hit(DOMAIN, value):
                    return found
        return None


def check(con: sqlite3.Connection, email: str, firms=None, config_entries=()) -> Hit | None:
    """Whether ``email`` must not be emailed, and why: an entry in force
    (address, domain, a firm it reaches) or a line of compliance.do_not_contact
    (``config_entries``). None: it may be."""
    from harvey.gate import do_not_contact_match

    found = Index(con).hit(email, firms)
    if found is not None:
        return found
    listed = do_not_contact_match(email or "", config_entries or ())
    return Hit("address" if "@" in listed else "domain", listed, "config") if listed else None


def entries(con: sqlite3.Connection, live_only: bool = True, limit: int = 10_000) -> list[dict]:
    """The entries, newest first (counts and kinds a page or a CSV shows)."""
    where = "WHERE undone_at IS NULL" if live_only else ""
    cols = ("id", "scope", "value", "reason", "campaign", "ref", "note", "at", "by",
            "undone_at", "undone_by", "undo_note")
    out = []
    for row in con.execute(f"SELECT {', '.join(cols)} FROM suppression {where} "
                           f"ORDER BY id DESC LIMIT ?", (int(limit),)):
        r = dict(zip(cols, row))
        r["scope"] = SCOPE_NAMES.get(r["scope"], str(r["scope"]))
        r["reason"] = REASON_NAMES.get(r["reason"], str(r["reason"]))
        out.append(r)
    return out
