"""Crime risk surface from MPD incident reports.

Three things distinguish this from "count crimes near the street":

* **Severity weighting.** A homicide and a stolen catalytic converter are not
  the same signal. Each offence carries a severity and, separately, a
  *pedestrian relevance* — how much it says about the risk to someone walking
  past. A burglary is a serious crime that tells you relatively little about
  street safety, and the two factors let us express that.
* **Recency decay.** Neighbourhoods change. Incidents decay exponentially so
  that last month weighs more than three years ago without throwing away the
  older data that gives the surface its stability.
* **Time of day.** MPD tags every incident with a shift (day / evening /
  midnight). We build separate day and night surfaces, because the streets
  that are risky at 2am are not the same ones that are risky at 2pm.

The surface is computed as a rasterised kernel density estimate rather than a
per-point sum. In a dense downtown cell there can be hundreds of incidents
within the kernel cutoff; a k-nearest-neighbour approximation would truncate
exactly where the signal is strongest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy import ndimage

from ..config import CRIME, CrimeConfig
from ..geo import sample_polyline, to_local

# MPD shift values that count as "night" for routing purposes.
NIGHT_SHIFTS = frozenset({"EVENING", "MIDNIGHT"})


@dataclass
class CrimeIncident:
    lat: float
    lon: float
    offense: str
    method: str
    shift: str
    reported_at: datetime


def incident_weight(
    incident: CrimeIncident, now: datetime, cfg: CrimeConfig = CRIME
) -> float:
    """Combined severity x relevance x weapon x recency weight."""
    offense = (incident.offense or "").strip().upper()
    severity = cfg.severity.get(offense, cfg.default_severity)
    relevance = cfg.pedestrian_relevance.get(offense, cfg.default_relevance)
    method = cfg.method_multiplier.get((incident.method or "").strip().upper(), 1.0)

    age_days = max(0.0, (now - incident.reported_at).total_seconds() / 86400.0)
    recency = math.exp(-math.log(2.0) * age_days / cfg.half_life_days)

    return severity * relevance * method * recency


class CrimeSurface:
    """A rasterised, severity-weighted crime density surface.

    Holds a day surface and a night surface on the same grid. Cell size is
    tied to the kernel bandwidth so the Gaussian is well resolved (bandwidth /
    6 gives sigma = 6 cells, plenty).
    """

    def __init__(
        self,
        incidents: list[CrimeIncident],
        now: datetime | None = None,
        cfg: CrimeConfig = CRIME,
        cell_size_m: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.now = now or datetime.now(timezone.utc)
        self.cell_size = cell_size_m or max(10.0, cfg.kernel_bandwidth_m / 6.0)

        if not incidents:
            self._empty = True
            self.day = np.zeros((1, 1), dtype=np.float32)
            self.night = np.zeros((1, 1), dtype=np.float32)
            self.x0 = self.y0 = 0.0
            self.day_scale = self.night_scale = 1.0
            return

        self._empty = False
        lat = np.array([i.lat for i in incidents], dtype=np.float64)
        lon = np.array([i.lon for i in incidents], dtype=np.float64)
        x, y = to_local(lat, lon)

        weights = np.array(
            [incident_weight(i, self.now, cfg) for i in incidents], dtype=np.float64
        )
        is_night = np.array(
            [(i.shift or "").strip().upper() in NIGHT_SHIFTS for i in incidents]
        )

        # Pad the grid so the kernel has room to spread at the edges.
        pad = cfg.kernel_cutoff_m + self.cell_size
        self.x0 = float(x.min() - pad)
        self.y0 = float(y.min() - pad)
        nx = int(math.ceil((float(x.max()) + pad - self.x0) / self.cell_size)) + 1
        ny = int(math.ceil((float(y.max()) + pad - self.y0) / self.cell_size)) + 1

        col = np.clip(((x - self.x0) / self.cell_size).astype(int), 0, nx - 1)
        row = np.clip(((y - self.y0) / self.cell_size).astype(int), 0, ny - 1)
        flat = row * nx + col

        # Night incidents also inform the day surface, and vice versa, but at
        # a discount: an area with a lot of nighttime robbery is not safe at
        # noon either, it is just less bad. A hard split would make each
        # surface noisier than the evidence supports.
        cross = 0.35
        day_w = np.where(is_night, weights * cross, weights)
        night_w = np.where(is_night, weights, weights * cross)

        day_grid = np.bincount(flat, weights=day_w, minlength=nx * ny).reshape(ny, nx)
        night_grid = np.bincount(
            flat, weights=night_w, minlength=nx * ny
        ).reshape(ny, nx)

        sigma_cells = cfg.kernel_bandwidth_m / self.cell_size
        truncate = cfg.kernel_cutoff_m / cfg.kernel_bandwidth_m
        self.day = ndimage.gaussian_filter(
            day_grid, sigma=sigma_cells, mode="constant", truncate=truncate
        ).astype(np.float32)
        self.night = ndimage.gaussian_filter(
            night_grid, sigma=sigma_cells, mode="constant", truncate=truncate
        ).astype(np.float32)

        # Normalise against a high percentile of the *populated* part of the
        # surface. Using the max would let a single extreme hotspot flatten
        # the rest of the city to near zero; using all cells including the
        # empty margin would drag the percentile down to nothing.
        self.day_scale = self._robust_scale(self.day)
        self.night_scale = self._robust_scale(self.night)

    def _robust_scale(self, grid: np.ndarray) -> float:
        populated = grid[grid > 0]
        if populated.size == 0:
            return 1.0
        scale = float(np.percentile(populated, self.cfg.normalisation_percentile))
        return scale if scale > 1e-9 else 1.0

    def sample(self, points_xy: np.ndarray, night: bool) -> np.ndarray:
        """Normalised density in [0, 1] at each local-metre point.

        Bilinear interpolation, so the surface is smooth across cell
        boundaries and a route does not get a step change in cost from
        crossing an arbitrary grid line.
        """
        if self._empty or len(points_xy) == 0:
            return np.zeros(len(points_xy), dtype=np.float32)

        grid = self.night if night else self.day
        scale = self.night_scale if night else self.day_scale
        ny, nx = grid.shape

        fx = (points_xy[:, 0] - self.x0) / self.cell_size
        fy = (points_xy[:, 1] - self.y0) / self.cell_size

        # map_coordinates takes (row, col) order.
        values = ndimage.map_coordinates(
            grid, np.vstack([fy, fx]), order=1, mode="constant", cval=0.0
        )
        return np.clip(values / scale, 0.0, 1.0).astype(np.float32)

    def score_segments(
        self, segment_coords: list[list[tuple[float, float]]], night: bool
    ) -> np.ndarray:
        """Mean normalised crime density along each segment."""
        n = len(segment_coords)
        out = np.zeros(n, dtype=np.float32)
        if n == 0 or self._empty:
            return out

        all_pts: list[np.ndarray] = []
        bounds: list[tuple[int, int]] = []
        cursor = 0
        for coords in segment_coords:
            pts = sample_polyline(coords, self.cfg.sample_spacing_m)
            all_pts.append(pts)
            bounds.append((cursor, cursor + len(pts)))
            cursor += len(pts)

        if cursor == 0:
            return out

        values = self.sample(np.vstack([p for p in all_pts if len(p)]), night)
        for i, (a, b) in enumerate(bounds):
            if b > a:
                out[i] = float(values[a:b].mean())
        return out
