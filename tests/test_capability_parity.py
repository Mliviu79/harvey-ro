"""Capabilities change nothing: the same inputs leave the same database.

Each scenario drives one source, search or enricher through Harvey's own
entry points with recorded (invented) answers in place of the network, then
dumps every table it can touch. The dump is compared with one recorded from
the code as it was before the capability wrappers and core ingest, so a
wrapper that drops a field, reorders a write or adds one fails here.

The dump leaves out what can't be the same twice (generated ids, which are
renamed in order of appearance; timestamps, kept only as set or not) and
the provenance columns observations gained with the wrappers (module,
capability, origin_company_id). A missing recording is printed and the test
skipped, so a new scenario is recorded by running it once.

Every company, person, address and number here is invented.
"""

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig
from harvey.config import load_config as _load_config
from harvey.models import Company
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import StateManager

pytestmark = pytest.mark.modules_on("web_marketing", "startups", "hiring", "web_search_scrape")

GOLDEN = Path(__file__).parent / "fixtures" / "parity"
# Columns added with the wrappers: provenance, not what was found. (A
# company's website_trust and website_source are the contact finder's, and
# no run recorded here finds a website that way.)
NEW_COLUMNS = {"observations": {"module", "capability", "origin_company_id"},
               "companies": {"website_trust", "website_source"}}
TABLES = ("companies", "company_ids", "company_activities", "observations", "prospects",
          "match_candidates", "company_merges", "company_merge_moves", "email_patterns",
          "runs", "settings")
# Rows that are about the install, not the run (vocabulary and module bookkeeping).
SETTING_SKIP = re.compile(r"^(signals?\.|modules?\.|ui\.|vocabulary|caen_)")


def load_config():
    """The tracked template, never a machine's own harvey.local.yaml."""
    return _load_config(str(Path(__file__).resolve().parents[1] / "harvey.yaml"))


# ── The dump ──

def dump(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ids: dict[str, str] = {}
    for table, prefix in (("companies", "C"), ("prospects", "P"), ("runs", "R")):
        for (value,) in con.execute(f"SELECT id FROM {table} ORDER BY rowid"):
            ids.setdefault(value, f"{prefix}{len([k for k in ids.values() if k[0] == prefix]) + 1}")

    def clean(value):
        if isinstance(value, str):
            for raw, label in ids.items():
                if raw and raw in value:
                    value = value.replace(raw, label)
        return value

    out = {}
    for table in TABLES:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        skip = NEW_COLUMNS.get(table, set()) | ({"id"} if table == "observations" else set())
        rows = []
        for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            item = {}
            for col in cols:
                if col in skip:
                    continue
                value = row[col]
                if col.endswith("_at") or col in ("observed_at", "send_at"):
                    value = value is not None and value != ""
                item[col] = clean(value)
            if table == "settings" and SETTING_SKIP.match(item.get("key", "")):
                continue
            if table == "settings":
                # Hunter's monthly counter is keyed by the month it ran in.
                item["key"] = re.sub(r"(_count:)\d{4}-\d{2}$", r"\1<month>", item["key"])
            rows.append(item)
        out[table] = rows
    con.close()
    return out


def check(name: str, got: dict):
    path = GOLDEN / f"{name}.json"
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False)
    if not path.exists():
        print(f"\n=====PARITY {name}=====\n{text}\n=====END {name}=====")
        pytest.skip(f"no recording for {name}: printed above")
    want = json.loads(path.read_text(encoding="utf-8"))
    for table in TABLES:
        mine, theirs = got.get(table) or [], want.get(table) or []
        if mine == theirs:
            continue
        changed = [(i, {k: (a.get(k), b.get(k)) for k in a.keys() | b.keys() if a.get(k) != b.get(k)})
                   for i, (a, b) in enumerate(zip(mine, theirs)) if a != b]
        pytest.fail(f"{name}: {table} differs: {len(mine)} rows, recorded {len(theirs)}; "
                    f"first changes (now, recorded): {changed[:3]}")


