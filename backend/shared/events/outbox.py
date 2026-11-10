from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.events.bus import EventBus, get_event_bus
from shared.events.schemas import Event

logger = logging.getLogger(__name__)


class TransactionalOutbox:
    """Implements the Transactional Outbox Pattern.

    Ensures that business domain updates and event publishing are atomic:
    Events are recorded in an `outbox_events` table within the caller's SQLite
    transaction and published asynchronously to the Event Bus with guaranteed delivery.
    """

    SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS outbox_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE NOT NULL,
        event_type TEXT NOT NULL,
        producer TEXT NOT NULL,
        payload TEXT NOT NULL,
        correlation_id TEXT,
        version TEXT NOT NULL DEFAULT '1.0',
        status TEXT NOT NULL DEFAULT 'PENDING',
        retry_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        published_at TEXT,
        last_error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events(status, created_at);
    """

    @classmethod
    def init_schema(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(cls.SCHEMA_DDL)

    @classmethod
    def stage_event(
        cls,
        conn: sqlite3.Connection,
        event: Event,
    ) -> None:
        """Stage an event in the same transaction as business data updates."""
        cls.init_schema(conn)
        conn.execute(
            """
            INSERT INTO outbox_events (
                event_id, event_type, producer, payload, correlation_id, version,
                status, retry_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.producer,
                json.dumps(event.payload),
                event.correlation_id,
                event.version,
                event.timestamp,
            ),
        )

    @classmethod
    async def publish_pending(
        cls,
        db_path: str | Path,
        bus: EventBus | None = None,
        max_batch: int = 50,
    ) -> int:
        """Fetch pending outbox records and publish them via the Event Bus."""
        bus = bus or get_event_bus()
        path = Path(db_path)
        if not path.exists():
            return 0

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cls.init_schema(conn)

        cursor = conn.execute(
            """
            SELECT id, event_id, event_type, producer, payload, correlation_id, version, retry_count
            FROM outbox_events
            WHERE status = 'PENDING'
            ORDER BY id ASC
            LIMIT ?
            """,
            (max_batch,),
        )
        rows = cursor.fetchall()
        published_count = 0

        for row in rows:
            event = Event(
                event_id=row["event_id"],
                event_type=row["event_type"],
                producer=row["producer"],
                payload=json.loads(row["payload"]),
                correlation_id=row["correlation_id"],
                version=row["version"],
            )

            try:
                success = await bus.publish(event)
                now = datetime.now(timezone.utc).isoformat()
                if success:
                    conn.execute(
                        "UPDATE outbox_events SET status = 'PUBLISHED', published_at = ?, last_error = NULL WHERE id = ?",
                        (now, row["id"]),
                    )
                    published_count += 1
                else:
                    conn.execute(
                        "UPDATE outbox_events SET retry_count = retry_count + 1, last_error = 'Publish failed' WHERE id = ?",
                        (row["id"],),
                    )
            except Exception as exc:
                logger.error("[OUTBOX] Error publishing outbox event %s: %s", row["event_id"], exc)
                conn.execute(
                    "UPDATE outbox_events SET retry_count = retry_count + 1, last_error = ? WHERE id = ?",
                    (str(exc)[:500], row["id"]),
                )

        conn.commit()
        conn.close()
        return published_count

    @classmethod
    async def outbox_worker_loop(
        cls,
        db_path: str | Path,
        bus: EventBus | None = None,
        interval_seconds: float = 3.0,
    ) -> None:
        """Background loop to periodically drain the outbox."""
        while True:
            try:
                await cls.publish_pending(db_path, bus)
            except Exception as exc:
                logger.debug("[OUTBOX] Worker pass error: %s", exc)
            await asyncio.sleep(interval_seconds)
