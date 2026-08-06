"""Stop lists, departure boards and trip detail."""

from __future__ import annotations

from datetime import date

import pytest

from getmehome.ingest.gtfs import load_gtfs
from getmehome.ingest.wmata_live import LivePrediction, _rail_minutes, station_code
from getmehome.transit_board import (
    DAY_S,
    attach_live,
    departures,
    estimate_rail_position,
    resolve_stop,
    stops_in_bbox,
    trip_detail,
)

from .fixtures import grid_coords

SERVICE_DATE = date(2026, 6, 15)  # a Monday


def write_feed(tmp_path, *, with_stations: bool = False):
    """A small two-route feed: a bus line and a rail line.

    When ``with_stations`` is set the rail stops become two platforms under a
    parent station, which is how WMATA actually publishes rail and the case
    that puts two markers a few metres apart if it is not handled.
    """
    d = tmp_path / "gtfs"
    d.mkdir()

    rows = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station"]
    for c in range(0, 7, 2):
        lat, lon = grid_coords(0, c)
        rows.append(f"B{c},A St & {c + 1}th,{lat},{lon},0,")

    for r in range(2, 7, 2):
        lat, lon = grid_coords(r, 6)
        if with_stations:
            rows.append(f"STN_{r},{chr(65 + r)} Street,{lat},{lon},1,")
            rows.append(f"PF_{r}_A0{r},{chr(65 + r)} Street,{lat},{lon},0,STN_{r}")
        else:
            rows.append(f"PF_{r}_A0{r},{chr(65 + r)} Street,{lat},{lon},0,")
    (d / "stops.txt").write_text("\n".join(rows) + "\n")

    (d / "routes.txt").write_text(
        "route_id,route_short_name,route_long_name,route_type\n"
        "RB,70,Georgia Avenue Line,3\n"
        "RR,Red,Red Line,1\n"
    )
    (d / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WK,1,1,1,1,1,1,1,20260101,20261231\n"
    )

    trips = ["route_id,service_id,trip_id,trip_headsign"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]

    def add(tid: str, route: str, headsign: str, stops: list[str], base: int, gap: int):
        trips.append(f"{route},WK,{tid},{headsign}")
        for i, sid in enumerate(stops):
            s = base + i * gap
            clock = f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
            times.append(f"{tid},{clock},{clock},{sid},{i}")

    bus_stops = [f"B{c}" for c in range(0, 7, 2)]
    rail_stops = [f"PF_{r}_A0{r}" for r in range(2, 7, 2)]

    # Bus every 10 minutes from 08:00, plus one trip that runs past midnight.
    for t in range(6):
        add(f"B_{t}", "RB", "Downtown", bus_stops, 8 * 3600 + t * 600, 120)
    add("B_late", "RB", "Downtown", bus_stops, 25 * 3600, 120)  # 01:00 next day

    for t in range(4):
        add(f"R_{t}", "RR", "Glenmont", rail_stops, 8 * 3600 + 300 + t * 360, 90)

    (d / "trips.txt").write_text("\n".join(trips) + "\n")
    (d / "stop_times.txt").write_text("\n".join(times) + "\n")
    return d


@pytest.fixture(scope="module")
def network(tmp_path_factory):
    path = write_feed(tmp_path_factory.mktemp("plain"))
    return load_gtfs(path, service_date=SERVICE_DATE)


@pytest.fixture(scope="module")
def station_network(tmp_path_factory):
    path = write_feed(tmp_path_factory.mktemp("stations"), with_stations=True)
    return load_gtfs(path, service_date=SERVICE_DATE)


# ---------------------------------------------------------------------------
# Stops on the map
# ---------------------------------------------------------------------------


def test_stops_in_a_viewport_carry_their_routes(network):
    stops, truncated = stops_in_bbox(network, 38.88, -77.05, 38.92, -77.00)

    assert not truncated
    assert stops
    bus = next(s for s in stops if s.mode == "bus")
    assert "70" in bus.routes


def test_a_viewport_that_excludes_everything_returns_nothing(network):
    stops, _ = stops_in_bbox(network, 40.0, -80.0, 41.0, -79.0)
    assert stops == []


