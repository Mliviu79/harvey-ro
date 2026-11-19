"""Records for a selection of registry firms, made by SQL over the attached
registry (overlay.store.Store.ensure_registry_records).

The registry is the one tests/test_registry.py builds, plus two CUIs on two
registrations each. Every company, person and number here is invented.
"""

import asyncio
import sqlite3
import time
from dataclasses import replace

import pytest

from harvey import queries as named
from harvey import registry
from harvey.collectors import onrc
from harvey.models import Company
from harvey.modules import job_kinds, runs
from harvey.modules.ro.mf_financials.store import Filter as FinFilter
from harvey.modules.ro.mf_taxpayers.store import Filter as TaxFilter
from harvey.modules.ro.onrc import queries as onrc_queries
from harvey.overlay import queries as overlay_queries
from harvey.overlay import recount, store
from harvey.registers import Stale
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim
from tests.test_onrc import CAEN, DATASET, FIRMS, PEOPLE, STATUS, firm, write
from tests.test_registry import (  # noqa: F401  (con, db_path and state are fixtures)
    EXTRA_CAEN, EXTRA_FIRMS, EXTRA_STATUS, con, db_path, state)

# One CUI on two registrations: an old one struck off, the one it operates
# under now (its canonical row).
PAIR = [
    firm("DOUA EXEMPLU SRL", "90000171", "J08/995/2005", "SRL", "Braşov", "Municipiul Braşov"),
    firm("DOUA EXEMPLU SRL", "90000171", "J08/996/2020", "SRL", "Braşov", "Municipiul Braşov"),
]
PAIR_CAEN = [("J08/995/2005", "8623", "2"), ("J08/996/2020", "8623", "2")]
PAIR_STATUS = [("J08/995/2005", "1084", ""), ("J08/996/2020", "1048", "")]
# One CUI on two registrations that both operate, in two counties: the
# canonical row is the higher number's, the one in Cluj.
TWINS = [
    firm("GEMENE EXEMPLU SRL", "90000188", "J08/997/2012", "SRL", "Braşov", "Municipiul Braşov"),
    firm("GEMENE EXEMPLU SRL", "90000188", "J12/998/2021", "SRL", "Cluj",
         "Municipiul Cluj-Napoca"),
]
TWINS_CAEN = [("J08/997/2012", "4778", "2"), ("J12/998/2021", "4778", "2")]
TWINS_STATUS = [("J08/997/2012", "1048", ""), ("J12/998/2021", "1048", "")]

ZAMBET = {("ro.cui", 90000017), ("ro.onrc_reg", "J08/100/2010")}
DINTE = {("ro.cui", 90000025), ("ro.onrc_reg", "J2025000200088")}
SOLE_TRADER = {("ro.onrc_reg", "F08/800/2018")}                 # no CUI
NAME_ONLY = {("ro.cui", 90000112), ("ro.onrc_reg", "J08/990/2019")}
DOUA = {("ro.cui", 90000171), ("ro.onrc_reg", "J08/996/2020")}
GEMENE = {("ro.cui", 90000188), ("ro.onrc_reg", "J12/998/2021")}
# "dentist BV": operating firms in Brașov authorised for dentistry, and
# the one that only says so in its name, in the registry's name order.
DENTISTS = [NAME_ONLY, DINTE, DOUA, SOLE_TRADER, ZAMBET]


@pytest.fixture
def raw(tmp_path):
    folder = tmp_path / "raw"
    folder.mkdir()
    return {
        "caen": write(folder / onrc.FILES["caen"], CAEN + EXTRA_CAEN + PAIR_CAEN + TWINS_CAEN),
        "status": write(folder / onrc.FILES["status"],
                        STATUS + EXTRA_STATUS + PAIR_STATUS + TWINS_STATUS),
        "firms": write(folder / onrc.FILES["firms"], FIRMS + EXTRA_FIRMS + PAIR + TWINS),
        "people": write(folder / onrc.FILES["people"], PEOPLE),
    }


def counted(selected, existing=0, created=0, left=0, **cui) -> dict:
    """What a call returns: the counts named, the others 0."""
    return {"selected": selected, "existing": existing, "created": created, "left": left,
            "cui_added": 0, "cui_left": 0, "cui_conflicts": 0, "unkeyable": 0, "excluded": 0,
            **cui}


def records(state_or_path) -> dict[int, set]:
    """{record: its identifiers as (scheme, key)}."""
    out: dict[int, set] = {}
    for fid, scheme, key in slim.q(state_or_path,
                                   "SELECT fi.firm, s.name, fi.key FROM firm_ident fi "
                                   "JOIN ident_scheme s ON s.id = fi.scheme"):
        out.setdefault(fid, set()).add((scheme, key))
    return out


def held(state_or_path) -> list[set]:
    return sorted(records(state_or_path).values(), key=sorted)


def same(found, expected) -> bool:
    return sorted(found, key=sorted) == sorted(expected, key=sorted)


def identity(state) -> int:
    return slim.one(state, "SELECT n FROM revision WHERE name = 'identity'") or 0


def tally(state) -> dict:
    return {name: n for name, n in slim.q(state, "SELECT name, n FROM tally") if n}


