"""Three-valued cohorts: every condition is true, false or not checked yet.

"Excluding running ads" used to keep every company nobody had checked, so a
list of "no ads" businesses was mostly businesses never looked at. Now an
exclusion is true only on a checked "no", and each company is a match, a
possible (nothing rules it out, something isn't checked) or no match.
"""

import asyncio
import csv
import io
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.signals as signals
from harvey.models import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "v.db"))
    await sm.init_db()
    await seed_signal_catalog(sm)
    return sm


async def _seed(state):
    """Ads: yes / no / never checked; plus one weak (low-confidence) yes."""
    ids = {}
    for name in ("Alfa", "Beta", "Gama", "Delta"):
        ids[name] = await state.add_company(
            Company(name=f"{name} EXEMPLU SRL", domain=f"{name.lower()}.example.com"))
    await state.add_observations([
        {"company_id": ids["Alfa"], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
        {"company_id": ids["Beta"], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 0},
        {"company_id": ids["Delta"], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1,
         "confidence": 0.3},
    ])
    return ids


def _names(rows):
    return [(r["name"].split()[0], r["verdict"]) for r in rows]


@pytest.mark.asyncio
async def test_require_reads_true_false_and_unknown(state):
    await _seed(state)
    counts, rows = await state.cohort_verdicts(["RUNNING_GOOGLE_ADS"], [],
                                               verdicts=("match", "possible", "no_match"))
    assert counts == {"match": 2, "possible": 1, "no_match": 1}
    assert _names(rows) == [("Alfa", "match"), ("Beta", "no_match"),
                            ("Delta", "match"), ("Gama", "possible")]


@pytest.mark.asyncio
async def test_exclude_is_true_only_on_a_checked_no(state):
    await _seed(state)
    counts, rows = await state.cohort_verdicts([], ["RUNNING_GOOGLE_ADS"],
                                               verdicts=("match",))
    assert counts == {"match": 1, "possible": 1, "no_match": 2}
    assert _names(rows) == [("Beta", "match")]


@pytest.mark.asyncio
async def test_a_weak_reading_is_unknown_under_a_confidence_floor(state):
    await _seed(state)
    counts, rows = await state.cohort_verdicts(
        ["RUNNING_GOOGLE_ADS"], [], verdicts=("possible",), min_confidence=0.5)
    assert counts == {"match": 1, "possible": 2, "no_match": 1}
    assert _names(rows) == [("Delta", "possible"), ("Gama", "possible")]
    # Excluding it: the weak yes neither rules Delta out nor lets it in.
    counts, _ = await state.cohort_verdicts(
        [], ["RUNNING_GOOGLE_ADS"], verdicts=(), min_confidence=0.5)
    assert counts == {"match": 1, "possible": 2, "no_match": 1}


@pytest.mark.asyncio
async def test_a_newer_reading_replaces_the_old_one(state):
    ids = await _seed(state)
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=ids["Alfa"],
                                collector="profile", value_num=0,
                                observed_at="2999-01-01 00:00:00")
    _, rows = await state.cohort_verdicts([], ["RUNNING_GOOGLE_ADS"])
    assert _names(rows) == [("Alfa", "match"), ("Beta", "match")]


@pytest.mark.asyncio
async def test_any_false_condition_wins_over_unknown(state):
    ids = await _seed(state)
    await state.add_observation("NO_ONLINE_BOOKING", company_id=ids["Beta"],
                                collector="profile", value_num=1)
    # Beta: no ads (false for require), booking gap (true). Gama: nothing.
    counts, rows = await state.cohort_verdicts(
        ["RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING"], [],
        verdicts=("match", "possible", "no_match"))
    assert dict(_names(rows)) == {"Alfa": "possible", "Beta": "no_match",
                                  "Delta": "possible", "Gama": "possible"}
    assert sum(counts.values()) == 4, "every company gets a verdict"


@pytest.mark.asyncio
async def test_no_condition_is_an_empty_cohort(state):
    await _seed(state)
    counts, rows = await state.cohort_verdicts([], [], verdicts=("possible",))
    assert counts == {"match": 0, "possible": 0, "no_match": 0} and rows == []


@pytest.mark.asyncio
async def test_a_range_is_false_only_on_a_reading_out_of_range(state):
    ids = await _seed(state)
    await state.add_observation("REVIEW_RATING", company_id=ids["Alfa"],
                                collector="discover", value_num=4.8)
    await state.add_observation("REVIEW_RATING", company_id=ids["Beta"],
                                collector="discover", value_num=3.1)
    counts, rows = await state.cohort_verdicts(
        [], [], ranges=[{"code": "REVIEW_RATING", "min": 4}],
        verdicts=("match", "possible", "no_match"))
    assert dict(_names(rows)) == {"Alfa": "match", "Beta": "no_match",
                                  "Delta": "possible", "Gama": "possible"}


