"""Removing and editing a message that hasn't gone.

Someone may change their mind about a queued message: until it is being
sent, it can be edited, or removed. Removing is not rejecting: the message
is kept as 'removed' (never sent, never counted, listed nowhere as
waiting), a cold email's person stays in the campaign and the cycle drafts
that step again, and a conversation's answer no longer holds the
conversation, so another can be written at once. A message whose send was
tried (an SMS or WhatsApp answer under its Idempotency-Key, an email with a
provider id) may have gone: it is never edited or removed. Every address,
number, firm and person is invented; nPathi is the fake one of
tests/test_npathi.py, the mailbox the fake of tests/test_outbox_native.py.
"""

import asyncio
import json
import sqlite3

import pytest

from harvey import campaigns, channels
from harvey.integrations import npathi
from harvey.modules import switches
from harvey.state import REMOVED_BY_YOU, StateManager
from tests.test_campaigns import (
    StubWriter, config, contact, cycle, make_sender, member_status, new_campaign,
)
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)
from tests.test_npathi import ANN, fake, state  # noqa: F401  (fixtures)
from tests.test_npathi_send import (  # noqa: F401  (fixtures)
    _answer, _convo, _firm, _no_quiet_hours, _ready, _row, _seed_convo, _sender, dash_npat, npat,
)


def _db(env):
    return StateManager(db_path=str(env["db"]))


async def _actions(state, kind):
    async with state._connect() as db:
        async with db.execute("SELECT agent, details_json FROM actions WHERE action_type = ?",
                              (kind,)) as cursor:
            return [(a, json.loads(d)) for a, d in await cursor.fetchall()]


# ── Removing: what may be removed ──

@pytest.mark.asyncio
@pytest.mark.modules_on("npathi")
@pytest.mark.parametrize("channel", ["email", "sms", "waba"])
@pytest.mark.parametrize("approved", [False, True])
async def test_a_waiting_row_of_any_channel_is_removed_and_kept(state, npat, channel, approved):
    _, pid = await _firm(state)
    if channel == "email":
        item = await state.add_outbox_item(
            prospect_id=pid, to_email="ana@alfa.example.com", subject="Re: o întrebare",
            body="Mulțumesc, revin mâine.", send_at="2026-01-01T09:00:00", kind="reply",
            status="approved" if approved else "pending_review",
            approved_by="you" if approved else "")
    else:
        convo = await _convo(state, pid, channel=channel)
        item = await _answer(state, convo, pid, channel=channel, approved=approved)
    assert await state.remove_outbox_item(item, by="dashboard") == 1
    row = await _row(state, item)
    assert (row["status"], row["error"]) == ("removed", REMOVED_BY_YOU)
    # Listed nowhere as waiting, and never sent.
    for status in ("pending_review", "approved"):
        assert await state.get_outbox(status=status) == []
    if channel != "email":
        await _ready(state, npat)
        assert await _sender(state, npat).run() == 0 and npat.sent == []
    # The log names the row, never its text or recipient.
    [(by, details)] = await _actions(state, "outbox_removed")
    assert by == "dashboard" and details["outbox_id"] == item
    assert details["channel"] == channel
    text = json.dumps(details)
    assert ANN not in text and "alfa.example.com" not in text and "200 lei" not in text
    assert "Mulțumesc" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sending", "sent", "failed", "cancelled", "rejected",
                                    "removed"])
async def test_a_row_that_isnt_waiting_is_never_removed(state, status):
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    await state.update_outbox_item(item, status=status)
    assert await state.remove_outbox_item(item, by="dashboard") == 0
    assert (await _row(state, item))["status"] == status
    assert await _actions(state, "outbox_removed") == []


@pytest.mark.asyncio
async def test_a_row_whose_send_was_tried_is_never_removed_or_edited(state):
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    tried = await _answer(state, convo, pid)
    # (nPathi didn't take it this time: it waits, approved, to go again
    # under the same Idempotency-Key, with the first text.)
    await state.set_setting(channels.ATTEMPT_KEY + tried, "1767000000")
    by_id = await _answer(state, await _convo(state, pid), pid)
    await state.update_outbox_item(by_id, message_id="1001")
    for item in (tried, by_id):
        assert await state.outbox_tried(item)
        assert await state.remove_outbox_item(item, by="dashboard") == 0
        row = await _row(state, item)
        assert await state.edit_outbox_item(item, "", "Alt text.", was=row, status="approved",
                                            approved_by="you", error="") == 0
        assert (await _row(state, item))["status"] == "approved"


