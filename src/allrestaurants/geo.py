"""Geographic helpers: laying search circles over an area and splitting them.

The Google Places "Nearby Search" endpoint returns at most 20 places for a
single query, no matter how many restaurants actually sit inside the circle you
asked about.  To enumerate *every* restaurant in a city you therefore have to
move the search circle around -- the "moving pin" trick -- and, whenever a
circle comes back full, split it into smaller circles and look again.

This module owns the arithmetic for both halves of that: tiling an area with
circles, and subdividing one circle into four that still cover it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

# Mean Earth radius (IUGG), metres.
EARTH_RADIUS_M = 6_371_008.8

# Metres per degree of latitude.  A spherical constant: it is off by roughly
# 0.1% against WGS84 at mid latitudes, which COVERAGE_MARGIN below absorbs.
METRES_PER_DEGREE_LAT = 111_320.0

# Both the grid spacing and the four-way split below are derived from bounds
# that are *exactly* tight -- any error at all, including the ~0.1% from the
# flat-earth constants above, opens hairline gaps between circles.  Shrinking
# the spacing (and growing the children) by this margin buys a safety factor
# twenty times the projection error for about 4% more API calls.
COVERAGE_MARGIN = 0.98


def metres_per_degree_lng(lat: float) -> float:
    """Metres covered by one degree of longitude at ``lat``.

    Shrinks to zero at the poles; clamped so callers never divide by ~0.
    """
    return max(METRES_PER_DEGREE_LAT * math.cos(math.radians(lat)), 1.0)


def offset(lat: float, lng: float, east_m: float, north_m: float) -> Tuple[float, float]:
    """Move a point by a local east/north offset in metres."""
    new_lat = lat + north_m / METRES_PER_DEGREE_LAT
    new_lng = lng + east_m / metres_per_degree_lng(lat)
    return new_lat, new_lng


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Circle:
    """One position of the pin: a circular search area."""

    lat: float
    lng: float
    radius_m: float
    depth: int = 0

    @property
    def key(self) -> str:
        """Stable identity, used to remember which circles were already done."""
        return f"{self.lat:.6f},{self.lng:.6f},{self.radius_m:.1f}"

    def children(self) -> List["Circle"]:
        """Split into four circles whose union covers this one.

        Sub-circles sit at (+/- r/2, +/- r/2) with radius ``r / sqrt(2)``.  The
        farthest any point of the parent disc can be from the nearest child
        centre is exactly ``r / sqrt(2)``, so that radius covers the parent --
        with COVERAGE_MARGIN added because "exactly" leaves no room for the
        projection error.
        """
        half = self.radius_m / 2.0
        child_radius = self.radius_m / math.sqrt(2.0) / COVERAGE_MARGIN
        out = []
        for east in (-half, half):
            for north in (-half, half):
                lat, lng = offset(self.lat, self.lng, east, north)
                out.append(Circle(lat, lng, child_radius, self.depth + 1))
        return out


def cover_bbox(
    south: float,
    west: float,
    north: float,
    east: float,
    cell_radius_m: float,
) -> List[Circle]:
    """Tile a lat/lng bounding box with overlapping circles.

    Circles of radius ``r`` on a square grid of spacing ``s`` cover the plane
    exactly when ``s <= r * sqrt(2)``, so that bound, less COVERAGE_MARGIN, is
    the spacing used.
    """
    if north < south or east < west:
        raise ValueError("bbox must be given as south < north and west < east")
    if cell_radius_m <= 0:
        raise ValueError("cell_radius_m must be positive")

    spacing_m = cell_radius_m * math.sqrt(2.0) * COVERAGE_MARGIN
    lat_step = spacing_m / METRES_PER_DEGREE_LAT

    circles: List[Circle] = []
    rows = max(1, math.ceil((north - south) / lat_step))
    for i in range(rows + 1):
        lat = south + i * lat_step
        if lat > north + lat_step:
            break
        lng_step = spacing_m / metres_per_degree_lng(lat)
        cols = max(1, math.ceil((east - west) / lng_step))
        for j in range(cols + 1):
            lng = west + j * lng_step
            if lng > east + lng_step:
                break
            circles.append(Circle(lat, lng, cell_radius_m))
    return circles


def cover_radius(
    center_lat: float,
    center_lng: float,
    radius_m: float,
    cell_radius_m: float,
) -> List[Circle]:
    """Tile a circular area of interest with smaller search circles."""
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    cell_radius_m = min(cell_radius_m, radius_m)

    lat_pad = radius_m / METRES_PER_DEGREE_LAT
    lng_pad = radius_m / metres_per_degree_lng(center_lat)
    grid = cover_bbox(
        center_lat - lat_pad,
        center_lng - lng_pad,
        center_lat + lat_pad,
        center_lng + lng_pad,
        cell_radius_m,
    )
    # Drop circles that cannot touch the area of interest at all.
    return [
        c
        for c in grid
        if haversine_m(center_lat, center_lng, c.lat, c.lng) <= radius_m + c.radius_m
    ]


def parse_latlng(text: str) -> Tuple[float, float]:
    """Parse ``"40.4093,49.8671"`` into a (lat, lng) pair."""
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    if len(parts) != 2:
        raise ValueError(f"expected 'lat,lng', got {text!r}")
    lat, lng = float(parts[0]), float(parts[1])
    if not -90 <= lat <= 90:
        raise ValueError(f"latitude out of range: {lat}")
    if not -180 <= lng <= 180:
        raise ValueError(f"longitude out of range: {lng}")
    return lat, lng


def parse_bbox(text: str) -> Tuple[float, float, float, float]:
    """Parse ``"south,west,north,east"`` into a 4-tuple."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"expected 'south,west,north,east', got {text!r}")
    south, west, north, east = (float(p) for p in parts)
    if south >= north:
        raise ValueError("south must be less than north")
    if west >= east:
        raise ValueError("west must be less than east")
    return south, west, north, east


def cell_radius_for_budget(area_m2: float, budget_calls: int) -> float:
    """Pick a starting circle radius whose grid roughly fits a call budget.

    Circles on a grid of spacing ``s`` each stand in for ``s**2`` of ground, so
    fitting ``n`` of them over an area means ``s = sqrt(area / n)``.  Inverting
    the spacing rule from :func:`cover_bbox` gives the radius.

    Only about 70% of the budget goes to the starting grid; the rest is left for
    splitting the circles that turn out to be dense.
    """
    if budget_calls < 1:
        raise ValueError("budget_calls must be at least 1")
    grid_calls = max(1.0, budget_calls * 0.7)
    spacing = math.sqrt(area_m2 / grid_calls)
    return max(25.0, spacing / (math.sqrt(2.0) * COVERAGE_MARGIN))
