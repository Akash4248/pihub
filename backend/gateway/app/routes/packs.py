from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from shared.config import get_settings

from app.services.pack_catalog import (
    canonical_pack_entries,
    canonical_pack_records,
    dedupe_curriculum_packs,
    iso_from_epoch,
    pack_matches_filters,
    pack_path,
    pack_service_packs,
    pack_stat,
    resolve_pack_service_pack_id,
)
from app.services.upstream import get_json, post_json, proxy_stream, proxy_to


router = APIRouter(tags=["Sync & Packs"])

logger = logging.getLogger(__name__)

settings = get_settings()


@router.get("/sync")
async def sync_get() -> dict[str, Any]:
    return await proxy_to(settings.pihub_url, "GET", "/sync")


@router.post("/sync")
async def sync_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_to(settings.pihub_url, "POST", "/sync", payload)


@router.get("/packs")
async def packs_get() -> dict[str, Any]:
    packs, cached, sources = await canonical_pack_records()
    return {
        "packs": packs,
        "cached": cached,
        "total_count": len(packs) + len(cached),
        "sources": sources,
    }


@router.get("/packs/sync")
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

    all_packs, _, _ = await canonical_pack_records()
    if not all_packs:
        fallback_packs = await pack_service_packs()
        if fallback_packs:
            all_packs, _ = canonical_pack_entries(fallback_packs, "pack_service")
            all_packs = dedupe_curriculum_packs(all_packs)
    all_records = {str(pack["pack_id"]): pack for pack in all_packs}
    packs = [
        pack
        for pack in all_packs
        if pack_matches_filters(pack, grade=grade, subject=subject, language=language)
    ]

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


@router.get("/packs/catalog")
async def packs_catalog() -> dict[str, Any]:
    packs, _, _ = await canonical_pack_records()
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
        stat = pack_stat(pack)
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


@router.get("/packs/coverage")
async def packs_coverage() -> dict[str, Any]:
    return await get_json(settings.pack_service_url, "/packs/coverage")


@router.get("/packs/multilingual/plan")
async def packs_multilingual_plan(
    target_language: str = Query(default="hi"),
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
) -> dict[str, Any]:
    return await get_json(
        settings.pack_service_url,
        "/packs/multilingual/plan",
        params={"target_language": target_language, "grade": grade, "subject": subject},
    )


@router.get("/packs/recommended")
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
    packs, _, _ = await canonical_pack_records()
    candidates = [
        pack
        for pack in packs
        if pack.get("installable")
        and str(pack.get("pack_id")) not in installed
        and pack_matches_filters(pack, grade=grade, subject=subject, language=language)
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
        "pack_stats": [pack_stat(pack) for pack in recommended],
    }


@router.post("/packs/generate")
async def packs_generate(payload: dict[str, Any]) -> Any:
    return await post_json(settings.pack_service_url, "/packs/generate", payload)


@router.get("/packs/{pack_id}/manifest")
async def pack_manifest(pack_id: str) -> dict[str, Any]:
    try:
        source_pack_id = await resolve_pack_service_pack_id(pack_id)
        manifest = await get_json(settings.pack_service_url, pack_path(source_pack_id, "manifest"))
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

    pack = await get_json(settings.pihub_url, f"/packs/{pack_id}")
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
            "updated_at": iso_from_epoch(pack.get("updated_at")),
            **(pack.get("metadata") or {}),
        },
        "chunk_count": 0,
        "flashcard_count": 0,
        "quiz_count": 0,
        "glossary_count": 0,
        "summary_count": 0,
        "artifact_counts": {},
    }


@router.get("/packs/{pack_id}/download")
async def pack_download(pack_id: str) -> StreamingResponse:
    try:
        source_pack_id = await resolve_pack_service_pack_id(pack_id)
        return await proxy_stream(settings.pack_service_url, "GET", pack_path(source_pack_id, "download"))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    return await proxy_stream(settings.pihub_url, "GET", pack_path(pack_id, "download"))
