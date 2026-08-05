"""End-to-end API tests against an injected fixture graph."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from getmehome.api.main import app
from getmehome.api.state import AppState, set_state
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
    assert "not near" in r.json()["detail"]


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