def positions(state) -> dict:
    return {r[0]: r[1:] for r in slim.q(
        state, "SELECT id, name_order, typeof(name_order), place_order, typeof(place_order) "
               "FROM firm")}


def firms(state) -> int:
    return slim.one(state, "SELECT COUNT(*) FROM firm")


def sequence(state):
    return slim.one(state, "SELECT seq FROM sqlite_sequence WHERE name = 'firm'")


def recount_differences(state) -> dict:
    """What `harvey doctor --recount` would list (overlay.recount.diff)."""
    db = sqlite3.connect(f"file:{state.db_path}?mode=ro", uri=True, isolation_level=None)
    try:
        return {k: v for k, v in recount.diff(db).items() if v}
    finally:
        db.close()


def firms_of(con, ids) -> set:
    """The firms a list of registry rows stands for, by CUI or (a firm with
    none) registration number: a row that isn't its CUI's canonical one
    stands for the canonical row."""
    return {r[0] for r in con.execute(
        "SELECT COALESCE(k.cui, k.reg) FROM firms g LEFT JOIN firm_by_cui b "
        "ON g.cui != '' AND b.cui = CAST(g.cui AS INTEGER) "
        "JOIN firm_key k ON k.firm_id = COALESCE(b.firm_id, g.id) "
        f"WHERE g.id IN ({','.join('?' * len(ids))})", ids)}


def matching(con, q) -> set:
    """The firms the registry's own search finds for a query."""
    return firms_of(con, registry.matching_ids(con, q))


# ── The two selections ──

@pytest.mark.asyncio
async def test_records_for_firms_chosen_by_registration_number(state, db_path):
    # A firm with a CUI, one with none, and a number the registry doesn't have.
    got = await state.ensure_registry_records(
        regs=["J08/100/2010", "F08/800/2018", "J99/1/1900"])
    assert got == counted(2, created=2)
    assert same(held(state), [ZAMBET, SOLE_TRADER])
    # A firm row and its identifiers, nothing made up front: no source, no
    # website or domain (the registry declares one for this firm), no
    # contact, no local description.
    assert slim.q(state, "SELECT live, website, domain, website_trust, website_source, "
                         "display_name, n_contacts FROM firm") == [(0, None, None, None, None,
                                                                    None, 0)] * 2
    for table in ("firm_source", "contact", "link", "link_evidence", "local_firm", "firm_key",
                  "person", "obs"):
        assert slim.one(state, f"SELECT COUNT(*) FROM {table}") == 0, table
    # Read like any firm a pull saved: described by the registry.
    found = await state.get_company_by_cui("90000017")
    assert found.name == "ZÂMBET EXEMPLU SRL" and found.location == "Brașov, Romania"
    assert set(await state.held_ids("ro.onrc_reg", ["F08/800/2018"])) == {"F08/800/2018"}


@pytest.mark.asyncio
async def test_records_for_the_firms_a_registry_query_matches(state, con):
    q = registry.resolve(con, "dentist BV")
    got = await state.ensure_registry_records(query=q)
    assert got == counted(5, created=5)
    assert same(held(state), DENTISTS)
    # Ids follow the registry's name order.
    assert [records(state)[i] for i in sorted(records(state))] == DENTISTS


@pytest.mark.asyncio
async def test_a_second_run_creates_nothing(state, con):
    q = registry.resolve(con, "dentist BV")
    await state.ensure_registry_records(query=q)
    before = (held(state), identity(state), tally(state), positions(state), sequence(state))
    assert await state.ensure_registry_records(query=q) == counted(5, existing=5)
    by_number = await state.ensure_registry_records(
        regs=["J08/100/2010", "F08/800/2018", "J08/996/2020"])
    assert by_number == counted(3, existing=3)
    assert (held(state), identity(state), tally(state), positions(state),
            sequence(state)) == before


@pytest.mark.asyncio
async def test_a_firm_with_a_record_gets_no_second_one(state, con):
    # One record holds the CUI alone, a pull saved another, and a third firm
    # was merged into a listing.
    by_cui = await state.add_company(Company(name="Clinica Zâmbet", cui="90000017"))
    await runs.registry_pull(state, con, registry.resolve(con, "8623/3"))
    listing = await state.add_company(Company(name="Farmacia Exemplu",
                                              domain="farma-exemplu.example.ro"))
    await state.ensure_registry_records(regs=["J08/600/2015"])
    farma = slim.firm_of_cui(state, 90000066)
    await state.merge_companies(listing, str(farma))
    assert slim.one(state, "SELECT live FROM firm WHERE id = ?", farma) == int(listing)
    before = records(state)
    assert same(before.values(), [{("ro.cui", 90000017)}, DINTE,
                                  {("ro.cui", 90000066), ("ro.onrc_reg", "J08/600/2015")}])
    count = firms(state)
    got = await state.ensure_registry_records(
        regs=["J08/100/2010", "J2025000200088", "J08/600/2015", "J08/990/2019"])
    assert got == counted(4, existing=3, created=1)
    after = records(state)
    # The records on file gained nothing; the one new firm has its own.
    assert {k: after[k] for k in before} == before
    assert after[int(by_cui)] == {("ro.cui", 90000017)}
    assert [v for k, v in after.items() if k not in before] == [NAME_ONLY]
    assert firms(state) == count + 1
    got = await state.ensure_registry_records(query=registry.resolve(con, "dentist BV"))
    assert got == counted(5, existing=3, created=2)


