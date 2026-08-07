"""Hexagonal binning of crime incidents, for the map overlay.

Why hexagons rather than squares: every neighbour of a hexagon is the same
distance away and shares a full edge, so a cluster reads the same regardless
of its orientation. A square grid has neighbours at two different distances
(edge vs corner), which makes diagonal clusters look weaker than identical
horizontal ones. It is the same reasoning behind H3, which is what SafeWalk AI
uses.

Two properties matter for this to feel right on a map:

* **The grid is anchored absolutely**, to the projection origin rather than to
  the requested viewport. Cells therefore stay put while you pan, instead of
  reflowing under your finger every time the bounding box shifts.
* **Cell size comes from a fixed ladder**, not a continuous function of zoom.
  A continuous size would re-bin on every pixel of a pinch; snapping to a
  ladder means the grid changes only at deliberate steps.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from ..cities import DC, CrimeVocabulary, PremisesWeights
from ..config import CRIME, CrimeConfig
from ..geo import Projection
from .crime_model import NIGHT_SHIFTS, incident_weight

# Cell circumradius options in metres. Chosen so each step is roughly 1.5x the
# last — enough of a jump to be visible, not so much that a zoom skips a level.
#
# The floor is 165m. Smaller cells look appealing zoomed in, but each one is a
# separate filled overlay on the map and render cost is what makes the grid
# stutter — the same mistake, at a smaller scale, as the per-street overlay
# this replaced. 110m was still too fine in practice.
SIZE_LADDER = (165.0, 250.0, 375.0, 560.0, 850.0, 1300.0, 2000.0)

# Hard ceiling on cells per response. Filled map polygons are expensive enough
# that this, not payload size, is the binding constraint. Measured against a
# real DC build rather than guessed: 320 still stuttered.
MAX_CELLS = 180

SQRT3 = math.sqrt(3.0)


def choose_radius(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    projection: Projection,
) -> float:
    """Smallest ladder size that keeps the viewport under ``MAX_CELLS``."""
    x0, y0 = projection.to_local(min_lat, min_lon)
    x1, y1 = projection.to_local(max_lat, max_lon)
    width = abs(float(x1) - float(x0))
    height = abs(float(y1) - float(y0))
    area = max(1.0, width * height)

    # Area of a regular hexagon with circumradius R is 3*sqrt(3)/2 * R^2.
    for radius in SIZE_LADDER:
        if area / (2.598 * radius * radius) <= MAX_CELLS:
            return radius
    return SIZE_LADDER[-1]


def axial_index(x: np.ndarray, y: np.ndarray, radius: float) -> tuple[np.ndarray, np.ndarray]:
    """Pointy-top axial hex coordinates for local-metre points."""
    q = (SQRT3 / 3.0 * x - y / 3.0) / radius
    r = (2.0 / 3.0 * y) / radius
    return _axial_round(q, r)


def _axial_round(q: np.ndarray, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Round fractional axial coordinates to the nearest hex.

    Done via cube coordinates: round all three, then correct whichever moved
    furthest so the constraint x + y + z = 0 still holds. Rounding the two
    axial components independently puts points in the wrong cell near edges.
    """
    cx = q
    cz = r
    cy = -cx - cz

    rx = np.round(cx)
    ry = np.round(cy)
    rz = np.round(cz)

    dx = np.abs(rx - cx)
    dy = np.abs(ry - cy)
    dz = np.abs(rz - cz)

    fix_x = (dx > dy) & (dx > dz)
    fix_y = (~fix_x) & (dy > dz)
    fix_z = (~fix_x) & (~fix_y)

    out_x = np.where(fix_x, -ry - rz, rx)
    out_z = np.where(fix_z, -rx - ry, rz)
    # ry is only needed to correct the others, so it is not returned.

    return out_x.astype(np.int64), out_z.astype(np.int64)


def axial_center(q: np.ndarray | int, r: np.ndarray | int, radius: float):
    """Local-metre centre of a pointy-top hex."""
    x = radius * SQRT3 * (np.asarray(q) + np.asarray(r) / 2.0)
    y = radius * 1.5 * np.asarray(r)
    return x, y


