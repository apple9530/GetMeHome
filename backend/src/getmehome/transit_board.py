"""Stop lists, departure boards and trip detail, over the loaded timetable.

The router already holds a full GTFS timetable in memory to plan journeys with.
Everything a departure board needs is in there — it just has to be asked a
different question: not "how do I get from A to B" but "what leaves from here,
and where does it go".

Three things this has to get right:

* **A station is not a platform.** WMATA rail carries two boardable platforms
  per station, and drawing both puts a pair of markers a few metres apart on
  the map with half the departures each. Platforms are collapsed onto their
  parent station, and a board for a station unions its platforms.
* **The map cannot draw eleven thousand bus stops.** Requests are capped, and
  when the cap bites the stops that survive are the ones most likely to be
  wanted: rail first, then whatever is served by the most routes.
* **Midnight.** GTFS times run past 24:00 for trips that begin before midnight
  and end after it, so "the next departure after 23:50" has to look into the
  following day's early hours as well as tonight's.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .ingest.gtfs import Pattern, TransitNetwork

# Ceiling on stops per map request. Filled markers are the binding cost, the
# same constraint that sizes the crime grid.
MAX_STOPS = 400

# How far past the requested time a board looks before giving up. Long enough
# to be useful on an hourly night route, short enough not to list tomorrow.
DEFAULT_HORIZON_S = 3 * 3600

DAY_S = 86_400

# How far a live prediction may sit from a scheduled departure and still be
# taken for the same vehicle. Wider than typical lateness, narrower than a
# frequent route's headway — beyond that the safer reading is that the
# prediction belongs to the next trip along.
MATCH_TOLERANCE_MIN = 15.0

# Modes drawn as rail rather than bus, and preferred when the cap bites.
RAIL_MODES = frozenset({"metro", "rail", "tram", "monorail", "funicular"})


@dataclass
class StopSummary:
    """One marker on the map."""

    # The parent station id where there is one, so a station is a single
    # marker rather than one per platform.
    id: str
    name: str
    lat: float
    lon: float
    # "metro" | "bus" | ... — the primary mode, for the icon.
    mode: str
    # Every route calling here, deduplicated and ordered.
    routes: list[str] = field(default_factory=list)
    # Platform stop ids collapsed into this marker.
    platform_ids: list[str] = field(default_factory=list)

    @property
    def is_rail(self) -> bool:
        return self.mode in RAIL_MODES


@dataclass
class ScheduledDeparture:
    """One row of a departure board."""

    route_name: str
    headsign: str
    mode: str
    # Seconds since midnight on the service day, so it can exceed 86400.
    departure_s: int
    pattern_id: int
    trip_id: str
    stop_position: int
    # Remaining stops after this one, for the "and then where" line.
    stops_remaining: int
    # Filled in from real-time data where there is any.
    live_minutes: int | None = None
    vehicle_id: str = ""

    @property
    def is_live(self) -> bool:
        return self.live_minutes is not None


@dataclass
class TripStop:
    """One call in a trip's full itinerary."""

    stop_id: str
    name: str
    lat: float
    lon: float
    arrival_s: int
    departure_s: int
    # True once the vehicle is past it, when a live position is known.
    passed: bool = False


@dataclass
class TripDetail:
    """A single vehicle's whole journey."""

    pattern_id: int
    trip_id: str
    route_name: str
    headsign: str
    mode: str
    stops: list[TripStop]
    # Seconds behind schedule; negative is early. Zero when unknown.
    deviation_s: float = 0.0
    # Present only when a position is known or could be estimated.
    vehicle_lat: float | None = None
    vehicle_lon: float | None = None
    vehicle_estimated: bool = False


# ----------------------------------------------------------------------
# Stops
# ----------------------------------------------------------------------


def stops_in_bbox(
    network: TransitNetwork,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    limit: int = MAX_STOPS,
    rail_only: bool = False,
) -> tuple[list[StopSummary], bool]:
    """Stops to draw for a viewport, and whether the list was truncated.

    Truncation is reported rather than hidden so the client can say "zoom in
    for the rest" instead of quietly showing an arbitrary subset.
    """
    routes_at, mode_at = _routes_by_stop(network)

    # Group platforms onto their parent station. A parent that is not itself
    # boardable never appears in `stops`, so its coordinates are the mean of
    # its platforms — which for a Metro station is the station.
    groups: dict[str, list[int]] = {}
    for i, stop in enumerate(network.stops):
        if not (min_lat <= stop.lat <= max_lat and min_lon <= stop.lon <= max_lon):
            continue
        groups.setdefault(stop.parent or stop.stop_id, []).append(i)

    summaries: list[StopSummary] = []
    for key, members in groups.items():
        modes = {mode_at.get(i, "bus") for i in members}
        mode = next((m for m in modes if m in RAIL_MODES), "")
        if not mode:
            mode = next(iter(sorted(modes)), "bus")
        if rail_only and mode not in RAIL_MODES:
            continue

        routes: list[str] = []
        for i in members:
            for name in routes_at.get(i, ()):
                if name and name not in routes:
                    routes.append(name)
        if not routes:
            # No pattern calls here on the loaded service day. Drawing it
            # would promise a timetable that does not exist.
            continue

        first = network.stops[members[0]]
        summaries.append(
            StopSummary(
                id=key,
                name=first.name,
                lat=sum(network.stops[i].lat for i in members) / len(members),
                lon=sum(network.stops[i].lon for i in members) / len(members),
                mode=mode,
                routes=sorted(routes, key=_route_sort_key),
                platform_ids=[network.stops[i].stop_id for i in members],
            )
        )

    truncated = len(summaries) > limit
    if truncated:
        # Rail first, then the busiest interchanges. Someone zoomed out far
        # enough to trip the cap is looking for the network's shape, not for
        # one particular bus stop.
        summaries.sort(key=lambda s: (0 if s.is_rail else 1, -len(s.routes), s.name))
        summaries = summaries[:limit]

    summaries.sort(key=lambda s: s.name)
    return summaries, truncated


