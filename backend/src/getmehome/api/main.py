"""HTTP API for the GetMeHome safety router."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..config import DC_BBOX, GEOCODER_URL, GEOCODER_USER_AGENT
from ..daylight import is_night as compute_is_night
from ..geo import simplify_polyline
from ..routing.multimodal import Itinerary, plan
from ..safety.cameras import cameras_in_bbox
from .schemas import (
    CameraModel,
    CameraResponse,
    GeocodeResponse,
    GeocodeResult,
    ItineraryModel,
    LegModel,
    MetaResponse,
    RouteRequest,
    RouteResponse,
    SafetyOverlayResponse,
    SegmentSafetyModel,
    StepModel,
)
from .state import get_state, load_state

log = logging.getLogger("getmehome.api")

# Overlay responses are capped so a zoomed-out request cannot return the whole
# city and stall the client.
MAX_OVERLAY_SEGMENTS = 4000
MAX_OVERLAY_CAMERAS = 1500


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    try:
        load_state()
    except FileNotFoundError as exc:
        # Start anyway so /health can report the problem rather than the
        # container crash-looping with the reason buried in logs.
        log.error("%s", exc)
    yield


app = FastAPI(
    title="GetMeHome",
    description="Safety-aware pedestrian and transit routing for Washington, DC",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _seconds_to_clock(seconds: float) -> str:
    """Seconds-since-midnight to HH:MM, wrapping past midnight."""
    total = int(seconds) % 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}"


def _flatten(coords: list[tuple[float, float]], tolerance_m: float = 2.0) -> list[float]:
    simplified = simplify_polyline(coords, tolerance_m) if len(coords) > 2 else coords
    out: list[float] = []
    for lat, lon in simplified:
        out.append(round(lat, 6))
        out.append(round(lon, 6))
    return out


def _safety_model(breakdown) -> dict:
    return {
        "overall": breakdown.overall,
        "lighting": breakdown.lighting,
        "crime": breakdown.crime,
        "isolation": breakdown.isolation,
        "pctWellLit": breakdown.pct_well_lit,
        "pctHighCrime": breakdown.pct_high_crime,
        "worstStretchRisk": breakdown.worst_stretch_risk,
        "worstStretchName": breakdown.worst_stretch_name,
    }


def _serialise(itinerary: Itinerary, index: int) -> ItineraryModel:
    legs: list[LegModel] = []
    for leg in itinerary.legs:
        legs.append(
            LegModel(
                mode=leg.mode,
                distance=round(leg.distance_m, 1),
                duration=round(leg.duration_s, 1),
                polyline=_flatten(leg.coords),
                steps=[StepModel(**s.to_dict()) for s in leg.steps],
                safety=_safety_model(leg.safety) if leg.safety else None,
                routeName=leg.route_name,
                headsign=leg.headsign,
                fromStopName=leg.from_stop_name,
                toStopName=leg.to_stop_name,
                departureTime=(
                    _seconds_to_clock(leg.departure_s) if leg.departure_s else ""
                ),
                arrivalTime=(
                    _seconds_to_clock(leg.arrival_s) if leg.arrival_s else ""
                ),
                numStops=leg.n_stops,
            )
        )

    return ItineraryModel(
        id=f"{itinerary.kind}-{index}",
        kind=itinerary.kind,
        label=itinerary.label,
        summary=itinerary.summary,
        duration=round(itinerary.duration_s, 1),
        walkDistance=round(itinerary.walk_distance_m, 1),
        departureTime=_seconds_to_clock(itinerary.departure_s),
        arrivalTime=_seconds_to_clock(itinerary.arrival_s),
        numTransfers=itinerary.n_transfers,
        safety=_safety_model(itinerary.safety),
        cameras={
            "camerasPassed": itinerary.cameras.get("cameras_passed", 0),
            "fractionCovered": itinerary.cameras.get("fraction_covered", 0.0),
        },
        legs=legs,
    )


@app.get("/health")
def health() -> dict:
    try:
        state = get_state()
    except RuntimeError:
        return {"status": "no_graph", "detail": "graph not built; run the build step"}
    return {
        "status": "ok",
        "segments": state.graph.n_segments,
        "transit": state.has_transit,
        "cameras": len(state.cameras),
    }


@app.get("/meta", response_model=MetaResponse)
def meta() -> MetaResponse:
    state = _require_state()
    m = state.graph.meta
    network = state.transit.network if state.transit else None
    return MetaResponse(
        builtAt=m.get("built_at", ""),
        nodes=state.graph.n_nodes,
        segments=state.graph.n_segments,
        streetlights=m.get("n_lights", 0),
        crimeIncidents=m.get("n_incidents", 0),
        cameras=len(state.cameras),
        transitStops=network.n_stops if network else 0,
        transitPatterns=len(network.patterns) if network else 0,
        crimeHistoryYears=m.get("crime_history_years", 0),
        bbox=m.get(
            "bbox",
            [DC_BBOX.min_lat, DC_BBOX.min_lon, DC_BBOX.max_lat, DC_BBOX.max_lon],
        ),
    )


def _require_state():
    try:
        return get_state()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Routing graph is not loaded. Run the build step first.",
        ) from exc


@app.post("/route", response_model=RouteResponse)
def route(request: RouteRequest) -> RouteResponse:
    state = _require_state()
    notices: list[str] = []

    when = request.departAt or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    night = (
        request.forceNight
        if request.forceNight is not None
        else compute_is_night(when, request.origin.lat, request.origin.lon)
    )

    origin = state.index.snap(request.origin.lat, request.origin.lon)
    if origin is None:
        raise HTTPException(
            status_code=422,
            detail="Start point is not near any walkable street in the DC area.",
        )
    destination = state.index.snap(request.destination.lat, request.destination.lon)
    if destination is None:
        raise HTTPException(
            status_code=422,
            detail="Destination is not near any walkable street in the DC area.",
        )

    modes = set(request.modes)
    if "transit" in modes and not state.has_transit:
        modes.discard("transit")
        modes.add("walk")
        notices.append("Transit data is unavailable; showing walking routes only.")

    itineraries = plan(
        state.index,
        origin,
        destination,
        when=when,
        is_night=night,
        modes=modes,
        avoid_cameras=request.avoidCameras,
        transit=state.transit,
        cameras=state.cameras if state.cameras else None,
        destination_name=request.destinationName,
    )

    if not itineraries:
        raise HTTPException(
            status_code=404, detail="No route found between those points."
        )

    if request.avoidCameras and not state.cameras:
        notices.append(
            "No ALPR camera data is loaded, so camera avoidance had no effect."
        )

    return RouteResponse(
        itineraries=[_serialise(it, i) for i, it in enumerate(itineraries)],
        isNight=night,
        generatedAt=datetime.now(UTC),
        notices=notices,
    )


@app.get("/cameras", response_model=CameraResponse)
def cameras(
    minLat: float = Query(...),
    minLon: float = Query(...),
    maxLat: float = Query(...),
    maxLon: float = Query(...),
) -> CameraResponse:
    """ALPR cameras in a bounding box, for the map overlay."""
    state = _require_state()
    hits = cameras_in_bbox(
        state.cameras, minLat, minLon, maxLat, maxLon, limit=MAX_OVERLAY_CAMERAS + 1
    )
    truncated = len(hits) > MAX_OVERLAY_CAMERAS
    hits = hits[:MAX_OVERLAY_CAMERAS]
    return CameraResponse(
        cameras=[
            CameraModel(
                id=c.osm_id or f"{c.lat:.5f},{c.lon:.5f}",
                lat=c.lat,
                lon=c.lon,
                direction=c.direction_deg,
                operator=c.operator,
            )
            for c in hits
        ],
        total=len(hits),
        truncated=truncated,
    )


@app.get("/safety/overlay", response_model=SafetyOverlayResponse)
def safety_overlay(
    minLat: float = Query(...),
    minLon: float = Query(...),
    maxLat: float = Query(...),
    maxLon: float = Query(...),
    night: bool | None = Query(None),
) -> SafetyOverlayResponse:
    """Per-segment risk in a bounding box, for colouring streets on the map."""
    state = _require_state()
    graph = state.graph

    is_night_now = (
        night
        if night is not None
        else compute_is_night(
            datetime.now(UTC),
            (minLat + maxLat) / 2,
            (minLon + maxLon) / 2,
        )
    )
    risk = graph.segment_risk(is_night_now)
    crime = graph.seg_crime_night if is_night_now else graph.seg_crime_day

    # Filter by segment start vertex, which is enough for an overlay and far
    # cheaper than a true geometry intersection.
    starts = graph.seg_geom_ptr[:-1]
    lat = graph.seg_geom[starts, 0]
    lon = graph.seg_geom[starts, 1]
    mask = (lat >= minLat) & (lat <= maxLat) & (lon >= minLon) & (lon <= maxLon)
    ids = np.nonzero(mask)[0]

    truncated = len(ids) > MAX_OVERLAY_SEGMENTS
    if truncated:
        # Keep the riskiest, since those are what the overlay exists to show.
        ids = ids[np.argsort(-risk[ids])[:MAX_OVERLAY_SEGMENTS]]

    return SafetyOverlayResponse(
        segments=[
            SegmentSafetyModel(
                polyline=_flatten(graph.segment_coords(int(s)), tolerance_m=6.0),
                risk=round(float(risk[s]), 3),
                lit=round(float(graph.seg_lit[s]), 3),
                crime=round(float(crime[s]), 3),
            )
            for s in ids
        ],
        isNight=is_night_now,
        truncated=truncated,
    )


@app.get("/geocode", response_model=GeocodeResponse)
def geocode(q: str = Query(..., min_length=2), limit: int = Query(8, le=20)):
    """Search for a place by name, restricted to the DC area."""
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": limit,
        "viewbox": f"{DC_BBOX.min_lon},{DC_BBOX.max_lat},{DC_BBOX.max_lon},{DC_BBOX.min_lat}",
        "bounded": 1,
        "addressdetails": 1,
    }
    try:
        response = httpx.get(
            f"{GEOCODER_URL}/search",
            params=params,
            headers={"User-Agent": GEOCODER_USER_AGENT},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Geocoder unavailable: {exc}"
        ) from exc

    results = []
    for row in payload:
        display = row.get("display_name", "")
        results.append(
            GeocodeResult(
                name=row.get("name") or display.split(",")[0],
                address=display,
                lat=float(row["lat"]),
                lon=float(row["lon"]),
            )
        )
    return GeocodeResponse(results=results)


@app.get("/reverse", response_model=GeocodeResponse)
def reverse(lat: float = Query(...), lon: float = Query(...)):
    """Name the place at a coordinate, for the 'drop a pin' flow."""
    try:
        response = httpx.get(
            f"{GEOCODER_URL}/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2"},
            headers={"User-Agent": GEOCODER_USER_AGENT},
            timeout=15.0,
        )
        response.raise_for_status()
        row = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Geocoder unavailable: {exc}"
        ) from exc

    display = row.get("display_name", "")
    if not display:
        return GeocodeResponse(results=[])
    return GeocodeResponse(
        results=[
            GeocodeResult(
                name=row.get("name") or display.split(",")[0],
                address=display,
                lat=float(row["lat"]),
                lon=float(row["lon"]),
            )
        ]
    )
