"""Modules on and off: the vocabulary comes from the manifests of the modules
that are on, a module that is off is refused by name (never skipped in
silence), and nothing is deleted when one goes off. All data is fictional."""

import asyncio
import contextlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.state as state_module
from harvey.collectors import discover as D
from harvey.models import Company
from harvey.modules import switches
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import MIGRATIONS, StateManager

WEB = Path(dash.__file__).resolve().parent / "web"
CATALOG = {s["code"]: s for s in SIGNAL_CATALOG}
OLD_COLUMNS = ("code", "label", "description", "category", "value_type", "collector",
               "cost_note", "confidence_floor", "status")

# What a new install has on. Core can't go off; the Romanian set is on;
# the original Harvey's general search and verification sources are on
# (they serve Romania too); its US-startup, hiring, website-marketing,
# keyless-scraping, LinkedIn and email-campaign modules are off.
DEFAULT_ON = {
    "export", "lang_en", "nominatim", "people", "phones", "website",
    "dataforseo", "email_verify", "hunter", "market_us", "osm", "scavio", "serper", "tinyfish",
    "lang_ro", "market_ro", "onrc",
}
DEFAULT_OFF = {"email_outreach", "hiring", "linkedin", "startups", "web_marketing",
               "web_search_scrape"}


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "harvey.db"))
    await sm.init_db()
    return sm


async def _rows(sm, sql, params=()):
    import aiosqlite
    async with aiosqlite.connect(sm.db_path) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


# ── Defaults ──


def test_the_default_table():
    on = switches.resolve({})
    assert on == DEFAULT_ON
    assert {m.id for m in switches.manifests()} == DEFAULT_ON | DEFAULT_OFF


def test_core_modules_are_on_even_if_a_setting_says_off():
    on = switches.resolve({switches.KEY.format("website"): switches.OFF})
    assert "website" in on and "people" in on


def test_a_module_whose_requirement_is_off_is_off():
    on = switches.resolve({switches.KEY.format("market_ro"): switches.OFF})
    assert "market_ro" not in on and "onrc" not in on


# ── The vocabulary from the manifests ──


@pytest.mark.asyncio
async def test_migration_adds_the_vocabulary_columns(state):
    assert (await _rows(state, "PRAGMA user_version"))[0][0] == len(MIGRATIONS) == 18
    cols = {r[1] for r in await _rows(state, "PRAGMA table_info(signal_codes)")}
    assert {"module", "subject", "unit", "cardinality", "keyed_by", "absence",
            "when_known", "available"} <= cols


@pytest.mark.asyncio
async def test_parity_old_catalog_rows_equal_the_new_ones(state, tmp_path):
    """The rows the old catalog seeding wrote are the rows the manifests write."""
    old = StateManager(str(tmp_path / "old.db"))
    await old.init_db()
    for sig in SIGNAL_CATALOG:   # what seeding from the catalog did
        await old.upsert_signal_code(
            sig["code"], label=sig["label"], description=sig["description"],
            category=sig["category"], value_type=sig["value_type"],
            collector=sig["collector"], cost_note=sig["cost_note"],
            confidence_floor=sig.get("confidence_floor", 0.0))
    assert await sync_vocabulary(state) == len(SIGNAL_CATALOG)
    sql = f"SELECT {', '.join(OLD_COLUMNS)} FROM signal_codes ORDER BY code"
    assert await _rows(state, sql) == await _rows(old, sql)


@pytest.mark.asyncio
async def test_each_signal_carries_its_module_and_how_it_reads(state):
    await sync_vocabulary(state)
    rows = {r["code"]: r for r in await state.get_signal_codes()}
    owners = {sig["code"]: m.id for m in switches.manifests() for sig in m.signals}
    assert {c: r["module"] for c, r in rows.items()} == owners
    assert {c for c, r in rows.items() if r["keyed_by"] == "phrase"} == set(D.RANK_CODES)
    assert rows["ACTIVITY_CODE"]["when_known"] == "source"
    assert rows["MAPS_RANK"]["when_known"] == "search"
    assert rows["PHONE_NUMBER"]["cardinality"] == "many"
    assert {r["subject"] for r in rows.values()} == {"company"}
    assert {r["absence"] for r in rows.values()} == {"unknown"}


@pytest.mark.asyncio
async def test_signals_of_modules_that_are_off_are_unavailable(state):
    await sync_vocabulary(state)
    rows = {r["code"]: r for r in await state.get_signal_codes()}
    off = {c for c, r in rows.items() if not r["available"]}
    assert off == {sig["code"] for m in switches.manifests()
                   if m.id in {"web_marketing", "startups", "hiring"} for sig in m.signals}
    shown = {r["code"] for r in await state.get_signal_codes(available_only=True)}
    assert shown == set(rows) - off


@pytest.mark.asyncio
async def test_a_signal_another_enabled_module_produces_stays(state):
    """FOUND_IN_SERP is declared by dataforseo, and OSM writes it too."""
    await switches.set_enabled(state, "dataforseo", False)
    rows = {r["code"]: r for r in await state.get_signal_codes()}
    assert rows["FOUND_IN_SERP"]["available"] == 1 and rows["NO_WEBSITE"]["available"] == 1
    assert rows["MAPS_RANK"]["available"] == 0 and rows["SERP_RANK"]["available"] == 1  # Serper


