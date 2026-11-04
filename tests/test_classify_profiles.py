"""Classifier profiles (harvey/classify_profiles.py): the built-ins and
what they measured, your own profiles, which task uses which, the older
settings taken as a profile, extra request fields, test runs, a run's
classifier budget (classify.Meter, jobs.classify_room) and its report,
and verifying a contact (harvey/contact_check.py). Every endpoint is a
fake (no network, no model). All data is fictional."""

import asyncio
import json
import sqlite3

import pytest

from harvey import classify, classify_decision, classify_profiles as profiles, contacts
from harvey import contact_check
from harvey.models import Company
from harvey.modules import jobs, lanes
from harvey.state import StateManager
from tests import slim
from tests.test_classify import (GOOD, FakeHTTP, FakeOpenAI, _card, _site_payload, said)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    from harvey import classify_prices

    monkeypatch.setattr(classify_prices, "_kept", (0.0, []))
    monkeypatch.setattr(classify_prices, "_failed_at", 0.0)
    monkeypatch.setattr(classify_prices, "_lock", None)
    classify._NO_JSON_MODE.clear()

    async def wait_gap(lane, seconds, key=""):
        lanes.interval(lane)

    monkeypatch.setattr(lanes, "wait_gap", wait_gap)


async def _state(path, **legacy) -> StateManager:
    # (The install's caps as settings, the rest as an earlier Harvey kept
    # its one classifier: tests.test_classify._state.)
    from tests.test_classify import _state as legacy_state

    return await legacy_state(path, **legacy)


LOCAL = {"name": "Local test model", "kind": "openai_compatible",
         "base_url": "http://127.0.0.1:11434/v1", "model": "small-3b", "key_name": "",
         "prices": {"in_per_m": 0.1, "out_per_m": 0.2}}


# ── The built-ins ──


def test_the_built_ins_are_the_measured_setups_with_counts_and_none_is_put_first():
    b = profiles.builtins()
    assert set(b) == {"deepinfra-gemma-3-27b", "deepinfra-mistral-small-3.2",
                      "deepinfra-mistral-small-24b-2501", "deepinfra-qwen3.8-27b-no-thinking",
                      "typesafe-jev", "ollama-qwen2.5-3b"}
    text = json.dumps(profiles.catalog()).lower()
    assert "recommend" not in text and "the cheapest" not in text and "suggest" not in text
    for p in b.values():
        m = p["measured"]
        assert p["builtin"] and m["test_set"].startswith("200 hand-labelled firms")
        assert isinstance(m["site_right"], int) and m["false_links"] == 0 and m["scored"] == 32
        assert profiles.tested(p)
    # Thinking off for the reasoning model, as it was measured.
    assert b["deepinfra-qwen3.8-27b-no-thinking"]["request_extra"] == {"reasoning_effort": "none"}
    # A decision model answers what it has questions for: no search results.
    assert b["typesafe-jev"]["tasks"] == ["site", "contacts"]
    assert b["typesafe-jev"]["prices"] == {"in_per_m": 0.042, "out_per_m": 0.0,
                                           "from_list": False}
    # The DeepInfra key's scopes are documented, the two added by hand named.
    scopes = profiles.key_notes("DEEPINFRA_API_KEY")["scopes"]
    assert "off by default" in scopes["billing:read"] and "off by default" in scopes["metrics"]


def test_the_built_in_questions_are_the_ones_code_measured():
    b = profiles.builtins()
    lm = b["deepinfra-gemma-3-27b"]["questions"]
    for name, t in classify.TASKS.items():
        assert lm[name] == {"prompt": t.prompt, "yes": t.yes, "no": t.no}
    assert b["typesafe-jev"]["questions"] == classify_decision.default_questions()


# ── Your own, the built-ins' copies, hiding ──


