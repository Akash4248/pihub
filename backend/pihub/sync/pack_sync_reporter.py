from __future__ import annotations

import logging
import os
from typing import Any, Optional
import httpx

logger = logging.getLogger(__name__)

PACK_SERVICE_URL = os.getenv("PACK_SERVICE_URL", "http://localhost:8030")
INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY", "c8387e80d8d42210e6fc10ef781f5f12c63e5f5321a8ec69ad4009c52faa6ca7")
DEFAULT_CLASSROOM_ID = os.getenv("PIHUB_CLASSROOM_NAME", "classroom_a").lower().replace(" ", "_")


class PackSyncReporter:
    """Dispatches verified synchronization acknowledgments to the Pack Service Event Store."""

    def __init__(
        self,
        pack_service_url: Optional[str] = None,
        internal_key: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.pack_service_url = (pack_service_url or PACK_SERVICE_URL).rstrip("/")
        self.internal_key = internal_key or INTERNAL_SERVICE_KEY
        self.timeout = timeout

    async def report_synchronization(
        self,
        pack_id: str,
        version: str,
        classroom_id: Optional[str] = None,
        device_id: Optional[str] = None,
        checksum: Optional[str] = None,
        status: str = "success",
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send PackSynchronized acknowledgment to Pack Service."""
        endpoint = f"{self.pack_service_url}/internal/packs/{pack_id}/synchronization"
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Service-Key": self.internal_key,
        }
        room = classroom_id or DEFAULT_CLASSROOM_ID
        dev = device_id or os.getenv("PIHUB_DEVICE_ID", "pi_node_master")
        chk = checksum or "verified"
        payload = {
            "version": version,
            "classroom_id": room,
            "device_id": dev,
            "checksum": chk,
            "status": status,
            "idempotency_key": idempotency_key or f"sync_{pack_id}_v{version}_{room}_{dev}",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("[SYNC_REPORTER] Failed to notify Pack Service of sync for %s: %s", pack_id, exc)
            return {"status": "failed", "error": str(exc)}

    def report_synchronization_sync(
        self,
        pack_id: str,
        version: str,
        classroom_id: Optional[str] = None,
        device_id: Optional[str] = None,
        checksum: Optional[str] = None,
        status: str = "success",
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synchronous version for threads and worker scripts."""
        endpoint = f"{self.pack_service_url}/internal/packs/{pack_id}/synchronization"
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Service-Key": self.internal_key,
        }
        room = classroom_id or DEFAULT_CLASSROOM_ID
        dev = device_id or os.getenv("PIHUB_DEVICE_ID", "pi_node_master")
        chk = checksum or "verified"
        payload = {
            "version": version,
            "classroom_id": room,
            "device_id": dev,
            "checksum": chk,
            "status": status,
            "idempotency_key": idempotency_key or f"sync_{pack_id}_v{version}_{room}_{dev}",
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("[SYNC_REPORTER] Failed to notify Pack Service of sync for %s: %s", pack_id, exc)
            return {"status": "failed", "error": str(exc)}
