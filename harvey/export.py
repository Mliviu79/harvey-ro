"""Prospect-list export — the list itself is the product.

Produces a CSV that drops straight into Instantly, Smartlead, or any
sequencer (email / first_name / last_name / company_name / personalization
columns), plus Harvey's own quality columns (score, email_status, signals)
so a human can eyeball what they're getting.
"""

import csv
import io
import logging
import re

from harvey.collectors.phones import PhoneHit, phone_hit, phone_region
from harvey.gate import do_not_contact_domain_match, do_not_contact_match
from harvey.state import StateManager

logger = logging.getLogger("harvey.export")

# Column order chosen so the file imports cleanly into cold-email tools
# without remapping; extra columns are simply available as custom variables.
CSV_COLUMNS = [
    "email", "first_name", "last_name", "company_name", "title",
    "website", "linkedin_url", "industry", "personalization",
    "email_status", "score", "seniority", "source", "status", "created_at",
    # Appended, not inserted, so existing sequencer column mappings still line up.
    "phone", "location",
]

# One row per business, whether or not anyone there has an email yet: for a
# local business the phone and the address are often all there is.
COMPANY_CSV_COLUMNS = [
    "name", "website", "domain", "phone", "location", "industry",
    "email", "email_status", "contact_name", "contact_title", "source",
    # Appended: phone stays the main number (international format when it
    # parses); other_phones are the rest the company's site published.
    "phone_type", "other_phones",
]

DEFAULT_EMAIL_STATUSES = ["verified", "risky"]


async def collect_prospects(
    state: StateManager,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
    config=None,
) -> list[dict]:
    """Fetch prospects matching the filters as export-ready dicts.

    Default view is the *deliverable* list (verified + risky emails).
    Outreach exports exclude opted-out prospects even with explicit filters.
    ``include_all`` disables every filter for a full dump.
    """
    import aiosqlite

    where = []
    params: list = []
    if not include_all:
        # Opt-outs must not be reintroduced by an outreach filter. IS NOT also
        # preserves legacy NULL statuses; only known opt-outs are suppressed.
        where.append("status IS NOT ?")
        params.append("opted_out")
        wanted = email_statuses if email_statuses is not None else DEFAULT_EMAIL_STATUSES
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            where.append(f"email != '' AND email_status IN ({placeholders})")
            params.extend(wanted)
        if min_score > 0:
            where.append("score >= ?")
            params.append(min_score)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)

    sql = "SELECT * FROM prospects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY score DESC, created_at DESC"

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        async with db.execute("SELECT id, phone, location FROM companies") as cursor:
            companies = {r["id"]: dict(r) for r in await cursor.fetchall()}

    if not include_all:
        # A list for another sequencer is still contact: leave out anyone on
        # compliance.do_not_contact, as the send gate would.
        rows = [r for r in rows
                if not do_not_contact_match(r.get("email") or "", _do_not_contact(config))]

    out = []
    for r in rows:
        company = companies.get(r.get("company_id") or "", {})
        out.append({
            "email": r.get("email", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "company_name": r.get("company", ""),
            "title": r.get("title", ""),
            "website": r.get("source_url", ""),
            "linkedin_url": r.get("linkedin_url", ""),
            "industry": r.get("industry", ""),
            "personalization": r.get("personalization_notes", ""),
            "email_status": r.get("email_status", ""),
            "score": r.get("score", 0),
            "seniority": r.get("seniority", ""),
            "source": r.get("source", ""),
            "status": r.get("status", ""),
            "created_at": r.get("created_at", ""),
            "phone": r.get("phone") or company.get("phone") or "",
            "location": company.get("location") or "",
        })
    return out


# Which contact speaks for a company in the business export: the most
# deliverable address first, then the higher score. An invalid (bounced)
# address is never shown: that contact ranks below one with no email at all
# and, if picked anyway, is exported without its address.
_EMAIL_STATUS_RANK = {"verified": 0, "risky": 1, "guess": 2}


async def collect_companies(state: StateManager, config=None) -> list[dict]:
    """One export-ready dict per company, with its best contact if any.

    Suppression mirrors the prospect export: a company whose domain is on
    compliance.do_not_contact is left out, and an opted-out prospect or an
    address on that list is never the contact shown.
    """
    import aiosqlite

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM companies ORDER BY name COLLATE NOCASE, created_at"
        ) as cursor:
            companies = [dict(r) for r in await cursor.fetchall()]
        async with db.execute(
            "SELECT * FROM prospects WHERE company_id != '' AND status IS NOT ?",
            ("opted_out",),
        ) as cursor:
            prospects = [dict(r) for r in await cursor.fetchall()]
        async with db.execute(
            "SELECT company_id, value_text FROM observations "
            "WHERE signal_code = 'PHONE_NUMBER' AND value_text != '' "
            "ORDER BY observed_at, rowid"
        ) as cursor:
            site_phones: dict[str, list[str]] = {}
            for r in await cursor.fetchall():
                numbers = site_phones.setdefault(r["company_id"], [])
                if r["value_text"] not in numbers:
                    numbers.append(r["value_text"])

    entries = _do_not_contact(config)
    by_company: dict[str, list[dict]] = {}
    for p in prospects:
        if do_not_contact_match(p.get("email") or "", entries):
            continue
        if p.get("email_status") == "invalid":
            p = {**p, "email": "", "bounced": True}
        by_company.setdefault(p["company_id"], []).append(p)

    out = []
    for c in companies:
        if do_not_contact_domain_match(c.get("domain") or "", entries):
            continue
        contacts = sorted(
            by_company.get(c["id"], []),
            key=lambda p: (
                not p.get("email"),
                bool(p.get("bounced")),
                _EMAIL_STATUS_RANK.get(p.get("email_status") or "", 3),
                -(p.get("score") or 0),
            ),
        )
        best = contacts[0] if contacts else {}
        name = " ".join(
            part for part in (best.get("first_name"), best.get("last_name")) if part
        )
        region = phone_region(c.get("location") or "", config, c.get("domain") or "")
        phone, phone_type, other_phones = _phone_columns(
            (c.get("phone") or best.get("phone") or "").strip(), region,
            site_phones.get(c["id"], []))
        out.append({
            "name": c.get("name", ""),
            "website": c.get("website", ""),
            "domain": c.get("domain", ""),
            "phone": phone,
            "location": c.get("location", ""),
            "industry": c.get("industry", ""),
            "email": best.get("email", ""),
            "email_status": best.get("email_status", "") if best.get("email") else "",
            "contact_name": name,
            "contact_title": best.get("title", ""),
            "source": c.get("source", ""),
            "phone_type": phone_type,
            "other_phones": other_phones,
        })
    return out


