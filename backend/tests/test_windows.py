"""Crime lookback windows and the night lighting reweighting.

These two changes answer the same complaint: night scores in DC were all high
and all similar, so they told the user nothing about which route to take. One
half of the fix is letting the user narrow the crime data to a recent window;
the other is scoring lighting comparatively, because on an absolute scale a
city that lights nearly every street has nearly every street scoring well.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from getmehome.config import CRIME, LIGHTING
from getmehome.safety.crime_model import (
    CrimeIncident,
    CrimeSurface,
    incident_weight,
    within_window,
)
from getmehome.safety.lighting import rank_scores
from getmehome.safety.scoring import apply_scores, score_route

from .fixtures import (
    build_grid_graph,
    dc_crime_index,
    dense_lights_along_column,
    grid_coords,
)

# Real wall-clock, not a fixed date: the build path measures incident ages
# against "now", so a fixture pinned to a past date would drift out of the
# narrow windows and silently stop testing them.
NOW = datetime.now(UTC)


def incidents_at(row, col, count, age_days, offense="ROBBERY"):
    """A cluster of incidents at one grid node, all the same age."""
    lat, lon = grid_coords(row, col)
    return [
        CrimeIncident(
            lat=lat + (i % 5 - 2) * 0.00008,
            lon=lon + (i % 7 - 3) * 0.00009,
            offense=offense,
            method="OTHERS",
            shift="MIDNIGHT" if i % 2 else "DAY",
            reported_at=NOW - timedelta(days=age_days),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_within_window_keeps_only_recent_incidents():
    recent = incidents_at(2, 2, 3, age_days=10)
    old = incidents_at(2, 2, 4, age_days=200)

    assert len(within_window(recent + old, 30, NOW)) == 3
    assert len(within_window(recent + old, 365, NOW)) == 7
    assert len(within_window(recent + old, None, NOW)) == 7


def test_window_none_means_no_filter_not_zero_days():
    """A missing window must not be read as an empty one."""
    old = incidents_at(2, 2, 5, age_days=900)
    assert len(within_window(old, None, NOW)) == 5


def test_future_dated_incidents_are_kept():
    """MPD's feed occasionally contains one; a clock skew should not drop it."""
    future = incidents_at(2, 2, 1, age_days=-1)
    assert len(within_window(future, 30, NOW)) == 1


def test_no_decay_inside_a_window():
    """Day 1 and day 29 of a 30-day window must weigh the same."""
    fresh = incidents_at(2, 2, 1, age_days=1)[0]
    stale = incidents_at(2, 2, 1, age_days=29)[0]

    assert incident_weight(fresh, NOW, decay=False) == pytest.approx(
        incident_weight(stale, NOW, decay=False)
    )
    # With decay on, they differ — that is the behaviour a window replaces.
    assert incident_weight(fresh, NOW) > incident_weight(stale, NOW)


def test_surface_ignores_incidents_outside_its_window():
    coords = [[grid_coords(2, 2), grid_coords(2, 3)]]
    old = incidents_at(2, 2, 80, age_days=200)

    narrow = CrimeSurface(old, now=NOW, window_days=30)
    wide = CrimeSurface(old, now=NOW, window_days=365)

    assert narrow.n_incidents == 0
    assert wide.n_incidents == 80
    assert float(narrow.score_segments(coords, night=True)[0]) == 0.0
    assert float(wide.score_segments(coords, night=True)[0]) > 0.0


def test_each_window_is_normalised_against_its_own_distribution():
    """"Worst areas in the last 30 days" must mean exactly that.

    A cluster that is the only recent activity should read as a hotspot in the
    30-day view even though a year of citywide data would dwarf it.
    """
    recent = incidents_at(2, 2, 40, age_days=5)
    background = []
    for row in range(1, 6):
        background += incidents_at(row, 5, 120, age_days=200)

    hot = [[grid_coords(2, 2), grid_coords(2, 3)]]

    narrow = CrimeSurface(recent + background, now=NOW, window_days=30)
    wide = CrimeSurface(recent + background, now=NOW, window_days=365)

    assert float(narrow.score_segments(hot, night=True)[0]) > float(
        wide.score_segments(hot, night=True)[0]
    )


# ---------------------------------------------------------------------------
# Windows on the graph
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scored_graph():
    graph = build_grid_graph()
    apply_scores(
        graph,
        lights=dense_lights_along_column(2),
        incidents=incidents_at(3, 4, 90, age_days=5)
        + incidents_at(1, 1, 200, age_days=300),
        cameras=[],
    )
    return graph


def test_graph_carries_every_configured_window(scored_graph):
    assert scored_graph.crime_windows == list(CRIME.windows_days)
    assert scored_graph.seg_crime.shape == (
        len(CRIME.windows_days),
        2,
        scored_graph.n_segments,
    )


def test_window_index_snaps_to_the_nearest_built_window(scored_graph):
    windows = scored_graph.crime_windows
    assert windows[scored_graph.window_index(30)] == 30
    assert windows[scored_graph.window_index(365)] == 365
    # Not built, but the intent is unambiguous.
    assert windows[scored_graph.window_index(90)] == 60
    assert windows[scored_graph.window_index(10_000)] == 365
    assert scored_graph.resolved_window(None) == CRIME.default_window_days


