from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.tutor import tutor_response


router = APIRouter(tags=["Demo"])

DEMO_TOPICS: list[dict[str, Any]] = [
    {
        "id": "grade6_science_water_cycle",
        "title": "Water Cycle",
        "grade": 6,
        "subject": "science",
        "chapter": "water cycle",
        "sample_question": "Explain the water cycle with an example.",
    },
    {
        "id": "grade6_science_photosynthesis",
        "title": "Photosynthesis",
        "grade": 6,
        "subject": "science",
        "chapter": "photosynthesis",
        "sample_question": "What is photosynthesis?",
    },
    {
        "id": "grade6_science_motion",
        "title": "Motion",
        "grade": 6,
        "subject": "science",
        "chapter": "motion",
        "sample_question": "Explain motion in simple words.",
    },
]


@router.get("/demo/topics")
async def demo_topics() -> dict[str, Any]:
    return {"topics": DEMO_TOPICS}


@router.get("/demo")
async def demo_index() -> dict[str, Any]:
    return {
        "status": "ready",
        "topics": DEMO_TOPICS,
        "endpoints": {
            "tutor": "/demo/tutor",
            "topics": "/demo/topics",
            "coverage": "/packs/coverage",
        },
    }


@router.post("/demo/tutor")
async def demo_tutor(payload: dict[str, Any]) -> Any:
    topic_id = str(payload.get("topic_id") or DEMO_TOPICS[0]["id"])
    topic = next((item for item in DEMO_TOPICS if item["id"] == topic_id), DEMO_TOPICS[0])
    demo_payload = {
        "question": payload.get("question") or topic["sample_question"],
        "grade": topic["grade"],
        "subject": topic["subject"],
        "chapter": topic["chapter"],
        "topic": topic["title"],
        "language": payload.get("language") or "en",
        "stream": bool(payload.get("stream", False)),
        "sessionState": {
            "session_id": payload.get("session_id") or f"demo_{topic['id']}",
            "student_id": payload.get("student_id") or "demo_student",
        },
    }
    return await tutor_response(demo_payload)
