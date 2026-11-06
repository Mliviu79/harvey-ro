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

from harvey import queries
from harvey.overlay import queries as _named  # noqa: F401  (registers the statements)
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
# Why waiting addresses aren't being checked (checks_held).
CHECKS_HELD_KEY = "campaign_checks_held"
# A preview decides whether an address is personal (each a read of every
# firm it is linked to) for this many of the addresses it would take.
PREVIEW_PERSONAL = 200
# The legitimate-interest note a campaign needs before it starts.
LI_NOTE_MIN = 80
# A campaign's or an audience's name, and each offer field, at most.
NAME_CHARS = 120
OFFER_CHARS = 500
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


def _x(con: sqlite3.Connection, name: str, params=()):
    """Run the named statement ``name`` (harvey/overlay/queries.py)."""
    return con.execute(queries.sql(name), params)


# The open statuses as one bound list (the statements' json_each).
_OPEN = json.dumps(OPEN_MEMBER)


# ── Sending accounts ──

def sync_account(con: sqlite3.Connection, provider: str, address: str) -> int:
    """Row 1 as today's settings say (channels.email.provider,
    persona.email): the account every campaign sends from until a second
    one exists. Returns its id."""
    _x(con, "overlay.campaigns.account_add", (_now(),))
    _x(con, "overlay.campaigns.account_sync",
       (provider or "", normalize(address), provider or "", normalize(address)))
    return 1


def accounts(con: sqlite3.Connection) -> list[dict]:
    cols = ("id", "label", "provider", "address", "profile_id", "enabled", "created_at")
    return [dict(zip(cols, r)) for r in _x(con, "overlay.campaigns.accounts")]


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
    name = (name or "").strip()[:NAME_CHARS]
    if not name:
        raise Refused("Dă-i un nume publicului. Give the audience a name.")
    query = clean_query(query)
    now = _now()
    cur = _x(con, "overlay.campaigns.audience_add",
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
    rows = [_audience(r) for r in _x(con, "overlay.campaigns.audiences", (int(bool(archived)),))]
    used = dict(_x(con, "overlay.campaigns.audience_use").fetchall())
    for r in rows:
        r["campaigns"] = used.get(r["id"], 0)
    return rows


def get_audience(con: sqlite3.Connection, audience_id) -> dict | None:
    row = _x(con, "overlay.campaigns.audience", (audience_id,)).fetchone()
    return _audience(row) if row else None


def update_audience(con: sqlite3.Connection, audience_id, *, name: str | None = None,
                    archived: bool | None = None) -> bool:
    """Rename or (un)archive. An audience is never deleted: a campaign may
    name it, and a running campaign keeps its own frozen copy anyway."""
    if name is not None and not name.strip():
        raise Refused("Dă-i un nume publicului. Give the audience a name.")
    if name is None and archived is None:
        return False
    return _x(con, "overlay.campaigns.audience_update",
              (None if name is None else name.strip()[:NAME_CHARS],
               None if archived is None else int(bool(archived)), _now(),
               audience_id)).rowcount == 1


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
    row = _x(con, "overlay.campaigns.get", (campaign_id,)).fetchone()
    return _campaign(row) if row else None


def list_campaigns(con: sqlite3.Connection, statuses=None) -> list[dict]:
    listed = json.dumps(list(statuses)) if statuses else None
    out = [_campaign(r) for r in _x(con, "overlay.campaigns.list", (listed, listed))]
    counts: dict = {}
    for campaign, status, n in _x(con, "overlay.campaigns.member_counts"):
        counts.setdefault(campaign, {})[MEMBER_NAMES.get(status, str(status))] = n
    sent = dict(_x(con, "overlay.campaigns.sent_counts").fetchall())
    replies = dict(_x(con, "overlay.campaigns.reply_counts").fetchall())
    for c in out:
        c["members"] = counts.get(c["id"], {})
        c["member_total"] = sum(c["members"].values())
        c["sent"] = sent.get(c["id"], 0)
        c["replies"] = replies.get(c["id"], 0)
    return out


# The fields _touch writes (overlay.campaigns.save), in its order.
_SAVED = ("name", "status", "sequence_json", "audience_id", "audience_query", "rolling",
          "rolled_at", "offer_json", "li_note", "review", "started_at", "ended_at",
          "updated_at")


def _touch(con, campaign_id: str, **fields) -> None:
    """Change some of a campaign's fields: the row is read and written back
    whole in the caller's transaction."""
    fields["updated_at"] = _now()
    unknown = set(fields) - set(_SAVED)
    if unknown:
        raise ValueError(f"not a campaign field: {sorted(unknown)}")
    row = _x(con, "overlay.campaigns.get", (campaign_id,)).fetchone()
    if row is None:
        return
    now = {**dict(zip(CAMPAIGN_COLS, row)), **fields}
    _x(con, "overlay.campaigns.save", (*(now[k] for k in _SAVED), campaign_id))


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
    name = (name or "").strip()[:NAME_CHARS]
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
    _x(con, "overlay.campaigns.add",
       (campaign_id, name, json.dumps(validate_sequence(sequence) if sequence else [],
                                      ensure_ascii=False),
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), audience_id,
        clean_query(query) if query and audience_id is None else "", int(bool(rolling)),
        acct["id"], acct["profile_id"], json.dumps(_offer(offer), ensure_ascii=False),
        (li_note or "").strip(), review, _now()))
    return campaign_id


