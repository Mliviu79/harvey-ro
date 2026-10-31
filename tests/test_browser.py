"""Reading websites: HTTP first, a browser over CDP only when HTTP isn't
enough, the fallback browser only when the first one is blocked, and the
public-website boundary on every rendered navigation. No network: the
browsers are fakes and every URL is a literal public or private address."""

import asyncio

import httpx
import pytest

from harvey import browser
from harvey.browser import FALLBACK, PRIMARY, PageReader, Renderer, render_reason

PUBLIC = "http://93.184.215.14"         # a literal public address: no DNS
OTHER_PUBLIC = "http://93.184.215.15"
SHELL = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'
STATIC = "<html><body><main>" + ("Cabinet stomatologic, programari telefonice. " * 40) + "</main></body></html>"
RENDERED = "<html><body><h1>Clinica Exemplu</h1><a href='tel:+40268000000'>0268 000 000</a>" + ("text " * 300) + "</body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(browser, "SETTLE_MIN_S", 0)
    monkeypatch.setattr(browser, "SETTLE_MAX_S", 0.05)
    monkeypatch.setattr(browser, "SETTLE_POLL_S", 0.01)
    browser._warned.clear()


# ── fakes ──

class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    def __init__(self, site):
        self.site = site
        self.url = ""

    async def goto(self, url, wait_until=None, timeout=None):
        self.site.visits.append(url)
        if self.site.delay:
            await asyncio.sleep(self.site.delay)
        outcome = self.site.outcome
        if isinstance(outcome, Exception):
            raise outcome
        status, html, landed = outcome
        self.url = landed or url
        self._html = html
        return FakeResponse(status)

    async def evaluate(self, js):
        return "same"

    async def content(self):
        return self._html


class FakeContext:
    def __init__(self, site, kwargs):
        self.site, self.kwargs = site, kwargs
        self.closed = False
        self.route_handler = None

    async def route(self, pattern, handler):
        self.route_handler = handler

    async def route_web_socket(self, pattern, handler):
        self.ws_handler = handler

    async def new_page(self):
        return FakePage(self.site)

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, site, version="145.0.0.0"):
        self.site, self.version = site, version
        self.contexts = []

    def is_connected(self):
        return True

    async def new_context(self, **kwargs):
        ctx = FakeContext(self.site, kwargs)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        pass


class Site:
    """What one browser endpoint does with every page: (status, html,
    landed_url) or an exception."""
    def __init__(self, outcome, delay=0.0):
        self.outcome = outcome
        self.visits = []
        self.delay = delay


class FakePlaywright:
    def __init__(self, sites):
        self.sites = sites          # {cdp_url: Site or Exception}
        self.connects = []
        self.chromium = self

    async def connect_over_cdp(self, url, headers=None, timeout=None):
        self.connects.append((url, headers))
        site = self.sites[url]
        if isinstance(site, Exception):
            raise site
        return FakeBrowser(site)

    async def stop(self):
        pass


def renderer_with(sites, endpoints):
    r = Renderer(endpoints)
    pw = FakePlaywright(sites)

    async def _pw():
        return pw
    r._playwright = _pw
    return r, pw


TWO = [(PRIMARY, "http://obscura:9230", "tok" * 11), (FALLBACK, "http://chromium:9231", "")]


# ── when to render ──

def test_render_reason_reads_js_shells_and_blocks_only():
    assert render_reason(PUBLIC + "/", html=SHELL, status=200) == "js"
    assert render_reason(PUBLIC + "/", html=STATIC, status=200) == ""
    assert render_reason(PUBLIC + "/", status=403) == "blocked"
    assert render_reason(PUBLIC + "/", html=CHALLENGE, status=200, blocked=True) == "blocked"
    # A transport failure is never rendered (a dead site would cost a
    # browser try per URL variant), nor are robots.txt and sitemaps.
    assert render_reason(PUBLIC + "/", status=None) == ""
    assert render_reason(PUBLIC + "/sitemap.xml", html="<urlset/>", status=200) == ""
    assert render_reason(PUBLIC + "/robots.txt", status=403) == ""
    # An app root with real server-rendered text around it is still static.
    assert render_reason(PUBLIC + "/", html=STATIC.replace("<main>", '<div id="root"></div><main>'),
                         status=200) == ""


