"""Risk-weighted A* over the walk graph.

The cost being minimised is walking time inflated by risk:

    cost(edge) = length / speed * (1 + lambda * risk(edge))

``lambda`` is the risk aversion dial. At 0 this reduces to a plain
shortest-time search, so the "fastest" route falls out of the same code path
as the "safest" one and the two are guaranteed comparable.

The heuristic is straight-line distance over walking speed. Because
``risk >= 0`` implies ``cost >= time >= straight_line / speed``, the heuristic
never overestimates and A* stays optimal for every value of lambda.

Origins and destinations rarely sit on a graph node, so both are snapped onto
the nearest segment and injected as virtual nodes. Without this a route from
mid-block would visibly jump to the end of the street before starting, which
users read as the app not knowing where they are.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from ..config import ROUTING
from ..geo import (
    interpolate_polyline,
    project_point_to_segment,
    sample_polyline,
    split_polyline,
    to_local,
    to_wgs84,
)
from ..graph.model import WalkGraph


@dataclass
class SnapPoint:
    """A lat/lon location bound onto the graph."""

    seg_id: int
    fraction: float  # position along the segment's forward geometry, 0-1
    lat: float
    lon: float
    distance_m: float  # how far the original point was from the graph
    node_a: int  # segment's forward-start node
    node_b: int  # segment's forward-end node


@dataclass
class PathLeg:
    """Traversal of one directed edge, possibly only part of it."""

    edge_id: int
    from_frac: float = 0.0
    to_frac: float = 1.0

    @property
    def fraction(self) -> float:
        return max(0.0, self.to_frac - self.from_frac)


@dataclass
class PathResult:
    legs: list[PathLeg]
    distance_m: float
    duration_s: float
    cost: float
    coords: list[tuple[float, float]] = field(default_factory=list)


class GraphIndex:
    """Spatial index + hot-loop-friendly views of a graph.

    A* in Python is dominated by per-element access cost, and indexing a numpy
    array with a Python int is several times slower than indexing a list. The
    structural arrays are converted to lists once here and reused across every
    query, which is worth roughly a 3x speedup on the search.
    """

    def __init__(self, graph: WalkGraph, snap_spacing_m: float = 20.0) -> None:
        self.graph = graph
        self.edge_to = graph.edge_to.tolist()
        self.edge_seg = graph.edge_seg.tolist()
        self.adj_ptr = graph.adj_ptr.tolist()
        self.adj_edges = graph.adj_edges.tolist()
        self.node_x = graph.node_x.tolist()
        self.node_y = graph.node_y.tolist()
        self.seg_length = graph.seg_length.tolist()

        # Densified points along every segment, for snapping.
        pts: list[np.ndarray] = []
        owner: list[int] = []
        for s in range(graph.n_segments):
            sampled = sample_polyline(graph.segment_coords(s), snap_spacing_m)
            if len(sampled):
                pts.append(sampled)
                owner.extend([s] * len(sampled))
        if pts:
            self._snap_points = np.vstack(pts)
            self._snap_owner = np.array(owner, dtype=np.int32)
            self._snap_tree = cKDTree(self._snap_points)
        else:
            self._snap_points = np.zeros((0, 2))
            self._snap_owner = np.zeros(0, dtype=np.int32)
            self._snap_tree = None

        # Segment -> (forward edge, reverse edge). build_graph emits the pair
        # contiguously, which this relies on.
        self._seg_edges = {int(s): (2 * int(s), 2 * int(s) + 1)
                           for s in range(graph.n_segments)}

    def segment_edges(self, seg_id: int) -> tuple[int, int]:
        return self._seg_edges[int(seg_id)]

    def snap(
        self, lat: float, lon: float, max_distance_m: float | None = None
    ) -> SnapPoint | None:
        """Bind a coordinate to the nearest point on the nearest segment."""
        if self._snap_tree is None:
            return None
        limit = max_distance_m or ROUTING.max_snap_distance_m

        px, py = to_local(lat, lon)
        px, py = float(px), float(py)

        # Take several candidate sample points: the nearest sample does not
        # always belong to the segment that is genuinely closest, especially
        # near intersections where segments converge.
        k = min(12, len(self._snap_points))
        dists, idxs = self._snap_tree.query([px, py], k=k)
        if k == 1:
            dists, idxs = [dists], [idxs]

        best: SnapPoint | None = None
        best_d = float("inf")
        for cand in {int(self._snap_owner[i]) for i in np.atleast_1d(idxs)}:
            snapped = self._project_onto_segment(cand, px, py)
            if snapped and snapped.distance_m < best_d:
                best, best_d = snapped, snapped.distance_m

        if best is None or best.distance_m > limit:
            return None
        return best

    def _project_onto_segment(
        self, seg_id: int, px: float, py: float
    ) -> SnapPoint | None:
        coords = self.graph.segment_coords(seg_id)
        if len(coords) < 2:
            return None

        lat = np.array([c[0] for c in coords])
        lon = np.array([c[1] for c in coords])
        xs, ys = to_local(lat, lon)

        seg_d = np.hypot(np.diff(xs), np.diff(ys))
        cum = np.concatenate([[0.0], np.cumsum(seg_d)])
        total = float(cum[-1])
        if total <= 0:
            return None

        best_d2 = float("inf")
        best_xy = (0.0, 0.0)
        best_along = 0.0
        for i in range(len(coords) - 1):
            qx, qy, t = project_point_to_segment(
                px, py, xs[i], ys[i], xs[i + 1], ys[i + 1]
            )
            d2 = (qx - px) ** 2 + (qy - py) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_xy = (qx, qy)
                best_along = float(cum[i] + t * seg_d[i])

        slat, slon = to_wgs84(*best_xy)
        fwd, _ = self.segment_edges(seg_id)
        return SnapPoint(
            seg_id=seg_id,
            fraction=min(1.0, max(0.0, best_along / total)),
            lat=float(slat),
            lon=float(slon),
            distance_m=math.sqrt(best_d2),
            node_a=int(self.graph.edge_from[fwd]),
            node_b=int(self.graph.edge_to[fwd]),
        )


def _virtual_transitions(
    index: GraphIndex,
    start: SnapPoint,
    end: SnapPoint,
    costs: list[float],
    times: list[float],
) -> tuple[list[tuple[int, float, float, PathLeg]], dict[int, tuple[float, float, PathLeg]], list[tuple[float, float, PathLeg]]]:
    """Build the edges connecting the virtual endpoints into the graph.

    Returns ``(from_start, to_end, direct)`` where ``direct`` covers the case
    of origin and destination sitting on the same segment.
    """
    s_fwd, s_rev = index.segment_edges(start.seg_id)
    e_fwd, e_rev = index.segment_edges(end.seg_id)
    t, u = start.fraction, end.fraction

    from_start: list[tuple[int, float, float, PathLeg]] = []
    # Toward node_b: forward edge, from t to the end.
    if t < 1.0:
        f = 1.0 - t
        from_start.append(
            (start.node_b, costs[s_fwd] * f, times[s_fwd] * f, PathLeg(s_fwd, t, 1.0))
        )
    # Toward node_a: reverse edge. Position t on the forward geometry is
    # position 1-t on the reversed geometry.
    if t > 0.0:
        from_start.append(
            (start.node_a, costs[s_rev] * t, times[s_rev] * t, PathLeg(s_rev, 1.0 - t, 1.0))
        )

    to_end: dict[int, tuple[float, float, PathLeg]] = {}
    if u > 0.0:
        to_end[end.node_a] = (costs[e_fwd] * u, times[e_fwd] * u, PathLeg(e_fwd, 0.0, u))
    if u < 1.0:
        f = 1.0 - u
        to_end[end.node_b] = (costs[e_rev] * f, times[e_rev] * f, PathLeg(e_rev, 0.0, f))

    direct: list[tuple[float, float, PathLeg]] = []
    if start.seg_id == end.seg_id:
        if u >= t:
            f = u - t
            direct.append((costs[s_fwd] * f, times[s_fwd] * f, PathLeg(s_fwd, t, u)))
        else:
            f = t - u
            direct.append(
                (costs[s_rev] * f, times[s_rev] * f, PathLeg(s_rev, 1.0 - t, 1.0 - u))
            )

    return from_start, to_end, direct


def shortest_path(
    index: GraphIndex,
    start: SnapPoint,
    end: SnapPoint,
    costs: list[float],
    times: list[float],
    edge_penalties: dict[int, float] | None = None,
) -> PathResult | None:
    """A* from ``start`` to ``end``.

    ``edge_penalties`` multiplies the cost of specific edges, which is how the
    alternatives search pushes subsequent routes away from ones already found
    without changing the underlying graph.
    """
    graph = index.graph
    n = graph.n_nodes
    VS, VE = n, n + 1

    from_start, to_end, direct = _virtual_transitions(index, start, end, costs, times)

    # Local bindings — this loop runs millions of times on a cross-town query.
    adj_ptr = index.adj_ptr
    adj_edges = index.adj_edges
    edge_to = index.edge_to
    node_x = index.node_x
    node_y = index.node_y
    speed = ROUTING.walk_speed_mps
    penalties = edge_penalties or {}

    ex, ey = to_local(end.lat, end.lon)
    ex, ey = float(ex), float(ey)

    def heuristic(node: int) -> float:
        if node >= n:
            return 0.0
        dx = node_x[node] - ex
        dy = node_y[node] - ey
        return math.sqrt(dx * dx + dy * dy) / speed

    best_cost: dict[int, float] = {VS: 0.0}
    parent: dict[int, tuple[int, PathLeg]] = {}
    elapsed: dict[int, float] = {VS: 0.0}
    heap: list[tuple[float, int]] = [(0.0, VS)]
    closed: set[int] = set()

    while heap:
        f, node = heapq.heappop(heap)
        if node in closed:
            continue
        closed.add(node)

        if node == VE:
            break

        g = best_cost[node]
        t_now = elapsed[node]

        if node == VS:
            transitions = [(nb, c, tm, leg) for nb, c, tm, leg in from_start]
            for c, tm, leg in direct:
                transitions.append((VE, c, tm, leg))
        else:
            transitions = []
            for i in range(adj_ptr[node], adj_ptr[node + 1]):
                e = adj_edges[i]
                c = costs[e]
                pen = penalties.get(e)
                if pen is not None:
                    c *= pen
                transitions.append((edge_to[e], c, times[e], PathLeg(e, 0.0, 1.0)))
            hop = to_end.get(node)
            if hop is not None:
                transitions.append((VE, hop[0], hop[1], hop[2]))

        for nxt, c, tm, leg in transitions:
            if nxt in closed:
                continue
            ng = g + c
            if ng < best_cost.get(nxt, float("inf")):
                best_cost[nxt] = ng
                elapsed[nxt] = t_now + tm
                parent[nxt] = (node, leg)
                heapq.heappush(heap, (ng + heuristic(nxt), nxt))

    if VE not in parent and VE not in best_cost:
        return None

    # Walk the parent chain back to the virtual start.
    legs: list[PathLeg] = []
    cur = VE
    guard = 0
    while cur != VS:
        step = parent.get(cur)
        if step is None:
            return None
        prev, leg = step
        legs.append(leg)
        cur = prev
        guard += 1
        if guard > 5_000_000:  # pathological cycle guard
            return None
    legs.reverse()

    distance = sum(
        graph.edge_length(leg.edge_id) * leg.fraction for leg in legs
    )
    return PathResult(
        legs=legs,
        distance_m=distance,
        duration_s=elapsed.get(VE, distance / speed),
        cost=best_cost.get(VE, 0.0),
        coords=path_coords(graph, legs),
    )


def one_to_many(
    index: GraphIndex,
    origin: SnapPoint,
    costs: list[float],
    times: list[float],
    max_time_s: float,
) -> dict[int, tuple[float, float]]:
    """Dijkstra from ``origin`` to every node within ``max_time_s`` of walking.

    Used to find which transit stops are reachable on foot. One search covering
    every nearby stop is dramatically cheaper than an A* per stop, and there
    can easily be fifty bus stops inside a ten-minute walk in central DC.

    Returns ``{node: (cost, walking_seconds)}``. Cost carries the risk
    weighting, walking seconds does not, so the caller can recover the risk
    surcharge as the difference.
    """
    adj_ptr = index.adj_ptr
    adj_edges = index.adj_edges
    edge_to = index.edge_to

    s_fwd, s_rev = index.segment_edges(origin.seg_id)
    t = origin.fraction

    best: dict[int, tuple[float, float]] = {}
    heap: list[tuple[float, float, int]] = []

    if t < 1.0:
        f = 1.0 - t
        heapq.heappush(heap, (costs[s_fwd] * f, times[s_fwd] * f, origin.node_b))
    if t > 0.0:
        heapq.heappush(heap, (costs[s_rev] * t, times[s_rev] * t, origin.node_a))

    while heap:
        cost, elapsed, node = heapq.heappop(heap)
        if node in best:
            continue
        if elapsed > max_time_s:
            continue
        best[node] = (cost, elapsed)

        for i in range(adj_ptr[node], adj_ptr[node + 1]):
            e = adj_edges[i]
            nxt = edge_to[e]
            if nxt in best:
                continue
            nt = elapsed + times[e]
            if nt <= max_time_s:
                heapq.heappush(heap, (cost + costs[e], nt, nxt))

    return best


def path_coords(graph: WalkGraph, legs: list[PathLeg]) -> list[tuple[float, float]]:
    """Stitch a path's legs into a single (lat, lon) polyline."""
    out: list[tuple[float, float]] = []
    for leg in legs:
        coords = graph.edge_coords(leg.edge_id)
        if leg.from_frac > 0.0 or leg.to_frac < 1.0:
            coords = _slice_polyline(coords, leg.from_frac, leg.to_frac)
        if not coords:
            continue
        # Avoid duplicating the shared vertex between consecutive legs.
        if out and out[-1] == coords[0]:
            out.extend(coords[1:])
        else:
            out.extend(coords)
    return out


def _slice_polyline(
    coords: list[tuple[float, float]], a: float, b: float
) -> list[tuple[float, float]]:
    """The portion of a polyline between fractions ``a`` and ``b``."""
    if b <= a:
        return [interpolate_polyline(coords, a)] if coords else []
    if a <= 0.0 and b >= 1.0:
        return list(coords)
    if a > 0.0:
        _, coords = split_polyline(coords, a)
        # The remaining piece is now (1-a) of the original length, so the end
        # fraction has to be rescaled into the new polyline's own coordinates.
        b = (b - a) / (1.0 - a) if a < 1.0 else 1.0
    if b < 1.0:
        coords, _ = split_polyline(coords, b)
    return coords
