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

pytestmark = pytest.mark.modules_on("npathi")

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

    monkeypatch.setattr(main, "in_quiet_hours", lambda config, at=None: True)
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


def _no_quiet_hours(monkeypatch):
    import harvey.main as main

    monkeypatch.setattr(main, "in_quiet_hours", lambda config, at=None: False)


def test_an_answer_you_write_goes_without_a_second_approval(client, env, dash_npat,  # noqa: F811
                                                            monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["can_answer"] and ANN not in json.dumps(row)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine la 10?"})
    assert r.status_code == 200 and r.json()["status"] == "sent", r.json()
    [sent] = dash_npat.sent
    assert "Vă sun mâine la 10?" in sent["body"]["body"] and sent["body"]["to"] == ANN
    out = client.get("/api/outbox").json()
    assert out["pending"] == [] and out["approved"] == []
    [item] = out["sent"]
    assert item["approved_by"] == "you" and item["to_phone"] == "+4072****"
    assert ANN not in json.dumps(out)


def test_an_answer_you_write_the_gate_would_block_is_refused_and_not_queued(  # noqa: F811
        client, env, dash_npat, monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    bad = client.post(f"/api/conversations/{convo}/answer", json={"body": "{Salut|Bună}"})
    assert bad.status_code == 400 and "curly braces" in bad.json()["message"]
    assert bad.json()["code"] == "text"
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert dash_npat.sent == []


def test_an_answer_you_write_to_a_listed_number_is_refused(client, env, dash_npat,  # noqa: F811
                                                           monkeypatch):
    from harvey.state import StateManager

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    asyncio.run(StateManager(db_path=str(env["db"])).suppress("phone", ANN, "by_hand", by="you"))
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 409 and r.json()["code"] == "dnc" and ANN not in r.text
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert dash_npat.sent == []


def test_an_answer_outside_the_window_is_refused_in_conversations(client, env, dash_npat):  # noqa: F811
    convo = _seed_convo(env, dash_npat, hours_ago=31 * 24)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 409 and "window" in r.json()["message"]
    assert r.json()["code"] == "window"
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("hold,wait", [("hours", "hours"), ("cap", "cap"),
                                       ("paused", "paused"), ("credits", "credits")])
def test_an_answer_you_write_that_must_wait_stays_approved_and_says_why(  # noqa: F811
        client, env, dash_npat, monkeypatch, hold, wait):
    from harvey.modules import switches
    from harvey.state import StateManager

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)

    async def setup():
        state = StateManager(db_path=str(env["db"]))
        if hold == "hours":
            await switches.set_module_setting(state, npathi.MODULE, "send_start", "00:00")
            await switches.set_module_setting(state, npathi.MODULE, "send_end", "00:00")
        elif hold == "cap":
            await switches.set_module_setting(state, npathi.MODULE, "daily_cap_sms", 0)
        elif hold == "paused":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 0.1)

    asyncio.run(setup())
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    data = r.json()
    assert r.status_code == 200 and (data["status"], data["wait"]) == ("approved", wait), data
    assert dash_npat.sent == []
    out = client.get("/api/outbox").json()
    [item] = out["approved"]
    assert item["approved_by"] == "you" and item["wait"] == wait and out["pending"] == []
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["waiting"]["wait"] == wait and row["waiting"]["id"] == data["outbox_id"]


def test_an_answer_you_write_while_a_pass_runs_goes_on_the_next(client, env, dash_npat,  # noqa: F811
                                                               monkeypatch):
    from harvey.agents import message_loop
    from harvey.state import StateManager

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)

    async def hold_lease():
        from harvey.modules.worker import acquire_lease

        assert await acquire_lease(StateManager(db_path=str(env["db"])), "another-pass",
                                   name=message_loop.LEASE)

    asyncio.run(hold_lease())
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 200 and r.json()["wait"] == "next_pass"
    assert dash_npat.sent == []
    [item] = client.get("/api/outbox").json()["approved"]
    assert item["status"] == "approved" and item["approved_by"] == "you"


