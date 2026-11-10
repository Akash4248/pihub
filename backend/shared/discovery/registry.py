from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ServiceInstance(BaseModel):
    service_name: str
    instance_id: str
    url: str
    healthy: bool = True
    last_heartbeat: float = Field(default_factory=time.time)
    weight: int = 1
    active_requests: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ServiceRegistry:
    """Dynamic Service Registry with health checking and multi-strategy load balancing."""

    def __init__(self) -> None:
        self._services: dict[str, dict[str, ServiceInstance]] = {}
        self._round_robin_counters: dict[str, itertools.cycle] = {}
        self._lock = asyncio.Lock()
        self._health_check_task: asyncio.Task | None = None

    def register(
        self,
        service_name: str,
        instance_id: str,
        url: str,
        metadata: dict[str, Any] | None = None,
        healthy: bool = True,
    ) -> ServiceInstance:
        """Register or update an instance in the service directory."""
        url = url.rstrip("/")
        instance = ServiceInstance(
            service_name=service_name,
            instance_id=instance_id,
            url=url,
            healthy=healthy,
            last_heartbeat=time.time(),
            metadata=metadata or {},
        )
        if service_name not in self._services:
            self._services[service_name] = {}
        self._services[service_name][instance_id] = instance
        self._refresh_round_robin(service_name)
        logger.info("[DISCOVERY] Registered %s (%s) at %s", service_name, instance_id, url)
        return instance

    def deregister(self, service_name: str, instance_id: str) -> bool:
        """Remove an instance from routing."""
        if service_name in self._services and instance_id in self._services[service_name]:
            del self._services[service_name][instance_id]
            self._refresh_round_robin(service_name)
            logger.info("[DISCOVERY] Deregistered %s (%s)", service_name, instance_id)
            return True
        return False

    def heartbeat(self, service_name: str, instance_id: str) -> bool:
        """Update last heartbeat timestamp for an instance."""
        if service_name in self._services and instance_id in self._services[service_name]:
            inst = self._services[service_name][instance_id]
            inst.last_heartbeat = time.time()
            inst.healthy = True
            return True
        return False

    def mark_unhealthy(self, service_name: str, instance_id_or_url: str) -> None:
        """Mark an instance as unhealthy after downstream failure."""
        if service_name in self._services:
            for inst_id, inst in self._services[service_name].items():
                if inst_id == instance_id_or_url or inst.url == instance_id_or_url.rstrip("/"):
                    inst.healthy = False
                    logger.warning("[DISCOVERY] Marked instance %s (%s) UNHEALTHY", service_name, inst_id)
            self._refresh_round_robin(service_name)

    def mark_healthy(self, service_name: str, instance_id_or_url: str) -> None:
        """Mark an instance as healthy."""
        if service_name in self._services:
            for inst_id, inst in self._services[service_name].items():
                if inst_id == instance_id_or_url or inst.url == instance_id_or_url.rstrip("/"):
                    inst.healthy = True
            self._refresh_round_robin(service_name)

    def get_instances(self, service_name: str, healthy_only: bool = True) -> list[ServiceInstance]:
        instances = list(self._services.get(service_name, {}).values())
        if healthy_only:
            return [inst for inst in instances if inst.healthy]
        return instances

    def list_all_services(self) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for s_name, inst_dict in self._services.items():
            result[s_name] = [inst.model_dump() for inst in inst_dict.values()]
        return result

    def resolve(
        self,
        service_name: str,
        default_url: str | None = None,
        strategy: str = "round_robin",
    ) -> str:
        """Resolve a healthy instance URL for a service using the specified load-balancer strategy.

        Falls back to default_url if no healthy instances are dynamically registered.
        """
        healthy_instances = self.get_instances(service_name, healthy_only=True)
        if not healthy_instances:
            if default_url:
                return default_url.rstrip("/")
            raise RuntimeError(f"Service discovery failed: No healthy instances for '{service_name}'")

        if strategy == "round_robin":
            cycle = self._round_robin_counters.get(service_name)
            if cycle:
                # Pick next from cycle (cycling over instance URLs)
                for _ in range(len(healthy_instances) + 1):
                    target_url = next(cycle)
                    # Verify it's still healthy
                    match = next((i for i in healthy_instances if i.url == target_url), None)
                    if match:
                        return target_url

        elif strategy == "least_connections":
            least_inst = min(healthy_instances, key=lambda i: i.active_requests)
            return least_inst.url

        # Default random strategy fallback
        return random.choice(healthy_instances).url

    def _refresh_round_robin(self, service_name: str) -> None:
        healthy_urls = [inst.url for inst in self.get_instances(service_name, healthy_only=True)]
        if healthy_urls:
            self._round_robin_counters[service_name] = itertools.cycle(healthy_urls)
        elif service_name in self._round_robin_counters:
            del self._round_robin_counters[service_name]

    async def probe_all_instances(self, timeout: float = 2.0) -> None:
        """Active health probe for all registered instances."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            for s_name, instances in list(self._services.items()):
                for inst_id, inst in list(instances.items()):
                    health_url = f"{inst.url}/health"
                    try:
                        resp = await client.get(health_url)
                        was_healthy = inst.healthy
                        inst.healthy = (resp.status_code == 200)
                        if was_healthy != inst.healthy:
                            logger.info(
                                "[DISCOVERY] Health status changed for %s (%s): %s",
                                s_name,
                                inst_id,
                                "HEALTHY" if inst.healthy else "UNHEALTHY",
                            )
                            self._refresh_round_robin(s_name)
                    except Exception as exc:
                        if inst.healthy:
                            logger.warning("[DISCOVERY] Probe failed for %s (%s): %s", s_name, inst_id, exc)
                            inst.healthy = False
                            self._refresh_round_robin(s_name)

    async def start_health_checker(self, interval_seconds: float = 15.0) -> None:
        """Periodic background health check probe."""
        while True:
            try:
                await self.probe_all_instances()
            except Exception as exc:
                logger.debug("[DISCOVERY] Health checker pass error: %s", exc)
            await asyncio.sleep(interval_seconds)


# Global singleton registry
_registry: ServiceRegistry | None = None


def get_service_registry() -> ServiceRegistry:
    global _registry
    if _registry is None:
        _registry = ServiceRegistry()
    return _registry
