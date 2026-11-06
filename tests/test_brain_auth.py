"""Settings → Model and brain.auth: the login a Claude or Codex child runs
under (the terminal's own, or Harvey's account folder), credentials in the
environment dropped from every child, the panel's status and its save, and
the agent's language. Every CLI is a stand-in: no claude or codex runs, no
model is called."""

import asyncio
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import brain_auth, claude_runtime
from harvey.brain import BRAIN_STATUS_KEY, Brain
from harvey.config import (BrainConfig, ConfigError, load_brain_config,
                           set_brain_settings)
from harvey.state import StateManager
from harvey.integrations import quota
from tests.conftest import REAL_CLAUDE_STATUS, REAL_CODEX_STATUS

# (The real /usage check, before conftest swaps in one that never runs.)
REAL_RUN_PROBE = quota._run_probe
REAL_PROBE_HELP = claude_runtime._probe_help

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "harvey" / "web"

TEMPLATE = """# Harvey's settings (fictional)
persona:
  name: H
  company: Acme Example
brain:
  # Which subscription Harvey thinks with
  provider: claude   # claude or codex
usage:
  max_daily_claude_percent: 80
"""


def run(coro):
    return asyncio.run(coro)


# The brain's USD caps and Codex's prices, none set.
NO_MONEY = {"usd_per_day": None, "usd_per_month": None, "codex_usd_per_mtok_in": None,
            "codex_usd_per_mtok_out": None}


# ── brain.auth in the config ──

def test_auth_is_terminal_unless_set():
    assert BrainConfig().auth == "terminal"
    assert BrainConfig(auth=" Account ").auth == "account"
    assert BrainConfig(auth="api-key").auth == "api_key"
    with pytest.raises(ValueError, match="not a way to log in"):
        BrainConfig(auth="password")


def test_saving_brain_settings_keeps_the_file_and_its_comments(tmp_path):
    template = tmp_path / "harvey.yaml"
    template.write_text(TEMPLATE, encoding="utf-8")
    written = set_brain_settings({"provider": "codex", "model": "gpt-6-luna",
                                  "reasoning_effort": "high", "auth": "terminal"}, template)
    assert written == tmp_path / "harvey.local.yaml"
    assert template.read_text(encoding="utf-8") == TEMPLATE        # the template untouched
    text = written.read_text(encoding="utf-8")
    assert "# Which subscription Harvey thinks with" in text and "# Harvey's settings" in text
    assert "auth" not in text                    # a default never chosen stays absent
    assert load_brain_config(str(written)) == BrainConfig(
        provider="codex", model="gpt-6-luna", reasoning_effort="high")
    assert yaml.safe_load(text)["usage"] == {"max_daily_claude_percent": 80}

    set_brain_settings({"auth": "account"}, template)
    assert load_brain_config(str(written)).auth == "account"
    before = written.read_bytes()
    for bad in ({"model": "--dangerous"}, {"auth": "password"}, {"colour": "red"},
                {"usd_per_day": "-1"}, {"usd_per_month": "lots"}):
        with pytest.raises(ConfigError):
            set_brain_settings(bad, template)
    assert written.read_bytes() == before


# ── The children's environment ──

def test_credentials_never_reach_a_claude_child_in_any_mode(monkeypatch):
    base = {"HOME": "/home/op", "CLAUDE_CONFIG_DIR": "/home/op/.claude",
            "ANTHROPIC_API_KEY": "fake-key", "ANTHROPIC_AUTH_TOKEN": "fake-token",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}
    base[claude_runtime.CREDENTIAL_ENV[2]] = "fake-oauth"
    for safe in (True, False):
        terminal = claude_runtime.runtime_env("/tmp/x", base, safe=safe)
        assert not set(claude_runtime.CREDENTIAL_ENV) & set(terminal)
        assert terminal["CLAUDE_CONFIG_DIR"] == "/home/op/.claude"   # the CLI's own login
        assert "ANTHROPIC_BASE_URL" not in terminal      # billed outside the login
        account = claude_runtime.runtime_env("/tmp/x", base, safe=safe, auth="account")
        assert not set(claude_runtime.CREDENTIAL_ENV) & set(account)
        folder = Path(account["CLAUDE_CONFIG_DIR"])
        assert folder == brain_auth.AUTH_DIR / "claude"
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE(brain_auth.AUTH_DIR.stat().st_mode) == 0o700
    assert list(folder.iterdir()) == []          # Harvey puts nothing in it


