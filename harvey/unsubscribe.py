"""Unsubscribe with no public server: a mailto header and a token.

Every cold (sequence) email carries

    List-Unsubscribe: <mailto:{sending address}?subject=dezabonare%20{token}>

so the recipient's mail client shows its own unsubscribe button. Pressing
it sends a short message to the sending mailbox itself, which the reply
poller already reads (agents/handler.py), and the token in its subject
names the email it answers. No List-Unsubscribe-Post header: RFC 8058's
one-click needs an HTTPS endpoint, and Harvey runs on the operator's
machine with no public address. Gmail's and Yahoo's one-click requirement
is for bulk senders (thousands a day to their users), far above Harvey's
daily caps; a mailto header is accepted below that.

The token is ten random base32 characters, stored on the outbox row when
the email is claimed for sending (outbox.unsub_token).
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import quote

WORD = "dezabonare"
TOKEN_LEN = 10
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# A mail client that doesn't decode the mailto subject leaves "%20" in it.
TOKEN_RE = re.compile(rf"\b{WORD}(?:%20|\s)*[:#-]?(?:%20|\s)*([A-Za-z2-7]{{{TOKEN_LEN}}})\b",
                      re.IGNORECASE)
# The opt-out keyword alone (a mailto answered by hand, a token that was
# cut): the Handler's keyword check reads it then.
WORD_RE = re.compile(rf"\b{WORD}\b", re.IGNORECASE)


def new_token() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LEN))


def header(address: str, token: str) -> str:
    """The List-Unsubscribe value for a cold email sent from ``address``."""
    return f"<mailto:{address}?subject={quote(f'{WORD} {token}')}>"


def headers(address: str, token: str) -> dict[str, str]:
    """The headers a cold email carries ({} with no sending address)."""
    return {"List-Unsubscribe": header(address, token)} if address and token else {}


def find_token(subject: str, body: str = "") -> str:
    """The token an unsubscribe message carries, in its subject or body
    (uppercase), or ""."""
    for text in (subject or "", (body or "")[:2000]):
        m = TOKEN_RE.search(text)
        if m:
            return m.group(1).upper()
    return ""
