"""Audiences and campaigns made by the user (harvey/campaigns.py), the
campaign cycle (agents/campaigner.py), and what sending and reading replies
do with them: every way into a campaign goes through the same rules, staging
only fills the review queue, every send check still refuses, and replies
land in their campaign. All data is fictional."""

import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey import campaigns, contacts
from harvey.agents import sender as sender_mod
from harvey.agents.campaigner import CampaignCycle
from harvey.config import HarveyConfig
from harvey.integrations.mail_provider import InboundMessage
from harvey.models import Company
from harvey.models.prospect import Prospect
from harvey.overlay import schema
from harvey.state import (
    CAMPAIGN_PAUSED, COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW, MANUAL_SEND_KEY,
    StateManager,
)
from tests.test_outbox_native import FakeProvider, make_handler

BASE = {
    "persona": {"name": "Ana", "company": "Exemplu", "role": "Vânzări",
                "email": "ana@trimite.example.com", "linkedin": "", "tone": "direct"},
    "product": {"name": "Programări", "description": "programări online", "pricing": "",
                "key_benefits": [], "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}
NOTE = ("Interes legitim: oferim programări online firmelor care au un site public. Adresa "
        "este publicată de firmă pentru contact comercial.")
SEQUENCE = [{"subject": "o întrebare", "body": "Bună ziua, cine răspunde la telefon? Mulțumesc.",
             "delay_days": 0},
            {"subject": "revin", "body": "Revin cu o idee scurtă. Merită?", "delay_days": 3}]


def config(provider="gmail", autopilot=False, **email):
    return HarveyConfig(**BASE, channels={"email": {
        "provider": provider, "require_approval": not autopilot, **email}})


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "harvey.db"))
        await sm.init_db()
        yield sm


async def contact(state, email, *, status="verified", trust="confirmed", firm=None,
                  name=None) -> str:
    """An email contact at a firm (made unless ``firm`` names one)."""
    domain = email.rpartition("@")[2]
    if firm is None:
        firm = await state.add_company(Company(name=name or domain.split(".")[0].title(),
                                               domain=domain, location="Brașov"))
    await state.add_contacts(firm, [contacts.Found(
        "email", email, status=status, source="website", trust=trust,
        evidence_url=f"https://{domain}/contact")])
    return firm


async def new_campaign(state, cfg=None, *, query="kind=email", rolling=False, name="Test",
                       start=True, review="inherit"):
    cfg = cfg or config()
    cid = await state.campaigns_call(
        campaigns.create_campaign, provider=cfg.channels.email.provider,
        address=cfg.persona.email, name=name, query=query, rolling=rolling,
        sequence=SEQUENCE, li_note=NOTE, review=review)
    counts = await campaigns.start(state, cfg, cid) if start else None
    return cid, counts


class StubWriter:
    """The Writer, without a model: records whom it was asked to write for."""
    is_native = True

    def __init__(self):
        self.asked = []

    async def write_for_members(self, campaign, prospects):
        self.asked += [p.email for p in prospects]
        return 0


def make_sender(state, cfg, monkeypatch, provider=None):
    provider = provider or FakeProvider()
    monkeypatch.setattr(sender_mod, "get_mail_provider", lambda c, e: provider)
    s = sender_mod.Sender(brain=None, state=state, config=cfg,
                          env=SimpleNamespace())
    s.send_pacing = False
    return s, provider


def cycle(state, cfg, sender, writer=None):
    return CampaignCycle(state, cfg, SimpleNamespace(), writer or StubWriter(), sender)


async def member_status(state, campaign_id, email):
    m = await state.campaign_member(campaign_id, email)
    return m and m["status_name"]


# ── Schema 27 and the campaign on file ──

@pytest.mark.asyncio
async def test_a_draft_on_file_becomes_an_auto_draft_with_members_and_sends_nothing(
        tmp_path, monkeypatch):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path)
    schema.create(con, version=26)
    for i, (email, status) in enumerate([("unu@alfa.example.com", "verified"),
                                         ("doi@beta.example.com", "verified"),
                                         ("trei@gama.example.com", "risky"),
                                         ("patru@delta.example.com", "guess")]):
        con.execute("INSERT INTO prospects (id, email, email_status, status) VALUES (?, ?, ?, ?)",
                    (f"p{i}", email, status, "queued"))
    con.execute("INSERT INTO campaigns (id, name, sequence_json, prospect_ids_json, status) "
                "VALUES ('c1', 'auto', ?, ?, 'draft')",
                ('[{"step": 1, "subject": "s", "body": "b", "delay_days": 0}]',
                 '["p0", "p1", "p2", "p3"]'))
    con.commit()
    con.close()

    state = StateManager(str(path))
    await state.init_db()
    con = sqlite3.connect(path)
    assert schema.version(con) == schema.LATEST == 43 and schema.missing(con) == []
    con.close()
    c = await state.campaign_record("c1")
    assert (c["status"], c["origin"], c["account_id"]) == ("draft", "auto", 1)
    members = {m["email"]: m["status_name"] for m in await state.campaign_members("c1")}
    assert members == {"unu@alfa.example.com": "ready", "doi@beta.example.com": "ready",
                       "trei@gama.example.com": "ready", "patru@delta.example.com": "skipped"}
    # Opening it again changes nothing.
    await StateManager(str(path)).init_db()
    assert len(await state.campaign_members("c1")) == 4

    # Nothing is staged or sent from it, autopilot or not: it stays a draft.
    cfg = config(autopilot=True)
    sender, provider = make_sender(state, cfg, monkeypatch)
    await sender._run_native()
    await cycle(state, cfg, sender).run()
    assert await state.get_outbox(limit=10) == [] and provider.sent == []
    assert (await state.campaign_record("c1"))["status"] == "draft"


