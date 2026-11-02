"""The database upgrade: a schema 24 harvey.db, with the files beside it, to
the slim schema 25 (harvey.db holds Harvey's own work; the registers are
read in place).

Stages, in order, each resumable:

- ``registry``, ``financials``, ``taxpayers``: each register's derived
  tables (its canonical row per tax code, name keys, current figures and
  flags) built into its own file, the trade registry first (the
  financials' ``filed`` reads it). A register that isn't loaded is skipped.
- ``anaf``: anaf.db rewritten compactly into ``anaf.db.new``
  (anaf/store.py: every body is decompressed again and compared before
  its chunk commits). Skipped when there is none, or it is compact already.
- ``harvey``: harvey.db migrated into ``harvey.db.v25`` (overlay/migrate.py).
- ``verify``: the new file checked against the old one (overlay/verify.py)
  and every ANAF answer hashed on both sides (anaf/store.verify).
- ``switch``: the new files take the live names, the old ones are kept as
  ``harvey.db.v24`` and ``anaf.db.old``. Refused when verification failed,
  or when harvey.db was written to after it was migrated (the migration
  and verification then run again). harvey.db is never missing: a hard
  link keeps the old file, then one rename puts the new one over it (two
  renames where links can't be made: a switch cut between them is
  finished on the next run, and nothing creates a database meanwhile).
  Nothing is deleted: delete_old() does that when the user asks.

The upgrade never changes the data in the schema 24 harvey.db or the old
anaf.db (the switch only checkpoints their write-ahead logs). With
an output folder (``out``) it writes nothing in the data folder at all
(derived tables go into ``<register>_derived.db`` files there, sources are
read as unchanging) and stops before the switch: a dry run on a copy.

Its progress is kept in ``upgrade.json`` (in the output folder: the data
folder by default), written as it goes, so the dashboard and ``harvey
upgrade`` show the same thing. One upgrade at a time: the running one holds
an OS lock on ``upgrade.lock``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.upgrade")

DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = "upgrade.json"
LOCK_FILE = "upgrade.lock"
NEW_DB = "harvey.db.v25"
OLD_DB = "harvey.db.v24"
NEW_ANAF = "anaf.db.new"
OLD_ANAF = "anaf.db.old"
SIDECARS = ("", "-wal", "-shm", "-journal")

STAGES = ("registry", "financials", "taxpayers", "anaf", "harvey", "verify", "switch")
# The register modules whose derived tables a stage builds, by stage.
DERIVED = {"registry": "onrc", "financials": "mf_financials", "taxpayers": "mf_taxpayers"}
ANAF_MODULE = "anaf"
VERIFY_SECTIONS = ("doctor", "counts", "stored", "registers", "filters")

# Seconds per GB of each source, measured on a 16.4 GB harvey.db, an
# 8.4 GB anaf.db and the full registers (36.6 minutes in all): an estimate
# shown before starting. ANAF's counts its rewrite and its hash check.
SECONDS_PER_GB = {"harvey": 22, "anaf": 108, "verify": 50}
DERIVED_SECONDS = 110
# What the new files take, as a share of what they are made from (the same
# install: 1.15 GB, 1.24 GB and 0.44 GB of derived tables).
NEW_SHARE = {"harvey": 0.07, "anaf": 0.15, "derived": 0.18}


class Refused(RuntimeError):
    """The upgrade won't start, go on, or switch: says why."""


@dataclass
class Layout:
    """Where the upgrade reads and writes. ``out`` None: in place (the
    new files beside the old ones, then the switch)."""
    data: Path
    out: Path | None = None

    def __post_init__(self):
        self.data = Path(self.data)
        self.out = Path(self.out) if self.out else None

    @property
    def target(self) -> Path:
        return self.out or self.data

    @property
    def db(self) -> Path:
        return self.data / "harvey.db"

    @property
    def new_db(self) -> Path:
        return self.target / NEW_DB

    @property
    def new_anaf(self) -> Path:
        return self.target / NEW_ANAF

    @property
    def state_file(self) -> Path:
        return self.target / STATE_FILE

    @property
    def lock_file(self) -> Path:
        return self.target / LOCK_FILE

    @property
    def read_only(self) -> bool:
        """Sources read as unchanging files (an output folder elsewhere)."""
        return self.out is not None

    def derived_file(self, register_file: str) -> Path | None:
        """Where a register's derived tables go: its own file in place,
        ``<name>_derived.db`` in the output folder otherwise."""
        if self.out is None:
            return None
        return self.out / f"{Path(register_file).stem}_derived.db"


