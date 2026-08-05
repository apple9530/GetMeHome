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
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                # DC's servers rate-limit under a full-history crime pull.
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

    def layer_fields(self, layer_id: int) -> list[str]:
        """Field names on a layer.

        Used to adapt to schema drift rather than assuming a column exists.
        """
        payload = self._get(f"{self.service_url}/{layer_id}", {"f": "json"})
        return [f.get("name", "") for f in payload.get("fields", []) or []]

    def iter_features(
        self,
        layer_id: int,
        where: str = "1=1",
        out_fields: str = "*",
        page_size: int = PAGE_SIZE,
    ) -> Iterator[dict]:
        """Yield GeoJSON features from a layer, paging until exhausted."""
        offset = 0
        while True:
            payload = self._get(
                f"{self.service_url}/{layer_id}/query",
                {
                    "where": where,
                    "outFields": out_fields,
                    "outSR": 4326,
                    "f": "geojson",
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                    "returnGeometry": "true",
                },
            )
            features = payload.get("features", []) or []
            if not features:
                return
            yield from features

            # `exceededTransferLimit` is the server telling us it truncated.
            # Some layers omit it, so a short page is also treated as the end.
            if not payload.get("exceededTransferLimit") and len(features) < page_size:
                return
            offset += len(features)


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
