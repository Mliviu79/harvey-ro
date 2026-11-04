"""What the classifier's calls cost, and what its account says.

A call's cost is the reply's own when it carries one (an OpenAI-compatible
reply's ``usage.estimated_cost``, DeepInfra's), else its tokens times the
model's price: the one entered in Settings → Classifier (price per million
input and output tokens), else, for an OpenAI-compatible endpoint that is
DeepInfra's, the model's price on DeepInfra's public price list. A decision
model is priced only by the prices entered. With none of those the price is
unknown: the call is recorded at 0, marked unpriced, and a USD cap can't
count it (the call caps still do).

The price list (``GET https://api.deepinfra.com/models/list``, no key) is
kept a day in the settings table; a fetch that fails keeps the list it
had (however old), else leaves the price unknown for a few minutes, and is
never an error for a call. The same list
gives the Classifier card its models to choose from (text-generation, for
openai_compatible), sorted by price and nothing else.

The Account box reads DeepInfra's billing with the classifier's own key,
server side (this month's usage per model, the key's limit and spend, the
balance), each part on its own: a key without the billing:read scope, or
an endpoint that doesn't answer, says so for that part only. The key never
leaves the server.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger("harvey.classify")

DEEPINFRA_HOST = "api.deepinfra.com"
DEEPINFRA = "https://api.deepinfra.com"
MODELS_URL = f"{DEEPINFRA}/models/list"
CACHE_KEY = "classify:deepinfra_models"
DAY = 86400.0
# After a failed fetch of the price list, how long the price stays unknown
# before the list is asked again (a call is never held up for it).
RETRY_FAILED = 600.0
FETCH_TIMEOUT = 8.0
# How long the Account box's answers are kept (the billing endpoints are
# not for every page load).
ACCOUNT_TTL = 300.0
# The DeepInfra model type each provider is asked through (only
# openai_compatible has a price list).
KINDS = {"openai_compatible": "text-generation"}
SCOPE = "billing:read"
NO_LIST = "DeepInfra's price list didn't answer"
# Never fetch the list (the agent's read-only tools: the copy kept, else
# the price unknown).
OFFLINE = False

_failed_at = 0.0
_lock: asyncio.Lock | None = None
# The list as last read (fetched at, rows): every call prices itself, so
# the settings row isn't parsed each time.
_kept: tuple[float, list] = (0.0, [])
_account: dict[str, tuple[float, dict]] = {}


def is_deepinfra(url: str) -> bool:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower() == DEEPINFRA_HOST
    except ValueError:
        return False


def _per_m(cents_per_token, discount: float) -> float | None:
    if isinstance(cents_per_token, bool) or not isinstance(cents_per_token, (int, float)):
        return None
    # (cents a token -> USD a million tokens; DeepInfra: price * (1 - discount))
    return round(float(cents_per_token) * 1e4 * (1.0 - discount), 6)


def model_row(m: dict, now: float | None = None) -> dict | None:
    """One price-list entry as Harvey keeps it: {"model", "type", "in_per_m",
    "out_per_m" (USD a million tokens, the discount applied while it runs),
    "context", "discount"}; None for a model priced by anything but tokens,
    a private or deprecated one, or a type the classifier can't ask."""
    if not isinstance(m, dict) or m.get("type") not in KINDS.values():
        return None
    if m.get("deprecated") or m.get("private"):
        return None
    p = m.get("pricing") or {}
    if p.get("type") not in ("tokens", "input_tokens"):
        return None
    discount = p.get("discount") or 0.0
    ends = p.get("discount_ends_at")
    if not isinstance(discount, (int, float)) or not 0 <= discount < 1 or (
            isinstance(ends, (int, float)) and ends < (now or time.time())):
        discount = 0.0
    price_in = _per_m(p.get("cents_per_input_token"), discount)
    # (A model priced by input tokens only gives its output for nothing.)
    price_out = _per_m(p.get("cents_per_output_token", 0.0), discount)
    if price_in is None or price_out is None or not m.get("model_name"):
        return None
    return {"model": str(m["model_name"]), "type": m["type"], "in_per_m": price_in,
            "out_per_m": price_out, "context": m.get("max_tokens") or None,
            "discount": round(float(discount), 4)}


