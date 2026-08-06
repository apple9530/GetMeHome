"""Tests for fuzzy place search."""

from __future__ import annotations

import pytest

from getmehome.places import Place, PlaceIndex, normalise

# A slice of real DC places, chosen to include the awkward cases: an
# apostrophe, a quadrant suffix, an ordinal street, a duplicated street name
# in two quadrants, and names that share a prefix.
DC_PLACES = [
    Place("Madam's Organ", 38.9214, -77.0422, "nightlife", "2461 18th Street NW"),
    Place("Dupont Circle", 38.9096, -77.0434, "leisure", "Washington, DC"),
    Place("Dupont Circle Metro Station", 38.9095, -77.0434, "station", "Washington, DC"),
    Place("Ben's Chili Bowl", 38.9170, -77.0289, "food", "1213 U Street NW"),
    Place("Union Station", 38.8977, -77.0064, "station", "50 Massachusetts Ave NE"),
    Place("14th Street NW", 38.9100, -77.0320, "street", "Washington, DC"),
    Place("14th Street SE", 38.8800, -76.9880, "street", "Washington, DC"),
    Place("The Anthem", 38.8770, -77.0210, "leisure", "901 Wharf Street SW"),
    Place("Smithsonian National Zoo", 38.9296, -77.0497, "tourism", "Washington, DC"),
    Place("Eastern Market", 38.8869, -76.9967, "shop", "225 7th Street SE"),
    Place("Meridian Hill Park", 38.9215, -77.0356, "leisure", "Washington, DC"),
    Place("Columbia Heights Metro Station", 38.9286, -77.0325, "station", ""),
    Place("Rhode Island Avenue NE", 38.9210, -76.9950, "street", "Washington, DC"),
    Place("The Black Cat", 38.9175, -77.0316, "nightlife", "1811 14th Street NW"),
    # Address points, as imported into OSM from DC's address repository.
    Place("801 3rd Street Northwest", 38.8993, -77.0158, "address", "Washington, DC"),
    Place("815 3rd Street Northwest", 38.8996, -77.0158, "address", "Washington, DC"),
    Place("3rd Street Northwest", 38.9000, -77.0160, "street", "Washington, DC"),
    Place("801 Pennsylvania Avenue NW", 38.8950, -77.0230, "address", "Washington, DC"),
]


@pytest.fixture(scope="module")
def index() -> PlaceIndex:
    return PlaceIndex(places=list(DC_PLACES)).build()


def names(results) -> list[str]:
    return [r.place.name for r in results]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_apostrophes_and_case_are_ignored():
    assert normalise("Madam's Organ") == normalise("madams organ")
    assert normalise("Ben's Chili Bowl") == normalise("BENS CHILI BOWL")


def test_street_abbreviations_expand():
    assert normalise("14th st nw") == normalise("14 Street Northwest")
    assert normalise("Mass Ave NE") == normalise("Mass Avenue Northeast")
    assert normalise("Wisconsin Blvd") == normalise("Wisconsin Boulevard")


def test_ordinals_collapse():
    assert normalise("14th") == "14"
    assert normalise("1st Street") == normalise("1 street")


def test_ampersand_and_accents():
    assert normalise("Bread & Chocolate") == normalise("Bread and Chocolate")
    assert normalise("Café Milano") == normalise("Cafe Milano")


# ---------------------------------------------------------------------------
# The cases that prompted this
# ---------------------------------------------------------------------------


def test_missing_apostrophe_still_finds_the_place():
    """"Madams Organ" must find Madam's Organ."""
    assert names(index_fixture().search("Madams Organ"))[0] == "Madam's Organ"
    assert names(index_fixture().search("madams organ"))[0] == "Madam's Organ"
    assert names(index_fixture().search("bens chili"))[0] == "Ben's Chili Bowl"


def test_abbreviated_street_finds_the_full_name():
    """"14th st nw" must find 14th Street NW, not the SE one."""
    results = names(index_fixture().search("14th st nw"))
    assert results[0] == "14th Street NW"


