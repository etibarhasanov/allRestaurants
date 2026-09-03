"""Sweep behaviour, driven by a fake Places API with a known ground truth."""

import math

import pytest

from allrestaurants.geo import Circle, haversine_m
from allrestaurants.places import MAX_RESULTS_PER_CALL, BudgetExhausted, PlacesError
from allrestaurants.scrape import Sweeper
from allrestaurants.store import Store


class FakePlaces:
    """Serves the 20 nearest of a fixed set of restaurants, like the real API."""

    def __init__(self, places, fail_at=None, budget=None):
        self.places = places  # list of (id, lat, lng)
        self.request_count = 0
        self.fail_at = fail_at or set()
        self.budget = budget
        self.calls = []

    def search_nearby(self, circle, **kwargs):
        if self.budget is not None and self.request_count >= self.budget:
            raise BudgetExhausted("out of budget")
        self.request_count += 1
        self.calls.append(circle)
        if circle.key in self.fail_at:
            raise PlacesError("simulated API failure")
        inside = [
            (haversine_m(circle.lat, circle.lng, lat, lng), pid, lat, lng)
            for pid, lat, lng in self.places
            if haversine_m(circle.lat, circle.lng, lat, lng) <= circle.radius_m
        ]
        inside.sort()
        return [
            {
                "id": pid,
                "displayName": {"text": f"R{pid}"},
                "location": {"latitude": lat, "longitude": lng},
                "rating": 4.0,
            }
            for _, pid, lat, lng in inside[:MAX_RESULTS_PER_CALL]
        ]


def dense_cluster(count, lat=40.0, lng=49.0, spread_m=120.0):
    """`count` restaurants packed tightly enough to saturate one circle."""
    out = []
    for i in range(count):
        angle = 2 * math.pi * i / count
        radius = spread_m * (0.3 + 0.7 * ((i % 5) / 4))
        dlat = (math.sin(angle) * radius) / 111_320.0
        dlng = (math.cos(angle) * radius) / (111_320.0 * math.cos(math.radians(lat)))
        out.append((f"p{i}", lat + dlat, lng + dlng))
    return out


def test_sparse_area_needs_no_splitting(tmp_path):
    places = [("a", 40.0, 49.0), ("b", 40.001, 49.001)]
    client = FakePlaces(places)
    store = Store(str(tmp_path / "t.db"))
    stats = Sweeper(client, store, workers=1).run([Circle(40.0, 49.0, 500.0)])

    assert stats.cells_searched == 1
    assert stats.cells_split == 0
    assert store.count() == 2
    store.close()


def test_saturated_circle_is_split_until_everything_is_found(tmp_path):
    """The whole point: 20 results back means more are hiding, so go deeper."""
    places = dense_cluster(60)
    client = FakePlaces(places)
    store = Store(str(tmp_path / "t.db"))

    stats = Sweeper(client, store, workers=1, min_radius_m=5.0, max_depth=8).run(
        [Circle(40.0, 49.0, 400.0)]
    )

    assert stats.cells_split > 0, "a full circle should have been split"
    assert stats.max_depth > 0
    assert store.count() == 60, "a single 20-result call would have found only 20"
    store.close()


def test_split_stops_at_the_radius_floor(tmp_path):
    client = FakePlaces(dense_cluster(60, spread_m=30.0))
    store = Store(str(tmp_path / "t.db"))
    stats = Sweeper(client, store, workers=1, min_radius_m=300.0).run(
        [Circle(40.0, 49.0, 400.0)]
    )
    assert stats.max_depth == 0
    store.close()


def test_split_stops_at_the_depth_limit(tmp_path):
    client = FakePlaces(dense_cluster(60))
    store = Store(str(tmp_path / "t.db"))
    stats = Sweeper(client, store, workers=1, min_radius_m=1.0, max_depth=2).run(
        [Circle(40.0, 49.0, 400.0)]
    )
    assert stats.max_depth == 2
    store.close()


def test_resume_skips_circles_already_searched(tmp_path):
    db = str(tmp_path / "t.db")
    places = [("a", 40.0, 49.0)]
    circles = [Circle(40.0, 49.0, 500.0), Circle(40.01, 49.0, 500.0)]

    store = Store(db)
    Sweeper(FakePlaces(places), store, workers=1).run(circles)
    store.close()

    store = Store(db)
    client = FakePlaces(places)
    stats = Sweeper(client, store, workers=1).run(circles)
    assert client.request_count == 0
    assert stats.cells_skipped == 2
    store.close()


