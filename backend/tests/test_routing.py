"""Tests for snapping, A* and alternatives generation."""

from __future__ import annotations

import numpy as np
import pytest

from getmehome.config import ROUTING
from getmehome.geo import haversine_m, polyline_length_m
from getmehome.routing.alternatives import compute_options, overlap_fraction
from getmehome.routing.astar import GraphIndex, shortest_path

from .fixtures import (
    build_grid_graph,
    camera_facing,
    crime_cluster_at,
    dense_lights_along_column,
    grid_coords,
)


@pytest.fixture(scope="module")
def grid():
    return build_grid_graph(7, 7)


@pytest.fixture(scope="module")
def index(grid):
    return GraphIndex(grid)


def test_graph_shape(grid):
    # 7x7 grid: 42 east-west + 42 north-south segments.
    assert grid.n_nodes == 49
    assert grid.n_segments == 84
    assert grid.n_edges == 168  # one directed pair per segment


def test_segment_edge_pairing(grid, index):
    """build_graph must emit forward/reverse edges contiguously."""
    for seg in range(grid.n_segments):
        fwd, rev = index.segment_edges(seg)
        assert grid.edge_seg[fwd] == seg
        assert grid.edge_seg[rev] == seg
        assert grid.edge_from[fwd] == grid.edge_to[rev]
        assert grid.edge_to[fwd] == grid.edge_from[rev]
        assert not grid.edge_rev[fwd]
        assert grid.edge_rev[rev]


def test_snap_to_nearest_segment(index):
    """A point mid-block snaps onto that block, not to an intersection."""
    lat0, lon0 = grid_coords(0, 0)
    lat1, lon1 = grid_coords(0, 1)
    mid_lat, mid_lon = (lat0 + lat1) / 2, (lon0 + lon1) / 2

    snap = index.snap(mid_lat, mid_lon)
    assert snap is not None
    assert snap.distance_m < 5.0
    # Roughly halfway along the segment it landed on.
    assert 0.3 < snap.fraction < 0.7


def test_snap_offset_point(index):
    """A point set back from the street still snaps, with the offset reported.

    Offsetting from a grid *node* would land on the crossing street, so this
    starts from mid-block and moves into the block's interior — the situation
    of someone standing inside a building rather than on the pavement.
    """
    lat_a, lon_a = grid_coords(2, 2)
    lat_b, lon_b = grid_coords(2, 3)
    mid_lat, mid_lon = (lat_a + lat_b) / 2, (lon_a + lon_b) / 2

    snap = index.snap(mid_lat + 0.00027, mid_lon)  # ~30m north, into the block
    assert snap is not None
    assert 20.0 < snap.distance_m < 40.0
    assert 0.3 < snap.fraction < 0.7


def test_snap_rejects_far_away(index):
    # Baltimore is well outside the fixture grid.
    assert index.snap(39.29, -76.61) is None


def test_shortest_path_is_manhattan_length(grid, index):
    """With zero risk aversion the router returns a minimal grid path."""
    start = index.snap(*grid_coords(0, 0))
    end = index.snap(*grid_coords(3, 4))
    costs, times = grid.edge_costs(is_night=False, risk_lambda=0.0)

    path = shortest_path(index, start, end, costs.tolist(), times.tolist())
    assert path is not None

    # 3 blocks north + 4 blocks east on a ~100m grid.
    expected = haversine_m(*grid_coords(0, 0), *grid_coords(3, 0)) + haversine_m(
        *grid_coords(3, 0), *grid_coords(3, 4)
    )
    assert path.distance_m == pytest.approx(expected, rel=0.02)


def test_path_geometry_is_continuous(grid, index):
    """Stitched route geometry must not contain jumps between legs."""
    start = index.snap(*grid_coords(0, 0))
    end = index.snap(*grid_coords(5, 5))
    costs, times = grid.edge_costs(is_night=False, risk_lambda=0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())

    assert path is not None
    assert len(path.coords) >= 2
    for a, b in zip(path.coords, path.coords[1:], strict=False):
        assert haversine_m(*a, *b) < 150.0

    # The polyline length should match the reported distance.
    assert polyline_length_m(path.coords) == pytest.approx(path.distance_m, rel=0.02)


