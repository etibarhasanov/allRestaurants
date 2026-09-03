"""The sweep itself: move the pin across an area and split where it saturates."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .geo import Circle
from .models import normalize_place
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

    A circle that returns the API's maximum of 20 results is almost certainly
    hiding more restaurants, so it gets replaced by four smaller circles and
    searched again.  Recursion stops at ``min_radius_m`` (or ``max_depth``),
    below which further splitting costs more in API calls than it finds.
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
        resume: bool = True,
        progress_every: int = 25,
        split_only_if_new: bool = False,
    ):
        self.client = client
        self.store = store
        self.included_types = list(included_types)
        self.min_radius_m = min_radius_m
        self.max_depth = max_depth
        self.workers = max(1, workers)
        self.language_code = language_code
        self.region_code = region_code
        self.rank_preference = rank_preference
        self.resume = resume
        self.progress_every = progress_every
        self.split_only_if_new = split_only_if_new
        self.stats = SweepStats()

    def _should_split(self, circle: Circle, count: int, new_here: int) -> bool:
        if count < MAX_RESULTS_PER_CALL:
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
        if self.resume and self.store.cell_done(circle.key):
            self.stats.cells_skipped += 1
            return []

        places = self.client.search_nearby(
            circle,
            included_types=self.included_types,
            language_code=self.language_code,
            region_code=self.region_code,
            rank_preference=self.rank_preference,
        )
        new_here = 0
        for raw in places:
            row = normalize_place(raw)
            if self.store.upsert_place(row, raw):
                new_here += 1
        self.stats.new_places += new_here

        count = len(places)
        split = self._should_split(circle, count, new_here)
        self.stats.cells_searched += 1
        self.stats.results_seen += count
        self.stats.max_depth = max(self.stats.max_depth, circle.depth)
        self.store.record_cell(circle, count, count >= MAX_RESULTS_PER_CALL, split)

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
