let currentTab = 'overview';
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };
let _modules = { email: false };   // dashboard modules (visibility only)

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];
const THEME_ICONS = {auto: 'contrast', light: 'sun', dark: 'moon'};

function currentTheme() {
  try { const s = localStorage.getItem('harvey-theme'); return THEMES.includes(s) ? s : 'auto'; }
  catch { return 'auto'; }
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  const btn = document.getElementById('theme-btn');
  if (btn) {
    btn.innerHTML = icon(THEME_ICONS[mode] || 'contrast') + '<span>' + escHtml(t('theme.' + mode)) + '</span>';
    btn.title = t('theme.title');
  }
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem('harvey-theme', next); } catch { /* private mode */ }
  applyTheme(next);
}

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  // A module that is off: said in the viewer's language, naming the module
  // (the server's English sentence is for the API and the CLI).
  if (data && data.off_module) return t('modp.off_refused', {name: moduleName(data.off_module)});
  return (data && (data.message || data.error || data.detail)) || fallback;
}

function moduleName(id) {
  return hasKey('en', 'module.' + id + '.name') ? t('module.' + id + '.name') : String(id);
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function safeHref(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? escHtml(s) : '';
}

// tel: and mailto: links from collected data: only the characters a number
// or an address can hold, so nothing else ever reaches the href.
function telHref(n) {
  // The number the text starts with, and nothing after it: a stray
  // extension ("int. 12"), a second number ("; 0740 …" or " / 0740 …") or
  // junk must never add digits to the one that gets dialled.
  const lead = String(n || '').split(/[;,]|\s\/\s/)[0].match(/^\s*\+?[\d\s().\-\/]*/);
  const digits = (lead ? lead[0] : '').replace(/[^\d+]/g, '');
  return /^\+?\d{4,16}$/.test(digits) ? 'tel:' + digits : '';
}

function mailHref(e) {
  const s = String(e || '').trim();
  return /^[^\s@<>"'(),;:\\]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(s) ? 'mailto:' + escHtml(s) : '';
}

// ── One status vocabulary ──
//
// Harvey's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Everything resolves through this map (label key in i18n.js, then tone), so
// a status reads the same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  new: 'active', contacted: 'active', replied: 'good', interested: 'good', meeting: 'note',
  not_interested: 'bad', bounced: 'bad', write_failed: 'bad', queued: 'active', opted_out: 'bad',
  draft: 'waiting', active: 'good', completed: 'idle', paused: 'bad',
  open: 'active', closed: 'idle', closed_won: 'good', closed_lost: 'bad', objection: 'waiting',
  question: 'active', ooo: 'idle', wrong_person: 'waiting', unsubscribe: 'bad', escalate: 'bad',
  needs_human: 'waiting',
  pending_review: 'waiting', approved: 'good', sending: 'active', scheduled: 'active', sent: 'good',
  failed: 'bad', rejected: 'idle', cancelled: 'idle',
  proposed: 'waiting', confirmed: 'good',
  verified: 'good', risky: 'waiting', guess: 'idle', invalid: 'bad',
  running: 'active', stale: 'bad', stalled: 'bad',
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const tone = own(STATUS, key);
  if (tone) return { label: t('st.' + key), tone: tone };
  const label = String(status || t('st.unknown')).replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let s = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(s) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';  // stored UTC
    const dt = new Date(s);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString(intlLocale(), {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// t() for a sentence with a count in it (plural forms pick on the number),
// escaped, with the number in bold.
function tBold(key, n) {
  const num = fmtN(n);
  const text = escHtml(t(key, {n: n}));
  const at = text.indexOf(num);
  return at < 0 ? text : text.slice(0, at) + '<b>' + num + '</b>' + text.slice(at + num.length);
}

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let s = String(d || '').trim().replace(' ', 'T');
  if (s && !/[zZ]$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
  return new Date(s);
}

// "3 min ago", "5 h ago", "yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  let rtf = null;
  try { rtf = new Intl.RelativeTimeFormat(intlLocale(), {numeric: 'auto', style: 'short'}); } catch { /* old browser */ }
  if (Math.abs(mins) < 1) return escHtml(t('time.now'));
  if (rtf && mins > -60 && mins < 60) return escHtml(rtf.format(-mins, 'minute'));
  if (rtf && mins > -60 * 24 && mins < 60 * 24) return escHtml(rtf.format(-Math.round(mins / 60), 'hour'));
  if (rtf && mins >= 60 * 24 && mins < 60 * 48) return escHtml(rtf.format(-1, 'day'));
  const opts = {month: 'short', day: 'numeric'};
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return escHtml(dt.toLocaleDateString(intlLocale(), opts));
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return t('time.today');
  if (diff === 1) return t('time.yesterday');
  return dt.toLocaleDateString(intlLocale(), {weekday: 'long', month: 'short', day: 'numeric'});
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString(intlLocale(), {hour: 'numeric', minute: '2-digit'});
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || '') + '">' +
    vals.map(v => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({length: n}, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'src.team_page', known_company_backfill: 'src.team_page',
  scraped_mailto: 'src.published', hunter_domain_search: 'Hunter', linkedin_search: 'LinkedIn',
  job_board: 'src.job_board', web_search: 'src.web_search', company_inbox: 'src.company_inbox',
  osm: 'OpenStreetMap', dataforseo_listings: 'DataForSEO', dataforseo_serp: 'DataForSEO SERP',
  dataforseo_maps: 'DataForSEO Maps', serper: 'Serper', tinyfish: 'TinyFish',
  scavio_maps: 'Scavio Maps', scavio_search: 'Scavio Search', 'registry:onrc': 'src.onrc', onrc: 'src.onrc',
};
function sourceLabel(s) {
  if (!s) return '';
  const hit = own(SOURCE_LABELS, s);
  if (hit) return escHtml(hit.startsWith('src.') ? t(hit) : hit);
  return escHtml(String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh'};
function agentName(agent) {
  const key = 'agent.' + String(agent || '');
  const label = t(key);
  return label === key ? String(agent || '') : label;
}

// One plain sentence per logged action, from the details each agent records.
// Values from the log are escaped before they go into the sentence.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = v => Number(v) || 0;
  switch (a.action_type) {
    case 'prospect': return t('act.prospect', {n: n(d.prospects_found)});
    case 'write_campaign': return t('act.write_campaign', {camp: camp, n: n(d.prospect_count)});
    case 'write_failed': return t(camp ? 'act.write_failed_in' : 'act.write_failed', {camp: camp});
    case 'stage_campaign': return t('act.stage_campaign', {n: n(d.staged), camp: camp}) +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return t('act.send_campaign', {camp: camp, n: n(d.leads_added)});
    case 'email_sent': return d.kind === 'reply' ? t('act.reply_sent', {who: who})
      : t('act.email_sent', {step: escHtml(String(d.step || 1)), who: who});
    case 'reply': return t('act.reply', {who: who}) + intent;
    case 'reply_queued': return t('act.reply_queued', {who: who}) + intent;
    case 'opt_out': return t('act.opt_out', {who: who}) + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return t('act.opt_out_undone', {who: who || t('act.a_contact')});
    case 'reply_after_opt_out': return t('act.reply_after_opt_out', {who: who});
    case 'escalation': return t('act.escalation', {who: who}) + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return t('act.bounce', {who: who});
    case 'manual_send': return t('act.manual_send');
    case 'manual_send_undone': return t('act.manual_send_undone');
    case 'outbox_edited': return t('act.outbox_edited');
    case 'sending_mode': return t('act.sending_mode', {mode: escHtml(d.mode || '')});
    case 'conversation_status':
      return d.to === 'closed' ? t('act.convo_closed')
        : d.from === 'needs_human' ? t('act.convo_handed_back') : t('act.convo_reopened');
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? t('act.heartbeat', {text: text}) : text;
}

// An empty or failed section: glyph, title, one short line. `boxed` gives
// it a card of its own where the container isn't one already.
function emptyState(glyph, title, copy, boxed, action) {
  return '<div class="empty' + (boxed ? ' boxed' : '') + '"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="empty-copy">' + copy + '</div>' +
    (action ? '<div class="empty-action">' + action + '</div>' : '') + '</div>';
}

// How the last read failed: 'network' (no answer at all) or the HTTP status
// the server answered with. The two need different next steps.
let _lastFail = null;

function offlineState() {
  const server = failedOnServer();
  return emptyState(icon('alert'),
    escHtml(t(server ? 'offline.server_title' : 'offline.title')),
    server ? escHtml(t('offline.server_copy', {status: _lastFail})) : t('offline.copy'), true,
    '<button type="button" class="btn btn-secondary btn-sm" onclick="loadCurrentTab()">' +
      icon('refresh') + escHtml(t('offline.retry')) + '</button>');
}

// True when the last failed read got an HTTP answer (the server is up but
// erred), false when nothing answered at all.
function failedOnServer() {
  return !!_lastFail && _lastFail !== 'network';
}

// A page whose data couldn't be loaded hides the parts that only make sense
// with that data (forms, builders: marked .needs-data), so nothing invites
// an edit or a spend against a blank page.
function markLoadFailed(sectionId, failed) {
  const sec = document.getElementById(sectionId);
  if (sec) sec.classList.toggle('load-failed', !!failed);
}

// The one-line version, for a small card that has nothing to show.
function offlineLine(cls) {
  const server = failedOnServer();
  return '<p class="' + (cls || 'muted small') + '">' +
    escHtml(t(server ? 'offline.server_short' : 'offline.short')) + '</p>';
}

async function api(path, opts) {
  try {
    const r = await authFetch(path, opts);
    if (await lockIfRefused(r)) return null;
    if (!r.ok) _lastFail = r.status;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? {success: false, ...body} : null;
    }
    return await r.json();
  } catch {
    _lastFail = 'network';
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? t('lock.server', {why: why})
    : authToken() ? t('lock.rejected') : t('lock.missing'));
  return true;
}

function showLock(reason) {
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = t('status.locked');
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

function unlockWithToken(event) {
  event.preventDefault();
  const input = document.getElementById('lock-token');
  const value = (input.value || '').trim();
  input.value = '';
  if (!setAuthToken(value)) {
    document.getElementById('lock-reason').textContent = t('lock.bad_format');
    return;
  }
  hideLock();
  init();
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn, opts) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path, opts);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast(t('export.failed'), 'error'); return; }
    const matched = Number(r.headers.get('X-Export-Matched') || 0);
    const rows = Number(r.headers.get('X-Export-Rows') || 0);
    if (matched > rows) showToast(t('export.truncated', {rows: rows, matched: matched}), 'error');
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast(t('export.failed_offline'), 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function postDownload(path, filename, body) {
  return downloadCsv(path, filename, null, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
}

function showToast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'success');
  el.setAttribute('role', 'status');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// The Clipboard API needs a secure context; opened by LAN address (Docker on
// another machine) it isn't there, so fall back to a hidden textarea.
function copyFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  const focus = document.activeElement;
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  if (focus && focus.focus) focus.focus();
  if (!ok) throw new Error('copy refused');
}

async function copyText(text, btn) {
  try {
    const value = String(text || '');
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else copyFallback(value);
    showToast(t('common.copied', {text: text}), 'success');
    if (btn) { btn.classList.add('done'); setTimeout(() => btn.classList.remove('done'), 1200); }
  } catch {
    showToast(t('common.copy_failed'), 'error');
  }
}

// ── Tabs and modules ──

function showTab(id, btn) {
  if (!document.getElementById(id)) id = 'overview';
  const section = document.getElementById(id);
  if (section.dataset.module && !_modules[section.dataset.module]) id = 'overview';
  currentTab = id;
  closeMenus();
  document.querySelectorAll('.section').forEach(s => s.classList.toggle('active', s.id === id));
  document.querySelectorAll('nav button').forEach(b => {
    const on = b.dataset.tab === id;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  try { sessionStorage.setItem('harvey-tab', id); } catch { /* ignore */ }
  loadCurrentTab();
  window.scrollTo(0, 0);
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'overview': loadOverview(); break;
    case 'today': loadLiveStatus(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadRuns(); loadTodayActivity(); break;
    case 'signals': loadSignals(); break;
    case 'modules': loadModulesPage(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadModulesCard(); if (_modules.email) loadSendingModeCard(); break;
    case 'controls': loadHarveyStatus(); loadLogs(); loadControlSending(); break;
  }
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) {
  showTab(id, document.querySelector('nav button[data-tab="' + id + '"]'));
}

function applyModules() {
  document.querySelectorAll('[data-module]').forEach(el => {
    if (el.classList.contains('section')) return;      // tabs show through showTab
    el.hidden = !_modules[el.dataset.module];
  });
  const sec = document.getElementById(currentTab);
  if (sec && sec.dataset.module && !_modules[sec.dataset.module]) showTab('overview');
}

async function loadModules() {
  const data = await api('/api/ui/modules');
  if (data && data.modules) _modules = Object.assign({email: false}, data.modules);
  applyModules();
}

function loadModulesCard() {
  const box = document.getElementById('module-email');
  const label = document.getElementById('module-email-state');
  if (box) box.checked = !!_modules.email;
  if (label) label.textContent = t(_modules.email ? 'mod.on' : 'mod.off');
}

async function setModule(name, on) {
  const res = await api('/api/ui/modules', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[name]: !!on}),
  });
  if (res && res.success) {
    _modules = Object.assign({email: false}, res.modules);
    applyModules();
    showToast(t(on ? 'mod.email.shown' : 'mod.email.hidden'), 'success');
    if (currentTab === 'overview') loadOverview();
    if (_modules.email) loadSendingModeCard();
  } else {
    showToast(failMsg(res, t('mod.failed')), 'error');
  }
  loadModulesCard();
}

// ── Surse: the modules Harvey is made of, each on or off ──

const MODULE_SETS = ['core', 'original', 'ro', 'shared'];
const MODULE_KINDS = ['source', 'search', 'enricher', 'market', 'gazetteer', 'language', 'action', 'ui'];

async function loadModulesPage() {
  const el = document.getElementById('modules-list');
  const data = await api('/api/modules');
  markLoadFailed('modules', !data || data.error);
  if (!data || data.error) { el.innerHTML = offlineState(); return; }
  renderModulesPage(data);
}

function renderModulesPage(data) {
  const el = document.getElementById('modules-list');
  const sets = (data.sets || []).filter(s => (s.modules || []).length);
  // Packs in data/modules that were left out, and why (the server's lines
  // name the file and the problem).
  const problems = (data.pack_problems || []).length
    ? '<div class="test-result error module-problems"><strong>' + escHtml(t('modp.pack_problems')) +
      '</strong><ul>' + data.pack_problems.map(p => '<li>' + escHtml(p) + '</li>').join('') + '</ul></div>'
    : '';
  if (!sets.length) {
    el.innerHTML = problems + emptyState(icon('layers'), escHtml(t('modp.empty')), escHtml(t('modp.empty_copy')), true);
    return;
  }
  el.innerHTML = problems + sets.map(s =>
    '<div class="subhead">' + escHtml(MODULE_SETS.includes(s.key) ? t('modp.set.' + s.key) : s.key) + '</div>' +
    '<div class="card module-list">' + s.modules.map(moduleRow).join('') + '</div>'
  ).join('');
}

function moduleRow(m) {
  const name = tServer('module.' + m.id + '.name', m.name);
  const cost = m.cost || {};
  const meta = [];
  const line = (key, html) => meta.push('<span class="k">' + escHtml(t(key)) + '</span><span class="v">' + html + '</span>');
  if ((m.signals || []).length) {
    line('modp.signals', m.signals.map(s =>
      '<span class="cost-chip">' + escHtml(tServer('sigc.' + s.code + '.label', s.label) || s.code) + '</span>').join(''));
  }
  // Presence only: the server never sends a value.
  if ((m.secrets || []).length) {
    line('modp.keys', m.secrets.map(k =>
      '<span class="email-tag ' + (k.present ? 'verified' : 'guess') + '" title="' +
        escHtml(t(k.present ? 'modp.key_set' : 'modp.key_missing')) + '">' + escHtml(k.key) +
        ' · ' + escHtml(t(k.present ? 'modp.key_set' : 'modp.key_missing')) + '</span>').join(''));
  }
  const price = [cost.free_tier && tServer('module.' + m.id + '.free', cost.free_tier),
                 cost.note && tServer('module.' + m.id + '.cost', cost.note)].filter(Boolean);
  if (price.length) line('modp.cost', escHtml(price.join(' · ')));
  if ((m.requires || []).length) {
    line('modp.requires', escHtml(m.requires.map(r => tServer('module.' + r.id + '.name', r.name || r.id)).join(', ')));
  }
  const kind = MODULE_KINDS.includes(m.kind) ? t('modp.kind.' + m.kind) : m.kind;
  return '<div class="module-row">' +
    '<div class="module-text">' +
      '<h3>' + escHtml(name) + ' <span class="tag">' + escHtml(kind) + '</span>' +
        (m.locked ? ' <span class="tag accent">' + escHtml(t('modp.core')) + '</span>' : '') + '</h3>' +
      '<p class="desc">' + escHtml(tServer('module.' + m.id + '.about', m.description)) + '</p>' +
      (meta.length ? '<div class="module-meta">' + meta.join('') + '</div>' : '') +
      (m.terms_note ? '<div class="module-terms">' + escHtml(tServer('module.' + m.id + '.terms', m.terms_note)) + '</div>' : '') +
    '</div>' +
    '<label class="switch"' + (m.locked ? ' title="' + escHtml(t('modp.locked')) + '"' : '') + '>' +
      '<input type="checkbox" ' + (m.enabled ? 'checked ' : '') + (m.locked ? 'disabled ' : '') +
        'aria-label="' + escHtml(name) + '" onchange="setModuleOn(' + jsq(m.id) + ', this.checked)">' +
      '<span class="track" aria-hidden="true"><span class="thumb"></span></span>' +
      '<span class="switch-label">' + escHtml(t(m.enabled ? 'mod.on' : 'mod.off')) + '</span>' +
    '</label>' +
  '</div>';
}

async function setModuleOn(id, on) {
  const res = await api('/api/modules/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: !!on}),
  });
  if (res && res.success) {
    showToast(t(on ? 'modp.toast_on' : 'modp.toast_off'), 'success');
    renderModulesPage(res);
    loadModules();
  } else {
    showToast(failMsg(res, t('mod.failed')), 'error');
    loadModulesPage();
  }
}

// ── Language ──

function switchLang(lang) {
  setLang(lang);
  document.querySelectorAll('.lang-switch button').forEach(b => {
    const on = b.dataset.lang === getLang();
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  applyTheme(currentTheme());
  if (!_locked) { loadHarveyStatus(); loadCurrentTab(); }
  if (_drawer.id) renderDrawer();
  if (currentTab !== 'settings') loadModulesCard();
}

// ── Overview: the landing page ──

async function loadOverview() {
  renderOverviewHints();
  const [ov, runs, setup] = await Promise.all([api('/api/overview'), api('/api/runs'), api('/api/setup-status')]);
  const statsEl = document.getElementById('overview-stats');
  if (!ov) {
    // Don't leave the last numbers next to "can't reach the server".
    statsEl.innerHTML = offlineState();
    const runsEl = document.getElementById('overview-runs');
    const setupEl = document.getElementById('overview-setup');
    if (runsEl) runsEl.innerHTML = offlineLine('pad muted');
    if (setupEl) setupEl.innerHTML = offlineLine();
    return;
  }
  navCount('nav-signals', ov.signals_proposed || 0);
  const nav = document.getElementById('nav-companies');
  if (nav) { nav.textContent = ov.companies ? fmtN(ov.companies) : ''; nav.className = 'nav-count soft' + (ov.companies ? ' on' : ''); }

  const total = ov.companies || 0;
  // A share above zero never reads as 0% or an empty bar: 10 of 2,421 is
  // "<1%" with a sliver of bar.
  const exact = n => total ? 100 * n / total : 0;
  const pct = n => n > 0 && exact(n) < 0.5 ? '<1' : Math.round(exact(n));
  const bar = n => n > 0 ? Math.max(exact(n), 1.5) : 0;
  const tile = (label, value, note, share, ico) =>
    '<div class="stat">' +
      '<div class="stat-label">' + icon(ico) + escHtml(label) + '</div>' +
      '<div class="stat-value' + (value ? '' : ' zero') + '">' + fmtN(value) + '</div>' +
      (share !== null ? '<div class="share" role="img" aria-label="' + escHtml(note) + '"><span style="width:' + share + '%"></span></div>' : '') +
      '<div class="stat-note">' + escHtml(note) + '</div>' +
    '</div>';
  statsEl.innerHTML = '<div class="stat-strip">' +
    tile(t('ov.companies'), total, t('ov.companies_note', {n: ov.with_activity || 0}), null, 'companies') +
    tile(t('ov.with_phone'), ov.with_phone || 0, t('ov.share', {p: pct(ov.with_phone || 0)}), bar(ov.with_phone || 0), 'phone') +
    tile(t('ov.with_email'), ov.with_email || 0, t('ov.share', {p: pct(ov.with_email || 0)}), bar(ov.with_email || 0), 'mail') +
    tile(t('ov.contacts'), ov.contacts || 0, t('ov.contacts_note'), null, 'contacts') +
    tile(t('ov.people'), ov.people || 0, t('ov.people_note'), null, 'layers') +
  '</div>';

  renderOverviewSending(ov.sending);
  renderRunsTable(document.getElementById('overview-runs'), runs, 6);
  renderOverviewSetup(setup, ov);
}

function renderOverviewHints() {
  const el = document.getElementById('overview-hints');
  if (!el) return;
  const examples = ['stomatologie', '8623', 'instalator Brașov', 'restaurant'];
  el.innerHTML = '<span class="muted">' + escHtml(t('ov.try')) + '</span>' + examples.map(x =>
    '<button type="button" class="chip-btn" onclick="overviewSearchFor(' + jsq(x) + ')">' + escHtml(x) + '</button>').join('');
}

function overviewSearch(event) {
  event.preventDefault();
  overviewSearchFor(document.getElementById('overview-q').value);
}

function overviewSearchFor(q) {
  _co.q = String(q || '').trim();
  _co.offset = 0;
  _co.selected.clear();
  const box = document.getElementById('company-search');
  if (box) box.value = _co.q;
  goTab('companies');
}

function renderRunsTable(el, runs, max) {
  if (!el) return;
  if (!Array.isArray(runs)) { el.innerHTML = '<p class="pad muted">' + escHtml(t('runs.unreadable')) + '</p>'; return; }
  if (!runs.length) {
    el.innerHTML = '<p class="pad muted">' + escHtml(t('runs.none')) + '</p>';
    return;
  }
  // On a phone each run is a small card (CSS: table.runs-table), labelled
  // from data-label, so no column hides off-screen.
  el.innerHTML = '<table class="nowrap compact runs-table"><thead><tr><th>' + escHtml(t('runs.what')) + '</th><th>' +
    escHtml(t('runs.status')) + '</th><th class="num">' + escHtml(t('runs.records')) + '</th><th class="num">' +
    escHtml(t('runs.cost')) + '</th><th>' + escHtml(t('runs.when')) + '</th></tr></thead><tbody>' +
    runs.slice(0, max || 25).map(r =>
      '<tr><td class="run-what"><span class="cell-main">' + escHtml(runName(r)) + '</span>' +
        (r.provider ? ' <span class="muted">· ' + sourceLabel(r.provider) + '</span>' : '') + '</td>' +
      '<td>' + badge(r.status) + '</td>' +
      '<td class="num" data-label="' + escHtml(t('runs.records')) + '">' + fmtN(r.records || 0) + '</td>' +
      '<td class="num muted" data-label="' + escHtml(t('runs.cost')) + '">' + (r.cost_usd ? escHtml(fmtUsd(r.cost_usd, 4)) : escHtml(t('runs.free'))) + '</td>' +
      '<td class="muted run-when">' + relTime(r.started_at) + '</td></tr>').join('') +
    '</tbody></table>';
}

function runName(r) {
  const key = 'runs.stage.' + String(r.stage || '');
  const label = t(key);
  return label === key ? String(r.stage || '') : label;
}

// Setup checks that matter for finding contacts (scope "finding"), plus
// whether any signal is confirmed and any company is on file.
function renderOverviewSetup(setup, ov) {
  const el = document.getElementById('overview-setup');
  if (!el) return;
  const checks = ((setup && setup.checks) || []).filter(c => c.scope === 'finding');
  const rows = checks.map(c => ({done: c.done, label: checkLabel(c), help: checkHelp(c), optional: !c.required}));
  rows.push({done: (ov.signals_confirmed || 0) > 0, label: t('ov.check.signals'),
             help: t('ov.check.signals_help'), tab: 'signals'});
  rows.push({done: (ov.companies || 0) > 0, label: t('ov.check.companies'),
             help: t('ov.check.companies_help'), tab: 'discover'});
  const needed = rows.filter(r => !r.optional);
  const done = needed.filter(r => r.done).length;
  el.innerHTML = '<div class="setup-head"><span class="setup-pct">' +
      escHtml(t('ov.setup_done', {done: done, total: needed.length})) + '</span></div>' +
    '<div class="bar" style="margin-bottom:6px"><span style="width:' +
      Math.round(100 * done / Math.max(1, needed.length)) + '%;background:var(--accent)"></span></div>' +
    rows.map(r =>
      '<div class="check-item">' +
        (r.done ? '<span class="check-icon done">' + icon('check') + '</span>' : '<span class="check-icon pending"></span>') +
        '<div class="check-info"><div class="check-label' + (r.done ? ' done' : '') + '">' + escHtml(r.label) +
          (r.optional ? '<span class="optional-tag">' + escHtml(t('common.optional')) + '</span>' : '') + '</div>' +
          (!r.done ? '<div class="check-help">' + escHtml(r.help) +
            (r.tab ? ' <a href="#" onclick="goTab(' + jsq(r.tab) + ');return false">' + escHtml(t('common.open')) + '</a>' : '') +
          '</div>' : '') +
        '</div></div>').join('');
}

// Setup-check wording, by the check's stable id (the server's English is
// the fallback for an id this dashboard doesn't know).
function checkLabel(c) {
  if (c.id === 'venv') return t('check.venv.' + (['docker', 'installed'].includes(c.env) ? c.env : 'venv'));
  if (c.id === 'email_provider') return t('check.email_provider', {provider: c.provider || ''});
  if (c.id === 'config') return t('check.config', {file: c.file || 'harvey.yaml'});
  if (c.id === 'warmup_service') return t('check.warmup.' + (c.choice || 'none'));
  const key = 'check.' + c.id;
  const label = t(key);
  return label === key ? (c.label || c.id) : label;
}

function checkHelp(c) {
  const key = c.id === 'email_provider' ? 'check.help.email_provider.' + (c.provider || 'instantly')
    : 'check.help.' + c.id;
  const help = t(key);
  return help === key ? (c.help || '') : help;
}

// ── Today: what needs a human (Email module) ──

let _setup = null;

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), escHtml(t('today.error_title')),
      escHtml(t('today.error_copy')) + ' ' + escHtml(data.error), true);
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">' + escHtml(t('today.clear_title')) + '</div>' +
      '<div class="qdetail">' + escHtml(t('today.clear_copy')) + '</div>' +
      '</div></div>';
    return;
  }

  // "good" items are optional next steps (read the sites, find more), not
  // finished ones: an arrow, never the done-tick the clear queue uses.
  const glyph = {bad: 'alert', warn: 'flag', good: 'arrow'};
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    const words = todayItemText(it, _todayStats);
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
        '<div class="qtitle">' + escHtml(words.title) + '</div>' +
        '<div class="qdetail">' + escHtml(words.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
        escHtml(words.action) + icon('arrow') + '</button>' +
    '</div>';
  }).join('') + '</div>';
}

