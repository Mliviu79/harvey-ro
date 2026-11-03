"""Audiences and campaigns: who the Email module writes to, and how far each got.

Contacts are the product; email is a module on top of them. A campaign is
something a person makes: an **audience** (a saved Contacts filter, kept as
the page's query string and read by contact_query.filter_from_query, so the
campaign gets exactly what the page showed) is taken when the campaign
starts, and, when it is rolling, read again at most hourly for new matches.
The campaign names a sending account and an AI profile, and carries its own
offer, sequence and legitimate-interest note.

Every way into a campaign (the start's snapshot, a roll, adding by hand) is
one function, add_members, so no address skips a rule:

    base rules     verified, catch-all (risky) or not yet checked; trust
                   confirmed or likely through a live firm; not flagged
                   do-not-contact
    suppression    suppression.check (address, domain, firm, the config list)
    overlap        an address in another active or paused campaign's
                   sequence, or sent cold email in the last OVERLAP_DAYS,
                   is skipped (counted in the preview), and so is one who
                   already answered; a rolling campaign takes it later
    personal       email_rules decides whether the address is a person's
                   (recorded; the note itself is decided when step 1 is
                   staged, so the preview shows what is sent)

``campaign_member`` is keyed on (campaign, address) and holds each
address's progress in that campaign; ``prospects`` stays the card for each
address (outbox and conversations key on it) and its status a summary.

Sending accounts: one row per mailbox, one per AI profile. Row 1 mirrors
today's settings (sync_account); caps, warmup, persona and reading replies
still read the settings until a second account exists.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from harvey.overlay.schema import MEMBER_ADDED_BY, MEMBER_STATUS
from harvey.overlay.schema import SUPPRESSION_REASON as SUPPRESSION_REASON_CODES

logger = logging.getLogger("harvey.campaigns")

M = MEMBER_STATUS
MEMBER_NAMES = {v: k for k, v in MEMBER_STATUS.items()}
# Still in the campaign's hands: may yet be written to.
OPEN_MEMBER = (M["waiting"], M["ready"], M["staged"], M["in_sequence"])
# A campaign whose members hold their addresses against every other one.
HOLDING_CAMPAIGNS = ("active", "paused")
CAMPAIGN_STATUSES = ("draft", "active", "paused", "finished", "archived")

# An address that got cold email isn't taken by another campaign for this long.
OVERLAP_DAYS = 90
# A rolling campaign reads its filter again at most this often.
ROLL_EVERY_SECONDS = 3600
# Addresses a roll added that may be checked by a verifier, per campaign and
# day (a roll can add many unchecked addresses; each check costs a credit).
ROLL_CHECKS_PER_DAY = 50
ROLL_CHECKS_KEY = "campaign_roll_checks:"
# The legitimate-interest note a campaign needs before it starts.
LI_NOTE_MIN = 80
LI_NOTE_TEMPLATE = (
    "Interes legitim: oferim {offer} firmelor care {criteria}. Adresa este publicată de "
    "firmă (sau găsită în surse publice) pentru contact comercial. Trimitem un mesaj și cel "
    "mult {followups} reveniri, fiecare cu dezabonare. Cine se opune nu mai este contactat "
    "niciodată.")

NATIVE_ONLY = ("Campaniile au nevoie de un cont Gmail, SMTP sau Microsoft (Setări → "
               "Trimitere). Campaigns need a Gmail, SMTP or Microsoft account.")

# Why a member ended when you rejected one of its emails, or its send was
# cut off mid-way or raised (it may have gone out, so nothing after it is
# sent).
REJECTED = "rejected"
CUT_OFF = "cut off mid-send"
# ...or the mailbox refused it (never retried, so nothing after it goes).
SEND_FAILED = "send failed"

# overlap's reason for an address contacted with no cold email on file.
CONTACTED_ELSEWHERE = "contacted outside the outbox"
# The prospect card statuses that mean someone answered (or was closed).
ANSWERED = ("replied", "meeting", "closed", "lost", "opted_out")


class Refused(ValueError):
    """A campaign or audience action that can't be done, and why."""

    def __init__(self, message: str, need: str = ""):
        super().__init__(message)
        self.need = need


def _now() -> int:
    return int(time.time())


def normalize(email) -> str:
    return (email or "").strip().lower()


# ── Sending accounts ──

def sync_account(con: sqlite3.Connection, provider: str, address: str) -> int:
    """Row 1 as today's settings say (channels.email.provider,
    persona.email): the account every campaign sends from until a second
    one exists. Returns its id."""
    con.execute("INSERT OR IGNORE INTO sending_account (id, label, created_at) "
                "VALUES (1, 'settings', ?)", (_now(),))
    con.execute("UPDATE sending_account SET provider = ?, address = ? WHERE id = 1 "
                "AND (provider != ? OR address != ?)",
                (provider or "", normalize(address), provider or "", normalize(address)))
    return 1