def test_a_second_answer_you_write_while_one_waits_is_refused(client, env, dash_npat,  # noqa: F811
                                                             monkeypatch):
    from harvey.modules import switches
    from harvey.state import StateManager

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)

    async def outside_hours():
        state = StateManager(db_path=str(env["db"]))
        await switches.set_module_setting(state, npathi.MODULE, "send_start", "00:00")
        await switches.set_module_setting(state, npathi.MODULE, "send_end", "00:00")

    asyncio.run(outside_hours())
    first = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert first.status_code == 200 and first.json()["wait"] == "hours"
    # (A double click, or a second answer written before the first went.)
    again = client.post(f"/api/conversations/{convo}/answer", json={"body": "Sau poimâine?"})
    assert again.status_code == 409 and again.json()["code"] == "waiting"
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert dash_npat.sent == []


def test_an_answer_you_write_waits_for_the_next_pass_when_sending_it_now_fails(  # noqa: F811
        client, env, dash_npat, monkeypatch):
    from harvey.agents import message_loop

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)

    async def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(message_loop, "send_now", locked)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 200 and r.json()["wait"] == "next_pass", r.text
    [item] = client.get("/api/outbox").json()["approved"]
    assert item["approved_by"] == "you" and dash_npat.sent == []


def test_an_answer_you_write_with_no_price_known_under_a_cap_waits(client, env, dash_npat,  # noqa: F811
                                                                   monkeypatch):
    from harvey.modules import switches
    from harvey.state import StateManager

    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    dash_npat.rates, dash_npat.price = [], None
    asyncio.run(switches.set_module_setting(StateManager(db_path=str(env["db"])),
                                            npathi.MODULE, "credits_per_month", 50.0))
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["wait"]) == (
        "approved", "unpriced"), r.text
    assert dash_npat.sent == []


@pytest.mark.asyncio
async def test_a_retry_with_no_price_known_under_a_cap_waits(state, npat):
    from harvey.modules import switches

    await _ready(state, npat)
    npat.rates, npat.price = [], None
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    npat.status_for["POST /messages/sms"] = [
        (409, {"error": {"code": "idempotency_in_progress", "message": "x"}}, {})]
    assert await _sender(state, npat).run() == 0
    assert (await _row(state, item))["error"] == messenger_mod.WAIT_RETRY
    # A cap set meanwhile: the retry waits for a price like any answer.
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 5.0)
    assert await _sender(state, npat).run() == 0 and npat.sent == []
    assert (await _row(state, item))["error"] == messenger_mod.WAIT_UNPRICED


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
async def test_with_no_price_known_a_cap_holds_answers_and_no_cap_lets_them_go(state, npat):
    from harvey.modules import switches

    await _ready(state, npat)
    npat.rates, npat.price = [], None
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 10.0)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    blocked = await _sender(state, npat).blocked(await _row(state, item))
    assert blocked == ("approved", messenger_mod.WAIT_UNPRICED) and blocked.code == "unpriced"
    assert await _sender(state, npat).run() == 0 and npat.sent == []
    assert (await _row(state, item))["error"] == messenger_mod.WAIT_UNPRICED
    assert (await channels.status(state))["unpriced_cap"] == ["sms"]
    # The month's cap alone holds it too.
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", None)
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_month", 100.0)
    assert (await _sender(state, npat).blocked(await _row(state, item))).code == "unpriced"
    # With no cap the price doesn't matter.
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_month", None)
    assert (await channels.status(state))["unpriced_cap"] == []
    assert await _sender(state, npat).run() == 1


