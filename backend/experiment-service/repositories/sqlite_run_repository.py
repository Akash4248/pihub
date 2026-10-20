from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from domain.enums import ExperimentExecutionMode, ExperimentStatus
from domain.models import ExperimentResult, ExperimentRun, ExperimentRunEvent
from repositories.run_repository import ExperimentEventRepository, ExperimentResultRepository, ExperimentRunRepository
from storage.sqlite_schema import EXPERIMENT_SQLITE_SCHEMA
from app.core.database import connect_sqlite, initialize_sqlite_database


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_model(model_type, payload):
    if hasattr(model_type, "model_validate"):
        return model_type.model_validate(payload)
    return model_type.parse_obj(payload)


def _validate_uuid(val: str, field_name: str) -> None:
    if not val or not isinstance(val, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    try:
        uuid.UUID(val)
    except ValueError:
        pass  # allow custom string IDs while ensuring non-empty string


class ExperimentSqliteStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[1] / "storage" / "experiment_service.sqlite3"
        self.db_path = Path(db_path or os.getenv("EXPERIMENT_SERVICE_DB_PATH", str(default_path)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path)

    def _init_db(self) -> None:
        initialize_sqlite_database(self.db_path, EXPERIMENT_SQLITE_SCHEMA)


class SqliteExperimentRunRepository(ExperimentRunRepository):
    def __init__(self, store: ExperimentSqliteStore | None = None) -> None:
        self.store = store or ExperimentSqliteStore()

    async def create_run(self, run: ExperimentRun) -> ExperimentRun:
        _validate_uuid(run.run_id, "run_id")
        _validate_uuid(run.experiment_id, "experiment_id")
        now = _iso(utc_now())
        with sqlite_transaction(self.store.db_path) as connection:
            connection.execute(
                """
                insert into experiment_runs (
                    run_id, experiment_id, student_id, execution_mode, status,
                    started_at, completed_at, duration_ms, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.experiment_id,
                    run.student_id,
                    run.execution_mode.value,
                    run.status.value,
                    _iso(run.started_at),
                    _iso(run.completed_at),
                    run.duration_ms,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "select * from experiment_runs where run_id = ?",
                (run.run_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create experiment run")
        return self._row_to_run(row)

    async def get_run(self, run_id: str) -> ExperimentRun | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "select * from experiment_runs where run_id = ?",
                (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    async def list_student_runs(self, student_id: str) -> list[ExperimentRun]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "select * from experiment_runs where student_id = ? order by created_at desc",
                (student_id,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    async def update_run_status(self, run_id: str, status: ExperimentStatus) -> ExperimentRun | None:
        now = utc_now()
        started_at = now if status == ExperimentStatus.RUNNING else None
        with self.store.connect() as connection:
            if started_at:
                connection.execute(
                    """
                    update experiment_runs
                    set status = ?, started_at = coalesce(started_at, ?), updated_at = ?
                    where run_id = ?
                    """,
                    (status.value, _iso(started_at), _iso(now), run_id),
                )
            else:
                connection.execute(
                    "update experiment_runs set status = ?, updated_at = ? where run_id = ?",
                    (status.value, _iso(now), run_id),
                )
        return await self.get_run(run_id)

    async def complete_run(self, run_id: str, result: ExperimentResult) -> ExperimentRun | None:
        with sqlite_transaction(self.store.db_path) as connection:
            row = connection.execute(
                "select * from experiment_runs where run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            current = self._row_to_run(row)
            now = utc_now()
            started = current.started_at or now
            duration_ms = int((now - started).total_seconds() * 1000)
            connection.execute(
                """
                update experiment_runs
                set status = ?, completed_at = ?, duration_ms = ?, updated_at = ?
                where run_id = ?
                """,
                (ExperimentStatus.COMPLETED.value, _iso(now), duration_ms, _iso(now), run_id),
            )
            updated_row = connection.execute(
                "select * from experiment_runs where run_id = ?",
                (run_id,),
            ).fetchone()
        return self._row_to_run(updated_row) if updated_row else None

    def _row_to_run(self, row: sqlite3.Row) -> ExperimentRun:
        return _validate_model(
            ExperimentRun,
            {
                "run_id": row["run_id"],
                "experiment_id": row["experiment_id"],
                "student_id": row["student_id"],
                "execution_mode": ExperimentExecutionMode(row["execution_mode"]),
                "status": ExperimentStatus(row["status"]),
                "started_at": _parse_datetime(row["started_at"]),
                "completed_at": _parse_datetime(row["completed_at"]),
                "duration_ms": row["duration_ms"],
            },
        )


class SqliteExperimentEventRepository(ExperimentEventRepository):
    def __init__(self, store: ExperimentSqliteStore | None = None) -> None:
        self.store = store or ExperimentSqliteStore()

    async def append_event(self, event: ExperimentRunEvent) -> ExperimentRunEvent:
        with self.store.connect() as connection:
            connection.execute(
                """
                insert into experiment_run_events (event_id, run_id, event_type, timestamp, payload_json)
                values (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.run_id,
                    event.event_type,
                    _iso(event.timestamp),
                    json.dumps(event.payload),
                ),
            )
        return event

    async def get_events(self, run_id: str) -> list[ExperimentRunEvent]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "select * from experiment_run_events where run_id = ? order by timestamp asc",
                (run_id,),
            ).fetchall()
        return [
            _validate_model(
                ExperimentRunEvent,
                {
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "event_type": row["event_type"],
                    "timestamp": _parse_datetime(row["timestamp"]) or utc_now(),
                    "payload": json.loads(row["payload_json"] or "{}"),
                },
            )
            for row in rows
        ]


class SqliteExperimentResultRepository(ExperimentResultRepository):
    def __init__(self, store: ExperimentSqliteStore | None = None) -> None:
        self.store = store or ExperimentSqliteStore()

    async def save_result(self, result: ExperimentResult) -> ExperimentResult:
        with self.store.connect() as connection:
            connection.execute(
                """
                insert into experiment_results (
                    result_id, run_id, completion_percentage, score,
                    observations_json, measurements_json, notes, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.run_id,
                    result.completion_percentage,
                    result.score,
                    json.dumps(result.observations),
                    json.dumps(result.measurements),
                    result.notes,
                    _iso(utc_now()),
                ),
            )
        return result

    async def get_result(self, run_id: str) -> ExperimentResult | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "select * from experiment_results where run_id = ? order by created_at desc limit 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _validate_model(
            ExperimentResult,
            {
                "result_id": row["result_id"],
                "run_id": row["run_id"],
                "completion_percentage": row["completion_percentage"],
                "score": row["score"],
                "observations": json.loads(row["observations_json"] or "[]"),
                "measurements": json.loads(row["measurements_json"] or "[]"),
                "notes": row["notes"],
            },
        )
