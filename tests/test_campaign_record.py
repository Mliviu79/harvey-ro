"""The legitimate-interest pieces and the loose ends of campaigns: the
acknowledgement's endpoints and its place on the dashboard, the campaign's
record (evidență) as JSON and CSV, the AI disclosure line under answers
autopilot sent, the privacy page after a personal address's source line,
a rejected or cut-off email ending its member, an email approved while its
campaign is paused, autopilot answers approved only at send time, and the
schema step that gives old conversations their campaign. All data is
fictional."""

import asyncio
import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import harvey.dashboard as dash
from harvey import acknowledgement, campaigns
from harvey.agents import sender as sender_mod
from harvey.config import ComplianceConfig, DEFAULT_AI_DISCLOSURE_LINE
from harvey.integrations.mail_provider import InboundMessage
from harvey.overlay import schema
from harvey.state import (
    CAMPAIGN_NOT_STARTED, CAMPAIGN_PAUSED, HELD_FOR_REVIEW, PRIVACY_LINK_HELD,
    RESTORED_FOR_REVIEW, REVIEW_AFTER_UNDO, StateManager,
)
from tests.test_campaigns import (  # noqa: F401  (state is a fixture)
    NOTE, SEQUENCE, config, contact, cycle, make_sender, member_status, new_campaign, state,
)
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.test_outbox_native import Cfg, FakeProvider, make_handler, seed_prospect

ALFA, BETA = "office@alfa.example.com", "office@beta.example.com"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _staged(state, cfg, monkeypatch, *emails, rolling=False):
    """A started campaign over these addresses, staged by one cycle."""
    for email in emails:
        await contact(state, email)
    cid, _ = await new_campaign(state, cfg, rolling=rolling)
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    return cid, sender, provider


def _first(rows, email):
    return next(r for r in rows if r["to_email"] == email and r["step"] == 1)


# ── The acknowledgement ──

@pytest.mark.no_acknowledgement
def test_the_acknowledgement_is_read_then_confirmed_with_the_word(client):
    a = client.get("/api/acknowledgement").json()
    assert a["given"] is False and a["version"] == acknowledgement.VERSION
    assert a["confirm_word"] == "accept"
    # The source line is not all art. 14 asks for: the text says so.
    assert "art. 14" in a["text_ro"] and "nu acoperă tot" in a["text_ro"]
    assert "does not cover everything art. 14" in a["text_en"]
    for wrong in ({"typed": "acept"}, {"typed": ""}, {}, ["accept"]):
        assert client.post("/api/acknowledgement", json=wrong).status_code == 400
    assert client.get("/api/acknowledgement").json()["given"] is False
    done = client.post("/api/acknowledgement", json={"typed": " Accept "}).json()
    assert done["success"] and done["given"] and done["by"] == "dashboard" and done["at"]
    again = client.post("/api/acknowledgement", json={"typed": "accept"}).json()
    assert again["given"] and again["at"] == done["at"]       # kept as first given


@pytest.mark.no_acknowledgement
def test_today_sends_you_to_settings_to_confirm_it(client, monkeypatch):
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: config())

    async def seed():
        s = StateManager(str(client.db_path))
        await s.init_db()
        pid = await seed_prospect(s, email=ALFA)
        await s.add_outbox_item(prospect_id=pid, to_email=ALFA, subject="s", body="b",
                                send_at=_now().isoformat())
    asyncio.run(seed())
    [item] = [i for i in client.get("/api/today").json()["items"] if i["key"] == "ack"]
    assert item["tab"] == "settings" and "Settings → Sending" in item["detail"]


def test_the_dashboard_has_a_dialog_for_it_not_a_browser_prompt():
    from pathlib import Path

    web = Path(dash.__file__).resolve().parent / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert '<dialog class="modal" id="ack-dialog"' in html and 'id="ack-card"' in html
    assert "openAckDialog(cwStart)" in js and "prompt(t('cw.ack_prompt'))" not in js
    assert "/api/acknowledgement" in js


# ── The record ──

