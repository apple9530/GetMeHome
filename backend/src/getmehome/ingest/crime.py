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
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from ..cities import DC
from ..config import CRIME
from ..safety.crime_model import CrimeIncident
from .arcgis import ArcGisClient, ArcGisError, feature_point, pick_field

log = logging.getLogger("getmehome.ingest.crime")

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
    service_url: str | None = None,
    years: int = CRIME.history_years,
    limit: int | None = None,
) -> list[CrimeIncident]:
    """Download the last ``years`` calendar years of MPD incidents.

    ArcGIS-specific, and so DC-specific. A city whose feed is a Socrata portal
    goes through :mod:`getmehome.ingest.socrata` instead; the build picks the
    adapter from ``city.crime.kind`` rather than from the city itself, so a new
    city on a familiar platform needs no new ingest code.
    """
    service_url = service_url or DC.crime.url
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

        targets = client.queryable_point_layers(targets)
        if not targets:
            raise RuntimeError(
                f"no queryable crime point layers at {service_url}"
            )
        log.info(
            "querying crime layers: %s",
            [(t.get("id"), t.get("name")) for t in targets],
        )

        for layer in targets:
            layer_id = layer["id"]
            try:
                fetched = _fetch_crime_layer(client, layer_id, seen, limit, len(incidents))
            except (ArcGisError, RuntimeError) as exc:
                # Losing one year still leaves a usable density surface; losing
                # the whole build over it does not.
                log.warning(
                    "skipping crime layer %s (%s): %s",
                    layer_id, layer.get("name", ""), exc,
                )
                continue
            incidents.extend(fetched)
            if limit and len(incidents) >= limit:
                break

    if not incidents:
        raise RuntimeError(f"no crime incidents returned by {service_url}")
    return incidents


def _fetch_crime_layer(
    client: ArcGisClient,
    layer_id: int,
    seen: set[str],
    limit: int | None,
    already: int,
) -> list[CrimeIncident]:
    """Read every incident from one layer, skipping CCNs already collected."""
    incidents: list[CrimeIncident] = []
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
        if limit and already + len(incidents) >= limit:
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
                    "p": i.premises,
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
            premises=r.get("p", ""),
        )
        for r in json.loads(path.read_text())
    ]
