"""HTTP API for the GetMeHome safety router."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ..config import DC_BBOX, GEOCODER_URL, GEOCODER_USER_AGENT
from ..daylight import is_night as compute_is_night
from ..geo import simplify_polyline
from ..routing.multimodal import Itinerary, plan
from ..safety.cameras import cameras_in_bbox
from ..safety.hexgrid import hex_vertices
from .schemas import (
    CameraModel,
    CameraResponse,
    CrimeCellModel,
    CrimeGridResponse,
    GeocodeResponse,
    GeocodeResult,
    ItineraryModel,
    LegModel,
    MetaResponse,
    OffenseCount,
    RouteRequest,
    RouteResponse,
    StepModel,
)
from .state import get_state, load_state

log = logging.getLogger("getmehome.api")

# Camera responses are capped so a zoomed-out request cannot return the whole
# city and stall the client. The crime grid needs no such cap: it adapts its
# cell size to the viewport instead.
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


def _flatten_pairs(coords: list[tuple[float, float]]) -> list[float]:
    """(lat, lon) pairs to a flat array, with no simplification."""
    out: list[float] = []
    for lat, lon in coords:
        out.append(round(lat, 6))
        out.append(round(lon, 6))
    return out


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
        "streetlights": state.graph.meta.get("n_lights", 0),
        "crimeIncidents": state.crime.count if state.crime else 0,
        "searchablePlaces": len(state.places) if state.places else 0,
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
        places=len(state.places) if state.places else 0,
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
            "No camera data is loaded, so camera avoidance had no effect."
        )

    # Lighting is the biggest term in the night model, so silently scoring a
    # night route with no lamp data would produce numbers that look fine and
    # mean much less than they appear to.
    if night and not state.graph.meta.get("n_lights"):
        notices.append(
            "No streetlight data is loaded, so lighting is not affecting "
            "these scores. Rebuild the graph to include it."
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


@app.get("/crime/grid", response_model=CrimeGridResponse)
def crime_grid(
    minLat: float = Query(...),
    minLon: float = Query(...),
    maxLat: float = Query(...),
    maxLon: float = Query(...),
    nightOnly: bool = Query(False),
) -> CrimeGridResponse:
    """Incidents binned into hexagons over a bounding box.

    This replaced a per-street risk overlay that shipped thousands of
    individual polylines and stalled the client trying to draw them. A few
    hundred hexagons carry the same information at a fraction of the render
    cost, and unlike a coloured street they can be tapped for the underlying
    incident counts.
    """
    state = _require_state()
    if state.crime is None or state.crime.count == 0:
        return CrimeGridResponse(
            cells=[], radius=0.0, totalIncidents=0, nightOnly=nightOnly
        )

    cells, radius = state.crime.cells(
        minLat, minLon, maxLat, maxLon, night_only=nightOnly
    )

    return CrimeGridResponse(
        cells=[
            CrimeCellModel(
                # Position-derived id: stable across requests at the same zoom,
                # so the client can keep a selection through a refresh.
                id=f"{radius:.0f}:{c.center_lat:.5f},{c.center_lon:.5f}",
                centerLat=round(c.center_lat, 6),
                centerLon=round(c.center_lon, 6),
                vertices=_flatten_pairs(
                    hex_vertices(c.center_lat, c.center_lon, radius)
                ),
                total=c.total,
                intensity=c.intensity,
                nightShare=round(c.night_share, 3),
                seriousCount=c.serious_count,
                byOffense=[
                    OffenseCount(
                        offense=b.offense,
                        displayName=b.display,
                        count=b.count,
                        category=b.category,
                        share=b.share,
                    )
                    for b in c.by_offense
                ],
                latest=c.latest.date().isoformat() if c.latest else "",
            )
            for c in cells
        ],
        radius=round(radius, 1),
        totalIncidents=sum(c.total for c in cells),
        nightOnly=nightOnly,
    )


@app.get("/geocode", response_model=GeocodeResponse)
def geocode(
    q: str = Query(..., min_length=1),
    limit: int = Query(12, le=25),
    lat: float | None = Query(None),
    lon: float | None = Query(None),
):
    """Search for a place by name.

    The local index built from the OSM extract answers first. It is forgiving
    about punctuation and abbreviations in ways an external geocoder is not —
    "madams organ" finds Madam's Organ, "14th st nw" finds 14th Street
    Northwest — and it has no rate limit, so it can answer every keystroke.

    Nominatim is consulted only to top up a thin result set, which is mostly
    house-number addresses that OSM carries as interpolation rather than as
    named objects.
    """
    state = None
    try:
        state = get_state()
    except RuntimeError:
        pass

    near = (lat, lon) if lat is not None and lon is not None else None
    results: list[GeocodeResult] = []
    seen: set[tuple[int, int]] = set()

    if state is not None and state.places is not None:
        for hit in state.places.search(q, limit=limit, near=near):
            key = (int(hit.place.lat * 20000), int(hit.place.lon * 20000))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                GeocodeResult(
                    name=hit.place.name,
                    address=hit.place.address,
                    lat=hit.place.lat,
                    lon=hit.place.lon,
                )
            )

    # Only reach outward when the local index came up short. Nominatim is
    # rate-limited, so calling it on every keystroke gets the app throttled
    # within a few words.
    if len(results) < 4:
        for row in _nominatim(q, limit):
            key = (int(row.lat * 20000), int(row.lon * 20000))
            if key in seen:
                continue
            seen.add(key)
            results.append(row)

    return GeocodeResponse(results=results[:limit])


def _nominatim(q: str, limit: int) -> list[GeocodeResult]:
    """Query the external geocoder, returning nothing if it is unavailable.

    Deliberately non-fatal: local results are usually enough, and a geocoder
    outage should degrade search rather than break it.
    """
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
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("geocoder unavailable: %s", exc)
        return []

    out: list[GeocodeResult] = []
    for row in payload:
        display = row.get("display_name", "")
        try:
            out.append(
                GeocodeResult(
                    name=row.get("name") or display.split(",")[0],
                    address=display,
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