# ── Making and starting a campaign ──

@pytest.mark.asyncio
async def test_a_new_campaign_needs_a_native_provider(state):
    with pytest.raises(campaigns.Refused) as e:
        await new_campaign(state, config(provider="instantly"), start=False)
    assert e.value.need == "native_provider" and "Gmail, SMTP sau Microsoft" in str(e.value)
    # A draft made on Gmail doesn't start once the settings say Instantly.
    cid, _ = await new_campaign(state, start=False)
    with pytest.raises(campaigns.Refused) as e:
        await campaigns.start(state, config(provider="instantly"), cid)
    assert e.value.need == "native_provider"


@pytest.mark.asyncio
@pytest.mark.no_acknowledgement
async def test_start_needs_the_note_the_acknowledgement_and_a_sequence(state):
    cfg = config()
    cid = await state.campaigns_call(campaigns.create_campaign, provider="gmail",
                                     address="a@trimite.example.com", name="Fără notă",
                                     query="kind=email", li_note="prea scurtă")
    with pytest.raises(campaigns.Refused) as e:
        await campaigns.start(state, cfg, cid)
    assert e.value.need == "li_note"
    await state.campaigns_call(campaigns.update_campaign, cid, li_note=NOTE)
    with pytest.raises(campaigns.Refused) as e:
        await campaigns.start(state, cfg, cid)
    assert e.value.need == "acknowledgement"
    await state.acknowledge("cli")
    with pytest.raises(campaigns.Refused) as e:
        await campaigns.start(state, cfg, cid)
    assert e.value.need == "sequence"
    await state.campaigns_call(campaigns.update_campaign, cid, sequence=SEQUENCE)
    await campaigns.start(state, cfg, cid)
    assert (await state.campaign_record(cid))["status"] == "active"


# ── Every way in goes through every rule ──