def accounts(con: sqlite3.Connection) -> list[dict]:
    cols = ("id", "label", "provider", "address", "profile_id", "enabled", "created_at")
    return [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM sending_account ORDER BY id")]


def account(con: sqlite3.Connection, account_id) -> dict | None:
    return next((a for a in accounts(con) if a["id"] == account_id), None)


# ── Audiences ──

def _audience(row) -> dict:
    keys = ("id", "name", "filter_json", "created_at", "updated_at", "archived")
    out = dict(zip(keys, row))
    try:
        out["query"] = str(json.loads(out.pop("filter_json") or "{}").get("query") or "")
    except (ValueError, TypeError, AttributeError):
        out["query"] = ""
    return out


def save_audience(con: sqlite3.Connection, name: str, query: str) -> int:
    """Save a Contacts filter (the page's query string) under a name. The
    kind is always email: a campaign writes to addresses."""
    name = (name or "").strip()[:120]
    if not name:
        raise Refused("Dă-i un nume publicului. Give the audience a name.")
    query = clean_query(query)
    now = _now()
    cur = con.execute("INSERT INTO audience (name, filter_json, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?)",
                      (name, json.dumps({"v": 1, "query": query}), now, now))
    return int(cur.lastrowid)


def clean_query(query: str) -> str:
    """A page's query string as an audience keeps it: its filters only (no
    page, sort or view), kind email."""
    from urllib.parse import urlencode

    from harvey.contact_query import query_args

    args = {k: v for k, v in query_args(query).items()
            if v not in ("", None) and k not in ("limit", "offset", "sort", "dir", "view", "kind",
                                                  "verdict", "tab")}
    return urlencode({"kind": "email", **args})


def audiences(con: sqlite3.Connection, archived: bool = False) -> list[dict]:
    rows = [_audience(r) for r in con.execute(
        "SELECT id, name, filter_json, created_at, updated_at, archived FROM audience "
        + ("" if archived else "WHERE archived = 0 ") + "ORDER BY id DESC")]
    used = dict(con.execute("SELECT audience_id, COUNT(*) FROM campaigns "
                            "WHERE audience_id IS NOT NULL GROUP BY audience_id").fetchall())
    for r in rows:
        r["campaigns"] = used.get(r["id"], 0)
    return rows


def get_audience(con: sqlite3.Connection, audience_id) -> dict | None:
    row = con.execute("SELECT id, name, filter_json, created_at, updated_at, archived "
                      "FROM audience WHERE id = ?", (audience_id,)).fetchone()
    return _audience(row) if row else None


def update_audience(con: sqlite3.Connection, audience_id, *, name: str | None = None,
                    archived: bool | None = None) -> bool:
    """Rename or (un)archive. An audience is never deleted: a campaign may
    name it, and a running campaign keeps its own frozen copy anyway."""
    sets, params = [], []
    if name is not None:
        if not name.strip():
            raise Refused("Dă-i un nume publicului. Give the audience a name.")
        sets.append("name = ?")
        params.append(name.strip()[:120])
    if archived is not None:
        sets.append("archived = ?")
        params.append(int(bool(archived)))
    if not sets:
        return False
    return con.execute(f"UPDATE audience SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                       (*params, _now(), audience_id)).rowcount == 1


# ── Campaign rows ──

CAMPAIGN_COLS = ("id", "name", "status", "sequence_json", "created_at", "audience_id",
                 "audience_query", "rolling", "rolled_at", "account_id", "profile_id",
                 "offer_json", "li_note", "review", "origin", "started_at", "ended_at",
                 "updated_at")


def _campaign(row) -> dict:
    c = dict(zip(CAMPAIGN_COLS, row))
    for key, default in (("sequence_json", []), ("offer_json", {})):
        try:
            c[key[:-5]] = json.loads(c.pop(key) or json.dumps(default))
        except (ValueError, TypeError):
            c[key[:-5]] = default
    c["rolling"] = bool(c["rolling"])
    return c


def get_campaign(con: sqlite3.Connection, campaign_id: str) -> dict | None:
    row = con.execute(f"SELECT {', '.join(CAMPAIGN_COLS)} FROM campaigns WHERE id = ?",
                      (campaign_id,)).fetchone()
    return _campaign(row) if row else None


def list_campaigns(con: sqlite3.Connection, statuses=None) -> list[dict]:
    where = ""
    params: tuple = ()
    if statuses:
        where = f"WHERE status IN ({','.join('?' * len(statuses))}) "
        params = tuple(statuses)
    out = [_campaign(r) for r in con.execute(
        f"SELECT {', '.join(CAMPAIGN_COLS)} FROM campaigns {where}ORDER BY created_at DESC",
        params)]
    counts: dict = {}
    for campaign, status, n in con.execute(
            "SELECT campaign, status, COUNT(*) FROM campaign_member GROUP BY campaign, status"):
        counts.setdefault(campaign, {})[MEMBER_NAMES.get(status, str(status))] = n
    sent = dict(con.execute("SELECT campaign_id, COUNT(*) FROM outbox WHERE status = 'sent' "
                            "AND kind = 'sequence' GROUP BY campaign_id").fetchall())
    replies = dict(con.execute("SELECT campaign_id, COUNT(*) FROM conversations "
                               "WHERE campaign_id != '' GROUP BY campaign_id").fetchall())
    for c in out:
        c["members"] = counts.get(c["id"], {})
        c["member_total"] = sum(c["members"].values())
        c["sent"] = sent.get(c["id"], 0)
        c["replies"] = replies.get(c["id"], 0)
    return out


def _touch(con, campaign_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", (*fields.values(), campaign_id))


def validate_sequence(steps) -> list[dict]:
    """A sequence as the user edited it: 1-5 steps, each with a subject and
    a body, the first sent at once and the others 1-60 days after the one
    before. Refused otherwise."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
        raise Refused("Secvența are între 1 și 5 emailuri. A sequence has 1 to 5 emails.")
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise Refused("Pas de secvență nevalid. Not a sequence step.")
        subject, body = str(s.get("subject") or "").strip(), str(s.get("body") or "").strip()
        if not subject or not body:
            raise Refused(f"Pasul {i + 1} nu are subiect sau text. Step {i + 1} has no "
                          "subject or body.")
        try:
            delay = 0 if i == 0 else int(s.get("delay_days", 3))
        except (TypeError, ValueError):
            raise Refused(f"Pasul {i + 1}: pauza e un număr de zile. Step {i + 1}: the wait "
                          "is a number of days.")
        if i and not 1 <= delay <= 60:
            raise Refused(f"Pasul {i + 1}: pauza e între 1 și 60 de zile. Step {i + 1}: wait "
                          "1 to 60 days.")
        out.append({"step": i + 1, "subject": subject[:200], "body": body[:4000],
                    "delay_days": delay})
    return out


def create_campaign(con: sqlite3.Connection, *, provider: str, address: str, name: str,
                    audience_id=None, query: str = "", rolling: bool = False,
                    offer: dict | None = None, sequence=None, li_note: str = "",
                    review: str = "inherit", account_id: int | None = None) -> str:
    """A draft campaign made by the user. Refused unless the mail provider
    is one Harvey sends through (Gmail, SMTP, Microsoft 365)."""
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS
    from harvey.state import _new_id

    if (provider or "").strip().lower() not in NATIVE_PROVIDERS:
        raise Refused(NATIVE_ONLY, need="native_provider")
    name = (name or "").strip()[:120]
    if not name:
        raise Refused("Dă-i un nume campaniei. Give the campaign a name.")
    if audience_id is not None and get_audience(con, audience_id) is None:
        raise Refused("Publicul nu există. No such audience.")
    if review not in ("inherit", "always"):
        raise Refused("review is inherit or always")
    sync_account(con, provider, address)
    acct = account(con, account_id if account_id is not None else 1)
    if acct is None or not acct["enabled"]:
        raise Refused("Contul de trimitere nu există. No such sending account.")
    campaign_id = _new_id()
    con.execute(
        "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, status, "
        "created_at, audience_id, audience_query, rolling, account_id, profile_id, offer_json, "
        "li_note, review, origin, updated_at) VALUES (?, ?, 'email', ?, '[]', 'draft', ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, 'user', ?)",
        (campaign_id, name, json.dumps(validate_sequence(sequence) if sequence else [],
                                       ensure_ascii=False),
         datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), audience_id,
         clean_query(query) if query and audience_id is None else "", int(bool(rolling)),
         acct["id"], acct["profile_id"], json.dumps(_offer(offer), ensure_ascii=False), (li_note or "").strip(),
         review, _now()))
    return campaign_id


def _offer(offer) -> dict:
    """The offer fields a campaign keeps (all text, cut to size)."""
    offer = offer if isinstance(offer, dict) else {}
    keys = ("offer", "goal", "booking_link", "language", "criteria")
    return {k: str(offer.get(k) or "").strip()[:500] for k in keys if offer.get(k)}


def update_campaign(con: sqlite3.Connection, campaign_id: str, **fields) -> dict:
    """Edit a campaign: name, offer, sequence, review and rolling at any
    time before it finishes; its audience and its legitimate-interest note
    only while it is a draft."""
    c = get_campaign(con, campaign_id)
    if c is None:
        raise Refused("Campania nu există. No such campaign.")
    if c["status"] in ("finished", "archived"):
        raise Refused("Campania s-a încheiat. The campaign has finished.")
    out = {}
    if "name" in fields and fields["name"] is not None:
        if not str(fields["name"]).strip():
            raise Refused("Dă-i un nume campaniei. Give the campaign a name.")
        out["name"] = str(fields["name"]).strip()[:120]
    if fields.get("offer") is not None:
        out["offer_json"] = json.dumps(_offer(fields["offer"]), ensure_ascii=False)
    if fields.get("sequence") is not None:
        out["sequence_json"] = json.dumps(validate_sequence(fields["sequence"]), ensure_ascii=False)
    if fields.get("li_note") is not None:
        note = str(fields["li_note"]).strip()[:4000]
        # The campaign started under its note, and the record shows that
        # one: it is written while the campaign is a draft only.
        if c["status"] != "draft" and note != (c["li_note"] or "").strip():
            raise Refused("Nota de interes legitim se scrie înainte de pornire. The "
                          "legitimate-interest note is written before the campaign starts.",
                          need="li_note")
        if c["status"] == "draft":
            out["li_note"] = note
    if fields.get("review") is not None:
        if fields["review"] not in ("inherit", "always"):
            raise Refused("review is inherit or always")
        out["review"] = fields["review"]
    if fields.get("rolling") is not None:
        out["rolling"] = int(bool(fields["rolling"]))
    for key in ("audience_id", "query"):
        if fields.get(key) is not None:
            if c["status"] != "draft":
                raise Refused("Publicul se alege înainte de pornire. The audience is chosen "
                              "before the campaign starts.")
            if key == "audience_id":
                if get_audience(con, fields[key]) is None:
                    raise Refused("Publicul nu există. No such audience.")
                out["audience_id"], out["audience_query"] = fields[key], ""
            else:
                out["audience_id"], out["audience_query"] = None, clean_query(fields[key])
    if out:
        _touch(con, campaign_id, **out)
    return get_campaign(con, campaign_id)


def set_status(con: sqlite3.Connection, campaign_id: str, action: str) -> dict:
    """pause, resume, stop or archive. Stopping cancels what the campaign
    still had waiting to go out and ends its open members; archiving hides a
    finished (or never started) one."""
    c = get_campaign(con, campaign_id)
    if c is None:
        raise Refused("Campania nu există. No such campaign.")
    status = c["status"]
    if action == "pause":
        if status != "active":
            raise Refused("Doar o campanie activă se pune pe pauză. Only an active campaign "
                          "can be paused.")
        _touch(con, campaign_id, status="paused")
        _note_paused(con, campaign_id, True)
    elif action == "resume":
        if status != "paused":
            raise Refused("Campania nu e pe pauză. The campaign isn't paused.")
        _touch(con, campaign_id, status="active")
        _note_paused(con, campaign_id, False)
    elif action == "stop":
        if status not in ("active", "paused", "draft"):
            raise Refused("Campania s-a încheiat deja. The campaign has finished already.")
        end(con, campaign_id, "stopped by you")
    elif action == "archive":
        if status in ("active", "paused"):
            raise Refused("Oprește campania înainte s-o arhivezi. Stop the campaign before "
                          "archiving it.")
        _touch(con, campaign_id, status="archived")
    else:
        raise Refused(f"unknown action {action!r}")
    return get_campaign(con, campaign_id)


def _note_paused(con: sqlite3.Connection, campaign_id: str, paused: bool) -> None:
    """The drain leaves a paused campaign's emails out of its pages (they
    would fill them), so the Outbox's note on why they wait is set here,
    and taken off on resume."""
    from harvey.state import CAMPAIGN_PAUSED

    if paused:
        con.execute("UPDATE outbox SET error = ? WHERE campaign_id = ? AND kind = 'sequence' "
                    "AND status = 'approved' AND COALESCE(error, '') = ''",
                    (CAMPAIGN_PAUSED, campaign_id))
    else:
        con.execute("UPDATE outbox SET error = '' WHERE campaign_id = ? AND kind = 'sequence' "
                    "AND status = 'approved' AND error = ?", (campaign_id, CAMPAIGN_PAUSED))


def end(con: sqlite3.Connection, campaign_id: str, reason: str) -> int:
    """Finish a campaign: what it has waiting to go out is cancelled and its
    open members stop. Returns how many emails were cancelled."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    cancelled = con.execute(
        "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? WHERE campaign_id = ? "
        "AND kind = 'sequence' AND status IN ('pending_review', 'approved')",
        (f"campaign {reason}"[:300], now, campaign_id)).rowcount
    con.execute(f"UPDATE campaign_member SET status = ?, ended_at = ?, end_reason = ? "
                f"WHERE campaign = ? AND status IN ({','.join('?' * len(OPEN_MEMBER))})",
                (M["stopped"], _now(), reason[:200], campaign_id, *OPEN_MEMBER))
    _touch(con, campaign_id, status="finished", ended_at=_now())
    return cancelled


def finish_if_done(con: sqlite3.Connection, campaign_id: str) -> bool:
    """A campaign that doesn't roll finishes once no member is open."""
    c = get_campaign(con, campaign_id)
    if c is None or c["status"] != "active" or c["rolling"]:
        return False
    if con.execute(f"SELECT 1 FROM campaign_member WHERE campaign = ? AND status IN "
                   f"({','.join('?' * len(OPEN_MEMBER))}) LIMIT 1",
                   (campaign_id, *OPEN_MEMBER)).fetchone():
        return False
    # Started with nobody (its filter matched no one): nothing will join
    # it either, so it finishes too.
    _touch(con, campaign_id, status="finished", ended_at=_now())
    return True


# ── Members ──

MEMBER_COLS = ("campaign", "email", "contact", "firm", "person", "prospect_id", "status",
               "personal", "added_by", "added_at", "last_step", "last_sent_at", "ended_at",
               "end_reason")


def _member(row) -> dict:
    m = dict(zip(MEMBER_COLS, row))
    m["status_name"] = MEMBER_NAMES.get(m["status"], str(m["status"]))
    return m


def member(con: sqlite3.Connection, campaign_id: str, email: str) -> dict | None:
    row = con.execute(f"SELECT {', '.join(MEMBER_COLS)} FROM campaign_member "
                      "WHERE campaign = ? AND email = ?", (campaign_id, normalize(email))).fetchone()
    return _member(row) if row else None


def members(con: sqlite3.Connection, campaign_id: str, status: str = "", limit: int = 500,
            offset: int = 0) -> list[dict]:
    where, params = "WHERE campaign = ?", [campaign_id]
    if status:
        if status not in MEMBER_STATUS:
            raise Refused(f"unknown member status {status!r}")
        where += " AND status = ?"
        params.append(M[status])
    return [_member(r) for r in con.execute(
        f"SELECT {', '.join(MEMBER_COLS)} FROM campaign_member {where} "
        "ORDER BY added_at, email LIMIT ? OFFSET ?", (*params, int(limit), int(offset)))]


def set_member(con: sqlite3.Connection, campaign_id: str, email: str, status: str,
               reason: str = "", **fields) -> bool:
    """Move a member on (``fields``: last_step, last_sent_at). A final
    status records when and why it ended."""
    sets = {"status": M[status], **fields}
    if M[status] not in OPEN_MEMBER:
        sets.update(ended_at=_now(), end_reason=reason[:200])
    cols = ", ".join(f"{k} = ?" for k in sets)
    return con.execute(f"UPDATE campaign_member SET {cols} WHERE campaign = ? AND email = ?",
                       (*sets.values(), campaign_id, normalize(email))).rowcount == 1


def stop_after(con: sqlite3.Connection, campaign_id: str, email: str, step: int,
               reason: str) -> bool:
    """One of a member's emails won't go (you rejected it, or its send was
    cut off and may have reached them): what it still had queued after it
    in this campaign is cancelled, and its place ends with ``reason``, so a
    campaign that doesn't roll can finish. True when the member ended."""
    con.execute("UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE campaign_id = ? AND to_email = ? AND kind = 'sequence' AND step > ? "
                "AND status IN ('pending_review', 'approved')",
                (f"step {int(step)} {reason}"[:300],
                 datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), campaign_id,
                 normalize(email), int(step)))
    m = member(con, campaign_id, email)
    if m is None or m["status"] not in OPEN_MEMBER:
        return False
    set_member(con, campaign_id, email, "stopped", reason)
    finish_if_done(con, campaign_id)
    return True