def _offer(offer) -> dict:
    """The offer fields a campaign keeps (all text, cut to size)."""
    offer = offer if isinstance(offer, dict) else {}
    keys = ("offer", "goal", "booking_link", "language", "criteria")
    return {k: str(offer.get(k) or "").strip()[:OFFER_CHARS] for k in keys if offer.get(k)}


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
        out["name"] = str(fields["name"]).strip()[:NAME_CHARS]
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
        _x(con, "overlay.campaigns.note_paused", (CAMPAIGN_PAUSED, campaign_id))
    else:
        _x(con, "overlay.campaigns.note_resumed", (campaign_id, CAMPAIGN_PAUSED))


def end(con: sqlite3.Connection, campaign_id: str, reason: str) -> int:
    """Finish a campaign: what it has waiting to go out is cancelled and its
    open members stop. Returns how many emails were cancelled."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    cancelled = _x(con, "overlay.campaigns.cancel_queued",
                   (f"campaign {reason}"[:300], now, campaign_id)).rowcount
    _x(con, "overlay.campaigns.end_members",
       (M["stopped"], _now(), reason[:200], campaign_id, _OPEN))
    _touch(con, campaign_id, status="finished", ended_at=_now())
    _tell_plan(con, campaign_id, "stopped")
    return cancelled


def _tell_plan(con: sqlite3.Connection, campaign_id: str, why: str) -> None:
    """A campaign Harvey's agent made: its plan is told it finished, in
    this transaction (harvey/agent/outreach.py; nothing for the user's)."""
    from harvey.agent import outreach

    outreach.finished_sync(con, campaign_id, why)


def finish_if_done(con: sqlite3.Connection, campaign_id: str) -> bool:
    """A campaign that doesn't roll finishes once no member is open."""
    c = get_campaign(con, campaign_id)
    if c is None or c["status"] != "active" or c["rolling"]:
        return False
    if _x(con, "overlay.campaigns.has_open", (campaign_id, _OPEN)).fetchone():
        return False
    # Started with nobody (its filter matched no one): nothing will join
    # it either, so it finishes too.
    _touch(con, campaign_id, status="finished", ended_at=_now())
    _tell_plan(con, campaign_id, "done")
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
    row = _x(con, "overlay.campaigns.member", (campaign_id, normalize(email))).fetchone()
    return _member(row) if row else None


def members(con: sqlite3.Connection, campaign_id: str, status: str = "", limit: int = 500,
            offset: int = 0) -> list[dict]:
    if status:
        if status not in MEMBER_STATUS:
            raise Refused(f"unknown member status {status!r}")
        rows = _x(con, "overlay.campaigns.members_status",
                  (campaign_id, M[status], int(limit), int(offset)))
    else:
        rows = _x(con, "overlay.campaigns.members", (campaign_id, int(limit), int(offset)))
    return [_member(r) for r in rows]


def set_member(con: sqlite3.Connection, campaign_id: str, email: str, status: str,
               reason: str = "", **fields) -> bool:
    """Move a member on (``fields``: last_step, last_sent_at). A final
    status records when and why it ended."""
    unknown = set(fields) - {"last_step", "last_sent_at"}
    if unknown:
        raise ValueError(f"not a member field: {sorted(unknown)}")
    ended = int(M[status] not in OPEN_MEMBER)
    return _x(con, "overlay.campaigns.set_member",
              (M[status], fields.get("last_step"), fields.get("last_sent_at"), ended, _now(),
               ended, reason[:200], campaign_id, normalize(email))).rowcount == 1


def stop_after(con: sqlite3.Connection, campaign_id: str, email: str, step: int,
               reason: str) -> bool:
    """One of a member's emails won't go (you rejected it, or its send was
    cut off and may have reached them): what it still had queued after it
    in this campaign is cancelled, and its place ends with ``reason``, so a
    campaign that doesn't roll can finish. True when the member ended."""
    _x(con, "overlay.campaigns.cancel_after",
       (f"step {int(step)} {reason}"[:300],
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), normalize(email),
        campaign_id, int(step)))
    m = member(con, campaign_id, email)
    if m is None or m["status"] not in OPEN_MEMBER:
        return False
    set_member(con, campaign_id, email, "stopped", reason)
    finish_if_done(con, campaign_id)
    return True


