"""Find, read and check every module manifest. Imports no module code.

Two places hold manifests:

    harvey/modules/<set>/<id>/module.toml   modules that ship with Harvey;
                                            only these may name Python code
    data/modules/<id>/module.toml           data-only packs a user adds
                                            (a market or a language): words
                                            and signals only, no ``entry``,
                                            .env keys, capabilities, lanes or
                                            settings, never part of core, and
                                            no code file anywhere in the
                                            folder

A set is a folder under harvey/modules (core first, the rest by name), so a
country's modules arrive as one new folder.

Every problem found is reported at once, one line each, naming the file, so
a bad manifest is fixed in one pass instead of one error at a time.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from harvey.paths import PROJECT_ROOT

API_VERSION = 1
REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "modules"
CORE_SET = "core"
KINDS = ("source", "search", "enricher", "market", "gazetteer", "language", "action", "ui")
# What a user-added pack may be: data a core matcher reads, never behaviour.
DATA_KINDS = ("market", "language")
REQUIRED = ("id", "set", "kind", "version", "api")
# The manifest keys a data pack may use: everything else either names code,
# reads a key, or claims what another module does.
PACK_KEYS = {*REQUIRED, "name", "description", "markets", "requires",
             "default_enabled", "terms_note", "cost", "signals"}
CAPABILITY_OPS = ("query", "lookup", "read", "sync", "expand", "judge", "action")
SETTING_TYPES = ("bool", "int", "float", "str", "list")
ABSENCE_VALUES = ("unknown", "false")
# What a signal is about, how many current values it has, what its rows are
# told apart by, and when in a search it can be known (from the source
# itself, from a search, by reading the company, or by a model's judgement).
SUBJECTS = ("company", "person")
CARDINALITIES = ("one", "many")
KEYED_BY = ("", "phrase")
WHEN_KNOWN = ("source", "search", "enrich", "judged")
SIGNAL_DEFAULTS = {"subject": "company", "unit": "", "cardinality": "one",
                   "keyed_by": "", "absence": "unknown", "when_known": "enrich"}
SIGNAL_TEXT = ("label", "category", "collector", "cost_note", "description", "unit")
VALUE_TYPES = ("num", "text", "bool")
# Discovery keys a capability claims: one module each, so a switch always
# names the module that really runs the source or preset.
OWNED_KEYS = ("provider", "preset")
CODE_FILES = (".py", ".pyc", ".pyo", ".pyw", ".pyz", ".pyd", ".js", ".mjs",
              ".cjs", ".ts", ".so", ".dll", ".dylib", ".wasm", ".html", ".htm",
              ".sh", ".exe")

_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENTRY = re.compile(r"^harvey(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ManifestError(ValueError):
    """One or more manifests are wrong. ``problems`` holds one line each."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass(frozen=True)
class Manifest:
    id: str
    set: str
    kind: str
    version: str
    path: Path
    data_only: bool = False
    name: str = ""
    description: str = ""
    api: int = API_VERSION
    markets: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    default_enabled: bool = False
    entry: str = ""
    # The existing files the module's capabilities run (dotted paths): what
    # the contract tests hold to the module rules.
    wraps: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    terms_note: str = ""
    cost: dict = field(default_factory=dict)
    lanes: tuple[dict, ...] = ()
    settings: dict = field(default_factory=dict)
    signals: tuple[dict, ...] = ()
    capabilities: tuple[dict, ...] = ()

    def summary(self) -> dict:
        """The row ``harvey modules list`` prints."""
        return {
            "id": self.id, "name": self.name, "set": self.set, "kind": self.kind,
            "version": self.version, "signals": len(self.signals),
            "default_enabled": self.default_enabled,
            "data_only": self.data_only, "path": str(self.path),
        }


def _str_list(value, name: str, where: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where}: {name} must be a list of strings")
        return ()
    return tuple(value)


