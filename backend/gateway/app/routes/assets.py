from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services.learning_assets import learning_asset
from app.services.planner import planner_lesson


router = APIRouter(tags=["Assets & Planner"])


@router.get("/flashcards")
async def flashcards(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await learning_asset("flashcards", grade, subject, chapter, topic)


@router.get("/quizzes")
async def quizzes(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await learning_asset("quizzes", grade, subject, chapter, topic)


@router.get("/glossary")
async def glossary(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await learning_asset("glossary", grade, subject, chapter, topic)


@router.get("/summaries")
async def summaries(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await learning_asset("summaries", grade, subject, chapter, topic)


@router.post("/planner/lesson")
async def planner_lesson_route(payload: dict[str, Any]) -> dict[str, Any]:
    return await planner_lesson(payload)
