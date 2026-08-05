"""A small client for DC's ArcGIS REST services.

Open Data DC serves everything through Esri MapServer/FeatureServer
endpoints. Two things about them shape this client:

* Layer numbering is not stable. MPD renumbers its crime layers when the year
  rolls over, and hardcoding an index produces a build that silently ingests
  the wrong year. So layers are discovered by name at runtime.
* Queries are capped server-side (usually 1000-2000 features). Every fetch has
  to page, and ``exceededTransferLimit`` is the only reliable signal that
  there is more to come.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx

DEFAULT_TIMEOUT = 120.0
PAGE_SIZE = 1000
MAX_RETRIES = 4


class ArcGisError(RuntimeError):
    pass


class ArcGisClient:
    def __init__(self, service_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.service_url = service_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "GetMeHome/1.0"}
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArcGisClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, url: str, params: dict) -> dict:
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and "error" in payload:
                    raise ArcGisError(str(payload["error"]))
                return payload
            except httpx.HTTPStatusError as exc:
                # A 4xx means the request itself is wrong, so retrying just
                # sleeps for fifteen seconds and fails again. 429 is the
                # exception: that one is asking us to slow down.
                status = exc.response.status_code
                if 400 <= status < 500 and status != 429:
                    raise ArcGisError(f"{url} returned {status}") from exc
                last = exc
            except (httpx.HTTPError, ValueError) as exc:
                # Timeouts and truncated bodies are worth retrying; DC's
                # servers do throttle under a full-history crime pull.
                last = exc

            if attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)

        raise ArcGisError(f"{url} failed after {MAX_RETRIES} attempts: {last}")

    def layers(self) -> list[dict]:
        """All layers in the service, with ids and names."""
        payload = self._get(self.service_url, {"f": "json"})
        return payload.get("layers", []) or []

    def find_layers(self, *needles: str) -> list[dict]:
        """Layers whose name contains any of ``needles`` (case-insensitive)."""
        wanted = [n.lower() for n in needles]
        return [
            layer
            for layer in self.layers()
            if any(n in layer.get("name", "").lower() for n in wanted)
        ]

    def layer_info(self, layer_id: int) -> dict:
        """Full metadata for a layer."""
        return self._get(f"{self.service_url}/{layer_id}", {"f": "json"})

    def layer_fields(self, layer_id: int) -> list[str]:
        """Field names on a layer.

        Used to adapt to schema drift rather than assuming a column exists.
        """
        payload = self.layer_info(layer_id)
        return [f.get("name", "") for f in payload.get("fields", []) or []]

    def queryable_point_layers(self, candidates: list[dict]) -> list[dict]:
        """Narrow a candidate list to layers we can actually query for points.

        Matching on layer name alone is not enough. A MapServer typically
        carries group layers, annotation layers and non-point feature classes
        whose names contain the same words as the one real data layer, and
        querying those returns "Invalid or missing input parameters" rather
        than anything useful.
        """
        usable: list[dict] = []
        for layer in candidates:
            layer_id = layer.get("id")
            if layer_id is None:
                continue
            # A group layer has children and no data of its own.
            if layer.get("subLayerIds"):
                continue
            try:
                info = self.layer_info(layer_id)
            except ArcGisError:
                continue

            if info.get("type") in ("Group Layer", "Raster Layer", "Annotation Layer"):
                continue
            if info.get("geometryType") != "esriGeometryPoint":
                continue
            if "Query" not in (info.get("capabilities") or "Query"):
                continue

            usable.append({**layer, "_info": info})
        return usable

    def iter_features(
        self,
        layer_id: int,
        where: str = "1=1",
        out_fields: str = "*",
        page_size: int = PAGE_SIZE,
    ) -> Iterator[dict]:
        """Yield GeoJSON-shaped features from a layer, paging until exhausted.

        Prefers ``f=geojson`` and falls back to Esri's own JSON. Not every
        layer on DC's older MapServer instances advertises GeoJSON output, and
        the ones that do not reject the request outright rather than
        negotiating down.
        """
        fmt = "geojson"
        offset = 0
        while True:
            params = {
                "where": where,
                "outFields": out_fields,
                "outSR": 4326,
                "f": fmt,
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "returnGeometry": "true",
            }
            try:
                payload = self._get(f"{self.service_url}/{layer_id}/query", params)
            except ArcGisError:
                if fmt != "geojson":
                    raise
                # Retry this same page as Esri JSON before giving up.
                fmt = "json"
                params["f"] = fmt
                payload = self._get(f"{self.service_url}/{layer_id}/query", params)

            raw = payload.get("features", []) or []
            if not raw:
                return

            features = raw if fmt == "geojson" else [_esri_to_geojson(f) for f in raw]
            yield from features

            # `exceededTransferLimit` is the server telling us it truncated.
            # Some layers omit it, so a short page is also treated as the end.
            if not payload.get("exceededTransferLimit") and len(raw) < page_size:
                return
            offset += len(raw)


def _esri_to_geojson(feature: dict) -> dict:
    """Reshape one Esri JSON feature into the GeoJSON shape callers expect."""
    geometry = feature.get("geometry") or {}
    x, y = geometry.get("x"), geometry.get("y")
    return {
        "properties": feature.get("attributes") or {},
        "geometry": (
            {"type": "Point", "coordinates": [x, y]}
            if x is not None and y is not None
            else None
        ),
    }


def feature_point(feature: dict) -> tuple[float, float] | None:
    """Extract (lat, lon) from a GeoJSON point feature.

    Falls back to LATITUDE/LONGITUDE attributes, which MPD populates even on
    records whose geometry is null.
    """
    geom = feature.get("geometry") or {}
    if geom.get("type") == "Point":
        coords = geom.get("coordinates") or []
        if len(coords) >= 2 and coords[0] is not None and coords[1] is not None:
            return float(coords[1]), float(coords[0])

    props = feature.get("properties") or {}
    lat = props.get("LATITUDE") or props.get("latitude") or props.get("Y")
    lon = props.get("LONGITUDE") or props.get("longitude") or props.get("X")
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None
    return None


def pick_field(available: list[str], *candidates: str) -> str | None:
    """First candidate present in ``available``, matched case-insensitively.

    DDOT in particular has renamed the same column several times across
    releases, so every read goes through this rather than a literal name.
    """
    lookup = {f.lower(): f for f in available}
    for c in candidates:
        hit = lookup.get(c.lower())
        if hit:
            return hit
    return None
