"""data/anaf.db in its compressed form (harvey/modules/ro/anaf/store.py): the
rewrite of a plain file, its hash check, answers kept across refreshes, and
the ANAF job reading and writing either form. Every answer is invented, in
the shape ANAF's PlatitorTvaRest v9 returns; every CUI is fictional."""

import json
import sqlite3

import pytest

from harvey.modules.ro.anaf import capabilities as caps
from harvey.modules.ro.anaf import service, store


def answer(cui: str, *, vat=True, efactura=True, inactive=False, phone="0268 000 210",
           street="Str. Exemplu") -> dict:
    return {
        "date_generale": {
            "cui": int(cui), "data": "2026-10-30", "denumire": f"EXEMPLU {cui} SRL",
            "adresa": "JUD. BRAŞOV, MUN. BRAŞOV, STR. EXEMPLU, NR.1", "nrRegCom": "J08/1/2015",
            "telefon": phone, "fax": "", "codPostal": "500001", "act": "",
            "stare_inregistrare": "INREGISTRAT din data 01.03.2015",
            "data_inregistrare": "2015-03-01", "cod_CAEN": "8623", "iban": "",
            "statusRO_e_Factura": efactura, "organFiscalCompetent": "",
            "forma_de_proprietate": "", "forma_organizare": "PERSOANA JURIDICA",
            "forma_juridica": "SOCIETATE CU RASPUNDERE LIMITATA"},
        "inregistrare_scop_Tva": {"scpTVA": vat, "perioade_TVA": [
            {"data_inceput_ScpTVA": "2019-05-01", "data_sfarsit_ScpTVA": "",
             "data_anul_imp_ScpTVA": "", "mesaj_ScpTVA": ""}] if vat else []},
        "inregistrare_RTVAI": {"statusTvaIncasare": False},
        "stare_inactiv": {"statusInactivi": inactive},
        "inregistrare_SplitTVA": {"statusSplitTVA": False},
        "adresa_sediu_social": {"sdenumire_Strada": street, "snumar_Strada": "1",
                                "sdenumire_Localitate": "Mun. Braşov",
                                "sdenumire_Judet": "BRAŞOV", "scod_Postal": "500001"},
        "adresa_domiciliu_fiscal": {},
    }


CUIS = [str(9100000 + i) for i in range(1, 41)]


def plain_file(path, cuis=CUIS) -> None:
    """A version 1 file as service.keep writes it."""
    found = {c: answer(c, vat=i % 2 == 0, inactive=i % 7 == 0) for i, c in enumerate(cuis)
             if i % 5}
    missing = {c for i, c in enumerate(cuis) if not i % 5}
    service.keep(path, found, missing, "2026-10-01T08:00:00+00:00")


def test_a_plain_file_is_rewritten_compressed_and_every_answer_comes_back(tmp_path):
    src, dst = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)
    before = src.read_bytes()
    out = store.rewrite(src, dst, chunk=7, min_free=0)
    assert src.read_bytes() == before                 # only read
    assert out["rows"] == len(CUIS) and out["mismatched"] == 0 and out["text_keys"] == 0
    assert out["time_not_round_trip"] == 0
    check = store.verify(src, dst)
    assert check == {"original": len(CUIS), "rewritten": len(CUIS), "parsed": len(CUIS),
                     "finished": True, "equal": True}
    con = sqlite3.connect(dst)
    assert store.version(con) == store.VERSION
    assert con.execute("SELECT typeof(cui), typeof(fetched), typeof(body) FROM answers "
                       "WHERE found = 1 LIMIT 1").fetchone() == ("integer", "integer", "blob")
    assert con.execute("SELECT COUNT(*) FROM answers WHERE dict = ?",
                       (store.DICT_ID,)).fetchone()[0] == 32
    # The parsed fields say what the check's facts say.
    for cui in CUIS[1:4]:
        vat, efactura, inactive, phone, form = con.execute(
            "SELECT vat, efactura, inactive, phone, legal_form FROM parsed WHERE cui = ?",
            (int(cui),)).fetchone()
        facts, contacts = caps.facts_for(json.loads(store.kept(con, [cui])[cui][2]))
        by_code = {f.code: f for f in facts}
        assert vat == int(by_code["VAT_REGISTERED"].value_num)
        assert efactura == int(by_code["EFACTURA_REGISTERED"].value_num)
        assert inactive == int(by_code["FISCALLY_INACTIVE"].value_num)
        assert phone == contacts[0].value and form == by_code["LEGAL_FORM"].value_text
    # A CUI ANAF didn't know: checked, every yes/no reads no.
    assert con.execute("SELECT vat, efactura, status FROM parsed WHERE cui = ?",
                       (int(CUIS[0]),)).fetchone() == (0, 0, None)
    # Compressed bodies are a fraction of the plain ones.
    plain = sqlite3.connect(src).execute("SELECT SUM(length(body)) FROM answers").fetchone()[0]
    packed = con.execute("SELECT SUM(length(body)) FROM answers").fetchone()[0]
    assert packed < plain / 2