# ── A record known by a registration number only ──

@pytest.mark.asyncio
async def test_a_record_known_by_number_only_gains_its_cui(state, con):
    by_number = await state.add_company(Company(name="Cluj Exemplu",
                                                external_id="onrc:J12/400/2012"))
    # A firm with no CUI is known by its number, and that is all of it.
    trader = await state.add_company(Company(name="Testescu Ana",
                                             external_id="onrc:F08/800/2018"))
    regs = ["J12/400/2012", "F08/800/2018"]
    start = identity(state)
    # Counting writes nothing.
    assert await state.ensure_registry_records(regs=regs, limit=0) == counted(
        2, existing=2, cui_left=1)
    assert identity(state) == start
    got = await state.ensure_registry_records(regs=regs)
    assert got == counted(2, existing=2, cui_added=1)
    assert records(state) == {
        int(by_number): {("ro.cui", 90000041), ("ro.onrc_reg", "J12/400/2012")},
        int(trader): SOLE_TRADER}
    assert identity(state) == start + 1 and firms(state) == 2
    assert recount_differences(state) == {}
    # Reached by its CUI from now on.
    assert (await state.get_company_by_cui("90000041")).id == by_number
    assert await state.ensure_registry_records(regs=regs) == counted(2, existing=2)
    assert await state.ensure_registry_records(
        query=registry.resolve(con, "8623 CJ")) == counted(1, existing=1)


@pytest.mark.asyncio
async def test_a_record_known_by_an_old_number_only_is_the_firms_record(state, con):
    # The kept places are current, so a record that changes rows is placed again.
    assert await state.refresh_orders() == 0
    old = await state.add_company(Company(name="Doua Exemplu",
                                          external_id="onrc:J08/995/2005"))
    row = {reg: rid for reg, rid in con.execute(
        "SELECT reg, id FROM firms WHERE cui = '90000171'")}
    assert positions(state)[int(old)][0] == row["J08/995/2005"]
    # Chosen by its canonical number, by both, or matched by a query: the
    # firm has a record, and no second one is made.
    for selection in ({"regs": ["J08/996/2020"]}, {"regs": ["J08/995/2005", "J08/996/2020"]},
                      {"query": registry.resolve(con, "doua")}):
        assert await state.ensure_registry_records(**selection, limit=0) == counted(
            1, existing=1, cui_left=1)
    got = await state.ensure_registry_records(regs=["J08/996/2020"])
    assert got == counted(1, existing=1, cui_added=1)
    assert records(state) == {int(old): {("ro.cui", 90000171), ("ro.onrc_reg", "J08/995/2005")}}
    # By its CUI it is the canonical row now, and placed there.
    assert positions(state)[int(old)][0] == row["J08/996/2020"]
    assert await state.ensure_registry_records(
        query=registry.resolve(con, "dentist BV")) == counted(5, existing=1, created=4)
    assert firms(state) == 5 and recount_differences(state) == {}


@pytest.mark.asyncio
async def test_a_cui_on_another_record_leaves_both_alone(state, con):
    # The number on one record, the CUI on another firm's.
    by_number = await state.add_company(Company(name="Cluj Exemplu",
                                                external_id="onrc:J12/400/2012"))
    by_cui = await state.add_company(Company(name="Clinica Cluj", cui="90000041"))
    # A record holding a firm's number and another firm's CUI.
    mixed = await state.add_company(Company(name="Amestec Exemplu", cui="90000058",
                                            external_id="onrc:J08/600/2015"))
    # A record known by number, merged into a firm that holds another CUI.
    part = await state.add_company(Company(name="Miscare Exemplu",
                                           external_id="onrc:J08/910/2021"))
    whole = await state.add_company(Company(name="Recuperare Exemplu", cui="90000104"))
    await state.merge_companies(whole, part)
    before = (records(state), identity(state), positions(state))
    regs = ["J12/400/2012", "J08/600/2015", "J08/910/2021"]
    for limit in (0, None):
        got = await state.ensure_registry_records(regs=regs, limit=limit)
        assert got == counted(3, existing=3, cui_conflicts=3)
        assert (records(state), identity(state), positions(state)) == before
    assert records(state)[int(by_number)] == {("ro.onrc_reg", "J12/400/2012")}
    assert records(state)[int(by_cui)] == {("ro.cui", 90000041)}
    assert records(state)[int(mixed)] == {("ro.cui", 90000058),
                                          ("ro.onrc_reg", "J08/600/2015")}
    # Once the two records are one firm, there is nothing to count.
    await state.merge_companies(by_cui, by_number)
    assert await state.ensure_registry_records(regs=["J12/400/2012"]) == counted(1, existing=1)


# ── A CUI on several registrations ──

@pytest.mark.asyncio
async def test_a_cui_on_two_registrations_is_one_record_of_its_canonical_row(state, con):
    # Both rows match by name; the firm is its canonical one.
    q = registry.resolve(con, "doua", all_statuses=True)
    assert len(registry.matching_ids(con, q)) == 2
    got = await state.ensure_registry_records(query=q)
    assert got == counted(1, created=1)
    assert held(state) == [DOUA]
    # Chosen by its old number, it is the same firm.
    got = await state.ensure_registry_records(regs=["J08/995/2005", "J08/996/2020"])
    assert got == counted(1, existing=1)