def test_the_codex_child_gets_no_api_key(tmp_path):
    from harvey import codex

    env = codex.build_child_env(str(tmp_path), str(tmp_path / "home"), {
        "PATH": "/bin", "OPENAI_API_KEY": "sk-fake", "CODEX_API_KEY": "fake",
        "OPENAI_BASE_URL": "https://example.invalid", "CODEX_HOME": "/real"})
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"} & set(env)
    assert env["CODEX_HOME"] == str(tmp_path / "home")
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY"} & codex.ENV_ALLOWLIST


def test_every_child_takes_the_mode(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "harvey.db"))
    brain = Brain(state, BrainConfig(auth="account"))
    assert brain.auth == "account" and brain.quota.auth == "account"
    codex_brain = Brain(state, BrainConfig(provider="codex", model="gpt-6-luna", auth="account"))
    assert codex_brain.codex.native_home == brain_auth.AUTH_DIR / "codex"
    assert Brain(state, BrainConfig(provider="codex", model="m")).codex.native_home is None

    # The agent's Claude process (a chat, a wake, a task).
    from harvey.agent import chat, driver

    seen = {}

    async def start(cmd, cwd, env, folders):
        seen["env"] = env
        for f in folders:
            shutil.rmtree(f, ignore_errors=True)
        return object()
    monkeypatch.setattr(chat.Process, "start", start)
    config = type("C", (), {"brain": BrainConfig(auth="account")})()
    assert driver.auth(config) == "account"
    run(driver.start("/bin/claude", str(tmp_path / "harvey.db"), "c1", auth=driver.auth(config)))
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(brain_auth.AUTH_DIR / "claude")

    # The /usage check.
    from harvey.integrations import quota

    asked = []

    async def probe(state, *, force=False, auth="terminal"):
        asked.append(auth)
        return False
    monkeypatch.setattr(quota, "probe", probe)
    run(state.init_db())
    run(quota.QuotaClient(state, auth="account").get_utilization())
    assert asked == ["account"]


def test_the_cli_checks_get_no_credentials_either(monkeypatch):
    # The /usage check, 'claude --help', '--version' and 'auth status' are
    # Claude children too.
    for key in claude_runtime.CREDENTIAL_ENV:
        monkeypatch.setenv(key, "fake-credential-for-test")
    envs = []

    def fake_run(cmd, **kw):
        envs.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    REAL_RUN_PROBE("/bin/claude", "account")
    REAL_PROBE_HELP("/bin/claude")
    brain_auth._run_claude("/bin/claude", ["auth", "status"], "terminal")
    assert len(envs) == 3
    for env in envs:
        assert not set(claude_runtime.CREDENTIAL_ENV) & set(env)
    assert envs[0]["CLAUDE_CONFIG_DIR"] == str(brain_auth.AUTH_DIR / "claude")


# ── The status ──

def test_claude_auth_status_is_read_without_the_email():
    text = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                       "email": "someone@example.com", "orgId": "org-1",
                       "orgName": "Acme Example", "subscriptionType": "max"})
    assert brain_auth.parse_claude_auth(text) == {
        "logged_in": True, "method": "claude.ai", "plan": "max", "api": "firstParty"}
    assert brain_auth.parse_claude_auth("not json")["logged_in"] is False
    assert brain_auth.parse_version("2.1.318 (Claude Code)") == "2.1.318"


