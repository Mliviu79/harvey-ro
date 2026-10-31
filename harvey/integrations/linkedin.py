"""LinkedIn browser automation via Playwright.

Safety-first: humanlike randomized delays, hard daily activity caps
(persisted across restarts), and consistent browser fingerprint to
protect the user's LinkedIn account from restriction.
"""

import asyncio
import json
import logging
import random
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger("harvey.linkedin")

from harvey.paths import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
COOKIES_PATH = DATA_DIR / "linkedin_cookies.json"
ACTIVITY_PATH = DATA_DIR / "linkedin_activity.json"
# Written when a login that sent the password didn't reach the feed (a
# checkpoint, a refusal, a stall). While it exists
# Harvey never tries to log in again on its own: repeated automatic logins
# are how accounts get locked. Delete it after signing in by hand.
LOGIN_HOLD_PATH = DATA_DIR / "linkedin_login_hold.json"

LOGIN_URL = "https://www.linkedin.com/login"
# LinkedIn's login page (React, generated ids, no <form>, a type=button
# submit). The old #username / #password / button[type=submit] are kept as
# a second choice in case an older page is served.
EMAIL_SELECTORS = ('input[type="email"]:visible', "#username")
PASSWORD_SELECTORS = ('input[type="password"]:visible', "#password")
SIGN_IN_TEXT = re.compile(r"^\s*Sign in\s*$")
LOGIN_FORM_TIMEOUT_MS = 15000
# The feed never goes network-idle, and Obscura reports idle too early:
# after submitting, poll the URL instead.
LOGIN_POLL_S = 0.5
LOGIN_WAIT_S = 30.0
_CHALLENGE_WORDS = ("checkpoint", "challenge", "captcha", "verify", "add-phone",
                    "two-step")


def classify_url(url: str) -> str:
    """"feed", "challenge", "login" or "other" for a LinkedIn URL."""
    path = urlparse(url or "").path.lower()
    if path.startswith("/feed") or path.startswith("/mynetwork"):
        return "feed"
    if any(word in path for word in _CHALLENGE_WORDS):
        return "challenge"
    if path.startswith("/login") or path.startswith("/uas/login") or path in ("", "/"):
        return "login"
    return "other"


def browser_endpoint(env=None) -> tuple[str, str]:
    """(cdp_url, token) of the browser LinkedIn should use: the fallback
    (Chromium) first, since LinkedIn refuses Obscura; then the primary;
    ("", "") to launch a local Chromium (a pip install with
    `playwright install chromium`). ``env`` is the process environment as
    core hands it over (LinkedInAutomation's ``browser_env``)."""
    from harvey.browser import primary_url
    env = env or {}
    fallback = (env.get("BROWSER_CDP_FALLBACK_URL") or "").strip()
    if fallback:
        return fallback, ""
    primary = primary_url(env)
    if primary:
        return primary, (env.get("BROWSER_CDP_TOKEN") or "").strip()
    return "", ""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Hard daily caps — conservative on purpose. LinkedIn restricts accounts
# that view too many profiles or run too many searches per day.
MAX_DAILY_SEARCH_PAGES = 20
MAX_DAILY_PROFILE_VIEWS = 40
MAX_SEARCH_PAGES_PER_QUERY = 5


