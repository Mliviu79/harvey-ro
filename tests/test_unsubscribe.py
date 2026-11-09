"""Unsubscribe with no public server (harvey/unsubscribe.py), and reading
every reply that could carry one: the List-Unsubscribe mailto header on
cold email through each provider, the token matched before anything else
about a message (an empty body included), Romanian opt-out words, messages
from strangers, the spam folder, and reads that cover the time Harvey was
off. No real email is sent: every provider is a fake or a mocked transport.
All data is fictional."""

import base64
import email
import email.policy
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from harvey import unsubscribe
from harvey.integrations.mail_provider import InboundMessage
from harvey.state import READ_GAP_KEY, StateManager
from tests.test_outbox_native import FakeProvider, make_handler, make_sender, seed_prospect


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class HeaderProvider(FakeProvider):
    """A fake mailbox that carries headers, as the three real ones do."""
    supports_headers = True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         headers=None):
        result = await super().send_email(to_email, subject, body, thread_ref, in_reply_to)
        self.sent[-1]["headers"] = headers
        return result


async def _queue(state, pid, to, *, step=1, kind="sequence", campaign="c1"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello", body="A short note. Question?",
        send_at=_now(), status="approved", campaign_id=campaign if kind == "sequence" else "",
        step=step, kind=kind)


async def _sent_cold(state, to="office@exemplu.example.com", status="contacted"):
    """A prospect, a sent first email with its token, a queued follow-up."""
    pid = await seed_prospect(state, email=to, status=status)
    first = await _queue(state, pid, to)
    token = unsubscribe.new_token()
    await state.update_outbox_item(first, status="sent", sent_at=_now(), message_id="<m1@x>",
                                   unsub_token=token)
    follow = await _queue(state, pid, to, step=2)
    return pid, first, follow, token


# ── The header ──

def test_the_header_is_a_mailto_with_the_token_and_no_one_click():
    token = unsubscribe.new_token()
    assert len(token) == 10 and set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    headers = unsubscribe.headers("harvey@exemplu.example.com", token)
    assert headers == {"List-Unsubscribe":
                       f"<mailto:harvey@exemplu.example.com?subject=dezabonare%20{token}>"}
    assert unsubscribe.find_token(f"dezabonare {token.lower()}") == token
    assert unsubscribe.find_token("Re: hello", f"DEZABONARE {token}\n") == token
    assert unsubscribe.find_token("dezabonare") == ""


@pytest.mark.asyncio
async def test_cold_email_carries_the_header_and_its_token_replies_dont(state):
    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    answered = await seed_prospect(state, email="office@altul.example.com", status="replied")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    await _queue(state, answered, "office@altul.example.com", kind="reply")
    provider = HeaderProvider()
    await make_sender(state, provider, require_approval=False)._drain_due()
    by_to = {m["to"]: m for m in provider.sent}
    token = (await state.get_outbox_item(cold))["unsub_token"]
    assert len(token) == 10
    assert by_to["office@exemplu.example.com"]["headers"] == {
        "List-Unsubscribe": f"<mailto:harvey@x.co?subject=dezabonare%20{token}>"}
    assert by_to["office@altul.example.com"]["headers"] is None
    assert not any("List-Unsubscribe-Post" in (m["headers"] or {}) for m in provider.sent)


