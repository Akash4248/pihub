from __future__ import annotations

import logging
from typing import Optional
from pydantic import BaseModel

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes import router as experiment_router
from app.ai.routes import router as ai_router
from app.builder.routes import router as builder_router
from app.classroom.routes import router as classroom_router
from app.core.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.core.database import verify_sqlite_database
from app.core.observability import request_observability_middleware
from app.core.payload_limits import payload_limit_middleware
from app.experiment_content.routes import router as experiment_content_router
from app.manifest.routes import router as manifest_router
from app.maintenance.routes import router as maintenance_router
from app.sharing.routes import router as sharing_router
from app.storage.manifest_storage_repository import ManifestStorageRepository
from app.sharing.repositories.sharing_repository import SharingRepository
from app.classroom.repositories.classroom_repository import ClassroomRepository


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("experiment-service")


class HealthResponse(BaseModel):
    status: str
    service: str
    version: Optional[str] = "1.0"


app = FastAPI(title="experiment-service")
app.middleware("http")(request_observability_middleware)
app.middleware("http")(payload_limit_middleware)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.include_router(experiment_content_router)
app.include_router(experiment_router)
app.include_router(manifest_router)
app.include_router(builder_router)
app.include_router(ai_router)
app.include_router(sharing_router)
app.include_router(classroom_router)
app.include_router(maintenance_router)


@app.get("/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="experiment-service")

import os
from pathlib import Path
if Path("/shared/packs/phet_simulations_v1/simulations").exists():
    app.mount("/simulations", StaticFiles(directory="/shared/packs/phet_simulations_v1/simulations", follow_symlink=False), name="simulations")
elif os.path.exists("/shared/simulations"):
    app.mount("/simulations", StaticFiles(directory="/shared/simulations", follow_symlink=False), name="simulations")

@app.middleware("http")
async def api_version_middleware(request, call_next):
    response = await call_next(request)
    response.headers["x-api-version"] = "1.0"
    return response


@app.on_event("startup")
async def verify_databases() -> None:
    repositories = (
        ManifestStorageRepository(),
        SharingRepository(),
        ClassroomRepository(),
    )
    for repository in repositories:
        report = verify_sqlite_database(repository.db_path)
        logger.info("[DATABASE] STARTUP_HEALTH_CHECK=%s", report)
    logger.info("[DATABASE] STARTUP_VERIFICATION_COMPLETE")