# ── Shared setup ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "parity.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No real waiting, and no developer .env: fixed keys for every source."""
    import asyncio

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    env = EnvConfig(dataforseo_login="u", dataforseo_password="p", serper_api_key="k",
                    tinyfish_api_key="k", scavio_api_key="k")
    monkeypatch.setattr("harvey.config.load_env", lambda *a, **k: env)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setenv("BROWSER_CDP_URL", "off")


def recorded(monkeypatch, handler):
    """Every httpx client answers from ``handler`` instead of the network."""
    real = httpx.AsyncClient

    class Recorded(real):
        def __init__(self, *a, **kw):
            kw.pop("transport", None)
            super().__init__(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Recorded)


def entry(new: str, old):
    """The entry point under its current name: the core run once it has
    moved out of the collector, the collector's own before."""
    module, _, name = new.rpartition(".")
    try:
        mod = __import__(module, fromlist=[name])
        return getattr(mod, name)
    except (ImportError, AttributeError):
        return old()


def run_discovery():
    return entry("harvey.modules.runs.run_discovery", lambda: D.run_discovery)


# ── Recorded answers (invented businesses, example domains) ──

DFS_LISTINGS = {"status_code": 20000, "cost": 0.0132, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"title": "Northvale Roofing Company", "category": "Roofing contractor",
     "place_id": "ChIJnorth1", "cid": "1001", "phone": "+13035550199",
     "url": "https://www.northvaleroofing.example/", "domain": "northvaleroofing.example",
     "address": "1400 Example Row", "address_info": {"city": "Denver", "region": "Colorado"},
     "is_claimed": True, "rating": {"value": 4.4, "votes_count": 512},
     "check_url": "https://maps.example/?cid=1001"},
    {"title": "No Website Roofing", "category": "Roofing contractor",
     "place_id": "ChIJnosite", "phone": "+15550100377", "url": None, "domain": None,
     "address_info": {"city": "Denver", "region": "Colorado"}, "is_claimed": False,
     "rating": {"value": 4.9, "votes_count": 31}},
    {"title": "Yelp", "category": "Directory", "place_id": "ChIJjunk",
     "url": "https://www.yelp.com/biz/x", "domain": "yelp.com"},
]}]}]}

DFS_SERP = {"status_code": 20000, "cost": 0.006, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "local_pack", "rank_group": 1, "title": "Kettleman Roofing",
     "domain": "kettlemanroofing.example", "url": "https://kettlemanroofing.example/",
     "phone": "+15550100411", "rating": {"value": 4.8, "votes_count": 90}},
    {"type": "local_pack", "rank_group": 2, "title": "Pack Only Roofing", "cid": "77",
     "phone": "+15550100422"},
    {"type": "organic", "rank_group": 1, "domain": "yelp.com",
     "url": "https://yelp.com/denver", "title": "Best Roofers"},
    {"type": "organic", "rank_group": 14, "domain": "kettlemanroofing.example",
     "url": "https://kettlemanroofing.example/", "title": "Kettleman Roofing | Denver"},
    {"type": "organic", "rank_group": 17, "domain": "northvaleroofing.example",
     "url": "https://www.northvaleroofing.example/roofs", "title": "Northvale Roofing Company"},
]}]}]}

DFS_MAPS = {"status_code": 20000, "cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
    {"type": "maps_search", "rank_group": 1, "title": "Northvale Roofing Company",
     "place_id": "ChIJnorth1", "cid": "1001", "domain": "northvaleroofing.example",
     "url": "https://www.northvaleroofing.example/", "phone": "+13035550199",
     "category": "Roofing contractor", "is_claimed": True,
     "address_info": {"city": "Denver", "region": "Colorado"},
     "rating": {"value": 4.5, "votes_count": 515}},
    {"type": "maps_paid_item", "rank_group": 1, "title": "Ad Roofing"},
    {"type": "maps_search", "rank_group": 2, "title": "Pack Only Roofing", "cid": "77",
     "phone": "+15550100422", "category": "Roofing contractor", "is_claimed": False,
     "rating": {"value": 3.9, "votes_count": 8}},
]}]}]}

