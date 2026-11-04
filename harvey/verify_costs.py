"""What address checks and Hunter's pattern lookups cost, counted as asked.

A check (email_verify's find_email) asks one or more verification services
in turn (harvey/integrations/email_finder.py: ZeroBounce, Reoon, Hunter;
the SMTP check is free), and may look the domain's pattern up with Hunter's
domain search first. Each request is one at that service: what its free
allowance and its bill count. Each service's manifest declares, under
``[cost.verifiers.<id>]``, the requests it gives free every month
(``free_per_month``) and, when known, what one past them costs in USD
(``usd``); ``kind = "search"`` marks a pattern lookup rather than a check.
None declares a price today: the user enters what their plan charges, the
service's module setting ``usd_<id>`` (Settings -> Email verification, or
``harvey modules set``), and an entered price wins over a declared one.

A request is $0 inside the service's free allowance and its price past
it. Past it with no price, it is refused when a USD cap applies (a run's
cap, or spend.per_day_usd / per_month_usd for background work): a cap
can't stop what it can't count. With no USD cap anywhere (the Email
module's audience) it is made and counted unpriced, and said so.

The month's requests are counted per service in the settings table,
whoever asked (the contact finder, the audience); the month is the
calendar month (UTC). EMAIL_VERIFY_MONTHLY_CHECKS (email_verify's
``monthly_searches``) caps the checks of every service together, so it
is the sum of those counters; Hunter's domain searches share the hunter
module's own allowance counter (HUNTER_MONTHLY_SEARCHES).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from harvey.modules import api

logger = logging.getLogger("harvey.verify_costs")

USES_KEY = "verify_uses:{verifier}:{month}"
# The module setting a service's entered price is kept in.
PRICE_SETTING = "usd_{}"
# Hunter's domain search: one of the hunter module's monthly allowance
# (ingest.Allowance), counted on that allowance's counter.
HUNTER_SEARCH = "hunter_search"
SHARED = {HUNTER_SEARCH: "hunter"}
# The module whose monthly_searches caps the checks, all services together.
CHECKS_MODULE = "email_verify"
# The most one request may be entered at, in USD: services charge cents,
# so more is a typo (a plan's monthly price, or cents typed as dollars).
MAX_USD = 10.0


def verifiers() -> dict[str, dict]:
    """{service id: {"module", "kind" ("check" or "search"),
    "free_per_month", "usd" (None: no price declared)}} from every
    manifest's ``[cost.verifiers]``."""
    from harvey.modules import switches

    out: dict[str, dict] = {}
    for m in switches.manifests():
        for vid, spec in ((m.cost or {}).get("verifiers") or {}).items():
            if not isinstance(spec, dict):
                continue
            usd = spec.get("usd")
            free = spec.get("free_per_month")
            out[str(vid)] = {
                "module": m.id,
                "kind": "search" if spec.get("kind") == "search" else "check",
                "free_per_month": int(free) if isinstance(free, int)
                and not isinstance(free, bool) and free > 0 else 0,
                "usd": _usd(usd)}
    return out


