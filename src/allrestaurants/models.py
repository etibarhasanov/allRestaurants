"""Flatten a Places API result into the row shape we store and sync."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Google returns price as an enum string; keep both a number (easy to sort and
# filter on) and the readable label (what a salesperson wants to see).
PRICE_LEVELS: Dict[str, int] = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}
PRICE_LABELS: Dict[str, str] = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}

# Google returns a place whenever "restaurant" appears anywhere in its type
# list, which sweeps in every supermarket, mall and hotel that happens to have
# a food court. They are not restaurants, and because they carry huge review
# counts they outrank real ones in a popularity-ranked search -- so they cost
# results, not just tidiness. Filtered on primary type, which is Google's own
# answer to "what is this place, mainly?".
NON_RESTAURANT_PRIMARY_TYPES = frozenset({
    "airport", "amusement_park", "bank", "bowling_alley", "bus_station",
    "casino", "convenience_store", "cultural_center", "department_store",
    "discount_store", "drugstore", "event_venue", "extended_stay_hotel",
    "ferry_terminal", "gas_station", "grocery_store", "gym", "home_goods_store",
    "hospital", "hostel", "hotel", "hypermarket", "inn", "liquor_store",
    "lodging", "market", "motel", "movie_theater", "museum", "night_club",
    "park", "parking", "pharmacy", "resort_hotel", "sauna", "school",
    "shopping_mall", "spa", "sporting_goods_store", "stadium", "store",
    "supermarket", "tourist_attraction", "train_station", "transit_station",
    "university", "warehouse_store", "wholesaler", "zoo",
})


def is_restaurant(primary_type: Optional[str]) -> bool:
    """Whether a place's primary type is a place you go to eat.

    Unknown types count as restaurants: Google adds new ones regularly, and
    wrongly dropping a real restaurant is worse than keeping a stray shop.
    """
    return (primary_type or "") not in NON_RESTAURANT_PRIMARY_TYPES


# addressComponent type -> our column name.
_ADDRESS_PARTS = {
    "street_number": "street_number",
    "route": "street",
    "locality": "city",
    "postal_town": "city",
    "administrative_area_level_1": "region",
    "administrative_area_level_2": "district",
    "country": "country",
    "postal_code": "postal_code",
}

# Every column of the ``restaurants`` table, in export order.
COLUMNS: List[str] = [
    "place_id",
    "name",
    "formatted_address",
    "short_address",
    "street_number",
    "street",
    "district",
    "city",
    "region",
    "postal_code",
    "country",
    "country_code",
    "latitude",
    "longitude",
    "google_maps_url",
    "website",
    "phone",
    "phone_international",
    "rating",
    "user_rating_count",
    "price_level",
    "price_label",
    "business_status",
    "primary_type",
    "primary_type_label",
    "types",
    "opening_hours",
    "open_now",
    "editorial_summary",
    "takeout",
    "delivery",
    "dine_in",
    "reservable",
    "serves_breakfast",
    "serves_lunch",
    "serves_dinner",
    "serves_beer",
    "serves_wine",
    "serves_vegetarian_food",
    "outdoor_seating",
    "good_for_children",
    "wheelchair_accessible_entrance",
    "utc_offset_minutes",
    "plus_code",
]

_BOOL_FIELDS = [
    ("takeout", "takeout"),
    ("delivery", "delivery"),
    ("dineIn", "dine_in"),
    ("reservable", "reservable"),
    ("servesBreakfast", "serves_breakfast"),
    ("servesLunch", "serves_lunch"),
    ("servesDinner", "serves_dinner"),
    ("servesBeer", "serves_beer"),
    ("servesWine", "serves_wine"),
    ("servesVegetarianFood", "serves_vegetarian_food"),
    ("outdoorSeating", "outdoor_seating"),
    ("goodForChildren", "good_for_children"),
]


def _text(value: Any) -> Optional[str]:
    """Unwrap Google's ``{"text": ..., "languageCode": ...}`` shape."""
    if isinstance(value, dict):
        return value.get("text")
    if isinstance(value, str):
        return value
    return None


def _address_components(raw: dict) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for component in raw.get("addressComponents") or []:
        long_text = component.get("longText")
        short_text = component.get("shortText")
        for kind in component.get("types") or []:
            column = _ADDRESS_PARTS.get(kind)
            if column and not out.get(column):
                out[column] = long_text
            if kind == "country" and not out.get("country_code"):
                out["country_code"] = short_text
    return out


def _opening_hours(raw: dict) -> Dict[str, Any]:
    hours = raw.get("regularOpeningHours") or {}
    descriptions = hours.get("weekdayDescriptions") or []
    return {
        "opening_hours": "\n".join(descriptions) if descriptions else None,
        "open_now": hours.get("openNow"),
    }


def normalize_place(raw: dict) -> Dict[str, Any]:
    """Map one Places API result onto :data:`COLUMNS`.

    Missing fields become ``None`` rather than raising, because which fields
    come back depends on the field-mask tier the sweep ran with.
    """
    location = raw.get("location") or {}
    price = raw.get("priceLevel")

    row: Dict[str, Any] = {column: None for column in COLUMNS}
    row.update(
        {
            "place_id": raw.get("id"),
            "name": _text(raw.get("displayName")),
            "formatted_address": raw.get("formattedAddress"),
            "short_address": raw.get("shortFormattedAddress"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "google_maps_url": raw.get("googleMapsUri"),
            "website": raw.get("websiteUri"),
            "phone": raw.get("nationalPhoneNumber"),
            "phone_international": raw.get("internationalPhoneNumber"),
            "rating": raw.get("rating"),
            "user_rating_count": raw.get("userRatingCount"),
            "price_level": PRICE_LEVELS.get(price) if price else None,
            "price_label": PRICE_LABELS.get(price) if price else None,
            "business_status": raw.get("businessStatus"),
            "primary_type": raw.get("primaryType"),
            "primary_type_label": _text(raw.get("primaryTypeDisplayName")),
            "types": ",".join(raw.get("types") or []) or None,
            "editorial_summary": _text(raw.get("editorialSummary")),
            "utc_offset_minutes": raw.get("utcOffsetMinutes"),
            "plus_code": (raw.get("plusCode") or {}).get("globalCode"),
        }
    )
    row.update(_address_components(raw))
    row.update(_opening_hours(raw))
    for api_field, column in _BOOL_FIELDS:
        row[column] = raw.get(api_field)

    accessibility = raw.get("accessibilityOptions") or {}
    row["wheelchair_accessible_entrance"] = accessibility.get(
        "wheelchairAccessibleEntrance"
    )

    return row


def row_to_json(row: Dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)
