"""The do-not-contact list (harvey/suppression.py) and schema 26.

Schema 26 is reached in place from the schema 25 the upgrade writes, once,
in one transaction; a file a newer Harvey made is refused by name. An entry
is keyed on what it names, so an address stays barred through a merge, its
undo, the contact deleted and found again, and its firm removed and pulled
again; contact.do_not_contact mirrors the address entries, and nothing may
clear the flag while one stands. All data is fictional."""

import asyncio
import sqlite3

import pytest

from harvey import contacts, resolver, suppression
from harvey.overlay import schema
from harvey.state import NewerBuild, StateManager, UpgradeNeeded
from tests.test_overlay_writes import (  # noqa: F401  (state is a fixture)
    _clean, _con, _firm, _hit, _listing, _one, _pull, state,
)

ADDR = "ana.popescu@exemplu-unu.example.com"


def _flag(state, value=ADDR):
    return _one(state, "SELECT do_not_contact FROM contact WHERE value = ?", value)


async def _hit_for(state, email, firm=None):
    return await state.suppression_check(email, firm=firm)


# ── Schema 26, in place ──

def _v25(path, *, flagged="", opted_out=""):
    con = sqlite3.connect(path, isolation_level=None)
    schema.create(con, version=schema.UPGRADE_TARGET)
    if flagged:
        con.execute("INSERT INTO contact (kind, value, do_not_contact, first_seen, last_seen) "
                    "VALUES (?, ?, 1, 1, 1)", (schema.EMAIL, flagged))
    if opted_out:
        con.execute("INSERT INTO contact (kind, value, first_seen, last_seen) VALUES (?, ?, 1, 1)",
                    (schema.EMAIL, opted_out))
        con.execute("INSERT INTO prospects (id, email, status) VALUES ('p1', ?, 'opted_out')",
                    (opted_out,))
    con.close()


def test_a_schema_25_file_steps_to_26_in_place_once(tmp_path):
    path = tmp_path / "harvey.db"
    _v25(path, flagged="flagged@exemplu.example.com", opted_out="gone@exemplu.example.com")
    asyncio.run(StateManager(str(path)).init_db())
    con = sqlite3.connect(path)
    assert schema.version(con) == schema.LATEST and schema.missing(con) == []
    rows = con.execute("SELECT scope, value, reason, by FROM suppression ORDER BY value").fetchall()
    migrated = schema.SUPPRESSION_REASON["migrated"]
    assert rows == [(1, "flagged@exemplu.example.com", migrated, "migration"),
                    (1, "gone@exemplu.example.com", migrated, "migration")]
    # The opted-out prospect's contact is flagged now too (the mirror).
    assert con.execute("SELECT do_not_contact FROM contact WHERE value = ?",
                       ("gone@exemplu.example.com",)).fetchone() == (1,)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(schema.SUPPRESSION_GUARD_NAMES) <= names
    con.close()
    # Opened again: nothing changes.
    asyncio.run(StateManager(str(path)).init_db())
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM suppression").fetchone() == (2,)
    con.close()


def test_two_openers_step_it_once(tmp_path):
    path = tmp_path / "harvey.db"
    _v25(path, flagged="flagged@exemplu.example.com")
    a = sqlite3.connect(path, isolation_level=None)
    b = sqlite3.connect(path, isolation_level=None)
    assert schema.step_up(a) == schema.LATEST
    # The second re-reads the version inside its transaction and does nothing.
    assert schema.step_up(b) == schema.LATEST
    assert a.execute("SELECT COUNT(*) FROM suppression").fetchone() == (1,)
    a.close()
    b.close()


def test_two_openers_at_once_step_it_once(tmp_path):
    import threading

    path = tmp_path / "harvey.db"
    _v25(path, flagged="flagged@exemplu.example.com", opted_out="gone@exemplu.example.com")
    start = threading.Barrier(4)
    results, errors = [], []

    def opener():
        con = sqlite3.connect(path, isolation_level=None, timeout=30)
        try:
            start.wait()
            results.append(schema.step_up(con))
        except Exception as e:      # noqa: BLE001 (reported below)
            errors.append(e)
        finally:
            con.close()

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results == [schema.LATEST] * 4
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM suppression").fetchone() == (2,)
    assert schema.version(con) == schema.LATEST
    con.close()


