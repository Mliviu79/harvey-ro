"""Each campaign's candidates and the contact change log (schema 38): the
start decides every match once and keeps why each was left out; a roll
decides only the contacts that changed since its cursor and its rows that
came due (the other campaign ended, the cold email turned 90 days old, a
do-not-contact entry was undone), never the whole filter but once a week;
and send_to_risky is the campaign's own, chosen while it is a draft and
never past the setting. All data is fictional."""

import re
import sqlite3
from datetime import timedelta

import pytest

from harvey import campaigns
from harvey.overlay import schema, triggers
from harvey.overlay.schema import CANDIDATE_STATE, CANDIDATE_WHY
from harvey.state import StateManager
from tests.test_campaigns import (  # noqa: F401  (the state fixture)
    _now, config, contact, new_campaign, state,
)


def _rows(state, cid) -> dict:
    con = sqlite3.connect(state.db_path)
    try:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT email, state, why, recheck_at FROM campaign_candidate WHERE campaign = ?",
            (cid,))}
    finally:
        con.close()


def _due_now(c: dict) -> dict:
    """The campaign as a roll an hour later sees it."""
    return {**c, "rolled_at": c["rolled_at"] - campaigns.ROLL_EVERY_SECONDS - 1}


async def _fresh(state, cid) -> dict:
    return await state.campaign_record(cid)


# ── Schema 38 ──

def test_schema_38_on_a_new_file_and_stepped_from_37(tmp_path):
    new = sqlite3.connect(":memory:")
    schema.create(new)
    old = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    schema.create(old, version=37)
    old.execute("INSERT INTO campaigns (id, name, status, rolling) VALUES ('c', 'n', 'active', 1)")
    assert schema.step_up(old) == schema.LATEST == 39
    for con in (new, old):
        assert schema.missing(con) == [] and triggers.missing(con) == []
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert set(triggers.CHANGE_NAMES) <= have
    # A campaign on file: no send_to_risky yet (the cycle fills it from the
    # setting), and its whole filter read at its next roll.
    assert old.execute("SELECT send_to_risky, candidate_seq, checked_all_at FROM campaigns") \
        .fetchone() == (None, 0, None)
    old.execute("BEGIN")
    assert campaigns.fill_send_to_risky(old, True) == 1
    old.execute("COMMIT")
    assert old.execute("SELECT send_to_risky FROM campaigns").fetchone() == (1,)


def test_the_change_triggers_survive_a_bulk_write_and_log_a_merge(tmp_path):
    con = sqlite3.connect(tmp_path / "h.db", isolation_level=None)
    schema.create(con)
    con.execute("INSERT INTO firm (id, created) VALUES (1, 1), (2, 1)")
    con.execute("INSERT INTO contact (id, kind, value, status, first_seen, last_seen) "
                "VALUES (10, ?, 'office@alfa.example.com', 1, 1, 1), "
                "(11, ?, '+40268000101', 0, 1, 1)", (schema.EMAIL, schema.PHONE))
    con.execute("INSERT INTO link (firm, contact, trust, first_seen, last_seen) "
                "VALUES (2, 10, 0, 1, 1), (2, 11, 0, 1, 1)")
    logged = lambda: [r[0] for r in con.execute(  # noqa: E731
        "SELECT contact FROM contact_change ORDER BY seq")]
    # An email contact and its link logged; a phone not.
    assert logged() == [10, 10]
    with triggers.bulk(con):
        con.execute("UPDATE firm SET live = 1 WHERE id = 2")
    assert logged()[-1] == 10 and 11 not in logged()
    assert set(triggers.CHANGE_NAMES) <= {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}


# ── The start and the roll ──

