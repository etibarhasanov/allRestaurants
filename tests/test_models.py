from allrestaurants.models import COLUMNS, normalize_place

SAMPLE = {
    "id": "ChIJ_sample_place_id",
    "name": "places/ChIJ_sample_place_id",
    "displayName": {"text": "Sahil Restaurant", "languageCode": "en"},
    "formattedAddress": "12 Nizami St, Baku 1000, Azerbaijan",
    "shortFormattedAddress": "12 Nizami St, Baku",
    "addressComponents": [
        {"longText": "12", "shortText": "12", "types": ["street_number"]},
        {"longText": "Nizami Street", "shortText": "Nizami St", "types": ["route"]},
        {"longText": "Baku", "shortText": "Baku", "types": ["locality"]},
        {"longText": "Azerbaijan", "shortText": "AZ", "types": ["country", "political"]},
        {"longText": "1000", "shortText": "1000", "types": ["postal_code"]},
    ],
    "location": {"latitude": 40.3725, "longitude": 49.8353},
    "types": ["restaurant", "food", "point_of_interest"],
    "primaryType": "restaurant",
    "primaryTypeDisplayName": {"text": "Restaurant"},
    "businessStatus": "OPERATIONAL",
    "googleMapsUri": "https://maps.google.com/?cid=123",
    "websiteUri": "https://example.az",
    "nationalPhoneNumber": "012 345 67 89",
    "internationalPhoneNumber": "+994 12 345 67 89",
    "rating": 4.6,
    "userRatingCount": 1284,
    "priceLevel": "PRICE_LEVEL_MODERATE",
    "regularOpeningHours": {
        "openNow": True,
        "weekdayDescriptions": ["Monday: 9:00 AM - 11:00 PM", "Tuesday: Closed"],
    },
    "editorialSummary": {"text": "Local dishes in a courtyard setting."},
    "takeout": True,
    "delivery": False,
    "accessibilityOptions": {"wheelchairAccessibleEntrance": True},
    "plusCode": {"globalCode": "8H8HR2VJ+2R"},
    "utcOffsetMinutes": 240,
}


def test_normalize_maps_every_column():
    row = normalize_place(SAMPLE)
    assert set(row) == set(COLUMNS)


def test_normalize_extracts_the_fields_that_matter():
    row = normalize_place(SAMPLE)
    assert row["place_id"] == "ChIJ_sample_place_id"
    assert row["name"] == "Sahil Restaurant"
    assert row["rating"] == 4.6
    assert row["user_rating_count"] == 1284
    assert row["google_maps_url"] == "https://maps.google.com/?cid=123"
    assert row["website"] == "https://example.az"
    assert row["phone"] == "012 345 67 89"
    assert row["latitude"] == 40.3725
    assert row["business_status"] == "OPERATIONAL"
    assert row["primary_type_label"] == "Restaurant"
    assert row["types"] == "restaurant,food,point_of_interest"
    assert row["editorial_summary"] == "Local dishes in a courtyard setting."


def test_price_level_becomes_a_number_and_a_label():
    row = normalize_place(SAMPLE)
    assert row["price_level"] == 2
    assert row["price_label"] == "$$"


def test_address_components_are_split_out():
    row = normalize_place(SAMPLE)
    assert row["street_number"] == "12"
    assert row["street"] == "Nizami Street"
    assert row["city"] == "Baku"
    assert row["country"] == "Azerbaijan"
    assert row["country_code"] == "AZ"
    assert row["postal_code"] == "1000"


def test_opening_hours_flatten_to_text():
    row = normalize_place(SAMPLE)
    assert row["opening_hours"] == "Monday: 9:00 AM - 11:00 PM\nTuesday: Closed"
    assert row["open_now"] is True


def test_booleans_keep_false_apart_from_missing():
    row = normalize_place(SAMPLE)
    assert row["takeout"] is True
    assert row["delivery"] is False
    assert row["dine_in"] is None
    assert row["wheelchair_accessible_entrance"] is True


def test_sparse_result_does_not_raise():
    """A cheaper field tier returns far less; missing must mean None."""
    row = normalize_place({"id": "abc", "name": "places/abc"})
    assert row["place_id"] == "abc"
    assert row["rating"] is None
    assert row["city"] is None
    assert set(row) == set(COLUMNS)


def test_is_restaurant_rejects_shops_hotels_and_malls():
    """These outrank real restaurants on review count, so they cost results."""
    from allrestaurants.models import is_restaurant

    for t in ("supermarket", "hypermarket", "grocery_store", "shopping_mall",
              "hotel", "market", "gas_station", "sauna", "bowling_alley"):
        assert not is_restaurant(t), t


def test_is_restaurant_keeps_every_kind_of_eating_place():
    from allrestaurants.models import is_restaurant

    for t in ("restaurant", "fast_food_restaurant", "pizza_restaurant", "cafe",
              "bistro", "pub", "brewpub", "gastropub", "meal_takeaway",
              "coffee_shop", "fine_dining_restaurant", "kebab_shop"):
        assert is_restaurant(t), t


def test_unknown_and_missing_types_are_kept():
    """Google adds types constantly; dropping a real restaurant is the worse error."""
    from allrestaurants.models import is_restaurant

    assert is_restaurant(None)
    assert is_restaurant("")
    assert is_restaurant("some_new_type_google_invented")
