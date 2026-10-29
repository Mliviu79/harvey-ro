"""The contact-finder dashboard: paged companies and contacts, the company
panel, exports, the Email module switch, the overview counts, and the
Python-environment setup check inside Docker. All data is fictional."""

import asyncio
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.models import Company
from harvey.models.prospect import Prospect
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

WEB = Path(dash.__file__).resolve().parent / "web"


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


def _seed(db_path) -> dict:
    """Five fictional companies; the first has two phones, people and emails."""
    async def go():
        s = StateManager(str(db_path))
        await s.init_db()
        await seed_signal_catalog(s)
        for code in ("PHONE_NUMBER", "CONTACT_FOUND"):
            await s.set_signal_status(code, "confirmed")
        ids = {}
        for i in range(5):
            ids[i] = await s.add_company(Company(
                name=f"EXEMPLU {i} SRL", cui=f"9000000{i}", domain=f"exemplu{i}.example",
                location="Brașov, Brașov, Romania", address=f"Str. Exemplului nr. {i}",
                phone="+40 268 000 101" if i == 0 else "", source="registry:onrc"))
        await s.add_observations([
            {"company_id": ids[0], "signal_code": "PHONE_NUMBER", "value_text": "+40700000102"},
            {"company_id": ids[0], "signal_code": "CONTACT_FOUND",
             "value_text": "Ana Exemplu — administrator"},
        ])
        await s.add_prospect(Prospect(company_id=ids[0], first_name="Ana", last_name="Exemplu",
                                      email="ana@exemplu0.example", email_status="verified"))
        await s.add_prospect(Prospect(company_id=ids[0], email="office@exemplu0.example",
                                      email_status="guess", source="company_inbox"))
        await s.add_prospect(Prospect(company_id=ids[0], first_name="Bad", last_name="Bounce",
                                      email="bad@exemplu0.example", email_status="invalid"))
        await s.add_prospect(Prospect(company_id=ids[1], first_name="Out", last_name="Opted",
                                      email="out@exemplu1.example", email_status="verified",
                                      status="opted_out"))
        await s.set_company_activities(ids[0], [("8623", 3)], source="onrc")
        return ids
    return asyncio.run(go())


# ── Companies ──


def test_companies_without_paging_is_the_original_list(client):
    _seed(client.db_path)
    rows = client.get("/api/companies").json()
    assert isinstance(rows, list) and len(rows) == 5
    assert "contact_count" in rows[0]


