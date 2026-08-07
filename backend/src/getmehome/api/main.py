"""HTTP API for the GetMeHome safety router."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from ..config import (
    GEOCODER_URL,
    GEOCODER_USER_AGENT,
    PUBLIC_BASE_URL,
)
from ..crime_pack import build_pack
from ..daylight import is_night as compute_is_night
from ..geo import simplify_polyline
from ..live.wmata import station_code
from ..places import parse_address, street_match
from ..routing.multimodal import Itinerary, plan
from ..safety.cameras import cameras_in_bbox
from ..safety.hexgrid import hex_vertices
from ..sharing import MIN_POLL_INTERVAL_S, get_store, view_of
from ..transit_board import (
    RAIL_MODES,
    attach_live,
    departures,
    estimate_rail_position,
    resolve_stop,
    stops_in_bbox,
    trip_detail,
)
from .schemas import (
    CameraModel,
    CameraResponse,
    CitiesResponse,
    CityModel,
    CrimeCellModel,
    CrimeGridResponse,
    DepartureModel,
    GeocodeResponse,
    GeocodeResult,
    ItineraryModel,
    LegModel,
    MetaResponse,
    OffenseCount,
    RouteRequest,
    RouteResponse,
    ShareCreatedResponse,
    ShareCreateRequest,
    ShareFinishRequest,
    ShareStatusResponse,
    ShareUpdateRequest,
    StepModel,
    StopBoardResponse,
    TransitStopModel,
    TransitStopsResponse,
    TripDetailResponse,
    TripStopModel,
    VehiclePosition,
)
from .state import get_state, preload_from_env, states

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
    # Cities load on first request. Preloading is opt-in because holding
    # every city resident costs the memory of the largest one per city, and a
    # deployment serving one of them should not pay for the others.
    try:
        preload_from_env()
    except Exception as exc:  # noqa: BLE001 — must not stop the app booting
        log.error("preload failed: %s", exc)

    built = [c.slug for c in states().available()]
    if built:
        log.info("cities built and ready: %s", ", ".join(built))
    else:
        # Start anyway so /health can report the problem rather than the
        # container crash-looping with the reason buried in logs.
        log.error(
            "No city has been built. Run:\n"
            "    cd backend && python -m getmehome.graph.build --city dc"
        )
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


@app.get("/cities", response_model=CitiesResponse)
def cities() -> CitiesResponse:
    """Which cities this server can route in, and which are ready.

    The app calls this before anything else. A city that is configured but not
    built is still listed, marked unavailable — that is a deployment state
    worth showing rather than a city that silently does not exist.
    """
    store = states()
    return CitiesResponse(
        cities=[
            CityModel(
                slug=city.slug,
                name=city.name,
                region=city.region,
                centerLat=round(city.center[0], 6),
                centerLon=round(city.center[1], 6),
                bbox=city.bbox.as_list(),
                timezone=city.timezone_name,
                available=store.is_built(city),
                loaded=store.is_loaded(city),
            )
            for city in CITIES.values()
        ],
        defaultCity=DEFAULT_CITY,
    )


@app.get("/health")
def health(city: str | None = Query(None)) -> dict:
    try:
        state = get_state(city)
    except (RuntimeError, FileNotFoundError, KeyError) as exc:
        return {
            "status": "no_graph",
            "detail": str(exc),
            "citiesBuilt": [c.slug for c in states().available()],
        }
    return {
        "status": "ok",
        "city": state.city.slug,
        "segments": state.graph.n_segments,
        "streetlights": state.graph.meta.get("n_lights", 0),
        "crimeIncidents": state.crime.count if state.crime else 0,
        "searchablePlaces": len(state.places) if state.places else 0,
        "transitStops": (
            state.transit.network.n_stops if state.transit else 0
        ),
        "transit": state.has_transit,
        "cameras": len(state.cameras),
    }


@app.get("/meta", response_model=MetaResponse)
def meta(city: str | None = Query(None)) -> MetaResponse:
    state = _require_state(city)
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
        crimeWindows=list(state.graph.crime_windows),
        defaultCrimeWindow=state.graph.resolved_window(None) or 0,
        city=state.city.slug,
        cityName=state.city.name,
        bbox=m.get("bbox", state.city.bbox.as_list()),
    )


def _off_map(lat: float, lon: float, city: City, which: str) -> str:
    """Why a point could not be snapped, in terms the user can act on.

    The commonest cause by far is having the wrong city selected, and "no
    walkable street near there" is a poor way to say so.
    """
    from ..cities import city_for_point  # noqa: PLC0415

    elsewhere = city_for_point(lat, lon)
    if elsewhere is not None and elsewhere.slug != city.slug:
        return (
            f"{which} point is in {elsewhere.name}, but {city.name} is "
            f"selected. Switch cities to route there."
        )
    if not city.bbox.contains(lat, lon):
        return f"{which} point is outside the {city.name} map."
    return f"{which} point is not near any walkable street in {city.name}."


def _require_city(slug: str | None) -> City:
    try:
        return get_city(slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_state(slug: str | None = None):
    """State for a city, loading it on first use.

    The first request for an unloaded city pays several seconds while its
    graph comes off disk. That is deliberate — see `api/state.py` — and it is
    why the app asks for a city up front rather than on the first route.
    """
    city = _require_city(slug)
    try:
        return get_state(city.slug)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{city.name} has not been built yet. Run: "
                f"python -m getmehome.graph.build --city {city.slug}"
            ),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/route", response_model=RouteResponse)
def route(request: RouteRequest) -> RouteResponse:
    state = _require_state(request.city)
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
            detail=_off_map(request.origin.lat, request.origin.lon, state.city, "Start"),
        )
    destination = state.index.snap(request.destination.lat, request.destination.lon)
    if destination is None:
        raise HTTPException(
            status_code=422,
            detail=_off_map(
                request.destination.lat,
                request.destination.lon,
                state.city,
                "Destination",
            ),
        )

    window = state.graph.resolved_window(request.crimeWindowDays)
    if (
        request.crimeWindowDays is not None
        and window is not None
        and window != request.crimeWindowDays
    ):
        notices.append(
            f"Crime data was built for {window}-day windows; "
            f"using {window} days instead of {request.crimeWindowDays}."
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
        window_days=request.crimeWindowDays,
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
        crimeWindowDays=window or 0,
        city=state.city.slug,
        generatedAt=datetime.now(UTC),
        notices=notices,
    )


@app.get("/cameras", response_model=CameraResponse)
def cameras(
    minLat: float = Query(...),
    minLon: float = Query(...),
    maxLat: float = Query(...),
    maxLon: float = Query(...),
    city: str | None = Query(None),
) -> CameraResponse:
    """ALPR cameras in a bounding box, for the map overlay."""
    state = _require_state(city)
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
    windowDays: int | None = Query(None),
    city: str | None = Query(None),
) -> CrimeGridResponse:
    """Incidents binned into hexagons over a bounding box.

    This replaced a per-street risk overlay that shipped thousands of
    individual polylines and stalled the client trying to draw them. A few
    hundred hexagons carry the same information at a fraction of the render
    cost, and unlike a coloured street they can be tapped for the underlying
    incident counts.
    """
    state = _require_state(city)
    # Snap to a built window so the map and the route agree on the period,
    # even if the client asks for one the graph was not built with.
    window = state.graph.resolved_window(windowDays) if windowDays else None
    if state.crime is None or state.crime.count == 0:
        return CrimeGridResponse(
            cells=[],
            radius=0.0,
            totalIncidents=0,
            nightOnly=nightOnly,
            windowDays=window or 0,
        )

    cells, radius = state.crime.cells(
        minLat, minLon, maxLat, maxLon, night_only=nightOnly, window_days=window
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
                    hex_vertices(
                        c.center_lat, c.center_lon, radius, state.graph.projection
                    )
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
        windowDays=window or 0,
    )


@app.get("/crime/pack")
def crime_pack(city: str | None = Query(None)) -> Response:
    """The whole city's crime grid, for use with no server.

    Built once and cached in memory: it is sixteen passes over the incident
    set and only changes when the graph is rebuilt. Served as pre-encoded
    JSON so a repeat download does not re-serialise tens of thousands of
    cells.

    An ETag lets a phone that already has the current pack skip the transfer
    entirely — a re-download after a rebuild is the only one that should cost
    anything.
    """
    state = _require_state(city)
    if state.crime is None or state.crime.count == 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No crime points for {state.city.name}. Rebuild with: "
                f"python -m getmehome.graph.build --city {state.city.slug}"
            ),
        )

    payload, etag = _cached_pack(state)
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "ETag": etag,
            # A pack is only replaced by a rebuild, so a day is safe and saves
            # a re-download for anyone who opens the app repeatedly.
            "Cache-Control": "public, max-age=86400",
            "X-Pack-Cells": str(_pack_cache[state.city.slug][2]),
        },
    )


# slug -> (encoded json, etag, cell count). Bounded by the number of cities.
_pack_cache: dict[str, tuple[bytes, str, int]] = {}


def _cached_pack(state) -> tuple[bytes, str]:
    slug = state.city.slug
    cached = _pack_cache.get(slug)
    if cached is not None:
        return cached[0], cached[1]

    log.info("building crime pack for %s", slug)
    pack = build_pack(state.crime, state.city)
    encoded = json.dumps(pack, separators=(",", ":")).encode()
    etag = f'W/"{hashlib.sha256(encoded).hexdigest()[:16]}"'
    _pack_cache[slug] = (encoded, etag, pack["totalCells"])
    log.info(
        "%s crime pack: %d cells, %.1f MB",
        slug, pack["totalCells"], len(encoded) / 1e6,
    )
    return encoded, etag


@app.get("/geocode", response_model=GeocodeResponse)
def geocode(
    q: str = Query(..., min_length=1),
    limit: int = Query(12, le=25),
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    city: str | None = Query(None),
):
    """Search for a place by name.

    The local index built from the OSM extract answers first. It is forgiving
    about punctuation and abbreviations in ways an external geocoder is not —
    "madams organ" finds Madam's Organ, "14th st nw" finds 14th Street
    Northwest — and it has no rate limit, so it can answer every keystroke.

    Nominatim is consulted only to top up a thin result set, which is mostly
    house-number addresses that OSM carries as interpolation rather than as
    named objects. Its results are merged into the ranking rather than
    appended to it: appending was the bug behind "801 3rd St NW" returning
    everything except the building, because the local index would return four
    plausible-looking near-misses and push the real answer to fifth.
    """
    selected = _require_city(city)
    state = None
    try:
        state = get_state(selected.slug)
    except (RuntimeError, FileNotFoundError):
        # Search still works through the external geocoder; it is just less
        # forgiving. Better than refusing to search at all.
        pass

    near = (lat, lon) if lat is not None and lon is not None else None
    results: list[GeocodeResult] = []
    seen: set[tuple[int, int]] = set()

    def add(result: GeocodeResult) -> None:
        # Nothing outside the selected city, whatever its source. A result the
        # user cannot route to is not a search result, it is a dead end — and
        # "Union Station" matching Washington's while New York is selected is
        # a genuinely confusing answer rather than a merely unhelpful one.
        if not selected.bbox.contains(result.lat, result.lon):
            return
        key = (int(result.lat * 20000), int(result.lon * 20000))
        if key in seen:
            return
        seen.add(key)
        results.append(result)

    # Without a device location, bias to the city's centre. Otherwise an
    # ambiguous name is resolved by text alone and a match on the far side of
    # the city can outrank the one round the corner.
    bias = near or selected.center

    if state is not None and state.places is not None:
        for hit in state.places.search(q, limit=limit, near=bias):
            add(
                GeocodeResult(
                    name=hit.place.name,
                    address=hit.place.address,
                    lat=hit.place.lat,
                    lon=hit.place.lon,
                )
            )

    # Reach outward when the local index came up short, or when an address
    # query has not produced the exact doorway. OSM's address coverage in DC
    # is good but not complete, and answering "801 3rd St NW" with the street
    # is no answer at all — 3rd Street NW runs for miles.
    query = parse_address(q)
    exact_found = query.is_address and any(
        _matches_address(query, r.name) for r in results
    )

    if len(results) < 4 or (query.is_address and not exact_found):
        for row in _nominatim(q, limit, selected):
            add(row)

    if query.is_address:
        results = _rank_addresses(query, results)

    return GeocodeResponse(results=results[:limit])


def _matches_address(query, name: str) -> bool:
    """Whether ``name`` is the exact doorway the query asked for."""
    candidate = parse_address(name)
    return (
        candidate.house_number == query.house_number
        and street_match(query, candidate) > 0.85
    )


def _rank_addresses(query, results: list[GeocodeResult]) -> list[GeocodeResult]:
    """Re-rank merged results for an address query.

    Runs over local and external results together, so wherever the exact
    address came from it ends up on top. A stable sort keeps each source's own
    ordering intact within a tier.
    """

    def rank(result: GeocodeResult) -> tuple[int, int]:
        candidate = parse_address(result.name)
        street = street_match(query, candidate)
        if street <= 0.0:
            # Not on the street that was asked for. Kept, because Nominatim
            # occasionally names a building rather than its address, but last.
            return (3, 0)
        if candidate.house_number is None:
            return (2, 0)  # the street itself
        delta = abs(candidate.house_number - query.house_number)
        return (0 if delta == 0 else 1, delta)

    return sorted(results, key=rank)


def _nominatim(q: str, limit: int, city: City) -> list[GeocodeResult]:
    """Query the external geocoder, returning nothing if it is unavailable.

    Deliberately non-fatal: local results are usually enough, and a geocoder
    outage should degrade search rather than break it.
    """
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": limit,
        # Bounded to the selected city. Without this, "Union Station" in New
        # York returns Washington's, which is a very confusing answer to get.
        "viewbox": (
            f"{city.bbox.min_lon},{city.bbox.max_lat},"
            f"{city.bbox.max_lon},{city.bbox.min_lat}"
        ),
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
                    name=_nominatim_name(row, display),
                    address=display,
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _nominatim_name(row: dict, display: str) -> str:
    """A usable label for a Nominatim row.

    For a house-number hit Nominatim's own ``name`` is usually the bare number
    or empty, and the first comma-separated chunk of ``display_name`` is just
    as unhelpful. Rebuilding it from the address parts gives "801 3rd Street
    Northwest", which is both readable and parseable by the address ranker.
    """
    address = row.get("address") or {}
    number = address.get("house_number")
    road = address.get("road")
    if number and road:
        return f"{number} {road}"
    return row.get("name") or road or display.split(",")[0]


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


# --------------------------------------------------------------------------
# Transit: stops, departure boards, live vehicles
# --------------------------------------------------------------------------


def _local_now(city: City) -> datetime:
    """Now in a city's local time.

    Timetables are in local time, and comparing a GTFS departure against a UTC
    clock is off by four or five hours depending on the season — which reads
    as the board being empty all evening. Per city rather than fixed, because
    the next city added will not be Eastern.
    """
    return datetime.now(city.timezone)


def _seconds_since_midnight(when: datetime) -> int:
    return when.hour * 3600 + when.minute * 60 + when.second


def _clock(seconds: int) -> str:
    total = int(seconds) % 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}"


def _require_transit(slug: str | None = None):
    state = _require_state(slug)
    if state.transit is None:
        city = state.city
        feeds = ", ".join(f.name for f in city.transit_feeds) or "none configured"
        raise HTTPException(
            status_code=503,
            detail=(
                f"No transit timetable for {city.name}. Download its feeds "
                f"and rebuild:\n"
                f"    make gtfs CITY={city.slug}\n"
                f"    make graph CITY={city.slug}\n"
                f"Feeds for this city: {feeds}."
            ),
        )
    return state


@app.get("/transit/stops", response_model=TransitStopsResponse)
def transit_stops(
    minLat: float = Query(...),
    minLon: float = Query(...),
    maxLat: float = Query(...),
    maxLon: float = Query(...),
    railOnly: bool = Query(False),
    limit: int = Query(400, le=800),
    city: str | None = Query(None),
) -> TransitStopsResponse:
    """Metro and bus stops in a viewport.

    Capped, and the cap is reported rather than hidden: DC has roughly eleven
    thousand bus stops, and a client that silently receives four hundred of
    them has no way to tell the user that is what happened.
    """
    state = _require_transit(city)
    found, truncated = stops_in_bbox(
        state.transit.network,
        minLat, minLon, maxLat, maxLon,
        limit=limit,
        rail_only=railOnly,
    )
    return TransitStopsResponse(
        stops=[
            TransitStopModel(
                id=s.id,
                name=s.name or "Stop",
                lat=round(s.lat, 6),
                lon=round(s.lon, 6),
                mode=s.mode,
                routes=s.routes[:12],
            )
            for s in found
        ],
        total=len(found),
        truncated=truncated,
    )


@app.get("/transit/stop/{stop_id}/board", response_model=StopBoardResponse)
def stop_board(
    stop_id: str,
    limit: int = Query(15, le=40),
    city: str | None = Query(None),
) -> StopBoardResponse:
    """The next departures from a stop.

    Scheduled times come from the loaded GTFS feed and are always available.
    Live predictions are folded in on top where WMATA has them, which is the
    next half hour or so and only while service is running.
    """
    state = _require_transit(city)
    network = state.transit.network

    indices = resolve_stop(network, stop_id)
    if not indices:
        raise HTTPException(status_code=404, detail="No such stop.")

    now = _local_now(state.city)
    now_s = _seconds_since_midnight(now)
    scheduled = departures(network, stop_id, after_s=now_s, limit=limit)

    stop = network.stops[indices[0]]
    mode = scheduled[0].mode if scheduled else "bus"

    live_note = ""
    if not state.live.enabled:
        # Each provider phrases its own reason: a missing WMATA key and a
        # missing protobuf dependency need different things done about them.
        live_note = state.live.disabled_reason
    else:
        predictions = _predictions_for(state, network, indices, mode)
        if predictions:
            scheduled = attach_live(scheduled, predictions, now_s)
        else:
            live_note = state.live.disabled_reason

    return StopBoardResponse(
        stopId=stop_id,
        stopName=stop.name or "Stop",
        mode=mode,
        departures=[
            DepartureModel(
                routeName=d.route_name,
                headsign=d.headsign,
                mode=d.mode,
                scheduledTime=_clock(d.departure_s),
                scheduledMinutes=int(round((d.departure_s - now_s) / 60.0)),
                liveMinutes=d.live_minutes,
                patternId=d.pattern_id,
                tripId=d.trip_id,
                stopsRemaining=d.stops_remaining,
                vehicleId=d.vehicle_id,
            )
            for d in scheduled
        ],
        live=any(d.is_live for d in scheduled),
        liveNote=live_note,
    )


def _predictions_for(state, network, indices: list[int], mode: str) -> list:
    """Live predictions for a stop, from whichever WMATA feed applies."""
    if mode in RAIL_MODES:
        codes = [
            code
            for i in indices
            if (code := station_code(network.stops[i].stop_id))
        ]
        return state.live.rail_predictions(codes)

    out: list = []
    for i in indices:
        out.extend(state.live.bus_predictions(network.stops[i].stop_id))
    return out


@app.get("/transit/trip/{pattern_id}/{trip_id}", response_model=TripDetailResponse)
def transit_trip(
    pattern_id: int,
    trip_id: str,
    fromStop: str = Query("", description="Stop the user is waiting at"),
    vehicleId: str = Query("", description="Live vehicle id from the board"),
    city: str | None = Query(None),
) -> TripDetailResponse:
    """A vehicle's whole journey: every call, its time, and where it is now.

    Position handling differs by mode because the upstream data does. A bus
    reports its coordinates, so it is drawn where it is and its schedule
    deviation is applied to every remaining call. A train reports a track
    circuit, which cannot be turned into a coordinate without a separate feed,
    so its position is interpolated from the next station's prediction and
    flagged as an estimate everywhere it appears.
    """
    state = _require_transit(city)
    network = state.transit.network

    vehicle = state.live.bus_position_for_trip(trip_id) if state.live.enabled else None
    detail = trip_detail(
        network,
        pattern_id,
        trip_id,
        deviation_s=vehicle.deviation_s if vehicle else 0.0,
        vehicle_lat=vehicle.lat if vehicle else None,
        vehicle_lon=vehicle.lon if vehicle else None,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="No such trip.")

    now_s = _seconds_since_midnight(_local_now(state.city))
    position: VehiclePosition | None = None
    note = ""

    if vehicle is not None:
        position = VehiclePosition(
            lat=round(vehicle.lat, 6), lon=round(vehicle.lon, 6), estimated=False
        )
    elif detail.mode in RAIL_MODES and state.live.enabled:
        estimate = _estimate_train(state, detail, vehicleId, fromStop)
        if estimate is not None:
            position = VehiclePosition(
                lat=round(estimate[0], 6), lon=round(estimate[1], 6), estimated=True
            )
            note = (
                "Train position is estimated from the next station's arrival "
                "time — WMATA does not publish train coordinates."
            )
    if position is None and not note:
        note = (
            "No live position for this vehicle; times shown are scheduled."
            if state.live.enabled
            else state.live.disabled_reason
        )

    return TripDetailResponse(
        patternId=detail.pattern_id,
        tripId=detail.trip_id,
        routeName=detail.route_name,
        headsign=detail.headsign,
        mode=detail.mode,
        stops=[
            TripStopModel(
                stopId=s.stop_id,
                name=s.name or "Stop",
                lat=round(s.lat, 6),
                lon=round(s.lon, 6),
                arrivalTime=_clock(s.arrival_s),
                minutes=int(round((s.arrival_s - now_s) / 60.0)),
                passed=s.passed,
            )
            for s in detail.stops
        ],
        polyline=_flatten_pairs([(s.lat, s.lon) for s in detail.stops]),
        deviationSeconds=round(detail.deviation_s, 1),
        vehicle=position,
        liveNote=note,
    )


def _estimate_train(state, detail, vehicle_id: str, from_stop: str):
    """Interpolate a train's position from where it is next predicted.

    Identifying *which* train is the hard part, and it is worth being precise
    about how it is done rather than hand-waving it.

    WMATA's rail predictions carry a ``TrainId``. If the departure board
    matched one to the departure the user tapped, that id is passed back here
    and the train is located by scanning predictions across every station on
    the route: the station reporting the smallest number of minutes for that
    id is the one it is heading to next, which places it on the leg before.

    Without an id there is nothing to trace, and guessing would put a dot on
    the map that means nothing. In that case no position is returned and the
    client says so.
    """
    codes: dict[str, int] = {}
    for i, stop in enumerate(detail.stops):
        code = station_code(stop.stop_id)
        if code and code not in codes:
            codes[code] = i
    if not codes:
        return None

    predictions = state.live.rail_predictions(list(codes))
    if not predictions:
        return None

    # The endpoint returns predictions for all the stations asked about, but
    # not which station each belongs to, so re-query per station only when an
    # id has to be traced. One call per station is too many; instead, use the
    # station the user is standing at as the anchor when there is no id.
    if not vehicle_id:
        anchor = codes.get(station_code(from_stop)) if from_stop else None
        if anchor is None or anchor == 0:
            return None
        soonest = min(
            (p for p in predictions if _same_line(p, detail)),
            key=lambda p: p.minutes,
            default=None,
        )
        if soonest is None:
            return None
        return estimate_rail_position(detail, anchor, float(soonest.minutes))

    best: tuple[int, float] | None = None
    for code, index in codes.items():
        if index == 0:
            continue
        for prediction in state.live.rail_predictions([code]):
            if prediction.vehicle_id != vehicle_id:
                continue
            if best is None or prediction.minutes < best[1]:
                best = (index, float(prediction.minutes))
    if best is None:
        return None
    return estimate_rail_position(detail, best[0], best[1])


def _same_line(prediction, detail) -> bool:
    """Whether a rail prediction plausibly belongs to this trip.

    Line code against route name, loosely. WMATA reports "RD" where the feed
    says "Red", so this compares on leading letters rather than demanding
    equality, and errs towards accepting: a wrong match moves an estimated
    dot, while no match removes the feature.
    """
    line = (prediction.route_name or "").strip().upper()
    route = (detail.route_name or "").strip().upper()
    if not line or not route:
        return True
    return line[:2] == route[:2] or route.startswith(line) or line.startswith(route[:2])


# --------------------------------------------------------------------------
# Live ETA sharing
#
# The one part of this service that handles a live human location, so the
# rules are worth stating where they are enforced rather than only in the
# module that implements them:
#
#   * The link is the credential. Nothing else identifies a recipient, so the
#     token is 128 bits of randomness and the page says who can see it.
#   * Reading and writing are separate. Creating a share returns an owner key
#     that never appears in the shared link; without it the link watches and
#     nothing more.
#   * Nothing is persisted, and only the current position is held — a
#     recipient sees where someone is, never where they have been.
#   * It ends on arrival, on a hard ceiling, and on silence, because the case
#     that matters is the walker who forgets to stop it.
# --------------------------------------------------------------------------


def _share_url(request: Request, token: str) -> str:
    """The link to hand to a friend.

    Prefers the configured public origin. Falling back to the request's own
    host is right when the service is exposed directly and wrong the moment it
    is behind a proxy: the scheme and host are then the proxy's internal ones,
    so the link comes out as ``http://app:8000/s/...`` — unreachable, and
    downgraded from HTTPS for a URL that carries a live location.

    Running uvicorn with ``--proxy-headers`` fixes the fallback for a
    well-configured proxy, but ``GETMEHOME_PUBLIC_URL`` removes the guesswork
    entirely and is what the deployment docs use.
    """
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/s/{token}"


@app.post("/share", response_model=ShareCreatedResponse)
def share_create(body: ShareCreateRequest, request: Request) -> ShareCreatedResponse:
    """Begin sharing a walk. Returns the watch link and the write key."""
    session = get_store().create(body.destinationName)
    return ShareCreatedResponse(
        token=session.token,
        url=_share_url(request, session.token),
        ownerKey=session.owner_key,
        expiresInSeconds=session.expires_at - session.created_at,
    )


@app.post("/share/{token}/update", response_model=ShareStatusResponse)
def share_update(token: str, body: ShareUpdateRequest) -> ShareStatusResponse:
    """Move the dot. Requires the owner key issued at creation."""
    session = get_store().update(
        token,
        body.ownerKey,
        lat=body.lat,
        lon=body.lon,
        eta_s=body.etaSeconds,
        remaining_m=body.remainingMetres,
    )
    if session is None:
        # One message for a bad token and a bad key alike: distinguishing them
        # would confirm that a guessed token exists.
        raise HTTPException(status_code=404, detail="No such share.")
    return _share_status(session)


@app.post("/share/{token}/end", response_model=ShareStatusResponse)
def share_end(token: str, body: ShareFinishRequest) -> ShareStatusResponse:
    """End a share, on arrival or because the walker stopped it."""
    session = get_store().finish(token, body.ownerKey, arrived=body.arrived)
    if session is None:
        raise HTTPException(status_code=404, detail="No such share.")
    return _share_status(session)


@app.get("/share/{token}", response_model=ShareStatusResponse)
def share_status(token: str) -> ShareStatusResponse:
    """What the recipient is allowed to see."""
    session = get_store().get(token)
    if session is None:
        raise HTTPException(status_code=404, detail="This link has expired.")
    return _share_status(session)


@app.get("/s/{token}", response_class=HTMLResponse)
def share_page(token: str) -> HTMLResponse:
    """The page a recipient opens.

    Served even for an unknown token, and the page reports the expiry itself.
    Returning 404 here would let anyone probing tokens tell a live share from
    a dead one by the status code alone.
    """
    from ..sharing import recipient_page  # noqa: PLC0415 — page template only

    return HTMLResponse(
        recipient_page(token),
        headers={
            # A live location has no business in a cache or a search index.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


def _share_status(session) -> ShareStatusResponse:
    view = view_of(session)
    return ShareStatusResponse(
        status=view.status,
        destinationName=view.destination_name,
        lat=view.lat,
        lon=view.lon,
        etaSeconds=view.eta_s,
        remainingMetres=view.remaining_m,
        updatedAgoSeconds=round(view.updated_ago_s, 1),
        expiresInSeconds=round(view.expires_in_s, 1),
        pollAfterSeconds=max(MIN_POLL_INTERVAL_S, 10),
    )
