"""Automated licence-plate-reader (ALPR) camera exposure.

This is a *privacy* layer, deliberately separate from the safety model. A
Flock camera on a pole is not a danger to a pedestrian, and folding it into a
"safety score" would make that score mean something muddier than it should.
It gets its own per-segment attribute, its own map overlay, and an opt-in
cost term.

Camera positions come from the OpenStreetMap ALPR tagging convention that the
DeFlock project popularised:

    man_made=surveillance
    surveillance:type=ALPR
    surveillance=public
    direction=<degrees or compass point>   # which way it faces
    operator=Flock Safety

Exposure is directional. A camera pointed north down a one-way street does not
see the parallel street a block south, so treating every camera as an
omnidirectional blob would both overstate coverage and make the avoidance
routing take pointless detours. Where a camera has no mapped direction we fall
back to omnidirectional at a discount.

Important limitation, and it belongs in the UI as well as here: this data is
crowdsourced and incomplete. A segment scoring zero means no camera has been
*mapped* there, not that no camera exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from ..config import CAMERAS, CameraConfig
from ..geo import Projection, sample_polyline

_MAX_NEIGHBOURS = 8

# OSM `direction` accepts compass points as well as degrees.
_COMPASS = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}


@dataclass
class AlprCamera:
    lat: float
    lon: float
    direction_deg: float | None  # None = unknown/omnidirectional
    operator: str = ""
    osm_id: str = ""

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "direction": self.direction_deg,
            "operator": self.operator,
            "id": self.osm_id,
        }


def parse_direction(raw: str | None) -> float | None:
    """Parse an OSM ``direction`` value into degrees, or None if unusable."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    if text in _COMPASS:
        return _COMPASS[text]
    try:
        return float(text) % 360.0
    except ValueError:
        return None


