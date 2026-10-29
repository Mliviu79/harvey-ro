"""CAEN activity codes found by words (harvey/caen.py).

Covers: the shipped nomenclature loads, every synonym points at a class that
exists, English and Romanian synonyms, accents and inflection, both
revisions for the classes Rev.3 split out of Rev.2's 8690, describe(),
unknown words, the `harvey caen` command and the dashboard endpoint.
Everything is offline: the nomenclature ships with the package.
"""

import sys

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import caen, cli

ORIGIN = "http://127.0.0.1:5555"


def _found(words):
    return [(m.code, m.version) for m in caen.search(words)]


# ── The nomenclature ──


def test_nomenclature_holds_both_current_revisions():
    classes = caen._classes()
    by_version = {v: sum(1 for (_, ver) in classes if ver == v) for v in (2, 3)}
    assert by_version[2] > 600 and by_version[3] > 600
    assert classes[("8623", 2)] == "Activități de asistență stomatologică"
    assert ("8690", 3) not in classes        # split up in Rev.3
    assert ("8695", 2) not in classes        # new in Rev.3
    assert ("0111", 2) in classes            # leading zero kept


def test_every_synonym_points_at_a_real_class():
    classes = caen._classes()
    missing = [(phrase, c) for phrase, cs in caen.SYNONYMS.items() for c in cs
               if c not in classes]
    assert missing == []


def test_every_english_name_belongs_to_a_real_class():
    assert [k for k in caen.ENGLISH_NAMES if k not in caen._classes()] == []


# ── Synonyms ──


@pytest.mark.parametrize("words", ["dentist", "dental", "stomatolog", "Stomatologie"])
def test_dental_words_in_both_languages(words):
    # Synonym classes first; classes that merely mention the word may follow
    # in search(), but codes_for() keeps to what the synonym means.
    assert _found(words)[:2] == [("8623", 3), ("8623", 2)]
    assert caen.codes_for(words) == {"8623"}


@pytest.mark.parametrize("words, code", [
    ("pharmacy", "4773"), ("farmacie", "4773"),
    ("doctor", "8621"), ("general practice", "8621"), ("medic de familie", "8621"),
    ("specialist", "8622"), ("clinica", "8622"), ("policlinica", "8622"),
    ("optician", "4778"), ("optica", "4778"),
    ("veterinary", "7500"),
])
def test_everyday_words_find_their_class_in_both_revisions(words, code):
    assert caen.codes_for(words) == {code}
    assert _found(words)[:2] == [(code, 3), (code, 2)]


def test_multi_word_synonym_wins_over_its_parts():
    hit = caen.search("Medicină de familie")[0]
    assert (hit.code, hit.why) == ("8621", 'synonym "medic de familie"')


@pytest.mark.parametrize("words, rev3", [
    ("lab", "8691"), ("laborator", "8691"), ("analize", "8691"),
    ("imaging", "8691"), ("radiologie", "8691"),
    ("physio", "8695"), ("kinetoterapie", "8695"), ("fizioterapie", "8695"),
    ("recuperare", "8695"), ("kinetoterapeut", "8695"), ("kinetoterapeuți", "8695"),
    ("fizioterapeut", "8695"), ("fizioterapeuti", "8695"), ("fizioterapia", "8695"),
    ("psiholog", "8693"), ("psychologist", "8693"),
])
def test_split_classes_answer_with_rev3_and_the_old_rev2_catch_all(words, rev3):
    assert _found(words)[:2] == [(rev3, 3), ("8690", 2)]
    assert caen.codes_for(words) == {rev3, "8690"}


@pytest.mark.parametrize("words", ["ortodont", "ortodonție", "orthodontist",
                                   "cabinet stomatologic", "Clinică stomatologică",
                                   "clinica stomatologica", "dental clinic",
                                   "stomatologie clinica", "clinic dentist",
                                   "cabinet medical stomatologic"])
def test_dental_words_win_over_generic_clinic_words(words):
    assert caen.codes_for(words) == {"8623"}
    assert _found(words)[:2] == [("8623", 3), ("8623", 2)]


def test_cabinet_medical_is_general_and_specialist_practice():
    assert caen.codes_for("cabinet medical") == {"8621", "8622"}
    assert caen.codes_for("Cabinet Medical") == {"8621", "8622"}


def test_a_generic_place_word_alone_still_counts():
    assert caen.codes_for("clinica") == {"8622"}


@pytest.mark.parametrize("typed, code", [("dentsit", "8623"), ("farmacei", "4773"),
                                         ("phsyio", "8695")])
def test_one_swapped_pair_in_a_long_word_is_forgiven(typed, code):
    assert code in caen.codes_for(typed)


