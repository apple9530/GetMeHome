"""Fuzzy place search over the OSM data we already ingest.

The app previously proxied Nominatim for every keystroke. Three problems with
that: it rate-limits aggressively, it matches close to exactly — "Madams
Organ" finds nothing for "Madam's Organ" — and it often returns a single
result where a person expects a list to choose from.

Since the build already parses the whole DC extract for the routing graph,
the named places are right there. Searching them locally means we control the
matching, so we can be forgiving in the specific ways that matter:

* **Punctuation is ignored.** "Madams Organ" and "Madam's Organ" normalise to
  the same string, as do "U-Street" and "U Street".
* **Abbreviations are expanded both ways.** "14th st nw" and "14th Street
  Northwest" become the same query, because people type whichever is shorter.
* **Typos still match.** Below the exact and prefix tiers there is a
  similarity score, so "Dupont Cirle" still finds Dupont Circle.

Results are ranked, not filtered — a weak match still appears, just lower
down, because the user is better placed than we are to spot the one they
meant.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# Street-type and directional abbreviations, expanded so the two spellings of
# the same address collide. DC addresses are unusually abbreviation-heavy
# because of the quadrant suffixes.
ABBREVIATIONS: dict[str, str] = {
    "st": "street",
    "str": "street",
    "ave": "avenue",
    "av": "avenue",
    "rd": "road",
    "dr": "drive",
    "blvd": "boulevard",
    "pl": "place",
    "ct": "court",
    "cir": "circle",
    "ln": "lane",
    "sq": "square",
    "ter": "terrace",
    "pkwy": "parkway",
    "hwy": "highway",
    "expy": "expressway",
    "aly": "alley",
    "plz": "plaza",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "nw": "northwest",
    "ne": "northeast",
    "sw": "southwest",
    "se": "southeast",
    "mt": "mount",
    "ft": "fort",
    "st.": "street",
    "univ": "university",
    "hosp": "hospital",
    "stn": "station",
    "intl": "international",
    "natl": "national",
    "dept": "department",
    "bldg": "building",
    "apt": "apartment",
}

# Ordinal suffixes are noise for matching: "14th" and "14" should collide.
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")
_NON_WORD = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")

# Rough importance by OSM category, used to break ties between equally good
# textual matches. A Metro station is a far more likely destination than a
# vending machine that happens to share a name.
CATEGORY_WEIGHT: dict[str, float] = {
    "station": 1.00,
    "transport": 0.92,
    "education": 0.86,
    "healthcare": 0.86,
    "tourism": 0.84,
    "leisure": 0.80,
    "food": 0.78,
    "nightlife": 0.78,
    "shop": 0.72,
    "civic": 0.72,
    "office": 0.62,
    "street": 0.58,
    "other": 0.50,
}


def normalise(text: str) -> str:
    """Fold a name or query into its comparable form."""
    if not text:
        return ""
    # Strip accents so "Cafés" matches "Cafes".
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))

    lowered = stripped.lower().replace("&", " and ")
    # Apostrophes vanish rather than becoming spaces: "Madam's" -> "madams",
    # which is what someone typing quickly produces.
    lowered = lowered.replace("'", "").replace("’", "")
    lowered = _NON_WORD.sub(" ", lowered)

    words = []
    for word in _WHITESPACE.split(lowered.strip()):
        if not word:
            continue
        ordinal = _ORDINAL.match(word)
        if ordinal:
            words.append(ordinal.group(1))
            continue
        words.append(ABBREVIATIONS.get(word, word))
    return " ".join(words)


def tokenise(text: str) -> list[str]:
    normalised = normalise(text)
    return normalised.split() if normalised else []


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    category: str = "other"
    # Street, neighbourhood or similar, shown as the result's second line.
    context: str = ""
    osm_id: str = ""

    @property
    def address(self) -> str:
        return self.context or "Washington, DC"


@dataclass
class ScoredPlace:
    place: Place
    score: float
    distance_m: float | None = None


@dataclass
class PlaceIndex:
    """Searchable index over named places."""

    places: list[Place] = field(default_factory=list)

    # Sorted (token, place index) pairs, for prefix lookup by bisection.
    _tokens: list[tuple[str, int]] = field(default_factory=list, repr=False)
    _token_keys: list[str] = field(default_factory=list, repr=False)
    # Trigram -> place indices, for matching through typos.
    _trigrams: dict[str, set[int]] = field(default_factory=dict, repr=False)
    _normalised: list[str] = field(default_factory=list, repr=False)

    def build(self) -> PlaceIndex:
        self._tokens = []
        self._trigrams = {}
        self._normalised = []

        for i, place in enumerate(self.places):
            normalised = normalise(place.name)
            self._normalised.append(normalised)
            for token in normalised.split():
                self._tokens.append((token, i))
            for gram in _trigrams_of(normalised):
                self._trigrams.setdefault(gram, set()).add(i)

        self._tokens.sort()
        self._token_keys = [t for t, _ in self._tokens]
        return self

    def __len__(self) -> int:
        return len(self.places)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 12,
        near: tuple[float, float] | None = None,
    ) -> list[ScoredPlace]:
        """Rank places against a query.

        ``near`` biases toward results close to the user, which matters in a
        city with a "14th Street" in more than one quadrant.
        """
        tokens = tokenise(query)
        if not tokens:
            return []

        normalised_query = " ".join(tokens)
        candidates = self._candidates(tokens, normalised_query)
        if not candidates:
            return []

        scored: list[ScoredPlace] = []
        for i in candidates:
            text = self._normalised[i]
            score = _similarity(normalised_query, tokens, text)
            if score <= 0:
                continue

            place = self.places[i]
            score *= 0.75 + 0.25 * CATEGORY_WEIGHT.get(place.category, 0.5)

            distance = None
            if near is not None:
                distance = _haversine(near[0], near[1], place.lat, place.lon)
                # A gentle bias only. Halving at ~4km keeps a strong textual
                # match across town above a weak one round the corner.
                score *= 1.0 / (1.0 + distance / 4000.0) ** 0.35

            scored.append(ScoredPlace(place=place, score=score, distance_m=distance))

        scored.sort(key=lambda s: -s.score)
        return _dedupe(scored)[:limit]

    def _candidates(self, tokens: list[str], normalised_query: str) -> set[int]:
        """Narrow to places worth scoring.

        Prefix matching first, which covers the overwhelming majority of
        queries and is cheap. Trigram overlap is the fallback that catches
        typos, and it only runs when prefixes came up short.
        """
        candidates: set[int] = set()
        for token in tokens:
            candidates |= self._prefix_matches(token)
            if len(candidates) > 4000:
                break

        if len(candidates) < 25:
            grams = _trigrams_of(normalised_query)
            counts: dict[int, int] = {}
            for gram in grams:
                for i in self._trigrams.get(gram, ()):  # noqa: PLC0206
                    counts[i] = counts.get(i, 0) + 1
            needed = max(2, len(grams) // 3)
            candidates |= {i for i, n in counts.items() if n >= needed}

        return candidates

    def _prefix_matches(self, prefix: str) -> set[int]:
        lo = bisect.bisect_left(self._token_keys, prefix)
        out: set[int] = set()
        for i in range(lo, len(self._tokens)):
            token, place_index = self._tokens[i]
            if not token.startswith(prefix):
                break
            out.add(place_index)
            if len(out) > 4000:
                break
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "n": p.name,
                        "la": round(p.lat, 6),
                        "lo": round(p.lon, 6),
                        "c": p.category,
                        "x": p.context,
                        "i": p.osm_id,
                    }
                    for p in self.places
                ]
            )
        )

    @classmethod
    def load(cls, path: Path) -> PlaceIndex:
        rows = json.loads(Path(path).read_text())
        index = cls(
            places=[
                Place(
                    name=r["n"],
                    lat=r["la"],
                    lon=r["lo"],
                    category=r.get("c", "other"),
                    context=r.get("x", ""),
                    osm_id=r.get("i", ""),
                )
                for r in rows
            ]
        )
        return index.build()


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


def _similarity(normalised_query: str, tokens: list[str], text: str) -> float:
    """How well a candidate matches, in [0, 1].

    Tiered rather than a single fuzzy ratio: an exact match must always beat a
    prefix match, which must always beat a merely similar one. A bare
    similarity ratio does not guarantee that, and gets embarrassing on short
    queries where unrelated short names score highly.
    """
    if not text:
        return 0.0
    if text == normalised_query:
        return 1.0
    if text.startswith(normalised_query):
        # Shorter completions rank above longer ones: typing "dupont" should
        # surface "Dupont Circle" before "Dupont Circle Hotel Parking".
        return 0.93 - min(0.08, 0.004 * (len(text) - len(normalised_query)))

    candidate_tokens = text.split()

    # Every query token is a prefix of some candidate token, in order or not.
    remaining = list(candidate_tokens)
    matched = 0
    for token in tokens:
        for i, candidate in enumerate(remaining):
            if candidate.startswith(token):
                matched += 1
                remaining.pop(i)
                break
    if matched == len(tokens):
        coverage = len(tokens) / max(1, len(candidate_tokens))
        return 0.72 + 0.15 * coverage

    if normalised_query in text:
        return 0.68

    ratio = SequenceMatcher(None, normalised_query, text).ratio()
    # Partial token matches lift the floor, so a single mistyped word in an
    # otherwise correct name is not fatal.
    if matched:
        ratio = max(ratio, 0.45 + 0.2 * (matched / len(tokens)))
    return ratio if ratio >= 0.55 else 0.0


def _trigrams_of(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _dedupe(scored: list[ScoredPlace]) -> list[ScoredPlace]:
    """Drop repeats of the same place.

    OSM frequently carries a venue as both a node and a building way, and a
    long street as many named segments. Keeping the best-scoring one of each
    stops the list filling with the same answer.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[ScoredPlace] = []
    for item in scored:
        key = (
            normalise(item.place.name),
            int(item.place.lat * 2000),  # ~50m buckets
            int(item.place.lon * 2000),
        )
        name_only = (normalise(item.place.name), 0, 0)
        if key in seen:
            continue
        # A street's many segments share a name but not a location, so collapse
        # those on name alone.
        if item.place.category == "street" and name_only in seen:
            continue
        seen.add(key)
        if item.place.category == "street":
            seen.add(name_only)
        out.append(item)
    return out


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
