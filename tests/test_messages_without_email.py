"""The Outbox and Conversations without the Email module.

They are shared: shown while any module with message channels is on (the
manifests' ``channels``: email_outreach's email, npathi's SMS and WhatsApp),
and each row keeps its own channel's rules. With only nPathi on, an SMS is
read, drafted, approved and sent, and an answer written on Conversations
goes; an email left in the Outbox from before stays listed and rejectable,
and is never approved. Every number, firm and person is invented; nPathi is
the fake one of tests/test_npathi.py, nothing reaches the network.
"""

import asyncio
import dataclasses
import json

import pytest

from harvey.integrations import npathi
from harvey.modules import switches
from harvey.modules.loader import cross_check
from harvey.state import StateManager
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)
from tests.test_npathi import ANN, Env, _message, _no_pace, fake, state  # noqa: F401
from tests.test_npathi_send import (  # noqa: F401  (fixtures)
    BOB, LoopBrain, LoopCfg, SendCfg, _answer, _beta, _convo, _firm, _loop, _no_quiet_hours,
    _ready, _row, _seed_convo, _sender, awake, dash_npat, npat,
)


def _db(env):
    return StateManager(db_path=str(env["db"]))


def _email_row(env, status="pending_review"):
    """A cold email queued while the Email module was on."""
    async def add():
        return await _db(env).add_outbox_item(
            prospect_id="p-old", kind="sequence", step=1, to_email="ion@beta.example.com",
            subject="Salut", body="Un email de dinainte.", send_at="2026-01-01T09:00:00",
            status=status)
    return asyncio.run(add())


# ── The manifests: who owns which channel ──


def test_each_channel_belongs_to_one_module():
    assert switches.channel_owners() == {"email": "email_outreach", "sms": "npathi",
                                         "waba": "npathi", "wa": "npathi"}
    assert set(switches.by_id()["npathi"].channels) == set(npathi.CHANNELS)
    assert "email_outreach" not in switches.by_id()["npathi"].requires


def test_a_channel_declared_twice_is_a_manifest_problem():
    mans = list(switches.shipped())
    osm = next(m for m in mans if m.id == "osm")
    mans[mans.index(osm)] = dataclasses.replace(osm, channels=("sms",))
    assert any("channel 'sms' is already declared" in p for p in cross_check(mans))


@pytest.mark.parametrize("on, shown", [(set(), False), ({"email_outreach"}, True),
                                       ({"npathi"}, True), ({"osm", "phones"}, False)])
def test_the_pages_show_with_any_channels_module(on, shown):
    assert switches.messaging_shown(on) is shown


def test_a_row_waits_for_its_own_channels_module():
    assert switches.channel_problem({"npathi"}, "sms") is None
    off = switches.channel_problem({"npathi"}, "email")
    assert isinstance(off, switches.ModuleOff) and off.module_id == "email_outreach"
    assert switches.channel_problem({"email_outreach"}, "") is None         # blank: email
    assert switches.channel_problem({"email_outreach"}, "wa").module_id == "npathi"
    assert "No module" in str(switches.channel_problem({"npathi"}, "fax"))


@pytest.mark.asyncio
async def test_email_can_go_off_while_npathi_stays_on(state):
    await switches.set_enabled(state, "email_verify", True)
    await switches.set_enabled(state, "email_outreach", True)
    await switches.set_enabled(state, "npathi", True)
    await switches.set_enabled(state, "email_outreach", False)          # not refused
    assert {"npathi"} <= await switches.enabled_ids(state)
    assert "email_outreach" not in await switches.enabled_ids(state)


# ── Email off, nPathi on ──


