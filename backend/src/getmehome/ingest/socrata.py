"""Socrata Open Data portals, which is how New York publishes.

Washington serves its data from ArcGIS MapServers; New York uses Socrata. The
two differ in more than the URL — Socrata pages with ``$limit``/``$offset``,
filters with SoQL rather than an Esri where-clause, and returns flat JSON
objects with lowercase snake_case keys instead of Esri features with an
``attributes`` bag.

Same defensive posture as the ArcGIS client, for the same reason: these
schemas drift. Every column is resolved through a list of candidate names and
a missing one degrades to a default rather than raising, so a renamed field
costs a one-line change here instead of a rewrite.

Both NYPD datasets carry a very large number of rows — the historic complaint
file is millions — so the fetch is bounded by date rather than pulling
everything and filtering locally.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from ..cities import City
from ..config import CRIME
from ..safety.crime_model import CrimeIncident
from ..safety.lighting import StreetLight, lumens_for_type

log = logging.getLogger("getmehome.ingest.socrata")

# Socrata caps a single page at 50k rows; asking for more is silently clamped.
PAGE_SIZE = 50_000

# Generous, because a page of fifty thousand rows over a public portal is not
# quick and a timeout here means re-fetching the whole page.
_TIMEOUT_S = 120.0

# --- NYPD complaint columns ------------------------------------------------
#
# The year-to-date and historic datasets share a schema, which is why both can
# be read by the same code. Candidates are listed because NYPD has renamed
# columns across releases before.
_NYPD_OFFENSE = ("ofns_desc", "offense_desc", "pd_desc")
_NYPD_DATE = ("cmplnt_fr_dt", "cmplnt_dt", "rpt_dt")
_NYPD_TIME = ("cmplnt_fr_tm", "cmplnt_tm")
_NYPD_LAT = ("latitude", "lat")
_NYPD_LON = ("longitude", "lon", "lng")
_NYPD_WEAPON = ("pd_desc", "weapon_desc")
# Where it happened. NYPD's column is `prem_typ_desc` — the first pass had it
# as `premises_typ_desc`, which does not exist, so every incident came back
# with no premises and none of the residential filtering could apply.
_NYPD_PREMISES = ("prem_typ_desc", "premises_desc", "premise_type")
_NYPD_ID = ("cmplnt_num", "complaint_number")

# --- NYC DOT street light columns ------------------------------------------
_LIGHT_LAT = ("latitude", "lat")
_LIGHT_LON = ("longitude", "lon", "lng")
_LIGHT_TYPE = ("lamp_type", "luminaire_type", "fixture_type", "bulb_type")
_LIGHT_WATT = ("wattage", "watts", "lamp_wattage")
_LIGHT_HEIGHT = ("pole_height", "height", "mast_height")


class SocrataError(RuntimeError):
    pass


def pick(row: dict, candidates: tuple[str, ...], default=None):
    """First populated value among ``candidates``.

    Case-insensitive, because portals are inconsistent about it and a column
    that exists under a different case is worse than one that is missing — it
    looks like the data is empty.
    """
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in candidates:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def fetch_rows(
    domain: str,
    dataset: str,
    where: str = "",
    select: str = "",
    limit: int | None = None,
    app_token: str = "",
) -> list[dict]:
    """Page through a Socrata dataset.

    ``$order=:id`` is not optional. Without an explicit order Socrata makes no
    stability guarantee across pages, so rows can be repeated or skipped as the
    offset advances — which shows up as a dataset that is subtly the wrong size
    and nothing that looks like an error.
    """
    url = f"{domain.rstrip('/')}/resource/{dataset}.json"
    headers = {"X-App-Token": app_token} if app_token else {}

    out: list[dict] = []
    offset = 0
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as client:
        while True:
            page_size = PAGE_SIZE
            if limit is not None:
                page_size = min(page_size, limit - len(out))
                if page_size <= 0:
                    break

            params = {
                "$limit": page_size,
                "$offset": offset,
                "$order": ":id",
            }
            if where:
                params["$where"] = where
            if select:
                params["$select"] = select

            try:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                page = response.json()
            except httpx.HTTPStatusError as exc:
                # A 4xx is a bad query or a dataset that has moved; retrying
                # cannot help and the message from the portal names the reason.
                raise SocrataError(
                    f"{dataset}: {exc.response.status_code} {exc.response.text[:200]}"
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise SocrataError(f"{dataset}: {exc}") from exc

            if not page:
                break
            out.extend(page)
            offset += len(page)
            log.info("%s: %d rows", dataset, len(out))
            if len(page) < page_size:
                break

    return out


# --------------------------------------------------------------------------
# Crime
# --------------------------------------------------------------------------


def shift_for_hour(hour: int) -> str:
    """Map an hour to the same three shift values MPD publishes.

    NYPD stamps a time rather than a shift, so one is derived. The boundaries
    match MPD's own tours, which is what makes the two cities' night surfaces
    comparable — both are then "evening plus midnight" rather than one being a
    tour and the other an arbitrary cut.
    """
    if 0 <= hour < 8:
        return "MIDNIGHT"
    if 8 <= hour < 16:
        return "DAY"
    return "EVENING"


def parse_nypd_datetime(date_value, time_value) -> datetime | None:
    """Combine NYPD's separate date and time columns.

    The date is ISO; the time is ``HH:MM:SS`` and is occasionally ``(null)``
    as a literal string, which is why it is parsed defensively rather than
    split on colons and trusted.
    """
    if not date_value:
        return None
    text = str(date_value).strip()
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    hour = minute = 0
    if time_value:
        parts = str(time_value).strip().split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            hour = minute = 0
    # Both are validated, not just the hour. NYPD writes junk into this
    # column often enough that an out-of-range minute is a real occurrence,
    # and letting it through raises out of `replace` and loses the incident.
    if not 0 <= hour <= 23:
        hour = 0
    if not 0 <= minute <= 59:
        minute = 0

    return stamp.replace(hour=hour, minute=minute, tzinfo=stamp.tzinfo or UTC)


def fetch_socrata_crime(
    city: City,
    years: int = CRIME.history_years,
    limit: int | None = None,
    app_token: str = "",
) -> list[CrimeIncident]:
    """NYPD complaints for a city whose feed is a Socrata portal.

    Bounded by date in the query rather than fetched whole and filtered here:
    the historic complaint file runs to millions of rows going back to 2006,
    and pulling all of it to keep three years would be gigabytes over the wire
    for no benefit.
    """
    since = (datetime.now(UTC) - timedelta(days=365 * years + 30)).date().isoformat()
    incidents: list[CrimeIncident] = []
    seen: set[str] = set()

    for dataset in city.crime.datasets:
        # The date column differs between the two NYPD datasets only in which
        # candidate is populated, so the filter names the one they share.
        where = f"cmplnt_fr_dt >= '{since}T00:00:00'"
        try:
            rows = fetch_rows(
                city.crime.url, dataset, where=where, limit=limit, app_token=app_token
            )
        except SocrataError as exc:
            # One dataset failing should not lose the other. The year-to-date
            # file alone is a usable, if shorter, history.
            log.warning("skipping %s: %s", dataset, exc)
            continue

        for row in rows:
            lat = pick(row, _NYPD_LAT)
            lon = pick(row, _NYPD_LON)
            if lat is None or lon is None:
                continue
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            # NYPD nulls a small number of coordinates to 0,0.
            if not city.bbox.contains(lat_f, lon_f):
                continue

            when = parse_nypd_datetime(
                pick(row, _NYPD_DATE), pick(row, _NYPD_TIME)
            )
            if when is None:
                continue

            key = str(pick(row, _NYPD_ID, "")) or f"{lat_f:.6f},{lon_f:.6f},{when}"
            if key in seen:
                continue
            seen.add(key)

            incidents.append(
                CrimeIncident(
                    lat=lat_f,
                    lon=lon_f,
                    offense=str(pick(row, _NYPD_OFFENSE, "")).strip().upper(),
                    method=_weapon_from(row),
                    shift=shift_for_hour(when.hour),
                    reported_at=when,
                    premises=str(pick(row, _NYPD_PREMISES, "")).strip().upper(),
                )
            )

    log.info("%d NYPD incidents after deduplication", len(incidents))
    return incidents


def _weapon_from(row: dict) -> str:
    """Best-effort weapon, from a description field that mostly is not one.

    NYPD has no clean weapon column in the complaint feed; ``pd_desc`` carries
    it inside a longer description for the offences where it matters. Rather
    than parse that properly, this looks for the two words that change the
    weighting and returns nothing otherwise — a missed weapon costs a 1.4x
    multiplier, while a wrongly inferred one inflates a real incident.
    """
    text = str(pick(row, _NYPD_WEAPON, "")).upper()
    if "FIREARM" in text or "GUN" in text or "PISTOL" in text:
        return "FIREARM"
    if "KNIFE" in text or "CUTTING" in text:
        return "KNIFE"
    return ""


# --------------------------------------------------------------------------
# Streetlights
# --------------------------------------------------------------------------


def fetch_socrata_streetlights(
    city: City, limit: int | None = None, app_token: str = ""
) -> list[StreetLight]:
    """A city's streetlight inventory from a Socrata portal.

    NYC's DOT dataset records the pole rather than the luminaire, and carries
    no wattage for most rows. That is fine for the model — spacing is what it
    is really measuring — but it does mean nearly every lamp lands on the
    default lumen figure, so the lighting score here is closer to a
    well-modelled density than to a photometric estimate. The ranking against
    the city that the score goes through afterwards makes that difference
    smaller than it sounds.
    """
    lights: list[StreetLight] = []
    for dataset in city.lights.datasets:
        try:
            rows = fetch_rows(
                city.lights.url, dataset, limit=limit, app_token=app_token
            )
        except SocrataError as exc:
            log.warning("skipping %s: %s", dataset, exc)
            continue

        for row in rows:
            lat = pick(row, _LIGHT_LAT)
            lon = pick(row, _LIGHT_LON)
            if lat is None or lon is None:
                # Some rows carry only a projected point; skip rather than
                # guess a coordinate system.
                continue
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if not city.bbox.contains(lat_f, lon_f):
                continue

            height = pick(row, _LIGHT_HEIGHT)
            try:
                height_m = float(height) * 0.3048 if height else 0.0
            except (TypeError, ValueError):
                height_m = 0.0
            # Feet in the source; a pole under 3m or over 25m is a data error.
            if not 3.0 <= height_m <= 25.0:
                height_m = 0.0

            lights.append(
                StreetLight(
                    lat=lat_f,
                    lon=lon_f,
                    lumens=lumens_for_type(
                        pick(row, _LIGHT_TYPE), pick(row, _LIGHT_WATT)
                    ),
                    height_m=height_m or 8.0,
                )
            )

    log.info("%d streetlights", len(lights))
    return lights
