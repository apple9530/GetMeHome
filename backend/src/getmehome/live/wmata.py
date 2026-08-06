"""WMATA real-time feeds.

The GTFS timetable says when a bus is *meant* to arrive. This says where it
actually is. Both are needed and neither replaces the other: predictions cover
the next half hour and vanish once service ends, while the schedule covers the
whole day and never goes down.

Everything here fails soft. A dead or rate-limited WMATA endpoint degrades the
app to scheduled times, which is what it showed before any of this existed —
it must never turn a working timetable into an error page.

What the public API can and cannot do, since it shapes the feature:

* **Buses report a position.** ``jBusPositions`` gives latitude, longitude,
  trip id and schedule deviation, so a bus can be drawn where it is and its
  lateness applied to every remaining stop.
* **Trains do not.** ``TrainPositions`` reports a track-circuit id, and turning
  that into a coordinate needs the standard-routes circuit map, which is a
  separate feed and a substantial amount of matching. What is available per
  station is an arrival prediction, so a train's position is *estimated* by
  interpolating between the station it last left and the one it is next
  predicted at. That estimate is labelled as one wherever it surfaces.
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from ..config import WMATA_API_KEY
from .base import LivePrediction, LiveVehicle

log = logging.getLogger("getmehome.wmata")

BUS_POSITIONS_URL = "https://api.wmata.com/Bus.svc/json/jBusPositions"
BUS_PREDICTIONS_URL = "https://api.wmata.com/NextBusService.svc/json/jPredictions"
RAIL_PREDICTIONS_URL = "https://api.wmata.com/StationPrediction.svc/json/GetPrediction"

# Responses are cached briefly. Predictions refresh roughly every 20-30s
# upstream, so asking more often than that spends quota without gaining
# freshness — and every stop tapped on the map is another call.
CACHE_TTL_S = 20.0

_TIMEOUT_S = 6.0

# WMATA rail GTFS stop ids carry the station code, in forms like "STN_A01" or
# "PF_A01_C". The code itself is a letter and two digits.
# Not \b: an underscore is a word character, so there is no boundary between
# "STN_" and "A01" and the pattern would never fire on WMATA's own ids.
_STATION_CODE = re.compile(r"(?:^|[^A-Z0-9])([A-Z]\d{2})(?:[^A-Z0-9]|$)")


class WmataLive:
    """Cached client for the real-time endpoints.

    One instance is held by the app state. It is deliberately synchronous:
    the calls are made from request handlers that are already threaded by
    FastAPI, and an async client here would need its own lifecycle for no
    measurable gain at this traffic.
    """

    def __init__(self, api_key: str = WMATA_API_KEY, timeout_s: float = _TIMEOUT_S):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[float, object]] = {}
        self._failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def disabled_reason(self) -> str:
        if not self.api_key:
            return "Live arrivals need a WMATA API key; showing the timetable."
        return "No live arrivals right now; showing the timetable."

    def _get(self, url: str, params: dict | None = None, cache_key: str = "") -> dict:
        """Fetch and cache, returning ``{}`` on any failure."""
        if not self.enabled:
            return {}

        key = cache_key or f"{url}?{sorted((params or {}).items())}"
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]  # type: ignore[return-value]

        try:
            response = httpx.get(
                url,
                params=params or {},
                headers={"api_key": self.api_key},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._failures += 1
            # Logged at warning once and debug thereafter: a sustained outage
            # would otherwise fill the log with one line per stop tapped.
            level = logging.WARNING if self._failures == 1 else logging.DEBUG
            log.log(level, "WMATA real-time unavailable (%s): %s", url, exc)
            # Serve a stale entry rather than nothing — a two-minute-old
            # prediction still beats no prediction.
            return hit[1] if hit is not None else {}  # type: ignore[return-value]

        self._failures = 0
        self._cache[key] = (now, payload)
        return payload

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def bus_predictions(self, stop_id: str) -> list[LivePrediction]:
        payload = self._get(BUS_PREDICTIONS_URL, {"StopID": stop_id})
        out: list[LivePrediction] = []
        for row in payload.get("Predictions", []) or []:
            try:
                out.append(
                    LivePrediction(
                        route_name=str(row.get("RouteID", "")),
                        headsign=str(row.get("DirectionText", "")),
                        minutes=int(row.get("Minutes", 0)),
                        mode="bus",
                        vehicle_id=str(row.get("VehicleID", "")),
                        trip_id=str(row.get("TripID", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    def rail_predictions(self, station_codes: list[str]) -> list[LivePrediction]:
        if not station_codes:
            return []
        codes = ",".join(sorted(set(station_codes)))
        payload = self._get(f"{RAIL_PREDICTIONS_URL}/{codes}", cache_key=f"rail:{codes}")

        out: list[LivePrediction] = []
        for row in payload.get("Trains", []) or []:
            minutes = _rail_minutes(row.get("Min"))
            if minutes is None:
                continue
            out.append(
                LivePrediction(
                    route_name=str(row.get("Line", "")),
                    headsign=str(row.get("DestinationName", "")),
                    minutes=minutes,
                    mode="metro",
                    vehicle_id=str(row.get("TrainId", "") or ""),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def bus_positions(
        self, lat: float | None = None, lon: float | None = None, radius_m: float = 0.0
    ) -> list[LiveVehicle]:
        params: dict = {}
        if lat is not None and lon is not None and radius_m > 0:
            params = {"Lat": lat, "Lon": lon, "Radius": int(radius_m)}
        payload = self._get(BUS_POSITIONS_URL, params)

        out: list[LiveVehicle] = []
        for row in payload.get("BusPositions", []) or []:
            try:
                out.append(
                    LiveVehicle(
                        vehicle_id=str(row.get("VehicleID", "")),
                        route_name=str(row.get("RouteID", "")),
                        headsign=str(row.get("TripHeadsign", "")),
                        lat=float(row["Lat"]),
                        lon=float(row["Lon"]),
                        mode="bus",
                        deviation_s=float(row.get("Deviation", 0.0) or 0.0) * 60.0,
                        trip_id=str(row.get("TripID", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def bus_position_for_trip(self, trip_id: str) -> LiveVehicle | None:
        """The vehicle running a given GTFS trip, if it is out there.

        The whole-fleet call is cached, so looking one bus up costs nothing
        beyond the first request in each cache window.
        """
        if not trip_id:
            return None
        for vehicle in self.bus_positions():
            if vehicle.trip_id == trip_id:
                return vehicle
        return None


def _rail_minutes(value) -> int | None:
    """WMATA reports rail arrivals as minutes, or as ARR / BRD / --- / ."""
    text = str(value or "").strip().upper()
    if text in ("ARR", "BRD"):
        return 0
    if text in ("", "---", "DLY"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def station_code(stop_id: str) -> str:
    """Pull a WMATA station code out of a GTFS rail stop id.

    Returns "" when the id does not look like one, which is the correct
    outcome for every bus stop and the safe one for a feed whose id format has
    changed — no code means no rail prediction, not a wrong prediction.
    """
    match = _STATION_CODE.search(stop_id.upper())
    return match.group(1) if match else ""
