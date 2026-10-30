"""A Romanian company's tax code (CUI, CIF): the one way every Romanian
module reads it.

``clean`` gives the digits a register or a file holds: no ``RO`` prefix, no
leading zeros, 2 to 10 digits. A sole trader's row can carry a 13-digit
personal numeric code (CNP) where the CUI goes: that is a person's national
id, never a CUI, so it is never kept or shown. ``valid`` also checks the
CUI's check digit, for a service that refuses a whole request over one bad
code (ANAF's).
"""

from __future__ import annotations

import re

_CHECK_KEY = "753217532"


def clean(raw) -> str:
    """The CUI as digits, or "" when there isn't a real one (0, a CNP, text)."""
    digits = re.sub(r"\D", "", str(raw or "").upper().removeprefix("RO")).lstrip("0")
    return digits if 2 <= len(digits) <= 10 else ""


def check_digit_ok(digits: str) -> bool:
    """Whether a clean CUI's last digit is its check digit."""
    if not digits or not digits.isdigit():
        return False
    body, check = digits[:-1], int(digits[-1])
    key = _CHECK_KEY[-len(body):] if body else ""
    total = sum(int(d) * int(k) for d, k in zip(body, key))
    return (total * 10) % 11 % 10 == check


def valid(raw) -> str:
    """The clean CUI when its check digit is right, else ""."""
    digits = clean(raw)
    return digits if digits and check_digit_ok(digits) else ""
