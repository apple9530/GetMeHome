"""The cities this service can route in.

Everything that differs between one city and the next lives in a :class:`City`
record: where it is, what its data sources are, and — the part that carries the
most judgement — how to read its crime feed.

**Crime vocabularies do not transfer.** Washington's MPD publishes offences as
`ASSAULT W/DANGEROUS WEAPON` and `THEFT F/AUTO`; New York's NYPD publishes
`FELONY ASSAULT` and `GRAND LARCENY OF MOTOR VEHICLE`. They are different
vocabularies describing different legal categories, so each city carries its
own severity and pedestrian-relevance weights. Sharing one table across both
would silently fall through to the default weight for every NYPD offence and
produce a crime surface that is really just a population-density map.

**Time of day is derived differently too.** MPD stamps every incident with a
shift; NYPD gives a timestamp. Each city says which it has, and the ingester
does the right thing.

Adding a third city means adding a record here and, if its feeds are not
Socrata or ArcGIS, an ingest adapter. Nothing else in the codebase should need
to learn its name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .geo import Projection


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

    def as_list(self) -> list[float]:
        return [self.min_lat, self.min_lon, self.max_lat, self.max_lon]


@dataclass(frozen=True)
class CrimeVocabulary:
    """How to read one city's crime feed.

    The four maps are keyed on that city's own offence strings, uppercased.
    Kept together because they have to be edited together: adding an offence to
    one and forgetting the others produces a plausible-looking weight built
    from three defaults.
    """

    severity: dict[str, float]
    pedestrian_relevance: dict[str, float]
    display_name: dict[str, str]
    category: dict[str, str]
    # Values in the feed's own weapon/method field that raise the weight.
    method_multiplier: dict[str, float] = field(default_factory=dict)

    def check(self) -> list[str]:
        """Offences that are not described consistently across all four maps.

        Used by the build to fail loudly rather than quietly scoring a real
        offence at the default weight.
        """
        known = set(self.severity)
        problems = []
        for name, table in (
            ("pedestrian_relevance", self.pedestrian_relevance),
            ("display_name", self.display_name),
            ("category", self.category),
        ):
            missing = known - set(table)
            if missing:
                problems.append(f"{name} is missing {sorted(missing)}")
            extra = set(table) - known
            if extra:
                problems.append(f"{name} has no severity for {sorted(extra)}")
        return problems


# --------------------------------------------------------------------------
# Premises
#
# NYPD publishes where an incident happened — `prem_typ_desc` — and it turns
# out to matter a great deal. A large share of New York's reported sexual
# offences and assaults occur inside dwellings, and a domestic assault in an
# eleventh-floor apartment says close to nothing about the risk of walking past
# the building. Counting it does two wrong things at once: it inflates the risk
# of residential streets, and it maps the location of housing rather than the
# location of street crime.
#
# So incidents are weighted by where they happened, in three bands.
# --------------------------------------------------------------------------

#: Substrings identifying a private dwelling. Matched case-insensitively
#: against the feed's premises string.
PRIVATE_PREMISES: tuple[str, ...] = (
    "RESIDENCE",
    "DWELLING",
    "APT",
    "APARTMENT",
    "PRIVATE HOUSE",
    "PUBLIC HOUSING",
)

#: Indoor but open to the public. A robbery outside a bar at 1am is squarely
#: pedestrian-relevant even though the report says the premises was the bar,
#: and transit interiors are somewhere people walk through at night — so these
#: are discounted rather than dropped.
SEMI_PUBLIC_PREMISES: tuple[str, ...] = (
    "BAR/NIGHT CLUB",
    "RESTAURANT",
    "DINER",
    "STORE",
    "SUPERMARKET",
    "DRUG STORE",
    "GAS STATION",
    "HOTEL",
    "TRANSIT",
    "SUBWAY",
    "STATION",
    "BUS",
    "TERMINAL",
    "HOSPITAL",
    "DOCTOR",
    "BANK",
    "CHECK CASHING",
    "COMMERCIAL",
    "STORE UNCLASSIFIED",
    "SHOE",
    "CLOTHING",
    "VARIETY STORE",
    "FAST FOOD",
    "GROCERY",
    "CHAIN STORE",
    "DEPARTMENT STORE",
    "BOOK/CARD",
    "JEWELRY",
    "LIQUOR STORE",
    "TELECOMM",
    "SMALL MERCHANT",
    "CANDY STORE",
    "BEAUTY",
    "LOAN",
    "SOCIAL CLUB",
    "GYM",
    "STORAGE",
    "FACTORY",
    "SCHOOL",
    "CHURCH",
    "SYNAGOGUE",
    "MOSQUE",
    "OTHER HOUSE OF WORSHIP",
)


def premises_class(premises: str) -> str:
    """One of "outdoor", "semi_public", "private" or "unknown".

    Order matters: "PUBLIC HOUSING" must not be read as public, and
    "RESIDENCE - APT. HOUSE" is a dwelling despite containing "HOUSE". Private
    is therefore tested first.
    """
    text = (premises or "").strip().upper()
    if not text:
        return "unknown"
    if any(needle in text for needle in PRIVATE_PREMISES):
        return "private"
    if any(needle in text for needle in SEMI_PUBLIC_PREMISES):
        return "semi_public"
    # "STREET", "PARK", "PARKING LOT/GARAGE", "HIGHWAY/PARKWAY", "OPEN AREAS
    # UNCLASSIFIED" — everywhere a pedestrian actually is.
    return "outdoor"


@dataclass(frozen=True)
class PremisesWeights:
    """How much an incident counts, given where it happened.

    Zero for private dwellings is a deliberate exclusion rather than a very
    small number: the question this model answers is "what is the risk of
    walking down this street", and a crime committed inside someone's home is
    not evidence about that. Including it at any weight makes the surface
    partly a map of where people live.

    ``unknown`` sits between the two. A feed that does not publish premises at
    all — Washington's does not — passes 1.0 here and is unaffected, which is
    why the default is a policy that weights everything equally.
    """

    outdoor: float = 1.0
    semi_public: float = 0.55
    private: float = 0.0
    unknown: float = 1.0

    def weight_for(self, premises: str) -> float:
        return getattr(self, premises_class(premises))

    @property
    def is_identity(self) -> bool:
        """True when this policy changes nothing, i.e. the feed has no premises."""
        return (
            self.outdoor == self.semi_public == self.private == self.unknown == 1.0
        )


#: A feed with no premises column. Everything counts, as it did before.
NO_PREMISES = PremisesWeights(
    outdoor=1.0, semi_public=1.0, private=1.0, unknown=1.0
)

#: Street-focused, for a feed that does publish premises.
STREET_ONLY = PremisesWeights()


@dataclass(frozen=True)
class CrimeSource:
    """Where a city's incident reports come from."""

    # "arcgis" (a MapServer with per-year layers) or "socrata" (an Open Data
    # portal). The adapter is chosen on this rather than on the city, so a new
    # city on a familiar platform needs no new ingest code.
    kind: str
    url: str
    # Socrata only: the dataset identifiers, newest first.
    datasets: tuple[str, ...] = ()
    # Whether the feed carries an explicit day/evening/night shift. Where it
    # does not, the hour of the timestamp is used instead.
    has_shift_field: bool = False
    # How to weight an incident by where it happened. Feeds that do not
    # publish premises get NO_PREMISES and are unaffected.
    premises: PremisesWeights = field(default_factory=lambda: NO_PREMISES)