@pytest.mark.parametrize("auth, answer, linked, problem", [
    ("terminal", {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro",
                  "email": "someone@example.com"}, True, ""),
    ("terminal", {"loggedIn": False, "authMethod": "none"}, False, "claude login"),
    ("account", {"loggedIn": False, "authMethod": "none"}, False, "Not linked yet"),
])
def test_claude_status_asks_the_cli_itself(monkeypatch, auth, answer, linked, problem):
    calls = []

    def fake(exe, args, auth_):
        calls.append((args, auth_))
        out = "2.1.318 (Claude Code)\n" if args == ["--version"] else json.dumps(answer)
        return subprocess.CompletedProcess(args, 0 if answer["loggedIn"] else 1, out, "")
    monkeypatch.setattr(brain_auth, "_run_claude", fake)
    found = REAL_CLAUDE_STATUS(auth)
    assert calls == [(["--version"], auth), (["auth", "status"], auth)]
    assert found["cli"] == {"ok": True, "version": "2.1.318", "problem": ""}
    assert found["linked"] is linked and problem in found["problem"]
    assert found["linked_as"] == ({"method": "claude.ai", "plan": "pro"} if linked else {})
    assert "example.com" not in json.dumps(found)


def test_claude_status_says_when_the_cli_cant_isolate(monkeypatch):
    def refuse(env=None, flags=()):
        raise claude_runtime.ClaudeRuntimeError("This Claude CLI lacks --safe-mode")
    monkeypatch.setattr(claude_runtime, "check_cli", refuse)
    monkeypatch.setattr(brain_auth, "_run_claude", lambda *a: pytest.fail("no CLI call"))
    found = REAL_CLAUDE_STATUS("terminal")
    assert found["cli"]["ok"] is False and "--safe-mode" in found["problem"]


def test_an_account_folder_with_no_login_isnt_linked_and_nothing_runs(monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    monkeypatch.setattr(codex, "check_cli_version", lambda *a, **k: "0.159.3")

    async def no_describe(self, timeout=0):
        raise AssertionError("no runtime starts for a folder with no login")
    monkeypatch.setattr(codex.CodexClient, "describe", no_describe)
    found = REAL_CODEX_STATUS(BrainConfig(provider="codex", model="m", auth="account"), True)
    assert found["linked"] is False and "Not linked yet" in found["problem"]
    assert found["cli"]["version"] == "0.159.3"


def test_codex_status_reports_the_account_type_and_plan(monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    monkeypatch.setattr(codex, "check_cli_version", lambda *a, **k: "0.159.3")

    async def describe(self, timeout=0):
        return {"version": "0.159.3", "account": {"type": "chatgpt", "plan": "plus"},
                "models": [{"id": "gpt-6-luna", "name": "gpt-6-luna", "efforts": []}]}
    monkeypatch.setattr(codex.CodexClient, "describe", describe)
    found = REAL_CODEX_STATUS(BrainConfig(provider="codex", model="gpt-6-luna"), True)
    assert found["linked"] is True and found["linked_as"] == {"method": "chatgpt", "plan": "plus"}
    assert [m["id"] for m in found["models"]] == ["gpt-6-luna"]


def test_status_is_kept_briefly_and_names_the_last_error(tmp_path, monkeypatch):
    calls = []

    def fake(auth):
        calls.append(auth)
        return {"cli": {"ok": True, "version": "2.1.318", "problem": ""}, "linked": True,
                "linked_as": {"method": "claude.ai"}, "problem": ""}
    monkeypatch.setattr(brain_auth, "claude_status", fake)
    state = StateManager(str(tmp_path / "harvey.db"))
    run(state.init_db())
    run(state.set_setting(BRAIN_STATUS_KEY, json.dumps(
        {"provider": "claude", "status": "paused", "kind": "setup", "reason": "x"})))
    one = run(brain_auth.status(BrainConfig(), state=state))
    two = run(brain_auth.status(BrainConfig(), state=state))
    assert calls == ["terminal"] and one == two
    assert one["last_error"] == "setup" and one["auth"] == "terminal"
    assert one["offered"] == ["terminal", "api_key"]
    assert [m["id"] for m in one["models"]] == ["opus", "sonnet", "haiku"]
    run(brain_auth.status(BrainConfig(), state=state, force=True))
    assert calls == ["terminal", "terminal"]


# ── The dashboard ──

@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    template = tmp_path / "harvey.yaml"
    template.write_text(TEMPLATE, encoding="utf-8")
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


def test_the_panel_reads_and_saves_the_brain(client, monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    got = client.get("/api/brain").json()
    assert (got["provider"], got["model"], got["auth"]) == ("claude", "", "terminal")
    assert got["offered"] == ["terminal", "api_key"] and "account" in got["auth_ready"]
    assert got["codex_refused"] == "" and got["key_set"] == {"claude": False, "codex": False}

    r = client.post("/api/brain", json={"provider": "codex", "model": ""})
    assert r.status_code == 400 and "Pick a Codex model" in r.json()["message"]
    r = client.post("/api/brain", json={"auth": "account"})
    assert r.status_code == 409                  # no link flow yet: not offered
    r = client.post("/api/brain", json={"auth": "api_key"})
    assert r.status_code == 409 and "Save the Claude API key first" in r.json()["message"]
    assert not (client.dir / "harvey.local.yaml").exists()

    r = client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna",
                                        "reasoning_effort": "low", "auth": "terminal"})
    assert r.status_code == 200 and r.json()["brain"] == {
        "provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "low", "auth": "terminal",
        **NO_MONEY}
    text = (client.dir / "harvey.local.yaml").read_text(encoding="utf-8")
    assert "# Which subscription Harvey thinks with" in text
    assert client.get("/api/brain").json()["model"] == "gpt-6-luna"
    # Back to Claude: Codex's effort goes with it.
    r = client.post("/api/brain", json={"provider": "claude", "model": "sonnet"})
    assert r.json()["brain"]["reasoning_effort"] == ""


def test_a_provider_switch_never_carries_the_other_ones_model(client, monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    assert client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna",
                                           "reasoning_effort": "low"}).status_code == 200
    # Back to Claude naming no model: Codex's model isn't Claude's.
    r = client.post("/api/brain", json={"provider": "claude"})
    assert r.status_code == 200 and r.json()["brain"] == {
        "provider": "claude", "model": "", "reasoning_effort": "", "auth": "terminal", **NO_MONEY}
    # A blank provider is no switch (and no write of the default).
    r = client.post("/api/brain", json={"provider": "", "model": "sonnet"})
    assert r.status_code == 200 and r.json()["brain"]["provider"] == "claude"
    assert r.json()["brain"]["model"] == "sonnet"
    # To Codex naming no model: refused, not Claude's model sent to Codex.
    r = client.post("/api/brain", json={"provider": "codex"})
    assert r.status_code == 400 and "Pick a Codex model" in r.json()["message"]
    assert load_brain_config(str(client.dir / "harvey.local.yaml")).model == "sonnet"


