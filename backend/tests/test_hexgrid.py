"""Tests for hex binning of crime incidents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from getmehome.geo import haversine_m, to_local
from getmehome.safety.crime_model import CrimeIncident
from getmehome.safety.hexgrid import (
    MAX_CELLS,
    SIZE_LADDER,
    CrimeIndex,
    axial_center,
    axial_index,
    choose_radius,
    hex_vertices,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)
CENTER_LAT, CENTER_LON = 38.9050, -77.0300


def incidents_at(lat, lon, count, offense="ROBBERY", shift="MIDNIGHT", spread=0.0):
    return [
        CrimeIncident(
            lat=lat + (i % 3 - 1) * spread,
            lon=lon + (i % 5 - 2) * spread,
            offense=offense,
            method="GUN" if i % 3 == 0 else "OTHERS",
            shift=shift,
            reported_at=NOW - timedelta(days=i * 3),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Hex geometry
# ---------------------------------------------------------------------------


def test_axial_round_trip_recovers_the_cell():
    """A hex centre must index back to its own hex."""
    radius = 200.0
    qs = np.array([0, 1, -3, 5, -7, 12])
    rs = np.array([0, -2, 4, 5, -1, -9])

    x, y = axial_center(qs, rs, radius)
    back_q, back_r = axial_index(x, y, radius)

    assert np.array_equal(back_q, qs)
    assert np.array_equal(back_r, rs)


def test_points_near_a_centre_land_in_that_cell():
    """Anything well inside a hex belongs to it."""
    radius = 200.0
    cx, cy = axial_center(3, -2, radius)

    # The inradius is sqrt(3)/2 * R; stay comfortably inside it.
    offsets = [(0, 0), (50, 0), (0, -50), (-40, 40), (60, 60)]
    for dx, dy in offsets:
        q, r = axial_index(np.array([cx + dx]), np.array([cy + dy]), radius)
        assert (int(q[0]), int(r[0])) == (3, -2), f"offset {(dx, dy)} escaped its cell"


def test_every_point_lands_in_exactly_one_cell():
    """The tiling covers the plane with no gaps and no double-counting.

    Binning is a pure function, so 'exactly one' is automatic — what this
    really checks is that no point produces a degenerate or absurd index, and
    that the assigned centre is always the nearest one.
    """
    rng = np.random.default_rng(11)
    radius = 150.0
    x = rng.uniform(-5000, 5000, 4000)
    y = rng.uniform(-5000, 5000, 4000)

    q, r = axial_index(x, y, radius)
    cx, cy = axial_center(q, r, radius)

    # No point may be further from its own centre than the circumradius.
    distance = np.hypot(x - cx, y - cy)
    assert distance.max() <= radius + 1e-6, (
        f"a point sat {distance.max():.1f}m from its cell centre, "
        f"beyond the {radius}m circumradius"
    )


def test_hex_vertices_sit_at_the_radius():
    verts = hex_vertices(CENTER_LAT, CENTER_LON, 250.0)
    assert len(verts) == 6
    for lat, lon in verts:
        assert haversine_m(CENTER_LAT, CENTER_LON, lat, lon) == pytest.approx(250, rel=0.02)


def test_radius_ladder_keeps_cell_count_bounded():
    """Zooming out must pick a bigger cell, never return thousands of them."""
    # The whole District.
    wide = choose_radius(38.79, -77.12, 39.00, -76.91)
    # A few blocks.
    tight = choose_radius(38.900, -77.035, 38.910, -77.025)

    assert wide in SIZE_LADDER
    assert tight in SIZE_LADDER
    assert wide > tight

    x0, y0 = to_local(38.79, -77.12)
    x1, y1 = to_local(39.00, -76.91)
    area = abs(float(x1) - float(x0)) * abs(float(y1) - float(y0))
    assert area / (2.598 * wide * wide) <= MAX_CELLS


def test_grid_is_anchored_not_relative_to_the_viewport():
    """Panning must not shift the cells under the user's finger."""
    index = CrimeIndex.from_incidents(
        incidents_at(CENTER_LAT, CENTER_LON, 40, spread=0.002), now=NOW
    )

    a, radius_a = index.cells(38.900, -77.040, 38.912, -77.020, radius_m=200.0)
    # Same zoom, viewport nudged east.
    b, radius_b = index.cells(38.900, -77.038, 38.912, -77.018, radius_m=200.0)

    assert radius_a == radius_b
    centres_a = {(round(c.center_lat, 6), round(c.center_lon, 6)) for c in a}
    centres_b = {(round(c.center_lat, 6), round(c.center_lon, 6)) for c in b}
    # The overlapping region must reuse identical cell centres.
    assert centres_a & centres_b, "cells reflowed when the viewport moved"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_cells_aggregate_counts_and_offenses():
    incidents = (
        incidents_at(CENTER_LAT, CENTER_LON, 12, offense="ROBBERY")
        + incidents_at(CENTER_LAT, CENTER_LON, 5, offense="HOMICIDE")
    )
    index = CrimeIndex.from_incidents(incidents, now=NOW)

    cells, radius = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)

    assert cells
    busiest = cells[0]
    assert busiest.total == 17
    breakdown = dict(busiest.by_offense)
    assert breakdown["ROBBERY"] == 12
    assert breakdown["HOMICIDE"] == 5
    # Most common first.
    assert busiest.by_offense[0][0] == "ROBBERY"