@dataclass(frozen=True)
class LightSource:
    """Where a city's streetlight inventory comes from."""

    kind: str  # "arcgis" | "socrata"
    url: str
    datasets: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitFeed:
    """One GTFS feed to download and merge."""

    name: str
    url: str
    # WMATA gates its GTFS behind the same key as its real-time API; the MTA
    # publishes static feeds openly.
    needs_wmata_key: bool = False


@dataclass(frozen=True)
class City:
    """Everything that varies between cities."""

    slug: str
    name: str
    # What the app shows under the name, e.g. "District of Columbia".
    region: str
    bbox: BBox
    timezone_name: str
    # Anchor for the local projection. The city's rough centre; the frame's
    # error grows with distance from it, and a few tens of kilometres of
    # equirectangular distortion is centimetres.
    origin: tuple[float, float]

    osm_extract_url: str
    crime: CrimeSource
    lights: LightSource
    transit_feeds: tuple[TransitFeed, ...]
    crime_vocabulary: CrimeVocabulary

    # Real-time transit provider: "wmata" (JSON REST) or "mta" (GTFS-Realtime
    # protobuf). Two genuinely different protocols, not two URLs.
    realtime: str = ""

    @property
    def projection(self) -> Projection:
        return Projection(self.origin[0], self.origin[1])

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def center(self) -> tuple[float, float]:
        return self.bbox.center

    # -- per-city storage -------------------------------------------------

    def data_dir(self, root: Path) -> Path:
        return root / self.slug

    def raw_dir(self, root: Path) -> Path:
        return self.data_dir(root) / "raw"

    def build_dir(self, root: Path) -> Path:
        return self.data_dir(root) / "build"