def test_companies_page_has_total_and_phone_email_first_fields(client):
    ids = _seed(client.db_path)
    page = client.get("/api/companies", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 5 and page["limit"] == 2 and len(page["companies"]) == 2
    rest = client.get("/api/companies", params={"limit": 2, "offset": 4}).json()
    assert len(rest["companies"]) == 1
    every = client.get("/api/companies", params={"limit": 100}).json()["companies"]
    first = next(c for c in every if c["id"] == ids[0])
    assert first["phone_display"] == "+40 268 000 101" and first["phone_type"] == "landline"
    assert first["other_phones"] == ["+40 700 000 102"]
    assert [e["email"] for e in first["emails"]] == ["ana@exemplu0.example", "office@exemplu0.example"]
    assert first["people"] == [{"name": "Ana Exemplu", "title": "administrator"}]
    assert first["activities"][0]["code"] == "8623"
    assert first["contact_count"] == 3
    opted = next(c for c in every if c["id"] == ids[1])
    assert opted["emails"] == []                       # an opt-out is never offered
    assert client.get("/api/companies", params={"limit": 10_000}).json()["limit"] == 200


def test_search_results_are_enriched_too(client):
    _seed(client.db_path)
    res = client.get("/api/companies/search", params={"q": "8623", "limit": 10}).json()
    assert res["total"] == 1
    (row,) = res["companies"]
    assert row["phone_display"] == "+40 268 000 101" and row["contact_count"] == 3
    assert row["activities"][0]["matched"] is True


def test_company_detail(client):
    ids = _seed(client.db_path)
    c = client.get(f"/api/companies/{ids[0]}").json()
    assert c["cui"] == "90000000" and c["address"] == "Str. Exemplului nr. 0"
    assert c["other_phones"] == ["+40 700 000 102"]
    assert client.get("/api/companies/nope").status_code == 404


# ── Exports ──


def _csv(text):
    return list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"))))


def test_company_export_by_ids_search_and_all(client):
    ids = _seed(client.db_path)
    r = client.post("/api/export/companies.csv", json={"ids": [ids[0], ids[2]]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert {row["name"] for row in _csv(r.text)} == {"EXEMPLU 0 SRL", "EXEMPLU 2 SRL"}
    found = _csv(client.post("/api/export/companies.csv", json={"q": "8623"}).text)
    assert [row["name"] for row in found] == ["EXEMPLU 0 SRL"]
    assert found[0]["other_phones"] == "+40 700 000 102"
    assert len(_csv(client.post("/api/export/companies.csv", json={}).text)) == 5
    data = client.post("/api/export/companies.json", json={"ids": [ids[0]]}).json()
    assert data["count"] == 1
    assert data["companies"][0]["id"] == ids[0] and data["companies"][0]["cui"] == "90000000"
    assert data["companies"][0]["email"] == "ana@exemplu0.example"


def test_company_export_refuses_bad_bodies(client):
    _seed(client.db_path)
    for body in ({"ids": "x"}, {"ids": [1]}, {"ids": ["a"] * 5001}, {"q": 3}, [1]):
        assert client.post("/api/export/companies.csv", json=body).status_code == 400
    assert client.post("/api/export/companies.json", json={"ids": "x"}).status_code == 400


def test_company_export_keeps_the_formula_guard(client):
    async def go():
        s = StateManager(str(client.db_path))
        await s.init_db()
        return await s.add_company(Company(name="=HYPERLINK(\"http://x.example\")",
                                           domain="formula.example"))
    cid = asyncio.run(go())
    text = client.post("/api/export/companies.csv", json={"ids": [cid]}).text
    assert _csv(text)[0]["name"].startswith("'=")


def test_exports_need_the_token(client):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as anon:
        for path in ("/api/export/companies.csv", "/api/export/companies.json",
                     "/api/export/prospects.csv", "/api/ui/modules"):
            assert anon.post(path, json={}).status_code == 401
        assert anon.get("/api/overview").status_code == 401


# ── Contacts ──


def test_contacts_page_search_filter_counts_and_selection_export(client):
    _seed(client.db_path)
    assert isinstance(client.get("/api/prospects").json(), list)       # unchanged without paging
    page = client.get("/api/prospects", params={"limit": 2}).json()
    assert page["total"] == 4 and len(page["prospects"]) == 2
    assert page["counts"]["all"] == 4 and page["counts"]["closed"] == 1
    ana = client.get("/api/prospects", params={"limit": 50, "q": "ana exemplu0"}).json()
    assert [p["email"] for p in ana["prospects"]] == ["ana@exemplu0.example"]
    assert ana["prospects"][0]["company_phone"] == "+40 268 000 101"
    closed = client.get("/api/prospects", params={"limit": 50, "filter": "closed"}).json()
    assert closed["total"] == 1 and closed["prospects"][0]["status"] == "opted_out"
    weird = client.get("/api/prospects", params={"limit": 50, "q": "100%_"}).json()
    assert weird["total"] == 0                          # LIKE wildcards are literal

    ids = [p["id"] for p in client.get("/api/prospects").json()]
    r = client.post("/api/export/prospects.csv", json={"ids": ids})
    emails = {row["email"] for row in _csv(r.text)}
    assert "out@exemplu1.example" not in emails          # opted out stays out
    assert "ana@exemplu0.example" in emails
    assert client.post("/api/export/prospects.csv", json={}).status_code == 400


def test_dashboard_csvs_open_in_excel_and_carry_the_registry_identity(client):
    ids = _seed(client.db_path)
    r = client.post("/api/export/companies.csv", json={"ids": [ids[0]]})
    assert r.text.startswith("\ufeffname,")                 # BOM: Excel reads ș, ț, ă
    (row,) = _csv(r.text)
    assert row["cui"] == "90000000" and row["address"] == "Str. Exemplului nr. 0"
    assert row["activities"].startswith("8623")
    data = client.post("/api/export/companies.json", json={"ids": [ids[0]]}).json()
    assert data["companies"][0]["activities"][0]["code"] == "8623"
    assert client.get("/api/export/prospects.csv", params={"bom": "true"}).text.startswith("\ufeff")
    assert not client.get("/api/export/prospects.csv").text.startswith("\ufeff")   # unchanged


def test_selected_contacts_never_hand_out_a_bounced_address(client):
    _seed(client.db_path)
    rows = client.get("/api/prospects").json()
    bad = next(p for p in rows if p["email"] == "bad@exemplu0.example")
    (row,) = _csv(client.post("/api/export/prospects.csv", json={"ids": [bad["id"]]}).text)
    assert row["email"] == "" and row["last_name"] == "Bounce"


def test_search_export_says_when_it_was_cut_short(client, monkeypatch):
    _seed(client.db_path)
    monkeypatch.setattr(dash, "EXPORT_SEARCH_MAX", 2)
    r = client.post("/api/export/companies.csv", json={"q": "Brașov"})
    assert len(_csv(r.text)) == 2
    assert r.headers["X-Export-Matched"] == "5" and r.headers["X-Export-Rows"] == "2"


def test_one_company_export_reads_only_that_company(client):
    from harvey.export import collect_companies
    ids = _seed(client.db_path)
    rows = asyncio.run(collect_companies(StateManager(str(client.db_path)), ids=[ids[0]]))
    assert [r["id"] for r in rows] == [ids[0]]
    assert rows[0]["other_phones"] == "+40 700 000 102"
    assert asyncio.run(collect_companies(StateManager(str(client.db_path)), ids=[])) == []


def test_do_not_contact_addresses_are_not_shown_or_counted(client, monkeypatch):
    import harvey.export as export
    ids = _seed(client.db_path)
    monkeypatch.setattr(export, "_do_not_contact", lambda config: ["ana@exemplu0.example"])
    first = client.get(f"/api/companies/{ids[0]}").json()
    assert [e["email"] for e in first["emails"]] == ["office@exemplu0.example"]
    monkeypatch.setattr(export, "_do_not_contact", lambda config: ["exemplu0.example"])
    assert client.get(f"/api/companies/{ids[0]}").json()["emails"] == []
    assert client.get("/api/overview").json()["with_email"] == 0


# ── Search ──


def test_company_search_finds_a_cui(client):
    ids = _seed(client.db_path)
    for q in ("90000002", "RO90000002", "ro 90000002"):
        res = client.get("/api/companies/search", params={"q": q}).json()
        assert [c["id"] for c in res["companies"]] == [ids[2]], q
    assert client.get("/api/companies/search", params={"q": "8623"}).json()["total"] == 1   # still CAEN


def test_contact_search_ignores_accents_and_finds_places_and_phones(client):
    async def go():
        s = StateManager(str(client.db_path))
        await s.init_db()
        cid = await s.add_company(Company(name="ȘABLON EXEMPLU SRL", domain="sablon.example",
                                          location="Brașov, Brașov, Romania", phone="+40 778 000 100"))
        await s.add_prospect(Prospect(company_id=cid, first_name="Ștefan", last_name="Exemplu",
                                      email="stefan@sablon.example", company="ȘABLON EXEMPLU SRL"))
    asyncio.run(go())
    for q in ("sablon", "Șablon", "brasov", "Brașov", "stefan", "0778 000", "+40778", "0040778"):
        page = client.get("/api/prospects", params={"limit": 50, "q": q}).json()
        assert [p["email"] for p in page["prospects"]] == ["stefan@sablon.example"], q


# ── Sending, visible without the Email module ──


def test_sending_status_is_read_only_and_says_when_mail_can_go_out(client):
    status = client.get("/api/sending/status").json()
    assert set(status) >= {"paused", "autopilot", "queued", "waiting", "live"}
    assert status["queued"] == 0 and status["paused"] == ""
    client.post("/api/sending/pause")
    paused = client.get("/api/sending/status").json()
    assert paused["paused"] and paused["live"] is False
    assert client.get("/api/overview").json()["sending"]["paused"] == paused["paused"]
    client.post("/api/sending/resume")


# ── Module switch ──


def test_email_module_is_off_by_default_and_switches(client):
    assert client.get("/api/ui/modules").json() == {"modules": {"email": False}}
    r = client.post("/api/ui/modules", json={"email": True})
    assert r.json() == {"success": True, "modules": {"email": True}}
    assert client.get("/api/ui/modules").json()["modules"]["email"] is True

    async def setting():
        s = StateManager(str(client.db_path))
        return await s.get_setting("ui.module.email"), await s.get_setting("sending_paused")
    assert asyncio.run(setting()) == ("on", "")          # visibility only: no kill switch
    client.post("/api/ui/modules", json={"email": False})
    assert client.get("/api/ui/modules").json()["modules"]["email"] is False


@pytest.mark.parametrize("body", [{}, {"email": "yes"}, {"sending": True}, ["email"], None])
def test_module_switch_refuses_anything_else(client, body):
    assert client.post("/api/ui/modules", json=body).status_code == 400


def test_module_switch_is_not_read_by_sending():
    """The heartbeat must never look at a dashboard-only setting."""
    root = Path(dash.__file__).resolve().parent
    for path in root.rglob("*.py"):
        if path.name != "dashboard.py":
            assert "ui.module" not in path.read_text(encoding="utf-8"), path


# ── Overview and live status ──


def test_overview_counts(client):
    _seed(client.db_path)
    ov = client.get("/api/overview").json()
    assert ov["companies"] == 5 and ov["with_phone"] == 1
    assert ov["with_email"] == 1                         # the opt-out's company doesn't count
    assert ov["contacts"] == 4 and ov["people"] == 1 and ov["with_activity"] == 1
    assert ov["signals_confirmed"] == 2


def test_overview_on_an_empty_install(client):
    assert client.get("/api/overview").json()["companies"] == 0


def test_live_status_steps_carry_stable_keys(client):
    _seed(client.db_path)
    d = client.get("/api/live-status").json()
    assert [s["key"] for s in d["steps"]] == ["find", "read", "emails", "verify", "write",
                                             "sent", "replies"]
    assert d["steps"][0]["vars"] == {"companies": 5}
    assert d["next_key"] and isinstance(d["next_vars"], dict)


# ── Setup check in Docker ──


def _venv_check(client):
    return next(c for c in client.get("/api/setup-status").json()["checks"] if c["id"] == "venv")


def test_python_environment_is_done_inside_docker(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)             # no .venv here
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    monkeypatch.setattr(dash, "CONTAINER_MARKERS", (marker,))
    check = _venv_check(client)
    assert check["done"] is True and "Docker" in check["label"] and check["env"] == "docker"
    assert check["scope"] == "finding"


def test_python_environment_without_venv_docker_or_install(client, monkeypatch, tmp_path):
    import importlib.metadata as md
    monkeypatch.setattr(dash, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dash, "CONTAINER_MARKERS", (tmp_path / "missing",))

    def no_dist(name):
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(md, "distribution", no_dist)
    assert _venv_check(client)["done"] is False
    (tmp_path / ".venv").mkdir()
    assert _venv_check(client)["done"] is True


def test_every_setup_check_says_which_view_it_belongs_to(client):
    checks = client.get("/api/setup-status").json()["checks"]
    assert {c["scope"] for c in checks} <= {"finding", "email"}
    assert {c["id"] for c in checks if c["scope"] == "finding"} >= {"venv", "verifier"}


# ── The page itself ──


def test_page_loads_i18n_after_auth_and_before_app(client):
    html = client.get("/").text
    assert html.index("/static/auth.js") < html.index("/static/i18n.js") < html.index("/static/app.js")
    assert client.get("/static/i18n.js").status_code == 200
    assert '<html lang="ro">' in html


def test_every_signal_has_a_romanian_label():
    src = (WEB / "i18n.js").read_text(encoding="utf-8")
    for sig in SIGNAL_CATALOG:
        assert f'"sigc.{sig["code"]}.label"' in src, sig["code"]


def _js_value(src: str, i: int) -> tuple[str, int]:
    """The JS literal starting at src[i] (a quoted string or a {...} object
    of them), returned as text, and where it ends."""
    if src[i] in "'\"":
        q, j = src[i], i + 1
        while src[j] != q:
            j += 2 if src[j] == "\\" else 1
        return src[i + 1:j], j + 1
    assert src[i] == "{", src[i:i + 40]
    depth, j, quote = 0, i, ""
    while True:
        ch = src[j]
        if quote:
            if ch == "\\":
                j += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1], j + 1
        j += 1


def _i18n_dicts() -> dict[str, dict[str, str]]:
    """{'ro': {key: value text}, 'en': {...}} read from i18n.js without a JS
    engine: the main dictionaries and the server-text ones."""
    src = (WEB / "i18n.js").read_text(encoding="utf-8")
    cut = [src.index("\n  ro: {\n"), src.index("\n  en: {\n"),
           src.index("\nObject.assign(I18N.ro, {"), src.index("\nObject.assign(I18N.en, {"), len(src)]
    out = {"ro": {}, "en": {}}
    for lang, (a, b) in (("ro", cut[0:2]), ("en", cut[1:3]), ("ro", cut[2:4]), ("en", cut[3:5])):
        seg = src[a:b]
        seg = seg[seg.index("{") + 1:]
        for m in re.finditer(r"""(?:^|[\s,{])(['"])([A-Za-z0-9_.\-]+)\1\s*:\s*""", seg):
            value, _ = _js_value(seg, m.end())
            assert m.group(2) not in out[lang] or out[lang][m.group(2)] == value, (lang, m.group(2))
            out[lang][m.group(2)] = value
    return out


def test_every_ui_key_exists_in_both_languages():
    """Runs without node: every key the dashboard uses is in both
    dictionaries, both have the same keys, and each pair takes the same
    placeholders."""
    d = _i18n_dicts()
    ro, en = d["ro"], d["en"]
    assert len(ro) > 900
    assert sorted(set(ro) - set(en)) == [] and sorted(set(en) - set(ro)) == []
    for key in ro:
        assert set(re.findall(r"\{(\w+)\}", ro[key])) - {"n"} == \
            set(re.findall(r"\{(\w+)\}", en[key])) - {"n"}, key

    app = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    prefixes = {k.split(".")[0] for k in ro}
    used = {lit for lit in re.findall(r"""['"]([a-z][a-z0-9_]*\.[a-z0-9_.\-]+)['"]""", app)
            if lit.split(".")[0] in prefixes and not lit.endswith(".")}
    used |= set(re.findall(r'data-i18n(?:-html|-placeholder|-title|-aria)?="([^"]+)"', html))
    assert sorted(k for k in used if k not in ro) == []

    # Keys built at run time: a word for every value app.js can produce.
    def table(name):
        body = re.search(r"const %s = \{(.*?)\};" % name, app, re.S).group(1)
        return re.findall(r"(\w+):", body)
    built = {f"st.{k}" for k in table("STATUS")}
    built |= {f"agent.{k}" for k in table("AGENT_ICONS")}
    built |= {f"phone.{k}" for k in ("mobile", "landline", "other")}
    built |= {f"theme.{k}" for k in ("auto", "light", "dark")}
    built |= {f"check.venv.{k}" for k in ("venv", "docker", "installed")}
    assert sorted(k for k in built if k not in ro) == []


def test_romanian_plurals_and_numbers_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (f"require('vm').runInThisContext(require('fs').readFileSync({json.dumps(str(WEB / 'i18n.js'))}, 'utf8'));"
              "process.stdout.write(JSON.stringify({n: t('page.range', {from: 1, to: 50, total: 2289}),"
              " few: t('co.more_phones', {n: 3}), many: t('co.more_phones', {n: 25}),"
              " firms: t('kpi.fact_companies', {n: 125}), usd: fmtUsd(0.25, 2)}));")
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    encoding="utf-8", check=True, timeout=30).stdout)
    assert out["n"] == "1–50 din 2.289"
    assert (out["few"], out["many"]) == ("+3 numere", "+25 de numere")
    assert out["firms"] == "125 de firme"
    assert out["usd"].replace("\u00a0", " ") == "0,25 $"


def test_startup_upgrades_an_older_database_before_any_page(monkeypatch, tmp_path):
    # Prezentare only reads; after an update it used to hit tables the old
    # schema didn't have, until some other page happened to upgrade it.
    import sqlite3

    from harvey.state import MIGRATIONS

    db = tmp_path / "harvey.db"
    with sqlite3.connect(db) as conn:
        for version, script in enumerate(MIGRATIONS[:15], start=1):
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert c.get("/api/overview").json()["with_activity"] == 0
