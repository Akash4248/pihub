from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from shared.chapter_naming import CHAPTER, classify_pdf_path

from .chapter_page_mapper import ChapterPageMapper
from .models import PdfBook, PdfChapterMapping
from .pdf_repository import PdfRepository


SUBJECT_ALIASES = {
    "mathematics": "maths",
    "math": "maths",
    "maths": "maths",
    "science": "science",
    "social": "social_science",
    "social science": "social_science",
    "social_science": "social_science",
}


class PdfRegistrationService:
    def __init__(
        self,
        repository: PdfRepository,
        library_root: Path,
        curriculum_manifest_path: Path | None = None,
    ) -> None:
        self.repository = repository
        self.library_root = library_root.resolve()
        self.mapper = ChapterPageMapper()
        self.chapter_overrides = self._load_chapter_overrides(curriculum_manifest_path)

    def _load_chapter_overrides(self, manifest_path: Path | None) -> dict[str, dict[str, Any]]:
        if manifest_path is None or not manifest_path.exists():
            return {}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        overrides: dict[str, dict[str, Any]] = {}
        for entry in (manifest.get("curriculum_index") or {}).values():
            for chapter in entry.get("chapters") or []:
                relative_path = str(chapter.get("relative_path") or "").lstrip("./")
                chapter_id = str(chapter.get("chapter_id") or "")
                if relative_path and chapter_id:
                    overrides[relative_path] = {
                        "chapter_id": chapter_id,
                        "chapter_title": chapter.get("chapter_name") or chapter.get("chapter_title"),
                    }
        return overrides

    def register_pdf(
        self,
        pdf_path: Path,
        grade: int,
        subject: str,
        language: str = "english",
        book_title: str | None = None,
    ) -> tuple[PdfBook, bool]:
        pdf_path = pdf_path.resolve()
        if not self.validate_pdf(pdf_path):
            raise FileNotFoundError(f"PDF not found or invalid: {pdf_path}")
        subject = self._normalize_subject(subject)
        try:
            relative_path = pdf_path.relative_to(self.library_root).as_posix()
        except ValueError:
            relative_path = pdf_path.name
        mapping = self.mapper.mapping_for_file(
            pdf_path,
            grade=grade,
            subject=subject,
            language=language,
            relative_path=relative_path,
        )
        path_context = self._path_context(pdf_path)
        if path_context:
            mapping.chapter_id = f"{mapping.chapter_id}_{self.mapper.slug(path_context)}"
            mapping.aliases.extend([path_context, self.mapper.slug(path_context)])
            mapping.aliases = sorted(set(mapping.aliases))
        book_id = self.book_id(grade, subject, mapping.chapter_title, language, path_context=path_context)
        book = PdfBook(
            book_id=book_id,
            grade=grade,
            subject=subject,
            book_title=book_title or mapping.chapter_title,
            language=language,
            pdf_path=str(pdf_path),
            pdf_file=pdf_path.name,
            chapter_mappings=[mapping],
            metadata={"source": "ncert_pdf_registration", "chapter_level_pdf": True},
        )
        created = self.repository.upsert_book(book)
        return book, created

    def validate_pdf(self, pdf_path: Path) -> bool:
        if not pdf_path.exists() or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            return False
        try:
            return pdf_path.read_bytes()[:5] == b"%PDF-"
        except OSError:
            return False

    def scan_library(self) -> dict[str, Any]:
        return self.rebuild_catalog()

    def rebuild_catalog(self) -> dict[str, Any]:
        """Rebuild the PDF manifest from the textbook library as the source of truth."""
        books_by_id: dict[str, PdfBook] = {}
        discovered = []
        missing = []
        duplicate_chapter_keys = []
        for pdf_path in sorted(self.library_root.rglob("*.pdf")):
            entry = self._catalog_entry_for_path(pdf_path)
            if not entry:
                continue
            try:
                book = self._book_from_catalog_entry(entry)
                if book.book_id in books_by_id:
                    duplicate_chapter_keys.append(book.book_id)
                books_by_id[book.book_id] = book
                discovered.append(book.book_id)
            except FileNotFoundError:
                missing.append(str(pdf_path))
        self.repository.replace_books(list(books_by_id.values()))
        validation = self.repository.validate()
        catalog = self.catalog_payload()
        return {
            "pdfs_registered": len(discovered),
            "books_discovered": validation["books"],
            "chapters_mapped": validation["chapters"],
            "duplicate_registrations": len(duplicate_chapter_keys),
            "duplicate_catalog_entries": sorted(set(duplicate_chapter_keys)),
            "missing_pdfs": missing + validation["missing_pdfs"],
            "invalid_page_ranges": validation["invalid_page_ranges"],
            "duplicate_chapter_mappings": validation["duplicate_chapter_mappings"],
            "language_coverage": validation["languages"],
            "grades": catalog["grades"],
            "subjects": catalog["subjects"],
            "manifest_path": str(self.repository.manifest_path),
            "library_root": str(self.library_root),
        }

    def catalog_payload(self) -> dict[str, Any]:
        entries = []
        for book in self.repository.list_books():
            for mapping in book.chapter_mappings:
                entries.append(
                    {
                        "grade": book.grade,
                        "subject": book.subject,
                        "book": book.book_title,
                        "chapter": mapping.chapter_title,
                        "chapter_id": mapping.chapter_id,
                        "pdf_path": book.pdf_path,
                        "pdf_file": book.pdf_file,
                        "page_count": mapping.end_page,
                        "start_page": mapping.start_page,
                        "end_page": mapping.end_page,
                        "language": book.language,
                    }
                )
        entries = sorted(entries, key=lambda item: (item["grade"], item["subject"], item["chapter"], item["language"]))
        return {
            "catalog_version": "1.0.0",
            "source": "ncert_textbook_library",
            "total_entries": len(entries),
            "grades": sorted({entry["grade"] for entry in entries}),
            "subjects": sorted({entry["subject"] for entry in entries}),
            "entries": entries,
        }

    def _parse_pdf_path(self, pdf_path: Path) -> dict[str, Any] | None:
        entry = self._catalog_entry_for_path(pdf_path)
        if not entry:
            return None
        return {
            "pdf_path": Path(entry["pdf_path"]),
            "grade": entry["grade"],
            "subject": entry["subject"],
            "language": entry["language"],
            "book_title": entry["book"],
        }

    def _catalog_entry_for_path(self, pdf_path: Path) -> dict[str, Any] | None:
        try:
            relative = pdf_path.relative_to(self.library_root)
        except ValueError:
            relative = pdf_path
        parts = [part for part in relative.parts if part]
        if len(parts) < 2:
            return None
        relative_path = Path(*parts).as_posix()
        if classify_pdf_path(relative_path) != CHAPTER:
            return None
        subject = self._subject_from_parts(parts)
        if subject is None:
            return None
        grade = self._grade_from_parts(parts)
        if grade is None:
            return None
        chapter = self.mapper.chapter_title_from_file(pdf_path)
        override = self.chapter_overrides.get(relative_path, {})
        chapter = override.get("chapter_title") or chapter
        return {
            "pdf_path": str(pdf_path.resolve()),
            "grade": grade,
            "subject": subject,
            "language": self._language_from_path(pdf_path),
            "book": chapter,
            "chapter": chapter,
            "chapter_id": override.get("chapter_id"),
            "page_count": self.mapper.page_count(pdf_path),
            "relative_path": relative_path,
        }

    def _book_from_catalog_entry(self, entry: dict[str, Any]) -> PdfBook:
        pdf_path = Path(str(entry["pdf_path"]))
        if not self.validate_pdf(pdf_path):
            raise FileNotFoundError(f"PDF not found or invalid: {pdf_path}")
        grade = int(entry["grade"])
        subject = str(entry["subject"])
        language = str(entry["language"])
        chapter = str(entry["chapter"])
        path_context = self._path_context(pdf_path)
        legacy_chapter_id = self.mapper.chapter_id(grade, subject, chapter, language)
        if path_context:
            legacy_chapter_id = f"{legacy_chapter_id}_{self.mapper.slug(path_context)}"
        chapter_id = str(entry.get("chapter_id") or legacy_chapter_id)
        aliases = set(self.mapper.aliases_for(grade, subject, chapter, language))
        aliases.add(legacy_chapter_id)
        aliases.add(chapter_id)
        mapping = PdfChapterMapping(
            chapter_id=chapter_id,
            chapter_title=chapter,
            start_page=1,
            end_page=int(entry["page_count"]),
            aliases=sorted(alias for alias in aliases if alias),
            metadata={
                "source": "ncert_textbook_catalog",
                "chapter_level_pdf": True,
                "pdf_file": pdf_path.name,
                "relative_path": str(entry.get("relative_path") or (pdf_path.relative_to(self.library_root) if self.library_root in pdf_path.parents else pdf_path)),
                "path_context": path_context,
            },
        )
        if path_context:
            mapping.aliases.extend([path_context, self.mapper.slug(path_context)])
            mapping.aliases = sorted(set(mapping.aliases))
        book_id = self.book_id(grade, subject, chapter, language, path_context=path_context)
        return PdfBook(
            book_id=book_id,
            grade=grade,
            subject=subject,
            book_title=chapter,
            language=language,
            pdf_path=str(pdf_path),
            pdf_file=pdf_path.name,
            chapter_mappings=[mapping],
            metadata={"source": "ncert_textbook_catalog", "chapter_level_pdf": True},
        )

    def book_id(self, grade: int, subject: str, title: str, language: str = "english", path_context: str = "") -> str:
        context = f"_{self.mapper.slug(path_context)}" if path_context else ""
        return f"pdf_grade_{grade}_{self.mapper.slug(subject)}_{self.mapper.slug(title)}{context}_{self.mapper.slug(language)}"

    def _path_context(self, pdf_path: Path) -> str:
        try:
            relative_parent = pdf_path.parent.relative_to(self.library_root)
        except ValueError:
            relative_parent = pdf_path.parent
        parts = [part for part in relative_parent.parts if re.search(r"(?:^|[\s_-])part[\s_-]*\d+", part, re.I)]
        return " ".join(parts)

    @staticmethod
    def _grade_from_parts(parts: list[str]) -> int | None:
        for part in parts:
            match = re.search(r"\b(?:class|grade)[\s_-]*(\d{1,2})(?:st|nd|rd|th)?\b", part, re.I)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _subject_from_parts(cls, parts: list[str]) -> str | None:
        for part in parts[:-1]:
            normalized = cls._normalize_subject(part)
            if normalized in {"maths", "science", "social_science", "english", "hindi", "chemistry", "physics", "biology"}:
                return normalized
        return cls._normalize_subject(parts[0]) if parts else None

    @staticmethod
    def _normalize_subject(value: str) -> str:
        normalized = value.replace("_", " ").strip().lower()
        normalized = re.sub(r"(?:^|[\s_-])part[\s_-]*\d+\b", "", normalized).strip()
        if normalized in SUBJECT_ALIASES:
            return SUBJECT_ALIASES[normalized]

        tokens = (
            ("social science", "social_science"),
            ("social", "social_science"),
            ("mathematics", "maths"),
            ("maths", "maths"),
            ("math", "maths"),
            ("chemistry", "chemistry"),
            ("physics", "physics"),
            ("biology", "biology"),
            ("science", "science"),
            ("english", "english"),
            ("hindi", "hindi"),
        )
        for token, subject in tokens:
            if re.search(rf"(?:^|[\s_-]){re.escape(token)}(?:$|[\s_-])", normalized):
                return subject
        return normalized.replace(" ", "_")

    @staticmethod
    def _language_from_path(pdf_path: Path) -> str:
        lowered = str(pdf_path).lower()
        if "kannada" in lowered or "kan " in lowered or " kan " in lowered:
            return "kannada"
        if "hindi" in lowered:
            return "hindi"
        return "english"