@pytest.mark.asyncio
async def test_off_hides_keeps_data_and_the_users_decision(state):
    await switches.set_enabled(state, "web_marketing", True)
    await state.set_signal_status("TECH_STACK", "confirmed")
    await state.set_signal_status("RUNNING_GOOGLE_ADS", "rejected")
    cid = await state.add_company(Company(name="Exemplu SRL", domain="exemplu.example"))
    await state.add_observation("TECH_STACK", company_id=cid, value_text="WordPress")
    assert "TECH_STACK" in await state.confirmed_signal_codes()

    await switches.set_enabled(state, "web_marketing", False)
    assert "TECH_STACK" not in await state.confirmed_signal_codes()   # never collected
    assert len(await state.get_observations(company_id=cid, signal_code="TECH_STACK")) == 1
    # The vocabulary still knows the code; an invented one is still refused.
    await state.add_observation("TECH_STACK", company_id=cid, value_text="Joomla")
    with pytest.raises(Exception, match="unknown signal_code"):
        await state.add_observation("NOT_A_SIGNAL", company_id=cid)

    await switches.set_enabled(state, "web_marketing", True)
    status = {r["code"]: r["status"] for r in await state.get_signal_codes()}
    assert (status["TECH_STACK"], status["RUNNING_GOOGLE_ADS"]) == ("confirmed", "rejected")


# ── Turning modules on and off ──


@pytest.mark.asyncio
async def test_switches_are_stored_per_install(state):
    assert await switches.set_enabled(state, "startups", True) >= {"startups"}
    assert await state.get_setting("modules.startups.enabled") == "on"
    on = await switches.set_enabled(state, "osm", False)
    assert "osm" not in on
    assert await state.get_setting("modules.osm.enabled") == "off"


@pytest.mark.asyncio
async def test_a_core_module_cant_be_turned_off(state):
    with pytest.raises(switches.SwitchRefused, match="core"):
        await switches.set_enabled(state, "website", False)


@pytest.mark.asyncio
async def test_enabling_needs_the_requirements_on_and_enables_nothing_else(state):
    await switches.set_enabled(state, "onrc", False)
    await switches.set_enabled(state, "market_ro", False)
    with pytest.raises(switches.SwitchRefused, match=r"market_ro"):
        await switches.set_enabled(state, "onrc", True)
    assert "market_ro" not in await switches.enabled_ids(state)


@pytest.mark.asyncio
async def test_disabling_a_required_module_names_who_needs_it(state):
    with pytest.raises(switches.SwitchRefused, match=r"onrc"):
        await switches.set_enabled(state, "market_ro", False)
    await switches.set_enabled(state, "email_outreach", True)
    with pytest.raises(switches.SwitchRefused, match=r"email_outreach"):
        await switches.set_enabled(state, "email_verify", False)


@pytest.mark.asyncio
async def test_an_unknown_module_is_refused(state):
    with pytest.raises(switches.SwitchRefused, match="no module"):
        await switches.set_enabled(state, "nope", True)


# ── Meeting modules for the first time ──


