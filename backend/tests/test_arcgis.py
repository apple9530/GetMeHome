"""Tests for the ArcGIS client and the DDOT/MPD ingesters.

Driven by a stub server that reproduces the shapes DC's MapServer actually
returns, including the failure that broke the first real build: a second layer
matching on name that rejects the query outright.
"""

from __future__ import annotations

import httpx
import pytest

from getmehome.ingest.arcgis import ArcGisClient, ArcGisError, _esri_to_geojson
from getmehome.ingest.crime import fetch_crime
from getmehome.ingest.streetlights import fetch_streetlights

SERVICE = "https://example.test/arcgis/rest/services/DDOT/Streetlights/MapServer"


def make_client(handler) -> ArcGisClient:
    client = ArcGisClient(SERVICE)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def point_feature(i: int) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-77.03 + i * 1e-4, 38.90]},
        "properties": {
            "OBJECTID": i,
            "LAMPTYPE": "LED",
            "WATTAGE": 100,
            "LIGHTHEIGHT": 26,  # feet
            "STATUS": "Active",
        },
    }


# ---------------------------------------------------------------------------
# The regression: a name-matching layer that cannot be queried
# ---------------------------------------------------------------------------


def ddot_handler(total: int = 2500):
    """DDOT's service: layer 0 is real, layer 1 rejects queries.

    This is what the first real build hit — layer 0 returned 72k lamps, then
    layer 1 answered HTTP 200 with an Esri error body and the whole build
    aborted, discarding everything already fetched.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)

        if path.endswith("/MapServer"):
            return httpx.Response(200, json={"layers": [
                {"id": 0, "name": "Street Lights"},
                {"id": 1, "name": "Street Light Poles"},
            ]})

        if path.endswith("/MapServer/0"):
            return httpx.Response(200, json={
                "type": "Feature Layer",
                "geometryType": "esriGeometryPoint",
                "capabilities": "Map,Query,Data",
                "fields": [
                    {"name": "OBJECTID"}, {"name": "LAMPTYPE"},
                    {"name": "WATTAGE"}, {"name": "LIGHTHEIGHT"},
                    {"name": "STATUS"},
                ],
            })

        if path.endswith("/MapServer/1"):
            return httpx.Response(200, json={
                "type": "Feature Layer",
                "geometryType": "esriGeometryPolygon",
                "capabilities": "Map,Query",
                "fields": [{"name": "OBJECTID"}],
            })

        if path.endswith("/MapServer/0/query"):
            offset = int(params.get("resultOffset", 0))
            size = int(params.get("resultRecordCount", 1000))
            page = [point_feature(i) for i in range(offset, min(offset + size, total))]
            return httpx.Response(200, json={
                "features": page,
                "exceededTransferLimit": offset + len(page) < total,
            })

        if path.endswith("/MapServer/1/query"):
            # HTTP 200 with an error body — exactly how Esri reports this.
            return httpx.Response(200, json={"error": {
                "code": 400,
                "message": "Invalid or missing input parameters.",
                "details": [],
            }})

        return httpx.Response(404, json={"error": {"message": "not found"}})

    return handler


def test_unqueryable_layer_is_filtered_out():
    """A non-point layer matching on name must never be queried."""
    with make_client(ddot_handler()) as client:
        candidates = client.find_layers("light")
        assert len(candidates) == 2  # both match on name

        usable = client.queryable_point_layers(candidates)
        assert [leg["id"] for leg in usable] == [0]


def test_fetch_streetlights_succeeds_despite_bad_layer(monkeypatch):
    """The build that previously aborted must now return every real lamp."""
    import getmehome.ingest.streetlights as module

    monkeypatch.setattr(module, "ArcGisClient", lambda url: make_client(ddot_handler()))
    lights = fetch_streetlights(SERVICE)

    assert len(lights) == 2500
    # Height was 26 feet; it must be converted, not taken as 26 metres.
    assert 7.5 < lights[0].height_m < 8.5
    assert lights[0].lumens > 0


def test_bad_layer_still_fails_when_nothing_else_works(monkeypatch):
    """Skipping bad layers must not turn a total failure into silent success."""
    def all_bad(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/MapServer"):
            return httpx.Response(200, json={"layers": [{"id": 0, "name": "Street Lights"}]})
        if request.url.path.endswith("/MapServer/0"):
            return httpx.Response(200, json={
                "type": "Feature Layer",
                "geometryType": "esriGeometryPoint",
                "capabilities": "Map,Query",
                "fields": [{"name": "OBJECTID"}],
            })
        return httpx.Response(200, json={"error": {"code": 400, "message": "nope"}})

    import getmehome.ingest.streetlights as module

    monkeypatch.setattr(module, "ArcGisClient", lambda url: make_client(all_bad))
    with pytest.raises(RuntimeError, match="no streetlights"):
        fetch_streetlights(SERVICE)


# ---------------------------------------------------------------------------
# Paging and format negotiation
# ---------------------------------------------------------------------------


def test_paging_collects_every_page():
    with make_client(ddot_handler(total=2500)) as client:
        features = list(client.iter_features(0, page_size=1000))
    assert len(features) == 2500


def test_falls_back_to_esri_json_when_geojson_unsupported():
    """Some DC layers reject f=geojson; the client must negotiate down."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        path = request.url.path
        if path.endswith("/MapServer/0/query"):
            calls.append(params.get("f", ""))
            if params.get("f") == "geojson":
                return httpx.Response(200, json={"error": {
                    "code": 400, "message": "Invalid or missing input parameters."
                }})
            offset = int(params.get("resultOffset", 0))
            if offset > 0:
                return httpx.Response(200, json={"features": []})
            return httpx.Response(200, json={"features": [
                {
                    "attributes": {"OBJECTID": 1, "LAMPTYPE": "HPS"},
                    "geometry": {"x": -77.03, "y": 38.90},
                }
            ]})
        return httpx.Response(404, json={})

    with make_client(handler) as client:
        features = list(client.iter_features(0))

    assert calls[:2] == ["geojson", "json"], "must try geojson first, then json"
    assert len(features) == 1
    # Converted into the GeoJSON shape the callers expect.
    assert features[0]["properties"]["LAMPTYPE"] == "HPS"
    assert features[0]["geometry"]["coordinates"] == [-77.03, 38.90]


