"""
Real Cache Management Service

Handles:
- Cache indexing and lookup
- Cache validation
- Metadata persistence
- Cache cleanup and eviction
- Storage diagnostics
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    """Cache entry metadata"""

    cache_id: str
    pack_id: str
    pack_name: str
    version: str
    file_path: str
    file_size: int = 0
    checksum: str = ""
    subject: str | None = None
    grade: int | None = None
    chapter: str | None = None
    cached_at: int = 0
    last_accessed: int = 0
    access_count: int = 0
    validated_at: int = 0
    is_valid: bool = True
    metadata: dict[str, Any] | None = None


class CacheManager:
    """Real cache management service"""

    def __init__(self, storage_dir: Path, max_cache_size_mb: int = 500) -> None:
        self.storage_dir = storage_dir
        self.cache_dir = storage_dir / "cache"
        self.metadata_dir = storage_dir / "cache_metadata"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

        self.max_cache_size_bytes = max_cache_size_mb * 1024 * 1024
        self.cache_index: dict[str, CacheEntry] = {}
        self._load_cache_metadata()

    def _load_cache_metadata(self) -> None:
        """Load cache metadata from disk"""
        metadata_file = self.metadata_dir / "cache_index.json"
        if metadata_file.exists():
            try:
                data = json.loads(metadata_file.read_text())
                for item in data:
                    entry = CacheEntry(**item)
                    self.cache_index[entry.cache_id] = entry
            except Exception:
                pass

    def _save_cache_metadata(self) -> None:
        """Persist cache metadata atomically"""
        metadata_file = self.metadata_dir / "cache_index.json"
        data = [asdict(entry) for entry in self.cache_index.values()]
        tmp_file = metadata_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            with open(tmp_file, "r") as f:
                os.fsync(f.fileno())
        except Exception:
            pass
        tmp_file.replace(metadata_file)

    def _get_cache_usage(self) -> int:
        """Get total cache usage in bytes"""
        total = 0
        for entry in self.cache_index.values():
            if entry.is_valid and (self.cache_dir / Path(entry.file_path).name).exists():
                total += entry.file_size
        return total

    def cache_pack(
        self,
        pack_id: str,
        pack_name: str,
        version: str,
        file_path: str,
        checksum: str,
        file_size: int,
        subject: str | None = None,
        grade: int | None = None,
        chapter: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CacheEntry:
        """Cache educational pack"""
        cache_id = str(uuid.uuid4())
        now = int(time.time())

        cache_entry = CacheEntry(
            cache_id=cache_id,
            pack_id=pack_id,
            pack_name=pack_name,
            version=version,
            file_path=file_path,
            file_size=file_size,
            checksum=checksum,
            subject=subject,
            grade=grade,
            chapter=chapter,
            cached_at=now,
            last_accessed=now,
            validated_at=now,
            metadata=metadata,
        )

        self.cache_index[cache_id] = cache_entry
        self._save_cache_metadata()
        self._check_and_evict_if_needed(file_size)

        return cache_entry

    def get_cached_pack(self, pack_id: str) -> CacheEntry | None:
        """Get cached pack if available"""
        for entry in self.cache_index.values():
            if entry.pack_id == pack_id and entry.is_valid:
                entry.access_count += 1
                entry.last_accessed = int(time.time())
                self._save_cache_metadata()
                return entry
        return None

    def validate_cache_entry(self, cache_id: str, expected_checksum: str) -> bool:
        """Validate cache entry integrity"""
        entry = self.cache_index.get(cache_id)
        if not entry:
            return False

        actual_checksum = entry.checksum
        is_valid = actual_checksum == expected_checksum

        entry.is_valid = is_valid
        entry.validated_at = int(time.time())
        self._save_cache_metadata()

        return is_valid

    def invalidate_cache_entry(self, cache_id: str) -> None:
        """Mark cache entry invalid"""
        if cache_id in self.cache_index:
            self.cache_index[cache_id].is_valid = False
            self._save_cache_metadata()

    def remove_from_cache(self, cache_id: str) -> bool:
        """Remove entry from cache"""
        entry = self.cache_index.get(cache_id)
        if not entry:
            return False

        try:
            cache_file = self.cache_dir / Path(entry.file_path).name
            if cache_file.exists():
                cache_file.unlink()
        except Exception:
            pass

        del self.cache_index[cache_id]
        self._save_cache_metadata()
        return True

    def _check_and_evict_if_needed(self, needed_bytes: int) -> None:
        """Evict old cache entries if needed"""
        current_usage = self._get_cache_usage()
        if current_usage + needed_bytes > self.max_cache_size_bytes:
            self._evict_lru()

    def _evict_lru(self) -> int:
        """Evict least recently used entries"""
        evicted_count = 0
        sorted_entries = sorted(
            [e for e in self.cache_index.values() if e.is_valid],
            key=lambda x: x.last_accessed,
        )

        for entry in sorted_entries:
            self.remove_from_cache(entry.cache_id)
            evicted_count += 1

            if self._get_cache_usage() < self.max_cache_size_bytes * 0.8:
                break

        return evicted_count

    def list_cached_packs(self) -> list[dict[str, Any]]:
        """List all cached packs"""
        return [asdict(entry) for entry in self.cache_index.values() if entry.is_valid]

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        valid_entries = [e for e in self.cache_index.values() if e.is_valid]
        total_size = sum(e.file_size for e in valid_entries)
        total_accesses = sum(e.access_count for e in valid_entries)

        return {
            "total_entries": len(valid_entries),
            "total_size_bytes": total_size,
            "max_size_bytes": self.max_cache_size_bytes,
            "usage_percent": (total_size / self.max_cache_size_bytes * 100) if self.max_cache_size_bytes > 0 else 0,
            "total_accesses": total_accesses,
            "avg_entry_age_seconds": (
                sum(int(time.time()) - e.cached_at for e in valid_entries) // len(valid_entries)
                if valid_entries
                else 0
            ),
        }

    def cleanup_stale_entries(self, days_old: int = 30) -> int:
        """Remove stale cache entries"""
        cutoff_time = int(time.time()) - (days_old * 86400)
        removed_count = 0

        cache_ids_to_remove = [
            cache_id
            for cache_id, entry in self.cache_index.items()
            if entry.last_accessed < cutoff_time
        ]

        for cache_id in cache_ids_to_remove:
            self.remove_from_cache(cache_id)
            removed_count += 1

        return removed_count

    def touch_cached_pack(self, pack_id: str) -> None:
        """Update access time for cached pack"""
        for entry in self.cache_index.values():
            if entry.pack_id == pack_id and entry.is_valid:
                entry.last_accessed = int(time.time())
                entry.access_count += 1
                self._save_cache_metadata()
                break

    def get_cache_health(self) -> dict[str, Any]:
        """Get overall cache health"""
        valid_entries = [e for e in self.cache_index.values() if e.is_valid]
        invalid_entries = [e for e in self.cache_index.values() if not e.is_valid]

        return {
            "healthy": len(invalid_entries) == 0,
            "valid_entries": len(valid_entries),
            "invalid_entries": len(invalid_entries),
            "total_size_mb": sum(e.file_size for e in valid_entries) / (1024 * 1024),
            "max_size_mb": self.max_cache_size_bytes / (1024 * 1024),
        }