@pytest.mark.asyncio
@pytest.mark.no_acknowledgement
async def test_the_record_holds_the_note_the_acknowledgement_members_sends_and_opt_outs(
        state, monkeypatch):
    await state.acknowledge("cli")
    cfg = config()
    for email in (ALFA, BETA):
        await contact(state, email)
    cid, _ = await new_campaign(state, cfg, query="kind=email&q=alfa")
    assert (await campaigns.add_by_hand(state, cfg, cid, [BETA]))["added"] == 1
    sender, provider = make_sender(state, cfg, monkeypatch)
    await cycle(state, cfg, sender).run()
    rows = await state.get_outbox(limit=10)
    await state.approve_outbox(_first(rows, ALFA)["id"])
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == [ALFA]
    assert await state.reject_outbox_item(_first(rows, BETA)["id"]) == 1
    await state.suppress("address", ALFA, "unsubscribe", by="reply", campaign=cid)

    rec = await state.campaigns_call(campaigns.record, cid, write=False)
    assert rec["li_note"] == NOTE and rec["campaign"]["name"] == "Test"
    assert rec["campaign"]["audience_query"] == "kind=email&q=alfa"
    ack = rec["acknowledgement"]
    assert (ack["version"], ack["by"], ack["current"]) == (acknowledgement.VERSION, "cli", True)
    assert ack["at"][:4].isdigit()
    members = {m["email"]: m for m in rec["members"]}
    assert (members[ALFA]["added_by"], members[BETA]["added_by"]) == ("snapshot", "by_hand")
    assert (members[BETA]["status"], members[BETA]["end_reason"]) == ("stopped", "rejected")
    assert members[ALFA]["last_step"] == 1 and members[ALFA]["last_sent_at"]
    [sent] = rec["sends"]
    assert (sent["email"], sent["step"], sent["subject"]) == (ALFA, 1, "o întrebare")
    [opt] = rec["opt_outs"]
    assert (opt["email"], opt["reason"], opt["here"], opt["undone_at"]) == (
        ALFA, "unsubscribe", True, "")
    assert rec["counts"] == {"members": 2, "sends": 1, "opt_outs": 1}

    text = campaigns.record_csv(rec)
    table = list(csv.reader(io.StringIO(text)))
    assert ["campaign", "li_note", NOTE] in table
    assert ["acknowledgement", "version", str(acknowledgement.VERSION)] in table
    assert any(r[:2] == ["members", ALFA] for r in table)
    assert any(r[:2] == ["sends", ALFA] for r in table)
    assert any(r[:3] == ["opt_outs", ALFA, "unsubscribe"] for r in table)


@pytest.mark.asyncio
async def test_a_record_cell_never_runs_as_a_formula(state):
    cid = await state.campaigns_call(
        campaigns.create_campaign, provider="gmail", address="", name="=HYPERLINK(1)",
        sequence=SEQUENCE, li_note="+" + NOTE)
    text = campaigns.record_csv(await state.campaigns_call(campaigns.record, cid))
    assert "'=HYPERLINK(1)" in text and "'+" + NOTE[:20] in text
    # A draft that never started has no acknowledgement it started under.
    assert (await state.campaigns_call(campaigns.record, cid))["acknowledgement"] is None
    with pytest.raises(campaigns.Refused):
        await state.campaigns_call(campaigns.record, "nimic")


