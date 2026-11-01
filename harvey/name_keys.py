"""The keys two records are compared on by name and town, with no database:
a register module's load builds its name index with the same keys the
resolver matches with (harvey/resolver.py re-exports them)."""

import csv
import io
import re
import unicodedata
from functools import lru_cache
from importlib import resources
from zlib import crc32

# Raised whenever fold(), name_key() or locality_key() change what they
# return: a name index a register built with other rules is then stale.
RULES = 1


@lru_cache(maxsize=1)
def version() -> str:
    """The rules' version: RULES and the legal forms list they strip."""
    data = resources.files("harvey").joinpath("reference/legal_forms.csv").read_bytes()
    return f"{RULES}.{crc32(data):08x}"


def fold(text: str) -> list[str]:
    """Lowercase words without accents (ș and ş alike), punctuation dropped."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", plain)


@lru_cache(maxsize=1)
def legal_forms() -> tuple[dict[int, set], dict[int, set]]:
    """(forms that can start a name, forms that can end one), each folded
    to words and grouped by length, longest first, from
    reference/legal_forms.csv."""
    text = resources.files("harvey").joinpath("reference/legal_forms.csv") \
        .read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    start, end = set(), set()
    for r in rows:
        words = tuple(fold(r["form"]))
        if not words:
            continue
        if r["where"] in ("start", "both"):
            start.add(words)
        if r["where"] in ("end", "both"):
            end.add(words)
    def by_length(forms):
        out: dict[int, set] = {}
        for form in forms:
            out.setdefault(len(form), set()).add(form)
        return dict(sorted(out.items(), reverse=True))
    return by_length(start), by_length(end)


def name_key(name: str) -> str:
    """The name two records are compared on: folded, legal forms stripped
    from either end. "" when too little is left to tell companies apart."""
    words = fold(name)
    start, end = legal_forms()
    stripped = True
    while stripped and words:
        stripped = False
        for size, forms in start.items():
            if len(words) > size and tuple(words[:size]) in forms:
                words, stripped = words[size:], True
                break
        for size, forms in end.items():
            if len(words) > size and tuple(words[-size:]) in forms:
                words, stripped = words[:-size], True
                break
    core = " ".join(words)
    return core if len(core.replace(" ", "")) >= 3 else ""


_PLACE_LABEL = re.compile(r"^(municipiul|mun|orasul|oras|comuna|com|satul|sat|loc)\s+")
_SECTOR = re.compile(r"\s+sector(ul)?\s+\d+$")
_SECTOR_ONLY = re.compile(r"sector(ul)?\s+\d+")


def locality_key(location: str) -> str:
    """The town a location names: its first part, folded, without an
    administrative label ("Municipiul", "Sat") or a Bucharest sector."""
    first = " ".join(fold((location or "").split(",")[0]))
    if _SECTOR_ONLY.fullmatch(first):
        # "Sector 1, București": the sector is Bucharest's, and so is the key
        # ("Mun. Bucuresti, Sector 2" reads as "bucuresti" too).
        return "bucuresti"
    return _SECTOR.sub("", _PLACE_LABEL.sub("", first)).strip()