@pytest.mark.asyncio
async def test_the_start_keeps_every_address_it_decided_with_why(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@beta.example.com", status="invalid")
    await contact(state, "office@gama.example.com", trust="unconfirmed")
    await contact(state, "office@delta.example.com", status="risky")
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert counts["added"] == 1
    rows = _rows(state, cid)
    ex = CANDIDATE_STATE["excluded"]
    assert rows == {"office@alfa.example.com": (CANDIDATE_STATE["member"], 0, None),
                    "office@beta.example.com": (ex, CANDIDATE_WHY["left_out"], None),
                    "office@gama.example.com": (ex, CANDIDATE_WHY["untrusted"], None),
                    "office@delta.example.com": (ex, CANDIDATE_WHY["risky_held"], None)}
    assert await state.campaigns_call(campaigns.candidate_counts, cid, write=False) == {
        "members": 1, "left_out": 1, "untrusted": 1, "risky_held": 1}
    c = await _fresh(state, cid)
    assert c["checked_all_at"] and c["candidate_seq"] > 0
    # Nothing changed: a roll decides nothing.
    counts = await campaigns.roll(state, cfg, _due_now(c))
    assert (counts["total"], counts["whole"]) == (0, False)
    # beta's address checked again and found good: only it is decided.
    await contact(state, "office@beta.example.com", status="verified",
                  firm=(await state.read("audience_rows", None, ["office@beta.example.com"]))[0][
                      "company_id"])
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert (counts["total"], counts["added"]) == (1, 1)
    assert _rows(state, cid)["office@beta.example.com"][:2] == (CANDIDATE_STATE["member"], 0)


@pytest.mark.asyncio
async def test_an_address_held_elsewhere_is_decided_again_when_that_campaign_ends(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    first, _ = await new_campaign(state, cfg, name="Prima")
    second, counts = await new_campaign(state, cfg, name="A doua", rolling=True)
    assert counts["overlap"] == 1
    assert _rows(state, second)["office@alfa.example.com"] == (
        CANDIDATE_STATE["excluded"], CANDIDATE_WHY["overlap"], None)
    # First come: while the first campaign holds it, a roll leaves it.
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, second)))
    assert counts["total"] == 0
    await state.campaigns_call(campaigns.set_status, first, "stop")
    assert _rows(state, second)["office@alfa.example.com"][2] is not None
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, second)))
    assert (counts["total"], counts["added"]) == (1, 1)
    assert _rows(state, second)["office@alfa.example.com"] == (CANDIDATE_STATE["member"], 0,
                                                                 None)


@pytest.mark.asyncio
async def test_cold_email_sent_lately_is_decided_again_when_it_turns_90_days_old(state):
    from harvey.models.prospect import Prospect

    cfg = config()
    await contact(state, "office@kappa.example.com")
    pid = await state.add_prospect(Prospect(email="office@kappa.example.com"))
    sent = await state.add_outbox_item(prospect_id=pid, to_email="office@kappa.example.com",
                                       subject="s", body="b", send_at=_now().isoformat(),
                                       campaign_id="old", step=1)
    sent_at = _now() - timedelta(days=30)
    await state.update_outbox_item(sent, status="sent", sent_at=sent_at.isoformat())
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert counts["overlap"] == 1
    _, why, recheck = _rows(state, cid)["office@kappa.example.com"]
    lapses = int((sent_at + timedelta(days=campaigns.OVERLAP_DAYS)).timestamp())
    assert why == CANDIDATE_WHY["overlap"] and abs(recheck - lapses) <= 2
    c = await _fresh(state, cid)
    assert (await campaigns.roll(state, cfg, _due_now(c)))["total"] == 0
    # 60 days on (the send, moved back, is out of the window): due, and
    # decided again without the whole filter being read.
    con = sqlite3.connect(state.db_path)
    con.execute("UPDATE outbox SET sent_at = ? WHERE id = ?",
                ((_now() - timedelta(days=91)).isoformat(), sent))
    con.commit()
    con.close()
    c = {**_due_now(await _fresh(state, cid)), "checked_all_at": recheck}
    counts = await campaigns.roll(state, cfg, c, now=recheck + 1)
    assert (counts["total"], counts["added"], counts["whole"]) == (1, 1, False)


