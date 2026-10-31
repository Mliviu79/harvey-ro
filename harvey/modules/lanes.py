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


# ── Free allowances: what a service gives free each period ──
#
# A search module's manifest declares the requests it may make free each
# period ([[free_allowances]]: per minute, hour, day or month; recurring
# only, never a one-time sign-up credit). Every request a search makes
# through it is counted here (count_uses), whoever made it: a person's run
# or the background. The background then takes only its share of what is
# left (free_left), so a person's own runs always find some.
#
# The counts are kept per module in data/lanes/<module>.uses, one counter
# per period for the period it is in, under the same OS lock as the pace;
# where that file can't be written, within the process. A month is the
# last 30 days, counted per day: a service's monthly credit renews on its
# own cycle (Brave's per key), not on the 1st, so a calendar month would
# let the background spend a cycle's share twice across the 1st.

_uses: dict[str, dict] = {}
_uses_guard = threading.Lock()

_WINDOWS = {"minute": "%Y-%m-%dT%H:%M", "hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d"}
# The rolling period: its days.
ROLLING = {"month": 30}
_PERIODS = (*_WINDOWS, *ROLLING)


def free_allowances(module_id: str) -> tuple[dict, ...]:
    """The free allowances a module's manifest declares (per, requests)."""
    return _allowances().get(module_id, ())


@lru_cache(maxsize=1)
def _allowances() -> dict[str, tuple[dict, ...]]:
    from harvey.modules.loader import load_manifests

    return {m.id: m.free_allowances for m in load_manifests(data_dir=None)
            if m.free_allowances}


def _window(per: str, now: float) -> str:
    return time.strftime(_WINDOWS[per], time.gmtime(now))


def _days(counts: dict, per: str, now: float) -> dict[str, int]:
    """A rolling period's counts per UTC day, those still inside it."""
    days = counts.get(per)
    if not isinstance(days, dict):
        return {}
    since = time.strftime("%Y-%m-%d", time.gmtime(now - ROLLING[per] * 86400))
    return {d: int(n) for d, n in days.items() if d > since}


def _current(counts: dict, specs, now: float) -> dict[str, int]:
    out = {}
    for spec in specs:
        per = spec["per"]
        if per in ROLLING:
            out[per] = sum(_days(counts, per, now).values())
            continue
        window, used = (counts.get(per) or ["", 0])[:2] \
            if isinstance(counts.get(per), list) else ("", 0)
        out[per] = int(used) if window == _window(per, now) else 0
    return out


def _read_uses(handle) -> dict:
    import json

    handle.seek(0)
    try:
        data = json.loads(handle.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _uses_file(module_id: str, change):
    """Run ``change(counts) -> counts | None`` on the module's counters
    under the OS lock (in memory when the file can't be written); returns
    the counters after it."""
    import json

    with _uses_guard:
        try:
            Path(LANE_DIR).mkdir(parents=True, exist_ok=True)
            handle = open(Path(LANE_DIR) / f"{module_id}.uses", "a+", encoding="utf-8")
        except OSError:
            handle = None
        if handle is None:
            counts = _uses.get(module_id, {})
            new = change(dict(counts))
            if new is not None:
                _uses[module_id] = counts = new
            return counts
        try:
            _lock_file(handle)
            try:
                counts = _read_uses(handle)
                new = change(dict(counts))
                if new is not None:
                    counts = new
                    try:
                        handle.seek(0)
                        handle.truncate()
                        handle.write(json.dumps(counts))
                        handle.flush()
                    except OSError:
                        pass
                return counts
            finally:
                _unlock_file(handle)
        finally:
            handle.close()


def count_uses(module_id: str, requests: int = 1) -> None:
    """Count ``requests`` made through a module against each of its free
    allowances (nothing for a module that declares none)."""
    specs = free_allowances(module_id)
    if not specs or requests <= 0:
        return
    now = time.time()

    def change(counts):
        used = _current(counts, specs, now)
        out = {k: v for k, v in counts.items() if k not in _PERIODS}
        for per in used:
            if per in ROLLING:
                days = _days(counts, per, now)
                today = time.strftime("%Y-%m-%d", time.gmtime(now))
                days[today] = days.get(today, 0) + int(requests)
                out[per] = days
            else:
                out[per] = [_window(per, now), used[per] + int(requests)]
        return out

    _uses_file(module_id, change)


def uses(module_id: str) -> dict[str, int]:
    """The requests counted in each declared period so far ({} for a
    module that declares no free allowance)."""
    specs = free_allowances(module_id)
    if not specs:
        return {}
    return _current(_uses_file(module_id, lambda counts: None), specs, time.time())


def free_left(module_id: str, share: float = 1.0) -> int | None:
    """How many more requests fit in every one of a module's free
    allowances, taking only ``share`` of each (the background's), or None
    when it declares none (it has no free allowance to count against)."""
    specs = free_allowances(module_id)
    if not specs:
        return None
    used = uses(module_id)
    share = min(max(float(share), 0.0), 1.0)
    return max(0, min(int(spec["requests"] * share) - used.get(spec["per"], 0)
                      for spec in specs))


# ── A search that said it can take no more for now ──
#
# Out of credit, a spent quota or a refused key: the background leaves the
# module alone until the time it gave (``retry_after``), or HOLD_SECONDS
# when it gave none, rather than make one more refused request a job.

HOLD_SECONDS = 3600.0


def hold(module_id: str, seconds: float | None = None) -> None:
    """Leave ``module_id`` out of the background for ``seconds``."""
    until = time.time() + max(float(seconds or HOLD_SECONDS), 1.0)
    _uses_file(module_id, lambda counts: {**counts, "held_until": until})


def held(module_id: str) -> float:
    """Seconds the module is still left out of the background (0: not)."""
    counts = _uses_file(module_id, lambda counts: None)
    try:
        return max(0.0, float(counts.get("held_until") or 0) - time.time())
    except (TypeError, ValueError):
        return 0.0
