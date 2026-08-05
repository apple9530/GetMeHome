"""Central configuration for the GetMeHome safety-routing backend.

Everything tunable about the safety model lives here so the weights can be
adjusted without touching the scoring or routing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent.parent
DATA_DIR = Path(os.environ.get("GETMEHOME_DATA_DIR", BACKEND_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
BUILD_DIR = DATA_DIR / "build"

GRAPH_FILE = BUILD_DIR / "dc_graph.npz"
GRAPH_META_FILE = BUILD_DIR / "dc_graph_meta.json"
TRANSIT_FILE = BUILD_DIR / "dc_transit.pkl"
# Incident points kept for the map's crime grid. The graph bakes crime into
# per-segment scores, which cannot be un-mixed back into individual
# incidents, so the raw points are carried separately for the overlay.
CRIME_POINTS_FILE = BUILD_DIR / "crime_points.npz"

# --------------------------------------------------------------------------
# Study area — Washington, DC (plus a small collar so routes near the border
# do not dead-end at the district line).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon
        )

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_lat + self.max_lat) / 2, (self.min_lon + self.max_lon) / 2)


DC_BBOX = BBox(min_lat=38.7800, min_lon=-77.1400, max_lat=39.0100, max_lon=-76.8900)

# --------------------------------------------------------------------------
# Upstream data sources
# --------------------------------------------------------------------------

DDOT_STREETLIGHTS_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DDOT/Streetlights/MapServer"
)

# MPD publishes one feature layer per year plus a rolling 30-day layer. The
# ingester discovers the layer index at runtime rather than hardcoding it,
# because DC renumbers the layers when they roll the year over.
MPD_CRIME_SERVICE_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/MapServer"
)

# Public mirror of the DC OSM extract. Any .osm.pbf covering the bbox works.
OSM_EXTRACT_URL = (
    "https://download.geofabrik.de/north-america/us/district-of-columbia-latest.osm.pbf"
)

WMATA_GTFS_URL = "https://api.wmata.com/gtfs/bus-gtfs-static.zip"
WMATA_RAIL_GTFS_URL = "https://api.wmata.com/gtfs/rail-gtfs-static.zip"
WMATA_API_KEY = os.environ.get("WMATA_API_KEY", "")

# Nominatim is used for geocoding. Self-host or swap for another provider in
# production; the public instance rate-limits aggressively.
GEOCODER_URL = os.environ.get(
    "GETMEHOME_GEOCODER_URL", "https://nominatim.openstreetmap.org"
)
GEOCODER_USER_AGENT = "GetMeHome/1.0 (safety routing for Washington DC)"


# --------------------------------------------------------------------------
# Safety model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LightingConfig:
    """Parameters for the streetlight illuminance model.

    We model each lamp as a point source and compute a relative illuminance at
    street level. The absolute units are meaningless; what matters is the
    ratio against ``reference_illuminance``, which maps a normally-lit
    residential street to roughly 1.0.
    """

    # Beyond this radius a lamp contributes nothing. Keeps the KD-tree query
    # bounded; at 40m a standard cobra-head is already contributing little.
    search_radius_m: float = 45.0

    # Typical mounting height. Enters the inverse-square term so that standing
    # directly under a lamp does not produce an infinite illuminance.
    default_mount_height_m: float = 8.0

    # Lumen output by lamp type, keyed on lowercase substrings found in the
    # DDOT LAMPTYPE / LIGHTTYPE fields. Falls back to ``default_lumens``.
    lumens_by_type: dict[str, float] = field(
        default_factory=lambda: {
            "led": 12000.0,
            "hps": 9500.0,
            "high pressure sodium": 9500.0,
            "mh": 11000.0,
            "metal halide": 11000.0,
            "mercury": 7000.0,
            "incandescent": 1200.0,
            "fluorescent": 4000.0,
            "gas": 900.0,  # DC still runs gas lamps in Georgetown
        }
    )
    default_lumens: float = 9000.0

    # Illuminance considered "adequately lit". Scores saturate above this.
    reference_illuminance: float = 18.0

    # Sample the edge geometry at this spacing when measuring lighting.
    sample_spacing_m: float = 12.0

    # A street is only as safe as its darkest stretch, so the edge score is a
    # blend of the mean and the 20th percentile of its samples.
    dark_gap_weight: float = 0.45


@dataclass(frozen=True)
class CrimeConfig:
    """Parameters for the crime-density model."""

    # Severity in [0, 1] per MPD OFFENSE value. These are the weights that
    # decide how much each crime type moves the risk needle.
    #
    # The gap between the violent and property groups is deliberately wide.
    # A stolen car and a sexual assault are not different points on one scale
    # of "how bad" — for someone deciding which street to walk down at night
    # they are barely the same kind of information. Property crime is left
    # non-zero because a street with a lot of it is usually a street with
    # little passive supervision, which is a weak but real signal.
    severity: dict[str, float] = field(
        default_factory=lambda: {
            # Violent and sexual offences.
            "HOMICIDE": 1.00,
            "SEX ABUSE": 1.00,
            "ASSAULT W/DANGEROUS WEAPON": 0.90,
            "ROBBERY": 0.85,
            # Property offences.
            "ARSON": 0.25,
            "BURGLARY": 0.15,
            "MOTOR VEHICLE THEFT": 0.08,
            "THEFT F/AUTO": 0.05,
            "THEFT/OTHER": 0.05,
        }
    )
    default_severity: float = 0.15

    # Readable names for MPD's offence codes.
    #
    # The raw values are database codes — "THEFT F/AUTO", "ASSAULT
    # W/DANGEROUS WEAPON" — and no amount of automatic title-casing turns
    # those into English. Mapped here rather than in the app so the same
    # wording is used everywhere and one file holds the vocabulary.
    display_name: dict[str, str] = field(
        default_factory=lambda: {
            "HOMICIDE": "Homicide",
            "SEX ABUSE": "Sexual offense",
            "ASSAULT W/DANGEROUS WEAPON": "Assault with a weapon",
            "ROBBERY": "Robbery",
            "ARSON": "Arson",
            "BURGLARY": "Burglary",
            "MOTOR VEHICLE THEFT": "Vehicle theft",
            "THEFT F/AUTO": "Theft from a vehicle",
            "THEFT/OTHER": "Theft",
        }
    )

    # Broad grouping, used to label and order what the map's crime grid shows.
    # Keeping this separate from the numeric weight means the UI can say
    # "3 violent" without re-deriving that from a severity threshold.
    category: dict[str, str] = field(
        default_factory=lambda: {
            "HOMICIDE": "violent",
            "ASSAULT W/DANGEROUS WEAPON": "violent",
            "ROBBERY": "violent",
            "SEX ABUSE": "sexual",
            "ARSON": "property",
            "BURGLARY": "property",
            "MOTOR VEHICLE THEFT": "property",
            "THEFT F/AUTO": "property",
            "THEFT/OTHER": "property",
        }
    )
    default_category: str = "other"

    @property
    def serious_categories(self) -> frozenset[str]:
        """Categories counted as violent for the UI's headline figure."""
        return frozenset({"violent", "sexual"})

    # How much each offence type bears on the safety of someone *walking past*
    # the location. A burglary is serious but is an indoor property crime and
    # says less about street risk than a street robbery does.
    pedestrian_relevance: dict[str, float] = field(
        default_factory=lambda: {
            "HOMICIDE": 1.00,
            "SEX ABUSE": 1.00,
            "ASSAULT W/DANGEROUS WEAPON": 1.00,
            "ROBBERY": 1.00,
            "ARSON": 0.35,
            "BURGLARY": 0.25,
            "MOTOR VEHICLE THEFT": 0.45,
            "THEFT F/AUTO": 0.50,
            "THEFT/OTHER": 0.60,
        }
    )
    default_relevance: float = 0.5

    # Weapon multipliers from the MPD METHOD field.
    method_multiplier: dict[str, float] = field(
        default_factory=lambda: {
            "GUN": 1.40,
            "KNIFE": 1.20,
            "OTHERS": 1.00,
        }
    )

    # Exponential recency decay. An incident from ~18 months ago counts about
    # a third as much as one from last week.
    half_life_days: float = 400.0

    # How far back to ingest. Three years balances a stable spatial signal
    # against neighbourhoods genuinely changing.
    history_years: int = 3

    # Gaussian KDE bandwidth. ~150m is roughly a city block and a half, which
    # is about how far the character of a DC street persists.
    kernel_bandwidth_m: float = 150.0
    kernel_cutoff_m: float = 450.0  # 3 sigma

    # Density is normalised against this percentile of the city-wide
    # distribution, then clamped to 1. Using a high percentile rather than the
    # max stops one extreme hotspot from flattening everywhere else to zero.
    normalisation_percentile: float = 97.0

    sample_spacing_m: float = 25.0


