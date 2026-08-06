"""Applies the safety models to a graph, and scores finished routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

import numpy as np

from ..config import CRIME
from ..graph.model import WalkGraph
from .cameras import AlprCamera
from .cameras import score_segments as score_cameras
from .crime_model import CrimeIncident, CrimeSurface
from .lighting import StreetLight
from .lighting import score_segments as score_lighting


def apply_scores(
    graph: WalkGraph,
    lights: list[StreetLight],
    incidents: list[CrimeIncident],
    cameras: list[AlprCamera],
    progress: Callable[[str], None] | None = None,
) -> dict[int, CrimeSurface]:
    """Populate a graph's lighting, crime and camera attributes in place.

    Crime is scored once per selectable lookback window. Doing this at build
    time rather than per request is not an optimisation, it is the only way it
    can work: each surface is a rasterised KDE over the whole city, which takes
    seconds, and the router needs the scores for every segment before it takes
    its first step.

    Returns the crime surfaces keyed by window, so the caller can keep them
    for diagnostics or for scoring transit stop locations later.
    """
    coords = [graph.segment_coords(i) for i in range(graph.n_segments)]

    graph.seg_lit = score_lighting(coords, lights)

    surfaces: dict[int, CrimeSurface] = {}
    graph.crime_windows = list(CRIME.windows_days)
    graph.seg_crime = np.zeros(
        (len(graph.crime_windows), 2, graph.n_segments), dtype=np.float32
    )
    for w, window in enumerate(graph.crime_windows):
        surface = CrimeSurface(incidents, window_days=window)
        if progress:
            progress(f"{window}d window: {surface.n_incidents} incidents")
        graph.seg_crime[w, 0] = surface.score_segments(coords, night=False)
        graph.seg_crime[w, 1] = surface.score_segments(coords, night=True)
        surfaces[window] = surface

    graph.seg_camera = score_cameras(coords, cameras)

    return surfaces


@dataclass
class SafetyBreakdown:
    """The safety numbers shown to the user for one route.

    Everything is 0-100 and higher is better, including ``crime`` — a route
    through a low-crime area scores high. Mixing "higher is worse" and
    "higher is better" scales in one panel is how people misread these.
    """

    overall: int
    lighting: int
    crime: int
    isolation: int
    # Share of route distance better lit than the median DC street. With
    # lighting ranked against the city, "well lit" can only be a comparative
    # claim — an absolute one would be true of almost every street here.
    pct_well_lit: int
    # Share of route distance in the worst crime-density areas.
    pct_high_crime: int
    # Worst single stretch, so a route with one bad block cannot hide behind
    # a good average.
    worst_stretch_risk: int
    worst_stretch_name: str

    def to_dict(self) -> dict:
        return asdict(self)


def score_route(
    graph: WalkGraph,
    edge_ids: list[int],
    is_night: bool,
    window_days: int | None = None,
) -> SafetyBreakdown:
    """Length-weighted safety breakdown for a sequence of directed edges.

    Weighting by length matters: a route is not made safe by threading
    through twenty short safe segments and one long dangerous one, and an
    unweighted mean over segments would say otherwise.
    """
    if not edge_ids:
        return SafetyBreakdown(100, 100, 100, 100, 100, 0, 0, "")

    segs = graph.edge_seg[np.asarray(edge_ids, dtype=np.int32)]
    lengths = graph.seg_length[segs].astype(np.float64)
    total = float(lengths.sum())
    if total <= 0:
        return SafetyBreakdown(100, 100, 100, 100, 100, 0, 0, "")

    w = lengths / total
    lit = graph.seg_lit[segs]
    crime = graph.crime_scores(is_night, window_days)[segs]
    isolation = graph.seg_isolation[segs]
    risk = graph.segment_risk(is_night, window_days)[segs]

    worst = int(np.argmax(risk))
    worst_name = graph.names[int(graph.seg_name[segs[worst]])] or "unnamed path"

    # During the day the lighting subscore is not meaningful, but showing a
    # blank would look broken, so report it as full marks and let the copy in
    # the app explain that lighting is not a factor in daylight.
    lighting_score = 100 if not is_night else int(round(100 * float((w * lit).sum())))

    return SafetyBreakdown(
        overall=int(round(100 * (1.0 - float((w * risk).sum())))),
        lighting=lighting_score,
        crime=int(round(100 * (1.0 - float((w * crime).sum())))),
        isolation=int(round(100 * (1.0 - float((w * isolation).sum())))),
        pct_well_lit=int(round(100 * float(w[lit > 0.5].sum()))),
        pct_high_crime=int(round(100 * float(w[crime > 0.6].sum()))),
        worst_stretch_risk=int(round(100 * float(risk[worst]))),
        worst_stretch_name=worst_name,
    )


def describe_tradeoff(fastest_s: float, this_s: float, safety_delta: int) -> str:
    """One line explaining what a route option buys you.

    Route pickers that show three near-identical cards with three numbers make
    the user do the comparison themselves. Doing it for them is most of the
    value of offering options at all.
    """
    extra_min = (this_s - fastest_s) / 60.0
    if extra_min < 0.5 and safety_delta <= 2:
        return "Fastest route"
    if extra_min < 0.5:
        return f"Same time, {safety_delta} points safer"
    if safety_delta <= 0:
        return f"{extra_min:.0f} min longer, no safety gain"
    return f"{extra_min:.0f} min longer, {safety_delta} points safer"
