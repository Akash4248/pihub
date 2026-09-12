from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class PiHubStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL;")
        connection.execute("PRAGMA busy_timeout = 10000;")
        connection.execute("PRAGMA synchronous = NORMAL;")
        connection.execute("PRAGMA foreign_keys = ON;")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists devices (
                    device_id text primary key,
                    device_name text not null,
                    role text not null,
                    classroom text,
                    auth_token text not null,
                    status text not null,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null,
                    last_seen integer
                );

                create table if not exists classroom (
                    id integer primary key check (id = 1),
                    classroom_name text,
                    teacher_name text,
                    sync_mode text not null,
                    metadata text not null,
                    updated_at integer not null
                );

                create table if not exists packs (
                    pack_id text primary key,
                    pack_name text not null,
                    version text not null,
                    subject text,
                    grade integer,
                    chapter text,
                    file_path text not null,
                    checksum text not null,
                    size_bytes integer not null,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null,
                    last_accessed integer,
                    hits integer not null default 0
                );

                create table if not exists sync_sessions (
                    session_id text primary key,
                    device_id text,
                    resource_type text,
                    resource_id text,
                    status text not null,
                    offset_bytes integer not null,
                    total_bytes integer,
                    retry_count integer not null default 0,
                    checksum text,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null
                );

                create table if not exists cache_entries (
                    cache_key text primary key,
                    payload text not null,
                    created_at integer not null,
                    updated_at integer not null
                );
                
                create table if not exists device_sessions (
                    session_id text primary key,
                    device_id text not null,
                    classroom text,
                    student_name text,
                    session_start integer not null,
                    session_end integer,
                    status text not null,
                    sync_status text not null,
                    last_heartbeat integer not null,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null
                );
                
                create table if not exists pack_cache (
                    pack_id text primary key,
                    pack_name text not null,
                    version text not null,
                    subject text,
                    grade integer,
                    chapter text,
                    file_path text not null,
                    checksum text not null,
                    size_bytes integer not null,
                    cached_at integer not null,
                    last_accessed integer,
                    access_count integer not null default 0,
                    priority integer not null default 1,
                    status text not null,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null
                );
                
                create table if not exists sync_queue (
                    queue_id text primary key,
                    action text not null,
                    resource_type text not null,
                    resource_id text,
                    target_devices text,
                    priority integer not null default 1,
                    status text not null,
                    retry_count integer not null default 0,
                    max_retries integer not null default 5,
                    checksum text,
                    size_bytes integer,
                    metadata text not null,
                    created_at integer not null,
                    scheduled_at integer,
                    started_at integer,
                    completed_at integer,
                    updated_at integer not null
                );
                
                create table if not exists sync_queue_events (
                    event_id text primary key,
                    queue_id text not null,
                    device_id text,
                    event_type text not null,
                    status text not null,
                    bytes_transferred integer,
                    error_message text,
                    metadata text not null,
                    created_at integer not null
                );
                
                create table if not exists pack_versions (
                    version_id text primary key,
                    pack_id text not null,
                    version_number text not null,
                    released_at integer,
                    changelog text,
                    metadata text not null,
                    created_at integer not null
                );
                
                create table if not exists backend_sync_state (
                    id integer primary key check (id = 1),
                    last_sync_time integer,
                    backend_available boolean not null default 1,
                    last_check_time integer,
                    offline_mode boolean not null default 0,
                    pending_pushes integer not null default 0,
                    metadata text not null,
                    updated_at integer not null
                );
                
                create table if not exists device_trust (
                    device_id text primary key,
                    is_trusted boolean not null default 0,
                    trust_level integer not null default 0,
                    trust_token text,
                    trusted_at integer,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null
                );

                create table if not exists learning_progress (
                    progress_id text primary key,
                    student_id text not null,
                    grade integer not null,
                    subject text not null,
                    chapter text,
                    score integer not null,
                    attempts integer not null,
                    updated_at text not null,
                    topic text,
                    metadata text not null,
                    created_at integer not null,
                    stored_at integer not null
                );

                create index if not exists idx_learning_progress_student
                on learning_progress(student_id, stored_at desc);

                create table if not exists quiz_sessions (
                    quiz_session_id text primary key,
                    student_id text not null,
                    active_quiz_id text not null,
                    grade integer,
                    subject text,
                    chapter text,
                    topic text,
                    current_question integer not null,
                    score integer not null,
                    total_questions integer not null,
                    status text not null,
                    questions text not null,
                    answers text not null,
                    metadata text not null,
                    created_at integer not null,
                    updated_at integer not null
                );

                create index if not exists idx_quiz_sessions_student
                on quiz_sessions(student_id, updated_at desc);

                create table if not exists student_device_map (
                    device_id text primary key,
                    student_id text unique,
                    updated_at integer not null
                );
                """
            )
            # Auto-backfill student_device_map from devices metadata if empty
            cursor = connection.execute("select count(*) from student_device_map")
            if cursor.fetchone()[0] == 0:
                connection.execute(
                    """
                    insert or ignore into student_device_map (device_id, student_id, updated_at)
                    select device_id, json_extract(metadata, '$.student_id'), strftime('%s','now')
                    from devices
                    where json_extract(metadata, '$.student_id') is not null
                    """
                )

    def map_student_device(self, device_id: str, student_id: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                insert into student_device_map (device_id, student_id, updated_at)
                values (?, ?, ?)
                on conflict(device_id) do update set student_id=excluded.student_id, updated_at=excluded.updated_at
                on conflict(student_id) do update set device_id=excluded.device_id, updated_at=excluded.updated_at
                """,
                (device_id, student_id, now),
            )

    def get_student_for_device(self, device_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("select student_id from student_device_map where device_id = ?", (device_id,)).fetchone()
            return str(row["student_id"]) if row and row["student_id"] else None

    def get_device_for_student(self, student_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("select device_id from student_device_map where student_id = ?", (student_id,)).fetchone()
            return str(row["device_id"]) if row and row["device_id"] else None

    def list_student_device_mappings(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select device_id, student_id, updated_at from student_device_map").fetchall()
            return [dict(row) for row in rows]

    def upsert_classroom(self, classroom_name: str | None, teacher_name: str | None, sync_mode: str, metadata: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                insert into classroom (id, classroom_name, teacher_name, sync_mode, metadata, updated_at)
                values (1, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    classroom_name = excluded.classroom_name,
                    teacher_name = excluded.teacher_name,
                    sync_mode = excluded.sync_mode,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (classroom_name, teacher_name, sync_mode, json.dumps(metadata), now),
            )
        return self.get_classroom()

    def get_classroom(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("select * from classroom where id = 1").fetchone()
        if row is None:
            return {"classroom_name": None, "teacher_name": None, "sync_mode": "offline", "metadata": {}}
        return {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}

    def register_device(
        self,
        device_name: str,
        role: str,
        classroom: str | None,
        metadata: dict[str, Any],
        auth_token: str,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        device_id = device_id or str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into devices (device_id, device_name, role, classroom, auth_token, status, metadata, created_at, updated_at, last_seen)
                values (?, ?, ?, ?, ?, 'online', ?, ?, ?, ?)
                """,
                (device_id, device_name, role, classroom, auth_token, json.dumps(metadata), now, now, now),
            )
        return {
            "device_id": device_id,
            "device_name": device_name,
            "role": role,
            "status": "online",
            "auth_token": auth_token,
            "classroom": classroom,
            "metadata": metadata,
        }

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from devices order by updated_at desc").fetchall()
        return [self._row_to_device(row) for row in rows]

    def heartbeat(self, device_id: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "update devices set last_seen = ?, updated_at = ?, status = 'online' where device_id = ?",
                (now, now, device_id),
            )

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from devices where device_id = ?", (device_id,)).fetchone()
        return self._row_to_device(row) if row else None

    def _row_to_device(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "metadata": json.loads(row["metadata"] or "{}"),
        }

    def add_pack(self, pack: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        pack_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into packs (
                    pack_id, pack_name, version, subject, grade, chapter,
                    file_path, checksum, size_bytes, metadata, created_at, updated_at, last_accessed, hits
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    pack_id,
                    pack["pack_name"],
                    pack["version"],
                    pack.get("subject"),
                    pack.get("grade"),
                    pack.get("chapter"),
                    pack["file_path"],
                    pack["checksum"],
                    pack["size_bytes"],
                    json.dumps(pack.get("metadata", {})),
                    now,
                    now,
                    now,
                ),
            )
        return {"pack_id": pack_id, **pack}

    def list_packs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from packs order by updated_at desc").fetchall()
        return [self._row_to_pack(row) for row in rows]

    def get_pack(self, pack_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from packs where pack_id = ?", (pack_id,)).fetchone()
        return self._row_to_pack(row) if row else None

    def touch_pack(self, pack_id: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "update packs set last_accessed = ?, updated_at = ?, hits = hits + 1 where pack_id = ?",
                (now, now, pack_id),
            )

    def _row_to_pack(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "metadata": json.loads(row["metadata"] or "{}"),
        }

    def start_session(self, session: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        session_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into sync_sessions (
                    session_id, device_id, resource_type, resource_id, status,
                    offset_bytes, total_bytes, retry_count, checksum, metadata, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session.get("device_id"),
                    session.get("resource_type"),
                    session.get("resource_id"),
                    session.get("status", "pending"),
                    session.get("offset_bytes", 0),
                    session.get("total_bytes"),
                    session.get("checksum"),
                    json.dumps(session.get("metadata", {})),
                    now,
                    now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from sync_sessions where session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from sync_sessions order by updated_at desc").fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]

    def update_session(self, session_id: str, **changes: Any) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        updated = {**session, **changes}
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                update sync_sessions set
                    status = ?, offset_bytes = ?, total_bytes = ?, retry_count = ?, checksum = ?, metadata = ?, updated_at = ?
                where session_id = ?
                """,
                (
                    updated.get("status", session["status"]),
                    updated.get("offset_bytes", session["offset_bytes"]),
                    updated.get("total_bytes", session.get("total_bytes")),
                    updated.get("retry_count", session.get("retry_count", 0)),
                    updated.get("checksum", session.get("checksum")),
                    json.dumps(updated.get("metadata", {})),
                    now,
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def increment_retry(self, session_id: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        return self.update_session(session_id, retry_count=int(session.get("retry_count", 0)) + 1, status="retrying")

    def set_cache(self, key: str, payload: dict[str, Any]) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                insert into cache_entries (cache_key, payload, created_at, updated_at)
                values (?, ?, ?, ?)
                on conflict(cache_key) do update set payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (key, json.dumps(payload), now, now),
            )

    def get_cache(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from cache_entries where cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def create_device_session(self, device_id: str, classroom: str, student_name: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = int(time.time())
        session_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into device_sessions (session_id, device_id, classroom, student_name, session_start, status, sync_status, last_heartbeat, metadata, created_at, updated_at)
                values (?, ?, ?, ?, ?, 'active', 'idle', ?, ?, ?, ?)
                """,
                (session_id, device_id, classroom, student_name, now, now, json.dumps(metadata or {}), now, now),
            )
        return self.get_device_session(session_id)

    def get_device_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from device_sessions where session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}

    def list_device_sessions(self, device_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if device_id:
                rows = connection.execute("select * from device_sessions where device_id = ? order by updated_at desc", (device_id,)).fetchall()
            else:
                rows = connection.execute("select * from device_sessions order by updated_at desc").fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]

    def update_device_session(self, session_id: str, **changes: Any) -> dict[str, Any] | None:
        session = self.get_device_session(session_id)
        if session is None:
            return None
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                update device_sessions set status = ?, sync_status = ?, last_heartbeat = ?, metadata = ?, updated_at = ?
                where session_id = ?
                """,
                (
                    changes.get("status", session["status"]),
                    changes.get("sync_status", session.get("sync_status", "idle")),
                    changes.get("last_heartbeat", now),
                    json.dumps(changes["metadata"] if "metadata" in changes else session.get("metadata", {})),
                    now,
                    session_id,
                ),
            )
        return self.get_device_session(session_id)

    def enqueue_sync(self, action: str, resource_type: str, resource_id: str | None = None, target_devices: list[str] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = int(time.time())
        queue_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into sync_queue (queue_id, action, resource_type, resource_id, target_devices, status, metadata, created_at, updated_at)
                values (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (queue_id, action, resource_type, resource_id, json.dumps(target_devices or []), json.dumps(metadata or {}), now, now),
            )
        return self.get_sync_queue_item(queue_id)

    def get_sync_queue_item(self, queue_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from sync_queue where queue_id = ?", (queue_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "target_devices": json.loads(row["target_devices"] or "[]"), "metadata": json.loads(row["metadata"] or "{}")}

    def list_sync_queue(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("select * from sync_queue where status = ? order by priority desc, created_at asc", (status,)).fetchall()
            else:
                rows = connection.execute("select * from sync_queue order by priority desc, created_at asc").fetchall()
        return [{**dict(row), "target_devices": json.loads(row["target_devices"] or "[]"), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]

    def mark_sync_queue_status(self, queue_id: str, status: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("update sync_queue set status = ?, updated_at = ? where queue_id = ?", (status, now, queue_id))
        return self.get_sync_queue_item(queue_id)

    def cache_pack(self, pack_id: str, pack_name: str, version: str, file_path: str, checksum: str, size_bytes: int, subject: str | None = None, grade: int | None = None, chapter: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                insert into pack_cache (pack_id, pack_name, version, subject, grade, chapter, file_path, checksum, size_bytes, cached_at, status, metadata, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)
                on conflict(pack_id) do update set version = excluded.version, cached_at = excluded.cached_at, updated_at = excluded.updated_at
                """,
                (pack_id, pack_name, version, subject, grade, chapter, file_path, checksum, size_bytes, now, json.dumps(metadata or {}), now, now),
            )
        return self.get_cached_pack(pack_id)

    def get_cached_pack(self, pack_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from pack_cache where pack_id = ?", (pack_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}

    def list_cached_packs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from pack_cache where status = 'ready' order by access_count desc").fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]

    def touch_cached_pack(self, pack_id: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "update pack_cache set last_accessed = ?, access_count = access_count + 1 where pack_id = ?",
                (now, pack_id),
            )

    def get_backend_sync_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("select * from backend_sync_state where id = 1").fetchone()
        if row is None:
            return {"backend_available": True, "offline_mode": False, "pending_pushes": 0}
        return {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}

    def set_backend_sync_state(self, available: bool, offline_mode: bool, pending_pushes: int = 0) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                insert into backend_sync_state (id, last_sync_time, backend_available, offline_mode, pending_pushes, metadata, updated_at)
                values (1, ?, ?, ?, ?, '{}', ?)
                on conflict(id) do update set backend_available = excluded.backend_available, offline_mode = excluded.offline_mode, pending_pushes = excluded.pending_pushes, updated_at = excluded.updated_at
                """,
                (now if available else None, available, offline_mode, pending_pushes, now),
            )

    def trust_device(self, device_id: str, is_trusted: bool = True, trust_level: int = 1) -> None:
        now = int(time.time())
        trust_token = str(uuid.uuid4()) if is_trusted else None
        with self._connect() as connection:
            connection.execute(
                """
                insert into device_trust (device_id, is_trusted, trust_level, trust_token, trusted_at, metadata, created_at, updated_at)
                values (?, ?, ?, ?, ?, '{}', ?, ?)
                on conflict(device_id) do update set is_trusted = excluded.is_trusted, trust_level = excluded.trust_level, trust_token = excluded.trust_token, updated_at = excluded.updated_at
                """,
                (device_id, is_trusted, trust_level, trust_token, now if is_trusted else None, now, now),
            )

    def is_device_trusted(self, device_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("select * from device_trust where device_id = ? and is_trusted = 1", (device_id,)).fetchone()
        return row is not None

    def upsert_progress(self, progress: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        progress_id = progress.get("progress_id") or str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                insert into learning_progress (
                    progress_id, student_id, grade, subject, chapter, score,
                    attempts, updated_at, topic, metadata, created_at, stored_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(progress_id) do update set
                    student_id = excluded.student_id,
                    grade = excluded.grade,
                    subject = excluded.subject,
                    chapter = excluded.chapter,
                    score = excluded.score,
                    attempts = excluded.attempts,
                    updated_at = excluded.updated_at,
                    topic = excluded.topic,
                    metadata = excluded.metadata,
                    stored_at = excluded.stored_at
                """,
                (
                    progress_id,
                    progress["student_id"],
                    progress["grade"],
                    progress["subject"],
                    progress.get("chapter"),
                    progress["score"],
                    progress["attempts"],
                    progress["updated_at"],
                    progress.get("topic"),
                    json.dumps(progress.get("metadata", {})),
                    now,
                    now,
                ),
            )
        record = self.get_progress(progress_id)
        if record is None:
            raise RuntimeError("Failed to store progress")
        return record

    def get_progress(self, progress_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from learning_progress where progress_id = ?", (progress_id,)).fetchone()
        return self._row_to_progress(row) if row else None

    def list_progress(self, student_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from learning_progress where student_id = ? order by stored_at desc",
                (student_id,),
            ).fetchall()
        return [self._row_to_progress(row) for row in rows]

    def _row_to_progress(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "metadata": json.loads(row["metadata"] or "{}"),
        }

    def create_quiz_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        quiz_session_id = payload.get("quiz_session_id") or str(uuid.uuid4())
        questions = payload.get("questions") or []
        total_questions = int(payload.get("total_questions") or len(questions))
        with self._connect() as connection:
            connection.execute(
                """
                insert into quiz_sessions (
                    quiz_session_id, student_id, active_quiz_id, grade, subject, chapter,
                    topic, current_question, score, total_questions, status, questions,
                    answers, metadata, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quiz_session_id,
                    payload["student_id"],
                    payload["active_quiz_id"],
                    payload.get("grade"),
                    payload.get("subject"),
                    payload.get("chapter"),
                    payload.get("topic"),
                    int(payload.get("current_question", 0)),
                    int(payload.get("score", 0)),
                    total_questions,
                    payload.get("status", "active"),
                    json.dumps(questions),
                    json.dumps(payload.get("answers", [])),
                    json.dumps(payload.get("metadata", {})),
                    now,
                    now,
                ),
            )
        record = self.get_quiz_session(quiz_session_id)
        if record is None:
            raise RuntimeError("Failed to create quiz session")
        return record

    def get_quiz_session(self, quiz_session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from quiz_sessions where quiz_session_id = ?",
                (quiz_session_id,),
            ).fetchone()
        return self._row_to_quiz_session(row) if row else None

    def list_quiz_sessions(self, student_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from quiz_sessions where student_id = ? order by updated_at desc",
                (student_id,),
            ).fetchall()
        return [self._row_to_quiz_session(row) for row in rows]

    def active_quiz_session(self, student_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select * from quiz_sessions
                where student_id = ? and status = 'active'
                order by updated_at desc
                limit 1
                """,
                (student_id,),
            ).fetchone()
        return self._row_to_quiz_session(row) if row else None

    def advance_quiz_session(self, quiz_session_id: str, answer: dict[str, Any]) -> dict[str, Any] | None:
        session = self.get_quiz_session(quiz_session_id)
        if session is None:
            return None
        answers = list(session.get("answers") or [])
        answers.append(answer)
        raw_score_delta = answer.get("score_delta")
        score_delta = int(raw_score_delta if raw_score_delta is not None else (1 if answer.get("correct") is True else 0))
        score = int(session.get("score", 0)) + score_delta
        current_question = int(session.get("current_question", 0)) + 1
        total_questions = int(session.get("total_questions", 0))
        status = "completed" if total_questions and current_question >= total_questions else "active"
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                update quiz_sessions
                set current_question = ?, score = ?, status = ?, answers = ?, updated_at = ?
                where quiz_session_id = ?
                """,
                (current_question, score, status, json.dumps(answers), now, quiz_session_id),
            )
        return self.get_quiz_session(quiz_session_id)

    def _row_to_quiz_session(self, row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["questions"] = json.loads(row["questions"] or "[]")
        record["answers"] = json.loads(row["answers"] or "[]")
        record["metadata"] = json.loads(row["metadata"] or "{}")
        total = int(row["total_questions"] or 0)
        current = int(row["current_question"] or 0)
        record["progress"] = {
            "current_question": current,
            "total_questions": total,
            "completed": total > 0 and current >= total,
            "percent": round((current / total) * 100, 2) if total else 0.0,
        }
        return record
