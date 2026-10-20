#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "curriculum-builder"))

from curriculum_manifest_builder import CurriculumManifestBuilder  # noqa: E402
from curriculum_scanner import CurriculumScanner  # noqa: E402
from shared.chapter_naming import CHAPTER, canonical_chapter_title, classify_pdf_path  # noqa: E402
from shared.textbook_paths import find_textbooks_root  # noqa: E402


PREFIX_PATTERN = re.compile(r"^\s*(?:chapter|unit|lesson|topic)\s*[-:#.]?\s*(?:\d+|[ivxlcdm]+)\b", re.I)
CODE_PATTERN = re.compile(r"^[a-z]{2,4}\d{3}$", re.I)
EXPECTED_SUBJECTS = {
    "mathematics": "mathematics",
    "science": "science",
    "social": "social_science",
}


def validate(
    root: Path,
    expected_pdfs: int | None = None,
    db_path: Path | None = None,
    pdf_manifest_path: Path | None = None,
) -> dict[str, Any]:
    pdf_paths = sorted(root.rglob("*.pdf"))
    classifications = Counter(classify_pdf_path(path.relative_to(root).as_posix()) for path in pdf_paths)
    scan = CurriculumScanner(root).scan()
    manifest = CurriculumManifestBuilder.create_manifest_from_scan(scan, version="validation")
    expected_chapters = classifications[CHAPTER]
    errors: list[str] = []
    warnings: list[str] = []

    if expected_pdfs is not None and len(pdf_paths) != expected_pdfs:
        errors.append(f"PDF count is {len(pdf_paths)}, expected {expected_pdfs}")

    if scan["metadata"]["total_pdfs"] != expected_chapters:
        errors.append(
            f"Scanner counted {scan['metadata']['total_pdfs']} chapters, expected {expected_chapters}"
        )

    chapter_ids: list[str] = []
    for key, entry in manifest["curriculum_index"].items():
        titles: list[str] = []
        for chapter in entry["chapters"]:
            title = str(chapter.get("chapter_name") or "").strip()
            chapter_id = str(chapter.get("chapter_id") or "").strip()
            relative_path = str(chapter.get("relative_path") or "")
            chapter_ids.append(chapter_id)
            titles.append(title.lower())
            if not title:
                errors.append(f"{key}: blank chapter title for {relative_path}")
            if PREFIX_PATTERN.match(title):
                errors.append(f"{key}: prefixed title {title!r} for {relative_path}")
            if CODE_PATTERN.fullmatch(title):
                errors.append(f"{key}: code-like title {title!r} for {relative_path}")
            if not (root / relative_path).is_file():
                errors.append(f"{key}: missing PDF {relative_path}")
            if chapter.get("kind", CHAPTER) != CHAPTER:
                errors.append(f"{key}: non-chapter PDF in manifest {relative_path}")

            canonical = canonical_chapter_title(title, relative_path=relative_path)
            if title != canonical:
                warnings.append(f"{key}: title {title!r} is not canonical ({canonical!r})")

            if relative_path.startswith("class_11/") or relative_path.startswith("class_12/"):
                if chapter.get("chapter_number") is None:
                    errors.append(f"{key}: class 11/12 chapter has no chapter number: {relative_path}")
                if CODE_PATTERN.fullmatch(Path(relative_path).stem):
                    expected = title.strip()
                    if expected.lower() == Path(relative_path).stem.lower():
                        errors.append(f"{key}: class 11/12 override missing for {relative_path}")

            top_level = relative_path.split("/", 1)[0]
            expected_subject = EXPECTED_SUBJECTS.get(top_level)
            if expected_subject and entry.get("subject") != expected_subject:
                errors.append(
                    f"{key}: expected subject {expected_subject!r}, found {entry.get('subject')!r}"
                )

        duplicates = [title for title, count in Counter(titles).items() if count > 1]
        if duplicates:
            errors.append(f"{key}: duplicate chapter titles {duplicates}")

    duplicate_ids = [chapter_id for chapter_id, count in Counter(chapter_ids).items() if count > 1]
    if duplicate_ids:
        errors.append(f"Duplicate chapter IDs: {duplicate_ids[:10]}")

    if expected_chapters and len(chapter_ids) != expected_chapters:
        errors.append(f"Manifest has {len(chapter_ids)} chapters, expected {expected_chapters}")

    missing_grades = sorted(set(range(1, 13)) - set(scan["metadata"]["grades"]))
    if missing_grades:
        errors.append(f"Missing grades: {missing_grades}")

    db_report: dict[str, Any] | None = None
    if db_path and db_path.exists():
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                "SELECT chapter_id, grade, subject, chapter_name, created_at FROM chapters"
            ).fetchall()
            videos = connection.execute("SELECT COUNT(*) FROM chapter_video_resources").fetchone()[0]
            duplicate_video_ids = connection.execute(
                "SELECT video_id, COUNT(*) FROM chapter_video_resources WHERE COALESCE(video_id, '') != '' GROUP BY video_id HAVING COUNT(*) > 1"
            ).fetchall()
            duplicate_video_urls = connection.execute(
                "SELECT video_url, COUNT(*) FROM chapter_video_resources WHERE COALESCE(video_url, '') != '' GROUP BY video_url HAVING COUNT(*) > 1"
            ).fetchall()
            orphan_videos = connection.execute(
                "SELECT COUNT(*) FROM chapter_video_resources v LEFT JOIN chapters c USING(chapter_id) WHERE c.chapter_id IS NULL"
            ).fetchone()[0]
            duplicate_db_titles = connection.execute(
                "SELECT grade, subject, chapter_name, COUNT(*) FROM chapters GROUP BY grade, subject, chapter_name HAVING COUNT(*) > 1"
            ).fetchall()
            null_created = connection.execute(
                "SELECT COUNT(*) FROM chapters WHERE created_at IS NULL OR created_at = ''"
            ).fetchone()[0]
            foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        prefixed = [row for row in rows if PREFIX_PATTERN.match(str(row[3] or ""))]
        db_report = {
            "chapters": len(rows),
            "videos": videos,
            "prefixed_chapters": len(prefixed),
            "prefixed_examples": [row[3] for row in prefixed[:10]],
            "duplicate_chapter_titles": len(duplicate_db_titles),
            "duplicate_video_ids": len(duplicate_video_ids),
            "duplicate_video_urls": len(duplicate_video_urls),
            "orphan_videos": orphan_videos,
            "null_created_at": null_created,
            "foreign_key_violations": len(foreign_key_violations),
        }
        if prefixed:
            warnings.append(f"content_engine.db still has {len(prefixed)} prefixed chapter names")
        if len(rows) != len(chapter_ids):
            errors.append(f"content_engine.db has {len(rows)} chapters, manifest has {len(chapter_ids)}")
        if duplicate_db_titles:
            errors.append(f"content_engine.db has {len(duplicate_db_titles)} duplicate grade/subject/chapter titles")
        if duplicate_video_ids:
            errors.append(f"content_engine.db has {len(duplicate_video_ids)} duplicate video_id values")
        if duplicate_video_urls:
            errors.append(f"content_engine.db has {len(duplicate_video_urls)} duplicate video_url values")
        if orphan_videos:
            errors.append(f"content_engine.db has {orphan_videos} orphan video rows")
        if foreign_key_violations:
            errors.append(f"content_engine.db has {len(foreign_key_violations)} foreign-key violations")
        if null_created:
            warnings.append(f"content_engine.db has {null_created} chapters with NULL created_at")

    pdf_report: dict[str, Any] | None = None
    if pdf_manifest_path and pdf_manifest_path.exists():
        payload = json.loads(pdf_manifest_path.read_text(encoding="utf-8"))
        books = payload.get("books", []) if isinstance(payload, dict) else []
        mappings = [
            mapping
            for book in books
            for mapping in (book.get("chapter_mappings") or [])
            if isinstance(mapping, dict)
        ]
        mapped_ids = [str(mapping.get("chapter_id") or "") for mapping in mappings]
        duplicate_mapped_ids = [key for key, count in Counter(mapped_ids).items() if key and count > 1]
        missing_mapped = sorted(set(chapter_ids) - set(mapped_ids))
        extra_mapped = sorted(set(mapped_ids) - set(chapter_ids))
        pdf_report = {
            "path": str(pdf_manifest_path),
            "books": len(books),
            "chapter_mappings": len(mappings),
            "unique_chapter_ids": len(set(mapped_ids)),
            "duplicate_chapter_ids": len(duplicate_mapped_ids),
            "missing_manifest_ids": len(missing_mapped),
            "extra_manifest_ids": len(extra_mapped),
        }
        if len(books) != expected_chapters:
            errors.append(f"PDF manifest has {len(books)} books, expected {expected_chapters}")
        if duplicate_mapped_ids:
            errors.append(f"PDF manifest has {len(duplicate_mapped_ids)} duplicate chapter IDs")
        if missing_mapped:
            errors.append(f"PDF manifest is missing {len(missing_mapped)} manifest chapter IDs")
        if extra_mapped:
            errors.append(f"PDF manifest has {len(extra_mapped)} unknown chapter IDs")

    report = {
        "textbooks_root": str(root),
        "pdfs_total": len(pdf_paths),
        "chapter_pdfs": expected_chapters,
        "classifications": dict(classifications),
        "grades": scan["metadata"]["grades"],
        "subjects": scan["metadata"]["subjects"],
        "manifest_chapters": len(chapter_ids),
        "unique_chapter_ids": len(set(chapter_ids)),
        "db": db_report,
        "pdf_manifest": pdf_report,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the canonical TEXTBOOKS catalog and manifest.")
    parser.add_argument("--textbooks-root", type=Path)
    parser.add_argument("--expected-pdfs", type=int, default=354)
    parser.add_argument("--db", type=Path, default=BACKEND_ROOT / "content_engine.db")
    parser.add_argument(
        "--pdf-manifest",
        type=Path,
        default=BACKEND_ROOT / ".local_run" / "packs" / "pdf_manifests" / "pdf_manifest.json",
    )
    parser.add_argument("--output", type=Path, default=BACKEND_ROOT.parent.parent / "textbook_validation_report.json")
    args = parser.parse_args()

    root = args.textbooks_root or find_textbooks_root(Path(__file__))
    if root is None:
        print("Could not locate TEXTBOOKS", file=sys.stderr)
        return 2

    report = validate(
        root,
        expected_pdfs=args.expected_pdfs,
        db_path=args.db,
        pdf_manifest_path=args.pdf_manifest,
    )
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
