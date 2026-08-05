"""Fetch MPD crime incidents from Open Data DC.

MPD publishes one layer per calendar year plus a rolling 30-day layer. We pull
several years so the density surface is stable — a single year of data in a
quiet neighbourhood is thin enough that one incident visibly bends routes —
and let the recency decay in the crime model handle the age weighting.

The 30-day layer overlaps the current year's layer, so records are
deduplicated on CCN (the MPD case number).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ..config import CRIME, MPD_CRIME_SERVICE_URL
from ..safety.crime_model import CrimeIncident
from .arcgis import ArcGisClient, feature_point, pick_field

_OFFENSE_FIELDS = ("OFFENSE", "OFFENSE_TEXT", "OFFENSETEXT", "CRIMETYPE")
_METHOD_FIELDS = ("METHOD", "WEAPON")
_SHIFT_FIELDS = ("SHIFT", "TIMEOFDAY")
_DATE_FIELDS = ("REPORT_DAT", "REPORTDATE", "START_DATE", "REPORTDATETIME", "DATETIME")
_CCN_FIELDS = ("CCN", "OCTO_RECORD_ID", "OBJECTID")

_YEAR_IN_NAME = re.compile(r"(20\d{2})")


def parse_timestamp(value) -> datetime | None:
    """Parse the several timestamp shapes MPD emits.

    Epoch milliseconds from the Esri JSON path, ISO-8601 from GeoJSON, and
    occasionally a bare date.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        # Esri sends epoch milliseconds. Values that look like seconds are
        # treated as such so a schema change does not silently yield 1970.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (None, "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = (
                datetime.fromisoformat(text)
                if fmt is None
                else datetime.strptime(text, fmt)
            )
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def fetch_crime(
    service_url: str = MPD_CRIME_SERVICE_URL,
    years: int = CRIME.history_years,
    limit: int | None = None,
) -> list[CrimeIncident]:
    """Download the last ``years`` calendar years of incidents."""
    current_year = datetime.now(UTC).year
    wanted_years = {str(y) for y in range(current_year - years + 1, current_year + 1)}

    incidents: list[CrimeIncident] = []
    seen: set[str] = set()

    with ArcGisClient(service_url) as client:
        layers = client.layers()
        if not layers:
            raise RuntimeError(f"no layers found at {service_url}")

        targets = []
        for layer in layers:
            name = layer.get("name", "")
            lowered = name.lower()
            if "crime" not in lowered:
                continue
            year_match = _YEAR_IN_NAME.search(name)
            if year_match:
                if year_match.group(1) in wanted_years:
                    targets.append(layer)
            elif "30 day" in lowered or "30-day" in lowered:
                targets.append(layer)

        if not targets:
            raise RuntimeError(
                f"no crime layers for {sorted(wanted_years)} at {service_url}; "
                f"available: {[layer.get('name') for layer in layers]}"
            )

        for layer in targets:
            layer_id = layer["id"]
            fields = client.layer_fields(layer_id)
            f_off = pick_field(fields, *_OFFENSE_FIELDS)
            f_met = pick_field(fields, *_METHOD_FIELDS)
            f_shift = pick_field(fields, *_SHIFT_FIELDS)
            f_date = pick_field(fields, *_DATE_FIELDS)
            f_ccn = pick_field(fields, *_CCN_FIELDS)

            for feature in client.iter_features(layer_id):
                point = feature_point(feature)
                if point is None:
                    continue
                props = feature.get("properties") or {}

                if f_ccn:
                    ccn = str(props.get(f_ccn) or "")
                    if ccn and ccn in seen:
                        continue
                    if ccn:
                        seen.add(ccn)

                when = parse_timestamp(props.get(f_date)) if f_date else None
                if when is None:
                    continue

                incidents.append(
                    CrimeIncident(
                        lat=point[0],
                        lon=point[1],
                        offense=str(props.get(f_off) or "").strip().upper(),
                        method=str(props.get(f_met) or "").strip().upper(),
                        shift=str(props.get(f_shift) or "").strip().upper(),
                        reported_at=when,
                    )
                )
                if limit and len(incidents) >= limit:
                    return incidents

    return incidents


def save_crime(incidents: list[CrimeIncident], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "lat": round(i.lat, 6),
                    "lon": round(i.lon, 6),
                    "o": i.offense,
                    "m": i.method,
                    "s": i.shift,
                    "t": i.reported_at.isoformat(),
                }
                for i in incidents
            ]
        )
    )


def load_crime(path: Path) -> list[CrimeIncident]:
    return [
        CrimeIncident(
            lat=r["lat"],
            lon=r["lon"],
            offense=r["o"],
            method=r["m"],
            shift=r["s"],
            reported_at=datetime.fromisoformat(r["t"]),
        )
        for r in json.loads(path.read_text())
    ]
