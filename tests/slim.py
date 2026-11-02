"""Small helpers for tests on the slim database (schema 25): harvey.db holds
Harvey's own rows and the registers are read in place from files beside it.
Every value they write is fictional."""

import sqlite3
from pathlib import Path

from harvey import registry


def q(state_or_path, sql, *args) -> list[tuple]:
    path = getattr(state_or_path, "db_path", state_or_path)
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def one(state_or_path, sql, *args):
    rows = q(state_or_path, sql, *args)
    return rows[0][0] if rows else None


def folder(state) -> Path:
    """The folder the registers are read from: the database's own."""
    return Path(state.db_path).parent


def registry_beside(state, files, dataset) -> Path:
    """The trade registry loaded from ``files`` beside the database (where
    the state reads it), as `harvey registry load` leaves it."""
    path = folder(state) / "registry.db"
    registry.build(files, dataset, path)
    return path


def firm_of_cui(state, cui) -> int | None:
    """The record holding a CUI (a member, after a merge)."""
    return one(state, "SELECT fi.firm FROM firm_ident fi JOIN ident_scheme s "
                      "ON s.id = fi.scheme WHERE s.name = 'ro.cui' AND fi.key = ?", int(cui))


def live_ids(state) -> list[str]:
    return [str(r[0]) for r in q(state, "SELECT id FROM firm WHERE live = 0 ORDER BY id")]


async def register_values(state, code, ids=None) -> list[str]:
    """Every value a loaded register gives ``code`` for the firms in Harvey
    (or ``ids``), sorted: what schema 24 copied into observations."""
    out = []
    for cid in (ids if ids is not None else live_ids(state)):
        for r in await state.read("register_readings", cid, [code]):
            out.append(r["value_text"] if r["value_text"] is not None else r["value_num"])
    return sorted(out)


async def web_values(state, code, ids=None) -> list[str]:
    """The stored (web) readings' history of ``code`` for the firms in
    Harvey (or ``ids``), sorted."""
    out = []
    for cid in (ids if ids is not None else live_ids(state)):
        out += [r["value_text"] for r in await state.get_observations(cid, code)]
    return sorted(out)


def registry_of(state, firms, dataset, codes=None, people=()) -> Path:
    """The trade registry beside the database holding ``firms``
    (collectors.onrc.Firm, operating), each authorised for ``codes``
    {reg: [(code, revision)]} (8623 Rev.3 by default), with ``people``
    [(reg, "FAMILY GIVEN", role)] as administrators: built from files in
    the registry's own shape, as a load builds it."""
    from tests.test_onrc import FIRM_HEADER, firm, write

    src = folder(state) / "registry-src"
    src.mkdir(exist_ok=True)
    rows = [FIRM_HEADER]
    for f in firms:
        y, m, d = (f.registered or "2010-01-01").split("-")
        row = list(firm(f.name, f.cui, f.reg, f.form, f.county, f.locality, f.address, "",
                        "", "", f.website or f.domain))
        row[3] = f"{d}/{m}/{y}"
        rows.append(tuple(row))
    caen = [("COD_INMATRICULARE", "COD_CAEN_AUTORIZAT", "VER_CAEN_AUTORIZAT")]
    status = [("COD_INMATRICULARE", "COD", "")]
    for f in firms:
        for code, rev in (codes or {}).get(f.reg, [("8623", 3)]):
            caen.append((f.reg, code, str(rev)))
        status.append((f.reg, "1048", ""))
    files = {"firms": write(src / "firms.csv", rows), "caen": write(src / "caen.csv", caen),
             "status": write(src / "status.csv", status)}
    if people:
        header = ("COD_INMATRICULARE", "PERSOANA_IMPUTERNICITA", "CALITATE", "DATA_NASTERE",
                  "LOCALITATE_NASTERE", "JUDET_NASTERE", "TARA_NASTERE", "LOCALITATE",
                  "JUDET", "TARA")
        files["people"] = write(src / "people.csv", [header] + [
            (reg, name, role, "", "", "", "", "", "", "") for reg, name, role in people])
    return registry_beside(state, files, dataset)


