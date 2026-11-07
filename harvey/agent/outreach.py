"""Campaigns under a plan: what Harvey's agent may do with the Email
module, and what the Email module tells it back (agent_campaign, schema
35).

The agent sits on top of the Email module and adds no way to send. It
saves an audience and makes a draft campaign through the Campaigns
page's own code (harvey/launch.py, harvey/campaigns.py), the draft
belonging to the plan it was made under; it edits only its own drafts,
counts what a campaign would take, and asks the user to start one
(``request_start``: a card in the chat, the start itself staying the
user's own Start with its acknowledgement, note and confirmations). It
may pause one of its plan's campaigns (the safe direction), never resume
or start one. Everything that sends stays where it was: the campaign
cycle drafts first emails (the Writer, tool-less) and stages them for
review, the Handler reads replies and queues its answers for review,
autopilot approves at the drain under its own rules, and the gate checks
every email.

What it does with replies: ``replies`` counts a campaign's conversations
by the intent, stage and status the Handler derived, with their ids;
``draft_reply`` has the Handler write an answer with the agent's short
guidance (bounded and masked) added to its prompt, queued as the Handler
queues its own but held for the user even on autopilot (AGENT_DRAFT_HELD:
the user's autopilot consent covers Harvey's own answers, not one the
agent asked for); ``suppress`` adds to the do-not-contact list
through its own writer (never an undo). The agent never sees a reply's
or a page's words: only the Handler and the Writer, which hold no tools,
read them.

What it is told: an event (wakes.note, structured fields only) for the
open approved plan a campaign belongs to: ``members`` (a snapshot or a
roll took addresses: counts), ``reply`` (a conversation of the campaign
changed: its id, the campaign, the intent, stage and status as enums,
whether they asked to stop, whether it needs a human, whether an answer
waits; never the text, the subject, the address or any summary of it)
and ``campaign`` (it finished, sending was paused by a rail, its checks
are held, with a code). Telling the agent never fails what the Email
module was doing.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries

logger = logging.getLogger("harvey.agent")

MEMBERS, REPLY, CAMPAIGN = "members", "reply", "campaign"
# What a reply event may say, as the Handler derives it (anything else is
# "other": an event carries enums, never text).
INTENTS = ("interested", "objection", "not_interested", "ooo", "wrong_person", "question",
           "unsubscribe", "escalate")
STAGES = ("initial_outreach", "engaged", "qualifying", "presenting", "negotiating",
          "closing", "closed_won", "closed_lost")
CONVERSATION_STATUSES = ("open", "closed", "needs_human", "replied", "meeting_booked")
# The intents Harvey answers (handler.AUTO_REPLY_INTENTS): an answer to any
# other (an opt-out, an escalation, a no, an out-of-office) is never drafted.
ANSWERABLE = ("interested", "objection", "question", "wrong_person")
GUIDANCE_CHARS = 500
CONVERSATIONS_LIMIT = 50
REASON_CHARS = 200


class Refused(ValueError):
    """What the agent asked of a campaign can't be done: the message says
    why."""


def _now() -> int:
    return int(time.time())


def _enum(value, allowed) -> str:
    value = str(value or "")
    return value if value in allowed else ("" if not value else "other")


async def _one(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchone()


async def _all(state, name: str, args=()):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            return await cur.fetchall()


# ── The link: a campaign the agent made belongs to its plan ──

_LINK = ("campaign", "plan", "chat", "created_at", "start_requested_at", "start_request")


def _link(row) -> dict | None:
    if row is None:
        return None
    out = dict(zip(_LINK, row))
    try:
        out["start_request"] = json.loads(out["start_request"] or "null")
    except ValueError:
        out["start_request"] = None
    return out


async def link_of(state, campaign_id) -> dict | None:
    return _link(await _one(state, "overlay.agent.campaign_link", (str(campaign_id or ""),)))


async def of_plan(state, plan_id) -> list[dict]:
    return [_link(r) for r in await _all(state, "overlay.agent.plan_campaigns", (int(plan_id),))]


def open_plan(con, campaign_id) -> int | None:
    """The open approved plan a campaign belongs to, on a caller's sqlite3
    connection (inside its transaction), or None."""
    row = con.execute(queries.sql("overlay.agent.campaign_open_plan"),
                      (str(campaign_id or ""), _now())).fetchone()
    return int(row[0]) if row else None


async def _own(state, plan, chat: str, campaign_id, *, draft: bool = False) -> dict:
    """The campaign when it belongs to ``plan`` (an approved plan of
    ``chat``): its row, else Refused."""
    from harvey import campaigns

    link = await link_of(state, campaign_id)
    if link is None or link["chat"] != chat or str(link["plan"]) != str(plan):
        raise Refused(f"campaign {campaign_id!r} isn't one made under plan {plan}: the agent "
                      "changes only its own")
    c = await state.campaigns_call(campaigns.get_campaign, str(campaign_id), write=False)
    if c is None:
        raise Refused(f"no campaign {campaign_id!r}")
    if draft and c["status"] != "draft":
        raise Refused(f"campaign {c['id']} is {c['status']}: only a draft is edited here (the "
                      "user changes a started one on the Campaigns page)")
    return c


# ── Events: what the Email module tells the plan ──

def note_sync(con, campaign_id, kind: str, ref: str, data: dict) -> int | None:
    """An event for the plan a campaign belongs to, written on a caller's
    connection inside its transaction (a campaign finishing): the plan's
    id when one was written. Its wake is queued by the worker's sweep
    within a minute. Never raises."""
    try:
        plan = open_plan(con, campaign_id)
        if plan is None:
            return None
        now = _now()
        cur = con.execute(queries.sql("overlay.agent.event_add"),
                          (plan, kind, str(ref), json.dumps(data, ensure_ascii=False), now, now))
        return plan if cur.rowcount else None
    except Exception as e:
        logger.warning("the agent's plan not told about campaign %s: %s", campaign_id, e)
        return None


async def note(state, campaign_id, kind: str, ref: str, data: dict) -> int | None:
    """An event for the plan a campaign belongs to, its wake queued: the
    event's id, or None (no open plan, told already). Never raises."""
    from harvey.agent import wakes

    try:
        row = await _one(state, "overlay.agent.campaign_open_plan",
                         (str(campaign_id or ""), _now()))
        if row is None:
            return None
        return await wakes.note(state, row[0], kind, ref, data)
    except Exception as e:
        logger.warning("the agent's plan not told about campaign %s: %s", campaign_id, e)
        return None


