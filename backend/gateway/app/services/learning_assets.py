"""Learning asset aggregation (flashcards, quizzes, glossary, summaries)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.config import get_settings
from shared.topic_normalization import normalize_subject

from app.services.pack_catalog import fetch_pack_preview, pack_service_packs


logger = logging.getLogger(__name__)

settings = get_settings()


def matches_topic(item: dict[str, Any], topic: str | None) -> bool:
    if not topic:
        return True
    topic_lower = topic.lower()
    candidates = [
        item.get("topic"),
        item.get("title"),
        item.get("term"),
        item.get("question"),
        item.get("front"),
    ]
    return any(isinstance(value, str) and topic_lower in value.lower() for value in candidates)


def asset_metadata(asset_name: str, item: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    source_title = item.get("title") or item.get("term") or item.get("front") or item.get("question") or manifest.get("chapter")
    difficulty = item.get("difficulty") or manifest.get("generation_metadata", {}).get("difficulty")
    source_types = {
        "flashcards": "flashcard",
        "quizzes": "quiz",
        "glossary": "glossary",
        "summaries": "summary",
    }
    return {
        "source_type": source_types.get(asset_name, asset_name),
        "source_title": source_title,
        "chapter": manifest.get("chapter"),
        "subject": manifest.get("subject"),
        "difficulty": difficulty,
        "version": manifest.get("version", "1.0.0"),
        "pack_id": manifest.get("pack_id"),
    }


async def asset_response(asset_name: str, filters: dict[str, Any]) -> dict[str, Any]:
    topic = filters.pop("topic", None)
    filters["subject"] = normalize_subject(filters.get("subject"))
    params = {key: value for key, value in filters.items() if value is not None}
    packs = await pack_service_packs(params)
    items: list[dict[str, Any]] = []

    valid_packs = [p for p in packs if p.get("pack_id")]
    dropped = 0
    if valid_packs:
        previews = await asyncio.gather(
            *[fetch_pack_preview(str(p["pack_id"])) for p in valid_packs],
            return_exceptions=True,
        )
        for pack, preview in zip(valid_packs, previews):
            if not isinstance(preview, dict):
                dropped += 1
                continue
            pack_id = str(pack["pack_id"])
            manifest = preview.get("manifest", {})
            for item in preview.get(asset_name, []) or []:
                if isinstance(item, dict) and matches_topic(item, topic):
                    metadata = asset_metadata(asset_name, item, manifest)
                    items.append({
                        "pack_id": pack_id,
                        "manifest": manifest,
                        "metadata": metadata,
                        **metadata,
                        **item,
                    })
    if dropped:
        logger.warning("[ASSET] PREVIEW_DROPPED asset=%s dropped=%d packs=%d", asset_name, dropped, len(valid_packs))
    return {"items": items, "total": len(items), "filters": {**params, "topic": topic}, "warnings": dropped}


async def chapter_knowledge_assets(filters: dict[str, Any]) -> list[dict[str, Any]]:
    params = {
        "grade": filters.get("grade"),
        "subject": normalize_subject(filters.get("subject")),
        "chapter": filters.get("chapter"),
    }
    topic = str(filters.get("topic") or "").lower()
    packs = await pack_service_packs({key: value for key, value in params.items() if value is not None})
    assets: list[dict[str, Any]] = []
    valid_packs = [p for p in packs[:2] if p.get("pack_id")]
    if valid_packs:
        previews = await asyncio.gather(
            *[fetch_pack_preview(str(p["pack_id"])) for p in valid_packs],
            return_exceptions=True,
        )
        for pack, preview in zip(valid_packs, previews):
            if not isinstance(preview, dict):
                continue
            pack_id = str(pack["pack_id"])
            knowledge = preview.get("chapter_knowledge") or {}
            if not isinstance(knowledge, dict):
                continue
            snippets = chapter_knowledge_snippets(knowledge, topic)
            if snippets:
                assets.append({
                    "title": preview.get("manifest", {}).get("chapter") or pack.get("chapter") or pack_id,
                    "text": "\n".join(snippets[:8]),
                    "metadata": {
                        "pack_id": pack_id,
                        "grade": pack.get("grade"),
                        "subject": pack.get("subject"),
                        "chapter": pack.get("chapter"),
                    },
                })
    return assets[:2]


def chapter_knowledge_snippets(knowledge: dict[str, Any], topic: str) -> list[str]:
    snippets: list[str] = []
    for key in ("concepts", "definitions", "relationships", "examples", "worked_examples", "formulas"):
        value = knowledge.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            text = knowledge_item_text(item)
            if not text:
                continue
            if topic and topic not in text.lower():
                continue
            snippets.append(f"{key}: {text}")
    if not snippets and not topic:
        for key in ("concepts", "definitions", "examples"):
            value = knowledge.get(key)
            if isinstance(value, list):
                snippets.extend(f"{key}: {knowledge_item_text(item)}" for item in value[:4] if knowledge_item_text(item))
    return snippets


def knowledge_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    for key in ("concept", "term", "title", "definition", "description", "text", "example", "formula"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    return " - ".join(parts).strip()


async def asset_search_bundle(payload: dict[str, Any], normalized_topic: str | None) -> dict[str, Any]:
    filters = {
        "grade": payload.get("grade"),
        "subject": normalize_subject(payload.get("subject")),
        "chapter": payload.get("chapter"),
        "topic": normalized_topic or payload.get("topic") or payload.get("question"),
    }
    summaries_task = asset_response("summaries", dict(filters))
    flashcards_task = asset_response("flashcards", dict(filters))
    quizzes_task = asset_response("quizzes", dict(filters))
    glossary_task = asset_response("glossary", dict(filters))
    chapter_task = chapter_knowledge_assets(filters)

    results = await asyncio.gather(
        summaries_task, flashcards_task, quizzes_task, glossary_task, chapter_task, return_exceptions=True
    )

    summaries = results[0] if isinstance(results[0], dict) else {}
    flashcards = results[1] if isinstance(results[1], dict) else {}
    quizzes = results[2] if isinstance(results[2], dict) else {}
    glossary = results[3] if isinstance(results[3], dict) else {}
    chapter_knowledge = results[4] if isinstance(results[4], list) else []

    return {
        "filters": filters,
        "summaries": summaries.get("items", [])[:3],
        "flashcards": flashcards.get("items", [])[:5],
        "quizzes": quizzes.get("items", [])[:5],
        "glossary": glossary.get("items", [])[:5],
        "chapter_knowledge": chapter_knowledge,
    }


def asset_bundle_count(bundle: dict[str, Any]) -> int:
    return sum(len(bundle.get(key, [])) for key in ("summaries", "flashcards", "quizzes", "glossary", "chapter_knowledge"))


def asset_context(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for item in bundle.get("summaries", []):
        context.append({
            "source_type": "summary",
            "title": item.get("source_title") or item.get("title"),
            "text": item.get("text"),
            "metadata": item.get("metadata", {}),
        })
    for item in bundle.get("glossary", []):
        context.append({
            "source_type": "glossary",
            "title": item.get("term") or item.get("source_title"),
            "text": item.get("definition"),
            "metadata": item.get("metadata", {}),
        })
    for item in bundle.get("flashcards", []):
        context.append({
            "source_type": "flashcard",
            "title": item.get("front") or item.get("source_title"),
            "text": item.get("back") or item.get("answer"),
            "metadata": item.get("metadata", {}),
        })
    for item in bundle.get("quizzes", []):
        context.append({
            "source_type": "quiz",
            "title": item.get("question") or item.get("source_title"),
            "text": item.get("answer") or item.get("explanation"),
            "metadata": item.get("metadata", {}),
        })
    for item in bundle.get("chapter_knowledge", []):
        context.append({
            "source_type": "chapter_knowledge",
            "title": item.get("title"),
            "text": item.get("text"),
            "metadata": item.get("metadata", {}),
        })
    return [item for item in context if item.get("text") or item.get("title")]


async def learning_asset(
    asset_name: str,
    grade: int | None,
    subject: str | None,
    chapter: str | None,
    topic: str | None,
) -> dict[str, Any]:
    return await asset_response(asset_name, {"grade": grade, "subject": subject, "chapter": chapter, "topic": topic})