def test_platforms_collapse_onto_their_station(station_network):
    """WMATA rail carries platforms under a parent; one station is one marker."""
    stops, _ = stops_in_bbox(station_network, 38.88, -77.05, 38.95, -77.00)
    rail = [s for s in stops if s.is_rail]

    assert rail, "expected rail stops"
    for stop in rail:
        assert stop.id.startswith("STN_")
    # One marker per station, not one per platform.
    assert len({s.id for s in rail}) == len(rail)


def test_a_stop_with_no_service_is_not_drawn(network):
    """Drawing it would promise a timetable that does not exist."""
    stops, _ = stops_in_bbox(network, 38.88, -77.05, 38.95, -77.00)
    assert all(stop.routes for stop in stops)


def test_the_cap_prefers_rail_and_reports_itself(station_network):
    stops, truncated = stops_in_bbox(
        station_network, 38.88, -77.05, 38.95, -77.00, limit=2
    )
    assert truncated
    assert len(stops) == 2
    assert all(s.is_rail for s in stops)


def test_rail_only_excludes_buses(station_network):
    stops, _ = stops_in_bbox(
        station_network, 38.88, -77.05, 38.95, -77.00, rail_only=True
    )
    assert stops and all(s.is_rail for s in stops)


def test_a_station_id_resolves_to_its_platforms(station_network):
    assert len(resolve_stop(station_network, "STN_2")) == 1
    assert resolve_stop(station_network, "nonsense") == []


# ---------------------------------------------------------------------------
# Departure boards
# ---------------------------------------------------------------------------


def test_departures_are_in_order_and_after_the_requested_time(network):
    board = departures(network, "B0", after_s=8 * 3600)

    assert board
    assert all(d.departure_s >= 8 * 3600 for d in board)
    assert board == sorted(board, key=lambda d: d.departure_s)


def test_a_board_carries_what_the_client_needs_to_follow_the_trip(network):
    first = departures(network, "B0", after_s=8 * 3600)[0]

    assert first.route_name == "70"
    assert first.headsign == "Downtown"
    assert first.trip_id
    assert first.stops_remaining == 3


def test_the_final_stop_of_a_pattern_offers_no_departures(network):
    """Nobody boards at the end of the line."""
    assert departures(network, "B6", after_s=8 * 3600) == []


def test_a_board_looks_past_midnight(network):
    """GTFS runs past 24:00, and at 23:50 the 01:00 trip is still ahead.

    Comparing a clock time against the schedule without this leaves the last
    departures of the night invisible for the whole hour before midnight.
    """
    board = departures(network, "B0", after_s=23 * 3600 + 50 * 60)
    assert board, "the after-midnight trip should still be offered"
    assert board[0].trip_id == "B_late"


def test_a_board_just_after_midnight_still_finds_the_night_trips(network):
    board = departures(network, "B0", after_s=10 * 60)  # 00:10
    assert board
    # The 01:00 trip is the next thing to run, and it is reported in *today's*
    # clock — 01:00, fifty minutes away — not as yesterday's 25:00.
    assert board[0].trip_id == "B_late"
    assert board[0].departure_s == 3600


def test_the_horizon_bounds_the_board(network):
    board = departures(network, "B0", after_s=8 * 3600, horizon_s=15 * 60)
    assert board
    assert all(d.departure_s <= 8 * 3600 + 15 * 60 for d in board)


def test_a_board_is_capped(network):
    assert len(departures(network, "B0", after_s=8 * 3600, limit=2)) == 2


# ---------------------------------------------------------------------------
# Live predictions folded onto a schedule
# ---------------------------------------------------------------------------


def test_a_prediction_attaches_to_the_matching_scheduled_departure(network):
    now = 8 * 3600
    board = departures(network, "B0", after_s=now, limit=3)

    board = attach_live(
        board,
        [LivePrediction(route_name="70", headsign="Downtown", minutes=12, mode="bus")],
        now_s=now,
    )
    # Scheduled at 0, 10 and 20 minutes; 12 belongs to the 10-minute one.
    assert board[1].live_minutes == 12
    assert board[0].live_minutes is None


def test_a_prediction_for_another_route_is_ignored(network):
    now = 8 * 3600
    board = departures(network, "B0", after_s=now, limit=3)
    board = attach_live(
        board,
        [LivePrediction(route_name="90", headsign="Elsewhere", minutes=2, mode="bus")],
        now_s=now,
    )
    assert all(d.live_minutes is None for d in board)


