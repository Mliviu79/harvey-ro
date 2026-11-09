"""The Codex account linked in Harvey (brain.auth: account): the
device-code link from Settings → Model, Harvey renewing that login in its
own folder, the logout, and what the panel, Today and the Docker image say.

Codex is tests/fake_codex.py throughout (no codex runs, no network, no
login): it answers the device-code login with a fictional code, writes a
fictional ChatGPT login when it "completes", and rewrites it on a renewal.
"""

import asyncio
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.codex as hc
import harvey.dashboard as dash
from harvey import brain_auth, codex_link
from harvey.config import BrainConfig, load_brain_config
from harvey.modules import worker
from harvey.state import StateManager
from tests.test_codex import _leftover_runtimes, _write_login
from tests.test_codex import fake, private_tempdir  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the Codex provider isn't supported on Windows")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def codex_here(monkeypatch):
    monkeypatch.setattr(hc, "_PLATFORM", "linux")
    monkeypatch.setattr(hc, "find_codex", lambda: "codex")
    codex_link._link = None
    yield
    link = codex_link._link
    if link is not None and link.task is not None and not link.task.done():
        link.task.cancel()
    codex_link._link = None


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


def account_home() -> Path:
    return brain_auth.ensure_account_dir("codex")


async def _lease(state):
    return await worker.lease_holder(state, name=codex_link.LEASE)


# ── The link ──

@pytest.mark.asyncio
async def test_a_device_login_starts_only_after_the_config_is_attested_with_no_login(
        fake, tmp_path):
    fake.use("device_wait")
    home = tmp_path / "staging"
    home.mkdir(mode=0o700)
    login = await hc.start_device_login(home, timeout=30)
    try:
        assert login.url == "https://auth.openai.com/codex/device"
        assert login.code == "WXYZ-12345" and login.login_id == "login-1"
    finally:
        await login.cancel()
    methods = fake.methods()
    assert methods.index("config/read") < methods.index("account/login/start")
    assert methods[-1] == "account/login/cancel"
    assert not {"thread/start", "turn/start"} & set(methods)
    servers = fake.servers()
    assert [s["codex_home"] for s in servers] == [str(home)] and servers[0]["auth"] is None
    assert 'forced_login_method = "chatgpt"' in servers[0]["config_toml"]
    assert _leftover_runtimes() == []


@pytest.mark.asyncio
async def test_a_device_code_must_send_the_user_to_openai(fake, tmp_path):
    fake.use("device_bad_url")
    home = tmp_path / "staging"
    home.mkdir(mode=0o700)
    with pytest.raises(hc.CodexError):
        await hc.start_device_login(home, timeout=30)
    assert _leftover_runtimes() == []


@pytest.mark.asyncio
async def test_a_link_moves_the_login_in_sets_the_account_and_ends_its_process(fake, state):
    fake.use("device_ok")
    told = []

    async def linked():
        told.append(True)
    started = await codex_link.start(state, on_linked=linked)
    assert started["status"] == "pending" and started["code"] == "WXYZ-12345"
    assert started["url"].startswith("https://auth.openai.com/")
    assert 0 < started["expires_in"] <= hc.DEVICE_LOGIN_SECONDS
    await codex_link._link.task
    got = codex_link.status()
    assert got == {"status": "linked", "plan": "plus"} and told == [True]
    # The login Codex wrote is where calls take it from; the staging
    # folder, the runtime and the lease are gone.
    assert codex_link.is_linked() and hc.check_native_auth(account_home())
    assert not codex_link.staging_home().exists()
    assert _leftover_runtimes() == [] and await _lease(state) is None
    assert json.loads(await state.get_setting(codex_link.RENEWAL_KEY))["ok"] is True


@pytest.mark.asyncio
async def test_a_cancelled_link_leaves_the_login_already_there(fake, state):
    # (An API key kept in Harvey's folder stays as it was.)
    home = account_home()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey",
                                                "OPENAI_API_KEY": "sk-fake-0000"}))
    (home / "auth.json").chmod(0o600)
    fake.use("device_wait")
    await codex_link.start(state)
    with pytest.raises(codex_link.LinkError) as busy:
        await codex_link.start(state)
    assert busy.value.kind == "busy"
    got = await codex_link.cancel()
    assert got["status"] == "cancelled" and "code" not in got
    assert "account/login/cancel" in fake.methods()
    assert hc.has_api_key_login(home) and not codex_link.is_linked()
    assert not codex_link.staging_home().exists() and await _lease(state) is None


