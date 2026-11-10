from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from shared.db.ownership import DATA_OWNERSHIP_CATALOG
from shared.discovery import ServiceInstance, get_service_registry
from shared.resilience import get_circuit_breaker_registry
from shared.saga import (
    PACK_PUBLICATION_SAGA_TYPE,
    get_saga_orchestrator,
    register_pack_publication_saga,
)
from shared.tracing import get_tracer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["microservices-architecture"])


# --- Circuit Breaker Endpoints ---
@router.get("/system/circuit-breakers")
async def get_circuit_breakers() -> dict[str, Any]:
    """Returns real-time status and failure metrics for all downstream microservice circuit breakers."""
    registry = get_circuit_breaker_registry()
    return {"circuit_breakers": registry.get_all_stats()}


@router.post("/system/circuit-breakers/{service_name}/reset")
async def reset_circuit_breaker(service_name: str) -> dict[str, Any]:
    """Manually resets a tripped circuit breaker to CLOSED state."""
    registry = get_circuit_breaker_registry()
    cb = registry.get_breaker(service_name)
    cb.reset()
    return {"status": "ok", "message": f"Circuit breaker for '{service_name}' has been reset to CLOSED."}


# --- Distributed Tracing Endpoints ---
@router.get("/system/traces")
async def get_recent_traces(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    """Inspect recent distributed traces with multi-service span breakdowns."""
    tracer = get_tracer(service_name="gateway")
    return {"traces": tracer.get_traces(limit=limit)}


# --- Dynamic Service Discovery Endpoints ---
class RegisterInstanceRequest(BaseModel):
    service_name: str
    instance_id: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/discovery/services")
async def list_discovered_services() -> dict[str, Any]:
    """List all registered microservice instances and their health status."""
    registry = get_service_registry()
    return {"services": registry.list_all_services()}


@router.post("/discovery/register")
async def register_service_instance(req: RegisterInstanceRequest) -> dict[str, Any]:
    """Dynamically register a new microservice instance into the gateway service mesh."""
    registry = get_service_registry()
    inst = registry.register(
        service_name=req.service_name,
        instance_id=req.instance_id,
        url=req.url,
        metadata=req.metadata,
    )
    return {"status": "registered", "instance": inst.model_dump()}


@router.post("/discovery/deregister")
async def deregister_service_instance(service_name: str, instance_id: str) -> dict[str, Any]:
    """Deregister an instance from the load-balancer pool."""
    registry = get_service_registry()
    success = registry.deregister(service_name, instance_id)
    if not success:
        raise HTTPException(status_code=404, detail="Service instance not found")
    return {"status": "deregistered", "service_name": service_name, "instance_id": instance_id}


# --- Database per Service Catalog ---
@router.get("/data-ownership")
async def get_data_ownership_catalog() -> dict[str, Any]:
    """Returns the explicit data ownership and database isolation boundaries for all microservices."""
    catalog = {domain.value: info for domain, info in DATA_OWNERSHIP_CATALOG.items()}
    return {
        "pattern": "Database-per-Service",
        "description": "Each microservice strictly owns its operational datastore; cross-service queries are prohibited.",
        "services": catalog,
    }


# --- Saga Distributed Transactions Endpoints ---
class StartPackSagaRequest(BaseModel):
    pack_id: str
    classroom_id: str = "class_001"
    grade: int = 8
    subject: str = "science"
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/sagas/pack-publication")
async def trigger_pack_publication_saga(req: StartPackSagaRequest) -> dict[str, Any]:
    """Trigger an orchestrated Saga workflow to publish a learning pack across services.

    Steps:
      1. Pack Service creates pack record
      2. Content Pipeline indexes pack content
      3. PiHub registers pack for classroom
    Compensates backwards on any step failure.
    """
    orchestrator = get_saga_orchestrator()
    register_pack_publication_saga(orchestrator)

    payload = {
        "pack_id": req.pack_id,
        "classroom_id": req.classroom_id,
        "grade": req.grade,
        "subject": req.subject,
        **req.metadata,
    }
    record = await orchestrator.execute(PACK_PUBLICATION_SAGA_TYPE, payload)
    return {
        "saga_id": record.saga_id,
        "saga_type": record.saga_type,
        "status": record.status.value,
        "completed_steps": record.completed_steps,
        "failed_step": record.failed_step,
        "error": record.error,
        "step_results": record.step_results,
    }


@router.get("/sagas/{saga_id}")
async def get_saga_status(saga_id: str) -> dict[str, Any]:
    """Get execution status and audit trace for a distributed saga."""
    orchestrator = get_saga_orchestrator()
    record = orchestrator.history.get(saga_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Saga with ID '{saga_id}' not found")
    return record.model_dump()
