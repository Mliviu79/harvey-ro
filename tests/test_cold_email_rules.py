"""The rules every cold email passes before it goes, whatever flow staged
it: the operator's acknowledgement (harvey/acknowledgement.py), the source
note on a first email to a personal address (harvey/email_rules.py), and
the do-not-contact list at send time. All data is fictional."""

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey import acknowledgement, email_rules
from harvey.gate import pre_send_check
from harvey.models import Company
from harvey.models.prospect import Prospect
from harvey.state import ACK_NEEDED, NOTE_ADDED, StateManager
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign, seed_prospect


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


async def _named(state, email="ana.popescu@exemplu.example.com"):
    """A named person at a firm (their prospect card links them by name)."""
    cid = await state.add_company(Company(name="Exemplu", domain="exemplu.example.com"))
    return await state.add_prospect(Prospect(
        company_id=cid, first_name="Ana", last_name="Popescu", title="Owner", company="Exemplu",
        email=email, email_status="verified", email_verified=True, status="queued"))


async def _queue(state, pid, to, *, step=1, kind="sequence", body="A short note. Question?",
                 note=""):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello", body=body, send_at=_now(),
        status="approved", campaign_id="c1" if kind == "sequence" else "", step=step, kind=kind,
        source_note=note)
    return item


# ── The gate ──

def test_the_gate_refuses_cold_email_without_the_acknowledgement():
    cold = pre_send_check("office@exemplu.example.com", "hello", "A note.", acknowledged=False,
                          personal=False)
    assert not cold and "acknowledgement" in cold.reasons[0]
    assert pre_send_check("office@exemplu.example.com", "Re: hello", "A note.",
                          kind="reply", acknowledged=False)


def test_the_gate_refuses_cold_email_when_the_caller_doesnt_say():
    # A caller that leaves out the acknowledgement, or whether a first
    # email's address is personal, is refused, not waved through.
    body = "A note. Question?"
    unsaid = pre_send_check("office@exemplu.example.com", "hello", body)
    assert not unsaid
    assert any("acknowledgement" in r for r in unsaid.reasons)
    assert any("personal" in r for r in unsaid.reasons)
    assert not pre_send_check("office@exemplu.example.com", "hello", body, personal=False)
    assert not pre_send_check("office@exemplu.example.com", "hello", body, acknowledged=True)
    # A follow-up doesn't need the personal check; a reply needs neither.
    assert pre_send_check("office@exemplu.example.com", "hello", body, acknowledged=True, step=2)
    assert pre_send_check("office@exemplu.example.com", "Re: hello", body, kind="reply")


def test_the_gate_refuses_a_personal_first_email_without_its_note():
    note = "Am găsit adresa pe site-ul dumneavoastră (exemplu.example.com)."
    bare = pre_send_check("ana@exemplu.example.com", "hello", "A note.", personal=True,
                          acknowledged=True)
    assert not bare and "source note" in bare.reasons[0]
    assert not pre_send_check("ana@exemplu.example.com", "hello", "A note.", personal=True,
                              source_note=note, acknowledged=True)   # kept on the row, edited out
    assert pre_send_check("ana@exemplu.example.com", "hello", f"A note.\n\n{note}",
                          personal=True, source_note=note, acknowledged=True)
    assert pre_send_check("ana@exemplu.example.com", "hello", "A follow-up.", personal=True,
                          step=2, acknowledged=True)
    assert pre_send_check("office@exemplu.example.com", "hello", "A note.", personal=False,
                          acknowledged=True)


# ── The sender ──

@pytest.mark.no_acknowledgement
@pytest.mark.asyncio
async def test_cold_email_waits_for_the_acknowledgement_and_replies_go(state):
    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    answered = await seed_prospect(state, email="office@altul.example.com", status="replied")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    reply = await _queue(state, answered, "office@altul.example.com", kind="reply")
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["office@altul.example.com"]
    assert (await state.get_outbox_item(reply))["status"] == "sent"
    row = await state.get_outbox_item(cold)
    assert (row["status"], row["error"]) == ("approved", ACK_NEEDED)

    assert await state.acknowledge("cli") is True
    assert await state.acknowledge("cli") is False      # once
    await sender._drain_due()
    assert (await state.get_outbox_item(cold))["status"] == "sent"


@pytest.mark.no_acknowledgement
@pytest.mark.asyncio
async def test_the_acknowledgement_is_versioned_by_its_text(state, monkeypatch):
    assert not await state.acknowledged()
    await state.acknowledge("dashboard")
    status = await state.acknowledgement_status()
    assert status["given"] and status["by"] == "dashboard"
    assert status["text_sha"] == acknowledgement.text_sha()
    assert "Legea 506/2004" in status["text_ro"] and "Law 506/2004" in status["text_en"]
    # A changed text asks again.
    monkeypatch.setattr(acknowledgement, "TEXT_RO", acknowledgement.TEXT_RO + " ")
    assert not await state.acknowledged()


