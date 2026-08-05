"""Extract the pedestrian network and ALPR cameras from an OSM extract.

Two passes over the .osm.pbf:

1. Count how many walkable ways reference each node, and collect surveillance
   nodes. No coordinates needed, so this pass is fast.
2. Re-read the ways with node locations resolved, splitting each way wherever
   it touches a node shared with another way.

The split is the important part. An OSM way runs from one named-street change
to the next and can cross a dozen intersections on the way, so routing on
un-split ways would let a route pass straight through junctions without being
able to turn at them.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import osmium

from ..config import FORBIDDEN_HIGHWAYS, WALKABLE_HIGHWAYS, BBox
from ..geo import polyline_length_m
from ..graph.model import RawSegment
from ..safety.cameras import AlprCamera, parse_direction

# Access tag values that forbid pedestrians.
_NO_FOOT = {"no", "private", "customers", "permit"}


def is_walkable(tags: dict[str, str]) -> bool:
    """Whether a way carries pedestrians."""
    highway = tags.get("highway", "")
    if highway in FORBIDDEN_HIGHWAYS:
        # A motorway with an explicit sidewalk is still walkable alongside.
        return tags.get("sidewalk") in ("both", "left", "right", "yes")
    if highway not in WALKABLE_HIGHWAYS:
        return False
    if tags.get("foot") in _NO_FOOT:
        return False
    if tags.get("access") in _NO_FOOT and tags.get("foot") not in ("yes", "designated"):
        return False
    # Mapped building outlines and squares tagged as areas are not linear ways.
    if tags.get("area") == "yes":
        return False
    return True


def is_alpr(tags: dict[str, str]) -> bool:
    """Whether a node is a mapped automated licence-plate reader.

    Follows the DeFlock tagging convention. ``surveillance:type=ALPR`` is the
    definitive marker; the operator check picks up nodes tagged before that
    key was in common use.
    """
    if tags.get("man_made") != "surveillance":
        return False
    if tags.get("surveillance:type", "").upper() == "ALPR":
        return True
    operator = tags.get("operator", "").lower()
    return "flock" in operator or "motorola" in operator and "alpr" in operator


def _tags_to_dict(obj) -> dict[str, str]:
    return {t.k: t.v for t in obj.tags}


def read_osm(
    path: Path, bbox: BBox | None = None
) -> tuple[dict[int, tuple[float, float]], list[RawSegment], list[AlprCamera]]:
    """Parse an extract into (node coords, walk segments, ALPR cameras)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"OSM extract not found: {path}")

    # --- pass 1: node reference counts + surveillance nodes ---------------
    ref_count: dict[int, int] = defaultdict(int)
    cameras: list[AlprCamera] = []

    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            tags = _tags_to_dict(obj)
            if is_alpr(tags):
                lat, lon = obj.location.lat, obj.location.lon
                if bbox is None or bbox.contains(lat, lon):
                    cameras.append(
                        AlprCamera(
                            lat=lat,
                            lon=lon,
                            direction_deg=parse_direction(
                                tags.get("direction")
                                or tags.get("camera:direction")
                                or tags.get("surveillance:direction")
                            ),
                            operator=tags.get("operator", ""),
                            osm_id=f"node/{obj.id}",
                        )
                    )
        elif obj.is_way():
            tags = _tags_to_dict(obj)
            if not is_walkable(tags):
                continue
            refs = [n.ref for n in obj.nodes]
            for i, ref in enumerate(refs):
                # Endpoints always break a way; interior nodes only break it
                # when another way also uses them.
                ref_count[ref] += 2 if i in (0, len(refs) - 1) else 1

    # --- pass 2: build segments with real coordinates ---------------------
    node_coords: dict[int, tuple[float, float]] = {}
    segments: list[RawSegment] = []

    processor = osmium.FileProcessor(str(path)).with_locations()
    for obj in processor:
        if not obj.is_way():
            continue
        tags = _tags_to_dict(obj)
        if not is_walkable(tags):
            continue

        name = tags.get("name", "") or tags.get("ref", "")
        highway = tags.get("highway", "")

        current_refs: list[int] = []
        current_coords: list[tuple[float, float]] = []

        for node in obj.nodes:
            try:
                lat, lon = node.location.lat, node.location.lon
            except (osmium.InvalidLocationError, RuntimeError):
                # Node clipped out of the extract; end the run here so we do
                # not connect across a gap that does not exist.
                if len(current_refs) >= 2:
                    _emit(segments, node_coords, current_refs, current_coords,
                          name, highway, tags, bbox)
                current_refs, current_coords = [], []
                continue

            current_refs.append(node.ref)
            current_coords.append((lat, lon))
            node_coords[node.ref] = (lat, lon)

            is_junction = ref_count.get(node.ref, 0) > 1
            if len(current_refs) >= 2 and is_junction:
                _emit(segments, node_coords, current_refs, current_coords,
                      name, highway, tags, bbox)
                # The junction node starts the next segment.
                current_refs = [node.ref]
                current_coords = [(lat, lon)]

        if len(current_refs) >= 2:
            _emit(segments, node_coords, current_refs, current_coords,
                  name, highway, tags, bbox)

    # Drop coordinates for nodes no attached segment uses.
    used = {s.node_a for s in segments} | {s.node_b for s in segments}
    node_coords = {k: v for k, v in node_coords.items() if k in used}

    return node_coords, segments, cameras


def _emit(
    segments: list[RawSegment],
    node_coords: dict[int, tuple[float, float]],
    refs: list[int],
    coords: list[tuple[float, float]],
    name: str,
    highway: str,
    tags: dict[str, str],
    bbox: BBox | None,
) -> None:
    """Append one segment, skipping degenerate and out-of-area geometry."""
    if len(refs) < 2 or refs[0] == refs[-1]:
        # A closed loop with no intermediate junction is unroutable — it
        # leaves from and returns to the same node with no way to get off.
        return
    if bbox is not None and not any(bbox.contains(la, lo) for la, lo in coords):
        return

    length = polyline_length_m(coords)
    if length <= 0.5:
        return

    segments.append(
        RawSegment(
            node_a=refs[0],
            node_b=refs[-1],
            coords=list(coords),
            length_m=length,
            name=name,
            highway=highway,
            tags=dict(tags),
        )
    )


def download_extract(url: str, dest: Path) -> Path:
    """Download an .osm.pbf, streaming so the file never lands in memory."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=600.0
    ) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    return dest
