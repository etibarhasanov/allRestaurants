"""The sweep itself: move the pin across an area and split where it saturates."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .geo import Circle
from .models import is_restaurant, normalize_place
from .places import MAX_RESULTS_PER_CALL, BudgetExhausted, PlacesClient, PlacesError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SweepStats:
    cells_searched: int = 0
    cells_skipped: int = 0
    cells_split: int = 0
    cells_failed: int = 0
    cells_pruned: int = 0
    cells_below_bar: int = 0
    skipped_below_bar: int = 0
    skipped_not_restaurant: int = 0
    results_seen: int = 0
    new_places: int = 0
    max_depth: int = 0
    stopped_early: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


class Sweeper:
    """Breadth-first sweep over search circles, splitting saturated ones.

    Two modes, chosen by ``min_reviews``:

    *Prominence* (``min_reviews > 0``, the default).  Results come back ranked
    by popularity, so each circle hands back the 20 most established places in
    it.  The stopping signal is the weakest of those 20: if even it clears the
    review bar, better places are probably hidden behind it and the circle is
    worth splitting; the moment a circle returns anything below the bar, its
    tail is in view and there is nothing left worth finding.  This costs an
    order of magnitude fewer calls than a census, because it stops as soon as
    the results stop being interesting rather than when they run out.

    *Census* (``min_reviews = 0``).  Results come back ranked by distance and
    any circle returning a full 20 is split, until nothing saturates.  This
    enumerates everything, including places with a handful of reviews, and is
    correspondingly expensive.

    Either way recursion also stops at ``min_radius_m`` or ``max_depth``.
    """

    def __init__(
        self,
        client: PlacesClient,
        store: Store,
        included_types: Sequence[str] = ("restaurant",),
        min_radius_m: float = 40.0,
        max_depth: int = 6,
        workers: int = 5,
        language_code: Optional[str] = None,
        region_code: Optional[str] = None,
        rank_preference: str = "DISTANCE",
        min_reviews: int = 0,
        restaurants_only: bool = True,
        resume: bool = True,
        progress_every: int = 25,
        split_only_if_new: bool = False,
    ):
        self.client = client
        self.store = store
        self.included_types = list(included_types)
        # Identifies the resume scope: the same circle searched for restaurants
        # and for cafes are two distinct searches.
        self.scope = ",".join(sorted(self.included_types))
        self.min_radius_m = min_radius_m
        self.max_depth = max_depth
        self.workers = max(1, workers)
        self.language_code = language_code
        self.region_code = region_code
        self.rank_preference = rank_preference
        self.min_reviews = max(0, min_reviews)
        self.restaurants_only = restaurants_only
        self.resume = resume
        self.progress_every = progress_every
        self.split_only_if_new = split_only_if_new
        self.stats = SweepStats()

    def _should_split(
        self, circle: Circle, count: int, new_here: int, below_bar: int
    ) -> bool:
        if count < MAX_RESULTS_PER_CALL:
            return False
        if self.min_reviews and below_bar:
            # Ranked by popularity, so the circle handed back its 20 best and
            # some of them still fell short of the bar.  Everything it did not
            # return ranks below those, so splitting can only surface places we
            # have already decided we do not want.
            self.stats.cells_below_bar += 1
            return False
        if self.split_only_if_new and new_here == 0:
            # Heuristic, and the main cost lever: a full circle whose every
            # result we already have is usually one that neighbouring circles
            # have already covered.  Usually, not always -- the API shows only
            # the top 20, so an unseen place can still hide behind 20 known
            # ones.  Trades a little recall for a lot of quota; off by default.
            self.stats.cells_pruned += 1
            return False
        if circle.depth >= self.max_depth:
            log.debug("depth limit reached at %s; some places may be missed", circle.key)
            return False
        if circle.radius_m / 2 < self.min_radius_m:
            log.debug("radius floor reached at %s; some places may be missed", circle.key)
            return False
        return True

    def _search_one(self, circle: Circle) -> List[Circle]:
        """Search one circle, persist its places, return any children to queue."""
        if self.resume:
            prior = self.store.get_cell(self.store.scoped_key(self.scope, circle.key))
            if prior is not None:
                self.stats.cells_skipped += 1
                # Re-queue the children of a circle that was split, so an
                # interrupted run resumes with its full frontier rather than
                # stopping at whatever level the budget ran out on.
                return circle.children() if prior["split"] else []

        places = self.client.search_nearby(
            circle,
            included_types=self.included_types,
            language_code=self.language_code,
            region_code=self.region_code,
            rank_preference=self.rank_preference,
        )
        new_here = 0
        below_bar = 0
        for raw in places:
            row = normalize_place(raw)
            if self.min_reviews:
                if (row.get("user_rating_count") or 0) < self.min_reviews:
                    below_bar += 1
                    continue
            primary = row.get("primary_type")
            # A type the caller explicitly asked for is never "not a restaurant":
            # --types bar,bakery means bars and bakeries are wanted.
            asked_for = primary in self.included_types
            if self.restaurants_only and not asked_for and not is_restaurant(primary):
                # Still counts toward the circle's 20, so it does not change the
                # split decision -- it just does not belong in the results.
                self.stats.skipped_not_restaurant += 1
                continue
            if self.store.upsert_place(row, raw):
                new_here += 1
        self.stats.new_places += new_here
        self.stats.skipped_below_bar += below_bar

        count = len(places)
        split = self._should_split(circle, count, new_here, below_bar)
        self.stats.cells_searched += 1
        self.stats.results_seen += count
        self.stats.max_depth = max(self.stats.max_depth, circle.depth)
        self.store.record_cell(
            circle, count, count >= MAX_RESULTS_PER_CALL, split, scope=self.scope
        )

        if not split:
            return []
        self.stats.cells_split += 1
        return circle.children()

    def _search_guarded(self, circle: Circle) -> List[Circle]:
        try:
            return self._search_one(circle)
        except BudgetExhausted:
            raise
        except PlacesError as exc:
            # One bad circle should not abandon a sweep that may be hours in.
            self.stats.cells_failed += 1
            log.error("giving up on circle %s: %s", circle.key, exc)
            return []

    def run(self, circles: Sequence[Circle]) -> SweepStats:
        """Sweep every circle, level by level, until nothing is left to split."""
        queue: List[Circle] = list(circles)
        level = 0
        try:
            while queue:
                log.info(
                    "level %d: %d circle(s) of radius ~%.0fm",
                    level,
                    len(queue),
                    queue[0].radius_m,
                )
                next_queue: List[Circle] = []
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    for children in pool.map(self._search_guarded, queue):
                        next_queue.extend(children)
                        done = self.stats.cells_searched + self.stats.cells_skipped
                        if self.progress_every and done % self.progress_every == 0:
                            self._log_progress()
                queue = next_queue
                level += 1
        except BudgetExhausted as exc:
            self.stats.stopped_early = str(exc)
            log.warning("stopping early: %s", exc)
        except KeyboardInterrupt:
            self.stats.stopped_early = "interrupted by user"
            log.warning("interrupted; progress is saved and --resume will continue")
        return self.stats

    def _log_progress(self) -> None:
        log.info(
            "%d circles searched (%d skipped), %d places stored, %d API calls",
            self.stats.cells_searched,
            self.stats.cells_skipped,
            self.store.count(),
            self.client.request_count,
        )