async def members_added(state, campaign_id: str, counts: dict, how: str) -> int | None:
    """A snapshot or a roll took addresses into the campaign: counts."""
    if not (counts or {}).get("added"):
        return None
    data = {"campaign": campaign_id, "how": how,
            **{k: int(counts.get(k) or 0) for k in ("added", "ready", "risky", "to_verify",
                                                     "suppressed", "overlap", "answered")}}
    return await note(state, campaign_id, MEMBERS, f"{how}@{time.time_ns()}", data)


async def reply(state, convo_id: str, *, asked_to_stop: bool = False) -> int | None:
    """A conversation of a campaign changed (a reply read, an opt-out, an
    unsubscribe): its structured fields, as the Handler derived them,
    never its text."""
    try:
        row = await _one(state, "overlay.agent.conversation", (str(convo_id or ""),))
    except Exception as e:
        logger.warning("conversation %s not read for the agent: %s", convo_id, e)
        return None
    if row is None or not row[2]:
        return None
    cid, prospect_id, campaign_id, intent, stage, status, messages, waiting, _last, _ch = row
    opted_out = False
    try:
        prospect = await state.get_prospect(prospect_id) if prospect_id else None
        opted_out = bool(prospect and prospect.status == "opted_out")
    except Exception:
        pass
    data = {"conversation": cid, "campaign": campaign_id,
            "intent": _enum(intent, INTENTS), "stage": _enum(stage, STAGES),
            "status": _enum(status, CONVERSATION_STATUSES),
            "asked_to_stop": bool(asked_to_stop or opted_out or intent == "unsubscribe"),
            "needs_human": status == "needs_human", "answer_waiting": bool(waiting)}
    return await note(state, campaign_id, REPLY, f"{cid}:{int(messages or 0)}:{status}", data)


