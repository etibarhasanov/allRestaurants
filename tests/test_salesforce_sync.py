import pytest

from allrestaurants.salesforce_sync import (
    ACCOUNT_FIELD_MAP,
    RESTAURANT_FIELD_MAP,
    SalesforceConfigError,
    build_record,
    sync,
)
from allrestaurants.store import Store


class FakeBulkObject:
    def __init__(self, results_for):
        self.results_for = results_for
        self.batches = []

    def upsert(self, records, external_id_field):
        self.batches.append((records, external_id_field))
        return self.results_for(records)


class FakeBulk:
    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, name):
        return self._obj


class FakeSalesforce:
    def __init__(self, obj):
        self.bulk = FakeBulk(obj)


def seed(tmp_path, rows):
    store = Store(str(tmp_path / "t.db"))
    for row in rows:
        store.upsert_place(row)
    return store


def test_build_record_maps_and_stamps():
    row = {"place_id": "abc", "name": "Sahil", "rating": 4.6, "takeout": 1,
           "delivery": 0, "website": None, "city": "Baku"}
    record = build_record(row, RESTAURANT_FIELD_MAP)
    assert record["Google_Place_Id__c"] == "abc"
    assert record["Name"] == "Sahil"
    assert record["Rating__c"] == 4.6
    assert record["Takeout__c"] is True
    assert record["Delivery__c"] is False
    assert "Website__c" not in record, "nulls must be omitted, not sent as empty"
    assert record["Last_Synced__c"].endswith("Z")


def test_build_record_truncates_over_long_text():
    row = {"place_id": "abc", "name": "N" * 200}
    assert len(build_record(row, RESTAURANT_FIELD_MAP)["Name"]) == 80


def test_build_record_skips_the_stamp_for_standard_objects():
    row = {"place_id": "abc", "name": "Sahil", "city": "Baku"}
    record = build_record(row, ACCOUNT_FIELD_MAP, stamp_synced=False)
    assert record["BillingCity"] == "Baku"
    assert "Last_Synced__c" not in record


def test_sync_upserts_and_records_ids(tmp_path):
    store = seed(tmp_path, [{"place_id": "a", "name": "A"}, {"place_id": "b", "name": "B"}])
    obj = FakeBulkObject(
        lambda records: [
            {"success": True, "created": True, "id": f"a0{i}", "errors": []}
            for i, _ in enumerate(records)
        ]
    )
    summary = sync(store, FakeSalesforce(obj))

    assert summary == {"total": 2, "success": 2, "created": 2, "failed": 0}
    assert obj.batches[0][1] == "Google_Place_Id__c"
    assert len(list(store.iter_places("salesforce_synced_at IS NOT NULL"))) == 2
    store.close()


def test_sync_counts_failures_without_marking_them_synced(tmp_path):
    store = seed(tmp_path, [{"place_id": "a", "name": "A"}])
    obj = FakeBulkObject(
        lambda records: [{"success": False, "created": False, "id": None,
                          "errors": [{"message": "REQUIRED_FIELD_MISSING"}]}]
    )
    summary = sync(store, FakeSalesforce(obj))
    assert summary["failed"] == 1
    assert len(list(store.iter_places("salesforce_synced_at IS NOT NULL"))) == 0
    store.close()


def test_only_new_skips_records_already_synced(tmp_path):
    store = seed(tmp_path, [{"place_id": "a", "name": "A"}, {"place_id": "b", "name": "B"}])
    store.mark_synced("a", "a01")
    obj = FakeBulkObject(
        lambda records: [{"success": True, "created": False, "id": "b01", "errors": []}]
    )
    summary = sync(store, FakeSalesforce(obj), only_unsynced=True)
    assert summary["total"] == 1
    assert obj.batches[0][0][0]["Google_Place_Id__c"] == "b"
    store.close()


def test_sync_batches_large_sets(tmp_path):
    store = seed(tmp_path, [{"place_id": f"p{i}", "name": f"P{i}"} for i in range(25)])
    obj = FakeBulkObject(
        lambda records: [{"success": True, "created": True, "id": "x", "errors": []}
                         for _ in records]
    )
    sync(store, FakeSalesforce(obj), batch_size=10)
    assert [len(b[0]) for b in obj.batches] == [10, 10, 5]
    store.close()


def test_dry_run_sends_nothing(tmp_path):
    store = seed(tmp_path, [{"place_id": "a", "name": "A"}])
    summary = sync(store, None, dry_run=True)
    assert summary["total"] == 1 and summary["success"] == 0
    assert len(list(store.iter_places("salesforce_synced_at IS NOT NULL"))) == 0
    store.close()


def test_external_id_must_exist_in_the_field_map(tmp_path):
    store = seed(tmp_path, [{"place_id": "a", "name": "A"}])
    with pytest.raises(SalesforceConfigError):
        sync(store, None, external_id_field="Not_A_Field__c", dry_run=True)
    store.close()
