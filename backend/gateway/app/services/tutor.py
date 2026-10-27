"""Tutor orchestration: planner fallback, asset enrichment, RAG forwarding."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from shared.config import get_settings
from shared.topic_normalization import normalize_topic, should_use_planner

from app.services import planner as planner_service
from app.services.learning_assets import asset_bundle_count, asset_context, asset_search_bundle
from app.services.metrics import record_retrieval_metric
from app.services.upstream import AI_PROXY_TIMEOUT, proxy_request, proxy_to
from app.services.validation import clamp_grade, validate_question


logger = logging.getLogger(__name__)

settings = get_settings()


def log_retrieval(query: str, normalized_topic: str | None, intent: Any, chunks_retrieved: int, chunks_used: int, fallback_reason: str | None) -> None:
    logger.info(
        "[RAG] RETRIEVAL query=%s normalized_topic=%s intent=%s chunks_retrieved=%s chunks_used=%s fallback_reason=%s",
        query,
        normalized_topic,
        intent,
        chunks_retrieved,
        chunks_used,
        fallback_reason,
    )


async def forward_rag(
    *,
    kind: str,
    query: str,
    path: str,
    method: str,
    limit: int,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Proxy a RAG request to content-pipeline, then log + record one metric per request."""
    started = time.perf_counter()
    result = await proxy_request(settings.content_pipeline_url, path, method=method, payload=payload, params=params)
    total_ms = (time.perf_counter() - started) * 1000
    results = result.get("results", []) if isinstance(result, dict) else []
    top_score = float(results[0].get("score", 0.0)) if results else 0.0
    record_retrieval_metric({
        "query": query,
        "kind": kind,
        "limit": limit,
        "results_returned": len(results),
        "top_score": round(top_score, 4),
        "chunks_retrieved": len(results),
        "chunks_used": len(results),
        "asset_matches": 0,
        "asset_ms": 0.0,
        "rag_ms": round(total_ms, 2),
        "total_ms": round(total_ms, 2),
        "fallback_reason": None if results else "no_rag_match",
    })
    logger.info(
        "[RAG] kind=%s query=%r limit=%d returned=%d top_score=%.3f total_ms=%.1f top_ids=%s",
        kind,
        query,
        limit,
        len(results),
        top_score,
        total_ms,
        [str(r.get("id"))[:8] for r in results[:5]],
    )
    return result


def planner_stream(response: dict[str, Any]) -> StreamingResponse:
    async def stream() -> Any:
        yield f"data: {json.dumps({'chunk': response.get('answer', ''), 'done': True})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


async def planner_tutor_response(payload: dict[str, Any], normalized_topic: str | None) -> dict[str, Any]:
    planner_payload = dict(payload)
    if normalized_topic:
        planner_payload["topic"] = normalized_topic
    lesson = await planner_service.planner_lesson(planner_payload)
    summary = lesson.get("summary", {})
    answer = str(summary.get("text") or "")
    return {
        "answer": answer,
        "model": "planner",
        "context": [],
        "response_source": "planner",
        "planner": lesson,
    }


async def tutor_response(payload: dict[str, Any]) -> Any:
    started = time.perf_counter()
    query = validate_question(payload, "question", "query")
    payload = {**payload, "grade": clamp_grade(payload.get("grade", 9))}
    normalization = normalize_topic(payload.get("topic"), query)

    normalized_topic = normalization.normalized_topic
    intent = payload.get("intent")

    if should_use_planner(intent, query):
        response = await planner_tutor_response(payload, normalized_topic)
        total_ms = (time.perf_counter() - started) * 1000
        record_retrieval_metric({
            "query": query,
            "intent": intent,
            "topic": payload.get("topic"),
            "normalized_topic": normalized_topic,
            "matched_alias": normalization.matched_alias,
            "response_source": "planner",
            "fallback_reason": "planner_trigger",
            "asset_matches": len(response.get("planner", {}).get("flashcards", [])) + len(response.get("planner", {}).get("quiz", [])),
            "chunks_retrieved": 0,
            "chunks_used": 0,
            "asset_ms": round(total_ms, 2),
            "rag_ms": 0.0,
            "total_ms": round(total_ms, 2),
        })
        log_retrieval(query, normalized_topic, intent, 0, 0, "planner_trigger")
        if payload.get("stream") is True:
            return planner_stream(response)
        return response

    asset_start = time.perf_counter()
    bundle = await asset_search_bundle(payload, normalized_topic)
    asset_ms = (time.perf_counter() - asset_start) * 1000
    asset_matches = asset_bundle_count(bundle)

    enriched_payload = dict(payload)
    if normalized_topic:
        enriched_payload["topic"] = normalized_topic
        enriched_payload["normalized_topic"] = normalized_topic
    enriched_payload["original_topic"] = normalization.original_topic
    enriched_payload["topic_alias"] = normalization.matched_alias
    enriched_payload["asset_context"] = asset_context(bundle)
    enriched_payload["asset_search"] = {
        "filters": bundle.get("filters", {}),
        "counts": {
            "summaries": len(bundle.get("summaries", [])),
            "flashcards": len(bundle.get("flashcards", [])),
            "quizzes": len(bundle.get("quizzes", [])),
            "glossary": len(bundle.get("glossary", [])),
        },
    }

    result = await proxy_to(settings.inference_service_url, "POST", "/ai/tutor", enriched_payload, timeout=AI_PROXY_TIMEOUT)
    total_ms = (time.perf_counter() - started) * 1000
    if isinstance(result, StreamingResponse):
        record_retrieval_metric({
            "query": query,
            "intent": intent,
            "topic": payload.get("topic"),
            "normalized_topic": normalized_topic,
            "matched_alias": normalization.matched_alias,
            "response_source": "inference_service",
            "fallback_reason": None if asset_matches else "no_asset_match",
            "asset_matches": asset_matches,
            "chunks_retrieved": 0,
            "chunks_used": 0,
            "asset_ms": round(asset_ms, 2),
            "rag_ms": 0.0,
            "total_ms": round(total_ms, 2),
            "stream": True,
        })
        log_retrieval(query, normalized_topic, intent, 0, 0, None if asset_matches else "no_asset_match")
        return result

    chunks_retrieved = len(result.get("context", [])) if isinstance(result, dict) else 0
    chunks_used = chunks_retrieved
    fallback_reason = None
    if asset_matches == 0 and chunks_retrieved == 0:
        fallback_reason = "no_asset_or_rag_match"
    elif asset_matches == 0:
        fallback_reason = "no_asset_match"
    elif chunks_retrieved == 0:
        fallback_reason = "no_rag_match"
    record_retrieval_metric({
        "query": query,
        "intent": intent,
        "topic": payload.get("topic"),
        "normalized_topic": normalized_topic,
        "matched_alias": normalization.matched_alias,
        "response_source": "inference_service",
        "fallback_reason": fallback_reason,
        "asset_matches": asset_matches,
        "chunks_retrieved": chunks_retrieved,
        "chunks_used": chunks_used,
        "asset_ms": round(asset_ms, 2),
        "rag_ms": 0.0,
        "total_ms": round(total_ms, 2),
        "stream": False,
    })
    log_retrieval(query, normalized_topic, intent, chunks_retrieved, chunks_used, fallback_reason)
    return result
