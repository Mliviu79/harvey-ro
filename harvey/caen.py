"""CAEN, Romania's activity codes, searched by the words people actually use.

The registry (ONRC) files every company under 4-digit CAEN classes, but
nobody prospects by typing "8623": they type "dentist" or "stomatologie".
This module turns words into class codes and codes back into words, from
the nomenclature ONRC publishes as open data (reference/caen_ro.csv).

Two revisions are live: Rev.2 (2008) and Rev.3 (2025). Most classes kept
their number, but some split: Rev.2's 8690 "other human health" became
Rev.3's labs (8691), physiotherapy (8695), psychologists (8693) and more.
A company registered before the switch can still carry the Rev.2 code, so
a search answers with both. Versions 0 and 1 (the pre-2008 nomenclatures)
are kept so old registrations still describe() correctly, but search never
returns them: their numbers mean different things (8513 was dental then,
it's secondary education now).

Matching order: the hand-kept synonyms below first, then the Romanian
class names (and the English ones, health classes only) word by word,
ignoring accents and small inflections ("stomatologică" finds
"stomatologic"). Deterministic, no network, no Claude.
"""

import csv
import io
import re
import unicodedata
from functools import lru_cache
from importlib import resources
from typing import NamedTuple

CURRENT_VERSIONS = (3, 2)          # newest first; what search() answers with
REVISION_LABELS = {0: "v0", 1: "v1", 2: "Rev.2", 3: "Rev.3"}


class Match(NamedTuple):
    code: str           # "8623", leading zeros kept ("0111")
    version: int        # 2 = Rev.2, 3 = Rev.3
    name_ro: str
    name_en: str | None
    why: str            # 'synonym "dentist"' or 'name matches "stomatologica"'


def _both(code: str) -> tuple[tuple[str, int], ...]:
    return ((code, 3), (code, 2))


_DENTAL = _both("8623")
_PHARMACY = _both("4773")
_GENERAL = _both("8621")
_SPECIALIST = _both("8622")
_MEDICAL_PRACTICE = _GENERAL + _SPECIALIST
# Rev.3 split these out of Rev.2's catch-all 8690, so ask for both.
_LAB = (("8691", 3), ("8690", 2))
_PHYSIO = (("8695", 3), ("8690", 2))
_PSYCH = (("8693", 3), ("8690", 2))
# No optician class of its own in either revision: opticians file under
# "other new goods", which also holds plenty of shops that aren't opticians.
_OPTICIAN = _both("4778")
# Animal health, not human health. Here because people ask for it.
_VET = _both("7500")

# Everyday words (English and Romanian, written without accents) -> classes.
# Every code here must exist in the nomenclature (tests/test_caen.py checks).
SYNONYMS: dict[str, tuple[tuple[str, int], ...]] = {
    "dentist": _DENTAL, "dental": _DENTAL, "dentistry": _DENTAL,
    "stomatolog": _DENTAL, "stomatologie": _DENTAL, "medic dentist": _DENTAL,
    "ortodont": _DENTAL, "ortodontie": _DENTAL, "orthodontist": _DENTAL,
    "orthodontics": _DENTAL, "cabinet stomatologic": _DENTAL,
    "clinica stomatologica": _DENTAL, "dental clinic": _DENTAL,
    "pharmacy": _PHARMACY, "chemist": _PHARMACY,
    "farmacie": _PHARMACY, "farmacii": _PHARMACY, "farmacist": _PHARMACY,
    "doctor": _GENERAL, "gp": _GENERAL, "general practice": _GENERAL,
    "general practitioner": _GENERAL, "family doctor": _GENERAL,
    "family medicine": _GENERAL, "medic de familie": _GENERAL,
    "medicina de familie": _GENERAL, "medicina generala": _GENERAL,
    "specialist": _SPECIALIST, "clinic": _SPECIALIST, "clinica": _SPECIALIST,
    "policlinica": _SPECIALIST, "polyclinic": _SPECIALIST,
    "cabinet medical": _MEDICAL_PRACTICE,
    "lab": _LAB, "laboratory": _LAB, "laborator": _LAB, "analize": _LAB,
    "imaging": _LAB, "radiology": _LAB, "radiologie": _LAB, "imagistica": _LAB,
    "physio": _PHYSIO, "physiotherapy": _PHYSIO, "physiotherapist": _PHYSIO,
    "rehabilitation": _PHYSIO, "kinetoterapie": _PHYSIO,
    "kinetoterapeut": _PHYSIO, "fizioterapie": _PHYSIO,
    "fizioterapeut": _PHYSIO, "recuperare": _PHYSIO,
    "psychologist": _PSYCH, "psychology": _PSYCH, "psychotherapist": _PSYCH,
    "psiholog": _PSYCH, "psihologie": _PSYCH, "psihoterapeut": _PSYCH,
    "psihoterapie": _PSYCH,
    "optician": _OPTICIAN, "optica": _OPTICIAN, "optical": _OPTICIAN,
    "veterinary": _VET, "vet": _VET, "veterinar": _VET,
}

