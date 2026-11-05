"""The agent's read-only tools (harvey/agent/): what query() may read and
how it is refused (SQLite's authorizer, a read-only connection, the
sample and its total, the clock), personal values masked or dropped,
the count view against the pages' own counts, the schema's join notes,
every tool through the MCP SDK's own client in-process and once over
stdio, the record of each call under the chat's message, and that no view
or tool returns the text a website, a reply or the settings hold. No
model is called. All data is fictional."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from harvey import registers
from harvey.agent import readonly, tools, views
from harvey.state import StateManager

ROOT = Path(__file__).resolve().parents[1]
_ADAPTERS = registers.adapters
# Planted in the columns and tables the agent never reads: none may come back.
PLANTED = ("PLANTED-BODY", "PLANTED-KEY", "PLANTED-PAGE", "PLANTED-REPLY", "PLANTED-THREAD",
           "PLANTED-DESCRIPTION", "PLANTED-NOTE", "PLANTED-EXCERPT")

# Every harvey.db table the agent may read and its readable columns, the
# personal ones marked ":personal" (query() refuses them: readonly.PERSONAL).
# A new table or column fails this test until it is decided: readable (add
# it here, marked when it is personal) or left out (readonly.EXCLUDED_TABLES / EXCLUDED_COLUMNS).
READABLE = {
    "acknowledgement": "kind version text_sha at by",
    "actions": "id action_type agent created_at",
    "audience": "id name filter_json created_at updated_at archived",
    "batch": "id ref",
    "campaign_member": "campaign email:personal contact firm person prospect_id status personal added_by "
                       "added_at last_step last_sent_at ended_at end_reason",
    "campaigns": "id name channel instantly_campaign_id sequence_json prospect_ids_json status "
                 "created_at audience_id audience_query rolling rolled_at account_id profile_id "
                 "offer_json li_note review origin started_at ended_at updated_at",
    "capability": "id module name",
    "contact": "id kind value:personal display:personal line_type status do_not_contact first_seen last_seen "
               "n_firms best_trust",
    "contact_check": "firm contact batch at verdict profile model p undone_at",
    "contact_kinds": "id kind label channel module available",
    "email_patterns": "domain pattern source confidence mx_type is_catch_all updated_at",
    "feedback": "id entity_type entity_id comment created_at",
    "firm": "id live created display_name website domain website_trust website_source "
            "website_fill site_read_at site_read_deep profiled_at last_search_at finder_at "
            "n_contacts n_email n_phone n_mobile best_trust n_people name_order place_order "
            "website_basis",
    "firm_ident": "scheme key firm since",
    "firm_key": "kind value:personal firm source",
    "firm_signal": "firm signal n_keys n_positive",
    "firm_source": "firm source batch ref first_seen last_seen",
    "ident_scheme": "id name module numeric legal",
    "job_batches": "id kind label params_json max_spend created_at report_json",
    "jobs": "id kind lane module capability label params_json dedupe_key status "
            "est_cost reserved actual_cost attempts not_before paused_reason "
            "stop_requested batch_id parent_id worker created_at started_at finished_at "
            "updated_at",
    "legacy_id": "uuid firm",
    "link": "firm contact trust person location first_seen last_seen",
    "link_evidence": "firm contact source person location capability trust confidence "
                     "evidence_url first_seen last_seen",
    "local_firm": "firm name county locality address phone:personal industry source_url gone country",
    "locations": "id firm name address locality county country point external_id phone:personal source "
                 "first_seen last_seen",
    "looked_for": "bit name",
    "match_candidate": "a b score reasons:personal status at decided_at",
    "merge": "id survivor merged score reasons:personal decided_by grp at undone_at location",
    "merge_member": "merge firm",
    "migration_note": "firm kind value",
    "module": "id name",
    "obs": "id firm signal key num confidence module capability run evidence_url at",
    "outbox": "id campaign_id prospect_id conversation_id step kind to_email:personal status send_at "
              "sent_at provider created_at updated_at approved_by source_note account_id",
    "person": "id firm name:personal name_norm:personal role source first_seen last_seen",
    "prospects": "id company_id first_name:personal last_name:personal email:personal email_verified "
                 "phone:personal phone_verified linkedin_url:personal title seniority department source source_url:personal status score "
                 "company industry company_size created_at updated_at email_status timezone",
    "reading": "firm signal key num confidence module at checked_at obs positive",
    "register_tally": "signal module mark loaded n_true n_false at n_values",
    "revision": "name n",
    "role": "name id",
    "runs": "id stage status provider records cost_usd params_json started_at ended_at",
    "search": "id target is_person origin module capability looked_for at outcome found cost "
              "batch",
    "sending_account": "id label provider address:personal profile_id enabled created_at",
    "signal": "id code label description category value_type unit cardinality keyed_by "
              "when_known cost_note status confidence_floor",
    "signal_source": "signal module how precedence absent",
    "signal_tally": "signal n_true n_checked",
    "source": "id name",
    "suppression": "id scope value:personal reason campaign at by undone_at undone_by",
    "tally": "name n",
    "usage_events": "id created_at agent task session_id request_key model input_tokens "
                    "output_tokens cache_read_tokens cache_creation_tokens cost_usd duration_ms "
                    "num_turns is_error source provider",
    "usage_log": "id date claude_calls usage_percent created_at codex_calls",
    "worker_lease": "name owner pid host heartbeat_at started_at",
}


@pytest.fixture(autouse=True)
def _no_registers(monkeypatch):
    """No register of the checkout's data folder is ever attached."""
    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [])


