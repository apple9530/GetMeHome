"""Multiple cities: the projection, the registry, and the vocabularies.

Most of this guards against the class of bug that does not crash. Routing New
York through Washington's projection, or scoring NYPD offences against MPD's
table, both produce confident wrong answers rather than errors — so they get
tested rather than trusted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from getmehome.cities import CITIES, DC, NYC, City, city_for_point, get_city
from getmehome.geo import Projection, frame_for, haversine_m, polyline_length_m

# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def test_a_projection_round_trips():
    for city in CITIES.values():
        lat, lon = city.center
        x, y = city.projection.to_local(lat, lon)
        back_lat, back_lon = city.projection.to_wgs84(x, y)
        assert float(back_lat) == pytest.approx(lat, abs=1e-9)
        assert float(back_lon) == pytest.approx(lon, abs=1e-9)


def test_the_origin_is_the_frames_zero():
    frame = Projection(40.7128, -73.9560)
    x, y = frame.to_local(40.7128, -73.9560)
    assert float(x) == pytest.approx(0.0)
    assert float(y) == pytest.approx(0.0)


def test_a_frame_measures_its_own_city_accurately():
    """Against a great-circle distance, which is the ground truth here."""
    for city in CITIES.values():
        lat0, lon0 = city.center
        # A kilometre east, roughly.
        lon1 = lon0 + 0.012
        measured = float(
            np.hypot(*[
                a - b
                for a, b in zip(
                    city.projection.to_local(lat0, lon1),
                    city.projection.to_local(lat0, lon0),
                    strict=True,
                )
            ])
        )
        truth = haversine_m(lat0, lon0, lat0, lon1)
        assert measured == pytest.approx(truth, rel=1e-3), city.slug


def test_the_wrong_citys_frame_is_wrong_by_a_lot():
    """The reason the projection is per-city and not a module global.

    An east-west distance measured through another city's origin is scaled by
    cos(their latitude) / cos(ours). Between DC and New York that is 2.6% —
    which over the width of New York is about a kilometre, in a direction
    nothing would ever flag as an error.
    """
    lat, lon = NYC.center
    east = lon + 0.05

    correct = _east_west_metres(NYC.projection, lat, lon, east)
    through_dc = _east_west_metres(DC.projection, lat, lon, east)
    truth = haversine_m(lat, lon, lat, east)

    assert correct == pytest.approx(truth, rel=1e-3)
    error = abs(through_dc - truth)
    assert error / truth > 0.02, "expected a >2% scale error"
    assert error > 80, f"expected a large absolute error, got {error:.0f} m"


def _east_west_metres(frame: Projection, lat: float, lon_a: float, lon_b: float) -> float:
    xa, _ = frame.to_local(lat, lon_a)
    xb, _ = frame.to_local(lat, lon_b)
    return abs(float(xb) - float(xa))


def test_polyline_helpers_need_no_city():
    """They anchor on their own geometry, so they are right in any city.

    This is what keeps the projection from having to be threaded through every
    measurement in the codebase.
    """
    for city in CITIES.values():
        lat, lon = city.center
        coords = [(lat, lon), (lat, lon + 0.01), (lat + 0.01, lon + 0.01)]
        measured = polyline_length_m(coords)
        truth = haversine_m(lat, lon, lat, lon + 0.01) + haversine_m(
            lat, lon + 0.01, lat + 0.01, lon + 0.01
        )
        assert measured == pytest.approx(truth, rel=1e-3), city.slug


def test_a_self_anchored_frame_starts_at_the_first_vertex():
    coords = [(40.75, -73.98), (40.76, -73.97)]
    frame = frame_for(coords)
    assert (frame.origin_lat, frame.origin_lon) == coords[0]


def test_an_empty_geometry_does_not_explode():
    assert polyline_length_m([]) == 0.0
    assert frame_for([]) == Projection(0.0, 0.0)


def test_a_projection_survives_a_round_trip_through_json():
    frame = NYC.projection
    assert Projection.from_dict(frame.to_dict()) == frame


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_both_cities_are_registered():
    assert set(CITIES) == {"dc", "nyc"}


def test_lookup_is_forgiving_about_case_and_whitespace():
    assert get_city(" DC ") is DC
    assert get_city("NYC") is NYC


def test_an_unknown_city_raises_rather_than_substituting_one():
    """Serving another city's graph would produce confident nonsense."""
    with pytest.raises(KeyError) as excinfo:
        get_city("boston")
    # The message lists what is available, because the commonest cause is a
    # typo rather than a genuinely absent city.
    assert "dc" in str(excinfo.value)


