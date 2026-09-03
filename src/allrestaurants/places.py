"""Thin client for the Google Places API (New).

Only the pieces this project needs: Nearby Search, plus retries and a request
budget so a runaway sweep cannot quietly burn through billing.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence

import requests

from .geo import Circle

log = logging.getLogger(__name__)

NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Hard API limits, not preferences.
MAX_RESULTS_PER_CALL = 20
MAX_RADIUS_M = 50_000.0

# Field masks, grouped by Google's billing SKUs.  Every extra tier costs more
# per call, so the tier is a deliberate choice rather than "ask for everything".
#
#   ids       - Essentials SKU. Just enough to count and de-duplicate places.
#   standard  - Essentials + Pro. Name, address, location, category, status.
#   ratings   - the above + Enterprise. Adds rating, review count, price,
#               phone, website, opening hours.  This is the default: it is the
#               tier that answers "what is this place's average review?".
#   full      - the above + Enterprise/Atmosphere. Adds the editorial blurb and
#               service attributes (takeout, delivery, reservations, ...).
FIELD_TIERS: Dict[str, List[str]] = {
    "ids": [
        "places.id",
        "places.name",
    ],
    "standard": [
        "places.id",
        "places.name",
        "places.displayName",
        "places.formattedAddress",
        "places.shortFormattedAddress",
        "places.addressComponents",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.primaryTypeDisplayName",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.plusCode",
        "places.utcOffsetMinutes",
    ],
    "ratings": [
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.regularOpeningHours",
    ],
    "full": [
        "places.editorialSummary",
        "places.takeout",
        "places.delivery",
        "places.dineIn",
        "places.reservable",
        "places.servesBreakfast",
        "places.servesLunch",
        "places.servesDinner",
        "places.servesBeer",
        "places.servesWine",
        "places.servesVegetarianFood",
        "places.outdoorSeating",
        "places.goodForChildren",
        "places.accessibilityOptions",
        "places.paymentOptions",
        "places.parkingOptions",
    ],
}

# Each tier is cumulative over the ones before it.
TIER_ORDER = ["ids", "standard", "ratings", "full"]


def field_mask(tier: str) -> str:
    """Build the ``X-Goog-FieldMask`` header value for a tier."""
    if tier not in TIER_ORDER:
        raise ValueError(f"unknown field tier {tier!r}; pick one of {TIER_ORDER}")
    fields: List[str] = []
    for name in TIER_ORDER[: TIER_ORDER.index(tier) + 1]:
        for f in FIELD_TIERS[name]:
            if f not in fields:
                fields.append(f)
    return ",".join(fields)


class PlacesError(RuntimeError):
    """A Places API call failed in a way we are not going to retry."""


class BudgetExhausted(RuntimeError):
    """The configured request budget ran out mid-sweep."""


class _RateLimiter:
    """Blocking token bucket, shared across worker threads."""

    def __init__(self, qps: float):
        self._min_interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._min_interval
        if sleep_for:
            time.sleep(sleep_for)


class PlacesClient:
    """Nearby Search with retries, rate limiting and a request budget."""

    def __init__(
        self,
        api_key: str,
        tier: str = "ratings",
        qps: float = 10.0,
        timeout: float = 30.0,
        max_retries: int = 5,
        max_requests: Optional[int] = None,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise PlacesError(
                "No Google Maps API key. Set GOOGLE_MAPS_API_KEY in .env or pass --api-key."
            )
        self.api_key = api_key
        self.field_mask = field_mask(tier)
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.session = session or requests.Session()
        self._limiter = _RateLimiter(qps)
        self._lock = threading.Lock()
        self.request_count = 0

    def _claim_request_slot(self) -> None:
        with self._lock:
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise BudgetExhausted(
                    f"request budget of {self.max_requests} calls is used up"
                )
            self.request_count += 1

    def search_nearby(
        self,
        circle: Circle,
        included_types: Sequence[str] = ("restaurant",),
        max_results: int = MAX_RESULTS_PER_CALL,
        rank_preference: str = "DISTANCE",
        language_code: Optional[str] = None,
        region_code: Optional[str] = None,
    ) -> List[dict]:
        """Return the places inside ``circle``, capped at 20 by the API.

        ``rank_preference`` defaults to DISTANCE rather than Google's usual
        POPULARITY, and that choice is what makes an exhaustive sweep work.
        Ranked by distance, a circle returns its 20 *nearest* places, so
        shrinking it reliably surfaces places the bigger circle hid.  Ranked by
        popularity, the same 20 well-known restaurants keep coming back however
        small the circle gets, and the split can never drill past them.
        """
        radius = min(float(circle.radius_m), MAX_RADIUS_M)
        body = {
            "includedTypes": list(included_types),
            "maxResultCount": min(int(max_results), MAX_RESULTS_PER_CALL),
            "rankPreference": rank_preference,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": circle.lat, "longitude": circle.lng},
                    "radius": radius,
                }
            },
        }
        if language_code:
            body["languageCode"] = language_code
        if region_code:
            body["regionCode"] = region_code

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": self.field_mask,
        }

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            self._claim_request_slot()
            try:
                resp = self.session.post(
                    NEARBY_SEARCH_URL, json=body, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = f"transport error: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json().get("places", []) or []
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:400]}"
                else:
                    raise PlacesError(
                        f"Places API returned HTTP {resp.status_code}: {resp.text[:800]}"
                    )

            if attempt < self.max_retries:
                backoff = min(2**attempt, 30) + random.uniform(0, 0.5)
                log.warning(
                    "retrying nearby search at %.5f,%.5f in %.1fs (%s)",
                    circle.lat,
                    circle.lng,
                    backoff,
                    last_error,
                )
                time.sleep(backoff)

        raise PlacesError(
            f"nearby search failed after {self.max_retries + 1} attempts: {last_error}"
        )


def resolve_types(raw: Optional[str]) -> List[str]:
    """Turn a comma-separated ``--types`` value into a list for the API."""
    if not raw:
        return ["restaurant"]
    types = [t.strip() for t in raw.split(",") if t.strip()]
    if not types:
        return ["restaurant"]
    return types