def finished_sync(con, campaign_id: str, why: str) -> None:
    """The campaign finished (stopped, or no member open): told to its plan
    in the same transaction."""
    note_sync(con, campaign_id, CAMPAIGN, "finished",
              {"campaign": campaign_id, "what": "finished", "why": why})


async def rail(state, what: str, **fields) -> int:
    """A rail of the Email module was reached for every campaign (sending
    paused by the bounce rate, the address checks held): each active
    campaign of an open plan is told, once a day per reason. How many."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    code = "".join(str(v) for v in fields.values())[:40]
    n = 0
    try:
        rows = await _all(state, "overlay.agent.campaigns_open_active", (_now(),))
    except Exception as e:
        logger.warning("the agent's plans not told about %s: %s", what, e)
        return 0
    for campaign_id, _plan in rows:
        data = {"campaign": campaign_id, "what": what,
                **{k: str(v)[:40] for k, v in fields.items()}}
        if await note(state, campaign_id, CAMPAIGN, f"{what}:{code}@{day}", data):
            n += 1
    return n


# ── Counts ──

async def status(state, campaign_id) -> dict:
    """A campaign in counts: its members by status, its cold email by
    status (sent among them), its conversations by intent, stage and
    status, the answers to them by status, the opt-outs and bounces among
    its members, and why its waiting addresses aren't checked (a code)."""
    from harvey import campaigns

    c = await state.campaigns_call(campaigns.get_campaign, str(campaign_id or ""), write=False)
    if c is None:
        raise Refused(f"no campaign {campaign_id!r}: campaigns() lists them")
    members = {campaigns.MEMBER_NAMES.get(s, str(s)): n
               for s, n in await _all(state, "overlay.agent.campaign_members", (c["id"],))}
    cold = {s: n for s, n in await _all(state, "overlay.agent.campaign_sequence", (c["id"],))}
    by_intent: dict = {}
    by_stage: dict = {}
    by_status: dict = {}
    for intent, stage, st, n in await _all(state, "overlay.agent.campaign_replies", (c["id"],)):
        for d, k in ((by_intent, _enum(intent, INTENTS) or "none"),
                     (by_stage, _enum(stage, STAGES) or "none"),
                     (by_status, _enum(st, CONVERSATION_STATUSES) or "none")):
            d[k] = d.get(k, 0) + n
    answers = {s: n for s, n in await _all(state, "overlay.agent.campaign_answers", (c["id"],))}
    held = None
    if members.get("waiting"):
        h = await campaigns.checks_held(state)
        held = {"code": h["code"]} if h else None
    link = await link_of(state, c["id"])
    return {"campaign": c["id"], "name": c["name"], "status": c["status"],
            "rolling": c["rolling"], "origin": c["origin"], "steps": len(c["sequence"]),
            "plan": link["plan"] if link else None,
            "start_requested": bool(link and link["start_requested_at"]),
            "members": members, "member_total": sum(members.values()),
            "sent": cold.get("sent", 0), "cold_email": cold,
            "replies": {"total": sum(by_status.values()), "by_intent": by_intent,
                        "by_stage": by_stage, "by_status": by_status,
                        "needs_human": by_status.get("needs_human", 0)},
            "answers": answers, "opted_out": members.get("suppressed", 0),
            "bounced": members.get("bounced", 0), "checks_held": held}


async def conversations(state, campaign_id: str, limit: int = CONVERSATIONS_LIMIT) -> list[dict]:
    """A campaign's newest conversations: ids and their enums, never text."""
    return [{"conversation": cid, "campaign": campaign_id, "intent": _enum(i, INTENTS),
             "stage": _enum(st, STAGES), "status": _enum(s, CONVERSATION_STATUSES),
             "needs_human": s == "needs_human", "answer_waiting": bool(w),
             "answerable": i in ANSWERABLE and s in ("open", "needs_human") and not w
             and last == "prospect" and not channel}
            for cid, i, st, s, _at, w, last, channel in await _all(
                state, "overlay.agent.campaign_conversations", (campaign_id, int(limit)))]


# ── What the agent does: through the Campaigns page's own code ──

