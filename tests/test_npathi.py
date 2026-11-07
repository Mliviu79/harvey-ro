"""nPathi Chat: the SMS and WhatsApp messages people send in, read in the
Handler's cycle, and the opt-outs they carry.

Every request goes to a fake nPathi Chat on httpx.MockTransport whose
answers have the shapes of the public API's schemas (Message, MessagePage,
Unsubscribed, the account). Every number, firm and person is invented
(numbers from blocks nobody is given); nothing reaches the network.
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from harvey import channels, contacts, suppression
from harvey.agents.messages import MessageReader, opt_out, phone_of
from harvey.integrations import npathi
from harvey.models import Company
from harvey.modules import lanes
from harvey.overlay import schema
from harvey.state import COLLEAGUE_REPLIED, Prospect
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)
from tests.test_outbox_native import Cfg, Env, StubBrain, state  # noqa: F401  (fixture)

pytestmark = pytest.mark.modules_on("npathi", "email_outreach")

KEY = "zk_test_fictional_key_7788"
URL = "https://chat.example.com"
ANN = "+40722000102"           # a mobile at one firm
DESK = "+40268000101"          # a switchboard two firms share
STRANGER = "+40740000103"      # nobody Harvey knows


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _message(mid: int, sender: str, body: str, channel: str = "sms", at: float | None = None):
    """A Message as GET /api/v1/messages lists it (every required field)."""
    return {
        "id": mid, "to": "+40722000100", "from": sender, "direction": "in", "sender": sender,
        "subject": None, "recipients": [], "cc": [], "provider_message_id": None, "body": body,
        "type": "text", "kind": "message", "contact_id": None, "site_id": None,
        "from_phone_id": None, "meta_message_id": None, "reply_to": None, "segments": 1,
        "channel": channel, "connection": None, "price": None, "status": "received",
        "failure_reason": None, "error": None, "error_message": None, "error_reason": None,
        "failover_from": None, "attachments": [], "campaign_id": None, "locked_at": None,
        "sent_at": None, "delivered_at": None, "read_at": None,
        "created_at": _iso(at if at is not None else time.time() - 60), "reference": None,
    }


class FakeNpathi:
    """The parts of nPathi Chat Harvey reads: its answers in the spec's shapes."""

    def __init__(self):
        self.messages: list[dict] = []
        self.unsubscribed: list[dict] = []
        self.requests: list[httpx.Request] = []
        # "METHOD /path" -> [(status, body, headers)], served before anything else
        self.status_for: dict[str, list] = {}
        self.page = 100

    def transport(self):
        return httpx.MockTransport(self.respond)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        queued = self.status_for.get(f"{request.method} {path}")
        if queued:
            status, body, headers = queued.pop(0)
            return httpx.Response(status, json=body, headers=headers)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"code": "unauthenticated",
                                                       "message": "Unauthenticated."}})
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/me":
            return httpx.Response(200, json={"data": {
                "profile": {"id": 1, "name": "Test Owner", "email": "owner@example.com"},
                "workspace": {"name": "Exemplu", "host": "chat.example.com"},
                "plan": {"name": "Business", "features": {"api": True, "sms": True, "wa": False}},
                "credits": {"balance": 120.5},
                "token": {"name": "harvey", "abilities": ["account.view", "sms.read", "waba.read",
                                                          "unsubscribed.manage",
                                                          "unsubscribed.view"],
                          "expires_at": None}}},
                headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"})
        if path == "/channels/available":
            return httpx.Response(200, json={"data": {
                "sms": {"default_route": False, "sender_ids": [{"id": 3, "name": "EXEMPLU"}],
                        "connections": [{"id": 9, "type": "phone_bridge", "name": "Office SIM"}]},
                "waba": {"phone_numbers": [{"id": 4, "display": "+40 722 000 100"}],
                         "templates": []}}})
        if path == "/messages" and request.method == "GET":
            rows = [m for m in self.messages if m["channel"] == q.get("channel")
                    and m["direction"] == q.get("direction")
                    and m["created_at"] >= q["since"]
                    and ("until" not in q or m["created_at"] <= q["until"])]
            rows.sort(key=lambda m: (m["created_at"], m["id"]), reverse=True)
            return self._page(rows, q)
        if path == "/unsubscribed" and request.method == "GET":
            return self._page(self.unsubscribed, q)
        if path == "/unsubscribed" and request.method == "POST":
            body = json.loads(request.content)
            row = {"id": len(self.unsubscribed) + 1, "phone": body["phone"],
                   "scope": body.get("scope", "all"), "reason": body.get("reason"),
                   "unsubscribed_at": _iso(time.time())}
            self.unsubscribed.append(row)
            return httpx.Response(201, json={"data": row})
        return httpx.Response(404, json={"error": {"code": "not_found",
                                                   "message": "The requested resource was not found."}})

    def _page(self, rows, q):
        start = int(q.get("cursor") or 0)
        size = min(int(q.get("limit") or 25), self.page)
        chunk = rows[start:start + size]
        more = start + size < len(rows)
        return httpx.Response(200, json={
            "data": chunk, "links": {"first": None, "last": None, "prev": None, "next": None},
            "meta": {"path": "/api/v1/x", "per_page": size,
                     "next_cursor": str(start + size) if more else None, "prev_cursor": None}})

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests
                if r.method == method and r.url.path == "/api/v1" + path]