def _manifests():
    from harvey import registers

    return {m.id: m for m in registers.declared()}


def schema_of(path: Path, immutable: bool = False) -> int:
    """The schema version of a database file (0: none there)."""
    path = Path(path)
    if not path.is_file():
        return 0
    con = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro"
                          + ("&immutable=1" if immutable else ""), uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def needed(data: Path | None = None) -> bool:
    """Whether harvey.db in ``data`` waits for the upgrade: an older schema
    the upgrade reads, or a switch to finish."""
    return waiting(Path(data or DATA_DIR) / "harvey.db")


def _target() -> int:
    """The schema the upgrade writes (25 as released; Harvey takes it
    through the later steps when it opens it)."""
    from harvey.overlay import schema

    return schema.UPGRADE_TARGET


def _newer(found: int) -> bool:
    from harvey.overlay import schema

    return found > schema.LATEST


def waiting(db: Path) -> bool:
    """Whether the database at ``db`` waits for the upgrade (Harvey opens
    nothing on it, and creates nothing in its place, until then)."""
    return 0 < schema_of(db) < _target() or switch_pending(db)


def switch_pending(db: Path) -> bool:
    """Whether a switch was cut short beside ``db``: no live database while
    the migrated or the kept one is there, or the live one switched while
    the rewritten anaf.db still waits for its name."""
    db = Path(db)
    found = schema_of(db)
    if found == 0:
        return db.with_name(db.name + ".v25").exists() or db.with_name(db.name + ".v24").exists()
    # (Beside the live harvey.db only: an output folder's files aren't switched.)
    return found >= _target() and db.name == "harvey.db" and (db.parent / NEW_ANAF).is_file()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _files_size(path: Path) -> int:
    return sum(_size(Path(str(path) + s)) for s in SIDECARS)


# ── State ──

def _blank() -> dict:
    return {"stages": {s: {"state": "todo"} for s in STAGES}, "started_at": None,
            "finished_at": None, "error": None, "verification": None, "out": None}