@pytest.mark.asyncio
async def test_a_price_paid_lets_answers_go_under_a_cap(state, npat):
    from harvey.modules import switches

    await _ready(state, npat)
    npat.rates = []
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    await _answer(state, convo, pid)
    assert await _sender(state, npat).run() == 1               # no cap yet: 0.8 paid
    await switches.set_module_setting(state, npathi.MODULE, "credits_per_day", 10.0)
    _, pid2 = await _firm(state, phone=BOB)
    convo2 = await _convo(state, pid2, phone=BOB)
    item = await _answer(state, convo2, pid2, phone=BOB)
    assert await _sender(state, npat).blocked(await _row(state, item)) is None
    assert (await channels.status(state))["unpriced_cap"] == []


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


# ── The messaging loop, apart from email ──

class LoopBrain(RecordingBrain):
    """The brain the loop asks: usable, within its budget unless told."""

    def __init__(self, intent="question", within=True):
        super().__init__(intent)
        self.within = within

    async def preflight(self):
        return ""

    async def is_within_budget(self, *_a, **_k):
        return self.within


class LoopCfg(SendCfg):
    class usage:
        max_daily_claude_percent = 80

        class quiet_hours:
            start, end, timezone = "22:00", "07:00", "UTC"


class InstantlyCfg(LoopCfg):
    """A mail provider Harvey can't send or read through."""

    class channels(SendCfg.channels):
        class email(SendCfg.channels.email):
            provider = "instantly"


async def _beta(state):
    """Another firm, with Bob's number (Alfa's is Ana's)."""
    beta = await state.add_company(Company(name="Beta SRL", domain="beta.example.com"))
    await state.add_contacts(beta, [contacts.Found("phone", BOB, source="phones")])
    return beta


def _loop(state, npat, config=None, brain=None):
    from harvey.agents.message_loop import Loop

    return Loop(state, config=config or LoopCfg(), env=Env(), brain=brain or LoopBrain(),
                transport=npat.transport())


@pytest.fixture
def awake(monkeypatch):
    import harvey.main as main

    monkeypatch.setattr(main, "in_quiet_hours", lambda config, at=None: False)


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [LoopCfg, InstantlyCfg])
async def test_the_loop_reads_and_sends_whatever_email_is_doing(state, npat, awake, monkeypatch,
                                                                config):
    from harvey.agents.handler import Handler

    async def mailbox_down(self):
        raise RuntimeError("the mailbox can't be read")

    monkeypatch.setattr(Handler, "_run_native", mailbox_down)
    await state.note_sends_held("replies couldn't all be read and handled.")
    await state.set_setting("mail_read_gap:harvey@x.co", json.dumps({"since": 1}))
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    approved = await _answer(state, convo, pid)
    await _beta(state)
    npat.messages = [_message(7, BOB, "Bună ziua, cât costă?")]
    brain = LoopBrain()
    out = await _loop(state, npat, config(), brain).run("test-pass")
    assert out == {"read": True, "sent": 1}
    assert (await _row(state, approved))["status"] == "sent"
    # Bob's message was read and an answer drafted: it waits for you.
    drafted = await state.get_outbox(status="pending_review", channel="sms")
    assert [d["to_phone"] for d in drafted] == [BOB] and brain.calls >= 2


@pytest.mark.asyncio
async def test_the_kill_switch_still_stops_the_loops_sends(state, npat, awake):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    await state.set_setting("sending_paused", "paused from dashboard")
    assert (await _loop(state, npat).run("test-pass"))["sent"] == 0
    row = await _row(state, item)
    assert npat.sent == [] and (row["status"], row["error"]) == (
        "approved", messenger_mod.WAIT_PAUSED)


@pytest.mark.asyncio
async def test_never_two_passes_at_once(state, npat, awake):
    from harvey.agents import message_loop

    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    async with message_loop.held(state, "one-pass") as got:
        assert got
        assert await _loop(state, npat).run("another-pass") is None
        assert not await message_loop.send_now(state, item, SendCfg(), npat.transport())
        async with message_loop.held(state, "a-third") as third:
            assert not third
    assert npat.sent == []
    # Released: the next pass runs.
    assert (await _loop(state, npat).run("another-pass"))["sent"] == 1


