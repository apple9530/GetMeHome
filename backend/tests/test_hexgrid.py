"""Tests for hex binning of crime incidents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from getmehome.cities import DC
from getmehome.config import CRIME
from getmehome.geo import haversine_m
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

from .fixtures import dc_crime_index

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
    verts = hex_vertices(CENTER_LAT, CENTER_LON, 250.0, DC.projection)
    assert len(verts) == 6
    for lat, lon in verts:
        assert haversine_m(CENTER_LAT, CENTER_LON, lat, lon) == pytest.approx(250, rel=0.02)


def test_radius_ladder_keeps_cell_count_bounded():
    """Zooming out must pick a bigger cell, never return thousands of them."""
    # The whole District.
    wide = choose_radius(38.79, -77.12, 39.00, -76.91, DC.projection)
    # A few blocks.
    tight = choose_radius(38.900, -77.035, 38.910, -77.025, DC.projection)

    assert wide in SIZE_LADDER
    assert tight in SIZE_LADDER
    assert wide > tight

    x0, y0 = DC.projection.to_local(38.79, -77.12)
    x1, y1 = DC.projection.to_local(39.00, -76.91)
    area = abs(float(x1) - float(x0)) * abs(float(y1) - float(y0))
    assert area / (2.598 * wide * wide) <= MAX_CELLS


def test_grid_is_anchored_not_relative_to_the_viewport():
    """Panning must not shift the cells under the user's finger."""
    index = dc_crime_index(
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
    index = dc_crime_index(incidents, now=NOW)

    cells, radius = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)

    assert cells
    busiest = cells[0]
    assert busiest.total == 17
    breakdown = {b.offense: b for b in busiest.by_offense}
    assert breakdown["ROBBERY"].count == 12
    assert breakdown["HOMICIDE"].count == 5
    assert breakdown["ROBBERY"].category == "violent"
    assert breakdown["HOMICIDE"].category == "violent"
    # Both are violent, so all 17 count as serious.
    assert busiest.serious_count == 17
    # Shares are a proportion of the cell's weighted risk.
    assert sum(b.share for b in busiest.by_offense) == pytest.approx(1.0, abs=0.01)


def test_intensity_is_severity_weighted_not_a_raw_count():
    """Five homicides must outrank twenty petty thefts."""
    homicides = incidents_at(38.9060, -77.0300, 5, offense="HOMICIDE")
    thefts = incidents_at(38.9000, -77.0300, 20, offense="THEFT/OTHER")
    index = dc_crime_index(homicides + thefts, now=NOW)

    cells, _ = index.cells(38.890, -77.040, 38.915, -77.020, radius_m=200.0)
    by_total = {c.total: c for c in cells}

    assert 5 in by_total and 20 in by_total
    assert by_total[5].intensity > by_total[20].intensity, (
        "severity weighting is not being applied"
    )
    # The busiest cell is normalised to 1.
    assert max(c.intensity for c in cells) == pytest.approx(1.0)


def test_night_filter_and_share():
    index = dc_crime_index(
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
    # The fixture is all robbery, which is violent.
    assert everything[0].serious_count == 20
    assert nights[0].total == 10
    assert nights[0].night_share == pytest.approx(1.0)


def test_latest_incident_is_reported():
    index = dc_crime_index(
        incidents_at(CENTER_LAT, CENTER_LON, 10), now=NOW
    )
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)
    # The fixture's newest incident is `NOW` itself.
    assert cells[0].latest.date() == NOW.date()


def test_incidents_outside_the_box_are_excluded():
    index = dc_crime_index(
        incidents_at(38.9800, -76.9200, 30), now=NOW  # far north-east
    )
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=200.0)
    assert cells == []


def test_empty_index_is_safe():
    index = dc_crime_index([], now=NOW)
    assert index.count == 0
    cells, radius = index.cells(38.895, -77.040, 38.915, -77.020)
    assert cells == []
    assert radius > 0


