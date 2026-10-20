"""
Synchronization Hardening Service

Handles:
- Persistent sync queue recovery
- Interrupted transfer recovery
- Resumable synchronization
- Retry persistence
- Reconnect-aware sync coordination
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from shared.file_utils import atomic_write_json


class PersistentSyncQueue:
    """Persistent synchronization queue that survives reboot"""

    def __init__(self, queue_file: Path | None = None) -> None:
        self.queue_file = queue_file or Path(os.getenv("STORAGE_DIR", "/storage")) / "sync_queue_persistent.json"
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_queue: list[dict[str, Any]] = []
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load queue from persistent storage"""
        if self.queue_file.exists():
            try:
                data = json.loads(self.queue_file.read_text())
                self.memory_queue = data.get("queue", [])
            except Exception:
                self.memory_queue = []

    def _persist_to_disk(self) -> None:
        """Write queue to persistent storage (crash-safe: tmp + fsync + rename)"""
        data = {"queue": self.memory_queue, "saved_at": int(time.time())}
        atomic_write_json(self.queue_file, data)

    def enqueue(self, job: dict[str, Any]) -> None:
        """Add job to persistent queue"""
        self.memory_queue.append(job)
        self._persist_to_disk()

    def dequeue(self) -> dict[str, Any] | None:
        """Remove and return first job from queue"""
        if not self.memory_queue:
            return None
        job = self.memory_queue.pop(0)
        self._persist_to_disk()
        return job

    def peek(self, count: int = 10) -> list[dict[str, Any]]:
        """Peek at next N jobs without removing"""
        return self.memory_queue[:count]

    def get_size(self) -> int:
        """Get queue size"""
        return len(self.memory_queue)

    def clear(self) -> None:
        """Clear entire queue"""
        self.memory_queue = []
        self._persist_to_disk()


class TransferRecoveryCoordinator:
    """Coordinate recovery of interrupted transfers"""

    def __init__(self, recovery_file: Path | None = None) -> None:
        storage_base = Path(os.getenv("STORAGE_DIR")) if os.getenv("STORAGE_DIR") else (Path(__file__).resolve().parents[3] / ".local_run" / "pihub_storage")
        self.recovery_file = recovery_file or (storage_base / "transfer_recovery.json")
        try:
            self.recovery_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.recovery_map: dict[str, dict[str, Any]] = {}
        self._load_recovery_state()

    def _load_recovery_state(self) -> None:
        """Load recovery state from disk"""
        if self.recovery_file.exists():
            try:
                data = json.loads(self.recovery_file.read_text())
                self.recovery_map = data.get("recovery_map", {})
            except Exception:
                self.recovery_map = {}

    def _persist_recovery_state(self) -> None:
        """Save recovery state to disk (crash-safe: tmp + fsync + rename)"""
        data = {
            "recovery_map": self.recovery_map,
            "persisted_at": int(time.time()),
        }
        atomic_write_json(self.recovery_file, data)

    def start_transfer(
        self,
        transfer_id: str,
        device_id: str,
        pack_id: str,
        total_bytes: int,
    ) -> dict[str, Any]:
        """Start tracking a transfer"""
        recovery_record = {
            "transfer_id": transfer_id,
            "device_id": device_id,
            "pack_id": pack_id,
            "total_bytes": total_bytes,
            "bytes_transferred": 0,
            "started_at": int(time.time()),
            "last_chunk_index": 0,
            "status": "in_progress",
            "retries": 0,
        }
        self.recovery_map[transfer_id] = recovery_record
        self._persist_recovery_state()
        return recovery_record

    def update_transfer_progress(
        self,
        transfer_id: str,
        bytes_transferred: int,
        chunk_index: int,
    ) -> dict[str, Any] | None:
        """Update transfer progress and persist"""
        if transfer_id not in self.recovery_map:
            return None

        record = self.recovery_map[transfer_id]
        record["bytes_transferred"] = bytes_transferred
        record["last_chunk_index"] = chunk_index
        record["last_update"] = int(time.time())
        self._persist_recovery_state()
        return record

    def mark_transfer_complete(self, transfer_id: str) -> dict[str, Any] | None:
        """Mark transfer as complete"""
        if transfer_id not in self.recovery_map:
            return None

        record = self.recovery_map[transfer_id]
        record["status"] = "complete"
        record["completed_at"] = int(time.time())
        self._persist_recovery_state()
        return record

    def mark_transfer_failed(self, transfer_id: str, error: str) -> dict[str, Any] | None:
        """Mark transfer as failed but recoverable"""
        if transfer_id not in self.recovery_map:
            return None

        record = self.recovery_map[transfer_id]
        record["status"] = "interrupted"
        record["error"] = error
        record["last_error_at"] = int(time.time())
        record["retries"] = record.get("retries", 0) + 1
        self._persist_recovery_state()
        return record

    def get_interrupted_transfers(self) -> list[dict[str, Any]]:
        """Get all interrupted transfers ready for recovery"""
        interrupted = [
            record
            for record in self.recovery_map.values()
            if record.get("status") == "interrupted" and record.get("retries", 0) < 3
        ]
        return sorted(interrupted, key=lambda x: x.get("last_error_at", 0))

    def cleanup_old_transfers(self, days_old: int = 7) -> int:
        """Remove old transfer recovery records"""
        cutoff_time = int(time.time()) - (days_old * 86400)
        old_transfers = [
            tid
            for tid, record in self.recovery_map.items()
            if record.get("status") == "complete" and record.get("completed_at", 0) < cutoff_time
        ]

        for tid in old_transfers:
            del self.recovery_map[tid]

        if old_transfers:
            self._persist_recovery_state()

        return len(old_transfers)

    def get_recovery_status(self) -> dict[str, Any]:
        """Get overall recovery status"""
        in_progress = sum(1 for r in self.recovery_map.values() if r.get("status") == "in_progress")
        interrupted = sum(1 for r in self.recovery_map.values() if r.get("status") == "interrupted")
        complete = sum(1 for r in self.recovery_map.values() if r.get("status") == "complete")

        return {
            "total_tracked": len(self.recovery_map),
            "in_progress": in_progress,
            "interrupted": interrupted,
            "complete": complete,
        }


class SyncDiagnosticsService:
    """Provide synchronization diagnostics and recovery insights"""

    def __init__(
        self,
        persistent_queue: PersistentSyncQueue,
        recovery_coordinator: TransferRecoveryCoordinator,
    ) -> None:
        self.queue = persistent_queue
        self.recovery = recovery_coordinator

    def get_sync_health(self) -> dict[str, Any]:
        """Get overall synchronization health"""
        queue_status = {
            "queue_size": self.queue.get_size(),
            "oldest_job": self.queue.peek(1)[0].get("created_at") if self.queue.peek(1) else None,
        }

        recovery_status = self.recovery.get_recovery_status()

        queue_backlog_minutes = 0
        if queue_status["oldest_job"]:
            queue_backlog_minutes = (int(time.time()) - queue_status["oldest_job"]) // 60

        return {
            "queue": queue_status,
            "recovery": recovery_status,
            "queue_backlog_minutes": queue_backlog_minutes,
            "status": (
                "healthy"
                if queue_status["queue_size"] < 10 and recovery_status["interrupted"] < 5
                else "degraded"
            ),
        }

    def get_sync_diagnostics(self) -> dict[str, Any]:
        """Get detailed sync diagnostics"""
        return {
            "health": self.get_sync_health(),
            "interrupted_transfers": self.recovery.get_interrupted_transfers(),
            "pending_queue_sample": self.queue.peek(10),
        }
