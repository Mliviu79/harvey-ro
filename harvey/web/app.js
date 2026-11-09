let currentTab = 'overview';
let _companies = [], _prospects = [], _campaigns = [];
let _desk = { items: [], i: 0 };
// Dashboard modules (visibility only): email is the Email module's switch;
// messages (Today, Outbox, Conversations) is on while any module with
// message channels is (email_outreach, npathi).
let _modules = { email: false, messages: false };

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
  // Work already waiting, or a running job that can't stop part-way: said
  // in the viewer's language from the job the server names.
  if (data && data.code === 'duplicate' && data.job) {
    return t('q.dup', {kind: jobKind(data.job.kind), label: data.job.label || '',
                       status: t('q.st.' + data.job.status)});
  }
  if (data && data.code === 'not_stoppable') return t('q.not_stoppable');
  if (data && data.code === 'optout_listed') return t('optout.still_listed');
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
// a campaign is `draft`, an email is `pending_review`, a plan is `proposed`.
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
  unchecked: 'idle', valid: 'good', likely: 'waiting', unconfirmed: 'idle',
  finished: 'idle', archived: 'idle', waiting: 'waiting', ready: 'active', staged: 'waiting',
  in_sequence: 'active', done: 'good', stopped: 'idle', suppressed: 'bad', skipped: 'idle', removed: 'idle',
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
  return escHtml(sourceText(s));
}

function sourceText(s) {
  if (!s) return '';
  const hit = own(SOURCE_LABELS, s);
  if (hit) return hit.startsWith('src.') ? t(hit) : hit;
  return String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh', agent: 'chat'};
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
    case 'today': if (_modules.email) { loadLiveStatus(); loadFunnel(); loadSetupStatus(); } loadToday(); loadTrend(); loadRuns(); loadJobRuns(); loadTodayActivity(); break;
    case 'modules': loadModulesPage(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': loadCompanies(); break;
    case 'registry': loadRegistry(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    // (Not while an answer is being edited there: what's typed would go.)
    case 'conversations': if (!_cvEdit.id) loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'queue': loadQueue(); break;
    case 'chat': loadChat(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadModelCard(); loadModulesCard(); loadFinderBgCard(); loadVerifyPrices(); loadClassifyCard(); loadChannelsCard(); if (_modules.email) { loadSendingModeCard(); loadAckCard(); } break;
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
  // The group's name: Email while the Email module is on, else Messages
  // (SMS and WhatsApp only).
  const label = document.getElementById('nav-group-messages');
  if (label) {
    label.dataset.i18n = _modules.email ? 'nav.group.email' : 'nav.group.messages';
    label.textContent = t(label.dataset.i18n);
  }
  // The Today and Outbox ledes speak of email only while the Email module
  // is on; else of messages (SMS and WhatsApp), or of neither.
  const lede = document.getElementById('today-lede');
  if (lede) {
    lede.dataset.i18n = _modules.email ? 'today.lede'
      : _modules.messages ? 'today.lede_messages' : 'today.lede_plain';
    lede.textContent = t(lede.dataset.i18n);
  }
  const obLede = document.getElementById('outbox-lede');
  if (obLede) {
    obLede.dataset.i18nHtml = _modules.email ? 'ob.lede' : 'ob.lede_messages';
    obLede.innerHTML = t(obLede.dataset.i18nHtml);
  }
  const sec = document.getElementById(currentTab);
  if (sec && sec.dataset.module && !_modules[sec.dataset.module]) showTab('overview');
}

async function loadModules() {
  const data = await api('/api/ui/modules');
  if (data && data.modules) _modules = Object.assign({email: false, messages: false}, data.modules);
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
    _modules = Object.assign({email: false, messages: false}, res.modules);
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
  loadPublicData();
}

// ── Date publice: the official datasets kept locally (registry, statements) ──

let _pdTimer = null;

async function loadPublicData() {
  const data = await api('/api/public-data');
  if (!data || data.error) return;
  renderPublicData(data);
}

function renderPublicData(data) {
  const el = document.getElementById('public-data');
  if (!el) return;
  _pdRunning = {fin: !!(data.financials || {}).running, tax: !!(data.taxpayers || {}).running,
                bounds: !!(data.boundaries || {}).running};
  const fin = data.financials || {};
  const reg = data.registry;
  const num = v => fmtN(Number(v || 0));
  const regLine = reg
    ? escHtml(t('pd.registry_loaded', {dataset: reg.dataset || '', firms: num(reg.firms),
                                       operating: num(reg.operating)})) + ' ' + relTime(reg.loaded_at)
    : escHtml(t('pd.registry_none'));
  const settings = fin.settings || {};
  const loaded = fin.loaded;
  let finBody;
  if (!fin.on) {
    finBody = '<p class="desc">' + escHtml(t('pd.off')) + '</p>';
  } else {
    const lines = [];
    lines.push(loaded
      ? escHtml(t('pd.fin_loaded', {years: String(loaded.years || '').split(' ').join(', '),
                                    companies: num(loaded.companies), rows: num(loaded.rows)})) +
        ' ' + relTime(loaded.loaded_at)
      : escHtml(t('pd.fin_none', {years: settings.years || 3})));
    lines.push(fin.checked_at ? escHtml(t('pd.fin_checked')) + ' ' + relTime(fin.checked_at)
                              : escHtml(t('pd.fin_never_checked')));
    const todo = fin.updates || [];
    if (todo.length) {
      lines.push(escHtml(t('pd.fin_updates')) + ' ' + todo.map(u =>
        '<span class="cost-chip">' + escHtml(u.year + ' ' + u.form + ' · ' + t('pd.why.' + u.why)) + '</span>').join(''));
    } else if (loaded && fin.checked_at) {
      lines.push(escHtml(t('pd.fin_uptodate')));
    }
    const rep = fin.report;
    if (rep && (rep.errors || []).length) {
      lines.push('<span class="error">' + escHtml(t('pd.failed', {error: rep.errors[0]})) + '</span>');
    }
    const busy = !!fin.running;
    finBody = lines.map(l => '<p class="desc">' + l + '</p>').join('') +
      '<div class="pd-actions">' +
        '<button class="btn primary" ' + (busy ? 'disabled ' : '') + 'onclick="loadFinancials()">' +
          escHtml(busy ? t('pd.loading') : t(loaded ? 'pd.update' : 'pd.load')) + '</button> ' +
        '<button class="btn" ' + (busy ? 'disabled ' : '') + 'onclick="checkFinancials()">' + escHtml(t('pd.check')) + '</button>' +
        '<label class="pd-setting">' + escHtml(t('pd.years')) + ' <input type="number" min="1" max="30" value="' +
          escHtml(String(settings.years || 3)) + '" onchange="setFinancialsSetting(\'years\', this.value)"></label>' +
        '<label class="pd-setting"><input type="checkbox" ' + (settings.auto_update ? 'checked ' : '') +
          'onchange="setFinancialsSetting(\'auto_update\', this.checked)"> ' + escHtml(t('pd.auto')) + '</label>' +
      '</div>' +
      '<div class="module-terms">' + escHtml(t('pd.source', {attribution: fin.attribution || ''})) + '</div>';
  }
  clearTimeout(_pdTimer);
  if (fin.running || (data.taxpayers || {}).running || (data.anaf || {}).running ||
      (data.boundaries || {}).running) {
    _pdTimer = setTimeout(pollFinancials, 3000);
  }
  el.innerHTML = '<div class="subhead">' + escHtml(t('pd.title')) + '</div>' +
    '<p class="desc">' + escHtml(t('pd.lede')) + '</p>' +
    '<div class="card module-list">' +
      '<div class="module-row"><div class="module-text"><h3>' + escHtml(t('pd.registry')) + '</h3>' +
        '<p class="desc">' + regLine + '</p></div></div>' +
      '<div class="module-row"><div class="module-text"><h3>' + escHtml(t('pd.fin')) + '</h3>' +
        finBody + '</div></div>' +
      '<div class="module-row"><div class="module-text"><h3>' + escHtml(t('pd.tax')) + '</h3>' +
        taxpayersBody(data.taxpayers || {}) + '</div></div>' +
      '<div class="module-row"><div class="module-text"><h3>' + escHtml(t('pd.anaf')) + '</h3>' +
        anafBody(data.anaf || {}) + '</div></div>' +
      (data.boundaries && data.boundaries.on !== undefined
        ? '<div class="module-row"><div class="module-text"><h3>' + escHtml(t('pd.bounds')) + '</h3>' +
          boundariesBody(data.boundaries) + '</div></div>' : '') +
    '</div>';
}

// OpenStreetMap's administrative boundaries: one extract a region, read locally.
function boundariesBody(b) {
  if (!b.on) return '<p class="desc">' + escHtml(t('pd.bounds_off')) + '</p>';
  const num = v => fmtN(Number(v || 0));
  const regions = (b.regions || []).join(', ');
  const lines = [];
  const loaded = b.loaded;
  if (loaded) {
    const levels = Object.entries(loaded.levels || {}).map(([level, n]) =>
      t('pd.bounds_level', {level: level, n: num(n)})).join(', ');
    lines.push(escHtml(t('pd.bounds_loaded', {units: num(loaded.units), levels: levels,
                                              regions: loaded.regions || regions})) +
      ' ' + relTime(loaded.loaded_at));
  } else if (regions) {
    lines.push(escHtml(t('pd.bounds_none', {regions: regions, size: tServer('module.boundaries.cost', b.size_note || '')})));
  } else {
    lines.push(escHtml(t('pd.bounds_no_region')));
  }
  (b.downloads || []).filter(d => d.file).forEach(d => lines.push(escHtml(t('pd.bounds_downloaded', {
    file: d.file, mb: fmtN(Math.round((d.bytes || 0) / 1e6)), day: d.day}))));
  lines.push(escHtml(t('pd.bounds_why')));
  if (b.library) lines.push('<span class="error">' + escHtml(tServer('pd.bounds_library', b.library)) + '</span>');
  const rep = b.report;
  if (rep && (rep.errors || []).length) lines.push('<span class="error">' + escHtml(t('pd.failed', {error: rep.errors[0]})) + '</span>');
  const busy = !!b.running;
  const blocked = busy || !!b.library || !regions;
  return lines.map(l => '<p class="desc">' + l + '</p>').join('') +
    '<div class="pd-actions">' +
      '<button class="btn primary" ' + (blocked ? 'disabled ' : '') + 'onclick="loadBoundaries(false)">' +
        escHtml(busy ? t('pd.bounds_loading') : t(loaded ? 'pd.update' : 'pd.load')) + '</button> ' +
      (loaded ? '<button class="btn" ' + (blocked ? 'disabled ' : '') + 'onclick="loadBoundaries(true)">' +
        escHtml(t('pd.bounds_fresh')) + '</button>' : '') +
    '</div>' +
    '<div class="module-terms">' + escHtml(t('pd.source', {attribution: b.attribution || ''})) + '</div>';
}

async function loadBoundaries(force) {
  const res = await api('/api/public-data/boundaries/load', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({force: !!force}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('pd.failed', {error: ''})), 'error'); return; }
  loadPublicData();
}

// The Ministry of Finance's taxpayer file: loaded once a year, read locally.
function taxpayersBody(tax) {
  if (!tax.on) return '<p class="desc">' + escHtml(t('pd.tax_off')) + '</p>';
  const num = v => fmtN(Number(v || 0));
  const loaded = tax.loaded;
  const settings = tax.settings || {};
  const lines = [];
  lines.push(loaded
    ? escHtml(t('pd.tax_loaded', {year: loaded.year || '', rows: num(loaded.rows), phones: num(loaded.with_phone),
                                  vat: num(loaded.vat), employers: num(loaded.employers), micro: num(loaded.micro)})) +
      ' ' + relTime(loaded.loaded_at)
    : escHtml(t('pd.tax_none')));
  lines.push(tax.checked_at ? escHtml(t('pd.fin_checked')) + ' ' + relTime(tax.checked_at)
                            : escHtml(t('pd.fin_never_checked')));
  if (tax.update) {
    lines.push(escHtml(t('pd.fin_updates')) + ' <span class="cost-chip">' +
      escHtml(tax.update.year + ' · ' + t('pd.why.' + tax.update.why)) + '</span>');
  } else if (loaded && tax.checked_at) {
    lines.push(escHtml(t('pd.fin_uptodate')));
  }
  const rep = tax.report;
  if (rep && (rep.errors || []).length) lines.push('<span class="error">' + escHtml(t('pd.failed', {error: rep.errors[0]})) + '</span>');
  const busy = !!tax.running;
  return lines.map(l => '<p class="desc">' + l + '</p>').join('') +
    '<div class="pd-actions">' +
      '<button class="btn primary" ' + (busy ? 'disabled ' : '') + 'onclick="loadTaxpayers()">' +
        escHtml(busy ? t('pd.tax_loading') : t(loaded ? 'pd.update' : 'pd.load')) + '</button> ' +
      '<button class="btn" ' + (busy ? 'disabled ' : '') + 'onclick="checkTaxpayers()">' + escHtml(t('pd.check')) + '</button>' +
      '<label class="pd-setting"><input type="checkbox" ' + (settings.auto_update ? 'checked ' : '') +
        'onchange="setTaxpayersSetting(\'auto_update\', this.checked)"> ' + escHtml(t('pd.tax_auto')) + '</label>' +
    '</div>' +
    '<div class="module-terms">' + escHtml(t('pd.source', {attribution: tax.attribution || ''})) + '</div>';
}

// ANAF's public web service: asked per company, one request a second.
function anafBody(anaf) {
  if (!anaf.on) return '<p class="desc">' + escHtml(t('pd.anaf_off')) + '</p>';
  const kept = anaf.kept || {};
  const job = anaf.job;
  const lines = [escHtml(t('pd.anaf_kept', {answers: fmtN(kept.answers || 0), found: fmtN(kept.found || 0),
                                            days: (anaf.settings || {}).cache_days || 30}))];
  if (anaf.running && job) {
    lines.push(escHtml(t('pd.anaf_running', {done: fmtN(job.done || 0), total: fmtN(job.total || 0)})));
  } else if (job && job.report) {
    lines.push(escHtml(anafSummary(job.report)));
    if ((job.report.errors || []).length) lines.push('<span class="error">' + escHtml(t('pd.anaf_failed', {error: job.report.errors[0]})) + '</span>');
  }
  const busy = !!anaf.running;
  return lines.map(l => '<p class="desc">' + l + '</p>').join('') +
    '<div class="pd-actions">' +
      '<button class="btn primary" ' + (busy ? 'disabled ' : '') + 'onclick="anafCheck({missing: true})">' +
        escHtml(t('pd.anaf_missing')) + '</button> ' +
      '<button class="btn" ' + (busy ? 'disabled ' : '') + 'onclick="anafCheck({})">' + escHtml(t('pd.anaf_all')) + '</button>' +
    '</div>' +
    '<div class="module-terms">' + escHtml(t('pd.anaf_terms')) + '</div>' +
    '<div class="module-terms">' + escHtml(t('pd.source', {attribution: anaf.attribution || ''})) + '</div>';
}

function anafSummary(rep) {
  const done = t('pd.anaf_done', {companies: fmtN(rep.companies || 0), asked: fmtN(rep.checked || 0),
                                  cached: fmtN(rep.cached || 0), updated: fmtN(rep.updated || 0),
                                  missing: fmtN(rep.not_found || 0)});
  return rep.stopped === 'busy' ? done + ' ' + t('pd.anaf_busy') : done;
}

let _anafWatch = null;

// Start an ANAF check ({ids}, {q}, {missing: true} or {} for every company
// with a CUI) and say when it is done.
async function anafCheck(body) {
  const res = await api('/api/anaf/check', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('pd.anaf_failed', {error: ''})), 'error'); return; }
  showToast(res.capped ? t('pd.anaf_started_capped', {n: fmtN(res.capped), total: fmtN(res.matched)}) : t('pd.anaf_started'));
  loadPublicData();
  clearTimeout(_anafWatch);
  _anafWatch = setTimeout(watchAnaf, 2000);
}

async function watchAnaf() {
  const data = await api('/api/public-data');
  const anaf = data && data.anaf;
  if (!anaf) return;
  if (anaf.running) { _anafWatch = setTimeout(watchAnaf, 2000); return; }
  const rep = anaf.job && anaf.job.report;
  if (rep) {
    if ((rep.errors || []).length && !rep.updated && !rep.cached) showToast(t('pd.anaf_failed', {error: rep.errors[0]}), 'error');
    else showToast(anafSummary(rep), 'success');
  }
  if (document.getElementById('public-data')) renderPublicData(data);
  if (_drawer.id) openCompany(_drawer.id);
}

async function loadTaxpayers() {
  const res = await api('/api/public-data/taxpayers/load', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('pd.failed', {error: ''})), 'error'); return; }
  loadPublicData();
}

async function checkTaxpayers() {
  const res = await api('/api/public-data/taxpayers/check', {method: 'POST'});
  if (!res || !res.success) { showToast(failMsg(res, t('pd.check_failed')), 'error'); return; }
  renderPublicData(res);
}

async function setTaxpayersSetting(key, value) {
  const res = await api('/api/public-data/taxpayers/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[key]: value}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('mod.failed')), 'error'); return; }
  renderPublicData(res);
}

let _pdRunning = {};

async function pollFinancials() {
  const data = await api('/api/public-data');
  if (!data || data.error) return;
  const fin = data.financials || {};
  const tax = data.taxpayers || {};
  if (_pdRunning.fin && !fin.running && fin.report) {
    const rep = fin.report;
    if ((rep.errors || []).length && !rep.companies) showToast(t('pd.failed', {error: rep.errors[0]}), 'error');
    else showToast(rep.unchanged ? t('pd.unchanged_toast') : t('pd.loaded_toast', {companies: fmtN(rep.companies || 0)}), 'success');
  }
  if (_pdRunning.tax && !tax.running && tax.report) {
    const rep = tax.report;
    if ((rep.errors || []).length && !rep.companies) showToast(t('pd.failed', {error: rep.errors[0]}), 'error');
    else showToast(rep.unchanged ? t('pd.tax_unchanged_toast') : t('pd.tax_loaded_toast', {companies: fmtN(rep.companies || 0)}), 'success');
  }
  const bounds = data.boundaries || {};
  if (_pdRunning.bounds && !bounds.running && bounds.report) {
    const rep = bounds.report;
    if ((rep.errors || []).length) showToast(t('pd.failed', {error: rep.errors[0]}), 'error');
    else showToast(t('pd.bounds_loaded_toast'), 'success');
  }
  _pdRunning = {fin: !!fin.running, tax: !!tax.running, bounds: !!bounds.running};
  renderPublicData(data);
}

async function loadFinancials() {
  const res = await api('/api/public-data/financials/load', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('pd.failed', {error: ''})), 'error'); return; }
  loadPublicData();
}

async function checkFinancials() {
  const res = await api('/api/public-data/financials/check', {method: 'POST'});
  if (!res || !res.success) { showToast(failMsg(res, t('pd.check_failed')), 'error'); return; }
  renderPublicData(res);
}

async function setFinancialsSetting(key, value) {
  const res = await api('/api/public-data/financials/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({[key]: value}),
  });
  if (!res || !res.success) { showToast(failMsg(res, t('mod.failed')), 'error'); return; }
  renderPublicData(res);
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
  loadOverviewSiteCheck();
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
    // Right after the upgrade or a register load, the register's counts
    // wait for their job: say so rather than show 0.
    tile(t('ov.companies'), total, ov.counting && ov.with_activity == null ? t('ov.counting') : t('ov.companies_note', {n: ov.with_activity || 0}), null, 'companies') +
    tile(t('ov.with_phone'), ov.with_phone || 0, t('ov.share', {p: pct(ov.with_phone || 0)}), bar(ov.with_phone || 0), 'phone') +
    tile(t('ov.with_email'), ov.with_email || 0, t('ov.share', {p: pct(ov.with_email || 0)}), bar(ov.with_email || 0), 'mail') +
    tile(t('ov.contacts'), ov.contacts || 0, t('ov.contacts_note'), null, 'contacts') +
    tile(t('ov.people'), ov.people || 0, ov.counting ? t('ov.people_counting') : t('ov.people_note'), null, 'layers') +
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
// whether any company is on file.
function renderOverviewSetup(setup, ov) {
  const el = document.getElementById('overview-setup');
  if (!el) return;
  const checks = ((setup && setup.checks) || []).filter(c => c.scope === 'finding');
  const rows = checks.map(c => ({done: c.done, label: checkLabel(c), help: checkHelp(c), optional: !c.required}));
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
  if (c.id === 'brain') return t('check.brain', {name: c.provider === 'codex' ? 'Codex' : 'Claude',
                                                model: c.model ? ' (' + c.model + ')' : ''});
  const key = 'check.' + c.id;
  const label = t(key);
  return label === key ? (c.label || c.id) : label;
}

function checkHelp(c) {
  const key = c.id === 'email_provider' ? 'check.help.email_provider.' + (c.provider || 'gmail')
    : 'check.help.' + c.id;
  const help = t(key);
  return help === key ? (c.help || '') : help;
}

// ── Today: what needs a human (any module with message channels) ──

let _setup = null;

// Today's items to show: those about email only (marked module: email) are
// left out while the Email module is off.
function todayItems(data) {
  return ((data && data.items) || []).filter(i => !i.module || _modules[i.module]);
}

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

  const items = todayItems(data);
  navCount('nav-today', items.filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

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
    case 'discover': return tr(t('today.item.discover'), t('today.item.discover_detail'), t('today.item.discover_action'));
    case 'profile': return tr(t('today.item.profile', {n: s.unprofiled || 0}), t('today.item.profile_detail'), t('today.item.profile_action'));
    case 'needs-human': return tr(t('today.item.needs_human', {n: s.needs_human || 0}), t('today.item.needs_human_detail'), t('today.item.needs_human_action'));
    case 'outbox': return tr(t(s.messages_pending ? 'today.item.outbox_any' : 'today.item.outbox', {n: s.outbox_pending || 0}),
      /Autopilot/.test(it.detail || '') ? t('today.item.outbox_auto') : t('today.item.outbox_detail'), t('today.item.outbox_action'));
    case 'replies': return tr(t('today.item.replies', {n: s.open_conversations || 0}), t('today.item.replies_detail'), t('today.item.replies_action'));
    case 'read-gap': return tr(t('today.item.read_gap'), t('today.item.read_gap_detail'), t('today.item.open_log'));
    case 'message-read-gap': return tr(t('today.item.message_gap', {channel: channelName(it.channel)}),
      t('today.item.message_gap_detail') + (it.since ? ' ' + t('today.item.message_gap_window', gapWindow(it.since, it.until)) : ''),
      t('today.item.message_gap_action'));
    case 'ack': return tr(t('today.item.ack', {n: s.cold_waiting || 0}), t('today.item.ack_detail'), t('today.item.ack_action'));
    case 'agent-wakes': return tr(t('today.item.agent_wakes'), it.detail, t('today.item.agent_wakes_action'));
    case 'codex-login': return tr(t('today.item.codex_login'), t('today.item.codex_login_detail'), t('today.item.codex_login_action'));
    case 'agent-free-plans': return tr(t('today.item.agent_free_plans', {n: s.agent_free_plans || 0}), t('today.item.agent_free_plans_detail'), t('today.item.agent_free_plans_action'));
    case 'site-check': return tr(t('today.item.site_check', {n: fmtN(s.sites_to_check || 0)}), t('today.item.site_check_detail'), t('today.item.site_check_action'));
    case 'campaign-checks': return tr(t('today.item.campaign_checks', {n: s.campaign_checks_waiting || 0}),
      checksHeldText(it.code, it.detail), t('today.item.campaign_checks_action'));
    case 'provider': return tr(t('today.item.provider'),
      // Instantly (removed) has its own words; any other name isn't one Harvey knows.
      String(it.provider || '').toLowerCase() === 'instantly' ? t('today.item.provider_detail')
        : t('today.item.provider_unknown', {provider: it.provider || ''}), t('today.item.provider_action'));
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

  const sent = card(t('kpi.sent'), fmtN(sum(tr.sent)), sparkBars(tr.sent, t('kpi.sent_spark')),
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
    '<span>' + tBold('kpi.fact_signals', stats.signals_on || 0) + '</span>';

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

// Today: the newest runs of jobs (a Find contacts, a Discover, a check),
// each with how long it took and what it cost, split.
async function loadJobRuns() {
  const el = document.getElementById('today-jobruns');
  const block = document.getElementById('today-jobruns-block');
  if (!el) return;
  const data = await api('/api/jobs/batches?limit=5');
  const runs = (data && data.runs) || [];
  if (!runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = runs.map(r => '<div class="job-run"><div class="run-head"><strong>' + escHtml(r.label || jobKind(r.kind)) + '</strong> ' +
      '<span class="muted small">' + escHtml(jobKind(r.kind)) + ' &middot; ' + relTime(r.created_at) + '</span> ' +
      '<button type="button" class="btn btn-ghost btn-sm" onclick="openQueue(' + jsq(r.id) + ')">' + escHtml(t('disc.see_jobs')) + '</button></div>' +
    runReportHtml(r.report) + '</div>').join('');
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

// A server-written text in this dashboard's language: the translation is
// used only while the server's English still matches the English it was
// made from, so a changed description never shows a stale translation.
function tServer(key, english) {
  if (getLang() === 'en' || !english) return english || '';
  return hasKey('en', key) && t(key, null, 'en') === english ? t(key) : english;
}

// ── Discover: tick sources, price them, then run them as one batch ──

// The sources ticked (search providers' keys and "<module>.<capability>"
// for a source run as a job, the registry), and each one's filters.
let _sources = null;
let _sourceFilters = {};
let _discoverPoll = null;
let _discData = null;

function sourceCard(p, kind) {
  const pv = (field) => kind === 'job' ? tServer('src.' + p.key + '.' + field, p[field] || '')
                                       : tServer('prov.' + p.key + '.' + field, p[field]);
  const on = _sources.has(p.key);
  const ready = p.configured
    ? '<span class="badge t-good">' + escHtml(t('disc.ready')) + '</span>'
    : '<span class="badge t-waiting">' + escHtml(t('disc.needs_key')) + '</span>';
  return '<label class="prov-card ' + (on ? 'selected' : '') + '">' +
    '<div class="prov-head"><input type="checkbox"' + (on ? ' checked' : '') +
      ' onchange="toggleSource(' + jsq(p.key) + ', this.checked)" aria-label="' + escHtml(pv('label')) + '">' +
      '<h3>' + escHtml(pv('label')) + '</h3>' + ready + '</div>' +
    '<div class="blurb">' + escHtml(pv('blurb')) + '</div>' +
    (kind === 'job'
      ? '<div class="row"><span class="k">' + escHtml(t('disc.cost')) + '</span><span class="v">' + escHtml(t('disc.free_count')) + '</span></div>' +
        (p.attribution ? '<div class="caveat">' + escHtml(p.attribution) + '</div>' : '')
      : '<div class="row"><span class="k">' + escHtml(t('disc.cost')) + '</span><span class="v">' + escHtml(pv('cost_note')) + '</span></div>' +
        '<div class="row"><span class="k">' + escHtml(t('disc.free')) + '</span><span class="v">' + escHtml(pv('free_tier')) + '</span></div>' +
        (p.needs_key
          ? '<div class="row"><span class="k">' + escHtml(t('disc.setup')) + '</span><span class="v">' +
            escHtml(t('disc.env_keys', {keys: p.env_keys.join(', ')})) + ' &middot; ' +
            '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer">' +
            escHtml(t('disc.get_key')) + '</a></span></div>'
          : '') +
        (p.caveat ? '<div class="caveat">' + escHtml(pv('caveat')) + '</div>' : '')) +
  '</label>';
}

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  // Without the providers there is nothing to estimate or run against.
  markLoadFailed('discover', !data || !data.providers);
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }
  _discData = data;

  // A source or preset whose module is off isn't offered (Surse turns it on).
  // A lookup-only search (Brave) finds a known company's website: never a
  // source here, only one of the searches "find websites" goes through.
  const providers = data.providers.filter(p => p.enabled !== false && !p.lookup_only);
  renderWebsiteFinders(data.providers.filter(p => p.enabled !== false && p.website_lookup));
  const jobbed = (data.sources || []).filter(s => s.enabled !== false);
  const known = new Set(providers.map(p => p.key).concat(jobbed.map(s => s.key)));
  if (_sources === null) {
    const last = (data.selected_all || []).filter(k => known.has(k));
    _sources = new Set(last.length ? last : [data.default].filter(k => known.has(k)));
  }
  _sources.forEach(k => { if (!known.has(k)) _sources.delete(k); });
  const presetSel = document.getElementById('disc-preset');
  if (presetSel && data.presets) {
    Array.from(presetSel.options || []).forEach(o => {
      if (o.value) o.hidden = o.disabled = data.presets[o.value] === false;
    });
    if (presetSel.value && data.presets[presetSel.value] === false) presetSel.value = '';
  }
  el.innerHTML = (providers.length || jobbed.length ? '' : emptyState(icon('layers'), escHtml(t('disc.none_on')),
      escHtml(t('disc.none_on_copy')), true,
      '<button type="button" class="btn btn-secondary btn-sm" onclick="goTab(\'modules\')">' +
        escHtml(t('disc.open_modules')) + '</button>')) +
    '<div class="prov-grid">' + jobbed.map(s => sourceCard(s, 'job')).join('') +
      providers.map(p => sourceCard(p, 'provider')).join('') + '</div>';
  renderSourceFilters(jobbed);

  const running = data.running;
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
  renderActiveRuns(data.active_runs || []);
  const shown = (data.active_runs || []).some(r => data.last_report && r.batch_id === data.last_report.batch_id);
  renderDiscoverResult(shown ? null : data.last_report, running && !(data.active_runs || []).length);
}

// The searches "find websites" can go through, under its checkbox.
function renderWebsiteFinders(finders) {
  const el = document.getElementById('disc-websites-note');
  if (!el) return;
  const names = finders.map(p => tServer('prov.' + p.key + '.label', p.label) + ' (' +
    (p.configured ? (p.cost_note ? tServer('prov.' + p.key + '.cost_note', p.cost_note) : t('runs.free'))
                  : t('disc.needs_key')) + ')');
  el.textContent = t('disc.websites_note') + ' ' +
    (names.length ? t('disc.websites_via', {names: names.join(', ')}) : t('disc.websites_none'));
}

// Anything changed on the form: estimate again before running.
function discoverChanged() {
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = t('disc.estimate_first');
}

function toggleSource(key, on) {
  if (on) _sources.add(key); else _sources.delete(key);
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = t('disc.estimate_first');
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

// The filters a ticked source declares in its manifest (the registry's
// activity, county, town, turnover...), one panel per source.
function renderSourceFilters(jobbed) {
  const el = document.getElementById('discover-filters');
  if (!el) return;
  const ticked = (jobbed || []).filter(s => _sources.has(s.key) && (s.filters || []).length);
  el.innerHTML = ticked.map(s => {
    const f = _sourceFilters[s.key] = _sourceFilters[s.key] || {};
    const field = (x) => sourceFilterField(s.key, x, f[x.key], 'sf-',
      'setSourceFilter(' + jsq(s.key) + ',' + jsq(x.key) + ',this.value)');
    return '<div class="card source-filters"><h3>' + escHtml(tServer('src.' + s.key + '.label', s.label)) + '</h3>' +
      '<div class="form-row">' + s.filters.map(field).join('') + '</div></div>';
  }).join('');
}

// One filter a source declares in its manifest, as a form field (the
// Discover panel's and the Registry page's): yes/no and choices as a
// select, numbers and text as inputs.
function sourceFilterField(sourceKey, x, value, prefix, onchange) {
  const id = prefix + sourceKey.replace(/\W/g, '-') + '-' + x.key;
  const label = tServer('src.' + sourceKey + '.filter.' + x.key, x.label || x.key);
  let input;
  if (x.type === 'yesno' || x.type === 'choice') {
    const options = x.type === 'yesno' ? [['', t('cl.f.any')], ['yes', t('cl.f.yes')], ['no', t('cl.f.no')]]
      : (x.options || []).map(o => [o, tServer('src.option.' + o, o)]);
    input = '<select class="form-input" id="' + id + '" onchange="' + onchange + '">' + options.map(o =>
      '<option value="' + escHtml(o[0]) + '"' + (String(value || '') === o[0] ? ' selected' : '') + '>' +
      escHtml(o[1]) + '</option>').join('') + '</select>';
  } else {
    input = '<input class="form-input" id="' + id + '" type="' + (x.type === 'number' ? 'number' : 'text') +
      '" value="' + escHtml(value || '') + '" oninput="' + onchange + '">';
  }
  return '<div class="form-group"><label class="form-label" for="' + id + '">' + escHtml(label) + '</label>' + input + '</div>';
}

function setSourceFilter(source, key, value) {
  const f = _sourceFilters[source] = _sourceFilters[source] || {};
  if (value === '' || value === null) delete f[key]; else f[key] = value;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = t('disc.estimate_first');
}

function discoverCap(raw) {
  const text = String(raw == null ? '' : raw).trim();
  const cap = Number(text);
  return text === '' || !isFinite(cap) || cap < 0 ? 1 : cap;
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  const providers = [..._sources];
  const filters = {};
  providers.forEach(k => { if (_sourceFilters[k]) filters[k] = _sourceFilters[k]; });
  return {
    providers: providers,
    filters: filters,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    // Blank: every result the source has; the cap is what stops a run.
    depth: Number(document.getElementById('disc-depth').value) || null,
    limit: Number(document.getElementById('disc-limit').value) || null,
    // 0 is a cap of nothing (free sources only); blank, the $1 default.
    max_spend: discoverCap(document.getElementById('disc-cap').value),
    preset: (document.getElementById('disc-preset') || {}).value || '',
    // Also look up a website for each company found with none: free
    // searches always, paid ones up to this amount, on top of the cap.
    find_websites: !!(document.getElementById('disc-websites') || {}).checked,
    websites_max_spend: discoverCap((document.getElementById('disc-websites-cap') || {}).value),
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  if (!_sources || !_sources.size) {
    el.innerHTML = '<div class="test-result error">' + escHtml(t('disc.pick_one')) + '</div>';
    return;
  }
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
  const sources = data.sources || [];
  const perSource = sources.length > 1 ? '<div class="query-list">' + sources.map(s =>
    escHtml(s.label) + ': ' + (s.free ? escHtml(t('disc.free_amount')) : escHtml(fmtUsd(s.estimated_cost, 4))) +
    (s.available !== null && s.available !== undefined ? ' &middot; ' + escHtml(t('disc.found_n', {n: fmtN(s.available)})) : '')
  ).join('<br>') + '</div>' : '';
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? escHtml(t('disc.free_amount')) : escHtml(fmtUsd(data.estimated_cost, 4))) + '</div>' +
    '<div class="muted small" style="margin-top:2px">' +
      escHtml(t('disc.queries', {n: data.query_count})) +
      (data.free ? '' : ' &middot; ' + escHtml(t(sources.length > 1 ? 'disc.cap_shared' : 'disc.cap_is', {cap: fmtUsd(cap, 2)}))) + '</div>' +
    (data.counted ? '<div class="muted small" style="margin-top:2px">' +
      escHtml(t('disc.counted', {n: fmtN(data.available || 0), cost: fmtUsd(data.count_cost || 0, 4)})) + '</div>' : '') +
    (data.counted === false && (data.errors || []).length ? '<div class="muted small" style="margin-top:2px">' +
      escHtml(t('disc.uncounted')) + ' ' + (data.errors || []).map(escHtml).join(' ') + '</div>' : '') +
    ((data.notes || []).length ? '<div class="test-result" style="margin-top:8px">' +
      (data.notes || []).map(n => escHtml(t('disc.left_out', {why: n}))).join('<br>') + '</div>' : '') +
    (over ? '<div class="test-result error" style="margin-top:12px">' + escHtml(t('disc.over_cap')) + '</div>' : '') +
    websitesEstimate(data.find_websites) +
    areaShapes(sources, data.cells_note) +
    perSource +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>' + escHtml(t('disc.more_queries', {n: data.queries.length - 40})) : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  // Over the cap still runs: the cap stops it, and it says what was left.
  btn.disabled = false;
  btn.textContent = data.free ? t('disc.run_free')
    : t('disc.run_paid', {cost: fmtUsd(Math.min(data.estimated_cost, cap), 2)});
}

// How each source covers its areas, in the estimate: one listings circle,
// a map grid's starting cells (a full cell splits as it runs), a point.
function areaShapes(sources, note) {
  const lines = (sources || []).filter(s => s.areas && s.shape).map(s => {
    if (s.shape === 'circle') return t('disc.shape_circle', {label: s.label, radius: (s.radius_km || []).join(', ')});
    if (s.shape === 'grid') return t('disc.shape_grid', {label: s.label, n: s.cells || 0});
    if (s.shape === 'point') return t('disc.shape_point', {label: s.label});
    return '';
  }).filter(Boolean);
  if (!lines.length) return '';
  return '<div class="muted small" style="margin-top:6px">' + lines.map(escHtml).join('<br>') +
    (note ? '<br>' + escHtml(t('disc.cells_note')) : '') + '</div>';
}

// What "find websites" would use, in the estimate: its amount, what a paid
// lookup costs, and the free searches that always run.
function websitesEstimate(w) {
  if (!w || !w.enabled) return '';
  const name = (m) => tServer('module.' + m + '.name', m);
  const parts = [t('disc.websites_up_to', {cap: fmtUsd(w.max_spend || 0, 2)})];
  if (w.per_lookup) parts.push(t('disc.websites_paid', {cost: fmtUsd(w.per_lookup, 4), n: fmtN(w.paid_lookups || 0)}));
  if ((w.free || []).length) parts.push(t('disc.websites_free', {names: w.free.map(name).join(', ')}));
  if (!(w.searchers || []).length) parts.push(t('disc.websites_none'));
  return '<div class="muted small" style="margin-top:6px">' + parts.map(escHtml).join(' &middot; ') + '</div>';
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
    // Searches asked again while the same one still waits weren't queued twice.
    if ((data.duplicates || []).length) showToast(t('disc.dupes', {n: data.duplicates.length}), 'success');
  } else {
    showToast(failMsg(data, t('disc.start_failed')), 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

// Stop a run, or one source within it.
async function stopDiscovery(batchId, source) {
  const body = {batch_id: batchId || ''};
  if (source) body.provider = source;
  const data = await api('/api/discover/stop', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  showToast(data && data.success ? t('disc.stopping')
                                 : failMsg(data, t('disc.stop_failed')), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

function sourceName(key) {
  const d = _discData || {};
  const job = (d.sources || []).find(s => s.key === key);
  if (job) return tServer('src.' + key + '.label', job.label);
  const p = (d.providers || []).find(x => x.key === key);
  return p ? tServer('prov.' + key + '.label', p.label) : key;
}

// Every run still working: each source's progress within it, and a Stop
// for the run and for each source.
function renderActiveRuns(runs) {
  const el = document.getElementById('discover-runs');
  if (!el) return;
  if (!runs.length) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">' + escHtml(t('disc.active_runs')) + '</div>' + runs.map(r => {
    const jobs = r.jobs || {};
    const head = '<div class="run-head"><strong>' + escHtml(r.label || r.batch_id) + '</strong> ' +
      '<span class="muted small">' + escHtml(t('disc.still_working', {n: jobs.running || 0, w: jobs.queued || 0})) +
      (r.max_spend !== null && r.max_spend !== undefined ? ' &middot; ' + escHtml(t('disc.spent_of', {spent: fmtUsd(r.spent || 0, 4), cap: fmtUsd(r.max_spend, 2)})) : '') +
      '</span> <button type="button" class="btn btn-secondary btn-sm" onclick="openQueue(' + jsq(r.batch_id) + ')">' +
        icon('queue') + escHtml(t('disc.see_jobs')) + '</button>' +
      (r.searches_active ? ' <button type="button" class="btn btn-danger btn-sm" onclick="stopDiscovery(' + jsq(r.batch_id) + ')">' +
        escHtml(t('disc.stop')) + '</button>' : '') + '</div>';
    const rows = (r.sources || []).map(s => {
      const sj = s.jobs || {};
      return '<tr><td>' + escHtml(sourceName(s.key)) + '</td>' +
        '<td class="muted small">' + escHtml(t('disc.source_jobs', {done: sj.done || 0, run: sj.running || 0, wait: sj.queued || 0})) + '</td>' +
        '<td>' + escHtml(t('disc.source_found', {n: fmtN(s.new_companies || 0), k: fmtN(s.known_companies || 0)})) + '</td>' +
        '<td class="muted">' + (s.actual_cost ? escHtml(fmtUsd(s.actual_cost, 4)) : escHtml(t('runs.free'))) + '</td>' +
        '<td>' + (s.active ? '<button type="button" class="btn btn-ghost btn-sm" onclick="stopDiscovery(' + jsq(r.batch_id) + ',' + jsq(s.key) + ')">' +
          escHtml(t('disc.stop_source')) + '</button>' : '') + '</td></tr>';
    }).join('');
    const finder = (r.contacts_found || r.finder_waiting)
      ? '<p class="muted small">' + escHtml(t('disc.finder_progress', {n: fmtN(r.contacts_found || 0), w: fmtN(r.finder_waiting || 0)})) + '</p>' : '';
    const w = r.websites;
    const websites = w && (w.looked_up || w.waiting)
      ? '<p class="muted small">' + escHtml(t('disc.websites_progress', {
          n: fmtN(w.looked_up || 0), found: fmtN(w.found || 0), w: fmtN(w.waiting || 0),
          spent: fmtUsd(w.spent || 0, 4), cap: fmtUsd(w.max_spend || 0, 2)})) + '</p>' : '';
    return '<div class="card run-card">' + head + (rows ? '<table class="data"><tbody>' + rows + '</tbody></table>' : '') + finder + websites + '</div>';
  }).join('');
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
    showToast(data.pending ? t('disc.reading', {n: data.pending}) : t('disc.all_read'), 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, t('disc.start_failed')), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (!report) {
    el.innerHTML = running ? '<div class="card"><h2>' + escHtml(t('disc.running')) + '</h2>' +
      '<p class="muted small">' + escHtml(t('disc.running_detail')) + '</p></div>' : '';
    return;
  }
  // The run is jobs on the Queue page: say what still waits, and link there.
  const jobsLink = report.batch_id
    ? '<button type="button" class="btn btn-secondary btn-sm" onclick="openQueue(' + jsq(report.batch_id) + ')">' +
        icon('queue') + escHtml(t('disc.see_jobs')) + '</button>' : '';
  const waiting = (report.jobs || {}).queued || 0;
  const notes = (report.running
      ? '<p class="muted small">' + escHtml(t('disc.still_working', {n: (report.jobs || {}).running || 0, w: waiting})) + '</p>' : '') +
    (report.spend_limited
      ? '<div class="test-result" style="margin-top:8px">' + escHtml(t('disc.spend_limited', {n: report.spend_limited})) + '</div>' : '');

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
  el.innerHTML = '<div class="subhead">' + escHtml(t('disc.last_run')) + ' ' + jobsLink + '</div>' + notes +
    '<div class="stats-grid">' +
      stat(t('disc.new'), fmtN(report.new_companies || 0), 'accent') +
      stat(t('disc.known'), fmtN(report.known_companies || 0)) +
      stat(t('disc.observations'), fmtN(report.observations || 0)) +
      stat(t('disc.junk'), fmtN(report.junk || 0)) +
      (report.profiled_companies ? stat(t('disc.sites_read'), fmtN(report.profiled_companies)) : '') +
      (report.contacts_found ? stat(t('disc.contacts_found'), fmtN(report.contacts_found), 'accent') : '') +
      stat(t('disc.actual_cost'), report.actual_cost ? escHtml(fmtUsd(report.actual_cost, 4)) : escHtml(t('runs.free'))) +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      escHtml(t('disc.stopped_early', {why: report.stopped})) + '</div>' : '') +
    (report.left_behind ? '<div class="test-result" style="margin-top:8px">' +
      escHtml(t('disc.unfetched', {n: fmtN(report.unfetched || 0), q: fmtN(report.queries_left || 0),
                                    cost: fmtUsd(report.unfetched_cost || 0, 2)})) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>' + escHtml(t('disc.problems')) + '</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
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

  const prov = data.provider || 'gmail';
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
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'set.ph.gmail_secret');
  savedPh('smtp-pass', data.smtp_password_set, 'set.ph.smtp_pass');
  savedPh('imap-pass', data.imap_password_set, 'set.same_smtp_pass');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('reoon-key', data.reoon_api_key_set, 'set.ph.reoon');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, 'set.ph.zerobounce');
  savedPh('hunter-key', data.hunter_api_key_set, 'set.ph.hunter');
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

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, t('set.saved_cf'));
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

// Quota gauges for the ACTIVE provider only: Claude's are what its CLI
// last reported (an answer's rate-limit event, or its /usage command), with
// when; Codex's are the ChatGPT plan's limits. When they can't be read, say
// so rather than hiding the card.
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
    const label = t(lkey) !== lkey ? t(lkey)
      : key.startsWith('seven_day_') ? t('use.window.seven_day_model', {model: key.slice(10)}) : key;
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(label) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  // When the newest reading was seen, and from which source (Claude's only).
  const seen = Object.values(data.quota).filter(w => w.seen_at)
    .sort((a, b) => (a.age_seconds || 0) - (b.age_seconds || 0))[0];
  if (seen) {
    const skey = 'use.source.' + seen.source;
    html += '<p class="muted">' + t('use.seen', {when: relTime(seen.seen_at)}) +
      (t(skey) !== skey ? ' &middot; ' + escHtml(t(skey)) : '') + '</p>';
  }
  return html;
}

// API-key mode: today's and this month's spend of Harvey's own calls, each
// beside its cap (brain.usd_per_day / usd_per_month) as a bar.
function moneyCardHtml(m) {
  if (!m) return '';
  let html = '<h2>' + escHtml(t('use.money_title')) + '</h2>';
  for (const [label, spent, cap] of [[t('use.money_today'), m.today_usd, m.usd_per_day],
                                     [t('use.money_month'), m.month_usd, m.usd_per_month]]) {
    const x = fmtUsd(spent || 0, 2);
    const pct = cap > 0 ? Math.min(100, Math.max(0, 100 * (spent || 0) / cap)) : 100;
    html += '<div class="progress-wrap"><div class="progress-label"><span class="text">' + escHtml(label) +
      '</span><span class="pct">' + escHtml(cap == null ? t('use.money_nocap', {x: x})
        : t('use.money_of', {x: x, y: fmtUsd(cap, 2)})) + '</span></div>' +
      (cap == null ? '' : '<div class="progress-bar"><div class="progress-fill ' + (pct >= 80 ? 'yellow' : 'green') +
        '" style="width:' + pct + '%"></div></div>') + '</div>';
  }
  html += '<p class="muted">' + escHtml(t(m.priced ? 'use.money_note' : 'use.money_no_price')) + '</p>';
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  // With an API key there is no plan: the money beside the brain's caps
  // takes the quota's place, and the lede says calls are billed.
  // (Not named "api": it would shadow api(), called above.)
  const billed = data.billing === 'api_key';
  const billing = document.getElementById('usage-billing');
  if (billing) {
    billing.setAttribute('data-i18n', billed ? 'use.billing_api' : 'use.billing_sub');
    billing.textContent = t(billed ? 'use.billing_api' : 'use.billing_sub');
  }
  const quotaEl = document.getElementById('usage-quota');
  const qHtml = billed ? moneyCardHtml(data.money) : quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars here: on a subscription plan usage isn't billed per token
  // (with an API key the money card above has them).
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
  // The SMS and WhatsApp answers sent through nPathi Chat: credits, not tokens.
  const m = data.messages;
  if (m) {
    const sentToday = Object.values(m.sent_today || {}).reduce((a, b) => a + (b || 0), 0);
    statsEl.innerHTML += '<div class="stat-card"><div class="label">' + escHtml(t('use.messages')) + '</div>' +
      '<div class="value">' + fmtN(m.credits_month || 0) + '</div><div class="breakdown">' +
        '<span class="chip">' + escHtml(t('ch.spend', {today: fmtN(m.credits_today || 0), month: fmtN(m.credits_month || 0)})) +
          escHtml(m.usd_today !== null && m.usd_today !== undefined ? t('ch.spend_usd', {today: fmtN(m.usd_today), month: fmtN(m.usd_month)}) : '') + '</span>' +
        '<span class="chip">' + escHtml(t('use.messages_sent', {n: sentToday})) + '</span>' +
      '</div></div>';
  }

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
  // (Approval or autopilot, the postal notice and Approve all are email's:
  // with the Email module off the Outbox holds SMS and WhatsApp answers.)
  const email = !!_modules.email;
  const [data, mode] = await Promise.all([api('/api/outbox'), email ? api('/api/sending-mode') : null]);
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

  // (SMS and WhatsApp answers are approved one at a time: Approve all leaves them.)
  const approvable = pending.length - pending.filter(p => heldReason(p) || isMessage(p) || p.off_module).length;
  actions.innerHTML =
    (email && pending.length && !data.paused && approvable
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
  if (email && data.compliance_notice && anyMail) {
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

  _obEdit.rows = data.approved || [];
  if (_obEdit.id && !_obEdit.rows.some(r => r.id === _obEdit.id && r.status === 'approved')) _obEdit.id = null;
  list.innerHTML = obEditor() +
    table(t('ob.approved'), data.approved, [
      [t('ob.to'), outboxTo], [t('ob.step'), r => r.step], [t('ob.subject'), outboxSubject],
      [t('ob.sends'), r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
      // Why an approved SMS or WhatsApp answer waits (the sending hours, a cap...).
      [t('ob.waits'), r => isMessage(r) && r.error ? msgWhy(r.wait, r.error) : '', true],
      [t('ob.change'), obRowActions, false, true],
    ]) +
    table(t('ob.sent'), data.sent, [
      [t('ob.to'), outboxTo], [t('ob.step'), r => r.step], [t('ob.subject'), outboxSubject],
      [t('ob.sent_at'), r => formatDate(r.sent_at) + (r.provider_status ? ' · ' + r.provider_status : ''), true],
    ]) +
    table(t('ob.failed'), data.failed, [
      [t('ob.to'), outboxTo], [t('ob.status'), r => badge(r.status), false, true],
      [t('ob.reason'), r => r.error, true], [t('ob.updated'), r => formatDate(r.updated_at), true],
    ]);
}

// An approved message that hasn't gone can still be edited (it stays
// approved when you approved it) or removed; never one being sent, or one
// whose send was tried (it may have gone).
const _obEdit = {id: null, rows: []};

function obRowActions(r) {
  if (r.status !== 'approved' || r.tried) return '';
  return '<button class="btn btn-secondary btn-sm" onclick="obEditOpen(' + jsq(r.id) + ')"' +
      (r.off_module ? ' disabled' : '') + '>' + escHtml(t('desk.edit')) + '</button> ' +
    '<button class="btn btn-secondary btn-sm" onclick="outboxRemove(' + jsq(r.id) + ')" title="' +
      escHtml(t('desk.remove_title')) + '">' + escHtml(t('desk.remove')) + '</button>';
}

function obEditor() {
  const r = _obEdit.rows.find(x => x.id === _obEdit.id);
  if (!r) return '';
  const msg = isMessage(r);
  return '<div class="card"><div class="subhead">' + escHtml(t('ob.edit_approved', {to: outboxTo(r)})) + '</div>' +
    (msg ? '' : '<input id="ob-edit-subject" class="desk-edit-subject" value="' + escHtml(r.subject || '') + '"' +
      (r.kind === 'reply' ? ' readonly title="' + escHtml(t('desk.reply_subject')) + '"' : '') + '>') +
    '<textarea id="ob-edit-body" class="desk-edit-body" rows="' + (msg ? 5 : 12) + '"' +
      (msg ? ' maxlength="' + (MESSAGE_MAX[r.channel] || 300) + '"' : '') + '>' + escHtml(r.body || '') + '</textarea>' +
    '<p class="muted small">' + escHtml(t('ob.edit_approved_note')) + '</p>' +
    '<div class="desk-actions"><button class="btn btn-primary btn-sm" onclick="obEditSave(' + jsq(r.id) + ')">' + escHtml(t('common.save')) + '</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="obEditOpen(null)">' + escHtml(t('common.cancel')) + '</button></div></div>';
}

function obEditOpen(id) {
  _obEdit.id = id;
  loadOutbox();
}

async function obEditSave(id) {
  const subj = document.getElementById('ob-edit-subject');
  const body = String(document.getElementById('ob-edit-body').value || '').trim();
  const data = await saveOutboxEdit(id, subj ? subj.value.trim() : '', body);
  if (data && data.success) {
    _obEdit.id = null;
    showToast(data.status === 'approved' ? t('desk.saved_approved') : t('desk.saved'), 'success');
    loadOutbox();
  } else showToast(editWhy(data), 'error');
}

// One edit, from the desk, the approved list or a conversation.
async function saveOutboxEdit(id, subject, body) {
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: subject, body: body}),
    });
    if (await lockIfRefused(r)) return null;
    return await r.json();
  } catch { return null; }
}

// Why an edit or a removal was refused, in the viewer's language when the
// server names it (gone, tried, the text's rules, a module off).
function editWhy(data, fallback) {
  if (data && data.off_module) return t('desk.module_off', {name: moduleName(data.off_module)});
  if (data && data.code) {
    const why = msgWhy(data.code, data.message, data.detail);
    return why.charAt(0).toUpperCase() + why.slice(1);
  }
  return failMsg(data, fallback || t('desk.save_failed'));
}

async function outboxRemove(id, after) {
  if ((!after && deskBusy()) || !confirm(t('desk.remove_confirm'))) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/remove', {method: 'POST'});
  if (data && data.success) {
    if (_obEdit.id === id) _obEdit.id = null;
    if (_cvEdit.id === id) _cvEdit.id = null;
    showToast(t('desk.removed'), 'success');
  } else showToast(editWhy(data, t('desk.action_failed')), 'error');
  if (after) after(); else loadOutbox();
}

// Who a row goes to, and what it is: an email's address and subject, an
// SMS or WhatsApp answer's channel and masked number.
function outboxTo(r) { return isMessage(r) ? channelName(r.channel) + ' ' + (r.to_phone || '') : r.to_email; }
function outboxSubject(r) { return isMessage(r) ? t('desk.message', {channel: channelName(r.channel)}) : r.subject; }

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
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>' + t('mode.approval') + '</b>' +
    '<p>' + t('mode.approval_detail') +
    ' <a href="#" onclick="goTab(\'settings\');return false">' + t('mode.about') + '</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">' + t('mode.unreadable') + '</div>';
  const auto = m.mode === 'autopilot';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || t('mode.no_domains');
  let html = '<div class="card mode-card"><h3>' + t('mode.card_title', {mode: auto ? t('mode.autopilot') : t('mode.approval_word')}) + '</h3>' +
    '<p>' + t('mode.explain_approval') + '</p>' +
    '<p>' + t('mode.explain_auto') + '</p>' +
    '<p class="muted">' + t('mode.yours', {domains: domains}) +
      (m.booking_page ? t('mode.booking', {page: escHtml(m.booking_page)}) : '') +
      t('mode.add_others', {file: escHtml(m.file)}) + '</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">' + t('mode.off_now') + '</span>' +
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
        : t('mode.toast_off') + (res.requeued ? ' ' + t('mode.toast_requeued', {n: res.requeued}) : ''), 'success');
    } else {
      showToast(failMsg(res, t('mode.toast_failed')), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// ── The cold-email acknowledgement (harvey/acknowledgement.py) ──
// Cold email waits until the operator has read the versioned text and typed
// the word once. The text comes from the server, in both languages; the
// page shows the one it is in.
let _ack = null, _ackDone = null, _ackBusy = false;

async function loadAck() {
  const a = await api('/api/acknowledgement');
  _ack = a && a.kind ? a : null;
  return _ack;
}

function ackWhen(a) {
  return a && a.at ? new Date(a.at * 1000).toLocaleString(intlLocale()) : '';
}

async function loadAckCard() {
  const el = document.getElementById('ack-card');
  if (!el) return;
  const a = await loadAck();
  if (!a) { el.innerHTML = '<div class="card muted">' + escHtml(t('ack.unreadable')) + '</div>'; return; }
  el.innerHTML = '<div class="card ack-card"><h3>' + escHtml(t('ack.card_title')) +
    ' <span class="email-tag ' + (a.given ? 'verified' : 'guess') + '">' + escHtml(t(a.given ? 'ack.given_tag' : 'ack.missing_tag')) + '</span></h3>' +
    '<p>' + escHtml(a.given ? t('ack.given', {v: a.version, when: ackWhen(a), by: a.by || '-'}) : t('ack.missing', {v: a.version})) + '</p>' +
    '<div class="convo-actions"><button class="btn ' + (a.given ? 'btn-ghost' : 'btn-primary') + ' btn-sm" onclick="openAckDialog()">' +
      escHtml(t(a.given ? 'ack.read' : 'ack.open')) + '</button></div></div>';
}

// The dialog: the text, and (until it is given) a box for the word.
// ``onDone`` runs once it is confirmed (the wizard's start).
async function openAckDialog(onDone) {
  const a = await loadAck();
  if (!a) { showToast(t('ack.unreadable'), 'error'); return; }
  _ackDone = typeof onDone === 'function' ? onDone : null;
  const lines = String((getLang() === 'en' ? a.text_en : a.text_ro) || '').split('\n');
  const title = lines.shift() || t('ack.card_title');
  const paras = lines.join('\n').trim().split(/\n\s*\n/).filter(Boolean);
  const word = String(a.confirm_word || 'accept');
  const foot = a.given
    ? '<p class="small muted">' + escHtml(t('ack.given', {v: a.version, when: ackWhen(a), by: a.by || '-'})) + '</p>' +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="closeAckDialog()">' + escHtml(t('common.close')) + '</button></div>'
    : '<label class="form-label" for="ack-typed">' + escHtml(t('ack.type', {word: word})) + '</label>' +
      '<input class="form-input" id="ack-typed" autocomplete="off" spellcheck="false" ' +
        'oninput="document.getElementById(\'ack-go\').disabled = this.value.trim().toLowerCase() !== ' + jsq(word) + '" ' +
        'onkeydown="if (event.key === \'Enter\' && !document.getElementById(\'ack-go\').disabled) confirmAck()">' +
      '<div class="btn-row"><button class="btn btn-primary btn-sm" id="ack-go" disabled onclick="confirmAck()">' + escHtml(t('ack.confirm')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="closeAckDialog()">' + escHtml(t('common.cancel')) + '</button></div>';
  document.getElementById('ack-body').innerHTML =
    '<div class="modal-head"><h2 id="ack-title">' + escHtml(title) + '</h2>' +
      '<span class="meta">' + escHtml(t('ack.version', {v: a.version})) + '</span></div>' +
    '<div class="modal-scroll">' + paras.map(p => '<p>' + escHtml(p) + '</p>').join('') + '</div>' +
    '<div class="modal-foot">' + foot + '</div>';
  const dlg = document.getElementById('ack-dialog');
  if (!dlg.open) { if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
  const input = document.getElementById('ack-typed');
  if (input) input.focus();
}

function closeAckDialog() {
  const dlg = document.getElementById('ack-dialog');
  if (dlg && dlg.open) { if (dlg.close) dlg.close(); else dlg.removeAttribute('open'); }
  _ackDone = null;
}

async function confirmAck() {
  const input = document.getElementById('ack-typed');
  if (_ackBusy || !input) return;
  _ackBusy = true;
  try {
    const res = await api('/api/acknowledgement', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                   body: JSON.stringify({typed: input.value.trim()})});
    if (!res || !res.success) { showToast(res && res.off_module ? failMsg(res) : t('cw.ack_failed'), 'error'); return; }
    const done = _ackDone;
    closeAckDialog();
    if (_cp.setup) _cp.setup.acknowledged = true;
    showToast(t('ack.done'), 'success');
    loadAckCard();
    if (done) await done();
  } finally {
    _ackBusy = false;
  }
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
// An SMS or WhatsApp answer (nPathi Chat) rather than an email, and the
// most characters each channel takes (channels.SEND_RULES).
function isMessage(item) { return !!item && !!item.channel && item.channel !== 'email'; }
const MESSAGE_MAX = {sms: 300, waba: 4096, wa: 4096};

function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : (it.to_email || it.to_phone);
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="' + escHtml(t('desk.held_title')) + '">' + escHtml(t('desk.held')) + '</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(isMessage(it) ? t('desk.message', {channel: channelName(it.channel)})
        : it.subject || t('desk.no_subject')) + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="' + escHtml(t('desk.reply_subject')) + '"' : '';
  // An SMS or WhatsApp answer has no subject, and a length the channel takes.
  const msg = isMessage(cur);
  const chars = msg ? '<div class="muted small">' + escHtml(t('desk.chars', {n: (unsaved.body || '').length,
    max: MESSAGE_MAX[cur.channel] || 300})) + '</div>' : '';
  const draft = editing
    ? (msg ? '<input id="desk-subject" type="hidden" value="">' : '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">') +
      '<textarea id="desk-body" class="desk-edit-body" rows="' + (msg ? 5 : 14) + '" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>' + chars
    : (msg ? '' : '<div class="subject">' + escHtml(cur.subject || t('desk.no_subject')) + '</div>') +
      '<div class="body">' + escHtml(cur.body) + '</div>' + chars;
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
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')"' +
        (cur.off_module ? ' disabled' : '') + '>' +
        escHtml(t('desk.approve')) + ' <kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')"' +
        (cur.off_module || cur.tried ? ' disabled' : '') + '>' +
        escHtml(t('desk.edit')) + ' <kbd>E</kbd></button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        escHtml(t('desk.reject')) + ' <kbd>R</kbd></button>' +
      // Remove deletes only this message (the person stays in the campaign);
      // not offered once a send was tried (it may have gone).
      (cur.tried ? '' : '<button class="btn btn-secondary" onclick="outboxRemove(' + jsq(cur.id) + ')" title="' +
        escHtml(t('desk.remove_title')) + '">' + escHtml(t('desk.remove')) + '</button>');

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">' + escHtml(t('desk.to')) + ' <b>' + escHtml(msg
          ? t('desk.message_to', {channel: channelName(cur.channel), number: cur.to_phone}) : cur.to_email) + '</b>' +
        '<span class="tag">' + escHtml(msg ? t('desk.message', {channel: channelName(cur.channel)})
          : cur.kind === 'reply' ? t('desk.reply') : t('desk.email_n', {n: cur.step})) + '</span>' +
        '<span class="muted">' + escHtml(t('desk.sends', {when: formatDate(cur.send_at)})) + '</span></div>' +
      renderDeskContext(cur.context) +
      // A row whose channel's module is off (an email from before the Email
      // module was turned off): listed and rejectable, never approved.
      (cur.off_module
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>' +
          escHtml(t('desk.module_off', {name: moduleName(cur.off_module)})) + '</b>' +
          '<div>' + escHtml(t('desk.module_off_detail')) + '</div></div></div>'
        : '') +
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
  const cur = _desk.items.find(x => x.id === id);
  if (cur && isMessage(cur)) {
    if (!body) { showToast(t('desk.need_body'), 'error'); return; }
  } else if (!subject || !body) { showToast(t('desk.need_both'), 'error'); return; }
  const data = await saveOutboxEdit(id, subject, body);
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast(t('desk.saved'), 'success');
    loadOutbox();
  } else {
    showToast(editWhy(data), 'error');
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

// Overview: the found websites waiting for a check (Today says it too, when
// the Email module is on), with the way to the Classifier card.
async function loadOverviewSiteCheck() {
  const el = document.getElementById('overview-site-check');
  if (!el) return;
  const c = await api('/api/classify');
  const n = (c && c.waiting) || 0;
  if (!n) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>' +
    escHtml(t('today.item.site_check', {n: fmtN(n)})) + '</b><p>' + escHtml(t('today.item.site_check_detail')) +
    '</p></div><button class="btn btn-secondary btn-sm" onclick="goTab(\'settings\')">' +
    escHtml(t('today.item.site_check_action')) + '</button></div>';
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

// opts.more: the total is a floor (counting stopped there); opts.jump: a
// page-number box.
function pagerHtml(state, total, fn, opts) {
  const o = opts || {};
  const from = total ? state.offset + 1 : 0;
  const to = Math.min(total, state.offset + state.limit);
  const pages = Math.max(1, Math.ceil(total / state.limit));
  return '<div class="pager">' +
    '<span class="pager-range">' + escHtml(t(o.more ? 'page.range_more' : 'page.range', {from: from, to: to, total: total})) + '</span>' +
    (o.jump ? '<label class="pager-size"><span>' + escHtml(t('page.page')) + '</span>' +
      '<input class="form-input" type="number" min="1"' + (o.more ? '' : ' max="' + pages + '"') +
      ' value="' + (Math.floor(state.offset / state.limit) + 1) + '" ' +
      'onchange="if (this.dataset.sent !== this.value) ' + fn + '(\'page\', this.value)" ' +
      'onkeydown="if (event.key === \'Enter\') { event.preventDefault(); this.dataset.sent = this.value; ' + fn + '(\'page\', this.value); }" ' +
      'aria-label="' + escHtml(t('page.page')) + '" style="width:6em"></label>' : '') +
    '<label class="pager-size"><span>' + escHtml(t('page.per_page')) + '</span>' +
      '<select class="form-input" onchange="' + fn + '(\'size\', this.value)" aria-label="' + escHtml(t('page.per_page')) + '">' +
      PAGE_SIZES.map(n => '<option value="' + n + '"' + (n === state.limit ? ' selected' : '') + '>' + n + '</option>').join('') +
      '</select></label>' +
    (o.numbers ? pageNumbersHtml(state, total, fn) : '') +
    '<span class="pager-btns">' +
      '<button class="btn btn-secondary btn-sm btn-icon" onclick="' + fn + '(\'prev\')"' + (state.offset > 0 ? '' : ' disabled') +
        ' aria-label="' + escHtml(t('page.prev')) + '" title="' + escHtml(t('page.prev')) + '">' + icon('chev-left') + '</button>' +
      '<button class="btn btn-secondary btn-sm btn-icon" onclick="' + fn + '(\'next\')"' + (to < total || (o.more && to === total) ? '' : ' disabled') +
        ' aria-label="' + escHtml(t('page.next')) + '" title="' + escHtml(t('page.next')) + '">' + icon('chev-right') + '</button>' +
    '</span></div>';
}

// Page numbers around the current one, plus the first and the last.
function pageNumbersHtml(state, total, fn) {
  const pages = Math.max(1, Math.ceil(total / state.limit));
  const cur = Math.floor(state.offset / state.limit) + 1;
  if (pages < 2) return '';
  const want = new Set([1, pages, cur - 2, cur - 1, cur, cur + 1, cur + 2].filter(n => n >= 1 && n <= pages));
  const list = [...want].sort((a, b) => a - b);
  let html = '<span class="pager-nums" role="group" aria-label="' + escHtml(t('page.pages')) + '">';
  list.forEach((n, i) => {
    if (i && n - list[i - 1] > 1) html += '<span class="pager-gap" aria-hidden="true">…</span>';
    html += n === cur
      ? '<button class="btn btn-primary btn-sm" aria-current="page" disabled>' + escHtml(fmtN(n)) + '</button>'
      : '<button class="btn btn-ghost btn-sm" onclick="' + fn + '(\'page\', ' + n + ')" aria-label="' +
        escHtml(t('page.go', {n: fmtN(n)})) + '">' + escHtml(fmtN(n)) + '</button>';
  });
  return html + '</span>';
}

function pageMove(state, how, value) {
  if (how === 'size') { state.limit = Number(value) || 50; state.offset = 0; savePageSize(state.limit); }
  else if (how === 'prev') state.offset = Math.max(0, state.offset - state.limit);
  else if (how === 'next') state.offset += state.limit;
  else if (how === 'page') state.offset = Math.max(0, (Math.floor(Number(value)) || 1) - 1) * state.limit;
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
// One server-side page of /api/companies: sorted, filtered (the Contacts
// page's company filters, plus activity, contact and source), with the
// total and page numbers. The search box, the filters and the selection
// live outside the rows, so a refresh re-renders the rows without losing
// any of them. A page cut from a kept ordering (a filter, or a sort by a
// figure) isn't refetched every 15 seconds: the ordering is the costly part.

let _co = { q: '', f: {}, sort: '', dir: '', offset: 0, limit: loadPageSize(), total: 0, selected: new Set(),
            timer: null, gen: 0, pagedBy: '', sources: [], financials: false, error: '', ms: 0 };
// [sort key, label key]: what the Sort menu offers (relevance only with a search).
const COMPANY_SORTS = [['created', 'co.sort.created'], ['name', 'co.sort.name'], ['place', 'co.sort.place'],
                       ['turnover', 'co.sort.turnover'], ['employees', 'co.sort.employees'],
                       ['contacts', 'co.sort.contacts'], ['searched', 'co.sort.searched']];
const COMPANY_CONTACT_FILTERS = [['any', 'co.f.contact_any'], ['phone', 'co.f.contact_phone'], ['mobile', 'co.f.contact_mobile'],
                                 ['email', 'co.f.contact_email'], ['none', 'co.f.contact_none']];

function companyFilter() {
  const f = Object.assign({}, _co.f);
  if (_co.q.trim()) f.search = _co.q.trim();
  return f;
}

function companyParams() {
  const p = new URLSearchParams();
  const f = companyFilter();
  Object.keys(f).forEach(k => { if (f[k] !== '' && f[k] !== undefined) p.set(k, f[k]); });
  if (_co.sort) p.set('sort', _co.sort);
  if (_co.dir) p.set('dir', _co.dir);
  p.set('limit', _co.limit);
  p.set('offset', _co.offset);
  return p.toString();
}

// A GET whose refusal says why (a bad filter): {ok, body}, or null offline.
async function apiRead(path) {
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return null;
    const body = await r.json().catch(() => null);
    if (!r.ok && !(body && body.error)) { _lastFail = r.status; return null; }
    return {ok: r.ok, body: body || {}};
  } catch {
    _lastFail = 'network';
    return null;
  }
}

async function loadCompanies() {
  const el = document.getElementById('companies-list');
  if (!document.getElementById('company-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search grow">' + icon('discover') +
        '<input class="form-input" id="company-search" type="search" autocomplete="off" ' +
        'data-i18n-placeholder="' + searchPh('co') + '" data-i18n-aria="co.search_label" ' +
        'placeholder="' + escHtml(t(searchPh('co'))) + '" aria-label="' + escHtml(t('co.search_label')) + '" ' +
        'oninput="companySearchInput(this.value)"></label>' +
      '<div id="company-sort"></div>' +
      '<div id="company-dupes"></div><div id="company-actions"></div>' +
      '</div><details class="more-fields" id="co-more"><summary id="co-more-sum"></summary><div id="co-filters"></div></details>' +
      '<div id="dupes-panel"></div><div id="company-selbar"></div><div id="company-rows"></div>';
    document.getElementById('company-search').value = _co.q;
    renderCompanyFilters();
  }
  if (_cv.signals === null) loadSignalChoices(renderCompanyFilters);
  renderCompanySort();
  const rowsEl = document.getElementById('company-rows');
  const q = _co.q.trim();
  const gen = ++_co.gen;
  loadDuplicates();
  if (q && q.split(/\s+/).every(w => w.startsWith('-'))) {
    _co.total = 0;
    _companies = [];
    renderCompanyActions();
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), escHtml(t('co.no_match')), escHtml(t('co.only_excluded'))) + '</div>';
    return;
  }
  if (!_companies.length) rowsEl.innerHTML = '<div class="table-card"><div class="skeleton"></div><div class="skeleton"></div></div>';
  const got = await apiRead('/api/companies?' + companyParams());
  if (gen !== _co.gen) return;                       // a newer page or search is on its way
  if (!got || !document.getElementById('company-rows')) { rowsEl.innerHTML = offlineState(); return; }
  const res = got.body;
  if (!got.ok) {
    _co.total = 0;
    _companies = [];
    renderCompanyActions();
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), escHtml(t('cl.bad_filter')), escHtml(res.error || '')) + '</div>';
    return;
  }
  const data = res.companies || [];
  const facets = JSON.stringify([res.sources, res.financials]);
  const changed = facets !== JSON.stringify([_co.sources, _co.financials]);
  Object.assign(_co, {total: res.total || 0, pagedBy: res.paged_by || '', sources: res.sources || [],
                      financials: !!res.financials, ms: res.ms || 0});
  if (res.sort && res.sort !== _co.sort && _co.sort) _co.sort = res.sort;   // the server fell back
  if (!data.length && _co.offset > 0 && _co.total > 0) {   // the list shrank: back to page one
    _co.offset = 0;
    return loadCompanies();
  }
  if (changed) renderCompanyFilters();
  _companies = data;
  _co.shownDir = res.dir || '';
  _co.shownSort = res.sort || '';
  renderCompanySort();
  renderCompanyActions();
  renderCompanySelbar();
  if (!data.length) {
    const filtered = q || Object.keys(_co.f).length;
    rowsEl.innerHTML = '<div class="table-card">' + (filtered
      ? emptyState(icon('discover'), escHtml(t('co.no_match')), escHtml(t(q ? 'co.no_match_copy' : 'cl.no_match_copy')))
      : emptyState(icon('companies'), escHtml(t('co.empty')), t('co.empty_copy'))) + '</div>';
    return;
  }
  const allOn = data.every(c => _co.selected.has(c.id));
  const th = (key, label) => {
    if (!key) return '<th>' + escHtml(t(label)) + '</th>';
    const on = _co.shownSort === key;
    return '<th aria-sort="' + (on ? (_co.shownDir === 'asc' ? 'ascending' : 'descending') : 'none') + '">' +
      '<button type="button" class="link-btn" onclick="sortCompanies(\'' + key + '\')" title="' + escHtml(t('cl.sort_by', {col: t(label)})) + '">' +
      escHtml(t(label)) + (on ? (_co.shownDir === 'asc' ? ' &#9650;' : ' &#9660;') : '') + '</button></th>';
  };
  let html = selectPageMobile(allOn, 'selectCompanyPage') + '<div class="table-card"><table class="data co-table"><thead><tr>' +
    '<th class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.page')) + '"' + (allOn ? ' checked' : '') +
      ' onchange="selectCompanyPage(this.checked)"></th>' +
    th('name', 'co.col.company') + th(null, 'co.col.phone') + th(null, 'co.col.email') + th(null, 'co.col.activities') +
    (_co.financials ? th('turnover', 'co.col.turnover') : '') + th('contacts', 'co.col.contacts') +
    th('created', 'co.col.source') + '</tr></thead><tbody>';
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
      (_co.financials ? '<td class="co-figs nowrap" data-label="' + escHtml(t('co.col.turnover')) + '">' + figuresCell(c.figures) + '</td>' : '') +
      '<td class="co-count" data-label="' + escHtml(t('co.col.contacts')) + '">' + (c.contacts ? fmtN(c.contacts) : '<span class="none">0</span>') + '</td>' +
      '<td class="co-src muted" data-label="' + escHtml(t('co.col.source')) + '"><div class="cell-stack"><span>' + sourceLabel(c.source) + '</span>' +
        '<span class="cell-sub">' + escHtml(t('co.found', {when: relTime(c.created_at)})) +
        (c.searched_at ? ' · ' + escHtml(t('co.searched', {when: relTime(c.searched_at)})) : '') + '</span></div></td></tr>';
  });
  rowsEl.innerHTML = html + '</tbody></table></div>' + pagerHtml(_co, _co.total, 'pageCompanies', {jump: true, numbers: true});
}

// Turnover (lei) and headcount of the latest year filed, or a dash.
function figuresCell(f) {
  if (!f || (f.turnover === null && f.employees === null)) return '<span class="none">—</span>';
  return '<div class="cell-stack"><span class="mono">' + (f.turnover === null ? '—' : escHtml(fmtLei(f.turnover))) + '</span>' +
    '<span class="cell-sub">' + escHtml([f.employees === null ? '' : t('co.staff', {n: f.employees}), f.year ? String(f.year) : '']
      .filter(Boolean).join(' · ')) + '</span></div>';
}

function fmtLei(n) {
  return fmtN(Math.round(Number(n))) + ' lei';
}

function renderCompanySort() {
  const el = document.getElementById('company-sort');
  if (!el) return;
  const searching = !!_co.q.trim();
  const opts = (searching ? [['relevance', 'co.sort.relevance']] : []).concat(
    COMPANY_SORTS.filter(s => _co.financials || (s[0] !== 'turnover' && s[0] !== 'employees')));
  const current = _co.sort || (searching ? 'relevance' : 'created');
  const dir = _co.shownDir || _co.dir || 'desc';
  el.innerHTML = '<div class="sort-pick"><label class="sr-only" for="co-sort">' + escHtml(t('co.sort.label')) + '</label>' +
    '<select class="form-input" id="co-sort" onchange="setCompanySort(this.value)" aria-label="' + escHtml(t('co.sort.label')) + '">' +
    opts.map(o => '<option value="' + o[0] + '"' + (o[0] === current ? ' selected' : '') + '>' + escHtml(t(o[1])) + '</option>').join('') +
    '</select>' + (current === 'relevance' ? '' :
    '<button type="button" class="btn btn-secondary btn-sm btn-icon" onclick="flipCompanySort()" aria-label="' +
      escHtml(t(dir === 'asc' ? 'co.sort.asc' : 'co.sort.desc')) + '" title="' + escHtml(t(dir === 'asc' ? 'co.sort.asc' : 'co.sort.desc')) + '">' +
      (dir === 'asc' ? '&#9650;' : '&#9660;') + '</button>') + '</div>';
}

function setCompanySort(key) {
  _co.sort = key === 'relevance' ? '' : key;
  _co.dir = '';
  _co.offset = 0;
  loadCompanies();
}

function flipCompanySort() {
  const dir = _co.shownDir || _co.dir || 'desc';
  if (!_co.sort) _co.sort = _co.shownSort && _co.shownSort !== 'relevance' ? _co.shownSort : 'created';
  _co.dir = dir === 'asc' ? 'desc' : 'asc';
  _co.offset = 0;
  loadCompanies();
}

function sortCompanies(key) {
  if ((_co.shownSort || _co.sort) === key) _co.dir = (_co.shownDir || 'desc') === 'asc' ? 'desc' : 'asc';
  else _co.dir = '';
  _co.sort = key;
  _co.offset = 0;
  loadCompanies();
}

// The signals collected (of modules that are on), for the require/exclude
// filters (shared with the Contacts page).
function loadSignalChoices(then) {
  _cv.signals = [];
  api('/api/signals').then(res => {
    _cv.signals = [];
    ((res && res.groups) || []).forEach(g => (g.signals || []).forEach(sig => {
      _cv.signals.push({code: sig.code, label: sig.label || sig.code});
    }));
    renderContactFilters();
    if (then) then();
  });
}

function renderCompanyFilters() {
  const el = document.getElementById('co-filters');
  if (!el) return;
  const f = _co.f;
  const n = Object.keys(f).filter(k => k !== 'unknown').length;
  const sum = document.getElementById('co-more-sum');
  if (sum) sum.textContent = n ? t('co.filters_n', {n: n}) : t('cl.filters');
  const opt = (value, label, current) => '<option value="' + escHtml(value) + '"' + (String(current || '') === value ? ' selected' : '') + '>' + escHtml(label) + '</option>';
  const select = (key, label, options) => '<div class="form-group"><label class="form-label" for="co-f-' + key + '">' + escHtml(t(label)) + '</label>' +
    '<select class="form-input" id="co-f-' + key + '" onchange="setCompanyFilter(\'' + key + '\', this.value)">' +
    opt('', t('cl.f.any'), f[key]) + options.map(o => opt(o[0], o[1], f[key])).join('') + '</select></div>';
  const input = (key, label, type, ph) => '<div class="form-group"><label class="form-label" for="co-f-' + key + '">' + escHtml(t(label)) + '</label>' +
    '<input class="form-input" id="co-f-' + key + '" type="' + (type || 'text') + '" value="' + escHtml(f[key] || '') + '"' +
    (ph ? ' placeholder="' + escHtml(t(ph)) + '"' : '') + ' oninput="setCompanyFilter(\'' + key + '\', this.value)"></div>';
  const yesNo = [['yes', t('cl.f.yes')], ['no', t('cl.f.no')]];
  const signals = (_cv.signals || []).map(s => [s.code, tServer('sigc.' + s.code + '.label', s.label)]);
  let html = '<div class="form-row">' + input('county', 'cl.f.county') + input('locality', 'cl.f.locality') +
    input('caen', 'co.f.caen', 'text', 'co.f.caen_ph') + '</div><div class="form-row">' +
    select('website', 'cl.f.website', yesNo) +
    select('contact', 'co.f.contact', COMPANY_CONTACT_FILTERS.map(v => [v[0], t(v[1])])) +
    select('found_in', 'cl.f.source', (_co.sources || []).map(v => [v, sourceText(v)])) + '</div><div class="form-row">' +
    select('require', 'cl.f.require', signals) + select('exclude', 'cl.f.exclude', signals) + '</div>' +
    (f.require || f.exclude ? '<p class="muted small">' + escHtml(t('co.f.signals_hint')) + '</p>' : '');
  if (_co.financials) {
    html += '<div class="form-row">' + input('min_turnover', 'cl.f.min_turnover', 'number') + input('max_turnover', 'cl.f.max_turnover', 'number') +
      input('min_employees', 'cl.f.min_employees', 'number') + input('max_employees', 'cl.f.max_employees', 'number') +
      '<div class="form-group"><label class="form-label"><input type="checkbox"' + (f.unknown === 'include' ? ' checked' : '') +
      ' onchange="setCompanyFilter(\'unknown\', this.checked ? \'include\' : \'\')"> ' + escHtml(t('cl.f.unknown')) + '</label></div></div>';
  }
  // Redrawn while someone types in it (the sources or signals arrived):
  // keep the focus and the caret in the same box.
  const active = el.contains(document.activeElement) ? document.activeElement : null;
  const end = active && active.value ? active.value.length : 0;
  const caret = active && /^(text|search)$/.test(active.type) ? [active.selectionStart ?? end, active.selectionEnd ?? end] : null;
  el.innerHTML = html + '<div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="clearCompanyFilters()">' +
    escHtml(t('cl.f.clear')) + '</button></div>';
  const again = active && active.id ? document.getElementById(active.id) : null;
  if (again) {
    if ('value' in active && again.tagName === 'INPUT' && again.type !== 'checkbox') again.value = active.value;
    again.focus();
    if (caret) try { again.setSelectionRange(caret[0], caret[1]); } catch (e) { /* not a text box */ }
  }
}

function setCompanyFilter(key, value) {
  if (value === '' || value === null || value === undefined) delete _co.f[key]; else _co.f[key] = String(value);
  _co.offset = 0;
  const sum = document.getElementById('co-more-sum');
  const n = Object.keys(_co.f).filter(k => k !== 'unknown').length;
  if (sum) sum.textContent = n ? t('co.filters_n', {n: n}) : t('cl.filters');
  if (key === 'require' || key === 'exclude') renderCompanyFilters();
  clearTimeout(_co.timer);
  _co.timer = setTimeout(loadCompanies, 300);
}

function clearCompanyFilters() {
  _co.f = {};
  _co.offset = 0;
  renderCompanyFilters();
  loadCompanies();
}

// ── Registry: every firm in the trade registry, with its financials ──
//
// One server-side page of /api/registry: the registry source's own filters
// (the manifest's, as on Descoperă), sorted by name, a latest-year figure
// or registration date, with the exact total and page numbers. Each firm
// says whether it is already in Harvey (by CUI); its panel shows every
// financial year side by side, the registry's record, the taxpayer file
// and ANAF's answer. Pulling the list or the ticked firms is a registry
// pull, counted first like on Descoperă (free).

const REGISTRY_SOURCE = 'onrc.registry_search';
let _rg = { q: '', f: {}, sort: '', dir: '', offset: 0, limit: loadPageSize(), total: 0, rows: [],
            selected: new Map(), timer: null, gen: 0, source: null, loaded: {}, pagedBy: '', ms: 0,
            shownSort: '', shownDir: '' };
const REGISTRY_SORTS = [['name', 'rg.sort.name'], ['turnover', 'rg.sort.turnover'], ['employees', 'rg.sort.employees'],
                        ['profit', 'rg.sort.profit'], ['registered', 'rg.sort.registered']];

function registryFilter() {
  const f = Object.assign({}, _rg.f);
  if (_rg.q.trim()) f.words = _rg.q.trim();
  return f;
}

// Whether the filter narrows the registry (status alone doesn't: with
// nothing else a pull would be every firm in Romania, and is refused).
function registryNarrowed() {
  return Object.keys(registryFilter()).some(k => k !== 'status' && k !== 'unknown');
}

function registryParams() {
  const p = new URLSearchParams();
  const f = registryFilter();
  Object.keys(f).forEach(k => { if (f[k] !== '' && f[k] !== undefined) p.set(k, f[k]); });
  if (_rg.sort) p.set('sort', _rg.sort);
  if (_rg.dir) p.set('dir', _rg.dir);
  p.set('limit', _rg.limit);
  p.set('offset', _rg.offset);
  return p.toString();
}

async function loadRegistry() {
  const el = document.getElementById('registry-list');
  if (!el) return;
  if (!document.getElementById('registry-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search grow">' + icon('discover') +
        '<input class="form-input" id="registry-search" type="search" autocomplete="off" ' +
        'data-i18n-placeholder="rg.search_ph" data-i18n-aria="rg.search_label" ' +
        'placeholder="' + escHtml(t('rg.search_ph')) + '" aria-label="' + escHtml(t('rg.search_label')) + '" ' +
        'oninput="registrySearchInput(this.value)"></label>' +
      '<div id="registry-sort"></div><div id="registry-actions"></div>' +
      '</div><details class="more-fields" id="rg-more"><summary id="rg-more-sum"></summary><div id="rg-filters"></div></details>' +
      '<div id="registry-pull"></div><div id="registry-selbar"></div><div id="registry-rows"></div>';
    document.getElementById('registry-search').value = _rg.q;
  }
  const rowsEl = document.getElementById('registry-rows');
  const gen = ++_rg.gen;
  if (!_rg.rows.length) rowsEl.innerHTML = '<div class="table-card"><div class="skeleton"></div><div class="skeleton"></div></div>';
  const got = await apiRead('/api/registry?' + registryParams());
  if (gen !== _rg.gen) return;
  if (!got || !document.getElementById('registry-rows')) { rowsEl.innerHTML = offlineState(); return; }
  const res = got.body;
  const firstSource = !_rg.source && res.source;
  Object.assign(_rg, {source: res.source || _rg.source, loaded: res.loaded || {}});
  // The filter labels are drawn in the page's language: again after a switch.
  if (firstSource || _rg.lang !== getLang() || !document.getElementById('rg-filters').innerHTML) {
    _rg.lang = getLang();
    renderRegistryFilters();
  }
  if (res.off) {
    _rg.total = 0; _rg.rows = [];
    renderRegistryChrome();
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('layers'), escHtml(t('rg.off')), escHtml(t('rg.off_copy'))) + '</div>';
    return;
  }
  if (!_rg.loaded.registry) {
    _rg.total = 0; _rg.rows = [];
    renderRegistryChrome();
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('layers'), escHtml(t('rg.not_loaded')), escHtml(t('rg.not_loaded_copy'))) + '</div>';
    return;
  }
  if (!got.ok) {
    _rg.total = 0; _rg.rows = [];
    renderRegistryChrome();
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), escHtml(t('cl.bad_filter')), escHtml(res.error || '')) + '</div>';
    return;
  }
  const data = res.rows || [];
  Object.assign(_rg, {total: res.total || 0, pagedBy: res.paged_by || '', ms: res.ms || 0,
                      shownSort: res.sort || '', shownDir: res.dir || ''});
  if (!data.length && _rg.offset > 0 && _rg.total > 0) { _rg.offset = 0; return loadRegistry(); }
  _rg.rows = data;
  renderRegistryChrome();
  if (!data.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), escHtml(t('rg.no_match')), escHtml(t('cl.no_match_copy'))) + '</div>';
    return;
  }
  const fin = !!_rg.loaded.financials;
  const allOn = data.every(r => _rg.selected.has(r.reg));
  const th = (key, label, cls) => {
    if (!key) return '<th' + (cls ? ' class="' + cls + '"' : '') + '>' + escHtml(t(label)) + '</th>';
    const on = _rg.shownSort === key;
    return '<th' + (cls ? ' class="' + cls + '"' : '') + ' aria-sort="' + (on ? (_rg.shownDir === 'asc' ? 'ascending' : 'descending') : 'none') + '">' +
      '<button type="button" class="link-btn" onclick="sortRegistry(\'' + key + '\')" title="' + escHtml(t('cl.sort_by', {col: t(label)})) + '">' +
      escHtml(t(label)) + (on ? (_rg.shownDir === 'asc' ? ' &#9650;' : ' &#9660;') : '') + '</button></th>';
  };
  let html = selectPageMobile(allOn, 'selectRegistryPage') + '<div class="table-card"><table class="data rg-table"><thead><tr>' +
    '<th class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.page')) + '"' + (allOn ? ' checked' : '') +
      ' onchange="selectRegistryPage(this.checked)"></th>' +
    th('name', 'rg.col.firm') + th(null, 'rg.col.place') + th(null, 'co.col.activities') +
    (fin ? th('turnover', 'rg.col.turnover') + th('profit', 'rg.col.profit') : '') +
    th('registered', 'rg.col.registered') + th(null, 'rg.col.harvey') + '</tr></thead><tbody>';
  data.forEach((r, i) => {
    const on = _rg.selected.has(r.reg);
    const f = r.figures;
    html += '<tr class="clickable' + (on ? ' selected' : '') + (r.operating ? '' : ' rg-closed') + '" onclick="registryRowOpen(event, ' + i + ')">' +
      '<td class="sel" data-label=""><input type="checkbox" aria-label="' + escHtml(t('sel.row', {name: r.name})) + '"' +
        (on ? ' checked' : '') + ' onchange="selectRegistryFirm(' + i + ', this.checked)"></td>' +
      '<td class="co-name"><div class="cell-stack"><button type="button" class="link-btn cell-main" onclick="openFirm(' + jsq(r.reg) + ')" ' +
        'aria-haspopup="dialog">' + escHtml(r.name) + '</button>' +
        '<span class="cell-sub">' + escHtml([r.cui ? 'CUI ' + r.cui : '', r.reg, r.form].filter(Boolean).join(' · ')) +
        (r.operating ? '' : ' · <span class="tag t-warn">' + escHtml(r.status || t('rg.not_operating')) + '</span>') + '</span></div></td>' +
      '<td data-label="' + escHtml(t('rg.col.place')) + '">' + escHtml([r.locality, r.county].filter(Boolean).join(', ') || '—') + '</td>' +
      '<td class="co-acts" data-label="' + escHtml(t('co.col.activities')) + '">' + companyActivities(r) + '</td>' +
      (fin ? '<td class="co-figs nowrap" data-label="' + escHtml(t('rg.col.turnover')) + '">' + figuresCell(f) + '</td>' +
             '<td class="mono nowrap" data-label="' + escHtml(t('rg.col.profit')) + '">' +
               (f && f.net_result !== null && f.net_result !== undefined ? escHtml(fmtLei(f.net_result)) : '<span class="none">—</span>') + '</td>' : '') +
      '<td class="nowrap" data-label="' + escHtml(t('rg.col.registered')) + '">' + escHtml(registryDate(r.registered) || '—') + '</td>' +
      '<td data-label="' + escHtml(t('rg.col.harvey')) + '">' + (r.company_id
        ? '<button type="button" class="tag t-good link-btn" onclick="openCompany(' + jsq(r.company_id) + ')">' + escHtml(t('rg.in_harvey')) + '</button>'
        : '<span class="none">' + escHtml(t('rg.not_in_harvey')) + '</span>') + '</td></tr>';
  });
  rowsEl.innerHTML = html + '</tbody></table></div>' + pagerHtml(_rg, _rg.total, 'pageRegistry', {jump: true, numbers: true}) +
    '<p class="muted small">' + escHtml(t('rg.totals_note', {ms: fmtN(_rg.ms)})) + '</p>';
}

// The registry writes dates as DD/MM/YYYY, sometimes with a time.
function registryDate(raw) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4})/.exec(String(raw || ''));
  return m ? m[3] + '-' + m[2] + '-' + m[1] : String(raw || '');
}

function renderRegistryChrome() {
  renderRegistrySort();
  renderRegistryActions();
  renderRegistrySelbar();
}

function renderRegistryFilters() {
  const el = document.getElementById('rg-filters');
  if (!el || !_rg.source) return;
  const sum = document.getElementById('rg-more-sum');
  const n = Object.keys(_rg.f).filter(k => k !== 'unknown' && !(k === 'status' && _rg.f[k] === 'operating')).length;
  if (sum) sum.textContent = n ? t('co.filters_n', {n: n}) : t('cl.filters');
  const fields = (_rg.source.filters || []).filter(x => x.key !== 'words');
  const active = el.contains(document.activeElement) ? document.activeElement.id : '';
  el.innerHTML = '<div class="form-row">' + fields.map(x => sourceFilterField(REGISTRY_SOURCE, x, _rg.f[x.key], 'rg-',
      'setRegistryFilter(' + jsq(x.key) + ',this.value)')).join('') + '</div>' +
    '<div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="clearRegistryFilters()">' + escHtml(t('cl.f.clear')) + '</button></div>';
  if (active && document.getElementById(active)) document.getElementById(active).focus();
}

// A filter change starts over: page one, no pull card, and no ticks (a
// ticked firm the new filter leaves out would be pulled unseen).
function registryFilterChanged() {
  _rg.offset = 0;
  closeRegistryPull();
  if (_rg.selected.size) { _rg.selected.clear(); renderRegistrySelbar(); renderRegistryActions(); }
}

// The list follows typing after a short pause; a pull asked for in that
// pause reloads first (registryReloadNow), so it counts what is shown.
function registryReloadSoon() {
  clearTimeout(_rg.timer);
  _rg.timer = setTimeout(() => { _rg.timer = null; loadRegistry(); }, 350);
}

async function registryReloadNow() {
  if (!_rg.timer) return;
  clearTimeout(_rg.timer);
  _rg.timer = null;
  await loadRegistry();
}

function setRegistryFilter(key, value) {
  if (value === '' || value === null || value === undefined) delete _rg.f[key]; else _rg.f[key] = String(value);
  registryFilterChanged();
  const sum = document.getElementById('rg-more-sum');
  const n = Object.keys(_rg.f).filter(k => k !== 'unknown' && !(k === 'status' && _rg.f[k] === 'operating')).length;
  if (sum) sum.textContent = n ? t('co.filters_n', {n: n}) : t('cl.filters');
  registryReloadSoon();
}

function clearRegistryFilters() {
  _rg.f = {};
  registryFilterChanged();
  renderRegistryFilters();
  clearTimeout(_rg.timer);
  _rg.timer = null;
  loadRegistry();
}

function registrySearchInput(value) {
  _rg.q = value;
  registryFilterChanged();
  registryReloadSoon();
}

function renderRegistrySort() {
  const el = document.getElementById('registry-sort');
  if (!el) return;
  const opts = REGISTRY_SORTS.filter(s => _rg.loaded.financials || !['turnover', 'employees', 'profit'].includes(s[0]));
  const current = _rg.shownSort || _rg.sort || 'name';
  const dir = _rg.shownDir || _rg.dir || 'asc';
  el.innerHTML = '<div class="sort-pick"><label class="sr-only" for="rg-sort">' + escHtml(t('co.sort.label')) + '</label>' +
    '<select class="form-input" id="rg-sort" onchange="setRegistrySort(this.value)" aria-label="' + escHtml(t('co.sort.label')) + '">' +
    opts.map(o => '<option value="' + o[0] + '"' + (o[0] === current ? ' selected' : '') + '>' + escHtml(t(o[1])) + '</option>').join('') +
    '</select><button type="button" class="btn btn-secondary btn-sm btn-icon" onclick="flipRegistrySort()" aria-label="' +
      escHtml(t(dir === 'asc' ? 'co.sort.asc' : 'co.sort.desc')) + '" title="' + escHtml(t(dir === 'asc' ? 'co.sort.asc' : 'co.sort.desc')) + '">' +
      (dir === 'asc' ? '&#9650;' : '&#9660;') + '</button></div>';
}

function setRegistrySort(key) {
  _rg.sort = key; _rg.dir = ''; _rg.offset = 0;
  loadRegistry();
}

function flipRegistrySort() {
  const dir = _rg.shownDir || _rg.dir || 'asc';
  if (!_rg.sort) _rg.sort = _rg.shownSort || 'name';
  _rg.dir = dir === 'asc' ? 'desc' : 'asc';
  _rg.offset = 0;
  loadRegistry();
}

function sortRegistry(key) {
  if ((_rg.shownSort || _rg.sort) === key) _rg.dir = (_rg.shownDir || 'asc') === 'asc' ? 'desc' : 'asc';
  else _rg.dir = '';
  _rg.sort = key;
  _rg.offset = 0;
  loadRegistry();
}

function pageRegistry(how, value) {
  pageMove(_rg, how, value);
  if (_rg.total && _rg.offset >= _rg.total) _rg.offset = Math.floor((_rg.total - 1) / _rg.limit) * _rg.limit;
  loadRegistry().then(() => {
    const top = document.getElementById('registry');
    if (top && how !== 'size') top.scrollIntoView({block: 'start'});
  });
}

function registryRowOpen(event, i) {
  if (event.target.closest('input, button, a')) return;
  const r = _rg.rows[i];
  if (r) openFirm(r.reg);
}

function selectRegistryFirm(i, on) {
  const r = _rg.rows[i];
  if (!r) return;
  if (on) _rg.selected.set(r.reg, r.name); else _rg.selected.delete(r.reg);
  registrySelectionOnly();
}

function selectRegistryPage(on) {
  _rg.rows.forEach(r => { if (on) _rg.selected.set(r.reg, r.name); else _rg.selected.delete(r.reg); });
  registrySelectionOnly();
}

// The ticks changed: redraw the row marks and the bars, not the page.
function registrySelectionOnly() {
  document.querySelectorAll('#registry-rows tbody tr').forEach((tr, i) => {
    const r = _rg.rows[i];
    if (!r) return;
    const on = _rg.selected.has(r.reg);
    tr.classList.toggle('selected', on);
    const box = tr.querySelector('td.sel input');
    if (box) box.checked = on;
  });
  const allOn = _rg.rows.length > 0 && _rg.rows.every(r => _rg.selected.has(r.reg));
  document.querySelectorAll('#registry-rows thead th.sel input, #registry-rows .sel-page-mobile input').forEach(b => { b.checked = allOn; });
  closeRegistryPull();
  renderRegistryActions();
  renderRegistrySelbar();
}

function renderRegistrySelbar() {
  const el = document.getElementById('registry-selbar');
  if (!el) return;
  const n = _rg.selected.size;
  el.innerHTML = n ? '<div class="selbar"><b>' + escHtml(t('sel.count', {n: n})) + '</b>' +
    '<button class="btn btn-primary btn-sm" onclick="pullRegistry(true)">' + escHtml(t('rg.pull_selected', {n: fmtN(n)})) + '</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="clearRegistrySelection()">' + escHtml(t('sel.clear')) + '</button></div>' : '';
}

function clearRegistrySelection() {
  _rg.selected.clear();
  registrySelectionOnly();
}

function renderRegistryActions() {
  const el = document.getElementById('registry-actions');
  if (!el) return;
  const n = _rg.selected.size;
  el.innerHTML = menuHtml('registry-menu', [
    {sep: true, label: t('actions.selection', {n: n})},
    {label: t('rg.pull'), icon: 'arrow', action: 'pullRegistry(true)', disabled: !n},
    {sep: true, label: t('actions.all_results', {n: _rg.total})},
    {label: t('rg.pull'), icon: 'arrow', action: 'pullRegistry(false)', disabled: !_rg.total || !registryNarrowed(),
     note: registryNarrowed() ? '' : t('rg.pull_needs_filter')},
  ]);
}

// Pull the ticked firms or the whole list into Harvey: counted first
// (Discover's estimate of a registry pull: free, read locally), then queued
// as a registry pull on the registry's lane.
let _rgPull = null;

function registryPullBody(selection) {
  const filters = selection ? {regs: [..._rg.selected.keys()]} : registryFilter();
  return {providers: [REGISTRY_SOURCE], filters: {[REGISTRY_SOURCE]: filters}, max_spend: 0,
          find_websites: true, websites_max_spend: 0};
}

async function pullRegistry(selection) {
  if (!selection) await registryReloadNow();
  const el = document.getElementById('registry-pull');
  if (!el) return;
  const body = registryPullBody(selection);
  _rgPull = {body: body};
  el.innerHTML = '<div class="card"><p class="muted small">' + escHtml(t('rg.counting')) + '</p></div>';
  const est = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!_rgPull || _rgPull.body !== body) return;
  if (!est || est.error) {
    el.innerHTML = '<div class="test-result error">' + escHtml(failMsg(est && {off_module: est.off_module, error: est.error}, t('disc.estimate_failed'))) + '</div>';
    return;
  }
  const n = est.available;
  el.innerHTML = '<div class="card finder-card"><h2>' + escHtml(t('rg.pull_title')) + '</h2>' +
    '<p>' + escHtml(n === null || n === undefined ? t('rg.pull_unknown') : t('rg.pull_summary', {n: fmtN(n)})) + '</p>' +
    '<p class="muted small">' + escHtml(t('rg.pull_free')) + '</p>' +
    ((est.errors || []).length ? '<p class="muted small">' + escHtml(est.errors.join('; ')) + '</p>' : '') +
    '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="runRegistryPull(this)"' + (n === 0 ? ' disabled' : '') + '>' +
      escHtml(n === null || n === undefined ? t('rg.pull') : t('rg.pull_run', {n: fmtN(n)})) + '</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="closeRegistryPull()">' + escHtml(t('common.cancel')) + '</button></div></div>';
}

function closeRegistryPull() {
  _rgPull = null;
  const el = document.getElementById('registry-pull');
  if (el) el.innerHTML = '';
}

async function runRegistryPull(btn) {
  if (!_rgPull) return;
  btn.disabled = true;
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(_rgPull.body),
  });
  if (data && data.success) {
    showToast(t('rg.pull_queued'), 'success');
    closeRegistryPull();
  } else {
    btn.disabled = false;
    showToast(failMsg(data, t('rg.pull_failed')), 'error');
  }
}

// ── A registry firm's panel ──

async function openFirm(reg) {
  _drawer = { id: 'firm:' + reg, data: null, contacts: null, opener: document.activeElement, firm: true };
  const drawer = document.getElementById('drawer');
  document.getElementById('drawer-backdrop').hidden = false;
  drawer.hidden = false;
  document.body.classList.add('drawer-open');
  renderFirmDrawer();
  const data = await api('/api/registry/firm?reg=' + encodeURIComponent(reg));
  if (_drawer.id !== 'firm:' + reg) return;
  _drawer.data = data || {error: true};
  renderFirmDrawer();
  drawer.focus({preventScroll: true});
}

function renderFirmDrawer() {
  const el = document.getElementById('drawer-body');
  const d = _drawer.data;
  const head = (title, sub) => '<div class="drawer-head"><div class="drawer-title"><h2 id="drawer-title">' + title + '</h2>' +
    (sub ? '<div class="drawer-sub">' + sub + '</div>' : '') + '</div>' +
    '<button class="btn btn-ghost btn-icon drawer-close" onclick="closeDrawer()" aria-label="' + escHtml(t('common.close')) + '">' +
    icon('x') + '</button></div>';
  if (!d) { el.innerHTML = head(escHtml(t('common.loading')), '') + '<div class="drawer-scroll"><div class="skeleton"></div><div class="skeleton"></div></div>'; return; }
  if (d.error) { el.innerHTML = head(escHtml(t('drawer.error')), ''); return; }
  const section = (title, ico, body, count) => '<section class="dsec"><h3>' + icon(ico) + escHtml(title) +
    (count ? '<span class="count">' + fmtN(count) + '</span>' : '') + '</h3>' + body + '</section>';
  const none = text => '<p class="none">' + escHtml(text) + '</p>';
  const firm = ((d.registry || {}).firms || [])[0] || {};
  const record = {cuis: d.cui ? [{cui: d.cui, sources: ['registry:onrc']}] : [], registry: d.registry,
                  financials: d.financials, anaf: d.anaf, taxpayer: d.taxpayer};
  const parts = recordSections({record: record, address: firm.address}, section, none, {yearsAcross: true});
  const actions = '<div class="btn-row">' + (d.company_id
    ? '<button class="btn btn-primary btn-sm" onclick="openCompany(' + jsq(d.company_id) + ')">' + escHtml(t('rg.open_in_harvey')) + '</button>'
    : '<button class="btn btn-primary btn-sm" onclick="pullOneFirm(' + jsq(d.reg) + ')">' + escHtml(t('rg.pull_one')) + '</button>') + '</div>';
  el.innerHTML = head(escHtml(firm.name || d.reg), escHtml([d.cui ? 'CUI ' + d.cui : '', d.reg].filter(Boolean).join(' · ')) +
      (d.company_id ? ' · <span class="tag t-good">' + escHtml(t('rg.in_harvey')) + '</span>' : '')) +
    '<div class="drawer-scroll">' + actions + parts.identity + parts.rest + '</div>';
}

// One firm, counted first like any pull (the firm may be gone from a
// registry loaded since the panel opened).
async function pullOneFirm(reg) {
  const body = {providers: [REGISTRY_SOURCE], filters: {[REGISTRY_SOURCE]: {regs: [reg]}}, max_spend: 0,
                find_websites: true, websites_max_spend: 0};
  const est = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!est || est.error || est.available === 0) {
    showToast(failMsg(est && est.error ? {off_module: est.off_module, error: est.error} : null, t('rg.pull_failed')), 'error');
    return;
  }
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (data && data.success) showToast(t('rg.pull_queued'), 'success');
  else showToast(failMsg(data, t('rg.pull_failed')), 'error');
}

// ── Possible duplicates ──
//
// Companies that may be one, as groups: every pair the resolver wasn't sure
// enough to merge, joined with the pairs it shares a company with, so four
// listings of one business are one decision. The person picks the record to
// keep, unticks any that aren't the same, and merges the rest in one click;
// another firm (a different CUI) is shown but can't be ticked. The merges in
// force, grouped as they were made, each one undoable alone or all at once.

let _dup = { open: false, view: 'pending', pending: 0, rows: [], gen: 0, busy: false, picks: {} };

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
    : (_dup.view === 'merged' ? mergedGroups(rows).map(mergedGroup) : rows.map(duplicateGroup)).join('');
  el.innerHTML = '<div class="table-card dup-panel"><div class="dup-head"><div><h3>' + escHtml(t('dup.title')) + '</h3>' +
    '<p class="muted">' + escHtml(t('dup.lede')) + '</p></div>' +
    '<div class="btn-row"><button type="button" class="btn btn-secondary btn-sm" onclick="scanDuplicates()">' + escHtml(t('dup.scan')) + '</button>' +
    '<button type="button" class="btn btn-ghost btn-sm" onclick="toggleDuplicates()">' + escHtml(t('dup.close')) + '</button></div></div>' +
    '<div class="segmented dup-tabs" role="group" aria-label="' + escHtml(t('dup.title')) + '">' + tab('pending', t('dup.tab.pending', {n: _dup.pending})) + tab('merged', t('dup.tab.merged')) + '</div>' +
    body + '</div>';
}

function dupReasons(reasons) {
  return (reasons || []).map(r => escHtml(t('dup.reason.' + r.code, {value: r.value || ''}))).join(' · ');
}

function dupSide(c, extra) {
  if (!c) return '<div class="dup-side"></div>';
  const place = String(c.location || '').replace(/,\s*Romania$/i, '');
  const phones = (c.phones || []).length ? c.phones : (c.phone ? [c.phone] : []);
  const domains = (c.domains || []).length ? c.domains : (c.domain ? [c.domain] : []);
  const line = parts => parts.filter(Boolean).join('<span class="dot-sep">·</span>');
  return '<div class="dup-side"><button type="button" class="link-btn cell-main" onclick="openCompany(' + jsq(c.id) + ')">' +
      escHtml(c.name || t('dup.no_name')) + '</button>' + (extra || '') +
    '<div class="cell-sub">' + line([c.cui ? '<span class="mono">' + escHtml(t('co.cui', {cui: c.cui})) + '</span>' : '',
      place ? escHtml(place) : '', c.address ? escHtml(c.address) : '']) + '</div>' +
    '<div class="cell-sub mono">' + (phones.length ? phones.map(escHtml).join(', ') : escHtml(t('co.no_phone'))) + '</div>' +
    ((c.emails || []).length ? '<div class="cell-sub mono">' + c.emails.map(escHtml).join(', ') + '</div>' : '') +
    (domains.length ? '<div class="cell-sub">' + domains.map(escHtml).join(', ') + '</div>' : '') +
    '<div class="cell-sub muted">' + line((c.sources || []).map(x => sourceLabel(String(x).replace(/^discover:/, ''))).concat(
      c.locations ? [escHtml(t('dup.locations', {n: c.locations}))] : [])) + '</div></div>';
}

// What the person chose in a group: the record to keep and the companies
// ticked as the same, kept while the list reloads.
function dupPick(g) {
  let pick = _dup.picks[g.key];
  if (!pick) {
    pick = {keep: g.keeper, out: {}};
    _dup.picks[g.key] = pick;
  }
  const keep = (g.members || []).find(m => m.id === pick.keep) || (g.members || [])[0] || {};
  // The firm the group merges into: the keeper's CUI, or, when the record
  // kept has none (a listing), the first member's that has one. Any other
  // CUI is another firm, whichever record is kept.
  const firm = keep.cui ? keep : ((g.members || []).find(m => m.id !== keep.id && m.cui) || {});
  return {keep: keep.id, keepCui: firm.cui || '', out: pick.out};
}

// Another firm than the group's: a different CUI, never one company.
function dupOtherFirm(m, pick) {
  return !!(m.cui && pick.keepCui && String(m.cui).replace(/\D/g, '').replace(/^0+/, '') !==
    String(pick.keepCui).replace(/\D/g, '').replace(/^0+/, ''));
}

function duplicateGroup(g) {
  const pick = dupPick(g);
  const members = g.members || [];
  const many = members.length > 2;
  const key = jsq(g.key);
  const rows = members.map(m => {
    const other = m.id !== pick.keep && dupOtherFirm(m, pick);
    const keepIt = '<label class="dup-pick"><input type="radio" name="keep-' + escHtml(g.key) + '"' +
      (m.id === pick.keep ? ' checked' : '') +
      ' onchange="dupSetKeep(' + key + ', ' + jsq(m.id) + ')"> ' + escHtml(t('dup.keep')) + '</label>';
    const same = m.id === pick.keep ? '' : many
      ? '<label class="dup-pick"><input type="checkbox"' + (!other && !pick.out[m.id] ? ' checked' : '') +
        (other ? ' disabled' : '') + ' onchange="dupToggle(' + key + ', ' + jsq(m.id) + ', this.checked)"> ' +
        escHtml(t('dup.in_group')) + '</label>' : '';
    const note = other ? '<div class="cell-sub warn">' + escHtml(t('dup.other_firm')) + '</div>' : '';
    return '<div class="dup-member' + (m.id === pick.keep ? ' keeper' : '') + (other ? ' other' : '') + '">' +
      '<div class="dup-choice">' + keepIt + same + '</div>' + dupSide(m, note) + '</div>';
  }).join('');
  const chosen = members.filter(m => m.id !== pick.keep && !dupOtherFirm(m, pick) && !pick.out[m.id]);
  const left = members.filter(m => m.id !== pick.keep && !dupOtherFirm(m, pick) && pick.out[m.id]);
  return '<div class="dup-pair dup-group"><div class="dup-why"><b>' + escHtml(t('dup.score', {n: Math.round((g.score || 0) * 100)})) + '</b> ' +
      (many ? escHtml(t('dup.members', {n: members.length})) + ' · ' : '') + dupReasons(g.reasons) + '</div>' +
    '<div class="dup-members">' + rows + '</div>' +
    '<div class="btn-row"><button type="button" class="btn btn-primary btn-sm"' + (chosen.length ? '' : ' disabled') +
      ' onclick="mergeGroup(' + key + ')">' + escHtml(chosen.length > 1 ? t('dup.merge_n', {n: chosen.length + 1}) : t('dup.same')) + '</button>' +
    (many ? '<button type="button" class="btn btn-secondary btn-sm"' + (left.length ? '' : ' disabled') +
      ' onclick="takeOut(' + key + ')">' + escHtml(t('dup.take_out', {n: left.length})) + '</button>' : '') +
    '<button type="button" class="btn btn-ghost btn-sm" onclick="separateGroup(' + key + ')">' +
      escHtml(t(many ? 'dup.all_different' : 'dup.different')) + '</button></div></div>';
}

function dupGroup(key) { return (_dup.rows || []).find(g => g.key === key); }

function dupSetKeep(key, id) {
  const g = dupGroup(key);
  if (!g) return;
  dupPick(g);
  _dup.picks[key].keep = id;
  delete _dup.picks[key].out[id];
  renderDuplicates();
}

function dupToggle(key, id, on) {
  const g = dupGroup(key);
  if (!g) return;
  dupPick(g);
  if (on) delete _dup.picks[key].out[id]; else _dup.picks[key].out[id] = true;
  renderDuplicates();
}

// The merges in force, as they were made: one card per group merge.
function mergedGroups(rows) {
  const out = [];
  const byGroup = {};
  rows.forEach(m => {
    const key = m.group_id ? 'g' + m.group_id : 'm' + m.id;
    if (!byGroup[key]) { byGroup[key] = {key: key, survivor: m.survivor, keeper: m.a, rows: []}; out.push(byGroup[key]); }
    byGroup[key].rows.push(m);
  });
  return out;
}

function mergedGroup(group) {
  const first = group.rows[0];
  const members = group.rows.map(m =>
    '<div class="dup-member"><div class="dup-why"><b>' + escHtml(t(m.decided_by === 'auto' ? 'dup.by_auto' : 'dup.by_you')) + '</b> ' +
      (m.score ? escHtml(t('dup.score', {n: Math.round(m.score * 100)})) + ' · ' : '') + dupReasons(m.reasons) + '</div>' +
      dupSide(m.b) +
      '<div class="btn-row"><button type="button" class="btn btn-secondary btn-sm" onclick="undoMerge(' + jsq(m.merged) + ')">' +
        escHtml(t('dup.undo')) + '</button></div></div>').join('');
  return '<div class="dup-pair dup-group"><div class="dup-why">' + escHtml(t('dup.kept')) + '</div>' +
    '<div class="dup-sides">' + dupSide(group.keeper) + '<div class="dup-side cell-sub">' +
      escHtml(t(group.rows.length > 1 ? 'dup.joined_n' : 'dup.joined_one', {n: group.rows.length})) + '</div></div>' +
    '<div class="dup-members">' + members + '</div>' +
    (group.rows.length > 1 ? '<div class="btn-row"><button type="button" class="btn btn-ghost btn-sm" onclick="undoGroup(' +
      jsq(first.merged) + ')">' + escHtml(t('dup.undo_group', {n: group.rows.length})) + '</button></div>' : '') + '</div>';
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
  if (panel) panel.querySelectorAll('button, input').forEach(b => { b.disabled = true; });
  try {
    return await api(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
  } finally {
    _dup.busy = false;
  }
}

async function dupDone(res, toast, key) {
  if (res === undefined) return;
  if (!res || res.success === false) { showToast(dupRefused(res), 'error'); return loadDuplicates(); }
  if (key) delete _dup.picks[key];
  showToast(toast);
  loadCompanies();
}

async function mergeGroup(key) {
  const g = dupGroup(key);
  if (!g) return;
  const pick = dupPick(g);
  const rest = (g.members || []).filter(m => m.id !== pick.keep && !dupOtherFirm(m, pick));
  const members = rest.filter(m => !pick.out[m.id]).map(m => m.id);
  const out = rest.filter(m => pick.out[m.id]).map(m => m.id);
  if (!members.length) return;
  const res = await dupPost('/api/duplicates/merge', {keep: pick.keep, members: members, out: out});
  dupDone(res, t(members.length > 1 ? 'dup.toast.merged_n' : 'dup.toast.merged', {n: members.length + 1}), key);
}

async function takeOut(key) {
  const g = dupGroup(key);
  if (!g) return;
  const pick = dupPick(g);
  const rest = (g.members || []).filter(m => !dupOtherFirm(m, pick) || m.id === pick.keep);
  const out = rest.filter(m => m.id !== pick.keep && pick.out[m.id]).map(m => m.id);
  if (!out.length) return;
  const res = await dupPost('/api/duplicates/separate', {out: out, rest: rest.filter(m => !out.includes(m.id)).map(m => m.id)});
  dupDone(res, t('dup.toast.taken_out', {n: out.length}), key);
}

async function separateGroup(key) {
  const g = dupGroup(key);
  if (!g) return;
  const res = await dupPost('/api/duplicates/separate', {out: (g.members || []).map(m => m.id)});
  dupDone(res, t('dup.toast.separate'), key);
}

async function undoMerge(companyId) {
  const res = await dupPost('/api/duplicates/undo', {company_id: companyId});
  dupDone(res, t('dup.toast.undone'));
}

async function undoGroup(companyId) {
  const res = await dupPost('/api/duplicates/undo', {company_id: companyId, group: true});
  dupDone(res, t('dup.toast.undone_group'));
}

async function scanDuplicates() {
  const res = await dupPost('/api/duplicates/scan', {});
  if (res === undefined) return;
  if (!res || res.success === false) { showToast(t('dup.refused.other'), 'error'); return loadDuplicates(); }
  showToast(t('dup.toast.scan'));
  renderDuplicates();
  const job = res.job;
  for (let i = 0; job && i < 120; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const q = await api('/api/jobs');
    const lane = ((q && q.lanes) || []).find(l => l.lane === 'duplicates');
    const busy = lane && [...(lane.running || []), ...(lane.waiting || [])].some(j => j.id === job);
    if (!busy) break;
  }
  loadDuplicates();
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
  const all = Object.keys(companyFilter()).length ? t('actions.all_results', {n: _co.total}) : t('actions.all_companies', {n: _co.total});
  el.innerHTML = menuHtml('company-menu', [
    {sep: true, label: t('actions.selection', {n: n})},
    {label: t('actions.csv'), action: 'exportCompanies(\'csv\', true)', disabled: !n},
    {label: t('actions.json'), action: 'exportCompanies(\'json\', true)', disabled: !n},
    {label: t('actions.anaf'), action: 'anafCompanies(true)', disabled: !n},
    {label: t('actions.find'), action: 'findForCompanies(true)', disabled: !n},
    {sep: true, label: all},
    {label: t('actions.csv'), action: 'exportCompanies(\'csv\', false)', disabled: !_co.total},
    {label: t('actions.json'), action: 'exportCompanies(\'json\', false)', disabled: !_co.total},
    {label: t('actions.anaf'), action: 'anafCompanies(false)', disabled: !_co.total},
    {label: t('actions.find'), action: 'findForCompanies(false)', disabled: !_co.total},
  ]);
}

// The contact finder for the selection, or the search's results (every
// company when nothing is searched): priced first, then run under a cap.
function findForCompanies(selection) {
  const body = selection ? {company_ids: [..._co.selected], force: true} : {filter: companyFilter()};
  openFinder('dupes-panel', body);
}

// The page's list (its filters and sort), or every company when nothing narrows it.
function companyListBody() {
  const f = companyFilter();
  return Object.keys(f).length ? {filter: f, sort: _co.sort, dir: _co.dir} : {};
}

// Check the selection, the list's results or every company at ANAF.
function anafCompanies(selection) {
  anafCheck(selection ? {ids: [..._co.selected]} : companyListBody());
}

function exportCompanies(format, selection) {
  const body = selection ? {ids: [..._co.selected]} : companyListBody();
  const narrowed = Object.keys(companyFilter()).length;
  const name = 'firme-' + (selection ? 'selectie' : narrowed ? 'cautare' : 'toate') + '-' + stamp() + '.' + format;
  postDownload('/api/export/companies.' + format, name, body);
}

function pageCompanies(how, value) {
  pageMove(_co, how, value);
  if (_co.total && _co.offset >= _co.total) _co.offset = Math.floor((_co.total - 1) / _co.limit) * _co.limit;
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
  if (_drawer.firm) return renderFirmDrawer();
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

  const findings = c.findings || {};
  const phones = [];
  const mainPhone = c.phone_display || c.phone;
  if (mainPhone) phones.push({n: mainPhone, kind: c.phone_type, main: true});
  (c.other_phones || []).forEach(n => phones.push({n: n}));
  const phoneRows = phones.map(p => {
    const href = telHref(p.n);
    const kind = p.kind ? t('phone.' + p.kind) : '';
    const found = (findings.phones || []).find(f => phoneTail(f.value) === phoneTail(p.n));
    return '<div class="drow">' +
      (href ? '<a class="mono big tel" href="' + href + '">' + escHtml(p.n) + '</a>' : '<span class="mono big">' + escHtml(p.n) + '</span>') +
      '<span class="drow-meta">' + [p.main ? escHtml(t('drawer.main')) : '', kind && kind !== 'phone.' + p.kind ? escHtml(kind) : '',
        found ? findingSources(found) : '']
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

  // Contacts, people, places and sources from the contacts model; the
  // people list falls back to the named contacts on older data.
  const reach = c.reach || {};
  const peopleList = (reach.people || []).length ? reach.people.map(p => ({name: p.name, title: p.role, reached: p.reached, own: true}))
    : (c.people || []);
  // Each person with the contacts found for them.
  const ownContacts = name => (reach.contacts || []).filter(r => r.person && r.person === name).map(r =>
    '<span class="person-lines">' + contactValue(r) + '</span>').join('');
  const people = peopleList.map(p =>
    '<div class="person"><span class="avatar">' + initials(...String(p.name).split(' ')) + '</span>' +
    '<div class="cell-stack"><span class="cell-main">' + escHtml(p.name) + '</span>' +
    (p.title ? '<span class="cell-sub">' + escHtml(roleText(p.title)) + '</span>' : '') +
    (p.own ? ownContacts(p.name) : '') + '</div>' +
    (p.own && !p.reached ? '<span class="tag">' + escHtml(t('drawer.to_search')) + '</span>' : '') + '</div>').join('');
  const reachRows = (reach.contacts || []).map(r =>
    '<div class="drow">' + contactValue(r) +
    '<span class="drow-meta">' + [r.status ? escHtml(statusMeta(r.status).label) : '', escHtml(statusMeta(r.trust).label),
      escHtml(r.person), r.shared_with ? escHtml(t('drawer.shared_with', {n: r.shared_with})) : '', (r.sources || [r.source]).map(sourceLabel).join(', ')]
      .filter(Boolean).join(' · ') + '</span></div>').join('');
  const placeRows = (reach.locations || []).map(l =>
    '<div class="drow addr"><span class="drow-text">' + escHtml([l.name, l.address].filter(Boolean).join(' — ')) + '</span>' +
    '<span class="drow-meta">' + [escHtml(l.phone), sourceLabel(l.source)].filter(Boolean).join(' · ') + '</span></div>').join('');
  const searchRows = (reach.searches || []).map(x =>
    '<div class="drow"><span class="drow-text">' + sourceLabel(x.module) + (x.person ? ' · ' + escHtml(x.person) : '') + '</span>' +
    '<span class="drow-meta">' + [x.searched_at ? escHtml(String(x.searched_at).slice(0, 16)) : '',
      escHtml(t('drawer.search.' + (x.outcome || 'nothing'), {n: x.found || 0}))].filter(Boolean).join(' · ') + '</span></div>').join('');
  const sourceRows = (reach.sources || []).map(x =>
    '<div class="drow"><span class="drow-text">' + sourceLabel(x.source) + '</span>' +
    '<span class="drow-meta">' + [escHtml(x.ref), x.first_seen ? escHtml(String(x.first_seen).slice(0, 10)) : ''].filter(Boolean).join(' · ') + '</span></div>').join('');

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
    [t('site.basis.title'), escHtml(siteBasisText(c.website_basis))],
    [t('drawer.source'), sourceLabel(c.source) + (safeHref(c.source_url) ? ' · <a href="' + safeHref(c.source_url) +
      '" target="_blank" rel="noopener noreferrer">' + escHtml(t('drawer.evidence')) + '</a>' : '')],
    [t('drawer.added'), relTime(c.created_at)],
  ].filter(f => f[1]);

  const addressRows = (findings.addresses || []).map(a =>
    '<div class="drow addr"><span class="drow-text">' + escHtml(a.value) + '</span>' +
    '<span class="drow-meta">' + findingSources(a) + '</span></div>').join('');
  const factRows = (findings.facts || []).map(f =>
    '<dt>' + escHtml(tServer('sigc.' + f.code + '.label', f.label)) + '</dt><dd>' + escHtml(factValue(f)) +
    '<span class="drow-meta"> · ' + escHtml(f.name) + (f.observed_at ? ' · ' + escHtml(String(f.observed_at).slice(0, 10)) : '') +
    '</span></dd>').join('');

  const rec = recordSections(c, section, none);
  el.innerHTML = head(escHtml(c.name), companyIdLine(c)) +
    '<div class="drawer-scroll">' + rec.identity +
    section(t('drawer.phones'), 'phone', phoneRows || none(t('drawer.no_phones')), phones.length) +
    (addressRows ? section(t('drawer.addresses'), 'pin', addressRows, (findings.addresses || []).length) : '') +
    (factRows ? section(t('drawer.official'), 'layers', '<dl class="facts-list">' + factRows + '</dl>') : '') +
    section(t('drawer.emails'), 'mail', emailRows || none(t('drawer.no_emails')), (c.emails || []).length) +
    (reachRows ? section(t('drawer.reach'), 'layers', reachRows, (reach.contacts || []).length) : '') +
    section(t('drawer.people'), 'contacts', people || none(t('drawer.no_people')), peopleList.length) +
    (placeRows ? section(t('drawer.locations'), 'pin', placeRows, (reach.locations || []).length) : '') +
    (sourceRows ? section(t('drawer.sources'), 'discover', sourceRows, (reach.sources || []).length) : '') +
    rec.rest +
    section(t('drawer.searches'), 'discover', (searchRows || none(t('drawer.never_searched'))) +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="findForOne(false)">' + escHtml(t('cl.find')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="findForOne(true)" title="' + escHtml(t('cl.find_force_title')) + '">' +
      escHtml(t('drawer.search_again')) + '</button></div><div id="drawer-finder"></div>', (reach.searches || []).length) +
    (contacts ? section(t('drawer.contacts'), 'layers', contacts, (_drawer.contacts || []).length) : '') +
    section(t('drawer.activities'), 'companies', companyActivities(c, true)) +
    section(t('drawer.details'), 'pin', '<dl class="facts-list">' + facts.map(f => '<dt>' + escHtml(f[0]) + '</dt><dd>' + f[1] + '</dd>').join('') + '</dl>') +
    '</div>' +
    '<div class="drawer-foot"><button class="btn btn-secondary btn-sm" onclick="exportOne(\'csv\')">' + icon('download') + 'CSV</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="exportOne(\'json\')">' + icon('download') + 'JSON</button>' +
    (c.cui ? '<button class="btn btn-secondary btn-sm" onclick="anafCheck({ids: [_drawer.id]})">' + escHtml(t('drawer.anaf')) + '</button>' : '') +
    '</div>';
}

// What the side databases hold on the company (c.record): its CUIs, every
// financial year, the trade registry, ANAF's answer, the taxpayer file.
// A database not loaded, or with nothing on the company, says so.
function recordSections(c, section, none, opts) {
  const o = opts || {};
  const r = c.record;
  if (!r) return {identity: section(t('rec.title'), 'layers', none(t('rec.unreadable'))), rest: ''};
  const missing = part => none(t(part && part.state === 'not_loaded' ? 'rec.not_loaded' : 'rec.none'));
  const dl = rows => '<dl class="facts-list">' + rows.filter(x => x[1] !== '' && x[1] !== null && x[1] !== undefined)
    .map(x => '<dt>' + escHtml(x[0]) + '</dt><dd>' + x[1] + '</dd>').join('') + '</dl>';
  const yn = v => v === null || v === undefined ? '' : escHtml(t(v ? 'drawer.yes' : 'drawer.no'));
  const day = v => v ? escHtml(String(v).slice(0, 10)) : '';
  const money = v => v === null || v === undefined ? '<span class="none">—</span>' : escHtml(fmtLei(v));
  let html = '';

  // Identity: CUIs (with who gave each), and the registry's or ANAF's legal form and status.
  const firm = ((r.registry || {}).firms || [])[0];
  const answer = ((r.anaf || {}).answers || []).find(a => a.found);
  const cuis = (r.cuis || []).map(x => '<span class="mono">' + escHtml(x.cui) + '</span>' +
    (x.sources.length ? ' <span class="drow-meta">' + x.sources.map(sourceLabel).join(', ') + '</span>' : '')).join('<br>');
  const identity = section(t('rec.identity'), 'companies', dl([
    [t('rec.cuis', {n: (r.cuis || []).length}), cuis || '<span class="none">' + escHtml(t('rec.no_cui')) + '</span>'],
    [t('rec.legal_form'), escHtml((firm && firm.form) || (answer && answer.legal_form) || '')],
    [t('rec.status'), escHtml((firm && firm.status) || (answer && answer.status) || '')],
    [t('rec.registered_office'), escHtml((firm && firm.address) || c.address || '')],
    [t('rec.registration'), firm ? '<span class="mono">' + escHtml(firm.reg) + '</span>' + (firm.registered ? ' · ' + day(firm.registered) : '') : ''],
  ]));

  // Financial years: one row per year (and CUI), newest first.
  const fin = r.financials || {};
  const years = fin.years || [];
  const multi = new Set(years.map(y => y.cui)).size > 1;
  // Years across (the Registry's firm panel): one column per year, a row per figure.
  const lei = v => v === null || v === undefined ? '<span class="none">—</span>' : escHtml(fmtN(Math.round(Number(v))));
  const across = () => '<div class="table-scroll"><table class="fin-table across"><thead><tr><th class="muted">lei</th>' +
    years.map(y => '<th>' + escHtml(String(y.year)) + (y.form ? ' <span class="muted">' + escHtml(y.form) + '</span>' : '') +
      (multi ? '<br><span class="muted mono">' + escHtml(y.cui) + '</span>' : '') + '</th>').join('') + '</tr></thead><tbody>' +
    [['rec.turnover', 'turnover', lei], ['rec.total_revenue', 'total_revenue', lei],
     ['rec.employees', 'employees', v => v === null || v === undefined ? '—' : escHtml(fmtN(v))],
     ['rec.net_result', 'net_result', lei], ['rec.equity', 'equity', lei], ['rec.debts', 'debts', lei]]
      .map(row => '<tr><th scope="row">' + escHtml(t(row[0])) + '</th>' +
        years.map(y => '<td class="mono">' + row[2](y[row[1]]) + '</td>').join('') + '</tr>').join('') +
    '</tbody></table></div>';
  html += section(t('rec.financials'), 'layers', years.length && o.yearsAcross ? across() : years.length
    ? '<div class="table-scroll"><table class="fin-table"><thead><tr><th>' + escHtml(t('rec.year')) + '</th>' +
      (multi ? '<th>CUI</th>' : '') + '<th>' + escHtml(t('rec.turnover')) + '</th><th>' + escHtml(t('rec.employees')) + '</th><th>' +
      escHtml(t('rec.net_result')) + '</th><th>' + escHtml(t('rec.equity')) + '</th><th>' + escHtml(t('rec.debts')) + '</th></tr></thead><tbody>' +
      years.map(y => '<tr><td class="nowrap">' + escHtml(String(y.year)) + (y.form ? ' <span class="muted">' + escHtml(y.form) + '</span>' : '') + '</td>' +
        (multi ? '<td class="mono">' + escHtml(y.cui) + '</td>' : '') +
        '<td class="mono">' + money(y.turnover) + '</td><td class="mono">' + (y.employees === null ? '—' : escHtml(fmtN(y.employees))) + '</td>' +
        '<td class="mono">' + money(y.net_result) + '</td><td class="mono">' + money(y.equity) + '</td><td class="mono">' + money(y.debts) + '</td></tr>').join('') +
      '</tbody></table></div>' + (fin.from_observations ? '<p class="muted small">' + escHtml(t('rec.from_observations')) + '</p>' : '')
    : missing(fin), years.length);

  // The trade registry: registration, every CAEN class, administrators.
  const reg = r.registry || {};
  html += section(t('rec.registry'), 'layers', (reg.firms || []).length ? reg.firms.map(f =>
    dl([[t('rec.name'), escHtml(f.name)], [t('rec.registration'), '<span class="mono">' + escHtml(f.reg) + '</span>'],
        [t('rec.registered'), day(f.registered)], [t('rec.status'), escHtml(f.status || '')],
        [t('rec.place'), escHtml([f.locality, f.county].filter(Boolean).join(', '))], [t('drawer.address'), escHtml(f.address || '')],
        [t('drawer.website'), escHtml(f.website || '')]]) +
    ((f.activities || []).length ? '<div class="acts">' + f.activities.map(a => '<div class="act"><span class="code">' + escHtml(a.code) +
      '</span><span class="aname">' + escHtml(a.name || a.name_en || '') + (a.revision ? ' <span class="muted">(' + escHtml(a.revision) + ')</span>' : '') +
      '</span></div>').join('') + '</div>' : '') +
    ((f.administrators || []).length ? '<h4 class="dsub">' + escHtml(t('rec.administrators')) + '</h4>' + f.administrators.map(p =>
      '<div class="drow"><span class="drow-text">' + escHtml(p.name) + '</span><span class="drow-meta">' + escHtml(p.role || '') + '</span></div>').join('') : '')
  ).join('') + (reg.dataset ? '<p class="muted small">' + escHtml(t('rec.dataset', {name: reg.dataset})) + '</p>' : '') : missing(reg));

  // ANAF's answer, as kept (30 days).
  const anaf = r.anaf || {};
  html += section(t('rec.anaf'), 'layers', (anaf.answers || []).length ? anaf.answers.map(a => a.found
    ? dl([['CUI', '<span class="mono">' + escHtml(a.cui) + '</span>'], [t('rec.vat'), yn(a.vat) + (a.vat_since ? ' · ' + escHtml(t('rec.since', {date: a.vat_since})) : '')],
          [t('rec.efactura'), yn(a.efactura)], [t('rec.inactive'), yn(a.inactive)], [t('rec.status'), escHtml(a.status || '')],
          [t('rec.legal_form'), escHtml(a.legal_form || '')], [t('drawer.address'), (a.addresses || []).map(escHtml).join('<br>')],
          [t('rec.phone'), (a.phones || []).map(escHtml).join(', ')], [t('rec.checked'), day(a.checked_at)]])
    : '<p class="none">' + escHtml(t('rec.anaf_unknown', {cui: a.cui, date: String(a.checked_at || '').slice(0, 10)})) + '</p>').join('')
    : missing(anaf));

  // The Ministry of Finance's taxpayer file.
  const tax = r.taxpayer || {};
  html += section(t('rec.taxpayer'), 'layers', (tax.rows || []).length ? tax.rows.map(x =>
    dl([['CUI', '<span class="mono">' + escHtml(x.cui) + '</span>'], [t('rec.name'), escHtml(x.name || '')],
        [t('drawer.address'), escHtml(x.address || '')], [t('rec.phone'), escHtml(x.phone || '')],
        [t('rec.vat'), yn(x.vat)], [t('rec.employer'), yn(x.employer)], [t('rec.micro'), yn(x.micro_tax)],
        [t('rec.status'), escHtml(x.status || '')], [t('rec.published'), day(x.published)]])).join('')
    : missing(tax));
  // Every signal on file: its latest reading, who observed it and when.
  if (r.signals === undefined) return {identity: identity, rest: html};
  const sigs = r.signals || [];
  html += section(t('rec.signals'), 'signals', sigs.length ? '<dl class="facts-list">' + sigs.map(x =>
    '<dt>' + escHtml(tServer('sigc.' + x.code + '.label', x.label)) + '</dt><dd>' + escHtml(factValue(x)) +
    '<span class="drow-meta"> · ' + sourceLabel(x.module) + (x.observed_at ? ' · ' + escHtml(String(x.observed_at).slice(0, 10)) : '') +
    '</span></dd>').join('') + '</dl>' : none(t('rec.no_signals')), sigs.length);
  return {identity: identity, rest: html};
}

// Find contacts for the open company (or search it again everywhere).
function findForOne(force) {
  if (_drawer.id) openFinder('drawer-finder', {company_ids: [_drawer.id], force: !!force});
}

// The last nine digits: one number written in two ways (+40 268..., 0268...).
function phoneTail(n) {
  return String(n || '').replace(/\D/g, '').slice(-9);
}

// Who gave a phone or an address, and when: official registers first.
function findingSources(f) {
  return (f.sources || []).map(s => (s.official ? '<b>' + escHtml(s.name) + '</b>' : escHtml(s.name)) +
    (s.observed_at ? ' ' + escHtml(String(s.observed_at).slice(0, 10)) : '')).join(', ');
}

function factValue(f) {
  if (f.value_type === 'bool') return f.value_num ? t('drawer.yes') : t('drawer.no');
  let v = f.value_text || '';
  if (f.value_num !== null && f.value_num !== undefined && f.value_type === 'num') {
    v = fmtN(Number(f.value_num)) + (f.unit ? ' ' + f.unit : '');
  }
  return v + (f.year ? ' (' + f.year + ')' : '');
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

// ── Contacts: every way to reach a company, one row per contact ──
//
// A chain's shared phone is one row reaching many companies (expand it to
// see them). "People to search" lists who is named at a company but has no
// email or phone of their own yet. The count is a capped query, so this
// list is fetched when asked for, never on the 15-second refresh.

let _cv = { view: 'contacts', q: '', f: {}, sort: '', dir: 'desc', offset: 0, limit: loadPageSize(),
            total: 0, more: false, rows: [], open: null, reach: {}, gen: 0, timer: null, force: false,
            sources: [], kinds: [], financials: false, signals: null, error: '', sel: new Map(),
            adding: false, checks: {}, check: '', review: [], verify: null };
const CONTACT_STATUSES = ['verified', 'risky', 'guess', 'unchecked', 'invalid', 'valid'];
const CONTACT_TRUST = ['confirmed', 'likely', 'unconfirmed'];
const CONTACT_LINES = ['mobile', 'fixed', 'voip', 'toll_free'];
const CONTACT_COLUMNS = [['value', 'cl.col.contact'], ['status', 'cl.col.status'], ['trust', 'cl.col.trust'],
                         ['companies', 'cl.col.companies'], [null, 'cl.col.person'], [null, 'cl.col.sources'],
                         ['first_seen', 'cl.col.first_seen']];
const PEOPLE_COLUMNS = [['name', 'cl.col.name'], ['company', 'cl.col.company'], [null, 'cl.col.place'],
                        [null, 'cl.col.sources'], ['first_seen', 'cl.col.first_seen'], ['searched_at', 'cl.col.searched']];

// The Contacts tab: the contacts list, the people still to search, and
// (Email module) the outreach list.
function loadProspects() {
  const el = document.getElementById('prospects-table');
  if (!_modules.email && _cv.view === 'outreach') _cv.view = 'contacts';
  if (!document.getElementById('ct-views')) {
    el.innerHTML = '<div class="toolbar"><div class="segmented" id="ct-views" role="group" data-i18n-aria="cl.views_label" ' +
      'aria-label="' + escHtml(t('cl.views_label')) + '"></div></div><div id="ct-body"></div>';
  }
  const views = ['contacts', 'people'].concat(_modules.email ? ['outreach'] : []);
  document.getElementById('ct-views').innerHTML = views.map(v =>
    '<button class="' + (v === _cv.view ? 'on' : '') + '" aria-pressed="' + (v === _cv.view) + '" onclick="setContactView(\'' + v + '\')">' +
      escHtml(t('cl.view.' + v)) + '</button>').join('');
  return _cv.view === 'outreach' ? loadOutreach() : loadContactList();
}

function setContactView(v) {
  if (v === _cv.view) return;
  _cv.view = v;
  _cv.offset = 0;
  _cv.sort = '';
  _cv.open = null;
  const body = document.getElementById('ct-body');
  if (body) body.innerHTML = '';
  loadProspects();
}

function contactParams(paged) {
  const p = new URLSearchParams();
  if (_cv.view === 'people') p.set('view', 'people');
  if (_cv.q.trim()) p.set('q', _cv.q.trim());
  Object.keys(_cv.f).forEach(k => { if (_cv.f[k] !== '' && _cv.f[k] !== undefined) p.set(k, _cv.f[k]); });
  if (_cv.sort) { p.set('sort', _cv.sort); p.set('dir', _cv.dir); }
  if (paged) { p.set('limit', _cv.limit); p.set('offset', _cv.offset); }
  return p.toString();
}

// The Contacts filters as a saved audience keeps them (no page, no sort).
function contactQuery() {
  const p = new URLSearchParams();
  if (_cv.q.trim()) p.set('q', _cv.q.trim());
  Object.keys(_cv.f).forEach(k => { if (_cv.f[k] !== '' && _cv.f[k] !== undefined) p.set(k, _cv.f[k]); });
  return p.toString();
}

async function loadContactList() {
  const body = document.getElementById('ct-body');
  if (!body) return;
  if (!document.getElementById('cl-rows')) {
    const ph = _cv.view === 'people' ? 'cl.people_search_ph' : searchPh('cl');
    body.innerHTML = '<div class="toolbar">' +
      '<label class="search grow">' + icon('discover') +
        '<input class="form-input" id="cl-search" type="search" autocomplete="off" data-i18n-placeholder="' + ph + '" ' +
        'data-i18n-aria="cl.search_label" placeholder="' + escHtml(t(ph)) + '" aria-label="' + escHtml(t('cl.search_label')) + '" ' +
        'oninput="contactListSearch(this.value)"></label><div id="cl-actions"></div></div>' +
      '<details class="more-fields" id="cl-more"><summary>' + escHtml(t('cl.filters')) + '</summary><div id="cl-filters"></div></details>' +
      '<div id="cl-finder"></div><div id="cl-addbar"></div><div id="cl-verify"></div><div id="cl-rows"></div>';
    loadFinderStatus();
    document.getElementById('cl-search').value = _cv.q;
    renderContactFilters();
  }
  if (_cv.signals === null) {
    _cv.signals = [];
    api('/api/signals').then(res => {
      _cv.signals = [];
      ((res && res.groups) || []).forEach(g => (g.signals || []).forEach(sig => {
        _cv.signals.push({code: sig.code, label: sig.label || sig.code});
      }));
      renderContactFilters();
    });
  }
  const gen = ++_cv.gen;
  if (_cv.check && _cv.view === 'contacts') return loadContactReview(gen);
  const res = await api('/api/contacts?' + contactParams(true));
  if (gen !== _cv.gen) return;
  const rowsEl = document.getElementById('cl-rows');
  if (!rowsEl) return;
  if (!res) { rowsEl.innerHTML = offlineState(); return; }
  const facets = JSON.stringify([res.sources, res.kinds, res.financials]);
  const changed = facets !== JSON.stringify([_cv.sources, _cv.kinds, _cv.financials]);
  Object.assign(_cv, {total: res.total || 0, more: !!res.more, rows: res.rows || [], error: res.error || '',
                      sources: res.sources || [], kinds: res.kinds || [], financials: !!res.financials});
  if (!_cv.rows.length && _cv.offset > 0 && _cv.total > 0) { _cv.offset = 0; return loadContactList(); }
  if (changed) renderContactFilters();
  renderContactList();
  if (_cv.view === 'contacts' && _cv.rows.length) loadContactVerdicts(_cv.rows.map(r => r.id), gen);
}

// The classifier's verdict still standing on each row shown (yes, no,
// unsure), fetched after the rows so the list never waits for it.
async function loadContactVerdicts(ids, gen) {
  const res = await api('/api/contacts/verify/verdicts?ids=' + ids.join(','));
  if (!res || !res.verdicts || (gen !== undefined && gen !== _cv.gen)) return;
  ids.forEach(id => { delete _cv.checks[id]; });
  Object.keys(res.verdicts).forEach(id => { _cv.checks[id] = res.verdicts[id]; });
  renderContactList();
}

// The links the classifier gave one verdict, still standing, newest first
// (unsure: the ones for a person to review).
async function loadContactReview(gen) {
  const rowsEl = document.getElementById('cl-rows');
  renderContactListActions();
  const res = await api('/api/contacts/verify/review?verdict=' + encodeURIComponent(_cv.check) + '&limit=200');
  if (gen !== _cv.gen || !rowsEl) return;
  if (!res) { rowsEl.innerHTML = offlineState(); return; }
  _cv.review = res.links || [];
  rowsEl.innerHTML = contactReviewHtml(_cv.review, _cv.check);
}

function contactReviewHtml(links, verdict) {
  const head = '<p class="muted small">' + escHtml(t('cl.review_note', {verdict: t('cl.v.' + verdict)})) + '</p>';
  if (!links.length) return '<div class="table-card">' + head + emptyState(icon('contacts'), escHtml(t('cl.review_empty', {verdict: t('cl.v.' + verdict)})), '') + '</div>';
  return '<div class="table-card">' + head + '<table class="data ct-table"><thead><tr><th>' + escHtml(t('cl.col.contact')) + '</th><th>' +
    escHtml(t('cl.col.company')) + '</th><th>' + escHtml(t('cl.col.verdict')) + '</th><th>' + escHtml(t('cl.col.checked')) + '</th></tr></thead><tbody>' +
    links.map(l => '<tr><td data-label="' + escHtml(t('cl.col.contact')) + '">' + contactValue({kind: l.kind, value: l.value}) + '</td>' +
      '<td data-label="' + escHtml(t('cl.col.company')) + '"><a href="#" class="cell-main co-link" onclick="openCompany(' + jsq(l.company_id) + ');return false">' +
        escHtml(l.company || l.company_id) + '</a></td>' +
      '<td data-label="' + escHtml(t('cl.col.verdict')) + '">' + verdictTag(l) + (l.note ? '<div class="cell-sub">' + escHtml(l.note) + '</div>' : '') + '</td>' +
      '<td class="muted nowrap" data-label="' + escHtml(t('cl.col.checked')) + '">' + (l.at ? escHtml(new Date(l.at * 1000).toLocaleString(intlLocale())) : '') +
        '<div class="cell-sub">' + escHtml(l.model || l.profile || '') + '</div></td></tr>').join('') + '</tbody></table></div>';
}

function setContactCheck(v) {
  _cv.check = v || '';
  _cv.offset = 0;
  loadContactList();
}

// A verdict as a tag: yes, no (with the undo of its run) or unsure.
function verdictTag(c, undo) {
  if (!c || !c.verdict) return '';
  const tone = {yes: 't-good', no: 't-bad', unsure: 't-warn'}[c.verdict] || '';
  const p = typeof c.p === 'number' ? ' ' + Math.round(c.p * 100) + '%' : '';
  return '<span class="tag ' + tone + '" title="' + escHtml(t('cl.v.title', {model: c.model || c.profile || ''})) + '">' +
      escHtml(t('cl.v.tag', {verdict: t('cl.v.' + c.verdict)}) + p) + '</span>' +
    (undo && c.verdict === 'no' && c.batch ? ' <button type="button" class="link-btn small" onclick="undoVerify(' + jsq(c.batch) + ')">' +
      escHtml(t('cl.v.undo')) + '</button>' : '');
}
function contactListSearch(value) {
  _cv.q = value;
  _cv.offset = 0;
  clearTimeout(_cv.timer);
  _cv.timer = setTimeout(loadContactList, 250);
}

function setContactListFilter(key, value) {
  if (value === '' || value === null || value === undefined) delete _cv.f[key]; else _cv.f[key] = String(value);
  _cv.offset = 0;
  clearTimeout(_cv.timer);
  _cv.timer = setTimeout(loadContactList, 250);
}

function clearContactFilters() {
  _cv.f = {};
  _cv.offset = 0;
  renderContactFilters();
  loadContactList();
}

function sortContacts(col) {
  if (_cv.sort === col) _cv.dir = _cv.dir === 'asc' ? 'desc' : 'asc';
  else { _cv.sort = col; _cv.dir = col === 'first_seen' || col === 'companies' ? 'desc' : 'asc'; }
  _cv.offset = 0;
  loadContactList();
}

function pageContactList(how, value) {
  pageMove(_cv, how, value);
  loadContactList().then(() => {
    const top = document.getElementById('prospects');
    if (top && how !== 'size') top.scrollIntoView({block: 'start'});
  });
}

function renderContactFilters() {
  const el = document.getElementById('cl-filters');
  if (!el) return;
  const f = _cv.f;
  const opt = (value, label, current) => '<option value="' + escHtml(value) + '"' + (String(current || '') === value ? ' selected' : '') + '>' + escHtml(label) + '</option>';
  const select = (key, label, options) => '<div class="form-group"><label class="form-label" for="cl-f-' + key + '">' + escHtml(t(label)) + '</label>' +
    '<select class="form-input" id="cl-f-' + key + '" onchange="setContactListFilter(\'' + key + '\', this.value)">' +
    opt('', t('cl.f.any'), f[key]) + options.map(o => opt(o[0], o[1], f[key])).join('') + '</select></div>';
  const input = (key, label, type) => '<div class="form-group"><label class="form-label" for="cl-f-' + key + '">' + escHtml(t(label)) + '</label>' +
    '<input class="form-input" id="cl-f-' + key + '" type="' + (type || 'text') + '" value="' + escHtml(f[key] || '') + '" ' +
    'oninput="setContactListFilter(\'' + key + '\', this.value)"></div>';
  const yesNo = [['yes', t('cl.f.yes')], ['no', t('cl.f.no')]];
  const signals = (_cv.signals || []).map(s => [s.code, tServer('sigc.' + s.code + '.label', s.label)]);
  const people = _cv.view === 'people';
  let html = '<div class="form-row">';
  if (!people) {
    const kinds = [];
    (_cv.kinds || []).forEach(k => {
      kinds.push([k.kind, kindLabel(k.kind)]);
      if (k.kind === 'phone') kinds.push(['mobile', t('cl.f.mobile')]);
    });
    html += select('kind', 'cl.f.kind', kinds) +
      select('status', 'cl.f.status', CONTACT_STATUSES.map(v => [v, statusMeta(v).label])) +
      select('trust', 'cl.f.trust', CONTACT_TRUST.map(v => [v, statusMeta(v).label])) +
      select('person', 'cl.f.person', yesNo) +
      '<div class="form-group"><label class="form-label" for="cl-f-check">' + escHtml(t('cl.f.check')) + '</label>' +
      '<select class="form-input" id="cl-f-check" onchange="setContactCheck(this.value)">' + opt('', t('cl.f.any'), _cv.check) +
      ['unsure', 'no', 'yes'].map(v => opt(v, t('cl.v.' + v), _cv.check)).join('') + '</select></div>';
  }
  html += select('source', 'cl.f.source', (_cv.sources || []).map(v => [v, sourceText(v)])) +
    select('website', 'cl.f.website', yesNo) + '</div><div class="form-row">' +
    input('county', 'cl.f.county') + input('locality', 'cl.f.locality') +
    select('require', 'cl.f.require', signals) + select('exclude', 'cl.f.exclude', signals) + '</div>';
  if (_cv.financials) {
    html += '<div class="form-row">' + input('min_turnover', 'cl.f.min_turnover', 'number') + input('max_turnover', 'cl.f.max_turnover', 'number') +
      input('min_employees', 'cl.f.min_employees', 'number') + input('max_employees', 'cl.f.max_employees', 'number') +
      '<div class="form-group"><label class="form-label"><input type="checkbox"' + (f.unknown === 'include' ? ' checked' : '') +
      ' onchange="setContactListFilter(\'unknown\', this.checked ? \'include\' : \'\')"> ' + escHtml(t('cl.f.unknown')) + '</label></div></div>';
  }
  el.innerHTML = html + '<div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="clearContactFilters()">' +
    escHtml(t('cl.f.clear')) + '</button></div>';
}

function contactHeader(cols) {
  return '<thead><tr>' + cols.map(c => {
    if (!c[0]) return '<th>' + escHtml(t(c[1])) + '</th>';
    const on = _cv.sort === c[0];
    return '<th aria-sort="' + (on ? (_cv.dir === 'asc' ? 'ascending' : 'descending') : 'none') + '">' +
      '<button type="button" class="link-btn" onclick="sortContacts(\'' + c[0] + '\')" title="' + escHtml(t('cl.sort_by', {col: t(c[1])})) + '">' +
      escHtml(t(c[1])) + (on ? (_cv.dir === 'asc' ? ' &#9650;' : ' &#9660;') : '') + '</button></th>';
  }).join('') + '</tr></thead>';
}

// A contact kind in words: the label its module declares, translated.
function kindLabel(kind) {
  const k = (_cv.kinds || []).find(x => x.kind === kind);
  return tServer('ck.' + kind, k ? k.label : kind);
}

function contactValue(r) {
  const shown = r.display || r.value;
  let link = '';
  if (r.kind === 'phone') link = telHref(r.value);
  else if (r.kind === 'email') link = mailHref(r.value);
  else link = safeHref(r.value);
  const ext = r.kind !== 'phone' && r.kind !== 'email';
  const main = link ? '<a class="' + (r.kind === 'phone' ? 'mono tel' : 'email') + '" href="' + link + '"' +
    (ext ? ' target="_blank" rel="noopener noreferrer"' : '') + '>' + escHtml(shown) + '</a>' : '<span class="mono">' + escHtml(shown) + '</span>';
  const kind = r.kind === 'phone' && r.line_type && r.line_type !== 'unknown' ? t('cl.line.' + r.line_type) : kindLabel(r.kind);
  return '<div class="contact-line">' + main + '<button class="copy" onclick="copyText(' + jsq(r.value) + ', this)" aria-label="' +
    escHtml(t('common.copy')) + '">' + icon('copy') + '</button></div><div class="cell-sub"><span class="tag">' + escHtml(kind) + '</span>' +
    (r.do_not_contact ? ' <span class="tag t-bad">' + escHtml(t('cl.do_not_contact')) + '</span>' : '') + '</div>';
}

function renderContactList() {
  const rowsEl = document.getElementById('cl-rows');
  if (!rowsEl) return;
  renderContactListActions();
  const people = _cv.view === 'people';
  if (_cv.error) { rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), escHtml(t('cl.bad_filter')), escHtml(_cv.error)) + '</div>'; return; }
  if (!_cv.rows.length) {
    const filtered = _cv.q.trim() || Object.keys(_cv.f).length;
    rowsEl.innerHTML = '<div class="table-card">' + (filtered
      ? emptyState(icon('discover'), escHtml(t('co.no_match')), escHtml(t('cl.no_match_copy')))
      : emptyState(icon('contacts'), escHtml(t(people ? 'cl.people_empty' : 'ct.empty')), escHtml(t(people ? 'cl.people_empty_copy' : 'ct.empty_copy')))) + '</div>';
    return;
  }
  // Any contact can be ticked to be verified with the classifier; ticked
  // email addresses can be added to a campaign (Email module).
  const ticks = !people;
  const allOn = _cv.rows.length > 0 && _cv.rows.every(r => _cv.sel.has(r.id));
  let head = contactHeader(people ? PEOPLE_COLUMNS : CONTACT_COLUMNS);
  if (ticks) {
    head = head.replace('<tr>', '<tr><th class="sel"><input type="checkbox" aria-label="' + escHtml(t('sel.page')) + '"' +
      (allOn ? ' checked' : '') + ' onchange="selectAddressPage(this.checked)"></th>');
  }
  let html = '<div class="table-card"><table class="data ct-table">' + head + '<tbody>';
  _cv.rows.forEach((r, i) => {
    if (people) {
      html += '<tr><td class="ct-name"><div class="cell-row"><span class="avatar">' + initials(...String(r.name).split(' ')) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + escHtml(r.name) + '</span><span class="cell-sub">' + escHtml(roleText(r.role)) + '</span></div></div></td>' +
        '<td data-label="' + escHtml(t('cl.col.company')) + '"><a href="#" class="cell-main co-link" onclick="openCompany(' + jsq(r.company_id) + ');return false">' +
          escHtml(r.company_name) + '</a>' + (r.cui ? '<div class="cell-sub mono">' + escHtml(r.cui) + '</div>' : '') + '</td>' +
        '<td class="muted" data-label="' + escHtml(t('cl.col.place')) + '">' + escHtml(String(r.location || '').replace(/,\s*Romania$/i, '')) + '</td>' +
        '<td class="muted" data-label="' + escHtml(t('cl.col.sources')) + '">' + sourceLabel(r.source) + '</td>' +
        '<td class="muted nowrap" data-label="' + escHtml(t('cl.col.first_seen')) + '">' + relTime(r.first_seen) + '</td>' +
        '<td class="muted nowrap" data-label="' + escHtml(t('cl.col.searched')) + '">' + (r.searched_at ? relTime(r.searched_at) : escHtml(t('cl.never'))) + '</td></tr>';
      return;
    }
    const open = _cv.open === r.id;
    const co = r.company || {};
    const reach = '<button type="button" class="link-btn" aria-expanded="' + open + '" onclick="toggleContactReach(' + i + ')">' +
      escHtml(r.companies > 1 ? t('kpi.fact_companies', {n: r.companies}) : t('cl.details')) + '</button>';
    const tick = !ticks ? '' : '<td class="sel" data-label=""><input type="checkbox" aria-label="' + escHtml(t('sel.row', {name: r.value})) + '"' +
        (_cv.sel.has(r.id) ? ' checked' : '') + ' onchange="selectAddress(' + i + ', this.checked)"></td>';
    html += '<tr class="' + (open ? 'selected' : '') + '">' + tick + '<td data-label="' + escHtml(t('cl.col.contact')) + '">' + contactValue(r) + '</td>' +
      '<td data-label="' + escHtml(t('cl.col.status')) + '">' + (r.status ? badge(r.status) : '') + '</td>' +
      '<td data-label="' + escHtml(t('cl.col.trust')) + '">' + badge(r.trust) +
        (_cv.checks[r.id] ? '<div class="cell-sub">' + verdictTag(_cv.checks[r.id], true) + '</div>' : '') + '</td>' +
      '<td data-label="' + escHtml(t('cl.col.companies')) + '"><div class="cell-stack">' +
        (co.id ? '<a href="#" class="cell-main co-link" onclick="openCompany(' + jsq(co.id) + ');return false">' + escHtml(co.name) + '</a>' : '') +
        '<span class="cell-sub">' + (r.companies > 1 ? reach : escHtml(String(co.location || '').replace(/,\s*Romania$/i, '')) + ' ' + reach) + '</span></div></td>' +
      '<td data-label="' + escHtml(t('cl.col.person')) + '">' + escHtml(co.person || '') +
        (r.people > 1 ? '<div class="cell-sub">' + escHtml(t('cl.people_n', {n: r.people})) + '</div>' : '') + '</td>' +
      '<td class="muted" data-label="' + escHtml(t('cl.col.sources')) + '">' + (r.sources || []).map(sourceLabel).join(', ') + '</td>' +
      '<td class="muted nowrap" data-label="' + escHtml(t('cl.col.first_seen')) + '">' + relTime(r.first_seen) + '</td></tr>';
    if (open) html += '<tr class="detail-row"><td colspan="' + (ticks ? 8 : 7) + '">' + contactReachHtml(_cv.reach[r.id], r) + '</td></tr>';
  });
  rowsEl.innerHTML = html + '</tbody></table></div>' + pagerHtml(_cv, _cv.total, 'pageContactList', {more: _cv.more, jump: true});
}

async function toggleContactReach(i) {
  const r = _cv.rows[i];
  if (!r) return;
  _cv.open = _cv.open === r.id ? null : r.id;
  renderContactList();
  if (_cv.open === null || _cv.reach[r.id]) return;
  const [data, checks] = await Promise.all([api('/api/contacts/' + encodeURIComponent(r.id)),
                                            api('/api/contacts/' + encodeURIComponent(r.id) + '/checks')]);
  _cv.reach[r.id] = data ? Object.assign({}, data, {checks: (checks && checks.checks) || []}) : {error: true};
  if (_cv.open === r.id) renderContactList();
}

function contactReachHtml(data, r) {
  if (!data) return '<div class="skeleton"></div>';
  if (data.error) return '<p class="none">' + escHtml(t('drawer.error')) + '</p>';
  // The classifier's verdicts on this contact's links, and verifying it.
  const checks = (data.checks || []).map(c => '<li>' + verdictTag(c, !c.undone) + ' ' + escHtml(c.company_id ? t('cl.v.at_company', {
      company: ((data.links || []).find(l => String(l.company_id) === String(c.company_id)) || {}).company || c.company_id}) : '') +
    (c.at ? ' · ' + escHtml(new Date(c.at * 1000).toLocaleString(intlLocale())) : '') + (c.undone ? ' · ' + escHtml(t('cl.v.undone')) : '') +
    (c.note ? ' <span class="muted">' + escHtml(c.note) + '</span>' : '') + '</li>').join('');
  const verify = r ? '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="openVerify([' + Number(r.id) + '])">' +
    escHtml(t('cl.verify_one')) + '</button></div>' : '';
  return (checks ? '<ul class="small cls-list">' + checks + '</ul>' : '') + verify +
    '<table class="data"><tbody>' + (data.links || []).map(l =>
    '<tr><td><a href="#" class="co-link" onclick="openCompany(' + jsq(l.company_id) + ');return false">' + escHtml(l.company) + '</a>' +
      (l.cui ? ' <span class="muted mono">' + escHtml(l.cui) + '</span>' : '') + '</td>' +
    '<td class="muted">' + escHtml(l.address || String(l.location || '').replace(/,\s*Romania$/i, '')) + '</td>' +
    '<td>' + escHtml(l.person) + (l.role ? ' <span class="muted">' + escHtml(roleText(l.role)) + '</span>' : '') + '</td>' +
    '<td>' + badge(l.trust) + '</td><td class="muted">' + sourceLabel(l.source) + '</td></tr>').join('') + '</tbody></table>';
}

function renderContactListActions() {
  const el = document.getElementById('cl-actions');
  if (!el) return;
  // Find contacts: the companies on file not yet searched; "search again"
  // also those already searched (each search is kept, see the company panel).
  const find = '<div class="btn-row"><label class="small" title="' + escHtml(t('cl.find_force_title')) + '">' +
    '<input type="checkbox" id="cl-force"' + (_cv.force ? ' checked' : '') + ' onchange="_cv.force = this.checked"> ' +
    escHtml(t('cl.find_force')) + '</label>' +
    '<button class="btn btn-secondary btn-sm" onclick="findContacts()">' + escHtml(t('cl.find')) + '</button>';
  // Campaigns (Email module): the filter as an audience, the ticked rows into a campaign.
  const email = _modules.email ? [
    {sep: true, label: t('cl.campaign_label')},
    {label: t('cl.save_audience'), note: t('cl.save_audience_note'), icon: 'campaigns', action: 'saveAudienceFromContacts()'},
    {label: t('cl.add_to_campaign', {n: selectedEmails().length}), icon: 'campaigns', action: 'openAddToCampaign()', disabled: !selectedEmails().length},
  ] : [];
  // The classifier: verify the ticked contacts; the review list by verdict.
  const verify = [
    {sep: true, label: t('cl.verify_label')},
    {label: t('cl.verify', {n: _cv.sel.size}), note: t('cl.verify_note'), icon: 'check', action: 'openVerify()', disabled: !_cv.sel.size},
  ];
  el.innerHTML = find + (_cv.view === 'people' ? '' : menuHtml('cl-menu', [
    {sep: true, label: t('cl.export_label')},
    {label: t('actions.csv'), note: t('cl.export_note'), action: 'exportContactList(\'csv\')', disabled: !_cv.total},
    {label: t('cl.export_json'), action: 'exportContactList(\'json\')', disabled: !_cv.total},
  ].concat(verify, email))) + '</div>';
}

// The ticked contacts, by id ({kind, value}).
function selectAddress(i, on) {
  const r = _cv.rows[i];
  if (!r) return;
  if (on) _cv.sel.set(r.id, {kind: r.kind, value: r.value}); else _cv.sel.delete(r.id);
  renderContactList();
}

function selectAddressPage(on) {
  _cv.rows.forEach(r => { if (on) _cv.sel.set(r.id, {kind: r.kind, value: r.value}); else _cv.sel.delete(r.id); });
  renderContactList();
}

function selectedEmails() {
  return [..._cv.sel.values()].filter(x => x.kind === 'email').map(x => x.value);
}

async function saveAudienceFromContacts() {
  const name = prompt(t('cl.audience_name_prompt'));
  if (!name || !name.trim()) return;
  const res = await api('/api/audiences', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({name: name.trim(), query: contactQuery()})});
  if (res && res.success) showToast(t('cl.audience_saved', {name: name.trim()}), 'success');
  else showToast(failMsg(res, t('camp.save_failed')), 'error');
}

// Pick a campaign (one not finished, made on the Campaigns page) for the ticked addresses.
async function openAddToCampaign() {
  const el = document.getElementById('cl-addbar');
  if (!el || !selectedEmails().length) return;
  const data = await api('/api/campaigns');
  const open = (data || []).filter(c => c.origin === 'user' && ['draft', 'active', 'paused'].includes(c.status));
  if (!open.length) { showToast(t('cl.no_campaign'), 'error'); return; }
  el.innerHTML = '<div class="card"><div class="form-row"><div class="form-group"><label class="form-label" for="cl-add-camp">' +
    escHtml(t('cl.add_to_campaign', {n: selectedEmails().length})) + '</label><select class="form-input" id="cl-add-camp">' +
    open.map(c => '<option value="' + escHtml(c.id) + '">' + escHtml(c.name) + ' (' + escHtml(statusMeta(c.status).label) + ')</option>').join('') +
    '</select></div><div class="btn-row"><button class="btn btn-primary btn-sm" onclick="addSelectedToCampaign()">' + escHtml(t('cl.add')) +
    '</button><button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'cl-addbar\').innerHTML=\'\'">' +
    escHtml(t('common.cancel')) + '</button></div></div><p class="muted small">' + escHtml(t('cl.add_rules')) + '</p></div>';
}

async function addSelectedToCampaign() {
  const sel = document.getElementById('cl-add-camp');
  if (!sel || _cv.adding) return;
  _cv.adding = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(sel.value) + '/members', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({emails: selectedEmails()})});
    if (!res || !res.success) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return; }
    showToast(t('cl.added', {n: res.counts.added, left: res.counts.total - res.counts.added + (res.counts.not_found || 0)}), 'success');
    [..._cv.sel.entries()].forEach(([id, x]) => { if (x.kind === 'email') _cv.sel.delete(id); });
    document.getElementById('cl-addbar').innerHTML = '';
    renderContactList();
  } finally {
    _cv.adding = false;
  }
}

// Verify contacts with the classifier (its contacts task): counted and
// priced first, then run under a cap; each answer (yes / no / unsure)
// lands on its row. A no lowers what a website's read gave the link to
// unconfirmed, and its run's undo puts it back; official sources and
// contacts in a campaign are never lowered.
async function openVerify(ids) {
  const el = document.getElementById('cl-verify');
  const chosen = ids || [..._cv.sel.keys()];
  if (!el || !chosen.length) return;
  _cv.verify = {ids: chosen};
  el.innerHTML = '<div class="card"><p class="muted small">' + escHtml(t('disc.pricing')) + '</p></div>';
  const est = await api('/api/contacts/verify/estimate', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                          body: JSON.stringify({contact_ids: chosen})});
  el.innerHTML = verifyEstimateHtml(est);
}

function verifyEstimateHtml(est) {
  if (!est || est.success === false || est.error) return '<div class="test-result error">' + escHtml(failMsg(est, t('cls.failed'))) + '</div>';
  const close = '<button class="btn btn-ghost btn-sm" onclick="closeVerify()">' + escHtml(t('common.cancel')) + '</button>';
  if (est.off) {
    return '<div class="card finder-card"><h2>' + escHtml(t('cl.verify_title')) + '</h2><p>' + escHtml(t('cl.verify_off')) + '</p>' +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="goTab(\'settings\')">' + escHtml(t('cl.verify_settings')) + '</button>' + close + '</div></div>';
  }
  // (Price unknown: nothing can be counted in USD; a small cap still bounds the run.)
  const cap = est.price_known ? Math.max(0.01, Math.ceil((est.usd_high || 0) * 100) / 100) : 0.05;
  return '<div class="card finder-card"><h2>' + escHtml(t('cl.verify_title')) + '</h2>' +
    '<p>' + escHtml(t('cl.verify_summary', {links: fmtN(est.links || 0), companies: fmtN(est.companies || 0),
      profile: est.profile_name || est.profile || '', model: est.model || ''})) + '</p>' +
    '<p class="muted small">' + escHtml(t(est.price_known ? 'cl.verify_cost' : 'cl.verify_cost_unknown',
      {n: fmtN(est.calls || 0), tok: fmtN(est.tokens_typical || 0), tokh: fmtN(est.tokens_high || 0),
       basis: t(est.basis === 'guess' ? 'est.basis_guess' : 'est.basis_month'),
       low: fmtUsd(est.usd_typical || 0, 4), high: fmtUsd(est.usd_high || 0, 4)})) + '</p>' +
    '<p class="muted small">' + escHtml(t('est.enforced')) + '</p>' +
    '<p class="muted small">' + escHtml(t('cl.verify_rules')) + '</p>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="cl-verify-cap">' + escHtml(t('finder.ccap')) + '</label>' +
      '<input class="form-input" id="cl-verify-cap" type="number" min="0" step="0.01" value="' + cap + '"></div></div>' +
    '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="runVerify(this)"' + (est.links ? '' : ' disabled') + '>' +
      escHtml(t('cl.verify_run', {n: fmtN(est.links || 0)})) + '</button>' + close + '</div></div>';
}

function closeVerify() {
  const el = document.getElementById('cl-verify');
  if (el) el.innerHTML = '';
  _cv.verify = null;
}

async function runVerify(btn) {
  const v = _cv.verify;
  if (!v) return;
  const cap = Number((document.getElementById('cl-verify-cap') || {}).value);
  if (!(cap >= 0)) { showToast(t('cls.t.cap_bad'), 'error'); return; }
  if (btn) btn.disabled = true;
  const res = await api('/api/contacts/verify', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({contact_ids: v.ids, max_spend: cap})});
  if (!res || !res.success) { if (btn) btn.disabled = false; showToast(failMsg(res, t('cls.failed')), 'error'); return; }
  if (!res.batch_id) { showToast(t('cl.verify_nothing'), 'success'); closeVerify(); return; }
  showToast(t('cl.verify_queued', {n: fmtN(res.links || 0)}), 'success');
  v.batch = res.batch_id;
  const el = document.getElementById('cl-verify');
  for (let i = 0; i < 2000; i++) {
    const r = await api('/api/jobs/batches/' + encodeURIComponent(res.batch_id) + '/report');
    if (_cv.verify !== v || !el) return;
    if (!r || !r.statuses) break;
    const active = runActive(r);
    if (!active) await refreshVerdicts(v);
    el.innerHTML = '<div class="card finder-card"><h2>' + escHtml(t(active ? 'cl.verify_running' : 'cl.verify_done')) + '</h2>' +
      (active ? '' : verifyCountsHtml(v)) + runReportHtml(r) +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="openQueue(' + jsq(res.batch_id) + ')">' + escHtml(t('disc.see_jobs')) + '</button>' +
      (!active && v.no ? '<button class="btn btn-secondary btn-sm" onclick="undoVerify(' + jsq(res.batch_id) + ')">' + escHtml(t('cl.v.undo_run')) + '</button>' : '') +
      '<button class="btn btn-ghost btn-sm" onclick="closeVerify()">' + escHtml(t('common.close')) + '</button></div></div>';
    if (!active) return;
    await new Promise(ok => setTimeout(ok, 3000));
    // (Each verdict on its row as it lands.)
    await refreshVerdicts(v);
  }
}

// The verdicts of a verification's contacts, onto the rows, counted.
async function refreshVerdicts(v) {
  const res = await api('/api/contacts/verify/verdicts?ids=' + v.ids.join(','));
  if (!res || !res.verdicts) return;
  v.yes = v.no = v.unsure = 0;
  v.ids.forEach(id => {
    const c = res.verdicts[id];
    if (c) { _cv.checks[id] = c; if (c.batch === v.batch) v[c.verdict] = (v[c.verdict] || 0) + 1; }
  });
  delete _cv.reach[_cv.open];
  renderContactList();
}

function verifyCountsHtml(v) {
  return '<p>' + escHtml(t('cl.verify_counts', {yes: fmtN(v.yes || 0), no: fmtN(v.no || 0), unsure: fmtN(v.unsure || 0)})) + '</p>';
}

async function undoVerify(batch) {
  if (!confirm(t('cl.v.undo_confirm'))) return;
  const res = await api('/api/contacts/verify/undo', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                      body: JSON.stringify({batch: batch})});
  showToast(res && res.success ? t('cl.v.undone_toast', {n: fmtN(res.restored || 0)}) : failMsg(res, t('cls.failed')),
            res && res.success ? 'success' : 'error');
  _cv.reach = {};
  if (_cv.verify && _cv.verify.batch === batch) closeVerify();
  loadContactList();
}

// Find contacts for the page's filter: priced first (websites to read are
// free, a company with no website is a paid search), then run under a cap.
function findContacts() {
  const filter = Object.assign({}, _cv.f);
  if (_cv.q.trim()) filter.q = _cv.q.trim();
  openFinder('cl-finder', {filter: filter, force: !!_cv.force});
}

// The contact finder's backlog, above the Contacts list.
async function loadFinderStatus() {
  const el = document.getElementById('cl-finder');
  const s = await api('/api/contacts/finder');
  if (!el || !s || el.dataset.open) return;
  const waiting = ((s.jobs || {}).queued || 0) + ((s.jobs || {}).running || 0);
  el.innerHTML = '<p class="muted small finder-status">' + escHtml(t('finder.backlog', {
    n: fmtN(s.without_contact || 0), site: fmtN(s.site_to_read || 0), done: fmtN(s.searched || 0)})) +
    ' &middot; ' + escHtml(t('finder.last_day', {n: fmtN((s.last_day || {}).companies || 0),
                                                 found: fmtN((s.last_day || {}).found || 0)})) +
    (waiting ? ' &middot; ' + escHtml(t('finder.waiting', {n: fmtN(waiting)})) : '') +
    (s.day_budget ? '' : ' &middot; ' + escHtml(t('finder.no_budget'))) + '</p>' +
    backgroundSearchLine(s.background_search, s.no_website || 0);
}

// The background's website searches inside free allowances, beside the
// backlog: on or off, and each search's period counts.
function backgroundSearchLine(bg, noSite) {
  if (!bg) return '';
  if (!bg.on) return '<p class="muted small finder-status">' + escHtml(t('finder.bg_off')) + '</p>';
  const name = (m) => tServer('module.' + m + '.name', m);
  const per = (p) => t('finder.per.' + p);
  const rows = (bg.searchers || []).map(x => name(x.module) + ': ' +
    Object.keys(x.allowance || {}).map(p => t('finder.bg_used', {used: fmtN((x.used || {})[p] || 0),
      of: fmtN(x.allowance[p]), per: per(p)})).join(', ') +
    ' (' + t('finder.bg_left', {n: fmtN(x.left || 0), d: fmtN((x.last_day || {}).searches || 0),
                                found: fmtN((x.last_day || {}).found || 0)}) + ')');
  return '<p class="muted small finder-status">' + escHtml(t('finder.bg', {n: fmtN(noSite),
    share: Math.round((bg.share || 0) * 100)})) + ' ' +
    (rows.length ? rows.map(escHtml).join(' &middot; ') : escHtml(t('finder.bg_none'))) + '</p>';
}

// Settings → Model: which subscription Harvey thinks with (Claude or
// Codex), the model, Codex's reasoning effort, how the CLI is logged in
// (brain.auth: only the ways whose flow exists are offered) and its status.
// Codex's models are its account's live list (/api/brain?full=1), read
// when Codex is chosen. With an API key: the key box (checked with a free
// call and saved by /api/brain/key, never shown again), the brain's USD
// caps, Codex's token prices, and Unlink.
let _model = null, _modelFull = {}, _modelPick = null, _modelBusy = false;
const MODEL_MONEY = ['usd_per_day', 'usd_per_month', 'codex_usd_per_mtok_in', 'codex_usd_per_mtok_out'];

async function loadModelCard(refresh) {
  const el = document.getElementById('model-card');
  if (!el) return;
  const s = await api('/api/brain' + (refresh ? '?refresh=1' : ''));
  _model = s && !s.error ? s : null;
  _modelPick = _model ? {provider: _model.provider, model: _model.model,
                         reasoning_effort: _model.reasoning_effort, auth: _model.auth, key: ''} : null;
  if (_modelPick) for (const k of MODEL_MONEY) _modelPick[k] = _model[k] == null ? '' : String(_model[k]);
  if (refresh) _modelFull = {};
  adoptCodexLink(_model);
  adoptClaudeLink(_model);
  renderModelCard();
  if (_model && _model.provider === 'codex') loadProviderStatus('codex', refresh);
}

// The status (and models) of a provider: Codex's with its live list
// (full=1); Claude's when it isn't the saved one (a switch not saved yet).
async function loadProviderStatus(provider, refresh) {
  if (_modelFull[provider] && !refresh) { renderModelCard(); return; }
  _modelFull[provider] = {loading: true};
  renderModelCard();
  const s = await api('/api/brain?provider=' + provider + (provider === 'codex' ? '&full=1' : '') +
                      (refresh ? '&refresh=1' : ''));
  _modelFull[provider] = s && !s.error ? s : {failed: true};
  adoptCodexLink(_modelFull[provider]);
  adoptClaudeLink(_modelFull[provider]);
  renderModelCard();
}

// An API key's line: set or not, and today's (this month's) spend beside
// the caps.
function modelSpendBits(s) {
  const m = s.spend;
  if (!m) return [];
  const usd = v => fmtUsd(v || 0, 2);
  const bits = [m.usd_per_day == null ? t('model.spend_today', {x: usd(m.today_usd)})
    : t('model.spend_today_of', {x: usd(m.today_usd), y: usd(m.usd_per_day)})];
  if (m.usd_per_month != null) bits.push(t('model.spend_month_of', {x: usd(m.month_usd), y: usd(m.usd_per_month)}));
  if (!m.priced) bits.push(t('model.no_price'));
  return bits;
}

function modelStatusLine(s) {
  if (!s) return '';
  const bits = [];
  const cli = s.cli || {};
  if (cli.version) bits.push(t('model.cli_version', {v: cli.version}));
  if (s.auth === 'api_key' && s.linked !== null && s.linked !== undefined) {
    bits.push(t(s.linked ? 'model.key_set' : 'model.key_none'));
    if (s.linked) bits.push(...modelSpendBits(s));
  } else if (s.linked === true) {
    const as = s.linked_as || {};
    bits.push(t('model.linked_as', {how: [as.method, as.plan].filter(Boolean).join(' · ') || '-'}));
    // (A Console account is billed per token: its spend beside the caps.)
    if (s.billed) bits.push(...modelSpendBits(s));
  } else if (s.linked === false) {
    bits.push(t(s.auth === 'account' ? 'model.not_linked' : 'model.not_logged_in'));
  } else if (cli.ok) {
    bits.push(t('model.unknown'));
  }
  if (s.last_error) bits.push(t('model.last_error', {kind: s.last_error}));
  const cls = s.linked === false || !cli.ok ? 'guess' : s.linked === true ? 'verified' : 'risky';
  return '<p class="small model-status"><span class="email-tag ' + cls + '">' +
    escHtml(t(s.linked === true && cli.ok ? 'model.ok' : cli.ok ? 'model.check' : 'model.cli_missing')) +
    '</span> ' + escHtml(bits.join(' · ')) + '</p>' +
    (s.problem ? '<p class="small muted">' + escHtml(s.problem) + '</p>' : '');
}

function renderModelCard() {
  const el = document.getElementById('model-card');
  if (!el) return;
  if (!_model) { el.innerHTML = '<p class="muted">' + escHtml(t('model.unreadable')) + '</p>'; return; }
  const p = _modelPick;
  const codex = p.provider === 'codex';
  const keySet = !!((_model.key_set || {})[p.provider]);
  // Codex's status is always its full one (its models); Claude's is the
  // saved one's, or its own read when the switch isn't saved yet.
  const full = codex || p.provider !== _model.provider ? _modelFull[p.provider] : null;
  const shown = full ? (!full.loading && !full.failed ? full : null) : _model;
  const models = (shown && shown.models) || [];
  const radio = (name, value, label, on, disabled, note) =>
    '<label class="check-line"><input type="radio" name="' + name + '" value="' + escHtml(value) + '"' +
      (on ? ' checked' : '') + (disabled ? ' disabled' : '') +
      ' onchange="pickModel(' + jsq(name) + ', this.value)"> <span>' + escHtml(label) + '</span>' +
      (note ? ' <span class="muted small">' + escHtml(note) + '</span>' : '') + '</label>';
  // (The ways the picked provider offers: Codex can link an account.)
  const ways = Array.from(new Set([...((shown && shown.offered) || _model.offered || ['terminal']),
                                   ...(p.provider === _model.provider ? [_model.auth] : [])]));
  const account = codex ? (((shown || (_model.provider === 'codex' ? _model : null)) || {}).codex_account || {}) : {};
  // Claude's account linked in Harvey (its kind, a link under way), and
  // whether the login picked is billed per token (held by the caps).
  const claudeShown = !codex ? (shown || (_model.provider !== 'codex' ? _model : null)) : null;
  const claudeAcc = (claudeShown && claudeShown.claude_account) || {};
  const billed = p.auth === 'api_key' || (!codex && p.auth === 'account' && claudeAcc.kind === 'console');
  const efforts = codex ? (((models.find(m => m.id === p.model) || {}).efforts || []).length
    ? models.find(m => m.id === p.model).efforts : (_model.efforts || [])) : [];
  let modelField = '<input class="form-input" id="model-name" list="model-options" autocomplete="off" value="' +
      escHtml(p.model || '') + '" placeholder="' + escHtml(t(codex ? 'model.codex_ph' : 'model.claude_ph')) +
      '" oninput="pickModel(\'model\', this.value)">' +
    '<datalist id="model-options">' + models.map(m => '<option value="' + escHtml(m.id) + '">' +
      escHtml(m.name && m.name !== m.id ? m.name : '') + '</option>').join('') + '</datalist>';
  let modelNote = '';
  if (codex) {
    if (full && full.loading) modelNote = t('model.codex_reading');
    else if (full && full.failed) modelNote = t('model.codex_unread');
    else if (shown && !models.length) modelNote = (shown.problem || t('model.codex_none'));
    else if (shown) modelNote = t('model.codex_list', {n: models.length});
  } else {
    modelNote = t('model.claude_list');
  }
  el.innerHTML =
    '<div class="settings-row"><div><h3>' + escHtml(t('model.provider')) + '</h3>' +
      '<p class="desc">' + escHtml(t('model.desc')) + '</p></div><div class="fields">' +
      radio('provider', 'claude', t('model.claude'), !codex, false, '') +
      radio('provider', 'codex', t('model.codex'), codex, !!_model.codex_refused,
            _model.codex_refused ? t('model.codex_refused') : '') +
      (_model.codex_refused ? '<p class="small muted">' + escHtml(_model.codex_refused) + '</p>' : '') +
    '</div></div>' +
    '<div class="settings-row"><div><h3>' + escHtml(t('model.model')) + '</h3>' +
      '<p class="desc">' + escHtml(modelNote) + '</p></div><div class="fields">' +
      '<div class="form-group"><label class="form-label" for="model-name">' + escHtml(t('model.model')) + '</label>' +
        modelField + '</div>' +
      (codex ? '<div class="form-group"><label class="form-label" for="model-effort">' + escHtml(t('model.effort')) +
        '</label><select class="form-input" id="model-effort" onchange="pickModel(\'reasoning_effort\', this.value)">' +
        '<option value="">' + escHtml(t('model.effort_default')) + '</option>' +
        efforts.map(e => '<option value="' + escHtml(e) + '"' + (p.reasoning_effort === e ? ' selected' : '') + '>' +
          escHtml(e) + '</option>').join('') + '</select></div>' : '') +
    '</div></div>' +
    '<div class="settings-row"><div><h3>' + escHtml(t('model.link')) + '</h3>' +
      '<p class="desc">' + escHtml(t(p.auth === 'api_key' ? (codex ? 'model.api_codex' : 'model.api_claude')
                                     : codex && p.auth === 'account' ? 'model.account_codex'
                                     : p.auth === 'account' ? 'model.account_claude'
                                     : codex ? 'model.terminal_codex' : 'model.terminal_claude')) + '</p></div>' +
      '<div class="fields">' +
      ways.map(w => radio('auth', w, t('model.auth.' + w), p.auth === w, false,
                          w === 'account' && _model.folder ? _model.folder : '')).join('') +
      (p.auth === 'api_key' ? modelKeyFields(p, codex, keySet) : '') +
      (codex && p.auth === 'account' ? codexAccountFields(account) : '') +
      (!codex && p.auth === 'account' ? claudeAccountFields(claudeAcc, claudeShown || {}) : '') +
      (billed && p.auth !== 'api_key' ? modelCapFields(p) : '') +
      modelStatusLine(shown || (p.provider === _model.provider ? _model : null)) +
      '<div class="btn-group"><button class="btn btn-primary btn-sm" id="model-save" onclick="saveModel()"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t('model.save')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="loadModelCard(true)">' + escHtml(t('model.recheck')) + '</button>' +
      (p.auth === 'api_key' && keySet ? '<button class="btn btn-ghost btn-sm" id="model-unlink" onclick="unlinkModelKey()"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t('model.unlink')) + '</button>' : '') +
      (codex && p.auth === 'account' && account.linked && !_codexLinkPending() ?
        '<button class="btn btn-ghost btn-sm" id="model-unlink-account" onclick="unlinkCodexAccount()"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t('model.unlink_account')) + '</button>' : '') +
      (!codex && p.auth === 'account' && claudeAcc.kind && !_claudeLinkActive() ?
        '<button class="btn btn-ghost btn-sm" id="model-unlink-account" onclick="unlinkClaudeAccount()"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t('model.unlink_account')) + '</button>' : '') + '</div>' +
    '</div></div>';
}

// Codex's account linked in Harvey (brain.auth: account): a ChatGPT
// device-code login. Link account starts it (/api/brain/codex/link): the
// page to open and the code to type there (the user's own to see), then a
// wait the panel polls until the server says linked, expired or failed.
// Linked: the plan, when Harvey last renewed the login, Unlink.
let _codexLink = null, _codexLinkTimer = null;

function _codexLinkPending() {
  return !!(_codexLink && _codexLink.status === 'pending');
}

// A link started before this page was opened (or in another tab): follow it.
function adoptCodexLink(s) {
  const link = s && s.codex_account && s.codex_account.link;
  if (link && link.status === 'pending' && !_codexLinkPending()) {
    _codexLink = link;
    followCodexLink();
  }
}

function codexAccountFields(account) {
  const link = _codexLink || (account.link && account.link.status === 'pending' ? account.link : null);
  if (link && link.status === 'pending') {
    const mins = Math.max(1, Math.ceil((link.expires_in || 0) / 60));
    return '<div class="codex-link" role="group" aria-label="' + escHtml(t('model.link_start')) + '">' +
      '<p class="small">' + escHtml(t('model.link_open')) + ' <a href="' + safeHref(link.url) +
        '" target="_blank" rel="noopener noreferrer">' + escHtml(link.url) + '</a></p>' +
      '<p class="small">' + escHtml(t('model.link_code')) + '</p>' +
      '<div class="codex-code"><code id="codex-link-code">' + escHtml(link.code) + '</code>' +
        '<button class="btn btn-secondary btn-sm" onclick="copyText(' + jsq(link.code) + ', this)">' +
        escHtml(t('model.link_copy')) + '</button></div>' +
      '<p class="small muted" aria-live="polite">' + escHtml(t('model.link_waiting', {n: mins})) + '</p>' +
      '<p class="small muted">' + escHtml(t('model.link_note')) + '</p>' +
      '<div class="btn-group"><button class="btn btn-ghost btn-sm" onclick="cancelCodexLink()">' +
        escHtml(t('model.link_cancel')) + '</button></div></div>';
  }
  let out = '';
  if (link && link.status === 'expired') out += '<p class="small muted">' + escHtml(t('model.link_expired')) + '</p>';
  else if (link && link.status === 'failed') out += '<p class="small muted">' + escHtml(t('model.link_failed', {kind: link.kind || '-'})) + '</p>';
  if (account.linked) {
    const bits = [t('model.linked_here')];
    if (account.renewed_at) bits.push(t('model.renewed', {when: new Date(account.renewed_at * 1000)
      .toLocaleString(intlLocale(), {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}));
    const last = account.last_try || {};
    if (last.ok === false && last.kind) bits.push(t('model.renew_failed', {kind: last.kind}));
    out += '<p class="small">' + escHtml(bits.join(' · ')) + '</p>';
    if (account.expired) out += '<p class="small"><span class="email-tag guess">' + escHtml(t('model.login_expired')) + '</span></p>';
  }
  if (!account.linked || account.expired) {
    out += '<p class="small muted">' + escHtml(t('model.link_note')) + '</p>' +
      '<div class="btn-group"><button class="btn btn-secondary btn-sm" id="model-link" onclick="startCodexLink()"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t(account.linked ? 'model.link_again' : 'model.link_start')) +
        '</button></div>';
  }
  return out;
}

async function startCodexLink() {
  if (_modelBusy || _codexLinkPending()) return;
  _modelBusy = true;
  try {
    const res = await api('/api/brain/codex/link', {method: 'POST'});
    if (!res || res.status !== 'pending') {
      showToast(failMsg(res, t('model.failed')), 'error');
      return;
    }
    _codexLink = res;
  } finally {
    _modelBusy = false;
  }
  renderModelCard();
  followCodexLink();
}

// Polls the link every few seconds until it ends (or the card is gone).
function followCodexLink() {
  clearTimeout(_codexLinkTimer);
  _codexLinkTimer = setTimeout(async () => {
    if (!document.getElementById('model-card') || !_codexLinkPending()) return;
    const res = await api('/api/brain/codex/link');
    if (!res || !res.status) { followCodexLink(); return; }
    _codexLink = res;
    if (res.status === 'pending') { renderModelCard(); followCodexLink(); return; }
    if (res.status === 'linked') {
      showToast(t('model.link_done'), 'success');
      _codexLink = null;
      if (_modelPick) _modelPick.auth = 'account';
      loadModelCard(true);
      return;
    }
    if (res.status === 'cancelled' || res.status === 'idle') _codexLink = null;
    renderModelCard();
  }, 3000);
}

async function cancelCodexLink() {
  clearTimeout(_codexLinkTimer);
  await api('/api/brain/codex/link', {method: 'DELETE'});
  _codexLink = null;
  showToast(t('model.link_cancelled'), 'success');
  renderModelCard();
}

async function unlinkCodexAccount() {
  if (_modelBusy) return;
  _modelBusy = true;
  try {
    const res = await api('/api/brain/codex/account', {method: 'DELETE'});
    if (res && res.success) showToast(t('model.account_unlinked'), 'success');
    else showToast(failMsg(res, t('model.failed')), 'error');
  } finally {
    _modelBusy = false;
  }
  loadModelCard(true);
}

// Claude's account linked in Harvey (brain.auth: account): the CLI's own
// login (claude auth login), relayed. Link account (a subscription) or Link
// a Console account starts it (/api/brain/claude/link): the sign-in page
// to open, then a box for the code that page gives, written to the CLI's
// prompt (/api/brain/claude/link/code). The panel polls until the server
// says linked, expired or failed; a code the CLI calls invalid is asked
// for again. Linked: which kind, "link again" once the CLI says it is
// logged out, Unlink.
let _claudeLink = null, _claudeLinkTimer = null;

function _claudeLinkActive() {
  return !!(_claudeLink && (_claudeLink.status === 'pending' || _claudeLink.status === 'waiting_code'));
}

// A link started before this page was opened (or in another tab): follow it.
function adoptClaudeLink(s) {
  const link = s && s.claude_account && s.claude_account.link;
  if (link && (link.status === 'pending' || link.status === 'waiting_code') && !_claudeLinkActive()) {
    _claudeLink = link;
    followClaudeLink();
  }
}

function claudeAccountFields(acc, s) {
  const saved = acc.link && (acc.link.status === 'pending' || acc.link.status === 'waiting_code') ? acc.link : null;
  const link = _claudeLink || saved;
  if (link && (link.status === 'pending' || link.status === 'waiting_code')) {
    const mins = Math.max(1, Math.ceil((link.expires_in || 0) / 60));
    const checking = link.status === 'pending';
    return '<div class="codex-link" role="group" aria-label="' + escHtml(t('model.link_start')) + '">' +
      (link.url ? '<p class="small">' + escHtml(t('model.claude_link_open')) + ' <a href="' + safeHref(link.url) +
        '" target="_blank" rel="noopener noreferrer">' + escHtml(t('model.claude_link_page')) + '</a></p>' : '') +
      '<div class="form-group"><label class="form-label" for="claude-link-code">' +
        escHtml(t('model.claude_link_code')) + '</label>' +
        '<div class="codex-code"><input class="form-input" id="claude-link-code" type="text" autocomplete="off"' +
        ' autocapitalize="off" spellcheck="false" maxlength="512"' + (checking ? ' disabled' : '') +
        ' onkeydown="if (event.key === \'Enter\') submitClaudeCode()">' +
        '<button class="btn btn-secondary btn-sm" id="claude-link-submit" onclick="submitClaudeCode()"' +
        (checking ? ' disabled' : '') + '>' + escHtml(t('model.claude_link_submit')) + '</button></div></div>' +
      (link.problem === 'invalid_code' ? '<p class="small"><span class="email-tag guess">' +
        escHtml(t('model.claude_code_invalid')) + '</span></p>' : '') +
      '<p class="small muted" aria-live="polite">' + escHtml(checking ? t('model.claude_checking')
        : t('model.claude_link_waiting', {n: mins})) + '</p>' +
      '<p class="small muted">' + escHtml(t('model.claude_link_note')) + '</p>' +
      '<div class="btn-group"><button class="btn btn-ghost btn-sm" onclick="cancelClaudeLink()">' +
        escHtml(t('model.link_cancel')) + '</button></div></div>';
  }
  let out = '';
  if (link && link.status === 'expired') out += '<p class="small muted">' + escHtml(t('model.claude_link_expired')) + '</p>';
  else if (link && link.status === 'failed') out += '<p class="small muted">' + escHtml(t('model.claude_link_failed', {kind: link.kind || '-'})) + '</p>';
  const ended = !!acc.kind && s.linked === false;
  if (acc.kind) {
    out += '<p class="small">' + escHtml(t(acc.kind === 'console' ? 'model.claude_linked_console' : 'model.claude_linked_here')) + '</p>';
    if (ended) out += '<p class="small"><span class="email-tag guess">' + escHtml(t('model.claude_login_ended')) + '</span></p>';
  }
  if (!acc.kind || ended) {
    out += '<p class="small muted">' + escHtml(t('model.claude_link_how')) + '</p>' +
      '<div class="btn-group"><button class="btn btn-secondary btn-sm" id="model-link" onclick="startClaudeLink(\'subscription\')"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t(acc.kind ? 'model.link_again' : 'model.link_start')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" id="model-link-console" onclick="startClaudeLink(\'console\')"' +
        (_modelBusy ? ' disabled' : '') + '>' + escHtml(t('model.claude_link_console')) + '</button></div>' +
      '<p class="small muted">' + escHtml(t('model.claude_console_note')) + '</p>';
  }
  return out;
}

async function startClaudeLink(kind) {
  if (_modelBusy || _claudeLinkActive()) return;
  _modelBusy = true;
  renderModelCard();
  try {
    const res = await api('/api/brain/claude/link', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({account: kind}),
    });
    if (!res || res.status !== 'waiting_code') {
      showToast(failMsg(res, t('model.failed')), 'error');
      return;
    }
    _claudeLink = res;
  } finally {
    _modelBusy = false;
    renderModelCard();
  }
  followClaudeLink();
}

// The pasted code goes to the CLI's prompt; the box is emptied either way.
async function submitClaudeCode() {
  const box = document.getElementById('claude-link-code');
  const code = box ? String(box.value || '').trim() : '';
  if (!code || !_claudeLinkActive()) return;
  if (box) box.value = '';
  const res = await api('/api/brain/claude/link/code', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: code}),
  });
  if (!res || !res.status) {
    showToast(failMsg(res, t('model.failed')), 'error');
    return;
  }
  _claudeLink = res;
  renderModelCard();
  followClaudeLink();
}

// Polls the link every few seconds until it ends (or the card is gone).
function followClaudeLink() {
  clearTimeout(_claudeLinkTimer);
  _claudeLinkTimer = setTimeout(async () => {
    if (!document.getElementById('model-card') || !_claudeLinkActive()) return;
    const res = await api('/api/brain/claude/link');
    if (!res || !res.status) { followClaudeLink(); return; }
    const was = _claudeLink;
    _claudeLink = res;
    if (res.status === 'pending' || res.status === 'waiting_code') {
      // (Redrawn only on a change, so a code being typed isn't wiped.)
      if (!was || was.status !== res.status || was.problem !== res.problem) renderModelCard();
      followClaudeLink();
      return;
    }
    if (res.status === 'linked') {
      showToast(t('model.claude_link_done'), 'success');
      _claudeLink = null;
      if (_modelPick) _modelPick.auth = 'account';
      loadModelCard(true);
      return;
    }
    if (res.status === 'cancelled' || res.status === 'idle') _claudeLink = null;
    renderModelCard();
  }, 3000);
}

async function cancelClaudeLink() {
  clearTimeout(_claudeLinkTimer);
  await api('/api/brain/claude/link', {method: 'DELETE'});
  _claudeLink = null;
  showToast(t('model.link_cancelled'), 'success');
  renderModelCard();
}

async function unlinkClaudeAccount() {
  if (_modelBusy) return;
  _modelBusy = true;
  try {
    const res = await api('/api/brain/claude/account', {method: 'DELETE'});
    if (res && res.success) showToast(t('model.account_unlinked'), 'success');
    else showToast(failMsg(res, t('model.failed')), 'error');
  } finally {
    _modelBusy = false;
  }
  loadModelCard(true);
}

// The brain's USD caps alone (a Console account, billed per token).
function modelCapFields(p) {
  const field = (id, key, label, ph) =>
    '<div class="form-group"><label class="form-label" for="' + id + '">' + escHtml(label) + '</label>' +
    '<input class="form-input" id="' + id + '" type="number" min="0" step="any" inputmode="decimal" value="' +
      escHtml(p[key] || '') + '" placeholder="' + escHtml(ph) + '" oninput="pickModel(' + jsq(key) + ', this.value)"></div>';
  return '<p class="small muted">' + escHtml(t('model.caps_console')) + ' ' + escHtml(t('model.caps_note')) + '</p>' +
    field('model-usd-day', 'usd_per_day', t('model.cap_day'), t('model.cap_ph')) +
    field('model-usd-month', 'usd_per_month', t('model.cap_month'), t('model.cap_ph'));
}

// The API key's box (a password field: what was saved is never shown), the
// brain's USD caps, and Codex's token prices (its CLI reports no cost).
function modelKeyFields(p, codex, keySet) {
  const field = (id, key, label, ph) =>
    '<div class="form-group"><label class="form-label" for="' + id + '">' + escHtml(label) + '</label>' +
    '<input class="form-input" id="' + id + '" type="number" min="0" step="any" inputmode="decimal" value="' +
      escHtml(p[key] || '') + '" placeholder="' + escHtml(ph) + '" oninput="pickModel(' + jsq(key) + ', this.value)"></div>';
  return '<div class="form-group"><label class="form-label" for="model-key">' + escHtml(t('model.key')) + '</label>' +
      '<input class="form-input" id="model-key" type="password" autocomplete="off" spellcheck="false" value="' +
        escHtml(p.key || '') + '" placeholder="' + escHtml(t(keySet ? 'model.key_ph_set' : 'model.key_ph')) +
        '" oninput="pickModel(\'key\', this.value)"></div>' +
    '<p class="small muted">' + escHtml(t('model.caps_note')) + '</p>' +
    field('model-usd-day', 'usd_per_day', t('model.cap_day'), t('model.cap_ph')) +
    field('model-usd-month', 'usd_per_month', t('model.cap_month'), t('model.cap_ph')) +
    (codex ? '<p class="small muted">' + escHtml(t('model.price_note')) + '</p>' +
      field('model-price-in', 'codex_usd_per_mtok_in', t('model.price_in'), t('model.price_ph')) +
      field('model-price-out', 'codex_usd_per_mtok_out', t('model.price_out'), t('model.price_ph')) : '');
}

function pickModel(key, value) {
  if (!_modelPick) return;
  if (key === 'provider' && value !== _modelPick.provider) {
    _modelPick.provider = value;
    // A model is one provider's: switching back gives the saved one again.
    _modelPick.model = value === _model.provider ? _model.model : '';
    _modelPick.reasoning_effort = value === _model.provider ? _model.reasoning_effort : '';
    if (value === 'codex' || value !== _model.provider) loadProviderStatus(value, false);
    renderModelCard();
    return;
  }
  _modelPick[key] = value;
  if (key === 'auth') renderModelCard();
}

async function saveModel() {
  if (!_modelPick || _modelBusy) return;
  const body = Object.assign({}, _modelPick);
  const key = String(body.key || '').trim();
  delete body.key;
  if (body.provider === 'codex' && !String(body.model || '').trim()) {
    showToast(t('model.codex_blank'), 'error');
    return;
  }
  if (body.provider === 'codex' && body.auth === 'account' && _codexLinkPending()) {
    showToast(t('model.link_first'), 'error');
    return;
  }
  const claudeAcc = ((_model.provider !== 'codex' ? _model : (_modelFull.claude || {})).claude_account) || {};
  if (body.provider !== 'codex' && body.auth === 'account' && (_claudeLinkActive() || !claudeAcc.kind)) {
    showToast(t('model.claude_link_first'), 'error');
    return;
  }
  const consoleBilled = body.provider !== 'codex' && body.auth === 'account' && claudeAcc.kind === 'console';
  if (consoleBilled && !String(body.usd_per_day ?? '').trim() && !String(body.usd_per_month ?? '').trim()) {
    showToast(t('model.cap_needed'), 'error');
    return;
  }
  if (body.auth === 'api_key' && !key && !((_model.key_set || {})[body.provider])) {
    showToast(t('model.key_needed'), 'error');
    return;
  }
  if (body.auth === 'api_key' && !String(body.usd_per_day ?? '').trim() && !String(body.usd_per_month ?? '').trim()) {
    showToast(t('model.cap_needed'), 'error');
    return;
  }
  _modelBusy = true;
  try {
    if (body.auth === 'api_key' && key) {
      // (Checked with a free call, then saved; the box is emptied either
      // way, and the key never comes back from the server.)
      const saved = await api('/api/brain/key', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: body.provider, key: key}),
      });
      _modelPick.key = '';
      const box = document.getElementById('model-key');
      if (box) box.value = '';
      if (!saved || !saved.success) {
        showToast(failMsg(saved, t('model.failed')), 'error');
        return;
      }
      showToast(t('model.key_saved'), 'success');
    }
    const res = await api('/api/brain', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      showToast(t('model.saved', {file: res.file}) + (res.loop_running ? ' ' + t('model.restart') : ''), 'success');
    } else {
      showToast(failMsg(res, t('model.failed')), 'error');
    }
  } finally {
    _modelBusy = false;
  }
  loadModelCard(true);
}

// Unlink: the saved key removed (Claude's from .env, Codex's from its own
// file), and a brain on it back to the terminal login.
async function unlinkModelKey() {
  if (!_modelPick || _modelBusy) return;
  _modelBusy = true;
  try {
    const res = await api('/api/brain/key?provider=' + encodeURIComponent(_modelPick.provider), {method: 'DELETE'});
    if (res && res.success) showToast(t('model.unlinked'), 'success');
    else showToast(failMsg(res, t('model.failed')), 'error');
  } finally {
    _modelBusy = false;
  }
  loadModelCard(true);
}

// Settings: the background's searches inside free allowances (on or off,
// and its share of each allowance).
async function loadFinderBgCard() {
  const el = document.getElementById('finder-bg-card');
  if (!el) return;
  const s = await api('/api/contacts/finder');
  const bg = (s && s.background_search) || {on: true, share: 0.5};
  el.innerHTML = '<div class="module-row"><div class="module-text">' +
      '<h3>' + escHtml(t('set.bg_title')) + '</h3>' +
      '<p class="desc">' + escHtml(t('set.bg_desc')) + '</p>' +
      '<label class="pd-setting">' + escHtml(t('set.bg_share')) +
        ' <input type="number" id="finder-bg-share" min="5" max="100" step="5" value="' +
        escHtml(String(Math.round((bg.share || 0.5) * 100))) + '" onchange="setFinderBg({background_share: Number(this.value) / 100})"> %</label>' +
    '</div><label class="switch"><input type="checkbox" id="finder-bg-on"' + (bg.on ? ' checked' : '') +
      ' onchange="setFinderBg({background_search: this.checked})">' +
      '<span class="track" aria-hidden="true"><span class="thumb"></span></span>' +
      '<span class="switch-label">' + escHtml(t(bg.on ? 'mod.on' : 'mod.off')) + '</span></label></div>';
}

// Settings → Channels: nPathi Chat's SMS and WhatsApp, reading and answering.
// The key is a password field, checked by nPathi's account call before it
// is saved, and never sent back: the card only knows whether one is saved.
function unixRel(ts) { return ts ? relTime(new Date(ts * 1000).toISOString()) : escHtml(t('ch.never')); }

// A read gap's window as the page says it ({from, to}: local times).
function gapWindow(since, until) {
  const at = ts => ts ? formatDate(new Date(ts * 1000).toISOString()) : '?';
  return {from: at(since), to: at(until)};
}

// One channel's sending controls: the switch, the sender answers go from
// (only one a reply can come back to), the daily cap and its own pause.
function channelSendHtml(ch, sentToday) {
  const ok = (ch.senders || []).filter(x => x.receives === true);
  const options = '<option value="">' + escHtml(t('ch.route_none')) + '</option>' + ok.map(x =>
    '<option value="' + escHtml(x.key) + '"' + (x.key === ch.route_chosen ? ' selected' : '') + '>' +
      escHtml(x.name || x.key) + '</option>').join('');
  const notes = [];
  if (ch.can_send === false) notes.push(t('ch.cant_send'));
  if (!ch.route) notes.push(t('ch.route_needed'));
  return '<div class="ch-send">' +
    '<label class="switch"><input type="checkbox"' + (ch.send ? ' checked' : '') + (ch.route ? '' : ' disabled') +
      ' onchange="setChannel(' + jsq(ch.id) + ', {send: this.checked})">' +
      '<span class="track" aria-hidden="true"><span class="thumb"></span></span>' +
      '<span class="switch-label">' + escHtml(t('ch.send')) + '</span></label>' +
    '<p class="muted small">' + escHtml((ch.window_hours || 0) >= 48
      ? t('ch.send_hint_days', {days: fmtN(Math.round(ch.window_hours / 24))})
      : t('ch.send_hint', {hours: fmtN(ch.window_hours || 0)})) + '</p>' +
    '<div class="form-group"><label class="form-label" for="ch-route-' + escHtml(ch.id) + '">' + escHtml(t('ch.route')) + '</label>' +
      '<select class="form-input" id="ch-route-' + escHtml(ch.id) + '" onchange="setChannel(' + jsq(ch.id) + ', {route: this.value})">' +
      options + '</select></div>' +
    '<div class="form-group"><label class="form-label" for="ch-cap-' + escHtml(ch.id) + '">' + escHtml(t('ch.cap')) + '</label>' +
      '<input type="number" min="0" max="500" class="form-input" id="ch-cap-' + escHtml(ch.id) + '" value="' + escHtml(String(ch.daily_cap || 0)) +
      '" onchange="setChannel(' + jsq(ch.id) + ', {daily_cap: Math.max(0, Math.round(Number(this.value) || 0))})"></div>' +
    '<p class="muted small">' + escHtml(t('ch.sent_today', {n: fmtN(sentToday || 0), cap: fmtN(ch.daily_cap || 0)})) + '</p>' +
    '<label class="small"><input type="checkbox"' + (ch.paused ? ' checked' : '') +
      ' onchange="setChannel(' + jsq(ch.id) + ', {paused: this.checked})"> ' + escHtml(t('ch.pause')) + '</label>' +
    notes.map(n => '<p class="small" style="color:var(--bad)">' + escHtml(n) + '</p>').join('') +
  '</div>';
}

// The sending hours, the credits caps and what was spent.
function channelSpendHtml(s) {
  const sp = s.spend || {};
  const num = v => v === null || v === undefined ? '' : String(v);
  const field = (id, label, value, type, extra) => '<div class="form-group"><label class="form-label" for="' + id + '">' +
    escHtml(label) + '</label><input class="form-input" id="' + id + '" type="' + type + '" value="' + escHtml(value) + '"' + (extra || '') + '></div>';
  const usd = sp.usd_today !== null && sp.usd_today !== undefined
    ? t('ch.spend_usd', {today: fmtN(sp.usd_today), month: fmtN(sp.usd_month)}) : '';
  const notes = [];
  if (s.credits_out) notes.push(t('ch.credits_out'));
  if ((s.unpriced_cap || []).length) notes.push(t('ch.unpriced_cap', {channels: s.unpriced_cap.map(channelName).join(', ')}));
  if ((s.optout_sync || {}).problem_kind === 'forbidden') notes.push(t('ch.sync_forbidden'));
  if (s.still_listed) notes.push(t('ch.still_listed', {n: s.still_listed}));
  return '<div class="settings-row"><div><h3>' + escHtml(t('ch.sending_title')) + '</h3>' +
      '<p class="muted small">' + escHtml(t('ch.spend', {today: fmtN(sp.credits_today || 0), month: fmtN(sp.credits_month || 0)})) + escHtml(usd) + '</p>' +
      (sp.unpriced ? '<p class="muted small">' + escHtml(t('ch.unpriced', {n: sp.unpriced})) + '</p>' : '') +
      notes.map(n => '<p class="small" style="color:var(--bad)">' + escHtml(n) + '</p>').join('') +
      '<p class="muted small">' + escHtml(t('ch.hours_hint')) + '</p></div>' +
    '<div class="fields">' +
      field('ch-start', t('ch.hours_start'), (s.send_hours || {}).start || '09:00', 'time') +
      field('ch-end', t('ch.hours_end'), (s.send_hours || {}).end || '20:00', 'time') +
      field('ch-cday', t('ch.credits_day'), num(sp.credits_per_day), 'number', ' min="0" step="any" placeholder="' + escHtml(t('ch.blank_none')) + '"') +
      field('ch-cmonth', t('ch.credits_month'), num(sp.credits_per_month), 'number', ' min="0" step="any" placeholder="' + escHtml(t('ch.blank_none')) + '"') +
      field('ch-usd', t('ch.usd_per_credit'), num(sp.usd_per_credit), 'number', ' min="0" step="any"') +
      '<div class="btn-group"><button class="btn btn-secondary btn-sm" onclick="saveChannelSettings()">' + escHtml(t('ch.save')) + '</button></div>' +
    '</div></div>';
}

async function setChannel(channel, change) {
  const res = await api('/api/channels/' + encodeURIComponent(channel), {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(change),
  });
  if (!(res && res.success)) showToast(failMsg(res, t('ch.connect_failed')), 'error');
  loadChannelsCard();
}

async function saveChannelSettings() {
  const v = id => String((document.getElementById(id) || {}).value || '').trim();
  const n = id => v(id) === '' ? null : Number(v(id));
  const res = await api('/api/channels/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({send_start: v('ch-start'), send_end: v('ch-end'), credits_per_day: n('ch-cday'),
                          credits_per_month: n('ch-cmonth'), usd_per_credit: n('ch-usd')}),
  });
  showToast(res && res.success ? t('ch.saved') : failMsg(res, t('ch.connect_failed')),
            res && res.success ? 'success' : 'error');
  loadChannelsCard();
}

async function loadChannelsCard() {
  const el = document.getElementById('channels-card');
  if (!el) return;
  const s = await api('/api/channels');
  if (!s) { el.innerHTML = '<p class="muted">' + escHtml(t('ch.unavailable')) + '</p>'; return; }
  const intro = '<p class="desc">' + escHtml(t('ch.desc')) + '</p>';
  if (!s.module_on) {
    el.innerHTML = '<div class="settings-row"><div><h3>nPathi Chat</h3>' + intro +
      '<p class="muted small">' + escHtml(t('ch.module_off')) + '</p></div></div>';
    return;
  }
  const a = s.account || {};
  const state = s.connected ? '<span class="email-tag verified">' + escHtml(t('ch.connected')) + '</span>'
    : '<span class="email-tag guess">' + escHtml(t('ch.not_connected')) + '</span>';
  const acct = a.ok
    ? '<p class="muted small">' + escHtml(t('ch.account', {workspace: a.workspace || '?', plan: a.plan || '?'})) +
      (typeof a.credits === 'number' ? ' · ' + escHtml(t('ch.credits', {n: fmtN(a.credits)})) : '') +
      ' · ' + escHtml(t('ch.checked')) + ' ' + unixRel(a.checked_at) + '</p>'
    : (a.error ? '<p class="small" style="color:var(--bad)">' + escHtml(a.error) + '</p>' : '');
  const rows = (s.channels || []).map(ch => {
    const senders = (ch.senders || []).map(x => '<li>' + escHtml(x.name || t('ch.default_route')) +
      ' <span class="muted small">' + escHtml(x.receives === true ? t('ch.receives') : x.receives === false
        ? t('ch.no_receive') : t('ch.receive_unknown')) + '</span></li>').join('');
    const notes = [];
    if (ch.can_read === false) notes.push(t('ch.cant_read'));
    if (ch.gap) notes.push(t('ch.gap_window', gapWindow(ch.gap.since, ch.gap.covered_from)));
    if (ch.problem) notes.push(t('ch.problem', {why: ch.problem}));
    return '<div class="module-row"><div class="module-text">' +
      '<h3>' + escHtml(ch.label) + '</h3>' +
      '<p class="muted small">' + escHtml(t('ch.last_read')) + ' ' + unixRel(ch.last_read_at) + '</p>' +
      (senders ? '<ul class="small">' + senders + '</ul>' : '<p class="muted small">' + escHtml(t('ch.no_senders')) + '</p>') +
      notes.map(n => '<p class="small" style="color:var(--bad)">' + escHtml(n) + '</p>').join('') +
      (s.connected ? channelSendHtml(ch, ((s.spend || {}).sent_today || {})[ch.id]) : '') +
      '</div><label class="switch"><input type="checkbox"' + (ch.receive ? ' checked' : '') +
      ' onchange="setChannelReceive(' + jsq(ch.id) + ', this.checked)">' +
      '<span class="track" aria-hidden="true"><span class="thumb"></span></span>' +
      '<span class="switch-label">' + escHtml(t(ch.receive ? 'ch.receiving' : 'mod.off')) + '</span></label></div>';
  }).join('');
  el.innerHTML = '<div class="settings-row"><div><h3>nPathi Chat ' + state + '</h3>' + intro + acct + '</div>' +
    '<div class="fields">' +
      '<div class="form-group"><label class="form-label" for="npathi-url">' + escHtml(t('ch.url')) + '</label>' +
        '<input type="url" class="form-input" id="npathi-url" placeholder="https://chat.example.com" autocomplete="off" value="' +
        escHtml(s.base_url || '') + '"></div>' +
      '<div class="form-group"><label class="form-label" for="npathi-key">' + escHtml(t('ch.key')) + '</label>' +
        '<div class="with-btn"><input type="password" class="form-input" id="npathi-key" autocomplete="off" placeholder="' +
        escHtml(s.key_set ? t('set.saved_ph') : t('ch.key_ph')) + '">' +
        '<button class="btn btn-secondary" onclick="toggleVisibility(\'npathi-key\')">' + escHtml(t('common.show')) + '</button></div></div>' +
      '<div class="btn-group"><button class="btn btn-primary btn-sm" onclick="connectChannels()">' + escHtml(t('ch.connect')) + '</button>' +
        (s.key_set && s.base_url ? '<button class="btn btn-secondary btn-sm" onclick="testChannels()">' + escHtml(t('ch.test')) + '</button>' : '') +
      '</div></div></div>' + rows + (s.connected ? channelSpendHtml(s) : '');
}

async function connectChannels() {
  const url = String((document.getElementById('npathi-url') || {}).value || '').trim();
  const key = String((document.getElementById('npathi-key') || {}).value || '').trim();
  const res = await api('/api/channels/connect', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(key ? {base_url: url, key: key} : {base_url: url}),
  });
  showToast(res && res.success ? t('ch.connected_ok') : failMsg(res, t('ch.connect_failed')),
            res && res.success ? 'success' : 'error');
  loadChannelsCard();
}

async function testChannels() {
  const res = await api('/api/channels/test', {method: 'POST'});
  showToast(res && res.success ? t('ch.test_ok') : failMsg(res, t('ch.connect_failed')),
            res && res.success ? 'success' : 'error');
  loadChannelsCard();
}

async function setChannelReceive(channel, on) {
  const res = await api('/api/channels/' + encodeURIComponent(channel), {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({receive: !!on}),
  });
  if (!(res && res.success)) showToast(failMsg(res, t('ch.connect_failed')), 'error');
  loadChannelsCard();
}

// Settings: what one request to each verification service (and Hunter's
// pattern lookup) costs past its free allowance on the user's plan. Blank:
// no price, and a run with a USD cap refuses those requests.
async function loadVerifyPrices() {
  const el = document.getElementById('verify-prices');
  if (!el) return;
  const s = await api('/api/verify/prices');
  if (!s || !s.services) { el.innerHTML = ''; return; }
  const c = s.checks || {};
  el.innerHTML = '<p class="desc">' + escHtml(t('set.vp_desc', {used: fmtN(c.used || 0), cap: fmtN(c.cap || 0)})) + '</p>' +
    s.services.map(v => '<div class="form-group"><label class="form-label" for="vp-' + escHtml(v.id) + '">' +
      escHtml(t(v.kind === 'search' ? 'set.vp_search' : 'set.vp_check', {name: v.id})) + '</label>' +
      '<input class="form-input" id="vp-' + escHtml(v.id) + '" type="number" min="0" max="10" step="0.0001" placeholder="' +
        escHtml(t('set.vp_blank')) + '" value="' + (v.entered === null || v.entered === undefined ? '' : escHtml(String(v.entered))) + '">' +
      '<div class="muted small">' + escHtml(t('set.vp_used', {free: fmtN(v.free_per_month), used: fmtN(v.used), past: fmtN(v.past_free)})) +
        (v.past_free && v.usd === null ? ' ' + escHtml(t('set.vp_unpriced')) : '') + '</div></div>').join('') +
    '<button class="btn btn-primary btn-sm" onclick="saveVerifyPrices()">' + escHtml(t('set.vp_save')) + '</button>';
  el.dataset.ids = s.services.map(v => v.id).join(',');
}

async function saveVerifyPrices() {
  const el = document.getElementById('verify-prices');
  if (!el || !el.dataset.ids) return;
  const body = {};
  el.dataset.ids.split(',').forEach(id => {
    const v = String((document.getElementById('vp-' + id) || {}).value || '').trim();
    body[id] = v === '' ? null : Number(v);
  });
  const res = await api('/api/verify/prices', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  showToast(res && res.success ? t('set.vp_saved') : failMsg(res, t('set.vp_failed')),
            res && res.success ? 'success' : 'error');
  loadVerifyPrices();
}

async function setFinderBg(change) {
  const res = await api('/api/contacts/finder/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(change),
  });
  showToast(res && res.success ? t('set.bg_saved') : failMsg(res, t('set.bg_failed')),
            res && res.success ? 'success' : 'error');
  loadFinderBgCard();
}

// Settings: the classifier (a small language model) that says which
// search results and which read sites are a firm's own, and which contacts
// on them are; and the check of the websites found before it. Each task is
// set to a profile (one complete setup: endpoint, model, the .env key's
// name, prices, the questions' texts) or off. The built-in profiles carry
// what Harvey's judge test measured on them, shown side by side, none put
// first. The key is named, never shown.
let _classify = null;       // /api/classify: the caps, the spend, what waits
let _clsProfiles = null;    // /api/classify/profiles
let _clsList = [];          // the profiles shown, sorted by name
let _clsEdit = null;        // the profile being edited: {id, builtin, d}

const CLS_TASKS = ['results', 'site', 'contacts'];
// The questions a decision model's tasks hold (ids and option keys are
// fixed by the server; only texts and bars change).
const CLS_SITE_QUESTIONS = ['names', 'own', 'directory', 'institution'];

// A classifier amount: small ones with four decimals.
function clsUsd(n) {
  if (n === null || n === undefined) return '–';
  return fmtUsd(n, Math.abs(Number(n)) >= 1 ? 2 : 4);
}

// What a task is for, when it is asked and what off means: the server's
// words, in the viewer's language while the English matches.
// A task's name ("Website"), with or without the server's words.
function clsTaskName(task) {
  return clsTaskText(task, 'label');
}

function clsTaskText(task, field) {
  const doc = (((_clsProfiles || {}).tasks || {})[task]) || {};
  return doc[field] ? tServer('cls.taskdoc.' + task + '.' + field, doc[field]) : t('cls.taskdoc.' + task + '.' + field);
}

// Its calls and spend today and this month, cost per site, tokens a call.
function classifyMetricsHtml(m) {
  if (!m || !m.today || !(m.month && m.month.calls)) return '<p class="desc" id="cls-metrics">' + escHtml(t('cls.metrics_none')) + '</p>';
  const tasks = Object.keys(m.per_task || {}).sort().map(k => {
    const v = m.per_task[k];
    return escHtml(t('cls.metrics.task', {task: clsTaskName(k), n: fmtN(v.calls || 0), usd: clsUsd(v.usd),
      tok: v.avg_tokens === null || v.avg_tokens === undefined ? '–' : fmtN(v.avg_tokens)}));
  }).join(' &middot; ');
  return '<p class="desc" id="cls-metrics">' + escHtml(t('cls.metrics', {
      today: fmtN(m.today.calls || 0), today_usd: clsUsd(m.today.usd), month: fmtN(m.month.calls || 0),
      month_usd: clsUsd(m.month.usd), site: clsUsd(m.cost_per_site),
      tok: m.month.avg_tokens === null || m.month.avg_tokens === undefined ? '–' : fmtN(m.month.avg_tokens)})) +
    (m.month.unpriced ? ' ' + escHtml(t('cls.cost_unknown')) : '') + '</p>' +
    (tasks ? '<p class="hint">' + tasks + '</p>' : '');
}

async function loadClassifyCard() {
  const el = document.getElementById('classify-card');
  if (!el) return;
  const [c, p] = await Promise.all([api('/api/classify'), api('/api/classify/profiles')]);
  if (!c || c.error) { el.innerHTML = '<p class="muted small">' + escHtml(failMsg(c, t('cls.failed'))) + '</p>'; return; }
  _classify = c;
  _clsProfiles = p && !p.error ? p : {profiles: [], use: c.use || {}, tasks: {}, hidden: []};
  const all = _clsProfiles.profiles || [];
  _clsList = all.filter(x => !x.hidden).sort((a, b) => String(a.name).localeCompare(String(b.name), intlLocale()));
  const hidden = all.filter(x => x.hidden).sort((a, b) => String(a.name).localeCompare(String(b.name), intlLocale()));
  el.innerHTML = clsAboutHtml() + clsTasksHtml() + clsBudgetHtml(c) +
    '<div class="settings-row"><div><h3>' + escHtml(t('cls.profiles')) + '</h3>' +
      '<p class="desc">' + escHtml(t('cls.profiles_desc')) + '</p>' +
      (_clsProfiles.about ? '<p class="hint">' + escHtml(tServer('cls.about_builtin', _clsProfiles.about)) + '</p>' : '') +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="newClsProfile()">' + escHtml(t('cls.add')) + '</button></div></div>' +
    '<div class="fields cls-wide"><div id="cls-editor">' + (_clsEdit ? clsEditorHtml() : '') + '</div>' +
      '<div class="cls-profiles" id="cls-profiles">' + (_clsList.length ? _clsList.map(clsProfileHtml).join('')
        : '<p class="muted small">' + escHtml(t('cls.no_profiles')) + '</p>') + '</div>' +
      (hidden.length ? '<details class="more-fields" id="cls-hidden"><summary>' + escHtml(t('cls.hidden_n', {n: hidden.length})) +
        '</summary><ul class="small">' + hidden.map(h => '<li>' + escHtml(h.name) + ' <button class="btn btn-ghost btn-sm" onclick="clsProfileAction(' +
          jsq(h.id) + ', \'restore\')">' + escHtml(t('cls.restore')) + '</button></li>').join('') + '</ul></details>' : '') +
    '</div></div>' +
    '<div class="settings-row"><div><h3>' + escHtml(t('rejudge.title')) + '</h3>' +
      '<p class="desc">' + escHtml(t('rejudge.desc')) + '</p></div>' +
    '<div class="fields" id="rejudge-box"><p class="muted small">' + escHtml(t(c.waiting ? 'rejudge.counting' : 'rejudge.none')) + '</p></div></div>';
  if (c.waiting) loadRejudgeCount();
}

// What the classifier is, when it is asked, and its rules, in plain words.
function clsAboutHtml() {
  const li = (k) => '<li>' + escHtml(t(k)) + '</li>';
  return '<div class="settings-row" id="cls-about"><div><h3>' + escHtml(t('cls.title')) + '</h3>' +
      '<p class="desc">' + escHtml(t('cls.about.what')) + '</p></div>' +
    '<div class="fields cls-wide"><p class="form-label">' + escHtml(t('cls.about.when')) + '</p><ul class="cls-list small">' +
      li('cls.about.when.find') + li('cls.about.when.again') + li('cls.about.when.check') + li('cls.about.when.verify') + '</ul>' +
      '<p class="form-label">' + escHtml(t('cls.about.rules')) + '</p><ul class="cls-list small">' +
      li('cls.about.rule.register') + li('cls.about.rule.likely') + li('cls.about.rule.off') + li('cls.about.rule.caps') +
      li('cls.about.rule.brain') + '</ul></div></div>';
}

// Each task: what it is for, when it is asked, what off means, and the
// profile that answers it (or off), changeable here.
function clsTasksHtml() {
  const use = (_clsProfiles && _clsProfiles.use) || {};
  const rows = CLS_TASKS.map(task => {
    const can = _clsList.filter(p => (p.tasks || []).includes(task));
    const current = use[task] || 'off';
    const opts = '<option value="off"' + (current === 'off' ? ' selected' : '') + '>' + escHtml(t('cls.use_off')) + '</option>' +
      can.map(p => '<option value="' + escHtml(p.id) + '"' + (p.id === current ? ' selected' : '') + '>' + escHtml(p.name) + '</option>').join('');
    return '<div class="cls-task" id="cls-task-' + task + '"><div class="cls-task-text"><h4>' + escHtml(clsTaskText(task, 'label')) + '</h4>' +
        '<p class="small">' + escHtml(clsTaskText(task, 'what')) + '</p>' +
        '<p class="muted small">' + escHtml(clsTaskText(task, 'when')) + '</p>' +
        '<p class="muted small">' + escHtml(t('cls.if_off', {text: clsTaskText(task, 'off')})) + '</p></div>' +
      '<div class="form-group"><label class="form-label" for="cls-use-' + task + '">' + escHtml(t('cls.answered_by')) + '</label>' +
        '<select class="form-input" id="cls-use-' + task + '" onchange="setClassifyUse(\'' + task + '\', this.value)">' + opts + '</select></div></div>';
  }).join('');
  return '<div class="settings-row" id="cls-tasks"><div><h3>' + escHtml(t('cls.tasks')) + '</h3>' +
      '<p class="desc">' + escHtml(t('cls.tasks_desc')) + '</p></div><div class="fields cls-wide">' + rows + '</div></div>';
}

async function setClassifyUse(task, id) {
  const res = await api('/api/classify/use', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({[task]: id})});
  showToast(res && res.success ? t('cls.use_saved', {task: clsTaskText(task, 'label')}) : failMsg(res, t('cls.failed')),
            res && res.success ? 'success' : 'error');
  loadClassifyCard();
}

// The install's caps (calls a day, USD a day and a month), today's and this
// month's spend, and the DeepInfra key's scopes.
function clsBudgetHtml(c) {
  const field = (id, label, value, extra) => '<div class="form-group"><label class="form-label" for="cls-' + id + '">' +
    escHtml(label) + '</label><input class="form-input" id="cls-' + id + '" type="number" value="' +
    escHtml(String(value === undefined || value === null ? '' : value)) + '"' + (extra || '') + '></div>';
  const perTask = CLS_TASKS.map(k => t('cls.task_cap', {task: clsTaskName(k), n: fmtN(((c.tasks || {})[k] || {}).per_day || 0)})).join(' · ');
  return '<div class="settings-row" id="cls-budget-row"><div><h3>' + escHtml(t('cls.budget')) + '</h3>' +
      '<p class="desc">' + escHtml(t('cls.budget_desc')) + '</p></div>' +
    '<div class="fields">' +
      '<p class="desc">' + escHtml(t('cls.calls_today', {n: fmtN(c.calls_today || 0), of: fmtN(c.per_day || 0)})) + '</p>' +
      Object.keys(c.held || {}).map(id => '<p class="hint warn cls-held">' +
        escHtml(t('cls.held_named', {name: c.held[id].name || id, text: clsHeldText(c.held[id])})) + '</p>').join('') +
      (c.budget_reached ? '<p class="hint warn" id="cls-budget">' + escHtml(t('cls.budget_reached')) + '</p>' : '') +
      classifyMetricsHtml(c.metrics) +
      '<div class="form-row">' + field('per_day', t('cls.per_day'), c.per_day, ' min="0"') +
        field('usd_day', t('cls.usd_per_day'), c.usd_per_day, ' min="0" step="any"') +
        field('usd_month', t('cls.usd_per_month'), c.usd_per_month, ' min="0" step="any"') + '</div>' +
      '<p class="hint">' + escHtml(t('cls.usd_hint')) + '</p>' +
      '<p class="hint">' + escHtml(perTask) + '</p>' +
      '<p class="hint">' + escHtml(t('cls.run_caps')) + '</p>' +
      '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="saveClassifyCaps()">' + escHtml(t('cls.save')) + '</button></div>' +
      '<p class="hint" id="cls-scopes">' + escHtml(t('cls.scopes')) + '</p>' +
    '</div></div>';
}

async function saveClassifyCaps() {
  const v = (id) => String((document.getElementById('cls-' + id) || {}).value || '').trim();
  // (Blank USD cap: none; the call caps stay.)
  const body = {per_day: Number(v('per_day') || 0), usd_per_day: v('usd_day'), usd_per_month: v('usd_month')};
  const res = await api('/api/classify/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  showToast(res && res.success ? t('cls.saved') : failMsg(res, t('cls.failed')), res && res.success ? 'success' : 'error');
  loadClassifyCard();
}

// ── One profile, as a card ──

// A profile's pause, asked for by its server: its own (a rate limit, an
// unknown model) or its account's (a refused key, no credit: every profile
// on the same endpoint and key waits). Each profile keeps its own pace.
function clsHeldText(h) {
  const secs = Number((h && h.seconds) || 0);
  const until = new Date(Date.now() + secs * 1000).toLocaleTimeString(intlLocale(), {hour: '2-digit', minute: '2-digit'});
  return t(h.scope === 'account' ? 'cls.held_account' : 'cls.held_profile', {min: Math.max(1, Math.ceil(secs / 60)), until: until}) +
    (h.why ? ' (' + h.why + ')' : '');
}

function clsKeyText(p) {
  if (p.kind === 'brain') return t('cls.key_none_brain');
  if (!p.key_name) return t('cls.key_none');
  return t(p.key_set ? 'cls.key_set' : 'cls.key_missing', {name: p.key_name});
}

function clsPriceText(p) {
  const pr = p.prices || {};
  if (p.kind === 'brain') return t('cls.price_brain');
  if (pr.from_list) return t('cls.price_list');
  if (pr.in_per_m !== null && pr.in_per_m !== undefined && pr.out_per_m !== null && pr.out_per_m !== undefined) {
    return t('cls.price_entered', {pin: clsUsd(pr.in_per_m), pout: clsUsd(pr.out_per_m)});
  }
  return t('cls.price_none');
}

// DeepInfra's price list and account box: the server's rule (a saved
// profile's is_deepinfra; a draft's base URL on the host the server names,
// exactly).
function clsIsDeepInfra(p) {
  if (typeof p.is_deepinfra === 'boolean') return p.is_deepinfra;
  const host = (_clsProfiles && _clsProfiles.deepinfra_host) || '';
  if (!host) return false;
  try { return new URL(p.base_url).hostname.toLowerCase() === host.toLowerCase(); } catch { return false; }
}

// A test's counts in words: the built-in's measured ones or a run of yours.
function clsCountsHtml(r) {
  if (!r) return '';
  const n = (v) => v === null || v === undefined ? '–' : fmtN(v);
  const parts = [];
  const right = r.site_right !== undefined ? r.site_right : r.correct;
  if (right !== undefined && r.scored !== undefined) {
    parts.push(t(r.task === 'results' ? 'cls.m.results' : 'cls.m.site', {right: n(right), scored: n(r.scored),
      fl: n(r.false_links), miss: n(r.misses)}));
  }
  if (r.contacts_expected) {
    parts.push(t('cls.m.contacts', {right: n(r.contacts_right), picked: n(r.contacts_picked), expected: n(r.contacts_expected)}));
  }
  if (r.median_ms !== null && r.median_ms !== undefined) {
    parts.push(t(r.p90_ms !== null && r.p90_ms !== undefined ? 'cls.m.time' : 'cls.m.time_median',
                 {med: n(r.median_ms), p90: n(r.p90_ms)}));
  }
  if (r.cost_usd !== null && r.cost_usd !== undefined) {
    parts.push(r.cost_per_1000_sites !== undefined
      ? t('cls.m.cost', {usd: clsUsd(r.cost_usd), per: clsUsd(r.cost_per_1000_sites)})
      : t(r.cost_known === false ? 'cls.m.cost_unknown' : 'cls.m.cost_only', {usd: clsUsd(r.cost_usd)}));
  }
  return escHtml(parts.join(' · '));
}

function clsTestSet(name) {
  if (name === 'built-in') return t('cls.set.builtin');
  if (name === 'your file') return t('cls.set.file');
  return tServer('cls.set.developers', name || '');
}

function clsResultsHtml(p) {
  const m = p.measured;
  let html = '';
  if (m) {
    html += '<p class="small cls-counts">' + clsCountsHtml(m) + '</p>' +
      '<p class="muted small">' + escHtml(t('cls.m.when', {date: m.measured_at || '', set: clsTestSet(m.test_set),
        cases: fmtN(m.cases || 0), asked: fmtN(m.asked || 0), scored: fmtN(m.scored || 0)})) + '</p>';
  }
  const runs = p.tests || [];
  if (!m && !p.tested) {
    html += '<p class="muted small cls-untested">' + escHtml(t(runs.length ? 'cls.not_tested_changed' : 'cls.not_tested')) + '</p>';
  }
  if (runs.length) {
    html += '<details class="cls-runs"><summary class="small">' + escHtml(t('cls.tests_yours', {n: runs.length})) + '</summary><ul class="small">' +
      runs.map(r => '<li>' + escHtml(t('cls.t.line', {task: clsTaskName((r.task || 'site')),
        when: r.at ? new Date(r.at * 1000).toLocaleDateString(intlLocale()) : '', set: clsTestSet(r.test_set)})) + ' ' +
        clsCountsHtml(r) + (r.stopped ? ' · ' + escHtml(t('cls.t.stopped', {why: r.stopped})) : '') + '</li>').join('') + '</ul></details>';
  }
  return html;
}

function clsProfileHtml(p) {
  const line = (label, value) => '<div class="cls-line"><span class="muted">' + escHtml(label) + '</span> ' + value + '</div>';
  const used = (p.used_for || []).map(k => clsTaskName(k));
  const free = (p.tasks || []).filter(k => !(p.used_for || []).includes(k));
  const extra = p.request_extra && Object.keys(p.request_extra).length ? JSON.stringify(p.request_extra) : '';
  const note = p.notes ? (p.builtin ? tServer('cls.note.' + p.id, p.notes) : p.notes) : '';
  const keyNote = p.key_name === 'DEEPINFRA_API_KEY' || clsIsDeepInfra(p) ? t('cls.scopes_short')
    : (p.key_notes && p.key_notes.note ? tServer('cls.keynote.' + p.key_name, p.key_notes.note) : '');
  const testable = (p.tasks || []).some(k => k === 'site' || k === 'results');
  const box = 'clsp-' + p.id;
  return '<div class="cls-profile" id="' + escHtml(box) + '"><div class="cls-profile-head"><h4>' + escHtml(p.name) + '</h4>' +
      '<span class="muted small">' + escHtml(t(p.builtin ? 'cls.builtin' : (p.based_on ? 'cls.yours_based' : 'cls.yours'),
        {name: p.based_on || ''})) + '</span></div>' +
    line(t('cls.f.kind'), escHtml(t('cls.provider.' + p.kind))) +
    (p.kind !== 'brain' ? line(t('cls.f.endpoint'), p.base_url ? '<span class="mono">' + escHtml(p.base_url) + '</span>' : '–') +
      line(t('cls.f.model'), p.model ? '<span class="mono">' + escHtml(p.model) + '</span>' : '–') : '') +
    line(t('cls.f.key'), escHtml(clsKeyText(p))) +
    line(t('cls.f.price'), escHtml(clsPriceText(p))) +
    (extra ? line(t('cls.f.extra'), '<span class="mono">' + escHtml(extra) + '</span>') : '') +
    line(t('cls.f.pace'), escHtml(t('cls.pace', {n: fmtN(p.per_minute || 0), s: fmtN(p.timeout_seconds || 0)}))) +
    line(t('cls.f.answers'), escHtml((p.tasks || []).map(k => clsTaskName(k)).join(', ') || '–')) +
    line(t('cls.f.used_for'), escHtml(used.length ? used.join(', ') : t('cls.not_used'))) +
    (p.held && p.held.seconds > 0 ? '<p class="hint warn cls-held">' + escHtml(clsHeldText(p.held)) + '</p>' : '') +
    clsResultsHtml(p) +
    (note ? '<p class="hint">' + escHtml(note) + '</p>' : '') +
    (keyNote ? '<p class="hint">' + escHtml(keyNote) + '</p>' : '') +
    (p.sdk_missing ? '<p class="hint warn">' + escHtml(t('cls.sdk_missing', {sdk: p.sdk_missing})) + '</p>' : '') +
    '<div class="btn-row">' +
      (free.length ? '<select class="form-input cls-use-for" aria-label="' + escHtml(t('cls.use_for')) + '" onchange="if (this.value) setClassifyUse(this.value, ' +
        jsq(p.id) + ')"><option value="">' + escHtml(t('cls.use_for')) + '</option>' +
        free.map(k => '<option value="' + k + '">' + escHtml(clsTaskName(k)) + '</option>').join('') + '</select>' : '') +
      (testable ? '<button class="btn btn-secondary btn-sm" onclick="openClsTest(' + jsq(p.id) + ')">' + escHtml(t('cls.test')) + '</button>' : '') +
      '<button class="btn btn-secondary btn-sm" onclick="editClsProfile(' + jsq(p.id) + ')">' + escHtml(t('cls.edit')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="duplicateClsProfile(' + jsq(p.id) + ')">' + escHtml(t('cls.duplicate')) + '</button>' +
      (p.builtin ? '<button class="btn btn-ghost btn-sm" onclick="clsProfileAction(' + jsq(p.id) + ', \'hide\')">' + escHtml(t('cls.hide')) + '</button>'
                 : '<button class="btn btn-ghost btn-sm" onclick="clsProfileAction(' + jsq(p.id) + ', \'remove\')">' + escHtml(t('cls.remove')) + '</button>') +
      (clsIsDeepInfra(p) && p.key_set ? '<button class="btn btn-ghost btn-sm" onclick="loadClassifyAccount(' + jsq(p.id) + ')">' + escHtml(t('cls.account')) + '</button>' : '') +
    '</div><div id="' + escHtml(box) + '-acct"></div><div id="' + escHtml(box) + '-test"></div></div>';
}

function clsProfile(id) {
  return ((_clsProfiles && _clsProfiles.profiles) || []).find(p => p.id === id) || null;
}

async function clsProfileAction(id, action) {
  const p = clsProfile(id);
  if (action === 'remove' && !confirm(t('cls.remove_confirm', {name: p ? p.name : id}))) return;
  const res = await api('/api/classify/profiles/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  showToast(res && res.success ? t('cls.done.' + action, {name: p ? p.name : id}) + ((res.off || []).length
              ? ' ' + t('cls.now_off', {tasks: res.off.map(k => clsTaskName(k)).join(', ')}) : '')
            : failMsg(res, t('cls.failed')), res && res.success ? 'success' : 'error');
  loadClassifyCard();
}

// A copy of a profile, as one of your own: a built-in's through the
// server (it keeps "based on"), one of yours with its fields.
async function duplicateClsProfile(id) {
  const p = clsProfile(id);
  if (!p) return;
  const res = p.builtin
    ? await api('/api/classify/profiles/' + encodeURIComponent(id), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    : await api('/api/classify/profiles', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                           body: JSON.stringify(Object.assign(clsFields(p), {name: t('cls.copy_name', {name: p.name})}))});
  showToast(res && res.success ? t('cls.copied', {name: (res.profile || {}).name || ''}) : failMsg(res, t('cls.failed')),
            res && res.success ? 'success' : 'error');
  loadClassifyCard();
}

// The fields a profile is saved with (what the server checks).
function clsFields(p) {
  const out = {};
  ['name', 'kind', 'base_url', 'model', 'key_name', 'request_extra', 'json_mode', 'per_minute', 'timeout_seconds',
   'prices', 'tasks', 'questions', 'notes'].forEach(k => { if (p[k] !== undefined && p[k] !== null) out[k] = JSON.parse(JSON.stringify(p[k])); });
  return out;
}

// ── Editing a profile ──

// The measured texts of a kind's family (a decision model's, or a
// language model's), from a built-in that carries them.
function clsDefaultQuestions(kind) {
  const decision = kind === 'decision';
  const from = ((_clsProfiles && _clsProfiles.profiles) || []).find(p => p.builtin && (p.kind === 'decision') === decision && p.questions);
  return from ? JSON.parse(JSON.stringify(from.questions)) : null;
}

function clsKindTasks(kind) {
  const docs = (_clsProfiles && _clsProfiles.tasks) || {};
  const can = CLS_TASKS.filter(k => !docs[k] || !docs[k].kinds || docs[k].kinds.includes(kind));
  return can.length ? can : (kind === 'decision' ? ['site', 'contacts'] : CLS_TASKS.slice());
}

function newClsProfile() {
  _clsEdit = {id: '', builtin: false, d: {name: '', kind: 'openai_compatible', base_url: '', model: '', key_name: '',
    request_extra: {}, json_mode: true, per_minute: 20, timeout_seconds: 30, prices: {in_per_m: null, out_per_m: null, from_list: false},
    tasks: clsKindTasks('openai_compatible'), questions: clsDefaultQuestions('openai_compatible'), notes: ''}};
  renderClsEditor();
}

function editClsProfile(id) {
  const p = clsProfile(id);
  if (!p) return;
  _clsEdit = {id: p.id, builtin: !!p.builtin, d: clsFields(p)};
  // (A built-in's edit is saved as a copy: named as one.)
  if (p.builtin) _clsEdit.d.name = t('cls.copy_name', {name: p.name});
  renderClsEditor();
}

function renderClsEditor() {
  const el = document.getElementById('cls-editor');
  if (!el) return;
  el.innerHTML = _clsEdit ? clsEditorHtml() : '';
  if (_clsEdit && el.scrollIntoView) el.scrollIntoView({block: 'start'});
}

function closeClsEditor() {
  _clsEdit = null;
  renderClsEditor();
}

// The kind changes what is asked: its tasks and its measured texts.
function setClsEditorKind(kind) {
  if (!_clsEdit) return;
  readClsEditor();
  _clsEdit.d.kind = kind;
  _clsEdit.d.tasks = clsKindTasks(kind);
  _clsEdit.d.questions = clsDefaultQuestions(kind);
  renderClsEditor();
}

function clsEditorHtml() {
  const d = _clsEdit.d;
  const val = (v) => escHtml(String(v === undefined || v === null ? '' : v));
  const input = (id, label, value, type, extra) => '<div class="form-group"><label class="form-label" for="clse-' + id + '">' +
    escHtml(label) + '</label><input class="form-input" id="clse-' + id + '" type="' + (type || 'text') + '" value="' + val(value) + '"' + (extra || '') + '></div>';
  const area = (id, label, value, rows) => '<div class="form-group"><label class="form-label" for="clse-' + id + '">' + escHtml(label) +
    '</label><textarea class="form-input" id="clse-' + id + '" rows="' + (rows || 3) + '">' + val(value) + '</textarea></div>';
  const kinds = ((_clsProfiles && _clsProfiles.kinds) || ['openai_compatible', 'anthropic', 'decision', 'brain']);
  const pr = d.prices || {};
  const deepinfra = d.kind === 'openai_compatible' && clsIsDeepInfra(d);
  const q = d.questions || null;
  let questions = '';
  if (!q) {
    questions = '<p class="hint">' + escHtml(t('cls.e.q_after_save')) + '</p>';
  } else {
    questions = (d.tasks || []).map(task => {
      const tq = q[task] || {};
      let body = '';
      if (d.kind !== 'decision') {
        body = area('q-' + task + '-prompt', t('cls.e.prompt'), tq.prompt, 4) + area('q-' + task + '-yes', t('cls.e.yes'), tq.yes, 2) +
          area('q-' + task + '-no', t('cls.e.no'), tq.no, 2);
      } else if (task === 'site') {
        const qs = tq.questions || {};
        body = CLS_SITE_QUESTIONS.filter(k => qs[k]).map(k => area('q-site-' + k, t('cls.e.question', {id: k}), qs[k].instructions, 3) +
            (qs[k].criteria ? '<div class="form-row">' + area('q-site-' + k + '-true', t('cls.e.crit_true'), qs[k].criteria.true, 2) +
              area('q-site-' + k + '-false', t('cls.e.crit_false'), qs[k].criteria.false, 2) + '</div>' : '')).join('') +
          '<div class="form-row">' + Object.keys(tq.needs || {}).map(k => input('q-site-needs-' + k, t('cls.e.needs', {id: k}), tq.needs[k], 'number',
              ' min="0.05" max="0.99" step="0.01"')).join('') +
            Object.keys(tq.vetoes || {}).map(k => input('q-site-vetoes-' + k, t('cls.e.vetoes', {id: k}), tq.vetoes[k], 'number',
              ' min="0.05" max="0.99" step="0.01"')).join('') + '</div>' +
          '<p class="hint">' + escHtml(t('cls.e.bars_hint')) + '</p>';
      } else if (task === 'contacts') {
        body = area('q-contacts-instructions', t('cls.e.instructions'), tq.instructions, 2) +
          Object.keys(tq.options || {}).map(k => input('q-contacts-opt-' + k, t('cls.e.option', {id: k}), tq.options[k])).join('') +
          input('q-contacts-bar', t('cls.yes'), tq.bar, 'number', ' min="0.5" max="0.99" step="0.01"') +
          '<p class="hint">' + escHtml(t('cls.yes_hint')) + '</p>';
      }
      return '<details class="more-fields cls-q"><summary>' + escHtml(t('cls.e.questions_for', {task: clsTaskName(task)})) + '</summary>' + body + '</details>';
    }).join('');
  }
  return '<div class="card cls-edit"><h4>' + escHtml(t(_clsEdit.id ? (_clsEdit.builtin ? 'cls.e.title_builtin' : 'cls.e.title') : 'cls.e.title_new',
      {name: d.name || ''})) + '</h4>' +
    (_clsEdit.builtin ? '<p class="hint">' + escHtml(t('cls.e.copy_note')) + '</p>' : '') +
    '<div class="form-row">' + input('name', t('cls.e.name'), d.name) +
      '<div class="form-group"><label class="form-label" for="clse-kind">' + escHtml(t('cls.f.kind')) + '</label>' +
      '<select class="form-input" id="clse-kind" onchange="setClsEditorKind(this.value)">' + kinds.map(k =>
        '<option value="' + escHtml(k) + '"' + (k === d.kind ? ' selected' : '') + '>' + escHtml(t('cls.provider.' + k)) + '</option>').join('') +
      '</select></div></div>' +
    (d.kind === 'decision' ? '<p class="hint">' + escHtml(t('cls.decision_hint')) + '</p>' : '') +
    (d.kind !== 'brain' ? '<div class="form-row">' + input('base_url', t('cls.base_url'), d.base_url, 'url', ' placeholder="http://127.0.0.1:11434/v1"') +
        input('model', t('cls.model'), d.model) + '</div>' +
      (deepinfra ? '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="loadClassifyModels()">' +
        escHtml(t('cls.models')) + '</button></div><div id="clse-models"></div>' : '') +
      '<div class="form-row">' + input('key_name', t('cls.key_name'), d.key_name) + '</div>' +
      '<p class="hint">' + escHtml(t('cls.e.key_hint')) + '</p>' : '<p class="hint">' + escHtml(t('cls.e.brain_hint')) + '</p>') +
    '<div class="form-row">' + input('per_minute', t('cls.per_minute'), d.per_minute, 'number', ' min="1" max="600"') +
      input('timeout', t('cls.timeout'), d.timeout_seconds, 'number', ' min="5" max="600"') + '</div>' +
    (d.kind !== 'brain' ? (deepinfra ? '<div class="form-group"><label class="pd-setting"><input type="checkbox" id="clse-from_list"' +
        (pr.from_list ? ' checked' : '') + '> ' + escHtml(t('cls.e.from_list')) + '</label></div>' : '') +
      '<div class="form-row">' + input('price_in', t('cls.price_in'), pr.in_per_m, 'number', ' min="0" step="any"') +
        input('price_out', t('cls.price_out'), pr.out_per_m, 'number', ' min="0" step="any"') + '</div>' +
      '<p class="hint">' + escHtml(t('cls.price_hint')) + '</p>' : '') +
    (d.kind !== 'decision' && d.kind !== 'brain' ? '<div class="form-group"><label class="pd-setting"><input type="checkbox" id="clse-json"' +
      (d.json_mode ? ' checked' : '') + '> ' + escHtml(t('cls.json_mode')) + '</label></div>' : '') +
    (d.kind !== 'brain' ? area('extra', t('cls.e.extra'), d.request_extra && Object.keys(d.request_extra).length ? JSON.stringify(d.request_extra) : '', 2) +
      '<p class="hint">' + escHtml(t('cls.e.extra_hint')) + '</p>' : '') +
    '<div class="form-group"><span class="form-label">' + escHtml(t('cls.f.answers')) + '</span>' + clsKindTasks(d.kind).map(k =>
      '<label class="pd-setting"><input type="checkbox" id="clse-task-' + k + '"' + ((d.tasks || []).includes(k) ? ' checked' : '') + '> ' +
      escHtml(clsTaskName(k)) + '</label>').join(' ') + '</div>' +
    '<p class="form-label">' + escHtml(t('cls.e.questions')) + '</p><p class="hint">' + escHtml(t('cls.e.questions_hint')) + '</p>' + questions +
    area('notes', t('cls.e.notes'), d.notes, 2) +
    '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="saveClsProfile()">' + escHtml(t('cls.save')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="closeClsEditor()">' + escHtml(t('common.cancel')) + '</button></div></div>';
}

// The editor's fields into _clsEdit.d; the reason when one can't be read.
function readClsEditor() {
  const d = _clsEdit.d;
  const el = (id) => document.getElementById('clse-' + id);
  const v = (id) => { const x = el(id); return x ? String(x.value || '').trim() : undefined; };
  const num = (id) => { const x = v(id); return x === undefined || x === '' ? null : Number(x); };
  if (v('name') !== undefined) d.name = v('name');
  ['base_url', 'model', 'key_name', 'notes'].forEach(k => { if (v(k) !== undefined) d[k] = v(k); });
  if (v('per_minute') !== undefined) d.per_minute = Math.round(num('per_minute') || 1);
  if (v('timeout') !== undefined) d.timeout_seconds = Math.round(num('timeout') || 30);
  if (el('json')) d.json_mode = !!el('json').checked;
  if (v('price_in') !== undefined) {
    d.prices = {in_per_m: num('price_in'), out_per_m: num('price_out'), from_list: !!(el('from_list') && el('from_list').checked)};
  }
  const tasks = clsKindTasks(d.kind).filter(k => el('task-' + k) && el('task-' + k).checked);
  if (tasks.length || clsKindTasks(d.kind).some(k => el('task-' + k))) d.tasks = tasks;
  let why = '';
  if (v('extra') !== undefined) {
    if (!v('extra')) d.request_extra = {};
    else {
      try {
        const x = JSON.parse(v('extra'));
        if (!x || typeof x !== 'object' || Array.isArray(x)) why = t('cls.e.extra_bad');
        else d.request_extra = x;
      } catch { why = t('cls.e.extra_bad'); }
    }
  }
  const q = d.questions;
  if (q) {
    (d.tasks || []).forEach(task => {
      const tq = q[task];
      if (!tq) return;
      if (d.kind !== 'decision') {
        ['prompt', 'yes', 'no'].forEach(k => { if (v('q-' + task + '-' + k) !== undefined) tq[k] = v('q-' + task + '-' + k); });
      } else if (task === 'site') {
        CLS_SITE_QUESTIONS.forEach(k => {
          if (!tq.questions || !tq.questions[k]) return;
          if (v('q-site-' + k) !== undefined) tq.questions[k].instructions = v('q-site-' + k);
          if (tq.questions[k].criteria) {
            const crit = {};
            ['true', 'false'].forEach(c => { const x = v('q-site-' + k + '-' + c); if (x) crit[c] = x; });
            tq.questions[k].criteria = crit;
          }
        });
        ['needs', 'vetoes'].forEach(part => Object.keys(tq[part] || {}).forEach(k => {
          const x = num('q-site-' + part + '-' + k);
          if (x !== null) tq[part][k] = x;
        }));
      } else if (task === 'contacts') {
        if (v('q-contacts-instructions') !== undefined) tq.instructions = v('q-contacts-instructions');
        Object.keys(tq.options || {}).forEach(k => { const x = v('q-contacts-opt-' + k); if (x !== undefined) tq.options[k] = x; });
        const bar = num('q-contacts-bar');
        if (bar !== null) tq.bar = bar;
      }
    });
  }
  return why;
}

async function saveClsProfile() {
  if (!_clsEdit) return;
  const why = readClsEditor();
  if (why) { showToast(why, 'error'); return; }
  const d = _clsEdit.d;
  const body = clsFields(d);
  if (d.prices && d.prices.from_list) body.prices = {from_list: true};
  if (body.kind === 'brain') { delete body.base_url; delete body.model; delete body.key_name; delete body.prices; }
  if (body.questions) {
    const q = {};
    (d.tasks || []).forEach(k => { if (body.questions[k]) q[k] = body.questions[k]; });
    body.questions = q;
  }
  const path = _clsEdit.id ? '/api/classify/profiles/' + encodeURIComponent(_clsEdit.id) : '/api/classify/profiles';
  const res = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!res || !res.success) { showToast(failMsg(res, t('cls.failed')), 'error'); return; }
  showToast(res.copied_from ? t('cls.saved_copy', {name: (res.profile || {}).name || ''}) : t('cls.saved'), 'success');
  _clsEdit = null;
  loadClassifyCard();
}

// DeepInfra's models with their prices: picking one fills the model field
// (saved with Save). Sorted by price, nothing else.
async function loadClassifyModels() {
  const el = document.getElementById('clse-models');
  if (!el) return;
  el.innerHTML = '<p class="muted small">' + escHtml(t('cls.models_loading')) + '</p>';
  const r = await api('/api/classify/models?provider=openai_compatible');
  const rows = (r && r.models) || [];
  if (!rows.length) {
    el.innerHTML = '<p class="muted small">' + escHtml(t(r && r.error ? 'cls.models_failed' : 'cls.models_none')) + '</p>';
    return;
  }
  el.innerHTML = '<div class="table-wrap"><table class="table small"><thead><tr><th>' + escHtml(t('cls.model')) + '</th><th>' +
    escHtml(t('cls.models.in')) + '</th><th>' + escHtml(t('cls.models.out')) + '</th><th>' + escHtml(t('cls.models.context')) +
    '</th><th></th></tr></thead><tbody>' + rows.map(m => '<tr><td>' + escHtml(m.model) + '</td><td>' + escHtml(clsUsd(m.in_per_m)) +
    '</td><td>' + escHtml(clsUsd(m.out_per_m)) + '</td><td>' + (m.context ? escHtml(fmtN(m.context)) : '–') +
    '</td><td><button class="btn btn-secondary btn-sm" data-model="' + escHtml(m.model) +
    '" onclick="pickClassifyModel(this.dataset.model)">' + escHtml(t('cls.models.pick')) + '</button></td></tr>').join('') +
    '</tbody></table></div><p class="hint">' + escHtml(t('cls.models_hint')) + '</p>';
}

function pickClassifyModel(name) {
  const input = document.getElementById('clse-model');
  if (input) input.value = name || '';
}

// A profile's DeepInfra account, read by the server with its key (the key
// stays there): this month's usage, the key's limit, the balance.
async function loadClassifyAccount(id) {
  const el = document.getElementById('clsp-' + id + '-acct');
  if (!el) return;
  const p = clsProfile(id) || {};
  el.innerHTML = '<p class="muted small">' + escHtml(t('cls.models_loading')) + '</p>';
  const a = await api('/api/classify/account?profile=' + encodeURIComponent(id));
  if (!a || !a.available) {
    el.innerHTML = '<p class="muted small">' + escHtml(a && a.code ? t('cls.account.err.' + a.code, {name: p.key_name || ''})
                                                                   : t('cls.failed')) + '</p>';
    return;
  }
  const part = (x, ok) => x && x.ok ? ok(x) : '<li class="muted">' + escHtml(x && x.code
    ? t('cls.account.err.' + x.code, {scope: x.scope || '', status: x.status || ''}) : t('cls.failed')) + '</li>';
  el.innerHTML = '<ul class="small">' +
    part(a.usage, u => '<li>' + escHtml(t('cls.account.usage', {usd: clsUsd(u.total_usd), period: u.period || ''})) +
      ((u.models || []).length ? ': ' + u.models.map(m => escHtml(m.model + ' ' + clsUsd(m.usd))).join(', ') : '') + '</li>') +
    part(a.limits, l => (l.keys || []).map(k => '<li>' + escHtml(t('cls.account.key', {name: k.name,
      spend: clsUsd(k.spend_usd), limit: k.limit_usd === null || k.limit_usd === undefined ? t('cls.account.no_limit') : clsUsd(k.limit_usd)})) + '</li>').join('')) +
    part(a.balance, b => '<li>' + escHtml(t('cls.account.balance', {credit: clsUsd(b.credit_usd), owed: clsUsd(b.owed_usd)})) +
      (b.suspended ? ' &middot; <b>' + escHtml(t('cls.account.suspended')) + '</b>' : '') + '</li>') + '</ul>';
}

// ── Testing a profile: counted and priced first, then run under a cap ──

function clsTestTasks(id) {
  return ['site', 'results'].filter(k => {
    const box = document.getElementById('clsp-' + id + '-t-' + k);
    return box && box.checked;
  });
}

function openClsTest(id) {
  const p = clsProfile(id);
  const el = document.getElementById('clsp-' + id + '-test');
  if (!p || !el) return;
  const tasks = ['site', 'results'].filter(k => (p.tasks || []).includes(k));
  el.innerHTML = '<div class="cls-test"><p class="small">' + escHtml(t('cls.t.about')) + '</p>' +
    '<div class="form-group">' + tasks.map(k => '<label class="pd-setting"><input type="checkbox" id="clsp-' + escHtml(id) + '-t-' + k + '"' +
      (k === 'site' ? ' checked' : '') + '> ' + escHtml(clsTaskName(k)) + '</label>').join(' ') + '</div>' +
    '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="estimateClsTest(' + jsq(id) + ')">' + escHtml(t('cls.t.price')) + '</button></div>' +
    '<div id="clsp-' + escHtml(id) + '-t-est"></div></div>';
}

async function estimateClsTest(id) {
  const el = document.getElementById('clsp-' + id + '-t-est');
  const tasks = clsTestTasks(id);
  if (!el) return;
  if (!tasks.length) { el.innerHTML = '<p class="muted small">' + escHtml(t('cls.t.pick')) + '</p>'; return; }
  el.innerHTML = '<p class="muted small">' + escHtml(t('disc.pricing')) + '</p>';
  const est = await api('/api/classify/profiles/' + encodeURIComponent(id) + '/test/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tasks: tasks})});
  el.innerHTML = clsTestEstimateHtml(id, est);
}

function clsTestEstimateHtml(id, est) {
  if (!est || est.success === false || est.error) return '<div class="test-result error">' + escHtml(failMsg(est, t('cls.failed'))) + '</div>';
  // (With the price unknown nothing can be counted in USD: a small cap
  // still bounds the run, and the call caps hold.)
  const cap = est.price_known ? Math.max(0.01, Math.ceil((est.usd_high || 0) * 100) / 100) : 0.05;
  return '<p class="small">' + escHtml(t(est.price_known ? 'cls.t.est' : 'cls.t.est_unknown', {calls: fmtN(est.calls || 0),
      tok: fmtN(est.tokens_in || 0), low: clsUsd(est.usd_typical || 0), high: clsUsd(est.usd_high || 0), rows: fmtN(est.rows || 0)})) + '</p>' +
    '<p class="muted small">' + escHtml(t('est.enforced')) + '</p>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="clsp-' + escHtml(id) + '-t-cap">' + escHtml(t('cls.t.cap')) + '</label>' +
      '<input class="form-input" id="clsp-' + escHtml(id) + '-t-cap" type="number" min="0" step="0.01" value="' + cap + '"></div></div>' +
    '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="runClsTest(' + jsq(id) + ', this)"' + (est.calls ? '' : ' disabled') + '>' +
      escHtml(t('cls.t.run', {n: fmtN(est.calls || 0)})) + '</button></div><div id="clsp-' + escHtml(id) + '-t-out"></div>';
}

async function runClsTest(id, btn) {
  const out = document.getElementById('clsp-' + id + '-t-out');
  const cap = Number((document.getElementById('clsp-' + id + '-t-cap') || {}).value);
  if (!(cap >= 0)) { showToast(t('cls.t.cap_bad'), 'error'); return; }
  if (btn) btn.disabled = true;
  const res = await api('/api/classify/profiles/' + encodeURIComponent(id) + '/test', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tasks: clsTestTasks(id), max_spend: cap})});
  if (!res || !res.success) { if (btn) btn.disabled = false; showToast(failMsg(res, t('cls.failed')), 'error'); return; }
  if (out) out.innerHTML = '<p class="muted small">' + escHtml(t('cls.testing')) + '</p>';
  for (let i = 0; i < 300; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const j = await api('/api/classify/test/' + encodeURIComponent(res.job_id));
    if (!j || j.error && !j.status) break;
    if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') {
      if (j.status !== 'done') {
        if (out) out.innerHTML = '<div class="test-result error">' + escHtml(j.error || t('cls.failed')) + '</div>';
        return;
      }
      showToast(t('cls.t.done'), 'success');
      loadClassifyCard();
      return;
    }
  }
}

async function loadRejudgeCount() {
  const el = document.getElementById('rejudge-box');
  if (!el) return;
  const r = await api('/api/contacts/site-checks');
  if (!r || r.error) { el.innerHTML = '<p class="muted small">' + escHtml(failMsg(r, t('cls.failed'))) + '</p>'; return; }
  if (!r.firms) { el.innerHTML = '<p class="muted small">' + escHtml(t('rejudge.none')) + '</p>'; return; }
  el.innerHTML = '<p>' + escHtml(t('rejudge.count', {n: fmtN(r.firms), u: fmtN(r.unconfirmed || 0), l: fmtN(r.likely || 0),
      ruled: fmtN(r.ruled_out_by_address || 0), read: fmtN(r.to_read || 0)})) + '</p>' +
    (r.provider === 'off' ? '<p class="muted small">' + escHtml(t('rejudge.off')) + '</p>' :
      (r.days ? '<p class="muted small">' + escHtml(t('rejudge.days', {n: fmtN(r.days)})) + '</p>' : '')) +
    '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="startRejudge(' + Number(r.firms || 0) + ')">' + escHtml(t('rejudge.start')) + '</button></div>';
}

async function startRejudge(n) {
  // (With the classifier off it still takes off what the address or a
  // register fact settles: said before it starts.)
  const off = !_classify || !_classify.provider || _classify.provider === 'off';
  if (!confirm(t(off ? 'rejudge.confirm_off' : 'rejudge.confirm', {n: fmtN(n || 0)}))) return;
  const res = await api('/api/contacts/site-checks', {method: 'POST'});
  showToast(res && res.success ? t('rejudge.started', {n: fmtN(res.queued || 0), batch: res.batch_id || ''})
            : failMsg(res, t('cls.failed')), res && res.success ? 'success' : 'error');
  loadRejudgeCount();
}

// Why a website a search found was taken: the register fact that confirmed
// it, or the classifier's yes (the company panel).
function siteBasisText(b) {
  if (!b || !b.found_by) return '';
  const by = t('site.basis.found_by', {module: tServer('module.' + b.found_by + '.name', b.found_by)});
  if (!b.by) return by + ' · ' + t('site.basis.unchecked');
  if (b.by !== 'judge') return by + ' · ' + t('site.basis.' + b.by);
  if (typeof b.p === 'number') {
    return by + ' · ' + t('site.basis.judge_yes_p', {model: b.model || '', p: Math.round(b.p * 100)});
  }
  return by + ' · ' + t('site.basis.judge_yes', {model: b.model || ''});
}

let _finder = null;

async function openFinder(target, body) {
  const el = document.getElementById(target);
  if (!el) return;
  el.dataset.open = '1';
  delete el.dataset.run;
  el.innerHTML = '<div class="card"><p class="muted small">' + escHtml(t('disc.pricing')) + '</p></div>';
  const est = await api('/api/contacts/find/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!est || est.error) {
    el.innerHTML = '<div class="test-result error">' + escHtml(failMsg(est, t('cl.find_failed'))) + '</div>';
    return;
  }
  _finder = {target: target, body: body, est: est};
  const paid = est.to_look_up > 0;
  const cap = paid ? Math.min(1, Math.max(0.01, Math.ceil(est.estimated_cost * 100) / 100)) : 0;
  el.innerHTML = '<div class="card finder-card"><h2>' + escHtml(t('finder.title')) + '</h2>' +
    '<p>' + escHtml(t('finder.summary', {n: fmtN(est.companies), site: fmtN(est.with_site),
                                         look: fmtN(est.to_look_up)})) +
      (est.more ? ' ' + escHtml(t('finder.more')) : '') + '</p>' +
    (paid ? (est.searchers.length
      ? '<p class="muted small">' + escHtml(t('finder.cost', {cost: fmtUsd(est.estimated_cost, 4),
                                                            most: fmtUsd(est.most, 4)})) + '</p>'
      : '<p class="muted small">' + escHtml(t('finder.no_search')) + '</p>') : '') +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="finder-cap">' +
      escHtml(t('finder.cap')) + '</label><input class="form-input" id="finder-cap" type="number" min="0" step="0.01" value="' + cap + '" oninput="finderCapChanged()">' +
      '<div class="muted small">' + escHtml(t('finder.cap_help')) + '</div></div>' + finderClassifyField(est.classifier) + '</div>' +
    '<div id="finder-checks-wrap">' + finderChecksLine(est.checks, cap) + '</div>' + finderClassifyLine(est.classifier) +
    '<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="runFinder(this)"' + (est.companies ? '' : ' disabled') + '>' +
      escHtml(t('finder.run', {n: fmtN(est.companies)})) + '</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="closeFinder()">' + escHtml(t('common.cancel')) + '</button></div></div>';
}

// The classifier's part of a Find-contacts run: how many questions it may
// ask and what they may cost, typical to high (a call's size varies with
// what it reads), and a cap of the run's own (blank: none, the install's
// caps hold; 0: no question, a site no register fact settles waits
// unlinked). Nothing to ask: no field.
function finderClassifyField(c) {
  if (!c || !c.calls) return '';
  const cap = c.price_known ? Math.max(0.01, Math.ceil((c.usd_high || 0) * 100) / 100) : '';
  return '<div class="form-group"><label class="form-label" for="finder-ccap">' + escHtml(t('finder.ccap')) + '</label>' +
    '<input class="form-input" id="finder-ccap" type="number" min="0" step="0.01" value="' + cap + '">' +
    '<div class="muted small">' + escHtml(t('finder.ccap_help')) + '</div></div>';
}

function finderClassifyLine(c) {
  if (!c || !c.calls) return '';
  const per = CLS_TASKS.filter(k => ((c.per_task || {})[k] || {}).calls).map(k =>
    t('finder.c_task', {task: clsTaskName(k), n: fmtN(c.per_task[k].calls)})).join(', ');
  return '<p class="muted small" id="finder-classify">' + escHtml(t(c.price_known ? 'finder.c_cost' : 'finder.c_cost_unknown',
    {n: fmtN(c.calls), per: per, low: fmtUsd(c.usd_typical || 0, 4), high: fmtUsd(c.usd_high || 0, 4)})) + '</p>' +
    '<p class="muted small">' + escHtml(t('est.enforced')) + '</p>';
}

// The address checks a run may make (only with a spend cap above 0) and
// what they may cost past each service's free allowance this month, at the
// prices entered in Settings; a service past its allowance with no price is
// named: the run refuses its checks. With no cap, one short line.
function finderChecksLine(k, cap) {
  if (!k || !k.checks || !(k.verifiers || []).length) return '';
  if (!(Number(cap) > 0)) return '<p class="muted small" id="finder-checks">' + escHtml(t('finder.k_need_cap')) + '</p>';
  const free = Object.keys(k.free_left || {}).map(v => t('finder.k_free', {name: v, n: fmtN(k.free_left[v])})).join(', ');
  return '<p class="muted small" id="finder-checks">' + escHtml(t(k.usd_high ? 'finder.k_cost' : 'finder.k_free_only',
      {n: fmtN(k.checks), per: fmtN(k.per_company || 0), usd: fmtUsd(k.usd_high || 0, 4), free: free,
       left: fmtN(k.checks_left || 0)})) +
    ((k.refused || []).length ? ' ' + escHtml(t('finder.k_refused', {names: k.refused.join(', ')})) : '') + '</p>';
}

function finderCapChanged() {
  const el = document.getElementById('finder-checks-wrap');
  if (!el || !_finder) return;
  el.innerHTML = finderChecksLine(_finder.est.checks, (document.getElementById('finder-cap') || {}).value);
}

function closeFinder() {
  if (!_finder) return;
  const el = document.getElementById(_finder.target);
  if (el) { delete el.dataset.open; delete el.dataset.run; el.innerHTML = ''; }
  if (_finder.target === 'cl-finder') loadFinderStatus();
  _finder = null;
}

async function runFinder(btn) {
  if (!_finder) return;
  btn.disabled = true;
  const cap = Number((document.getElementById('finder-cap') || {}).value) || 0;
  const body = Object.assign({}, _finder.body, {max_spend: cap});
  // (Blank: no classifier cap of the run's own.)
  const ccap = document.getElementById('finder-ccap');
  if (ccap && String(ccap.value).trim() !== '') body.classify_max_spend = Number(ccap.value) || 0;
  const data = await api('/api/contacts/find', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (data && data.success) {
    showToast(t(data.force ? 'cl.find_queued_force' : 'cl.find_queued') + ' ' +
              t('finder.queued', {n: fmtN(data.queued || 0)}), 'success');
    if (data.batch_id) watchRun(_finder.target, data.batch_id);
    else closeFinder();
  } else {
    btn.disabled = false;
    showToast(failMsg(data, t('cl.find_failed')), 'error');
  }
}

// A run's report in words: its jobs, how long it took, what it cost split
// (searches, address checks, the classifier, anything else).
function runReportHtml(r) {
  if (!r || !r.statuses) return '';
  const st = r.statuses || {};
  const c = r.classify || {};
  const secs = (s) => s === null || s === undefined ? '–' : (s >= 90 ? t('run.min', {n: fmtN(Math.round(s / 60))}) : t('run.sec', {n: fmtN(Math.round(s))}));
  const state = ['running', 'queued', 'paused', 'done', 'failed', 'cancelled'].filter(k => st[k])
    .map(k => t('run.st.' + k, {n: fmtN(st[k])})).join(', ');
  const cost = [t('run.searches', {usd: fmtUsd(r.searches_usd || 0, 4)}),
    t('run.checks', {n: fmtN(r.checks_made || 0), usd: fmtUsd(r.checks_usd || 0, 4)}) +
      (Object.keys(r.services || {}).length ? ' (' + Object.keys(r.services).map(k => k + ' ' + fmtN(r.services[k])).join(', ') + ')' : '') +
      (r.checks_refused ? ' ' + t('run.checks_refused', {n: fmtN(r.checks_refused)}) : ''),
    t('run.classify', {n: fmtN(c.calls || 0), tok: fmtN((c.tokens_in || 0) + (c.tokens_out || 0)), usd: fmtUsd(c.usd || 0, 4)}) +
      (c.unpriced ? ' ' + t('run.unpriced', {n: fmtN(c.unpriced)}) : '')];
  if (r.lookups_made) cost.push(t('run.lookups', {n: fmtN(r.lookups_made), usd: fmtUsd(r.lookups_usd || 0, 4)}));
  if (r.other_usd) cost.push(t('run.other', {usd: fmtUsd(r.other_usd, 4)}));
  return '<div class="run-report"><p class="small">' + escHtml(t('run.jobs', {n: fmtN(r.jobs || 0), state: state})) + ' &middot; ' +
      escHtml(t('run.took', {wall: secs(r.seconds), work: secs(r.job_seconds)})) + '</p>' +
    '<p class="small">' + escHtml(cost.join(' · ')) + ' &middot; <b>' + escHtml(t('run.total', {usd: fmtUsd(r.total_usd || 0, 4)})) + '</b></p>' +
    (c.refused ? '<p class="hint warn">' + escHtml(t('run.refused', {n: fmtN(c.refused)})) + '</p>' : '') + '</div>';
}

function runActive(r) {
  const st = (r && r.statuses) || {};
  return !!(st.running || st.queued);
}

// A run just started, followed where it was started: its report every few
// seconds while its jobs run, then what it took and cost.
async function watchRun(target, batchId) {
  const el = document.getElementById(target);
  if (!el) return;
  el.dataset.open = '1';
  el.dataset.run = batchId;
  for (let i = 0; i < 2000; i++) {
    const r = await api('/api/jobs/batches/' + encodeURIComponent(batchId) + '/report');
    if (el.dataset.run !== batchId) return;
    if (!r || !r.statuses) { closeFinder(); return; }
    const active = runActive(r);
    el.innerHTML = '<div class="card finder-card"><h2>' + escHtml(t(active ? 'run.running' : 'run.finished')) + '</h2>' + runReportHtml(r) +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="openQueue(' + jsq(batchId) + ')">' + escHtml(t('disc.see_jobs')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="closeFinder()">' + escHtml(t('common.close')) + '</button></div></div>';
    if (!active) return;
    await new Promise(res => setTimeout(res, 3000));
    if (el.dataset.run !== batchId) return;
  }
}

function exportContactList(format) {
  downloadCsv('/api/export/contacts.' + format + '?' + contactParams(false), 'contacte-' + stamp() + '.' + format);
}

// ── Contacts: same pattern as Companies ──

let _ct = { q: '', filter: 'all', offset: 0, limit: 50, total: 0, counts: {}, selected: new Set(), timer: null, gen: 0 };
const CONTACT_FILTERS = ['all', 'todo', 'contacted', 'replied', 'closed'];

// The Email module's outreach list (the prospects): reachable from the
// Contacts tab while that module is on.
async function loadOutreach() {
  const el = document.getElementById('ct-body');
  if (!el) return;
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
  if (!(res.prospects || []).length && _ct.offset > 0 && _ct.total > 0) { _ct.offset = 0; return loadOutreach(); }
  _prospects = res.prospects || [];
  renderContacts();
}

function contactSearchInput(value) {
  _ct.q = value;
  _ct.offset = 0;
  clearTimeout(_ct.timer);
  _ct.timer = setTimeout(loadOutreach, 250);
}

function setContactFilter(f) { _ct.filter = f; _ct.offset = 0; loadOutreach(); }

function pageContacts(how, value) {
  pageMove(_ct, how, value);
  loadOutreach().then(() => {
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
  loadOutreach();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  _cv.view = 'outreach';
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
  if (!ok) showToast(failMsg(res, t('camp.save_failed')), 'error');
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

// Each campaign's sequence, step by step, with sending by hand when Harvey
// can't send itself.
function renderSequenceCards(data) {
  const el = document.getElementById('campaigns-list');
  if (!el) return;
  if (!data.length) { el.innerHTML = ''; return; }
  let html = '<div class="subhead">' + escHtml(t('cp.sequences')) + '</div>';
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

// ── Campaigns you make: the list, the audiences, the new-campaign wizard ──
//
// A campaign takes its members from an audience (a saved Contacts filter),
// through the same rules wherever they come from; staging only fills the
// review queue. The wizard saves a draft first, so generating the sequence
// and starting work on what is saved.

let _cp = {tab: 'campaigns', setup: null, open: null, detail: null, wiz: null, busy: false, previews: {}};
const CAMPAIGN_TABS = ['campaigns', 'audiences'];
const MEMBER_ORDER = ['waiting', 'ready', 'staged', 'in_sequence', 'done', 'replied', 'stopped', 'suppressed',
                      'bounced', 'skipped', 'removed'];

async function loadCampaigns() {
  const tabs = document.getElementById('camp-tabs');
  if (tabs) tabs.innerHTML = CAMPAIGN_TABS.map(v =>
    '<button class="' + (v === _cp.tab ? 'on' : '') + '" aria-pressed="' + (v === _cp.tab) + '" onclick="setCampaignTab(\'' + v + '\')">' +
      escHtml(t('cp.tab.' + v)) + '</button>').join('');
  _cp.setup = await api('/api/campaigns/setup') || _cp.setup;
  renderCampaignWizard();
  if (_cp.tab === 'audiences') {
    document.getElementById('campaign-detail').innerHTML = '';
    document.getElementById('campaigns-list').innerHTML = '';
    return loadAudiences();
  }
  const el = document.getElementById('campaigns-table');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), escHtml(t('camp.empty')), escHtml(t('camp.empty_copy')));
    document.getElementById('campaigns-list').innerHTML = '';
    return;
  }
  const act = (id, action, label, cls) => '<button class="btn ' + (cls || 'btn-ghost') + ' btn-sm" onclick="campaignAction(' + jsq(id) + ', \'' + action + '\')">' +
    escHtml(t(label)) + '</button>';
  let html = '<div class="table-card"><table class="data"><thead><tr><th>' + escHtml(t('cp.col.name')) + '</th><th>' + escHtml(t('cp.col.status')) +
    '</th><th>' + escHtml(t('cp.col.members')) + '</th><th>' + escHtml(t('cp.col.sent')) + '</th><th>' + escHtml(t('cp.col.replies')) +
    '</th><th>' + escHtml(t('cp.col.created')) + '</th><th></th></tr></thead><tbody>';
  data.forEach(c => {
    const auto = c.origin !== 'user';
    const actions = [];
    actions.push('<button class="btn btn-secondary btn-sm" onclick="openCampaign(' + jsq(c.id) + ')">' + escHtml(t('cp.open')) + '</button>');
    if (c.status === 'draft' && !auto) actions.push('<button class="btn btn-ghost btn-sm" onclick="editCampaign(' + jsq(c.id) + ')">' + escHtml(t('cp.edit')) + '</button>');
    if (c.status === 'active') actions.push(act(c.id, 'pause', 'cp.pause'));
    if (c.status === 'paused') actions.push(act(c.id, 'resume', 'cp.resume'));
    if (['draft', 'active', 'paused'].includes(c.status)) actions.push(act(c.id, 'stop', 'cp.stop'));
    if (['draft', 'finished'].includes(c.status)) actions.push(act(c.id, 'archive', 'cp.archive'));
    html += '<tr><td data-label="' + escHtml(t('cp.col.name')) + '"><div class="cell-stack"><span class="cell-main">' + escHtml(c.name || t('camp.untitled')) + '</span>' +
        '<span class="cell-sub">' + escHtml(t(auto ? 'cp.origin_auto' : 'cp.origin_user')) + (c.rolling ? ' · ' + escHtml(t('cp.rolling')) : '') + '</span></div></td>' +
      '<td data-label="' + escHtml(t('cp.col.status')) + '">' + badge(c.status) + '</td>' +
      '<td data-label="' + escHtml(t('cp.col.members')) + '">' + fmtN(c.member_total || 0) + '</td>' +
      '<td data-label="' + escHtml(t('cp.col.sent')) + '">' + fmtN(c.sent || 0) + '</td>' +
      '<td data-label="' + escHtml(t('cp.col.replies')) + '">' + fmtN(c.replies || 0) + '</td>' +
      '<td class="muted nowrap" data-label="' + escHtml(t('cp.col.created')) + '">' + relTime(c.created_at) + '</td>' +
      '<td><div class="btn-row">' + actions.join('') + '</div></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
  renderSequenceCards(data);
  if (_cp.open) loadCampaignDetail();
}

function setCampaignTab(v) {
  if (v === _cp.tab) return;
  _cp.tab = v;
  document.getElementById('campaigns-table').innerHTML = '';
  loadCampaigns();
}

async function campaignAction(id, action) {
  if (_cp.busy) return;
  if (action === 'stop' && !confirm(t('cp.stop_confirm'))) return;
  _cp.busy = true;
  try {
    const res = await api('/api/campaigns/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
    if (res && res.success) showToast(t('cp.done.' + action), 'success');
    else showToast(failMsg(res, t('camp.save_failed')), 'error');
  } finally {
    _cp.busy = false;
  }
  loadCampaigns();
}

// Why a member left a campaign, as the server keeps it (English, sometimes
// with a name or date after it): the known reasons in the page's language,
// the rest of the text kept as it is.
const MEMBER_REASONS = [
  [/^they replied$/, 'cp.why.replied'], [/^the email bounced$/, 'cp.why.bounced'],
  [/^every step sent$/, 'cp.why.done'], [/^(campaign )?stopped by you$/, 'cp.why.stopped'],
  [/^taken out by you$/, 'cp.why.removed'], [/^sent_by_hand$/, 'cp.why.hand_sent'],
  [/^no trusted link any more$/, 'cp.why.untrusted'], [/^no usable address$/, 'cp.why.no_address'],
  [/^(unsubscribe|opt_out|objection)$/, 'cp.why.opted_out'],
  [/^email status /, 'cp.why.email_status'], [/^prospect status /, 'cp.why.prospect_status'],
  [/^a colleague replied/, 'cp.why.colleague'], [/^(overlap: |already contacted \()in campaign /, 'cp.why.in_campaign'],
  [/^(overlap: |already contacted \()cold email sent /, 'cp.why.sent_lately'],
  [/^(overlap: |already contacted \()contacted outside the outbox/, 'cp.why.contacted_elsewhere'],
  [/^already contacted/, 'cp.why.already'], [/^gate: /, 'cp.why.gate'],
  [/^rejected$/, 'cp.why.rejected'], [/^cut off mid-send$/, 'cp.why.cut_off'],
  [/^send failed$/, 'cp.why.send_failed'], [/^deployed to Instantly$/, 'cp.why.instantly'],
  [/ is on the do-not-contact list/, 'cp.why.suppressed'],
];

function memberReason(text) {
  const s = String(text || '');
  for (const [re, key] of MEMBER_REASONS) {
    const m = s.match(re);
    if (!m) continue;
    // What follows the known words (a campaign's name, a date, a status).
    let rest = s.slice(0, m.index) + s.slice(m.index + m[0].length);
    if (m[0].includes('(')) rest = rest.replace(/\)$/, '');
    return t(key) + (rest.trim() ? ' ' + rest.trim() : '');
  }
  return s;
}

// Why campaign addresses wait for a check (campaigns.checks_held's code).
function checksHeldText(code, fallback) {
  return ['no_price', 'day', 'month'].includes(code) ? t('cp.checks_held.' + code) : String(fallback || '');
}

function openCampaign(id) {
  _cp.open = id;
  loadCampaignDetail();
}

// How many addresses a campaign's rules left out as it last decided them.
function candidatesLeftOut(cands) {
  return Object.entries(cands || {}).reduce((n, [k, v]) => n + (k === 'members' ? 0 : (Number(v) || 0)), 0);
}

async function loadCampaignDetail() {
  const el = document.getElementById('campaign-detail');
  if (!el || !_cp.open) return;
  const data = await api('/api/campaigns/' + encodeURIComponent(_cp.open));
  if (!data) { el.innerHTML = ''; _cp.open = null; return; }
  _cp.detail = data;
  const c = data.campaign;
  const counts = MEMBER_ORDER.filter(k => (c.members || {})[k]).map(k =>
    '<span class="tag">' + escHtml(statusMeta(k).label) + ' <b>' + fmtN(c.members[k]) + '</b></span>').join(' ');
  const open = ['waiting', 'ready', 'staged', 'in_sequence'];
  const rows = (data.members || []).map((m, i) =>
    '<tr><td class="mono">' + escHtml(m.email) + '</td><td>' + badge(m.status_name) + '</td>' +
    '<td>' + escHtml(String(m.last_step || 0)) + '</td><td class="muted">' + escHtml(memberReason(m.end_reason)) + '</td>' +
    '<td>' + (open.includes(m.status_name) && ['draft', 'active', 'paused'].includes(c.status)
      ? '<button class="btn btn-ghost btn-sm" onclick="removeCampaignMember(' + i + ')">' + escHtml(t('cp.remove')) + '</button>' : '') +
    '</td></tr>').join('');
  el.innerHTML = '<div class="card"><div class="card-head"><h3>' + escHtml(c.name) + '</h3><span class="meta">' + badge(c.status) + '</span></div>' +
    '<p class="small">' + (counts || escHtml(t('cp.no_members'))) + '</p>' +
    (data.checks_held ? '<p class="small"><b>' + escHtml(t('cp.checks_held', {n: fmtN((c.members || {}).waiting || 0)})) + '</b> ' +
      escHtml(checksHeldText(data.checks_held.code, data.checks_held.why)) + '</p>' : '') +
    (candidatesLeftOut(c.candidates) ? '<p class="small muted">' + escHtml(t('cp.candidates', {n: fmtN(candidatesLeftOut(c.candidates))})) + '</p>' : '') +
    '<div class="subhead">' + escHtml(t('cp.filter')) + '</div>' +
    '<p class="small"><code>' + escHtml(c.query || t('cp.no_filter')) + '</code>' +
      (c.query ? ' <button class="btn btn-ghost btn-sm" onclick="openQueryInContacts(' + jsq(c.query) + ')">' + escHtml(t('cp.open_in_contacts')) + '</button>' : '') + '</p>' +
    '<div class="subhead">' + escHtml(t('cw.li_title')) + '</div>' + campaignNoteHtml(c) +
    (rows ? '<div class="table-card"><table class="data"><thead><tr><th>' + escHtml(t('cp.col.address')) + '</th><th>' + escHtml(t('cp.col.status')) +
      '</th><th>' + escHtml(t('cp.col.step')) + '</th><th>' + escHtml(t('cp.col.why')) + '</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '') +
    '<div class="btn-row" style="margin-top:12px">' +
      '<button class="btn btn-secondary btn-sm" onclick="downloadCampaignRecord(\'csv\', this)">' + escHtml(t('cp.record_csv')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="downloadCampaignRecord(\'json\', this)">' + escHtml(t('cp.record_json')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_cp.open=null;document.getElementById(\'campaign-detail\').innerHTML=\'\'">' +
      escHtml(t('common.close')) + '</button></div>' +
    '<p class="small muted">' + escHtml(t('cp.record_note')) + '</p></div>';
}

// The legitimate-interest note: editable while the campaign is a draft,
// shown as it was kept once it has started.
function campaignNoteHtml(c) {
  if (c.status !== 'draft') {
    return '<p class="small" style="white-space:pre-wrap">' + escHtml(c.li_note || t('cp.no_note')) + '</p>';
  }
  const min = (_cp.setup && _cp.setup.li_note_min) || 80;
  const n = String(c.li_note || '').trim().length;
  return '<textarea class="form-input" rows="4" id="cp-li" aria-label="' + escHtml(t('cw.li_title')) + '" ' +
      'oninput="document.getElementById(\'cp-li-n\').textContent=this.value.trim().length">' + escHtml(c.li_note || '') + '</textarea>' +
    '<div class="btn-row"><span class="small muted"><span id="cp-li-n">' + n + '</span> / ' + min + '</span>' +
    '<button class="btn btn-secondary btn-sm" onclick="saveCampaignNote()">' + escHtml(t('cp.save_note')) + '</button></div>';
}

async function saveCampaignNote() {
  const el = document.getElementById('cp-li');
  if (!el || !_cp.open) return;
  const res = await api('/api/campaigns/' + encodeURIComponent(_cp.open), {
    method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({li_note: el.value})});
  if (res && res.success) { showToast(t('cp.note_saved'), 'success'); loadCampaigns(); }
  else showToast(failMsg(res, t('camp.save_failed')), 'error');
}

// The campaign's record (evidență), as the server builds it.
function downloadCampaignRecord(format, btn) {
  if (!_cp.open) return;
  downloadCsv('/api/campaigns/' + encodeURIComponent(_cp.open) + '/record.' + format,
              'evidenta-' + _cp.open + '-' + stamp() + '.' + format, btn);
}

async function removeCampaignMember(i) {
  const m = _cp.detail && (_cp.detail.members || [])[i];
  if (!m || !confirm(t('cp.remove_confirm', {email: m.email}))) return;
  const res = await api('/api/campaigns/' + encodeURIComponent(_cp.open) + '/members', {
    method: 'DELETE', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({emails: [m.email]})});
  if (!res || !res.success) showToast(failMsg(res, t('camp.save_failed')), 'error');
  loadCampaigns();
}

// A saved filter back on the Contacts page.
function openQueryInContacts(query) {
  const p = new URLSearchParams(query);
  _cv.f = {};
  _cv.q = p.get('q') || '';
  p.forEach((v, k) => { if (k !== 'q') _cv.f[k] = v; });
  _cv.view = 'contacts';
  _cv.offset = 0;
  const body = document.getElementById('ct-body');
  if (body) body.innerHTML = '';
  goTab('prospects');
}

// ── Audiences ──

async function loadAudiences() {
  const el = document.getElementById('campaigns-table');
  const data = await api('/api/audiences');
  if (!data) { el.innerHTML = offlineState(); return; }
  const rows = data.audiences || [];
  if (!rows.length) {
    el.innerHTML = emptyState(icon('contacts'), escHtml(t('cp.aud_empty')), escHtml(t('cp.aud_empty_copy')));
    return;
  }
  el.innerHTML = '<div class="table-card"><table class="data"><thead><tr><th>' + escHtml(t('cp.col.name')) + '</th><th>' +
    escHtml(t('cp.filter')) + '</th><th>' + escHtml(t('cp.col.campaigns')) + '</th><th></th></tr></thead><tbody>' +
    rows.map(a => '<tr><td class="cell-main">' + escHtml(a.name) + '</td><td class="mono small">' + escHtml(a.query) + '</td>' +
      '<td>' + fmtN(a.campaigns || 0) + '</td><td><div class="btn-row">' +
      '<button class="btn btn-secondary btn-sm" onclick="previewAudience(' + a.id + ')">' + escHtml(t('cw.preview')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="openQueryInContacts(' + jsq(a.query) + ')">' + escHtml(t('cp.open_in_contacts')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="renameAudience(' + a.id + ', ' + jsq(a.name) + ')">' + escHtml(t('cp.rename')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="archiveAudience(' + a.id + ')">' + escHtml(t('cp.archive')) + '</button></div>' +
      (_cp.previews[a.id] ? previewHtml(_cp.previews[a.id]) : '') + '</td></tr>').join('') + '</tbody></table></div>';
}

async function previewAudience(id) {
  const res = await api('/api/audiences/' + id + '/preview');
  if (!res || !res.counts) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return; }
  _cp.previews[id] = res.counts;
  loadAudiences();
}

async function renameAudience(id, name) {
  const next = prompt(t('cp.rename_prompt'), name);
  if (!next || !next.trim()) return;
  const res = await api('/api/audiences/' + id, {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({name: next.trim()})});
  if (!res || !res.success) showToast(failMsg(res, t('camp.save_failed')), 'error');
  loadAudiences();
}

async function archiveAudience(id) {
  await api('/api/audiences/' + id, {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify({archived: true})});
  loadAudiences();
}

// What a campaign would take, counted (nothing is written by a preview).
function previewHtml(c) {
  const p = c.personal || {};
  const line = (key, n) => '<li>' + tBold(key, n || 0) + '</li>';
  return '<ul class="small preview-counts">' +
    line('cw.c.ready', c.ready) + line('cw.c.risky', c.risky) + line('cw.c.to_verify', c.to_verify) +
    line('cw.c.left_out', c.left_out) + line('cw.c.untrusted', c.untrusted) + line('cw.c.suppressed', c.suppressed) +
    line('cw.c.overlap', c.overlap) + line('cw.c.answered', c.answered) + line('cw.c.risky_held', c.risky_held) +
    line('cw.c.personal', (p.named || 0) + (p.free_mail || 0) + (p.natural_person || 0)) +
    line('cw.c.firms', c.firms) + '</ul>' +
    (c.personal_sample ? '<p class="small muted">' + escHtml(t('cw.c.personal_sample', {m: fmtN(c.personal_sample)})) + '</p>' : '') +
    (c.send_to_risky !== undefined ? '<p class="small muted">' + escHtml(t(c.send_to_risky ? 'cw.c.risky_taken' : 'cw.c.risky_not_taken')) + '</p>' : '') +
    (c.days_at_cap ? '<p class="small muted">' + escHtml(t('cw.c.days', {n: c.days_at_cap, cap: (_cp.setup || {}).max_daily_sends || 0})) + '</p>' : '');
}

// ── The new-campaign wizard ──

function openCampaignWizard() {
  const s = _cp.setup || {};
  _cp.wiz = {id: null, name: '', audience: (s.audiences || []).length ? String(s.audiences[0].id) : 'current', rolling: false,
             offer: (s.offer || {}).offer || '', goal: 'reply', booking_link: '', review: false, risky: !!s.send_to_risky,
             steps: [{subject: '', body: '', delay_days: 0}], li_note: s.li_note_template || '', counts: null};
  renderCampaignWizard();
  const el = document.getElementById('campaign-wizard');
  if (el) el.scrollIntoView({block: 'start'});
}

async function editCampaign(id) {
  const data = await api('/api/campaigns/' + encodeURIComponent(id));
  if (!data) return;
  const c = data.campaign;
  _cp.wiz = {id: c.id, name: c.name, audience: c.audience_id ? String(c.audience_id) : 'current', rolling: !!c.rolling,
             offer: (c.offer || {}).offer || '', goal: (c.offer || {}).goal || 'reply', booking_link: (c.offer || {}).booking_link || '',
             review: c.review === 'always', steps: (c.sequence || []).length ? c.sequence : [{subject: '', body: '', delay_days: 0}],
             risky: c.send_to_risky === null || c.send_to_risky === undefined ? !!(_cp.setup || {}).send_to_risky : !!c.send_to_risky,
             status: c.status || 'draft',
             li_note: c.li_note || '', counts: null, query: c.audience_query || ''};
  renderCampaignWizard();
}

function closeCampaignWizard() {
  _cp.wiz = null;
  renderCampaignWizard();
}

function cwSet(key, value) { if (_cp.wiz) _cp.wiz[key] = value; }

function cwStep(i, key, value) {
  const st = _cp.wiz && _cp.wiz.steps[i];
  if (!st) return;
  st[key] = key === 'delay_days' ? Number(value) || 0 : value;
}

function cwAddStep() {
  if (!_cp.wiz || _cp.wiz.steps.length >= 5) return;
  _cp.wiz.steps.push({subject: '', body: '', delay_days: 3});
  renderCampaignWizard();
}

function cwRemoveStep(i) {
  if (!_cp.wiz || _cp.wiz.steps.length <= 1) return;
  _cp.wiz.steps.splice(i, 1);
  renderCampaignWizard();
}

function renderCampaignWizard() {
  const el = document.getElementById('campaign-wizard');
  if (!el) return;
  const s = _cp.setup;
  const w = _cp.wiz;
  if (!w) { el.innerHTML = ''; return; }
  if (s && !s.native) {
    el.innerHTML = '<div class="banner bad">' + icon('alert') + '<div><b>' + escHtml(t('cw.native_title')) + '</b><p>' +
      escHtml(t('cw.native_only')) + '</p><div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="goTab(\'settings\')">' +
      escHtml(t('cw.to_settings')) + '</button><button class="btn btn-ghost btn-sm" onclick="closeCampaignWizard()">' +
      escHtml(t('common.close')) + '</button></div></div></div>';
    return;
  }
  const opt = (v, label, cur) => '<option value="' + escHtml(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + escHtml(label) + '</option>';
  const audiences = ((s && s.audiences) || []).map(a => opt(a.id, a.name, w.audience)).join('') +
    opt('current', t('cw.from_contacts'), w.audience);
  const accounts = ((s && s.accounts) || []).map(a => opt(a.id, (a.address || a.label) + ' (' + (a.provider || '?') + ')', 1)).join('');
  const steps = w.steps.map((st, i) => '<div class="card"><div class="card-head"><h3>' +
      escHtml(i === 0 ? t('camp.first') : t('camp.followup')) + '</h3>' +
      (i ? '<button class="btn btn-ghost btn-sm" onclick="cwRemoveStep(' + i + ')">' + escHtml(t('cw.remove_step')) + '</button>' : '') + '</div>' +
      '<div class="form-row"><div class="form-group"><label class="form-label" for="cw-subj-' + i + '">' + escHtml(t('cw.subject')) + '</label>' +
      '<input class="form-input" id="cw-subj-' + i + '" value="' + escHtml(st.subject) + '" oninput="cwStep(' + i + ', \'subject\', this.value)"></div>' +
      (i ? '<div class="form-group"><label class="form-label" for="cw-delay-' + i + '">' + escHtml(t('cw.delay')) + '</label>' +
        '<input class="form-input" type="number" min="1" max="60" id="cw-delay-' + i + '" value="' + escHtml(String(st.delay_days || 3)) + '" ' +
        'oninput="cwStep(' + i + ', \'delay_days\', this.value)"></div>' : '') + '</div>' +
      '<div class="form-group"><label class="form-label" for="cw-body-' + i + '">' + escHtml(t('cw.body')) + '</label>' +
      '<textarea class="form-input" rows="5" id="cw-body-' + i + '" oninput="cwStep(' + i + ', \'body\', this.value)">' + escHtml(st.body) + '</textarea></div></div>').join('');
  const min = (s && s.li_note_min) || 80;
  el.innerHTML = '<div class="card"><div class="card-head"><h3>' + escHtml(t(w.id ? 'cw.edit_title' : 'cw.title')) + '</h3>' +
      '<button class="btn btn-ghost btn-sm" onclick="closeCampaignWizard()">' + escHtml(t('common.close')) + '</button></div>' +
    '<div class="form-group"><label class="form-label" for="cw-name">' + escHtml(t('cw.name')) + '</label>' +
      '<input class="form-input" id="cw-name" value="' + escHtml(w.name) + '" oninput="cwSet(\'name\', this.value)"></div>' +
    '<div class="subhead">1. ' + escHtml(t('cw.step.audience')) + '</div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="cw-aud">' + escHtml(t('cw.audience')) + '</label>' +
      '<select class="form-input" id="cw-aud" onchange="cwSet(\'audience\', this.value)">' + audiences + '</select></div>' +
      '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="cwPreview()">' + escHtml(t('cw.preview')) + '</button></div></div>' +
    '<label class="check-line small"><input type="checkbox"' + (w.rolling ? ' checked' : '') + ' onchange="cwSet(\'rolling\', this.checked)"> ' +
      escHtml(t('cw.rolling')) + '</label>' + (w.counts ? previewHtml(w.counts) : '') +
    '<div class="subhead">2. ' + escHtml(t('cw.step.offer')) + '</div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="cw-offer">' + escHtml(t('cw.offer')) + '</label>' +
      '<input class="form-input" id="cw-offer" value="' + escHtml(w.offer) + '" oninput="cwSet(\'offer\', this.value)"></div>' +
      '<div class="form-group"><label class="form-label" for="cw-goal">' + escHtml(t('cw.goal')) + '</label>' +
      '<select class="form-input" id="cw-goal" onchange="cwSet(\'goal\', this.value)">' + opt('reply', t('cw.goal.reply'), w.goal) +
        opt('meeting', t('cw.goal.meeting'), w.goal) + '</select></div>' +
      '<div class="form-group"><label class="form-label" for="cw-book">' + escHtml(t('cw.booking')) + '</label>' +
      '<input class="form-input" id="cw-book" value="' + escHtml(w.booking_link) + '" oninput="cwSet(\'booking_link\', this.value)"></div></div>' +
    '<div class="btn-row"><button class="btn btn-secondary btn-sm" onclick="cwGenerate()">' + escHtml(t('cw.generate')) + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="cwAddStep()"' + (w.steps.length >= 5 ? ' disabled' : '') + '>' + escHtml(t('cw.add_step')) + '</button></div>' +
    '<p class="small muted">' + escHtml(t('cw.merge_hint')) + '</p>' + steps +
    '<div class="subhead">3. ' + escHtml(t('cw.step.sending')) + '</div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="cw-acct">' + escHtml(t('cw.account')) + '</label>' +
      '<select class="form-input" id="cw-acct">' + accounts + '</select></div>' +
      '<div class="form-group"><label class="form-label" for="cw-profile">' + escHtml(t('cw.profile')) + '</label>' +
      '<select class="form-input" id="cw-profile" disabled>' + opt('', t('cw.profile_soon'), '') + '</select></div></div>' +
    '<label class="check-line small"><input type="checkbox"' + (w.review ? ' checked' : '') + ' onchange="cwSet(\'review\', this.checked)"> ' +
      escHtml(t('cw.review')) + '</label>' +
    '<label class="check-line small"><input type="checkbox"' + (w.risky && s && s.send_to_risky ? ' checked' : '') +
      (s && s.send_to_risky && (w.status || 'draft') === 'draft' ? '' : ' disabled') + ' onchange="cwSet(\'risky\', this.checked)"> ' + escHtml(t('cw.risky')) + '</label>' +
      (s && s.send_to_risky ? '' : '<p class="small muted">' + escHtml(t('cw.risky_setting_off')) + '</p>') +
    '<div class="subhead">4. ' + escHtml(t('cw.li_title')) + '</div>' +
    '<p class="small muted">' + escHtml(t('cw.li_help', {n: min})) + '</p>' +
    '<textarea class="form-input" rows="5" id="cw-li" oninput="cwSet(\'li_note\', this.value);document.getElementById(\'cw-li-n\').textContent=this.value.trim().length">' +
      escHtml(w.li_note) + '</textarea><p class="small muted"><span id="cw-li-n">' + w.li_note.trim().length + '</span> / ' + min + '</p>' +
    '<div class="subhead">5. ' + escHtml(t('cw.step.start')) + '</div>' +
    '<p class="small">' + escHtml(t(s && s.acknowledged ? 'cw.start_note' : 'cw.ack_needed')) + '</p>' +
    '<div class="btn-row"><button class="btn btn-secondary" onclick="cwSave()">' + escHtml(t('cw.save')) + '</button>' +
      '<button class="btn btn-primary" onclick="cwStart()">' + escHtml(t('cw.start')) + '</button></div></div>';
}

function cwBody() {
  const w = _cp.wiz;
  const body = {name: w.name, rolling: !!w.rolling, li_note: w.li_note, review: w.review ? 'always' : 'inherit',
                offer: {offer: w.offer, goal: w.goal, booking_link: w.booking_link}};
  // (A started campaign's is sent unchanged: it changes while a draft only.)
  body.send_to_risky = !!w.risky;
  if (w.audience === 'current') body.query = contactQuery() || 'kind=email';
  else body.audience_id = Number(w.audience);
  const steps = w.steps.filter(st => st.subject.trim() || st.body.trim());
  if (steps.length) body.sequence = steps;
  return body;
}

async function cwPreview() {
  const w = _cp.wiz;
  if (!w) return;
  const res = w.audience === 'current'
    ? await api('/api/audiences/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                           body: JSON.stringify({query: contactQuery() || 'kind=email', send_to_risky: !!w.risky})})
    : await api('/api/audiences/' + Number(w.audience) + '/preview?send_to_risky=' + (w.risky ? 'true' : 'false'));
  if (!res || !res.counts) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return; }
  w.counts = res.counts;
  renderCampaignWizard();
}

// Save the draft (made the first time); true when saved.
async function cwSave(quiet) {
  const w = _cp.wiz;
  if (!w || _cp.busy) return false;
  _cp.busy = true;
  try {
    const body = cwBody();
    const res = w.id
      ? await api('/api/campaigns/' + encodeURIComponent(w.id), {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                                                                body: JSON.stringify(body)})
      : await api('/api/campaigns', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!res || !res.success) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return false; }
    if (!w.id) w.id = res.id;
    if (!quiet) { showToast(t('cw.saved'), 'success'); loadCampaigns(); }
    return true;
  } finally {
    _cp.busy = false;
  }
}

async function cwGenerate() {
  const w = _cp.wiz;
  if (!w || !(await cwSave(true))) return;
  showToast(t('cw.generating'));
  const res = await api('/api/campaigns/' + encodeURIComponent(w.id) + '/templates', {method: 'POST'});
  if (!res || !res.success) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return; }
  w.steps = res.sequence;
  renderCampaignWizard();
}

async function cwStart() {
  const w = _cp.wiz;
  if (!w || !(await cwSave(true))) return;
  if (_cp.setup && !_cp.setup.acknowledged) {
    // Started once it is confirmed; closing the dialog starts nothing.
    openAckDialog(cwStart);
    return;
  }
  const res = await api('/api/campaigns/' + encodeURIComponent(w.id) + '/start', {method: 'POST'});
  if (!res || !res.success) { showToast(failMsg(res, t('camp.save_failed')), 'error'); return; }
  showToast(t('cw.started', {n: (res.counts || {}).added || 0}), 'success');
  _cp.open = w.id;
  _cp.wiz = null;
  loadCampaigns();
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

// A conversation's or a message's channel, as the dashboard names it.
function channelName(ch) {
  const k = 'ch.name.' + ch;
  return t(k) !== k ? t(k) : String(ch || '');
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
      (sent ? 'Harvey' : escHtml(first)) + (m.channel ? ' &middot; ' + escHtml(channelName(m.channel)) : '') +
      ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + escHtml(t('cv.drafted')) + '</div>' +
      '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">' + escHtml(t('cv.review')) + icon('arrow') + '</button></div>'
    : '';
  // An answer approved and not sent yet (held by the sending hours, a cap,
  // a pause): with why it waits.
  const w = c.waiting;
  const waiting = w ? convoWaiting(c, w) : '';
  const h = c.handle;
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || (h && h.value) || t('cv.unknown');
  const stageKey = 'cv.stage.' + c.stage;
  const stage = c.stage && t(stageKey) !== stageKey ? '<span class="tag">' + escHtml(t(stageKey)) + '</span>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email, h && h.value !== name ? h.value : ''].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (h || (c.channel && c.channel !== 'email') ? '<span class="tag">' + escHtml(channelName((h && h.channel) || c.channel)) + '</span>' : '') +
      (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
      stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted small">' + escHtml(t('cv.no_messages')) + '</p>') +
    draft + waiting + '</div>' + convoAnswer(c) + convoActions(c) + '</div>';
}

// An answer you approved that hasn't gone: with why it waits, and, until it
// is being sent (or a send of it was tried), Edit inline and Remove (then the
// answer box comes back).
const _cvEdit = {id: null};

function convoWaiting(c, w) {
  const open = w.status === 'approved' && !w.tried;
  const editing = open && _cvEdit.id === w.id;
  const why = w.status === 'approved' && w.error
    ? '<p class="muted small">' + escHtml(t('cv.waiting_why', {why: msgWhy(w.wait, w.error)})) + '</p>' : '';
  const body = editing
    ? '<textarea class="form-input" id="cv-edit-' + escHtml(w.id) + '" rows="3" maxlength="' +
        (MESSAGE_MAX[(c.handle || {}).channel] || 300) + '">' + escHtml(w.body) + '</textarea>' +
      '<p class="small" style="color:var(--bad)" id="cv-edit-why-' + escHtml(w.id) + '" hidden></p>'
    : '<div class="td-body">' + escHtml(w.body) + '</div>';
  const actions = !open ? '' : editing
    ? '<button class="btn btn-primary btn-sm" onclick="convoEditSave(' + jsq(w.id) + ')">' + escHtml(t('common.save')) + '</button> ' +
      '<button class="btn btn-secondary btn-sm" onclick="convoEdit(null)">' + escHtml(t('common.cancel')) + '</button>'
    : '<button class="btn btn-secondary btn-sm" onclick="convoEdit(' + jsq(w.id) + ')">' + escHtml(t('desk.edit')) + '</button> ' +
      '<button class="btn btn-secondary btn-sm" onclick="outboxRemove(' + jsq(w.id) + ', loadConversations)" title="' +
        escHtml(t('cv.remove_title')) + '">' + escHtml(t('desk.remove')) + '</button>';
  return '<div class="thread-draft"><div class="sender">' + icon('queue') + escHtml(t('cv.waiting')) + '</div>' +
    body + why + (actions ? '<div class="desk-actions">' + actions + '</div>' : '') + '</div>';
}

function convoEdit(id) {
  _cvEdit.id = id;
  loadConversations();
}

async function convoEditSave(id) {
  const el = document.getElementById('cv-edit-' + id);
  const body = String((el && el.value) || '').trim();
  if (!body) { showToast(t('desk.need_body'), 'error'); return; }
  const data = await saveOutboxEdit(id, '', body);
  if (data && data.success) {
    _cvEdit.id = null;
    showToast(t('desk.saved_approved'), 'success');
    loadConversations();
    return;
  }
  // Refused: said under the text, which stays as you wrote it.
  const why = editWhy(data);
  const box = document.getElementById('cv-edit-why-' + id);
  if (box) { box.textContent = why; box.hidden = false; }
  showToast(why, 'error');
}

// Why an SMS or WhatsApp answer can't go (now, or ever), in the viewer's
// language, from the code the server names; its own sentence otherwise.
function msgWhy(code, fallback, detail) {
  return code && hasKey('en', 'msg.why.' + code) ? t('msg.why.' + code, {detail: detail || ''}) : (fallback || '');
}

// An answer you write to an SMS or WhatsApp conversation: back to the number
// it came from, approved by you as you send it (no second approval in the
// Outbox), and sent when every check lets it.
function convoAnswer(c) {
  if (!c.can_answer || c.draft || c.waiting) return '';
  const ch = channelName((c.handle && c.handle.channel) || c.channel);
  const id = 'cv-answer-' + c.id;
  return '<div class="convo-answer"><label class="form-label" for="' + escHtml(id) + '">' + escHtml(t('cv.answer', {channel: ch})) + '</label>' +
    '<textarea class="form-input" id="' + escHtml(id) + '" rows="3" maxlength="' + (MESSAGE_MAX[(c.handle || {}).channel] || 300) +
      '" placeholder="' + escHtml(t('cv.answer_ph')) + '"></textarea>' +
    '<p class="muted small">' + escHtml(t('cv.answer_note')) + '</p>' +
    '<p class="small" style="color:var(--bad)" id="cv-answer-why-' + escHtml(c.id) + '" hidden></p>' +
    '<button class="btn btn-secondary btn-sm" onclick="answerConvo(' + jsq(c.id) + ')">' + escHtml(t('cv.answer_queue')) + '</button></div>';
}

async function answerConvo(id) {
  const el = document.getElementById('cv-answer-' + id);
  const body = String((el && el.value) || '').trim();
  if (!body) { showToast(t('desk.need_body'), 'error'); return; }
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/answer', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({body: body}),
  });
  if (data && data.success && data.status === 'sent') showToast(t('cv.answer_sent'), 'success');
  else if (data && data.success && data.wait === 'next_pass') showToast(t('cv.answer_queued'), 'success');
  else if (data && data.success) showToast(t('cv.answer_waits', {why: msgWhy(data.wait, data.why)}), 'success');
  else {
    // Refused: said in the answer box, and nothing was queued.
    let why = data && data.code ? msgWhy(data.code, data.message, data.detail) : failMsg(data, t('cv.answer_failed'));
    why = why.charAt(0).toUpperCase() + why.slice(1);
    const box = document.getElementById('cv-answer-why-' + id);
    if (box) { box.textContent = why; box.hidden = false; }
    showToast(why, 'error');
    return;
  }
  if (data.warning === 'window_closes') {
    // Queued as written, but it can only go once the channel's window has
    // closed: the provider will refuse it then. Said now, kept on the page.
    const at = s => new Date(s * 1000).toLocaleString(intlLocale(), {weekday: 'short', hour: '2-digit', minute: '2-digit'});
    showToast(t('cv.answer_late', {goes: at(data.goes_at), closes: at(data.closes_at)}), 'error');
  }
  loadConversations();
  loadToday();
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

// ── Chat: the chats with Harvey's agent, each answer streamed as it is written, and the plans it proposed ──

// The open chat ('' a new one, made by its first message), its messages,
// the answer being streamed here ({chat, turn, text, calls, end}) and the
// stream's AbortController: leaving a chat drops only this page's
// stream, the answer goes on and is followed again on the way back.
let _chatId = '';
let _chatData = null;
let _chatLive = null;
let _chatAbort = null;
let _chatList = null;
let _chatSearch = null;

async function loadChat() {
  await Promise.all([loadChatList(), loadChatMessages()]);
}

async function loadChatList() {
  const box = document.getElementById('chat-q');
  const q = box ? box.value.trim() : '';
  const [data, plans] = await Promise.all([api('/api/agent/chats?q=' + encodeURIComponent(q)),
                                           api('/api/agent/plans?limit=50')]);
  const el = document.getElementById('chat-chats');
  if (!data) { el.innerHTML = offlineLine(); return; }
  _chatList = data;
  const note = document.getElementById('chat-note');
  note.innerHTML = data.refused
    ? '<div class="banner wait">' + icon('alert') + '<div>' + escHtml(t('chat.refused_codex')) + '</div></div>'
    : data.provider === 'codex'
      ? '<p class="muted small">' + escHtml(t('chat.on_codex', {model: data.model || t('chat.model_default')})) + '</p>'
      : '';
  el.innerHTML = renderChatList(data.chats || [], _chatId, q);
  const side = document.getElementById('chat-plans');
  if (side) side.innerHTML = renderPlanList((plans && plans.plans) || [], data.chats || []);
}

// The plans still open (proposed, waiting for the user; approved, being
// worked on), newest first: each opens its chat.
function renderPlanList(list, chats) {
  const open = list.filter(p => p.status === 'proposed' || p.status === 'approved');
  if (!open.length) return '<p class="muted small">' + escHtml(t('chat.plans_none')) + '</p>';
  const titles = {};
  chats.forEach(c => { titles[c.id] = c.title; });
  return open.map(p => '<button type="button" class="chat-open chat-plan-item" data-chat="' + escHtml(p.chat) + '" onclick="openChat(this.dataset.chat)">' +
    '<span class="chat-item-title">' + escHtml(t('chat.plan.title', {id: p.id})) + ' · ' + escHtml(p.goal) + '</span>' +
    '<span class="chat-item-when">' + planBadge(p.status) + ' ' + escHtml(fmtUsd(p.budget_usd, 2)) +
      (titles[p.chat] ? ' · ' + escHtml(t('chat.plan.in_chat', {title: titles[p.chat]})) : '') + '</span></button>').join('');
}

const PLAN_TONE = {proposed: 'waiting', approved: 'active', rejected: 'idle', cancelled: 'idle',
                   expired: 'note', done: 'good'};

function planBadge(status) {
  return '<span class="badge t-' + (PLAN_TONE[status] || 'idle') + '">' + escHtml(t('chat.plan.st.' + status)) + '</span>';
}

// A plan the agent proposed, under the answer that proposed it: its goal,
// steps, budget and time; Approve and Reject while it waits; once
// approved, what its runs committed and spent and each run (to the Queue),
// with Cancel while it is open.
function renderPlanCard(p) {
  const steps = (p.steps || []).map(x => '<li>' + escHtml(x) + '</li>').join('');
  const budget = p.budget_usd > 0 ? t('chat.plan.budget', {budget: fmtUsd(p.budget_usd, 2)}) : t('chat.plan.budget_free');
  // (How long it stays open: while it waits, or once approved; a closed
  // plan says only its status.)
  const time = p.status === 'proposed' ? t('chat.plan.open_for', {n: p.expiry_hours})
    : p.status === 'approved' && p.expires_at ? t('chat.plan.until', {when: new Date(p.expires_at * 1000).toLocaleString(intlLocale())}) : '';
  let html = '<div class="plan-card" data-plan="' + escHtml(String(p.id)) + '">' +
    '<div class="plan-head"><b>' + escHtml(t('chat.plan.title', {id: p.id})) + '</b> ' + planBadge(p.status) + '</div>' +
    '<div class="plan-goal">' + escHtml(p.goal) + '</div>' +
    (steps ? '<ol class="plan-steps">' + steps + '</ol>' : '') +
    '<div class="muted small">' + escHtml(budget) + (time ? ' · ' + escHtml(time) : '') + '</div>';
  if (p.status !== 'proposed' && p.status !== 'rejected') {
    html += '<div class="small plan-money">' + escHtml(t('chat.plan.money', {committed: fmtUsd(p.committed_usd || 0, 2),
      spent: fmtUsd(p.spent_usd || 0, 2), budget: fmtUsd(p.budget_usd, 2), remaining: fmtUsd(p.remaining_usd || 0, 2)})) + '</div>';
    // (On an API key the agent's own model calls are money: their line, and
    // why a plan with no budget gets no wakes or tasks.)
    if (p.billed) html += '<div class="small muted plan-agent-usd">' + escHtml(t('chat.plan.agent_usd', {usd: fmtUsd(p.agent_usd || 0, 4)})) + '</div>';
    if (p.no_runs) html += '<div class="small plan-no-runs">' + escHtml(t('chat.plan.no_runs')) + '</div>';
    const runs = (p.runs || []).filter(r => r.batch_id);
    if (runs.length) {
      html += '<div class="plan-runs"><div class="muted small">' + escHtml(t('chat.plan.runs')) + '</div><ul>' + runs.map(r =>
        '<li><code>' + escHtml(r.tool) + '</code> ' + escHtml(r.label || '') + ' <span class="muted small">' +
        escHtml(t(r.live ? 'chat.plan.run_live' : 'chat.plan.run_done')) + ' · ' + escHtml(fmtUsd(r.spent_usd || 0, 4)) + '</span> ' +
        '<button type="button" class="btn btn-ghost btn-sm" data-batch="' + escHtml(r.batch_id) + '" onclick="openQueue(this.dataset.batch)">' +
        escHtml(t('chat.plan.run_open')) + '</button></li>').join('') + '</ul></div>';
    }
  }
  if (p.status === 'proposed') {
    html += '<div class="btn-row"><button type="button" class="btn btn-primary btn-sm" data-plan="' + escHtml(String(p.id)) + '" onclick="approvePlan(this.dataset.plan)">' +
      escHtml(t('chat.plan.approve')) + '</button><button type="button" class="btn btn-sm" data-plan="' + escHtml(String(p.id)) + '" onclick="decidePlan(this.dataset.plan, \'reject\')">' +
      escHtml(t('chat.plan.reject')) + '</button></div>';
  }
  html += planCampaigns(p.campaigns);
  html += planTasks(p);
  if (p.status === 'approved') {
    html += planWakes(p.wakes);
    html += '<div class="btn-row"><button type="button" class="btn btn-sm" data-plan="' + escHtml(String(p.id)) + '" onclick="decidePlan(this.dataset.plan, \'cancel\')">' +
      escHtml(t('chat.plan.cancel')) + '</button></div>';
  }
  return html + '</div>';
}

// The campaigns the agent made under a plan: a line each (status, members,
// sent, replies), and for a draft it asked the user to start, a card with
// what the Start would take (its audience counted, its sequence, its note,
// the sending account, what is still to do) and a button to the campaign's
// Start on the Campaigns page: the start itself stays there.
function planCampaigns(list) {
  if (!list || !list.length) return '';
  return '<div class="plan-campaigns"><div class="muted small">' + escHtml(t('chat.camp.title')) + '</div><ul>' + list.map(c =>
    '<li><b>' + escHtml(c.name || c.id) + '</b> <span class="badge t-' + escHtml(CAMPAIGN_TONE[c.status] || 'idle') + '">' + escHtml(t('chat.camp.st.' + c.status)) + '</span> ' +
      '<span class="muted small">' + escHtml(t('chat.camp.line', {members: fmtN(c.members || 0), sent: fmtN(c.sent || 0), replies: fmtN(c.replies || 0)})) +
      (c.needs_human ? ' · ' + escHtml(t('chat.camp.needs_you', {n: fmtN(c.needs_human)})) : '') + '</span>' +
      (c.request ? startRequestCard(c) : '') + '</li>').join('') + '</ul></div>';
}

const CAMPAIGN_TONE = {draft: 'waiting', active: 'active', paused: 'note', finished: 'good', archived: 'idle'};

function startRequestCard(c) {
  const r = c.request || {};
  const counts = r.preview || {};
  const todo = (r.to_do || []).map(k => '<li>' + escHtml(t('chat.camp.todo.' + k)) + '</li>').join('');
  const steps = (r.sequence || []).map(st => '<li><b>' + escHtml(st.subject || '') + '</b>' +
    (st.step > 1 ? ' <span class="muted small">' + escHtml(t('chat.camp.after_days', {n: st.delay_days || 0})) + '</span>' : '') +
    '<div class="start-body">' + escHtml(st.body || '') + '</div></li>').join('');
  const acct = r.account ? (r.account.address || '') + (r.account.provider ? ' (' + r.account.provider + ')' : '') : '';
  return '<div class="start-card"><div class="plan-head"><b>' + escHtml(t('chat.camp.request')) + '</b></div>' +
    (r.audience ? '<div class="small">' + escHtml(t('chat.camp.audience', {name: r.audience.name})) + '</div>' : '') +
    '<div class="small">' + escHtml(t('chat.camp.counts', {ready: fmtN(counts.ready || 0), risky: fmtN(counts.risky || 0),
      check: fmtN(counts.to_verify || 0), out: fmtN((counts.left_out || 0) + (counts.untrusted || 0) + (counts.suppressed || 0) +
      (counts.overlap || 0) + (counts.answered || 0) + (counts.risky_held || 0)), firms: fmtN(counts.firms || 0)})) + '</div>' +
    (acct ? '<div class="small">' + escHtml(t('chat.camp.account', {account: acct})) + '</div>' : '') +
    (r.rolling ? '<div class="small muted">' + escHtml(t('chat.camp.rolling')) + '</div>' : '') +
    (steps ? '<div class="muted small">' + escHtml(t('chat.camp.sequence')) + '</div><ol class="plan-steps">' + steps + '</ol>' : '') +
    '<div class="muted small">' + escHtml(t('chat.camp.note')) + '</div><div class="start-body small">' + escHtml(r.li_note || '') + '</div>' +
    (todo ? '<div class="muted small">' + escHtml(t('chat.camp.todo')) + '</div><ul>' + todo + '</ul>' : '') +
    '<div class="small">' + escHtml(t('chat.camp.check')) + '</div>' +
    '<div class="btn-row"><button type="button" class="btn btn-primary btn-sm" data-campaign="' + escHtml(c.id) + '" onclick="openCampaignStart(this.dataset.campaign)">' +
      escHtml(t('chat.camp.open')) + '</button></div></div>';
}

// The campaign's draft opened on the Campaigns page, its Start there (the
// acknowledgement first when it isn't given, then the start).
async function openCampaignStart(id) {
  _cp.tab = 'campaigns';
  goTab('campaigns');
  await editCampaign(id);
  const el = document.getElementById('campaign-wizard');
  if (el) el.scrollIntoView({block: 'start'});
}

// The tasks the agent handed to a child under a plan: a line each (its
// profile, status and a short result line), Cancel while one is queued or
// running, and the tokens they used together.
const TASK_TONE = {queued: 'waiting', running: 'active', done: 'good', invalid: 'note', failed: 'bad',
                   cancelled: 'idle'};

function planTasks(p) {
  const list = p.tasks || [];
  if (!list.length) return '';
  return '<div class="plan-tasks"><div class="muted small">' + escHtml(t('chat.task.title')) +
    (p.task_tokens ? ' · ' + escHtml(t('chat.task.tokens', {n: p.task_tokens})) : '') + '</div><ul>' + list.map(x =>
    '<li><b>' + escHtml(t('chat.task.profile.' + x.profile)) + '</b> #' + escHtml(String(x.task)) +
      (x.campaign ? ' <span class="muted small">' + escHtml(x.campaign) + '</span>' : '') +
      ' <span class="badge t-' + escHtml(TASK_TONE[x.status] || 'idle') + '">' + escHtml(t('chat.task.st.' + x.status)) + '</span>' +
      (taskLine(x) ? ' <span class="muted small task-line">' + escHtml(taskLine(x)) + '</span>' : '') +
      (x.status === 'queued' || x.status === 'running'
        ? ' <button type="button" class="btn btn-ghost btn-sm" data-task="' + escHtml(String(x.task)) + '" onclick="cancelTask(this.dataset.task)">' +
          escHtml(t('chat.task.cancel')) + '</button>' : '') + '</li>').join('') + '</ul></div>';
}

// A task's result in counts, in the page's words (a problem as Harvey wrote it).
function taskLine(x) {
  const c = x.counts || {};
  if (x.profile === 'researcher' && 'areas' in c) return t('chat.task.found', {areas: c.areas, phrases: c.phrases, sources: c.sources});
  if ('edits' in c) return (c.ok ? t('chat.task.check_ok') : t('chat.task.check_problems', {n: c.problems})) + ' · ' +
    t('chat.task.edits', {edits: c.edits, todo: c.to_do});
  // (A cancelled one's badge says it all; an invalid or failed one's problem
  // is Harvey's own diagnostic.)
  return x.status === 'cancelled' ? '' : (x.problem || '');
}

async function cancelTask(id) {
  const r = await api('/api/agent/tasks/' + encodeURIComponent(id) + '/cancel', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}'});
  planDone(r, 'chat.task.cancelled');
}

// An approved plan's wakes to come: each scheduled one with its time, its
// note and a Cancel; a wake queued now.
function planWakes(w) {
  if (!w) return '';
  const list = w.scheduled || [];
  if (!list.length && !w.job) return '';
  let html = '<div class="plan-wakes"><div class="muted small">' + escHtml(t('chat.plan.wakes')) + '</div>';
  if (w.job) html += '<div class="small">' + escHtml(t('chat.plan.wake_queued')) + '</div>';
  if (list.length) {
    html += '<ul>' + list.map(x => '<li>' + escHtml(new Date(x.due_at * 1000).toLocaleString(intlLocale())) +
      (x.note ? ' · <span class="muted">' + escHtml(x.note) + '</span>' : '') +
      ' <button type="button" class="btn btn-ghost btn-sm" data-wake="' + escHtml(String(x.id)) + '" onclick="cancelWake(this.dataset.wake)">' +
      escHtml(t('chat.plan.wake_cancel')) + '</button></li>').join('') + '</ul>';
  }
  return html + '</div>';
}

async function cancelWake(id) {
  const r = await api('/api/agent/wakes/' + encodeURIComponent(id) + '/cancel', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}'});
  planDone(r, 'chat.plan.wake_cancelled');
}

function chatPlan(id) {
  return ((_chatData && _chatData.plans) || []).find(p => String(p.id) === String(id));
}

// Approve: a confirmation naming the budget, which the request carries
// (the server approves only the budget the user saw).
async function approvePlan(id) {
  const p = chatPlan(id);
  if (!p || !confirm(t('chat.plan.approve_confirm', {id: p.id, budget: fmtUsd(p.budget_usd, 2)}))) return;
  const r = await api('/api/agent/plans/' + encodeURIComponent(p.id) + '/approve', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({budget_usd: p.budget_usd})});
  planDone(r, 'chat.plan.approved');
}

async function decidePlan(id, action) {
  const p = chatPlan(id);
  if (!p) return;
  if (action === 'cancel' && !confirm(t('chat.plan.cancel_confirm', {id: p.id}))) return;
  const r = await api('/api/agent/plans/' + encodeURIComponent(p.id) + '/' + action, {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}'});
  planDone(r, action === 'cancel' ? 'chat.plan.cancelled' : 'chat.plan.rejected');
}

function planDone(r, key) {
  if (r && r.success) showToast(t(key), 'success');
  else showToast((r && r.message) || t('chat.plan.failed'), 'error');
  loadChat();
}

function searchChats() {
  clearTimeout(_chatSearch);
  _chatSearch = setTimeout(loadChatList, 250);
}

// The chats, newest activity first: the open one marked, each with its
// rename and delete.
function renderChatList(list, current, q) {
  if (!list.length) {
    return '<p class="muted small">' + escHtml(t(q ? 'chat.none_found' : 'chat.none')) + '</p>';
  }
  return list.map(c => '<div class="chat-item' + (c.id === current ? ' on' : '') + '" data-chat="' + escHtml(c.id) + '">' +
    '<button type="button" class="chat-open" onclick="openChat(this.parentNode.dataset.chat)"' + (c.id === current ? ' aria-current="true"' : '') + '>' +
    '<span class="chat-item-title">' + escHtml(c.title || t('chat.untitled')) + '</span>' +
    '<span class="chat-item-when">' + (c.answering ? '<span class="badge t-active">' + escHtml(t('chat.answering')) + '</span> ' : '') +
      (c.unread && !c.answering ? '<span class="badge t-note">' + escHtml(t('chat.wake.unread')) + '</span> ' : '') + chatWhen(c.last_at) + '</span></button>' +
    '<button type="button" class="icon-btn" onclick="renameChat(this.parentNode.dataset.chat)" title="' + escHtml(t('chat.rename')) + '" aria-label="' + escHtml(t('chat.rename')) + '">' + icon('pen') + '</button>' +
    '<button type="button" class="icon-btn" onclick="deleteChat(this.parentNode.dataset.chat)" title="' + escHtml(t('chat.delete')) + '" aria-label="' + escHtml(t('chat.delete')) + '">' + icon('x') + '</button>' +
    '</div>').join('');
}

async function loadChatMessages() {
  const el = document.getElementById('chat-list');
  if (!_chatId) { _chatData = {messages: [], calls: {}, chat: null}; paintChat(); return; }
  const id = _chatId;
  const data = await api('/api/agent/chats/' + encodeURIComponent(id) + '/messages?limit=50');
  if (id !== _chatId) return;
  if (!data) {
    if (_lastFail === 404) { _chatId = ''; loadChat(); return; }
    el.innerHTML = offlineState();
    return;
  }
  _chatData = data;
  paintChat();
  if (data.live && (!_chatLive || _chatLive.turn !== data.live)) followChat(id, data.live);
}

// scroll: 'older' keeps the reader where they are (older messages added
// above); 'follow' (an answer as it is written) stays at the end only when
// the reader is there; otherwise the end is shown.
function paintChat(scroll) {
  const data = _chatData || {messages: []};
  const el = document.getElementById('chat-list');
  const live = _chatLive && _chatLive.chat === _chatId ? _chatLive : null;
  const top = el.scrollTop;
  const below = el.scrollHeight - top;
  const atEnd = below <= el.clientHeight + 48;
  el.innerHTML = renderChat(data, live);
  el.scrollTop = scroll === 'older' ? el.scrollHeight - below
    : (scroll === 'follow' && !atEnd ? top : el.scrollHeight);
  const busy = !!(live && !live.end) || !!data.busy;
  const refused = !!(_chatList && _chatList.refused);
  document.getElementById('chat-send').disabled = refused || busy;
  document.getElementById('chat-stop').hidden = !busy;
  document.getElementById('chat-title').textContent = data.chat ? (data.chat.title || t('chat.untitled')) : t('chat.new');
  document.getElementById('chat-tokens').innerHTML = chatContext(data.chat, live);
}

// How long the open chat is (its newest answer's context), and the hint to
// start a new chat past the server's threshold.
function chatContext(chat, live) {
  const ended = live && live.end && live.end.context !== undefined ? live.end : null;
  const n = ended && ended.context ? ended.context : (chat ? chat.context : 0);
  const hint = ended && ended.context ? !!ended.hint : !!(chat && chat.hint);
  if (!n) return '';
  return '<span class="muted small">' + escHtml(t('chat.context', {n: fmtN(n)})) + '</span>' +
    (hint ? ' <span class="badge t-note">' + escHtml(t('chat.hint_new')) + '</span>' : '');
}

function chatWhen(sec) {
  return sec ? relTime(new Date(Number(sec) * 1000).toISOString()) : '';
}

function renderChat(data, live) {
  const msgs = data.messages || [];
  if (!msgs.length) {
    return emptyState(icon('chat'), escHtml(t('chat.empty')), escHtml(t('chat.empty_copy')));
  }
  const calls = data.calls || {};
  const answered = new Set(msgs.filter(m => m.role === 'agent').map(m => m.turn));
  const last = msgs[msgs.length - 1].turn;
  // (Each plan under the answer that proposed it; one whose answer isn't
  // shown, older or still being written, after the messages.)
  const plans = data.plans || [];
  const shown = new Set();
  // (Older messages are on file: the chat is read 50 at a time.)
  let html = data.more ? '<button type="button" class="btn btn-sm chat-older" onclick="loadOlderChat()">' +
    escHtml(t('chat.older')) + '</button>' : '';
  for (const m of msgs) {
    if (m.role === 'user') {
      html += '<div class="chat-msg user"><div class="chat-meta">' + escHtml(t('chat.you')) + ' · ' + chatWhen(m.created_at) + '</div>' +
        '<div class="chat-text">' + escHtml(m.text) + '</div></div>';
      if (!answered.has(m.turn)) {
        html += live && live.turn === m.turn ? chatLiveHtml(live)
          : chatPending(m, calls[m.turn] || [], m.turn === last && m.status === 'stopped');
      }
      continue;
    }
    const err = String(m.status || '').startsWith('error:') ? m.status.slice(6) : '';
    // (An answer Harvey wrote on a wake: who woke it and why.)
    const who = m.wake ? t('chat.wake.by', {why: wakeLabel(m.wake)}) : t('chat.harvey');
    html += '<div class="chat-msg agent' + (err ? ' error' : '') + (m.wake ? ' wake' : '') + '"><div class="chat-meta">' + escHtml(who) + ' · ' + chatWhen(m.created_at) + '</div>' +
      (err ? '<div class="chat-text"><b>' + escHtml(t(own(CHAT_ERRORS, err) ? 'chat.err.' + err : 'chat.err.failed')) + '</b>' +
               (m.text ? '<div class="muted small">' + escHtml(m.text) + '</div>' : '') + '</div>'
           : '<div class="chat-text">' + escHtml(m.text) + '</div>') +
      chatCalls(calls[m.turn] || []) + (err && m.turn === last && !(live && !live.end) ? chatRetry(m.turn) : '') + '</div>';
    for (const p of plans) {
      if (p.turn === m.turn && !shown.has(p.id)) { shown.add(p.id); html += renderPlanCard(p); }
    }
  }
  for (const p of plans) if (!shown.has(p.id)) html += renderPlanCard(p);
  if (data.waking) html += '<div class="chat-msg agent pending"><div class="chat-text muted">' + escHtml(t('chat.waking')) + '</div></div>';
  return html;
}

// Why a wake woke, in the page's words: the runs that finished, a wake the
// agent scheduled, another module's event.
function wakeLabel(w) {
  const runs = (w && w.runs) || [];
  const kinds = (w && w.kinds) || [];
  const parts = [];
  if (runs.length) parts.push(t('chat.wake.run', {run: runs.slice(0, 3).join(', ')}));
  if (kinds.indexOf('scheduled') >= 0) parts.push(t('chat.wake.scheduled'));
  kinds.filter(k => k !== 'run_done' && k !== 'scheduled')
    .forEach(k => parts.push(['members', 'reply', 'campaign', 'task_done'].includes(k) ? t('chat.wake.kind.' + k)
      : t('chat.wake.event', {kind: String(k).replace(/_/g, ' ')})));
  return parts.join('; ') || t('chat.wake.woken');
}

const CHAT_ERRORS = {login: 1, max_turns: 1, failed: 1, timeout: 1, stopped: 1, budget: 1};
const CHAT_REFUSALS = {empty: 1, too_long: 1, codex: 1, budget: 1, busy: 1, elsewhere: 1, cli: 1,
                       full: 1, missing: 1, retry: 1, waking: 1};

// Send the chat's newest message again (its answer failed or was stopped).
function chatRetry(turn) {
  return '<div class="chat-retry"><button type="button" class="btn btn-sm" data-turn="' + escHtml(turn) + '" onclick="retryChat(this.dataset.turn)">' +
    icon('refresh') + escHtml(t('chat.retry')) + '</button></div>';
}

// A message with no answer that isn't being streamed here: still being
// answered (in another Harvey process), or stopped (the newest can be
// sent again).
function chatPending(m, list, retry) {
  const line = m.status === 'stopped' ? t('chat.stopped') : t('chat.thinking');
  return '<div class="chat-msg agent pending"><div class="chat-text muted">' + escHtml(line) + '</div>' + chatCalls(list) +
    (retry ? chatRetry(m.turn) : '') + '</div>';
}

// The answer as it is written: its text so far and each tool call, the
// ones still running marked.
function chatLiveHtml(live) {
  const calls = live.calls.map(c => ({tool: c.tool, arguments: c.arguments, ms: c.ms || 0,
                                      outcome: c.outcome || 'running', result: c.result || ''}));
  const text = live.text ? '<div class="chat-text">' + escHtml(live.text) + '</div>'
    : '<div class="chat-text muted">' + escHtml(t('chat.thinking')) + '</div>';
  return '<div class="chat-msg agent pending" id="chat-live">' + text +
    (calls.length ? chatCalls(calls, true) : '') + '</div>';
}

// The tool calls a turn made, folded (open while the answer is written):
// what the agent read to answer.
function chatCalls(list, open) {
  if (!list.length) return '';
  const tone = {ok: 'good', refused: 'note', running: 'active'};
  const rows = list.map(c => '<li><code>' + escHtml(c.tool) + '</code> ' +
    '<span class="badge t-' + (tone[c.outcome] || 'bad') + '">' +
    escHtml(t('chat.call.' + (own({ok: 1, refused: 1, error: 1, running: 1}, c.outcome) ? c.outcome : 'error'))) + '</span> ' +
    (c.outcome === 'running' ? '' : '<span class="muted small">' + escHtml(fmtN(c.ms || 0)) + ' ms</span>') +
    (c.arguments && Object.keys(c.arguments).length ? '<div class="chat-args"><code>' + escHtml(JSON.stringify(c.arguments)) + '</code></div>' : '') +
    (c.result ? '<div class="muted small">' + escHtml(c.result) + '</div>' : '') + '</li>').join('');
  return '<details class="chat-calls"' + (open ? ' open' : '') + '><summary>' + escHtml(t('chat.calls', {n: list.length})) + '</summary><ul>' + rows + '</ul></details>';
}

// The open chat's messages before the oldest shown, added above them.
async function loadOlderChat() {
  const id = _chatId;
  const msgs = (_chatData && _chatData.messages) || [];
  if (!id || !msgs.length) return;
  const data = await api('/api/agent/chats/' + encodeURIComponent(id) + '/messages?limit=50&before=' + msgs[0].id);
  if (!data || id !== _chatId || !_chatData) return;
  _chatData.messages = (data.messages || []).concat(_chatData.messages);
  _chatData.calls = Object.assign({}, data.calls || {}, _chatData.calls || {});
  _chatData.more = !!data.more;
  paintChat('older');
}

// One event of the stream into the live answer.
function chatEvent(live, ev) {
  if (ev.type === 'text') live.text += ev.text || '';
  else if (ev.type === 'tool_use') live.calls.push({tool: ev.tool, arguments: ev.arguments || {}});
  else if (ev.type === 'tool_result') {
    const c = live.calls.find(x => !x.outcome && x.tool === ev.tool) || live.calls.find(x => !x.outcome);
    if (c) Object.assign(c, {outcome: ev.outcome, ms: ev.ms, result: ev.result});
    else live.calls.push({tool: ev.tool, arguments: {}, outcome: ev.outcome, ms: ev.ms, result: ev.result});
  } else if (ev.type === 'done' || ev.type === 'error' || ev.type === 'gone') live.end = ev;
  return live;
}

// Read a chat's answer as server-sent events (fetch, so the token goes in
// the header: EventSource can't send it), from its first event, and paint
// each as it comes while that chat is open.
async function followChat(chatId, turn) {
  if (_chatAbort) _chatAbort.abort();
  const abort = new AbortController();
  _chatAbort = abort;
  const live = {chat: chatId, turn: turn, text: '', calls: [], end: null};
  _chatLive = live;
  paintChat();
  let r = null;
  try {
    r = await authFetch('/api/agent/chats/' + encodeURIComponent(chatId) + '/stream?turn=' + encodeURIComponent(turn),
                        {signal: abort.signal});
  } catch { r = null; }
  if (r && r.ok && r.body) {
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {stream: true});
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const line = buf.slice(0, i).split('\n').find(l => l.startsWith('data: '));
          buf = buf.slice(i + 2);
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch { continue; }
          chatEvent(live, ev);
          if (_chatLive === live && currentTab === 'chat') paintChat('follow');
        }
      }
    } catch { /* left for another chat, or the connection dropped: the messages say how it ended */ }
  }
  if (abort.signal.aborted) return;
  if (_chatLive === live) _chatLive = null;
  if (_chatAbort === abort) _chatAbort = null;
  if (live.end && live.end.type === 'error' && live.end.code === 'stopped') showToast(t('chat.stopped_toast'));
  if (currentTab === 'chat') {
    loadChatList();
    if (_chatId === chatId) loadChatMessages();
  }
}

// Leave the open chat's stream (its answer goes on on the server).
function leaveChat() {
  if (_chatAbort) _chatAbort.abort();
  _chatAbort = null;
  _chatLive = null;
}

function openChat(id) {
  if (id === _chatId) { toggleChats(false); return; }
  leaveChat();
  _chatId = id || '';
  _chatData = null;
  toggleChats(false);
  // (Opening it reads its wake answers.)
  const entry = _chatList && (_chatList.chats || []).find(c => c.id === _chatId);
  if (entry) entry.unread = 0;
  if (_chatList) document.getElementById('chat-chats').innerHTML =
    renderChatList(_chatList.chats || [], _chatId, (document.getElementById('chat-q').value || '').trim());
  document.getElementById('chat-list').innerHTML = '<p class="muted">' + escHtml(t('common.loading')) + '</p>';
  loadChatMessages();
}

function newChat() {
  openChat('');
  const box = document.getElementById('chat-input');
  if (box) box.focus();
}

// The list of chats on a narrow screen: shown or folded away.
function toggleChats(open) {
  const side = document.getElementById('chat-side');
  const btn = document.getElementById('chat-toggle');
  if (!side) return;
  const on = open === undefined ? !side.classList.contains('open') : !!open;
  side.classList.toggle('open', on);
  if (btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
}

async function renameChat(id) {
  const c = ((_chatList && _chatList.chats) || []).find(x => x.id === id);
  const title = prompt(t('chat.rename_prompt'), c ? c.title : '');
  if (title === null || !title.trim()) return;
  const r = await api('/api/agent/chats/' + encodeURIComponent(id), {method: 'PATCH',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: title})});
  if (!r || r.success === false) { chatRefused(r); return; }
  loadChatList();
  if (id === _chatId && _chatData) { _chatData.chat = Object.assign({}, _chatData.chat || {}, r.chat); paintChat(); }
}

async function deleteChat(id) {
  const c = ((_chatList && _chatList.chats) || []).find(x => x.id === id);
  if (!confirm(t('chat.delete_confirm', {title: (c && c.title) || t('chat.untitled')}))) return;
  const r = await api('/api/agent/chats/' + encodeURIComponent(id), {method: 'DELETE'});
  if (!r || r.success === false) { chatRefused(r); return; }
  showToast(t('chat.deleted'));
  if (id === _chatId) openChat('');
  loadChatList();
}

function chatRefused(r) {
  const code = r && own(CHAT_REFUSALS, r.code) ? r.code : '';
  // (The CLI's own words say what to do about it: a login, a version.)
  const detail = code === 'cli' && r.message ? ' ' + r.message : '';
  showToast(code ? t('chat.refused.' + code) + detail : ((r && r.message) || t('chat.send_failed')), 'error');
}

function chatKey(ev) {
  // Enter sends, Shift+Enter is a new line.
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); sendChat(ev); }
}

// What a message or a retry started: the chat it is in, open, its answer
// followed.
async function chatStarted(r) {
  if (_chatId !== r.chat) leaveChat();
  _chatId = r.chat;
  await loadChat();
  if (!_chatLive || _chatLive.turn !== r.turn) followChat(r.chat, r.turn);
}

async function sendChat(ev) {
  if (ev) ev.preventDefault();
  const box = document.getElementById('chat-input');
  const btn = document.getElementById('chat-send');
  const text = (box.value || '').trim();
  if (!text || btn.disabled) return;
  btn.disabled = true;
  const path = _chatId ? '/api/agent/chats/' + encodeURIComponent(_chatId) + '/messages' : '/api/agent/chats';
  const r = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify({text: text})});
  if (!r || r.success === false) { btn.disabled = false; chatRefused(r); return; }
  box.value = '';
  await chatStarted(r);
}

async function retryChat(turn) {
  if (!_chatId) return;
  const r = await api('/api/agent/chats/' + encodeURIComponent(_chatId) + '/retry', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({turn: turn})});
  if (!r || r.success === false) { chatRefused(r); return; }
  await chatStarted(r);
}

// While the Chat tab is open (the page's 15 s refresh): the list again (a
// wake's answer marks its chat), and the open chat when it changed and no
// answer is being followed here.
async function pollChat() {
  if (_chatLive) return;
  const before = _chatList && (_chatList.chats || []).find(c => c.id === _chatId);
  await loadChatList();
  if (!_chatId || _chatLive) return;
  const now = _chatList && (_chatList.chats || []).find(c => c.id === _chatId);
  if (now && (!before || now.last_at !== before.last_at || now.unread || now.answering !== before.answering ||
              (_chatData && _chatData.waking))) loadChatMessages();
}

// ── The agent's memory: its notes and playbooks, which the user sees and edits ──

let _memory = null;

function toggleMemory(open) {
  const el = document.getElementById('chat-memory');
  if (!el) return;
  const on = open === undefined ? el.hidden : !!open;
  el.hidden = !on;
  if (on) loadMemory();
}

async function loadMemory() {
  const el = document.getElementById('chat-memory-body');
  const data = await api('/api/agent/memory');
  if (!data) { el.innerHTML = offlineState(); return; }
  _memory = data;
  el.innerHTML = renderMemory(data);
}

function renderMemory(data) {
  const u = data.usage || {};
  const notes = data.notes || [];
  const books = data.playbooks || [];
  const by = a => t(a === 'agent' ? 'chat.memory.by_agent' : 'chat.memory.by_you');
  let html = '<p class="muted small">' + escHtml(t('chat.memory.lede')) + '</p>' +
    '<h4 class="chat-side-head">' + escHtml(t('chat.memory.notes')) + ' <span class="muted small">' +
    escHtml(t('chat.memory.used', {used: fmtN(u.chars || 0), limit: fmtN(u.limit || 0)})) + '</span></h4>';
  html += notes.length ? '<ul class="memory-list">' + notes.map(n => '<li data-note="' + escHtml(String(n.id)) + '">' +
    '<textarea class="form-input" rows="2" maxlength="' + escHtml(String(u.note_limit || 300)) + '" aria-label="' + escHtml(t('chat.memory.notes')) + '">' + escHtml(n.text) + '</textarea>' +
    '<div class="memory-row"><span class="muted small">' + escHtml(by(n.author)) + ' · ' + chatWhen(n.updated_at) + '</span>' +
    '<button type="button" class="btn btn-sm" onclick="saveNote(this.closest(\'li\').dataset.note)">' + escHtml(t('chat.memory.save')) + '</button>' +
    '<button type="button" class="btn btn-ghost btn-sm" onclick="deleteNote(this.closest(\'li\').dataset.note)">' + escHtml(t('chat.memory.delete')) + '</button></div></li>').join('') + '</ul>'
    : '<p class="muted small">' + escHtml(t('chat.memory.none_notes')) + '</p>';
  html += '<div class="memory-add"><textarea id="memory-new" class="form-input" rows="2" maxlength="' + escHtml(String(u.note_limit || 300)) + '" placeholder="' +
    escHtml(t('chat.memory.add_placeholder')) + '" aria-label="' + escHtml(t('chat.memory.add_placeholder')) + '"></textarea>' +
    '<button type="button" class="btn btn-sm" onclick="addNote()">' + escHtml(t('chat.memory.add')) + '</button></div>';
  html += '<h4 class="chat-side-head">' + escHtml(t('chat.memory.playbooks')) + '</h4>';
  html += books.length ? '<ul class="memory-list">' + books.map(p => '<li data-playbook="' + escHtml(String(p.id)) + '"><details><summary>' +
    escHtml(p.name) + ' <span class="muted small">' + escHtml(by(p.author)) + ' · ' + chatWhen(p.updated_at) + '</span></summary>' +
    '<textarea class="form-input" rows="8" maxlength="' + escHtml(String(data.playbook_limit || 4000)) + '" aria-label="' + escHtml(p.name) + '">' + escHtml(p.body) + '</textarea>' +
    '<div class="memory-row"><button type="button" class="btn btn-sm" onclick="savePlaybook(this.closest(\'li\').dataset.playbook)">' + escHtml(t('chat.memory.save')) + '</button>' +
    '<button type="button" class="btn btn-ghost btn-sm" onclick="deletePlaybook(this.closest(\'li\').dataset.playbook)">' + escHtml(t('chat.memory.delete')) + '</button></div></details></li>').join('') + '</ul>'
    : '<p class="muted small">' + escHtml(t('chat.memory.none_playbooks')) + '</p>';
  return html;
}

async function memoryDone(r) {
  if (r && r.success) showToast(t('chat.memory.saved'), 'success');
  else showToast((r && r.message) || t('chat.plan.failed'), 'error');
  loadMemory();
}

async function addNote() {
  const box = document.getElementById('memory-new');
  const text = (box && box.value || '').trim();
  if (!text) return;
  memoryDone(await api('/api/agent/memory/notes', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                    body: JSON.stringify({text: text})}));
}

async function saveNote(id) {
  const box = document.querySelector('#chat-memory li[data-note="' + id + '"] textarea');
  if (!box) return;
  memoryDone(await api('/api/agent/memory/notes/' + encodeURIComponent(id), {method: 'PATCH',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: box.value})}));
}

async function deleteNote(id) {
  if (!confirm(t('chat.memory.delete_confirm'))) return;
  memoryDone(await api('/api/agent/memory/notes/' + encodeURIComponent(id), {method: 'DELETE'}));
}

async function savePlaybook(id) {
  const box = document.querySelector('#chat-memory li[data-playbook="' + id + '"] textarea');
  if (!box) return;
  memoryDone(await api('/api/agent/memory/playbooks/' + encodeURIComponent(id), {method: 'PATCH',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({body: box.value})}));
}

async function deletePlaybook(id) {
  if (!confirm(t('chat.memory.delete_confirm'))) return;
  memoryDone(await api('/api/agent/memory/playbooks/' + encodeURIComponent(id), {method: 'DELETE'}));
}

async function stopChat() {
  if (!_chatId) return;
  const r = await api('/api/agent/chats/' + encodeURIComponent(_chatId) + '/stop', {method: 'POST'});
  if (!r) showToast(t('chat.send_failed'), 'error');
}

// ── Queue (Coadă): the background jobs, per lane ──

let _queueBatch = '';

const JOB_TONE = {queued: 'waiting', running: 'active', done: 'good', failed: 'bad',
                  cancelled: 'idle', paused: 'note'};

function jobBadge(status) {
  return '<span class="badge t-' + (JOB_TONE[status] || 'idle') + '">' +
    escHtml(t('q.st.' + status)) + '</span>';
}

function openQueue(batch) {
  _queueBatch = batch || '';
  const head = document.getElementById('queue-batch');
  if (head) head.innerHTML = '';
  goTab('queue');
}

function jobKind(kind) {
  const key = 'q.kind.' + kind;
  const said = t(key);
  return said === key ? String(kind || '').replace(/_/g, ' ') : said;
}

function jobRow(j, buttons) {
  const p = j.progress || {};
  const bits = [];
  if (p.pages) bits.push(t('q.pages', {n: fmtN(p.pages)}));
  if (p.results) bits.push(t('q.results', {n: fmtN(p.results)}));
  if (j.actual_cost) bits.push(t('q.cost', {cost: fmtUsd(j.actual_cost, 4)}));
  // (The classifier's part of it, and how long a finished job took.)
  if (p.classify && p.classify.calls) bits.push(t('q.classify', {n: fmtN(p.classify.calls), usd: fmtUsd(p.classify.usd || 0, 4)}));
  const ck = (j.result && j.result.spend) || {};
  if (ck.checks_made) bits.push(t('q.checks', {n: fmtN(ck.checks_made), usd: fmtUsd(ck.checks || 0, 4)}) +
    (ck.checks_refused ? ' ' + t('run.checks_refused', {n: fmtN(ck.checks_refused)}) : ''));
  if (j.result && j.result.ms) bits.push(t('q.took', {n: fmtN(Math.max(1, Math.round(j.result.ms / 1000)))}));
  if (j.status === 'paused') bits.push(t('q.paused_why', {why: j.paused_reason === 'spend limit'
    ? t('q.why.spend') : j.paused_reason === 'paused' ? t('q.why.hand') : (j.paused_reason || j.error || '')}));
  if (j.status === 'queued' && j.error) bits.push(t('q.retry_at', {why: j.error}));
  if (j.status === 'failed' && j.error) bits.push(j.error);
  if (j.status === 'running' && j.last_line) bits.push(j.last_line.trim());
  const act = (buttons || []).map(([action, label]) =>
    '<button type="button" class="btn btn-secondary btn-sm" onclick="jobAction(' + jsq(j.id) + ',' +
      jsq(action) + ')">' + escHtml(t(label)) + '</button>').join('');
  return '<div class="job-row">' +
    '<div class="job-main"><div class="job-title">' + jobBadge(j.status) + ' ' +
      '<span class="job-kind">' + escHtml(jobKind(j.kind)) + '</span> ' + escHtml(j.label || '') + '</div>' +
      (bits.length ? '<div class="job-detail">' + bits.map(escHtml).join(' &middot; ') + '</div>' : '') +
    '</div>' + (act ? '<div class="job-actions">' + act + '</div>' : '') + '</div>';
}

// A lane is a module's (or the contact search's): its name in the viewer's
// language when the page knows it, else the module's own.
function laneName(l) {
  return own({contacts: 1, counts: 1}, l.lane) ? tServer('q.lane.' + l.lane, l.name)
                                    : tServer('module.' + l.lane + '.name', l.name);
}

function laneCard(l) {
  const block = (title, list, buttons, more) => list.length
    ? '<div class="job-group"><div class="job-group-title">' + escHtml(t(title)) + '</div>' +
        list.map(j => jobRow(j, typeof buttons === 'function' ? buttons(j) : buttons)).join('') +
        (more ? '<div class="muted small">' + escHtml(t('q.more', {n: fmtN(more)})) + '</div>' : '') +
      '</div>' : '';
  const toggle = l.paused
    ? '<button type="button" class="btn btn-primary btn-sm" onclick="laneAction(' + jsq(l.lane) + ',\'resume\')">' + escHtml(t('q.resume_lane')) + '</button>'
    : '<button type="button" class="btn btn-secondary btn-sm" onclick="laneAction(' + jsq(l.lane) + ',\'pause\')">' + escHtml(t('q.pause_lane')) + '</button>';
  return '<div class="card lane-card">' +
    '<div class="lane-head"><h3>' + escHtml(laneName(l)) + ' <span class="sig-code">' + escHtml(l.lane) + '</span></h3>' + toggle + '</div>' +
    (l.paused ? '<div class="test-result error">' + escHtml(t('q.lane_paused', {why: l.paused === 'paused' ? t('q.why.hand') : l.paused})) + '</div>' : '') +
    // Only a search stops part-way (after its page); a load or a check
    // runs to its end, so it offers no Cancel or Pause while it runs.
    block('q.running', l.running, j => j.stoppable ? [['cancel', 'q.cancel'], ['pause', 'q.pause']] : []) +
    block('q.waiting', l.waiting, [['cancel', 'q.cancel']], l.waiting_more) +
    block('q.paused', l.paused_jobs, [['resume', 'q.resume'], ['cancel', 'q.cancel']]) +
    block('q.failed', l.failed, [['retry', 'q.retry']]) +
    block('q.done', l.done, []) +
    (!l.running.length && !l.waiting.length && !l.paused_jobs.length && !l.failed.length && !l.done.length
      ? '<p class="muted small">' + escHtml(t('q.lane_empty')) + '</p>' : '') +
  '</div>';
}

async function loadQueue() {
  const el = document.getElementById('queue-list');
  const head = document.getElementById('queue-batch');
  if (!el) return;
  const data = await api('/api/jobs' + (_queueBatch ? '?batch=' + encodeURIComponent(_queueBatch) : ''));
  if (!data || data.error) { el.innerHTML = offlineState(); return; }
  navCount('nav-queue', (data.running || 0) + (data.waiting || 0));
  const batch = data.batch;
  // The 5-second refresh leaves the cap field alone while someone types in it.
  const capInput = document.getElementById('queue-cap') || document.getElementById('queue-ccap');
  const typing = capInput && (document.activeElement === capInput || capInput.dataset.edited === '1');
  // (A run's classifier cap: its own, beside the searches' one.)
  const ccap = batch && batch.params ? batch.params.classify_max_spend : undefined;
  if (!typing) head.innerHTML = (data.worker ? '' : '<div class="test-result error">' + escHtml(t('q.worker_off')) + '</div>') +
    (_queueBatch ? '<div class="card"><p class="desc">' + escHtml(t('q.batch', {label: (batch && batch.label) || ''})) +
      ' <button type="button" class="btn btn-secondary btn-sm" onclick="openQueue(\'\')">' + escHtml(t('q.show_all')) + '</button></p>' +
      (data.report ? runReportHtml(data.report) : '') +
      (batch && batch.max_spend !== null && batch.max_spend !== undefined
        ? '<div class="form-row"><label class="form-label" for="queue-cap">' + escHtml(t('q.cap')) + '</label>' +
          '<input class="form-input" id="queue-cap" type="number" min="0" step="0.25" value="' + escHtml(String(batch.max_spend)) + '" oninput="this.dataset.edited=\'1\'">' +
          '<button type="button" class="btn btn-primary btn-sm" onclick="raiseCap(' + jsq(_queueBatch) + ')">' + escHtml(t('q.raise')) + '</button></div>'
        : '') +
      (ccap !== null && ccap !== undefined
        ? '<div class="form-row"><label class="form-label" for="queue-ccap">' + escHtml(t('q.ccap')) + '</label>' +
          '<input class="form-input" id="queue-ccap" type="number" min="0" step="0.01" value="' + escHtml(String(ccap)) + '" oninput="this.dataset.edited=\'1\'">' +
          '<button type="button" class="btn btn-primary btn-sm" onclick="raiseCap(' + jsq(_queueBatch) + ', true)">' + escHtml(t('q.raise')) + '</button></div>'
        : '') + '</div>' : '');
  if (!(data.lanes || []).length) {
    el.innerHTML = emptyState(icon('queue'), escHtml(t('q.empty')), escHtml(t('q.empty_copy')), true);
    return;
  }
  el.innerHTML = data.lanes.map(laneCard).join('');
}

async function jobAction(id, action) {
  const res = await api('/api/jobs/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  showToast(res && res.success ? t('q.ok') : failMsg(res, t('q.failed_toast')),
            res && res.success ? 'success' : 'error');
  loadQueue();
}

async function laneAction(lane, action) {
  const res = await api('/api/lanes/' + encodeURIComponent(lane) + '/' + action, {method: 'POST'});
  showToast(res && res.success ? t('q.ok') : failMsg(res, t('q.failed_toast')),
            res && res.success ? 'success' : 'error');
  loadQueue();
}

async function raiseCap(batch, classifier) {
  const input = document.getElementById(classifier ? 'queue-ccap' : 'queue-cap');
  const cap = Number((input || {}).value);
  const res = await api('/api/jobs/batches/' + encodeURIComponent(batch) + '/cap', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(classifier ? {classify_max_spend: cap} : {max_spend: cap}),
  });
  showToast(res && res.success ? t('q.raised', {n: res.resumed}) : failMsg(res, t('q.failed_toast')),
            res && res.success ? 'success' : 'error');
  if (input && res && res.success) { delete input.dataset.edited; input.blur(); }
  loadQueue();
}

async function loadQueueCount() {
  const data = await api('/api/jobs/counts');
  if (data) navCount('nav-queue', (data.running || 0) + (data.waiting || 0));
}

// ── Init & live refresh ──

// ── Database upgrade: shown instead of the pages while harvey.db is the old schema ──
//
// /api/upgrade says whether harvey.db needs the upgrade, whether one is
// running, each stage and the verification. Every other API call answers
// 409 until the switch, so no page is drawn under this screen.

let _upgrade = null, _upgradeTimer = null, _upgradeShown = false;

function gb(n) { return fmtN(Math.round((n || 0) / 1e8) / 10) + ' GB'; }

async function checkUpgrade() {
  const s = await api('/api/upgrade');
  if (!s) return false;
  _upgrade = s;
  renderOldFiles(s);
  if (s.needed || s.running || (_upgradeShown && s.switched)) {
    showUpgrade(s);
    return true;
  }
  return false;
}

function showUpgrade(s) {
  _upgradeShown = true;
  const el = document.getElementById('upgrade-screen');
  if (!el) return;
  el.style.display = 'flex';
  document.getElementById('upgrade-body').innerHTML = upgradeHtml(s);
  clearTimeout(_upgradeTimer);
  if (s.running || s.needed) _upgradeTimer = setTimeout(checkUpgrade, s.running ? 2000 : 15000);
}

function upgradeStage(st) {
  const p = st.progress || {};
  let extra = '';
  if (st.state === 'running') {
    if (p.step) extra = t('upgrade.step', {step: escHtml(p.step), at: p.at || '?', of: p.of || '?'});
    else if (p.section) extra = t('upgrade.section', {section: escHtml(p.section), at: p.at, of: p.of});
    else if (p.rows) extra = t('upgrade.rows', {n: p.rows});
  } else if (st.seconds) {
    extra = t('upgrade.took', {n: Math.max(1, Math.round(st.seconds))});
  }
  return '<li class="upgrade-stage ' + escHtml(st.state || 'todo') + '"><span>' +
    escHtml(t('upgrade.stage.' + st.id)) + '</span><span class="soft">' +
    escHtml(t('upgrade.state.' + (st.state || 'todo'))) + (extra ? ' · ' + extra : '') +
    '</span></li>';
}

// A verification count's name in the page's language (the server's own
// name when this release has no translation of it).
function upgradeCountLabel(k) {
  const label = t('upgrade.count.' + k);
  return label === 'upgrade.count.' + k ? k : label;
}

function upgradeVerification(v) {
  if (!v) return '';
  let h = '<h3>' + escHtml(v.passed ? t('upgrade.verified') : t('upgrade.not_verified')) + '</h3>';
  const rows = Object.entries(v.counts || {});
  if (rows.length) {
    h += '<table class="data compact"><thead><tr><th></th><th>' + escHtml(t('upgrade.old')) +
      '</th><th>' + escHtml(t('upgrade.new')) + '</th></tr></thead><tbody>' +
      rows.map(([k, r]) => '<tr><td>' + escHtml(upgradeCountLabel(k)) + '</td><td>' + fmtN(r.old ?? 0) +
        '</td><td>' + fmtN(r.new ?? 0) + '</td></tr>').join('') + '</tbody></table>';
  }
  const parts = ['stored', 'registers', 'filters'].filter(k => v[k]);
  if (parts.length) {
    h += '<p class="soft">' + parts.map(k => escHtml(t('upgrade.checks.' + k,
      {passed: v[k].passed, n: v[k].checked}))).join(' · ') + '</p>';
  }
  if (v.failures && v.failures.length) {
    h += '<ul class="soft">' + v.failures.slice(0, 20).map(f => '<li>' + escHtml(f) + '</li>').join('') + '</ul>';
  }
  return h;
}

function upgradeHtml(s) {
  const e = s.estimate || {}, sizes = e.sizes || {};
  let h = '<h2>' + escHtml(t('upgrade.title')) + '</h2>';
  if (s.switched && !s.needed) {
    h += '<p class="lede">' + escHtml(t('upgrade.done')) + '</p>' + upgradeVerification(s.verification);
    h += '<button class="btn btn-primary" onclick="location.reload()">' + escHtml(t('upgrade.open')) + '</button>';
    return h;
  }
  if (s.switch_pending) h += '<p class="banner wait">' + escHtml(t('upgrade.switch_pending')) + '</p>';
  h += '<p class="lede">' + t('upgrade.what') + '</p>';
  h += '<p class="lede">' + t('upgrade.steps') + '</p>';
  if (e.sizes) {
    h += '<table class="data compact"><tbody>' +
      '<tr><td>' + escHtml(t('upgrade.sizes')) + '</td><td>harvey.db ' + gb(sizes.harvey) +
      (sizes.anaf ? ', anaf.db ' + gb(sizes.anaf) : '') + '</td></tr>' +
      '<tr><td>' + escHtml(t('upgrade.time')) + '</td><td>' +
      escHtml(t('upgrade.minutes', {n: Math.max(1, Math.round((e.seconds || 0) / 60))})) + '</td></tr>' +
      '<tr><td>' + escHtml(t('upgrade.need')) + '</td><td>' +
      escHtml(t('upgrade.need_value', {need: gb(e.need_bytes), floor: gb(e.floor_bytes)})) + '</td></tr>' +
      '<tr><td>' + escHtml(t('upgrade.free')) + '</td><td>' +
      (e.free_bytes != null ? gb(e.free_bytes) : escHtml(t('upgrade.free_unknown'))) + '</td></tr>' +
      '</tbody></table>';
  }
  if (e.free_error) h += '<p class="banner wait">' + t('upgrade.free_error') + '</p>';
  else if (e.free_bytes != null && e.free_bytes < e.floor_bytes) {
    h += '<p class="banner wait">' + escHtml(t('upgrade.too_little', {floor: gb(e.floor_bytes)})) + '</p>';
  }
  const started = s.stages.some(st => st.state && st.state !== 'todo');
  if (started || s.running) h += '<ol class="upgrade-stages">' + s.stages.map(upgradeStage).join('') + '</ol>';
  // (A failed verification says so below, in the page's language, with
  // what differed: the server's English summary of it isn't repeated.)
  const unverified = s.verification && s.verification.passed === false;
  if (s.error && !s.running && !unverified) {
    h += '<p class="banner wait">' + escHtml(t('upgrade.stopped', {why: s.error})) + '</p>';
  }
  h += upgradeVerification(s.verification);
  if (s.running) {
    h += '<p class="soft">' + escHtml(t('upgrade.running')) + '</p>';
  } else {
    const blocked = !s.switch_pending && (!!e.free_error || (e.free_bytes != null && e.free_bytes < e.floor_bytes));
    h += '<button class="btn btn-primary" id="upgrade-start" onclick="startUpgrade()"' +
      (blocked ? ' disabled' : '') + '>' + escHtml(t(started ? 'upgrade.resume' : 'upgrade.start')) + '</button>';
  }
  return h;
}

async function startUpgrade() {
  const btn = document.getElementById('upgrade-start');
  if (btn) btn.disabled = true;
  const r = await api('/api/upgrade/start', {method: 'POST'});
  if (r && r.success === false) showToast(r.message || r.detail || t('upgrade.not_started'), 'error');
  setTimeout(checkUpgrade, 800);
}

function renderOldFiles(s) {
  const el = document.getElementById('old-files-note');
  if (!el) return;
  const files = (s && !s.needed && s.old_files) || [];
  if (!files.length) { el.hidden = true; return; }
  const total = files.reduce((a, f) => a + f.bytes, 0);
  el.hidden = false;
  el.innerHTML = '<span>' + escHtml(t('upgrade.old_files', {names: files.map(f => f.name).join(', '), size: gb(total)})) +
    '</span> <button class="btn btn-sm" onclick="deleteOldFiles()">' + escHtml(t('upgrade.delete')) + '</button>';
}

async function deleteOldFiles() {
  const word = t('upgrade.delete_word');
  const typed = prompt(t('upgrade.delete_prompt', {word: word}));
  if (typed == null) return;
  if (typed.trim().toLowerCase() !== word) { showToast(t('upgrade.delete_mismatch', {word: word}), 'error'); return; }
  const r = await api('/api/upgrade/delete-old', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: typed.trim().toLowerCase()})});
  if (r && r.success !== false) showToast(t('upgrade.deleted', {size: gb(r.freed)}), 'success');
  else showToast((r && (r.message || r.detail)) || t('upgrade.not_deleted'), 'error');
  checkUpgrade();
}

async function init() {
  applyI18n();
  switchLangButtons();
  applyTheme(currentTheme());
  _co.limit = _ct.limit = loadPageSize();
  if (await checkUpgrade()) return;
  await loadModules();
  let tab = 'overview';
  try { tab = sessionStorage.getItem('harvey-tab') || 'overview'; } catch { /* ignore */ }
  goTab(tab);
  loadHarveyStatus();
  loadQueueCount();
}

function switchLangButtons() {
  document.querySelectorAll('.lang-switch button').forEach(b => {
    const on = b.dataset.lang === getLang();
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

init();

// Agent status: quick poll (not while locked, nor under the upgrade screen:
// it would only be refused)
setInterval(() => { if (!_locked && !_upgradeShown) loadHarveyStatus(); }, 8000);

// The Queue badge (running + waiting) stays live from any tab; the Queue
// page itself refreshes faster while it is open.
setInterval(() => {
  if (document.hidden || _locked || _upgradeShown) return;
  if (currentTab === 'queue') loadQueue(); else loadQueueCount();
}, 5000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && !_upgradeShown && currentTab === 'today') loadLiveStatus();
}, 10000);

// Email-module nav counts stay live wherever you are, so "something needs
// me" is visible from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || _locked || _upgradeShown || !_modules.messages || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', todayItems(data).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 20000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden || _locked || _upgradeShown || document.querySelector('.menu.open')) return;
  switch (currentTab) {
    case 'overview': loadOverview(); break;
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadJobRuns(); loadTodayActivity(); break;
    // A page cut from a kept ordering isn't refetched: the ordering is the costly part.
    case 'companies': if (_co.pagedBy !== 'snapshot') loadCompanies(); break;
    // The contacts list isn't refetched (its count is a big query): only
    // the outreach list, which the Email loop changes as it goes.
    case 'prospects': if (_cv.view === 'outreach' && !_optOut.id) loadOutreach(); break;
    case 'campaigns': loadCampaigns(); break;
    // (Not while an answer is being edited there: what's typed would go.)
    case 'conversations': if (!_cvEdit.id) loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); loadControlSending(); break;
    // (A wake's answer lands without a stream: the list and the open chat
    // are read again.)
    case 'chat': pollChat(); break;
  }
}, 15000);
