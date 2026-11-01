"""The upgrade from schema 24 to schema 25 (harvey/overlay/migrate.py) and its
verification (verify.py), on a small schema 24 database made of fictional
firms, people and contacts (example.com addresses, invented numbers)."""

import asyncio
import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from harvey.overlay import migrate, recount, schema, triggers, verify
from harvey.registers import Adapter
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

def _t(day: int, hour: int = 10) -> str:
    return f"2026-01-{day:02d} {hour:02d}:00:00"


# Firms: (id, name, cui, external_id, source, created day, website, domain,
# website_trust, location)
FIRMS = [
    ("aaaa00000001", "Exemplu Unu SRL", "1000004", "onrc:J08/1/2015", "registry:onrc", 1,
     "", "", "", "Brașov, Brașov, Romania"),
    ("aaaa00000002", "Exemplu Doi SRL", "1000012", "onrc:J08/2/2015", "registry:onrc", 1,
     "https://doi.example.com", "", "", "Brașov, Brașov, Romania"),
    ("aaaa00000003", "Exemplu Trei SRL", "1000020", "onrc:J08/3/2015", "registry:onrc", 2,
     "", "", "", "Codlea, Brașov, Romania"),
    ("aaaa00000004", "Exemplu Patru SRL", "1000039", "onrc:J08/4/2015", "registry:onrc", 2,
     "https://patru.example.com", "patru.example.com", "confirmed", "Cluj-Napoca, Cluj, Romania"),
    ("aaaa00000005", "Exemplu Cinci SRL", "1000047", "onrc:J08/5/2015", "registry:onrc", 3,
     "", "", "", "Brașov, Brașov, Romania"),
    ("aaaa00000006", "Exemplu Sase SRL", "", "onrc:J08/6/2015", "registry:onrc", 3,
     "", "", "", "Brașov, Brașov, Romania"),
    ("aaaa00000007", "Exemplu Sapte II", "", "onrc:J08/7/2015", "registry:onrc", 4,
     "", "", "", "Râșnov, Brașov, Romania"),
    ("bbbb00000001", "Cafe Exemplu", "", "osm:node/101", "discover:osm", 4,
     "https://cafe.example.com", "", "", "Brașov, Brașov"),
    ("bbbb00000002", "Dentist Exemplu", "", "dataforseo:cid:202", "discover:dataforseo_listings",
     5, "https://dentist.example.com", "dentist.example.com", "likely", "Brașov, Brașov"),
    ("bbbb00000003", "Atelier Exemplu", "1000055", "", "discover:serper", 5,
     "https://atelier.example.com", "", "unconfirmed", "Brașov"),
]
R1, R2, R3, R4, R5, R6, R7 = (f"aaaa0000000{i}" for i in range(1, 8))
L1, L2, L3 = "bbbb00000001", "bbbb00000002", "bbbb00000003"


