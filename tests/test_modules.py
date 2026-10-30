"""Module manifests: the ones Harvey ships match today's code, and the loader
refuses a wrong one with a sentence that names the file and the problem."""

import contextlib
import importlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harvey.collectors.discover import PRESETS, PROVIDERS, RANK_CODES
from harvey.modules.loader import ManifestError, load_manifests
from harvey.signals import SIGNAL_CATALOG

ROOT = Path(__file__).resolve().parent.parent
CATALOG = {s["code"]: s for s in SIGNAL_CATALOG}


@pytest.fixture(scope="module")
def shipped():
    return load_manifests(data_dir=None)


def _declared(manifests):
    return [(m.id, sig) for m in manifests for sig in m.signals]


def test_every_catalog_signal_is_declared_by_exactly_one_module(shipped):
    owners: dict[str, list[str]] = {}
    for mid, sig in _declared(shipped):
        owners.setdefault(sig["code"], []).append(mid)
    assert set(CATALOG) <= set(owners)
    assert {code: ids for code, ids in owners.items() if len(ids) != 1} == {}
    # A signal only a module declares carries its own words.
    for _, sig in _declared(shipped):
        if sig["code"] not in CATALOG:
            assert sig.get("label") and sig.get("description") and sig.get("cost_note"), sig["code"]


@pytest.mark.parametrize("field,default", [
    ("label", None), ("category", None), ("value_type", None),
    ("collector", None), ("cost_note", None),
    ("confidence_floor", 0.0), ("absence", "unknown"),
])
def test_declared_signals_match_the_catalog(shipped, field, default):
    wrong = {sig["code"]: (sig.get(field, default), CATALOG[sig["code"]].get(field, default))
             for _, sig in _declared(shipped) if sig["code"] in CATALOG
             and sig.get(field, default) != CATALOG[sig["code"]].get(field, default)}
    assert wrong == {}


def test_rank_signals_are_keyed_by_the_search_phrase(shipped):
    keyed = {sig["code"] for _, sig in _declared(shipped) if sig.get("keyed_by") == "phrase"}
    assert keyed == set(RANK_CODES)


def test_every_discovery_provider_and_preset_belongs_to_one_module(shipped):
    caps = [(m.id, c) for m in shipped for c in m.capabilities]
    providers = [c["provider"] for _, c in caps if "provider" in c]
    presets = [c["preset"] for _, c in caps if "preset" in c]
    assert sorted(providers) == sorted(PROVIDERS)
    assert sorted(presets) == sorted(PRESETS)


def _resolve(dotted: str):
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except ModuleNotFoundError:
            continue
        for name in parts[cut:]:
            obj = getattr(obj, name)
        return obj
    raise ModuleNotFoundError(dotted)


def test_every_entry_points_at_existing_code(shipped):
    for m in shipped:
        if m.entry:
            assert _resolve(m.entry) is not None, m.id


def test_secrets_are_env_keys_harvey_documents(shipped):
    documented = (ROOT / ".env.example").read_text()
    for m in shipped:
        for key in m.secrets:
            assert f"\n{key}=" in documented, (m.id, key)


def test_shipped_sets_and_folders():
    sets = {m.set for m in load_manifests(data_dir=None)}
    assert sets == {"core", "original", "ro"}


def test_listing_imports_no_module_code():
    code = ("import sys; from harvey.modules.loader import load_manifests; "
            "load_manifests(data_dir=None); "
            "print(sorted(m for m in sys.modules if m.startswith('harvey')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    assert json.loads(out.replace("'", '"')) == [
        "harvey", "harvey.modules", "harvey.modules.loader", "harvey.paths"]


def test_cli_lists_modules_as_json(tmp_path, monkeypatch):
    import harvey.state
    from harvey.cli import main

    monkeypatch.setattr(harvey.state, "DB_PATH", tmp_path / "harvey.db")

    out = io.StringIO()
    argv = sys.argv
    sys.argv = ["harvey", "modules", "list", "--json"]
    try:
        with contextlib.redirect_stdout(out):
            main()
    finally:
        sys.argv = argv
    rows = json.loads(out.getvalue())
    by_id = {r["id"]: r for r in rows}
    assert by_id["onrc"] == {**by_id["onrc"], "set": "ro", "kind": "source", "signals": 1}
    declared = sum(len(m.signals) for m in load_manifests(data_dir=None))
    assert sum(r["signals"] for r in rows) == declared >= len(SIGNAL_CATALOG)
    assert by_id["mf_financials"]["signals"] == 9
    assert by_id["linkedin"]["default_enabled"] is False


# ── the loader on fictional manifests ──

GOOD = """
id = "{id}"
set = "{set}"
kind = "search"
version = "1.0.0"
api = 1
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel / "module.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _problems(repo: Path, data: Path | None = None) -> str:
    with pytest.raises(ManifestError) as err:
        load_manifests(repo_dir=repo, data_dir=data)
    return str(err.value)


def test_a_good_manifest_loads(tmp_path):
    _write(tmp_path, "core/demo", GOOD.format(id="demo", set="core") + """
