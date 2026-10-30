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
    same name (legal form stripped), same town      0.7

Several kinds of evidence add up (two independent hints are stronger than
either: 0.7 and 0.8 make 0.94). At ATTACH or more the record is the same
company; from REVIEW up to ATTACH a new company is kept and the pair goes
to the review list; below that they are different companies. Two different
CUIs (or registration numbers) never match, whatever else agrees. Nothing
here asks a model: the same input always gives the same answer.

A merge moves everything one company holds (observations, contacts,
identifiers, activities) onto the other and logs each row it moved, so
undoing it puts back exactly what it took. Nothing is deleted, and nothing
already on the surviving record is overwritten: its empty columns are
filled, every other value stays on file with its source.
"""

import csv
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

ATTACH = 0.85
REVIEW = 0.6
# What each kind of evidence is worth on its own.
WEIGHTS = {
    "cui": 1.0, "reg": 1.0, "external": 1.0,
    "domain": 0.9,
    "declared_domain": 0.7,     # a website a registry row declares, unchecked
    "phone": 0.8,
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
NAME_HOLDERS_MAX = 3
# A domain two companies on file hold already is a shared site (a group's,
# a bank's that its branches declare), not one business's.
DOMAIN_HOLDERS_MAX = 1
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

    @property
    def attach(self) -> bool:
        return bool(self.company_id) and self.score >= ATTACH

    @property
    def review(self) -> bool:
        return bool(self.company_id) and REVIEW <= self.score < ATTACH


# ── Keys ──

def fold(text: str) -> list[str]:
    """Lowercase words without accents (ș and ş alike), punctuation dropped."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", plain)


@lru_cache(maxsize=1)
def legal_forms() -> tuple[dict[int, set], dict[int, set]]:
    """(forms that can start a name, forms that can end one), each folded
    to words and grouped by length, longest first, from
    reference/legal_forms.csv."""
    text = resources.files("harvey").joinpath("reference/legal_forms.csv") \
        .read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    start, end = set(), set()
    for r in rows:
        words = tuple(fold(r["form"]))
        if not words:
            continue
        if r["where"] in ("start", "both"):
            start.add(words)
        if r["where"] in ("end", "both"):
            end.add(words)
    def by_length(forms):
        out: dict[int, set] = {}
        for form in forms:
            out.setdefault(len(form), set()).add(form)
        return dict(sorted(out.items(), reverse=True))
    return by_length(start), by_length(end)


def name_key(name: str) -> str:
    """The name two records are compared on: folded, legal forms stripped
    from either end. "" when too little is left to tell companies apart."""
    words = fold(name)
    start, end = legal_forms()
    stripped = True
    while stripped and words:
        stripped = False
        for size, forms in start.items():
            if len(words) > size and tuple(words[:size]) in forms:
                words, stripped = words[size:], True
                break
        for size, forms in end.items():
            if len(words) > size and tuple(words[-size:]) in forms:
                words, stripped = words[:-size], True
                break
    core = " ".join(words)
    return core if len(core.replace(" ", "")) >= 3 else ""


_PLACE_LABEL = re.compile(r"^(municipiul|mun|orasul|oras|comuna|com|satul|sat|loc)\s+")
_SECTOR = re.compile(r"\s+sector(ul)?\s+\d+$")
_SECTOR_ONLY = re.compile(r"sector(ul)?\s+\d+")


def locality_key(location: str) -> str:
    """The town a location names: its first part, folded, without an
    administrative label ("Municipiul", "Sat") or a Bucharest sector."""
    first = " ".join(fold((location or "").split(",")[0]))
    if _SECTOR_ONLY.fullmatch(first):
        # "Sector 1, București": the sector is Bucharest's, and so is the key
        # ("Mun. Bucuresti, Sector 2" reads as "bucuresti" too).
        return "bucuresti"
    return _SECTOR.sub("", _PLACE_LABEL.sub("", first)).strip()


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


def identifiers(company, extra_ids=()) -> list[tuple[str, str]]:
    """Every (kind, value) a company is known by: its CUI, registration
    number, listing ids, domain, phone, and name in its town."""
    get = (lambda k: company.get(k) or "") if isinstance(company, dict) \
        else (lambda k: getattr(company, k, "") or "")
    out: list[tuple[str, str]] = []
    cui = re.sub(r"\D", "", get("cui").upper().removeprefix("RO")).lstrip("0")
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


