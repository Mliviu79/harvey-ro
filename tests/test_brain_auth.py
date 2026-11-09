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
    written = set_brain_settings({"model": "gpt-6-luna", "reasoning_effort": "high",
                                  "auth": "terminal"}, template, provider="codex")
    set_brain_settings({"provider": "codex"}, template)
    assert written == tmp_path / "harvey.local.yaml"
    assert template.read_text(encoding="utf-8") == TEMPLATE        # the template untouched
    text = written.read_text(encoding="utf-8")
    assert "# Which subscription Harvey thinks with" in text and "# Harvey's settings" in text
    assert "auth" not in text                    # a default never chosen stays absent
    assert yaml.safe_load(text)["brain"] == {"provider": "codex", "codex": {
        "model": "gpt-6-luna", "reasoning_effort": "high"}}
    loaded = load_brain_config(str(written))
    assert (loaded.provider, loaded.model, loaded.reasoning_effort) == ("codex", "gpt-6-luna", "high")
    assert yaml.safe_load(text)["usage"] == {"max_daily_claude_percent": 80}

    # One provider's login never touches the other's.
    set_brain_settings({"auth": "account"}, template, provider="claude")
    loaded = load_brain_config(str(written))
    assert loaded.claude.auth == "account" and loaded.codex.auth == "terminal"
    assert loaded.auth == "terminal"                       # (the default's, Codex's)
    before = written.read_bytes()
    for bad, which in (({"model": "--dangerous"}, "claude"), ({"auth": "password"}, "codex"),
                       ({"colour": "red"}, "claude"), ({"usd_per_day": "-1"}, "codex"),
                       ({"usd_per_month": "lots"}, "claude"),
                       ({"reasoning_effort": "low"}, "claude"),     # Codex's only
                       ({"model": "opus"}, None), ({"provider": "gemini"}, None)):
        with pytest.raises(ConfigError):
            set_brain_settings(bad, template, provider=which)
    assert written.read_bytes() == before


FLAT = """# Harvey's settings (fictional)
persona:
  name: H  # the sender
brain:
  provider: codex
  model: gpt-6-luna
  auth: account
  reasoning_effort: low
  usd_per_day: 3
# Usage comes after.
usage:
  max_daily_claude_percent: 80  # leave the rest for you
"""


