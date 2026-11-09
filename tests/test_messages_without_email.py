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
        # Nor rewritten: an email draft is the Email module's.
        r = client.post(f"/api/outbox/{item}/edit", json={"subject": "Alt", "body": "Alt text."})
        assert r.status_code == 409 and r.json()["off_module"] == "email_outreach"
        assert asyncio.run(_row(_db(env), item))["body"] == "Un email de dinainte."
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


# ── What only the Email module does: refused while it is off ──


def _email_on_for(fn):
    """``fn()`` run as if the Email module were on too (a test's setup:
    what it made stays when the module goes off again)."""
    real = switches.resolve
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(switches, "resolve", lambda stored, mans=None: real(
            {**stored, switches.KEY.format(switches.EMAIL_MODULE): switches.ON}, mans))
        return fn()


def test_email_problem_names_the_email_module():
    assert switches.email_problem({"email_outreach"}, "A new campaign") is None
    off = switches.email_problem({"npathi"}, "A new campaign")
    assert isinstance(off, switches.ModuleOff) and off.module_id == "email_outreach"
    assert str(off).startswith("A new campaign needs the Email campaigns module")


def _off(r):
    assert r.status_code == 409, r.json()
    assert r.json()["off_module"] == "email_outreach" and r.json()["success"] is False
    return r


@pytest.mark.modules_on("npathi")
def test_campaign_work_is_refused_with_email_off_and_its_history_stays_open(client, env,
                                                                            monkeypatch):
    import harvey.dashboard as dash
    from tests.test_campaigns import NOTE, SEQUENCE, config
    from tests.test_dashboard_campaigns import _seed

    cfg = config()
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)
    _seed(env["db"], "office@alfa.example.com", "office@beta.example.com")

    def setup():
        aid = client.post("/api/audiences", json={"name": "Alfa", "query": "q=alfa"}).json()["id"]
        draft = client.post("/api/campaigns", json={"name": "Ciornă", "audience_id": aid,
                                                    "li_note": NOTE, "sequence": SEQUENCE})
        paused = client.post("/api/campaigns", json={"name": "Pornită", "audience_id": aid,
                                                     "li_note": NOTE, "sequence": SEQUENCE})
        pid = paused.json()["id"]
        assert client.post(f"/api/campaigns/{pid}/start").json()["success"]
        assert client.post(f"/api/campaigns/{pid}/pause").json()["success"]
        return aid, draft.json()["id"], pid
    aid, cid, pid = _email_on_for(setup)

    def no_model(*a, **k):
        raise AssertionError("the model was asked with the Email module off")
    monkeypatch.setattr(dash, "_campaign_writer", no_model)

    _off(client.post("/api/audiences", json={"name": "Beta", "query": "q=beta"}))
    _off(client.post("/api/campaigns", json={"name": "Alta", "li_note": NOTE}))
    _off(client.patch(f"/api/campaigns/{cid}", json={"name": "Redenumită"}))
    _off(client.post(f"/api/campaigns/{cid}/templates"))
    _off(client.put(f"/api/campaigns/{cid}/templates", json={"sequence": SEQUENCE}))
    _off(client.post(f"/api/campaigns/{cid}/members", json={"emails": ["office@beta.example.com"]}))
    _off(client.post(f"/api/campaigns/{cid}/start"))
    _off(client.post(f"/api/campaigns/{pid}/resume"))
    _off(client.post("/api/acknowledgement", json={"typed": "accept"}))
    _off(client.post("/api/sending-mode", json={"mode": "autopilot", "confirm": "autopilot"}))
    _off(client.post("/api/prospects/p-x/sent", json={"sent": True}))
    _off(client.patch(f"/api/audiences/{aid}", json={"name": "Redenumit"}))

    # Nothing changed, and what it did stays readable and exportable.
    listed = {c["id"]: c for c in client.get("/api/campaigns").json()}
    assert listed[cid]["status"] == "draft" and listed[cid]["name"] == "Ciornă"
    assert listed[pid]["status"] == "paused"
    assert [a["name"] for a in client.get("/api/audiences").json()["audiences"]] == ["Alfa"]
    assert client.get(f"/api/campaigns/{pid}").status_code == 200
    assert client.get(f"/api/campaigns/{pid}/record.csv").status_code == 200
    assert client.get(f"/api/audiences/{aid}/preview").status_code == 200
    # Stopping and archiving (the safe way) stay open.
    assert client.post(f"/api/campaigns/{pid}/stop").json()["campaign"]["status"] == "finished"
    assert client.post(f"/api/campaigns/{cid}/archive").json()["campaign"]["status"] == "archived"
    assert client.patch(f"/api/audiences/{aid}", json={"archived": True}).json()["success"]
    assert client.get("/api/audiences").json()["audiences"] == []


