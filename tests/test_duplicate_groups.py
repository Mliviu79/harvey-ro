"""Duplicates as groups, branches as locations, email as a key.

The review list used to be strictly pairwise: four records of one business
were six questions, a pair stayed on the list after one of its sides was
merged into a third company, and a chain's branches could only be merged
two at a time, their addresses lost. Now pending pairs are read as
connected groups (each side followed to the company it stands for), a
group merges into one keeper in one go (undone per member or all at once),
a merged company's address becomes one of the keeper's locations, an email
counts like a phone (a free-mail one never), and a contact several firms
share is never a reason to merge them.
"""

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from harvey import contacts, resolver
from harvey.collectors import discover as D
from harvey.models import Company
from harvey.modules import ingest
from harvey.signals import sync_vocabulary
from harvey.state import StateManager

TOWNS = ["Brașov", "Cluj", "Sibiu", "Iași", "Arad", "Deva", "Turda", "Bacău"]


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "g.db"))
    await sm.init_db()
    await sync_vocabulary(sm)
    for code in ("PHONE_NUMBER", "FOUND_IN_SERP", "CONTACT_FOUND"):
        await sm.set_signal_status(code, "confirmed")
    return sm


def _rows(path, sql, *args):
    with sqlite3.connect(path) as con:
        return con.execute(sql, args).fetchall()


def _phone(n: int) -> str:
    return f"+4072{n:07d}"


async def _listing(state, name, town, *, phone="", domain="", address="", n=0, cui=""):
    return await state.add_company(Company(
        name=name, location=f"{town}, Romania", phone=phone, domain=domain,
        website=f"https://{domain}" if domain else "", address=address, cui=cui,
        external_id=f"dataforseo:cid:{n}" if n else "", source="discover:dataforseo_maps"))


async def _second_phone(state, company_id, phone):
    await state.add_observation(company_id=company_id, signal_code="PHONE_NUMBER",
                                collector="profile", value_text=phone)


async def _chain(state, size: int) -> list[str]:
    """``size`` records without a CUI, each sharing a phone with the next:
    pairs only, never all of them one key, so the group is what joins them."""
    ids = []
    for i in range(size):
        cid = await _listing(state, f"Exemplu {chr(65 + i)}lfa", TOWNS[i], phone=_phone(i), n=i + 1)
        if i:
            await _second_phone(state, cid, _phone(i - 1))
        ids.append(cid)
    return ids


async def _scan(state) -> tuple[int, int]:
    done = await state.scan_duplicates()
    return done["merged"], done["review"]


async def _groups(state):
    total, groups = await state.duplicate_groups(100)
    assert total == len(groups)
    return groups


def _snapshot(path):
    tables = {
        "companies": "SELECT id, name, cui, external_id, domain, website, phone, address, "
                     "location, merged_into FROM companies ORDER BY id",
        "observations": "SELECT id, company_id FROM observations ORDER BY id",
        "company_ids": "SELECT company_id, kind, value FROM company_ids ORDER BY 1, 2, 3",
        "locations": "SELECT id, company_id, locality, address, external_id FROM locations ORDER BY id",
        "links": "SELECT id, company_id, contact_id, location_id FROM company_contacts ORDER BY id",
        "candidates": "SELECT company_a, company_b, status FROM match_candidates "
                      "WHERE status = 'separate' ORDER BY 1, 2",
    }
    return {name: _rows(path, sql) for name, sql in tables.items()}


