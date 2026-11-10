from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field

from shared.events.bus import get_event_bus
from shared.events.schemas import Event, EventType

logger = logging.getLogger(__name__)


class SagaStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    FAILED = "FAILED"


class SagaStep(BaseModel):
    name: str
    service: str
    # Note: functions are registered separately in python runtime
    retry_limit: int = 2
    timeout_seconds: float = 10.0


class SagaRecord(BaseModel):
    saga_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    saga_type: str
    status: SagaStatus = SagaStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)
    step_results: dict[str, Any] = Field(default_factory=dict)
    failed_step: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


StepExecutor = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]
StepCompensator = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class SagaDefinition:
    def __init__(self, saga_type: str) -> None:
        self.saga_type = saga_type
        self.steps: list[tuple[SagaStep, StepExecutor, StepCompensator]] = []

    def add_step(
        self,
        step: SagaStep,
        executor: StepExecutor,
        compensator: StepCompensator,
    ) -> SagaDefinition:
        self.steps.append((step, executor, compensator))
        return self


class SagaOrchestrator:
    """Orchestrated Saga Pattern Coordinator.

    Manages distributed transaction execution across services, state persistence,
    and automatic compensating actions upon intermediate failures.
    """

    SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS saga_records (
        saga_id TEXT PRIMARY KEY,
        saga_type TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        completed_steps_json TEXT NOT NULL,
        step_results_json TEXT NOT NULL,
        failed_step TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.registry: dict[str, SagaDefinition] = {}
        self.history: dict[str, SagaRecord] = {}
        self.db_path = Path(db_path) if db_path else None

        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.executescript(self.SCHEMA_DDL)

    def register(self, definition: SagaDefinition) -> None:
        self.registry[definition.saga_type] = definition

    def _persist(self, record: SagaRecord) -> None:
        record.updated_at = datetime.now(timezone.utc).isoformat()
        self.history[record.saga_id] = record
        if self.db_path:
            try:
                import json
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO saga_records (
                            saga_id, saga_type, status, payload_json,
                            completed_steps_json, step_results_json,
                            failed_step, error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.saga_id,
                            record.saga_type,
                            record.status.value,
                            json.dumps(record.payload),
                            json.dumps(record.completed_steps),
                            json.dumps(record.step_results),
                            record.failed_step,
                            record.error,
                            record.created_at,
                            record.updated_at,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.error("[SAGA] Failed to persist saga %s: %s", record.saga_id, exc)

    async def execute(self, saga_type: str, initial_payload: dict[str, Any]) -> SagaRecord:
        definition = self.registry.get(saga_type)
        if not definition:
            raise ValueError(f"Saga type '{saga_type}' is not registered")

        record = SagaRecord(
            saga_type=saga_type,
            status=SagaStatus.IN_PROGRESS,
            payload=dict(initial_payload),
        )
        self._persist(record)

        bus = get_event_bus()
        await bus.publish(
            Event(
                event_type=EventType.SAGA_STARTED.value,
                producer="saga-orchestrator",
                correlation_id=record.saga_id,
                payload={"saga_id": record.saga_id, "saga_type": saga_type},
            )
        )

        current_data = dict(initial_payload)
        executed_steps: list[tuple[SagaStep, StepCompensator]] = []

        for step, executor, compensator in definition.steps:
            logger.info("[SAGA:%s] Executing step '%s' (service=%s)", record.saga_id, step.name, step.service)
            step_success = False
            last_err: Exception | None = None

            for attempt in range(1, step.retry_limit + 1):
                try:
                    result = await asyncio.wait_for(executor(current_data), timeout=step.timeout_seconds)
                    current_data.update(result or {})
                    record.step_results[step.name] = result
                    record.completed_steps.append(step.name)
                    executed_steps.append((step, compensator))
                    step_success = True
                    self._persist(record)

                    await bus.publish(
                        Event(
                            event_type=EventType.SAGA_STEP_COMPLETED.value,
                            producer="saga-orchestrator",
                            correlation_id=record.saga_id,
                            payload={"saga_id": record.saga_id, "step": step.name, "result": result},
                        )
                    )
                    break
                except Exception as exc:
                    last_err = exc
                    logger.warning("[SAGA:%s] Step '%s' attempt %d failed: %s", record.saga_id, step.name, attempt, exc)
                    if attempt < step.retry_limit:
                        await asyncio.sleep(0.5)

            if not step_success:
                logger.error(
                    "[SAGA:%s] Step '%s' failed permanently. Starting compensating rollback.",
                    record.saga_id,
                    step.name,
                )
                record.status = SagaStatus.COMPENSATING
                record.failed_step = step.name
                record.error = str(last_err)
                self._persist(record)

                await bus.publish(
                    Event(
                        event_type=EventType.SAGA_STEP_FAILED.value,
                        producer="saga-orchestrator",
                        correlation_id=record.saga_id,
                        payload={"saga_id": record.saga_id, "step": step.name, "error": str(last_err)},
                    )
                )

                # Execute Compensating Transactions in reverse order
                await self._compensate(record, executed_steps, current_data)
                return record

        # All steps succeeded
        record.status = SagaStatus.COMPLETED
        self._persist(record)
        logger.info("[SAGA:%s] Saga '%s' completed successfully!", record.saga_id, saga_type)

        await bus.publish(
            Event(
                event_type=EventType.SAGA_COMPLETED.value,
                producer="saga-orchestrator",
                correlation_id=record.saga_id,
                payload={"saga_id": record.saga_id, "saga_type": saga_type, "results": record.step_results},
            )
        )
        return record

    async def _compensate(
        self,
        record: SagaRecord,
        executed_steps: list[tuple[SagaStep, StepCompensator]],
        context: dict[str, Any],
    ) -> None:
        """Run backward compensations in reverse order."""
        bus = get_event_bus()
        compensation_failed = False

        for step, compensator in reversed(executed_steps):
            logger.info("[SAGA:%s] Compensating step '%s'...", record.saga_id, step.name)
            try:
                step_data = record.step_results.get(step.name, {})
                combined = {**context, **step_data}
                await asyncio.wait_for(compensator(combined), timeout=step.timeout_seconds)
                logger.info("[SAGA:%s] Compensation for '%s' succeeded.", record.saga_id, step.name)
            except Exception as c_exc:
                logger.critical("[SAGA:%s] Compensation FAILED for '%s': %s", record.saga_id, step.name, c_exc)
                compensation_failed = True

        record.status = SagaStatus.FAILED if compensation_failed else SagaStatus.COMPENSATED
        self._persist(record)

        await bus.publish(
            Event(
                event_type=EventType.SAGA_COMPENSATED.value,
                producer="saga-orchestrator",
                correlation_id=record.saga_id,
                payload={"saga_id": record.saga_id, "status": record.status.value},
            )
        )


_orchestrator: SagaOrchestrator | None = None


def get_saga_orchestrator() -> SagaOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = SagaOrchestrator()
    return _orchestrator