def test_intensity_is_severity_weighted_not_a_raw_count():
    """Five homicides must outrank twenty petty thefts."""
    homicides = incidents_at(38.9060, -77.0300, 5, offense="HOMICIDE")
    thefts = incidents_at(38.9000, -77.0300, 20, offense="THEFT/OTHER")
    index = CrimeIndex.from_incidents(homicides + thefts, now=NOW)

    cells, _ = index.cells(38.890, -77.040, 38.915, -77.020, radius_m=200.0)
    by_total = {c.total: c for c in cells}

    assert 5 in by_total and 20 in by_total
    assert by_total[5].intensity > by_total[20].intensity, (
        "severity weighting is not being applied"
    )
    # The busiest cell is normalised to 1.
    assert max(c.intensity for c in cells) == pytest.approx(1.0)


def test_night_filter_and_share():
    index = CrimeIndex.from_incidents(
        incidents_at(CENTER_LAT, CENTER_LON, 10, shift="MIDNIGHT")
        + incidents_at(CENTER_LAT, CENTER_LON, 10, shift="DAY"),
        now=NOW,
    )

    everything, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)
    nights, _ = index.cells(
        38.895, -77.040, 38.915, -77.020, radius_m=400.0, night_only=True
    )

    assert everything[0].total == 20
    assert everything[0].night_share == pytest.approx(0.5)
    assert nights[0].total == 10
    assert nights[0].night_share == pytest.approx(1.0)


def test_latest_incident_is_reported():
    index = CrimeIndex.from_incidents(
        incidents_at(CENTER_LAT, CENTER_LON, 10), now=NOW
    )
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)
    # The fixture's newest incident is `NOW` itself.
    assert cells[0].latest.date() == NOW.date()


def test_incidents_outside_the_box_are_excluded():
    index = CrimeIndex.from_incidents(
        incidents_at(38.9800, -76.9200, 30), now=NOW  # far north-east
    )
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=200.0)
    assert cells == []


def test_empty_index_is_safe():
    index = CrimeIndex.from_incidents([], now=NOW)
    assert index.count == 0
    cells, radius = index.cells(38.895, -77.040, 38.915, -77.020)
    assert cells == []
    assert radius > 0


def test_round_trip_through_disk(tmp_path):
    index = CrimeIndex.from_incidents(
        incidents_at(CENTER_LAT, CENTER_LON, 25, spread=0.001), now=NOW
    )
    path = tmp_path / "points.npz"
    index.save(path)

    loaded = CrimeIndex.load(path)
    assert loaded.count == index.count
    assert loaded.offenses == index.offenses

    before, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=250.0)
    after, _ = loaded.cells(38.895, -77.040, 38.915, -77.020, radius_m=250.0)
    assert [c.total for c in before] == [c.total for c in after]


def test_large_dataset_stays_under_the_cell_cap():
    """A city-wide view of a realistic dataset must not flood the client."""
    rng = np.random.default_rng(3)
    incidents = [
        CrimeIncident(
            lat=float(lat), lon=float(lon),
            offense="THEFT/OTHER", method="OTHERS", shift="DAY",
            reported_at=NOW - timedelta(days=int(d)),
        )
        for lat, lon, d in zip(
            rng.uniform(38.80, 39.00, 40_000),
            rng.uniform(-77.12, -76.91, 40_000),
            rng.integers(0, 1000, 40_000),
            strict=True,
        )
    ]
    index = CrimeIndex.from_incidents(incidents, now=NOW)

    cells, radius = index.cells(38.79, -77.13, 39.01, -76.90)
    assert len(cells) <= MAX_CELLS, f"{len(cells)} cells would stall the map"
    assert radius >= SIZE_LADDER[0]
    # Nothing lost: every incident in view is counted somewhere.
    assert sum(c.total for c in cells) == 40_000