def read_state(layout: Layout) -> dict:
    try:
        state = json.loads(layout.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    for s in STAGES:
        state.setdefault("stages", {}).setdefault(s, {"state": "todo"})
    return state


def _write_state(layout: Layout, state: dict) -> None:
    layout.target.mkdir(parents=True, exist_ok=True)
    part = layout.state_file.with_suffix(".json.part")
    part.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    os.replace(part, layout.state_file)


def running(layout: Layout) -> bool:
    """Whether an upgrade holds the lock now (probing never keeps it)."""
    from harvey import pidfile

    if not layout.lock_file.exists():
        return False
    return pidfile.loop_lock_held(layout.lock_file)


def old_files(layout: Layout) -> list[dict]:
    """The files the switch kept aside, with their sizes."""
    out = []
    for name in (OLD_DB, OLD_ANAF):
        for suffix in SIDECARS:
            path = layout.data / (name + suffix)
            if path.is_file():
                out.append({"name": path.name, "bytes": _size(path)})
    return out


def estimate(layout: Layout) -> dict:
    """Sizes of what the upgrade reads, the time and disk it should take,
    and free space where it writes (``free_error`` when that can't be
    known: on WSL in Docker, the Windows drive must be mounted)."""
    from harvey import disk
    from harvey.overlay.migrate import MIN_FREE_BYTES

    mans = _manifests()
    sizes = {"harvey": _files_size(layout.db)}
    for stage, mid in DERIVED.items():
        m = mans.get(mid)
        sizes[stage] = _size(layout.data / m.register["file"]) if m else 0
    anaf = mans.get(ANAF_MODULE)
    anaf_path = layout.data / anaf.register["file"] if anaf else None
    sizes["anaf"] = _size(anaf_path) if anaf_path and \
        _anaf_version(anaf_path, layout.read_only) == 1 else 0
    gb = {k: v / 1e9 for k, v in sizes.items()}
    seconds = (gb["harvey"] * (SECONDS_PER_GB["harvey"] + SECONDS_PER_GB["verify"])
               + gb["anaf"] * SECONDS_PER_GB["anaf"]
               + (DERIVED_SECONDS if any(sizes[s] for s in DERIVED) else 0))
    need = int(sizes["harvey"] * NEW_SHARE["harvey"] + sizes["anaf"] * NEW_SHARE["anaf"]
               + sum(sizes[s] for s in DERIVED) * NEW_SHARE["derived"])
    free, free_error = None, None
    try:
        free = disk.free_bytes(layout.target / NEW_DB)
    except disk.HostDriveUnknown as e:
        free_error = str(e)
    except OSError as e:
        free_error = str(e)
    return {"sizes": sizes, "seconds": int(seconds), "need_bytes": need,
            "floor_bytes": MIN_FREE_BYTES, "free_bytes": free, "free_error": free_error,
            "in_docker": Path("/.dockerenv").exists()}


def status(layout: Layout | None = None) -> dict:
    """Everything a screen shows: whether an upgrade is needed, running or
    done, each stage, the verification, the estimate and the old files."""
    layout = layout or Layout(DATA_DIR)
    schema = schema_of(layout.db, layout.read_only)
    state = read_state(layout)
    pending = not layout.out and switch_pending(layout.db)
    out = {"schema": schema, "needed": (0 < schema < _target() or pending) and not layout.out,
           "newer": _newer(schema),
           "switch_pending": pending,
           "running": running(layout), "stages": [{"id": s, **state["stages"][s]}
                                                   for s in STAGES],
           "error": state.get("error"), "verification": state.get("verification"),
           "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
           "old_files": old_files(layout)}
    out["switched"] = state["stages"]["switch"].get("state") == "done"
    if out["needed"] or layout.out:
        out["estimate"] = estimate(layout)
    return out


# ── Running ──

class _Progress:
    """The stage being run, its detail written to the state file at most
    once a second."""

    def __init__(self, layout: Layout, state: dict, say):
        self.layout, self.state, self.say = layout, state, say
        self._last = 0.0

    def stage(self, name: str) -> dict:
        return self.state["stages"][name]

    def begin(self, name: str) -> None:
        st = self.stage(name)
        st.update(state="running", detail=st.get("detail"), began=time.time())
        self.state["error"] = None
        self.save()
        self.say(f"{name}: started")

    def tick(self, name: str, **detail) -> None:
        self.stage(name)["progress"] = detail
        if time.monotonic() - self._last >= 1:
            self._last = time.monotonic()
            self.save()

    def end(self, name: str, state: str = "done", **detail) -> None:
        st = self.stage(name)
        began = st.pop("began", None)
        if began:
            st["seconds"] = round(st.get("seconds", 0) + time.time() - began, 1)
        st.pop("progress", None)
        st["state"] = state
        if detail:
            st["detail"] = detail
        self.save()
        self.say(f"{name}: {state}" + (f" ({st['seconds']} s)" if st.get("seconds") else ""))

    def save(self) -> None:
        _write_state(self.layout, self.state)


def _anaf_version(path: Path, immutable: bool = False) -> int:
    if not path.is_file():
        return 0
    from harvey.modules.ro.anaf import store

    con = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro"
                          + ("&immutable=1" if immutable else ""), uri=True)
    try:
        return store.version(con)
    finally:
        con.close()


def _adapters(layout: Layout, anaf_path: Path | None):
    """The register adapters the migration and verification read through:
    derived tables from the output folder when there is one, ANAF from its
    rewritten file."""
    from harvey import registers

    out = []
    for mid, m in _manifests().items():
        derived = layout.derived_file(m.register["file"]) if mid in DERIVED.values() else None
        a = registers.adapter_for(m, layout.data,
                                  derived=f"{m.register['attach']}x" if derived else "")
        if derived is not None:
            a.derived_path = derived
        if mid == ANAF_MODULE and anaf_path is not None:
            a.path = anaf_path
        if a.path is not None and a.path.is_file():
            out.append(a)
    return out


def _check_space(layout: Layout) -> None:
    from harvey import disk
    from harvey.overlay.migrate import MIN_FREE_BYTES

    try:
        free = disk.free_bytes(layout.target / NEW_DB)
    except disk.HostDriveUnknown as e:
        raise Refused(str(e)) from e
    if free < MIN_FREE_BYTES:
        raise Refused(f"{free / 1e9:.1f} GB free where the new files would be written; the "
                      f"upgrade needs at least {MIN_FREE_BYTES / 1e9:.0f} GB")