# --------------------------------------------------------------------------
# Washington, DC
# --------------------------------------------------------------------------

# MPD's offence values. The gap between the violent and property groups is
# deliberately wide: a stolen car and a sexual assault are not different points
# on one scale of "how bad" — for someone deciding which street to walk down at
# night they are barely the same kind of information. Property crime stays
# non-zero because a street with a lot of it usually has little passive
# supervision, which is a weak but real signal.
DC_CRIME = CrimeVocabulary(
    severity={
        "HOMICIDE": 1.00,
        "SEX ABUSE": 1.00,
        "ASSAULT W/DANGEROUS WEAPON": 0.90,
        "ROBBERY": 0.85,
        "ARSON": 0.25,
        "BURGLARY": 0.15,
        "MOTOR VEHICLE THEFT": 0.08,
        "THEFT F/AUTO": 0.05,
        "THEFT/OTHER": 0.05,
    },
    # How much each offence bears on the safety of someone *walking past*. A
    # burglary is serious but is an indoor property crime and says less about
    # street risk than a street robbery does.
    pedestrian_relevance={
        "HOMICIDE": 1.00,
        "SEX ABUSE": 1.00,
        "ASSAULT W/DANGEROUS WEAPON": 1.00,
        "ROBBERY": 1.00,
        "ARSON": 0.35,
        "BURGLARY": 0.25,
        "MOTOR VEHICLE THEFT": 0.45,
        "THEFT F/AUTO": 0.50,
        "THEFT/OTHER": 0.60,
    },
    # The raw values are database codes and no amount of automatic title-casing
    # turns "THEFT F/AUTO" into English.
    display_name={
        "HOMICIDE": "Homicide",
        "SEX ABUSE": "Sexual offense",
        "ASSAULT W/DANGEROUS WEAPON": "Assault with a weapon",
        "ROBBERY": "Robbery",
        "ARSON": "Arson",
        "BURGLARY": "Burglary",
        "MOTOR VEHICLE THEFT": "Vehicle theft",
        "THEFT F/AUTO": "Theft from a vehicle",
        "THEFT/OTHER": "Theft",
    },
    category={
        "HOMICIDE": "violent",
        "ASSAULT W/DANGEROUS WEAPON": "violent",
        "ROBBERY": "violent",
        "SEX ABUSE": "sexual",
        "ARSON": "property",
        "BURGLARY": "property",
        "MOTOR VEHICLE THEFT": "property",
        "THEFT F/AUTO": "property",
        "THEFT/OTHER": "property",
    },
    method_multiplier={"GUN": 1.40, "KNIFE": 1.20, "OTHERS": 1.00},
)

DC = City(
    slug="dc",
    name="Washington",
    region="District of Columbia",
    # A small collar past the district line so routes near the border do not
    # dead-end at it.
    bbox=BBox(min_lat=38.7800, min_lon=-77.1400, max_lat=39.0100, max_lon=-76.8900),
    timezone_name="America/New_York",
    origin=(38.9047, -77.0164),
    osm_extract_url=(
        "https://download.geofabrik.de/north-america/us/"
        "district-of-columbia-latest.osm.pbf"
    ),
    crime=CrimeSource(
        kind="arcgis",
        # MPD publishes one feature layer per year plus a rolling 30-day layer.
        # The ingester discovers the layer index at runtime rather than
        # hardcoding it, because DC renumbers them when the year rolls over.
        url="https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/MapServer",
        has_shift_field=True,
    ),
    lights=LightSource(
        kind="arcgis",
        url=(
            "https://maps2.dcgis.dc.gov/dcgis/rest/services/DDOT/"
            "Streetlights/MapServer"
        ),
    ),
    transit_feeds=(
        TransitFeed(
            "wmata-rail",
            "https://api.wmata.com/gtfs/rail-gtfs-static.zip",
            needs_wmata_key=True,
        ),
        TransitFeed(
            "wmata-bus",
            "https://api.wmata.com/gtfs/bus-gtfs-static.zip",
            needs_wmata_key=True,
        ),
    ),
    crime_vocabulary=DC_CRIME,
    realtime="wmata",
)