# ── Groups ──


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [3, 4, 6])
async def test_a_chain_of_pairs_is_one_group_merged_in_one_go(state, size):
    ids = await _chain(state, size)
    done = await state.scan_duplicates()
    assert done["review"] == size - 1 and done["merged"] == 0

    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted(ids)
    assert len(group["pairs"]) == size - 1 and group["score"] == 0.8
    assert await state.count_duplicates() == 1

    keep = group["keeper"]
    result = await state.merge_group(keep, [m for m in ids if m != keep])
    assert result["survivor"] == keep and len(result["merged"]) == size - 1
    assert await _groups(state) == []
    assert _rows(state.db_path, "SELECT COUNT(*) FROM companies WHERE merged_into = ''") == [(1,)]
    merges = await state.merges()
    assert {m["group_id"] for m in merges} == {result["group_id"]}
    # Each member's town is one of the keeper's places now.
    assert len(_rows(state.db_path, "SELECT id FROM locations WHERE company_id = ?", keep)) \
        == size - 1


@pytest.mark.asyncio
async def test_a_pair_made_stale_by_a_merge_follows_the_survivor(state):
    a, b, c = await _chain(state, 3)
    await state.scan_duplicates()
    await state.merge_companies(a, b)               # the a-b pair, decided alone

    [group] = await _groups(state)
    # b-c is now a-c: b stands for a, and isn't shown as a company of its own.
    assert sorted(m["id"] for m in group["members"]) == sorted([a, c])
    assert b not in {m["id"] for m in group["members"]}


@pytest.mark.asyncio
async def test_undo_one_member_then_the_whole_group(state):
    ids = await _chain(state, 4)
    await state.scan_duplicates()
    before = _snapshot(state.db_path)
    [group] = await _groups(state)
    keep = group["keeper"]
    members = [m for m in ids if m != keep]
    await state.merge_group(keep, members)

    # One member back: it is its own company, and on the list again.
    await state.undo_merge(members[0])
    assert (await state.get_company(members[0])).merged_into == ""
    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted([keep, members[0]])

    # The rest of the group at once, from the keeper's id.
    undone = await state.undo_group(keep)
    assert {u["merged"] for u in undone} == set(members[1:])
    assert _snapshot(state.db_path) == before
    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted(ids)
    with pytest.raises(resolver.MergeRefused):
        await state.undo_group(keep)


@pytest.mark.asyncio
async def test_take_one_out_of_a_group(state):
    a, b, c = await _chain(state, 3)
    await state.scan_duplicates()
    await state.separate_group([c], [a, b])
    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted([a, b])

    # Said of c and b, it holds for the company b joins.
    await state.merge_group(a, [b])
    assert await _groups(state) == []
    assert await _scan(state) == (0, 0)
    assert await _groups(state) == []


@pytest.mark.asyncio
async def test_all_different_takes_the_whole_group_off(state):
    ids = await _chain(state, 4)
    await state.scan_duplicates()
    assert await state.separate_group(ids) == 6
    assert await _groups(state) == []


# ── Two CUIs ──


@pytest.mark.asyncio
async def test_a_group_with_two_cuis_never_merges_them(state):
    x = await _listing(state, "Alfa Unu SRL", "Brașov", phone=_phone(1), cui="90000001")
    y = await _listing(state, "Beta Doi SRL", "Cluj", phone=_phone(1), cui="90000002")
    # A listing with the phone both firms have: it could be either.
    saved = await state.save_company(Company(name="Gama", location="Sibiu, Romania",
                                             phone=_phone(1), external_id="dataforseo:cid:9"))
    assert saved.created and not saved.match.attach
    listing = saved.company_id
    assert {r["code"] for r in saved.match.reasons} >= {"phone", "shared_contact"}

    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted([x, y, listing])
    pairs = {frozenset((p["a"], p["b"])) for p in group["pairs"]}
    assert frozenset((x, y)) not in pairs              # never asked about the two firms
    keep = group["keeper"]
    other = ({x, y} - {keep}).pop()
    flags = {m["id"]: m["conflict"] for m in group["members"]}
    assert flags == {keep: False, other: True, listing: False}

    with pytest.raises(resolver.MergeRefused) as refused:
        await state.merge_group(keep, [listing, other])
    assert refused.value.code == "different_cui"
    assert _rows(state.db_path, "SELECT COUNT(*) FROM company_merges") == [(0,)]

    await state.merge_group(keep, [listing])
    # The listing is the kept firm's now; with the other firm, a CUI conflict.
    assert await _groups(state) == []


