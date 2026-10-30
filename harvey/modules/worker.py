"""The worker: runs the jobs in harvey/modules/jobs.py.

One full worker per database. The dashboard starts one inside its own
process (the usual install runs only the dashboard); ``harvey worker`` and
``harvey run`` start one for an install without it. Which of them works is
decided by a lease row with a heartbeat: whoever holds a fresh lease runs
jobs, the others wait. A CLI command that waits for its jobs follows that
worker, or, when there is none, runs just its own jobs.

Inside, one loop per lane: each lane runs its jobs in order, and lanes run
side by side. Each lane has a lease of its own, taken by whoever runs it
now: a command running its own registry pull holds the registry's lane
only, and a dashboard started meanwhile runs every other lane. The leases
are renewed from a thread, so a job that holds up the event loop never
lets a second worker take over work that is still running; and a job's
writes are fenced to the worker running it. Jobs left "running" on a lane
nobody holds go back to the queue, and a search among them resumes from
the page it reached.

A job ends done, failed (with the message), cancelled, or paused. A
passing problem (the network, a 5xx, a 429) is tried again later, waiting
longer each time; a source out of credit pauses its whole lane.

Quiet hours are the Email module's: the worker runs at any hour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from harvey.modules import jobs

logger = logging.getLogger("harvey.worker")

LEASE_NAME = "worker"
# A lease whose heartbeat is older than this belongs to a worker that is gone.
LEASE_TTL_SECONDS = 30.0
# How often the worker looks for new work when nobody tells it.
POLL_SECONDS = 2.0
# How often a source set to update on its own is checked for being due.
DUE_SOURCES_EVERY_SECONDS = 900
# How long a stopping worker lets running jobs reach the end of their page:
# well inside the 10 s "docker stop" gives before it kills the process, so
# a restart usually takes the clean path (the page kept, the cursor saved).
SHUTDOWN_GRACE_SECONDS = 6.0
# Writing how a job ended is tried this many times when the database is
# busy, so a job is never left marked running.
FINISH_TRIES = 5


# ── The lease ──

async def acquire_lease(state, owner: str, ttl: float = LEASE_TTL_SECONDS,
                        name: str = LEASE_NAME) -> bool:
    """Take the lease ``name`` (the worker's, or a lane's), or renew it
    when ``owner`` already holds it. False while another worker's
    heartbeat is fresh."""
    now = time.time()
    async with state._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "INSERT INTO worker_lease (name, owner, pid, host, heartbeat_at, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, pid = excluded.pid, "
                "host = excluded.host, heartbeat_at = excluded.heartbeat_at, "
                "started_at = CASE WHEN worker_lease.owner = excluded.owner "
                "THEN worker_lease.started_at ELSE excluded.started_at END "
                "WHERE worker_lease.owner = excluded.owner OR worker_lease.heartbeat_at < ?",
                (name, owner, os.getpid(), socket.gethostname(), now, now, now - ttl))
            async with db.execute("SELECT owner FROM worker_lease WHERE name = ?",
                                  (name,)) as cur:
                row = await cur.fetchone()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return bool(row and row[0] == owner)


async def release_lease(state, owner: str, name: str = LEASE_NAME) -> None:
    async with state._connect() as db:
        await db.execute("DELETE FROM worker_lease WHERE name = ? AND owner = ?",
                         (name, owner))
        await db.commit()


async def lease_holder(state, ttl: float = LEASE_TTL_SECONDS,
                       name: str = LEASE_NAME) -> dict | None:
    """The worker holding a fresh lease ({owner, pid, host, ...}), or None."""
    import aiosqlite

    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM worker_lease WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
    if row is None or row["heartbeat_at"] < time.time() - ttl:
        return None
    return dict(row)


class _Renewer:
    """Renews a worker's leases from a thread of its own, every third of
    their life, so a job that holds up the event loop (synchronous work)
    doesn't let the leases go stale under a worker that is still running.
    A lease another worker has taken meanwhile is noted in ``lost``."""

    def __init__(self, db_path: str, owner: str, ttl: float):
        self.db_path, self.owner, self.ttl = db_path, owner, ttl
        self.names: set[str] = set()
        self.lost: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease:{owner}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def hold(self, name: str) -> None:
        with self._lock:
            self.names.add(name)
            self.lost.discard(name)

    def drop(self, name: str) -> None:
        with self._lock:
            self.names.discard(name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.ttl)

    def renew(self) -> None:
        with self._lock:
            names = sorted(self.names)
        if not names:
            return
        with closing(sqlite3.connect(self.db_path, timeout=max(1.0, self.ttl / 2))) as db:
            gone = []
            for name in names:
                cur = db.execute("UPDATE worker_lease SET heartbeat_at = ? "
                                 "WHERE name = ? AND owner = ?", (time.time(), name, self.owner))
                if cur.rowcount == 0:
                    gone.append(name)
            db.commit()
        if gone:
            with self._lock:
                self.lost.update(n for n in gone if n in self.names)

    def _run(self) -> None:
        while not self._stop.wait(self.ttl / 3):
            try:
                self.renew()
            except Exception as e:
                logger.debug("worker %s: renewing its leases failed: %s", self.owner, e)


# ── What a running job can ask for ──

class JobContext:
    """Handed to a job's code: its parameters and progress, a log, the
    stop it must check between pages, and the money it may spend."""

    def __init__(self, state, job: jobs.Job, worker: "Worker", lane_state: dict):
        self.state = state
        self.job = job
        self.worker = worker
        self.lane = lane_state
        self.params = job.params
        self.progress = dict(job.progress)
        self.actual_cost = float(job.actual_cost or 0.0)
        # How it ended besides done: a person's cancel or pause, the
        # spend limit, or "requeue" (the lane paused, the worker stopping).
        self.outcome = ""
        self.paused_reason = ""
        self._said: list[asyncio.Task] = []
        self._loop = asyncio.get_running_loop()

    @property
    def id(self) -> str:
        return self.job.id

    async def save(self, **changes) -> None:
        """Merge ``changes`` into the job's progress and store it."""
        self.progress.update(changes)
        await self._store()

    async def _store(self) -> None:
        if not await jobs.save_progress(self.state, self.job.id, self.progress,
                                        actual_cost=self.actual_cost, worker=self.worker.name):
            self.outcome = jobs.LOST

    async def say(self, line: str) -> None:
        """One line of the job's own log (a waiting command prints it)."""
        log = list(self.progress.get("log") or [])
        seq = (log[-1][0] + 1) if log else 1
        log.append([seq, str(line)])
        await self.save(log=log[-jobs.LOG_LINES:])

    def say_soon(self, line: str) -> None:
        """``say`` from code that can't wait: a progress callback, here or
        in a thread a load runs in."""
        def start():
            self._said.append(asyncio.ensure_future(self.say(line)))
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is self._loop:
            start()
        else:
            self._loop.call_soon_threadsafe(start)

    async def flush(self) -> None:
        said, self._said = self._said, []
        if said:
            await asyncio.gather(*said, return_exceptions=True)

    async def spend(self, amount: float) -> None:
        """Record money spent, before the next request is made: a worker
        killed after this still has it counted against the caps."""
        self.actual_cost += float(amount or 0.0)
        await self._store()

    async def reserve(self, estimate: float, first: float, *, per_day: float | None = None,
                      per_month: float | None = None) -> float:
        """The money this job may spend; SpendLimit when the run's cap (or
        the day's, the month's) can't pay for its ``first`` request."""
        self.job.actual_cost = self.actual_cost
        return await jobs.reserve(self.state, self.job, estimate, first,
                                  per_day=per_day, per_month=per_month)

    async def stop_reason(self) -> str:
        """Why the job must stop at the next page, or "": cancelled or
        paused by a person, its lane paused, the worker shutting down, or
        the job no longer this worker's."""
        asked = await jobs.stop_requested(self.state, self.job.id, self.worker.name)
        lease = self.lane.get("lease")
        if (asked == jobs.LOST or self.outcome == jobs.LOST
                or (lease and lease in self.worker.lost_leases())):
            self.outcome = jobs.LOST
            return "paused"
        if asked == jobs.STOP_CANCEL:
            self.outcome = jobs.CANCELLED
            return "cancelled"
        if asked == jobs.STOP_PAUSE:
            self.outcome, self.paused_reason = jobs.PAUSED, jobs.PAUSED_BY_HAND
            return "paused"
        if await jobs.lane_paused(self.state, self.job.lane):
            self.outcome = jobs.QUEUED
            return "paused"
        if self.worker.stopping:
            self.outcome = jobs.QUEUED
            return "paused"
        return ""

    def pause(self, reason: str) -> None:
        self.outcome, self.paused_reason = jobs.PAUSED, reason

    async def lane_run(self, stage: str, provider: str = "") -> str:
        """One run in the runs log for everything this lane does until it
        has nothing left (a hundred site reads are one run, not a hundred)."""
        run = self.lane.get("run")
        if run is None:
            run = self.lane["run"] = {"id": await self.state.start_run(stage, provider=provider),
                                      "records": 0}
        return run["id"]

    def count(self, records: int = 1) -> None:
        if self.lane.get("run"):
            self.lane["run"]["records"] += records


