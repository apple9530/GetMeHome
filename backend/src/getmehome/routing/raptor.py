"""RAPTOR over the WMATA timetable, with safety-penalised walking and waiting.

Plain RAPTOR minimises arrival time. We want something slightly different:
the itinerary that is best once you account for how exposed its walking legs
and platform waits are. So the algorithm carries two clocks.

* ``actual`` is real wall-clock time. Every feasibility test — can I catch
  this trip, does this transfer connect — uses this one, so the itineraries
  we return are always physically catchable.
* ``label`` is actual time plus accumulated risk penalties. All comparisons
  and pruning use this one, so the search prefers itineraries whose walking
  and waiting happen in safer places.

This is a generalised-cost approximation rather than a full multi-criteria
(McRAPTOR) search. It can in principle miss a Pareto-optimal journey that is
slower on the label clock but better on some other axis. In exchange it runs
in the same time as ordinary RAPTOR, which is what makes sub-second responses
possible. Running it twice — once with the penalties off and once with them
on — gives the fast option and the safe option, which is what the UI needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import TRANSIT, TransitConfig
from ..ingest.gtfs import TransitNetwork

INF = float("inf")


@dataclass
class TransitLeg:
    kind: str  # "transit" | "transfer"
    from_stop: int
    to_stop: int
    departure_s: int
    arrival_s: int
    pattern_id: int = -1
    trip_index: int = -1
    route_name: str = ""
    mode: str = ""
    headsign: str = ""
    # Intermediate stops, for drawing the leg on the map.
    intermediate: list[int] = field(default_factory=list)


@dataclass
class Journey:
    legs: list[TransitLeg]
    access_stop: int
    egress_stop: int
    departure_s: int  # boarding the first vehicle
    arrival_s: int  # alighting from the last
    n_transfers: int
    label_s: float  # penalised objective, for ranking


@dataclass
class AccessPoint:
    """Reaching a stop on foot from the origin (or the destination from one)."""

    stop: int
    seconds: float  # real walking time
    penalty_s: float = 0.0  # risk surcharge on that walk


def run_raptor(
    network: TransitNetwork,
    access: list[AccessPoint],
    egress: list[AccessPoint],
    departure_s: int,
    stop_risk: list[float] | None = None,
    apply_penalties: bool = True,
    cfg: TransitConfig = TRANSIT,
) -> list[Journey]:
    """Return one journey per round (i.e. per transfer count).

    ``stop_risk`` is a per-stop risk in [0, 1] used to price waiting time.
    Standing on an unlit platform for twelve minutes is real exposure, and an
    algorithm that only counts walking will happily route you into it.
    """
    n = network.n_stops
    max_rounds = cfg.max_transfers + 1
    risk = stop_risk or [0.0] * n

    # actual[k][stop], label[k][stop]
    actual = [[INF] * n for _ in range(max_rounds + 1)]
    label = [[INF] * n for _ in range(max_rounds + 1)]
    parent: list[dict[int, TransitLeg]] = [dict() for _ in range(max_rounds + 1)]
    best_label = [INF] * n

    egress_by_stop = {e.stop: e for e in egress}

    marked: set[int] = set()
    for a in access:
        if a.stop >= n:
            continue
        arrive = departure_s + a.seconds
        lab = arrive + (a.penalty_s if apply_penalties else 0.0)
        if lab < label[0][a.stop]:
            actual[0][a.stop] = arrive
            label[0][a.stop] = lab
            best_label[a.stop] = min(best_label[a.stop], lab)
            marked.add(a.stop)

    if not marked:
        return []

    journeys: list[Journey] = []
    horizon = departure_s + cfg.max_journey_duration_s

    for k in range(1, max_rounds + 1):
        actual[k] = actual[k - 1][:]
        label[k] = label[k - 1][:]
        parent[k] = dict(parent[k - 1])

        # --- collect the patterns worth scanning, and where to start ------
        queue: dict[int, int] = {}
        for s in marked:
            for pid, pos in network.stop_patterns.get(s, ()):
                cur = queue.get(pid)
                if cur is None or pos < cur:
                    queue[pid] = pos
        marked = set()

        # --- scan each pattern once --------------------------------------
        for pid, start_pos in queue.items():
            pattern = network.patterns[pid]
            trip = -1
            board_pos = -1
            board_penalty = 0.0

            for pos in range(start_pos, len(pattern.stops)):
                stop = pattern.stops[pos]

                if trip >= 0:
                    arrive = pattern.arrival(trip, pos)
                    if arrive > horizon:
                        break
                    lab = arrive + (board_penalty if apply_penalties else 0.0)
                    if lab < best_label[stop] and lab < label[k][stop]:
                        actual[k][stop] = arrive
                        label[k][stop] = lab
                        best_label[stop] = lab
                        parent[k][stop] = TransitLeg(
                            kind="transit",
                            from_stop=pattern.stops[board_pos],
                            to_stop=stop,
                            departure_s=pattern.departure(trip, board_pos),
                            arrival_s=arrive,
                            pattern_id=pid,
                            trip_index=trip,
                            route_name=pattern.route_name,
                            mode=pattern.mode,
                            headsign=pattern.headsign,
                            intermediate=pattern.stops[board_pos + 1 : pos],
                        )
                        marked.add(stop)

                # Can we board an earlier trip here than the one we are on?
                ready = actual[k - 1][stop]
                if ready == INF:
                    continue
                ready += cfg.board_penalty_s

                candidate = _earliest_trip(pattern, pos, ready, trip)
                if candidate is not None and candidate != trip:
                    trip = candidate
                    board_pos = pos
                    wait = max(0.0, pattern.departure(trip, pos) - ready)
                    carried = label[k - 1][stop] - actual[k - 1][stop]
                    board_penalty = (
                        carried
                        + wait * cfg.wait_risk_multiplier * risk[stop]
                    )

        # --- relax footpaths ---------------------------------------------
        for s in list(marked):
            base_actual = actual[k][s]
            base_label = label[k][s]
            for target, seconds in network.transfers.get(s, ()):
                arrive = base_actual + seconds
                lab = base_label + seconds * (
                    1.0 + (risk[target] if apply_penalties else 0.0)
                )
                if lab < best_label[target] and lab < label[k][target]:
                    actual[k][target] = arrive
                    label[k][target] = lab
                    best_label[target] = lab
                    parent[k][target] = TransitLeg(
                        kind="transfer",
                        from_stop=s,
                        to_stop=target,
                        departure_s=int(base_actual),
                        arrival_s=int(arrive),
                    )
                    marked.add(target)

        if not marked:
            break

        journey = _extract_best(
            network, actual, label, parent, k, egress_by_stop, apply_penalties
        )
        if journey is not None:
            journeys.append(journey)

    return _prune(journeys)


def _earliest_trip(
    pattern, pos: int, ready_at: float, current: int
) -> int | None:
    """Index of the earliest trip departing ``pos`` at or after ``ready_at``.

    Bounded above by ``current`` — a trip later than the one already boarded
    can never help, and skipping them keeps the scan linear.
    """
    hi = len(pattern.trips) if current < 0 else current
    lo = 0
    # Binary search: trips are sorted by departure and assumed non-overtaking.
    while lo < hi:
        mid = (lo + hi) // 2
        if pattern.departure(mid, pos) >= ready_at:
            hi = mid
        else:
            lo = mid + 1
    return lo if lo < len(pattern.trips) and (current < 0 or lo < current) else None


def _extract_best(
    network: TransitNetwork,
    actual: list[list[float]],
    label: list[list[float]],
    parent: list[dict[int, TransitLeg]],
    k: int,
    egress_by_stop: dict[int, AccessPoint],
    apply_penalties: bool,
) -> Journey | None:
    """Best journey using at most ``k`` vehicles, including the walk to the end."""
    best_stop, best_score = -1, INF
    for stop, point in egress_by_stop.items():
        if stop >= len(label[k]) or label[k][stop] == INF:
            continue
        score = label[k][stop] + point.seconds
        if apply_penalties:
            score += point.penalty_s
        if score < best_score:
            best_score, best_stop = score, stop

    if best_stop < 0:
        return None

    legs: list[TransitLeg] = []
    cur = best_stop
    guard = 0
    while cur in parent[k]:
        leg = parent[k][cur]
        legs.append(leg)
        cur = leg.from_stop
        guard += 1
        if guard > 200:
            break
    legs.reverse()

    if not legs:
        return None

    transit_legs = [leg for leg in legs if leg.kind == "transit"]
    if not transit_legs:
        return None

    return Journey(
        legs=legs,
        access_stop=legs[0].from_stop,
        egress_stop=best_stop,
        departure_s=transit_legs[0].departure_s,
        arrival_s=transit_legs[-1].arrival_s,
        n_transfers=max(0, len(transit_legs) - 1),
        label_s=best_score,
    )


def _prune(journeys: list[Journey]) -> list[Journey]:
    """Drop journeys that are worse than a simpler one on both axes.

    An extra transfer has to buy something. If a two-transfer journey does not
    arrive earlier than the one-transfer alternative, nobody wants it.
    """
    out: list[Journey] = []
    best_arrival = INF
    for j in sorted(journeys, key=lambda j: j.n_transfers):
        if j.arrival_s < best_arrival - 1:
            out.append(j)
            best_arrival = j.arrival_s
    return sorted(out, key=lambda j: j.label_s)
