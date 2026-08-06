"""The routable walk graph.

The graph is stored as flat numpy arrays rather than Python objects. DC's
pedestrian network is on the order of 10^5 segments, and a dict-of-objects
representation makes A* roughly two orders of magnitude slower and blows up
memory. Flat arrays keep a full cross-town route in the low milliseconds.

Two levels of structure:

* **Segments** carry geometry and all the scored attributes. One segment per
  undirected piece of street between two graph nodes.
* **Directed edges** are what the router traverses. Each walkable segment
  yields two of them, differing only in direction of travel, both pointing at
  the same segment for geometry and attributes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import CAMERAS, CRIME, ISOLATION, LIGHTING, RISK, ROUTING


@dataclass
class WalkGraph:
    """A scored, routable pedestrian network."""

    # --- nodes ---------------------------------------------------------
    node_lat: np.ndarray  # float64 (n_nodes,)
    node_lon: np.ndarray  # float64 (n_nodes,)
    node_x: np.ndarray  # float64, local metres
    node_y: np.ndarray  # float64, local metres

    # --- segments (undirected, carry geometry + scores) ----------------
    seg_geom_ptr: np.ndarray  # int32 (n_segs + 1,) offsets into seg_geom
    seg_geom: np.ndarray  # float32 (n_coords, 2) lat/lon
    seg_length: np.ndarray  # float32 (n_segs,) metres
    # Crime scores for every selectable lookback window, normalised 0-1.
    # Shape (n_windows, 2, n_segs); axis 1 is [day, night]. Each window is
    # scored and normalised independently, so "the last 30 days" is its own
    # picture of the city rather than a faded copy of the annual one.
    seg_crime: np.ndarray  # float32
    seg_lit: np.ndarray  # float32 (n_segs,) 0=dark, 1=well lit
    seg_isolation: np.ndarray  # float32 (n_segs,) 0-1
    seg_camera: np.ndarray  # float32 (n_segs,) ALPR exposure 0-1
    seg_name: np.ndarray  # int32 (n_segs,) index into `names`
    seg_highway: np.ndarray  # int8 (n_segs,) index into `highways`

    # --- directed edges ------------------------------------------------
    edge_from: np.ndarray  # int32 (n_edges,)
    edge_to: np.ndarray  # int32 (n_edges,)
    edge_seg: np.ndarray  # int32 (n_edges,)
    edge_rev: np.ndarray  # bool (n_edges,) traverse geometry backwards

    # --- CSR adjacency: outgoing edge ids per node ---------------------
    adj_ptr: np.ndarray  # int32 (n_nodes + 1,)
    adj_edges: np.ndarray  # int32 (n_edges,)

    names: list[str]
    highways: list[str]
    # Lookback in days for each slice of ``seg_crime``, in the same order.
    crime_windows: list[int]
    meta: dict

    # ------------------------------------------------------------------
    # Basic properties
    # ------------------------------------------------------------------

    @property
    def n_nodes(self) -> int:
        return len(self.node_lat)

    @property
    def n_segments(self) -> int:
        return len(self.seg_length)

    @property
    def n_edges(self) -> int:
        return len(self.edge_from)

    def segment_coords(self, seg_id: int) -> list[tuple[float, float]]:
        """Geometry of a segment as a list of (lat, lon)."""
        a, b = int(self.seg_geom_ptr[seg_id]), int(self.seg_geom_ptr[seg_id + 1])
        return [(float(la), float(lo)) for la, lo in self.seg_geom[a:b]]

    def edge_coords(self, edge_id: int) -> list[tuple[float, float]]:
        """Geometry of a directed edge, oriented in the direction of travel."""
        coords = self.segment_coords(int(self.edge_seg[edge_id]))
        return coords[::-1] if self.edge_rev[edge_id] else coords

    def edge_length(self, edge_id: int) -> float:
        return float(self.seg_length[self.edge_seg[edge_id]])

    def edge_name(self, edge_id: int) -> str:
        return self.names[int(self.seg_name[self.edge_seg[edge_id]])]

    def edge_highway(self, edge_id: int) -> str:
        return self.highways[int(self.seg_highway[self.edge_seg[edge_id]])]

    def outgoing(self, node: int) -> np.ndarray:
        """Edge ids leaving ``node``."""
        return self.adj_edges[self.adj_ptr[node] : self.adj_ptr[node + 1]]

    # ------------------------------------------------------------------
    # Crime windows
    # ------------------------------------------------------------------

    def window_index(self, window_days: int | None) -> int:
        """Slice of ``seg_crime`` for a requested lookback.

        Unknown values snap to the nearest available window rather than
        raising: a client asking for 90 days should get the closest thing the
        graph was built with, not an error.
        """
        if not self.crime_windows:
            return 0
        if window_days is None:
            window_days = CRIME.default_window_days
        return min(
            range(len(self.crime_windows)),
            key=lambda i: abs(self.crime_windows[i] - window_days),
        )

    def resolved_window(self, window_days: int | None) -> int | None:
        """The window actually used for a request, after snapping."""
        if not self.crime_windows:
            return None
        return self.crime_windows[self.window_index(window_days)]

    def crime_scores(self, is_night: bool, window_days: int | None = None) -> np.ndarray:
        """Per-segment crime score for one window and time of day."""
        return self.seg_crime[self.window_index(window_days), 1 if is_night else 0]

    @property
    def seg_crime_day(self) -> np.ndarray:
        """Default-window daytime crime, for callers that do not pick one."""
        return self.crime_scores(is_night=False)

    @property
    def seg_crime_night(self) -> np.ndarray:
        return self.crime_scores(is_night=True)

    # ------------------------------------------------------------------
    # Risk
    # ------------------------------------------------------------------

    def segment_risk(
        self, is_night: bool, window_days: int | None = None
    ) -> np.ndarray:
        """Per-segment composite risk in [0, 1] for the given period.

        This is the number the router's cost function is built on. The three
        components are combined as a weighted mean (not a sum), so the result
        stays in [0, 1] and the weights stay interpretable as "how much does
        this factor matter" rather than needing to sum to one.

        Darkness is raised to ``LIGHTING.darkness_exponent`` first. With an
        exponent below 1 the curve rises steeply out of zero, so a street that
        is merely somewhat worse lit than its neighbours already carries a
        substantial share of the penalty rather than a proportional sliver.
        Without it, DC's near-universal street lighting compresses every
        candidate route into the same narrow band and the night score stops
        discriminating between them at all.
        """
        w_crime, w_dark, w_iso = RISK.for_period(is_night)
        total = w_crime + w_dark + w_iso
        if total <= 0:
            return np.zeros(self.n_segments, dtype=np.float32)

        crime = self.crime_scores(is_night, window_days)
        darkness = np.clip(1.0 - self.seg_lit, 0.0, 1.0) ** LIGHTING.darkness_exponent
        risk = (
            w_crime * crime + w_dark * darkness + w_iso * self.seg_isolation
        ) / total
        return np.clip(risk, 0.0, 1.0).astype(np.float32)

    def edge_costs(
        self,
        is_night: bool,
        risk_lambda: float,
        avoid_cameras: bool = False,
        window_days: int | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per-directed-edge (cost, base_time) arrays.

        ``cost`` is what A* minimises: walking time inflated by risk. A
        segment with risk 1.0 at ``risk_lambda=7`` costs eight times its true
        walking time, which is what makes the router willing to take a long
        way round to avoid it.

        ``base_time`` is the honest walking time, reported to the user.
        """
        seg_risk = self.segment_risk(is_night, window_days)
        seg_time = self.seg_length / ROUTING.walk_speed_mps

        multiplier = 1.0 + risk_lambda * seg_risk
        if avoid_cameras:
            multiplier = multiplier + CAMERAS.avoid_lambda * self.seg_camera

        seg_cost = seg_time * multiplier
        return seg_cost[self.edge_seg], seg_time[self.edge_seg]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path, meta_path: Path | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            node_lat=self.node_lat,
            node_lon=self.node_lon,
            node_x=self.node_x,
            node_y=self.node_y,
            seg_geom_ptr=self.seg_geom_ptr,
            seg_geom=self.seg_geom,
            seg_length=self.seg_length,
            seg_crime=self.seg_crime,
            crime_windows=np.array(self.crime_windows, dtype=np.int32),
            seg_lit=self.seg_lit,
            seg_isolation=self.seg_isolation,
            seg_camera=self.seg_camera,
            seg_name=self.seg_name,
            seg_highway=self.seg_highway,
            edge_from=self.edge_from,
            edge_to=self.edge_to,
            edge_seg=self.edge_seg,
            edge_rev=self.edge_rev,
            adj_ptr=self.adj_ptr,
            adj_edges=self.adj_edges,
        )
        meta_path = meta_path or path.with_name(path.stem + "_meta.json")
        meta_path.write_text(
            json.dumps(
                {"names": self.names, "highways": self.highways, "meta": self.meta},
                indent=1,
            )
        )

    @classmethod
    def load(cls, path: Path, meta_path: Path | None = None) -> WalkGraph:
        z = np.load(path, allow_pickle=False)
        meta_path = meta_path or path.with_name(path.stem + "_meta.json")
        sidecar = json.loads(Path(meta_path).read_text())

        if "seg_crime" in z:
            seg_crime = z["seg_crime"]
            crime_windows = [int(w) for w in z["crime_windows"]]
        else:
            # A graph built before crime windows existed. Present its single
            # pair of surfaces as one window so the app still runs; the window
            # picker will show one option until the graph is rebuilt.
            seg_crime = np.stack(
                [np.stack([z["seg_crime_day"], z["seg_crime_night"]])]
            ).astype(np.float32)
            crime_windows = [CRIME.default_window_days]

        return cls(
            node_lat=z["node_lat"],
            node_lon=z["node_lon"],
            node_x=z["node_x"],
            node_y=z["node_y"],
            seg_geom_ptr=z["seg_geom_ptr"],
            seg_geom=z["seg_geom"],
            seg_length=z["seg_length"],
            seg_crime=seg_crime,
            crime_windows=crime_windows,
            seg_lit=z["seg_lit"],
            seg_isolation=z["seg_isolation"],
            seg_camera=z["seg_camera"],
            seg_name=z["seg_name"],
            seg_highway=z["seg_highway"],
            edge_from=z["edge_from"],
            edge_to=z["edge_to"],
            edge_seg=z["edge_seg"],
            edge_rev=z["edge_rev"],
            adj_ptr=z["adj_ptr"],
            adj_edges=z["adj_edges"],
            names=sidecar["names"],
            highways=sidecar["highways"],
            meta=sidecar.get("meta", {}),
        )


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------


