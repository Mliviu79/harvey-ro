"""A large verification (harvey/contact_check.py, enqueue_contact_checks):
any selection up to contact_check.MAX_RUN contacts is taken and queued in
pieces of at most MAX_LINKS links, in order, under one batch: one
classifier cap over the whole run, one report, one cancel. The agent's
verify_contacts, the dashboard's endpoints and the CLI take it the same
way. Every model is a fake (no network). All data is fictional."""

import argparse
import asyncio
import json
import sqlite3
import time

import pytest

from harvey import classify, contact_check, contacts
from harvey import classify_profiles as profiles
from harvey.models import Company
from harvey.modules import jobs
from harvey.modules.job_kinds import enqueue_contact_checks
from tests.test_classify import FakeOpenAI
from tests.test_classify_profiles import LOCAL, _quick, _state  # noqa: F401 (the fixture)

FIRMS, PER_FIRM = 45, 100          # 4,500 contacts: three pieces of 2,000 links at most


def run(coro):
    return asyncio.run(coro)


async def _many(s, db, firms=FIRMS, per=PER_FIRM) -> list[int]:
    """``firms`` fictional firms, each with ``per`` email contacts a
    website's read linked to it; the contact ids in order."""
    fids = [int(await s.add_company(Company(name=f"PIECE {i} EXAMPLE SRL",
                                            location="Brașov, Brașov, Romania",
                                            source="registry:onrc")))
            for i in range(firms)]
    now = int(time.time())
    con = sqlite3.connect(db)
    con.execute("INSERT OR IGNORE INTO source (id, name) VALUES (1, 'website')")
    ids, n = [], 1000
    for k, fid in enumerate(fids):
        for j in range(per):
            n += 1
            con.execute("INSERT INTO contact (id, kind, value, first_seen, last_seen) "
                        "VALUES (?, 1, ?, ?, ?)", (n, f"c{j}@f{k}.example.com", now, now))
            con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, "
                        "last_seen) VALUES (?, ?, 1, 1, ?, ?)", (fid, n, now, now))
            ids.append(n)
    con.commit()
    con.close()
    return ids


def _pieces_of(db, batch) -> list[int]:
    """The links each queued job holds, in the order they were queued."""
    with sqlite3.connect(db) as con:
        return [r[0] for r in con.execute(
            "SELECT json_array_length(params_json, '$.links') FROM jobs WHERE batch_id = ? "
            "ORDER BY rowid", (batch,))]