markets = ["XX"]
entry = "harvey.collectors.discover"
[cost]
as_of = 2026-01-01
usd = 0.0
[[signals]]
code = "DEMO_SIGNAL"
[[capabilities]]
id = "demo_search"
op = "query"
""")
    (m,) = load_manifests(repo_dir=tmp_path, data_dir=None)
    assert (m.id, m.set, m.markets, m.signals[0]["code"]) == ("demo", "core", ("XX",), "DEMO_SIGNAL")
    assert m.default_enabled is False


@pytest.mark.parametrize("extra,expected", [
    ('kind = "teleporter"', "unknown kind 'teleporter'"),
    ("api = 2", "api = 2, but this Harvey reads api = 1 only"),
    ('requires = ["nowhere"]', "requires 'nowhere', which no module is"),
    ('entry = "os.system"', "entry 'os.system' must be a dotted path inside the harvey package"),
    ('entry = "harveyx.thing"', "must be a dotted path inside the harvey package"),
    ('version = "one"', "version 'one' must look like 1.2.3"),
    ("default_enabled = \"yes\"", "default_enabled must be true or false"),
    ("[cost]\nusd = 1.0", "[cost] needs as_of"),
    ("[[signals]]\ncode = \"lower\"", "needs a code in capitals"),
    ("[[signals]]\ncode = \"X_A\"\nabsence = \"maybe\"", "absence 'maybe'"),
    ("[[capabilities]]\nid = \"c\"\nop = \"fly\"", "op 'fly'"),
    ("[settings.x]\ndefault = 1", "setting x needs a type"),
])
def test_bad_fields_are_refused_with_a_clear_reason(tmp_path, extra, expected):
    base = GOOD.format(id="demo", set="core")
    key = extra.split("=")[0].strip()
    if key in ("kind", "api", "version"):
        base = "\n".join(line for line in base.splitlines() if not line.startswith(key))
    path = _write(tmp_path, "core/demo", base + "\n" + extra + "\n")
    problems = _problems(tmp_path)
    assert expected in problems
    assert str(path) in problems


def test_missing_required_fields_are_named(tmp_path):
    _write(tmp_path, "core/demo", 'id = "demo"\nset = "core"\n')
    assert "missing kind, version, api" in _problems(tmp_path)


def test_unreadable_toml_is_reported(tmp_path):
    _write(tmp_path, "core/demo", 'id = "demo\n')
    assert "can't be read as TOML" in _problems(tmp_path)


def test_id_and_set_must_match_their_folders(tmp_path):
    _write(tmp_path, "core/demo", GOOD.format(id="other", set="original"))
    problems = _problems(tmp_path)
    assert "id 'other' must match its folder name 'demo'" in problems
    assert "set 'original' must match the folder it is in ('core')" in problems


def test_duplicate_ids_are_refused(tmp_path):
    repo, data = tmp_path / "repo", tmp_path / "data"
    _write(repo, "core/demo", GOOD.format(id="demo", set="core"))
    _write(data, "demo", GOOD.format(id="demo", set="shared").replace("search", "market"))
    assert "id 'demo' is already used by" in _problems(repo, data)


def test_a_signal_declared_by_two_modules_is_refused(tmp_path):
    sig = '\n[[signals]]\ncode = "SHARED_CODE"\n'
    _write(tmp_path, "core/one", GOOD.format(id="one", set="core") + sig)
    _write(tmp_path, "core/two", GOOD.format(id="two", set="core") + sig)
    assert "signal SHARED_CODE is already declared by one" in _problems(tmp_path)


def test_a_signal_declared_twice_in_one_manifest_is_refused(tmp_path):
    sig = '\n[[signals]]\ncode = "TWICE"\n'
    _write(tmp_path, "core/one", GOOD.format(id="one", set="core") + sig + sig)
    assert "signal TWICE is declared twice" in _problems(tmp_path)


def test_every_problem_is_reported_at_once(tmp_path):
    _write(tmp_path, "core/one", GOOD.format(id="one", set="core").replace("api = 1", "api = 3"))
    _write(tmp_path, "core/two", GOOD.format(id="two", set="core").replace("search", "nope"))
    problems = _problems(tmp_path)
    assert "api = 3" in problems and "unknown kind 'nope'" in problems


# ── data-only packs in data/modules ──

PACK = """
id = "market_xx"
set = "shared"
kind = "market"
version = "1.0.0"
api = 1
markets = ["XX"]
"""


def test_a_data_pack_loads_and_says_so(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK)
    (data / "market_xx" / "regions.csv").write_text("code,name\nA1,Example Region\n")
    (m,) = load_manifests(repo_dir=tmp_path / "repo", data_dir=data)
    assert m.data_only and m.id == "market_xx"


@pytest.mark.parametrize("name", ["hook.py", "nested/panel.js"])
def test_a_data_pack_with_code_is_refused(tmp_path, name):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK)
    target = data / "market_xx" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('hello')\n")
    problems = _problems(tmp_path / "repo", data)
    assert "holds data only; remove" in problems and Path(name).name in problems


def test_a_data_pack_naming_an_entry_is_refused(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK + 'entry = "harvey.cli"\n')
    assert "can't name code (entry = 'harvey.cli')" in _problems(tmp_path / "repo", data)


def test_a_data_pack_must_be_a_market_or_a_language(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK.replace('"market"', '"search"'))
    assert "can only be a market or a language, not a search" in _problems(tmp_path / "repo", data)


def test_a_pack_may_require_a_shipped_module(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK + 'requires = ["website"]\n')
    ids = {m.id for m in load_manifests(data_dir=data)}
    assert {"market_xx", "website"} <= ids


# ── what a data pack may hold, and what manifests may claim ──


def test_a_data_pack_cant_be_part_of_core(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK.replace('set = "shared"', 'set = "core"'))
    assert "can't be part of Harvey's core" in _problems(tmp_path / "repo", data)


@pytest.mark.parametrize("extra,field", [
    ('secrets = ["BROWSER_CDP_FALLBACK_URL"]\n', "secrets"),
    ('[[capabilities]]\nid = "x"\nop = "query"\nprovider = "dataforseo_serp"\n', "capabilities"),
    ('[[lanes]]\nid = "x"\n', "lanes"),
    ('[settings.x]\ntype = "int"\n', "settings"),
])
def test_a_data_pack_holds_words_and_signals_only(tmp_path, extra, field):
    """A pack can't widen Settings' .env keys, claim a shipped discovery
    source, or bring back another module's signals through a capability."""
    data = tmp_path / "data"
    _write(data, "market_xx", PACK + extra)
    assert f"holds words and signals only; remove {field}" in _problems(tmp_path / "repo", data)