@pytest.mark.asyncio
async def test_a_chain_phone_on_300_firms_proposes_nothing(state):
    async with state.batch() as batch:
        for n in range(300):
            await batch.add_company(Company(name=f"Farmacia {n} Exemplu SRL", cui=str(91000000 + n),
                                            location=f"{TOWNS[n % 8]}, Romania",
                                            phone="+40210000300"))
    assert _rows(state.db_path, "SELECT COUNT(*) FROM company_ids WHERE kind = 'phone' "
                                "AND value = '+40210000300'") == [(300,)]
    saved = await state.save_company(Company(name="Farmacia Exemplu", location="Deva, Romania",
                                             phone="+40210000300", external_id="osm:node/1"))
    assert saved.created and saved.match.company_id is None and not saved.match.others
    assert await _scan(state) == (0, 0)
    assert await _groups(state) == []
    assert _rows(state.db_path, "SELECT COUNT(*) FROM match_candidates") == [(0,)]


@pytest.mark.asyncio
async def test_two_firms_sharing_a_phone_are_never_a_question(state):
    await _listing(state, "Alfa Unu SRL", "Brașov", phone=_phone(2), cui="90000003")
    await _listing(state, "Alfa Doi SRL", "Brașov", phone=_phone(2), cui="90000004")
    assert (await state.scan_duplicates())["review"] == 0
    assert await _groups(state) == []


# ── Branches as locations ──


@pytest.mark.asyncio
async def test_a_listing_with_the_one_firms_phone_becomes_its_location(state):
    firm = await _listing(state, "Exemplu Dent SRL", "Brașov", phone=_phone(3), cui="90000005",
                          address="Str. Exemplu nr. 1")
    biz = D.Business(name="Exemplu Dent Cluj", phone="0720 000 003", location="Cluj, Romania",
                     external_id="dataforseo:cid:31", provider="dataforseo_maps",
                     address="Str. Alta nr. 2")
    before = _snapshot(state.db_path)
    saved = await ingest.listing(state, D.business_record(biz, D.DiscoveryQuery(term="dentist")))

    assert saved.company_id == firm and not saved.created
    [merge] = await state.merges()
    assert merge["survivor"] == firm and {"phone", "one_firm"} <= {r["code"] for r in merge["reasons"]}
    [place] = _rows(state.db_path, "SELECT id, company_id, locality, address, external_id "
                                   "FROM locations")
    # (A search result's street address isn't kept: discover.business_record.)
    assert place[1:] == (firm, "Cluj", "", "dataforseo:cid:31")
    # The listing's phone reaches the firm through that place.
    tied = _rows(state.db_path, "SELECT DISTINCT l.location_id FROM company_contacts l "
                                "JOIN contacts ct ON ct.id = l.contact_id "
                                "WHERE l.company_id = ? AND ct.value = ? AND l.location_id != 0",
                 firm, _phone(3))
    assert tied == [(place[0],)]

    await state.undo_merge(merge["merged"])
    after = _snapshot(state.db_path)
    assert after["locations"] == [] and all(r[3] == 0 for r in after["links"] if r[1] == firm)
    assert {r[0] for r in after["companies"]} == {r[0] for r in before["companies"]} | {merge["merged"]}


@pytest.mark.asyncio
async def test_a_listing_matching_two_firms_goes_to_review(state):
    x = await _listing(state, "Alfa SRL", "Brașov", phone=_phone(4), cui="90000006")
    y = await _listing(state, "Gama SRL", "Sibiu", cui="90000007")
    # Its phone is one firm's, its name and town the other's.
    saved = await state.save_company(Company(
        name="Gama", location="Sibiu, Romania", phone=_phone(4),
        external_id="dataforseo:cid:41"))
    assert saved.created and not saved.match.attach
    assert saved.match.score < resolver.ATTACH
    asked = {frozenset((p["company_a"], p["company_b"])) for p in await state.duplicates()}
    assert asked == {frozenset((x, saved.company_id)), frozenset((y, saved.company_id))}
    assert await state.merges() == []


