from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from app.events.event_store import ConcurrencyException, PackEventStore
from app.events.projections import PackProjections

logger = logging.getLogger(__name__)


class PackEventService:
    def __init__(self, db_path: str | Path):
        self.store = PackEventStore(db_path)
        self.projections = PackProjections(db_path)

    def record_pack_created(
        self,
        pack_id: str,
        version: str,
        grade: Optional[int] = None,
        subject: Optional[str] = None,
        chapter: Optional[str] = None,
        checksum: Optional[str] = None,
        size_bytes: int = 0,
        chunk_count: int = 0,
        actor_id: Optional[str] = "pack_generator",
        actor_type: Optional[str] = "system",
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record PackCreated event when a pack is generated and saved."""
        current_version = self.store.get_latest_version(pack_id)
        if current_version > 0:
            # Check if this pack & version already has a PackCreated event
            events = self.store.get_events(pack_id)
            for ev in events:
                if ev.get("event_type") == "PackCreated" and ev.get("payload", {}).get("version") == version:
                    return ev

        next_version = current_version + 1
        payload = {
            "version": version,
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "chunk_count": chunk_count,
        }
        idem_key = idempotency_key or f"created_{pack_id}_v{version}"
        event = self.store.append_event(
            aggregate_id=pack_id,
            expected_version=next_version,
            event_type="PackCreated",
            actor_id=actor_id,
            actor_type=actor_type,
            payload=payload,
            idempotency_key=idem_key,
        )
        self.projections.apply_event(event)
        return event

    def record_pack_published(
        self,
        pack_id: str,
        version: Optional[str] = None,
        publisher_id: Optional[str] = "admin",
        actor_type: Optional[str] = "admin",
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Publish a pack, transitioning it from draft to published."""
        current_state = self.projections.get_pack_state(pack_id, version)
        if not current_state:
            raise ValueError(f"Pack '{pack_id}' does not exist in event store. Generate it first.")

        target_version = version or current_state["version"]
        if current_state.get("publication_status") == "published":
            # Already published, return current state
            return current_state

        next_version = self.store.get_latest_version(pack_id) + 1
        payload = {
            "version": target_version,
            "publisher": publisher_id,
            "notes": notes,
        }
        idem_key = idempotency_key or f"published_{pack_id}_v{target_version}"
        event = self.store.append_event(
            aggregate_id=pack_id,
            expected_version=next_version,
            event_type="PackPublished",
            actor_id=publisher_id,
            actor_type=actor_type,
            payload=payload,
            idempotency_key=idem_key,
        )
        self.projections.apply_event(event)
        return event

    def record_pack_downloaded(
        self,
        pack_id: str,
        version: str,
        classroom_id: Optional[str] = None,
        device_id: Optional[str] = None,
        actor_type: Optional[str] = "classroom_node",
        request_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record when a node or client requests and downloads a pack archive."""
        next_version = self.store.get_latest_version(pack_id) + 1
        payload = {
            "version": version,
            "request_id": request_id,
            "client_ip": client_ip,
        }
        event = self.store.append_event(
            aggregate_id=pack_id,
            expected_version=next_version,
            event_type="PackDownloaded",
            actor_id=classroom_id or "anonymous_node",
            actor_type=actor_type,
            classroom_id=classroom_id or "default_classroom",
            device_id=device_id or "unknown_device",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self.projections.apply_event(event)
        return event

    def record_pack_synchronized(
        self,
        pack_id: str,
        version: str,
        classroom_id: str,
        device_id: str,
        checksum: str,
        status: str = "success",
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record acknowledgment from node that a pack was successfully stored & verified."""
        idem_key = idempotency_key or f"sync_{pack_id}_v{version}_{classroom_id}_{device_id}"
        next_version = self.store.get_latest_version(pack_id) + 1
        payload = {
            "version": version,
            "checksum": checksum,
            "status": status,
        }
        event = self.store.append_event(
            aggregate_id=pack_id,
            expected_version=next_version,
            event_type="PackSynchronized",
            actor_id=device_id,
            actor_type="classroom_node",
            classroom_id=classroom_id,
            device_id=device_id,
            payload=payload,
            idempotency_key=idem_key,
        )
        self.projections.apply_event(event)
        return event

    def record_pack_updated(
        self,
        pack_id: str,
        previous_version: str,
        new_version: str,
        checksum: Optional[str] = None,
        actor_id: Optional[str] = "pack_generator",
    ) -> dict[str, Any]:
        """Record PackUpdated when a newer version supersedes an existing version."""
        next_version = self.store.get_latest_version(pack_id) + 1
        payload = {
            "previous_version": previous_version,
            "version": new_version,
            "checksum": checksum,
        }
        event = self.store.append_event(
            aggregate_id=pack_id,
            expected_version=next_version,
            event_type="PackUpdated",
            actor_id=actor_id,
            actor_type="system",
            payload=payload,
        )
        self.projections.apply_event(event)
        return event

    def get_history(self, pack_id: str) -> list[dict[str, Any]]:
        return self.store.get_events(pack_id)

    def get_status(self, pack_id: str, version: Optional[str] = None) -> Optional[dict[str, Any]]:
        return self.projections.get_pack_state(pack_id, version)

    def get_classroom_packs(self, classroom_id: str) -> list[dict[str, Any]]:
        return self.projections.get_classroom_packs(classroom_id)

    def get_analytics(self) -> dict[str, Any]:
        return self.projections.get_distribution_analytics()

    def replay_and_rebuild(self) -> dict[str, Any]:
        """Event Sourcing core capability: wipe projections and replay full event stream."""
        all_events = self.store.get_all_events()
        self.projections.rebuild_projections(all_events)
        return {"replayed_events_count": len(all_events), "status": "rebuilt_successfully"}