SERPER = {"organic": [
    {"title": "Kettleman Roofing", "link": "https://www.kettlemanroofing.example/", "position": 3},
    {"title": "Angi", "link": "https://www.angi.com/companylist/denver/roofing", "position": 4},
    {"title": "Brightpath Labs - backed by Y Combinator",
     "link": "https://brightpathlabs.example/about", "position": 5},
    {"title": "Careers at Lumenhaus", "link": "https://jobs.lever.co/lumenhaus.example/123",
     "position": 6},
    {"title": "Northvale Roofing Company", "link": "https://northvaleroofing.example/", "position": 8},
]}

TINYFISH = {"query": "roofer Denver, CO", "results": [
    {"position": 1, "site_name": "summitroofing.example", "title": "Summit Roofing - Denver",
     "url": "https://summitroofing.example/services"},
    {"position": 2, "site_name": "directory.example", "title": "Top Roofers in Denver | Directory",
     "url": "https://www.directory.example/companylist/denver/roofing"},
], "total_results": 2, "page": 0}

OVERPASS = {"elements": [
    {"type": "node", "id": 501, "tags": {"name": "Summit Roofing", "craft": "roofer",
     "website": "https://summitroofing.example", "phone": "+1 555 010 0500",
     "addr:city": "Denver", "addr:state": "CO", "addr:street": "Main St", "addr:housenumber": "5"}},
    {"type": "way", "id": 502, "tags": {"name": "Ridge Roofers", "craft": "roofer",
     "phone": "+1 555 010 0501"}},
    {"type": "node", "id": 503, "tags": {"craft": "roofer"}},
]}

SCAVIO_MAPS = {"local_results": [
    {"position": 1, "title": "Kettleman Roofing", "place_id": "ChIJkettle",
     "website": "https://www.kettlemanroofing.example/?utm_source=gbp",
     "phone": "(303) 555-0199", "address": "12 Grant St, Denver, CO 80203",
     "type": "Roofing contractor", "rating": 4.9, "reviews": 409},
    {"position": 2, "title": "No Site Roofing", "place_id": "ChIJnosite2",
     "type": "Roofing contractor", "rating": 5, "reviews": 12},
], "credits_used": 1}

SCAVIO_SEARCH = {"organic_results": [
    {"position": 1, "title": "Kettleman Roofing | Denver", "link": "https://www.kettlemanroofing.example/"},
    {"position": 2, "title": "Summit Roofing", "link": "https://summitroofing.example/"},
], "credits_used": 1, "pagination": {"current": 1}}


def provider_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "business_listings" in url:
        return httpx.Response(200, json=DFS_LISTINGS)
    if "organic/live" in url:
        return httpx.Response(200, json=DFS_SERP)
    if "maps/live" in url:
        return httpx.Response(200, json=DFS_MAPS)
    if "serper" in url:
        return httpx.Response(200, json=SERPER)
    if "tinyfish" in url or "agent.tinyfish" in url:
        return httpx.Response(200, json=TINYFISH)
    if "overpass" in url or "kumi.systems" in url or "openstreetmap" in url:
        return httpx.Response(200, json=OVERPASS)
    if "scavio" in url and "maps" in url:
        return httpx.Response(200, json=SCAVIO_MAPS)
    if "scavio" in url:
        return httpx.Response(200, json=SCAVIO_SEARCH)
    return httpx.Response(404, json={})


DENVER = dict(location="Denver, CO", coordinate="39.7392,-104.9903,40")


async def _discover(state, provider, queries, max_spend=5.0):
    return await run_discovery()(state, load_config(), provider, queries, max_spend=max_spend)


# ── Discovery: every search capability ──

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["osm", "dataforseo_listings", "dataforseo_serp",
                                      "dataforseo_maps", "serper", "tinyfish",
                                      "scavio_maps", "scavio_search"])