@pytest.mark.asyncio
async def test_a_provider_that_cant_carry_headers_is_sent_none(state):
    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    await _queue(state, pid, "office@exemplu.example.com")
    provider = FakeProvider()        # its send_email takes no headers
    await make_sender(state, provider, require_approval=False)._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_gmail_and_smtp_put_the_header_in_the_message(monkeypatch):
    from harvey.integrations.gmail import GmailProvider
    from harvey.integrations.smtp_mail import SmtpImapProvider

    cfg = SimpleNamespace(persona=SimpleNamespace(name="Harvey", email="harvey@x.example.com"))
    headers = unsubscribe.headers("harvey@x.example.com", "ABCDEFGHIJ")
    gmail = GmailProvider(cfg, SimpleNamespace())
    gmail._request = AsyncMock(side_effect=[{"id": "g1", "threadId": "t1"}, None])
    assert (await gmail.send_email("ana@exemplu.example.com", "hello", "Body.",
                                   headers=headers)).ok
    raw = gmail._request.call_args_list[0].kwargs["json"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert msg["List-Unsubscribe"] == headers["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] is None

    sent = []

    async def fake_send(message, **kw):
        sent.append(message)

    import harvey.integrations.smtp_mail as smtp_mail

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    smtp = SmtpImapProvider(cfg, SimpleNamespace(smtp_host="smtp.example.com",
                                                 smtp_username="harvey", smtp_password="x"))
    assert (await smtp.send_email("ana@exemplu.example.com", "hello", "Body.",
                                  headers=headers)).ok
    assert sent[0]["List-Unsubscribe"] == headers["List-Unsubscribe"]
    assert smtp.from_address() == "harvey@x.example.com"


@pytest.mark.asyncio
async def test_microsoft_drafts_an_email_with_headers_from_its_mime_text(monkeypatch):
    from harvey.integrations import microsoft

    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"mail": "harvey@x.example.com"})
        if request.url.path.endswith("/send"):
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "d1", "internetMessageId": "<d1@x>",
                                         "conversationId": "c1"})

    cfg = SimpleNamespace(persona=SimpleNamespace(name="Harvey", email="harvey@x.example.com"))
    p = microsoft.MicrosoftProvider(cfg, SimpleNamespace(m365_client_id="id"),
                                    transport=httpx.MockTransport(handler))
    monkeypatch.setattr(p, "_token", AsyncMock(return_value="t"))
    headers = unsubscribe.headers("harvey@x.example.com", "ABCDEFGHIJ")
    result = await p.send_email("ana@exemplu.example.com", "hello", "Body.", headers=headers)
    assert result.ok and result.message_id == "<d1@x>"
    create = next(c for c in calls if c.method == "POST" and c.url.path == "/v1.0/me/messages")
    assert create.headers["content-type"] == "text/plain"
    mime = email.message_from_bytes(base64.b64decode(create.content), policy=email.policy.default)
    assert mime["List-Unsubscribe"] == headers["List-Unsubscribe"]
    assert mime["To"] == "ana@exemplu.example.com" and "Body." in mime.get_content()
    # Without headers: the JSON draft, as before.
    calls.clear()
    await p.send_email("ana@exemplu.example.com", "hello", "Body.")
    create = next(c for c in calls if c.method == "POST" and c.url.path == "/v1.0/me/messages")
    assert json.loads(create.content)["subject"] == "hello"


# ── The token, matched first ──

@pytest.mark.asyncio
async def test_an_empty_body_mailto_unsubscribe_is_honoured(state):
    pid, first, follow, token = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u1", from_email="office@exemplu.example.com",
                                       subject=f"dezabonare {token}", body="")]
    handler = make_handler(state, provider)
    assert await handler._run_native() is True
    hit = await state.suppression_check("office@exemplu.example.com")
    assert hit.reason == "unsubscribe"
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"
    assert await state.is_reply_processed("u1")
    assert [i for i in await state.get_outbox(limit=10) if i["kind"] == "reply"] == []


@pytest.mark.asyncio
async def test_an_unsubscribe_from_another_address_bars_both(state):
    pid, _, _, token = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u2", from_email="ana@personal.example.com",
                                       subject="Unsubscribe", body=f"dezabonare {token}")]
    await make_handler(state, provider)._run_native()
    for address in ("office@exemplu.example.com", "ana@personal.example.com"):
        assert (await state.suppression_check(address)).reason == "unsubscribe"


