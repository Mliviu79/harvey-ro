"""The Claude plan's usage gauge, from what the CLI reports: rate-limit
events in a run's stream and its /usage command. Harvey never reads the
Claude login. No CLI runs here: its output is fixture text (fictional
values), and the /usage check's subprocess is a stand-in."""

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

import harvey.integrations.quota as quota
from harvey import claude_runtime
from harvey.brain import Brain
from harvey.state import StateManager

# The real check, before conftest swaps in one that does nothing.
_REAL_PROBE = quota.probe

# (The hour now: a recorded reading is stamped with the real clock.)
NOW = int(time.time()) // 3600 * 3600
WEEK_RESET = NOW + 3 * 86400
SESSION_RESET = NOW + 2 * 3600


def _event(info: dict) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "uuid": "00000000-0000-4000-8000-000000000001", "session_id": "s-1"}


WEEK_ONLY = {"status": "allowed", "resetsAt": WEEK_RESET, "rateLimitType": "seven_day",
             "utilization": 0.17, "overageStatus": "rejected",
             "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
             "unifiedWindows": {"seven_day": {"utilization": 0.17, "resetsAt": WEEK_RESET,
                                              "status": "allowed"}}}
SESSION_ONLY = {"status": "allowed", "resetsAt": SESSION_RESET, "rateLimitType": "five_hour",
                "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
                "isUsingOverage": False,
                "unifiedWindows": {"five_hour": {"utilization": 0.05,
                                                 "resetsAt": SESSION_RESET}}}

VERIFIED_USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current week (all models): 17% used · resets Nov 12, 2:59am (UTC)\n"
    "Current week (Fable): 0% used · resets Nov 12, 3am (UTC)")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "q.db"))
        await sm.init_db()
        yield sm


# ── Rate-limit events ──


def test_event_readings_are_fractions_with_their_reset_and_status():
    readings = quota.readings_from_info(WEEK_ONLY, NOW)
    assert set(readings) == {"seven_day"}
    w = readings["seven_day"]
    assert w["utilization"] == pytest.approx(0.17)
    assert w["resets_at"] == WEEK_RESET and w["status"] == "allowed"
    assert w["seen_at"] == NOW and w["source"] == quota.SOURCE_ANSWER
    assert quota.rate_limit_info({"type": "result"}) is None
    assert quota.rate_limit_info(_event(WEEK_ONLY)) is WEEK_ONLY


def test_a_window_with_no_status_takes_the_events_own_for_its_type():
    readings = quota.readings_from_info(SESSION_ONLY, NOW)
    assert readings["five_hour"]["status"] == "allowed"
    assert readings["five_hour"]["utilization"] == pytest.approx(0.05)


def test_the_type_named_alone_is_read_from_the_event_itself():
    info = {"status": "allowed_warning", "resetsAt": WEEK_RESET,
            "rateLimitType": "seven_day", "utilization": 0.81}
    assert quota.readings_from_info(info, NOW)["seven_day"]["utilization"] == \
        pytest.approx(0.81)
    # No utilization anywhere and not rejected: nothing to read.
    assert quota.readings_from_info({"status": "allowed", "rateLimitType": "seven_day"},
                                    NOW) == {}


def test_windows_merge_one_by_one_and_drop_past_their_reset():
    stored = quota.merge({}, quota.readings_from_info(WEEK_ONLY, NOW), NOW, WEEK_ONLY)
    stored = quota.merge(stored, quota.readings_from_info(SESSION_ONLY, NOW + 60),
                         NOW + 60, SESSION_ONLY)
    # (One answer reported the week, a later one only the session: both kept.)
    assert set(stored["windows"]) == {"seven_day", "five_hour"}
    assert stored["windows"]["seven_day"]["seen_at"] == NOW
    assert stored["status"] == "allowed" and stored["rate_limit_type"] == "five_hour"
    assert stored["overage"] == {"status": "rejected",
                                 "disabled_reason": "org_level_disabled", "using": False}
    # A newer reading of the week replaces its last.
    newer = dict(WEEK_ONLY, unifiedWindows={"seven_day": {"utilization": 0.2,
                                                          "resetsAt": WEEK_RESET}})
    stored = quota.merge(stored, quota.readings_from_info(newer, NOW + 120), NOW + 120)
    assert stored["windows"]["seven_day"]["utilization"] == pytest.approx(0.2)
    # Past the session's reset it is gone; the week stays.
    later = SESSION_RESET + 1
    assert set(quota.live(stored, later)) == {"seven_day"}
    assert set(quota.merge(stored, {}, later)["windows"]) == {"seven_day"}


