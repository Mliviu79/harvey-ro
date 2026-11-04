"""Classifier profiles: one complete setup each, and which task uses which.

A profile is everything one classifier needs: its kind (openai_compatible,
anthropic, decision, brain), endpoint, model, the name of the .env key
that holds its key (never the key), extra request fields (a small JSON
object merged into the request body: {"reasoning_effort": "none"} turns a
reasoning model's thinking off), JSON mode, pace, timeout, prices, the
tasks it answers, and its questions per task as data: for a language
model each task's prompt and its yes and no texts; for a decision model
the site questions' texts and bars and the contacts question's options
and bar. Code keeps the structure (the statement and question ids, what
an answer may name, how answers combine): only texts and bars change, and
bars stay inside their ranges.

Built-in profiles ship in reference/classifier_profiles.json, read-only,
with what Harvey's judge test measured on them (counts). They are only the
setups that tested well; none is put above another. Editing one
makes a copy of your own ("based on" it); a built-in can be hidden and
shown again. Your own profiles (settings table, ``classify:profiles``)
can be added, edited and removed. A profile of your own is "not tested"
until a test run records results on it, and again after any change to
what it asks (its endpoint, model, extra fields or questions).

Each task (results, site, contacts) is set to a profile that can answer it,
or off (``classify:use``). The install's caps stay the contact finder's
settings: calls a day, each task's day cap, the USD caps a day and a month.

A profile is the only place a classifier's setup lives. An install whose
older ``classify_*`` settings name a provider (an earlier Harvey kept its
one classifier there) gets that setup once as a profile of its own,
``settings``, set for every task it answered (migrate_legacy); those
settings are never read again after that.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from importlib import resources

KINDS = ("openai_compatible", "anthropic", "decision", "brain")
BUILTIN_FILE = "reference/classifier_profiles.json"
PROFILES_KEY = "classify:profiles"
HIDDEN_KEY = "classify:hidden"
USE_KEY = "classify:use"
TESTS_KEY = "classify:tests"
LEGACY_KEY = "classify:legacy"
LEGACY_ID = "settings"
# Where the older settings were kept (the contact finder's, by name), and
# the key name they used when none was set.
LEGACY_PREFIX = "modules.contact_finder.classify_"
LEGACY_KEY_NAME = "CLASSIFY_API_KEY"
OFF = "off"
# Test runs kept per profile.
TESTS_KEPT = 10
MAX_PROFILES = 50

# Fields a profile's extra request body may never set: the request's own.
RESERVED = frozenset({
    "model", "messages", "max_tokens", "max_completion_tokens", "response_format",
    "temperature", "stream", "n", "tools", "tool_choice", "functions", "function_call",
    "system", "state", "questions", "stop", "logprobs", "top_logprobs", "seed", "user",
    "extra_body", "extra_headers"})
_EXTRA_KEY = re.compile(r"[a-z][a-z0-9_]{0,40}")
MAX_EXTRA = 8
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,59}")
TEXT_MAX = 2000

# What each task is for and when it is asked, for the Classifier card.
TASK_DOCS = {
    "results": {
        "label": "Search results",
        "what": "Which of a web search's results could be the company's own website, "
                "before anything is visited (one request for all the results).",
        "when": "When the contact finder looks a company with no website up by its name and "
                "town, and two or more results pass the address checks.",
        "off": "The three results whose title best matches the company's name are read.",
    },
    "site": {
        "label": "Website",
        "what": "Whether one website read is the company's own (one request per "
                "candidate, each asked alone).",
        "when": "After a search found candidates no register fact settles (the firm's CUI, "
                "registration number, an official phone or email on its pages), and when "
                "found websites are checked again (harvey contacts rejudge).",
        "off": "A searched site no register fact confirms is kept unlinked, waiting for a "
               "check.",
    },
    "contacts": {
        "label": "Contacts on the site",
        "what": "Which phones, emails and profiles read on the chosen site are the "
                "company's own (not a web agency's footer phone, not an office abroad); "
                "and, asked about one contact, a verification: yes, no or unsure.",
        "when": "On a site chosen for a company, before its contacts are linked; and when "
                "you verify contacts (Contacts, or harvey contacts verify).",
        "off": "On a site the model chose, only the contacts an official source gives the "
               "firm are linked; on one a register fact confirmed, every contact read there.",
    },
}

_builtin_cache: dict | None = None


# ── The built-in file ──


def catalog() -> dict:
    """The built-in file as shipped (a copy): its question sets, the keys'
    notes (where to make one, the scopes it needs) and the profiles."""
    global _builtin_cache
    if _builtin_cache is None:
        text = resources.files("harvey").joinpath(BUILTIN_FILE).read_text(encoding="utf-8")
        _builtin_cache = json.loads(text)
    return copy.deepcopy(_builtin_cache)


def builtins() -> dict[str, dict]:
    """The built-in profiles by id, each complete (its question set filled
    in) and marked ``builtin``."""
    data = catalog()
    sets = data.get("question_sets") or {}
    out = {}
    for p in data.get("profiles") or []:
        p = dict(p)
        q = p.get("questions")
        if isinstance(q, str):
            p["questions"] = copy.deepcopy(sets.get(q) or {})
        p["builtin"] = True
        p.setdefault("measured", None)
        out[p["id"]] = normalise(p)
    return out


def key_notes(key_name: str) -> dict | None:
    """What the built-in file says of a key (its provider, where it is
    made, the scopes it needs and which are off on a new key)."""
    return (catalog().get("keys") or {}).get(key_name)


# ── One profile ──


def kind_tasks(kind: str) -> tuple[str, ...]:
    """The tasks a kind can answer: a decision model only those with
    decision questions (no search-results design)."""
    from harvey import classify

    if kind == "decision":
        return tuple(n for n, t in classify.TASKS.items() if t.decision is not None)
    if kind in KINDS:
        return tuple(classify.TASKS)
    return ()


def default_questions(kind: str) -> dict:
    """The measured texts for every task ``kind`` can answer."""
    from harvey import classify, classify_decision

    if kind == "decision":
        return classify_decision.default_questions()
    return {n: {"prompt": t.prompt, "yes": t.yes, "no": t.no}
            for n, t in classify.TASKS.items()}


def extra_problem(extra) -> str:
    """Why ``extra`` can't be a profile's extra request fields ("" when it
    can): a JSON object of at most MAX_EXTRA plain keys (lower case
    letters, digits, _) whose values are text, a number, true/false or
    null, never one of the request's own fields (RESERVED)."""
    if extra is None:
        return ""
    if not isinstance(extra, dict):
        return "request_extra is a JSON object"
    if len(extra) > MAX_EXTRA:
        return f"request_extra holds at most {MAX_EXTRA} fields"
    for k, v in extra.items():
        if not isinstance(k, str) or not _EXTRA_KEY.fullmatch(k):
            return f"request_extra: {str(k)[:40]!r} isn't a plain field name"
        if k in RESERVED:
            return f"request_extra can't set {k}: the request sets it"
        if not (v is None or isinstance(v, (bool, int, float)) or
                (isinstance(v, str) and len(v) <= 100)):
            return f"request_extra: {k} is text (100 characters at most), a number, " \
                   "true/false or null"
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return f"request_extra: {k} isn't a number"
    return ""


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} is text")
    value = value.strip()
    if len(value) > TEXT_MAX:
        raise ValueError(f"{what} is {TEXT_MAX} characters at most")
    return value


