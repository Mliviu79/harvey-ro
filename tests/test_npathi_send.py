"""nPathi Chat: answering the SMS and WhatsApp messages people send in.

The Handler drafts an answer on the channel a message came on, it waits in
the Outbox for the user (autopilot never approves one), and the messenger
sends it under its outbox row's Idempotency-Key, inside the rails: only an
answer within the channel's window, the do-not-contact list, the sending
hours, the daily and credits caps, the text's rules. Every request goes to
a fake nPathi Chat on httpx.MockTransport (the public API's shapes); every
number, firm and person is invented. Nothing reaches the network.
"""

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harvey import channels, contacts, suppression
from harvey.agents import messenger as messenger_mod
from harvey.agents.messages import MessageReader
from harvey.agents.messenger import MessageSender
from harvey.integrations import npathi
from harvey.models import Company
from harvey.models.conversation import Conversation, Message
from harvey.modules import jobs
from harvey.state import MAYBE_SENT_PREFIX, Prospect
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)
from tests.test_npathi import (  # noqa: F401  (fixtures)
    ANN, KEY, STRANGER, URL, CountingBrain, Cfg, Env, FakeNpathi, _iso, _message, _no_pace,
    fake, state,
)
from tests.test_outbox_native import FakeProvider

pytestmark = pytest.mark.modules_on("npathi", "email_outreach")

DESK = "+40268000101"          # a fixed line
BOB = "+40730000104"           # another mobile


class SendCfg(Cfg):
    """The test config with a firm name to sign the first answer with."""

    class persona(Cfg.persona):
        company = "Exemplu Consult"

    class channels(Cfg.channels):
        class email(Cfg.channels.email):
            link_domains = ["exemplu-consult.example.com"]


class SendingNpathi(FakeNpathi):
    """FakeNpathi that also takes sends (idempotent per key, as nPathi's
    24 hours are), lists a sent message by id, and has the plan's rates."""

    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []
        self.by_key: dict[str, dict] = {}
        self.price = 0.8
        self.rates = [{"channel": "sms", "route": "phone_bridge", "unit": "message",
                       "credits": 0.5}]
        self.later: dict[int, dict] = {}    # id -> fields GET /messages/{id} reports now

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        queued = self.status_for.get(f"{request.method} {path}")
        if queued or request.headers.get("Authorization") != f"Bearer {KEY}":
            return super().respond(request)
        if request.method == "POST" and path in ("/messages/sms", "/messages/waba",
                                                 "/messages/whatsapp"):
            self.requests.append(request)
            key = request.headers.get("Idempotency-Key", "")
            if key in self.by_key:
                return httpx.Response(202, json={"data": self.by_key[key]})
            body = json.loads(request.content)
            row = _message(1000 + len(self.sent), body["to"], body.get("body") or "",
                           channel=path.rsplit("/", 1)[1].replace("whatsapp", "wa"))
            row.update({"direction": "out", "status": "queued", "price": self.price,
                        "to": body["to"], "reference": body.get("reference")})
            self.sent.append({"path": path, "body": body, "key": key, "row": row})
            self.by_key[key] = row
            return httpx.Response(202, json={"data": row})
        if request.method == "GET" and path.startswith("/messages/") and path[10:].isdigit():
            self.requests.append(request)
            mid = int(path[10:])
            row = next((s["row"] for s in self.sent if s["row"]["id"] == mid), None)
            if row is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})
            return httpx.Response(200, json={"data": {**row, **self.later.get(mid, {})}})
        if request.method == "GET" and path == "/rates":
            self.requests.append(request)
            return httpx.Response(200, json={"data": {"unit": "credits", "channels": self.rates,
                                                      "network": [], "ai": []}})
        return super().respond(request)


@pytest.fixture
def npat(monkeypatch):
    monkeypatch.setenv(npathi.KEY_ENV, KEY)
    return SendingNpathi()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _ready(state, npat, *, send=True):
    """Connected, the SMS sender chosen (the phone bridge, a reply comes
    back to it) and answers on, any hour of the day."""
    await channels.set_base_url(state, URL)
    await channels.check(state, npathi.Npathi(URL, KEY, transport=npat.transport()))
    await channels.set_route(state, "sms", "phone_bridge:9")
    await channels.set_sending(state, "sms", send)
    from harvey.modules import switches

    await switches.set_module_setting(state, npathi.MODULE, "send_start", "00:00")
    await switches.set_module_setting(state, npathi.MODULE, "send_end", "23:59")