@pytest.mark.asyncio
async def test_an_unknown_token_falls_back_to_the_opt_out_words(state):
    pid, _, follow, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u3", from_email="office@exemplu.example.com",
                                       subject="dezabonare ZZZZZZZZZZ", body="")]
    await make_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "opt_out"
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_spam_folder_is_read_for_the_token_only(state):
    pid, _, _, token = await _sent_cold(state)
    other = await seed_prospect(state, email="office@altul.example.com", status="contacted")
    provider = FakeProvider()

    async def spam():
        return [InboundMessage(provider_id="s1", from_email="office@exemplu.example.com",
                               subject=f"dezabonare {token}", folder="spam"),
                InboundMessage(provider_id="s2", from_email="office@altul.example.com",
                               subject="Interested!", body="Tell me more", folder="spam")]

    provider.get_spam_unsubscribes = spam
    assert await make_handler(state, provider)._run_native() is True
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.get_prospect(other)).status == "contacted"   # never read as a reply
    assert await state.get_conversations_by_status("open") == []


# ── Romanian opt-outs and objections ──

@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["Nu mă mai contactați, vă rog.", "nu ma mai contactati",
                                  "Dezabonare", "STOP", "Nu, mulțumesc"])
async def test_romanian_opt_out_words_are_honoured_without_the_model(state, body):
    pid, _, _, _ = await _sent_cold(state)
    colleague = await seed_prospect(state, email="vanzari@exemplu.example.com", status="queued")
    queued = await _queue(state, colleague, "vanzari@exemplu.example.com", campaign="c2")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r1", from_email="office@exemplu.example.com",
                                       subject="Re: hello", body=body)]
    handler = make_handler(state, provider, intent="interested")   # the model would say yes
    await handler._run_native()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "opt_out"
    # Every sequence to their firm stops now (the same email domain here).
    assert (await state.get_outbox_item(queued))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_objection_is_suppressed_as_one_and_handed_to_you(state):
    pid, _, _, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r2", from_email="office@exemplu.example.com",
                                       subject="Re: hello",
                                       body="De unde aveți adresa mea? Ștergeți-mi datele.")]
    await make_handler(state, provider, intent="question")._run_native()
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "objection"
    assert [c.prospect_id for c in await state.get_conversations_by_status("needs_human")] == [pid]


def test_the_romanian_opt_out_line_is_the_default_and_passes_the_validator():
    from harvey.config import DEFAULT_OPT_OUT_LINE, ComplianceConfig

    assert "Răspundeți „stop\"" in DEFAULT_OPT_OUT_LINE
    assert ComplianceConfig().opt_out_line == DEFAULT_OPT_OUT_LINE
    assert ComplianceConfig(opt_out_line="Răspundeți cu „dezabonare” și gata.").opt_out_line
    with pytest.raises(ValueError):
        ComplianceConfig(opt_out_line="Vă mulțumesc pentru timp.")


# ── Messages from strangers ──

@pytest.mark.asyncio
async def test_a_stranger_is_marked_processed_and_never_holds_sending(state):
    await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="n1", from_email="news@buletin.example.com",
                                       subject="Newsletter", body="This week's offers.")]
    handler = make_handler(state, provider)
    assert await handler._run_native() is True
    assert await state.is_reply_processed("n1")
    assert await state.get_conversations_by_status("needs_human") == []


@pytest.mark.asyncio
async def test_a_colleague_answering_the_thread_goes_to_you_and_stops_the_firm(state):
    pid, _, follow, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="c1", from_email="director@exemplu.example.com",
                                       subject="Re: hello", body="Who are you?",
                                       in_reply_to="<m1@x>")]
    assert await make_handler(state, provider)._run_native() is True
    handed = await state.get_conversations_by_status("needs_human")
    assert [c.prospect_id for c in handed] == [pid]
    assert "director@exemplu.example.com" in handed[0].thread[0].content
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_someone_answering_our_thread_and_asking_to_stop_is_suppressed(state):
    await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="n2", from_email="ion@forward.example.com",
                                       subject="Re: hello", body="Vă rog nu mai scrieți.",
                                       in_reply_to="<m1@x>")]
    await make_handler(state, provider)._run_native()
    assert (await state.suppression_check("ion@forward.example.com")).reason == "opt_out"


