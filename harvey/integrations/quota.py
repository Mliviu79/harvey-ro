"""Claude subscription quota gauge, from what the Claude CLI itself says.

Harvey never reads the Claude login: no credentials file, no keychain, no
call of its own to Anthropic with the operator's token (Anthropic's terms
don't let a developer collect, store or pass on Claude.ai credentials). The
numbers come from two places the CLI provides on its own:

- the ``rate_limit_event`` line every stream-json run writes (the brain's
  calls, the agent's chats, wakes and tasks): the plan's status and, per
  window (``five_hour``, ``seven_day``, a model's week), its utilization as
  a fraction and when it resets. An answer may report only some windows, so
  each window keeps its latest reading with the time it was seen, and a
  window is dropped once its reset has passed (as Claude's statusline does);
- the ``/usage`` command, asked of a tool-less print-mode CLI (``probe``)
  when no fresh reading exists: before the day's first call, while idle,
  after a reset. Its lines ("Current week (all models): 17% used · resets
  Nov 12, 2:59am (UTC)") are parsed tolerantly: text it doesn't recognise
  (an API-key login, a changed wording) is no reading, never an error, and
  is never logged (it can name the account).

The readings are kept in the settings table (SETTING_KEY), so every Harvey
process (the dashboard, the worker, the CLI) shares them. ``QuotaClient``
returns them in the shape the budget check, the Usage tab and ``harvey
usage`` read: per window ``utilization`` in percent (0-100) and
``resets_at``, plus when and from which source it was seen. A window the
CLI said is ``rejected`` (the plan's limit reached) reads 100% until it
resets, so the budget check holds Harvey back the way a spent quota does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("harvey.quota")

# The settings row: {"windows": {name: {utilization (0-1), resets_at (epoch
# seconds or None), resets_text, status, seen_at, source}}, "status",
# "rate_limit_type", "overage": {status, disabled_reason, using}, "seen_at",
# "probed_at", "probe_answered_at", "listed": {at, windows: what the last
# /usage check that gave a reading listed}}.
SETTING_KEY = "claude_usage"
SOURCE_ANSWER = "answer"
SOURCE_USAGE = "usage_check"
# The /usage check runs at most this often, and only when the newest reading
# is older than this.
PROBE_EVERY_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 60
# A reading with no reset time the CLI gave in a form Harvey can date is
# kept this long.
UNDATED_KEEP_SECONDS = 3600
# The windows the budget check must know, and how old a reading of one may
# be, before the gauge alone lets Harvey spend (else the call counter holds
# too: Brain.is_within_budget). A window a /usage check this recent didn't
# list is known: the plan has none now.
REQUIRED_WINDOWS = ("five_hour", "seven_day")
STALE_SECONDS = 1800
# A /usage check the model answered (its slash command not run by the CLI
# itself) isn't asked again for this long: it would spend the plan.
PROBE_ANSWERED_BACKOFF_SECONDS = 86400
# The window a rejected event that names none is kept under (as Codex's).
LIMIT_WINDOW = "limit_reached"

# Databases whose /usage check is running in this process (one at a time).
_probing: set[str] = set()


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def _epoch(value) -> int | None:
    n = _num(value)
    return int(n) if n is not None and n > 0 else None


# ── Rate-limit events ──

def rate_limit_info(event) -> dict | None:
    """The ``rate_limit_info`` of a stream-json ``rate_limit_event`` line
    (None for any other line)."""
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    return info if isinstance(info, dict) else None


def readings_from_info(info: dict, now: float | None = None) -> dict:
    """{window: reading} from one rate_limit_info: every window in
    ``unifiedWindows``, and the one ``rateLimitType`` names with the
    event's own utilization, reset and status when it isn't listed there."""
    now = time.time() if now is None else now
    out: dict = {}
    top_status = str(info.get("status") or "")
    top_type = str(info.get("rateLimitType") or "")
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        for name, w in windows.items():
            if not isinstance(w, dict) or not isinstance(name, str) or not name:
                continue
            util = _num(w.get("utilization"))
            status = str(w.get("status") or (top_status if name == top_type else ""))
            if util is None and status != "rejected":
                continue
            out[name] = {"utilization": util if util is not None else 1.0,
                         "resets_at": _epoch(w.get("resetsAt")), "resets_text": "",
                         "status": status, "seen_at": now, "source": SOURCE_ANSWER}
    if top_type and top_type not in out:
        util = _num(info.get("utilization"))
        if util is not None or top_status == "rejected":
            out[top_type] = {"utilization": util if util is not None else 1.0,
                             "resets_at": _epoch(info.get("resetsAt")), "resets_text": "",
                             "status": top_status, "seen_at": now, "source": SOURCE_ANSWER}
    elif top_type and top_status == "rejected":
        out[top_type]["status"] = "rejected"
    if not top_type and top_status == "rejected" and not out:
        # (The limit reached, no window named: full all the same until
        # the event's reset.)
        out[LIMIT_WINDOW] = {"utilization": 1.0, "resets_at": _epoch(info.get("resetsAt")),
                             "resets_text": "", "status": "rejected", "seen_at": now,
                             "source": SOURCE_ANSWER}
    return out


def _expired(reading: dict, now: float) -> bool:
    resets = reading.get("resets_at")
    if isinstance(resets, (int, float)):
        return resets <= now
    return now - float(reading.get("seen_at") or 0) > UNDATED_KEEP_SECONDS


def merge(stored: dict, readings: dict, now: float | None = None,
          info: dict | None = None) -> dict:
    """``stored`` with ``readings`` put in (each window's latest replaces
    its last) and every window past its reset dropped; ``info`` (an
    event's rate_limit_info) also sets the plan's last status and overage."""
    now = time.time() if now is None else now
    out = dict(stored or {})
    windows = {k: v for k, v in (out.get("windows") or {}).items()
               if isinstance(v, dict)}
    windows.update(readings)
    out["windows"] = {k: v for k, v in windows.items() if not _expired(v, now)}
    if readings:
        out["seen_at"] = now
    if info:
        out["status"] = str(info.get("status") or "")
        out["rate_limit_type"] = str(info.get("rateLimitType") or "")
        if any(k in info for k in ("overageStatus", "overageDisabledReason", "isUsingOverage")):
            out["overage"] = {"status": str(info.get("overageStatus") or ""),
                              "disabled_reason": str(info.get("overageDisabledReason") or ""),
                              "using": bool(info.get("isUsingOverage"))}
    return out


def live(stored: dict, now: float | None = None) -> dict:
    """The stored windows whose reset hasn't passed."""
    now = time.time() if now is None else now
    return {k: v for k, v in ((stored or {}).get("windows") or {}).items()
            if isinstance(v, dict) and not _expired(v, now)}


def limited(stored: dict, now: float | None = None) -> bool:
    """Whether the CLI last said a window that hasn't reset yet is
    rejected (the plan's limit reached)."""
    return any(w.get("status") == "rejected" for w in live(stored, now).values())


async def load(state) -> dict:
    try:
        stored = json.loads(await state.get_setting(SETTING_KEY) or "{}")
    except Exception:
        return {}
    return stored if isinstance(stored, dict) else {}


async def _save(state, stored: dict) -> None:
    await state.set_setting(SETTING_KEY, json.dumps(stored, ensure_ascii=False))


async def note_infos(state, infos: list[dict], source: str = SOURCE_ANSWER) -> bool:
    """Record rate_limit_infos read from a run's stream (oldest first).
    Never raises: a reading lost is a reading the next answer brings."""
    infos = [i for i in infos or [] if isinstance(i, dict)]
    if not infos or state is None:
        return False
    try:
        stored = await load(state)
        now = time.time()
        for info in infos:
            readings = readings_from_info(info, now)
            for r in readings.values():
                r["source"] = source
            stored = merge(stored, readings, now, info)
        await _save(state, stored)
        return True
    except Exception as e:
        logger.debug(f"Claude usage reading not recorded: {e}")
        return False


async def note(state, event) -> bool:
    """Record one stream-json line if it is a rate_limit_event."""
    info = rate_limit_info(event)
    return await note_infos(state, [info]) if info else False


# ── The /usage check ──

# "Current week (all models): 17% used · resets Nov 12, 2:59am (UTC)"
_USAGE_LINE = re.compile(
    r"^\s*(?P<label>[^:\n]{1,80}?)\s*:\s*(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*used\b"
    r"(?:[^\n]*?\bresets?\s+(?P<when>[^\n]+?))?\s*$", re.IGNORECASE | re.MULTILINE)
# "Nov 12, 2:59am (UTC)", "3pm (Europe/Bucharest)", "Nov 12 at 3am"
_WHEN = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s*(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*(?:\((?P<tz>[^)]+)\))?\s*$",
    re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def window_key(label: str) -> str | None:
    """The rate-limit window a /usage line's label names (None: not one)."""
    low = label.strip().lower()
    if "session" in low:
        return "five_hour"
    if "week" in low:
        paren = re.search(r"\(([^)]*)\)", label)
        if paren is None or "all models" in paren.group(1).lower():
            return "seven_day"
        slug = re.sub(r"[^a-z0-9]+", "_", paren.group(1).lower()).strip("_")
        return f"seven_day_{slug}" if slug else "seven_day"
    return None


def parse_reset(text: str, now: float | None = None) -> int | None:
    """Epoch seconds of a /usage reset time, or None when it can't be
    dated (an unknown zone or wording)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    m = _WHEN.match((text or "").strip())
    if not m:
        return None
    try:
        tz = ZoneInfo(m.group("tz").strip()) if m.group("tz") else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if hour > 23 or minute > 59:
        return None
    current = datetime.fromtimestamp(time.time() if now is None else now, tz)
    try:
        if m.group("mon"):
            month = _MONTHS.get(m.group("mon")[:3].lower())
            if month is None:
                return None
            when = current.replace(month=month, day=int(m.group("day")), hour=hour,
                                   minute=minute, second=0, microsecond=0)
            if when < current - timedelta(days=1):
                # (No year given: the next one, for a date past by more
                # than a day.)
                when = when.replace(year=when.year + 1)
        else:
            when = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when < current:
                when += timedelta(days=1)
    except ValueError:
        return None
    return int(when.timestamp())


def parse_usage_text(text: str, now: float | None = None) -> dict:
    """{window: reading} from the /usage command's text; {} for text it
    doesn't recognise."""
    now = time.time() if now is None else now
    out: dict = {}
    for m in _USAGE_LINE.finditer(text or ""):
        key = window_key(m.group("label"))
        if key is None or key in out:
            continue
        when = (m.group("when") or "").strip()
        resets = parse_reset(when, now) if when else None
        out[key] = {"utilization": min(float(m.group("pct")), 1000.0) / 100.0,
                    "resets_at": resets, "resets_text": "" if resets else when[:60],
                    "status": "", "seen_at": now, "source": SOURCE_USAGE}
    return out


def model_answered(result: dict | None) -> bool:
    """Whether a run's result line says a model was called (turns, tokens
    or cost): the /usage check's never is, when the CLI runs the command
    itself."""
    if not isinstance(result, dict):
        return False
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    counts = [result.get("num_turns"), result.get("total_cost_usd"),
              *(usage.get(k) for k in ("input_tokens", "output_tokens",
                                       "cache_creation_input_tokens",
                                       "cache_read_input_tokens"))]
    return any((_num(n) or 0) > 0 for n in counts)


def output_text(stdout: str) -> tuple[str, list[dict]]:
    """(the text a stream-json run answered, its rate_limit_infos): the
    result's text, and the assistant's text blocks. A run a model answered
    has no text here (model_answered): only the CLI's own words are read."""
    texts, infos, result = [], [], None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        info = rate_limit_info(event)
        if info:
            infos.append(info)
        elif event.get("type") == "result":
            result = event
            if isinstance(event.get("result"), str):
                texts.append(event["result"])
        elif event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
    if model_answered(result):
        return "", infos
    return "\n".join(texts), infos


def _answered(stdout: str) -> bool:
    for line in reversed((stdout or "").splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return model_answered(event)
    return False


def _run_probe(exe: str, auth: str = "terminal") -> str | None:
    """The /usage command's stream-json output (None when it didn't run or
    failed), under the login ``auth`` (brain.auth). Its text is never
    logged."""
    from harvey import claude_runtime

    cmd = claude_runtime.build_usage_probe_command(exe)
    with claude_runtime.isolated_cwd() as cwd:
        try:
            out = subprocess.run(
                cmd, cwd=cwd, env=claude_runtime.runtime_env(cwd, auth=auth), input="/usage",
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"The /usage check didn't run ({type(e).__name__}).")
            return None
    if out.returncode != 0:
        logger.debug(f"The /usage check exited with code {out.returncode}.")
        return None
    return out.stdout or ""


async def probe(state, *, force: bool = False, auth: str = "terminal") -> bool:
    """Ask the CLI's /usage command for the windows and record them. At most
    every PROBE_EVERY_SECONDS (``force``: now), one at a time per database;
    False when nothing was read. ``auth``: the login the calls use."""
    from harvey import claude_runtime

    if state is None:
        return False
    key = str(getattr(state, "db_path", ""))
    if key in _probing:
        return False
    _probing.add(key)
    try:
        stored = await load(state)
        now = time.time()
        if not force and now - float(stored.get("probed_at") or 0) < PROBE_EVERY_SECONDS:
            return False
        if not force and now - float(stored.get("probe_answered_at") or 0) \
                < PROBE_ANSWERED_BACKOFF_SECONDS:
            return False
        # (Recorded first: a check that fails isn't repeated every call.)
        stored["probed_at"] = now
        await _save(state, stored)
        try:
            exe = await asyncio.to_thread(claude_runtime.check_cli, None,
                                          claude_runtime.PROBE_FLAGS)
        except claude_runtime.ClaudeRuntimeError as e:
            logger.debug(f"The /usage check wasn't run: {e}")
            return False
        stdout = await asyncio.to_thread(_run_probe, exe, auth)
        if not stdout:
            return False
        text, infos = output_text(stdout)
        readings = parse_usage_text(text, time.time())
        stored = await load(state)
        if _answered(stdout):
            # (This CLI sent /usage to the model instead of running it: no
            # reading from that text, and not asked again for a day.)
            logger.warning("The /usage check was answered by the model, not the CLI: "
                           "it is not run again for a day.")
            stored["probe_answered_at"] = time.time()
        for info in infos:
            found = readings_from_info(info)
            for r in found.values():
                r["source"] = SOURCE_USAGE
            stored = merge(stored, found, info=info)
        stored = merge(stored, readings)
        if readings:
            # (Every window the plan has now: one it doesn't list, such as
            # the session with none running, isn't unknown.)
            stored["listed"] = {"at": time.time(), "windows": sorted(readings)}
        await _save(state, stored)
        if not readings:
            logger.debug("The /usage check gave no reading (not a subscription login, "
                         "or a wording Harvey doesn't know).")
        return bool(readings or infos)
    except Exception as e:
        logger.debug(f"The /usage check failed ({type(e).__name__}).")
        return False
    finally:
        _probing.discard(key)


# ── The gauge ──

def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gauge(stored: dict, now: float | None = None) -> dict | None:
    """The live windows as callers read them: utilization in percent (a
    rejected window 100), resets_at (ISO, else the CLI's own words), status,
    seen_at (ISO), age_seconds and source. None when there are none."""
    now = time.time() if now is None else now
    out = {}
    for name, r in live(stored, now).items():
        util = (_num(r.get("utilization")) or 0.0) * 100.0
        if r.get("status") == "rejected":
            util = max(util, 100.0)
        resets = r.get("resets_at")
        seen = float(r.get("seen_at") or now)
        out[name] = {
            "utilization": round(util, 1),
            "resets_at": _iso(resets) if isinstance(resets, (int, float))
            else str(r.get("resets_text") or ""),
            "status": str(r.get("status") or ""),
            "seen_at": _iso(seen),
            "age_seconds": max(0, int(now - seen)),
            "source": str(r.get("source") or SOURCE_ANSWER),
        }
    return out or None


def unknown_windows(stored: dict, now: float | None = None,
                    fresh: float = STALE_SECONDS) -> list[str]:
    """Of REQUIRED_WINDOWS, those with no reading seen within ``fresh``
    seconds that a /usage check that recent didn't say are absent."""
    now = time.time() if now is None else now
    windows = live(stored, now)
    listed = (stored or {}).get("listed")
    listed_recently = isinstance(listed, dict) and \
        now - float(_num(listed.get("at")) or 0) <= fresh
    out = []
    for name in REQUIRED_WINDOWS:
        r = windows.get(name)
        if r and now - float(_num(r.get("seen_at")) or 0) <= fresh:
            continue
        if listed_recently and name not in (listed.get("windows") or ()):
            continue
        out.append(name)
    return out


class QuotaClient:
    """The Claude plan's windows as the CLI last reported them (``state``:
    where the readings are kept; ``auth``: the login a /usage check runs
    under, brain.auth)."""

    def __init__(self, state=None, auth: str = "terminal"):
        self.state = state
        self.auth = auth
        self._stored: dict = {}

    async def get_utilization(self, probe_if_stale: bool = True) -> dict | None:
        """{window: {utilization (percent, 0-100), resets_at, status,
        seen_at, age_seconds, source}} or None. With no reading newer than
        PROBE_EVERY_SECONDS, asks /usage first (``probe_if_stale``)."""
        if self.state is None:
            return None
        stored = await load(self.state)
        if probe_if_stale:
            windows = live(stored)
            newest = max((float(w.get("seen_at") or 0) for w in windows.values()), default=0)
            now = time.time()
            if (now - newest >= PROBE_EVERY_SECONDS
                    or unknown_windows(stored, now, PROBE_EVERY_SECONDS)) \
                    and await probe(self.state, auth=self.auth):
                stored = await load(self.state)
        self._stored = stored
        return gauge(stored)

    def unknown_windows(self) -> list[str]:
        """The windows the budget check must know that the last
        get_utilization didn't (Brain.is_within_budget: the call counter
        then holds too)."""
        return unknown_windows(self._stored)