def end_memberships(con: sqlite3.Connection, email: str, status: str, reason: str) -> int:
    """Every open membership of an address ends (it replied, was suppressed
    or bounced): in every campaign, whatever its state."""
    return con.execute(
        f"UPDATE campaign_member SET status = ?, ended_at = ?, end_reason = ? WHERE email = ? "
        f"AND status IN ({','.join('?' * len(OPEN_MEMBER))})",
        (M[status], _now(), reason[:200], normalize(email), *OPEN_MEMBER)).rowcount


def stop_colleagues(con: sqlite3.Connection, prospect_ids, reason: str) -> int:
    """Their company answered: the open memberships of these prospects
    (the colleagues state.stop_company found) stop, in every campaign."""
    ids = list(prospect_ids)
    if not ids:
        return 0
    return con.execute(
        f"UPDATE campaign_member SET status = ?, ended_at = ?, end_reason = ? "
        f"WHERE prospect_id IN ({','.join('?' * len(ids))}) "
        f"AND status IN ({','.join('?' * len(OPEN_MEMBER))})",
        (M["stopped"], _now(), reason[:200], *ids, *OPEN_MEMBER)).rowcount


def overlap(con: sqlite3.Connection, email: str, campaign_id: str) -> str:
    """Why another campaign holds this address now ("" when none does):
    it is open in an active or paused campaign, or it got cold email in the
    last OVERLAP_DAYS."""
    email = normalize(email)
    row = con.execute(
        f"SELECT c.name FROM campaign_member m JOIN campaigns c ON c.id = m.campaign "
        f"WHERE m.email = ? AND m.campaign != ? AND c.status IN ('active', 'paused') "
        f"AND m.status IN ({','.join('?' * len(OPEN_MEMBER))}) LIMIT 1",
        (email, campaign_id, *OPEN_MEMBER)).fetchone()
    if row:
        return f"in campaign {row[0]!r}"
    since = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=OVERLAP_DAYS)).isoformat()
    row = con.execute("SELECT MAX(sent_at) FROM outbox WHERE to_email = ? AND kind = 'sequence' "
                      "AND status = 'sent' AND campaign_id != ? AND sent_at >= ?",
                      (email, campaign_id, since)).fetchone()
    if row and row[0]:
        return f"cold email sent {str(row[0])[:10]}"
    # Contacted with no cold email on file (an earlier release sent it
    # through Instantly, or it was sent by hand): when is unknown, so it
    # counts as recent.
    if con.execute("SELECT 1 FROM prospects p WHERE p.email = ? AND p.status = 'contacted' "
                   "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.prospect_id = p.id "
                   "AND o.kind = 'sequence' AND o.status = 'sent') LIMIT 1",
                   (email,)).fetchone():
        return CONTACTED_ELSEWHERE
    return ""


