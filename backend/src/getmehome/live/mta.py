"""MTA real-time, via GTFS-Realtime.

Structurally different from the WMATA client next door, and worth saying how,
because the difference is an advantage rather than an inconvenience:

* **Predictions join exactly.** Every GTFS-Realtime entity carries the static
  feed's own ``trip_id``. WMATA's real-time trip ids do not reliably match its
  static feed, so predictions there are matched on route and rough time — a
  heuristic that occasionally attaches a prediction to the wrong departure.
  Here the join is exact and there is nothing to guess.
* **One request covers a whole feed.** A GTFS-RT message carries every trip on
  its lines, so a departure board costs one cached fetch rather than one call
  per stop. That matters at New York's scale: a busy interchange serves a dozen
  routes and WMATA's shape would be a dozen calls.
* **Subway feeds are split by line group.** The MTA publishes seven of them,
  and there is no single "all trains" endpoint, so a station lookup fetches the
  feeds covering its lines rather than all seven.

Bus positions come from the vehicle-positions feed and are real coordinates.
Subway positions are *not* published as coordinates in GTFS-RT either — the
trip update gives predicted arrival times per stop — so a train's position is
estimated between stations exactly as it is for WMATA, and labelled the same
way.

**This code has never run against the live feeds.** ``gtfs.org``'s bindings and
the MTA's endpoints are both stable and documented, but nothing here has seen a
real payload. It fails soft in every direction: a missing dependency, a moved
endpoint or an unparseable message all degrade to scheduled times rather than
to an error page.
"""

from __future__ import annotations

import logging
import time

import httpx

from ..config import MTA_API_KEY
from .base import LivePrediction, LiveVehicle

log = logging.getLogger("getmehome.mta")

# Subway trip updates, by line group. The MTA splits these and publishes no
# combined feed, so a lookup fetches only the groups a station's lines belong
# to.
SUBWAY_FEEDS: dict[str, str] = {
    "ACE": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace",
    "BDFM": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm",
    "G": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-g",
    "JZ": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-jz",
    "NQRW": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw",
    "L": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-l",
    "1234567": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs",
}

# Which feed carries each route. Keyed on the route id the static feed uses.
ROUTE_TO_FEED: dict[str, str] = {
    **{r: "ACE" for r in ("A", "C", "E", "H", "FS")},
    **{r: "BDFM" for r in ("B", "D", "F", "M")},
    "G": "G",
    **{r: "JZ" for r in ("J", "Z")},
    **{r: "NQRW" for r in ("N", "Q", "R", "W")},
    "L": "L",
    **{r: "1234567" for r in ("1", "2", "3", "4", "5", "6", "7", "GS", "SI")},
}

BUS_TRIP_UPDATES = "https://gtfsrt.prod.obanyc.com/tripUpdates"
BUS_VEHICLE_POSITIONS = "https://gtfsrt.prod.obanyc.com/vehiclePositions"

# GTFS-RT feeds refresh every 30 seconds or so upstream. Asking more often
# spends bandwidth to parse an identical message.
CACHE_TTL_S = 25.0

_TIMEOUT_S = 10.0


