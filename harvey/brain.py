"""Brain — Harvey's thinking engine.

Runs on the operator's Claude subscription (Claude Code headless, the
default) or, when ``brain.provider: codex`` is configured, their ChatGPT
subscription through an isolated Codex runtime (see harvey/codex.py). Both
can be set up side by side (brain.claude, brain.codex): a Brain is one
provider's (its BrainConfig view, BrainConfig.for_provider), fixed, and a
failing provider never falls back to the other one: its pause and its
budget hold only the work that runs on it. With ``auth: api_key`` the
calls are billed per token instead, and that provider's USD caps hold them
before each call (harvey/brain_spend.py).
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from harvey.state import StateManager

logger = logging.getLogger("harvey.brain")

from harvey.paths import PROJECT_ROOT
from harvey.textio import read_user_text

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300  # a single Claude call should never hang forever
DEFAULT_MAX_RETRIES = 2  # retries on transient failures (non-zero exit, timeout)
RETRY_BASE_DELAY = 5.0  # seconds; doubles per attempt
KILL_REAP_SECONDS = 10.0  # waiting for a killed child on shutdown

# stderr fragments that indicate retrying is pointless
_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)


PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

# Settings key holding a provider's last health transition, for `harvey
# status` and the dashboard: {"provider", "status": "ok"|"paused", "reason",
# "since", and for pauses "kind" (plus "until", epoch seconds, for usage
# limits)}, one per provider (status_key): a pause of one never says
# anything about the other. BRAIN_STATUS_KEY alone is what an older Harvey
# wrote for its one provider, read while a provider has no key of its own.
BRAIN_STATUS_KEY = "brain_status"


def status_key(provider: str) -> str:
    return f"{BRAIN_STATUS_KEY}:{provider}"

# Codex failures that repeat on every call until the operator acts (login,
# CLI version, model choice, an unsafe Codex config layer). After one, the
# Brain stops starting runtimes until something changes.
_PERSISTENT_CODEX_KINDS = {"setup", "auth", "isolation"}
# Codex failures that can come after the model was asked: with an API key
# such a call is counted at a turn's bound (brain_spend.unknown_usd).
_UNREPORTED_CODEX_KINDS = {"timeout", "turn_failed"}
# Re-check a paused provider at least this often even if nothing changed
# on disk (e.g. a managed config layer was fixed).
PROVIDER_RECHECK_SECONDS = 1800
# After Codex reports the plan's usage limit, stop calling it (and keep the
# budget gate closed) for this long.
USAGE_LIMIT_PAUSE_SECONDS = 3600


def parse_brain_status(raw: str) -> dict:
    """The stored BRAIN_STATUS_KEY value as a dict ({} if absent or bad)."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def active_brain_status(raw: str, provider: str) -> dict:
    """The stored status if it belongs to ``provider``, else {}: a pause
    recorded for Codex says nothing about Claude."""
    status = parse_brain_status(raw)
    return status if status.get("provider") == provider else {}


async def stored_status(state, provider: str, default: str | None = None) -> dict:
    """``provider``'s last recorded health transition ({} if none): its own
    key, else what an older Harvey recorded for it. That older record was
    its one provider's, which it hid once the other one was chosen: with
    ``default`` (the default provider now) it is read for the default only,
    so a pause of a provider not in use then never shows as one now."""
    try:
        raw = await state.get_setting(status_key(provider))
        if not raw and (default is None or provider == default):
            raw = await state.get_setting(BRAIN_STATUS_KEY)
    except Exception:
        return {}
    return active_brain_status(raw or "", provider)


# The stream-json lines a run writes before its result.
_STREAM_EVENTS = ("system", "assistant", "user", "stream_event", "rate_limit_event")



# What a metered read takes from the CLI's stdout at a time.
READ_CHUNK = 1 << 16


def _metered(line: bytes, meter) -> bool:
    """One stream-json line fed to ``meter`` (its result isn't: the CLI's
    own figure is read from it): whether the limit is reached."""
    text = line.strip()
    if not text.startswith(b"{"):
        return False
    try:
        event = json.loads(text)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") != "result" and meter.feed(event)

class BrainUnavailable(Exception):
    """The provider can't be used until the operator acts. Raised only where
    a caller must leave its input untouched for a later retry."""


