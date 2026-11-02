"""Is this the same company? One deterministic answer for every source.

A company reaches Harvey from a map listing, a search result, its own site
or the trade registry, each with its own key: a place id, a domain, a tax
code (CUI). Each source used to dedup by its own key alone, so a Maps hit
for a registry firm with no website became a second company. Here every
key a company is known by lives in ``company_ids``, and an incoming record
is scored against what is on file:

    same CUI, registration number or listing id     1.0
    same domain (not a free-mail or directory one)  0.9
    same phone, held by at most two companies       0.8
    same email (not on a free-mail provider), ditto 0.8
    same name (legal form stripped), same town      0.7

Several kinds of evidence add up (two independent hints are stronger than
either: 0.7 and 0.8 make 0.94). At ATTACH or more the record is the same
company; from REVIEW up to ATTACH a new company is kept and the pair goes
to the review list; below that they are different companies. Two different
CUIs (or registration numbers) never match, whatever else agrees. Nothing
here asks a model: the same input always gives the same answer.

A contact is many to many: a chain's phone is one contact linked to every
unit, never a reason to merge them. So a phone, email or website counts by
who else holds it (``CONTACT_KEYS``):

- held by exactly one registered firm (a CUI) and records without one (map
  listings): those records are its branches, and merging makes each one a
  location of the firm. Automatic for a website, and for a phone or email
  shared with at most BRANCHES_AUTO_MAX of them; more go to review;
- held by two or three registered firms (a family's number, an
  accountant's): never evidence between those firms, and a record without
  a CUI holding it goes to review against each, never merged on its own;
- held by more firms than that: a chain's switchboard, ignored.

The review list is read as groups (``groups``): every pair still to decide,
each side followed to the company it stands for now, joined into connected
groups, so a pair made stale by a later merge disappears and four records
of one business are one decision, not six.

A merge moves everything one company holds (observations, contacts,
identifiers, activities) onto the other and logs each row it moved, so
undoing it puts back exactly what it took. Nothing is deleted, and nothing
already on the surviving record is overwritten: its empty columns are
filled, every other value stays on file with its source.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

from harvey import contacts
# The name and town keys (re-exported: resolver.fold, resolver.name_key, ...).
from harvey.name_keys import fold, locality_key, name_key  # noqa: F401

logger = logging.getLogger("harvey.resolver")

ATTACH = 0.85
REVIEW = 0.6
# What each kind of evidence is worth on its own.
WEIGHTS = {
    "cui": 1.0, "reg": 1.0, "external": 1.0,
    "domain": 0.9,
    "declared_domain": 0.7,     # a website a registry row declares, unchecked
    "phone": 0.8,
    "email": 0.8,
    "name": 0.7,
}
# Keys that are a record's own: matching one means it is that record.
SAME_RECORD = ("cui", "reg", "external", "domain")
# The legal identity: two different values are two different companies.
LEGAL = ("cui", "reg")
# A phone more companies share than this is a switchboard or a call centre,
# and a name in one town more often than this is a chain: neither says
# which of them a new record is.
PHONE_HOLDERS_MAX = 2
EMAIL_HOLDERS_MAX = 2
NAME_HOLDERS_MAX = 3
# A domain two companies on file hold already is a shared site (a group's,
# a bank's that its branches declare), not one business's.
DOMAIN_HOLDERS_MAX = 1
# The keys that are a way to reach a business: counted by how many firms
# (legal identities) hold them, not only by how many records do.
CONTACT_KEYS = ("phone", "email", "domain")
# One registered firm's phone or email on at most this many records without
# a CUI: they are its branches, merged on their own (each becomes one of its
# locations). More than that is put to a person as one group: a franchise's
# call-centre number on fifty listings must not fold them all into the
# first franchisee whose CUI arrived.
BRANCHES_AUTO_MAX = 3
# A contact two or three firms share puts a record without a CUI up for
# review against each. Held by more firms, it is a chain's switchboard or an
# accountant's number: it says nothing about which of them a record is.
SHARED_FIRMS_REVIEW_MAX = 3
# As sure as a question gets: what a review-only match is held under.
REVIEW_CAP = round(ATTACH - 0.01, 2)
# Sites many businesses link to as "their website": booking platforms, link
# shorteners and link-in-bio pages, and company directories. A subdomain
# counts as its parent (maps.app.goo.gl is goo.gl). Free-mail, social and
# site-builder domains are left out by discover.is_junk already.
SHARED_DOMAINS = frozenset({
    "bit.ly", "goo.gl", "tinyurl.com", "t.ly", "ow.ly", "rebrand.ly", "cutt.ly",
    "linktr.ee", "lnk.bio", "bio.link", "beacons.ai", "wa.me", "m.me", "g.page",
    "booking.com", "airbnb.com", "calendly.com", "booksy.com", "fresha.com",
    "clicksanitas.ro", "doctorul.ro",
    "listafirme.ro", "termene.ro", "cylex.ro", "paginiaurie.ro",
})
# Company columns a merge may fill on the survivor when it has none.
MERGE_FILL = ("cui", "external_id", "domain", "website", "phone", "address",
              "location", "industry")
# Of those, the ones only one company may hold: moved, not copied.
UNIQUE_COLUMNS = ("cui", "external_id", "domain")


class MergeRefused(ValueError):
    """A merge or an undo that can't be done. ``code`` says why."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class Match:
    company_id: str | None = None
    score: float = 0.0
    # [{"code": "phone", "value": "+40..."}, ...], in plain words on screen
    reasons: list[dict] = field(default_factory=list)
    # Matched on a key the record holds itself (CUI, listing id, domain):
    # the same record, not two to merge.
    same_record: bool = False
    # The incoming record's keys the matched company doesn't hold yet.
    new_ids: list[tuple[str, str]] = field(default_factory=list)
    # A same-record match through a merged company: that company, the one
    # the record really is before the merge ("" otherwise).
    origin: str = ""
    # Other companies the record may be, each a question of its own:
    # [(company_id, score, reasons)], when the best isn't sure enough.
    others: list[tuple[str, float, list[dict]]] = field(default_factory=list)

    @property
    def attach(self) -> bool:
        return bool(self.company_id) and self.score >= ATTACH

    @property
    def review(self) -> bool:
        return bool(self.company_id) and REVIEW <= self.score < ATTACH


# ── Keys ──

@lru_cache(maxsize=8192)
def usable_domain(domain: str) -> bool:
    """A domain that identifies one business: not a free mailbox provider,
    a directory, a social network or a site builder."""
    from harvey.collectors.discover import is_junk
    from harvey.state import FREE_MAIL_DOMAINS

    domain = (domain or "").strip().lower()
    if not domain or domain in FREE_MAIL_DOMAINS or is_junk(domain):
        return False
    parts = domain.split(".")
    return not any(".".join(parts[i:]) in SHARED_DOMAINS for i in range(len(parts) - 1))


@lru_cache(maxsize=8192)
def phone_key(raw: str, location: str = "", domain: str = "") -> str:
    """A phone number in E.164, read in the company's country; "" when it
    isn't a valid number (one already in E.164 is checked too)."""
    from harvey.collectors.phones import phone_hit, phone_region

    raw = (raw or "").strip()
    if not raw:
        return ""
    hit = phone_hit(raw, phone_region(location, None, domain))
    return hit.number if hit else ""


def usable_email(value: str) -> bool:
    """An email address that identifies one business: not on a free-mail
    provider (a gmail.com or yahoo.ro mailbox is anyone's), a directory or a
    shared site."""
    local, _, domain = (value or "").strip().lower().rpartition("@")
    return bool(local) and usable_domain(domain)


def cui_key(raw: str) -> str:
    """A CUI as a key: digits only, without the RO prefix or leading zeros."""
    return re.sub(r"\D", "", (raw or "").upper().removeprefix("RO")).lstrip("0")


def identity(ids: dict[str, set[str]]) -> tuple[str, str] | None:
    """The firm a company is legally (its CUI, else its registration number),
    or None for a record without one (a map listing, a website)."""
    for kind in LEGAL:
        if ids.get(kind):
            return kind, min(ids[kind])
    return None


def _firm_of(cui: str, reg: str) -> tuple[str, str] | None:
    cui = cui_key(cui)
    return ("cui", cui) if cui else (("reg", reg) if reg else None)