# ── Removing a campaign's step: the person stays, the step comes again ──

async def _staged(state, monkeypatch, writer=None):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    cid, _ = await new_campaign(state, cfg)
    sender, provider = make_sender(state, cfg, monkeypatch)
    writer = writer or StubWriter()
    await cycle(state, cfg, sender, writer).run()
    rows = {r["step"]: r for r in await state.get_outbox(limit=10)}
    assert set(rows) == {1, 2}
    return cfg, cid, sender, provider, writer, rows


@pytest.mark.asyncio
async def test_a_removed_first_email_is_drafted_again_and_the_member_stays(state,  # noqa: F811
                                                                        monkeypatch):
    cfg, cid, sender, _, writer, rows = await _staged(state, monkeypatch)
    email = "office@alfa.example.com"
    assert await member_status(state, cid, email) == "staged"
    assert await state.remove_outbox_item(rows[1]["id"], by="cli") == 1
    # Not a rejection: the member stays in, back to be drafted.
    assert await member_status(state, cid, email) == "ready"
    [(by, details)] = await _actions(state, "outbox_removed")
    assert by == "cli" and details["drafted_again"] and details["step"] == 1
    await cycle(state, cfg, sender, writer).run()
    assert writer.asked == [email, email]              # a new first email is written
    again = [r for r in await state.get_outbox(limit=10) if r["step"] == 1
             and r["status"] == "pending_review"]
    assert len(again) == 1 and again[0]["id"] != rows[1]["id"]
    assert (await _row(state, rows[1]["id"]))["status"] == "removed"   # kept for the record
    assert (await _row(state, rows[2]["id"]))["status"] == "pending_review"
    assert await member_status(state, cid, email) == "staged"


@pytest.mark.asyncio
async def test_a_removed_follow_up_is_drafted_again_after_the_first_went(state,  # noqa: F811
                                                                      monkeypatch):
    cfg, cid, sender, _, writer, rows = await _staged(state, monkeypatch)
    email = "office@alfa.example.com"
    await state.update_outbox_item(rows[1]["id"], status="sent",
                                   sent_at="2026-01-01T09:00:00", message_id="<m1@example.com>")
    await state.member_sent(cid, email, 1)
    assert await member_status(state, cid, email) == "in_sequence"
    assert await state.remove_outbox_item(rows[2]["id"], by="dashboard") == 1
    assert await member_status(state, cid, email) == "ready"
    await cycle(state, cfg, sender, writer).run()
    # No new first email (theirs went): only the removed step, from the template.
    assert writer.asked == [email]
    [step2] = [r for r in await state.get_outbox(status="pending_review", limit=10)]
    assert step2["step"] == 2 and step2["id"] != rows[2]["id"]
    assert await member_status(state, cid, email) == "in_sequence"
    # The step it followed is the one that went, not the removed one.
    assert (await state.get_previous_step(step2))["status"] == "sent"


@pytest.mark.asyncio
async def test_a_follow_up_waits_while_the_step_before_it_is_drafted_again(state,  # noqa: F811
                                                                         monkeypatch):
    cfg, cid, sender, provider, _, rows = await _staged(state, monkeypatch)
    await state.approve_outbox(rows[2]["id"])
    await state.update_outbox_item(rows[2]["id"], send_at="2026-01-01T09:00:00")
    assert await state.remove_outbox_item(rows[1]["id"], by="dashboard") == 1
    await sender._drain_due()
    # Neither sent nor cancelled (cancelling it would end the member).
    assert provider.sent == []
    assert (await _row(state, rows[2]["id"]))["status"] == "approved"
    assert await member_status(state, cid, "office@alfa.example.com") == "ready"


@pytest.mark.asyncio
async def test_a_rejected_email_still_ends_its_member(state, monkeypatch):  # noqa: F811
    _, cid, _, _, _, rows = await _staged(state, monkeypatch)
    assert await state.reject_outbox_item(rows[1]["id"]) == 1
    assert await member_status(state, cid, "office@alfa.example.com") == "stopped"


