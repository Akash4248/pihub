from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from shared.config import get_settings
from shared.security import verify_internal_api_key

from app.services.upstream import get_json, post_json, proxy_to


router = APIRouter(tags=["PiHub"])

settings = get_settings()


@router.post("/progress")
async def progress_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await post_json(settings.pihub_url, "/progress", payload)


@router.get("/progress/{student_id}")
async def progress_get(student_id: str) -> dict[str, Any]:
    return await get_json(settings.pihub_url, f"/progress/{student_id}")


@router.post("/quiz-sessions")
async def quiz_session_create(payload: dict[str, Any]) -> dict[str, Any]:
    return await post_json(settings.pihub_url, "/quiz-sessions", payload)


@router.get("/quiz-sessions/student/{student_id}")
async def quiz_sessions_student(student_id: str, active_only: bool = Query(default=False)) -> dict[str, Any]:
    return await get_json(settings.pihub_url, f"/quiz-sessions/student/{student_id}", {"active_only": active_only})


@router.get("/quiz-sessions/{quiz_session_id}")
async def quiz_session_get(quiz_session_id: str) -> dict[str, Any]:
    return await get_json(settings.pihub_url, f"/quiz-sessions/{quiz_session_id}")


@router.post("/quiz-sessions/{quiz_session_id}/answer")
async def quiz_session_answer(quiz_session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await post_json(settings.pihub_url, f"/quiz-sessions/{quiz_session_id}/answer", payload)


@router.get("/classroom", dependencies=[Depends(verify_internal_api_key)])
async def classroom_get() -> dict[str, Any]:
    return await get_json(settings.pihub_url, "/classroom")


@router.post("/classroom", dependencies=[Depends(verify_internal_api_key)])
async def classroom_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_to(settings.pihub_url, "POST", "/classroom", payload)


@router.get("/devices", dependencies=[Depends(verify_internal_api_key)])
async def devices_get() -> dict[str, Any]:
    return await get_json(settings.pihub_url, "/devices")


@router.post("/devices", dependencies=[Depends(verify_internal_api_key)])
async def devices_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_to(settings.pihub_url, "POST", "/devices", payload)