@pytest.mark.asyncio
async def test_a_strangers_newsletter_with_an_unsubscribe_link_bars_nobody(state):
    # Not about anyone Harvey wrote to: its "dezabonare" link is the
    # newsletter's own, not a request to Harvey.
    await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="n3", from_email="office@furnizor.example.com", subject="Noutăți luna aceasta",
        body="Ofertele lunii.\n\nPentru dezabonare apăsați aici.")]
    assert await make_handler(state, provider)._run_native() is True
    assert await state.suppression_check("office@furnizor.example.com") is None
    assert await state.is_reply_processed("n3")


@pytest.mark.asyncio
async def test_a_colleagues_quote_of_a_newsletter_isnt_their_opt_out(state):
    # From a firm Harvey wrote to, but the "dezabonare" is in what they
    # quoted, not in their own words: handed to you, not suppressed.
    pid, _, _, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="n4", from_email="director@exemplu.example.com", subject="Fwd: noutăți",
        body="Ne puteți suna mâine?\n\nOn Mon, 2 Nov 2026, buletin@furnizor.example.com wrote:\n"
             "> Pentru dezabonare apăsați aici.")]
    await make_handler(state, provider)._run_native()
    assert await state.suppression_check("director@exemplu.example.com") is None
    assert [c.prospect_id for c in await state.get_conversations_by_status("needs_human")] == [pid]


# ── Reads that cover the time Harvey was off ──

class CoverageProvider(FakeProvider):
    """Reads back only to ``reach`` (unix seconds), whatever it is asked."""

    def __init__(self, reach):
        super().__init__()
        self.reach = reach
        self.asked = []

    async def get_replies(self, limit=50):
        self.asked.append(self.read_since)
        self.covered_from = self.reach
        return self.inbound


@pytest.mark.asyncio
async def test_a_read_that_misses_the_gap_holds_cold_email_until_one_covers_it(state):
    mailbox = "fake:harvey@x.co"
    ten_days = time.time() - 10 * 86400
    await state.set_setting("mail_read_at:" + mailbox, str(int(ten_days)))
    provider = CoverageProvider(reach=time.time() - 7 * 86400)
    handler = make_handler(state, provider)
    assert await handler._run_native() is True
    assert provider.asked == [int(ten_days)]
    assert json.loads(await state.get_setting(READ_GAP_KEY))["since"] == int(ten_days)
    assert await state.last_mail_read(mailbox) == int(ten_days)      # not moved on

    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    answered = await seed_prospect(state, email="office@altul.example.com", status="replied")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    await _queue(state, answered, "office@altul.example.com", kind="reply")
    sender_mail = FakeProvider()
    sender = make_sender(state, sender_mail, require_approval=False)
    await sender._drain_due()
    assert [m["to"] for m in sender_mail.sent] == ["office@altul.example.com"]
    assert (await state.get_outbox_item(cold))["status"] == "approved"

    # A read that reaches back before the last complete one closes the gap.
    provider.reach = ten_days - 86400
    assert await handler._run_native() is True
    assert not await state.get_setting(READ_GAP_KEY)
    assert await state.last_mail_read(mailbox) > ten_days
    await sender._drain_due()
    assert (await state.get_outbox_item(cold))["status"] == "sent"


class FakeImap:
    """imaplib.IMAP4_SSL as a list of messages: records what was asked
    (("select", folder, readonly), or (command, args) of a UID command)."""
    log: list = []
    inbox = []

    def __init__(self, host, port):
        self.selected = ""

    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder, readonly=False):
        FakeImap.log.append(("select", folder, readonly))
        self.selected = folder
        return "OK", [b"1"]

    def response(self, name):
        return "OK", [b"7"]

    def uid(self, command, *args):
        FakeImap.log.append((command, args))
        if command == "SEARCH":
            return "OK", [" ".join(str(i + 1) for i in range(len(self.inbox))).encode()]
        uid = int(args[0])
        return "OK", [(b"1 (BODY[] {1})", self.inbox[uid - 1])]

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Junk) "/" "Junk"']

    def logout(self):
        return "BYE", []


def _raw(n, subject="Re: hello", body="Thanks"):
    return (f"From: ana{n}@exemplu.example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: <r{n}@exemplu.example.com>\r\n\r\n{body}\r\n").encode()


