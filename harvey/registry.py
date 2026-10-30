"""The whole trade registry (ONRC), loaded once per dataset, searched locally.

ONRC publishes one national snapshot per dataset (harvey/collectors/onrc.py
reads its files). Instead of streaming the 1.2 GB again for every county or
activity someone asks about, ``build()`` loads all of it once into its own
SQLite database, data/registry.db, beside the working one (harvey.db):

    firms            every registration, any status: name, CUI, registration
                     number, county, locality, registered office (none for
                     sole traders and family businesses), website, status
                     codes and whether it is operating
    firm_caen        every CAEN class each firm is authorised for, as codes
    administrators   the people who run each firm: name and role only
    caen             the nomenclature (names, everyday words), joined at
                     query time, never copied onto firms
    firm_fts         full-text index: name, locality, county, and tags for
                     the county code, "operating" and each current class
    caen_fts         full-text index over class names and everyday words
    meta             which dataset this is, when it was loaded, counts

The same privacy rules as the streaming import apply when loading: no
personal numeric code (CNP) where a CUI goes, no birth data or home
locality (those columns are never read), no street address for natural
persons trading. A new dataset is built in a temporary file and renamed
over the old one, so a reader never sees a half-loaded registry.

``search()`` answers words the way harvey search does (every word must
match, accent-insensitive, prefixes, a 4-digit code exact, -word excludes),
with activity words resolved to CAEN classes first: "dentist" finds firms
authorised for dental practice, whatever they are called. ``search_draft()``
reads the firms a query matches as the onrc module's draft; core copies
them into the working database (harvey/modules/runs.py, registry_pull),
under the same identity, fill-gaps and confirmation rules as any source.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harvey.collectors import onrc
from harvey.paths import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.db"
SCHEMA_VERSION = "2"

# Rows per executemany while loading; progress is reported this often.
LOAD_BATCH = 50_000
PROGRESS_ROWS = 1_000_000

CURRENT = (2, 3)        # the CAEN revisions a search matches (older numbers mean other things)

# A search counts its matches up to this many and then says "more than":
# counting all 800k firms a word like "comert" matches takes a second,
# counting the first 100k a tenth of that.
COUNT_CAP = 100_000

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per registration. id follows the firms' names alphabetically,
-- so the full-text index returns results in name order without sorting.
CREATE TABLE firms (
    id INTEGER PRIMARY KEY,
    reg TEXT NOT NULL,
    name TEXT NOT NULL,
    cui TEXT NOT NULL DEFAULT '',
    form TEXT NOT NULL DEFAULT '',
    county TEXT NOT NULL DEFAULT '',
    county_code TEXT NOT NULL DEFAULT '',
    locality TEXT NOT NULL DEFAULT '',
    places TEXT NOT NULL DEFAULT '',    -- every place in the registry's locality
                                        -- ("Purcăreni Tărlungeni": village, commune),
                                        -- when it says more than locality
    address TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    registered TEXT NOT NULL DEFAULT '',
    statuses TEXT NOT NULL DEFAULT '',
    operating INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE firm_caen (
    firm_id INTEGER NOT NULL,
    code INTEGER NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (firm_id, code, version)
) WITHOUT ROWID;

CREATE TABLE administrators (
    firm_id INTEGER NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE caen (
    id INTEGER PRIMARY KEY,
    code INTEGER NOT NULL,
    version INTEGER NOT NULL,
    name_ro TEXT NOT NULL DEFAULT '',
    name_en TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    firms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (code, version)
);

CREATE VIRTUAL TABLE caen_fts USING fts5(
    names, keywords, content='', tokenize='unicode61 remove_diacritics 2');

CREATE VIRTUAL TABLE firm_fts USING fts5(
    name, locality, county, tags,
    content='', detail='column', tokenize='unicode61 remove_diacritics 2');
"""

STAGE = """
CREATE TABLE stage.firms (reg TEXT, sort TEXT, name TEXT, cui TEXT, form TEXT,
    county TEXT, county_code TEXT, locality TEXT, places TEXT, address TEXT,
    website TEXT, domain TEXT, registered TEXT);
CREATE TABLE stage.status (reg TEXT, code TEXT);
CREATE TABLE stage.caen (reg TEXT, code INTEGER, version INTEGER);
CREATE TABLE stage.people (reg TEXT, first_name TEXT, last_name TEXT, role TEXT);
CREATE TABLE stage.closed (code TEXT PRIMARY KEY);
"""