def test_path_starts_and_ends_at_snapped_points(grid, index):
    lat_s, lon_s = grid_coords(1, 1)
    lat_e, lon_e = grid_coords(4, 3)
    start = index.snap(lat_s, lon_s)
    end = index.snap(lat_e, lon_e)
    costs, times = grid.edge_costs(is_night=False, risk_lambda=0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())

    assert haversine_m(*path.coords[0], start.lat, start.lon) < 10.0
    assert haversine_m(*path.coords[-1], end.lat, end.lon) < 10.0


def test_same_segment_routing(grid, index):
    """Origin and destination on the same block routes directly along it."""
    lat0, lon0 = grid_coords(0, 0)
    lat1, lon1 = grid_coords(0, 1)
    start = index.snap(lat0 + (lat1 - lat0) * 0.25, lon0 + (lon1 - lon0) * 0.25)
    end = index.snap(lat0 + (lat1 - lat0) * 0.75, lon0 + (lon1 - lon0) * 0.75)
    assert start.seg_id == end.seg_id

    costs, times = grid.edge_costs(is_night=False, risk_lambda=0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())

    assert path is not None
    assert len(path.legs) == 1
    # Half a ~100m block.
    assert 35.0 < path.distance_m < 65.0


def test_same_segment_backwards(grid, index):
    """Routing backwards along one block works and does not loop the grid."""
    lat0, lon0 = grid_coords(0, 0)
    lat1, lon1 = grid_coords(0, 1)
    start = index.snap(lat0 + (lat1 - lat0) * 0.8, lon0 + (lon1 - lon0) * 0.8)
    end = index.snap(lat0 + (lat1 - lat0) * 0.2, lon0 + (lon1 - lon0) * 0.2)

    costs, times = grid.edge_costs(is_night=False, risk_lambda=0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())

    assert path is not None
    assert path.distance_m < 100.0


def test_risk_aversion_changes_the_route(grid):
    """The whole premise: a dangerous street must divert a safety-seeking route.

    A heavy crime cluster is placed on the 4th column. The fastest route from
    the south-west to the north-east corner is free to use it; the safest one
    should not.
    """
    from getmehome.safety.scoring import apply_scores

    incidents = []
    for r in range(1, 6):
        incidents.extend(crime_cluster_at(r, 3, count=60, offense="ROBBERY"))

    apply_scores(grid, lights=[], incidents=incidents, cameras=[])
    index = GraphIndex(grid)

    start = index.snap(*grid_coords(0, 0))
    end = index.snap(*grid_coords(6, 6))

    fast_costs, times = grid.edge_costs(True, ROUTING.lambda_fastest)
    safe_costs, _ = grid.edge_costs(True, ROUTING.lambda_safest)

    fast = shortest_path(index, start, end, fast_costs.tolist(), times.tolist())
    safe = shortest_path(index, start, end, safe_costs.tolist(), times.tolist())

    assert fast is not None and safe is not None

    from getmehome.safety.scoring import score_route

    fast_score = score_route(grid, [leg.edge_id for leg in fast.legs], True)
    safe_score = score_route(grid, [leg.edge_id for leg in safe.legs], True)

    assert safe_score.overall > fast_score.overall, (
        f"safest route ({safe_score.overall}) should beat fastest "
        f"({fast_score.overall})"
    )
    # And it should cost something in time — a free lunch would mean the
    # fastest search was simply wrong.
    assert safe.duration_s >= fast.duration_s


def test_lighting_influences_night_routing(grid):
    """A lit street should attract a night route away from a dark parallel one."""
    from getmehome.safety.scoring import apply_scores, score_route

    lights = dense_lights_along_column(2)
    apply_scores(grid, lights=lights, incidents=[], cameras=[])
    index = GraphIndex(grid)

    lit_col_segments = np.where(grid.seg_lit > 0.5)[0]
    assert len(lit_col_segments) > 0, "lights should light up some segments"

    start = index.snap(*grid_coords(0, 2))
    end = index.snap(*grid_coords(6, 2))

    night_costs, times = grid.edge_costs(True, ROUTING.lambda_safest)
    path = shortest_path(index, start, end, night_costs.tolist(), times.tolist())
    score = score_route(grid, [leg.edge_id for leg in path.legs], True)

    # Straight up the lit column: high lighting score and no detour.
    assert score.lighting > 50
    assert score.pct_well_lit > 50