def describe(state, *firms, dataset=None, people=(), codes=None):
    """Add ``firms`` (collectors.onrc.Firm) to the registry beside the
    database (every firm added so far kept): what describes a record that
    holds their CUI. ``codes``: {reg: [(code, revision)]}, 8623 Rev.3 by
    default."""
    from harvey.collectors import onrc

    held = getattr(state, "_registry_firms", {})
    held.update({f.reg: f for f in firms})
    state._registry_firms = held
    state._registry_people = list(getattr(state, "_registry_people", [])) + list(people)
    state._registry_codes = {**getattr(state, "_registry_codes", {}), **(codes or {})}
    registry_of(state, list(held.values()), dataset or onrc.Dataset(
        "firme-01-01-2026", "2026-01-01", {}), people=state._registry_people,
        codes=state._registry_codes)


def firm(name, cui, town="Brașov", address="", website="", domain="", county=""):
    """An operating registry firm (collectors.onrc.Firm) for describe()."""
    from harvey.collectors import onrc

    return onrc.Firm(reg=f"J08/{cui}/2010", name=name, cui=str(cui), form="SRL",
                     county=county or town, locality=town, address=address, website=website,
                     domain=domain, registered="2010-01-01")


async def pull(state, *firms, dataset, codes=None, people=(), saved=None):
    """A registry pull of ``firms`` as the onrc module saves one, with the
    registry beside the database holding every firm pulled so far (where
    the pulled firms' names and places are read)."""
    from harvey.collectors import onrc
    from harvey.modules import runs

    held = getattr(state, "_registry_firms", {})
    held.update({f.reg: f for f in firms})
    state._registry_firms = held
    state._registry_people = list(getattr(state, "_registry_people", [])) + list(people)
    registry_of(state, list(held.values()), dataset, codes=codes, people=state._registry_people)
    report = onrc.ImportReport(dataset=dataset.name)
    draft = onrc.firms_draft(list(firms), dataset, codes={f.reg: [("8623", 3)] for f in firms},
                             classes=[("8623", None)]).stamp("onrc", "registry_search")
    draft.values.update(codes=("ACTIVITY_CODE",), collector=onrc.COLLECTOR)
    await runs.save_source_draft(state, draft, report, module="onrc", saved_ids=saved)
    assert not report.errors, report.errors
    return report


SNAPSHOT = {
    "firm": "SELECT id, live, display_name, website, domain, website_trust, website_fill "
            "FROM firm ORDER BY id",
    "firm_ident": "SELECT scheme, key, firm FROM firm_ident ORDER BY 1, 2",
    "firm_key": "SELECT kind, value, firm FROM firm_key ORDER BY 1, 2, 3",
    "local_firm": "SELECT * FROM local_firm ORDER BY firm",
    "firm_source": "SELECT firm, source, batch, ref FROM firm_source ORDER BY 1, 2, 3, 4",
    "link": "SELECT firm, contact, trust, person, location FROM link ORDER BY 1, 2",
    "link_evidence": "SELECT firm, contact, source, person, trust FROM link_evidence "
                     "ORDER BY 1, 2, 3, 4",
    "person": "SELECT id, firm, name, role FROM person ORDER BY id",
    "obs": "SELECT id, firm, signal, key, num, text FROM obs ORDER BY id",
    "prospects": "SELECT id, company_id FROM prospects ORDER BY id",
    "locations": "SELECT * FROM locations ORDER BY id",
}


def snapshot(state) -> dict:
    """Harvey's own rows (no clocks): what a merge and its undo must leave
    as they were."""
    return {name: q(state, sql) for name, sql in SNAPSHOT.items()}


def recount_diff(state) -> dict:
    """Kept values that differ from a full recount (empty when the
    triggers and bulk recounts kept every count right)."""
    from harvey.overlay import recount

    con = sqlite3.connect(state.db_path, isolation_level=None)
    try:
        return {k: v for k, v in recount.diff(con).items() if v}
    finally:
        con.close()