def _check_unchanging(layout: Layout) -> None:
    """An output folder reads the sources as files that don't change
    (immutable: no locks, the write-ahead log not read): refused while one
    is in use, which such a read would misread without an error."""
    from harvey.overlay import migrate

    files = [layout.db]
    for m in _manifests().values():
        if m.register:
            files.append(layout.data / m.register["file"])
    for path in files:
        if path.is_file() and _size(Path(str(path) + "-wal")):
            raise Refused(f"{path.name} is in use (its write-ahead log holds changes): an output "
                          "folder reads a copy of the data folder; stop Harvey, or copy the "
                          "folder, then upgrade again")
    con = sqlite3.connect(migrate._uri(layout.db, read_only=True, immutable=True), uri=True)
    try:
        if migrate.live_worker(con, "main"):
            raise Refused(f"{layout.db.name} is in use (a worker is running jobs on it): an "
                          "output folder reads a copy of the data folder; stop Harvey, then "
                          "upgrade again")
    finally:
        con.close()


def run(layout: Layout | None = None, *, say=None, check_space: bool = True) -> dict:
    """Run (or resume) the upgrade to its end; returns status(). Raises
    Refused when it can't start or go on (the state file says why too)."""
    from harvey import pidfile

    layout = layout or Layout(DATA_DIR)
    say = say or (lambda line: logger.info("upgrade: %s", line))
    schema = schema_of(layout.db, layout.read_only)
    state = read_state(layout)
    # A switch cut short is finished, whatever else the state file says.
    pending = not layout.out and switch_pending(layout.db)
    if _newer(schema):
        from harvey.overlay import schema as overlay_schema
        from harvey.state import newer_build_message

        raise Refused(newer_build_message(schema, overlay_schema.LATEST))
    if schema >= _target() and not layout.out and not pending:
        return status(layout)
    if schema != 24 and not pending:
        if schema == 0:
            raise Refused(f"no database at {layout.db}")
        raise Refused(f"{layout.db} is schema {schema}: the upgrade reads schema 24. Start "
                      "the Harvey release before this one once (it brings the database to "
                      "schema 24), then upgrade")
    layout.target.mkdir(parents=True, exist_ok=True)
    lock = pidfile.hold_loop_lock(layout.lock_file)
    if lock is None:
        raise Refused("an upgrade is already running")
    try:
        if layout.out:
            _check_unchanging(layout)
        if check_space and not pending:
            _check_space(layout)
        state.setdefault("started_at", None)
        state["started_at"] = state["started_at"] or time.time()
        state["out"] = str(layout.out) if layout.out else None
        progress = _Progress(layout, state, say)
        try:
            if pending:
                _switch(layout, progress)
            else:
                _registers(layout, progress)
                anaf_path = _anaf(layout, progress)
                adapters = _adapters(layout, anaf_path)
                source = _source(layout)
                _harvey(layout, progress, adapters, source)
                _verify(layout, progress, adapters, anaf_path, source)
                if layout.out:
                    progress.end("switch", "skipped", why="output folder: nothing is switched")
                else:
                    _switch(layout, progress)
            state["finished_at"] = time.time()
            progress.save()
        except BaseException as e:
            running_now = [s for s in STAGES if state["stages"][s].get("state") == "running"]
            for s in running_now:
                progress.end(s, "failed" if isinstance(e, Exception) else "stopped")
            state["error"] = f"{type(e).__name__}: {e}" if not isinstance(e, Refused) else str(e)
            progress.save()
            raise
    finally:
        pidfile.release_loop_lock(lock)
    return status(layout)


def _registers(layout: Layout, progress: _Progress) -> None:
    from importlib import import_module

    mans = _manifests()
    built: dict[str, Path | None] = {}
    for stage, mid in DERIVED.items():
        m = mans.get(mid)
        path = layout.data / m.register["file"] if m else None
        target = layout.derived_file(m.register["file"]) if m else None
        if stage == "registry":
            built["registry"] = target
        if progress.stage(stage).get("state") in ("done", "skipped"):
            if target is not None and not target.is_file() \
                    and progress.stage(stage)["state"] == "done":
                progress.stage(stage)["state"] = "todo"    # its output went missing
            else:
                continue
        if path is None or not path.is_file():
            progress.end(stage, "skipped", why="not loaded")
            continue
        progress.begin(stage)
        derived = import_module(m.register["adapter"].rsplit(".", 1)[0] + ".derived")
        if stage == "financials":
            reg = mans.get(DERIVED["registry"])
            reg_path = layout.data / reg.register["file"] if reg else None
            counts = derived.reindex(path, target, registry=reg_path,
                                     registry_derived=built.get("registry"))
        else:
            counts = derived.reindex(path, target)
        progress.end(stage, **{k: v for k, v in counts.items() if k != "seconds"})


