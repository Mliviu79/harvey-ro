"""Jobs: the work Harvey does in the background, one row each.

Everything that takes longer than a click (a search, a site read, a
registry load, a check at ANAF) is a job on a lane. A lane is one outside
source (a module's id, or the website reader's): its jobs run one at a
time, in the order they were asked for, and the lanes run side by side,
so a slow TinyFish search never holds up an OpenStreetMap one. The pace a
service allows is still kept by harvey/modules/lanes.py, request by
request.

A job is queued, running, then done, failed or cancelled. A paused one
waits for a person: its lane ran out of credit, or the spend limit of the
run it belongs to was reached (raise it, then resume). One click that
becomes several jobs (one search per city) gives them one batch id, and a
paid batch's cap is what its jobs reserve from before they spend.

The same work can't be queued twice while it is still to finish (the
dedupe key); asking again names the job already there.

The worker (harvey/modules/worker.py) claims and runs jobs; this file is
the table and the rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harvey.transient import Transient, is_transient  # noqa: F401 (part of this API)

logger = logging.getLogger("harvey.jobs")

QUEUED, RUNNING, DONE, FAILED, CANCELLED, PAUSED = (
    "queued", "running", "done", "failed", "cancelled", "paused")
STATUSES = (QUEUED, RUNNING, DONE, FAILED, CANCELLED, PAUSED)
# Still to finish: a second copy of the same work is refused.
ACTIVE = (QUEUED, RUNNING, PAUSED)
FINISHED = (DONE, FAILED, CANCELLED)

# Why a job paused, as stored (the dashboard says it in its own words).
SPEND_LIMIT = "spend limit"
PAUSED_BY_HAND = "paused"

# What a running job is asked to do between two pages. LOST is no
# person's ask: the job isn't this worker's any more (another worker took
# its lane over while this one was stuck), so it stops and writes nothing.
STOP_CANCEL, STOP_PAUSE, LOST = "cancel", "pause", "lost"

# A job that hits a passing problem (the network, a 5xx, a 429) is tried
# this many times in all, waiting longer each time, then fails.
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 900

# Lines of a job's own log kept in its progress (the CLI prints them).
LOG_LINES = 200

LANE_PAUSED_KEY = "lanes.{}.paused"
# The lease row of whoever runs a lane right now (harvey/modules/worker.py).
LANE_LEASE = "lane:{}"


def backoff_seconds(attempts: int) -> int:
    """Wait before try number ``attempts + 1``: 30 s, 60 s, 120 s ... 15 min."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_CAP_SECONDS)


# ── What a job can end with besides a result ──

class JobError(Exception):
    """The job can't be done as asked; trying again won't help. ``code``
    is the exit status a command waiting for it ends with."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class LaneExhausted(Exception):
    """The source can take no more for now (out of credit, a refused key,
    a used-up allowance). With ``retry_after`` (seconds) the job waits that
    long and tries again; without, the whole lane pauses until a person
    resumes it."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SpendLimit(Exception):
    """The run's spend cap can't pay for what this job would spend next."""


class Duplicate(ValueError):
    """The same work is already queued or running: ``job`` is that one."""

    def __init__(self, job: "Job"):
        self.job = job
        super().__init__(
            f"The same {job.kind.replace('_', ' ')} is already {job.status} "
            f"(job {job.id}"
            + (f", {job.label}" if job.label else "") + "). "
            "See it on the Queue page, or: harvey jobs list")


# ── A job as read back ──

