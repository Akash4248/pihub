from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from shared.config import get_settings

logger = logging.getLogger(__name__)


class TokenBucket:
    def __init__(self, capacity: int, fill_rate: float) -> None:
        self.capacity = float(capacity)
        self.fill_rate = float(fill_rate)  # tokens per second
        self.tokens = float(capacity)
        self.last_update = time.time()

    def consume(self, tokens: float = 1.0) -> tuple[bool, int, float]:
        now = time.time()
        elapsed = now - self.last_update
        self.last_update = now

        # Replenish
        self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)

        if self.tokens >= tokens:
            self.tokens -= tokens
            remaining = int(self.tokens)
            return True, remaining, 0.0

        # Calculate time needed to replenish 1 token
        needed = tokens - self.tokens
        retry_after = round(needed / self.fill_rate, 2)
        return False, 0, retry_after


class RateLimiter:
    """In-memory Token Bucket rate limiter per client key."""

    def __init__(self, requests_per_minute: int = 120, burst_capacity: int = 30) -> None:
        self.capacity = burst_capacity
        self.fill_rate = requests_per_minute / 60.0
        self.buckets: dict[str, TokenBucket] = {}

    def is_allowed(self, client_key: str) -> tuple[bool, int, float]:
        if client_key not in self.buckets:
            self.buckets[client_key] = TokenBucket(self.capacity, self.fill_rate)
        return self.buckets[client_key].consume()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI Middleware enforcing Token Bucket rate limiting on public routes."""

    EXEMPT_PREFIXES = (
        "/health", "/healthz", "/ready", "/livez", "/metrics",
        "/docs", "/redoc", "/openapi.json", "/favicon.ico", "/simulations",
    )

    def __init__(self, app: Any, requests_per_minute: int | None = None, burst: int | None = None) -> None:
        super().__init__(app)
        settings = get_settings()
        rpm = requests_per_minute or settings.rate_limit_per_minute
        burst_cap = burst or settings.rate_limit_burst
        self.limiter = RateLimiter(requests_per_minute=rpm, burst_capacity=burst_cap)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
            return await call_next(request)

        # Derive client key (authenticated user token or client IP)
        client_key = request.client.host if request.client else "unknown_client"
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            client_key = auth_header[-16:]

        allowed, remaining, retry_after = self.limiter.is_allowed(client_key)

        if not allowed:
            logger.warning("[RATE_LIMIT] Client %s exceeded rate limit on %s (retry in %.1fs)", client_key, path, retry_after)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "TOO_MANY_REQUESTS",
                    "detail": "Rate limit exceeded. Please slow down your requests.",
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(int(retry_after) + 1),
                    "X-RateLimit-Limit": str(self.limiter.capacity),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limiter.capacity)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
