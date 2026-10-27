"""LAN discovery beacon (UDP) and node capability payloads."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any
from urllib.parse import urlparse

from shared.config import get_settings


logger = logging.getLogger(__name__)

settings = get_settings()


def node_ip() -> str:
    configured_host = urlparse(settings.host_url).hostname
    if configured_host and configured_host not in {"0.0.0.0", "localhost"}:
        return configured_host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "0.0.0.0"


def capabilities() -> dict[str, bool]:
    return {
        "rag": True,
        "sync": True,
        "assets": True,
        "streaming": True,
        "planner": True,
        "progress": True,
        "metrics": True,
        "experiments": True,
        "voice": settings.voice_service_enabled,
        "audio": settings.voice_service_enabled,
    }


def discovery_payload() -> dict[str, Any]:
    return {
        "name": "PIHUB",
        "service": "PIHUB",
        "version": "1.0",
        "node_type": "hub",
        "ip": node_ip(),
        "api_port": settings.gateway_port,
        "health": "/health",
        "discovery": "/discovery",
        "capabilities": capabilities(),
        "supports_rag": True,
        "supports_sync": True,
        "supports_assets": True,
        "supports_voice": settings.voice_service_enabled,
    }


async def discovery_beacon_loop() -> None:
    port = settings.discovery_port
    while True:
        payload = json.dumps(discovery_payload(), separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as beacon:
                beacon.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                beacon.sendto(payload, ("255.255.255.255", port))
        except OSError as exc:
            logger.debug("[DISCOVERY] BEACON_ERROR=%s", exc)
        await asyncio.sleep(15)