# Words for a kind of place, not an activity. When the query also names the
# activity ("clinică stomatologică", "dentist clinic"), that one decides and
# these are dropped: a dental clinic is a dental practice, not a specialist
# one.
GENERIC_PLACES = frozenset({"clinic", "clinica", "policlinica", "polyclinic",
                            "cabinet medical"})

# Short English names for the health-related classes only. Everything else
# shows its Romanian name: a guessed translation is worse than none.
_HEALTH_EN = {
    "8610": "Hospital activities",
    "8621": "General medical practice",
    "8622": "Specialist medical practice",
    "8623": "Dental practice",
    "8710": "Residential nursing care",
    "8720": "Residential mental health and addiction care",
    "8730": "Residential care for the elderly and disabled",
    "4773": "Pharmacies",
    "4774": "Medical and orthopaedic goods shops",
    "4778": "Other specialised retail (incl. opticians)",
    "7500": "Veterinary activities (not human health)",
}
ENGLISH_NAMES: dict[tuple[str, int], str] = {
    **{(c, v): n for c, n in _HEALTH_EN.items() for v in (2, 3)},
    ("8690", 2): "Other human health activities (labs, imaging, physio, psychologists)",
    ("8691", 3): "Diagnostic imaging and medical laboratories",
    ("8692", 3): "Ambulance transport",
    ("8693", 3): "Psychologists and psychotherapists",
    ("8694", 3): "Nursing and midwifery",
    ("8695", 3): "Physiotherapy",
    ("8696", 3): "Traditional, complementary and alternative medicine",
    ("8697", 3): "Intermediation for medical and dental services",
    ("8699", 3): "Other human health activities n.e.c.",
}

# Joining words that would otherwise match half the nomenclature.
_STOPWORDS = {"ale", "cel", "cea", "cu", "din", "fara", "pentru", "prin", "sau",
              "activitati", "activitate", "and", "for", "the", "incl", "not"}


def _fold(text: str) -> str:
    """Lowercase without diacritics: both ș (comma) and ş (cedilla) become s."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _fold(text))
            if len(t) >= 3 or t == "gp"]


def _content(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _STOPWORDS]


def _same_word(a: str, b: str) -> int:
    """2 for the same word, 1 for the same word inflected, 0 otherwise.

    Romanian inflects at the end ("stomatolog", "stomatologică",
    "stomatologice", "clinicii"), so two words of 5+ letters count as one
    when one is the other plus up to three letters, or they differ only in
    a short ending (one letter against at most two). "restaurant" and
    "restaurare" share eight letters but are different words.
    """
    if a == b:
        return 2
    if min(len(a), len(b)) < 5:
        return 0
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    if common < 5:
        return 0
    tails = sorted((len(a) - common, len(b) - common))
    return 1 if (tails[0] == 0 and tails[1] <= 3) or tails <= [1, 2] else 0


@lru_cache(maxsize=1)
def _classes() -> dict[tuple[str, int], str]:
    """(code, version) -> Romanian name, from the shipped nomenclature."""
    text = resources.files("harvey").joinpath("reference/caen_ro.csv").read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    return {(r["code"], int(r["version"])): r["name_ro"] for r in rows}


@lru_cache(maxsize=1)
def _index() -> list[tuple[str, int, set[str]]]:
    """Searchable words per current class: Romanian name plus any English one."""
    out = []
    for (code, version), name in sorted(_classes().items()):
        if version not in CURRENT_VERSIONS:
            continue
        words = {code} | set(_content(_tokens(name)))   # "8623" finds itself
        words |= set(_content(_tokens(ENGLISH_NAMES.get((code, version), ""))))
        out.append((code, version, words))
    return out


def _normalize_code(code) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    return digits.zfill(4) if 0 < len(digits) <= 4 else digits


def _normalize_version(version) -> int | None:
    if version is None or version == "":
        return None
    digits = re.sub(r"\D", "", str(version))
    return int(digits) if digits else None


def _match(code: str, version: int, why: str) -> Match:
    return Match(code, version, _classes()[(code, version)],
                 ENGLISH_NAMES.get((code, version)), why)


def _swapped(typed: str, word: str) -> bool:
    """One pair of neighbouring letters swapped ("dentsit" for "dentist"),
    in a word of 6+ letters. Only for synonyms: a short list of known
    words, where a swap can't land on a different one."""
    if len(word) < 6 or len(typed) != len(word) or typed == word:
        return False
    diff = [i for i, (x, y) in enumerate(zip(typed, word)) if x != y]
    return (len(diff) == 2 and diff[1] == diff[0] + 1
            and typed[diff[0]] == word[diff[1]] and typed[diff[1]] == word[diff[0]])