def conflict(mine: dict[str, set[str]], theirs: dict[str, set[str]]) -> str:
    """The legal key two companies hold different values of ("cui", "reg"),
    or "" when nothing says they are different companies."""
    for kind in LEGAL:
        if mine.get(kind) and theirs.get(kind) and not mine[kind] & theirs[kind]:
            return kind
    return ""


def _combined(weights) -> float:
    miss = 1.0
    for w in weights:
        miss *= 1.0 - w
    return round(1.0 - miss, 2)


def _score(found: dict[str, tuple[float, str]], mine: dict[str, set[str]],
           theirs: dict[str, set[str]]):
    """(score, reasons, evidence used) for two companies sharing ``found``
    ({code: (weight, value)}), or None when nothing counts."""
    if conflict(mine, theirs):
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


async def match(db, company, extra_ids=(), declared_domain: bool = False) -> Match:
    """The company on file this record is, with how sure and why. Reads
    only. ``declared_domain``: the record's website is self-declared (a
    registry row), worth a review, not a match on its own."""
    ids = identifiers(company, extra_ids)
    if not ids:
        return Match()
    mine: dict[str, set[str]] = {}
    for kind, value in ids:
        mine.setdefault(kind, set()).add(value)
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
    evidence: dict[str, dict[str, tuple[float, str]]] = {}
    for (kind, value), live in holders.items():
        if kind == "phone" and len(live) > PHONE_HOLDERS_MAX:
            continue
        if kind == "domain" and len(live) > DOMAIN_HOLDERS_MAX:
            continue
        if kind == "name" and len(live) > NAME_HOLDERS_MAX:
            continue
        code = "declared_domain" if kind == "domain" and declared_domain else kind
        for cid in live:
            evidence.setdefault(cid, {})[code] = (WEIGHTS[code], value)
    if not evidence:
        return Match(new_ids=ids)
    for cid in evidence:
        if cid in via_merged:              # reached through a merged record
            theirs_all[cid] = await ids_of(db, cid)

    best = Match(new_ids=ids)
    for cid in sorted(evidence):
        scored = _score(evidence[cid], mine, theirs_all[cid])
        if scored is None:
            continue
        score, reasons, found = scored
        if score > best.score:
            held = {pair for pair, live in holders.items() if cid in live}
            same = any(k in SAME_RECORD for k in found)
            best = Match(cid, score, reasons, same,
                         [pair for pair in ids if pair not in held],
                         min(merged_owner.get(cid, ())) if same and merged_owner.get(cid) else "")
    if best.same_record and not best.origin:
        best.origin = await _moved_from(db, [pair for pair in ids if pair[0] in SAME_RECORD])
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