def test_esri_conversion_handles_null_geometry():
    converted = _esri_to_geojson({"attributes": {"A": 1}, "geometry": {}})
    assert converted["geometry"] is None
    assert converted["properties"] == {"A": 1}


def test_error_body_on_http_200_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "bad"}})

    with make_client(handler) as client, pytest.raises(ArcGisError):
        client.layers()


# ---------------------------------------------------------------------------
# Crime
# ---------------------------------------------------------------------------


def test_crime_skips_bad_year_layer_and_dedupes(monkeypatch):
    """One failing year must not lose the others, and CCNs must not double."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)

        if path.endswith("/MapServer"):
            return httpx.Response(200, json={"layers": [
                {"id": 0, "name": "Crime Incidents in 2026"},
                {"id": 1, "name": "Crime Incidents in 2025"},
                {"id": 8, "name": "Crime Incidents in the Last 30 Days"},
            ]})

        if path.rstrip("/").split("/")[-1] in {"0", "1", "8"}:
            return httpx.Response(200, json={
                "type": "Feature Layer",
                "geometryType": "esriGeometryPoint",
                "capabilities": "Map,Query",
                "fields": [
                    {"name": "CCN"}, {"name": "OFFENSE"}, {"name": "METHOD"},
                    {"name": "SHIFT"}, {"name": "REPORT_DAT"},
                ],
            })

        if path.endswith("/1/query"):
            return httpx.Response(200, json={"error": {
                "code": 400, "message": "Invalid or missing input parameters."
            }})

        if path.endswith("/query"):
            if int(params.get("resultOffset", 0)) > 0:
                return httpx.Response(200, json={"features": []})
            # Layer 8 repeats layer 0's CCN — the 30-day layer overlaps the
            # current year, which is why deduplication exists.
            return httpx.Response(200, json={"features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-77.03, 38.90]},
                "properties": {
                    "CCN": "24001234",
                    "OFFENSE": "ROBBERY",
                    "METHOD": "GUN",
                    "SHIFT": "MIDNIGHT",
                    "REPORT_DAT": "2026-01-15T03:20:00Z",
                },
            }]})

        return httpx.Response(404, json={})

    import getmehome.ingest.crime as module

    monkeypatch.setattr(module, "ArcGisClient", lambda url: make_client(handler))
    incidents = fetch_crime(SERVICE, years=2)

    # Layer 1 failed; layers 0 and 8 returned the same CCN, so one incident.
    assert len(incidents) == 1
    assert incidents[0].offense == "ROBBERY"
    assert incidents[0].method == "GUN"
    assert incidents[0].reported_at.year == 2026