def test_noscript_and_empty_root_mark_a_short_page_as_js():
    page = ("<html><body><noscript>Va rugam activati JavaScript pentru acest site.</noscript>"
            + "<p>" + "Meniu " * 60 + "</p></body></html>")
    assert browser.looks_js_only(page)


def test_endpoints_from_env():
    assert browser.endpoints_from_env({}) == []
    # docker-compose.yml points Harvey at the obscura service by default;
    # BROWSER_CDP_URL overrides it and "off" turns the browser off.
    default = {"HARVEY_BROWSER_CDP_DEFAULT": "http://127.0.0.1:9230", "BROWSER_CDP_TOKEN": "t"}
    assert browser.endpoints_from_env(default) == [(PRIMARY, "http://127.0.0.1:9230", "t")]
    assert browser.endpoints_from_env({**default, "BROWSER_CDP_URL": ""}) == [
        (PRIMARY, "http://127.0.0.1:9230", "t")]
    assert browser.endpoints_from_env({**default, "BROWSER_CDP_URL": "off"}) == []
    assert browser.endpoints_from_env({**default, "BROWSER_CDP_URL": "http://10.1.1.1:9230"})[0][1] \
        == "http://10.1.1.1:9230"
    env = {"BROWSER_CDP_URL": "http://127.0.0.1:9230", "BROWSER_CDP_TOKEN": "t",
           "BROWSER_CDP_FALLBACK_URL": "http://127.0.0.1:9231"}
    assert browser.endpoints_from_env(env) == [
        (PRIMARY, "http://127.0.0.1:9230", "t"), (FALLBACK, "http://127.0.0.1:9231", "")]


# ── no browser ──

@pytest.mark.asyncio
async def test_no_browser_configured_keeps_http_and_never_starts_playwright(caplog):
    reader = PageReader(Renderer([]))
    assert await reader.after_http(PUBLIC + "/", html=SHELL, status=200) is None
    assert await reader.after_http(PUBLIC + "/b", html=SHELL, status=200) is None
    assert reader.renderer._pw is None
    assert sum("no browser is set" in r.message for r in caplog.records) == 1
    assert not reader.browser_first(PUBLIC + "/")


@pytest.mark.asyncio
async def test_unreachable_browser_falls_back_to_http_and_waits_before_retrying(caplog):
    r, pw = renderer_with({"http://obscura:9230": ConnectionRefusedError("down")},
                          [(PRIMARY, "http://obscura:9230", "")])
    reader = PageReader(r)
    assert await reader.after_http(PUBLIC + "/", html=SHELL, status=200) is None
    assert await reader.after_http(PUBLIC + "/b", html=SHELL, status=200) is None
    assert len(pw.connects) == 1          # not again within RECONNECT_AFTER_S
    assert sum("can't reach browser" in r.message for r in caplog.records) == 1


# ── which browser ──

@pytest.mark.asyncio
async def test_js_shell_is_rendered_by_the_primary_and_the_fallback_is_untouched():
    primary, fallback = Site((200, RENDERED, None)), Site((200, RENDERED, None))
    r, pw = renderer_with({"http://obscura:9230": primary, "http://chromium:9231": fallback}, TWO)
    reader = PageReader(r)
    page = await reader.after_http(PUBLIC + "/", html=SHELL, status=200)
    assert page.text == RENDERED and page.method == PRIMARY
    assert reader.method(PUBLIC + "/") == PRIMARY and reader.method(PUBLIC + "/x") == "http"
    assert fallback.visits == []
    # The token goes to the primary only; a context per page, closed after.
    assert pw.connects[0][1] == {"Authorization": "Bearer " + "tok" * 11}
    ctx = r.endpoints[0].browser.contexts[0]
    assert ctx.closed and ctx.kwargs["locale"] == "ro-RO"
    assert "Chrome/145" in ctx.kwargs["user_agent"] and "Headless" not in ctx.kwargs["user_agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_outcome", [
    (403, "<html><body>Forbidden</body></html>", None),
    (200, CHALLENGE, None),
    RuntimeError("Page.goto: Network error: connection reset by peer"),
    RuntimeError("Page.goto: net::ERR_SSL_PROTOCOL_ERROR"),
])
async def test_a_blocked_primary_hands_over_to_the_fallback(primary_outcome):
    primary, fallback = Site(primary_outcome), Site((200, RENDERED, None))
    r, _ = renderer_with({"http://obscura:9230": primary, "http://chromium:9231": fallback}, TWO)
    page = await r.render(PUBLIC + "/")
    assert page is not None and page.method == FALLBACK
    assert fallback.visits == [PUBLIC + "/"]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_outcome", [
    RuntimeError("Page.goto: Timeout 20000ms exceeded."),
    RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED"),
    (404, "<html>Not found</html>", None),
])
async def test_failures_another_browser_cant_fix_dont_use_the_fallback(primary_outcome):
    primary, fallback = Site(primary_outcome), Site((200, RENDERED, None))
    r, _ = renderer_with({"http://obscura:9230": primary, "http://chromium:9231": fallback}, TWO)
    assert await r.render(PUBLIC + "/") is None
    assert fallback.visits == []


