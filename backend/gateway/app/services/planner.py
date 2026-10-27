"""Planner lesson assembly from prebuilt learning assets."""

from __future__ import annotations

from typing import Any

from shared.topic_normalization import normalize_subject, normalize_topic

from app.services.learning_assets import asset_response
from app.services.validation import validate_question


async def planner_lesson(payload: dict[str, Any]) -> dict[str, Any]:
    question = validate_question(payload, "question", "topic", "title")
    filters = {
        "grade": payload.get("grade"),
        "subject": normalize_subject(payload.get("subject")),
        "chapter": payload.get("chapter"),
        "topic": normalize_topic(payload.get("topic"), question).normalized_topic or question,
    }

    summary_assets = await asset_response("summaries", dict(filters))
    flashcard_assets = await asset_response("flashcards", dict(filters))
    quiz_assets = await asset_response("quizzes", dict(filters))

    summaries = summary_assets.get("items", [])
    flashcards = flashcard_assets.get("items", [])[:10]
    quizzes = quiz_assets.get("items", [])[:8]
    first_summary = summaries[0] if summaries else {}
    summary_text = first_summary.get("text") or (
        f"No prebuilt summary was found for {question or 'this lesson'}. Use tutor fallback for explanation."
    )

    return {
        "request": payload,
        "response_source": "planner",
        "summary": {
            "title": first_summary.get("title") or question or "Lesson",
            "text": summary_text,
            "metadata": first_summary.get("metadata", {}),
        },
        "flashcards": flashcards,
        "practice_questions": quizzes,
        "quiz": quizzes,
        "asset_counts": {
            "summaries": len(summaries),
            "flashcards": len(flashcards),
            "quizzes": len(quizzes),
        },
    }