OPERATING_TAG = "activ"


def county_tag(code: str) -> str:
    return "j" + code.lower()


def class_tag(code, version) -> str:
    return f"c{int(code):04d}r{int(version)}"


# ── Loading ──

def _county_codes() -> dict[str, str]:
    """Folded county name -> its two-letter code ("brasov" -> "BV")."""
    return {onrc._fold(name): code for code, name in onrc.COUNTIES.items()}


def _batches(rows, size=LOAD_BATCH):
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


_PLACE_LABELS = frozenset({"municipiul", "municipiu", "oras", "orasul", "comuna",
                           "sat", "satul", "loc", "localitatea", "sector", "sectorul"})


def places(raw: str) -> str:
    """Every place a registry locality names, without the administrative
    labels: "Sat Purcăreni, Comuna Tărlungeni" -> "Purcăreni Tărlungeni".
    A firm in a village is found by its commune too, as the streaming
    import found it."""
    out: list[str] = []
    for part in (raw or "").split(","):
        name = onrc.clean_locality(part)
        if name and name not in out:
            out.append(name)
    return " ".join(out)


def _firm_rows(path: Path):
    codes = _county_codes()
    county_cache: dict[str, str] = {}
    for row in onrc.read_rows(path, onrc._FIRM_COLUMNS):
        rec = dict(zip(onrc._FIRM_COLUMNS, row))
        firm = onrc.parse_firm(rec)
        if not firm.reg:
            continue
        code = county_cache.get(firm.county)
        if code is None:
            name = onrc.county_name(firm.county)
            code = county_cache[firm.county] = codes.get(onrc._fold(name or ""), "")
        where = places(rec["ADR_LOCALITATE"])
        yield (firm.reg, onrc._fold(firm.name), firm.name, firm.cui, firm.form,
               firm.county, code, firm.locality, "" if where == firm.locality else where,
               firm.address, firm.website, firm.domain, firm.registered)


def _caen_rows(path: Path):
    for reg, code, ver in onrc.read_rows(path, ("COD_INMATRICULARE", "COD_CAEN_AUTORIZAT",
                                                "VER_CAEN_AUTORIZAT")):
        code, ver = code.strip(), ver.strip()
        if code.isdigit() and ver.isdigit() and len(code) <= 4:
            yield reg.strip(), int(code), int(ver)


def _status_rows(path: Path):
    for reg, code in onrc.read_rows(path, ("COD_INMATRICULARE", "COD")):
        yield reg.strip(), code.strip()


def _people_rows(path: Path):
    for reg, name, role in onrc.read_rows(path, onrc.PEOPLE_COLUMNS):
        person = onrc.parse_person(name, role)
        if person:
            yield (reg.strip(), *person)