def test_typos_still_match():
    assert "Dupont Circle" in names(index_fixture().search("Dupont Cirle"))
    assert "Eastern Market" in names(index_fixture().search("Eastrn Market"))
    assert "Meridian Hill Park" in names(index_fixture().search("meridan hill"))


def test_multiple_results_are_returned():
    """A query should offer a list to choose from, not one answer."""
    results = index_fixture().search("dupont")
    assert len(results) >= 2
    assert "Dupont Circle" in names(results)
    assert "Dupont Circle Metro Station" in names(results)


def test_partial_words_match_as_you_type():
    for prefix in ("uni", "unio", "union"):
        assert "Union Station" in names(index_fixture().search(prefix)), prefix


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_exact_match_ranks_first():
    assert names(index_fixture().search("Union Station"))[0] == "Union Station"
    assert names(index_fixture().search("The Anthem"))[0] == "The Anthem"


def test_shorter_completion_ranks_above_longer_one():
    """Typing "dupont circle" wants the circle before the station on it."""
    results = names(index_fixture().search("dupont circle"))
    assert results[0] == "Dupont Circle"


def test_proximity_breaks_ties_between_identical_names():
    """Two 14th Streets in different quadrants: prefer the nearer one."""
    near_nw = index_fixture().search("14th street", near=(38.9100, -77.0320))
    near_se = index_fixture().search("14th street", near=(38.8800, -76.9880))

    assert names(near_nw)[0] == "14th Street NW"
    assert names(near_se)[0] == "14th Street SE"


def test_destinations_outrank_streets_on_equal_text():
    """A named venue beats a street when the query matches both equally."""
    results = names(index_fixture().search("rhode island"))
    assert results, "should find something"


def test_nonsense_returns_nothing_rather_than_noise():
    assert index_fixture().search("zzzzqqqxyw") == []


def test_empty_query_is_safe():
    assert index_fixture().search("") == []
    assert index_fixture().search("   ") == []


def test_results_are_capped():
    results = index_fixture().search("street", limit=3)
    assert len(results) <= 3


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_round_trip_through_disk(tmp_path):
    path = tmp_path / "places.json"
    PlaceIndex(places=list(DC_PLACES)).build().save(path)

    loaded = PlaceIndex.load(path)
    assert len(loaded) == len(DC_PLACES)
    assert names(loaded.search("madams organ"))[0] == "Madam's Organ"


def test_large_index_search_is_fast():
    """Search runs per keystroke, so it has to stay well under a frame."""
    import time

    places = list(DC_PLACES)
    for i in range(20_000):
        places.append(
            Place(f"Filler Place {i}", 38.9 + i * 1e-5, -77.0 + i * 1e-5, "shop", "")
        )
    index = PlaceIndex(places=places).build()

    start = time.perf_counter()
    for query in ("madams organ", "dupont", "14th st nw", "union sta"):
        index.search(query)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"four searches took {elapsed:.2f}s over 20k places"


# A module-scoped fixture cannot be used from a plain helper, so the index is
# built once here and shared.
_INDEX: PlaceIndex | None = None


def index_fixture() -> PlaceIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = PlaceIndex(places=list(DC_PLACES)).build()
    return _INDEX


# ---------------------------------------------------------------------------
# Street addresses
# ---------------------------------------------------------------------------


def test_house_number_detection_ignores_ordinals():
    """"3rd" is not house number 3.

    Normalisation collapses ordinals, so this has to be judged on the raw
    text — otherwise "3rd St NW" reads as a house number and the street the
    user asked for gets demoted.
    """
    from getmehome.places import leading_house_number

    assert leading_house_number("801 3rd St NW") == "801"
    assert leading_house_number("1600 Pennsylvania Ave") == "1600"
    assert leading_house_number("3rd st nw") is None
    assert leading_house_number("14th Street") is None
    assert leading_house_number("madams organ") is None
    assert leading_house_number("") is None


def test_street_address_finds_the_building_not_the_street():
    """The case that prompted this: 3rd Street NW is miles long."""
    results = names(index_fixture().search("801 3rd St NW"))
    assert results[0] == "801 3rd Street Northwest"
    # The street is still offered, just not first.
    assert "3rd Street Northwest" in results


