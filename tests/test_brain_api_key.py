"""The brain on an API key (brain.auth: api_key): the key checked with a
free call and saved through Settings → Model's own flow, never shown back
or logged; passed to a Claude child as ANTHROPIC_API_KEY in that mode only;
the brain's USD caps refusing a call before it is made; Codex's tokens
priced, or refused under a cap with no price; and the Usage page and
``harvey usage`` showing money instead of the plan's windows. Every key is
a fake, every HTTP answer a stand-in (httpx.MockTransport), every CLI a
stand-in: nothing reaches a provider and no model is called."""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.brain as brain_mod
import harvey.dashboard as dash
from harvey import brain_auth, brain_spend, claude_runtime
from harvey.brain import Brain
from harvey.config import BrainConfig, load_brain_config
from harvey.state import StateManager

FAKE_KEY = "sk-ant-fake-key-for-tests-0000"
TEMPLATE = """# Harvey's settings (fictional)
persona:
  name: H
  company: Acme Example
brain:
  provider: claude
usage:
  max_daily_claude_percent: 80
"""


def run(coro):
    return asyncio.run(coro)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def _transport(status: int, seen: list):
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"data": []})
    return httpx.MockTransport(answer)


# ── The free check of a key ──

@pytest.mark.parametrize("status, ok, words", [
    (200, True, ""), (401, False, "refused this key (HTTP 401)"),
    (403, False, "refused this key (HTTP 403)"), (500, False, "answered HTTP 500"),
])
def test_a_key_is_checked_with_the_model_list_only(status, ok, words):
    seen = []
    why = run(brain_auth.check_key("claude", FAKE_KEY, transport=_transport(status, seen)))
    assert (why == "") is ok and words in why and FAKE_KEY not in why
    [req] = seen
    # A GET of the model list: no model call, nothing billed.
    assert req.method == "GET" and str(req.url) == brain_auth.ANTHROPIC_MODELS_URL
    assert req.headers["x-api-key"] == FAKE_KEY
    assert req.headers["anthropic-version"] == brain_auth.ANTHROPIC_VERSION


def test_a_codex_key_is_checked_against_openais_model_list():
    seen = []
    assert run(brain_auth.check_key("codex", "sk-fake-openai", transport=_transport(200, seen))) == ""
    assert str(seen[0].url) == brain_auth.OPENAI_MODELS_URL and seen[0].method == "GET"
    assert seen[0].headers["authorization"] == "Bearer sk-fake-openai"


def test_an_unreachable_provider_or_a_malformed_key_says_so_without_the_key():
    def down(request):
        raise httpx.ConnectError("no route", request=request)
    why = run(brain_auth.check_key("claude", FAKE_KEY, transport=httpx.MockTransport(down)))
    assert "Couldn't reach api.anthropic.com" in why and FAKE_KEY not in why
    seen = []
    for bad in ("", "two words", "x" * 600):
        assert "Paste the whole API key" in run(brain_auth.check_key(
            "claude", bad, transport=_transport(200, seen)))
    assert seen == []                                     # nothing sent for those