@pytest.mark.asyncio
async def test_the_snapshot_takes_only_what_every_rule_allows_and_the_preview_counts_it(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")                                   # ready
    await contact(state, "office@beta.example.com", status="unchecked", trust="likely")  # verify
    await contact(state, "office@gama.example.com", status="risky")             # risky held
    await contact(state, "office@delta.example.com", status="invalid")           # left out
    await contact(state, "office@epsilon.example.com", trust="unconfirmed")      # untrusted
    await contact(state, "office@zeta.example.com")
    await state.suppress("address", "office@zeta.example.com", "by_hand", by="you")  # listed
    await contact(state, "office@iota.example.com")
    await contact(state, "office@kappa.example.com")
    # iota is in another active campaign's sequence; kappa got cold email lately.
    other, _ = await new_campaign(state, cfg, query="kind=email&q=iota", name="Alta")
    assert await member_status(state, other, "office@iota.example.com") == "ready"
    pid = await state.add_prospect(Prospect(email="office@kappa.example.com"))
    sent = await state.add_outbox_item(prospect_id=pid, to_email="office@kappa.example.com",
                                       subject="s", body="b", send_at=_now().isoformat(),
                                       campaign_id="old", step=1)
    await state.update_outbox_item(sent, status="sent",
                                   sent_at=(_now() - timedelta(days=30)).isoformat())

    preview = await campaigns.preview(state, cfg, query="kind=email")
    cid, counts = await new_campaign(state, cfg)
    for key in ("total", "ready", "to_verify", "risky_held", "left_out", "untrusted",
                "suppressed", "overlap"):
        assert preview[key] == counts[key], key
    assert (counts["ready"], counts["to_verify"], counts["risky_held"], counts["left_out"],
            counts["untrusted"], counts["suppressed"], counts["overlap"]) == (1, 1, 1, 1, 1, 1, 2)
    assert counts["added"] == 2 and preview["added"] == 0
    members = {m["email"]: m["status_name"] for m in await state.campaign_members(cid)}
    assert members == {"office@alfa.example.com": "ready", "office@beta.example.com": "waiting"}
    # Each member has its prospect card, linked to its firm.
    for m in await state.campaign_members(cid):
        p = await state.get_prospect(m["prospect_id"])
        assert p.email == m["email"] and p.company_id


@pytest.mark.asyncio
async def test_an_address_another_campaign_holds_is_skipped_and_counted(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    first, _ = await new_campaign(state, cfg, name="Prima")
    second, counts = await new_campaign(state, cfg, name="A doua")
    assert counts["overlap"] == 1 and counts["added"] == 0
    assert await state.campaign_member(second, "office@alfa.example.com") is None
    # Added by hand: the same rule.
    by_hand = await campaigns.add_by_hand(state, cfg, second, ["office@alfa.example.com",
                                                               "nimeni@nicaieri.example.com"])
    assert (by_hand["overlap"], by_hand["added"], by_hand["not_found"]) == (1, 0, 1)
    # Once the first one ends, the address is free (it was never written to).
    await state.campaigns_call(campaigns.set_status, first, "stop")
    assert (await campaigns.add_by_hand(state, cfg, second,
                                        ["office@alfa.example.com"]))["added"] == 1


@pytest.mark.asyncio
async def test_rolling_adds_new_matches_through_every_rule_and_caps_their_checks(
        state, monkeypatch):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    cid, _ = await new_campaign(state, cfg, rolling=True)
    c = await state.campaign_record(cid)
    # Not due within the hour.
    assert await campaigns.roll(state, cfg, c) is None
    for name in ("beta", "gama", "delta"):
        await contact(state, f"office@{name}.example.com", status="unchecked")
    await contact(state, "office@zeta.example.com", status="unchecked")
    await state.suppress("domain", "zeta.example.com", "by_hand", by="you")
    await contact(state, "office@iota.example.com", status="unchecked")
    other, _ = await new_campaign(state, cfg, query="kind=email&q=iota", name="Alta")

    c["rolled_at"] -= campaigns.ROLL_EVERY_SECONDS + 1
    counts = await campaigns.roll(state, cfg, c)
    # Only the contacts that changed are read: alfa, a member already, isn't.
    assert (counts["added"], counts["suppressed"], counts["overlap"], counts["already"],
            counts["whole"]) == (3, 1, 1, 0, False)
    rolled = [m for m in await state.campaign_members(cid) if m["added_by"] == 2]
    assert {m["status_name"] for m in rolled} == {"waiting"} and len(rolled) == 3

    # Their checks are capped per campaign and day.
    from harvey.agents import campaigner
    from harvey.modules import switches

    checked = []

    async def check(self, row, enabled):
        checked.append(row["email"])
        return "verified"

    async def enabled_ids(state_):
        return frozenset({"email_verify"})
    monkeypatch.setattr(campaigner.Audience, "_check", check)
    monkeypatch.setattr(switches, "enabled_ids", enabled_ids)
    monkeypatch.setattr(campaigns, "ROLL_CHECKS_PER_DAY", 2)
    sender, _ = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    await cycle(state, cfg, sender).run()
    rolled = {m["email"]: m["status_name"] for m in await state.campaign_members(cid)
              if m["added_by"] == 2}
    assert len([e for e in checked if e in rolled]) == 2
    assert sorted(rolled.values()) == ["staged", "staged", "waiting"]


# ── Writing, staging and the queue ──

@pytest.mark.asyncio
async def test_the_writer_only_writes_for_addresses_that_can_be_sent(state, monkeypatch):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@beta.example.com")
    cid, _ = await new_campaign(state, cfg)
    # Listed after it joined: no model call is spent on it, and it is never staged.
    await state.suppress("address", "office@beta.example.com", "by_hand", by="you")
    writer = StubWriter()
    sender, _ = make_sender(state, cfg, monkeypatch)
    assert await cycle(state, cfg, sender, writer).run()
    assert writer.asked == ["office@alfa.example.com"]
    assert await member_status(state, cid, "office@beta.example.com") == "suppressed"
    assert {r["to_email"] for r in await state.get_outbox(limit=10)} == {"office@alfa.example.com"}


@pytest.mark.asyncio
async def test_a_paused_queue_promotes_nothing_on_resume_without_review(state, monkeypatch):
    cfg = config(autopilot=True)
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@beta.example.com")
    cid, _ = await new_campaign(state, cfg)
    await state.set_setting(sender_mod.KILL_SWITCH_KEY, "paused by you")
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()       # staging goes on while paused...
    await sender._run_native()                  # ...and the drain doesn't
    rows = await state.get_outbox(limit=10)
    assert len(rows) == 4 and {(r["status"], r["approved_by"]) for r in rows} == {
        ("pending_review", "")}
    assert provider.sent == []

    # Resumed with autopilot off: nothing is approved, nothing goes.
    await state.delete_setting(sender_mod.KILL_SWITCH_KEY)
    manual, provider = make_sender(state, config(), monkeypatch)
    await manual._run_native()
    assert provider.sent == [] and await state.get_outbox(status="approved") == []

    # Autopilot on: approved at send time, through its review. A planted link
    # waits for a human; the clean one goes.
    beta = next(r for r in rows if r["to_email"] == "office@beta.example.com" and r["step"] == 1)
    await state.update_outbox_item(beta["id"], body=beta["body"] + " https://altundeva.example.org/x")
    sender, provider = make_sender(state, cfg, monkeypatch)
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["office@alfa.example.com"]
    held = await state.get_outbox_item(beta["id"])
    assert held["status"] == "pending_review" and held["error"].startswith(HELD_FOR_REVIEW)
    assert await member_status(state, cid, "office@alfa.example.com") == "in_sequence"


@pytest.mark.asyncio
async def test_a_campaign_that_reviews_everything_is_never_promoted(state, monkeypatch):
    cfg = config(autopilot=True)
    await contact(state, "office@alfa.example.com")
    await new_campaign(state, cfg, review="always")
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    await sender._run_native()
    assert provider.sent == [] and await state.get_outbox(status="approved") == []


async def _approved_first_email(state, cfg, monkeypatch, email="office@alfa.example.com"):
    """An active campaign with one member whose first email is approved."""
    firm = await contact(state, email)
    cid, _ = await new_campaign(state, cfg, query=f"kind=email&q={email.split('@')[1][:4]}")
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    [item] = [r for r in await state.get_outbox(limit=10) if r["step"] == 1]
    await state.approve_outbox(item["id"])
    return cid, firm, item, sender, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["paused", "stopped", "removed", "replied", "hand_sent",
                                   "other_campaign_sent", "colleague", "suppressed"])
async def test_every_send_check_still_refuses(state, monkeypatch, check):
    cfg = config()
    cid, firm, item, sender, provider = await _approved_first_email(state, cfg, monkeypatch)
    email, pid = item["to_email"], item["prospect_id"]
    if check == "paused":
        await state.campaigns_call(campaigns.set_status, cid, "pause")
        expect = ("approved", CAMPAIGN_PAUSED)
    elif check == "stopped":
        # Stopped after the drain read it: _campaign_hold catches it.
        await state.campaigns_call(campaigns._touch, cid, status="finished")
        expect = ("cancelled", "campaign finished")
    elif check == "removed":
        await state.set_campaign_member(cid, email, "removed", "taken out by you")
        expect = ("cancelled", "member removed: taken out by you")
    elif check == "replied":
        await state.update_prospect_status(pid, "replied")
        expect = ("cancelled", "prospect status 'replied'")
    elif check == "hand_sent":
        await state.set_setting(MANUAL_SEND_KEY + pid, "queued")
        expect = ("cancelled", HAND_SENT_REASON)
    elif check == "other_campaign_sent":
        other = await state.add_outbox_item(prospect_id=pid, to_email=email, subject="s",
                                            body="b", send_at=_now().isoformat(),
                                            campaign_id="alta", step=1)
        await state.update_outbox_item(other, status="sent", sent_at=_now().isoformat())
        expect = ("cancelled", "already contacted (cold email sent")
    elif check == "colleague":
        await contact(state, "director@alfa.example.com", firm=firm)
        colleague = await state.add_prospect(Prospect(
            company_id=firm, first_name="Dan", email="director@alfa.example.com"))
        await state.stop_company(colleague)
        expect = ("cancelled", COLLEAGUE_REPLIED)
    else:
        await state.suppress("address", email, "by_hand", by="you")
        expect = ("failed", "gate:")
    await sender._drain_due()
    row = await state.get_outbox_item(item["id"])
    assert provider.sent == []
    assert row["status"] == expect[0] and (row["error"] or "").startswith(expect[1]), row["error"]


@pytest.mark.asyncio
async def test_a_hand_sent_prospect_cancels_what_was_staged_for_them(state, monkeypatch):
    cfg = config()
    cid, _, item, sender, provider = await _approved_first_email(state, cfg, monkeypatch)
    await state.set_setting(MANUAL_SEND_KEY + item["prospect_id"], "queued")
    # Staging again doesn't bring them back either.
    await state.set_campaign_member(cid, item["to_email"], "ready")
    await sender._stage_campaign_native(
        next(c for c in await state.get_campaigns_by_status("active") if c.id == cid))
    assert await member_status(state, cid, item["to_email"]) == "stopped"
    await sender._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item["id"]))["error"] == HAND_SENT_REASON
    # Undoing the hand send brings both back, for review.
    await state.delete_setting(MANUAL_SEND_KEY + item["prospect_id"])
    assert await state.restore_hand_cancelled_outbox(item["prospect_id"], HAND_SENT_REASON) == 1
    assert await member_status(state, cid, item["to_email"]) == "staged"
    assert (await state.get_outbox_item(item["id"]))["status"] == "pending_review"