def already_contacted(con: sqlite3.Connection, email: str, campaign_id: str,
                      outbox_id: str = "") -> str:
    """For a first email: why it would be a second one ("" when it isn't):
    this campaign already sent this address its first email (another row),
    or another campaign's sequence holds it or wrote to it lately."""
    row = con.execute("SELECT 1 FROM outbox WHERE to_email = ? AND campaign_id = ? "
                      "AND kind = 'sequence' AND step = 1 AND status = 'sent' AND id != ? LIMIT 1",
                      (normalize(email), campaign_id, outbox_id)).fetchone()
    if row:
        return "already contacted"
    why = overlap(con, email, campaign_id)
    return f"already contacted ({why})" if why else ""


def review_always(con: sqlite3.Connection, campaign_id: str) -> bool:
    row = con.execute("SELECT review FROM campaigns WHERE id = ?", (campaign_id or "",)).fetchone()
    return bool(row and row[0] == "always")


# ── Adding members (snapshot, roll, by hand) ──

def _empty_counts() -> dict:
    return {"total": 0, "ready": 0, "risky": 0, "to_verify": 0, "risky_held": 0,
            "left_out": 0, "untrusted": 0, "suppressed": 0, "overlap": 0, "answered": 0,
            "already": 0, "added": 0, "firms": 0,
            "personal": {"named": 0, "free_mail": 0, "natural_person": 0}}