@pytest.mark.asyncio
async def test_a_code_nobody_typed_expires_and_its_process_ends(fake, state, monkeypatch):
    monkeypatch.setattr(hc, "DEVICE_LOGIN_SECONDS", 1)
    fake.use("device_wait")
    await codex_link.start(state)
    await codex_link._link.task
    assert codex_link.status() == {"status": "expired"}
    assert _leftover_runtimes() == [] and not codex_link.is_linked()


@pytest.mark.parametrize("scenario, kind", [("device_denied", "refused"),
                                            ("device_request", "isolation")])
@pytest.mark.asyncio
async def test_a_link_that_doesnt_complete_says_its_class_only(fake, state, scenario, kind):
    fake.use(scenario)
    await codex_link.start(state)
    await codex_link._link.task
    assert codex_link.status() == {"status": "failed", "kind": kind}
    assert not codex_link.is_linked() and await _lease(state) is None


@pytest.mark.asyncio
async def test_one_link_or_renewal_at_a_time_per_install(fake, state):
    assert await worker.acquire_lease(state, "renew-elsewhere", 30, codex_link.LEASE)
    with pytest.raises(codex_link.LinkError) as err:
        await codex_link.start(state)
    assert err.value.kind == "busy" and fake.servers() == []


@pytest.mark.asyncio
async def test_the_lease_is_kept_while_a_link_starts_slowly(state, monkeypatch):
    # (Starting the login can outlast the lease's life: nothing else may
    # take the lease meanwhile.)
    monkeypatch.setattr(codex_link, "LEASE_TTL_SECONDS", 0.3)

    async def slow(home, timeout=0):
        await asyncio.sleep(1.0)
        raise hc.CodexError("no device code", kind="protocol")
    monkeypatch.setattr(hc, "start_device_login", slow)
    starting = asyncio.create_task(codex_link.start(state))
    await asyncio.sleep(0.7)
    assert not await worker.acquire_lease(state, "renew-elsewhere", 0.3, codex_link.LEASE)
    with pytest.raises(codex_link.LinkError):
        await starting
    assert await _lease(state) is None


@pytest.mark.parametrize("breaks, status", [("_move_in", "failed"), ("_record", "linked")])
@pytest.mark.asyncio
async def test_a_link_that_breaks_after_the_code_never_stays_pending(
        fake, state, monkeypatch, breaks, status):
    fake.use("device_ok")

    def move_in():
        raise RuntimeError("fictional")

    async def record(*a, **k):
        raise RuntimeError("fictional")
    monkeypatch.setattr(codex_link, breaks, move_in if breaks == "_move_in" else record)
    await codex_link.start(state)
    await codex_link._link.task
    assert codex_link.status()["status"] == status
    assert await _lease(state) is None and _leftover_runtimes() == []
    # (A new link can start: nothing waits for a code forever.)
    fake.use("device_wait")
    assert (await codex_link.start(state))["status"] == "pending"
    await codex_link.cancel()


@pytest.mark.asyncio
async def test_linked_is_said_only_once_the_brain_is_set_to_it(fake, state):
    # (The panel reloads on "linked": brain.auth must be saved by then.)
    fake.use("device_ok")
    seen = []

    async def linked():
        await asyncio.sleep(0.3)
        seen.append(codex_link.status()["status"])
    await codex_link.start(state, on_linked=linked)
    for _ in range(100):
        if (account_home() / "auth.json").exists():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert codex_link.status()["status"] == "pending"
    await codex_link._link.task
    assert seen == ["pending"] and codex_link.status()["status"] == "linked"


@pytest.mark.asyncio
async def test_a_cancel_once_the_code_was_typed_lets_the_login_land(fake, state, monkeypatch):
    fake.use("device_ok")
    told, record = [], codex_link._record

    async def slow_record(*a, **k):
        await asyncio.sleep(0.5)
        return await record(*a, **k)
    monkeypatch.setattr(codex_link, "_record", slow_record)

    async def linked():
        told.append(True)
    await codex_link.start(state, on_linked=linked)
    for _ in range(200):
        if (account_home() / "auth.json").exists():
            break
        await asyncio.sleep(0.01)
    got = await codex_link.cancel()
    assert got["status"] == "linked" and told == [True] and codex_link.is_linked()
    assert codex_link._link.login.code == "" and await _lease(state) is None


