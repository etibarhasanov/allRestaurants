"""Record linkage between a curated list and a collected dataset.

Every case here was first found by hand-checking a real comparison, then
turned into a test so the matcher decides it rather than a person.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from crosscheck import (  # noqa: E402
    Linker, address_key, compare, is_closed, name_score, norm, phone_key, tokens,
)


def rec(name, lat, lng, phone="", address=""):
    return {"name": name, "lat": lat, "lng": lng, "phone": phone, "address": address}


# -- keys ------------------------------------------------------------------

def test_phone_key_ignores_country_code_and_spacing():
    assert phone_key("+372 661 0180") == phone_key("6610180") == "6610180"
    assert phone_key("+372 5395 7134") == phone_key("372 5395 7134") == "53957134"
    assert phone_key("00372 661 0180") == "6610180"


def test_phone_key_handles_both_landline_and_mobile_lengths():
    """A 7-digit landline and an 8-digit mobile cannot share one fixed slice."""
    assert phone_key("+372 661 0180") == "6610180"      # landline, 7
    assert phone_key("+372 5630 3977") == "56303977"    # mobile, 8
    assert phone_key("+372 372 3720") == "3723720"      # digits repeating the code


def test_phone_key_rejects_junk():
    assert phone_key("") is None
    assert phone_key("n/a") is None
    assert phone_key("123") is None


def test_address_key_survives_estonian_street_markers():
    assert address_key("Laboratooriumi tn 23, 10133 Tallinn") == "laboratooriumi 23"
    assert address_key("Laboratooriumi 23, 10133 Tallinn") == "laboratooriumi 23"
    assert address_key("Tartu mnt 50, 10115 Tallinn") == "tartu 50"


def test_address_key_keeps_the_unit_that_separates_neighbours():
    """Telliskivi 60-2 and 60M are different businesses in one block."""
    assert address_key("Telliskivi tn 60-2") != address_key("Telliskivi 60M")


def test_address_key_needs_both_street_and_number():
    assert address_key("Kopli, 10412 Tallinn") is None
    assert address_key("") is None


def test_name_normalisation_handles_symbols_and_diacritics():
    assert "degrees" in norm("180° by Matthias Diether")
    assert norm("Sfäär") == norm("Sfaar")
    assert tokens("Restoran Olde Hansa") == ["olde", "hansa"]


# -- the matches a name matcher gets wrong ---------------------------------

def test_phone_links_records_whose_names_share_nothing():
    """Lb23 is listed by its street address; the phone number settles it."""
    curated = rec("Laboratooriumi 23", 59.44071, 24.74440, "+372 5395 7134",
                  "Laboratooriumi 23, 10133 Tallinn")
    collected = [rec("Lb23", 59.44071, 24.74440, "+372 5395 7134",
                     "Laboratooriumi tn 23, 10133 Tallinn")]
    m = Linker(collected).find(curated)
    assert m.matched and m.evidence == "phone"
    assert name_score(curated["name"], "Lb23") < 0.55, "names alone would fail"


def test_phone_links_a_degree_symbol_to_its_spelled_form():
    curated = rec("180° by Matthias Diether", 59.4520, 24.7289, "+372 661 0180")
    collected = [rec("180 Degrees Restaurant", 59.4520, 24.7289, "+372 661 0180")]
    assert Linker(collected).find(curated).evidence == "phone"


def test_phone_links_a_renamed_branch():
    curated = rec("KotKot", 59.4420, 24.7404, "+372 5630 3977")
    collected = [rec("kot.NOBLESSNER", 59.4420, 24.7404, "+372 5630 3977")]
    assert Linker(collected).find(curated).evidence == "phone"


def test_phone_wins_when_the_sources_disagree_about_position():
    """A curated list may point at the street door, Google at the unit."""
    curated = rec("La Boulangerie", 59.4409, 24.7583, "+372 5855 0320")
    collected = [rec("La Boulangerie", 59.4421, 24.7601, "+372 5855 0320")]
    m = Linker(collected).find(curated)
    assert m.matched and m.evidence == "phone"
    assert m.distance_m > 120, "position alone would have rejected this"


def test_address_links_when_no_phone_is_recorded():
    curated = rec("Some Bakery", 59.4360, 24.7452, "", "Suur-Karja 12, 10140 Tallinn")
    collected = [rec("Different Trading Name", 59.4360, 24.7452, "",
                     "Suur-Karja tn 12, 10140 Tallinn, Estonia")]
    assert Linker(collected).find(curated).evidence == "address"


# -- the false matches proximity produces ----------------------------------

def test_neighbours_with_different_phones_do_not_match():
    """Pilsneri baar and Uus Laine sit 13 m apart and are different bars."""
    curated = rec("Pilsneri baar", 59.4437, 24.7392, "+372 5555 0001",
                  "Kopli, 10412 Tallinn")
    collected = [rec("Uus Laine", 59.4437, 24.7393, "+372 5555 0002",
                     "Vana-Kalamaja tn 1, 10412 Tallinn")]
    assert not Linker(collected).find(curated).matched


def test_same_building_different_business_does_not_match():
    """The Brick Coffee Roastery vs Melt Froyo, 10 m apart."""
    curated = rec("The Brick Coffee Roastery", 59.4390, 24.7280,
                  "+372 5668 2150", "Telliskivi 60M, 10412 Tallinn")
    collected = [rec("Melt Froyo", 59.4390, 24.7281, "+372 5668 9999",
                     "Telliskivi tn 60-2, 10412 Tallinn")]
    assert not Linker(collected).find(curated).matched


def test_a_similar_name_far_away_does_not_match():
    curated = rec("Kohvik Kesklinn", 59.4300, 24.7500, "")
    collected = [rec("Kohvik Kesklinn", 59.5000, 24.9000, "")]
    assert not Linker(collected).find(curated).matched


# -- closures --------------------------------------------------------------

def test_closed_places_are_excluded_not_counted_as_misses():
    """Google never returns a closed place, so no sweep can find one."""
    targets = [
        rec("Open Place", 59.44, 24.75, "+372 111 1111"),
        {**rec("Shut Place", 59.44, 24.75, "+372 222 2222"), "closed": True},
    ]
    collected = [rec("Open Place", 59.44, 24.75, "+372 111 1111")]
    matches, misses, closed = compare(targets, collected)
    assert len(matches) == 1
    assert len(misses) == 0
    assert [c["name"] for c in closed] == ["Shut Place"]


def test_is_closed_accepts_the_string_forms_json_produces():
    assert is_closed({"closed": True}) and is_closed({"closed": "true"})
    assert not is_closed({"closed": False})
    assert not is_closed({"closed": ""})
    assert not is_closed({})


def test_a_record_with_no_coordinates_still_links_on_phone():
    curated = {"name": "No Coords", "phone": "+372 333 3333"}
    collected = [rec("No Coords Cafe", 59.44, 24.75, "+372 333 3333")]
    assert Linker(collected).find(curated).evidence == "phone"


# -- a strong key that disagrees vetoes a weaker one -----------------------

def test_a_contradicting_phone_vetoes_an_address_match():
    """Kokomo Coffee Roasters and KIOSK NO3 share a doorway at Ankru 10.

    The address agrees and they are 32 m apart, but the phone numbers differ,
    so they are different businesses.
    """
    curated = rec("Kokomo Coffee Roasters", 59.4552, 24.6757, "+372 5624 0970",
                  "Ankru 10, 11713 Tallinn")
    collected = [rec("KIOSK NO3", 59.4551, 24.6756, "+372 524 5645",
                     "Ankru tn 10, 11713 Tallinn")]
    assert not Linker(collected).find(curated).matched


def test_a_contradicting_phone_vetoes_a_weak_name_match():
    """Similar-ish names at one spot, different lines: different businesses."""
    curated = rec("Central Coffee House", 59.44, 24.75, "+372 111 1111")
    collected = [rec("Centrum Grill", 59.44, 24.75, "+372 222 2222")]
    assert not Linker(collected).find(curated).matched


def test_identical_names_outvote_a_disagreeing_phone():
    """Deliberate: one source's number is stale far more often than two
    identically-named businesses share a doorstep."""
    curated = rec("Cafe Central", 59.44, 24.75, "+372 111 1111")
    collected = [rec("Cafe Central", 59.44, 24.75, "+372 222 2222")]
    assert Linker(collected).find(curated).matched


def test_a_missing_phone_on_either_side_is_not_a_contradiction():
    """Lb23 has no phone in the collected data; the address still identifies it."""
    curated = rec("Laboratooriumi 23", 59.44071, 24.74440, "+372 5395 7134",
                  "Laboratooriumi 23, 10133 Tallinn")
    collected = [rec("Lb23", 59.44071, 24.74440, "",
                     "Laboratooriumi tn 23, 10133 Tallinn")]
    assert Linker(collected).find(curated).evidence == "address"


def test_a_stale_phone_does_not_veto_an_otherwise_identical_record():
    """Crustum Bakery: same name, same address, 0 m apart, different numbers.

    One source simply holds an out-of-date number. Two different businesses do
    not share a name *and* an address.
    """
    curated = rec("Crustum Bakery", 59.3900, 24.6800, "+372 5770 4909",
                  "Iva 12, Mustamäe, 12618 Tallinn")
    collected = [rec("Crustum Bakery", 59.3900, 24.6800, "+372 5344 7119",
                     "Iva tn 12, 12618 Tallinn")]
    assert Linker(collected).find(curated).matched


def test_the_veto_still_applies_when_the_names_differ():
    """Kokomo vs KIOSK NO3 at one address: nothing vouches for them being one."""
    curated = rec("Kokomo Coffee Roasters", 59.4552, 24.6757, "+372 5624 0970",
                  "Ankru 10, 11713 Tallinn")
    collected = [rec("KIOSK NO3", 59.4551, 24.6756, "+372 524 5645",
                     "Ankru tn 10, 11713 Tallinn")]
    assert not Linker(collected).find(curated).matched


# -- diagnosing a miss -----------------------------------------------------

def cell(lat, lng, r, saturated=False):
    return {"lat": lat, "lng": lng, "radius_m": r, "saturated": saturated}


def test_diagnose_reports_an_area_never_covered():
    from crosscheck import NEVER_SEARCHED, diagnose
    reason, d = diagnose(rec("Far Away", 59.60, 24.90), [cell(59.44, 24.75, 300)])
    assert reason == NEVER_SEARCHED
    assert d["gap_m"] > 0


def test_diagnose_reports_saturation_when_every_circle_was_full():
    from crosscheck import SEARCHED_SATURATED, diagnose
    cells = [cell(59.44, 24.75, 300, True), cell(59.4401, 24.7501, 200, True)]
    reason, d = diagnose(rec("Buried", 59.44, 24.75), cells)
    assert reason == SEARCHED_SATURATED
    assert d["circles"] == 2 and d["saturated"] == 2


def test_diagnose_points_at_filtering_when_a_circle_had_room():
    """A circle that returned fewer than 20 saw everything it was asked for,
    so a place inside it was excluded on type or on the review bar."""
    from crosscheck import SEARCHED_NOT_RETURNED, diagnose
    cells = [cell(59.44, 24.75, 300, False)]
    reason, _ = diagnose(rec("Filtered Out", 59.44, 24.75), cells)
    assert reason == SEARCHED_NOT_RETURNED


def test_diagnose_handles_a_record_without_coordinates():
    from crosscheck import NO_POSITION, diagnose
    reason, _ = diagnose({"name": "No Coords"}, [cell(59.44, 24.75, 300)])
    assert reason == NO_POSITION