@pytest.mark.asyncio
async def test_a_do_not_contact_undo_sets_its_candidates_due(state):
    cfg = config()
    await contact(state, "office@zeta.example.com")
    entry = await state.suppress("domain", "zeta.example.com", "by_hand", by="you")
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert counts["suppressed"] == 1
    assert (await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid))))["total"] == 0
    await state.undo_suppression(entry, by="you", note="greșeală")
    assert _rows(state, cid)["office@zeta.example.com"][2] is not None
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert (counts["total"], counts["added"]) == (1, 1)


@pytest.mark.asyncio
async def test_a_due_row_the_filter_no_longer_matches_waits_for_nothing(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    await contact(state, "office@iota.example.com")
    first, _ = await new_campaign(state, cfg, query="kind=email&q=iota", name="Prima")
    second, _ = await new_campaign(state, cfg, name="A doua", rolling=True)
    con = sqlite3.connect(state.db_path)
    # (Its frozen filter changed under it: iota no longer matches.)
    con.execute("UPDATE campaigns SET audience_query = 'kind=email&q=alfa' WHERE id = ?",
                (second,))
    con.commit()
    con.close()
    await state.campaigns_call(campaigns.set_status, first, "stop")
    # (A second on: a row set due in the roll's own second is kept due.)
    c = _due_now(await _fresh(state, second))
    counts = await campaigns.roll(state, cfg, c, now=c["rolled_at"] + campaigns.ROLL_EVERY_SECONDS
                                  + 3)
    assert counts["total"] == 0
    assert _rows(state, second)["office@iota.example.com"][2] is None


@pytest.mark.asyncio
async def test_the_whole_filter_is_read_weekly_and_when_the_log_was_pruned(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    cid, _ = await new_campaign(state, cfg, rolling=True)
    c = await _fresh(state, cid)
    assert (await campaigns.roll(state, cfg, _due_now(c)))["whole"] is False
    c = await _fresh(state, cid)
    c["checked_all_at"] -= campaigns.FULL_CHECK_SECONDS
    counts = await campaigns.roll(state, cfg, _due_now(c))
    assert counts["whole"] is True and counts["already"] == 1
    assert (await _fresh(state, cid))["checked_all_at"] > c["checked_all_at"]
    # Paused, the log is pruned past its cursor; resumed, it reads it whole.
    await state.campaigns_call(campaigns.set_status, cid, "pause")
    await contact(state, "office@beta.example.com")
    assert await state.campaigns_call(campaigns.prune_changes) > 0
    await state.campaigns_call(campaigns.set_status, cid, "resume")
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert counts["whole"] is True and counts["added"] == 1


@pytest.mark.asyncio
async def test_the_log_is_pruned_below_the_slowest_rolling_campaign(state):
    cfg = config()
    await contact(state, "office@alfa.example.com")
    cid, _ = await new_campaign(state, cfg, rolling=True)
    cursor = (await _fresh(state, cid))["candidate_seq"]
    await contact(state, "office@beta.example.com")
    # What the campaign read before its start goes; beta's change, not yet
    # read, is kept.
    await state.campaigns_call(campaigns.prune_changes)
    _, low = await state.campaigns_call(campaigns.change_marks, write=False)
    assert low == cursor + 1
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert counts["added"] == 1
    assert await state.campaigns_call(campaigns.prune_changes) > 0
    top, low = await state.campaigns_call(campaigns.change_marks, write=False)
    assert low is None and top == (await _fresh(state, cid))["candidate_seq"]


# ── A roll reads only what changed ──

@pytest.mark.asyncio
async def test_a_roll_with_one_changed_contact_reads_that_contact_only(state, monkeypatch):
    """Sixty addresses left out; one becomes good. The roll's statements
    read that contact by its id: no statement reads the contacts, links,
    outbox or candidates beyond a few rows (by the production-sized
    statistics tests/test_query_plans.py plans with), and the contact
    rows read are that one."""
    from tests import test_query_plans as plans

    cfg = config()
    firms = {}
    for i in range(60):
        email = f"office@firma{i:02d}.example.com"
        firms[email] = await contact(state, email, status="invalid")
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert (counts["left_out"], counts["added"]) == (60, 0)
    changed = "office@firma07.example.com"
    await contact(state, changed, status="verified", firm=firms[changed])
    con = sqlite3.connect(state.db_path)
    plans._inject(con, "main", {**plans.OVERLAY_STATS, **plans.EMAIL_STATS})
    target = con.execute("SELECT id FROM contact WHERE value = ?", (changed,)).fetchone()[0]
    con.close()

    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        c = real(*args, **kwargs)
        c.set_trace_callback(lambda sql: seen.append(sql.strip()))
        return c
    monkeypatch.setattr(sqlite3, "connect", connect)
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    monkeypatch.setattr(sqlite3, "connect", real)
    assert (counts["total"], counts["added"], counts["whole"]) == (1, 1, False)

    big = {"contact", "link", "link_evidence", "outbox", "prospects", "campaign_candidate",
           "campaign_member", "contact_change", "firm", "firm_ident", "person"}
    con = real(state.db_path)
    bad, read_ids = {}, set()
    for sql in seen:
        if not re.match(r"(?is)^(select|with|insert|update|delete)\b", sql) \
                or sql.startswith("--"):
            continue
        try:
            plan = plans.explain(con, sql)
        except sqlite3.Error:
            continue
        found = {f for f in plans.findings(sql, plan, bounded=True, con=con)
                 if f.startswith("scan:") and f[5:] in big}
        if found:
            bad[sql[:120]] = sorted(found) + plan
        if "FROM json_each(" in sql and "CROSS JOIN contact c" in sql \
                and "c.display" in sql:
            read_ids |= {int(x) for x in re.findall(r"\d+", sql.split("json_each('")[1]
                                                    .split("')")[0])}
    con.close()
    assert bad == {}, "\n".join(f"{k}: {v}" for k, v in bad.items())
    assert read_ids == {target}


# ── send_to_risky per campaign ──

@pytest.mark.asyncio
async def test_send_to_risky_is_the_campaigns_own_and_never_past_the_setting(state):
    on = config(send_to_risky=True)
    await contact(state, "office@delta.example.com", status="risky")
    took, _ = await new_campaign(state, on, name="Ia", start=False)
    await state.campaigns_call(campaigns.update_campaign, took, send_to_risky=True)
    counts = await campaigns.start(state, on, took)
    assert (await _fresh(state, took))["send_to_risky"] is True and counts["risky"] == 1
    assert counts["send_to_risky"] is True
    await state.campaigns_call(campaigns.set_status, took, "stop")
    # Chosen off while a draft: held, though the setting is on.
    held, _ = await new_campaign(state, on, name="Ține", start=False)
    await state.campaigns_call(campaigns.update_campaign, held, send_to_risky=False)
    preview = await campaigns.preview(state, on, query="kind=email", campaign_id=held)
    assert (preview["risky_held"], preview["send_to_risky"]) == (1, False)
    counts = await campaigns.start(state, on, held)
    assert (counts["risky_held"], counts["added"]) == (1, 0)
    # Once started it doesn't change; unchanged is fine.
    with pytest.raises(campaigns.Refused):
        await state.campaigns_call(campaigns.update_campaign, held, send_to_risky=True)
    await state.campaigns_call(campaigns.update_campaign, held, send_to_risky=False, name="X")
    # On in a campaign, off in the setting: held (the send gate reads the setting).
    off = config(send_to_risky=False)
    assert campaigns.risky_allowed({"send_to_risky": True}, off) is False
    assert campaigns.risky_allowed({"send_to_risky": None}, on) is True
    preview = await campaigns.preview(state, off, query="kind=email", send_to_risky=True)
    assert preview["send_to_risky"] is False


@pytest.mark.asyncio
async def test_send_to_risky_takes_only_true_or_false(state):
    cid, _ = await new_campaign(state, config(send_to_risky=True), name="Text", start=False)
    await state.campaigns_call(campaigns.update_campaign, cid, send_to_risky=False)
    with pytest.raises(campaigns.Refused):
        await state.campaigns_call(campaigns.update_campaign, cid, send_to_risky="false")
    assert (await _fresh(state, cid))["send_to_risky"] is False


def test_harvey_campaigns_new_send_to_risky(tmp_path, monkeypatch, capsys):
    import asyncio
    import json

    from harvey import cli
    from harvey import config as config_mod
    from harvey import state as state_mod
    from tests.test_campaigns import NOTE, SEQUENCE

    path = str(tmp_path / "harvey.db")
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    cfg = config(send_to_risky=True)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    asyncio.run(StateManager(path).init_db())
    sequence = tmp_path / "sequence.json"
    sequence.write_text(json.dumps(SEQUENCE), encoding="utf-8")

    def made(*flags):
        monkeypatch.setattr("sys.argv", ["harvey", "campaigns", "new", "--name", "A", "--query",
                                         "kind=email", "--note", NOTE, "--sequence-file",
                                         str(sequence), *flags])
        cli.main()
        cid = capsys.readouterr().out.split("Draft campaign ")[1].split()[0]
        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT send_to_risky FROM campaigns WHERE id = ?",
                               (cid,)).fetchone()[0]
        finally:
            con.close()
    assert (made(), made("--no-send-to-risky"), made("--send-to-risky")) == (1, 0, 1)


# ── Every change a rule reads, and the incremental roll against a whole one ──

@pytest.mark.asyncio
async def test_a_link_given_a_person_is_read_again(state):
    """An address is read through one link, through a person first: when
    a surer link gains a person, that link is the one read (another trust,
    another firm), so the address is decided again."""
    from harvey import contacts as contacts_mod
    from harvey.models import Company

    cfg = config()
    alfa = await contact(state, "office@delta.example.com", trust="likely")
    beta = await state.add_company(Company(name="Beta", domain="beta.example.com"))
    await state.add_contacts(beta, [contacts_mod.Found(
        "email", "office@delta.example.com", status="verified", source="website",
        trust="unconfirmed", person="Ion Exemplu", evidence_url="https://beta.example.com/")])
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert counts["untrusted"] == 1
    # The same address on alfa's page, now with a name next to it.
    await state.add_contacts(alfa, [contacts_mod.Found(
        "email", "office@delta.example.com", status="verified", source="website",
        trust="likely", person="Ana Exemplu", evidence_url="https://delta.example.com/echipa")])
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert (counts["total"], counts["added"]) == (1, 1)


@pytest.mark.asyncio
async def test_a_separation_reads_the_survivors_addresses_again(state):
    """Someone answered at a firm merged into alfa: alfa's address is left
    out. The merge undone, alfa no longer carries that answer, and its
    address is decided again (the survivor's links are logged, not only
    the record that came back)."""
    from harvey.models.prospect import Prospect

    cfg = config()
    alfa = await contact(state, "office@alfa.example.com")
    beta = await contact(state, "office@beta.example.com")
    pid = await state.add_prospect(Prospect(email="office@beta.example.com",
                                            company_id=str(beta)))
    await state.stop_company(pid)
    await state.merge_companies(alfa, beta)
    cid, counts = await new_campaign(state, cfg, rolling=True)
    assert _rows(state, cid)["office@alfa.example.com"][:2] == (
        CANDIDATE_STATE["excluded"], CANDIDATE_WHY["answered"])
    await state.undo_merge(beta)
    await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    assert _rows(state, cid)["office@alfa.example.com"][:2] == (CANDIDATE_STATE["member"], 0)


@pytest.mark.asyncio
async def test_a_hook_that_fires_while_a_roll_decides_is_kept(state, monkeypatch):
    """The campaign holding an address ends between a roll's reads and
    its writes: the row the hook set due stays due (the decision written
    didn't see it), and the next roll takes the address."""
    cfg = config()
    await contact(state, "office@alfa.example.com")
    first, _ = await new_campaign(state, cfg, name="Prima")
    second, _ = await new_campaign(state, cfg, name="A doua", rolling=True)
    # Something else of the second campaign's changes, so the roll decides alfa.
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO contact_change (contact) SELECT id FROM contact WHERE value = ?",
                ("office@alfa.example.com",))
    con.commit()
    con.close()
    real = campaigns._put_candidates

    def put(con, campaign_id, decided, since):
        if campaign_id == second:
            campaigns.set_status(con, first, "stop")
        return real(con, campaign_id, decided, since)
    monkeypatch.setattr(campaigns, "_put_candidates", put)
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, second)))
    monkeypatch.setattr(campaigns, "_put_candidates", real)
    assert counts["overlap"] == 1
    assert _rows(state, second)["office@alfa.example.com"][2] is not None
    counts = await campaigns.roll(state, cfg, _due_now(await _fresh(state, second)))
    assert counts["added"] == 1


