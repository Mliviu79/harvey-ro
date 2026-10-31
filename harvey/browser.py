"""A real browser for the pages plain HTTP can't read, reached over CDP.

Harvey reads websites with plain HTTP first. A page that is only a
JavaScript shell (almost no visible text, an empty app root), or one whose
HTTP answer was a block, is rendered by the browser at BROWSER_CDP_URL
(Obscura by default). When that browser is blocked too (a 403, a bot
challenge, a TLS reset), the browser at BROWSER_CDP_FALLBACK_URL (Chromium)
gets one try. With neither set, or neither reachable, Harvey stays on HTTP
and says so once in the log.

The browsers run as separate services (docker-compose.yml); Harvey only
connects to them with Playwright's connect_over_cdp. Every navigation is
checked against the same public-website boundary as Harvey's HTTP client,
before the request and again for where it landed; sub-requests to
non-public addresses are aborted where the browser honours that.
"""

import asyncio
import logging
import os
import re
import time
import weakref
from dataclasses import dataclass
from urllib.parse import urlparse

from harvey.public_http import UnsafeURLError, check_public_url

logger = logging.getLogger("harvey.browser")

PRIMARY, FALLBACK = "browser", "browser_fallback"
HTTP = "http"

CONNECT_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 20_000
RENDER_BUDGET_S = 35.0          # one page, navigation and settle included
SETTLE_MIN_S, SETTLE_MAX_S, SETTLE_POLL_S = 0.6, 4.0, 0.3
MAX_HTML_CHARS = 2_000_000
# Pages one browser renders at once, across every reader in the process
# (each site reader has one). Obscura runs each page's
# JavaScript on its own thread and slows down well before Chromium does.
PAGES_PER_BROWSER = 3
# Set by docker-compose.yml for the harvey service: the obscura service's
# address, used when BROWSER_CDP_URL is blank. "off" turns the browser off.
DEFAULT_URL_ENV = "HARVEY_BROWSER_CDP_DEFAULT"
_OFF = ("off", "none", "no", "0", "false")
# After a failed connection, don't try that browser again for a while.
RECONNECT_AFTER_S = 300.0
# Under this much visible text a page is probably a JavaScript shell. The
# sites that rendered to real content had 1-123 characters over HTTP; real
# static pages have thousands.
MIN_VISIBLE_TEXT = 200
BLOCK_STATUSES = (403, 429, 503)

_EMPTY_ROOT = re.compile(
    r"""(?is)<(div|main|section)\b[^>]*\bid\s*=\s*["'](root|app|__next|__nuxt|___gatsby)["'][^>]*>\s*</\1>"""
    r"|<app-root\b[^>]*>\s*</app-root>")
_NOSCRIPT_JS = re.compile(
    r"(?is)<noscript\b[^>]*>[^<]{0,300}?(enable|activa\w*|activeaz\w*|turn on)\b[^<]{0,80}javascript")
_SKIP_PATH = re.compile(r"(?i)\.(txt|xml|json|pdf|jpe?g|png|gif|webp|svg|css|js|ico|zip)$")

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def _visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def looks_js_only(html: str) -> bool:
    """A page whose content arrives by JavaScript: very little visible text,
    an empty app root, or a <noscript> asking for JavaScript."""
    if not html:
        return False
    text = len(_visible_text(html))
    if text < MIN_VISIBLE_TEXT:
        return True
    return text < 1000 and bool(_EMPTY_ROOT.search(html) or _NOSCRIPT_JS.search(html))


def render_reason(url: str, *, html: str = "", status: int | None = None,
                  blocked: bool = False) -> str:
    """Why plain HTTP's answer for ``url`` should be rendered: "blocked",
    "js", or "" to keep the HTTP answer. A transport failure (status None,
    not blocked) is never rendered: a dead site would cost a browser try per
    URL variant."""
    if _SKIP_PATH.search(urlparse(url).path or ""):
        return ""
    if blocked or status in BLOCK_STATUSES:
        return "blocked"
    if status == 200 and looks_js_only(html):
        return "js"
    return ""