@pytest.fixture(autouse=True)
def _no_pace(monkeypatch):
    """The lane's half-second pace isn't waited for here (its holds are)."""
    async def no_wait(lane):
        return None

    monkeypatch.setattr(lanes, "wait", no_wait)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv(npathi.KEY_ENV, KEY)
    return FakeNpathi()


class CountingBrain(StubBrain):
    def __init__(self, intent="question"):
        super().__init__(intent=intent)
        self.calls = 0

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.calls += 1
        return await super().think(prompt, session_id, agent, task)


def _handler(state, fake, intent="question"):
    from harvey.agents.handler import Handler

    handler = Handler(brain=CountingBrain(intent), state=state, config=Cfg(), env=Env())
    handler.message_transport = fake.transport()
    return handler


async def _connect(state):
    await channels.set_base_url(state, URL)


async def _firm(state, name, domain, phone):
    firm = await state.add_company(Company(name=name, domain=domain))
    await state.add_contacts(firm, [contacts.Found("phone", phone, source="phones")])
    return firm


async def _person(state, firm, email, phone=""):
    return await state.add_prospect(Prospect(
        first_name=email.split("@")[0].title(), last_name="Popescu", title="Owner",
        company="Exemplu", company_id=str(firm), email=email, email_status="verified",
        email_verified=True, status="contacted", phone=phone))


async def _queued(state, pid, email, step=2):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b",
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        status="approved", campaign_id="c1", step=step, kind="sequence")


async def _status(state, item_id):
    [row] = [r for r in await state.get_outbox() if r["id"] == item_id]
    return row["status"], row.get("error") or ""


async def _conversations(state):
    out = []
    for status in ("open", "needs_human", "closed"):
        out += await state.get_conversations_by_status(status)
    return out


# ── The client ──

@pytest.mark.asyncio
async def test_the_account_and_the_channels_are_read_with_the_key_as_bearer(fake):
    client = npathi.Npathi(URL, KEY, transport=fake.transport())
    account = await client.account()
    assert (account.workspace, account.plan, account.credits) == ("Exemplu", "Business", 120.5)
    assert account.can_read("sms") and not account.can_read("wa")
    assert "api" in account.features and "wa" not in account.features
    senders = await client.channels()
    assert [(s["type"], s["receives"]) for s in senders["sms"]] == [
        ("phone_bridge", True), ("sender_id", False)]
    assert senders["waba"][0]["receives"] and "wa" not in senders
    assert all(r.headers["Authorization"] == f"Bearer {KEY}" for r in fake.requests)
    assert KEY not in repr(client)