@pytest.mark.asyncio
async def test_imap_reads_by_date_without_marking_anything_seen(monkeypatch):
    import imaplib

    from harvey.integrations.smtp_mail import SmtpImapProvider

    FakeImap.log = []
    FakeImap.inbox = [_raw(1), _raw(2)]
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    cfg = SimpleNamespace(persona=SimpleNamespace(name="Harvey", email="harvey@x.example.com"))
    p = SmtpImapProvider(cfg, SimpleNamespace(smtp_host="mail.example.com",
                                              smtp_username="harvey", smtp_password="x"))
    p.read_since = time.time() - 12 * 86400
    found = await p.get_replies()
    assert [m.provider_id for m in found] == ["<r1@exemplu.example.com>",
                                              "<r2@exemplu.example.com>"]
    assert ("select", "INBOX", True) in FakeImap.log
    search = next(e[1] for e in FakeImap.log if e[0] == "SEARCH")
    assert search[1].startswith("SINCE ") and "UNSEEN" not in search[1]
    fetches = [e[1] for e in FakeImap.log if e[0] == "FETCH"]
    assert fetches and all(a[1] == "(BODY.PEEK[])" for a in fetches)
    assert p.covered_from <= p.read_since
    # Read again: nothing is downloaded twice.
    FakeImap.log = []
    await p.get_replies()
    assert [e for e in FakeImap.log if e[0] == "FETCH"] == []
    # The junk folder: searched for the unsubscribe word only.
    FakeImap.log = []
    FakeImap.inbox = [_raw(3, subject="dezabonare ABCDEFGHIJ", body="")]
    spam = await p.get_spam_unsubscribes()
    assert [m.folder for m in spam] == ["spam"]
    assert ("select", "Junk", True) in FakeImap.log
    assert 'SUBJECT "dezabonare"' in next(e[1] for e in FakeImap.log if e[0] == "SEARCH")[1]


@pytest.mark.asyncio
async def test_an_imap_failure_is_a_failed_read(monkeypatch):
    import imaplib

    from harvey.integrations.smtp_mail import SmtpImapProvider

    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    p = SmtpImapProvider(SimpleNamespace(persona=SimpleNamespace(name="", email="")),
                         SimpleNamespace(smtp_host="mail.example.com", smtp_username="h",
                                         smtp_password="x"))
    with pytest.raises(OSError):
        await p.get_replies()


# ── An opt-out that can't be recorded is never lost ──

@pytest.mark.asyncio
async def test_an_unsubscribe_whose_write_fails_is_read_again(state, monkeypatch):
    pid, _, follow, token = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u9", from_email="office@exemplu.example.com",
                                       subject=f"dezabonare {token}", body="")]
    handler = make_handler(state, provider)
    real = state.suppress

    async def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(state, "suppress", locked)
    assert await handler._run_native() is False          # sending waits meanwhile
    assert not await state.is_reply_processed("u9")
    monkeypatch.setattr(state, "suppress", real)
    assert await handler._run_native() is True           # next cycle
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "unsubscribe"
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"
    assert await state.is_reply_processed("u9")
    assert not await state.get_setting("stop_retry:u9")   # the count is forgotten


@pytest.mark.asyncio
async def test_an_opt_out_that_always_fails_is_handed_to_you_after_five_tries(state, monkeypatch):
    from harvey.agents.handler import STOP_RETRIES

    _, _, _, token = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u10", from_email="office@exemplu.example.com",
                                       subject=f"dezabonare {token}", body="")]
    handler = make_handler(state, provider)

    async def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(state, "suppress", locked)
    for _ in range(STOP_RETRIES - 1):
        await handler._run_native()
        assert not await state.is_reply_processed("u10")
    await handler._run_native()
    assert await state.is_reply_processed("u10")         # it can't hold sending forever
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT details_json FROM actions WHERE action_type = 'escalation'"
                              ) as cur:
            rows = [json.loads(r[0]) for r in await cur.fetchall()]
    assert any("couldn't be recorded" in r.get("reason", "") for r in rows)