def test_narrow_and_wide_windows_score_different_streets(scored_graph):
    """The recent cluster and the old one are in different places."""
    recent = scored_graph.crime_scores(is_night=True, window_days=30)
    annual = scored_graph.crime_scores(is_night=True, window_days=365)

    assert not np.allclose(recent, annual)
    # The worst street under each window should not be the same one.
    assert int(np.argmax(recent)) != int(np.argmax(annual))


def test_window_changes_the_reported_safety_score(scored_graph):
    from getmehome.routing.astar import GraphIndex, shortest_path

    index = GraphIndex(scored_graph)
    start = index.snap(*grid_coords(3, 3))
    end = index.snap(*grid_coords(3, 6))

    costs, times = scored_graph.edge_costs(True, 0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())
    assert path is not None
    edges = [leg.edge_id for leg in path.legs]

    # This route runs through the recent cluster and nowhere near the old one,
    # so a 30-day view must rate it worse than a 365-day view.
    assert (
        score_route(scored_graph, edges, True, window_days=30).crime
        < score_route(scored_graph, edges, True, window_days=365).crime
    )


def test_the_graph_round_trips_all_windows(tmp_path, scored_graph):
    from getmehome.graph.model import WalkGraph

    path = tmp_path / "graph.npz"
    scored_graph.save(path)
    loaded = WalkGraph.load(path)

    assert loaded.crime_windows == scored_graph.crime_windows
    np.testing.assert_allclose(loaded.seg_crime, scored_graph.seg_crime)


def test_the_hex_grid_honours_the_same_window():
    index = dc_crime_index(
        incidents_at(2, 2, 30, age_days=5) + incidents_at(2, 2, 30, age_days=200),
        now=NOW,
    )
    box = (38.88, -77.05, 38.93, -77.00)

    everything, _ = index.cells(*box, now=NOW)
    recent, _ = index.cells(*box, window_days=30, now=NOW)

    assert sum(c.total for c in everything) == 60
    assert sum(c.total for c in recent) == 30


# ---------------------------------------------------------------------------
# Lighting: ranked against the city
# ---------------------------------------------------------------------------


def test_rank_preserves_order():
    ranked = rank_scores(np.array([0.9, 0.1, 0.5, 0.7], dtype=np.float32))
    assert list(np.argsort(ranked)) == list(np.argsort([0.9, 0.1, 0.5, 0.7]))
    assert ranked.min() == 0.0
    assert ranked.max() == pytest.approx(1.0)


def test_unlit_segments_all_rank_at_zero():
    """The block of segments with no lamp in range must come out fully dark.

    Averaging their tied rank would put a completely unlit street at the
    middle of the scale, which is the opposite of what it deserves.
    """
    scores = np.array([0.0, 0.0, 0.0, 0.4, 0.9], dtype=np.float32)
    ranked = rank_scores(scores)
    assert list(ranked[:3]) == [0.0, 0.0, 0.0]
    assert ranked[3] == pytest.approx(3 / 4)


def test_ranking_spreads_a_compressed_distribution():
    """The actual complaint: everything bunched near the top of the scale.

    DC lights nearly all of its streets, so the absolute scores arrive in a
    narrow high band. Ranking must open that band out, or the differences
    between routes never reach the score.
    """
    compressed = np.linspace(0.86, 0.99, 500).astype(np.float32)
    ranked = rank_scores(compressed)

    assert compressed.std() < 0.05
    assert ranked.std() > 0.25


def test_a_uniform_city_is_not_declared_dark():
    """No comparative information means no comparative claim."""
    flat = np.full(100, 0.8, dtype=np.float32)
    np.testing.assert_allclose(rank_scores(flat), flat)


def test_darkness_exponent_penalises_a_small_shortfall_hard():
    """A street a little worse lit than its neighbours must feel it.

    A linear term would price a segment at the 40th percentile of lighting at
    0.6 of the maximum penalty. The exponent lifts that materially, which is
    what makes the router willing to detour for a better-lit street.
    """
    darkness = np.array([0.2, 0.4], dtype=np.float64)
    curved = darkness ** LIGHTING.darkness_exponent

    assert LIGHTING.darkness_exponent < 1.0
    assert curved[0] > darkness[0] * 1.5
    assert curved[1] > darkness[1] * 1.3


def test_night_risk_actually_separates_lit_from_unlit_streets():
    """End to end: the lit column must score clearly better than the rest.

    Before the reweighting this difference existed but was a couple of points
    out of a hundred, which is not a basis for choosing a route.
    """
    graph = build_grid_graph()
    apply_scores(graph, lights=dense_lights_along_column(2), incidents=[], cameras=[])

    risk = graph.segment_risk(is_night=True)
    lit = risk[graph.seg_lit > 0.75]
    dark = risk[graph.seg_lit < 0.25]

    assert len(lit) and len(dark)
    assert float(dark.mean()) - float(lit.mean()) > 0.15