@dataclass
class RenderedPage:
    """A rendered page, shaped like the parts of an httpx.Response the
    website readers use (``.text``, ``.url``, ``.status_code``)."""
    url: str
    text: str
    status_code: int
    method: str          # PRIMARY or FALLBACK

    @property
    def html(self) -> str:
        return self.text


class BrowserBlocked(Exception):
    """The site refused this browser (403, challenge, TLS reset)."""


class BrowserUnavailable(Exception):
    """No browser at that endpoint right now."""


def _is_block_error(message: str) -> bool:
    """A navigation error that another browser might get past."""
    low = message.lower()
    if any(k in low for k in ("timeout", "name_not_resolved", "dns", "private/internal",
                               "err_address_unreachable", "err_connection_refused")):
        return False
    return any(k in low for k in ("tls", "ssl", "handshake", "reset", "connection closed",
                                  "err_connection", "err_http2", "err_empty_response",
                                  "403", "forbidden", "network error", "err_failed"))


# Page slots per browser URL, shared by every Renderer on the same event
# loop (a semaphore can't be shared across loops).
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" = \
    weakref.WeakKeyDictionary()


def _page_slots(url: str) -> asyncio.Semaphore:
    per_loop = _SLOTS.setdefault(asyncio.get_running_loop(), {})
    if url not in per_loop:
        per_loop[url] = asyncio.Semaphore(PAGES_PER_BROWSER)
    return per_loop[url]


class _Endpoint:
    def __init__(self, name: str, url: str, token: str = ""):
        self.name, self.url, self.token = name, url, token
        self.browser = None
        self.user_agent = ""
        self.down_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def pages(self) -> asyncio.Semaphore:
        return _page_slots(self.url)

    async def get(self, playwright):
        if self.browser is not None and self.browser.is_connected():
            return self.browser
        if time.monotonic() < self.down_until:
            raise BrowserUnavailable(self.name)
        async with self._lock:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
            try:
                self.browser = await playwright.chromium.connect_over_cdp(
                    self.url, headers=headers, timeout=CONNECT_TIMEOUT_MS)
            except Exception as e:
                self.browser = None
                self.down_until = time.monotonic() + RECONNECT_AFTER_S
                _warn_once(f"connect:{self.name}",
                           f"browser: can't reach {self.name} at {self.url} "
                           f"({type(e).__name__}); reading pages over plain HTTP. "
                           f"Check the browser service is running"
                           + (" and BROWSER_CDP_TOKEN in .env is the one it "
                              "started with." if self.name == PRIMARY else "."))
                raise BrowserUnavailable(self.name) from e
            major = (self.browser.version or "").split(".")[0]
            if major.isdigit():
                # The engine's own version, never "HeadlessChrome".
                self.user_agent = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                   f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
            logger.info(f"browser: connected to {self.name} ({self.browser.version})")
            return self.browser

    async def close(self):
        if self.browser is not None:
            try:
                await self.browser.close()      # disconnects; the service keeps running
            except Exception:
                pass
            self.browser = None


def primary_url(env=None) -> str:
    """The primary browser's CDP address: BROWSER_CDP_URL, else the
    compose default (DEFAULT_URL_ENV); "" when neither is set or
    BROWSER_CDP_URL is "off"."""
    env = os.environ if env is None else env
    value = (env.get("BROWSER_CDP_URL") or "").strip()
    if value.lower() in _OFF:
        return ""
    return value or (env.get(DEFAULT_URL_ENV) or "").strip()


def endpoints_from_env(env=None) -> list[tuple[str, str, str]]:
    """[(name, cdp_url, token)] for the configured browsers, primary first."""
    env = os.environ if env is None else env
    primary = primary_url(env)
    fallback = (env.get("BROWSER_CDP_FALLBACK_URL") or "").strip()
    token = (env.get("BROWSER_CDP_TOKEN") or "").strip()
    out = []
    if primary:
        out.append((PRIMARY, primary, token))
    if fallback and fallback != primary:
        out.append((FALLBACK, fallback, ""))
    return out