@pytest.mark.asyncio
async def test_an_opt_out_reply_is_recorded_before_anything_else_can_fail(state, monkeypatch):
    pid = await seed_prospect(state, email="ana@exemplu.example.com", status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="o1", from_email="ana@exemplu.example.com",
                                       subject="Re: hello", body="Nu mai doresc, mulțumesc.")]
    handler = make_handler(state, provider)

    async def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(state, "suppression_check", locked)
    assert await handler._run_native() is False
    assert (await state.get_prospect(pid)).status == "opted_out"
    import sqlite3
    con = sqlite3.connect(state.db_path)
    assert con.execute("SELECT COUNT(*) FROM suppression WHERE value = ? AND undone_at IS NULL",
                       ("ana@exemplu.example.com",)).fetchone() == (1,)
    con.close()


# ── The one-click undo lifts only what Harvey read in their words ──

@pytest.mark.asyncio
async def test_the_one_click_undo_refuses_an_unsubscribe(state):
    from harvey import suppression

    pid, _, _, token = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u11", from_email="office@exemplu.example.com",
                                       subject=f"dezabonare {token}", body="")]
    await make_handler(state, provider)._run_native()
    with pytest.raises(suppression.Refused, match="do-not-contact list"):
        await state.undo_opt_out(pid, details={})
    assert (await state.get_prospect(pid)).status == "opted_out"     # nothing written
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "unsubscribe"


@pytest.mark.asyncio
async def test_the_one_click_undo_lifts_a_keyword_opt_out(state):
    pid, _, _, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="o2", from_email="office@exemplu.example.com",
                                       subject="Re: hello", body="Stop, mulțumesc")]
    await make_handler(state, provider)._run_native()
    assert (await state.suppression_check("office@exemplu.example.com")).reason == "opt_out"
    assert await state.undo_opt_out(pid, details={"reason": "a test"}) is True
    assert await state.suppression_check("office@exemplu.example.com") is None


# ── Romanian opt-outs: the common forms, and what isn't one ──

@pytest.mark.parametrize("text", [
    "Nu mai dorim să primim astfel de mesaje.",
    "Vă rog nu ne mai trimiteți emailuri.",
    "Stop, mulțumesc",
    "Nu mai vreau emailuri de la voi.",
    "Vă rog să mă scoateți din listă.",
    "Dezabonați-ne, vă rog.",
    "Nu, mulțumim.",
    "Nu doresc să mai primesc oferte.",
    "scoateti-ma din lista",
    "Stop\n\nTrimis de pe iPhone",
    "Nu mai doresc să primesc.",
    "Nu mai dorim să primim de la voi astfel de mesaje.",
    "Nu doresc să mai fiu contactat.",
    "Nu mai vrem oferte.",
])
def test_common_romanian_opt_outs_are_caught(text):
    from harvey.agents.handler import _opt_out_reason

    assert _opt_out_reason(text, text) is not None


@pytest.mark.parametrize("text", [
    "Nu mai doresc să aștept, când putem vorbi?",
    "Vă rog nu ștergeți datele din ofertă, le folosim.",
    "Furnizorul nu mai trimite facturi pe hârtie.",
    "Da, mulțumesc",
    "Nu mai dorim să primim facturi pe hârtie, aveți varianta electronică?",
    "Nu mai doresc oferta inițială, ci pachetul mare.",
    "Nu mai vrem reclamații de la clienți, de aceea ne interesează.",
])
def test_ordinary_replies_are_not_opt_outs(text):
    from harvey.agents.handler import _opt_out_reason

    assert _opt_out_reason(text, text) is None


def test_an_erasure_request_is_an_objection():
    from harvey.agents.handler import _opt_out_reason

    assert _opt_out_reason("Ștergeți-mi datele.", "Ștergeți-mi datele.")["objection"] is True


# ── The token and the subject ──

def test_a_token_left_percent_encoded_is_found():
    assert unsubscribe.find_token("dezabonare%20ABCDEFGHIJ") == "ABCDEFGHIJ"
    assert unsubscribe.find_token("DEZABONARE%20abcdefghij") == "ABCDEFGHIJ"