# ── Renewal ──

def _linked(refreshed_ago: float, expires_in: float = 9 * 86400) -> Path:
    home = account_home()
    _write_login(home, expires_in=expires_in, refreshed_ago=refreshed_ago)
    return home


@pytest.mark.asyncio
async def test_harvey_renews_the_linked_login_in_its_own_folder(fake, state):
    home = _linked(refreshed_ago=2 * 86400)
    before = json.loads((home / "auth.json").read_text())
    got = await codex_link.renew_if_due(state)
    assert got["ok"] is True and got["kind"] == ""
    after = json.loads((home / "auth.json").read_text())
    assert hc.last_refreshed(after) > hc.last_refreshed(before)
    # Attested first in a runtime with no login at all, then in Harvey's
    # folder; one account/read asking for the refresh, nothing else.
    servers = fake.servers()
    assert servers[0]["auth"] is None and servers[0]["codex_home"] != str(home)
    assert servers[-1]["codex_home"] == str(home)
    reads = [e["params"] for e in fake.entries() if e.get("method") == "account/read"]
    assert reads == [{"refreshToken": True}]
    assert not {"thread/start", "turn/start"} & set(fake.methods())
    assert _leftover_runtimes() == [] and await _lease(state) is None
    # Renewed: not due again for a day.
    assert await codex_link.renew_if_due(state) is None
    assert len(fake.servers()) == len(servers)


@pytest.mark.asyncio
async def test_the_lease_is_kept_while_a_renewal_runs_long(state, monkeypatch):
    monkeypatch.setattr(codex_link, "LEASE_TTL_SECONDS", 0.3)
    _linked(refreshed_ago=2 * 86400)

    async def slow(home, timeout=0):
        await asyncio.sleep(1.0)
        return {"plan": "plus"}
    monkeypatch.setattr(hc, "renew_login", slow)
    renewing = asyncio.create_task(codex_link.renew_if_due(state))
    await asyncio.sleep(0.7)
    # (A link can't start while Codex may still be writing the login.)
    assert not await worker.acquire_lease(state, "link-elsewhere", 0.3, codex_link.LEASE)
    await renewing
    assert await _lease(state) is None


@pytest.mark.asyncio
async def test_a_login_about_to_run_out_is_renewed_early(state):
    native = json.loads(_linked(refreshed_ago=3600, expires_in=3 * 3600)
                        .joinpath("auth.json").read_text())
    now = time.time()
    assert codex_link.due(native, {"ok": True, "next_at": now + 80000}, now)
    fresh = json.loads(_linked(refreshed_ago=3600).joinpath("auth.json").read_text())
    assert not codex_link.due(fresh, {}, now)
    old = json.loads(_linked(refreshed_ago=2 * 86400).joinpath("auth.json").read_text())
    assert codex_link.due(old, {}, now)
    # A failure waits its retry, even for a login running out.
    assert not codex_link.due(native, {"ok": False, "next_at": now + 60}, now)


@pytest.mark.parametrize("scenario, kind", [("refresh_fails", "protocol"),
                                            ("refresh_gone", "auth"),
                                            ("refresh_stays", "")])
@pytest.mark.asyncio
async def test_a_failed_renewal_is_retried_and_says_link_again_once_unusable(
        fake, state, scenario, kind):
    _linked(refreshed_ago=2 * 86400)
    fake.use(scenario)
    got = await codex_link.renew_if_due(state)
    if scenario == "refresh_stays":
        # (Codex answered without renewing a login still good for days:
        # tried again in a day, not an error.)
        assert got["ok"] is True
        return
    assert got["ok"] is False and got["kind"] == kind
    assert got["next_at"] - got["at"] == codex_link.RENEW_RETRY_SECONDS
    assert await codex_link.renew_if_due(state) is None          # (waits its retry)
    linked = await codex_link.renewal_status(state)
    assert linked["linked"] is True and linked["expired"] is (kind == "auth")
    # Past the point a call can use it: link again, and calls say so.
    _linked(refreshed_ago=9 * 86400)
    assert (await codex_link.renewal_status(state))["expired"] is True
    _, native = hc._load_native_auth(account_home())
    with pytest.raises(hc.CodexError) as err:
        hc.build_access_snapshot(native, linked=True)
    assert err.value.kind == "auth" and "link the account again" in str(err.value)


