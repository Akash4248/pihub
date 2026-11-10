from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    PACK_CREATED = "pack.created"
    PACK_UPDATED = "pack.updated"
    DEVICE_REGISTERED = "device.registered"
    STUDENT_PROGRESS_UPDATED = "student.progress.updated"
    QUIZ_COMPLETED = "quiz.completed"
    CONTENT_INDEXED = "content.indexed"
    SYNC_COMPLETED = "sync.completed"
    
    # Saga Coordination Events
    SAGA_STARTED = "saga.started"
    SAGA_STEP_COMPLETED = "saga.step.completed"
    SAGA_STEP_FAILED = "saga.step.failed"
    SAGA_COMPENSATED = "saga.compensated"
    SAGA_COMPLETED = "saga.completed"


class Event(BaseModel):
    """Standardized event envelope across all microservices."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    version: str = "1.0"
    producer: str
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> Event:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)