@pytest.mark.asyncio
async def test_a_stop_subject_with_a_signature_only_body_is_an_opt_out(state):
    pid, _, follow, _ = await _sent_cold(state)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="u12", from_email="office@exemplu.example.com",
                                       subject="dezabonare", body="Trimis de pe iPhone")]
    # The model would read the signature as a question.
    await make_handler(state, provider, intent="question")._run_native()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_spam_message_moved_to_the_inbox_is_read_as_a_reply(state):
    pid, _, follow, _ = await _sent_cold(state)
    provider = FakeProvider()
    msg = dict(provider_id="s9", from_email="office@exemplu.example.com",
               subject="Re: hello", body="Interesant, cât costă?")

    async def spam():
        return [InboundMessage(**msg, folder="spam")]

    provider.get_spam_unsubscribes = spam
    handler = make_handler(state, provider, intent="question")
    await handler._run_native()
    assert (await state.get_prospect(pid)).status == "contacted"     # spam: token only
    assert not await state.is_reply_processed("s9")

    async def none():
        return []

    provider.get_spam_unsubscribes = none
    provider.inbound = [InboundMessage(**msg)]                       # moved to the inbox
    await handler._run_native()
    assert (await state.get_prospect(pid)).status == "replied"
    assert (await state.get_outbox_item(follow))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_gmail_marks_spam_on_a_copy_not_its_cached_message():
    from harvey.integrations.gmail import GmailProvider

    gmail = GmailProvider(SimpleNamespace(persona=SimpleNamespace(name="", email="")),
                          SimpleNamespace())
    cached = InboundMessage(provider_id="g9", from_email="ana@exemplu.example.com",
                            subject="dezabonare ABCDEFGHIJ")
    gmail._read_week = AsyncMock(return_value=[cached])
    [found] = await gmail.get_spam_unsubscribes()
    assert found.folder == "spam" and cached.folder == ""


# ── The sending address of the unsubscribe header ──

@pytest.mark.asyncio
async def test_gmail_and_microsoft_name_the_signed_in_mailbox_with_no_persona_email():
    from harvey.integrations.gmail import GmailProvider
    from harvey.integrations.microsoft import MicrosoftProvider

    blank = SimpleNamespace(persona=SimpleNamespace(name="Harvey", email=""))
    gmail = GmailProvider(blank, SimpleNamespace())
    gmail._request = AsyncMock(return_value={"historyId": "7",
                                             "emailAddress": "Harvey@Trimite.example.com"})
    assert gmail.from_address() == ""
    assert await gmail.sender_address() == "harvey@trimite.example.com"
    assert gmail.from_address() == "harvey@trimite.example.com"

    ms = MicrosoftProvider.__new__(MicrosoftProvider)
    ms.config, ms._me = blank, ""

    async def me():
        ms._me = "harvey@trimite.example.com"
        return ms._me

    ms._my_address = me
    assert await ms.sender_address() == "harvey@trimite.example.com"
    assert ms.from_address() == "harvey@trimite.example.com"


@pytest.mark.asyncio
async def test_cold_email_waits_when_no_sending_address_is_known(state, monkeypatch):
    from harvey.state import NO_SENDER_ADDRESS
    from tests.test_outbox_native import Cfg

    monkeypatch.setattr(Cfg.persona, "email", "")
    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    answered = await seed_prospect(state, email="office@altul.example.com", status="replied")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    await _queue(state, answered, "office@altul.example.com", kind="reply")
    provider = HeaderProvider()
    await make_sender(state, provider, require_approval=False)._drain_due()
    assert [m["to"] for m in provider.sent] == ["office@altul.example.com"]
    item = await state.get_outbox_item(cold)
    assert (item["status"], item["error"]) == ("approved", NO_SENDER_ADDRESS)


# ── A mailbox too big to read back that far ──

