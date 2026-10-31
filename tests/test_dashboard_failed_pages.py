"""Dashboard pages when their data fails to load, and text that must not
contradict what the page shows.

Each check runs the real app.js functions under node against a small fake
DOM, with /api reads stubbed: a 500 must never read as "can't reach the
server", a page whose data failed must not offer a blank form to save, and
"nothing yet" must not sit next to runs listed on the same page. All data is
fictional.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import harvey.dashboard as dash

WEB = Path(dash.__file__).resolve().parent / "web"
ROOT = Path(dash.__file__).resolve().parent


def _fn(src, name):
    """One top-level function from app.js, `async` kept."""
    m = re.search(r"^(async )?function " + re.escape(name) + r"\(", src, re.M)
    assert m, name
    depth, i = 0, src.index("{", m.start())
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[m.start():i]


# A fake DOM just big enough for the loaders under test.
FAKE_DOM = r"""
const els = {};
function el(id) {
  if (!els[id]) {
    const set = new Set();
    els[id] = {id: id, innerHTML: '', textContent: '', className: '', style: {}, value: '',
      checked: false, placeholder: '',
      classList: {toggle(c, on) { if (on) set.add(c); else set.delete(c); }, contains(c) { return set.has(c); }},
      querySelector() { return null; }};
  }
  return els[id];
}
global.document = {getElementById: el, querySelector: () => null, querySelectorAll: () => [], documentElement: {}};
var _lastFail = null, _locked = false, _modules = {email: false}, currentTab = 'overview';
const toasts = [];
function showToast(m) { toasts.push(m); }
let API = {}, FAIL = 500;
async function api(path) {
  const key = path.split('?')[0];
  if (Object.prototype.hasOwnProperty.call(API, key) && API[key] !== null) return API[key];
  _lastFail = FAIL;
  return null;
}
"""

APP_FUNCS = ("escHtml", "jsq", "own", "icon", "emptyState", "failedOnServer", "offlineState",
             "offlineLine", "markLoadFailed", "loadSettings", "applyEnvKeys", "saveEnv",
             "loadHarveyStatus",
             "loadLiveStatus", "loadActivity", "loadTodayActivity", "renderRunsTable", "runName",
             "loadOverview", "agentName")


def _run(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    funcs = "\n".join(_fn(app, n) for n in APP_FUNCS)
    agent_icons = re.search(r"^const AGENT_ICONS = \{.*?\};", app, re.M | re.S).group(0)
    stubs = ("function badge(s) { return '<span class=\"badge\">' + s + '</span>'; }\n"
             "function sourceLabel(s) { return String(s); }\n"
             "function relTime() { return 'now'; }\n"
             "function actionText(a) { return String(a.action_type); }\n"
             "function renderOverviewHints() {} function navCount() {}\n"
             "function renderOverviewSending() {} function renderOverviewSetup() {}\n")
    script = (
        "global.localStorage = {getItem: () => null, setItem() {}};"
        f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(WEB / 'i18n.js'))}, 'utf8'));"
        f"require('vm').runInThisContext({json.dumps(FAKE_DOM + stubs + agent_icons + chr(10) + funcs)});"
        "(async () => { setLang('en'); const out = {};"
        + body +
        " process.stdout.write(JSON.stringify(out)); })().catch(e => { console.error(e); process.exit(1); });")
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_settings_error_shows_the_error_card_and_refuses_to_save():
    out = _run(
        "API = {}; FAIL = 500; await loadSettings();"
        "out.err = el('settings-error').innerHTML;"
        "out.failed = el('settings').classList.contains('load-failed');"
        "await saveEnv({REOON_API_KEY: 'x'}, 'saved'); out.toasts = toasts.slice();"
        "API = {'/api/settings': {provider: 'smtp'}}; await loadSettings();"
        "out.errAfter = el('settings-error').innerHTML;"
        "out.failedAfter = el('settings').classList.contains('load-failed');")
    assert "HTTP 500" in out["err"] and 'class="empty boxed"' in out["err"]
    assert out["failed"] is True
    assert out["toasts"] == ["Settings couldn’t be read, so nothing is saved. Reload the page."]
    assert out["errAfter"] == "" and out["failedAfter"] is False


def test_settings_form_is_hidden_while_its_data_failed():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    settings = html[html.index('<div id="settings" class="section">'):html.index("<!-- Controls -->")]
    assert '<div id="settings-error"></div>' in settings
    assert 'class="needs-data" id="settings-body"' in settings
    # Every form and save button sits inside the hidden body.
    body = settings[settings.index('id="settings-body"'):]
    assert settings.count("<button") == body.count("<button")
    assert settings.count("<input") == body.count("<input")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.section\.load-failed \.needs-data\s*\{\s*display:\s*none", css)


def test_discover_and_signals_hide_their_forms_when_the_page_failed():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    disc = html[html.index('<div id="discover" class="section">'):html.index("<!-- Companies -->")]
    sig = html[html.index('<div id="signals" class="section">'):html.index("<!-- Discover")]
    assert 'estimateDiscovery()' in disc
    # The estimate/run form and the cohort builder are marked needs-data.
    form = disc[disc.index('data-i18n="disc.run"') - 60:]
    assert form.count("needs-data") >= 2 and form.index("needs-data") < form.index("estimateDiscovery()")
    assert sig.count("needs-data") == 2 and sig.index("needs-data") < sig.index('id="cohort-builder"')
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "markLoadFailed('signals'" in _fn(app, "loadSignals")
    assert "markLoadFailed('discover'" in _fn(app, "loadDiscoverProviders")


@pytest.mark.parametrize("fail, header, meta", [
    (500, "Server error", "returned an error (HTTP 500)"),
    ("network", "Offline", "can’t reach the server"),
])
def test_status_pill_and_control_card_tell_server_errors_from_no_connection(fail, header, meta):
    out = _run(
        f"API = {{}}; FAIL = {json.dumps(fail)}; await loadHarveyStatus(); await loadLiveStatus();"
        "out.header = el('header-status-text').textContent; out.label = el('control-label').textContent;"
        "out.meta = el('control-meta').textContent; out.live = el('live-status').innerHTML;")
    assert out["header"] == header and out["label"] == header
    assert meta in out["meta"]
    if fail == 500:
        assert "HTTP 500" in out["live"] and "dashboard window" not in out["live"]
    else:
        assert "dashboard window" in out["live"]


RUNS = [{"stage": "import", "provider": "onrc", "status": "completed", "records": 2421,
         "cost_usd": 0, "started_at": "2026-01-01T10:00:00"}]


def test_activity_log_empty_message_does_not_deny_the_runs_above_it():
    out = _run(
        f"API = {{'/api/activity': [], '/api/runs': {json.dumps(RUNS)}}}; await loadActivity();"
        "out.withRuns = el('activity-list').innerHTML;"
        "API = {'/api/activity': [], '/api/runs': []}; await loadActivity();"
        "out.noRuns = el('activity-list').innerHTML;")
    assert "No actions in the log yet" in out["withRuns"] and "runs above" in out["withRuns"]
    assert "hasn’t taken any actions" not in out["withRuns"]
    assert "No activity yet" in out["noRuns"]


def test_today_recent_activity_points_at_runs_instead_of_nothing_yet():
    out = _run(
        f"API = {{'/api/activity': [{{agent: 'main', action_type: 'heartbeat'}}], '/api/runs': {json.dumps(RUNS)}}};"
        "await loadTodayActivity(); out.withRuns = el('today-activity').innerHTML;"
        "API = {'/api/activity': [], '/api/runs': []}; await loadTodayActivity();"
        "out.noRuns = el('today-activity').innerHTML;")
    assert "under Runs" in out["withRuns"] and "Nothing yet" not in out["withRuns"]
    assert "Nothing yet" in out["noRuns"]


def test_runs_table_is_labelled_for_the_phone_card_layout():
    out = _run(f"const box = el('runs'); renderRunsTable(box, {json.dumps(RUNS)}, 6); out.html = box.innerHTML;")
    assert 'class="nowrap compact runs-table"' in out["html"]
    assert 'data-label="Records"' in out["html"] and 'data-label="Cost"' in out["html"]
    css = (WEB / "app.css").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 700px)"):]
    assert re.search(r"table\.runs-table thead\s*\{\s*display:\s*none", narrow)
    assert re.search(r"table\.runs-table tr\s*\{[^}]*display:\s*grid", narrow)


def test_overview_share_under_one_percent_is_not_zero():
    out = _run(
        "API = {'/api/overview': {companies: 2421, with_phone: 0, with_email: 10, contacts: 1, people: 2},"
        " '/api/runs': [], '/api/setup-status': {checks: []}};"
        "await loadOverview(); out.html = el('overview-stats').innerHTML;")
    assert "&lt;1% of companies" in out["html"]
    widths = re.findall(r'<span style="width:([\d.]+)%">', out["html"])
    assert widths == ["0", "1.5"]          # phone: none; email: a visible sliver


def test_stored_english_role_labels_are_translated_for_display():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    # The fixed labels the collectors write must stay in the map.
    assert '"General inbox"' in (ROOT / "agents" / "audience.py").read_text(encoding="utf-8")
    assert '("founder", "Founder")' in (ROOT / "collectors" / "people.py").read_text(encoding="utf-8")
    script = (
        "global.localStorage = {getItem: () => null, setItem() {}};"
        f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(WEB / 'i18n.js'))}, 'utf8'));"
        "setLang('ro'); const ro = ['General inbox', 'Owner', 'Founder', 'Co-founder', 'Partner', 'general inbox ',"
        " 'Medic primar', 'constructor', '__proto__', null].map(x => roleText(x));"
        "setLang('en'); const en = roleText('General inbox');"
        "process.stdout.write(JSON.stringify({ro: ro, en: en}));")
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    encoding="utf-8", check=True, timeout=30).stdout)
    assert out["ro"][:6] == ["Adresă generală", "Proprietar", "Fondator", "Cofondator", "Asociat",
                             "Adresă generală"]
    assert out["ro"][6:] == ["Medic primar", "constructor", "__proto__", ""]   # anything else as stored
    assert out["en"] == "General inbox"
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"escHtml\((?:p|e)\.title\)", app)   # every role render goes through roleText


def test_counts_use_the_text_face_not_mono():
    """In Geist Mono the ro-RO thousands dot takes a full cell: "2. 421"."""
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert re.search(r"--num:\s*var\(--sans\)", css)
    for sel in (".stat-value", ".kpi-value", ".legend-val", ".cohort-size", ".pager-range"):
        rule = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css).group(1)
        assert "var(--mono)" not in rule, sel
    assert re.search(r"body\s*\{[^}]*font-variant-numeric:\s*tabular-nums", css)
