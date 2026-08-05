"""GTFS loading and the timetable structure RAPTOR runs on.

RAPTOR does not operate on GTFS routes. It needs *patterns*: groups of trips
that visit exactly the same sequence of stops, so the algorithm can scan a
pattern once and know that trip ordering along it is consistent. WMATA's bus
routes have many variants sharing a route_id (short turns, branches), so
building patterns rather than trusting route_id matters for correctness — a
scan over a mixed route can otherwise "board" a trip that never reaches the
stop it was selected for.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# GTFS route_type -> the mode name we show in the UI.
ROUTE_TYPE_NAMES = {
    0: "tram",
    1: "metro",
    2: "rail",
    3: "bus",
    4: "ferry",
    5: "cable_tram",
    6: "gondola",
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}


@dataclass
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float
    parent: str = ""
    # Filled in by the safety pass: how exposed it is to wait here.
    risk: float = 0.0


@dataclass
class Pattern:
    """A distinct stop sequence, with all trips that follow it."""

    pattern_id: int
    route_id: str
    route_name: str
    mode: str
    headsign: str
    stops: list[int] = field(default_factory=list)  # stop indices
    # trips[t] = list of (arrival_s, departure_s) parallel to `stops`.
    trips: list[list[tuple[int, int]]] = field(default_factory=list)
    trip_ids: list[str] = field(default_factory=list)

    def departure(self, trip: int, stop_pos: int) -> int:
        return self.trips[trip][stop_pos][1]

    def arrival(self, trip: int, stop_pos: int) -> int:
        return self.trips[trip][stop_pos][0]


@dataclass
class TransitNetwork:
    stops: list[Stop]
    stop_index: dict[str, int]
    patterns: list[Pattern]
    # stop index -> [(pattern index, position within pattern)]
    stop_patterns: dict[int, list[tuple[int, int]]]
    # stop index -> [(stop index, walk seconds)] — short in-station links.
    transfers: dict[int, list[tuple[int, int]]]

    @property
    def n_stops(self) -> int:
        return len(self.stops)


def parse_time(value: str) -> int:
    """GTFS HH:MM:SS to seconds after midnight. Hours may exceed 24."""
    if not value:
        return -1
    parts = value.strip().split(":")
    if len(parts) != 3:
        return -1
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return -1


def _read_csv(source: zipfile.ZipFile | Path, name: str) -> list[dict]:
    """Read one GTFS table from either a zip or an unpacked directory."""
    if isinstance(source, zipfile.ZipFile):
        if name not in source.namelist():
            return []
        raw = source.read(name).decode("utf-8-sig")
    else:
        path = source / name
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(raw)))


def active_services(source, on: date) -> set[str]:
    """Service ids running on a given date, honouring calendar_dates overrides."""
    active: set[str] = set()
    weekday = [
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ][on.weekday()]
    stamp = on.strftime("%Y%m%d")

    for row in _read_csv(source, "calendar.txt"):
        if row.get(weekday) != "1":
            continue
        if row.get("start_date", "") <= stamp <= row.get("end_date", "99999999"):
            active.add(row["service_id"])

    for row in _read_csv(source, "calendar_dates.txt"):
        if row.get("date") != stamp:
            continue
        if row.get("exception_type") == "1":
            active.add(row["service_id"])
        elif row.get("exception_type") == "2":
            active.discard(row["service_id"])

    return active


def load_gtfs(
    path: Path, service_date: date | None = None, max_transfer_walk_m: float = 250.0
) -> TransitNetwork:
    """Build a :class:`TransitNetwork` from a GTFS zip or directory.

    Only trips running on ``service_date`` are loaded. Rebuilding for a
    different date is cheap relative to holding every service pattern in
    memory, and it keeps the RAPTOR scan tight.
    """
    source: zipfile.ZipFile | Path
    if path.is_dir():
        source = path
    else:
        source = zipfile.ZipFile(path)

    service_date = service_date or date.today()
    services = active_services(source, service_date)

    stops: list[Stop] = []
    stop_index: dict[str, int] = {}
    for row in _read_csv(source, "stops.txt"):
        # location_type 1 is a station node, 2+ are entrances/generic nodes.
        # Only boardable platforms (0 or blank) belong in the timetable.
        if row.get("location_type", "0") not in ("", "0"):
            continue
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        stop_index[row["stop_id"]] = len(stops)
        stops.append(
            Stop(
                stop_id=row["stop_id"],
                name=row.get("stop_name", "").strip(),
                lat=lat,
                lon=lon,
                parent=row.get("parent_station", "").strip(),
            )
        )

    routes: dict[str, dict] = {}
    for row in _read_csv(source, "routes.txt"):
        try:
            rtype = int(row.get("route_type", "3"))
        except ValueError:
            rtype = 3
        routes[row["route_id"]] = {
            "name": (
                row.get("route_short_name") or row.get("route_long_name") or ""
            ).strip(),
            "long_name": row.get("route_long_name", "").strip(),
            "mode": ROUTE_TYPE_NAMES.get(rtype, "transit"),
        }

    trips: dict[str, dict] = {}
    for row in _read_csv(source, "trips.txt"):
        if row.get("service_id") not in services:
            continue
        trips[row["trip_id"]] = {
            "route_id": row.get("route_id", ""),
            "headsign": row.get("trip_headsign", "").strip(),
        }

    # Group stop_times by trip.
    by_trip: dict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    for row in _read_csv(source, "stop_times.txt"):
        tid = row.get("trip_id")
        if tid not in trips:
            continue
        sid = row.get("stop_id")
        if sid not in stop_index:
            continue
        arr = parse_time(row.get("arrival_time", ""))
        dep = parse_time(row.get("departure_time", ""))
        if arr < 0 and dep < 0:
            continue  # interpolated stop with no times; skip rather than guess
        arr = arr if arr >= 0 else dep
        dep = dep if dep >= 0 else arr
        try:
            seq = int(row.get("stop_sequence", "0"))
        except ValueError:
            continue
        by_trip[tid].append((seq, arr, dep, sid))

    # Bucket trips into patterns keyed on their exact stop sequence.
    pattern_lookup: dict[tuple[int, ...], int] = {}
    patterns: list[Pattern] = []

    for tid, entries in by_trip.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: e[0])
        stop_seq = tuple(stop_index[e[3]] for e in entries)
        times = [(e[1], e[2]) for e in entries]

        key = stop_seq
        pid = pattern_lookup.get(key)
        if pid is None:
            meta = trips[tid]
            route = routes.get(meta["route_id"], {})
            pid = len(patterns)
            pattern_lookup[key] = pid
            patterns.append(
                Pattern(
                    pattern_id=pid,
                    route_id=meta["route_id"],
                    route_name=route.get("name", ""),
                    mode=route.get("mode", "transit"),
                    headsign=meta["headsign"],
                    stops=list(stop_seq),
                )
            )
        patterns[pid].trips.append(times)
        patterns[pid].trip_ids.append(tid)

    # RAPTOR requires trips within a pattern to be ordered by departure and
    # non-overtaking. Sorting by first departure gives the ordering; genuine
    # overtaking is rare enough in scheduled service to ignore.
    for p in patterns:
        order = sorted(range(len(p.trips)), key=lambda i: p.trips[i][0][1])
        p.trips = [p.trips[i] for i in order]
        p.trip_ids = [p.trip_ids[i] for i in order]

    stop_patterns: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for p in patterns:
        for pos, s in enumerate(p.stops):
            stop_patterns[s].append((p.pattern_id, pos))

    transfers = _build_transfers(source, stops, stop_index, max_transfer_walk_m)

    if isinstance(source, zipfile.ZipFile):
        source.close()

    return TransitNetwork(
        stops=stops,
        stop_index=stop_index,
        patterns=patterns,
        stop_patterns=dict(stop_patterns),
        transfers=transfers,
    )


def _build_transfers(
    source, stops: list[Stop], stop_index: dict[str, int], max_walk_m: float
) -> dict[int, list[tuple[int, int]]]:
    """In-station and short street transfers between stops.

    Uses the feed's own transfers.txt where present, and fills in geometric
    neighbours otherwise — WMATA's rail feed declares platform links but bus
    stops on opposite corners of an intersection usually have none.
    """
    from ..geo import to_local
    import numpy as np
    from scipy.spatial import cKDTree

    transfers: dict[int, set[tuple[int, int]]] = defaultdict(set)

    for row in _read_csv(source, "transfers.txt"):
        a = stop_index.get(row.get("from_stop_id", ""))
        b = stop_index.get(row.get("to_stop_id", ""))
        if a is None or b is None or a == b:
            continue
        if row.get("transfer_type") == "3":  # transfer not possible
            continue
        try:
            seconds = int(row.get("min_transfer_time") or 120)
        except ValueError:
            seconds = 120
        transfers[a].add((b, seconds))

    if stops:
        lat = np.array([s.lat for s in stops])
        lon = np.array([s.lon for s in stops])
        x, y = to_local(lat, lon)
        pts = np.column_stack([x, y])
        tree = cKDTree(pts)
        for a, b in tree.query_pairs(max_walk_m):
            d = float(np.hypot(*(pts[a] - pts[b])))
            seconds = int(d / 1.35) + 20  # walking plus a moment to orient
            transfers[a].add((b, seconds))
            transfers[b].add((a, seconds))

    return {k: sorted(v) for k, v in transfers.items()}