def _counts(counts: dict) -> dict:
    """A preview's counts (campaigns.add_members): numbers only."""
    out = {}
    for k, v in (counts or {}).items():
        if isinstance(v, dict):
            out[k] = {a: int(b or 0) for a, b in v.items() if isinstance(b, (int, float))}
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def checked_sequence(steps) -> list[dict]:
    """A sequence as the wizard keeps it (campaigns.validate_sequence), each
    step also checked as the gate will check it when it is sent
    (gate.template_problems): Refused saying which step and why."""
    from harvey import campaigns, gate

    try:
        out = campaigns.validate_sequence(steps)
    except campaigns.Refused as e:
        raise Refused(str(e)) from None
    for s in out:
        problems = gate.template_problems(s["subject"], s["body"])
        if problems:
            raise Refused(f"step {s['step']}: " + "; ".join(problems))
    return out


def _note(text) -> str:
    from harvey import campaigns

    note = str(text or "").strip()
    if len(note) < campaigns.LI_NOTE_MIN:
        raise Refused(f"the legitimate-interest note is at least {campaigns.LI_NOTE_MIN} "
                      "characters: why these firms, where their addresses come from, how "
                      "many follow-ups, that an objection is honoured for good")
    return note


def _offer(offer) -> dict:
    if isinstance(offer, str):
        return {"offer": offer}
    return offer if isinstance(offer, dict) else {}


async def _audience(state, audience) -> dict:
    from harvey import campaigns

    try:
        aid = int(audience)
    except (TypeError, ValueError):
        raise Refused("audience is an audience's id (save_audience gives one)") from None
    a = await state.campaigns_call(campaigns.get_audience, aid, write=False)
    if a is None or a["archived"]:
        raise Refused(f"no audience {audience}")
    return a


async def save_audience(state, config, name, query: str) -> dict:
    """An audience saved the way "Salvează ca public" saves it, and what a
    campaign would take from it, counted: its id and the counts, never an
    address."""
    from harvey import campaigns, launch

    try:
        # (The name as the page sends it: campaigns.save_audience strips it
        # and cuts it to campaigns.NAME_CHARS, for both.)
        aid = await launch.save_audience(state, str(name or ""), query)
    except (campaigns.Refused, ValueError) as e:
        raise Refused(str(e)) from None
    a = await state.campaigns_call(campaigns.get_audience, aid, write=False)
    counts = await campaigns.preview(state, config, query=a["query"])
    return {"audience": aid, "name": a["name"], "preview": _counts(counts)}


async def create(state, config, plan: int, chat: str, *, name, audience, offer, sequence,
                 note, rolling: bool = False, always_review: bool = False,
                 send_to_risky: bool | None = None) -> dict:
    """A draft campaign, made the way the wizard's Save makes one, that
    belongs to ``plan``. Nothing is started or staged. ``send_to_risky``:
    the setting's unless given (never past it: campaigns.risky_allowed)."""
    from harvey import campaigns, launch

    a = await _audience(state, audience)
    body = {"name": str(name or ""), "audience_id": a["id"],
            "rolling": bool(rolling), "offer": _offer(offer),
            "sequence": checked_sequence(sequence), "li_note": _note(note),
            "review": "always" if always_review else "inherit"}
    if send_to_risky is not None:
        body["send_to_risky"] = bool(send_to_risky)
    try:
        campaign_id = await launch.new_campaign(state, config, body)
    except (campaigns.Refused, ValueError) as e:
        raise Refused(str(e)) from None
    async with state._connect() as db:
        await db.execute(queries.sql("overlay.agent.campaign_link_add"),
                         (campaign_id, int(plan), chat, _now()))
        await db.commit()
    return {"campaign": campaign_id, "status": "draft", "audience": a["id"],
            "steps": len(body["sequence"]), "rolling": body["rolling"]}


