"""The database upgrade the user runs (harvey/upgrade.py): its stages in
order, resuming after a stop, refusing to switch without a passing
verification, the switch keeping the old files, deleting them on request,
the output-folder dry run that never writes the data folder, and the
dashboard and CLI around it. While harvey.db waits for the upgrade, Harvey
opens nothing on it: pages answer 409, the worker waits. All data is
fictional (the schema 24 fixture of test_overlay_migrate)."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harvey import upgrade
from harvey.state import StateManager, UpgradeNeeded
from tests import test_query_plans as plans
from tests.test_anaf_store import plain_file
from tests.test_overlay_migrate import build_v24


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """A data folder: a schema 24 harvey.db, the registers and a plain
    (version 1) anaf.db."""
    d = tmp_path_factory.mktemp("data")
    build_v24(d / "harvey.db")
    plans.build_registry(d / "registry.db")
    plans.build_financials(d / "financials.db")
    plans.build_taxpayers(d / "taxpayers.db")
    plain_file(d / "anaf.db")
    return d


@pytest.fixture
def data(template, tmp_path) -> Path:
    d = tmp_path / "data"
    shutil.copytree(template, d)
    return d


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path: Path) -> int:
    return upgrade.schema_of(path)


def _passing(monkeypatch):
    """Verification as it would pass (its own checks are verify.py's tests)."""
    from harvey.overlay import verify

    real = verify.report

    def report(new, old, **kw):
        rep = real(new, old, **kw)
        for section in kw.get("sections", ()):
            if section == "registers" and isinstance(rep.get("registers"), dict):
                for row in rep["registers"].values():
                    if isinstance(row, dict):
                        row["passed"] = True
        rep["failures"] = verify.failures(rep)
        rep["passed"] = not rep["failures"]
        return rep

    monkeypatch.setattr(verify, "report", report)


def test_an_old_database_is_never_opened_or_changed_by_harvey(data):
    before = _sha(data / "harvey.db")
    with pytest.raises(UpgradeNeeded):
        asyncio.run(StateManager(str(data / "harvey.db")).init_db())
    assert _sha(data / "harvey.db") == before
    assert upgrade.needed(data) and _version(data / "harvey.db") == 24


def test_a_new_install_starts_on_the_slim_database(tmp_path):
    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())
    assert _version(path) == 25 and not upgrade.needed(tmp_path)


def test_the_worker_waits_and_takes_no_lease_while_the_upgrade_is_pending(data):
    from harvey.modules.worker import Worker

    state = StateManager(str(data / "harvey.db"))

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(state, poll=0.05).run(stop))
        await asyncio.sleep(0.3)
        assert not task.done()              # waiting, not failed
        stop.set()
        return await asyncio.wait_for(task, 5)

    before = _sha(data / "harvey.db")
    assert asyncio.run(go()) is True
    assert _sha(data / "harvey.db") == before   # no lease written into it