async def scan(db) -> dict:
    """Score the companies already on file against each other, once (the
    keys of a database from before the resolver arrive all at once, so no
    record was ever matched against the others). The same rules as a new
    record: ATTACH or more is merged (undoable), REVIEW to ATTACH goes to
    the review list. Never commits."""
    caps = {"phone": PHONE_HOLDERS_MAX, "name": NAME_HOLDERS_MAX,
            "domain": DOMAIN_HOLDERS_MAX}
    shared: dict[tuple[str, str], dict[str, tuple[float, str]]] = {}
    async with db.execute(
        "SELECT i.kind, i.value, group_concat(i.company_id, char(31)) "
        "FROM company_ids i JOIN companies c ON c.id = i.company_id "
        "WHERE i.kind IN ('phone', 'name', 'domain') AND COALESCE(c.merged_into, '') = '' "
        "GROUP BY i.kind, i.value HAVING COUNT(DISTINCT i.company_id) > 1"
    ) as cur:
        async for kind, value, holders in cur:
            holders = sorted(set(holders.split("\x1f")))
            if len(holders) > caps[kind]:
                continue
            for i, a in enumerate(holders):
                for b in holders[i + 1:]:
                    shared.setdefault((a, b), {})[kind] = (WEIGHTS[kind], value)
    scored = []
    for (a, b), found in shared.items():
        result = _score(found, await ids_of(db, a), await ids_of(db, b))
        if result is not None and result[0] >= REVIEW:
            scored.append((result[0], a, b, result[1]))
    merged = review = 0
    for score, a, b, reasons in sorted(scored, key=lambda x: (-x[0], x[1], x[2])):
        # The registry's record (it has a CUI) keeps the company, else the
        # older one, as when a person says "same company".
        async with db.execute(
            "SELECT id FROM companies WHERE id IN (?, ?) AND COALESCE(merged_into, '') = '' "
            "ORDER BY (COALESCE(cui, '') = ''), created_at, rowid", (a, b)
        ) as cur:
            order = [r[0] for r in await cur.fetchall()]
        if len(order) != 2:                 # one of them joined a third already
            continue
        if score < ATTACH:
            await add_candidate(db, order[0], order[1], score, reasons)
            review += 1
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
                        reasons: list[dict]) -> None:
    """"Is B the same company as A?", for a person to answer. One row per
    pair, whichever way round it was asked."""
    async with db.execute(
        "SELECT 1 FROM match_candidates WHERE (company_a = ? AND company_b = ?) "
        "OR (company_a = ? AND company_b = ?)", (company_a, company_b, company_b, company_a)
    ) as cur:
        if await cur.fetchone():
            return
    await db.execute(
        "INSERT INTO match_candidates (company_a, company_b, score, reasons_json) "
        "VALUES (?, ?, ?, ?)", (company_a, company_b, score, json.dumps(reasons)))


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

    async def move(table: str, keys: str, where_free: str = ""):
        await db.execute(
            f"INSERT INTO company_merge_moves (merge_id, tbl, key1, key2, key3) "
            f"SELECT ?, '{table}', {keys} FROM {table} t WHERE t.company_id = ?{where_free}",
            (merge_id, merged, *([survivor] if where_free else [])))
        await db.execute(
            f"UPDATE {table} SET company_id = ? WHERE company_id = ? AND EXISTS ("
            f"SELECT 1 FROM company_merge_moves m WHERE m.merge_id = ? AND m.tbl = '{table}' "
            f"AND {_MOVE_MATCH[table]})", (survivor, merged, merge_id))

    await move("observations", "t.id, '', ''")
    await move("prospects", "t.id, '', ''")
    # A key or an activity the survivor already has stays where it is.
    await move("company_ids", "t.kind, t.value, ''",
               " AND NOT EXISTS (SELECT 1 FROM company_ids s WHERE s.company_id = ? "
               "AND s.kind = t.kind AND s.value = t.value)")
    await move("company_activities", "t.code, CAST(t.version AS TEXT), t.source",
               " AND NOT EXISTS (SELECT 1 FROM company_activities s WHERE s.company_id = ? "
               "AND s.code = t.code AND s.version = t.version AND s.source = t.source)")
    # Companies merged into this one earlier now stand for the survivor.
    await db.execute(
        "INSERT INTO company_merge_moves (merge_id, tbl, key1, key2, key3) "
        "SELECT ?, 'companies', id, '', '' FROM companies WHERE merged_into = ?",
        (merge_id, merged))
    await db.execute("UPDATE companies SET merged_into = ? WHERE merged_into = ?",
                     (survivor, merged))

    filled, moved = {}, {}
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
    await db.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (survivor, merged))
    await db.execute("UPDATE company_merges SET filled_json = ?, moved_json = ? WHERE id = ?",
                     (json.dumps(filled), json.dumps(moved), merge_id))
    if not await decide_candidate(db, survivor, merged, "merged"):
        await db.execute(
            "INSERT INTO match_candidates (company_a, company_b, score, reasons_json, status, "
            "decided_at) VALUES (?, ?, ?, ?, 'merged', CURRENT_TIMESTAMP)",
            (survivor, merged, score or 0.0, json.dumps(reasons or [])))
    # Search: the merged company leaves the index, the survivor is redone.
    await db.executemany("INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (?)",
                         [(survivor,), (merged,)])
    return merge_id


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
    # An undo says the merge was wrong, not that they are two companies: a
    # mistaken click must not take the pair off the list for good.
    await decide_candidate(db, survivor, merged, "pending")
    await db.executemany("INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (?)",
                         [(survivor,), (merged,)])
    return {"survivor": survivor, "merged": merged, "merge_id": merge_id}
