from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.chapter_naming import chapter_title_for_pdf, classify_pdf_path, CHAPTER
from shared.text_normalization import normalize_curriculum_name

SUBJECT_MAP = {
    "mathematics": "maths",
    "maths": "maths",
    "science": "science",
    "social_science": "social_science",
    "biology": "biology",
    "chemistry": "chemistry",
    "physics": "physics",
}


def slug(value: Any) -> str:
    text = str(value or "").strip().lower().replace(".pdf", "")
    text = re.sub(r"[\u2013\u2014]", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def page_count(path: Path) -> int:
    try:
        output = subprocess.check_output(["pdfinfo", str(path)], text=True, stderr=subprocess.DEVNULL)
        match = re.search(r"^Pages:\s*(\d+)", output, re.MULTILINE)
        if match:
            return max(1, int(match.group(1)))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    raw = path.read_bytes()
    return max(1, len(re.findall(rb"/Type\s*/Page\b", raw)))


def path_context(relative_path: Path) -> str:
    return " ".join(
        part
        for part in relative_path.parent.parts
        if re.search(r"(?:^|[\s_-])part[\s_-]*\d+", part, re.I)
    )


def aliases_for(grade: int, subject: str, title: str, language: str, chapter_id: str) -> list[str]:
    normalized = normalize_curriculum_name(title)
    aliases = {title, normalized, slug(title), chapter_id}
    match = re.match(r"chapter\s+(\d+)\s+(.+)", normalized)
    if match:
        aliases.add(match.group(2))
        aliases.add(slug(match.group(2)))
    return sorted(alias for alias in aliases if alias)


def build_manifest(manifest_path: Path, textbooks_root: Path, pdf_library_root: Path | None = None) -> dict[str, Any]:
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    pdf_library_root = pdf_library_root or textbooks_root
    books = []
    for entry in source.get("curriculum_index", {}).values():
        grade = int(entry["grade"])
        subject = SUBJECT_MAP.get(entry["subject"], entry["subject"])
        language = entry.get("language", "english")
        for chapter in entry.get("chapters", []):
            relative_path = Path(chapter["relative_path"])
            if classify_pdf_path(relative_path.as_posix()) != CHAPTER:
                continue
            pdf_path = textbooks_root / relative_path
            title = chapter_title_for_pdf(relative_path.as_posix(), pdf_path.name)
            context = path_context(relative_path)
            context_slug = slug(context)
            chapter_id = str(chapter["chapter_id"])
            legacy_chapter_id = f"chapter_{grade}_{slug(subject)}_{slug(title)}_{slug(language)}"
            if context_slug:
                legacy_chapter_id = f"{legacy_chapter_id}_{context_slug}"
            book_id = f"pdf_grade_{grade}_{slug(subject)}_{slug(title)}"
            if context_slug:
                book_id = f"{book_id}_{context_slug}"
            book_id = f"{book_id}_{slug(language)}"
            books.append(
                {
                    "book_id": book_id,
                    "grade": grade,
                    "subject": subject,
                    "book_title": title,
                    "language": language,
                    "pdf_path": str(pdf_library_root / relative_path),
                    "pdf_file": pdf_path.name,
                    "chapter_mappings": [
                        {
                            "chapter_id": chapter_id,
                            "chapter_title": title,
                            "start_page": 1,
                            "end_page": page_count(pdf_path),
                            "aliases": sorted(
                                set(aliases_for(grade, subject, title, language, chapter_id))
                                | ({legacy_chapter_id} if legacy_chapter_id != chapter_id else set())
                            ),
                            "metadata": {
                                "source": "ncert_textbook_catalog",
                                "chapter_level_pdf": True,
                                "pdf_file": pdf_path.name,
                                "relative_path": relative_path.as_posix(),
                                "path_context": context,
                            },
                        }
                    ],
                    "metadata": {"source": "ncert_textbook_catalog", "chapter_level_pdf": True},
                }
            )
    books.sort(key=lambda item: (item["grade"], item["subject"], item["language"], item["book_title"], item["book_id"]))
    return {
        "manifest_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "books": books,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the runtime PDF manifest from the curriculum manifest.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--textbooks-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--pdf-library-root",
        type=Path,
        default=None,
        help="Root used in pdf_path fields (use /shared/textbooks for Docker)",
    )
    args = parser.parse_args()
    payload = build_manifest(args.manifest, args.textbooks_root, args.pdf_library_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "books": len(payload["books"])}, indent=2))


if __name__ == "__main__":
    main()