// A Today item in the dashboard's language, by its stable key. Items whose
// wording the server builds from live detail (a pause reason, a hold) keep
// the server's text.
// The kill switch's reason is free text; the one the dashboard itself
// writes reads in the dashboard's language.
function pauseReason(why) {
  return String(why || '').trim() === 'paused from dashboard' ? t('send.paused_by_dashboard') : String(why || '');
}

function todayItemText(it, stats) {
  const s = stats || {};
  const same = {title: it.title, detail: it.detail, action: it.action};
  const tr = (title, detail, action) => ({title: title, detail: detail, action: action});
  switch (it.key) {
    case 'paused': return tr(t('today.item.paused'), t('today.item.paused_detail', {why: pauseReason(String(it.detail || '').split('. Nothing will go out')[0])}), t('today.item.paused_action'));
    case 'setup': {
      const missing = ((_setup && _setup.checks) || []).filter(c => c.required && !c.done).slice(0, 3).map(checkLabel);
      return tr(t('today.item.setup'), missing.length ? missing.join(', ') : t('today.item.setup_detail'), t('today.item.setup_action'));
    }
    case 'signals': return tr(t('today.item.signals', {n: s.signals_proposed || 0}), t('today.item.signals_detail'), t('today.item.signals_action'));
    case 'signals-none': return tr(t('today.item.signals_none'), t('today.item.signals_none_detail'), t('today.item.signals_action'));
    case 'discover': return tr(t('today.item.discover'), t('today.item.discover_detail'), t('today.item.discover_action'));
    case 'profile': return tr(t('today.item.profile', {n: s.unprofiled || 0}), t('today.item.profile_detail'), t('today.item.profile_action'));
    case 'needs-human': return tr(t('today.item.needs_human', {n: s.needs_human || 0}), t('today.item.needs_human_detail'), t('today.item.needs_human_action'));
    case 'outbox': return tr(t('today.item.outbox', {n: s.outbox_pending || 0}),
      /Autopilot/.test(it.detail || '') ? t('today.item.outbox_auto') : t('today.item.outbox_detail'), t('today.item.outbox_action'));
    case 'replies': return tr(t('today.item.replies', {n: s.open_conversations || 0}), t('today.item.replies_detail'), t('today.item.replies_action'));
  }
  if (/Open the log/.test(it.action || '')) same.action = t('today.item.open_log');
  return same;
}

// ── Live status: the "what's happening?" answer, kept on screen ──

