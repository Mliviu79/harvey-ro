"""The Contacts page's filters, read from its query string.

One parser for everything that narrows contacts the way the page does: the
page itself (/api/contacts and its exports), Find contacts, and a saved
audience (harvey/campaigns.py), which keeps the page's query string and is
read again with this whenever a campaign takes or rolls its members. So
what the page shows for a filter is exactly what a campaign gets.

No FastAPI here: the heartbeat and the CLI read audiences too.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl

CUI_QUERY = re.compile(r"\s*(?:RO\s*)?(\d{2,10})\s*", re.IGNORECASE)


def range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def number_arg(raw):
    """A bound typed in a filter box: a number, or None when empty."""
    if raw is None or str(raw).strip() == "":
        return None
    return range_bound(str(raw).strip())


def codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    out = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(out) > 200 or any(len(c) > 128 for c in out):
        raise ValueError("too many or oversized signal codes")
    return out


async def refuse_hidden_signals(state, signal_codes) -> None:
    """A signal of a module that is off can't be filtered on: it is hidden,
    and its data stops at the day the module went off."""
    hidden = {r["code"]: r for r in await state.get_signal_codes() if not r.get("available", 1)}
    for code in signal_codes:
        if code in hidden:
            from harvey.modules.switches import ModuleOff
            raise ModuleOff(hidden[code].get("module") or "", f"The signal {code}")


def caen_arg(raw: str) -> tuple[str, ...]:
    """CAEN codes from a filter box: 4-digit codes (also written 86.23 or
    8623/3) and words, turned into the classes they name. ValueError when
    words name no class."""
    if not raw:
        return ()
    from harvey import caen

    text = re.sub(r"\b(\d{2})\.(\d{2})\b", r"\1\2", raw)
    text = re.sub(r"\b(\d{4})/\d+\b", r"\1", text)
    found, words = [], []
    for token in re.split(r"[\s,;]+", text):
        if re.fullmatch(r"\d{4}", token):
            found.append(token)
        elif re.fullmatch(r"\d{3}", token):
            # A CAEN group (47.1): every class in it.
            group = sorted({code for code, _ in caen._classes() if code.startswith(token)})
            if not group:
                raise ValueError(f"No CAEN class is in group {token}")
            found += group
        elif token:
            words.append(token)
    if words:
        named = sorted(caen.codes_for(" ".join(words)))
        if not named:
            raise ValueError(f"No CAEN class matches \"{' '.join(words)}\"")
        found += named
    return tuple(dict.fromkeys(found))


def contact_arg(raw: str) -> str:
    """has-contact filter: any, none, mobile or a contact kind."""
    if raw and not re.fullmatch(r"[a-z_]{1,32}", raw):
        raise ValueError("contact must be any, none, mobile or a contact kind")
    return raw


def search_arg(raw: str) -> tuple[str, str, bool]:
    """(CUI digits, full-text expression, nothing) from the Companies search
    box. A number typed on its own ("10004070", "RO 10004070"; a bare
    four-digit number stays a CAEN code) gives both: the CUI when a company
    has it, else the words (the caller decides, filter_from_query). Only
    single letters or common words: ``nothing`` (no company matches).
    ValueError when only -words are typed (nothing to look for)."""
    if not raw.strip():
        return "", "", False
    from harvey.state import search_terms

    match, _ = search_terms(raw)
    m = CUI_QUERY.fullmatch(raw)
    if m and (len(m.group(1)) != 4 or raw.strip().upper().startswith("RO")):
        return m.group(1), match, False
    if not match:
        if all(t.startswith("-") for t in raw.split()):
            raise ValueError("Type a word to look for: a word with a minus only leaves things out.")
        return "", "", True
    return "", match, False


def query_args(query: str) -> dict[str, str]:
    """A query string ("kind=email&county=BV") as the page's arguments (the
    last value of a repeated key, as the page reads it)."""
    return dict(parse_qsl((query or "").lstrip("?"), keep_blank_values=True))


async def filter_from_query(state, args) -> tuple[object, bool]:
    """The ContactFilter the query string (or its arguments, a mapping)
    asks for, and whether the financials database must be attached. Raises
    ValueError (or ModuleOff for a hidden signal) on a bad argument."""
    from harvey import contacts

    if isinstance(args, str):
        args = query_args(args)
    get = lambda k: (args.get(k) or "").strip()[:200]  # noqa: E731
    f = contacts.ContactFilter(
        kind=get("kind"), status=get("status"), trust=get("trust"),
        has_person=get("person"), source=get("source"), county=get("county"),
        locality=get("locality"), website=get("website"), q=get("q"),
        caen=caen_arg(get("caen")), contact=contact_arg(get("contact")),
        found_in=get("found_in"))
    f.cui, f.search, f.nothing = search_arg(get("search"))
    return await _overlay_filter(state, f, args, get), False


async def _overlay_filter(state, f, args, get):
    """The rest of filter_from_query on the overlay: the words typed, the
    cohort's signals and the figures' bounds, as the overlay reads them."""
    f.words = get("search") if (f.search or f.cui) else ""
    if f.cui and not await state.read("cui_set_size", f.cui):
        # No company has that number: it is a word ("24").
        f.cui = ""
        f.nothing = not f.search
    if f.cui:
        f.words = ""
    require, exclude = codes(args.get("require") or ""), codes(args.get("exclude") or "")
    if require or exclude:
        await refuse_hidden_signals(state, [*require, *exclude])
        f.signals = (tuple(require), tuple(exclude))
    bounds = {k: number_arg(args.get(k)) for k in
              ("min_turnover", "max_turnover", "min_employees", "max_employees")}
    if any(v is not None for v in bounds.values()):
        f.bounds = {**bounds, "unknown": "include" if get("unknown") == "include" else "exclude"}
    return f