async def _fetch() -> list[dict]:
    from harvey import classify

    async with classify._http(FETCH_TIMEOUT) as client:
        r = await client.get(MODELS_URL)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("the price list isn't a list")
    now = time.time()
    return [row for row in (model_row(m, now) for m in data) if row]


async def models(state) -> tuple[list[dict], str]:
    """DeepInfra's priced models (model_row each) and "" — from the copy
    kept a day, else fetched now, else the copy kept before however old;
    ([], why) when there is none."""
    global _failed_at, _lock, _kept
    if _kept[1] and time.time() - _kept[0] < DAY:
        return _kept[1], ""
    try:
        kept = json.loads(await state.get_setting(CACHE_KEY) or "{}")
        at = float(kept.get("at") or 0)
    except (TypeError, ValueError, AttributeError):
        kept, at = {}, 0.0
    if time.time() - at < DAY and isinstance(kept.get("models"), list):
        _kept = (at, kept["models"])
        return kept["models"], ""
    # (A list older than a day is still the latest price known: kept while
    # a fresh one can't be had, so a known price never turns unknown, and
    # the USD caps stop counting, because DeepInfra didn't answer once.)
    stale = kept["models"] if isinstance(kept.get("models"), list) and kept["models"] \
        else _kept[1]

    def failed():
        return (stale, "") if stale else ([], NO_LIST)

    if time.time() - _failed_at < RETRY_FAILED or OFFLINE:
        return failed()
    _lock = _lock or asyncio.Lock()
    async with _lock:
        # (Another call may have fetched it, or failed, while this one waited.)
        if _kept[1] and time.time() - _kept[0] < DAY:
            return _kept[1], ""
        if time.time() - _failed_at < RETRY_FAILED:
            return failed()
        try:
            rows = await _fetch()
        except Exception as e:
            _failed_at = time.time()
            logger.info("classify: DeepInfra's price list: %s", e)
            return failed()
        at = time.time()
        await state.set_setting(CACHE_KEY, json.dumps({"at": at, "models": rows}))
        _kept = (at, rows)
    return rows, ""


async def choices(state, provider: str) -> dict:
    """The models the Classifier card offers for ``provider`` (its
    DeepInfra type), cheapest first: {"models": [...], "error": ""}."""
    kind = KINDS.get(provider)
    if not kind:
        return {"models": [], "error": "no price list for this provider"}
    rows, why = await models(state)
    picked = sorted((r for r in rows if r["type"] == kind),
                    key=lambda r: (r["in_per_m"] + r["out_per_m"], r["in_per_m"], r["model"]))
    return {"models": picked, "error": why, "kind": kind}


async def list_price(state, model: str) -> tuple[float, float] | None:
    """(input, output) USD a million tokens for ``model`` on DeepInfra's
    list, or None (not listed, or the list can't be had)."""
    rows, _ = await models(state)
    for r in rows:
        if r["model"] == model:
            return r["in_per_m"], r["out_per_m"]
    return None


async def price(state, s) -> tuple[tuple[float, float] | None, str]:
    """((input, output) USD a million tokens, where from: "entered" |
    "deepinfra"), or (None, "") when the price is unknown. Both prices
    entered win; a single one entered is no price (enter 0 for a side that
    is free)."""
    if s.price_in is not None and s.price_out is not None:
        return (s.price_in, s.price_out), "entered"
    if s.model and s.provider == "openai_compatible" and is_deepinfra(s.base_url):
        listed = await list_price(state, s.model)
        if listed:
            return listed, "deepinfra"
    return None, ""


def cost(tokens_in: int, tokens_out: int, per_m: tuple[float, float]) -> float:
    return round((tokens_in * per_m[0] + tokens_out * per_m[1]) / 1e6, 10)