def hex_vertices(
    center_lat: float,
    center_lon: float,
    radius_m: float,
    projection: Projection,
) -> list[tuple[float, float]]:
    """The six corners of a pointy-top hex, as (lat, lon).

    Provided so the client draws exactly the cell the server binned into,
    rather than approximating it.
    """
    cx, cy = projection.to_local(center_lat, center_lon)
    points = []
    for i in range(6):
        angle = math.pi / 180.0 * (60 * i - 30)
        vx = float(cx) + radius_m * math.cos(angle)
        vy = float(cy) + radius_m * math.sin(angle)
        lat, lon = projection.to_wgs84(vx, vy)
        points.append((float(lat), float(lon)))
    return points


def readable_offense(code: str, vocabulary: CrimeVocabulary | None = None) -> str:
    """Turn a police offence code into something a person would say.

    Falls back to a tidied version of the code rather than showing it raw, so
    a new offence type the department starts publishing degrades to "Theft
    From Boat" rather than "THEFT F/BOAT".
    """
    vocab = vocabulary or DC.crime_vocabulary
    known = vocab.display_name.get(code)
    if known:
        return known
    cleaned = code.replace("/", " / ").replace("  ", " ").strip()
    return " ".join(word.capitalize() for word in cleaned.split()) or "Other"


@dataclass
class OffenseBreakdown:
    """One offence type's contribution to a cell."""

    offense: str
    # Human-readable form of `offense`, for display.
    display: str
    count: int
    category: str
    # Share of the cell's weighted intensity, 0-1. This is what makes the list
    # honest: a cell can be 40 car break-ins and 1 robbery, and the counts
    # alone would bury the fact that the robbery is most of the risk.
    share: float


@dataclass
class CrimeCell:
    """One hexagon's worth of aggregated incidents."""

    center_lat: float
    center_lon: float
    total: int
    # Severity-weighted intensity in [0, 1], normalised against the busiest
    # cell in the response. Incidents outside the requested lookback window
    # are excluded entirely rather than down-weighted.
    intensity: float
    # Ordered by weighted contribution, not by raw count.
    by_offense: list[OffenseBreakdown]
    night_count: int
    # Violent and sexual offences only.
    serious_count: int
    latest: datetime | None

    @property
    def night_share(self) -> float:
        return self.night_count / self.total if self.total else 0.0