@pytest.mark.asyncio
async def test_a_group_merge_keeps_every_branch_address(state):
    firm = await _listing(state, "Exemplu Clinic SRL", "Brașov", cui="90000008",
                          address="Str. Exemplu nr. 1")
    branches = []
    for i, town in enumerate(("Cluj", "Sibiu", "Arad", "Deva", "Iași")):
        cid = await _listing(state, f"Exemplu Clinic {town}", town, phone=_phone(50),
                             address=f"Str. Filiala nr. {i}", n=50 + i)
        branches.append(cid)
    await state.add_observation(company_id=firm, signal_code="PHONE_NUMBER",
                                value_text=_phone(50))
    # One firm's phone on five listings: more than merge on their own.
    done = await state.scan_duplicates()
    assert done["merged"] == 0 and done["review"] == 5
    [group] = await _groups(state)
    assert group["keeper"] == firm and len(group["members"]) == 6
    assert {"phone", "many_listings"} <= {r["code"] for r in group["reasons"]}

    before = _snapshot(state.db_path)
    await state.merge_group(firm, branches)
    places = _rows(state.db_path, "SELECT locality FROM locations WHERE company_id = ? "
                                  "ORDER BY locality", firm)
    assert [p[0] for p in places] == ["Arad", "Cluj", "Deva", "Iași", "Sibiu"]
    await state.undo_group(firm)
    assert _snapshot(state.db_path) == before


@pytest.mark.asyncio
async def test_the_same_address_adds_no_location(state):
    firm = await _listing(state, "Exemplu SRL", "Brașov", cui="90000009", address="Str. Exemplu 1")
    other = await _listing(state, "Exemplu", "Brașov", address="Str. Exemplu 1", n=91)
    await state.merge_group(firm, [other])
    assert _rows(state.db_path, "SELECT COUNT(*) FROM locations") == [(0,)]


# ── Email as a key ──


async def _email(state, company_id, address):
    async with state._connect() as db:
        await contacts.add(db, company_id, contacts.Found("email", address, source="profile"))
        await db.commit()


@pytest.mark.asyncio
async def test_a_shared_email_is_a_question(state):
    a = await _listing(state, "Alfa Unu", "Brașov", n=1)
    b = await _listing(state, "Beta Doi", "Cluj", n=2)
    for cid in (a, b):
        await _email(state, cid, "Office@Exemplu-Dent.example.com")
    assert _rows(state.db_path, "SELECT COUNT(*) FROM company_ids WHERE kind = 'email' "
                                "AND value = 'office@exemplu-dent.example.com'") == [(2,)]
    await state.scan_duplicates()
    [group] = await _groups(state)
    assert group["score"] == 0.8
    assert group["reasons"] == [{"code": "email", "value": "office@exemplu-dent.example.com"}]
    assert "office@exemplu-dent.example.com" in group["members"][0]["emails"]


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["exemplu.fictiv.0000@gmail.com", "exemplu.fictiv.0000@yahoo.ro"])
async def test_a_free_mail_address_is_nobodys_identity(state, address):
    a = await _listing(state, "Alfa Unu", "Brașov", n=1)
    b = await _listing(state, "Beta Doi", "Cluj", n=2)
    for cid in (a, b):
        await _email(state, cid, address)
    assert not resolver.usable_email(address)
    assert (await state.scan_duplicates())["review"] == 0
    assert await _groups(state) == []


