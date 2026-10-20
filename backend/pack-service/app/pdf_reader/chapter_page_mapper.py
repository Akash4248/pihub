from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from shared.chapter_naming import (
    chapter_number_for_pdf,
    chapter_title_for_pdf,
    classify_pdf_path,
)
from shared.text_normalization import normalize_curriculum_name

from .models import PdfChapterMapping


class ChapterPageMapper:
    """Build and validate chapter-to-PDF page mappings."""

    def chapter_id(self, grade: int, subject: str, chapter: str, language: str = "english") -> str:
        chapter_part = self.slug(chapter)
        return f"chapter_{grade}_{self.slug(subject)}_{chapter_part}_{self.slug(language)}"

    def mapping_for_file(
        self,
        pdf_path: Path,
        grade: int,
        subject: str,
        language: str = "english",
        relative_path: str | None = None,
    ) -> PdfChapterMapping:
        reference = relative_path or pdf_path.name
        title = chapter_title_for_pdf(reference, pdf_path.name)
        chapter_id = self.chapter_id(grade, subject, title, language)
        return PdfChapterMapping(
            chapter_id=chapter_id,
            chapter_title=title,
            start_page=1,
            end_page=self.page_count(pdf_path),
            aliases=self.aliases_for(grade, subject, title, language),
            metadata={
                "source": "pdf_library_scan",
                "pdf_file": pdf_path.name,
                "relative_path": reference,
                "chapter_number": chapter_number_for_pdf(reference, pdf_path.name),
                "classification": classify_pdf_path(reference),
            },
        )

    def validate_mapping(self, mapping: PdfChapterMapping, page_count: int | None = None) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if mapping.start_page < 1:
            errors.append("start_page<1")
        if mapping.end_page < mapping.start_page:
            errors.append("end_page_before_start_page")
        if page_count is not None and mapping.end_page > page_count:
            errors.append("end_page_exceeds_pdf_pages")
        return not errors, errors

    def aliases_for(self, grade: int, subject: str, chapter: str, language: str = "english") -> list[str]:
        normalized = normalize_curriculum_name(chapter)
        slug = self.slug(chapter)
        aliases = {
            chapter,
            normalized,
            slug,
            self.chapter_id(grade, subject, chapter, language),
            f"chapter_{grade}_{self.slug(subject)}_{slug}_{self.slug(language)}",
        }
        match = re.match(r"chapter\s+(\d+)\s+(.+)", normalized)
        if match:
            aliases.add(match.group(2))
            aliases.add(self.slug(match.group(2)))
        return sorted(alias for alias in aliases if alias)

    @staticmethod
    def chapter_title_from_file(pdf_path: Path) -> str:
        return chapter_title_for_pdf(pdf_path.name, pdf_path.name)

    @staticmethod
    def slug(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace(".pdf", "")
        normalized = re.sub(r"[\u2013\u2014]", "-", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.replace("&", " and ")
        return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")

    @staticmethod
    def page_count(pdf_path: Path) -> int:
        try:
            output = subprocess.check_output(["pdfinfo", str(pdf_path)], text=True, stderr=subprocess.DEVNULL)
            match = re.search(r"^Pages:\s*(\d+)", output, re.MULTILINE)
            if match:
                return max(1, int(match.group(1)))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            raw = pdf_path.read_bytes()
        except OSError:
            return 1
        matches = re.findall(rb"/Type\s*/Page\b", raw)
        return max(1, len(matches))