def test_a_prediction_far_from_any_scheduled_time_is_dropped(network):
    """An unmatched prediction has no trip behind it, so it cannot be tapped."""
    now = 8 * 3600
    board = departures(network, "B0", after_s=now, limit=1)
    board = attach_live(
        board,
        [LivePrediction(route_name="70", headsign="Downtown", minutes=90, mode="bus")],
        now_s=now,
    )
    assert board[0].live_minutes is None
    assert len(board) == 1


def test_no_predictions_leaves_the_schedule_untouched(network):
    board = departures(network, "B0", after_s=8 * 3600, limit=3)
    assert attach_live(board, [], now_s=8 * 3600) == board


# ---------------------------------------------------------------------------
# Trip detail
# ---------------------------------------------------------------------------


def test_a_trip_lists_every_call_in_order(network):
    detail = trip_detail(network, _pattern_of(network, "B_0"), "B_0")

    assert detail is not None
    assert len(detail.stops) == 4
    assert detail.route_name == "70"
    arrivals = [s.arrival_s for s in detail.stops]
    assert arrivals == sorted(arrivals)


def test_a_late_vehicle_pushes_every_remaining_time_back(network):
    pattern = _pattern_of(network, "B_0")
    on_time = trip_detail(network, pattern, "B_0")
    late = trip_detail(network, pattern, "B_0", deviation_s=300.0)

    assert on_time is not None and late is not None
    for a, b in zip(on_time.stops, late.stops, strict=True):
        assert b.arrival_s == a.arrival_s + 300


def test_calls_already_made_are_flagged_from_position_not_the_clock(network):
    """A bus twenty minutes late has not made the stops its schedule claims."""
    pattern = _pattern_of(network, "B_0")
    third = grid_coords(0, 4)
    detail = trip_detail(
        network, pattern, "B_0", vehicle_lat=third[0], vehicle_lon=third[1]
    )

    assert detail is not None
    assert [s.passed for s in detail.stops] == [True, True, False, False]


def test_an_unknown_trip_is_not_invented(network):
    assert trip_detail(network, 0, "no-such-trip") is None
    assert trip_detail(network, 999, "B_0") is None


# ---------------------------------------------------------------------------
# Estimated train position
# ---------------------------------------------------------------------------


def test_a_train_at_the_platform_sits_on_the_station(network):
    detail = trip_detail(network, _pattern_of(network, "R_0"), "R_0")
    assert detail is not None

    position = estimate_rail_position(detail, 1, minutes_away=0.0)
    assert position is not None
    assert position == pytest.approx((detail.stops[1].lat, detail.stops[1].lon))


def test_a_train_mid_leg_sits_between_the_two_stations(network):
    detail = trip_detail(network, _pattern_of(network, "R_0"), "R_0")
    assert detail is not None

    # The leg takes 90 seconds, so 45 seconds out is halfway.
    position = estimate_rail_position(detail, 1, minutes_away=0.75)
    assert position is not None
    lat = (detail.stops[0].lat + detail.stops[1].lat) / 2
    assert position[0] == pytest.approx(lat, abs=1e-6)


def test_no_estimate_before_the_first_station(network):
    """There is no previous station to interpolate from."""
    detail = trip_detail(network, _pattern_of(network, "R_0"), "R_0")
    assert detail is not None
    assert estimate_rail_position(detail, 0, minutes_away=1.0) is None


# ---------------------------------------------------------------------------
# WMATA response parsing
# ---------------------------------------------------------------------------


def test_rail_arrival_words_become_minutes():
    assert _rail_minutes("ARR") == 0
    assert _rail_minutes("BRD") == 0
    assert _rail_minutes("7") == 7
    # No number and no arrival: not a prediction at all.
    assert _rail_minutes("---") is None
    assert _rail_minutes("DLY") is None
    assert _rail_minutes(None) is None


def test_a_station_code_is_pulled_out_of_a_rail_stop_id():
    assert station_code("STN_A01") == "A01"
    assert station_code("PF_2_A02") == "A02"
    # A bus stop has no code, and inventing one would produce a wrong
    # prediction rather than no prediction.
    assert station_code("1001234") == ""
    assert station_code("B0") == ""


def test_a_day_is_a_day():
    assert DAY_S == 86_400


def _pattern_of(network, trip_id: str) -> int:
    for pattern in network.patterns:
        if trip_id in pattern.trip_ids:
            return pattern.pattern_id
    raise AssertionError(f"no pattern carries {trip_id}")