@pytest.mark.asyncio
async def test_existing_email_links_become_keys_on_upgrade(tmp_path):
    from harvey.state import MIGRATIONS
    path = str(tmp_path / "old.db")
    sm = StateManager(path)
    await sm.init_db()
    cid = await sm.add_company(Company(name="Exemplu", location="Cluj"))
    async with sm._connect() as db:
        await contacts.add(db, cid, contacts.Found("email", "ana@exemplu.example.com"))
        await db.commit()
    with sqlite3.connect(path) as con:      # as before v24: no email keys
        con.execute("DELETE FROM company_ids WHERE kind = 'email'")
        con.execute("DROP TRIGGER trg_company_contacts_email")
        con.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
    with sqlite3.connect(path) as con:
        con.executescript(MIGRATIONS[-1].replace(
            "ALTER TABLE company_merges ADD COLUMN group_id INTEGER DEFAULT 0;", ""))
    assert _rows(path, "SELECT company_id, value FROM company_ids WHERE kind = 'email'") == \
        [(cid, "ana@exemplu.example.com")]


# ── The scan ──


@pytest.mark.asyncio
async def test_scan_follows_a_pair_whose_side_merged_earlier_in_the_same_pass(state):
    """The scan used to drop a pair when an earlier pair of the same pass had
    merged one of its sides into a third company: here b joins the firm (same
    name, town and phone), and the b-c question was lost."""
    firm = await _listing(state, "EXEMPLU SRL", "Brașov", phone=_phone(7), cui="90000010")
    b = await _listing(state, "Exemplu", "Brașov", phone=_phone(7), n=71)
    c = await _listing(state, "Altceva Doi", "Cluj", phone=_phone(8), n=72)
    await _second_phone(state, b, _phone(8))

    done = await state.scan_duplicates()
    assert done["merged"] == 1
    [merge] = await state.merges()
    assert (merge["survivor"], merge["merged"]) == (firm, b)
    [group] = await _groups(state)
    assert sorted(m["id"] for m in group["members"]) == sorted([firm, c])


@pytest.mark.asyncio
async def test_scan_respects_what_a_person_decided(state):
    firm = await _listing(state, "Exemplu SRL", "Brașov", phone=_phone(9), cui="90000011")
    listing = await _listing(state, "Exemplu Cluj", "Cluj", phone=_phone(9), n=91)
    assert (await state.scan_duplicates())["merged"] == 1       # one firm's phone: a branch
    await state.undo_merge(listing)
    # Undone: back to the list, never merged on its own again.
    assert (await state.scan_duplicates())["merged"] == 0
    assert len(await _groups(state)) == 1
    await state.separate_group([listing], [firm])
    assert await _scan(state) == (0, 0)
    assert await _groups(state) == []


# ── API and CLI ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import harvey.dashboard as dash

    db = tmp_path / "harvey.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.db_path = db
        yield c


async def _seeded(path, size=4):
    sm = StateManager(str(path))
    await sm.init_db()
    await sync_vocabulary(sm)
    await sm.set_signal_status("PHONE_NUMBER", "confirmed")
    ids = await _chain(sm, size)
    await sm.scan_duplicates()
    return sm, ids


def test_group_merge_take_out_and_undo_over_the_api(client):
    _, ids = asyncio.run(_seeded(client.db_path))
    data = client.get("/api/duplicates").json()
    assert data["pending"] == 1
    [group] = data["rows"]
    keep = group["keeper"]
    rest = [m["id"] for m in group["members"] if m["id"] != keep]

    done = client.post("/api/duplicates/merge",
                       json={"keep": keep, "members": rest[:2], "out": rest[2:]}).json()
    assert done["survivor"] == keep and sorted(done["merged"]) == sorted(rest[:2])
    assert client.get("/api/duplicates").json()["pending"] == 0
    merged = client.get("/api/duplicates?status=merged").json()["rows"]
    assert {m["group_id"] for m in merged} == {done["group_id"]} and len(merged) == 2

    r = client.post("/api/duplicates/undo", json={"company_id": keep, "group": True})
    assert r.status_code == 200 and len(r.json()["undone"]) == 2
    # The one taken out stays apart from the keeper.
    [group] = client.get("/api/duplicates").json()["rows"]
    assert frozenset((keep, rest[2])) not in {frozenset((p["a"], p["b"])) for p in group["pairs"]}
    assert client.post("/api/duplicates/separate",
                       json={"out": [m["id"] for m in group["members"]]}).json() == {"ok": True}
    assert client.get("/api/duplicates").json()["pending"] == 0


