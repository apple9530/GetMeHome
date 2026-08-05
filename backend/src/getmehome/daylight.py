"""Whether it is dark outside, from solar position.

The lighting factor should switch on when the sun is actually down, not at a
fixed clock hour. In DC sunset moves by nearly three hours across the year, so
a hardcoded 7pm cutoff would treat a pitch-dark 5:30pm in December as daytime
and a bright 8pm in June as night — and the lighting weight is the single
biggest term in the night risk model.

Uses the NOAA solar position approximation, which is accurate to well under a
minute for civil-twilight purposes.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from .config import ROUTING

# Civil twilight. Streetlights matter from the point where natural light stops
# being adequate, which is a little before geometric sunset.
CIVIL_TWILIGHT_ELEVATION = -6.0


def solar_elevation(when: datetime, lat: float, lon: float) -> float:
    """Sun elevation above the horizon, in degrees."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    utc = when.astimezone(UTC)

    # Julian day.
    a = (14 - utc.month) // 12
    y = utc.year + 4800 - a
    m = utc.month + 12 * a - 3
    jdn = (
        utc.day
        + (153 * m + 2) // 5
        + 365 * y
        + y // 4
        - y // 100
        + y // 400
        - 32045
    )
    day_fraction = (utc.hour - 12) / 24.0 + utc.minute / 1440.0 + utc.second / 86400.0
    jd = jdn + day_fraction
    n = jd - 2451545.0

    mean_longitude = (280.460 + 0.9856474 * n) % 360.0
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_longitude = math.radians(
        mean_longitude
        + 1.915 * math.sin(mean_anomaly)
        + 0.020 * math.sin(2 * mean_anomaly)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)

    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude))
    right_ascension = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_longitude),
        math.cos(ecliptic_longitude),
    )

    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    local_sidereal = math.radians(gmst * 15.0 + lon)
    hour_angle = local_sidereal - right_ascension

    lat_r = math.radians(lat)
    elevation = math.asin(
        math.sin(lat_r) * math.sin(declination)
        + math.cos(lat_r) * math.cos(declination) * math.cos(hour_angle)
    )
    return math.degrees(elevation)


def is_night(when: datetime, lat: float, lon: float) -> bool:
    """True when the sun is below civil twilight at that place and time."""
    try:
        return solar_elevation(when, lat, lon) < CIVIL_TWILIGHT_ELEVATION
    except (ValueError, OverflowError):
        # Fall back to the configured clock hours rather than failing a route.
        hour = when.hour
        return hour >= ROUTING.night_start_hour or hour < ROUTING.night_end_hour