def test_the_dashboard_and_the_cli_export_the_record(client, monkeypatch, tmp_path, capsys):
    from harvey import cli
    from harvey import config as config_mod
    from harvey import state as state_mod

    cfg = config()
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)

    async def seed():
        s = StateManager(str(client.db_path))
        await s.init_db()
        await contact(s, ALFA)
        cid, _ = await new_campaign(s, cfg)
        return cid
    cid = asyncio.run(seed())
    r = client.get(f"/api/campaigns/{cid}/record.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["members"][0]["email"] == ALFA and r.json()["li_note"] == NOTE
    r = client.get(f"/api/campaigns/{cid}/record.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿section,field,value") and ALFA in r.text
    assert client.get(f"/api/campaigns/{cid}/record.xml").status_code == 404
    assert client.get("/api/campaigns/nimic/record.json").status_code == 404

    monkeypatch.setattr(state_mod, "DB_PATH", str(client.db_path))
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    out = tmp_path / "evidenta.json"
    monkeypatch.setattr("sys.argv", ["harvey", "campaigns", "export", cid, "--format", "json",
                                     "--out", str(out)])
    cli.main()
    said = capsys.readouterr().out
    assert "1 member(s), 0 email(s) sent, 0 opt-out(s)" in said
    assert json.loads(out.read_text(encoding="utf-8"))["campaign"]["id"] == cid


# ── The AI disclosure line ──

async def _reply_row(state, email=ALFA):
    pid = await seed_prospect(state, email=email, status="replied")
    return await state.add_outbox_item(prospect_id=pid, to_email=email, kind="reply",
                                       subject="Re: o întrebare", body="Da, marți merge.",
                                       send_at=_now().isoformat())


@pytest.mark.asyncio
async def test_only_an_answer_autopilot_sent_carries_the_disclosure_line(state, monkeypatch):
    item = await _reply_row(state)
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    from tests.test_outbox_native import make_sender as native_sender

    provider = FakeProvider()
    await native_sender(state, provider, require_approval=False)._drain_due()
    [sent] = provider.sent
    assert sent["body"] == "Da, marți merge.\n\n" + DEFAULT_AI_DISCLOSURE_LINE
    row = await state.get_outbox_item(item)
    assert row["approved_by"] == "autopilot" and row["body"] == sent["body"]

    # An answer you approved goes as you approved it.
    mine = await _reply_row(state, email=BETA)
    assert await state.approve_outbox(mine) == 1
    provider = FakeProvider()
    await native_sender(state, provider, require_approval=False)._drain_due()
    assert [m["body"] for m in provider.sent] == ["Da, marți merge."]


@pytest.mark.asyncio
async def test_cold_email_never_carries_it_and_it_can_be_turned_off(state, monkeypatch):
    cfg = config(autopilot=True)
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA)
    await sender._run_native()
    [cold] = provider.sent
    assert DEFAULT_AI_DISCLOSURE_LINE not in cold["body"]

    off = config(autopilot=True)
    off.compliance = ComplianceConfig(ai_disclosure_line="")
    await _reply_row(state, email=BETA)
    sender, provider = make_sender(state, off, monkeypatch)
    await sender._drain_due()
    assert [m["body"] for m in provider.sent] == ["Da, marți merge."]
    with pytest.raises(ValueError):
        ComplianceConfig(ai_disclosure_line="Scris de AI, vezi https://altundeva.example.org")


# ── The privacy page after a personal address's source line ──

@pytest.mark.asyncio
async def test_a_personal_first_email_carries_the_privacy_page(state, monkeypatch):
    cfg = config()
    cfg.compliance = ComplianceConfig(privacy_url="https://exemplu.example.com/date")
    await _staged(state, cfg, monkeypatch, "ion.popescu@alfa.example.com", BETA)
    rows = await state.get_outbox(limit=10)
    personal = _first(rows, "ion.popescu@alfa.example.com")
    assert personal["source_note"].endswith("https://exemplu.example.com/date")
    assert personal["body"].endswith(personal["source_note"])
    # A role mailbox gets no source line, so no link either.
    role = _first(rows, BETA)
    assert role["source_note"] == "" and "exemplu.example.com/date" not in role["body"]
    # Its follow-ups don't carry it.
    assert all("exemplu.example.com/date" not in r["body"] for r in rows if r["step"] > 1)


@pytest.mark.asyncio
async def test_the_privacy_link_counts_against_the_one_link(state, monkeypatch):
    """With the privacy page, a personal first email has room for no other
    link: one that has one waits for you (it would fail at send for good),
    and goes once the other link is out."""
    cfg = config()
    cfg.compliance = ComplianceConfig(privacy_url="https://exemplu.example.com/date")
    cid, sender, provider = await _staged(state, cfg, monkeypatch, "ion.popescu@alfa.example.com")
    item = _first(await state.get_outbox(limit=10), "ion.popescu@alfa.example.com")
    body = item["body"].replace("Mulțumesc.", "Mulțumesc. https://exemplu.example.com/demo")
    await state.update_outbox_item(item["id"], body=body)
    await state.approve_outbox(item["id"])
    await sender._drain_due()
    row = await state.get_outbox_item(item["id"])
    assert provider.sent == [] and (row["status"], row["error"]) == (
        "pending_review", PRIVACY_LINK_HELD)
    assert await member_status(state, cid, "ion.popescu@alfa.example.com") == "staged"
    # Approve all leaves it for a look of its own (only its follow-up goes).
    assert await state.approve_outbox() == 1
    assert (await state.get_outbox_item(item["id"]))["status"] == "pending_review"
    await state.edit_outbox_draft(item["id"], item["subject"], item["body"])
    await state.approve_outbox(item["id"])
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["ion.popescu@alfa.example.com"]


