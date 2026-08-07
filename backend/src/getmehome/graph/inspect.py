"""Why a built city scores the way it does.

    python -m getmehome.graph.inspect --city nyc

Reads a graph that has already been built and reports the two things the build
summary compresses into a single number each: how much of the street network
the streetlight inventory actually reaches, and how fresh the crime data is.

It exists because ``median lighting score: 0.0`` is ambiguous in a way that
matters. Lighting is *ranked against the city* and ties take the lowest rank,
so every segment with no lamp within the search radius shares a score of
exactly zero. A median of zero therefore means "at least half the segments have
no lamp in range" — which has two completely different causes:

* the inventory did not ingest, and nothing is lit; or
* the inventory ingested fine but the graph covers ground it does not, which
  for New York is most of the difference between the five boroughs and a
  bounding box that also contains Newark, Jersey City, Hoboken, southern
  Westchester and western Nassau.

The first is a bug. The second is a boundary, and its consequence is subtler:
lighting ranks are computed over the whole graph, so hundreds of thousands of
segments that were never eligible for a New York lamp sit in the same
distribution as the streets being compared. The distance-to-nearest-lamp
breakdown below separates the two in one look.

Runs entirely offline against ``data/<city>/``. No network, no rebuild.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

import numpy as np
from scipy.spatial import cKDTree

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from ..config import (
    CRIME_POINTS_NAME,
    DATA_DIR,
    GRAPH_META_NAME,
    GRAPH_NAME,
    LIGHTING,
)
from ..ingest.streetlights import load_streetlights
from ..safety.hexgrid import CrimeIndex
from .model import WalkGraph

# Highway types reported separately. A footway through a park and a residential
# street are both "unlit" at zero, and only one of those is surprising.
INTERESTING_HIGHWAYS = (
    "residential", "footway", "sidewalk", "path", "service", "primary",
    "secondary", "tertiary", "living_street", "pedestrian", "steps",
)


def nearest_lamp_per_segment(graph: WalkGraph, tree: cKDTree) -> np.ndarray:
    """Distance in metres from each segment to its nearest lamp.

    Measured over **every vertex of the segment**, not a single representative
    point. That matters for the comparison this whole report turns on: the
    scorer samples along the polyline every few metres, so a long street whose
    far end passes a lamp is lit even though its first vertex is a hundred
    metres away. Measuring from one endpoint would manufacture a disagreement
    between "has a lamp in range" and "scores as lit" and make a correct build
    look broken.

    One tree query for every vertex in the city, then a segmented minimum via
    ``reduceat`` — no Python loop over segments.
    """
    ptr = graph.seg_geom_ptr.astype(np.int64)
    lat = graph.seg_geom[:, 0].astype(np.float64)
    lon = graph.seg_geom[:, 1].astype(np.float64)
    x, y = graph.projection.to_local(lat, lon)
    distances, _ = tree.query(
        np.column_stack([np.asarray(x), np.asarray(y)]), workers=-1
    )
    return np.minimum.reduceat(distances, ptr[:-1])


def lighting_report(city: City, graph: WalkGraph, root) -> None:
    lights_path = city.raw_dir(root) / "streetlights.json"
    n_segments = graph.n_segments
    lit = graph.seg_lit
    lit_share = float((lit > 0.0).mean())

    print(f"\n--- {city.name}: lighting ---")
    print(f"  segments in the graph      {n_segments:,}")
    print(f"  segments with a lamp in range  {(lit > 0).sum():,} ({lit_share:.1%})")
    print(f"  median lighting score      {float(np.median(lit)):.3f}")
    print(
        "  (the score is a rank, and every segment with no lamp within "
        f"{LIGHTING.search_radius_m:.0f} m ties at 0.0 — so the median is zero "
        "whenever that group is more than half the graph)"
    )

    if not lights_path.exists():
        print(
            f"\n  No {lights_path} — cannot say whether the lamps are missing "
            f"or merely far away. Re-run the build with --refresh."
        )
        return

    lights = load_streetlights(lights_path)
    print(f"\n  lamps ingested             {len(lights):,}")
    if not lights:
        print(
            "  Zero lamps. This is the bug case: night scores are crime-only "
            "and cannot tell a lit street from a dark one.\n"
            f"  Run: python -m getmehome.ingest.probe --city {city.slug} --lights"
        )
        return

    lamp_lat = np.array([lamp.lat for lamp in lights])
    lamp_lon = np.array([lamp.lon for lamp in lights])
    print(
        f"  lamp bounds                "
        f"{lamp_lat.min():.3f}..{lamp_lat.max():.3f} N, "
        f"{lamp_lon.min():.3f}..{lamp_lon.max():.3f} W"
    )

    seg_lat = graph.seg_geom[:, 0]
    seg_lon = graph.seg_geom[:, 1]
    print(
        f"  segment bounds             "
        f"{seg_lat.min():.3f}..{seg_lat.max():.3f} N, "
        f"{seg_lon.min():.3f}..{seg_lon.max():.3f} W"
    )

    # Distance from every segment to its nearest lamp. This is the number that
    # separates the two explanations: a parsing or units error puts lamps and
    # streets in the same place but scores nothing, while a coverage gap shows
    # up as a long tail of segments kilometres from the nearest lamp.
    lx, ly = graph.projection.to_local(lamp_lat, lamp_lon)
    tree = cKDTree(np.column_stack([np.asarray(lx), np.asarray(ly)]))
    nearest = nearest_lamp_per_segment(graph, tree)

    print("\n  distance to the nearest lamp, by percentile:")
    for p in (10, 25, 50, 75, 90, 99):
        print(f"    p{p:<3} {np.percentile(nearest, p):>10,.0f} m")

    # The verdict.
    #
    # A segment scores above zero if and only if some lamp falls inside the
    # search radius, so `in_range` and `lit_share` are the same quantity
    # measured two ways — one from the lamp positions, one from the scored
    # graph. Agreement means the scoring is doing what the geometry says, and
    # the whole story is then about where the lamps are. Disagreement is the
    # only result here that implicates the model itself.
    in_range = float((nearest <= LIGHTING.search_radius_m).mean())
    far = float((nearest > 500.0).mean())
    print(f"\n  within the {LIGHTING.search_radius_m:.0f} m search radius  {in_range:.1%}")
    print(f"  over 500 m from any lamp    {far:.1%}")

    print("\n  Reading:")
    if abs(in_range - lit_share) > 0.02:
        print(
            f"    {in_range:.1%} of segments have a lamp in range but "
            f"{lit_share:.1%} score as lit. Those should match — the "
            "discrepancy is in the scoring, not the data. Look at\n"
            "    sample_polyline and the lamp height/lumen parsing."
        )
    elif far > 0.1:
        print(
            f"    {far:.1%} of segments are more than half a kilometre from "
            "the nearest lamp. That is a coverage boundary rather than a\n"
            "    parsing failure: the graph reaches ground the inventory does "
            "not. For New York that is mostly New Jersey, Westchester and\n"
            "    Nassau, all inside the bounding box and none of them carrying "
            "NYC DOT lamps. The lighting rank is computed over every\n"
            "    segment here, so those are diluting the comparison between "
            "the streets you actually route on."
        )
    else:
        print(
            "    Lamps and streets are in the same places and the scores "
            "agree with the geometry. A low lit share here is simply how\n"
            "    sparsely this city lights the network the graph includes — "
            "footpaths and service roads carry most of it (see below)."
        )

    # Which kinds of street are unlit. A park path scoring zero is expected; a
    # residential street scoring zero is the thing worth knowing about.
    print("\n  share with a lamp in range, by street type:")
    for name in INTERESTING_HIGHWAYS:
        if name not in graph.highways:
            continue
        mask = graph.seg_highway == graph.highways.index(name)
        count = int(mask.sum())
        if count < 50:
            continue
        print(f"    {name:<14} {float((lit[mask] > 0).mean()):>6.1%}  of {count:>9,}")


def crime_report(city: City, graph: WalkGraph, root) -> None:
    points = city.build_dir(root) / CRIME_POINTS_NAME
    print(f"\n--- {city.name}: crime ---")
    if not points.exists():
        print(f"  No {points} — the map grid will be empty. Rebuild.")
        return

    index = CrimeIndex.load(points)
    latest = index.latest
    print(f"  incidents held             {index.count:,}")
    if latest is None:
        print("  No dated incidents at all.")
        return

    age = (datetime.now(UTC) - latest).days
    print(f"  newest incident            {latest.date().isoformat()} ({age} days ago)")
    print("\n  incidents inside each lookback window:")
    for window in graph.crime_windows:
        cutoff = datetime.now(UTC).timestamp() - window * 86400.0
        n = int((index.timestamps >= cutoff).sum())
        flag = "   <- empty, the feed is further behind than this" if n == 0 else ""
        print(f"    {window:>4}d  {n:>9,}{flag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default=DEFAULT_CITY, choices=sorted(CITIES))
    parser.add_argument("--data", default=str(DATA_DIR))
    args = parser.parse_args(argv)

    from pathlib import Path  # noqa: PLC0415 — only needed for the CLI

    root = Path(args.data)
    city = get_city(args.city)
    graph_path = city.build_dir(root) / GRAPH_NAME
    if not graph_path.exists():
        print(
            f"No graph at {graph_path}. Build it first:\n"
            f"    make graph CITY={city.slug}"
        )
        return 1

    graph = WalkGraph.load(graph_path, city.build_dir(root) / GRAPH_META_NAME)
    lighting_report(city, graph, root)
    crime_report(city, graph, root)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