@dataclass
class RawSegment:
    """A street piece before it becomes part of the compiled graph."""

    node_a: int
    node_b: int
    coords: list[tuple[float, float]]
    length_m: float
    name: str
    highway: str
    tags: dict[str, str]


def isolation_from_tags(highway: str, tags: dict[str, str]) -> float:
    """Risk contributed by the physical character of the way itself.

    Where we have no lamp within 45m and no crime within 450m, these tags are
    the only signal we have that a route is a dark cut-through rather than a
    main road, so it is worth getting right.
    """
    score = ISOLATION.by_highway.get(highway, ISOLATION.default)

    if tags.get("tunnel") in ("yes", "building_passage") or tags.get("covered") == "yes":
        score += ISOLATION.tunnel_penalty
    if tags.get("service") == "alley":
        score += ISOLATION.alley_penalty
    if tags.get("sidewalk") in ("no", "none"):
        score += ISOLATION.no_sidewalk_penalty
    if tags.get("lit") == "yes":
        score += ISOLATION.lit_tag_bonus
    elif tags.get("lit") == "no":
        score += 0.15

    # A footpath through green space is the classic "shortcut you would not
    # take at night" — the OSM tags for the surrounding land use rarely make
    # it onto the way itself, so we key off the path tags we do have.
    if highway in ("path", "footway", "track") and tags.get("surface") in (
        "ground",
        "dirt",
        "grass",
        "unpaved",
        "gravel",
        "earth",
    ):
        score += ISOLATION.park_penalty

    return float(min(1.0, max(0.0, score)))


