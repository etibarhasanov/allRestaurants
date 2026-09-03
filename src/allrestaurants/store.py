"""SQLite storage for swept places, plus CSV/JSON export.

The database doubles as the sweep's resume log: every search circle that
finished is recorded, so re-running the same command picks up where it left off
instead of paying Google for the same calls twice.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from .models import COLUMNS

# Columns we manage on top of the place fields themselves.
_META_COLUMNS = [
    ("raw_json", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("salesforce_id", "TEXT"),
    ("salesforce_synced_at", "TEXT"),
]

_TEXT_COLUMNS = {
    "latitude": "REAL",
    "longitude": "REAL",
    "rating": "REAL",
    "user_rating_count": "INTEGER",
    "price_level": "INTEGER",
    "utc_offset_minutes": "INTEGER",
}


def _sql_type(column: str) -> str:
    if column in _TEXT_COLUMNS:
        return _TEXT_COLUMNS[column]
    if column in {
        "open_now",
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
    }:
        return "INTEGER"
    return "TEXT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe-enough SQLite wrapper (one connection, one lock)."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        columns = ["place_id TEXT PRIMARY KEY"]
        columns += [
            f"{c} {_sql_type(c)}" for c in COLUMNS if c != "place_id"
        ]
        columns += [f"{name} {kind}" for name, kind in _META_COLUMNS]
        with self._lock:
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS restaurants ({', '.join(columns)})"
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cells (
                    cell_key     TEXT PRIMARY KEY,
                    latitude     REAL NOT NULL,
                    longitude    REAL NOT NULL,
                    radius_m     REAL NOT NULL,
                    depth        INTEGER NOT NULL,
                    result_count INTEGER,
                    saturated    INTEGER,
                    split        INTEGER DEFAULT 0,
                    searched_at  TEXT
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_restaurants_city ON restaurants(city)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_restaurants_rating ON restaurants(rating)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_restaurants_synced "
                "ON restaurants(salesforce_synced_at)"
            )
            self.conn.commit()

    # -- places ------------------------------------------------------------

    def upsert_place(self, row: Dict[str, Any], raw: Optional[dict] = None) -> bool:
        """Insert or refresh one place.  Returns True if it was new."""
        place_id = row.get("place_id")
        if not place_id:
            return False

        payload = {c: row.get(c) for c in COLUMNS}
        for key, value in list(payload.items()):
            if isinstance(value, bool):
                payload[key] = int(value)
        payload["raw_json"] = json.dumps(raw, ensure_ascii=False) if raw else None

        now = _now()
        with self._lock:
            cur = self.conn.execute(
                "SELECT place_id FROM restaurants WHERE place_id = ?", (place_id,)
            )
            is_new = cur.fetchone() is None

            payload["last_seen_at"] = now
            if is_new:
                payload["first_seen_at"] = now
                names = list(payload.keys())
                self.conn.execute(
                    f"INSERT INTO restaurants ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    [payload[n] for n in names],
                )
            else:
                # Never overwrite a known value with NULL: a cheaper field tier
                # or a partial re-sweep should not erase data already collected.
                names = [n for n in payload if n != "place_id"]
                assignments = ", ".join(f"{n} = COALESCE(?, {n})" for n in names)
                self.conn.execute(
                    f"UPDATE restaurants SET {assignments}, last_seen_at = ? "
                    "WHERE place_id = ?",
                    [payload[n] for n in names] + [now, place_id],
                )
            self.conn.commit()
        return is_new

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]

    def iter_places(
        self, where: str = "", params: Optional[list] = None
    ) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM restaurants"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY name COLLATE NOCASE"
        with self._lock:
            rows = self.conn.execute(sql, params or []).fetchall()
        return iter(rows)

    def mark_synced(self, place_id: str, salesforce_id: Optional[str]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE restaurants SET salesforce_id = COALESCE(?, salesforce_id), "
                "salesforce_synced_at = ? WHERE place_id = ?",
                (salesforce_id, _now(), place_id),
            )
            self.conn.commit()

    # -- resume log --------------------------------------------------------

    def cell_done(self, cell_key: str) -> bool:
        return self.get_cell(cell_key) is not None

    def get_cell(self, cell_key: str):
        """Return the log row for a searched circle, or None if never searched.

        Resuming needs more than a yes/no: a circle that was split has children
        that may never have been searched, and those have to be queued again or
        the sweep resumes with a truncated frontier and calls itself finished.
        """
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM search_cells WHERE cell_key = ?", (cell_key,)
            ).fetchone()

    def record_cell(
        self, cell, result_count: int, saturated: bool, split: bool
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO search_cells "
                "(cell_key, latitude, longitude, radius_m, depth, result_count, "
                " saturated, split, searched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cell.key,
                    cell.lat,
                    cell.lng,
                    cell.radius_m,
                    cell.depth,
                    result_count,
                    int(saturated),
                    int(split),
                    _now(),
                ),
            )
            self.conn.commit()

    def cell_stats(self) -> Dict[str, int]:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(saturated), 0) AS saturated, "
                "COALESCE(MAX(depth), 0) AS max_depth "
                "FROM search_cells"
            ).fetchone()
        return {
            "cells": row["total"],
            "saturated": row["saturated"],
            "max_depth": row["max_depth"],
        }

    def reset_cells(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM search_cells")
            self.conn.commit()

    # -- maintenance -------------------------------------------------------

    def prune_stale_content(self, older_than_days: int = 30) -> int:
        """Clear cached Google content older than ``older_than_days``.

        Google's Places terms allow caching place IDs indefinitely but not the
        rest of the content, so this blanks every other column while keeping the
        ID (and any Salesforce link) intact.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat(timespec="seconds")
        clearable = [c for c in COLUMNS if c != "place_id"] + ["raw_json"]
        assignments = ", ".join(f"{c} = NULL" for c in clearable)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE restaurants SET {assignments} "
                "WHERE last_seen_at IS NOT NULL AND last_seen_at < ?",
                (cutoff,),
            )
            self.conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# -- export ----------------------------------------------------------------


def export_csv(store: Store, path: str, where: str = "") -> int:
    rows = list(store.iter_places(where))
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in COLUMNS})
    return len(rows)


def export_json(store: Store, path: str, where: str = "") -> int:
    rows = [{c: row[c] for c in COLUMNS} for row in store.iter_places(where)]
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    return len(rows)