class CrimeIndex:
    """Holds incident points in flat arrays and bins them on demand.

    Binning a hundred thousand points is a handful of numpy operations, so
    this is fast enough to do per request and avoids precomputing a grid at
    every possible zoom.
    """

    def __init__(
        self,
        lat: np.ndarray,
        lon: np.ndarray,
        weight: np.ndarray,
        offense_ids: np.ndarray,
        is_night: np.ndarray,
        timestamps: np.ndarray,
        offenses: list[str],
        cfg: CrimeConfig = CRIME,
        projection: Projection | None = None,
        vocabulary: CrimeVocabulary | None = None,
        city_slug: str = DC.slug,
    ) -> None:
        self.cfg = cfg
        self.vocabulary = vocabulary or DC.crime_vocabulary
        self.city_slug = city_slug
        # The frame the binning happens in. The grid is anchored to this
        # origin rather than to the viewport, which is what keeps cells still
        # while the map pans — so it has to be the city's own frame, and it is
        # persisted alongside the points.
        self.projection = projection or Projection(0.0, 0.0)
        # Parallel to `offenses`: the broad category of each offence name.
        self.categories = [
            self.vocabulary.category.get(name, cfg.default_category)
            for name in offenses
        ]
        self.lat = lat
        self.lon = lon
        self.weight = weight
        self.offense_ids = offense_ids
        self.is_night = is_night
        self.timestamps = timestamps  # epoch seconds
        self.offenses = offenses

        x, y = self.projection.to_local(lat, lon)
        self.x = np.asarray(x)
        self.y = np.asarray(y)

    @property
    def count(self) -> int:
        return len(self.lat)

    @classmethod
    def from_incidents(
        cls,
        incidents: list,
        now: datetime | None = None,
        cfg: CrimeConfig = CRIME,
        projection: Projection | None = None,
        vocabulary: CrimeVocabulary | None = None,
        city_slug: str = DC.slug,
        premises: PremisesWeights | None = None,
    ) -> CrimeIndex:
        if not incidents:
            empty = np.zeros(0)
            return cls(
                empty, empty, empty, empty.astype(np.int16), empty.astype(bool),
                empty, [], cfg, projection, vocabulary, city_slug,
            )

        now = now or datetime.now(incidents[0].reported_at.tzinfo)
        vocab: dict[str, int] = {}
        offenses: list[str] = []
        ids = []
        for inc in incidents:
            name = (inc.offense or "UNKNOWN").strip().upper() or "UNKNOWN"
            if name not in vocab:
                vocab[name] = len(offenses)
                offenses.append(name)
            ids.append(vocab[name])

        return cls(
            lat=np.array([i.lat for i in incidents], dtype=np.float64),
            lon=np.array([i.lon for i in incidents], dtype=np.float64),
            # Undecayed: the lookback window the caller picks is the recency
            # filter, so an incident either counts or it does not.
            weight=np.array(
                [
                    incident_weight(
                        i, now, vocabulary, cfg, decay=False, premises=premises
                    )
                    for i in incidents
                ],
                dtype=np.float32,
            ),
            offense_ids=np.array(ids, dtype=np.int16),
            is_night=np.array(
                [(i.shift or "").strip().upper() in NIGHT_SHIFTS for i in incidents],
                dtype=bool,
            ),
            timestamps=np.array(
                [i.reported_at.timestamp() for i in incidents], dtype=np.float64
            ),
            offenses=offenses,
            cfg=cfg,
            projection=projection,
            vocabulary=vocabulary,
            city_slug=city_slug,
        )

    def cells(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        radius_m: float | None = None,
        night_only: bool = False,
        max_offense_kinds: int = 6,
        window_days: int | None = None,
        now: datetime | None = None,
    ) -> tuple[list[CrimeCell], float]:
        """Aggregate incidents in a bounding box into hexes.

        ``window_days`` restricts the aggregation to incidents reported in the
        last N days; ``None`` counts everything held. The map and the router
        take the same window, so the grid a user is looking at is the data
        their route was scored against.

        Returns the cells and the radius actually used, so the client can draw
        the hexagons at the right size.
        """
        radius = radius_m or choose_radius(
            min_lat, min_lon, max_lat, max_lon, self.projection
        )
        if self.count == 0:
            return [], radius

        # Pad by one cell so hexes straddling the edge are still complete.
        pad_lat = radius / 111_320.0
        pad_lon = pad_lat / max(0.1, math.cos(math.radians((min_lat + max_lat) / 2)))

        mask = (
            (self.lat >= min_lat - pad_lat)
            & (self.lat <= max_lat + pad_lat)
            & (self.lon >= min_lon - pad_lon)
            & (self.lon <= max_lon + pad_lon)
        )
        if night_only:
            mask &= self.is_night
        if window_days is not None:
            reference = now or datetime.now(UTC)
            mask &= self.timestamps >= reference.timestamp() - window_days * 86400.0
        if not mask.any():
            return [], radius

        idx = np.nonzero(mask)[0]
        q, r = axial_index(self.x[idx], self.y[idx], radius)

        # Pack the (q, r) pair into one sortable key.
        key = (q.astype(np.int64) << 32) ^ (r.astype(np.int64) & 0xFFFFFFFF)
        order = np.argsort(key, kind="stable")
        key_sorted = key[order]
        idx_sorted = idx[order]

        boundaries = np.flatnonzero(np.diff(key_sorted)) + 1
        starts = np.concatenate([[0], boundaries])
        ends = np.concatenate([boundaries, [len(key_sorted)]])

        q_sorted = q[order]
        r_sorted = r[order]

        raw: list[tuple] = []
        max_intensity = 0.0
        for s, e in zip(starts, ends, strict=True):
            members = idx_sorted[s:e]
            intensity = float(self.weight[members].sum())
            max_intensity = max(max_intensity, intensity)
            raw.append((int(q_sorted[s]), int(r_sorted[s]), members, intensity))

        scale = max_intensity if max_intensity > 0 else 1.0

        cells: list[CrimeCell] = []
        for cq, cr, members, intensity in raw:
            cx, cy = axial_center(cq, cr, radius)
            clat, clon = self.projection.to_wgs84(cx, cy)

            member_offenses = self.offense_ids[members]
            member_weights = self.weight[members]
            counts = Counter(member_offenses.tolist())

            # Weighted contribution per offence type, so the list can be
            # ordered by what actually drives the cell's risk. A cell can be
            # forty car break-ins and one robbery, and ordering by raw count
            # would bury the fact that the robbery is most of the risk.
            #
            # Grouped by *display name*, not by raw code. Police feeds split
            # categories in ways that are legally meaningful and useless in a
            # two-line summary — NYPD separates rape from its broader
            # sexual-offence bucket, and showing both as adjacent rows reading
            # "Sexual offense" would look like a bug. Any two codes the
            # vocabulary labels the same are one row here.
            grouped: dict[str, dict] = {}
            for oid, count in counts.items():
                label = readable_offense(self.offenses[oid], self.vocabulary)
                entry = grouped.setdefault(
                    label,
                    {
                        "offense": self.offenses[oid],
                        "count": 0,
                        "weight": 0.0,
                        "category": self.categories[oid],
                    },
                )
                entry["count"] += int(count)
                entry["weight"] += float(
                    member_weights[member_offenses == oid].sum()
                )
                # Where merged codes disagree on category, the more serious
                # one wins — a group containing anything violent is violent.
                if self.categories[oid] in self.cfg.serious_categories:
                    entry["category"] = self.categories[oid]

            total_weight = sum(e["weight"] for e in grouped.values()) or 1.0

            breakdown = [
                OffenseBreakdown(
                    offense=entry["offense"],
                    display=label,
                    count=entry["count"],
                    category=entry["category"],
                    share=round(entry["weight"] / total_weight, 4),
                )
                for label, entry in sorted(
                    grouped.items(), key=lambda kv: -kv[1]["weight"]
                )
            ][:max_offense_kinds]

            serious = sum(
                int(counts[oid])
                for oid in counts
                if self.categories[oid] in self.cfg.serious_categories
            )

            latest_ts = float(self.timestamps[members].max())
            cells.append(
                CrimeCell(
                    center_lat=float(clat),
                    center_lon=float(clon),
                    total=int(len(members)),
                    intensity=round(intensity / scale, 4),
                    by_offense=breakdown,
                    night_count=int(self.is_night[members].sum()),
                    serious_count=serious,
                    latest=datetime.fromtimestamp(latest_ts, tz=UTC),
                )
            )

        cells.sort(key=lambda c: c.intensity, reverse=True)
        return cells, radius

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path) -> None:
        np.savez_compressed(
            path,
            lat=self.lat,
            lon=self.lon,
            weight=self.weight,
            offense_ids=self.offense_ids,
            is_night=self.is_night,
            timestamps=self.timestamps,
            offenses=np.array(self.offenses, dtype=object),
            projection=np.array(
                [self.projection.origin_lat, self.projection.origin_lon]
            ),
            # The city slug, so a loaded index reads its offence codes through
            # the vocabulary they were written with rather than the default
            # city's — which would label every NYPD offence by its raw code.
            city=np.array(self.city_slug),
        )

    @classmethod
    def load(cls, path) -> CrimeIndex:
        from ..cities import CITIES  # noqa: PLC0415 — avoids an import cycle

        z = np.load(path, allow_pickle=True)
        slug = str(z["city"]) if "city" in z else DC.slug
        city = CITIES.get(slug, DC)
        return cls(
            lat=z["lat"],
            lon=z["lon"],
            weight=z["weight"],
            offense_ids=z["offense_ids"],
            is_night=z["is_night"],
            timestamps=z["timestamps"],
            offenses=[str(s) for s in z["offenses"]],
            projection=(
                Projection(float(z["projection"][0]), float(z["projection"][1]))
                if "projection" in z
                else None
            ),
            vocabulary=city.crime_vocabulary,
            city_slug=city.slug,
        )