def _check_signals(raw, where: str, problems: list[str]) -> tuple[dict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(f"{where}: [[signals]] must be an array of tables")
        return ()
    out, seen = [], set()
    for i, sig in enumerate(raw):
        code = sig.get("code") if isinstance(sig, dict) else None
        if not isinstance(code, str) or not _CODE.match(code):
            problems.append(f"{where}: signal #{i + 1} needs a code in capitals "
                            f"(A-Z, 0-9, _), got {code!r}")
            continue
        if code in seen:
            problems.append(f"{where}: signal {code} is declared twice")
            continue
        seen.add(code)
        for name, allowed in (("absence", ABSENCE_VALUES), ("subject", SUBJECTS),
                              ("cardinality", CARDINALITIES), ("keyed_by", KEYED_BY),
                              ("when_known", WHEN_KNOWN)):
            value = sig.get(name, SIGNAL_DEFAULTS[name])
            if value not in allowed:
                problems.append(f"{where}: signal {code} has {name} {value!r}; "
                                f"it must be one of {', '.join(repr(a) for a in allowed)}")
        for name in SIGNAL_TEXT:
            if name in sig and not isinstance(sig[name], str):
                problems.append(f"{where}: signal {code} {name} must be a string")
        if sig.get("value_type", "text") not in VALUE_TYPES:
            problems.append(f"{where}: signal {code} has value_type "
                            f"{sig.get('value_type')!r}; it must be one of "
                            f"{', '.join(VALUE_TYPES)}")
        floor = sig.get("confidence_floor", 0.0)
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) \
                or not 0 <= floor <= 1:
            problems.append(f"{where}: signal {code} confidence_floor must be a "
                            f"number from 0 to 1, got {floor!r}")
        out.append({**SIGNAL_DEFAULTS, **sig})
    return tuple(out)


def _check_capabilities(raw, where: str, problems: list[str]) -> tuple[dict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(f"{where}: [[capabilities]] must be an array of tables")
        return ()
    out, seen = [], set()
    for i, cap in enumerate(raw):
        cid = cap.get("id") if isinstance(cap, dict) else None
        if not isinstance(cid, str) or not _ID.match(cid):
            problems.append(f"{where}: capability #{i + 1} needs an id "
                            f"(lower case, digits, _), got {cid!r}")
            continue
        if cid in seen:
            problems.append(f"{where}: capability {cid} is declared twice")
            continue
        seen.add(cid)
        if cap.get("op") not in CAPABILITY_OPS:
            problems.append(f"{where}: capability {cid} has op {cap.get('op')!r}; "
                            f"it must be one of {', '.join(CAPABILITY_OPS)}")
        out.append(dict(cap))
    return tuple(out)


def _check_settings(raw, where: str, problems: list[str]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f"{where}: [settings] must be a table")
        return {}
    for key, spec in raw.items():
        kind = spec.get("type") if isinstance(spec, dict) else None
        if kind not in SETTING_TYPES:
            problems.append(f"{where}: setting {key} needs a type, one of "
                            f"{', '.join(SETTING_TYPES)}; got {kind!r}")
    return dict(raw)


def _check_cost(raw, where: str, problems: list[str]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f"{where}: [cost] must be a table")
        return {}
    as_of = raw.get("as_of")
    try:
        date.fromisoformat(as_of.isoformat() if isinstance(as_of, date) else str(as_of))
    except ValueError:
        problems.append(f"{where}: [cost] needs as_of, the date its prices were "
                        f"checked (YYYY-MM-DD); got {as_of!r}")
    return dict(raw)


def _check_lanes(raw, where: str, problems: list[str]) -> tuple[dict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
            isinstance(lane, dict) and isinstance(lane.get("id"), str) for lane in raw):
        problems.append(f"{where}: every [[lanes]] entry needs an id")
        return ()
    return tuple(dict(lane) for lane in raw)


def _code_files(folder: Path) -> list[str]:
    """Files a data pack must not hold: code, and links (which can point
    anywhere, code included)."""
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                  if p.is_symlink() or (p.is_file() and p.suffix.lower() in CODE_FILES))