def test_a_tampered_body_fails_the_hash_check(tmp_path):
    src, dst = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)
    store.rewrite(src, dst, min_free=0)
    con = sqlite3.connect(dst)
    cui = int(CUIS[1])
    dict_id, blob = store.compress(json.dumps(answer(CUIS[1], vat=False)))
    con.execute("UPDATE answers SET body = ? WHERE cui = ?", (blob, cui))
    con.commit()
    assert store.verify(src, dst)["equal"] is False


class Stop(Exception):
    pass


def test_a_rewrite_cut_short_resumes_to_the_same_answers(tmp_path):
    src = tmp_path / "anaf.db"
    plain_file(src)
    whole = store.rewrite(src, tmp_path / "whole.db", chunk=4, min_free=0)
    calls = [0]

    def stop(rows):
        calls[0] += 1
        if calls[0] % 3 == 0:
            raise Stop

    cut = tmp_path / "cut.db"
    for _ in range(20):
        try:
            out = store.rewrite(src, cut, chunk=4, on_chunk=stop, min_free=0)
            break
        except Stop:
            continue
    assert out["digest"] == whole["digest"] and out["rows"] == whole["rows"]
    assert store.verify(src, cut)["equal"]
    rows = lambda p: sqlite3.connect(p).execute(  # noqa: E731
        "SELECT * FROM answers ORDER BY cui").fetchall()
    assert rows(cut) == rows(tmp_path / "whole.db")


def test_the_job_keeps_every_replaced_answer_in_the_compressed_file(tmp_path):
    src, path = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)
    store.rewrite(src, path, min_free=0)
    a, b = CUIS[1], CUIS[2]
    # The same answer again: no history, only the time moves.
    service.keep(path, {a: answer(a, vat=False)}, set(), "2026-11-01T08:00:00+00:00")
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM answers_history").fetchone()[0] == 0
    assert con.execute("SELECT fetched FROM answers WHERE cui = ?", (int(a),)).fetchone()[0] == \
        store.to_unix("2026-11-01T08:00:00+00:00")
    # A different one: the old answer goes to history, raw and whole.
    old_body = store.kept(con, [b])[b][2]
    con.close()
    service.keep(path, {b: answer(b, street="Str. Alta")}, {CUIS[0]}, "2026-11-02T08:00:00+00:00")
    con = sqlite3.connect(path)
    hist = con.execute("SELECT fetched, found, dict, body FROM answers_history WHERE cui = ?",
                       (int(b),)).fetchall()
    assert len(hist) == 1 and store.decompress(hist[0][2], hist[0][3]) == old_body
    assert "Str. Alta" in con.execute("SELECT address FROM parsed WHERE cui = ?",
                                      (int(b),)).fetchone()[0]
    con.close()
    # The job reads the compressed file as it read the plain one.
    kept = service.kept(path, [a, b, CUIS[0], "9199999"])
    assert set(kept) == {a, b, CUIS[0]} and kept[CUIS[0]][1] is None
    assert kept[b][1]["adresa_sediu_social"]["sdenumire_Strada"] == "Str. Alta"
    assert service.kept(path, [b], read_only=True) == {b: kept[b]}
    status = service.status(path)
    assert status["answers"] == len(CUIS) and status["latest"] == "2026-11-02T08:00:00+00:00"


