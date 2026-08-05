"""Geodetic helpers.

Washington DC spans about 25km, so we work in a local equirectangular
projection anchored at the centre of the city. Over that extent the distortion
against a proper projected CRS is a few centimetres, which is far below the
accuracy of any of our inputs, and it keeps every distance computation to
plain numpy arithmetic.
"""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6_371_008.8

# Projection origin — the geographic centre of the District.
ORIGIN_LAT = 38.9047
ORIGIN_LON = -77.0164

_LAT_SCALE = EARTH_RADIUS_M * math.pi / 180.0
_LON_SCALE = _LAT_SCALE * math.cos(math.radians(ORIGIN_LAT))


def to_local(lat, lon):
    """Project lat/lon (degrees) to local metres east/north of the origin."""
    lat = np.asarray(lat, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    x = (lon - ORIGIN_LON) * _LON_SCALE
    y = (lat - ORIGIN_LAT) * _LAT_SCALE
    return x, y


def to_wgs84(x, y):
    """Inverse of :func:`to_local`."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    lon = x / _LON_SCALE + ORIGIN_LON
    lat = y / _LAT_SCALE + ORIGIN_LAT
    return lat, lon


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
    x, y = to_local(lat, lon)
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def sample_polyline(
    coords: list[tuple[float, float]], spacing_m: float
) -> np.ndarray:
    """Resample a (lat, lon) polyline at roughly ``spacing_m`` intervals.

    Returns an ``(n, 2)`` array of local x/y coordinates. Always includes the
    two endpoints, and never returns fewer than one point, so a zero-length
    edge still gets scored.
    """
    if not coords:
        return np.zeros((0, 2))
    lat = np.array([c[0] for c in coords], dtype=np.float64)
    lon = np.array([c[1] for c in coords], dtype=np.float64)
    x, y = to_local(lat, lon)
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

    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = to_local(lat, lon)
    seg = np.hypot(np.diff(x), np.diff(y))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    target = cum[-1] * fraction
    tx = float(np.interp(target, cum, x))
    ty = float(np.interp(target, cum, y))
    plat, plon = to_wgs84(tx, ty)
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

    lat = np.array([c[0] for c in coords])
    lon = np.array([c[1] for c in coords])
    x, y = to_local(lat, lon)
    seg = np.hypot(np.diff(x), np.diff(y))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    target = cum[-1] * fraction

    idx = int(np.searchsorted(cum, target, side="right")) - 1
    idx = min(max(idx, 0), len(coords) - 2)

    tx = float(np.interp(target, cum, x))
    ty = float(np.interp(target, cum, y))
    plat, plon = to_wgs84(tx, ty)
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
    x, y = to_local(lat, lon)
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
