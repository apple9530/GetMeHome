"""End-to-end API tests against an injected fixture graph."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from getmehome.api.main import app
from getmehome.api.state import AppState, set_state
from getmehome.cities import DC
from getmehome.routing.astar import GraphIndex
from getmehome.safety.hexgrid import CrimeIndex
from getmehome.safety.scoring import apply_scores

from .fixtures import (
    build_grid_graph,
    camera_facing,
    crime_cluster_at,
    dense_lights_along_column,
    grid_coords,
)


@pytest.fixture(scope="module")
def client():
    graph = build_grid_graph(7, 7)
    incidents = []
    for r in range(1, 6):
        incidents.extend(crime_cluster_at(r, 3, count=60))
    cams = [camera_facing(r, 5, direction_deg=90.0) for r in range(1, 6)]

    apply_scores(
        graph,
        lights=dense_lights_along_column(1),
        incidents=incidents,
        cameras=cams,
    )
    def build_state():
        return AppState(
            city=DC,
            graph=graph,
            index=GraphIndex(graph),
            cameras=cams,
            transit=None,
            crime=CrimeIndex.from_incidents(incidents),
        )

    set_state(build_state())
    # The lifespan hook tries to load a real graph from disk and clears state
    # when it cannot find one, so re-inject after the client starts.
    with TestClient(app, raise_server_exceptions=True) as c:
        set_state(build_state())
        yield c


def _route_body(**overrides):
    body = {
        "origin": dict(zip(("lat", "lon"), grid_coords(0, 0), strict=True)),
        "destination": dict(zip(("lat", "lon"), grid_coords(6, 6), strict=True)),
        "destinationName": "the far corner",
        "modes": ["walk"],
        "forceNight": True,
    }
    body.update(overrides)
    return body


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_meta(client):
    r = client.get("/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["segments"] == 84
    assert body["cameras"] == 5


def test_route_returns_itineraries(client):
    r = client.post("/route", json=_route_body())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["isNight"] is True
    assert body["itineraries"]

    for it in body["itineraries"]:
        assert it["kind"] == "walk"
        assert it["duration"] > 0
        assert 0 <= it["safety"]["overall"] <= 100
        assert it["label"]
        assert it["legs"]

        leg = it["legs"][0]
        # Flat [lat, lon, ...] pairs.
        assert len(leg["polyline"]) >= 4
        assert len(leg["polyline"]) % 2 == 0
        assert leg["steps"]
        assert leg["steps"][0]["maneuver"] == "depart"
        assert leg["steps"][-1]["maneuver"] == "arrive"
        assert "the far corner" in leg["steps"][-1]["instruction"]


def test_route_options_are_ranked_and_explained(client):
    body = client.post("/route", json=_route_body()).json()
    durations = [i["duration"] for i in body["itineraries"]]
    assert durations == sorted(durations)
    for it in body["itineraries"]:
        assert it["summary"], "every option needs a plain-language tradeoff line"


def test_night_and_day_can_differ(client):
    night = client.post("/route", json=_route_body(forceNight=True)).json()
    day = client.post("/route", json=_route_body(forceNight=False)).json()
    assert night["isNight"] is True
    assert day["isNight"] is False
    # Lighting only counts at night.
    assert day["itineraries"][0]["safety"]["lighting"] == 100


def test_avoid_cameras_flag(client):
    plain = client.post("/route", json=_route_body()).json()
    avoided = client.post("/route", json=_route_body(avoidCameras=True)).json()

    worst_plain = max(i["cameras"]["camerasPassed"] for i in plain["itineraries"])
    worst_avoided = max(i["cameras"]["camerasPassed"] for i in avoided["itineraries"])
    assert worst_avoided <= worst_plain


def test_route_rejects_far_away_origin(client):
    r = client.post("/route", json=_route_body(origin={"lat": 39.29, "lon": -76.61}))
    assert r.status_code == 422
    detail = r.json()["detail"]
    # The message names the city, because "no walkable street near there" is
    # a poor way to say "you have the wrong city selected".
    assert "Washington" in detail


def test_route_validates_input(client):
    r = client.post("/route", json=_route_body(modes=[]))
    assert r.status_code == 422

    r = client.post("/route", json=_route_body(origin={"lat": 200.0, "lon": 0.0}))
    assert r.status_code == 422


def test_transit_requested_without_data_gives_notice(client):
    body = client.post("/route", json=_route_body(modes=["transit"])).json()
    assert body["notices"]
    assert "Transit data is unavailable" in body["notices"][0]
    # It must still return something usable rather than failing.
    assert body["itineraries"]


def test_cameras_overlay(client):
    r = client.get(
        "/cameras",
        params={"minLat": 38.85, "minLon": -77.10, "maxLat": 38.99, "maxLon": -76.95},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    for cam in body["cameras"]:
        assert cam["direction"] == 90.0
        assert cam["operator"] == "Flock Safety"


def test_cameras_overlay_respects_bbox(client):
    r = client.get(
        "/cameras",
        params={"minLat": 0.0, "minLon": 0.0, "maxLat": 1.0, "maxLon": 1.0},
    )
    assert r.json()["total"] == 0


def test_crime_grid(client):
    r = client.get(
        "/crime/grid",
        params={
            "minLat": 38.895,
            "minLon": -77.040,
            "maxLat": 38.912,
            "maxLon": -77.020,
        },
    )
    assert r.status_code == 200
    body = r.json()

    assert body["cells"], "the fixture's crime cluster should produce cells"
    assert body["radius"] > 0
    assert body["totalIncidents"] > 0

    for cell in body["cells"]:
        # Six corners, as flat lat/lon pairs.
        assert len(cell["vertices"]) == 12
        assert cell["total"] > 0
        assert 0.0 <= cell["intensity"] <= 1.0
        assert 0.0 <= cell["nightShare"] <= 1.0
        assert cell["byOffense"]
        assert sum(o["count"] for o in cell["byOffense"]) <= cell["total"]
        assert cell["id"]

    # Sorted busiest-first, and the busiest is normalised to 1.
    intensities = [c["intensity"] for c in body["cells"]]
    assert intensities == sorted(intensities, reverse=True)
    assert intensities[0] == pytest.approx(1.0)


def test_crime_grid_night_filter(client):
    params = {
        "minLat": 38.895, "minLon": -77.040,
        "maxLat": 38.912, "maxLon": -77.020,
    }
    everything = client.get("/crime/grid", params=params).json()
    nights = client.get("/crime/grid", params={**params, "nightOnly": "true"}).json()

    assert nights["nightOnly"] is True
    assert nights["totalIncidents"] <= everything["totalIncidents"]


def test_crime_grid_adapts_cell_size_to_zoom(client):
    """Zooming out must return bigger cells, not more of them."""
    tight = client.get("/crime/grid", params={
        "minLat": 38.900, "minLon": -77.035,
        "maxLat": 38.906, "maxLon": -77.027,
    }).json()
    wide = client.get("/crime/grid", params={
        "minLat": 38.79, "minLon": -77.13,
        "maxLat": 39.01, "maxLon": -76.90,
    }).json()

    assert wide["radius"] > tight["radius"]
    assert len(wide["cells"]) <= 700


def test_crime_grid_outside_dc_is_empty(client):
    r = client.get("/crime/grid", params={
        "minLat": 0.0, "minLon": 0.0, "maxLat": 0.5, "maxLon": 0.5,
    })
    assert r.status_code == 200
    assert r.json()["cells"] == []


def test_step_voice_and_display_text_differ_usefully(client):
    body = client.post("/route", json=_route_body()).json()
    steps = body["itineraries"][0]["legs"][0]["steps"]

    for step in steps[:-1]:
        assert step["instruction"]
        assert step["voice"]
    # Distances are folded into the display text but not the spoken text.
    with_distance = [s for s in steps if " for " in s["instruction"]]
    assert with_distance
    assert all(" for " not in s["voice"] for s in with_distance)


# ---------------------------------------------------------------------------
# Crime lookback windows
# ---------------------------------------------------------------------------


def test_meta_advertises_the_available_windows(client):
    body = client.get("/meta").json()
    assert body["crimeWindows"] == [30, 60, 180, 365]
    assert body["defaultCrimeWindow"] in body["crimeWindows"]


def test_route_echoes_the_window_it_used(client):
    body = client.post("/route", json=_route_body(crimeWindowDays=30)).json()
    assert body["crimeWindowDays"] == 30


def test_an_unbuilt_window_snaps_and_says_so(client):
    body = client.post("/route", json=_route_body(crimeWindowDays=90)).json()
    assert body["crimeWindowDays"] == 60
    assert any("60 days" in n for n in body["notices"])


def test_omitting_the_window_uses_the_default(client):
    body = client.post("/route", json=_route_body()).json()
    assert body["crimeWindowDays"] == 365
    assert not any("window" in n.lower() for n in body["notices"])


def test_crime_grid_accepts_a_window(client):
    params = {
        "minLat": 38.895,
        "minLon": -77.035,
        "maxLat": 38.906,
        "maxLon": -77.022,
        "windowDays": 30,
    }
    narrow = client.get("/crime/grid", params=params).json()
    everything = client.get(
        "/crime/grid", params={k: v for k, v in params.items() if k != "windowDays"}
    ).json()

    assert narrow["windowDays"] == 30
    assert everything["windowDays"] == 0
    # The fixture spreads incidents back over most of a year, so a 30-day view
    # must be a strict subset.
    assert narrow["totalIncidents"] < everything["totalIncidents"]


# ---------------------------------------------------------------------------
# Address geocoding
# ---------------------------------------------------------------------------


def test_external_results_are_merged_into_the_ranking_not_appended(client, monkeypatch):
    """The bug behind "801 3rd St NW": the right answer arrived fifth.

    The local index returns several plausible near-misses, so appending
    Nominatim's results left the exact address below all of them. It has to be
    ranked against them, not after them.
    """
    from getmehome.api import main
    from getmehome.api.schemas import GeocodeResult
    from getmehome.places import Place, PlaceIndex

    local = PlaceIndex(
        places=[
            Place("3rd Street Northwest", 38.9000, -77.0160, "street", ""),
            Place("799 3rd Street Northwest", 38.8991, -77.0158, "address", ""),
            Place("803 3rd Street Northwest", 38.8994, -77.0158, "address", ""),
            Place("1401 3rd Street Northwest", 38.9090, -77.0158, "address", ""),
        ]
    ).build()

    state = main.get_state()
    monkeypatch.setattr(state, "places", local, raising=False)
    monkeypatch.setattr(
        main,
        "_nominatim",
        lambda q, limit, city: [
            GeocodeResult(
                name="801 3rd Street Northwest",
                address="801 3rd Street Northwest, Washington, DC",
                lat=38.8993,
                lon=-77.0158,
            )
        ],
    )

    body = main.geocode(q="801 3rd St NW", limit=12, lat=None, lon=None, city=None)
    assert body.results[0].name == "801 3rd Street Northwest"


def test_a_nominatim_house_number_row_gets_a_readable_name():
    """Nominatim's own `name` for a doorway is the bare number or nothing."""
    from getmehome.api.main import _nominatim_name

    row = {
        "name": "",
        "address": {"house_number": "801", "road": "3rd Street Northwest"},
    }
    assert _nominatim_name(row, "801, 3rd Street Northwest, Washington") == (
        "801 3rd Street Northwest"
    )

    # No house number: fall back to whatever the row does carry.
    assert _nominatim_name({"name": "Madam's Organ"}, "Madam's Organ, 18th St") == (
        "Madam's Organ"
    )