def _anaf(layout: Layout, progress: _Progress) -> Path | None:
    """The ANAF file the new harvey.db reads: the rewrite's, the current
    one when it is compact already, or None when there is none."""
    from harvey.modules.ro.anaf import store

    m = _manifests().get(ANAF_MODULE)
    src = layout.data / m.register["file"] if m else None
    st = progress.stage("anaf")
    if src is None or not src.is_file():
        if st.get("state") != "done":
            progress.end("anaf", "skipped", why="not loaded")
        return layout.new_anaf if layout.new_anaf.is_file() else None
    found = _anaf_version(src, layout.read_only)
    if found == store.VERSION:
        progress.end("anaf", "skipped", why="compact already")
        return src
    if st.get("state") == "done" and _anaf_version(layout.new_anaf) == store.VERSION:
        return layout.new_anaf
    progress.begin("anaf")
    report = store.rewrite(src, layout.new_anaf, immutable=layout.read_only, min_free=0,
                           on_chunk=lambda rows: progress.tick("anaf", rows=rows))
    if report["mismatched"]:
        raise Refused(f"{report['mismatched']} ANAF answers didn't read back the same after "
                      "compressing: the upgrade stops here")
    progress.end("anaf", rows=report["rows"], bytes=report["bytes"])
    return layout.new_anaf


def _source(layout: Layout) -> str:
    """A digest of the schema 24 harvey.db's content as the migration
    reads it (migrate.fingerprint: rows, highest rowid, latest change per
    table): what a migration and a verification were made from."""
    from harvey.overlay import migrate

    con = sqlite3.connect(migrate._uri(layout.db, read_only=True, immutable=layout.read_only),
                          uri=True)
    try:
        return _digest(migrate.fingerprint(con, "main"))
    finally:
        con.close()