@pytest.mark.modules_on("npathi")
def test_the_cli_approves_a_text_only_when_the_gate_would_send_it(tmp_path, monkeypatch):
    """``harvey outbox --approve`` checks an SMS answer's text as the
    dashboard's Approve does: one the sender would block stays waiting."""
    import harvey.config as config_mod
    from tests.test_module_switches import _cli

    db = tmp_path / "harvey.db"
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: SendCfg())

    async def seed():
        s = StateManager(db_path=str(db))
        await s.init_db()
        bad = await _answer(s, "c-1", "p-1", body="Salut {nume}", approved=False)
        good = await _answer(s, "c-2", "p-2", approved=False)
        return bad, good

    bad, good = asyncio.run(seed())
    code, out = _cli(monkeypatch, db, "outbox", "--approve", bad)
    assert code == 1 and "would block this message" in out and "curly braces" in out
    assert asyncio.run(_row(StateManager(db_path=str(db)), bad))["status"] == "pending_review"
    code, out = _cli(monkeypatch, db, "outbox", "--approve", good)
    assert code == 0 and "Approved." in out
    assert asyncio.run(_row(StateManager(db_path=str(db)), good))["status"] == "approved"


# ── The pages' words with the Email module off ──


LEDES = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {'today-lede': {dataset: {}}, 'outbox-lede': {dataset: {}}};
global.document = {getElementById: id => els[id] || null, querySelectorAll: () => []};
global.currentTab = 'today';
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext('let _modules;' + pick('function applyModules() {', 'async function loadModules()'));
const out = {};
for (const [name, mods] of Object.entries({email: {email: true, messages: true},
                                            messages: {email: false, messages: true},
                                            none: {email: false, messages: false}})) {
  vm.runInThisContext('_modules = ' + JSON.stringify(mods));
  applyModules();
  out[name] = {today: els['today-lede'].textContent, outbox: els['outbox-lede'].innerHTML};
}
process.stdout.write(JSON.stringify(out));
'''


@pytest.mark.parametrize("lang", ["ro", "en"])
def test_the_ledes_speak_of_messages_with_email_off(lang):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parent.parent / "harvey" / "web"
    res = subprocess.run([node, "-e", LEDES, str(web / "app.js"), str(web / "i18n.js"), lang],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-3000:]
    out = json.loads(res.stdout)
    assert "email" in out["email"]["today"].lower() and "mail" in out["email"]["outbox"].lower()
    for page in ("today", "outbox"):
        assert "mail" not in out["messages"][page].lower(), out["messages"][page]
    assert ("mesaje" if lang == "ro" else "messages") in out["messages"]["today"]
    assert "SMS" in out["messages"]["outbox"]
    assert "mail" not in out["none"]["today"].lower()


@pytest.mark.modules_on("npathi")
def test_the_cli_refuses_the_email_modules_work_with_it_off(tmp_path, monkeypatch, capsys):
    """``harvey campaigns new|start|resume|add``, ``harvey audiences save``,
    ``harvey ack`` and ``harvey sending accept-gap`` are refused with the
    Email module off, as the dashboard refuses them (a start would check
    addresses at a verifier); listing, pausing and the record stay open."""
    import builtins
    import sqlite3

    import harvey.config as config_mod
    from harvey.state import READ_GAP_KEY
    from tests.test_campaigns import NOTE, SEQUENCE, config, contact
    from tests.test_module_switches import _cli

    db = tmp_path / "harvey.db"
    cfg = config()
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "accept")
    sequence = tmp_path / "sequence.json"
    sequence.write_text(json.dumps(SEQUENCE), encoding="utf-8")

    async def seed():
        s = StateManager(db_path=str(db))
        await s.init_db()
        await contact(s, "office@alfa.example.com")
        await s.set_setting(READ_GAP_KEY, json.dumps(
            {"since": 1, "covered_from": 2, "reason": "a test gap",
             "mailbox": "fake:h@x.example.com"}))
    asyncio.run(seed())

    def setup():
        code, out = _cli(monkeypatch, db, "audiences", "save", "Alfa", "--q", "alfa")
        assert code == 0, out
        code, out = _cli(monkeypatch, db, "campaigns", "new", "--name", "Alfa", "--audience",
                         "1", "--note", NOTE, "--sequence-file", str(sequence))
        assert code == 0, out
        return out.split("Draft campaign ")[1].split()[0]
    cid = _email_on_for(setup)

    def counts():
        con = sqlite3.connect(db)
        try:
            return (con.execute("SELECT COUNT(*) FROM audience").fetchone()[0],
                    con.execute("SELECT COUNT(*), MAX(status) FROM campaigns").fetchone(),
                    con.execute("SELECT COUNT(*) FROM campaign_member").fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM settings WHERE key = ?",
                                (READ_GAP_KEY,)).fetchone()[0])
        finally:
            con.close()
    before = counts()
    for argv in (("campaigns", "new", "--name", "Beta", "--audience", "1", "--note", NOTE),
                 ("campaigns", "start", cid), ("campaigns", "resume", cid),
                 ("campaigns", "add", cid, "office@alfa.example.com"),
                 ("audiences", "save", "Beta", "--q", "beta"), ("ack",),
                 ("sending", "accept-gap")):
        code, out = _cli(monkeypatch, db, *argv)
        assert code == 1 and "Email campaigns module (email_outreach), which is off" in out, argv
    assert counts() == before
    assert asyncio.run(StateManager(db_path=str(db)).acknowledgement_status())["given"] is False
    code, out = _cli(monkeypatch, db, "campaigns", "list")
    assert code == 0 and "Alfa" in out
    code, out = _cli(monkeypatch, db, "audiences", "list")
    assert code == 0 and "Alfa" in out
    code, out = _cli(monkeypatch, db, "ack", "--status")
    assert code == 0 and "Not confirmed yet" in out
    code, out = _cli(monkeypatch, db, "campaigns", "export", cid, "--out",
                     str(tmp_path / "record.json"))
    assert code == 0 and (tmp_path / "record.json").exists()


@pytest.mark.modules_on("npathi")
def test_a_read_gap_is_not_accepted_on_the_dashboard_with_email_off(client, env):
    from harvey.state import READ_GAP_KEY

    asyncio.run(_db(env).set_setting(READ_GAP_KEY, json.dumps(
        {"since": 1, "covered_from": 2, "reason": "a test gap",
         "mailbox": "fake:h@x.example.com"})))
    _off(client.post("/api/mail/read-gap/accept", json={"typed": "accept"}))
    assert asyncio.run(_db(env).read_gap()) is not None


def test_the_campaign_pages_say_a_module_refusal_in_the_viewers_language():
    """The Campaigns and Contacts actions show a refusal through failMsg,
    which words ``off_module`` in RO or EN, not the server's English."""
    from pathlib import Path

    app = (Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js").read_text(
        encoding="utf-8")
    assert "failMsg(res, t('camp.save_failed'))" in app
    assert "(res && res.message) || t('camp.save_failed')" not in app
    assert "res && res.off_module ? failMsg(res) : t('cw.ack_failed')" in app
