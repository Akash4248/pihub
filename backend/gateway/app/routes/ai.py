from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from shared.config import get_settings
from shared.security import sanitize_llm_prompt

from app.services.tutor import tutor_response
from app.services.upstream import AI_PROXY_TIMEOUT, proxy_to
from app.services.validation import validate_question


router = APIRouter(tags=["AI"])

settings = get_settings()


@router.post("/ai/chat")
async def ai_chat(payload: dict[str, Any]) -> Any:
    validate_question(payload, "prompt", "message", "question", "query")
    sanitized_payload = dict(payload)
    if "prompt" in sanitized_payload and isinstance(sanitized_payload["prompt"], str):
        sanitized_payload["prompt"] = sanitize_llm_prompt(sanitized_payload["prompt"])
    if "message" in sanitized_payload and isinstance(sanitized_payload["message"], str):
        sanitized_payload["message"] = sanitize_llm_prompt(sanitized_payload["message"])
    return await proxy_to(settings.inference_service_url, "POST", "/ai/chat", sanitized_payload, timeout=AI_PROXY_TIMEOUT)


@router.post("/ai/tutor")
async def ai_tutor(payload: dict[str, Any]) -> Any:
    return await tutor_response(payload)


@router.post("/ai/tutor/debug")
async def ai_tutor_debug(payload: dict[str, Any]) -> Any:
    return await proxy_to(settings.inference_service_url, "POST", "/ai/tutor/debug", payload)


@router.post("/ai/tutor/evaluate")
async def ai_tutor_evaluate(payload: dict[str, Any]) -> Any:
    return await proxy_to(settings.inference_service_url, "POST", "/ai/tutor/evaluate", payload)


@router.get("/ai/health")
async def ai_health() -> dict[str, Any]:
    return await proxy_to(settings.inference_service_url, "GET", "/ai/health")
