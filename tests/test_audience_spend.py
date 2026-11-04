"""The Email module's address checks under the install's spend limits
(harvey/agents/audience.py, the campaign cycle, verify_costs.InstallSpend):
a priced check is reserved from spend.per_day_usd / per_month_usd and
recorded in the job-less ledger jobs.spent_since reads with the jobs' own;
past a free tier with no price it is refused while any limit is set and
the address waits, held with why, until it could be asked; with no limit
it is made and counted unpriced; a free one is never refused. Every
verifier is a fake; all data is fictional."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import campaigns, verify_costs
from harvey.agents.campaigner import CampaignCycle
from harvey.config import HarveyConfig
from harvey.modules import jobs, switches
from tests.test_campaigns import BASE, StubWriter, contact, make_sender, new_campaign
from tests.test_contact_finder import _state
from tests.test_verify_costs import _network, _spec

EMAIL = "office@alfa.example.com"
ENV = {"reoon_api_key": "k"}


def run(coro):
    return asyncio.run(coro)


def _config(**spend):
    return HarveyConfig(**BASE, spend=spend, channels={"email": {
        "provider": "gmail", "require_approval": True}})


@pytest.fixture
def verify_on(monkeypatch):
    async def enabled_ids(state_):
        return frozenset({"email_verify"})
    monkeypatch.setattr(switches, "enabled_ids", enabled_ids)


async def _campaign(s, cfg):
    await contact(s, EMAIL, status="unchecked")
    cid, _ = await new_campaign(s, cfg)
    return cid


async def _cycle(s, cfg, monkeypatch):
    sender, _ = make_sender(s, cfg, monkeypatch)
    return await CampaignCycle(s, cfg, ENV, StubWriter(), sender).run()


async def _status(s, cid):
    return (await s.campaign_member(cid, EMAIL))["status_name"]


# ── The ledger: spend no job made, counted with the jobs' ──

def test_the_ledger_adds_up_with_the_jobs_and_counts_by_day_and_month(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        batch = await jobs.new_batch(s, "find_contacts", "t")
        job_id = await jobs.enqueue(s, "find_contacts", "contacts", {"company_id": "1"},
                                    batch_id=batch)
        await jobs.save_progress(s, job_id, {}, actual_cost=0.5)
        async with s._connect() as db:
            await db.execute("UPDATE jobs SET started_at = datetime('now') WHERE id = ?",
                             (job_id,))
            await db.commit()
        await jobs.record_spend(s, 0.004)
        await jobs.record_spend(s, 0.006)
        await jobs.record_spend(s, 0.0)                     # (nothing written)
        await s.set_setting(jobs.LEDGER_KEY + "2000-01-01", "9")    # another month
        # A classifier row (its job already carries its cost) isn't counted again.
        await s.record_usage_event(agent="classify", task="classify.site", cost_usd=3.0,
                                   provider="openai_compatible")
        return (await jobs.spent_since(s, "start of day"),
                await jobs.spent_since(s, "start of month"))

    day, month = run(go())
    assert day == pytest.approx(0.51) and month == pytest.approx(0.51)


def test_a_jobs_reservation_sees_what_the_audience_spent(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        await jobs.record_spend(s, 0.99)
        batch = await jobs.new_batch(s, "find_contacts", "t")
        me = await jobs.get(s, await jobs.enqueue(s, "find_contacts", "contacts",
                                                  {"company_id": "1"}, batch_id=batch))
        with pytest.raises(jobs.SpendLimit, match="for the day"):
            await jobs.reserve(s, me, 0.02, 0.02, per_day=1.0)

    run(go())


# ── InstallSpend through the Meter ──

def test_a_priced_check_is_reserved_and_recorded_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0, 0.004)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        spend = verify_costs.InstallSpend(s, per_day=1.0)
        meter = await verify_costs.Meter.start(s, spend)
        made = await meter.ask("reoon")
        return made, spend.spent, await jobs.spent_since(s, "start of day")

    made, spent, day = run(go())
    assert made is True and spent == pytest.approx(0.004) and day == pytest.approx(0.004)


def test_with_no_limit_a_priced_check_is_still_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0, 0.004)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s, verify_costs.InstallSpend(s))
        return await meter.ask("reoon"), await jobs.spent_since(s, "start of month")

    made, month = run(go())
    assert made is True and month == pytest.approx(0.004)


def test_the_limit_reached_refuses_a_priced_check_and_never_a_free_one(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "reoon": _spec(0, 0.004), "zerobounce": _spec(100)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        await jobs.record_spend(s, 2.0)
        spend = verify_costs.InstallSpend(s, per_day=5.0, per_month=2.0)
        meter = await verify_costs.Meter.start(s, spend)
        blocked = await meter.blocked("reoon")
        paid, free = await meter.ask("reoon"), await meter.ask("zerobounce")
        zero = verify_costs.InstallSpend(s, per_day=0.0)
        free_at_zero = await (await verify_costs.Meter.start(s, zero)).ask("zerobounce")
        return blocked, paid, free, free_at_zero, meter.refused, \
            await verify_costs.used(s, "reoon")

    blocked, paid, free, free_at_zero, refused, used = run(go())
    assert blocked == "the spend limit for the month ($2.00) is reached"
    assert (paid, free, free_at_zero, used) == (False, True, True, 0)
    assert refused == [("reoon", blocked)]
    assert verify_costs.hold_code(blocked) == "month"


def test_two_priced_checks_at_once_never_both_take_the_last_room(tmp_path, monkeypatch):
    # Room for one $0.004 request under the day's limit: two asked at once
    # (each its own InstallSpend, as two cycles would be) make one.
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0, 0.004)})

    async def go():
        s = await _state(tmp_path / "harvey.db")

        async def one():
            meter = await verify_costs.Meter.start(s, verify_costs.InstallSpend(s, per_day=0.005))
            return await meter.ask("reoon")

        made = await asyncio.gather(*(one() for _ in range(4)))
        return sorted(made), await jobs.spent_since(s, "start of day"), \
            await verify_costs.used(s, "reoon")

    made, day, used = run(go())
    assert made == [False, False, False, True]
    assert day == pytest.approx(0.004) and used == 1


def test_past_a_free_tier_with_no_price_is_refused_under_any_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        out = []
        for limits in ({"per_day": 10.0}, {"per_month": 10.0}, {}):
            meter = await verify_costs.Meter.start(s, verify_costs.InstallSpend(s, **limits))
            out.append((await meter.ask("reoon"), dict(meter.unpriced)))
        return out

    assert run(go()) == [(False, {}), (False, {}), (True, {"reoon": 1})]
    assert verify_costs.hold_code("no price for reoon past its free tier") == "no_price"


# ── The campaign cycle ──

def test_an_unpriced_check_waits_held_until_a_price_is_entered(tmp_path, monkeypatch,
                                                               verify_on):
    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0)})
    cfg = _config(per_day_usd=1.0)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _campaign(s, cfg)
        await _cycle(s, cfg, monkeypatch)
        first = await _status(s, cid), await campaigns.checks_held(s)
        await _cycle(s, cfg, monkeypatch)               # (held: nothing asked)
        again = list(asked)
        await verify_costs.set_price(s, "reoon", 0.004)
        await _cycle(s, cfg, monkeypatch)
        return first, again, await _status(s, cid), await campaigns.checks_held(s), \
            await jobs.spent_since(s, "start of day")

    (status, held), again, after, cleared, day = run(go())
    assert status == "waiting" and held["code"] == "no_price"
    assert held["services"] == ["reoon"] and "no price for reoon" in held["why"]
    assert again == []
    assert asked == ["reoon"] and after in ("ready", "staged") and cleared is None
    assert day == pytest.approx(0.004)


def test_with_no_limit_an_unpriced_check_is_made_as_before(tmp_path, monkeypatch, verify_on):
    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0)})
    cfg = _config()

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _campaign(s, cfg)
        await _cycle(s, cfg, monkeypatch)
        return await _status(s, cid), await campaigns.checks_held(s), \
            await verify_costs.used(s, "reoon")

    status, held, used = run(go())
    assert asked == ["reoon"] and status in ("ready", "staged")
    assert held is None and used == 1


def test_the_days_limit_reached_holds_the_checks_until_it_has_room(tmp_path, monkeypatch,
                                                                   verify_on):
    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0, 0.004)})
    cfg = _config(per_day_usd=0.5)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _campaign(s, cfg)
        await jobs.record_spend(s, 0.5)                 # (the finder's run, say)
        await _cycle(s, cfg, monkeypatch)
        held = await _status(s, cid), await campaigns.checks_held(s), \
            await verify_costs.used(s, "reoon")
        roomy = _config(per_day_usd=1.0)
        await _cycle(s, roomy, monkeypatch)
        return held, await _status(s, cid), await campaigns.checks_held(s)

    (status, held, used), after, cleared = run(go())
    assert status == "waiting" and held["code"] == "day" and used == 0
    assert asked == ["reoon"] and after in ("ready", "staged") and cleared is None


def test_a_check_a_refused_service_left_undecided_waits(tmp_path, monkeypatch, verify_on):
    # ZeroBounce (free) can't decide, Reoon has no price under a limit:
    # not a check done, nothing kept on the contact.
    asked = _network(monkeypatch, zerobounce={"status": "unknown"}, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "zerobounce": _spec(100), "reoon": _spec(0)})
    cfg = _config(per_month_usd=3.0)
    env = {"zerobounce_api_key": "k", **ENV}

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _campaign(s, cfg)
        sender, _ = make_sender(s, cfg, monkeypatch)
        for _ in range(2):
            await CampaignCycle(s, cfg, env, StubWriter(), sender).run()
        rows = await s.read("audience_rows", None, [EMAIL])
        return await _status(s, cid), await campaigns.checks_held(s), rows[0]["status"]

    status, held, kept = run(go())
    assert asked == ["zerobounce"]      # (the second cycle asked nothing)
    assert status == "waiting" and held["services"] == ["reoon"] and kept == "unchecked"


# ── Where it shows ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def test_today_and_the_campaign_page_say_why_addresses_wait(client, monkeypatch):
    cfg = _config(per_day_usd=1.0)

    async def go():
        s = await _state(client.db_path)
        cid = await _campaign(s, cfg)
        await campaigns.hold_checks(s, {"code": "day", "why": "the spend limit for the day "
                                        "($1.00) is reached", "services": ["reoon"]})
        return cid

    cid = run(go())
    today = client.get("/api/today").json()
    item = next(i for i in today["items"] if i["key"] == "campaign-checks")
    assert item["code"] == "day" and "spend.per_day_usd" in item["detail"]
    assert today["stats"]["campaign_checks_waiting"] == 1
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["checks_held"]["code"] == "day"


def test_every_reason_has_its_words_in_both_languages():
    import pathlib
    import re

    text = (pathlib.Path(dash.__file__).parent / "web" / "i18n.js").read_text()
    for key in ("today.item.campaign_checks", "today.item.campaign_checks_action",
                "cp.checks_held", "cp.checks_held.no_price", "cp.checks_held.day",
                "cp.checks_held.month"):
        assert len(re.findall(r'"' + re.escape(key) + r'":', text)) == 2, key
    assert set(dash._CHECKS_HELD) == {"no_price", "day", "month"}