function liveStepText(s) {
  const v = s.vars || {};
  switch (s.key) {
    case 'find': return [t('live.find'), t('live.find_d', {n: v.companies || 0})];
    case 'read': return [t('live.read'), t('live.read_d', {done: v.profiled || 0, total: v.with_site || 0}) +
      (v.eta_min !== null && v.eta_min !== undefined ? ' · ' + etaText(v.eta_min) : '') +
      (v.no_site ? ' · ' + t('live.read_nosite', {n: v.no_site}) : '')];
    case 'emails': return [t('live.emails'), t('live.emails_d', {done: v.checked || 0, total: v.profiled || 0, n: v.emails || 0})];
    case 'verify': return [t('live.verify'), t('live.verify_d', {v: v.verified || 0, r: v.risky || 0, u: v.unconfirmed || 0}) +
      (v.invalid ? ' · ' + t('live.verify_bad', {n: v.invalid}) : '')];
    case 'write': return [t('live.write'), t('live.write_d', {n: v.written || 0})];
    case 'sent': return [t('live.sent'), t('live.sent_d', {done: v.sent || 0, total: v.written || 0})];
    case 'replies': return [t('live.replies'), v.replies ? t('live.replies_d', {n: v.replies}) : t('live.replies_none')];
  }
  return [s.label, s.detail];
}