@dataclass
class Job:
    id: str
    kind: str
    lane: str
    status: str = QUEUED
    module: str = ""
    capability: str = ""
    label: str = ""
    params: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    dedupe_key: str | None = None
    est_cost: float = 0.0
    reserved: float = 0.0
    actual_cost: float = 0.0
    attempts: int = 0
    not_before: str | None = None
    error: str = ""
    paused_reason: str = ""
    stop_requested: str = ""
    batch_id: str = ""
    parent_id: str = ""
    worker: str = ""
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "Job":
        data = dict(row)

        def load(key):
            try:
                value = json.loads(data.pop(key, None) or "{}")
            except (TypeError, ValueError):
                value = {}
            return value if isinstance(value, dict) else {}

        params, progress, result = load("params_json"), load("progress_json"), load("result_json")
        known = {f for f in cls.__dataclass_fields__}
        return cls(params=params, progress=progress, result=result,
                   **{k: v for k, v in data.items() if k in known})

    def as_dict(self) -> dict:
        """The job as the dashboard shows it (its log left out)."""
        progress = {k: v for k, v in self.progress.items() if k != "log"}
        log = self.progress.get("log") or []
        return {
            "id": self.id, "kind": self.kind, "lane": self.lane, "status": self.status,
            "module": self.module, "label": self.label, "params": self.params,
            "progress": progress, "last_line": log[-1][1] if log else "",
            "result": self.result, "est_cost": round(self.est_cost or 0.0, 4),
            "actual_cost": round(self.actual_cost or 0.0, 4), "attempts": self.attempts,
            "not_before": self.not_before, "error": self.error,
            "paused_reason": self.paused_reason, "batch_id": self.batch_id,
            "parent_id": self.parent_id, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "stoppable": stoppable(self.kind),
        }


# ── Kinds: what a job of each kind runs ──

@dataclass(frozen=True)
class Kind:
    name: str
    run: Callable[[Any], Awaitable[dict | None]]
    # Whether a running job of this kind can be cancelled or paused part
    # way (it reads JobContext.stop_reason between pages). One that can't
    # (a load, a check of a hundred firms in one request) runs to its end.
    stoppable: bool = False


KINDS: dict[str, Kind] = {}


def kind(name: str, *, stoppable: bool = False):
    """Register the code that runs jobs of kind ``name``. It takes a
    JobContext (harvey/modules/worker.py) and returns the job's result."""
    def register(fn):
        KINDS[name] = Kind(name, fn, stoppable)
        return fn
    return register


def stoppable(kind_name: str) -> bool:
    found = handler(kind_name)
    return bool(found and found.stoppable)


def handler(name: str) -> Kind | None:
    if name not in KINDS:
        # The kinds live beside the runs they call; importing them registers them.
        import harvey.modules.job_kinds  # noqa: F401
    return KINDS.get(name)


# ── Waking a worker in this process when something is queued ──

_listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
_listeners_lock = threading.Lock()


def listen(event: asyncio.Event) -> None:
    with _listeners_lock:
        _listeners.append((asyncio.get_running_loop(), event))


def unlisten(event: asyncio.Event) -> None:
    with _listeners_lock:
        _listeners[:] = [(loop, ev) for loop, ev in _listeners if ev is not event]


def notify() -> None:
    """Tell any worker in this process that there is new work."""
    with _listeners_lock:
        targets = list(_listeners)
    for loop, event in targets:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass        # its loop is closed: it is going away


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


async def _fetch_job(db, job_id: str) -> Job | None:
    import aiosqlite

    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
    return Job.from_row(row) if row else None


# ── Batches ──

async def new_batch(state, kind_: str, label: str = "", params: dict | None = None,
                    max_spend: float | None = None) -> str:
    batch_id = _new_id()
    async with state._connect() as db:
        await db.execute(
            "INSERT INTO job_batches (id, kind, label, params_json, max_spend) VALUES (?, ?, ?, ?, ?)",
            (batch_id, kind_, label, _json(params), max_spend))
        await db.commit()
    return batch_id


async def drop_batch(state, batch_id: str) -> None:
    """Forget a batch none of whose jobs was queued (all were duplicates)."""
    async with state._connect() as db:
        await db.execute("DELETE FROM job_batches WHERE id = ? AND NOT EXISTS "
                         "(SELECT 1 FROM jobs WHERE batch_id = ?)", (batch_id, batch_id))
        await db.commit()


async def get_batch(state, batch_id: str) -> dict | None:
    import aiosqlite

    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM job_batches WHERE id = ?", (batch_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["params"] = json.loads(out.pop("params_json") or "{}")
    except (TypeError, ValueError):
        out["params"] = {}
    return out