# ── DeepInfra's account, read with the classifier's key ──


def _usd(cents) -> float | None:
    return round(cents / 100.0, 4) if isinstance(cents, (int, float)) and not isinstance(
        cents, bool) else None


def _usage(j) -> dict:
    months = (j or {}).get("months") or []
    period = _dt.datetime.now(_dt.timezone.utc).strftime("%Y.%m")
    month = next((m for m in months if m.get("period") == period), months[0] if months else {})
    per: dict[str, float] = {}
    for item in month.get("items") or []:
        name = ((item.get("model") or {}).get("model_name")) or "?"
        per[name] = per.get(name, 0.0) + (item.get("cost") or 0)
    return {"period": month.get("period") or period, "total_usd": _usd(month.get("total_cost") or 0),
            "models": sorted(({"model": k, "usd": _usd(v)} for k, v in per.items()),
                             key=lambda r: (-(r["usd"] or 0), r["model"]))}


def _limits(j) -> dict:
    keys = [{"name": str(k.get("name") or ""), "limit_usd": k.get("limit"),
             "spend_usd": k.get("spend")} for k in (j or []) if isinstance(k, dict)]
    return {"keys": keys}


def _balance(j) -> dict:
    j = j or {}
    bal = j.get("stripe_balance")
    # (Negative: funds ready to spend; positive: money owed. Only these
    # fields: the checklist also holds a billing address.)
    return {"credit_usd": round(-bal, 4) if isinstance(bal, (int, float)) and bal < 0 else 0.0,
            "owed_usd": round(bal, 4) if isinstance(bal, (int, float)) and bal > 0 else 0.0,
            "since_invoice_usd": j.get("recent"), "suspended": bool(j.get("suspended"))}


PARTS = {"usage": ("/payment/usage?from=current&to=current", _usage),
         "limits": ("/payment/key-limits", _limits),
         "balance": ("/payment/checklist", _balance)}


async def _part(client, path: str, read) -> dict:
    try:
        r = await client.get(DEEPINFRA + path)
    except Exception as e:
        logger.info("classify: DeepInfra %s: %s", path, e)
        return {"ok": False, "code": "no_answer", "error": "DeepInfra didn't answer"}
    if r.status_code in (401, 403):
        return {"ok": False, "code": "scope", "scope": SCOPE,
                "error": f"the key can't read this: it needs the {SCOPE} scope"}
    if r.status_code != 200:
        return {"ok": False, "code": "status", "status": r.status_code,
                "error": f"DeepInfra answered {r.status_code}"}
    try:
        return {"ok": True, **read(r.json())}
    except Exception:
        return {"ok": False, "code": "unreadable", "error": "an answer Harvey can't read"}


async def account(s, *, refresh: bool = False) -> dict:
    """DeepInfra's billing for the classifier's key: {"available", "why",
    "usage", "limits", "balance"}, each part {"ok", "error", ...}. Kept
    ACCOUNT_TTL seconds; never raises, never holds the key."""
    from harvey import classify

    if not is_deepinfra(s.base_url):
        return {"available": False, "code": "not_deepinfra",
                "why": "the endpoint isn't DeepInfra's"}
    key = classify._key(s)
    if not key:
        return {"available": False, "code": "no_key",
                "why": f"{s.key_name} holds no value in .env"}
    kept = _account.get(s.key_name)
    if kept and not refresh and time.time() - kept[0] < ACCOUNT_TTL:
        return kept[1]
    out = {"available": True, "why": ""}
    try:
        async with classify._http(15.0, headers={"Authorization": f"Bearer {key}"}) as client:
            for name, (path, read) in PARTS.items():
                out[name] = await _part(client, path, read)
    except Exception as e:
        logger.info("classify: DeepInfra's account: %s", e)
        for name in PARTS:
            out.setdefault(name, {"ok": False, "code": "no_answer",
                                  "error": "DeepInfra didn't answer"})
    _account[s.key_name] = (time.time(), out)
    return out