@pytest.mark.asyncio
async def test_a_refused_key_says_so_in_harveys_words(fake):
    client = npathi.Npathi(URL, "zk_wrong_key", transport=fake.transport())
    with pytest.raises(npathi.NpathiError) as e:
        await client.account()
    assert e.value.kind == "key" and e.value.code == "unauthenticated"
    assert "Unauthenticated." not in str(e.value) and "zk_wrong_key" not in str(e.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,kind", [
    (402, "insufficient_credits", "credits"), (403, "missing_ability", "forbidden"),
    (500, "server_error", "server"), (422, "validation_failed", "refused")])
async def test_each_refusal_has_its_class_and_never_the_servers_text(fake, status, code, kind):
    fake.status_for["GET /me"] = [(status, {"error": {"code": code, "message": "PLANTED-TEXT"}}, {})]
    with pytest.raises(npathi.NpathiError) as e:
        await npathi.Npathi(URL, KEY, transport=fake.transport()).account()
    assert (e.value.kind, e.value.code) == (kind, code) and "PLANTED" not in str(e.value)


@pytest.mark.asyncio
async def test_a_429_holds_the_lane_for_retry_after_without_another_request(fake):
    fake.status_for["GET /me"] = [(429, {"error": {"code": "rate_limited", "message": "x",
                                               "details": {"retry_after": 5}}},
                               {"Retry-After": "120"})]
    client = npathi.Npathi(URL, KEY, transport=fake.transport())
    with pytest.raises(npathi.NpathiError) as e:
        await client.account()
    assert e.value.kind == "rate" and e.value.retry_after == 120
    assert 100 < lanes.hold_info(npathi.LANE)[0] <= 120
    before = len(fake.requests)
    with pytest.raises(npathi.NpathiError, match="request limit"):
        await client.account()
    assert len(fake.requests) == before           # held: nothing sent
    lanes.hold(npathi.LANE, 0.001)


@pytest.mark.asyncio
async def test_no_header_holds_the_lane_longer_than_an_hour(fake, monkeypatch):
    """A wrong or hostile Retry-After or reset time can't stop reading (a
    STOP among it) for days."""
    held = []
    monkeypatch.setattr(lanes, "hold", lambda lane, seconds=None, why="": held.append(seconds))
    fake.status_for["GET /me"] = [
        (429, {"error": {"code": "rate_limited", "message": "x"}}, {"Retry-After": "9999999"}),
        (200, {"data": {}}, {"X-RateLimit-Remaining": "0",
                             "X-RateLimit-Reset": str(int(time.time()) + 9999999)})]
    client = npathi.Npathi(URL, KEY, transport=fake.transport())
    with pytest.raises(npathi.NpathiError):
        await client.account()
    await client.account()
    assert len(held) == 2 and all(0 < h <= 3600 for h in held)


@pytest.mark.asyncio
async def test_a_used_up_window_holds_the_lane_until_it_resets(fake):
    reset = int(time.time()) + 30
    fake.status_for["GET /me"] = [(200, {"data": {}}, {"X-RateLimit-Remaining": "0",
                                                   "X-RateLimit-Reset": str(reset)})]
    await npathi.Npathi(URL, KEY, transport=fake.transport()).account()
    assert 0 < lanes.hold_info(npathi.LANE)[0] <= 30
    lanes.hold(npathi.LANE, 0.001)


@pytest.mark.asyncio
async def test_inbound_walks_every_page_through_next_cursor(fake):
    now = time.time()
    fake.messages = [_message(i, STRANGER, f"mesaj {i}", at=now - i) for i in range(1, 251)]
    read = await npathi.Npathi(URL, KEY, transport=fake.transport()).inbound("sms", now - 3600)
    assert read.complete and len(read.messages) == 250
    pages = fake.calls("GET", "/messages")
    assert len(pages) == 3 and "cursor" not in pages[0].url.params
    assert pages[1].url.params["cursor"] == "100" and pages[0].url.params["limit"] == "100"
    assert pages[0].url.params["direction"] == "in"


@pytest.mark.asyncio
async def test_a_post_carries_the_same_idempotency_key_for_the_same_request(fake):
    client = npathi.Npathi(URL, KEY, transport=fake.transport())
    await client.add_unsubscribed(ANN, "asked Harvey to stop")
    await client.add_unsubscribed(ANN, "asked Harvey to stop")
    first, second = fake.calls("POST", "/unsubscribed")
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert json.loads(first.content) == {"phone": ANN, "scope": "all",
                                         "reason": "asked Harvey to stop"}