def test_no_city_means_the_default():
    assert get_city(None) is get_city("dc")


def test_each_citys_origin_is_inside_its_own_bbox():
    for city in CITIES.values():
        assert city.bbox.contains(*city.origin), city.slug


def test_the_bboxes_do_not_overlap():
    """Overlapping extents would make `city_for_point` ambiguous."""
    a, b = DC.bbox, NYC.bbox
    separated = (
        a.max_lat < b.min_lat
        or b.max_lat < a.min_lat
        or a.max_lon < b.min_lon
        or b.max_lon < a.min_lon
    )
    assert separated


def test_a_point_resolves_to_the_city_containing_it():
    assert city_for_point(*DC.center) is DC
    assert city_for_point(*NYC.center) is NYC
    # The Atlantic.
    assert city_for_point(38.0, -70.0) is None


def test_each_city_has_somewhere_to_put_its_build(tmp_path):
    for city in CITIES.values():
        assert city.build_dir(tmp_path) != DC.build_dir(tmp_path) or city is DC
        assert city.slug in str(city.build_dir(tmp_path))


def test_cities_do_not_share_a_build_directory(tmp_path):
    """Two cities writing one directory would overwrite each other's graph."""
    dirs = {city.build_dir(tmp_path) for city in CITIES.values()}
    assert len(dirs) == len(CITIES)


# ---------------------------------------------------------------------------
# Crime vocabularies
# ---------------------------------------------------------------------------


def test_every_vocabulary_is_internally_consistent():
    """An offence weighted in one table and missing from another scores at the
    default, which looks like data rather than like a bug."""
    for city in CITIES.values():
        assert city.crime_vocabulary.check() == [], city.slug


def test_the_two_cities_use_genuinely_different_offence_codes():
    """If they overlapped, one table might appear to work for both."""
    shared = set(DC.crime_vocabulary.severity) & set(NYC.crime_vocabulary.severity)
    # Robbery, arson and burglary happen to be named the same in both.
    assert shared <= {"ROBBERY", "ARSON", "BURGLARY"}


def test_nypd_offences_are_unweighted_by_the_dc_vocabulary():
    """The failure this is all guarding against.

    Scoring New York's incidents against Washington's table finds almost
    nothing and falls through to the default weight for the rest, which turns
    the crime surface into a map of where people are.
    """
    nypd = set(NYC.crime_vocabulary.severity)
    recognised = nypd & set(DC.crime_vocabulary.severity)
    assert len(recognised) / len(nypd) < 0.3


def test_violent_and_property_stay_far_apart_in_both_cities():
    """The weighting judgement has to survive being restated in a new
    vocabulary, or the two cities are not measuring the same thing."""
    groups = [
        (
            DC,
            ["HOMICIDE", "SEX ABUSE", "ASSAULT W/DANGEROUS WEAPON", "ROBBERY"],
            ["MOTOR VEHICLE THEFT", "THEFT F/AUTO", "THEFT/OTHER", "BURGLARY"],
        ),
        (
            NYC,
            ["MURDER & NON-NEGL. MANSLAUGHTER", "RAPE", "FELONY ASSAULT", "ROBBERY"],
            ["GRAND LARCENY OF MOTOR VEHICLE", "GRAND LARCENY", "PETIT LARCENY", "BURGLARY"],
        ),
    ]
    for city, violent, property_crime in groups:
        vocab = city.crime_vocabulary

        def effective(name: str, vocab=vocab) -> float:
            return vocab.severity[name] * vocab.pedestrian_relevance[name]

        assert min(effective(n) for n in violent) > 4 * max(
            effective(n) for n in property_crime
        ), city.slug


def test_every_offence_lands_in_a_known_category():
    for city in CITIES.values():
        for name, group in city.crime_vocabulary.category.items():
            assert group in {"violent", "sexual", "property", "other"}, (
                city.slug, name,
            )


def test_display_names_are_readable():
    """No raw database codes should reach a person."""
    for city in CITIES.values():
        for code, shown in city.crime_vocabulary.display_name.items():
            assert shown != code
            assert "/" not in shown or " / " in shown, (city.slug, shown)
            assert not shown.isupper(), (city.slug, shown)


def test_a_broken_vocabulary_is_reported_rather_than_silently_defaulted():
    from getmehome.cities import CrimeVocabulary

    broken = CrimeVocabulary(
        severity={"A": 1.0, "B": 0.5},
        pedestrian_relevance={"A": 1.0},
        display_name={"A": "Alpha", "B": "Bravo"},
        category={"A": "violent", "B": "property", "C": "other"},
    )
    problems = broken.check()
    assert any("pedestrian_relevance" in p and "B" in p for p in problems)
    assert any("category" in p and "C" in p for p in problems)


