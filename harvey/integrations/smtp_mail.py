"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling reads the
inbox by date (the last week, or back to the last complete read when
Harvey was off longer) in read-only mode with BODY.PEEK, so nothing is
marked seen: a reply the operator opened first, or one read just before a
crash, is still read next time (the Handler deduplicates). The spam/junk
folder is searched for unsubscribe messages only.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import aiosmtplib

from harvey.integrations.mail_provider import (
    UNSUBSCRIBE_WORD,
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
    partial_reach,
    read_window_start,
)

logger = logging.getLogger("harvey.smtp")

# More messages than this in the window and only the newest this many are
# read: the read is then complete only from the oldest of them (covered_from),
# and the Handler holds cold email until the gap is read or accepted.
MAX_MESSAGES = 5000
# Folder names a junk folder goes by when the server marks none \Junk.
JUNK_NAMES = ("Junk", "Spam", "Junk E-mail", "Junk Email", "[Gmail]/Spam", "INBOX.Junk",
              "INBOX.Spam", "Spam-uri", "Nedorite")


class ImapError(RuntimeError):
    pass


class SmtpImapProvider(MailProvider):
    name = "smtp"
    supports_headers = True

    def __init__(self, config, env):
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass
        # Messages already read, by (folder, UIDVALIDITY, UID): a message's
        # content never changes, so each cycle downloads only what's new.
        self._read_cache: dict[tuple, InboundMessage | None] = {}
        self._cut_short = False      # the last inbox read had more than MAX_MESSAGES

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    def from_address(self) -> str:
        return (getattr(self.config.persona, "email", "") or self.smtp_user or "").strip()

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: dict | None = None,
    ) -> SendResult:
        persona = self.config.persona
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email or self.smtp_user}>"
        msg["Subject"] = subject
        message_id = make_msgid(domain=(persona.email or self.smtp_user).split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        for name, value in (headers or {}).items():
            msg[name] = value
        msg.set_content(body)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Every inbox message since the window's start; raises on any
        failure (the heartbeat then holds sends: an unread reply can't have
        stopped its follow-ups)."""
        start = read_window_start(self.read_since)
        loop = asyncio.get_event_loop()
        self._cut_short = False
        messages = await loop.run_in_executor(None, self._fetch, start, False)
        self.covered_from = partial_reach(messages) if self._cut_short else start
        return messages

    async def get_spam_unsubscribes(self, limit: int = 500) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch,
                                          read_window_start(self.read_since), True)

    def _fetch(self, start: float, junk: bool) -> list[InboundMessage]:
        """The inbox since ``start``, or (``junk``) the junk folder's
        unsubscribe messages since then."""
        since = datetime.fromtimestamp(start, timezone.utc).strftime("%d-%b-%Y")
        out: list[InboundMessage] = []
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            if not junk:
                out += self._read_folder(conn, "INBOX", f"SINCE {since}")
            elif folder := self._junk_folder(conn):
                # Copies: the cached message itself stays as read.
                out += [replace(msg, folder="spam") for msg in self._read_folder(
                    conn, folder, f'SINCE {since} SUBJECT "{UNSUBSCRIBE_WORD}"', junk=True)]
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _read_folder(self, conn, folder: str, criteria: str,
                     junk: bool = False) -> list[InboundMessage]:
        """The messages of ``folder`` matching ``criteria``, read-only and
        with BODY.PEEK (nothing is marked seen); the newest MAX_MESSAGES of
        them when there are more (the inbox's read is then cut short). The
        cache keeps only the folder's messages this search found."""
        status, data = conn.select(_quoted(folder), readonly=True)
        if status != "OK":
            raise ImapError(f"couldn't open {folder}")
        status, validity = conn.response("UIDVALIDITY")
        validity = (validity or [b""])[0] or b""
        status, data = conn.uid("SEARCH", None, criteria)
        if status != "OK":
            raise ImapError(f"couldn't search {folder}")
        uids = (data[0] or b"").split()
        if len(uids) > MAX_MESSAGES:
            logger.warning(f"IMAP: {folder} has {len(uids)} messages since the last complete "
                           f"read; reading the newest {MAX_MESSAGES}.")
            uids = uids[-MAX_MESSAGES:]     # UIDs rise with arrival
            if not junk:
                self._cut_short = True
        out = []
        for uid in uids:
            key = (folder, validity, uid)
            if key not in self._read_cache:
                status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK":
                    raise ImapError(f"couldn't read a message in {folder}")
                raw = next((p[1] for p in parts or () if isinstance(p, tuple)), None)
                self._read_cache[key] = self._parse_rfc822(raw) if raw else None
            if self._read_cache[key] is not None:
                out.append(self._read_cache[key])
        found = {(folder, validity, uid) for uid in uids}
        self._read_cache = {k: v for k, v in self._read_cache.items()
                            if k[0] != folder or k in found}
        return out

    @staticmethod
    def _junk_folder(conn) -> str:
        """The junk folder's name: the one the server marks \\Junk, else a
        usual name; "" when there is none."""
        status, rows = conn.list()
        if status != "OK":
            return ""
        names = {}
        for row in rows or ():
            line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
            m = re.match(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").strip().strip('"')
            if "\\junk" in m.group("flags").lower():
                return name
            names[name.lower()] = name
        return next((names[n.lower()] for n in JUNK_NAMES if n.lower() in names), "")

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        # The thread, and what marks mail sent to a list (Handler: is_bulk).
        headers = {name.lower(): str(msg.get(name, "") or "").strip()
                   for name in ("References", "List-Unsubscribe", "List-Id", "Precedence")}
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
            headers=headers,
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"


def _quoted(folder: str) -> str:
    """A folder name as IMAP takes it (quoted when it has a space)."""
    return f'"{folder}"' if (" " in folder and not folder.startswith('"')) else folder