def _routes_by_stop(
    network: TransitNetwork,
) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Route names and primary mode per stop index."""
    routes: dict[int, list[str]] = {}
    modes: dict[int, str] = {}
    for stop_idx, calls in network.stop_patterns.items():
        names: list[str] = []
        mode = ""
        for pattern_id, _ in calls:
            pattern = network.patterns[pattern_id]
            if pattern.route_name and pattern.route_name not in names:
                names.append(pattern.route_name)
            if not mode or (pattern.mode in RAIL_MODES and mode not in RAIL_MODES):
                mode = pattern.mode
        routes[stop_idx] = names
        modes[stop_idx] = mode or "bus"
    return routes, modes


def _route_sort_key(name: str) -> tuple:
    """Order route names the way a rider reads them: 30N before 30S, 70 before 79."""
    digits = "".join(c for c in name if c.isdigit())
    return (0 if digits else 1, int(digits) if digits else 0, name)


def resolve_stop(network: TransitNetwork, stop_id: str) -> list[int]:
    """Stop indices for an id that may name a station or a single platform."""
    direct = network.stop_index.get(stop_id)
    children = [
        i for i, stop in enumerate(network.stops) if stop.parent == stop_id
    ]
    if direct is not None and direct not in children:
        children.append(direct)
    return children


# ----------------------------------------------------------------------
# Departure boards
# ----------------------------------------------------------------------


def departures(
    network: TransitNetwork,
    stop_id: str,
    after_s: int,
    limit: int = 20,
    horizon_s: int = DEFAULT_HORIZON_S,
) -> list[ScheduledDeparture]:
    """Scheduled departures from a stop, soonest first."""
    indices = resolve_stop(network, stop_id)
    if not indices:
        return []

    out: list[ScheduledDeparture] = []
    for stop_idx in indices:
        for pattern_id, position in network.stop_patterns.get(stop_idx, ()):
            pattern = network.patterns[pattern_id]
            # The last stop of a pattern is an arrival, not a departure —
            # nobody boards there.
            if position >= len(pattern.stops) - 1:
                continue
            out.extend(
                _pattern_departures(
                    pattern, position, after_s, horizon_s, limit
                )
            )

    out.sort(key=lambda d: d.departure_s)
    return out[:limit]


def _pattern_departures(
    pattern: Pattern,
    position: int,
    after_s: int,
    horizon_s: int,
    limit: int,
) -> list[ScheduledDeparture]:
    """Departures of one pattern from one stop position.

    Trips within a pattern are ordered by departure, so the first candidate is
    found by bisection rather than by scanning a day of service.
    """
    times = [trip[position][1] for trip in pattern.trips]
    if not times:
        return []

    out: list[ScheduledDeparture] = []
    remaining = len(pattern.stops) - position - 1

    # Two passes over the same trip list.
    #
    # A GTFS service day runs past 24:00, so a trip that leaves at 25:10 is
    # yesterday's trip still out on the road. The first pass takes today's
    # service day as it is, which at 23:50 already includes that 25:10
    # departure. The second re-reads the same late trips as belonging to
    # yesterday — at 00:10 the 25:10 departure is 01:10 this morning, fifty
    # minutes away, so it maps in by subtracting a day rather than adding one.
    for offset in (0, -DAY_S):
        start = bisect.bisect_left(times, after_s - offset)
        for i in range(start, len(times)):
            departure = times[i] + offset
            if departure > after_s + horizon_s:
                break
            out.append(
                ScheduledDeparture(
                    route_name=pattern.route_name,
                    headsign=pattern.headsign,
                    mode=pattern.mode,
                    departure_s=departure,
                    pattern_id=pattern.pattern_id,
                    trip_id=pattern.trip_ids[i],
                    stop_position=position,
                    stops_remaining=remaining,
                )
            )
            if len(out) >= limit:
                return out
    return out


def attach_live(
    scheduled: list[ScheduledDeparture],
    predictions: list,
    now_s: int,
) -> list[ScheduledDeparture]:
    """Fold real-time predictions into a scheduled board.

    Matched on route and rough time rather than on trip id, because WMATA's
    prediction trip ids do not reliably correspond to the ones in the static
    feed. A prediction close in time to a scheduled departure on the same
    route is the same vehicle often enough to be worth showing; matching
    wrongly costs a minute or two of displayed accuracy, while not matching at
    all costs the whole feature.

    Unmatched predictions are dropped rather than appended. A prediction with
    no scheduled counterpart has no trip behind it, so tapping it could not
    show a route — and a row that does nothing when tapped is worse than a
    row that is not there.
    """
    if not predictions:
        return scheduled

    # Prediction-first, not departure-first. Walking the departures and giving
    # each its nearest prediction lets the 08:00 bus claim a prediction of
    # twelve minutes that obviously belongs to the 08:10 one, because 08:00 is
    # considered first and twelve minutes is inside the tolerance. Matching
    # each prediction to its own nearest departure has no such bias.
    taken: set[int] = set()
    for prediction in predictions:
        best_i, best_gap = None, MATCH_TOLERANCE_MIN
        for i, departure in enumerate(scheduled):
            if i in taken:
                continue
            if _normalise_route(prediction.route_name) != _normalise_route(
                departure.route_name
            ):
                continue
            gap = abs(prediction.minutes - (departure.departure_s - now_s) / 60.0)
            if gap < best_gap:
                best_i, best_gap = i, gap
        if best_i is not None:
            taken.add(best_i)
            scheduled[best_i].live_minutes = prediction.minutes
            scheduled[best_i].vehicle_id = prediction.vehicle_id

    return scheduled


def _normalise_route(name: str) -> str:
    return name.strip().upper().replace(" ", "").replace("-", "")


# ----------------------------------------------------------------------
# Trip detail
# ----------------------------------------------------------------------


def trip_detail(
    network: TransitNetwork,
    pattern_id: int,
    trip_id: str,
    deviation_s: float = 0.0,
    vehicle_lat: float | None = None,
    vehicle_lon: float | None = None,
) -> TripDetail | None:
    """A trip's whole itinerary, with every call and its time."""
    if not 0 <= pattern_id < len(network.patterns):
        return None
    pattern = network.patterns[pattern_id]
    try:
        trip = pattern.trip_ids.index(trip_id)
    except ValueError:
        return None

    times = pattern.trips[trip]
    stops = [
        TripStop(
            stop_id=network.stops[stop_idx].stop_id,
            name=network.stops[stop_idx].name,
            lat=network.stops[stop_idx].lat,
            lon=network.stops[stop_idx].lon,
            arrival_s=int(times[i][0] + deviation_s),
            departure_s=int(times[i][1] + deviation_s),
        )
        for i, stop_idx in enumerate(pattern.stops)
    ]

    detail = TripDetail(
        pattern_id=pattern_id,
        trip_id=trip_id,
        route_name=pattern.route_name,
        headsign=pattern.headsign,
        mode=pattern.mode,
        stops=stops,
        deviation_s=deviation_s,
        vehicle_lat=vehicle_lat,
        vehicle_lon=vehicle_lon,
    )
    if vehicle_lat is not None and vehicle_lon is not None:
        _mark_passed(detail, vehicle_lat, vehicle_lon)
    return detail