async def set_batch_cap(state, batch_id: str, max_spend: float, pool: str = "") -> None:
    """A run's new spend limit. A run with amounts of its own inside its
    cap (``pools``) gives the change to ``pool`` (its first pool, a
    Discover run's searches, when that names none of them): the others keep
    their amount, so a search the old limit paused can carry on."""
    batch = await get_batch(state, batch_id)
    params = (batch or {}).get("params") or {}
    pools = params.get("pools") or {}
    async with state._connect() as db:
        if pools:
            target = pool if pool in pools else next(iter(pools))
            others = sum(float(v) for k, v in pools.items() if k != target)
            pools[target] = round(max(0.0, float(max_spend) - others), 6)
            params["pools"] = pools
            await db.execute("UPDATE job_batches SET params_json = ? WHERE id = ?",
                             (json.dumps(params), batch_id))
        await db.execute("UPDATE job_batches SET max_spend = ? WHERE id = ?",
                         (float(max_spend), batch_id))
        await db.commit()


async def batch_spent(state, batch_id: str, *, exclude: str = "") -> float:
    """What a batch's jobs other than ``exclude`` spent, plus what its
    other running jobs hold reserved."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT COALESCE(SUM(actual_cost), 0) + COALESCE(SUM(CASE WHEN status = 'running' "
                "THEN MAX(reserved - actual_cost, 0) ELSE 0 END), 0) "
                "FROM jobs WHERE batch_id = ? AND id != ?", (batch_id, exclude)) as cur:
            (spent,) = await cur.fetchone()
    return float(spent or 0.0)


async def pool_spent(state, batch_id: str, pool: str, *, exclude: str = "") -> float:
    """batch_spent, of the batch's jobs in one pool (``params.pool``) only."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT COALESCE(SUM(actual_cost), 0) + COALESCE(SUM(CASE WHEN status = 'running' "
                "THEN MAX(reserved - actual_cost, 0) ELSE 0 END), 0) "
                "FROM jobs WHERE batch_id = ? AND id != ? "
                "AND json_extract(params_json, '$.pool') = ?", (batch_id, exclude, pool)) as cur:
            (spent,) = await cur.fetchone()
    return float(spent or 0.0)


async def spent_since(state, since: str, *, exclude: str = "") -> float:
    """What every job started since ``since`` (a SQLite datetime modifier
    such as 'start of day') spent, plus what running ones hold reserved."""
    async with state._connect() as db:
        async with db.execute(
                "SELECT COALESCE(SUM(actual_cost), 0) + COALESCE(SUM(CASE WHEN status = 'running' "
                "THEN MAX(reserved - actual_cost, 0) ELSE 0 END), 0) FROM jobs "
                "WHERE started_at >= datetime('now', ?) AND id != ?", (since, exclude)) as cur:
            (spent,) = await cur.fetchone()
    return float(spent or 0.0)


async def reserve(state, job: Job, estimate: float, first: float, *,
                  per_day: float | None = None, per_month: float | None = None) -> float:
    """Reserve what ``job`` may spend before it spends anything: its
    ``estimate``, or what is left when that is less, of its batch's cap
    and of the install's limits for the day and the month (UTC;
    ``per_day`` / ``per_month``, none when None). Returns the money it may
    spend; SpendLimit when what is left can't pay for even its ``first``
    request. A job with no cap anywhere may spend without limit."""
    own = float(job.actual_cost or 0.0)
    left = float("inf")
    batch = await get_batch(state, job.batch_id) if job.batch_id else None
    cap = batch.get("max_spend") if batch else None
    limits = []
    if cap is not None:
        limits.append((float(cap) - await batch_spent(state, job.batch_id, exclude=job.id) - own,
                       f"the run's spend limit (${float(cap):.2f}) is reached"))
        # A run with amounts of its own inside its cap (``pools``: a
        # Discover run's searches and its website lookups): a job draws
        # from its own pool only.
        pools = (batch.get("params") or {}).get("pools") or {}
        pool = (job.params or {}).get("pool")
        if pool in pools:
            amount = float(pools[pool])
            limits.append((amount - await pool_spent(state, job.batch_id, pool, exclude=job.id)
                           - own, f"the run's amount for {pool} (${amount:.2f}) is reached"))
    for limit, since, what in ((per_day, "start of day", "day"),
                               (per_month, "start of month", "month")):
        if limit is not None:
            # This job's own spend counts on the day (month) it started.
            mine = own if job.started_at and await _started_since(state, job.id, since) else 0.0
            limits.append((float(limit) - await spent_since(state, since, exclude=job.id) - mine,
                           f"the spend limit for the {what} (${float(limit):.2f}) is reached"))
    for room, why in limits:
        room = max(0.0, room)
        if first > room + 1e-9:
            raise SpendLimit(why)
        left = min(left, room)
    if left == float("inf"):
        return left
    hold = min(max(estimate, first), left)
    async with state._connect() as db:
        await db.execute("UPDATE jobs SET reserved = ? WHERE id = ?", (own + hold, job.id))
        await db.commit()
    return left