async def candidate_rows(state, *, query: str | None = None, emails=None) -> list[dict]:
    """The email contacts a query string (the page's filters) or a list of
    addresses names, as overlay.reads.audience_rows gives them."""
    if emails is not None:
        return await state.read("audience_rows", None, [normalize(e) for e in emails])
    from harvey.contact_query import filter_from_query

    f, _ = await filter_from_query(state, query or "")
    return await state.read("audience_rows", f)


async def add_members(state, config, campaign_id: str, rows: list[dict], added_by: str,
                      *, dry_run: bool = False) -> dict:
    """Take ``rows`` (candidate_rows) into a campaign, each through every
    rule (the module docstring), and return the counts. ``dry_run``: the
    preview, nothing written. A member gets its prospect card (made when
    the address has none) and starts waiting for a check (unchecked),
    ready (verified, or catch-all with send_to_risky on)."""
    from harvey import email_rules, suppression
    from harvey.config import live_do_not_contact

    allow_risky = bool(getattr(config.channels.email, "send_to_risky", False))
    listed = live_do_not_contact(config)
    counts = _empty_counts()
    firms: set = set()
    take: list[tuple[dict, str, int]] = []

    def screen(con):
        index = suppression.Index(con)
        answered_firms = _answered_firms(con)
        for row in rows:
            counts["total"] += 1
            email = normalize(row["email"])
            if member(con, campaign_id, email) is not None:
                counts["already"] += 1
                continue
            if row["status"] not in ("verified", "risky", "unchecked"):
                counts["left_out"] += 1
                continue
            if row["trust"] not in ("confirmed", "likely"):
                counts["untrusted"] += 1
                continue
            firm = [row["company_id"]] if row.get("company_id") else None
            if row["do_not_contact"] or (index and index.hit(email, firm)) or (
                    listed and suppression.check(con, email, firm, listed)):
                counts["suppressed"] += 1
                continue
            p = con.execute("SELECT status FROM prospects WHERE email = ?", (email,)).fetchone()
            if (p and p[0] in ANSWERED) or (row.get("company_id")
                                            and str(row["company_id"]) in answered_firms):
                counts["answered"] += 1         # they, or someone at their company
                continue
            if overlap(con, email, campaign_id):
                counts["overlap"] += 1
                continue
            if row["status"] == "risky" and not allow_risky:
                counts["risky_held"] += 1
                continue
            status = {"verified": "ready", "risky": "ready", "unchecked": "waiting"}[row["status"]]
            counts[{"verified": "ready", "risky": "risky",
                    "unchecked": "to_verify"}[row["status"]]] += 1
            take.append((row, status, 0))

    await state._in_tx(screen, write=False)
    kept = []
    for row, status, _ in take:
        reason = email_rules.personal_reason(row["email"], await state.read(
            "address_facts", row["email"]))
        if reason:
            counts["personal"][{email_rules.NAMED: "named", email_rules.FREE_MAIL: "free_mail",
                                email_rules.NATURAL_PERSON: "natural_person"}[reason]] += 1
        firms.add(row.get("company_id"))
        kept.append((row, status, reason))
    counts["firms"] = len(firms - {None})
    sendable = counts["ready"] + counts["risky"] + counts["to_verify"]
    cap = max(int(getattr(config.channels.email, "max_daily_sends", 0) or 0), 1)
    counts["days_at_cap"] = math.ceil(sendable / cap) if sendable else 0
    if dry_run:
        return counts
    for row, status, reason in kept:
        prospect_id = await _prospect_card(state, row, status)
        added = await state._in_tx(lambda con: con.execute(
            "INSERT OR IGNORE INTO campaign_member (campaign, email, contact, firm, person, "
            "prospect_id, status, personal, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)", (campaign_id, normalize(row["email"]), row["contact_id"], row.get("firm"),
                      row.get("person_id") or 0, prospect_id, M[status], reason,
                      MEMBER_ADDED_BY[added_by], _now())).rowcount)
        counts["added"] += added
    return counts