def _usd(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and 0 <= value <= MAX_USD else None


async def prices(state) -> dict[str, dict]:
    """verifiers() with each service's entered price (``entered``, its
    module's usd_<id> setting; None when blank) in place of the declared
    one (``declared``)."""
    from harvey.modules import switches

    specs = verifiers()
    settings: dict[str, dict] = {}
    for vid, spec in specs.items():
        if spec["module"] not in settings:
            try:
                settings[spec["module"]] = await switches.module_settings(state, spec["module"])
            except Exception as e:
                logger.debug("verify prices read failed: %s", e)
                settings[spec["module"]] = {}
        entered = _usd(settings[spec["module"]].get(PRICE_SETTING.format(vid)))
        spec["declared"], spec["entered"] = spec["usd"], entered
        if entered is not None:
            spec["usd"] = entered
    return specs


async def set_price(state, service: str, usd) -> float | None:
    """Enter (or, with None or "", clear) what one request to ``service``
    past its free allowance costs. ValueError for a service no manifest
    declares or a price that isn't a number of dollars from 0 to MAX_USD."""
    from harvey.modules import switches

    spec = verifiers().get(service)
    if spec is None:
        raise ValueError(f"no verification service {service!r}")
    if usd is not None and usd != "":
        try:
            usd = float(usd)
        except (TypeError, ValueError):
            raise ValueError(f"{service}: the price must be a number of dollars") from None
        if not 0 <= usd <= MAX_USD:
            raise ValueError(f"{service}: the price must be from 0 to ${MAX_USD:g} "
                             "(one request, in dollars)")
    return await switches.set_module_setting(state, spec["module"],
                                             PRICE_SETTING.format(service), usd)


def _month() -> str:
    return f"{datetime.now(timezone.utc):%Y-%m}"


def _key(verifier: str) -> str:
    if verifier in SHARED:
        from harvey.modules.ingest import ALLOWANCE_KEY

        return ALLOWANCE_KEY.format(module=SHARED[verifier], month=_month())
    return USES_KEY.format(verifier=verifier, month=_month())


async def _count(state, key: str) -> int:
    try:
        return int(await state.get_setting(key, "0") or "0")
    except (TypeError, ValueError):
        return 0
    except Exception as e:
        logger.debug("verify uses read failed: %s", e)
        return 0


async def used(state, verifier: str) -> int:
    """Requests ``verifier`` was asked this month."""
    return await _count(state, _key(verifier))


async def checks_used(state, specs: dict | None = None) -> int:
    """The checks every verification service was asked this month, plus
    what the counter of the checks themselves held before each service was
    counted (an install upgraded mid-month)."""
    from harvey.modules.ingest import ALLOWANCE_KEY

    specs = specs if specs is not None else verifiers()
    n = sum([await used(state, v) for v, s in specs.items() if s["kind"] == "check"])
    return n + await _count(state, ALLOWANCE_KEY.format(module=CHECKS_MODULE, month=_month()))


async def checks_left(state, specs: dict | None = None) -> int:
    """What is left this month of EMAIL_VERIFY_MONTHLY_CHECKS."""
    from harvey.modules.ingest import monthly_allowance

    return max(monthly_allowance(CHECKS_MODULE) - await checks_used(state, specs), 0)


def _price(spec: dict | None, used_before: int) -> tuple[float, bool]:
    """(USD, priced) of one request after ``used_before`` this month."""
    if spec is None:
        return 0.0, False
    if used_before < spec["free_per_month"]:
        return 0.0, True
    return (spec["usd"], True) if spec["usd"] is not None else (0.0, False)


def no_price(service: str) -> str:
    return f"no price for {service} past its free tier"


def _bucket(spec: dict | None) -> str:
    # (Spend.by and the run's report: address checks, pattern lookups.)
    return "lookups" if spec and spec["kind"] == "search" else "checks"


class Meter(api.Budget):
    """The requests one run (or one audience check) makes to verification
    services and Hunter's domain search, each counted, priced and allowed
    as it is about to go out (api.Budget: email_finder calls ``ask`` before
    each service and ``take("hunter")`` before a domain search).

    ``spend``: the run's finder.Spend, whose cap (``spend.cap``) makes a
    request past its free allowance with no price refused, and from which
    a priced one is reserved before and recorded after; None: no USD cap
    (counted, priced where a price is known, made either way). ``made``,
    ``usd`` and ``unpriced`` per service; ``refused``: (service, why) for
    each request not made, in order (``mark`` gives where a check's own
    begin)."""

    def __init__(self, state, specs: dict, spend=None):
        self.state = state
        self.specs = specs
        self.spend = spend
        self.made: dict[str, int] = {}
        self.usd: dict[str, float] = {}
        self.unpriced: dict[str, int] = {}
        self.refused: list[tuple[str, str]] = []

    @classmethod
    async def start(cls, state, spend=None) -> "Meter":
        return cls(state, await prices(state), spend)

    @property
    def capped(self) -> bool:
        return self.spend is not None and bool(getattr(self.spend, "cap", False))

    def mark(self) -> int:
        return len(self.refused)

    def spent(self) -> float:
        return round(sum(self.usd.values()), 6)

    def total(self, kind: str = "check") -> tuple[int, float, int]:
        """(requests made, USD, unpriced) of the services of ``kind``."""
        ids = [v for v, s in self.specs.items() if s["kind"] == kind]
        return (sum(self.made.get(v, 0) for v in ids),
                round(sum(self.usd.get(v, 0.0) for v in ids), 6),
                sum(self.unpriced.get(v, 0) for v in ids))

    async def refusal(self, service: str) -> str:
        """Why a request to ``service`` wouldn't be made now, or "" (read
        only: the month's caps and the price; the run's money is asked for
        when the request is made)."""
        from harvey.modules.ingest import Allowance

        spec = self.specs.get(service)
        if spec is not None and spec["kind"] == "search":
            if await Allowance(self.state).left(SHARED[service]) <= 0:
                return ("Hunter's domain searches for this month are used "
                        "(HUNTER_MONTHLY_SEARCHES)")
        elif await checks_left(self.state, self.specs) <= 0:
            return "this month's address checks are used (EMAIL_VERIFY_MONTHLY_CHECKS)"
        _, priced = _price(spec, await used(self.state, service))
        if not priced and self.capped:
            return no_price(service)
        return ""

    async def ask(self, service: str) -> bool:
        """Count one request to ``service`` and say whether it may be made:
        refused (nothing counted, the reason kept) when a month's cap is
        reached, past the free allowance with no price under a USD cap, or
        when the run's money can't pay for it."""
        spec = self.specs.get(service)
        why = await self.refusal(service)
        if why:
            self.refused.append((service, why))
            return False
        key = _key(service)
        try:
            # (The count after this one, read and written in one statement:
            # two requests at once never both take the last free one.)
            before = await self.state.increment_setting(key) - 1
        except Exception as e:
            logger.debug("verify uses write failed: %s", e)
            if self.capped:
                # Uncounted, it can't be known free: a cap refuses it.
                self.refused.append((service, "this month's requests couldn't be counted"))
                return False
            return True
        cost, priced = _price(spec, before)
        why = await self._over_month(service, spec, before)
        if not why and not priced and self.capped:
            why = no_price(service)
        if not why and cost > 0 and self.spend is not None \
                and await self.spend.allow(cost) is None:
            why = self.spend.refused
        if why:
            await self.state.increment_setting(key, -1)
            self.refused.append((service, why))
            return False
        self.made[service] = self.made.get(service, 0) + 1
        if not priced:
            self.unpriced[service] = self.unpriced.get(service, 0) + 1
            logger.info("%s asked past its free tier with no price entered: counted, "
                        "not priced (no USD cap applies)", service)
        elif cost:
            self.usd[service] = round(self.usd.get(service, 0.0) + cost, 6)
            if self.spend is not None:
                await self.spend.record(cost, _bucket(spec))
        return True

    async def _over_month(self, service: str, spec: dict | None, before: int) -> str:
        """Why the request just counted (``before`` this month at its
        service) is past a month's cap after all, or "": two asked at once
        both read one left before either was counted."""
        from harvey.modules.ingest import monthly_allowance

        if spec is not None and spec["kind"] == "search":
            if before >= monthly_allowance(SHARED[service]):
                return ("Hunter's domain searches for this month are used "
                        "(HUNTER_MONTHLY_SEARCHES)")
        elif await checks_used(self.state, self.specs) > monthly_allowance(CHECKS_MODULE):
            return "this month's address checks are used (EMAIL_VERIFY_MONTHLY_CHECKS)"
        return ""

    # api.Budget: Hunter's domain search is drawn with take("hunter").
    async def left(self, what: str) -> int:
        from harvey.modules.ingest import Allowance

        return await Allowance(self.state).left(what)

    async def take(self, what: str) -> bool:
        if what == SHARED[HUNTER_SEARCH]:
            return await self.ask(HUNTER_SEARCH)
        from harvey.modules.ingest import Allowance

        return await Allowance(self.state).take(what)

    def as_dict(self) -> dict:
        return {"made": dict(self.made), "usd": dict(self.usd),
                "unpriced": dict(self.unpriced),
                "refused": [f"{s}: {why}" for s, why in self.refused]}


async def projection(state, wants: dict[str, int], *, capped: bool = True) -> dict:
    """What ``wants`` ({service: requests at most}) may cost if every one
    is made: per service, the requests past what is left of its free
    allowance this month at its price (entered, else declared).
    ``refused``: the services some of those would be past their free
    allowance at with no price, refused under a USD cap (``capped``; with
    none, ``unpriced``: made, not counted in USD). ``checks_left``: what is
    left of EMAIL_VERIFY_MONTHLY_CHECKS, every service's checks together.
    An estimate: the caps are enforced request by request."""
    specs = await prices(state)
    out = {"usd_high": 0.0, "refused": [], "unpriced": [], "free_left": {}, "prices": {},
           "checks_left": await checks_left(state, specs)}
    for name, n in wants.items():
        spec = specs.get(name)
        if spec is None:
            out["refused" if capped else "unpriced"].append(name)
            continue
        left = max(0, spec["free_per_month"] - await used(state, name))
        out["free_left"][name] = left
        out["prices"][name] = spec["usd"]
        past = max(0, int(n) - left)
        if not past:
            continue
        if spec["usd"] is None:
            out["refused" if capped else "unpriced"].append(name)
        else:
            out["usd_high"] = round(out["usd_high"] + past * spec["usd"], 6)
    return out


async def status(state) -> dict:
    """Every service this month, for Settings and the CLI: its module,
    kind, free allowance, requests made, how many of them were past the
    allowance, and its price (entered, declared); with the month's checks
    against EMAIL_VERIFY_MONTHLY_CHECKS."""
    from harvey.modules.ingest import monthly_allowance

    specs = await prices(state)
    rows = []
    for vid, spec in specs.items():
        n = await used(state, vid)
        rows.append({"id": vid, "module": spec["module"], "kind": spec["kind"],
                     "free_per_month": spec["free_per_month"], "used": n,
                     "past_free": max(0, n - spec["free_per_month"]),
                     "entered": spec["entered"], "declared": spec["declared"],
                     "usd": spec["usd"]})
    return {"services": rows, "month": _month(),
            "checks": {"cap": monthly_allowance(CHECKS_MODULE),
                       "used": await checks_used(state, specs)}}