def _seed(db: Path) -> None:
    """A firm with a contact and a fact, an email with a body, a reply's
    preview, a do-not-contact note, a setting: the text ones planted."""
    asyncio.run(StateManager(str(db)).init_db())
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute("INSERT INTO firm (id, live, created, display_name, website, domain) "
                "VALUES (1, 0, ?, 'Alfa Dent SRL', 'https://alfa.example.com', 'alfa.example.com')",
                (now,))
    con.execute("INSERT INTO local_firm (firm, name, county, locality, description) "
                "VALUES (1, 'Alfa Dent SRL', 'CJ', 'Cluj-Napoca', 'PLANTED-DESCRIPTION')")
    con.execute("INSERT INTO contact (id, kind, value, first_seen, last_seen) "
                "VALUES (1, 1, 'office@alfa.example.com', ?, ?)", (now, now))
    con.execute("INSERT INTO source (id, name) VALUES (1, 'website')")
    con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, last_seen) "
                "VALUES (1, 1, 1, 0, ?, ?)", (now, now))
    con.execute("INSERT INTO signal (id, code, label, value_type) VALUES (900, 'TEST_FACT', "
                "'A test fact', 'text')")
    con.execute("INSERT INTO obs (firm, signal, text, at) VALUES (1, 900, 'PLANTED-PAGE', ?)",
                (now,))
    con.execute("INSERT INTO settings (key, value) VALUES ('test.key', 'PLANTED-KEY')")
    con.execute("INSERT INTO outbox (id, to_email, subject, body, status) VALUES "
                "('o1', 'office@alfa.example.com', 'Salut', 'PLANTED-BODY', 'pending_review')")
    con.execute("INSERT INTO conversations (id, thread_json) VALUES ('c1', '[\"PLANTED-THREAD\"]')")
    con.execute("INSERT INTO actions (id, action_type, details_json) VALUES "
                "('a1', 'escalation', '{\"reply_preview\": \"PLANTED-REPLY\"}')")
    con.execute("INSERT INTO suppression (scope, value, reason, at, note) VALUES "
                "(1, 'nu@beta.example.com', 2, ?, 'PLANTED-NOTE')", (now,))
    con.execute("INSERT OR IGNORE INTO module (name) VALUES ('website')")
    module = con.execute("SELECT id FROM module WHERE name = 'website'").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO capability (module, name) VALUES (?, 'read_site')",
                (module,))
    cap = con.execute("SELECT id FROM capability WHERE module = ? AND name = 'read_site'",
                      (module,)).fetchone()[0]
    con.execute("INSERT INTO search (target, module, capability, at, outcome, detail) "
                "VALUES (1, ?, ?, ?, 1, 'PLANTED-EXCERPT')", (module, cap, now))
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "harvey.db"
    _seed(path)
    return path


def _query(db, sql, **kw):
    con, _ = readonly.connect(db)
    try:
        return readonly.run_query(con, sql, **kw)
    finally:
        con.close()


# ── What may be read ──

def test_every_readable_table_and_column_is_decided(db):
    con, _ = readonly.connect(db)
    try:
        found = {t: " ".join(c + (":personal" if readonly.personal(t, c) else "")
                             for c in cols)
                 for t, cols in readonly.readable(con).items()}
    finally:
        con.close()
    assert found == READABLE
    # (Every personal column is one the agent reads: none is left over.)
    assert all(c in READABLE[t].replace(":", " ").split() for t, c in readonly.PERSONAL)


def test_a_read_of_what_is_allowed_comes_back(db):
    out = _query(db, "SELECT f.display_name, c.kind FROM firm f JOIN link l ON l.firm = f.id "
                     "JOIN contact c ON c.id = l.contact WHERE f.live = 0")
    assert {k: out[k] for k in ("columns", "rows", "truncated", "total")} == {
        "columns": ["display_name", "kind"],
        "rows": [["Alfa Dent SRL", 1]], "truncated": False, "total": 1}
    assert "sample" in out["note"]
    counted = _query(db, "WITH x AS (SELECT status FROM outbox) SELECT status, COUNT(*) FROM x "
                         "GROUP BY status")
    # (An aggregate is the answer: in full, with no sample note.)
    assert counted == {"columns": ["status", "COUNT(*)"], "rows": [["pending_review", 1]],
                       "truncated": False}