def _answered_firms(con: sqlite3.Connection) -> set[str]:
    """The live firms where someone answered (state.stop_company's marks),
    as the ids audience rows carry. Staging checks colleagues again, by
    email domain too (state.colleague_who_replied)."""
    from harvey.state import COMPANY_STOP_KEY

    return {str(r[0]) for r in con.execute(
        "SELECT DISTINCT (SELECT CASE f.live WHEN 0 THEN f.id ELSE f.live END FROM firm f "
        "WHERE f.id = p.company_id) FROM settings s JOIN prospects p "
        "ON s.key = ? || p.id WHERE s.key LIKE ? AND p.company_id != ''",
        (COMPANY_STOP_KEY, COMPANY_STOP_KEY + "%")) if r[0] is not None}


async def _prospect_card(state, row: dict, status: str) -> str:
    """The address's prospect card (made the first time it joins any
    campaign): the Writer's facts about the person and firm."""
    existing = await state.get_prospect_by_email(row["email"])
    if existing is not None:
        return existing.id
    from harvey.agents.audience import Audience
    from harvey.modules import ingest
    from harvey.state import brief_note

    prospect = Audience._prospect(row, "unchecked" if status == "waiting" else row["status"])
    if status == "waiting":
        prospect.email_status, prospect.email_verified = "", False
    try:
        prospect.personalization_notes = brief_note(await state.company_brief(row["company_id"]))
    except Exception as e:
        logger.debug(f"Campaigns: no notes for {row['email']}: {e}")
    return await ingest.prospect(state, prospect)


async def preview(state, config, *, query: str | None = None, emails=None,
                  campaign_id: str = "") -> dict:
    """What a campaign would take, counted (nothing written)."""
    rows = await candidate_rows(state, query=query, emails=emails)
    return await add_members(state, config, campaign_id or "", rows, "snapshot", dry_run=True)


def frozen_query(con: sqlite3.Connection, c: dict) -> str:
    """The filter a campaign takes its members by: its own frozen copy,
    else (a draft) its saved audience's."""
    if c.get("audience_query"):
        return c["audience_query"]
    if c.get("audience_id") is not None:
        a = get_audience(con, c["audience_id"])
        return a["query"] if a else ""
    return ""