def test_round_trip_through_disk(tmp_path):
    index = dc_crime_index(
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
    index = dc_crime_index(incidents, now=NOW)

    cells, radius = index.cells(38.79, -77.13, 39.01, -76.90)
    assert len(cells) <= MAX_CELLS, f"{len(cells)} cells would stall the map"
    assert radius >= SIZE_LADDER[0]
    # Nothing lost: every incident in view is counted somewhere.
    assert sum(c.total for c in cells) == 40_000


# ---------------------------------------------------------------------------
# Severity weighting
# ---------------------------------------------------------------------------


def test_one_robbery_outweighs_many_car_break_ins():
    """Property crime volume must not drown out a single violent offence.

    This is the whole point of weighting rather than counting: a block with
    forty car break-ins and one robbery is not more dangerous to walk down
    than a block with one robbery and nothing else.
    """
    incidents = (
        incidents_at(CENTER_LAT, CENTER_LON, 40, offense="THEFT F/AUTO", shift="DAY")
        + incidents_at(CENTER_LAT, CENTER_LON, 1, offense="ROBBERY")
    )
    index = dc_crime_index(incidents, now=NOW)
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)

    top = cells[0].by_offense[0]
    assert top.offense == "ROBBERY", (
        "the breakdown is ordered by count, not by weighted risk"
    )
    assert top.share > 0.5
    assert cells[0].serious_count == 1
    assert cells[0].total == 41


def test_sexual_offences_rank_with_the_most_serious():
    """Sexual offences must sit at the top of the severity scale."""

    vocab = DC.crime_vocabulary
    assert vocab.severity["SEX ABUSE"] == vocab.severity["HOMICIDE"]
    assert vocab.category["SEX ABUSE"] == "sexual"
    assert "sexual" in CRIME.serious_categories


def test_violent_and_property_severity_are_far_apart():
    """The gap has to be wide enough to actually change routing decisions."""

    violent = ["HOMICIDE", "SEX ABUSE", "ASSAULT W/DANGEROUS WEAPON", "ROBBERY"]
    property_crime = ["MOTOR VEHICLE THEFT", "THEFT F/AUTO", "THEFT/OTHER", "BURGLARY"]

    def effective(name: str) -> float:
        vocab = DC.crime_vocabulary
        return vocab.severity[name] * vocab.pedestrian_relevance[name]

    worst_property = max(effective(n) for n in property_crime)
    least_violent = min(effective(n) for n in violent)

    assert least_violent > worst_property * 8, (
        f"least violent ({least_violent:.3f}) should dominate worst property "
        f"({worst_property:.3f}) by a wide margin"
    )


def test_property_crime_still_registers():
    """Not zero: heavy property crime is a weak signal of low supervision."""

    for name in ("MOTOR VEHICLE THEFT", "THEFT F/AUTO", "THEFT/OTHER"):
        assert DC.crime_vocabulary.severity[name] > 0


def test_a_violent_cell_outranks_a_high_volume_property_cell():
    """End to end: intensity, not just the breakdown, reflects severity."""
    violent = incidents_at(38.9060, -77.0300, 3, offense="SEX ABUSE")
    property_crime = incidents_at(
        38.9000, -77.0300, 60, offense="MOTOR VEHICLE THEFT", shift="DAY"
    )
    index = dc_crime_index(violent + property_crime, now=NOW)

    cells, _ = index.cells(38.890, -77.040, 38.915, -77.020, radius_m=200.0)
    by_total = {c.total: c for c in cells}

    assert by_total[3].intensity > by_total[60].intensity
    assert by_total[3].serious_count == 3
    assert by_total[60].serious_count == 0