def test_the_address_is_https_and_never_a_private_or_link_local_one(monkeypatch):
    monkeypatch.delenv(npathi.DEV_URL_ENV, raising=False)
    assert npathi.base_url_problem("https://chat.example.com") == ""
    for bad in ("http://chat.example.com", "https://10.0.0.5", "https://127.0.0.1",
                "https://localhost", "https://169.254.169.254", "ftp://chat.example.com",
                "https://u:p@chat.example.com", ""):
        assert npathi.base_url_problem(bad), bad
    monkeypatch.setenv(npathi.DEV_URL_ENV, "1")
    assert npathi.base_url_problem("http://127.0.0.1:8080") == ""
    assert npathi.base_url_problem("https://169.254.169.254")     # never
    assert npathi.normalize_base_url("https://chat.example.com/api/v1/") == URL


@pytest.mark.asyncio
async def test_the_key_never_reaches_a_log(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.status_for["GET /me"] = [(500, {"error": {"code": "server_error", "message": KEY}}, {})]
    client = npathi.Npathi(URL, KEY, transport=fake.transport())
    with pytest.raises(npathi.NpathiError) as e:
        await client.account()
    await client.account()
    assert KEY not in str(e.value)
    assert KEY not in caplog.text and fake.requests


# ── Schema 40 ──

def test_a_schema_39_file_steps_up_to_40_once(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path, isolation_level=None)
    schema.create(con, version=39)
    con.execute("INSERT INTO outbox (id, to_email, status) VALUES ('o1', 'a@example.com', 'sent')")
    assert schema.step_up(con) == schema.LATEST == 40
    assert schema.missing(con) == []
    assert con.execute("SELECT channel, to_phone, price_credits FROM outbox").fetchone() == (
        "email", "", None)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(schema.PHONE_SUPPRESSION_GUARD_NAMES) <= names
    assert set(schema.guard_names(con)) <= names
    assert schema.step_up(con) == 40             # again: nothing to do
    con.close()


@pytest.mark.asyncio
async def test_the_email_counters_count_email_only(state):
    pid = await state.add_prospect(Prospect(first_name="Ana", email="ana@example.com",
                                            email_status="verified"))
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    sent = await state.add_outbox_item(prospect_id=pid, to_email="ana@example.com",
                                       subject="s", body="b", send_at=now, status="approved")
    await state.update_outbox_item(sent, status="sent", sent_at=now)
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO outbox (id, kind, status, sent_at, channel, to_phone) VALUES "
                "('m1', 'reply', 'sent', datetime('now'), 'sms', ?)", (ANN,))
    con.commit()
    con.close()
    assert await state.count_outbox_sent() == 1
    assert await state.count_outbox_sent_today() == 1
    assert await state.count_outbox_sent_today(kind="reply") == 0
    assert await state.count_outbox_sent_today(kind="sequence", since="2000-01-01") == 1


# ── Phones on the do-not-contact list ──

@pytest.mark.asyncio
async def test_a_phone_entry_flags_the_contact_and_check_reads_it(state):
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    assert await state.suppression_check("", phone=ANN) is None
    await state.suppress("phone", "+40 722 000 102", "opt_out", by="reply")
    hit = await state.suppression_check("", phone=ANN)
    assert (hit.scope, hit.value) == ("phone", ANN)
    con = sqlite3.connect(state.db_path)
    assert con.execute("SELECT do_not_contact FROM contact WHERE value = ?", (ANN,)).fetchone() \
        == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="do-not-contact"):
        con.execute("UPDATE contact SET do_not_contact = 0 WHERE value = ?", (ANN,))
    con.close()
    # A firm entry bars every number linked to the firm.
    await state.suppress("firm", firm, "by_hand", by="you")
    con = sqlite3.connect(state.db_path)
    assert suppression.check(con, "", phone=DESK) is None
    con.close()
    with pytest.raises(suppression.Refused):
        await state.suppress("phone", "0722 000 102", "opt_out", by="reply")


