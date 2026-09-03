"""Push collected restaurants into Salesforce via the Bulk API."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# db column -> Salesforce field, for the custom object shipped in
# salesforce/metadata/.  Deploy that first, or point --object at your own
# object and adjust the map below.
RESTAURANT_FIELD_MAP: Dict[str, str] = {
    "place_id": "Google_Place_Id__c",
    "name": "Name",
    "google_maps_url": "Google_Maps_URL__c",
    "website": "Website__c",
    "phone": "Phone__c",
    "phone_international": "Phone_International__c",
    "formatted_address": "Formatted_Address__c",
    "street": "Street__c",
    "city": "City__c",
    "region": "Region__c",
    "postal_code": "Postal_Code__c",
    "country": "Country__c",
    "latitude": "Latitude__c",
    "longitude": "Longitude__c",
    "rating": "Rating__c",
    "user_rating_count": "User_Rating_Count__c",
    "price_level": "Price_Level__c",
    "price_label": "Price_Label__c",
    "business_status": "Business_Status__c",
    "primary_type": "Primary_Type__c",
    "primary_type_label": "Cuisine_Type__c",
    "types": "Google_Types__c",
    "opening_hours": "Opening_Hours__c",
    "editorial_summary": "Description__c",
    "takeout": "Takeout__c",
    "delivery": "Delivery__c",
    "dine_in": "Dine_In__c",
    "reservable": "Reservable__c",
    "serves_beer": "Serves_Beer__c",
    "serves_wine": "Serves_Wine__c",
    "serves_vegetarian_food": "Serves_Vegetarian__c",
    "outdoor_seating": "Outdoor_Seating__c",
    "good_for_children": "Good_For_Children__c",
    "wheelchair_accessible_entrance": "Wheelchair_Accessible__c",
}

# Alternative: treat each restaurant as a standard Account.  Needs one custom
# External Id text field (Google_Place_Id__c) added to Account.
ACCOUNT_FIELD_MAP: Dict[str, str] = {
    "place_id": "Google_Place_Id__c",
    "name": "Name",
    "website": "Website",
    "phone": "Phone",
    "street": "BillingStreet",
    "city": "BillingCity",
    "region": "BillingState",
    "postal_code": "BillingPostalCode",
    "country": "BillingCountry",
    "editorial_summary": "Description",
}

FIELD_MAPS: Dict[str, Dict[str, str]] = {
    "Restaurant__c": RESTAURANT_FIELD_MAP,
    "Account": ACCOUNT_FIELD_MAP,
}

# Salesforce text fields reject over-long values outright; trim rather than
# lose the whole record to a STRING_TOO_LONG on one field.
FIELD_MAX_LENGTH: Dict[str, int] = {
    "Name": 80,
    "Formatted_Address__c": 255,
    "Street__c": 255,
    "BillingStreet": 255,
    "Google_Types__c": 1000,
    "Opening_Hours__c": 2000,
    "Description__c": 4000,
    "Description": 32000,
}

_BOOLEAN_COLUMNS = {
    "takeout",
    "delivery",
    "dine_in",
    "reservable",
    "serves_beer",
    "serves_wine",
    "serves_vegetarian_food",
    "outdoor_seating",
    "good_for_children",
    "wheelchair_accessible_entrance",
}


class SalesforceConfigError(RuntimeError):
    """Salesforce credentials are missing or inconsistent."""


def _fetch_client_credentials_token(
    client_id: str, client_secret: str, instance_url: str
) -> Tuple[str, str]:
    """OAuth 2.0 client credentials flow against a Connected App."""
    token_url = instance_url.rstrip("/") + "/services/oauth2/token"
    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SalesforceConfigError(
            f"client credentials flow failed (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    payload = resp.json()
    return payload["access_token"], payload.get("instance_url", instance_url)


def connect(env: Dict[str, str]):
    """Build a ``simple_salesforce.Salesforce`` session from env values."""
    try:
        from simple_salesforce import Salesforce
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SalesforceConfigError(
            "simple-salesforce is not installed. Run: pip install simple-salesforce"
        ) from exc

    api_version = env.get("SF_API_VERSION") or "60.0"
    access_token = env.get("SF_ACCESS_TOKEN")
    instance_url = env.get("SF_INSTANCE_URL")
    client_id = env.get("SF_CLIENT_ID")
    client_secret = env.get("SF_CLIENT_SECRET")
    username = env.get("SF_USERNAME")

    if client_id and client_secret and instance_url and not access_token:
        access_token, instance_url = _fetch_client_credentials_token(
            client_id, client_secret, instance_url
        )

    if access_token and instance_url:
        log.info("connecting to Salesforce at %s with an OAuth token", instance_url)
        return Salesforce(
            instance_url=instance_url, session_id=access_token, version=api_version
        )

    if username and env.get("SF_PASSWORD"):
        log.info("connecting to Salesforce as %s", username)
        return Salesforce(
            username=username,
            password=env["SF_PASSWORD"],
            security_token=env.get("SF_SECURITY_TOKEN", ""),
            domain=env.get("SF_DOMAIN", "login"),
            version=api_version,
        )

    raise SalesforceConfigError(
        "No usable Salesforce credentials. Set either SF_USERNAME/SF_PASSWORD"
        " (+SF_SECURITY_TOKEN), SF_CLIENT_ID/SF_CLIENT_SECRET/SF_INSTANCE_URL,"
        " or SF_ACCESS_TOKEN/SF_INSTANCE_URL. See .env.example."
    )


def build_record(
    row: Any, field_map: Dict[str, str], stamp_synced: bool = True
) -> Dict[str, Any]:
    """Turn one stored row into a Salesforce record dict."""
    record: Dict[str, Any] = {}
    for column, sf_field in field_map.items():
        try:
            value = row[column]
        except (KeyError, IndexError):
            continue
        if value is None:
            continue
        if column in _BOOLEAN_COLUMNS:
            value = bool(value)
        elif isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            limit = FIELD_MAX_LENGTH.get(sf_field)
            if limit and len(value) > limit:
                value = value[:limit]
        record[sf_field] = value

    if stamp_synced and "Last_Synced__c" not in record:
        record["Last_Synced__c"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return record


def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def sync(
    store,
    sf,
    object_name: str = "Restaurant__c",
    external_id_field: str = "Google_Place_Id__c",
    field_map: Optional[Dict[str, str]] = None,
    batch_size: int = 5000,
    only_unsynced: bool = False,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Upsert stored restaurants into Salesforce, keyed on the external id."""
    field_map = field_map or FIELD_MAPS.get(object_name, RESTAURANT_FIELD_MAP)
    if external_id_field not in field_map.values():
        raise SalesforceConfigError(
            f"external id field {external_id_field!r} is not in the field map; "
            "add it before syncing or the upsert cannot match records"
        )
    # Restaurant__c custom fields do not exist on Account and vice versa.
    stamp_synced = object_name.endswith("__c")

    where = "salesforce_synced_at IS NULL" if only_unsynced else ""
    rows = list(store.iter_places(where))
    if limit:
        rows = rows[:limit]

    records: List[Dict[str, Any]] = []
    place_ids: List[str] = []
    for row in rows:
        record = build_record(row, field_map, stamp_synced=stamp_synced)
        if not record.get(external_id_field):
            continue
        records.append(record)
        place_ids.append(row["place_id"])

    summary = {"total": len(records), "success": 0, "created": 0, "failed": 0}
    if not records:
        return summary

    if dry_run:
        log.info("dry run: would upsert %d record(s) into %s", len(records), object_name)
        log.info("sample record: %s", records[0])
        return summary

    bulk_object = getattr(sf.bulk, object_name)
    for batch_records, batch_ids in zip(
        _chunks(records, batch_size), _chunks(place_ids, batch_size)
    ):
        results = bulk_object.upsert(batch_records, external_id_field)
        for result, place_id in zip(results, batch_ids):
            if result.get("success"):
                summary["success"] += 1
                if result.get("created"):
                    summary["created"] += 1
                store.mark_synced(place_id, result.get("id"))
            else:
                summary["failed"] += 1
                log.error("upsert failed for %s: %s", place_id, result.get("errors"))
        log.info(
            "batch done: %d ok / %d failed so far",
            summary["success"],
            summary["failed"],
        )
    return summary