@pytest.mark.asyncio
async def test_the_old_number_alone_makes_the_canonical_record(state, db_path):
    got = await state.ensure_registry_records(regs=["J08/995/2005"])
    assert got == counted(1, created=1)
    assert held(state) == [DOUA]


@pytest.mark.asyncio
async def test_a_matched_registration_stands_for_its_canonical_row(state, con):
    # Two operating registrations in two counties: a search in Brașov finds
    # the one there, which isn't the canonical row.
    q = registry.resolve(con, "gemene BV")
    there = registry.matching_ids(con, q)
    assert [r[0] for r in con.execute("SELECT reg FROM firms WHERE id = ?", there)] == [
        "J08/997/2012"]
    assert matching(con, q) == {90000188}
    assert await state.ensure_registry_records(query=q, limit=0) == counted(1, left=1)
    assert await state.ensure_registry_records(query=q) == counted(1, created=1)
    assert held(state) == [GEMENE]
    # The same firm by either number, by both, and by a search that finds
    # both rows or the other one.
    for selection in ({"regs": ["J08/997/2012"]}, {"regs": ["J12/998/2021"]},
                      {"regs": ["J12/998/2021", "J08/997/2012"]},
                      {"query": registry.resolve(con, "gemene")},
                      {"query": registry.resolve(con, "gemene CJ")}):
        assert await state.ensure_registry_records(**selection) == counted(1, existing=1)
    assert firms(state) == 1
    # Struck off in Brașov, operating elsewhere: the same when only the old
    # row matches.
    old = registry.Query(match='tags : "c8623r2" NOT tags : "activ"', all_statuses=True)
    assert "J08/995/2005" in [r[0] for r in con.execute(
        "SELECT f.reg FROM firm_fts t JOIN firms f ON f.id = t.rowid "
        "WHERE t.firm_fts MATCH ?", (old.match,))]
    got = await state.ensure_registry_records(query=old)
    assert got["selected"] == got["created"] == len(matching(con, old))
    assert DOUA in held(state)


@pytest.mark.asyncio
async def test_a_selection_of_nearly_the_whole_registry(state, con):
    every = con.execute("SELECT COUNT(*) FROM firm_key").fetchone()[0]
    regs = [r[0] for r in con.execute("SELECT reg FROM firms ORDER BY reg")]
    assert len(regs) > every > 10
    # Every operating firm: a broad match is taken, it is only read.
    q = registry.resolve(con, "-nimic")
    operating = matching(con, q)
    assert 10 < len(operating) < every
    assert await state.ensure_registry_records(query=q, limit=0) == counted(
        len(operating), left=len(operating))
    assert firms(state) == 0
    assert await state.ensure_registry_records(query=q, limit=4) == counted(
        len(operating), created=4, left=len(operating) - 4)
    # Every registration number there is: each firm once.
    got = await state.ensure_registry_records(regs=regs)
    assert got == counted(every, existing=4, created=every - 4)
    made = records(state)
    assert len(made) == firms(state) == every
    assert {dict(keys).get("ro.cui", dict(keys)["ro.onrc_reg"])
            for keys in made.values()} == firms_of(
        con, [r[0] for r in con.execute("SELECT id FROM firms")])
    assert recount_differences(state) == {}
    assert await state.ensure_registry_records(query=q) == counted(
        len(operating), existing=len(operating))
    assert await state.ensure_registry_records(regs=regs) == counted(every, existing=every)


# ── The cap ──

@pytest.mark.asyncio
async def test_the_cap_counts_only_firms_with_no_record(state, con):
    q = registry.resolve(con, "8623 BV")
    first = await state.ensure_registry_records(query=q, limit=2)
    assert first == counted(4, created=2, left=2)
    # The first two in the registry's name order.
    assert same(held(state), [DINTE, DOUA])
    second = await state.ensure_registry_records(query=q, limit=2)
    assert second == counted(4, existing=2, created=2)
    assert same(held(state), [DINTE, DOUA, SOLE_TRADER, ZAMBET])
    third = await state.ensure_registry_records(query=q, limit=2)
    assert third == counted(4, existing=4)


@pytest.mark.asyncio
async def test_a_cap_of_zero_only_counts(state, con):
    await state.ensure_registry_records(regs=["J08/100/2010"])
    before = (identity(state), sequence(state))
    got = await state.ensure_registry_records(query=registry.resolve(con, "dentist BV"), limit=0)
    assert got == counted(5, existing=1, left=4)
    assert firms(state) == 1 and (identity(state), sequence(state)) == before


# ── The write lock ──