def _bar(value, what: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{what} is a number from {lo} to {hi}")
    return round(min(hi, max(lo, float(value))), 2)


def check_questions(kind: str, tasks, raw) -> dict:
    """A profile's questions checked against code's structure, texts
    trimmed and bars clamped; a task not given gets the measured ones.
    ValueError for a shape code doesn't have (an id it doesn't ask, an
    option it doesn't read)."""
    from harvey import classify, classify_decision as d

    raw = raw if isinstance(raw, dict) else {}
    if any(k not in classify.TASKS for k in raw):
        raise ValueError("questions are for " + ", ".join(classify.TASKS))
    base = default_questions(kind)
    out = {}
    for task in tasks:
        given = raw.get(task)
        q = copy.deepcopy(base.get(task) or {})
        if given is None:
            out[task] = q
            continue
        if not isinstance(given, dict):
            raise ValueError(f"questions.{task} is an object")
        if kind != "decision":
            if set(given) - {"prompt", "yes", "no"}:
                raise ValueError(f"questions.{task}: prompt, yes and no only")
            for k in ("prompt", "yes", "no"):
                if k in given:
                    q[k] = _text(given[k], f"questions.{task}.{k}")
        elif task == "site":
            if set(given) - {"questions", "needs", "vetoes"}:
                raise ValueError("questions.site: questions, needs and vetoes only")
            if any(not isinstance(given.get(k) or {}, dict)
                   for k in ("questions", "needs", "vetoes")):
                raise ValueError("questions.site: questions, needs and vetoes are objects")
            for qid, item in (given.get("questions") or {}).items():
                if qid not in q["questions"] or not isinstance(item, dict) \
                        or set(item) - {"instructions", "criteria"}:
                    raise ValueError(f"questions.site.questions: {', '.join(q['questions'])}, "
                                     "each its instructions and criteria")
                if "instructions" in item:
                    q["questions"][qid]["instructions"] = _text(
                        item["instructions"], f"questions.site.{qid}.instructions")
                if "criteria" in item:
                    crit = item["criteria"]
                    if crit in (None, {}):
                        q["questions"][qid].pop("criteria", None)
                    elif not isinstance(crit, dict) or set(crit) - {"true", "false"}:
                        raise ValueError(f"questions.site.{qid}.criteria: true and false")
                    else:
                        q["questions"][qid]["criteria"] = {
                            k: _text(v, f"questions.site.{qid}.criteria.{k}")
                            for k, v in crit.items()}
            for part in ("needs", "vetoes"):
                for k, v in (given.get(part) or {}).items():
                    if k not in q[part]:
                        raise ValueError(f"questions.site.{part}: {', '.join(q[part])}")
                    q[part][k] = _bar(v, f"questions.site.{part}.{k}", *d.BAR_RANGE)
        else:
            if set(given) - {"instructions", "options", "bar"}:
                raise ValueError(f"questions.{task}: instructions, options and bar only")
            if not isinstance(given.get("options") or {}, dict):
                raise ValueError(f"questions.{task}.options is an object")
            if "instructions" in given:
                text = _text(given["instructions"], f"questions.{task}.instructions")
                if "{n}" not in text:
                    raise ValueError(f"questions.{task}.instructions names the contact as {{n}}")
                q["instructions"] = text
            for k, v in (given.get("options") or {}).items():
                if k not in q["options"]:
                    raise ValueError(f"questions.{task}.options: {', '.join(q['options'])}")
                q["options"][k] = _text(v, f"questions.{task}.options.{k}")
            if "bar" in given:
                q["bar"] = _bar(given["bar"], f"questions.{task}.bar", *classify.YES_RANGE)
        out[task] = q
    return out


def normalise(p: dict) -> dict:
    """A profile with every field, as stored (no checks: check() is for
    what a person sends)."""
    kind = p.get("kind") if p.get("kind") in KINDS else "openai_compatible"
    can = kind_tasks(kind)
    tasks = [t for t in (p.get("tasks") or can) if t in can]
    prices = p.get("prices") or {}
    out = {"id": p["id"], "name": p.get("name") or p["id"], "kind": kind,
           "base_url": p.get("base_url") or "", "model": p.get("model") or "",
           "key_name": p.get("key_name") or "", "request_extra": dict(p.get("request_extra") or {}),
           "json_mode": bool(p.get("json_mode", True)),
           "per_minute": int(p.get("per_minute") or 20),
           "timeout_seconds": int(p.get("timeout_seconds") or 30),
           "prices": {"in_per_m": prices.get("in_per_m"), "out_per_m": prices.get("out_per_m"),
                      "from_list": bool(prices.get("from_list"))},
           "tasks": tasks, "questions": p.get("questions") or default_questions(kind),
           "notes": p.get("notes") or "", "builtin": bool(p.get("builtin")),
           "based_on": p.get("based_on") or "", "measured": p.get("measured"),
           "tested_hash": p.get("tested_hash") or "",
           "created_at": p.get("created_at"), "updated_at": p.get("updated_at")}
    if p.get("setup"):
        out["setup"] = p["setup"]
    return out


def check(raw: dict, *, base: dict | None = None) -> dict:
    """A profile a person sends, checked and filled from ``base`` (the one
    it changes); ValueError says what is wrong. The key is named, never
    given."""
    from harvey import classify

    if not isinstance(raw, dict):
        raise ValueError("a profile is a JSON object")
    known = {"name", "kind", "base_url", "model", "key_name", "request_extra", "json_mode",
             "per_minute", "timeout_seconds", "prices", "tasks", "questions", "notes"}
    extra = set(raw) - known
    if extra:
        raise ValueError("unknown fields: " + ", ".join(sorted(extra)))
    p = copy.deepcopy(base) if base else {"kind": "openai_compatible", "json_mode": True,
                                          "per_minute": 20, "timeout_seconds": 30,
                                          "prices": {}, "request_extra": {}}
    kind_changed = "kind" in raw and raw["kind"] != p.get("kind")
    p.update({k: v for k, v in raw.items() if k != "questions"})
    if p.get("kind") not in KINDS:
        raise ValueError("kind is one of " + ", ".join(KINDS))
    name = p.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("name is text, 80 characters at most")
    p["name"] = name.strip()
    for k in ("base_url", "model", "key_name", "notes"):
        if not isinstance(p.get(k) or "", str):
            raise ValueError(f"{k} is text")
        p[k] = (p.get(k) or "").strip()
    if len(p["model"]) > 120 or len(p["base_url"]) > 300 or len(p["notes"]) > 1000:
        raise ValueError("model (120), base_url (300) or notes (1000) is too long")
    why = classify.base_url_problem(p["base_url"])
    if why:
        raise ValueError(why)
    if p["key_name"]:
        why = classify.key_name_problem(p["key_name"])
        if why:
            raise ValueError(f"key_name is the name of a .env key, never the key: {why}")
    if p["kind"] == "decision" and not (p["base_url"] and p["model"] and p["key_name"]):
        raise ValueError("a decision model needs its full endpoint URL, its model and its "
                         "key's name")
    if p["kind"] == "openai_compatible" and not p["model"]:
        raise ValueError("an OpenAI-compatible endpoint needs a model")
    if p["kind"] == "anthropic" and not (p["model"] and p["key_name"]):
        raise ValueError("a Claude model needs its model id and its key's name")
    why = extra_problem(p.get("request_extra"))
    if why:
        raise ValueError(why)
    p["request_extra"] = dict(p.get("request_extra") or {})
    p["json_mode"] = bool(p.get("json_mode", True))
    for k, lo, hi in (("per_minute", 1, 600), ("timeout_seconds", 5, 600)):
        v = p.get(k)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{k} is a whole number from {lo} to {hi}")
        p[k] = min(hi, max(lo, v))
    prices = p.get("prices") or {}
    if not isinstance(prices, dict) or set(prices) - {"in_per_m", "out_per_m", "from_list"}:
        raise ValueError("prices: in_per_m and out_per_m (USD a million tokens), or "
                         "from_list")
    fixed = {}
    for k in ("in_per_m", "out_per_m"):
        v = prices.get(k)
        fixed[k] = None if v in (None, "") else classify.money_setting(v)
        if v not in (None, "") and (fixed[k] is None or isinstance(v, bool)):
            raise ValueError(f"prices.{k} is USD of at least 0, or null")
    fixed["from_list"] = bool(prices.get("from_list"))
    if fixed["from_list"] and not (p["kind"] == "openai_compatible" and _deepinfra(p)):
        raise ValueError("prices from the provider's list are DeepInfra's chat models' only")
    p["prices"] = fixed
    can = kind_tasks(p["kind"])
    tasks = p.get("tasks") if "tasks" in raw or (base and not kind_changed) else list(can)
    if not isinstance(tasks, list) or not tasks or any(t not in can for t in tasks):
        raise ValueError(f"tasks: some of {', '.join(can)} (a {p['kind']} profile)")
    p["tasks"] = [t for t in can if t in tasks]
    old_q = {} if kind_changed else (p.get("questions") or {})
    given = raw.get("questions")
    merged = {t: old_q.get(t) for t in p["tasks"] if old_q.get(t)}
    if given is not None:
        if not isinstance(given, dict):
            raise ValueError("questions is an object, per task")
        merged.update(given)
    p["questions"] = check_questions(p["kind"], p["tasks"], merged)
    return p


def _deepinfra(p: dict) -> bool:
    from harvey import classify_prices

    return classify_prices.is_deepinfra(p.get("base_url") or "")


def asks_hash(p: dict) -> str:
    """What a profile asks and of whom: a test's results hold for it only
    while this is the same."""
    what = {k: p.get(k) for k in ("kind", "base_url", "model", "request_extra", "json_mode",
                                  "questions")}
    return hashlib.sha256(json.dumps(what, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()[:16]


def tested(p: dict) -> bool:
    """A built-in is measured; one of your own is tested while what it
    asks is what its last test run asked."""
    if p.get("builtin"):
        return bool(p.get("measured"))
    return bool(p.get("tested_hash")) and p["tested_hash"] == asks_hash(p)


def settings_of(p: dict, base) -> "object":
    """``p`` as classify.Settings, with the install's caps from ``base``
    (classify.install_caps)."""
    from harvey import classify

    prices = p.get("prices") or {}
    q = p.get("questions") or {}
    yes = (q.get("contacts") or {}).get("bar") if p["kind"] == "decision" else None
    return classify.Settings(
        provider=p["kind"], base_url=p.get("base_url") or "", model=p.get("model") or "",
        key_name=p.get("key_name") or "", json_mode=bool(p.get("json_mode")),
        per_day=base.per_day, per_minute=int(p.get("per_minute") or 0),
        timeout=float(p.get("timeout_seconds") or 30), tasks=_caps_only(base.tasks),
        yes=classify.yes_setting(yes if yes is not None else classify.YES_DEFAULT),
        price_in=None if prices.get("from_list") else prices.get("in_per_m"),
        price_out=None if prices.get("from_list") else prices.get("out_per_m"),
        usd_per_day=base.usd_per_day, usd_per_month=base.usd_per_month,
        profile=p["id"], profile_name=p.get("name") or "",
        request_extra=dict(p.get("request_extra") or {}), questions=q,
        answers=tuple(p.get("tasks") or ()))


def _caps_only(tasks: dict) -> dict:
    """The install's per-task day caps, without the older settings' on/off
    switches: which tasks are asked is ``classify:use`` now (those switches
    were taken into it once, migrate_legacy), so a task set to a profile is
    on."""
    return {t: {k: v for k, v in spec.items() if k != "on"}
            for t, spec in (tasks or {}).items() if isinstance(spec, dict)}


def off_settings(base):
    from harvey import classify

    return classify.Settings(per_day=base.per_day, tasks=dict(base.tasks),
                             usd_per_day=base.usd_per_day, usd_per_month=base.usd_per_month,
                             profile=OFF)


# ── Stored ──


async def _load(state, key: str, default):
    try:
        value = json.loads(await state.get_setting(key) or "null")
    except (TypeError, ValueError):
        value = None
    return default if value is None or not isinstance(value, type(default)) else value


async def _save(state, key: str, value) -> None:
    await state.set_setting(key, json.dumps(value, ensure_ascii=False))


async def own(state) -> dict[str, dict]:
    """Your own profiles by id."""
    stored = await _load(state, PROFILES_KEY, {})
    out = {}
    for pid, p in stored.items():
        if isinstance(p, dict) and _ID.fullmatch(str(pid)):
            p["id"] = pid
            p["builtin"] = False
            out[pid] = normalise(p)
    return out


async def hidden(state) -> list[str]:
    return [h for h in await _load(state, HIDDEN_KEY, []) if isinstance(h, str)]


async def every(state, *, with_hidden: bool = False) -> dict[str, dict]:
    """Every profile by id: the built-ins (not hidden, unless
    ``with_hidden``), then yours."""
    gone = set() if with_hidden else set(await hidden(state))
    out = {k: v for k, v in builtins().items() if k not in gone}
    out.update(await own(state))
    return out


def _flag(raw, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("on", "true", "1", "yes")


def _number(raw, default, kind=int):
    try:
        return kind(str(raw).strip()) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _legacy(state) -> dict | None:
    """The single setup an earlier Harvey kept in the contact finder's
    ``classify_*`` settings, read as stored (they are no setting of this
    Harvey's any more); None when they name no provider."""
    from harvey import classify

    stored = await state.settings_with_prefix(LEGACY_PREFIX)
    raw = {k[len(LEGACY_PREFIX):]: v for k, v in stored.items() if k.startswith(LEGACY_PREFIX)}
    provider = (raw.get("provider") or "").strip()
    if provider not in KINDS:
        return None
    tasks = classify.tasks_setting(raw.get("tasks"))
    return {"provider": provider, "base_url": (raw.get("base_url") or "").strip(),
            "model": (raw.get("model") or "").strip(),
            "key_name": (raw.get("key_name") or LEGACY_KEY_NAME).strip(),
            "json_mode": _flag(raw.get("json_mode"), True),
            "per_minute": _number(raw.get("per_minute"), 20),
            "timeout": _number(raw.get("timeout_seconds"), 30),
            "yes": classify.yes_setting(raw.get("yes", classify.YES_DEFAULT)),
            "price_in": classify.money_setting(raw.get("price_in_per_m")),
            "price_out": classify.money_setting(raw.get("price_out_per_m")),
            "on": {t: bool((tasks.get(t) or {}).get("on", True)) for t in kind_tasks(provider)}}


def _from_legacy(legacy: dict) -> dict:
    kind = legacy["provider"]
    q = default_questions(kind)
    if kind == "decision" and "contacts" in q:
        q["contacts"]["bar"] = legacy["yes"]
    p = {"id": LEGACY_ID, "name": "Your earlier settings", "kind": kind,
         "base_url": legacy["base_url"], "model": legacy["model"],
         "key_name": legacy["key_name"], "json_mode": legacy["json_mode"],
         "per_minute": min(600, max(1, int(legacy["per_minute"] or 20))),
         "timeout_seconds": min(600, max(5, int(legacy["timeout"] or 30))),
         "prices": {"in_per_m": legacy["price_in"], "out_per_m": legacy["price_out"],
                    "from_list": legacy["price_in"] is None and legacy["price_out"] is None
                    and kind == "openai_compatible" and _deepinfra(legacy)},
         "tasks": list(kind_tasks(kind)), "questions": q,
         "notes": "Made once from the classifier settings an earlier Harvey kept.",
         "created_at": int(time.time()), "updated_at": int(time.time())}
    return p


async def migrate_legacy(state, use: dict) -> dict:
    """Once per install: the older ``classify_*`` settings, when they name
    a provider, become the ``settings`` profile, set for the tasks they
    were on for (a task already set to a profile keeps it); marked done
    (``classify:legacy``) and never read again: the profile is where the
    setup lives from then on. Settings naming no provider make nothing.
    Returns the tasks' use, as stored."""
    if await state.get_setting(LEGACY_KEY):
        return use
    legacy = await _legacy(state)
    if legacy is None:
        return use
    stored = await _load(state, PROFILES_KEY, {})
    if LEGACY_ID not in stored:
        stored[LEGACY_ID] = _from_legacy(legacy)
        await _save(state, PROFILES_KEY, stored)
    for t in kind_tasks(legacy["provider"]):
        if legacy["on"].get(t, True) and (use.get(t) or OFF) == OFF:
            use[t] = LEGACY_ID
    await _save(state, USE_KEY, use)
    await _save(state, LEGACY_KEY, {"migrated_at": int(time.time()), "profile": LEGACY_ID})
    return use


async def uses(state) -> dict[str, str]:
    """Each task's profile id, or "off"."""
    from harvey import classify

    stored = await migrate_legacy(state, await _load(state, USE_KEY, {}))
    return {t: stored.get(t) or OFF for t in classify.TASKS}


async def resolve(state) -> dict:
    """Every task's classify.Settings as the install sets them: its
    profile's (off when none, when the profile is gone or hidden, or can't
    answer it), with the install's caps; each one's ``by_task`` is the
    whole map."""
    from harvey import classify

    base = await classify.install_caps(state)
    use = await migrate_legacy(state, await _load(state, USE_KEY, {}))
    profiles = await every(state)
    by_task, made = {}, {}
    for t in classify.TASKS:
        p = profiles.get(use.get(t) or OFF)
        if p is None or t not in p["tasks"]:
            by_task[t] = off_settings(base)
            continue
        if p["id"] not in made:
            made[p["id"]] = settings_of(p, base)
        by_task[t] = made[p["id"]]
    for s in {id(v): v for v in by_task.values()}.values():
        s.by_task = by_task
    return by_task


async def settings_for(state, profile_id: str):
    """classify.Settings asking ``profile_id`` for every task it answers
    (a test run's), with the install's caps; None when there is no such
    profile (hidden built-ins count)."""
    from harvey import classify

    p = (await every(state, with_hidden=True)).get(profile_id)
    if p is None:
        return None
    base = await classify.install_caps(state)
    s = settings_of(p, base)
    off = off_settings(base)
    s.by_task = {t: (s if t in p["tasks"] else off) for t in classify.TASKS}
    off.by_task = s.by_task
    return s


def _new_id(name: str, taken) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "profile"
    pid, n = stem, 2
    # (LEGACY_ID is kept for the older settings' profile: one of yours
    # never takes it, so their one-time move can't land on it.)
    while pid in taken or pid in (OFF, LEGACY_ID):
        pid, n = f"{stem}-{n}", n + 1
    return pid


async def save(state, raw: dict, *, profile_id: str = "") -> dict:
    """Add a profile (no ``profile_id``) or change one: a built-in's change
    is a copy of your own, based on it. Returns the profile saved.
    ValueError says what is wrong; KeyError: no such profile."""
    stored = await _load(state, PROFILES_KEY, {})
    every_ = await every(state, with_hidden=True)
    base = None
    if profile_id:
        base = every_.get(profile_id)
        if base is None:
            raise KeyError(profile_id)
    p = check(raw, base=base)
    now = int(time.time())
    if base is None or base.get("builtin"):
        if len(stored) >= MAX_PROFILES:
            raise ValueError(f"{MAX_PROFILES} profiles of your own at most")
        pid = _new_id(p["name"] if base is None or "name" in raw
                      else f"{p['name']} copy", set(every_) | set(stored))
        if base is not None and "name" not in raw:
            p["name"] = f"{base['name']} (copy)"
        p.update({"id": pid, "builtin": False, "based_on": base["id"] if base else "",
                  "measured": None, "tested_hash": "", "created_at": now})
        p.pop("setup", None)
    else:
        pid = profile_id
    p["updated_at"] = now
    p["builtin"] = False
    stored[pid] = {k: v for k, v in p.items() if k != "builtin"}
    await _save(state, PROFILES_KEY, stored)
    return (await own(state))[pid]


async def remove(state, profile_id: str) -> dict:
    """Remove a profile of your own; the tasks it was set for are off.
    KeyError: none of yours by that id."""
    stored = await _load(state, PROFILES_KEY, {})
    if profile_id not in stored:
        raise KeyError(profile_id)
    del stored[profile_id]
    await _save(state, PROFILES_KEY, stored)
    return {"removed": profile_id, "off": await _unuse(state, profile_id)}


async def _unuse(state, profile_id: str) -> list[str]:
    use = await _load(state, USE_KEY, {})
    off = [t for t, pid in use.items() if pid == profile_id]
    if off:
        use.update({t: OFF for t in off})
        await _save(state, USE_KEY, use)
    return off


async def hide(state, profile_id: str, *, shown: bool = False) -> dict:
    """Hide a built-in (``shown``: show it again); a task set to it is off.
    KeyError: no built-in by that id."""
    if profile_id not in builtins():
        raise KeyError(profile_id)
    gone = [h for h in await hidden(state) if h != profile_id]
    off = []
    if not shown:
        gone.append(profile_id)
        off = await _unuse(state, profile_id)
    await _save(state, HIDDEN_KEY, gone)
    return {"hidden": gone, "off": off}


async def set_use(state, changes: dict) -> dict:
    """Set tasks to profiles ({task: profile id or "off"}). ValueError for
    an unknown task, a profile that doesn't exist or is hidden, or one
    that can't answer the task."""
    from harvey import classify

    if not isinstance(changes, dict) or not changes:
        raise ValueError("expected {task: profile id or \"off\"}")
    profiles = await every(state)
    for task, pid in changes.items():
        if task not in classify.TASKS:
            raise ValueError("tasks are " + ", ".join(classify.TASKS))
        if pid == OFF:
            continue
        p = profiles.get(pid) if isinstance(pid, str) else None
        if p is None:
            raise ValueError(f"no profile {pid!r} (or it is hidden)")
        if task not in p["tasks"]:
            raise ValueError(f"{p['name']} doesn't answer the {task} task")
    use = await _load(state, USE_KEY, {})
    use.update(changes)
    await _save(state, USE_KEY, use)
    return await uses(state)


# ── Test runs ──


async def tests(state, profile_id: str | None = None):
    """The test runs kept, newest first: one profile's, or {id: [...]}."""
    kept = await _load(state, TESTS_KEY, {})
    if profile_id is None:
        return {k: v for k, v in kept.items() if isinstance(v, list)}
    runs = kept.get(profile_id)
    return runs if isinstance(runs, list) else []


def counts_of(result: dict) -> dict:
    """A judge test's result as the counts a profile keeps (never a row's
    data: its failures' ids stay with the job)."""
    keep = ("task", "rows", "asked", "scored", "correct", "false_links", "misses", "invalid",
            "unsure", "not_asked", "contacts_right", "contacts_picked", "contacts_expected",
            "accuracy", "contact_precision", "contact_recall", "tokens_in", "tokens_out",
            "cost_usd", "cost_known", "stopped", "calls", "seconds")
    out = {k: result.get(k) for k in keep if k in result}
    lat = result.get("latency_ms") or {}
    out["median_ms"], out["p90_ms"] = lat.get("median"), lat.get("p90")
    return out


async def record_test(state, profile_id: str, result: dict, *, test_set: str,
                      job_id: str = "", asks: str = "") -> dict:
    """Keep a test run on ``profile_id`` (counts, cost, time, date, which
    test set; at most TESTS_KEPT); one of your own is tested for what it
    asks now, when that is what the test asked (``asks``: asks_hash when
    the test started; a profile changed meanwhile stays not tested)."""
    entry = {**counts_of(result), "at": int(time.time()), "test_set": test_set,
             "job_id": job_id}
    p = (await every(state, with_hidden=True)).get(profile_id)
    if p is not None:
        entry["asks"] = asks or asks_hash(p)
    kept = await _load(state, TESTS_KEY, {})
    kept[profile_id] = ([entry] + [r for r in kept.get(profile_id) or []
                                   if isinstance(r, dict)])[:TESTS_KEPT]
    await _save(state, TESTS_KEY, kept)
    stored = await _load(state, PROFILES_KEY, {})
    if p is not None and profile_id in stored and not result.get("stopped") \
            and entry["asks"] == asks_hash(p):
        stored[profile_id]["tested_hash"] = entry["asks"]
        await _save(state, PROFILES_KEY, stored)
    return entry


# ── As the dashboard shows them ──


def public(p: dict, *, use: dict, runs: list, key_set: bool | None,
           hidden_: bool = False) -> dict:
    """A profile for the Classifier card: every field (the key's name,
    never a value), whether its key is in .env, which tasks it is set for,
    whether it is tested, its measured results (a built-in's) and its
    test runs kept. ``is_deepinfra``: its base URL is DeepInfra's API host
    exactly (classify_prices.is_deepinfra), the rule its price list and
    account box follow."""
    from harvey import classify

    out = {k: v for k, v in p.items() if k != "tested_hash"}
    out.update({"tested": tested(p), "used_for": [t for t, pid in use.items() if pid == p["id"]],
                "is_deepinfra": _deepinfra(p),
                "can_answer": list(kind_tasks(p["kind"])), "key_set": key_set,
                "tests": runs, "hidden": hidden_,
                "sdk_missing": classify.sdk_missing(p["kind"]),
                "key_notes": key_notes(p.get("key_name") or "")})
    return out


def key_set(p: dict) -> bool | None:
    """Whether the .env holds a value under the profile's key name (None
    for a kind that needs none)."""
    from harvey import classify

    if p["kind"] == "brain":
        return None
    if not p.get("key_name"):
        return None if p["kind"] == "openai_compatible" else False
    return bool(classify._key(classify.Settings(key_name=p["key_name"])))