def test_the_upgrade_still_writes_schema_25():
    con = sqlite3.connect(":memory:", isolation_level=None)
    schema.create(con, triggers=False, version=schema.UPGRADE_TARGET)
    assert schema.version(con) == 25
    assert "suppression" not in {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert schema.guard_names(con) == schema.GUARD_NAMES


def test_a_file_a_newer_harvey_made_is_refused_by_name(tmp_path):
    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {schema.LATEST + 1}")
    con.close()
    with pytest.raises(NewerBuild, match="made by a newer Harvey"):
        asyncio.run(StateManager(str(path)).init_db())
    from harvey import upgrade

    with pytest.raises(upgrade.Refused, match="made by a newer Harvey"):
        upgrade.run(upgrade.Layout(tmp_path), check_space=False)
    assert not upgrade.needed(tmp_path)


def test_an_older_build_refuses_a_schema_26_file_with_the_newer_harvey_message(tmp_path,
                                                                               monkeypatch):
    path = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(path)).init_db())           # schema 26
    # The release before this one: it reads up to 25.
    monkeypatch.setattr(schema, "LATEST", 25)
    monkeypatch.setattr(schema, "VERSION", 25)
    monkeypatch.setattr(schema, "STEPS", ())
    with pytest.raises(UpgradeNeeded) as e:
        asyncio.run(StateManager(str(path)).init_db())
    assert isinstance(e.value, NewerBuild)
    assert "made by a newer Harvey (schema 26; this one reads up to schema 25)" in str(e.value)
    assert "harvey upgrade" not in str(e.value)


# ── The mirror and its guard ──

@pytest.mark.asyncio
async def test_an_entry_flags_the_contact_and_nothing_else_may_clear_it(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=1))
    await state.add_contacts(a, [contacts.Found("email", ADDR, source="website")])
    assert _flag(state) == 0
    entry = await state.suppress("address", ADDR.upper(), "by_hand", by="you")
    assert entry and _flag(state) == 1
    assert _one(state, "SELECT n FROM tally WHERE name = 'contacts_do_not_contact'") == 1
    # A stray clear is refused; the list is the only writer of the flag
    # (the store has no write of its own for it any more).
    con = _con(state)
    with pytest.raises(sqlite3.IntegrityError, match="do-not-contact list"):
        con.execute("UPDATE contact SET do_not_contact = 0 WHERE value = ?", (ADDR,))
    con.close()
    from harvey.overlay import store

    assert not hasattr(store.Store, "set_do_not_contact")
    assert _flag(state) == 1
    # A second entry for the same address is one entry; the undo clears the
    # flag only once no entry stands.
    assert await state.suppress("address", ADDR, "opt_out", by="reply") is None
    assert await state.undo_suppression(entry, by="you", note="a test")
    assert _flag(state) == 0
    assert _one(state, "SELECT n FROM tally WHERE name = 'contacts_do_not_contact'") == 0
    assert _one(state, "SELECT undone_by FROM suppression WHERE id = ?", entry) == "you"
    assert _clean(state) == {}


# ── Surviving merges, undo, re-finds, removal and re-pull ──

@pytest.mark.asyncio
async def test_a_suppressed_address_survives_merge_undo_delete_and_refind(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=1))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=2))
    found = contacts.Found("email", ADDR, source="website")
    await state.add_contacts(a, [found])
    await state.suppress("address", ADDR, "unsubscribe", by="unsubscribe")

    await state.merge_companies(b, a)
    assert _flag(state) == 1 and (await _hit_for(state, ADDR, firm=b)).reason == "unsubscribe"
    await state.undo_merge(a)
    assert _flag(state) == 1 and await _hit_for(state, ADDR, firm=a) is not None
    assert _clean(state) == {}

    # The contact deleted (its evidence first) and found again: a new row,
    # flagged on insert by the trigger.
    con = _con(state)
    cid = con.execute("SELECT id FROM contact WHERE value = ?", (ADDR,)).fetchone()[0]
    con.execute("DELETE FROM link_evidence WHERE contact = ?", (cid,))
    con.execute("DELETE FROM link WHERE contact = ?", (cid,))
    con.execute("DELETE FROM contact WHERE id = ?", (cid,))
    assert con.execute("SELECT COUNT(*) FROM contact WHERE value = ?", (ADDR,)).fetchone() == (0,)
    con.close()
    await state.add_contacts(b, [found])
    assert _one(state, "SELECT COUNT(*) FROM contact WHERE value = ?", ADDR) == 1
    assert _flag(state) == 1 and await _hit_for(state, ADDR) is not None
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_firm_entry_survives_removal_and_a_pull_again_by_its_tax_code(state):
    _, saved = await _pull(state, _firm(71, domain="exemplu-sapte.example.com"))
    a = saved[0]
    entry = await state.suppress("firm", a, "by_hand", by="you")
    assert _one(state, "SELECT value FROM suppression WHERE id = ?", entry) == "ro.cui:90000071"
    # Their owner objected too, before Harvey had found the address.
    await state.suppress("address", ADDR, "objection", by="reply")
    done = await state.remove_company(a)
    assert done["removed"] == a
    _, again = await _pull(state, _firm(71))
    assert again[0] != a
    con = _con(state)
    try:
        assert suppression.Index(con).firm_hit(again[0]).value == "ro.cui:90000071"
    finally:
        con.close()
    # Found on the firm pulled again: the address comes back flagged, and
    # any other address of the firm is barred through it.
    other = "office@exemplu-sapte.example.com"
    await state.add_contacts(again[0], [contacts.Found("email", ADDR, source="website"),
                                        contacts.Found("email", other, source="website")])
    assert _flag(state) == 1 and _flag(state, other) == 0
    assert (await _hit_for(state, ADDR)).scope == "address"
    assert (await _hit_for(state, other)).scope == "firm"
    assert _clean(state) == {}