def test_daylight_ignores_lighting(grid):
    """Lighting must not affect daytime risk — the sun is up."""
    from getmehome.safety.scoring import apply_scores

    apply_scores(grid, lights=dense_lights_along_column(2), incidents=[], cameras=[])

    day_risk = grid.segment_risk(is_night=False)
    lit = grid.seg_lit > 0.5
    dark = grid.seg_lit < 0.05
    if lit.any() and dark.any():
        # With no crime data, daytime risk comes only from isolation, which is
        # identical across this uniform grid.
        assert day_risk[lit].mean() == pytest.approx(day_risk[dark].mean(), abs=1e-6)


def test_camera_avoidance_diverts_route(grid):
    """Asking to avoid ALPR cameras must actually change the route."""
    from getmehome.safety.scoring import apply_scores

    # Cameras looking east, along the middle column.
    cams = [camera_facing(r, 3, direction_deg=90.0) for r in range(1, 6)]
    cams += [camera_facing(r, 3, direction_deg=270.0) for r in range(1, 6)]

    apply_scores(grid, lights=[], incidents=[], cameras=cams)
    assert grid.seg_camera.max() > 0.2, "cameras should expose some segments"

    index = GraphIndex(grid)
    start = index.snap(*grid_coords(0, 0))
    end = index.snap(*grid_coords(6, 6))

    plain, times = grid.edge_costs(True, 0.0, avoid_cameras=False)
    avoid, _ = grid.edge_costs(True, 0.0, avoid_cameras=True)

    p1 = shortest_path(index, start, end, plain.tolist(), times.tolist())
    p2 = shortest_path(index, start, end, avoid.tolist(), times.tolist())

    exposure_1 = max(
        float(grid.seg_camera[grid.edge_seg[leg.edge_id]]) for leg in p1.legs
    )
    exposure_2 = max(
        float(grid.seg_camera[grid.edge_seg[leg.edge_id]]) for leg in p2.legs
    )
    assert exposure_2 <= exposure_1


def test_compute_options_returns_distinct_routes(grid):
    from getmehome.safety.scoring import apply_scores

    incidents = []
    for r in range(1, 6):
        incidents.extend(crime_cluster_at(r, 3, count=80, offense="HOMICIDE"))
    apply_scores(grid, lights=[], incidents=incidents, cameras=[])
    index = GraphIndex(grid)

    options = compute_options(
        index,
        index.snap(*grid_coords(0, 0)),
        index.snap(*grid_coords(6, 6)),
        is_night=True,
    )

    assert 1 <= len(options) <= 3
    # Sorted quickest-first.
    durations = [o.path.duration_s for o in options]
    assert durations == sorted(durations)
    # Every option carries a usable safety score.
    for o in options:
        assert 0 <= o.safety.overall <= 100
        assert o.label
        assert o.summary

    if len(options) > 1:
        # Kept options must be genuinely different routes.
        assert overlap_fraction(grid, options[0].path, options[-1].path) <= (
            ROUTING.max_route_overlap
        )


def test_detour_ratio_is_capped(grid):
    """The safest option must not become an absurd diversion."""
    from getmehome.safety.scoring import apply_scores

    incidents = []
    for r in range(7):
        for c in range(7):
            if (r + c) % 2 == 0:
                incidents.extend(crime_cluster_at(r, c, count=30))
    apply_scores(grid, lights=[], incidents=incidents, cameras=[])
    index = GraphIndex(grid)

    options = compute_options(
        index,
        index.snap(*grid_coords(0, 0)),
        index.snap(*grid_coords(6, 6)),
        is_night=True,
    )
    fastest = min(o.path.duration_s for o in options)
    for o in options:
        assert o.path.duration_s <= fastest * ROUTING.max_detour_ratio * 1.05


def test_unreachable_returns_none(grid, index):
    """A destination off the graph yields no snap rather than a bad route."""
    assert index.snap(0.0, 0.0) is None
