"""The data pipeline: raw sources in, scored routable graph out.

Run as ``python -m getmehome.graph.build``. Each stage caches its raw download
under ``data/raw`` so a re-run after a code change does not re-fetch tens of
thousands of crime records.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from ..config import (
    BUILD_DIR,
    CRIME,
    DC_BBOX,
    GRAPH_FILE,
    GRAPH_META_FILE,
    OSM_EXTRACT_URL,
    RAW_DIR,
    TRANSIT_FILE,
)
from ..ingest.crime import fetch_crime, load_crime, save_crime
from ..ingest.gtfs import load_gtfs
from ..ingest.osm import download_extract, read_osm
from ..ingest.streetlights import fetch_streetlights, load_streetlights, save_streetlights
from ..safety.cameras import AlprCamera
from ..safety.scoring import apply_scores
from .model import WalkGraph, build_graph, largest_connected_component

log = logging.getLogger("getmehome.build")

OSM_FILE = RAW_DIR / "district-of-columbia-latest.osm.pbf"
LIGHTS_FILE = RAW_DIR / "streetlights.json"
CRIME_FILE = RAW_DIR / "crime.json"
CAMERAS_FILE = RAW_DIR / "cameras.json"
GTFS_BUS = RAW_DIR / "wmata-bus-gtfs.zip"
GTFS_RAIL = RAW_DIR / "wmata-rail-gtfs.zip"


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
    refresh: bool = False,
    skip_transit: bool = False,
    osm_path: Path | None = None,
) -> WalkGraph:
    """Run the whole pipeline and write the graph to disk."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # --- OSM ----------------------------------------------------------
    source = osm_path or OSM_FILE
    if not source.exists():
        log.info("downloading OSM extract from %s", OSM_EXTRACT_URL)
        download_extract(OSM_EXTRACT_URL, source)
    log.info("parsing %s", source)
    node_coords, segments, osm_cameras = read_osm(source, bbox=DC_BBOX)
    log.info(
        "parsed %d nodes, %d walkable segments, %d ALPR cameras",
        len(node_coords), len(segments), len(osm_cameras),
    )
    if not segments:
        raise RuntimeError("no walkable segments parsed — is the extract valid?")

    # --- streetlights --------------------------------------------------
    if refresh or not LIGHTS_FILE.exists():
        log.info("fetching DDOT streetlights")
        lights = fetch_streetlights()
        save_streetlights(lights, LIGHTS_FILE)
    else:
        lights = load_streetlights(LIGHTS_FILE)
    log.info("%d streetlights", len(lights))

    # --- crime ---------------------------------------------------------
    if refresh or not CRIME_FILE.exists():
        log.info("fetching MPD crime incidents (%d years)", CRIME.history_years)
        incidents = fetch_crime()
        save_crime(incidents, CRIME_FILE)
    else:
        incidents = load_crime(CRIME_FILE)
    log.info("%d crime incidents", len(incidents))

    # --- extra cameras -------------------------------------------------
    cameras = list(osm_cameras)
    if CAMERAS_FILE.exists():
        extra = [
            AlprCamera(
                lat=r["lat"],
                lon=r["lon"],
                direction_deg=r.get("direction"),
                operator=r.get("operator", ""),
                osm_id=r.get("id", ""),
            )
            for r in json.loads(CAMERAS_FILE.read_text())
        ]
        # De-duplicate against the OSM set on rounded position — the same
        # camera is often present in both sources.
        seen = {(round(c.lat, 5), round(c.lon, 5)) for c in cameras}
        for c in extra:
            key = (round(c.lat, 5), round(c.lon, 5))
            if key not in seen:
                seen.add(key)
                cameras.append(c)
        log.info("%d cameras after merging %s", len(cameras), CAMERAS_FILE.name)

    # --- compile + prune + score ---------------------------------------
    graph = build_graph(node_coords, segments)
    node_coords, segments = _prune_to_main_component(node_coords, segments, graph)
    graph = build_graph(
        node_coords,
        segments,
        meta={
            "built_at": datetime.now(timezone.utc).isoformat(),
            "n_lights": len(lights),
            "n_incidents": len(incidents),
            "n_cameras": len(cameras),
            "crime_history_years": CRIME.history_years,
            "bbox": [DC_BBOX.min_lat, DC_BBOX.min_lon, DC_BBOX.max_lat, DC_BBOX.max_lon],
        },
    )

    log.info("scoring %d segments", graph.n_segments)
    apply_scores(graph, lights, incidents, cameras)

    graph.meta["lit_median"] = round(float(np.median(graph.seg_lit)), 3)
    graph.meta["crime_night_median"] = round(
        float(np.median(graph.seg_crime_night)), 3
    )
    graph.meta["camera_exposed_segments"] = int((graph.seg_camera > 0.15).sum())

    graph.save(GRAPH_FILE, GRAPH_META_FILE)
    log.info("wrote %s (%.1f MB)", GRAPH_FILE, GRAPH_FILE.stat().st_size / 1e6)

    (BUILD_DIR / "cameras.json").write_text(
        json.dumps([c.to_dict() for c in cameras])
    )

    # --- transit -------------------------------------------------------
    if not skip_transit:
        feeds = [p for p in (GTFS_RAIL, GTFS_BUS) if p.exists()]
        if feeds:
            networks = [load_gtfs(p, service_date=date.today()) for p in feeds]
            merged = _merge_networks(networks)
            with TRANSIT_FILE.open("wb") as fh:
                pickle.dump(merged, fh)
            log.info(
                "wrote %s: %d stops, %d patterns",
                TRANSIT_FILE, merged.n_stops, len(merged.patterns),
            )
        else:
            log.warning(
                "no GTFS feeds in %s — transit routing will be unavailable. "
                "Download them with `make gtfs` (needs WMATA_API_KEY).",
                RAW_DIR,
            )

    return graph


def _merge_networks(networks: list):
    """Combine several GTFS feeds into one network.

    WMATA ships rail and bus as separate feeds, and a journey that takes the
    Metro then a bus needs both in one timetable or RAPTOR can never transfer
    between them.
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
    for net, mapping in zip(networks, remap):
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
        Path("/nonexistent"), stops, merged.stop_index, TRANSIT.max_transfer_walk_m
    )
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the DC safety-routing graph")
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

    try:
        graph = build(
            refresh=args.refresh, skip_transit=args.skip_transit, osm_path=args.osm
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        log.error("build failed: %s", exc)
        return 1

    print(
        f"\nBuilt graph: {graph.n_nodes:,} nodes, {graph.n_segments:,} segments\n"
        f"  median lighting score : {graph.meta.get('lit_median')}\n"
        f"  median night crime    : {graph.meta.get('crime_night_median')}\n"
        f"  camera-exposed segs   : {graph.meta.get('camera_exposed_segments'):,}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