async def start(state, config, campaign_id: str) -> dict:
    """Start (or, from a draft that was started before, restart) a campaign:
    the note, the acknowledgement, a sequence and a native provider first;
    then its audience is frozen and taken (the snapshot). Returns the
    counts of the snapshot."""
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS

    c = await state._in_tx(lambda con: get_campaign(con, campaign_id), write=False)
    if c is None:
        raise Refused("Campania nu există. No such campaign.")
    if c["status"] != "draft":
        raise Refused("Doar o ciornă pornește. Only a draft starts.")
    if (config.channels.email.provider or "").lower() not in NATIVE_PROVIDERS:
        raise Refused(NATIVE_ONLY, need="native_provider")
    if len((c["li_note"] or "").strip()) < LI_NOTE_MIN:
        raise Refused(f"Nota de interes legitim are cel puțin {LI_NOTE_MIN} de caractere. "
                      f"The legitimate-interest note needs at least {LI_NOTE_MIN} characters.",
                      need="li_note")
    if not await state.acknowledged():
        raise Refused("Confirmă întâi emailul la rece (harvey ack). Acknowledge cold email "
                      "first (harvey ack).", need="acknowledgement")
    try:
        validate_sequence(c["sequence"])
    except Refused:
        raise Refused("Campania nu are o secvență. The campaign has no sequence.",
                      need="sequence")
    query = await state._in_tx(lambda con: frozen_query(con, c), write=False)
    counts = _empty_counts()
    if query:
        rows = await candidate_rows(state, query=query)
        counts = await add_members(state, config, campaign_id, rows, "snapshot")
    await state._in_tx(lambda con: _touch(con, campaign_id, status="active",
                                          audience_query=query, started_at=_now(),
                                          rolled_at=_now()))
    return counts


async def add_by_hand(state, config, campaign_id: str, emails) -> dict:
    """Ticked rows (or typed addresses) into a campaign that hasn't
    finished, through every rule."""
    c = await state._in_tx(lambda con: get_campaign(con, campaign_id), write=False)
    if c is None:
        raise Refused("Campania nu există. No such campaign.")
    if c["status"] not in ("draft", "active", "paused"):
        raise Refused("Campania s-a încheiat. The campaign has finished.")
    rows = await candidate_rows(state, emails=emails)
    counts = await add_members(state, config, campaign_id, rows, "by_hand")
    counts["not_found"] = len({normalize(e) for e in emails} - {r["email"] for r in rows})
    return counts


async def remove_by_hand(state, campaign_id: str, emails) -> int:
    """Take addresses out ("Scoate din campanie"): what was waiting for them
    in this campaign is cancelled."""
    def run(con):
        n = 0
        for email in {normalize(e) for e in emails}:
            m = member(con, campaign_id, email)
            if m is None or m["status"] not in OPEN_MEMBER:
                continue
            set_member(con, campaign_id, email, "removed", "taken out by you")
            con.execute("UPDATE outbox SET status = 'cancelled', error = 'taken out of the "
                        "campaign', updated_at = ? WHERE campaign_id = ? AND to_email = ? "
                        "AND kind = 'sequence' AND status IN ('pending_review', 'approved')",
                        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                         campaign_id, email))
            n += 1
        return n
    return await state._in_tx(run)


async def roll(state, config, c: dict, now: int | None = None) -> dict | None:
    """A rolling campaign reads its frozen filter again (at most once per
    ROLL_EVERY_SECONDS) and takes the new matches through every rule.
    Members that stop matching stay. None when it wasn't due."""
    now = _now() if now is None else now
    if not c.get("rolling") or c["status"] != "active":
        return None
    if c.get("rolled_at") and now - int(c["rolled_at"]) < ROLL_EVERY_SECONDS:
        return None
    await state._in_tx(lambda con: _touch(con, c["id"], rolled_at=now))
    if not c.get("audience_query"):
        return None
    rows = await candidate_rows(state, query=c["audience_query"])
    counts = await add_members(state, config, c["id"], rows, "rolling")
    if counts["added"]:
        logger.info(f"Campaigns: '{c['name']}' took {counts['added']} new address(es).")
    return counts


async def roll_checks_left(state, campaign_id: str) -> int:
    """How many of a campaign's rolled addresses may still be checked today."""
    key = f"{ROLL_CHECKS_KEY}{campaign_id}:{datetime.now(timezone.utc):%Y-%m-%d}"
    try:
        used = int(await state.get_setting(key) or 0)
    except ValueError:
        used = 0
    return max(ROLL_CHECKS_PER_DAY - used, 0)


async def count_roll_check(state, campaign_id: str) -> None:
    key = f"{ROLL_CHECKS_KEY}{campaign_id}:{datetime.now(timezone.utc):%Y-%m-%d}"
    await state.increment_setting(key)


# ── The record (evidență): what a campaign did, for the operator's file ──

# The do-not-contact reasons that are the person's own objection.
OPT_OUT_REASONS = ("unsubscribe", "opt_out", "objection", "complaint")
ADDED_BY_NAMES = {v: k for k, v in MEMBER_ADDED_BY.items()}


def _iso(seconds) -> str:
    if seconds in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(seconds), timezone.utc).replace(
            tzinfo=None).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(seconds)


