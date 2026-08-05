"""Tests for OSM parsing: walkability rules, junction splitting, ALPR nodes."""

from __future__ import annotations

import pytest

from getmehome.config import DC_BBOX
from getmehome.ingest.osm import is_alpr, is_walkable, read_osm
from getmehome.safety.cameras import parse_direction

# A hand-built extract. Two crossing streets sharing node 3, a motorway that
# must be excluded, a private drive that must be excluded, and two ALPR nodes
# (one with a bearing, one without).
OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
 <node id="1" lat="38.9000" lon="-77.0300"/>
 <node id="2" lat="38.9000" lon="-77.0290"/>
 <node id="3" lat="38.9000" lon="-77.0280"/>
 <node id="4" lat="38.9000" lon="-77.0270"/>
 <node id="5" lat="38.9010" lon="-77.0280"/>
 <node id="6" lat="38.8990" lon="-77.0280"/>
 <node id="7" lat="38.9020" lon="-77.0300"/>
 <node id="8" lat="38.9020" lon="-77.0260"/>
 <node id="9" lat="38.8980" lon="-77.0300"/>
 <node id="10" lat="38.8980" lon="-77.0260"/>
 <node id="20" lat="38.9001" lon="-77.0280">
  <tag k="man_made" v="surveillance"/>
  <tag k="surveillance:type" v="ALPR"/>
  <tag k="operator" v="Flock Safety"/>
  <tag k="direction" v="90"/>
 </node>
 <node id="21" lat="38.9009" lon="-77.0281">
  <tag k="man_made" v="surveillance"/>
  <tag k="surveillance:type" v="ALPR"/>
 </node>
 <node id="22" lat="38.9005" lon="-77.0285">
  <tag k="man_made" v="surveillance"/>
  <tag k="surveillance:type" v="camera"/>
 </node>
 <way id="100">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>
  <tag k="highway" v="residential"/>
  <tag k="name" v="K St NW"/>
  <tag k="lit" v="yes"/>
 </way>
 <way id="101">
  <nd ref="5"/><nd ref="3"/><nd ref="6"/>
  <tag k="highway" v="footway"/>
  <tag k="name" v="Park Path"/>
  <tag k="surface" v="ground"/>
 </way>
 <way id="102">
  <nd ref="7"/><nd ref="8"/>
  <tag k="highway" v="motorway"/>
  <tag k="name" v="I-395"/>
 </way>
 <way id="103">
  <nd ref="9"/><nd ref="10"/>
  <tag k="highway" v="service"/>
  <tag k="access" v="private"/>
 </way>
</osm>
"""


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    path = tmp_path_factory.mktemp("osm") / "sample.osm"
    path.write_text(OSM_XML)
    return read_osm(path, bbox=DC_BBOX)


def test_walkability_rules():
    assert is_walkable({"highway": "residential"})
    assert is_walkable({"highway": "footway"})
    assert not is_walkable({"highway": "motorway"})
    # ...unless a sidewalk is mapped alongside it.
    assert is_walkable({"highway": "motorway", "sidewalk": "both"})
    assert not is_walkable({"highway": "residential", "foot": "no"})
    assert not is_walkable({"highway": "service", "access": "private"})
    # An explicit foot permission overrides a general access restriction.
    assert is_walkable(
        {"highway": "service", "access": "private", "foot": "designated"}
    )
    assert not is_walkable({"highway": "pedestrian", "area": "yes"})
    assert not is_walkable({"building": "yes"})


def test_alpr_detection():
    assert is_alpr(
        {"man_made": "surveillance", "surveillance:type": "ALPR"}
    )
    assert is_alpr({"man_made": "surveillance", "operator": "Flock Safety"})
    # A plain CCTV camera is not a plate reader.
    assert not is_alpr(
        {"man_made": "surveillance", "surveillance:type": "camera"}
    )
    assert not is_alpr({"highway": "residential"})


def test_direction_parsing():
    assert parse_direction("90") == 90.0
    assert parse_direction("NE") == 45.0
    assert parse_direction("360") == 0.0
    assert parse_direction("") is None
    assert parse_direction(None) is None
    assert parse_direction("sideways") is None


def test_excluded_ways_are_absent(parsed):
    _, segments, _ = parsed
    names = {s.name for s in segments}
    assert "I-395" not in names, "motorway must be excluded"
    highways = {s.highway for s in segments}
    assert "service" not in highways, "private drive must be excluded"


def test_ways_split_at_junctions(parsed):
    """K St crosses the path at node 3, so it must become two segments."""
    _, segments, _ = parsed
    k_st = [s for s in segments if s.name == "K St NW"]
    assert len(k_st) == 2

    # One runs 1->3, the other 3->4.
    endpoints = {(s.node_a, s.node_b) for s in k_st}
    assert (1, 3) in endpoints
    assert (3, 4) in endpoints

    # The first keeps its intermediate vertex.
    first = next(s for s in k_st if s.node_a == 1)
    assert len(first.coords) == 3

    path = [s for s in segments if s.name == "Park Path"]
    assert len(path) == 2


def test_segment_tags_and_lengths(parsed):
    _, segments, _ = parsed
    for s in segments:
        assert s.length_m > 0
        assert s.node_a != s.node_b
        assert len(s.coords) >= 2

    k_st = next(s for s in segments if s.name == "K St NW")
    assert k_st.tags.get("lit") == "yes"


def test_cameras_extracted(parsed):
    _, _, cameras = parsed
    assert len(cameras) == 2, "the plain CCTV node must not be included"

    directed = [c for c in cameras if c.direction_deg is not None]
    undirected = [c for c in cameras if c.direction_deg is None]
    assert len(directed) == 1
    assert directed[0].direction_deg == 90.0
    assert directed[0].operator == "Flock Safety"
    assert len(undirected) == 1


def test_graph_builds_from_parsed_osm(parsed):
    """The parsed extract must compile into a routable graph."""
    from getmehome.graph.model import build_graph
    from getmehome.routing.astar import GraphIndex

    node_coords, segments, _ = parsed
    graph = build_graph(node_coords, segments)

    assert graph.n_segments == 4  # K St x2 + Park Path x2
    assert graph.n_edges == 8

    index = GraphIndex(graph)
    start = index.snap(38.9000, -77.0300)
    end = index.snap(38.9010, -77.0280)
    assert start is not None and end is not None

    from getmehome.routing.astar import shortest_path

    costs, times = graph.edge_costs(is_night=False, risk_lambda=0.0)
    path = shortest_path(index, start, end, costs.tolist(), times.tolist())
    assert path is not None
    assert path.distance_m > 0


def test_isolation_reflects_tags(parsed):
    """An unpaved park path must score as more isolated than a lit street."""
    from getmehome.graph.model import build_graph

    node_coords, segments, _ = parsed
    graph = build_graph(node_coords, segments)

    by_name = {}
    for i in range(graph.n_segments):
        name = graph.names[int(graph.seg_name[i])]
        by_name.setdefault(name, []).append(float(graph.seg_isolation[i]))

    assert max(by_name["Park Path"]) > max(by_name["K St NW"])


def test_bbox_filtering(tmp_path):
    """Geometry outside the study area is dropped."""
    from getmehome.config import BBox

    path = tmp_path / "sample.osm"
    path.write_text(OSM_XML)
    tiny = BBox(min_lat=0.0, min_lon=0.0, max_lat=1.0, max_lon=1.0)
    _, segments, cameras = read_osm(path, bbox=tiny)
    assert segments == []
    assert cameras == []