class LinkedInAutomation:
    def __init__(self, email: str, password: str, browser_env=None):
        self.email = email
        self.password = password
        # Where the browser runs (BROWSER_CDP_*), from whoever opens the session.
        self.browser_env = browser_env
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._remote = False
        self._login_result: Optional[bool] = None
        self._activity = self._load_activity()

    # ── Daily activity caps ──

    @staticmethod
    def _load_activity() -> dict:
        """Load today's activity counters (persisted so restarts don't reset caps)."""
        today = date.today().isoformat()
        try:
            if ACTIVITY_PATH.exists():
                data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("date") == today:
                    return {
                        "date": today,
                        "search_pages": int(data.get("search_pages", 0)),
                        "profile_views": int(data.get("profile_views", 0)),
                    }
        except (ValueError, OSError) as e:
            logger.debug(f"Could not load LinkedIn activity log: {e}")
        return {"date": today, "search_pages": 0, "profile_views": 0}

    def _save_activity(self):
        try:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACTIVITY_PATH.write_text(json.dumps(self._activity), encoding="utf-8")
        except OSError as e:
            logger.debug(f"Could not save LinkedIn activity log: {e}")

    def _record(self, kind: str):
        # Roll over if the date changed mid-run
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        self._activity[kind] = self._activity.get(kind, 0) + 1
        self._save_activity()

    def _cap_reached(self, kind: str, cap: int) -> bool:
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        if self._activity.get(kind, 0) >= cap:
            logger.warning(
                f"LinkedIn daily cap reached for {kind} "
                f"({cap}/day). Skipping to protect the account."
            )
            return True
        return False

    # ── Humanlike behavior ──

    async def _random_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Human-like random delay between actions, with occasional long pauses."""
        delay = random.uniform(min_s, max_s)
        # ~10% of the time, take a noticeably longer "reading" pause
        if random.random() < 0.10:
            delay += random.uniform(4.0, 10.0)
        await asyncio.sleep(delay)

    async def _humanlike_scroll(self):
        """Scroll the page in a few small increments like a human reading."""
        if not self.page:
            return
        try:
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(250, 700))
                await asyncio.sleep(random.uniform(0.4, 1.4))
        except Exception as e:
            logger.debug(f"Scroll simulation failed: {e}")

    async def _wait_settled(self, timeout: int = 15000):
        """Wait for network idle without crashing on busy pages."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            # Feed-like pages never go fully idle; that's fine.
            pass

    # ── Lifecycle ──

    async def start(self):
        """Launch browser and set up context."""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        cdp_url, token = browser_endpoint(self.browser_env)
        if cdp_url:
            headers = {"Authorization": f"Bearer {token}"} if token else None
            self.browser = await self._pw.chromium.connect_over_cdp(
                cdp_url, headers=headers, timeout=15000)
            self._remote = True
        else:
            self.browser = await self._pw.chromium.launch(headless=True)

        # Always use the same fingerprint (viewport + UA), whether or not
        # we have saved cookies — a changing fingerprint is a red flag.
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )

        # Load cookies if we have them (avoids re-login)
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
                if isinstance(cookies, list):
                    await self.context.add_cookies(cookies)
                    logger.info("Loaded saved LinkedIn cookies.")
            except Exception as e:
                logger.warning(f"Could not load saved LinkedIn cookies: {e}")

        self.page = await self.context.new_page()

    def _save_cookies_sync(self, cookies: list):
        try:
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(cookies), encoding="utf-8")
            try:
                COOKIES_PATH.chmod(0o600)  # session cookies are credentials
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Could not save LinkedIn cookies: {e}")

    async def stop(self):
        """Save cookies and close browser."""
        try:
            if self.context:
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                logger.info("Saved LinkedIn cookies.")
        except Exception as e:
            logger.debug(f"Could not save cookies on shutdown: {e}")
        try:
            if self._remote and self.context:
                # A shared browser service: close only what Harvey opened.
                await self.context.close()
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.debug(f"Error stopping Playwright: {e}")
        finally:
            self._pw = None

    # ── Auth ──

    @staticmethod
    def login_hold() -> Optional[dict]:
        """The reason Harvey stopped logging in, or None."""
        try:
            if LOGIN_HOLD_PATH.exists():
                data = json.loads(LOGIN_HOLD_PATH.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {"reason": "unknown"}
        except (ValueError, OSError):
            return {"reason": "unreadable hold file"}
        return None

    @staticmethod
    def _hold(reason: str):
        try:
            LOGIN_HOLD_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOGIN_HOLD_PATH.write_text(json.dumps(
                {"reason": reason, "at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
        except OSError as e:
            logger.debug(f"Could not write LinkedIn login hold: {e}")
        logger.warning(
            f"LinkedIn login stopped: {reason}. Harvey won't try again on its own. "
            f"Sign in to LinkedIn by hand in a normal browser, then delete "
            f"data/{LOGIN_HOLD_PATH.name}.")

    async def _first_visible(self, selectors, timeout_ms: int):
        """The first selector that shows up, or None."""
        per = max(1000, timeout_ms // len(selectors))
        for selector in selectors:
            try:
                await self.page.wait_for_selector(selector, state="visible", timeout=per)
                return selector
            except Exception:
                continue
        return None

    async def _sign_in_button(self):
        button = self.page.locator("button:visible", has_text=SIGN_IN_TEXT).first
        try:
            if await button.count():
                return button
        except Exception:
            pass
        return self.page.locator('button[type="submit"]:visible').first

    async def login(self) -> bool:
        """Log into LinkedIn, once. Never retried: any failure returns
        False, and once the credentials may have been sent (a checkpoint, a
        refused password, a submit that didn't reach the feed) it also puts
        a hold on automatic logins (see LOGIN_HOLD_PATH). A caller may make a
        new instance every time, so only the hold stops the next attempt."""
        if self._login_result is not None:
            return self._login_result
        self._login_result = False

        hold = self.login_hold()
        if hold:
            logger.warning(
                f"LinkedIn login is on hold ({hold.get('reason', 'unknown')}, "
                f"{hold.get('at', '')}). Delete {LOGIN_HOLD_PATH.name} in data/ "
                f"after signing in by hand.")
            return False

        if not self.email or not self.password:
            logger.warning("LinkedIn credentials not configured; cannot log in.")
            return False

        if not self.page:
            await self.start()

        status = None
        try:
            resp = await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            status = getattr(resp, "status", None)
        except Exception as e:
            logger.error(f"Could not load LinkedIn login page: {type(e).__name__}")
            return False

        where = classify_url(self.page.url)
        if where == "feed":
            # A saved cookie session redirected straight past the form.
            logger.info("Already logged into LinkedIn.")
            self._login_result = True
            return True
        if where == "challenge":
            self._hold("LinkedIn showed a security checkpoint before the login form")
            return False

        email_sel = await self._first_visible(EMAIL_SELECTORS, LOGIN_FORM_TIMEOUT_MS)
        pass_sel = email_sel and await self._first_visible(PASSWORD_SELECTORS, 5000)
        if not email_sel or not pass_sel:
            hint = (" LinkedIn refuses some browsers (Obscura gets a 403): set "
                    "BROWSER_CDP_FALLBACK_URL to the Chromium service."
                    if status in (403, 429, 999) else
                    " The page may have changed.")
            logger.error(f"LinkedIn login form not found (HTTP {status}); "
                         f"no credentials were sent.{hint}")
            return False

        await self._random_delay(1, 3)
        clicking = False
        try:
            await self.page.locator(email_sel).first.fill(self.email)
            await self._random_delay(0.5, 1.5)
            await self.page.locator(pass_sel).first.fill(self.password)
            await self._random_delay(0.5, 1.5)
            button = await self._sign_in_button()
            clicking = True
            await button.click()
        except Exception as e:
            # Never include credentials in logged errors.
            msg = str(e)
            for secret in (self.password, self.email):
                if secret:
                    msg = msg.replace(secret, "***")
            logger.error(f"LinkedIn login could not submit the form: {msg.splitlines()[0][:200] if msg else type(e).__name__}")
            if clicking:
                # The click may have gone through: count it as an attempt.
                self._hold("the login form failed while submitting")
            return False

        where = "login"
        waited = 0.0
        while waited < LOGIN_WAIT_S:
            await asyncio.sleep(LOGIN_POLL_S)
            waited += LOGIN_POLL_S
            where = classify_url(self.page.url)
            if where != "login":
                break

        if where == "feed":
            logger.info("Successfully logged into LinkedIn.")
            try:
                self._save_cookies_sync(await self.context.cookies())
            except Exception as e:
                logger.debug(f"Could not save LinkedIn cookies: {e}")
            self._login_result = True
            return True
        if where == "challenge":
            self._hold("LinkedIn asked for a security check after the password")
            return False
        if where == "login":
            refused = False
            try:
                refused = await self.page.evaluate(
                    "() => !!document.querySelector('#error-for-password, #error-for-username, "
                    "[role=alert]')")
            except Exception:
                pass
            self._hold("LinkedIn refused the email or password" if refused else
                       "the login didn't leave the login page")
            return False
        self._hold(f"the login landed somewhere unexpected "
                   f"({urlparse(self.page.url).path[:60]})")
        return False

    # ── Search ──

    async def search_people(
        self,
        keywords: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
        max_results: int = 25,
    ) -> list[dict]:
        """Search LinkedIn for people matching criteria.

        Returns list of dicts with: name, title, company, linkedin_url, location
        """
        if not self.page:
            await self.start()
            if not await self.login():
                return []

        # Fold all criteria into the keyword search (URL-encoded).
        terms = " ".join(t for t in [keywords, title, company, location] if t).strip()
        if not terms:
            logger.warning("search_people called with no criteria; skipping.")
            return []

        search_url = (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords={quote_plus(terms)}"
        )

        results = []
        page_num = 1

        while len(results) < max_results:
            if self._cap_reached("search_pages", MAX_DAILY_SEARCH_PAGES):
                break

            url = f"{search_url}&page={page_num}"
            try:
                await self.page.goto(url, timeout=30000)
            except Exception as e:
                logger.warning(f"Failed to load search page {page_num}: {e}")
                break

            self._record("search_pages")
            await self._random_delay(3, 6)
            await self._wait_settled()
            await self._humanlike_scroll()

            # Extract search result cards
            cards = await self.page.query_selector_all("div.entity-result__item")

            if not cards:
                # Try alternate selector
                cards = await self.page.query_selector_all(
                    "li.reusable-search__result-container"
                )

            if not cards:
                logger.info(f"No results found on page {page_num}.")
                break

            for card in cards:
                if len(results) >= max_results:
                    break

                try:
                    profile = await self._extract_search_card(card)
                    if profile:
                        results.append(profile)
                except Exception as e:
                    logger.debug(f"Error extracting search card: {e}")
                    continue

            page_num += 1
            await self._random_delay(4, 8)

            # Safety: don't paginate deep on a single query
            if page_num > MAX_SEARCH_PAGES_PER_QUERY:
                break

        logger.info(f"Found {len(results)} LinkedIn profiles.")
        return results

    async def _extract_search_card(self, card) -> Optional[dict]:
        """Extract person info from a LinkedIn search result card."""
        # Get profile link
        link_el = await card.query_selector("a.app-aware-link")
        if not link_el:
            return None
        href = await link_el.get_attribute("href")
        if not href or "/in/" not in href:
            return None
        linkedin_url = href.split("?")[0]

        # Get name
        name_el = await card.query_selector("span.entity-result__title-text a span")
        name = await name_el.inner_text() if name_el else ""
        name = name.strip()

        # Get title/headline
        title_el = await card.query_selector("div.entity-result__primary-subtitle")
        title_text = await title_el.inner_text() if title_el else ""
        title_text = title_text.strip()

        # Get location
        loc_el = await card.query_selector("div.entity-result__secondary-subtitle")
        location = await loc_el.inner_text() if loc_el else ""
        location = location.strip()

        # Split name
        parts = name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        # Try to extract company from title (often "Title at Company")
        company = ""
        if " at " in title_text:
            title_text, company = (
                s.strip() for s in title_text.split(" at ", 1)
            )

        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title_text,
            "company": company,
            "linkedin_url": linkedin_url,
            "location": location,
        }

    # ── Profiles ──

    async def extract_profile(self, linkedin_url: str) -> Optional[dict]:
        """Scrape a LinkedIn profile page for detailed info."""
        if not self.page:
            await self.start()
            if not await self.login():
                return None

        if self._cap_reached("profile_views", MAX_DAILY_PROFILE_VIEWS):
            return None

        try:
            await self.page.goto(linkedin_url, timeout=30000)
        except Exception as e:
            logger.warning(f"Failed to load profile {linkedin_url}: {e}")
            return None

        self._record("profile_views")
        await self._random_delay(3, 5)
        await self._wait_settled()
        await self._humanlike_scroll()

        try:
            # Name
            name_el = await self.page.query_selector("h1")
            name = await name_el.inner_text() if name_el else ""

            # Headline
            headline_el = await self.page.query_selector("div.text-body-medium")
            headline = await headline_el.inner_text() if headline_el else ""

            # Location
            loc_el = await self.page.query_selector("span.text-body-small.inline")
            location = await loc_el.inner_text() if loc_el else ""

            # About section
            about_el = await self.page.query_selector(
                "section.pv-about-section div.inline-show-more-text"
            )
            about = await about_el.inner_text() if about_el else ""

            parts = name.strip().split(" ", 1)

            return {
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "headline": headline.strip(),
                "location": location.strip(),
                "about": about.strip(),
                "linkedin_url": linkedin_url,
            }

        except Exception as e:
            logger.error(f"Error extracting profile {linkedin_url}: {e}")
            return None