def test_no_resume_searches_everything_again(tmp_path):
    db = str(tmp_path / "t.db")
    circles = [Circle(40.0, 49.0, 500.0)]
    store = Store(db)
    Sweeper(FakePlaces([("a", 40.0, 49.0)]), store, workers=1).run(circles)
    store.close()

    store = Store(db)
    client = FakePlaces([("a", 40.0, 49.0)])
    Sweeper(client, store, workers=1, resume=False).run(circles)
    assert client.request_count == 1
    store.close()


def test_one_failing_circle_does_not_abort_the_sweep(tmp_path):
    bad = Circle(40.01, 49.0, 500.0)
    circles = [Circle(40.0, 49.0, 500.0), bad, Circle(40.02, 49.0, 500.0)]
    client = FakePlaces([("a", 40.0, 49.0)], fail_at={bad.key})
    store = Store(str(tmp_path / "t.db"))

    stats = Sweeper(client, store, workers=1).run(circles)
    assert stats.cells_failed == 1
    assert stats.cells_searched == 2
    store.close()


def test_budget_exhaustion_stops_cleanly_and_keeps_progress(tmp_path):
    client = FakePlaces(dense_cluster(60), budget=3)
    store = Store(str(tmp_path / "t.db"))
    stats = Sweeper(client, store, workers=1, min_radius_m=5.0).run(
        [Circle(40.0, 49.0, 400.0)]
    )

    assert stats.stopped_early
    assert store.count() > 0, "places found before the budget ran out must be kept"
    store.close()


def test_concurrent_workers_produce_the_same_result(tmp_path):
    places = dense_cluster(60)
    circles = [Circle(40.0, 49.0, 400.0)]

    serial = Store(str(tmp_path / "serial.db"))
    Sweeper(FakePlaces(places), serial, workers=1, min_radius_m=5.0).run(circles)
    serial_count = serial.count()
    serial.close()

    parallel = Store(str(tmp_path / "parallel.db"))
    Sweeper(FakePlaces(places), parallel, workers=8, min_radius_m=5.0).run(circles)
    assert parallel.count() == serial_count == 60
    parallel.close()


def test_split_only_if_new_prunes_circles_that_add_nothing(tmp_path):
    """The main cost lever: measured at ~13x fewer calls on a dense fixture."""
    places = dense_cluster(60)
    circles = [Circle(40.0, 49.0, 400.0)]

    thorough = Store(str(tmp_path / "thorough.db"))
    exhaustive = FakePlaces(places)
    Sweeper(exhaustive, thorough, workers=1, min_radius_m=5.0, max_depth=8).run(circles)
    thorough.close()

    pruned = Store(str(tmp_path / "pruned.db"))
    cheap = FakePlaces(places)
    stats = Sweeper(
        cheap, pruned, workers=1, min_radius_m=5.0, max_depth=8,
        split_only_if_new=True,
    ).run(circles)

    assert stats.cells_pruned > 0
    assert cheap.request_count < exhaustive.request_count
    pruned.close()


def test_resume_rebuilds_the_pending_split_frontier(tmp_path):
    """A run cut short mid-split must resume deeper, not declare itself done.

    Regression: resume used to skip an already-searched circle without
    re-queueing the children it had been split into, so the second run found
    nothing left to do and reported a truncated sweep as complete.
    """
    db = str(tmp_path / "t.db")
    places = dense_cluster(60)
    circles = [Circle(40.0, 49.0, 400.0)]

    store = Store(db)
    first = FakePlaces(places, budget=3)
    stats = Sweeper(first, store, workers=1, min_radius_m=5.0, max_depth=8).run(circles)
    partial = store.count()
    store.close()
    assert stats.stopped_early, "fixture should have run out of budget"

    store = Store(db)
    second = FakePlaces(places)
    Sweeper(second, store, workers=1, min_radius_m=5.0, max_depth=8).run(circles)

    assert second.request_count > 0, "resume made no calls at all"
    assert store.count() > partial, "resume added nothing"
    assert store.count() == 60, "resume did not reach the full set"
    store.close()


def test_resume_does_not_requeue_children_of_unsplit_circles(tmp_path):
    db = str(tmp_path / "t.db")
    circles = [Circle(40.0, 49.0, 500.0)]
    store = Store(db)
    Sweeper(FakePlaces([("a", 40.0, 49.0)]), store, workers=1).run(circles)
    store.close()

    store = Store(db)
    client = FakePlaces([("a", 40.0, 49.0)])
    Sweeper(client, store, workers=1).run(circles)
    assert client.request_count == 0
    store.close()
