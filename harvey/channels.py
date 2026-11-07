"""The messaging channels: SMS and WhatsApp through nPathi Chat, receiving only.

People send Harvey an SMS or a WhatsApp message; Harvey reads it into a
conversation (harvey/agents/messages.py, in the Handler's cycle). Harvey
never writes first on these channels: Law 506/2004 art. 12 wants prior
consent for a commercial SMS or app message, for a company as much as for a
person, and WhatsApp's rules forbid cold messages. Answering a message
someone sent is a later change; until then such a conversation is handed to
the user.

What is kept per install, in the settings table:

- ``msg_read_at:<channel>``: when the channel was last read completely
  (unix seconds), as ``mail_read_at:`` is for a mailbox;
- ``msg_read_gap:<channel>``: while a read hasn't reached back to the one
  before it (JSON: since, covered_from, reason): that channel's sends wait
  and Today says so, as ``mail_read_gap`` does for cold email;
- ``msg_read_problem:<channel>``: why the last read failed (Harvey's own
  sentence and the error's class, never nPathi's text);
- ``npathi:account``: the last account check (plan, the key's abilities,
  credits, the channels and their senders), for the settings card;
- ``npathi:optout_sync_at`` and ``npathi:optout_pending:<number>``: nPathi's
  opt-out list read once a day, and Harvey's own opt-outs still to add to it.
"""

from __future__ import annotations

import json
import time

from harvey.integrations import npathi

READ_KEY = "msg_read_at:"
GAP_KEY = "msg_read_gap:"
PROBLEM_KEY = "msg_read_problem:"
ACCOUNT_KEY = "npathi:account"
SYNC_KEY = "npathi:optout_sync_at"
PENDING_KEY = "npathi:optout_pending:"
SYNC_EVERY_SECONDS = 86400
CHANNELS = tuple(npathi.CHANNELS)
SETTING = "receive_{}"


def _json(raw) -> dict:
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


async def receiving(state) -> list[str]:
    """The channels whose inbound messages are read (their switches on)."""
    from harvey.modules import switches

    settings = await switches.module_settings(state, npathi.MODULE)
    return [ch for ch in CHANNELS if settings.get(SETTING.format(ch))]


async def set_receiving(state, channel: str, on: bool) -> None:
    from harvey.modules import switches

    if channel not in CHANNELS:
        raise ValueError(f"no channel {channel!r}: one of {', '.join(CHANNELS)}")
    await switches.set_module_setting(state, npathi.MODULE, SETTING.format(channel), bool(on))


async def last_read(state, channel: str) -> float | None:
    try:
        return float(await state.get_setting(READ_KEY + channel) or "") or None
    except ValueError:
        return None


async def read_gap(state, channel: str) -> dict | None:
    raw = await state.get_setting(GAP_KEY + channel)
    return _json(raw) if raw else None


async def gaps(state) -> dict[str, dict]:
    """{channel: gap} for every channel whose read hasn't reached back."""
    return {ch: gap for ch in CHANNELS if (gap := await read_gap(state, ch)) is not None}


async def save_account(state, account: npathi.Account | None, senders: dict | None,
                       error: npathi.NpathiError | None = None) -> dict:
    """Keep the outcome of an account check for the card (never the key)."""
    if account is None:
        kept = _json(await state.get_setting(ACCOUNT_KEY))
        kept.update({"ok": False, "checked_at": int(time.time()),
                     "error": str(error) if error else "", "error_kind": error.kind if error else ""})
    else:
        kept = {"ok": True, "checked_at": int(time.time()), "error": "", "error_kind": "",
                "workspace": account.workspace, "plan": account.plan,
                "features": list(account.features), "abilities": list(account.abilities),
                "credits": account.credits, "key_expires_at": account.key_expires_at,
                "senders": senders or {}}
    await state.set_setting(ACCOUNT_KEY, json.dumps(kept))
    return kept


async def check(state, client: npathi.Npathi) -> dict:
    """Ask nPathi who the key is and what it may use; keep the answer.
    Raises NpathiError (kept as the card's error too)."""
    try:
        account = await client.account()
        senders = await client.channels()
    except npathi.NpathiError as e:
        await save_account(state, None, None, e)
        raise
    return await save_account(state, account, senders)


async def status(state) -> dict:
    """What the settings card and ``harvey channels status`` show: whether
    the module is on, the address, whether a key is saved, the last account
    check, and per channel its switch, whether the key may read it, its
    senders (each with whether a reply can come back to it), when it was
    last read completely, any gap and the last failure. No request is made."""
    from harvey.modules import switches

    url = await npathi.base_url(state)
    account = _json(await state.get_setting(ACCOUNT_KEY))
    on = set(await receiving(state))
    abilities = set(account.get("abilities") or ())
    senders = account.get("senders") or {}
    rows = []
    for ch in CHANNELS:
        problem = _json(await state.get_setting(PROBLEM_KEY + ch))
        rows.append({
            "id": ch, "label": npathi.LABELS[ch], "receive": ch in on,
            "can_read": (npathi.READ_ABILITY[ch] in abilities) if account.get("ok") else None,
            "senders": senders.get(ch) or [],
            "last_read_at": await last_read(state, ch),
            "gap": await read_gap(state, ch),
            "problem": problem.get("why", ""), "problem_kind": problem.get("kind", ""),
        })
    return {
        "module_on": await switches.is_enabled(state, npathi.MODULE),
        "base_url": url, "base_url_problem": npathi.base_url_problem(url) if url else "",
        "key_set": bool(npathi.key_from_env()),
        "connected": bool(account.get("ok")) and bool(url) and bool(npathi.key_from_env()),
        "account": {k: account.get(k) for k in
                    ("workspace", "plan", "features", "abilities", "credits", "key_expires_at",
                     "checked_at", "error", "error_kind", "ok")} if account else {},
        "channels": rows,
    }


async def set_base_url(state, url: str) -> str:
    """Keep the install's address (checked: https, a public host)."""
    from harvey.modules import switches

    url = npathi.normalize_base_url(url)
    problem = npathi.base_url_problem(url)
    if problem:
        raise ValueError(problem)
    await switches.set_module_setting(state, npathi.MODULE, "base_url", url)
    return url