async def edit(state, plan: int, chat: str, campaign_id, *, name=None, audience=None,
               offer=None, sequence=None, note=None, rolling=None,
               always_review=None, send_to_risky=None) -> dict:
    """One of this plan's drafts edited the way the wizard's Save edits it."""
    from harvey import campaigns

    c = await _own(state, plan, chat, campaign_id, draft=True)
    fields: dict = {}
    if name is not None:
        fields["name"] = str(name)
    if audience is not None:
        fields["audience_id"] = (await _audience(state, audience))["id"]
    if offer is not None:
        fields["offer"] = _offer(offer)
    if sequence is not None:
        fields["sequence"] = checked_sequence(sequence)
    if note is not None:
        fields["li_note"] = _note(note)
    if rolling is not None:
        fields["rolling"] = bool(rolling)
    if always_review is not None:
        fields["review"] = "always" if always_review else "inherit"
    if send_to_risky is not None:
        fields["send_to_risky"] = bool(send_to_risky)
    if not fields:
        raise Refused("say what to change")
    try:
        done = await state.campaigns_call(_edit_draft, c["id"], fields)
    except campaigns.Refused as e:
        raise Refused(str(e)) from None
    return {"campaign": done["id"], "status": done["status"], "changed": sorted(fields)}


def _edit_draft(con, campaign_id: str, fields: dict) -> dict:
    """campaigns.update_campaign on a campaign that is a draft when the
    write is made, in the same transaction (one the user started meanwhile
    is theirs: its review, rolling, offer and sequence stay as they are).
    Always-review only goes on here: once a draft asks for it (the agent,
    or the user ticking it on the wizard), only the user takes it off."""
    from harvey import campaigns

    c = campaigns.get_campaign(con, campaign_id)
    if c is None or c["status"] != "draft":
        raise campaigns.Refused(f"campaign {campaign_id} is "
                                f"{c['status'] if c else 'gone'}: only a draft is edited here")
    if fields.get("review") == "inherit" and c["review"] == "always":
        raise campaigns.Refused(f"campaign {campaign_id} asks to review every email: only the "
                                "user turns that off, on the Campaigns page")
    return campaigns.update_campaign(con, campaign_id, **fields)


async def preview(state, config, campaign_id) -> dict:
    """What a campaign would take from its audience now, counted."""
    from harvey import campaigns

    c = await state.campaigns_call(campaigns.get_campaign, str(campaign_id or ""), write=False)
    if c is None:
        raise Refused(f"no campaign {campaign_id!r}: campaigns() lists them")
    query = await state.campaigns_call(campaigns.frozen_query, c, write=False)
    if not query:
        return {"campaign": c["id"], "status": c["status"], "preview": None,
                "note": "it names no audience"}
    counts = await campaigns.preview(state, config, query=query, campaign_id=c["id"])
    return {"campaign": c["id"], "status": c["status"], "preview": _counts(counts),
            "send_to_risky": bool(counts.get("send_to_risky"))}


async def to_do(state, config, c: dict) -> list[str]:
    """What the user's Start still needs (campaigns.start refuses without
    it): a native provider, the note, the acknowledgement, a sequence, an
    audience."""
    from harvey import campaigns
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS

    out = []
    if (config.channels.email.provider or "").lower() not in NATIVE_PROVIDERS:
        out.append("native_provider")
    if len((c["li_note"] or "").strip()) < campaigns.LI_NOTE_MIN:
        out.append("li_note")
    if not await state.acknowledged():
        out.append("acknowledgement")
    try:
        campaigns.validate_sequence(c["sequence"])
    except campaigns.Refused:
        out.append("sequence")
    if not await state.campaigns_call(campaigns.frozen_query, c, write=False):
        out.append("audience")
    return out


async def request_start(state, config, plan: int, chat: str, campaign_id) -> dict:
    """Ask the user to start one of this plan's drafts: the chat shows it
    as a card (its audience counted now, its sequence, its note, the
    sending account) whose button opens it on the Campaigns page. Nothing
    starts here."""
    c = await _own(state, plan, chat, campaign_id, draft=True)
    counted = await preview(state, config, c["id"])
    need = await to_do(state, config, c)
    request = {"preview": counted["preview"], "to_do": need}
    async with state._connect() as db:
        await db.execute(queries.sql("overlay.agent.campaign_start_request"),
                         (_now(), json.dumps(request), c["id"]))
        await db.commit()
    return {"campaign": c["id"], "requested": True, "to_do": need,
            "preview": counted["preview"],
            "note": "the user starts it on the Campaigns page (Start: the acknowledgement, the "
                    "note and their confirmation); nothing is sent before that"}