@pytest.mark.asyncio
async def test_removed_rows_count_toward_nothing(state, monkeypatch):  # noqa: F811
    _, cid, _, _, _, rows = await _staged(state, monkeypatch)
    await state.approve_outbox(rows[1]["id"])
    assert await state.count_queued_first_emails() == 1
    assert await state.remove_outbox_item(rows[1]["id"], by="dashboard") == 1
    assert await state.count_queued_first_emails() == 0
    assert await state.count_outbox_sent_today() == 0
    assert await state.messages_sent_today("sms") == 0
    counts = await state.campaigns_call(campaigns.record, cid, write=False)
    assert counts["sends"] == []


# ── Editing what hasn't gone ──

@pytest.mark.asyncio
@pytest.mark.modules_on("npathi")
async def test_an_approved_answer_you_edit_stays_approved_and_goes_with_the_new_text(state, npat):
    await _ready(state, npat)
    _, pid = await _firm(state)
    convo = await _convo(state, pid)
    item = await _answer(state, convo, pid)
    row = await _row(state, item)
    new = ("Exemplu Consult: 150 lei pe lună. Vă sun joi?\n"
           "Răspunde STOP ca să nu mai primești mesaje.")
    assert await state.edit_outbox_item(item, "", new, was=row, status="approved",
                                        approved_by="you", error="") == 1
    assert await _sender(state, npat).run() == 1
    [sent] = npat.sent
    assert sent["body"]["body"] == new and sent["key"] == f"harvey-outbox-{item}"


# ── The dashboard ──

def _outside_hours(env):
    async def run():
        await switches.set_module_setting(_db(env), npathi.MODULE, "send_start", "00:00")
        await switches.set_module_setting(_db(env), npathi.MODULE, "send_end", "00:00")
    asyncio.run(run())


def _open_hours(env):
    async def run():
        await switches.set_module_setting(_db(env), npathi.MODULE, "send_start", "00:00")
        await switches.set_module_setting(_db(env), npathi.MODULE, "send_end", "23:59")
    asyncio.run(run())


