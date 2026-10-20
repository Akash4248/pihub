from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from shared.chapter_naming import canonical_chapter_title

SUBJECT_LABELS = {
    "biology": "Biology",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
    "physics": "Physics",
    "science": "Science",
    "social_science": "Social Science",
}


def match_key(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_manifest_index(path: Path) -> dict[tuple[int, str], list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    index: dict[tuple[int, str], list[dict]] = {}
    for entry in data.get("curriculum_index", {}).values():
        grade = int(entry["grade"])
        for chapter in entry.get("chapters", []):
            key = (grade, match_key(chapter["chapter_name"]))
            index.setdefault(key, []).append(
                {
                    "title": chapter["chapter_name"],
                    "subject": entry["subject"],
                }
            )
    return index


def resolve_match(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    subjects = {candidate["subject"] for candidate in candidates}
    titles = {candidate["title"] for candidate in candidates}
    if len(subjects) == 1 and len(titles) == 1:
        return candidates[0]
    return None


def migrate(db_path: Path, manifest_path: Path, apply: bool) -> dict:
    index = load_manifest_index(manifest_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT chapter_id, grade, subject, chapter_name FROM chapters WHERE grade BETWEEN 6 AND 10"
    ).fetchall()

    updates: list[tuple[str, str, str]] = []
    unmatched: list[str] = []
    for row in rows:
        title = canonical_chapter_title(row["chapter_name"])
        candidates = index.get((int(row["grade"]), match_key(title)), [])
        match = resolve_match(candidates)
        if match:
            subject = SUBJECT_LABELS.get(match["subject"], match["subject"].replace("_", " ").title())
            title = match["title"]
        else:
            subject = row["subject"]
            unmatched.append(row["chapter_name"])

        if title != row["chapter_name"] or subject != row["subject"]:
            updates.append((row["chapter_id"], title, subject))

    report = {
        "db": str(db_path),
        "manifest": str(manifest_path),
        "rows_examined": len(rows),
        "chapters_updated": len(updates),
        "unmatched_rows": len(unmatched),
        "unmatched_examples": unmatched[:20],
        "applied": False,
    }

    if not apply:
        connection.close()
        return report

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{timestamp}")
    shutil.copy2(db_path, backup)
    report["backup"] = str(backup)

    with connection:
        for chapter_id, title, subject in updates:
            connection.execute(
                "UPDATE chapters SET chapter_name = ?, subject = ? WHERE chapter_id = ?",
                (title, subject, chapter_id),
            )
            connection.execute(
                "UPDATE chapter_video_resources SET chapter_title = ?, subject = ? WHERE chapter_id = ?",
                (title, subject, chapter_id),
            )
    connection.close()
    report["applied"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize legacy content_engine.chapters names and subjects.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = migrate(args.db, args.manifest, args.apply)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