async def _started_since(state, job_id: str, since: str) -> bool:
    async with state._connect() as db:
        async with db.execute("SELECT started_at >= datetime('now', ?) FROM jobs WHERE id = ?",
                              (since, job_id)) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


# ── Queueing ──

async def enqueue(state, kind_: str, lane: str, params: dict | None = None, *,
                  dedupe_key: str | None = None, module: str = "", capability: str = "",
                  label: str = "", est_cost: float = 0.0, batch_id: str = "",
                  parent_id: str = "") -> str:
    """Queue one job; returns its id. Duplicate when the same work
    (``dedupe_key``) is already queued, running or paused."""
    ids, dupes = await enqueue_many(state, [dict(
        kind=kind_, lane=lane, params=params, dedupe_key=dedupe_key, module=module,
        capability=capability, label=label, est_cost=est_cost)],
        batch_id=batch_id, parent_id=parent_id)
    if dupes:
        raise Duplicate(dupes[0])
    return ids[0]


async def enqueue_many(state, specs: list[dict], *, batch_id: str = "",
                       parent_id: str = "") -> tuple[list[str], list[Job]]:
    """Queue several jobs in one transaction. Returns (the new ids, the
    jobs already there for specs that were duplicates: those are skipped)."""
    import sqlite3

    ids: list[str] = []
    dupes: list[Job] = []
    if not specs:
        return ids, dupes
    async with state._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for spec in specs:
                job_id = _new_id()
                key = spec.get("dedupe_key")
                try:
                    await db.execute(
                        "INSERT INTO jobs (id, kind, lane, module, capability, label, params_json, "
                        "dedupe_key, est_cost, batch_id, parent_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (job_id, spec["kind"], spec["lane"], spec.get("module") or "",
                         spec.get("capability") or "", spec.get("label") or "",
                         _json(spec.get("params")), key, float(spec.get("est_cost") or 0.0),
                         spec.get("batch_id", batch_id) or "", parent_id))
                except sqlite3.IntegrityError:
                    async with db.execute(
                            "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN "
                            "('queued', 'running', 'paused')", (key,)) as cur:
                        row = await cur.fetchone()
                    there = await _fetch_job(db, row[0]) if row else None
                    if there is not None:
                        dupes.append(there)
                    continue
                ids.append(job_id)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    if ids:
        notify()
    return ids, dupes


# ── The worker's side ──

async def claim(state, lane: str, worker: str, batch_ids=None) -> Job | None:
    """Take the next job due on ``lane`` (oldest first) and mark it
    running, in one statement: two workers can never take the same job.
    ``batch_ids`` limits it to those batches."""
    import aiosqlite

    where = ["lane = ?", "status = 'queued'",
             "(not_before IS NULL OR not_before <= datetime('now'))"]
    args: list = [lane]
    if batch_ids is not None:
        batch_ids = list(batch_ids)
        if not batch_ids:
            return None
        where.append(f"batch_id IN ({', '.join('?' * len(batch_ids))})")
        args += batch_ids
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "UPDATE jobs SET status = 'running', worker = ?, "
                "started_at = COALESCE(started_at, CURRENT_TIMESTAMP), stop_requested = '', "
                "paused_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT id FROM jobs WHERE " + " AND ".join(where) +
                " ORDER BY rowid LIMIT 1) AND status = 'queued' RETURNING *",
                (worker, *args)) as cur:
            row = await cur.fetchone()
        await db.commit()
    return Job.from_row(row) if row else None


