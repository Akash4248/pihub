from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from shared.config import get_settings
from shared.schemas import HealthResponse, IngestResponse, Metadata, SearchRequest, SearchResponse
from shared.topic_normalization import normalize_subject, normalize_topic, should_use_planner, topic_aliases
from app.services.experiment_service_client import ExperimentGatewayMetrics, ExperimentServiceClient


settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)
retrieval_metrics: deque[dict[str, Any]] = deque(maxlen=200)
experiment_gateway_metrics = ExperimentGatewayMetrics()

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    app.state.http = httpx.AsyncClient(
        timeout=settings.gateway_http_timeout_seconds,
        limits=limits,
    )
    app.state.experiment_client = ExperimentServiceClient(
        app.state.http,
        settings.experiment_service_url,
        experiment_gateway_metrics,
    )
    app.state.started_at = time.time()
    app.state.discovery_task = asyncio.create_task(_discovery_beacon_loop())
    try:
        yield
    finally:
        app.state.discovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.discovery_task
        await app.state.http.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

from app.services.auth_service import (
    register_user,
    authenticate_user,
    get_current_user_required,
    get_current_user_optional,
    extract_user_llm_credentials
)
from fastapi import Security

@app.post("/api/auth/register")
async def api_auth_register(payload: dict[str, Any]):
    email = payload.get("email")
    password = payload.get("password")
    full_name = payload.get("full_name", "Student User")
    role = payload.get("role", "student")
    grade = payload.get("grade", 9)
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    return register_user(email=email, password=password, full_name=full_name, role=role, grade=grade)

@app.post("/api/auth/login")
async def api_auth_login(payload: dict[str, Any]):
    email = payload.get("email")
    password = payload.get("password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    return authenticate_user(email=email, password=password)

@app.get("/api/auth/me")
async def api_auth_me(current_user: dict = Security(get_current_user_required)):
    return current_user


def _log_tag(path: str) -> str:
    if path.startswith("/discovery"):
        return "DISCOVERY"
    if path.startswith("/sync") or path.startswith("/packs/sync"):
        return "SYNC"
    if path.startswith("/packs") or path in {"/flashcards", "/quizzes", "/glossary", "/summaries"}:
        return "PACK"
    if path.startswith("/rag"):
        return "RAG"
    if path.startswith("/experiments") or path.startswith("/experiment-templates") or path.startswith("/experiment-runs") or path.startswith("/analytics") or path.startswith("/experiment-metrics"):
        return "EXPERIMENT_GATEWAY"
    if settings.voice_service_enabled and (path.startswith("/api/voice") or path.startswith("/voice")):
        return "VOICE"
    if path.startswith("/ai") or path.startswith("/tutor") or path.startswith("/planner") or path.startswith("/metrics/tutor") or path.startswith("/metrics/retrieval"):
        return "TUTOR"
    if path.startswith("/progress"):
        return "PROGRESS"
    if path.startswith("/quiz-sessions"):
        return "PROGRESS"
    return "REQUEST"


@app.middleware("http")
async def structured_logging(request: Request, call_next):
    if request.scope.get("type") == "websocket":
        return await call_next(request)
    started = time.perf_counter()
    tag = _log_tag(request.url.path)
    if request.url.path == "/ai/tutor":
        body = await request.body()
        logger.info("[TUTOR] RAW_REQUEST=%s", body.decode("utf-8", errors="ignore"))

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive)

    if tag == "DISCOVERY":
        logger.info("[DISCOVERY] REQUEST_RECEIVED method=%s path=%s", request.method, request.url.path)
    logger.info("[%s] REQUEST_START method=%s path=%s", tag, request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "[%s] REQUEST_ERROR method=%s path=%s duration_ms=%.2f error=%s",
            tag,
            request.method,
            request.url.path,
            duration_ms,
            exc,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "[%s] REQUEST_END method=%s path=%s status=%s duration_ms=%.2f",
        tag,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    if tag == "DISCOVERY":
        logger.info("[DISCOVERY] REQUEST_COMPLETED method=%s path=%s", request.method, request.url.path)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.error("[TUTOR] VALIDATION_ERROR=%s", exc.errors())
    return JSONResponse(
        status_code=400,
        content={"detail": exc.errors()},
    )


async def _proxy_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await app.state.http.post(f"{settings.content_pipeline_url}{path}", json=payload)
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


async def _proxy_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.content_pipeline_url}{path}", params=params)
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