@pytest.mark.modules_on("npathi")
def test_a_waiting_answer_is_removed_and_a_new_one_written_and_sent(client, env, dash_npat,  # noqa: F811
                                                                    monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    _outside_hours(env)
    first = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert first.status_code == 200 and first.json()["wait"] == "hours"
    item = first.json()["outbox_id"]
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["waiting"]["id"] == item and row["waiting"]["tried"] is False
    again = client.post(f"/api/conversations/{convo}/answer", json={"body": "Sau poimâine?"})
    assert again.status_code == 409 and again.json()["code"] == "waiting"
    assert again.json()["outbox_id"] == item and "edit or remove" in again.json()["message"]
    r = client.post(f"/api/outbox/{item}/remove")
    assert r.status_code == 200 and r.json() == {"success": True}
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["waiting"] is None and row["can_answer"]
    out = client.get("/api/outbox").json()
    assert out["approved"] == [] and [f["status"] for f in out["failed"]] == ["removed"]
    _open_hours(env)
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Sau poimâine?"})
    assert r.status_code == 200 and r.json()["status"] == "sent", r.json()
    [sent] = dash_npat.sent
    assert "Sau poimâine?" in sent["body"]["body"] and "Vă sun mâine?" not in sent["body"]["body"]


@pytest.mark.modules_on("npathi")
def test_a_waiting_answer_edited_on_the_conversation_stays_approved(client, env, dash_npat,  # noqa: F811
                                                                    monkeypatch):
    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    _outside_hours(env)
    item = client.post(f"/api/conversations/{convo}/answer",
                       json={"body": "Vă sun mâine?"}).json()["outbox_id"]
    stored = asyncio.run(_row(_db(env), item))["body"]
    edited = stored.replace("Vă sun mâine?", "Vă sun joi?")
    r = client.post(f"/api/outbox/{item}/edit", json={"body": edited})
    assert r.json() == {"success": True, "status": "approved"}, r.json()
    bad = client.post(f"/api/outbox/{item}/edit", json={"body": "{Salut|Bună}"})
    assert bad.status_code == 400 and bad.json()["code"] == "text" and bad.json()["detail"]
    _open_hours(env)
    import harvey.dashboard as dash
    from harvey.agents import message_loop

    assert asyncio.run(message_loop.send_now(_db(env), item, dash._load_config_quietly()))
    [sent] = dash_npat.sent
    assert sent["body"]["body"] == edited


@pytest.mark.modules_on("npathi")
def test_a_tried_answer_is_refused_with_why(client, env, dash_npat, monkeypatch):  # noqa: F811
    _no_quiet_hours(monkeypatch)
    convo = _seed_convo(env, dash_npat)
    _outside_hours(env)
    item = client.post(f"/api/conversations/{convo}/answer",
                       json={"body": "Vă sun mâine?"}).json()["outbox_id"]
    asyncio.run(_db(env).set_setting(channels.ATTEMPT_KEY + item, "1767000000"))
    [row] = client.get("/api/outbox").json()["approved"]
    assert row["tried"] is True
    for action, payload in (("edit", {"body": "Alt text."}), ("remove", None)):
        r = client.post(f"/api/outbox/{item}/{action}", json=payload)
        assert r.status_code == 409 and r.json()["code"] == "tried", (action, r.json())
    assert asyncio.run(_row(_db(env), item))["status"] == "approved"


def test_remove_needs_the_dashboard_token(client, env):  # noqa: F811
    async def seed():
        return await _db(env).add_outbox_item(
            prospect_id="p1", kind="reply", to_email="ion@beta.example.com", subject="Re: s",
            body="b", send_at="2026-01-01T09:00:00")
    item = asyncio.run(seed())
    r = client.post(f"/api/outbox/{item}/remove", headers={"Authorization": ""})
    assert r.status_code == 401
    assert asyncio.run(_row(_db(env), item))["status"] == "pending_review"


def test_an_email_is_removed_with_the_email_module_off(client, env):  # noqa: F811
    async def seed():
        return await _db(env).add_outbox_item(
            prospect_id="p-old", kind="sequence", step=1, to_email="ion@beta.example.com",
            subject="Salut", body="Un email de dinainte.", send_at="2026-01-01T09:00:00")
    item = asyncio.run(seed())
    assert client.post(f"/api/outbox/{item}/edit",
                       json={"subject": "Alt", "body": "Alt text."}).status_code == 409
    r = client.post(f"/api/outbox/{item}/remove")
    assert r.status_code == 200 and r.json()["success"]
    assert asyncio.run(_row(_db(env), item))["status"] == "removed"
    assert client.post(f"/api/outbox/{item}/remove").json()["code"] == "gone"


# ── The CLI ──

def test_the_cli_removes_one_message(tmp_path, monkeypatch):
    from tests.test_module_switches import _cli

    db = tmp_path / "harvey.db"

    async def seed():
        s = StateManager(db_path=str(db))
        await s.init_db()
        keep = await s.add_outbox_item(
            prospect_id="p1", kind="reply", to_email="ion@beta.example.com", subject="Re: s",
            body="b", send_at="2026-01-01T09:00:00")
        tried = await s.add_outbox_item(
            prospect_id="p2", kind="reply", to_email="dan@beta.example.com", subject="Re: s",
            body="b", send_at="2026-01-01T09:00:00", status="approved", approved_by="you")
        await s.update_outbox_item(tried, message_id="<m1@example.com>")
        return keep, tried

    item, tried = asyncio.run(seed())
    code, out = _cli(monkeypatch, db, "outbox", "--remove", item)
    assert code == 0 and "Removed" in out
    code, out = _cli(monkeypatch, db, "outbox", "--remove", item)
    assert code == 1 and "Nothing waiting" in out
    code, out = _cli(monkeypatch, db, "outbox", "--remove", tried)
    assert code == 1 and "may have gone" in out
    with sqlite3.connect(db) as con:
        assert dict(con.execute("SELECT id, status FROM outbox").fetchall()) == {
            item: "removed", tried: "approved"}


# ── A decision made on a stale read never brings a removed row back ──

@pytest.mark.asyncio
async def test_a_drain_hold_never_brings_back_an_email_removed_meanwhile(state,  # noqa: F811
                                                                       monkeypatch):
    _, cid, sender, provider, _, rows = await _staged(state, monkeypatch)
    item = rows[1]["id"]
    await state.approve_outbox(item)
    await state.update_outbox_item(item, send_at="2026-01-01T09:00:00")

    async def removed_meanwhile(row, prospect, allow_risky):
        # (You remove it while the drain is deciding about it; the drain
        # holds it, approved, as it would for the acknowledgement.)
        await state.remove_outbox_item(row["id"], by="dashboard")
        return "approved", "waiting for something"

    monkeypatch.setattr(sender, "_blocked", removed_meanwhile)
    await sender._drain_due()
    assert (await _row(state, item))["status"] == "removed"
    assert await member_status(state, cid, "office@alfa.example.com") == "ready"
    assert provider.sent == []


@pytest.mark.asyncio
@pytest.mark.modules_on("npathi")
async def test_a_messenger_hold_never_brings_back_an_answer_removed_meanwhile(state, npat,
                                                                             monkeypatch):
    from harvey.agents.messenger import WAIT_HOURS, Hold, MessageSender

    await _ready(state, npat)
    _, pid = await _firm(state)
    item = await _answer(state, await _convo(state, pid), pid)

    async def removed_meanwhile(self, row, client=None):
        # (You remove it while the messenger decides; it would wait for the
        # sending hours, approved.)
        await state.remove_outbox_item(row["id"], by="dashboard")
        return Hold("approved", WAIT_HOURS, "hours")

    monkeypatch.setattr(MessageSender, "blocked", removed_meanwhile)
    assert await _sender(state, npat).run() == 0
    assert (await _row(state, item))["status"] == "removed" and npat.sent == []


@pytest.mark.asyncio
async def test_the_note_added_at_the_drain_never_overwrites_a_text_you_edited(state,  # noqa: F811
                                                                             monkeypatch):
    _, _, sender, _, _, rows = await _staged(state, monkeypatch)
    item = rows[1]["id"]
    await state.approve_outbox(item)
    read = await _row(state, item)
    edited = "Bună ziua, un text nou scris de tine."
    assert await state.edit_outbox_item(item, read["subject"], edited, was=read,
                                        status="approved", approved_by="you", error="") == 1
    # (The drain's write as it would make it from what it read before the edit.)
    assert await state.update_outbox_item(item, body=read["body"] + "\n\nsursa",
                                          if_updated=read["updated_at"]) == 0
    assert (await _row(state, item))["body"] == edited


# ── Autopilot and the drain's page after a removed step ──

@pytest.mark.asyncio
async def test_autopilot_approves_a_follow_up_once_its_redrafted_step_went(state,  # noqa: F811
                                                                          monkeypatch):
    cfg, cid, sender, _, writer, rows = await _staged(state, monkeypatch)
    step2 = rows[2]["id"]
    await state.update_outbox_item(step2, send_at="2026-01-01T09:00:00")
    assert await state.remove_outbox_item(rows[1]["id"], by="dashboard") == 1
    far = "2099-01-01T00:00:00"
    # The step before it was removed and isn't back yet: it waits.
    assert await state.promote_for_autopilot(far, 10) == 0
    assert step2 not in {r["id"] for r in await state.get_outbox(
        status="pending_review", skip_unready_follow_ups=True)}
    await cycle(state, cfg, sender, writer).run()
    [again] = [r for r in await state.get_outbox(status="pending_review")
               if r["step"] == 1]
    # Drafted again, not gone yet: still waits.
    await state.update_outbox_item(again["id"], send_at=far)
    assert await state.promote_for_autopilot("2026-06-01T00:00:00", 10) == 0
    await state.update_outbox_item(again["id"], status="sent", sent_at="2026-01-02T09:00:00",
                                   message_id="<m2@example.com>")
    # It went (the removed row beside it is only the record): the follow-up goes on.
    assert step2 in {r["id"] for r in await state.get_outbox(
        status="pending_review", skip_unready_follow_ups=True)}
    assert await state.promote_for_autopilot(far, 10) == 1
    assert (await _row(state, step2))["status"] == "approved"


@pytest.mark.asyncio
async def test_an_approved_row_with_no_approver_on_file_can_still_be_edited(state):
    _, pid = await _firm(state)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="ana@alfa.example.com", subject="Re: o întrebare",
        body="Mulțumesc.", send_at="2026-01-01T09:00:00", kind="reply", status="approved")
    with sqlite3.connect(state.db_path) as con:
        con.execute("UPDATE outbox SET approved_by = NULL WHERE id = ?", (item,))
    row = await _row(state, item)
    assert await state.edit_outbox_item(item, "Re: o întrebare", "Alt text.", was=row,
                                        status="pending_review", approved_by="",
                                        error="") == 1
