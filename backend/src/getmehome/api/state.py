"""Process-wide loaded state, one set per city.

The graph, spatial index and transit timetable are expensive to build and
completely immutable once loaded, so they are constructed once and shared
across requests.

**Cities load lazily and stay loaded.** Holding every city resident from
startup would mean paying for New York's graph on a server that only ever
serves Washington, and New York's is several times the larger — the OSM extract
covers the whole state. So the first request for a city pays the load and every
one after it is free. The cost of that choice is a slow first request after a
restart, which is why `GETMEHOME_PRELOAD_CITIES` exists for deployments that
would rather pay it at boot.

Nothing is ever evicted. A loaded city is a large allocation and dropping one
under memory pressure would just mean reloading it on the next request, turning
a memory problem into a latency problem without fixing either. If both cities
genuinely do not fit, the answer is a bigger machine or one process per city.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from ..config import (
    CAMERAS_NAME,
    CRIME_POINTS_NAME,
    DATA_DIR,
    GRAPH_META_NAME,
    GRAPH_NAME,
    PLACES_NAME,
    TRANSIT_NAME,
)
from ..graph.model import WalkGraph
from ..live import live_provider_for
from ..places import PlaceIndex
from ..routing.astar import GraphIndex
from ..routing.multimodal import TransitIndex
from ..safety.cameras import AlprCamera
from ..safety.hexgrid import CrimeIndex

log = logging.getLogger("getmehome.state")


@dataclass
class AppState:
    """Everything loaded for one city."""

    city: City
    graph: WalkGraph
    index: GraphIndex
    cameras: list[AlprCamera]
    transit: TransitIndex | None = None
    crime: CrimeIndex | None = None
    places: PlaceIndex | None = None
    # Real-time transit client for this city's agency. Always present; it
    # reports itself disabled when it has no key or no support, so callers
    # need no separate check.
    live: object = field(default=None)

    def __post_init__(self) -> None:
        if self.live is None:
            self.live = live_provider_for(self.city)

    @property
    def has_transit(self) -> bool:
        return self.transit is not None


class CityStates:
    """Lazily loaded state, keyed by city slug.

    Locked because two requests for an unloaded city can arrive at once, and
    loading a graph twice concurrently would double the peak memory for no
    reason — the loser of the race throws its copy away.
    """

    def __init__(self, root: Path = DATA_DIR) -> None:
        self.root = root
        self._states: dict[str, AppState] = {}
        self._lock = threading.Lock()

    def available(self) -> list[City]:
        """Cities that have been built, in registry order."""
        return [c for c in CITIES.values() if self.is_built(c)]

    def is_built(self, city: City) -> bool:
        return (city.build_dir(self.root) / GRAPH_NAME).exists()

    def is_loaded(self, city: City) -> bool:
        return city.slug in self._states

    def get(self, slug: str | None) -> AppState:
        """Load a city if needed, then return its state."""
        city = get_city(slug)
        existing = self._states.get(city.slug)
        if existing is not None:
            return existing

        with self._lock:
            # Re-check: another thread may have loaded it while we waited.
            existing = self._states.get(city.slug)
            if existing is not None:
                return existing
            state = load_city(city, self.root)
            self._states[city.slug] = state
            return state

    def set(self, state: AppState) -> None:
        """Inject state directly. Used by tests to avoid touching disk."""
        with self._lock:
            self._states[state.city.slug] = state

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def preload(self, slugs: list[str]) -> None:
        """Load cities at startup rather than on first request."""
        for slug in slugs:
            try:
                self.get(slug)
            except (FileNotFoundError, KeyError) as exc:
                log.warning("could not preload %s: %s", slug, exc)


def load_city(city: City, root: Path = DATA_DIR) -> AppState:
    """Load one city's build output from disk."""
    build = city.build_dir(root)
    graph_path = build / GRAPH_NAME

    if not graph_path.exists():
        raise FileNotFoundError(
            f"No graph for {city.name} at {graph_path}. Build it first:\n"
            f"    cd backend && python -m getmehome.graph.build --city {city.slug}"
        )

    log.info("loading %s from %s", city.name, graph_path)
    graph = WalkGraph.load(graph_path, build / GRAPH_META_NAME)
    log.info(
        "%s graph: %d nodes, %d segments, %d edges",
        city.slug, graph.n_nodes, graph.n_segments, graph.n_edges,
    )

    # A graph built for another city loaded under this slug would route
    # confidently and wrongly, so the mismatch is fatal rather than a warning.
    built_for = graph.meta.get("city")
    if built_for and built_for != city.slug:
        raise RuntimeError(
            f"{graph_path} was built for {built_for!r}, not {city.slug!r}. "
            f"Rebuild it, or move it under data/{built_for}/build."
        )

    index = GraphIndex(graph)

    cameras: list[AlprCamera] = []
    camera_file = build / CAMERAS_NAME
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
    log.info("%s: %d ALPR cameras", city.slug, len(cameras))

    transit = None
    transit_path = build / TRANSIT_NAME
    if transit_path.exists():
        with transit_path.open("rb") as fh:
            network = pickle.load(fh)
        transit = TransitIndex(network, index)
        log.info(
            "%s transit: %d stops, %d patterns",
            city.slug, network.n_stops, len(network.patterns),
        )
    else:
        log.warning("%s: no transit timetable at %s — walking only", city.slug, transit_path)

    crime = None
    crime_path = build / CRIME_POINTS_NAME
    if crime_path.exists():
        crime = CrimeIndex.load(crime_path)
        log.info("%s: %d crime incidents for the map grid", city.slug, crime.count)
    else:
        log.warning(
            "%s: no crime points at %s — the crime grid overlay will be empty. "
            "Rebuild to generate it.",
            city.slug, crime_path,
        )

    places = None
    places_path = build / PLACES_NAME
    if places_path.exists():
        places = PlaceIndex.load(places_path)
        log.info("%s: %d searchable places", city.slug, len(places))
    else:
        log.warning(
            "%s: no place index at %s — search will fall back to the external "
            "geocoder. Rebuild to generate it.",
            city.slug, places_path,
        )

    return AppState(
        city=city,
        graph=graph,
        index=index,
        cameras=cameras,
        transit=transit,
        crime=crime,
        places=places,
    )


_states = CityStates()


def states() -> CityStates:
    return _states


def load_state(slug: str | None = None) -> AppState:
    """Load one city eagerly. Kept for the startup path and for tests."""
    return _states.get(slug or DEFAULT_CITY)


def get_state(slug: str | None = None) -> AppState:
    """State for a city, loading it if this is the first request for it."""
    return _states.get(slug)


def set_state(state: AppState) -> None:
    """Inject state directly. Used by tests to avoid touching disk."""
    _states.set(state)


def preload_from_env() -> None:
    """Honour ``GETMEHOME_PRELOAD_CITIES``.

    A comma-separated list of slugs, or ``all``. Unset means load on demand,
    which keeps startup fast and the first request for each city slow.
    """
    raw = os.environ.get("GETMEHOME_PRELOAD_CITIES", "").strip()
    if not raw:
        return
    slugs = sorted(CITIES) if raw.lower() == "all" else [
        s.strip() for s in raw.split(",") if s.strip()
    ]
    log.info("preloading cities: %s", ", ".join(slugs))
    _states.preload(slugs)