def test_a_rejected_window_reads_full_until_it_resets():
    info = {"status": "rejected", "resetsAt": SESSION_RESET, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.93, "resetsAt": SESSION_RESET}}}
    stored = quota.merge({}, quota.readings_from_info(info, NOW), NOW, info)
    assert stored["windows"]["five_hour"]["status"] == "rejected"
    assert quota.limited(stored, NOW)
    g = quota.gauge(stored, NOW + 30)
    assert g["five_hour"]["utilization"] == 100.0
    assert g["five_hour"]["status"] == "rejected" and g["five_hour"]["age_seconds"] == 30
    assert not quota.limited(stored, SESSION_RESET + 1)
    assert quota.gauge(stored, SESSION_RESET + 1) is None
    # Rejected with no utilization at all: full all the same.
    bare = {"status": "rejected", "resetsAt": SESSION_RESET, "rateLimitType": "five_hour"}
    assert quota.readings_from_info(bare, NOW)["five_hour"]["utilization"] == 1.0


def test_the_gauge_is_in_percent_with_when_and_where_seen():
    stored = quota.merge({}, quota.readings_from_info(WEEK_ONLY, NOW), NOW)
    g = quota.gauge(stored, NOW + 90)
    assert g == {"seven_day": {
        "utilization": 17.0, "status": "allowed",
        "resets_at": datetime.fromtimestamp(WEEK_RESET, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seen_at": datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_seconds": 90, "source": "answer"}}


@pytest.mark.asyncio
async def test_noted_events_are_kept_in_the_settings_table(state):
    assert await quota.note(state, _event(SESSION_ONLY))
    assert not await quota.note(state, {"type": "result", "result": "ok"})
    assert await quota.note(state, _event(WEEK_ONLY))
    stored = json.loads(await state.get_setting(quota.SETTING_KEY))
    assert set(stored["windows"]) == {"five_hour", "seven_day"}
    # A broken row is no reading, never an error.
    await state.set_setting(quota.SETTING_KEY, "not json")
    assert await quota.QuotaClient(state).get_utilization() is None
    assert await quota.note(state, _event(WEEK_ONLY))


# ── The brain's stream ──


def _stream(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "Hello",
          "session_id": "s-1", "num_turns": 1, "usage": {"input_tokens": 3,
                                                         "output_tokens": 4}}


def test_the_brain_reads_the_result_line_and_the_rate_limit_events():
    raw = _stream({"type": "system", "subtype": "init", "session_id": "s-1"},
                  {"type": "assistant", "message": {"content": [{"type": "text",
                                                                 "text": "Hello"}]}},
                  _event(SESSION_ONLY), RESULT)
    text, payload, infos = Brain._parse_stream(raw)
    assert text == "Hello" and payload == RESULT and infos == [SESSION_ONLY]
    # The old one-object output reads the same.
    assert Brain._parse_result_payload(json.dumps(RESULT)) == ("Hello", RESULT)


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode

    async def communicate(self, data=None):
        return self.stdout, b""


def _fake_cli(monkeypatch, outputs, calls):
    import harvey.brain as brain_mod

    async def fake_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc(outputs[min(len(calls), len(outputs)) - 1])

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
async def test_a_brain_call_records_the_plans_usage(state, monkeypatch):
    calls = []
    _fake_cli(monkeypatch, [_stream(_event(WEEK_ONLY), RESULT).encode()], calls)
    assert await Brain(state).think("hi") == "Hello"
    cmd = calls[0]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    g = await quota.QuotaClient(state).get_utilization(probe_if_stale=False)
    assert g["seven_day"]["utilization"] == 17.0 and g["seven_day"]["source"] == "answer"


@pytest.mark.asyncio
async def test_a_rejected_call_isnt_retried_and_holds_the_budget(state, monkeypatch):
    rejected = {"status": "rejected", "resetsAt": NOW + 10**8, "rateLimitType": "five_hour",
                "unifiedWindows": {"five_hour": {"utilization": 1.0,
                                                 "resetsAt": NOW + 10**8}}}
    error = dict(RESULT, is_error=True, result="Claude usage limit reached")
    calls = []
    _fake_cli(monkeypatch, [_stream(_event(rejected), error).encode()], calls)
    brain = Brain(state)
    assert await brain.think("hi") == ""
    assert len(calls) == 1
    assert await brain.is_within_budget(max_percent=80) is False


# ── The budget check, unchanged for the same readings ──


@pytest.mark.parametrize("utilization, within", [(0.79, True), (0.80, False), (0.95, False)])
@pytest.mark.asyncio
async def test_is_within_budget_compares_the_worst_window(state, utilization, within):
    reset = int(datetime.now(timezone.utc).timestamp()) + 3600
    info = {"status": "allowed", "rateLimitType": "seven_day",
            "unifiedWindows": {"seven_day": {"utilization": utilization, "resetsAt": reset},
                               "five_hour": {"utilization": 0.1, "resetsAt": reset}}}
    await quota.note_infos(state, [info])
    assert await Brain(state).is_within_budget(max_percent=80) is within


@pytest.mark.asyncio
async def test_no_reading_falls_back_to_the_call_counter(state):
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is True
    await state.increment_usage()
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is False



@pytest.mark.asyncio
async def test_a_window_the_gauge_doesnt_know_isnt_room_to_spend(state):
    # (Only the week reported, 17% of it: the session isn't known, so the
    # call counter holds too, as with no gauge.)
    await quota.note_infos(state, [WEEK_ONLY])
    await state.increment_usage()
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is False
    assert await Brain(state).is_within_budget(max_daily_calls=5, max_percent=80) is True
    # Both known and fresh: the gauge alone decides, as before.
    await quota.note_infos(state, [SESSION_ONLY])
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is True


@pytest.mark.asyncio
async def test_a_window_a_recent_usage_check_didnt_list_is_known(state, monkeypatch):
    # (The verified text has no session line: no session running.)
    monkeypatch.setattr(quota, "_run_probe", lambda exe: _stream(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": VERIFIED_USAGE_TEXT}]}}))
    monkeypatch.setattr(quota, "probe", _REAL_PROBE)
    await state.increment_usage()
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is True
    stored = json.loads(await state.get_setting(quota.SETTING_KEY))
    assert stored["listed"]["windows"] == ["seven_day", "seven_day_fable"]
    # That listing grown old (and no new check yet): unknown again.
    stored["listed"]["at"] -= quota.STALE_SECONDS + 60
    stored["probed_at"] = time.time()
    await state.set_setting(quota.SETTING_KEY, json.dumps(stored))
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is False


@pytest.mark.asyncio
async def test_a_required_window_missing_asks_usage_even_with_fresh_answers(state,
                                                                           monkeypatch):
    runs = []
    monkeypatch.setattr(quota, "_run_probe", lambda exe: runs.append(exe) or "")
    monkeypatch.setattr(quota, "probe", _REAL_PROBE)
    await quota.note_infos(state, [WEEK_ONLY])
    await quota.QuotaClient(state).get_utilization()
    assert runs == ["claude"]
    # Both windows fresh: no check.
    await state.set_setting(quota.SETTING_KEY, "{}")
    await quota.note_infos(state, [WEEK_ONLY, SESSION_ONLY])
    await quota.QuotaClient(state).get_utilization()
    assert runs == ["claude"]


@pytest.mark.asyncio
async def test_a_reading_from_long_ago_isnt_room_to_spend(state):
    await quota.note_infos(state, [WEEK_ONLY, SESSION_ONLY])
    await state.increment_usage()
    stored = json.loads(await state.get_setting(quota.SETTING_KEY))
    for w in stored["windows"].values():
        w["seen_at"] -= quota.STALE_SECONDS + 60
    await state.set_setting(quota.SETTING_KEY, json.dumps(stored))
    assert await Brain(state).is_within_budget(max_daily_calls=1, max_percent=80) is False
    # An old reading over the limit still holds Harvey back.
    for w in stored["windows"].values():
        w["utilization"] = 0.9
    await state.set_setting(quota.SETTING_KEY, json.dumps(stored))
    assert await Brain(state).is_within_budget(max_daily_calls=100, max_percent=80) is False


def test_a_rejected_event_naming_no_window_still_holds():
    bare = {"status": "rejected", "resetsAt": SESSION_RESET}
    readings = quota.readings_from_info(bare, NOW)
    assert readings[quota.LIMIT_WINDOW]["utilization"] == 1.0
    stored = quota.merge({}, readings, NOW, bare)
    assert quota.limited(stored, NOW)
    assert quota.gauge(stored, NOW)[quota.LIMIT_WINDOW]["utilization"] == 100.0
    assert not quota.limited(stored, SESSION_RESET + 1)


@pytest.mark.asyncio
async def test_a_stream_cut_short_is_no_answer(state, monkeypatch):
    calls = []
    cut = _stream({"type": "system", "subtype": "init", "session_id": "s-1"},
                  {"type": "assistant", "message": {"content": [{"type": "text",
                                                                 "text": "Hel"}]}})
    _fake_cli(monkeypatch, [cut.encode()], calls)
    import harvey.brain as brain_mod

    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    assert await Brain(state).think("hi", max_retries=1) == ""
    assert len(calls) == 2
    # Output that isn't a stream at all is still read as plain text.
    assert Brain._parse_stream("plain words") == ("plain words", None, [])
    assert Brain._parse_stream('{"type": "interested"}')[0] == '{"type": "interested"}'

# ── The /usage command's text ──


def test_the_verified_usage_text():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    r = quota.parse_usage_text(VERIFIED_USAGE_TEXT, now)
    assert set(r) == {"seven_day", "seven_day_fable"}
    assert r["seven_day"]["utilization"] == pytest.approx(0.17)
    assert r["seven_day"]["resets_at"] == int(datetime(2026, 11, 12, 2, 59,
                                                       tzinfo=timezone.utc).timestamp())
    assert r["seven_day_fable"]["utilization"] == 0.0
    assert r["seven_day_fable"]["resets_at"] == int(datetime(2026, 11, 12, 3, 0,
                                                             tzinfo=timezone.utc).timestamp())
    assert r["seven_day"]["source"] == quota.SOURCE_USAGE


def test_a_session_line_a_zone_and_a_date_in_the_new_year():
    now = datetime(2026, 12, 30, 22, 0, tzinfo=timezone.utc).timestamp()
    text = ("Current session: 42% used · resets 1:30am (Europe/Bucharest)\n"
            "Current week (all models): 61% used · resets Jan 3, 9pm (UTC)\n"
            "Current week (Opus): 5.5% used · resets Jan 3, 9pm (Mars/Olympus)")
    r = quota.parse_usage_text(text, now)
    assert r["five_hour"]["utilization"] == pytest.approx(0.42)
    # 1:30am in Bucharest (UTC+2) after 22:00 UTC: the next one, 23:30 UTC.
    assert r["five_hour"]["resets_at"] == int(datetime(2026, 12, 30, 23, 30,
                                                       tzinfo=timezone.utc).timestamp())
    assert r["seven_day"]["resets_at"] == int(datetime(2027, 1, 3, 21, 0,
                                                       tzinfo=timezone.utc).timestamp())
    # A zone that can't be read: the reset kept as the CLI's own words.
    assert r["seven_day_opus"]["resets_at"] is None
    assert r["seven_day_opus"]["resets_text"] == "Jan 3, 9pm (Mars/Olympus)"
    g = quota.gauge(quota.merge({}, r, now), now)
    assert g["seven_day_opus"]["resets_at"] == "Jan 3, 9pm (Mars/Olympus)"
    # (Kept an hour, having no reset to drop it at.)
    assert "seven_day_opus" not in quota.live({"windows": r}, now + 2 * 3600)


@pytest.mark.parametrize("text", [
    "",
    "Unknown command: /usage",
    "You are currently using API usage billing. /usage is only available for "
    "subscription plans.",
    "Total cost: $0.12\nTotal duration (API): 3s",
    "Current week (all models): lots used",
])
def test_text_it_doesnt_know_is_no_reading(text):
    assert quota.parse_usage_text(text, NOW) == {}


def test_the_probes_text_comes_from_the_stream():
    out = _stream({"type": "system", "subtype": "init"},
                  {"type": "assistant", "message": {"content": [
                      {"type": "text", "text": VERIFIED_USAGE_TEXT}]}},
                  {"type": "result", "subtype": "success", "result": "", "num_turns": 0,
                   "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}})
    text, infos = quota.output_text(out + "\nnot json")
    assert "Current week (all models): 17% used" in text and infos == []


# ── The /usage check ──


def test_the_probes_command_keeps_the_isolation_but_slash_commands():
    cmd = claude_runtime.build_usage_probe_command("claude")
    assert "--disable-slash-commands" not in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    for flag in ("--strict-mcp-config", "--safe-mode", "--no-session-persistence",
                 "--verbose", "-p"):
        assert flag in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--mcp-config" not in cmd and "--model" not in cmd
    # Its help check asks for every flag it uses, and the brain's still asks
    # for --disable-slash-commands.
    assert "--disable-slash-commands" not in claude_runtime.PROBE_FLAGS
    assert set(claude_runtime.PROBE_FLAGS) == set(claude_runtime.REQUIRED_FLAGS) - {
        "--disable-slash-commands"}
    assert "--disable-slash-commands" in claude_runtime.REQUIRED_FLAGS
    assert "--verbose" in claude_runtime.REQUIRED_FLAGS


@pytest.mark.asyncio
async def test_the_probe_records_its_reading_at_most_every_interval(state, monkeypatch):
    runs = []

    def fake_run(exe):
        runs.append(exe)
        return _stream({"type": "assistant", "message": {"content": [
            {"type": "text", "text": VERIFIED_USAGE_TEXT}]}})

    monkeypatch.setattr(quota, "_run_probe", fake_run)
    monkeypatch.setattr(quota, "probe", _REAL_PROBE)
    g = await quota.QuotaClient(state).get_utilization()
    assert runs == ["claude"]
    assert g["seven_day"]["utilization"] == 17.0
    assert g["seven_day"]["source"] == "usage_check"
    # A fresh reading: no second check.
    await quota.QuotaClient(state).get_utilization()
    assert len(runs) == 1
    # Stale, but checked within the interval: still none.
    stored = json.loads(await state.get_setting(quota.SETTING_KEY))
    for w in stored["windows"].values():
        w["seen_at"] -= 2 * quota.PROBE_EVERY_SECONDS
    await state.set_setting(quota.SETTING_KEY, json.dumps(stored))
    await quota.QuotaClient(state).get_utilization()
    assert len(runs) == 1
    assert await _REAL_PROBE(state, force=True)
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_a_probe_that_fails_or_says_nothing_known_is_no_reading(state, monkeypatch):
    monkeypatch.setattr(quota, "probe", _REAL_PROBE)
    monkeypatch.setattr(quota, "_run_probe", lambda exe: None)
    assert await quota.QuotaClient(state).get_utilization() is None
    monkeypatch.setattr(quota, "_run_probe", lambda exe: _stream(
        {"type": "result", "result": "You are using an API key."}))
    assert not await _REAL_PROBE(state, force=True)
    assert await quota.QuotaClient(state).get_utilization(probe_if_stale=False) is None


@pytest.mark.asyncio
async def test_the_probe_refuses_a_cli_without_its_flags(state, monkeypatch):
    runs = []
    monkeypatch.setattr(quota, "_run_probe", lambda exe: runs.append(exe))
    monkeypatch.setattr(claude_runtime, "_probe_help", lambda exe: "  --tools  --verbose")
    assert not await _REAL_PROBE(state, force=True)
    assert runs == []



@pytest.mark.asyncio
async def test_a_probe_the_model_answered_gives_no_reading_and_waits_a_day(state, monkeypatch):
    runs = []

    def answered(exe):
        runs.append(exe)
        # (A CLI that sent /usage to the model: its text could say anything.)
        return _stream({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Current week (all models): 1% used"}]}},
            {"type": "result", "result": "Current week (all models): 1% used",
             "num_turns": 1, "total_cost_usd": 0.002,
             "usage": {"input_tokens": 40, "output_tokens": 9}})

    monkeypatch.setattr(quota, "_run_probe", answered)
    assert not await _REAL_PROBE(state, force=True)
    assert await quota.QuotaClient(state).get_utilization(probe_if_stale=False) is None
    stored = json.loads(await state.get_setting(quota.SETTING_KEY))
    stored["probed_at"] = 0
    await state.set_setting(quota.SETTING_KEY, json.dumps(stored))
    assert not await _REAL_PROBE(state)
    assert len(runs) == 1

# ── Never the login ──


_LOGIN_READS = re.compile(r"credentials\.json|find-generic-password|dump-keychain|"
                          r"oauth/usage|claudeAiOauth|accessToken|CLAUDE_CODE_OAUTH_TOKEN")


def test_no_code_reads_the_claude_login():
    root = Path(__file__).resolve().parents[1] / "harvey"
    found = []
    for path in root.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _LOGIN_READS.search(code):
                found.append(f"{path.relative_to(root.parent)}:{n}")
    assert found == []


def test_harvey_usage_says_when_and_where_seen():
    from harvey.cli import _quota_seen

    line = _quota_seen({"seven_day": {"utilization": 17.0, "seen_at": "2026-11-05T10:00:00Z",
                                      "age_seconds": 125, "source": "usage_check"},
                        "five_hour": {"utilization": 5.0, "seen_at": "2026-11-05T09:00:00Z",
                                      "age_seconds": 3725, "source": "answer"}})
    assert line == ("Last seen 2 min ago (2026-11-05T10:00:00Z), from the CLI's /usage "
                    "command.")
    # Codex's gauge doesn't say: nothing printed.
    assert _quota_seen({"five_hour": {"utilization": 21.0, "resets_at": ""}}) == ""


@pytest.mark.asyncio
async def test_a_rejected_reading_rests_the_agents_lane(state):
    from harvey.agent import wakes
    from harvey.modules import jobs

    rejected = {"status": "rejected", "resetsAt": NOW + 10**6, "rateLimitType": "five_hour"}
    payload = {"type": "result", "is_error": True, "result": "Something went wrong"}
    # (Without the reading this is a failed run.)
    with pytest.raises(jobs.JobError):
        await wakes.no_answer(state, payload, "Something went wrong", "", "", 0)
    await quota.note_infos(state, [rejected])
    with pytest.raises(jobs.LaneExhausted) as e:
        await wakes.no_answer(state, payload, "Something went wrong", "", "", 0)
    assert e.value.retry_after == wakes.QUOTA_RETRY_SECONDS
