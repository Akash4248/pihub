"""
Real Device Management Service

Handles:
- Device registration lifecycle
- Session management  
- Heartbeat tracking
- Reconnect recovery
- Classroom grouping
- Device state persistence
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class DeviceStatus(str, Enum):
    """Device connection status"""

    DISCOVERED = "discovered"
    REGISTERING = "registering"
    REGISTERED = "registered"
    ONLINE = "online"
    IDLE = "idle"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class DeviceRole(str, Enum):
    """Device role in classroom"""

    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"
    OBSERVER = "observer"


@dataclass
class DeviceSession:
    """Device session tracking"""

    session_id: str
    device_id: str
    device_name: str
    role: str
    classroom_id: str
    status: str = "active"
    created_at: int = 0
    last_heartbeat: int = 0
    heartbeat_interval: int = 30
    max_offline_time: int = 300
    consecutive_failures: int = 0
    metadata: dict[str, Any] | None = None

    def is_healthy(self) -> bool:
        """Check if session is healthy"""
        now = int(time.time())
        time_since_heartbeat = now - self.last_heartbeat
        return time_since_heartbeat < self.max_offline_time and self.consecutive_failures < 3

    def is_expired(self) -> bool:
        """Check if session has expired"""
        now = int(time.time())
        return (now - self.last_heartbeat) > (self.max_offline_time * 2)


class DeviceManager:
    """Real device management service"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.active_sessions: dict[str, DeviceSession] = {}
        self.device_groups: dict[str, list[str]] = {}

    def register_device(
        self,
        classroom_id: str,
        device_name: str,
        device_ip: str,
        role: str = "student",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        """Register new device in classroom"""
        device_id = str(uuid.uuid4())
        auth_token = str(uuid.uuid4())

        meta = {**(metadata or {}), "device_ip": device_ip, "registered_at": int(time.time())}
        device = self.store.register_device(
            device_id=device_id,
            device_name=device_name,
            role=role,
            classroom=classroom_id,
            metadata=meta,
            auth_token=auth_token,
        )

        if "student_id" in meta and meta["student_id"]:
            self.store.map_student_device(device_id, str(meta["student_id"]))

        session = DeviceSession(
            session_id=str(uuid.uuid4()),
            device_id=device_id,
            device_name=device_name,
            role=role,
            classroom_id=classroom_id,
            status=DeviceStatus.REGISTERED.value,
            created_at=int(time.time()),
            last_heartbeat=int(time.time()),
            metadata={"device_ip": device_ip},
        )
        self.active_sessions[device_id] = session
        self.store.create_device_session(device["device_id"], classroom_id, device_name)

        return device_id, auth_token, device.get("device_id", device_id)

    def heartbeat(self, device_id: str, auth_token: str) -> bool:
        """Process device heartbeat"""
        device = self.store.get_device(device_id)
        if not device or device.get("auth_token") != auth_token:
            return False

        self.store.heartbeat(device_id)
        session = self.active_sessions.get(device_id)

        if session:
            session.last_heartbeat = int(time.time())
            session.consecutive_failures = 0
            if session.status in (DeviceStatus.OFFLINE.value, DeviceStatus.RECONNECTING.value):
                session.status = DeviceStatus.ONLINE.value
            return True

        return False

    def handle_reconnect(self, device_id: str, auth_token: str) -> dict[str, Any]:
        """Handle device reconnection"""
        device = self.store.get_device(device_id)
        if not device or device.get("auth_token") != auth_token:
            return {"status": "error", "message": "Invalid device"}

        session = self.active_sessions.get(device_id)
        if session:
            session.status = DeviceStatus.ONLINE.value
            session.consecutive_failures = 0
            session.last_heartbeat = int(time.time())

        self.store.heartbeat(device_id)

        return {
            "status": "ok",
            "device_id": device_id,
            "session_id": session.session_id if session else None,
            "classroom_id": session.classroom_id if session else None,
        }

    def get_device_session(self, device_id: str) -> DeviceSession | None:
        """Get device session"""
        return self.active_sessions.get(device_id)

    def list_classroom_devices(self, classroom_id: str) -> list[dict[str, Any]]:
        """List all devices in classroom"""
        devices = []
        for session in self.active_sessions.values():
            if session.classroom_id == classroom_id:
                devices.append(asdict(session))
        return devices

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """Get device status with sanitized credentials"""
        device = self.store.get_device(device_id)
        if not device:
            return {"status": "unknown"}

        sanitized_device = {key: value for key, value in device.items() if key != "auth_token"}
        sessions = self.store.list_device_sessions(device_id)
        student_id = self.store.get_student_for_device(device_id)

        return {
            "device": sanitized_device,
            "session": sessions[0] if sessions else None,
            "trust": self.store.is_device_trusted(device_id),
            "student_id": student_id,
        }

    def add_to_group(self, device_id: str, group_name: str) -> None:
        """Add device to group"""
        if group_name not in self.device_groups:
            self.device_groups[group_name] = []
        if device_id not in self.device_groups[group_name]:
            self.device_groups[group_name].append(device_id)

    def get_group_devices(self, group_name: str) -> list[str]:
        """Get devices in group"""
        return self.device_groups.get(group_name, [])

    def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions"""
        expired = [sid for sid, session in self.active_sessions.items() if session.is_expired()]
        for sid in expired:
            del self.active_sessions[sid]
        return len(expired)

    def handle_device_offline(self, device_id: str) -> dict[str, Any]:
        """Handle device going offline"""
        session = self.active_sessions.get(device_id)
        if session:
            session.status = DeviceStatus.OFFLINE.value
            session.consecutive_failures += 1

        return {
            "device_id": device_id,
            "status": "offline",
            "consecutive_failures": session.consecutive_failures if session else 0,
        }

    def get_classroom_status(self, classroom_id: str) -> dict[str, Any]:
        """Get overall classroom device status"""
        devices = self.list_classroom_devices(classroom_id)
        online_count = sum(1 for d in devices if d["status"] == DeviceStatus.ONLINE.value)
        offline_count = sum(1 for d in devices if d["status"] == DeviceStatus.OFFLINE.value)

        return {
            "classroom_id": classroom_id,
            "total_devices": len(devices),
            "online_devices": online_count,
            "offline_devices": offline_count,
            "devices": devices,
        }
