from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.simulations import experiments_catalog_payload


router = APIRouter(tags=["Experiments"])


@router.get("/experiments/catalog")
async def experiments_catalog() -> Any:
    """Serve the PhET catalog formerly provided by experiment-service.

    The catalog file lives alongside the simulation bundles, so the gateway
    only needs the read-only simulations mount; no extra service is required.
    """
    return experiments_catalog_payload()
