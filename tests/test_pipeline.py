"""Which companies still need their site read, and how what was read counts.

Site reads are found by a query, not handed a list by discovery. These
tests pin that property down, because it is what makes a crashed run
resumable and re-observation a time series instead of a duplicate.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.models import Company
from harvey.signals import SIGNAL_CATALOG, sync_vocabulary
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


# ── The join: which companies still need a look ──


@pytest.mark.asyncio
async def test_a_new_company_needs_profiling(state):
    await state.add_company(Company(name="Baker", domain="baker.example"))
    pending = await state.companies_needing_profile()
    assert [c["domain"] for c in pending] == ["baker.example"]
    assert await state.count_companies_needing_profile() == 1


@pytest.mark.asyncio
async def test_a_profiled_company_drops_out(state):
    cid = await state.add_company(Company(name="Baker", domain="baker.example"))
    await state.add_observation("TECH_STACK", company_id=cid,
                                collector="profile", value_text="wordpress")
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_a_company_read_through_the_browser_drops_out_too(state):
    cid = await state.add_company(Company(name="Baker", domain="baker.example"))
    await state.add_observation("TECH_STACK", company_id=cid,
                                collector="profile:browser", value_text="wix")
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_a_stale_profile_comes_back_round(state):
    """Re-reading on a schedule is what makes 'they dropped their agency'
    visible. A company profiled last year is not done forever."""
    cid = await state.add_company(Company(name="Baker", domain="baker.example"))
    await state.add_observation("TECH_STACK", company_id=cid, collector="profile",
                                value_text="wordpress",
                                observed_at="2020-01-01 00:00:00")
    assert await state.count_companies_needing_profile(stale_days=90) == 1
    assert await state.count_companies_needing_profile(stale_days=99999) == 0


@pytest.mark.asyncio
async def test_discovery_observations_do_not_count_as_a_profile(state):
    """DISCOVER writes observations too. Only PROFILE's own count."""
    cid = await state.add_company(Company(name="Baker", domain="baker.example"))
    await state.add_observation("SERP_RANK", company_id=cid,
                                collector="discover", value_num=14)
    assert await state.count_companies_needing_profile() == 1


@pytest.mark.asyncio
async def test_websiteless_companies_are_never_queued_for_profiling(state):
    """No site to read. That is the NO_WEBSITE cohort, not a backlog."""
    await state.add_company(Company(name="No Site", external_id="osm:node/1"))
    assert await state.count_companies_needing_profile() == 0


# ── The backlog ──


@pytest.mark.asyncio
async def test_state_summary_reports_the_profiling_backlog(state):
    await state.add_company(Company(name="Baker", domain="baker.example"))
    summary = await state.get_state_summary()
    assert summary["unprofiled"] == 1


# ── Cohorts read the value, not just the row ──


@pytest.mark.asyncio
async def test_a_false_boolean_is_recorded_but_not_in_the_cohort(state):
    """PROFILE records RUNNING_GOOGLE_ADS=0 for everyone it checks — knowing
    you looked is worth keeping, and a later 0→1 flip is a real event. But
    "companies running Google Ads" must not silently mean "companies we
    checked for Google Ads", which is everyone.
    """
    running = await state.add_company(Company(name="Spender", domain="a.com"))
    checked = await state.add_company(Company(name="Not spending", domain="b.com"))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=running,
                                collector="profile", value_num=1)
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=checked,
                                collector="profile", value_num=0)

    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == [running]

    counts = {c["signal_code"]: c for c in await state.signal_counts()}
    assert counts["RUNNING_GOOGLE_ADS"]["companies"] == 1

    # The negative finding is still on disk.
    rows = await state.get_observations(company_id=checked)
    assert rows[0]["value_num"] == 0


@pytest.mark.asyncio
async def test_text_signals_count_by_their_existence(state):
    """INCUMBENT_AGENCY has no numeric value — the row IS the finding."""
    cid = await state.add_company(Company(name="Baker", domain="a.com"))
    await state.add_observation("INCUMBENT_AGENCY", company_id=cid,
                                collector="profile", value_text="Studio3")
    assert await state.cohort(["INCUMBENT_AGENCY"]) == [cid]


@pytest.mark.asyncio
async def test_a_cohort_reflects_the_newest_observation(state):
    """They dropped their agency last quarter. The cohort should say so."""
    cid = await state.add_company(Company(name="Baker", domain="a.com"))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=cid,
                                collector="profile", value_num=1,
                                observed_at="2026-01-01 00:00:00")
    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == [cid]

    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=cid,
                                collector="profile", value_num=0,
                                observed_at="2026-06-01 00:00:00")
    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == []

    # Both observations survive — that time series is the durable asset.
    assert len(await state.get_observations(company_id=cid)) == 2


@pytest.mark.asyncio
async def test_exclusions_also_read_the_value(state):
    """Excluding "has online booking" must not exclude everyone who was
    merely checked for it."""
    a = await state.add_company(Company(name="A", domain="a.com"))
    b = await state.add_company(Company(name="B", domain="b.com"))
    for cid, booking in ((a, 1), (b, 0)):
        await state.add_observation("NO_SCHEMA_MARKUP", company_id=cid,
                                    collector="profile", value_num=1)
        await state.add_observation("NO_ONLINE_BOOKING", company_id=cid,
                                    collector="profile", value_num=booking)

    both = await state.cohort(["NO_SCHEMA_MARKUP"])
    assert sorted(both) == sorted([a, b])

    # Only A actually lacks booking, so excluding it leaves B.
    assert await state.cohort(["NO_SCHEMA_MARKUP"], exclude=["NO_ONLINE_BOOKING"]) == [b]