# ── The worker ──

class Worker:
    """Runs jobs. A full worker (no ``batch_ids``) holds the worker lease
    while it runs; one limited to ``batch_ids`` (a CLI command running
    only its own work) takes no worker lease, only the leases of the lanes
    it runs, so a full worker started meanwhile runs every other lane."""

    def __init__(self, state, *, name: str | None = None, batch_ids=None,
                 poll: float = POLL_SECONDS, lease_ttl: float = LEASE_TTL_SECONDS,
                 wait_for_lease: bool = True, due_sources: bool = True):
        self.state = state
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.batch_ids = set(batch_ids) if batch_ids is not None else None
        self.full = batch_ids is None
        self.poll = poll
        self.lease_ttl = lease_ttl
        self.wait_for_lease = wait_for_lease
        self.due_sources = due_sources and self.full
        self.stopping = False
        self.holding = False
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._next_due = 0.0
        self._renewer: _Renewer | None = None

    def lost_leases(self) -> set[str]:
        return set(self._renewer.lost) if self._renewer is not None else set()

    async def run(self, stop: asyncio.Event | None = None) -> bool:
        """Work until ``stop`` is set. Returns False when the lease was
        held by another worker and ``wait_for_lease`` is off."""
        stop = stop or asyncio.Event()
        jobs.listen(self._wake)
        try:
            if self.full:
                while not stop.is_set():
                    if await acquire_lease(self.state, self.name, self.lease_ttl):
                        break
                    if not self.wait_for_lease:
                        return False
                    if await _wait(stop, self.lease_ttl / 3):
                        return True
                if stop.is_set():
                    return True
                self.holding = True
            self._renewer = _Renewer(self.state.db_path, self.name, self.lease_ttl)
            if self.full:
                self._renewer.hold(LEASE_NAME)
            self._renewer.start()
            logger.info("worker %s: running jobs", self.name)
            next_sweep = 0.0
            while not stop.is_set():
                if LEASE_NAME in self._renewer.lost:
                    logger.warning("worker %s: lost the lease; stopping", self.name)
                    break
                if self.full and time.monotonic() >= next_sweep:
                    # Jobs left running on a lane nobody holds (a worker
                    # that died, a lane loop that crashed) go back.
                    next_sweep = time.monotonic() + self.lease_ttl / 3
                    await jobs.sweep_stale(self.state, fresh_after=time.time() - self.lease_ttl)
                await self._start_lanes()
                if self.due_sources and time.monotonic() >= self._next_due:
                    self._next_due = time.monotonic() + DUE_SOURCES_EVERY_SECONDS
                    await _enqueue_due_sources(self.state)
                self._wake.clear()
                try:
                    await asyncio.wait_for(_first(stop, self._wake), timeout=self.poll)
                except asyncio.TimeoutError:
                    pass
            return True
        finally:
            jobs.unlisten(self._wake)
            await self._shutdown()
            if self._renewer is not None:
                self._renewer.stop()
            if self.holding:
                try:
                    await release_lease(self.state, self.name)
                except Exception as e:
                    logger.debug("worker: releasing the lease failed: %s", e)
                self.holding = False

    def idle(self) -> bool:
        return not any(not t.done() for t in self._tasks.values())

    async def _start_lanes(self) -> None:
        paused = await jobs.paused_lanes(self.state)
        for lane in await jobs.ready_lanes(self.state, self.batch_ids):
            task = self._tasks.get(lane)
            if lane in paused or (task is not None and not task.done()):
                continue
            self._tasks[lane] = asyncio.create_task(self._lane(lane), name=f"lane:{lane}")

    async def _shutdown(self) -> None:
        self.stopping = True
        running = [t for t in self._tasks.values() if not t.done()]
        if running:
            # Running jobs stop at their next page and go back to the queue.
            done, pending = await asyncio.wait(running, timeout=SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    async def _lane(self, lane: str) -> None:
        lease = jobs.LANE_LEASE.format(lane)
        if not await acquire_lease(self.state, self.name, self.lease_ttl, name=lease):
            return          # another worker runs this lane now
        self._renewer.hold(lease)
        lane_state: dict = {"lease": lease}
        try:
            # Whatever is still marked running on this lane is nobody's now.
            await jobs.sweep_stale(self.state, lane)
            while not self.stopping:
                if lease in self._renewer.lost or await jobs.lane_paused(self.state, lane):
                    break
                job = await jobs.claim(self.state, lane, self.name, self.batch_ids)
                if job is None:
                    break
                await self.run_job(job, lane_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("lane %s stopped", lane)
        finally:
            run = lane_state.get("run")
            if run is not None:
                try:
                    await self.state.finish_run(run["id"], records=run["records"])
                except Exception as e:
                    logger.debug("lane %s: closing its run failed: %s", lane, e)
            self._renewer.drop(lease)
            try:
                await _shielded(release_lease(self.state, self.name, lease))
            except asyncio.CancelledError:
                pass

    async def _finish(self, job: jobs.Job, status: str, **kw) -> None:
        """Record how ``job`` ended, fenced to this worker, trying again
        while the database is busy."""
        for attempt in range(FINISH_TRIES):
            try:
                if not await jobs.finish(self.state, job.id, status, worker=self.name, **kw):
                    logger.info("job %s: no longer this worker's; its end isn't recorded here",
                                job.id)
                return
            except Exception as e:
                if attempt + 1 >= FINISH_TRIES or not jobs.is_transient(e):
                    raise
                await asyncio.sleep(0.5 * 2 ** attempt)

    async def run_job(self, job: jobs.Job, lane_state: dict | None = None) -> None:
        """Run one claimed job and record how it ended."""
        state = self.state
        if self._renewer is None:
            self._renewer = _Renewer(state.db_path, self.name, self.lease_ttl)
        kind = jobs.handler(job.kind)
        if kind is None:
            await self._finish(job, jobs.FAILED, error=f"no code runs jobs of kind {job.kind!r}")
            return
        ctx = JobContext(state, job, self, lane_state if lane_state is not None else {})
        try:
            try:
                result = await kind.run(ctx)
            finally:
                await ctx.flush()
        except asyncio.CancelledError:
            # Cut off (the worker stopped): back to the queue, as far as it got.
            await _shielded(self._finish(job, jobs.QUEUED, progress=ctx.progress,
                                         actual_cost=ctx.actual_cost))
            raise
        except jobs.SpendLimit as e:
            await self._finish(job, jobs.PAUSED, paused_reason=jobs.SPEND_LIMIT, error=str(e),
                               progress=ctx.progress, actual_cost=ctx.actual_cost)
            return
        except jobs.LaneExhausted as e:
            if e.retry_after:
                await self._finish(job, jobs.QUEUED, error=str(e), retry_in=e.retry_after,
                                   progress=ctx.progress, actual_cost=ctx.actual_cost)
            else:
                await jobs.pause_lane(state, job.lane, str(e))
                await self._finish(job, jobs.QUEUED, error=str(e), progress=ctx.progress,
                                   actual_cost=ctx.actual_cost)
            logger.warning("lane %s: %s", job.lane, e)
            return
        except jobs.JobError as e:
            await self._finish(job, jobs.FAILED, error=str(e), result={"exit_code": e.code},
                               progress=ctx.progress, actual_cost=ctx.actual_cost)
            return
        except Exception as e:
            message = str(e) or e.__class__.__name__
            # Only a failed try counts: waits (an allowance, a restart, a
            # pause) never use up a job's tries.
            tries = (job.attempts or 0) + 1
            if jobs.is_transient(e) and tries < jobs.MAX_ATTEMPTS:
                wait = jobs.backoff_seconds(tries)
                logger.warning("job %s (%s): %s; trying again in %d s", job.id, job.kind,
                               message, wait)
                await self._finish(job, jobs.QUEUED, error=message, retry_in=wait,
                                   attempts=tries, progress=ctx.progress,
                                   actual_cost=ctx.actual_cost)
            else:
                logger.exception("job %s (%s) failed", job.id, job.kind)
                await self._finish(job, jobs.FAILED, error=message, attempts=tries,
                                   progress=ctx.progress, actual_cost=ctx.actual_cost)
            return
        status = ctx.outcome or jobs.DONE
        if status == jobs.LOST:
            logger.info("job %s: another worker took its lane over; leaving it to that one",
                        job.id)
            return
        await self._finish(job, status, result=result or {},
                           paused_reason=ctx.paused_reason if status == jobs.PAUSED else "",
                           progress=ctx.progress, actual_cost=ctx.actual_cost)


async def _shielded(coro):
    try:
        await asyncio.shield(coro)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("worker: recording an interrupted job failed: %s", e)


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _first(*events: asyncio.Event) -> None:
    waits = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def _enqueue_due_sources(state) -> None:
    try:
        from harvey.modules.job_kinds import enqueue_due_sources
        await enqueue_due_sources(state)
    except Exception as e:
        logger.warning("worker: checking sources set to update failed: %s", e)


# ── Running the worker on its own (harvey worker) ──

async def serve(state, stop: asyncio.Event | None = None) -> None:
    """``harvey worker``: run jobs until stopped, waiting for the lease
    while another worker (the dashboard's) holds it."""
    await state.init_db()
    worker = Worker(state)
    holder = await lease_holder(state)
    if holder:
        logger.info("Another worker (pid %s on %s) is running jobs; waiting until it stops.",
                    holder.get("pid"), holder.get("host"))
    await worker.run(stop)


# ── A command that waits for its own jobs ──

# A job queued to try again later than this is left to the worker: a
# command doesn't wait an hour for a source's allowance to come back.
WAIT_FOR_RETRY_SECONDS = 300


def _settled(job: jobs.Job, paused_lanes: dict) -> bool:
    """Nothing more will happen to ``job`` while a command waits: it
    finished, it is paused, its lane is paused, or it waits to try again
    much later."""
    from datetime import datetime, timedelta, timezone

    if job.status in (*jobs.FINISHED, jobs.PAUSED):
        return True
    if job.status != jobs.QUEUED:
        return False
    if job.lane in paused_lanes:
        return True
    if job.not_before:
        try:
            when = datetime.fromisoformat(str(job.not_before))
        except ValueError:
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return when - now > timedelta(seconds=WAIT_FOR_RETRY_SECONDS)
    return False


async def wait_for_batch(state, batch_id: str, *, say=print, poll: float = 0.5,
                         lease_ttl: float = LEASE_TTL_SECONDS) -> list[jobs.Job]:
    """Wait until every job of ``batch_id`` has finished (or paused),
    printing each job's log lines as they come. When no worker holds the
    lease, run these jobs here; otherwise follow the worker that does.
    Returns the batch's jobs, oldest first."""
    stop = asyncio.Event()
    holder = await lease_holder(state, lease_ttl)
    task = None
    if holder is None:
        worker = Worker(state, batch_ids={batch_id}, wait_for_lease=False,
                        lease_ttl=lease_ttl, poll=min(poll, POLL_SECONDS))
        task = asyncio.create_task(worker.run(stop))
    else:
        say(f"  Queued. The worker running on {holder.get('host') or 'this machine'} "
            f"(pid {holder.get('pid')}) runs it; following its progress (Ctrl-C stops "
            "following, the job goes on).")
    printed: dict[str, int] = {}
    try:
        while True:
            found = list(reversed(await jobs.list_jobs(state, batch_id=batch_id, limit=100000)))
            for job in found:
                last = printed.get(job.id, 0)
                for seq, line in job.progress.get("log") or []:
                    if seq > last:
                        say(line)
                        last = seq
                printed[job.id] = last
            paused = await jobs.paused_lanes(state)
            if found and all(_settled(j, paused) for j in found):
                return found
            if task is not None and task.done():
                if task.exception() is not None:
                    raise task.exception()
                if not task.result():
                    # Another worker took the lease meanwhile: follow it.
                    task = None
            await asyncio.sleep(poll)
    finally:
        stop.set()
        if task is not None:
            await task