def identifiers(company, extra_ids=()) -> list[tuple[str, str]]:
    """Every (kind, value) a company is known by: its CUI, registration
    number, listing ids, domain, phone, and name in its town."""
    get = (lambda k: company.get(k) or "") if isinstance(company, dict) \
        else (lambda k: getattr(company, k, "") or "")
    out: list[tuple[str, str]] = []
    cui = cui_key(get("cui"))
    if cui:
        out.append(("cui", cui))
    for external in (get("external_id"), *extra_ids):
        if not external:
            continue
        out.append(("external", external))
        if external.startswith("onrc:") and len(external) > 5:
            out.append(("reg", external[5:]))
    domain = get("domain").strip().lower()
    if domain and usable_domain(domain):
        out.append(("domain", domain))
    try:
        phone = phone_key(get("phone"), get("location"), domain)
    except Exception:
        phone = ""
    if phone:
        out.append(("phone", phone))
    name, place = name_key(get("name")), locality_key(get("location"))
    if name and place:
        out.append(("name", f"{name}@{place}"))
    return list(dict.fromkeys(out))


async def register(db, company_id: str, ids, source: str = "",
                   confidence: float = 1.0) -> None:
    """Record keys for a company. Keys only add up: one it was known by
    before stays, beside the new one."""
    if company_id and ids:
        await db.executemany(
            "INSERT OR IGNORE INTO company_ids (company_id, kind, value, source, confidence) "
            "VALUES (?, ?, ?, ?, ?)",
            [(company_id, kind, value, source or "", confidence) for kind, value in ids])


