"""Address checks and Hunter's pattern lookups in dollars (harvey/
verify_costs.py): each service's free allowance comes from the manifests
and its price from what the user entered; a request is $0 inside the
allowance and its price past it, and past it with no price it is refused
under a USD cap (a searches row error, not a check done) and counted
unpriced with none. Each service asked is one check, the same in its own
counter and in EMAIL_VERIFY_MONTHLY_CHECKS. The run's report is stored
once the run is done; the estimate says what the run will do. Every
verifier is a fake; all data is fictional."""

import asyncio

import pytest

from harvey import finder, verify_costs
from harvey.integrations import email_finder as ef
from harvey.modules import ingest, jobs, runs
from tests.test_contact_finder import _company, _state


def run(coro):
    return asyncio.run(coro)


def _spec(free, usd=None, kind="check", module="email_verify"):
    return {"module": module, "kind": kind, "free_per_month": free, "usd": usd}


class _Spend:
    """A run's Spend with a cap: what was reserved and recorded."""

    def __init__(self, room=1.0):
        self.room, self.reserved, self.recorded = room, [], []
        self.spend = finder.Spend(self._reserve, self._record)

    async def _reserve(self, estimate, first, **_):
        if first > self.room:
            raise jobs.SpendLimit("the run's spend limit ($0.00) is reached")
        self.reserved.append(estimate)
        return self.room

    async def _record(self, amount):
        self.recorded.append(amount)


def _network(monkeypatch, *, zerobounce=None, reoon=None, hunter=None, pattern=None):
    """A Google-hosted domain (ZeroBounce, then Reoon, then Hunter) whose
    verifiers answer from these verdicts, counting what each was asked."""
    asked = []

    async def mx(domain):
        return "aspmx.l.google.com"

    def answer(name, verdict):
        async def fake(email, key):
            asked.append(name)
            return verdict
        return fake

    async def domain_pattern(domain, key):
        asked.append("hunter_search")
        return pattern

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "verify_zerobounce", answer("zerobounce", zerobounce))
    monkeypatch.setattr(ef, "verify_reoon", answer("reoon", reoon))
    monkeypatch.setattr(ef, "verify_hunter", answer("hunter", hunter))
    monkeypatch.setattr(ef, "_hunter_domain_pattern", domain_pattern)
    return asked


async def _firm(s):
    cid = await _company(s, name="ETA EXAMPLE SRL", domain="eta.example")
    await s.add_people(cid, [("POPESCU ION", "administrator")], source="onrc")
    await s.record_search("company", cid, "website", "read_site", looked_for=runs.CONTACT_READ)
    return cid


def _rows(db, sql):
    import sqlite3

    with sqlite3.connect(db) as con:
        return con.execute(sql).fetchall()


# ── Prices ──


def test_the_manifests_declare_free_allowances_and_no_paid_price():
    v = verify_costs.verifiers()
    assert {k: (x["module"], x["kind"], x["free_per_month"]) for k, x in v.items()} == {
        "reoon": ("email_verify", "check", 600), "zerobounce": ("email_verify", "check", 100),
        "hunter": ("hunter", "check", 50), "hunter_search": ("hunter", "search", 25)}
    # (No paid price is declared anywhere: the user enters theirs.)
    assert all(x["usd"] is None for x in v.values())


def test_an_entered_price_wins_and_a_blank_one_clears_it(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        await verify_costs.set_price(s, "reoon", 0.004)
        await verify_costs.set_price(s, "hunter_search", "0.1")
        entered = await verify_costs.prices(s)
        await verify_costs.set_price(s, "reoon", "")
        cleared = await verify_costs.prices(s)
        problems = []
        for service, usd in (("reoon", "abc"), ("reoon", -1), ("nobody", 1)):
            try:
                await verify_costs.set_price(s, service, usd)
            except ValueError as e:
                problems.append(str(e))
        return entered, cleared, problems

    entered, cleared, problems = run(go())
    assert (entered["reoon"]["usd"], entered["reoon"]["entered"]) == (0.004, 0.004)
    assert entered["hunter_search"]["usd"] == 0.1 and entered["zerobounce"]["usd"] is None
    assert cleared["reoon"]["usd"] is None and cleared["hunter_search"]["usd"] == 0.1
    assert len(problems) == 3


# ── The Meter: counted, priced, refused as asked ──


def test_a_request_is_free_inside_the_allowance_and_priced_past_it(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"zerobounce": _spec(1, 0.008)})
    spend = _Spend()

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s, spend.spend)
        return [await meter.ask("zerobounce") for _ in range(2)], meter

    made, meter = run(go())
    assert made == [True, True] and meter.usd == {"zerobounce": 0.008}
    assert spend.reserved == [0.008] and spend.recorded == [0.008]
    assert spend.spend.by == {"checks": 0.008}


