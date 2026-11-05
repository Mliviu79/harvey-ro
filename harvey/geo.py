"""Areas a search covers: a place's point, the circle around it, its box and
(when a boundaries gazetteer is loaded) its outline, and the cells a capped
map search is split into.

Market-neutral and offline: nothing here asks the network or knows a
country. A place becomes an Area from what a gazetteer said about it
(Nominatim's point and bounding box, a boundary's outline from the
boundaries module) or from a circle the user wrote (icp.geo_coordinates).

Three shapes, one per kind of source (the search module's manifest says
which: ``area`` on its capability):

- ``circle``: a listings source with no result cap (DataForSEO Business
  Listings) takes ONE circle that covers the area; what comes back outside
  the area is dropped (by the outline when there is one, else the box).
- ``grid``: a map search that serves at most N places (Scavio's 120,
  DataForSEO Maps' 700) is tiled: coarse cells first, and a cell that comes
  back full is split into four, until the cells are small (MIN_CELL_KM).
- ``point``: a ranked web search (SERP) searches from the area's point;
  its radius means nothing there.

Coordinates are degrees (WGS84), distances kilometres. A box is
(south, west, north, east). Rings are lists of (lng, lat) pairs, read with
the even-odd rule, so a hole is a ring inside another.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EARTH_KM = 6371.0088
# A circle is never smaller than this nor larger than that: a point with no
# box (a village) still searches its surroundings, and a country-sized box
# is still one request.
MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 500.0
# The radius a place gets when its gazetteer gave no box (an answer cached
# before boxes were kept, a point the user wrote without one).
DEFAULT_RADIUS_KM = 40.0
# The grid starts coarse: the area's longest side is cut into cells of
# about START_KM, START_CELLS a side at most (a town is one cell, a county
# 2 x 2), and a cell that comes back full is split into four while its side
# is at least twice MIN_CELL_KM: the dense parts get small cells, the
# empty ones cost one search.
START_KM = 30.0
START_CELLS = 2
MIN_CELL_KM = 1.0
# A cell whose page is mostly places seen in earlier cells stops there:
# this share of the page, at least SEEN_MIN_RESULTS of them.
SEEN_STOP = 0.8
SEEN_MIN_RESULTS = 5
SHAPES = ("", "circle", "grid", "point")


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def _clamp_radius(r: float) -> float:
    return round(max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, r)), 1)


def radius_from_bbox(lat: float, lng: float, bbox) -> float:
    """The radius of the circle round (lat, lng) that holds the whole box:
    the distance to its farthest corner, clamped."""
    s, w, n, e = bbox
    far = max(distance_km(lat, lng, la, lo) for la in (s, n) for lo in (w, e))
    return _clamp_radius(far)


def covering_radius(lat: float, lng: float, rings) -> float:
    """The radius of the circle round (lat, lng) that holds every point of
    the outline, clamped."""
    far = max((distance_km(lat, lng, la, lo) for ring in rings for lo, la in ring),
              default=0.0)
    return _clamp_radius(far)


def bbox_of(rings) -> tuple[float, float, float, float] | None:
    pts = [(lo, la) for ring in rings for lo, la in ring]
    if not pts:
        return None
    return (min(p[1] for p in pts), min(p[0] for p in pts),
            max(p[1] for p in pts), max(p[0] for p in pts))


def box_around(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """The box that holds a circle."""
    dlat = math.degrees(radius_km / EARTH_KM)
    dlng = dlat / max(0.01, math.cos(math.radians(lat)))
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def in_rings(lat: float, lng: float, rings) -> bool:
    """Even-odd point-in-polygon over every ring (holes are rings too)."""
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            if (y1 > lat) != (y2 > lat):
                x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lng < x:
                    inside = not inside
    return inside


def ring_area_km2(ring) -> float:
    """A ring's signed area on a local equirectangular projection (good to
    a fraction of a percent at county size)."""
    if len(ring) < 3:
        return 0.0
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    k = math.radians(1) * EARTH_KM
    pts = [(lo * k * math.cos(lat0), la * k) for lo, la in ring]
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1])) / 2


def area_km2(rings) -> float:
    """Outer rings minus holes: the absolute sum, even-odd (a ring inside
    an odd number of others is a hole)."""
    total = 0.0
    for i, ring in enumerate(rings):
        a = abs(ring_area_km2(ring))
        if not ring:
            continue
        lo, la = ring[0]
        depth = sum(1 for j, other in enumerate(rings) if j != i and in_rings(la, lo, [other]))
        total += -a if depth % 2 else a
    return round(max(0.0, total), 2)


def zoom_for_radius(radius_km: float, low: int = 3, high: int = 16) -> int:
    """The map zoom whose view spans about a circle of ``radius_km``: a
    zoom-z view spans roughly 40,000 km / 2^z, so a 40 km radius is zoom
    10 (a metro area) and 5 km zoom 13."""
    if not math.isfinite(radius_km) or radius_km <= 0:
        radius_km = DEFAULT_RADIUS_KM
    return max(low, min(high, round(math.log2(40000 / max(radius_km, 0.5)))))


def parse_coordinate(coordinate: str) -> tuple[float, float, float | None] | None:
    """"lat,lng[,radius_km]" -> (lat, lng, radius or None); None when it
    isn't a point on Earth."""
    parts = [p.strip() for p in (coordinate or "").split(",")]
    if len(parts) < 2 or not all(parts[:2]):
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    radius = None
    if len(parts) > 2 and parts[2]:
        try:
            radius = float(parts[2])
        except ValueError:
            radius = None
        if radius is not None and (not math.isfinite(radius) or radius <= 0):
            radius = None
    return lat, lng, radius


