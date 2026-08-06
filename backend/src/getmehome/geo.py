"""Geodetic helpers.

A city spans a few tens of kilometres, so we work in a local equirectangular
projection anchored inside it. Over that extent the distortion against a proper
projected CRS is a few centimetres — far below the accuracy of any of our
inputs — and it keeps every distance computation to plain numpy arithmetic.

**The anchor is not global.** It used to be: a module-level origin at the
centre of the District, which every call site shared. That is fine for one
city and quietly wrong for two. The east-west scale is ``cos(origin_lat)``, so
projecting New York (40.7 degrees) through Washington's origin (38.9) shrinks
every east-west distance by 2.6% — around a kilometre across the city, in a
direction nothing would ever flag as an error.

So there are two kinds of projection here, and the distinction is the point:

* :class:`Projection` — a named, stable frame for a whole city's dataset.
  Anything that has to agree with something else computed earlier uses one of
  these: graph node coordinates, KD-trees of lamps and cameras, the crime
  raster, the hex grid. Each city carries its own, and it is stored with the
  data rather than looked up, so a graph can never be read through the wrong
  frame.
* **Self-anchored helpers** — the polyline functions below take no projection
  at all. Measuring, resampling or simplifying one line only needs a frame
  local to *that line*, so they anchor on its own first vertex. That makes them
  correct in any city with nothing to thread through, and marginally more
  accurate than a city-wide frame even in the city they came from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_371_008.8

_DEG_M = EARTH_RADIUS_M * math.pi / 180.0


@dataclass(frozen=True)
class Projection:
    """A local equirectangular frame anchored at one point.

    Frozen and cheap to construct. Carried by the data it was used to build,
    which is what stops a city's coordinates being read through another
    city's frame.
    """

    origin_lat: float
    origin_lon: float

    @property
    def lon_scale(self) -> float:
        return _DEG_M * math.cos(math.radians(self.origin_lat))

    def to_local(self, lat, lon):
        """Project lat/lon (degrees) to metres east/north of the origin."""
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        return (lon - self.origin_lon) * self.lon_scale, (lat - self.origin_lat) * _DEG_M

    def to_wgs84(self, x, y):
        """Inverse of :meth:`to_local`."""
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        return y / _DEG_M + self.origin_lat, x / self.lon_scale + self.origin_lon

    def to_dict(self) -> dict:
        return {"originLat": self.origin_lat, "originLon": self.origin_lon}

    @classmethod
    def from_dict(cls, data: dict) -> Projection:
        return cls(
            origin_lat=float(data["originLat"]), origin_lon=float(data["originLon"])
        )


def frame_for(coords) -> Projection:
    """A frame anchored on the first vertex of some geometry.

    What the self-anchored helpers use. The error in an equirectangular frame
    grows with distance from its origin, so anchoring on the geometry itself
    keeps it at its smallest for exactly the thing being measured.
    """
    if len(coords) == 0:
        return Projection(0.0, 0.0)
    first = coords[0]
    return Projection(float(first[0]), float(first[1]))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Used where exactness beats speed."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_difference(a: float, b: float) -> float:
    """Signed smallest angle from bearing ``a`` to ``b``, in (-180, 180].

    Positive is clockwise (a right turn).
    """
    d = (b - a + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def polyline_length_m(coords: list[tuple[float, float]]) -> float:
    """Total length of a (lat, lon) polyline."""
    if len(coords) < 2:
        return 0.0
    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = frame_for(coords).to_local(lat, lon)
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def sample_polyline(
    coords: list[tuple[float, float]], spacing_m: float, projection: Projection
) -> np.ndarray:
    """Resample a (lat, lon) polyline at roughly ``spacing_m`` intervals.

    Returns an ``(n, 2)`` array of local x/y coordinates. Always includes the
    two endpoints, and never returns fewer than one point, so a zero-length
    edge still gets scored.

    The projection is required rather than self-anchored, and that is the
    difference between this and the helpers above. These points are compared
    against *other* points — lamps in a KD-tree, cells in the crime raster,
    every other segment in the snap index — so they have to share one frame.
    Self-anchoring here would give every segment its own origin and put the
    whole city on top of itself.
    """
    if not coords:
        return np.zeros((0, 2))
    lat = np.array([c[0] for c in coords], dtype=np.float64)
    lon = np.array([c[1] for c in coords], dtype=np.float64)
    x, y = projection.to_local(lat, lon)
    pts = np.column_stack([x, y])
    if len(pts) == 1:
        return pts

    seg = np.hypot(np.diff(x), np.diff(y))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = cum[-1]
    if total <= 0:
        return pts[:1]

    n = max(2, int(math.ceil(total / spacing_m)) + 1)
    targets = np.linspace(0.0, total, n)
    sx = np.interp(targets, cum, x)
    sy = np.interp(targets, cum, y)
    return np.column_stack([sx, sy])


def project_point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float, float]:
    """Closest point on segment AB to P.

    Returns ``(x, y, t)`` where ``t`` in [0, 1] is the position along AB.
    """
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return ax, ay, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = min(1.0, max(0.0, t))
    return ax + t * dx, ay + t * dy, t


def interpolate_polyline(
    coords: list[tuple[float, float]], fraction: float
) -> tuple[float, float]:
    """Point at ``fraction`` (0-1) of the way along a (lat, lon) polyline."""
    if not coords:
        raise ValueError("empty polyline")
    if len(coords) == 1 or fraction <= 0:
        return coords[0]
    if fraction >= 1:
        return coords[-1]

    frame = frame_for(coords)
    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = frame.to_local(lat, lon)
    seg = np.hypot(np.diff(x), np.diff(y))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    target = cum[-1] * fraction
    tx = float(np.interp(target, cum, x))
    ty = float(np.interp(target, cum, y))
    plat, plon = frame.to_wgs84(tx, ty)
    return float(plat), float(plon)


def split_polyline(
    coords: list[tuple[float, float]], fraction: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Cut a polyline at ``fraction`` of its length, returning both halves.

    The split point appears as the last vertex of the first half and the first
    vertex of the second, so both halves are individually valid geometries.
    """
    if len(coords) < 2 or fraction <= 0:
        return [coords[0]] if coords else [], list(coords)
    if fraction >= 1:
        return list(coords), [coords[-1]]

    frame = frame_for(coords)
    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = frame.to_local(lat, lon)
    seg = np.hypot(np.diff(x), np.diff(y))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    target = cum[-1] * fraction

    idx = int(np.searchsorted(cum, target, side="right")) - 1
    idx = min(max(idx, 0), len(coords) - 2)

    tx = float(np.interp(target, cum, x))
    ty = float(np.interp(target, cum, y))
    plat, plon = frame.to_wgs84(tx, ty)
    cut = (float(plat), float(plon))

    return coords[: idx + 1] + [cut], [cut] + coords[idx + 1 :]


def simplify_polyline(
    coords: list[tuple[float, float]], tolerance_m: float = 2.0
) -> list[tuple[float, float]]:
    """Douglas-Peucker simplification, to keep response payloads small."""
    if len(coords) < 3:
        return list(coords)

    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = frame_for(coords).to_local(lat, lon)
    pts = np.column_stack([x, y])
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True

    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        a, b = pts[lo], pts[hi]
        ab = b - a
        norm = math.hypot(ab[0], ab[1])
        seg = pts[lo + 1 : hi]
        if norm < 1e-9:
            dist = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            # Perpendicular distance via the 2-D cross product.
            dist = np.abs(
                ab[0] * (a[1] - seg[:, 1]) - (a[0] - seg[:, 0]) * ab[1]
            ) / norm
        k = int(np.argmax(dist))
        if dist[k] > tolerance_m:
            split = lo + 1 + k
            keep[split] = True
            stack.append((lo, split))
            stack.append((split, hi))

    return [coords[i] for i in range(len(coords)) if keep[i]]


def compass_direction(bearing: float) -> str:
    """Human-readable compass point for a bearing, e.g. 'northeast'."""
    names = [
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    ]
    return names[int((bearing % 360.0 + 22.5) // 45.0) % 8]
