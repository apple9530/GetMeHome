"""Street lighting scores from the DDOT streetlight inventory.

Each lamp is treated as a point source and we compute a relative illuminance
at street level for sample points along every segment. The units are
arbitrary — what matters is the ratio against a reference value calibrated so
that a normally-spaced residential street scores highly and an unlit one
scores near zero.

The reason this is modelled physically rather than as "count lamps within
50m" is that lamp *spacing* is what determines whether a street has dark gaps,
and a count cannot see gaps. A street with four lamps clustered at one end and
80 metres of nothing is not equivalent to a street with four evenly spaced
lamps, but a count says it is.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from ..config import LIGHTING, LightingConfig
from ..geo import sample_polyline, to_local

# How many nearby lamps to consider per sample point. Contributions fall off
# as 1/d^2, so beyond the nearest dozen or so the sum has converged.
_MAX_NEIGHBOURS = 16


class StreetLight:
    """A single lamp."""

    __slots__ = ("lat", "lon", "lumens", "height_m")

    def __init__(
        self, lat: float, lon: float, lumens: float, height_m: float
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.lumens = lumens
        self.height_m = height_m


def lumens_for_type(
    lamp_type: str | None, wattage: float | None, cfg: LightingConfig = LIGHTING
) -> float:
    """Best-effort lumen output for a DDOT lamp record.

    DDOT's schema has shifted over the years and different records populate
    different fields, so this takes whatever is present and degrades to a
    sensible default rather than failing.
    """
    if lamp_type:
        key = str(lamp_type).strip().lower()
        for needle, lumens in cfg.lumens_by_type.items():
            if needle in key:
                base = lumens
                break
        else:
            base = cfg.default_lumens
    else:
        base = cfg.default_lumens

    # Where wattage is recorded, scale within the type. LED efficacy is around
    # 110 lm/W and HPS around 100, so a flat 100 lm/W is close enough given
    # everything else here is an approximation.
    if wattage:
        try:
            w = float(wattage)
            if 10.0 <= w <= 1500.0:
                return max(base * 0.35, min(base * 2.5, w * 100.0))
        except (TypeError, ValueError):
            pass
    return base


def illuminance_at(
    points_xy: np.ndarray,
    tree: cKDTree,
    lumens: np.ndarray,
    heights: np.ndarray,
    cfg: LightingConfig = LIGHTING,
) -> np.ndarray:
    """Relative illuminance at each of ``points_xy`` (local metres).

    Inverse-square falloff with the mounting height folded in, so a point
    directly beneath a lamp gets a large but finite value rather than
    dividing by zero.
    """
    if len(points_xy) == 0:
        return np.zeros(0, dtype=np.float64)
    if tree is None or len(lumens) == 0:
        return np.zeros(len(points_xy), dtype=np.float64)

    k = min(_MAX_NEIGHBOURS, len(lumens))
    dist, idx = tree.query(
        points_xy, k=k, distance_upper_bound=cfg.search_radius_m, workers=-1
    )
    if k == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    # Points with fewer than k neighbours in range come back with index ==
    # len(tree.data) and distance == inf. Mask them out.
    valid = np.isfinite(dist)
    safe_idx = np.where(valid, idx, 0)

    lm = lumens[safe_idx]
    h = heights[safe_idx]
    d2 = np.where(valid, dist, 0.0) ** 2

    contrib = np.where(valid, lm / (d2 + h * h), 0.0)
    return contrib.sum(axis=1)


def score_segments(
    segment_coords: list[list[tuple[float, float]]],
    lights: list[StreetLight],
    cfg: LightingConfig = LIGHTING,
) -> np.ndarray:
    """Lighting score in [0, 1] per segment. 0 is unlit, 1 is brightly lit.

    The per-segment score blends the mean illuminance with the 20th
    percentile. Using the mean alone lets one bright lamp mask a long dark
    stretch, which is exactly the situation a pedestrian cares about.
    """
    n = len(segment_coords)
    scores = np.zeros(n, dtype=np.float32)
    if n == 0:
        return scores

    if lights:
        lx, ly = to_local(
            np.array([lamp.lat for lamp in lights]), np.array([lamp.lon for lamp in lights])
        )
        tree = cKDTree(np.column_stack([lx, ly]))
        lumens = np.array([lamp.lumens for lamp in lights], dtype=np.float64)
        heights = np.array([lamp.height_m for lamp in lights], dtype=np.float64)
    else:
        tree, lumens, heights = None, np.zeros(0), np.zeros(0)

    # Sample every segment in one pass so the KD-tree query is a single
    # vectorised call rather than one call per segment.
    all_samples: list[np.ndarray] = []
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for coords in segment_coords:
        pts = sample_polyline(coords, cfg.sample_spacing_m)
        all_samples.append(pts)
        bounds.append((cursor, cursor + len(pts)))
        cursor += len(pts)

    if cursor == 0:
        return scores

    stacked = np.vstack([p for p in all_samples if len(p)])
    illum = illuminance_at(stacked, tree, lumens, heights, cfg)

    # Saturating transform: 1 - exp(-I/I_ref). Smooth, bounded, and reaches
    # 0.63 at the reference illuminance rather than clipping hard.
    point_scores = 1.0 - np.exp(-illum / cfg.reference_illuminance)

    w = cfg.dark_gap_weight
    for i, (a, b) in enumerate(bounds):
        if b <= a:
            continue
        s = point_scores[a:b]
        scores[i] = (1.0 - w) * float(s.mean()) + w * float(np.percentile(s, 20))

    return np.clip(scores, 0.0, 1.0).astype(np.float32)
