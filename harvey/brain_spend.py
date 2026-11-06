"""What the brain costs when it is billed per token (``brain.auth:
api_key``), and the caps that hold it.

In the subscription modes (terminal, account) nothing here applies: the
plan's own windows hold Harvey back (Brain.is_within_budget), and no call
is counted as money.

With an API key:

- Every call of Harvey's own model (the brain's, the agent's chats, wakes
  and tasks) is a usage_events row with ``source`` SOURCE and its cost:
  the one Claude's CLI reports with the call (``costUSD`` /
  ``total_cost_usd``), or, for Codex, whose app-server reports tokens only,
  its tokens at the prices entered (``brain.codex_usd_per_mtok_in`` /
  ``_out``, USD per million; cached input is priced as input, so a cost is
  never lower than it was). A cost that is unknown is never read as $0:
  a call that ended with no cost (a CLI that timed out, died or was
  stopped before its result, or a result with tokens and no cost) is
  counted at a turn's bound for each turn it may have used
  (``unknown_usd``, source SOURCE_ESTIMATE), and an unpriced Codex call
  (no cap set) keeps its tokens (source SOURCE_UNPRICED), priced when the
  spend is read at the prices entered by then.
- ``brain.usd_per_day`` / ``usd_per_month`` (UTC; None: no cap) are
  checked before each call against what those rows already cost
  (``refusal``): reached, the call isn't made. A cap of 0 is no calls.
  With a cap set and Codex unpriced the spend can't be counted, so no call
  is made either ("no price entered"). Brain's daily call counter stays as
  a backstop under the caps.
- An agent wake or task is a job (agent lane): before it starts it holds
  its worst case (``run_worst_usd``) of its plan's budget (plans.hold_run,
  the same transaction as an action's reserve) and of the install's
  ``spend.per_day_usd`` / ``per_month_usd`` (jobs.reserve), and its cost
  goes into the job's actual_cost when it ends (wakes.charge), which is
  what the plan's committed money and the install's day and month limits
  count. The chat isn't a job: it is held by the brain caps only.

A run's worst case is its turn limit (chat.MAX_TURNS for a wake,
tasks.TASK_TURNS for a task) times the most a turn may cost: the largest
cost per turn of the agent's own API-key runs on this provider in the last
OBSERVED_DAYS, and never less than a turn's bound by default: for Claude
TURN_USD_FLOOR (a turn re-reads the conversation, which the chat's history
keeps within chat.PRIME_TOKENS plus the prompt, the tools and their
results, about TURN_INPUT_TOKENS, mostly from the CLI's cache, and writes
up to TURN_OUTPUT_TOKENS), for Codex those tokens at the prices entered.
A run isn't stopped part-way when it costs more: the hold is the bound
it starts under, and what it really cost is what is counted after.
"""

from __future__ import annotations

import datetime as _dt
import logging

import harvey.overlay.queries  # noqa: F401  (registers the named statements)
from harvey import queries

logger = logging.getLogger("harvey.brain")

API_KEY = "api_key"
# usage_events.source of a call billed to the API key: its cost as
# reported (or priced), counted at a turn's bound (unknown), or Codex's
# tokens with no price entered yet.
SOURCE = "api_key"
SOURCE_ESTIMATE = "api_key_estimate"
SOURCE_UNPRICED = "api_key_unpriced"
# A turn's bound (see the module's doc).
TURN_INPUT_TOKENS = 60_000
TURN_OUTPUT_TOKENS = 4_000
TURN_USD_FLOOR = 0.10
OBSERVED_DAYS = 30
EPSILON = 1e-9


def api_key(brain) -> bool:
    return (getattr(brain, "auth", "") or "") == API_KEY


def _has_prices(brain) -> bool:
    return (getattr(brain, "codex_usd_per_mtok_in", None) is not None
            and getattr(brain, "codex_usd_per_mtok_out", None) is not None)


def priced(brain) -> bool:
    """Whether this brain's calls get a cost: Claude's CLI reports its own,
    Codex's needs both prices entered."""
    if getattr(brain, "provider", "claude") != "codex":
        return True
    return _has_prices(brain)


def token_cost(brain, input_tokens=0, output_tokens=0, cache_read_tokens=0,
               cache_creation_tokens=0) -> float:
    """A Codex call's cost at the prices entered (0 when there is none):
    every input token, cached or not, at the input price."""
    if not _has_prices(brain):
        return 0.0
    tokens_in = int(input_tokens or 0) + int(cache_read_tokens or 0) + \
        int(cache_creation_tokens or 0)
    return (tokens_in * float(brain.codex_usd_per_mtok_in)
            + int(output_tokens or 0) * float(brain.codex_usd_per_mtok_out)) / 1_000_000


