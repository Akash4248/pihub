from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from shared.config import get_settings
from shared.security import verify_internal_api_key

from app.services.upstream import get_json, proxy_request


router = APIRouter(tags=["Debug"])

settings = get_settings()


@router.get("/debug/curriculum", dependencies=[Depends(verify_internal_api_key)])
async def debug_curriculum() -> dict[str, Any]:
    return await get_json(settings.content_pipeline_url, "/debug/curriculum")


@router.get("/debug/curriculum-relations", dependencies=[Depends(verify_internal_api_key)])
async def debug_curriculum_relations() -> dict[str, Any]:
    return await get_json(settings.content_pipeline_url, "/debug/curriculum-relations")


@router.get("/debug/metadata", dependencies=[Depends(verify_internal_api_key)])
async def debug_metadata(path: str) -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/metadata", method="GET", params={"path": path})


@router.get("/debug/chunks", dependencies=[Depends(verify_internal_api_key)])
async def debug_chunks(path: str) -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/chunks", method="GET", params={"path": path})


@router.post("/debug/retrieval", dependencies=[Depends(verify_internal_api_key)])
async def debug_retrieval(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/retrieval", method="POST", payload=payload)


@router.get("/debug/similarity", dependencies=[Depends(verify_internal_api_key)])
async def debug_similarity(left: str, right: str) -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/similarity", method="GET", params={"left": left, "right": right})


@router.get("/debug/pack-preview", dependencies=[Depends(verify_internal_api_key)])
async def debug_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/pack-preview", method="GET", params={"path": path, "pack_name": pack_name})


@router.get("/debug/learning-pack-preview", dependencies=[Depends(verify_internal_api_key)])
async def debug_learning_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/debug/learning-pack-preview", method="GET", params={"path": path, "pack_name": pack_name})