@pytest.mark.asyncio
async def test_only_the_write_waits_for_the_write_lock(state, con):
    q = registry.resolve(con, "dentist BV")
    await state.ensure_registry_records(regs=["J08/100/2010"])
    other = sqlite3.connect(state.db_path, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO settings (key, value) VALUES ('test.held', '1')")
        # Counting takes no write lock: it doesn't wait for the one held.
        started = time.monotonic()
        got = await state.ensure_registry_records(query=q, limit=0)
        assert got == counted(5, existing=1, left=4)
        # Nor does a selection with nothing to write.
        assert await state.ensure_registry_records(regs=["J08/100/2010"]) == counted(
            1, existing=1)
        assert time.monotonic() - started < 5
        # Creating does: it is done once the other writer commits.
        task = asyncio.create_task(state.ensure_registry_records(query=q))
        await asyncio.sleep(0.5)
        assert not task.done() and firms(state) == 1
        other.execute("COMMIT")
        assert await task == counted(5, existing=1, created=4)
    finally:
        if other.in_transaction:
            other.execute("ROLLBACK")
        other.close()
    assert same(held(state), DENTISTS)


@pytest.mark.asyncio
async def test_a_record_another_writer_made_meanwhile_counts_as_existing(state, db_path,
                                                                         monkeypatch):
    read = onrc_queries.Adapter.select_firms
    meanwhile = []

    def racing(self, db, schemes, **selection):
        read(self, db, schemes, **selection)
        # The selection is read; another writer saves one of its firms.
        for company in meanwhile:
            store.call(state.db_path, "add_company", company)
        meanwhile.clear()

    monkeypatch.setattr(onrc_queries.Adapter, "select_firms", racing)
    # By its CUI.
    meanwhile.append(Company(name="Clinica Zâmbet", cui="90000017"))
    got = await state.ensure_registry_records(regs=["J08/100/2010", "F08/800/2018"])
    assert got == counted(2, existing=1, created=1)
    assert same(held(state), [{("ro.cui", 90000017)}, SOLE_TRADER])
    # By its registration number, and by another registration of its CUI.
    meanwhile += [Company(name="Dinte Test", external_id="onrc:J2025000200088"),
                  Company(name="Doua Exemplu", external_id="onrc:J08/995/2005")]
    got = await state.ensure_registry_records(
        regs=["J2025000200088", "J08/996/2020", "J08/990/2019"])
    assert got == counted(3, existing=2, created=1)
    assert same(held(state), [{("ro.cui", 90000017)}, SOLE_TRADER, NAME_ONLY,
                              {("ro.onrc_reg", "J2025000200088")},
                              {("ro.onrc_reg", "J08/995/2005")}])
    assert firms(state) == 5 and recount_differences(state) == {}
    # The next call sees them as it sees any record known by number only.
    got = await state.ensure_registry_records(regs=["J2025000200088", "J08/996/2020"])
    assert got == counted(2, existing=2, cui_added=2)


# ── Ids ──

@pytest.mark.asyncio
async def test_ids_on_a_new_database_and_after_a_removed_firm(state, con):
    assert slim.q(state, "SELECT seq FROM sqlite_sequence WHERE name = 'firm'") == []
    await state.ensure_registry_records(regs=["J08/100/2010", "F08/800/2018"])
    assert sorted(records(state)) == [1, 2]
    assert sequence(state) == 2
    # The next firm any other write adds goes on from there; a removed
    # firm's id is never given again.
    added = await state.add_company(Company(name="Alt Exemplu SRL"))
    assert added == "3"
    assert (await state.remove_company(added))["removed"] == "3"
    await state.ensure_registry_records(query=registry.resolve(con, "8623/3"))
    assert sorted(records(state)) == [1, 2, 4]
    assert sequence(state) == 4
    assert await state.add_company(Company(name="Inca Un Exemplu SRL")) == "5"


@pytest.mark.asyncio
async def test_ids_when_the_sequence_and_the_highest_id_disagree(state, db_path):
    def set_sequence(statement):
        db = sqlite3.connect(state.db_path)
        db.execute(statement)
        db.commit()
        db.close()

    await state.ensure_registry_records(regs=["J08/100/2010", "F08/800/2018"])
    # The sequence behind the highest id on file: ids go on after the id.
    set_sequence("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'firm'")
    await state.ensure_registry_records(regs=["J08/990/2019"])
    assert sorted(records(state)) == [1, 2, 3] and sequence(state) == 3
    # No row for it at all.
    set_sequence("DELETE FROM sqlite_sequence WHERE name = 'firm'")
    await state.ensure_registry_records(regs=["J2025000200088"])
    assert sorted(records(state)) == [1, 2, 3, 4] and sequence(state) == 4
    # The sequence ahead of it (firms removed since): ids go on after the
    # sequence, in the registry's name order.
    set_sequence("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'firm'")
    await state.ensure_registry_records(regs=["J08/996/2020", "J12/400/2012"])
    assert sorted(records(state)) == [1, 2, 3, 4, 41, 42] and sequence(state) == 42
    assert records(state)[42] == DOUA
    assert await state.add_company(Company(name="Alt Exemplu SRL")) == "43"
    assert recount_differences(state) == {}


# ── Several calls on one connection ──

def test_calls_on_one_connection_across_a_commit_and_a_failed_write(state, con, monkeypatch):
    db, attached = store.connect(state.db_path)
    try:
        s = store.Store(db, registers=attached)

        def waiting() -> list[int]:
            return [db.execute(f"SELECT COUNT(*) FROM temp.{table}").fetchone()[0]
                    for table in overlay_queries.TEMP_TABLES]

        first = s.ensure_registry_records(regs=["J08/100/2010", "F08/800/2018"])
        # Committed by the call itself: another connection reads it.
        assert first == counted(2, created=2) and not db.in_transaction
        assert same(held(state), [ZAMBET, SOLE_TRADER]) and waiting() == [0, 0, 0]
        before = (records(state), identity(state), tally(state), sequence(state))

        # A write that fails once firms were given their ids: all of it is
        # taken back, the sequence too.
        good = named.REGISTRY["overlay.records.ident.reg"]
        monkeypatch.setitem(named.REGISTRY, good.name, replace(
            good, sql="INSERT INTO firm_ident (scheme, key, firm, since) "
                      "SELECT ?1, NULL, ?2 + n.n, ?3 FROM temp.recording n"))
        q = registry.resolve(con, "dentist BV")
        with pytest.raises(sqlite3.IntegrityError):
            s.ensure_registry_records(query=q)
        assert not db.in_transaction and waiting() == [0, 0, 0]
        assert (records(state), identity(state), tally(state), sequence(state)) == before
        # A refused selection and a broken full-text expression leave it as clean.
        with pytest.raises(ValueError, match="nothing to select"):
            s.ensure_registry_records(query=registry.resolve(con, ""))
        with pytest.raises(sqlite3.OperationalError, match="fts5"):
            s.ensure_registry_records(query=registry.Query(match='tags : ("activ"'))
        assert not db.in_transaction and waiting() == [0, 0, 0]
        assert (records(state), identity(state), tally(state), sequence(state)) == before

        # The same connection goes on: the ids the failed write had given.
        monkeypatch.setitem(named.REGISTRY, good.name, good)
        assert s.ensure_registry_records(query=q, limit=2) == counted(
            5, existing=2, created=2, left=1)
        assert sorted(records(state)) == [1, 2, 3, 4]
        assert s.ensure_registry_records(query=q) == counted(5, existing=4, created=1)
        assert s.ensure_registry_records(query=q) == counted(5, existing=5)
        assert sorted(records(state)) == [1, 2, 3, 4, 5] and sequence(state) == 5
        assert same(held(state), DENTISTS) and waiting() == [0, 0, 0]
        assert recount_differences(state) == {}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_broken_full_text_expression_raises_and_writes_nothing(state, con):
    with pytest.raises(sqlite3.OperationalError, match="fts5"):
        await state.ensure_registry_records(query=registry.Query(match='tags : ("activ"'))
    assert firms(state) == 0 and identity(state) == 0 and tally(state) == {}
    assert slim.q(state, "SELECT seq FROM sqlite_sequence WHERE name = 'firm'") == []
    assert await state.ensure_registry_records(regs=["J08/100/2010"]) == counted(1, created=1)


# ── The same as a pull leaves ──

@pytest.mark.asyncio
async def test_identifiers_are_the_ones_a_pull_writes(state, con, raw, tmp_path):
    # The same firms pulled into a second database, the registry beside it.
    other = tmp_path / "pulled"
    other.mkdir()
    registry.build(raw, DATASET, other / "registry.db")
    pulled = StateManager(str(other / "test.db"))
    await pulled.init_db()
    await sync_vocabulary(pulled)
    there = registry.connect(other / "registry.db")
    try:
        report = await runs.registry_pull(pulled, there, registry.resolve(there, "dentist BV"))
    finally:
        there.close()
    assert report.new_companies == 5 and not report.errors

    await state.ensure_registry_records(query=registry.resolve(con, "dentist BV"))
    assert held(state) == held(pulled) and len(held(state)) == 5
    # The identity revision moved as much (a firm and each identifier), and
    # as many firms are counted. A pull also counts what it copied up
    # front: a declared website, the administrators as people.
    assert identity(state) == identity(pulled) == 5 + 9
    assert tally(state) == {"firms": 5}
    assert tally(pulled) == {"firms": 5, "firms_with_site": 1, "people": 2}


@pytest.mark.asyncio
async def test_new_records_take_their_places_in_the_name_and_place_orders(state, con):
    await state.ensure_registry_records(query=registry.resolve(con, "8623", all_statuses=True))
    await state.ensure_registry_records(regs=["J08/600/2015", "F08/800/2018"])
    made = positions(state)
    assert len(made) >= 8
    assert all(kind == ("real", "real") for kind in ((p[1], p[3]) for p in made.values()))
    # Two towns among them, so two places.
    assert len({p[2] for p in made.values()}) > 1
    # Every firm placed again from nothing gets the same.
    assert await state.refresh_orders() == len(made)
    assert positions(state) == made


@pytest.mark.asyncio
async def test_places_agree_with_the_ones_kept_for_the_current_load(state, con):
    # The kept places are current (a registry load's follow-up): a pulled
    # firm is placed as it is saved, new records in the same write.
    assert await state.refresh_orders() == 0
    await runs.registry_pull(state, con, registry.resolve(con, "8623/3"))
    await state.ensure_registry_records(query=registry.resolve(con, "dentist"))
    made = positions(state)
    assert len(made) == 6 and all(p[0] is not None and p[2] is not None for p in made.values())
    db = sqlite3.connect(state.db_path)
    db.execute("DELETE FROM settings WHERE key = 'overlay.orders_mark'")
    db.commit()
    db.close()
    assert await state.refresh_orders() == 6
    assert positions(state) == made


@pytest.mark.asyncio
async def test_kept_counts_and_the_identity_revision_agree_with_a_recount(state, con):
    await state.add_company(Company(name="Clinica Zâmbet", cui="90000017",
                                    domain="zambet-exemplu.example.ro"))
    start = identity(state)
    q = registry.resolve(con, "dentist")
    first = await state.ensure_registry_records(query=q, limit=3)
    second = await state.ensure_registry_records(query=q)
    more = await state.ensure_registry_records(regs=["J08/600/2015", "J08/300/2001"])
    created = first["created"] + second["created"] + more["created"]
    assert (first["created"], second["created"], more["created"]) == (3, 2, 2)
    assert recount_differences(state) == {}
    assert tally(state) == {"firms": 1 + created, "firms_with_site": 1}
    assert slim.one(state, "SELECT COUNT(*) FROM firm WHERE live = 0") == 1 + created
    # It moves once per new firm and once per identifier written (one firm
    # has no CUI).
    idents = slim.one(state, "SELECT COUNT(*) FROM firm_ident") - 1
    assert idents == 2 * created - 1
    assert identity(state) == start + created + idents


# ── Financial bounds and taxpayer conditions ──

def _financials(path, rows):
    from harvey.modules.ro.mf_financials import derived
    from harvey.modules.ro.mf_financials import store as fin

    db = sqlite3.connect(path)
    db.executescript(fin.SCHEMA)
    db.execute(fin.LATEST)
    db.executemany("INSERT INTO accounts (cui, year, form, turnover, employees) "
                   "VALUES (?, 2025, 'UU', ?, ?)", rows)
    db.execute(fin.FILL_LATEST)
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("years", "2025"),
                                                      ("loaded_at", "2026-10-02 00:00:00")])
    derived.build(db)
    db.commit()
    db.close()


