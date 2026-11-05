"""Administrative boundaries, in a local database (data/boundaries.db).

``build()`` reads OpenStreetMap extracts (source.py downloads them from
Geofabrik) with pyosmium and keeps every administrative boundary
(``boundary=administrative`` with an ``admin_level`` and a name) as one
row:

    unit    the boundary's name and its folded form (what a search by name
            compares), its admin level (OSM's: in most countries 2 is the
            country, 4 a region or county, 8 a town or commune), its ref
            tags (``ref``, ``ref:*`` such as ``ref:siruta``, ISO3166-2,
            ``name:en``), its parent (the smallest unit of a lower level
            that holds its centre), its centre, box and area in km², and
            its outline simplified to about SIMPLIFY_DEGREES (rings of
            [lng, lat], read even-odd)
    meta    the regions and files it was built from, when, the counts and
            the licence

pyosmium (``osmium`` on PyPI) is imported only here, inside the load: an
install without it can still read a database built elsewhere, and says
"library missing" where the load is offered. A new load is built in
``<target>.building`` and renamed over the old one, so a reader never sees
half a database. The file is small (a few MB for a country): its
statements are written here, not named, as a side file's are.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from harvey import geo
from harvey.paths import PROJECT_ROOT

BOUNDARIES_PATH = PROJECT_ROOT / "data" / "boundaries.db"
SCHEMA_VERSION = "1"
LICENCE = "ODbL 1.0 (© OpenStreetMap contributors)"
# The outline kept: points closer than this to the simplified line are
# dropped (about 50 m), enough to tell a town from its neighbour.
SIMPLIFY_DEGREES = 0.0005
LEVELS = range(2, 11)
# The units a lookup by name returns, and the children one names.
FIND_LIMIT = 20
CHILDREN_LIMIT = 200
# The tags kept besides the name and level.
_REF = re.compile(r"^(ref|ref:[a-z0-9_:-]+|ISO3166-2|name:en)$")
MISSING = "library missing: pyosmium (osmium) isn't installed; docker compose build harvey"

_SCHEMA = """
CREATE TABLE unit (
  id INTEGER PRIMARY KEY, osm TEXT NOT NULL, name TEXT NOT NULL, fold TEXT NOT NULL,
  level INTEGER NOT NULL, parent INTEGER, refs TEXT NOT NULL DEFAULT '{}',
  lat REAL NOT NULL, lng REAL NOT NULL, south REAL NOT NULL, west REAL NOT NULL,
  north REAL NOT NULL, east REAL NOT NULL, area_km2 REAL NOT NULL, rings TEXT NOT NULL
);
CREATE INDEX unit_fold ON unit(fold);
CREATE INDEX unit_parent ON unit(parent, level);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
_UNIT = ("id", "osm", "name", "fold", "level", "parent", "refs", "lat", "lng",
         "south", "west", "north", "east", "area_km2")


class BoundariesBusy(RuntimeError):
    """Another load of the same file is running."""


_LIBRARY: list[str] = []


def library_problem() -> str:
    """"" when pyosmium can be imported, else what to do (MISSING). Asked
    once a process."""
    if not _LIBRARY:
        try:
            import osmium  # noqa: F401
            _LIBRARY.append("")
        except ImportError:
            _LIBRARY.append(MISSING)
    return _LIBRARY[0]