def test_the_brain_endpoints_need_the_token(client):
    from fastapi.testclient import TestClient as TC

    with TC(dash.app, base_url="http://127.0.0.1:5555") as bare:
        assert bare.get("/api/brain").status_code in (401, 403)
        assert bare.post("/api/brain", json={"model": "opus"}).status_code in (401, 403)
    assert not (client.dir / "harvey.local.yaml").exists()


def test_codex_is_refused_where_it_cant_run(client, monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "win32")
    assert "Windows" in client.get("/api/brain").json()["codex_refused"]
    r = client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna"})
    assert r.status_code == 409 and "Windows" in r.json()["message"]


def test_setup_status_has_a_brain_entry(client, monkeypatch):
    def fake(auth):
        return {"cli": {"ok": True, "version": "2.1.318", "problem": ""}, "linked": False,
                "linked_as": {}, "problem": "Claude isn't logged in: run 'claude login'."}
    monkeypatch.setattr(brain_auth, "claude_status", fake)
    checks = {c["id"]: c for c in client.get("/api/setup-status").json()["checks"]}
    brain = checks["brain"]
    assert brain["done"] is False and brain["required"] is True
    assert brain["provider"] == "claude" and brain["auth"] == "terminal"
    assert "claude login" in brain["help"]


# ── The panel under node ──

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


PANEL = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {'model-card': {innerHTML: ''}};
global.document = {getElementById: id => els[id] || null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => escHtml(JSON.stringify(String(s == null ? '' : s)));
global.safeHref = u => /^https?:\/\//i.test(String(u || '').trim()) ? escHtml(String(u).trim()) : '';
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('let _model = null', '// Settings: the background'));
const s = JSON.parse(process.argv[4]);
_model = s;
_modelPick = {provider: s.provider, model: s.model, reasoning_effort: s.reasoning_effort, auth: s.auth};
if (s.provider === 'codex') _modelFull.codex = s;
const other = process.argv[5] ? JSON.parse(process.argv[5]) : null;
if (other) { _modelFull[other.provider] = other; _modelPick.provider = other.provider;
             _modelPick.model = ''; _modelPick.reasoning_effort = ''; }