def _v17_db(path: Path, *, confirmed=(), settings=(), companies=0):
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:17]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 17")
    for code in CATALOG:
        conn.execute("INSERT INTO signal_codes (code, status) VALUES (?, ?)",
                     (code, "confirmed" if code in confirmed else "proposed"))
    for key, value in settings:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    for i in range(companies):
        conn.execute("INSERT INTO companies (id, name) VALUES (?, ?)", (f"c{i}", f"Exemplu {i}"))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_an_upgrade_keeps_working_modules_on(tmp_path, monkeypatch):
    """LinkedIn's keys are set and HIRING_ROLE was confirmed: both modules
    ship off, and stay on here. The old Email switch moves over, and its old
    key stays for an older Harvey started on the same database."""
    monkeypatch.setattr(switches, "present_env_keys",
                        lambda: {"LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "SERPER_API_KEY"})
    path = tmp_path / "old.db"
    _v17_db(path, confirmed=("HIRING_ROLE",), settings=[("ui.module.email", "on")])
    sm = StateManager(str(path))
    await sm.init_db()
    stored = await sm.settings_with_prefix("modules.")
    assert stored == {"modules.linkedin.enabled": "on", "modules.hiring.enabled": "on",
                      "modules.email_outreach.enabled": "on"}
    assert await sm.get_setting("ui.module.email") == "on"
    on = await switches.enabled_ids(sm)
    assert {"linkedin", "hiring", "email_outreach"} <= on
    assert not on & {"startups", "web_marketing", "web_search_scrape"}


@pytest.mark.asyncio
async def test_an_upgrade_with_no_key_set_takes_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(switches, "present_env_keys", lambda: {"SERPER_API_KEY"})
    path = tmp_path / "old.db"
    _v17_db(path, companies=1, settings=[("ui.module.email", "")])
    sm = StateManager(str(path))
    await sm.init_db()
    assert await sm.settings_with_prefix("modules.") == {}
    assert "linkedin" not in await switches.enabled_ids(sm)


@pytest.mark.asyncio
async def test_a_new_install_takes_the_defaults_whatever_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(switches, "present_env_keys",
                        lambda: {"LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"})
    sm = StateManager(str(tmp_path / "new.db"))
    await sm.init_db()
    assert await sm.settings_with_prefix("modules.") == {}
    assert await switches.enabled_ids(sm) == DEFAULT_ON


def test_the_upgrade_rule_only_counts_keyed_search_and_enricher_modules():
    mans = switches.manifests()
    every_key = {k for m in mans for k in m.secrets}
    assert switches.upgrade_enabled(mans, every_key, set()) == ["linkedin"]
    assert switches.upgrade_enabled(mans, set(), {"YC_BACKED"}) == ["startups"]


# ── Discovery: a source or preset whose module is off is refused ──


@pytest.mark.asyncio
async def test_run_discovery_refuses_a_disabled_provider(state):
    await switches.set_enabled(state, "serper", False)
    config = SimpleNamespace(icp=SimpleNamespace(industries=["roofing"], geography=["Denver"]),
                             product=SimpleNamespace(name="x"))
    with pytest.raises(switches.ModuleOff, match="harvey modules enable serper"):
        await D.run_discovery(state, config, "serper", dry_run=True)


@pytest.mark.asyncio
async def test_run_discovery_refuses_a_preset_whose_module_is_off(state):
    queries = [D.DiscoveryQuery(term="x", preset="yc")]
    with pytest.raises(switches.ModuleOff, match="startups"):
        await D.run_discovery(state, None, "serper", queries, dry_run=True)


def test_cli_discover_refuses_a_disabled_provider(tmp_path, monkeypatch, capsys):
    from harvey.cli import cmd_discover

    db = tmp_path / "harvey.db"
    monkeypatch.setattr(state_module, "DB_PATH", db)

    async def turn_off():
        sm = StateManager(str(db))
        await sm.init_db()
        await switches.set_enabled(sm, "tinyfish", False)
    asyncio.run(turn_off())
    args = SimpleNamespace(provider="tinyfish", providers=False, estimate=True, city="",
                           depth=30, limit=100, max_spend=1.0, no_profile=True, preset="")
    with pytest.raises(SystemExit) as ei:
        cmd_discover(args)
    assert ei.value.code == 2
    assert "harvey modules enable tinyfish" in capsys.readouterr().out


def test_provider_menu_says_which_module_and_whether_on():
    menu = {m["key"]: m for m in D.provider_menu({}, {"osm", "nominatim"})}
    assert (menu["osm"]["module"], menu["osm"]["enabled"]) == ("osm", True)
    assert (menu["serper"]["module"], menu["serper"]["enabled"]) == ("serper", False)
    assert all(m["enabled"] for m in D.provider_menu({}))


# ── ONRC ──


@pytest.mark.asyncio
async def test_saving_registry_firms_needs_the_onrc_module(state):
    from harvey.collectors import onrc

    await switches.set_enabled(state, "onrc", False)
    with pytest.raises(switches.ModuleOff, match="onrc"):
        await onrc.save_firms(state, [], None, onrc.ImportReport(dataset="x"), codes={})


# ── CLI ──


def _cli(monkeypatch, db, *argv):
    from harvey.cli import main

    monkeypatch.setattr(state_module, "DB_PATH", db)
    out = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["harvey", *argv])
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            main()
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue()


def test_cli_enable_disable_and_list(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    code, out = _cli(monkeypatch, db, "modules", "list", "--json")
    assert code == 0 and not db.exists()            # listing creates nothing
    assert {r["id"] for r in json.loads(out) if r["enabled"]} == DEFAULT_ON

    code, out = _cli(monkeypatch, db, "modules", "enable", "startups")
    assert code == 0 and "is on" in out and "YC_BACKED" in out
    code, out = _cli(monkeypatch, db, "modules", "disable", "startups")
    assert code == 0 and "Nothing was deleted" in out
    code, out = _cli(monkeypatch, db, "modules", "disable", "website")
    assert code == 2 and "core" in out
    code, out = _cli(monkeypatch, db, "modules", "disable", "market_ro")
    assert code == 2 and "onrc" in out
    code, out = _cli(monkeypatch, db, "modules", "enable")
    assert code == 2 and "Which module" in out
    _, out = _cli(monkeypatch, db, "modules", "list", "--json")
    rows = {r["id"]: r for r in json.loads(out)}
    assert rows["startups"]["enabled"] is False and rows["osm"]["enabled"] is True


# ── Dashboard ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    for key in ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "REOON_API_KEY", "SERPER_API_KEY"):
        monkeypatch.setenv(key, "")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


def test_modules_page_lists_sets_keys_present_never_values(client, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "fictional-serper-value-123")
    data = client.get("/api/modules").json()
    assert [s["key"] for s in data["sets"]] == ["core", "original", "ro"]
    rows = {m["id"]: m for s in data["sets"] for m in s["modules"]}
    assert rows["serper"]["secrets"] == [{"key": "SERPER_API_KEY", "present": True}]
    assert {"key": "REOON_API_KEY", "present": False} in rows["email_verify"]["secrets"]
    assert "fictional-serper-value-123" not in json.dumps(data)
    assert rows["website"]["locked"] and rows["website"]["enabled"]
    assert not rows["startups"]["enabled"]
    assert [s["code"] for s in rows["onrc"]["signals"]] == ["ACTIVITY_CODE"]
    assert rows["linkedin"]["terms_note"] and rows["dataforseo"]["cost"]["free_tier"]


def test_modules_switch_through_the_api(client):
    r = client.post("/api/modules/startups", json={"enabled": True})
    assert r.status_code == 200 and r.json()["success"]
    sigs = {s["code"] for g in client.get("/api/signals").json()["groups"] for s in g["signals"]}
    assert "YC_BACKED" in sigs

    r = client.post("/api/modules/website", json={"enabled": False})
    assert r.status_code == 409 and "core" in r.json()["message"]
    r = client.post("/api/modules/market_ro", json={"enabled": False})
    assert r.status_code == 409 and "onrc" in r.json()["message"]
    assert client.post("/api/modules/nope", json={"enabled": True}).status_code == 404
    assert client.post("/api/modules/osm", json={"enabled": "yes"}).status_code == 400


def test_signals_page_hides_modules_that_are_off(client):
    data = client.get("/api/signals").json()
    shown = {s["code"] for g in data["groups"] for s in g["signals"]}
    assert "TECH_STACK" not in shown and "YC_BACKED" not in shown and "HIRING_ROLE" not in shown
    assert data["total"] == len(shown) and data["hidden"] == len(CATALOG) - len(shown)
    assert [g["key"] for g in data["groups"]] == ["people", "phones", "website", "dataforseo",
                                                 "email_verify", "onrc"]
    assert all(s["when_known"] for g in data["groups"] for s in g["signals"])

    r = client.post("/api/signals/status", json={"codes": ["TECH_STACK"], "status": "confirmed"})
    assert r.json()["changed"] == 0 and r.json()["module_off"] == ["TECH_STACK"]
    r = client.post("/api/cohort", json={"require": ["TECH_STACK"]})
    assert r.status_code == 400 and "web_marketing" in r.json()["error"]
    assert client.get("/api/cohort.csv", params={"require": "TECH_STACK"}).status_code == 400


def test_discover_api_refuses_and_hides_disabled_sources(client):
    client.post("/api/modules/serper", json={"enabled": False})
    data = client.get("/api/discover/providers").json()
    menu = {p["key"]: p for p in data["providers"]}
    assert menu["serper"]["enabled"] is False and menu["osm"]["enabled"] is True
    assert data["presets"] == {"yc": False, "hiring": False, "accelerator": False,
                               "funding": False}
    r = client.post("/api/discover/estimate", json={"provider": "serper"})
    assert r.status_code == 409 and "harvey modules enable serper" in r.json()["error"]
    r = client.post("/api/discover/run", json={"provider": "serper"})
    assert r.status_code == 409 and not r.json()["success"]
    r = client.post("/api/discover/estimate", json={"provider": "tinyfish", "preset": "yc"})
    assert r.status_code == 409 and "startups" in r.json()["error"]


def test_settings_offers_the_env_keys_of_enabled_modules(client):
    data = client.get("/api/settings").json()
    assert "REOON_API_KEY" in data["env_keys"] and "HUNTER_API_KEY" in data["env_keys"]
    assert "LINKEDIN_EMAIL" not in data["env_keys"]           # linkedin ships off
    assert "GMAIL_CLIENT_ID" not in data["env_keys"]          # so does email_outreach
    assert "TRULYINBOX_API_KEY" not in data["env_keys"]       # saved by its own flow

    r = client.post("/api/settings/env", json={"LINKEDIN_EMAIL": "someone@example.com"})
    assert r.status_code == 409 and "linkedin" in r.json()["message"]
    assert not (dash.ENV_FILE.exists() and "LINKEDIN" in dash.ENV_FILE.read_text())

    client.post("/api/modules/linkedin", json={"enabled": True})
    r = client.post("/api/settings/env", json={"LINKEDIN_EMAIL": "someone@example.com"})
    assert r.json()["success"]
    assert "LINKEDIN_EMAIL=someone@example.com" in dash.ENV_FILE.read_text()
    assert "LINKEDIN_EMAIL" in client.get("/api/settings").json()["env_keys"]


def test_the_email_switch_is_the_email_outreach_module(client):
    client.post("/api/ui/modules", json={"email": True})
    assert client.get("/api/modules").json()["sets"][1]["modules"][1]["id"] == "email_outreach"
    rows = {m["id"]: m for s in client.get("/api/modules").json()["sets"] for m in s["modules"]}
    assert rows["email_outreach"]["enabled"] is True
    assert "GMAIL_CLIENT_ID" in client.get("/api/settings").json()["env_keys"]
    # Email campaigns need the verifier: it can't go while they are on.
    r = client.post("/api/modules/email_verify", json={"enabled": False})
    assert r.status_code == 409 and "email_outreach" in r.json()["message"]
    client.post("/api/ui/modules", json={"email": False})
    client.post("/api/modules/email_verify", json={"enabled": False})
    r = client.post("/api/ui/modules", json={"email": True})
    assert r.status_code == 409 and "email_verify" in r.json()["message"]


# ── Dashboard strings ──


def _server_strings(lang: str) -> dict:
    src = (WEB / "i18n.js").read_text(encoding="utf-8")
    start = src.index(f"\nObject.assign(I18N.{lang}, {{")
    block = src[start:src.index("\n});", start)]
    return {k: json.loads(f'"{v}"') for k, v in
            re.findall(r'^\s+"(module\.[a-z_]+\.[a-z]+)": "((?:[^"\\]|\\.)*)",$', block, re.M)}


def test_every_module_speaks_both_languages_and_english_matches_the_manifest():
    en, ro = _server_strings("en"), _server_strings("ro")
    assert set(en) == set(ro)
    for m in switches.manifests():
        cost = m.cost or {}
        want = {"name": m.name, "about": m.description, "terms": m.terms_note,
                "free": cost.get("free_tier", ""), "cost": cost.get("note", "")}
        for field, text in want.items():
            key = f"module.{m.id}.{field}"
            if text:
                assert en.get(key) == text, key
                assert ro.get(key), key
            else:
                assert key not in en, key


def test_modules_page_renders_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    data = {"sets": [{"key": "core", "modules": [
        {"id": "website", "name": "Website reader", "description": "Reads a site.", "set": "core",
         "kind": "enricher", "enabled": True, "locked": True, "requires": [], "signals": [],
         "secrets": [], "cost": {"note": "", "free_tier": ""}, "terms_note": ""}]},
        {"key": "original", "modules": [
        {"id": "serper", "name": "Serper", "description": "Google search.", "set": "original",
         "kind": "search", "enabled": False, "locked": False, "requires": [],
         "signals": [{"code": "SERP_RANK", "label": "Search rank position"}],
         "secrets": [{"key": "SERPER_API_KEY", "present": True}],
         "cost": {"note": "", "free_tier": "2,500 queries free, no card"}, "terms_note": ""}]}]}
    app = (WEB / "app.js").read_text(encoding="utf-8")
    start = app.index("const MODULE_SETS =")
    funcs = app[start:app.index("async function setModuleOn(")]
    script = r"""
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const el = {innerHTML: ''};
global.document = {getElementById: () => el};
global.escHtml = s => String(s === undefined || s === null ? '' : s).replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/"/g, '&quot;');
global.jsq = s => "'" + s + "'";
global.icon = n => '<svg></svg>';
global.emptyState = (g, title) => '<div class="empty">' + title + '</div>';
global.tServer = (key, english) => (getLang() === 'en' || !english) ? (english || '')
  : (hasKey('en', key) && t(key, null, 'en') === english ? t(key) : english);
vm.runInThisContext(process.argv[2]);
const out = {};
for (const lang of ['ro', 'en']) {
  vm.runInThisContext("LANG = '" + lang + "';");
  renderModulesPage(JSON.parse(process.argv[3]));
  out[lang] = el.innerHTML;
}
vm.runInThisContext("LANG = 'ro';");
renderModulesPage({sets: []});
out.empty = el.innerHTML;
console.log(JSON.stringify(out));
"""
    res = subprocess.run([node, "-e", script, str(WEB / "i18n.js"), funcs, json.dumps(data)],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    out = json.loads(res.stdout)
    ro, en = out["ro"], out["en"]
    assert "De bază" in ro and "Harvey original" in ro and "Cititorul de site" in ro
    assert "2.500 de căutări gratuite, fără card" in ro and "Poziția în căutare" in ro
    assert "SERPER_API_KEY · setată" in ro
    assert re.search(r'<input type="checkbox" checked disabled aria-label="Cititorul de site"', ro)
    assert re.search(r'<input type="checkbox" aria-label="Serper"', ro)
    assert "The original Harvey" in en and "SERPER_API_KEY · set" in en
    assert "Niciun modul găsit" in out["empty"]


def test_signal_rows_say_when_known_and_settings_hide_keys_of_modules_off():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    funcs = (app[app.index("function tServer("):app.index("async function loadSignals(")]
             + app[app.index("function sigLabel("):app.index("async function setSignals(")]
             + app[app.index("function applyEnvKeys("):app.index("function saveGmail(")])
    script = r"""
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
global.escHtml = s => String(s); global.jsq = s => JSON.stringify(s); global.badge = s => '';
const fields = ['REOON_API_KEY', 'LINKEDIN_EMAIL', 'REOON_API_KEY ZEROBOUNCE_API_KEY HUNTER_API_KEY']
  .map(keys => ({dataset: {env: keys}, hidden: false}));
global.document = {querySelectorAll: () => fields};
vm.runInThisContext(process.argv[2]);
const row = sigRow({code: 'ACTIVITY_CODE', label: 'Registered activity (CAEN)', description: 'd',
  cost_note: 'free (ONRC open data, data.gov.ro)', status: 'proposed', when_known: 'source'});
applyEnvKeys(['HUNTER_API_KEY']);
console.log(JSON.stringify({row, hidden: fields.map(f => f.hidden)}));
"""
    res = subprocess.run([node, "-e", script, str(WEB / "i18n.js"), funcs],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    out = json.loads(res.stdout)
    assert "se știe din sursă" in out["row"]
    assert out["hidden"] == [True, True, False]


# ── Upgrades keep what was running; opening a database changes nothing else ──


def _v17_with(path: Path, *statements):
    _v17_db(path)
    conn = sqlite3.connect(path)
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


SCOUT_TRACES = {
    "web_search_scrape": ("INSERT INTO companies (id, name, domain, source) VALUES (?, ?, ?, ?)",
                          ("c1", "Exemplu Web", "web.example.com", "web_search")),
    "web_marketing": ("INSERT INTO companies (id, name, domain, tech_stack_json) VALUES (?, ?, ?, ?)",
                      ("c2", "Exemplu Tech", "tech.example.com", '["HubSpot"]')),
    "hiring": ("INSERT INTO companies (id, name, domain, signals_json) VALUES (?, ?, ?, ?)",
               ("c3", "Exemplu Hiring", "jobs.example.com",
                '[{"type": "hiring", "detail": "Engineer", "found_at": "2026-01-01"}]')),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("module_id", sorted(SCOUT_TRACES))
async def test_an_upgrade_keeps_the_scout_parts_the_install_has_data_from(tmp_path, monkeypatch,
                                                                          module_id):
    """Scout's search-engine reading, tech detection and careers scan ran
    with no switch before modules existed. An install with data from one
    keeps it on; one without (an install that only used OSM) takes the
    default, off."""
    monkeypatch.setattr(switches, "present_env_keys", lambda: set())
    path = tmp_path / "old.db"
    _v17_with(path, SCOUT_TRACES[module_id])
    sm = StateManager(str(path))
    await sm.init_db()
    on = await switches.enabled_ids(sm)
    assert module_id in on
    assert not (set(SCOUT_TRACES) - {module_id}) & on


@pytest.mark.asyncio
async def test_an_upgrade_with_only_job_board_contacts_keeps_hiring(tmp_path, monkeypatch):
    monkeypatch.setattr(switches, "present_env_keys", lambda: set())
    path = tmp_path / "old.db"
    _v17_with(path,
              ("INSERT INTO companies (id, name, source) VALUES (?, ?, ?)", ("c1", "Exemplu", "osm")),
              ("INSERT INTO prospects (id, first_name, last_name, company, source) "
               "VALUES (?, ?, ?, ?, ?)", ("p1", "Ana", "Pop", "Exemplu", "job_board")))
    sm = StateManager(str(path))
    await sm.init_db()
    assert "hiring" in await switches.enabled_ids(sm)


def test_the_upgrade_rule_needs_one_key_not_every_alternative():
    """Reoon or ZeroBounce alone was enough for verification; a module that
    ships off and lists both stays on with either."""
    m = switches.by_id()["email_verify"]
    off_by_default = SimpleNamespace(**{**m.__dict__, "default_enabled": False})
    assert switches.upgrade_enabled([off_by_default], {"ZEROBOUNCE_API_KEY"}, set()) == [
        "email_verify"]
    assert switches.upgrade_enabled([off_by_default], set(), set()) == []


@pytest.mark.asyncio
async def test_opening_an_old_database_reads_env_key_names_only(tmp_path, monkeypatch):
    """A port Harvey can't parse in .env used to stop the database opening
    (the upgrade loaded .env through config); and the upgrade wrote .env
    into the process environment. Only names are read now."""
    import os

    import harvey.config as config

    env = tmp_path / ".env"
    env.write_text("SMTP_PORT=smtp\nLINKEDIN_EMAIL=someone@example.com\n")
    monkeypatch.setattr(switches, "ENV_FILE", env)
    monkeypatch.setenv("SMTP_PORT", "smtp")
    monkeypatch.delenv("LINKEDIN_EMAIL", raising=False)
    monkeypatch.setattr(config, "load_env", lambda: pytest.fail("the upgrade loaded .env"))
    path = tmp_path / "old.db"
    _v17_db(path, companies=1)
    sm = StateManager(str(path))
    await sm.init_db()
    assert (await _rows(sm, "PRAGMA user_version"))[0][0] == len(MIGRATIONS)
    assert "linkedin" in await switches.enabled_ids(sm)
    assert "LINKEDIN_EMAIL" not in os.environ


# ── Packs in data/modules: a broken one is left out, never fatal ──


@pytest.fixture
def packs(tmp_path, monkeypatch):
    folder = tmp_path / "packs"
    monkeypatch.setattr(switches, "DATA_DIR", folder)
    switches.manifests.cache_clear()
    yield folder
    switches.manifests.cache_clear()


def _pack(folder: Path, pid: str, body: str):
    (folder / pid).mkdir(parents=True)
    (folder / pid / "module.toml").write_text(
        f'id = "{pid}"\nset = "xx"\nkind = "market"\nversion = "1.0.0"\napi = 1\n' + body)


@pytest.mark.asyncio
async def test_a_broken_pack_is_left_out_and_said_so(packs, tmp_path, monkeypatch):
    _pack(packs, "market_xx", 'default_enabled = true\nmarkets = ["XX"]\n')
    (packs / "market_yy").mkdir()
    (packs / "market_yy" / "module.toml").write_text(            # claims core
        'id = "market_yy"\nset = "core"\nkind = "market"\nversion = "1.0.0"\napi = 1\n')
    _pack(packs, "lang_zz", '[[signals]]\ncode = "TECH_STACK"\n')   # another module's code
    ids = {m.id for m in switches.manifests()}
    assert "market_xx" in ids and not {"market_yy", "lang_zz"} & ids
    problems = "\n".join(switches.pack_problems())
    assert "market_yy" in problems and "TECH_STACK is already declared" in problems

    # An old database still opens and upgrades: the upgrade reads the
    # shipped manifests only.
    monkeypatch.setattr(switches, "present_env_keys", lambda: set())
    path = tmp_path / "old.db"
    _v17_db(path, companies=1)
    sm = StateManager(str(path))
    await sm.init_db()
    assert (await _rows(sm, "PRAGMA user_version"))[0][0] == len(MIGRATIONS)
    assert await sync_vocabulary(sm) == len(CATALOG)


def test_the_modules_page_shows_why_a_pack_was_left_out(packs, client):
    (packs / "market_yy").mkdir(parents=True)
    (packs / "market_yy" / "module.toml").write_text("id = 'market_yy'\n")
    data = client.get("/api/modules").json()
    assert data["sets"] and any("market_yy" in p and "missing" in p for p in data["pack_problems"])


# ── The vocabulary sync and a switch in another process ──


@pytest.mark.asyncio
async def test_a_sync_reads_the_switches_inside_its_own_write(state, monkeypatch):
    """The availability written always matches the switches as stored when
    the rows are written: a switch made between a sync's read and its write
    can't be overwritten with a stale view."""
    await switches.set_enabled(state, "web_marketing", False)
    real = state.write_vocabulary

    async def flip_first(rows_for, prefix):
        # Another process turns web_marketing on just before this sync writes.
        await state.set_setting(switches.KEY.format("web_marketing"), switches.ON)
        return await real(rows_for, prefix)

    monkeypatch.setattr(state, "write_vocabulary", flip_first)
    await sync_vocabulary(state)
    await state.set_signal_status("TECH_STACK", "confirmed")
    assert "TECH_STACK" in await state.confirmed_signal_codes()


# ── Hunter and the verifiers follow their switches ──


@pytest.mark.asyncio
async def test_find_email_calls_no_service_whose_module_is_off(state, monkeypatch):
    from harvey.integrations import email_finder as ef

    called = []

    async def mx(domain):
        return "mx.example.com"

    async def hunter_pattern(domain, key):
        called.append("hunter")
        return ("{first}.{last}", 0.9)

    async def verifier(name):
        async def check(*a, **k):
            called.append(name)
            return None
        return check

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "fictional-key")
    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter_pattern)
    for name in ("verify_reoon", "verify_zerobounce", "verify_hunter",
                 "verify_email_smtp", "detect_catch_all"):
        monkeypatch.setattr(ef, name, await verifier(name))
    env = SimpleNamespace(reoon_api_key="fictional", zerobounce_api_key="fictional")

    await ef.find_email("Ana", "Pop", "example.com", env=env, state=state)
    assert {"hunter", "verify_reoon", "verify_hunter", "verify_email_smtp"} <= set(called)

    called.clear()
    await switches.set_enabled(state, "hunter", False)
    await switches.set_enabled(state, "email_verify", False)
    result = await ef.find_email("Ion", "Pop", "example.org", env=env, state=state)
    assert called == [] and result.status == "guess"
    called.clear()
    assert await ef.verify_published_address("office@example.org", env=env, state=state) == "guess"
    assert called == []


# ── Settings, refusals and the ONRC commands ──


def test_a_key_that_holds_a_value_stays_editable_with_its_module_off(client, monkeypatch):
    """The heartbeat sends through SMTP whatever the Email switch says: a
    password in use can be rotated (or cleared) with the module off. Keys
    that are empty stay hidden and refused."""
    dash.ENV_FILE.write_text("SMTP_HOST=mail.example.com\nSMTP_PASSWORD=old-example\n")
    keys = client.get("/api/settings").json()["env_keys"]
    assert {"SMTP_HOST", "SMTP_PASSWORD"} <= set(keys) and "SMTP_USERNAME" not in keys
    r = client.post("/api/settings/env", json={"SMTP_PASSWORD": "new-example"})
    assert r.status_code == 200 and "SMTP_PASSWORD=new-example" in dash.ENV_FILE.read_text()
    r = client.post("/api/settings/env", json={"SMTP_USERNAME": "someone@example.com"})
    assert r.status_code == 409 and r.json()["off_module"] == "email_outreach"


def test_module_off_refusals_name_the_module_for_the_page(client):
    client.post("/api/modules/serper", json={"enabled": False})
    r = client.post("/api/discover/estimate", json={"provider": "serper"})
    assert r.status_code == 409 and r.json()["off_module"] == "serper"
    r = client.post("/api/discover/run", json={"provider": "serper"})
    assert r.json()["off_module"] == "serper" and r.json()["success"] is False
    r = client.post("/api/cohort", json={"require": ["TECH_STACK"]})
    assert r.status_code == 400 and r.json()["off_module"] == "web_marketing"
    r = client.get("/api/cohort.csv", params={"require": "TECH_STACK"})
    assert r.json()["off_module"] == "web_marketing"


def test_the_modules_page_names_what_a_module_needs(client):
    rows = {m["id"]: m for s in client.get("/api/modules").json()["sets"] for m in s["modules"]}
    assert rows["email_outreach"]["requires"] == [{"id": "email_verify",
                                                   "name": "Email verification"}]


@pytest.mark.parametrize("argv", [
    ("registry", "load"), ("registry", "search", "dentist"), ("import-onrc", "--county", "BV"),
])
def test_onrc_commands_are_refused_before_any_download(tmp_path, monkeypatch, argv):
    import harvey.cli as cli

    db = tmp_path / "harvey.db"
    _cli(monkeypatch, db, "modules", "disable", "onrc")

    async def no_download(*a, **k):
        pytest.fail("the registry was fetched with the onrc module off")
    monkeypatch.setattr(cli, "_ensure_registry", no_download)
    monkeypatch.setattr(cli, "_registry_path", lambda args: tmp_path / "registry.db")
    code, out = _cli(monkeypatch, db, *argv)
    assert code == 2 and "harvey modules enable onrc" in out


def test_refusals_and_module_names_read_in_the_viewers_language():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    funcs = app[app.index("function failMsg("):app.index("// Lookup tables keyed by stored strings")]
    script = r"""
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
vm.runInThisContext(process.argv[2]);
const body = {success: false, message: 'Saving X needs the Serper module (serper), which is off.',
              off_module: 'serper'};
const out = {};
for (const lang of ['ro', 'en']) {
  vm.runInThisContext("LANG = '" + lang + "';");
  out[lang] = failMsg(body, 'x');
}
out.unknown = failMsg({off_module: 'not_shipped'}, 'x');
out.plain = failMsg({message: 'a run is already going'}, 'x');
console.log(JSON.stringify(out));
"""
    res = subprocess.run([node, "-e", script, str(WEB / "i18n.js"), funcs],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    out = json.loads(res.stdout)
    assert out["ro"] == "Pentru asta trebuie pornit modulul Serper. Îl găsești în Surse."
    assert out["en"] == "This needs the Serper module, which is off. Turn it on in Sources."
    assert "not_shipped" in out["unknown"] and out["plain"] == "a run is already going"


def test_the_modules_page_lists_requirements_by_name_and_left_out_packs():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    data = {"pack_problems": ["data/modules/market_yy/module.toml: missing set"],
            "sets": [{"key": "original", "modules": [
                {"id": "email_outreach", "name": "Email campaigns", "description": "", "set": "original",
                 "kind": "action", "enabled": False, "locked": False,
                 "requires": [{"id": "email_verify", "name": "Email verification"}],
                 "signals": [], "secrets": [], "cost": {}, "terms_note": ""}]}]}
    app = (WEB / "app.js").read_text(encoding="utf-8")
    funcs = app[app.index("const MODULE_SETS ="):app.index("async function setModuleOn(")]
    script = r"""
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const el = {innerHTML: ''};
global.document = {getElementById: () => el};
global.escHtml = s => String(s === undefined || s === null ? '' : s).replace(/</g, '&lt;');
global.jsq = s => "'" + s + "'";
global.icon = n => '';
global.emptyState = (g, title) => title;
global.tServer = (key, english) => (getLang() === 'en' || !english) ? (english || '')
  : (hasKey('en', key) && t(key, null, 'en') === english ? t(key) : english);
vm.runInThisContext(process.argv[2]);
vm.runInThisContext("LANG = 'ro';");
renderModulesPage(JSON.parse(process.argv[3]));
console.log(JSON.stringify(el.innerHTML));
"""
    res = subprocess.run([node, "-e", script, str(WEB / "i18n.js"), funcs, json.dumps(data)],
                         text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    html = json.loads(res.stdout)
    ro_name = json.loads(subprocess.run(
        [node, "-e", "const fs=require('fs'),vm=require('vm');"
         "vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));"
         "LANG='ro';console.log(JSON.stringify(t('module.email_verify.name')))",
         str(WEB / "i18n.js")], text=True, capture_output=True, check=True).stdout)
    assert "Are nevoie de</span><span class=\"v\">" + ro_name in html
    assert "email_verify" not in html
    assert "Pachete din data/modules lăsate deoparte" in html and "market_yy" in html


def test_the_modules_page_stretches_its_rows_on_a_phone():
    """In the narrow column a row's children sized to their content (a long
    line of signal chips) and pushed the page sideways."""
    css = (WEB / "app.css").read_text(encoding="utf-8")
    narrow = css[css.index(".module-problems li"):]
    narrow = narrow[narrow.index("@media (max-width: 600px)"):]
    narrow = narrow[:narrow.index("}\n}") + 3]
    assert re.search(r"\.module-list \.module-row \{[^}]*align-items: stretch", narrow)