@pytest.mark.parametrize("sql, why", [
    ("SELECT * FROM settings", "settings isn't readable"),
    ("SELECT COUNT(*) FROM settings", "settings isn't readable"),
    ("SELECT value FROM settings WHERE key = 'x'", "settings isn't readable"),
    ("SELECT body FROM outbox", "outbox.body isn't readable"),
    ("SELECT * FROM outbox", "isn't read"),
    ("SELECT id FROM outbox WHERE body LIKE '%B%'", "outbox.body isn't readable"),
    ("SELECT (SELECT body FROM outbox LIMIT 1)", "outbox.body isn't readable"),
    ("WITH x AS (SELECT body AS b FROM outbox) SELECT length(b) FROM x", "outbox.body isn't"),
    ("SELECT thread_json FROM conversations", "conversations isn't readable"),
    ("SELECT COUNT(*) FROM processed_replies", "processed_replies isn't readable"),
    ("SELECT details_json FROM actions", "actions.details_json isn't readable"),
    ("SELECT text FROM obs", "obs.text isn't readable"),
    ("SELECT r.text FROM reading r", "reading.text isn't readable"),
    ("SELECT detail FROM search", "search.detail isn't readable"),
    ("SELECT note FROM suppression", "suppression.note isn't readable"),
    ("SELECT description FROM local_firm", "local_firm.description isn't readable"),
    ("SELECT personalization_notes FROM prospects", "personalization_notes isn't readable"),
    ("SELECT progress_json FROM jobs", "jobs.progress_json isn't readable"),
    ("SELECT * FROM agent_message", "agent_message isn't readable"),
    ("SELECT * FROM agent_tool_call", "agent_tool_call isn't readable"),
    ("SELECT sql FROM sqlite_master", "sqlite_master isn't readable"),
    ("SELECT COUNT(*) FROM local_fts", ""),
    ("SELECT * FROM pragma_table_info('firm')", ""),
])
def test_excluded_tables_and_columns_are_refused_by_the_authorizer(db, sql, why):
    with pytest.raises(readonly.Refused) as e:
        _query(db, sql)
    assert why in str(e.value)


@pytest.mark.parametrize("sql", [
    "INSERT INTO firm (live, created) VALUES (0, 1)",
    "UPDATE firm SET display_name = 'x'",
    "DELETE FROM firm",
    "WITH x AS (SELECT 1) DELETE FROM firm",
    "WITH x AS (SELECT 1) UPDATE firm SET live = 0",
    "PRAGMA query_only = 0",
    "PRAGMA table_info(firm)",
    "ATTACH DATABASE ':memory:' AS extra",
    "DETACH DATABASE main",
    "CREATE TABLE x (a)",
    "CREATE TEMP VIEW v AS SELECT body FROM outbox",
    "DROP TABLE firm",
    "BEGIN",
    "VACUUM",
    "SELECT 1; DELETE FROM firm",
    "SELECT load_extension('x')",
    "",
])
def test_anything_but_one_read_is_refused(db, sql):
    with pytest.raises(readonly.Refused):
        _query(db, sql)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*), MAX(display_name) FROM firm").fetchone() == \
        (1, "Alfa Dent SRL")
    con.close()


def test_the_connection_itself_is_read_only(db):
    con, _ = readonly.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE firm SET display_name = 'x'")
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        con.close()


def test_rows_are_a_sample_with_their_total(db):
    big = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3000) " \
          "SELECT i FROM n"
    out = _query(db, big)
    assert len(out["rows"]) == readonly.SAMPLE_ROWS and out["truncated"] is True
    assert out["total"] == 3000 and "shape" in out["note"]
    assert len(_query(db, big, limit=5)["rows"]) == 5
    assert len(_query(db, big, limit=100_000)["rows"]) == readonly.SAMPLE_CAP
    # (A trailing semicolon or comment doesn't break the count.)
    assert _query(db, big + "; ")["total"] == 3000
    assert _query(db, big + " -- every number")["total"] == 3000
    # An aggregate's groups come back in full, up to MAX_ROWS.
    grouped = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 300) " \
              "SELECT i % 150, COUNT(*) FROM n GROUP BY 1"
    out = _query(db, grouped)
    assert len(out["rows"]) == 150 and "total" not in out
    long = _query(db, "SELECT printf('%.*c', 5000, 'x'), x'00ff'")["rows"][0]
    assert len(long[0]) == readonly.CELL_CHARS + 1 and long[1] == "<2 bytes>"


def test_a_total_that_cant_be_counted_in_time_says_so(db):
    # (The first rows come at once; counting them all never ends.)
    forever = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT i FROM n"
    out = _query(db, forever, seconds=0.3)
    assert len(out["rows"]) == readonly.SAMPLE_ROWS
    assert out["total"] is None and "isn't known" in out["total_note"]


def test_a_refused_write_says_what_it_would_have_done(db):
    # (The authorizer's action codes share numbers with result codes: an
    # INSERT is named as one, not as "toobig".)
    with pytest.raises(readonly.Refused) as e:
        _query(db, "WITH x AS (SELECT 1) INSERT INTO firm (id, live, created) VALUES (9, 0, 0)")
    assert "would insert" in str(e.value)


@pytest.mark.parametrize("sql", [
    "SELECT zeroblob(500000000)",
    "SELECT length(replace(hex(zeroblob(400000)), '0', '00000'))",
    "SELECT " + ", ".join(["1"] * 150),
])
def test_a_query_cant_make_a_huge_value_or_a_wide_row(db, sql):
    with pytest.raises(readonly.Refused) as e:
        _query(db, sql)
    assert "too large" in str(e.value)
    # (The limits are the query's only: the connection gets its own back.)
    con, _ = readonly.connect(db)
    try:
        before = con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        with pytest.raises(readonly.Refused):
            readonly.run_query(con, sql)
        assert con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == before
    finally:
        con.close()
    # (printf() past the limit makes nothing: NULL.)
    assert _query(db, "SELECT length(printf('%.*c', 400000000, 'x'))")["rows"] == [[None]]


