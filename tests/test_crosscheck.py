"""Name matching across sources - the part that quietly ruins a coverage check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from crosscheck import Catalogue, norm, score, tokens  # noqa: E402


def cat(*names):
    return Catalogue([{"name": n} for n in names])


def test_generic_words_are_not_identity():
    assert tokens("Restoran Olde Hansa") == ["olde", "hansa"]
    assert tokens("Cafe Maiasmokk") == ["maiasmokk"]


def test_a_name_made_only_of_generic_words_keeps_them():
    assert tokens("The Restaurant") == ["the", "restaurant"]


def test_diacritics_do_not_break_matching():
    assert score("Härg", "Restoran Härg") == 1.0
    assert score("Sfäär", "Sfaar") > 0.9


def test_prefix_and_suffix_noise_still_matches():
    assert score("Olde Hansa", "Restoran Olde Hansa") == 1.0
    assert score("Peppersack", "Peppersack Restaurant") >= 0.9


def test_extra_words_in_the_middle_still_match():
    """The real case: a guide's name is a subset of Google's, with words between."""
    assert score("Põhjala Tap Room", "Põhjala Brewery & Tap Room") >= 0.9


def test_a_short_candidate_cannot_score_a_perfect_match():
    """Regression: dividing containment by the shorter side let a one-token
    candidate match anything. 'Kitchen Room' reduces to 'room', which scored
    1.0 against 'Pohjala Tap Room' and hid a place that was present."""
    assert score("Põhjala Tap Room", "Kitchen Rõõm") < 0.6


def test_short_names_require_an_exact_token():
    """Cru/Rado/Salt collide with too much to be matched fuzzily."""
    row, s = cat("ORU Bistro", "Radio", "SushiArt").find("Cru")
    assert s < 0.72, "Cru must not match ORU Bistro"

    row, s = cat("Radio", "Rado Resto").find("Rado")
    assert s == 1.0 and row["name"] == "Rado Resto"


def test_find_returns_the_best_of_several_candidates():
    row, s = cat("Vegan Restoran V", "Vesta", "VESTA Restaurant").find("Vesta")
    assert row["name"] in ("Vesta", "VESTA Restaurant")
    assert s >= 0.72


def test_absent_name_is_reported_missing():
    row, s = cat("Olde Hansa", "F-Hoone").find("NOA Chef's Hall")
    assert s < 0.72