def _members(path, cid) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute(
            "SELECT email FROM campaign_member WHERE campaign = ?", (cid,))}
    finally:
        con.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_an_incremental_roll_takes_what_a_whole_one_would(state, tmp_path, seed):
    """Fictional firms and addresses at random, a campaign started on a
    random filter, then random changes of every kind the log holds (an
    address's status, a new address, the same address at another firm
    with a person, do-not-contact entries and their undo, another
    campaign ending, a merge and its undo, an answer at a firm). A copy
    of the file reads its whole filter; the file itself reads only what
    changed: both take the same addresses (each through add_members)."""
    import random

    from harvey import contacts as contacts_mod
    from harvey.models import Company
    from harvey.models.prospect import Prospect

    rng = random.Random(seed)
    cfg = config(send_to_risky=rng.random() < 0.5)
    statuses = ("verified", "risky", "unchecked", "invalid", "guess")
    trusts = ("confirmed", "likely", "unconfirmed")
    names = ("alfa", "beta", "gama", "delta", "kappa", "iota", "zeta", "sigma")
    firms, emails = [], []
    for i in range(16):
        word = names[i % len(names)]
        domain = f"{word}{i}.example.com"
        firm = await state.add_company(Company(name=f"{word.title()} {i}", domain=domain,
                                               location="Brașov"))
        firms.append(firm)
        for local in ("office", "ana")[:rng.randint(1, 2)]:
            email = f"{local}@{domain}"
            emails.append((email, firm))
            await state.add_contacts(firm, [contacts_mod.Found(
                "email", email, status=rng.choice(statuses), source="website",
                trust=rng.choice(trusts), evidence_url=f"https://{domain}/contact")])
    query = rng.choice(["kind=email", "kind=email&q=alfa", "kind=email&q=office",
                        "kind=email&status=verified", "kind=email&trust=likely",
                        "kind=email&person=yes"])
    other, _ = await new_campaign(state, cfg, name="Prima",
                                  query=rng.choice(["kind=email&q=beta", "kind=email&q=ana"]))
    cid, _ = await new_campaign(state, cfg, name="A doua", query=query, rolling=True)
    entries, merged = [], []
    for _ in range(14):
        move = rng.randrange(8)
        email, firm = rng.choice(emails)
        if move == 0:
            await state.add_contacts(firm, [contacts_mod.Found(
                "email", email, status=rng.choice(statuses), source="verifier",
                trust=rng.choice(trusts), evidence_url="https://verificare.example.com/")])
        elif move == 1:
            domain = email.rpartition("@")[2]
            new = f"vanzari{rng.randrange(100)}@{domain}"
            emails.append((new, firm))
            await state.add_contacts(firm, [contacts_mod.Found(
                "email", new, status=rng.choice(statuses), source="website",
                trust=rng.choice(trusts), evidence_url=f"https://{domain}/")])
        elif move == 2:
            await state.add_contacts(rng.choice(firms), [contacts_mod.Found(
                "email", email, status="verified", source="website",
                trust=rng.choice(trusts), person="Ana Exemplu",
                evidence_url="https://echipa.example.com/")])
        elif move == 3:
            scope, value = rng.choice([("address", email),
                                       ("domain", email.rpartition("@")[2])])
            entry = await state.suppress(scope, value, "by_hand", by="you")
            if entry:
                entries.append(entry)
        elif move == 4 and entries:
            await state.undo_suppression(entries.pop(rng.randrange(len(entries))), by="you")
        elif move == 5:
            if (await _fresh(state, other))["status"] in ("active", "paused"):
                await state.campaigns_call(campaigns.set_status, other, "stop")
        elif move == 6:
            a, b = rng.sample(firms, 2)
            if merged and rng.random() < 0.5:
                await state.undo_merge(merged.pop())
            else:
                try:
                    await state.merge_companies(a, b)
                    merged.append(b)
                except Exception:
                    pass
        else:
            pid = await state.add_prospect(Prospect(email=email, company_id=str(firm)))
            await state.stop_company(pid)

    copy = str(tmp_path / f"whole{seed}.db")
    src, dst = sqlite3.connect(state.db_path), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    whole_state = StateManager(copy)
    await whole_state.init_db()
    part = await campaigns.roll(state, cfg, _due_now(await _fresh(state, cid)))
    whole = await campaigns.roll(whole_state, cfg, _due_now(await _fresh(whole_state, cid)),
                                 whole=True)
    assert part["whole"] is False and whole["whole"] is True
    took = _members(state.db_path, cid)
    assert took == _members(copy, cid), (seed, query)