def test_past_the_allowance_with_no_price_is_refused_under_a_cap_and_counted_without(
        tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(1)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        capped = await verify_costs.Meter.start(s, _Spend().spend)
        under = [await capped.ask("reoon") for _ in range(2)]
        used = await verify_costs.used(s, "reoon")
        free = await verify_costs.Meter.start(s)        # (the audience: no USD cap)
        return under, used, capped.refused, await free.ask("reoon"), free.unpriced

    under, used, refused, made, unpriced = run(go())
    assert under == [True, False] and used == 1
    assert refused == [("reoon", "no price for reoon past its free tier")]
    assert made is True and unpriced == {"reoon": 1}


def test_a_request_the_runs_money_cant_pay_for_is_refused_and_not_counted(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0, 0.01)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s, _Spend(room=0.0).spend)
        return await meter.ask("reoon"), meter.refused, await verify_costs.used(s, "reoon")

    made, refused, used = run(go())
    assert made is False and used == 0 and "spend limit" in refused[0][1]


def test_each_service_asked_is_one_check_in_both_counts(tmp_path, monkeypatch):
    # ZeroBounce can't decide, Reoon does: two checks at two services,
    # and two of the month's EMAIL_VERIFY_MONTHLY_CHECKS.
    _network(monkeypatch, zerobounce={"status": "unknown"}, reoon={"status": "safe"})
    env = {"zerobounce_api_key": "k", "reoon_api_key": "k"}

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s)
        found = await ef.find_email("Ion", "Popescu", "eta.example", env=env, budget=meter)
        return (found.status, {v: await verify_costs.used(s, v) for v in ("zerobounce", "reoon")},
                await verify_costs.checks_used(s), meter.total())

    status, per_service, total, made = run(go())
    assert status == "verified" and per_service == {"zerobounce": 1, "reoon": 1}
    assert total == 2 and made == (2, 0.0, 0)


def test_the_months_checks_count_every_service_and_the_old_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_VERIFY_MONTHLY_CHECKS", "3")

    async def go():
        s = await _state(tmp_path / "harvey.db")
        # (An install upgraded mid-month: the checks counted before.)
        await ingest.Allowance(s).take("email_verify")
        meter = await verify_costs.Meter.start(s)
        made = [await meter.ask("reoon"), await meter.ask("zerobounce"), await meter.ask("reoon")]
        return made, meter.refused, await verify_costs.checks_left(s)

    made, refused, left = run(go())
    assert made == [True, True, False] and left == 0
    assert "EMAIL_VERIFY_MONTHLY_CHECKS" in refused[0][1]


def test_hunters_pattern_lookup_is_priced_and_refused_like_a_check(tmp_path, monkeypatch):
    asked = _network(monkeypatch, pattern=("first.last", 0.8))
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "hunter_search": _spec(1, kind="search", module="hunter")})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        meter = await verify_costs.Meter.start(s, _Spend().spend)
        first = await ef.derive_pattern("eta.example", budget=meter)
        second = await ef.derive_pattern("theta.example", budget=meter)
        await verify_costs.set_price(s, "hunter_search", 0.05)
        spend = _Spend()
        priced = await verify_costs.Meter.start(s, spend.spend)
        third = await ef.derive_pattern("iota.example", budget=priced)
        return first, second, third, meter.refused, spend, await ingest.Allowance(s).left(
            "hunter")

    first, second, third, refused, spend, left = run(go())
    assert first[1] == "hunter_domain_search" and second[1] == "default"
    assert refused == [("hunter_search", "no price for hunter_search past its free tier")]
    assert third[1] == "hunter_domain_search" and spend.spend.by == {"lookups": 0.05}
    # (One counter for HUNTER_MONTHLY_SEARCHES and the free allowance.)
    assert asked == ["hunter_search", "hunter_search"] and left == 25 - 2


# ── The finder ──


def test_the_finder_records_what_each_request_cost(tmp_path, monkeypatch):
    _network(monkeypatch, zerobounce={"status": "valid"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"zerobounce": _spec(0, 0.008)})
    spend = _Spend()

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _firm(s)
        return await finder.find(s, cid, spend=spend.spend,
                                 env={"zerobounce_api_key": "test-key"})

    found = run(go())
    assert spend.reserved == [0.008] and spend.recorded == [0.008]
    assert found.contacts == 1
    assert found.as_dict()["spend"] == {
        "searches": 0.0, "checks": 0.008, "checks_made": 1, "checks_refused": 0,
        "lookups": 0.0, "lookups_made": 0, "services": {"zerobounce": 1}}