def test_a_result_stops_at_its_size_budget(db):
    # (An aggregate's 200 groups, five 900-character columns each.)
    wide = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 900) " \
           "SELECT max(i), " + ", ".join(["printf('%.*c', 900, 'x')"] * 5) + " FROM n GROUP BY i"
    out = _query(db, wide)
    assert out["truncated"] is True
    assert len(out["rows"]) < readonly.MAX_ROWS
    assert sum(len(c) for r in out["rows"] for c in r[1:]) <= \
        readonly.RESULT_BUDGET + 5 * readonly.CELL_CHARS


def test_the_read_only_state_cant_write_through_its_own_transactions(db):
    # (The do-not-contact list and the acknowledgement are written through
    # StateManager._tx: on the agent's state that connection is read-only.)
    state = readonly.ReadOnlyState(str(db))
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(state.suppress("address", "nou@gamma.example.com", "objection", by="agent"))
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 1
    finally:
        con.close()
    assert asyncio.run(state._in_tx(lambda c: c.execute(
        "SELECT COUNT(*) FROM firm").fetchone()[0], write=False)) == 1


def test_a_slow_query_is_stopped(db):
    forever = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) " \
              "SELECT COUNT(*) FROM n"
    with pytest.raises(readonly.Refused) as e:
        _query(db, forever, seconds=0.2)
    assert "longer than 0.2 s" in str(e.value)


def test_a_register_is_attached_read_only_and_read_in_full(db, tmp_path, monkeypatch):
    reg = tmp_path / "registry.db"
    con = sqlite3.connect(reg)
    con.execute("CREATE TABLE firms (id INTEGER PRIMARY KEY, name TEXT, county TEXT)")
    con.execute("INSERT INTO firms VALUES (1, 'Beta Construct SRL', 'BV')")
    con.execute("CREATE TABLE notes (text TEXT)")
    con.execute("INSERT INTO notes VALUES ('register text')")
    con.commit()
    con.close()

    class Fake:
        module, alias, keyed_by = "onrc", "reg", ""
        resolve = registers.Adapter.resolve

        def attach(self, c):
            registers.attach_read_only(c, self.alias, reg)
            return True

    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [Fake()])
    assert _query(db, "SELECT name FROM reg.firms WHERE county = 'BV'")["rows"] == \
        [["Beta Construct SRL"]]
    assert _query(db, "SELECT COUNT(*) FROM notes")["rows"] == [[1]]
    for sql in ("DELETE FROM reg.firms", "INSERT INTO reg.notes VALUES ('x')"):
        with pytest.raises(readonly.Refused):
            _query(db, sql)
    assert sqlite3.connect(reg).execute("SELECT COUNT(*) FROM firms").fetchone()[0] == 1
    described = views.schema(db)["registers"]
    assert described == {"reg": {"module": "onrc", "tables": {
        "firms": ["id", "name", "county"], "notes": ["text"]}}}


# ── The tools, through the SDK's client ──

def _chat(db, chat="chat1", turn="turn1") -> str:
    """A chat answering ``turn`` (what the server records its calls under)."""
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO agent_chat (id, title, created_at, last_at, turn) "
                "VALUES (?, 'Firme', 1, 1, ?)", (chat, turn))
    con.commit()
    con.close()
    return chat


def _call(db, calls, turn="turn1"):
    """[(tool, arguments)] through the MCP client in-process, for a chat
    answering ``turn``: [(is_error, text)]."""
    from mcp import Client

    chat = _chat(db, turn=turn)

    async def run():
        out = []
        async with Client(tools.build(str(db), chat)) as c:
            for name, args in calls:
                r = await c.call_tool(name, args)
                out.append((r.is_error, r.content[0].text))
        return out
    return asyncio.run(run())


def _records(db, turn="turn1"):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT tool, outcome, arguments, result FROM agent_tool_call "
                           "WHERE turn = ? ORDER BY id", (turn,)).fetchall()
    finally:
        con.close()


def test_a_call_is_filed_under_the_message_it_started_for_and_none_without_one(db):
    """A slow call that ends after its answer was stopped (and the chat's
    next message started) stays with the message it was made for; one made
    with no message being answered (an answer stopped, a chat deleted)
    isn't filed at all."""
    chat = _chat(db, "c1", "first")
    t = tools.Tools(str(db), chat)

    async def slow():
        _chat(db, "c1", "second")
        return {"n": 1}

    async def run():
        await t.call("counts", {}, slow)
        _chat(db, "c1", "")
        await t.call("pipeline", {}, lambda: asyncio.sleep(0, {"n": 2}))
    asyncio.run(run())
    assert [r[:2] for r in _records(db, "first")] == [("counts", "ok")]
    assert _records(db, "second") == [] and _records(db, "") == []


READ_TOOLS = ["schema", "counts", "query", "pipeline", "firms", "coverage", "contacts_summary",
              "campaigns", "spend", "estimate", "jobs", "plan_status", "sets", "notes",
              "read_playbook", "playbooks", "writing_rules", "campaign_preview",
              "campaign_status", "replies", "areas", "plan_cells", "terms", "phrase_yield",
              "tasks"]
ACTION_TOOLS = ["propose_plan", "close_plan", "save_set", "discover", "registry_pull",
                "find_contacts", "verify_contacts", "cancel_run", "schedule_wake", "remember",
                "forget", "save_playbook", "save_audience", "create_campaign", "edit_campaign",
                "request_start", "pause_campaign", "draft_reply", "suppress", "assign_task",
                "cancel_task"]