@pytest.mark.asyncio
async def test_a_rank_search_that_no_longer_finds_it_is_a_checked_no(state):
    cid = await state.add_company(Company(name="EXEMPLU SRL", domain="example.com"))
    await state.add_observation("IN_LOCAL_PACK", company_id=cid, collector="discover",
                                value_num=2, value_text="dentist Brasov",
                                observed_at="2026-01-01 00:00:00")
    _, rows = await state.cohort_verdicts([], ["IN_LOCAL_PACK"], verdicts=("match", "no_match"))
    assert _names(rows) == [("EXEMPLU", "no_match")]
    await state.add_observation("IN_LOCAL_PACK", company_id=cid, collector="discover",
                                value_num=0, value_text="dentist Brasov",
                                observed_at="2026-02-01 00:00:00")
    _, rows = await state.cohort_verdicts([], ["IN_LOCAL_PACK"], verdicts=("match", "no_match"))
    assert _names(rows) == [("EXEMPLU", "match")]


@pytest.mark.asyncio
async def test_absence_false_reads_a_missing_row_as_no(state, monkeypatch):
    """For a collector that looks at every company and writes only positives."""
    await _seed(state)
    catalog = [dict(s, absence="false") if s["code"] == "RUNNING_GOOGLE_ADS" else s
               for s in SIGNAL_CATALOG]
    monkeypatch.setattr(signals, "SIGNAL_CATALOG", catalog)
    counts, rows = await state.cohort_verdicts([], ["RUNNING_GOOGLE_ADS"],
                                               verdicts=("match",))
    assert counts == {"match": 2, "possible": 0, "no_match": 2}
    assert _names(rows) == [("Beta", "match"), ("Gama", "match")]
    states = await state.signal_states(["RUNNING_GOOGLE_ADS"])
    assert states["RUNNING_GOOGLE_ADS"] == {"true": 2, "false": 2, "unknown": 0}


@pytest.mark.asyncio
async def test_signal_states_count_every_company_once(state):
    await _seed(state)
    states = await state.signal_states(["RUNNING_GOOGLE_ADS", "TECH_STACK"])
    assert states["RUNNING_GOOGLE_ADS"] == {"true": 2, "false": 1, "unknown": 1}
    assert states["TECH_STACK"] == {"true": 0, "false": 0, "unknown": 4}


def test_the_catalog_says_how_absence_reads():
    assert all(s.get("absence", "unknown") in signals.ABSENCE_VALUES for s in SIGNAL_CATALOG)
    # No collector today visits every company, so none reads absence as "no".
    assert {signals.absence(s["code"]) for s in SIGNAL_CATALOG} == {"unknown"}
    assert signals.absence("NOT_A_SIGNAL") == "unknown"


@pytest.mark.asyncio
async def test_old_cohort_helpers_return_matches_only(state):
    ids = await _seed(state)
    assert sorted(await state.cohort(["RUNNING_GOOGLE_ADS"])) == sorted(
        [ids["Alfa"], ids["Delta"]])
    size, rows = await state.cohort_companies([], ["RUNNING_GOOGLE_ADS"])
    assert size == 1 and _names(rows) == [("Beta", "match")]


# ── Dashboard ──


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


def _seed_client(client):
    async def go():
        sm = StateManager(str(client.db_path))
        await sm.init_db()
        await seed_signal_catalog(sm)
        await _seed(sm)
    asyncio.run(go())


def test_exclusion_only_cohort_with_three_counts(client):
    _seed_client(client)
    body = client.post("/api/cohort", json={"exclude": ["RUNNING_GOOGLE_ADS"]}).json()
    assert body["size"] == 1 and body["verdict"] == "match"
    assert body["counts"] == {"match": 1, "possible": 1, "no_match": 2}
    assert [(c["name"], c["verdict"]) for c in body["companies"]] == [
        ("Beta EXEMPLU SRL", "match")]
    possible = client.post("/api/cohort", json={"exclude": ["RUNNING_GOOGLE_ADS"],
                                                "verdict": "possible"}).json()
    assert possible["size"] == 1 and possible["total"] == 1
    assert [c["name"] for c in possible["companies"]] == ["Gama EXEMPLU SRL"]


@pytest.mark.parametrize("verdict", ["maybe", ["match", 3], 7])
def test_a_bad_verdict_is_a_client_error(client, verdict):
    assert client.post("/api/cohort", json={"require": ["RUNNING_GOOGLE_ADS"],
                                            "verdict": verdict}).status_code == 400