async def ready_lanes(state, batch_ids=None) -> list[str]:
    """Lanes with a job due now, in the order their oldest job was queued."""
    where, args = "", []
    if batch_ids is not None:
        batch_ids = list(batch_ids)
        if not batch_ids:
            return []
        where = f" AND batch_id IN ({', '.join('?' * len(batch_ids))})"
        args = batch_ids
    async with state._connect() as db:
        async with db.execute(
                "SELECT lane, MIN(rowid) FROM jobs WHERE status = 'queued' "
                "AND (not_before IS NULL OR not_before <= datetime('now'))" + where +
                " GROUP BY lane ORDER BY 2", args) as cur:
            return [r[0] for r in await cur.fetchall()]


def _owned(worker: str | None) -> tuple[str, tuple]:
    """The fence on a running job's writes: only the worker running it
    may write it (a worker that lost its lane writes nothing)."""
    if worker is None:
        return "", ()
    return " AND status = 'running' AND worker = ?", (worker,)


async def save_progress(state, job_id: str, progress: dict, *,
                        actual_cost: float | None = None, worker: str | None = None) -> bool:
    """Store a running job's progress. With ``worker``, only while that
    worker still runs it; False when it doesn't."""
    sets, args = ["progress_json = ?", "updated_at = CURRENT_TIMESTAMP"], [_json(progress)]
    if actual_cost is not None:
        sets.append("actual_cost = ?")
        args.append(float(actual_cost))
    fence, fence_args = _owned(worker)
    async with state._connect() as db:
        cur = await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?{fence}",
                               (*args, job_id, *fence_args))
        await db.commit()
        return cur.rowcount > 0


