"""Shared shapes for real-time transit, and the no-op provider."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LivePrediction:
    """One upcoming arrival, as the operator currently expects it."""

    route_name: str
    headsign: str
    minutes: int
    mode: str
    vehicle_id: str = ""
    # The GTFS trip id, where the feed gives one. WMATA's real-time trip ids do
    # not reliably match the static feed, so predictions there are matched on
    # route and time; GTFS-Realtime carries the real id and can be joined
    # exactly. Anything consuming this should prefer the id when it is present
    # and fall back to matching when it is not.
    trip_id: str = ""


@dataclass
class LiveVehicle:
    """Where a vehicle is now."""

    vehicle_id: str
    route_name: str
    headsign: str
    lat: float
    lon: float
    mode: str = "bus"
    # Seconds ahead (negative) or behind (positive) schedule.
    deviation_s: float = 0.0
    trip_id: str = ""
    # True when the position was interpolated rather than reported.
    estimated: bool = False


class NullLive:
    """A provider for a city with no real-time source.

    Exists so callers never branch on whether a provider is present — an
    absent feature and a broken one should look the same to the code that
    consumes them, and different only in what the user is told.
    """

    def __init__(self, reason: str = "Live arrivals are not available here.") -> None:
        self._reason = reason

    @property
    def enabled(self) -> bool:
        return False

    @property
    def disabled_reason(self) -> str:
        return self._reason

    def bus_predictions(self, stop_id: str) -> list[LivePrediction]:
        return []

    def rail_predictions(self, station_codes: list[str]) -> list[LivePrediction]:
        return []

    def bus_position_for_trip(self, trip_id: str) -> LiveVehicle | None:
        return None
