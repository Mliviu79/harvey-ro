"""A site read's requests: the menu read (where contacts come from) has its
own budget, and everything else the read fetches (About and press pages,
the page a search found, robots.txt, sitemap.xml, the careers page and its
job board's feed) a small one of its own, so the second can never starve
the first. A signal whose page the extra budget couldn't pay for stays
unanswered, never a "no", and a page the read fetched once is never asked
for again. Every site is a mock transport; all data is fictional."""

import asyncio

import httpx
from harvey.browser import PageReader, Renderer
from harvey.collectors import profile
from harvey.collectors.profile import (
    MAX_EXTRA_FETCHES, MAX_SITE_FETCHES, MAX_SITE_PAGES, site_draft,
)
from harvey.modules import api

DOMAIN = "acme-startup.example"
SITE = f"https://{DOMAIN}"
FILLER = "<p>" + "Acme builds tools for small teams. " * 30 + "</p>"
MENU = ('<nav><a href="/team/">Team</a><a href="/about/">About</a>'
        '<a href="/contact/">Contact</a></nav>')
TEAM = ("<h3>Jane Doe</h3><p>Founder and CEO</p><h3>John Roe</h3><p>Head of Growth</p>"
        + FILLER)
# Everything these signals look for beyond the homepage.
EXTRA_SIGNALS = {"YC_BACKED", "ACCELERATOR", "FUNDING_ROUND", "BLOCKS_AI_CRAWLERS",
                 "SITE_PAGE_COUNT", "BLOG_STALE", "HIRING_ROLE", "OPEN_ROLES"}
PEOPLE = {"CONTACT_FOUND", "DECISION_MAKER_TITLE", "LIKELY_OWNER"}


class Site:
    """``pages`` (path -> html) at DOMAIN, 404 everywhere else; remembers
    every request."""

    def __init__(self, pages):
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.asked.append(url)
        if request.url.host != DOMAIN:
            return httpx.Response(404, text="not here", headers={"content-type": "text/html"})
        path = request.url.path.rstrip("/") or "/"
        page = self.pages.get(path)
        if page is None:
            return httpx.Response(404, text="not here", headers={"content-type": "text/html"})
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    def count(self, path):
        return sum(1 for u in self.asked if (httpx.URL(u).path.rstrip("/") or "/") == path)


def _read(site, wanted, source_url=""):
    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(site), follow_redirects=True)
        client.public_only = True
        ctx = api.Context(http=client, browser=PageReader(Renderer([])), wanted=frozenset(wanted),
                          settings={"contacts": True, "role_keywords": ["engineer"]})
        try:
            return await site_draft(api.Input(company_id="c1", name="Acme", domain=DOMAIN,
                                              extra={"source_url": source_url}), ctx)
        finally:
            await client.aclose()

    return asyncio.run(go())


def _menu_requests(site):
    menu = {"/team", "/about", "/contact", "/"}
    return [u for u in site.asked if (httpx.URL(u).path.rstrip("/") or "/") in menu]


def test_the_extra_budget_covers_every_signal_and_never_eats_the_menu_read():
    # No About, press, careers, robots or sitemap page: every guess misses.
    site = Site({"/": MENU + FILLER, "/team": TEAM, "/contact": "<form></form>" + FILLER})
    draft = _read(site, EXTRA_SIGNALS | PEOPLE,
                  source_url=f"{SITE}/blog/we-raised")
    menu = _menu_requests(site)
    extra = [u for u in site.asked if u not in menu]
    assert len(extra) <= MAX_EXTRA_FETCHES
    assert len(menu) <= MAX_SITE_FETCHES and len(draft.pages) <= MAX_SITE_PAGES
    # The menu read still found its people and its contact form.
    assert {(p.first_name, p.last_name) for p in draft.people} == {("Jane", "Doe"), ("John", "Roe")}
    assert any(c.kind == "contact_form" for c in draft.contacts)


def test_normal_use_with_every_signal_confirmed_fits_the_extra_budget():
    """An About page, a careers link and its board: every signal answered."""
    home = MENU + '<a href="/careers">Careers</a>' + FILLER
    careers = '<a href="https://boards.greenhouse.io/acme">Open roles</a>' + FILLER
    about = "<p>Acme is backed by Y Combinator (YC W24).</p>" + FILLER
    site = Site({"/": home, "/about": about, "/careers": careers, "/team": TEAM,
                 "/robots.txt": "User-agent: *\nAllow: /\n",
                 "/sitemap.xml": "<urlset><url><loc>https://acme-startup.example/</loc>"
                                 "<lastmod>2026-09-01</lastmod></url></urlset>"})
    draft = _read(site, EXTRA_SIGNALS | PEOPLE)
    codes = {f.code for f in draft.facts}
    assert {"YC_BACKED", "ACCELERATOR", "FUNDING_ROUND", "BLOCKS_AI_CRAWLERS",
            "SITE_PAGE_COUNT", "BLOG_STALE"} <= codes
    # The board's feed was asked for (here it doesn't answer: nothing recorded).
    assert any("greenhouse" in u for u in site.asked)


