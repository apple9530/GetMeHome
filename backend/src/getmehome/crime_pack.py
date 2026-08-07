"""A whole city's crime grid, pre-baked for offline use.

Someone walking home with no signal is exactly the person this app is for, so
the crime overlay is worth having without a server. The question is what to
ship.

**Not the incidents.** DC holds well over a hundred thousand over three years
and New York several times that. Shipping them would mean a large download and
re-implementing severity weighting, Gaussian binning and hex rounding in Swift
— three chances to disagree with the server and produce an overlay that is
subtly not the one the routing uses.

**So: the finished cells.** The server bins the whole city once per (window,
radius) pair and ships the result. The phone filters to its viewport and draws.
The offline overlay is then byte-identical to the online one at the same
radius, because it *is* the online one.

Two things keep the size down:

* **Only the coarser radii.** The fine end of the ladder is where the cell
  count explodes — halving the radius quadruples it — and the fine levels are
  the ones a phone with no signal is least likely to need, since they only
  appear when zoomed into a few blocks. Baking 375m and up covers a
  neighbourhood view and costs a fraction of what 165m alone would.
* **Centres, not corners.** Vertices are twelve floats per cell, several times
  the rest of the record. The pack ships the city's projection origin instead
  and the client derives the six corners — the same arithmetic, moved to where
  it is free.

Night-only is deliberately *not* baked. It would double the pack for a toggle,
and the app disables it while offline rather than silently showing the wrong
thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .cities import City
from .config import CRIME
from .safety.hexgrid import SIZE_LADDER, CrimeIndex

log = logging.getLogger("getmehome.crime_pack")

#: Ladder sizes baked into the pack, coarsest-relevant first.
#:
#: The two finest rungs (165m, 250m) are omitted. At 165m a city the size of
#: New York is tens of thousands of cells per window, which is most of the
#: pack for the zoom level someone offline is least likely to be at. The client
#: rounds up to the nearest baked radius and says the resolution is limited.
PACK_RADII: tuple[float, ...] = tuple(r for r in SIZE_LADDER if r >= 375.0)

#: Offence kinds kept per cell. The tail is long and each entry costs bytes;
#: four covers what drives a cell's risk with the rest folded away.
MAX_OFFENSE_KINDS = 4


@dataclass
class PackedCell:
    """One hexagon, in its smallest useful form."""

    lat: float
    lon: float
    total: int
    intensity: float
    night_share: float
    serious_count: int
    latest: str
    by_offense: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        # Short keys. At tens of thousands of cells the key names are a
        # meaningful share of the file, and nothing reads this by hand.
        return {
            "a": round(self.lat, 5),
            "o": round(self.lon, 5),
            "t": self.total,
            "i": round(self.intensity, 3),
            "n": round(self.night_share, 2),
            "s": self.serious_count,
            "d": self.latest,
            "b": self.by_offense,
        }


def build_pack(
    crime: CrimeIndex,
    city: City,
    windows: tuple[int, ...] | None = None,
    radii: tuple[float, ...] = PACK_RADII,
    now: datetime | None = None,
) -> dict:
    """Bin a whole city at every window and radius, ready to ship.

    Expensive — sixteen passes over the incident set for DC — so the caller
    caches it. It only changes when the graph is rebuilt.
    """
    now = now or datetime.now(UTC)
    windows = windows or CRIME.windows_days
    bbox = city.bbox

    levels: list[dict] = []
    total_cells = 0

    for window in windows:
        for radius in radii:
            cells, actual = crime.cells(
                bbox.min_lat,
                bbox.min_lon,
                bbox.max_lat,
                bbox.max_lon,
                radius_m=radius,
                window_days=window,
                max_offense_kinds=MAX_OFFENSE_KINDS,
                now=now,
            )
            packed = [
                PackedCell(
                    lat=c.center_lat,
                    lon=c.center_lon,
                    total=c.total,
                    intensity=c.intensity,
                    night_share=c.night_share,
                    serious_count=c.serious_count,
                    latest=c.latest.date().isoformat() if c.latest else "",
                    by_offense=[
                        {
                            "n": b.display,
                            "c": b.count,
                            "g": b.category,
                            "s": round(b.share, 3),
                        }
                        for b in c.by_offense
                    ],
                ).to_dict()
                for c in cells
            ]
            total_cells += len(packed)
            levels.append(
                {
                    "windowDays": window,
                    "radius": round(actual, 1),
                    "cells": packed,
                }
            )

    log.info(
        "%s crime pack: %d cells across %d levels",
        city.slug, total_cells, len(levels),
    )

    return {
        "city": city.slug,
        "cityName": city.name,
        "generatedAt": now.isoformat(),
        # The client derives each hexagon's six corners from its centre, so it
        # needs the frame those centres were binned in. Getting this wrong
        # would draw cells that do not tile.
        "projection": city.projection.to_dict(),
        "bbox": bbox.as_list(),
        "windows": list(windows),
        "radii": [round(r, 1) for r in radii],
        # Said plainly so the app can tell the user rather than letting them
        # wonder why the grid stops getting finer.
        "finestRadius": round(min(radii), 1),
        "nightOnlySupported": False,
        "totalCells": total_cells,
        "levels": levels,
    }