# ── Reading what people send in ──

@pytest.mark.asyncio
async def test_nothing_is_read_while_the_module_is_off(state, fake, monkeypatch):
    from harvey.modules import switches

    real = switches.resolve
    monkeypatch.setattr(switches, "resolve",
                        lambda stored, mans=None: real(stored, mans) - {"npathi"})
    await _connect(state)
    fake.messages = [_message(1, STRANGER, "Bună ziua")]
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    assert fake.requests == []


@pytest.mark.asyncio
async def test_a_stop_goes_first_and_reaches_every_list(state, fake):
    await _connect(state)
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    ann = await _person(state, firm, "ana@alfa.example.com", ANN)
    bob = await _person(state, firm, "bogdan@alfa.example.com")
    queued, colleague = await _queued(state, ann, "ana@alfa.example.com"), \
        await _queued(state, bob, "bogdan@alfa.example.com")
    fake.messages = [_message(1, ANN, "STOP")]
    handler = _handler(state, fake)
    assert await MessageReader(handler, fake.transport()).run()
    assert handler.brain.calls == 0                       # no model for an opt-out
    assert (await state.suppression_check("", phone=ANN)).reason == "opt_out"
    assert (await state.suppression_check("ana@alfa.example.com")) is not None
    assert (await state.get_prospect(ann)).status == "opted_out"
    assert (await _status(state, queued))[0] == "cancelled"
    status, error = await _status(state, colleague)
    assert status == "cancelled" and COLLEAGUE_REPLIED in error
    [convo] = await _conversations(state)
    assert (convo.status, convo.intent, convo.stage) == ("closed", "unsubscribe", "closed_lost")
    assert convo.channel == "sms" and convo.thread[0].channel == "sms"
    [post] = fake.calls("POST", "/unsubscribed")
    assert json.loads(post.content)["phone"] == ANN and post.headers["Idempotency-Key"]


@pytest.mark.parametrize("text,stops", [
    ("STOP", True), ("Stop.", True), ("dezabonare", True), ("Nu mă mai contactați!", True),
    ("NU", True), ("Nu mai trimiteți mesaje", True), ("stop all", True),
    ("Când puteți veni?", False), ("Nu am primit oferta", False), ("Stop by tomorrow?", False)])
def test_the_opt_out_words(text, stops):
    assert (opt_out(text) is not None) is stops


def test_a_sender_is_read_as_a_number():
    assert phone_of(ANN) == ANN
    assert phone_of("40722000102@s.whatsapp.net") == ANN
    assert phone_of("40722000102") == ANN
    assert phone_of("exemplu_user") == ""


@pytest.mark.asyncio
async def test_a_message_is_handled_once(state, fake):
    await _connect(state)
    fake.messages = [_message(7, STRANGER, "Bună ziua, aveți program sâmbăta?")]
    for _ in range(2):
        assert await MessageReader(_handler(state, fake), fake.transport()).run()
    [convo] = await _conversations(state)
    assert len(convo.thread) == 1 and await state.is_reply_processed("npathi:7")


@pytest.mark.asyncio
async def test_an_unknown_number_goes_to_you(state, fake):
    await _connect(state)
    fake.messages = [_message(1, STRANGER, "Bună ziua, cât costă?"),
                     _message(2, STRANGER, "Revin cu un telefon.", at=time.time() - 30)]
    handler = _handler(state, fake)
    assert await MessageReader(handler, fake.transport()).run()
    [convo] = await _conversations(state)
    assert (convo.status, convo.prospect_id, len(convo.thread)) == ("needs_human", "", 2)
    assert handler.brain.calls == 0
    handles = await state.conversation_handles([convo.id])
    assert handles == {convo.id: {"channel": "sms", "value": STRANGER}}