def _taxpayers(path, rows):
    from harvey.modules.ro.mf_taxpayers import derived
    from harvey.modules.ro.mf_taxpayers import store as tax

    db = sqlite3.connect(path)
    db.executescript(tax.SCHEMA)
    db.executemany("INSERT INTO taxpayers (cui, name, vat, employer, micro_tax) "
                   "VALUES (?, ?, ?, ?, ?)", rows)
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("published", "2026-06-30"),
                                                      ("loaded_at", "2026-07-01 00:00:00")])
    derived.build(db)
    db.commit()
    db.close()


def _selected(state, q) -> set:
    """The firms a query selects, by CUI or registration number: the
    registry's own read of it (no write: nothing but a temporary table)."""
    db, attached = store.connect(state.db_path)
    try:
        s = store.Store(db, registers=attached)
        adapter = next(a for a in attached if hasattr(a, "select_firms"))
        db.execute(overlay_queries.TEMP_TABLES["registering"])
        adapter.select_firms(db, [s.schemes.named(name).id
                                  for name in adapter.record_schemes()], query=q)
        return {r[0] for r in db.execute("SELECT COALESCE(cui, reg) FROM temp.registering "
                                         "WHERE left_out IS NULL")}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_financial_bounds_and_taxpayer_conditions_select_as_the_search_does(
        state, con, tmp_path):
    fin_path, tax_path = tmp_path / "financials.db", tmp_path / "taxpayers.db"
    # A firm with both figures, one with a turnover and no headcount, one
    # with a headcount and no turnover; two firms file nothing.
    _financials(fin_path, [("90000017", 250000, 4), ("90000025", 1500000, None),
                           ("90000171", None, 12)])
    _taxpayers(tax_path, [("90000017", "ZAMBET EXEMPLU SRL", 1, 1, 0),
                          ("90000025", "DINTE TEST SRL", 0, 1, 1),
                          ("90000171", "DOUA EXEMPLU SRL", 1, 0, None)])
    bounds = [None] + [FinFilter(unknown=unknown, **b) for unknown in ("exclude", "include")
                       for b in ({"min_turnover": 200000}, {"max_turnover": 1000000},
                                 {"min_turnover": 200000, "max_turnover": 1000000},
                                 {"min_employees": 5}, {"max_employees": 5},
                                 {"min_turnover": 1, "min_employees": 1},
                                 {"max_turnover": 300000, "min_employees": 4,
                                  "max_employees": 4})]
    flags = [None, TaxFilter(employer=True), TaxFilter(employer=False), TaxFilter(vat=True),
             TaxFilter(micro=True), TaxFilter(micro=False),
             TaxFilter(employer=True, vat=False, micro=True)]
    seen = set()
    for fin in bounds:
        for tax in flags:
            q = registry.resolve(con, "dentist BV", fin=fin, fin_path=fin_path, tax=tax,
                                 tax_path=tax_path)
            expected = matching(con, q)
            assert _selected(state, q) == expected, (fin, tax)
            seen.add(frozenset(expected))
    # The conditions told the firms apart (not one answer for them all).
    assert len(seen) >= 6 and frozenset() in seen
    assert firms(state) == 0
    # A condition holds for a firm whichever of its registrations matched:
    # the struck-off row of a firm that files, every status asked.
    q = registry.resolve(con, "doua", all_statuses=True, fin=FinFilter(min_employees=10),
                         fin_path=fin_path, tax=TaxFilter(vat=True), tax_path=tax_path)
    assert _selected(state, q) == matching(con, q) == {90000171}
    q = registry.resolve(con, "dentist BV", fin=FinFilter(min_turnover=200000),
                         fin_path=fin_path, tax=TaxFilter(employer=True), tax_path=tax_path)
    got = await state.ensure_registry_records(query=q)
    assert got == counted(2, created=2)
    assert same(held(state), [ZAMBET, DINTE])