def test_a_check_that_needed_an_unpriced_service_is_an_error_not_a_check_done(tmp_path,
                                                                              monkeypatch):
    # ZeroBounce (free) can't decide; Reoon is past its free tier with no
    # price: refused under the run's cap, so the check isn't done.
    asked = _network(monkeypatch, zerobounce={"status": "unknown"}, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "zerobounce": _spec(100), "reoon": _spec(0)})
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        found = await finder.find(s, cid, spend=_Spend().spend,
                                  env={"zerobounce_api_key": "k", "reoon_api_key": "k"})
        return found, await s.read("finder_people", cid, *finder.EMAIL)

    found, still = run(go())
    assert asked == ["zerobounce"] and found.contacts == 0
    assert found.checks_refused == 1 and found.checks == 1
    assert any("no price for reoon past its free tier" in why for why in found.skipped)
    ((outcome, detail),) = _rows(db, "SELECT outcome, detail FROM search WHERE is_person = 1")
    assert outcome == 0 and detail.startswith("no price for reoon past its free tier")
    # (Not a search done: the person is searched again by the next run.)
    assert [p["name"] for p in still] == ["POPESCU ION"]


def test_a_check_with_every_service_unpriced_isnt_made(tmp_path, monkeypatch):
    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _firm(s)
        return await finder.find(s, cid, spend=_Spend().spend, env={"reoon_api_key": "k"})

    found = run(go())
    assert asked == [] and found.checks_refused == 1
    assert found.skipped[-1] == "address check skipped: no price for reoon past its free tier"


def test_a_check_a_free_service_answered_keeps_its_answer(tmp_path, monkeypatch):
    # ZeroBounce is refused, Reoon (free) decides: the check is done.
    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "zerobounce": _spec(0), "reoon": _spec(600)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _firm(s)
        return await finder.find(s, cid, spend=_Spend().spend,
                                 env={"zerobounce_api_key": "k", "reoon_api_key": "k"})

    found = run(go())
    assert asked == ["reoon"] and found.contacts == 1 and found.checks_refused == 0


def test_a_refused_check_still_links_the_next_persons_published_address(tmp_path,
                                                                        monkeypatch):
    # Nobody's check can be made (Reoon unpriced past its free tier), but
    # the second person's own address is on the site: linked, for nothing.
    from harvey import contacts

    asked = _network(monkeypatch, reoon={"status": "safe"})
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(0)})
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        cid = await _firm(s)
        await s.add_people(cid, [("IONESCU ANA", "director")], source="onrc")
        await s.add_contacts(cid, [contacts.Found(
            "email", "ana.ionescu@eta.example", source="website", capability="read_site",
            evidence_url="https://eta.example/contact", trust="confirmed")])
        found = await finder.find(s, cid, spend=_Spend().spend, env={"reoon_api_key": "k"})
        return found

    found = run(go())
    assert asked == [] and found.checks_refused == 1
    outcomes = {0: "error", 1: "found", 2: "nothing"}
    assert sorted((n, outcomes[o]) for n, o in _rows(
        db, "SELECT p.name, s.outcome FROM search s JOIN person p ON p.id = s.target "
            "WHERE s.is_person = 1")) == [("IONESCU ANA", "found"), ("POPESCU ION", "error")]


def test_a_price_is_a_number_of_dollars_within_bounds(tmp_path):
    async def go():
        s = await _state(tmp_path / "harvey.db")
        refused = []
        for usd in (verify_costs.MAX_USD + 1, "nan", "inf", -0.01, True):
            try:
                await verify_costs.set_price(s, "reoon", usd)
            except ValueError:
                refused.append(usd)
        await verify_costs.set_price(s, "reoon", verify_costs.MAX_USD)
        return refused, (await verify_costs.prices(s))["reoon"]["usd"]

    refused, kept = run(go())
    # (True reads as 1.0 to float(): a number, so it is kept as $1.)
    assert refused == [verify_costs.MAX_USD + 1, "nan", "inf", -0.01] and kept == 10.0