@pytest.mark.asyncio
async def test_a_booking_link_with_the_privacy_page_waits_from_staging(state, monkeypatch):
    """The campaign's own first step carries a link: staged with the note,
    so it shows in review, and autopilot never approves it."""
    cfg = config(autopilot=True, link_domains=["cal.example.com", "exemplu.example.com"])
    cfg.compliance = ComplianceConfig(privacy_url="https://exemplu.example.com/date")
    monkeypatch.setitem(SEQUENCE[0], "body",
                        "Bună ziua, alegeți o oră: https://cal.example.com/ana Mulțumesc.")
    cid, sender, provider = await _staged(state, cfg, monkeypatch,
                                          "ion.popescu@alfa.example.com", BETA)
    rows = await state.get_outbox(limit=10)
    personal = _first(rows, "ion.popescu@alfa.example.com")
    assert (personal["status"], personal["error"]) == ("pending_review", PRIVACY_LINK_HELD)
    # A role mailbox gets no privacy page: its one link is the booking one.
    assert _first(rows, BETA)["error"] == ""
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == [BETA]
    assert (await state.get_outbox_item(personal["id"]))["status"] == "pending_review"


def test_privacy_url_must_be_a_web_address():
    assert ComplianceConfig(privacy_url=None).privacy_url == ""
    assert ComplianceConfig(privacy_url=" https://exemplu.example.com/date ").privacy_url == \
        "https://exemplu.example.com/date"
    for bad in ("exemplu.example.com", "javascript:alert(1)", "https://x y.example.com"):
        with pytest.raises(ValueError):
            ComplianceConfig(privacy_url=bad)


# ── A rejected or cut-off email ends its member ──

@pytest.mark.asyncio
async def test_a_rejected_first_email_ends_its_member_and_the_campaign_can_finish(
        state, monkeypatch):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA)
    rows = await state.get_outbox(limit=10)
    assert await state.reject_outbox_item(_first(rows, ALFA)["id"]) == 1
    m = await state.campaign_member(cid, ALFA)
    assert (m["status_name"], m["end_reason"]) == ("stopped", campaigns.REJECTED)
    follow = next(r for r in rows if r["step"] == 2)
    assert (await state.get_outbox_item(follow["id"]))["status"] == "cancelled"
    assert (await state.campaign_record(cid))["status"] == "finished"
    # Rejecting what is already gone changes nothing.
    assert await state.reject_outbox_item(_first(rows, ALFA)["id"]) == 0


@pytest.mark.asyncio
async def test_a_rejected_email_leaves_a_rolling_campaign_running(state, monkeypatch):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA, rolling=True)
    await state.reject_outbox_item(_first(await state.get_outbox(limit=10), ALFA)["id"])
    assert await member_status(state, cid, ALFA) == "stopped"
    assert (await state.campaign_record(cid))["status"] == "active"