async def test_discovery_search(state, monkeypatch, provider):
    recorded(monkeypatch, provider_handler)
    # On file before the run: the same business by phone and name in its
    # town (merged automatically), and one by domain (the same record).
    await state.save_company(Company(name="Northvale Roofing Company", phone="+13035550199",
                                     location="Denver, Colorado", source="manual"))
    await state.add_company(Company(name="Kettleman", domain="kettlemanroofing.example",
                                    source="manual"))
    term = "roofer"
    queries = [D.DiscoveryQuery(term=term, **DENVER), D.DiscoveryQuery(term=term, **DENVER)]
    await _discover(state, provider, queries)
    # Again, later: known businesses, ranks only where better, and a rank
    # nobody holds any more says so.
    await _discover(state, provider, [D.DiscoveryQuery(term=term, **DENVER)])
    check(f"discover_{provider}", dump(state.db_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["yc", "accelerator", "funding", "hiring"])
async def test_discovery_preset(state, monkeypatch, preset):
    recorded(monkeypatch, provider_handler)
    config = load_config()
    config.icp.titles = ["Engineer"]
    queries = D.build_queries(config, preset=preset)[:2]
    for q in queries:
        # The funding search's window starts 18 months before today.
        q.term = re.sub(r"after:\S+", "after:2025-01-01", q.term)
    await run_discovery()(state, config, "serper", queries, max_spend=5.0)
    check(f"preset_{preset}", dump(state.db_path))


@pytest.mark.asyncio
async def test_geocode_cache(state, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.params.get("q"))
        if request.url.params.get("q") == "Nowhere, ZZ":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"lat": "39.7392", "lon": "-104.9903"}])

    recorded(monkeypatch, handler)
    resolve = entry("harvey.modules.runs.resolve_coordinates", lambda: D.resolve_coordinates)
    queries = [D.DiscoveryQuery(term="roofer", location="Denver, CO"),
               D.DiscoveryQuery(term="roofer", location="Nowhere, ZZ"),
               D.DiscoveryQuery(term="plumber", location="Denver, CO")]
    await resolve(state, queries)
    await resolve(state, [D.DiscoveryQuery(term="roofer", location="Denver, CO")])
    assert calls == ["Denver, CO", "Nowhere, ZZ"]
    got = dump(state.db_path)
    got["settings"] += [{"query": q.location, "coordinate": q.coordinate} for q in queries]
    check("geocode", got)


# ── ONRC: the registry pull and the streaming import ──

@pytest.mark.asyncio
async def test_registry_pull(state, tmp_path):
    from harvey import registry
    from tests.test_registry import EXTRA_CAEN, EXTRA_FIRMS, EXTRA_STATUS
    from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, write
    from harvey.collectors import onrc

    folder = tmp_path / "raw"
    folder.mkdir()
    raw = {"caen": write(folder / onrc.FILES["caen"], CAEN + EXTRA_CAEN),
           "status": write(folder / onrc.FILES["status"], STATUS + EXTRA_STATUS),
           "firms": write(folder / onrc.FILES["firms"], FIRMS + EXTRA_FIRMS),
           "people": write(folder / onrc.FILES["people"], PEOPLE)}
    registry.build(raw, DATASET, tmp_path / "registry.db")
    con = registry.connect(tmp_path / "registry.db")
    pull = entry("harvey.modules.runs.registry_pull", lambda: registry.pull)
    # On file already: one by CUI (filled), one holding a declared domain.
    await state.add_company(Company(name="Clinica Zâmbet", cui="90000017", source="discover:osm"))
    await state.add_company(Company(name="Alt Zâmbet", domain="zambet-exemplu.example.ro"))
    await pull(state, con, registry.resolve(con, "dentist BV"))
    await pull(state, con, registry.resolve(con, "8623 BV"))
    await pull(state, con, registry.resolve(con, "8690"), with_administrators=False)
    con.close()
    check("registry_pull", dump(state.db_path))


