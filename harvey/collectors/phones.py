"""Phone numbers a business publishes on its own pages.

Read from pages Harvey already fetched (the profile stage's homepage and
team page, Scout's site read), never a request of its own. ``tel:`` links
and schema.org ``telephone`` values come first, being the site's own
machine-readable claim; then the visible text, where only a number
libphonenumber validates for the company's country counts. Fax numbers
and the digits inside emails, URLs, IBANs, registration codes, tax ids,
dates, prices, postal codes and order or invoice numbers are not phone
numbers.

Without a country (``region`` None) only numbers written with their
international prefix (+40 or 0040) can be read, since "0268 123 456"
means nothing without one.
"""

from dataclasses import dataclass
import html as html_lib
import json
import re
from urllib.parse import unquote

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberMatcher, PhoneNumberType, Leniency

MAX_PHONES = 5
# Pages are capped like the email scan, so a huge page can't stall the matcher.
MAX_PAGE_CHARS = 400_000

_TYPES = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
}


@dataclass(frozen=True)
class PhoneHit:
    number: str     # E.164: "+40268123456"
    display: str    # international format: "+40 268 123 456"
    type: str       # mobile | landline | other
    source: str     # tel_link | schema | page_text

    @property
    def confidence(self) -> float:
        """Full for the site's machine-readable claim, a little less for a
        number read out of the text."""
        return 1.0 if self.source in ("tel_link", "schema") else 0.9


def phone_type(number) -> str:
    """mobile / landline / other. US and Canadian numbers can't be told
    apart (FIXED_LINE_OR_MOBILE), so they are "other"."""
    return _TYPES.get(phonenumbers.number_type(number), "other")


# What a number can take: an SMS audience is the mobiles. Numbers whose kind
# the numbering plan doesn't say (US and Canadian ones) are "unknown".
_LINE_TYPES = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "fixed",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.TOLL_FREE: "toll_free",
}


def line_type(number) -> str:
    """mobile / fixed / voip / toll_free / unknown, for a parsed number."""
    return _LINE_TYPES.get(phonenumbers.number_type(number), "unknown")


def _hit(number, source: str) -> PhoneHit:
    return PhoneHit(
        number=phonenumbers.format_number(number, PhoneNumberFormat.E164),
        display=phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL),
        type=phone_type(number),
        source=source,
    )


def _region(region: str | None) -> str | None:
    region = (region or "").strip().upper()
    return region if len(region) == 2 and region != "ZZ" else None


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _international(raw: str) -> bool:
    return raw.lstrip().startswith(("+", "00"))


def _written_whole(raw: str, number) -> bool:
    """A national number counts only with its trunk prefix as dialled
    ("0268 123 456", not "268123456" — an order or account number), or
    with its country code ("40 268 123 456", "1 303 555 0123")."""
    if _international(raw):
        return True
    digits = _digits(raw)
    national = _digits(phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL))
    return digits in (national, f"{number.country_code}{number.national_number}")


def parse_phone(raw: str, region: str | None = None):
    """``raw`` as a valid number, or None. With no region, only an
    international number ("+40 ...", "0040 ...") parses."""
    raw = (raw or "").strip()
    region = _region(region)
    if region is None and raw.startswith("00"):
        raw = "+" + raw[2:]
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number) or not _written_whole(raw, number):
        return None
    return number


def phone_hit(raw: str, region: str | None = None, source: str = "") -> PhoneHit | None:
    """A stored number (a listing's, say) in every form, or None when it
    doesn't parse."""
    number = parse_phone(raw, region)
    return _hit(number, source) if number is not None else None


def field_phones(raw: str, region: str | None = None) -> list[PhoneHit]:
    """Every valid number in a register's phone field, which can hold
    several ("0268 000 000 / 0722 000 000") or a national number written
    without its trunk 0. Shorter fragments are left out."""
    out: list[PhoneHit] = []
    for part in re.split(r"[/,;]|\s{2,}", str(raw or "")):
        part = part.strip()
        if len(_digits(part)) < 6:
            continue
        hit = phone_hit(part, region, "register")
        if hit is None and region and not part.startswith(("0", "+")):
            hit = phone_hit("0" + part, region, "register")
        if hit is not None and hit.number not in {h.number for h in out}:
            out.append(hit)
    return out