def test_two_requests_at_once_never_both_take_the_months_last(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_VERIFY_MONTHLY_CHECKS", "1")
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "1")
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {
        "reoon": _spec(600), "zerobounce": _spec(100),
        "hunter_search": _spec(25, kind="search", module="hunter")})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        a, b = (await verify_costs.Meter.start(s), await verify_costs.Meter.start(s))
        checks = await asyncio.gather(a.ask("reoon"), b.ask("zerobounce"))
        lookups = await asyncio.gather(a.take("hunter"), b.take("hunter"))
        return checks, lookups, await verify_costs.checks_used(s), \
            await ingest.Allowance(s).left("hunter")

    checks, lookups, used, left = run(go())
    assert sum(checks) <= 1 and used == sum(checks)
    assert sorted(lookups) == [False, True] and left == 0


def test_a_request_that_cant_be_counted_is_refused_under_a_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(600)})

    async def go():
        s = await _state(tmp_path / "harvey.db")

        async def broken(key, by=1):
            raise OSError("disk I/O error")

        capped = await verify_costs.Meter.start(s, _Spend().spend)
        free = await verify_costs.Meter.start(s)
        monkeypatch.setattr(s, "increment_setting", broken)
        return await capped.ask("reoon"), await free.ask("reoon")

    assert run(go()) == (False, True)


# ── The run's report and the estimate ──


def test_a_finished_runs_report_is_stored_and_cleared_when_a_job_runs_again(tmp_path):
    db = tmp_path / "harvey.db"

    async def go():
        s = await _state(db)
        batch = await jobs.new_batch(s, "find_contacts", "t", max_spend=1.0)
        job_id = await jobs.enqueue(s, "find_contacts", "contacts", {}, batch_id=batch)
        running = await jobs.batch_report(s, batch)
        stored_while_running = _rows(db, "SELECT report_json FROM job_batches")
        await jobs.finish(s, job_id, jobs.FAILED, result={"spend": {
            "searches": 0.0, "checks": 0.016, "checks_made": 3, "checks_refused": 1,
            "lookups": 0.05, "lookups_made": 1, "services": {"reoon": 2, "zerobounce": 1}}})
        report = await jobs.batch_report(s, batch)
        # (Read from the row: what is written there is what is given.)
        with __import__("sqlite3").connect(db) as con:
            con.execute("UPDATE job_batches SET report_json = json_set(report_json, "
                        "'$.report.checks_made', 99)")
        kept = await jobs.batch_report(s, batch)
        await jobs.retry(s, job_id)
        cleared = _rows(db, "SELECT report_json FROM job_batches")
        return running, stored_while_running, report, kept, cleared

    running, stored_while_running, report, kept, cleared = run(go())
    assert running["statuses"] == {"queued": 1} and stored_while_running == [(None,)]
    assert (report["checks_made"], report["checks_usd"], report["checks_refused"]) == (
        3, 0.016, 1)
    assert report["services"] == {"reoon": 2, "zerobounce": 1}
    assert (report["lookups_made"], report["lookups_usd"]) == (1, 0.05)
    line = jobs.report_line(report)
    assert "reoon 2, zerobounce 1" in line and "1 refused" in line and "1 pattern lookups" in line
    assert kept["checks_made"] == 99 and cleared == [(None,)]


def test_the_estimate_prices_checks_and_names_the_services_it_would_refuse(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _company(s, name="ETA EXAMPLE SRL", domain="eta.example")
        for _ in range(100):
            await s.increment_setting(verify_costs._key("zerobounce"))
        await verify_costs.set_price(s, "zerobounce", 0.01)
        return await finder.checks_estimate(
            s, [{"id": cid, "site": True}], {"email_verify", "hunter"},
            {"reoon_api_key": "k", "zerobounce_api_key": "k"})

    est = run(go())
    n = finder.MAX_PEOPLE_PER_COMPANY
    assert est["checks"] == n and est["lookups"] == 1
    assert est["verifiers"] == ["zerobounce", "reoon", "hunter"]
    # ZeroBounce's free 100 are used: n at the price entered; Hunter's 50
    # and 25 and Reoon's 600 still cover the rest.
    assert est["usd_high"] == pytest.approx(n * 0.01) and est["refused"] == []
    assert est["free_left"]["zerobounce"] == 0 and est["checks_left"] == 600 - 100
    assert est["prices"]["zerobounce"] == 0.01 and est["prices"]["reoon"] is None


def test_the_estimate_names_an_unpriced_service_past_its_free_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_costs, "verifiers", lambda: {"reoon": _spec(1)})

    async def go():
        s = await _state(tmp_path / "harvey.db")
        cid = await _company(s, name="ETA EXAMPLE SRL", domain="eta.example")
        return await finder.checks_estimate(s, [{"id": cid, "site": True}], {"email_verify"},
                                            {"reoon_api_key": "k"})

    assert run(go())["refused"] == ["reoon"]