async def stop_requested(state, job_id: str, worker: str | None = None) -> str:
    """What a person asked of a running job ("", cancel, pause); LOST
    when ``worker`` no longer runs it."""
    async with state._connect() as db:
        async with db.execute("SELECT stop_requested, status, worker FROM jobs WHERE id = ?",
                              (job_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return STOP_CANCEL
    if worker is not None and (row[1] != RUNNING or row[2] != worker):
        return LOST
    return row[0] or ""


async def finish(state, job_id: str, status: str, *, result: dict | None = None,
                 error: str = "", paused_reason: str = "", retry_in: float | None = None,
                 progress: dict | None = None, actual_cost: float | None = None,
                 attempts: int | None = None, worker: str | None = None) -> bool:
    """Record how a running job ended: done, failed, cancelled, paused, or
    queued again (``retry_in`` seconds from now, or at once). ``attempts``
    is how many of its tries failed so far. With ``worker``, only while
    that worker still runs it; False when it doesn't (nothing written)."""
    sets = ["status = ?", "error = ?", "paused_reason = ?", "stop_requested = ''",
            "updated_at = CURRENT_TIMESTAMP", "worker = ''"]
    args: list = [status, (error or "")[:1000], paused_reason]
    if status in FINISHED:
        sets.append("finished_at = CURRENT_TIMESTAMP")
    sets.append("not_before = " + ("datetime('now', ?)" if retry_in else "NULL"))
    if retry_in:
        args.append(f"+{int(retry_in)} seconds")
    if result is not None:
        sets.append("result_json = ?")
        args.append(_json(result))
    if progress is not None:
        sets.append("progress_json = ?")
        args.append(_json(progress))
    if actual_cost is not None:
        sets.append("actual_cost = ?")
        args.append(float(actual_cost))
    if attempts is not None:
        sets.append("attempts = ?")
        args.append(int(attempts))
    fence, fence_args = _owned(worker)
    async with state._connect() as db:
        cur = await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?{fence}",
                               (*args, job_id, *fence_args))
        await db.commit()
        written = cur.rowcount > 0
    if status == QUEUED and written:
        notify()
    return written


async def sweep_stale(state, lane: str | None = None, *, fresh_after: float = 0.0) -> int:
    """Running jobs whose worker is gone (it was killed, the machine
    restarted, it lost its lane) go back to the queue; their progress
    stays, so a search resumes from its page. With ``lane``: every running
    job of that lane (called by the worker that has just taken the lane's
    lease, so none of them is anybody's). Without: every running job whose
    lane nobody holds a lease on that was renewed after ``fresh_after``
    (a time.time())."""
    if lane is not None:
        where, args = "lane = ?", (lane,)
    else:
        where = ("NOT EXISTS (SELECT 1 FROM worker_lease WHERE worker_lease.name = "
                 "'lane:' || jobs.lane AND worker_lease.heartbeat_at >= ?)")
        args = (fresh_after,)
    async with state._connect() as db:
        cur = await db.execute(
            "UPDATE jobs SET status = 'queued', worker = '', stop_requested = '', "
            f"updated_at = CURRENT_TIMESTAMP WHERE status = 'running' AND {where}", args)
        await db.commit()
        swept = cur.rowcount
    if swept:
        logger.info("jobs: %d interrupted job(s) back in the queue", swept)
    return swept


# ── A person's side ──

async def get(state, job_id: str) -> Job | None:
    async with state._connect() as db:
        return await _fetch_job(db, job_id)


async def find(state, ref: str) -> Job | None:
    """A job by its id, or by the start of it (as the CLI prints them)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    job = await get(state, ref)
    if job is not None:
        return job
    async with state._connect() as db:
        async with db.execute("SELECT id FROM jobs WHERE id LIKE ? LIMIT 2",
                              (ref.replace("%", "") + "%",)) as cur:
            rows = await cur.fetchall()
    return await get(state, rows[0][0]) if len(rows) == 1 else None


class Refused(ValueError):
    """A person asked for something a job's state doesn't allow. ``code``
    names the case for the dashboard to say it in its own words."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.code = code


async def _set_status(state, job: Job, to: str, *, allowed: tuple[str, ...],
                      extra: str = "", args: tuple = ()) -> Job:
    """Move ``job`` (as just read) to ``to``; _Moved when its status
    changed since it was read."""
    if job.status not in allowed:
        raise Refused(f"Job {job.id} is {job.status}; it can't be {to} now.")
    async with state._connect() as db:
        cur = await db.execute(
            f"UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP{extra} "
            f"WHERE id = ? AND status = ?", (to, *args, job.id, job.status))
        await db.commit()
        if cur.rowcount == 0:
            # It changed meanwhile (a worker claimed it): the caller looks again.
            raise _Moved()
    return await get(state, job.id)


class _Moved(Exception):
    """A job's status changed between reading it and updating it."""


async def _ask_running(state, job: Job, ask: str, verb: str) -> Job | None:
    """Ask a running job to stop at its next page; None when it isn't
    running any more (the caller looks again)."""
    if not stoppable(job.kind):
        raise Refused(f"Job {job.id} is running and can't be {verb} part-way; "
                      "it ends on its own.", code="not_stoppable")
    async with state._connect() as db:
        cur = await db.execute(
            "UPDATE jobs SET stop_requested = ? WHERE id = ? AND status = 'running'",
            (ask, job.id))
        await db.commit()
        if cur.rowcount == 0:
            return None
    return await get(state, job.id)


async def _act(state, job_id: str, act) -> Job:
    """Run ``act(job)`` on the job as it is now, looking again (a few
    times) when a worker changed it in between."""
    for _ in range(5):
        job = await get(state, job_id)
        if job is None:
            raise Refused(f"There is no job {job_id}.")
        try:
            done = await act(job)
        except _Moved:
            continue
        if done is not None:
            return done
    raise Refused(f"Job {job_id} keeps changing; try again.")


async def cancel(state, job_id: str) -> Job:
    """Cancel a job. One waiting is cancelled at once; a running search
    stops after the page it is on (what it found so far is kept). A
    running job that can't stop part-way is refused."""
    async def act(job):
        if job.status == RUNNING:
            return await _ask_running(state, job, STOP_CANCEL, "cancelled")
        return await _set_status(state, job, CANCELLED, allowed=(QUEUED, PAUSED),
                                 extra=", finished_at = CURRENT_TIMESTAMP")
    return await _act(state, job_id, act)


async def pause(state, job_id: str) -> Job:
    """Hold a job: a waiting one stays put, a running search stops after its page."""
    async def act(job):
        if job.status == RUNNING:
            return await _ask_running(state, job, STOP_PAUSE, "paused")
        return await _set_status(state, job, PAUSED, allowed=(QUEUED,),
                                 extra=", paused_reason = ?", args=(PAUSED_BY_HAND,))
    return await _act(state, job_id, act)


async def resume(state, job_id: str) -> Job:
    """Put a paused job back in the queue; it carries on from where it stopped."""
    job = await _act(state, job_id, lambda j: _set_status(
        state, j, QUEUED, allowed=(PAUSED,),
        extra=", paused_reason = '', error = '', not_before = NULL"))
    notify()
    return job


async def retry(state, job_id: str) -> Job:
    """Queue a failed or cancelled job again, from where it got to.
    Refused when the same work is already waiting."""
    import sqlite3

    try:
        job = await _act(state, job_id, lambda j: _set_status(
            state, j, QUEUED, allowed=(FAILED, CANCELLED),
            extra=", attempts = 0, error = '', not_before = NULL, finished_at = NULL"))
    except sqlite3.IntegrityError:
        job = await get(state, job_id)
        async with state._connect() as db:
            async with db.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN "
                    "('queued', 'running', 'paused')", (job.dedupe_key,)) as cur:
                row = await cur.fetchone()
        there = await get(state, row[0]) if row else None
        if there is not None:
            raise Duplicate(there)
        raise
    notify()
    return job


