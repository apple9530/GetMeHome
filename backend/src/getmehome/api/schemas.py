"""Request and response models for the HTTP API.

Field names are camelCase because the consumer is a Swift client and
``Codable`` maps camelCase without a custom key strategy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Coordinate(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class RouteRequest(BaseModel):
    origin: Coordinate
    destination: Coordinate
    destinationName: str = "your destination"
    # Omit to plan for right now.
    departAt: datetime | None = None
    modes: list[Literal["walk", "transit"]] = Field(default=["walk", "transit"])
    avoidCameras: bool = False
    # Overrides the automatic sunset-based determination. Exposed mainly so a
    # user planning tomorrow's late walk can see the night scoring now.
    forceNight: bool | None = None

    @field_validator("modes")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one mode is required")
        return v


class SafetyScore(BaseModel):
    overall: int
    lighting: int
    crime: int
    isolation: int
    pctWellLit: int
    pctHighCrime: int
    worstStretchRisk: int
    worstStretchName: str


class CameraExposure(BaseModel):
    camerasPassed: int = 0
    fractionCovered: float = 0.0


class StepModel(BaseModel):
    maneuver: str
    instruction: str
    voice: str
    street: str
    distance: float
    duration: float
    startIndex: int
    location: Coordinate
    safetyNote: str = ""
    voiceTriggers: list[float] = []


class LegModel(BaseModel):
    mode: str
    distance: float
    duration: float
    # Encoded as a flat [lat, lon, lat, lon, ...] array: roughly half the JSON
    # bytes of a list of objects, which matters on a cellular connection.
    polyline: list[float]
    steps: list[StepModel] = []
    safety: SafetyScore | None = None

    routeName: str = ""
    headsign: str = ""
    fromStopName: str = ""
    toStopName: str = ""
    departureTime: str = ""
    arrivalTime: str = ""
    numStops: int = 0


class ItineraryModel(BaseModel):
    id: str
    kind: Literal["walk", "transit"]
    label: str
    summary: str
    duration: float
    walkDistance: float
    departureTime: str
    arrivalTime: str
    numTransfers: int
    safety: SafetyScore
    cameras: CameraExposure
    legs: list[LegModel]


class RouteResponse(BaseModel):
    itineraries: list[ItineraryModel]
    isNight: bool
    generatedAt: datetime
    # Present when the request succeeded but something was degraded, e.g.
    # transit was asked for but no GTFS feed is loaded.
    notices: list[str] = []


class CameraModel(BaseModel):
    id: str
    lat: float
    lon: float
    # Null means the direction is unmapped; the client should draw it as
    # omnidirectional rather than guessing a bearing.
    direction: float | None = None
    operator: str = ""


class CameraResponse(BaseModel):
    cameras: list[CameraModel]
    total: int
    truncated: bool = False


class SegmentSafetyModel(BaseModel):
    """One segment's safety, for the map's colour overlay."""

    polyline: list[float]
    risk: float
    lit: float
    crime: float


class SafetyOverlayResponse(BaseModel):
    segments: list[SegmentSafetyModel]
    isNight: bool
    truncated: bool = False


class GeocodeResult(BaseModel):
    name: str
    address: str
    lat: float
    lon: float


class GeocodeResponse(BaseModel):
    results: list[GeocodeResult]


class MetaResponse(BaseModel):
    builtAt: str = ""
    nodes: int
    segments: int
    streetlights: int
    crimeIncidents: int
    cameras: int
    transitStops: int
    transitPatterns: int
    crimeHistoryYears: int
    bbox: list[float]
