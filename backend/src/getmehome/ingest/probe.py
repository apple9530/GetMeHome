"""Show what a city's data feeds actually publish.

    python -m getmehome.ingest.probe --city nyc

Every ingester in this package resolves each field it needs through a list of
candidate column names, because open-data portals rename things. That design
degrades gracefully — a renamed column becomes a missing value rather than a
crash — and the cost of it is that a wrong guess is *silent*. A streetlight
dataset whose coordinate lives somewhere unexpected does not fail the build; it
produces zero lamps, and the night score quietly stops distinguishing a lit
street from an unlit one.

This prints one row of each configured dataset and says, per field, which
candidate matched and what it matched to. It is the fastest way to answer "is
the ingester reading this feed correctly", and it needs no build, no cache and
no graph — just network access to the portal.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from .socrata import (
    _GEOM_COLUMNS,
    _LIGHT_HEIGHT,
    _LIGHT_LAT,
    _LIGHT_LON,
    _LIGHT_TYPE,
    _LIGHT_WATT,
    _NYPD_DATE,
    _NYPD_LAT,
    _NYPD_LON,
    _NYPD_OFFENSE,
    _NYPD_PREMISES,
    _NYPD_TIME,
    SocrataError,
    coordinates_of,
    fetch_rows,
    pick,
)

log = logging.getLogger("getmehome.probe")

#: What each ingester looks for, under which candidate names, and whether its
#: absence is a real problem. Several fields are genuinely optional — NYC
#: records almost no lamp wattage, and the model does not need it — so they are
#: reported but do not count against the exit status. A missing coordinate or
#: offence name does.
CRIME_FIELDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("offense", _NYPD_OFFENSE, True),
    ("date", _NYPD_DATE, True),
    ("time", _NYPD_TIME, False),
    ("latitude", _NYPD_LAT, True),
    ("longitude", _NYPD_LON, True),
    ("premises", _NYPD_PREMISES, True),
)

LIGHT_FIELDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("latitude", _LIGHT_LAT, True),
    ("longitude", _LIGHT_LON, True),
    ("lamp type", _LIGHT_TYPE, False),
    ("wattage", _LIGHT_WATT, False),
    ("pole height", _LIGHT_HEIGHT, False),
)


def _matched(row: dict, candidates: tuple[str, ...]) -> tuple[str, object] | None:
    """Which candidate name is populated on this row, and its value."""
    lowered = {str(k).lower(): (k, v) for k, v in row.items()}
    for name in candidates:
        hit = lowered.get(name.lower())
        if hit is not None and hit[1] not in (None, ""):
            return hit
    return None


def _describe(row: dict, fields, label: str) -> bool:
    """Print one row's field resolution. Returns True if anything was missed."""
    print(f"\n  Columns present ({len(row)}):")
    print("    " + ", ".join(sorted(row)))
    print(f"\n  What the {label} ingester resolves:")

    missing = []
    for name, candidates, required in fields:
        hit = _matched(row, candidates)
        if hit is None:
            if required:
                missing.append(name)
            note = "MISSING" if required else "absent (optional)"
            print(f"    {name:<14} {note} — tried {', '.join(candidates)}")
        else:
            column, value = hit
            text = str(value)
            if len(text) > 60:
                text = text[:57] + "…"
            print(f"    {name:<14} {column} = {text}")

    geom = pick(row, _GEOM_COLUMNS)
    if geom is not None:
        print(f"    {'geometry':<14} present ({json.dumps(geom)[:60]}…)")

    # The one that decides whether the row survives at all: named columns
    # first, then the geometry fallback, exactly as the ingester does it.
    named = {name: candidates for name, candidates, _ in fields}
    point = coordinates_of(row, named.get("latitude", ()), named.get("longitude", ()))
    if point is not None:
        print(f"    {'-> coordinate':<14} {point[0]:.5f}, {point[1]:.5f}")
        # A resolved coordinate makes a missing lat/lon column a non-problem.
        missing = [m for m in missing if m not in ("latitude", "longitude")]
    else:
        print(
            f"    {'-> coordinate':<14} NONE — this row would be skipped, and "
            "a dataset of rows like it ingests to nothing"
        )
        missing.append("coordinate")
    return bool(missing)


def probe(city: City, crime: bool = True, lights: bool = True) -> int:
    """Fetch one row from each configured dataset and describe it."""
    problems = 0

    sources = []
    if crime:
        sources.append(("crime", city.crime, CRIME_FIELDS))
    if lights:
        sources.append(("streetlight", city.lights, LIGHT_FIELDS))

    for label, source, fields in sources:
        if source.kind != "socrata":
            print(
                f"\n=== {city.name} {label} ===\n"
                f"  {source.kind} feed at {source.url} — this probe only reads "
                f"Socrata portals."
            )
            continue

        for dataset in source.datasets:
            print(f"\n=== {city.name} {label}: {source.url}/resource/{dataset} ===")
            try:
                rows = fetch_rows(source.url, dataset, limit=1)
            except SocrataError as exc:
                print(f"  UNREACHABLE: {exc}")
                problems += 1
                continue
            if not rows:
                print("  The dataset returned no rows at all.")
                problems += 1
                continue
            if _describe(rows[0], fields, label):
                problems += 1

    return problems


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default=DEFAULT_CITY, choices=sorted(CITIES))
    parser.add_argument(
        "--crime", action="store_true", help="probe only the crime feed"
    )
    parser.add_argument(
        "--lights", action="store_true", help="probe only the streetlight feed"
    )
    args = parser.parse_args(argv)

    # No flag means both, which is what you want the first time.
    both = not (args.crime or args.lights)
    problems = probe(
        get_city(args.city),
        crime=both or args.crime,
        lights=both or args.lights,
    )

    if problems:
        print(
            f"\n{problems} dataset(s) had a field the ingester could not "
            f"resolve. Add the real column name to the matching tuple in "
            f"getmehome/ingest/socrata.py."
        )
    else:
        print("\nEvery field resolved. The ingester is reading these feeds correctly.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
