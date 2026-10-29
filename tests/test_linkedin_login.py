"""LinkedIn login: today's form (no #username/#password, a type=button
"Sign in"), a clear stop when the form isn't there, checkpoints that put
automatic logins on hold, and never a second attempt. The page is a fake
backed by a fictional fixture; nothing reaches LinkedIn."""

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from harvey.integrations import linkedin
from harvey.integrations.linkedin import LinkedInAutomation, browser_endpoint, classify_url

FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_login.html").read_text()
OLD_FORM_GONE = "<html><body><h1>Request refused</h1></body></html>"


def _hidden(el) -> bool:
    for node in [el, *el.parents]:
        if getattr(node, "attrs", None) is None:
            continue
        if node.has_attr("hidden") or "display:none" in (node.get("style") or "").replace(" ", ""):
            return True
    return False


class FakeLocator:
    def __init__(self, page, elements):
        self.page, self.elements = page, elements

    @property
    def first(self):
        return FakeLocator(self.page, self.elements[:1])

    async def count(self):
        return len(self.elements)

    async def fill(self, value):
        if not self.elements:
            raise TimeoutError("no element")
        self.page.filled[self.elements[0].get("name")] = value

    async def click(self):
        if not self.elements:
            raise TimeoutError("no element")
        self.page.clicked.append(self.elements[0].get("id"))
        self.page.url = self.page.after_click


class FakePage:
    def __init__(self, html, status=200, after_click="https://www.linkedin.com/feed/",
                 start_url=None):
        self.soup = BeautifulSoup(html, "html.parser")
        self.status, self.after_click, self.start_url = status, after_click, start_url
        self.url = ""
        self.gotos, self.filled, self.clicked = [], {}, []

    def _select(self, selector):
        visible = selector.endswith(":visible")
        found = self.soup.select(selector.removesuffix(":visible"))
        return [e for e in found if not (visible and _hidden(e))]

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = self.start_url or url
        return type("Resp", (), {"status": self.status})()

    async def wait_for_selector(self, selector, state=None, timeout=None):
        if not self._select(selector):
            raise TimeoutError(selector)

    def locator(self, selector, has_text=None):
        found = self._select(selector)
        if has_text is not None:
            found = [e for e in found if re.search(has_text, e.get_text())]
        return FakeLocator(self, found)

    async def evaluate(self, js):
        return bool(self.soup.select("[role=alert]"))


class FakeContext:
    async def cookies(self):
        return [{"name": "li_at", "value": "fixture"}]


@pytest.fixture
def li(tmp_path, monkeypatch):
    monkeypatch.setattr(linkedin, "LOGIN_HOLD_PATH", tmp_path / "linkedin_login_hold.json")
    monkeypatch.setattr(linkedin, "COOKIES_PATH", tmp_path / "linkedin_cookies.json")
    monkeypatch.setattr(linkedin, "ACTIVITY_PATH", tmp_path / "linkedin_activity.json")
    monkeypatch.setattr(linkedin, "LOGIN_POLL_S", 0.001)
    monkeypatch.setattr(linkedin, "LOGIN_WAIT_S", 0.05)

    def make(page):
        bot = LinkedInAutomation("someone@example.com", "fixture-password")
        bot.page, bot.context = page, FakeContext()

        async def no_delay(*a, **k):
            return None
        bot._random_delay = no_delay
        return bot
    return make


@pytest.mark.asyncio
async def test_current_login_form_is_filled_and_signed_in(li):
    page = FakePage(FIXTURE)
    bot = li(page)
    assert await bot.login() is True
    # The visible email field, not the hidden decoy; the exact "Sign in"
    # button, not the hidden one or "Sign in with Apple".
    assert page.filled == {"session_key": "someone@example.com",
                           "session_password": "fixture-password"}
    assert page.clicked == [":r4:"]
    assert linkedin.COOKIES_PATH.exists()


@pytest.mark.asyncio
async def test_missing_form_stops_with_a_clear_error_and_sends_nothing(li, caplog):
    page = FakePage(OLD_FORM_GONE, status=403)
    bot = li(page)
    assert await bot.login() is False
    assert page.filled == {} and page.clicked == []
    msg = " ".join(r.message for r in caplog.records)
    assert "login form not found" in msg and "BROWSER_CDP_FALLBACK_URL" in msg
    assert "fixture-password" not in msg
    # Not a checkpoint: no hold, but no second attempt from this instance.
    assert not linkedin.LOGIN_HOLD_PATH.exists()
    assert await bot.login() is False
    assert page.gotos == [linkedin.LOGIN_URL]


@pytest.mark.asyncio
async def test_checkpoint_after_submit_holds_every_later_login(li):
    page = FakePage(FIXTURE, after_click="https://www.linkedin.com/checkpoint/challenge/AQ123")
    assert await li(page).login() is False
    assert linkedin.LOGIN_HOLD_PATH.exists()
    # The next cycle's fresh instance doesn't even open the page.
    later = FakePage(FIXTURE)
    assert await li(later).login() is False
    assert later.gotos == [] and later.filled == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("after_click", [
    "https://www.linkedin.com/login",            # stayed on the form, no error shown
    "https://www.linkedin.com/in/someone/",      # landed somewhere unexpected
])
async def test_every_failed_submit_holds_later_logins(li, after_click):
    """Scout makes a new LinkedInAutomation every cycle, so anything short
    of the hold would send the credentials again next cycle."""
    refused = FIXTURE.replace("<main>", '<main><div role="alert">Wrong password.</div>')
    assert await li(FakePage(refused, after_click="https://www.linkedin.com/login")).login() is False
    assert "refused" in linkedin.LinkedInAutomation.login_hold()["reason"]
    linkedin.LOGIN_HOLD_PATH.unlink()
    assert await li(FakePage(FIXTURE, after_click=after_click)).login() is False
    assert linkedin.LOGIN_HOLD_PATH.exists()
    later = FakePage(FIXTURE)
    assert await li(later).login() is False
    assert later.gotos == [] and later.filled == {}


@pytest.mark.asyncio
async def test_saved_session_skips_the_form(li):
    page = FakePage(FIXTURE, start_url="https://www.linkedin.com/feed/")
    assert await li(page).login() is True
    assert page.filled == {}


def test_classify_url():
    assert classify_url("https://www.linkedin.com/feed/") == "feed"
    assert classify_url("https://www.linkedin.com/checkpoint/challenge/x") == "challenge"
    assert classify_url("https://www.linkedin.com/uas/login-submit") == "login"
    assert classify_url("https://www.linkedin.com/in/someone/") == "other"


def test_linkedin_prefers_the_fallback_browser():
    assert browser_endpoint({}) == ("", "")
    # The compose default for the obscura service, and "off".
    assert browser_endpoint({"HARVEY_BROWSER_CDP_DEFAULT": "http://127.0.0.1:9230",
                             "BROWSER_CDP_TOKEN": "t"}) == ("http://127.0.0.1:9230", "t")
    assert browser_endpoint({"HARVEY_BROWSER_CDP_DEFAULT": "http://127.0.0.1:9230",
                             "BROWSER_CDP_URL": "off"}) == ("", "")
    assert browser_endpoint({"BROWSER_CDP_URL": "http://127.0.0.1:9230",
                             "BROWSER_CDP_TOKEN": "t"}) == ("http://127.0.0.1:9230", "t")
    assert browser_endpoint({"BROWSER_CDP_URL": "http://127.0.0.1:9230", "BROWSER_CDP_TOKEN": "t",
                             "BROWSER_CDP_FALLBACK_URL": "http://127.0.0.1:9231"}) == (
        "http://127.0.0.1:9231", "")