def fold(text: str) -> str:
    """Lower case, no diacritics (ș and ş alike), words joined by one space."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def simplify(ring: list, tolerance: float = SIMPLIFY_DEGREES) -> list:
    """Douglas-Peucker on one closed ring of (lng, lat), iterative."""
    if len(ring) <= 4:
        return list(ring)
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, at = -1.0, -1
        for i in range(a + 1, b):
            x, y = ring[i]
            d = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm if norm
                 else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5)
            if d > far:
                far, at = d, i
        if at > 0 and far > tolerance:
            keep[at] = True
            stack += [(a, at), (at, b)]
    out = [p for p, k in zip(ring, keep) if k]
    return out if len(out) >= 4 else list(ring)


def centre(rings: list) -> tuple[float, float]:
    """(lat, lng): the area-weighted centroid of the outline, or its box's
    centre when that falls outside it (a crescent, a coast)."""
    sx = sy = sa = 0.0
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            cross = x1 * y2 - x2 * y1
            sa += cross
            sx += (x1 + x2) * cross
            sy += (y1 + y2) * cross
    box = geo.bbox_of(rings)
    if abs(sa) > 1e-12:
        lat, lng = sy / (3 * sa), sx / (3 * sa)
        if geo.in_rings(lat, lng, rings):
            return lat, lng
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _read(files: list[Path], progress=None) -> list[dict]:
    """Every administrative boundary in the files (pyosmium's area
    assembly: a relation's ways joined into rings), measured on its full
    rings and kept with them simplified."""
    try:
        import osmium
    except ImportError:
        raise RuntimeError(MISSING) from None

    found: dict[str, dict] = {}
    for path in files:
        fp = (osmium.FileProcessor(str(path))
              .with_areas(osmium.filter.TagFilter(("boundary", "administrative")))
              .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
        for area in fp:
            tags = {t.k: t.v for t in area.tags}
            name = (tags.get("name") or "").strip()
            try:
                level = int(tags.get("admin_level") or 0)
            except ValueError:
                continue
            if not name or level not in LEVELS or tags.get("boundary") != "administrative":
                continue
            osm = ("w" if area.from_way() else "r") + str(area.orig_id())
            if osm in found:
                continue        # the same boundary in two overlapping extracts
            rings = []
            try:
                for outer in area.outer_rings():
                    rings.append([(n.lon, n.lat) for n in outer])
                    for inner in area.inner_rings(outer):
                        rings.append([(n.lon, n.lat) for n in inner])
            except osmium.InvalidLocationError:
                continue
            rings = [r for r in rings if len(r) >= 4]
            if not rings:
                continue
            # Measured on the full outline, kept simplified: a country's
            # boundaries in full are millions of points, too many to hold.
            lat, lng = centre(rings)
            found[osm] = {"osm": osm, "name": name, "level": level,
                          "refs": {k: v for k, v in tags.items() if _REF.match(k)},
                          "area_km2": geo.area_km2(rings), "lat": lat, "lng": lng,
                          "box": geo.bbox_of(rings),
                          "simple": [simplify(r) for r in rings]}
            if progress and len(found) % 1000 == 0:
                progress("reading boundaries", len(found))
    return list(found.values())


def _parents(units: list[dict]) -> None:
    """Each unit's parent: the closest-level unit of a lower level whose
    outline holds its centre (the smallest, on a tie)."""
    by_level = sorted(units, key=lambda u: (-u["level"], u["area_km2"]))
    for u in units:
        u["parent"] = None
        for cand in by_level:
            if cand["level"] >= u["level"] or cand is u:
                continue
            s, w, n, e = cand["box"]
            if not (s <= u["lat"] <= n and w <= u["lng"] <= e):
                continue
            if geo.in_rings(u["lat"], u["lng"], cand["simple"]):
                u["parent"] = cand["id"]
                break


def build(files: list[Path], target: Path | None = None, *, regions=(), progress=None) -> dict:
    """Load the boundaries of these extracts into a fresh database at
    ``target``, one load at a time (BoundariesBusy). Returns its meta."""
    from harvey.pidfile import _try_lock, _unlock

    target = Path(target or BOUNDARIES_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = open(target.with_name(target.name + ".lock"), "a+", encoding="utf-8")
    try:
        if not _try_lock(lock):
            raise BoundariesBusy(f"another load of {target} is running; try again when it "
                                 "has finished")
        try:
            return _build(files, target, list(regions), progress)
        finally:
            _unlock(lock)
    finally:
        lock.close()


def _build(files, target: Path, regions: list, progress) -> dict:
    started = time.monotonic()
    units = _read([Path(f) for f in files], progress)
    for i, u in enumerate(sorted(units, key=lambda u: u["osm"]), start=1):
        u["id"] = i
    if progress:
        progress("placing each unit in its parent", len(units))
    _parents(units)
    building = target.with_name(target.name + ".building")
    building.unlink(missing_ok=True)
    con = sqlite3.connect(building)
    try:
        con.executescript(_SCHEMA)
        con.executemany(
            "INSERT INTO unit (id, osm, name, fold, level, parent, refs, lat, lng, south, west, "
            "north, east, area_km2, rings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(u["id"], u["osm"], u["name"], fold(u["name"]), u["level"], u["parent"],
              json.dumps(u["refs"], ensure_ascii=False), round(u["lat"], 6), round(u["lng"], 6),
              *(round(v, 6) for v in u["box"]), u["area_km2"],
              json.dumps([[[round(x, 5), round(y, 5)] for x, y in r] for r in u["simple"]]))
             for u in units])
        meta = {
            "schema": SCHEMA_VERSION, "regions": " ".join(regions),
            "files": json.dumps([Path(f).name for f in files]),
            "loaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "load_seconds": str(round(time.monotonic() - started, 1)),
            "units": str(len(units)), "licence": LICENCE,
            "levels": json.dumps({str(lv): sum(1 for u in units if u["level"] == lv)
                                  for lv in sorted({u["level"] for u in units})}),
        }
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", sorted(meta.items()))
        con.commit()
    finally:
        con.close()
    building.replace(target)
    return status(target) or meta


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The database, read-only. FileNotFoundError when it isn't loaded."""
    path = Path(path or BOUNDARIES_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"no boundaries loaded at {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def status(path: Path | None = None) -> dict | None:
    """The meta of the loaded database and its size on disk, or None."""
    path = Path(path or BOUNDARIES_PATH)
    if not path.is_file():
        return None
    con = connect(path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    for k in ("files", "levels"):
        try:
            meta[k] = json.loads(meta.get(k) or "null")
        except ValueError:
            meta[k] = None
    meta["units"] = int(meta.get("units") or 0)
    meta["bytes"] = path.stat().st_size
    return meta


def _unit(row, children: int | None = None, rings: bool = False) -> dict:
    out = {k: row[k] for k in _UNIT if k != "fold"}
    out["refs"] = json.loads(row["refs"] or "{}")
    out["bbox"] = [out.pop("south"), out.pop("west"), out.pop("north"), out.pop("east")]
    if children is not None:
        out["children"] = children
    if rings:
        out["rings"] = [[tuple(p) for p in r] for r in json.loads(row["rings"])]
    return out


def _children(con, unit_id: int) -> int:
    return con.execute("SELECT COUNT(*) FROM unit WHERE parent = ?", (unit_id,)).fetchone()[0]


def find(con, place: str, limit: int = FIND_LIMIT) -> list[dict]:
    """The units a place name may mean, the likeliest first: an exact name
    before one that ends with it after a word ("Județul Exemplu" for
    "Exemplu"); with a second part ("Exemplu, Țara"), the ones whose parent
    has that name first; then the larger level first. Without outlines."""
    parts = [fold(p) for p in (place or "").split(",") if fold(p)]
    if not parts:
        return []
    name, rest = parts[0], parts[1:]
    rows = con.execute(
        "SELECT u.*, p.fold AS parent_fold FROM unit u LEFT JOIN unit p ON p.id = u.parent "
        "WHERE u.fold = ? OR u.fold LIKE ? ORDER BY u.level, u.area_km2 DESC",
        (name, "% " + name.replace("%", "").replace("_", ""))).fetchall()
    rows = [r for r in rows if r["fold"] == name or r["fold"].endswith(" " + name)
            and len(r["fold"].split()) == len(name.split()) + 1]

    def rank(r):
        return (r["fold"] != name, not (rest and r["parent_fold"] in rest), r["level"])
    return [_unit(r, _children(con, r["id"])) for r in sorted(rows, key=rank)[:limit]]


def unit(con, unit_id: int) -> dict | None:
    """One unit with its outline, its parent's name and its children (the
    first CHILDREN_LIMIT, by level then name) and how many there are by
    level."""
    row = con.execute("SELECT * FROM unit WHERE id = ?", (int(unit_id),)).fetchone()
    if row is None:
        return None
    out = _unit(row, rings=True)
    if row["parent"]:
        parent = con.execute("SELECT id, name, level FROM unit WHERE id = ?",
                             (row["parent"],)).fetchone()
        out["parent_name"] = parent["name"] if parent else ""
    kids = con.execute("SELECT id, name, level, area_km2 FROM unit WHERE parent = ? "
                       "ORDER BY level, name LIMIT ?", (row["id"], CHILDREN_LIMIT)).fetchall()
    out["children"] = [dict(k) for k in kids]
    out["children_by_level"] = {str(lv): n for lv, n in con.execute(
        "SELECT level, COUNT(*) FROM unit WHERE parent = ? GROUP BY level ORDER BY level",
        (row["id"],))}
    return out
