"""Fetch the DDOT streetlight inventory from Open Data DC.

DDOT's schema has drifted across releases — the lamp type has been carried as
LAMPTYPE, LIGHTTYPE and BULBTYPE at various points, and mounting height is
sometimes absent entirely. Rather than pin one schema and break on the next
publish, every column is resolved through :func:`pick_field` and missing
values fall back to the configured defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import DDOT_STREETLIGHTS_URL, LIGHTING
from ..safety.lighting import StreetLight, lumens_for_type
from .arcgis import ArcGisClient, feature_point, pick_field

# Candidate column names, in the order we prefer them.
_TYPE_FIELDS = ("LAMPTYPE", "LIGHTTYPE", "BULBTYPE", "FIXTURETYPE", "LUMINAIRETYPE")
_WATT_FIELDS = ("WATTAGE", "WATTS", "LAMPWATTAGE", "POWER")
_HEIGHT_FIELDS = ("LIGHTHEIGHT", "POLEHEIGHT", "MOUNTHEIGHT", "HEIGHT")
_STATUS_FIELDS = ("STATUS", "ASSETSTATUS", "LIGHTSTATUS")

# Status values meaning the lamp is not currently producing light.
_DEAD_STATUSES = {
    "removed", "retired", "abandoned", "inactive", "out", "decommissioned",
}


def fetch_streetlights(
    service_url: str = DDOT_STREETLIGHTS_URL, limit: int | None = None
) -> list[StreetLight]:
    """Download every streetlight in the District."""
    lights: list[StreetLight] = []

    with ArcGisClient(service_url) as client:
        layers = client.find_layers("street light", "streetlight", "light")
        if not layers:
            layers = client.layers()
        if not layers:
            raise RuntimeError(f"no layers found at {service_url}")

        for layer in layers:
            layer_id = layer["id"]
            fields = client.layer_fields(layer_id)
            f_type = pick_field(fields, *_TYPE_FIELDS)
            f_watt = pick_field(fields, *_WATT_FIELDS)
            f_height = pick_field(fields, *_HEIGHT_FIELDS)
            f_status = pick_field(fields, *_STATUS_FIELDS)

            for feature in client.iter_features(layer_id):
                point = feature_point(feature)
                if point is None:
                    continue
                props = feature.get("properties") or {}

                if f_status:
                    status = str(props.get(f_status) or "").strip().lower()
                    if status in _DEAD_STATUSES:
                        continue

                height = LIGHTING.default_mount_height_m
                if f_height:
                    try:
                        raw = float(props.get(f_height) or 0)
                        # Values are sometimes feet, sometimes metres. Anything
                        # above 20 is implausible in metres for a street lamp.
                        if raw > 20:
                            raw *= 0.3048
                        if 2.0 <= raw <= 20.0:
                            height = raw
                    except (TypeError, ValueError):
                        pass

                lights.append(
                    StreetLight(
                        lat=point[0],
                        lon=point[1],
                        lumens=lumens_for_type(
                            props.get(f_type) if f_type else None,
                            props.get(f_watt) if f_watt else None,
                        ),
                        height_m=height,
                    )
                )
                if limit and len(lights) >= limit:
                    return lights

    return lights


def save_streetlights(lights: list[StreetLight], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "lat": lamp.lat,
                    "lon": lamp.lon,
                    "lm": round(lamp.lumens),
                    "h": round(lamp.height_m, 1),
                }
                for lamp in lights
            ]
        )
    )


def load_streetlights(path: Path) -> list[StreetLight]:
    return [
        StreetLight(lat=r["lat"], lon=r["lon"], lumens=r["lm"], height_m=r["h"])
        for r in json.loads(path.read_text())
    ]