def _regex_keys(value, path: str = "") -> list[str]:
    """Keys anywhere in a pack that would hand Harvey a pattern to run."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if "regex" in str(k).lower() or "pattern" in str(k).lower():
                found.append(here)
            found += _regex_keys(v, here)
    elif isinstance(value, list):
        for v in value:
            found += _regex_keys(v, path)
    return found


def repo_sets(repo_dir: Path = REPO_DIR) -> tuple[str, ...]:
    """The sets Harvey ships: one folder each under harvey/modules, core
    first, then by name."""
    folder = Path(repo_dir)
    names = sorted(p.name for p in folder.iterdir()
                   if p.is_dir() and _ID.match(p.name)) if folder.is_dir() else []
    return tuple(sorted(names, key=lambda n: (n != CORE_SET, n)))


def read_manifest(path: Path, *, data_only: bool = False,
                  problems: list[str] | None = None) -> Manifest | None:
    """One manifest, checked on its own. Problems are appended to
    ``problems`` (raised as a ManifestError when none is given); returns
    None when the file can't make a manifest at all."""
    own = problems is None
    problems = [] if own else problems
    before = len(problems)
    where = str(path)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        problems.append(f"{where}: can't be read as TOML: {e}")
        raw = None
    if raw is not None:
        manifest = _build(raw, path, data_only, where, problems)
    else:
        manifest = None
    if own and len(problems) > before:
        raise ManifestError(problems)
    return manifest if len(problems) == before else None


def _build(raw: dict, path: Path, data_only: bool, where: str,
           problems: list[str]) -> Manifest | None:
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)}")
        return None
    mid, mset, kind, version, api = (raw[k] for k in REQUIRED)
    if not isinstance(mid, str) or not _ID.match(mid):
        problems.append(f"{where}: id {mid!r} must be lower case letters, digits and _")
    elif mid != path.parent.name:
        problems.append(f"{where}: id {mid!r} must match its folder name "
                        f"{path.parent.name!r}")
    if not isinstance(mset, str) or not _ID.match(mset):
        problems.append(f"{where}: set {mset!r} must be lower case letters, digits and _")
    elif data_only and mset == CORE_SET:
        problems.append(f"{where}: a pack in data/modules can't be part of "
                        f"Harvey's core (set = {mset!r})")
    elif not data_only and mset != path.parent.parent.name:
        problems.append(f"{where}: set {mset!r} must match the folder it is in "
                        f"({path.parent.parent.name!r})")
    if kind not in KINDS:
        problems.append(f"{where}: unknown kind {kind!r}; it must be one of "
                        f"{', '.join(KINDS)}")
    elif data_only and kind not in DATA_KINDS:
        problems.append(f"{where}: a pack in data/modules can only be a "
                        f"{' or a '.join(DATA_KINDS)}, not a {kind}")
    if not isinstance(version, str) or not _VERSION.match(version):
        problems.append(f"{where}: version {version!r} must look like 1.2.3")
    if type(api) is not int or api != API_VERSION:
        problems.append(f"{where}: api = {api!r}, but this Harvey reads "
                        f"api = {API_VERSION} only")

    entry = raw.get("entry", "")
    if data_only and entry:
        problems.append(f"{where}: a pack in data/modules can't name code "
                        f"(entry = {entry!r}); only Harvey's own modules can")
    elif entry and (not isinstance(entry, str) or not _ENTRY.match(entry)):
        problems.append(f"{where}: entry {entry!r} must be a dotted path inside "
                        f"the harvey package (harvey.something)")
    wraps = _str_list(raw.get("wraps"), "wraps", where, problems)
    for path in wraps:
        if not _ENTRY.match(path):
            problems.append(f"{where}: wraps {path!r} must be a dotted path inside "
                            f"the harvey package (harvey.something)")
    if data_only:
        code = _code_files(path.parent)
        if code:
            problems.append(f"{where}: a pack in data/modules holds data only; "
                            f"remove {', '.join(code)}")
        extra = sorted(k for k in raw if k not in PACK_KEYS and k != "entry")
        if extra:
            problems.append(f"{where}: a pack in data/modules holds words and "
                            f"signals only; remove {', '.join(extra)}")
        regex = _regex_keys(raw)
        if regex:
            problems.append(f"{where}: a pack in data/modules can't carry patterns; "
                            f"remove {', '.join(regex)}")
    default = raw.get("default_enabled", False)
    if not isinstance(default, bool):
        problems.append(f"{where}: default_enabled must be true or false")
    terms = raw.get("terms_note", "")
    if not isinstance(terms, str):
        problems.append(f"{where}: terms_note must be a string")
    texts = {k: raw.get(k, "") for k in ("name", "description")}
    for k, v in texts.items():
        if not isinstance(v, str):
            problems.append(f"{where}: {k} must be a string")

    return Manifest(
        id=mid, set=mset, kind=kind, version=version, path=path,
        data_only=data_only, api=api,
        name=texts["name"] if isinstance(texts["name"], str) else "",
        description=texts["description"] if isinstance(texts["description"], str) else "",
        markets=_str_list(raw.get("markets"), "markets", where, problems),
        requires=_str_list(raw.get("requires"), "requires", where, problems),
        default_enabled=default is True,
        entry=entry if isinstance(entry, str) else "",
        wraps=wraps,
        secrets=_str_list(raw.get("secrets"), "secrets", where, problems),
        terms_note=terms if isinstance(terms, str) else "",
        cost=_check_cost(raw.get("cost"), where, problems),
        lanes=_check_lanes(raw.get("lanes"), where, problems),
        settings=_check_settings(raw.get("settings"), where, problems),
        signals=_check_signals(raw.get("signals"), where, problems),
        capabilities=_check_capabilities(raw.get("capabilities"), where, problems),
    )