# ── Settings → Model's own flow ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    (tmp_path / "harvey.yaml").write_text(TEMPLATE, encoding="utf-8")
    env = tmp_path / ".env"
    monkeypatch.setattr(dash, "ENV_FILE", env)
    monkeypatch.setattr(brain_auth, "ENV_FILE", env)
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_resolve_config_file",
                        lambda: tmp_path / ("harvey.local.yaml" if (tmp_path / "harvey.local.yaml")
                                            .exists() else "harvey.yaml"))
    monkeypatch.setattr(dash, "_check_harvey_pid", lambda: None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.dir = tmp_path
        yield c


def test_the_claude_key_is_checked_saved_under_harveys_name_and_never_echoed(
        client, monkeypatch, caplog):
    checked = []

    async def check(provider, key, **kw):
        checked.append(provider)
        return "" if key == FAKE_KEY else "api.anthropic.com refused this key (HTTP 401): check it."
    monkeypatch.setattr(brain_auth, "check_key", check)
    caplog.set_level(logging.DEBUG)
    # A refused key: nothing written.
    r = client.post("/api/brain/key", json={"provider": "claude", "key": "sk-ant-wrong"})
    assert r.status_code == 400 and "refused this key" in r.json()["message"]
    assert not (client.dir / ".env").exists()
    # A good one: kept in .env under the Harvey-only name, never returned.
    r = client.post("/api/brain/key", json={"provider": "claude", "key": FAKE_KEY})
    assert r.status_code == 200 and r.json() == {"success": True, "provider": "claude",
                                                "key_set": True}
    env = (client.dir / ".env").read_text(encoding="utf-8")
    assert f"{brain_auth.CLAUDE_KEY_ENV}={FAKE_KEY}" in env and "ANTHROPIC_API_KEY" not in env
    assert checked == ["claude", "claude"]
    # Nothing the browser reads carries it: only that one is set.
    got = client.get("/api/brain")
    assert FAKE_KEY not in got.text and got.json()["key_set"]["claude"] is True
    for path in ("/api/settings", "/api/setup-status", "/api/usage"):
        assert FAKE_KEY not in client.get(path).text, path
    assert brain_auth.CLAUDE_KEY_ENV not in client.get("/api/settings").json()["env_keys"]
    # The generic .env editor can't write it.
    client.post("/api/settings/env", json={brain_auth.CLAUDE_KEY_ENV: "sk-ant-other"})
    assert brain_auth.claude_key() == FAKE_KEY
    # Now API-key mode can be chosen, with caps.
    r = client.post("/api/brain", json={"auth": "api_key", "usd_per_day": "2.5",
                                        "usd_per_month": ""})
    assert r.status_code == 200, r.text
    saved = load_brain_config(str(client.dir / "harvey.local.yaml"))
    assert (saved.auth, saved.usd_per_day, saved.usd_per_month) == ("api_key", 2.5, None)
    shown = client.get("/api/brain").json()
    assert shown["spend"]["usd_per_day"] == 2.5 and shown["spend"]["today_usd"] == 0
    # Unlink: the key cleared, the brain back on the terminal login.
    r = client.delete("/api/brain/key?provider=claude")
    assert r.status_code == 200 and r.json()["auth"] == "terminal"
    assert brain_auth.claude_key() == "" and not brain_auth.key_set("claude")
    assert load_brain_config(str(client.dir / "harvey.local.yaml")).auth == "terminal"
    assert FAKE_KEY not in caplog.text
    con = sqlite3.connect(client.dir / "harvey.db")
    logged = con.execute("SELECT details_json FROM actions WHERE action_type = 'brain_key'")
    logged = [row[0] for row in logged.fetchall()]
    con.close()
    assert len(logged) == 2 and not any(FAKE_KEY in d for d in logged)


def test_the_codex_key_goes_to_codex_and_unlink_removes_its_file(client, monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")

    async def check(provider, key, **kw):
        return ""
    linked = []

    async def link(key):
        # (Stands in for Codex's own login: it writes its file.)
        home = brain_auth.ensure_account_dir("codex")
        (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey",
                                                    "OPENAI_API_KEY": key}))
        (home / "auth.json").chmod(0o600)
        linked.append(True)
    monkeypatch.setattr(brain_auth, "check_key", check)
    monkeypatch.setattr(brain_auth, "link_codex_key", link)
    r = client.post("/api/brain/key", json={"provider": "codex", "key": "sk-fake-openai"})
    assert r.status_code == 200 and linked == [True]
    assert not (client.dir / ".env").exists()          # Harvey keeps no copy
    assert client.get("/api/brain").json()["key_set"] == {"claude": False, "codex": True}
    r = client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna",
                                        "auth": "api_key", "codex_usd_per_mtok_in": "1.25",
                                        "codex_usd_per_mtok_out": "10"})
    assert r.status_code == 200, r.text
    assert client.delete("/api/brain/key?provider=codex").json()["auth"] == "terminal"
    assert not (brain_auth.account_dir("codex") / "auth.json").exists()
    # A ChatGPT login there is never removed by Unlink.
    (brain_auth.account_dir("codex") / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
    assert brain_auth.unlink_codex_key() is False
    assert (brain_auth.account_dir("codex") / "auth.json").exists()


def test_the_key_endpoints_need_the_token(client):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as bare:
        assert bare.post("/api/brain/key", json={"provider": "claude",
                                                 "key": FAKE_KEY}).status_code in (401, 403)
        assert bare.delete("/api/brain/key?provider=claude").status_code in (401, 403)
    assert not (client.dir / ".env").exists()


# ── The key reaches a Claude child in API-key mode only ──

def test_the_key_is_injected_only_in_api_key_mode(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{brain_auth.CLAUDE_KEY_ENV}={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(brain_auth, "ENV_FILE", env_file)
    base = {"HOME": "/home/op", "CLAUDE_CONFIG_DIR": "/home/op/.claude",
            brain_auth.CLAUDE_KEY_ENV: FAKE_KEY, "ANTHROPIC_API_KEY": "sk-ant-from-env"}
    for safe in (True, False):
        for auth in ("terminal", "account"):
            env = claude_runtime.runtime_env("/tmp/x", base, safe=safe, auth=auth)
            assert "ANTHROPIC_API_KEY" not in env and brain_auth.CLAUDE_KEY_ENV not in env
            assert FAKE_KEY not in json.dumps(env)
        env = claude_runtime.runtime_env("/tmp/x", base, safe=safe, auth="api_key")
        assert env["ANTHROPIC_API_KEY"] == FAKE_KEY and brain_auth.CLAUDE_KEY_ENV not in env
        # Its own config folder: no other login there to be used instead.
        assert env["CLAUDE_CONFIG_DIR"] == str(brain_auth.key_dir())
    # No key saved: the folder alone, so the call fails as not logged in.
    env_file.write_text("", encoding="utf-8")
    env = claude_runtime.runtime_env("/tmp/x", base, auth="api_key")
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CONFIG_DIR"] == str(brain_auth.key_dir())


# ── The brain's USD caps ──

class _Proc:
    returncode = 0

    def __init__(self, cost):
        self.cost = cost

    async def communicate(self, data=None):
        return (json.dumps({"type": "result", "result": "ok", "session_id": "s",
                            "num_turns": 1, "total_cost_usd": self.cost,
                            "usage": {"input_tokens": 100, "output_tokens": 10}}).encode(), b"")


def _calls(monkeypatch, cost=0.25):
    started = []

    async def fake_exec(*cmd, **kwargs):
        started.append(kwargs["env"])
        return _Proc(cost)
    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    return started


async def _spent(state, usd, source="api_key"):
    await state.record_usage_event(agent="writer", task="t", cost_usd=usd, source=source)


@pytest.mark.asyncio
async def test_a_cap_reached_refuses_the_call_before_it_is_made(state, monkeypatch):
    started = _calls(monkeypatch)
    brain = Brain(state, BrainConfig(auth="api_key", usd_per_day=0.5))
    assert await brain.is_within_budget(max_percent=80) is True
    assert await brain.think("hi", max_retries=0) == "ok"
    # Its cost, as the CLI reported it, is the API key's.
    assert await brain_spend.spent(state, "day") == pytest.approx(0.25)
    await _spent(state, 0.3)                              # now $0.55 of $0.50
    assert await brain.is_within_budget(max_percent=80) is False
    assert "USD cap for the day ($0.50" in brain.budget_why and "$0.55 spent" in brain.budget_why
    assert await brain.think("hi", max_retries=0) == ""
    assert len(started) == 1                              # the second call never started
    # A month cap counts the same rows; 0 is no calls at all.
    zero = Brain(state, BrainConfig(auth="api_key", usd_per_month=0))
    assert await zero.is_within_budget() is False and "for the month" in zero.budget_why


@pytest.mark.asyncio
async def test_only_api_key_rows_count_and_the_call_counter_stays(state, monkeypatch):
    _calls(monkeypatch)
    # A subscription's notional cost and the classifier's own are not the brain's money.
    await _spent(state, 5.0, source="result_json")
    await _spent(state, 5.0, source="classify:priced")
    brain = Brain(state, BrainConfig(auth="api_key", usd_per_day=1.0))
    assert await brain_spend.spent(state, "day") == 0.0
    assert await brain.is_within_budget(max_daily_calls=1) is True
    await state.increment_usage()
    assert await brain.is_within_budget(max_daily_calls=1) is False
    assert "daily call limit" in brain.budget_why


@pytest.mark.asyncio
async def test_on_a_subscription_the_caps_and_money_dont_apply(state, monkeypatch):
    _calls(monkeypatch)
    await _spent(state, 9.0)
    brain = Brain(state, BrainConfig(usd_per_day=0.01))   # (set, but not in API-key mode)
    assert await brain.think("hi", max_retries=0) == "ok"
    assert await brain_spend.refusal(state, brain.config) == ""
    async with state._connect() as db:
        async with db.execute("SELECT source, cost_usd FROM usage_events WHERE agent = ''") as c:
            assert await c.fetchall() == [("result_json", 0.25)]


@pytest.mark.asyncio
async def test_codex_is_priced_from_its_tokens_or_refused_under_a_cap_with_no_price(state):
    cfg = BrainConfig(provider="codex", model="gpt-6-luna", auth="api_key", usd_per_day=1.0)
    assert await brain_spend.refusal(state, cfg) == brain_spend.NO_PRICE
    # No cap: an unpriced Codex call runs (and counts nothing).
    assert await brain_spend.refusal(state, cfg.model_copy(update={"usd_per_day": None})) == ""
    priced = cfg.model_copy(update={"codex_usd_per_mtok_in": 1.25, "codex_usd_per_mtok_out": 10})
    assert await brain_spend.refusal(state, priced) == ""
    brain = Brain(state, priced)
    assert brain.codex.api_key is True

    class Result:
        thread_id, model, duration_ms = "thr", "gpt-6-luna", 5
        usage = {"input_tokens": 600_000, "cache_read_tokens": 400_000,
                 "cache_creation_tokens": 0, "output_tokens": 100_000}
    cost = await brain._record_codex_usage(Result(), agent="writer", task="t", label="")
    # Every input token (cached too) at the input price: 1M x $1.25 + 0.1M x $10.
    assert cost == pytest.approx(2.25)
    assert await brain_spend.spent(state, "day") == pytest.approx(2.25)
    assert "USD cap for the day" in await brain_spend.refusal(state, priced)


@pytest.mark.asyncio
async def test_an_unpriced_codex_calls_tokens_count_once_a_price_is_entered(state):
    # (No cap, no price: it runs, and its tokens are kept, not read as $0
    # once the user sets a cap and the prices later the same month.)
    cfg = BrainConfig(provider="codex", model="gpt-6-luna", auth="api_key")
    brain = Brain(state, cfg)

    class Result:
        thread_id, model, duration_ms = "thr", "gpt-6-luna", 5
        usage = {"input_tokens": 900_000, "cache_read_tokens": 100_000,
                 "cache_creation_tokens": 0, "output_tokens": 100_000}
    assert await brain._record_codex_usage(Result(), agent="writer", task="t", label="") == 0.0
    priced = cfg.model_copy(update={"usd_per_day": 2.0, "codex_usd_per_mtok_in": 1.0,
                                    "codex_usd_per_mtok_out": 10.0})
    assert await brain_spend.spent(state, "day", priced) == pytest.approx(2.0)
    assert "USD cap for the day" in await brain_spend.refusal(state, priced)


class _TimedOut:
    returncode = None

    async def communicate(self, data=None):
        await asyncio.sleep(3600)

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return -9


@pytest.mark.asyncio
async def test_a_claude_call_that_times_out_with_an_api_key_is_counted_never_zero(
        state, monkeypatch):
    async def fake_exec(*cmd, **kwargs):
        return _TimedOut()
    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    brain = Brain(state, BrainConfig(auth="api_key", usd_per_day=1.0))
    assert await brain.think("hi", max_retries=0, timeout=0.05) == ""
    assert await brain_spend.spent(state, "day") == pytest.approx(brain_spend.TURN_USD_FLOOR)
    # On a subscription a timed-out call records nothing, as before.
    sub = Brain(state, BrainConfig())
    assert await sub.think("hi", max_retries=0, timeout=0.05) == ""
    async with state._connect() as db:
        async with db.execute("SELECT source FROM usage_events") as c:
            assert await c.fetchall() == [("api_key_estimate",)]


@pytest.mark.asyncio
async def test_a_result_with_tokens_and_no_cost_is_counted_at_a_turns_bound(state, monkeypatch):
    _calls(monkeypatch, cost=None)
    brain = Brain(state, BrainConfig(auth="api_key"))
    assert await brain.think("hi", max_retries=0) == "ok"
    assert await brain_spend.spent(state, "day") == pytest.approx(brain_spend.TURN_USD_FLOOR)
    # (Not the turn costs a wake's worst case is learnt from.)
    assert await brain_spend.per_turn_usd(state, brain.config) == brain_spend.TURN_USD_FLOOR


def test_a_runs_worst_case_is_its_turns_at_the_most_a_turn_cost(tmp_path):
    state = StateManager(str(tmp_path / "harvey.db"))
    run(state.init_db())
    claude = BrainConfig(auth="api_key")
    assert run(brain_spend.run_worst_usd(state, claude, 12)) == pytest.approx(
        12 * brain_spend.TURN_USD_FLOOR)
    # A dearer turn seen in the last days raises it.
    run(state.record_usage_event(agent="agent", task="wake", cost_usd=0.9, num_turns=3,
                                 source="api_key"))
    assert run(brain_spend.run_worst_usd(state, claude, 12)) == pytest.approx(12 * 0.3)
    codex = BrainConfig(provider="codex", model="m", auth="api_key")
    assert run(brain_spend.run_worst_usd(state, codex, 8)) is None
    codex = codex.model_copy(update={"codex_usd_per_mtok_in": 2.0, "codex_usd_per_mtok_out": 8.0})
    per_turn = (brain_spend.TURN_INPUT_TOKENS * 2.0 + brain_spend.TURN_OUTPUT_TOKENS * 8.0) / 1e6
    assert run(brain_spend.run_worst_usd(state, codex, 8)) == pytest.approx(8 * per_turn)


def test_a_plans_hold_counts_against_its_actions_and_is_refused_past_its_budget(tmp_path):
    from harvey.agent import plans
    from harvey.modules import jobs

    state = StateManager(str(tmp_path / "harvey.db"))
    run(state.init_db())

    async def go():
        async with state._connect() as db:
            await db.execute("INSERT INTO agent_chat (id, title, created_at, last_at) "
                             "VALUES ('c1', 'Test', 1, 1)")
            await db.commit()
        p = await plans.propose(state, "c1", "t0", "A fictional goal", ["One step"], 2.0, 72)
        await plans.decide(state, p["id"], True, plans.BY_DASHBOARD, budget_usd=2.0)
        job = await jobs.enqueue(state, "agent_wake", "agent", {"plan": p["id"]},
                                 dedupe_key=f"agent_wake:{p['id']}")
        async with state._connect() as db:
            await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job,))
            await db.commit()
        await plans.hold_run(state, p["id"], job, 1.5)
        assert (await plans.money(state, p["id"]))["committed_usd"] == pytest.approx(1.5)
        # An action can take only what the hold leaves.
        with pytest.raises(plans.Refused, match=r"\$0.50 of \$2.00 remains"):
            await plans.reserve(state, p["id"], "c1", "t1", "find_contacts", {},
                                cap=0.6, classify_cap=0)
        # A second run's hold too.
        other = await jobs.enqueue(state, "agent_task", "agent", {"task": 1})
        with pytest.raises(plans.Refused, match="budget can't cover it"):
            await plans.hold_run(state, p["id"], other, 0.6)
        # Done at $0.20: the hold gives way to what it cost.
        async with state._connect() as db:
            await db.execute("UPDATE jobs SET status = 'done', actual_cost = 0.2 WHERE id = ?",
                             (job,))
            await db.commit()
        assert (await plans.money(state, p["id"]))["committed_usd"] == pytest.approx(0.2)
    run(go())


# ── The Usage page and `harvey usage` ──

def test_the_usage_page_shows_money_in_api_key_mode(client, monkeypatch):
    monkeypatch.setattr(dash, "_active_brain",
                        lambda: BrainConfig(auth="api_key", usd_per_day=3.0))
    run(StateManager(str(client.dir / "harvey.db")).record_usage_event(
        agent="writer", task="t", cost_usd=1.2, source="api_key"))
    data = client.get("/api/usage").json()
    assert data["billing"] == "api_key" and data["quota"] is None and data["quota_status"] == ""
    assert data["money"]["today_usd"] == pytest.approx(1.2) and data["money"]["usd_per_day"] == 3.0
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig())
    data = client.get("/api/usage").json()
    assert data["billing"] == "subscription" and data["money"] is None


