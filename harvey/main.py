"""Harvey's main heartbeat loop. Always Be Closing.

The heartbeat is the Email module's: it reads replies, writes, and sends.
Finding companies, reading their sites and loading public data are jobs
the worker runs (harvey/modules/worker.py); when there is nobody left to
write to, the heartbeat queues a contact search as one. With the Email
module off it does nothing at all.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta

import pytz

from harvey.brain import Brain
from harvey.sending_mode import sync_sending_mode
from harvey.config import (
    ConfigError, load_config, load_env, HarveyConfig, legacy_sending_held, sending_notice,
    compliance_notice, send_window_of, config_to_edit,
)
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.modules import switches
from harvey.pidfile import (
    LOCK_FILE, hold_loop_lock, read_pid, release_loop_lock, remove_pid_file, write_pid_file,
)
from harvey.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvey")

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900

# Hard ceiling on one cycle's parallel agents. return_exceptions=True on
# gather() only isolates a task that raises — a task that hangs (a socket or
# lock that never resolves) blocks gather() forever, taking the whole
# heartbeat loop down silently with it. This is the only thing standing
# between one stuck agent and Harvey freezing for hours with no agent
# knowing it's dead — found in production after exactly that happened.
CYCLE_TIMEOUT_SECONDS = 600

# Cap on a single quiet-hours sleep chunk. See the quiet-hours block in
# heartbeat() for why this exists — a system sleep/wake cycle during one
# multi-hour asyncio.wait_for left Harvey silently stalled well past when
# quiet hours actually ended.
QUIET_HOURS_POLL_SECONDS = 300

# Phase-1 tasks that read replies; sending waits for all of them to succeed.
REPLY_READERS = ("handle_replies", "read_replies")


def in_quiet_hours(config: HarveyConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def sends_through_quiet_hours(config: HarveyConfig) -> bool:
    """True when quiet hours pause Harvey's own work but not cold email
    that is due in its recipient's window.

    With a send window in each recipient's time zone, the operator's night
    can be a recipient's morning. Prospecting and writing still sleep, and
    replies wait for the operator's hours as before. A heartbeat wakes only
    when a cold email can go now (Sender.cold_due_in_window): it reads and
    handles replies first (so stop-on-reply has run; reading a reply is what
    consumes it, so it can't be left for the morning), then runs the sender,
    which stages any draft campaign into the outbox (no model call, nothing
    sent) and sends only cold email. Otherwise it does nothing. Native mail
    providers only.
    """
    email = config.channels.email
    window = send_window_of(config)
    return bool(window is not None and window.recipient_timezone
                and email.enabled and email.provider in NATIVE_PROVIDERS)


def seconds_until_quiet_hours_end(config: HarveyConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


# When nothing is writable, status says why once this many unusable
# prospects (a guessed, invalid or missing email) were found in the last 24
# hours: without a working verifier every address is a guess, and the
# Writer never drafts for one. Nothing re-checks a guess, so the window is
# a day, never all time.
PROSPECT_BACKLOG = 20


def _new_counts(summary: dict) -> tuple[int, int]:
    """(all 'new' prospects, the ones the Writer can draft for)."""
    prospects = summary.get("prospects") or {}
    new = (prospects.get("new", 0) if isinstance(prospects, dict) else 0) or 0
    # A summary built without the writable count keeps the old rule.
    writable = summary.get("writable_prospects", new) or 0
    return new, writable


def unusable_backlog_notice(summary: dict) -> str:
    """Why the Writer has had nothing to write for about a day, or ""."""
    _, writable = _new_counts(summary)
    recent = summary.get("recent_unusable_prospects", 0) or 0
    if writable or recent < PROSPECT_BACKLOG:
        return ""
    return (
        f"Harvey found {recent} prospects in the last day, but none has a verified "
        "or catch-all email, so for about a day there was nothing to write for. Add "
        "an email verifier key (REOON_API_KEY, ZEROBOUNCE_API_KEY or HUNTER_API_KEY "
        "in .env) or check it still has credits. `harvey export --all` exports the "
        "list as it is."
    )


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: HarveyConfig,
    summary: dict | None = None,
    email_on: bool = True,
) -> str:
    """Decide what Harvey should do next based on current state.

    With the Email module off there is nothing to decide: "idle" (the
    heartbeat doesn't run its cycle at all).

    Uses deterministic priority rules (handle_replies > send_campaign >
    write_campaign > idle) instead of burning a Claude call on a decision
    that is fully derivable from pipeline counts. This saves budget every
    cycle and removes a fragile LLM string-parsing step. Finding people to
    write to isn't the heartbeat's: the contact finder (harvey/finder.py)
    runs on its own, whatever the Email switch says.
    """
    if summary is None:
        summary = await state.get_state_summary()

    # Only prospects the Writer can draft for trigger writing: one with a
    # guessed email stays 'new' for good, and counting it starved the cycle.
    new_prospects, writable = _new_counts(summary)
    draft_campaigns = summary.get("draft_campaigns", 0) or 0
    open_conversations = summary.get("open_conversations", 0) or 0

    # Held Instantly drafts can't deploy; choosing send_campaign for them
    # every cycle would starve the writer forever.
    deployable = 0 if legacy_sending_held(config) else draft_campaigns

    if not email_on:
        action, reason = "idle", "the Email module is off"
    elif open_conversations > 0:
        action, reason = "handle_replies", f"{open_conversations} open conversation(s) waiting"
    elif deployable > 0:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif writable > 0:
        action, reason = "write_campaign", f"{writable} new prospect(s) with no drafts ready"
    else:
        action, reason = "idle", f"{new_prospects} new prospect(s), none ready to write for"
        if unusable_backlog_notice(summary):
            logger.warning(unusable_backlog_notice(summary))

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _run_agents(tasks: list) -> dict:
    """Run (name, coroutine) pairs in parallel; return {name: outcome}.

    Errors are caught per task so one bad agent never takes down the cycle,
    and the whole batch is bounded so one HUNG agent (no exception, just
    never returns) can't either. A task's outcome is what it returned, or
    the exception it raised; a task cut off by the bound has no outcome.
    """
    if not tasks:
        return {}
    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
            timeout=CYCLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Cycle timed out after {CYCLE_TIMEOUT_SECONDS}s with "
            f"{[t[0] for t in tasks]} still running — cancelled, "
            "moving on to the next heartbeat instead of hanging forever."
        )
        results = []
    outcomes = {}
    for (name, _), result in zip(tasks, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.error(f"Agent {name} failed: {result}", exc_info=result)
        outcomes[name] = result
    return outcomes


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, waking immediately on shutdown.

    Returns True if a shutdown was requested during the sleep.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def send_hold_reason(unread: list, paused: str, read_problem: str) -> str | None:
    """What to record about this cycle's send phase: why sending is held by
    unread replies, "" when it ran (clear the note), or None to leave the
    note alone (a paused brain has its own Today card)."""
    if paused:
        return None
    if unread:
        return read_problem or "replies couldn't all be read and handled."
    return ""


async def record_send_phase(state, held_reason: str):
    """Note why sending was held this cycle (Today and `harvey status` show
    it), or clear the note when sending ran. Never stops the loop."""
    try:
        if held_reason:
            await state.note_sends_held(held_reason)
        else:
            await state.clear_sends_held()
    except Exception as e:
        logger.warning(f"Couldn't record the send hold: {e}")


async def heartbeat(stop_event: asyncio.Event | None = None):
    """Harvey's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("Harvey is online. Always Be Closing.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return
    env = load_env()
    state = StateManager()
    brain = Brain(state, config.brain)
    logger.info(
        f"Thinking with {brain.provider_label}"
        + (f" ({brain.model})" if brain.model else "")
        + f". Change brain.provider in {config_to_edit()} to switch."
    )
    for notice in (sending_notice(config), compliance_notice(config)):
        if notice:
            logger.warning("=" * 60)
            logger.warning(notice)
            logger.warning("=" * 60)

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from harvey.agents.writer import Writer
    from harvey.agents.sender import Sender
    from harvey.agents.handler import Handler
    from harvey.agents.analyst import Analyst
    from harvey.agents.audience import Audience

    audience = Audience(state, env)
    writer = Writer(brain, state, config, env)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)
    analyst = Analyst(state)

    interval = config.usage.heartbeat_interval_minutes * 60
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            # The Email module's switch is read every cycle: off means no
            # writing, sending or reading mail, whatever harvey.yaml says,
            # and so nothing for the heartbeat to do (finding companies and
            # reading sites are jobs; the worker runs them).
            email_on = await switches.is_enabled(state, switches.EMAIL_MODULE)
            if not email_on:
                logger.info("The Email module is off: nothing to do. Checking again in "
                            f"{config.usage.heartbeat_interval_minutes} minutes.")
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 0. Approval or autopilot, as the config file says now: the
            # dashboard can switch it while Harvey runs.
            await sync_sending_mode(config, state)

            # 1. Check quiet hours. With send windows in recipients' own time
            # zones, quiet hours only stop prospecting and writing: a cycle
            # still reads replies and sends what's due (sends_through_quiet_hours).
            quiet = in_quiet_hours(config)
            send_only = (email_on and quiet and sender.is_native
                         and sends_through_quiet_hours(config))
            if quiet and not send_only:
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                # Sleep in short polled chunks rather than one multi-hour
                # wait: found in production that a system sleep/wake cycle
                # during a long asyncio.wait_for left the process silently
                # stalled for the rest of the quiet-hours window (11+ hours
                # in one case) instead of resuming once the Mac woke up.
                # Re-checking wall-clock quiet hours every few minutes
                # bounds the worst case to QUIET_HOURS_POLL_SECONDS instead
                # of however long the missed timer would have run.
                shutting_down = False
                while in_quiet_hours(config):
                    if await _interruptible_sleep(QUIET_HOURS_POLL_SECONDS, stop_event):
                        shutting_down = True
                        break
                if shutting_down:
                    break
                continue
            # Quiet hours wake only for cold email that can go now: due,
            # inside its recipient's window, not held by a cap or a pause.
            # Replies wait for the operator's hours, as they always have.
            sender.cold_only = send_only
            if send_only and not await sender.cold_due_in_window():
                logger.info(
                    f"Quiet hours. No cold email can go now; checking again in "
                    f"{config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 2. Is the brain usable at all? (Codex: CLI version, isolation,
            # login, account and model checked without a prompt.) If not, do nothing
            # this cycle — no replies consumed, nothing written or sent —
            # and say why, rather than running agents that would each fail.
            problem = await brain.preflight()
            if problem:
                logger.error(
                    f"Harvey is paused: {problem} Nothing was sent or changed. "
                    f"Re-checking in {config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 3. Check usage budget (real subscription quota when readable,
            # else Harvey's own call counter)
            if not await brain.is_within_budget(
                max_calls, max_percent=config.usage.max_daily_claude_percent
            ):
                logger.info(
                    f"{brain.provider_label} usage limit reached "
                    f"({config.usage.max_daily_claude_percent}% of quota or "
                    f"{max_calls} calls). Sleeping 1h, then re-checking."
                )
                if await _interruptible_sleep(3600, stop_event):
                    break
                continue

            # 4. Decide what to do (in quiet hours: only read replies and send)
            if send_only:
                logger.info("Quiet hours: reading replies, then sending what's due "
                            "in recipients' own time zones.")
                summary = {}
                action = "quiet_hours_send"
            else:
                # The email contacts the finder found, as prospects to write
                # to (the finder itself never depends on email).
                try:
                    await audience.run()
                except Exception as e:
                    logger.warning(f"Audience: couldn't pick prospects from the contacts: {e}")
                logger.info("Checking pipeline state...")
                summary = await state.get_state_summary()
                action = await decide_next_action(brain, state, config, summary=summary,
                                                  email_on=email_on)

            # 5. Execute — run independent agents in parallel where possible
            # Handler is always safe to run alongside other agents
            tasks = []
            has_open_convos = summary.get("open_conversations", 0) > 0

            if action == "handle_replies":
                tasks.append(("handle_replies", handler.run()))
            elif action == "write_campaign":
                tasks.append(("write_campaign", writer.run()))
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
            elif action == "idle":
                # Analyst is cheap (no Claude calls) — keep analytics fresh
                tasks.append(("analyze", analyst.run()))

            # Sending happens in a second phase, after every agent above has
            # finished: the handler's stop-on-reply must cancel follow-ups to
            # people who just replied before anything is sent. Native mail
            # providers drain the outbox every cycle, so their mailbox is
            # read every cycle too (handle_replies above only runs when a
            # conversation is already open).
            send_tasks = []
            if not email_on:
                pass
            elif action == "send_campaign":
                send_tasks.append(("send_campaign", sender.run))
            elif sender.is_native:
                send_tasks.append(("send_outbox", sender.run))
            if email_on and sender.is_native and not any(n == "handle_replies" for n, _ in tasks):
                tasks.append(("read_replies", handler.run()))

            outcomes = await _run_agents(tasks)

            # Phase 2: send — only after every reply-reading task this cycle
            # finished reading and handling replies (it returns True; a
            # failure, exception or timeout doesn't), and only if the brain
            # didn't stop being usable meanwhile. Otherwise a reply may be
            # unread, so its stop-on-reply hasn't run: queued emails wait.
            if send_tasks:
                unread = [n for n, _ in tasks
                          if n in REPLY_READERS and outcomes.get(n) is not True]
                paused = brain.paused_reason
                if paused or unread:
                    reason = paused or "replies couldn't all be read and handled."
                    logger.error(
                        f"Not sending this cycle: {reason} Queued emails wait "
                        "until replies can be read again."
                    )
                    held = send_hold_reason(unread, paused, getattr(handler, "last_read_problem", ""))
                    if held is not None:
                        await record_send_phase(state, held)
                else:
                    await record_send_phase(state, send_hold_reason([], "", ""))
                    await _run_agents([(name, run()) for name, run in send_tasks])

            # 6. Log the action (best-effort; never kills the loop)
            try:
                await state.log_action(action_type=action, agent="main")
            except Exception as e:
                logger.warning(f"Failed to log action '{action}': {e}")

            consecutive_errors = 0

            # 7. Sleep until next heartbeat
            logger.info(
                f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
            )
            if await _interruptible_sleep(interval, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event):
                break

    logger.info("Harvey shutting down. Deals don't close themselves, but I need a break.")


def _needs_setup() -> bool:
    """Check if Harvey needs first-time setup."""
    from harvey.paths import PROJECT_ROOT
    from harvey.config import _find_config_file, ConfigFileNotFoundError

    env_file = PROJECT_ROOT / ".env"

    # If .env doesn't exist, definitely needs setup
    if not env_file.exists():
        return True

    # harvey.local.yaml wins over the tracked harvey.yaml template, same
    # resolution order the real config loader uses (see config.py).
    try:
        config_file = _find_config_file()
    except ConfigFileNotFoundError:
        return True

    try:
        import yaml
        from harvey.textio import read_user_text
        config = yaml.safe_load(read_user_text(config_file))
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Company", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    # The jobs the heartbeat queues (finding contacts) need a worker. With
    # the dashboard up, its worker runs them and this one waits; without
    # it, this one runs every job, at any hour (quiet hours are email's).
    worker = asyncio.create_task(_run_worker(stop_event))
    try:
        await heartbeat(stop_event)
    finally:
        stop_event.set()
        await asyncio.gather(worker, return_exceptions=True)


async def _run_worker(stop_event: asyncio.Event):
    from harvey.modules.worker import Worker, lease_holder

    try:
        state = StateManager()
        await state.init_db()
        holder = await lease_holder(state)
        if holder:
            logger.info("Jobs run in the worker on %s (pid %s); this one stands by.",
                        holder.get("host"), holder.get("pid"))
        await Worker(state).run(stop_event)
    except Exception as e:
        logger.warning(f"The job worker stopped: {e}. Jobs wait until the dashboard "
                       "or harvey worker runs them.")


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        if not sys.stdin.isatty():
            # Started by the dashboard or a service: nobody can answer the wizard.
            print("Harvey isn't set up yet. Run `harvey setup` in a terminal first.")
            sys.exit(1)
        print("\n  First time running Harvey? Let's get you set up.\n")
        from harvey.setup import run_setup
        asyncio.run(run_setup())
        return

    # One loop at a time: two would both drain the same Outbox. The lock
    # also tells the dashboard the PID file names a live loop.
    lock = hold_loop_lock(LOCK_FILE)
    if lock is None:
        running = read_pid()
        print(f"Harvey is already running{f' (pid {running})' if running else ''}. "
              "Stop it first, from the dashboard or with Ctrl+C in its terminal.")
        sys.exit(1)
    write_pid_file()
    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")
    finally:
        remove_pid_file()
        release_loop_lock(lock)


if __name__ == "__main__":
    from harvey.textio import utf8_stdio
    utf8_stdio()
    main()
