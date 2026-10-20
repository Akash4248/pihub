from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.chapter_naming import canonical_chapter_title

SUBJECT_LABELS = {
    "biology": "Biology",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
    "physics": "Physics",
    "science": "Science",
    "social_science": "Social Science",
}

SCHEMA = """
CREATE TABLE chapters (
    chapter_id VARCHAR PRIMARY KEY,
    subject VARCHAR NOT NULL,
    grade INTEGER NOT NULL,
    chapter_name VARCHAR NOT NULL,
    description TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE chapter_video_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id TEXT NOT NULL,
    grade INTEGER NOT NULL,
    subject TEXT NOT NULL,
    chapter_title TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    video_title TEXT NOT NULL,
    video_url TEXT NOT NULL,
    video_id TEXT,
    rank INTEGER NOT NULL,
    duration_seconds INTEGER,
    language TEXT NOT NULL DEFAULT 'en',
    description TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(chapter_id, rank),
    FOREIGN KEY(chapter_id) REFERENCES chapters(chapter_id) ON DELETE CASCADE
);
CREATE INDEX idx_video_res_chapter ON chapter_video_resources(chapter_id, rank);
CREATE INDEX idx_video_res_grade_subject ON chapter_video_resources(grade, subject, rank);
CREATE UNIQUE INDEX idx_video_res_video_id ON chapter_video_resources(video_id) WHERE video_id IS NOT NULL;
"""


def match_key(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_manifest_chapters(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    chapters: list[dict[str, Any]] = []
    for entry in data.get("curriculum_index", {}).values():
        grade = int(entry["grade"])
        subject_key = entry["subject"]
        for chapter in entry.get("chapters", []):
            chapters.append(
                {
                    "chapter_id": chapter["chapter_id"],
                    "grade": grade,
                    "subject_key": subject_key,
                    "subject": SUBJECT_LABELS.get(subject_key, subject_key.replace("_", " ").title()),
                    "chapter_name": chapter["chapter_name"],
                    "relative_path": chapter["relative_path"],
                }
            )
    return chapters


def old_video_rows(connection: sqlite3.Connection) -> tuple[dict[str, str], list[sqlite3.Row]]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT chapter_id, grade, subject, chapter_name FROM chapters"
    ).fetchall()
    return {str(row["chapter_id"]): str(row["chapter_name"]) for row in rows}, rows


def build_id_map(
    old_rows: list[sqlite3.Row],
    new_chapters: list[dict[str, Any]],
) -> dict[str, str]:
    by_subject: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    by_grade: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for chapter in new_chapters:
        title_key = match_key(chapter["chapter_name"])
        by_subject.setdefault((chapter["grade"], chapter["subject_key"], title_key), []).append(chapter)
        by_grade.setdefault((chapter["grade"], title_key), []).append(chapter)

    mapping: dict[str, str] = {}
    for row in old_rows:
        title = canonical_chapter_title(str(row["chapter_name"]))
        subject = str(row["subject"] or "").lower().replace(" ", "_")
        key = match_key(title)
        candidates = by_subject.get((int(row["grade"]), subject, key), [])
        if not candidates:
            candidates = by_grade.get((int(row["grade"]), key), [])
        if candidates:
            mapping[str(row["chapter_id"])] = candidates[0]["chapter_id"]
    return mapping


def rebuild(db_path: Path, manifest_path: Path, apply: bool) -> dict[str, Any]:
    chapters = load_manifest_chapters(manifest_path)
    old_connection = sqlite3.connect(db_path)
    old_chapters, old_rows = old_video_rows(old_connection)
    videos = old_connection.execute(
        """
        SELECT chapter_id, grade, subject, chapter_title, channel_name, video_title,
               video_url, video_id, rank, duration_seconds, language, description, created_at
        FROM chapter_video_resources
        """
    ).fetchall()
    old_connection.close()

    id_map = build_id_map(old_rows, chapters)
    new_by_id = {chapter["chapter_id"]: chapter for chapter in chapters}

    report: dict[str, Any] = {
        "db": str(db_path),
        "manifest": str(manifest_path),
        "old_chapters": len(old_chapters),
        "new_chapters": len(chapters),
        "mapped_old_chapters": len(id_map),
        "old_videos": len(videos),
        "preserved_videos": 0,
        "dropped_videos": 0,
        "applied": False,
    }

    preserved = []
    seen: set[tuple[str, int]] = set()
    for video in videos:
        new_id = id_map.get(str(video["chapter_id"]))
        if new_id is None:
            continue
        key = (new_id, int(video["rank"]))
        if key in seen:
            continue
        seen.add(key)
        chapter = new_by_id[new_id]
        preserved.append(
            (
                new_id,
                chapter["grade"],
                chapter["subject"],
                chapter["chapter_name"],
                video["channel_name"],
                video["video_title"],
                video["video_url"],
                video["video_id"],
                video["rank"],
                video["duration_seconds"],
                video["language"],
                video["description"],
                video["created_at"],
            )
        )

    report["preserved_videos"] = len(preserved)
    report["dropped_videos"] = len(videos) - len(preserved)
    if not apply:
        return report

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{timestamp}")
    shutil.copy2(db_path, backup)
    report["backup"] = str(backup)

    temp_path = db_path.with_name(f"{db_path.name}.rebuild-{timestamp}")
    if temp_path.exists():
        temp_path.unlink()
    connection = sqlite3.connect(temp_path)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.executescript(SCHEMA)
        connection.executemany(
            """
            INSERT INTO chapters (chapter_id, subject, grade, chapter_name, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chapter["chapter_id"],
                    chapter["subject"],
                    chapter["grade"],
                    chapter["chapter_name"],
                    f"Chapter on {chapter['chapter_name']}",
                    created_at,
                )
                for chapter in chapters
            ],
        )
        connection.executemany(
            """
            INSERT INTO chapter_video_resources (
                chapter_id, grade, subject, chapter_title, channel_name, video_title,
                video_url, video_id, rank, duration_seconds, language, description, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            preserved,
        )
        connection.commit()
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        connection.close()
    os.replace(temp_path, db_path)
    report["applied"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild content_engine.db from the canonical curriculum manifest.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = rebuild(args.db, args.manifest, args.apply)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