@pytest.mark.asyncio
async def test_registry_import(state, tmp_path):
    from harvey.collectors import onrc
    from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, write

    files = {"caen": write(tmp_path / onrc.FILES["caen"], CAEN),
             "status": write(tmp_path / onrc.FILES["status"], STATUS),
             "firms": write(tmp_path / onrc.FILES["firms"], FIRMS),
             "people": write(tmp_path / onrc.FILES["people"], PEOPLE)}
    imp = entry("harvey.modules.runs.import_registry", lambda: onrc.import_registry)
    await imp(state, files, DATASET, county="BV", classes=onrc.parse_classes("dentist"))
    await imp(state, files, DATASET, county="BV", classes=onrc.parse_classes("fizioterapie"),
              do_not_contact=["zambet-exemplu.example.ro"])
    check("registry_import", dump(state.db_path))


# ── The website reader and what reads its pages ──

HOME = """<html><head><title>Brightpath Dental | Denver Dentist</title>
<meta property="og:site_name" content="Brightpath Dental">
<script src="https://www.googletagmanager.com/gtag/js?id=AW-123"></script>
<script>fbq('init', '1');</script>
<script type="application/ld+json">{"@type": "Dentist", "telephone": "+1 303 555 0142"}</script>
<script src="https://js.hs-scripts.com/1.js"></script>
</head><body>
<nav><a href="/about">About</a> <a href="/team">Our team</a> <a href="/careers">Careers</a>
<a href="/contact">Contact</a></nav>
<p>Brightpath Dental is backed by Y Combinator (W21). We are proud to announce
our $4M seed round, led by Example Ventures, in March 2026.</p>
<p>Call us: <a href="tel:+13035550142">(303) 555-0142</a></p>
<form action="/book"><input name="email"></form>
<a href="https://calendly.com/brightpath">Book online</a>
<footer>Website by <a href="https://pixelcraft-agency.example">PixelCraft Agency</a></footer>
</body></html>""" + "<!-- padding -->" * 60

ABOUT = """<html><body><h1>About Brightpath Dental</h1>
<p>We joined Techstars Denver 2022 and the Example Accelerator.</p></body></html>""" + " " * 600
TEAM = """<html><body><h2>Meet the team</h2>
<div class="team"><h3>Nora Ellison</h3><p>Owner &amp; President</p>
<h3>Ivo Quist</h3><p>Practice Manager</p></div>
<p>Email Nora at nellison@brightpathdental.example or the office at office@brightpathdental.example.</p>
</body></html>""" + " " * 600
CAREERS = """<html><body><h1>Careers</h1>
<a href="https://boards.greenhouse.io/brightpathdental">Open roles</a></body></html>""" + " " * 600
CONTACT = """<html><body><h1>Contact</h1><p>Front desk: +1 303 555 0177</p>
<a href="mailto:office@brightpathdental.example">office@brightpathdental.example</a>
</body></html>""" + " " * 600
ROBOTS = "User-agent: GPTBot\nDisallow: /\n"
SITEMAP = ("<urlset><url><loc>https://brightpathdental.example/</loc><lastmod>2025-01-02</lastmod></url>"
           "<url><loc>https://brightpathdental.example/about</loc><lastmod>2024-06-01</lastmod></url></urlset>")
GREENHOUSE = {"departments": [{"name": "Clinical", "jobs": [
    {"title": "Dental Hygienist", "absolute_url": "https://boards.greenhouse.io/brightpathdental/jobs/1",
     "location": {"name": "Denver, CO"}, "updated_at": "2026-09-01T00:00:00Z",
     "departments": [{"name": "Clinical"}]},
    {"title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/brightpathdental/jobs/2",
     "location": {"name": "Remote"}, "updated_at": "2026-09-15T00:00:00Z",
     "departments": [{"name": "Engineering"}]},
]}]}
SITE = {"": HOME, "/about": ABOUT, "/team": TEAM, "/careers": CAREERS, "/contact": CONTACT,
        "/robots.txt": ROBOTS, "/sitemap.xml": SITEMAP}