def _listed_phones(raw: str, region) -> list:
    """A stored phone cell as numbers: one, or several as OpenStreetMap
    writes them ("+40 268 000 101;+40 740 000 102", "... / ..."). A piece
    that doesn't parse stays as written."""
    whole = phone_hit(raw, region)
    if whole is not None or not raw:
        return [whole] if whole is not None else []
    out: list = []
    for piece in (p.strip() for p in re.split(r"[;,]", raw)):
        if not piece:
            continue
        hit = phone_hit(piece, region)
        if hit is not None:
            out.append(hit)
            continue
        # "/" only splits when every side is a number: "0268/000.101" is one.
        parts = [p.strip() for p in piece.split("/") if p.strip()]
        hits = [phone_hit(p, region) for p in parts]
        if len(parts) > 1 and all(hits):
            out.extend(hits)
        else:
            out.append(piece)
    return out


def _phone_columns(raw: str, region, site: list[str]) -> tuple[str, str, str]:
    """phone, phone_type and other_phones. The stored cell's first number is
    the phone; its other numbers, then the site's (E.164 observations), are
    other_phones, each once, all in the international format "; "-joined."""
    listed = _listed_phones(raw, region)
    main = listed[0] if listed and isinstance(listed[0], PhoneHit) else None
    others, seen = [], {main.number} if main else set()
    rest = listed[1:] if main else []
    for item in rest + [phone_hit(n) or n for n in site]:
        key = item.number if isinstance(item, PhoneHit) else item
        if key in seen:
            continue
        seen.add(key)
        others.append(item.display if isinstance(item, PhoneHit) else item)
    return (main.display if main else raw, main.type if main else "", "; ".join(others))


def _do_not_contact(config) -> list[str]:
    from harvey.config import (
        ConfigError, ConfigFileNotFoundError, do_not_contact_from_file, live_do_not_contact,
    )
    if config is not None:
        return live_do_not_contact(config)
    try:
        return do_not_contact_from_file()
    except (ConfigFileNotFoundError, FileNotFoundError):
        return []
    except Exception as e:
        # Never export the people the list was meant to keep out.
        raise ConfigError(f"compliance.do_not_contact couldn't be read ({e}). "
                          "Fix it before exporting.") from e


_PLAIN_NUMBER = re.compile(r"[+\-]?[0-9 ().\-/;+]+\Z")


def csv_cell(value) -> str:
    """A cell a spreadsheet shows as text, never runs as a formula.

    Names, titles and addresses come from other people's websites, so a
    cell starting with = + - @ is quoted — unless it is only digits and
    phone punctuation ("+40 712 000 000", or several split by "; "), which
    can't call anything.
    """
    text = "" if value is None else str(value)
    if _PLAIN_NUMBER.match(text):
        return text
    # Spreadsheet software may ignore leading whitespace before formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def to_csv(rows: list[dict], columns: list[str] = CSV_COLUMNS) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: csv_cell(row.get(k)) for k in columns})
    return buf.getvalue()


async def export_prospects_csv(
    state: StateManager,
    out_path: str | None = None,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
    config=None,
) -> tuple[int, str]:
    """Export prospects to CSV. Returns (row_count, csv_text).

    Writes to ``out_path`` when given; caller handles printing/serving.
    """
    rows = await collect_prospects(
        state,
        email_statuses=email_statuses,
        min_score=min_score,
        statuses=statuses,
        include_all=include_all,
        config=config,
    )
    text = to_csv(rows)
    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} prospects to {out_path}")
    return len(rows), text


async def export_companies_csv(
    state: StateManager, out_path: str | None = None, config=None,
) -> tuple[int, str]:
    """Export one row per company to CSV. Returns (row_count, csv_text)."""
    rows = await collect_companies(state, config=config)
    text = to_csv(rows, COMPANY_CSV_COLUMNS)
    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} companies to {out_path}")
    return len(rows), text