# --------------------------------------------------------------------------
# New York City
# --------------------------------------------------------------------------

# NYPD's `ofns_desc` values. A different vocabulary from MPD's, weighted on the
# same principle: what does this tell a pedestrian about this street tonight.
#
# Two NYPD-specific judgements worth naming. "FELONY ASSAULT" is the violent
# one and sits with robbery; plain "ASSAULT 3 & RELATED OFFENSES" is a
# misdemeanour that covers a great deal of non-street conduct and is weighted
# far lower. And NYPD reports petit and grand larceny separately, which is a
# distinction about the value taken rather than about risk to a passer-by, so
# both sit near the bottom.
NYC_CRIME = CrimeVocabulary(
    severity={
        "MURDER & NON-NEGL. MANSLAUGHTER": 1.00,
        "RAPE": 1.00,
        # NYPD's "SEX CRIMES" is a much broader bucket than its name suggests
        # — forcible touching and third-degree sexual abuse sit in it
        # alongside far graver offences, and it is several times the size of
        # the rape category. Weighting it near homicide, as the first pass
        # did, made a handful of reports dominate every cell they appeared in
        # and produced sexual-offence shares that did not survive a sense
        # check against DC. It stays serious and stays in the sexual category;
        # it is no longer treated as equivalent to rape.
        "SEX CRIMES": 0.70,
        "FELONY ASSAULT": 0.90,
        "ROBBERY": 0.85,
        "KIDNAPPING & RELATED OFFENSES": 0.85,
        "ARSON": 0.25,
        "ASSAULT 3 & RELATED OFFENSES": 0.30,
        "DANGEROUS WEAPONS": 0.45,
        "BURGLARY": 0.15,
        "GRAND LARCENY OF MOTOR VEHICLE": 0.08,
        "GRAND LARCENY": 0.06,
        "PETIT LARCENY": 0.05,
        "CRIMINAL MISCHIEF & RELATED OF": 0.08,
    },
    pedestrian_relevance={
        "MURDER & NON-NEGL. MANSLAUGHTER": 1.00,
        "RAPE": 1.00,
        "SEX CRIMES": 1.00,
        "FELONY ASSAULT": 1.00,
        "ROBBERY": 1.00,
        "KIDNAPPING & RELATED OFFENSES": 0.90,
        "ARSON": 0.35,
        "ASSAULT 3 & RELATED OFFENSES": 0.70,
        "DANGEROUS WEAPONS": 0.80,
        "BURGLARY": 0.25,
        "GRAND LARCENY OF MOTOR VEHICLE": 0.45,
        "GRAND LARCENY": 0.55,
        "PETIT LARCENY": 0.55,
        "CRIMINAL MISCHIEF & RELATED OF": 0.45,
    },
    display_name={
        "MURDER & NON-NEGL. MANSLAUGHTER": "Homicide",
        # Deliberately the same label for both. NYPD splits rape from its
        # broader sexual-offence bucket; a person reading a map does not want
        # that distinction drawn for them in a two-line summary, and the
        # breakdown groups rows by display name so these merge into one.
        "RAPE": "Sexual offense",
        "SEX CRIMES": "Sexual offense",
        "FELONY ASSAULT": "Felony assault",
        "ROBBERY": "Robbery",
        "KIDNAPPING & RELATED OFFENSES": "Kidnapping",
        "ARSON": "Arson",
        "ASSAULT 3 & RELATED OFFENSES": "Assault",
        "DANGEROUS WEAPONS": "Weapons offense",
        "BURGLARY": "Burglary",
        "GRAND LARCENY OF MOTOR VEHICLE": "Vehicle theft",
        "GRAND LARCENY": "Theft",
        "PETIT LARCENY": "Petty theft",
        "CRIMINAL MISCHIEF & RELATED OF": "Criminal damage",
    },
    category={
        "MURDER & NON-NEGL. MANSLAUGHTER": "violent",
        "FELONY ASSAULT": "violent",
        "ROBBERY": "violent",
        "ASSAULT 3 & RELATED OFFENSES": "violent",
        "DANGEROUS WEAPONS": "violent",
        "KIDNAPPING & RELATED OFFENSES": "violent",
        "RAPE": "sexual",
        "SEX CRIMES": "sexual",
        "ARSON": "property",
        "BURGLARY": "property",
        "GRAND LARCENY OF MOTOR VEHICLE": "property",
        "GRAND LARCENY": "property",
        "PETIT LARCENY": "property",
        "CRIMINAL MISCHIEF & RELATED OF": "property",
    },
    # NYPD's weapon field is `pd_desc`-adjacent and inconsistently populated;
    # these are the values that appear often enough to be worth acting on.
    method_multiplier={
        "FIREARM": 1.40,
        "GUN": 1.40,
        "PISTOL": 1.40,
        "KNIFE/CUTTING INSTRUMENT": 1.20,
        "KNIFE": 1.20,
    },
)