def site_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "greenhouse" in url and "/v1/boards/" in url or "boards-api.greenhouse" in url:
        return httpx.Response(200, json=GREENHOUSE)
    host = request.url.host.removeprefix("www.")
    if host != "brightpathdental.example" or request.url.scheme != "https":
        return httpx.Response(404, text="")
    path = request.url.path.rstrip("/")
    if path in SITE:
        ctype = "text/plain" if path == "/robots.txt" else (
            "application/xml" if path == "/sitemap.xml" else "text/html")
        return httpx.Response(200, text=SITE[path], headers={"content-type": ctype})
    return httpx.Response(404, text="not here")


@pytest.mark.asyncio
async def test_website_profile(state, monkeypatch):
    recorded(monkeypatch, site_handler)
    from harvey.collectors import profile as P
    from harvey import public_http

    monkeypatch.setattr(public_http, "PublicHTTPTransport", lambda: httpx.MockTransport(site_handler))
    from datetime import date, datetime

    class Day(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 30)

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 12, tzinfo=tz)

    # A sitemap's age and a raise's date are read against today.
    monkeypatch.setattr(P, "date", Day)
    monkeypatch.setattr(P, "datetime", Now)
    config = load_config()
    config.icp.hiring_signals = ["hygienist"]
    await state.add_company(Company(name="Denver, CO | Dentist Near Me",
                                    domain="brightpathdental.example", location="Denver, CO",
                                    source_url="https://brightpathdental.example/about"))
    await state.add_company(Company(name="Gone Dental", domain="gone-dental.example"))
    pending = await state.companies_needing_profile(limit=10, stale_days=90)
    profile = entry("harvey.modules.runs.profile_companies", lambda: P.profile_companies)
    await profile(state, pending, config=config)
    check("website_profile", dump(state.db_path))


# ── Contacts: people, phones, email pattern and verification ──

@pytest.mark.asyncio
async def test_scout_contacts(state, monkeypatch):
    recorded(monkeypatch, site_handler)
    from harvey import public_http
    from harvey.agents import scout as scout_mod
    from harvey.integrations import email_finder as ef

    monkeypatch.setattr(public_http, "PublicHTTPTransport", lambda: httpx.MockTransport(site_handler))

    async def mx(domain):
        return "mx.brightpathdental.example"

    async def reoon(email, key):
        return {"status": "valid" if email.startswith("nellison") else "invalid", "catch_all": False}

    async def smtp(email):
        return None

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "verify_reoon", reoon)
    monkeypatch.setattr(ef, "verify_email_smtp", smtp)
    config = load_config()
    config.icp.titles = ["Owner", "Practice Manager"]
    env = EnvConfig(reoon_api_key="k")
    await state.add_company(Company(name="Brightpath Dental", domain="brightpathdental.example",
                                    location="Denver, CO", source="discover:osm"))
    scout = scout_mod.Scout(None, state, config, env)
    await scout._prospect_via_known_companies()
    check("scout_contacts", dump(state.db_path))


@pytest.mark.asyncio
async def test_scout_hunter(state, monkeypatch):
    from harvey.agents import scout as scout_mod
    from harvey.integrations import email_finder as ef

    async def people(domain, key, limit=10):
        return [{"first_name": "Nora", "last_name": "Vale", "position": "Owner",
                 "value": f"nora@{domain}", "verification": {"status": "valid"},
                 "linkedin": "https://linkedin.example/in/noravale"},
                {"first_name": "Ivo", "last_name": "Quist", "position": "Intern",
                 "value": f"ivo@{domain}"}]

    monkeypatch.setattr(ef, "hunter_domain_people", people)
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "1")
    config = load_config()
    config.icp.titles = ["Owner"]
    env = EnvConfig(hunter_api_key="k")
    await state.add_company(Company(name="Clinic 1", domain="clinic1.example"))
    # No site, so never asked about: one company is due, and it is this one.
    await state.add_company(Company(name="Clinic 2"))
    await scout_mod.Scout(None, state, config, env)._prospect_via_hunter()
    check("scout_hunter", dump(state.db_path))
