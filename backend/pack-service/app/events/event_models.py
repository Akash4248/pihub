from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class PackEventType(str, Enum):
    PACK_CREATED = "PackCreated"
    PACK_PUBLISHED = "PackPublished"
    PACK_DOWNLOADED = "PackDownloaded"
    PACK_SYNCHRONIZED = "PackSynchronized"
    PACK_UPDATED = "PackUpdated"


class PackEvent(BaseModel):
    event_id: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    actor_id: Optional[str] = None
    actor_type: Optional[str] = None
    classroom_id: Optional[str] = None
    device_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    occurred_at: str


class PublishPackRequest(BaseModel):
    publisher_id: Optional[str] = "admin"
    actor_type: Optional[str] = "admin"
    notes: Optional[str] = None


class SyncAcknowledgmentRequest(BaseModel):
    version: str
    classroom_id: str
    device_id: str
    checksum: str
    status: str = "success"
    idempotency_key: Optional[str] = None


class ClassroomDistributionState(BaseModel):
    pack_id: str
    version: str
    classroom_id: str
    distribution_status: str
    last_device_id: Optional[str] = None
    download_count: int = 0
    sync_count: int = 0
    last_event_id: str
    last_updated: str


class PackStateProjection(BaseModel):
    pack_id: str
    version: str
    publication_status: str
    created_at: str
    published_at: Optional[str] = None
    last_event_id: str
    current_version_num: int
    distribution: list[ClassroomDistributionState] = Field(default_factory=list)


class PackHistoryResponse(BaseModel):
    pack_id: str
    total_events: int
    events: list[PackEvent]


class DistributionAnalyticsResponse(BaseModel):
    total_events: int
    total_packs_tracked: int
    published_packs_count: int
    draft_packs_count: int
    total_downloads: int
    total_synchronizations: int
    active_classrooms: list[str]