function etaText(min) {
  if (min === null || min === undefined) return '';
  return min < 1 ? t('live.eta_soon') : t('live.eta', {n: Math.round(min)});
}

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const server = failedOnServer();
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = t(server ? 'live.stale_server' : 'live.stale');
    else el.innerHTML = '<p class="muted live-flag">' +
      escHtml(server ? t('live.server_error', {status: _lastFail}) : t('live.offline')) + '</p>';
    return;
  }
  const glyph = {done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: ''};
  const fill = {done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)'};
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    const [label, detail] = liveStepText(s);
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(label) + '</span>' +
      '<span class="step-detail">' + escHtml(detail) + '</span>' + bar +
    '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? t('live.job_profile')
    : r.stage === 'discover' ? t('live.job_discover', {provider: r.provider || ''})
    : String(r.stage || t('live.job'));
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
        ' · ' + escHtml(t('live.minutes', {n: r.elapsed_min})) + (r.eta_min !== null && r.eta_min !== undefined ? ' · ' + escHtml(etaText(r.eta_min)) : '') + '</span>').join('')
    : '<span class="live-now">' + escHtml(t('live.nothing')) + '</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
      escHtml(t('live.stopped', {jobs: (d.stopped || []).map(jobName).join(', ')})) + '</p></div>'
    : '';
  const nextKey = d.next_key ? 'live.next.' + d.next_key : '';
  const next = nextKey && t(nextKey, d.next_vars || {}) !== nextKey ? t(nextKey, d.next_vars || {}) : d.next;
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
      '<span class="setup-pct">' + escHtml(t('live.cost', {cost: fmtUsd(d.cost_usd || 0, 2)})) + '</span></div>' +
    stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (next ? '<div class="live-next">' + icon('arrow') + '<span><b>' + escHtml(t('live.next')) + '</b> · ' + escHtml(next) + '</span>' +
      (d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">' + escHtml(t('common.go')) + '</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">' + escHtml(t('live.updated', {time: new Date().toLocaleTimeString(intlLocale())})) + '</div>';
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const el = document.getElementById('today-kpis');
  if (!el) return;
  const tr = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note) =>
    '<div class="kpi"><div class="kpi-label">' + escHtml(label) + '</div>' +
    '<div class="kpi-main"><span class="kpi-value' + (value ? '' : ' zero') + '">' + value + '</span>' + spark + '</div>' +
    '<span class="kpi-note">' + note + '</span></div>';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const sent = stats.email_provider === 'instantly'
    ? card(t('kpi.sent'), '&mdash;', '', escHtml(t('kpi.sent_instantly')))
    : card(t('kpi.sent'), fmtN(sum(tr.sent)), sparkBars(tr.sent, t('kpi.sent_spark')),
           tBold('kpi.sent_note', stats.outbox_approved || 0));
  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? t('kpi.to_approve', {n: stats.outbox_pending}) : '',
    stats.needs_human ? t('kpi.handed', {n: stats.needs_human}) : '',
  ].filter(Boolean).join(' · ') || t('kpi.nothing_to_approve');
  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span>' + tBold('kpi.fact_companies', stats.companies || 0) + '</span>' +
    '<span>' + tBold('kpi.fact_unread', stats.unprofiled || 0) + '</span>' +
    '<span>' + tBold('kpi.fact_signals', stats.signals_confirmed || 0) + '</span>';

  el.innerHTML = '<div class="kpis">' +
    card(t('kpi.found'), fmtN(stats.prospects || 0), sparkBars(tr.found, t('kpi.found_spark')),
         tBold('kpi.this_week', last7(tr.found))) +
    sent +
    card(t('kpi.conversations'), fmtN(stats.open_conversations || 0), sparkBars(tr.replies, t('kpi.replies_spark')),
         tBold('kpi.new_14', sum(tr.replies))) +
    card(t('kpi.waiting'), fmtN(waiting), '', escHtml(waitNote)) +
  '</div>';
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const tr = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (tr && Array.isArray(tr.days)) { _trend = tr; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) {  // offline: keep the last numbers; Today's queue says so
    if (!el.querySelector('.legend-rows')) el.innerHTML = offlineLine();
    return;
  }
  if (data.error) { el.innerHTML = '<p class="muted small">' + escHtml(t('today.funnel_error')) + '</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    [t('stage.to_write'), pipe.to_write || 0, 'c-accent-soft'],
    [t('stage.contacted'), pipe.contacted || 0, 'c-accent'],
    [t('stage.replied'), pipe.replied || 0, 'c-active'],
    [t('stage.interested'), pipe.interested || 0, 'c-good'],
    [t('stage.ready'), pipe.ready_to_meet || 0, 'c-note'],
    [t('stage.closed'), pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted small">' + escHtml(t('today.funnel_empty')) + '</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="' + escHtml(t('today.by_stage')) + '">' +
      stages.filter(s => s[1] > 0).map(s =>
        '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + escHtml(s[0]) + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + escHtml(s[0]) + '</span>' +
      '<span class="legend-val">' + fmtN(s[1]) + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const [data, runs] = await Promise.all([api('/api/activity'), api('/api/runs')]);
  if (!Array.isArray(data)) {  // offline: keep what's shown; Today's queue says so
    if (!el.querySelector('.timeline')) el.innerHTML = offlineLine();
    return;
  }
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    // The Runs card beside this one may list imports and searches: point
    // at it rather than claim nothing has happened.
    const ran = Array.isArray(runs) && runs.length > 0;
    el.innerHTML = '<p class="muted small">' +
      escHtml(t(ran ? 'today.activity_empty_runs' : 'today.activity_empty')) + '</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
      '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(agentName(a.agent)) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? fmtN(n) : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
      '<span class="name">' + escHtml(runName(r)) + '</span>' + badge(r.status) +
      '<span class="when">' + relTime(r.started_at) + '</span>' +
      '<span class="stats">' + escHtml(t('runs.records_n', {n: r.records || 0})) + ' · ' +
        (r.cost_usd ? escHtml(fmtUsd(r.cost_usd, 4)) : escHtml(t('runs.free'))) + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (Today, Email module; disappears once it's done) ──

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }
  _setup = data;

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  // The first unfinished required step is the one to do now.
  const next = data.checks.find(c => c.required && !c.done);
  const renderCheck = (c, optional) =>
    '<div class="check-item' + (c === next ? ' current' : '') + '">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(checkLabel(c)) +
          (optional ? '<span class="optional-tag">' + escHtml(t('common.optional')) + '</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(checkHelp(c)) + '</div>' : '') +
        (!c.done && c.id === 'warmup_service' ? warmupServiceActions(c) : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);
  const done = required.filter(c => c.done).length;

  el.innerHTML =
    '<div class="card setup-card">' +
      '<div class="setup-head"><div><h3>' + escHtml(t('setup.title')) + '</h3>' +
        '<p class="muted">' + escHtml(t('setup.lede')) + '</p></div>' +
        '<span class="setup-pct">' + escHtml(t('setup.done', {done: done, total: required.length})) + '</span></div>' +
      '<div class="bar thick" style="margin-bottom:8px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
      required.map(c => renderCheck(c, false)).join('') +
      (optional.length
        ? '<details style="margin-top:10px"><summary class="muted small">' +
          escHtml(t('setup.optional_n', {n: optional.length})) + '</summary>' +
          '<div style="margin-top:6px">' +
          optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
        : '') +
    '</div>';
}

// The warmup step: get TrulyInbox, paste its key, or say you use another service.
function warmupServiceActions(c) {
  return '<div class="btn-group warmup-actions">' +
      '<a class="btn btn-primary btn-sm" href="' + safeHref(c.link) + '" target="_blank" ' +
        'rel="noopener sponsored">' + escHtml(t('warmup.get')) + '</a>' +
      '<input class="form-input" id="trulyinbox-key" type="password" autocomplete="off" ' +
        'placeholder="' + escHtml(t('warmup.key_ph')) + '" aria-label="' + escHtml(t('warmup.key_label')) + '" ' +
        'onkeydown="if (event.key === \'Enter\') connectTrulyInbox()">' +
      '<button class="btn btn-secondary btn-sm" id="trulyinbox-go" onclick="connectTrulyInbox()">' + escHtml(t('warmup.connect')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="useOtherWarmup()">' + escHtml(t('warmup.other')) + '</button>' +
    '</div>';
}

async function connectTrulyInbox() {
  const input = document.getElementById('trulyinbox-key');
  const go = document.getElementById('trulyinbox-go');
  if (!input || !input.value.trim()) return;
  if (go) go.disabled = true;
  const data = await api('/api/trulyinbox/connect', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: input.value.trim()}),
  });
  if (go) go.disabled = false;
  if (data && data.success) {
    input.value = '';
    showToast(data.message || t('warmup.connected'), 'success');
    loadSetupStatus();
  } else {
    showToast((data && data.message) || t('warmup.failed'), 'error');
  }
}

async function useOtherWarmup() {
  const data = await api('/api/trulyinbox/other', {method: 'POST'});
  if (data && data.success) loadSetupStatus();
}

// ── Signals: Harvey proposes, you confirm ──

// A server-written text in this dashboard's language: the translation is
// used only while the server's English still matches the English it was
// made from, so a changed description never shows a stale translation.
function tServer(key, english) {
  if (getLang() === 'en' || !english) return english || '';
  return hasKey('en', key) && t(key, null, 'en') === english ? t(key) : english;
}

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  markLoadFailed('signals', !data || data.error);
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [[t('sig.confirmed'), s.confirmed || 0, 'c-good'], [t('sig.waiting'), s.proposed || 0, 'c-wait'],
                 [t('sig.off'), s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + escHtml(p[0]) + '</span>' +
      '<span class="legend-val">' + fmtN(p[1]) + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(tServer('module.' + g.key + '.name', g.label)) + '</h3><p>' +
        escHtml(tServer('module.' + g.key + '.about', g.blurb)) + '</p>' +
      '</div>' +
      (undecided
        ? '<div class="btn-row">' +
            '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
              ', \'confirmed\')">' + escHtml(t('sig.confirm_all', {n: undecided})) + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
              ', \'rejected\')">' + escHtml(t('sig.skip_all', {n: undecided})) + '</button>' +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigLabel(sig) {
  return tServer('sigc.' + sig.code + '.label', sig.label || sig.code);
}

const WHEN_KNOWN = ['source', 'search', 'enrich', 'judged'];

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  // Checked either way, and the rest: "not checked" is unknown, never "no".
  const st = sig.states || {};
  const seen = st.true || st.false
    ? '<span class="cost-chip" title="' + escHtml(t('sig.states_hint')) + '">' +
        escHtml(t('sig.states', {yes: st.true || 0, no: st.false || 0, unk: st.unknown || 0})) + '</span>'
    : sig.companies
      ? '<span class="cost-chip">' + escHtml(t('sig.seen', {n: sig.companies})) + '</span>' : '';
  // When in a search the fact can be known: from the source itself, from a
  // search, by reading the company, or by a model's judgement.
  const when = WHEN_KNOWN.includes(sig.when_known)
    ? '<span class="cost-chip" title="' + escHtml(t('sig.when_hint')) + '">' +
        escHtml(t('sig.when.' + sig.when_known)) + '</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">' + escHtml(t('sig.floor', {p: Math.round(sig.confidence_floor * 100)})) + '</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
        '],\'rejected\')">' + escHtml(t('sig.turn_off')) + '</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">' + escHtml(t('sig.turn_on')) + '</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">' + escHtml(t('sig.confirm')) + '</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'rejected\')">' + escHtml(t('sig.skip')) + '</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sigLabel(sig)) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(tServer('sigc.' + sig.code + '.desc', sig.description)) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(tServer('sigc.' + sig.code + '.cost', sig.cost_note) || t('sig.cost_unknown')) + '</span>' +
        when + floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(status === 'confirmed' ? t('sig.toast_confirmed', {n: data.changed})
                                     : t('sig.toast_off', {n: data.changed}), 'success');
  } else {
    showToast(failMsg(data, t('sig.toast_failed')), 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  // Without the providers there is nothing to estimate or run against.
  markLoadFailed('discover', !data || !data.providers);
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  // A source or preset whose module is off isn't offered (Surse turns it on).
  const providers = data.providers.filter(p => p.enabled !== false);
  if (!providers.some(p => p.key === _provider)) _provider = null;
  _provider = _provider || [data.selected, data.default].find(k => providers.some(p => p.key === k)) ||
    (providers[0] || {}).key || null;
  const presetSel = document.getElementById('disc-preset');
  if (presetSel && data.presets) {
    Array.from(presetSel.options || []).forEach(o => {
      if (o.value) o.hidden = o.disabled = data.presets[o.value] === false;
    });
    if (presetSel.value && data.presets[presetSel.value] === false) presetSel.value = '';
  }
  const pv = (p, field) => tServer('prov.' + p.key + '.' + field, p[field]);
  el.innerHTML = (providers.length ? '' : emptyState(icon('layers'), escHtml(t('disc.none_on')),
      escHtml(t('disc.none_on_copy')), true,
      '<button type="button" class="btn btn-secondary btn-sm" onclick="goTab(\'modules\')">' +
        escHtml(t('disc.open_modules')) + '</button>')) +
    '<div class="prov-grid">' + providers.map(p => {
    const ready = p.configured
      ? '<span class="badge t-good">' + escHtml(t('disc.ready')) + '</span>'
      : '<span class="badge t-waiting">' + escHtml(t('disc.needs_key')) + '</span>';
    return '<button type="button" class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" aria-pressed="' + (p.key === _provider) + '" onclick="pickProvider(' + jsq(p.key) + ')">' +
      '<div class="prov-head"><h3>' + escHtml(pv(p, 'label')) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(pv(p, 'blurb')) + '</div>' +
      '<div class="row"><span class="k">' + escHtml(t('disc.cost')) + '</span><span class="v">' +
        escHtml(pv(p, 'cost_note')) + '</span></div>' +
      '<div class="row"><span class="k">' + escHtml(t('disc.free')) + '</span><span class="v">' +
        escHtml(pv(p, 'free_tier')) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">' + escHtml(t('disc.setup')) + '</span><span class="v">' +
          escHtml(t('disc.env_keys', {keys: p.env_keys.join(', ')})) + ' &middot; ' +
          '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
          escHtml(t('disc.get_key')) + '</a></span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(pv(p, 'caveat')) + '</div>' : '') +
    '</button>';
  }).join('') + '</div>';

  const running = data.running;
  // Reading sites can't be stopped part-way; only a discovery run can.
  document.getElementById('disc-stop').style.display = running && data.running_kind !== 'profile' ? '' : 'none';
  renderProfileCard(data.unprofiled || 0, running);
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    btn.disabled = true;
    btn.textContent = t('disc.estimate_first');
    showToast(data.running_kind === 'profile' ? t('disc.profile_done') : t('disc.done'), 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = t('disc.estimate_first');
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  return {
    provider: _provider,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth').value) || 30,
    limit: Number(document.getElementById('disc-limit').value) || 100,
    max_spend: Number(document.getElementById('disc-cap').value) || 1,
    preset: (document.getElementById('disc-preset') || {}).value || '',
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted small">' + escHtml(t('disc.pricing')) + '</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml(failMsg(data && {off_module: data.off_module, error: data.error},
                      t('disc.estimate_failed'))) + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? escHtml(t('disc.free_amount')) : escHtml(fmtUsd(data.estimated_cost, 4))) + '</div>' +
    '<div class="muted small" style="margin-top:2px">' +
      escHtml(t('disc.queries', {n: data.query_count})) +
      (data.free ? '' : ' &middot; ' + escHtml(t('disc.cap_is', {cap: fmtUsd(cap, 2)}))) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' + escHtml(t('disc.over_cap')) + '</div>' : '') +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>' + escHtml(t('disc.more_queries', {n: data.queries.length - 40})) : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? t('disc.over')
    : (data.free ? t('disc.run_free') : t('disc.run_paid', {cost: fmtUsd(data.estimated_cost, 2)}));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  btn.disabled = true;
  btn.textContent = t('disc.running');
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (data && data.success) {
    showToast(t('disc.started', {n: data.queries}), 'success');
  } else {
    showToast((data && data.message) || t('disc.start_failed'), 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', {method: 'POST'});
  showToast(data && data.success ? t('disc.stopping')
                                 : failMsg(data, t('disc.stop_failed')), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

// Companies found but not looked at: reading their sites is free and
// model-free, and it is where phones and people come from.
function renderProfileCard(pending, running) {
  const el = document.getElementById('discover-profile');
  if (!el) return;
  if (!pending) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">' + escHtml(t('disc.read_title')) + '</div><div class="card"><div class="queue-item good bare">' +
    '<div class="qtext"><div class="qtitle">' + escHtml(t('disc.unread', {n: pending})) +
    '</div><div class="qdetail">' + escHtml(t('disc.read_detail')) + '</div></div>' +
    '<button class="btn btn-primary btn-sm" onclick="runProfile(this)"' + (running ? ' disabled' : '') + '>' +
    escHtml(running ? t('disc.busy') : t('disc.read_go')) + '</button></div></div>';
}

async function runProfile(btn) {
  btn.disabled = true;
  btn.textContent = t('disc.starting');
  const data = await api('/api/profile/run', {method: 'POST'});
  if (data && data.success) {
    showToast(data.pending ? t('disc.reading', {n: Math.min(data.pending, 200)}) : t('disc.all_read'), 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, t('disc.start_failed')), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>' + escHtml(t('disc.running')) + '</h2>' +
      '<p class="muted small">' + escHtml(t('disc.running_detail')) + '</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + escHtml(label) + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  if (report.profiled_companies !== undefined && report.new_companies === undefined) {
    el.innerHTML = '<div class="subhead">' + escHtml(t('disc.last_run')) + '</div><div class="stats-grid">' +
      stat(t('disc.sites_read'), fmtN(report.profiled_companies || 0), 'accent') +
      stat(t('disc.facts'), fmtN(report.profile_observations || 0)) +
      stat(t('runs.cost'), escHtml(t('runs.free'))) + '</div>' +
      (report.stopped ? '<div class="test-result error" style="margin-top:14px">' + escHtml(t('disc.stopped_early', {why: report.stopped})) + '</div>' : '');
    return;
  }
  el.innerHTML = '<div class="subhead">' + escHtml(t('disc.last_run')) + '</div>' +
    '<div class="stats-grid">' +
      stat(t('disc.new'), fmtN(report.new_companies || 0), 'accent') +
      stat(t('disc.known'), fmtN(report.known_companies || 0)) +
      stat(t('disc.observations'), fmtN(report.observations || 0)) +
      stat(t('disc.junk'), fmtN(report.junk || 0)) +
      (report.profiled_companies ? stat(t('disc.sites_read'), fmtN(report.profiled_companies)) : '') +
      stat(t('disc.actual_cost'), report.actual_cost ? escHtml(fmtUsd(report.actual_cost, 4)) : escHtml(t('runs.free'))) +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      escHtml(t('disc.stopped_early', {why: report.stopped})) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>' + escHtml(t('disc.problems')) + '</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

// verdict: the result tab shown, match / possible / no_match.
let _cohort = { require: new Set(), exclude: new Set(), ranges: {}, verdict: 'match' };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted small">' + escHtml(t('cohort.empty')) + '</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + escHtml(title) +
      ' <span class="hint">' + escHtml(hint) + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(\'' + key + '\',' + jsq(s.code) + ',this.checked)">' +
        '<span>' + escHtml(sigLabel(s)) + '</span>' +
        '<span class="n">' + fmtN(s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + escHtml(t('cohort.' + side)) + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div class="spacer"></div><div><div class="subhead" style="margin-top:0">' + escHtml(t('cohort.range')) +
      ' <span class="hint">' + escHtml(t('cohort.range_hint')) + '</span></div>' +
      '<div class="cohort-grid">' +
      numeric.map(s =>
        '<div class="cohort-pick cohort-range"><span>' + escHtml(sigLabel(s)) + '</span>' +
          bound(s.code, 'min') + '<span class="muted">' + escHtml(t('cohort.to')) + '</span>' + bound(s.code, 'max') + '</div>'
      ).join('') + '</div></div>' : '';

  el.innerHTML = col(t('cohort.must'), 'require', t('cohort.must_hint')) +
    '<div class="spacer"></div>' +
    col(t('cohort.must_not'), 'exclude', t('cohort.must_not_hint')) + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || {min: null, max: null};
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  const verdict = _cohort.verdict || 'match';
  if (!require.length && !exclude.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">' + escHtml(t('cohort.loading')) + '</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude, ranges: ranges,
                          verdict: verdict, offset: offset, limit: COHORT_PAGE}),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' +
      escHtml(failMsg({off_module: data.off_module, error: data.error}, '')) + '</div>' : '';
    return;
  }

  // Every company lands in one of three tabs; "possible" is not checked yet.
  const counts = data.counts || {match: data.size};
  const shown = data.total !== undefined ? data.total : data.size;
  let html = '<div class="cohort-out">' +
    '<div class="segmented" role="group" aria-label="' + escHtml(t('cohort.tabs_label')) + '">' +
    ['match', 'possible', 'no_match'].map(v =>
      '<button class="' + (v === verdict ? 'on' : '') + '" aria-pressed="' + (v === verdict) +
        '" onclick="setCohortVerdict(\'' + v + '\')">' + escHtml(t('cohort.tab.' + v)) +
        ' <span class="n">' + fmtN(counts[v] || 0) + '</span></button>').join('') + '</div>' +
    '<div class="cohort-size" style="margin-top:14px">' + fmtN(shown) + '</div>' +
    '<div class="muted small" style="margin-top:2px">' +
      escHtml(verdict === 'match' ? t('cohort.match', {n: shown}) : t('cohort.count.' + verdict, {n: shown})) + '</div>' +
    '<div class="muted small" style="margin-top:2px">' + escHtml(t('cohort.explain.' + verdict)) + '</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>' + escHtml(t('cohort.company')) + '</th><th>' + escHtml(t('cohort.domain')) + '</th><th>' +
      escHtml(t('cohort.industry')) + '</th><th>' + escHtml(t('cohort.location')) + '</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < shown;
    html += '<div class="pager-row">' +
      '<span class="muted small">' + escHtml(t('page.range', {from: first, to: last, total: shown})) + '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
        (hasPrev ? '' : ' disabled') + '>' + escHtml(t('page.prev')) + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
        (hasNext ? '' : ' disabled') + '>' + escHtml(t('page.next')) + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">' + escHtml(t('cohort.export')) + '</button>' +
      '</div>';
  } else if (shown === 0) {
    html += '<p class="muted small" style="margin-top:12px">' +
      escHtml(t(verdict === 'match' ? 'cohort.none' : 'cohort.none_tab')) + '</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({code: code, min: r.min, max: r.max}));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({require: [..._cohort.require].join(','),
                                  exclude: [..._cohort.exclude].join(',')});
  // The tab on screen; the CSV says each company's verdict either way.
  if (_cohort.verdict && _cohort.verdict !== 'match') qs.set('verdict', _cohort.verdict);
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = t('export.exporting');
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'grup-' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = t('cohort.export');
  } catch (error) {
    button.textContent = t('export.retry');
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

function setCohortVerdict(verdict) {
  _cohort.verdict = verdict;
  _cohortOffset = 0;
  runCohort();
}

// ── Settings ──

// Saving is only offered once the saved values have been read: a form
// filled from nothing would look like a fresh install and invite
// overwriting real keys.
let _settingsLoaded = false;

async function loadSettings() {
  const data = await api('/api/settings');
  const errEl = document.getElementById('settings-error');
  _settingsLoaded = !!data;
  markLoadFailed('settings', !data);
  if (!data) { if (errEl) errEl.innerHTML = offlineState(); return; }
  if (errEl) errEl.innerHTML = '';

  applyEnvKeys(data.env_keys);

  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, baseKey) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? t('set.saved_ph') : t(baseKey);
  };
  const tag = (id, ok) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? t('set.tag_set') : t('set.tag_not_set');
  };

  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'set.ph.gmail_secret');
  savedPh('smtp-pass', data.smtp_password_set, 'set.ph.smtp_pass');
  savedPh('imap-pass', data.imap_password_set, 'set.same_smtp_pass');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('instantly-key', data.instantly_api_key_set, 'set.ph.instantly');
  savedPh('reoon-key', data.reoon_api_key_set, 'set.ph.reoon');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, 'set.ph.zerobounce');
  savedPh('hunter-key', data.hunter_api_key_set, 'set.ph.hunter');
  savedPh('linkedin-password', data.linkedin_password_set, 'set.ph.password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'set.ph.cf_token');

  const chip = (id, authorized, started, cmd) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (authorized) { el.className = 'email-tag verified'; el.textContent = t('set.authorized'); }
    else if (started) { el.className = 'email-tag risky'; el.textContent = t('set.run_cmd', {cmd: cmd}); }
    else { el.className = 'email-tag guess'; el.textContent = t('set.not_set_up'); }
  };
  chip('gmail-status', data.gmail_authorized, data.gmail_client_secret_set, 'harvey gmail auth');
  chip('m365-status', data.m365_authorized, data.m365_client_id, 'harvey microsoft auth');
  tag('reoon-status', data.reoon_api_key_set);
  tag('zerobounce-status', data.zerobounce_api_key_set);
  tag('hunter-status', data.hunter_api_key_set);
}

// Settings offers the .env keys of the modules that are on: a field (or a
// card) marked data-env="KEY ..." shows while any of its keys is offered.
function applyEnvKeys(keys) {
  if (!Array.isArray(keys)) return;
  const on = new Set(keys);
  document.querySelectorAll('[data-env]').forEach(el => {
    el.hidden = !el.dataset.env.split(/\s+/).some(k => on.has(k));
  });
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, t('set.saved_gmail')).then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  saveEnv(payload, t('set.saved_smtp')).then(loadSettings);
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast(t('set.m365_need_id'), 'error'); return; }
  saveEnv({M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim()},
          t('set.saved_m365')).then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast(t('set.need_key'), 'error'); return; }
  saveEnv(payload, t('set.saved_verifiers')).then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  if (!_settingsLoaded) { showToast(t('set.not_loaded'), 'error'); return; }
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || t('set.save_failed'), 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast(t('set.need_new_key'), 'error'); return; }
  saveEnv({INSTANTLY_API_KEY: key}, t('set.saved_instantly'));
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, t('set.saved_linkedin'));
}

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, t('set.saved_cf'));
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">' + escHtml(t('set.testing')) + '</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">' + escHtml(t(failedOnServer() ? 'offline.server_short' : 'set.unreachable')) + '</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadHarveyStatus() {
  const data = await api('/api/harvey/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    // "Offline" only when nothing answered; a server that answered with an
    // error is up, and restarting the dashboard isn't the next step.
    const server = failedOnServer();
    const word = t(server ? 'status.server_error' : 'status.offline');
    headerDot.className = 'status-dot offline';
    headerText.textContent = word;
    // The Controls card can't know either: say so instead of "Stopped".
    document.getElementById('control-dot').className = 'dot offline';
    const off = document.getElementById('control-label');
    off.className = 'label';
    off.textContent = word;
    document.getElementById('control-meta').textContent = server
      ? t('ctl.status_unknown_server', {status: _lastFail}) : t('ctl.status_unknown');
    document.getElementById('btn-start').style.display = 'none';
    document.getElementById('btn-stop').style.display = 'none';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? t('status.running') : t('status.stopped');
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? t('ctl.running') : t('ctl.stopped');

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; ' + escHtml(t('ctl.started', {when: formatDate(data.started_at)}));
    meta.innerHTML = info;
  } else {
    meta.textContent = t('ctl.idle');
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/harvey/start', {method: 'POST'});
  if (data && data.success) showToast(t('ctl.started_toast'), 'success');
  else showToast((data && data.message) || t('ctl.start_failed'), 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/harvey/stop', {method: 'POST'});
  if (data && data.success) showToast(t('ctl.stopped_toast'), 'success');
  else showToast((data && data.message) || t('ctl.stop_failed'), 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const data = await api('/api/harvey/logs');
  const el = document.getElementById('log-viewer');
  if (!data) {
    el.textContent = failedOnServer() ? t('offline.server_short') : t('ctl.logs_unreadable');
  } else if (data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = t('ctl.no_logs');
  }
}

// ── Usage ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + t('use.calls_today', {who: label}) + ' <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
      escHtml(st.provider === 'codex' ? 'Codex' : t('use.brain')) + ' <b>' + t('use.paused') + '</b>: ' +
      escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="' + escHtml(t('email.status_title', {s: m.label})) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? t('use.quota_codex') : t('use.quota_claude');
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex' ? t('use.quota_codex_none') : t('use.quota_claude_none');
    return html + '<p class="muted">' + escHtml(why) + ' ' + escHtml(t('use.quota_fallback')) + '</p>';
  }
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? t('use.resets', {when: formatDate(w.resets_at)}) : '';
    const lkey = 'use.window.' + key;
    const label = t(lkey) === lkey ? key : t(lkey);
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(label) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const tot = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + escHtml(label) + '</div>' +
      '<div class="value">' + fmtN(p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">' + escHtml(t('use.calls')) + '</span>' +
        '<span class="chip">' + escHtml(t('use.out')) + ' <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">' + escHtml(t('use.in')) + ' <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">' + escHtml(t('use.cached')) + ' <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card(t('use.today'), tot.today) + card(t('use.week'), tot.week) + card(t('use.month'), tot.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>' + escHtml(t('use.per_day')) + ' <span class="hint muted">' +
      escHtml(t('use.per_day_hint', {n: days.length, peak: fmtTokens(maxOut)})) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="' + escHtml(t('use.per_day')) + '">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + fmtTokens(d.output_tokens) + ' · ' +
          escHtml(t('use.calls_n', {n: d.calls || 0})) + '"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
        escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, field) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    let h = '<div class="card flush"><div class="card-head"><h3>' + escHtml(title) + '</h3></div><table><thead><tr>' +
      '<th>' + escHtml(t('use.col.' + field)) + '</th><th>' + escHtml(t('use.calls')) + '</th><th class="num">' +
      escHtml(t('use.input')) + '</th><th class="num">' + escHtml(t('use.output')) + '</th><th class="num">' +
      escHtml(t('use.cache_read')) + '</th></tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td class="cell-main">' + escHtml(String(r[field] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
          (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + fmtN(r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), escHtml(t('use.empty_title')), escHtml(t('use.empty_copy')));
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table(t('use.by_provider'), providers, 'provider') : '') +
      table(t('use.by_agent'), data.by_agent, 'agent') +
      table(t('use.by_task'), data.by_task, 'task') +
      table(t('use.by_model'), data.by_model, 'model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts (Email module) ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), escHtml(t('ob.error')), escHtml(data.error), true);
    list.innerHTML = '';
    return;
  }

  const pending = data.pending || [];
  _desk.items = pending;
  _desk.footer = data.footer || '';
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  const approvable = pending.length - pending.filter(p => heldReason(p)).length;
  actions.innerHTML =
    (pending.length && !data.paused && approvable
      ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" title="' +
        escHtml(t('ob.approve_all_title')) + '">' + escHtml(t('ob.approve_all', {n: approvable})) + '</button>' : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' + escHtml(t('ob.resume')) + '</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' + escHtml(t('ob.pause')) + '</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = sendingModeBanner(_sendingMode) + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>' + escHtml(t('ob.paused')) + '</b>' +
        '<p>' + escHtml(t('ob.paused_detail', {why: pauseReason(data.paused)})) + '</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>' + escHtml(t('ob.postal')) + '</b>' +
        '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), escHtml(t('ob.empty')),
      escHtml(_sendingMode && _sendingMode.mode === 'autopilot' ? t('ob.empty_auto') : t('ob.empty_copy'))) +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + escHtml(title) + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + escHtml(c[0]) + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  list.innerHTML =
    table(t('ob.approved'), data.approved, [
      [t('ob.to'), r => r.to_email], [t('ob.step'), r => r.step], [t('ob.subject'), r => r.subject],
      [t('ob.sends'), r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    table(t('ob.sent'), data.sent, [
      [t('ob.to'), r => r.to_email], [t('ob.step'), r => r.step], [t('ob.subject'), r => r.subject],
      [t('ob.sent_at'), r => formatDate(r.sent_at), true],
    ]) +
    table(t('ob.failed'), data.failed, [
      [t('ob.to'), r => r.to_email], [t('ob.status'), r => badge(r.status), false, true],
      [t('ob.reason'), r => r.error, true], [t('ob.updated'), r => formatDate(r.updated_at), true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'csrc.team_page',
  company_website: 'csrc.team_page',
  scraped_mailto: 'csrc.published',
  linkedin_search: 'csrc.linkedin',
  hunter_domain_search: 'csrc.hunter',
  job_board: 'csrc.job_board',
  web_search: 'csrc.web_search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + escHtml(k) + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, roleText(ctx.contact_title)].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? t('desk.score', {n: ctx.score}) : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  const src = own(CONTACT_SOURCES, ctx.contact_source);
  return '<div class="desk-context">' +
    row(t('desk.company'), escHtml(ctx.company)) +
    row(t('desk.website'), site) +
    row(t('desk.about'), escHtml(ctx.description)) +
    row(t('desk.contact'), who) +
    row(t('desk.found_via'), escHtml(src ? t(src) : ctx.contact_source)) +
    row(t('desk.email'), email) +
    row(t('desk.signals'), signals) +
    row(t('desk.campaign'), ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">' + escHtml(t('desk.campaign_note')) + '</span>' : '') +
    row(t('desk.notes'), escHtml(ctx.notes)) +
  '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): Harvey sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>' + t('mode.auto_on') + '</b>' +
      '<p>' + t('mode.auto_on_detail') + (m.held ? ' ' + t('mode.held_now', {n: m.held}) : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>' + t('mode.turn_off') + '</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>' + t('mode.approval') + '</b>' +
    '<p>' + (instantly ? t('mode.approval_instantly') : t('mode.approval_detail')) +
    ' <a href="#" onclick="goTab(\'settings\');return false">' + t('mode.about') + '</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">' + t('mode.unreadable') + '</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || t('mode.no_domains');
  let html = '<div class="card mode-card"><h3>' + t('mode.card_title', {mode: auto ? t('mode.autopilot') : t('mode.approval_word')}) + '</h3>' +
    '<p>' + t('mode.explain_approval') + (instantly ? ' ' + t('mode.explain_instantly') : '') + '</p>' +
    '<p>' + t('mode.explain_auto') + '</p>' +
    '<p class="muted">' + t('mode.yours', {domains: domains}) +
      (m.booking_page ? t('mode.booking', {page: escHtml(m.booking_page)}) : '') +
      t('mode.add_others', {file: escHtml(m.file)}) + '</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">' + t('mode.off_now') +
      (instantly ? ' ' + t('mode.off_instantly') : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>' + t('mode.turn_off') + '</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">' + t('mode.confirm_text') + '</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="' + t('mode.confirm_label') + '" ' +
        'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
        'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">' + t('mode.turn_on') + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">' + t('common.cancel') + '</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">' + t('mode.turn_on_dots') + '</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = {mode};
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? t('mode.toast_on')
        : t('mode.toast_off') + (res.requeued ? ' ' + t('mode.toast_requeued', {n: res.requeued}) : '') +
          (_sendingMode && _sendingMode.provider === 'instantly' ? ' ' + t('mode.toast_instantly') : ''), 'success');
    } else {
      showToast(failMsg(res, t('mode.toast_failed')), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="' + escHtml(t('desk.held_title')) + '">' + escHtml(t('desk.held')) + '</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(it.subject || t('desk.no_subject')) + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="' + escHtml(t('desk.reply_subject')) + '"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">' +
      '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || t('desk.no_subject')) + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="' + escHtml(t('desk.footer_title')) + '">' +
        escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">' + escHtml(t('common.save')) + '</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(null)">' + escHtml(t('common.cancel')) + '</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
        escHtml(t('desk.approve')) + ' <kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
        escHtml(t('desk.edit')) + ' <kbd>E</kbd></button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        escHtml(t('desk.reject')) + ' <kbd>R</kbd></button>';

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">' + escHtml(t('desk.to')) + ' <b>' + escHtml(cur.to_email) + '</b>' +
        '<span class="tag">' + escHtml(cur.kind === 'reply' ? t('desk.reply') : t('desk.email_n', {n: cur.step})) + '</span>' +
        '<span class="muted">' + escHtml(t('desk.sends', {when: formatDate(cur.send_at)})) + '</span></div>' +
      renderDeskContext(cur.context) +
      (heldReason(cur)
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>' + escHtml(t('desk.held_title')) + '</b>' +
          '<div>' + escHtml(heldReason(cur)) + '. ' + escHtml(t('desk.held_detail')) + '</div></div></div>'
        : '') +
      draft + footer +
      '<div class="desk-actions">' + actions +
        '<span class="muted small" style="margin-left:auto">' +
          escHtml(t('desk.of', {i: i + 1, n: items.length})) + '</span>' +
      '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast(t('desk.busy'), 'error');
  return true;
}

async function deskSave(id) {
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast(t('desk.need_both'), 'error'); return; }
  let data = null;
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: subject, body: body}),
    });
    if (await lockIfRefused(r)) return;
    data = await r.json();
  } catch {}
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast(t('desk.saved'), 'success');
    loadOutbox();
  } else {
    showToast((data && data.message) || t('desk.save_failed'), 'error');
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard: Escape closes menus and the company panel everywhere; the
// Outbox review keys work only on the Outbox, never while typing.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && (_drawer.id || document.querySelector('.menu.open'))) {
    const open = document.querySelector('.menu.open');
    const wasInMenu = open && open.contains(document.activeElement);
    closeMenus();
    if (_drawer.id) closeDrawer();
    else if (open && wasInMenu) { const b = open.querySelector('.menu-btn'); if (b) b.focus(); }
    return;
  }
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function outboxAct(id, action) {
  if (deskBusy()) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'approve' ? t('desk.approved') : t('desk.rejected'), 'success');
  else showToast(failMsg(data, t('desk.action_failed')), 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  if (deskBusy()) return;
  const data = await api('/api/outbox/approve-all', {method: 'POST'});
  if (data && data.success) showToast(t('desk.approved_all', {n: data.approved}) +
    (data.held ? ' ' + t('desk.held_left', {n: data.held}) : ''), 'success');
  else showToast(failMsg(data, t('desk.approve_all_failed')), 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'pause' ? t('ob.paused_toast') : t('ob.resumed_toast'), 'success');
  else showToast(failMsg(data, action === 'pause' ? t('ob.pause_failed') : t('ob.resume_failed')), 'error');
  if (currentTab === 'outbox') loadOutbox();
  else loadCurrentTab();
}

// What email will do with nobody watching, said outside the Email module
// too: hiding the module's tabs must never hide that mail can still go out,
// or the switch that stops it.
function sendingStateText(s) {
  if (s.paused) return {tone: 'idle', title: t('send.paused'), detail: t('send.paused_detail')};
  const parts = [];
  let title = t('send.quiet');
  if (s.autopilot) {
    title = t('send.live_auto');
    if (s.queued) parts.push(t('send.queued', {n: s.queued}));
  } else if (s.queued) {
    title = t('send.live_queued', {n: s.queued});
    parts.push(t('send.approval'));
  } else if (s.autopilot === false) {
    parts.push(t('send.approval'));
  }
  if (s.waiting) parts.push(t('send.waiting', {n: s.waiting}));
  return {tone: s.live ? 'wait' : 'good', title: title, detail: parts.join(' · ')};
}

function sendingButton(s) {
  return s.paused
    ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' + escHtml(t('ob.resume')) + '</button>'
    : '<button class="btn btn-danger btn-sm" onclick="sendingToggle(\'pause\')">' + escHtml(t('ob.pause')) + '</button>';
}

// Overview: a notice only while mail can go out and the module is hidden.
function renderOverviewSending(s) {
  const el = document.getElementById('overview-sending');
  if (!el) return;
  if (!s || !s.live || _modules.email) { el.innerHTML = ''; return; }
  const words = sendingStateText(s);
  el.innerHTML = '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>' + escHtml(words.title) + '</b>' +
    '<p>' + (words.detail ? escHtml(words.detail) + '. ' : '') + escHtml(t('send.hidden_note')) + '</p></div>' + sendingButton(s) + '</div>';
}

// Controls: always there, whatever the modules.
async function loadControlSending() {
  const el = document.getElementById('control-sending');
  if (!el) return;
  const s = await api('/api/sending/status');
  if (!s) { el.innerHTML = '<p class="muted">' + escHtml(t('send.unreadable')) + '</p>'; return; }
  const words = sendingStateText(s);
  // Secondary to the agent card above it: same layout, smaller headline,
  // so the sending state stays visible without competing.
  el.innerHTML = '<div class="status-big compact"><div class="dot ' + (s.paused ? 'stopped' : s.live ? 'running' : 'stopped') + '"></div>' +
      '<span class="label">' + escHtml(words.title) + '</span></div>' +
    (words.detail ? '<div class="status-meta">' + escHtml(words.detail) + '</div>' : '') +
    '<div class="btn-group">' + sendingButton(s) + '</div>';
}

// ── Shared table pieces: pager, selection, actions menu ──

// The search box's hint, short on a phone so it isn't cut mid-word.
function searchPh(prefix) {
  const narrow = typeof matchMedia === 'function' && matchMedia('(max-width: 700px)').matches;
  return prefix + (narrow ? '.search_ph_short' : '.search_ph');
}

const PAGE_SIZES = [25, 50, 100];

function pagerHtml(state, total, fn) {
  const from = total ? state.offset + 1 : 0;
  const to = Math.min(total, state.offset + state.limit);
  return '<div class="pager">' +
    '<span class="pager-range">' + escHtml(t('page.range', {from: from, to: to, total: total})) + '</span>' +
    '<label class="pager-size"><span>' + escHtml(t('page.per_page')) + '</span>' +
      '<select class="form-input" onchange="' + fn + '(\'size\', this.value)" aria-label="' + escHtml(t('page.per_page')) + '">' +
      PAGE_SIZES.map(n => '<option value="' + n + '"' + (n === state.limit ? ' selected' : '') + '>' + n + '</option>').join('') +
      '</select></label>' +
    '<span class="pager-btns">' +
      '<button class="btn btn-secondary btn-sm btn-icon" onclick="' + fn + '(\'prev\')"' + (state.offset > 0 ? '' : ' disabled') +
        ' aria-label="' + escHtml(t('page.prev')) + '" title="' + escHtml(t('page.prev')) + '">' + icon('chev-left') + '</button>' +
      '<button class="btn btn-secondary btn-sm btn-icon" onclick="' + fn + '(\'next\')"' + (to < total ? '' : ' disabled') +
        ' aria-label="' + escHtml(t('page.next')) + '" title="' + escHtml(t('page.next')) + '">' + icon('chev-right') + '</button>' +
    '</span></div>';
}

function pageMove(state, how, value) {
  if (how === 'size') { state.limit = Number(value) || 50; state.offset = 0; savePageSize(state.limit); }
  else if (how === 'prev') state.offset = Math.max(0, state.offset - state.limit);
  else if (how === 'next') state.offset += state.limit;
}

function savePageSize(n) { try { localStorage.setItem('harvey-page-size', String(n)); } catch { /* ignore */ } }
function loadPageSize() {
  try { const n = Number(localStorage.getItem('harvey-page-size')); return PAGE_SIZES.includes(n) ? n : 50; }
  catch { return 50; }
}

function closeMenus() {
  document.querySelectorAll('.menu.open').forEach(m => {
    m.classList.remove('open');
    const b = m.querySelector('.menu-btn');
    if (b) b.setAttribute('aria-expanded', 'false');
  });
}

function toggleMenu(btn, event) {
  if (event) event.stopPropagation();
  const menu = btn.closest('.menu');
  const open = !menu.classList.contains('open');
  closeMenus();
  if (open) {
    menu.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    const first = menu.querySelector('.menu-list button:not(:disabled)');
    if (first) first.focus();
  }
}

document.addEventListener('click', e => { if (!e.target.closest('.menu')) closeMenus(); });

function menuHtml(id, items) {
  return '<div class="menu" id="' + id + '">' +
    '<button class="btn btn-secondary menu-btn" aria-haspopup="true" aria-expanded="false" onclick="toggleMenu(this, event)">' +
      escHtml(t('actions.title')) + icon('chev-down') + '</button>' +
    '<div class="menu-list" role="menu">' + items.map(it => it.sep
      ? '<div class="menu-sep" role="separator">' + (it.label ? escHtml(it.label) : '') + '</div>'
      : '<button role="menuitem" onclick="closeMenus();' + it.action + '"' + (it.disabled ? ' disabled' : '') + '>' +
          icon(it.icon || 'download') + '<span>' + escHtml(it.label) + '</span>' +
          (it.note ? '<span class="menu-note">' + escHtml(it.note) + '</span>' : '') + '</button>').join('') +
    '</div></div>';
}

// Phones show the rows as cards without the table header, so the
// select-all box moves above them.
function selectPageMobile(allOn, fn) {
  return '<label class="sel-page-mobile"><input type="checkbox"' + (allOn ? ' checked' : '') +
    ' onchange="' + fn + '(this.checked)"><span>' + escHtml(t('sel.page')) + '</span></label>';
}

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

// ── Companies: phone and email first ──
//
// Server-side pages: /api/companies/search when there is a query, else
// /api/companies. The search box and the selection live outside the rows,
// so the 15-second refresh re-renders them without losing either.

let _co = { q: '', offset: 0, limit: 50, total: 0, selected: new Set(), timer: null, gen: 0 };

async function loadCompanies() {
  const el = document.getElementById('companies-list');
  if (!document.getElementById('company-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search grow">' + icon('discover') +
        '<input class="form-input" id="company-search" type="search" autocomplete="off" ' +
        'data-i18n-placeholder="' + searchPh('co') + '" data-i18n-aria="co.search_label" ' +
        'placeholder="' + escHtml(t(searchPh('co'))) + '" aria-label="' + escHtml(t('co.search_label')) + '" ' +
        'oninput="companySearchInput(this.value)"></label>' +
      '<div id="company-dupes"></div><div id="company-actions"></div>' +
      '</div><div id="dupes-panel"></div><div id="company-selbar"></div><div id="company-rows"></div>';
    document.getElementById('company-search').value = _co.q;
  }
  const rowsEl = document.getElementById('company-rows');
  const q = _co.q.trim();
  const gen = ++_co.gen;
  loadDuplicates();
  const res = q
    ? await api('/api/companies/search?limit=' + _co.limit + '&offset=' + _co.offset + '&q=' + encodeURIComponent(q))
    : await api('/api/companies?limit=' + _co.limit + '&offset=' + _co.offset);
  if (gen !== _co.gen) return;                       // a newer page or search is on its way
  if (!res || !document.getElementById('company-rows')) { rowsEl.innerHTML = offlineState(); return; }
  const data = res.companies || [];
  _co.total = res.total || 0;
  if (!data.length && _co.offset > 0 && _co.total > 0) {   // the list shrank: back to page one
    _co.offset = 0;
    return loadCompanies();
  }
  _companies = data;
  renderCompanyActions();
  renderCompanySelbar();
  if (!data.length) {
    const onlyExcluded = q && q.split(/\s+/).every(w => w.startsWith('-'));
    rowsEl.innerHTML = '<div class="table-card">' + (q
      ? emptyState(icon('discover'), escHtml(t('co.no_match')), escHtml(t(onlyExcluded ? 'co.only_excluded' : 'co.no_match_copy')))
      : emptyState(icon('companies'), escHtml(t('co.empty')), t('co.empty_copy'))) + '</div>';
    return;
  }
  const allOn = data.every(c => _co.selected.has(c.id));
  let html = selectPageMobile(allOn, 'selectCompanyPage') + '<div class="table-card"><table class="data co-table"><thead><tr>' +
    '<th class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.page')) + '"' + (allOn ? ' checked' : '') +
      ' onchange="selectCompanyPage(this.checked)"></th>' +
    '<th>' + escHtml(t('co.col.company')) + '</th><th>' + escHtml(t('co.col.phone')) + '</th><th>' +
    escHtml(t('co.col.email')) + '</th><th>' + escHtml(t('co.col.activities')) + '</th><th>' +
    escHtml(t('co.col.source')) + '</th></tr></thead><tbody>';
  data.forEach((c, i) => {
    const on = _co.selected.has(c.id);
    html += '<tr class="clickable' + (on ? ' selected' : '') + '" onclick="rowOpen(event, ' + i + ')">' +
      '<td class="sel" data-label=""><input type="checkbox" aria-label="' + escHtml(t('sel.row', {name: c.name})) + '"' +
        (on ? ' checked' : '') + ' onchange="selectCompany(' + i + ', this.checked)"></td>' +
      '<td class="co-name"><div class="cell-stack"><button type="button" class="link-btn cell-main" onclick="openCompany(' + jsq(c.id) + ')" ' +
        'aria-haspopup="dialog">' + escHtml(c.name) + '</button>' +
        '<span class="cell-sub">' + companyIdLine(c) + '</span></div></td>' +
      '<td class="co-phone" data-label="' + escHtml(t('co.col.phone')) + '">' + phoneCell(c) + '</td>' +
      '<td class="co-email" data-label="' + escHtml(t('co.col.email')) + '">' + emailCell(c) + '</td>' +
      '<td class="co-acts" data-label="' + escHtml(t('co.col.activities')) + '">' + companyActivities(c) + '</td>' +
      '<td class="co-src muted" data-label="' + escHtml(t('co.col.source')) + '">' + sourceLabel(c.source) + '</td></tr>';
  });
  rowsEl.innerHTML = html + '</tbody></table></div>' + pagerHtml(_co, _co.total, 'pageCompanies');
}

// ── Possible duplicates ──
//
// Pairs the resolver wasn't sure enough to merge on its own, side by side,
// for a person to decide; and the merges in force, each one undoable.

let _dup = { open: false, view: 'pending', pending: 0, rows: [], gen: 0, busy: false };

async function loadDuplicates() {
  const gen = ++_dup.gen;
  const res = await api('/api/duplicates?status=' + (_dup.open ? _dup.view : 'pending'));
  if (gen !== _dup.gen || !res) return;
  _dup.pending = res.pending || 0;
  _dup.rows = res.rows || [];
  renderDuplicates();
}

function renderDuplicates() {
  const btn = document.getElementById('company-dupes');
  if (btn) {
    btn.innerHTML = _dup.pending || _dup.open
      ? '<button type="button" class="chip-btn' + (_dup.pending ? ' on' : '') + '" aria-expanded="' + _dup.open +
        '" onclick="toggleDuplicates()">' + escHtml(t('dup.button', {n: _dup.pending})) + '</button>'
      : '';
  }
  const el = document.getElementById('dupes-panel');
  if (!el) return;
  if (!_dup.open) { el.innerHTML = ''; return; }
  const tab = (view, label) => '<button type="button"' + (_dup.view === view ? ' class="on" aria-pressed="true"' : ' aria-pressed="false"') +
    ' onclick="setDuplicatesView(' + jsq(view) + ')">' + escHtml(label) + '</button>';
  const rows = _dup.rows;
  const body = !rows.length
    ? '<p class="muted">' + escHtml(t(_dup.view === 'merged' ? 'dup.empty_merged' : 'dup.empty')) + '</p>'
    : rows.map(_dup.view === 'merged' ? mergedRow : duplicateRow).join('');
  el.innerHTML = '<div class="table-card dup-panel"><div class="dup-head"><div><h3>' + escHtml(t('dup.title')) + '</h3>' +
    '<p class="muted">' + escHtml(t('dup.lede')) + '</p></div>' +
    '<button type="button" class="btn btn-ghost btn-sm" onclick="toggleDuplicates()">' + escHtml(t('dup.close')) + '</button></div>' +
    '<div class="segmented dup-tabs" role="group" aria-label="' + escHtml(t('dup.title')) + '">' + tab('pending', t('dup.tab.pending', {n: _dup.pending})) + tab('merged', t('dup.tab.merged')) + '</div>' +
    body + '</div>';
}

function dupReasons(reasons) {
  return (reasons || []).map(r => escHtml(t('dup.reason.' + r.code, {value: r.value || ''}))).join(' · ');
}

function dupSide(c) {
  if (!c) return '<div class="dup-side"></div>';
  const place = String(c.location || '').replace(/,\s*Romania$/i, '');
  const phones = (c.phones || []).length ? c.phones : (c.phone ? [c.phone] : []);
  const domains = (c.domains || []).length ? c.domains : (c.domain ? [c.domain] : []);
  const line = parts => parts.filter(Boolean).join('<span class="dot-sep">·</span>');
  return '<div class="dup-side"><button type="button" class="link-btn cell-main" onclick="openCompany(' + jsq(c.id) + ')">' +
      escHtml(c.name || t('dup.no_name')) + '</button>' +
    '<div class="cell-sub">' + line([c.cui ? '<span class="mono">' + escHtml(t('co.cui', {cui: c.cui})) + '</span>' : '',
      place ? escHtml(place) : '']) + '</div>' +
    '<div class="cell-sub mono">' + (phones.length ? phones.map(escHtml).join(', ') : escHtml(t('co.no_phone'))) + '</div>' +
    (domains.length ? '<div class="cell-sub">' + domains.map(escHtml).join(', ') + '</div>' : '') +
    '<div class="cell-sub muted">' + line((c.sources || []).map(x => sourceLabel(String(x).replace(/^discover:/, '')))) + '</div></div>';
}

function duplicateRow(p) {
  return '<div class="dup-pair"><div class="dup-why"><b>' + escHtml(t('dup.score', {n: Math.round((p.score || 0) * 100)})) + '</b> ' +
      dupReasons(p.reasons) + '</div>' +
    '<div class="dup-sides">' + dupSide(p.a) + dupSide(p.b) + '</div>' +
    '<div class="btn-row"><button type="button" class="btn btn-primary btn-sm" onclick="decideDuplicate(\'merge\', ' +
      jsq(p.company_a) + ', ' + jsq(p.company_b) + ')">' + escHtml(t('dup.same')) + '</button>' +
    '<button type="button" class="btn btn-secondary btn-sm" onclick="decideDuplicate(\'separate\', ' +
      jsq(p.company_a) + ', ' + jsq(p.company_b) + ')">' + escHtml(t('dup.different')) + '</button></div></div>';
}

function mergedRow(m) {
  return '<div class="dup-pair"><div class="dup-why"><b>' + escHtml(t(m.decided_by === 'auto' ? 'dup.by_auto' : 'dup.by_you')) + '</b> ' +
      (m.score ? escHtml(t('dup.score', {n: Math.round(m.score * 100)})) + ' · ' : '') + dupReasons(m.reasons) + '</div>' +
    '<div class="dup-sides">' + dupSide(m.b) + dupSide(m.a) + '</div>' +
    '<div class="cell-sub">' + escHtml((m.b && m.b.name) || '') + ' ' + escHtml(t('dup.joined')) + ' ' + escHtml((m.a && m.a.name) || '') + '</div>' +
    '<div class="btn-row"><button type="button" class="btn btn-secondary btn-sm" onclick="undoMerge(' + jsq(m.merged) + ')">' +
      escHtml(t('dup.undo')) + '</button></div></div>';
}

function toggleDuplicates() {
  _dup.open = !_dup.open;
  _dup.view = 'pending';
  _dup.rows = [];
  renderDuplicates();
  loadDuplicates();
}

function setDuplicatesView(view) {
  _dup.view = view;
  _dup.rows = [];
  renderDuplicates();
  loadDuplicates();
}

function dupRefused(res) {
  const key = 'dup.refused.' + (res && res.error);
  return t(hasKey(LANG, key) ? key : 'dup.refused.other');
}

// One decision at a time, its buttons off until it is answered: a double
// click must not send the same merge twice.
async function dupPost(path, body) {
  if (_dup.busy) return undefined;
  _dup.busy = true;
  const panel = document.getElementById('dupes-panel');
  if (panel) panel.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    return await api(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
  } finally {
    _dup.busy = false;
  }
}

async function decideDuplicate(how, a, b) {
  const res = await dupPost('/api/duplicates/' + how, {a: a, b: b});
  if (res === undefined) return;
  if (!res || res.success === false) { showToast(dupRefused(res), 'error'); return loadDuplicates(); }
  showToast(t(how === 'merge' ? 'dup.toast.merged' : 'dup.toast.separate'));
  loadCompanies();
}

async function undoMerge(companyId) {
  const res = await dupPost('/api/duplicates/undo', {company_id: companyId});
  if (res === undefined) return;
  if (!res || res.success === false) { showToast(dupRefused(res), 'error'); return loadDuplicates(); }
  showToast(t('dup.toast.undone'));
  loadCompanies();
}

function companyIdLine(c) {
  const place = String(c.location || '').replace(/,\s*Romania$/i, '');
  return [c.cui ? '<span class="mono">' + escHtml(t('co.cui', {cui: c.cui})) + '</span>' : '',
          place ? escHtml(place) : ''].filter(Boolean).join('<span class="dot-sep">·</span>');
}

function phoneCell(c) {
  const main = c.phone_display || c.phone || '';
  if (!main) return '<span class="none">' + escHtml(t('co.no_phone')) + '</span>';
  const href = telHref(main);
  const others = (c.other_phones || []).length;
  const kind = c.phone_type ? t('phone.' + c.phone_type) : '';
  return '<div class="contact-cell">' +
    '<div class="contact-line">' + (href
      ? '<a class="mono tel" href="' + href + '" onclick="event.stopPropagation()">' + escHtml(main) + '</a>'
      : '<span class="mono">' + escHtml(main) + '</span>') +
      '<button class="copy" onclick="event.stopPropagation();copyText(' + jsq(main) + ', this)" aria-label="' +
        escHtml(t('common.copy')) + '" title="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>' +
    ((kind && kind !== 'phone.' + c.phone_type) || others
      ? '<div class="cell-sub">' + [kind && kind !== 'phone.' + c.phone_type ? escHtml(kind) : '',
          others ? escHtml(t('co.more_phones', {n: others})) : ''].filter(Boolean).join(' · ') + '</div>' : '') +
    '</div>';
}

function emailCell(c) {
  const emails = c.emails || [];
  if (!emails.length) return '<span class="none">' + escHtml(t('co.no_email')) + '</span>';
  const e = emails[0];
  const href = mailHref(e.email);
  const m = statusMeta(e.status || 'guess');
  return '<div class="contact-cell"><div class="contact-line">' +
    '<span class="mini-dot t-' + m.tone + '" title="' + escHtml(m.label) + '"></span>' +
    (href ? '<a class="email" href="' + href + '" onclick="event.stopPropagation()">' + escHtml(e.email) + '</a>'
          : '<span class="email">' + escHtml(e.email) + '</span>') +
    '<button class="copy" onclick="event.stopPropagation();copyText(' + jsq(e.email) + ', this)" aria-label="' +
      escHtml(t('common.copy')) + '" title="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>' +
    '<div class="cell-sub">' + [escHtml(m.label), e.name ? escHtml(e.name) : '',
      emails.length > 1 ? escHtml(t('co.more_emails', {n: emails.length - 1})) : ''].filter(Boolean).join(' · ') + '</div></div>';
}

function rowOpen(event, i) {
  if (event.target.closest('a, button, input, label, select')) return;
  const c = _companies[i];
  if (c) openCompany(c.id);
}

function selectCompany(i, on) {
  const c = _companies[i];
  if (!c) return;
  if (on) _co.selected.add(c.id); else _co.selected.delete(c.id);
  loadCompaniesSelectionOnly();
}

function selectCompanyPage(on) {
  _companies.forEach(c => { if (on) _co.selected.add(c.id); else _co.selected.delete(c.id); });
  loadCompaniesSelectionOnly();
}

// Re-mark the rows without fetching the page again.
function loadCompaniesSelectionOnly() {
  document.querySelectorAll('#company-rows tbody tr').forEach((tr, i) => {
    const c = _companies[i];
    const on = !!(c && _co.selected.has(c.id));
    tr.classList.toggle('selected', on);
    const box = tr.querySelector('td.sel input');
    if (box) box.checked = on;
  });
  const allOn = _companies.length > 0 && _companies.every(c => _co.selected.has(c.id));
  document.querySelectorAll('#company-rows thead input, #company-rows .sel-page-mobile input')
    .forEach(box => { box.checked = allOn; });
  renderCompanyActions();
  renderCompanySelbar();
}

function renderCompanySelbar() {
  const el = document.getElementById('company-selbar');
  if (!el) return;
  const n = _co.selected.size;
  el.innerHTML = n ? '<div class="selbar"><b>' + escHtml(t('sel.count', {n: n})) + '</b>' +
    '<button class="btn btn-ghost btn-sm" onclick="clearCompanySelection()">' + escHtml(t('sel.clear')) + '</button></div>' : '';
}

function clearCompanySelection() {
  _co.selected.clear();
  loadCompaniesSelectionOnly();
}

function renderCompanyActions() {
  const el = document.getElementById('company-actions');
  if (!el) return;
  const n = _co.selected.size;
  const all = _co.q.trim() ? t('actions.all_results', {n: _co.total}) : t('actions.all_companies', {n: _co.total});
  el.innerHTML = menuHtml('company-menu', [
    {sep: true, label: t('actions.selection', {n: n})},
    {label: t('actions.csv'), action: 'exportCompanies(\'csv\', true)', disabled: !n},
    {label: t('actions.json'), action: 'exportCompanies(\'json\', true)', disabled: !n},
    {sep: true, label: all},
    {label: t('actions.csv'), action: 'exportCompanies(\'csv\', false)', disabled: !_co.total},
    {label: t('actions.json'), action: 'exportCompanies(\'json\', false)', disabled: !_co.total},
  ]);
}

function exportCompanies(format, selection) {
  const body = selection ? {ids: [..._co.selected]} : (_co.q.trim() ? {q: _co.q.trim()} : {});
  const name = 'firme-' + (selection ? 'selectie' : _co.q.trim() ? 'cautare' : 'toate') + '-' + stamp() + '.' + format;
  postDownload('/api/export/companies.' + format, name, body);
}

function pageCompanies(how, value) {
  pageMove(_co, how, value);
  loadCompanies().then(() => {
    const top = document.getElementById('companies');
    if (top && how !== 'size') top.scrollIntoView({block: 'start'});
  });
}

function companySearchInput(value) {
  _co.q = value;
  _co.offset = 0;
  clearTimeout(_co.timer);
  _co.timer = setTimeout(loadCompanies, 250);
}

// What a company does, in words: its CAEN classes (the ones a search
// matched first), else the industry it came with. Names as the registry
// writes them, in Romanian.
function companyActivities(c, all) {
  const acts = c.activities || [];
  if (!acts.length) return c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '<span class="none">—</span>';
  const shown = [], seen = new Set();
  for (const a of acts) {
    if (seen.has(a.code)) continue;
    seen.add(a.code);
    if (all || (shown.length < 2 && (a.matched || !acts.some(x => x.matched)))) shown.push(a);
  }
  const more = seen.size - shown.length;
  return '<div class="acts">' + shown.map(a =>
    '<div class="act' + (a.matched ? ' matched' : '') + '" title="' + escHtml((a.name || '') + (a.revision ? ' (' + a.revision + ')' : '')) + '">' +
      '<span class="code">' + escHtml(a.code) + '</span><span class="aname">' +
      escHtml(a.name || a.name_en || '') + '</span></div>').join('') +
    (more > 0 ? '<div class="cell-sub">' + escHtml(t('co.more_acts', {n: more})) + '</div>' : '') + '</div>';
}

// ── The company panel ──

let _drawer = { id: null, data: null, contacts: null, opener: null };

async function openCompany(id) {
  _drawer = { id: id, data: null, contacts: null, opener: document.activeElement };
  const drawer = document.getElementById('drawer');
  document.getElementById('drawer-backdrop').hidden = false;
  drawer.hidden = false;
  document.body.classList.add('drawer-open');
  renderDrawer();
  const [data, contacts] = await Promise.all([
    api('/api/companies/' + encodeURIComponent(id)),
    api('/api/companies/' + encodeURIComponent(id) + '/contacts'),
  ]);
  if (_drawer.id !== id) return;
  _drawer.data = data || {error: true};
  _drawer.contacts = Array.isArray(contacts) ? contacts : [];
  renderDrawer();
  // The panel itself takes focus (Tab reaches the close button next), so
  // a mouse click doesn't light up a focus ring.
  drawer.focus({preventScroll: true});
}

function closeDrawer() {
  const opener = _drawer.opener;
  _drawer = { id: null, data: null, contacts: null, opener: null };
  document.getElementById('drawer').hidden = true;
  document.getElementById('drawer-backdrop').hidden = true;
  document.body.classList.remove('drawer-open');
  if (opener && opener.focus) opener.focus();
}

function renderDrawer() {
  const el = document.getElementById('drawer-body');
  const c = _drawer.data;
  const head = (title, sub) => '<div class="drawer-head"><div class="drawer-title"><h2 id="drawer-title">' + title + '</h2>' +
    (sub ? '<div class="drawer-sub">' + sub + '</div>' : '') + '</div>' +
    '<button class="btn btn-ghost btn-icon drawer-close" onclick="closeDrawer()" aria-label="' + escHtml(t('common.close')) + '">' +
    icon('x') + '</button></div>';
  if (!c) { el.innerHTML = head(escHtml(t('common.loading')), '') + '<div class="drawer-scroll"><div class="skeleton"></div><div class="skeleton"></div></div>'; return; }
  if (c.error) { el.innerHTML = head(escHtml(t('drawer.error')), '') ; return; }

  const section = (title, ico, body, count) => '<section class="dsec"><h3>' + icon(ico) + escHtml(title) +
    (count ? '<span class="count">' + fmtN(count) + '</span>' : '') + '</h3>' + body + '</section>';
  const none = text => '<p class="none">' + escHtml(text) + '</p>';

  const phones = [];
  const mainPhone = c.phone_display || c.phone;
  if (mainPhone) phones.push({n: mainPhone, kind: c.phone_type, main: true});
  (c.other_phones || []).forEach(n => phones.push({n: n}));
  const phoneRows = phones.map(p => {
    const href = telHref(p.n);
    const kind = p.kind ? t('phone.' + p.kind) : '';
    return '<div class="drow">' +
      (href ? '<a class="mono big tel" href="' + href + '">' + escHtml(p.n) + '</a>' : '<span class="mono big">' + escHtml(p.n) + '</span>') +
      '<span class="drow-meta">' + [p.main ? escHtml(t('drawer.main')) : '', kind && kind !== 'phone.' + p.kind ? escHtml(kind) : '']
        .filter(Boolean).join(' · ') + '</span>' +
      '<button class="copy" onclick="copyText(' + jsq(p.n) + ', this)" aria-label="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>';
  }).join('');

  const emailRows = (c.emails || []).map(e => {
    const href = mailHref(e.email);
    const m = statusMeta(e.status || 'guess');
    return '<div class="drow"><span class="mini-dot t-' + m.tone + '"></span>' +
      (href ? '<a class="email" href="' + href + '">' + escHtml(e.email) + '</a>' : '<span class="email">' + escHtml(e.email) + '</span>') +
      '<span class="drow-meta">' + [escHtml(m.label), escHtml(e.name), escHtml(roleText(e.title))].filter(Boolean).join(' · ') + '</span>' +
      '<button class="copy" onclick="copyText(' + jsq(e.email) + ', this)" aria-label="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>';
  }).join('');

  const people = (c.people || []).map(p =>
    '<div class="person"><span class="avatar">' + initials(...String(p.name).split(' ')) + '</span>' +
    '<div class="cell-stack"><span class="cell-main">' + escHtml(p.name) + '</span>' +
    (p.title ? '<span class="cell-sub">' + escHtml(roleText(p.title)) + '</span>' : '') + '</div></div>').join('');

  const contacts = (_drawer.contacts || []).map(p => {
    const name = [p.first_name, p.last_name].filter(Boolean).join(' ') || p.email || '—';
    const ph = p.phone ? (telHref(p.phone) ? '<a class="mono tel" href="' + telHref(p.phone) + '">' + escHtml(p.phone) + '</a>' : escHtml(p.phone)) : '';
    const em = p.email ? (mailHref(p.email) ? '<a class="email" href="' + mailHref(p.email) + '">' + escHtml(p.email) + '</a>' : escHtml(p.email)) + emailTag(p) : '';
    return '<div class="person"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
      '<div class="cell-stack"><span class="cell-main">' + escHtml(name) + '</span>' +
      (p.title ? '<span class="cell-sub">' + escHtml(roleText(p.title)) + '</span>' : '') +
      ([em, ph].filter(Boolean).length ? '<span class="person-lines">' + [em, ph].filter(Boolean).join('<br>') + '</span>' : '') +
      '</div><span class="cell-sub">' + sourceLabel(p.source) + '</span></div>';
  }).join('');

  const site = safeHref(c.website || (c.domain ? 'https://' + c.domain : ''));
  const facts = [
    [t('drawer.cui'), c.cui ? '<span class="mono">' + escHtml(c.cui) + '</span>' : ''],
    [t('drawer.address'), escHtml(c.address)],
    [t('drawer.place'), escHtml(String(c.location || '').replace(/,\s*Romania$/i, ''))],
    [t('drawer.website'), site ? '<a href="' + site + '" target="_blank" rel="noopener noreferrer">' +
      escHtml(String(c.website || c.domain).replace(/^https?:\/\//i, '')) + ' &#8599;</a>' : ''],
    [t('drawer.source'), sourceLabel(c.source) + (safeHref(c.source_url) ? ' · <a href="' + safeHref(c.source_url) +
      '" target="_blank" rel="noopener noreferrer">' + escHtml(t('drawer.evidence')) + '</a>' : '')],
    [t('drawer.added'), relTime(c.created_at)],
  ].filter(f => f[1]);

  el.innerHTML = head(escHtml(c.name), companyIdLine(c)) +
    '<div class="drawer-scroll">' +
    section(t('drawer.phones'), 'phone', phoneRows || none(t('drawer.no_phones')), phones.length) +
    section(t('drawer.emails'), 'mail', emailRows || none(t('drawer.no_emails')), (c.emails || []).length) +
    section(t('drawer.people'), 'contacts', people || none(t('drawer.no_people')), (c.people || []).length) +
    (contacts ? section(t('drawer.contacts'), 'layers', contacts, (_drawer.contacts || []).length) : '') +
    section(t('drawer.activities'), 'companies', companyActivities(c, true)) +
    section(t('drawer.details'), 'pin', '<dl class="facts-list">' + facts.map(f => '<dt>' + escHtml(f[0]) + '</dt><dd>' + f[1] + '</dd>').join('') + '</dl>') +
    '</div>' +
    '<div class="drawer-foot"><button class="btn btn-secondary btn-sm" onclick="exportOne(\'csv\')">' + icon('download') + 'CSV</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="exportOne(\'json\')">' + icon('download') + 'JSON</button></div>';
}

function exportOne(format) {
  if (!_drawer.id) return;
  postDownload('/api/export/companies.' + format, 'firma-' + (_drawer.data && _drawer.data.cui || _drawer.id) + '.' + format,
               {ids: [_drawer.id]});
}

// Focus stays inside the panel while it is open.
document.addEventListener('keydown', e => {
  if (e.key !== 'Tab' || !_drawer.id) return;
  const items = [...document.querySelectorAll('#drawer a[href], #drawer button:not(:disabled)')];
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  else if (!e.target.closest('#drawer')) { e.preventDefault(); first.focus(); }
});

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || t('fb.prompt'));
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(entityType === 'campaign' ? t('fb.saved_campaign') : t('fb.saved_contact'), 'success');
  else showToast((data && data.message) || t('fb.failed'), 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, t('fb.contact_prompt'));
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, t('fb.campaign_prompt'));
}

// ── Contacts: same pattern as Companies ──

let _ct = { q: '', filter: 'all', offset: 0, limit: 50, total: 0, counts: {}, selected: new Set(), timer: null, gen: 0 };
const CONTACT_FILTERS = ['all', 'todo', 'contacted', 'replied', 'closed'];

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search grow">' + icon('discover') +
        '<input class="form-input" id="contact-search" type="search" autocomplete="off" ' +
        'data-i18n-placeholder="' + searchPh('ct') + '" data-i18n-aria="ct.search_label" placeholder="' + escHtml(t(searchPh('ct'))) + '" ' +
        'aria-label="' + escHtml(t('ct.search_label')) + '" oninput="contactSearchInput(this.value)"></label>' +
      '<div class="segmented" id="contact-filters" role="group" data-i18n-aria="ct.filter_label" aria-label="' + escHtml(t('ct.filter_label')) + '"></div>' +
      '<div id="contact-actions"></div>' +
      '</div><div id="contact-selbar"></div><div id="contact-rows"></div>';
    document.getElementById('contact-search').value = _ct.q;
  }
  const filter = _modules.email ? _ct.filter : 'all';
  const gen = ++_ct.gen;
  const res = await api('/api/prospects?limit=' + _ct.limit + '&offset=' + _ct.offset +
    '&filter=' + encodeURIComponent(filter) + '&q=' + encodeURIComponent(_ct.q.trim()));
  if (gen !== _ct.gen) return;
  const rowsEl = document.getElementById('contact-rows');
  if (!res || !rowsEl) { if (rowsEl) rowsEl.innerHTML = offlineState(); return; }
  _ct.total = res.total || 0;
  _ct.counts = res.counts || {};
  if (!(res.prospects || []).length && _ct.offset > 0 && _ct.total > 0) { _ct.offset = 0; return loadProspects(); }
  _prospects = res.prospects || [];
  renderContacts();
}

function contactSearchInput(value) {
  _ct.q = value;
  _ct.offset = 0;
  clearTimeout(_ct.timer);
  _ct.timer = setTimeout(loadProspects, 250);
}

function setContactFilter(f) { _ct.filter = f; _ct.offset = 0; loadProspects(); }

function pageContacts(how, value) {
  pageMove(_ct, how, value);
  loadProspects().then(() => {
    const top = document.getElementById('prospects');
    if (top && how !== 'size') top.scrollIntoView({block: 'start'});
  });
}

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  if (!rowsEl) return;
  const email = !!_modules.email;
  filtersEl.hidden = !email;
  filtersEl.innerHTML = email ? CONTACT_FILTERS.map(f =>
    '<button class="' + (f === _ct.filter ? 'on' : '') + '" aria-pressed="' + (f === _ct.filter) + '" onclick="setContactFilter(\'' + f + '\')">' +
      escHtml(t('ct.filter.' + f)) + ' <span class="n">' + fmtN(_ct.counts[f] || 0) + '</span></button>'
  ).join('') : '';
  renderContactActions();
  renderContactSelbar();

  if (!_prospects.length) {
    rowsEl.innerHTML = '<div class="table-card">' + (_ct.q.trim() || _ct.filter !== 'all'
      ? emptyState(icon('discover'), escHtml(t('co.no_match')), escHtml(t('ct.no_match_copy')))
      : emptyState(icon('contacts'), escHtml(t('ct.empty')), escHtml(t('ct.empty_copy')))) + '</div>';
    return;
  }
  const allOn = _prospects.every(p => _ct.selected.has(p.id));
  let html = selectPageMobile(allOn, 'selectContactPage') + '<div class="table-card"><table class="data ct-table"><thead><tr>' +
    '<th class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.page')) + '"' + (allOn ? ' checked' : '') +
      ' onchange="selectContactPage(this.checked)"></th>' +
    '<th>' + escHtml(t('ct.col.name')) + '</th><th>' + escHtml(t('ct.col.company')) + '</th><th>' +
    escHtml(t('co.col.phone')) + '</th><th>' + escHtml(t('co.col.email')) + '</th>' +
    (email ? '<th>' + escHtml(t('ct.col.status')) + '</th>' : '') +
    '<th>' + escHtml(t('co.col.source')) + '</th><th>' + escHtml(t('ct.col.added')) + '</th>' +
    (email ? '<th></th>' : '') + '</tr></thead><tbody>';
  _prospects.forEach((p, i) => {
    const on = _ct.selected.has(p.id);
    const name = [p.first_name, p.last_name].filter(Boolean).join(' ');
    // A company cell can hold several numbers ("+40 …;+40 …"): show the first.
    const phones = String(p.phone || p.company_phone || '').split(/\s*[;,]\s*/).filter(Boolean);
    const phone = phones[0] || '';
    const phoneHref = telHref(phone);
    const mh = mailHref(p.email);
    html += '<tr class="' + (on ? 'selected' : '') + '">' +
      '<td class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.row', {name: name || p.email})) + '"' +
        (on ? ' checked' : '') + ' onchange="selectContact(' + i + ', this.checked)"></td>' +
      '<td class="ct-name"><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + (name ? escHtml(name) : '<span class="muted">' + escHtml(t('ct.inbox')) + '</span>') + '</span>' +
        '<span class="cell-sub">' + escHtml(roleText(p.title)) + '</span></div></div></td>' +
      '<td data-label="' + escHtml(t('ct.col.company')) + '"><div class="cell-stack">' +
        (p.company_id ? '<a href="#" class="cell-main co-link" onclick="openCompany(' + jsq(p.company_id) + ');return false">' +
          escHtml(p.company_name || p.company) + '</a>' : '<span class="cell-main">' + escHtml(p.company) + '</span>') +
        '<span class="cell-sub">' + escHtml(String(p.company_location || '').replace(/,\s*Romania$/i, '')) + '</span></div></td>' +
      '<td data-label="' + escHtml(t('co.col.phone')) + '">' + (phone
        ? '<div class="contact-line">' + (phoneHref ? '<a class="mono tel" href="' + phoneHref + '">' + escHtml(phone) + '</a>' : '<span class="mono">' + escHtml(phone) + '</span>') +
          '<button class="copy" onclick="copyText(' + jsq(phone) + ', this)" aria-label="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>' +
          (!p.phone ? '<div class="cell-sub">' + escHtml(t('ct.company_phone')) +
            (phones.length > 1 ? ' · ' + escHtml(t('co.more_phones', {n: phones.length - 1})) : '') + '</div>' : (p.phone_verified ? '<div class="cell-sub verified">' + escHtml(t('ct.phone_verified')) + '</div>' : ''))
        : '<span class="none">' + escHtml(t('co.no_phone')) + '</span>') + '</td>' +
      '<td data-label="' + escHtml(t('co.col.email')) + '">' + (p.email
        ? '<div class="contact-line">' + (mh ? '<a class="email" href="' + mh + '">' + escHtml(p.email) + '</a>' : '<span class="email">' + escHtml(p.email) + '</span>') +
          '<button class="copy" onclick="copyText(' + jsq(p.email) + ', this)" aria-label="' + escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div>' +
          '<div class="cell-sub">' + emailTag(p) + '</div>'
        : '<span class="none">' + escHtml(t('co.no_email')) + '</span>') + '</td>' +
      (email ? '<td data-label="' + escHtml(t('ct.col.status')) + '">' + badge(p.status) + '</td>' : '') +
      '<td class="muted" data-label="' + escHtml(t('co.col.source')) + '">' + sourceLabel(p.source) + '</td>' +
      '<td class="muted nowrap" data-label="' + escHtml(t('ct.col.added')) + '">' + relTime(p.created_at) + '</td>' +
      (email ? '<td><div class="cell-row">' + (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id
        ? '<button class="btn btn-ghost btn-sm" onclick="toggleOptOut(' + i + ')" ' +
          'aria-expanded="' + (_optOut.id === p.id) + '" title="' + escHtml(t('ct.optout_title')) + '">' +
          escHtml(p.status === 'opted_out' ? t('ct.why') : t('ct.optout_history')) + '</button>' : '') +
        '<button class="btn btn-ghost btn-sm row-action" onclick="fbProspect(' + i + ')" ' +
        'title="' + escHtml(t('ct.feedback_title')) + '">' + escHtml(t('ct.feedback')) + '</button></div></td>' : '') +
      '</tr>';
    if (email && _optOut.id === p.id) html += '<tr class="detail-row"><td colspan="9">' + optOutPanel(p) + '</td></tr>';
  });
  rowsEl.innerHTML = html + '</tbody></table></div>' + pagerHtml(_ct, _ct.total, 'pageContacts');
}

