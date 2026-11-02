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


# ── Matching ──

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


def scan_key(kind: str, value: str, here: dict, shared: dict, flags: dict, firm: dict) -> None:
    """Add the pairs one key held by several companies makes (``here``:
    company -> its firm or None) to a scan's ``shared`` keys and ``flags``."""
    if kind == "email" and not usable_email(value):
        return
    pairs, flag = _scan_pairs(kind, here)
    if not pairs:
        return
    firm.update(here)
    for a, b in pairs:
        key = (a, b) if a < b else (b, a)
        shared.setdefault(key, {})[kind] = (WEIGHTS[kind], value)
        if flag:
            flags.setdefault(key, set()).add(flag)


def scored_pairs(shared: dict, flags: dict, firm: dict, keys: dict, legal=LEGAL) -> list:
    """The scan's pairs scored (``keys``: every company's keys), those at
    REVIEW or more, most likely first (see scan_candidates)."""
    scored = []
    for (a, b), found in shared.items():
        result = _score(found, keys[a], keys[b], legal)
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


# ── Merges: a branch's place ──

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


# ── Groups ──