@pytest.mark.asyncio
async def test_the_terminal_login_is_never_renewed(fake, state, tmp_path, monkeypatch):
    native = tmp_path / "dot-codex"
    native.mkdir()
    _write_login(native, refreshed_ago=8 * 86400)
    monkeypatch.setenv("CODEX_HOME", str(native))
    before = (native / "auth.json").read_bytes()
    assert await codex_link.renew_if_due(state, force=True) is None
    assert fake.servers() == [] and (native / "auth.json").read_bytes() == before
    with pytest.raises(hc.CodexError) as err:
        hc.build_access_snapshot(json.loads(before))
    assert "Harvey never renews" in str(err.value)


@pytest.mark.asyncio
async def test_a_call_copies_the_linked_login_without_its_refresh_token(fake, state):
    _linked(refreshed_ago=3600)
    client = hc.CodexClient(model="gpt-5.5", native_home=account_home())
    await client.run("Say hi.", timeout=30)
    given = [s["auth"] for s in fake.servers() if s["auth"]]
    assert given and all(a["tokens"]["refresh_token"] == "" for a in given)
    assert all(s["codex_home"] != str(account_home()) for s in fake.servers())


@pytest.mark.asyncio
async def test_the_worker_looks_for_a_renewal_beside_its_loop(state, monkeypatch):
    seen = []

    async def renew(st, now=None, force=False):
        seen.append(st)
    monkeypatch.setattr(codex_link, "renew_if_due", renew)
    w = worker.Worker(state, name="w-test", poll=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await task
    assert seen == [state]


# ── Unlink ──

@pytest.mark.parametrize("scenario", ["ok", "logout_fails"])
@pytest.mark.asyncio
async def test_unlink_is_codexs_own_logout_and_the_file_goes_either_way(fake, state, scenario):
    _linked(refreshed_ago=3600)
    fake.use(scenario)
    assert await codex_link.unlink(state) is True
    assert not (account_home() / "auth.json").exists()
    assert ("account/logout" in fake.methods()) is True
    assert await codex_link.unlink(state) is False


# ── The dashboard ──

TEMPLATE = """# Harvey's settings (fictional)
persona:
  name: H
  company: Acme Example
brain:
  provider: codex
  model: gpt-6-luna
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    (tmp_path / "harvey.yaml").write_text(TEMPLATE, encoding="utf-8")
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


def _saved_auth(client):
    path = client.dir / "harvey.local.yaml"
    return load_brain_config(str(path)).auth if path.exists() else "terminal"


def test_the_panel_links_the_account_and_sets_the_brain_to_it(client, fake):
    r = client.post("/api/brain", json={"provider": "codex", "auth": "account"})
    assert r.status_code == 409 and "Link the ChatGPT account first" in r.json()["message"]
    fake.use("device_ok")
    started = client.post("/api/brain/codex/link").json()
    assert started["status"] == "pending" and started["code"] == "WXYZ-12345"
    for _ in range(200):
        got = client.get("/api/brain/codex/link").json()
        if got["status"] != "pending":
            break
        time.sleep(0.05)
    assert got == {"status": "linked", "plan": "plus"}
    assert _saved_auth(client) == "account"
    brain = client.get("/api/brain").json()
    assert brain["offered"] == ["terminal", "account", "api_key"]
    assert brain["codex_account"]["linked"] is True
    assert brain["codex_account"]["link"]["status"] == "linked"
    # Unlink: Codex's logout, and the brain back on the terminal login.
    r = client.delete("/api/brain/codex/account").json()
    assert r == {"success": True, "removed": True, "auth": "terminal"}
    assert _saved_auth(client) == "terminal" and not codex_link.is_linked()


def test_a_pending_link_can_be_cancelled_from_the_panel(client, fake):
    fake.use("device_wait")
    assert client.post("/api/brain/codex/link").json()["status"] == "pending"
    assert client.post("/api/brain/codex/link").status_code == 409        # one at a time
    assert client.delete("/api/brain/codex/link").json()["status"] == "cancelled"
    assert client.get("/api/brain/codex/link").json()["status"] == "cancelled"


def test_the_link_endpoints_need_the_token(client):
    bare = TestClient(dash.app, base_url="http://127.0.0.1:5555")
    for method, url in (("post", "/api/brain/codex/link"), ("get", "/api/brain/codex/link"),
                        ("delete", "/api/brain/codex/link"),
                        ("delete", "/api/brain/codex/account")):
        assert getattr(bare, method)(url).status_code == 401


def test_claude_offers_its_own_account_link_once_linked(client):
    from harvey.config import set_brain_settings
    set_brain_settings({"provider": "claude"}, client.dir / "harvey.yaml")
    assert client.get("/api/brain").json()["offered"] == ["terminal", "account", "api_key"]
    r = client.post("/api/brain", json={"provider": "claude", "auth": "account"})
    assert r.status_code == 409 and "Link the Claude account first" in r.json()["message"]


def test_today_says_link_again_when_the_renewal_failed(client, monkeypatch):
    from harvey.config import set_brain_settings
    set_brain_settings({"auth": "account"}, client.dir / "harvey.yaml", provider="codex")
    monkeypatch.setattr(dash, "_active_brain",
                        lambda: BrainConfig(provider="codex", model="gpt-6-luna", auth="account"))
    _linked(refreshed_ago=9 * 86400)
    keys = [i["key"] for i in client.get("/api/today").json()["items"]]
    assert "codex-login" in keys
    _linked(refreshed_ago=3600)
    keys = [i["key"] for i in client.get("/api/today").json()["items"]]
    assert "codex-login" not in keys


# ── Docker and the docs ──

def test_the_image_installs_the_one_supported_codex_and_checks_it():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pinned = re.search(r"^ARG CODEX_VERSION=(\S+)$", text, re.M).group(1)
    assert pinned == hc.SUPPORTED_CODEX_VERSIONS[-1]
    assert '"@openai/codex@${CODEX_VERSION}"' in text
    assert "from harvey.codex import check_cli_version" in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Codex isn't supported in Docker" not in readme


# ── The panel under node ──

def _codex_status(**account):
    from tests.test_brain_auth import CLAUDE
    return {**CLAUDE, "provider": "codex", "model": "gpt-6-luna", "auth": "account",
            "offered": ["terminal", "account", "api_key"], "folder": "data/auth/codex",
            "cli": {"ok": True, "version": "0.159.3", "problem": ""},
            "models": [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "efforts": []}],
            "codex_account": account, **({"linked": False, "linked_as": {}}
                                         if not account.get("linked") else
                                         {"linked": True, "linked_as": {"method": "chatgpt",
                                                                        "plan": "plus"}})}


@pytest.mark.parametrize("lang, words", [
    ("en", ["Link account", "ChatGPT", "Settings → Security"]),
    ("ro", ["Conectează contul", "Settings → Security"]),
])
def test_the_panel_offers_the_account_link_for_codex(lang, words):
    from tests.test_brain_auth import _panel
    html = _panel(lang, _codex_status(linked=False, expired=False))
    assert 'value="account" checked' in html and 'id="model-link-codex"' in html
    for w in words:
        assert w in html, w


@pytest.mark.parametrize("lang, words", [
    ("en", ["1. Open this page:", "type this code", "Waiting for the code", "15 minutes",
            "Cancel", "Copy"]),
    ("ro", ["1. Deschide pagina:", "scrie acest cod", "Se așteaptă codul", "Anulează",
            "Copiază"]),
])
def test_a_pending_link_shows_the_page_and_the_code_to_type(lang, words):
    from tests.test_brain_auth import _panel
    link = {"status": "pending", "url": "https://auth.openai.com/codex/device",
            "code": "WXYZ-12345", "expires_in": 900}
    html = _panel(lang, _codex_status(linked=False, expired=False, link=link))
    assert '<code id="codex-link-code">WXYZ-12345</code>' in html
    assert 'href="https://auth.openai.com/codex/device" target="_blank" rel="noopener noreferrer"' in html
    assert 'id="model-link-codex"' not in html           # (one link at a time)
    for w in words:
        assert w in html, w


def test_a_linked_account_shows_unlink_and_link_again_once_expired():
    from tests.test_brain_auth import _panel
    html = _panel("en", _codex_status(linked=True, expired=False, renewed_at=1790000000,
                                      last_try={"at": 1790000000, "ok": True, "kind": ""}))
    assert "ChatGPT account linked in Harvey" in html and "renewed" in html
    assert 'id="model-unlink-account-codex"' in html and 'id="model-link-codex"' not in html
    html = _panel("en", _codex_status(linked=True, expired=True, renewed_at=1790000000,
                                      last_try={"at": 1790000000, "ok": False, "kind": "auth"}))
    assert "link the account again" in html and "the last renewal failed (auth)" in html
    assert "Link the account again" in html


# ── Linked while Claude was chosen ──

CLAUDE_TEMPLATE = """# Harvey's settings (fictional)
persona:
  name: H
  company: Acme Example
brain:
  provider: claude
"""


def test_a_new_default_takes_the_account_linked_in_harvey_when_it_never_named_one(monkeypatch):
    monkeypatch.setattr(codex_link, "is_linked", lambda: True)
    # (Linked while Claude was the default, before each provider kept its
    # own login: an older config names none for Codex.)
    assert brain_auth.linked_auth(BrainConfig(provider="claude"), "codex") == "account"
    # A login Codex's settings name stays its own.
    named = BrainConfig(provider="claude", codex={"auth": "terminal"})
    assert brain_auth.linked_auth(named, "codex") == ""
    monkeypatch.setattr(codex_link, "is_linked", lambda: False)
    assert brain_auth.linked_auth(BrainConfig(provider="claude"), "codex") == ""


def _link(client, fake):
    fake.use("device_ok")
    assert client.post("/api/brain/codex/link").json()["status"] == "pending"
    for _ in range(200):
        got = client.get("/api/brain/codex/link").json()
        if got["status"] != "pending":
            break
        time.sleep(0.05)
    assert got["status"] == "linked"


def test_codex_linked_while_claude_is_chosen_shows_its_models_and_saves_with_it(
        client, fake, monkeypatch):
    from tests.conftest import REAL_CODEX_STATUS

    (client.dir / "harvey.yaml").write_text(CLAUDE_TEMPLATE, encoding="utf-8")
    _link(client, fake)
    homes = []

    async def describe(self, timeout=0):
        # (Codex's answer, read from the folder the client was given.)
        homes.append(self.native_home)
        return {"version": "0.159.3", "account": {"type": "chatgpt", "plan": "plus"},
                "models": [{"id": "gpt-6-luna", "name": "gpt-6-luna", "efforts": []}]}
    monkeypatch.setattr(brain_auth, "codex_status", REAL_CODEX_STATUS)
    monkeypatch.setattr(hc, "check_cli_version", lambda *a, **k: "0.159.3")
    monkeypatch.setattr(hc.CodexClient, "describe", describe)
    # The link never switches the provider: Claude stays the default, on its
    # own login, and Codex's own login is the account linked.
    linked = load_brain_config(str(client.dir / "harvey.local.yaml"))
    assert (linked.provider, linked.auth) == ("claude", "terminal")
    assert linked.codex.auth == "account"
    seen = client.get("/api/brain?provider=codex&full=1&refresh=1").json()
    assert seen["auth"] == "account"
    assert "No Codex login found" not in (seen.get("problem") or "")
    assert seen["linked"] is True and [m["id"] for m in seen["models"]] == ["gpt-6-luna"]
    assert homes and all(h == account_home() for h in homes)
    # The login picked on the panel is the one asked about.
    terminal = client.get("/api/brain?provider=codex&auth=terminal").json()
    assert terminal["auth"] == "terminal"
    # Codex's own model saved (Claude still the default), then Codex made
    # the default: on the account linked.
    model = seen["models"][0]["id"]
    r = client.post("/api/brain", json={"provider": "codex", "model": model})
    assert r.status_code == 200, r.text
    assert load_brain_config(str(client.dir / "harvey.local.yaml")).provider == "claude"
    assert client.post("/api/brain", json={"default": "codex"}).status_code == 200
    saved = load_brain_config(str(client.dir / "harvey.local.yaml"))
    assert (saved.provider, saved.auth, saved.model) == ("codex", "account", model)
    assert saved.claude.auth == "terminal"