def record(con: sqlite3.Connection, campaign_id: str) -> dict:
    """A campaign's record: its legitimate-interest note, the cold-email
    acknowledgement it started under (version and time), every member with
    how it was added and why it ended, every email sent, and the opt-outs
    of its addresses. Read only; the operator's evidence of the balancing
    test and of honouring objections."""
    from harvey import acknowledgement, email_rules

    c = get_campaign(con, campaign_id)
    if c is None:
        raise Refused("Campania nu există. No such campaign.")
    acks = con.execute("SELECT version, text_sha, at, by FROM acknowledgement WHERE kind = ? "
                       "ORDER BY at", (acknowledgement.KIND,)).fetchall()
    started = c["started_at"]
    # The one in force when it started (else, a draft, the latest given).
    ack = next((a for a in reversed(acks) if started and a[2] <= int(started)), None) \
        or (acks[-1] if acks else None)
    members_out = []
    for m in members(con, campaign_id, limit=1_000_000):
        members_out.append({
            "email": m["email"], "status": m["status_name"],
            "added_by": ADDED_BY_NAMES.get(m["added_by"], str(m["added_by"])),
            "added_at": _iso(m["added_at"]),
            "personal": email_rules.REASON_NAMES.get(m["personal"], ""),
            "last_step": m["last_step"], "last_sent_at": _iso(m["last_sent_at"]),
            "ended_at": _iso(m["ended_at"]), "end_reason": m["end_reason"]})
    # Its cold email, and the answers sent in its conversations.
    sends = [{"email": r[0], "step": r[1], "subject": r[2], "sent_at": r[3] or "",
              "source_note": r[4] or "", "message_id": r[5] or "", "kind": r[6]}
             for r in con.execute(
                 "SELECT to_email, step, subject, sent_at, source_note, message_id, kind "
                 "FROM outbox WHERE status = 'sent' AND ((campaign_id = ? AND kind = 'sequence') "
                 "OR (kind = 'reply' AND conversation_id IN "
                 "(SELECT id FROM conversations WHERE campaign_id = ?))) "
                 "ORDER BY sent_at, step", (campaign_id, campaign_id))]
    opt_outs = _opt_outs(con, campaign_id, members_out)
    return {
        "campaign": {"id": c["id"], "name": c["name"], "status": c["status"],
                     "origin": c["origin"], "audience_query": frozen_query(con, c),
                     "rolling": c["rolling"], "created_at": str(c["created_at"] or ""),
                     "started_at": _iso(c["started_at"]), "ended_at": _iso(c["ended_at"]),
                     "steps": len(c["sequence"]), "review": c["review"]},
        "li_note": c["li_note"] or "",
        "acknowledgement": ({"version": ack[0], "text_sha": ack[1], "at": _iso(ack[2]),
                             "by": ack[3], "current": ack[0] == acknowledgement.VERSION
                             and ack[1] == acknowledgement.text_sha()} if ack else None),
        "members": members_out, "sends": sends, "opt_outs": opt_outs,
        "counts": {"members": len(members_out), "sends": len(sends),
                   "opt_outs": len(opt_outs)},
        "exported_at": _iso(_now()),
    }


def _opt_outs(con: sqlite3.Connection, campaign_id: str, members_out: list[dict]) -> list[dict]:
    """The opt-outs that reach this campaign's members: an entry for their
    address, their domain (or a parent of it) or their firm, and any made
    in this campaign. ``email``: the member addresses it covers."""
    from harvey import suppression

    scopes = suppression.SUPPRESSION_SCOPE
    codes = [SUPPRESSION_REASON_CODES[r] for r in OPT_OUT_REASONS]
    rows = con.execute(
        f"SELECT scope, value, reason, campaign, at, by, undone_at, undo_note FROM suppression "
        f"WHERE reason IN ({','.join('?' * len(codes))}) ORDER BY at, id", codes).fetchall()
    if not rows:
        return []
    firm_rows = any(r[0] == scopes["firm"] for r in rows)
    covers: dict[tuple[int, str], set[str]] = {}
    firms = {r[0]: r[1] for r in con.execute(
        "SELECT email, firm FROM campaign_member WHERE campaign = ?", (campaign_id,))}
    for m in members_out:
        email = m["email"]
        local, _, host = email.rpartition("@")
        keys = {(scopes["address"], email), (scopes["address"], f"{local.split('+', 1)[0]}@{host}")}
        keys.update((scopes["domain"], d) for d in suppression._parents(host))
        if firm_rows:
            ids = [f for f in (firms.get(email),) if f not in (None, "")]
            ids += suppression.firms_of_address(con, email)
            keys.update((scopes["firm"], v) for v in suppression.firm_values(con, ids))
        for key in keys:
            covers.setdefault(key, set()).add(email)
    out = []
    for scope, value, reason, campaign, at, by, undone_at, undo_note in rows:
        reached = covers.get((scope, value), set())
        if not reached and campaign != campaign_id:
            continue
        out.append({"email": " ".join(sorted(reached)) or value,
                    "reason": suppression.REASON_NAMES.get(reason, ""),
                    "here": campaign == campaign_id, "at": _iso(at), "by": by,
                    "undone_at": _iso(undone_at), "undo_note": undo_note or "",
                    "scope": suppression.SCOPE_NAMES.get(scope, str(scope)), "value": value})
    return out


def record_csv(rec: dict) -> str:
    """The record as one CSV: a section per part (campaign, note,
    acknowledgement, members, sends, opt-outs), each with its own header."""
    import csv
    import io

    from harvey.export import csv_cell

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "field", "value"])
    for k, v in rec["campaign"].items():
        w.writerow(["campaign", k, csv_cell(v)])
    w.writerow(["campaign", "li_note", csv_cell(rec["li_note"])])
    for k, v in (rec["acknowledgement"] or {"version": "", "at": ""}).items():
        w.writerow(["acknowledgement", k, csv_cell(v)])
    w.writerow(["campaign", "exported_at", rec["exported_at"]])
    for section, rows in (("members", rec["members"]), ("sends", rec["sends"]),
                          ("opt_outs", rec["opt_outs"])):
        w.writerow([])
        if not rows:
            w.writerow([section, "(none)"])
            continue
        w.writerow([section, *rows[0].keys()])
        for r in rows:
            w.writerow([section, *[csv_cell(v) for v in r.values()]])
    return out.getvalue()


def li_note_template(offer: str = "", criteria: str = "", followups: int = 2) -> str:
    return LI_NOTE_TEMPLATE.format(offer=offer or "{ofertă}", criteria=criteria or
                                   "{criteriu public}", followups=followups)
