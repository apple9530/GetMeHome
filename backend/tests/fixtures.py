"""Synthetic fixtures.

The real DC build needs network access to DDOT, MPD, OSM and WMATA. These
fixtures let the routing, scoring and instruction logic be tested for real
without any of that, on a graph whose correct answers can be worked out by
hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from getmehome.geo import polyline_length_m
from getmehome.graph.model import RawSegment, build_graph
from getmehome.safety.cameras import AlprCamera
from getmehome.safety.crime_model import CrimeIncident
from getmehome.safety.lighting import StreetLight

# A regular grid centred near Logan Circle, DC. Spacing is about 100m so the
# numbers stay easy to reason about.
GRID_ORIGIN_LAT = 38.9000
GRID_ORIGIN_LON = -77.0300
LAT_STEP = 0.0009  # ~100m
LON_STEP = 0.00115  # ~100m at this latitude


def grid_node_id(row: int, col: int, cols: int) -> int:
    return row * cols + col


def grid_coords(row: int, col: int) -> tuple[float, float]:
    return (GRID_ORIGIN_LAT + row * LAT_STEP, GRID_ORIGIN_LON + col * LON_STEP)


def build_grid_graph(rows: int = 7, cols: int = 7):
    """A ``rows`` x ``cols`` street grid.

    North-south streets are named ``1st St`` etc; east-west are lettered
    ``A St`` etc, which mirrors DC's actual naming and makes the generated
    turn instructions readable in test output.
    """
    node_coords: dict[int, tuple[float, float]] = {}
    for r in range(rows):
        for c in range(cols):
            node_coords[grid_node_id(r, c, cols)] = grid_coords(r, c)

    segments: list[RawSegment] = []

    def add(a: int, b: int, name: str, highway: str = "residential", **tags):
        ca, cb = node_coords[a], node_coords[b]
        segments.append(
            RawSegment(
                node_a=a,
                node_b=b,
                coords=[ca, cb],
                length_m=polyline_length_m([ca, cb]),
                name=name,
                highway=highway,
                tags=dict(tags),
            )
        )

    letters = "ABCDEFGHIJ"
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:  # east-west
                add(
                    grid_node_id(r, c, cols),
                    grid_node_id(r, c + 1, cols),
                    f"{letters[r]} St NW",
                )
            if r + 1 < rows:  # north-south
                add(
                    grid_node_id(r, c, cols),
                    grid_node_id(r + 1, c, cols),
                    f"{c + 1}th St NW",
                )

    return build_graph(node_coords, segments, meta={"fixture": "grid"})


def dense_lights_along_column(col: int, rows: int = 7, cols: int = 7):
    """Streetlights every ~25m up one column of the grid."""
    lights: list[StreetLight] = []
    for r in range(rows - 1):
        lat0, lon0 = grid_coords(r, col)
        lat1, lon1 = grid_coords(r + 1, col)
        for k in range(4):
            f = k / 4.0
            lights.append(
                StreetLight(
                    lat=lat0 + (lat1 - lat0) * f,
                    lon=lon0 + (lon1 - lon0) * f,
                    lumens=11000.0,
                    height_m=8.0,
                )
            )
    return lights


def crime_cluster_at(row: int, col: int, count: int = 40, offense: str = "ROBBERY"):
    """A cluster of incidents centred on one grid node."""
    lat, lon = grid_coords(row, col)
    now = datetime.now(UTC)
    out: list[CrimeIncident] = []
    for i in range(count):
        out.append(
            CrimeIncident(
                lat=lat + (i % 5 - 2) * 0.00008,
                lon=lon + (i % 7 - 3) * 0.00009,
                offense=offense,
                method="GUN" if i % 4 == 0 else "OTHERS",
                shift="MIDNIGHT" if i % 2 else "EVENING",
                reported_at=now - timedelta(days=i * 5),
            )
        )
    return out


def camera_facing(row: int, col: int, direction_deg: float | None = 0.0):
    lat, lon = grid_coords(row, col)
    return AlprCamera(
        lat=lat,
        lon=lon,
        direction_deg=direction_deg,
        operator="Flock Safety",
        osm_id=f"node/{row}{col}",
    )
