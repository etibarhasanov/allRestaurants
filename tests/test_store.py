import csv
import json

from allrestaurants.geo import Circle
from allrestaurants.store import Store, export_csv, export_json


def make_row(place_id, **overrides):
    row = {"place_id": place_id, "name": f"Place {place_id}", "rating": 4.2,
           "city": "Baku", "latitude": 40.0, "longitude": 49.0}
    row.update(overrides)
    return row


def test_upsert_reports_new_then_existing(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    assert store.upsert_place(make_row("a")) is True
    assert store.upsert_place(make_row("a")) is False
    assert store.count() == 1
    store.close()


def test_update_never_blanks_a_known_value(tmp_path):
    """A later, cheaper sweep must not wipe fields it did not ask for."""
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a", website="https://example.com", rating=4.8))
    store.upsert_place({"place_id": "a", "name": "Place a", "city": "Baku"})
    row = next(store.iter_places())
    assert row["website"] == "https://example.com"
    assert row["rating"] == 4.8
    store.close()


def test_update_overwrites_with_a_fresher_value(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a", rating=4.2))
    store.upsert_place(make_row("a", rating=3.9))
    assert next(store.iter_places())["rating"] == 3.9
    store.close()


def test_booleans_round_trip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a", takeout=True, delivery=False))
    row = next(store.iter_places())
    assert row["takeout"] == 1
    assert row["delivery"] == 0
    store.close()


def test_row_without_place_id_is_rejected(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    assert store.upsert_place({"name": "no id"}) is False
    assert store.count() == 0
    store.close()


def test_cell_log_drives_resume(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    circle = Circle(40.0, 49.0, 500.0)
    assert store.cell_done(circle.key) is False
    store.record_cell(circle, result_count=20, saturated=True, split=True)
    assert store.cell_done(circle.key) is True

    stats = store.cell_stats()
    assert stats["cells"] == 1 and stats["saturated"] == 1

    store.reset_cells()
    assert store.cell_done(circle.key) is False
    store.close()


def test_store_reopens_existing_database(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    store.upsert_place(make_row("a"))
    store.close()

    reopened = Store(path)
    assert reopened.count() == 1
    reopened.close()


def test_mark_synced(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a"))
    assert len(list(store.iter_places("salesforce_synced_at IS NOT NULL"))) == 0
    store.mark_synced("a", "001xx000000001")
    row = next(store.iter_places())
    assert row["salesforce_id"] == "001xx000000001"
    assert row["salesforce_synced_at"]
    store.close()


def test_prune_clears_content_but_keeps_ids(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a"))
    store.mark_synced("a", "001xx000000001")
    # Backdate the record so it counts as stale.
    store.conn.execute("UPDATE restaurants SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    store.conn.commit()

    assert store.prune_stale_content(older_than_days=30) == 1
    row = next(store.iter_places())
    assert row["place_id"] == "a"
    assert row["name"] is None
    assert row["rating"] is None
    assert row["salesforce_id"] == "001xx000000001"
    store.close()


def test_exports(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_place(make_row("a", rating=4.9))
    store.upsert_place(make_row("b", rating=3.1))

    csv_path = str(tmp_path / "out.csv")
    assert export_csv(store, csv_path) == 2
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["place_id"] for r in rows} == {"a", "b"}

    json_path = str(tmp_path / "out.json")
    assert export_json(store, json_path, "rating >= 4.5") == 1
    with open(json_path, encoding="utf-8") as fh:
        assert json.load(fh)[0]["place_id"] == "a"
    store.close()