# Text that holds digits but is never a phone number. Masked (not deleted)
# before matching, so the digits either side of it can't join up.
_NOT_PHONES = [
    re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),             # emails
    re.compile(r"(?i)\b(?:https?://|www\.)\S+"),                       # URLs
    re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"),  # IBAN
    re.compile(r"\b[JFC]\s?\d{1,2}\s?/\s?\d+\s?/\s?\d{2,4}\b"),        # J08/1234/2010
    re.compile(r"\bJ\d{13}\b"),                                         # J2010001234083
    re.compile(r"(?i)\b(?:C\.?U\.?I\.?|C\.?I\.?F\.?|cod\s+fiscal|"
               r"cod\s+unic\s+de\s+[iî]nregistrare|VAT|EIN|tax\s+id)"
               r"\s*(?:no\.?|nr\.?)?\s*[:.]?\s*(?:RO\s?)?\d[\d\s-]{1,12}\d"),  # tax ids
    re.compile(r"\bRO\s?\d{2,10}\b"),                                   # RO12345678
    re.compile(r"(?i)[$€£]\s?\d[\d.,]*"),                              # $1,250
    # Only the number written against the currency, at most one thousands
    # group: "0268 412 345 250 lei" keeps its phone.
    re.compile(r"(?i)\b\d{1,3}(?:[.,]\d{3})?(?:[.,]\d{1,2})?\s?"
               r"(?:lei|ron|eur|euro|usd|gbp|dollars?)\b"),             # 1.250 lei
    re.compile(r"(?i)\b(?:cod\s+po[sș]tal|zip(?:\s+code)?|postcode|postal\s+code)"
               r"\s*[:.]?\s*\d[\d\s-]{2,9}\d"),                         # postal codes
]

# Dates: yyyy-mm-dd, and dd.mm.yyyy / d.m.yy. A real year and month, and
# not one link of a longer digit chain, so "0268-41-23-46" and
# "0740-12-11-20" (2-2-2 groups) stay phones.
_DATES = [
    re.compile(r"\b(?:19|20)\d{2}([./-])(?:0?[1-9]|1[0-2])\1(?:0?[1-9]|[12]\d|3[01])\b"
               r"(?![./-]\d)"),
    re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])([./-])(?:0?[1-9]|1[0-2])\1(?:\d{4}|\d{2})\b"
               r"(?![./-]\d)"),
]

# A fax label before the number: "Fax:", "Telefax", "Fax (secretariat):",
# a "Fax" cell before the number's cell; "F: ..." right before it. Only
# label text between them, no digits and no phone word.
_FAX_WORD = re.compile(r"(?i)\b(?:tele)?fax\b")
_F_LABEL = re.compile(r"\bF\s*[.:]\s*[-–]?\s*$")
_FAX_GAP = re.compile(r"(?i)\d|tel|phone|mobil|gsm|call|apel|sun[aă]")
FAX_LABEL_CHARS = 30
# "Tel/Fax", "Tel./Fax", "Telefon - fax": a phone that takes faxes too.
_TEL_AND = re.compile(r"(?i)\b(?:tel(?:efon)?|phone)\.?\s*(?:/|&|-|–|,|și|si|and|or|sau)\s*$")
# "0268 412 353 (fax)", "0268 412 353 - fax": the label after the number.
_FAX_AFTER = re.compile(r"(?i)^[^\S\n]*(?:\(\s*(?:tele)?fax\b|[-–—][^\S\n]*(?:tele)?fax\b)")
# "Fax: A / B": B is in the same list as A.
_LIST_GAP = re.compile(r"[^\S\n]*[/,;][^\S\n]*\Z")

# Order, invoice and reference numbers: their label right before the digits.
_REF_LABEL = re.compile(
    r"(?i)(?:\b(?:order|invoice|ref|reference|nr|no|comanda|comandă|factura|factură|"
    r"contract|aviz|dosar)\b\.?|#)[^\d\n]*$")
_PHONE_LABEL = re.compile(
    r"(?i)\b(?:tel|telefon|mobil|phone|call|programări|programari|sun[aă])")
REF_LABEL_CHARS = 15


_BLOCK_TAG = re.compile(
    r"(?i)</?(?:br|p|div|li|ul|ol|dd|dt|td|th|tr|table|h[1-6]|section|article|"
    r"header|footer|nav|aside|address|blockquote|hr|form|label|option)\b[^>]*>")


_JSON_LD = re.compile(
    r"""(?is)<script\b[^>]*type\s*=\s*["']?application/ld\+json["']?[^>]*>(.*?)</script>""")
_ITEMPROP = re.compile(
    r"""(?is)<(\w+)\b([^>]*\bitemprop\s*=\s*["']telephone["'][^>]*)>""")
_CONTENT = re.compile(r"""(?is)\bcontent\s*=\s*["']([^"']*)["']""")
_TEL_HREF = re.compile(r'(?i)href\s*=\s*["\']\s*tel:([^"\'>]+)["\']')


_CODE_CLOSE = {"script": re.compile(r"(?i)</script\s*>"),
               "style": re.compile(r"(?i)</style\s*>")}
_TAG_NAME = re.compile(r"<([A-Za-z][\w:-]*)")