# ── What is refused ──

@pytest.mark.asyncio
async def test_selections_that_are_refused(state, con, db_path, monkeypatch):
    ensure = state.ensure_registry_records
    # A query whose match is empty: it names nothing to select by.
    for words in ("", "si de la"):
        q = registry.resolve(con, words)
        assert q.match == ""
        with pytest.raises(ValueError, match="nothing to select"):
            await ensure(query=q)
    # A query whose match is every firm (only exclusions or conditions, over
    # every status): there is no full-text expression to read it by.
    for q in (registry.resolve(con, "-zambet", all_statuses=True),
              registry.resolve(con, all_statuses=True, fin=FinFilter(min_turnover=1)),
              registry.resolve(con, all_statuses=True, tax=TaxFilter(vat=True))):
        assert q.match == registry.EVERY
        with pytest.raises(ValueError, match="isn't taken yet"):
            await ensure(query=q)
    # A broad one is not refused: the same exclusion over operating firms is
    # a full-text expression, and the selection is only read.
    broad = registry.resolve(con, "-zambet")
    assert broad.match not in ("", registry.EVERY)
    got = await ensure(query=broad, limit=0)
    assert got == counted(len(matching(con, broad)), left=len(matching(con, broad)))
    assert got["selected"] > 10
    # Registry ids belong to one load: chosen firms come by number.
    with pytest.raises(ValueError, match="numbers themselves"):
        await ensure(query=registry.resolve(con, regs=["J08/100/2010"]))
    with pytest.raises(ValueError, match="one of the two"):
        await ensure()
    with pytest.raises(ValueError, match="one of the two"):
        await ensure(regs=["J08/100/2010"], query=registry.resolve(con, "dentist"))
    for limit in (-1, store.RECORDS_MAX + 1):
        with pytest.raises(ValueError, match="at a time"):
            await ensure(regs=["J08/100/2010"], limit=limit)
    # A yes or no is not a number of records (True would read as 1).
    for limit in (True, False):
        with pytest.raises(ValueError, match="not a yes or no"):
            await ensure(regs=["J08/100/2010"], limit=limit)
    for regs in ([""], [8], ["J08/100/2010", None]):
        with pytest.raises(ValueError, match="registration numbers"):
            await ensure(regs=regs)
    # The bound on firms chosen one by one is the registry adapter's (the
    # jobs layer reads it from there).
    assert job_kinds.REGS_MAX == onrc_queries.REGS_MAX == 10_000
    monkeypatch.setattr(onrc_queries, "REGS_MAX", 2)
    assert await ensure(regs=["J08/100/2010", "F08/800/2018"], limit=0) == counted(2, left=2)
    with pytest.raises(ValueError, match="one by one"):
        await ensure(regs=["J08/100/2010", "F08/800/2018", "J12/400/2012"])
    # A condition whose file isn't loaded.
    with pytest.raises(FileNotFoundError, match="financial"):
        await ensure(query=registry.resolve(con, "dentist", fin=FinFilter(min_turnover=1)))
    with pytest.raises(FileNotFoundError, match="taxpayer"):
        await ensure(query=registry.resolve(con, "dentist", tax=TaxFilter(vat=True)))
    assert firms(state) == 0
    assert identity(state) == 0 and tally(state) == {}


