"""Payload validation helpers shared by tutor, planner, and RAG routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from shared.security import sanitize_llm_prompt


def validate_question(payload: dict[str, Any], *keys: str) -> str:
    """Reject empty/oversize prompts before any asset-search or LLM fanout."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            query = sanitize_llm_prompt(value.strip())
            if len(query) > 2000:
                raise HTTPException(status_code=422, detail=f"Field '{key}' exceeds 2000 characters.")
            return query
    raise HTTPException(status_code=422, detail=f"One of {list(keys)} must be a non-empty string.")


def clamp_grade(value: Any) -> int:
    try:
        grade = int(value)
    except (TypeError, ValueError):
        return 9
    return min(12, max(5, grade))