def test_the_api_refuses_a_group_with_two_cuis(client):
    async def seed():
        sm = StateManager(str(client.db_path))
        await sm.init_db()
        x = await _listing(sm, "Alfa SRL", "Brașov", cui="90000012")
        y = await _listing(sm, "Beta SRL", "Cluj", cui="90000013")
        z = await _listing(sm, "Gama", "Sibiu", n=5)
        return x, y, z
    x, y, z = asyncio.run(seed())
    r = client.post("/api/duplicates/merge", json={"keep": x, "members": [z, y]})
    assert r.status_code == 409 and r.json()["error"] == "different_cui"
    assert _rows(client.db_path, "SELECT COUNT(*) FROM company_merges") == [(0,)]
    bad = client.post("/api/duplicates/merge", json={"keep": x})
    assert bad.status_code == 400


def test_the_scan_is_a_job(client):
    asyncio.run(_seeded(client.db_path, 3))
    first = client.post("/api/duplicates/scan").json()
    assert first["queued"] and first["job"]
    again = client.post("/api/duplicates/scan").json()
    assert again == {"job": first["job"], "queued": False}
    rows = _rows(client.db_path, "SELECT kind, lane, status FROM jobs WHERE id = ?", first["job"])
    assert rows == [("duplicates_scan", "duplicates", "queued")]


