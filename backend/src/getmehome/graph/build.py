"""The data pipeline: raw sources in, scored routable graph out.

Run as ``python -m getmehome.graph.build --city dc``. Each stage caches its
raw download under ``data/<slug>/raw`` so a re-run after a code change does
not re-fetch tens of thousands of crime records.

Everything is scoped to one city. Downloads, build artefacts and the crime
vocabulary all come off the :class:`~getmehome.cities.City` record, so two
cities cannot overwrite each other's data and a city's incidents cannot be
scored against another's offence table.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from ..config import (
    CAMERAS_NAME,
    CRIME,
    CRIME_POINTS_NAME,
    DATA_DIR,
    GRAPH_META_NAME,
    GRAPH_NAME,
    PLACES_NAME,
    TRANSIT_NAME,
    WMATA_API_KEY,
)
from ..ingest.crime import fetch_crime, load_crime, save_crime
from ..ingest.gtfs import load_gtfs
from ..ingest.osm import download_extract, read_osm
from ..ingest.socrata import fetch_socrata_crime, fetch_socrata_streetlights
from ..ingest.streetlights import fetch_streetlights, load_streetlights, save_streetlights
from ..places import PlaceIndex
from ..safety.cameras import AlprCamera
from ..safety.hexgrid import CrimeIndex
from ..safety.scoring import apply_scores
from .model import WalkGraph, build_graph, largest_connected_component

log = logging.getLogger("getmehome.build")


@dataclass
class Paths:
    """Every file a build reads or writes, for one city."""

    city: City
    root: Path = DATA_DIR

    @property
    def raw(self) -> Path:
        return self.city.raw_dir(self.root)

    @property
    def build(self) -> Path:
        return self.city.build_dir(self.root)

    @property
    def osm(self) -> Path:
        # Named after the extract rather than the city, so pointing two cities
        # at one regional extract reuses the download instead of fetching it
        # twice. NYC's Geofabrik file covers the whole state.
        return self.raw / Path(self.city.osm_extract_url).name

    @property
    def lights(self) -> Path:
        return self.raw / "streetlights.json"

    @property
    def crime(self) -> Path:
        return self.raw / "crime.json"

    @property
    def extra_cameras(self) -> Path:
        return self.raw / "cameras.json"

    def gtfs(self, feed_name: str) -> Path:
        return self.raw / f"{feed_name}.zip"


def _prune_to_main_component(
    node_coords: dict, segments: list, graph: WalkGraph
) -> tuple[dict, list]:
    """Keep only segments in the graph's largest connected component.

    Extracts always contain orphans — a path clipped at the bbox edge, a
    mis-tagged driveway. Leaving them in means a user who happens to be
    standing on one gets "no route found" with no explanation.
    """
    keep_nodes = largest_connected_component(graph)

    # Map dense graph node indices back to the original OSM ids.
    used = sorted({s.node_a for s in segments} | {s.node_b for s in segments})
    keep_osm = {used[i] for i in range(len(used)) if keep_nodes[i]}

    kept = [s for s in segments if s.node_a in keep_osm and s.node_b in keep_osm]
    dropped = len(segments) - len(kept)
    if dropped:
        log.info(
            "dropped %d disconnected segments (%.1f%%)",
            dropped,
            100.0 * dropped / max(1, len(segments)),
        )
    coords = {k: v for k, v in node_coords.items() if k in keep_osm}
    return coords, kept


def build(
    city: City,
    refresh: bool = False,
    skip_transit: bool = False,
    osm_path: Path | None = None,
    data_root: Path = DATA_DIR,
) -> WalkGraph:
    """Run the whole pipeline for one city and write its graph to disk."""
    paths = Paths(city, data_root)
    paths.raw.mkdir(parents=True, exist_ok=True)
    paths.build.mkdir(parents=True, exist_ok=True)

    # Fail before the expensive part rather than after it: an offence present
    # in one of the vocabulary's four tables and missing from another scores
    # at the default weight, which looks like data rather than like a bug.
    problems = city.crime_vocabulary.check()
    if problems:
        raise RuntimeError(
            f"{city.slug}: inconsistent crime vocabulary — " + "; ".join(problems)
        )

    projection = city.projection

    # --- OSM ----------------------------------------------------------
    source = osm_path or paths.osm
    if not source.exists():
        log.info("downloading OSM extract from %s", city.osm_extract_url)
        download_extract(city.osm_extract_url, source)
    log.info("parsing %s", source)
    node_coords, segments, osm_cameras, places = read_osm(source, bbox=city.bbox)
    log.info(
        "parsed %d nodes, %d walkable segments, %d cameras, %d named places",
        len(node_coords), len(segments), len(osm_cameras), len(places),
    )
    if not segments:
        raise RuntimeError(
            f"no walkable segments parsed for {city.name} — does the extract "
            f"cover {city.bbox.as_list()}?"
        )

    # --- streetlights --------------------------------------------------
    if refresh or not paths.lights.exists():
        log.info("fetching streetlights (%s)", city.lights.kind)
        lights = _fetch_lights(city)
        save_streetlights(lights, paths.lights)
    else:
        lights = load_streetlights(paths.lights)
    log.info("%d streetlights", len(lights))

    # --- crime ---------------------------------------------------------
    if refresh or not paths.crime.exists():
        log.info(
            "fetching crime incidents (%s, %d years)",
            city.crime.kind, CRIME.history_years,
        )
        incidents = _fetch_crime(city)
        save_crime(incidents, paths.crime)
    else:
        incidents = load_crime(paths.crime)
    log.info("%d crime incidents", len(incidents))

    _warn_on_unknown_offences(city, incidents)

    # --- extra cameras -------------------------------------------------
    cameras = list(osm_cameras)
    if paths.extra_cameras.exists():
        extra = [
            AlprCamera(
                lat=r["lat"],
                lon=r["lon"],
                direction_deg=r.get("direction"),
                operator=r.get("operator", ""),
                osm_id=r.get("id", ""),
            )
            for r in json.loads(paths.extra_cameras.read_text())
        ]
        # De-duplicate against the OSM set on rounded position — the same
        # camera is often present in both sources.
        seen = {(round(c.lat, 5), round(c.lon, 5)) for c in cameras}
        for c in extra:
            key = (round(c.lat, 5), round(c.lon, 5))
            if key not in seen:
                seen.add(key)
                cameras.append(c)
        log.info("%d cameras after merging %s", len(cameras), paths.extra_cameras.name)

    # --- compile + prune + score ---------------------------------------
    graph = build_graph(node_coords, segments, projection)
    node_coords, segments = _prune_to_main_component(node_coords, segments, graph)
    graph = build_graph(
        node_coords,
        segments,
        projection,
        meta={
            "city": city.slug,
            "city_name": city.name,
            "built_at": datetime.now(UTC).isoformat(),
            "n_lights": len(lights),
            "n_incidents": len(incidents),
            "n_cameras": len(cameras),
            "n_places": len(places),
            "crime_history_years": CRIME.history_years,
            "bbox": city.bbox.as_list(),
        },
    )

    log.info(
        "scoring %d segments across %d crime windows",
        graph.n_segments, len(CRIME.windows_days),
    )
    apply_scores(
        graph,
        lights,
        incidents,
        cameras,
        progress=log.info,
        vocabulary=city.crime_vocabulary,
    )

    graph.meta["lit_median"] = round(float(np.median(graph.seg_lit)), 3)
    graph.meta["crime_windows"] = list(graph.crime_windows)
    graph.meta["crime_night_median"] = round(
        float(np.median(graph.seg_crime_night)), 3
    )
    graph.meta["camera_exposed_segments"] = int((graph.seg_camera > 0.15).sum())

    # Night risk spread, recorded so a build can be checked against the
    # complaint that started this: if the interquartile range is tiny, the
    # score is not discriminating between routes and the weights need work.
    night_risk = graph.segment_risk(is_night=True)
    q1, q3 = (float(v) for v in np.percentile(night_risk, [25, 75]))
    graph.meta["night_risk_p25"] = round(q1, 3)
    graph.meta["night_risk_p75"] = round(q3, 3)

    graph_file = paths.build / GRAPH_NAME
    graph.save(graph_file, paths.build / GRAPH_META_NAME)
    log.info("wrote %s (%.1f MB)", graph_file, graph_file.stat().st_size / 1e6)

    (paths.build / CAMERAS_NAME).write_text(
        json.dumps([c.to_dict() for c in cameras])
    )

    places_file = paths.build / PLACES_NAME
    PlaceIndex(places=places).save(places_file)
    log.info("wrote %s: %d searchable places", places_file, len(places))

    points_file = paths.build / CRIME_POINTS_NAME
    CrimeIndex.from_incidents(
        incidents,
        projection=projection,
        vocabulary=city.crime_vocabulary,
        city_slug=city.slug,
    ).save(points_file)
    log.info("wrote %s (%.1f MB)", points_file, points_file.stat().st_size / 1e6)

    # --- transit -------------------------------------------------------
    if not skip_transit:
        _build_transit(city, paths, projection)

    return graph


def _fetch_lights(city: City):
    """Pick the streetlight adapter from the platform, not from the city."""
    if city.lights.kind == "socrata":
        return fetch_socrata_streetlights(city)
    return fetch_streetlights(city.lights.url)


def _fetch_crime(city: City):
    if city.crime.kind == "socrata":
        return fetch_socrata_crime(city)
    return fetch_crime(city.crime.url)


def _warn_on_unknown_offences(city: City, incidents: list) -> None:
    """Report offences the vocabulary has no weight for.

    Not fatal — a feed can add a category at any time and the default weight is
    a reasonable guess — but it is exactly the symptom of a vocabulary written
    against the wrong city, so it needs to be visible in the build log rather
    than discovered from a strange-looking map.
    """
    known = set(city.crime_vocabulary.severity)
    unknown: dict[str, int] = {}
    for incident in incidents:
        name = (incident.offense or "").strip().upper()
        if name and name not in known:
            unknown[name] = unknown.get(name, 0) + 1
    if not unknown:
        return

    share = sum(unknown.values()) / max(1, len(incidents))
    top = sorted(unknown.items(), key=lambda kv: -kv[1])[:8]
    log.warning(
        "%.1f%% of incidents have no weight in the %s vocabulary and fall back "
        "to the default. Commonest: %s",
        100 * share, city.slug, ", ".join(f"{n} ({c})" for n, c in top),
    )
    if share > 0.5:
        log.error(
            "More than half the incidents are unweighted. That usually means "
            "the vocabulary belongs to another city, or the feed's offence "
            "column has been renamed — check cities.%s.", city.slug.upper(),
        )


def _build_transit(city: City, paths: Paths, projection) -> None:
    """Load and merge whatever GTFS feeds are present for a city."""
    present = [
        (feed, paths.gtfs(feed.name))
        for feed in city.transit_feeds
        if paths.gtfs(feed.name).exists()
    ]
    if not present:
        needs_key = any(f.needs_wmata_key for f in city.transit_feeds)
        log.warning(
            "no GTFS feeds in %s — transit routing will be unavailable. "
            "Download them with `make gtfs CITY=%s`%s.",
            paths.raw,
            city.slug,
            " (needs WMATA_API_KEY)" if needs_key and not WMATA_API_KEY else "",
        )
        return

    networks = [
        load_gtfs(path, projection, service_date=date.today())
        for _, path in present
    ]
    merged = _merge_networks(networks, projection)
    transit_file = paths.build / TRANSIT_NAME
    with transit_file.open("wb") as fh:
        pickle.dump(merged, fh)
    log.info(
        "wrote %s: %d stops, %d patterns from %d feeds",
        transit_file, merged.n_stops, len(merged.patterns), len(networks),
    )


def _merge_networks(networks: list, projection):
    """Combine several GTFS feeds into one network.

    Both agencies ship more than one feed and a journey that uses two of them
    needs a single timetable, or RAPTOR can never transfer between them. WMATA
    splits rail from bus; the MTA splits the subway from five borough bus
    feeds, so New York merges six.
    """
    from ..ingest.gtfs import TransitNetwork, _build_transfers  # noqa: PLC0415

    if len(networks) == 1:
        return networks[0]

    stops = []
    stop_index: dict[str, int] = {}
    remap: list[dict[int, int]] = []

    for net in networks:
        mapping: dict[int, int] = {}
        for old, stop in enumerate(net.stops):
            # Feeds can reuse ids, so namespace them by stop name + position.
            key = f"{stop.stop_id}@{round(stop.lat, 5)},{round(stop.lon, 5)}"
            idx = stop_index.get(key)
            if idx is None:
                idx = len(stops)
                stop_index[key] = idx
                stops.append(stop)
            mapping[old] = idx
        remap.append(mapping)

    patterns = []
    stop_patterns: dict[int, list[tuple[int, int]]] = {}
    for net, mapping in zip(networks, remap, strict=True):
        for p in net.patterns:
            p.pattern_id = len(patterns)
            p.stops = [mapping[s] for s in p.stops]
            patterns.append(p)
            for pos, s in enumerate(p.stops):
                stop_patterns.setdefault(s, []).append((p.pattern_id, pos))

    from ..config import TRANSIT

    merged = TransitNetwork(
        stops=stops,
        stop_index={s.stop_id: i for i, s in enumerate(stops)},
        patterns=patterns,
        stop_patterns=stop_patterns,
        transfers={},
    )
    # Rebuild transfers across the combined stop set so rail-to-bus links exist.
    merged.transfers = _build_transfers(
        Path("/nonexistent"),
        stops,
        merged.stop_index,
        TRANSIT.max_transfer_walk_m,
        projection,
    )
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a city's safety-routing graph")
    parser.add_argument(
        "--city",
        default=DEFAULT_CITY,
        choices=sorted(CITIES),
        help=f"which city to build (default: {DEFAULT_CITY})",
    )
    parser.add_argument(
        "--all", action="store_true", help="build every known city in turn"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-download streetlights and crime"
    )
    parser.add_argument(
        "--skip-transit", action="store_true", help="skip building the GTFS timetable"
    )
    parser.add_argument("--osm", type=Path, help="path to an .osm.pbf to use")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.all and args.osm:
        parser.error("--osm names one extract, so it cannot be used with --all")

    targets = sorted(CITIES) if args.all else [args.city]
    failed: list[str] = []

    for slug in targets:
        city = get_city(slug)
        log.info("=== building %s (%s) ===", city.name, city.slug)
        try:
            graph = build(
                city,
                refresh=args.refresh,
                skip_transit=args.skip_transit,
                osm_path=args.osm,
            )
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            log.error("%s build failed: %s", slug, exc)
            failed.append(slug)
            # With --all, one city failing should not lose the others: a build
            # is tens of minutes and the second city may be fine.
            continue

        print(
            f"\n{city.name}: {graph.n_nodes:,} nodes, {graph.n_segments:,} segments\n"
            f"  median lighting score : {graph.meta.get('lit_median')}\n"
            f"  median night crime    : {graph.meta.get('crime_night_median')}\n"
            f"  night risk p25 - p75  : {graph.meta.get('night_risk_p25')}"
            f" - {graph.meta.get('night_risk_p75')}\n"
            f"  crime windows (days)  : {graph.meta.get('crime_windows')}\n"
            f"  camera-exposed segs   : {graph.meta.get('camera_exposed_segments'):,}\n"
        )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