# ── Replies ──

async def _sent(state, item, message_id):
    await state.update_outbox_item(item["id"], status="sent", sent_at=_now().isoformat(),
                                   message_id=message_id)


@pytest.mark.asyncio
async def test_a_reply_stops_every_campaign_at_the_same_firm(state, monkeypatch):
    cfg = config()
    firm = await contact(state, "office@alfa.example.com")
    await contact(state, "vanzari@alfa.example.com", firm=firm)
    first, _ = await new_campaign(state, cfg, query="kind=email&q=office", name="Unu")
    second, _ = await new_campaign(state, cfg, query="kind=email&q=vanzari", name="Doi")
    sender, _ = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    rows = await state.get_outbox(limit=10)
    office = next(r for r in rows if r["to_email"] == "office@alfa.example.com" and r["step"] == 1)
    await _sent(state, office, "<m1@trimite.example.com>")

    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="r1", from_email="office@alfa.example.com",
                                       subject="Re: o întrebare", body="Cât costă?",
                                       message_id="<r1@alfa.example.com>",
                                       in_reply_to="<m1@trimite.example.com>")]
    await make_handler(state, provider)._run_native()
    assert await member_status(state, first, "office@alfa.example.com") == "replied"
    assert await member_status(state, second, "vanzari@alfa.example.com") == "stopped"
    theirs = [r for r in await state.get_outbox(limit=10)
              if r["to_email"] == "vanzari@alfa.example.com"]
    assert theirs and {r["status"] for r in theirs} == {"cancelled"}
    assert all(r["error"].startswith(COLLEAGUE_REPLIED) for r in theirs)


@pytest.mark.asyncio
@pytest.mark.parametrize("threaded", [True, False])
async def test_a_reply_is_attributed_to_its_campaign(state, monkeypatch, threaded):
    cfg = config()
    cid, _, item, sender, _ = await _approved_first_email(state, cfg, monkeypatch)
    await _sent(state, item, "<m1@trimite.example.com>")
    provider = FakeProvider()
    # Without In-Reply-To (a provider that kept its own id): the last cold
    # email to that address.
    provider.inbound = [InboundMessage(
        provider_id="r1", from_email=item["to_email"], subject="Re: o întrebare",
        body="Cât costă?", in_reply_to="<m1@trimite.example.com>" if threaded else "")]
    await make_handler(state, provider)._run_native()
    convos = await state.get_conversations_by_status("open")
    assert [c.campaign_id for c in convos] == [cid]
    assert next(x for x in await state.campaigns_call(campaigns.list_campaigns, write=False)
                if x["id"] == cid)["replies"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [("List-Unsubscribe", "<mailto:list@alfa.example.com>"),
                                    ("List-Id", "<stiri.alfa.example.com>"),
                                    ("Precedence", "bulk")])