def test_cohort_csv_carries_the_verdict(client):
    _seed_client(client)
    r = client.get("/api/cohort.csv", params={"require": "RUNNING_GOOGLE_ADS",
                                              "verdict": "all"})
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][-1] == "verdict"
    assert sorted((row[0].split()[0], row[-1]) for row in rows[1:]) == [
        ("Alfa", "match"), ("Beta", "no_match"), ("Delta", "match"), ("Gama", "possible")]
    only = client.get("/api/cohort.csv", params={"exclude": "RUNNING_GOOGLE_ADS"})
    assert [row[0] for row in list(csv.reader(io.StringIO(only.text)))[1:]] == [
        "Beta EXEMPLU SRL"]


def test_signals_api_reports_true_false_unknown(client):
    _seed_client(client)
    data = client.get("/api/signals").json()
    ads = next(s for g in data["groups"] for s in g["signals"]
               if s["code"] == "RUNNING_GOOGLE_ADS")
    assert ads["states"] == {"true": 2, "false": 1, "unknown": 1}
    assert ads["absence"] == "unknown"
    assert ads["companies"] == 2


# ── Discovery records the listing's "no" ──


def test_a_listing_records_website_and_claim_both_ways():
    from harvey.collectors.discover import Business, _observations
    confirmed = {"NO_WEBSITE", "UNCLAIMED_LISTING"}

    def rows(**kw):
        biz = Business(name="EXEMPLU SRL", **kw)
        return {r["signal_code"]: r["value_num"]
                for r in _observations(biz, "c1", confirmed, "run")}
    assert rows(has_website=True, is_claimed=True) == {"NO_WEBSITE": 0.0,
                                                       "UNCLAIMED_LISTING": 0.0}
    assert rows(has_website=False, is_claimed=False) == {"NO_WEBSITE": 1.0,
                                                         "UNCLAIMED_LISTING": 1.0}
    assert rows() == {}


# ── The Signals page ──


def _node_page(script):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    app = Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js"
    out = subprocess.run([node, "-e", script, str(app), str(app.parent / "i18n.js")],
                         text=True, encoding="utf-8", capture_output=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("lang", ["ro", "en"])
def test_cohort_result_shows_three_tabs_and_what_possible_means(lang):
    data = _node_page(r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
vm.runInThisContext("LANG = '%s';");
const el = {innerHTML: ''}, bodies = [];
global.document = {getElementById: () => el};
global.escHtml = s => String(s);
global.api = async (path, opts) => { bodies.push(JSON.parse(opts.body));
  return {size: 1, total: 5, verdict: 'possible', offset: 0,
          counts: {match: 1, possible: 5, no_match: 2},
          companies: [{name: 'EXEMPLU SRL', domain: 'example.com', verdict: 'possible'}]}; };
vm.runInThisContext("const _cohort = {require: new Set(), exclude: new Set(['RUNNING_GOOGLE_ADS']), ranges: {}, verdict: 'possible'};" +
 "let _cohortOffset = 0, _cohortGeneration = 0; const COHORT_PAGE = 50;" +
 app.slice(app.indexOf('async function runCohort('), app.indexOf('async function exportCohort(')));
(async () => { await runCohort();
  console.log(JSON.stringify({html: el.innerHTML, body: bodies[0],
    possible: t('cohort.explain.possible'), tab: t('cohort.tab.possible'),
    hasEn: hasKey('en', 'cohort.explain.possible'), hasRo: hasKey('ro', 'cohort.explain.possible')})); })()
  .catch(e => {console.error(e); process.exit(1);});
''' % lang)
    assert data["body"]["verdict"] == "possible" and data["body"]["exclude"] == ["RUNNING_GOOGLE_ADS"]
    assert data["hasEn"] and data["hasRo"]
    html = data["html"]
    assert html.count("setCohortVerdict(") == 3
    assert 'class="on" aria-pressed="true" onclick="setCohortVerdict(\'possible\')' in html
    assert data["possible"] in html and data["tab"] in html
    assert "EXEMPLU SRL" in html


def test_signal_row_shows_yes_no_and_not_checked():
    data = _node_page(r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
vm.runInThisContext("LANG = 'en';");
global.escHtml = s => String(s);
global.jsq = s => JSON.stringify(s);
global.badge = s => '';
vm.runInThisContext(app.slice(app.indexOf('function tServer('), app.indexOf('async function loadSignals(')));
vm.runInThisContext(app.slice(app.indexOf('function sigLabel('), app.indexOf('async function setSignals(')));
const sig = {code: 'RUNNING_GOOGLE_ADS', label: 'Running Google Ads', description: 'd',
             cost_note: 'free', status: 'confirmed', companies: 2,
             states: {true: 2, false: 1, unknown: 7}};
console.log(JSON.stringify({checked: sigRow(sig),
  unchecked: sigRow({...sig, companies: 0, states: {true: 0, false: 0, unknown: 10}})}));
''')
    assert "2 yes · 1 no · 7 not checked" in data["checked"]
    assert "not checked" not in data["unchecked"]