def test_a_built_ins_edit_is_a_copy_of_your_own_and_not_tested(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        copy = await profiles.save(s, {"per_minute": 10}, profile_id="deepinfra-gemma-3-27b")
        return copy, await profiles.every(s)

    copy, every = run(go())
    assert copy["id"] != "deepinfra-gemma-3-27b" and copy["based_on"] == "deepinfra-gemma-3-27b"
    assert copy["per_minute"] == 10 and not copy["builtin"] and not profiles.tested(copy)
    assert copy["measured"] is None
    assert every["deepinfra-gemma-3-27b"]["per_minute"] == 20      # the built-in as shipped


def test_a_test_run_makes_a_profile_tested_until_what_it_asks_changes(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, LOCAL)
        await profiles.record_test(s, p["id"], {"task": "site", "asked": 3, "correct": 3,
                                                "scored": 3, "latency_ms": {"median": 9}},
                                   test_set="built-in")
        tested = profiles.tested((await profiles.own(s))[p["id"]])
        await profiles.save(s, {"per_minute": 30}, profile_id=p["id"])     # not what it asks
        still = profiles.tested((await profiles.own(s))[p["id"]])
        await profiles.save(s, {"questions": {"site": {"yes": "the same firm, plainly."}}},
                            profile_id=p["id"])
        after = (await profiles.own(s))[p["id"]]
        return tested, still, after, await profiles.tests(s, p["id"])

    tested, still, after, runs = run(go())
    assert tested and still and not profiles.tested(after)
    assert after["questions"]["site"]["yes"] == "the same firm, plainly."
    assert after["questions"]["site"]["prompt"] == classify.SITE_PROMPT
    assert runs[0]["correct"] == 3 and runs[0]["median_ms"] == 9 and runs[0]["test_set"] == "built-in"


def test_a_profile_changed_while_its_test_ran_stays_not_tested(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, LOCAL)
        asks = profiles.asks_hash(p)                      # as the test starts
        await profiles.save(s, {"model": "small-7b"}, profile_id=p["id"])
        await profiles.record_test(s, p["id"], {"task": "site", "asked": 3, "correct": 3},
                                   test_set="built-in", asks=asks)
        return (await profiles.own(s))[p["id"]], await profiles.tests(s, p["id"])

    after, runs = run(go())
    assert not profiles.tested(after) and runs[0]["correct"] == 3


def test_a_task_set_to_a_profile_is_asked_though_the_older_settings_had_it_off(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db", provider="openai_compatible",
                         base_url="http://127.0.0.1:11434/v1", model="small-3b",
                         tasks=json.dumps({"results": {"on": False, "per_day": 40}}))
        before = (await classify.settings(s, "results")).task_on("results")
        mine = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"results": mine["id"]})
        after = await classify.settings(s, "results")
        return before, after

    before, after = run(go())
    assert not before
    assert after.task_on("results") and after.task_cap("results") == 40


