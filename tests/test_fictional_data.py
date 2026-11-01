"""The repo is public: phone numbers and free-mail addresses in code, tests
and docs must be ones no real subscriber can hold.

Romanian numbers use a subscriber part starting with 0 ("0268 000 101",
"0722 000 102"), a block that isn't handed out; US numbers use 555-01xx
(or the invalid 555 area code); a free-mail address is exemplu.fictiv.*,
or the provider's own system mailbox (mailer-daemon@googlemail.com)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DIRS = ("harvey", "tests", "docs", "skills", "prompts")
_EXT = {".py", ".md", ".json", ".html", ".js", ".sql", ".yaml", ".yml", ".toml",
        ".txt", ".csv"}

_RO = re.compile(r"(?<![\w+])(?:\+40|0040|0) ?([237](?:[ ./-]?\d){8})(?!\d)")
_US = [re.compile(r"(?<![\w.+/-])(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-](\d{3})[ .-](\d{4})"
                  r"(?![\w/-])"),
       re.compile(r"\+1([2-9]\d{2})(\d{3})(\d{4})\b")]
_FREE_MAIL = re.compile(r"\b([\w.+-]+)@(?:gmail|googlemail|yahoo|hotmail|outlook|icloud|aol"
                        r"|protonmail|proton|ymail|live|gmx|yandex)\.[a-z.]{2,6}\b", re.I)
_FICTIONAL_OR_SYSTEM = ("exemplu.fictiv", "mailer-daemon", "postmaster")


def _files():
    for top in _DIRS:
        yield from (p for p in (ROOT / top).rglob("*") if p.suffix in _EXT and p.is_file())
    yield from (p for p in ROOT.glob("*") if p.suffix in _EXT and p.is_file())


def _ro_unassigned(national: str) -> bool:
    # Bucharest (21, 31) has a two-digit area code, every other one three.
    return (national[2:] if national[:2] in ("21", "31") else national[3:]).startswith("0")


def _us_fictional(area: str, exchange: str, line: str) -> bool:
    return (area == "555" or exchange[0] in "01"
            or (exchange == "555" and line.startswith("01")))


def offenders(text: str) -> list[str]:
    found = [m.group(0) for m in _RO.finditer(text)
             if not _ro_unassigned(re.sub(r"\D", "", m.group(1)))]
    found += [m.group(0) for rx in _US for m in rx.finditer(text)
              if not _us_fictional(*m.groups())]
    found += [m.group(0) for m in _FREE_MAIL.finditer(text)
              if not m.group(1).lower().startswith(_FICTIONAL_OR_SYSTEM)]
    return found


def test_the_check_tells_fictional_from_assignable():
    assert offenders("Tel 0268 000 101, 0722 000 102, +40 21 000 0300") == []
    assert offenders("(303) 555-0123, +15550100199, exemplu.fictiv.0001@gmail.com") == []
    # Shaped like real ones, yet nobody's: area codes 0299 and 999 aren't
    # in use, and a Gmail name has at least six letters.
    assert offenders("Tel 0299 412 345") == ["0299 412 345"]
    assert offenders("+40299100200") == ["+40299100200"]
    assert offenders("call (999) 555-1212") == ["(999) 555-1212"]
    assert offenders("a@gmail.com") == ["a@gmail.com"]


def test_no_assignable_phone_or_free_mail_address_in_the_repo():
    this = Path(__file__).resolve()
    bad = [f"{p.relative_to(ROOT)}: {hit}" for p in _files() if p.resolve() != this
           for hit in offenders(p.read_text(encoding="utf-8", errors="ignore"))]
    assert bad == []