def test_the_bulk_read_gives_what_reach_gives_one_contact(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await s.add_company(Company(name="EXEMPLU CONSTRUCTII SRL", cui="90000017",
                                          location="Brașov, Brașov, Romania",
                                          source="registry:onrc"))
        await s.add_contacts(cid, [
            contacts.Found("email", "office@exemplu-constructii.example.com", source="website",
                           capability="read_site",
                           evidence_url="https://exemplu-constructii.example.com/",
                           trust="likely"),
            contacts.Found("phone", "+40268000111", source="mf_taxpayers", trust="confirmed"),
            contacts.Found("phone", "+40268000111", source="website", capability="read_site",
                           trust="likely")])
        with sqlite3.connect(db) as con:
            ids = [r[0] for r in con.execute("SELECT id FROM contact ORDER BY id")]
        bulk = await s.read("contact_links", ids + [999_999])
        one = {i: (await s.read("reach", i))["links"] for i in ids}
        return ids, bulk, one

    ids, bulk, one = run(go())
    assert set(bulk) == set(ids)                       # one not on file is left out
    for i in ids:
        assert [(r["company_id"], r["source"], r["trust"], r["evidence_url"])
                for r in bulk[i]["links"]] == [
            (r["company_id"], r["source"], r["trust"], r["evidence_url"]) for r in one[i]]


def test_a_large_selection_is_queued_in_pieces_under_one_batch(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        ids = await _many(s, db)
        chosen = await contact_check.links(s, ids)
        est = await contact_check.estimate(s, chosen)
        batch, new, dupes = await enqueue_contact_checks(s, chosen, max_spend=0.5)
        return est, batch, new, dupes, await jobs.get_batch(s, batch)

    est, batch, new, dupes, b = run(go())
    assert est["contacts"] == est["links"] == 4500 and est["pieces"] == 3
    assert est["companies"] == FIRMS and not est["more"]
    assert len(new) == FIRMS and not dupes
    sizes = _pieces_of(db, batch)
    assert sizes == [PER_FIRM] * FIRMS
    # (20 firms of 100 links fill a piece: 20, 20 and 5, in the contacts' order.)
    assert [len(p) for p in contact_check.pieces(
        [{"company_id": str(k), "contact_id": k * 1000 + j} for k in range(FIRMS)
         for j in range(PER_FIRM)])] == [20, 20, 5]
    assert b["params"]["pieces"] == 3 and b["params"]["links"] == 4500
    assert b["params"]["classify_max_spend"] == 0.5
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM job_batches").fetchone() == (1,)


def test_a_company_is_never_split_across_pieces(monkeypatch):
    monkeypatch.setattr(contact_check, "MAX_LINKS", 3)
    chosen = ([{"company_id": "a", "contact_id": i} for i in range(2)]
              + [{"company_id": "b", "contact_id": 10 + i} for i in range(5)]
              + [{"company_id": "a", "contact_id": 20}]
              + [{"company_id": "c", "contact_id": 30}])
    split = contact_check.pieces(chosen)
    assert [[(cid, len(g)) for cid, g in p] for p in split] == [[("a", 3)], [("b", 5)],
                                                               [("c", 1)]]


def test_one_cap_holds_the_whole_run_and_one_cancel_stops_every_piece(tmp_path, monkeypatch):
    from tests.test_jobs import run_until_idle

    # (Pieces of two links: three firms of two contacts are three pieces.
    # The worst case of a call is what it costs, so the cap counts calls.)
    monkeypatch.setattr(contact_check, "MAX_LINKS", 2)
    monkeypatch.setattr(classify, "worst_tokens", lambda counted: 420)
    fake = FakeOpenAI(*[json.dumps({"c1": "yes"})] * 6).install(monkeypatch)
    fake.usage.completion_tokens = classify.TASKS["contacts"].max_output_tokens
    call = 420 * 0.1 / 1e6 + fake.usage.completion_tokens * 0.2 / 1e6
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        ids = await _many(s, db, firms=3, per=2)
        chosen = await contact_check.links(s, ids)
        batch, new, _ = await enqueue_contact_checks(s, chosen, max_spend=3.5 * call)
        await run_until_idle(s)
        after = [await jobs.get(s, j) for j in new]
        report = await jobs.batch_report(s, batch)
        from harvey.agent import plans
        cancelled = await plans.cancel_run(s, batch)
        return after, report, cancelled, [(await jobs.get(s, j)).status for j in new]

    after, report, cancelled, statuses = run(go())
    assert len(fake.requests) == 3                     # the first piece and half the second
    assert after[0].status == jobs.DONE and after[0].result["yes"] == 2
    assert after[1].status == jobs.PAUSED and after[1].paused_reason == jobs.SPEND_LIMIT
    assert after[2].status in (jobs.PAUSED, jobs.QUEUED)
    assert report["classify"]["calls"] == 3
    assert report["classify"]["usd"] == pytest.approx(3 * call, abs=1e-9)
    assert cancelled == 2 and statuses == [jobs.DONE, jobs.CANCELLED, jobs.CANCELLED]


def test_a_limit_takes_the_first_contacts_and_the_bound_is_the_runs(tmp_path):
    db = tmp_path / "harvey.db"
    from harvey import launch

    async def go():
        s = await _state(db)
        ids = await _many(s, db, firms=5, per=10)
        first, e1 = await launch.verify_selection(s, {"contact_ids": ids, "limit": 15})
        every, e2 = await launch.verify_selection(s, {"contact_ids": ids})
        filtered, e3 = await launch.verify_selection(s, {"filter": {}})
        bad, e4 = await launch.verify_selection(
            s, {"contact_ids": list(range(contact_check.MAX_RUN + 1))})
        return ids, first, every, filtered, bad, (e1, e2, e3, e4)

    ids, first, every, filtered, bad, errors = run(go())
    assert [link["contact_id"] for link in first] == ids[:15]
    assert len(every) == 50 and len(filtered) == 50          # 200 by default for a filter
    assert errors[:3] == ("", "", "") and not bad and f"{contact_check.MAX_RUN:,}" in errors[3]


# ── The dashboard and the CLI ──


@pytest.fixture
def client(tmp_path, monkeypatch):
    import harvey.dashboard as dash
    from fastapi.testclient import TestClient

    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)

    async def seed():
        s = await _state(db)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        return await _many(s, db)
    ids = run(seed())
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.ids, c.db = ids, db
        yield c


def test_the_dashboard_prices_and_queues_a_large_selection_as_one_run(client):
    est = client.post("/api/contacts/verify/estimate", json={"contact_ids": client.ids}).json()
    assert est["links"] == 4500 and est["pieces"] == 3 and est["usd_high"] > 0
    r = client.post("/api/contacts/verify", json={"contact_ids": client.ids,
                                                  "max_spend": 0.25}).json()
    assert r["success"] and r["queued"] == FIRMS and r["links"] == 4500 and r["pieces"] == 3
    with sqlite3.connect(client.db) as con:
        assert con.execute("SELECT COUNT(DISTINCT batch_id) FROM jobs").fetchone() == (1,)
        assert con.execute("SELECT json_extract(params_json, '$.classify_max_spend') "
                           "FROM job_batches").fetchone() == (0.25,)
    again = client.post("/api/contacts/verify", json={"contact_ids": client.ids,
                                                      "max_spend": 0.25})
    assert again.status_code == 409                       # every company waiting already


def test_the_cli_queues_a_large_selection_as_one_run(tmp_path, monkeypatch, capsys):
    from harvey import cli

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        ids = await _many(s, db)
        args = argparse.Namespace(undo="", review=False, contact=ids, county="", locality="",
                                  website="", q="", limit=None, max_spend=0.3, dry_run=False,
                                  background=True)
        await cli._verify(s, args)

    run(go())
    out = capsys.readouterr().out
    assert "4,500 link(s)" in out and "3 pieces" in out and "over the whole run" in out
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*), COUNT(DISTINCT batch_id) FROM jobs").fetchone() == (
            FIRMS, 1)