def build_v24(path: Path) -> None:
    """A schema 24 database as the app leaves it, then fictional rows."""
    sm = StateManager(str(path))
    asyncio.run(sm.init_db())
    asyncio.run(sync_vocabulary(sm))
    con = sqlite3.connect(path)
    con.execute("INSERT OR IGNORE INTO contact_kinds (kind, label) VALUES ('contact_form', 'Form')")
    for code in ("TECH_STACK", "RUNNING_GOOGLE_ADS", "SERP_RANK", "PHONE_NUMBER", "VAT_REGISTERED",
                 "TURNOVER", "CONTACT_FOUND", "ACTIVITY_CODE"):
        assert con.execute("SELECT 1 FROM signal_codes WHERE code = ?", (code,)).fetchone(), code
    for fid, name, cui, ext, source, day, site, domain, trust, place in FIRMS:
        con.execute(
            "INSERT INTO companies (id, name, cui, external_id, source, created_at, website, domain, "
            "website_trust, website_source, location, address, phone, industry, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Str. Exemplu 1', '', 'Exemplu', ?)",
            (fid, name, cui, ext, source, _t(day), site, domain, trust,
             "tinyfish" if trust else "", place, f"https://listing.example.com/{fid}"
             if source.startswith("discover") else ""))
        if cui:
            con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                        "VALUES (?, 'cui', ?, ?)", (fid, cui, source))
        if ext:
            con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                        "VALUES (?, 'external', ?, ?)", (fid, ext, source))
        if domain:
            con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                        "VALUES (?, 'domain', ?, ?)", (fid, domain, source))
    # A listing kept by its place id before, beside its cid.
    con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                "VALUES (?, 'external', 'dataforseo:ChIJexample', 'discover:dataforseo_listings')",
                (L2,))
    # An identifier no module declares: kept aside.
    con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                "VALUES (?, 'external', 'nowhere:77', 'discover:osm')", (L1,))
    con.execute("UPDATE companies SET profile_checked_at = ?, prospects_checked_at = ? WHERE id = ?",
                (_t(6), _t(6), R2))

    # Merges as schema 24 makes them. 1: the listing L1 into R1, which takes
    # its website and its domain (moved). 2 and 3: a chain, R3 into R2, then
    # R2 into R5. 4: R6 into R4, undone.
    # (L3, a listing that showed a CUI, keeps its own.)
    con.execute("UPDATE companies SET website = 'https://cafe.example.com', "
                "domain = 'cafe.example.com' WHERE id = ?", (R1,))
    con.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (R1, L1))
    con.execute("INSERT INTO company_merges (id, survivor, merged, score, reasons_json, decided_by, "
                "filled_json, moved_json, created_at) VALUES (1, ?, ?, 0.9, '[\"domain\"]', 'auto', "
                "'{\"website\": \"https://cafe.example.com\", \"domain\": \"cafe.example.com\"}', "
                "'{\"domain\": \"cafe.example.com\"}', ?)", (R1, L1, _t(7)))
    con.execute("INSERT INTO company_ids (company_id, kind, value, source) "
                "VALUES (?, 'domain', 'cafe.example.com', 'discover:osm')", (R1,))
    con.execute("INSERT INTO company_merge_moves (merge_id, tbl, key1, key2) "
                "VALUES (1, 'company_ids', 'domain', 'cafe.example.com')")
    con.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (R2, R3))
    con.execute("INSERT INTO company_merges (id, survivor, merged, created_at, group_id) "
                "VALUES (2, ?, ?, ?, 7)", (R2, R3, _t(8)))
    con.execute("UPDATE companies SET merged_into = ? WHERE id IN (?, ?)", (R5, R2, R3))
    con.execute("INSERT INTO company_merge_moves (merge_id, tbl, key1) VALUES (3, 'companies', ?)",
                (R3,))
    con.execute("INSERT INTO company_merges (id, survivor, merged, created_at, group_id) "
                "VALUES (3, ?, ?, ?, 7)", (R5, R2, _t(9)))
    con.execute("INSERT INTO company_merges (id, survivor, merged, created_at, undone_at) "
                "VALUES (4, ?, ?, ?, ?)", (R4, R6, _t(9), _t(10)))
    con.executemany("INSERT INTO match_candidates (company_a, company_b, score, status) "
                    "VALUES (?, ?, ?, ?)", [(R1, L1, 0.9, "merged"), (R4, R6, 0.7, "separate"),
                                            (L2, L3, 0.6, "pending")])

    # Contacts and their evidence.
    contacts = [(1, "phone", "+40268000001", "mobile", "valid"),
                (2, "phone", "+40268000002", "fixed", "valid"),
                (3, "email", "office@doi.example.com", "", "unchecked"),
                (4, "contact_form", "https://doi.example.com/contact", "", ""),
                (5, "phone", "+40722000005", "mobile", "valid")]
    con.executemany("INSERT INTO contacts (id, kind, value, display, line_type, status, first_seen, "
                    "last_seen) VALUES (?, ?, ?, ?, ?, ?, '2026-01-02 10:00:00', "
                    "'2026-01-03 10:00:00')", [(i, k, v, v, lt, st) for i, k, v, lt, st in contacts])
    people = [(1, R1, "Ana Exemplu", "ana exemplu", "administrator", "onrc"),
              (2, R1, "Ion Exemplu", "ion exemplu", "administrator", "onrc"),
              (3, R2, "Maria Exemplu", "maria exemplu", "owner", "people"),
              (4, R5, "Dan Exemplu", "dan exemplu", "", "registry")]
    con.executemany("INSERT INTO people (id, company_id, name, name_norm, role, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)", people)
    links = [  # company, contact, person, source, capability, trust, origin
        (R1, 1, 0, "registry:onrc", "", "confirmed", R1),
        (R1, 1, 0, "anaf", "company_check", "confirmed", R1),
        (R1, 5, 2, "website", "read_site", "likely", R1),
        (R1, 2, 0, "discover:osm", "", "confirmed", L1),       # moved from the listing
        (R5, 3, 3, "website", "read_site", "unconfirmed", R2),
        (R5, 4, 0, "website", "read_site", "unconfirmed", R2),
        (L2, 2, 0, "phones", "site_phones", "unconfirmed", L2),  # a phone two firms share
        (R7, 5, 0, "registry:onrc", "", "confirmed", R7),
    ]
    con.executemany("INSERT INTO company_contacts (company_id, contact_id, person_id, source, "
                    "capability, trust, origin_company_id, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-02 10:00:00', '2026-01-04 10:00:00')",
                    links)

    # Searches: a deep website read, a plain one, the finder, one with an origin.
    searches = [("company", R5, "website", "read_site",
                 "phone,email,contact_form,social,people,owner", _t(11), "found", 2, R5),
                ("company", R4, "website", "read_site", "phone,contact_form", _t(11), "nothing", 0,
                 R4),
                ("company", R5, "contact_finder", "find_contacts", "website,people,email,phone",
                 _t(12), "found", 1, R2),
                ("company", L2, "tinyfish", "web_search", "website", _t(12), "error", 0, L2),
                ("person", "3", "people", "find_people", "people,email", _t(12), "nothing", 0, R2)]
    con.executemany("INSERT INTO searches (target, target_id, module, capability, looked_for, "
                    "searched_at, outcome, found, origin_company_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    searches)

    # Observations: a register's copies (left behind) and web signals.
    obs = [  # id, company, code, module, collector, num, text, at
        ("o01", R1, "VAT_REGISTERED", "anaf", "anaf", 1, "", _t(3)),
        ("o02", R1, "TURNOVER", "mf_financials", "financials", 50000, "2024", _t(3)),
        ("o03", R1, "CONTACT_FOUND", "onrc", "registry", None, "Ana Exemplu — administrator", _t(3)),
        ("o04", R7, "ACTIVITY_CODE", "", "registry", None, "5610/3", _t(3)),
        ("o05", R4, "RUNNING_GOOGLE_ADS", "web_marketing", "profile", 1, "", _t(11, 9)),
        ("o06", R4, "RUNNING_GOOGLE_ADS", "web_marketing", "profile", 1, "", _t(12, 9)),
        ("o07", R4, "RUNNING_GOOGLE_ADS", "web_marketing", "profile", 1, "", _t(13, 9)),
        ("o08", R4, "RUNNING_GOOGLE_ADS", "web_marketing", "profile", 0, "", _t(14, 9)),
        ("o09", R4, "RUNNING_GOOGLE_ADS", "web_marketing", "profile", 0, "", _t(15, 9)),
        ("o10", R4, "TECH_STACK", "web_marketing", "profile", None, "WordPress", _t(11)),
        ("o11", R4, "TECH_STACK", "web_marketing", "profile", None, "HubSpot", _t(11)),
        ("o12", R5, "TECH_STACK", "", "profile", None, "Shopify", _t(11)),
        ("o13", L2, "SERP_RANK", "", "discover", 14, "dentist brasov", _t(5)),
        ("o14", L2, "SERP_RANK", "", "discover", 9, "dentist brasov", _t(6)),
        ("o15", R5, "PHONE_NUMBER", "phones", "profile", None, "+40268000009", _t(11)),
        ("o16", R1, "TECH_STACK", "web_marketing", "profile", None, "Wix", _t(11)),
    ]
    con.executemany("INSERT INTO observations (id, company_id, signal_code, module, collector, "
                    "value_num, value_text, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", obs)
    # The listing's tech row moved to the survivor by merge 1, logged.
    con.execute("UPDATE observations SET company_id = ? WHERE id = 'o16'", (R1,))
    con.execute("INSERT INTO company_merge_moves (merge_id, tbl, key1) VALUES (1, 'observations', "
                "'o16')")
    con.executemany("INSERT INTO company_sources (company_id, source, ref, origin_company_id) "
                    "VALUES (?, ?, ?, ?)",
                    [(R1, "registry:onrc", "J08/1/2015", R1), (R1, "anaf", "", R1),
                     (R1, "discover:osm", "node/101", L1), (L2, "discover:dataforseo_listings",
                                                            "cid:202", L2)])
    con.execute("INSERT INTO locations (company_id, name, locality, county, external_id, source, "
                "origin_company_id) VALUES (?, 'Cafe Exemplu', 'Brașov', 'Brașov', 'node/101', "
                "'discover:osm', ?)", (R1, L1))
    # The phone the listing showed was found for that place.
    con.execute("UPDATE company_contacts SET location_id = 1 WHERE contact_id = 2 "
                "AND origin_company_id = ?", (L1,))
    con.execute("INSERT INTO prospects (id, company_id, first_name, last_name, email, status, "
                "source) VALUES ('p1', ?, 'Maria', 'Exemplu', 'maria@doi.example.com', 'new', "
                "'website')", (R5,))
    con.execute("INSERT INTO jobs (id, kind, lane, params_json) VALUES ('j1', 'find_contacts', "
                "'contacts', ?)", (f'{{"company_ids": ["{R4}", "{L2}"]}}',))
    con.execute("INSERT INTO settings (key, value) VALUES ('example', 'on')")
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def v24(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("v24") / "harvey.db"
    build_v24(path)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(src: Path, dst: Path, **kw) -> dict:
    return migrate.migrate(src, dst, min_free=0, **kw)


def _con(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _firm(con, uuid: str) -> int:
    """New id by creation order: the fixture's order of FIRMS (created day,
    then id) is the id order."""
    order = sorted(FIRMS, key=lambda f: (f[5], f[0]))
    return [f[0] for f in order].index(uuid) + 1


def dump(path: Path) -> dict:
    """Every table's rows (the migration's own timings left out)."""
    con = _con(path)
    try:
        out = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                   "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'local_fts%' "
                                   "ORDER BY name"):
            cols = "step, done, last, rows" if name == "migrate_progress" else "*"
            if name == "migrate_progress":
                out[name] = con.execute(f"SELECT step, done FROM {name} ORDER BY step").fetchall()
                continue
            out[name] = sorted(con.execute(f"SELECT {cols} FROM {name}").fetchall(), key=repr)
        out["fts"] = con.execute("SELECT rowid FROM local_fts WHERE local_fts MATCH 'exemplu' "
                                 "ORDER BY rowid").fetchall()
        return out
    finally:
        con.close()


# ── What moves over ──

def test_the_upgrade_keeps_what_harvey_added_and_leaves_the_register_copies(v24, tmp_path):
    before = _sha(v24)
    dst = tmp_path / "harvey.db.v25"
    report = _run(v24, dst, chunk=3)
    assert _sha(v24) == before                      # the source is only read
    assert report["finish"]["done"]
    assert not Path(str(dst) + ".work").exists()
    con = sqlite3.connect(dst, isolation_level=None)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731

    assert schema.version(con) == schema.VERSION
    assert {k: v for k, v in recount.diff(con).items() if v} == {}
    assert triggers.missing(con) == []
    # Firms: new ids in creation order; merges flattened.
    assert one("SELECT COUNT(*) FROM firm") == len(FIRMS)
    r1, r2, r3, r4, r5, r6, r7 = (_firm(con, u) for u in (R1, R2, R3, R4, R5, R6, R7))
    l1, l2, l3 = (_firm(con, u) for u in (L1, L2, L3))
    live = dict(q("SELECT id, live FROM firm"))
    assert live[l1] == r1 and live[r3] == r5 and live[r2] == r5 and live[r6] == 0
    assert one("SELECT COUNT(*) FROM firm WHERE live = 0") == 7
    assert q("SELECT merge, firm FROM merge_member WHERE merge = 3 ORDER BY firm") == \
        [(3, r2), (3, r3)]
    assert one("SELECT undone_at IS NOT NULL FROM merge WHERE id = 4") == 1
    assert one("SELECT COUNT(*) FROM match_candidate") == 3
    # Identifiers: integer CUIs, registration numbers, listing ids (an alias
    # too); the one no module declares is kept aside.
    idents = {(s, k): f for s, k, f in q(
        "SELECT s.name, fi.key, fi.firm FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme")}
    assert idents[("ro.cui", 1000004)] == r1
    assert one("SELECT typeof(key) FROM firm_ident fi JOIN ident_scheme s ON s.id = fi.scheme "
               "WHERE s.name = 'ro.cui' LIMIT 1") == "integer"
    assert idents[("ro.onrc_reg", "J08/7/2015")] == r7
    assert idents[("osm.element", "node/101")] == l1
    assert idents[("dataforseo.listing", "cid:202")] == l2
    assert idents[("dataforseo.listing", "ChIJexample")] == l2
    assert idents[("ro.cui", 1000055)] == l3
    assert q("SELECT firm, kind, value FROM migration_note WHERE kind LIKE 'ident:%'") == \
        [(l1, "ident:external", "nowhere:77")]
    # Websites: declared ones (no trust) kept as declared; a merge's fill
    # marked on its survivor, the moved domain back on the listing.
    site = {f: (w, d, t, fill) for f, w, d, t, fill in q(
        "SELECT id, website, domain, website_trust, website_fill FROM firm")}
    assert site[r2] == ("https://doi.example.com", None, None, None)
    assert site[r4] == ("https://patru.example.com", "patru.example.com",
                        schema.TRUST["confirmed"], None)
    assert site[r1] == ("https://cafe.example.com", None, None, 1)
    assert site[l1][1] == "cafe.example.com"
    # Firms no register describes are described locally (a firm the
    # registry knows by its number only is described by the registry).
    assert sorted(r[0] for r in q("SELECT firm FROM local_firm")) == sorted([l1, l2])
    assert q("SELECT locality, county FROM local_firm WHERE firm = ?", l1) == \
        [("Brașov", "Brașov")]
    assert q("SELECT rowid FROM local_fts WHERE local_fts MATCH 'cafe'") == [(l1,)]
    # Values with no column kept aside; names not compared without a register.
    assert one("SELECT COUNT(*) FROM migration_note WHERE kind = 'prospects_checked_at'") == 1
    # (every firm a register knows: 6 by CUI, 2 by registration number only)
    assert one("SELECT COUNT(*) FROM migration_note WHERE kind = 'name'") == 8
    assert report["names"]["detail"]["not_named"] == ["ro.cui", "ro.onrc_reg"]
    # Contacts as they are; evidence keyed to the record it was found for.
    assert one("SELECT COUNT(*) FROM contact") == 6        # 5 + the prospect's email
    assert dict(q("SELECT id, status FROM contact WHERE kind = 2")) == {1: 5, 2: 5, 5: 5}
    assert one("SELECT line_type FROM contact WHERE id = 1") == schema.LINE_TYPE["mobile"]
    assert one("SELECT COUNT(*) FROM link_evidence") == 9      # 8 + the prospect's own
    assert one("SELECT firm FROM link_evidence WHERE contact = 2 AND source = "
               "(SELECT id FROM source WHERE name = 'discover:osm')") == l1
    assert one("SELECT COUNT(*) FROM link_evidence WHERE firm = ?", r2) == 2
    assert one("SELECT n_firms FROM contact WHERE id = 2") == 2   # R1 (via the listing) and L2
    assert one("SELECT location FROM link WHERE firm = ? AND contact = 2", l1) == 1
    assert one("SELECT COUNT(*) FROM link WHERE location != 0") == 1
    assert one("SELECT n_contacts FROM firm WHERE id = ?", r1) == 3
    # People: a register's administrator stays only when a link names him.
    assert sorted(r[0] for r in q("SELECT id FROM person")) == [2, 3, 4, 5]
    assert one("SELECT firm FROM person WHERE id = 3") == r2
    # Searches, with their origin and the deep-read bit.
    assert one("SELECT COUNT(*) FROM search") == 5
    assert one("SELECT origin FROM search WHERE id = 3") == r2
    assert one("SELECT is_person FROM search WHERE id = 5") == 1
    deep = one("SELECT id FROM role WHERE name = 'deep_bit'")
    assert one("SELECT looked_for & ? FROM search WHERE id = 1", deep) == deep
    assert one("SELECT site_read_deep IS NOT NULL FROM firm WHERE id = ?", r5) == 1
    assert one("SELECT site_read_at IS NOT NULL AND site_read_deep IS NULL FROM firm "
               "WHERE id = ?", r4) == 1
    assert one("SELECT last_search_at FROM firm WHERE id = ?", l2) is None   # an error
    # Web signals move with history only on change; a register's copies don't.
    codes = dict(q("SELECT s.code, COUNT(*) FROM obs o JOIN signal s ON s.id = o.signal "
                   "GROUP BY s.code"))
    assert codes == {"RUNNING_GOOGLE_ADS": 2, "TECH_STACK": 4, "SERP_RANK": 2, "PHONE_NUMBER": 1}
    ads = one("SELECT id FROM signal WHERE code = 'RUNNING_GOOGLE_ADS'")
    at, checked, positive = con.execute("SELECT at, checked_at, positive FROM reading "
                                        "WHERE signal = ?", (ads,)).fetchone()
    assert positive == 0 and checked > at
    assert one("SELECT firm FROM obs WHERE text = 'Wix'") == l1    # moved by the merge: back
    assert one("SELECT n_true FROM signal_tally WHERE signal = (SELECT id FROM signal "
               "WHERE code = 'TECH_STACK')") == 3                   # R4, R5, R1 (via the listing)
    # Sources, outreach, legacy ids.
    assert one("SELECT COUNT(*) FROM firm_source") == 4
    assert one("SELECT firm FROM firm_source WHERE ref = 'node/101'") == l1
    assert one("SELECT firm FROM prospects WHERE id = 'p1'") == r5
    assert one("SELECT firm FROM locations") == l1
    assert one("SELECT COUNT(*) FROM jobs") == 1
    assert dict(q("SELECT uuid, firm FROM legacy_id")) == {R4: r4, L2: l2}
    # The vocabulary keeps the user's confirmations; every source is listed.
    assert one("SELECT status FROM signal WHERE code = 'TECH_STACK'") == \
        sqlite3.connect(v24).execute("SELECT status FROM signal_codes WHERE code = 'TECH_STACK'"
                                     ).fetchone()[0]
    how = dict(q("SELECT m.name, ss.how FROM signal_source ss JOIN module m ON m.id = ss.module "
                 "JOIN signal s ON s.id = ss.signal WHERE s.code = 'PHONE_NUMBER'"))
    assert how["phones"] == 0 and how["anaf"] == 1 and how["mf_taxpayers"] == 1
    con.close()


class Stop(Exception):
    pass


def test_a_run_cut_short_at_any_point_resumes_to_the_same_file(v24, tmp_path):
    whole = tmp_path / "whole.db"
    _run(v24, whole, chunk=2, obs_chunk=3)
    expected = dump(whole)
    cut = tmp_path / "cut.db"
    calls, stops = [0], 0
    for every in (1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 1000):
        def stop(step, last, every=every):
            calls[0] += 1
            if calls[0] % every == 0:
                raise Stop(step)
        try:
            _run(v24, cut, chunk=2, obs_chunk=3, on_chunk=stop)
            break
        except Stop:
            stops += 1
    assert stops >= 5
    assert dump(cut) == expected


def test_a_killed_process_resumes_to_the_same_file(v24, tmp_path):
    whole = tmp_path / "whole.db"
    _run(v24, whole, chunk=1, obs_chunk=1)
    cut = tmp_path / "cut.db"
    script = textwrap.dedent(f"""
        import time
        from harvey.overlay import migrate
        migrate.migrate({str(v24)!r}, {str(cut)!r}, min_free=0, chunk=1, obs_chunk=1,
                        on_chunk=lambda step, last: time.sleep(0.05))
    """)
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    proc = subprocess.Popen([sys.executable, "-c", script], env=env)
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if cut.exists():
                try:
                    c = _con(cut)
                    n = c.execute("SELECT COUNT(*) FROM migrate_progress WHERE done = 1").fetchone()[0]
                    c.close()
                except sqlite3.Error:
                    continue
                if n >= 6:
                    break
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait()
    assert proc.returncode == -signal.SIGKILL
    c = _con(cut)
    assert c.execute("SELECT done FROM migrate_progress WHERE step = 'finish'").fetchone() is None
    c.close()
    _run(v24, cut, chunk=1, obs_chunk=1)
    assert dump(cut) == dump(whole)


def test_the_upgrade_refuses_to_start_without_room_or_on_the_wrong_files(v24, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(migrate, "free_bytes", lambda path: 29 * 10**9)
    with pytest.raises(migrate.Refused, match="30 GB"):
        migrate.migrate(v24, tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()
    monkeypatch.setattr(migrate, "free_bytes", lambda path: 31 * 10**9)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (a)")
    with pytest.raises(migrate.Refused, match="schema 0"):
        migrate.migrate(other, tmp_path / "out.db")
    with pytest.raises(migrate.Refused, match="isn't an upgrade's output"):
        migrate.migrate(v24, other)


class NamesRegister(Adapter):
    """A register that names firms by CUI and by registration number (fictional tables)."""
    module = "onrc"

    def names_sql(self, scheme=""):
        if scheme in ("", "ro.cui"):
            return ("SELECT fi.firm, r.name FROM firm_ident fi CROSS JOIN reg.names r "
                    "WHERE fi.scheme = ? AND r.cui = fi.key")
        return ("SELECT fi.firm, r.name FROM firm_ident fi CROSS JOIN reg.by_reg r "
                "WHERE fi.scheme = ? AND r.reg = fi.key")


def test_a_name_that_differs_from_the_register_goes_into_the_note(v24, tmp_path):
    reg = tmp_path / "registry.db"
    c = sqlite3.connect(reg)
    c.execute("CREATE TABLE names (cui INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO names VALUES (?, ?)",
                  [(1000004, "Exemplu Unu SRL"), (1000012, "EXEMPLU DOI SRL"),
                   (1000020, "Exemplu Trei SRL"), (1000039, "Exemplu Patru SRL"),
                   (1000047, "Exemplu Cinci SRL")])
    # By registration number: the firm with no CUI is there, another isn't.
    c.execute("CREATE TABLE by_reg (reg TEXT PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO by_reg VALUES (?, ?)",
                  [("J08/1/2015", "Exemplu Unu SRL"), ("J08/6/2015", "Exemplu Sase SRL")])
    c.commit()
    c.close()
    dst = tmp_path / "out.db"
    _run(v24, dst, registers=[NamesRegister("reg", reg, keyed_by="ro.cui")])
    con = _con(dst)
    notes = con.execute("SELECT firm, value FROM migration_note WHERE kind = 'name'").fetchall()
    # The renamed firm; a firm the register has under neither of its keys
    # (a CUI, a registration number) is described locally, marked gone (its
    # last name and place kept); one it has by number only is described by it.
    assert notes == [(_firm(con, R2), "Exemplu Doi SRL")]
    gone = dict(con.execute("SELECT firm, gone FROM local_firm WHERE gone = 1").fetchall())
    assert gone == {_firm(con, L3): 1, _firm(con, R7): 1}
    assert con.execute("SELECT name FROM local_firm WHERE firm = ?",
                       (_firm(con, L3),)).fetchone() == ("Atelier Exemplu",)
    assert con.execute("SELECT COUNT(*) FROM local_firm WHERE firm = ?",
                       (_firm(con, R6),)).fetchone() == (0,)
    assert con.execute("SELECT COUNT(*) FROM firm WHERE display_name IS NOT NULL").fetchone()[0] == 0


def test_verification_matches_the_old_counts(v24, tmp_path):
    dst = tmp_path / "out.db"
    _run(v24, dst)
    rep = verify.report(dst, v24, sections=("doctor", "counts", "stored"))
    assert {k: v for k, v in rep["doctor"].items() if v} == {}
    c = rep["counts"]
    for name in ("firms (live)", "firms (all records)", "contacts", "link evidence", "searches",
                 "sources (record found for, source, reference)", "merges", "merges in force",
                 "merged records", "match candidates", "people Harvey found or a link names",
                 "prospects", "jobs", "websites (live)", "links (record found for, contact)",
                 "links (live firm, contact)", "links with a place", "searches with an origin"):
        assert c[name]["old"] == c[name]["new"] and c[name]["passed"], name
    for code, row in rep["stored"].items():
        assert (row["old_true"], row["old_checked"]) == (row["new_true"], row["new_checked"]), code
    assert rep["passed"] and rep["failures"] == []


def test_verification_fails_on_a_wrong_output(v24, tmp_path):
    """The checks can fail: a row lost in the output, and a register signal
    whose readings no loaded module provides."""
    dst = tmp_path / "out.db"
    _run(v24, dst)
    con = sqlite3.connect(dst)
    con.execute("DELETE FROM search WHERE id = 2")
    con.commit()
    con.close()
    rep = verify.report(dst, v24, sections=("counts", "registers"), adapters=[])
    assert not rep["passed"]
    assert any(f.startswith("counts: searches:") for f in rep["failures"])
    vat = rep["registers"]["VAT_REGISTERED"]
    assert vat["by cause"] == {"no loaded module reads it": 1} and not vat["passed"]


def test_place_lines_split_into_locality_and_county():
    assert migrate.place_parts("Brașov, Brașov, Romania") == ("Brașov", "Brașov")
    assert migrate.place_parts("Denver, CO") == ("Denver", "CO")
    assert migrate.place_parts("Codlea") == ("Codlea", "")
    assert migrate.place_parts("") == ("", "")


def test_a_source_written_to_during_the_run_is_refused(tmp_path):
    """The upgrade reads the schema 24 file in many transactions: a write
    to it while it runs (or before a resume) would carry half of it over,
    so the run refuses before finishing, and so does every resume."""
    src, dst = tmp_path / "v24.db", tmp_path / "v25.db"
    build_v24(src)
    written = []

    def write_once(step, last):
        if step == "evidence" and not written:
            written.append(1)
            w = sqlite3.connect(src)
            w.execute("INSERT INTO contacts (id, kind, value, line_type, status) "
                      "VALUES (999, 'phone', '+40700000999', 'mobile', 'valid')")
            w.execute("INSERT INTO company_contacts (company_id, contact_id, source, trust) "
                      "VALUES (?, 999, 'website', 'confirmed')", (R4,))
            w.commit()
            w.close()

    with pytest.raises(migrate.Refused, match="written to after this upgrade started"):
        _run(src, dst, chunk=2, on_chunk=write_once)
    with pytest.raises(migrate.Refused, match="contacts"):
        _run(src, dst, chunk=2)
    c = _con(dst)
    assert c.execute("SELECT done FROM migrate_progress WHERE step = 'finish'").fetchone() is None
    c.close()


def test_the_upgrade_refuses_while_a_worker_holds_the_database(tmp_path):
    src = tmp_path / "v24.db"
    build_v24(src)
    w = sqlite3.connect(src)
    w.execute("INSERT INTO worker_lease (name, owner, heartbeat_at, started_at) "
              "VALUES ('worker', 'example', ?, ?)", (time.time(), time.time()))
    w.commit()
    with pytest.raises(migrate.Refused, match="a worker is running"):
        _run(src, tmp_path / "v25.db")
    w.execute("UPDATE worker_lease SET heartbeat_at = ?", (time.time() - 3600,))   # stale
    w.commit()
    w.close()
    assert _run(src, tmp_path / "v25.db")["finish"]["done"]


def test_what_a_merge_copied_onto_a_survivor_stays_the_merged_records(tmp_path):
    """Schema 24 merges copied empty columns (an address) onto the survivor;
    schema 25 keeps the survivor's own description, so undoing the merge
    leaves nothing of the merged record behind."""
    from harvey.overlay import merges

    src = tmp_path / "v24.db"
    build_v24(src)
    w = sqlite3.connect(src)
    # L2 had no address; the merge of L3 into it copied L3's.
    w.execute("UPDATE companies SET address = 'Str. Atelierului 5' WHERE id IN (?, ?)", (L2, L3))
    w.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (L2, L3))
    w.execute("INSERT INTO company_merges (id, survivor, merged, filled_json, created_at) "
              "VALUES (5, ?, ?, '{\"address\": \"Str. Atelierului 5\"}', ?)", (L2, L3, _t(9)))
    w.commit()
    w.close()
    dst = tmp_path / "v25.db"
    report = _run(src, dst)
    assert report["merges"]["detail"]["fills_cleared"] == 1
    con = sqlite3.connect(dst, isolation_level=None)
    l2, l3 = _firm(con, L2), _firm(con, L3)
    assert con.execute("SELECT address FROM local_firm WHERE firm = ?", (l2,)).fetchone() == (None,)
    merges.undo(con, 5)
    assert con.execute("SELECT live FROM firm WHERE id = ?", (l3,)).fetchone() == (0,)
    assert con.execute("SELECT address FROM local_firm WHERE firm = ?", (l2,)).fetchone() == (None,)


def test_free_space_counts_the_drive_a_virtual_disk_lives_on(tmp_path, monkeypatch):
    from harvey import disk

    usage = {str(tmp_path): 500 * 10**9}
    real = disk.shutil.disk_usage

    def fake(path):
        for prefix, free in sorted(usage.items(), key=lambda kv: -len(kv[0])):
            if str(path).startswith(prefix):
                return real(tmp_path)._replace(free=free)
        return real(path)

    monkeypatch.setattr(disk.shutil, "disk_usage", fake)
    monkeypatch.setattr(disk, "on_wsl", lambda: True)
    monkeypatch.setattr(disk, "DEFAULT_WSL_DRIVE", tmp_path / "no-such-drive")
    monkeypatch.delenv("HARVEY_HOST_DRIVE", raising=False)
    with pytest.raises(disk.HostDriveUnknown):
        disk.free_bytes(tmp_path / "out.db")
    src = tmp_path / "v24.db"
    build_v24(src)
    with pytest.raises(migrate.Refused, match="HARVEY_HOST_DRIVE"):
        migrate.migrate(src, tmp_path / "out.db")
    host = tmp_path / "host"
    host.mkdir()
    usage[str(host)] = 20 * 10**9
    monkeypatch.setenv("HARVEY_HOST_DRIVE", str(host))
    assert disk.free_bytes(tmp_path / "out.db") == 20 * 10**9
    with pytest.raises(migrate.Refused, match="20.0 GB free"):
        migrate.migrate(src, tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()
    monkeypatch.setenv("HARVEY_HOST_DRIVE", "none")
    assert disk.free_bytes(tmp_path / "out.db") == 500 * 10**9
