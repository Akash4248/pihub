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


def generate_socratic_response(query: str, topic: str | None, grade: int = 9) -> dict[str, Any]:
    q_lower = query.lower()
    t_name = topic or "Science & Mathematics"

    if any(k in q_lower for k in ["speed", "velocity", "acceleration", "motion"]):
        answer = (
            f"Great question about **{t_name}**!\n\n"
            f"**Query**: *\"{query}\"*\n\n"
            f"### Key NCERT Class {grade} Concepts:\n"
            f"• **Speed** is a scalar quantity equal to total distance divided by time ($s = d/t$).\n"
            f"• **Velocity** is a vector quantity defined as displacement per unit time ($v = s/t$).\n"
            f"• **Acceleration** ($a$) is the rate of change of velocity ($a = \\frac{{v - u}}{{t}}$).\n\n"
            f"### Equations of Motion:\n"
            f"1. $v = u + at$\n"
            f"2. $s = ut + \\frac{{1}}{{2}}at^2$\n"
            f"3. $v^2 = u^2 + 2as$"
        )
        formulas = ["v = u + at", "s = ut + ½at²", "v² = u² + 2as"]
    elif any(k in q_lower for k in ["force", "newton", "inertia", "momentum"]):
        answer = (
            f"Excellent question on **Force & Newton's Laws** (Class {grade} NCERT)!\n\n"
            f"**Query**: *\"{query}\"*\n\n"
            f"### Newton's Laws of Motion:\n"
            f"1. **First Law (Inertia)**: An object remains at rest or uniform motion unless acted upon by an external net force.\n"
            f"2. **Second Law ($F = ma$)**: Force equals mass times acceleration ($F = m \\cdot a$). Momentum $p = mv$.\n"
            f"3. **Third Law**: To every action, there is an equal and opposite reaction."
        )
        formulas = ["F = ma", "p = mv", "F_{12} = -F_{21}"]
    elif any(k in q_lower for k in ["cell", "nucleus", "mitochondria", "dna", "plant", "animal"]):
        answer = (
            f"Wonderful question in **Biology: Cell Biology** (Class {grade} NCERT)!\n\n"
            f"**Query**: *\"{query}\"*\n\n"
            f"### Core Concepts:\n"
            f"• **The Cell** is the fundamental structural and functional unit of life.\n"
            f"• **Mitochondria**: Known as the powerhouse of the cell, generating ATP energy.\n"
            f"• **Nucleus**: Contains genetic material (DNA/Chromosomes) controlling cell functions.\n"
            f"• **Plant vs Animal Cells**: Plant cells contain cell walls and chloroplasts for photosynthesis."
        )
        formulas = ["C_6H_{12}O_6 + 6O_2 \\rightarrow 6CO_2 + 6H_2O + ATP"]
    elif any(k in q_lower for k in ["atom", "molecule", "electron", "proton", "element", "acid", "base", "ph"]):
        answer = (
            f"Great inquiry in **Chemistry & Atomic Structure** (Class {grade} NCERT)!\n\n"
            f"**Query**: *\"{query}\"*\n\n"
            f"### Key NCERT Principles:\n"
            f"• **Atoms** consist of protons (+), neutrons (0), and orbiting electrons (-).\n"
            f"• **Atomic Number ($Z$)**: Number of protons in nucleus.\n"
            f"• **Mass Number ($A$)**: Protons + Neutrons ($A = Z + N$).\n"
            f"• **Acids & Bases**: Acids release $H^+$ ions in water (pH < 7); Bases release $OH^-$ ions (pH > 7)."
        )
        formulas = ["pH = -\\log_{10}[H^+]", "A = Z + N"]
    elif any(k in q_lower for k in ["math", "equation", "triangle", "quadratic", "pythagoras", "area"]):
        answer = (
            f"Here is the mathematical breakdown for your question in **{t_name}** (Class {grade} NCERT):\n\n"
            f"**Query**: *\"{query}\"*\n\n"
            f"### Key Formulas & Theorems:\n"
            f"• **Pythagoras Theorem**: In a right-angled triangle, $a^2 + b^2 = c^2$.\n"
            f"• **Quadratic Formula**: $x = \\frac{{-b \\pm \\sqrt{{b^2 - 4ac}}}}{{2a}}$.\n"
            f"• **Area of Triangle**: $\\text{{Area}} = \\frac{{1}}{{2}} \\times \\text{{base}} \\times \\text{{height}}$."
        )
        formulas = ["a² + b² = c²", "x = (-b ± √(b² - 4ac)) / (2a)"]
    else:
        answer = (
            f"Thanks for asking about **{t_name}** (Grade {grade} NCERT)!\n\n"
            f"**Your Question**: *\"{query}\"*\n\n"
            f"### NCERT Socratic Guidance:\n"
            f"• To solve questions regarding **{query}**, first identify given variables and target quantities.\n"
            f"• Recall foundational principles of {t_name} from your textbook chapters.\n"
            f"• Step 1: Write down standard formulas.\n"
            f"• Step 2: Substitute values with proper SI units.\n"
            f"• Step 3: Verify boundary conditions and dimensional consistency."
        )
        formulas = ["NCERT Standard Formula Set"]

    return {
        "answer": answer,
        "formulas": formulas,
        "hasAudio": True,
        "explainOptions": ["Real-World Analogy", "Step-by-Step Math", "Simpler Language", "Visual Simulation"],
        "source": "native_gateway_dynamic_socratic_engine",
    }