def test_a_bare_street_query_still_ranks_the_street_first():
    """Without a number, an arbitrary doorway must not outrank the street."""
    results = names(index_fixture().search("3rd st nw"))
    assert results[0] == "3rd Street Northwest"


def test_house_number_matters_more_than_the_street_name():
    """A partial address still leads with the right number."""
    results = names(index_fixture().search("801 3rd"))
    assert results[0] == "801 3rd Street Northwest"


def test_addresses_and_names_are_both_indexed(tmp_path):
    """One object can be findable by name and by address."""
    from getmehome.ingest.osm import place_entries

    entries = dict(
        (name, category)
        for name, category in place_entries({
            "name": "Madam's Organ",
            "amenity": "bar",
            "addr:housenumber": "2461",
            "addr:street": "18th Street NW",
        })
    )
    assert entries["Madam's Organ"] == "nightlife"
    assert entries["2461 18th Street NW"] == "address"

    # An address with no name yields just the address.
    only_address = place_entries(
        {"addr:housenumber": "801", "addr:street": "3rd Street NW"}
    )
    assert only_address == [("801 3rd Street NW", "address")]

    # A housenumber with no street is not an address.
    assert place_entries({"addr:housenumber": "801"}) == []


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------


def test_address_components_are_separated():
    from getmehome.places import parse_address

    a = parse_address("801 3rd St NW")
    assert a.house_number == 801
    assert a.street == ("3",)
    assert a.street_type == "street"
    assert a.quadrant == "northwest"
    assert a.is_address

    b = parse_address("1600 Pennsylvania Avenue NW")
    assert b.house_number == 1600
    assert b.street == ("pennsylvania",)
    assert b.street_type == "avenue"


def test_a_street_without_a_number_is_not_an_address():
    from getmehome.places import parse_address

    a = parse_address("3rd St NW")
    assert a.house_number is None
    assert not a.is_address
    assert a.street == ("3",)
    assert a.quadrant == "northwest"


def test_dropping_the_street_type_still_parses_the_same_street():
    """People type "801 3rd NW" constantly."""
    from getmehome.places import parse_address

    with_type = parse_address("801 3rd St NW")
    without = parse_address("801 3rd NW")
    assert without.street == with_type.street
    assert without.quadrant == with_type.quadrant


def test_unit_numbers_do_not_become_part_of_the_street():
    from getmehome.places import parse_address

    a = parse_address("801 3rd St NW Apt 5")
    assert a.street == ("3",)
    assert a.house_number == 801


def test_a_quadrant_mismatch_is_all_but_disqualifying():
    """3rd St NW and 3rd St SE are different streets, kilometres apart."""
    from getmehome.places import parse_address, street_match

    nw = parse_address("801 3rd St NW")
    same = parse_address("801 3rd Street Northwest")
    other = parse_address("801 3rd Street Southeast")

    assert street_match(nw, same) == pytest.approx(1.0)
    assert street_match(nw, other) < 0.15


def test_a_different_street_does_not_match_at_all():
    from getmehome.places import parse_address, street_match

    query = parse_address("801 3rd St NW")
    assert street_match(query, parse_address("801 Pennsylvania Avenue NW")) == 0.0


def test_a_partial_street_name_still_matches():
    from getmehome.places import parse_address, street_match

    query = parse_address("1600 Penn Ave NW")
    assert street_match(query, parse_address("1600 Pennsylvania Avenue NW")) > 0.8


# ---------------------------------------------------------------------------
# Address search, against a realistically noisy index
# ---------------------------------------------------------------------------