@pytest.mark.asyncio
async def test_unreachable_primary_uses_the_fallback():
    fallback = Site((200, RENDERED, None))
    r, _ = renderer_with({"http://obscura:9230": OSError("refused"),
                          "http://chromium:9231": fallback}, TWO)
    page = await r.render(PUBLIC + "/")
    assert page.method == FALLBACK


@pytest.mark.asyncio
async def test_a_host_blocked_over_http_goes_straight_to_the_browser_after():
    site = Site((200, RENDERED, None))
    r, _ = renderer_with({"http://obscura:9230": site}, TWO[:1])
    reader = PageReader(r)
    assert await reader.after_http(PUBLIC + "/", status=403) is not None
    assert reader.browser_first(PUBLIC + "/contact")
    assert not reader.browser_first(PUBLIC + "/sitemap.xml")
    assert not reader.browser_first(OTHER_PUBLIC + "/")


@pytest.mark.asyncio
async def test_huge_pages_are_capped(monkeypatch):
    monkeypatch.setattr(browser, "MAX_HTML_CHARS", 1000)
    r, _ = renderer_with({"http://obscura:9230": Site((200, "x" * 5000, None))}, TWO[:1])
    page = await r.render(PUBLIC + "/")
    assert len(page.text) == 1000


@pytest.mark.asyncio
async def test_a_host_every_browser_refused_is_not_rendered_again_this_run():
    primary, fallback = Site((403, "<html>Forbidden</html>", None)), Site((403, "<html>No</html>", None))
    r, _ = renderer_with({"http://obscura:9230": primary, "http://chromium:9231": fallback}, TWO)
    assert await r.render(PUBLIC + "/") is None
    assert await r.render(PUBLIC + "/contact") is None
    assert primary.visits == fallback.visits == [PUBLIC + "/"]
    # A host where only one page is missing stays renderable.
    ok = Site((404, "<html>Not found</html>", None))
    r2, _ = renderer_with({"http://obscura:9230": ok}, TWO[:1])
    assert await r2.render(PUBLIC + "/a") is None
    assert await r2.render(PUBLIC + "/b") is None
    assert ok.visits == [PUBLIC + "/a", PUBLIC + "/b"]


@pytest.mark.asyncio
async def test_waiting_for_a_page_slot_is_not_charged_to_the_page(monkeypatch):
    monkeypatch.setattr(browser, "PAGES_PER_BROWSER", 1)
    monkeypatch.setattr(browser, "RENDER_BUDGET_S", 0.3)
    browser._SLOTS.clear()
    site = Site((200, RENDERED, None), delay=0.2)
    # Two readers share the slot.
    r1, _ = renderer_with({"http://obscura:9230": site}, TWO[:1])
    r2, _ = renderer_with({"http://obscura:9230": site}, TWO[:1])
    pages = await asyncio.gather(*(r.render(f"{PUBLIC}/{i}") for i, r in enumerate((r1, r2, r1))))
    assert all(p is not None for p in pages)       # 0.6 s in all, 0.2 s each


# ── the public-website boundary ──

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9230/json/version", "http://10.0.0.5/", "http://169.254.169.254/latest/",
    "http://[::1]/", "http://localhost/", "file:///etc/passwd", "http://user:pw@93.184.215.14/",
])
async def test_private_targets_never_reach_a_browser(url):
    site = Site((200, RENDERED, None))
    r, pw = renderer_with({"http://obscura:9230": site}, TWO[:1])
    assert await r.render(url) is None
    assert pw.connects == [] and site.visits == []


@pytest.mark.asyncio
async def test_a_page_that_redirects_to_a_private_address_is_dropped():
    site = Site((200, RENDERED, "http://192.168.1.1/admin"))
    fallback = Site((200, RENDERED, None))
    r, _ = renderer_with({"http://obscura:9230": site, "http://chromium:9231": fallback}, TWO)
    assert await r.render(PUBLIC + "/") is None
    assert fallback.visits == []          # not retried elsewhere either


