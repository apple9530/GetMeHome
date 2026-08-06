"""Stitching walking and transit into complete door-to-door itineraries.

The transit search and the walk router are separate engines, and the join
between them is where most of the useful work happens:

* Access and egress walks are found with a single one-to-many Dijkstra each,
  costed with the same risk weighting as any other walk. The *difference*
  between the risk-weighted cost and the plain walking time becomes the
  penalty RAPTOR sees, so a stop that is four minutes away down a dark
  underpass competes as if it were further than one six minutes away on a
  lit main road.
* Waiting is priced. A stop's risk comes from the street it stands on, and
  RAPTOR charges for time spent standing there.
* The chosen access and egress walks are then re-routed properly with A* so
  the itinerary comes back with real geometry and real turn instructions,
  rather than the straight lines most transit apps draw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import ROUTING, TRANSIT
from ..ingest.gtfs import TransitNetwork
from ..nav.instructions import NavStep, build_steps
from ..safety.cameras import AlprCamera, coverage_along_route
from ..safety.scoring import SafetyBreakdown, score_route
from .alternatives import compute_options
from .astar import GraphIndex, SnapPoint, one_to_many, shortest_path
from .raptor import AccessPoint, Journey, run_raptor

# Walking legs shorter than this are dropped. A stop can snap to essentially
# the same point as the origin, and "Walk 0 m to the bus stop" is noise in the
# itinerary and produces a degenerate one-vertex polyline on the map.
MIN_WALK_LEG_M = 8.0


@dataclass
class ItineraryLeg:
    """One leg of a door-to-door itinerary."""

    mode: str  # "walk" | "metro" | "bus" | "rail" | "transfer"
    distance_m: float = 0.0
    duration_s: float = 0.0
    coords: list[tuple[float, float]] = field(default_factory=list)
    steps: list[NavStep] = field(default_factory=list)
    safety: SafetyBreakdown | None = None

    # Transit-only fields.
    route_name: str = ""
    headsign: str = ""
    from_stop_name: str = ""
    to_stop_name: str = ""
    departure_s: int = 0
    arrival_s: int = 0
    n_stops: int = 0


@dataclass
class Itinerary:
    kind: str  # "walk" | "transit"
    label: str
    summary: str
    legs: list[ItineraryLeg]
    duration_s: float
    walk_distance_m: float
    safety: SafetyBreakdown
    departure_s: int
    arrival_s: int
    n_transfers: int = 0
    cameras: dict = field(default_factory=dict)


class TransitIndex:
    """Binds a transit network to a walk graph.

    Every stop is snapped onto the walk graph once at build time. Doing it per
    request would dominate the response time, and stop positions do not move.
    """

    def __init__(self, network: TransitNetwork, index: GraphIndex) -> None:
        self.network = network
        self.index = index
        self.stop_node: list[int] = []

        graph = index.graph
        # Snap every stop once, then read its risk out of each (window, period)
        # surface. Twelve thousand stops times eight surfaces is a few hundred
        # kilobytes and saves re-snapping on every request.
        self.windows = list(graph.crime_windows) or [None]
        risk = {
            (w, night): graph.segment_risk(night, w)
            for w in self.windows
            for night in (False, True)
        }
        self._stop_risk: dict[tuple, list[float]] = {k: [] for k in risk}

        for stop in network.stops:
            snap = index.snap(stop.lat, stop.lon, max_distance_m=150.0)
            self.stop_node.append(-1 if snap is None else snap.node_a)
            for key, arr in risk.items():
                # An unsnappable stop gets a neutral 0.5 rather than 0: we do
                # not know it is safe, we know nothing about it.
                self._stop_risk[key].append(
                    0.5 if snap is None else float(arr[snap.seg_id])
                )

        # node -> stops standing on it, for the one-to-many lookup.
        self.node_stops: dict[int, list[int]] = {}
        for si, node in enumerate(self.stop_node):
            if node >= 0:
                self.node_stops.setdefault(node, []).append(si)

    def stop_risk(self, is_night: bool, window_days: int | None = None) -> list[float]:
        window = self.index.graph.resolved_window(window_days)
        return self._stop_risk[(window, is_night)]

    def reachable_stops(
        self,
        origin: SnapPoint,
        costs: list[float],
        times: list[float],
        max_walk_m: float,
    ) -> list[AccessPoint]:
        """Stops within a walk of ``origin``, with their risk surcharge."""
        max_time = max_walk_m / ROUTING.walk_speed_mps
        reached = one_to_many(self.index, origin, costs, times, max_time)

        points: list[AccessPoint] = []
        for node, (cost, seconds) in reached.items():
            for si in self.node_stops.get(node, ()):
                points.append(
                    AccessPoint(
                        stop=si,
                        seconds=seconds,
                        penalty_s=max(0.0, cost - seconds),
                    )
                )
        return points


def _seconds_since_midnight(when: datetime) -> int:
    return when.hour * 3600 + when.minute * 60 + when.second


def _walk_leg(
    index: GraphIndex,
    a: SnapPoint,
    b: SnapPoint,
    costs: list[float],
    times: list[float],
    is_night: bool,
    destination_name: str,
    window_days: int | None = None,
) -> ItineraryLeg | None:
    """Route and describe one walking leg."""
    path = shortest_path(index, a, b, costs, times)
    if path is None:
        return None
    graph = index.graph
    return ItineraryLeg(
        mode="walk",
        distance_m=path.distance_m,
        duration_s=path.duration_s,
        coords=path.coords,
        steps=build_steps(
            graph, path.legs, is_night, destination_name, window_days
        ),
        safety=score_route(
            graph, [leg.edge_id for leg in path.legs], is_night, window_days
        ),
    )


def _combine_safety(legs: list[ItineraryLeg]) -> SafetyBreakdown:
    """Aggregate the walking legs' safety into one figure for the itinerary.

    Only walking legs count. Time on a train is not scored by this model, and
    averaging a train ride in as "perfectly safe" would let a long ride paper
    over a genuinely bad walk at the far end — which is exactly the walk the
    user needs warning about.
    """
    walks = [leg for leg in legs if leg.mode == "walk" and leg.safety is not None]
    if not walks:
        return SafetyBreakdown(100, 100, 100, 100, 100, 0, 0, "")

    total = sum(leg.distance_m for leg in walks) or 1.0
    w = [leg.distance_m / total for leg in walks]

    def blend(attr: str) -> int:
        return int(
            round(sum(wi * getattr(leg.safety, attr) for wi, leg in zip(w, walks, strict=True)))
        )

    worst = max(walks, key=lambda leg: leg.safety.worst_stretch_risk).safety
    return SafetyBreakdown(
        overall=blend("overall"),
        lighting=blend("lighting"),
        crime=blend("crime"),
        isolation=blend("isolation"),
        pct_well_lit=blend("pct_well_lit"),
        pct_high_crime=blend("pct_high_crime"),
        worst_stretch_risk=worst.worst_stretch_risk,
        worst_stretch_name=worst.worst_stretch_name,
    )


def build_transit_itinerary(
    transit: TransitIndex,
    journey: Journey,
    origin: SnapPoint,
    destination: SnapPoint,
    costs: list[float],
    times: list[float],
    is_night: bool,
    destination_name: str,
    label: str,
    cameras: list[AlprCamera] | None = None,
    window_days: int | None = None,
) -> Itinerary | None:
    """Turn a RAPTOR journey into a full itinerary with real walk geometry."""
    index = transit.index
    network = transit.network
    legs: list[ItineraryLeg] = []

    first_stop = network.stops[journey.access_stop]
    access_snap = index.snap(first_stop.lat, first_stop.lon, max_distance_m=150.0)
    if access_snap is None:
        return None

    access = _walk_leg(
        index, origin, access_snap, costs, times, is_night, first_stop.name,
        window_days,
    )
    if access is None:
        return None  # genuinely unroutable
    if access.distance_m >= MIN_WALK_LEG_M:
        legs.append(access)

    for leg in journey.legs:
        if leg.kind == "transfer":
            a = network.stops[leg.from_stop]
            b = network.stops[leg.to_stop]
            sa = index.snap(a.lat, a.lon, max_distance_m=150.0)
            sb = index.snap(b.lat, b.lon, max_distance_m=150.0)
            if sa and sb:
                walk = _walk_leg(
                    index, sa, sb, costs, times, is_night, b.name, window_days
                )
                if walk is not None and walk.distance_m >= MIN_WALK_LEG_M:
                    walk.from_stop_name = a.name
                    walk.to_stop_name = b.name
                    legs.append(walk)
            continue

        a = network.stops[leg.from_stop]
        b = network.stops[leg.to_stop]
        pattern = network.patterns[leg.pattern_id]
        coords = [(a.lat, a.lon)]
        coords += [
            (network.stops[s].lat, network.stops[s].lon) for s in leg.intermediate
        ]
        coords.append((b.lat, b.lon))

        legs.append(
            ItineraryLeg(
                mode=leg.mode or "transit",
                duration_s=leg.arrival_s - leg.departure_s,
                coords=coords,
                route_name=leg.route_name or pattern.route_name,
                headsign=leg.headsign,
                from_stop_name=a.name,
                to_stop_name=b.name,
                departure_s=leg.departure_s,
                arrival_s=leg.arrival_s,
                n_stops=len(leg.intermediate) + 1,
            )
        )

    last_stop = network.stops[journey.egress_stop]
    egress_snap = index.snap(last_stop.lat, last_stop.lon, max_distance_m=150.0)
    if egress_snap is None:
        return None
    egress = _walk_leg(
        index, egress_snap, destination, costs, times, is_night, destination_name,
        window_days,
    )
    if egress is None:
        return None
    if egress.distance_m >= MIN_WALK_LEG_M:
        legs.append(egress)

    walk_distance = sum(leg.distance_m for leg in legs if leg.mode == "walk")
    walk_before = sum(
        leg.duration_s for leg in legs[: _first_transit_index(legs)] if leg.mode == "walk"
    )
    depart = int(journey.departure_s - walk_before)
    arrive = int(
        journey.arrival_s
        + sum(leg.duration_s for leg in legs[_last_transit_index(legs) + 1 :])
    )

    all_coords = [c for leg in legs for c in leg.coords]
    return Itinerary(
        kind="transit",
        label=label,
        summary="",
        legs=legs,
        duration_s=arrive - depart,
        walk_distance_m=walk_distance,
        safety=_combine_safety(legs),
        departure_s=depart,
        arrival_s=arrive,
        n_transfers=journey.n_transfers,
        cameras=coverage_along_route(all_coords, cameras) if cameras else {},
    )


def _first_transit_index(legs: list[ItineraryLeg]) -> int:
    for i, leg in enumerate(legs):
        if leg.mode not in ("walk", "transfer"):
            return i
    return len(legs)


def _last_transit_index(legs: list[ItineraryLeg]) -> int:
    for i in range(len(legs) - 1, -1, -1):
        if legs[i].mode not in ("walk", "transfer"):
            return i
    return -1


def plan(
    index: GraphIndex,
    origin: SnapPoint,
    destination: SnapPoint,
    when: datetime,
    is_night: bool,
    modes: set[str],
    avoid_cameras: bool = False,
    transit: TransitIndex | None = None,
    cameras: list[AlprCamera] | None = None,
    destination_name: str = "your destination",
    window_days: int | None = None,
) -> list[Itinerary]:
    """Produce the full set of itineraries for a request."""
    graph = index.graph
    results: list[Itinerary] = []

    if "walk" in modes:
        for option in compute_options(
            index,
            origin,
            destination,
            is_night,
            avoid_cameras,
            cameras,
            window_days=window_days,
        ):
            steps = build_steps(
                graph, option.path.legs, is_night, destination_name, window_days
            )
            leg = ItineraryLeg(
                mode="walk",
                distance_m=option.path.distance_m,
                duration_s=option.path.duration_s,
                coords=option.path.coords,
                steps=steps,
                safety=option.safety,
            )
            start = _seconds_since_midnight(when)
            results.append(
                Itinerary(
                    kind="walk",
                    label=option.label,
                    summary=option.summary,
                    legs=[leg],
                    duration_s=option.path.duration_s,
                    walk_distance_m=option.path.distance_m,
                    safety=option.safety,
                    departure_s=start,
                    arrival_s=int(start + option.path.duration_s),
                    cameras=option.cameras,
                )
            )

    if "transit" in modes and transit is not None:
        results.extend(
            _plan_transit(
                index,
                transit,
                origin,
                destination,
                when,
                is_night,
                avoid_cameras,
                cameras,
                destination_name,
                window_days,
            )
        )

    return results


def _plan_transit(
    index: GraphIndex,
    transit: TransitIndex,
    origin: SnapPoint,
    destination: SnapPoint,
    when: datetime,
    is_night: bool,
    avoid_cameras: bool,
    cameras: list[AlprCamera] | None,
    destination_name: str,
    window_days: int | None = None,
) -> list[Itinerary]:
    graph = index.graph
    depart_s = _seconds_since_midnight(when)
    stop_risk = transit.stop_risk(is_night, window_days)

    out: list[Itinerary] = []
    seen: set[tuple] = set()

    # Two passes: one indifferent to risk (the fast option), one that prices
    # it (the safe option). Each uses walk costs matching its own objective.
    for label, lam, penalise in (
        ("Fastest", ROUTING.lambda_fastest, False),
        ("Safest", ROUTING.lambda_safest, True),
    ):
        cost_arr, time_arr = graph.edge_costs(
            is_night, lam, avoid_cameras, window_days
        )
        costs, times = cost_arr.tolist(), time_arr.tolist()

        access = transit.reachable_stops(
            origin, costs, times, TRANSIT.max_access_walk_m
        )
        egress = transit.reachable_stops(
            destination, costs, times, TRANSIT.max_access_walk_m
        )
        if not access or not egress:
            continue

        journeys = run_raptor(
            transit.network,
            access,
            egress,
            depart_s,
            stop_risk=stop_risk,
            apply_penalties=penalise,
        )

        for journey in journeys[:2]:
            key = (
                journey.access_stop,
                journey.egress_stop,
                journey.departure_s,
                journey.arrival_s,
            )
            if key in seen:
                continue
            seen.add(key)

            itinerary = build_transit_itinerary(
                transit,
                journey,
                origin,
                destination,
                costs,
                times,
                is_night,
                destination_name,
                label,
                cameras,
                window_days,
            )
            if itinerary is not None:
                out.append(itinerary)

    out.sort(key=lambda i: i.arrival_s)
    return out
