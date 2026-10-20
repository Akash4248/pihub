from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.core.database import connect_sqlite, initialize_sqlite_database, sqlite_transaction


CLASSROOM_SCHEMA = """
create table if not exists classroom_sessions (
    session_id text primary key,
    teacher_id text not null,
    title text not null,
    active integer not null,
    created_at text not null
);

create table if not exists classroom_assignments (
    assignment_id text primary key,
    session_id text not null,
    manifest_id text not null,
    revision integer not null,
    title text not null,
    instructions text,
    due_date text,
    share_package_json text,
    created_at text not null,
    foreign key (session_id) references classroom_sessions(session_id)
);

create table if not exists classroom_students (
    id text primary key,
    session_id text not null,
    student_id text not null,
    joined_at text not null,
    unique(session_id, student_id),
    foreign key (session_id) references classroom_sessions(session_id)
);

create table if not exists classroom_submissions (
    submission_id text primary key,
    assignment_id text not null,
    student_id text not null,
    result_id text not null,
    submission_package_json text,
    verified integer not null,
    verification_json text,
    submitted_at text not null,
    foreign key (assignment_id) references classroom_assignments(assignment_id)
);

create index if not exists idx_classroom_assignments_session
on classroom_assignments(session_id, created_at desc);

create index if not exists idx_classroom_sessions_teacher
on classroom_sessions(teacher_id, created_at desc);

create index if not exists idx_classroom_submissions_assignment
on classroom_submissions(assignment_id, submitted_at desc);

create index if not exists idx_classroom_submissions_student
on classroom_submissions(student_id, submitted_at desc);

create unique index if not exists idx_classroom_submission_dedupe
on classroom_submissions(assignment_id, student_id, result_id);
"""


class ClassroomRepository:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Path(__file__).resolve().parents[2] / "storage" / "classroom.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_session(self, session: dict[str, Any]) -> dict[str, Any]:
        with sqlite_transaction(self.db_path) as connection:
            connection.execute(
                """
                insert into classroom_sessions (session_id, teacher_id, title, active, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    session["teacher_id"],
                    session["title"],
                    1 if session.get("active", True) else 0,
                    session["created_at"],
                ),
            )
        return self.get_session(session["session_id"]) or {}

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("select * from classroom_sessions order by created_at desc").fetchall()
            return [self._session_row(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from classroom_sessions where session_id = ?", (session_id,)).fetchone()
            return self._session_row(row) if row else None

    def create_assignment(self, assignment: dict[str, Any]) -> dict[str, Any]:
        with sqlite_transaction(self.db_path) as connection:
            connection.execute(
                """
                insert into classroom_assignments (
                    assignment_id, session_id, manifest_id, revision, title,
                    instructions, due_date, share_package_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment["assignment_id"],
                    assignment["session_id"],
                    assignment["manifest_id"],
                    assignment["revision"],
                    assignment["title"],
                    assignment.get("instructions"),
                    assignment.get("due_date"),
                    json.dumps(assignment.get("share_package", {}), sort_keys=True),
                    assignment["created_at"],
                ),
            )
        return self.get_assignment(assignment["assignment_id"]) or {}

    def list_assignments(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from classroom_assignments where session_id = ? order by created_at desc",
                (session_id,),
            ).fetchall()
            return [self._assignment_row(row) for row in rows]

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from classroom_assignments where assignment_id = ?", (assignment_id,)).fetchone()
            return self._assignment_row(row) if row else None

    def ensure_student(self, session_id: str, student_id: str, joined_at: str) -> None:
        with sqlite_transaction(self.db_path) as connection:
            connection.execute(
                """
                insert or ignore into classroom_students (id, session_id, student_id, joined_at)
                values (?, ?, ?, ?)
                """,
                (f"{session_id}:{student_id}", session_id, student_id, joined_at),
            )

    def create_submission(self, submission: dict[str, Any]) -> dict[str, Any]:
        with sqlite_transaction(self.db_path) as connection:
            self._insert_submission(connection, submission)
        return self.get_submission(submission["submission_id"]) or {}

    def create_submission_with_student(self, session_id: str, submission: dict[str, Any], joined_at: str) -> dict[str, Any]:
        with sqlite_transaction(self.db_path) as connection:
            connection.execute(
                """
                insert or ignore into classroom_students (id, session_id, student_id, joined_at)
                values (?, ?, ?, ?)
                """,
                (f"{session_id}:{submission['student_id']}", session_id, submission["student_id"], joined_at),
            )
            self._insert_submission(connection, submission)
        return self.get_submission(submission["submission_id"]) or {}

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("select * from classroom_submissions where submission_id = ?", (submission_id,)).fetchone()
            return self._submission_row(row) if row else None

    def list_submissions(self, assignment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from classroom_submissions where assignment_id = ? order by submitted_at desc",
                (assignment_id,),
            ).fetchall()
            return [self._submission_row(row) for row in rows]

    def analytics(self) -> dict[str, int]:
        with self._connect() as connection:
            assignments = self._count(connection, "classroom_assignments")
            students = self._count(connection, "classroom_students")
            submissions = self._count(connection, "classroom_submissions")
            return {
                "assignments_created": assignments,
                "assignments_started": students,
                "assignments_completed": submissions,
                "assignments_submitted": submissions,
            }

    def _count(self, connection: sqlite3.Connection, table: str) -> int:
        row = connection.execute(f"select count(*) as total from {table}").fetchone()
        return int(row["total"]) if row else 0

    def _session_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["active"] = bool(data["active"])
        return data

    def _assignment_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["share_package"] = json.loads(data.pop("share_package_json") or "{}")
        return data

    def _submission_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["verified"] = bool(data["verified"])
        data["submission_package"] = json.loads(data.pop("submission_package_json") or "null")
        data["verification"] = json.loads(data.pop("verification_json") or "{}")
        return data

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path)

    def _initialize(self) -> None:
        initialize_sqlite_database(self.db_path, CLASSROOM_SCHEMA)

    def _insert_submission(self, connection: sqlite3.Connection, submission: dict[str, Any]) -> None:
        connection.execute(
            """
            insert into classroom_submissions (
                submission_id, assignment_id, student_id, result_id,
                submission_package_json, verified, verification_json, submitted_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission["submission_id"],
                submission["assignment_id"],
                submission["student_id"],
                submission["result_id"],
                json.dumps(submission.get("submission_package"), sort_keys=True) if submission.get("submission_package") else None,
                1 if submission.get("verified") else 0,
                json.dumps(submission.get("verification", {}), sort_keys=True),
                submission["submitted_at"],
            ),
        )
