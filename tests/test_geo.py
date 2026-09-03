import math

import pytest

from allrestaurants.geo import (
    COVERAGE_MARGIN,
    Circle,
    cover_bbox,
    cover_radius,
    haversine_m,
    offset,
    parse_bbox,
    parse_latlng,
)


def test_offset_moves_the_expected_distance():
    """Flat-earth offsets are ~0.1% off at mid latitudes; COVERAGE_MARGIN covers it."""
    lat, lng = offset(40.4093, 49.8671, east_m=1000, north_m=0)
    assert haversine_m(40.4093, 49.8671, lat, lng) == pytest.approx(1000, rel=5e-3)


def test_children_cover_the_parent_circle():
    """Every point of the parent must fall inside at least one child."""
    parent = Circle(40.4093, 49.8671, 1000.0)
    children = parent.children()
    assert len(children) == 4

    worst = 0.0
    for i in range(72):
        angle = 2 * math.pi * i / 72
        for frac in (0.25, 0.5, 0.75, 1.0):
            east = math.cos(angle) * parent.radius_m * frac
            north = math.sin(angle) * parent.radius_m * frac
            lat, lng = offset(parent.lat, parent.lng, east, north)
            nearest = min(haversine_m(lat, lng, c.lat, c.lng) for c in children)
            worst = max(worst, nearest / children[0].radius_m)
    assert worst <= 1.0, f"a point sat {worst:.3f} child-radii from every child"


def test_children_shrink_and_track_depth():
    child = Circle(0.0, 0.0, 800.0, depth=2).children()[0]
    assert child.radius_m == pytest.approx(800.0 / math.sqrt(2) / COVERAGE_MARGIN)
    assert child.depth == 3


def test_cover_bbox_leaves_no_gaps():
    south, west, north, east = 40.38, 49.83, 40.42, 49.90
    circles = cover_bbox(south, west, north, east, 400.0)
    assert circles

    # Sample the box densely; every sample must be inside some circle.
    for i in range(21):
        for j in range(21):
            lat = south + (north - south) * i / 20
            lng = west + (east - west) * j / 20
            assert any(
                haversine_m(lat, lng, c.lat, c.lng) <= c.radius_m for c in circles
            ), f"gap at {lat},{lng}"


def test_cover_radius_covers_the_disc_and_trims_far_cells():
    lat, lng, radius = 40.4093, 49.8671, 3000.0
    circles = cover_radius(lat, lng, radius, 500.0)
    assert circles

    for c in circles:
        assert haversine_m(lat, lng, c.lat, c.lng) <= radius + c.radius_m

    for i in range(60):
        angle = 2 * math.pi * i / 60
        for frac in (0.0, 0.5, 0.99):
            east = math.cos(angle) * radius * frac
            north = math.sin(angle) * radius * frac
            plat, plng = offset(lat, lng, east, north)
            assert any(
                haversine_m(plat, plng, c.lat, c.lng) <= c.radius_m for c in circles
            ), f"gap at {plat},{plng}"


def test_circle_key_is_stable_and_distinct():
    assert Circle(1.0, 2.0, 300.0).key == Circle(1.0, 2.0, 300.0, depth=4).key
    assert Circle(1.0, 2.0, 300.0).key != Circle(1.0, 2.0, 150.0).key


def test_parse_helpers():
    assert parse_latlng(" 40.4093 , 49.8671 ") == (40.4093, 49.8671)
    assert parse_bbox("40.38,49.83,40.42,49.90") == (40.38, 49.83, 40.42, 49.90)
    with pytest.raises(ValueError):
        parse_latlng("40.4093")
    with pytest.raises(ValueError):
        parse_latlng("95,10")
    with pytest.raises(ValueError):
        parse_bbox("40.42,49.83,40.38,49.90")


def test_cell_radius_for_budget_scales_with_area_and_budget():
    from allrestaurants.geo import cell_radius_for_budget

    area = math.pi * 5000**2
    small_budget = cell_radius_for_budget(area, 50)
    large_budget = cell_radius_for_budget(area, 500)
    assert small_budget > large_budget, "a smaller budget needs bigger circles"

    bigger_area = cell_radius_for_budget(math.pi * 10000**2, 50)
    assert bigger_area > small_budget, "a bigger area needs bigger circles"

    with pytest.raises(ValueError):
        cell_radius_for_budget(area, 0)


def test_budget_sizing_fits_the_grid_inside_the_budget():
    """Regression: --budget 50 used to build a 52-circle starting grid."""
    from types import SimpleNamespace

    from allrestaurants.cli import _build_circles

    for radius_km in (1, 2, 5, 8, 20):
        for budget in (20, 50, 100, 300):
            args = SimpleNamespace(
                bbox=None, center="59.4370,24.7536",
                radius_km=radius_km, cell_radius_m=1000.0, budget=budget,
            )
            circles = _build_circles(args)
            assert len(circles) <= budget * 0.7 + 1, (
                f"{radius_km}km at budget {budget}: {len(circles)} starting circles"
            )