def test_a_plain_file_stays_plain_until_the_upgrade(tmp_path):
    path = tmp_path / "anaf.db"
    plain_file(path)
    service.keep(path, {CUIS[1]: answer(CUIS[1], vat=True)}, set(), "2026-11-01T08:00:00+00:00")
    con = sqlite3.connect(path)
    assert store.version(con) == 1
    assert con.execute("SELECT typeof(body) FROM answers LIMIT 1").fetchone()[0] == "text"


def test_bodies_round_trip_with_the_dictionary_named_on_the_row():
    body = json.dumps(answer("9100001"), ensure_ascii=False, separators=(",", ":"))
    dict_id, blob = store.compress(body)
    assert dict_id == store.DICT_ID and store.decompress(dict_id, blob) == body
    assert store.compress("") == (None, None) and store.decompress(None, None) == ""
    other = {7: b'{"other":"dictionary"}'}
    with pytest.raises(Exception):
        store.decompress(7, blob, other)


def test_an_unfinished_rewrite_is_never_taken_for_a_whole_file(tmp_path):
    src, dst = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)

    def stop(rows):
        raise Stop

    with pytest.raises(Stop):
        store.rewrite(src, dst, chunk=4, on_chunk=stop, min_free=0)
    con = sqlite3.connect(dst)
    assert store.version(con) == 0
    con.close()
    with pytest.raises(store.Unfinished):
        service.kept(dst, [CUIS[1]])
    with pytest.raises(store.Unfinished):
        service.keep(dst, {CUIS[1]: answer(CUIS[1])}, set(), "2026-11-01T08:00:00+00:00")
    assert store.verify(src, dst)["equal"] is False
    store.rewrite(src, dst, chunk=4, min_free=0)
    assert store.verify(src, dst) == {"original": len(CUIS), "rewritten": len(CUIS),
                                      "parsed": len(CUIS), "finished": True, "equal": True}
    # A file that is something else is never written over.
    with pytest.raises(store.Refused, match="isn't a rewrite's output"):
        store.rewrite(dst, src, min_free=0)


def test_a_rewrite_refuses_to_start_without_room(tmp_path, monkeypatch):
    from harvey import disk

    src, dst = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)
    monkeypatch.setattr(disk, "free_bytes", lambda path: 29 * 10**9)
    with pytest.raises(store.Refused, match="30 GB"):
        store.rewrite(src, dst)
    assert not dst.exists()


def test_an_answer_under_a_key_that_isnt_a_number_is_kept_whole(tmp_path):
    src, dst = tmp_path / "anaf.db", tmp_path / "anaf.db.new"
    plain_file(src)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO answers VALUES ('0912345', '2026-10-01T08:00:00+00:00', 1, ?)",
                (json.dumps(answer("912345")),))
    con.commit()
    con.close()
    out = store.rewrite(src, dst, min_free=0)
    assert out["rows"] == len(CUIS) + 1 and out["text_keys"] == 1
    assert store.verify(src, dst)["equal"]
    con = sqlite3.connect(dst)
    assert store.kept(con, ["0912345"])["0912345"][1] == 1
    con.close()
    service.keep(dst, {"0912345": answer("912345", vat=False)}, set(), "2026-11-01T08:00:00+00:00")
    assert service.kept(dst, ["0912345"])["0912345"][1]["inregistrare_scop_Tva"]["scpTVA"] is False


def test_two_answers_replaced_in_the_same_second_both_stay_in_history(tmp_path):
    path = tmp_path / "anaf.db"
    con = sqlite3.connect(path)
    store.create(con)
    a = CUIS[1]
    for street, when in (("Str. Unu", "2026-11-01T08:00:00+00:00"),
                         ("Str. Doi", "2026-11-01T08:00:00+00:00"),
                         ("Str. Trei", "2026-11-01T08:00:00+00:00"),
                         ("Str. Patru", "2026-11-02T08:00:00+00:00")):
        store.keep(con, [(a, when, 1, json.dumps(answer(a, street=street)))])
    con.commit()
    bodies = [store.decompress(d, b) for d, b in con.execute(
        "SELECT dict, body FROM answers_history WHERE cui = ? ORDER BY rowid", (int(a),))]
    assert [json.loads(b)["adresa_sediu_social"]["sdenumire_Strada"] for b in bodies] == \
        ["Str. Unu", "Str. Doi", "Str. Trei"]
