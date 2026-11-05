"""Dashboard API for the signal listing, the cohort engine behind the
filters, and Today.

These cover the contract the UI depends on: the signals of modules that are
on are listed (read only, with their costs and counts), every one of them is
collected with no confirmation, and a cohort is a query rather than a
static list.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.modules import switches
from harvey.signals import SIGNAL_CATALOG
from harvey.state import StateManager

# Every signal the shipped modules declare: the old catalog's, and the ones a
# module brings with its own words (the financial statements').
DECLARED = sorted({sig["code"] for m in switches.shipped() for sig in m.signals})

# These modules ship off; here they are on, as a user who wants them has them.
pytestmark = pytest.mark.modules_on("web_marketing", "startups", "hiring")


@pytest.fixture
def client(monkeypatch):
    """A dashboard bound to a throwaway database, used the way its own UI
    uses it: same origin, with the token (tests/test_dashboard_auth.py covers
    everyone else)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


# ── The listing ──


def test_signals_list_every_declared_signal_with_no_status(client):
    """Read only: no summary of decisions, no status to set."""
    data = client.get("/api/signals").json()
    assert data["total"] == len(DECLARED)
    assert "summary" not in data
    assert all("status" not in s for g in data["groups"] for s in g["signals"])
    assert client.post("/api/signals/status",
                       json={"codes": ["SERP_RANK"], "status": "confirmed"}).status_code in (
        404, 405)


def test_signals_grouped_by_module_with_plain_language(client):
    data = client.get("/api/signals").json()
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["people", "phones", "website", "dataforseo", "email_verify", "hiring",
                    "startups", "web_marketing", "anaf", "mf_financials", "mf_taxpayers",
                    "onrc"]
    for group in data["groups"]:
        assert group["label"] and group["blurb"]  # every module explains itself
        for sig in group["signals"]:
            assert sig["description"] and sig["cost_note"]


# ── Cohorts (the engine the Contacts and Companies filters use) ──


@pytest.mark.asyncio
async def _seed_observations(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    from harvey.signals import sync_vocabulary
    await sync_vocabulary(state)
    from harvey.models import Company

    ids = []
    for n, name in enumerate(["Alpha Roofing", "Beta Roofing", "Gamma Roofing"]):
        ids.append(await state.add_company(
            Company(name=name, domain=f"{name.split()[0].lower()}.com")))
    # Alpha: agency + ads. Beta: agency only. Gamma: ads + booking already.
    await state.add_observations([
        {"company_id": ids[0], "signal_code": "INCUMBENT_AGENCY", "value_text": "Studio3"},
        {"company_id": ids[0], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
        {"company_id": ids[1], "signal_code": "INCUMBENT_AGENCY", "value_text": "Etna"},
        {"company_id": ids[2], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
        {"company_id": ids[2], "signal_code": "NO_ONLINE_BOOKING", "value_num": 1},
    ])
    return ids


def test_cohort_is_a_set_intersection(client):
    import asyncio
    asyncio.run(_seed_observations(client.db_path))
    state = StateManager(str(client.db_path))

    async def verdicts(require, exclude=(), verdict=("match",)):
        return await state.cohort_verdicts(list(require), list(exclude), verdicts=verdict)

    counts, rows = asyncio.run(verdicts(["INCUMBENT_AGENCY", "RUNNING_GOOGLE_ADS"]))
    assert counts["match"] == 1 and rows[0]["name"] == "Alpha Roofing"
    assert asyncio.run(verdicts(["RUNNING_GOOGLE_ADS"]))[0]["match"] == 2

    # Alpha was never checked for booking: not a match, a possible one.
    counts, _ = asyncio.run(verdicts(["RUNNING_GOOGLE_ADS"], ["NO_ONLINE_BOOKING"]))
    assert counts == {"match": 0, "possible": 2, "no_match": 1}
    _, possible = asyncio.run(verdicts(["RUNNING_GOOGLE_ADS"], ["NO_ONLINE_BOOKING"],
                                       ("possible",)))
    assert [c["name"] for c in possible] == ["Alpha Roofing", "Beta Roofing"]
    assert {c["verdict"] for c in possible} == {"possible"}


async def _seed_large_cohort(db_path, n):
    state = StateManager(str(db_path))
    await state.init_db()
    from harvey.signals import sync_vocabulary
    await sync_vocabulary(state)
    from harvey.models import Company

    ids = [await state.add_company(Company(name=f"Co {i:03d}", domain=f"co{i:03d}.com"))
           for i in range(n)]
    await state.add_observations([
        {"company_id": cid, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1}
        for cid in ids
    ])


def test_cohort_pages_through_every_match(client):
    """Past the first page used to be unreachable: 147 matches showed 50."""
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 120))
    state = StateManager(str(client.db_path))

    seen = []
    for offset in (0, 50, 100):
        size, page = asyncio.run(state.cohort_companies(["RUNNING_GOOGLE_ADS"], [],
                                                        offset=offset, limit=50))
        assert size == 120
        seen += [c["name"] for c in page]

    assert len(seen) == 120
    assert len(set(seen)) == 120, "pages must not overlap"
    assert seen == sorted(seen), "pages are in a stable order"


def test_cohort_with_no_requirements_is_empty_not_everything(client):
    """An empty cohort must never mean 'select all' — that would silently
    turn a mis-click into a campaign against the entire database."""
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 3))
    state = StateManager(str(client.db_path))
    assert asyncio.run(state.cohort_verdicts([], [])) == (
        {"match": 0, "possible": 0, "no_match": 0}, [])


