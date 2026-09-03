import pytest

from allrestaurants.geo import Circle
from allrestaurants.places import (
    MAX_RESULTS_PER_CALL,
    BudgetExhausted,
    PlacesClient,
    PlacesError,
    field_mask,
    resolve_types,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def test_field_mask_is_cumulative():
    ids = field_mask("ids")
    ratings = field_mask("ratings")
    assert "places.id" in ids
    assert "places.rating" not in ids
    assert "places.rating" in ratings
    assert "places.displayName" in ratings
    assert "places.editorialSummary" not in ratings
    assert "places.editorialSummary" in field_mask("full")


def test_field_mask_rejects_an_unknown_tier():
    with pytest.raises(ValueError):
        field_mask("everything")


def test_client_requires_an_api_key():
    with pytest.raises(PlacesError):
        PlacesClient(api_key="")


def test_search_builds_the_expected_request():
    session = FakeSession([FakeResponse(200, {"places": [{"id": "a"}]})])
    client = PlacesClient("key", tier="ratings", qps=0, session=session)
    places = client.search_nearby(Circle(40.0, 49.0, 500.0), included_types=["cafe"])

    assert places == [{"id": "a"}]
    sent = session.requests[0]
    assert sent["headers"]["X-Goog-Api-Key"] == "key"
    assert "places.rating" in sent["headers"]["X-Goog-FieldMask"]
    assert sent["json"]["includedTypes"] == ["cafe"]
    assert sent["json"]["maxResultCount"] == MAX_RESULTS_PER_CALL
    circle = sent["json"]["locationRestriction"]["circle"]
    assert circle["center"] == {"latitude": 40.0, "longitude": 49.0}
    assert circle["radius"] == 500.0


def test_radius_is_clamped_to_the_api_maximum():
    session = FakeSession([FakeResponse(200, {"places": []})])
    client = PlacesClient("key", qps=0, session=session)
    client.search_nearby(Circle(40.0, 49.0, 200_000.0))
    assert session.requests[0]["json"]["locationRestriction"]["circle"]["radius"] == 50_000.0


def test_missing_places_key_means_no_results():
    session = FakeSession([FakeResponse(200, {})])
    client = PlacesClient("key", qps=0, session=session)
    assert client.search_nearby(Circle(40.0, 49.0, 500.0)) == []


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("allrestaurants.places.time.sleep", lambda _: None)
    session = FakeSession(
        [FakeResponse(429, text="slow down"), FakeResponse(200, {"places": [{"id": "a"}]})]
    )
    client = PlacesClient("key", qps=0, session=session)
    assert client.search_nearby(Circle(40.0, 49.0, 500.0)) == [{"id": "a"}]
    assert client.request_count == 2


def test_client_error_is_not_retried():
    session = FakeSession([FakeResponse(403, text="API key not authorized")])
    client = PlacesClient("key", qps=0, session=session)
    with pytest.raises(PlacesError, match="403"):
        client.search_nearby(Circle(40.0, 49.0, 500.0))
    assert len(session.requests) == 1


def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("allrestaurants.places.time.sleep", lambda _: None)
    session = FakeSession([FakeResponse(503, text="unavailable") for _ in range(4)])
    client = PlacesClient("key", qps=0, max_retries=3, session=session)
    with pytest.raises(PlacesError, match="after 4 attempts"):
        client.search_nearby(Circle(40.0, 49.0, 500.0))


def test_budget_is_enforced_before_the_call_goes_out():
    session = FakeSession([FakeResponse(200, {"places": []})])
    client = PlacesClient("key", qps=0, max_requests=1, session=session)
    client.search_nearby(Circle(40.0, 49.0, 500.0))
    with pytest.raises(BudgetExhausted):
        client.search_nearby(Circle(40.0, 49.0, 500.0))
    assert len(session.requests) == 1


def test_resolve_types():
    assert resolve_types(None) == ["restaurant"]
    assert resolve_types("  ") == ["restaurant"]
    assert resolve_types("restaurant, cafe ,bar") == ["restaurant", "cafe", "bar"]


def test_rank_defaults_to_distance():
    """POPULARITY would make an exhaustive sweep impossible - see search_nearby."""
    session = FakeSession([FakeResponse(200, {"places": []})])
    client = PlacesClient("key", qps=0, session=session)
    client.search_nearby(Circle(40.0, 49.0, 500.0))
    assert session.requests[0]["json"]["rankPreference"] == "DISTANCE"
