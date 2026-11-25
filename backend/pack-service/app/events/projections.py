from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional


class PackProjections:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.initialize()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def initialize(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pack_state (
                    pack_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    publication_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    last_event_id TEXT NOT NULL,
                    current_version_num INTEGER NOT NULL,
                    PRIMARY KEY (pack_id, version)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS classroom_pack_state (
                    pack_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    classroom_id TEXT NOT NULL,
                    distribution_status TEXT NOT NULL,
                    last_device_id TEXT,
                    download_count INTEGER DEFAULT 0,
                    sync_count INTEGER DEFAULT 0,
                    last_event_id TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    PRIMARY KEY (pack_id, version, classroom_id)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_classroom_pack_state_room
                ON classroom_pack_state(classroom_id);
            """)

    def apply_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        aggregate_id = event.get("aggregate_id")
        event_id = event.get("event_id")
        occurred_at = event.get("occurred_at")
        payload = event.get("payload") or {}
        aggregate_version = int(event.get("aggregate_version", 1))
        version = str(payload.get("version") or "1.0.0")

        with self._get_connection() as conn:
            if event_type == "PackCreated":
                conn.execute("""
                    INSERT INTO pack_state (
                        pack_id, version, publication_status, created_at,
                        published_at, last_event_id, current_version_num
                    ) VALUES (?, ?, 'draft', ?, NULL, ?, ?)
                    ON CONFLICT(pack_id, version) DO UPDATE SET
                        last_event_id = excluded.last_event_id,
                        current_version_num = excluded.current_version_num;
                """, (aggregate_id, version, occurred_at, event_id, aggregate_version))

            elif event_type == "PackPublished":
                conn.execute("""
                    UPDATE pack_state
                    SET publication_status = 'published',
                        published_at = ?,
                        last_event_id = ?,
                        current_version_num = ?
                    WHERE pack_id = ? AND version = ?;
                """, (occurred_at, event_id, aggregate_version, aggregate_id, version))

            elif event_type == "PackDownloaded":
                classroom_id = event.get("classroom_id") or "anonymous_node"
                device_id = event.get("device_id")
                conn.execute("""
                    INSERT INTO classroom_pack_state (
                        pack_id, version, classroom_id, distribution_status,
                        last_device_id, download_count, sync_count,
                        last_event_id, last_updated
                    ) VALUES (?, ?, ?, 'downloaded', ?, 1, 0, ?, ?)
                    ON CONFLICT(pack_id, version, classroom_id) DO UPDATE SET
                        distribution_status = CASE 
                            WHEN distribution_status = 'synchronized' THEN 'synchronized'
                            ELSE 'downloaded'
                        END,
                        download_count = download_count + 1,
                        last_device_id = excluded.last_device_id,
                        last_event_id = excluded.last_event_id,
                        last_updated = excluded.last_updated;
                """, (aggregate_id, version, classroom_id, device_id, event_id, occurred_at))

            elif event_type == "PackSynchronized":
                classroom_id = event.get("classroom_id") or "anonymous_node"
                device_id = event.get("device_id")
                conn.execute("""
                    INSERT INTO classroom_pack_state (
                        pack_id, version, classroom_id, distribution_status,
                        last_device_id, download_count, sync_count,
                        last_event_id, last_updated
                    ) VALUES (?, ?, ?, 'synchronized', ?, 0, 1, ?, ?)
                    ON CONFLICT(pack_id, version, classroom_id) DO UPDATE SET
                        distribution_status = 'synchronized',
                        sync_count = sync_count + 1,
                        last_device_id = excluded.last_device_id,
                        last_event_id = excluded.last_event_id,
                        last_updated = excluded.last_updated;
                """, (aggregate_id, version, classroom_id, device_id, event_id, occurred_at))

            elif event_type == "PackUpdated":
                old_version = payload.get("previous_version")
                if old_version:
                    conn.execute("""
                        UPDATE pack_state
                        SET publication_status = 'superseded'
                        WHERE pack_id = ? AND version = ?;
                    """, (aggregate_id, str(old_version)))

                conn.execute("""
                    INSERT INTO pack_state (
                        pack_id, version, publication_status, created_at,
                        published_at, last_event_id, current_version_num
                    ) VALUES (?, ?, 'draft', ?, NULL, ?, ?)
                    ON CONFLICT(pack_id, version) DO UPDATE SET
                        last_event_id = excluded.last_event_id,
                        current_version_num = excluded.current_version_num;
                """, (aggregate_id, version, occurred_at, event_id, aggregate_version))

    def get_pack_state(self, pack_id: str, version: Optional[str] = None) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            if version:
                row = conn.execute(
                    "SELECT * FROM pack_state WHERE pack_id = ? AND version = ?",
                    (pack_id, version),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM pack_state WHERE pack_id = ? ORDER BY current_version_num DESC LIMIT 1",
                    (pack_id,),
                ).fetchone()

            if not row:
                return None

            data = dict(row)
            # Fetch all classroom distributions for this pack & version
            dist_rows = conn.execute(
                "SELECT * FROM classroom_pack_state WHERE pack_id = ? AND version = ?",
                (pack_id, data["version"]),
            ).fetchall()
            data["distribution"] = [dict(d) for d in dist_rows]
            return data

    def get_classroom_packs(self, classroom_id: str) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.*, p.publication_status, p.created_at, p.published_at 
                FROM classroom_pack_state c
                LEFT JOIN pack_state p ON c.pack_id = p.pack_id AND c.version = p.version
                WHERE c.classroom_id = ?
                ORDER BY c.last_updated DESC
                """,
                (classroom_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_distribution_analytics(self) -> dict[str, Any]:
        with self._get_connection() as conn:
            total_packs = conn.execute("SELECT COUNT(DISTINCT pack_id) AS c FROM pack_state").fetchone()["c"]
            published = conn.execute("SELECT COUNT(*) AS c FROM pack_state WHERE publication_status = 'published'").fetchone()["c"]
            draft = conn.execute("SELECT COUNT(*) AS c FROM pack_state WHERE publication_status = 'draft'").fetchone()["c"]
            downloads = conn.execute("SELECT COALESCE(SUM(download_count), 0) AS c FROM classroom_pack_state").fetchone()["c"]
            syncs = conn.execute("SELECT COALESCE(SUM(sync_count), 0) AS c FROM classroom_pack_state").fetchone()["c"]
            rooms = [r["classroom_id"] for r in conn.execute("SELECT DISTINCT classroom_id FROM classroom_pack_state").fetchall()]
            event_count = conn.execute("SELECT COUNT(*) AS c FROM pack_events").fetchone()["c"]

            return {
                "total_events": event_count,
                "total_packs_tracked": total_packs,
                "published_packs_count": published,
                "draft_packs_count": draft,
                "total_downloads": downloads,
                "total_synchronizations": syncs,
                "active_classrooms": rooms,
            }

    def rebuild_projections(self, events: list[dict[str, Any]]) -> None:
        """Wipes read model tables and replays event stream to rebuild state."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM classroom_pack_state;")
            conn.execute("DELETE FROM pack_state;")

        for event in events:
            self.apply_event(event)