async def _firm(state, phone=ANN, line="mobile"):
    firm = await state.add_company(Company(name="Alfa SRL", domain="alfa.example.com"))
    await state.add_contacts(firm, [contacts.Found("phone", phone, source="phones")])
    if line != "mobile":
        from harvey.overlay.schema import LINE_TYPE

        with sqlite3.connect(state.db_path) as con:
            con.execute("UPDATE contact SET line_type = ? WHERE value = ?",
                        (LINE_TYPE[line], phone))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Popescu", company="Alfa SRL", company_id=str(firm),
        email="ana@alfa.example.com", email_status="verified", status="contacted", phone=phone))
    return firm, pid


async def _convo(state, pid, phone=ANN, channel="sms", hours_ago=1.0):
    convo = Conversation(id="", prospect_id=pid, channel=channel, intent="question",
                         thread=[Message(sender="prospect", content="Cât costă?", channel=channel)])
    convo.id = await state.add_conversation(convo)
    await state.put_handle(convo.id, channel, phone, at=time.time() - hours_ago * 3600,
                           ref="npathi:1")
    return convo.id


async def _answer(state, convo, pid, body="Exemplu Consult: 200 lei pe lună. Vă sun mâine?\n"
                  "Răspunde STOP ca să nu mai primești mesaje.", phone=ANN, channel="sms",
                  approved=True):
    return await state.add_outbox_item(
        prospect_id=pid, conversation_id=convo, kind="reply", to_email="", subject="",
        body=body, send_at=_now().isoformat(), provider="npathi", channel=channel,
        to_phone=phone, status="approved" if approved else "pending_review",
        approved_by="you" if approved else "")


async def _row(state, item_id):
    return await state.get_outbox_item(item_id)


def _sender(state, npat):
    return MessageSender(state, SendCfg(), npat.transport())


class RecordingBrain(CountingBrain):
    def __init__(self, intent="question"):
        super().__init__(intent)
        self.prompts = []

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append(prompt)
        return await super().think(prompt, session_id, agent, task)


def _handler(state, npat, intent="question"):
    from harvey.agents.handler import Handler

    h = Handler(brain=RecordingBrain(intent), state=state, config=SendCfg(), env=Env())
    h.message_transport = npat.transport()
    return h


# ── Drafting ──