@pytest.mark.asyncio
async def test_an_email_cut_off_mid_send_ends_its_member(state, monkeypatch):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA, BETA)
    rows = await state.get_outbox(limit=10)
    first = _first(rows, ALFA)
    await state.approve_outbox(first["id"])
    assert await state.claim_outbox_item(first["id"])
    stale = (_now() - timedelta(hours=2)).isoformat()
    with sqlite3.connect(state.db_path) as con:
        con.execute("UPDATE outbox SET updated_at = ? WHERE id = ?", (stale, first["id"]))
    assert await state.fail_stale_sending_outbox() == 1
    row = await state.get_outbox_item(first["id"])
    assert row["status"] == "failed" and row["error"].startswith("may have gone out")
    m = await state.campaign_member(cid, ALFA)
    assert (m["status_name"], m["end_reason"]) == ("stopped", campaigns.CUT_OFF)
    follow = next(r for r in rows if r["to_email"] == ALFA and r["step"] == 2)
    assert (await state.get_outbox_item(follow["id"]))["status"] == "cancelled"
    # The other member goes on; the campaign isn't over.
    assert await member_status(state, cid, BETA) == "staged"
    assert (await state.campaign_record(cid))["status"] == "active"
    # The sender's own clean-up says the same.
    await sender._run_native()
    assert provider.sent == []


# ── An email approved while its campaign is paused ──

@pytest.mark.asyncio
async def test_an_email_approved_while_paused_says_why_it_waits(state, monkeypatch):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA, BETA)
    await state.campaigns_call(campaigns.set_status, cid, "pause")
    rows = await state.get_outbox(limit=10)
    assert await state.approve_outbox(_first(rows, ALFA)["id"]) == 1
    assert (await state.get_outbox_item(_first(rows, ALFA)["id"]))["error"] == CAMPAIGN_PAUSED
    assert await state.approve_outbox() == 3                 # approve all: the same note
    assert {r["error"] for r in await state.get_outbox(status="approved")} == {CAMPAIGN_PAUSED}
    await sender._drain_due()
    assert provider.sent == []
    await state.campaigns_call(campaigns.set_status, cid, "resume")
    assert {r["error"] for r in await state.get_outbox(status="approved")} == {""}
    await sender._drain_due()
    assert sorted(m["to"] for m in provider.sent) == [ALFA, BETA]


# ── Autopilot answers are approved when they go, never when queued ──

@pytest.mark.asyncio
async def test_an_autopilot_answer_waits_for_the_drain_and_its_review(state, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    await seed_prospect(state, email="jane@acme.example", status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="jane@acme.example",
                                       subject="Re: hi", body="Cât costă?")]
    handler = make_handler(state, provider)
    handler.brain._reply = "Costă puțin. Detalii: https://altundeva.example.org/oferta"
    await handler._run_native()
    [queued] = await state.get_outbox(limit=10)
    assert (queued["kind"], queued["status"], queued["approved_by"]) == (
        "reply", "pending_review", "")

    # Sending paused: no drain, so nothing is approved.
    from tests.test_outbox_native import make_sender as native_sender

    await state.set_setting(sender_mod.KILL_SWITCH_KEY, "paused by you")
    await native_sender(state, provider, require_approval=False)._run_native()
    assert (await state.get_outbox_item(queued["id"]))["status"] == "pending_review"

    # Resumed: the drain approves it and reviews it then; the planted link
    # sends it back to you instead of out.
    await state.delete_setting(sender_mod.KILL_SWITCH_KEY)
    await native_sender(state, provider, require_approval=False)._run_native()
    row = await state.get_outbox_item(queued["id"])
    assert provider.sent == [] and row["status"] == "pending_review"
    assert row["error"].startswith(HELD_FOR_REVIEW)


# ── Schema 28: old conversations get their campaign ──

