"""The operator's acknowledgement before cold email goes out.

Harvey sends unsolicited commercial email to firms, PFA, II and IF
included, from addresses found in public sources. Whether and how that is
lawful for a given business is the operator's call, so cold (sequence)
email waits until they have read this text and typed "accept" once
(``harvey ack``, or the dashboard's endpoint). It is stored with its version
and the SHA-256 of the text: changing the text bumps the version and asks
again. The send gate refuses cold email without it (gate.pre_send_check).

Not legal advice: what the tool does to help is listed, and the
responsibility stays with the operator.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

KIND = "cold_email_li"
VERSION = 1
CONFIRM_WORD = "accept"

TEXT_RO = """Confirmare: email la rece către firme, pe interes legitim

Harvey trimite emailuri comerciale nesolicitate unor firme, inclusiv PFA, II și IF, la adrese găsite în surse publice. Legea 506/2004, art. 12, cere în general acordul prealabil pentru comunicări comerciale prin email; tu decizi dacă și cum se aplică activității tale.

Pentru adresele care identifică o persoană, GDPR permite prelucrarea pe interes legitim (art. 6 alin. (1) lit. f) doar după un test de echilibru, cere informarea persoanei cel târziu la primul contact (art. 14: cine ești, scopul, temeiul, sursa adresei, dreptul de opoziție) și respectarea opoziției imediat și fără condiții (art. 21).

Harvey te ajută: sursa adresei în primul email către o adresă personală, dezabonare în fiecare email rece (antet și un rând care cere un răspuns), o listă de suprimare permanentă verificată înainte de fiecare trimitere. Rândul cu sursa nu acoperă tot ce cere art. 14 (identitatea ta, scopul, temeiul): completează-l tu, de exemplu cu o pagină de confidențialitate.

Răspunderea pentru trimitere îți aparține. Acesta nu este un sfat juridic. Scrie „accept" pentru a confirma."""

TEXT_EN = """Acknowledgement: cold email to firms, on legitimate interest

Harvey sends unsolicited commercial email to firms, PFA, II and IF included, at addresses found in public sources. Law 506/2004, art. 12, generally requires prior consent for commercial communications by email; you decide whether and how that applies to your business.

For addresses that identify a person, the GDPR allows processing on legitimate interest (art. 6(1)(f)) only after a balancing test, requires informing the person at the first contact at the latest (art. 14: who you are, the purpose, the legal basis, the address's source, the right to object), and requires honouring an objection at once and unconditionally (art. 21).

Harvey helps: the address's source in the first email to a personal address, an unsubscribe in every cold email (a header and a line asking for a reply), and a permanent suppression list checked before every send. The source line does not cover everything art. 14 asks for (your identity, the purpose, the legal basis): add it yourself, for example with a privacy page.

Responsibility for sending is yours. This is not legal advice. Type "accept" to confirm."""


def text_sha() -> str:
    """The hash stored with an acknowledgement: both languages' text."""
    return hashlib.sha256((TEXT_RO + "\n\n" + TEXT_EN).encode("utf-8")).hexdigest()


def given(con: sqlite3.Connection) -> bool:
    """Whether the current version of the text was acknowledged."""
    try:
        return con.execute("SELECT 1 FROM acknowledgement WHERE kind = ? AND version = ? "
                           "AND text_sha = ?", (KIND, VERSION, text_sha())).fetchone() is not None
    except sqlite3.OperationalError:
        return False    # a file without the table (older schema) has none


def record(con: sqlite3.Connection, by: str) -> bool:
    """Store the acknowledgement of the current text (``by``: cli or
    dashboard). False when it was already given."""
    return con.execute(
        "INSERT INTO acknowledgement (kind, version, text_sha, at, by) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT DO NOTHING", (KIND, VERSION, text_sha(), int(time.time()), by)).rowcount == 1


def status(con: sqlite3.Connection) -> dict:
    """What a page or the CLI shows: the text, its version and whether (and
    when) it was acknowledged."""
    row = con.execute("SELECT at, by FROM acknowledgement WHERE kind = ? AND version = ? "
                      "AND text_sha = ?", (KIND, VERSION, text_sha())).fetchone()
    return {"kind": KIND, "version": VERSION, "text_sha": text_sha(), "text_ro": TEXT_RO,
            "text_en": TEXT_EN, "confirm_word": CONFIRM_WORD, "given": row is not None,
            "at": row[0] if row else None, "by": row[1] if row else ""}