# ---------------------------------------------------------------------------
# Cities
# ---------------------------------------------------------------------------


def test_the_city_list_includes_unbuilt_cities(client):
    """A configured-but-unbuilt city is a deployment state worth showing.

    Hiding it would make a missing build look like a city the app has never
    heard of, which sends someone looking in the wrong place.
    """
    body = client.get("/cities").json()
    slugs = {c["slug"] for c in body["cities"]}
    assert slugs == {"dc", "nyc"}
    assert body["defaultCity"] == "dc"

    by_slug = {c["slug"]: c for c in body["cities"]}
    # NYC has no build in the test environment.
    assert by_slug["nyc"]["available"] is False
    assert by_slug["nyc"]["name"] == "New York"
    assert by_slug["dc"]["bbox"] == [38.78, -77.14, 39.01, -76.89]


def test_an_unknown_city_is_rejected_rather_than_substituted(client):
    """Serving another city's graph would produce confident nonsense."""
    response = client.get("/meta", params={"city": "boston"})
    assert response.status_code == 404
    assert "boston" in response.json()["detail"]


def test_a_configured_but_unbuilt_city_says_how_to_build_it(client):
    response = client.get("/meta", params={"city": "nyc"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "New York" in detail
    assert "--city nyc" in detail


def test_meta_names_the_city_it_answered_for(client):
    body = client.get("/meta", params={"city": "dc"}).json()
    assert body["city"] == "dc"
    assert body["cityName"] == "Washington"


def test_a_route_echoes_the_city_it_used(client):
    body = client.post("/route", json=_route_body()).json()
    assert body["city"] == "dc"


def test_a_point_in_another_city_says_so(client):
    """The commonest cause of an unsnappable point is the wrong city.

    "No walkable street near there" is a poor way to say "you are looking at
    the wrong map", and it sends people to check their GPS instead.
    """
    body = _route_body()
    # Times Square.
    body["origin"] = {"lat": 40.7580, "lon": -73.9855}
    response = client.post("/route", json=body)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "New York" in detail
    assert "Washington" in detail


def test_a_point_in_no_known_city_says_that_instead(client):
    body = _route_body()
    body["origin"] = {"lat": 51.5074, "lon": -0.1278}  # London
    response = client.post("/route", json=body)

    assert response.status_code == 422
    assert "outside the Washington map" in response.json()["detail"]


def test_health_reports_which_cities_are_built(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["city"] == "dc"