def manifest_paths(repo_dir: Path = REPO_DIR,
                   data_dir: Path | None = DATA_DIR) -> list[tuple[Path, bool]]:
    """Every manifest on disk, with whether it is a data-only pack."""
    found = [(p, False) for p in sorted(Path(repo_dir).glob("*/*/module.toml"))]
    if data_dir is not None and Path(data_dir).is_dir():
        found += [(p, True) for p in sorted(Path(data_dir).glob("*/module.toml"))]
    return found


def load_manifests(repo_dir: Path = REPO_DIR,
                   data_dir: Path | None = DATA_DIR) -> list[Manifest]:
    """Every manifest, checked one by one and against each other: ids are
    unique, ``requires`` names a module that exists, each signal code is
    declared by one module only, and each discovery provider or preset is
    claimed by one module only. Raises ManifestError listing every problem.
    """
    problems: list[str] = []
    manifests = [m for path, data_only in manifest_paths(repo_dir, data_dir)
                 if (m := read_manifest(path, data_only=data_only, problems=problems))]
    problems += cross_check(manifests)
    if problems:
        raise ManifestError(problems)
    return sort_manifests(manifests, repo_dir)


def sort_manifests(manifests, repo_dir: Path = REPO_DIR) -> list[Manifest]:
    """Shipped sets in folder order (core first), then packs' own sets."""
    order = repo_sets(repo_dir)
    return sorted(manifests, key=lambda m: (m.set not in order,
                                            order.index(m.set) if m.set in order else 0,
                                            m.set, m.id))


def cross_check(manifests) -> list[str]:
    """What is wrong between manifests that each read fine on their own."""
    problems: list[str] = []

    by_id: dict[str, Manifest] = {}
    for m in manifests:
        if m.id in by_id:
            problems.append(f"{m.path}: id {m.id!r} is already used by {by_id[m.id].path}")
        else:
            by_id[m.id] = m
    owner: dict[str, Manifest] = {}
    for m in manifests:
        for need in m.requires:
            if need not in by_id:
                problems.append(f"{m.path}: requires {need!r}, which no module is")
            elif need == m.id:
                problems.append(f"{m.path}: requires itself")
        for sig in m.signals:
            code = sig["code"]
            if code in owner and owner[code] is not m:
                problems.append(f"{m.path}: signal {code} is already declared by "
                                f"{owner[code].id} ({owner[code].path})")
            else:
                owner[code] = m
    claimed: dict[tuple[str, str], Manifest] = {}
    for m in manifests:
        for cap in m.capabilities:
            for field_ in OWNED_KEYS:
                key = cap.get(field_)
                if not key:
                    continue
                first = claimed.setdefault((field_, key), m)
                if first is not m:
                    problems.append(f"{m.path}: {field_} {key!r} is already claimed "
                                    f"by {first.id} ({first.path})")
    return problems