class Brain:
    def __init__(self, state: StateManager, brain_config=None):
        from harvey.config import BrainConfig

        self.state = state
        self.config = brain_config or BrainConfig()
        self.provider = self.config.provider
        self.model = self.config.model
        self.codex = None
        # Whether the last think_json got no reply at all (an outage, logout,
        # rate limit or timeout), as opposed to a reply that wasn't valid
        # JSON. Callers that count model failures must not count outages.
        self.last_reply_empty = False
        # Codex health (unused for Claude, whose behaviour is unchanged).
        self.unavailable = ""
        self._unavailable_at = 0.0
        self._unavailable_fingerprint = None
        self._usage_limited_until = 0.0
        self._usage_pause_recorded = False
        self._status_synced = False
        # Why the last budget check said no, in Harvey's words ("" when it
        # didn't, or when only a subscription's share was used: callers say
        # that one themselves).
        self.budget_why = ""
        from harvey import brain_auth
        self.auth = brain_auth.auth_of(self.config)
        # (Billed per token: an API key, or a Claude Console account.)
        self.api_key = brain_auth.billed(self.provider, self.auth)
        if self.provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            self.codex = CodexClient(
                model=self.model, reasoning_effort=self.config.reasoning_effort,
                native_home=brain_auth.codex_home(self.auth), api_key=self.api_key,
            )
            self.quota = CodexQuotaClient(self.codex)
        else:
            from harvey.integrations.quota import QuotaClient
            self.quota = QuotaClient(state, auth=self.auth)

    def for_profile(self, profile_id: str = "") -> "Brain":
        """The brain that writes for a campaign's AI profile. One profile
        exists today, so every id (and "", none) gets this brain. The
        Writer asks by the campaign's profile; the Handler doesn't yet."""
        return self

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS[self.provider]

    @property
    def paused_reason(self) -> str:
        """Why calls are currently being skipped ("" when they aren't)."""
        if self.unavailable:
            return self.unavailable
        if time.monotonic() < self._usage_limited_until:
            return f"{self.provider_label} usage limit reached for this plan"
        return ""

    async def preflight(self) -> str:
        """Can this Brain be used right now? Returns "" or the reason not.

        Codex only (Claude keeps its existing behaviour): runs the adapter's
        full pre-prompt check (CodexClient.preflight): CLI version, route and
        lockdown attested with no login in the runtime, the native login read
        only (expiry, renewal), then with the access-only copy the configured
        model against the catalog, the ChatGPT account, and a started
        tool-less thread. No turn, so no inference; nothing is written to the
        native login. While paused, the check is repeated only when the login
        file or the Codex executable changes, or every
        PROVIDER_RECHECK_SECONDS, so a persistent problem doesn't start
        runtimes every heartbeat.
        """
        if self.provider != "codex":
            return await self._claude_preflight()
        from harvey.codex import CodexError

        await self._expire_usage_pause()
        if self.unavailable:
            unchanged = self.codex.fingerprint() == self._unavailable_fingerprint
            recent = time.monotonic() - self._unavailable_at < PROVIDER_RECHECK_SECONDS
            if unchanged and recent:
                return self.unavailable
        try:
            await self.codex.preflight()
        except CodexError as e:
            if e.kind in _PERSISTENT_CODEX_KINDS:
                await self._mark_unavailable(e)
                return self.unavailable
            return str(e)  # transient: skip this cycle, check again next time
        except Exception as e:
            return f"Codex preflight failed unexpectedly ({type(e).__name__})."
        if self.unavailable:
            await self._mark_available()
        elif not self._status_synced:
            logger.info(
                f"Codex ready: supported CLI, isolation verified, login and account "
                f"valid, model {self.model or 'auto'} usable with no tools (no prompt sent).")
            # A pause recorded before a restart must not outlive the problem.
            self._status_synced = True
            stored = await stored_status(self.state, self.provider)
            until = stored.get("until")
            if (stored.get("status") == "paused" and stored.get("kind") == "usage_limit"
                    and isinstance(until, (int, float)) and until > time.time()):
                # Still inside a usage-limit pause recorded before a restart.
                self._usage_limited_until = time.monotonic() + (until - time.time())
                self._usage_pause_recorded = True
            elif stored.get("status") == "paused":
                await self._mark_available()
        return ""

    async def _claude_preflight(self) -> str:
        """Claude: the installed CLI must support every isolation flag
        (claude_runtime.check_cli). Runs ``claude --help`` only: no login
        check, no model call. Returns "" or why calls are paused."""
        from harvey.claude_runtime import ClaudeRuntimeError, check_cli

        try:
            await asyncio.to_thread(check_cli)
        except ClaudeRuntimeError as e:
            await self._mark_unavailable(e)
            return self.unavailable
        if self.unavailable:
            await self._mark_available()
        return ""

    def _fingerprint(self):
        if self.provider == "codex":
            return self.codex.fingerprint()
        from harvey.claude_runtime import fingerprint
        return fingerprint()

    async def _start_usage_pause(self):
        self._usage_limited_until = time.monotonic() + USAGE_LIMIT_PAUSE_SECONDS
        until = time.time() + USAGE_LIMIT_PAUSE_SECONDS
        resume = time.strftime("%H:%M UTC", time.gmtime(until))
        await self._record_status(
            "paused",
            f"{self.provider_label} usage limit reached for this plan; calls resume "
            f"after {resume}.",
            kind="usage_limit", until=until)
        self._usage_pause_recorded = True

    async def _expire_usage_pause(self):
        """Record recovery once a usage-limit pause has run out."""
        if self._usage_pause_recorded and time.monotonic() >= self._usage_limited_until:
            self._usage_pause_recorded = False
            if not self.unavailable:
                logger.info(f"{self.provider_label} usage-limit pause is over; resuming.")
                await self._record_status("ok", "")

    async def _mark_unavailable(self, error):
        first = not self.unavailable or self.unavailable != str(error)
        self.unavailable = str(error)
        self._unavailable_at = time.monotonic()
        self._unavailable_fingerprint = self._fingerprint()
        if first:
            await self._record_status("paused", self.unavailable,
                                      kind=getattr(error, "kind", "setup"))

    async def _mark_available(self):
        logger.info(f"{self.provider_label} is available again; resuming.")
        self.unavailable = ""
        self._unavailable_fingerprint = None
        await self._record_status("ok", "")

    async def _record_status(self, status: str, reason: str, **extra):
        try:
            await self.state.set_setting(status_key(self.provider), json.dumps({
                "provider": self.provider, "status": status, "reason": reason[:500],
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **extra,
            }))
        except Exception as e:
            logger.warning(f"Failed to record brain status: {e}")

    async def think(
        self,
        prompt: str,
        session_id: str | None = None,
        expect_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        agent: str = "",
        task: str = "",
    ) -> str:
        """Send a prompt to the configured provider and return the response.

        Retries transient failures with exponential backoff and enforces a
        hard timeout so a hung CLI call can never stall the heartbeat.
        Returns "" on unrecoverable failure (callers already handle empty).

        ``agent``/``task`` label the call in the usage_events ledger so the
        dashboard can attribute tokens and cost to specific work.

        With an API key, every attempt is first checked against the brain's
        USD caps (brain_spend.refusal): reached, nothing is sent and "" is
        returned.
        """
        if self.provider == "codex":
            return await self._think_codex(
                prompt, session_id=session_id, timeout=timeout,
                max_retries=max_retries, agent=agent, task=task,
            )

        from harvey import claude_runtime

        # Never start an unisolated call: a CLI missing any isolation flag
        # pauses Claude (like a Codex setup problem) until it's updated.
        try:
            executable = await asyncio.to_thread(claude_runtime.check_cli)
        except claude_runtime.ClaudeRuntimeError as e:
            logger.error(f"Claude call not sent: {e}")
            await self._mark_unavailable(e)
            return ""
        if self.unavailable:
            await self._mark_available()

        # The prompt always travels on stdin: argv is visible to other local
        # users, Windows caps it at ~32k chars, and .cmd shims re-parse it.
        cmd = claude_runtime.build_command(executable, self.model, session_id)

        logger.debug(f"Brain call (session={session_id}): {prompt[:100]}...")

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying brain call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)
            if await self._over_usd_cap():
                return ""
            meter = await self._meter()

            process = None
            try:
                # The directory outlives the child: it's removed only after
                # the process has exited (Windows can't delete a live cwd).
                with claude_runtime.isolated_cwd() as cwd:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        # A fresh empty directory: never the checkout (whose
                        # CLAUDE.md is the setup wizard) or its ancestors.
                        cwd=cwd,
                        env=claude_runtime.runtime_env(cwd, auth=self.auth),
                        # A pipe, never the inherited terminal.
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        if meter is None:
                            stdout, stderr = await asyncio.wait_for(
                                process.communicate(prompt.encode("utf-8")),
                                timeout=timeout,
                            )
                        else:
                            stdout, stderr = await asyncio.wait_for(
                                self._read_metered(process, prompt, meter),
                                timeout=timeout,
                            )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"Claude call timed out after {timeout:.0f}s. Killing process.")
                        try:
                            process.kill()
                            await process.wait()
                        except ProcessLookupError:
                            pass
                        stdout = None
                    except BaseException:
                        # Shutting down: stop and reap the child before its
                        # directory is removed, then let the cancellation
                        # through. The reap is bounded so a stuck child can't
                        # hold shutdown; a second cancel ends it at once.
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(process.wait(),
                                                   timeout=KILL_REAP_SECONDS)
                        except Exception as e:
                            logger.warning(f"Claude child not reaped on shutdown: {e!r}")
                        raise
                if stdout is None:
                    last_error = "timeout"
                    await self._count_unreported(agent, task, session_id,
                                                 at_least=meter.cost if meter else 0.0)
                    continue  # retry

                if meter is not None and meter.stopped:
                    # (Stopped at what was left of the brain's caps: what it
                    # cost is charged, its partial reply isn't an answer,
                    # and no retry is made.)
                    _, payload, limits = self._parse_stream(
                        (stdout or b"").decode(errors="replace"))
                    try:
                        await self.state.increment_usage()
                        await self._record_usage(payload or {"usage": {}, "is_error": True},
                                                 agent=agent, task=task,
                                                 label=session_id or "",
                                                 unknown_turns=0 if payload else 1,
                                                 at_least=meter.cost)
                    except Exception as e:
                        logger.warning(f"Failed to record usage event: {e}")
                    self.budget_why = (f"the call was stopped at the brain's USD cap: "
                                       f"${meter.line:.2f} was left of it")
                    logger.info(f"Claude call stopped: {self.budget_why}.")
                    return ""

                if process.returncode != 0:
                    error = stderr.decode(errors="replace").strip()
                    _, failed, limits = self._parse_stream(
                        (stdout or b"").decode(errors="replace"))
                    if self.api_key:
                        # (Billed all the same: its result's cost, or a
                        # turn's bound when it gave none.)
                        try:
                            await self._record_usage(failed or {"usage": {}, "is_error": True},
                                                     agent=agent, task=task,
                                                     label=session_id or "",
                                                     unknown_turns=0 if failed else 1)
                        except Exception as e:
                            logger.warning(f"Failed to record usage event: {e}")
                    if limits:
                        from harvey.integrations import quota

                        await quota.note_infos(self.state, limits)
                    if any(i.get("status") == "rejected" for i in limits):
                        logger.error("Claude usage limit reached for this plan; "
                                     "calls wait until it resets.")
                        return ""
                    logger.error(
                        f"Claude exited with code {process.returncode}: {error[:300]}"
                    )
                    last_error = error
                    if any(p in error.lower() for p in _NON_RETRYABLE_PATTERNS):
                        logger.error(
                            "Non-retryable Claude error (auth). "
                            "Run 'claude login' and restart Harvey."
                        )
                        return ""
                    continue  # retry transient failures

                raw = stdout.decode(errors="replace").strip()
                response, payload, limits = self._parse_stream(raw)
                # The plan's usage as the CLI reported it (never fails the
                # call).
                limited = any(i.get("status") == "rejected" for i in limits)
                if limits:
                    from harvey.integrations import quota

                    await quota.note_infos(self.state, limits)

                # Usage accounting must never break the response path.
                try:
                    await self.state.increment_usage()
                except Exception as e:
                    logger.warning(f"Failed to record usage counter: {e}")
                try:
                    await self._record_usage(payload, agent=agent, task=task,
                                             label=session_id or "")
                except Exception as e:
                    logger.warning(f"Failed to record usage event: {e}")
                if payload is None:
                    await self._count_unreported(agent, task, session_id)

                if payload is None and raw and not response:
                    logger.error("Claude's output ended without a result.")
                    last_error = "no result"
                    continue

                if payload and payload.get("is_error"):
                    # The CLI reported an API error (overloaded, rate limited)
                    # inside a successful exit. That text is not the model's
                    # answer, so no caller may use it as one: retry like any
                    # other transient failure.
                    logger.error(f"Claude reported an error: {response[:300]}")
                    last_error = response
                    if limited:
                        # The plan's limit is reached: retrying can't help
                        # before it resets, and the reading now holds the
                        # budget check back until then.
                        logger.error("Claude usage limit reached for this plan; "
                                     "calls wait until it resets.")
                        return ""
                    continue

                logger.debug(f"Brain response: {response[:200]}...")
                return response

            except FileNotFoundError:
                logger.error(
                    "Claude CLI not found. Install it: https://code.claude.com/docs/en/setup"
                )
                return ""  # not retryable
            except asyncio.CancelledError:
                # Shutting down — kill the child so it doesn't orphan
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                logger.error(f"Brain error: {e}")
                last_error = str(e)
                if process is not None:
                    # (A call that started is billed all the same.)
                    await self._count_unreported(agent, task, session_id,
                                                 at_least=meter.cost if meter else 0.0)
                continue
            finally:
                if meter is not None:
                    # (Its cost is recorded: no longer in flight.)
                    meter.close()

        logger.error(
            f"Brain call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    async def _think_codex(
        self,
        prompt: str,
        session_id: str | None,
        timeout: float,
        max_retries: int,
        agent: str,
        task: str,
    ) -> str:
        """One isolated Codex turn per attempt (see harvey/codex.py)."""
        from harvey.codex import CodexError

        paused = self.paused_reason
        if paused:
            # Known not to work until the operator acts (or the limit
            # resets): don't start a runtime per call.
            logger.debug(f"Codex call skipped: {paused}")
            return ""
        logger.debug(f"Brain call via Codex (label={session_id})")
        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying Codex call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)
            if await self._over_usd_cap():
                return ""
            meter = await self._meter()
            try:
                # (Metered only with an API key: a subscription's call is
                # made as before.)
                result = await self.codex.run(
                    prompt, timeout=timeout, skip_if=lambda: self.paused_reason,
                    **({"meter": meter} if meter is not None else {}))
            except CodexError as e:
                if e.kind == "paused":
                    logger.debug(f"Codex call skipped: {e}")
                    return ""
                if e.kind == "budget":
                    # (Stopped at what was left of the brain's caps: its
                    # tokens so far charged, no retry.)
                    try:
                        await self.state.increment_usage(provider="codex")
                        await self._record_codex_usage(e.result, agent=agent, task=task,
                                                       label=session_id or "")
                    except Exception as err:
                        logger.warning(f"Failed to record usage event: {err}")
                    self.budget_why = str(e)
                    logger.info(f"Codex call stopped: {e}.")
                    return ""
                if e.kind in _UNREPORTED_CODEX_KINDS:
                    # (A turn that may have run, its tokens unknown.)
                    await self._count_unreported(agent, task, session_id,
                                                 at_least=meter.cost if meter else 0.0)
                last_error = str(e)
                if e.retryable:
                    logger.warning(f"Codex call failed: {e}")
                    continue
                logger.error(f"Codex call failed: {e}")
                if e.kind in _PERSISTENT_CODEX_KINDS:
                    await self._mark_unavailable(e)
                elif e.kind == "usage_limit":
                    await self._start_usage_pause()
                    invalidate = getattr(self.quota, "invalidate", None)
                    if invalidate:
                        invalidate()
                return ""
            except Exception as e:
                # Not expected (the adapter reports failures as CodexError);
                # log only the type, since messages could carry call details.
                # CancelledError is not an Exception, so shutdown propagates.
                logger.error(f"Codex call failed unexpectedly ({type(e).__name__}).")
                return ""
            else:
                try:
                    await self.state.increment_usage(provider="codex")
                except Exception as e:
                    logger.warning(f"Failed to record usage counter: {e}")
                try:
                    await self._record_codex_usage(result, agent=agent, task=task,
                                                   label=session_id or "")
                except Exception as e:
                    logger.warning(f"Failed to record usage event: {e}")
                return result.text
            finally:
                if meter is not None:
                    # (Its cost is recorded: no longer in flight.)
                    meter.close()

        logger.error(
            f"Codex call failed after {max_retries + 1} attempts. "
            f"Last error: {last_error[:200]}"
        )
        return ""

    async def _count_unreported(self, agent: str, task: str, label,
                                at_least: float = 0.0) -> None:
        """With an API key: a call that ended with no result (timed out,
        died) was billed all the same, so it is counted at a turn's bound
        (brain_spend.unknown_usd), never as $0, nor below what its stream
        was counted at. Nothing on a subscription."""
        if not self.api_key:
            return
        try:
            await self._record_usage({"usage": {}, "num_turns": 1, "is_error": True},
                                     agent=agent, task=task, label=label or "",
                                     unknown_turns=1, at_least=at_least)
        except Exception as e:
            logger.warning(f"Failed to record usage event: {e}")

    async def _meter(self):
        """With an API key, the meter a call runs under: stopped at what is
        left of the brain's caps (brain_spend.Meter). None on a
        subscription."""
        if not self.api_key:
            return None
        from harvey import brain_spend

        return brain_spend.Meter(self.config,
                                 room=await brain_spend.room(self.state, self.config))

    @staticmethod
    async def _read_metered(process, prompt: str, meter) -> tuple[bytes, bytes]:
        """communicate(), reading the stream a line at a time into ``meter``:
        when it reaches its limit the CLI is ended (``meter.stopped``) and
        what it wrote so far is returned."""
        process.stdin.write(prompt.encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        process.stdin.close()
        errors = asyncio.ensure_future(process.stderr.read())
        # (Read in chunks, never readline(): a stream-json line holds a
        # whole reply, past a stream's 64 KiB line limit.)
        read, pending = [], b""
        try:
            while True:
                chunk = await process.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                read.append(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                if any(_metered(line, meter) for line in lines):
                    meter.stopped = True
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    break
            await process.wait()
            return b"".join(read), await errors
        finally:
            if not errors.done():
                errors.cancel()

    async def _over_usd_cap(self) -> bool:
        """With an API key: whether the brain's USD caps hold the next call
        back (logged; brain_spend.refusal). Always False on a subscription."""
        if not self.api_key:
            return False
        from harvey import brain_spend

        try:
            why = await brain_spend.refusal(self.state, self.config)
        except Exception as e:
            # (Spend that can't be read isn't room to spend.)
            why = f"the brain's spend couldn't be read ({type(e).__name__})"
        if why:
            self.budget_why = why
            logger.info(f"{self.provider_label} call not sent: {why}.")
            return True
        return False

    def _cost(self, row: dict) -> tuple[float, str]:
        """(cost, source) of one usage row: as the CLI reported it, source
        ``result_json`` / ``codex_app_server``, on a subscription (where
        Codex's is 0: ChatGPT plans aren't billed per token); with an API
        key, source brain_spend.SOURCE, and Codex's tokens at the prices
        entered."""
        if not self.api_key:
            return (float(row.get("cost_usd") or 0.0),
                    "codex_app_server" if self.provider == "codex" else "result_json")
        from harvey import brain_spend

        if self.provider == "codex":
            if not brain_spend.priced(self.config):
                # (Its tokens kept, priced when the spend is read.)
                return 0.0, brain_spend.SOURCE_UNPRICED
            cost = brain_spend.token_cost(
                self.config, row.get("input_tokens"), row.get("output_tokens"),
                row.get("cache_read_tokens"), row.get("cache_creation_tokens"))
            return cost, brain_spend.SOURCE
        return float(row.get("cost_usd") or 0.0), brain_spend.SOURCE

    async def _record_codex_usage(self, result, agent: str, task: str, label: str) -> float:
        """One usage row per Codex call: cost 0 on a ChatGPT plan, which
        isn't billed per token; with an API key, its tokens at the prices
        entered. Returns the cost."""
        if not agent and label.startswith("harvey-"):
            agent = label[len("harvey-"):].split("-")[0]
        usage = dict(result.usage)
        usage.pop("cost_usd", None)
        cost, source = self._cost(usage)
        await self.state.record_usage_event(
            agent=agent,
            task=task or label,
            session_id=result.thread_id,
            model=result.model,
            duration_ms=result.duration_ms,
            num_turns=1,
            source=source,
            provider="codex",
            cost_usd=cost,
            **usage,
        )
        return cost

    @staticmethod
    def _parse_result_payload(raw: str) -> tuple[str, dict | None]:
        """Split CLI output into (response_text, result_payload).

        With ``--output-format stream-json`` stdout is one JSON event a
        line, the last the result, whose ``result`` field is the model's
        text (the object ``--output-format json`` printed alone, which is
        read the same way). If no line is a result (older CLI, truncated
        output), fall back to treating stdout as plain text so a formatting
        change can never break Harvey's pipeline.
        """
        text, payload, _ = Brain._parse_stream(raw)
        return text, payload

    @staticmethod
    def _parse_stream(raw: str) -> tuple[str, dict | None, list[dict]]:
        """(response_text, result_payload, rate_limit_infos) of a call's
        stdout: the last result line, and every rate_limit_event's info
        (harvey/integrations/quota.py), oldest first."""
        from harvey.integrations.quota import rate_limit_info

        payload, infos, events = None, [], 0
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events += event.get("type") in _STREAM_EVENTS
            info = rate_limit_info(event)
            if info:
                infos.append(info)
            elif event.get("type") == "result":
                payload = event
        if payload is None:
            # (Events with no result: a stream cut short, never the model's
            # text. Only output that isn't a stream is read as plain text.)
            return ("" if events else raw), None, infos
        return str(payload.get("result") or ""), payload, infos

    async def _record_usage(
        self, payload: dict | None, agent: str, task: str, label: str,
        unknown_turns: int = 0, at_least: float = 0.0,
    ) -> float:
        """Write per-model usage rows from a CLI result payload. Returns
        what they cost (brain_spend: with an API key, money; else the
        CLI's own figure, which isn't billed).

        With an API key a cost that is unknown is never $0: a payload with
        tokens and no cost, or one marked ``unknown_turns`` (a call that
        ended with no result: the turns it may have used), is counted at
        brain_spend.unknown_usd, and never below ``at_least`` (what its
        stream was counted at: brain_spend.Meter). Nothing changes on a
        subscription."""
        if not payload:
            return 0.0

        if not agent and label.startswith("harvey-"):
            # Callers historically pass labels like "harvey-scout-score".
            agent = label[len("harvey-"):].split("-")[0]

        session = str(payload.get("session_id") or "")
        duration_ms = int(payload.get("duration_ms") or 0)
        num_turns = int(payload.get("num_turns") or 0)
        is_error = bool(payload.get("is_error"))

        # modelUsage includes subagent spend; the flat `usage` field does not.
        model_usage = payload.get("modelUsage")
        rows = []
        if isinstance(model_usage, dict) and model_usage:
            for model, mu in model_usage.items():
                if not isinstance(mu, dict):
                    continue
                rows.append({
                    "model": str(model),
                    "input_tokens": int(mu.get("inputTokens") or 0),
                    "output_tokens": int(mu.get("outputTokens") or 0),
                    "cache_read_tokens": int(mu.get("cacheReadInputTokens") or 0),
                    "cache_creation_tokens": int(mu.get("cacheCreationInputTokens") or 0),
                    "cost_usd": float(mu.get("costUSD") or 0.0),
                })
        else:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                rows.append({
                    "model": "",
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(payload.get("total_cost_usd") or 0.0),
                })

        total = 0.0
        sources = []
        for row in rows:
            # (The agent's Codex runs give their result in this shape too:
            # harvey/agent/driver.py.)
            row["cost_usd"], source = self._cost(row)
            sources.append(source)
            total += row["cost_usd"]
        if self.api_key and total <= 0:
            from harvey import brain_spend

            tokens = sum(int(r.get(k) or 0) for r in rows for k in (
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
            # (An unpriced Codex call keeps its tokens instead.)
            if (unknown_turns or tokens) and all(x == brain_spend.SOURCE for x in sources):
                turns = unknown_turns or max(1, num_turns)
                estimate = max(await brain_spend.unknown_usd(self.state, self.config, turns),
                               float(at_least or 0.0))
                if estimate > 0:
                    if not rows:
                        rows.append({"model": ""})
                        sources.append(brain_spend.SOURCE)
                    rows[0]["cost_usd"] = estimate
                    sources[0] = brain_spend.SOURCE_ESTIMATE
                    num_turns = turns
                    total = estimate
        for row, source in zip(rows, sources):
            await self.state.record_usage_event(
                agent=agent,
                task=task or label,
                session_id=session,
                duration_ms=duration_ms,
                num_turns=num_turns,
                is_error=is_error,
                source=source,
                provider=self.provider,
                **row,
            )
        return total

    async def think_json(
        self,
        prompt: str,
        session_id: str | None = None,
        agent: str = "",
        task: str = "",
    ) -> dict | list | None:
        """Send a prompt and parse the response as JSON."""
        full_prompt = (
            prompt
            + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        )
        response = await self.think(
            full_prompt, session_id=session_id, agent=agent, task=task
        )
        self.last_reply_empty = not response
        if not response:
            return None
        parsed = self._extract_json(response)
        if parsed is None:
            logger.error(f"Failed to parse JSON from brain: {response[:200]}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """Best-effort JSON extraction: strips code fences, then falls back to
        locating the outermost JSON object/array in surrounding prose."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Fall back: model wrapped the JSON in explanation text
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_ch)
            end = cleaned.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    async def check_usage(self) -> float:
        """Check today's usage on this Brain's provider (own call count).

        Returns the number of calls made today. Errors are treated as
        "over budget is unknown" and return a safe 0.0 so a broken DB read
        doesn't crash the heartbeat.
        """
        try:
            return await self.state.get_usage_today(provider=self.provider)
        except Exception as e:
            logger.warning(f"Could not read usage from state: {e}")
            return 0.0

    async def is_within_budget(
        self, max_daily_calls: int = 200, max_percent: float | None = None
    ) -> bool:
        """Check whether Harvey may spend more of the active subscription.

        Preferred signal: the account's REAL utilization windows (Claude's,
        as its CLI reports them in each answer and its /usage command, or
        Codex's ChatGPT-plan rate limits), compared
        against max_percent so Harvey always leaves the remainder for the
        user's own interactive work. Fallback when those are unavailable:
        Harvey's own call counter for this provider against max_daily_calls.

        With an API key there is no plan: the brain's USD caps
        (brain_spend.refusal) take the windows' place, and the call counter
        stays as a backstop. ``budget_why`` says why it said no.
        """
        self.budget_why = ""
        await self._expire_usage_pause()
        if time.monotonic() < self._usage_limited_until:
            # Codex said the plan is out of usage: an unreadable quota must
            # not be mistaken for room to spend.
            logger.info(f"{self.paused_reason}; waiting for it to reset.")
            return False
        if self.api_key:
            if await self._over_usd_cap():
                return False
            calls = await self.check_usage()
            if calls >= max_daily_calls:
                self.budget_why = (f"Harvey made its {max_daily_calls} {self.provider_label} "
                                   "calls for today (the daily call limit)")
                return False
            return True
        if max_percent is not None:
            try:
                windows = await self.quota.get_utilization()
            except Exception as e:
                logger.debug(f"Quota check errored: {e}")
                windows = None
            if windows:
                worst = max(
                    (w["utilization"] for w in windows.values()), default=0.0
                )
                within = worst < max_percent
                if not within:
                    resets = ", ".join(
                        f"{name} resets {w['resets_at']}"
                        for name, w in windows.items()
                        if w.get("resets_at")
                    )
                    logger.info(
                        f"{self.provider_label} quota gate: utilization {worst:.0f}% >= "
                        f"{max_percent:.0f}% limit. {resets}"
                    )
                    return False
                if not self._gauge_unknown(windows):
                    return True
                # (A window the gauge doesn't know now, or knows only from
                # a while ago, isn't room to spend: the call counter holds
                # as well, as with no gauge at all.)

        calls = await self.check_usage()
        return calls < max_daily_calls

    def _gauge_unknown(self, windows: dict) -> bool:
        """Whether a window the gauge must know is missing, or was last
        seen too long ago (Claude's: the CLI reports only some windows in
        an answer; Codex's reader asks for every one, so never)."""
        unknown = getattr(self.quota, "unknown_windows", None)
        return bool(unknown()) if callable(unknown) else False

    def load_prompt(self, prompt_name: str, **kwargs) -> str:
        """Load a prompt template from the prompts/ directory and fill in variables."""
        prompt_file = PROMPTS_DIR / f"{prompt_name}.md"
        try:
            template = read_user_text(prompt_file)
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read prompt file {prompt_file}: {e}")
            return ""
        for key, value in kwargs.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        # Surface templating mistakes early instead of sending {{foo}} to Claude
        leftover = re.findall(r"\{\{(\w+)\}\}", template)
        if leftover:
            logger.warning(
                f"Prompt '{prompt_name}' has unfilled variables: {sorted(set(leftover))}"
            )
        return template

    def load_skill(self, skill_name: str) -> str:
        """Load a skill knowledge file from the skills/ directory."""
        skill_file = SKILLS_DIR / f"{skill_name}.md"
        try:
            return read_user_text(skill_file)
        except FileNotFoundError:
            logger.warning(f"Skill file not found: {skill_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read skill file {skill_file}: {e}")
            return ""

    def load_skills_for_agent(self, agent_name: str) -> str:
        """Load all relevant skills for a specific sub-agent.

        Returns concatenated skill content that should be injected
        into the agent's prompts as foundational knowledge.
        """
        # product_knowledge and competitive_intel are auto-generated by the trainer
        # and loaded by every agent that needs product context
        skill_map = {
            "writer": ["email_frameworks", "signal_playbook", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "handler": ["objection_handling", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "sender": ["email_frameworks", "product_knowledge"],
        }

        skill_names = skill_map.get(agent_name, [])
        if not skill_names:
            return ""

        sections = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                sections.append(content)

        if not sections:
            return ""

        return (
            "\n\n---\n## FOUNDATIONAL KNOWLEDGE\n"
            "Use the following frameworks and best practices to guide your work:\n\n"
            + "\n\n---\n\n".join(sections)
        )
