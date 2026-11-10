from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any, AsyncGenerator

import httpx
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from shared.config import get_settings
from shared.logging.json_logger import current_trace_id

logger = logging.getLogger(__name__)


def generate_trace_id() -> str:
    """Generate a 32-character hex trace ID complying with W3C TraceContext."""
    return secrets.token_hex(16)


def generate_span_id() -> str:
    """Generate a 16-character hex span ID complying with W3C TraceContext."""
    return secrets.token_hex(8)


class Span:
    """Represents a discrete timed execution segment within a distributed trace."""

    def __init__(
        self,
        name: str,
        service_name: str,
        trace_id: str,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.service_name = service_name
        self.trace_id = trace_id
        self.span_id = span_id or generate_span_id()
        self.parent_span_id = parent_span_id
        self.start_time = time.time()
        self.end_time: float | None = None
        self.duration_ms: float = 0.0
        self.attributes: dict[str, Any] = attributes or {}
        self.status = "OK"
        self.error: str | None = None

    def end(self, status: str = "OK", error: str | None = None) -> None:
        self.end_time = time.time()
        self.duration_ms = round((self.end_time - self.start_time) * 1000, 2)
        self.status = status
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "service": self.service_name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "status": self.status,
            "error": self.error,
        }

    @property
    def traceparent(self) -> str:
        """Returns W3C traceparent header value."""
        return f"00-{self.trace_id}-{self.span_id}-01"


class DistributedTracer:
    """Distributed tracing manager supporting W3C trace context, Jaeger OTLP export, and in-memory trace indexing."""

    def __init__(self, service_name: str = "pihub-service") -> None:
        self.service_name = service_name
        settings = get_settings()
        self.jaeger_url = settings.jaeger_url
        self.enabled = settings.enable_tracing
        self._completed_spans: list[Span] = []
        self._active_spans: dict[str, Span] = {}
        self._lock = asyncio.Lock()

    def start_span(
        self,
        name: str,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        t_id = trace_id or current_trace_id.get() or generate_trace_id()
        current_trace_id.set(t_id)

        span = Span(
            name=name,
            service_name=self.service_name,
            trace_id=t_id,
            parent_span_id=parent_span_id,
            attributes=attributes,
        )
        self._active_spans[span.span_id] = span
        return span

    def end_span(self, span: Span, status: str = "OK", error: str | None = None) -> None:
        span.end(status=status, error=error)
        self._active_spans.pop(span.span_id, None)
        self._completed_spans.append(span)
        if len(self._completed_spans) > 2000:
            self._completed_spans.pop(0)

        # Asynchronously push to Jaeger OTLP if configured and loop is running
        if self.enabled and self.jaeger_url:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._export_span_to_jaeger(span))
            except RuntimeError:
                pass

    async def _export_span_to_jaeger(self, span: Span) -> None:
        """Best-effort export of span to Jaeger / Tempo OTLP HTTP receiver."""
        try:
            otlp_payload = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": span.service_name}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": span.trace_id,
                                        "spanId": span.span_id,
                                        "parentSpanId": span.parent_span_id or "",
                                        "name": span.name,
                                        "startTimeUnixNano": int(span.start_time * 1e9),
                                        "endTimeUnixNano": int((span.end_time or span.start_time) * 1e9),
                                        "attributes": [
                                            {"key": k, "value": {"stringValue": str(v)}}
                                            for k, v in span.attributes.items()
                                        ],
                                        "status": {"code": 1 if span.status == "OK" else 2},
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.post(self.jaeger_url, json=otlp_payload)
        except Exception:
            # Non-blocking: trace export failures never affect transaction flow
            pass

    @contextlib.asynccontextmanager
    async def span(
        self,
        name: str,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Span, None]:
        s = self.start_span(name, trace_id=trace_id, parent_span_id=parent_span_id, attributes=attributes)
        try:
            yield s
            self.end_span(s, status="OK")
        except Exception as exc:
            self.end_span(s, status="ERROR", error=str(exc))
            raise

    def inject_headers(self, headers: dict[str, str], span: Span | None = None) -> dict[str, str]:
        """Injects W3C traceparent and X-Trace-ID into outgoing headers for inter-service calls."""
        res = dict(headers)
        if span:
            res["traceparent"] = span.traceparent
            res["X-Trace-ID"] = span.trace_id
        else:
            t_id = current_trace_id.get()
            if t_id:
                s_id = generate_span_id()
                res["traceparent"] = f"00-{t_id}-{s_id}-01"
                res["X-Trace-ID"] = t_id
        return res

    @staticmethod
    def extract_trace_context(headers: dict[str, str]) -> tuple[str | None, str | None]:
        """Parses W3C traceparent header: 00-{trace_id}-{parent_span_id}-{flags}."""
        traceparent = headers.get("traceparent") or headers.get("Traceparent")
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 4 and len(parts[1]) == 32:
                return parts[1], parts[2]

        x_trace_id = headers.get("x-trace-id") or headers.get("X-Trace-ID")
        if x_trace_id:
            return x_trace_id, None

        return None, None

    def get_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        """Returns structured trace groups organized by trace_id."""
        traces: dict[str, list[dict[str, Any]]] = {}
        for s in reversed(self._completed_spans[-limit * 10 :]):
            traces.setdefault(s.trace_id, []).append(s.to_dict())

        return [
            {
                "trace_id": t_id,
                "spans_count": len(spans),
                "total_duration_ms": max(s.get("duration_ms", 0.0) for s in spans),
                "spans": spans,
            }
            for t_id, spans in list(traces.items())[:limit]
        ]


_tracer: DistributedTracer | None = None


def get_tracer(service_name: str = "pihub-service") -> DistributedTracer:
    global _tracer
    if _tracer is None:
        _tracer = DistributedTracer(service_name=service_name)
    return _tracer


class TracingMiddleware(BaseHTTPMiddleware):
    """FastAPI Middleware that creates root spans and propagates W3C traceparent."""

    def __init__(self, app: Any, service_name: str = "service") -> None:
        super().__init__(app)
        self.tracer = get_tracer(service_name)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        trace_id, parent_span_id = self.tracer.extract_trace_context(dict(request.headers))
        if not trace_id:
            trace_id = generate_trace_id()

        trace_token = current_trace_id.set(trace_id)

        span_name = f"HTTP {request.method} {request.url.path}"
        span = self.tracer.start_span(
            name=span_name,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            attributes={
                "http.method": request.method,
                "http.url": str(request.url),
                "http.path": request.url.path,
                "client.host": request.client.host if request.client else "unknown",
            },
        )

        try:
            response = await call_next(request)
            span.attributes["http.status_code"] = response.status_code
            self.tracer.end_span(span, status="OK" if response.status_code < 400 else "ERROR")
            response.headers["traceparent"] = span.traceparent
            response.headers["X-Trace-ID"] = trace_id
            return response
        except Exception as exc:
            self.tracer.end_span(span, status="ERROR", error=str(exc))
            raise
        finally:
            current_trace_id.reset(trace_token)
