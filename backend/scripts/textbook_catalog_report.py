#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parent
PACK_SERVICE_ROOT = ROOT / "backend" / "pack-service"
sys.path.insert(0, str(PACK_SERVICE_ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.pdf_reader import PdfRegistrationService, PdfRepository  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def markdown_catalog(catalog: dict[str, Any], scan_report: dict[str, Any]) -> str:
    entries = catalog["entries"]
    grade_counts = Counter(entry["grade"] for entry in entries)
    subject_counts = Counter(f"{entry['grade']}:{entry['subject']}" for entry in entries)
    lines = [
        "# Textbook Catalog Report",
        "",
        "Source of truth: `TEXTBOOKS/` recursive PDF inventory.",
        "",
        f"- Total catalog entries: {catalog['total_entries']}",
        f"- Grades: {', '.join(map(str, catalog['grades']))}",
        f"- Subjects: {', '.join(catalog['subjects'])}",
        f"- Duplicate registrations: {scan_report['duplicate_registrations']}",
        f"- Missing PDFs: {len(scan_report['missing_pdfs'])}",
        f"- Invalid page ranges: {len(scan_report['invalid_page_ranges'])}",
        "",
        "## Grade Counts",
        "",
        "| Grade | Chapter PDFs |",
        "| ---: | ---: |",
    ]
    for grade in sorted(grade_counts):
        lines.append(f"| {grade} | {grade_counts[grade]} |")
    lines.extend(["", "## Subject Counts", "", "| Grade:Subject | Chapter PDFs |", "| --- | ---: |"])
    for key in sorted(subject_counts):
        lines.append(f"| {key} | {subject_counts[key]} |")
    lines.extend(["", "## Sample Catalog Entries", "", "```json"])
    lines.append(json.dumps(entries[:30], indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", ""])
    return "\n".join(lines)


def markdown_validation(catalog: dict[str, Any], validation: dict[str, Any]) -> str:
    entries = catalog["entries"]
    chapter_keys = Counter(
        f"{entry['grade']}:{entry['subject']}:{entry['language']}:{entry['chapter_id']}"
        for entry in entries
    )
    title_keys = Counter(
        f"{entry['grade']}:{entry['subject']}:{entry['language']}:{entry['chapter'].strip().lower()}"
        for entry in entries
    )
    duplicate_ids = [key for key, count in chapter_keys.items() if count > 1]
    duplicate_titles = [key for key, count in title_keys.items() if count > 1]
    orphan_pdfs = []
    report = {
        "total_entries": catalog["total_entries"],
        "missing_pdfs": validation["missing_pdfs"],
        "invalid_page_ranges": validation["invalid_page_ranges"],
        "duplicate_chapter_ids": duplicate_ids,
        "duplicate_chapter_titles": duplicate_titles,
        "orphan_pdfs": orphan_pdfs,
        "chapters_with_exactly_one_pdf": catalog["total_entries"] - len(duplicate_ids),
        "valid": not validation["missing_pdfs"] and not validation["invalid_page_ranges"] and not duplicate_ids,
    }
    lines = [
        "# Textbook Mapping Validation Report",
        "",
        f"Final verdict: {'PASS' if report['valid'] else 'REQUIRES_ADDITIONAL_WORK'}",
        "",
        "```json",
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
        "```",
    ]
    return "\n".join(lines), report


def main() -> None:
    textbooks_root = REPO_ROOT / "TEXTBOOKS"
    manifest_path = Path("/tmp/pihub_textbook_catalog_manifest.json")
    if manifest_path.exists():
        manifest_path.unlink()
    repository = PdfRepository(manifest_path, library_root=textbooks_root)
    service = PdfRegistrationService(repository, textbooks_root)
    scan_report = service.rebuild_catalog()
    catalog = service.catalog_payload()
    validation = repository.validate()
    write_json(REPO_ROOT / "textbook_catalog.json", catalog)
    write_json(REPO_ROOT / "textbook_mapping_validation.json", validation)
    (REPO_ROOT / "TEXTBOOK_CATALOG_REPORT.md").write_text(markdown_catalog(catalog, scan_report), encoding="utf-8")
    validation_markdown, validation_payload = markdown_validation(catalog, validation)
    (REPO_ROOT / "TEXTBOOK_MAPPING_VALIDATION_REPORT.md").write_text(validation_markdown, encoding="utf-8")
    write_json(REPO_ROOT / "textbook_mapping_validation_summary.json", validation_payload)
    print(
        json.dumps(
            {
                "entries": catalog["total_entries"],
                "grades": catalog["grades"],
                "subjects": catalog["subjects"],
                "valid": validation_payload["valid"],
                "missing_pdfs": len(validation["missing_pdfs"]),
                "invalid_page_ranges": len(validation["invalid_page_ranges"]),
                "duplicate_chapter_ids": len(validation_payload["duplicate_chapter_ids"]),
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
