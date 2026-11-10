"""PIHUB gateway composition root.

Route handlers live in ``app.routes.*``; business logic lives in
``app.services.*``. This module only wires middleware, lifespan, mounts,
and routers — keep new endpoints in the appropriate routes module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from shared.config import get_settings
from shared.security import InternalAuthMiddleware
from shared.tracing import TracingMiddleware
from shared.resilience import RateLimitMiddleware

from app.context import bind_application
from app.middleware.structlog_middleware import StructuredLoggingMiddleware
from app.routes import (
    ai,
    architecture,
    assets,
    auth,
    debug,
    demo,
    experiments,
    ingest,
    packs,
    pdf,
    pihub,
    rag,
    system,
    voice,
)
from app.services import simulations
from app.services.discovery import discovery_beacon_loop


settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    app.state.http = httpx.AsyncClient(
        timeout=settings.gateway_http_timeout_seconds,
        limits=limits,
    )
    app.state.started_at = time.time()
    app.state.discovery_task = asyncio.create_task(discovery_beacon_loop())
    try:
        yield
    finally:
        app.state.discovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.discovery_task
        await app.state.http.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
bind_application(app)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(TracingMiddleware, service_name="gateway")
app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(
    InternalAuthMiddleware,
    public_prefixes=(
        "/health", "/healthz", "/ready", "/readyz", "/livez", "/ping",
        "/metrics", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
        "/auth/register", "/auth/login", "/",
        "/api/v1/system", "/api/v1/discovery", "/api/v1/data-ownership",
        "/api/v1/sagas",
    ),
)
app.mount(
    "/simulations",
    StaticFiles(directory=str(simulations.SIMULATIONS_DIR), html=True, check_dir=False),
    name="simulations",
)


# Kept here (not in a router) because validation errors can originate from any
# sub-router and FastAPI expects the handler on the root application.
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.error("[TUTOR] VALIDATION_ERROR=%s", exc.errors())
    return JSONResponse(
        status_code=400,
        content={"detail": exc.errors()},
    )


# Registration order mirrors the original monolith so overlapping paths keep
# their original precedence.
for router in (
    architecture.router,
    auth.router,
    system.router,
    ingest.router,
    rag.router,
    ai.router,
    voice.router,
    packs.router,
    pdf.router,
    experiments.router,
    assets.router,
    demo.router,
    pihub.router,
    debug.router,
):
    app.include_router(router)
    # Expose API v1 versioned alias for core routers
    if router not in (architecture.router, auth.router, system.router):
        app.include_router(router, prefix="/api/v1")