async def test_bulk_mail_that_doesnt_answer_us_suppresses_nothing(state, monkeypatch, header):
    cfg = config()
    cid, _, item, _, _ = await _approved_first_email(state, cfg, monkeypatch)
    await _sent(state, item, "<m1@trimite.example.com>")
    email = item["to_email"]
    # The firm's newsletter, from an address Harvey never wrote to.
    news = "noutati@alfa.example.com"
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="n1", from_email=news, subject="Noutăți: dezabonare oricând",
        body="Noutățile lunii. Pentru dezabonare scrieți stop.",
        headers={header[0].lower(): header[1]})]
    handler = make_handler(state, provider)
    assert await handler._run_native() is True
    assert await state.suppression_check(news) is None
    assert await state.suppression_check(email) is None
    assert (await state.get_prospect(item["prospect_id"])).status != "opted_out"
    assert await state.get_conversations_by_status("open") == []
    assert await state.is_reply_processed("n1")
    assert await member_status(state, cid, email) == "staged"

    # The same headers on a message that answers our email: their words count.
    provider.inbound = [InboundMessage(
        provider_id="n2", from_email=email, subject="Re: o întrebare", body="stop",
        in_reply_to="<m1@trimite.example.com>", headers={header[0].lower(): header[1]})]
    await make_handler(state, provider)._run_native()
    assert await state.suppression_check(email) is not None
    assert await member_status(state, cid, email) == "suppressed"


@pytest.mark.asyncio
async def test_bulk_headers_on_mail_from_someone_we_wrote_to_still_count(state, monkeypatch):
    """A reply whose thread headers don't name what was stored (a provider's
    own message id) and that carries a list header: it comes from an address
    Harvey sent cold email to, so its opt-out counts."""
    cfg = config()
    cid, _, item, _, _ = await _approved_first_email(state, cfg, monkeypatch)
    await _sent(state, item, "<m1@trimite.example.com>")
    email = item["to_email"]
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email=email, subject="Re: o întrebare", body="stop",
        in_reply_to="<id-al-furnizorului@mail.example.com>",
        headers={"list-unsubscribe": "<mailto:list@alfa.example.com>"})]
    await make_handler(state, provider)._run_native()
    assert await state.suppression_check(email) is not None
    assert await member_status(state, cid, email) == "suppressed"


# ── Audiences ──

@pytest.mark.asyncio
async def test_an_audience_keeps_the_page_filters_and_reads_them_like_the_page(state):
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@beta.example.com")
    aid = await state.campaigns_call(
        campaigns.save_audience, "Alfa", "kind=phone&q=alfa&limit=50&offset=100&sort=value")
    audience = await state.campaigns_call(campaigns.get_audience, aid, write=False)
    assert audience["query"] == "kind=email&q=alfa"
    from harvey.contact_query import filter_from_query
    f, _ = await filter_from_query(state, audience["query"])
    page = await state.read("contacts_listing", f, "", "desc", 50, 0, False)
    rows = await campaigns.candidate_rows(state, query=audience["query"])
    assert [r["value"] for r in page["rows"]] == [r["email"] for r in rows] == [
        "office@alfa.example.com"]
    with pytest.raises(campaigns.Refused):
        await state.campaigns_call(campaigns.save_audience, " ", "kind=email")


# ── The CLI ──

@pytest.mark.modules_on("email_outreach")
def test_harvey_audiences_and_campaigns(tmp_path, monkeypatch, capsys):
    import asyncio

    from harvey import cli
    from harvey import config as config_mod
    from harvey import state as state_mod

    path = str(tmp_path / "harvey.db")
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    cfg = config()
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)

    async def seed():
        s = StateManager(path)
        await s.init_db()
        await contact(s, "office@alfa.example.com")
    asyncio.run(seed())

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["harvey", *argv])
        cli.main()
        return capsys.readouterr().out

    assert "Saved audience 1: kind=email&q=alfa" in run("audiences", "save", "Alfa", "--q", "alfa")
    assert "1 ready" in run("audiences", "show", "1")
    sequence = tmp_path / "sequence.json"
    sequence.write_text(json.dumps(SEQUENCE), encoding="utf-8")
    out = run("campaigns", "new", "--name", "Alfa", "--audience", "1", "--note", NOTE,
              "--sequence-file", str(sequence))
    cid = out.split("Draft campaign ")[1].split()[0]
    assert "1 member(s) taken" in run("campaigns", "start", cid)
    assert "office@alfa.example.com" in run("campaigns", "members", cid)
    assert "paused" in run("campaigns", "pause", cid)
    assert "Alfa" in run("campaigns", "list")
    cfg.channels.email.provider = "instantly"
    with pytest.raises(SystemExit):
        run("campaigns", "new", "--name", "X", "--note", NOTE)
    assert "Gmail, SMTP sau Microsoft" in capsys.readouterr().out


# ── The heartbeat ──

@pytest.mark.asyncio
async def test_the_heartbeat_works_the_campaigns_whatever_else_is_waiting(state, monkeypatch):
    """An open conversation and paused sending used to starve writing (one
    action a cycle). Now each cycle reads, drains, then works the campaigns."""
    import asyncio

    import harvey.agents.campaigner as campaigner_mod
    import harvey.main as main_mod
    from harvey.models.conversation import Conversation
    from tests.test_send_window import _patch_heartbeat

    from harvey.modules import switches

    await switches.set_enabled(state, switches.EMAIL_MODULE, True)
    await state.add_conversation(Conversation(id="", prospect_id="p", status="open"))
    await state.set_setting(sender_mod.KILL_SWITCH_KEY, "paused by you")
    calls, stop = [], asyncio.Event()
    _patch_heartbeat(monkeypatch, config(), state, calls, stop)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c, at=None: False)

    class Cycle:
        def __init__(self, *a):
            pass

        async def run(self):
            calls.append("campaigns")
            return True
    monkeypatch.setattr(campaigner_mod, "CampaignCycle", Cycle)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == ["replies", "send", "campaigns"]


# ── The queue between campaigns, overlap, the note, the members' ends ──