def since(period: str) -> str:
    """The start of today ("day") or this month ("month"), UTC, as
    usage_events.created_at spells it."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%d 00:00:00" if period == "day" else "%Y-%m-01 00:00:00")


async def _value(state, name: str, args):
    async with state._connect() as db:
        async with db.execute(queries.sql(name), args) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def spent(state, period: str = "day", brain=None) -> float:
    """What Harvey's own API-key calls cost today or this month (UTC): the
    costs recorded, plus unpriced Codex calls' tokens at ``brain``'s prices
    (none entered: they can't be counted, and refusal holds every call
    under a cap)."""
    async with state._connect() as db:
        async with db.execute(queries.sql("overlay.brain.spend_since"),
                              (since(period),)) as cur:
            row = await cur.fetchone()
    cost, tokens_in, tokens_out = (row or (0, 0, 0))
    total = float(cost or 0.0)
    if (tokens_in or tokens_out) and brain is not None and _has_prices(brain):
        total += token_cost(brain, tokens_in, tokens_out)
    return total


def caps(brain) -> list[tuple[float, str]]:
    return [(float(cap), period) for cap, period in (
        (getattr(brain, "usd_per_day", None), "day"),
        (getattr(brain, "usd_per_month", None), "month")) if cap is not None]


NO_PRICE = ("no price entered for Codex's tokens (brain.codex_usd_per_mtok_in and "
            "codex_usd_per_mtok_out): with a USD cap set, Harvey can't count what a call "
            "costs, so none is made")


async def refusal(state, brain) -> str:
    """Why an API-key call can't be made now ("" when it can, and always in
    the subscription modes): a cap reached, or a cap that can't be counted."""
    if not api_key(brain):
        return ""
    held = caps(brain)
    if held and not priced(brain):
        return NO_PRICE
    for cap, period in held:
        used = await spent(state, period, brain)
        if used >= cap - EPSILON:
            return (f"the brain's USD cap for the {period} (${cap:.2f}, brain.usd_per_{period}) "
                    f"is reached: ${used:.2f} spent")
    return ""


async def totals(state, brain) -> dict:
    """Today's and this month's API-key spend beside the caps (the Usage
    tab, ``harvey usage``, Settings → Model)."""
    return {"today_usd": round(await spent(state, "day", brain), 4),
            "month_usd": round(await spent(state, "month", brain), 4),
            "usd_per_day": getattr(brain, "usd_per_day", None),
            "usd_per_month": getattr(brain, "usd_per_month", None),
            "priced": priced(brain),
            "codex_usd_per_mtok_in": getattr(brain, "codex_usd_per_mtok_in", None),
            "codex_usd_per_mtok_out": getattr(brain, "codex_usd_per_mtok_out", None)}


async def per_turn_usd(state, brain) -> float | None:
    """The most one agent turn may cost (see the module's doc); None when
    Codex has no price entered."""
    provider = getattr(brain, "provider", "claude")
    if provider == "codex":
        if not priced(brain):
            return None
        floor = token_cost(brain, TURN_INPUT_TOKENS, TURN_OUTPUT_TOKENS)
    else:
        floor = TURN_USD_FLOOR
    start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=OBSERVED_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    try:
        seen = float(await _value(state, "overlay.brain.agent_turn_cost", (start, provider)) or 0)
    except Exception as e:
        logger.debug("the agent's turn costs not read: %s", e)
        seen = 0.0
    return max(floor, seen)


async def unknown_usd(state, brain, turns: int) -> float:
    """What a call whose cost wasn't reported counts as: ``turns`` (at
    least one) at the most a turn may cost (0 for an unpriced Codex,
    which keeps no cost at all)."""
    per_turn = await per_turn_usd(state, brain)
    return 0.0 if per_turn is None else round(max(1, int(turns)) * per_turn, 6)


async def run_worst_usd(state, brain, turns: int) -> float | None:
    """A wake's or a task's worst case: its turn limit at the most a turn
    may cost (None: Codex unpriced)."""
    per_turn = await per_turn_usd(state, brain)
    return None if per_turn is None else round(int(turns) * per_turn, 6)


async def hold_run(ctx, plan_id: int, brain, spend, turns: int, what: str, retry: float) -> float:
    """Before an agent run (``what``: a wake, a task) starts in API-key
    mode: its worst case held of the install's day and month limits
    (``spend``: config.spend) and of plan ``plan_id``'s budget; 0 in the
    subscription modes (nothing held). Raises jobs.LaneExhausted (retried
    after ``retry`` seconds, no try used) when it can't be held, marked
    ``plan_only`` when it is the plan's budget that can't cover it (the
    other reasons hold every agent run alike)."""
    from harvey.agent import plans
    from harvey.modules import jobs

    if not api_key(brain):
        return 0.0
    worst = await run_worst_usd(ctx.state, brain, turns)
    if worst is None:
        raise jobs.LaneExhausted(
            f"the agent's {what}s wait: no price entered for Codex's tokens "
            "(brain.codex_usd_per_mtok_in and codex_usd_per_mtok_out), so what a "
            f"{what} may cost can't be held of its plan's budget", retry_after=retry)
    try:
        await ctx.reserve(worst, worst, per_day=getattr(spend, "per_day_usd", None),
                          per_month=getattr(spend, "per_month_usd", None))
    except jobs.SpendLimit as e:
        raise jobs.LaneExhausted(f"the agent's {what}s wait: {e} (a {what} may cost up to "
                                 f"${worst:.2f})", retry_after=retry) from None
    try:
        await plans.hold_run(ctx.state, plan_id, ctx.job.id, worst, spent=ctx.actual_cost)
    except plans.Refused as e:
        # (This plan's, not the lane's: only this run waits, other plans'
        # wakes and tasks go on.)
        err = jobs.LaneExhausted(f"the agent's {what} for plan {int(plan_id)} waits: {e}",
                                 retry_after=retry)
        err.plan_only = True
        raise err from None
    return worst


async def charge(ctx, brain, cost: float) -> None:
    """An agent run's cost into its job's actual_cost (API-key mode only:
    a subscription's calls are no money)."""
    if api_key(brain) and cost and cost > 0:
        await ctx.spend(cost)