@pytest.mark.asyncio
async def test_a_firm_with_no_tax_code_is_matched_through_its_live_record(state):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=1))
    b, _ = await _listing(state, _hit(name="Doi Exemplu", n=2))
    entry = await state.suppress("firm", a, "by_hand", by="you")
    assert _one(state, "SELECT value FROM suppression WHERE id = ?", entry) == f"firm:{a}"
    await state.add_contacts(b, [contacts.Found("email", "office@doi.example.com",
                                                source="website")])
    assert await _hit_for(state, "office@doi.example.com") is None
    await state.merge_companies(a, b)       # b's records are a's now
    assert (await _hit_for(state, "office@doi.example.com")).scope == "firm"
    await state.undo_merge(b)
    assert await _hit_for(state, "office@doi.example.com") is None


@pytest.mark.asyncio
async def test_a_firm_with_no_tax_code_is_barred_by_its_domain_after_removal(state):
    a, _ = await _listing(state, _hit(name="Trei Exemplu", n=3, domain="trei.example.com"))
    entry = await state.suppress("firm", a, "by_hand", by="you")
    assert _one(state, "SELECT COUNT(*) FROM suppression WHERE scope = ? AND value = ? "
                "AND undone_at IS NULL", suppression.DOMAIN, "trei.example.com") == 1
    await state.remove_company(a)
    await _listing(state, _hit(name="Trei Exemplu", n=33, domain="trei.example.com"))
    hit = await _hit_for(state, "office@trei.example.com")
    assert (hit.scope, hit.value) == ("domain", "trei.example.com")
    # Undoing the firm entry undoes its domain with it.
    assert await state.undo_suppression(entry, by="you", note="a test")
    assert await _hit_for(state, "office@trei.example.com") is None


@pytest.mark.asyncio
async def test_a_firm_with_no_domain_of_its_own_gets_no_domain_entry(state):
    a, _ = await _listing(state, _hit(name="Patru Exemplu", n=4))
    await state.suppress("firm", a, "by_hand", by="you")
    assert _one(state, "SELECT COUNT(*) FROM suppression WHERE scope = ?", suppression.DOMAIN) == 0


@pytest.mark.asyncio
async def test_domains_cover_subdomains_free_mail_is_refused_and_the_config_list_counts(state):
    await state.suppress("domain", "https://www.exemplu.example.com/contact", "by_hand", by="you")
    assert (await _hit_for(state, "ana@mail.exemplu.example.com")).value == "exemplu.example.com"
    assert await _hit_for(state, "ana@notexemplu.example.com") is None
    with pytest.raises(suppression.Refused, match="free-mail"):
        await state.suppress("domain", "gmail.com", "by_hand", by="you")
    with pytest.raises(suppression.Refused):
        await state.suppress("address", "not an address", "by_hand", by="you")
    # A plus-tagged variant of an address entry is barred too.
    await state.suppress("address", "ion@exemplu-doi.example.com", "by_hand", by="you")
    assert await _hit_for(state, "ion+news@exemplu-doi.example.com") is not None

    class Cfg:
        from harvey.config import ComplianceConfig
        compliance = ComplianceConfig(do_not_contact=["concurent.example.com"])

    hit = await state.suppression_check("x@concurent.example.com", config=Cfg())
    assert hit.reason == "config" and "do-not-contact list" in hit.text
    # An undone entry no longer counts.
    entry = (await state.suppression_entries())[0]["id"]
    await state.undo_suppression(entry, by="you")
    assert entry not in {e["id"] for e in await state.suppression_entries()}