# A contact with its kind's name (contacts.kind is the kind's id on schema 25).
CONTACTS = "(SELECT c.*, k.kind AS kind_name FROM contact c JOIN contact_kinds k ON k.id = c.kind)"
# A link's evidence with its source's name and the trust's name.
EVIDENCE = ("(SELECT e.*, s.name AS source_name, CASE e.trust WHEN 0 THEN 'confirmed' "
            "WHEN 1 THEN 'likely' ELSE 'unconfirmed' END AS trust_name FROM link_evidence e "
            "JOIN source s ON s.id = e.source)")


def links(state, cid=None) -> list[tuple]:
    """(kind, value, line type, trust, person, status) of every link (a
    record's and those merged into it, when ``cid`` is given), sorted."""
    where, args = "", ()
    if cid is not None:
        where = "WHERE l.firm IN (SELECT ? UNION ALL SELECT id FROM firm WHERE live = ?)"
        args = (int(cid), int(cid))
    lines = {0: "", 1: "mobile", 2: "fixed", 3: "voip", 4: "toll_free"}
    statuses = {0: "unchecked", 1: "verified", 2: "risky", 3: "guess", 4: "invalid", 5: "valid"}
    trusts = {0: "confirmed", 1: "likely", 2: "unconfirmed"}
    rows = q(state, f"SELECT k.kind, c.value, c.line_type, l.trust, COALESCE(p.name, ''), "
                    f"c.status FROM link l JOIN contact c ON c.id = l.contact "
                    f"JOIN contact_kinds k ON k.id = c.kind LEFT JOIN person p ON p.id = l.person "
                    f"{where}", *args)
    return sorted((k, v, lines.get(lt or 0, ""), trusts[t], p, statuses.get(st or 0, ""))
                  for k, v, lt, t, p, st in rows)


def searches(state) -> list[tuple]:
    """(target, module, capability, looked for, outcome, cost, batch) of
    every search, in order."""
    outcomes = {0: "error", 1: "found", 2: "nothing", 3: "skipped"}
    bits = dict(q(state, "SELECT bit, name FROM looked_for"))
    out = []
    for is_person, module, cap, looked, outcome, cost, batch in q(
            state, "SELECT s.is_person, m.name, c.name, s.looked_for, s.outcome, s.cost, b.ref "
                   "FROM search s JOIN module m ON m.id = s.module "
                   "JOIN capability c ON c.id = s.capability LEFT JOIN batch b ON b.id = s.batch "
                   "ORDER BY s.id"):
        names = ",".join(n for b, n in sorted(bits.items()) if looked & b)
        out.append(("person" if is_person else "company", module, cap, names,
                    outcomes[outcome], cost, batch or ""))
    return out


def financials_beside(state, rows, year: int = 2024) -> Path:
    """The financial statements beside the database: ``rows`` (cui,
    turnover, employees) for ``year``, or (cui, year, turnover, employees),
    as a load builds them."""
    from harvey.modules.ro.mf_financials import derived, store

    rows = [r if len(r) == 4 else (r[0], year, r[1], r[2]) for r in rows]
    path = folder(state) / "financials.db"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.execute(store.LATEST)
    con.executemany("INSERT INTO accounts (cui, year, form, turnover, employees) "
                    "VALUES (?, ?, 'UU', ?, ?)",
                    [(str(cui), y, turnover, employees) for cui, y, turnover, employees in rows])
    con.execute(store.FILL_LATEST)
    years = ",".join(str(y) for y in sorted({r[1] for r in rows}))
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("years", years),
                                                      ("loaded_at", "2026-01-02 00:00:00")])
    derived.build(con)
    con.commit()
    con.close()
    return path


def taxpayers_beside(state, rows, published: str = "2026-06-30") -> Path:
    """The taxpayer file beside the database: ``rows`` (cui, name, vat,
    employer, micro_tax, phone), as a load builds it."""
    from harvey.modules.ro.mf_taxpayers import derived, store

    path = folder(state) / "taxpayers.db"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(store.SCHEMA)
    con.executemany("INSERT INTO taxpayers (cui, name, vat, employer, micro_tax, phone) "
                    "VALUES (?, ?, ?, ?, ?, ?)", [(str(r[0]), *r[1:]) for r in rows])
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("published", published),
                                                      ("loaded_at", f"{published} 00:00:00")])
    derived.build(con)
    con.commit()
    con.close()
    return path