@pytest.mark.asyncio
async def test_a_paused_campaigns_approved_email_doesnt_hold_up_another(state, monkeypatch):
    """A paused campaign's rows stay approved; due first, they used to fill
    every page the drain read, and nothing else went out."""
    cfg = config()
    monkeypatch.setattr(sender_mod, "MAX_SENDS_PER_CYCLE", 1)    # pages of 3
    for i in range(4):
        await contact(state, f"office@alfa{i}.example.com")
    a, _ = await new_campaign(state, cfg, query="kind=email&q=alfa", name="A")
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    await state.approve_outbox()
    await state.campaigns_call(campaigns.set_status, a, "pause")
    await contact(state, "office@beta.example.com")
    b, _ = await new_campaign(state, cfg, query="kind=email&q=beta", name="B")
    await cycle(state, cfg, sender).run()
    [row] = [r for r in await state.get_outbox(limit=50) if r["campaign_id"] == b
             and r["step"] == 1]
    await state.approve_outbox(row["id"])
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["office@beta.example.com"]
    held = [r for r in await state.get_outbox(limit=50) if r["campaign_id"] == a
            and r["step"] == 1]
    assert len(held) == 4 and {r["status"] for r in held} == {"approved"}


@pytest.mark.asyncio
async def test_writing_waits_while_two_days_of_first_emails_are_queued(state, monkeypatch):
    """Each draft is a model call: the cycle writes only while the queue of
    unsent first emails is under two days of the cold cap, and at most
    WRITE_PER_CYCLE across every campaign."""
    cfg = config(max_daily_sends=2)
    for i in range(6):
        await contact(state, f"office@alfa{i}.example.com")
    await new_campaign(state, cfg, query="kind=email&q=alfa")
    writer = StubWriter()
    sender, _ = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender, writer).run()
    assert len(writer.asked) == 4
    await cycle(state, cfg, sender, writer).run()
    assert len(writer.asked) == 4                     # the queue holds two days already
    assert await state.count_queued_first_emails() == 4

    cfg = config()
    for name in ("beta", "gama"):
        for i in range(6):
            await contact(state, f"office@{name}{i}.example.com")
        await new_campaign(state, cfg, query=f"kind=email&q={name}", name=name)
    writer = StubWriter()
    await cycle(state, cfg, sender, writer).run()
    assert len(writer.asked) == 8                     # across both campaigns


@pytest.mark.asyncio
async def test_a_prospect_contacted_with_no_cold_email_on_file_counts_as_overlap(
        state, monkeypatch):
    """Contacted outside the outbox (an earlier release's Instantly, or by hand):
    left out and counted, and a first email staged before is cancelled."""
    cfg = config()
    firm = await contact(state, "office@alfa.example.com")
    pid = await state.add_prospect(Prospect(company_id=firm, first_name="Ana",
                                            email="office@alfa.example.com",
                                            email_status="verified", email_verified=True))
    await state.update_prospect_status(pid, "contacted")
    preview = await campaigns.preview(state, cfg, query="kind=email")
    cid, counts = await new_campaign(state, cfg)
    assert preview["overlap"] == counts["overlap"] == 1 and counts["added"] == 0
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    await state.approve_outbox()
    await sender._drain_due()
    assert provider.sent == [] and await state.campaign_member(cid, "office@alfa.example.com") \
        is None

    # Already a member when it was marked contacted: its first email is cancelled.
    cid2, _, item, sender, provider = await _approved_first_email(
        state, cfg, monkeypatch, email="office@beta.example.com")
    await state.update_prospect_status(item["prospect_id"], "contacted")
    await sender._drain_due()
    row = await state.get_outbox_item(item["id"])
    assert provider.sent == [] and row["status"] == "cancelled"
    assert campaigns.CONTACTED_ELSEWHERE in row["error"]


@pytest.mark.asyncio
async def test_a_started_campaigns_note_cant_be_changed(state):
    """The record shows the note the campaign started under: only a draft's
    note is written, whatever calls update_campaign (PATCH included)."""
    await contact(state, "office@alfa.example.com")
    draft, _ = await new_campaign(state, start=False, name="Ciornă")
    assert (await state.campaigns_call(campaigns.update_campaign, draft,
                                       li_note=""))["li_note"] == ""
    cid, _ = await new_campaign(state, name="Pornită")
    longer = NOTE + " Cine cere, primește lista surselor."
    for note in ("", "prea scurtă", longer):
        with pytest.raises(campaigns.Refused) as e:
            await state.campaigns_call(campaigns.update_campaign, cid, li_note=note)
        assert e.value.need == "li_note"
    assert (await state.campaign_record(cid))["li_note"] == NOTE
    # The same note (a save of the whole form) is no change, so it passes.
    assert (await state.campaigns_call(campaigns.update_campaign, cid, li_note=NOTE,
                                       name="Redenumită"))["name"] == "Redenumită"


@pytest.mark.asyncio
async def test_the_preview_leaves_out_a_firm_that_answered(state):
    cfg = config()
    firm = await contact(state, "office@alfa.example.com")
    await contact(state, "director@alfa.example.com", firm=firm)
    colleague = await state.add_prospect(Prospect(company_id=firm, first_name="Dan",
                                                  email="director@alfa.example.com"))
    await state.update_prospect_status(colleague, "replied")
    await state.stop_company(colleague)
    await contact(state, "office@beta.example.com")
    counts = await campaigns.preview(state, cfg, query="kind=email")
    assert (counts["answered"], counts["ready"]) == (2, 1)


@pytest.mark.asyncio
async def test_a_member_whose_email_the_gate_failed_is_ended(state, monkeypatch):
    """A failed email is never retried: its member stops, and the campaign
    can finish."""
    cfg = config()
    cid, _, item, sender, provider = await _approved_first_email(state, cfg, monkeypatch)
    await state.update_outbox_item(item["id"], body=item["body"] + " {{necompletat}}")
    await sender._drain_due()
    assert provider.sent == [] and (await state.get_outbox_item(item["id"]))["status"] == "failed"
    m = await state.campaign_member(cid, item["to_email"])
    assert m["status_name"] == "stopped" and m["end_reason"].startswith("gate:")
    await cycle(state, cfg, sender).run()
    assert (await state.campaign_record(cid))["status"] == "finished"