async def pause(state, plan: int, chat: str, campaign_id) -> dict:
    """Pause one of this plan's active campaigns, as its Pause button does:
    what it staged waits, nothing more is staged. Only the user resumes."""
    from harvey import campaigns

    c = await _own(state, plan, chat, campaign_id)
    try:
        done = await state.campaigns_call(campaigns.set_status, c["id"], "pause")
    except campaigns.Refused as e:
        raise Refused(str(e)) from None
    return {"campaign": done["id"], "status": done["status"],
            "note": "paused: only the user resumes it, on the Campaigns page"}


async def replies(state, campaign_ids: list[str], limit: int = CONVERSATIONS_LIMIT) -> dict:
    """These campaigns' conversations counted by intent, stage and status,
    and the newest ones' ids with their enums (no text)."""
    by_intent: dict = {}
    by_stage: dict = {}
    by_status: dict = {}
    listed: list = []
    for cid in campaign_ids:
        for intent, stage, st, n in await _all(state, "overlay.agent.campaign_replies", (cid,)):
            for d, k in ((by_intent, _enum(intent, INTENTS) or "none"),
                         (by_stage, _enum(stage, STAGES) or "none"),
                         (by_status, _enum(st, CONVERSATION_STATUSES) or "none")):
                d[k] = d.get(k, 0) + n
        listed += await conversations(state, cid, limit)
    listed = listed[:max(1, min(int(limit or CONVERSATIONS_LIMIT), CONVERSATIONS_LIMIT))]
    return {"total": sum(by_status.values()), "by_intent": by_intent, "by_stage": by_stage,
            "by_status": by_status, "needs_human": by_status.get("needs_human", 0),
            "answer_waiting": sum(1 for c in listed if c["answer_waiting"]),
            "conversations": listed}


def guidance_text(text) -> str:
    """The agent's guidance as the Handler's prompt gets it: one paragraph,
    its emails and phone numbers masked, GUIDANCE_CHARS at most."""
    from harvey.agent import memory

    return " ".join(memory.masked(str(text or "")).split())[:GUIDANCE_CHARS]


def reply_handler(state, config):
    """The Handler that writes an answer the agent asks for (tests replace
    it): the install's brain, settings and mailbox."""
    from harvey.agents.handler import Handler
    from harvey.brain import Brain
    from harvey.config import load_env

    return Handler(Brain(state, config.brain), state, config, load_env())


async def _conversation(state, plan: int, chat: str, convo_id) -> dict:
    row = await _one(state, "overlay.agent.conversation", (str(convo_id or ""),))
    if row is None:
        raise Refused(f"no conversation {convo_id!r}: replies() lists them")
    out = dict(zip(("id", "prospect_id", "campaign_id", "intent", "stage", "status",
                    "messages", "waiting", "last", "channel"), row))
    if not out["campaign_id"]:
        raise Refused(f"conversation {out['id']} belongs to no campaign")
    await _own(state, plan, chat, out["campaign_id"])
    return out