# ── Opting out, and its undo, in one write ──

@pytest.mark.asyncio
async def test_an_opt_out_and_its_undo_go_through_the_list_in_one_write(state):
    from harvey.models.prospect import Prospect

    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=1))
    pid = await state.add_prospect(Prospect(company_id=a, first_name="Ana", last_name="Popescu",
                                            email=ADDR, email_status="verified"))
    await state.update_prospect_status(pid, "opted_out", reason="opt_out", note="nu mai doresc")
    assert _flag(state) == 1
    assert _one(state, "SELECT reason FROM suppression WHERE value = ? AND undone_at IS NULL",
                ADDR) == schema.SUPPRESSION_REASON["opt_out"]
    # An entry you made by hand isn't lifted by undoing their opt-out.
    await state.suppress("domain", "exemplu-unu.example.com", "by_hand", by="you")
    assert await state.undo_opt_out(pid, details={"reason": "a test"})
    assert (await state.get_prospect(pid)).status == "replied"
    assert _flag(state) == 0
    assert (await _hit_for(state, ADDR)).scope == "domain"
    # Undone with the opt-out's own write: not opted out any more, nothing written.
    assert await state.undo_opt_out(pid) is False


@pytest.mark.asyncio
async def test_the_audience_never_offers_a_suppressed_address(state, monkeypatch):
    a, _ = await _listing(state, _hit(name="Unu Exemplu", n=1, domain="unu.example.com"))
    for value in ("office@unu.example.com", "vanzari@unu.example.com"):
        await state.add_contacts(a, [contacts.Found("email", value, source="website",
                                                    status="verified", trust="confirmed")])
    before = {r["email"] for r in await state.email_audience(50)}
    assert before == {"office@unu.example.com", "vanzari@unu.example.com"}
    await state.suppress("address", "office@unu.example.com", "by_hand", by="you")
    assert {r["email"] for r in await state.email_audience(50)} == {"vanzari@unu.example.com"}
    await state.suppress("firm", a, "by_hand", by="you")
    assert await state.email_audience(50) == []


# ── Free-mail domains never key a firm ──

@pytest.mark.parametrize("domain", ["rdslink.ro", "clicknet.ro", "email.ro", "yahoo.ro",
                                    "gmail.com"])
def test_romanian_webmail_never_identifies_a_firm(domain):
    resolver.usable_domain.cache_clear()
    assert not resolver.usable_domain(domain)
    assert resolver.usable_domain("exemplu-unu.example.com")


# ── A future copy migration must carry the list ──

def test_verify_compares_the_list_when_the_source_has_one(tmp_path):
    from harvey.overlay import verify

    old, new = tmp_path / "old.db", tmp_path / "new.db"
    for path in (old, new):
        con = sqlite3.connect(path, isolation_level=None)
        schema.create(con)
        con.close()
    con = sqlite3.connect(old)
    con.execute("INSERT INTO suppression (scope, value, reason, at, by) "
                "VALUES (1, 'ana@exemplu.example.com', 2, 1, 'reply')")
    con.commit()
    con.close()
    con = sqlite3.connect(new)
    con.execute("ATTACH DATABASE ? AS old", (str(old),))
    rows = verify.suppression_counts(con)
    assert rows["do-not-contact entries"] == {"old": 1, "new": 0, "rule": "equal",
                                              "passed": False}
    assert not rows["do-not-contact entries (digest)"]["passed"]
    assert any("do-not-contact" in f for f in verify.failures({"counts": rows}))
    con.execute("INSERT INTO main.suppression SELECT * FROM old.suppression")
    assert all(r["passed"] for r in verify.suppression_counts(con).values())
    con.close()
    # A schema 24 source has no list: nothing to compare.
    bare = sqlite3.connect(":memory:")
    bare.execute("ATTACH DATABASE ':memory:' AS old")
    assert verify.suppression_counts(bare) == {}
