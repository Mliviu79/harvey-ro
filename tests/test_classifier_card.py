"""The dashboard's classifier pages, under node, in both languages: the
Classifier card (what it is for and when it is asked, each task's profile,
the profiles side by side with what they measured, editing and testing
one, the caps and the DeepInfra key's scopes), the classifier cap of a
Find-contacts run and its report, the Queue's and Today's run reports,
and verifying contacts on the Contacts page. The card is drawn from what
the real API answers (a fresh install, no network, no model). All data is
fictional."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harvey import classify_profiles as profiles
from harvey.state import StateManager
from tests.test_classify_profiles import (LOCAL, _firm_with_site_contacts, _ids, _quick,  # noqa: F401
                                          _state, client, run)

WEB = Path(__file__).resolve().parent.parent / "harvey" / "web"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _js(script: str, *args) -> dict:
    res = subprocess.run([_node(), "-e", script, str(WEB / "app.js"), str(WEB / "i18n.js"), *args],
                         text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr[-3000:]
    return json.loads(res.stdout)


# The helpers app.js takes from its top, as the dashboard has them.
PRELUDE = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
global.localStorage = {getItem: () => process.argv[3], setItem: () => {}};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: '', value: '', id, dataset: {}, style: {}, blur() {}});
global.document = {getElementById: id => els[id] || null, activeElement: null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => escHtml(JSON.stringify(String(s == null ? '' : s)));
global.failMsg = (d, f) => (d && (d.message || d.error)) || f;
global.icon = n => '<i>' + n + '</i>';
global.relTime = s => String(s);
global.badge = s => '<b>' + s + '</b>';
global.own = (o, k) => Object.prototype.hasOwnProperty.call(o, k) ? o[k] : undefined;
global.tServer = function (key, english) {
  if (getLang() === 'en' || !english) return english || '';
  return hasKey('en', key) && t(key, null, 'en') === english ? t(key) : english;
};
global._modules = {email: false};
global.loadPageSize = () => 50;
const toasts = [], posted = [];
global.showToast = (m, kind) => toasts.push([m, kind]);
global.confirm = () => true;
const pick = (from, to) => app.slice(app.indexOf(from), app.indexOf(to));
'''


# ── The Classifier card, from the real API ──


def _card_payloads(client, tmp_path):
    mine = client.post("/api/classify/profiles", json=LOCAL).json()["profile"]
    tested = client.post("/api/classify/profiles", json={
        **LOCAL, "name": "Another local model", "model": "small-7b",
        "request_extra": {"reasoning_effort": "none"}}).json()["profile"]
    assert client.post("/api/classify/use", json={"site": "typesafe-jev",
                                                  "contacts": "typesafe-jev"}).json()["success"]
    s = StateManager(str(tmp_path / "harvey.db"))
    run(profiles.record_test(s, tested["id"], {
        "task": "site", "rows": 30, "asked": 12, "scored": 10, "correct": 9, "false_links": 0,
        "misses": 1, "contacts_right": 7, "contacts_picked": 7, "contacts_expected": 8,
        "latency_ms": {"median": 1200, "p90": 2400}, "cost_usd": 0.0004, "cost_known": True},
        test_set="built-in", job_id="j1"))
    status = client.get("/api/classify").json()
    view = client.get("/api/classify/profiles").json()
    # (No key in this test's environment: as if DeepInfra's were set, for its account box.)
    for p in view["profiles"]:
        if p["key_name"] == "DEEPINFRA_API_KEY":
            p["key_set"] = True
    # (A pause each, as a server would ask: one profile's own, one its account's.)
    held = {"typesafe-jev": {"seconds": 290, "why": "RateLimitError 429", "scope": "profile"},
            mine["id"]: {"seconds": 1800, "why": "AuthenticationError 401", "scope": "account"}}
    for p in view["profiles"]:
        if p["id"] in held:
            p["held"] = held[p["id"]]
    status["held"] = {"typesafe-jev": {**held["typesafe-jev"], "name": "TypeSafe: Jev"}}
    return mine, tested, status, view


CARD = PRELUDE + r'''
const status = JSON.parse(process.argv[4]), view = JSON.parse(process.argv[5]);
el('classify-card');
global.api = async (path, opts) => {
  if (opts && opts.method === 'POST') posted.push([path, opts.body ? JSON.parse(opts.body) : null]);
  if (path === '/api/classify') return status;
  if (path === '/api/classify/profiles') return view;
  if (path.endsWith('/test/estimate')) return {calls: 14, tokens_in: 9800, usd_typical: 0.0011, usd_high: 0.0031,
                                               price_known: true, rows: 30, by_task: {}, note: 'server words'};
  if (path.endsWith('/test')) return {success: true, job_id: 'job-1', batch_id: 'b-1'};
  if (path.startsWith('/api/classify/test/')) return {status: 'done', result: {tasks: {}}};
  if (path.startsWith('/api/classify/profiles/')) return {success: true, profile: {name: 'Copy'}, copied_from: 'typesafe-jev'};
  if (path === '/api/classify/use') return {success: true};
  return {};
};
global.setTimeout = (f) => { f(); return 0; };
vm.runInThisContext(pick('// Settings: the classifier (a small language model)', 'let _finder = null;'));
(async () => {
  await loadClassifyCard();
  const card = els['classify-card'].innerHTML;
  // Editing a built-in: a copy of your own, its decision questions and bars shown.
  el('cls-editor');
  editClsProfile('typesafe-jev');
  const editor = els['cls-editor'].innerHTML;
  el('clse-extra').value = '{"reasoning_effort": "none"}';
  el('clse-q-contacts-bar').value = '0.8';
  el('clse-q-site-needs-own').value = '0.5';
  el('clse-task-site'); els['clse-task-site'].checked = true;
  el('clse-task-contacts'); els['clse-task-contacts'].checked = true;
  await saveClsProfile();
  // A bad extra field is said, nothing sent.
  editClsProfile(view.profiles.find(p => !p.builtin).id);
  el('clse-extra').value = '[1, 2]';
  const before = posted.length;
  await saveClsProfile();
  const refused = posted.length === before;
  // Testing a profile: priced first, then run under the cap typed.
  const mine = view.profiles.find(p => p.name === 'Local test model');
  el('clsp-' + mine.id + '-test');
  openClsTest(mine.id);
  const testBox = els['clsp-' + mine.id + '-test'].innerHTML;
  el('clsp-' + mine.id + '-t-site'); els['clsp-' + mine.id + '-t-site'].checked = true;
  el('clsp-' + mine.id + '-t-est');
  await estimateClsTest(mine.id);
  const est = els['clsp-' + mine.id + '-t-est'].innerHTML;
  el('clsp-' + mine.id + '-t-cap').value = '0.01';
  el('clsp-' + mine.id + '-t-out');
  await runClsTest(mine.id, {disabled: false});
  await setClassifyUse('results', mine.id);
  // DeepInfra: a saved profile's flag from the server; a draft's base URL on its host exactly.
  const di = [clsIsDeepInfra({is_deepinfra: false, base_url: 'https://api.deepinfra.com/v1/openai'}),
              clsIsDeepInfra({base_url: 'https://API.deepinfra.com/v1/openai'}),
              clsIsDeepInfra({base_url: 'https://api.deepinfra.com.example.com/v1'}),
              clsIsDeepInfra({base_url: 'https://other.deepinfra.com/v1'})];
  console.log(JSON.stringify({card, editor, est, testBox, refused, posted, toasts, di}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_the_classifier_card_explains_itself_and_shows_the_profiles_side_by_side(
        client, tmp_path):
    mine, tested, status, view = _card_payloads(client, tmp_path)
    out = {lang: _js(CARD, lang, json.dumps(status), json.dumps(view)) for lang in ("ro", "en")}
    ro, en = out["ro"]["card"], out["en"]["card"]
    # What it is, when it is asked and its rules, in plain words.
    assert "When it is asked" in en and "Când e întrebat" in ro
    assert "Search again on a company whose website a search found" in en
    assert "Verify with the classifier, on Contacts" in en
    assert "likely at most, never confirmed" in en and "cel mult probabil, niciodată confirmat" in ro
    assert "nothing is guessed in its place" in en
    assert "never uses it" in en and "nu îl folosește niciodată" in ro
    # Each task: what it is for, when, off; and the profile answering it.
    assert "The three results whose title best matches" in en
    assert "Se citesc cele trei rezultate" in ro                  # the server's words, translated
    site = en.split('id="cls-use-site"')[1].split('</select>')[0]
    assert 'value="typesafe-jev" selected' in site
    results = en.split('id="cls-use-results"')[1].split('</select>')[0]
    assert "typesafe-jev" not in results                           # it can't answer it
    assert f'value="{mine["id"]}"' in results
    # The profiles, sorted by name, every built-in with what it measured.
    names = [h.split("</h4>")[0] for h in en.split('<div class="cls-profile-head"><h4>')[1:]]
    assert names == sorted(names, key=str.lower) and len(names) == 8
    assert "Website: 32 of 32 right · 0 false links · 0 missed" in en
    assert "Contacts: 53 right of 54 picked, 54 expected" in en
    assert "Website: 28 of 32 right · 0 false links · 4 missed" in en          # the local model
    assert "Measured 2026-11-03 on 200 hand-labelled firms (developers&#39; test, not shipped)" in en \
        or "Measured 2026-11-03 on 200 hand-labelled firms (developers' test, not shipped)" in en
    assert "200 de firme etichetate de mână" in ro
    assert "median 213 ms, p90 528 ms" in en and "for 1,000 website questions" in en
    assert "{&quot;reasoning_effort&quot;:&quot;none&quot;}" in en
    assert "Doar cu gândirea oprită" in ro                         # a built-in's note, translated
    # Yours: not tested until a test run records on it; the tested one's run shown.
    assert "Not tested: Test shows its counts here." in en and "Netestat" in ro
    assert "Test runs: 1" in en and "Website: 9 of 10 right" in en
    assert "Contacts: 7 right of 7 picked, 8 expected" in en
    # Endpoint, model, the key's name and whether it is set, the price.
    assert "https://api.deepinfra.com/v1/openai" in en and "google/gemma-3-27b-it" in en
    assert "DEEPINFRA_API_KEY holds a value in .env." in en
    assert "TYPESAFE_API_KEY holds no value in .env" in en
    assert "DeepInfra&#39;s price list" in en or "DeepInfra's price list" in en
    assert "$0.0420 in, $0.0000 out a 1M tokens" in en
    # Actions: use for a task, test, edit, duplicate, hide or remove; the account box.
    assert en.count("loadClassifyAccount(") == 4                   # the DeepInfra ones, key set
    assert "clsProfileAction(&quot;typesafe-jev&quot;, 'hide')" in en
    assert f"clsProfileAction(&quot;{mine['id']}&quot;, 'remove')" in en
    assert "duplicateClsProfile(" in en and "editClsProfile(" in en and "openClsTest(" in en
    # The caps and the DeepInfra key's scopes, plainly.
    assert 'id="cls-usd_day"' in en and 'id="cls-per_day"' in en
    assert "Add billing:read" in en and "metrics (usage metrics) by hand" in en
    assert "account_scoped_jwt off" in en and "Adaugă de mână billing:read" in ro
    assert "No classifier call this month yet." in en
    # Each profile's own pause, and its account's; the card's status names who waits.
    jev = en.split('id="clsp-typesafe-jev"')[1].split('class="cls-profile"')[0]
    assert "paused for 5 more min, until" in jev and "the other profiles go on (RateLimitError 429)" in jev
    local = en.split(f'id="clsp-{mine["id"]}"')[1].split('class="cls-profile"')[0]
    assert "paused for 30 more min" in local and "every profile on the same account" in local
    budget = en.split('id="cls-budget-row"')[1].split('class="settings-row"')[0]
    assert ": paused for 5 more min" in budget and "30 more min" not in budget
    assert "în pauză încă 5 min" in ro and "toate profilurile de pe același cont" in ro
    assert out["en"]["di"] == [False, True, False, False]
    # No old single setup on the card: caps, tasks and profiles only.
    for gone in ('id="cls-provider"', 'id="cls-base_url"', 'id="cls-model"', 'id="cls-key_name"'):
        assert gone not in en
    # No ranking, no badge, no recommendation.
    for text in (en.lower(), ro.lower()):
        for word in ("recommend", "recomand", "suggest", "sugera", "cel mai bun"):
            assert word not in text, word
    # The editor: a built-in's edit is a copy of your own, with its questions and bars.
    ed = out["en"]["editor"]
    assert "Save keeps your changes as a profile of your own" in ed
    assert 'id="clse-q-site-names"' in ed and 'id="clse-q-contacts-bar"' in ed
    assert 'value="TypeSafe: Jev (copy)"' in ed or "(copy)" in ed
    saved = [b for p, b in out["en"]["posted"] if p == "/api/classify/profiles/typesafe-jev"]
    assert saved, out["en"]["posted"]
    body = saved[0]
    assert body["request_extra"] == {"reasoning_effort": "none"}
    assert body["questions"]["contacts"]["bar"] == 0.8
    assert body["questions"]["site"]["needs"]["own"] == 0.5
    assert body["tasks"] == ["site", "contacts"] and body["kind"] == "decision"
    assert body["name"].endswith("(copy)")
    assert out["en"]["refused"]
    # A test: priced first, then run under the cap typed; the result is on the profile.
    assert ("14 questions on 30 cases, about 9,800 tokens in: estimated $0.0011 typical to $0.0031 high."
            in out["en"]["est"])
    assert "An estimate, not a limit" in out["en"]["est"] and 'value="0.01"' in out["en"]["est"]
    assert "14 întrebări pe 30 cazuri" in out["ro"]["est"] and "E o estimare, nu o limită" in out["ro"]["est"]
    assert "Run the test (14 questions)" in out["en"]["est"]
    for lang, word in (("en", "at most"), ("ro", "cel mult")):
        assert word not in out[lang]["est"]
    ran = [b for p, b in out["en"]["posted"] if p.endswith("/test")]
    assert ran == [{"tasks": ["site"], "max_spend": 0.01}]
    assert ["Test done: its counts are on the profile.", "success"] in out["en"]["toasts"]
    assert ["/api/classify/use", {"results": mine["id"]}] in out["en"]["posted"]


# ── Find contacts: the classifier's cap and the run's report ──


FINDER = PRELUDE + r'''
global.openQueue = () => {};
global.loadFinderStatus = () => {};
el('cl-finder');
let reports = 0;
global.api = async (path, opts) => {
  if (opts && opts.method === 'POST') posted.push([path, opts.body ? JSON.parse(opts.body) : null]);
  if (path === '/api/contacts/find/estimate') return {companies: 3, with_site: 2, to_look_up: 1,
    estimated_cost: 0.004, most: 0.008, searchers: [{provider: 'serper'}],
    checks: {checks: 6, usd_high: 0, refused: ['reoon'], free_left: {reoon: 2}, checks_left: 40, verifiers: ['reoon'], per_company: 2},
    classifier: {calls: 7, usd_typical: 0.004, usd_high: 0.0123, price_known: true, note: 'server words',
                 per_task: {results: {calls: 1}, site: {calls: 5}, contacts: {calls: 1}}}};
  if (path === '/api/contacts/find') return {success: true, queued: 3, batch_id: 'b-9'};
  if (path === '/api/jobs/batches/b-9/report') {
    reports++;
    return {jobs: 3, statuses: reports > 1 ? {done: 3} : {running: 1, queued: 2}, seconds: 42.4, job_seconds: 30,
            searches_usd: 0.004, checks_usd: 0.003, checks_made: 2, checks_refused: 1, services: {reoon: 2}, other_usd: 0, total_usd: 0.0092,
            classify: {calls: 7, errors: 0, tokens_in: 4000, tokens_out: 90, usd: 0.0022, unpriced: 0, refused: reports > 1 ? 1 : 0}};
  }
  return {};
};
global.setTimeout = (f) => { f(); return 0; };
vm.runInThisContext(pick('let _cv = ', '// ── Contacts: same pattern as Companies'));
(async () => {
  await openFinder('cl-finder', {filter: {}, force: false});
  const card = els['cl-finder'].innerHTML;
  // The address checks' line follows the cap typed: none run with 0.
  el('finder-checks-wrap');
  el('finder-cap').value = '0';
  finderCapChanged();
  const nocap = els['finder-checks-wrap'].innerHTML;
  el('finder-cap').value = '0.5';
  finderCapChanged();
  const capped = els['finder-checks-wrap'].innerHTML;
  el('finder-cap').value = '0.01';
  el('finder-ccap').value = '0.02';
  await runFinder({disabled: false});
  for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));
  const done = els['cl-finder'].innerHTML;
  // A blank classifier cap: none of the run's own.
  await openFinder('cl-finder', {filter: {}, force: false});
  el('finder-ccap').value = '';
  reports = 5;
  await runFinder({disabled: false});
  console.log(JSON.stringify({card, done, posted, nocap, capped}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_find_contacts_takes_a_classifier_cap_and_shows_the_runs_report():
    out = {lang: _js(FINDER, lang) for lang in ("ro", "en")}
    en, ro = out["en"], out["ro"]
    assert 'id="finder-ccap"' in en["card"] and 'value="0.02"' in en["card"]
    assert ("Classifier: 7 questions (Search results 1, Website 5, Contacts on the site 1), "
            "estimated $0.0040 typical to $0.0123 high.") in en["card"]
    assert "Clasificatorul: 7 întrebări" in ro["card"] and "(obișnuit)" in ro["card"]
    assert "the call is refused if it may not fit" in en["card"] and "server words" not in en["card"]
    # Address checks: as many as two a company, free while the allowance lasts, the
    # one with no price named: the run refuses it.
    assert "as many as 6, 2 a company (40 left this month" in en["card"]
    assert "reoon 2 free left" in en["card"]
    assert "reoon: no price past the free allowance, so the run refuses" in en["card"]
    assert "fără preț peste cele gratuite" in ro["card"]
    # With a cap of 0 no check runs: one short line instead.
    assert en["nocap"] == ('<p class="muted small" id="finder-checks">Addresses are checked '
                           'only with a cap above 0.</p>')
    assert "doar cu un plafon peste 0" in ro["nocap"] and "as many as 6" in en["capped"]
    for lang, word in (("en", "at most"), ("ro", "cel mult")):
        assert word not in out[lang]["card"]
    sent = [b for p, b in en["posted"] if p == "/api/contacts/find"]
    assert sent[0] == {"filter": {}, "force": False, "max_spend": 0.01, "classify_max_spend": 0.02}
    assert "classify_max_spend" not in sent[1]
    # The report, once its jobs are done: time and cost split.
    assert "Finished" in en["done"] and "Gata" in ro["done"]
    assert "Jobs: 3 (3 done)" in en["done"]
    assert "time 42 s (first start to last finish), 30 s of work" in en["done"]
    assert "searches $0.0040" in en["done"]
    assert "2 address checks $0.0030 (reoon 2) (1 refused: a month&#39;s cap" in en["done"] or \
        "2 address checks $0.0030 (reoon 2) (1 refused: a month's cap" in en["done"]
    assert "classifier 7 calls, 4,090 tokens, $0.0022" in en["done"] and "total $0.0092" in en["done"]
    assert "The classifier&#39;s money ran out for 1 jobs" in en["done"] or \
        "The classifier's money ran out for 1 jobs" in en["done"]
    assert "clasificator 7 apeluri" in ro["done"] and "verificări de adrese" in ro["done"]


# ── The Queue's and Today's reports ──


QUEUE = PRELUDE + r'''
global.navCount = () => {}; global.emptyState = () => 'E'; global.offlineState = () => 'O';
const report = {jobs: 2, statuses: {done: 1, paused: 1}, seconds: 400, job_seconds: 380, searches_usd: 0.01,
  checks_usd: 0, checks_made: 0, other_usd: 0.005, total_usd: 0.02,
  classify: {calls: 12, errors: 1, tokens_in: 7000, tokens_out: 200, usd: 0.005, unpriced: 2, refused: 0}};
global.api = async (path, opts) => {
  if (opts && opts.method === 'POST') posted.push([path, opts.body ? JSON.parse(opts.body) : null]);
  if (path === '/api/jobs?batch=b1') return {lanes: [{lane: 'classify', name: 'Classifier', paused: '', counts: {},
      running: [], waiting: [], failed: [], paused_jobs: [],
      done: [{id: 'j1', kind: 'verify_contacts', status: 'done', progress: {classify: {calls: 4, usd: 0.0011}},
              result: {ms: 5300, spend: {checks_made: 2, checks: 0.001, checks_refused: 0}},
              label: 'EXEMPLU SRL', actual_cost: 0.0011}]}],
    running: 0, waiting: 0, worker: {pid: 1}, batch: {label: 'Find contacts', max_spend: 1, params: {classify_max_spend: 0.5}},
    report: report};
  if (path === '/api/jobs/batches?limit=5') return {runs: [{id: 'b1', kind: 'find_contacts', label: 'Find contacts',
    created_at: '2026-11-03 10:00:00', report: report}]};
  return {success: true, resumed: 1};
};
vm.runInThisContext(pick('const JOB_TONE', '// ── Init & live refresh ──'));
vm.runInThisContext(pick('// A run\'s report in words', 'function runActive('));
vm.runInThisContext(pick('// Today: the newest runs of jobs', '// ── Setup checklist'));
['queue-list', 'queue-batch', 'today-jobruns', 'today-jobruns-block'].forEach(el);
(async () => {
  _queueBatch = 'b1';
  await loadQueue();
  el('queue-ccap').value = '0.75';
  await raiseCap('b1', true);
  await loadJobRuns();
  console.log(JSON.stringify({head: els['queue-batch'].innerHTML, list: els['queue-list'].innerHTML,
                              today: els['today-jobruns'].innerHTML, posted}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_the_queue_and_today_show_each_runs_time_and_cost_split():
    out = {lang: _js(QUEUE, lang) for lang in ("ro", "en")}
    en, ro = out["en"], out["ro"]
    assert "time 7 min (first start to last finish), 6 min of work" in en["head"]
    assert "classifier 12 calls, 7,200 tokens, $0.0050 (2 unpriced)" in en["head"]
    assert "other $0.0050" in en["head"] and "total $0.0200" in en["head"]
    assert 'id="queue-ccap"' in en["head"] and 'value="0.5"' in en["head"]
    assert "New classifier cap ($)" in en["head"] and "Plafon nou clasificator" in ro["head"]
    assert ["/api/jobs/batches/b1/cap", {"classify_max_spend": 0.75}] in en["posted"]
    assert "classifier: 4 calls, $0.0011" in en["list"] and "took 5 s" in en["list"]
    assert "address checks: 2, $0.0010" in en["list"] and "verificări de adrese: 2" in ro["list"]
    assert "clasificator: 4 apeluri" in ro["list"]
    assert "Find contacts" in en["today"] and "total $0.0200" in en["today"]
    assert "openQueue(&quot;b1&quot;)" in en["today"] and "Lucrări: 2" in ro["today"]


def test_the_recent_runs_api_gives_each_runs_report(client):
    from harvey.modules import jobs

    import harvey.dashboard as dash

    s = StateManager(str(dash.DB_PATH))

    async def go():
        batch = await jobs.new_batch(s, "find_contacts", "Find contacts")
        await jobs.enqueue_many(s, [dict(kind="find_contacts", lane="contacts",
                                         params={"company_id": "1"}, label="x")], batch_id=batch)
        await jobs.new_batch(s, "discover", "nothing queued")      # (no job: not a run)
        return batch

    batch = run(go())
    runs = client.get("/api/jobs/batches").json()["runs"]
    assert [r["id"] for r in runs] == [batch]
    assert runs[0]["report"]["jobs"] == 1 and runs[0]["report"]["statuses"] == {"queued": 1}


# ── Verifying contacts on the Contacts page ──


def test_the_verdicts_api_gives_each_contacts_standing_verdict(client):
    import harvey.dashboard as dash

    db = dash.DB_PATH

    async def go():
        s = StateManager(str(db))
        cid = await _firm_with_site_contacts(s)
        ids = _ids(db)
        email, phone = ids["office@exemplu-constructii.test"], ids["+40268000111"]
        await s.check_link(cid, email, verdict="no", batch="v1", profile="p", model="m")
        await s.check_link(cid, phone, verdict="unsure", batch="v2", profile="p", model="m")
        await s.undo_contact_checks("v1")
        return email, phone

    email, phone = run(go())
    got = client.get("/api/contacts/verify/verdicts", params={"ids": f"{email},{phone}"}).json()
    assert list(got["verdicts"]) == [str(phone)]                   # the undone one is left out
    assert got["verdicts"][str(phone)]["verdict"] == "unsure"
    assert client.get("/api/contacts/verify/verdicts", params={"ids": "x"}).status_code == 400


VERIFY = PRELUDE + r'''
global.initials = () => 'AE'; global.roleText = s => s;
global.telHref = v => 'tel:' + v; global.mailHref = v => 'mailto:' + v; global.safeHref = v => v;
global.menuHtml = (id, items) => items.map(i => (i.label || '') + (i.disabled ? '[off]' : '') + '<' + (i.action || '') + '>').join('|');
global.loadPageSize = () => 50; global.offlineState = () => 'offline'; global.emptyState = (i, a, b) => a + b;
global.searchPh = () => 'cl.search_ph'; global.statusMeta = s => ({label: s});
global.openQueue = () => {}; global.goTab = () => {}; global.openCompany = () => {};
['cl-rows', 'cl-actions', 'cl-filters', 'cl-search', 'ct-body', 'cl-finder', 'cl-verify', 'cl-addbar'].forEach(el);
const rows = [{id: 7, kind: 'phone', value: '+40268000111', display: '+40 268 000 111', line_type: 'fixed', status: 'valid',
               trust: 'likely', companies: 1, people: 0, sources: ['website'], company: {id: 'c1', name: 'EXEMPLU SRL', location: 'Brașov'}, first_seen: 'x'},
              {id: 8, kind: 'email', value: 'office@exemplu.test', status: 'verified', trust: 'likely', companies: 1, people: 0,
               sources: ['website'], company: {id: 'c1', name: 'EXEMPLU SRL'}, first_seen: 'x'}];
let verdicts = {'7': {verdict: 'no', batch: 'v-1', model: 'jev-latest', p: 0.12, company_id: 'c1'}};
let reports = 0;
global.api = async (path, opts) => {
  if (opts && opts.method === 'POST') posted.push([path, opts.body ? JSON.parse(opts.body) : null]);
  if (path.startsWith('/api/contacts/verify/verdicts')) return {verdicts: verdicts};
  if (path === '/api/contacts/verify/estimate') return {links: 2, companies: 1, calls: 2, tokens_typical: 900, tokens_high: 1400,
    basis: 'this month', usd_typical: 0.0002, usd_high: 0.0004, price_known: true, profile: 'typesafe-jev',
    profile_name: 'TypeSafe: Jev', model: 'jev-latest', off: false, note: 'server words'};
  if (path === '/api/contacts/verify') return {success: true, batch_id: 'v-2', queued: 1, links: 2};
  if (path === '/api/jobs/batches/v-2/report') {
    reports++;
    verdicts = {'7': {verdict: 'no', batch: 'v-2'}, '8': {verdict: 'unsure', batch: 'v-2'}};
    return {jobs: 1, statuses: {done: 1}, seconds: 3, job_seconds: 3, searches_usd: 0, checks_usd: 0, checks_made: 0,
            other_usd: 0, total_usd: 0.0002, classify: {calls: 2, tokens_in: 1800, tokens_out: 20, usd: 0.0002}};
  }
  if (path === '/api/contacts/verify/undo') return {success: true, checks: 2, restored: 1};
  if (path.startsWith('/api/contacts/verify/review')) return {verdict: 'unsure', links: [{company_id: 'c1', company: 'EXEMPLU SRL',
    contact_id: 8, kind: 'email', value: 'office@exemplu.test', verdict: 'unsure', at: 1793700000, model: 'jev-latest'}]};
  if (path === '/api/contacts/7') return {links: [{company_id: 'c1', company: 'EXEMPLU SRL', trust: 'likely', source: 'website'}]};
  if (path === '/api/contacts/7/checks') return {checks: [{company_id: 'c1', verdict: 'no', batch: 'v-1', model: 'jev-latest', at: 1793700000, undone: false}]};
  if (path.startsWith('/api/signals')) return {groups: []};
  return {total: 2, more: false, sources: ['website'], kinds: [{kind: 'phone', label: 'Phone'}, {kind: 'email', label: 'Email'}],
          financials: false, rows: rows};
};
global.setTimeout = (f) => { f(); return 0; };
vm.runInThisContext(pick('const SOURCE_LABELS', 'const AGENT_ICONS'));
vm.runInThisContext(pick('const PAGE_SIZES', 'function savePageSize('));
vm.runInThisContext(pick('let _cv = ', '// ── Contacts: same pattern as Companies'));
(async () => {
  _cv.view = 'contacts';
  await loadContactList();
  for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));
  const list = els['cl-rows'].innerHTML, filters = els['cl-filters'].innerHTML;
  selectAddressPage(true);
  const actions = els['cl-actions'].innerHTML;
  await openVerify();
  const est = els['cl-verify'].innerHTML;
  el('cl-verify-cap').value = '0.01';
  await runVerify({disabled: false});
  const done = els['cl-verify'].innerHTML, after = els['cl-rows'].innerHTML;
  await toggleContactReach(0);
  const panel = els['cl-rows'].innerHTML;
  await undoVerify('v-2');
  setContactCheck('unsure');
  for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({list, filters, actions, est, done, after, panel, review: els['cl-rows'].innerHTML, posted, toasts}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_contacts_are_verified_with_the_classifier_priced_first_and_each_verdict_shown():
    out = {lang: _js(VERIFY, lang) for lang in ("ro", "en")}
    en, ro = out["en"], out["ro"]
    # Each row's standing verdict, a no with the undo of its run.
    assert "classifier: no 12%" in en["list"] and "clasificator: nu 12%" in ro["list"]
    assert "undoVerify(&quot;v-1&quot;)" in en["list"]
    assert "Classifier&#39;s verdict" in en["filters"] or "Classifier's verdict" in en["filters"]
    assert 'value="unsure"' in en["filters"] and "Verdictul clasificatorului" in ro["filters"]
    # Any kind of contact can be ticked; the action counts them.
    assert "Verify with the classifier (2)<openVerify()>" in en["actions"]
    assert "Verifică cu clasificatorul (2)" in ro["actions"]
    # Priced first: the profile, the model, the questions and their cost; then a cap.
    assert "2 links at 1 companies, each asked of the contacts task: TypeSafe: Jev (jev-latest)." in en["est"]
    assert ("2 questions, about 900 tokens each (from this month's calls), 1,400 the largest: "
            "estimated $0.0002 typical to $0.0004 high.") in en["est"].replace("&#39;", "'")
    assert "An estimate, not a limit" in en["est"] and "E o estimare" in ro["est"]
    assert 'value="0.01"' in en["est"]
    assert 'id="cl-verify-cap"' in en["est"] and "a model never confirms" in en["est"]
    sent = [b for p, b in en["posted"] if p == "/api/contacts/verify"]
    assert sent == [{"contact_ids": [7, 8], "max_spend": 0.01}]
    assert "yes 0 · no 1 · unsure 1" in en["done"] and "da 0 · nu 1 · nesigur 1" in ro["done"]
    assert "Undo this run" in en["done"] and "classifier 2 calls" in en["done"]
    assert "classifier: unsure" in en["after"]
    # One contact's panel: its verdicts and its own Verify.
    assert "openVerify([7])" in en["panel"] and "at EXEMPLU SRL" in en["panel"]
    assert ["/api/contacts/verify/undo", {"batch": "v-2"}] in en["posted"]
    assert ["1 links put back.", "success"] in en["toasts"]
    # The unsure ones, for review.
    assert "office@exemplu.test" in en["review"] and "classifier: unsure" in en["review"]
    assert "the other filters don&#39;t apply here" in en["review"] or \
        "the other filters don't apply here" in en["review"]


def test_every_new_classifier_string_is_in_both_languages():
    script = r'''
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const pre = ['cls.', 'cl.v.', 'cl.verify', 'cl.review', 'run.', 'finder.c', 'finder.k', 'est.', 'q.classify', 'q.checks',
             'q.took', 'q.ccap', 'today.jobruns'];
const pick = (L) => Object.keys(I18N[L]).filter(k => pre.some(p => k.startsWith(p))).sort();
console.log(JSON.stringify({ro: pick('ro'), en: pick('en')}));
'''
    keys = _js(script)
    assert keys["ro"] == keys["en"] and len(keys["ro"]) > 200