@pytest.mark.asyncio
async def test_a_message_gets_an_answer_drafted_that_waits_for_you(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    npat.messages = [_message(1, ANN, "Bună ziua, cât costă?")]
    handler = _handler(state, npat)
    assert await MessageReader(handler, npat.transport()).run()
    [row] = await state.get_outbox(status="pending_review")
    assert (row["channel"], row["to_phone"], row["kind"], row["subject"]) == (
        "sms", ANN, "reply", "")
    assert row["body"].startswith("Exemplu Consult: ") and row["body"].endswith(
        "Răspunde STOP ca să nu mai primești mesaje.")
    assert row["approved_by"] == "" and npat.sent == []
    [convo] = await state.get_conversations_by_status("open")
    assert row["conversation_id"] == convo.id
    [prompt] = [p for p in handler.brain.prompts if "goes by SMS" in p]
    assert "at most" in prompt and "curly braces" in prompt


@pytest.mark.asyncio
async def test_with_answers_off_on_the_channel_it_goes_to_you(state, npat):
    await _ready(state, npat, send=False)
    await _firm(state)
    npat.messages = [_message(1, ANN, "Bună ziua, cât costă?")]
    assert await MessageReader(_handler(state, npat), npat.transport()).run()
    assert await state.get_outbox(status="pending_review") == []
    [convo] = await state.get_conversations_by_status("needs_human")
    assert convo.channel == "sms"


@pytest.mark.asyncio
async def test_autopilot_never_approves_or_sends_an_answer(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid, approved=False)
    assert await state.promote_for_autopilot(_now().isoformat(), 100, kind="reply") == 0
    assert await state.approve_outbox() == 0              # Approve all leaves it too
    assert await _sender(state, npat).run() == 0
    assert (await _row(state, item))["status"] == "pending_review" and npat.sent == []
    # Approved some other way (not by you): back to the Outbox.
    await state.update_outbox_item(item, status="approved", approved_by="autopilot")
    assert await _sender(state, npat).run() == 0
    row = await _row(state, item)
    assert row["status"] == "pending_review" and row["error"] == messenger_mod.NEEDS_YOU


@pytest.mark.asyncio
async def test_the_email_drain_never_sends_an_answer(state, npat):
    from tests.test_outbox_native import make_sender

    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    provider = FakeProvider()
    await make_sender(state, provider).run()
    assert provider.sent == [] and (await _row(state, item))["status"] == "approved"


# ── Sending ──

@pytest.mark.asyncio
async def test_an_approved_answer_goes_with_its_rows_own_key(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    [sent] = npat.sent
    assert sent["path"] == "/messages/sms" and sent["key"] == f"harvey-outbox-{item}"
    assert sent["body"]["to"] == ANN and sent["body"]["connection"] == {"type": "phone_bridge",
                                                                        "id": 9}
    row = await _row(state, item)
    assert (row["status"], row["provider_status"], row["price_credits"]) == ("sent", "queued", 0.8)
    assert row["message_id"] == str(sent["row"]["id"]) and row["sent_at"]
    stored = await state.get_conversation(convo)
    assert (stored.thread[-1].sender, stored.thread[-1].channel) == ("harvey", "sms")
    # Email's own counts don't see it.
    assert await state.count_outbox_sent_today() == 0


@pytest.mark.asyncio
async def test_a_send_cut_off_goes_again_under_the_same_key_once(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    # Harvey claimed it and nPathi took it, then Harvey stopped before it
    # recorded the answer.
    assert await state.claim_outbox_item(item)
    await state.set_setting(channels.ATTEMPT_KEY + item, str(int(time.time())))
    client = npathi.Npathi(URL, KEY, transport=npat.transport())
    await client.send("sms", ANN, (await _row(state, item))["body"], {"type": "phone_bridge",
                                                                        "id": 9},
                      key=f"harvey-outbox-{item}")
    with sqlite3.connect(state.db_path) as con:
        con.execute("UPDATE outbox SET updated_at = ? WHERE id = ?",
                    ((_now() - timedelta(minutes=30)).isoformat(), item))
    assert await _sender(state, npat).run() == 1
    assert len(npat.sent) == 1                              # nPathi answered with the first
    posts = [r for r in npat.requests if r.method == "POST" and r.url.path.endswith("/sms")]
    assert len(posts) == 2 and {p.headers["Idempotency-Key"] for p in posts} == {
        f"harvey-outbox-{item}"}
    assert (await _row(state, item))["status"] == "sent"


@pytest.mark.asyncio
async def test_a_send_cut_off_past_a_day_isnt_tried_again(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    await state.set_setting(channels.ATTEMPT_KEY + item, str(int(time.time() - 25 * 3600)))
    assert await _sender(state, npat).run() == 0
    row = await _row(state, item)
    assert row["status"] == "failed" and row["error"].startswith(MAYBE_SENT_PREFIX)
    assert npat.sent == []


# ── The rails: one refusal each ──

async def _blocked(state, npat, **answer):
    _, pid = await _firm(state, **{k: answer.pop(k) for k in ("line",) if k in answer})
    convo = await _convo(state, pid, hours_ago=answer.pop("hours_ago", 1.0),
                         phone=answer.pop("handle", ANN))
    item = await _answer(state, convo, pid, **answer)
    return item, await _sender(state, npat).blocked(await _row(state, item))


@pytest.mark.asyncio
@pytest.mark.parametrize("given,why", [
    ({"phone": "0722000102"}, "E.164"),
    ({"line": "fixed"}, "mobile only"),
    ({"hours_ago": 31 * 24}, "within its window"),
    ({"handle": BOB}, "only an answer"),
    ({"body": "Exemplu Consult: " + "a" * 300 + "\nRăspunde STOP ca să nu mai primești mesaje."},
     "too long"),
    ({"body": "Exemplu Consult: {Salut|Bună}\nRăspunde STOP ca să nu mai primești mesaje."},
     "curly braces"),
    ({"body": "Exemplu Consult: vezi https://other.example.net/x\n"
              "Răspunde STOP ca să nu mai primești mesaje."}, "not one of your domains"),
    ({"body": "Exemplu Consult: click here\nRăspunde STOP ca să nu mai primești mesaje."},
     "banned phrases"),
    ({"body": "200 lei pe lună.\nRăspunde STOP ca să nu mai primești mesaje."}, "names your firm"),
    ({"body": "Exemplu Consult: 200 lei pe lună."}, "opt-out line"),
])
async def test_each_rail_refuses(state, npat, given, why):
    await _ready(state, npat)
    item, blocked = await _blocked(state, npat, **given)
    assert blocked[0] == "failed" and why in blocked[1], blocked


@pytest.mark.asyncio
async def test_whatsapp_business_keeps_to_its_24_hours(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid, channel="waba", hours_ago=25)
    item = await _answer(state, convo, pid, channel="waba")
    await channels.set_sending(state, "waba", True)
    await channels.set_route(state, "waba", "waba_number:4")
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked[0] == "failed" and "no templates" in blocked[1]


@pytest.mark.asyncio
async def test_the_do_not_contact_list_refuses_the_number_the_person_and_the_firm(state, npat):
    await _ready(state, npat)
    firm, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    sender = _sender(state, npat)
    for scope, value in (("phone", ANN), ("address", "ana@alfa.example.com"), ("firm", firm)):
        entry = await state.suppress(scope, value, "by_hand", by="you")
        blocked = await sender.blocked(await _row(state, item))
        assert blocked[0] == "failed" and "do-not-contact" in blocked[1], (scope, blocked)
        assert ANN not in blocked[1]
        await state.undo_suppression(entry, by="you")
    assert await sender.blocked(await _row(state, item)) is None


@pytest.mark.asyncio
async def test_a_number_nPathi_cant_reach_is_refused(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    await state.mark_unreachable(["sms"], ANN, "invalid_number")
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked[0] == "failed" and "invalid_number" in blocked[1]


@pytest.mark.asyncio
async def test_a_second_message_before_they_answer_is_refused(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    first = await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    second = await _answer(state, convo, pid, body="Ați văzut mesajul?")
    blocked = await _sender(state, npat).blocked(await _row(state, second))
    assert blocked[0] == "failed" and "second message" in blocked[1]
    assert (await _row(state, first))["status"] == "sent"


@pytest.mark.asyncio
async def test_only_the_first_answer_needs_the_name_and_the_opt_out_line(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    await state.put_handle(convo, "sms", ANN, at=time.time() + 2, ref="npathi:2")  # they wrote again
    later = await _answer(state, convo, pid, body="Mulțumesc, vă aștept.")
    assert await _sender(state, npat).blocked(await _row(state, later)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("setup,wait", [
    ("off", messenger_mod.WAIT_OFF),
    ("route", messenger_mod.WAIT_ROUTE),
    ("paused", messenger_mod.WAIT_PAUSED),
    ("paused_channel", messenger_mod.WAIT_PAUSED),
    ("gap", messenger_mod.WAIT_READ),
    ("read_failed", messenger_mod.WAIT_READ),
    ("hours", messenger_mod.WAIT_HOURS),
    ("cap", messenger_mod.WAIT_CAP),
])
async def test_each_wait_keeps_it_approved(state, npat, setup, wait):
    from harvey.modules import switches

    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    if setup == "off":
        await channels.set_sending(state, "sms", False)
        await channels.set_sending(state, "waba", True)   # (the drain runs; sms is off)
    elif setup == "route":
        await switches.set_module_setting(state, npathi.MODULE, "route_sms", "sender_id:3")
    elif setup == "paused":
        await state.set_setting("sending_paused", "paused from dashboard")
    elif setup == "paused_channel":
        await state.set_setting(channels.PAUSED_KEY + "sms", "paused from dashboard")
    elif setup == "gap":
        await state.set_setting(channels.GAP_KEY + "sms", json.dumps(
            {"since": 1, "covered_from": 2, "channel": "sms"}))
    elif setup == "read_failed":
        await state.set_setting(channels.PROBLEM_KEY + "sms", json.dumps({"kind": "server"}))
    elif setup == "hours":
        await switches.set_module_setting(state, npathi.MODULE, "send_start", "00:00")
        await switches.set_module_setting(state, npathi.MODULE, "send_end", "00:00")
    elif setup == "cap":
        await switches.set_module_setting(state, npathi.MODULE, "daily_cap_sms", 0)
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked == ("approved", wait)
    await _sender(state, npat).run()
    row = await _row(state, item)
    assert npat.sent == [] and row["status"] == "approved"


@pytest.mark.asyncio
async def test_quiet_hours_always_hold_answers(state, npat, monkeypatch):
    import harvey.main as main

    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)

    class QuietCfg(SendCfg):
        class usage:
            class quiet_hours:
                start, end, timezone = "00:00", "23:59", "UTC"

    monkeypatch.setattr(main, "in_quiet_hours", lambda config: True)
    blocked = await MessageSender(state, QuietCfg(), npat.transport()).blocked(
        await _row(state, item))
    assert blocked == ("approved", messenger_mod.WAIT_HOURS)


# ── Money ──

@pytest.mark.asyncio
async def test_the_credits_caps_hold_answers_with_the_plans_price(state, npat):
    from harvey.modules import switches

    await _ready(state, npat)
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 1.0)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1           # 0.8 of 1.0 spent
    _, pid2 = await _firm(state, phone=BOB)
    convo2 = await _convo(state, pid2, phone=BOB)
    item = await _answer(state, convo2, pid2, phone=BOB)
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked == ("approved", messenger_mod.WAIT_CREDITS)  # 0.8 + 0.5 x 2 segments > 1
    spend = await channels.spend(state)
    assert spend["credits_today"] == pytest.approx(0.8) and spend["usd_today"] is None
    await switches.set_module_setting(state, npathi.MODULE, "usd_per_credit", 0.05)
    assert (await channels.spend(state))["usd_today"] == pytest.approx(0.04)
    assert len([r for r in npat.requests if r.url.path.endswith("/rates")]) == 1  # a day


def test_an_sms_counts_its_segments():
    assert channels.segments("a" * 160) == 1 and channels.segments("a" * 161) == 2
    assert channels.segments("ș" * 70) == 1 and channels.segments("ș" * 71) == 2


@pytest.mark.asyncio
async def test_out_of_credits_pauses_the_lane_and_reading_goes_on(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    npat.status_for["POST /messages/sms"] = [
        (402, {"error": {"code": "insufficient_credits", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    assert await jobs.lane_paused(state, npathi.LANE) == npathi.NO_CREDITS
    row = await _row(state, item)
    assert (row["status"], row["error"]) == ("approved", messenger_mod.WAIT_NO_CREDITS)
    assert await _sender(state, npat).blocked(row) == ("approved", messenger_mod.WAIT_PAUSED)
    npat.messages = [_message(5, STRANGER, "Bună ziua")]
    assert await MessageReader(_handler(state, npat), npat.transport()).run()
    assert await state.is_reply_processed("npathi:5")
    assert (await channels.status(state))["credits_out"]


# ── Delivery ──

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsubscribed", "invalid_number", "no_whatsapp"])
async def test_a_delivery_failure_is_acted_on_and_never_touches_email(state, npat, failure):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    mid = int((await _row(state, item))["message_id"])
    npat.later[mid] = {"status": "failed", "failure_reason": failure}
    await _sender(state, npat).read_back(npathi.Npathi(URL, KEY, transport=npat.transport()))
    row = await _row(state, item)
    assert (row["status"], row["provider_status"]) == ("failed", "failed") and failure in row["error"]
    listed = await state.suppression_check("", phone=ANN)
    assert (listed is not None) is (failure == "unsubscribed")
    assert bool(await state.unreachable("sms", ANN)) is (failure == "invalid_number")
    assert bool(await state.unreachable("wa", ANN)) is (failure == "no_whatsapp")
    assert not await state.get_setting("sending_paused")
    assert await state.get_setting("bounce_count") in ("", "0")
    # Read back once: nothing in flight any more.
    await _sender(state, npat).read_back(npathi.Npathi(URL, KEY, transport=npat.transport()))
    assert len([r for r in npat.requests if r.url.path.endswith(f"/messages/{mid}")]) == 1


# ── Exports, undo and nPathi's opt-out list ──

@pytest.mark.asyncio
async def test_the_exports_leave_out_a_stopped_number(state, npat):
    from harvey.export import collect_companies, collect_prospects

    def digits(company):
        return "".join(c for c in company["phone"] + company["other_phones"] if c.isdigit())

    firm, pid = await _firm(state)
    assert [p["phone"] for p in await collect_prospects(state)] == [ANN]
    [company] = await collect_companies(state, ids=[str(firm)])
    assert "722000102" in digits(company)
    await state.suppress("phone", ANN, "opt_out", by="reply")
    [row] = await collect_prospects(state)
    assert row["phone"] == "" and row["email"] == "ana@alfa.example.com"
    [company] = await collect_companies(state, ids=[str(firm)])
    assert "722000102" not in digits(company)


def test_the_contacts_export_leaves_out_a_stopped_number(client, env):  # noqa: F811
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        firm = await state.add_company(Company(name="Alfa SRL", domain="alfa.example.com"))
        await state.add_contacts(firm, [contacts.Found("phone", ANN, source="phones"),
                                        contacts.Found("phone", BOB, source="phones")])
        await state.suppress("phone", ANN, "opt_out", by="reply")

    asyncio.run(seed())
    text = client.get("/api/export/contacts.csv").text.replace(" ", "")
    assert "730000104" in text and "722000102" not in text


@pytest.mark.asyncio
async def test_undoing_an_opt_out_lifts_their_numbers_entry(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    npat.messages = [_message(1, ANN, "STOP")]
    assert await MessageReader(_handler(state, npat), npat.transport()).run()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.suppression_check("", phone=ANN) is not None
    assert await state.undo_opt_out(pid)
    assert await state.suppression_check("", phone=ANN) is None
    assert await state.suppression_check("ana@alfa.example.com") is None


@pytest.mark.asyncio
async def test_an_objection_by_sms_isnt_lifted_with_one_click(state, npat):
    _, pid = await _firm(state)
    await state.update_prospect_status(pid, "opted_out", reason="opt_out", by="reply")
    await state.suppress("phone", ANN, "objection", by="reply")
    with pytest.raises(suppression.Refused):
        await state.undo_opt_out(pid)


@pytest.mark.asyncio
async def test_the_sync_keeps_a_number_you_took_off_the_list(state, npat):
    await _ready(state, npat)
    entry = await state.suppress("phone", ANN, "opt_out", by="reply")
    await state.undo_suppression(entry, by="you")
    npat.unsubscribed = [
        {"id": 1, "phone": ANN, "scope": "all", "unsubscribed_at": _iso(time.time() - 3600)},
        {"id": 2, "phone": BOB, "scope": "all", "unsubscribed_at": _iso(time.time() - 3600)}]
    assert await MessageReader(_handler(state, npat), npat.transport()).run()
    assert await state.suppression_check("", phone=ANN) is None      # your undo stands
    assert await state.suppression_check("", phone=BOB) is not None
    assert (await channels.status(state))["still_listed"] == 1
    # A STOP nPathi recorded after your undo puts it back.
    npat.unsubscribed[0]["unsubscribed_at"] = _iso(time.time() + 60)
    await state.set_setting(channels.SYNC_KEY, "0")
    assert await MessageReader(_handler(state, npat), npat.transport()).run()
    assert await state.suppression_check("", phone=ANN) is not None


@pytest.mark.asyncio
async def test_a_key_that_cant_read_the_list_waits_a_day(state, npat):
    await _ready(state, npat)
    forbidden = (403, {"error": {"code": "forbidden", "message": "x"}}, {})
    npat.status_for["GET /unsubscribed"] = [forbidden, forbidden]
    for _ in range(2):
        await MessageReader(_handler(state, npat), npat.transport()).run()
    assert len(npat.calls("GET", "/unsubscribed")) == 1
    assert (await channels.status(state))["optout_sync"]["problem_kind"] == "forbidden"


# ── The agent and the dashboard ──

@pytest.mark.asyncio
async def test_the_handlers_draft_for_the_agent_stays_held(state, npat):
    from harvey.state import AGENT_DRAFT_HELD

    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _handler(state, npat).draft_answer(convo, "Propune un apel", hold=True)
    row = await _row(state, item)
    assert (row["channel"], row["to_phone"], row["status"], row["error"]) == (
        "sms", ANN, "pending_review", AGENT_DRAFT_HELD)
    assert await state.approve_outbox() == 0


@pytest.fixture
def dash_npat(npat, monkeypatch):
    real = npathi.Npathi.__init__

    def init(self, base_url, api_key, *, transport=None):
        real(self, base_url, api_key, transport=transport or npat.transport())

    monkeypatch.setattr(npathi.Npathi, "__init__", init)
    return npat


def _seed_convo(env, npat, hours_ago=1.0):
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await _ready(state, npat)
        _, pid = await _firm(state)
        return await _convo(state, pid, hours_ago=hours_ago)

    return asyncio.run(seed())


def test_you_can_write_an_answer_in_conversations(client, env, dash_npat):  # noqa: F811
    convo = _seed_convo(env, dash_npat)
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["can_answer"] and ANN not in json.dumps(row)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine la 10?"})
    assert r.status_code == 200 and r.json()["status"] == "pending_review"
    out = client.get("/api/outbox").json()
    [item] = out["pending"]
    assert item["channel"] == "sms" and item["to_phone"] == "+4072****" and item["subject"] == ""
    assert "Vă sun mâine la 10?" in item["body"]
    assert ANN not in json.dumps(out)
    bad = client.post(f"/api/conversations/{convo}/answer", json={"body": "{Salut|Bună}"})
    assert bad.status_code == 400 and "curly braces" in bad.json()["message"]


def test_an_answer_outside_the_window_is_refused_in_conversations(client, env, dash_npat):  # noqa: F811
    convo = _seed_convo(env, dash_npat, hours_ago=31 * 24)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 409 and "window" in r.json()["message"]


def test_approve_refuses_an_answer_the_gate_would_block(client, env, dash_npat):  # noqa: F811
    from harvey.state import StateManager

    convo = _seed_convo(env, dash_npat)

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        pid = (await state.get_conversation(convo)).prospect_id
        return await _answer(state, convo, pid, body="x" * 400, approved=False)

    item = asyncio.run(seed())
    r = client.post(f"/api/outbox/{item}/approve")
    assert r.status_code == 409 and "too long" in r.json()["message"]
    r = client.post(f"/api/outbox/{item}/edit", json={"body": "Vă sun mâine?"})
    assert r.status_code == 400 and "opt-out line" in r.json()["message"]


def test_the_channels_card_sets_sending(client, env, dash_npat):  # noqa: F811
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await channels.set_base_url(state, URL)
        await channels.check(state, npathi.Npathi(URL, KEY))

    asyncio.run(seed())
    assert client.post("/api/channels/sms", json={"send": True}).status_code == 409
    assert client.post("/api/channels/sms", json={"route": "sender_id:3"}).status_code == 400
    r = client.post("/api/channels/sms", json={"route": "phone_bridge:9", "daily_cap": 5})
    assert r.status_code == 200
    r = client.post("/api/channels/sms", json={"send": True})
    sms = next(c for c in r.json()["status"]["channels"] if c["id"] == "sms")
    assert (sms["send"], sms["route"], sms["daily_cap"]) == (True, "phone_bridge:9", 5)
    r = client.post("/api/channels/settings", json={"credits_per_day": 10, "send_end": "19:30"})
    status = r.json()["status"]
    assert status["spend"]["credits_per_day"] == 10 and status["send_hours"]["end"] == "19:30"
    assert client.post("/api/channels/settings", json={"send_end": "7pm"}).status_code == 400
    assert client.get("/api/usage").json()["messages"]["credits_today"] == 0


# ── What the review found ──

@pytest.mark.asyncio
async def test_a_key_still_in_progress_waits_and_one_unknown_is_never_sent_again(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    npat.status_for["POST /messages/sms"] = [
        (409, {"error": {"code": "idempotency_in_progress", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    row = await _row(state, item)
    assert (row["status"], row["error"]) == ("approved", messenger_mod.WAIT_RETRY)
    assert await _sender(state, npat).run() == 1 and len(npat.sent) == 1
    # nPathi can't say what became of the first try: failed as possibly sent.
    _, pid2 = await _firm(state, phone=BOB)
    convo2 = await _convo(state, pid2, phone=BOB)
    item2 = await _answer(state, convo2, pid2, phone=BOB)
    npat.status_for["POST /messages/sms"] = [
        (409, {"error": {"code": "idempotency_response_unavailable", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    row = await _row(state, item2)
    assert row["status"] == "failed" and row["error"].startswith(MAYBE_SENT_PREFIX)


@pytest.mark.asyncio
async def test_an_answer_that_may_have_gone_is_never_followed_by_another(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    first = await _answer(state, convo, pid)
    await state.update_outbox_item(first, status="failed", error=MAYBE_SENT_PREFIX + "x")
    item = await _answer(state, convo, pid, body="Exemplu Consult: altceva.\n"
                         "Răspunde STOP ca să nu mai primești mesaje.")
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked[0] == "failed" and "second message" in blocked[1]


@pytest.mark.asyncio
async def test_with_no_price_known_a_cap_lets_one_answer_go_until_it_is_priced(state, npat):
    from harvey.modules import switches

    await _ready(state, npat)
    npat.rates, npat.price = [], None
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 10.0)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    _, pid2 = await _firm(state, phone=BOB)
    convo2 = await _convo(state, pid2, phone=BOB)
    item = await _answer(state, convo2, pid2, phone=BOB)
    assert await _sender(state, npat).blocked(await _row(state, item)) == (
        "approved", messenger_mod.WAIT_CREDITS)
    # nPathi says what the first one cost: the next goes.
    mid = npat.sent[0]["row"]["id"]
    npat.later[mid] = {"price": 0.5}
    await _sender(state, npat).read_back(npathi.Npathi(URL, KEY, transport=npat.transport()))
    assert await _sender(state, npat).blocked(await _row(state, item)) is None


@pytest.mark.asyncio
async def test_an_answer_that_failed_on_delivery_still_counts_its_credits_and_cap(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1
    mid = int((await _row(state, item))["message_id"])
    npat.later[mid] = {"status": "failed", "failure_reason": "carrier_rejected"}
    await _sender(state, npat).read_back(npathi.Npathi(URL, KEY, transport=npat.transport()))
    assert (await _row(state, item))["status"] == "failed"
    spend = await channels.spend(state)
    assert spend["credits_today"] == pytest.approx(0.8) and spend["sent_today"]["sms"] == 1


@pytest.mark.asyncio
async def test_npathis_refusal_of_a_number_you_took_off_the_list_keeps_your_undo(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    entry = await state.suppress("phone", ANN, "opt_out", by="reply")
    await state.undo_suppression(entry, by="you")
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    npat.status_for["POST /messages/sms"] = [
        (422, {"error": {"code": "contact_unsubscribed", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    assert (await _row(state, item))["status"] == "failed"
    assert await state.suppression_check("", phone=ANN) is None
    # One you never took off goes on the list.
    _, pid2 = await _firm(state, phone=BOB)
    convo2 = await _convo(state, pid2, phone=BOB)
    await _answer(state, convo2, pid2, phone=BOB)
    npat.status_for["POST /messages/sms"] = [
        (422, {"error": {"code": "contact_unsubscribed", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    assert await state.suppression_check("", phone=BOB) is not None


@pytest.mark.asyncio
async def test_an_sms_to_a_number_of_unknown_line_type_may_go(state, npat):
    await _ready(state, npat)
    item, blocked = await _blocked(state, npat, line="unknown")
    assert blocked is None


def test_a_message_opt_out_line_too_long_is_refused_not_cut():
    from pydantic import ValidationError

    from harvey.config import ComplianceConfig

    # Cutting at 120 would drop the STOP the check found past it.
    with pytest.raises(ValidationError):
        ComplianceConfig(message_opt_out_line="Mulțumim. " * 13 + "Răspunde STOP.")
    line = ComplianceConfig(message_opt_out_line="Răspunde STOP ca să nu mai primești mesaje.")
    assert line.message_opt_out_line.endswith("STOP ca să nu mai primești mesaje.")