def test_an_older_flat_brain_loads_and_the_first_save_writes_the_new_shape(tmp_path):
    local = tmp_path / "harvey.local.yaml"
    local.write_text(FLAT, encoding="utf-8")
    # The flat keys are the default provider's (Codex's effort is Codex's);
    # the other provider is the terminal login with no model.
    loaded = load_brain_config(str(local))
    assert (loaded.provider, loaded.model, loaded.auth, loaded.usd_per_day) == (
        "codex", "gpt-6-luna", "account", 3.0)
    assert loaded.reasoning_effort == "low"
    assert (loaded.claude.auth, loaded.claude.model) == ("terminal", "")
    claude = BrainConfig(provider="claude", reasoning_effort="high")
    assert claude.codex.reasoning_effort == "high" and claude.claude.reasoning_effort == ""

    set_brain_settings({"model": "sonnet"}, local, provider="claude")
    text = local.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["brain"] == {
        "provider": "codex", "claude": {"model": "sonnet"},
        "codex": {"model": "gpt-6-luna", "reasoning_effort": "low", "auth": "account",
                  "usd_per_day": 3.0}}
    # Only brain: changed: every comment outside it stays.
    for kept in ("# Harvey's settings", "name: H  # the sender", "# Usage comes after.",
                 "max_daily_claude_percent: 80  # leave the rest for you"):
        assert kept in text, kept
    again = load_brain_config(str(local))
    assert again.codex.model_dump() == loaded.codex.model_dump()
    assert (again.provider, again.claude.model) == ("codex", "sonnet")
    # A save with nothing to change leaves the file as it is.
    before = local.read_bytes()
    set_brain_settings({"model": "sonnet"}, local, provider="claude")
    assert local.read_bytes() == before


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
    assert one["offered"] == ["terminal", "account", "api_key"]
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
    assert got["default"] == "claude" and set(got["providers"]) == {"claude", "codex"}
    # (The default's fields stay at the top, as the page and setup read them.)
    assert (got["provider"], got["model"], got["auth"]) == ("claude", "", "terminal")
    mine = got["providers"]["claude"]
    assert mine["default"] is True and got["providers"]["codex"]["default"] is False
    assert mine["offered"] == ["terminal", "account", "api_key"] and "account" in got["auth_ready"]
    assert got["codex_refused"] == "" and mine["key_set"] == {"claude": False, "codex": False}

    r = client.post("/api/brain", json={"provider": "codex", "model": ""})
    assert r.status_code == 400 and "Pick a Codex model" in r.json()["message"]
    r = client.post("/api/brain", json={"provider": "claude", "auth": "account"})
    assert r.status_code == 409                  # no account linked yet
    r = client.post("/api/brain", json={"provider": "claude", "auth": "api_key"})
    assert r.status_code == 409 and "Save the Claude API key first" in r.json()["message"]
    r = client.post("/api/brain", json={"default": "codex"})
    assert r.status_code == 400 and "Codex model" in r.json()["message"]
    r = client.post("/api/brain", json={"model": "opus"})
    assert r.status_code == 400 and "Say which provider" in r.json()["message"]
    assert not (client.dir / "harvey.local.yaml").exists()

    r = client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna",
                                        "reasoning_effort": "low", "auth": "terminal"})
    assert r.status_code == 200
    saved = r.json()["brain"]
    assert saved["default"] == "claude"          # saving Codex never made it the default
    assert saved["codex"] == {"model": "gpt-6-luna", "reasoning_effort": "low",
                              "auth": "terminal", **NO_MONEY}
    text = (client.dir / "harvey.local.yaml").read_text(encoding="utf-8")
    assert "# Which subscription Harvey thinks with" in text
    both = client.get("/api/brain").json()["providers"]
    assert both["codex"]["model"] == "gpt-6-luna" and both["claude"]["model"] == ""

    # The default: Codex now (it has a model), then back.
    r = client.post("/api/brain", json={"default": "codex"})
    assert r.status_code == 200 and r.json()["brain"]["default"] == "codex"
    assert client.get("/api/brain").json()["model"] == "gpt-6-luna"
    r = client.post("/api/brain", json={"provider": "claude", "model": "sonnet"})
    assert r.json()["brain"]["claude"]["model"] == "sonnet"
    assert r.json()["brain"]["default"] == "codex"
    assert "reasoning_effort" not in r.json()["brain"]["claude"]
    r = client.post("/api/brain", json={"default": "claude"})
    loaded = load_brain_config(str(client.dir / "harvey.local.yaml"))
    assert (loaded.provider, loaded.model, loaded.codex.model) == ("claude", "sonnet", "gpt-6-luna")


def test_each_provider_keeps_its_own_model(client, monkeypatch):
    from harvey import codex

    monkeypatch.setattr(codex, "_PLATFORM", "linux")
    assert client.post("/api/brain", json={"provider": "codex", "model": "gpt-6-luna",
                                           "reasoning_effort": "low"}).status_code == 200
    assert client.post("/api/brain", json={"default": "codex"}).status_code == 200
    # Claude's settings are Claude's: Codex's model and effort aren't carried.
    r = client.post("/api/brain", json={"provider": "claude", "model": ""})
    assert r.status_code == 200 and r.json()["brain"]["claude"] == {
        "model": "", "auth": "terminal", "usd_per_day": None, "usd_per_month": None}
    r = client.post("/api/brain", json={"provider": "claude", "reasoning_effort": "low"})
    assert r.status_code == 400 and "Not a claude setting" in r.json()["message"]
    # To Claude as the default: its own model, none of Codex's.
    assert client.post("/api/brain", json={"default": "claude"}).status_code == 200
    loaded = load_brain_config(str(client.dir / "harvey.local.yaml"))
    assert (loaded.model, loaded.reasoning_effort) == ("", "")
    assert (loaded.codex.model, loaded.codex.reasoning_effort) == ("gpt-6-luna", "low")


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
    r = client.post("/api/brain", json={"default": "codex"})
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
const other = process.argv[5] ? JSON.parse(process.argv[5]) : null;
const providers = {};
providers[s.provider] = s;
const second = s.provider === 'codex' ? 'claude' : 'codex';
providers[second] = other || {provider: second, model: '', auth: 'terminal', offered: ['terminal'],
                              cli: {ok: true, version: '', problem: ''}, linked: null};
adoptModel({default: s.default_provider || s.provider, providers: providers,
            codex_refused: s.codex_refused || '', efforts: s.efforts || []});
