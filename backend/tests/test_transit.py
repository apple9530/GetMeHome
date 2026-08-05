"""Tests for GTFS loading, RAPTOR, and multimodal stitching."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from getmehome.ingest.gtfs import load_gtfs, parse_time
from getmehome.routing.astar import GraphIndex
from getmehome.routing.multimodal import TransitIndex, plan
from getmehome.routing.raptor import AccessPoint, run_raptor

from .fixtures import build_grid_graph, grid_coords

SERVICE_DATE = date(2026, 6, 15)  # a Monday


def write_gtfs(tmp_path):
    """A two-route feed over the fixture grid.

    Route X runs west-to-east along the bottom row; route Y runs
    south-to-north up the last column. Reaching the far corner therefore needs
    one transfer, which is what makes this worth testing.
    """
    d = tmp_path / "gtfs"
    d.mkdir()

    stops = []
    for c in range(0, 7, 2):  # bottom row, every other node
        lat, lon = grid_coords(0, c)
        stops.append((f"X{c}", f"A St & {c + 1}th", lat, lon))
    for r in range(2, 7, 2):  # last column
        lat, lon = grid_coords(r, 6)
        stops.append((f"Y{r}", f"7th & {chr(65 + r)} St", lat, lon))

    (d / "stops.txt").write_text(
        "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
        + "".join(f"{s},{n},{la},{lo},0\n" for s, n, la, lo in stops)
    )
    (d / "routes.txt").write_text(
        "route_id,route_short_name,route_long_name,route_type\n"
        "RX,X,A Street Line,3\n"
        "RY,Y,7th Street Line,1\n"
    )
    (d / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\n"
    )

    trips = ["route_id,service_id,trip_id,trip_headsign"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]

    # Route X: departures every 10 minutes from 20:00, 2 min between stops.
    for t in range(6):
        tid = f"X_{t}"
        trips.append(f"RX,WK,{tid},Eastbound")
        base = 20 * 3600 + t * 600
        for i, c in enumerate(range(0, 7, 2)):
            s = base + i * 120
            times.append(
                f"{tid},{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},"
                f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},X{c},{i}"
            )

    # Route Y: every 6 minutes from 20:05.
    for t in range(8):
        tid = f"Y_{t}"
        trips.append(f"RY,WK,{tid},Northbound")
        base = 20 * 3600 + 300 + t * 360
        for i, r in enumerate(range(2, 7, 2)):
            s = base + i * 90
            times.append(
                f"{tid},{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},"
                f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},Y{r},{i}"
            )

    (d / "trips.txt").write_text("\n".join(trips) + "\n")
    (d / "stop_times.txt").write_text("\n".join(times) + "\n")
    return d


@pytest.fixture(scope="module")
def feed(tmp_path_factory):
    return write_gtfs(tmp_path_factory.mktemp("feed"))


@pytest.fixture(scope="module")
def network(feed):
    return load_gtfs(feed, service_date=SERVICE_DATE)


def test_parse_time_handles_past_midnight():
    assert parse_time("08:30:00") == 8 * 3600 + 1800
    assert parse_time("25:15:30") == 25 * 3600 + 15 * 60 + 30
    assert parse_time("") == -1
    assert parse_time("garbage") == -1


def test_network_loads(network):
    assert network.n_stops == 7  # 4 on route X + 3 on route Y
    assert len(network.patterns) == 2
    modes = {p.mode for p in network.patterns}
    assert modes == {"bus", "metro"}


def test_trips_sorted_by_departure(network):
    for p in network.patterns:
        firsts = [t[0][1] for t in p.trips]
        assert firsts == sorted(firsts)


def test_service_date_filtering(feed):
    """A Sunday has no service in this feed, so nothing should load."""
    sunday = load_gtfs(feed, service_date=date(2026, 6, 14))
    assert all(len(p.trips) == 0 for p in sunday.patterns) or not sunday.patterns


def test_raptor_finds_direct_ride(network):
    """Boarding at the first stop of route X and riding east."""
    x0 = network.stop_index["X0"]
    x6 = network.stop_index["X6"]

    journeys = run_raptor(
        network,
        access=[AccessPoint(stop=x0, seconds=60)],
        egress=[AccessPoint(stop=x6, seconds=60)],
        departure_s=20 * 3600,
        apply_penalties=False,
    )

    assert journeys
    best = journeys[0]
    assert best.n_transfers == 0
    # First trip departs 20:00; we arrive at the stop at 20:01, so we take the
    # 20:10 departure and ride 3 hops of 2 minutes.
    assert best.departure_s == 20 * 3600 + 600
    assert best.arrival_s == 20 * 3600 + 600 + 360


def test_raptor_finds_transfer(network):
    """Corner to corner needs route X then route Y."""
    x0 = network.stop_index["X0"]
    y6 = network.stop_index["Y6"]

    journeys = run_raptor(
        network,
        access=[AccessPoint(stop=x0, seconds=30)],
        egress=[AccessPoint(stop=y6, seconds=30)],
        departure_s=20 * 3600,
        apply_penalties=False,
    )

    assert journeys, "should find a transferring journey"
    best = min(journeys, key=lambda j: j.arrival_s)
    assert best.n_transfers >= 1
    modes = [l.mode for l in best.legs if l.kind == "transit"]
    assert "bus" in modes and "metro" in modes


def test_raptor_respects_departure_time(network):
    """A trip that has already left cannot be boarded."""
    x0 = network.stop_index["X0"]
    x6 = network.stop_index["X6"]

    # Route X runs 6 trips at 10-minute headways from 20:00, so the last one
    # leaves at 20:50. Asking at 21:00 is genuinely past the end of service.
    late = run_raptor(
        network,
        access=[AccessPoint(stop=x0, seconds=0)],
        egress=[AccessPoint(stop=x6, seconds=0)],
        departure_s=21 * 3600,
        apply_penalties=False,
    )
    assert late == []

    # And the last departure is still catchable one minute before it leaves.
    just_in_time = run_raptor(
        network,
        access=[AccessPoint(stop=x0, seconds=0)],
        egress=[AccessPoint(stop=x6, seconds=0)],
        departure_s=20 * 3600 + 49 * 60,
        apply_penalties=False,
    )
    assert just_in_time
    assert just_in_time[0].departure_s == 20 * 3600 + 50 * 60


def test_raptor_no_access_returns_nothing(network):
    assert run_raptor(network, [], [], 20 * 3600) == []


def test_wait_penalty_changes_ranking(network):
    """A risky stop should be penalised for the time spent waiting there."""
    x0 = network.stop_index["X0"]
    x6 = network.stop_index["X6"]
    risk = [0.0] * network.n_stops

    plain = run_raptor(
        network,
        [AccessPoint(stop=x0, seconds=30)],
        [AccessPoint(stop=x6, seconds=30)],
        20 * 3600,
        stop_risk=risk,
        apply_penalties=True,
    )

    risk[x0] = 1.0
    risky = run_raptor(
        network,
        [AccessPoint(stop=x0, seconds=30)],
        [AccessPoint(stop=x6, seconds=30)],
        20 * 3600,
        stop_risk=risk,
        apply_penalties=True,
    )

    assert plain and risky
    # Same physical journey, but the label reflects the waiting exposure.
    assert risky[0].arrival_s == plain[0].arrival_s
    assert risky[0].label_s > plain[0].label_s


def test_multimodal_plan_produces_itineraries(network):
    from getmehome.safety.scoring import apply_scores

    graph = build_grid_graph(7, 7)
    apply_scores(graph, lights=[], incidents=[], cameras=[])
    index = GraphIndex(graph)
    transit = TransitIndex(network, index)

    itineraries = plan(
        index,
        index.snap(*grid_coords(0, 0)),
        index.snap(*grid_coords(6, 6)),
        when=datetime(2026, 6, 15, 20, 0),
        is_night=True,
        modes={"walk", "transit"},
        transit=transit,
        destination_name="the corner",
    )

    assert itineraries
    kinds = {i.kind for i in itineraries}
    assert "walk" in kinds

    for it in itineraries:
        assert it.duration_s > 0
        assert 0 <= it.safety.overall <= 100
        assert it.legs
        # Walking legs must carry real geometry and instructions.
        for leg in it.legs:
            if leg.mode == "walk":
                assert len(leg.coords) >= 2
                assert leg.steps
                assert leg.steps[-1].maneuver == "arrive"


def test_transit_itinerary_has_ordered_legs(network):
    from getmehome.safety.scoring import apply_scores

    graph = build_grid_graph(7, 7)
    apply_scores(graph, lights=[], incidents=[], cameras=[])
    index = GraphIndex(graph)
    transit = TransitIndex(network, index)

    itineraries = plan(
        index,
        index.snap(*grid_coords(0, 0)),
        index.snap(*grid_coords(6, 6)),
        when=datetime(2026, 6, 15, 20, 0),
        is_night=True,
        modes={"transit"},
        transit=transit,
    )

    for it in itineraries:
        assert it.kind == "transit"
        # A negligible walk to a stop right outside the door is dropped rather
        # than shown as "walk 0 m", so the first leg is a walk only when there
        # is actually something to walk.
        for leg in it.legs:
            if leg.mode == "walk":
                assert leg.distance_m >= 8.0
                assert len(leg.coords) >= 2
        # Transit legs must be time-ordered.
        rides = [l for l in it.legs if l.mode not in ("walk", "transfer")]
        for a, b in zip(rides, rides[1:]):
            assert a.arrival_s <= b.departure_s
        assert it.arrival_s > it.departure_s