@pytest.mark.asyncio
async def test_without_the_model_only_opt_outs_are_handled_and_the_rest_wait(state, npat,
                                                                              awake):
    await _ready(state, npat)
    await _firm(state)
    await _beta(state)
    npat.messages = [_message(8, ANN, "STOP"), _message(9, BOB, "Cât costă?")]
    brain = LoopBrain(within=False)
    out = await _loop(state, npat, brain=brain).run("test-pass")
    assert out["read"] is False and brain.calls == 0
    assert await state.suppression_check("", phone=ANN) is not None
    assert await state.is_reply_processed("npathi:8")
    assert not await state.is_reply_processed("npathi:9")
    # Within its budget again: Bob's message is handled on the next pass.
    brain.within = True
    assert (await _loop(state, npat, brain=brain).run("test-pass"))["read"] is True
    assert await state.is_reply_processed("npathi:9") and brain.calls >= 1


@pytest.mark.asyncio
async def test_quiet_hours_keep_the_model_out_of_the_loop(state, npat, monkeypatch):
    import harvey.main as main

    monkeypatch.setattr(main, "in_quiet_hours", lambda config, at=None: True)
    await _ready(state, npat)
    await _beta(state)
    npat.messages = [_message(9, BOB, "Cât costă?")]
    brain = LoopBrain()
    assert (await _loop(state, npat, brain=brain).run("test-pass"))["read"] is False
    assert brain.calls == 0 and not await state.is_reply_processed("npathi:9")


@pytest.mark.asyncio
async def test_a_full_worker_runs_the_messaging_passes(state, npat, awake, monkeypatch):
    from harvey.agents import message_loop
    from harvey.modules.worker import Worker

    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    ran = asyncio.Event()
    owners = []

    class TestLoop(message_loop.Loop):
        def __init__(self, st):
            super().__init__(st, config=LoopCfg(), env=Env(), brain=LoopBrain(),
                             transport=npat.transport())

        async def run(self, owner):
            owners.append(owner)
            try:
                return await super().run(owner)
            finally:
                ran.set()

    monkeypatch.setattr(message_loop, "Loop", TestLoop)
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(state, poll=0.05).run(stop))
    await asyncio.wait_for(ran.wait(), timeout=10)
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    assert (await _row(state, item))["status"] == "sent" and len(owners) == 1


def _seed_late(env, npat, channel):
    """A conversation whose last message came 23 hours ago, with the day's
    cap on its channel used up: the earliest it can go is tomorrow."""
    from harvey.modules import switches
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await _ready(state, npat)
        if channel == "waba":
            await channels.set_sending(state, "waba", True)
            await channels.set_route(state, "waba", "waba_number:4")
        await switches.set_module_setting(state, npathi.MODULE, f"daily_cap_{channel}", 0)
        _, pid = await _firm(state)
        return await _convo(state, pid, channel=channel, hours_ago=23.0)

    return asyncio.run(seed())


def test_an_answer_that_can_only_go_after_whatsapps_window_is_queued_with_a_warning(  # noqa: F811
        client, env, dash_npat, monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_late(env, dash_npat, "waba")
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    data = r.json()
    assert r.status_code == 200 and (data["status"], data["wait"]) == ("approved", "cap"), data
    assert data["warning"] == "window_closes" and data["goes_at"] > data["closes_at"]
    [item] = client.get("/api/outbox").json()["approved"]
    assert item["approved_by"] == "you" and item["channel"] == "waba"


def test_an_sms_answer_held_to_the_next_day_has_no_warning(client, env, dash_npat,  # noqa: F811
                                                           monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_late(env, dash_npat, "sms")
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    data = r.json()
    assert r.status_code == 200 and (data["status"], data["wait"]) == ("approved", "cap"), data
    assert "warning" not in data