def _synonym_hits(tokens: list[str]) -> list[tuple[int, str, tuple]]:
    """(position, phrase, classes) for each synonym in the query, in query
    order. Longer phrases claim their words first, so "medic de familie"
    wins over a bare "medic". A generic place word ("clinică") is dropped
    when another synonym names the activity."""
    phrases = sorted(SYNONYMS, key=lambda p: (-len(_tokens(p)), p))
    used: set[int] = set()
    hits = []
    for phrase in phrases:
        words = _tokens(phrase)
        for start in range(len(tokens) - len(words) + 1):
            span = range(start, start + len(words))
            if used & set(span):
                continue
            if all(_same_word(tokens[i], w) or _swapped(tokens[i], w)
                   for i, w in zip(span, words)):
                hits.append((start, phrase, SYNONYMS[phrase]))
                used |= set(span)
                break
    specific = [h for h in hits if h[1] not in GENERIC_PLACES]
    return sorted(specific or hits)


def search(words: str, limit: int | None = None) -> list[Match]:
    """Classes for some words, best first, in both current revisions.

    Synonym hits come first, in the order their words appear in the query.
    Then classes whose name holds every word of the query (allowing for
    inflection), closest matches first. Every word, not just the ones no
    synonym used: "asistență stomatologică" should not bring back every
    class that mentions "asistență".
    """
    tokens = _tokens(words)
    hits = _synonym_hits(tokens)
    out: list[Match] = []
    seen: set[tuple[str, int]] = set()
    for _, phrase, classes in hits:
        for code, version in classes:
            if (code, version) in _classes() and (code, version) not in seen:
                seen.add((code, version))
                out.append(_match(code, version, f'synonym "{phrase}"'))

    rest = _content(tokens)
    if rest:
        scored = []
        for code, version, name_words in _index():
            if (code, version) in seen:
                continue
            per_word = [max((_same_word(q, w) for w in name_words), default=0)
                        for q in rest]
            if all(per_word):
                scored.append((-sum(per_word), code, -version))
        for _, code, neg_version in sorted(scored):
            why = "name matches " + ", ".join(f'"{t}"' for t in rest)
            out.append(_match(code, -neg_version, why))

    return out[:limit] if limit else out


def classes_for(words: str) -> list[tuple[str, int]]:
    """(code, version) pairs the words point to, best first, across Rev.2
    and Rev.3.

    When a synonym knows the words, only its classes: "stomatologică" means
    dental practices, not the factories whose name also says "stomatologice".
    Otherwise every class search() found by name. The version matters when
    reading the registry, where a code means different things in different
    nomenclatures.
    """
    found = search(words)
    chosen = [m for m in found if m.why.startswith("synonym ")] or found
    return list(dict.fromkeys((m.code, m.version) for m in chosen))


def codes_for(words: str) -> set[str]:
    """The class codes the words point to, across Rev.2 and Rev.3."""
    return {code for code, _ in classes_for(words)}


def describe(code, version=None) -> str | None:
    """A class in words: the English name where Harvey has one, else the
    Romanian one. Without a version, the newest revision that has the code.
    None for a code the nomenclature doesn't have.

    Also takes an ACTIVITY_CODE value, which carries its revision:
    describe("8690/2") is Rev.2's 8690."""
    if version is None and "/" in str(code or ""):
        code, _, version = str(code).partition("/")
    code = _normalize_code(code)
    wanted = _normalize_version(version)
    versions = (wanted,) if wanted is not None else (3, 2, 1, 0)
    for v in versions:
        name = _classes().get((code, v))
        if name is not None:
            return ENGLISH_NAMES.get((code, v)) or name
    return None


def revision_label(version: int) -> str:
    return REVISION_LABELS.get(version, f"v{version}")
