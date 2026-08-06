"""Real-time transit, per agency.

The two agencies do not merely differ in URL — they publish different
protocols, and pretending otherwise is how this would end up with a WMATA
shape bolted onto an MTA feed:

* **WMATA** serves JSON over REST: one call per stop for predictions, one for
  the whole bus fleet's positions. Buses report latitude and longitude; trains
  report a track-circuit id that cannot be turned into a coordinate without a
  separate feed.
* **The MTA** serves GTFS-Realtime protobuf. Predictions and positions arrive
  in the same message, keyed by GTFS trip id — which means, unlike WMATA, the
  live data joins onto the static timetable exactly rather than by matching on
  route and rough time. Subway feeds are split by line group, bus feeds cover
  the whole city.

`LiveProvider` is the surface the API talks to, and it is written around what
both can answer rather than around either one's shape. Everything is optional:
a provider with no key, no support, or a dead upstream reports itself disabled
and the app falls back to scheduled times, which is what it showed before any
of this existed.
"""

from __future__ import annotations

from typing import Protocol

from ..cities import City
from .base import LivePrediction, LiveVehicle, NullLive


class LiveProvider(Protocol):
    """What the API needs from a real-time source."""

    @property
    def enabled(self) -> bool:
        """Whether this provider can currently answer anything at all."""
        ...

    @property
    def disabled_reason(self) -> str:
        """Why not, phrased for a user rather than an operator."""
        ...

    def bus_predictions(self, stop_id: str) -> list[LivePrediction]: ...

    def rail_predictions(self, station_codes: list[str]) -> list[LivePrediction]: ...

    def bus_position_for_trip(self, trip_id: str) -> LiveVehicle | None: ...


def live_provider_for(city: City) -> LiveProvider:
    """The real-time client for a city's transit agency.

    Imported lazily so a deployment serving only one city does not pay for the
    other's dependencies — the MTA client needs protobuf bindings that a
    Washington-only install has no reason to have installed.
    """
    if city.realtime == "wmata":
        from .wmata import WmataLive  # noqa: PLC0415

        return WmataLive()
    if city.realtime == "mta":
        from .mta import MtaLive  # noqa: PLC0415

        return MtaLive()
    return NullLive(
        f"{city.name} has no real-time transit source configured."
    )


__all__ = [
    "LivePrediction",
    "LiveProvider",
    "LiveVehicle",
    "NullLive",
    "live_provider_for",
]