class Renderer:
    """Renders public pages through the configured browsers. One per run;
    ``aclose()`` disconnects (the browser services keep running)."""

    def __init__(self, endpoints: list[tuple[str, str, str]] | None = None):
        cfg = endpoints_from_env() if endpoints is None else endpoints
        self.endpoints = [_Endpoint(*e) for e in cfg]
        self._pw = None
        self._pw_lock = asyncio.Lock()
        self._dns: dict[str, bool] = {}
        # Hosts every browser refused or timed out on this run: their other
        # pages and URL variants aren't rendered again.
        self.refused_hosts: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.endpoints)

    async def _playwright(self):
        async with self._pw_lock:
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            return self._pw

    async def aclose(self):
        for ep in self.endpoints:
            await ep.close()
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def _public(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if host in self._dns:
            return self._dns[host]
        try:
            await check_public_url(url)
            ok = True
        except UnsafeURLError:
            ok = False
        self._dns[host] = ok
        return ok

    @staticmethod
    def _host(url: str) -> str:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")

    async def render(self, url: str) -> RenderedPage | None:
        """The rendered page, or None when no browser could read it. Tries
        the primary browser, then the fallback when the primary is blocked
        or unavailable. Never raises."""
        if not self.endpoints:
            return None
        host = self._host(url)
        if host in self.refused_hosts:
            return None
        try:
            await check_public_url(url)
        except UnsafeURLError as e:
            logger.debug(f"browser: refused {url[:80]}: {e}")
            return None
        tried = 0
        for ep in self.endpoints:
            try:
                browser = await ep.get(await self._playwright())
                # Waiting for a free page slot isn't part of the page's budget.
                async with ep.pages:
                    tried += 1
                    return await asyncio.wait_for(self._render_with(ep, browser, url),
                                                  RENDER_BUDGET_S)
            except BrowserUnavailable:
                continue
            except BrowserBlocked as e:
                logger.debug(f"browser: {ep.name} blocked on {url[:80]}: {e}")
                continue
            except UnsafeURLError as e:
                logger.info(f"browser: {url[:80]} left the public web ({e}); page dropped")
                return None
            except asyncio.TimeoutError:
                logger.debug(f"browser: {ep.name} timed out on {url[:80]}")
                self.refused_hosts.add(host)
                return None
            except Exception as e:
                logger.debug(f"browser: {ep.name} failed on {url[:80]}: {type(e).__name__}: {e}")
                return None
        if tried:
            # Every browser that answered refused this host.
            self.refused_hosts.add(host)
        return None

    async def _guard(self, route):
        """Abort sub-requests to non-public hosts (Chromium honours this;
        Obscura has its own private-network block)."""
        req_url = route.request.url
        scheme = urlparse(req_url).scheme
        try:
            if scheme in ("data", "blob", "about"):
                await route.continue_()
            elif scheme in ("http", "https") and await self._public(req_url):
                await route.continue_()
            else:
                await route.abort()
        except Exception:
            pass

    async def _ws_guard(self, ws):
        """Close WebSockets to non-public hosts; context.route doesn't see
        them."""
        target = re.sub(r"^ws", "http", ws.url, flags=re.I)
        try:
            if await self._public(target):
                ws.connect_to_server()
            else:
                await ws.close()
        except Exception:
            pass

    async def _render_with(self, ep: _Endpoint, browser, url: str) -> RenderedPage:
        from harvey.collectors.profile import looks_like_challenge
        kwargs = {"locale": "ro-RO", "accept_downloads": False,
                  "extra_http_headers": {"Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"}}
        if ep.user_agent:
            kwargs["user_agent"] = ep.user_agent
        context = await browser.new_context(**kwargs)
        try:
            await context.route("**/*", self._guard)
            try:
                await context.route_web_socket(re.compile(".*"), self._ws_guard)
            except Exception as e:
                # Obscura blocks private addresses itself; the Chromium
                # service also sits behind the egress proxy.
                logger.debug(f"browser: {ep.name} can't route WebSockets: {e}")
            page = await context.new_page()
            try:
                resp = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            except Exception as e:
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                if _is_block_error(msg):
                    raise BrowserBlocked(msg[:160]) from e
                raise
            status = resp.status if resp is not None else 200
            await _settle(page)
            final = page.url or url
            # Where the navigation ended up must be public too.
            await check_public_url(final)
            html = (await page.content())[:MAX_HTML_CHARS]
            if status in BLOCK_STATUSES or looks_like_challenge(html, status):
                raise BrowserBlocked(f"status {status}")
            if status != 200:
                raise RuntimeError(f"status {status}")
            return RenderedPage(url=final, text=html, status_code=status, method=ep.name)
        finally:
            try:
                await context.close()
            except Exception:
                pass


_SETTLE_JS = ("() => (document.getElementsByTagName('*').length) + ':' + "
              "(document.body ? document.body.textContent.length : 0)")


async def _settle(page) -> None:
    """Wait until the DOM stops changing: two equal readings in a row after
    a short minimum, at most SETTLE_MAX_S. Obscura's "networkidle" fires
    before late XHRs finish, and feeds never go idle, so neither is used."""
    await asyncio.sleep(SETTLE_MIN_S)
    deadline = time.monotonic() + SETTLE_MAX_S - SETTLE_MIN_S
    last, same = None, 0
    while time.monotonic() < deadline:
        try:
            now = await page.evaluate(_SETTLE_JS)
        except Exception:
            return
        same = same + 1 if now == last else 0
        if same >= 2:
            return
        last = now
        await asyncio.sleep(SETTLE_POLL_S)


class PageReader:
    """HTTP first, a browser when HTTP isn't enough; one per collector run.

    Callers fetch over HTTP as before, then ask ``after_http`` whether the
    answer needs rendering. A host whose HTTP answer was a block goes
    straight to the browser for its other pages (``browser_first``).
    ``methods`` maps each URL to what produced it, for provenance.
    """

    def __init__(self, renderer: Renderer | None = None):
        self.renderer = renderer if renderer is not None else Renderer()
        self.browser_hosts: set[str] = set()
        self.methods: dict[str, str] = {}

    @staticmethod
    def _host(url: str) -> str:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")

    def browser_first(self, url: str) -> bool:
        return (self.renderer.configured and self._host(url) in self.browser_hosts
                and not _SKIP_PATH.search(urlparse(url).path or ""))

    async def after_http(self, url: str, *, html: str = "", status: int | None = None,
                         blocked: bool = False) -> RenderedPage | None:
        """A rendered page to use instead of HTTP's answer, or None to keep it."""
        reason = render_reason(url, html=html, status=status, blocked=blocked)
        if not reason:
            return None
        if not self.renderer.configured:
            _warn_once("unconfigured",
                       "browser: a page needs JavaScript or blocked plain HTTP, and no "
                       "browser is set (BROWSER_CDP_URL); reading it over HTTP only.")
            return None
        page = await self.renderer.render(url)
        if page is not None:
            if reason == "blocked":
                self.browser_hosts.add(self._host(url))
            self.methods[url] = self.methods[page.url] = page.method
            logger.info(f"browser: {url[:80]} read with {page.method} ({reason})")
        return page

    async def render(self, url: str) -> RenderedPage | None:
        page = await self.renderer.render(url)
        if page is not None:
            self.methods[url] = self.methods[page.url] = page.method
        return page

    def method(self, url: str) -> str:
        return self.methods.get(url, HTTP)

    async def aclose(self):
        await self.renderer.aclose()