def visibility(
    dist_m: np.ndarray,
    bearing_cam_to_point: np.ndarray,
    cam_direction: np.ndarray,
    has_direction: np.ndarray,
    cfg: CameraConfig = CAMERAS,
) -> np.ndarray:
    """Per-(point, camera) exposure in [0, 1].

    Combines a distance falloff with an angular term that tapers toward the
    edge of the field of view rather than cutting off hard — the mapped
    bearing is an estimate, so a step function at exactly the FOV edge would
    imply more precision than the data has.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        near = np.clip(1.0 - dist_m / cfg.range_m, 0.0, 1.0)
        distance_term = near**cfg.falloff_exponent

    # Angular offset between where the camera looks and where the point is.
    delta = np.abs(
        (bearing_cam_to_point - cam_direction + 180.0) % 360.0 - 180.0
    )
    # Cosine taper across the FOV: full weight on axis, zero at the edge.
    angular = np.clip(
        np.cos(np.pi * 0.5 * np.minimum(delta / cfg.fov_half_angle_deg, 1.0)), 0.0, 1.0
    )

    directed = distance_term * angular
    undirected = distance_term * cfg.undirected_weight
    return np.where(has_direction, directed, undirected)


def score_segments(
    segment_coords: list[list[tuple[float, float]]],
    cameras: list[AlprCamera],
    projection: Projection,
    cfg: CameraConfig = CAMERAS,
) -> np.ndarray:
    """Per-segment ALPR exposure in [0, 1].

    The segment score is the *maximum* over its sample points, not the mean.
    Walking through one camera's cone is a discrete privacy event; averaging
    it over a long segment would let a 400m street dilute a camera at its
    corner into near-nothing, and the router would then happily walk you past
    it.
    """
    n = len(segment_coords)
    out = np.zeros(n, dtype=np.float32)
    if n == 0 or not cameras:
        return out

    cam_lat = np.array([c.lat for c in cameras], dtype=np.float64)
    cam_lon = np.array([c.lon for c in cameras], dtype=np.float64)
    cx, cy = projection.to_local(cam_lat, cam_lon)
    tree = cKDTree(np.column_stack([cx, cy]))

    cam_dir = np.array(
        [c.direction_deg if c.direction_deg is not None else 0.0 for c in cameras],
        dtype=np.float64,
    )
    cam_has_dir = np.array(
        [c.direction_deg is not None for c in cameras], dtype=bool
    )

    all_pts: list[np.ndarray] = []
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for coords in segment_coords:
        pts = sample_polyline(coords, cfg.sample_spacing_m, projection)
        all_pts.append(pts)
        bounds.append((cursor, cursor + len(pts)))
        cursor += len(pts)

    if cursor == 0:
        return out

    samples = np.vstack([p for p in all_pts if len(p)])
    k = min(_MAX_NEIGHBOURS, len(cameras))
    dist, idx = tree.query(
        samples, k=k, distance_upper_bound=cfg.range_m, workers=-1
    )
    if k == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    valid = np.isfinite(dist)
    safe_idx = np.where(valid, idx, 0)

    # Bearing from camera to sample point, in the local planar frame. North is
    # +y, east is +x, and bearings run clockwise from north.
    dx = samples[:, 0][:, None] - cx[safe_idx]
    dy = samples[:, 1][:, None] - cy[safe_idx]
    bearing = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0

    exposure = visibility(
        np.where(valid, dist, np.inf),
        bearing,
        cam_dir[safe_idx],
        cam_has_dir[safe_idx],
        cfg,
    )
    exposure = np.where(valid, exposure, 0.0)
    point_exposure = exposure.max(axis=1)

    for i, (a, b) in enumerate(bounds):
        if b > a:
            out[i] = float(point_exposure[a:b].max())

    return np.clip(out, 0.0, 1.0).astype(np.float32)


def cameras_in_bbox(
    cameras: list[AlprCamera],
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    limit: int = 2000,
) -> list[AlprCamera]:
    """Cameras inside a bounding box, for the map overlay."""
    hits = [
        c
        for c in cameras
        if min_lat <= c.lat <= max_lat and min_lon <= c.lon <= max_lon
    ]
    return hits[:limit]


def coverage_along_route(
    coords: list[tuple[float, float]],
    cameras: list[AlprCamera],
    projection: Projection,
    cfg: CameraConfig = CAMERAS,
) -> dict:
    """Summarise camera exposure over a full route, for the UI.

    Returns the number of distinct cameras whose cone the route passes
    through and the fraction of the route's length under any coverage.
    """
    if not cameras or len(coords) < 2:
        return {"cameras_passed": 0, "fraction_covered": 0.0}

    cam_lat = np.array([c.lat for c in cameras], dtype=np.float64)
    cam_lon = np.array([c.lon for c in cameras], dtype=np.float64)
    cx, cy = projection.to_local(cam_lat, cam_lon)
    tree = cKDTree(np.column_stack([cx, cy]))

    cam_dir = np.array(
        [c.direction_deg if c.direction_deg is not None else 0.0 for c in cameras]
    )
    cam_has_dir = np.array([c.direction_deg is not None for c in cameras], dtype=bool)

    samples = sample_polyline(coords, cfg.sample_spacing_m, projection)
    if len(samples) == 0:
        return {"cameras_passed": 0, "fraction_covered": 0.0}

    k = min(_MAX_NEIGHBOURS, len(cameras))
    dist, idx = tree.query(samples, k=k, distance_upper_bound=cfg.range_m, workers=-1)
    if k == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    valid = np.isfinite(dist)
    safe_idx = np.where(valid, idx, 0)
    dx = samples[:, 0][:, None] - cx[safe_idx]
    dy = samples[:, 1][:, None] - cy[safe_idx]
    bearing = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0

    exposure = visibility(
        np.where(valid, dist, np.inf), bearing, cam_dir[safe_idx], cam_has_dir[safe_idx], cfg
    )
    exposure = np.where(valid, exposure, 0.0)

    # "Passed" means meaningfully inside a cone, not merely within range.
    seen = np.unique(safe_idx[(exposure > 0.25) & valid])
    covered = float((exposure.max(axis=1) > 0.15).mean())

    return {"cameras_passed": int(seen.size), "fraction_covered": round(covered, 3)}