def test_schema_28_gives_an_old_conversation_the_campaign_it_answers(tmp_path):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path, isolation_level=None)
    schema.create(con, triggers=False, version=27)
    con.executescript("""
        INSERT INTO campaigns (id, name, status) VALUES ('c1', 'Unu', 'active'),
                                                       ('c2', 'Doi', 'active');
        INSERT INTO outbox (id, campaign_id, prospect_id, kind, status, sent_at)
          VALUES ('o1', 'c1', 'p1', 'sequence', 'sent', '2026-10-01T09:00:00'),
                 ('o2', 'c2', 'p1', 'sequence', 'sent', '2026-10-20T09:00:00'),
                 ('o3', 'c2', 'p2', 'sequence', 'pending_review', NULL);
        INSERT INTO conversations (id, prospect_id, campaign_id, created_at)
          VALUES ('v1', 'p1', '', '2026-10-05 10:00:00'),
                 ('v2', 'p1', 'c2', '2026-10-21 10:00:00'),
                 ('v3', 'p2', '', '2026-10-21 10:00:00'),
                 ('v4', 'p3', '', '2026-10-21 10:00:00'),
                 ('v5', 'p4', '', '2026-09-01 10:00:00');
        INSERT INTO outbox (id, campaign_id, prospect_id, kind, status, sent_at)
          VALUES ('o4', 'c2', 'p4', 'sequence', 'sent', '2026-10-20T09:00:00');
    """)
    assert schema.step_up(con) == schema.LATEST == 40
    got = dict(con.execute("SELECT id, campaign_id FROM conversations").fetchall())
    # The cold email sent before it began; one already attributed is kept;
    # none sent, or none at all, leaves it as it was. A conversation older
    # than every cold email to them answers none of them: it stays empty.
    assert got == {"v1": "c1", "v2": "c2", "v3": "", "v4": "", "v5": ""}
    con.close()


# ── Instantly in the settings: the dashboard and the contact finder keep working ──

def test_the_dashboard_says_what_to_pick_and_keeps_working_on_instantly(client, monkeypatch):
    import harvey.config as config_mod

    cfg = config(provider="instantly")
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)
    monkeypatch.setattr(dash, "_current_provider", lambda: "instantly")
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    [item] = [i for i in client.get("/api/today").json()["items"] if i["key"] == "provider"]
    assert item["tab"] == "settings" and "gmail, smtp or microsoft" in item["detail"]
    [check] = [c for c in client.get("/api/setup-status").json()["checks"]
               if c["id"] == "email_provider"]
    assert check["done"] is False and "Instantly nu mai e suportat" in check["help"]
    settings = client.get("/api/settings").json()
    assert settings["provider"] == "instantly" and "instantly_api_key_set" not in settings
    assert client.post("/api/settings/test-instantly", json={"api_key": "x"}).status_code in (
        404, 405)
    assert client.get("/api/campaigns").status_code == 200
    assert client.get("/api/sending/status").status_code == 200


def test_the_contact_finder_works_whatever_the_mail_provider(tmp_path, monkeypatch):
    from harvey import finder
    from harvey.modules import api
    from tests.test_contact_finder import (
        ENV, Fakes, _company, _contact, _site, _spend, _state, run,
    )

    Fakes(sites={"alfa.example": _site(
        "https://alfa.example/", "<p>Alfa</p>", _contact("email", "office@alfa.example"),
        _contact("phone", "+40722000111"),
        people=[api.Person(first_name="Ana", last_name="Exemplu", title="Owner",
                           module="people", capability="find_people")])}).install(monkeypatch)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _company(s, name="ALFA EXAMPLE SRL", domain="alfa.example",
                             location="Brașov, Brașov, Romania")
        return await finder.find(s, cid, spend=_spend(cap=False), env=ENV,
                                 config=config(provider="instantly"))

    found = run(go())
    assert found.steps == ["site"] and found.contacts == 2


# ── A campaign an earlier release deployed to Instantly ──

@pytest.mark.asyncio
async def test_an_instantly_campaign_is_never_staged_through_the_mailbox(tmp_path, monkeypatch):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path)
    schema.create(con, version=26)
    for pid, email, status in (("p0", "unu@alfa.example.com", "new"),
                               ("p1", "doi@beta.example.com", "contacted")):
        con.execute("INSERT INTO prospects (id, email, email_status, status) VALUES "
                    "(?, ?, 'verified', ?)", (pid, email, status))
    con.execute("INSERT INTO campaigns (id, name, sequence_json, prospect_ids_json, status, "
                "instantly_campaign_id) VALUES ('c1', 'auto', ?, '[\"p0\", \"p1\"]', 'active', "
                "'inst-1')", ('[{"step": 1, "subject": "s", "body": "b", "delay_days": 0}, '
                              '{"step": 2, "subject": "s", "body": "b", "delay_days": 3}]',))
    con.commit()
    con.close()

    state = StateManager(str(path))
    await state.init_db()
    # Never uploaded: it stops, saying where the campaign goes on.
    m = await state.campaign_member("c1", "unu@alfa.example.com")
    assert (m["status_name"], m["end_reason"]) == ("stopped", "deployed to Instantly")
    assert await member_status(state, "c1", "doi@beta.example.com") == "in_sequence"

    cfg = config(autopilot=True)
    sender, provider = make_sender(state, cfg, monkeypatch)
    await sender._run_native()
    await cycle(state, cfg, sender).run()
    assert await state.get_outbox(limit=10) == [] and provider.sent == []
    # Whatever reopens a member there, the mailbox still stages nothing.
    await state.set_campaign_member("c1", "unu@alfa.example.com", "ready")
    await sender._run_native()
    assert await state.get_outbox(limit=10) == [] and provider.sent == []