def test_cell_size_floor_prevents_tiny_cells():
    """Zooming right in must not produce hundreds of tiny polygons."""
    from getmehome.safety.hexgrid import MAX_CELLS, SIZE_LADDER

    assert min(SIZE_LADDER) >= 165.0

    # A few blocks across, the tightest a user is likely to zoom.
    radius = choose_radius(38.9000, -77.0350, 38.9060, -77.0270, DC.projection)
    assert radius >= min(SIZE_LADDER)

    # And a mid-zoom view stays within the render budget.
    x0, y0 = DC.projection.to_local(38.890, -77.060)
    x1, y1 = DC.projection.to_local(38.920, -77.010)
    area = abs(float(x1) - float(x0)) * abs(float(y1) - float(y0))
    mid = choose_radius(38.890, -77.060, 38.920, -77.010, DC.projection)
    assert area / (2.598 * mid * mid) <= MAX_CELLS


def test_offense_codes_become_readable_english():
    """MPD codes are database values; they must never reach the screen."""
    from getmehome.safety.hexgrid import readable_offense

    assert readable_offense("THEFT/OTHER") == "Theft"
    assert readable_offense("THEFT F/AUTO") == "Theft from a vehicle"
    assert readable_offense("MOTOR VEHICLE THEFT") == "Vehicle theft"
    assert readable_offense("ASSAULT W/DANGEROUS WEAPON") == "Assault with a weapon"
    assert readable_offense("SEX ABUSE") == "Sexual offense"

    # Nothing shouty or slash-laden survives, including for unmapped codes.
    for code in ("THEFT/OTHER", "THEFT F/AUTO", "SOME NEW/CODE"):
        rendered = readable_offense(code)
        assert rendered != rendered.upper() or len(rendered) < 4
        assert "  " not in rendered


def test_breakdown_carries_a_display_name():
    index = dc_crime_index(
        incidents_at(CENTER_LAT, CENTER_LON, 5, offense="THEFT F/AUTO"), now=NOW
    )
    cells, _ = index.cells(38.895, -77.040, 38.915, -77.020, radius_m=400.0)
    entry = cells[0].by_offense[0]
    assert entry.offense == "THEFT F/AUTO"
    assert entry.display == "Theft from a vehicle"


# ---------------------------------------------------------------------------
# Grouping the breakdown by what it is called
# ---------------------------------------------------------------------------


def test_two_codes_with_one_label_appear_as_one_row():
    """NYPD splits rape from its broader sexual-offence bucket. Both are
    labelled "Sexual offense", and two adjacent rows reading the same thing
    would look like a bug rather than like a legal distinction.
    """
    from getmehome.cities import NYC
    from getmehome.safety.hexgrid import CrimeIndex

    def nypd(offense: str, n: int) -> list[CrimeIncident]:
        return [
            CrimeIncident(
                lat=CENTER_LAT + (i % 3) * 0.00005,
                lon=CENTER_LON + (i % 3) * 0.00005,
                offense=offense,
                method="",
                shift="EVENING",
                reported_at=NOW - timedelta(days=i),
                premises="STREET",
            )
            for i in range(n)
        ]

    index = CrimeIndex.from_incidents(
        nypd("RAPE", 2) + nypd("SEX CRIMES", 3) + nypd("ROBBERY", 4),
        now=NOW,
        projection=DC.projection,
        vocabulary=NYC.crime_vocabulary,
        city_slug="nyc",
    )
    cells, _ = index.cells(
        CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01
    )
    assert cells

    labels = [entry.display for entry in cells[0].by_offense]
    assert labels.count("Sexual offense") == 1, labels

    sexual = next(e for e in cells[0].by_offense if e.display == "Sexual offense")
    # Both codes' counts land in the one row.
    assert sexual.count == 5
    assert sexual.category == "sexual"


def test_grouped_shares_still_sum_to_one():
    from getmehome.cities import NYC
    from getmehome.safety.hexgrid import CrimeIndex

    incidents = [
        CrimeIncident(
            lat=CENTER_LAT, lon=CENTER_LON, offense=offense, method="",
            shift="EVENING", reported_at=NOW, premises="STREET",
        )
        for offense in ("RAPE", "SEX CRIMES", "ROBBERY", "PETIT LARCENY")
    ]
    index = CrimeIndex.from_incidents(
        incidents,
        now=NOW,
        projection=DC.projection,
        vocabulary=NYC.crime_vocabulary,
        city_slug="nyc",
    )
    cells, _ = index.cells(
        CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01
    )
    total = sum(e.share for e in cells[0].by_offense)
    assert total == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# What the grid leaves out, and what it says about itself