renderModelCard();
console.log(JSON.stringify({html: els['model-card'].innerHTML}));
'''


def _panel(lang: str, status: dict, picked: dict | None = None) -> str:
    """The card for the saved ``status``; ``picked``: the other provider
    chosen (not saved yet) with its own status."""
    res = subprocess.run([_node(), "-e", PANEL, str(WEB / "app.js"), str(WEB / "i18n.js"), lang,
                          json.dumps(status), json.dumps(picked) if picked else ""],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-3000:]
    return json.loads(res.stdout)["html"]


CLAUDE = {"provider": "claude", "model": "", "reasoning_effort": "", "auth": "terminal",
          "offered": ["terminal", "api_key"], "folder": "", "codex_refused": "", "last_error": "",
          "cli": {"ok": True, "version": "2.1.318", "problem": ""}, "linked": True,
          "linked_as": {"method": "claude.ai", "plan": "max"}, "problem": "",
          "models": [{"id": m, "name": m, "efforts": []} for m in ("opus", "sonnet", "haiku")],
          "efforts": ["minimal", "low", "medium", "high", "xhigh"]}


@pytest.mark.parametrize("lang, words", [
    ("en", ["Terminal login", "linked: claude.ai · max", "CLI 2.1.318", "Ready"]),
    ("ro", ["Autentificarea din terminal", "conectat: claude.ai · max", "Gata"]),
])
def test_the_panel_offers_only_the_ways_that_work(lang, words):
    html = _panel(lang, CLAUDE)
    for w in words:
        assert w in html, w
    # The account link's flow isn't here yet: no button for it.
    assert 'value="account"' not in html and 'value="api_key"' in html
    assert 'id="model-key"' not in html                  # the key box: API key only
    assert '<option value="opus">' in html and 'id="model-effort"' not in html


def test_the_panel_shows_codex_refused_and_its_live_models():
    html = _panel("en", {**CLAUDE, "codex_refused": "Harvey's Codex provider supports macOS "
                                                    "and Linux only."})
    assert 'value="codex" disabled' in html and "macOS and Linux only" in html
    codex = {**CLAUDE, "provider": "codex", "model": "gpt-6-luna", "auth": "account",
             "folder": "data/auth/codex", "linked": False, "linked_as": {},
             "problem": "Not linked yet: no login in data/auth/codex.",
             "models": [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": ["low", "high"]}]}
    html = _panel("en", codex)
    assert '<option value="gpt-6-luna">GPT-6 Luna</option>' in html
    assert '<option value="low">' in html and '<option value="xhigh">' not in html
    assert 'value="account" checked' in html and "data/auth/codex" in html
    assert "not linked yet" in html and "gpt-5.5" not in html


def test_switching_the_panel_to_claude_shows_claudes_models_and_status():
    codex = {**CLAUDE, "provider": "codex", "model": "gpt-6-luna", "linked": True,
             "linked_as": {"method": "chatgpt", "plan": "plus"},
             "cli": {"ok": True, "version": "0.159.3", "problem": ""},
             "models": [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": ["low"]}]}
    html = _panel("en", codex, picked=CLAUDE)
    # Claude's aliases and Claude's own status line, none of Codex's.
    assert '<option value="opus">' in html and "gpt-6-luna" not in html
    assert "CLI 2.1.318" in html and "chatgpt" not in html and "0.159.3" not in html
    assert 'id="model-effort"' not in html


@pytest.mark.parametrize("lang, words", [
    ("en", ["API key", "API key set", "today $0.40 of $2.00", "this month $1.10 of $20.00",
            "Unlink", "Per day (USD)"]),
    ("ro", ["Cheie API", "cheie API setată", "azi 0,40", "Deconectează",
            "Pe zi (USD)"]),
])
def test_the_panel_in_api_key_mode_has_the_key_box_caps_and_unlink(lang, words):
    spend = {"today_usd": 0.4, "month_usd": 1.1, "usd_per_day": 2.0, "usd_per_month": 20.0,
             "priced": True}
    status = {**CLAUDE, "auth": "api_key", "linked": True, "linked_as": {"method": "api_key"},
              "key_set": {"claude": True, "codex": False}, "spend": spend,
              "usd_per_day": 2.0, "usd_per_month": 20.0}
    html = _panel(lang, status)
    for w in words:
        assert w in html, w
    # The key box is a password field, empty: what was saved never comes back.
    assert 'id="model-key" type="password"' in html and 'value=""' in html
    assert 'id="model-unlink"' in html and 'id="model-price-in"' not in html
    # Codex: its token prices too, and no Unlink with no key saved.
    codex = {**status, "provider": "codex", "model": "gpt-6-luna", "spend": None,
             "key_set": {"claude": True, "codex": False}, "linked": False, "linked_as": {}}
    html = _panel(lang, codex)
    assert 'id="model-price-in"' in html and 'id="model-price-out"' in html
    assert 'id="model-unlink"' not in html


# ── The agent's language ──

def test_the_agent_answers_in_the_language_of_the_latest_message():
    prompt = (ROOT / "prompts" / "agent.md").read_text(encoding="utf-8")
    assert "Answer in the language of the user's latest message" in prompt
    assert "an English question gets an English answer" in prompt
    assert "answer in Romanian" in prompt                 # only when it can't be told
    assert "On a wake, with no new message, use the language of the user's latest" in prompt
    assert "the emails', not the chat's" in prompt
    task = (ROOT / "prompts" / "agent_task.md").read_text(encoding="utf-8")
    assert "in the language the agent's request is written in" in task


def test_a_wake_keeps_the_chats_language(tmp_path):
    from harvey.agent import wakes

    state = StateManager(str(tmp_path / "harvey.db"))
    run(state.init_db())

    async def money(state, plan_id):
        return {"committed_usd": 0.0}

    async def opening(state, chat_id, turn):
        return ["(prompt)"]
    import harvey.agent.chat as chat
    import harvey.agent.plans as plans
    mp = pytest.MonkeyPatch()
    mp.setattr(plans, "money", money)
    mp.setattr(chat, "opening", opening)
    try:
        text = run(wakes.prime(state, {"id": 1, "chat": "c1", "expires_at": 1_800_000_000,
                                       "budget_usd": 0.0, "goal": "g"}, [], "t1"))
    finally:
        mp.undo()
    assert "in the language of the user's latest message in this chat" in text


USAGE_TAB = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: '', textContent: '', style: {},
                                        setAttribute() {}});
global.document = {getElementById: el};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
global.offlineState = () => 'offline';
global.quotaCardHtml = () => 'QUOTA';
global.api = async () => JSON.parse(process.argv[4]);
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
vm.runInThisContext(pick('// API-key mode: today', 'async function loadOutbox'));
loadUsage().catch(e => { if (e instanceof ReferenceError && /'api'/.test(e.message)) throw e; })
  .then(() => console.log(JSON.stringify({quota: els['usage-quota'].innerHTML,
                                          billing: els['usage-billing'].textContent})));
'''


@pytest.mark.parametrize("lang, words", [
    ("en", ["Today (UTC)", "of $2.50"]), ("ro", ["2,50"])])
def test_the_usage_tab_shows_the_money_in_api_key_mode(lang, words):
    # (loadUsage once named a local "api", which shadowed api() and broke
    # the Usage tab in every mode.)
    data = {"billing": "api_key", "totals": {}, "by_day": [], "by_provider": {},
            "money": {"today_usd": 0.83, "month_usd": 0.83, "usd_per_day": 2.5,
                      "usd_per_month": 40.0, "priced": True}}
    res = subprocess.run([_node(), "-e", USAGE_TAB, str(WEB / "app.js"), str(WEB / "i18n.js"),
                          lang, json.dumps(data)],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-3000:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert "QUOTA" not in out["quota"]
    for w in words:
        assert w in out["quota"], w
    assert out["billing"] and "token" in out["billing"].lower()