def test_the_server_offers_its_reading_tools_and_its_actions_only(db):
    """Every tool is a read, or one of the actions under a plan: none
    approves, sends, starts or resumes a campaign, undoes a do-not-contact
    entry, merges or changes a setting, or fetches a page."""
    from mcp import Client

    async def run():
        async with Client(tools.build(str(db), "t")) as c:
            return (await c.list_tools()).tools
    listed = asyncio.run(run())
    assert sorted(t.name for t in listed) == sorted(READ_TOOLS + ACTION_TOOLS)
    by = {t.name: t for t in listed}
    assert all(by[n].annotations.read_only_hint and not by[n].annotations.destructive_hint
               for n in READ_TOOLS)
    assert not any(by[n].annotations.read_only_hint for n in ACTION_TOOLS)
    for word in ("approve", "send", "outbox", "autopilot", "unsuppress", "undo", "resume",
                 "merge", "setting", "fetch", "acknowledg"):
        assert not any(word in n for n in ACTION_TOOLS), word
    assert not any(n.startswith("start") for n in ACTION_TOOLS)


def test_every_tool_answers_and_none_returns_the_planted_text(db):
    calls = [("schema", {}), ("pipeline", {}), ("firms", {"filter": {"county": "CJ"}}),
             ("firms", {}), ("coverage", {"filter": {"county": "CJ"}}), ("coverage", {}),
             ("counts", {"filter": {"county": "CJ"}}), ("counts", {}),
             ("contacts_summary", {}), ("campaigns", {}), ("spend", {}),
             ("jobs", {}), ("query", {"sql": "SELECT * FROM firm"}),
             ("query", {"sql": "SELECT name, county, locality FROM local_firm"}),
             ("estimate", {"action": "registry", "params": {"county": "CJ"}})]
    answers = _call(db, calls)
    assert [e for e, _ in answers] == [False] * len(calls)
    every = " ".join(text for _, text in answers)
    for planted in PLANTED:
        assert planted not in every, planted
    got = dict(zip([n for n, _ in calls], [json.loads(t) for _, t in answers]))
    assert "settings" in got["schema"]["left_out"]["tables"]
    assert "outbox.body" in got["schema"]["left_out"]["columns"]
    assert "body" not in got["schema"]["harvey_db"]["outbox"]["columns"]
    assert any(f["code"] == "TEST_FACT" for f in got["schema"]["facts"])
    assert got["pipeline"]["firms"]["firms"] == 1
    assert got["pipeline"]["outbox"] == {"sequence": {"pending_review": 1}}
    assert got["firms"]["total"] == 1 and got["firms"]["sample"][0]["name"] == "Alfa Dent SRL"
    assert got["coverage"]["firms"]["firms"] == 1
    assert got["coverage"]["firms"]["without_contact"] == 0
    assert got["coverage"]["searches"][0]["module"] == "website"
    assert got["counts"]["firms"]["with_email"] == 1
    assert got["counts"]["contacts"]["by_kind"] == {"email": 1}
    assert got["contacts_summary"]["by_kind"] == {"email": 1}
    assert got["spend"]["spent_today_usd"] == 0.0
    assert [r[:2] for r in _records(db)] == [(n, "ok") for n, _ in calls]


def test_a_refusal_and_an_error_are_recorded_and_said(db):
    answers = _call(db, [("query", {"sql": "SELECT value FROM settings"}),
                         ("query", {"sql": "SELECT nope FROM firm"}),
                         ("estimate", {"action": "spend_it"}),
                         ("jobs", {"action": "get", "id": "nothing"})])
    assert all(error for error, _ in answers)
    assert "refused: the table settings isn't readable" in answers[0][1]
    assert "no such column" in answers[1][1]
    rows = _records(db)
    assert [(t, o) for t, o, *_ in rows] == [("query", "refused"), ("query", "refused"),
                                            ("estimate", "refused"), ("jobs", "refused")]
    assert json.loads(rows[0][2]) == {"sql": "SELECT value FROM settings",
                                      "limit": readonly.SAMPLE_ROWS}
    assert "PLANTED" not in " ".join(r[3] for r in rows)