@pytest.mark.asyncio
async def test_imap_reads_the_newest_messages_and_says_where_it_is_complete_from(monkeypatch):
    import imaplib

    from harvey.integrations import smtp_mail
    from harvey.integrations.smtp_mail import SmtpImapProvider

    def dated(n, day):
        return (f"From: ana{n}@exemplu.example.com\r\nSubject: Re: hello\r\n"
                f"Date: {day:02d} Oct 2026 10:00:00 +0000\r\n"
                f"Message-ID: <d{n}@exemplu.example.com>\r\n\r\nThanks\r\n").encode()

    FakeImap.log = []
    FakeImap.inbox = [dated(1, 1), dated(2, 5), dated(3, 9), dated(4, 12)]
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(smtp_mail, "MAX_MESSAGES", 2)
    cfg = SimpleNamespace(persona=SimpleNamespace(name="Harvey", email="harvey@x.example.com"))
    p = SmtpImapProvider(cfg, SimpleNamespace(smtp_host="mail.example.com",
                                              smtp_username="harvey", smtp_password="x"))
    p.read_since = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    found = await p.get_replies()                        # no longer a failed read
    assert [m.provider_id for m in found] == ["<d3@exemplu.example.com>",
                                              "<d4@exemplu.example.com>"]
    assert p.covered_from == datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()
    # The cache keeps only what the folder's search still finds.
    assert {k[2] for k in p._read_cache} == {b"3", b"4"}


@pytest.mark.asyncio
async def test_the_imap_cache_forgets_what_left_the_window(monkeypatch):
    import imaplib

    from harvey.integrations.smtp_mail import SmtpImapProvider

    FakeImap.inbox = [_raw(1), _raw(2), _raw(3)]
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    p = SmtpImapProvider(SimpleNamespace(persona=SimpleNamespace(name="", email="h@x.example.com")),
                         SimpleNamespace(smtp_host="mail.example.com", smtp_username="h",
                                         smtp_password="x"))
    await p.get_replies()
    assert len(p._read_cache) == 3
    FakeImap.inbox = [_raw(1)]                           # the others left the window
    await p.get_replies()
    assert {k[2] for k in p._read_cache} == {b"1"}


@pytest.mark.asyncio
async def test_a_gap_can_be_accepted_and_cold_email_goes_again(state):
    mailbox = "fake:harvey@x.co"
    ten_days = time.time() - 10 * 86400
    await state.set_setting("mail_read_at:" + mailbox, str(int(ten_days)))
    provider = CoverageProvider(reach=time.time() - 2 * 86400)   # a read cut short
    handler = make_handler(state, provider)
    assert await handler._run_native() is True
    assert (await state.read_gap())["mailbox"] == mailbox

    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await sender._drain_due()
    from harvey.state import READ_GAP_HELD
    assert (await state.get_outbox_item(cold))["error"] == READ_GAP_HELD

    assert await state.accept_read_gap("you") is True
    assert await state.read_gap() is None
    assert await state.last_mail_read(mailbox) > time.time() - 60
    assert await state.accept_read_gap("you") is False
    # The next read, cut short as before, still reaches back past it.
    assert await handler._run_native() is True
    assert await state.read_gap() is None
    await sender._drain_due()
    item = await state.get_outbox_item(cold)
    assert (item["status"], item["error"]) == ("sent", "")


@pytest.mark.modules_on("email_outreach")
def test_harvey_sending_accept_gap_needs_the_word(tmp_path, monkeypatch, capsys):
    import asyncio
    import builtins
    import sys

    from harvey import cli, state as state_module

    sm = StateManager(str(tmp_path / "harvey.db"))
    asyncio.run(sm.init_db())
    asyncio.run(sm.set_setting(READ_GAP_KEY, json.dumps(
        {"since": 1, "covered_from": 2, "reason": "a test gap", "mailbox": "fake:h@x.example.com"})))
    monkeypatch.setattr(state_module, "StateManager", lambda: sm)
    monkeypatch.setattr(sys, "argv", ["harvey", "sending", "accept-gap"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": "no")
    with pytest.raises(SystemExit):
        cli.main()
    assert asyncio.run(sm.read_gap()) is not None
    monkeypatch.setattr(builtins, "input", lambda prompt="": "accept")
    cli.main()
    assert asyncio.run(sm.read_gap()) is None