@pytest.mark.modules_on("npathi")
class TestOnlyNpathi:
    def test_the_pages_are_shown_and_email_is_not(self, client):
        assert client.get("/api/ui/modules").json()["modules"] == {"email": False,
                                                                   "messages": True}

    @pytest.mark.asyncio
    async def test_a_message_is_read_drafted_and_sent_with_email_off(self, state, npat, awake):
        await _ready(state, npat)
        await _firm(state)
        await _beta(state)
        npat.messages = [_message(7, BOB, "Bună ziua, cât costă?")]
        out = await _loop(state, npat, LoopCfg(), LoopBrain()).run("test-pass")
        assert out == {"read": True, "sent": 0}
        [draft] = await state.get_outbox(status="pending_review", channel="sms")
        assert draft["to_phone"] == BOB and draft["approved_by"] == ""
        assert await state.approve_outbox(draft["id"]) == 1          # (the user, in the Outbox)
        assert (await _loop(state, npat, LoopCfg(), LoopBrain()).run("test-pass"))["sent"] == 1
        assert (await _row(state, draft["id"]))["status"] == "sent"

    def test_an_sms_draft_is_approved_on_the_outbox_page(self, client, env, dash_npat,
                                                          monkeypatch):
        import harvey.dashboard as dash

        # (The firm the first answer is signed with.)
        monkeypatch.setattr(dash, "_load_config_quietly", lambda: SendCfg())
        convo = _seed_convo(env, dash_npat)

        async def seed():
            db = _db(env)
            pid = (await db.get_conversation(convo)).prospect_id
            return await _answer(db, convo, pid, approved=False)

        item = asyncio.run(seed())
        [row] = client.get("/api/outbox").json()["pending"]
        assert row["id"] == item and "off_module" not in row
        r = client.post(f"/api/outbox/{item}/approve")
        assert r.status_code == 200 and r.json()["success"], r.json()
        assert asyncio.run(_sender(_db(env), dash_npat).run()) == 1
        assert asyncio.run(_row(_db(env), item))["status"] == "sent"

    def test_an_answer_you_write_goes(self, client, env, dash_npat, monkeypatch):
        _no_quiet_hours(monkeypatch)
        convo = _seed_convo(env, dash_npat)
        [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
        assert row["can_answer"]
        r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
        assert r.status_code == 200 and r.json()["status"] == "sent", r.json()
        assert len(dash_npat.sent) == 1

    def test_an_email_from_before_is_listed_and_never_approved(self, client, env):
        item = _email_row(env)
        [row] = client.get("/api/outbox").json()["pending"]
        assert row["id"] == item and row["off_module"] == "email_outreach"
        r = client.post(f"/api/outbox/{item}/approve")
        assert r.status_code == 409 and r.json()["off_module"] == "email_outreach"
        assert "Email campaigns" in r.json()["message"]
        r = client.post("/api/outbox/approve-all")
        assert r.status_code == 409 and r.json()["off_module"] == "email_outreach"
        assert asyncio.run(_row(_db(env), item))["status"] == "pending_review"
        assert client.post(f"/api/outbox/{item}/reject").json()["success"]
        assert asyncio.run(_row(_db(env), item))["status"] == "rejected"

    def test_today_marks_what_is_about_email_only(self, client, env, dash_npat):
        convo = _seed_convo(env, dash_npat)

        async def seed():
            db = _db(env)
            pid = (await db.get_conversation(convo)).prospect_id
            await _answer(db, convo, pid, approved=False)
            await db.set_setting("mail_read_gap", json.dumps({"since": 1, "reason": "x"}))
        asyncio.run(seed())
        today = client.get("/api/today").json()
        keys = {i["key"]: i for i in today["items"]}
        assert keys["outbox"].get("module") is None and today["stats"]["messages_pending"] == 1
        assert keys["outbox"]["title"] == "1 item waiting for approval"
        assert keys["read-gap"]["module"] == "email"

    def test_today_never_says_autopilot_with_email_off(self, client, env, dash_npat,
                                                        monkeypatch):
        """Autopilot is the Email module's: an answer on another channel is
        never approved by it, so Today doesn't say it sends the rest."""
        import harvey.dashboard as dash

        monkeypatch.setattr(dash, "_require_approval_now", lambda: False)   # (autopilot on)
        convo = _seed_convo(env, dash_npat)

        async def seed():
            db = _db(env)
            pid = (await db.get_conversation(convo)).prospect_id
            await _answer(db, convo, pid, approved=False)
        asyncio.run(seed())
        [item] = [i for i in client.get("/api/today").json()["items"] if i["key"] == "outbox"]
        assert "Autopilot" not in item["detail"]
        assert item["detail"].startswith("Nothing sends until you approve it")

    def test_the_kill_switch_holds_an_answer_you_write(self, client, env, dash_npat,
                                                        monkeypatch):
        _no_quiet_hours(monkeypatch)
        convo = _seed_convo(env, dash_npat)
        assert client.post("/api/sending/pause").json()["success"]
        r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
        assert r.status_code == 200 and r.json()["wait"] == "paused", r.json()
        assert dash_npat.sent == []


# ── Email on, nPathi off ──


@pytest.mark.modules_on("email_outreach")
def test_with_npathi_off_an_sms_row_is_not_approved_and_email_is(client, env, dash_npat):
    convo = _seed_convo(env, dash_npat)

    async def seed():
        db = _db(env)
        pid = (await db.get_conversation(convo)).prospect_id
        return await _answer(db, convo, pid, approved=False)

    sms = asyncio.run(seed())
    email = _email_row(env)
    assert client.get("/api/ui/modules").json()["modules"] == {"email": True, "messages": True}
    pending = {r["id"]: r for r in client.get("/api/outbox").json()["pending"]}
    assert pending[sms]["off_module"] == "npathi" and "off_module" not in pending[email]
    r = client.post(f"/api/outbox/{sms}/approve")
    assert r.status_code == 409 and r.json()["off_module"] == "npathi"
    assert client.post(f"/api/outbox/{email}/approve").json() == {"success": True}
    [row] = [c for c in client.get("/api/conversations").json() if c["id"] == convo]
    assert row["can_answer"] is False
    r = client.post(f"/api/conversations/{convo}/answer", json={"body": "Vă sun mâine?"})
    assert r.status_code == 409 and r.json()["off_module"] == "npathi"


# ── Both off ──


def test_with_both_off_the_pages_are_hidden(client, env):
    assert client.get("/api/ui/modules").json()["modules"] == {"email": False, "messages": False}
    item = _email_row(env)
    assert client.post(f"/api/outbox/{item}/approve").status_code == 409


# ── The CLI ──


@pytest.mark.modules_on("npathi")
def test_the_cli_refuses_an_email_with_email_off(tmp_path, monkeypatch):
    from tests.test_module_switches import _cli

    db = tmp_path / "harvey.db"

    async def seed():
        s = StateManager(db_path=str(db))
        await s.init_db()
        return await s.add_outbox_item(
            prospect_id="p-old", kind="sequence", step=1, to_email="ion@beta.example.com",
            subject="Salut", body="Un email de dinainte.", send_at="2026-01-01T09:00:00",
            status="pending_review")

    item = asyncio.run(seed())
    code, out = _cli(monkeypatch, db, "outbox")
    assert code == 0 and "module is off" in out
    code, out = _cli(monkeypatch, db, "outbox", "--approve", item)
    assert code == 1 and "Email campaigns" in out
    code, out = _cli(monkeypatch, db, "outbox", "--approve-all")
    assert code == 1
    code, out = _cli(monkeypatch, db, "outbox", "--reject", item)
    assert code == 0 and "Rejected" in out