def test_estimates_spend_nothing_and_queue_nothing(db, monkeypatch):
    import httpx

    def no_network(*a, **k):
        raise AssertionError("an estimate made a request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    from harvey.config import load_config
    cfg = load_config(str(ROOT / "harvey.yaml"))
    cfg.icp.industries = ["dentist"]
    cfg.icp.geography = ["Cluj-Napoca"]
    monkeypatch.setattr("harvey.modules.job_kinds._config", lambda: cfg)
    answers = _call(db, [
        ("estimate", {"action": "discover", "params": {"provider": "osm"}}),
        ("estimate", {"action": "discover", "params": {"provider": "serper",
                                                        "terms": ["stomatolog"],
                                                        "cities": ["Brașov"]}}),
        ("estimate", {"action": "find_contacts", "params": {"county": "CJ"}}),
        ("estimate", {"action": "verify", "params": {"contact_ids": [1]}}),
    ])
    assert [e for e, _ in answers] == [False] * 4, answers
    osm, serper, find, verify = [json.loads(t) for _, t in answers]
    assert osm["free"] is True and osm["query_count"] == 1
    assert serper["queries"] and serper["estimated_cost_usd"] > 0
    assert "companies" in find and "classifier" in find
    assert verify["links"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM job_batches").fetchone()[0] == 0
    con.close()


def test_jobs_are_listed_and_a_report_is_read_without_storing_it(db):
    from harvey.modules import jobs

    async def queue():
        state = StateManager(str(db))
        batch = await jobs.new_batch(state, "discover", "Test run")
        job_id = await jobs.enqueue(state, "discover", "osm", {"q": 1}, batch_id=batch,
                                    label="dentist in Cluj")
        await jobs.finish(state, job_id, jobs.DONE, result={})
        return batch, job_id
    batch, job_id = asyncio.run(queue())
    listed, got, report = [json.loads(t) for _, t in _call(db, [
        ("jobs", {}), ("jobs", {"action": "get", "id": job_id}),
        ("jobs", {"action": "report", "id": batch})])]
    assert [j["id"] for j in listed["jobs"]] == [job_id]
    assert got["job"]["label"] == "dentist in Cluj" and "last_line" not in got["job"]
    assert report["report"]["jobs"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT report_json FROM job_batches WHERE id = ?",
                       (batch,)).fetchone()[0] is None
    con.close()


def test_the_server_speaks_mcp_over_stdio(db):
    """The process the CLI starts: python -m harvey.agent.tools, with
    nothing but the protocol on its stdout."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    chat = _chat(db, "stdio-chat", "stdio-turn")

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "harvey.agent.tools", "--db", str(db),
                                          "--chat", chat],
            env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                r = await session.call_tool("query", {"sql": "SELECT COUNT(*) AS n FROM firm"})
                return [t.name for t in listed.tools], r
    names, r = asyncio.run(asyncio.wait_for(run(), 60))
    assert "query" in names and not r.is_error
    assert json.loads(r.content[0].text)["rows"] == [[1]]
    assert [t[:2] for t in _records(db, "stdio-turn")] == [("query", "ok")]


# ── Personal values ──

@pytest.mark.parametrize("value, shown", [
    ("office@alfa.example.com", "o***@alfa.example.com"),
    ("+40722000102", "+4072****"),
    ("0722 000 102", "0722****"),
    ("tel:+40722000102 or ana.pop@beta.example.com", "tel:+4072**** or a***@beta.example.com"),
    ("https://www.linkedin.com/in/ana-popescu-123", "https://www.linkedin.com/in/***"),
    # Not personal: a CUI, a registration number, a date, a count, a firm, a site.
    ("10004070", "10004070"), ("J08/123/2010", "J08/123/2010"), ("2026-11-04", "2026-11-04"),
    ("8621", "8621"), ("Alfa Dent SRL", "Alfa Dent SRL"),
    ("https://alfa.example.com/contact", "https://alfa.example.com/contact"),
])
def test_emails_and_phones_are_masked_and_nothing_else(value, shown):
    assert readonly.mask(value) == shown
    assert readonly.mask_result({"rows": [[value, 3]], "n": 3}) == {"rows": [[shown, 3]], "n": 3}


def _personal_seed(db) -> None:
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute("INSERT INTO person (id, firm, name, name_norm, role, first_seen, last_seen) "
                "VALUES (1, 1, 'Ana Popescu', 'ana popescu', 'administrator', ?, ?)", (now, now))
    con.execute("INSERT INTO contact (id, kind, value, display, line_type, first_seen, last_seen) "
                "VALUES (2, 2, '+40722000102', '0722 000 102', 1, ?, ?)", (now, now))
    con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, last_seen) "
                "VALUES (1, 2, 1, 0, ?, ?)", (now, now))
    con.execute("INSERT INTO firm_key (kind, value, firm) VALUES (1, 'ana@alfa.example.com', 1)")
    con.execute("INSERT INTO match_candidate (a, b, score, reasons, status, at) VALUES "
                "(1, 2, 0.7, '[{\"code\": \"phone\", \"value\": \"+40722000102\"}]', "
                "'pending', ?)", (now,))
    con.commit()
    con.close()


# Every way SQL can rebuild a personal value from the column: none may run.
DERIVED = [
    "SELECT value FROM contact",
    "SELECT * FROM contact",
    "SELECT name FROM person",
    "SELECT p.name, COUNT(*) FROM person p",
    "WITH x AS (SELECT name AS n FROM person) SELECT n FROM x",
    "SELECT substr(value, 2) FROM contact",
    "SELECT value || 'x' FROM contact",
    "SELECT replace(value, '@', ' at ') FROM contact",
    "SELECT hex(value) FROM contact",
    "SELECT value + 0 FROM contact",
    "SELECT CAST(substr(value, 2) AS INTEGER), COUNT(*) FROM contact GROUP BY 1",
    "SELECT group_concat(value, '|') FROM contact",
    "SELECT json_group_array(value) FROM contact",
    "SELECT COUNT(*) FROM contact GROUP BY value",
    "SELECT CASE WHEN value LIKE '+4072%' THEN 'a' ELSE 'b' END FROM contact",
    "SELECT unicode(substr(name, 1, 1)) FROM person",
    "SELECT COUNT(*) FROM contact WHERE value = '+40722000102'",
    "SELECT (SELECT value FROM contact LIMIT 1)",
    "SELECT value FROM firm_key",
    "SELECT reasons FROM match_candidate",
    "SELECT max(display) FROM contact",
    "SELECT to_email FROM outbox",
]


@pytest.mark.parametrize("sql", DERIVED)
def test_a_query_that_reads_a_personal_column_is_refused_however_it_reads_it(db, sql):
    _personal_seed(db)
    with pytest.raises(readonly.Refused) as e:
        _query(db, sql)
    assert "is personal" in str(e.value) and "count rows" in str(e.value)
    for value in ("40722000102", "Ana", "ana@", "0722 000"):
        assert value not in str(e.value)


def test_counts_by_the_columns_that_arent_personal_still_answer(db):
    _personal_seed(db)
    assert _query(db, "SELECT kind, line_type, COUNT(*) FROM contact GROUP BY kind, line_type "
                      "ORDER BY kind")["rows"] == [[1, None, 1], [2, 1, 1]]
    assert _query(db, "SELECT role, COUNT(*) FROM person GROUP BY role")["rows"] == \
        [["administrator", 1]]
    assert _query(db, "SELECT COUNT(*) FROM contact")["rows"] == [[2]]


def test_no_tool_returns_a_personal_value(db):
    _personal_seed(db)
    answers = _call(db, [
        ("query", {"sql": "SELECT value, display FROM contact ORDER BY id"}),
        ("query", {"sql": "SELECT group_concat(value) FROM contact"}),
        ("firms", {}), ("contacts_summary", {}), ("pipeline", {}), ("schema", {}),
        ("query", {"sql": "SELECT kind, COUNT(*) FROM contact GROUP BY kind"}),
    ])
    assert [e for e, _ in answers] == [True, True] + [False] * 5
    every = " ".join(t for _, t in answers)
    for personal in ("office@alfa", "40722000102", "0722 000 102", "Ana Popescu", "ana popescu",
                     "ana@alfa"):
        assert personal not in every, personal
    # The record of each call keeps no personal value either.
    assert "40722000102" not in " ".join(r[2] + r[3] for r in _records(db))


def test_an_error_is_masked_before_the_model_or_the_record_sees_it(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("could not read office@alfa.example.com at +40722000102")

    monkeypatch.setattr(views, "pipeline", boom)
    answers = _call(db, [("pipeline", {})])
    assert answers[0][0] is True
    for text in (answers[0][1], " ".join(r[3] for r in _records(db))):
        assert "office@alfa" not in text and "40722000102" not in text
        assert "o***@alfa.example.com" in text


def test_a_cut_result_stays_within_its_size(db, monkeypatch):
    monkeypatch.setattr(views, "pipeline", lambda db_path: {"x": "y" * (tools.RESULT_CHARS * 3)})
    (error, text), = _call(db, [("pipeline", {})])
    assert not error and len(text) <= tools.RESULT_CHARS
    assert json.loads(text)["cut"] is True


def test_a_registers_personal_columns_are_refused(db, tmp_path, monkeypatch):
    from tests.test_query_plans import build_registry

    reg = tmp_path / "plans-registry.db"
    build_registry(reg)

    class Onrc:
        module, alias, keyed_by = "onrc", "reg", "ro.cui"

        def attach(self, c):
            registers.attach_read_only(c, self.alias, reg)
            return True

    monkeypatch.setattr(registers, "adapters", lambda *a, **k: [Onrc()])
    for sql in ("SELECT first_name, role FROM reg.administrators",
                "SELECT COUNT(*) FROM reg.administrators GROUP BY last_name",
                "SELECT a.role FROM reg.administrators a WHERE a.last_name LIKE 'P%'"):
        with pytest.raises(readonly.Refused, match="administrators.*is personal"):
            _query(db, sql)
    assert _query(db, "SELECT role, COUNT(*) FROM reg.administrators GROUP BY role "
                      "ORDER BY role")["rows"] == [["administrator", 1], ["asociat", 1]]
    assert _query(db, "SELECT COUNT(*) FROM reg.administrators")["rows"] == [[2]]


# ── Counts, as the pages count them ──

@pytest.fixture
def counted(tmp_path) -> Path:
    """Firms in two counties, with and without a website, an email, a
    phone (mobile or fixed), and contacts of each status."""
    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())
    con = sqlite3.connect(path)
    now = int(time.time())
    con.execute("INSERT INTO source (id, name) VALUES (1, 'website')")
    firms = [(1, "CJ", "https://a.example.com"), (2, "CJ", None), (3, "CJ", None),
             (4, "BV", "https://d.example.com"), (5, "BV", None), (6, "CJ", None)]
    for fid, county, site in firms:
        con.execute("INSERT INTO firm (id, live, created, website) VALUES (?, 0, ?, ?)",
                    (fid, fid, site))
        con.execute("INSERT INTO local_firm (firm, name, county, locality) VALUES (?, ?, ?, ?)",
                    (fid, f"Firma {fid} SRL", county, "Oras"))
    contacts = [(1, 1, "a@a.example.com", None, 1, 1), (2, 2, "+40721000001", 1, 0, 1),
                (3, 1, "b@b.example.com", None, 3, 2), (4, 2, "+40264000002", 2, 0, 4),
                (5, 1, "e@e.example.com", None, 2, 5), (6, 2, "+40721000006", 1, 0, 6)]
    for cid, kind, value, line, status, firm in contacts:
        con.execute("INSERT INTO contact (id, kind, value, line_type, status, first_seen, "
                    "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, kind, value, line, status,
                                                             now, now))
        con.execute("INSERT INTO link_evidence (firm, contact, source, trust, first_seen, "
                    "last_seen) VALUES (?, ?, 1, 0, ?, ?)", (firm, cid, now, now))
    con.commit()
    con.close()
    return path


FILTERS = [{}, {"county": "CJ"}, {"county": "CJ", "website": "no"}, {"website": "yes"},
           {"county": "BV", "contact": "none"}, {"contact": "email"}, {"contact": "mobile"},
           {"county": "CJ", "kind": "phone"}, {"kind": "mobile"}, {"status": "verified"},
           {"county": "CJ", "contact": "any", "website": "no"}]


@pytest.mark.parametrize("raw", FILTERS)
def test_counts_are_the_companies_and_contacts_pages_counts(counted, raw):
    from harvey.contact_query import filter_from_query
    from harvey.overlay import reads

    state = readonly.ReadOnlyState(str(counted))
    got = asyncio.run(views.counts(state, raw))
    f, _ = asyncio.run(filter_from_query(state, {k: str(v) for k, v in raw.items()}))
    con, attached = readonly.connect(counted)
    try:
        reader = reads.Reader(con, attached)
        companies, _, _ = reader.companies_page(f, "created", "desc", 50, 0)
        contacts_total, _, _ = reads.Reader(con, attached).contacts_page(f, "", "desc", 50, 0)
    finally:
        con.close()
    assert got["firms"]["firms"] == companies, raw
    assert got["contacts"]["contacts"] == contacts_total, raw
    assert got["firms"]["firms"] == got["firms"]["with_website"] + got["firms"]["without_website"]


def test_one_counts_call_answers_a_whole_question(counted):
    state = readonly.ReadOnlyState(str(counted))
    got = asyncio.run(views.counts(state, {"county": "CJ"}))
    assert got["filter"] == {"county": "CJ"}
    assert {k: got["firms"][k] for k in ("firms", "with_website", "without_website",
                                          "without_contact", "with_email", "with_phone",
                                          "with_mobile", "with_email_or_mobile")} == {
        "firms": 4, "with_website": 1, "without_website": 3, "without_contact": 1,
        "with_email": 2, "with_phone": 2, "with_mobile": 2, "with_email_or_mobile": 3}
    contacts = got["contacts"]
    assert contacts["by_kind"] == {"email": 2, "phone": 2} and contacts["mobile"] == 2
    # (Verified and risky emails can be sent to; a guess can't.)
    assert contacts["deliverable_email"] == 1
    assert contacts["by_status"] == {"verified": 1, "guess": 1, "unchecked": 2}
    everything = asyncio.run(views.counts(state, {}))
    assert everything["firms"]["firms"] == 6 and everything["firms"]["with_email_or_mobile"] == 4
    assert everything["contacts"]["contacts"] == 6
    with pytest.raises(readonly.Refused) as e:
        asyncio.run(views.counts(state, {"judet": "CJ"}))
    assert "no filter judet" in str(e.value)


def test_firms_returns_a_sample_and_its_total(counted):
    state = readonly.ReadOnlyState(str(counted))
    got = asyncio.run(views.firms(state, {"county": "CJ"}, limit=2))
    assert (got["total"], got["shown"], len(got["sample"])) == (4, 2, 2)
    assert "sample" in got["note"]
    # (Never more than FIRMS_LIMIT, whatever is asked.)
    assert asyncio.run(views.firms(state, {}, limit=500))["shown"] == 6 <= views.FIRMS_LIMIT


# ── The schema's notes ──

def test_every_column_a_schema_note_names_exists(db, tmp_path, monkeypatch):
    import re

    from tests.test_query_plans import build_financials, build_registry, build_taxpayers

    # (The registers' own adapters, over files built in their own shape.)
    folder = tmp_path / "registers"
    folder.mkdir()
    build_registry(folder / "registry.db")
    build_financials(folder / "financials.db")
    build_taxpayers(folder / "taxpayers.db")
    monkeypatch.setattr(registers, "adapters", lambda *a, **k: _ADAPTERS(folder))
    described = views.schema(db)
    assert {"reg", "fin", "tax"} <= set(described["registers"])
    named = set(re.findall(r"`([a-z_.]+)`", " ".join(views.SCHEMA_NOTES)))
    assert len(named) > 20
    for name in named:
        parts = name.split(".")
        if len(parts) == 2:
            table, column = parts
            assert column in described["harvey_db"][table]["columns"], name
        else:
            alias, table, column = parts
            assert column in described["registers"][alias]["tables"][table], name
    # The whole schema fits in one tool result, registers and all.
    assert len(json.dumps(described, ensure_ascii=False)) < tools.RESULT_CHARS
    # Each personal column is named for the agent, the registers' too.
    assert set(described["harvey_db"]["person"]["personal"]) == {"name", "name_norm"}
    assert "value" in described["harvey_db"]["contact"]["personal"]
    assert {"administrators.first_name", "administrators.last_name"} <= set(
        described["registers"]["reg"]["personal"])
    assert "taxpayers.phone" in described["registers"]["tax"]["personal"]


def test_calls_are_recorded_under_the_message_the_chat_answers_at_that_moment(db):
    from mcp import Client

    chat = _chat(db, "chat2", "first")

    async def run():
        async with Client(tools.build(str(db), chat)) as c:
            await c.call_tool("pipeline", {})
            _chat(db, "chat2", "second")
            await c.call_tool("campaigns", {})
    asyncio.run(run())
    assert [r[0] for r in _records(db, "first")] == ["pipeline"]
    assert [r[0] for r in _records(db, "second")] == ["campaigns"]
