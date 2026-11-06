"""What the brain costs when it is billed per token (``brain.auth:
api_key``, or a Claude account linked from the Anthropic Console:
brain_auth.billed), and the caps that hold it.

In the subscription modes (terminal, a subscription account) nothing here
applies: the plan's own windows hold Harvey back (Brain.is_within_budget),
and no call is counted as money.

With an API key (a Console account alike):

- Every call of Harvey's own model (the brain's, the agent's chats, wakes
  and tasks) is a usage_events row with ``source`` SOURCE and its cost:
  the one Claude's CLI reports with the call (``costUSD`` /
  ``total_cost_usd``), or, for Codex, whose app-server reports tokens only,
  its tokens at the prices entered (``brain.codex_usd_per_mtok_in`` /
  ``_out``, USD per million; cached input is priced as input, so a cost is
  never lower than it was). A cost that is unknown is never read as $0:
  a call that ended with no cost (a CLI that timed out, died or was
  stopped before its result, or a result with tokens and no cost) is
  counted at a run's worst case for the turns it may have used
  (``unknown_usd``, source SOURCE_ESTIMATE), never below what its stream
  counted, and an unpriced Codex call (made before a cap was required)
  keeps its tokens (source SOURCE_UNPRICED), priced when the spend is read
  at the prices entered by then.
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
- API-key mode needs a cap: with neither ``usd_per_day`` nor
  ``usd_per_month`` set, no call is made (NO_CAP; Settings → Model and
  POST /api/brain refuse the mode without one, and a harvey.local.yaml
  written by hand still loads).

A run's worst case is its turn limit (chat.MAX_TURNS for a wake,
tasks.TASK_TURNS for a task) at a turn's bound (``turn_bound_usd``): for
Claude, priced at its model's row of the price table
(reference/model_prices.json: the alias brain.model names, each price at
its dearest for a blank or unknown one), the first turn writing the whole prompt to
the cache (TURN_INPUT_TOKENS: the chat's history within chat.PRIME_TOKENS
plus the prompt, the tools and their results) and every turn reading it
back, writing what is new (TURN_WRITE_TOKENS: a tool's result, the turn
before) and up to TURN_OUTPUT_TOKENS; for Codex, TURN_INPUT_TOKENS in and
TURN_OUTPUT_TOKENS out a turn at the prices entered. A turn never counts
less than the dearest per-turn cost the agent's own API-key runs had on
this provider in the last OBSERVED_DAYS.

The hold is a limit, not an estimate (``Meter``): while a wake, a task, a
chat answer or a brain call runs, its cost is counted from the stream as
it comes (Claude: each model call's usage at its model's row, the output
not yet reported counted from the characters streamed; Codex: the thread's
tokens at the prices entered), and when it reaches the run's hold (a wake,
a task) or what is left of the brain's day and month caps when it started
(any run, shared with the runs in flight in this process: _LIVE), the run
is interrupted the way Stop does it:
what it wrote is kept, it ends ``budget``, and what it cost is charged (the
CLI's own figure when its result gives one; with none, never less than
the stream counted).
A model call's output is only known as it streams, so a stop can come up
to one call's unseen output (its thinking) past the line.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import weakref
from functools import lru_cache

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
# A turn's bound (see the module's doc). It replaced a flat $0.10 a turn,
# which an Opus-class first turn writing its prompt to the cache already
# passes: the tokens are priced at the model's own row instead, and the
# run is stopped when it reaches its hold (Meter), so the bound is the
# most a run is allowed, not a guess of what it costs.
TURN_INPUT_TOKENS = 60_000
TURN_WRITE_TOKENS = 20_000
TURN_OUTPUT_TOKENS = 4_000
OBSERVED_DAYS = 30
EPSILON = 1e-9
# The price table (package data) and the characters a streamed output
# token is counted at before its call reports it: fewer than a model
# really writes, so the count runs ahead, never behind.
PRICES_FILE = "reference/model_prices.json"
CHARS_PER_TOKEN = 3.0


def api_key(brain) -> bool:
    """Whether ``brain``'s calls are billed per token, which is what every
    rule here applies to: an API key, or a Claude account linked from the
    Anthropic Console (brain_auth.billed)."""
    from harvey import brain_auth

    return brain_auth.billed(getattr(brain, "provider", "") or "claude",
                             getattr(brain, "auth", "") or "")


def _has_prices(brain) -> bool:
    return (getattr(brain, "codex_usd_per_mtok_in", None) is not None
            and getattr(brain, "codex_usd_per_mtok_out", None) is not None)


# ── Claude's prices (reference/model_prices.json) ──

@lru_cache(maxsize=1)
def price_table() -> dict:
    """The price table: {"aliases": {...}, "models": [rows], "checked",
    "source"}. Read once; a table that can't be read raises (a broken
    package is no reason to price a call at $0)."""
    from importlib import resources

    data = json.loads(resources.files("harvey").joinpath(PRICES_FILE).read_text(
        encoding="utf-8"))
    rows = [r for r in data.get("models") or [] if isinstance(r, dict) and r.get("model")]
    if not rows:
        raise ValueError(f"{PRICES_FILE} lists no model")
    return {**data, "models": rows}


PRICE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


def _dearest(rows: list[dict]) -> dict:
    """Each price at its highest in the table (no one row is dearest in
    every field: a cache read is dearer on an older model)."""
    return {"model": "", **{k: max(float(r[k]) for r in rows) for k in PRICE_FIELDS}}


def price_row(model: str = "") -> dict:
    """The price row of a Claude model: an alias (opus, sonnet, haiku) by
    the table's aliases, a full id by the longest row its name starts with
    (a dated or ``[1m]`` id too); a blank or unknown name, each price at its
    dearest in the table (marked ``unknown``)."""
    table = price_table()
    rows = table["models"]
    name = str(model or "").strip().lower()
    name = name.split("[", 1)[0].split("@", 1)[0]
    for prefix in ("us.anthropic.", "eu.anthropic.", "anthropic."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = str((table.get("aliases") or {}).get(name, name))
    best = None
    for row in rows:
        m = row["model"]
        if name == m or (name.startswith(m) and name[len(m)] in "-:"):
            if best is None or len(m) > len(best["model"]):
                best = row
    return best if best is not None else {**_dearest(rows), "unknown": True}


def claude_usd(usage: dict, model: str = "") -> float:
    """One model call's cost from its usage (a Claude message's ``usage``:
    input, cache read, cache write, broken down by the cache's lifetime
    when it says, else at the hour's price; output) at its model's row."""
    row = price_row(model)

    def n(key, src=usage):
        v = (src or {}).get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 \
            else 0.0
    written = n("cache_creation_input_tokens")
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    five, hour = n("ephemeral_5m_input_tokens", split), n("ephemeral_1h_input_tokens", split)
    hour += max(0.0, written - five - hour)
    return (n("input_tokens") * row["input"] + n("cache_read_input_tokens") * row["cache_read"]
            + five * row["cache_write_5m"] + hour * row["cache_write_1h"]
            + n("output_tokens") * row["output"]) / 1_000_000


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


NO_CAP = ("set a USD cap for the brain (brain.usd_per_day or usd_per_month, Settings → "
          "Model): billed per token, Harvey makes no call without one")


async def refusal(state, brain) -> str:
    """Why an API-key call can't be made now ("" when it can, and always in
    the subscription modes): no cap set, a cap reached, or a cap that can't
    be counted."""
    if not api_key(brain):
        return ""
    held = caps(brain)
    if not held:
        return NO_CAP
    if not priced(brain):
        return NO_PRICE
    for cap, period in held:
        used = await spent(state, period, brain)
        if used >= cap - EPSILON:
            return (f"the brain's USD cap for the {period} (${cap:.2f}, brain.usd_per_{period}) "
                    f"is reached: ${used:.2f} spent")
    return ""


async def room(state, brain) -> float | None:
    """What is left of the brain's day and month caps now (the smaller;
    None in the subscription modes, or with no cap): the most a chat
    answer or a brain call starting now may cost (Meter)."""
    if not api_key(brain):
        return None
    left = None
    for cap, period in caps(brain):
        rest = max(0.0, cap - await spent(state, period, brain))
        left = rest if left is None else min(left, rest)
    return left


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


def turn_bound_usd(brain) -> tuple[float, float] | None:
    """(the first turn's extra, every turn's) bound in USD (see the
    module's doc); None when Codex has no price entered."""
    if getattr(brain, "provider", "claude") == "codex":
        if not priced(brain):
            return None
        return 0.0, token_cost(brain, TURN_INPUT_TOKENS, TURN_OUTPUT_TOKENS)
    row = price_row(getattr(brain, "model", "") or "")
    first = TURN_INPUT_TOKENS * row["cache_write_1h"] / 1_000_000
    turn = (TURN_INPUT_TOKENS * row["cache_read"] + TURN_WRITE_TOKENS * row["cache_write_1h"]
            + TURN_OUTPUT_TOKENS * row["output"]) / 1_000_000
    return first, turn


async def per_turn_usd(state, brain) -> float | None:
    """The most one agent turn may cost (see the module's doc), the first
    turn's extra aside; None when Codex has no price entered."""
    provider = getattr(brain, "provider", "claude")
    bound = turn_bound_usd(brain)
    if bound is None:
        return None
    start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=OBSERVED_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    try:
        seen = float(await _value(state, "overlay.brain.agent_turn_cost", (start, provider)) or 0)
    except Exception as e:
        logger.debug("the agent's turn costs not read: %s", e)
        seen = 0.0
    return max(bound[1], seen)


async def unknown_usd(state, brain, turns: int) -> float:
    """What a call whose cost wasn't reported counts as: ``turns`` (at
    least one) at a run's worst case (0 for an unpriced Codex, which keeps
    no cost at all)."""
    worst = await run_worst_usd(state, brain, max(1, int(turns)))
    return 0.0 if worst is None else worst


async def run_worst_usd(state, brain, turns: int) -> float | None:
    """A wake's or a task's worst case: its turn limit at a turn's bound,
    with the first turn's write (None: Codex unpriced)."""
    per_turn = await per_turn_usd(state, brain)
    if per_turn is None:
        return None
    return round(turn_bound_usd(brain)[0] + int(turns) * per_turn, 6)


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


# ── A run's cost as it streams ──

def _codex_tokens(model_usage) -> dict:
    """The token counts of a result's modelUsage (Codex's: the thread's
    whole so far), summed over its models, in normalize_usage's shape."""
    out = {"input_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
           "output_tokens": 0}
    for mu in (model_usage or {}).values() if isinstance(model_usage, dict) else ():
        if not isinstance(mu, dict):
            continue
        for key, field in (("input_tokens", "inputTokens"), ("cache_read_tokens",
                                                             "cacheReadInputTokens"),
                           ("cache_creation_tokens", "cacheCreationInputTokens"),
                           ("output_tokens", "outputTokens")):
            v = mu.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[key] += int(v)
    return out


# Every metered run of this process (a weak set: one dropped without
# close() is forgotten), and the money all of them counted, ever: a run's
# ``room`` is checked against what every run in flight adds after it
# starts, so chats, wakes, tasks and brain calls running side by side
# share what was left of the caps instead of each spending all of it. Runs
# of another Harvey process (``harvey run`` beside the dashboard) are seen
# only once they are recorded.
_LIVE: "weakref.WeakSet[Meter]" = weakref.WeakSet()
_COUNTED = [0.0]


class Meter:
    """What a run has cost so far, from the events its CLI streams, and
    whether it reached ``limit`` (its own hold, USD) or ``room`` (what was
    left of the brain's day and month caps when it started, shared with
    the runs in flight: see _LIVE); None: not held by it. ``why`` says
    which was reached ("hold" or "cap"); ``close()`` once its cost is
    recorded.

    Claude: each model call (a message, by its id) at its model's row
    (claude_usd), each field the largest the call reported (message_start,
    message_delta, the assistant events), its output never less than the
    characters it streamed (CHARS_PER_TOKEN). Codex: ``{"type": "usage",
    "tokens": ...}`` events (the thread's tokens so far, normalize_usage's
    shape: the agent's driver writes them, the brain's client feeds them),
    less ``baseline`` (a live chat process's modelUsage at its last answer),
    at the prices entered."""

    def __init__(self, brain, limit: float | None = None, *, room: float | None = None,
                 baseline=None):
        self.brain = brain
        self.limit = None if limit is None else max(0.0, float(limit))
        # (What was left of the caps less what the runs in flight have
        # counted and not yet recorded; then every run's growth from here.)
        self.room = None if room is None else max(
            0.0, float(room) - sum(m.cost for m in list(_LIVE)))
        self._start = _COUNTED[0]
        self._added = 0.0
        self.why = ""
        self.codex = getattr(brain, "provider", "claude") == "codex"
        self._calls: dict[str, dict] = {}
        self._current = ""
        self._seq = 0
        self._base = _codex_tokens(baseline)
        self._codex = 0.0
        # (Set by whoever ended the run at the limit.)
        self.stopped = False
        _LIVE.add(self)

    def close(self) -> None:
        """The run's cost is recorded (or it never ran): no longer in flight."""
        _LIVE.discard(self)

    @property
    def cost(self) -> float:
        if self.codex:
            return self._codex
        total = 0.0
        for c in self._calls.values():
            usage = dict(c["usage"])
            streamed = math.ceil(max(c["chars"], c["content_chars"]) / CHARS_PER_TOKEN)
            usage["output_tokens"] = max(float(usage.get("output_tokens") or 0), streamed)
            total += claude_usd(usage, c["model"] or getattr(self.brain, "model", "") or "")
        return total

    @property
    def reached(self) -> bool:
        # (A run that costs nothing, Codex's prices entered as 0, never
        # reaches a limit of 0.)
        cost = self.cost
        if cost > self._added:
            _COUNTED[0] += cost - self._added
            self._added = cost
        if self.limit is not None and cost > 0 and cost >= self.limit - EPSILON:
            self.why = self.why or "hold"
            return True
        grown = _COUNTED[0] - self._start
        if self.room is not None and grown > 0 and grown >= self.room - EPSILON:
            self.why = self.why or "cap"
            return True
        return False

    @property
    def line(self) -> float:
        """Where it stops (USD): its hold, else the room it started with."""
        if self.why == "cap" or self.limit is None:
            return self.room or 0.0
        return self.limit

    def _call(self, mid: str = "") -> dict:
        mid = mid or self._current
        if not mid:
            self._seq += 1
            mid = self._current = f"_{self._seq}"
        call = self._calls.get(mid)
        if call is None:
            call = self._calls[mid] = {"model": "", "usage": {}, "chars": 0,
                                       "content_chars": 0}
        return call

    @staticmethod
    def _merge(call: dict, usage, model) -> None:
        if model:
            call["model"] = str(model)
        if not isinstance(usage, dict):
            return
        have = call["usage"]
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            v = usage.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                have[key] = max(float(have.get(key) or 0), float(v))
        split = usage.get("cache_creation")
        if isinstance(split, dict):
            kept = have.setdefault("cache_creation", {})
            for key, v in split.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    kept[key] = max(float(kept.get(key) or 0), float(v))

    def feed(self, event) -> bool:
        """Count one streamed event; whether the limit is reached."""
        if not isinstance(event, dict):
            return self.reached
        kind = event.get("type")
        if kind == "usage":
            tokens = event.get("tokens") if isinstance(event.get("tokens"), dict) else {}
            now = {k: max(0, int(tokens.get(k) or 0) - self._base[k]) for k in self._base}
            self._codex = max(self._codex, token_cost(
                self.brain, now["input_tokens"], now["output_tokens"],
                now["cache_read_tokens"], now["cache_creation_tokens"]))
        elif kind == "stream_event" and not self.codex:
            e = event.get("event") if isinstance(event.get("event"), dict) else {}
            if e.get("type") == "message_start":
                message = e.get("message") if isinstance(e.get("message"), dict) else {}
                mid = str(message.get("id") or "")
                if not mid:
                    self._seq += 1
                    mid = f"_{self._seq}"
                self._current = mid
                self._merge(self._call(mid), message.get("usage"), message.get("model"))
            elif e.get("type") == "message_delta":
                self._merge(self._call(), e.get("usage"), None)
            elif e.get("type") == "content_block_delta":
                delta = e.get("delta") if isinstance(e.get("delta"), dict) else {}
                for key in ("text", "partial_json", "thinking"):
                    if isinstance(delta.get(key), str):
                        self._call()["chars"] += len(delta[key])
        elif kind == "assistant" and not self.codex:
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            call = self._call(str(message.get("id") or ""))
            self._merge(call, message.get("usage"), message.get("model"))
            chars = 0
            for block in message.get("content") or []:
                if isinstance(block, dict):
                    for key in ("text", "thinking"):
                        if isinstance(block.get(key), str):
                            chars += len(block[key])
                    if block.get("type") == "tool_use":
                        chars += len(json.dumps(block.get("input") or {}, ensure_ascii=False))
            call["content_chars"] = max(call["content_chars"], chars)
        return self.reached