def test_the_estimate_leaves_out_firms_the_run_builds_no_address_for(tmp_path):
    # A found site waiting for its check, and a firm with no website that
    # every search has looked up already: the run checks no address there.
    rows = [{"id": "a", "site": True}, {"id": "b", "site": True, "website_waiting": True},
            {"id": "c", "site": False, "look_with": []},
            {"id": "d", "site": False, "look_with": ["a search"]}]

    async def go():
        s = await _state(tmp_path / "harvey.db")
        return await finder.checks_estimate(s, rows, {"email_verify"}, {"reoon_api_key": "k"})

    assert run(go())["checks"] == 2 * finder.MAX_PEOPLE_PER_COMPANY


def test_settings_show_and_take_the_prices(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import harvey.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        bad = c.post("/api/verify/prices", json={"reoon": "abc"})
        too_much = c.post("/api/verify/prices", json={"reoon": 25, "zerobounce": 0.01})
        unknown = c.post("/api/verify/prices", json={"nobody": 1})
        saved = c.post("/api/verify/prices", json={"reoon": 0.004, "hunter_search": None})
        shown = c.get("/api/verify/prices").json()
    assert bad.status_code == 400 and unknown.status_code == 400
    # (One price out of bounds: nothing of the request is stored.)
    assert too_much.status_code == 400
    assert saved.json()["success"] is True
    by = {r["id"]: r for r in shown["services"]}
    assert by["reoon"]["entered"] == 0.004 and by["hunter_search"]["entered"] is None
    assert by["zerobounce"]["entered"] is None
    assert shown["checks"] == {"cap": 600, "used": 0}


PRICES = r'''
global.api = async (path, opts) => {
  if (opts && opts.method === 'POST') { posted.push([path, JSON.parse(opts.body)]); return {success: true}; }
  return {month: '2026-11', checks: {cap: 600, used: 130}, services: [
    {id: 'reoon', module: 'email_verify', kind: 'check', free_per_month: 600, used: 30, past_free: 0,
     entered: null, declared: null, usd: null},
    {id: 'zerobounce', module: 'email_verify', kind: 'check', free_per_month: 100, used: 100, past_free: 0,
     entered: 0.008, declared: null, usd: 0.008},
    {id: 'hunter_search', module: 'hunter', kind: 'search', free_per_month: 25, used: 27, past_free: 2,
     entered: null, declared: null, usd: null}]};
};
vm.runInThisContext(pick('// Settings: what one request to each verification service', 'async function setFinderBg('));
el('verify-prices');
(async () => {
  await loadVerifyPrices();
  const card = els['verify-prices'].innerHTML;
  el('vp-reoon').value = '0.004'; el('vp-zerobounce').value = ''; el('vp-hunter_search').value = '0.05';
  await saveVerifyPrices();
  console.log(JSON.stringify({card, posted, toasts}));
})().catch(e => {console.error(e); process.exit(1);});
'''


def test_settings_show_each_services_month_and_take_a_price_in_both_languages():
    from tests.test_classifier_card import PRELUDE, _js

    out = {lang: _js(PRELUDE + PRICES, lang) for lang in ("ro", "en")}
    en, ro = out["en"], out["ro"]
    assert "this month 130 of 600 (EMAIL_VERIFY_MONTHLY_CHECKS" in en["card"]
    assert "zerobounce check (USD)" in en["card"] and 'value="0.008"' in en["card"]
    assert "hunter_search pattern lookup (USD)" in en["card"]
    assert "25 free a month; this month 27, 2 of them past the free ones." in en["card"]
    assert "No price: those past it weren" in en["card"]
    assert "Căutare de tipar hunter_search" in ro["card"] and "luna asta 130 din 600" in ro["card"]
    assert en["posted"] == [["/api/verify/prices",
                             {"reoon": 0.004, "zerobounce": None, "hunter_search": 0.05}]]
    assert en["toasts"] == [["Prices saved.", "success"]]


def test_every_new_price_and_check_string_is_in_both_languages():
    from tests.test_classifier_card import _js

    script = r'''
const fs = require('fs'), vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const pre = ['set.vp_', 'finder.k_', 'run.checks', 'run.lookups'];
const pick = (L) => Object.keys(I18N[L]).filter(k => pre.some(p => k.startsWith(p))).sort();
console.log(JSON.stringify({ro: pick('ro'), en: pick('en')}));
'''
    keys = _js(script)
    assert keys["ro"] == keys["en"] and "set.vp_desc" in keys["en"] \
        and "finder.k_need_cap" in keys["en"] and "run.lookups" in keys["en"]