@pytest.mark.asyncio
async def test_a_campaign_whose_audience_matched_no_one_finishes(state, monkeypatch):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    cid, counts = await new_campaign(state, cfg, query="kind=email&q=nimeni")
    assert counts["added"] == 0
    rolling, _ = await new_campaign(state, cfg, query="kind=email&q=nimeni", rolling=True,
                                    name="Rulantă")
    sender, _ = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    assert (await state.campaign_record(cid))["status"] == "finished"
    assert (await state.campaign_record(rolling))["status"] == "active"


def test_a_migrated_member_past_its_last_step_is_done(tmp_path):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path)
    schema.create(con, version=26)
    for pid, email in (("p0", "unu@alfa.example.com"), ("p1", "doi@beta.example.com")):
        con.execute("INSERT INTO prospects (id, email, email_status, status) VALUES "
                    "(?, ?, 'verified', 'contacted')", (pid, email))
    con.execute("INSERT INTO campaigns (id, name, sequence_json, prospect_ids_json, status) "
                "VALUES ('c1', 'auto', ?, '[\"p0\", \"p1\"]', 'active')",
                ('[{"step": 1, "subject": "s", "body": "b", "delay_days": 0}, '
                 '{"step": 2, "subject": "s", "body": "b", "delay_days": 3}]',))
    rows = [("o1", "p0", 1, "unu@alfa.example.com", "sent"),
            ("o2", "p0", 2, "unu@alfa.example.com", "sent"),
            ("o3", "p1", 1, "doi@beta.example.com", "sent"),
            ("o4", "p1", 2, "doi@beta.example.com", "approved")]
    for oid, pid, step, email, status in rows:
        con.execute("INSERT INTO outbox (id, campaign_id, prospect_id, step, to_email, status, "
                    "sent_at) VALUES (?, 'c1', ?, ?, ?, ?, ?)",
                    (oid, pid, step, email, status,
                     _now().isoformat() if status == "sent" else None))
    con.commit()
    from harvey.overlay.schema import _to_27
    _to_27(con)
    members = {r[0]: r[1] for r in con.execute("SELECT email, status FROM campaign_member")}
    con.close()
    assert members == {"unu@alfa.example.com": schema.MEMBER_STATUS["done"],
                       "doi@beta.example.com": schema.MEMBER_STATUS["in_sequence"]}


@pytest.mark.asyncio
async def test_the_writers_campaign_members_are_marked_as_its_own(state):
    from harvey.models.campaign import Campaign, EmailStep

    pid = await state.add_prospect(Prospect(email="office@alfa.example.com",
                                            email_status="verified"))
    cid = await state.add_campaign(Campaign(
        id="", name="Scrisă de Harvey", status="draft", prospect_ids=[pid],
        sequence=[EmailStep(step=1, subject="s", body="b", delay_days=0)]))
    [m] = await state.campaign_members(cid)
    assert m["added_by"] == schema.MEMBER_ADDED_BY["writer"] != schema.MEMBER_ADDED_BY["by_hand"]


@pytest.mark.asyncio
async def test_writing_for_a_campaign_puts_the_writers_brain_back(state):
    from harvey.agents.writer import Writer

    class Profiled:
        def load_skills_for_agent(self, agent):
            return ""

    class Base(Profiled):
        def for_profile(self, profile_id=""):
            return Profiled()

    base = Base()
    writer = Writer(base, state, config())

    async def nothing(*a, **k):
        assert writer.brain is not base           # the profile's brain writes
        return 0
    writer._personalize_first_emails = nothing
    writer._write_sequence = nothing
    campaign = SimpleNamespace(id="c", offer_json="{}", profile_id="alt")
    await writer.write_for_members(campaign, [])
    assert writer.brain is base
    await writer.write_templates(campaign, [])
    assert writer.brain is base


@pytest.mark.asyncio
async def test_autopilot_approves_no_more_than_the_cold_cap_leaves(state, monkeypatch):
    cfg = config(autopilot=True, max_daily_sends=1)
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@beta.example.com")
    await new_campaign(state, cfg)
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    await sender._run_native()
    assert len(provider.sent) == 1
    firsts = [r for r in await state.get_outbox(limit=10) if r["step"] == 1]
    assert sorted((r["status"], r["approved_by"]) for r in firsts) == [
        ("pending_review", ""), ("sent", "autopilot")]


def test_every_reason_a_member_ends_with_reads_in_both_languages():
    """The members table shows why an address left in the page's language:
    each reason the server writes matches an entry, whose key both
    languages have."""
    import re
    from pathlib import Path

    from harvey import suppression

    web = Path(__file__).resolve().parent.parent / "harvey" / "web"
    app = (web / "app.js").read_text(encoding="utf-8")
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    block = app[app.index("const MEMBER_REASONS"):app.index("function memberReason")]
    entries = [(re.compile(rx.replace("\\\\", "\\")), key)
               for rx, key in re.findall(r"\[/(.+?)/, '([a-z_.]+)'\]", block)]
    assert entries
    hit = suppression.Hit("address", "office@alfa.example.com", "unsubscribe", 1)
    reasons = ["they replied", "the email bounced", "every step sent", "stopped by you",
               "taken out by you", HAND_SENT_REASON, "no trusted link any more",
               "no usable address", "unsubscribe", "opt_out", "objection",
               "email status 'invalid'", "prospect status 'replied'",
               f"{COLLEAGUE_REPLIED} (Dan)", "overlap: in campaign 'Alfa'",
               "overlap: cold email sent 2026-10-01", f"overlap: {campaigns.CONTACTED_ELSEWHERE}",
               f"already contacted ({campaigns.CONTACTED_ELSEWHERE})", "already contacted",
               "gate: unrendered merge tags", hit.text]
    for reason in reasons:
        keys = [key for rx, key in entries if rx.search(reason)]
        assert keys, reason
    for _, key in entries:
        assert i18n.count(f"'{key}':") == 2, key