function selectContact(i, on) {
  const p = _prospects[i];
  if (!p) return;
  if (on) _ct.selected.add(p.id); else _ct.selected.delete(p.id);
  renderContacts();
}

function selectContactPage(on) {
  _prospects.forEach(p => { if (on) _ct.selected.add(p.id); else _ct.selected.delete(p.id); });
  renderContacts();
}

function renderContactSelbar() {
  const el = document.getElementById('contact-selbar');
  if (!el) return;
  const n = _ct.selected.size;
  el.innerHTML = n ? '<div class="selbar"><b>' + escHtml(t('sel.count', {n: n})) + '</b>' +
    '<button class="btn btn-ghost btn-sm" onclick="_ct.selected.clear();renderContacts()">' + escHtml(t('sel.clear')) + '</button></div>' : '';
}

function renderContactActions() {
  const el = document.getElementById('contact-actions');
  if (!el) return;
  const n = _ct.selected.size;
  el.innerHTML = menuHtml('contact-menu', [
    {sep: true, label: t('actions.selection', {n: n})},
    {label: t('actions.csv'), action: 'exportContacts()', disabled: !n},
    {sep: true, label: t('actions.all_contacts')},
    {label: t('actions.deliverable'), note: t('actions.deliverable_note'), action: 'exportAllContacts(false)'},
    {label: t('actions.everything'), note: t('actions.everything_note'), action: 'exportAllContacts(true)'},
  ]);
}