def test_a_spent_extra_budget_leaves_signals_unanswered_not_no(monkeypatch):
    monkeypatch.setattr(profile, "MAX_EXTRA_FETCHES", 0)
    home = MENU + '<a href="https://boards.greenhouse.io/acme">Jobs</a>' + FILLER
    site = Site({"/": home, "/team": TEAM})
    draft = _read(site, EXTRA_SIGNALS | PEOPLE | {"NO_SCHEMA_MARKUP"})
    codes = {f.code for f in draft.facts}
    # Nothing the extra budget pays for was asked, and nothing is said about it.
    assert not codes & EXTRA_SIGNALS
    assert not any(p in u for u in site.asked
                   for p in ("/robots.txt", "/sitemap.xml", "/press", "/careers", "greenhouse"))
    # The homepage's own signals and the menu read are untouched.
    assert "NO_SCHEMA_MARKUP" in codes
    assert {(p.first_name, p.last_name) for p in draft.people} == {("Jane", "Doe"), ("John", "Roe")}


def test_with_budget_left_a_missing_page_is_a_no():
    site = Site({"/": MENU + FILLER})
    draft = _read(site, {"YC_BACKED", "ACCELERATOR", "FUNDING_ROUND", "BLOCKS_AI_CRAWLERS"})
    facts = {f.code: f.value_num for f in draft.facts}
    assert facts == {"YC_BACKED": 0, "ACCELERATOR": 0, "FUNDING_ROUND": 0, "BLOCKS_AI_CRAWLERS": 0}


def test_a_page_is_fetched_once_per_read():
    # /about is the startup signals' About page and the menu's About page.
    site = Site({"/": MENU + FILLER, "/about": "<p>About Acme.</p>" + FILLER, "/team": TEAM})
    draft = _read(site, {"YC_BACKED", "ACCELERATOR", "FUNDING_ROUND", "HIRING_ROLE"} | PEOPLE)
    assert site.count("/about") == 1
    assert any(p.url.rstrip("/") == f"{SITE}/about" for p in draft.pages)
    # "/careers" and "/careers/" (both guessed) are one page too.
    assert site.count("/careers") == 1



def test_a_failed_fetch_is_asked_again_by_the_menu_read():
    # The About guess times out once; the menu's About link asks again.
    asked = []

    def handler(request):
        path = request.url.path.rstrip("/") or "/"
        asked.append(path)
        if request.url.host != DOMAIN:
            return httpx.Response(404, text="no", headers={"content-type": "text/html"})
        if path == "/about" and asked.count("/about") == 1:
            raise httpx.ReadTimeout("slow")
        pages = {"/": '<nav><a href="/about/">About</a><a href="/contact/">Contact</a></nav>'
                      + FILLER, "/about": TEAM}
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404, text="no", headers={"content-type": "text/html"})

    draft = _read(handler, {"YC_BACKED"} | PEOPLE)
    assert asked.count("/about") == 2
    assert {(p.first_name, p.last_name) for p in draft.people} == {("Jane", "Doe"), ("John", "Roe")}


def test_the_sites_www_and_bare_spellings_are_one_page():
    # The site answers on the bare host; the menu links the www spelling,
    # which redirects to it: /about is fetched once.
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if request.url.host == "www." + DOMAIN:
            return httpx.Response(301, headers={"location": f"{SITE}{request.url.path}"})
        if request.url.host != DOMAIN:
            return httpx.Response(404, text="no", headers={"content-type": "text/html"})
        path = request.url.path.rstrip("/") or "/"
        pages = {"/": f'<nav><a href="https://www.{DOMAIN}/about/">About</a>'
                      f'<a href="/contact/">Contact</a></nav>' + FILLER, "/about": TEAM}
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404, text="no", headers={"content-type": "text/html"})

    draft = _read(handler, {"YC_BACKED"} | PEOPLE)
    assert [u for u in asked if u.rstrip("/").endswith("/about")] == [f"{SITE}/about"]
    assert {(p.first_name, p.last_name) for p in draft.people} == {("Jane", "Doe"), ("John", "Roe")}


def test_the_careers_pages_own_roles_count_when_the_budget_cannot_pay_the_feed(monkeypatch):
    # The careers page takes the last extra request; its board's feed can't
    # be paid for, so the page's own roles are read, as when a feed fails.
    monkeypatch.setattr(profile, "MAX_EXTRA_FETCHES", 1)
    careers = ('<a href="https://boards.greenhouse.io/acme">Board</a>'
               "<h3>Hiring: Field Technician</h3>" + FILLER)
    site = Site({"/": '<a href="/careers">Careers</a>' + FILLER, "/careers": careers})
    draft = _read(site, {"HIRING_ROLE", "OPEN_ROLES"})
    assert [(f.code, f.value_text) for f in draft.facts] == [
        ("HIRING_ROLE", "Hiring: Field Technician")]
    assert not any("greenhouse" in u for u in site.asked)