async def register_company(db, company_id: str) -> None:
    """Record the keys a company's own columns hold now (after a fill or a
    rename), beside those it had."""
    async with db.execute(
        "SELECT name, cui, external_id, domain, phone, location, source "
        "FROM companies WHERE id = ?", (company_id,)
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return
        names = [c[0] for c in cur.description]
    row = dict(zip(names, row))
    await register(db, company_id, identifiers(row), row.get("source") or "")


# ── Matching ──

async def live_id(db, company_id: str) -> str:
    """The company a record stands for now: itself, or the one it was
    merged into."""
    seen = set()
    while company_id and company_id not in seen:
        seen.add(company_id)
        async with db.execute("SELECT merged_into FROM companies WHERE id = ?",
                              (company_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return ""
        if not row[0]:
            return company_id
        company_id = row[0]
    return company_id


async def ids_of(db, company_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    async with db.execute("SELECT kind, value FROM company_ids WHERE company_id = ?",
                          (company_id,)) as cur:
        for kind, value in await cur.fetchall():
            out.setdefault(kind, set()).add(value)
    return out


def conflict(mine: dict[str, set[str]], theirs: dict[str, set[str]], legal=LEGAL) -> str:
    """The legal key two companies hold different values of ("cui", "reg"),
    or "" when nothing says they are different companies."""
    for kind in legal:
        if mine.get(kind) and theirs.get(kind) and not mine[kind] & theirs[kind]:
            return kind
    return ""


def _combined(weights) -> float:
    miss = 1.0
    for w in weights:
        miss *= 1.0 - w
    return round(1.0 - miss, 2)


def _score(found: dict[str, tuple[float, str]], mine: dict[str, set[str]],
           theirs: dict[str, set[str]], legal=LEGAL):
    """(score, reasons, evidence used) for two companies sharing ``found``
    ({code: (weight, value)}), or None when nothing counts."""
    if conflict(mine, theirs, legal):
        return None
    reasons = []
    if "name" in found and mine.get("domain") and theirs.get("domain") \
            and not mine["domain"] & theirs["domain"]:
        # Same name, different websites: two branches or two businesses
        # that happen to share a name, so the name says nothing.
        found = {k: v for k, v in found.items() if k != "name"}
        reasons.append({"code": "different_websites"})
    if not found:
        return None
    score = _combined(w for w, _ in found.values())
    reasons = [{"code": code, "value": value}
               for code, (_, value) in sorted(found.items(), key=lambda kv: -kv[1][0])] + reasons
    return score, reasons, found


def _contact_rule(kind: str, live: set[str], firm: dict, my_firm=None,
                  declared_domain: bool = False) -> tuple[list[str], str]:
    """Which of ``live`` (the companies holding a key the record has) the
    key is evidence for, and how: ("boost": as sure as a merge, "many" or
    "shared": a question only, "": its weight). ``firm`` gives each holder's
    legal identity, ``my_firm`` the record's own (see CONTACT_KEYS)."""
    if kind == "name":
        return (sorted(live) if len(live) <= NAME_HOLDERS_MAX else []), ""
    if kind not in CONTACT_KEYS:                # the record's own keys
        return sorted(live), ""
    firms = {firm.get(cid) for cid in live} - {None}
    unregistered = [cid for cid in live if firm.get(cid) is None]
    every_firm = firms | ({my_firm} if my_firm else set())
    if not every_firm:
        cap = {"phone": PHONE_HOLDERS_MAX, "email": EMAIL_HOLDERS_MAX,
               "domain": DOMAIN_HOLDERS_MAX}[kind]
        return (sorted(live) if len(live) <= cap else []), ""
    if len(every_firm) == 1:
        # One firm's contact: the records without a CUI holding it are its
        # branches (a declared website stays a question: it is unchecked).
        branches = len(unregistered) + (0 if my_firm else 1)
        flag = "" if declared_domain else (
            "boost" if kind == "domain" or branches <= BRANCHES_AUTO_MAX else "many")
        if my_firm:         # the firm itself arriving: its branches on file
            targets = [cid for cid in live if firm.get(cid) in (None, my_firm)]
        else:               # a branch arriving: the firm on file
            targets = [cid for cid in live if firm.get(cid)]
        return sorted(targets), flag
    if my_firm is None and len(firms) <= SHARED_FIRMS_REVIEW_MAX:
        return sorted(cid for cid in live if firm.get(cid)), "shared"
    return [], ""


def _flagged(score: float, reasons: list[dict], flags) -> tuple[float, list[dict]]:
    """A score as the contact rules leave it: a question held under ATTACH,
    or a firm's branch raised to it."""
    if "shared" in flags or "many" in flags or "site" in flags:
        code = "shared_contact" if "shared" in flags else (
            "many_listings" if "many" in flags else "shared_site")
        return min(max(score, REVIEW), REVIEW_CAP), reasons + [{"code": code}]
    if "boost" in flags and score < ATTACH:
        return ATTACH, reasons + [{"code": "one_firm"}]
    return score, reasons


async def match(db, company, extra_ids=(), declared_domain: bool = False) -> Match:
    """The company on file this record is, with how sure and why. Reads
    only. ``declared_domain``: the record's website is self-declared (a
    registry row), worth a review, not a match on its own."""
    ids = identifiers(company, extra_ids)
    if not ids:
        return Match()
    # Every holder of every key, and every key those holders have, in one
    # query (a registry import matches hundreds of thousands of rows:
    # round trips are its cost).
    where = " OR ".join("(kind = ? AND value = ?)" for _ in ids)
    async with db.execute(
        "SELECT i.company_id, i.kind, i.value, COALESCE(c.merged_into, '') "
        "FROM company_ids i JOIN companies c ON c.id = i.company_id "
        f"WHERE i.company_id IN (SELECT company_id FROM company_ids WHERE {where})",
        [x for pair in ids for x in pair]
    ) as cur:
        rows = await cur.fetchall()
    wanted = set(ids)
    holders: dict[tuple[str, str], set[str]] = {}
    theirs_all: dict[str, dict[str, set[str]]] = {}
    via_merged: set[str] = set()
    # live company -> the merged rows holding one of the record's own keys
    merged_owner: dict[str, set[str]] = {}
    for cid, kind, value, merged_into in rows:
        live = await live_id(db, cid) if merged_into else cid
        if not live:
            continue
        if live != cid:
            via_merged.add(live)
        if (kind, value) in wanted:
            holders.setdefault((kind, value), set()).add(live)
            if live != cid and kind in SAME_RECORD:
                merged_owner.setdefault(live, set()).add(cid)
        if live == cid:
            theirs_all.setdefault(cid, {}).setdefault(kind, set()).add(value)
    for cid in via_merged:                 # reached through a merged record
        theirs_all[cid] = await ids_of(db, cid)
    best = decide(ids, holders, theirs_all, merged_owner, declared_domain)
    if best.same_record and not best.origin:
        best.origin = await _moved_from(db, [pair for pair in ids if pair[0] in SAME_RECORD])
    return best


def decide(ids, holders: dict[tuple[str, str], set[str]],
           theirs_all: dict[str, dict[str, set[str]]], merged_owner: dict[str, set[str]],
           declared_domain: bool = False, legal=LEGAL) -> Match:
    """The match itself, from what is on file: ``holders`` (each of the
    record's keys -> the live companies holding it), ``theirs_all`` (each
    such company -> every key it holds), ``merged_owner`` (a live company
    -> its merged records that hold one of the record's own keys). Pure:
    both schemas gather these their own way. ``legal``: the kinds two
    different values of which are two companies."""
    mine: dict[str, set[str]] = {}
    for kind, value in ids:
        mine.setdefault(kind, set()).add(value)
    my_firm = identity(mine)
    firm = {cid: identity(theirs_all.get(cid, {})) for live in holders.values() for cid in live}
    evidence: dict[str, dict[str, tuple[float, str]]] = {}
    flags: dict[str, set[str]] = {}
    for (kind, value), live in holders.items():
        code = "declared_domain" if kind == "domain" and declared_domain else kind
        targets, flag = _contact_rule(kind, live, firm, my_firm, declared_domain)
        for cid in targets:
            evidence.setdefault(cid, {})[code] = (WEIGHTS[code], value)
            if flag:
                flags.setdefault(cid, set()).add(flag)
    if not evidence:
        return Match(new_ids=list(ids))

    scored = []
    for cid in sorted(evidence):
        result = _score(evidence[cid], mine, theirs_all.get(cid, {}), legal)
        if result is None:
            continue
        score, reasons = _flagged(result[0], result[1], flags.get(cid, ()))
        scored.append((score, cid, reasons, result[2]))
    if my_firm is None:
        # A record without a CUI that may be any of several firms is a
        # question for each, never merged into one of them on its own.
        firms = {firm.get(cid) for score, cid, _, _ in scored if score >= REVIEW} - {None}
        if len(firms) > 1:
            scored = [(min(score, REVIEW_CAP), cid,
                       reasons + ([{"code": "several_firms"}] if score >= REVIEW else []), found)
                      for score, cid, reasons, found in scored]
    best = Match(new_ids=list(ids))
    for score, cid, reasons, found in scored:
        if score > best.score:
            held = {pair for pair, live in holders.items() if cid in live}
            same = any(k in SAME_RECORD for k in found)
            best = Match(cid, score, reasons, same,
                         [pair for pair in ids if pair not in held],
                         min(merged_owner.get(cid, ())) if same and merged_owner.get(cid) else "")
    if not best.attach:
        best.others = [(cid, score, reasons) for score, cid, reasons, _ in scored
                       if cid != best.company_id and REVIEW <= score < ATTACH]
    return best


async def _moved_from(db, keys) -> str:
    """The company one of ``keys`` belonged to before a merge (still in
    force) moved it onto the survivor, or "": a record matched by such a
    key is that company, merged. Through a chain of merges (A into S, S
    into T) the earliest merge names the key's first owner, A."""
    for kind, value in keys:
        async with db.execute(
            "SELECT m.merged FROM company_merge_moves mm JOIN company_merges m ON m.id = mm.merge_id "
            "WHERE mm.tbl = 'company_ids' AND mm.key1 = ? AND mm.key2 = ? AND m.undone_at IS NULL "
            "ORDER BY m.id ASC LIMIT 1", (kind, value)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row[0]
    return ""


# ── Matching on the overlay (schema 25) ──
#
# The same rule over Harvey's own firm ids: identifiers are firm_ident rows
# under the schemes modules declare (overlay/idents.py), domains a firm's
# own column or a key Harvey found (firm_key), a phone's holders the firms
# linking it, a name in its town a key of a firm no register describes or a
# register's own name index (counted over the firms in Harvey only). Only a
# legal scheme's keys keep two firms apart (ident_scheme.legal: a tax code;
# a registration number is matched on, never a reason to refuse). Ids in
# the Match are firm ids as text, like every company id the callers pass.

def match_firm(con, company, extra_ids=(), declared_domain: bool = False, *, schemes,
               registers=()) -> Match:
    """match() on an overlay connection. Reads only. ``schemes``: the
    database's Schemes; ``registers``: register adapters attached to
    ``con`` (their name index answers the name in its town)."""
    import harvey.overlay.queries  # noqa: F401  (registers the statements read below)

    ids = identifiers(company, extra_ids)
    if not ids:
        return Match()
    record_holders: dict[tuple[str, str], set[int]] = {}
    for kind, value in ids:
        found = _overlay_holders(con, schemes, kind, value, company, registers)
        if found:
            record_holders[(kind, value)] = found
    records = {r for found in record_holders.values() for r in found}
    live = _overlay_live(con, records)
    holders: dict[tuple[str, str], set[str]] = {}
    merged_owner: dict[str, set[str]] = {}
    for (kind, value), found in record_holders.items():
        for record in found:
            to = live.get(record)
            if not to:
                continue
            holders.setdefault((kind, value), set()).add(str(to))
            if to != record and kind in SAME_RECORD:
                merged_owner.setdefault(str(to), set()).add(str(record))
    for pair in [p for p, held in holders.items()
                 if p[0] in CONTACT_KEYS and len(held) > SHARED_FIRMS_REVIEW_MAX]:
        if _shared_by_many_firms(con, schemes, holders[pair]):
            # A switchboard's number says nothing (_contact_rule): the keys
            # of its thousands of holders aren't gathered for nothing.
            del holders[pair]
    theirs_all = {cid: _overlay_keys(con, schemes, int(cid))
                  for cid in {c for held in holders.values() for c in held}}
    return decide(ids, holders, theirs_all, merged_owner, declared_domain,
                  legal=schemes.legal_kinds())


def _shared_by_many_firms(con, schemes, live: set[str]) -> bool:
    """Whether more than SHARED_FIRMS_REVIEW_MAX firms (legal identities)
    hold a contact among the live firms ``live``: then it is evidence for
    none of them, whatever the record is. Stops reading at the first
    firm past the limit."""
    from harvey.queries import sql

    firms = set()
    for cid in sorted(live, key=int):
        ids: dict[str, set[str]] = {}
        for scheme, key in con.execute(sql("overlay.resolve.group_idents"), (int(cid), int(cid))):
            for kind, value in schemes.kinds_of(scheme, key):
                ids.setdefault(kind, set()).add(value)
        firm = identity(ids)
        if firm is not None:
            firms.add(firm)
            if len(firms) > SHARED_FIRMS_REVIEW_MAX:
                return True
    return False


def _overlay_live(con, records) -> dict[int, int]:
    from harvey.queries import sql

    out = {}
    for record in records:
        row = con.execute(sql("overlay.resolve.live"), (record,)).fetchone()
        if row:
            out[record] = row[0]
    return out


def _overlay_holders(con, schemes, kind: str, value: str, company, registers) -> set[int]:
    """The records (firm ids, merged or not) holding one key of a record."""
    from harvey.overlay.schema import FIRM_KEY, PHONE
    from harvey.overlay.triggers import active
    from harvey.queries import sql

    out: set[int] = set()
    if kind in ("cui", "reg", "external"):
        key = schemes.key(kind, value)
        if key is not None:
            out |= {r[0] for r in con.execute(sql("overlay.resolve.ident"), key)}
    elif kind == "domain":
        out |= {r[0] for r in con.execute(sql("overlay.resolve.domain"), (value,))}
        out |= {r[0] for r in con.execute(sql("overlay.resolve.key"),
                                          (FIRM_KEY["domain"], value))}
    elif kind in ("email", "name"):
        out |= {r[0] for r in con.execute(sql("overlay.resolve.key"), (FIRM_KEY[kind], value))}
    elif kind == "phone":
        out |= {r[0] for r in con.execute(sql("overlay.resolve.phone"), (PHONE, value))}
        if active(con):
            # A bulk write links its contacts only at its recount: the
            # evidence it added so far holds them already.
            out |= {r[0] for r in con.execute(
                "SELECT t.firm FROM temp.touched_link t JOIN contact c ON c.id = t.contact "
                "WHERE c.kind = ? AND c.value = ?", (PHONE, value))}
    if kind == "name":
        out |= _register_name_holders(con, schemes, company, registers)
    return out


# Registers whose stale name index was reported (once per process).
_STALE_NAMES_LOGGED: set[str] = set()


def _register_name_holders(con, schemes, company, registers) -> set[int]:
    """The firms in Harvey a register names this way in this town."""
    from harvey.queries import sql
    from harvey.registers import Stale

    get = (lambda k: company.get(k) or "") if isinstance(company, dict) \
        else (lambda k: getattr(company, k, "") or "")
    out: set[int] = set()
    attached = {r[1] for r in con.execute("PRAGMA database_list")}
    for adapter in registers:
        if adapter.alias not in attached or not hasattr(adapter, "name_holders"):
            continue
        try:
            reg_ids = adapter.name_holders(con, get("name"), get("location"))
        except Stale as e:
            if adapter.module not in _STALE_NAMES_LOGGED:
                _STALE_NAMES_LOGGED.add(adapter.module)
                logger.warning("matching by name in %s is off until its name index is rebuilt "
                               "(reload the register): %s", adapter.module, e)
            continue
        if not reg_ids or len(reg_ids) > 50:
            # More than a chain's worth: says nothing about which it is.
            continue
        for scheme_name, key in adapter.keys_of_ids(con, reg_ids):
            scheme = schemes.named(scheme_name)
            if scheme is not None:
                out |= {r[0] for r in con.execute(sql("overlay.resolve.ident"), (scheme.id, key))}
    return out


def _overlay_keys(con, schemes, live: int) -> dict[str, set[str]]:
    """Every key the records of live firm ``live`` hold, as the resolver's kinds."""
    from harvey.overlay.schema import FIRM_KEY
    from harvey.queries import sql

    names = {v: k for k, v in FIRM_KEY.items()}
    out: dict[str, set[str]] = {}
    for scheme, key in con.execute(sql("overlay.resolve.group_idents"), (live, live)):
        for kind, value in schemes.kinds_of(scheme, key):
            out.setdefault(kind, set()).add(value)
    for kind, value in con.execute(sql("overlay.resolve.group_keys"), (live, live)):
        out.setdefault(names.get(kind, str(kind)), set()).add(value)
    for (domain,) in con.execute(sql("overlay.resolve.group_domains"), (live, live)):
        out.setdefault("domain", set()).add(domain)
    return out


async def scan(db) -> dict:
    """Score the companies already on file against each other (the keys of
    a database from before the resolver arrive all at once, and a contact
    found after a company was saved, an email say, was never matched). The
    same rules as a new record: ATTACH or more is merged (undoable), REVIEW
    to ATTACH goes to the review list. Never commits."""
    return await apply_scan(db, await scan_candidates(db))


async def scan_candidates(db) -> list[tuple[float, str, str, list[dict]]]:
    """The pairs on file that score REVIEW or more: (score, a, b, reasons),
    most likely first. Reads only, so a caller can do this outside its write
    transaction and hold the write lock for apply_scan alone."""
    shared: dict[tuple[str, str], dict[str, tuple[float, str]]] = {}
    flags: dict[tuple[str, str], set[str]] = {}
    firm: dict[str, tuple[str, str] | None] = {}
    async with db.execute(
        "SELECT i.kind, i.value, group_concat(i.company_id || char(30) || COALESCE(c.cui, '') "
        "|| char(30) || CASE WHEN c.external_id LIKE 'onrc:_%' THEN substr(c.external_id, 6) "
        "ELSE '' END, char(31)) "
        "FROM company_ids i JOIN companies c ON c.id = i.company_id "
        "WHERE i.kind IN ('phone', 'name', 'domain', 'email') "
        "AND COALESCE(c.merged_into, '') = '' "
        "GROUP BY i.kind, i.value HAVING COUNT(DISTINCT i.company_id) > 1"
    ) as cur:
        async for kind, value, packed in cur:
            if kind == "email" and not usable_email(value):
                continue
            here: dict[str, tuple[str, str] | None] = {}
            for part in packed.split("\x1f"):
                cid, cui, reg = (part.split("\x1e") + ["", ""])[:3]
                here[cid] = _firm_of(cui, reg)
            pairs, flag = _scan_pairs(kind, here)
            if not pairs:
                continue
            firm.update(here)
            for a, b in pairs:
                key = (a, b) if a < b else (b, a)
                shared.setdefault(key, {})[kind] = (WEIGHTS[kind], value)
                if flag:
                    flags.setdefault(key, set()).add(flag)
    known: dict[str, dict[str, set[str]]] = {}

    async def keys(cid):
        if cid not in known:
            known[cid] = await ids_of(db, cid)
        return known[cid]

    scored = []
    for (a, b), found in shared.items():
        result = _score(found, await keys(a), await keys(b))
        if result is None:
            continue
        score, reasons = _flagged(result[0], result[1], flags.get((a, b), ()))
        if score >= REVIEW:
            scored.append((score, a, b, reasons))
    # A record without a CUI that may be one of several firms: a question
    # for each, never merged into one of them on its own.
    firms_of: dict[str, set] = {}
    for _, a, b, _ in scored:
        if (firm.get(a) is None) != (firm.get(b) is None):
            loose, held = (a, b) if firm.get(a) is None else (b, a)
            firms_of.setdefault(loose, set()).add(firm[held])
    torn = {cid for cid, firms in firms_of.items() if len(firms) > 1}
    if torn:
        scored = [(min(score, REVIEW_CAP), a, b, reasons + [{"code": "several_firms"}])
                  if (a in torn and firm.get(b)) or (b in torn and firm.get(a))
                  else (score, a, b, reasons) for score, a, b, reasons in scored]
    # The same through a chain of sure pairs: L1 is only F1's, L2 only F2's,
    # and L1 and L2 are one business. Merged pair by pair, both would end in
    # whichever firm came first, and the other firm's pair would meet a CUI
    # conflict and vanish. A connected group of sure pairs that reaches two
    # firms is a question as a whole.
    scored = _hold_chains(scored, firm)
    return sorted(scored, key=lambda x: (-x[0], x[1], x[2]))


def _hold_chains(scored, firm: dict) -> list:
    """``scored`` with every sure pair (ATTACH or more) of a connected group
    of sure pairs that holds more than one firm held under ATTACH, so a
    person decides it (see scan_candidates)."""
    parent: dict[str, str] = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for score, a, b, _ in scored:
        if score >= ATTACH:
            parent[root(a)] = root(b)
    firms_in: dict[str, set] = {}
    for cid in list(parent):
        if firm.get(cid):
            firms_in.setdefault(root(cid), set()).add(firm[cid])
    held = {r for r, firms in firms_in.items() if len(firms) > 1}
    if not held:
        return scored
    out = []
    for score, a, b, reasons in scored:
        if score >= ATTACH and root(a) in held:
            score = REVIEW_CAP
            if not any(r.get("code") == "several_firms" for r in reasons):
                reasons = reasons + [{"code": "several_firms"}]
        out.append((score, a, b, reasons))
    return out


def _scan_pairs(kind: str, here: dict) -> tuple[list[tuple[str, str]], str]:
    """The pairs one shared key makes among the companies holding it
    (``here``: company -> its firm or None), and how they count."""
    holders = sorted(here)
    every = [(a, b) for i, a in enumerate(holders) for b in holders[i + 1:]]
    if kind == "name":
        return (every if len(holders) <= NAME_HOLDERS_MAX else []), ""
    firms = {f for f in here.values() if f}
    loose = [cid for cid in holders if not here[cid]]
    if not firms:
        # Records without a CUI: a phone or email two of them hold is
        # evidence. A website two of them hold is a question only: on file
        # already, they were not one record when they were saved (a group's
        # site, a page two businesses share).
        cap = {"phone": PHONE_HOLDERS_MAX, "email": EMAIL_HOLDERS_MAX, "domain": 2}[kind]
        return (every if len(holders) <= cap else []), ("site" if kind == "domain" else "")
    held = [cid for cid in holders if here[cid]]
    if len(firms) == 1:
        flag = "boost" if kind == "domain" or len(loose) <= BRANCHES_AUTO_MAX else "many"
        return [(f, cid) for f in held for cid in loose], flag
    if len(firms) <= SHARED_FIRMS_REVIEW_MAX:
        return [(f, cid) for f in held for cid in loose], "shared"
    return [], ""


async def _decided(db, a: str, b: str) -> str:
    """What a person already said about two companies: "separate", "undone"
    (a merge of the two was undone: never merged on its own again), or ""."""
    async with db.execute(
        "SELECT 1 FROM match_candidates WHERE status = 'separate' AND ((company_a = ? AND "
        "company_b = ?) OR (company_a = ? AND company_b = ?))", (a, b, b, a)
    ) as cur:
        if await cur.fetchone():
            return "separate"
    async with db.execute(
        "SELECT 1 FROM company_merges WHERE undone_at IS NOT NULL AND ((survivor = ? AND "
        "merged = ?) OR (survivor = ? AND merged = ?))", (a, b, b, a)
    ) as cur:
        return "undone" if await cur.fetchone() else ""


async def keeper_order(db, ids) -> list[str]:
    """Live companies among ``ids`` in the order one keeps the record: the
    registry's (it has a CUI) first, then the one holding most (contacts
    and facts), then the older, as when a person says "same company"."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    async with db.execute(
        f"SELECT c.id, COALESCE(c.cui, '') = '', "
        f"(SELECT COUNT(*) FROM company_contacts l WHERE l.company_id = c.id) + "
        f"(SELECT COUNT(*) FROM observations o WHERE o.company_id = c.id), c.created_at, c.rowid "
        f"FROM companies c WHERE c.id IN ({marks}) AND COALESCE(c.merged_into, '') = ''", ids
    ) as cur:
        rows = await cur.fetchall()
    rows.sort(key=lambda r: (r[1], -r[2], r[3] or "", r[4]))
    return [r[0] for r in rows]


async def unknown_pairs(db, scored) -> list:
    """``scored`` less the pairs the review list holds already (a question
    asked, or answered "different"), unless sure enough to merge and still
    undecided: what apply_scan has to look at under the write lock. Reads only."""
    async with db.execute("SELECT company_a, company_b, status FROM match_candidates") as cur:
        known = {frozenset((a, b)): status for a, b, status in await cur.fetchall()}
    return [row for row in scored
            if frozenset(row[1:3]) not in known
            or (row[0] >= ATTACH and known[frozenset(row[1:3])] == "pending")]


async def apply_scan(db, scored) -> dict:
    """Merge or queue for review what scan_candidates found. Each pair is
    followed to the companies its sides stand for now: when an earlier pair
    of the same pass merged one side into a third company, the pair is that
    company's (it used to be dropped, so a record matching two others that
    merged was never merged or asked about). Pairs a person decided are
    left alone. Never commits."""
    merged = review = 0
    for score, a, b, reasons in scored:
        a, b = await live_id(db, a), await live_id(db, b)
        if not a or not b or a == b:
            continue
        if conflict(await ids_of(db, a), await ids_of(db, b)):
            continue
        decided = await _decided(db, a, b)
        if decided == "separate":
            continue
        order = await keeper_order(db, (a, b))
        if len(order) != 2:
            continue
        if score < ATTACH or decided == "undone":
            # Counted only when asked for the first time: a pair waiting on
            # the list already is not news on every look.
            review += await add_candidate(db, order[0], order[1], score, reasons)
            continue
        try:
            await merge(db, order[0], order[1], score=score, reasons=reasons,
                        decided_by="auto")
            merged += 1
        except MergeRefused:
            continue
    return {"merged": merged, "review": review}


# ── Review list ──

async def add_candidate(db, company_a: str, company_b: str, score: float,
                        reasons: list[dict]) -> bool:
    """"Is B the same company as A?", for a person to answer. One row per
    pair, whichever way round it was asked. True when the question is new."""
    async with db.execute(
        "SELECT 1 FROM match_candidates WHERE (company_a = ? AND company_b = ?) "
        "OR (company_a = ? AND company_b = ?)", (company_a, company_b, company_b, company_a)
    ) as cur:
        if await cur.fetchone():
            return False
    await db.execute(
        "INSERT INTO match_candidates (company_a, company_b, score, reasons_json) "
        "VALUES (?, ?, ?, ?)", (company_a, company_b, score, json.dumps(reasons)))
    return True


async def decide_candidate(db, a: str, b: str, status: str) -> int:
    cur = await db.execute(
        "UPDATE match_candidates SET status = ?, decided_at = CURRENT_TIMESTAMP "
        "WHERE (company_a = ? AND company_b = ?) OR (company_a = ? AND company_b = ?)",
        (status, a, b, b, a))
    return cur.rowcount


# ── Merge and undo ──

async def _company_row(db, company_id: str) -> dict | None:
    async with db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)) as cur:
        row = await cur.fetchone()
        if not row:
            return None
        names = [c[0] for c in cur.description]
    return dict(zip(names, row))


async def merge(db, survivor: str, merged: str, *, score: float | None = None,
                reasons: list[dict] | None = None, decided_by: str = "you") -> int:
    """Merge ``merged`` into ``survivor`` and return the merge's id. Never
    commits. Refused (MergeRefused) for a company merged already, or for
    two companies with different CUIs or registration numbers."""
    if survivor == merged:
        raise MergeRefused("same_company")
    keep, gone = await _company_row(db, survivor), await _company_row(db, merged)
    if keep is None or gone is None:
        raise MergeRefused("not_found")
    if keep.get("merged_into") or gone.get("merged_into"):
        raise MergeRefused("already_merged")
    kind = conflict(await ids_of(db, merged), await ids_of(db, survivor))
    if kind:
        raise MergeRefused("different_" + kind)

    cur = await db.execute(
        "INSERT INTO company_merges (survivor, merged, score, reasons_json, decided_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (survivor, merged, score, json.dumps(reasons or []), decided_by))
    merge_id = cur.lastrowid

    await _move(db, merge_id, survivor, merged, "observations")
    await _move(db, merge_id, survivor, merged, "prospects")
    await _move_keys(db, merge_id, survivor, merged)
    places, loose = await _places_before(db, merged)
    # People, contact links, places and sources (harvey/contacts.py): each
    # row says whom it was found for, so these need no log of their own.
    await contacts.merge(db, survivor, merged, merge_id)
    if not places and branch_place(keep, gone):
        await _as_location(db, merge_id, survivor, merged, gone, loose)
    await _carry_separate(db, merge_id, survivor, merged)
    # Companies merged into this one earlier now stand for the survivor.
    await db.execute(
        "INSERT INTO company_merge_moves (merge_id, tbl, key1, key2, key3) "
        "SELECT ?, 'companies', id, '', '' FROM companies WHERE merged_into = ? "
        "AND merged_into != ''",
        (merge_id, merged))
    # ("!= ''" lets SQLite use the partial index on merged_into; "= ?"
    # alone can't, and reads every company on file once per merge.)
    await db.execute("UPDATE companies SET merged_into = ? WHERE merged_into = ? "
                     "AND merged_into != ''",
                     (survivor, merged))

    await _fill_columns(db, merge_id, survivor, merged, keep, gone)
    await db.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (survivor, merged))
    if not await decide_candidate(db, survivor, merged, "merged"):
        await db.execute(
            "INSERT INTO match_candidates (company_a, company_b, score, reasons_json, status, "
            "decided_at) VALUES (?, ?, ?, ?, 'merged', CURRENT_TIMESTAMP)",
            (survivor, merged, score or 0.0, json.dumps(reasons or [])))
    # Search: the merged company leaves the index, the survivor is redone.
    await db.executemany("INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (?)",
                         [(survivor,), (merged,)])
    return merge_id


async def _move(db, merge_id: int, survivor: str, merged: str, table: str,
                keys: str = "t.id, '', ''", where_free: str = "") -> None:
    """Move ``merged``'s rows of ``table`` to the survivor, each logged under
    ``merge_id`` so its undo puts back exactly those."""
    await db.execute(
        f"INSERT INTO company_merge_moves (merge_id, tbl, key1, key2, key3) "
        f"SELECT ?, '{table}', {keys} FROM {table} t WHERE t.company_id = ?{where_free}",
        (merge_id, merged, *([survivor] if where_free else [])))
    await db.execute(
        f"UPDATE {table} SET company_id = ? WHERE company_id = ? AND EXISTS ("
        f"SELECT 1 FROM company_merge_moves m WHERE m.merge_id = ? AND m.tbl = '{table}' "
        f"AND {_MOVE_MATCH[table]})", (survivor, merged, merge_id))


async def _move_keys(db, merge_id: int, survivor: str, merged: str) -> None:
    """The merged company's keys and activities: one the survivor already
    has stays where it is (parked on the merged record)."""
    await _move(db, merge_id, survivor, merged, "company_ids", "t.kind, t.value, ''",
                " AND NOT EXISTS (SELECT 1 FROM company_ids s WHERE s.company_id = ? "
                "AND s.kind = t.kind AND s.value = t.value)")
    await _move(db, merge_id, survivor, merged, "company_activities",
                "t.code, CAST(t.version AS TEXT), t.source",
                " AND NOT EXISTS (SELECT 1 FROM company_activities s WHERE s.company_id = ? "
                "AND s.code = t.code AND s.version = t.version AND s.source = t.source)")


async def _fill_columns(db, merge_id: int, survivor: str, merged: str, keep: dict,
                        gone: dict) -> None:
    """Fill the survivor's empty columns from the merged company, adding to
    what ``merge_id`` filled and moved, so its undo empties exactly those."""
    async with db.execute("SELECT filled_json, moved_json FROM company_merges WHERE id = ?",
                          (merge_id,)) as cur:
        row = await cur.fetchone()
    filled = json.loads((row and row[0]) or "{}")
    moved = json.loads((row and row[1]) or "{}")
    for column in MERGE_FILL:
        value = gone.get(column) or ""
        if not value or (keep.get(column) or "").strip():
            continue
        if column in UNIQUE_COLUMNS:
            await db.execute(f"UPDATE companies SET {column} = '' WHERE id = ?", (merged,))
            moved[column] = value
        await db.execute(f"UPDATE companies SET {column} = ?, updated_at = CURRENT_TIMESTAMP "
                         f"WHERE id = ?", (value, survivor))
        filled[column] = value
    await db.execute("UPDATE company_merges SET filled_json = ?, moved_json = ? WHERE id = ?",
                     (json.dumps(filled), json.dumps(moved), merge_id))


async def _refill(db, survivor: str) -> None:
    """After an undo, what the survivor still stands for: a row another
    company merged into it had in common with the one taken back (the same
    key, the same contact from the same source) was parked on that company
    when it merged, and an empty column it could fill was full. Each is
    moved or filled now, logged under that company's own merge, so undoing
    it later still puts back exactly what it took."""
    async with db.execute(
        "SELECT id, merged FROM company_merges WHERE survivor = ? AND undone_at IS NULL "
        "ORDER BY id", (survivor,)
    ) as cur:
        merges = await cur.fetchall()
    for merge_id, merged in merges:
        gone = await _company_row(db, merged)
        if gone is None or gone.get("merged_into") != survivor:
            continue
        await _move_keys(db, merge_id, survivor, merged)
        await contacts.refill(db, survivor, merged)
        await _fill_columns(db, merge_id, survivor, merged, await _company_row(db, survivor),
                            gone)


async def _email_keys(db, survivor: str, merged: str) -> None:
    """Email keys follow the links that give them (they are written when a
    link is, state.py's trigger): one the survivor holds with no link left
    to it went back with the link; the merged record's links are its keys."""
    if not await contacts._ready(db):
        return
    await db.execute(
        "DELETE FROM company_ids WHERE company_id = ? AND kind = 'email' AND NOT EXISTS ("
        "SELECT 1 FROM company_contacts l JOIN contacts ct ON ct.id = l.contact_id "
        "WHERE ct.kind = 'email' AND ct.value = company_ids.value AND (l.company_id = ? "
        "OR l.company_id IN (SELECT id FROM companies WHERE merged_into = ? "
        "AND merged_into != '')))", (survivor, survivor, survivor))
    await db.execute(
        "INSERT OR IGNORE INTO company_ids (company_id, kind, value, source, confidence) "
        "SELECT l.company_id, 'email', ct.value, COALESCE(MIN(l.source), ''), "
        "MAX(COALESCE(l.confidence, 1.0)) FROM company_contacts l "
        "JOIN contacts ct ON ct.id = l.contact_id WHERE l.company_id = ? AND ct.kind = 'email' "
        "GROUP BY ct.value", (merged,))


def branch_place(keep: dict, gone: dict) -> bool:
    """Is ``gone`` somewhere else than ``keep``: two street addresses that
    differ, or (without both) two towns? A branch's place is kept as one of
    the company's locations when it is merged, not lost."""
    a_keep, a_gone = " ".join(fold(keep.get("address") or "")), \
        " ".join(fold(gone.get("address") or ""))
    if a_keep and a_gone:
        return a_keep != a_gone
    t_keep, t_gone = locality_key(keep.get("location") or ""), \
        locality_key(gone.get("location") or "")
    return bool(t_keep) and bool(t_gone) and t_keep != t_gone


async def _places_before(db, merged: str) -> tuple[int, list[int]]:
    """(how many places the merged company has, its contact links tied to
    no place), read before a merge moves them."""
    if not await contacts._ready(db):
        return 0, []
    async with db.execute("SELECT COUNT(*) FROM locations WHERE company_id = ?",
                          (merged,)) as cur:
        places = (await cur.fetchone())[0]
    async with db.execute("SELECT id FROM company_contacts WHERE company_id = ? "
                          "AND location_id = 0", (merged,)) as cur:
        loose = [r[0] for r in await cur.fetchall()]
    return places, loose


async def _as_location(db, merge_id: int, survivor: str, merged: str, gone: dict,
                       loose: list[int]) -> None:
    """The merged company's own address as one of the survivor's locations,
    its contacts reached through it. Logged, so an undo takes the place away
    and unties the contacts again."""
    location_id, created = await contacts.place(
        db, survivor, name=gone.get("name") or "", address=gone.get("address") or "",
        location=gone.get("location") or "", external_id=gone.get("external_id") or "",
        phone=gone.get("phone") or "", source=gone.get("source") or "", origin=merged,
        refresh=False)
    if not location_id:
        return
    if created:
        await db.execute("INSERT INTO company_merge_moves (merge_id, tbl, key1) "
                         "VALUES (?, 'locations.created', ?)", (merge_id, str(location_id)))
    for at in range(0, len(loose), 500):
        chunk = loose[at:at + 500]
        marks = ",".join("?" * len(chunk))
        await db.execute(
            "INSERT INTO company_merge_moves (merge_id, tbl, key1, key2) "
            f"SELECT ?, 'company_contacts.location', CAST(id AS TEXT), '0' FROM company_contacts "
            f"WHERE id IN ({marks}) AND location_id = 0", (merge_id, *chunk))
        await db.execute(f"UPDATE company_contacts SET location_id = ? WHERE id IN ({marks}) "
                         "AND location_id = 0", (location_id, *chunk))


async def _carry_separate(db, merge_id: int, survivor: str, merged: str) -> None:
    """"Different companies" said of the merged company holds for the
    survivor too (it is that company now). Logged for the undo."""
    async with db.execute(
        "SELECT company_a, company_b FROM match_candidates WHERE status = 'separate' "
        "AND (company_a = ? OR company_b = ?)", (merged, merged)
    ) as cur:
        others = {b if a == merged else a for a, b in await cur.fetchall()} - {survivor, merged}
    for other in sorted(others):
        async with db.execute(
            "SELECT 1 FROM match_candidates WHERE (company_a = ? AND company_b = ?) "
            "OR (company_a = ? AND company_b = ?)", (survivor, other, other, survivor)
        ) as cur:
            if await cur.fetchone():
                continue
        await db.execute(
            "INSERT INTO match_candidates (company_a, company_b, status, decided_at) "
            "VALUES (?, ?, 'separate', CURRENT_TIMESTAMP)", (survivor, other))
        await db.execute("INSERT INTO company_merge_moves (merge_id, tbl, key1, key2) "
                         "VALUES (?, 'match_candidates', ?, ?)", (merge_id, survivor, other))


# How a logged move finds its row again (in ``move`` and in ``undo``).
_MOVE_MATCH = {
    "observations": "m.key1 = {t}.id",
    "prospects": "m.key1 = {t}.id",
    "company_ids": "m.key1 = {t}.kind AND m.key2 = {t}.value",
    "company_activities": "m.key1 = {t}.code AND m.key2 = CAST({t}.version AS TEXT) "
                          "AND m.key3 = {t}.source",
}
_MOVE_MATCH = {table: cond.format(t=table) for table, cond in _MOVE_MATCH.items()}


async def undo(db, merged: str) -> dict:
    """Undo the merge that took ``merged``: every row it moved goes back,
    and so does every fact found for it since (observations whose
    origin_company_id is it, written to the survivor after the merge); the
    columns it filled on the survivor are emptied again, and the pair goes
    back on the review list for a person to answer. Never commits. Refused
    when ``merged`` isn't merged, or when its survivor has since been
    merged itself (undo that one first)."""
    async with db.execute(
        "SELECT id, survivor, filled_json, moved_json FROM company_merges "
        "WHERE merged = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (merged,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise MergeRefused("not_merged")
    merge_id, survivor, filled, moved = row[0], row[1], json.loads(row[2] or "{}"), \
        json.loads(row[3] or "{}")
    keep, gone = await _company_row(db, survivor), await _company_row(db, merged)
    if keep is not None and keep.get("merged_into"):
        raise MergeRefused("survivor_merged")
    # Facts found for the merged record after the merge landed on the
    # survivor (the run that merged it, or a re-run that finds its listing
    # again, writes to the company it stands for). Each says which record
    # it was found for (origin_company_id): the merged one, or one merged
    # into it before, goes back.
    await db.execute(
        "UPDATE observations SET company_id = ? WHERE company_id = ? AND (origin_company_id = ? "
        "OR origin_company_id IN (SELECT key1 FROM company_merge_moves "
        "WHERE merge_id = ? AND tbl = 'companies'))",
        (merged, survivor, merged, merge_id))
    # Rows from before facts carried their origin: the merged record's own
    # listing is told by its source URL.
    source_url = ((gone or {}).get("source_url") or "").strip()
    if source_url and source_url != ((keep or {}).get("source_url") or "").strip():
        await db.execute(
            "UPDATE observations SET company_id = ? WHERE company_id = ? AND evidence_url = ? "
            "AND COALESCE(origin_company_id, '') = ''",
            (merged, survivor, source_url))

    # The branch place the merge made goes, and the contacts it tied to it
    # are tied to no place again; unless another merge still in force tied
    # its own contacts to the same place (two branches in one town): the
    # place is that merge's now.
    await _hand_over_places(db, merge_id, survivor)
    await db.execute(
        "UPDATE company_contacts SET location_id = 0 WHERE id IN (SELECT CAST(key1 AS INTEGER) "
        "FROM company_merge_moves WHERE merge_id = ? AND tbl = 'company_contacts.location')",
        (merge_id,))
    created = "(SELECT CAST(key1 AS INTEGER) FROM company_merge_moves WHERE merge_id = ? " \
              "AND tbl = 'locations.created')"
    await db.execute(f"UPDATE company_contacts SET location_id = 0 WHERE location_id IN {created}",
                     (merge_id,))
    await db.execute(f"DELETE FROM locations WHERE id IN {created}", (merge_id,))
    await db.execute(
        "DELETE FROM match_candidates WHERE status = 'separate' AND EXISTS (SELECT 1 FROM "
        "company_merge_moves m WHERE m.merge_id = ? AND m.tbl = 'match_candidates' "
        "AND m.key1 = match_candidates.company_a AND m.key2 = match_candidates.company_b)",
        (merge_id,))
    await contacts.undo(db, survivor, merged, merge_id)
    for table in ("observations", "prospects", "company_ids", "company_activities"):
        await db.execute(
            f"UPDATE OR IGNORE {table} SET company_id = ? WHERE company_id = ? AND EXISTS ("
            f"SELECT 1 FROM company_merge_moves m WHERE m.merge_id = ? AND m.tbl = '{table}' "
            f"AND {_MOVE_MATCH[table]})", (merged, survivor, merge_id))
    await db.execute(
        "UPDATE companies SET merged_into = ? WHERE id IN (SELECT key1 FROM company_merge_moves "
        "WHERE merge_id = ? AND tbl = 'companies')", (merged, merge_id))
    for column, value in filled.items():
        await db.execute(f"UPDATE companies SET {column} = '' WHERE id = ? AND {column} = ?",
                         (survivor, value))
    for column, value in moved.items():
        await db.execute(f"UPDATE companies SET {column} = ? WHERE id = ?", (value, merged))
    await db.execute("UPDATE companies SET merged_into = '' WHERE id = ?", (merged,))
    await db.execute("UPDATE company_merges SET undone_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (merge_id,))
    # A key the survivor holds in its own right again (its columns, a phone
    # its site gives) went back with the moved row: it is the survivor's too.
    await register_company(db, survivor)
    await db.execute(
        "INSERT OR IGNORE INTO company_ids (company_id, kind, value, source, confidence) "
        "SELECT company_id, 'phone', value_text, COALESCE(collector, ''), MAX(confidence) "
        "FROM observations WHERE company_id = ? AND signal_code = 'PHONE_NUMBER' "
        "AND value_text LIKE '+%' GROUP BY value_text", (survivor,))
    await _refill(db, survivor)
    await _email_keys(db, survivor, merged)
    # An undo says the merge was wrong, not that they are two companies: a
    # mistaken click must not take the pair off the list for good.
    await decide_candidate(db, survivor, merged, "pending")
    await db.executemany("INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (?)",
                         [(survivor,), (merged,)])
    return {"survivor": survivor, "merged": merged, "merge_id": merge_id}


async def _hand_over_places(db, merge_id: int, survivor: str) -> None:
    """A place ``merge_id`` created that another merge still in force into
    ``survivor`` uses too (its logged contacts point at it) passes to that
    merge: its creation is logged under it, and it says it was found for
    that merge's company."""
    async with db.execute(
        "SELECT CAST(c.key1 AS INTEGER), MIN(other.merge_id) FROM company_merge_moves c "
        "JOIN company_contacts cc ON cc.location_id = CAST(c.key1 AS INTEGER) "
        "JOIN company_merge_moves other ON other.tbl = 'company_contacts.location' "
        "AND other.key1 = CAST(cc.id AS TEXT) AND other.merge_id != ? "
        "JOIN company_merges m ON m.id = other.merge_id AND m.undone_at IS NULL "
        "AND m.survivor = ? "
        "WHERE c.merge_id = ? AND c.tbl = 'locations.created' GROUP BY c.key1",
        (merge_id, survivor, merge_id)
    ) as cur:
        handed = await cur.fetchall()
    for location_id, other in handed:
        await db.execute(
            "UPDATE company_merge_moves SET merge_id = ? WHERE merge_id = ? "
            "AND tbl = 'locations.created' AND key1 = ?", (other, merge_id, str(location_id)))
        await db.execute(
            "UPDATE locations SET origin_company_id = (SELECT merged FROM company_merges "
            "WHERE id = ?) WHERE id = ?", (other, location_id))


# ── Groups ──

async def _live_map(db, ids) -> dict[str, str]:
    """Each of ``ids`` -> the live company it stands for now ("" for none),
    following merges in a few queries however many ids there are."""
    out: dict[str, str] = {}
    parent: dict[str, str] = {}
    todo = set(ids)
    while todo:
        batch = list(todo)
        todo = set()
        for at in range(0, len(batch), 900):
            chunk = batch[at:at + 900]
            async with db.execute(
                f"SELECT id, COALESCE(merged_into, '') FROM companies "
                f"WHERE id IN ({','.join('?' * len(chunk))})", chunk
            ) as cur:
                for cid, into in await cur.fetchall():
                    parent[cid] = into
        for cid in batch:
            into = parent.get(cid)
            if into and into not in parent:
                todo.add(into)
        for cid in batch:
            parent.setdefault(cid, None)

    def resolve(cid):
        seen = set()
        while cid and parent.get(cid) and cid not in seen:
            seen.add(cid)
            cid = parent[cid]
        return cid if cid and parent.get(cid) is not None else ""
    for cid in ids:
        out[cid] = resolve(cid)
    return out


async def _firms(db, ids) -> dict[str, tuple[str, str] | None]:
    out: dict[str, tuple[str, str] | None] = {cid: None for cid in ids}
    ids = list(ids)
    for at in range(0, len(ids), 900):
        chunk = ids[at:at + 900]
        # "+kind": the lookup goes by company (idx_company_ids_company), not
        # through every CUI on file by the primary key's kind.
        async with db.execute(
            f"SELECT company_id, kind, value FROM company_ids WHERE +kind IN ('cui', 'reg') "
            f"AND company_id IN ({','.join('?' * len(chunk))})", chunk
        ) as cur:
            held: dict[str, dict[str, set[str]]] = {}
            for cid, kind, value in await cur.fetchall():
                held.setdefault(cid, {}).setdefault(kind, set()).add(value)
        for cid, keys in held.items():
            out[cid] = identity(keys)
    return out


async def groups(db) -> list[dict]:
    """The review list as groups: every pending pair, each side followed to
    the company it stands for now (a pair whose sides are one company since
    a merge is gone; one a person said are different, or two firms with
    different CUIs, is left out), joined into connected groups. Each group:
    {"members": [ids], "pairs": [{"a", "b", "score", "reasons"}], "score":
    the highest}, most likely first, then largest."""
    async with db.execute(
        "SELECT company_a, company_b, score, reasons_json, status FROM match_candidates "
        "WHERE status IN ('pending', 'separate')"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return []
    live = await _live_map(db, {x for r in rows for x in r[:2]})
    apart = {frozenset((live[a], live[b])) for a, b, _, _, status in rows
             if status == "separate" and live[a] and live[b]}
    edges: dict[frozenset, dict] = {}
    for a, b, score, reasons_json, status in rows:
        if status != "pending":
            continue
        a, b = live[a], live[b]
        key = frozenset((a, b))
        if not a or not b or a == b or key in apart:
            continue
        try:
            reasons = json.loads(reasons_json or "[]")
        except (json.JSONDecodeError, TypeError):
            reasons = []
        edge = edges.setdefault(key, {"a": min(a, b), "b": max(a, b), "score": 0.0,
                                      "reasons": []})
        if (score or 0) > edge["score"]:
            edge["score"] = round(score or 0, 2)
        for r in reasons:
            if r not in edge["reasons"]:
                edge["reasons"].append(r)
    if not edges:
        return []
    firm = await _firms(db, {x for key in edges for x in key})
    parent: dict[str, str] = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    kept = []
    for edge in edges.values():
        fa, fb = firm.get(edge["a"]), firm.get(edge["b"])
        if fa and fb and fa != fb:              # two firms: never one company
            continue
        kept.append(edge)
        parent[root(edge["a"])] = root(edge["b"])
    out: dict[str, dict] = {}
    for edge in kept:
        group = out.setdefault(root(edge["a"]), {"members": set(), "pairs": [], "score": 0.0})
        group["members"].update((edge["a"], edge["b"]))
        group["pairs"].append(edge)
        group["score"] = max(group["score"], edge["score"])
    result = []
    for group in out.values():
        group["members"] = sorted(group["members"])
        group["pairs"].sort(key=lambda e: (-e["score"], e["a"], e["b"]))
        result.append(group)
    result.sort(key=lambda g: (-g["score"], -len(g["members"]), g["members"][0]))
    return result


async def merge_group(db, keep: str, members, decided_by: str = "you") -> dict:
    """Merge every one of ``members`` into ``keep`` in one go, and return
    {"survivor", "merged": [ids], "group_id"}. Each merge is logged on its
    own (undone one by one, or all at once with undo_group). Refused
    (MergeRefused) before anything moves when any two of them hold
    different CUIs or registration numbers, or one is merged already.
    Never commits."""
    members = list(dict.fromkeys(m for m in members if m and m != keep))
    if not members:
        raise MergeRefused("same_company")
    everyone = [keep, *members]
    rows = {cid: await _company_row(db, cid) for cid in everyone}
    if any(row is None for row in rows.values()):
        raise MergeRefused("not_found")
    if any(row.get("merged_into") for row in rows.values()):
        raise MergeRefused("already_merged")
    keys = {cid: await ids_of(db, cid) for cid in everyone}
    for i, a in enumerate(everyone):
        for b in everyone[i + 1:]:
            kind = conflict(keys[a], keys[b])
            if kind:
                raise MergeRefused("different_" + kind,
                                   f"{a} and {b} hold different {kind} values")
    group_id = 0
    for member in members:
        score, reasons = await _pair_reasons(db, keep, member, everyone)
        merge_id = await merge(db, keep, member, score=score, reasons=reasons,
                               decided_by=decided_by)
        group_id = group_id or merge_id
        await db.execute("UPDATE company_merges SET group_id = ? WHERE id = ?",
                         (group_id, merge_id))
    return {"survivor": keep, "merged": members, "group_id": group_id}


async def _pair_reasons(db, keep: str, member: str, everyone) -> tuple[float | None, list]:
    """Why ``member`` is in the group: its pending pair with the keeper, or
    else its best pair with any other member."""
    marks = ",".join("?" * len(everyone))
    async with db.execute(
        "SELECT score, reasons_json, (company_a IN (?, ?) AND company_b IN (?, ?)) AS direct "
        f"FROM match_candidates WHERE status = 'pending' AND ((company_a = ? AND company_b IN "
        f"({marks})) OR (company_b = ? AND company_a IN ({marks}))) "
        "ORDER BY direct DESC, score DESC LIMIT 1",
        (keep, member, keep, member, member, *everyone, member, *everyone)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None, []
    try:
        return row[0], json.loads(row[1] or "[]")
    except (json.JSONDecodeError, TypeError):
        return row[0], []


async def undo_group(db, company_id: str) -> list[dict]:
    """Undo a whole group merge: the one ``company_id`` joined, else the one
    it kept (refused as "several_groups" when it kept more than one still in
    force: name one of the merged companies instead). Every member goes back
    on its own, newest merge first. Never commits."""
    async with db.execute(
        "SELECT group_id FROM company_merges WHERE merged = ? AND undone_at IS NULL "
        "AND group_id != 0 ORDER BY id DESC LIMIT 1", (company_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        async with db.execute(
            "SELECT DISTINCT group_id FROM company_merges WHERE survivor = ? "
            "AND undone_at IS NULL AND group_id != 0 LIMIT 2", (company_id,)
        ) as cur:
            kept = await cur.fetchall()
        if len(kept) > 1:
            raise MergeRefused("several_groups")
        row = kept[0] if kept else None
    if row is None:
        raise MergeRefused("not_merged")
    async with db.execute(
        "SELECT merged FROM company_merges WHERE group_id = ? AND undone_at IS NULL "
        "ORDER BY id DESC", (row[0],)
    ) as cur:
        members = [r[0] for r in await cur.fetchall()]
    return [await undo(db, member) for member in members]


async def separate(db, out, rest) -> int:
    """"Different companies": each of ``out`` is not any of ``rest`` (the
    rest of its group), and those pairs leave the review list for good.
    With ``rest`` empty, every pair among ``out``. Returns how many pairs
    were decided. Never commits."""
    out = list(dict.fromkeys(x for x in out if x))
    rest = [x for x in dict.fromkeys(rest or ()) if x and x not in out]
    pairs = [(a, b) for a in out for b in rest] if rest else \
        [(a, b) for i, a in enumerate(out) for b in out[i + 1:]]
    for a, b in pairs:
        if not await decide_candidate(db, a, b, "separate"):
            await db.execute(
                "INSERT OR IGNORE INTO match_candidates (company_a, company_b, status, "
                "decided_at) VALUES (?, ?, 'separate', CURRENT_TIMESTAMP)", (a, b))
    return len(pairs)
