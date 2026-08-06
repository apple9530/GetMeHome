"""Process-wide loaded state.

The graph, spatial index and transit timetable are expensive to build and
completely immutable once loaded, so they are constructed once at startup and
shared across requests. Loading lazily on first request instead would make the
first user of every deploy wait several seconds.
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    BUILD_DIR,
    CRIME_POINTS_FILE,
    GRAPH_FILE,
    GRAPH_META_FILE,
    PLACES_FILE,
    TRANSIT_FILE,
)
from ..graph.model import WalkGraph
from ..ingest.wmata_live import WmataLive
from ..places import PlaceIndex
from ..routing.astar import GraphIndex
from ..routing.multimodal import TransitIndex
from ..safety.cameras import AlprCamera
from ..safety.hexgrid import CrimeIndex

log = logging.getLogger("getmehome.state")


@dataclass
class AppState:
    graph: WalkGraph
    index: GraphIndex
    cameras: list[AlprCamera]
    transit: TransitIndex | None = None
    crime: CrimeIndex | None = None
    places: PlaceIndex | None = None
    # Real-time WMATA client. Always present; it reports itself disabled when
    # no API key is configured, so callers need no separate check.
    live: WmataLive = field(default_factory=WmataLive)

    @property
    def has_transit(self) -> bool:
        return self.transit is not None


_state: AppState | None = None


def load_state(
    graph_path: Path = GRAPH_FILE,
    meta_path: Path = GRAPH_META_FILE,
    transit_path: Path = TRANSIT_FILE,
    crime_path: Path = CRIME_POINTS_FILE,
    places_path: Path = PLACES_FILE,
) -> AppState:
    """Load everything from the build directory."""
    global _state

    if not graph_path.exists():
        raise FileNotFoundError(
            f"No graph at {graph_path}. Build it first:\n"
            f"    cd backend && python -m getmehome.graph.build"
        )

    log.info("loading graph from %s", graph_path)
    graph = WalkGraph.load(graph_path, meta_path)
    log.info(
        "graph: %d nodes, %d segments, %d edges",
        graph.n_nodes, graph.n_segments, graph.n_edges,
    )

    index = GraphIndex(graph)

    cameras: list[AlprCamera] = []
    camera_file = BUILD_DIR / "cameras.json"
    if camera_file.exists():
        cameras = [
            AlprCamera(
                lat=r["lat"],
                lon=r["lon"],
                direction_deg=r.get("direction"),
                operator=r.get("operator", ""),
                osm_id=r.get("id", ""),
            )
            for r in json.loads(camera_file.read_text())
        ]
    log.info("%d ALPR cameras", len(cameras))

    transit = None
    if transit_path.exists():
        with transit_path.open("rb") as fh:
            network = pickle.load(fh)
        transit = TransitIndex(network, index)
        log.info(
            "transit: %d stops, %d patterns",
            network.n_stops, len(network.patterns),
        )
    else:
        log.warning("no transit timetable at %s — walking only", transit_path)

    crime = None
    if crime_path.exists():
        crime = CrimeIndex.load(crime_path)
        log.info("%d crime incidents for the map grid", crime.count)
    else:
        log.warning(
            "no crime points at %s — the crime grid overlay will be empty. "
            "Rebuild the graph to generate it.",
            crime_path,
        )

    places = None
    if places_path.exists():
        places = PlaceIndex.load(places_path)
        log.info("%d searchable places", len(places))
    else:
        log.warning(
            "no place index at %s — search will fall back to the external "
            "geocoder. Rebuild the graph to generate it.",
            places_path,
        )

    _state = AppState(
        graph=graph,
        index=index,
        cameras=cameras,
        transit=transit,
        crime=crime,
        places=places,
    )
    return _state


def get_state() -> AppState:
    if _state is None:
        raise RuntimeError("application state not loaded")
    return _state


def set_state(state: AppState) -> None:
    """Inject state directly. Used by tests to avoid touching disk."""
    global _state
    _state = state