# ── Queued whole or not at all ──


def _failing_second_write(monkeypatch, seen: list, s=None):
    """jobs.enqueue_many as it is, but its second write fails; before
    failing it notes whether the classifier's lane could take a job the
    first write queued."""
    real = jobs.enqueue_many
    calls = {"n": 0}

    async def flaky(state, specs, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            seen.append(await jobs.claim(state, "classify", "test-worker"))
            raise sqlite3.OperationalError("database is locked")
        return await real(state, specs, **kw)
    monkeypatch.setattr(jobs, "enqueue_many", flaky)


def test_a_run_whose_later_piece_fails_to_queue_leaves_nothing_queued(tmp_path, monkeypatch):
    db = tmp_path / "harvey.db"
    seen: list = []

    async def go():
        s = await _state(db)
        ids = await _many(s, db)
        chosen = await contact_check.links(s, ids)
        _failing_second_write(monkeypatch, seen)
        with pytest.raises(sqlite3.OperationalError):
            await enqueue_contact_checks(s, chosen, max_spend=0.5)

    run(go())
    # (The first piece's jobs could not start while the run was being
    # queued, and were taken off with the batch when the write failed.)
    assert seen == [None]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM job_batches").fetchone() == (0,)


def test_a_run_queued_whole_can_start_at_once(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        ids = await _many(s, db)
        chosen = await contact_check.links(s, ids)
        batch, new, _ = await enqueue_contact_checks(s, chosen, max_spend=0.5)
        first = await jobs.claim(s, "classify", "test-worker")
        return batch, new, first

    batch, new, first = run(go())
    assert first is not None and first.id == new[0] and first.batch_id == batch
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM jobs WHERE not_before IS NOT NULL"
                           ).fetchone() == (0,)


def test_the_cli_takes_the_first_limit_of_the_contacts_given(tmp_path, capsys):
    # (--limit used to be ignored for --contact ids: every one was taken.)
    from harvey import cli

    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        ids = await _many(s, db, firms=5, per=10)
        args = argparse.Namespace(undo="", review=False, contact=ids, county="", locality="",
                                  website="", q="", limit=15, max_spend=0.3, dry_run=False,
                                  background=True)
        await cli._verify(s, args)
        return ids

    ids = run(go())
    assert "15 link(s)" in capsys.readouterr().out
    with sqlite3.connect(db) as con:
        taken = sorted(c for (raw,) in con.execute("SELECT params_json FROM jobs")
                       for c in (link["contact_id"] for link in json.loads(raw)["links"]))
    assert taken == ids[:15]
