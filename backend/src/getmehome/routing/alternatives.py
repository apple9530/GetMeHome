"""Generating the set of route options the user chooses between.

Rather than computing one route and perturbing it, each option comes from a
genuine search at a different risk aversion. That guarantees every option is
optimal for *some* coherent preference, which is what makes the comparison
meaningful — a route that is merely "the fast route bent slightly" is not
actually the safest route and should not be labelled as one.

Options that turn out to be near-duplicates are collapsed, because three
cards showing the same line on the map with three slightly different numbers
is worse than one card that says the fastest way is also the safest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import ROUTING
from ..graph.model import WalkGraph
from ..safety.cameras import AlprCamera, coverage_along_route
from ..safety.scoring import SafetyBreakdown, describe_tradeoff, score_route
from .astar import GraphIndex, PathResult, SnapPoint, shortest_path


@dataclass
class RouteOption:
    kind: str  # "fastest" | "balanced" | "safest"
    label: str
    summary: str
    path: PathResult
    safety: SafetyBreakdown
    cameras: dict = field(default_factory=dict)


_LABELS = {
    "fastest": ("Fastest", "Quickest way there"),
    "balanced": ("Balanced", "A good compromise"),
    "safest": ("Safest", "Best-lit, lowest-risk streets"),
}


def _segment_lengths(graph: WalkGraph, path: PathResult) -> dict[int, float]:
    """Length contributed per segment, for the overlap test."""
    out: dict[int, float] = {}
    for leg in path.legs:
        seg = int(graph.edge_seg[leg.edge_id])
        out[seg] = out.get(seg, 0.0) + graph.edge_length(leg.edge_id) * leg.fraction
    return out


def overlap_fraction(graph: WalkGraph, a: PathResult, b: PathResult) -> float:
    """Fraction of the shorter route's length shared with the longer one."""
    sa, sb = _segment_lengths(graph, a), _segment_lengths(graph, b)
    if not sa or not sb:
        return 0.0
    shared = sum(min(sa[s], sb[s]) for s in sa.keys() & sb.keys())
    denom = min(sum(sa.values()), sum(sb.values()))
    return shared / denom if denom > 0 else 0.0


def compute_options(
    index: GraphIndex,
    start: SnapPoint,
    end: SnapPoint,
    is_night: bool,
    avoid_cameras: bool = False,
    cameras: list[AlprCamera] | None = None,
    lambdas: dict[str, float] | None = None,
) -> list[RouteOption]:
    """Compute the fastest / balanced / safest options for a walk."""
    graph = index.graph
    lambdas = lambdas or {
        "fastest": ROUTING.lambda_fastest,
        "balanced": ROUTING.lambda_balanced,
        "safest": ROUTING.lambda_safest,
    }

    found: list[tuple[str, PathResult]] = []
    for kind in ("fastest", "balanced", "safest"):
        cost_arr, time_arr = graph.edge_costs(
            is_night, lambdas[kind], avoid_cameras=avoid_cameras
        )
        path = shortest_path(
            index, start, end, cost_arr.tolist(), time_arr.tolist()
        )
        if path is not None:
            found.append((kind, path))

    if not found:
        return []

    fastest_duration = min(p.duration_s for _, p in found)

    # A "safest" route that triples the journey is not a route anyone takes.
    # Re-run an over-long option at progressively lower risk aversion until it
    # fits the detour budget, so the user still gets a safer choice rather
    # than an absurd one or nothing at all.
    capped: list[tuple[str, PathResult]] = []
    for kind, path in found:
        budget = fastest_duration * ROUTING.max_detour_ratio
        if path.duration_s <= budget or kind == "fastest":
            capped.append((kind, path))
            continue

        lam = lambdas[kind]
        for _ in range(3):
            lam *= 0.5
            cost_arr, time_arr = graph.edge_costs(
                is_night, lam, avoid_cameras=avoid_cameras
            )
            retry = shortest_path(
                index, start, end, cost_arr.tolist(), time_arr.tolist()
            )
            if retry is not None and retry.duration_s <= budget:
                capped.append((kind, retry))
                break
        else:
            # Even at low aversion it does not fit; keep the fastest-fitting
            # attempt rather than dropping the option silently.
            capped.append((kind, path))

    # Collapse near-duplicates, keeping the safer of any pair.
    ordered = sorted(
        capped, key=lambda kp: {"safest": 0, "balanced": 1, "fastest": 2}[kp[0]]
    )
    kept: list[tuple[str, PathResult]] = []
    merged_kinds: dict[int, list[str]] = {}
    for kind, path in ordered:
        dup_of = None
        for i, (_, existing) in enumerate(kept):
            if overlap_fraction(graph, path, existing) > ROUTING.max_route_overlap:
                dup_of = i
                break
        if dup_of is None:
            merged_kinds[len(kept)] = [kind]
            kept.append((kind, path))
        else:
            merged_kinds[dup_of].append(kind)

    options: list[RouteOption] = []
    for i, (kind, path) in enumerate(kept):
        kinds = merged_kinds[i]
        safety = score_route(graph, [l.edge_id for l in path.legs], is_night)
        label = _merged_label(kinds)
        options.append(
            RouteOption(
                kind=kinds[-1],  # the fastest of the merged set
                label=label,
                summary="",
                path=path,
                safety=safety,
                cameras=(
                    coverage_along_route(path.coords, cameras) if cameras else {}
                ),
            )
        )

    # Order as the user reads them: quickest first, safest last.
    options.sort(key=lambda o: o.path.duration_s)

    best_safety = max(o.safety.overall for o in options)
    fastest_s = min(o.path.duration_s for o in options)
    for o in options:
        o.summary = describe_tradeoff(
            fastest_s, o.path.duration_s, o.safety.overall - _safety_of_fastest(options)
        )
        if o.safety.overall == best_safety and len(options) > 1:
            o.summary = o.summary  # already reflects the delta

    return options


def _safety_of_fastest(options: list[RouteOption]) -> int:
    return min(options, key=lambda o: o.path.duration_s).safety.overall


def _merged_label(kinds: list[str]) -> str:
    """Label for an option, accounting for collapsed duplicates."""
    if len(kinds) == 1:
        return _LABELS[kinds[0]][0]
    if set(kinds) >= {"fastest", "safest"}:
        return "Fastest & safest"
    ordered = [k for k in ("safest", "balanced", "fastest") if k in kinds]
    return " & ".join(_LABELS[k][0] for k in ordered)