NYC = City(
    slug="nyc",
    name="New York",
    region="New York City",
    # All five boroughs, with a collar into Hudson County and Yonkers so the
    # bridges and tunnels do not dead-end mid-span.
    bbox=BBox(min_lat=40.4700, min_lon=-74.2700, max_lat=40.9400, max_lon=-73.6800),
    timezone_name="America/New_York",
    origin=(40.7128, -73.9560),
    osm_extract_url=(
        "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf"
    ),
    crime=CrimeSource(
        kind="socrata",
        url="https://data.cityofnewyork.us",
        # Year-to-date first, then the historic file. NYPD splits them, and the
        # YTD dataset is the only one carrying the last few months.
        datasets=("5uac-w243", "qgea-i56i"),
        # NYPD stamps a time, not a shift.
        has_shift_field=False,
        # NYPD publishes `prem_typ_desc`, so incidents inside dwellings are
        # excluded. See PremisesWeights for why that is an exclusion rather
        # than a discount.
        premises=STREET_ONLY,
    ),
    lights=LightSource(
        kind="socrata",
        url="https://data.cityofnewyork.us",
        # DOT street light poles.
        datasets=("l63v-9uzt",),
    ),
    transit_feeds=(
        TransitFeed(
            "mta-subway",
            "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
        ),
        # The MTA splits buses by borough. Manhattan and Brooklyn alone are
        # most of the pedestrian-relevant network, but all five are listed so
        # a build is the whole city rather than an arbitrary part of it.
        TransitFeed("mta-bus-manhattan", "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_m.zip"),
        TransitFeed("mta-bus-brooklyn", "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_b.zip"),
        TransitFeed("mta-bus-queens", "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_q.zip"),
        TransitFeed("mta-bus-bronx", "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_bx.zip"),
        TransitFeed(
            "mta-bus-staten-island",
            "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_si.zip",
        ),
    ),
    crime_vocabulary=NYC_CRIME,
    realtime="mta",
)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

CITIES: dict[str, City] = {city.slug: city for city in (DC, NYC)}

DEFAULT_CITY = os.environ.get("GETMEHOME_DEFAULT_CITY", "dc")


def get_city(slug: str | None) -> City:
    """Look a city up by slug, falling back to the default.

    Unknown slugs raise rather than silently serving another city — routing
    someone through the wrong city's graph would produce confident nonsense.
    """
    if not slug:
        slug = DEFAULT_CITY
    key = slug.strip().lower()
    if key not in CITIES:
        known = ", ".join(sorted(CITIES))
        raise KeyError(f"unknown city {slug!r}; known cities are {known}")
    return CITIES[key]


def city_for_point(lat: float, lon: float) -> City | None:
    """The city whose bounding box contains a coordinate, if any.

    Used to notice that someone has selected a city they are not standing in,
    which is worth telling them rather than returning "no route found".
    """
    for city in CITIES.values():
        if city.bbox.contains(lat, lon):
            return city
    return None