@dataclass(frozen=True)
class RiskWeights:
    """How lighting, crime and street character combine into one risk value."""

    crime_day: float = 0.75
    crime_night: float = 0.85
    darkness_day: float = 0.0  # daylight — streetlights are irrelevant
    darkness_night: float = 0.90
    isolation_day: float = 0.20
    isolation_night: float = 0.45

    def for_period(self, is_night: bool) -> tuple[float, float, float]:
        if is_night:
            return self.crime_night, self.darkness_night, self.isolation_night
        return self.crime_day, self.darkness_day, self.isolation_day


@dataclass(frozen=True)
class IsolationConfig:
    """Risk contribution from the physical character of the way itself.

    Derived from OSM tags. A lit arterial with shopfronts feels different at
    2am from an unlit path through a park, and the tags capture some of that
    even where we have no lamp or crime data.
    """

    by_highway: dict[str, float] = field(
        default_factory=lambda: {
            "primary": 0.05,
            "secondary": 0.08,
            "tertiary": 0.12,
            "residential": 0.20,
            "living_street": 0.20,
            "unclassified": 0.28,
            "service": 0.40,
            "pedestrian": 0.15,
            "footway": 0.35,
            "path": 0.55,
            "track": 0.70,
            "steps": 0.45,
            "cycleway": 0.45,
            "corridor": 0.40,
        }
    )
    default: float = 0.30

    # Additive penalties, applied then clamped to 1.
    tunnel_penalty: float = 0.30
    park_penalty: float = 0.25  # path inside a park/wood at night
    no_sidewalk_penalty: float = 0.10
    alley_penalty: float = 0.35
    lit_tag_bonus: float = -0.10  # OSM lit=yes corroborates the lamp data


