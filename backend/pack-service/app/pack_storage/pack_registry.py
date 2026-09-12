from __future__ import annotations

from pathlib import Path
from typing import Any

from ..pack_system.pack_metadata_store import PackMetadataStore


class PackRegistry:
    def __init__(self, storage_root: Path) -> None:
        self.store = PackMetadataStore(storage_root / "pack_index.json")

    def register(self, manifest: dict[str, Any], pack_dir: str, archive_path: str) -> dict[str, Any]:
        record = {
            "pack_id": manifest.get("pack_id"),
            "version": manifest.get("version"),
            "grade": manifest.get("grade"),
            "subject": manifest.get("subject"),
            "chapter": manifest.get("chapter"),
            "language": manifest.get("language"),
            "checksum": manifest.get("checksum"),
            "content_checksum": manifest.get("content_checksum"),
            "artifact_counts": manifest.get("artifact_counts", {}),
            "retrieval_index_version": manifest.get("retrieval_index_version"),
            "generated_at": manifest.get("generated_at"),
            "quality_scores": manifest.get("quality_scores", {}),
            "generation_metadata": manifest.get("generation_metadata", {}),
            "pack_dir": pack_dir,
            "archive_path": archive_path,
        }
        self.store.upsert(record)
        return record

    def list(self) -> list[dict[str, Any]]:
        return self.store.list()

    def get(self, pack_id: str, version: str | None = None) -> dict[str, Any] | None:
        return self.store.get(pack_id, version)

    def remove(self, pack_id: str, version: str | None = None) -> None:
        self.store.remove(pack_id, version)

    def search(self, **criteria: Any) -> list[dict[str, Any]]:
        from ..pack_system.version_manager import VersionManager
        records = self.store.list()
        results: list[dict[str, Any]] = []
        for record in records:
            matched = True
            for key, expected in criteria.items():
                if expected is None:
                    continue
                actual = record.get(key)
                if key == "version" and isinstance(expected, str) and isinstance(actual, str):
                    try:
                        if VersionManager.normalize(actual) != VersionManager.normalize(expected):
                            matched = False
                            break
                    except ValueError:
                        if actual != expected:
                            matched = False
                            break
                elif actual != expected:
                    matched = False
                    break
            if matched:
                results.append(record)
        return results