@pytest.mark.asyncio
async def test_a_shared_number_stops_no_firm(state, fake):
    await _connect(state)
    one = await _firm(state, "Alfa SRL", "alfa.example.com", DESK)
    two = await _firm(state, "Beta SRL", "beta.example.com", DESK)
    a, b = await _person(state, one, "ana@alfa.example.com"), \
        await _person(state, two, "bogdan@beta.example.com")
    qa, qb = await _queued(state, a, "ana@alfa.example.com"), \
        await _queued(state, b, "bogdan@beta.example.com")
    fake.messages = [_message(1, DESK, "Sunați-ne luni, vă rog.")]
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    assert (await _status(state, qa))[0] == (await _status(state, qb))[0] == "approved"
    assert await state.colleague_who_replied(a) is None
    [convo] = await _conversations(state)
    assert convo.status == "needs_human" and convo.prospect_id == ""


@pytest.mark.asyncio
async def test_an_sms_reply_cancels_the_email_sequence(state, fake):
    await _connect(state)
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    ann = await _person(state, firm, "ana@alfa.example.com", ANN)
    bob = await _person(state, firm, "bogdan@alfa.example.com")
    queued = await _queued(state, ann, "ana@alfa.example.com")
    colleague = await _queued(state, bob, "bogdan@alfa.example.com")
    fake.messages = [_message(1, ANN, "Sună interesant, sunați-mă mâine.")]
    handler = _handler(state, fake, intent="interested")
    assert await MessageReader(handler, fake.transport()).run()
    assert handler.brain.calls == 1
    assert (await _status(state, queued))[0] == "cancelled"
    assert COLLEAGUE_REPLIED in (await _status(state, colleague))[1]
    assert (await state.get_prospect(ann)).status == "replied"
    [convo] = await _conversations(state)
    # Answering on SMS comes later: the user answers it.
    assert (convo.prospect_id, convo.intent, convo.status) == (ann, "interested", "needs_human")


@pytest.mark.asyncio
async def test_a_firm_with_no_prospect_gets_one_and_counts_as_answered(state, fake):
    await _connect(state)
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    fake.messages = [_message(1, ANN, "Aveți disponibilitate săptămâna viitoare?")]
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    [convo] = await _conversations(state)
    card = await state.get_prospect(convo.prospect_id)
    assert (card.company_id, card.phone, card.source) == (str(firm), ANN, "npathi")
    later = await _person(state, firm, "ana@alfa.example.com")
    assert await state.colleague_who_replied(later)


@pytest.mark.asyncio
async def test_a_read_that_cant_reach_back_keeps_a_gap_until_it_is_read(state, fake, monkeypatch):
    await _connect(state)
    now = time.time()
    fake.messages = [_message(i, STRANGER, f"mesaj {i}", at=now - 600 - i)
                     for i in range(1, 251)]
    monkeypatch.setattr(npathi, "MAX_PAGES", 2)
    reader = MessageReader(_handler(state, fake), fake.transport())
    assert await reader.run()
    gap = await channels.read_gap(state, "sms")
    assert gap and gap["since"] < gap["covered_from"] and "STOP" in gap["reason"]
    assert await channels.last_read(state, "sms")        # what came after is read on
    monkeypatch.setattr(npathi, "MAX_PAGES", 20)
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    assert await channels.read_gap(state, "sms") is None
    assert all([await state.is_reply_processed(f"npathi:{i}") for i in range(1, 251)])


@pytest.mark.asyncio
async def test_a_failed_read_leaves_the_mark_and_says_why(state, fake):
    await _connect(state)
    fake.status_for["GET /messages"] = [(429, {"error": {"code": "rate_limited", "message": "x"}},
                                     {"Retry-After": "60"})]
    assert not await MessageReader(_handler(state, fake), fake.transport()).run()
    assert await channels.last_read(state, "sms") is None
    st = await channels.status(state)
    sms = next(c for c in st["channels"] if c["id"] == "sms")
    assert sms["problem_kind"] == "rate"
    lanes.hold(npathi.LANE, 0.001)


