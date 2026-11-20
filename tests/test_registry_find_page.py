"""The Companies page's All firms list and its Find contacts card, under
node, in both languages: the scope the page opens on, the registry lines
of the estimate and of a run as it starts (a figure of 0 not said), the
estimate's refusals in the page's words, an estimate still being priced
when the list changes, and where the Overview's companies check leads.
All data is fictional."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


# The helpers app.js takes from its top, as the dashboard has them; the
# language is argv[3], a remembered scope argv[4].
PRELUDE = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
const stored = {'harvey-lang': process.argv[3], 'harvey-companies-scope': process.argv[4] || null};
global.localStorage = {getItem: k => k in stored ? stored[k] : process.argv[3], setItem: (k, v) => { stored[k] = v; }};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: '', value: '', id, dataset: {}, style: {}});
global.document = {getElementById: id => els[id] || null, activeElement: null};
global.escHtml = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => escHtml(JSON.stringify(String(s == null ? '' : s)));
global.failMsg = (d, f) => (d && (d.message || d.error)) || f;
global.icon = n => '<i>' + n + '</i>';
global.showToast = () => {};
global.loadFinderStatus = () => {};
global.loadPageSize = () => 50;
const pick = (from, to) => {
  const a = app.indexOf(from), b = app.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error('not found: ' + from + ' .. ' + to);
  return app.slice(a, b);
};
const calls = [];
'''

FINDER = r'''
vm.runInThisContext(pick('let _finder = null;', '// A run just started, followed where it was started'));
global.watchRun = () => {};
'''


# ── The scope the page opens on ──

SCOPE = PRELUDE + r'''
vm.runInThisContext(pick('let _co = {', '// [sort key, label key]'));
vm.runInThisContext(pick('// The scope this browser chose last', 'function setCompanyScope'));
const answers = JSON.parse(process.argv[5]);
global.api = async path => { calls.push(path); return answers[path.split('?')[0]] ?? null; };
(async () => {
  const first = await companyScope();
  const kept = _co.scope;
  console.log(JSON.stringify({first, kept, calls}));
})().catch(e => {console.error(e); process.exit(1);});
'''

LOADED = {"/api/registry": {"total": 9, "rows": [], "loaded": {"registry": True}}}
NO_REGISTRY = {"/api/registry": {"total": 0, "rows": [], "loaded": {"registry": False}}}


def _scope(remembered: str, answers: dict) -> dict:
    return _js(SCOPE, "en", remembered, json.dumps(answers))


def test_the_page_opens_on_all_firms_while_a_registry_is_loaded_and_harvey_holds_no_firm():
    got = _scope("", {"/api/companies": {"total": 0}, **LOADED})
    assert got["first"] == "all" and got["kept"] == "all"
    assert got["calls"] == ["/api/companies?limit=1", "/api/registry?limit=1"]


def test_with_firms_in_harvey_the_page_opens_on_in_harvey_without_asking_the_registry():
    got = _scope("", {"/api/companies": {"total": 4}, **LOADED})
    assert got["first"] == "harvey" and got["calls"] == ["/api/companies?limit=1"]


def test_a_remembered_choice_of_all_firms_is_kept_without_a_read():
    got = _scope("all", {"/api/companies": {"total": 4}})
    assert got["first"] == "all" and got["calls"] == []


def test_with_no_registry_the_page_opens_on_in_harvey():
    for answers in ({"/api/companies": {"total": 0}, **NO_REGISTRY},
                    {"/api/companies": {"total": 0},
                     "/api/registry": {"off": True, "loaded": {"registry": True}}}):
        got = _scope("", answers)
        assert got["first"] == "harvey" and got["kept"] == "harvey"


def test_a_read_that_failed_decides_nothing():
    got = _scope("", {"/api/companies": {"total": 0}})          # /api/registry: no answer
    assert got["first"] == "harvey" and got["kept"] == ""


# ── The estimate card's registry lines ──

CARD = PRELUDE + FINDER + r'''
const ests = JSON.parse(process.argv[5]);
let next = null;
global.api = async (path, opts) => { calls.push([path, JSON.parse(opts.body)]); return next; };
el('registry-finder');
(async () => {
  const out = [];
  for (const est of ests) {
    next = est;
    await openFinder('registry-finder', {registry: {words: 'dentist', county: 'BV'}});
    out.push(els['registry-finder'].innerHTML);
  }
  console.log(JSON.stringify({cards: out}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def _est(**registry) -> dict:
    return {"companies": 2, "with_site": 1, "to_look_up": 0, "estimated_cost": 0, "most": 0,
            "searchers": [], "checks": {"checks": 0}, "classifier": {"calls": 0},
            "registry": {"selected": 312, "existing": 2, "to_create": 300, "left": 10,
                         "excluded": 0, "unkeyable": 0, "note": "server words", **registry}}


def _cards(lang: str, *ests) -> list[str]:
    return _js(CARD, lang, "", json.dumps(list(ests)))["cards"]


def test_the_estimate_card_says_the_firms_a_registry_selection_leaves_out():
    ests = (_est(excluded=3), _est(unkeyable=2, left=0), _est())
    en = _cards("en", *ests)
    ro = _cards("ro", *ests)
    assert "Registry selection: 312 firms." in en[0]
    assert ("Already in Harvey: 2 · entering Harvey with this run: 300 · "
            "left for a later run: 10") in en[0]
    assert "left out, their declared website on the do-not-contact list: 3" in en[0]
    assert "can&#39;t key by" not in en[0] and "can't key by" not in en[0]
    # Only the line whose figure isn't 0, and no "left for a later run" at 0.
    assert "left out, a CUI Harvey can't key by: 2" in en[1]
    assert "do-not-contact" not in en[1] and "left for a later run" not in en[1]
    assert "left out" not in en[2]
    assert "lista de necontactat: 3" in ro[0] and "drept cheie: 2" in ro[1]
    assert "lăsate deoparte" not in ro[2]


def test_the_estimates_registry_refusals_read_in_the_pages_language():
    refusals = ({"error": "the registry selection only excludes: name an activity",
                 "code": "registry_only_excludes"},
                {"error": "a registry query with nothing for the search index to match "
                          "isn't taken yet", "code": "registry_every_status"},
                {"error": "some other refusal"})
    en = _cards("en", *refusals)
    ro = _cards("ro", *refusals)
    assert "The selection only excludes firms" in en[0] and "only excludes:" not in en[0]
    assert "Selecția doar exclude firme" in ro[0]
    assert "With every registration" in en[1] and "Cu toate înregistrările" in ro[1]
    assert "isn&#39;t taken yet" not in ro[1] and "isn't taken yet" not in ro[1]
    # A refusal with no code of its own: the server's words, as before.
    assert "some other refusal" in ro[2]


# ── A run as it starts ──

RAN = PRELUDE + FINDER + r'''
const runs = JSON.parse(process.argv[5]);
console.log(JSON.stringify({lines: runs.map(finderRegistryRan)}));
'''


def test_the_run_line_says_what_it_took_and_left_out_and_what_was_already_waiting():
    runs = [
        {"registry": {"created": 40, "left": 5, "excluded": 2, "unkeyable": 1},
         "checks_first": {"jobs": 1, "companies": 40}, "already_waiting": 3},
        {"registry": {"created": 40, "left": 0, "excluded": 0, "unkeyable": 0},
         "checks_first": {"jobs": 0, "companies": 0}, "already_waiting": 0},
        {"queued": 4},
    ]
    en = _js(RAN, "en", "", json.dumps(runs))["lines"]
    ro = _js(RAN, "ro", "", json.dumps(runs))["lines"]
    assert en[0] == ("Entered Harvey now: 40. Checked at ANAF first (free): 40; each one's search "
                     "starts when its check is done. Already waiting from an earlier run (its "
                     "search or its ANAF check): 3. Left out, their declared website on the "
                     "do-not-contact list: 2. Left out, a CUI Harvey can't key by: 1. Left for "
                     "a later run: 5; run Find contacts again on the same selection.")
    assert en[1] == "Entered Harvey now: 40."
    assert en[2] == "" and ro[2] == ""
    assert "Așteptau deja de la o rulare anterioară" in ro[0] and ro[1] == "Intrate acum în Harvey: 40."


# ── An estimate still being priced when the list changes ──

RACE = PRELUDE + FINDER + r'''
const releases = [];
global.api = (path, opts) => { calls.push([path, JSON.parse(opts.body)]);
                               return new Promise(r => { releases.push(r); }); };
vm.runInThisContext(pick('const REGISTRY_SOURCE', 'const REGISTRY_SORTS'));
vm.runInThisContext(pick('function registryFilter()', 'function registryParams()'));
vm.runInThisContext(pick('// A filter change starts over', 'function registryReloadSoon'));
vm.runInThisContext(pick('// The contact finder for the ticked firms or the filter', "// ── A registry firm's panel ──"));
global.registryReloadNow = async () => {};
global.loadRegistry = async () => {};
global.renderRegistrySelbar = () => {}; global.renderRegistryActions = () => {};
const est = n => ({companies: 0, with_site: 0, to_look_up: 0, estimated_cost: 0, most: 0, searchers: [],
                   checks: {checks: 0}, classifier: {calls: 0},
                   registry: {selected: n, existing: 0, to_create: n, left: 0, excluded: 0, unkeyable: 0}});
const tick = () => new Promise(r => setTimeout(r, 5));
const card = () => els['registry-finder'].innerHTML;
(async () => {
  const out = {};
  el('registry-finder'); el('companies-registry');
  // 1. The filter changes while the estimate is priced: no card for the old one.
  _rg.q = 'dentist'; _rg.f = {county: 'BV'};
  let p = findForRegistry(false); await tick();
  _rg.q = 'frizerie'; _rg.f = {county: 'CJ'};
  registryFilterChanged();
  releases.shift()(est(312)); await p; await tick();
  out.filter = {card: card(), finder: _finder, pending: els['registry-finder'].dataset.pending || null};
  // 2. A tick changes meanwhile (the selection bar's own close).
  _rg.selected.set('J08/100/2010', {});
  p = findForRegistry(true); await tick();
  closeRegistryFinder();
  releases.shift()(est(1)); await p; await tick();
  out.tick = {card: card(), finder: _finder};
  // 3. A second estimate asked before the first answers: only the second is drawn.
  _rg.q = 'dentist'; _rg.f = {county: 'BV'};
  const a = findForRegistry(false); await tick();
  _rg.q = 'frizerie'; _rg.f = {county: 'CJ'};
  const b = findForRegistry(false); await tick();
  releases[1](est(20)); await b; await tick();
  releases[0](est(312)); await a; await tick();
  out.newest = {card: card(), sent: _finder && _finder.body.registry};
  console.log(JSON.stringify(out));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_an_estimate_still_priced_when_the_list_changes_draws_no_card():
    out = _js(RACE, "en", "")
    for case in ("filter", "tick"):
        assert out[case]["card"] == "" and out[case]["finder"] is None, (case, out[case])
    assert out["filter"]["pending"] is None
    # The newest estimate is the one drawn, and the one Run sends.
    assert "Registry selection: 20 firms." in out["newest"]["card"]
    assert "312" not in out["newest"]["card"]
    assert out["newest"]["sent"] == {"county": "CJ", "words": "frizerie"}


# ── The Overview's companies check ──

OVERVIEW = PRELUDE + r'''
vm.runInThisContext(pick('async function loadOverview()', 'function renderOverviewHints()'));
vm.runInThisContext(pick('function renderOverviewSetup(', '// Setup-check wording'));
['overview-stats', 'overview-runs', 'overview-setup'].forEach(el);
global.renderOverviewHints = () => {}; global.loadOverviewSiteCheck = () => {};
global.renderOverviewSending = () => {}; global.renderRunsTable = () => {};
const answers = JSON.parse(process.argv[5]);
global.api = async path => { calls.push(path); return answers[path.split('?')[0]] ?? null; };
(async () => {
  await loadOverview();
  console.log(JSON.stringify({setup: els['overview-setup'].innerHTML, calls}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def _overview(lang: str, answers: dict) -> dict:
    base = {"/api/runs": [], "/api/setup-status": {"checks": []}}
    return _js(OVERVIEW, lang, "", json.dumps({**base, **answers}))


def test_the_overviews_companies_check_leads_where_companies_come_from():
    empty = {"/api/overview": {"companies": 0}}
    loaded = _overview("en", {**empty, **LOADED})
    assert "goTab(&quot;companies&quot;)" in loaded["setup"]
    assert "Run Find contacts from Companies → All firms" in loaded["setup"]
    for answers in ({**empty, **NO_REGISTRY}, empty,
                    {**empty, "/api/registry": {"off": True, "loaded": {"registry": True}}}):
        for lang in ("en", "ro"):
            got = _overview(lang, answers)
            assert "goTab(&quot;discover&quot;)" in got["setup"], got
            assert "companies&quot;)" not in got["setup"]
    assert "Search for companies under Discover" in _overview("en", empty)["setup"]
    assert "Caută firme la Descoperă" in _overview("ro", empty)["setup"]
    # Companies on file: the check is done, and the registry isn't asked.
    done = _overview("en", {"/api/overview": {"companies": 3}})
    assert "/api/registry?limit=1" not in done["calls"] and "goTab" not in done["setup"]


# ── Both languages ──

def test_every_new_string_is_in_both_languages_and_all_firms_names_one_list():
    script = r'''
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const keys = L => Object.keys(I18N[L]).filter(k => k.startsWith('finder.rg_') || k.startsWith('finder.why.')
                                               || k.startsWith('ov.check.companies')).sort();
console.log(JSON.stringify({ro: keys('ro'), en: keys('en'), scope: I18N.ro['co.scope.all'],
                            menu: I18N.ro['actions.all_companies']}));
'''
    got = _js(script)
    assert got["ro"] == got["en"]
    assert {"finder.rg_excluded", "finder.rg_unkeyable", "finder.rg_waiting",
            "finder.why.registry_only_excludes", "finder.why.registry_every_status",
            "ov.check.companies_help_discover"} <= set(got["en"])
    # The In Harvey Actions menu's header doesn't read as the registry's list.
    assert got["menu"] != got["scope"] + " ({n})" and "Harvey" in got["menu"]
