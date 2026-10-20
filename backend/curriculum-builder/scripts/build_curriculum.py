#!/usr/bin/env python3
"""Orchestrator: Scan textbooks and run end-to-end build using running services.

Safe execution strategy: defaults to dry-run and single-chapter pilot unless --full provided.
This script uses Docker+curl to call the running `content-pipeline` and `pack-service`
containers so it does not require published host ports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).parent.parent))

from build_utils import (
    copy_to_container,
    ingest_directory_via_content_pipeline,
    generate_pack_via_pack_service,
)

from curriculum_scanner import CurriculumScanner
from subject_mapper import SubjectMapper
from shared.text_normalization import normalize_curriculum_name
from shared.textbook_paths import find_textbooks_root

logger = logging.getLogger("build_curriculum")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


BUILD_CACHE = Path(__file__).parent.parent / "build_cache.json"
REPORT_DIR = Path(__file__).resolve().parent.parent / "build_reports"
CONTENT_CONTAINER = "pihub-content-pipeline"
PACK_CONTAINER = "pihub-pack-service"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_metadata_from_path(path: Path) -> Dict[str, Optional[str]]:
    """Infer grade, subject, chapter, language from a file path.

    Rules:
    - grade: from 'class X', 'grade X', or 'Xth' patterns in path parts
    - subject: look for folder names containing known subjects
    - chapter: from filename with extension removed, strip trailing numbers/parts
    - language: 'kannada' if folder contains 'kannada', else 'english'
    """
    import re

    text = str(path)
    parts = [p.lower() for p in path.parts]

    # Grade
    grade = None
    for p in parts:
        m = re.search(r"class[ _-]?(\d{1,2})", p)
        if m:
            grade = int(m.group(1))
            break
        m = re.search(r"grade[ _-]?(\d{1,2})", p)
        if m:
            grade = int(m.group(1))
            break
        m = re.search(r"(\d{1,2})(?:th|st|nd|rd)\b", p)
        if m:
            grade = int(m.group(1))
            break

    # Subject
    subject = None
    SUBJECT_PATTERNS = ["math", "maths", "mathematics", "science", "social", "social science", "english"]
    for p in parts:
        for s in SUBJECT_PATTERNS:
            if s in p:
                # normalize
                if "math" in s:
                    subject = "maths"
                elif "social" in s:
                    subject = "social_science"
                else:
                    subject = normalize_curriculum_name(s)
                break
        if subject:
            break

    # Chapter from filename
    stem = path.stem
    # Remove common suffixes like 'part', numbers, year ranges
    stem_clean = re.sub(r"(?i)\bpart\b[\s\-]*\d+", "", stem)
    stem_clean = re.sub(r"[\(\)\[\]]", "", stem_clean)
    stem_clean = re.sub(r"[-_]{1,}|\s{2,}", " ", stem_clean)
    # remove trailing numbers and separators
    stem_clean = re.sub(r"[\s\-_]*(\d{1,3})(?:[\s\-_]*v\d+)?$", "", stem_clean)
    chapter = normalize_curriculum_name(stem_clean.strip())
    # Remove file-year like '2025-26'
    chapter = re.sub(r"\b\d{4}(?:[-/]\d{2,4})?\b", "", chapter).strip()
    # Remove trailing hyphens/spaces
    chapter = normalize_curriculum_name(chapter)

    # Language
    language = "english"
    if any("kannada" in p for p in parts):
        language = "kannada"

    return {"grade": grade, "subject": subject, "chapter": chapter or None, "language": language}


def load_cache() -> Dict[str, Dict]:
    if BUILD_CACHE.exists():
        return json.loads(BUILD_CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: Dict[str, Dict]) -> None:
    BUILD_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _count_statuses(report: Dict[str, Dict]) -> Dict[str, int]:
    counts = {"completed": 0, "skipped": 0, "failed": 0, "dry-run": 0}
    for item in report.values():
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _update_report_index(report_path: Path, subject: Optional[str], grade: Optional[int], report: Dict[str, Dict], dry_run: bool) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = REPORT_DIR / "build_report_index.json"
    index = {"reports": []}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            index = {"reports": []}

    normalized_subject = SubjectMapper.normalize_subject(normalize_curriculum_name(subject)) if subject else None
    counts = _count_statuses(report)
    total_items = len(report)
    record = {
        "report_file": report_path.name,
        "report_path": str(report_path),
        "subject": normalized_subject or "all",
        "grade": grade,
        "dry_run": dry_run,
        "created_at": datetime.utcnow().isoformat(),
        "total_items": total_items,
        "completed": counts["completed"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "dry_run_items": counts["dry-run"],
    }

    reports = index.get("reports") or []
    reports.append(record)
    index = {
        "updated_at": datetime.utcnow().isoformat(),
        "reports": reports,
        "by_subject": {},
    }
    for item in reports:
        key = item.get("subject") or "all"
        index["by_subject"].setdefault(key, []).append(item)

    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def run(files: List[Path], dry_run: bool = True, force: bool = False) -> Dict[str, Dict]:
    """Process a list of textbook files through the pipeline."""
    cache = load_cache()
    report: Dict[str, Dict] = {}

    for file_path in files:
        logger.info("Processing: %s", file_path)

        checksum = sha256_file(file_path)
        cached = cache.get(str(file_path))
        if not force and cached and cached.get("checksum") == checksum:
            logger.info("Skipping unchanged file: %s", file_path)
            report[str(file_path)] = {"status": "skipped", "reason": "unchanged"}
            continue

        # Preserve original relative path under /shared/content so content-pipeline
        # can extract grade/subject metadata from the file path.
        TEXTBOOKS_ROOT = find_textbooks_root(Path(__file__))
        if TEXTBOOKS_ROOT is None:
            raise ValueError("Could not locate TEXTBOOKS directory")

        logger.debug("TEXTBOOKS_ROOT=%s, file_path=%s", TEXTBOOKS_ROOT, file_path)
        try:
            rel_parent = file_path.relative_to(TEXTBOOKS_ROOT).parent
            dest_dir = f"/shared/content/{rel_parent}"
        except Exception as exc:
            # fallback to orchestrator folder if relative path can't be determined
            logger.warning("Could not relativize %s to %s: %s", file_path, TEXTBOOKS_ROOT, exc)
            dest_dir = f"/shared/content/orchestrator/{file_path.stem}"

        dest_path = f"{dest_dir}/{file_path.name}"

        if dry_run:
            logger.info("Dry-run: would copy to container %s -> %s", file_path, dest_path)
            logger.info("Dry-run: would call ingest and pack generation")
            report[str(file_path)] = {"status": "dry-run"}
            continue

        # 1) copy into content-pipeline container
        logger.info("Copying %s -> %s inside container %s", file_path, dest_path, CONTENT_CONTAINER)
        copy_to_container(file_path, CONTENT_CONTAINER, dest_path)

        # 2) trigger ingestion (directory)
        ingest_resp = ingest_directory_via_content_pipeline(CONTENT_CONTAINER, dest_dir)
        logger.info("Ingest response: %s", ingest_resp.get("chunks_created"))

        # 3) extract metadata from ingest response
        meta = None
        results = ingest_resp.get("results") or []
        if results:
            meta = results[0].get("metadata", {})

        grade = meta.get("grade") if meta else None
        subject = normalize_curriculum_name(meta.get("subject")) if meta and meta.get("subject") else None
        chapter = normalize_curriculum_name(meta.get("textbook_name")) if meta and meta.get("textbook_name") else None

        # Ingest metadata can be noisy (e.g., subject set to filename). Prefer inferred values
        inferred = extract_metadata_from_path(file_path)
        def valid_subject(s: Optional[str]) -> bool:
            if not s:
                return False
            s_low = str(s).lower()
            if s_low.endswith('.pdf') or '.' in s_low:
                return False
            # basic sanity: subject should be short and alphabetic
            if len(s_low) > 30 and ' ' in s_low:
                return False
            return True

        grade = grade or inferred.get("grade")
        if subject and chapter and normalize_curriculum_name(subject) == normalize_curriculum_name(chapter):
            subject = inferred.get("subject")
        if not valid_subject(subject):
            subject = inferred.get("subject")
        chapter = chapter or inferred.get("chapter")
        if subject:
            subject = normalize_curriculum_name(subject)
        if chapter:
            chapter = normalize_curriculum_name(chapter)
        logger.info("Inferred metadata: %s", inferred)

        # 4) generate pack via pack-service
        pack_req = {
            "pack_type": "chapter",
            "grade": int(grade) if grade is not None else None,
            "subject": subject,
            "chapter": chapter,
        }
        logger.info("Requesting pack generation: %s", pack_req)
        pack_resp = generate_pack_via_pack_service(PACK_CONTAINER, pack_type="chapter", grade=grade, subject=subject, chapter=chapter)
        logger.info("Pack response: %s", pack_resp)

        # 5) update cache and report
        cache[str(file_path)] = {"checksum": checksum, "last_pack": pack_resp}
        report[str(file_path)] = {"status": "completed", "pack": pack_resp}

    save_cache(cache)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--textbooks-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--pilot", action="store_true", help="Run single-chapter pilot (first PDF)")
    parser.add_argument("--force", action="store_true", help="Rebuild even when cache says a chapter is unchanged")
    parser.add_argument("--grade", type=int)
    parser.add_argument("--subject")
    args = parser.parse_args()

    textbooks_root = Path(args.textbooks_root).resolve() if args.textbooks_root else find_textbooks_root(Path(__file__))
    if textbooks_root is None:
        logger.error("Could not locate TEXTBOOKS directory")
        return 1
    scanner = CurriculumScanner(textbooks_root)
    scan = scanner.scan()

    normalized_subject = SubjectMapper.normalize_subject(normalize_curriculum_name(args.subject)) if args.subject else None

    # Build list of files to process
    files: List[Path] = []
    for key, entry in scan.get("curriculum", {}).items():
        if args.grade and entry.get("grade") != args.grade:
            continue
        entry_subject = SubjectMapper.normalize_subject(normalize_curriculum_name(entry.get("subject"))) if entry.get("subject") else None
        if normalized_subject and entry_subject != normalized_subject:
            continue
        for ch in entry.get("chapters", []):
            rel = ch.get("relative_path")
            if not rel:
                continue
            files.append(textbooks_root / rel)

    if not files:
        logger.error("No textbook files found to process")
        return 1

    # Safe execution: pilot mode
    if args.pilot:
        files = [files[0]]

    report = run(files, dry_run=args.dry_run, force=args.force)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out = REPORT_DIR / f"build_report_{timestamp}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    _update_report_index(out, args.subject, args.grade, report, args.dry_run)

    latest = Path.cwd() / "build_report.json"
    latest.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Build report saved: %s", out)
    logger.info("Latest build report updated: %s", latest)


if __name__ == "__main__":
    main()