@pytest.mark.asyncio
async def test_nPathis_opt_out_list_is_read_once_a_day(state, fake):
    await _connect(state)
    fake.unsubscribed = [
        {"id": 1, "phone": ANN, "scope": "all", "reason": None, "unsubscribed_at": _iso(1)},
        {"id": 2, "phone": STRANGER, "scope": "voice", "reason": None, "unsubscribed_at": None}]
    for _ in range(2):
        await MessageReader(_handler(state, fake), fake.transport()).run()
    assert len(fake.calls("GET", "/unsubscribed")) == 1
    entries = {e["value"]: e for e in await state.suppression_entries()}
    assert set(entries) == {ANN}                         # a voice-only entry isn't a text opt-out
    assert (entries[ANN]["reason"], entries[ANN]["by"]) == ("by_hand", "npathi")


@pytest.mark.asyncio
async def test_a_number_nPathi_refuses_doesnt_hold_the_others_behind_it(state, fake):
    """A 422 (a number nPathi won't take) is final: asking again every cycle
    changes nothing, and it mustn't keep the numbers after it from going."""
    await _connect(state)
    refused = {"error": {"code": "validation_failed", "message": "x"}}
    fake.status_for["POST /unsubscribed"] = [(422, refused, {})]
    fake.messages = [_message(1, STRANGER, "STOP")]
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    assert not await state.settings_with_prefix(channels.PENDING_KEY)
    assert await state.suppression_check("", phone=STRANGER) is not None
    # Two still to add, the first refused: the second goes all the same.
    await state.set_setting(channels.PENDING_KEY + ANN, "1")
    await state.set_setting(channels.PENDING_KEY + DESK, "1")
    fake.status_for["POST /unsubscribed"] = [(422, refused, {})]
    await MessageReader(_handler(state, fake), fake.transport()).run()
    assert not await state.settings_with_prefix(channels.PENDING_KEY)
    posted = [json.loads(r.content)["phone"] for r in fake.calls("POST", "/unsubscribed")]
    assert posted[-2:] == sorted([ANN, DESK])


@pytest.mark.asyncio
async def test_a_stop_is_on_the_list_before_anything_else_can_fail(state, fake, monkeypatch):
    await _connect(state)
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    await _person(state, firm, "ana@alfa.example.com")

    async def broken(*a, **k):
        raise RuntimeError("the database is busy")

    monkeypatch.setattr(state, "phone_firms", broken)
    fake.messages = [_message(1, ANN, "STOP")]
    assert not await MessageReader(_handler(state, fake), fake.transport()).run()
    assert (await state.suppression_check("", phone=ANN)).reason == "opt_out"


class DownBrain(CountingBrain):
    """A brain whose provider stopped answering (logged out)."""
    paused_reason = "the Claude CLI is logged out"

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.calls += 1
        return ""


@pytest.mark.asyncio
async def test_an_opt_out_is_recorded_while_the_model_is_down(state, fake):
    """A message that needs the model waits for it; a STOP after it in the
    same read needs none, and is recorded now (their email must not go)."""
    await _connect(state)
    firm = await _firm(state, "Alfa SRL", "alfa.example.com", ANN)
    ann = await _person(state, firm, "ana@alfa.example.com", phone=ANN)
    other = await _firm(state, "Beta SRL", "beta.example.com", "+40722000104")
    bob = await _person(state, other, "bogdan@beta.example.com", phone="+40722000104")
    queued = await _queued(state, bob, "bogdan@beta.example.com")
    fake.messages = [_message(1, ANN, "Când puteți veni?", at=time.time() - 120),
                     _message(2, "+40722000104", "STOP", at=time.time() - 60)]
    handler = _handler(state, fake)
    handler.brain = DownBrain()
    assert not await MessageReader(handler, fake.transport()).run()
    assert not await state.is_reply_processed("npathi:1")
    assert await state.is_reply_processed("npathi:2")
    assert await state.suppression_check("", phone="+40722000104") is not None
    assert (await state.get_prospect(bob)).status == "opted_out"
    assert (await _status(state, queued))[0] == "cancelled"
    assert (await state.get_prospect(ann)).status != "opted_out"
    assert await channels.last_read(state, "sms") is None   # the mark waits