class MtaLive:
    """Cached GTFS-Realtime client for the MTA."""

    def __init__(self, api_key: str = MTA_API_KEY, timeout_s: float = _TIMEOUT_S):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[float, object]] = {}
        self._failures = 0
        self._bindings_missing = False

    @property
    def enabled(self) -> bool:
        return not self._bindings_missing

    @property
    def disabled_reason(self) -> str:
        if self._bindings_missing:
            return (
                "Live arrivals need the gtfs-realtime-bindings package; "
                "showing the timetable."
            )
        return "No live arrivals right now; showing the timetable."

    # ------------------------------------------------------------------
    # Fetch and parse
    # ------------------------------------------------------------------

    def _feed(self, url: str):
        """Fetch and parse one GTFS-Realtime feed, or return None.

        The bindings are an optional dependency: a Washington-only deployment
        has no reason to install protobuf. Their absence disables live data
        rather than breaking the import, which is why this is caught here and
        not at module level.
        """
        if self._bindings_missing:
            return None

        hit = self._cache.get(url)
        now = time.monotonic()
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]

        try:
            from google.transit import gtfs_realtime_pb2  # noqa: PLC0415
        except ImportError:
            self._bindings_missing = True
            log.warning(
                "gtfs-realtime-bindings is not installed; MTA live data is off. "
                "pip install gtfs-realtime-bindings"
            )
            return None

        headers = {"x-api-key": self.api_key} if self.api_key else {}
        try:
            response = httpx.get(url, headers=headers, timeout=self.timeout_s)
            response.raise_for_status()
            message = gtfs_realtime_pb2.FeedMessage()
            message.ParseFromString(response.content)
        except Exception as exc:  # noqa: BLE001 — protobuf raises broadly
            self._failures += 1
            level = logging.WARNING if self._failures == 1 else logging.DEBUG
            log.log(level, "MTA feed unavailable (%s): %s", url, exc)
            # A stale message beats none: a thirty-second-old prediction is
            # still a prediction.
            return hit[1] if hit is not None else None

        self._failures = 0
        self._cache[url] = (now, message)
        return message

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def _predictions_from(self, message, stop_ids: set[str], mode: str) -> list[LivePrediction]:
        """Pull arrivals at a set of stops out of a trip-updates message."""
        if message is None:
            return []
        now = time.time()
        out: list[LivePrediction] = []

        for entity in message.entity:
            if not entity.HasField("trip_update"):
                continue
            update = entity.trip_update
            route = update.trip.route_id
            trip_id = update.trip.trip_id

            for stop_time in update.stop_time_update:
                if stop_time.stop_id not in stop_ids:
                    continue
                # Departure where there is one; a terminus only has arrival.
                event = (
                    stop_time.departure
                    if stop_time.HasField("departure")
                    else stop_time.arrival
                )
                if event is None or not event.time:
                    continue
                minutes = int(round((event.time - now) / 60.0))
                if minutes < 0:
                    # Already gone. The feed keeps recent stops briefly.
                    continue
                out.append(
                    LivePrediction(
                        route_name=route,
                        headsign="",
                        minutes=minutes,
                        mode=mode,
                        vehicle_id=entity.id,
                        trip_id=trip_id,
                    )
                )

        out.sort(key=lambda p: p.minutes)
        return out

    def bus_predictions(self, stop_id: str) -> list[LivePrediction]:
        return self._predictions_from(
            self._feed(BUS_TRIP_UPDATES), {stop_id}, "bus"
        )

    def rail_predictions(self, station_codes: list[str]) -> list[LivePrediction]:
        """Subway arrivals at a set of GTFS stop ids.

        ``station_codes`` here are GTFS stop ids, not the letter-and-digit
        codes WMATA uses — the interface is shared but the identifier space is
        each agency's own. The feeds to query are derived from the route
        letters embedded in those ids, which is what avoids fetching all seven.
        """
        if not station_codes:
            return []

        wanted = set(station_codes)
        # Subway stop ids are the station id plus a direction suffix (N/S), and
        # the platform ids in the static feed carry it. Match on both.
        wanted |= {s.rstrip("NS") for s in station_codes}

        out: list[LivePrediction] = []
        for feed_url in self._feeds_for(station_codes):
            out.extend(self._predictions_from(self._feed(feed_url), wanted, "metro"))
        out.sort(key=lambda p: p.minutes)
        return out

    def _feeds_for(self, stop_ids: list[str]) -> list[str]:
        """The subway feeds that could carry these stops.

        MTA subway stop ids begin with the route letter or number of the line
        the station is on — "A31", "L06", "635". Mapping that first character
        to a feed avoids fetching all seven for one station, at the cost of
        occasionally missing a train from another line calling at a shared
        platform. Where the character does not resolve, every feed is queried
        rather than none: a slow answer beats a wrong one.
        """
        feeds: set[str] = set()
        for stop_id in stop_ids:
            head = stop_id[:1].upper()
            group = ROUTE_TO_FEED.get(head)
            if group:
                feeds.add(SUBWAY_FEEDS[group])
        return sorted(feeds) if feeds else sorted(set(SUBWAY_FEEDS.values()))

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def bus_position_for_trip(self, trip_id: str) -> LiveVehicle | None:
        """Where the bus running a given GTFS trip is.

        Exact, unlike the WMATA equivalent: GTFS-Realtime carries the static
        feed's own trip id, so there is no matching to do.
        """
        if not trip_id:
            return None
        message = self._feed(BUS_VEHICLE_POSITIONS)
        if message is None:
            return None

        for entity in message.entity:
            if not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle
            if vehicle.trip.trip_id != trip_id:
                continue
            if not vehicle.HasField("position"):
                return None
            return LiveVehicle(
                vehicle_id=vehicle.vehicle.id or entity.id,
                route_name=vehicle.trip.route_id,
                headsign="",
                lat=float(vehicle.position.latitude),
                lon=float(vehicle.position.longitude),
                mode="bus",
                # GTFS-RT has no schedule-deviation field. The trip update
                # carries per-stop delays, which is a better number anyway and
                # is already folded into the predictions above.
                deviation_s=0.0,
                trip_id=trip_id,
            )
        return None