# ---------------------------------------------------------------------------
# Real-time providers
# ---------------------------------------------------------------------------


def test_each_city_gets_a_provider_matching_its_agency():
    from getmehome.live import live_provider_for

    assert type(live_provider_for(DC)).__name__ == "WmataLive"
    assert type(live_provider_for(NYC)).__name__ == "MtaLive"


def test_a_city_with_no_realtime_source_gets_a_working_no_op():
    """An absent feature and a broken one look the same to the caller."""
    from getmehome.live import live_provider_for

    nowhere = City(
        slug="nowhere",
        name="Nowhere",
        region="",
        bbox=DC.bbox,
        timezone_name="UTC",
        origin=DC.origin,
        osm_extract_url="",
        crime=DC.crime,
        lights=DC.lights,
        transit_feeds=(),
        crime_vocabulary=DC.crime_vocabulary,
        realtime="",
    )
    provider = live_provider_for(nowhere)
    assert not provider.enabled
    assert provider.disabled_reason
    assert provider.bus_predictions("anything") == []
    assert provider.rail_predictions(["anything"]) == []
    assert provider.bus_position_for_trip("anything") is None


def test_a_provider_without_credentials_says_what_is_missing():
    from getmehome.live.wmata import WmataLive

    assert "WMATA API key" in WmataLive(api_key="").disabled_reason


# ---------------------------------------------------------------------------
# NYPD feed parsing
# ---------------------------------------------------------------------------


def test_a_timestamp_becomes_the_same_shift_vocabulary_mpd_publishes():
    """NYPD stamps a time; MPD stamps a shift. Both have to end up comparable."""
    from getmehome.ingest.socrata import shift_for_hour

    assert shift_for_hour(2) == "MIDNIGHT"
    assert shift_for_hour(11) == "DAY"
    assert shift_for_hour(21) == "EVENING"
    # Every hour maps somewhere.
    assert {shift_for_hour(h) for h in range(24)} == {"MIDNIGHT", "DAY", "EVENING"}


def test_nypds_split_date_and_time_columns_combine():
    from getmehome.ingest.socrata import parse_nypd_datetime

    when = parse_nypd_datetime("2026-03-14T00:00:00.000", "23:45:00")
    assert when is not None
    assert (when.year, when.month, when.day) == (2026, 3, 14)
    assert (when.hour, when.minute) == (23, 45)


def test_a_missing_or_junk_time_does_not_lose_the_incident():
    """NYPD writes "(null)" as a literal string often enough to matter."""
    from getmehome.ingest.socrata import parse_nypd_datetime

    for bad in (None, "", "(null)", "nonsense", "99:99:99"):
        when = parse_nypd_datetime("2026-03-14T00:00:00.000", bad)
        assert when is not None, bad
        assert when.hour == 0

    # A missing date, though, is not an incident we can place in time.
    assert parse_nypd_datetime(None, "12:00:00") is None
    assert parse_nypd_datetime("not-a-date", "12:00:00") is None


def test_column_lookup_is_case_insensitive():
    """A column present under another case looks like missing data."""
    from getmehome.ingest.socrata import pick

    assert pick({"Latitude": "40.7"}, ("latitude",)) == "40.7"
    assert pick({"ofns_desc": "ROBBERY"}, ("OFNS_DESC",)) == "ROBBERY"
    assert pick({"a": ""}, ("a",), default="fallback") == "fallback"
    assert pick({}, ("missing",)) is None


def test_a_weapon_is_only_claimed_when_it_is_there():
    """A wrongly inferred weapon inflates a real incident by 40%."""
    from getmehome.ingest.socrata import _weapon_from

    assert _weapon_from({"pd_desc": "ROBBERY,BEGIN A FIREARM"}) == "FIREARM"
    assert _weapon_from({"pd_desc": "ASSAULT 2,1,UNCLASSIFIED"}) == ""
    assert _weapon_from({}) == ""


def test_geometry_helpers_agree_with_great_circle_distance():
    """A sanity check that the whole projection idea holds up at NYC's scale."""
    lat, lon = NYC.center
    for dlat, dlon in ((0.05, 0.0), (0.0, 0.05), (0.05, 0.05)):
        frame = NYC.projection
        xa, ya = frame.to_local(lat, lon)
        xb, yb = frame.to_local(lat + dlat, lon + dlon)
        measured = math.hypot(float(xb - xa), float(yb - ya))
        truth = haversine_m(lat, lon, lat + dlat, lon + dlon)
        assert measured == pytest.approx(truth, rel=2e-3)
