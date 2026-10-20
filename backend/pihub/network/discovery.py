"""
Classroom Network Discovery Service

Handles:
- Local network interface detection
- Classroom IP address management
- Hotspot mode detection
- Local device discovery
- Classroom session tracking
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shared.file_utils import atomic_write_json


@dataclass
class NetworkInterface:
    """Local network interface information"""

    name: str
    ip_address: str
    netmask: str
    gateway: str | None = None
    is_hotspot: bool = False
    mac_address: str | None = None
    status: str = "up"
    mtu: int = 1500


@dataclass
class NetworkSession:
    """Classroom local network session"""

    session_id: str
    classroom_name: str
    network_interface: str
    local_ip: str
    gateway: str
    network_cidr: str
    created_at: int
    hotspot_enabled: bool
    max_devices: int = 50
    active_devices: int = 0


# Alias for backward compatibility
ClassroomSession = NetworkSession


class NetworkDiscovery:
    """Classroom network discovery and management"""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir or Path(os.getenv("STORAGE_DIR", "/storage")) / "network"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.state_dir / "classroom_session.json"
        self.interfaces_file = self.state_dir / "interfaces.json"
        self._session: ClassroomSession | None = None
        self._load_session()

    def _load_session(self) -> None:
        """Load existing classroom session"""
        if self.session_file.exists():
            data = json.loads(self.session_file.read_text())
            self._session = ClassroomSession(**data)

    def _save_session(self) -> None:
        """Persist classroom session (crash-safe: tmp + fsync + rename)"""
        if self._session:
            atomic_write_json(self.session_file, asdict(self._session))

    def get_local_interfaces(self) -> list[NetworkInterface]:
        """Discover local network interfaces"""
        interfaces = []
        try:
            result = subprocess.run(
                ["ip", "-j", "link", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            link_data = json.loads(result.stdout) if result.returncode == 0 else []

            result = subprocess.run(
                ["ip", "-j", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            addr_data = json.loads(result.stdout) if result.returncode == 0 else []

            for link in link_data:
                if link["operstate"] != "UP":
                    continue
                for addr in addr_data:
                    if addr["ifname"] == link["ifname"]:
                        for addr_info in addr.get("addr_info", []):
                            if addr_info["family"] == "inet":
                                interfaces.append(
                                    NetworkInterface(
                                        name=link["ifname"],
                                        ip_address=addr_info["local"],
                                        netmask=str(addr_info.get("prefixlen", 24)),
                                        status="up",
                                        mac_address=link.get("address"),
                                    )
                                )
        except Exception:
            pass

        return interfaces

    def detect_hotspot_interface(self) -> NetworkInterface | None:
        """Detect if running as WiFi hotspot"""
        interfaces = self.get_local_interfaces()
        for iface in interfaces:
            if iface.name in ("wlan0", "ap0", "uap0"):
                iface.is_hotspot = True
                return iface
        return None

    def get_gateway_ip(self, interface_name: str) -> str | None:
        """Get gateway IP for interface"""
        try:
            result = subprocess.run(
                ["ip", "-j", "route", "show", f"dev", interface_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            routes = json.loads(result.stdout) if result.returncode == 0 else []
            for route in routes:
                if route.get("dst") == "default":
                    return route.get("gateway")
        except Exception:
            pass
        return None

    def create_classroom_session(
        self,
        classroom_name: str,
        network_interface: str | None = None,
        hotspot_enabled: bool = False,
    ) -> ClassroomSession:
        """Create new classroom network session"""
        iface_name = network_interface or "eth0"
        interfaces = {iface.name: iface for iface in self.get_local_interfaces()}

        if iface_name not in interfaces:
            iface_name = interfaces.keys().__iter__().__next__() if interfaces else "eth0"

        iface = interfaces.get(iface_name)
        if not iface:
            iface = NetworkInterface(name=iface_name, ip_address="0.0.0.0", netmask="24")

        gateway = self.get_gateway_ip(iface.name)
        session = ClassroomSession(
            session_id=str(uuid.uuid4()),
            classroom_name=classroom_name,
            network_interface=iface.name,
            local_ip=iface.ip_address,
            gateway=gateway or "192.168.1.1",
            network_cidr=f"{iface.ip_address}/{iface.netmask}",
            created_at=int(time.time()),
            hotspot_enabled=hotspot_enabled or iface.is_hotspot,
        )
        self._session = session
        self._save_session()
        return session

    def get_classroom_session(self) -> ClassroomSession | None:
        """Get current classroom session"""
        return self._session

    def register_device_on_network(self, device_id: str, device_ip: str) -> dict[str, Any]:
        """Register device joining classroom network"""
        if self._session:
            self._session.active_devices = min(self._session.active_devices + 1, self._session.max_devices)
            self._save_session()

        return {
            "device_id": device_id,
            "device_ip": device_ip,
            "session_id": self._session.session_id if self._session else None,
            "classroom_name": self._session.classroom_name if self._session else None,
            "local_gateway": self._session.gateway if self._session else None,
            "registered_at": int(time.time()),
        }

    def unregister_device_from_network(self, device_id: str) -> dict[str, Any]:
        """Unregister device leaving classroom network"""
        if self._session:
            self._session.active_devices = max(0, self._session.active_devices - 1)
            self._save_session()

        return {
            "device_id": device_id,
            "unregistered_at": int(time.time()),
            "remaining_devices": self._session.active_devices if self._session else 0,
        }

    def get_network_status(self) -> dict[str, Any]:
        """Get overall network status"""
        session = self.get_classroom_session()
        interfaces = self.get_local_interfaces()

        return {
            "status": "connected" if interfaces else "disconnected",
            "session": asdict(session) if session else None,
            "interfaces": [asdict(iface) for iface in interfaces],
            "active_devices": session.active_devices if session else 0,
            "hotspot_available": any(iface.is_hotspot for iface in interfaces),
            "timestamp": int(time.time()),
        }


class ClassroomIntranet:
    """Local classroom intranet management"""

    def __init__(self, discovery: NetworkDiscovery) -> None:
        self.discovery = discovery
        self.device_registry: dict[str, dict[str, Any]] = {}
        self.local_routes: dict[str, str] = {}

    def announce_device(self, device_id: str, device_name: str, device_ip: str, device_port: int) -> dict[str, Any]:
        """Announce device on local classroom network"""
        self.device_registry[device_id] = {
            "device_id": device_id,
            "device_name": device_name,
            "device_ip": device_ip,
            "device_port": device_port,
            "announced_at": int(time.time()),
        }
        return self.device_registry[device_id]

    def discover_local_devices(self) -> list[dict[str, Any]]:
        """Discover other devices on classroom network"""
        return list(self.device_registry.values())

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Get registered device info"""
        return self.device_registry.get(device_id)

    def unannounce_device(self, device_id: str) -> None:
        """Remove device from local registry"""
        self.device_registry.pop(device_id, None)

    def get_intranet_status(self) -> dict[str, Any]:
        """Get intranet status"""
        return {
            "session": self.discovery.get_classroom_session(),
            "registered_devices": len(self.device_registry),
            "devices": list(self.device_registry.values()),
        }