@pytest.mark.asyncio
async def test_an_opt_out_nPathi_didnt_take_is_sent_at_the_next_cycle(state, fake):
    await _connect(state)
    fake.status_for["POST /unsubscribed"] = [(503, {"error": {"code": "server_error",
                                                         "message": "x"}}, {})] * 1
    fake.messages = [_message(1, STRANGER, "STOP")]
    assert await MessageReader(_handler(state, fake), fake.transport()).run()
    assert await state.settings_with_prefix(channels.PENDING_KEY)
    await MessageReader(_handler(state, fake), fake.transport()).run()
    assert not await state.settings_with_prefix(channels.PENDING_KEY)
    assert any(json.loads(r.content)["phone"] == STRANGER
               for r in fake.calls("POST", "/unsubscribed") if r.content)


@pytest.mark.asyncio
async def test_a_message_never_holds_email(state, fake):
    """The Handler's cycle reads messages after the mailbox; a failed
    messaging read doesn't make the cycle's read fail."""
    from tests.test_outbox_native import FakeProvider

    await _connect(state)
    fake.status_for["GET /messages"] = [(500, {"error": {"code": "server_error", "message": "x"}},
                                     {})] * 3
    handler = _handler(state, fake)
    handler.provider = FakeProvider()
    assert await handler.run() is True


# ── Settings → Channels and Conversations ──

@pytest.fixture
def dash_fake(fake, monkeypatch):
    real = npathi.Npathi.__init__

    def init(self, base_url, api_key, *, transport=None):
        real(self, base_url, api_key, transport=transport or fake.transport())

    monkeypatch.setattr(npathi.Npathi, "__init__", init)
    return fake


def test_connecting_checks_the_key_first_and_never_returns_it(client, dash_fake, monkeypatch):
    import harvey.dashboard as dash

    monkeypatch.delenv(npathi.KEY_ENV)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/channels/connect", json={"base_url": URL, "key": "zk_wrong_key"})
    assert r.status_code == 400 and "refused the API key" in r.json()["message"]
    assert written == {}
    r = client.post("/api/channels/connect", json={"base_url": "http://chat.example.com",
                                                   "key": KEY})
    assert r.status_code == 400 and written == {}
    r = client.post("/api/channels/connect", json={"base_url": URL + "/api/v1", "key": KEY})
    assert r.status_code == 200 and written == {npathi.KEY_ENV: KEY}
    monkeypatch.setenv(npathi.KEY_ENV, KEY)
    status = client.get("/api/channels").json()
    assert status["connected"] and status["base_url"] == URL and status["key_set"]
    assert KEY not in json.dumps(status) and KEY not in r.text
    sms = next(c for c in status["channels"] if c["id"] == "sms")
    assert sms["receive"] and sms["can_read"] and len(sms["senders"]) == 2
    r = client.post("/api/channels/wa", json={"receive": True})
    assert next(c for c in r.json()["status"]["channels"] if c["id"] == "wa")["receive"]
    assert client.post("/api/channels/fax", json={"receive": True}).status_code == 400
    assert client.post("/api/channels/test").json()["success"]


def test_the_key_isnt_saved_through_the_generic_env_path(client, monkeypatch):
    import harvey.dashboard as dash

    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    client.post("/api/settings/env", json={npathi.KEY_ENV: "zk_anything"})
    assert npathi.KEY_ENV not in written


def test_a_conversation_shows_its_channel_and_the_number_masked(client, env, dash_fake):
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await channels.set_base_url(state, URL)
        dash_fake.messages = [_message(1, STRANGER, "Bună ziua")]
        await MessageReader(_handler(state, dash_fake), dash_fake.transport()).run()

    asyncio.run(seed())
    [row] = client.get("/api/conversations").json()
    assert row["handle"] == {"channel": "sms", "value": "+4074****"}
    assert row["channel"] == "sms" and row["thread"][0]["channel"] == "sms"
    assert STRANGER not in json.dumps(row)


def test_a_gap_is_a_today_item(client, env):
    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await state.set_setting(channels.GAP_KEY + "sms", json.dumps(
            {"since": 1, "covered_from": 2, "reason": "the SMS read couldn't reach back"}))

    asyncio.run(seed())
    items = client.get("/api/today").json()["items"]
    item = next(i for i in items if i["key"] == "message-read-gap")
    assert item["channel"] == "sms" and "reach back" in item["reason"]
