from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Context variables propagated across asynchronous tasks
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_request_id", default="")
current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_trace_id", default="")
current_service_name: contextvars.ContextVar[str] = contextvars.ContextVar("current_service_name", default="pihub-service")


class JsonFormatter(logging.Formatter):
    """Outputs standardized single-line JSON logs ready for Loki, Grafana, and ELK."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": getattr(record, "service", current_service_name.get()),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", current_request_id.get() or None),
            "trace_id": getattr(record, "trace_id", current_trace_id.get() or None),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Optional extras
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, "path"):
            log_entry["path"] = record.path
        if hasattr(record, "status_code"):
            log_entry["status_code"] = record.status_code

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_service_logging(service_name: str, log_level: str = "INFO") -> None:
    """Configures root logger with structured JSON formatting for the specified service."""
    current_service_name.set(service_name)
    level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)

    # Replace existing handlers to prevent duplicate output
    root.handlers.clear()
    root.addHandler(handler)


class DistributedLoggingMiddleware(BaseHTTPMiddleware):
    """FastAPI Middleware that extracts or assigns X-Request-ID and logs requests in JSON."""

    def __init__(self, app: Any, service_name: str = "service") -> None:
        super().__init__(app)
        self.service_name = service_name
        self.logger = logging.getLogger(f"http.{service_name}")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
        req_token = current_request_id.set(req_id)
        svc_token = current_service_name.set(self.service_name)

        start_time = time.time()
        try:
            response = await call_next(request)
            duration_ms = round((time.time() - start_time) * 1000, 2)

            response.headers["X-Request-ID"] = req_id
            self.logger.info(
                f"{request.method} {request.url.path} {response.status_code} ({duration_ms}ms)",
                extra={
                    "request_id": req_id,
                    "service": self.service_name,
                    "path": request.url.path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response
        except Exception as exc:
            duration_ms = round((time.time() - start_time) * 1000, 2)
            self.logger.error(
                f"{request.method} {request.url.path} FAILED ({duration_ms}ms): {exc}",
                exc_info=True,
                extra={
                    "request_id": req_id,
                    "service": self.service_name,
                    "path": request.url.path,
                    "method": request.method,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                },
            )
            raise
        finally:
            current_request_id.reset(req_token)
            current_service_name.reset(svc_token)