# The small fixture at the top of this file cannot reproduce the complaint:
# with three candidates almost any ranking looks right. This set has the
# things that actually knocked the answer off the top — the same house number
# on other streets, the same street in another quadrant, and named places
# whose names begin with the same digits.
NOISY_PLACES = [
    Place("801 3rd Street Northwest", 38.8993, -77.0158, "address", "Washington, DC"),
    Place("803 3rd Street Northwest", 38.8994, -77.0158, "address", "Washington, DC"),
    Place("799 3rd Street Northwest", 38.8991, -77.0158, "address", "Washington, DC"),
    Place("1401 3rd Street Northwest", 38.9090, -77.0158, "address", "Washington, DC"),
    Place("3rd Street Northwest", 38.9000, -77.0160, "street", "Washington, DC"),
    Place("3rd Street Southeast", 38.8830, -76.9960, "street", "Washington, DC"),
    Place("801 3rd Street Southeast", 38.8831, -76.9960, "address", "Washington, DC"),
    Place("801 Pennsylvania Avenue Northwest", 38.8950, -77.0230, "address", ""),
    Place("801 K Street Northwest", 38.9020, -77.0230, "address", "Washington, DC"),
    Place("801 Restaurant", 38.9100, -77.0400, "food", "801 17th Street NW"),
    Place("8010 Wisconsin Avenue", 38.9900, -77.0970, "address", "Bethesda, MD"),
    Place("Third Street Tunnel", 38.8960, -77.0160, "other", "Washington, DC"),
]


@pytest.fixture(scope="module")
def noisy() -> PlaceIndex:
    return PlaceIndex(places=list(NOISY_PLACES)).build()


def test_the_exact_address_wins_outright(noisy):
    """The case that prompted the rewrite."""
    results = names(noisy.search("801 3rd St NW"))
    assert results[0] == "801 3rd Street Northwest"


def test_every_spelling_of_the_same_address_agrees(noisy):
    for query in (
        "801 3rd St NW",
        "801 3rd Street NW",
        "801 3rd street northwest",
        "801 3RD ST NW",
        "801 3rd NW",
    ):
        assert names(noisy.search(query))[0] == "801 3rd Street Northwest", query


def test_neighbouring_house_numbers_come_next(noisy):
    """Address data is never complete, so near neighbours are useful."""
    results = names(noisy.search("801 3rd St NW"))
    assert set(results[1:3]) == {"803 3rd Street Northwest", "799 3rd Street Northwest"}


def test_the_street_is_offered_but_below_the_doorways(noisy):
    results = names(noisy.search("801 3rd St NW"))
    assert "3rd Street Northwest" in results
    assert results.index("3rd Street Northwest") > results.index(
        "801 3rd Street Northwest"
    )


def test_the_wrong_quadrant_does_not_outrank_the_right_one(noisy):
    results = names(noisy.search("801 3rd St NW"))
    assert "801 3rd Street Southeast" not in results[:3]


def test_the_same_number_on_another_street_is_excluded(noisy):
    results = names(noisy.search("801 3rd St NW"))
    for wrong in (
        "801 Pennsylvania Avenue Northwest",
        "801 K Street Northwest",
        "8010 Wisconsin Avenue",
    ):
        assert wrong not in results, wrong


def test_a_name_that_starts_with_the_same_digits_is_not_an_address(noisy):
    assert "801 Restaurant" not in names(noisy.search("801 3rd St NW"))
    # But it is still findable by name.
    assert "801 Restaurant" in names(noisy.search("801 Restaurant"))


def test_a_far_house_number_on_the_right_street_ranks_low(noisy):
    results = names(noisy.search("801 3rd St NW"))
    if "1401 3rd Street Northwest" in results:
        assert results.index("1401 3rd Street Northwest") > results.index(
            "3rd Street Northwest"
        )


def test_a_bare_street_still_leads_with_the_street(noisy):
    results = names(noisy.search("3rd St NW"))
    assert results[0] == "3rd Street Northwest"


def test_a_bare_street_respects_the_quadrant(noisy):
    assert names(noisy.search("3rd St NW"))[0] == "3rd Street Northwest"
    assert names(noisy.search("3rd St SE"))[0] == "3rd Street Southeast"


def test_another_address_on_another_street_still_works(noisy):
    assert names(noisy.search("801 Penn Ave NW"))[0] == (
        "801 Pennsylvania Avenue Northwest"
    )
    assert names(noisy.search("801 K St NW"))[0] == "801 K Street Northwest"
