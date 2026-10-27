"""Canonical pack catalog aggregation across pack-service and pihub."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from shared.config import get_settings

from app.services.upstream import get_json


logger = logging.getLogger(__name__)

settings = get_settings()


def iso_from_epoch(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def pack_path(pack_id: Any, suffix: str) -> str:
    return f"/packs/{quote(str(pack_id), safe='')}/{suffix.lstrip('/')}"


def pack_checksum(pack: dict[str, Any]) -> str:
    return str(pack.get("checksum") or pack.get("hash") or pack.get("content_checksum") or "")


def normalize_pack_public_id(pack_id: Any) -> str:
    normalized = str(pack_id or "").lower()
    normalized = normalized.replace("\u2013", "_").replace("\u2014", "_")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "pack"


def pack_id_suffix(pack: dict[str, Any]) -> str:
    checksum = pack_checksum(pack).replace("sha256:", "")
    checksum = re.sub(r"[^a-zA-Z0-9]", "", checksum)
    if checksum:
        return checksum[:8].lower()
    return str(abs(hash(str(pack.get("pack_id") or ""))))[:8]


def public_pack_id(raw_pack_id: Any, pack: dict[str, Any], used_ids: set[str]) -> str:
    base_id = normalize_pack_public_id(raw_pack_id)
    public_id = base_id
    if public_id in used_ids:
        public_id = f"{base_id}_{pack_id_suffix(pack)}"
    counter = 2
    while public_id in used_ids:
        public_id = f"{base_id}_{pack_id_suffix(pack)}_{counter}"
        counter += 1
    used_ids.add(public_id)
    return public_id


def canonical_pack_entry(pack: dict[str, Any], public_pack_id: str, source: str | None = None) -> dict[str, Any]:
    checksum = pack_checksum(pack)
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
    subject = normalize_pack_subject(pack.get("subject"))
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
        "manifest_url": pack_path(public_pack_id, "manifest"),
        "download_url": pack_path(public_pack_id, "download"),
    }
    if source:
        entry["source"] = source
    return entry


def canonical_pack_entries(
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
        public_id = public_pack_id(raw_pack_id, pack, used)
        entries.append(canonical_pack_entry(pack, public_id, source))
        source_ids[public_id] = raw_pack_id
    return entries, source_ids


def filter_value(value: Any) -> str:
    return str(value or "").strip().lower()


def curriculum_pack_key(pack: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(pack.get("grade") or "").strip(),
        filter_value(pack.get("subject")),
        filter_value(pack.get("chapter")),
        filter_value(pack.get("language") or "english"),
    )


def pack_publication_rank(pack: dict[str, Any]) -> tuple[int, int, int, int, str]:
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


def pack_quality_passed(pack: dict[str, Any]) -> bool:
    quality_scores = pack.get("quality_scores") or {}
    return bool(quality_scores.get("quality_gate_passed") or quality_scores.get("retrieval_precision"))


def normalize_pack_subject(subject: Any) -> str:
    normalized = filter_value(subject)
    if normalized in {"math", "mathematics", "maths"}:
        return "maths"
    if normalized in {"science"}:
        return "science"
    if normalized in {"social", "social science", "social_science"}:
        return "social_science"
    return normalized


def is_certified_curriculum_pack(pack: dict[str, Any]) -> bool:
    counts = pack.get("artifact_counts") or {}
    subject = normalize_pack_subject(pack.get("subject"))
    return (
        subject in {"maths", "science", "social_science"}
        and bool(pack.get("installable"))
        and int(counts.get("content") or 0) > 0
    )


def dedupe_curriculum_packs(packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for pack in packs:
        if not is_certified_curriculum_pack(pack):
            continue
        key = curriculum_pack_key(pack)
        if not all(key):
            passthrough.append(pack)
            continue
        existing = selected.get(key)
        if existing is None or pack_publication_rank(pack) > pack_publication_rank(existing):
            selected[key] = pack
    return [*selected.values(), *passthrough]


async def pack_service_packs(params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        data = await get_json(settings.pack_service_url, "/packs/search" if params else "/packs", params=params)
    except HTTPException as exc:
        # Outage is NOT an empty catalog: log loudly so [] is never mistaken
        # for "no packs installed". Callers keep the 200 shape.
        logger.warning("[PACK] PACK_SERVICE_UNAVAILABLE status=%s detail=%s", exc.status_code, exc.detail)
        return []
    packs = data.get("packs", [])
    return packs if isinstance(packs, list) else []


async def resolve_pack_service_pack_id(pack_id: str) -> str:
    packs = await pack_service_packs()
    used_ids: set[str] = set()
    _, source_ids = canonical_pack_entries(packs, "pack_service", used_ids)
    if pack_id in source_ids:
        return source_ids[pack_id]
    for pack in packs:
        raw_pack_id = str(pack.get("pack_id") or "")
        if pack_id == raw_pack_id:
            return raw_pack_id
    return pack_id


async def canonical_pack_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    cached: list[dict[str, Any]] = []

    pack_service_list = await pack_service_packs()
    pack_service_entries, _ = canonical_pack_entries(pack_service_list, "pack_service", used_ids)
    pack_service_entries = dedupe_curriculum_packs(pack_service_entries)
    for entry in pack_service_entries:
        records[entry["pack_id"]] = entry

    pihub_count = 0
    try:
        pihub_packs = await get_json(settings.pihub_url, "/packs")
        pihub_entries, _ = canonical_pack_entries(pihub_packs.get("packs", []), "pihub", used_ids)
        pihub_entries = dedupe_curriculum_packs(pihub_entries)
        for entry in pihub_entries:
            if entry["pack_id"] not in records:
                pihub_count += 1
            records.setdefault(entry["pack_id"], entry)
        cached, _ = canonical_pack_entries(pihub_packs.get("cached", []), "pihub_cache", used_ids)
    except HTTPException as exc:
        logger.warning("[PACK] PIHUB_PACK_LIST_UNAVAILABLE status=%s detail=%s", exc.status_code, exc.detail)

    return list(records.values()), cached, {
        "pack_service": len(pack_service_list),
        "pihub": pihub_count,
        "pihub_cache": len(cached),
    }


def pack_stat(pack: dict[str, Any]) -> dict[str, Any]:
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


def pack_matches_filters(
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
    if subject is not None and filter_value(pack.get("subject")) != filter_value(subject):
        return False
    if language is not None and filter_value(pack.get("language")) != filter_value(language):
        return False
    return True


async def fetch_pack_preview(pack_id: str) -> dict[str, Any] | None:
    try:
        res = await get_json(settings.pack_service_url, f"/packs/{pack_id}/preview")
        return res if isinstance(res, dict) else None
    except Exception:
        return None