async def pause_lane(state, lane: str, reason: str = PAUSED_BY_HAND) -> None:
    """Hold every job of a lane: none starts, and a running one stops
    after its page and goes back to the queue."""
    await state.set_setting(LANE_PAUSED_KEY.format(lane), reason or PAUSED_BY_HAND)


async def resume_lane(state, lane: str) -> None:
    await state.delete_setting(LANE_PAUSED_KEY.format(lane))
    notify()


async def known_lane(state, lane: str) -> bool:
    """A lane a job can run on: a module's, the contact search's, or one
    that holds jobs or is paused already. Anything else is a typo, and
    pausing it would only make a lane that doesn't exist."""
    from harvey.modules import switches
    from harvey.modules.job_kinds import CONTACTS_LANE

    if lane in switches.by_id() or lane == CONTACTS_LANE:
        return True
    if lane in await paused_lanes(state):
        return True
    async with state._connect() as db:
        async with db.execute("SELECT 1 FROM jobs WHERE lane = ? LIMIT 1", (lane,)) as cur:
            return await cur.fetchone() is not None


async def lane_paused(state, lane: str) -> str:
    """Why ``lane`` is paused, or ""."""
    return await state.get_setting(LANE_PAUSED_KEY.format(lane))


async def paused_lanes(state) -> dict[str, str]:
    prefix = "lanes."
    out = {}
    for key, value in (await state.settings_with_prefix(prefix)).items():
        if key.endswith(".paused") and value:
            out[key[len(prefix):-len(".paused")]] = value
    return out


async def list_jobs(state, *, lane: str | None = None, statuses=None,
                    batch_id: str | None = None, limit: int = 200,
                    oldest_first: bool = False) -> list[Job]:
    """Jobs, newest first (``oldest_first``: in the order they run)."""
    import aiosqlite

    where, args = [], []
    if lane:
        where.append("lane = ?")
        args.append(lane)
    if statuses:
        statuses = list(statuses)
        where.append(f"status IN ({', '.join('?' * len(statuses))})")
        args += statuses
    if batch_id:
        where.append("batch_id = ?")
        args.append(batch_id)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY rowid {'ASC' if oldest_first else 'DESC'} LIMIT ?"
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (*args, int(limit))) as cur:
            return [Job.from_row(r) for r in await cur.fetchall()]


async def counts(state) -> dict:
    """{status: n} over every job, and per lane: {"lanes": {lane: {status: n}}}."""
    out: dict = {s: 0 for s in STATUSES}
    lanes: dict[str, dict[str, int]] = {}
    async with state._connect() as db:
        async with db.execute("SELECT lane, status, COUNT(*) FROM jobs GROUP BY lane, status") as cur:
            for lane, status, n in await cur.fetchall():
                out[status] = out.get(status, 0) + n
                lanes.setdefault(lane, {})[status] = n
    out["lanes"] = lanes
    return out
