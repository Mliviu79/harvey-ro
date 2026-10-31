"""The Registry page (harvey/registry_view.py, /api/registry): the trade
registry browsed with its financials, filtered as a pull is, sorted, paged
from a kept list, and pulled by filter or by the firms ticked.

A small invented registry is built here directly (every name, number and
person is fictional), with statements over three years, a taxpayer file and
a few ANAF answers.
"""

import json
import random
import sqlite3

import pytest
import pytest_asyncio

from harvey import registry, registry_view
from harvey.collectors import onrc
from harvey.models import Company
from harvey.modules.job_kinds import registry_params, registry_plan
from harvey.modules.ro.anaf import service as anaf_service
from harvey.modules.ro.mf_financials import store as fin_store
from harvey.modules.ro.mf_taxpayers import store as tax_store
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

N = 400
CODES = ["BV", "CJ", "B", "IS", "TM"]
CLASSES = [8623, 4711, 4120, 6201, 5610]


def cui(body: int) -> str:
    b = str(body)
    key = "753217532"[-len(b):]
    total = sum(int(d) * int(k) for d, k in zip(b, key))
    return b + str(total * 10 % 11 % 10)


def firm_cui(i: int) -> str:
    return cui(9_300_000 + i) if i % 11 else ""


def build(folder, n=N):
    """registry.db, financials.db, taxpayers.db and anaf.db in ``folder``."""
    rnd = random.Random(3)
    names = sorted((f"{w} {i:04d} SRL", i) for i, w in
                   ((i, rnd.choice(["EXEMPLU", "DENTAL", "AUTO", "PRIMA"])) for i in range(n)))
    reg = sqlite3.connect(folder / "registry.db")
    reg.executescript(registry.SCHEMA)
    for fid, (name, i) in enumerate(names, start=1):
        code = CODES[i % len(CODES)]
        operating = i % 3 != 0
        reg.execute("INSERT INTO firms VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            fid, f"J{i % 40:02d}/{i}/20{10 + i % 15}", name, firm_cui(i), "SRL",
            onrc.COUNTIES[code], code, f"Oras {code}", "", f"Str. Exemplu {i}", "", "",
            f"{1 + i % 28:02d}/{1 + i % 12:02d}/{1995 + i % 30}" if i % 17 else "",
            "1048" if operating else "1048 1072", int(operating)))
        reg.execute("INSERT INTO firm_caen VALUES (?, ?, 2)", (fid, CLASSES[i % len(CLASSES)]))
        if i % 4 == 0:
            reg.execute("INSERT INTO firm_caen VALUES (?, ?, 2)", (fid, CLASSES[(i + 1) % 5]))
        reg.execute("INSERT INTO administrators VALUES (?, ?, ?, 'ADMINISTRATOR')",
                    (fid, f"Persoana{i}", "Exemplescu"))
    reg.execute("CREATE UNIQUE INDEX firms_reg ON firms(reg)")
    reg.execute("CREATE INDEX firms_cui ON firms(cui) WHERE cui != ''")
    reg.execute("CREATE INDEX administrators_firm ON administrators(firm_id)")
    registry._load_caen(reg)
    registry.fill_search_index(reg)
    reg.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("schema", registry.SCHEMA_VERSION), ("dataset", "firme-01-10-2026"),
        ("firms", str(n))])
    reg.commit()
    reg.close()

    fin = sqlite3.connect(folder / "financials.db")
    fin.executescript(fin_store.SCHEMA)
    for i in range(n):
        c = firm_cui(i)
        if not c or i % 5 == 4:
            continue
        for year in (2025, 2024, 2023):
            if year == 2025 and i % 7 == 0:
                continue            # filed nothing for the latest year: 2024 is its latest
            vals = [None] * len(fin_store.FIELDS)
            # Ties on purpose (i % 50), unknown cells (i % 9, i % 13).
            vals[fin_store.FIELDS.index("turnover")] = None if i % 9 == 0 else (i % 50) * 1000 + year
            vals[fin_store.FIELDS.index("employees")] = None if i % 13 == 0 else i % 30
            profit = (i % 23 - 8) * 100
            vals[fin_store.FIELDS.index("net_profit")] = max(profit, 0)
            vals[fin_store.FIELDS.index("net_loss")] = max(-profit, 0)
            vals[fin_store.FIELDS.index("equity")] = i * 10
            fin.execute(f"INSERT INTO accounts VALUES ({','.join('?' * (4 + len(vals)))})",
                        (c, year, "BL", "8623", *vals))
    for year in (2025, 2024, 2023):
        fin.execute("INSERT INTO files VALUES (?, 'BL', ?, ?, '', '', 'CC-BY 4.0', 0, 0)",
                    (year, f"bl{year}.txt", f"https://data.gov.example/{year}"))
    fin.execute(fin_store.LATEST)
    fin.execute(fin_store.FILL_LATEST)
    fin.execute("INSERT INTO meta VALUES ('years', '2025 2024 2023')")
    fin.commit()
    fin.close()

    tax = sqlite3.connect(folder / "taxpayers.db")
    tax.executescript(tax_store.SCHEMA)
    for i in range(n):
        c = firm_cui(i)
        if c and i % 6:
            tax.execute("INSERT INTO taxpayers (cui, name, vat, micro_tax, employer) "
                        "VALUES (?, ?, ?, ?, ?)", (c, f"EXEMPLU {i}", i % 2, int(i % 3 == 1),
                                                   int(i % 4 != 0)))
    tax.commit()
    tax.close()

    anaf = anaf_service._connect(folder / "anaf.db")
    anaf.execute("INSERT INTO answers VALUES (?, '2026-10-15T00:00:00+00:00', 1, ?)",
                 (firm_cui(1), json.dumps({"date_generale": {"cui": int(firm_cui(1)),
                                                             "denumire": "EXEMPLU 0001 SRL"},
                                           "inregistrare_scop_Tva": {"scpTVA": True}})))
    anaf.commit()
    anaf.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    build(tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(fin_store, "FINANCIALS_PATH", tmp_path / "financials.db")
    monkeypatch.setattr(tax_store, "TAXPAYERS_PATH", tmp_path / "taxpayers.db")
    monkeypatch.setattr(anaf_service, "CACHE_PATH", tmp_path / "anaf.db")
    registry_view.SNAPSHOTS.clear()
    registry_view.RANKINGS.clear()
    yield tmp_path
    registry_view.SNAPSHOTS.clear()
    registry_view.RANKINGS.clear()


FILTERS = [
    {}, {"status": "all"}, {"county": "BV"}, {"county": "B", "status": "all"},
    {"words": "dentist"}, {"words": "4711"}, {"words": "auto -dentist"},
    {"words": "8623", "county": "CJ"}, {"locality": "Oras IS"},
    {"min_turnover": "20000"}, {"max_turnover": "15000", "unknown": "include"},
    {"min_employees": "5", "max_employees": "20"},
    {"min_employees": "10", "unknown": "include", "status": "all"},
    {"employer": "yes"}, {"vat": "no", "micro": "yes"},
    {"county": "TM", "min_turnover": "10000", "employer": "yes"},
    {"min_turnover": "1000", "vat": "yes", "status": "all"},
    # Only exclusions: every operating firm (or every firm) less those.
    {"words": "-dentist"}, {"words": "-dentist", "status": "all"},
    {"words": "-auto -BV", "status": "all"}, {"words": "-dentist", "min_employees": "5"},
    # Only joining words: no words at all.
    {"words": "si"}, {"words": "de la", "status": "all"},
]


def pull_ids(con, params):
    """The firms a pull of these parameters saves (the registry's own search)."""
    q = registry_view.resolve(con, params)
    return sorted(registry.matching_ids(con, q))


@pytest.mark.parametrize("filters", FILTERS, ids=lambda f: json.dumps(f, sort_keys=True))
@pytest.mark.parametrize("probe_max", [0, 10_000])
def test_the_page_lists_exactly_what_a_pull_saves(world, monkeypatch, filters, probe_max):
    # Both ways of checking figures and taxpayer conditions: firm by firm
    # (a narrow list) and from the statements' side (a broad one).
    monkeypatch.setattr(registry_view, "PROBE_MAX", probe_max)
    params = registry_params(filters)
    try:
        registry_plan(filters)
        pullable = True
    except ValueError:
        pullable = False
    con = registry_view.connect()
    try:
        listed = list(registry_view.matching(con, registry_view.resolve(con, params)))
        if pullable:
            assert listed == pull_ids(con, params)
            assert registry.count(con, registry_view.resolve(con, params)) == len(listed)
        else:
            # No filter: every operating firm (or every firm), which a
            # pull refuses (registry_plan) and the page lists.
            want = con.execute("SELECT id FROM firms" + (
                "" if filters.get("status") == "all" else " WHERE operating") + " ORDER BY id")
            assert listed == [r[0] for r in want]
            with pytest.raises(ValueError):
                registry_plan(filters)
    finally:
        con.close()


def test_only_excluded_words_leave_those_firms_out(world):
    # "-dentist" listed every firm, dentists too, while a pull counted 0.
    con = registry_view.connect()
    try:
        dentists = {r[0] for r in con.execute(
            "SELECT rowid FROM firm_fts WHERE firm_fts MATCH 'tags : c8623r2'")}
        for status in ("operating", "all"):
            params = registry_plan({"words": "-dentist", "status": status})
            q = registry_view.resolve(con, params)
            listed = set(registry_view.matching(con, q))
            want = {r[0] for r in con.execute("SELECT id FROM firms" + (
                " WHERE operating" if status == "operating" else ""))} - dentists
            assert listed == want and dentists and registry.count(con, q) == len(want)
    finally:
        con.close()


def test_joining_words_alone_say_nothing():
    assert not registry.says_anything("si")
    assert not registry.says_anything("de la -si")
    assert registry.says_anything("-auto") and registry.says_anything("BV")
    assert registry.says_anything("86.23") and registry.says_anything("medic de familie")
    assert registry_params({"words": "si de"})["words"] == ""
    with pytest.raises(ValueError):
        registry_plan({"words": "si"})


def test_the_registration_date_sort_reads_its_index(world):
    con = registry_view.connect()
    try:
        before = list(registry_view._read_ids(con, registry_view.RANKS["registered"]))
    finally:
        con.close()
    # A registry loaded before the index existed gets it once.
    assert registry.ensure_registered_index(world / "registry.db") is True
    assert registry.ensure_registered_index(world / "registry.db") is False
    con = registry_view.connect()
    try:
        plan = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN " + registry_view.RANKS["registered"]))
        assert "firms_registered" in plan and "TEMP B-TREE" not in plan
        assert list(registry_view._read_ids(con, registry_view.RANKS["registered"])) == before
    finally:
        con.close()


def test_warming_up_adds_the_index_then_computes_every_ranking(world, monkeypatch):
    import threading

    monkeypatch.setattr(registry_view, "WARM_FIRMS", 0)
    registry_view._warmed.clear()
    assert registry_view.warm() is True
    for t in threading.enumerate():
        if t.name == "registry-rankings":
            t.join(30)
    assert sorted(k[1] for k in registry_view.RANKINGS._items) == sorted(
        ["operating", *registry_view.RANKS])
    con = sqlite3.connect(world / "registry.db")
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'firms_registered'").fetchone()
    con.close()


def test_financial_conditions_alone_are_a_filter_a_pull_can_take(world):
    # Before, a turnover bound with no words or area resolved to nothing.
    con = registry_view.connect()
    try:
        params = registry_plan({"min_turnover": "30000"})
        assert len(pull_ids(con, params)) > 10
        every = registry_plan({"min_turnover": "30000", "status": "all"})
        assert len(pull_ids(con, every)) > len(pull_ids(con, params))
    finally:
        con.close()


def figures_by_firm(folder):
    con = sqlite3.connect(folder / "registry.db")
    con.execute("ATTACH DATABASE ? AS fin", (str(folder / "financials.db"),))
    out = {}
    for fid, reg, t, e, p, l, date in con.execute(
            "SELECT f.id, f.reg, l.turnover, l.employees, a.net_profit, a.net_loss, f.registered "
            "FROM firms f LEFT JOIN fin.latest l ON l.cui = f.cui AND f.cui != '' "
            "LEFT JOIN fin.accounts a ON a.cui = l.cui AND a.year = l.year"):
        net = None if p is None and l is None else (p or 0) - (l or 0)
        day = date[6:10] + date[3:5] + date[0:2] if len(date) >= 10 else None
        out[reg] = {"id": fid, "turnover": t, "employees": e, "profit": net, "registered": day}
    con.close()
    return out


def every_page(params, sort, way, size=37):
    regs, offset, total = [], 0, None
    while True:
        got = registry_view.page(params, sort, way, size, offset)
        total = got["total"]
        regs += [r["reg"] for r in got["rows"]]
        offset += size
        if offset >= total:
            return regs, total, got


@pytest.mark.parametrize("sort", ["name", "turnover", "employees", "profit", "registered"])
@pytest.mark.parametrize("way", ["asc", "desc"])
@pytest.mark.parametrize("filters", [{}, {"county": "BV", "status": "all"}, {"min_employees": "3"}])
def test_every_sort_both_ways_pages_through_the_whole_list(world, sort, way, filters):
    params = registry_params(filters)
    facts = figures_by_firm(world)
    con = registry_view.connect()
    try:
        members = set(registry_view.matching(con, registry_view.resolve(con, params)))
    finally:
        con.close()
    regs, total, _ = every_page(params, sort, way)
    assert total == len(members) == len(regs) == len(set(regs))
    assert {facts[r]["id"] for r in regs} == members
    if sort == "name":
        ids = [facts[r]["id"] for r in regs]
        assert ids == sorted(ids, reverse=way == "desc")
        return
    values = [facts[r][sort] for r in regs]
    have = [v for v in values if v is not None]
    # Firms with a value first, in order; the ones with none last, both ways.
    assert values[:len(have)] == have and all(v is None for v in values[len(have):])
    assert have == sorted(have, reverse=way == "desc")
    # Ties in name order going down (the reverse going up).
    for a, b in zip(regs, regs[1:]):
        if facts[a][sort] is not None and facts[a][sort] == facts[b][sort]:
            assert (facts[a]["id"] < facts[b]["id"]) == (way == "desc")


def test_later_pages_come_from_the_kept_list(world, monkeypatch):
    params = registry_params({"county": "BV", "status": "all"})
    first = registry_view.page(params, "turnover", "", 20, 0)
    assert first["paged_by"] == "computed" and first["dir"] == "desc"
    calls = []
    monkeypatch.setattr(registry_view, "ordering",
                        lambda *a: calls.append(a) or pytest.fail("recomputed"))
    deep = registry_view.page(params, "turnover", "asc", 20, first["total"] - 20)
    assert deep["paged_by"] == "kept" and deep["total"] == first["total"] and not calls
    assert len(deep["rows"]) == 20


def test_a_reload_drops_the_kept_list(world):
    params = registry_params({"county": "CJ"})
    registry_view.page(params, "turnover", "", 10, 0)
    path = world / "financials.db"
    stat = path.stat()
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert registry_view.page(params, "turnover", "", 10, 0)["paged_by"] == "computed"


def test_rows_carry_latest_figures_and_activities(world):
    got = registry_view.page(registry_params({"words": "8623"}), "name", "", 50, 0)
    facts = figures_by_firm(world)
    for row in got["rows"]:
        f = facts[row["reg"]]
        assert any(a["code"] == "8623" and a["matched"] for a in row["activities"])
        if f["turnover"] is None and f["employees"] is None and f["profit"] is None:
            assert row["figures"] is None or row["figures"]["turnover"] is None
        else:
            assert row["figures"]["turnover"] == f["turnover"]
            assert row["figures"]["net_result"] == f["profit"]
        assert "id" not in row


def test_figure_sorts_fall_back_to_name_without_statements(world):
    (world / "financials.db").unlink()
    got = registry_view.page(registry_params({}), "turnover", "", 10, 0)
    assert got["sort"] == "name"
    with pytest.raises(FileNotFoundError):
        registry_view.page(registry_params({"min_turnover": "1"}), "name", "", 10, 0)


def test_a_firm_with_every_year_and_its_records(world):
    con = registry_view.connect()
    (reg,) = con.execute("SELECT reg FROM firms WHERE cui = ?", (firm_cui(1),)).fetchone()
    con.close()
    got = registry_view.firm(reg)
    assert got["cui"] == firm_cui(1)
    assert [y["year"] for y in got["financials"]["years"]] == [2025, 2024, 2023]
    assert got["registry"]["firms"][0]["reg"] == reg
    assert got["registry"]["firms"][0]["administrators"][0]["role"] == "ADMINISTRATOR"
    assert got["taxpayer"]["state"] == "ok" and got["anaf"]["state"] == "ok"
    assert registry_view.firm("J99/0/1999") is None


def test_chosen_firms_are_a_query_of_their_own(world):
    con = registry_view.connect()
    try:
        regs = [r[0] for r in con.execute("SELECT reg FROM firms WHERE operating = 0 LIMIT 3")]
        params = registry_plan({"regs": regs + ["J99/0/1999"], "words": "ignored"})
        assert params["label"] == "4 chosen firms" and params["words"] == ""
        q = registry_view.resolve(con, params)
        found = registry.matching_ids(con, q)
        assert sorted(r[0] for r in con.execute(
            f"SELECT reg FROM firms WHERE id IN ({','.join('?' * len(found))})", found)) == sorted(regs)
        assert registry.count(con, q) == 3
    finally:
        con.close()
    with pytest.raises(ValueError):
        registry_params({"regs": "J08/1/2015"})
    with pytest.raises(ValueError):
        registry_params({"regs": [f"J{i}" for i in range(10_001)]})


def test_status_all_is_a_registry_filter_of_its_own():
    assert registry_params({"county": "BV"})["all_statuses"] is False
    assert registry_params({"county": "BV", "status": "all"})["all_statuses"] is True
    assert registry_params({"county": "BV", "status": "operating"})["all_statuses"] is False
    with pytest.raises(ValueError):
        registry_plan({"status": "all"})


@pytest_asyncio.fixture
async def state(world):
    sm = StateManager(str(world / "harvey.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    yield sm


@pytest.mark.asyncio
async def test_a_pull_of_chosen_firms_saves_exactly_them(world, state):
    from harvey.modules.runs import registry_pull

    con = registry_view.connect()
    try:
        regs = [r[0] for r in con.execute("SELECT reg FROM firms ORDER BY id DESC LIMIT 4")]
        q = registry_view.resolve(con, registry_plan({"regs": regs}))
        report = await registry_pull(state, con, q)
    finally:
        con.close()
    assert report.new_companies == 4 and not report.errors
    with sqlite3.connect(state.db_path) as db:
        saved = sorted(r[0][5:] for r in db.execute("SELECT external_id FROM companies"))
    assert saved == sorted(regs)


@pytest.fixture
def client(world, monkeypatch):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash
    import harvey.state as state_mod

    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")      # nothing runs: just the queue
    monkeypatch.setattr(dash, "DB_PATH", world / "harvey.db")
    monkeypatch.setattr(state_mod, "DB_PATH", world / "harvey.db")
    dash.token_store.configure(world / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_the_api_pages_marks_firms_on_file_and_shows_one(world, client):
    import asyncio

    async def add():
        sm = StateManager(str(world / "harvey.db"))
        await sm.init_db()
        return await sm.add_company(Company(name="EXEMPLU PE FISIER SRL", cui=firm_cui(2),
                                            source="registry:onrc"))
    company_id = asyncio.run(add())
    res = client.get("/api/registry", params={"county": "B", "status": "all", "sort": "name",
                                              "limit": 500})
    body = res.json()
    assert res.status_code == 200 and body["loaded"] == {"registry": True, "financials": True,
                                                         "taxpayers": True}
    assert [f["key"] for f in body["source"]["filters"]][:4] == ["words", "county", "locality",
                                                                 "status"]
    marked = [r for r in body["rows"] if r["company_id"]]
    assert [r["cui"] for r in marked] == [firm_cui(2)] and marked[0]["company_id"] == company_id
    assert body["total"] == len(body["rows"])
    reg = marked[0]["reg"]
    # A firm with no CUI is saved under its registration number.
    bare = next(r for r in body["rows"] if not r["cui"])

    async def add_bare():
        sm = StateManager(str(world / "harvey.db"))
        await sm.init_db()
        return await sm.add_company(Company(name=bare["name"], source="registry:onrc",
                                            external_id=f"onrc:{bare['reg']}"))
    bare_id = asyncio.run(add_bare())
    again = client.get("/api/registry", params={"county": "B", "status": "all", "sort": "name",
                                                "limit": 500}).json()
    assert {r["reg"]: r["company_id"] for r in again["rows"] if r["company_id"]} == {
        reg: company_id, bare["reg"]: bare_id}
    assert client.get("/api/registry/firm", params={"reg": bare["reg"]}).json()[
        "company_id"] == bare_id
    firm = client.get("/api/registry/firm", params={"reg": reg}).json()
    assert firm["company_id"] == company_id and firm["registry"]["firms"][0]["reg"] == reg
    assert client.get("/api/registry/firm", params={"reg": "J99/0/1999"}).status_code == 404
    bad = client.get("/api/registry", params={"county": "Atlantis"})
    assert bad.status_code == 400 and bad.json()["error"]


def test_the_api_counts_and_queues_a_pull_of_the_ticked_firms(world, client):
    from harvey.modules import jobs

    con = registry_view.connect()
    regs = [r[0] for r in con.execute("SELECT reg FROM firms LIMIT 5")]
    con.close()
    body = {"providers": ["onrc.registry_search"], "max_spend": 0,
            "filters": {"onrc.registry_search": {"regs": regs}}}
    est = client.post("/api/discover/estimate", json=body).json()
    assert est["available"] == 5 and est["free"] is True
    run = client.post("/api/discover/run", json=body).json()
    assert run["success"] and run["queries"] == 1
    with sqlite3.connect(world / "harvey.db") as db:
        (params,) = db.execute(
            "SELECT params_json FROM jobs WHERE kind = 'registry_pull'").fetchone()
    assert sorted(json.loads(params)["regs"]) == sorted(regs)
    by_filter = dict(body, filters={"onrc.registry_search": {"words": "dentist", "status": "all"}})
    est = client.post("/api/discover/estimate", json=by_filter).json()
    con = registry_view.connect()
    try:
        assert est["available"] == len(pull_ids(con, registry_params(
            {"words": "dentist", "status": "all"})))
    finally:
        con.close()