async def draft_reply(state, config, plan: int, chat: str, convo_id, guidance) -> dict:
    """An answer to a conversation of this plan's campaign, written by the
    Handler with the agent's guidance and queued for review as the Handler
    queues its own, held for the user even on autopilot. Refused when the conversation isn't one to
    answer (an opt-out, an escalation, a no, closed), an answer to it
    waits already, its person or firm is on the do-not-contact list, or
    Harvey's share of the Claude plan is used."""
    c = await _conversation(state, plan, chat, convo_id)
    if c["waiting"]:
        raise Refused(f"an answer to conversation {c['id']} waits already: the user reviews "
                      "it in the Outbox")
    if c["intent"] not in ANSWERABLE:
        raise Refused(f"conversation {c['id']} is {c['intent'] or 'unclassified'}: Harvey "
                      "doesn't answer that (the user does, on the Conversations page)")
    if c["status"] not in ("open", "needs_human"):
        raise Refused(f"conversation {c['id']} is {c['status']}")
    if c["last"] != "prospect":
        raise Refused(f"conversation {c['id']}'s newest reply has its answer already: "
                      "nothing new of theirs to answer")
    if c["channel"]:
        # (An SMS or WhatsApp message: Harvey doesn't answer on those
        # channels yet, and an email answer to it isn't what they asked for.)
        from harvey.integrations import npathi

        raise Refused(f"conversation {c['id']}'s newest message came by "
                      f"{npathi.LABELS.get(c['channel'], 'another channel')}: the user "
                      "answers it, on the Conversations page")
    prospect = await state.get_prospect(c["prospect_id"])
    if prospect is None or prospect.status == "opted_out":
        raise Refused(f"conversation {c['id']}'s person asked not to be contacted")
    hit = await state.suppression_check(prospect.email, firm=prospect.company_id, config=config)
    if hit is not None:
        raise Refused(f"conversation {c['id']}'s person or firm is on the do-not-contact list")
    text = guidance_text(guidance)
    if not text:
        raise Refused("give short guidance for the answer: what it should say or ask")
    handler = reply_handler(state, config)
    percent = getattr(getattr(config, "usage", None), "max_daily_claude_percent", None)
    if not await handler.brain.is_within_budget(max_percent=percent):
        raise Refused(f"{handler.brain.budget_why}: try later" if handler.brain.budget_why else
                      "Harvey's share of the Claude plan is used for now "
                      "(usage.max_daily_claude_percent): try later")
    # (It waits for the user even on autopilot, approved by them one at a
    # time: autopilot is the user's consent to Harvey's own answers, not to
    # one the agent asked for. AGENT_DRAFT_HELD: no autopilot promotion, no
    # Approve all.)
    item = await handler.draft_answer(c["id"], text, by="agent", hold=True)
    if not item:
        raise Refused("the model gave no usable answer, or one waits already: the user can "
                      "answer on the Conversations page")
    return {"conversation": c["id"], "outbox": item, "status": "pending_review",
            "waits_for_user": True,
            "note": "queued in the Outbox for the user: an answer you asked for waits for "
                    "their approval even on autopilot; it is not sent yet"}


async def suppress(state, config, plan: int, chat: str, *, conversation=None, contact=None,
                   firm=None, reason: str = "") -> dict:
    """Add to the do-not-contact list (never an undo): a conversation's
    person (their status opted out, the entry, their campaigns ended, what
    was queued for them cancelled, as the Handler records an opt-out), a
    contact's email address, or a firm. Every entry is ``by_hand`` by
    ``agent``: the one-click "Undo opt-out" (which lifts what Harvey read
    in their own words) refuses while one stands; the user undoes it on
    the do-not-contact list."""
    given = [x for x in (conversation, contact, firm) if x not in (None, "")]
    if len(given) != 1:
        raise Refused("name one: a conversation, a contact or a firm")
    note = " ".join(str(reason or "").split())[:REASON_CHARS]
    from harvey import suppression

    if conversation not in (None, ""):
        c = await _conversation(state, plan, chat, conversation)
        prospect = await state.get_prospect(c["prospect_id"])
        if prospect is None:
            raise Refused(f"conversation {c['id']} has no person on file")
        if prospect.status == "opted_out":
            return {"conversation": c["id"], "added": False, "note": "on the list already"}
        # (by_hand, by the agent: the agent never read their words, so the
        # entry isn't one "Undo opt-out" lifts in one click. The user lifts
        # it on the do-not-contact list, saying why: state._undo_opt_out.)
        await state.update_prospect_status(prospect.id, "opted_out", reason="by_hand",
                                           by=suppression.BY_AGENT, note=note)
        cancelled = await state.cancel_pending_outbox_for_prospect(prospect.id,
                                                                   reason="opted out")
        await state.update_conversation(c["id"], status="closed", stage="closed_lost")
        return {"conversation": c["id"], "added": True, "cancelled": cancelled}
    try:
        if contact not in (None, ""):
            row = await _one(state, "overlay.agent.contact_value", (int(contact),))
            if row is None or row[0] != "email":
                raise Refused(f"contact {contact} isn't an email address on file")
            try:
                made = await state.suppress("address", row[1], "by_hand",
                                            by=suppression.BY_AGENT, note=note)
            except suppression.Refused:
                # (Its words name the address, which the agent never reads.)
                raise Refused(f"contact {int(contact)}'s address isn't one the list "
                              "takes") from None
            return {"contact": int(contact), "added": made is not None}
        made = await state.suppress("firm", str(firm), "by_hand", by=suppression.BY_AGENT,
                                    note=note)
        return {"firm": str(firm), "added": made is not None}
    except Refused:
        raise
    except suppression.Refused as e:
        raise Refused(str(e)) from None
    except (TypeError, ValueError):
        raise Refused("contact is a contact's id; firm is a firm's id") from None


