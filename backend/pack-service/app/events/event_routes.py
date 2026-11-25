from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.events.event_models import (
    ClassroomDistributionState,
    DistributionAnalyticsResponse,
    PackHistoryResponse,
    PackStateProjection,
    PublishPackRequest,
    SyncAcknowledgmentRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pack Lifecycle & Event Sourcing"])


def _event_service(request: Request):
    service = getattr(request.app.state, "event_service", None)
    if not service:
        raise HTTPException(status_code=500, detail="Event service not initialized on app state")
    return service


# ==============================================================================
# CQRS COMMANDS (State Modifying Operations)
# ==============================================================================

@router.post("/packs/{pack_id}/publish", response_model=dict[str, Any])
def publish_pack(
    pack_id: str,
    request: Request,
    payload: Optional[PublishPackRequest] = None,
    version: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Publish a generated learning pack, making it approved for classroom distribution."""
    service = _event_service(request)
    publisher = payload.publisher_id if payload else "admin"
    actor_type = payload.actor_type if payload else "admin"
    notes = payload.notes if payload else None

    # Check if pack exists in repository
    repo = getattr(request.app.state, "pack_repository", None)
    if repo:
        pack = repo.get_pack(pack_id, version)
        if not pack:
            raise HTTPException(status_code=404, detail=f"Pack '{pack_id}' not found in repository")
        target_version = version or pack.get("version", "1.0.0")

        # Ensure PackCreated is recorded if this is a legacy/existing pack
        service.record_pack_created(
            pack_id=pack_id,
            version=target_version,
            grade=pack.get("grade"),
            subject=pack.get("subject"),
            chapter=pack.get("chapter"),
            checksum=pack.get("checksum"),
            size_bytes=pack.get("size_bytes", 0),
        )
    else:
        target_version = version or "1.0.0"

    try:
        event = service.record_pack_published(
            pack_id=pack_id,
            version=target_version,
            publisher_id=publisher,
            actor_type=actor_type,
            notes=notes,
        )
        status = service.get_status(pack_id, target_version)
        return {
            "status": "published",
            "pack_id": pack_id,
            "version": target_version,
            "event": event,
            "current_state": status,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Error publishing pack '%s'", pack_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/internal/packs/{pack_id}/synchronization", response_model=dict[str, Any])
def acknowledge_synchronization(
    pack_id: str,
    payload: SyncAcknowledgmentRequest,
    request: Request,
    x_internal_service_key: Optional[str] = Header(None, alias="X-Internal-Service-Key"),
) -> dict[str, Any]:
    """
    Authenticated internal endpoint called by PIHUB Node once a pack has been
    successfully transferred, verified (checksum), and unpacked in classroom offline storage.
    """
    service = _event_service(request)

    # Record PackCreated if pack exists in repo but has no events yet
    repo = getattr(request.app.state, "pack_repository", None)
    if repo:
        pack = repo.get_pack(pack_id, payload.version)
        if pack:
            service.record_pack_created(
                pack_id=pack_id,
                version=payload.version,
                grade=pack.get("grade"),
                subject=pack.get("subject"),
                chapter=pack.get("chapter"),
                checksum=pack.get("checksum"),
                size_bytes=pack.get("size_bytes", 0),
            )

    event = service.record_pack_synchronized(
        pack_id=pack_id,
        version=payload.version,
        classroom_id=payload.classroom_id,
        device_id=payload.device_id,
        checksum=payload.checksum,
        status=payload.status,
        idempotency_key=payload.idempotency_key,
    )

    state = service.get_status(pack_id, payload.version)
    return {
        "status": "acknowledged",
        "pack_id": pack_id,
        "classroom_id": payload.classroom_id,
        "event_id": event.get("event_id"),
        "aggregate_version": event.get("aggregate_version"),
        "current_state": state,
    }


@router.post("/packs/events/replay", response_model=dict[str, Any])
def replay_event_projections(request: Request) -> dict[str, Any]:
    """
    Event Sourcing Demonstration: Clears the CQRS read model tables and replays
    all events from the immutable event store to reconstruct identical state.
    """
    service = _event_service(request)
    return service.replay_and_rebuild()


# ==============================================================================
# CQRS QUERIES (Read Side Projections)
# ==============================================================================

@router.get("/packs/{pack_id}/history", response_model=PackHistoryResponse)
def get_pack_history(pack_id: str, request: Request) -> PackHistoryResponse:
    """Retrieve full append-only lifecycle event stream for a pack."""
    service = _event_service(request)
    events = service.get_history(pack_id)
    if not events:
        # Fallback for existing un-evented packs: check if pack exists
        repo = getattr(request.app.state, "pack_repository", None)
        pack = repo.get_pack(pack_id) if repo else None
        if not pack:
            raise HTTPException(status_code=404, detail=f"No lifecycle history found for pack '{pack_id}'")

        # Lazily seed PackCreated
        service.record_pack_created(
            pack_id=pack_id,
            version=pack.get("version", "1.0.0"),
            grade=pack.get("grade"),
            subject=pack.get("subject"),
            chapter=pack.get("chapter"),
            checksum=pack.get("checksum"),
            size_bytes=pack.get("size_bytes", 0),
        )
        events = service.get_history(pack_id)

    return PackHistoryResponse(pack_id=pack_id, total_events=len(events), events=events)


@router.get("/packs/{pack_id}/status", response_model=PackStateProjection)
def get_pack_status(pack_id: str, request: Request, version: Optional[str] = Query(default=None)) -> PackStateProjection:
    """Retrieve projected CQRS state of a pack including publication and classroom sync status."""
    service = _event_service(request)
    status = service.get_status(pack_id, version)
    if not status:
        # Check repo and lazily create initial event
        repo = getattr(request.app.state, "pack_repository", None)
        pack = repo.get_pack(pack_id, version) if repo else None
        if not pack:
            raise HTTPException(status_code=404, detail=f"Pack '{pack_id}' not found")

        target_version = version or pack.get("version", "1.0.0")
        service.record_pack_created(
            pack_id=pack_id,
            version=target_version,
            grade=pack.get("grade"),
            subject=pack.get("subject"),
            chapter=pack.get("chapter"),
            checksum=pack.get("checksum"),
            size_bytes=pack.get("size_bytes", 0),
        )
        status = service.get_status(pack_id, target_version)

    return PackStateProjection(**status)


@router.get("/classrooms/{classroom_id}/packs", response_model=list[dict[str, Any]])
def get_classroom_packs(classroom_id: str, request: Request) -> list[dict[str, Any]]:
    """Retrieve all learning packs distributed to or synchronized by a specific classroom."""
    service = _event_service(request)
    return service.get_classroom_packs(classroom_id)


@router.get("/packs/distribution/analytics", response_model=DistributionAnalyticsResponse)
def get_distribution_analytics(request: Request) -> DistributionAnalyticsResponse:
    """Get system-wide metrics across events, publications, and classroom syncs."""
    service = _event_service(request)
    return DistributionAnalyticsResponse(**service.get_analytics())