def _tidy(html: str) -> str:
    """The page with comments dropped and every ``<`` that isn't a tag
    start escaped, in one linear pass.

    The tag patterns here scan from a ``<`` to its ``>`` or ``</script>``;
    on a page of unclosed tags each would rescan to the end of the page
    from every ``<``, and any site Harvey visits can serve that. After this
    every ``<`` is a tag with its own end, so each scan stops there. An
    unclosed tag, comment, script or style ends the page, as in a browser."""
    out: list[str] = []
    i, n = 0, len(html)
    while i < n:
        lt = html.find("<", i)
        if lt < 0:
            out.append(html[i:])
            break
        out.append(html[i:lt])
        if html.startswith("<!--", lt):
            end = html.find("-->", lt + 4)
            if end < 0:
                break
            out.append(" ")
            i = end + 3
            continue
        nxt = html[lt + 1:lt + 2]
        if not (nxt.isascii() and (nxt.isalpha() or nxt in "/!?")):
            out.append("&lt;")
            i = lt + 1
            continue
        gt = html.find(">", lt + 1)
        if gt < 0:
            break
        tag = html[lt:gt + 1]
        out.append("<" + tag[1:].replace("<", "&lt;"))
        i = gt + 1
        name = _TAG_NAME.match(tag)
        close = _CODE_CLOSE.get(name.group(1).lower()) if name else None
        if close is not None:
            end = close.search(html, i)
            if end is None:
                break
            out.append(html[i:end.start()].replace("<", "&lt;"))
            # Exactly the close the later patterns look for: "</script >"
            # left as written would send each of them to the end of the page.
            out.append(f"</{name.group(1).lower()}>")
            i = end.end()
    return "".join(out)


def _strip_code(html: str) -> str:
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    return re.sub(r"(?is)<style\b.*?</style>", " ", html)


def _visible_text(html: str) -> str:
    html = _strip_code(html)
    # A block tag becomes a line break, which the matcher never reads across:
    # "<li>0268 000 101</li><li>0740 000 102</li>" is two numbers, not one.
    # An inline one ("0268 <b>000 101</b>") is a space inside a number.
    html = re.sub(_BLOCK_TAG, "\n", html)
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", html)).replace("\xa0", " ")
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r" ?\n[\s]*", "\n", text)