# ── The plan card ──

async def for_plan(state, plan_id) -> list[dict]:
    """The plan's campaigns as its card shows them: each one's status and
    counts, and, while a draft whose start was asked for, the request (its
    audience, sequence, note and sending account, what was counted)."""
    from harvey import campaigns

    out = []
    for link in await of_plan(state, plan_id):
        try:
            s = await status(state, link["campaign"])
        except Refused:
            continue
        entry = {"id": s["campaign"], "name": s["name"], "status": s["status"],
                 "members": s["member_total"], "sent": s["sent"],
                 "replies": s["replies"]["total"], "needs_human": s["replies"]["needs_human"],
                 "start_requested": bool(link["start_requested_at"])}
        if link["start_requested_at"] and s["status"] == "draft":
            c = await state.campaigns_call(campaigns.get_campaign, link["campaign"],
                                           write=False)
            audience = await state.campaigns_call(campaigns.get_audience, c["audience_id"],
                                                  write=False) if c["audience_id"] else None
            account = await state.campaigns_call(campaigns.account, c["account_id"] or 1,
                                                 write=False)
            entry["request"] = {
                "at": link["start_requested_at"],
                "audience": {"id": audience["id"], "name": audience["name"],
                             "query": audience["query"]} if audience else None,
                "sequence": [{k: st.get(k) for k in ("step", "subject", "body", "delay_days")}
                             for st in c["sequence"]],
                "li_note": c["li_note"] or "", "rolling": c["rolling"],
                "review": c["review"], "offer": c["offer"],
                "account": {"address": account["address"], "provider": account["provider"]}
                if account else None,
                **(link["start_request"] or {})}
        out.append(entry)
    return out


def writing_rules(config) -> dict:
    """What a campaign's templates are written to: the email frameworks
    (skills/email_frameworks.md, the Writer's own), the merge variables
    the Sender fills, the gate's limits, the language the install writes
    in, and the legitimate-interest note's minimum and template."""
    from harvey import campaigns, gate
    from harvey.brain import SKILLS_DIR
    from harvey.config import DEFAULT_OPT_OUT_LINE, ENGLISH_OPT_OUT_LINE, compliance_of
    from harvey.textio import read_user_text

    try:
        frameworks = read_user_text(SKILLS_DIR / "email_frameworks.md")
    except OSError:
        frameworks = ""
    line = getattr(compliance_of(config), "opt_out_line", "") if config is not None else ""
    product = getattr(config, "product", None)
    return {
        "language": "en" if line == ENGLISH_OPT_OUT_LINE else "ro",
        "language_note": "the install's opt-out line is "
                         + ("English" if line == ENGLISH_OPT_OUT_LINE else "Romanian"
                            if line in ("", DEFAULT_OPT_OUT_LINE) else "the user's own")
                         + ": write in its language unless the user asks otherwise",
        "merge_vars": [f"{{{{{v}}}}}" for v in gate.MERGE_VARS],
        "sequence": {"steps": "1 to 5", "first_delay_days": 0, "delay_days": "1 to 60",
                     "subject_chars": gate.MAX_SUBJECT_LEN, "body_words": gate.MAX_BODY_WORDS,
                     "links": gate.MAX_LINKS, "aim": "email 1 and 2 under 75 words, email 3 "
                                                     "under 40; subjects lowercase, 2-5 words"},
        "added_when_sent": "the postal address, the opt-out line, the unsubscribe header and, "
                           "for a personal address, where it was found: never write them",
        "banned_phrases": list(gate.BANNED_PHRASES),
        "li_note_min": campaigns.LI_NOTE_MIN,
        "li_note_template": campaigns.li_note_template(getattr(product, "name", "") or ""),
        "frameworks": frameworks,
    }