# ── A send that failed ends its member ──

@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raised", "refused"])
async def test_a_failed_send_ends_its_member_and_the_campaign_can_finish(state, monkeypatch, how):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA)
    rows = await state.get_outbox(limit=10)
    await state.approve_outbox(_first(rows, ALFA)["id"])
    if how == "raised":
        async def boom(*a, **k):
            raise ConnectionError("conexiune închisă")
        monkeypatch.setattr(provider, "send_email", boom)
    else:
        provider.fail_next = True
    await sender._drain_due()
    row = await state.get_outbox_item(_first(rows, ALFA)["id"])
    assert row["status"] == "failed"
    m = await state.campaign_member(cid, ALFA)
    reason = campaigns.CUT_OFF if how == "raised" else campaigns.SEND_FAILED
    assert (m["status_name"], m["end_reason"]) == ("stopped", reason)
    follow = next(r for r in rows if r["step"] == 2)
    assert (await state.get_outbox_item(follow["id"]))["status"] == "cancelled"
    assert (await state.campaign_record(cid))["status"] == "finished"


def test_the_page_names_every_reason_a_member_ends():
    from pathlib import Path

    web = Path(dash.__file__).resolve().parent / "web"
    js = (web / "app.js").read_text(encoding="utf-8")
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    for reason, key in ((campaigns.SEND_FAILED, "cp.why.send_failed"),
                        ("deployed to Instantly", "cp.why.instantly")):
        assert f"[/^{reason}$/, '{key}']" in js
        assert i18n.count(f"'{key}':") == 2


# ── What waits for you stays waiting for you ──

@pytest.mark.asyncio
async def test_approve_all_leaves_the_first_answer_after_an_undo(state):
    item = await _reply_row(state)
    await state.update_outbox_item(item, error=REVIEW_AFTER_UNDO)
    other = await _reply_row(state, email=BETA)
    assert await state.approve_outbox() == 1
    assert (await state.get_outbox_item(item))["status"] == "pending_review"
    assert (await state.get_outbox_item(other))["status"] == "approved"
    assert await state.approve_outbox(item) == 1


@pytest.mark.asyncio
async def test_what_an_undone_hand_send_brings_back_autopilot_never_approves(state):
    pid = await seed_prospect(state, status="queued")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.example", subject="s",
                                body="b", send_at=_now().isoformat(), status="approved",
                                approved_by="autopilot", campaign_id="c1", step=1)
    await state.cancel_pending_outbox_for_prospect(pid, reason="sent_by_hand")
    assert await state.restore_hand_cancelled_outbox(pid, reason="sent_by_hand") == 1
    [row] = await state.get_outbox(status="pending_review")
    assert (row["error"], row["approved_by"]) == (RESTORED_FOR_REVIEW, "")
    later = (_now() + timedelta(days=30)).isoformat()
    assert await state.promote_for_autopilot(later, 10) == 0
    assert await state.approve_outbox(row["id"]) == 1


