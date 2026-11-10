from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from shared.events.bus import EventBus, get_event_bus
from shared.events.schemas import Event

logger = logging.getLogger(__name__)


class IdempotencyStore:
    """Tracks processed event IDs to prevent duplicate execution (idempotency)."""

    SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS processed_events (
        event_id TEXT NOT NULL,
        consumer_id TEXT NOT NULL,
        processed_at TEXT NOT NULL,
        PRIMARY KEY (event_id, consumer_id)
    );
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        self._memory_set: set[tuple[str, str]] = set()

        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.executescript(self.SCHEMA_DDL)

    def is_processed(self, event_id: str, consumer_id: str) -> bool:
        if (event_id, consumer_id) in self._memory_set:
            return True
        if self.db_path and self.db_path.exists():
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.execute(
                    "SELECT 1 FROM processed_events WHERE event_id = ? AND consumer_id = ?",
                    (event_id, consumer_id),
                )
                return cur.fetchone() is not None
        return False

    def mark_processed(self, event_id: str, consumer_id: str) -> None:
        self._memory_set.add((event_id, consumer_id))
        if self.db_path:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_events (event_id, consumer_id, processed_at) VALUES (?, ?, ?)",
                    (event_id, consumer_id, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()


class ResilientConsumer:
    """Consumer wrapper providing idempotency, automated retries, and dead-lettering."""

    def __init__(
        self,
        consumer_id: str,
        bus: EventBus | None = None,
        idempotency_store: IdempotencyStore | None = None,
        max_retries: int = 3,
    ) -> None:
        self.consumer_id = consumer_id
        self.bus = bus or get_event_bus()
        self.idempotency = idempotency_store or IdempotencyStore()
        self.max_retries = max_retries
        self.dead_letters: list[dict[str, Any]] = []

    def wrap_handler(
        self,
        handler: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> Callable[[Event], Coroutine[Any, Any, None]]:
        async def _wrapped(event: Event) -> None:
            # 1. Idempotency check
            if self.idempotency.is_processed(event.event_id, self.consumer_id):
                logger.info(
                    "[CONSUMER:%s] Skipping duplicate event %s (%s)",
                    self.consumer_id,
                    event.event_id,
                    event.event_type,
                )
                return

            # 2. Retry loop
            attempts = 0
            while attempts < self.max_retries:
                try:
                    await handler(event)
                    self.idempotency.mark_processed(event.event_id, self.consumer_id)
                    logger.debug(
                        "[CONSUMER:%s] Successfully processed %s (%s)",
                        self.consumer_id,
                        event.event_id,
                        event.event_type,
                    )
                    return
                except Exception as exc:
                    attempts += 1
                    logger.warning(
                        "[CONSUMER:%s] Attempt %d/%d failed for event %s: %s",
                        self.consumer_id,
                        attempts,
                        self.max_retries,
                        event.event_id,
                        exc,
                    )
                    if attempts < self.max_retries:
                        await asyncio.sleep(0.5 * (2 ** (attempts - 1)))

            # 3. Dead letter routing
            dl_entry = {
                "event": event.model_dump(),
                "consumer_id": self.consumer_id,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
            }
            self.dead_letters.append(dl_entry)
            logger.error(
                "[CONSUMER:%s] Event %s exceeded max retries (%d). Routed to Dead-Letter Queue.",
                self.consumer_id,
                event.event_id,
                self.max_retries,
            )

        return _wrapped

    async def register(
        self,
        pattern: str,
        handler: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> None:
        wrapped = self.wrap_handler(handler)
        await self.bus.subscribe(pattern, wrapped, queue_name=f"{self.consumer_id}.{pattern}")