async def _get_json(base_url: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = await app.state.http.get(f"{base_url}{path}", params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {str(exc)}")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


async def _post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await app.state.http.post(f"{base_url}{path}", json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {str(exc)}")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


async def _post_multipart(
    base_url: str,
    path: str,
    file: UploadFile,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = await file.read()
    files = {
        "file": (
            file.filename or "audio.wav",
            content,
            file.content_type or "application/octet-stream",
        )
    }
    try:
        response = await app.state.http.post(f"{base_url}{path}", params=params, files=files)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {str(exc)}")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=_error_detail(response.text))
    return response.json()


def _iso_from_epoch(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _pack_path(pack_id: Any, suffix: str) -> str:
    return f"/packs/{quote(str(pack_id), safe='')}/{suffix.lstrip('/')}"


def _pdf_path(*parts: Any) -> str:
    encoded = "/".join(quote(str(part), safe="") for part in parts if part is not None)
    return f"/api/v1/pdf/{encoded}" if encoded else "/api/v1/pdf"


def _pack_checksum(pack: dict[str, Any]) -> str:
    return str(pack.get("checksum") or pack.get("hash") or pack.get("content_checksum") or "")


def _normalize_pack_public_id(pack_id: Any) -> str:
    normalized = str(pack_id or "").lower()
    normalized = normalized.replace("\u2013", "_").replace("\u2014", "_")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "pack"


def _pack_id_suffix(pack: dict[str, Any]) -> str:
    checksum = _pack_checksum(pack).replace("sha256:", "")
    checksum = re.sub(r"[^a-zA-Z0-9]", "", checksum)
    if checksum:
        return checksum[:8].lower()
    return str(abs(hash(str(pack.get("pack_id") or ""))))[:8]


def _public_pack_id(raw_pack_id: Any, pack: dict[str, Any], used_ids: set[str]) -> str:
    base_id = _normalize_pack_public_id(raw_pack_id)
    public_id = base_id
    if public_id in used_ids:
        public_id = f"{base_id}_{_pack_id_suffix(pack)}"
    counter = 2
    while public_id in used_ids:
        public_id = f"{base_id}_{_pack_id_suffix(pack)}_{counter}"
        counter += 1
    used_ids.add(public_id)
    return public_id


def _canonical_pack_entry(pack: dict[str, Any], public_pack_id: str, source: str | None = None) -> dict[str, Any]:
    checksum = _pack_checksum(pack)
    compressed_size_mb = pack.get("compressed_size_mb")
    size_bytes = int(pack.get("size_bytes") or 0)
    if size_bytes <= 0 and compressed_size_mb not in {None, ""}:
        try:
            size_bytes = max(0, int(float(compressed_size_mb) * 1024 * 1024))
        except (TypeError, ValueError):
            size_bytes = 0
    artifact_counts = pack.get("artifact_counts", {}) or {}
    archive_exists = bool(pack.get("archive_exists", size_bytes > 0))
    manifest_exists = bool(pack.get("manifest_exists", bool(artifact_counts)))
    subject = _normalize_pack_subject(pack.get("subject"))
    entry = {
        "pack_id": public_pack_id,
        "version": pack.get("version", "1.0.0"),
        "grade": pack.get("grade"),
        "subject": subject,
        "chapter": pack.get("chapter"),
        "language": pack.get("language"),
        "checksum": checksum,
        "hash": checksum,
        "content_checksum": pack.get("content_checksum"),
        "size_bytes": size_bytes,
        "compressed_size_mb": float(compressed_size_mb) if compressed_size_mb not in {None, ""} else round(size_bytes / (1024 * 1024), 4) if size_bytes > 0 else 0.0,
        "artifact_counts": artifact_counts,
        "chunk_count": int(artifact_counts.get("content") or 0),
        "quality_scores": pack.get("quality_scores", {}) or {},
        "archive_exists": archive_exists,
        "manifest_exists": manifest_exists,
        "installable": archive_exists and manifest_exists and bool(checksum) and size_bytes > 0,
        "updated_at": pack.get("generated_at") or pack.get("updated_at") or pack.get("cached_at"),
        "manifest_url": _pack_path(public_pack_id, "manifest"),
        "download_url": _pack_path(public_pack_id, "download"),
    }
    if source:
        entry["source"] = source
    return entry


def _canonical_pack_entries(
    packs: list[dict[str, Any]],
    source: str,
    used_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    used = used_ids if used_ids is not None else set()
    entries: list[dict[str, Any]] = []
    source_ids: dict[str, str] = {}
    for pack in packs:
        raw_pack_id = str(pack.get("pack_id") or "")
        if not raw_pack_id:
            continue
        public_pack_id = _public_pack_id(raw_pack_id, pack, used)
        entries.append(_canonical_pack_entry(pack, public_pack_id, source))
        source_ids[public_pack_id] = raw_pack_id
    return entries, source_ids


def _curriculum_pack_key(pack: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(pack.get("grade") or "").strip(),
        _filter_value(pack.get("subject")),
        _filter_value(pack.get("chapter")),
        _filter_value(pack.get("language") or "english"),
    )


def _pack_publication_rank(pack: dict[str, Any]) -> tuple[int, int, int, int, str]:
    counts = pack.get("artifact_counts") or {}
    quality_scores = pack.get("quality_scores") or {}
    quality_passed = quality_scores.get("quality_gate_passed")
    if quality_passed is None:
        quality_passed = quality_scores.get("retrieval_precision")
    content_count = int(counts.get("content") or 0)
    score = (
        1 if pack.get("installable") else 0,
        1 if quality_passed else 0,
        content_count,
        int(pack.get("size_bytes") or 0),
        str(pack.get("updated_at") or ""),
    )
    return score


def _pack_quality_passed(pack: dict[str, Any]) -> bool:
    quality_scores = pack.get("quality_scores") or {}
    return bool(quality_scores.get("quality_gate_passed") or quality_scores.get("retrieval_precision"))


def _normalize_pack_subject(subject: Any) -> str:
    normalized = _filter_value(subject)
    if normalized in {"math", "mathematics", "maths"}:
        return "maths"
    if normalized in {"science"}:
        return "science"
    if normalized in {"social", "social science", "social_science"}:
        return "social_science"
    return normalized


def _is_certified_curriculum_pack(pack: dict[str, Any]) -> bool:
    counts = pack.get("artifact_counts") or {}
    subject = _normalize_pack_subject(pack.get("subject"))
    return (
        subject in {"maths", "science", "social_science"}
        and bool(pack.get("installable"))
        and int(counts.get("content") or 0) > 0
    )


def _dedupe_curriculum_packs(packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for pack in packs:
        if not _is_certified_curriculum_pack(pack):
            continue
        key = _curriculum_pack_key(pack)
        if not all(key):
            passthrough.append(pack)
            continue
        existing = selected.get(key)
        if existing is None or _pack_publication_rank(pack) > _pack_publication_rank(existing):
            selected[key] = pack
    return [*selected.values(), *passthrough]


async def _resolve_pack_service_pack_id(pack_id: str) -> str:
    packs = await _pack_service_packs()
    used_ids: set[str] = set()
    _, source_ids = _canonical_pack_entries(packs, "pack_service", used_ids)
    if pack_id in source_ids:
        return source_ids[pack_id]
    for pack in packs:
        raw_pack_id = str(pack.get("pack_id") or "")
        if pack_id == raw_pack_id:
            return raw_pack_id
    return pack_id


async def _canonical_pack_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    cached: list[dict[str, Any]] = []

    pack_service_packs = await _pack_service_packs()
    pack_service_entries, _ = _canonical_pack_entries(pack_service_packs, "pack_service", used_ids)
    pack_service_entries = _dedupe_curriculum_packs(pack_service_entries)
    for entry in pack_service_entries:
        records[entry["pack_id"]] = entry

    pihub_count = 0
    try:
        pihub_packs = await _get_json(settings.pihub_url, "/packs")
        pihub_entries, _ = _canonical_pack_entries(pihub_packs.get("packs", []), "pihub", used_ids)
        pihub_entries = _dedupe_curriculum_packs(pihub_entries)
        for entry in pihub_entries:
            if entry["pack_id"] not in records:
                pihub_count += 1
            records.setdefault(entry["pack_id"], entry)
        cached, _ = _canonical_pack_entries(pihub_packs.get("cached", []), "pihub_cache", used_ids)
    except HTTPException as exc:
        logger.warning("[PACK] PIHUB_PACK_LIST_UNAVAILABLE status=%s detail=%s", exc.status_code, exc.detail)

    return list(records.values()), cached, {
        "pack_service": len(pack_service_packs),
        "pihub": pihub_count,
        "pihub_cache": len(cached),
    }


def _pack_stat(pack: dict[str, Any]) -> dict[str, Any]:
    counts = pack.get("artifact_counts") or {}
    return {
        "pack_id": pack.get("pack_id"),
        "grade": pack.get("grade"),
        "subject": pack.get("subject"),
        "chapter": pack.get("chapter"),
        "language": pack.get("language"),
        "size_bytes": int(pack.get("size_bytes") or 0),
        "chunk_count": int(counts.get("content") or 0),
        "installable": bool(pack.get("installable")),
    }


def _filter_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _pack_matches_filters(
    pack: dict[str, Any],
    grade: int | None = None,
    subject: str | None = None,
    language: str | None = None,
) -> bool:
    if grade is not None:
        try:
            if int(pack.get("grade")) != grade:
                return False
        except (TypeError, ValueError):
            return False
    if subject is not None and _filter_value(pack.get("subject")) != _filter_value(subject):
        return False
    if language is not None and _filter_value(pack.get("language")) != _filter_value(language):
        return False
    return True


def _matches_topic(item: dict[str, Any], topic: str | None) -> bool:
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


def _error_detail(text: str) -> Any:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def _node_ip() -> str:
    configured_host = urlparse(settings.host_url).hostname
    if configured_host and configured_host not in {"0.0.0.0", "localhost"}:
        return configured_host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "0.0.0.0"


def _capabilities() -> dict[str, bool]:
    return {
        "rag": True,
        "sync": True,
        "assets": True,
        "streaming": True,
        "planner": True,
        "progress": True,
        "metrics": True,
        "experiments": True,
        "voice": settings.voice_service_enabled,
        "audio": settings.voice_service_enabled,
    }


def _record_retrieval_metric(metric: dict[str, Any]) -> None:
    retrieval_metrics.appendleft({
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
        **metric,
    })


def _retrieval_summary() -> dict[str, Any]:
    metrics = list(retrieval_metrics)
    if not metrics:
        return {
            "count": 0,
            "averages": {},
            "recent": [],
            "topic_aliases": topic_aliases(),
        }
    numeric_keys = ("asset_ms", "rag_ms", "total_ms", "asset_matches", "chunks_retrieved", "chunks_used")
    averages = {
        key: round(sum(float(metric.get(key, 0.0)) for metric in metrics) / len(metrics), 2)
        for key in numeric_keys
    }
    return {
        "count": len(metrics),
        "averages": averages,
        "recent": metrics[:40],
        "topic_aliases": topic_aliases(),
    }


def _discovery_payload() -> dict[str, Any]:
    return {
        "name": "PIHUB",
        "service": "PIHUB",
        "version": "1.0",
        "node_type": "hub",
        "ip": _node_ip(),
        "api_port": settings.gateway_port,
        "health": "/health",
        "discovery": "/discovery",
        "capabilities": _capabilities(),
        "supports_rag": True,
        "supports_sync": True,
        "supports_assets": True,
        "supports_voice": settings.voice_service_enabled,
    }


async def _discovery_beacon_loop() -> None:
    port = 47890
    while True:
        payload = json.dumps(_discovery_payload(), separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as beacon:
                beacon.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                beacon.sendto(payload, ("255.255.255.255", port))
        except OSError as exc:
            logger.debug("[DISCOVERY] BEACON_ERROR=%s", exc)
        await asyncio.sleep(15)


def _asset_metadata(asset_name: str, item: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
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


async def _pack_service_packs(params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        data = await _get_json(settings.pack_service_url, "/packs/search" if params else "/packs", params=params)
    except HTTPException:
        return []
    packs = data.get("packs", [])
    return packs if isinstance(packs, list) else []


async def _asset_response(asset_name: str, filters: dict[str, Any]) -> dict[str, Any]:
    topic = filters.pop("topic", None)
    filters["subject"] = normalize_subject(filters.get("subject"))
    params = {key: value for key, value in filters.items() if value is not None}
    packs = await _pack_service_packs(params)
    items: list[dict[str, Any]] = []
    for pack in packs:
        pack_id = pack.get("pack_id")
        if not pack_id:
            continue
        try:
            preview = await _get_json(settings.pack_service_url, f"/packs/{pack_id}/preview")
        except HTTPException:
            continue
        manifest = preview.get("manifest", {})
        for item in preview.get(asset_name, []) or []:
            if isinstance(item, dict) and _matches_topic(item, topic):
                metadata = _asset_metadata(asset_name, item, manifest)
                items.append({
                    "pack_id": pack_id,
                    "manifest": manifest,
                    "metadata": metadata,
                    **metadata,
                    **item,
                })
    return {"items": items, "total": len(items), "filters": {**params, "topic": topic}}


async def _chapter_knowledge_assets(filters: dict[str, Any]) -> list[dict[str, Any]]:
    params = {
        "grade": filters.get("grade"),
        "subject": normalize_subject(filters.get("subject")),
        "chapter": filters.get("chapter"),
    }
    topic = str(filters.get("topic") or "").lower()
    packs = await _pack_service_packs({key: value for key, value in params.items() if value is not None})
    assets: list[dict[str, Any]] = []
    for pack in packs[:2]:
        pack_id = pack.get("pack_id")
        if not pack_id:
            continue
        try:
            preview = await _get_json(settings.pack_service_url, f"/packs/{pack_id}/preview")
        except HTTPException:
            continue
        knowledge = preview.get("chapter_knowledge") or {}
        if not isinstance(knowledge, dict):
            continue
        snippets = _chapter_knowledge_snippets(knowledge, topic)
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


def _chapter_knowledge_snippets(knowledge: dict[str, Any], topic: str) -> list[str]:
    snippets: list[str] = []
    for key in ("concepts", "definitions", "relationships", "examples", "worked_examples", "formulas"):
        value = knowledge.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            text = _knowledge_item_text(item)
            if not text:
                continue
            if topic and topic not in text.lower():
                continue
            snippets.append(f"{key}: {text}")
    if not snippets and not topic:
        for key in ("concepts", "definitions", "examples"):
            value = knowledge.get(key)
            if isinstance(value, list):
                snippets.extend(f"{key}: {_knowledge_item_text(item)}" for item in value[:4] if _knowledge_item_text(item))
    return snippets


def _knowledge_item_text(item: Any) -> str:
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


async def _asset_search_bundle(payload: dict[str, Any], normalized_topic: str | None) -> dict[str, Any]:
    filters = {
        "grade": payload.get("grade"),
        "subject": normalize_subject(payload.get("subject")),
        "chapter": payload.get("chapter"),
        "topic": normalized_topic or payload.get("topic") or payload.get("question"),
    }
    summaries = await _asset_response("summaries", dict(filters))
    flashcards = await _asset_response("flashcards", dict(filters))
    quizzes = await _asset_response("quizzes", dict(filters))
    glossary = await _asset_response("glossary", dict(filters))
    return {
        "filters": filters,
        "summaries": summaries.get("items", [])[:3],
        "flashcards": flashcards.get("items", [])[:5],
        "quizzes": quizzes.get("items", [])[:5],
        "glossary": glossary.get("items", [])[:5],
        "chapter_knowledge": await _chapter_knowledge_assets(filters),
    }


def _asset_bundle_count(bundle: dict[str, Any]) -> int:
    return sum(len(bundle.get(key, [])) for key in ("summaries", "flashcards", "quizzes", "glossary", "chapter_knowledge"))


def _asset_context(bundle: dict[str, Any]) -> list[dict[str, Any]]:
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


async def _proxy_stream(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    response = await _send_with_retry(
        base_url,
        method,
        path,
        payload=payload,
        headers=headers,
        stream=True,
    )

    if response.is_error:
        body = await response.aread()
        body_text = body.decode("utf-8", errors="replace")
        await response.aclose()
        raise HTTPException(
            status_code=response.status_code,
            detail=_error_detail(body_text),
        )

    content_type = response.headers.get("content-type", "application/octet-stream")
    passthrough_headers = {
        key: value
        for key in ("content-length", "content-disposition", "etag", "last-modified", "accept-ranges", "content-range", "cache-control")
        if (value := response.headers.get(key)) is not None
    }
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        media_type=content_type,
        headers=passthrough_headers,
        background=BackgroundTask(response.aclose),
    )


async def _proxy_to(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    if isinstance(payload, dict) and payload.get("stream") is True:
        return await _proxy_stream(base_url, method, path, payload)

    response = await _send_with_retry(base_url, method, path, payload=payload)

    if response.is_error:
        raise HTTPException(
            status_code=response.status_code,
            detail=_error_detail(response.text)
        )

    content_type = response.headers.get("content-type", "")

    if "application/json" in content_type:
        return response.json()

    logger.error(
        "Unexpected content type: %s",
        content_type
    )

    logger.error(
        "Response body: %s",
        response.text[:2000]
    )

    raise HTTPException(
        status_code=502,
        detail=f"Unexpected response type from inference service: {content_type}"
    )


async def _send_with_retry(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    retries: int = 3,
) -> httpx.Response:
    url = f"{base_url}{path}"
    retryable = settings.voice_service_enabled and base_url == settings.voice_service_url
    delay = 0.25
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            if stream:
                request = app.state.http.build_request(method, url, json=payload, headers=headers)
                return await app.state.http.send(request, stream=True)
            return await app.state.http.request(method, url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            last_error = exc
            if not retryable or attempt >= retries:
                break
            await asyncio.sleep(delay)
            delay *= 2

    raise HTTPException(status_code=503, detail=f"Service unavailable: {last_error}")


async def _planner_tutor_response(payload: dict[str, Any], normalized_topic: str | None) -> dict[str, Any]:
    planner_payload = dict(payload)
    if normalized_topic:
        planner_payload["topic"] = normalized_topic
    lesson = await planner_lesson(planner_payload)
    summary = lesson.get("summary", {})
    answer = str(summary.get("text") or "")
    return {
        "answer": answer,
        "model": "planner",
        "context": [],
        "response_source": "planner",
        "planner": lesson,
    }


def _planner_stream(response: dict[str, Any]) -> StreamingResponse:
    async def stream() -> Any:
        yield f"data: {json.dumps({'chunk': response.get('answer', ''), 'done': True})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/discovery")
async def discovery() -> dict[str, Any]:
    return _discovery_payload()


@app.get("/discovery/beacon")
async def discovery_beacon() -> dict[str, Any]:
    return {
        "protocol": "udp-broadcast",
        "port": 47890,
        "interval_seconds": 15,
        "payload": _discovery_payload(),
    }


@app.get("/tutor/capabilities")
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


@app.get("/health")
async def health() -> dict[str, Any]:
    checks: dict[str, Any] = {"gateway": "ok"}
    status = "healthy"
    inference_ok = False
    experiment_ok = False
    voice_ok = False
    database_ok = False
    pack_count = 0
    chunk_count = 0

    try:
        content = await app.state.http.get(f"{settings.content_pipeline_url}/health")
        checks["content_pipeline"] = content.json()
        if content.is_error:
            status = "degraded"
    except Exception as exc:  # pragma: no cover - network failure path
        checks["content_pipeline"] = {"status": "error", "detail": str(exc)}
        status = "degraded"

    try:
        qdrant = await app.state.http.get(f"{settings.qdrant_url}/healthz")
        checks["qdrant"] = {"status_code": qdrant.status_code, "body": qdrant.text}
        if qdrant.is_error:
            status = "degraded"
    except Exception as exc:  # pragma: no cover - network failure path
        checks["qdrant"] = {"status": "error", "detail": str(exc)}
        status = "degraded"

    try:
        collection = await app.state.http.get(f"{settings.qdrant_url}/collections/{settings.qdrant_collection}")
        collection_body = collection.json()
        checks["qdrant_collection"] = collection_body
        if collection.is_success:
            result = collection_body.get("result", {})
            chunk_count = int(result.get("points_count") or result.get("vectors_count") or 0)
    except Exception as exc:  # pragma: no cover - network failure path
        checks["qdrant_collection"] = {"status": "error", "detail": str(exc)}

    try:
        inference = await app.state.http.get(f"{settings.inference_service_url}/ai/health")
        checks["inference_service"] = inference.json()
        inference_ok = inference.is_success
        if inference.is_error:
            status = "degraded"
    except Exception as exc:  # pragma: no cover - network failure path
        checks["inference_service"] = {"status": "error", "detail": str(exc)}
        status = "degraded"

    try:
        experiment_health = await app.state.experiment_client.health()
        checks["experiment_service"] = experiment_health
        experiment_ok = bool(experiment_health.get("healthy"))
        if not experiment_ok and settings.experiment_service_required:
            status = "degraded"
    except Exception as exc:  # pragma: no cover - defensive path
        checks["experiment_service"] = {"healthy": False, "error": str(exc)}
        if settings.experiment_service_required:
            status = "degraded"

    if settings.voice_service_enabled:
        try:
            voice = await app.state.http.get(f"{settings.voice_service_url}/health")
            checks["voice_service"] = voice.json()
            voice_ok = voice.is_success
            if voice.is_error and settings.voice_service_required:
                status = "degraded"
        except Exception as exc:  # pragma: no cover - network failure path
            checks["voice_service"] = {"healthy": False, "error": str(exc)}
            if settings.voice_service_required:
                status = "degraded"
    else:
        checks["voice_service"] = {"healthy": True, "disabled": True}

    try:
        pihub = await app.state.http.get(f"{settings.pihub_url}/health")
        checks["pihub"] = pihub.json()
        database_ok = pihub.is_success
        if pihub.is_error:
            status = "degraded"
    except Exception as exc:  # pragma: no cover - network failure path
        checks["pihub"] = {"status": "error", "detail": str(exc)}
        status = "degraded"

    try:
        packs = await app.state.http.get(f"{settings.pihub_url}/packs")
        if packs.is_success:
            body = packs.json()
            pack_count = len(body.get("packs", [])) + len(body.get("cached", []))
    except Exception as exc:  # pragma: no cover - network failure path
        checks["packs"] = {"status": "error", "detail": str(exc)}

    try:
        pack_service = await app.state.http.get(f"{settings.pack_service_url}/packs")
        if pack_service.is_success:
            body = pack_service.json()
            pack_count = max(pack_count, len(body.get("packs", [])))
            checks["pack_service"] = {"status": "ok", "pack_count": len(body.get("packs", []))}
        elif pack_service.is_error:
            checks["pack_service"] = {"status": "degraded", "status_code": pack_service.status_code}
    except Exception as exc:  # pragma: no cover - network failure path
        checks["pack_service"] = {"status": "error", "detail": str(exc)}

    return {
        "status": status,
        "version": "1.0",
        "service": "gateway",
        "inference_service": inference_ok,
        "experiment_service": {"healthy": experiment_ok},
        "voice_service": (
            {"healthy": True, "disabled": True}
            if not settings.voice_service_enabled
            else {"healthy": voice_ok}
        ),
        "database": database_ok,
        "pack_count": pack_count,
        "chunk_count": chunk_count,
        "uptime_seconds": int(time.time() - getattr(app.state, "started_at", time.time())),
        "checks": checks,
    }


@app.post("/content/upload", response_model=IngestResponse)
@app.post("/upload", response_model=IngestResponse)
async def upload_content(
    file: UploadFile = File(...),
    grade: int | None = Form(default=None),
    subject: str | None = Form(default=None),
    chapter: str | None = Form(default=None),
    topic: str | None = Form(default=None),
    language: str | None = Form(default=None),
) -> dict[str, Any]:
    metadata = Metadata(grade=grade, subject=subject, chapter=chapter, topic=topic, language=language)
    content = await file.read()
    response = await app.state.http.post(
        f"{settings.content_pipeline_url}/ingest/pdf",
        data={"metadata": metadata.model_dump_json()},
        files={"file": (file.filename or "upload.pdf", content, file.content_type or "application/pdf")},
    )
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/ingest/pdf", response_model=IngestResponse)
async def ingest_pdf(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default=None),
) -> dict[str, Any]:
    content = await file.read()
    response = await app.state.http.post(
        f"{settings.content_pipeline_url}/ingest/pdf",
        data={"metadata": metadata, "source": source},
        files={"file": (file.filename or "upload.pdf", content, file.content_type or "application/pdf")},
    )
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/ingest/textbook", response_model=IngestResponse)
async def ingest_textbook(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default="textbook"),
) -> dict[str, Any]:
    content = await file.read()
    response = await app.state.http.post(
        f"{settings.content_pipeline_url}/ingest/textbook",
        data={"metadata": metadata, "source": source or "textbook"},
        files={"file": (file.filename or "textbook.pdf", content, file.content_type or "application/pdf")},
    )
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/ingest/generated-pack")
async def ingest_generated_pack(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_json("/ingest/generated-pack", payload)


@app.post("/admin/reset-rag")
async def reset_rag() -> dict[str, Any]:
    return await _proxy_json("/admin/reset-rag", {})


@app.post("/rag/search", response_model=SearchResponse)
async def rag_search(request: SearchRequest) -> dict[str, Any]:
    return await _proxy_json("/rag/search", request.model_dump())


@app.post("/rag/curriculum_search")
async def rag_curriculum_search(request: SearchRequest) -> dict[str, Any]:
    return await _proxy_json("/rag/curriculum_search", request.model_dump())


@app.get("/rag/chapter", response_model=SearchResponse)
async def rag_chapter(chapter: str, limit: int = 5) -> dict[str, Any]:
    return await _proxy_get("/rag/chapter", {"chapter": chapter, "limit": limit})


@app.get("/rag/subject", response_model=SearchResponse)
async def rag_subject(subject: str, limit: int = 5) -> dict[str, Any]:
    return await _proxy_get("/rag/subject", {"subject": subject, "limit": limit})


@app.post("/ai/chat")
async def ai_chat(payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.inference_service_url, "POST", "/ai/chat", payload)


async def _query_ollama(query: str, topic: str | None, grade: int = 9) -> str | None:
    t_clean = (topic or "General Science & Mathematics").strip()
    sys_prompt = (
        f"You are an expert NCERT school AI tutor for Class {grade} {t_clean}.\n"
        f"Answer the student's question directly, accurately, and clearly using simple school-level concepts.\n"
        f"Use headers (e.g. ###), bold text, bullet points, and key formulas where applicable.\n"
        f"Do not output internal thinking monologues or <think> tags."
    )
    user_prompt = f"Student Question: {query}\nChapter/Topic: {t_clean}\nGrade: Class {grade}"
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "http://127.0.0.1:11434/api/chat",
                json={
                    "model": "gemma4:latest",
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 250,
                    }
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message", {})
                content = (msg.get("content") or "").strip()
                if content:
                    return content
                # Fallback: if content is empty due to reasoning monologue, check thinking field
                thinking = (msg.get("thinking") or "").strip()
                if thinking:
                    cleaned = re.sub(r"(?i)^here'?s a thinking process.*?:", "", thinking).strip()
                    if cleaned:
                        return cleaned
    except Exception as exc:
        logger.error("[OLLAMA] Local Ollama call failed: %s", exc, exc_info=True)
    return None

def generate_socratic_response(query: str, topic: str | None, grade: int = 9) -> dict[str, Any]:
    q_clean = query.strip()
    t_clean = (topic or "General Science & Mathematics").strip()

    t_title = t_clean.title()
    answer = (
        f"Hello! 👋 I am your NCERT Class {grade} Socratic AI Tutor for **{t_title}**.\n\n"
        f"How can I help you master **{t_title}** today? Ask me any question or concept from this chapter to begin!"
    )
    return {
        "answer": answer,
        "formulas": [],
        "hasAudio": True,
        "explainOptions": ["Explain Core Concepts", "Show Example Problems", "Real-World Applications"],
        "source": "live_generative_socratic_greeting",
    }


@app.post("/ai/tutor")
async def ai_tutor(payload: dict[str, Any]) -> Any:
    started = time.perf_counter()
    query = str(payload.get("question") or payload.get("query") or "").strip()
    normalization = normalize_topic(payload.get("topic"), query)
    normalized_topic = normalization.normalized_topic
    grade = payload.get("grade") or 9
    intent = payload.get("intent")

    if should_use_planner(intent, query):
        response = await _planner_tutor_response(payload, normalized_topic)
        total_ms = (time.perf_counter() - started) * 1000
        _record_retrieval_metric({
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
        if payload.get("stream") is True:
            return _planner_stream(response)
        return response

    # 1. Check standalone greeting
    if re.match(r"^(hi|hello|hey|hii+|good\s*(morning|afternoon|evening)|how are you)[\s!.?]*$", query, re.IGNORECASE):
        return generate_socratic_response(query, normalized_topic or payload.get("topic"), grade)

    # 2. Query Local Ollama LLM (gemma4)
    ollama_text = await _query_ollama(query, normalized_topic or payload.get("topic"), grade)
    if ollama_text:
        total_ms = (time.perf_counter() - started) * 1000
        logger.info("[TUTOR] Ollama LLM response generated in %.2f ms", total_ms)
        return {
            "answer": ollama_text,
            "formulas": [f"NCERT Class {grade} {normalized_topic or payload.get('topic') or 'Core'} Principles"],
            "hasAudio": True,
            "explainOptions": ["Real-World Analogy", "Step-by-Step Math", "Simpler Language", "Visual Simulation"],
            "source": "ollama_gemma4_llm",
        }

    # 3. Try inference-service proxy
    try:
        enriched_payload = dict(payload)
        if normalized_topic:
            enriched_payload["topic"] = normalized_topic
        result = await _proxy_to(settings.inference_service_url, "POST", "/ai/tutor", enriched_payload)
        if isinstance(result, dict) and result.get("answer"):
            return result
        if isinstance(result, StreamingResponse):
            return result
    except Exception as exc:
        logger.warning("[TUTOR] Inference service proxy failed (%s).", exc)

    # 4. Explicit Timeout / Failure Error - NEVER return fake template
    raise HTTPException(
        status_code=504,
        detail="Backend LLM timed out or is unavailable. Please load a local .gguf model or retry."
    )


@app.post("/ai/tutor/debug")
async def ai_tutor_debug(payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.inference_service_url, "POST", "/ai/tutor/debug", payload)


@app.post("/ai/tutor/evaluate")
async def ai_tutor_evaluate(payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.inference_service_url, "POST", "/ai/tutor/evaluate", payload)


@app.get("/ai/health")
async def ai_health() -> dict[str, Any]:
    return await _proxy_to(settings.inference_service_url, "GET", "/ai/health")


@app.post("/voice/query")
@app.post("/api/voice/query")
async def voice_query(payload: dict[str, Any]) -> Any:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")
    return await _proxy_to(settings.voice_service_url, "POST", "/voice/query", payload)


@app.post("/voice/tts")
@app.post("/api/voice/tts")
async def voice_tts(payload: dict[str, Any]) -> Any:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")
    return await _proxy_to(settings.voice_service_url, "POST", "/voice/tts", payload)


@app.post("/voice/stt")
@app.post("/api/voice/stt")
async def voice_stt(
    file: UploadFile = File(...),
    language: str | None = Query(default=None),
    enable_partial_transcripts: bool = Query(default=False),
) -> dict[str, Any]:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")
    return await _post_multipart(
        settings.voice_service_url,
        "/voice/stt",
        file,
        {
            "language": language,
            "enable_partial_transcripts": enable_partial_transcripts,
        },
    )


@app.get("/voice/audio/{asset_id:path}")
@app.get("/api/voice/audio/{asset_id:path}")
async def voice_audio(asset_id: str, request: Request) -> StreamingResponse:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    return await _proxy_stream(
        settings.voice_service_url,
        "GET",
        f"/voice/audio/{quote(asset_id, safe='')}",
        headers=headers,
    )


@app.get("/voice/metrics")
@app.get("/api/voice/metrics")
async def voice_metrics() -> dict[str, Any]:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")
    return await _get_json(settings.voice_service_url, "/voice/metrics")


import websockets

@app.websocket("/voice/stream")
@app.websocket("/api/voice/stream")
@app.websocket("/api/v1/voice/stream")
async def voice_stream_proxy(websocket: WebSocket) -> None:
    await websocket.accept()
    if not settings.voice_service_enabled:
        await websocket.send_json({"type": "error", "message": "Voice service disabled"})
        await websocket.close()
        return
    target_url = settings.voice_service_url.replace("http://", "ws://").replace("https://", "wss://") + "/voice/stream"
    logger.info(f"[GATEWAY] Proxying WebSocket connection to: {target_url}")
    
    session_id = "unknown"
    start_time = time.time()
    
    try:
        async with websockets.connect(target_url) as voice_ws:
            async def client_to_service():
                nonlocal session_id
                try:
                    while True:
                        msg = await websocket.receive_text()
                        try:
                            data = json.loads(msg)
                            if data.get("type") == "audio_start":
                                session_id = data.get("session_id") or "unknown"
                                logger.info(f"[GATEWAY] Session ID identified: {session_id}")
                        except Exception:
                            pass
                        await voice_ws.send(msg)
                except WebSocketDisconnect:
                    logger.info(f"[GATEWAY] Client disconnected from session {session_id}")
                except Exception as e:
                    logger.error(f"[GATEWAY] Error forwarding client to service: {e}")

            async def service_to_client():
                try:
                    while True:
                        msg = await voice_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as e:
                    logger.debug(f"[GATEWAY] Voice service connection closed or errored: {e}")

            await asyncio.gather(client_to_service(), service_to_client())
    except Exception as e:
        logger.error(f"[GATEWAY] Fail to connect to voice service: {e}")
        try:
            await websocket.send_json({"type": "error", "message": f"Voice service unavailable: {e}"})
            await websocket.close()
        except Exception:
            pass


@app.get("/sync")
async def sync_get() -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.pihub_url}/sync")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/sync")
async def sync_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_to(settings.pihub_url, "POST", "/sync", payload)


@app.get("/packs")
async def packs_get() -> dict[str, Any]:
    packs, cached, sources = await _canonical_pack_records()
    return {
        "packs": packs,
        "cached": cached,
        "total_count": len(packs) + len(cached),
        "sources": sources,
    }


@app.get("/packs/sync")
async def packs_sync(
    known_hashes: str | None = Query(default=None),
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    language: str | None = Query(default=None),
) -> dict[str, Any]:
    client_hashes: dict[str, str] = {}
    if known_hashes:
        try:
            decoded_hashes = json.loads(known_hashes)
            if isinstance(decoded_hashes, dict):
                client_hashes = {str(key): str(value) for key, value in decoded_hashes.items()}
        except json.JSONDecodeError:
            logger.warning("[SYNC] INVALID_KNOWN_HASHES=%s", known_hashes[:500])

    all_packs, _, _ = await _canonical_pack_records()
    if not all_packs:
        fallback_packs = await _pack_service_packs()
        if fallback_packs:
            all_packs, _ = _canonical_pack_entries(fallback_packs, "pack_service")
            all_packs = _dedupe_curriculum_packs(all_packs)
    all_records = {str(pack["pack_id"]): pack for pack in all_packs}
    packs = [
        pack
        for pack in all_packs
        if _pack_matches_filters(pack, grade=grade, subject=subject, language=language)
    ]
    if not packs and all_packs:
        packs = all_packs
    records = {str(pack["pack_id"]): pack for pack in packs}
    changed_packs = [
        pack
        for pack in packs
        if not client_hashes or client_hashes.get(str(pack["pack_id"])) != str(pack.get("hash", ""))
    ]
    deleted_packs = [
        {"pack_id": pack_id}
        for pack_id in client_hashes
        if pack_id not in all_records
    ]
    asset_updates = [
        {
            "pack_id": pack["pack_id"],
            "version": pack.get("version", "1.0.0"),
            "hash": pack.get("hash", ""),
            "checksum": pack.get("checksum", ""),
            "size_bytes": pack.get("size_bytes", 0),
            "compressed_size_mb": pack.get("compressed_size_mb", 0.0),
            "manifest_url": pack.get("manifest_url"),
            "download_url": pack.get("download_url"),
            "artifact_counts": pack.get("artifact_counts", {}),
            "assets": pack.get("artifact_counts", {}),
            "installable": pack.get("installable", False),
            "archive_exists": pack.get("archive_exists", False),
            "manifest_exists": pack.get("manifest_exists", False),
        }
        for pack in changed_packs
    ]

    return {
        "version": "1.0",
        "server_version": "1.0",
        "count": len(packs),
        "filters": {
            "grade": grade,
            "subject": subject,
            "language": language,
        },
        "packs": packs,
        "changed_packs": changed_packs,
        "deleted_packs": deleted_packs,
        "asset_updates": asset_updates,
    }


@app.get("/packs/catalog")
async def packs_catalog() -> dict[str, Any]:
    packs, _, _ = await _canonical_pack_records()
    grouped: dict[int, dict[str, Any]] = {}
    for pack in packs:
        try:
            grade = int(pack.get("grade"))
        except (TypeError, ValueError):
            continue
        subject = str(pack.get("subject") or "unknown")
        grade_group = grouped.setdefault(grade, {"grade": grade, "subjects": {}})
        subject_group = grade_group["subjects"].setdefault(
            subject,
            {
                "subject": subject,
                "pack_count": 0,
                "chunk_count": 0,
                "download_size_bytes": 0,
                "chapters": set(),
            },
        )
        stat = _pack_stat(pack)
        subject_group["pack_count"] += 1
        subject_group["chunk_count"] += stat["chunk_count"]
        subject_group["download_size_bytes"] += stat["size_bytes"]
        if pack.get("chapter"):
            subject_group["chapters"].add(str(pack.get("chapter")))

    grades: list[dict[str, Any]] = []
    for grade in sorted(grouped):
        subjects: list[dict[str, Any]] = []
        for subject_group in sorted(grouped[grade]["subjects"].values(), key=lambda item: item["subject"]):
            size_bytes = int(subject_group["download_size_bytes"])
            subjects.append({
                "subject": subject_group["subject"],
                "pack_count": subject_group["pack_count"],
                "chapter_count": len(subject_group["chapters"]),
                "chunk_count": subject_group["chunk_count"],
                "download_size_bytes": size_bytes,
                "download_size_mb": round(size_bytes / (1024 * 1024), 2),
            })
        grades.append({"grade": grade, "subjects": subjects})

    total_size = sum(int(pack.get("size_bytes") or 0) for pack in packs)
    return {
        "grades": grades,
        "total_packs": len(packs),
        "total_chunks": sum(int((pack.get("artifact_counts") or {}).get("content") or 0) for pack in packs),
        "total_download_size_bytes": total_size,
        "total_download_size_mb": round(total_size / (1024 * 1024), 2),
    }


@app.get("/packs/coverage")
async def packs_coverage() -> dict[str, Any]:
    return await _get_json(settings.pack_service_url, "/packs/coverage")


@app.get("/packs/multilingual/plan")
async def packs_multilingual_plan(
    target_language: str = Query(default="hi"),
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _get_json(
        settings.pack_service_url,
        "/packs/multilingual/plan",
        params={"target_language": target_language, "grade": grade, "subject": subject},
    )


@app.get("/packs/recommended")
async def packs_recommended(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    language: str | None = Query(default=None),
    installed_pack_ids: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    installed = {
        item.strip()
        for item in (installed_pack_ids or "").split(",")
        if item.strip()
    }
    packs, _, _ = await _canonical_pack_records()
    candidates = [
        pack
        for pack in packs
        if pack.get("installable")
        and str(pack.get("pack_id")) not in installed
        and _pack_matches_filters(pack, grade=grade, subject=subject, language=language)
    ]
    candidates.sort(key=lambda pack: (
        int(pack.get("grade") or 0),
        str(pack.get("subject") or ""),
        str(pack.get("chapter") or ""),
        str(pack.get("pack_id") or ""),
    ))
    recommended = candidates[:limit]
    return {
        "count": len(recommended),
        "total_candidates": len(candidates),
        "filters": {
            "grade": grade,
            "subject": subject,
            "language": language,
            "installed_pack_ids": sorted(installed),
        },
        "packs": recommended,
        "pack_stats": [_pack_stat(pack) for pack in recommended],
    }


@app.post("/packs/generate")
async def packs_generate(payload: dict[str, Any]) -> Any:
    return await _post_json(settings.pack_service_url, "/packs/generate", payload)


@app.get("/packs/{pack_id}/manifest")
async def pack_manifest(pack_id: str) -> dict[str, Any]:
    try:
        source_pack_id = await _resolve_pack_service_pack_id(pack_id)
        manifest = await _get_json(settings.pack_service_url, _pack_path(source_pack_id, "manifest"))
        counts = manifest.get("artifact_counts", {})
        return {
            "pack_id": pack_id,
            "version": manifest.get("version", "1.0.0"),
            "metadata": {
                "grade": manifest.get("grade"),
                "subject": manifest.get("subject"),
                "chapter": manifest.get("chapter"),
                "language": manifest.get("language"),
                "generated_at": manifest.get("generated_at"),
                "checksum": manifest.get("checksum"),
                "content_checksum": manifest.get("content_checksum"),
                "quality_scores": manifest.get("quality_scores", {}),
                "generation_metadata": manifest.get("generation_metadata", {}),
            },
            "chunk_count": int(counts.get("content", 0)),
            "flashcard_count": int(counts.get("flashcards", 0)),
            "quiz_count": int(counts.get("quizzes", 0)),
            "glossary_count": int(counts.get("glossary", 0)),
            "summary_count": int(counts.get("summaries", 0)),
            "artifact_counts": counts,
        }
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    pack = await _get_json(settings.pihub_url, f"/packs/{pack_id}")
    return {
        "pack_id": pack.get("pack_id", pack_id),
        "version": pack.get("version", "1.0.0"),
        "metadata": {
            "pack_name": pack.get("pack_name"),
            "grade": pack.get("grade"),
            "subject": pack.get("subject"),
            "chapter": pack.get("chapter"),
            "checksum": pack.get("checksum"),
            "size_bytes": pack.get("size_bytes"),
            "updated_at": _iso_from_epoch(pack.get("updated_at")),
            **(pack.get("metadata") or {}),
        },
        "chunk_count": 0,
        "flashcard_count": 0,
        "quiz_count": 0,
        "glossary_count": 0,
        "summary_count": 0,
        "artifact_counts": {},
    }


@app.get("/packs/{pack_id}/download")
async def pack_download(pack_id: str) -> StreamingResponse:
    try:
        source_pack_id = await _resolve_pack_service_pack_id(pack_id)
        return await _proxy_stream(settings.pack_service_url, "GET", _pack_path(source_pack_id, "download"))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    return await _proxy_stream(settings.pihub_url, "GET", _pack_path(pack_id, "download"))


@app.get("/api/v1/pdf/catalog")
async def pdf_catalog() -> Any:
    return await _get_json(settings.pack_service_url, "/api/v1/pdf/catalog")


@app.get("/api/v1/pdf/resolve")
async def pdf_resolve(
    grade: int = Query(...),
    subject: str = Query(...),
    chapter: str = Query(...),
    language: str = Query(default="english"),
) -> Any:
    return await _get_json(
        settings.pack_service_url,
        "/api/v1/pdf/resolve",
        params={"grade": grade, "subject": subject, "chapter": chapter, "language": language},
    )


@app.get("/api/v1/pdf/book/{grade}/{subject}")
async def pdf_book(grade: int, subject: str, language: str = Query(default="english")) -> Any:
    return await _get_json(
        settings.pack_service_url,
        _pdf_path("book", grade, subject),
        params={"language": language},
    )


@app.get("/api/v1/pdf/chapter/{chapter_id}/metadata")
async def pdf_chapter_metadata(chapter_id: str) -> Any:
    return await _get_json(settings.pack_service_url, _pdf_path("chapter", chapter_id, "metadata"))


@app.get("/api/v1/pdf/chapter/{chapter_id}")
async def pdf_chapter(chapter_id: str) -> Any:
    return await _get_json(settings.pack_service_url, _pdf_path("chapter", chapter_id))


@app.get("/api/v1/pdf/file/{book_id}")
async def pdf_file(book_id: str) -> StreamingResponse:
    return await _proxy_stream(settings.pack_service_url, "GET", _pdf_path("file", book_id))


def _query_params(**values: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        params[key] = value
    return params


@app.get("/experiments")
async def experiments_get(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    required_sensors: list[str] = Query(default=[]),
    execution_modes: list[str] = Query(default=[]),
    tags: list[str] = Query(default=[]),
) -> Any:
    params = _query_params(
        grade=grade,
        subject=subject,
        chapter=chapter,
        topic=topic,
        difficulty=difficulty,
        required_sensors=required_sensors,
        execution_modes=execution_modes,
        tags=tags,
    )
    return await app.state.experiment_client.get_experiments(params)


@app.get("/experiments/catalog")
async def experiments_catalog() -> Any:
    return await app.state.experiment_client.get_catalog()


@app.get("/simulations/{path:path}")
async def proxy_simulations(path: str) -> StreamingResponse:
    return await _proxy_stream(settings.experiment_service_url, "GET", f"/simulations/{path}")


@app.get("/experiments/search")
async def experiments_search(
    q: str | None = Query(default=None),
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    required_sensors: list[str] = Query(default=[]),
    execution_modes: list[str] = Query(default=[]),
    tags: list[str] = Query(default=[]),
) -> Any:
    params = _query_params(
        q=q,
        grade=grade,
        subject=subject,
        chapter=chapter,
        topic=topic,
        difficulty=difficulty,
        required_sensors=required_sensors,
        execution_modes=execution_modes,
        tags=tags,
    )
    return await app.state.experiment_client.search_experiments(params)


@app.get("/experiments/{experiment_id}/download")
async def experiment_pack_download(experiment_id: str) -> StreamingResponse:
    return await _proxy_stream(settings.experiment_service_url, "GET", f"/experiments/{quote(experiment_id, safe='')}/download")


@app.get("/experiments/{experiment_id}/certification")
async def experiment_certification(experiment_id: str) -> Any:
    return await app.state.experiment_client.get_certification(experiment_id)


@app.get("/experiments/{experiment_id}")
async def experiment_get(experiment_id: str) -> Any:
    return await app.state.experiment_client.get_experiment(experiment_id)


@app.get("/chapters/{chapter_id}/experiments")
async def chapter_experiments(chapter_id: str) -> Any:
    return await app.state.experiment_client.get_chapter_experiments(chapter_id)


@app.post("/classroom/sessions")
async def classroom_session_create(payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.experiment_service_url, "POST", "/classroom/sessions", payload)


@app.get("/classroom/sessions")
async def classroom_sessions(page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)) -> Any:
    return await _get_json(settings.experiment_service_url, "/classroom/sessions", params={"page": page, "page_size": page_size})


@app.post("/classroom/sessions/{session_id}/assignments")
async def classroom_assignment_create(session_id: str, payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.experiment_service_url, "POST", f"/classroom/sessions/{quote(session_id, safe='')}/assignments", payload)


@app.get("/classroom/sessions/{session_id}/assignments")
async def classroom_assignments(
    session_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Any:
    return await _get_json(
        settings.experiment_service_url,
        f"/classroom/sessions/{quote(session_id, safe='')}/assignments",
        params={"page": page, "page_size": page_size},
    )


@app.post("/classroom/assignments/{assignment_id}/submit")
async def classroom_assignment_submit(assignment_id: str, payload: dict[str, Any]) -> Any:
    return await _proxy_to(settings.experiment_service_url, "POST", f"/classroom/assignments/{quote(assignment_id, safe='')}/submit", payload)


@app.get("/classroom/assignments/{assignment_id}/submissions")
async def classroom_assignment_submissions(
    assignment_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Any:
    return await _get_json(
        settings.experiment_service_url,
        f"/classroom/assignments/{quote(assignment_id, safe='')}/submissions",
        params={"page": page, "page_size": page_size},
    )


@app.get("/classroom/analytics")
async def classroom_session_analytics() -> Any:
    return await _get_json(settings.experiment_service_url, "/classroom/analytics")


@app.get("/experiment-templates")
async def experiment_templates() -> Any:
    return await app.state.experiment_client.get_templates()


@app.post("/experiment-runs")
async def experiment_run_create(payload: dict[str, Any]) -> Any:
    return await app.state.experiment_client.create_run(payload)


@app.get("/experiment-runs/student/{student_id}")
async def experiment_runs_student(student_id: str) -> Any:
    return await app.state.experiment_client.get_student_runs(student_id)


@app.get("/experiment-runs/{run_id}")
async def experiment_run_get(run_id: str) -> Any:
    return await app.state.experiment_client.get_run(run_id)


@app.post("/experiment-runs/{run_id}/events")
async def experiment_run_event(run_id: str, payload: dict[str, Any]) -> Any:
    return await app.state.experiment_client.append_event(run_id, payload)


@app.post("/experiment-runs/{run_id}/complete")
async def experiment_run_complete(run_id: str, payload: dict[str, Any]) -> Any:
    return await app.state.experiment_client.complete_run(run_id, payload)


@app.get("/analytics/student/{student_id}")
async def experiment_student_analytics(student_id: str) -> Any:
    return await app.state.experiment_client.get_student_analytics(student_id)


@app.get("/analytics/experiment/{experiment_id}")
async def experiment_analytics(experiment_id: str) -> Any:
    return await app.state.experiment_client.get_experiment_analytics(experiment_id)


@app.get("/analytics/system")
async def experiment_system_analytics() -> Any:
    return await app.state.experiment_client.get_system_analytics()


@app.get("/analytics/top-experiments")
async def experiment_top_analytics(limit: int = Query(default=10, ge=1, le=100)) -> Any:
    return await app.state.experiment_client.get_top_experiments({"limit": limit})


@app.get("/experiment-metrics")
async def experiment_metrics() -> Any:
    service_metrics = await app.state.experiment_client.get_metrics()
    return {
        "service": service_metrics,
        "gateway": experiment_gateway_metrics.snapshot(),
    }


@app.get("/flashcards")
async def flashcards(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _asset_response("flashcards", {"grade": grade, "subject": subject, "chapter": chapter, "topic": topic})


@app.get("/quizzes")
async def quizzes(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _asset_response("quizzes", {"grade": grade, "subject": subject, "chapter": chapter, "topic": topic})


@app.get("/glossary")
async def glossary(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _asset_response("glossary", {"grade": grade, "subject": subject, "chapter": chapter, "topic": topic})


@app.get("/summaries")
async def summaries(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _asset_response("summaries", {"grade": grade, "subject": subject, "chapter": chapter, "topic": topic})


@app.post("/planner/lesson")
async def planner_lesson(payload: dict[str, Any]) -> dict[str, Any]:
    question = str(payload.get("question") or payload.get("topic") or payload.get("title") or "").strip()
    filters = {
        "grade": payload.get("grade"),
        "subject": normalize_subject(payload.get("subject")),
        "chapter": payload.get("chapter"),
        "topic": normalize_topic(payload.get("topic"), question).normalized_topic or question,
    }

    summary_assets = await _asset_response("summaries", dict(filters))
    flashcard_assets = await _asset_response("flashcards", dict(filters))
    quiz_assets = await _asset_response("quizzes", dict(filters))

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


@app.get("/demo/topics")
async def demo_topics() -> dict[str, Any]:
    return {"topics": DEMO_TOPICS}


@app.get("/demo")
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


@app.post("/demo/tutor")
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
    return await ai_tutor(demo_payload)


@app.get("/metrics/tutor")
async def tutor_metrics() -> dict[str, Any]:
    return await _get_json(settings.inference_service_url, "/metrics/tutor")


@app.get("/metrics/retrieval")
async def retrieval_metrics_endpoint() -> dict[str, Any]:
    inference = {}
    try:
        inference = await _get_json(settings.inference_service_url, "/metrics/retrieval")
    except HTTPException as exc:
        inference = {"status": "unavailable", "detail": exc.detail}
    return {
        "gateway": _retrieval_summary(),
        "inference": inference,
    }


@app.post("/progress")
async def progress_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post_json(settings.pihub_url, "/progress", payload)


@app.get("/progress/{student_id}")
async def progress_get(student_id: str) -> dict[str, Any]:
    return await _get_json(settings.pihub_url, f"/progress/{student_id}")


@app.post("/quiz-sessions")
async def quiz_session_create(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post_json(settings.pihub_url, "/quiz-sessions", payload)


@app.get("/quiz-sessions/student/{student_id}")
async def quiz_sessions_student(student_id: str, active_only: bool = Query(default=False)) -> dict[str, Any]:
    return await _get_json(settings.pihub_url, f"/quiz-sessions/student/{student_id}", {"active_only": active_only})


@app.get("/quiz-sessions/{quiz_session_id}")
async def quiz_session_get(quiz_session_id: str) -> dict[str, Any]:
    return await _get_json(settings.pihub_url, f"/quiz-sessions/{quiz_session_id}")


@app.post("/quiz-sessions/{quiz_session_id}/answer")
async def quiz_session_answer(quiz_session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await _post_json(settings.pihub_url, f"/quiz-sessions/{quiz_session_id}/answer", payload)


@app.get("/classroom")
async def classroom_get() -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.pihub_url}/classroom")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/classroom")
async def classroom_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_to(settings.pihub_url, "POST", "/classroom", payload)


@app.post("/ingest/directory")
async def ingest_directory(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_to(settings.content_pipeline_url, "POST", "/ingest/directory", payload)


@app.get("/devices")
async def devices_get() -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.pihub_url}/devices")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/devices")
async def devices_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_to(settings.pihub_url, "POST", "/devices", payload)


@app.get("/debug/curriculum")
async def debug_curriculum() -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.content_pipeline_url}/debug/curriculum")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/debug/curriculum-relations")
async def debug_curriculum_relations() -> dict[str, Any]:
    response = await app.state.http.get(f"{settings.content_pipeline_url}/debug/curriculum-relations")
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/debug/metadata")
async def debug_metadata(path: str) -> dict[str, Any]:
    return await _proxy_get("/debug/metadata", {"path": path})


@app.get("/debug/chunks")
async def debug_chunks(path: str) -> dict[str, Any]:
    return await _proxy_get("/debug/chunks", {"path": path})


@app.post("/debug/retrieval")
async def debug_retrieval(payload: dict[str, Any]) -> dict[str, Any]:
    return await _proxy_json("/debug/retrieval", payload)


@app.get("/debug/similarity")
async def debug_similarity(left: str, right: str) -> dict[str, Any]:
    return await _proxy_get("/debug/similarity", {"left": left, "right": right})


@app.get("/debug/pack-preview")
async def debug_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    return await _proxy_get("/debug/pack-preview", {"path": path, "pack_name": pack_name})


@app.get("/debug/learning-pack-preview")
async def debug_learning_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    return await _proxy_get("/debug/learning-pack-preview", {"path": path, "pack_name": pack_name})


# --- MENTORA STITCH DYNAMIC BACKEND CATALOG & FEATURE ENDPOINTS ---

CHAPTERS_BY_GRADE_SUBJECT: dict[tuple[int, str], list[dict[str, Any]]] = {
    # Grade 5
    (5, "Science"): [
        {"number": 1, "title": "Super Senses", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Sense organs", "Animal senses"]},
        {"number": 2, "title": "A Snake Charmer's Story", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Poisonous snakes", "Wildlife protection"]},
        {"number": 3, "title": "From Tasting to Digesting", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Taste buds", "Digestive system"]},
        {"number": 4, "title": "Mangoes Round the Year", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Food preservation", "Spoilage"]},
        {"number": 5, "title": "Seeds and Seeds", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Germination", "Dispersal of seeds"]},
        {"number": 6, "title": "Every Drop Counts", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Water conservation", "Lakes and wells"]},
        {"number": 7, "title": "Experiments with Water", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Floating and sinking", "Solubility"]},
        {"number": 8, "title": "A Treat for Mosquitoes", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Malaria and Dengue", "Prevention"]},
        {"number": 9, "title": "Up You Go!", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Mountaineering", "Leadership and team work"]},
        {"number": 10, "title": "Walls Tell Stories", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Golconda Fort", "Historical monuments"]},
        {"number": 11, "title": "Sunita in Space", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Gravity", "Space travel"]},
        {"number": 12, "title": "What if it Finishes...?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Fuels", "Petroleum conservation"]},
        {"number": 13, "title": "A Shelter so High!", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Leh and Ladakh", "Cold deserts"]},
        {"number": 14, "title": "When the Earth Shook!", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Earthquakes", "Disaster management"]},
        {"number": 15, "title": "Blow Hot, Blow Cold", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Breathing", "Air flow and temperature"]},
        {"number": 16, "title": "Who will do this Work?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Cleanliness", "Social equality"]},
        {"number": 17, "title": "Across the Wall", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Gender equality", "Sportsmanship"]},
        {"number": 18, "title": "No Place for Us?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Displacement", "Dams and migration"]},
        {"number": 19, "title": "A Seed tells a Farmer's Story", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Agriculture", "Traditional vs Modern farming"]},
        {"number": 20, "title": "Whose Forests?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Forest conservation", "Tribal life"]},
        {"number": 21, "title": "Like Father, Like Daughter", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Heredity traits", "Family characteristics"]},
        {"number": 22, "title": "On the Move Again", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Seasonal migration", "Sugarcane workers"]},
    ],
    (5, "Mathematics"): [
        {"number": 1, "title": "The Fish Tale", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Large numbers", "Speed calculations"]},
        {"number": 2, "title": "Shapes and Angles", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Angles", "Right angles"]},
        {"number": 3, "title": "How Many Squares?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Area of grid shapes", "Perimeter"]},
        {"number": 4, "title": "Parts and Wholes", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Fractions", "Equal parts"]},
        {"number": 5, "title": "Does it Look the Same?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Symmetry", "Rotations"]},
        {"number": 6, "title": "Be My Multiple, I'll be Your Factor", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Factors and Multiples", "LCM"]},
        {"number": 7, "title": "Can You See the Pattern?", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Number patterns", "Magic squares"]},
        {"number": 8, "title": "Mapping Your Way", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Scale drawing", "Directions and maps"]},
        {"number": 9, "title": "Boxes and Sketches", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["3D nets", "Cube drawings"]},
        {"number": 10, "title": "Tenths and Hundredths", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Decimals", "Centimetres and millimetres"]},
        {"number": 11, "title": "Area and its Boundary", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Perimeter and area formulas"]},
        {"number": 12, "title": "Smart Charts", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Tally marks", "Bar charts"]},
        {"number": 13, "title": "Ways to Multiply and Divide", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Multiplication methods", "Division word problems"]},
        {"number": 14, "title": "How Big? How Heavy?", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Volume of cuboids", "Weight measurement"]},
    ],

    # Grade 6
    (6, "Science"): [
        {"number": 1, "title": "Components of Food", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Nutrients", "Balanced diet"]},
        {"number": 2, "title": "Sorting Materials into Groups", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Material properties", "Solubility"]},
        {"number": 3, "title": "Separation of Substances", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Sieving", "Filtration", "Evaporation"]},
        {"number": 4, "title": "Getting to Know Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Roots", "Stems", "Leaves", "Flowers"]},
        {"number": 5, "title": "Body Movements", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Human skeleton", "Joints"]},
        {"number": 6, "title": "The Living Organisms — Characteristics and Habitats", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Adaptations", "Terrestrial vs Aquatic"]},
        {"number": 7, "title": "Motion and Measurement of Distances", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["SI units", "Types of motion"]},
        {"number": 8, "title": "Light, Shadows and Reflections", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Opaque objects", "Shadows", "Mirrors"]},
        {"number": 9, "title": "Electricity and Circuits", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Electric cell", "Switches", "Conductors"]},
        {"number": 10, "title": "Fun with Magnets", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Poles of magnet", "Magnetic compass"]},
        {"number": 11, "title": "Air Around Us", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Composition of air", "Oxygen cycle"]},
    ],
    (6, "Mathematics"): [
        {"number": 1, "title": "Knowing Our Numbers", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Place value", "Estimation", "Brackets"]},
        {"number": 2, "title": "Whole Numbers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Number line", "Properties of whole numbers"]},
        {"number": 3, "title": "Playing with Numbers", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Prime numbers", "HCF and LCM"]},
        {"number": 4, "title": "Basic Geometrical Ideas", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Points", "Lines", "Polygons", "Circles"]},
        {"number": 5, "title": "Understanding Elementary Shapes", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Angles", "Triangles", "3D Shapes"]},
        {"number": 6, "title": "Integers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Positive and negative numbers"]},
        {"number": 7, "title": "Fractions", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Types of fractions", "Equivalent fractions"]},
        {"number": 8, "title": "Decimals", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Tenths and hundredths", "Comparing decimals"]},
        {"number": 9, "title": "Data Handling", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Pictographs", "Bar graphs"]},
        {"number": 10, "title": "Mensuration", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Perimeter", "Area of rectangle & square"]},
        {"number": 11, "title": "Algebra", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Matchstick patterns", "Variables"]},
        {"number": 12, "title": "Ratio and Proportion", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Ratios", "Unitary method"]},
    ],

    # Grade 7
    (7, "Science"): [
        {"number": 1, "title": "Nutrition in Plants", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Photosynthesis", "Heterotrophs"]},
        {"number": 2, "title": "Nutrition in Animals", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Human digestion", "Ruminants"]},
        {"number": 3, "title": "Heat", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Conduction", "Convection", "Radiation"]},
        {"number": 4, "title": "Acids, Bases and Salts", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Litmus indicator", "Neutralisation"]},
        {"number": 5, "title": "Physical and Chemical Changes", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Rusting", "Crystallisation"]},
        {"number": 6, "title": "Respiration in Organisms", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Aerobic vs Anaerobic", "Breathing"]},
        {"number": 7, "title": "Transportation in Animals and Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Circulatory system", "Xylem & Phloem"]},
        {"number": 8, "title": "Reproduction in Plants", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Asexual reproduction", "Pollination"]},
        {"number": 9, "title": "Motion and Time", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Simple pendulum", "Speed-time graphs"]},
        {"number": 10, "title": "Electric Current and Its Effects", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Heating effect", "Electromagnets"]},
        {"number": 11, "title": "Light", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Mirrors and lenses", "Prisms"]},
        {"number": 12, "title": "Forests: Our Lifeline", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Forest ecosystem", "Interdependence"]},
        {"number": 13, "title": "Wastewater Story", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Sewage treatment", "Sanitation"]},
    ],
    (7, "Mathematics"): [
        {"number": 1, "title": "Integers", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Integer operations", "Properties"]},
        {"number": 2, "title": "Fractions and Decimals", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Multiplication & division"]},
        {"number": 3, "title": "Data Handling", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Mean, median, mode", "Bar graphs"]},
        {"number": 4, "title": "Simple Equations", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Solving equations"]},
        {"number": 5, "title": "Lines and Angles", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Complementary & supplementary", "Transversals"]},
        {"number": 6, "title": "The Triangle and Its Properties", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Exterior angle", "Pythagoras theorem"]},
        {"number": 7, "title": "Comparing Quantities", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Percentages", "Profit & Loss", "Simple interest"]},
        {"number": 8, "title": "Rational Numbers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Standard form", "Number line"]},
        {"number": 9, "title": "Perimeter and Area", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Parallelograms", "Triangles", "Circles"]},
        {"number": 10, "title": "Algebraic Expressions", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Terms and factors", "Adding expressions"]},
        {"number": 11, "title": "Exponents and Powers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Laws of exponents", "Scientific notation"]},
        {"number": 12, "title": "Symmetry", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Line symmetry", "Rotational symmetry"]},
        {"number": 13, "title": "Visualising Solid Shapes", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Nets for 3D shapes", "Views of solids"]},
    ],

    # Grade 8
    (8, "Science"): [
        {"number": 1, "title": "Crop Production and Management", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Soil preparation", "Sowing & Irrigation"]},
        {"number": 2, "title": "Microorganisms: Friend and Foe", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Vaccines", "Pathogens", "Food preservation"]},
        {"number": 3, "title": "Coal and Petroleum", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Fossil fuels", "Petroleum refining"]},
        {"number": 4, "title": "Combustion and Flame", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Ignition temp", "Flame structure"]},
        {"number": 5, "title": "Conservation of Plants and Animals", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Deforestation", "Biosphere reserves"]},
        {"number": 6, "title": "Reproduction in Animals", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Sexual reproduction", "Metamorphosis"]},
        {"number": 7, "title": "Reaching the Age of Adolescence", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Puberty", "Hormones"]},
        {"number": 8, "title": "Force and Pressure", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Contact forces", "Pressure in fluids"]},
        {"number": 9, "title": "Friction", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Static & kinetic friction", "Fluid friction"]},
        {"number": 10, "title": "Sound", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Vibrations", "Frequency and pitch"]},
        {"number": 11, "title": "Chemical Effects of Electric Current", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Electroplating", "Conduction in liquids"]},
        {"number": 12, "title": "Some Natural Phenomena", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Lightning", "Earthquakes"]},
        {"number": 13, "title": "Light", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Reflection laws", "Human eye"]},
    ],
    (8, "Mathematics"): [
        {"number": 1, "title": "Rational Numbers", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Properties of rational numbers"]},
        {"number": 2, "title": "Linear Equations in One Variable", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Solving equations", "Applications"]},
        {"number": 3, "title": "Understanding Quadrilaterals", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Polygons", "Parallelogram properties"]},
        {"number": 4, "title": "Data Handling", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Pie charts", "Probability"]},
        {"number": 5, "title": "Squares and Square Roots", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Square root division method"]},
        {"number": 6, "title": "Cubes and Cube Roots", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Prime factorisation for cube root"]},
        {"number": 7, "title": "Comparing Quantities", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Discounts", "GST", "Compound interest"]},
        {"number": 8, "title": "Algebraic Expressions and Identities", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Algebraic identities"]},
        {"number": 9, "title": "Mensuration", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Area of trapezium", "Surface area & volume"]},
        {"number": 10, "title": "Exponents and Powers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Negative exponents", "Laws"]},
        {"number": 11, "title": "Direct and Inverse Proportions", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Direct & inverse variation"]},
        {"number": 12, "title": "Factorisation", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Common factors", "Splitting middle term"]},
        {"number": 13, "title": "Introduction to Graphs", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Line graphs", "Cartesian coordinates"]},
    ],

    # Grade 9
    (9, "Physics"): [
        {"number": 1, "title": "Motion", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Speed", "Velocity", "Acceleration", "Equations of Motion"]},
        {"number": 2, "title": "Force and Laws of Motion", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Inertia", "Newton's First Law", "Momentum", "F=ma"]},
        {"number": 3, "title": "Gravitation", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Universal Law", "Free Fall", "Mass vs Weight"]},
        {"number": 4, "title": "Work and Energy", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Kinetic Energy", "Potential Energy", "Power"]},
        {"number": 5, "title": "Sound", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Wave Nature", "Speed of Sound", "Ultrasound"]},
    ],
    (9, "Chemistry"): [
        {"number": 1, "title": "Matter in Our Surroundings", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["States of Matter", "Evaporation", "Latent Heat"]},
        {"number": 2, "title": "Is Matter Around Us Pure", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Mixtures", "Solutions", "Separation Techniques"]},
        {"number": 3, "title": "Atoms and Molecules", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Dalton's Theory", "Atomic Mass", "Mole Concept"]},
        {"number": 4, "title": "Structure of the Atom", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Electrons", "Protons", "Bohr Model", "Valency"]},
    ],
    (9, "Biology"): [
        {"number": 1, "title": "The Fundamental Unit of Life", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Cell Membrane", "Organelles", "Nucleus"]},
        {"number": 2, "title": "Tissues", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Plant Tissues", "Animal Tissues", "Xylem & Phloem"]},
        {"number": 3, "title": "Improvement in Food Resources", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Crop Production", "Nutrient Management"]},
    ],
    (9, "Mathematics"): [
        {"number": 1, "title": "Number Systems", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Irrational Numbers", "Real Numbers", "Laws of Exponents"]},
        {"number": 2, "title": "Polynomials", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Degree", "Zeroes", "Factor Theorem"]},
        {"number": 3, "title": "Coordinate Geometry", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Cartesian Plane", "Quadrants", "Plotting Points"]},
        {"number": 4, "title": "Linear Equations in Two Variables", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Graphing", "Solutions"]},
        {"number": 5, "title": "Introduction to Euclid's Geometry", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Axioms and Postulates"]},
        {"number": 6, "title": "Lines and Angles", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Intersecting Lines", "Parallel Lines & Transversals"]},
        {"number": 7, "title": "Triangles", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Congruence Criteria", "Inequalities in a Triangle"]},
        {"number": 8, "title": "Quadrilaterals", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Parallelogram Theorems", "Mid-point Theorem"]},
        {"number": 9, "title": "Circles", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Angle Subtended by Arc", "Cyclic Quadrilaterals"]},
        {"number": 10, "title": "Heron's Formula", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Area of Triangle Formula"]},
        {"number": 11, "title": "Surface Areas and Volumes", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Cone", "Sphere", "Hemisphere"]},
        {"number": 12, "title": "Statistics", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Bar Graphs", "Histograms", "Frequency Polygon"]},
    ],

    # Grade 10
    (10, "Physics"): [
        {"number": 1, "title": "Light - Reflection and Refraction", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Mirror Formula", "Refractive Index", "Lens Formula"]},
        {"number": 2, "title": "Human Eye and Colourful World", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Defects of Vision", "Dispersion", "Atmospheric Refraction"]},
        {"number": 3, "title": "Electricity", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Ohm's Law", "Resistors in Series & Parallel", "Joule's Law"]},
        {"number": 4, "title": "Magnetic Effects of Electric Current", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Magnetic Field Lines", "Solenoid", "Fleming's Left Hand Rule"]},
    ],
    (10, "Chemistry"): [
        {"number": 1, "title": "Chemical Reactions and Equations", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Balancing Equations", "Types of Chemical Reactions"]},
        {"number": 2, "title": "Acids, Bases and Salts", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["pH Scale", "Plaster of Paris", "Bleaching Powder"]},
        {"number": 3, "title": "Metals and Non-Metals", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Reactivity Series", "Ionic Compounds", "Metallurgy"]},
        {"number": 4, "title": "Carbon and Its Compounds", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Covalent Bonding", "Homologous Series", "Ethanol & Ethanoic Acid"]},
    ],
    (10, "Biology"): [
        {"number": 1, "title": "Life Processes", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Nutrition", "Respiration", "Transportation", "Excretion"]},
        {"number": 2, "title": "Control and Coordination", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Nervous System", "Reflex Arc", "Plant Hormones"]},
        {"number": 3, "title": "How do Organisms Reproduce?", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Asexual vs Sexual", "Human Reproductive System", "Reproductive Health"]},
        {"number": 4, "title": "Heredity and Evolution", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Mendelian Inheritance", "Monohybrid & Dihybrid Cross"]},
        {"number": 5, "title": "Our Environment", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Ecosystem", "Food Chain & Web", "Ozone Depletion"]},
    ],
    (10, "Mathematics"): [
        {"number": 1, "title": "Real Numbers", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Fundamental Theorem of Arithmetic", "Proof of Irrationality"]},
        {"number": 2, "title": "Polynomials", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Geometrical Meaning of Zeroes", "Relationship between Zeroes & Coefficients"]},
        {"number": 3, "title": "Pair of Linear Equations in Two Variables", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Graphical Method", "Substitution & Elimination Methods"]},
        {"number": 4, "title": "Quadratic Equations", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Standard Form", "Quadratic Formula", "Nature of Roots"]},
        {"number": 5, "title": "Arithmetic Progressions", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["nth term of AP", "Sum of first n terms"]},
        {"number": 6, "title": "Triangles", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Basic Proportionality Theorem", "Criteria for Similarity"]},
        {"number": 7, "title": "Coordinate Geometry", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Distance Formula", "Section Formula"]},
        {"number": 8, "title": "Introduction to Trigonometry", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Trigonometric Ratios", "Identities"]},
        {"number": 9, "title": "Some Applications of Trigonometry", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Heights and Distances", "Angles of Elevation & Depression"]},
        {"number": 10, "title": "Circles", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Tangent to a Circle", "Tangents from External Point"]},
        {"number": 11, "title": "Areas Related to Circles", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Area of Sector & Segment"]},
        {"number": 12, "title": "Surface Areas and Volumes", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Combination of Solids", "Volume of Combined Solids"]},
        {"number": 13, "title": "Statistics", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Mean, Median, Mode of Grouped Data"]},
        {"number": 14, "title": "Probability", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Classical Definition of Probability"]},
    ],

    # Grade 11
    (11, "Physics"): [
        {"number": 1, "title": "Units and Measurements", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["SI Units", "Dimensional Analysis", "Errors"]},
        {"number": 2, "title": "Motion in a Straight Line", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Position-Time Graphs", "Kinematics"]},
        {"number": 3, "title": "Motion in a Plane", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Vectors", "Projectile Motion", "Circular Motion"]},
        {"number": 4, "title": "Laws of Motion", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Newton's Laws", "Momentum", "Friction"]},
        {"number": 5, "title": "Work, Energy and Power", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Work-Energy Theorem", "Collisions"]},
        {"number": 6, "title": "System of Particles and Rotational Motion", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Center of mass", "Torque", "Angular momentum"]},
        {"number": 7, "title": "Gravitation", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Kepler's laws", "Gravitational potential", "Satellites"]},
        {"number": 8, "title": "Mechanical Properties of Solids", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Stress and strain", "Hooke's law", "Young's modulus"]},
        {"number": 9, "title": "Mechanical Properties of Fluids", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Pascal's law", "Bernoulli's principle", "Viscosity"]},
        {"number": 10, "title": "Thermal Properties of Matter", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Heat capacity", "Thermal expansion", "Newton's law of cooling"]},
        {"number": 11, "title": "Thermodynamics", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["First & Second law", "Carnot engine"]},
        {"number": 12, "title": "Kinetic Theory", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Ideal gas laws", "Degrees of freedom"]},
        {"number": 13, "title": "Oscillations", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Simple harmonic motion", "Damped oscillations"]},
        {"number": 14, "title": "Waves", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Transverse & longitudinal waves", "Doppler effect"]},
    ],
    (11, "Chemistry"): [
        {"number": 1, "title": "Some Basic Concepts of Chemistry", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Mole Concept", "Stoichiometry"]},
        {"number": 2, "title": "Structure of Atom", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Quantum Numbers", "Bohr Model"]},
        {"number": 3, "title": "Classification of Elements and Periodicity in Properties", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Periodic Trends", "Electronegativity"]},
        {"number": 4, "title": "Chemical Bonding and Molecular Structure", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["VSEPR Theory", "Hybridisation", "MO Theory"]},
        {"number": 5, "title": "Chemical Thermodynamics", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Enthalpy", "Entropy", "Gibbs Free Energy"]},
        {"number": 6, "title": "Equilibrium", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Le Chatelier's Principle", "pH and Buffers", "Solubility Product"]},
        {"number": 7, "title": "Redox Reactions", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Oxidation Number", "Balancing Redox Reactions"]},
        {"number": 8, "title": "Organic Chemistry: Some Basic Principles and Techniques", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["IUPAC Nomenclature", "Isomerism", "Reaction Mechanisms"]},
        {"number": 9, "title": "Hydrocarbons", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Alkanes, Alkenes, Alkynes", "Aromaticity & Benzene"]},
    ],
    (11, "Biology"): [
        {"number": 1, "title": "The Living World", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Taxonomic Hierarchy", "Binomial Nomenclature"]},
        {"number": 2, "title": "Biological Classification", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Five Kingdom System", "Monera, Fungi"]},
        {"number": 3, "title": "Plant Kingdom", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Algae, Bryophytes, Gymnosperms"]},
        {"number": 4, "title": "Animal Kingdom", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Non-chordates and Chordates"]},
        {"number": 5, "title": "Morphology of Flowering Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Roots, Stems, Inflorescence"]},
        {"number": 6, "title": "Anatomy of Flowering Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Tissues", "Dicot vs Monocot anatomy"]},
        {"number": 7, "title": "Structural Organisation in Animals", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Animal Tissues", "Cockroach anatomy"]},
        {"number": 8, "title": "Cell: The Unit of Life", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Prokaryotic vs Eukaryotic cells", "Organelles"]},
        {"number": 9, "title": "Biomolecules", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Proteins, Nucleic Acids, Enzymes"]},
        {"number": 10, "title": "Cell Cycle and Cell Division", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Mitosis", "Meiosis"]},
        {"number": 11, "title": "Photosynthesis in Higher Plants", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Light reactions", "C3 and C4 Pathways"]},
        {"number": 12, "title": "Respiration in Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Glycolysis", "Krebs Cycle", "Electron Transport"]},
        {"number": 13, "title": "Plant Growth and Development", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Auxins, Gibberellins, Photoperiodism"]},
        {"number": 14, "title": "Breathing and Exchange of Gases", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Human Respiratory System", "Gas Transport"]},
        {"number": 15, "title": "Body Fluids and Circulation", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Blood groups", "Cardiac Cycle", "ECG"]},
        {"number": 16, "title": "Excretory Products and Their Elimination", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Nephron structure", "Urine formation"]},
        {"number": 17, "title": "Locomotion and Movement", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Muscle contraction", "Skeletal joints"]},
        {"number": 18, "title": "Neural Control and Coordination", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Neuron", "Conduction of nerve impulse"]},
        {"number": 19, "title": "Chemical Coordination and Integration", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Endocrine glands", "Hormone action"]},
    ],
    (11, "Mathematics"): [
        {"number": 1, "title": "Sets", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Venn Diagrams", "Union and Intersection"]},
        {"number": 2, "title": "Relations and Functions", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Cartesian Product", "Domain and Range"]},
        {"number": 3, "title": "Trigonometric Functions", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Radian Measure", "Trigonometric Identities"]},
        {"number": 4, "title": "Complex Numbers and Quadratic Equations", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Imaginary numbers", "Modulus and Conjugate"]},
        {"number": 5, "title": "Linear Inequalities", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Algebraic solutions of inequalities"]},
        {"number": 6, "title": "Permutations and Combinations", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Factorial n", "nPr and nCr formulas"]},
        {"number": 7, "title": "Binomial Theorem", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Pascal's triangle", "General term"]},
        {"number": 8, "title": "Sequences and Series", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Arithmetic & Geometric Progressions"]},
        {"number": 9, "title": "Straight Lines", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Slope of a line", "Forms of line equations"]},
        {"number": 10, "title": "Conic Sections", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Circles", "Parabolas", "Ellipses", "Hyperbolas"]},
        {"number": 11, "title": "Introduction to Three Dimensional Geometry", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Coordinates in 3D space", "Distance formula"]},
        {"number": 12, "title": "Limits and Derivatives", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Intuitive idea of limit", "Standard derivatives"]},
        {"number": 13, "title": "Statistics", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Mean deviation", "Variance and standard deviation"]},
        {"number": 14, "title": "Probability", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Sample spaces", "Events and axioms"]},
    ],

    # Grade 12
    (12, "Physics"): [
        {"number": 1, "title": "Electric Charges and Fields", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Coulomb's Law", "Electric Dipole", "Gauss's Law"]},
        {"number": 2, "title": "Electrostatic Potential and Capacitance", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Equipotential Surfaces", "Capacitors in Series & Parallel"]},
        {"number": 3, "title": "Current Electricity", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Drift Velocity", "Kirchhoff's Rules", "Wheatstone Bridge"]},
        {"number": 4, "title": "Moving Charges and Magnetism", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Biot-Savart Law", "Ampere's Law", "Cyclotron"]},
        {"number": 5, "title": "Magnetism and Matter", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Magnetic dipole moment", "Para-, Dia-, Ferromagnetism"]},
        {"number": 6, "title": "Electromagnetic Induction", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Faraday's Law", "Lenz's Law", "Self & Mutual Inductance"]},
        {"number": 7, "title": "Alternating Current", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["LCR Series Circuit", "Resonance", "Transformers"]},
        {"number": 8, "title": "Electromagnetic Waves", "status": "not_started", "duration": "20 mins", "mastery": 0, "topics": ["Displacement current", "EM Spectrum"]},
        {"number": 9, "title": "Ray Optics and Optical Instruments", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Total Internal Reflection", "Prisms", "Microscopes"]},
        {"number": 10, "title": "Wave Optics", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Huygens Principle", "Young's Double Slit", "Diffraction"]},
        {"number": 11, "title": "Dual Nature of Radiation and Matter", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Photoelectric effect", "de Broglie wavelength"]},
        {"number": 12, "title": "Atoms", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Alpha-particle scattering", "Bohr model of hydrogen atom"]},
        {"number": 13, "title": "Nuclei", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Binding energy", "Nuclear fission and fusion"]},
        {"number": 14, "title": "Semiconductor Electronics: Materials, Devices and Simple Circuits", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["p-n junction diode", "Rectifiers", "Zener diode"]},
    ],
    (12, "Chemistry"): [
        {"number": 1, "title": "Solutions", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Raoult's Law", "Colligative Properties", "Osmotic Pressure"]},
        {"number": 2, "title": "Electrochemistry", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Nernst Equation", "Kohlrausch Law", "Batteries"]},
        {"number": 3, "title": "Chemical Kinetics", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Rate of Reaction", "Order & Molecularity", "Arrhenius Equation"]},
        {"number": 4, "title": "The d- and f- Block Elements", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Transition metals", "Lanthanoid contraction"]},
        {"number": 5, "title": "Coordination Compounds", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["IUPAC naming", "Werner's theory", "Crystal Field Theory"]},
        {"number": 6, "title": "Haloalkanes and Haloarenes", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["SN1 and SN2 mechanisms", "Polyhalogen compounds"]},
        {"number": 7, "title": "Alcohols, Phenols and Ethers", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Reactions of phenols", "Preparation of ethers"]},
        {"number": 8, "title": "Aldehydes, Ketones and Carboxylic Acids", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Nucleophilic addition", "Aldol condensation", "Cannizzaro reaction"]},
        {"number": 9, "title": "Amines", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Basicity of amines", "Diazonium salts"]},
        {"number": 10, "title": "Biomolecules", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Carbohydrates", "Proteins and Amino Acids", "DNA & RNA"]},
    ],
    (12, "Biology"): [
        {"number": 1, "title": "Sexual Reproduction in Flowering Plants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Microsporogenesis", "Double Fertilisation"]},
        {"number": 2, "title": "Human Reproduction", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Gametogenesis", "Menstrual Cycle", "Implantation"]},
        {"number": 3, "title": "Reproductive Health", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Contraception", "ART techniques"]},
        {"number": 4, "title": "Principles of Inheritance and Variation", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Mendelian Genetics", "Sex Determination", "Pedigree analysis"]},
        {"number": 5, "title": "Molecular Basis of Inheritance", "status": "not_started", "duration": "45 mins", "mastery": 0, "topics": ["DNA replication", "Transcription", "Translation", "Human Genome Project"]},
        {"number": 6, "title": "Evolution", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Origin of life", "Darwinism", "Hardy-Weinberg principle"]},
        {"number": 7, "title": "Human Health and Disease", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Immunity", "Cancer", "AIDS", "Drugs & Alcohol abuse"]},
        {"number": 8, "title": "Microbes in Human Welfare", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Household products", "Sewage treatment", "Biocontrol agents"]},
        {"number": 9, "title": "Biotechnology: Principles and Processes", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Recombinant DNA technology", "Restriction enzymes", "PCR"]},
        {"number": 10, "title": "Biotechnology and Its Applications", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Bt cotton", "Gene therapy", "Transgenic animals"]},
        {"number": 11, "title": "Organisms and Populations", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Organism and environment", "Population growth models"]},
        {"number": 12, "title": "Ecosystem", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Energy flow", "Ecological pyramids", "Primary productivity"]},
        {"number": 13, "title": "Biodiversity and Conservation", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Loss of biodiversity", "In-situ & Ex-situ conservation"]},
    ],
    (12, "Mathematics"): [
        {"number": 1, "title": "Relations and Functions", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Equivalence Relations", "One-One & Onto Functions"]},
        {"number": 2, "title": "Inverse Trigonometric Functions", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["Principal Value Branches", "Properties"]},
        {"number": 3, "title": "Matrices", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Matrix Operations", "Transpose", "Symmetric & Skew Symmetric"]},
        {"number": 4, "title": "Determinants", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Area of Triangle", "Adjoint and Inverse of Matrix"]},
        {"number": 5, "title": "Continuity and Differentiability", "status": "not_started", "duration": "40 mins", "mastery": 0, "topics": ["Chain Rule", "Logarithmic Differentiation", "Second Order Derivatives"]},
        {"number": 6, "title": "Application of Derivatives", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Rate of change", "Increasing/Decreasing functions", "Maxima & Minima"]},
        {"number": 7, "title": "Integrals", "status": "not_started", "duration": "45 mins", "mastery": 0, "topics": ["Indefinite & Definite Integrals", "Integration by parts/partial fractions"]},
        {"number": 8, "title": "Application of Integrals", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Area under simple curves", "Area between two curves"]},
        {"number": 9, "title": "Differential Equations", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Order and degree", "Variable separable", "Linear differential equations"]},
        {"number": 10, "title": "Vector Algebra", "status": "not_started", "duration": "30 mins", "mastery": 0, "topics": ["Dot product", "Cross product", "Direction cosines"]},
        {"number": 11, "title": "Three Dimensional Geometry", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Direction ratios", "Equation of line in 3D", "Shortest distance"]},
        {"number": 12, "title": "Linear Programming", "status": "not_started", "duration": "25 mins", "mastery": 0, "topics": ["LPP formulation", "Graphical solution method"]},
        {"number": 13, "title": "Probability", "status": "not_started", "duration": "35 mins", "mastery": 0, "topics": ["Conditional probability", "Bayes' Theorem", "Random variable"]},
    ],
}

NCERT_CURRICULUM_DATA: dict[int, list[dict[str, Any]]] = {
    5: [
        {"name": "Science", "code": "SCI05", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 22, "color": "0xFF10B981", "icon": "science_outlined"},
        {"name": "Mathematics", "code": "MAT05", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 14, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    6: [
        {"name": "Science", "code": "SCI06", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 11, "color": "0xFF10B981", "icon": "science_outlined"},
        {"name": "Mathematics", "code": "MAT06", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 12, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    7: [
        {"name": "Science", "code": "SCI07", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFF10B981", "icon": "science_outlined"},
        {"name": "Mathematics", "code": "MAT07", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    8: [
        {"name": "Science", "code": "SCI08", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFF10B981", "icon": "science_outlined"},
        {"name": "Mathematics", "code": "MAT08", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    9: [
        {"name": "Physics", "code": "PHY09", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 5, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE09", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 4, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO09", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 3, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT09", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 12, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    10: [
        {"name": "Physics", "code": "PHY10", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 4, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE10", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 4, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO10", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 5, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT10", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 14, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    11: [
        {"name": "Physics", "code": "PHY11", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 14, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE11", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 9, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO11", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 19, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT11", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 14, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    12: [
        {"name": "Physics", "code": "PHY12", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 14, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE12", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 10, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO12", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT12", "progress": 0.0, "chaptersCompleted": 0, "totalChapters": 13, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
}


@app.get("/catalog/subjects")
async def get_catalog_subjects(grade: int = Query(9, ge=5, le=12)):
    subjects = NCERT_CURRICULUM_DATA.get(grade, NCERT_CURRICULUM_DATA[9])
    res = []
    for s in subjects:
        sub_name = s["name"]
        chapter_count = len(CHAPTERS_BY_GRADE_SUBJECT.get((grade, sub_name), []))
        item = dict(s)
        if chapter_count > 0:
            item["totalChapters"] = chapter_count
        res.append(item)
    return res


@app.get("/catalog/chapters")
async def get_catalog_chapters(subject: str = Query("Physics"), grade: int = Query(9)):
    normalized_sub = "Physics"
    for s in ["Physics", "Chemistry", "Biology", "Science", "Mathematics"]:
        if s.lower() in subject.lower():
            normalized_sub = s
            break

    if (grade, normalized_sub) in CHAPTERS_BY_GRADE_SUBJECT:
        return CHAPTERS_BY_GRADE_SUBJECT[(grade, normalized_sub)]

    for (g, s), ch_list in CHAPTERS_BY_GRADE_SUBJECT.items():
        if s == normalized_sub:
            return ch_list

    return CHAPTERS_BY_GRADE_SUBJECT[(9, "Physics")]



@app.get("/api/v1/simulations/{chapter}")
async def get_chapter_simulation(chapter: str):
    clean_title = chapter.replace("_", " ").title()
    return {
        "chapter": chapter,
        "title": f"Interactive {clean_title} Lab",
        "description": f"Adjust key variables in real-time to observe principles of {clean_title}.",
        "parameters": [
            {"id": "param1", "name": f"{clean_title} Intensity (I)", "unit": "units", "min": 0.5, "max": 5.0, "default": 2.0, "step": 0.1},
            {"id": "param2", "name": f"Environmental Factor (k)", "unit": "const", "min": 1.0, "max": 10.0, "default": 5.0, "step": 0.5},
        ],
        "formulas": [
            f"Standard NCERT Equation for {clean_title}",
            "Parameter Relationship & SI Units",
        ],
        "canvasHtml": f"<div id='sim-{chapter}'>Interactive HTML5 Canvas Player</div>",
    }


@app.get("/videos/{chapter}")
async def get_chapter_videos(chapter: str):
    clean_title = chapter.replace("_", " ").title()
    return {
        "chapter": chapter,
        "currentVideo": {
            "id": f"v_{chapter}_01",
            "youtubeId": "tBmavvMwu68",
            "title": f"NCERT Explanation: {clean_title}",
            "channel": "NCERT Official",
            "duration": "15:00",
        },
        "playlist": [
            {"id": "v1", "title": f"1. Introduction to {clean_title}", "duration": "05:00", "timestamp": "00:00"},
            {"id": "v2", "title": f"2. Core Principles & Formulas of {clean_title}", "duration": "05:00", "timestamp": "05:00"},
            {"id": "v3", "title": f"3. Solved Examples & NCERT Exercises", "duration": "05:00", "timestamp": "10:00"},
        ],
        "islAvailable": True,
        "islVideoId": f"isl_{chapter}",
        "keyTakeaways": [
            f"Master core definitions and theoretical foundation of {clean_title}.",
            "Apply equations and problem-solving steps for NCERT examinations.",
        ],
    }


@app.get("/quizzes/{chapter}")
async def get_chapter_quiz(chapter: str):
    clean_title = chapter.replace("_", " ").title()
    return {
        "chapter": chapter,
        "title": f"Diagnostic Assessment: {clean_title}",
        "totalQuestions": 3,
        "userMastery": 0,
        "questions": [
            {
                "id": "q1",
                "question": f"What is the primary conceptual principle studied in {clean_title}?",
                "options": ["Fundamental Properties", "Derived Equations", "Practical Applications", "All of the above"],
                "correctIndex": 3,
                "explanation": f"{clean_title} encompasses fundamental properties, equations, and practical applications.",
            },
            {
                "id": "q2",
                "question": f"Which parameter is essential for analyzing {clean_title}?",
                "options": ["SI Unit Measurements", "Boundary Conditions", "Both A and B", "None of the above"],
                "correctIndex": 2,
                "explanation": "Both SI unit measurements and boundary conditions are essential for analytical solutions.",
            },
            {
                "id": "q3",
                "question": f"How are concepts of {clean_title} validated in NCERT experiments?",
                "options": ["Observation & Data Gathering", "Controlled Laboratory Tests", "Mathematical Derivations", "All of the above"],
                "correctIndex": 3,
                "explanation": "Validation requires observation, controlled testing, and mathematical consistency.",
            },
        ],
    }


@app.get("/api/user/profile")
async def get_user_profile():
    return {
        "name": "Student User",
        "email": "student@mentora.in",
        "grade": 9,
        "streakDays": 0,
        "dailyMinsLearned": 0,
        "dailyGoalMins": 30,
        "activeSubject": "Science",
        "activeChapter": "Overview",
    }

