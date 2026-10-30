"""Lanes: one pace per outside service, shared by everything that calls it.

A module's manifest names the pace a service allows (``[[lanes]]`` with
``min_interval_seconds``, or ``max_per_second``), and its capability calls
``await wait(lane)`` right before each request. Every caller in the process
takes its turn on one lock per lane, so two runs at once (a check from the
dashboard and a registry pull's follow-up) never add up to more than the
service allows.

Between processes on one machine (the dashboard and a CLI command) the time
of the last request is kept in data/lanes/<lane>.last under an OS lock, so
they take turns too. Where that file can't be written (a read-only
checkout), the pace still holds within the process: a thread lock per lane
covers callers on other event loops and threads.

A lane no manifest declares is refused, not left unpaced: a typo in a
capability's lane, or a manifest that failed to load, must not mean
requests as fast as they go.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.lanes")

LANE_DIR = PROJECT_ROOT / "data" / "lanes"

_locks: dict[str, asyncio.Lock] = {}
_last: dict[str, float] = {}
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class UnknownLane(ValueError):
    """A lane no manifest declares."""


@lru_cache(maxsize=1)
def _lanes() -> tuple[dict, ...]:
    # Only Harvey's own modules declare lanes (a data pack can't).
    from harvey.modules.loader import load_manifests

    return tuple(spec for m in load_manifests(data_dir=None) for spec in m.lanes)


def interval(lane: str) -> float:
    """Seconds between two requests in ``lane``: the slowest pace any
    manifest gives it. UnknownLane when no manifest declares it."""
    gap = 0.0
    declared = False
    for spec in _lanes():
        if spec.get("id") != lane:
            continue
        declared = True
        if spec.get("min_interval_seconds"):
            gap = max(gap, float(spec["min_interval_seconds"]))
        elif spec.get("max_per_second"):
            gap = max(gap, 1.0 / float(spec["max_per_second"]))
    if not declared:
        raise UnknownLane(f"no module declares the lane {lane!r}")
    return gap


def _thread_lock(lane: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(lane)
        if lock is None:
            lock = _thread_locks[lane] = threading.Lock()
        return lock


def _lock(lane: str) -> asyncio.Lock:
    # One lock per lane and event loop (a test, or a CLI command, runs its own).
    loop = asyncio.get_running_loop()
    key = f"{lane}:{id(loop)}"
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _lock_file(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        logger.debug("lane lock unavailable: %s", e)


def _unlock_file(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _turn(lane: str, gap: float) -> float:
    """Wait (blocking, so it runs in a thread) until ``gap`` seconds have
    passed since the last request any caller made in ``lane``: this
    process's (under the lane's thread lock) or another's (the lane file);
    record now and return it."""
    with _thread_lock(lane):
        now = _file_turn(lane, gap, _last.get(lane, 0.0))
        _last[lane] = now
        return now


def _file_turn(lane: str, gap: float, last_here: float) -> float:
    try:
        Path(LANE_DIR).mkdir(parents=True, exist_ok=True)
        handle = open(Path(LANE_DIR) / f"{lane}.last", "a+", encoding="utf-8")
    except OSError:
        handle = None
    if handle is None:
        delay = last_here + gap - time.time()
        if delay > 0:
            time.sleep(delay)
        return time.time()
    try:
        _lock_file(handle)
        try:
            handle.seek(0)
            try:
                last = max(float(handle.read().strip() or 0), last_here)
            except ValueError:
                last = last_here
            delay = last + gap - time.time()
            if delay > 0:
                time.sleep(delay)
            now = time.time()
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(f"{now:.3f}")
                handle.flush()
            except OSError:
                pass
            return now
        finally:
            _unlock_file(handle)
    finally:
        handle.close()


async def wait(lane: str) -> None:
    """Return when a request in ``lane`` may go out, and count it as made:
    never two requests closer together than the lane's pace."""
    gap = interval(lane)
    if gap <= 0:
        return
    async with _lock(lane):
        await asyncio.to_thread(_turn, lane, gap)