function exportAllContacts(everything) {
  downloadCsv('/api/export/prospects.csv?bom=true' + (everything ? '&all=true' : ''),
              'contacte-' + (everything ? 'toate' : 'sigure') + '-' + stamp() + '.csv');
}

function exportContacts() {
  postDownload('/api/export/prospects.csv', 'contacte-selectie-' + stamp() + '.csv', {ids: [..._ct.selected]});
}

// Why someone is opted out, and undoing it. Harvey never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = {id: null, data: null, armed: false, busy: false};
const OPT_OUT_RULES = {
  phrase: 'optout.rule.phrase',
  no_thanks: 'optout.rule.no_thanks',
  only_words: 'optout.rule.only_words',
  model: 'optout.rule.model',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = {id: null, data: null, armed: false, busy: false}; renderContacts(); return; }
  _optOut = {id: p.id, data: null, armed: false, busy: false};
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || {error: true};
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">' + escHtml(t('common.loading')) + '</div>';
  if (d.error) return '<div class="optout-panel muted">' + escHtml(t('optout.load_failed')) + '</div>';
  let html = '<div class="optout-panel"><h4>' + t(d.opted_out ? 'optout.why_title' : 'optout.history_title', {name: name}) + '</h4>';
  if (d.recorded) {
    const rule = own(OPT_OUT_RULES, d.rule);
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(rule ? t(rule) : d.rule || '') + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>' + escHtml(t('optout.no_record')) + ' ' +
      escHtml(d.message ? t('optout.last_message') : t('optout.no_reply')) + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || t('optout.they')) +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => escHtml(t('optout.you_undid')) + ' ' + relTime(h.at)).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">' +
      escHtml(t('optout.not_now', {status: statusMeta(d.status).label})) + '</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">' + escHtml(t('optout.confirm_text')) + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
        '>' + escHtml(t('optout.confirm')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">' + escHtml(t('common.cancel')) + '</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">' + escHtml(t('optout.undo')) + '</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true}),
  });
  if (res && res.success) showToast(t('optout.undone'), 'success');
  else showToast(failMsg(res, t('optout.undo_failed')), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || {error: true};
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  goTab('prospects');
  _ct.q = email || '';
  _ct.filter = 'all';
  _ct.offset = 0;
  const search = document.getElementById('contact-search');
  if (search) search.value = _ct.q;
  await loadProspects();
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? 'there' : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account Harvey sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast(t('camp.popup_blocked'), 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so Harvey can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast(t('camp.placeholder_left'), 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sent}),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch {}
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || t('camp.save_failed'), 'error');
  else if (!sent && res.sent) showToast(t('camp.already_replied'));
  else showToast(sent ? t('camp.marked') : t('camp.unmarked'), 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    [t('stage.contacted'), count(['contacted']), 'c-accent'],
    [t('stage.replied'), count(['replied']), 'c-active'],
    [t('stage.interested'), count(['interested', 'meeting']), 'c-good'],
    [t('stage.waiting'), count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push([t('stage.closed'), closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="' + escHtml(t('camp.progress')) + '">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + escHtml(p[0]) + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + escHtml(p[0]) + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), escHtml(t('camp.empty')), escHtml(t('camp.empty_copy')));
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when Harvey can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label class="check-line small">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; ' + escHtml(t('camp.sent_by_you')) + '</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: Harvey never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            escHtml(t('camp.open_gmail')) + '</button>' +
            '<span class="muted small">' + escHtml(t('camp.no_followups')) + '</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // Harvey sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="check-line small muted">' +
              '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
              escHtml(t('camp.already_emailed', {email: r.email})) + '</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">' + escHtml(t('camp.catch_all')) + '</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          escHtml(t('camp.open_gmail')) + '</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' + escHtml(t('camp.mark_sent')) + '</label>';
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + escHtml(s === 0 ? t('camp.first') : t('camp.followup')) +
        (step.delay_days ? ' &middot; ' + escHtml(t('camp.delay', {n: step.delay_days})) : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || t('camp.untitled')) + '</h3>' +
        '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">' + escHtml(t('ct.feedback')) + '</button></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + escHtml(t('camp.prospects', {n: pc})) + '</span>' +
        '<span>' + escHtml(t('camp.emails', {n: (c.sequence || []).length})) + '</span><span>' +
        escHtml(t('camp.created')) + ' ' + relTime(c.created_at) + '</span></div>' +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted small" style="padding-top:16px">' + escHtml(t('camp.no_steps')) + '</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: Harvey's cold emails (from the Outbox), the
// replies, Harvey's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const CONVO_FILTERS = [
  ['needs', c => !!c.draft || c.status === 'needs_human'],
  ['open', c => c.status !== 'closed'],
  ['closed', c => c.status === 'closed'],
  ['all', () => true],
];

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations');
  if (!data) { el.innerHTML = offlineState(); return; }
  _convos = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('conversations'), escHtml(t('cv.empty')), escHtml(t('cv.empty_copy')));
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][1])) _convoFilter = 'open';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[3];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="' + escHtml(t('cv.filter_label')) + '">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(\'' + f[0] + '\')">' + escHtml(t('cv.filter.' + f[0])) + ' <span class="n">' +
      _convos.filter(f[1]).length + '</span></button>').join('') + '</div></div>';
  // Handed-to-you first, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : c.draft ? 1 : 2;
  const shown = _convos.filter(filter[1]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), escHtml(t('cv.none')), escHtml(t('cv.none_copy'))) + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(convoCard).join('');
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ts: e.sent_at, email: e}))
    .concat((c.thread || []).map(m => ({ts: m.timestamp, msg: m})))
    .map(it => ({...it, key: +parseTs(it.ts)}))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || t('cv.them');
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>' + escHtml(t('desk.email_n', {n: it.email.step})) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.sender === 'harvey';
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      (sent ? 'Harvey' : escHtml(first)) + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + escHtml(t('cv.drafted')) + '</div>' +
      '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">' + escHtml(t('cv.review')) + icon('arrow') + '</button></div>'
    : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || t('cv.unknown');
  const stageKey = 'cv.stage.' + c.stage;
  const stage = c.stage && t(stageKey) !== stageKey ? '<span class="tag">' + escHtml(t(stageKey)) + '</span>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
      stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted small">' + escHtml(t('cv.no_messages')) + '</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