@dataclass
class Area:
    """A place a search covers. ``source`` says where its shape came from:
    "boundaries" (an outline: ``unit``, ``level``, ``area_km2``),
    "nominatim" (a point and a box), "config" (a circle the user wrote)."""

    name: str = ""
    lat: float = 0.0
    lng: float = 0.0
    radius_km: float = DEFAULT_RADIUS_KM
    bbox: tuple[float, float, float, float] | None = None
    source: str = ""
    unit: int | None = None
    level: int | None = None
    area_km2: float | None = None
    # The outline (rings of (lng, lat)); never stored with a query, read
    # again from the boundaries file by ``unit`` when it is needed.
    rings: list = field(default_factory=list, repr=False)

    @property
    def coordinate(self) -> str:
        """"lat,lng,radius_km": how a listings source is told the circle."""
        return f"{self.lat:.4f},{self.lng:.4f},{self.radius_km:g}"

    def as_dict(self) -> dict:
        """What a search keeps (its job's params): everything but the
        outline."""
        out = {"name": self.name, "lat": round(self.lat, 6), "lng": round(self.lng, 6),
               "radius_km": self.radius_km, "source": self.source}
        if self.bbox:
            out["bbox"] = [round(v, 6) for v in self.bbox]
        for k in ("unit", "level", "area_km2"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        return out

    @classmethod
    def from_dict(cls, d: dict | None) -> "Area | None":
        if not isinstance(d, dict) or "lat" not in d or "lng" not in d:
            return None
        try:
            bbox = tuple(float(v) for v in d["bbox"]) if d.get("bbox") else None
            return cls(name=str(d.get("name") or ""), lat=float(d["lat"]), lng=float(d["lng"]),
                       radius_km=float(d.get("radius_km") or DEFAULT_RADIUS_KM),
                       bbox=bbox if bbox and len(bbox) == 4 else None,
                       source=str(d.get("source") or ""),
                       unit=int(d["unit"]) if d.get("unit") is not None else None,
                       level=int(d["level"]) if d.get("level") is not None else None,
                       area_km2=float(d["area_km2"]) if d.get("area_km2") is not None else None)
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_coordinate(cls, coordinate: str, name: str = "", source: str = "config"
                        ) -> "Area | None":
        """A circle someone wrote ("lat,lng,radius"): its box is the
        square that holds it. ``source`` "point": a point whose radius is
        only the default (a gazetteer gave no box), so it has no shape: no
        box, never tiled, nothing filtered by it."""
        point = parse_coordinate(coordinate)
        if point is None:
            return None
        lat, lng, radius = point
        radius = _clamp_radius(radius or DEFAULT_RADIUS_KM)
        return cls(name=name, lat=lat, lng=lng, radius_km=radius,
                   bbox=None if source == "point" else box_around(lat, lng, radius),
                   source=source)

    @property
    def shaped(self) -> bool:
        """Whether the area has a shape a search can be tiled over: an
        outline, a gazetteer's box or a circle the user wrote."""
        return bool(self.rings or self.bbox)

    @classmethod
    def from_bbox(cls, lat: float, lng: float, bbox, name: str = "",
                  source: str = "nominatim") -> "Area":
        """A gazetteer's point and box: the circle round the point that
        holds the box."""
        return cls(name=name, lat=lat, lng=lng, radius_km=radius_from_bbox(lat, lng, bbox),
                   bbox=tuple(bbox), source=source)

    def contains(self, lat, lng) -> bool | None:
        """Whether a point is in the area: by its outline when it has one,
        else its box; None when the point is unknown or there is no shape."""
        if lat is None or lng is None:
            return None
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return None
        if self.rings:
            return in_rings(lat, lng, self.rings)
        if self.bbox:
            s, w, n, e = self.bbox
            return s <= lat <= n and w <= lng <= e
        return None


# ── The grid ──

Cell = tuple  # (south, west, north, east)


def cell_side_km(cell) -> float:
    s, w, n, e = cell
    mid = (s + n) / 2
    return max(distance_km(s, w, n, w), distance_km(mid, w, mid, e))


def cell_centre(cell) -> tuple[float, float]:
    s, w, n, e = cell
    return (s + n) / 2, (w + e) / 2


def cell_radius_km(cell) -> float:
    """The circle round the cell's centre that holds it: the distance to
    its farthest corner (away from the equator the corners nearer it are
    farther apart, so the four are measured)."""
    s, w, n, e = cell
    lat, lng = cell_centre(cell)
    far = max(distance_km(lat, lng, la, lo) for la in (s, n) for lo in (w, e))
    return round(max(0.1, far), 2)


def cell_coordinate(cell) -> str:
    lat, lng = cell_centre(cell)
    return f"{lat:.5f},{lng:.5f},{cell_radius_km(cell):g}"


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Whether two segments of (x, y) points cross (touching counts)."""
    def side(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 0) - (v < 0)

    d1, d2 = side(q1, q2, p1), side(q1, q2, p2)
    d3, d4 = side(p1, p2, q1), side(p1, p2, q2)
    if d1 != d2 and d3 != d4:
        return True
    def on(a, b, c):
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))
    return ((d1 == 0 and on(q1, q2, p1)) or (d2 == 0 and on(q1, q2, p2))
            or (d3 == 0 and on(p1, p2, q1)) or (d4 == 0 and on(p1, p2, q2)))


def _touches(area: Area, cell) -> bool:
    """Whether a cell has anything of the area in it: by the outline (a
    corner or the centre inside it, a point of it inside the cell, or an
    edge of it crossing the cell's), else by the box (they overlap)."""
    s, w, n, e = cell
    if area.rings:
        lat, lng = cell_centre(cell)
        if any(in_rings(la, lo, area.rings) for la, lo in
               ((lat, lng), (s, w), (s, e), (n, w), (n, e))):
            return True
        if any(s <= la <= n and w <= lo <= e for ring in area.rings for lo, la in ring):
            return True
        sides = (((w, s), (e, s)), ((e, s), (e, n)), ((e, n), (w, n)), ((w, n), (w, s)))
        for ring in area.rings:
            for i in range(len(ring)):
                a, b = ring[i], ring[(i + 1) % len(ring)]
                if max(a[0], b[0]) < w or min(a[0], b[0]) > e \
                        or max(a[1], b[1]) < s or min(a[1], b[1]) > n:
                    continue
                if any(_segments_cross(a, b, c1, c2) for c1, c2 in sides):
                    return True
        return False
    if area.bbox:
        bs, bw, bn, be = area.bbox
        return not (e < bw or w > be or n < bs or s > bn)
    return True


def start_cells(area: Area) -> list:
    """The coarse cells a tiled search starts with: the area's box cut
    into k x k (k from START_KM, at most START_CELLS), keeping the cells
    that hold some of the area."""
    box = area.bbox or box_around(area.lat, area.lng, area.radius_km)
    s, w, n, e = box
    longest = cell_side_km(box)
    k = max(1, min(START_CELLS, math.ceil(longest / START_KM - 1e-9)))
    dlat, dlng = (n - s) / k, (e - w) / k
    cells = [(s + i * dlat, w + j * dlng, s + (i + 1) * dlat, w + (j + 1) * dlng)
             for i in range(k) for j in range(k)]
    kept = [c for c in cells if _touches(area, c)]
    return kept or cells[:1]


def can_split(cell) -> bool:
    return cell_side_km(cell) >= 2 * MIN_CELL_KM


def split(area: Area, cell) -> list:
    """A full cell's four quarters that hold some of the area."""
    s, w, n, e = cell
    mlat, mlng = (s + n) / 2, (w + e) / 2
    quarters = [(s, w, mlat, mlng), (s, mlng, mlat, e), (mlat, w, n, mlng), (mlat, mlng, n, e)]
    return [q for q in quarters if _touches(area, q)]


def mostly_seen(ids, seen) -> bool:
    """A page that is mostly places earlier cells already found."""
    ids = [i for i in ids if i]
    if len(ids) < SEEN_MIN_RESULTS:
        return False
    return sum(1 for i in ids if i in seen) >= SEEN_STOP * len(ids)