@app.post("/ai/tutor")
async def ai_tutor(payload: dict[str, Any]) -> Any:
    started = time.perf_counter()
    query = str(payload.get("question") or payload.get("query") or "").strip()
    normalization = normalize_topic(payload.get("topic"), query)
    normalized_topic = normalization.normalized_topic
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
        logger.info(
            "[RAG] RETRIEVAL query=%s normalized_topic=%s intent=%s chunks_retrieved=%s chunks_used=%s fallback_reason=%s",
            query,
            normalized_topic,
            intent,
            0,
            0,
            "planner_trigger",
        )
        if payload.get("stream") is True:
            return _planner_stream(response)
        return response

    asset_start = time.perf_counter()
    bundle = await _asset_search_bundle(payload, normalized_topic)
    asset_ms = (time.perf_counter() - asset_start) * 1000
    asset_matches = _asset_bundle_count(bundle)

    enriched_payload = dict(payload)
    if normalized_topic:
        enriched_payload["topic"] = normalized_topic
        enriched_payload["normalized_topic"] = normalized_topic
    enriched_payload["original_topic"] = normalization.original_topic
    enriched_payload["topic_alias"] = normalization.matched_alias
    enriched_payload["asset_context"] = _asset_context(bundle)
    enriched_payload["asset_search"] = {
        "filters": bundle.get("filters", {}),
        "counts": {
            "summaries": len(bundle.get("summaries", [])),
            "flashcards": len(bundle.get("flashcards", [])),
            "quizzes": len(bundle.get("quizzes", [])),
            "glossary": len(bundle.get("glossary", [])),
        },
    }

    try:
        result = await _proxy_to(settings.inference_service_url, "POST", "/ai/tutor", enriched_payload)
    except Exception as exc:
        logger.warning("[TUTOR] Inference service proxy failed (%s). Using Native Dynamic Socratic fallback.", exc)
        return generate_socratic_response(query, normalized_topic or payload.get("topic"), payload.get("grade", 9))
    total_ms = (time.perf_counter() - started) * 1000
    if isinstance(result, StreamingResponse):
        _record_retrieval_metric({
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
        logger.info(
            "[RAG] RETRIEVAL query=%s normalized_topic=%s intent=%s chunks_retrieved=%s chunks_used=%s fallback_reason=%s",
            query,
            normalized_topic,
            intent,
            0,
            0,
            None if asset_matches else "no_asset_match",
        )
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
    _record_retrieval_metric({
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
    logger.info(
        "[RAG] RETRIEVAL query=%s normalized_topic=%s intent=%s chunks_retrieved=%s chunks_used=%s fallback_reason=%s",
        query,
        normalized_topic,
        intent,
        chunks_retrieved,
        chunks_used,
        fallback_reason,
    )
    return result


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

NCERT_CURRICULUM_DATA: dict[int, list[dict[str, Any]]] = {
    9: [
        {"name": "Physics", "code": "PHY09", "progress": 0.58, "chaptersCompleted": 7, "totalChapters": 12, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE09", "progress": 0.45, "chaptersCompleted": 5, "totalChapters": 11, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO09", "progress": 0.60, "chaptersCompleted": 6, "totalChapters": 10, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT09", "progress": 0.40, "chaptersCompleted": 6, "totalChapters": 15, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    10: [
        {"name": "Physics", "code": "PHY10", "progress": 0.65, "chaptersCompleted": 8, "totalChapters": 13, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE10", "progress": 0.50, "chaptersCompleted": 6, "totalChapters": 12, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Biology", "code": "BIO10", "progress": 0.70, "chaptersCompleted": 7, "totalChapters": 10, "color": "0xFF14B8A6", "icon": "nature_outlined"},
        {"name": "Mathematics", "code": "MAT10", "progress": 0.55, "chaptersCompleted": 8, "totalChapters": 15, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    11: [
        {"name": "Physics", "code": "PHY11", "progress": 0.42, "chaptersCompleted": 6, "totalChapters": 14, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE11", "progress": 0.38, "chaptersCompleted": 5, "totalChapters": 14, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Mathematics", "code": "MAT11", "progress": 0.35, "chaptersCompleted": 5, "totalChapters": 16, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
    12: [
        {"name": "Physics", "code": "PHY12", "progress": 0.30, "chaptersCompleted": 4, "totalChapters": 14, "color": "0xFF4F46E5", "icon": "science_outlined"},
        {"name": "Chemistry", "code": "CHE12", "progress": 0.28, "chaptersCompleted": 4, "totalChapters": 14, "color": "0xFF10B981", "icon": "biotech_outlined"},
        {"name": "Mathematics", "code": "MAT12", "progress": 0.32, "chaptersCompleted": 4, "totalChapters": 13, "color": "0xFFF59E0B", "icon": "calculate_outlined"},
    ],
}

CHAPTERS_DATA: dict[str, list[dict[str, Any]]] = {
    "Physics": [
        {"number": 1, "title": "Motion", "status": "completed", "duration": "25 mins", "mastery": 92, "topics": ["Speed", "Velocity", "Acceleration", "Equations of Motion"]},
        {"number": 2, "title": "Force and Laws of Motion", "status": "in_progress", "duration": "30 mins", "mastery": 58, "topics": ["Inertia", "Newton's First Law", "Momentum", "F=ma"]},
        {"number": 3, "title": "Gravitation", "status": "locked", "duration": "20 mins", "mastery": 0, "topics": ["Universal Law", "Free Fall", "Mass vs Weight"]},
        {"number": 4, "title": "Work and Energy", "status": "locked", "duration": "35 mins", "mastery": 0, "topics": ["Kinetic Energy", "Potential Energy", "Power"]},
        {"number": 5, "title": "Sound", "status": "locked", "duration": "25 mins", "mastery": 0, "topics": ["Wave Nature", "Speed of Sound", "Ultrasound"]},
    ],
    "Chemistry": [
        {"number": 1, "title": "Matter in Our Surroundings", "status": "completed", "duration": "20 mins", "mastery": 88, "topics": ["States of Matter", "Evaporation", "Latent Heat"]},
        {"number": 2, "title": "Is Matter Around Us Pure", "status": "in_progress", "duration": "35 mins", "mastery": 62, "topics": ["Mixtures", "Solutions", "Separation Techniques"]},
        {"number": 3, "title": "Atoms and Molecules", "status": "locked", "duration": "40 mins", "mastery": 0, "topics": ["Dalton's Theory", "Atomic Mass", "Mole Concept"]},
        {"number": 4, "title": "Structure of the Atom", "status": "locked", "duration": "30 mins", "mastery": 0, "topics": ["Electrons", "Protons", "Bohr Model", "Valency"]},
    ],
    "Biology": [
        {"number": 1, "title": "The Fundamental Unit of Life", "status": "completed", "duration": "30 mins", "mastery": 95, "topics": ["Cell Membrane", "Organelles", "Nucleus"]},
        {"number": 2, "title": "Tissues", "status": "in_progress", "duration": "35 mins", "mastery": 50, "topics": ["Plant Tissues", "Animal Tissues", "Xylem & Phloem"]},
        {"number": 3, "title": "Improvement in Food Resources", "status": "locked", "duration": "25 mins", "mastery": 0, "topics": ["Crop Production", "Nutrient Management"]},
    ],
    "Mathematics": [
        {"number": 1, "title": "Number Systems", "status": "completed", "duration": "30 mins", "mastery": 90, "topics": ["Irrational Numbers", "Real Numbers", "Laws of Exponents"]},
        {"number": 2, "title": "Polynomials", "status": "in_progress", "duration": "40 mins", "mastery": 65, "topics": ["Degree", "Zeroes", "Factor Theorem"]},
        {"number": 3, "title": "Coordinate Geometry", "status": "locked", "duration": "25 mins", "mastery": 0, "topics": ["Cartesian Plane", "Quadrants", "Plotting Points"]},
        {"number": 4, "title": "Linear Equations in Two Variables", "status": "locked", "duration": "35 mins", "mastery": 0, "topics": ["Graphing", "Solutions"]},
    ]
}


@app.get("/catalog/subjects")
async def get_catalog_subjects(grade: int = Query(9, ge=5, le=12)):
    subjects = NCERT_CURRICULUM_DATA.get(grade, NCERT_CURRICULUM_DATA[9])
    return subjects


@app.get("/catalog/chapters")
async def get_catalog_chapters(subject: str = Query("Physics"), grade: int = Query(9)):
    normalized_sub = "Physics"
    for s in ["Physics", "Chemistry", "Biology", "Mathematics"]:
        if s.lower() in subject.lower():
            normalized_sub = s
            break
    chapters = CHAPTERS_DATA.get(normalized_sub, CHAPTERS_DATA["Physics"])
    return chapters


@app.get("/api/v1/simulations/{chapter}")
async def get_chapter_simulation(chapter: str):
    return {
        "chapter": chapter,
        "title": "Interactive Pendulum & Motion Lab",
        "description": "Adjust length (L) and gravitational acceleration (g) to observe periodic motion and velocity in real-time.",
        "parameters": [
            {"id": "length", "name": "Pendulum Length (L)", "unit": "m", "min": 0.5, "max": 5.0, "default": 2.0, "step": 0.1},
            {"id": "gravity", "name": "Gravity (g)", "unit": "m/s²", "min": 1.6, "max": 24.8, "default": 9.8, "step": 0.1},
        ],
        "formulas": [
            "T = 2π √(L / g)",
            "v_max = √(2gL (1 - cos θ))",
        ],
        "canvasHtml": "<div id='pendulum-sim'>Interactive HTML5 Canvas Player</div>",
    }


@app.get("/videos/{chapter}")
async def get_chapter_videos(chapter: str):
    return {
        "chapter": chapter,
        "currentVideo": {
            "id": "v_motion_01",
            "youtubeId": "tBmavvMwu68",
            "title": "NCERT Class 9 Physics: Motion & Equations of Motion",
            "channel": "NCERT Official",
            "duration": "18:45",
        },
        "playlist": [
            {"id": "v1", "title": "1. Distance vs Displacement Explained", "duration": "05:20", "timestamp": "00:00"},
            {"id": "v2", "title": "2. Deriving Equations of Motion (v = u + at)", "duration": "08:15", "timestamp": "05:20"},
            {"id": "v3", "title": "3. Uniform Circular Motion", "duration": "05:10", "timestamp": "13:35"},
        ],
        "islAvailable": True,
        "islVideoId": "isl_motion_9",
        "keyTakeaways": [
            "Speed is scalar (magnitude only), Velocity is vector (magnitude + direction).",
            "First equation of motion: v = u + at relates final velocity, initial velocity, acceleration, and time.",
            "Area under velocity-time graph represents displacement covered.",
        ],
    }


@app.get("/quizzes/{chapter}")
async def get_chapter_quiz(chapter: str):
    return {
        "chapter": chapter,
        "title": "Diagnostic Assessment: Motion & Velocity",
        "totalQuestions": 3,
        "userMastery": 85,
        "questions": [
            {
                "id": "q1",
                "question": "A car covers 100 meters in 5 seconds. What is its average speed?",
                "options": ["10 m/s", "20 m/s", "25 m/s", "50 m/s"],
                "correctIndex": 1,
                "explanation": "Speed = Distance / Time = 100 m / 5 s = 20 m/s.",
            },
            {
                "id": "q2",
                "question": "Which of the following is a vector quantity?",
                "options": ["Distance", "Speed", "Velocity", "Mass"],
                "correctIndex": 2,
                "explanation": "Velocity has both magnitude and direction, making it a vector quantity.",
            },
            {
                "id": "q3",
                "question": "An object starts from rest and accelerates at 2 m/s² for 4 seconds. What is its final velocity?",
                "options": ["4 m/s", "8 m/s", "12 m/s", "16 m/s"],
                "correctIndex": 1,
                "explanation": "Using v = u + at: u = 0, a = 2, t = 4 => v = 0 + (2 * 4) = 8 m/s.",
            },
        ],
    }


@app.get("/api/user/profile")
async def get_user_profile():
    return {
        "name": "Rahul Sharma",
        "email": "rahul.student@mentora.in",
        "grade": 9,
        "streakDays": 5,
        "dailyMinsLearned": 24,
        "dailyGoalMins": 30,
        "activeSubject": "Physics",
        "activeChapter": "Motion",
    }