@pytest.mark.asyncio
async def test_it_needs_a_registry_with_its_key_tables_and_no_open_transaction(
        state, db_path, tmp_path):
    # It opens its own transactions: refused inside one, a batch's too.
    db, attached = store.connect(state.db_path)
    try:
        db.execute("BEGIN")
        with pytest.raises(RuntimeError, match="own transactions"):
            store.Store(db, registers=attached).ensure_registry_records(regs=["J08/100/2010"])
        db.execute("ROLLBACK")
    finally:
        db.close()
    async with state.batch() as batch:
        with pytest.raises(RuntimeError, match="own transactions"):
            await batch.ensure_registry_records(regs=["J08/100/2010"])
    # A registry loaded before its key tables were all built.
    old = sqlite3.connect(db_path)
    old.execute("DROP TABLE place_rank")
    old.commit()
    old.close()
    with pytest.raises(Stale, match="run harvey registry load --force"):
        await state.ensure_registry_records(regs=["J08/100/2010"])
    # No registry beside the database at all.
    alone = tmp_path / "alone"
    alone.mkdir()
    sm = StateManager(str(alone / "test.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    with pytest.raises(FileNotFoundError, match="registry"):
        await sm.ensure_registry_records(regs=["J08/100/2010"])
    assert firms(state) == 0