def end_memberships(con: sqlite3.Connection, email: str, status: str, reason: str) -> int:
    """Every open membership of an address ends (it replied, was suppressed
    or bounced): in every campaign, whatever its state."""
    return _x(con, "overlay.campaigns.end_address",
              (M[status], _now(), reason[:200], normalize(email), _OPEN)).rowcount


def stop_colleagues(con: sqlite3.Connection, prospect_ids, reason: str) -> int:
    """Their company answered: the open memberships of these prospects
    (the colleagues state.stop_company found) stop, in every campaign."""
    ids = list(prospect_ids)
    if not ids:
        return 0
    return _x(con, "overlay.campaigns.end_prospects",
              (M["stopped"], _now(), reason[:200], json.dumps(ids), _OPEN)).rowcount


def overlap(con: sqlite3.Connection, email: str, campaign_id: str) -> str:
    """Why another campaign holds this address now ("" when none does):
    it is open in an active or paused campaign, or it got cold email in the
    last OVERLAP_DAYS."""
    email = normalize(email)
    row = _x(con, "overlay.campaigns.held_by", (email, campaign_id, _OPEN)).fetchone()
    if row:
        return f"in campaign {row[0]!r}"
    since = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=OVERLAP_DAYS)).isoformat()
    row = _x(con, "overlay.campaigns.sent_lately", (email, since, campaign_id)).fetchone()
    if row and row[0]:
        return f"cold email sent {str(row[0])[:10]}"
    # Contacted with no cold email on file (an earlier release sent it
    # through Instantly, or it was sent by hand): when is unknown, so it
    # counts as recent.
    if _x(con, "overlay.campaigns.contacted_elsewhere", (email,)).fetchone():
        return CONTACTED_ELSEWHERE
    return ""


def already_contacted(con: sqlite3.Connection, email: str, campaign_id: str,
                      outbox_id: str = "") -> str:
    """For a first email: why it would be a second one ("" when it isn't):
    this campaign already sent this address its first email (another row),
    or another campaign's sequence holds it or wrote to it lately."""
    row = _x(con, "overlay.campaigns.first_sent",
             (normalize(email), campaign_id, outbox_id)).fetchone()
    if row:
        return "already contacted"
    why = overlap(con, email, campaign_id)
    return f"already contacted ({why})" if why else ""


def review_always(con: sqlite3.Connection, campaign_id: str) -> bool:
    row = _x(con, "overlay.campaigns.review", (campaign_id or "",)).fetchone()
    return bool(row and row[0] == "always")


# ── Adding members (snapshot, roll, by hand) ──