def test_schema_28_keeps_an_answer_queued_after_an_undo_waiting(tmp_path):
    path = tmp_path / "harvey.db"
    con = sqlite3.connect(path, isolation_level=None)
    schema.create(con, triggers=False, version=27)
    con.executescript("""
        INSERT INTO prospects (id, email, status) VALUES
          ('p1', 'unu@alfa.example.com', 'replied'), ('p2', 'doi@beta.example.com', 'replied'),
          ('p3', 'trei@gama.example.com', 'write_failed');
        INSERT INTO actions (id, action_type, agent, details_json, created_at) VALUES
          ('a1', 'opt_out_undone', 'dashboard', '{"prospect_id": "p1"}', '2026-10-01 09:00:00');
        INSERT INTO outbox (id, prospect_id, kind, status, error, created_at) VALUES
          ('r1', 'p1', 'reply', 'pending_review', '', '2026-10-02 09:00:00'),
          ('r2', 'p2', 'reply', 'pending_review', '', '2026-10-02 09:00:00'),
          ('r3', 'p1', 'reply', 'pending_review', '', '2026-09-01 09:00:00');
    """)
    assert schema.step_up(con) == schema.LATEST
    errors = dict(con.execute("SELECT id, error FROM outbox").fetchall())
    assert errors == {"r1": REVIEW_AFTER_UNDO, "r2": "", "r3": ""}
    # The status nothing could reset any more is 'new' again.
    assert con.execute("SELECT status FROM prospects WHERE id = 'p3'").fetchone() == ("new",)
    con.close()


# ── The record: every opt-out that reaches a member, and the answers sent ──

@pytest.mark.asyncio
async def test_the_record_has_domain_and_firm_opt_outs_and_answers_sent(state, monkeypatch):
    from harvey.models import Conversation

    cfg = config()
    gama = "office@gama.example.com"
    await contact(state, ALFA)
    await contact(state, BETA)
    firm = await contact(state, gama)
    cid, _ = await new_campaign(state, cfg)
    await state.suppress("domain", "beta.example.com", "objection", by="you")
    await state.suppress("firm", firm, "objection", by="you")
    await state.suppress("domain", "altundeva.example.org", "objection", by="you")
    pid = await seed_prospect(state, email=ALFA, status="replied")
    convo = await state.add_conversation(Conversation(id="", prospect_id=pid, campaign_id=cid))
    await state.add_outbox_item(prospect_id=pid, to_email=ALFA, kind="reply",
                                conversation_id=convo, subject="Re: o întrebare",
                                body="Da, marți merge.", send_at=_now().isoformat(),
                                status="sent")
    rec = await state.campaigns_call(campaigns.record, cid, write=False)
    reached = {(o["scope"], o["value"]): o["email"] for o in rec["opt_outs"]}
    assert reached[("domain", "beta.example.com")] == BETA
    assert reached[("domain", "gama.example.com")] == gama
    assert any(scope == "firm" and email == gama for (scope, _), email in reached.items())
    assert ("domain", "altundeva.example.org") not in reached
    assert [(s["kind"], s["email"]) for s in rec["sends"]] == [("reply", ALFA)]


# ── A stale note doesn't outlive the send ──

@pytest.mark.asyncio
@pytest.mark.parametrize("note", [CAMPAIGN_PAUSED, CAMPAIGN_NOT_STARTED])
async def test_a_sent_email_keeps_no_waiting_note(state, monkeypatch, note):
    cfg = config()
    cid, sender, provider = await _staged(state, cfg, monkeypatch, ALFA)
    item = _first(await state.get_outbox(limit=10), ALFA)
    await state.approve_outbox(item["id"])
    await state.update_outbox_item(item["id"], error=note)
    await sender._drain_due()
    row = await state.get_outbox_item(item["id"])
    assert provider.sent and (row["status"], row["error"]) == ("sent", "")


# ── An email provider Harvey doesn't know ──

def test_today_names_an_unknown_provider_not_instantly(client, monkeypatch):
    import harvey.config as config_mod

    cfg = config(provider="altceva")
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)
    monkeypatch.setattr(dash, "_current_provider", lambda: "altceva")
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    [item] = [i for i in client.get("/api/today").json()["items"] if i["key"] == "provider"]
    assert item["provider"] == "altceva" and "Instantly" not in item["detail"]
    from pathlib import Path

    web = Path(dash.__file__).resolve().parent / "web"
    js = (web / "app.js").read_text(encoding="utf-8")
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    assert "t('today.item.provider_unknown', {provider:" in js
    assert i18n.count("'today.item.provider_unknown':") == 2