def _mark_passed(detail: TripDetail, lat: float, lon: float) -> None:
    """Flag the calls the vehicle has already made.

    Judged on position along the route rather than on the clock, because a bus
    running twenty minutes late has not made the stops its schedule says it
    has — which is exactly the situation the live view exists for.
    """
    if not detail.stops:
        return
    nearest = min(
        range(len(detail.stops)),
        key=lambda i: _squared_distance(detail.stops[i], lat, lon),
    )
    for i, stop in enumerate(detail.stops):
        stop.passed = i < nearest


def _squared_distance(stop: TripStop, lat: float, lon: float) -> float:
    # Flat approximation: over one bus route the curvature is irrelevant and
    # this runs per stop per request.
    dlat = stop.lat - lat
    dlon = (stop.lon - lon) * math.cos(math.radians(lat))
    return dlat * dlat + dlon * dlon


def estimate_rail_position(
    detail: TripDetail, next_stop_index: int, minutes_away: float
) -> tuple[float, float] | None:
    """Where a train probably is, from its next station and how far off it is.

    WMATA publishes track-circuit ids for trains rather than coordinates, and
    resolving those needs a separate feed and a lot of matching. Interpolating
    between the last station and the next one is accurate to a few hundred
    metres in the middle of a run and exact at a platform, which is enough for
    "is my train nearly here". Everything that surfaces this labels it as an
    estimate.
    """
    stops = detail.stops
    if not 0 < next_stop_index < len(stops):
        return None

    previous = stops[next_stop_index - 1]
    following = stops[next_stop_index]
    leg_s = max(1.0, following.arrival_s - previous.departure_s)
    # Fraction of the leg still to run, clamped: a prediction longer than the
    # leg itself means the train has not left the previous station.
    remaining = min(1.0, max(0.0, minutes_away * 60.0 / leg_s))
    travelled = 1.0 - remaining

    return (
        previous.lat + (following.lat - previous.lat) * travelled,
        previous.lon + (following.lon - previous.lon) * travelled,
    )