def _empty_counts() -> dict:
    return {"total": 0, "ready": 0, "risky": 0, "to_verify": 0, "risky_held": 0,
            "left_out": 0, "untrusted": 0, "suppressed": 0, "overlap": 0, "answered": 0,
            "already": 0, "added": 0, "firms": 0, "personal_sample": 0,
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
            p = _x(con, "overlay.campaigns.prospect_status", (email,)).fetchone()
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
    firms = {row.get("company_id") for row, _, _ in take}
    counts["firms"] = len(firms - {None})
    # (Whether an address is personal reads every firm it is linked to: a
    # member records it, a preview counts it over its first PREVIEW_PERSONAL
    # addresses and says so when it took more.)
    decide = take[:PREVIEW_PERSONAL] if dry_run else take
    if dry_run and len(take) > PREVIEW_PERSONAL:
        counts["personal_sample"] = PREVIEW_PERSONAL
    kept = []
    for row, status, _ in decide:
        reason = email_rules.personal_reason(row["email"], await state.read(
            "address_facts", row["email"]))
        if reason:
            counts["personal"][{email_rules.NAMED: "named", email_rules.FREE_MAIL: "free_mail",
                                email_rules.NATURAL_PERSON: "natural_person"}[reason]] += 1
        kept.append((row, status, reason))
    sendable = counts["ready"] + counts["risky"] + counts["to_verify"]
    cap = max(int(getattr(config.channels.email, "max_daily_sends", 0) or 0), 1)
    counts["days_at_cap"] = math.ceil(sendable / cap) if sendable else 0
    if dry_run:
        return counts
    for row, status, reason in kept:
        prospect_id = await _prospect_card(state, row, status)
        added = await state._in_tx(lambda con: _x(
            con, "overlay.campaigns.member_add",
            (campaign_id, normalize(row["email"]), row["contact_id"], row.get("firm"),
             row.get("person_id") or 0, prospect_id, M[status], reason,
             MEMBER_ADDED_BY[added_by], _now())).rowcount)
        counts["added"] += added
    return counts


def _answered_firms(con: sqlite3.Connection) -> set[str]:
    """The live firms where someone answered (state.stop_company's marks),
    as the ids audience rows carry. Staging checks colleagues again, by
    email domain too (state.colleague_who_replied)."""
    from harvey.state import COMPANY_STOP_KEY

    # (The marks' keys as a range: the prefix up to the one after it.)
    end = COMPANY_STOP_KEY[:-1] + chr(ord(COMPANY_STOP_KEY[-1]) + 1)
    return {str(r[0]) for r in _x(con, "overlay.campaigns.answered_firms",
                                  (COMPANY_STOP_KEY, end, len(COMPANY_STOP_KEY) + 1))
            if r[0] is not None}


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
    await _members_told(state, campaign_id, counts, "snapshot")
    return counts


async def _members_told(state, campaign_id: str, counts: dict, how: str) -> None:
    """A campaign Harvey's agent made took members: its plan is told how
    many (harvey/agent/outreach.py; nothing for the user's)."""
    from harvey.agent import outreach

    await outreach.members_added(state, campaign_id, counts, how)


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
            _x(con, "overlay.campaigns.cancel_address",
               ("taken out of the campaign",
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), email,
                campaign_id))
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
        await _members_told(state, c["id"], counts, "rolling")
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


async def checks_held(state) -> dict | None:
    """Why waiting addresses aren't checked now, every campaign alike, or
    None: {"code": "no_price" | "day" | "month" | "other"
    (verify_costs.hold_code), "why", "services" (those refused),
    "configured" (the services a check could ask then), "at"}.
    The campaign cycle sets it when a check was refused and clears it once
    those services could be asked again."""
    try:
        held = json.loads(await state.get_setting(CHECKS_HELD_KEY) or "null")
    except (TypeError, ValueError):
        return None
    return held if isinstance(held, dict) and held.get("code") else None


async def hold_checks(state, wait: dict | None) -> None:
    if wait:
        await state.set_setting(CHECKS_HELD_KEY, json.dumps(
            {**wait, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    elif await state.get_setting(CHECKS_HELD_KEY):
        await state.delete_setting(CHECKS_HELD_KEY)


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
    acks = _x(con, "overlay.campaigns.acknowledgements", (acknowledgement.KIND,)).fetchall()
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
             for r in _x(con, "overlay.campaigns.record_sends", (campaign_id, campaign_id))]
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
    rows = _x(con, "overlay.campaigns.opt_outs", (json.dumps(codes),)).fetchall()
    if not rows:
        return []
    firm_rows = any(r[0] == scopes["firm"] for r in rows)
    covers: dict[tuple[int, str], set[str]] = {}
    firms = {r[0]: r[1] for r in _x(con, "overlay.campaigns.member_firms", (campaign_id,))}
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