def test_the_changelog_has_one_heading_of_each_kind_per_release():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    for release in re.split(r"^## ", text, flags=re.M)[1:]:
        headings = re.findall(r"^### (.+)$", release, flags=re.M)
        assert len(headings) == len(set(headings)), (release.splitlines()[0], headings)


# ── The campaign SQL: named, and by an index ──

def test_the_outbox_is_indexed_by_address_on_a_new_file_and_an_old_one(tmp_path):
    new = sqlite3.connect(":memory:")
    schema.create(new)
    old = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    schema.create(old, version=36)
    assert old.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_outbox_to_email'") \
        .fetchone() is None
    assert schema.step_up(old) == schema.LATEST == 43
    for con in (new, old):
        cols = [r[2] for r in con.execute("PRAGMA index_info('idx_outbox_to_email')")]
        assert cols == ["to_email", "kind", "status", "sent_at"]


def test_campaigns_runs_only_named_statements():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "harvey" / "campaigns.py").read_text(
        encoding="utf-8")
    assert len(re.findall(r"\b(?:SELECT|INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", text)) == 0
    assert text.count(".execute(") == 1           # the helper that runs a named one


@pytest.mark.asyncio
async def test_touch_writes_the_fields_it_was_given_and_keeps_the_rest(state):
    cid, _ = await new_campaign(state, start=False)
    before = await state.campaigns_call(campaigns.get_campaign, cid, write=False)
    await state.campaigns_call(campaigns.update_campaign, cid, query="kind=email&q=alfa")
    after = await state.campaigns_call(campaigns.get_campaign, cid, write=False)
    assert after["audience_id"] is None and after["audience_query"] == "kind=email&q=alfa"
    assert {k: after[k] for k in ("name", "status", "sequence", "li_note", "review")} == \
        {k: before[k] for k in ("name", "status", "sequence", "li_note", "review")}
    with pytest.raises(ValueError, match="not a campaign field"):
        await state.campaigns_call(campaigns._touch, cid, origin="auto")


@pytest.mark.asyncio
async def test_an_audience_is_renamed_or_archived_one_field_at_a_time(state):
    aid = await state.campaigns_call(campaigns.save_audience, "Brașov", "kind=email")
    assert not await state.campaigns_call(campaigns.update_audience, aid)
    assert await state.campaigns_call(campaigns.update_audience, aid, archived=True)
    a = await state.campaigns_call(campaigns.get_audience, aid, write=False)
    assert (a["name"], a["archived"]) == ("Brașov", 1)
    assert await state.campaigns_call(campaigns.update_audience, aid, name="Sibiu")
    a = await state.campaigns_call(campaigns.get_audience, aid, write=False)
    assert (a["name"], a["archived"]) == ("Sibiu", 1)
    assert [x["id"] for x in await state.campaigns_call(campaigns.audiences, write=False)] == []
    assert [x["id"] for x in await state.campaigns_call(campaigns.audiences, True,
                                                        write=False)] == [aid]


@pytest.mark.asyncio
async def test_list_campaigns_filters_by_status_and_counts_members(state):
    await contact(state, "office@alfa.example.com")
    active, _ = await new_campaign(state, name="Activă")
    draft, _ = await new_campaign(state, name="Ciornă", start=False)
    every = {c["id"]: c for c in await state.campaigns_call(campaigns.list_campaigns,
                                                            write=False)}
    assert set(every) == {active, draft} and every[active]["member_total"] == 1
    drafts = await state.campaigns_call(campaigns.list_campaigns, ["draft"], write=False)
    assert [c["id"] for c in drafts] == [draft]


@pytest.mark.asyncio
async def test_a_big_preview_counts_personal_addresses_over_its_first_ones(state, monkeypatch):
    cfg = config()
    for name in ("alfa", "beta", "gama"):
        await contact(state, f"office@{name}.example.com")
    delta = await contact(state, "office@delta.example.com")
    await contact(state, "exemplu.fictiv.0002@gmail.com", firm=delta)
    asked = []
    real = state.read

    async def read(method, *args, **kwargs):
        if method == "address_facts":
            asked.append(args[0])
        return await real(method, *args, **kwargs)

    monkeypatch.setattr(state, "read", read)
    exact = await campaigns.preview(state, cfg, query="kind=email")
    assert len(asked) == 5 and exact["personal_sample"] == 0
    assert sum(exact["personal"].values()) == 1
    asked.clear()
    monkeypatch.setattr(campaigns, "PREVIEW_PERSONAL", 2)
    sampled = await campaigns.preview(state, cfg, query="kind=email")
    assert len(asked) == 2 and sampled["personal_sample"] == 2
    # Every other number is the same, and taking them still decides each one.
    for key in ("total", "ready", "risky", "to_verify", "firms", "days_at_cap", "overlap"):
        assert sampled[key] == exact[key], key
    asked.clear()
    cid, counts = await new_campaign(state, cfg)
    assert counts["added"] == 5 and len(asked) == 5 and counts["personal_sample"] == 0
    assert {m["email"]: m["personal"] for m in await state.campaign_members(cid)}[
        "exemplu.fictiv.0002@gmail.com"] != 0
