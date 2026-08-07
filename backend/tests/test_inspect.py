"""The build inspector, which exists to read `median lighting score: 0.0`.

That number is ambiguous — lighting is ranked and every segment with no lamp
in range ties at zero, so a median of zero only says "at least half the graph
has no lamp nearby". The inspector's job is to say which of the two very
different reasons applies, so its measurements have to be trustworthy.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from getmehome.cities import DC
from getmehome.config import LIGHTING
from getmehome.graph.inspect import nearest_lamp_per_segment
from getmehome.safety.scoring import apply_scores

from .fixtures import build_grid_graph, dense_lights_along_column


def _scored_graph(lights):
    graph = build_grid_graph(7, 7)
    apply_scores(graph, lights=lights, incidents=[], cameras=[])
    return graph


def _tree(lights):
    x, y = DC.projection.to_local(
        np.array([lamp.lat for lamp in lights]),
        np.array([lamp.lon for lamp in lights]),
    )
    return cKDTree(np.column_stack([np.asarray(x), np.asarray(y)]))


def test_lamp_distance_agrees_with_the_scored_graph():
    """The invariant the whole diagnosis rests on.

    A segment scores above zero exactly when a lamp falls inside the search
    radius, so counting from the lamp positions and counting from the scored
    graph must give the same answer. When they agree, the story is entirely
    about where the lamps are; when they disagree, the scoring is at fault.
    A disagreement the *inspector itself* manufactures would send anyone
    reading it to the wrong half of the code.
    """
    lights = dense_lights_along_column(1)
    graph = _scored_graph(lights)

    nearest = nearest_lamp_per_segment(graph, _tree(lights))
    in_range = nearest <= LIGHTING.search_radius_m
    lit = graph.seg_lit > 0.0

    assert in_range.tolist() == lit.tolist()


def test_distance_is_measured_over_the_whole_segment():
    """Not from one endpoint.

    The scorer samples along the polyline, so a long street whose far end
    passes a lamp is lit while its first vertex is far away. Measuring from an
    endpoint would report that street as unlit-but-scored-lit and invent a
    discrepancy in a perfectly good build.
    """
    lights = dense_lights_along_column(1)
    graph = _scored_graph(lights)
    tree = _tree(lights)

    whole = nearest_lamp_per_segment(graph, tree)

    heads = graph.seg_geom[graph.seg_geom_ptr[:-1].astype(np.int64)]
    hx, hy = graph.projection.to_local(
        heads[:, 0].astype(np.float64), heads[:, 1].astype(np.float64)
    )
    from_head, _ = tree.query(np.column_stack([np.asarray(hx), np.asarray(hy)]))

    # Never worse than the endpoint measurement, and strictly better somewhere
    # — otherwise the two are the same thing and this guards nothing.
    assert np.all(whole <= from_head + 1e-6)
    assert np.any(whole < from_head - 1e-6)


def test_a_city_with_no_lamps_reports_nothing_in_range():
    graph = _scored_graph([])
    assert float((graph.seg_lit > 0).mean()) == 0.0
    # And the rank collapses rather than declaring the whole city maximally
    # dark, which is what `rank_scores` refuses to do on a total tie.
    assert float(np.median(graph.seg_lit)) == 0.0