def test_cli_lists_groups_merges_them_and_undoes_the_group(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import harvey.state as state_mod
    from harvey import cli

    _, ids = asyncio.run(_seeded(tmp_path / "cli.db", 3))
    monkeypatch.setattr(state_mod, "DB_PATH", tmp_path / "cli.db")
    run = lambda **kw: cli.cmd_duplicates(SimpleNamespace(  # noqa: E731
        **{"merge": None, "separate": None, "separate_from": None, "undo": None,
           "undo_group": None, "scan": False, "limit": 50, **kw}))
    run()
    out = capsys.readouterr().out
    assert "1 group to decide" in out and "3 companies" in out and "keep" in out
    line = next(x for x in out.splitlines() if "--merge" in x)
    keep, *members = line.split("--merge", 1)[1].split()
    assert sorted([keep, *members]) == sorted(ids)

    run(merge=[keep, *members])
    out = capsys.readouterr().out
    hint = next(x for x in out.splitlines() if "--undo-group" in x).split("--undo-group", 1)[1]
    assert hint.split() and hint.split()[0] in members      # a merged member: unambiguous
    run(undo_group=hint.split()[0])
    assert "2 companies are on their own again" in capsys.readouterr().out
    run(separate=[members[0]], separate_from=[keep, members[1]])
    assert "Kept apart" in capsys.readouterr().out
    run()
    assert "2 companies" in capsys.readouterr().out


# ── What the review of the group merge found ──


def _key(path, company_id, kind, value):
    with sqlite3.connect(path) as con:
        con.execute("INSERT OR IGNORE INTO company_ids (company_id, kind, value) VALUES (?, ?, ?)",
                    (company_id, kind, value))
        con.commit()


@pytest.mark.asyncio
async def test_sure_pairs_chaining_to_two_firms_are_one_question(state):
    """L1 holds only F1's phone, L2 only F2's, and L1 and L2 are one business
    (name in one town and a phone). Each pair alone is sure; merged in turn,
    both listings would end in whichever firm came first and the other
    firm's pair would vanish on the CUI conflict. Nothing merges: it is one
    group for a person."""
    f1 = await _listing(state, "Alfa Unu SRL", "Brașov", phone=_phone(911), cui="90000911")
    f2 = await _listing(state, "Beta Doi SRL", "Cluj", phone=_phone(912), cui="90000912")
    l1 = await _listing(state, "Exemplu Zeta", "Sibiu", phone=_phone(913), n=911)
    l2 = await _listing(state, "Exemplu Zeta", "Sibiu", phone=_phone(914), n=912)
    for cid, value in ((l1, _phone(911)), (l2, _phone(912)), (l2, _phone(913))):
        _key(state.db_path, cid, "phone", value)
    done = await state.scan_duplicates()
    assert done["merged"] == 0
    assert _rows(state.db_path, "SELECT COUNT(*) FROM companies WHERE merged_into != ''") == [(0,)]
    [group] = await _groups(state)
    assert {l1, l2} <= {m["id"] for m in group["members"]}
    assert "several_firms" in {r["code"] for r in group["reasons"]}
    # Merging it all is refused; the keeper's firm and its listings merge.
    with pytest.raises(resolver.MergeRefused):
        await state.merge_group(f1, [l1, l2, f2])
    other = [m["id"] for m in group["members"] if m["conflict"]]
    assert other == [f2]


@pytest.mark.asyncio
async def test_undoing_one_member_leaves_what_another_still_gives_the_keeper(state):
    keep = await _listing(state, "Exemplu Dent SRL", "Brașov", phone=_phone(921),
                          cui="90000921", address="Str. Exemplu 1")
    a = await _listing(state, "Exemplu Dent Cluj", "Cluj", phone=_phone(922), n=921)
    b = await _listing(state, "Exemplu Dent Sibiu", "Sibiu", phone=_phone(923), n=922)
    shared = contacts.Found("email", "office@dent.example.com", source="site")
    for cid in (a, b):
        await state.add_contacts(cid, [shared])
        await state.add_company_source(cid, "discover:serper")
    await state.merge_group(keep, [a, b])

    def held(cid):
        return (_rows(state.db_path, "SELECT ct.value FROM company_contacts l JOIN contacts ct "
                                     "ON ct.id = l.contact_id WHERE l.company_id = ? "
                                     "AND ct.kind = 'email'", cid),
                _rows(state.db_path, "SELECT source FROM company_sources WHERE company_id = ? "
                                     "AND source = 'discover:serper'", cid),
                _rows(state.db_path, "SELECT value FROM company_ids WHERE company_id = ? "
                                     "AND kind = 'email'", cid))
    await state.undo_merge(a)
    emails, sources, keys = held(keep)
    assert emails and sources and keys == [("office@dent.example.com",)]
    assert held(a)[0] and held(a)[2]            # a has its own back, key and all
    # Undoing b too leaves the keeper as it was before either merge.
    await state.undo_merge(b)
    assert held(keep) == ([], [], [])
    assert held(b)[0] and held(b)[1] and held(b)[2]


@pytest.mark.asyncio
async def test_undoing_one_member_keeps_a_column_another_member_fills(state):
    keep = await _listing(state, "Exemplu Gama SRL", "Brașov", cui="90000931")
    a = await _listing(state, "Exemplu Gama Cluj", "Cluj", phone=_phone(932), n=931)
    b = await _listing(state, "Exemplu Gama Sibiu", "Sibiu", phone=_phone(933), n=932)
    before = _snapshot(state.db_path)
    await state.merge_group(keep, [a, b])
    assert (await state.get_company(keep)).phone == _phone(932)
    await state.undo_merge(a)
    assert (await state.get_company(keep)).phone == _phone(933)
    assert (await state.get_company(a)).external_id == "dataforseo:cid:931"
    await state.undo_merge(b)
    assert _snapshot(state.db_path)["companies"] == before["companies"]
    assert _snapshot(state.db_path)["company_ids"] == before["company_ids"]


@pytest.mark.asyncio
async def test_two_branches_in_one_town_keep_their_place_when_one_is_undone(state):
    keep = await _listing(state, "Exemplu Eta SRL", "Brașov", phone=_phone(941),
                          cui="90000941", address="Str. Exemplu 1")
    a = await _listing(state, "Exemplu Eta Unu", "Cluj", phone=_phone(942))
    b = await _listing(state, "Exemplu Eta Doi", "Cluj", phone=_phone(943))
    before = _snapshot(state.db_path)
    await state.merge_group(keep, [a, b])
    [(place,)] = _rows(state.db_path, "SELECT id FROM locations WHERE company_id = ?", keep)
    await state.undo_merge(a)
    assert _rows(state.db_path, "SELECT id FROM locations WHERE company_id = ?", keep) \
        == [(place,)]
    tied = _rows(state.db_path, "SELECT l.location_id FROM company_contacts l JOIN contacts ct "
                                "ON ct.id = l.contact_id WHERE ct.value = ?", _phone(943))
    assert tied and all(t == (place,) for t in tied)
    await state.undo_merge(b)
    assert _snapshot(state.db_path) == before


@pytest.mark.asyncio
async def test_two_records_without_a_cui_sharing_a_website_are_a_question(state):
    x = await _listing(state, "Exemplu Rho", "Brașov", phone=_phone(951), n=951)
    y = await _listing(state, "Exemplu Sigma", "Deva", phone=_phone(952), n=952)
    for cid in (x, y):
        _key(state.db_path, cid, "domain", "grup.example.com")
    assert await _scan(state) == (0, 1)
    [group] = await _groups(state)
    assert "shared_site" in {r["code"] for r in group["reasons"]}


@pytest.mark.asyncio
async def test_a_pair_waiting_on_the_list_is_not_news_on_every_look(state):
    await _listing(state, "Exemplu Iota SRL", "Brașov", phone=_phone(961), cui="90000961")
    branch = await _listing(state, "Exemplu Iota Cluj", "Cluj", phone=_phone(962), n=961)
    _key(state.db_path, branch, "phone", _phone(961))
    assert await _scan(state) == (1, 0)
    await state.undo_merge(branch)
    assert await _scan(state) == (0, 0)
    assert await _scan(state) == (0, 0)
    assert await state.count_duplicates() == 1


@pytest.mark.asyncio
async def test_an_email_found_after_a_merge_goes_back_with_its_link(state):
    firm = await _listing(state, "Exemplu Kappa SRL", "Brașov", phone=_phone(971),
                          cui="90000971")
    branch = await _listing(state, "Exemplu Kappa Cluj", "Cluj", phone=_phone(972), n=971)
    await state.merge_group(firm, [branch])
    await state.add_contacts(firm, [contacts.Found("email", "cluj@kappa.example.com",
                                                   source="site")], origin=branch)
    await state.undo_merge(branch)
    keys = _rows(state.db_path, "SELECT company_id FROM company_ids WHERE kind = 'email'")
    assert keys == [(branch,)]


@pytest.mark.asyncio
async def test_undo_group_names_a_merged_member_when_the_keeper_kept_two(state):
    keep = await _listing(state, "Exemplu Lambda SRL", "Brașov", cui="90000981")
    a = await _listing(state, "Exemplu Lambda Cluj", "Cluj", phone=_phone(982), n=981)
    b = await _listing(state, "Exemplu Lambda Sibiu", "Sibiu", phone=_phone(983), n=982)
    c = await _listing(state, "Exemplu Lambda Arad", "Arad", phone=_phone(984), n=983)
    await state.merge_group(keep, [a, b])
    await state.merge_group(keep, [c])
    with pytest.raises(resolver.MergeRefused) as refused:
        await state.undo_group(keep)
    assert refused.value.code == "several_groups"
    done = await state.undo_group(b)
    assert sorted(d["merged"] for d in done) == sorted([a, b])
    assert (await state.get_company(c)).merged_into == keep