def _digest(fingerprint: dict) -> str:
    import hashlib

    # (As the migration keeps it: a JSON round trip.)
    text = json.dumps(json.loads(json.dumps(fingerprint)), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _migrated_from(path: Path) -> str | None:
    """The digest of the source an (unfinished or finished) migration in
    ``path`` started from; None when it has none."""
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT detail FROM migrate_progress WHERE step = 'source'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return _digest(json.loads(row[0])) if row and row[0] else None


def _remigrate(layout: Layout, progress: _Progress, why: str) -> None:
    """Throw the migration and its verification away: made from a source
    that has changed since."""
    for suffix in SIDECARS:
        Path(str(layout.new_db) + suffix).unlink(missing_ok=True)
        Path(str(layout.new_db) + ".work" + suffix).unlink(missing_ok=True)
    for name in ("harvey", "verify", "switch"):
        progress.state["stages"][name] = {"state": "todo"}
    progress.state["verification"] = None
    progress.save()
    progress.say(why)


def _harvey(layout: Layout, progress: _Progress, adapters, source: str) -> None:
    from harvey.overlay import migrate

    st = progress.stage("harvey")
    made_from = _migrated_from(layout.new_db)
    if made_from is not None and made_from != source:
        _remigrate(layout, progress, "harvey: harvey.db changed since it was migrated: "
                                     "migrating it again")
    elif st.get("state") == "done" and not layout.new_db.is_file():
        _remigrate(layout, progress, "harvey: the migrated file is gone: migrating again")
    if progress.stage("harvey").get("state") == "done" and layout.new_db.is_file():
        return
    progress.begin("harvey")
    names = [s.__name__.lstrip("_") for s in migrate.STEPS]

    def tick(step, last):
        progress.tick("harvey", step=step, of=len(names),
                      at=names.index(step) + 1 if step in names else None, rows=last)

    try:
        migrate.migrate(layout.db, layout.new_db, immutable=layout.read_only, min_free=0,
                        registers=adapters, on_chunk=tick)
    except migrate.Refused as e:
        raise Refused(str(e)) from e
    progress.stage("harvey")["source"] = _migrated_from(layout.new_db)
    progress.end("harvey", bytes=_size(layout.new_db))


def summary(rep: dict) -> dict:
    """A verification report reduced to what a screen shows (counts only)."""
    out = {"passed": rep.get("passed"), "failures": rep.get("failures", [])[:50],
           "seconds": rep.get("seconds", {})}
    doctor = rep.get("doctor")
    if isinstance(doctor, dict) and "error" not in doctor:
        out["doctor"] = {"checked": len(doctor), "differing": sum(1 for v in doctor.values() if v)}
    counts = rep.get("counts")
    if isinstance(counts, dict) and "error" not in counts:
        out["counts"] = {k: {"old": v.get("old"), "new": v.get("new"), "passed": v.get("passed")}
                         for k, v in counts.items() if isinstance(v, dict) and "old" in v}
    for section in ("stored", "registers", "filters"):
        part = rep.get(section)
        if isinstance(part, dict) and "error" not in part:
            rows = [v for v in part.values() if isinstance(v, dict)]
            out[section] = {"checked": len(rows),
                            "passed": sum(1 for v in rows if v.get("passed") is not False)}
    if section_filters := rep.get("filters"):
        if isinstance(section_filters, dict) and "error" not in section_filters:
            out["filter_totals"] = {k: {"old": v.get("old"), "new": v.get("new")}
                                    for k, v in section_filters.items() if isinstance(v, dict)}
    if "anaf" in rep:
        out["anaf"] = rep["anaf"]
    return out


def _verify(layout: Layout, progress: _Progress, adapters, anaf_path: Path | None,
            source: str) -> None:
    from harvey.modules.ro.anaf import store
    from harvey.overlay import verify

    st = progress.stage("verify")
    if st.get("state") == "done" and (progress.state.get("verification") or {}).get("passed") \
            and st.get("source") == source:
        return
    progress.begin("verify")
    report: dict = {"seconds": {}}
    for at, section in enumerate(VERIFY_SECTIONS, 1):
        progress.tick("verify", section=section, at=at, of=len(VERIFY_SECTIONS) + 1)
        progress.save()
        part = verify.report(layout.new_db, layout.db, adapters=adapters,
                             immutable=layout.read_only, sections=(section,))
        report[section] = part.get(section)
        report["seconds"].update(part.get("seconds", {}))
    m = _manifests().get(ANAF_MODULE)
    src = layout.data / m.register["file"] if m else None
    if anaf_path is not None and src is not None and anaf_path != src and src.is_file():
        progress.tick("verify", section="anaf", at=len(VERIFY_SECTIONS) + 1,
                      of=len(VERIFY_SECTIONS) + 1)
        progress.save()
        started = time.monotonic()
        report["anaf"] = store.verify(src, anaf_path, immutable=layout.read_only)
        report["seconds"]["anaf"] = round(time.monotonic() - started, 1)
    report["failures"] = verify.failures(report)
    if "anaf" in report and not report["anaf"].get("equal"):
        report["failures"].append("anaf: the rewritten answers don't hash equal to the originals")
    if _source(layout) != source:
        report["failures"].append("harvey.db was written to while it was verified: stop Harvey, "
                                  "then upgrade again (it migrates it again)")
    report["passed"] = not report["failures"]
    progress.stage("verify")["source"] = source
    progress.state["verification"] = summary(report)
    _write_report(layout, report)
    progress.end("verify", "done" if report["passed"] else "failed")
    if not report["passed"]:
        shown = "; ".join(report["failures"][:3])
        raise Refused(f"verification failed ({len(report['failures'])} differences: {shown}): "
                      "the old files stay in use, nothing was switched")


def _write_report(layout: Layout, report: dict) -> None:
    """The full verification report beside the state (counts only)."""
    path = layout.target / "upgrade-verification.json"
    path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


def _rename(src: Path, dst: Path) -> None:
    """``src`` and its sidecar files to ``dst``'s names."""
    for suffix in SIDECARS:
        a, b = Path(str(src) + suffix), Path(str(dst) + suffix)
        if a.exists():
            os.replace(a, b)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _settle(path: Path) -> None:
    """Everything in ``path`` itself, no write-ahead log or shared memory
    file beside it (so it can be linked, or renamed over, alone). Refused
    while something else has it open for writing."""
    con = sqlite3.connect(f"file:{quote(str(path.resolve()))}", uri=True, isolation_level=None)
    try:
        busy = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        con.close()
    leftover = [s for s in ("-wal", "-journal") if _size(Path(str(path) + s))]
    if busy or leftover:
        raise Refused(f"{path.name} is open in another program (Harvey, a database browser): "
                      "close it, then upgrade again")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _swap(live: Path, new: Path, old: Path) -> None:
    """``new`` takes ``live``'s name and ``live`` is kept as ``old``, with
    ``live`` never missing: a hard link keeps the old file, then one atomic
    rename puts the new one over it. Where hard links can't be made, two
    renames (a cut between them is finished on the next run). Both files
    are settled already."""
    if live.is_file() and not _same(live, old):
        try:
            os.link(live, old)
        except OSError:
            os.replace(live, old)
    os.replace(new, live)


def _switch(layout: Layout, progress: _Progress) -> None:
    """The new files take the live names; the old ones are kept aside.
    Every refusal is decided before anything is renamed, and each rename
    from what is on disk, so a switch cut short finishes on the next run."""
    from harvey.modules.ro.anaf import store
    from harvey.overlay import migrate

    if not (progress.state.get("verification") or {}).get("passed"):
        raise Refused("verification hasn't passed: nothing is switched")
    progress.begin("switch")
    db, new_db, old_db = layout.db, layout.new_db, layout.data / OLD_DB
    found = schema_of(db)
    # Every refusal first.
    if found == 24:
        if not new_db.is_file():
            raise Refused(f"{new_db.name} is missing: upgrade again (it migrates again)")
        if old_db.exists() and not _same(db, old_db):
            raise Refused(f"{old_db.name} already exists: move it away, then upgrade again")
        con = sqlite3.connect(f"file:{quote(str(db.resolve()))}?mode=ro", uri=True)
        try:
            if migrate.live_worker(con, "main"):
                raise Refused("a worker is running jobs on this database: stop harvey "
                              "worker (and harvey run), then upgrade again")
        finally:
            con.close()
        if _source(layout) != progress.stage("verify").get("source"):
            _remigrate(layout, progress, "switch: harvey.db changed since it was verified")
            raise Refused("harvey.db was written to after it was migrated: stop Harvey, then "
                          "upgrade again (it migrates and verifies it again)")
    elif found == 0:
        if not (new_db.is_file() and old_db.is_file()):
            raise Refused(f"there is no harvey.db, and no {new_db.name} and {old_db.name} to "
                          f"finish the switch with: rename {old_db.name} back to harvey.db, "
                          "then upgrade again")
    elif found < _target():
        raise Refused(f"{db.name} is schema {found}: nothing is switched")
    m = _manifests().get(ANAF_MODULE)
    anaf = None
    if m is not None and layout.new_anaf.is_file():
        live, old = layout.data / m.register["file"], layout.data / OLD_ANAF
        if live.is_file() and _anaf_version(live) == store.VERSION:
            raise Refused(f"{live.name} is compact already and {layout.new_anaf.name} is "
                          f"beside it: move {layout.new_anaf.name} away, then upgrade again")
        if live.is_file() and old.exists() and not _same(live, old):
            raise Refused(f"{old.name} already exists: move it away, then upgrade again")
        anaf = (live, old)
    # Then the renames: harvey.db, then anaf.db.
    if found in (0, 24):
        _settle(new_db)
        if found == 24:
            _settle(db)
        _swap(db, new_db, old_db)
    if anaf is not None:
        live, old = anaf
        _settle(layout.new_anaf)
        if live.is_file():
            _settle(live)
        _swap(live, layout.new_anaf, old)
    if schema_of(db) < _target():
        raise Refused("the switch didn't leave a schema 25 harvey.db: nothing more was changed")
    progress.end("switch", kept=[f["name"] for f in old_files(layout)])


def delete_old(layout: Layout | None = None) -> int:
    """Delete the files the switch kept aside: only once the switch is done
    (the live harvey.db is schema 25 or later, nothing waits to take a live
    name). Returns the bytes freed."""
    layout = layout or Layout(DATA_DIR)
    if schema_of(layout.db) < _target() or switch_pending(layout.db) or layout.new_db.exists() \
            or read_state(layout)["stages"]["switch"].get("state") != "done":
        raise Refused("the switch isn't finished: the old files stay")
    if running(layout):
        raise Refused("an upgrade is running")
    freed = 0
    for f in old_files(layout):
        path = layout.data / f["name"]
        path.unlink(missing_ok=True)
        freed += f["bytes"]
    return freed