# ---------------------------------------------------------------------------


def _nyc_index(incidents, premises_policy):
    from getmehome.cities import NYC

    return CrimeIndex.from_incidents(
        incidents,
        now=NOW,
        projection=DC.projection,
        vocabulary=NYC.crime_vocabulary,
        city_slug="nyc",
        premises=premises_policy,
    )


def test_indoor_incidents_are_left_out_of_the_grid_entirely():
    """Not counted at weight zero — dropped.

    Counting them produced a cell that said "12 incidents" and drew at zero
    intensity: an invisible hexagon with a number attached. Over a residential
    viewport the whole overlay could vanish that way while the response still
    claimed hundreds of incidents, which is exactly the shape of "the crime
    graph isn't showing".
    """
    from getmehome.cities import STREET_ONLY

    indoors = [
        CrimeIncident(
            lat=CENTER_LAT, lon=CENTER_LON, offense="FELONY ASSAULT", method="",
            shift="MIDNIGHT", reported_at=NOW, premises="RESIDENCE - APT. HOUSE",
        )
        for _ in range(12)
    ]
    cells, _ = _nyc_index(indoors, STREET_ONLY).cells(
        CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01
    )
    assert cells == []

    outdoors = indoors + [
        CrimeIncident(
            lat=CENTER_LAT, lon=CENTER_LON, offense="ROBBERY", method="",
            shift="MIDNIGHT", reported_at=NOW, premises="STREET",
        )
    ]
    cells, _ = _nyc_index(outdoors, STREET_ONLY).cells(
        CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01
    )
    # The count now describes street crime, which is what the surface models.
    assert [c.total for c in cells] == [1]


def test_a_city_with_no_premises_column_keeps_everything():
    """The filter must not quietly apply to Washington, which publishes none."""
    from getmehome.cities import NO_PREMISES

    incidents = incidents_at(CENTER_LAT, CENTER_LON, 8)
    cells, _ = _nyc_index(incidents, NO_PREMISES).cells(
        CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01
    )
    assert sum(c.total for c in cells) == 8


def test_the_index_reports_its_newest_incident():
    """How an empty grid explains itself.

    New York publishes in quarterly batches, so a 30-day lookback there can
    legitimately match nothing while Washington's daily feed matches plenty.
    Without a date to point at, that is indistinguishable from a bug.
    """
    index = dc_crime_index(
        incidents_at(CENTER_LAT, CENTER_LON, 5)
        + incidents_at(CENTER_LAT, CENTER_LON, 1, offense="HOMICIDE")
    )
    assert index.latest is not None
    assert index.latest <= NOW

    empty = dc_crime_index([])
    assert empty.latest is None


def test_a_stale_feed_returns_no_cells_for_a_short_window():
    """The mechanism behind the empty New York grid, stated directly."""
    old = [
        CrimeIncident(
            lat=CENTER_LAT, lon=CENTER_LON, offense="ROBBERY", method="",
            shift="MIDNIGHT", reported_at=NOW - timedelta(days=120),
        )
        for _ in range(20)
    ]
    index = dc_crime_index(old)
    box = (CENTER_LAT - 0.01, CENTER_LON - 0.01, CENTER_LAT + 0.01, CENTER_LON + 0.01)

    recent, _ = index.cells(*box, window_days=30, now=NOW)
    assert recent == []

    # And the same data over a window that reaches them is fine, so the empty
    # result above is about recency and nothing else.
    year, _ = index.cells(*box, window_days=365, now=NOW)
    assert sum(c.total for c in year) == 20
