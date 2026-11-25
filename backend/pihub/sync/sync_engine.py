from __future__ import annotations

import json
import time
from typing import Any


class SyncEngine:
    """Educational classroom synchronization engine."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.sync_batch_size = 10
        self.max_retries = 5

    def process_pending_syncs(self) -> dict[str, Any]:
        """Process pending sync queue items."""
        pending = self.store.list_sync_queue("pending")
        processed = 0
        failed = 0

        for item in pending[: self.sync_batch_size]:
            try:
                self._process_sync_item(item)
                processed += 1
            except Exception as e:
                failed += 1
                retry_count = item.get("retry_count", 0)
                if retry_count < self.max_retries:
                    self.store.mark_sync_queue_status(item["queue_id"], "pending")
                else:
                    self.store.mark_sync_queue_status(item["queue_id"], "failed")

        return {"processed": processed, "failed": failed, "pending": len(pending) - processed}

    def _process_sync_item(self, item: dict[str, Any]) -> None:
        """Process a single sync queue item."""
        action = item.get("action")
        resource_type = item.get("resource_type")

        if action == "distribute_pack":
            self._handle_pack_distribution(item)
        elif action == "push_progress":
            self._handle_progress_push(item)
        elif action == "pull_assignments":
            self._handle_assignment_pull(item)

        self.store.mark_sync_queue_status(item["queue_id"], "completed")

    def _handle_pack_distribution(self, item: dict[str, Any]) -> None:
        """Handle educational pack distribution to devices."""
        resource_id = item.get("resource_id")
        target_devices = item.get("target_devices", [])
        pack = self.store.get_cached_pack(resource_id) or self.store.get_pack(resource_id)
        if not pack:
            return

        for device_id in target_devices:
            self.store.start_session(
                {
                    "device_id": device_id,
                    "resource_type": "pack",
                    "resource_id": resource_id,
                    "offset_bytes": 0,
                    "total_bytes": pack.get("size_bytes"),
                    "status": "pending",
                    "checksum": pack.get("checksum"),
                    "metadata": {"pack_version": pack.get("version")},
                }
            )

        try:
            from pihub.sync.pack_sync_reporter import PackSyncReporter
            PackSyncReporter().report_synchronization_sync(
                pack_id=resource_id,
                version=pack.get("version", "1.0.0"),
                checksum=pack.get("checksum"),
            )
        except Exception:
            pass

    def _handle_progress_push(self, item: dict[str, Any]) -> None:
        """Handle pushing student progress to backend."""
        pass

    def _handle_assignment_pull(self, item: dict[str, Any]) -> None:
        """Handle pulling assignments from backend."""
        pass

    def broadcast_to_classroom(self, action: str, resource_type: str, resource_id: str, classroom: str | None = None) -> str:
        """Broadcast a sync action to all classroom devices."""
        if classroom:
            devices = []
            for device in self.store.list_devices():
                if device.get("classroom") == classroom:
                    devices.append(device["device_id"])
        else:
            devices = [d["device_id"] for d in self.store.list_devices() if d["status"] == "online"]

        return self.store.enqueue_sync(action, resource_type, resource_id, devices, {
            "broadcast": True,
            "target_classroom": classroom,
            "timestamp": int(time.time()),
        }).get("queue_id")

    def handle_device_offline(self, device_id: str) -> None:
        """Handle device going offline - preserve sync state."""
        device = self.store.get_device(device_id)
        if device:
            self.store.heartbeat(device_id)

    def handle_device_online(self, device_id: str) -> None:
        """Handle device coming online - resume pending syncs."""
        sessions = self.store.list_sync_queue()
        device_sessions = [s for s in sessions if device_id in s.get("target_devices", [])]
        if device_sessions:
            self.store.mark_sync_queue_status(device_sessions[0]["queue_id"], "ready")

    def get_sync_status(self) -> dict[str, Any]:
        """Get overall sync system status."""
        pending = self.store.list_sync_queue("pending")
        in_progress = self.store.list_sync_queue("transferring")
        completed = self.store.list_sync_queue("complete")
        failed = self.store.list_sync_queue("failed")

        return {
            "queue_status": {
                "pending": len(pending),
                "in_progress": len(in_progress),
                "completed": len(completed),
                "failed": len(failed),
            },
            "total_items": len(pending) + len(in_progress) + len(completed) + len(failed),
            "oldest_pending": pending[0]["created_at"] if pending else None,
        }
