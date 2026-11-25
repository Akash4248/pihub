from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ConcurrencyException(Exception):
    """Raised when an aggregate version collision occurs during event append."""
    pass


class PackEventStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def initialize(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pack_events (
                    event_id TEXT PRIMARY KEY,
                    aggregate_id TEXT NOT NULL,
                    aggregate_version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    actor_type TEXT,
                    classroom_id TEXT,
                    device_id TEXT,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(aggregate_id, aggregate_version)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pack_events_aggregate 
                ON pack_events(aggregate_id, aggregate_version);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pack_events_type 
                ON pack_events(event_type);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pack_events_classroom
                ON pack_events(classroom_id);
            """)

    def get_latest_version(self, aggregate_id: str) -> int:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(aggregate_version) AS max_v FROM pack_events WHERE aggregate_id = ?",
                (aggregate_id,),
            ).fetchone()
            if row and row["max_v"] is not None:
                return int(row["max_v"])
            return 0

    def append_event(
        self,
        aggregate_id: str,
        expected_version: int,
        event_type: str,
        payload: dict[str, Any],
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        classroom_id: Optional[str] = None,
        device_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Append an event to the aggregate's event stream atomically.
        Guarantees:
        1. Idempotency: If idempotency_key was already used, returns the previous event.
        2. Optimistic Concurrency: Enforces expected_version == current_version + 1.
        3. Append-only: History is never updated or deleted.
        """
        if not occurred_at:
            occurred_at = datetime.now(timezone.utc).isoformat()

        # Check existing idempotency key before opening write lock
        if idempotency_key:
            with self._get_connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM pack_events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    data = dict(existing)
                    data["payload"] = json.loads(data["payload"])
                    return data

        event_id = str(uuid.uuid4())
        event_record = {
            "event_id": event_id,
            "aggregate_id": aggregate_id,
            "aggregate_version": expected_version,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "classroom_id": classroom_id,
            "device_id": device_id,
            "payload": json.dumps(payload),
            "idempotency_key": idempotency_key,
            "occurred_at": occurred_at,
        }

        with self._get_connection() as conn:
            try:
                # Enforce version check inside transaction
                row = conn.execute(
                    "SELECT MAX(aggregate_version) AS max_v FROM pack_events WHERE aggregate_id = ?",
                    (aggregate_id,),
                ).fetchone()
                current_v = int(row["max_v"]) if row and row["max_v"] is not None else 0

                if expected_version != current_v + 1:
                    raise ConcurrencyException(
                        f"Concurrency error for pack '{aggregate_id}': expected version {expected_version}, but current version is {current_v}."
                    )

                conn.execute("""
                    INSERT INTO pack_events (
                        event_id, aggregate_id, aggregate_version, event_type,
                        actor_id, actor_type, classroom_id, device_id,
                        payload, idempotency_key, occurred_at
                    ) VALUES (
                        :event_id, :aggregate_id, :aggregate_version, :event_type,
                        :actor_id, :actor_type, :classroom_id, :device_id,
                        :payload, :idempotency_key, :occurred_at
                    )
                """, event_record)
            except sqlite3.IntegrityError as exc:
                if idempotency_key and "idempotency_key" in str(exc):
                    existing = conn.execute(
                        "SELECT * FROM pack_events WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing:
                        data = dict(existing)
                        data["payload"] = json.loads(data["payload"])
                        return data
                raise ConcurrencyException(f"Aggregate version constraint violation: {exc}") from exc

        return_data = dict(event_record)
        return_data["payload"] = payload
        return return_data

    def get_events(self, aggregate_id: str, from_version: int = 1) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pack_events 
                WHERE aggregate_id = ? AND aggregate_version >= ?
                ORDER BY aggregate_version ASC
                """,
                (aggregate_id, from_version),
            ).fetchall()
            events = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                events.append(item)
            return events

    def get_all_events(self) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pack_events ORDER BY aggregate_id, aggregate_version ASC"
            ).fetchall()
            events = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                events.append(item)
            return events
