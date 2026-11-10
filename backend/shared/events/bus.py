from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

from shared.config import get_settings
from shared.events.schemas import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Resilient RabbitMQ Event Bus with in-memory fallback for offline/testing."""

    def __init__(
        self,
        rabbitmq_url: str | None = None,
        exchange_name: str | None = None,
        dlx_name: str | None = None,
        dlq_name: str | None = None,
    ) -> None:
        settings = get_settings()
        self.rabbitmq_url = rabbitmq_url or settings.rabbitmq_url
        self.exchange_name = exchange_name or settings.rabbitmq_exchange
        self.dlx_name = dlx_name or settings.rabbitmq_dlx
        self.dlq_name = dlq_name or settings.rabbitmq_dlq

        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._dlx_exchange: Any = None
        self._is_connected = False
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._published_history: list[Event] = []
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def connect(self) -> bool:
        """Attempt connection to RabbitMQ; fall back gracefully if unreachable."""
        async with self._lock:
            if self._is_connected:
                return True

            try:
                import aio_pika

                self._connection = await asyncio.wait_for(
                    aio_pika.connect_robust(self.rabbitmq_url),
                    timeout=3.0,
                )
                self._channel = await self._connection.channel()
                await self._channel.set_qos(prefetch_count=10)

                # Declare Dead Letter Exchange & Queue
                self._dlx_exchange = await self._channel.declare_exchange(
                    self.dlx_name,
                    aio_pika.ExchangeType.TOPIC,
                    durable=True,
                )
                dlq = await self._channel.declare_queue(self.dlq_name, durable=True)
                await dlq.bind(self._dlx_exchange, routing_key="#")

                # Declare Main Events Exchange
                self._exchange = await self._channel.declare_exchange(
                    self.exchange_name,
                    aio_pika.ExchangeType.TOPIC,
                    durable=True,
                )

                self._is_connected = True
                logger.info("[EVENT_BUS] Connected to RabbitMQ at %s", self.rabbitmq_url)
                return True
            except Exception as exc:
                logger.warning(
                    "[EVENT_BUS] RabbitMQ connection failed (%s); operating in in-memory event bus mode",
                    exc,
                )
                self._is_connected = False
                return False

    async def publish(self, event: Event, routing_key: str | None = None) -> bool:
        """Publish event to RabbitMQ exchange or local memory subscribers."""
        key = routing_key or event.event_type
        self._published_history.append(event)

        # 1. Deliver to local in-memory subscribers (always active for fast local dispatch)
        await self._dispatch_local(event, key)

        # 2. Deliver to RabbitMQ if connected
        if not self._is_connected:
            await self.connect()

        if self._is_connected and self._exchange is not None:
            try:
                import aio_pika

                message = aio_pika.Message(
                    body=event.to_json().encode("utf-8"),
                    content_type="application/json",
                    message_id=event.event_id,
                    correlation_id=event.correlation_id,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers={
                        "producer": event.producer,
                        "version": event.version,
                        "event_type": event.event_type,
                    },
                )
                await self._exchange.publish(message, routing_key=key)
                logger.debug("[EVENT_BUS] Published %s (key=%s, id=%s)", event.event_type, key, event.event_id)
                return True
            except Exception as exc:
                logger.error("[EVENT_BUS] Failed to publish event %s to RabbitMQ: %s", event.event_id, exc)
                return False

        return True

    async def subscribe(
        self,
        event_type_pattern: str,
        handler: EventHandler,
        queue_name: str | None = None,
    ) -> None:
        """Register subscriber for event pattern."""
        self._subscribers.setdefault(event_type_pattern, []).append(handler)

        if not self._is_connected:
            await self.connect()

        if self._is_connected and self._channel is not None:
            try:
                import aio_pika

                q_name = queue_name or f"pihub.{event_type_pattern}"
                # Bind with Dead Letter Exchange configuration
                queue = await self._channel.declare_queue(
                    q_name,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": self.dlx_name,
                        "x-dead-letter-routing-key": f"dlq.{event_type_pattern}",
                    },
                )
                await queue.bind(self._exchange, routing_key=event_type_pattern)

                async def _on_message(msg: aio_pika.IncomingMessage) -> None:
                    async with msg.process(requeue=False, reject_on_redelivered=True):
                        try:
                            parsed_event = Event.from_json(msg.body)
                            await handler(parsed_event)
                        except Exception as h_exc:
                            logger.error("[EVENT_BUS] Handler error for %s: %s", msg.message_id, h_exc)
                            raise

                await queue.consume(_on_message)
                logger.info("[EVENT_BUS] Subscribed to %s on queue %s", event_type_pattern, q_name)
            except Exception as exc:
                logger.warning("[EVENT_BUS] Failed to bind RabbitMQ queue %s: %s", event_type_pattern, exc)

    async def _dispatch_local(self, event: Event, routing_key: str) -> None:
        """Dispatch event to local registered handlers matching the pattern."""
        for pattern, handlers in self._subscribers.items():
            if self._matches_pattern(pattern, routing_key):
                for handler in handlers:
                    try:
                        res = handler(event)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:
                        logger.error("[EVENT_BUS] Local subscriber %s failed: %s", pattern, exc)

    @staticmethod
    def _matches_pattern(pattern: str, key: str) -> bool:
        if pattern == "#" or pattern == key:
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return key.startswith(prefix)
        return False

    async def close(self) -> None:
        async with self._lock:
            if self._connection:
                try:
                    await self._connection.close()
                except Exception:
                    pass
                self._is_connected = False


# Global singleton instance
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def dispatch_event_safe(event: Event) -> None:
    """Safely dispatches an event if an event loop is running, without raising on sync contexts."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(get_event_bus().publish(event))
    except RuntimeError:
        # No running event loop (e.g. sync unit test thread)
        pass
    except Exception as exc:
        logger.debug("[EVENT_BUS] Safe dispatch skipped: %s", exc)