def test_the_cohort_endpoints_are_gone(client):
    """The cohort builder went with the Signals tab: the filters (Contacts,
    Companies) and the agent read the same engine."""
    assert client.post("/api/cohort", json={"require": ["SERP_RANK"]}).status_code in (404, 405)
    assert client.get("/api/cohort.csv", params={"require": "SERP_RANK"}).status_code in (
        404, 405)


def test_signal_counts_surface_in_the_review_ui(client):
    import asyncio
    asyncio.run(_seed_observations(client.db_path))
    data = client.get("/api/signals").json()
    agency = next(s for g in data["groups"] for s in g["signals"]
                  if s["code"] == "INCUMBENT_AGENCY")
    assert agency["companies"] == 2


# ── Today ──


def test_today_asks_nothing_about_signals(client):
    """Nothing waits for a signal to be confirmed: Today names no such item,
    and an empty install is pointed at finding businesses."""
    data = client.get("/api/today").json()
    keys = [i["key"] for i in data["items"]]
    assert "signals" not in keys and "signals-none" not in keys
    assert "discover" in keys
    assert all(i.get("tab") != "signals" for i in data["items"])
    assert data["stats"]["signals_on"] == len(DECLARED)


def test_today_stats_are_present_even_on_an_empty_install(client):
    stats = client.get("/api/today").json()["stats"]
    for key in ("companies", "prospects", "signals_on",
                "outbox_pending", "open_conversations"):
        assert key in stats


# ── Static assets ──


def test_dashboard_serves_its_files(client):
    assert client.get("/").status_code == 200
    assert "app.css" in client.get("/").text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize("filename, url, media_type", [
    ("index.html", "/", "text/html"),
    ("app.css", "/static/app.css", "text/css"),
    ("app.js", "/static/app.js", "text/javascript"),
    ("icon.svg", "/static/icon.svg", "image/svg+xml"),
])
def test_dashboard_text_is_utf8_with_windows_default_encoding(
    client, monkeypatch, tmp_path, filename, url, media_type,
):
    """UTF-8 assets must survive a CP1252 host without crashes or mojibake."""
    content = "/* ═ café — 日本語 */"
    (tmp_path / filename).write_bytes(content.encode("utf-8"))
    monkeypatch.setattr(dash, "WEB_DIR", tmp_path)
    read_text = Path.read_text

    def windows_read_text(path, encoding=None, errors=None, **kwargs):
        return read_text(path, encoding=encoding or "cp1252", errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", windows_read_text)
    response = client.get(url)
    assert response.status_code == 200
    assert response.text == content
    assert response.content == content.encode("utf-8")
    assert response.headers["content-type"].startswith(media_type)


def test_static_route_refuses_to_escape_the_web_directory(client):
    assert client.get("/static/..%2f..%2fharvey.yaml").status_code == 404
    assert client.get("/static/nope.css").status_code == 404


# ── Catalog invariants the UI and CLI both rely on ──


def test_cost_notes_are_machine_classifiable():
    """`harvey signals` and the agent read the cost note. A signal that
    costs credits must not read as free: its spend is held by its run's cap,
    and the note is what tells a person or the agent it has one.
    """
    paid = {"SERP_RANK", "MAPS_RANK", "EMAIL_STATUS", "EMAIL_PATTERN", "FOUND_IN_SERP"}
    for sig in SIGNAL_CATALOG:
        free = sig["cost_note"].lower().startswith(("free", "included"))
        if sig["code"] in paid:
            continue
        assert free, f"{sig['code']} has an unclassifiable cost note: {sig['cost_note']!r}"

    serp = next(s for s in SIGNAL_CATALOG if s["code"] == "SERP_RANK")
    assert not serp["cost_note"].lower().startswith("free")
    email = next(s for s in SIGNAL_CATALOG if s["code"] == "EMAIL_STATUS")
    assert not email["cost_note"].lower().startswith("free")


def test_every_signal_shows_under_its_module_with_when_it_is_known(client):
    data = client.get("/api/signals").json()
    shown = {s["code"]: (g["key"], s["when_known"]) for g in data["groups"] for s in g["signals"]}
    assert set(shown) == set(DECLARED)
    assert shown["SERP_RANK"] == ("dataforseo", "search")
    assert shown["TURNOVER"] == ("mf_financials", "source")
    assert shown["ACTIVITY_CODE"] == ("onrc", "source")
    assert shown["TECH_STACK"] == ("web_marketing", "enrich")


def test_signals_api_marks_numeric_signals_for_the_range_filters(client):
    data = client.get("/api/signals").json()
    sigs = {s["code"]: s for g in data["groups"] for s in g["signals"]}
    assert sigs["SERP_RANK"]["value_type"] == "num"