class FakeRoute:
    def __init__(self, url):
        self.request = type("Req", (), {"url": url})()
        self.verdict = None

    async def continue_(self):
        self.verdict = "continue"

    async def abort(self):
        self.verdict = "abort"


@pytest.mark.asyncio
@pytest.mark.parametrize("url,verdict", [
    (PUBLIC + "/app.js", "continue"),
    ("data:image/png;base64,AAAA", "continue"),
    ("http://10.0.0.1/internal", "abort"),
    ("http://169.254.169.254/latest/meta-data", "abort"),
    ("http://127.0.0.1:5555/api", "abort"),
    ("ws://93.184.215.14/socket", "abort"),
])
async def test_sub_requests_to_private_addresses_are_aborted(url, verdict):
    route = FakeRoute(url)
    await Renderer([])._guard(route)
    assert route.verdict == verdict


class FakeWebSocketRoute:
    def __init__(self, url):
        self.url, self.verdict = url, None

    def connect_to_server(self):
        self.verdict = "connect"

    async def close(self, **kwargs):
        self.verdict = "close"


@pytest.mark.asyncio
@pytest.mark.parametrize("url,verdict", [
    ("wss://93.184.215.14/socket", "connect"),
    ("ws://10.0.0.1:18999/x", "close"),
    ("ws://127.0.0.1:5555/api", "close"),
    ("ws://localhost/x", "close"),
])
async def test_websockets_to_private_addresses_are_closed(url, verdict):
    ws = FakeWebSocketRoute(url)
    await Renderer([])._ws_guard(ws)
    assert ws.verdict == verdict


@pytest.mark.asyncio
async def test_every_rendered_page_routes_websockets_through_the_guard():
    r, _ = renderer_with({"http://obscura:9230": Site((200, RENDERED, None))}, TWO[:1])
    await r.render(PUBLIC + "/")
    assert r.endpoints[0].browser.contexts[0].ws_handler == r._ws_guard


# ── the website readers use it ──

class FakeHTTP:
    def __init__(self, responses):
        self.responses = responses

    async def get(self, url):
        status, html = self.responses[url]
        return httpx.Response(status, text=html, headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))

    async def aclose(self):
        pass


class FakeReader(PageReader):
    def __init__(self, html=RENDERED, method=PRIMARY):
        super().__init__(Renderer([]))
        self.html, self.m, self.asked = html, method, []

    async def after_http(self, url, *, html="", status=None, blocked=False):
        reason = render_reason(url, html=html, status=status, blocked=blocked)
        self.asked.append((url, reason))
        if not reason:
            return None
        if reason == "blocked":
            self.browser_hosts.add(self._host(url))
        self.methods[url] = self.m
        return browser.RenderedPage(url=url, text=self.html, status_code=200, method=self.m)


@pytest.mark.asyncio
async def test_profile_collector_renders_js_shells_and_records_the_method():
    from harvey.collectors.profile import ProfileCollector
    c = ProfileCollector(confirmed={"NO_ONLINE_BOOKING"})
    c._client = FakeHTTP({"https://clinica.test": (200, SHELL)})
    c._pages = FakeReader()
    obs = await c.profile_company("c1", "clinica.test")
    assert obs and obs[0]["collector"] == "profile:browser"
    assert c._pages.asked == [("https://clinica.test", "js")]


@pytest.mark.asyncio
async def test_profile_collector_keeps_static_pages_on_http():
    from harvey.collectors.profile import ProfileCollector
    c = ProfileCollector(confirmed={"NO_ONLINE_BOOKING"})
    c._client = FakeHTTP({"https://clinica.test": (200, STATIC)})
    c._pages = FakeReader()
    obs = await c.profile_company("c1", "clinica.test")
    assert obs[0]["collector"] == "profile"


@pytest.mark.asyncio
async def test_profile_collector_renders_a_challenged_site_and_clears_the_challenge():
    from harvey.collectors.profile import ProfileCollector
    c = ProfileCollector(confirmed=set())
    c._client = FakeHTTP({"https://clinica.test": (403, CHALLENGE)})
    c._pages = FakeReader()
    assert await c._get("https://clinica.test") == RENDERED
    assert "clinica.test" not in c.challenged