@pytest.mark.asyncio
async def test_the_filter_read_by_ids_keeps_what_the_whole_filter_keeps(state):
    """audience_rows(f, only=ids), what a roll reads, is the whole
    filter's rows among those ids, for filters of every kind a Contacts
    query carries (words, status, trust, a person or none, the source,
    a website), over random fictional subsets."""
    import random

    from harvey import contacts as contacts_mod
    from harvey.contact_query import filter_from_query
    from harvey.models import Company

    rng = random.Random(7)
    words = ("alfa", "beta", "gama", "delta")
    firms = []
    for i in range(12):
        word = words[i % len(words)]
        domain = f"{word}{i}.example.com"
        firm = await state.add_company(Company(
            name=f"{word.title()} Servicii {i}", location="Brașov",
            domain=domain if i % 3 else "", website=f"https://{domain}" if i % 3 else ""))
        firms.append(firm)
        for local in ("office", "ana", "vanzari")[:rng.randint(1, 3)]:
            await state.add_contacts(firm, [contacts_mod.Found(
                "email", f"{local}@{domain}", status=rng.choice(("verified", "risky", "invalid")),
                source=rng.choice(("website", "verifier")), trust=rng.choice(
                    ("confirmed", "likely", "unconfirmed")),
                person="Ana Exemplu" if local == "ana" and rng.random() < 0.7 else "",
                evidence_url=f"https://{domain}/contact")])
    # Some addresses at a second firm too.
    for i in range(4):
        await state.add_contacts(rng.choice(firms), [contacts_mod.Found(
            "email", f"office@{words[i]}{i}.example.com", status="verified", source="website",
            trust="likely", evidence_url="https://alt.example.com/")])
    con = sqlite3.connect(state.db_path)
    ids = [r[0] for r in con.execute("SELECT id FROM contact WHERE kind = ?", (schema.EMAIL,))]
    con.close()
    queries_ = ["kind=email", "kind=email&q=alfa", "kind=email&q=office", "kind=email&q=ana+beta",
                "kind=email&q=servicii", "kind=email&status=verified", "kind=email&trust=likely",
                "kind=email&person=yes", "kind=email&person=no", "kind=email&source=verifier",
                "kind=email&website=yes", "kind=email&website=no&trust=confirmed",
                "kind=email&q=gama&person=yes&source=website"]
    matched = 0
    for q in queries_:
        f, _ = await filter_from_query(state, q)
        whole = {r["contact_id"]: r for r in await state.read("audience_rows", f)}
        matched += 0 < len(whole) < len(ids)
        for _ in range(4):
            some = rng.sample(ids, rng.randint(1, len(ids)))
            part = await state.read("audience_rows", f, None, some)
            assert {r["contact_id"] for r in part} == set(whole) & set(some), q
            for r in part:
                assert r == whole[r["contact_id"]], q
    # (Most filters keep some addresses and leave others.)
    assert matched >= 9
