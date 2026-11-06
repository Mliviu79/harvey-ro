"""The Claude account linked in Harvey (brain.auth: account): the CLI's own
``claude auth login`` relayed from Settings → Model (its page shown, the
pasted code written to its prompt under a pseudo-terminal), the account
moved in by one rename, a Console account held by the USD caps, the
logout, and what the panel and Today say.

The CLI is tests/fake_claude_login.py throughout, shaped like what the
real one printed with no login: no claude runs, no network, no login.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import brain_auth, brain_spend, claude_link, claude_runtime
from harvey.config import BrainConfig, load_brain_config, set_brain_settings
from harvey.modules import worker
from harvey.state import StateManager
from tests.fake_claude_login import GOOD_CODE

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the link runs the CLI under a pseudo-terminal")

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_claude_login.py"


class FakeCli:
    def __init__(self, folder: Path):
        self.folder = folder
        self.log = folder / "calls.jsonl"
        self.path = folder / "claude"

    def use(self, scenario: str = "ok") -> str:
        self.path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {scenario} '
                             f'"{self.log}" "$@"\n', encoding="utf-8")
        self.path.chmod(0o755)
        return str(self.path)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake(monkeypatch, tmp_path_factory):
    cli = FakeCli(tmp_path_factory.mktemp("fake-claude"))
    cli.use("ok")
    monkeypatch.setattr(claude_runtime, "check_cli", lambda *a, **k: str(cli.path))
    # (A credential in Harvey's own environment never reaches the CLI.)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-credential-for-test")
    claude_link._link = None
    yield cli
    link = claude_link._link
    if link is not None and link.task is not None and not link.task.done():
        link.task.cancel()
    claude_link._link = None


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "harvey.db"))
    await s.init_db()
    return s


async def _until(predicate, seconds: float = 20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = claude_link.status()
        if predicate(got):
            return got
        await asyncio.sleep(0.05)
    raise AssertionError(f"still {claude_link.status()}")


async def _ended():
    link = claude_link._link
    await asyncio.gather(link.task, return_exceptions=True)
    return claude_link.status()


async def _lease(state):
    return await worker.lease_holder(state, name=claude_link.LEASE)


def _signed_in(folder: Path) -> bool:
    # (The stand-in's own file: what a test checks, never Harvey.)
    return (folder / "fictional-login").exists()


# ── What is relayed ──

@pytest.mark.parametrize("url", [
    "https://claude.com/cai/oauth/authorize?code=true",
    "https://platform.claude.com/oauth/authorize?x=1",
    "https://claude.ai/oauth/authorize",
    "https://console.anthropic.com/oauth/authorize",
])
def test_a_claude_or_anthropic_page_is_shown(url):
    assert claude_link.url_problem(url) == ""


@pytest.mark.parametrize("url", [
    "http://claude.com/cai/oauth/authorize",
    "https://login.example.com/oauth/authorize",
    "https://claude.com.example.com/oauth",
    "https://notclaude.com/oauth",
    "https://someone@claude.com/oauth",
    "https://claude.com:8443/oauth",
    "javascript:alert(1)",
])
def test_any_other_page_fails_the_link(url):
    assert claude_link.url_problem(url) != ""


def test_the_page_is_read_from_the_clis_output_without_its_escapes():
    text = ("Opening browser to sign in…\r\nIf the browser didn't open, visit: "
            "\x1b]8;;https://claude.com/cai/oauth/authorize?a=1\x07https://claude.com/cai/oauth/"
            "authorize?a=1\x1b]8;;\x07\r\nPaste code here if prompted > ")
    assert claude_link.login_url(text) == "https://claude.com/cai/oauth/authorize?a=1"
    assert claude_link.login_url("Opening browser to sign in…\r\n") is None


@pytest.mark.parametrize("code, ok", [
    (GOOD_CODE, True), ("  abc#def  ", True), ("", False), (None, False), (42, False),
    ("two words", False), ("line\nbreak", False), ("x" * 513, False), ("café#x", False),
])
def test_only_one_line_of_printable_characters_is_written_as_a_code(code, ok):
    assert (claude_link.code_problem(code) == "") is ok


# ── The link ──

@pytest.mark.asyncio
async def test_a_link_relays_the_page_and_the_code_then_moves_the_account_in(
        fake, state, caplog):
    caplog.set_level(logging.DEBUG)
    told = []

    async def linked():
        told.append(True)
    started = await claude_link.start(state, "subscription", on_linked=linked)
    assert started["status"] == "waiting_code" and started["account"] == "subscription"
    assert started["url"].startswith("https://claude.com/cai/oauth/authorize?")
    assert 0 < started["expires_in"] <= claude_link.LINK_SECONDS
    # A malformed code: the CLI asks again, and so does the panel.
    assert (await claude_link.submit_code("not-a-code"))["status"] == "pending"
    got = await _until(lambda s: s.get("problem") == "invalid_code")
    assert got["status"] == "waiting_code"
    await claude_link.submit_code(GOOD_CODE)
    got = await _ended()
    assert got == {"status": "linked", "account": "subscription", "method": "claude.ai",
                   "plan": "max"}
    assert told == [True]
    # The CLI's login is where calls look for it; the staging folder, the
    # process's terminal and the lease are gone; Harvey noted the kind.
    assert _signed_in(claude_link.account_home())
    assert not claude_link.staging_home().exists()
    assert brain_auth.claude_account_kind() == "subscription" and claude_link.has_account()
    assert not brain_auth.billed("claude", "account")
    assert await _lease(state) is None
    calls = fake.calls()
    login = [c for c in calls if c["args"][:2] == ["auth", "login"]]
    assert login and login[0]["args"] == ["auth", "login", "--claudeai"]
    assert login[0]["tty"] and login[0]["ctty"] and login[0]["claude_mds_off"] == "1"
    assert login[0]["home"] == str(claude_link.staging_home())
    status = [c for c in calls if c["args"][:2] == ["auth", "status"]]
    assert status and status[0]["home"] == str(claude_link.staging_home())
    assert not any(c["credential"] for c in calls)
    # Neither the code nor the page's query is ever logged.
    for record in caplog.records:
        text = record.getMessage()
        assert GOOD_CODE not in text and "not-a-code" not in text
        assert "QUERYSECRET" not in text and "oauth/authorize" not in text


@pytest.mark.asyncio
async def test_a_code_the_cli_refuses_fails_the_link_and_leaves_the_account_there(
        fake, state):
    home = claude_link.account_home()
    home.mkdir(parents=True)
    (home / "earlier").write_text("an earlier login (fictional)")
    await claude_link.start(state)
    await claude_link.submit_code("abc#def")
    got = await _ended()
    assert got["status"] == "failed" and got["kind"] == "refused" and "url" not in got
    assert (home / "earlier").exists() and not _signed_in(home)
    assert not claude_link.staging_home().exists() and await _lease(state) is None


@pytest.mark.asyncio
async def test_a_page_harvey_wont_send_you_to_fails_the_link(fake, state, caplog):
    caplog.set_level(logging.DEBUG)
    fake.use("bad_url")
    with pytest.raises(claude_link.LinkError) as e:
        await claude_link.start(state)
    assert e.value.kind == "url" and "QUERYSECRET" not in str(e.value)
    assert "example.com" not in str(e.value)
    assert not claude_link.staging_home().exists() and await _lease(state) is None
    assert claude_link.status()["status"] == "idle"
    assert not any("QUERYSECRET" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, kind", [("no_url", "timeout"), ("exit_early", "setup")])
async def test_a_cli_that_shows_no_page_fails_the_link(fake, state, monkeypatch, scenario,
                                                       kind):
    monkeypatch.setattr(claude_link, "URL_SECONDS", 1)
    fake.use(scenario)
    with pytest.raises(claude_link.LinkError) as e:
        await claude_link.start(state)
    assert e.value.kind == kind
    assert not claude_link.staging_home().exists() and await _lease(state) is None


@pytest.mark.asyncio
async def test_a_link_nobody_finishes_expires_and_its_process_ends(fake, state, monkeypatch):
    monkeypatch.setattr(claude_link, "LINK_SECONDS", 1)
    await claude_link.start(state)
    process = claude_link._link.cli.process
    got = await _ended()
    assert got["status"] == "expired" and "url" not in got
    assert process.returncode is not None
    assert not claude_link.staging_home().exists() and await _lease(state) is None


@pytest.mark.asyncio
async def test_a_signed_in_cli_that_doesnt_exit_is_asked_and_ended(fake, state, monkeypatch):
    monkeypatch.setattr(claude_link, "SETTLE_SECONDS", 0.2)
    fake.use("linger")
    await claude_link.start(state)
    process = claude_link._link.cli.process
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"
    assert process.returncode is not None and _signed_in(claude_link.account_home())


@pytest.mark.asyncio
async def test_a_cancelled_link_ends_its_process_and_one_runs_at_a_time(fake, state):
    started = await claude_link.start(state)
    process = claude_link._link.cli.process
    with pytest.raises(claude_link.LinkError) as busy:
        await claude_link.start(state)
    assert busy.value.kind == "busy"
    claude_link.account_home().mkdir(parents=True)          # (an account to unlink)
    with pytest.raises(claude_link.LinkError) as busy:
        await claude_link.unlink(state)
    assert busy.value.kind == "busy"
    got = await claude_link.cancel()
    assert started["status"] == "waiting_code"
    assert got == {"status": "cancelled", "account": "subscription"}
    assert process.returncode is not None
    assert not claude_link.staging_home().exists() and await _lease(state) is None
    with pytest.raises(claude_link.LinkError) as e:
        await claude_link.submit_code(GOOD_CODE)
    assert e.value.kind == "state"


@pytest.mark.asyncio
async def test_a_link_waits_while_another_process_holds_the_lease(fake, state):
    assert await worker.acquire_lease(state, "elsewhere", 30, claude_link.LEASE)
    with pytest.raises(claude_link.LinkError) as e:
        await claude_link.start(state)
    assert e.value.kind == "busy" and fake.calls() == []


@pytest.mark.asyncio
async def test_the_folder_there_before_is_put_back_when_the_rename_fails(
        fake, state, monkeypatch):
    home = claude_link.account_home()
    home.mkdir(parents=True)
    (home / "earlier").write_text("an earlier login (fictional)")
    claude_link._write_note("subscription")
    real = os.replace

    def replace(src, dst):
        if Path(src) == claude_link.staging_home():
            raise OSError("fictional failure")
        return real(src, dst)
    monkeypatch.setattr(claude_link.os, "replace", replace)
    await claude_link.start(state, "console")
    await claude_link.submit_code(GOOD_CODE)
    got = await _ended()
    assert got["status"] == "failed" and got["kind"] == "save"
    assert (home / "earlier").exists() and not claude_link._aside_home().exists()
    assert brain_auth.claude_account_kind() == "subscription"


@pytest.mark.asyncio
async def test_a_new_link_replaces_the_folder_there_before(fake, state):
    home = claude_link.account_home()
    home.mkdir(parents=True)
    (home / "earlier").write_text("an earlier login (fictional)")
    await claude_link.start(state)
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"
    assert not (home / "earlier").exists() and _signed_in(home)
    assert not claude_link._aside_home().exists()


def test_a_page_the_output_ends_on_is_waited_for():
    # The output comes in pieces: a page cut short (or one still inside
    # its hyperlink's escape) is never shown, the whole one is.
    page = "https://claude.com/cai/oauth/authorize?code=true&state=QUERYSECRET"
    head = "If the browser didn't open, visit: "
    assert claude_link.login_url(head + page[:40]) is None
    assert claude_link.login_url(head + "\x1b]8;;" + page[:40]) is None
    assert claude_link.login_url(head + "\x1b]8;;" + page + "\x07" + page[:30]) is None
    assert claude_link.login_url(head + "\x1b]8;;" + page + "\x07" + page + "\x1b]8") is None
    assert claude_link.login_url(head + "\x1b]8;;" + page + "\x07" + page + "\x1b]8;;\x07\r\n") \
        == page


@pytest.mark.asyncio
async def test_the_terminal_is_closed_once(tmp_path):
    # A second stop (the link's own, then its end's) must not close the
    # descriptor its number was given to since.
    master, slave = os.openpty()
    os.close(slave)
    p = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await p.wait()
    cli = claude_link._Cli(process=p, master=master)
    await cli.stop()
    other = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(other, master)                  # (the number reused elsewhere)
        await cli.stop()
        os.fstat(master)                        # still open
        with pytest.raises(OSError):
            cli.write("x")
    finally:
        os.close(other)
        try:
            os.close(master)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_a_system_with_no_pseudo_terminal_refuses_the_link(fake, state, monkeypatch):
    monkeypatch.setitem(sys.modules, "pty", None)           # (as on Windows)
    with pytest.raises(claude_link.LinkError) as e:
        await claude_link.start(state)
    assert e.value.kind == "setup" and "Linux or macOS" in str(e.value)
    assert fake.calls() == [] and await _lease(state) is None


@pytest.mark.asyncio
async def test_an_answer_after_more_output_than_is_kept_is_still_read(fake, state, monkeypatch):
    monkeypatch.setattr(claude_link, "CODE_SECONDS", 120)
    fake.use("chatty")
    await claude_link.start(state)
    await _until(lambda s: claude_link._link.cli.total > claude_link.OUTPUT_MAX_CHARS)
    await claude_link.submit_code("not-a-code")
    got = await _until(lambda s: s.get("problem") == "invalid_code", 10)
    assert got["status"] == "waiting_code"
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"


@pytest.mark.asyncio
async def test_a_cli_that_ends_after_a_code_is_decided_by_its_status(fake, state, monkeypatch):
    # Its last words read as asking again (an earlier message drawn
    # again), then it signed in and exited: linked, as its status says.
    monkeypatch.setattr(claude_link, "CODE_SECONDS", 120)
    fake.use("sticky_invalid")
    await claude_link.start(state)
    await claude_link.submit_code("not-a-code")
    await _until(lambda s: s.get("problem") == "invalid_code")
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"
    assert _signed_in(claude_link.account_home())


@pytest.mark.asyncio
async def test_a_console_account_is_billed_and_held_by_the_caps(fake, state):
    started = await claude_link.start(state, "console")
    assert started["url"].startswith("https://platform.claude.com/oauth/authorize?")
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"
    assert [c["args"] for c in fake.calls() if c["args"][:2] == ["auth", "login"]] == [
        ["auth", "login", "--console"]]
    assert brain_auth.claude_account_kind() == "console"
    brain = BrainConfig(provider="claude", auth="account")
    assert brain_auth.billed("claude", "account") and brain_spend.api_key(brain)
    # With no cap, no call is made.
    assert await brain_spend.refusal(state, brain) == brain_spend.NO_CAP
    # (Codex's account is never a Console one.)
    assert not brain_auth.billed("codex", "account")


def test_a_note_harvey_cant_read_counts_as_billed(monkeypatch):
    brain_auth._owner_only(brain_auth.AUTH_DIR)
    assert brain_auth.claude_account_kind() == ""
    brain_auth.claude_link_note().write_text("not json")
    assert brain_auth.claude_account_kind() == "console"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ok", "logout_fails"])
async def test_unlink_is_the_clis_own_logout_and_the_folder_goes_either_way(
        fake, state, scenario):
    await claude_link.start(state)
    await claude_link.submit_code(GOOD_CODE)
    assert (await _ended())["status"] == "linked"
    fake.use(scenario)
    assert await claude_link.unlink(state) is True
    logout = [c for c in fake.calls() if c["args"][:2] == ["auth", "logout"]]
    assert logout and logout[0]["home"] == str(claude_link.account_home())
    assert not claude_link.account_home().exists() and not claude_link.has_account()
    assert await _lease(state) is None
    assert await claude_link.unlink(state) is False


# ── The status ──

def test_a_linked_account_logged_out_says_link_again(monkeypatch):
    from tests.conftest import REAL_CLAUDE_STATUS

    def fake_run(exe, args, auth):
        out = "2.1.318 (Claude Code)\n" if args == ["--version"] else json.dumps(
            {"loggedIn": False, "authMethod": "none"})
        return subprocess.CompletedProcess(args, 1, out, "")
    monkeypatch.setattr(brain_auth, "_run_claude", fake_run)
    monkeypatch.setattr(claude_runtime, "check_cli", lambda *a, **k: "/bin/claude")
    assert "press Link account" in REAL_CLAUDE_STATUS("account")["problem"]
    claude_link._write_note("subscription")
    assert REAL_CLAUDE_STATUS("account")["problem"] == brain_auth.LINK_ENDED


# ── The dashboard ──

TEMPLATE = """# Harvey's settings (fictional)
persona:
  name: H
  company: Acme Example
