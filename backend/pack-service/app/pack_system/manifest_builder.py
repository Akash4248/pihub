from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .checksum_generator import ChecksumGenerator
from .version_manager import VersionManager
from shared.text_normalization import normalize_curriculum_name, normalize_language_code


class ManifestBuilder:
    def __init__(self, retrieval_index_version: str = "v2") -> None:
        self.retrieval_index_version = retrieval_index_version

    def build(
        self,
        *,
        pack_id: str,
        grade: int | None,
        subject: str | None,
        chapter: str | None,
        language: str | None,
        version: str,
        artifact_counts: dict[str, int],
        generation_metadata: dict[str, Any] | None = None,
        content_checksum_source: dict[str, Any] | None = None,
        quality_scores: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        normalized_version = VersionManager.normalize(version)
        manifest: dict[str, Any] = {
            "pack_id": pack_id,
            "version": normalized_version,
            "grade": grade,
            "subject": normalize_curriculum_name(subject) if subject is not None else None,
            "chapter": normalize_curriculum_name(chapter) if chapter is not None else None,
            "language": (normalize_language_code(language) or normalize_curriculum_name(language)) if language is not None else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retrieval_index_version": self.retrieval_index_version,
            "artifact_counts": artifact_counts,
            "generation_metadata": generation_metadata or {},
        }
        if content_checksum_source is not None:
            manifest["content_checksum"] = ChecksumGenerator.checksum_dict(content_checksum_source)
        if quality_scores:
            manifest["quality_scores"] = quality_scores
        checksum_source = {key: value for key, value in manifest.items() if key != "checksum"}
        manifest["checksum"] = ChecksumGenerator.checksum_dict(checksum_source)
        return manifest