@pytest.mark.asyncio
async def test_a_personal_first_email_without_its_note_gets_it_and_waits_for_you(state):
    pid = await _named(state)
    item = await _queue(state, pid, "ana.popescu@exemplu.example.com")
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._drain_due()
    assert provider.sent == []
    row = await state.get_outbox_item(item)
    assert (row["status"], row["error"]) == ("pending_review", NOTE_ADDED)
    assert row["source_note"] and row["body"].endswith(row["source_note"])
    # Approved as it is now, it goes, the note above the footer.
    assert await state.approve_outbox(item) == 1
    await sender._drain_due()
    assert len(provider.sent) == 1 and row["source_note"] in provider.sent[0]["body"]


@pytest.mark.asyncio
async def test_staging_adds_the_note_to_a_personal_first_email_only(state):
    named = await _named(state)
    inbox = await seed_prospect(state, email="office@altul.example.com", status="new")
    campaign = await seed_campaign(state, [named, inbox])
    await make_sender(state, FakeProvider())._stage_campaign_native(campaign)
    rows = {(r["to_email"], r["step"]): r for r in await state.get_outbox(limit=10)}
    first = rows[("ana.popescu@exemplu.example.com", 1)]
    assert first["source_note"] == email_rules.NOTE_OTHER
    assert first["body"].endswith(email_rules.NOTE_OTHER)
    assert rows[("ana.popescu@exemplu.example.com", 2)]["source_note"] == ""
    assert email_rules.NOTE_OTHER not in rows[("ana.popescu@exemplu.example.com", 2)]["body"]
    assert rows[("office@altul.example.com", 1)]["source_note"] == ""


@pytest.mark.asyncio
async def test_a_suppressed_address_is_never_sent_to_reply_or_not(state):
    pid = await seed_prospect(state, email="office@exemplu.example.com", status="queued")
    cold = await _queue(state, pid, "office@exemplu.example.com")
    reply = await _queue(state, pid, "office@exemplu.example.com", kind="reply")
    await state.suppress("domain", "exemplu.example.com", "by_hand", by="you")
    provider = FakeProvider()
    await make_sender(state, provider, require_approval=False)._drain_due()
    assert provider.sent == []
    for item in (cold, reply):
        row = await state.get_outbox_item(item)
        assert row["status"] == "failed" and "do-not-contact list" in row["error"]


@pytest.mark.asyncio
async def test_staging_while_paused_only_fills_the_review_queue(state):
    pid = await seed_prospect(state, email="office@exemplu.example.com")
    campaign = await seed_campaign(state, [pid])
    await state.set_setting("sending_paused", "paused manually")
    provider = FakeProvider()
    await make_sender(state, provider, require_approval=False)._run_native()
    rows = await state.get_outbox(limit=10)
    # Staged, even on autopilot, for review: nothing approved, nothing sent.
    assert rows and {(r["status"], r["approved_by"]) for r in rows} == {("pending_review", "")}
    assert provider.sent == []
    assert [c.id for c in await state.get_campaigns_by_status("active")] == [campaign.id]


# ── Which addresses are personal ──

@pytest.mark.parametrize("local, people, named", [
    ("ana.popescu", [], True),          # first.last
    ("ana", ["Ana Popescu"], True),
    ("apopescu", ["POPESCU ANA"], True),  # an initial and the family name
    ("popescu", ["Ana Popescu"], True),
    ("office", ["Ana Popescu"], False),
    ("contabilitate", [], False),
    ("vanzari.brasov", [], False),
    ("info2", [], False),
    ("ana", [], False),
])
def test_a_local_part_that_names_a_person(local, people, named):
    assert email_rules.names_person(local, people) is named


def test_personal_by_link_free_mail_and_legal_form(monkeypatch):
    import harvey.state

    base = {"person_link": False, "people": [], "firm_forms": []}
    assert email_rules.personal_reason("office@firma.example.com",
                                       {**base, "person_link": True}) == email_rules.NAMED
    assert email_rules.personal_reason("exemplu.fictiv.ro@yahoo.ro", base) == email_rules.FREE_MAIL
    # A home-ISP mailbox counts as free mail through the list (a fictional
    # domain stands in for the real ones on it).
    assert "rdslink.ro" in harvey.state.FREE_MAIL_DOMAINS
    monkeypatch.setattr(harvey.state, "FREE_MAIL_DOMAINS",
                        harvey.state.FREE_MAIL_DOMAINS | {"isp-exemplu.test"})
    assert email_rules.personal_reason("office@isp-exemplu.test", base) == email_rules.FREE_MAIL
    assert email_rules.personal_reason("office@firma.example.com",
                                       {**base, "firm_forms": [["PFA", []]]}) \
        == email_rules.NATURAL_PERSON
    # A firm's register form wins over its ANAF reading; with none, ANAF's
    # spelled-out form.
    assert email_rules.personal_reason(
        "office@firma.example.com",
        {**base, "firm_forms": [["SRL", ["PERSOANA FIZICA AUTORIZATA"]]]}) == 0
    assert email_rules.personal_reason(
        "office@firma.example.com",
        {**base, "firm_forms": [["", ["ÎNTREPRINDERE INDIVIDUALĂ"]]]}) == email_rules.NATURAL_PERSON
    assert email_rules.personal_reason("office@firma.example.com", base) == 0


