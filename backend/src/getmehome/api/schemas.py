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


class CityModel(BaseModel):
    """One city this server knows about."""

    slug: str
    name: str
    region: str
    centerLat: float
    centerLon: float
    # [minLat, minLon, maxLat, maxLon]
    bbox: list[float]
    timezone: str
    # Whether it has been built. A configured-but-unbuilt city is still listed
    # so the app can say what is missing rather than pretending it is not a
    # city at all.
    available: bool = True
    # Whether it is currently resident in memory. Cities load on first use, so
    # a request for one that is not loaded is several seconds slower.
    loaded: bool = False


class CitiesResponse(BaseModel):
    cities: list[CityModel]
    defaultCity: str


class RouteRequest(BaseModel):
    # Which city's graph to route on. Omit for the server's default.
    city: str | None = None
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
    # How far back the crime data should look, in days. Omit for the graph's
    # default. Values that were not built snap to the nearest one that was.
    crimeWindowDays: int | None = None

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
    # The window actually used, after snapping the request to a built one.
    crimeWindowDays: int = 0
    city: str = ""
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


class OffenseCount(BaseModel):
    # The raw MPD code, kept so clients can key on something stable.
    offense: str
    # What to actually show a person.
    displayName: str = ""
    count: int
    # "violent" | "sexual" | "property" | "other"
    category: str = "other"
    # Share of the cell's weighted risk, 0-1. Ordering by this rather than by
    # count is what stops a pile of car break-ins burying a single robbery.
    share: float = 0.0


class CrimeCellModel(BaseModel):
    """One hexagon of aggregated incidents, for the crime grid overlay."""

    id: str
    centerLat: float
    centerLon: float
    # The six corners, flat [lat, lon, ...]. Sent rather than derived on the
    # client so the drawn cell is exactly the one the server binned into.
    vertices: list[float]
    total: int
    intensity: float
    nightShare: float
    # Violent and sexual offences only.
    seriousCount: int = 0
    byOffense: list[OffenseCount]
    latest: str = ""


class CrimeGridResponse(BaseModel):
    cells: list[CrimeCellModel]
    # Cell circumradius in metres, so the client can label the scale.
    radius: float
    totalIncidents: int
    nightOnly: bool = False
    # Lookback applied, in days. 0 means everything held.
    windowDays: int = 0
    # Which city answered. Lets the client refuse to draw a grid it did not
    # ask for rather than painting one city's hexagons over another's map.
    city: str = ""
    # How many incidents the city holds in total, and the date of the most
    # recent one. Both are about the *feed*, not the viewport, and they exist
    # so an empty grid can explain itself: New York's police data is published
    # in quarterly batches, so a 30-day lookback over New York can legitimately
    # match nothing while Washington's near-live feed matches plenty. Without
    # these the overlay simply fails to appear and looks broken.
    heldIncidents: int = 0
    latestIncident: str = ""


class GeocodeResult(BaseModel):
    name: str
    address: str
    lat: float
    lon: float


class GeocodeResponse(BaseModel):
    results: list[GeocodeResult]
    # The city these results were searched in. Every result is inside its
    # bounding box; the client checks this against the city it asked for and
    # discards the response if they disagree, so a stale or misrouted answer
    # can never mix two cities' addresses into one list.
    city: str = ""


class MetaResponse(BaseModel):
    builtAt: str = ""
    nodes: int
    segments: int
    streetlights: int
    crimeIncidents: int
    cameras: int
    transitStops: int
    transitPatterns: int
    places: int = 0
    crimeHistoryYears: int
    # Selectable crime lookback windows, in days, and which one is default.
    crimeWindows: list[int] = []
    defaultCrimeWindow: int = 0
    city: str = ""
    cityName: str = ""
    bbox: list[float]
    # ISO date of the most recent incident held, and how stale that makes the
    # data. Surfaced because the two cities' feeds update on completely
    # different cadences and "no crime data on the map" is otherwise
    # indistinguishable from a bug.
    latestIncident: str = ""
    crimeDataAgeDays: int = 0
    # Share of segments with any lamp in range, 0-1.
    #
    # Deliberately not the median lighting score: lighting is *ranked* against
    # the city and every segment with no lamp near it ties at the bottom, so a
    # city where a third of streets are unlit has a median of zero and looks
    # identical to one with no streetlight data at all. This number cannot be
    # confused that way — zero means no inventory was ingested.
    litShare: float = 0.0


# --------------------------------------------------------------------------
# Transit stops, timetables and live vehicles
# --------------------------------------------------------------------------


class TransitStopModel(BaseModel):
    """One stop marker on the map."""

    id: str
    name: str
    lat: float
    lon: float
    # "metro" | "bus" | "rail" | ...
    mode: str
    routes: list[str] = []


class TransitStopsResponse(BaseModel):
    stops: list[TransitStopModel]
    total: int
    # True when the viewport holds more stops than were returned, so the
    # client can say "zoom in" rather than implying this is all of them.
    truncated: bool = False


class DepartureModel(BaseModel):
    routeName: str
    headsign: str
    mode: str
    # HH:MM on the service day.
    scheduledTime: str
    # Minutes from now, from the schedule.
    scheduledMinutes: int
    # Minutes from now per the operator's live prediction, when there is one.
    liveMinutes: int | None = None
    patternId: int
    tripId: str
    stopsRemaining: int
    vehicleId: str = ""


class StopBoardResponse(BaseModel):
    stopId: str
    stopName: str
    mode: str
    departures: list[DepartureModel]
    # Whether any live prediction was available, and why not when it was not.
    live: bool = False
    liveNote: str = ""


class TripStopModel(BaseModel):
    stopId: str
    name: str
    lat: float
    lon: float
    arrivalTime: str
    # Minutes from now. Negative once the call is in the past.
    minutes: int
    passed: bool = False


class VehiclePosition(BaseModel):
    lat: float
    lon: float
    # True when interpolated from predictions rather than reported. Trains are
    # always estimated: WMATA publishes track circuits, not coordinates.
    estimated: bool = False


class TripDetailResponse(BaseModel):
    patternId: int
    tripId: str
    routeName: str
    headsign: str
    mode: str
    stops: list[TripStopModel]
    # Flat [lat, lon, ...] along the whole trip, for the map.
    polyline: list[float]
    # Seconds behind schedule; negative is early.
    deviationSeconds: float = 0.0
    vehicle: VehiclePosition | None = None
    liveNote: str = ""


# --------------------------------------------------------------------------
# Live ETA sharing
# --------------------------------------------------------------------------


class ShareCreateRequest(BaseModel):
    destinationName: str = Field("their destination", max_length=120)


class ShareCreatedResponse(BaseModel):
    token: str
    # The link to hand to a friend. Watching only — it cannot move the dot.
    url: str
    # The walker's write credential. Returned once, never in the shared link,
    # and never echoed back by any read endpoint.
    ownerKey: str
    expiresInSeconds: float


class ShareUpdateRequest(BaseModel):
    ownerKey: str
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    etaSeconds: float | None = Field(None, ge=0)
    remainingMetres: float | None = Field(None, ge=0)


class ShareFinishRequest(BaseModel):
    ownerKey: str
    # True when the walk completed; false when the walker stopped sharing.
    arrived: bool = False


class ShareStatusResponse(BaseModel):
    """What a recipient sees. Deliberately carries no owner key."""

    status: str  # active | arrived | stale | ended
    destinationName: str
    lat: float | None = None
    lon: float | None = None
    etaSeconds: float | None = None
    remainingMetres: float | None = None
    updatedAgoSeconds: float = 0
    expiresInSeconds: float = 0
    pollAfterSeconds: int = 10