def test_the_upgrade_runs_every_stage_switches_and_keeps_the_old_files(data, monkeypatch):
    _passing(monkeypatch)
    old_db, old_anaf = _sha(data / "harvey.db"), _sha(data / "anaf.db")
    lines = []
    s = upgrade.run(upgrade.Layout(data), say=lines.append, check_space=False)
    states = {st["id"]: st["state"] for st in s["stages"]}
    assert states == dict.fromkeys(upgrade.STAGES, "done")
    assert s["switched"] and not s["needed"] and s["verification"]["passed"]
    assert s["verification"]["counts"]["firms (live)"]["old"] == \
        s["verification"]["counts"]["firms (live)"]["new"]
    assert s["verification"]["anaf"]["equal"]
    # The new files took the live names; the old ones are kept, unchanged.
    assert _version(data / "harvey.db") == 25
    assert _sha(data / upgrade.OLD_DB) == old_db and _sha(data / upgrade.OLD_ANAF) == old_anaf
    con = sqlite3.connect(data / "anaf.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] != 0
    con.close()
    assert not (data / upgrade.NEW_DB).exists() and not (data / upgrade.NEW_ANAF).exists()
    names = {f["name"] for f in s["old_files"]}
    assert {upgrade.OLD_DB, upgrade.OLD_ANAF} <= names
    # Registers got their derived tables in their own files.
    con = sqlite3.connect(data / "financials.db")
    assert con.execute("SELECT COUNT(*) FROM filed").fetchone()[0] > 0
    con.close()
    # Harvey opens the new file; the old files go only when asked.
    asyncio.run(StateManager(str(data / "harvey.db")).init_db())
    freed = upgrade.delete_old(upgrade.Layout(data))
    assert freed > 0 and upgrade.old_files(upgrade.Layout(data)) == []
    assert upgrade.run(upgrade.Layout(data), check_space=False)["switched"]   # nothing to do


def test_a_failed_verification_switches_nothing(data, monkeypatch):
    from harvey.overlay import verify

    real = verify.report

    def failing(new, old, **kw):
        rep = real(new, old, **kw)
        if "counts" in kw.get("sections", ()):
            rep["counts"]["firms (live)"]["passed"] = False
        return rep

    monkeypatch.setattr(verify, "report", failing)
    with pytest.raises(upgrade.Refused, match="verification failed"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    s = upgrade.status(upgrade.Layout(data))
    assert s["needed"] and not s["switched"] and not s["verification"]["passed"]
    assert any("firms (live)" in f for f in s["verification"]["failures"])
    assert _version(data / "harvey.db") == 24 and not (data / upgrade.OLD_DB).exists()
    with pytest.raises(upgrade.Refused):
        upgrade.delete_old(upgrade.Layout(data))


def test_a_stopped_upgrade_resumes_where_it_stopped(data, monkeypatch):
    from harvey.overlay import migrate

    _passing(monkeypatch)
    real = migrate._observations

    def crash(run):
        raise RuntimeError("power cut")

    monkeypatch.setattr(migrate, "STEPS", tuple(crash if s is real else s
                                                for s in migrate.STEPS))
    with pytest.raises(RuntimeError, match="power cut"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    s = upgrade.status(upgrade.Layout(data))
    states = {st["id"]: st["state"] for st in s["stages"]}
    assert states["registry"] == states["anaf"] == "done" and states["harvey"] == "failed"
    assert "power cut" in s["error"] and s["needed"] and not s["running"]
    seconds = {st["id"]: st.get("seconds") for st in s["stages"]}
    monkeypatch.setattr(migrate, "STEPS", tuple(real if s is crash else s
                                                for s in migrate.STEPS))
    s = upgrade.run(upgrade.Layout(data), check_space=False)
    assert s["switched"] and s["error"] is None
    # The finished stages weren't run again.
    assert {st["id"]: st.get("seconds") for st in s["stages"]}["registry"] == seconds["registry"]


def test_an_output_folder_leaves_the_data_folder_untouched(data, tmp_path, monkeypatch):
    _passing(monkeypatch)
    before = {p.name: _sha(p) for p in data.iterdir()}
    out = tmp_path / "out"
    s = upgrade.run(upgrade.Layout(data, out), check_space=False)
    assert {p.name: _sha(p) for p in data.iterdir()} == before
    states = {st["id"]: st["state"] for st in s["stages"]}
    assert states["switch"] == "skipped" and states["verify"] == "done"
    assert _version(out / upgrade.NEW_DB) == 25 and (out / upgrade.NEW_ANAF).is_file()
    assert (out / "registry_derived.db").is_file() and (out / "financials_derived.db").is_file()
    assert json.loads((out / upgrade.STATE_FILE).read_text())["verification"]["passed"]


def test_the_estimate_explains_a_drive_it_cant_see(data, monkeypatch):
    from harvey import disk

    def unseen(path):
        raise disk.HostDriveUnknown("set HARVEY_HOST_DRIVE_SOURCE=/mnt/c in .env")

    monkeypatch.setattr(disk, "free_bytes", unseen)
    est = upgrade.status(upgrade.Layout(data))["estimate"]
    assert est["free_bytes"] is None and "HARVEY_HOST_DRIVE_SOURCE" in est["free_error"]
    assert est["sizes"]["harvey"] > 0 and est["sizes"]["anaf"] > 0 and est["seconds"] > 0
    with pytest.raises(upgrade.Refused, match="HARVEY_HOST_DRIVE_SOURCE"):
        upgrade.run(upgrade.Layout(data))


@pytest.fixture
def client(data, monkeypatch):
    import harvey.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", data / "harvey.db")
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    dash.token_store.configure(data / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_the_dashboard_shows_the_upgrade_instead_of_the_pages(client, data, monkeypatch):
    import harvey.dashboard as dash
    from harvey import disk

    r = client.get("/api/companies")
    assert r.status_code == 409 and r.json()["upgrade_required"]
    s = client.get("/api/upgrade").json()
    assert s["needed"] and s["schema"] == 24 and not s["running"]
    monkeypatch.setattr(disk, "free_bytes", lambda path: 10 * 10**9)
    r = client.post("/api/upgrade/start")
    assert r.status_code == 409 and "30 GB" in r.json()["message"]

    monkeypatch.setattr(disk, "free_bytes", lambda path: 100 * 10**9)
    _passing(monkeypatch)
    started = []
    monkeypatch.setattr(dash, "_spawn_upgrade", lambda: started.append(
        upgrade.run(upgrade.Layout(data), check_space=False)))
    assert client.post("/api/upgrade/start").json()["success"]
    assert started and started[0]["switched"]
    assert client.get("/api/companies").status_code == 200
    s = client.get("/api/upgrade").json()
    assert not s["needed"] and s["old_files"]
    assert client.post("/api/upgrade/delete-old", json={"confirm": "da"}).status_code == 400
    assert (data / upgrade.OLD_DB).exists()
    r = client.post("/api/upgrade/delete-old", json={"confirm": "Șterge"}).json()
    assert r["success"] and r["freed"] > 0 and not (data / upgrade.OLD_DB).exists()


def test_the_cli_checks_runs_and_deletes(data, monkeypatch, capsys):
    import sys

    from harvey import cli, disk

    _passing(monkeypatch)
    monkeypatch.setattr(disk, "free_bytes", lambda path: 100 * 10**9)

    def harvey(*argv):
        monkeypatch.setattr(sys, "argv", ["harvey", *argv])
        cli.main()
        return capsys.readouterr().out

    out = harvey("upgrade", "--check", "--data", str(data))
    assert "needs the upgrade" in out and _version(data / "harvey.db") == 24
    out = harvey("upgrade", "--yes", "--data", str(data))
    assert "Verification: passed" in out and _version(data / "harvey.db") == 25
    out = harvey("upgrade", "--delete-old", "--yes", "--data", str(data))
    assert "freed" in out and not (data / upgrade.OLD_DB).exists()


def test_a_registry_load_builds_the_financials_filed_again(tmp_path):
    from harvey.modules.ro.mf_financials import derived

    plans.build_registry(tmp_path / "registry.db")
    plans.build_financials(tmp_path / "financials.db")
    counts = derived.refile(tmp_path / "financials.db", tmp_path / "registry.db")
    assert counts is not None and "filed_yes" in counts
    assert derived.refile(tmp_path / "financials.db", tmp_path / "missing.db") is None


# ── The switch: never without a live harvey.db, finished on the next run ──

def _verified(data, monkeypatch, *, stop_before_switch=True):
    """Every stage up to a passed verification, the switch not run."""
    _passing(monkeypatch)
    real = upgrade._switch

    def held(layout, progress):
        raise upgrade.Refused("held for the test")

    monkeypatch.setattr(upgrade, "_switch", held)
    with pytest.raises(upgrade.Refused, match="held"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    monkeypatch.setattr(upgrade, "_switch", real)


def _replace_failing_for(monkeypatch, name, times=1):
    """os.replace refusing to move ``name`` (a crash between renames)."""
    import os

    real = os.replace
    left = {"n": times}

    def flaky(a, b):
        if Path(a).name == name and left["n"] > 0:
            left["n"] -= 1
            raise OSError("cut short (test)")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)


def test_a_switch_cut_between_two_renames_is_finished_and_nothing_takes_its_place(
        data, monkeypatch):
    import os

    _verified(data, monkeypatch)
    original = _sha(data / "harvey.db")
    # No hard links here: two renames, cut between them.
    monkeypatch.setattr(os, "link", lambda a, b: (_ for _ in ()).throw(OSError("no links")))
    _replace_failing_for(monkeypatch, upgrade.NEW_DB)
    with pytest.raises(OSError, match="cut short"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    assert not (data / "harvey.db").exists()
    s = upgrade.status(upgrade.Layout(data))
    assert s["needed"] and s["switch_pending"] and upgrade.needed(data)
    # Nothing creates a database in its place meanwhile: not a worker, not a page.
    state = StateManager(str(data / "harvey.db"))
    with pytest.raises(UpgradeNeeded, match="unfinished"):
        asyncio.run(state.init_db())
    assert asyncio.run(state.needs_upgrade())

    async def wait_briefly():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, stop.set)
        return await state.wait_until_upgraded(stop, poll=0.02)

    assert asyncio.run(wait_briefly()) is False
    assert not (data / "harvey.db").exists()
    with pytest.raises(upgrade.Refused):
        upgrade.delete_old(upgrade.Layout(data))
    # The next run finishes it.
    s = upgrade.run(upgrade.Layout(data), check_space=False)
    assert s["switched"] and not s["needed"] and _version(data / "harvey.db") == 25
    assert _sha(data / upgrade.OLD_DB) == original
    assert not (data / upgrade.NEW_DB).exists() and not (data / upgrade.NEW_ANAF).exists()
    assert upgrade.delete_old(upgrade.Layout(data)) > 0


def test_with_hard_links_harvey_db_is_never_missing(data, monkeypatch):
    import os

    _verified(data, monkeypatch)
    original = _sha(data / "harvey.db")
    real = os.replace
    seen = []

    def watching(a, b):
        if Path(a).name == upgrade.NEW_DB:
            seen.append(((data / "harvey.db").exists(), (data / upgrade.OLD_DB).exists()))
        return real(a, b)

    monkeypatch.setattr(os, "replace", watching)
    s = upgrade.run(upgrade.Layout(data), check_space=False)
    assert seen == [(True, True)] and s["switched"]
    assert _sha(data / upgrade.OLD_DB) == original and _version(data / "harvey.db") == 25


def test_a_cut_after_the_link_resumes_without_refusing_its_own_link(data, monkeypatch):
    _verified(data, monkeypatch)
    _replace_failing_for(monkeypatch, upgrade.NEW_DB)
    with pytest.raises(OSError):
        upgrade.run(upgrade.Layout(data), check_space=False)
    assert _version(data / "harvey.db") == 24 and (data / upgrade.OLD_DB).exists()
    s = upgrade.run(upgrade.Layout(data), check_space=False)
    assert s["switched"] and _version(data / "harvey.db") == 25


def test_the_anaf_half_cut_short_is_finished_and_the_worker_waits_for_it(data, monkeypatch):
    from harvey.modules.ro.anaf import store

    _verified(data, monkeypatch)
    _replace_failing_for(monkeypatch, upgrade.NEW_ANAF)
    with pytest.raises(OSError):
        upgrade.run(upgrade.Layout(data), check_space=False)
    assert _version(data / "harvey.db") == 25 and (data / upgrade.NEW_ANAF).is_file()
    s = upgrade.status(upgrade.Layout(data))
    assert s["needed"] and s["switch_pending"] and not s["switched"]
    assert asyncio.run(StateManager(str(data / "harvey.db")).needs_upgrade())
    s = upgrade.run(upgrade.Layout(data), check_space=False)
    assert s["switched"] and not s["needed"]
    assert upgrade._anaf_version(data / "anaf.db") == store.VERSION
    assert (data / upgrade.OLD_ANAF).is_file()


def test_every_refusal_comes_before_the_first_rename(data, monkeypatch):
    _verified(data, monkeypatch)
    (data / upgrade.OLD_ANAF).write_bytes(b"left from before")
    with pytest.raises(upgrade.Refused, match="anaf.db.old already exists"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    assert _version(data / "harvey.db") == 24 and not (data / upgrade.OLD_DB).exists()
    (data / upgrade.OLD_ANAF).unlink()
    assert upgrade.run(upgrade.Layout(data), check_space=False)["switched"]


def _add_firm(path: Path) -> None:
    """Harvey writing to the schema 24 file: one more company."""
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(companies)")]
    picked = ", ".join("'cccc00000001'" if c == "id" else
                       "'Firma Noua Exemplu'" if c == "name" else
                       "'noua.example'" if c == "domain" else
                       "''" if c in ("cui", "external_id", "merged_into", "website") else c
                       for c in cols)
    con.execute(f"INSERT INTO companies ({', '.join(cols)}) SELECT {picked} FROM companies "
                "WHERE COALESCE(cui, '') = '' LIMIT 1")
    con.commit()
    con.close()


def _firms(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM firm").fetchone()[0]
    finally:
        con.close()


def test_a_write_after_the_migration_is_migrated_again_never_switched_stale(data, monkeypatch):
    _passing(monkeypatch)
    (data / upgrade.OLD_DB).write_bytes(b"left from before")     # the switch refuses
    with pytest.raises(upgrade.Refused, match="already exists"):
        upgrade.run(upgrade.Layout(data), check_space=False)
    before = _firms(data / upgrade.NEW_DB)
    _add_firm(data / "harvey.db")
    (data / upgrade.OLD_DB).unlink()
    lines = []
    s = upgrade.run(upgrade.Layout(data), say=lines.append, check_space=False)
    assert any("changed since it was migrated" in line for line in lines)
    assert "verify: started" in lines and s["switched"]
    assert _firms(data / "harvey.db") == before + 1


def test_a_write_between_verification_and_switch_is_refused(data, monkeypatch):
    _verified(data, monkeypatch)
    _add_firm(data / "harvey.db")
    with pytest.raises(upgrade.Refused, match="written to after it was migrated"):
        upgrade._switch(upgrade.Layout(data), upgrade._Progress(
            upgrade.Layout(data), upgrade.read_state(upgrade.Layout(data)), lambda line: None))
    assert _version(data / "harvey.db") == 24
    s = upgrade.status(upgrade.Layout(data))
    assert {st["id"]: st["state"] for st in s["stages"]}["harvey"] == "todo"


def test_a_missing_migration_after_a_pass_is_verified_again(data, monkeypatch):
    _verified(data, monkeypatch)
    for suffix in upgrade.SIDECARS:
        Path(str(data / upgrade.NEW_DB) + suffix).unlink(missing_ok=True)
    lines = []
    s = upgrade.run(upgrade.Layout(data), say=lines.append, check_space=False)
    assert "harvey: started" in lines and "verify: started" in lines and s["switched"]


def test_old_files_go_only_once_the_switch_is_done(tmp_path):
    asyncio.run(StateManager(str(tmp_path / "harvey.db")).init_db())
    (tmp_path / upgrade.OLD_DB).write_bytes(b"not switched by any upgrade")
    with pytest.raises(upgrade.Refused, match="isn't finished"):
        upgrade.delete_old(upgrade.Layout(tmp_path))
    assert (tmp_path / upgrade.OLD_DB).exists()


def test_an_output_folder_refuses_a_database_in_use(data, tmp_path, monkeypatch):
    _passing(monkeypatch)
    con = sqlite3.connect(data / "harvey.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO settings (key, value) VALUES ('wal_test', 'x')")
    con.commit()
    try:
        with pytest.raises(upgrade.Refused, match="in use"):
            upgrade.run(upgrade.Layout(data, tmp_path / "out"), check_space=False)
    finally:
        con.close()


def test_the_worker_queues_the_register_counts_and_the_overview_says_counting(
        client, data, monkeypatch):
    from harvey.modules.worker import Worker

    _passing(monkeypatch)
    assert upgrade.run(upgrade.Layout(data), check_space=False)["switched"]
    ov = client.get("/api/overview").json()
    assert ov["counting"] is True and ov["with_activity"] is None
    state = StateManager(str(data / "harvey.db"))

    async def briefly():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.5, stop.set)
        await Worker(state, poll=0.05).run(stop)

    asyncio.run(briefly())
    con = sqlite3.connect(data / "harvey.db")
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'register_counts'").fetchone()[0] == 1
    con.close()
    asyncio.run(state.recount_register_tally())
    assert client.get("/api/overview").json()["counting"] is False
    assert asyncio.run(state.read("register_tally_stale")) is False


def test_a_slim_database_from_an_earlier_build_is_refused_by_name(tmp_path):
    from harvey.state import EarlierBuild

    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE prospects RENAME COLUMN company_id TO firm")
    con.commit()
    con.close()
    with pytest.raises(EarlierBuild, match="prospects.company_id"):
        asyncio.run(StateManager(str(path)).init_db())


def test_every_verification_count_has_a_label_in_both_languages(data, monkeypatch):
    import re

    _passing(monkeypatch)
    s = upgrade.run(upgrade.Layout(data, data.parent / "out"), check_space=False)
    keys = set(s["verification"]["counts"])
    assert {"copied: outbox", "copied: processed_replies", "columns copied"} <= keys
    text = (Path(upgrade.__file__).parent / "web" / "i18n.js").read_text(encoding="utf-8")
    for k in keys:
        quoted = re.escape(k.replace("'", "\\'"))
        assert len(re.findall(rf"'upgrade\.count\.{quoted}':", text)) == 2, k


def test_the_upgrade_screen_stops_the_page_polling_and_promises_nothing_it_cant_keep():
    web = Path(upgrade.__file__).parent / "web"
    app = (web / "app.js").read_text(encoding="utf-8")
    tail = app[app.index("// Agent status: quick poll"):]
    blocks = tail.split("setInterval(")[1:]
    assert blocks and all("_upgradeShown" in b.split("}, ")[0] for b in blocks)
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    assert "Paginile se deschid apoi în sub o secundă." not in i18n
    assert "Pages then open in under a second." not in i18n


def test_the_screen_resumes_a_cut_switch_whatever_the_free_space(client, data, monkeypatch):
    import harvey.dashboard as dash
    from harvey import disk

    _verified(data, monkeypatch)
    _replace_failing_for(monkeypatch, upgrade.NEW_ANAF)
    with pytest.raises(OSError):
        upgrade.run(upgrade.Layout(data), check_space=False)
    assert client.get("/api/companies").status_code == 409      # still the upgrade screen
    s = client.get("/api/upgrade").json()
    assert s["needed"] and s["switch_pending"]
    monkeypatch.setattr(disk, "free_bytes", lambda path: 1 * 10**9)
    started = []
    monkeypatch.setattr(dash, "_spawn_upgrade", lambda: started.append(
        upgrade.run(upgrade.Layout(data), check_space=False)))
    assert client.post("/api/upgrade/start").json()["success"]
    assert started and started[0]["switched"]
    assert client.get("/api/companies").status_code == 200