@pytest.mark.parametrize("name", ["hook.pyw", "bundle.pyz", "panel.cjs", "mod.wasm", "page.html"])
def test_more_kinds_of_code_file_are_refused_in_a_pack(tmp_path, name):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK)
    (data / "market_xx" / name).write_text("x\n")
    assert f"holds data only; remove {name}" in _problems(tmp_path / "repo", data)


def test_a_link_in_a_pack_is_refused(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK)
    (tmp_path / "elsewhere.csv").write_text("code,name\n")
    (data / "market_xx" / "regions.csv").symlink_to(tmp_path / "elsewhere.csv")
    assert "holds data only; remove regions.csv" in _problems(tmp_path / "repo", data)


def test_a_pattern_in_a_pack_is_refused(tmp_path):
    data = tmp_path / "data"
    _write(data, "market_xx", PACK + '[[signals]]\ncode = "X_ONE"\nregex = "(a+)+$"\n')
    assert "can't carry patterns; remove signals.regex" in _problems(tmp_path / "repo", data)


def test_two_modules_cant_claim_one_discovery_source(tmp_path):
    cap = '[[capabilities]]\nid = "find"\nop = "query"\nprovider = "same_source"\n'
    _write(tmp_path, "core/one", GOOD.format(id="one", set="core") + cap)
    _write(tmp_path, "original/two", GOOD.format(id="two", set="original") + cap)
    assert "provider 'same_source' is already claimed by one" in _problems(tmp_path)


@pytest.mark.parametrize("signal,expected", [
    ('label = { en = "Net turnover", ro = "Cifra de afaceri" }', "X_ONE label must be a string"),
    ('confidence_floor = "high"', "X_ONE confidence_floor must be a number from 0 to 1"),
    ("confidence_floor = 2", "X_ONE confidence_floor must be a number from 0 to 1"),
    ('value_type = "money"', "X_ONE has value_type 'money'"),
])
def test_signal_fields_are_checked_before_they_reach_the_vocabulary(tmp_path, signal, expected):
    _write(tmp_path, "core/demo", GOOD.format(id="demo", set="core")
           + f'[[signals]]\ncode = "X_ONE"\n{signal}\n')
    assert expected in _problems(tmp_path)


def test_api_must_be_a_whole_number(tmp_path):
    _write(tmp_path, "core/demo", GOOD.format(id="demo", set="core").replace("api = 1", "api = 1.0"))
    assert "api = 1.0, but this Harvey reads api = 1 only" in _problems(tmp_path)


def test_a_new_set_of_modules_is_one_folder(tmp_path):
    """A country's modules arrive as a folder of their own: no list of sets
    in the code to extend. Core sorts first, the rest by name."""
    _write(tmp_path, "fr/market_fr", GOOD.format(id="market_fr", set="fr"))
    _write(tmp_path, "core/demo", GOOD.format(id="demo", set="core"))
    _write(tmp_path, "original/osm_x", GOOD.format(id="osm_x", set="original"))
    assert [m.set for m in load_manifests(repo_dir=tmp_path, data_dir=None)] == [
        "core", "fr", "original"]