brain:
  provider: claude
"""


@pytest.fixture
def client(monkeypatch, tmp_path, fake):
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


def _saved(client):
    path = client.dir / "harvey.local.yaml"
    return load_brain_config(str(path if path.exists() else client.dir / "harvey.yaml"))


def _poll(client, until, seconds: float = 20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = client.get("/api/brain/claude/link").json()
        if until(got):
            return got
        time.sleep(0.05)
    raise AssertionError(got)


def test_the_panel_links_the_account_and_sets_the_brain_to_it(client):
    got = client.get("/api/brain").json()
    assert got["offered"] == ["terminal", "account", "api_key"]
    assert got["claude_account"] == {"kind": "", "link": {"status": "idle"}}
    r = client.post("/api/brain", json={"auth": "account"})
    assert r.status_code == 409 and "Link the Claude account first" in r.json()["message"]
    assert client.post("/api/brain/claude/link", json={"account": "other"}).status_code == 400
    started = client.post("/api/brain/claude/link", json={"account": "subscription"}).json()
    assert started["status"] == "waiting_code" and started["url"].startswith("https://claude.com/")
    assert client.post("/api/brain/claude/link", json={}).status_code == 409   # one at a time
    r = client.post("/api/brain/claude/link/code", json={"code": "two words"})
    assert r.status_code == 400
    client.post("/api/brain/claude/link/code", json={"code": GOOD_CODE})
    got = _poll(client, lambda s: s["status"] not in ("pending", "waiting_code"))
    assert got["status"] == "linked"
    assert _saved(client).auth == "account"
    assert client.get("/api/brain").json()["claude_account"]["kind"] == "subscription"
    r = client.post("/api/brain/claude/link/code", json={"code": GOOD_CODE})
    assert r.status_code == 409                          # no link waits for one
    # Unlink: the CLI's logout, and the brain back on the terminal login.
    r = client.delete("/api/brain/claude/account").json()
    assert r == {"success": True, "removed": True, "auth": "terminal"}
    assert _saved(client).auth == "terminal" and not claude_link.has_account()


def test_a_link_under_way_can_be_cancelled_from_the_panel(client):
    assert client.post("/api/brain/claude/link", json={}).json()["status"] == "waiting_code"
    assert client.delete("/api/brain/claude/link").json()["status"] == "cancelled"
    assert client.get("/api/brain/claude/link").json()["status"] == "cancelled"


def test_a_console_account_needs_a_cap_before_the_brain_uses_it(client):
    client.post("/api/brain/claude/link", json={"account": "console"})
    client.post("/api/brain/claude/link/code", json={"code": GOOD_CODE})
    assert _poll(client, lambda s: s["status"] == "linked")["account"] == "console"
    set_brain_settings({"auth": "terminal"}, dash._resolve_config_file())
    r = client.post("/api/brain", json={"auth": "account"})
    assert r.status_code == 400 and "USD cap" in r.json()["message"]
    r = client.post("/api/brain", json={"auth": "account", "usd_per_day": "2"})
    assert r.status_code == 200 and _saved(client).auth == "account"
    got = client.get("/api/brain").json()
    assert got["billed"] is True and got["spend"] is not None


def test_the_link_endpoints_need_the_token(client):
    bare = TestClient(dash.app, base_url="http://127.0.0.1:5555")
    for method, url in (("post", "/api/brain/claude/link"), ("get", "/api/brain/claude/link"),
                        ("delete", "/api/brain/claude/link"),
                        ("post", "/api/brain/claude/link/code"),
                        ("delete", "/api/brain/claude/account")):
        assert getattr(bare, method)(url).status_code == 401


def test_today_says_link_again_when_the_account_is_logged_out(client, monkeypatch):
    set_brain_settings({"auth": "account"}, client.dir / "harvey.yaml")
    monkeypatch.setattr(dash, "_active_brain",
                        lambda: BrainConfig(provider="claude", auth="account"))
    claude_link._write_note("subscription")

    def status(linked):
        return lambda auth: {"cli": {"ok": True, "version": "", "problem": ""},
                             "linked": linked, "linked_as": {}, "problem": ""}
    monkeypatch.setattr(brain_auth, "claude_status", status(False))
    brain_auth.forget()
    assert "claude-login" in [i["key"] for i in client.get("/api/today").json()["items"]]
    monkeypatch.setattr(brain_auth, "claude_status", status(True))
    brain_auth.forget()
    assert "claude-login" not in [i["key"] for i in client.get("/api/today").json()["items"]]


# ── The panel under node ──

def _claude_status(kind="", link=None, linked=None, billed=False):
    from tests.test_brain_auth import CLAUDE
    return {**CLAUDE, "auth": "account", "offered": ["terminal", "account", "api_key"],
            "folder": "data/auth/claude", "billed": billed,
            "claude_account": {"kind": kind, "link": link or {"status": "idle"}},
            "linked": linked, "linked_as": {"method": "claude.ai", "plan": "max"} if linked else {},
            "spend": {"today_usd": 0.5, "usd_per_day": 2, "month_usd": 0.5,
                      "usd_per_month": None, "priced": True} if billed else None}


@pytest.mark.parametrize("lang, words", [
    ("en", ["Link account", "Link a Console account", "billed per token", "claude auth login"]),
    ("ro", ["Conectează contul", "Conectează un cont Console", "plătește pe token"]),
])
def test_the_panel_offers_both_claude_account_links(lang, words):
    from tests.test_brain_auth import _panel
    html = _panel(lang, _claude_status(linked=False))
    assert 'value="account" checked' in html
    assert 'id="model-link"' in html and 'id="model-link-console"' in html
    assert 'id="model-unlink-account"' not in html
    for w in words:
        assert w in html, w


@pytest.mark.parametrize("lang, words", [
    ("en", ["1. Open the sign-in page:", "paste the code the page gives you", "Submit",
            "Waiting for the code", "15 minutes", "Cancel"]),
    ("ro", ["1. Deschide pagina de autentificare:", "lipește aici codul", "Trimite",
            "Se așteaptă codul", "Anulează"]),
])
def test_a_link_waiting_for_its_code_shows_the_page_and_a_code_box(lang, words):
    from tests.test_brain_auth import _panel
    link = {"status": "waiting_code", "account": "subscription", "expires_in": 900,
            "url": "https://claude.com/cai/oauth/authorize?code=true&state=fictional"}
    html = _panel(lang, _claude_status(link=link))
    assert 'href="https://claude.com/cai/oauth/authorize?code=true&amp;state=fictional"' in html
    assert 'id="claude-link-code"' in html and 'id="model-link"' not in html
    for w in words:
        assert w in html, w
    again = _panel("en", _claude_status(link={**link, "problem": "invalid_code"}))
    assert "The code isn't complete" in again
    checking = _panel("en", _claude_status(link={**link, "status": "pending"}))
    assert "Checking the code" in checking and "submitClaudeCode()\" disabled" in checking


def test_a_linked_console_account_shows_its_caps_and_unlink():
    from tests.test_brain_auth import _panel
    html = _panel("en", _claude_status(kind="console", linked=True, billed=True))
    assert "Anthropic Console account linked in Harvey (billed per token)" in html
    assert 'id="model-usd-day"' in html and 'id="model-key"' not in html
    assert "today $0.50 of $2.00" in html or "$0.50" in html
    assert 'id="model-unlink-account"' in html and 'id="model-link"' not in html
    ended = _panel("en", _claude_status(kind="subscription", linked=False))
    assert "link the account again" in ended and "Link the account again" in ended
    subscription = _panel("en", _claude_status(kind="subscription", linked=True))
    assert 'id="model-usd-day"' not in subscription