def test_hiding_a_built_in_and_removing_your_own_turn_their_tasks_off(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        mine = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": "typesafe-jev", "contacts": mine["id"]})
        await profiles.hide(s, "typesafe-jev")
        hidden = await profiles.uses(s), "typesafe-jev" in await profiles.every(s)
        await profiles.hide(s, "typesafe-jev", shown=True)
        await profiles.remove(s, mine["id"])
        return hidden, await profiles.uses(s), "typesafe-jev" in await profiles.every(s)

    (use, listed), after, shown = run(go())
    assert use["site"] == "off" and use["contacts"] != "off" and not listed
    assert after == {"results": "off", "site": "off", "contacts": "off"} and shown


def test_a_profile_can_be_set_only_for_a_task_it_answers(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        with pytest.raises(ValueError, match="doesn't answer"):
            await profiles.set_use(s, {"results": "typesafe-jev"})
        with pytest.raises(ValueError, match="no profile"):
            await profiles.set_use(s, {"site": "nobody"})
        return await profiles.set_use(s, {"site": "typesafe-jev"})

    assert run(go())["site"] == "typesafe-jev"


@pytest.mark.parametrize("extra, ok", [
    ({"reasoning_effort": "none"}, True), ({"top_k": 5, "min_p": 0.1}, True),
    ({"model": "other"}, False), ({"messages": []}, False), ({"max_tokens": 9}, False),
    ({"response_format": {}}, False), ({"nested": {"a": 1}}, False), ({"Bad-Key": 1}, False),
    ("reasoning_effort=none", False)])
def test_extra_request_fields_are_plain_and_never_the_requests_own(extra, ok):
    assert (profiles.extra_problem(extra) == "") is ok


def test_the_questions_keep_codes_structure_and_bars_their_range():
    with pytest.raises(ValueError):
        profiles.check_questions("decision", ["site"], {"site": {"questions": {"other": {}}}})
    with pytest.raises(ValueError):
        profiles.check_questions("decision", ["contacts"],
                                 {"contacts": {"options": {"someone_else": "x"}}})
    with pytest.raises(ValueError):
        profiles.check_questions("decision", ["contacts"],
                                 {"contacts": {"instructions": "Whose is it?"}})
    q = profiles.check_questions("decision", ["site", "contacts"], {
        "site": {"needs": {"names": 2.0}, "vetoes": {"directory": 0.0}},
        "contacts": {"bar": 0.1}})
    assert q["site"]["needs"]["names"] == 0.99 and q["site"]["vetoes"]["directory"] == 0.05
    assert q["contacts"]["bar"] == classify.YES_RANGE[0]


# ── The older settings, taken as a profile ──


def test_an_install_with_a_provider_set_gets_it_as_a_profile_with_its_caps(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db", provider="openai_compatible",
                         base_url="http://127.0.0.1:11434/v1", model="small-3b",
                         per_day=300, tasks=json.dumps({"results": {"on": False,
                                                                    "per_day": 40}}))
        site = await classify.settings(s, "site")
        first = await profiles.uses(s)
        # (Taken once: changed afterwards, the older setting is not read.)
        await s.set_setting(f"{profiles.LEGACY_PREFIX}model", "small-7b")
        await profiles.set_use(s, {"contacts": "off"})
        again = await classify.settings(s, "site")
        return site, first, again, await profiles.own(s), await profiles.uses(s)

    site, use, again, own, later = run(go())
    assert use == {"results": "off", "site": "settings", "contacts": "settings"}
    assert site.profile == "settings" and site.model == "small-3b" and site.per_day == 300
    assert site.task_cap("results") == 40
    assert again.model == "small-3b" and own["settings"]["model"] == "small-3b"
    assert later["contacts"] == "off"


def test_an_install_with_no_provider_set_writes_nothing(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        await classify.settings(s)
        return await s.settings_with_prefix("classify:")

    assert run(go()) == {}


# ── Each task asks its own profile ──


def test_each_task_asks_the_profile_it_is_set_to_with_its_texts_and_extra_fields(
        tmp_path, monkeypatch):
    fake = FakeOpenAI(GOOD, json.dumps({"c1": "yes"})).install(monkeypatch)
    card = _card(contacts=[("phone", "+40268000111", "Tel +40268000111")])

    async def go():
        s = await _state(tmp_path / "harvey.db")
        a = await profiles.save(s, {**LOCAL, "name": "Site model", "request_extra":
                                    {"reasoning_effort": "none"},
                                    "questions": {"site": {"prompt": "THE SITE PROMPT, edited "
                                                                     "for this test."}}})
        b = await profiles.save(s, {**LOCAL, "name": "Contacts model", "model": "small-7b"})
        await profiles.set_use(s, {"site": a["id"], "contacts": b["id"]})
        one = await classify.ask(s, "site", _site_payload())
        two = await classify.ask(s, "contacts", _site_payload(card))
        return one, two

    one, two = run(go())
    assert one.value == {"yes": True} and two.value["contacts"] == [0]
    assert [r["model"] for r in fake.requests] == ["small-3b", "small-7b"]
    assert fake.requests[0]["extra_body"] == {"reasoning_effort": "none"}
    assert "extra_body" not in fake.requests[1]
    assert fake.requests[0]["messages"][0]["content"].startswith("THE SITE PROMPT, edited")


def test_a_decision_profile_sends_its_texts_bars_and_extra_fields(tmp_path, monkeypatch):
    fake = FakeHTTP(said({"names": 0.55, "own": 0.81, "directory": 0.04,
                          "institution": 0.02})).install(monkeypatch)
    monkeypatch.setenv("CLASSIFY_DECISION_KEY", "test-decision-value")

    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, {
            "name": "Decision test", "kind": "decision",
            "base_url": "https://decisions.exemplu.test/v1/decide", "model": "decision-test-1",
            "key_name": "CLASSIFY_DECISION_KEY", "request_extra": {"temperature_hint": "low"},
            "questions": {"site": {"needs": {"names": 0.6},
                                   "questions": {"own": {"instructions": "Edited own."}}}}})
        await profiles.set_use(s, {"site": p["id"]})
        return await classify.ask(s, "site", _site_payload())

    ans = run(go())
    body = json.loads(fake.requests[0].content)
    assert body["temperature_hint"] == "low" and body["model"] == "decision-test-1"
    assert body["questions"]["own"]["instructions"] == "Edited own."
    # names 0.55 is under the edited bar (0.6): not the firm's.
    assert ans.value["yes"] is False


# ── A run's classifier money ──


def test_a_meter_that_refuses_stops_the_call_before_anything_is_sent(tmp_path, monkeypatch):
    fake = FakeOpenAI(GOOD).install(monkeypatch)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": p["id"]})
        asked = []

        async def room(meter, estimate):
            asked.append(estimate)
            return "the run's classifier limit ($0.00) is reached"

        token = classify.METER.set(classify.Meter(room))
        try:
            return await classify.ask(s, "site", _site_payload()), asked
        finally:
            classify.METER.reset(token)

    ans, asked = run(go())
    assert ans.why == classify.RUN_BUDGET and fake.requests == []
    # (The estimate: the request's tokens and the most it may answer, at the price.)
    assert asked and asked[0] > 0


def test_a_meter_counts_each_call_and_charges_its_cost(tmp_path, monkeypatch):
    FakeOpenAI(GOOD).install(monkeypatch)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": p["id"]})
        charged = []

        async def charge(meter, cost):
            charged.append(cost)

        meter = classify.Meter(None, charge)
        token = classify.METER.set(meter)
        try:
            await classify.ask(s, "site", _site_payload())
        finally:
            classify.METER.reset(token)
        return meter.as_dict(), charged

    m, charged = run(go())
    # 420 tokens in at $0.1/M, 17 out at $0.2/M (the fake's usage).
    assert m["calls"] == 1 and m["tokens_in"] == 420 and m["tokens_out"] == 17
    assert charged == [pytest.approx(420 * 0.1 / 1e6 + 17 * 0.2 / 1e6)]
    assert m["usd"] == pytest.approx(charged[0], abs=1e-6)


def _job(state, batch, **progress):
    async def go():
        job_id = await jobs.enqueue(state, "find_contacts", "contacts", {"company_id": "1"},
                                    batch_id=batch)
        await jobs.save_progress(state, job_id, progress, actual_cost=progress.get(
            "spent", 0.0))
        return await jobs.get(state, job_id)
    return go()


def test_the_runs_classifier_cap_counts_its_jobs_and_the_search_cap_leaves_them_out(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        batch = await jobs.new_batch(s, "find_contacts", "t", params={"classify_max_spend": 0.05},
                                     max_spend=1.0)
        await _job(s, batch, classify={"usd": 0.04, "calls": 4}, spent=0.04)
        me = await _job(s, batch)
        searches = await jobs.batch_spent(s, batch, exclude=me.id)
        fits = await jobs.classify_room(s, me, 0.005, own_classify=0.0, own=0.0)
        over = await jobs.classify_room(s, me, 0.02, own_classify=0.0, own=0.0)
        none = await jobs.new_batch(s, "find_contacts", "t", params={"classify_max_spend": 0})
        zero = await jobs.classify_room(s, await _job(s, none), 0.0, own_classify=0, own=0)
        return searches, fits, over, zero, await jobs.batch_report(s, batch)

    searches, fits, over, zero, report = run(go())
    assert searches == 0.0 and fits == "" and "classifier limit" in over and zero
    assert report["classify"]["calls"] == 4 and report["classify"]["usd"] == 0.04
    assert report["total_usd"] == 0.04 and report["searches_usd"] == 0.0
    assert "classifier 4 calls" in jobs.report_line(report)


def test_the_days_spend_limit_counts_the_classifier_too(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        batch = await jobs.new_batch(s, "find_contacts", "t")
        other = await _job(s, batch, classify={"usd": 0.9}, spent=0.9)
        async with s._connect() as db:
            await db.execute("UPDATE jobs SET started_at = datetime('now') WHERE id = ?",
                             (other.id,))
            await db.commit()
        me = await _job(s, batch)
        return await jobs.classify_room(s, me, 0.2, own_classify=0, own=0, per_day=1.0)

    assert "for the day" in run(go())


def test_a_running_jobs_classifier_calls_dont_use_up_what_it_holds_for_searches(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        batch = await jobs.new_batch(s, "find_contacts", "t", max_spend=1.0)
        other = await _job(s, batch, classify={"usd": 0.1}, spent=0.1)
        async with s._connect() as db:
            # (It reserved 0.5 for its searches, then the classifier cost 0.1.)
            await db.execute("UPDATE jobs SET status = 'running', reserved = 0.5, "
                             "started_at = datetime('now') WHERE id = ?", (other.id,))
            await db.commit()
        me = await _job(s, batch)
        return (await jobs.batch_spent(s, batch, exclude=me.id),
                await jobs.spent_since(s, "start of day", exclude=me.id))

    searches, day = run(go())
    assert searches == pytest.approx(0.5) and day == pytest.approx(0.6)


def test_a_finished_runs_report_is_kept_until_one_of_its_jobs_changes(tmp_path, monkeypatch):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        batch = await jobs.new_batch(s, "find_contacts", "t", max_spend=1.0)
        job = await _job(s, batch, classify={"usd": 0.01, "calls": 1}, spent=0.01)
        await jobs.finish(s, job.id, jobs.DONE, result={"spend": {"checks_made": 1}})
        first = await jobs.batch_report(s, batch)
        made = []
        real = jobs._batch_report

        async def counted(*a):
            made.append(1)
            return await real(*a)

        monkeypatch.setattr(jobs, "_batch_report", counted)
        kept = await jobs.batch_report(s, batch)
        # (Retried: a finished run's report is read again once it changed.)
        await jobs.finish(s, job.id, jobs.QUEUED)
        running = await jobs.batch_report(s, batch)
        return first, kept, running, made

    first, kept, running, made = run(go())
    assert first == kept and first["checks_made"] == 1 and first["statuses"] == {"done": 1}
    assert running["statuses"] == {"queued": 1} and made == [1]


# ── Each profile's pace and hold ──

HOSTED = {"name": "Hosted", "kind": "openai_compatible", "base_url": "https://llm.exemplu.test/v1",
          "model": "hosted-1", "key_name": "HOSTED_API_KEY"}


def test_a_429_holds_only_the_profile_it_came_from(tmp_path, monkeypatch):
    from tests.test_classify import RateLimitError

    fake = FakeOpenAI(RateLimitError("rate limited", 429), '{"c1": "yes"}').install(monkeypatch)
    paced = []

    async def wait_gap(lane, seconds, key=""):
        paced.append((lane, key))

    monkeypatch.setattr(lanes, "wait_gap", wait_gap)
    card = _card(contacts=[("phone", "+40268000111", "Tel. +40 268 000 111")])

    async def go():
        s = await _state(tmp_path / "harvey.db")
        hosted = await profiles.save(s, HOSTED)
        local = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": hosted["id"], "contacts": local["id"]})
        site = await classify.ask(s, "site", _site_payload())
        contacts_ = await classify.ask(s, "contacts", _site_payload(card))
        by_task = (await classify.settings(s)).by_task
        return site, contacts_, by_task, hosted, local

    site, contacts_, by_task, hosted, local = run(go())
    assert site.why == "held" and contacts_.value == {"contacts": [0], "no": []}
    held = classify.held(by_task["site"])
    assert held["seconds"] > 0 and held["scope"] == "profile" and "429" in held["why"]
    assert classify.held(by_task["contacts"])["seconds"] == 0
    # Each profile's pace is its own, in the classify lane.
    assert paced == [("classify", f"classify.{hosted['id']}"),
                     ("classify", f"classify.{local['id']}")]
    assert len(fake.requests) == 2


def test_a_refused_key_holds_every_profile_on_that_account_and_no_other(tmp_path, monkeypatch):
    from tests.test_classify import _Err

    class AuthenticationError(_Err):
        pass

    FakeOpenAI(AuthenticationError("bad key", 401)).install(monkeypatch)

    async def go():
        s = await _state(tmp_path / "harvey.db")
        one = await profiles.save(s, HOSTED)
        two = await profiles.save(s, {**HOSTED, "name": "Hosted two", "model": "hosted-2"})
        local = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": one["id"]})
        await classify.ask(s, "site", _site_payload())
        caps = await classify.install_caps(s)
        return [classify.held(profiles.settings_of(p, caps)) for p in (one, two, local)]

    one, two, local = run(go())
    assert one["scope"] == two["scope"] == "account" and two["seconds"] > 0
    assert local["seconds"] == 0


def test_the_card_says_which_profile_is_held_and_until_when(client, tmp_path):
    pid = client.post("/api/classify/profiles", json=HOSTED).json()["profile"]["id"]
    client.post("/api/classify/use", json={"site": pid})

    async def hold():
        s = await _state(tmp_path / "harvey.db")
        caps = await classify.install_caps(s)
        p = (await profiles.own(s))[pid]
        classify.hold(profiles.settings_of(p, caps), 120, "RateLimitError 429")

    run(hold())
    status = client.get("/api/classify").json()
    assert status["held"][pid]["scope"] == "profile" and 0 < status["held"][pid]["seconds"] <= 120
    assert status["held"][pid]["why"] == "RateLimitError 429" and status["held_seconds"] > 0
    view = {p["id"]: p for p in client.get("/api/classify/profiles").json()["profiles"]}
    assert view[pid]["held"]["seconds"] > 0 and view["ollama-qwen2.5-3b"]["held"]["seconds"] == 0
    assert view["deepinfra-gemma-3-27b"]["is_deepinfra"] and not view[pid]["is_deepinfra"]


# ── Estimates: a range, never a limit ──


def test_the_find_estimate_counts_found_sites_read_again_as_a_range(tmp_path):
    from harvey import finder

    async def go():
        s = await _state(tmp_path / "harvey.db")
        mine = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {t: mine["id"] for t in classify.TASKS})
        found = await s.add_company(Company(name="ALFA EXEMPLU SRL", source="registry:onrc"))
        await s.set_found_website(found, "https://alfa-exemplu.test/", trust="likely",
                                  source="tinyfish", basis={"by": "judge", "model": "m"})
        waiting = await s.add_company(Company(name="BETA EXEMPLU SRL", source="registry:onrc"))
        await s.set_found_website(waiting, "https://beta-exemplu.test/", trust="unconfirmed",
                                  source="tinyfish")
        await s.add_company(Company(name="GAMA EXEMPLU SRL", source="registry:onrc"))
        selected = await finder.select(s, force=True)
        guess = await finder.classify_estimate(s, selected)
        for tokens in (600, 800, 1400):
            await s.record_usage_event(agent="classify", task="classify.site", model="small-3b",
                                       input_tokens=tokens, output_tokens=5, source="classify",
                                       provider="openai_compatible")
        seen = await finder.classify_estimate(s, selected)
        return {c["id"]: c for c in selected}, found, waiting, guess, seen

    rows, found, waiting, guess, seen = run(go())
    assert rows[found]["website_found"] and not rows[found]["website_waiting"]
    assert rows[waiting]["website_waiting"]
    # The firm with no site: a results question, three site ones, one contacts;
    # the found site read again: one site question and one contacts; the
    # waiting one: none.
    assert {t: p["calls"] for t, p in guess["per_task"].items()} == {
        "results": 1, "site": 4, "contacts": 2}
    assert guess["calls"] == 7 and guess["note"] == classify.ESTIMATE_NOTE
    assert 0 < guess["usd_typical"] < guess["usd_high"]
    site = seen["per_task"]["site"]
    assert site["basis"] == "this month" and site["tokens_typical"] == 933
    assert site["tokens_high"] == 1400
    assert guess["per_task"]["site"]["tokens_high"] == classify.worst_tokens(
        classify.TASKS["site"].max_input_tokens)


def test_the_find_estimate_counts_only_what_the_run_will_do(tmp_path):
    # A found site read since, and a company every search already looked up:
    # a run that isn't forced does neither again, and the estimate agrees.
    from harvey import finder
    from harvey.modules import runs, switches

    env = {"serper_api_key": "test-key"}

    async def go():
        s = await _state(tmp_path / "harvey.db")
        mine = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {t: mine["id"] for t in classify.TASKS})
        found = await s.add_company(Company(name="ALFA EXEMPLU SRL", source="registry:onrc"))
        await s.set_found_website(found, "https://alfa-exemplu.test/", trust="likely",
                                  source="tinyfish", basis={"by": "judge", "model": "m"})
        await s.record_search("company", found, *finder.SITE, looked_for=runs.CONTACT_READ)
        looked = await s.add_company(Company(name="GAMA EXEMPLU SRL", source="registry:onrc"))
        enabled = await switches.enabled_ids(s)
        ways = finder.searchers(enabled, env)
        for w in ways:
            await s.record_search("company", looked, w.module, w.capability,
                                  looked_for="website", outcome="nothing")
        selected = await finder.select(s, force=True)
        out = {}
        for force in (False, True):
            planned = await finder.plan(s, selected, enabled, env, force=force)
            out[force] = (finder.estimate(planned, enabled, env),
                          await finder.classify_estimate(s, planned))
        return ways, out

    ways, out = run(go())
    assert ways
    plain, forced = out[False], out[True]
    assert plain[0]["to_look_up"] == 0 and plain[0]["estimated_cost"] == 0
    assert plain[1]["calls"] == 0
    assert forced[0]["to_look_up"] == 1
    assert {t: p["calls"] for t, p in forced[1]["per_task"].items()} == {
        "results": 1, "site": finder.MAX_CANDIDATES + 1, "contacts": 2}


def test_the_call_is_refused_when_its_worst_case_doesnt_fit_the_run(tmp_path, monkeypatch):
    fake = FakeOpenAI(GOOD).install(monkeypatch)
    seen = []

    async def room(meter, estimate):
        seen.append(estimate)
        return "the run's classifier limit ($0.00) is reached"

    async def go():
        s = await _state(tmp_path / "harvey.db")
        mine = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"site": mine["id"]})
        st = (await classify.settings(s, "site")).for_task("site")
        counted = classify.request(st, "site", _site_payload())["tokens"]
        token = classify.METER.set(classify.Meter(room))
        try:
            ans = await classify.ask(s, "site", _site_payload())
        finally:
            classify.METER.reset(token)
        worst, _ = await classify.call_cost(s, st, "site", classify.worst_tokens(counted))
        return ans, worst, counted

    ans, worst, counted = run(go())
    assert ans.why == classify.RUN_BUDGET and fake.requests == []
    assert seen == [pytest.approx(worst)]
    assert classify.worst_tokens(counted) > counted


# ── Verifying a contact ──


async def _firm_with_site_contacts(s):
    slim.describe(s, slim.firm("EXEMPLU CONSTRUCTII SRL", 90000017, "Brașov",
                               address="Str. Florilor nr. 12"))
    cid = await s.add_company(Company(name="EXEMPLU CONSTRUCTII SRL", cui="90000017",
                                      location="Brașov, Brașov, Romania", source="registry:onrc"))
    await s.add_contacts(cid, [
        contacts.Found("email", "office@exemplu-constructii.test", source="website",
                       capability="read_site", evidence_url="https://exemplu-constructii.test/",
                       trust="likely"),
        contacts.Found("phone", "+40268000111", source="mf_taxpayers", trust="confirmed"),
        contacts.Found("phone", "+40268000111", source="website", capability="read_site",
                       trust="likely")])
    return cid


def _ids(db):
    with sqlite3.connect(db) as con:
        return {v: i for i, v in con.execute("SELECT id, value FROM contact")}


def _trusts(db, contact):
    with sqlite3.connect(db) as con:
        return sorted(r[0] for r in con.execute(
            "SELECT trust FROM link_evidence WHERE contact = ?", (contact,)))


def test_a_no_lowers_only_a_sites_evidence_and_its_undo_puts_it_back(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm_with_site_contacts(s)
        ids = _ids(db)
        email, phone = ids["office@exemplu-constructii.test"], ids["+40268000111"]
        a = await s.check_link(cid, email, verdict="no", batch="v1", profile="p", model="m")
        lowered = _trusts(db, email)
        again = await s.check_link(cid, email, verdict="yes", batch="v1")
        b = await s.check_link(cid, phone, verdict="no", batch="v1")
        c = await s.check_link(cid, phone, verdict="unsure", batch="v2")
        review = await s.read("contact_checks_open", "unsure")
        undone = await s.undo_contact_checks("v1")
        return a, lowered, again, b, c, review, undone, email, phone, \
            await s.read("contact_checks", email)

    a, lowered, again, b, c, review, undone, email, phone, checks = run(go())
    assert a == {"verdict": "no", "lowered": 1, "kept": ""} and lowered == [2]
    assert again["kept"] == "checked already in this run"
    assert b["lowered"] == 0 and "official source" in b["kept"]
    assert _trusts(db, phone) == [0, 1]                # never lowered
    assert [r["contact_id"] for r in review] == [phone]
    assert undone == {"checks": 2, "restored": 1} and _trusts(db, email) == [1]
    assert checks[0]["verdict"] == "no" and checks[0]["undone"]


def test_a_language_model_may_say_unsure_about_one_contact(tmp_path, monkeypatch):
    FakeOpenAI(json.dumps({"c1": "unsure"})).install(monkeypatch)
    card = _card(contacts=[("phone", "+40268000999", "Tel +40268000999")])

    async def go():
        s = await _state(tmp_path / "harvey.db")
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        return await classify.ask(s, "contacts", {**_site_payload(card), "unsure": True})

    ans = run(go())
    assert ans.value["unsure"] == [0] and contact_check.verdict_of(ans.value) == ("unsure", None)


@pytest.mark.parametrize("p, verdict", [(0.9, "yes"), (0.5, "unsure"), (0.2, "no")])
def test_a_decision_models_probability_is_yes_no_or_unsure_at_the_bar(p, verdict):
    card = _card(contacts=[("phone", "+40268000999", "Tel")])
    value = classify_decision.check_contacts(
        {"ch_0": {"company_own": p, "platform": 1 - p}},
        {"cards": [card], "shown_contacts": 12, "unsure": True}, 0.7)
    assert contact_check.verdict_of(value)[0] == verdict


def test_verifying_a_company_asks_about_each_link_once_and_keeps_the_verdicts(
        tmp_path, monkeypatch):
    fake = FakeOpenAI(json.dumps({"c1": "no"}), json.dumps({"c1": "yes"})).install(monkeypatch)
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm_with_site_contacts(s)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        ids = _ids(db)
        chosen = await contact_check.links(s, [ids["office@exemplu-constructii.test"],
                                               ids["+40268000111"]])
        est = await contact_check.estimate(s, chosen)
        first = await contact_check.verify_company(s, cid, chosen, batch="v1")
        second = await contact_check.verify_company(s, cid, chosen, batch="v1")
        return est, first, second

    est, first, second = run(go())
    assert est["calls"] == 2 and est["price_known"] and 0 < est["usd_typical"] < est["usd_high"]
    assert est["basis"] == "guess" and est["note"] == classify.ESTIMATE_NOTE
    assert (first["asked"], first["no"], first["yes"], first["lowered"]) == (2, 1, 1, 1)
    assert second["asked"] == 0 and len(fake.requests) == 2      # a resumed job asks the rest
    # (The state is the firm's facts and where the contact was found.)
    user = fake.requests[0]["messages"][1]["content"]
    assert "EXEMPLU CONSTRUCTII SRL" in user and "on file from website" in user


# ── The dashboard ──


@pytest.fixture
def client(tmp_path, monkeypatch):
    import harvey.dashboard as dash
    from fastapi.testclient import TestClient

    db = tmp_path / "harvey.db"
    monkeypatch.setenv("HARVEY_DASHBOARD_WORKER", "off")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: None)
    run(_state(db))
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def test_the_profiles_api_lists_adds_copies_and_sets_tasks(client):
    r = client.get("/api/classify/profiles").json()
    assert {p["id"] for p in r["profiles"]} >= set(profiles.builtins())
    assert r["tasks"]["site"]["when"] and r["use"] == {"results": "off", "site": "off",
                                                       "contacts": "off"}
    jev = next(p for p in r["profiles"] if p["id"] == "typesafe-jev")
    assert jev["can_answer"] == ["site", "contacts"] and jev["tested"] and jev["key_set"] is False
    bad = client.post("/api/classify/profiles", json={**LOCAL, "request_extra": {"model": "x"}})
    assert bad.status_code == 400
    assert client.post("/api/classify/profiles", json={**LOCAL, "key_name": "sk-1"}
                       ).status_code == 400
    mine = client.post("/api/classify/profiles", json=LOCAL).json()["profile"]
    assert not mine["builtin"]
    copy = client.post("/api/classify/profiles/typesafe-jev", json={"per_minute": 30}).json()
    assert copy["copied_from"] == "typesafe-jev" and copy["profile"]["based_on"] == "typesafe-jev"
    assert client.post("/api/classify/use", json={"results": "typesafe-jev"}).status_code == 400
    use = client.post("/api/classify/use", json={"site": mine["id"]}).json()
    assert use["use"]["site"] == mine["id"] and use["profile"] == mine["id"]
    est = client.post(f"/api/classify/profiles/{mine['id']}/test/estimate",
                      json={"tasks": ["site"]}).json()
    assert est["calls"] > 0 and est["price_known"] and 0 < est["usd_typical"] < est["usd_high"]
    assert "enforced" in est["note"]
    assert client.post(f"/api/classify/profiles/{mine['id']}/test",
                       json={"tasks": ["site"]}).status_code == 400     # no cap typed
    assert client.post(f"/api/classify/profiles/{mine['id']}/test/estimate",
                       json={"file": "/no/such/file.jsonl"}).status_code == 400
    assert client.post(f"/api/classify/profiles/{mine['id']}/remove").json()["off"] == ["site"]
    assert client.post("/api/classify/profiles/typesafe-jev/hide").json()["hidden"] == [
        "typesafe-jev"]


def test_a_test_file_is_a_jsonl_file_never_another(client, tmp_path):
    mine = client.post("/api/classify/profiles", json=LOCAL).json()["profile"]
    other = tmp_path / "notes.txt"
    other.write_text('{"id": "x"}\n')
    link = tmp_path / "cases.jsonl"
    link.symlink_to(other)
    for path in (other, link, tmp_path / ".env"):
        r = client.post(f"/api/classify/profiles/{mine['id']}/test/estimate",
                        json={"file": str(path)})
        assert r.status_code == 400


def test_a_find_contacts_run_takes_a_classifier_cap(client):
    r = client.post("/api/contacts/find", json={"company_ids": [], "classify_max_spend": "x"})
    assert r.status_code == 400 and r.json()["code"] == "bad_cap"


# ── No test reaches a .env it didn't write ──


def test_no_dotenv_is_loaded_without_a_path_and_classifier_keys_are_clear(monkeypatch):
    import os

    import dotenv

    assert dotenv.load_dotenv() is False
    assert not any(k.startswith("CLASSIFY_") for k in os.environ)
    assert "DEEPINFRA_API_KEY" not in os.environ and "TYPESAFE_API_KEY" not in os.environ


def test_a_tmp_env_file_still_loads(tmp_path, monkeypatch):
    import os

    import dotenv

    env = tmp_path / ".env"
    env.write_text("HARVEY_TEST_ONLY_VALUE=fictional\n")
    monkeypatch.delenv("HARVEY_TEST_ONLY_VALUE", raising=False)
    assert dotenv.load_dotenv(env) is True and os.environ["HARVEY_TEST_ONLY_VALUE"] == "fictional"
    monkeypatch.delenv("HARVEY_TEST_ONLY_VALUE")



def test_a_verification_run_pauses_at_its_classifier_cap_and_carries_on_when_raised(
        tmp_path, monkeypatch):
    from harvey.modules.job_kinds import enqueue_contact_checks
    from tests.test_jobs import run_until_idle

    fake = FakeOpenAI(json.dumps({"c1": "yes"}), json.dumps({"c1": "yes"})).install(monkeypatch)
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm_with_site_contacts(s)
        p = await profiles.save(s, LOCAL)
        await profiles.set_use(s, {"contacts": p["id"]})
        chosen = await contact_check.links(s, list(_ids(db).values()))
        batch, new, _ = await enqueue_contact_checks(s, chosen, max_spend=0.0)
        await run_until_idle(s)
        paused = await jobs.get(s, new[0])
        sent = len(fake.requests)
        await jobs.set_classify_cap(s, batch, 1.0)
        await jobs.resume(s, new[0])
        await run_until_idle(s)
        return cid, paused, sent, await jobs.get(s, new[0]), await jobs.batch_report(s, batch)

    cid, paused, sent, done, report = run(go())
    assert paused.status == jobs.PAUSED and paused.paused_reason == jobs.SPEND_LIMIT
    assert sent == 0 and "classifier limit" in paused.error
    assert done.status == jobs.DONE and done.result["yes"] == 2
    assert report["classify"]["calls"] == 2 and report["classify"]["usd"] > 0
    # (Its classifier calls are its spend: the day's limits count them.)
    assert done.actual_cost == pytest.approx(report["classify"]["usd"], abs=1e-6)


def test_no_profile_of_yours_takes_the_older_settings_id(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        return await profiles.save(s, {**LOCAL, "name": "Settings"})

    assert run(go())["id"] == "settings-2"


def test_a_held_profile_rests_only_the_jobs_that_ask_it(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        verify = await jobs.enqueue(s, "verify_contacts", "classify", {"company_id": "1"})
        test = await jobs.enqueue(s, "classify_test", "classify", {"profile": "local"})
        await jobs.rest_lane(s, "classify", 600, kind="verify_contacts")
        return (await jobs.get(s, verify)).not_before, (await jobs.get(s, test)).not_before

    rested, other = run(go())
    # (The contacts profile is held: another profile's test on the lane goes on.)
    assert rested and other is None


def test_two_checks_at_once_never_both_take_the_last_free_one(tmp_path, monkeypatch):
    from harvey import verify_costs

    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "reoon": {"module": "email_verify", "kind": "check", "free_per_month": 1,
                  "usd": 0.01}})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s)
        both = await asyncio.gather(meter.ask("reoon"), meter.ask("reoon"))
        return both, meter.usd, await verify_costs.used(s, "reoon")

    both, usd, used = run(go())
    assert both == [True, True] and usd == {"reoon": 0.01} and used == 2
