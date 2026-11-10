from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Coroutine

from fastapi import HTTPException

from shared.config import get_settings

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenException(HTTPException):
    def __init__(self, service_name: str, message: str | None = None) -> None:
        detail = message or f"Service '{service_name}' is temporarily unavailable (circuit breaker is OPEN)."
        super().__init__(
            status_code=503,
            detail={
                "status": "degraded",
                "error": "CIRCUIT_BREAKER_OPEN",
                "service": service_name,
                "detail": detail,
            },
        )


class CircuitBreaker:
    """Production-grade Circuit Breaker pattern implementation with per-service fault isolation."""

    def __init__(
        self,
        name: str,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        half_open_max_calls: int = 2,
        fallback_factory: Callable[..., Any] | None = None,
    ) -> None:
        settings = get_settings()
        self.name = name
        self.failure_threshold = failure_threshold or settings.circuit_breaker_failure_threshold
        self.recovery_timeout = recovery_timeout or settings.circuit_breaker_recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.fallback_factory = fallback_factory

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.half_open_calls = 0
        self.last_failure_time = 0.0
        self.total_requests = 0
        self.total_failures = 0
        self.total_circuit_open_rejections = 0
        self._lock = asyncio.Lock()

    @property
    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            return (time.time() - self.last_failure_time) >= self.recovery_timeout
        return self.half_open_calls < self.half_open_max_calls

    async def call(self, func: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            self.total_requests += 1
            now = time.time()

            # State Transition check: OPEN -> HALF_OPEN
            if self.state == CircuitState.OPEN:
                if (now - self.last_failure_time) >= self.recovery_timeout:
                    logger.info("[CIRCUIT_BREAKER:%s] Recovery timeout reached -> Transition to HALF_OPEN", self.name)
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    self.success_count = 0
                else:
                    self.total_circuit_open_rejections += 1
                    logger.warning("[CIRCUIT_BREAKER:%s] Fast-failing request (Circuit is OPEN)", self.name)
                    if self.fallback_factory:
                        return self.fallback_factory(*args, **kwargs)
                    raise CircuitBreakerOpenException(self.name)

            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    self.total_circuit_open_rejections += 1
                    logger.warning("[CIRCUIT_BREAKER:%s] HALF_OPEN test capacity reached, rejecting extra call", self.name)
                    if self.fallback_factory:
                        return self.fallback_factory(*args, **kwargs)
                    raise CircuitBreakerOpenException(self.name, "Circuit is testing recovery in HALF_OPEN state.")
                self.half_open_calls += 1

        # Execute target function
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as exc:
            await self._on_failure(exc)
            if self.fallback_factory:
                logger.info("[CIRCUIT_BREAKER:%s] Providing graceful fallback response for %s", self.name, exc)
                return self.fallback_factory(*args, **kwargs)
            raise

    async def _on_success(self) -> None:
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                logger.info(
                    "[CIRCUIT_BREAKER:%s] HALF_OPEN probe succeeded (%d/%d)",
                    self.name,
                    self.success_count,
                    self.half_open_max_calls,
                )
                if self.success_count >= self.half_open_max_calls:
                    logger.info("[CIRCUIT_BREAKER:%s] Service recovered! Transition to CLOSED", self.name)
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.half_open_calls = 0
                    self.success_count = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            self.total_failures += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                logger.warning(
                    "[CIRCUIT_BREAKER:%s] Probe failed in HALF_OPEN (%s) -> Transition back to OPEN",
                    self.name,
                    exc,
                )
                self.state = CircuitState.OPEN
                self.half_open_calls = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                logger.warning(
                    "[CIRCUIT_BREAKER:%s] Failure %d/%d: %s",
                    self.name,
                    self.failure_count,
                    self.failure_threshold,
                    exc,
                )
                if self.failure_count >= self.failure_threshold:
                    logger.error(
                        "[CIRCUIT_BREAKER:%s] Threshold exceeded -> Transition to OPEN for %.1fs",
                        self.name,
                        self.recovery_timeout,
                    )
                    self.state = CircuitState.OPEN

    def reset(self) -> None:
        """Manually reset circuit breaker to healthy CLOSED state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.half_open_calls = 0
        self.last_failure_time = 0.0

    def get_stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "half_open_calls": self.half_open_calls,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_rejections": self.total_circuit_open_rejections,
            "last_failure_time": self.last_failure_time,
        }


class CircuitBreakerRegistry:
    """Manages dedicated Circuit Breakers for each downstream microservice."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_breaker(
        self,
        service_name: str,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        fallback_factory: Callable[..., Any] | None = None,
    ) -> CircuitBreaker:
        if service_name not in self._breakers:
            self._breakers[service_name] = CircuitBreaker(
                name=service_name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                fallback_factory=fallback_factory,
            )
        return self._breakers[service_name]

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        return {name: cb.get_stats() for name, cb in self._breakers.items()}


_cb_registry: CircuitBreakerRegistry | None = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    global _cb_registry
    if _cb_registry is None:
        _cb_registry = CircuitBreakerRegistry()
    return _cb_registry
