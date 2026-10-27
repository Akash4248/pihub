from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter

from fastapi import HTTPException

from shared.config import get_settings

from app.context import get_application
from app.services.discovery import discovery_payload
from app.services.upstream import get_json, safe_http_get_json


router = APIRouter(tags=["System"])

settings = get_settings()


@router.get("/discovery")
async def discovery() -> dict[str, Any]:
    return discovery_payload()


@router.get("/discovery/beacon")
async def discovery_beacon() -> dict[str, Any]:
    return {
        "protocol": "udp-broadcast",
        "port": settings.discovery_port,
        "interval_seconds": 15,
        "payload": discovery_payload(),
    }


@router.get("/tutor/capabilities")
async def tutor_capabilities() -> dict[str, bool]:
    return {
        "streaming": True,
        "rag": True,
        "flashcards": True,
        "quizzes": True,
        "glossary": True,
        "summaries": True,
        "planner": True,
        "metrics": True,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    checks: dict[str, Any] = {"gateway": "ok"}
    status = "healthy"

    # Define tasks for concurrent execution
    content_task = safe_http_get_json(f"{settings.content_pipeline_url}/health")
    qdrant_task = safe_http_get_json(f"{settings.qdrant_url}/healthz")
    qdrant_coll_task = safe_http_get_json(f"{settings.qdrant_url}/collections/{settings.qdrant_collection}")
    inference_task = safe_http_get_json(f"{settings.inference_service_url}/ai/health")
    pihub_task = safe_http_get_json(f"{settings.pihub_url}/health")
    pihub_packs_task = safe_http_get_json(f"{settings.pihub_url}/packs")
    pack_svc_task = safe_http_get_json(f"{settings.pack_service_url}/packs")

    voice_task = (
        safe_http_get_json(f"{settings.voice_service_url}/health")
        if settings.voice_service_enabled
        else None
    )

    tasks = [content_task, qdrant_task, qdrant_coll_task, inference_task, pihub_task, pihub_packs_task, pack_svc_task]
    if voice_task:
        tasks.append(voice_task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process content_pipeline
    content_ok, content_body = results[0] if isinstance(results[0], tuple) else (False, {"error": str(results[0])})
    checks["content_pipeline"] = content_body
    if not content_ok:
        status = "degraded"

    # Process qdrant
    qdrant_ok, qdrant_body = results[1] if isinstance(results[1], tuple) else (False, {"error": str(results[1])})
    checks["qdrant"] = qdrant_body
    if not qdrant_ok:
        status = "degraded"

    # Process qdrant collection
    coll_ok, coll_body = results[2] if isinstance(results[2], tuple) else (False, {"error": str(results[2])})
    checks["qdrant_collection"] = coll_body
    chunk_count = 0
    if coll_ok and isinstance(coll_body, dict):
        res = coll_body.get("result", {})
        if isinstance(res, dict):
            chunk_count = int(res.get("points_count") or res.get("vectors_count") or 0)

    # Process inference
    inference_ok, inference_body = results[3] if isinstance(results[3], tuple) else (False, {"error": str(results[3])})
    checks["inference_service"] = inference_body
    if not inference_ok:
        status = "degraded"

    # Process pihub
    pihub_ok, pihub_body = results[4] if isinstance(results[4], tuple) else (False, {"error": str(results[4])})
    checks["pihub"] = pihub_body
    database_ok = pihub_ok
    if not pihub_ok:
        status = "degraded"

    # Process packs count
    pack_count = 0
    packs_ok, packs_body = results[5] if isinstance(results[5], tuple) else (False, {})
    if packs_ok and isinstance(packs_body, dict):
        pack_count = len(packs_body.get("packs", [])) + len(packs_body.get("cached", []))

    # Process pack_service
    pack_svc_ok, pack_svc_body = results[6] if isinstance(results[6], tuple) else (False, {})
    if pack_svc_ok and isinstance(pack_svc_body, dict):
        pack_count = max(pack_count, len(pack_svc_body.get("packs", [])))
        checks["pack_service"] = {"status": "ok", "pack_count": len(pack_svc_body.get("packs", []))}
    else:
        checks["pack_service"] = pack_svc_body if isinstance(pack_svc_body, dict) else {"status": "degraded"}

    # Process voice service
    voice_ok = False
    if settings.voice_service_enabled and len(results) > 7:
        v_ok, v_body = results[7] if isinstance(results[7], tuple) else (False, {})
        checks["voice_service"] = v_body
        voice_ok = v_ok
        if not v_ok and settings.voice_service_required:
            status = "degraded"
    else:
        checks["voice_service"] = {"healthy": True, "disabled": True}

    application = get_application()
    return {
        "status": status,
        "version": "1.0",
        "service": "gateway",
        "inference_service": inference_ok,
        "voice_service": (
            {"healthy": True, "disabled": True}
            if not settings.voice_service_enabled
            else {"healthy": voice_ok}
        ),
        "database": database_ok,
        "pack_count": pack_count,
        "chunk_count": chunk_count,
        "uptime_seconds": int(time.time() - getattr(application.state, "started_at", time.time())),
        "checks": checks,
    }


@router.get("/metrics/tutor")
async def tutor_metrics() -> dict[str, Any]:
    return await get_json(settings.inference_service_url, "/metrics/tutor")


@router.get("/metrics/retrieval")
async def retrieval_metrics_endpoint() -> dict[str, Any]:
    from app.services.metrics import retrieval_summary

    inference = {}
    try:
        inference = await get_json(settings.inference_service_url, "/metrics/retrieval")
    except HTTPException as exc:
        inference = {"status": "unavailable", "detail": exc.detail}
    content_pipeline = {}
    try:
        content_pipeline = await get_json(settings.content_pipeline_url, "/rag/metrics")
    except HTTPException as exc:
        content_pipeline = {"status": "unavailable", "detail": exc.detail}
    except Exception as exc:
        content_pipeline = {"status": "unavailable", "detail": str(exc)}
    return {
        "gateway": retrieval_summary(),
        "content_pipeline": content_pipeline,
        "inference": inference,
    }