def _telephones(data, out: list[str]) -> None:
    """Every schema.org ``telephone`` value, at any depth."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "telephone":
                values = value if isinstance(value, list) else [value]
                out.extend(v for v in values if isinstance(v, str))
            else:
                _telephones(value, out)
    elif isinstance(data, list):
        for item in data:
            _telephones(item, out)


def _schema_phones(html: str) -> list[str]:
    """``telephone`` from JSON-LD blocks and itemprop markup, as written."""
    values: list[str] = []
    for m in _JSON_LD.finditer(html):
        try:
            _telephones(json.loads(m.group(1).strip()), values)
        except (ValueError, RecursionError):
            continue
    for m in _ITEMPROP.finditer(html):
        content = _CONTENT.search(m.group(2))
        if content:
            values.append(html_lib.unescape(content.group(1)))
            continue
        close = re.search(rf"(?i)</{re.escape(m.group(1))}\s*>", html[m.end():m.end() + 300])
        if close:
            values.append(_visible_text(html[m.end():m.end() + close.start()]).strip())
    return values


def _masked(text: str) -> str:
    for pattern in _NOT_PHONES:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)

    def date(m):
        # "0740-12-11-20" is a phone: a date is never a link of a longer chain.
        before = m.string[max(0, m.start() - 2):m.start()]
        if len(before) == 2 and before[0].isdigit() and before[1] == m.group(1):
            return m.group(0)
        return " " * len(m.group(0))

    for pattern in _DATES:
        text = pattern.sub(date, text)
    return text


def _fax_before(text: str, start: int) -> bool:
    """A fax label before ``start``: "F:" right before it, or a fax word
    within FAX_LABEL_CHARS with only label text (one line or cell break at
    most) between — and not "Tel/Fax", which is a phone too."""
    offset = max(0, start - FAX_LABEL_CHARS)
    before = text[offset:start]
    if _F_LABEL.search(before):
        return True
    words = list(_FAX_WORD.finditer(before))
    if not words:
        return False
    word = words[-1]
    gap = before[word.end():]
    if _FAX_GAP.search(gap) or gap.count("\n") > 1:
        return False
    # "0268 … (fax), 0740 …": a note closing on the word labels the number
    # before it, not this one.
    if gap.lstrip().startswith(")"):
        return False
    at = offset + word.start()
    return not _TEL_AND.search(text[max(0, at - 12):at])


def _names_fax(label: str) -> bool:
    """Link text such as "Fax" or "Fax secretariat" (not "Tel/Fax")."""
    label = label.strip()
    for word in _FAX_WORD.finditer(label):
        if not _TEL_AND.search(label[max(0, word.start() - 12):word.start()]):
            return True
    return False


def _tel_link_is_fax(html: str, start: int, end: int) -> bool:
    """A tel: link whose own text, or the label before it, says Fax."""
    a_start = html.rfind("<a", max(0, start - 500), start)
    a_start = a_start if a_start >= 0 else start
    opened = html.find(">", end)
    close = html.find("</a", end, end + 500)
    if 0 <= opened < close and _names_fax(_visible_text(html[opened + 1:close])):
        return True
    before = _visible_text(html[max(0, a_start - 300):a_start]).rstrip("\n")
    return _fax_before(before, len(before))


def _is_reference(text: str, start: int) -> bool:
    """An order, invoice or reference number: its label right before the
    digits on the same line, and no phone label with it ("Tel. nr. 0268..."
    is a phone)."""
    # A bounded look back: slicing all of text[:start] per match is
    # quadratic on a page of digits. 200 chars reach past any label.
    line = text[max(0, start - 200):start].rsplit("\n", 1)[-1]
    before = re.split(r"\d", line)[-1][-REF_LABEL_CHARS:]
    return bool(_REF_LABEL.search(before)) and not _PHONE_LABEL.search(before)


def phones_from_html(html: str, region: str | None = None,
                     limit: int = MAX_PHONES) -> list[PhoneHit]:
    """The phone numbers a page publishes, tel: links first, then schema.org
    ``telephone``, then the text; deduped by E.164 in the order seen, at
    most ``limit``."""
    html = (html or "")[:MAX_PAGE_CHARS]
    region = _region(region)
    hits: list[PhoneHit] = []
    seen: set[str] = set()

    def add(number, source):
        hit = _hit(number, source)
        if hit.number not in seen:
            seen.add(hit.number)
            hits.append(hit)

    # A commented-out number (an old footer, a JSON-LD block switched off)
    # is not one the site publishes.
    html = _tidy(html)
    schema = _schema_phones(html)
    html = _strip_code(html)

    for m in _TEL_HREF.finditer(html):
        raw = re.sub(r"[;,].*$", "", unquote(html_lib.unescape(m.group(1))))
        number = parse_phone(re.sub(r"\s", "", raw), region)
        if number is not None and not _tel_link_is_fax(html, m.start(), m.end()):
            add(number, "tel_link")

    for raw in schema:
        number = parse_phone(raw, region)
        if number is not None:
            add(number, "schema")

    text = _visible_text(html)
    masked = _masked(text)
    if region is None:
        # The matcher needs a region for 00; with none, only + can follow.
        # " +" keeps the length, so offsets still point into ``text``.
        masked = re.sub(r"(?<![\d+])00(?=[\s.-]?[1-9])", " +", masked)
    prev_end, prev_fax = -1, False
    for m in PhoneNumberMatcher(masked, region or "ZZ", leniency=Leniency.VALID):
        # "Fax: A / B": B is a fax too.
        listed = prev_fax and _LIST_GAP.fullmatch(text[prev_end:m.start]) is not None
        fax = (listed or _fax_before(text, m.start)
               or _FAX_AFTER.match(text[m.end:m.end + 20]) is not None)
        prev_end, prev_fax = m.end, fax
        if fax or _is_reference(text, m.start):
            continue
        if region is not None and not _written_whole(m.raw_string, m.number):
            continue
        add(m.number, "page_text")
        if len(hits) >= limit:
            break
    return hits[:limit]


# ccTLDs used as generic names (".io", ".co", ".ai"), which say nothing
# about where the business is.
_GENERIC_TLDS = {"io", "co", "ai", "me", "tv", "fm", "ly", "gg", "cc", "ws",
                 "to", "sh", "so"}


def phone_region(location: str = "", config=None, domain: str = "") -> str | None:
    """The country to read a business's national numbers in, first of:
    its domain's country TLD (".ro"); the country its location certainly
    names; ``compliance.precleared.prospect_country``; the one country
    every ``icp.geography`` entry is in; the location's uncertain reading.
    None when nothing says.

    The domain beats the rest: a clinica.ro found while harvey.yaml still
    says "United States" is Romanian, and so is one whose location ends in
    a county code that reads as a country ("Sibiu, SB")."""
    from harvey.send_window import _prospect_country, country_code, country_for, icp_country

    located, certain = country_for(location or "")
    tld = domain.lower().rstrip(".").rsplit(".", 1)[-1] if domain else ""
    candidates = [
        country_code(tld) if tld and tld not in _GENERIC_TLDS else "",
        located if certain else "",
        _prospect_country(config) if config is not None else "",
        icp_country(config) if config is not None else "",
        located,
    ]
    for country in candidates:
        if country and phonenumbers.country_code_for_region(country):
            return country
    return None