def test_harvey_usage_prints_the_money_beside_the_caps(monkeypatch, tmp_path, capsys):
    from harvey import cli
    import harvey.config as config_mod
    import harvey.state as state_mod

    db = str(tmp_path / "harvey.db")
    run(StateManager(db).init_db())
    run(StateManager(db).record_usage_event(agent="writer", task="t", cost_usd=0.75,
                                            source="api_key"))
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(db))
    monkeypatch.setattr(config_mod, "load_brain_config",
                        lambda *a: BrainConfig(auth="api_key", usd_per_day=2.0))
    cli.cmd_usage(type("A", (), {"days": 30})())
    out = capsys.readouterr().out
    assert "API key, billed per token" in out and "$0.75 of $2.00 (brain.usd_per_day)" in out
    assert "(no cap)" in out and "Subscription plans aren't billed" not in out


def test_the_agents_codex_runner_takes_the_key_codex_saved(monkeypatch):
    from harvey import codex
    from harvey.agent import driver

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    monkeypatch.setattr(codex, "find_codex", lambda: "/bin/codex")
    monkeypatch.setattr(codex, "check_cli_version", lambda *a, **k: "0.159.3")
    home = brain_auth.ensure_account_dir("codex")
    config = type("C", (), {"brain": BrainConfig(provider="codex", model="m", auth="api_key")})()
    with pytest.raises(driver.Unusable, match="No Codex API key"):
        run(driver.check(config))
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey",
                                                "OPENAI_API_KEY": "sk-fake-openai"}))
    (home / "auth.json").chmod(0o600)
    runner = run(driver.check(config))
    assert runner.api_key is True and runner.native_home == home
    # The terminal login is never the key's.
    terminal = type("C", (), {"brain": BrainConfig(provider="codex", model="m")})()
    monkeypatch.setattr(codex, "check_native_auth", lambda home, api=False: home)
    assert run(driver.check(terminal)).api_key is False
