"""The cost estimate, which has been wrong in the expensive direction before.

The multiplier estimate priced Tallinn's district passes at 286 calls against
an actual 171.  The measured law that replaced it is fitted in
etibarhasanov/allBerlin by replaying this sweep over real coordinates; these
tests pin it to the run it was fitted to.
"""

import math

import pytest

from allrestaurants.cli import (APPROX_PRICE_PER_CALL, FREE_CALLS_PER_MONTH,
                                MEASURED_CALLS_CONSTANT, _measured_calls, main)


def test_the_law_reproduces_the_sweep_it_was_fitted_to():
    """1,110 Tallinn places over a 207 km2 bbox measured 2,390 calls."""
    assert _measured_calls(1110, 207, base_circles=1, min_reviews=25) == \
        pytest.approx(2390, rel=0.05)


def test_cost_follows_the_geometric_mean_not_either_term():
    """Doubling the places at fixed area costs sqrt(2), not 2: one call
    returns up to twenty places however densely they sit."""
    a = _measured_calls(1000, 100, 1, 25)
    b = _measured_calls(2000, 100, 1, 25)
    c = _measured_calls(1000, 200, 1, 25)
    assert b / a == pytest.approx(math.sqrt(2), rel=0.01)
    assert c / a == pytest.approx(math.sqrt(2), rel=0.01)


def test_calls_per_place_fall_as_density_rises():
    sparse = _measured_calls(1000, 400, 1, 25) / 1000
    dense = _measured_calls(4000, 400, 1, 25) / 4000
    assert dense < sparse


def test_the_starting_grid_is_a_floor():
    """Empty ground still costs one call per circle."""
    assert _measured_calls(0, 100, base_circles=250, min_reviews=25) == 250
    assert _measured_calls(5, 100, base_circles=250, min_reviews=25) == 250


def test_census_costs_about_six_times_a_review_bar_sweep():
    """Measured on this repo's own fixture: 2,176 calls against 358."""
    bar = _measured_calls(1000, 100, 1, min_reviews=25)
    census = _measured_calls(1000, 100, 1, min_reviews=0)
    assert census / bar == pytest.approx(2176 / 358, rel=0.01)


def test_estimate_runs_and_reports_the_free_tier(capsys):
    assert main(["estimate", "--bbox", "52.505,13.320,52.570,13.420",
                 "--expect-places", "2415", "--cell-radius-m", "300"]) == 0
    out = capsys.readouterr().out
    assert "measured law" in out
    assert "free this month" in out
    assert "after the free tier" in out
    # The retired $200 credit must not be quoted as if it still existed.
    assert "$200 monthly credit was retired" in out


def test_estimate_without_a_place_count_says_so(capsys):
    assert main(["estimate", "--center", "59.4372,24.7453", "--radius-km", "1"]) == 0
    out = capsys.readouterr().out
    assert "--expect-places" in out
    assert "(sparse area)" in out


def test_estimate_respects_the_budget_cap(capsys):
    main(["estimate", "--center", "59.4372,24.7453", "--radius-km", "5",
          "--budget", "100", "--expect-places", "5000"])
    out = capsys.readouterr().out
    calls = int(_field(out, "API calls (estimate)").split()[0].replace(",", ""))
    assert calls <= 100


def test_ids_tier_is_not_free_on_nearby_search(capsys):
    """Text Search and Place Details have a free IDs-Only SKU; Nearby Search
    does not. This once printed $0.00, and a whole census was priced on it."""
    main(["estimate", "--bbox", "52.505,13.320,52.570,13.420",
          "--expect-places", "2415", "--tier", "ids"])
    out = capsys.readouterr().out
    assert "$0.00" not in out.split("free this month")[0]
    assert "no IDs-Only SKU" in out
    assert APPROX_PRICE_PER_CALL["ids"] == APPROX_PRICE_PER_CALL["standard"]


def test_price_table_matches_the_free_tier_table():
    assert set(APPROX_PRICE_PER_CALL) == set(FREE_CALLS_PER_MONTH)
    assert FREE_CALLS_PER_MONTH["ids"] == 5_000    # billed at Pro
    assert FREE_CALLS_PER_MONTH["ratings"] == 1_000  # Enterprise
    assert APPROX_PRICE_PER_CALL["ratings"] > APPROX_PRICE_PER_CALL["standard"]


def test_the_constant_is_the_measured_one():
    assert MEASURED_CALLS_CONSTANT == pytest.approx(5.0, abs=0.2)


def _field(text, label):
    for line in text.splitlines():
        if line.strip().startswith(label):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no {label!r} line in output")