def build_graph(
    node_coords: dict[int, tuple[float, float]],
    segments: list[RawSegment],
    meta: dict | None = None,
) -> WalkGraph:
    """Compile raw segments into a :class:`WalkGraph`.

    Scored attributes (lighting, crime, cameras) are left at zero here; the
    scoring pass fills them in afterwards. Isolation is derived from tags and
    so can be computed immediately.
    """
    from ..geo import to_local

    # Renumber the (sparse, OSM-derived) node ids into a dense 0..n-1 range.
    used = sorted({s.node_a for s in segments} | {s.node_b for s in segments})
    remap = {old: new for new, old in enumerate(used)}

    lat = np.array([node_coords[o][0] for o in used], dtype=np.float64)
    lon = np.array([node_coords[o][1] for o in used], dtype=np.float64)
    x, y = to_local(lat, lon)

    name_index: dict[str, int] = {"": 0}
    names: list[str] = [""]
    hw_index: dict[str, int] = {}
    highways: list[str] = []

    geom_parts: list[np.ndarray] = []
    geom_ptr = [0]
    seg_len, seg_name_ids, seg_hw_ids, seg_iso = [], [], [], []
    e_from, e_to, e_seg, e_rev = [], [], [], []

    for i, s in enumerate(segments):
        arr = np.array(s.coords, dtype=np.float32)
        geom_parts.append(arr)
        geom_ptr.append(geom_ptr[-1] + len(arr))

        seg_len.append(s.length_m)

        if s.name not in name_index:
            name_index[s.name] = len(names)
            names.append(s.name)
        seg_name_ids.append(name_index[s.name])

        if s.highway not in hw_index:
            hw_index[s.highway] = len(highways)
            highways.append(s.highway)
        seg_hw_ids.append(hw_index[s.highway])

        seg_iso.append(isolation_from_tags(s.highway, s.tags))

        a, b = remap[s.node_a], remap[s.node_b]
        # Walking is bidirectional on every walkable way; oneway restrictions
        # apply to vehicles, not pedestrians.
        e_from.extend([a, b])
        e_to.extend([b, a])
        e_seg.extend([i, i])
        e_rev.extend([False, True])

    n_nodes = len(used)
    edge_from = np.array(e_from, dtype=np.int32)

    # Build CSR adjacency by counting sort on the source node.
    counts = np.bincount(edge_from, minlength=n_nodes)
    adj_ptr = np.zeros(n_nodes + 1, dtype=np.int32)
    np.cumsum(counts, out=adj_ptr[1:])
    adj_edges = np.argsort(edge_from, kind="stable").astype(np.int32)

    return WalkGraph(
        node_lat=lat,
        node_lon=lon,
        node_x=x,
        node_y=y,
        seg_geom_ptr=np.array(geom_ptr, dtype=np.int32),
        seg_geom=(
            np.concatenate(geom_parts) if geom_parts else np.zeros((0, 2), np.float32)
        ),
        seg_length=np.array(seg_len, dtype=np.float32),
        seg_crime=np.zeros(
            (len(CRIME.windows_days), 2, len(segments)), dtype=np.float32
        ),
        crime_windows=list(CRIME.windows_days),
        seg_lit=np.zeros(len(segments), dtype=np.float32),
        seg_isolation=np.array(seg_iso, dtype=np.float32),
        seg_camera=np.zeros(len(segments), dtype=np.float32),
        seg_name=np.array(seg_name_ids, dtype=np.int32),
        seg_highway=np.array(seg_hw_ids, dtype=np.int8),
        edge_from=edge_from,
        edge_to=np.array(e_to, dtype=np.int32),
        edge_seg=np.array(e_seg, dtype=np.int32),
        edge_rev=np.array(e_rev, dtype=bool),
        adj_ptr=adj_ptr,
        adj_edges=adj_edges,
        names=names,
        highways=highways,
        meta=meta or {},
    )


def largest_connected_component(graph: WalkGraph) -> np.ndarray:
    """Boolean mask of nodes in the graph's largest connected component.

    OSM extracts always contain orphan fragments — a driveway clipped at the
    bbox, a mis-tagged path. Routing to one of them fails in a way that looks
    like a bug, so the builder drops them.
    """
    n = graph.n_nodes
    seen = np.zeros(n, dtype=bool)
    best = np.zeros(n, dtype=bool)
    best_size = 0

    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component = [start]
        while stack:
            node = stack.pop()
            for e in graph.outgoing(node):
                nxt = int(graph.edge_to[e])
                if not seen[nxt]:
                    seen[nxt] = True
                    stack.append(nxt)
                    component.append(nxt)
        if len(component) > best_size:
            best_size = len(component)
            best = np.zeros(n, dtype=bool)
            best[component] = True

    return best