def test_the_legal_form_is_decided_for_each_linked_firm():
    # Firm A is an SRL in the register; firm B has no register form and
    # ANAF reads it as a PFA: the address reaches a natural-person business.
    assert email_rules.natural_person_form([["SRL", []], ["", ["PERSOANA FIZICA AUTORIZATA"]]])
    assert not email_rules.natural_person_form([["SRL", ["PERSOANA FIZICA AUTORIZATA"]],
                                                ["SA", []]])


def test_the_source_note_follows_the_evidence():
    site = {"email": "ana@exemplu.example.com", "sources": ["website"],
            "evidence_urls": ["https://www.exemplu.example.com/contact"], "domains": []}
    assert email_rules.source_note(site) == \
        "Am găsit adresa pe site-ul dumneavoastră (exemplu.example.com)."
    assert email_rules.source_note({"sources": ["onrc"]}) == email_rules.NOTE_REGISTER
    assert email_rules.source_note({"sources": ["osm"]}).startswith("Am găsit adresa în ")
    assert email_rules.source_note({"email": "ana@exemplu.example.com",
                                    "sources": ["email_verify"], "domains": []}) == \
        "Am dedus adresa din formatul adreselor publice ale firmei (exemplu.example.com)."
    assert email_rules.source_note({"sources": ["a-module-nobody-knows"]}) == \
        email_rules.NOTE_OTHER
    assert email_rules.source_note({}) == email_rules.NOTE_OTHER
    assert email_rules.with_note("Body.", "Note.") == "Body.\n\nNote."
    assert email_rules.with_note("Body.\n\nNote.", "Note.") == "Body.\n\nNote."


@pytest.mark.asyncio
async def test_any_link_naming_a_person_makes_the_address_personal(state):
    from harvey import contacts

    a = await state.add_company(Company(name="Unu", domain="unu.example.com"))
    b = await state.add_company(Company(name="Doi", domain="doi.example.com"))
    address = "rezervari@unu.example.com"
    await state.add_contacts(a, [contacts.Found("email", address, source="website")])
    assert await state.personal_address(address) == (0, "")
    # A second firm names a person behind it: personal, whichever link a
    # campaign would pick.
    await state.add_contacts(b, [contacts.Found("email", address, person="Ion Ionescu",
                                                source="website")])
    reason, note = await state.personal_address(address)
    assert reason == email_rules.NAMED and note.startswith("Am găsit adresa pe site-ul")


# ── The CLI and the dashboard ──

@pytest.mark.no_acknowledgement
def test_harvey_ack_records_it_only_when_typed(tmp_path, monkeypatch, capsys):
    import asyncio

    from harvey import cli
    from harvey import state as state_mod

    path = str(tmp_path / "harvey.db")
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    args = SimpleNamespace(status=False, english=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "nu")
    with pytest.raises(SystemExit):
        cli.cmd_ack(args)
    assert not asyncio.run(StateManager(path).acknowledged())
    monkeypatch.setattr("builtins.input", lambda prompt="": "accept")
    cli.cmd_ack(args)
    assert asyncio.run(StateManager(path).acknowledged())
    assert "Confirmare" in capsys.readouterr().out


@pytest.fixture
def client(tmp_path, monkeypatch):
    import asyncio

    import harvey.dashboard as dash
    from fastapi.testclient import TestClient

    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())
    monkeypatch.setattr(dash, "DB_PATH", path)
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    dash.token_store.configure(tmp_path / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


@pytest.mark.no_acknowledgement
def test_the_dashboard_endpoints(client):
    assert client.get("/api/acknowledgement").json()["given"] is False
    assert client.post("/api/acknowledgement", json={"typed": "ok"}).status_code == 400
    assert client.post("/api/acknowledgement", json={"typed": "accept"}).json()["given"] is True

    assert client.post("/api/suppression", json={"scope": "domain",
                                                 "value": "gmail.com"}).status_code == 400
    added = client.post("/api/suppression", json={"scope": "address",
                                                  "value": "ana@exemplu.example.com"}).json()
    assert added["added"]
    rows = client.get("/api/suppression").json()["rows"]
    assert [(r["scope"], r["value"], r["reason"]) for r in rows] == [
        ("address", "ana@exemplu.example.com", "by_hand")]
    assert "ana@exemplu.example.com" in client.get("/api/suppression.csv").text
    assert client.post(f"/api/suppression/{added['id']}/undo", json={}).json()["success"]
    assert client.get("/api/suppression").json()["rows"] == []