@dataclass(frozen=True)
class CameraConfig:
    """Automated licence-plate-reader (ALPR) camera exposure.

    Deliberately kept out of the safety score. Passing a surveillance camera
    is not dangerous, it is a privacy preference, and folding it into a
    "safety" number would make both numbers mean less. It is carried as its
    own per-edge attribute and only enters the cost function when the caller
    explicitly asks to avoid cameras.

    Source data is the OSM ALPR tagging convention popularised by DeFlock:
    ``man_made=surveillance`` + ``surveillance:type=ALPR``. It is crowdsourced
    and therefore incomplete — absence of a camera in the data is not evidence
    of absence on the street.
    """

    # Effective read range of a plate reader. Flock's published figure is
    # around 23m for plate capture; we use a more conservative radius since
    # the camera still records outside its reliable-read distance.
    range_m: float = 70.0

    # Half-angle of the field-of-view cone. Real ALPR optics are narrow; this
    # is deliberately generous because the mapped ``direction`` is usually
    # eyeballed by a contributor rather than measured.
    fov_half_angle_deg: float = 35.0

    # Cameras with no mapped direction are treated as omnidirectional, but
    # discounted since we do not know that they actually face the street.
    undirected_weight: float = 0.55

    # Exposure decays with distance out to ``range_m``.
    falloff_exponent: float = 1.5

    sample_spacing_m: float = 15.0

    # Cost multiplier when avoidance is requested. High enough to force a
    # detour around a covered block, low enough that it will still route
    # through one if the alternative is a huge diversion.
    avoid_lambda: float = 4.0

    # Optional live source, queried at build time if reachable.
    deflock_api_url: str = "https://deflock.me/api/v1/reports"


@dataclass(frozen=True)
class RoutingConfig:
    walk_speed_mps: float = 1.35  # ~4.9 km/h, an average adult walking pace

    # Risk aversion (lambda) per preference. Cost multiplier on an edge is
    # ``1 + lambda * risk``, so lambda=6 means a maximally risky street costs
    # 7x its length. These three produce the fastest/balanced/safest options.
    lambda_fastest: float = 0.0
    lambda_balanced: float = 2.0
    lambda_safest: float = 7.0

    # Alternatives are rejected if they share more than this fraction of their
    # length with an already-selected route.
    max_route_overlap: float = 0.75

    # Cap on how much longer a "safest" route may be than the fastest one.
    # Without this the router will happily send someone on a 3km detour.
    max_detour_ratio: float = 2.2

    # Snapping a coordinate to the graph.
    max_snap_distance_m: float = 250.0

    # Night is defined by these hours in local time when the sun position is
    # not available; the API prefers real sunset/sunrise when it can.
    night_start_hour: int = 19
    night_end_hour: int = 7


@dataclass(frozen=True)
class TransitConfig:
    max_transfers: int = 3
    # How far someone will walk to reach a stop.
    max_access_walk_m: float = 900.0
    max_transfer_walk_m: float = 400.0
    # Waiting at a stop is exposure too — a minute standing at an unlit bus
    # stop counts for more than a minute walking down a busy street.
    wait_risk_multiplier: float = 1.35
    # Boarding/alighting overhead, seconds.
    board_penalty_s: float = 45.0
    max_journey_duration_s: float = 3 * 3600
    # Search window after the requested departure time.
    departure_window_s: float = 90 * 60


LIGHTING = LightingConfig()
CRIME = CrimeConfig()
RISK = RiskWeights()
ISOLATION = IsolationConfig()
CAMERAS = CameraConfig()
ROUTING = RoutingConfig()
TRANSIT = TransitConfig()

# Highway values that are walkable. Everything else is dropped from the graph.
WALKABLE_HIGHWAYS = frozenset(
    {
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "residential",
        "living_street",
        "unclassified",
        "service",
        "pedestrian",
        "footway",
        "path",
        "steps",
        "track",
        "cycleway",
        "corridor",
    }
)

# Motorways and their links are never walkable regardless of other tags.
FORBIDDEN_HIGHWAYS = frozenset(
    {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed"}
)