// (Codex's own read, with its models.)
_modelFull.codex = providers.codex;
renderModelCard();
console.log(JSON.stringify({html: els['model-card'].innerHTML}));
'''


def _panel(lang: str, status: dict, picked: dict | None = None) -> str:
    """The card with ``status`` the default provider's (``default_provider``
    in it: another default); ``picked``: the other provider's own status
    (else the terminal login, nothing known)."""
    res = subprocess.run([_node(), "-e", PANEL, str(WEB / "app.js"), str(WEB / "i18n.js"), lang,
                          json.dumps(status), json.dumps(picked) if picked else ""],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-3000:]
    return json.loads(res.stdout)["html"]


def _section(html: str, provider: str) -> str:
    """One provider's card of the panel."""
    claude, codex = html.index('id="model-claude"'), html.index('id="model-codex"')
    return html[claude:codex] if provider == "claude" else html[codex:]


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
    html = _section(_panel(lang, CLAUDE), "claude")
    for w in words:
        assert w in html, w
    # (Only the ways the status offers: this one lists no account.)
    assert 'value="account"' not in html and 'value="api_key"' in html
    assert 'id="model-key-claude"' not in html           # the key box: API key only
    assert '<option value="opus">' in html and 'id="model-effort"' not in html


def test_the_panel_shows_codex_refused_and_its_live_models():
    html = _panel("en", {**CLAUDE, "codex_refused": "Harvey's Codex provider supports macOS "
                                                    "and Linux only."})
    assert 'name="default-provider" value="codex" disabled' in html
    assert "macOS and Linux only" in html and 'id="model-name-codex"' not in html
    codex = {**CLAUDE, "provider": "codex", "model": "gpt-6-luna", "auth": "account",
             "folder": "data/auth/codex", "linked": False, "linked_as": {},
             "problem": "Not linked yet: no login in data/auth/codex.",
             "models": [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": ["low", "high"]}]}
    html = _section(_panel("en", codex), "codex")
    assert '<option value="gpt-6-luna">GPT-6 Luna</option>' in html
    assert '<option value="low">' in html and '<option value="xhigh">' not in html
    assert 'value="account" checked' in html and "data/auth/codex" in html
    assert "not linked yet" in html and "gpt-5.5" not in html


def test_both_providers_have_their_own_card_and_the_default_is_marked():
    codex = {**CLAUDE, "provider": "codex", "model": "gpt-6-luna", "linked": True,
             "linked_as": {"method": "chatgpt", "plan": "plus"},
             "cli": {"ok": True, "version": "0.159.3", "problem": ""},
             "models": [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": ["low"]}]}
    html = _panel("en", codex, picked=CLAUDE)
    # Claude's aliases and Claude's own status line, none of Codex's.
    mine = _section(html, "claude")
    assert '<option value="opus">' in mine and "gpt-6-luna" not in mine
    assert "CLI 2.1.318" in mine and "chatgpt" not in mine and "0.159.3" not in mine
    assert 'id="model-effort"' not in mine
    theirs = _section(html, "codex")
    assert "gpt-6-luna" in theirs and "0.159.3" in theirs and 'id="model-effort"' in theirs
    # Codex is the default: its radio checked, its card marked.
    assert 'name="default-provider" value="codex" checked' in html
    assert ">default</span>" in theirs and ">default</span>" not in mine
    # Codex with no model can't be made the default.
    blank = _panel("en", CLAUDE, picked={**codex, "model": ""})
    assert 'name="default-provider" value="codex" disabled' in blank
    assert "save a Codex model first" in blank


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
    html = _section(_panel(lang, status), "claude")
    for w in words:
        assert w in html, w
    # The key box is a password field, empty: what was saved never comes back.
    assert 'id="model-key-claude" type="password"' in html and 'value=""' in html
    assert 'id="model-unlink-claude"' in html and 'id="model-price-in-claude"' not in html
    # Codex: its token prices too, and no Unlink with no key saved.
    codex = {**status, "provider": "codex", "model": "gpt-6-luna", "spend": None,
             "key_set": {"claude": True, "codex": False}, "linked": False, "linked_as": {}}
    html = _section(_panel(lang, codex), "codex")
    assert 'id="model-price-in-codex"' in html and 'id="model-price-out-codex"' in html
    assert 'id="model-unlink-codex"' not in html


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