def _remove(path: Path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


class RegistryBusy(RuntimeError):
    """Another load of the same registry is running."""


def build(files: dict[str, Path], dataset: onrc.Dataset, target: Path = REGISTRY_PATH,
          progress=None) -> dict:
    """Load a whole dataset into a fresh registry at ``target``, one load at
    a time: RegistryBusy when another is already loading it (two would
    share, and delete, each other's scratch files). See _build."""
    from harvey.pidfile import _try_lock, _unlock

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = open(target.with_name(target.name + ".lock"), "a+", encoding="utf-8")
    try:
        if not _try_lock(lock):
            raise RegistryBusy(f"another load of {target} is running; try again "
                               "when it has finished")
        try:
            return _build(files, dataset, target, progress)
        finally:
            _unlock(lock)
    finally:
        lock.close()


def _build(files: dict[str, Path], dataset: onrc.Dataset, target: Path,
           progress=None) -> dict:
    """Load a whole dataset into a fresh registry database at ``target``.

    ``files`` maps onrc.FILES roles to the downloaded files; "people" may be
    missing, and then the registry has no administrators. Built in
    ``<target>.building`` (with a scratch ``<target>.stage`` beside it) and
    renamed over ``target`` only once complete. ``progress(step, rows)`` is
    called as it goes. Returns the counts stored in meta. Scratch files a
    killed load left behind are removed first (safe: build holds the lock).
    """
    building = target.with_name(target.name + ".building")
    stage = target.with_name(target.name + ".stage")
    _remove(building)
    _remove(stage)
    started = time.monotonic()

    def say(step, rows=None):
        if progress:
            progress(step, rows)

    con = sqlite3.connect(building, isolation_level=None)
    try:
        con.execute("ATTACH DATABASE ? AS stage", (str(stage),))
        for schema in ("main", "stage"):
            con.execute(f"PRAGMA {schema}.journal_mode=OFF")
            con.execute(f"PRAGMA {schema}.synchronous=OFF")
            con.execute(f"PRAGMA {schema}.cache_size=-400000")
        con.execute("PRAGMA temp_store=FILE")
        con.executescript(SCHEMA)       # executescript commits: before BEGIN
        con.executescript(STAGE)
        con.execute("BEGIN")

        def load(step, sql, rows):
            done = 0
            for batch in _batches(rows):
                con.executemany(sql, batch)
                before, done = done, done + len(batch)
                if done // PROGRESS_ROWS != before // PROGRESS_ROWS:
                    say(step, done)
            say(step, done)
            return done

        load("firms", "INSERT INTO stage.firms VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             _firm_rows(files["firms"]))
        load("statuses", "INSERT INTO stage.status VALUES (?,?)",
             _status_rows(files["status"]))
        load("activities", "INSERT INTO stage.caen VALUES (?,?,?)",
             _caen_rows(files["caen"]))
        if files.get("people"):
            load("administrators", "INSERT INTO stage.people VALUES (?,?,?,?)",
                 _people_rows(files["people"]))
        con.executemany("INSERT INTO stage.closed VALUES (?)",
                        [(c,) for c in sorted(onrc.CLOSED_STATUSES)])

        say("indexing")
        # Status per registration: every code it carries, and operating when
        # one is 1048 and none says closed or closing (onrc.CLOSED_STATUSES).
        con.execute("""
            CREATE TABLE stage.status_by_reg AS
            SELECT reg, group_concat(code, ' ') AS codes,
                   MAX(code = ?) AND NOT MAX(code IN (SELECT code FROM stage.closed))
                       AS operating
            FROM (SELECT DISTINCT reg, code FROM stage.status ORDER BY reg, code)
            GROUP BY reg""", (onrc.ACTIVE_STATUS,))
        con.execute("CREATE UNIQUE INDEX stage.status_by_reg_reg ON status_by_reg(reg)")
        # A registration listed twice in the firms file keeps its first row.
        con.execute("""
            INSERT INTO firms (reg, name, cui, form, county, county_code, locality, places,
                               address, website, domain, registered, statuses, operating)
            SELECT f.reg, f.name, f.cui, f.form, f.county, f.county_code, f.locality,
                   f.places, f.address, f.website, f.domain, f.registered,
                   COALESCE(s.codes, ''), COALESCE(s.operating, 0)
            FROM stage.firms f
            LEFT JOIN stage.status_by_reg s ON s.reg = f.reg
            WHERE f.rowid IN (SELECT MIN(rowid) FROM stage.firms GROUP BY reg)
            ORDER BY f.sort, f.reg""")
        con.execute("CREATE UNIQUE INDEX firms_reg ON firms(reg)")
        con.execute("CREATE INDEX firms_cui ON firms(cui) WHERE cui != ''")
        say("activities by firm")
        con.execute("""
            INSERT OR IGNORE INTO firm_caen (firm_id, code, version)
            SELECT f.id, c.code, c.version
            FROM stage.caen c JOIN firms f ON f.reg = c.reg
            ORDER BY f.id, c.code, c.version""")
        con.execute("""
            INSERT INTO administrators (firm_id, first_name, last_name, role)
            SELECT f.id, p.first_name, p.last_name, p.role
            FROM stage.people p JOIN firms f ON f.reg = p.reg
            WHERE p.rowid IN (SELECT MIN(rowid) FROM stage.people
                              GROUP BY reg, first_name, last_name)
            ORDER BY f.id, p.rowid""")
        con.execute("CREATE INDEX administrators_firm ON administrators(firm_id)")

        say("nomenclature")
        _load_caen(con)
        say("search index")
        con.execute(f"""
            INSERT INTO firm_fts (rowid, name, locality, county, tags)
            SELECT f.id, f.name, CASE WHEN f.places != '' THEN f.places ELSE f.locality END,
                   f.county,
                   trim(CASE WHEN f.county_code != '' THEN 'j' || lower(f.county_code)
                             ELSE '' END
                        || CASE WHEN f.operating THEN ' {OPERATING_TAG}' ELSE '' END
                        || ' ' || COALESCE((
                            SELECT group_concat(printf('c%04dr%d', k.code, k.version), ' ')
                            FROM firm_caen k
                            WHERE k.firm_id = f.id AND k.version IN (2, 3)), ''))
            FROM firms f ORDER BY f.id""")
        con.execute("INSERT INTO firm_fts (firm_fts) VALUES ('optimize')")

        counts = {
            "firms": con.execute("SELECT COUNT(*) FROM firms").fetchone()[0],
            "operating": con.execute(
                "SELECT COUNT(*) FROM firms WHERE operating").fetchone()[0],
            "activities": con.execute("SELECT COUNT(*) FROM firm_caen").fetchone()[0],
            "administrators": con.execute(
                "SELECT COUNT(*) FROM administrators").fetchone()[0],
        }
        meta = {
            "schema": SCHEMA_VERSION,
            "dataset": dataset.name,
            "date": dataset.date,
            "page": dataset.page,
            "loaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "load_seconds": f"{time.monotonic() - started:.0f}",
            "with_administrators": "1" if files.get("people") else "0",
            **{k: str(v) for k, v in counts.items()},
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        con.execute("COMMIT")
        con.execute("DETACH DATABASE stage")
        con.execute("ANALYZE")
    except BaseException:
        con.close()
        _remove(building)
        raise
    finally:
        con.close()
        _remove(stage)
    os.replace(building, target)
    return meta


def _load_caen(con):
    """The nomenclature (harvey/caen.py, the one source), with how many
    firms carry each class, and its search index (current revisions only)."""
    from harvey.caen import reference_rows

    con.executemany(
        "INSERT INTO caen (code, version, name_ro, name_en, keywords) VALUES (?,?,?,?,?)",
        [(int(code), version, name_ro, name_en or "", keywords or "")
         for code, version, name_ro, name_en, keywords in reference_rows()
         if code.isdigit()])
    con.execute("CREATE TEMP TABLE class_counts AS SELECT code, version, COUNT(*) AS n "
                "FROM firm_caen GROUP BY code, version")
    con.execute("""
        UPDATE caen SET firms = COALESCE((
            SELECT n FROM class_counts t
            WHERE t.code = caen.code AND t.version = caen.version), 0)""")
    con.execute("DROP TABLE class_counts")
    con.execute("""
        INSERT INTO caen_fts (rowid, names, keywords)
        SELECT id, name_ro || ' ' || name_en, keywords FROM caen
        WHERE version IN (2, 3)""")


# ── Reading ──

def connect(path: Path = REGISTRY_PATH) -> sqlite3.Connection:
    """The registry, read-only. FileNotFoundError when it isn't loaded."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no registry at {path}: run harvey registry load")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def status(path: Path = REGISTRY_PATH) -> dict | None:
    """The meta of the loaded registry (dataset, date, loaded_at, counts),
    plus its size on disk, or None when there is none."""
    path = Path(path)
    if not path.is_file():
        return None
    con = connect(path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    meta["bytes"] = str(path.stat().st_size)
    meta["path"] = str(path)
    return meta


# ── Searching ──

_FTS_TEXT = "{name locality county}"


def _quote(word: str, prefix: bool) -> str:
    return f'"{word}"' + ("*" if prefix else "")


def _or(terms: list[str]) -> str:
    return "(" + " OR ".join(terms) + ")"


@dataclass
class Query:
    """What someone typed, resolved: the FTS5 expression, and the classes it
    named (everyday-word classes first, then classes whose name matched)."""
    match: str = ""
    words: list[str] = field(default_factory=list)
    keyword_classes: list[tuple[str, int]] = field(default_factory=list)
    name_classes: list[tuple[str, int]] = field(default_factory=list)
    explicit_classes: list[tuple[str, int | None]] = field(default_factory=list)
    counties: list[str] = field(default_factory=list)
    # The query's words that name a place (a locality or county): firms
    # there rank before firms that only carry the word in their name.
    places: list[str] = field(default_factory=list)

    @property
    def classes(self) -> list[tuple[str, int | None]]:
        """The classes the request named, in rank order: what pull records
        as ACTIVITY_CODE and picks the industry from."""
        return list(dict.fromkeys(self.explicit_classes + self.keyword_classes
                                  + self.name_classes))

    def names_class(self, code: str, version: int) -> bool:
        if (code, version) in self.keyword_classes or (code, version) in self.name_classes:
            return True
        return any(c == code and (v == version or (v is None and version in CURRENT))
                   for c, v in self.explicit_classes)


def _classes_for_word(con, word: str) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(classes whose everyday words match, classes whose name only
    matches) for one folded query word, current revisions, most used first."""
    prefix = len(word) >= 3

    def hits(column):
        rows = con.execute(
            "SELECT c.code, c.version FROM caen_fts JOIN caen c ON c.id = caen_fts.rowid "
            "WHERE caen_fts MATCH ? ORDER BY c.firms DESC, c.code, c.version DESC",
            (f"{column} : {_quote(word, prefix)}",)).fetchall()
        return [(f"{r[0]:04d}", r[1]) for r in rows]

    keyword = hits("keywords")
    named = [pair for pair in hits("names") if pair not in keyword]
    return keyword, named


def _code_classes(con, code: str, version: int | None) -> list[tuple[str, int]]:
    versions = (version,) if version in CURRENT else CURRENT
    marks = ",".join("?" * len(versions))
    rows = con.execute(f"SELECT version FROM caen WHERE code = ? AND version IN ({marks}) "
                       "ORDER BY version DESC", (int(code), *versions)).fetchall()
    return [(code, r[0]) for r in rows] or [(code, v) for v in versions]


def _tags(classes) -> str:
    return "tags : " + _or([_quote(class_tag(c, v), False) for c, v in classes])


def resolve(con, words: str = "", *, classes=None, counties=None, localities=None,
            all_statuses: bool = False) -> Query:
    """Turn a query into a Query. ``words`` follow harvey search: every
    word must match the firm's name, locality or county, or one of its
    classes (by everyday word, class name, or a 4-digit code: "8623",
    "86.23", "8623/2" for Rev.2 only); "-word" must not; single letters
    are dropped, except a county code ("B" is București, "BV" Brașov).
    ``classes`` (code, version or None), ``counties`` (registry spelling)
    and ``localities`` are filters on top, as import-onrc gives them.
    Operating firms only, unless ``all_statuses``."""
    from harvey.words import SEARCH_STOPWORDS as _SEARCH_STOPWORDS, fold_words as _fold_words

    q = Query(explicit_classes=list(classes or []))
    text = re.sub(r"\b(\d{2})\.(\d{2})\b", r"\1\2", words or "")
    wanted: list[str] = []
    unwanted: list[str] = []
    want_counties: list[str] = []
    skip_counties: list[str] = []
    for token in text.split()[:20]:
        negative = token.startswith("-") and len(token) > 1
        bare = token.lstrip("-")
        county = _county_word(bare)
        if county:
            (skip_counties if negative else want_counties).append(county)
            continue
        code = re.fullmatch(r"(\d{4})/(\d+)", bare)
        if code:
            (unwanted if negative else wanted).append(f"{code.group(1)}/{code.group(2)}")
            continue
        # Joining words stay until phrases are found ("medic de familie").
        (unwanted if negative else wanted).extend(
            w for w in _fold_words(bare)
            if len(w) > 1 and (not negative or w not in _SEARCH_STOPWORDS))
    wanted = list(dict.fromkeys(wanted))
    unwanted = [w for w in dict.fromkeys(unwanted) if w not in wanted]

    def term(word, collect: bool):
        code = re.fullmatch(r"(\d{4})(?:/(\d+))?", word)
        if code:
            found = _code_classes(con, code.group(1),
                                  int(code.group(2)) if code.group(2) else None)
            if collect:
                q.keyword_classes += [p for p in found if p not in q.keyword_classes]
            return _tags(found)
        keyword, named = _classes_for_word(con, word)
        if collect:
            q.keyword_classes += [p for p in keyword if p not in q.keyword_classes]
            q.name_classes += [p for p in named if p not in q.name_classes]
        text_term = f"{_FTS_TEXT} : {_quote(word, len(word) >= 3)}"
        if collect and not word.isdigit() and _is_place(con, word):
            q.places.append(word)
        found = keyword + named
        return _or([text_term, _tags(found)]) if found else text_term

    # Words that together make one everyday phrase ("service auto", "medic
    # de familie") resolve as that phrase: its classes, or all its words in
    # the firm's name. Read one by one, "service" and "auto" each match
    # dozens of classes.
    parts = []
    units = [u for u in _phrase_units(wanted)
             if not isinstance(u, str) or u not in _SEARCH_STOPWORDS]
    wanted = [w for w in wanted if w not in _SEARCH_STOPWORDS]
    for unit in units:
        if isinstance(unit, str):
            parts.append(term(unit, True))
            continue
        phrase_words, found = unit
        q.keyword_classes += [p for p in found if p not in q.keyword_classes]
        text_term = f"{_FTS_TEXT} : (" + " AND ".join(
            _quote(w, len(w) >= 3) for w in phrase_words
            if w not in _SEARCH_STOPWORDS) + ")"
        parts.append(_or([text_term, _tags(found)]))
    q.words = [w.split("/")[0] for w in wanted]
    if q.explicit_classes:
        expanded = []
        for code, version in q.explicit_classes:
            expanded += [(code, v) for v in ((version,) if version is not None else CURRENT)]
        parts.append(_tags(expanded))
    names = list(dict.fromkeys((counties or []) + want_counties))
    codes = [c for c in (_county_code(n) for n in names) if c]
    q.counties = names
    if codes:
        parts.append(_tags_raw([county_tag(c) for c in codes]))
    places = []
    for place in localities or []:
        tokens = [w for w in _fold_words(place) if w not in _PLACE_LABELS]
        if tokens:
            places.append("locality : (" + " AND ".join(_quote(t, False) for t in tokens) + ")")
    if places:
        parts.append(_or(places))
    if not parts:
        return q
    if not all_statuses:
        parts.append(f"tags : {OPERATING_TAG}")
    match = " AND ".join(parts)
    excluded = [term(w, False) for w in unwanted]
    skip_codes = [c for c in (_county_code(n) for n in skip_counties
                              if n not in names) if c]
    if skip_codes:
        excluded.append(_tags_raw([county_tag(c) for c in skip_codes]))
    if excluded:
        match = f"({match})" + "".join(f" NOT {x}" for x in excluded)
    q.match = match
    return q


def _phrase_units(words: list[str]) -> list:
    """The query words in order, each a str, except runs that form a
    multi-word everyday phrase from harvey/caen.py, which come as
    (words, current classes)."""
    from harvey.caen import _synonym_hits, _tokens

    plain = [i for i, w in enumerate(words) if not w[:1].isdigit()]
    hits = {}
    for start, phrase, classes in _synonym_hits([words[i] for i in plain]):
        size = len(_tokens(phrase))
        found = [(c, v) for c, v in classes if v in CURRENT]
        if size > 1 and found:
            span = plain[start:start + size]
            if span == list(range(span[0], span[0] + size)):    # adjacent in the query
                hits[span[0]] = (size, found)
    out, i = [], 0
    while i < len(words):
        if i in hits:
            size, found = hits[i]
            out.append((words[i:i + size], found))
            i += size
        else:
            out.append(words[i])
            i += 1
    return out


def _tags_raw(tags: list[str]) -> str:
    return "tags : " + _or([_quote(t, False) for t in tags])


def _county_word(token: str) -> str | None:
    """The county a short token names: a plate code ("BV", "B", "cj") or
    "Bucharest"/"Buc". Longer county names stay ordinary words, matched in
    the county and locality columns."""
    letters = token.strip(".,;")
    if not letters.isalpha():
        return None
    if len(letters) <= 2 and letters.upper() in onrc.COUNTIES:
        return onrc.COUNTIES[letters.upper()]
    if onrc._fold(letters) in onrc._COUNTY_ALIASES:
        return onrc.county_name(letters)
    return None


def _is_place(con, word: str) -> bool:
    """Whether some firm's locality or county is (or holds) this word."""
    return con.execute("SELECT 1 FROM firm_fts WHERE firm_fts MATCH ? LIMIT 1",
                       (f"{{locality county}} : {_quote(word, False)}",)).fetchone() is not None


def _county_code(name: str) -> str:
    return _county_codes().get(onrc._fold(name or ""), "")


def _count(con, match: str, cap: int | None = None) -> int:
    if cap:
        return con.execute("SELECT COUNT(*) FROM (SELECT rowid FROM firm_fts "
                           "WHERE firm_fts MATCH ? LIMIT ?)", (match, cap)).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM firm_fts WHERE firm_fts MATCH ?",
                       (match,)).fetchone()[0]


def count(con, q: Query, cap: int | None = None) -> int:
    """How many firms a resolved query matches, counting no further than ``cap``."""
    return _count(con, q.match, cap) if q.match else 0


def _ids(con, match: str, limit: int, offset: int) -> list[int]:
    if limit <= 0:
        return []
    return [r[0] for r in con.execute(
        "SELECT rowid FROM firm_fts WHERE firm_fts MATCH ? ORDER BY rowid LIMIT ? OFFSET ?",
        (match, limit, offset))]


def _tiers(q: Query) -> list[str]:
    """The query split into result tiers, best first: firms holding a class
    an everyday word named, then the rest (a class name matched, or the
    firm's own name); within each, firms in the place a word names before
    firms that only carry it in their name ("8623 cristian": dentists in
    Cristian before Dr. Cristian's practice in Cluj). One tier when there
    is nothing to rank by."""
    if not q.match:
        return []
    tiers = [q.match]
    if q.keyword_classes:
        top = _tags(q.keyword_classes)
        tiers = [f"({q.match}) AND {top}", f"({q.match}) NOT {top}"]
    if q.places:
        there = "{locality county} : (" + " AND ".join(
            _quote(w, False) for w in q.places) + ")"
        tiers = [x for t in tiers for x in (f"({t}) AND {there}", f"({t}) NOT {there}")]
    return tiers


def matching_ids(con, q: Query, limit: int | None = None, offset: int = 0) -> list[int]:
    """Firm ids for a resolved query, best tier first, alphabetical within
    a tier. All of them when ``limit`` is None."""
    out: list[int] = []
    big = 1 << 62
    want = big if limit is None else limit
    for match in _tiers(q):
        if len(out) >= want:
            break
        if offset:
            n = _count(con, match, offset + 1)      # only: is the offset inside it?
            if offset >= n:
                offset -= n
                continue
        got = _ids(con, match, min(want - len(out), big), offset)
        offset = 0
        out += got
    return out


def search(con, words: str = "", *, classes=None, counties=None, localities=None,
           all_statuses: bool = False, limit: int = 20, offset: int = 0) -> dict:
    """{"total", "more", "rows", "query"}: how many firms match, and one
    page of them, each with its activities in words (the ones the query
    named first). Counting stops at COUNT_CAP: then "total" is COUNT_CAP
    and "more" is True (there are more than that)."""
    q = resolve(con, words, classes=classes, counties=counties, localities=localities,
                all_statuses=all_statuses)
    if not q.match:
        return {"total": 0, "more": False, "rows": [], "query": q}
    limit = max(1, min(int(limit), 500))
    total = _count(con, q.match, COUNT_CAP + 1)
    more = total > COUNT_CAP
    ids = matching_ids(con, q, limit, max(0, int(offset)))
    return {"total": min(total, COUNT_CAP), "more": more,
            "rows": firm_rows(con, ids, q), "query": q}


def firm_rows(con, ids: list[int], q: Query | None = None) -> list[dict]:
    """The firms, in the order given, as dicts for display."""
    from harvey.caen import revision_label

    if not ids:
        return []
    names = _status_names()
    marks = ",".join("?" * len(ids))
    firms = {r["id"]: dict(r) for r in con.execute(
        f"SELECT * FROM firms WHERE id IN ({marks})", ids)}
    acts: dict[int, list[dict]] = {i: [] for i in ids}
    for r in con.execute(
            f"""SELECT k.firm_id, k.code, k.version, COALESCE(c.name_ro, '') AS name,
                       COALESCE(c.name_en, '') AS name_en
                FROM firm_caen k LEFT JOIN caen c ON c.code = k.code AND c.version = k.version
                WHERE k.firm_id IN ({marks})
                ORDER BY k.firm_id, k.code, k.version DESC""", ids):
        code = f"{r['code']:04d}"
        acts[r["firm_id"]].append({
            "code": code, "version": r["version"], "revision": revision_label(r["version"]),
            "name": r["name"], "name_en": r["name_en"],
            "matched": bool(q and q.names_class(code, r["version"])),
        })
    out = []
    for i in ids:
        row = firms.get(i)
        if not row:
            continue
        codes = row["statuses"].split()
        closing = [c for c in codes if c in onrc.CLOSED_STATUSES]
        label = closing[0] if closing else (
            onrc.ACTIVE_STATUS if onrc.ACTIVE_STATUS in codes else (codes[0] if codes else ""))
        row["operating"] = bool(row["operating"])
        row["status"] = names.get(label, label)
        row["activities"] = sorted(acts.get(i, []), key=lambda a: not a["matched"])
        out.append(row)
    return out


_STATUS_NAMES: dict[str, str] | None = None


def _status_names() -> dict[str, str]:
    global _STATUS_NAMES
    if _STATUS_NAMES is None:
        _STATUS_NAMES = onrc.status_names()
    return _STATUS_NAMES


# ── Reading chosen firms for the working database ──

def firms_for_pull(con, ids: list[int], with_administrators: bool = True):
    """(Firms, reg -> classes, reg -> administrators) for these ids, in the
    shapes onrc.firms_draft takes."""
    firms, codes, people = [], {}, {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        regs = {}
        for r in con.execute(f"SELECT * FROM firms WHERE id IN ({marks})", chunk):
            regs[r["id"]] = r["reg"]
            firms.append((r["id"], onrc.Firm(
                reg=r["reg"], name=r["name"], cui=r["cui"], form=r["form"],
                county=r["county"], locality=r["locality"], address=r["address"],
                website=r["website"], domain=r["domain"], registered=r["registered"])))
        for r in con.execute(f"SELECT firm_id, code, version FROM firm_caen "
                             f"WHERE firm_id IN ({marks}) ORDER BY firm_id, code, version",
                             chunk):
            codes.setdefault(regs[r[0]], []).append((f"{r[1]:04d}", r[2]))
        if with_administrators:
            for r in con.execute(f"SELECT firm_id, first_name, last_name, role "
                                 f"FROM administrators WHERE firm_id IN ({marks}) "
                                 f"ORDER BY rowid", chunk):
                people.setdefault(regs[r[0]], []).append((r[1], r[2], r[3]))
    order = {i: n for n, i in enumerate(ids)}
    firms.sort(key=lambda pair: order[pair[0]])
    return [f for _, f in firms], codes, people


def dataset_of(con) -> onrc.Dataset:
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    return onrc.Dataset(meta.get("dataset", ""), meta.get("date", ""), {})


def search_draft(con, q: Query, *, limit: int | None = None,
                 with_administrators: bool = True):
    """The firms a resolved query matches (the first ``limit``, best first)
    as a Draft (onrc.firms_draft): ``counts["matched"]`` how many,
    ``values["dataset"]`` the registry's dataset."""
    ids = matching_ids(con, q, limit) if q.match else []
    firms, codes, people = firms_for_pull(con, ids, with_administrators)
    dataset = dataset_of(con)
    draft = onrc.firms_draft(firms, dataset, codes=codes, people=people, classes=q.classes,
                             with_administrators=with_administrators)
    draft.counts["matched"] = len(ids)
    draft.values["dataset"] = dataset
    return draft
