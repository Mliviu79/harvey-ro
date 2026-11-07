"""The messaging channels' own loop: SMS and WhatsApp read and answered
apart from email.

A full worker (harvey/modules/worker.py: the dashboard's own, ``harvey
worker``'s, ``harvey run``'s) runs a pass every EVERY_SECONDS beside its
jobs, one at a time, and every pass holds the lease LEASE while it runs, so
one database never has two at once (the Conversations page's send of an
answer you wrote takes the same lease, and leaves it for the next pass when
a pass holds it). A pass reads every channel that is on (MessageReader:
opt-outs, stop-on-reply, the Handler's classifier and drafts), then reads
back deliveries and sends the approved answers that may go
(MessageSender).

Nothing on the email side holds it: a mailbox read that failed, a mail
provider Harvey can't send through, a gap that holds cold email, the
heartbeat's cycle stopping early or not running at all. What stops it: the
module off (nothing is read or sent), and for sending the rails
MessageSender.blocked keeps (the global kill switch, the channel's own
pause, the npathi lane paused when nPathi said the credits ran out, quiet
hours and the sending hours, the caps). The model is asked only for a
message that needs it (its intent, an answer), and not in quiet hours,
while the brain can't be used or past its budget: such a message is read
again on a later pass. An opt-out and stop-on-reply need no model, and
sending an approved answer needs none.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from harvey.integrations import npathi

logger = logging.getLogger("harvey.messages")

LEASE = "messaging"
# How often a full worker starts a pass (one at a time).
EVERY_SECONDS = 60


@asynccontextmanager
async def held(state, owner: str):
    """The messaging lease, renewed from a thread while held: yields True,
    or False while someone else's pass holds it."""
    from harvey.modules.worker import LEASE_TTL_SECONDS, _Renewer, acquire_lease, release_lease

    if not await acquire_lease(state, owner, LEASE_TTL_SECONDS, name=LEASE):
        yield False
        return
    renewer = _Renewer(state.db_path, owner, LEASE_TTL_SECONDS)
    renewer.hold(LEASE)
    renewer.start()
    try:
        yield True
    finally:
        await asyncio.shield(asyncio.to_thread(renewer.stop))
        try:
            await asyncio.shield(release_lease(state, owner, LEASE))
        except Exception as e:
            logger.debug("messaging: releasing the lease failed: %s", e)


class Loop:
    """A worker's messaging passes. ``config``, ``env``, ``brain`` and
    ``transport`` are for tests; a worker reads the settings each pass and
    keeps one Brain while they say the same."""

    def __init__(self, state, *, config=None, env=None, brain=None, transport=None):
        self.state = state
        self._config, self._env, self._brain = config, env, brain
        self._brain_for = None
        self.transport = transport

    def _settings(self):
        from harvey.config import load_config, load_env

        if self._config is not None:
            return self._config, self._env
        return load_config(), load_env()

    def _brain_of(self, config):
        if self._config is not None and self._brain is not None:
            return self._brain
        from harvey.brain import Brain

        key = config.brain.model_dump() if hasattr(config.brain, "model_dump") else None
        if self._brain is None or key != self._brain_for:
            self._brain, self._brain_for = Brain(self.state, config.brain), key
        return self._brain

    async def run(self, owner: str) -> dict | None:
        """One pass: read, then send. {"read", "sent"}; None when another
        pass holds the lease. Never raises but for a stop."""
        from harvey.modules import switches

        if not await switches.is_enabled(self.state, npathi.MODULE):
            return {"read": None, "sent": 0}
        async with held(self.state, owner) as got:
            if not got:
                return None
            try:
                config, env = self._settings()
            except Exception as e:
                logger.error(f"Messaging: Harvey's settings can't be read ({type(e).__name__}); "
                             "nothing read or sent.")
                return {"read": None, "sent": 0}
            read = await self._read(config, env)
            from harvey.agents.messenger import MessageSender

            sent = await MessageSender(self.state, config, self.transport).run()
            return {"read": read, "sent": sent}

    async def _read(self, config, env) -> bool | None:
        from harvey.agents.handler import Handler

        try:
            brain = self._brain_of(config)
            handler = Handler(brain, self.state, config, env)
        except Exception as e:
            logger.error(f"Messaging: messages not read ({type(e).__name__}).")
            return None
        handler.message_transport = self.transport
        decided: list[bool] = []

        async def may_think() -> bool:
            # (Decided once a pass, on the first message that needs it.)
            if not decided:
                decided.append(await self._model_ok(config, brain))
                if decided[0]:
                    handler.skills = brain.load_skills_for_agent("handler")
            return decided[0]

        return await handler.read_messages(may_think)

    async def _model_ok(self, config, brain) -> bool:
        from harvey.main import in_quiet_hours

        try:
            if in_quiet_hours(config):
                return False
            if await brain.preflight():
                return False
            percent = config.usage.max_daily_claude_percent
            return await brain.is_within_budget(max(int(200 * (percent / 100)), 1),
                                                max_percent=percent)
        except Exception as e:
            logger.warning(f"Messaging: the model's budget couldn't be checked "
                           f"({type(e).__name__}); messages that need it wait.")
            return False


async def send_now(state, item_id: str, config, transport=None) -> bool:
    """Try the answer you just wrote at once, under the lease, through every
    check the drain makes. False when a pass holds the lease (that pass or
    the next one sends it) or nPathi isn't connected."""
    from harvey.agents.messenger import MessageSender

    owner = f"answer:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    async with held(state, owner) as got:
        if not got:
            return False
        client = await npathi.client(state, transport=transport)
        if client is None:
            return False
        await MessageSender(state, config, transport).send_one(client, item_id)
        return True
