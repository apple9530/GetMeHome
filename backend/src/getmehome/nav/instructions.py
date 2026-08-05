"""Turn-by-turn instruction generation.

Consecutive legs on the same street are collapsed into one step, and a
maneuver is emitted wherever the street name changes or the geometry turns
sharply enough to need calling out. Both conditions are needed: DC streets
change name at circles without turning, and paths turn hard without changing
name.

Each step carries a display string and a shorter spoken string. They differ
on purpose — "Turn right onto Rhode Island Avenue Northwest" is right on a
screen and too long in a headphone prompt when the turn is 20 metres away.

Steps also carry a safety note where the stretch ahead is notably dark or
high-risk. Telling someone *why* the route bent is what makes a safety router
trustworthy rather than merely opinionated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..geo import angle_difference, bearing_deg, compass_direction
from ..graph.model import WalkGraph
from ..routing.astar import PathLeg

# Turn classification thresholds, in degrees.
_STRAIGHT = 22.0
_SLIGHT = 50.0
_NORMAL = 125.0
_SHARP = 160.0

# How far ahead of a maneuver each spoken prompt fires, in metres.
VOICE_TRIGGERS = (200.0, 60.0, 15.0)


@dataclass
class NavStep:
    maneuver: str  # depart | straight | left | right | slight_left | ... | arrive
    instruction: str
    voice: str
    street: str
    distance_m: float
    duration_s: float
    # Index into the route's coordinate list where this step begins.
    start_index: int
    location: tuple[float, float]
    safety_note: str = ""
    # Distances at which the app should speak, nearest-last.
    voice_triggers: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "maneuver": self.maneuver,
            "instruction": self.instruction,
            "voice": self.voice,
            "street": self.street,
            "distance": round(self.distance_m, 1),
            "duration": round(self.duration_s, 1),
            "startIndex": self.start_index,
            "location": {"lat": self.location[0], "lon": self.location[1]},
            "safetyNote": self.safety_note,
            "voiceTriggers": self.voice_triggers,
        }


def classify_turn(angle: float) -> str:
    """Map a signed bearing change to a maneuver name. Positive is right."""
    a = abs(angle)
    if a < _STRAIGHT:
        return "straight"
    if a > _SHARP:
        return "uturn"
    side = "right" if angle > 0 else "left"
    if a < _SLIGHT:
        return f"slight_{side}"
    if a < _NORMAL:
        return side
    return f"sharp_{side}"


_PHRASES = {
    "straight": "Continue",
    "left": "Turn left",
    "right": "Turn right",
    "slight_left": "Bear left",
    "slight_right": "Bear right",
    "sharp_left": "Sharp left",
    "sharp_right": "Sharp right",
    "uturn": "Make a U-turn",
}


def format_distance(metres: float) -> str:
    """Distances the way a person would say them, not to the metre."""
    if metres < 20:
        return f"{int(round(metres / 5) * 5)} m"
    if metres < 100:
        return f"{int(round(metres / 10) * 10)} m"
    if metres < 1000:
        return f"{int(round(metres / 50) * 50)} m"
    return f"{metres / 1000:.1f} km"


def _safety_note(
    graph: WalkGraph, seg_ids: list[int], is_night: bool
) -> str:
    """A short warning if the stretch ahead is dark or high-crime."""
    if not seg_ids:
        return ""
    lit = float(sum(graph.seg_lit[s] for s in seg_ids) / len(seg_ids))
    crime_arr = graph.seg_crime_night if is_night else graph.seg_crime_day
    crime = float(sum(crime_arr[s] for s in seg_ids) / len(seg_ids))

    if is_night and lit < 0.2:
        return "This stretch is poorly lit — stay alert"
    if crime > 0.65:
        return "Higher-incident area — stay alert"
    if is_night and lit < 0.4:
        return "Patchy lighting along here"
    return ""


def _leg_bearings(
    coords: list[tuple[float, float]]
) -> tuple[float, float]:
    """(entry bearing, exit bearing) of a polyline."""
    if len(coords) < 2:
        return 0.0, 0.0
    entry = bearing_deg(*coords[0], *coords[1])
    exit_ = bearing_deg(*coords[-2], *coords[-1])
    return entry, exit_


def build_steps(
    graph: WalkGraph,
    legs: list[PathLeg],
    is_night: bool,
    destination_name: str = "your destination",
) -> list[NavStep]:
    """Turn a path into a list of navigation steps."""
    if not legs:
        return []

    # Group consecutive legs into steps, breaking on name change or real turn.
    groups: list[list[PathLeg]] = []
    prev_name: str | None = None
    prev_exit: float | None = None

    for leg in legs:
        coords = graph.edge_coords(leg.edge_id)
        name = graph.edge_name(leg.edge_id)
        entry, exit_ = _leg_bearings(coords)

        turned = (
            prev_exit is not None
            and abs(angle_difference(prev_exit, entry)) >= _STRAIGHT
        )
        if not groups or name != prev_name or turned:
            groups.append([leg])
        else:
            groups[-1].append(leg)

        prev_name = name
        prev_exit = exit_

    steps: list[NavStep] = []
    coord_cursor = 0
    prev_group_exit: float | None = None

    for gi, group in enumerate(groups):
        seg_ids = [int(graph.edge_seg[l.edge_id]) for l in group]
        distance = sum(graph.edge_length(l.edge_id) * l.fraction for l in group)
        duration = distance / 1.35

        first_coords = graph.edge_coords(group[0].edge_id)
        entry_bearing, _ = _leg_bearings(first_coords)
        street = graph.edge_name(group[0].edge_id) or "the path"
        location = first_coords[0]

        if gi == 0:
            maneuver = "depart"
            heading = compass_direction(entry_bearing)
            named = street if street != "the path" else "the path"
            instruction = f"Head {heading} on {named}"
            voice = f"Head {heading} on {named}"
        else:
            angle = angle_difference(prev_group_exit or 0.0, entry_bearing)
            maneuver = classify_turn(angle)
            phrase = _PHRASES[maneuver]
            if maneuver == "straight":
                instruction = f"Continue on {street}"
                voice = f"Continue on {street}"
            else:
                instruction = f"{phrase} onto {street}"
                voice = f"{phrase} onto {street}"

        # Count the vertices this step contributes so the app can map a step
        # to a slice of the route polyline.
        vertex_count = 0
        for leg in group:
            c = graph.edge_coords(leg.edge_id)
            vertex_count += max(0, len(c) - (1 if vertex_count else 0))

        steps.append(
            NavStep(
                maneuver=maneuver,
                instruction=instruction,
                voice=voice,
                street=street,
                distance_m=distance,
                duration_s=duration,
                start_index=coord_cursor,
                location=location,
                safety_note=_safety_note(graph, seg_ids, is_night),
                voice_triggers=[d for d in VOICE_TRIGGERS if d < distance],
            )
        )
        coord_cursor += vertex_count
        _, prev_group_exit = _leg_bearings(graph.edge_coords(group[-1].edge_id))

    # Fold the distance of each step into the *preceding* instruction, which
    # is how people actually navigate: "in 200m, turn right" not "turn right,
    # that step is 200m long".
    for i, step in enumerate(steps[:-1]):
        nxt = steps[i + 1]
        if nxt.maneuver not in ("arrive", "depart"):
            dist = format_distance(step.distance_m)
            step.instruction = f"{step.instruction} for {dist}"

    last_coords = graph.edge_coords(legs[-1].edge_id)
    steps.append(
        NavStep(
            maneuver="arrive",
            instruction=f"Arrive at {destination_name}",
            voice=f"You have arrived at {destination_name}",
            street=graph.edge_name(legs[-1].edge_id),
            distance_m=0.0,
            duration_s=0.0,
            start_index=coord_cursor,
            location=last_coords[-1],
            voice_triggers=[],
        )
    )
    return steps