def test_short_words_and_bigger_typos_are_not_guessed():
    assert caen.codes_for("tve") == set()                 # "vet", too short to guess
    assert "8623" not in caen.codes_for("dnetsit")        # two swaps


def test_vet_is_offered_but_labelled_as_not_human_health():
    m = caen.search("vet")[0]
    assert m.code == "7500" and "not human health" in m.name_en


# ── Accents, inflection and names ──


@pytest.mark.parametrize("words", ["stomatologică", "stomatologica", "STOMATOLOGICĂ",
                                   "asistență stomatologică", "asistenţă stomatologică"])
def test_accents_cedillas_and_inflection_are_ignored(words):
    assert caen.search(words)[0][:2] == ("8623", 3)
    assert caen.codes_for(words) == {"8623"}


def test_name_search_needs_every_word():
    found = _found("asistență medicală")
    assert set(found) == {("8621", 3), ("8621", 2), ("8622", 3), ("8622", 2)}


def test_name_search_does_not_confuse_similar_words():
    # "restaurare" (restoration) shares eight letters with "restaurant"
    assert caen.codes_for("restaurant") == {"5610", "5611"}


def test_a_code_finds_itself():
    assert _found("8695") == [("8695", 3)]


def test_results_carry_names_and_a_reason():
    m = caen.search("dentist")[0]
    assert m.name_ro == "Activități de asistență stomatologică"
    assert m.name_en == "Dental practice"
    assert m.why == 'synonym "dentist"'
    other = caen.search("restaurant")[0]
    assert other.name_en is None and other.why == 'name matches "restaurant"'


def test_search_is_deterministic():
    assert caen.search("instrumente optice") == caen.search("instrumente optice")


@pytest.mark.parametrize("words", ["", "   ", "zzzqx", "de si cu"])
def test_unknown_words_find_nothing(words):
    assert caen.search(words) == []
    assert caen.codes_for(words) == set()


def test_search_never_returns_pre_2008_codes():
    # 8513 was dental before 2008; today it is secondary education.
    assert all(m.version in (2, 3) for m in caen.search("stomatologic"))


# ── describe() ──


def test_describe_prefers_english_where_harvey_has_it():
    assert caen.describe("8623") == "Dental practice"
    assert caen.describe(8695, "Rev.3") == "Physiotherapy"
    assert caen.describe("86.90", 2).startswith("Other human health activities")


def test_describe_falls_back_to_romanian_and_old_versions():
    assert caen.describe("5610", 2).startswith("Restaurante")
    assert caen.describe("111", 2) == caen._classes()[("0111", 2)]
    assert caen.describe("8513", 1) == "Activități de asistență stomatologică"


def test_describe_reads_an_activity_code_value():
    # ACTIVITY_CODE observations store "code/revision".
    assert caen.describe("8623/3") == "Dental practice"
    assert caen.describe("8690/2").startswith("Other human health activities")
    assert caen.describe("8690/3") is None


def test_describe_unknown_code_or_version():
    assert caen.describe("9999") is None
    assert caen.describe("8695", 2) is None
    assert caen.describe("") is None


# ── CLI ──


def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["harvey", "caen", *argv])
    cli.main()
    return capsys.readouterr().out


def test_cli_prints_code_revision_and_name(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, "kinetoterapie")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "8695  Rev.3  Activități de fizioterapie (Physiotherapy)"
    assert lines[1] == 'synonym "kinetoterapie"'
    assert lines[2].startswith("8690  Rev.2  Alte activități referitoare la sănătatea umană")


def test_cli_joins_words_and_limits(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, "medic", "de", "familie", "--limit", "1")
    assert "8621  Rev.3" in out and "Rev.2" not in out


def test_cli_says_when_nothing_matches(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, "zzzqx")
    assert 'No CAEN class matches "zzzqx"' in out


# ── Dashboard ──


@pytest.fixture
def authed():
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c


def test_endpoint_returns_matches_and_codes(authed):
    r = authed.get("/api/caen/search", params={"q": "laborator analize"})
    assert r.status_code == 200
    body = r.json()
    assert body["codes"] == ["8690", "8691"]
    first = body["matches"][0]
    assert first == {"code": "8691", "version": 3, "revision": "Rev.3",
                     "name_ro": "Servicii de diagnostic imagistic și activități "
                                "ale laboratoarelor medicale",
                     "name_en": "Diagnostic imaging and medical laboratories",
                     "why": 'synonym "laborator"'}


def test_endpoint_with_no_query_is_empty(authed):
    assert authed.get("/api/caen/search").json() == {"query": "", "matches": [], "codes": []}


def test_endpoint_needs_the_token():
    with TestClient(dash.app, base_url=ORIGIN) as c:
        assert c.get("/api/caen/search", params={"q": "dentist"}).status_code == 401
