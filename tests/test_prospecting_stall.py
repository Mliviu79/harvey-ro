"""Prospects with no address Harvey can write to are counted apart, and
`harvey status` says why nothing is written when only they came in for a
day (no verifier key, or its credits ran out), never for good.
"""

import pytest
import pytest_asyncio

from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "stall.db"))
    await state.init_db()
    return state


async def add(state, n, email, email_status):
    await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        email=email, email_status=email_status,
    ))


@pytest.mark.asyncio
async def test_summary_counts_only_prospects_the_writer_can_use(state):
    await add(state, 1, "a@one.test", "verified")
    await add(state, 2, "b@two.test", "risky")
    await add(state, 3, "c@three.test", "guess")
    await add(state, 4, "d@four.test", "invalid")
    await add(state, 5, "", "")
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 5
    assert summary["writable_prospects"] == 2


async def age_all(state, days):
    """Pretend every prospect was found ``days`` ago."""
    import aiosqlite
    from datetime import datetime, timedelta, timezone
    then = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET created_at = ?", (then,))
        await db.commit()


@pytest.mark.asyncio
async def test_a_day_of_unusable_prospects_says_why_nothing_is_written(state):
    # With no usable email at all (say, no verifier key), the Writer has
    # nothing to draft for. A day's worth of them, and status says why.
    from harvey.main import PROSPECT_BACKLOG, unusable_backlog_notice
    for n in range(PROSPECT_BACKLOG):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    summary = await state.get_state_summary()
    notice = unusable_backlog_notice(summary)
    assert str(PROSPECT_BACKLOG) in notice and "verifier" in notice and "about a day" in notice
    assert "paused prospecting" not in notice


@pytest.mark.asyncio
async def test_the_notice_never_becomes_permanent(state):
    # Nothing re-checks a guessed address, so an all-time count would say
    # it for good. Once the guesses are a day old, it stops.
    from harvey.main import PROSPECT_BACKLOG, unusable_backlog_notice
    for n in range(PROSPECT_BACKLOG + 10):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    await age_all(state, days=2)
    summary = await state.get_state_summary()
    assert unusable_backlog_notice(summary) == ""


@pytest.mark.asyncio
async def test_no_backlog_notice_while_things_are_moving(state):
    from harvey.main import unusable_backlog_notice
    await add(state, 1, "p1@co1.test", "guess")
    assert unusable_backlog_notice(await state.get_state_summary()) == ""
    await add(state, 2, "p2@co2.test", "verified")
    for n in range(3, 30):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    assert unusable_backlog_notice(await state.get_state_summary()) == ""