// Harvey answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + escHtml(t('cv.handed')) + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">' + escHtml(t('cv.hand_back')) + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">' + escHtml(t('common.close')) + '</button></div>';
  }
  if (c.status === 'open') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">' + escHtml(t('cv.close')) + '</button></div>';
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: Harvey never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + escHtml(t('cv.opted_out')) + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">' + escHtml(t('cv.why_opted_out')) + '</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">' + escHtml(t('cv.reopen')) + '</button></div>';
  }
  return '';
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status}),
  });
  if (data && data.success) showToast(status === 'open' ? t('cv.reopened') : t('cv.closed'), 'success');
  else showToast(failMsg(data, t('cv.update_failed')), 'error');
  loadConversations();
  loadToday();
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const [data, runs] = await Promise.all([api('/api/activity'), api('/api/runs')]);
  renderRunsTable(document.getElementById('activity-runs'), runs, 25);
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    // Runs listed above are activity too: never say "nothing yet" next to them.
    el.innerHTML = Array.isArray(runs) && runs.length
      ? emptyState(icon('activity'), escHtml(t('act.empty_log')), escHtml(t('act.empty_log_copy')))
      : emptyState(icon('activity'), escHtml(t('act.empty')), escHtml(t('act.empty_copy')));
    return;
  }
  let html = '<div class="card"><div class="timeline">';
  let day = '';
  for (const a of data) {
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    html += '<div class="tl-item' + (a.agent === 'main' ? ' quiet' : '') + '"><span class="tl-time">' + escHtml(timeOfDay(a.created_at)) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(agentName(a.agent)) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// ── Init & live refresh ──

async function init() {
  applyI18n();
  switchLangButtons();
  applyTheme(currentTheme());
  _co.limit = _ct.limit = loadPageSize();
  await loadModules();
  let tab = 'overview';
  try { tab = sessionStorage.getItem('harvey-tab') || 'overview'; } catch { /* ignore */ }
  goTab(tab);
  loadHarveyStatus();
}

function switchLangButtons() {
  document.querySelectorAll('.lang-switch button').forEach(b => {
    const on = b.dataset.lang === getLang();
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

init();

// Agent status: quick poll (not while locked: it would only be refused)
setInterval(() => { if (!_locked) loadHarveyStatus(); }, 8000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'today') loadLiveStatus();
}, 10000);

// Email-module nav counts stay live wherever you are, so "something needs
// me" is visible from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || _locked || !_modules.email || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 20000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden || _locked || document.querySelector('.menu.open')) return;
  switch (currentTab) {
    case 'overview': loadOverview(); break;
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadTodayActivity(); break;
    case 'companies': loadCompanies(); break;
    case 'prospects': if (!_optOut.id) loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); loadControlSending(); break;
  }
}, 15000);
