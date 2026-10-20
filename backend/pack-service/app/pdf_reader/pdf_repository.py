from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.text_normalization import normalize_curriculum_name

from .chapter_page_mapper import ChapterPageMapper
from .models import PdfBook, PdfChapterMapping, PdfReference


class PdfRepository:
    def __init__(self, manifest_path: Path, library_root: Path | None = None) -> None:
        self.manifest_path = manifest_path
        self.library_root = library_root
        self.mapper = ChapterPageMapper()
        self._books: list[PdfBook] = []
        self._chapter_index: dict[str, tuple[PdfBook, PdfChapterMapping]] = {}
        self.reload()

    def reload(self) -> None:
        self._books = []
        self._chapter_index = {}
        if not self.manifest_path.exists():
            return
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        raw_books = payload.get("books", payload if isinstance(payload, list) else [])
        self._books = [PdfBook(**item) for item in raw_books]
        for book in self._books:
            for mapping in book.chapter_mappings:
                keys = {
                    mapping.chapter_id,
                    self._key(mapping.chapter_id),
                    self._key(mapping.chapter_title),
                    *[self._key(alias) for alias in mapping.aliases],
                }
                for key in keys:
                    if key:
                        self._chapter_index.setdefault(key, (book, mapping))

    def save(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "manifest_version": "1.0.0",
            "books": [self._dump(book) for book in self._books],
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        self.reload()

    def list_books(self) -> list[PdfBook]:
        return list(self._books)

    def upsert_book(self, book: PdfBook) -> bool:
        duplicate = False
        books = []
        for existing in self._books:
            if existing.book_id == book.book_id:
                duplicate = True
                books.append(book)
            else:
                books.append(existing)
        if not duplicate:
            books.append(book)
        self._books = sorted(books, key=lambda item: (item.grade, item.subject, item.language, item.book_title))
        self.save()
        return not duplicate

    def replace_books(self, books: list[PdfBook]) -> None:
        self._books = sorted(books, key=lambda item: (item.grade, item.subject, item.language, item.book_title, item.book_id))
        self.save()

    def get_book(self, grade: int, subject: str, language: str = "english") -> PdfBook | None:
        books = self.get_books(grade, subject, language)
        return books[0] if books else None

    def get_books(self, grade: int, subject: str, language: str = "english") -> list[PdfBook]:
        subject_key = self._subject_key(subject)
        language_key = self._key(language)
        exact = [
            book
            for book in self._books
            if int(book.grade) == int(grade) and self._subject_key(book.subject) == subject_key and self._key(book.language) == language_key
        ]
        if exact:
            return exact
        return [book for book in self._books if int(book.grade) == int(grade) and self._subject_key(book.subject) == subject_key]

    def get_chapter_pdf(self, chapter_id: str) -> PdfReference | None:
        found = self._chapter_index.get(self._key(chapter_id))
        if not found:
            return None
        return self._reference(*found)

    def get_chapter_page_range(self, chapter_id: str) -> tuple[int, int] | None:
        reference = self.get_chapter_pdf(chapter_id)
        if reference is None:
            return None
        return reference.start_page, reference.end_page

    def resolve_pdf_reference(
        self,
        grade: int,
        subject: str,
        chapter: str,
        language: str = "english",
    ) -> PdfReference | None:
        keys = [
            self.mapper.chapter_id(grade, subject, chapter, language),
            chapter,
            normalize_curriculum_name(chapter),
            self.mapper.slug(chapter),
        ]
        for key in keys:
            reference = self.get_chapter_pdf(key)
            if reference and int(reference.grade) == int(grade) and self._subject_key(reference.subject) == self._subject_key(subject):
                if self._key(reference.language) == self._key(language) or not language:
                    return reference
        book = self.get_book(grade, subject, language)
        if not book:
            return None
        chapter_key = self._key(chapter)
        for mapping in book.chapter_mappings:
            if chapter_key in {self._key(mapping.chapter_title), self._key(mapping.chapter_id), *[self._key(alias) for alias in mapping.aliases]}:
                return self._reference(book, mapping)
        return None

    def validate(self) -> dict[str, Any]:
        missing = []
        invalid_ranges = []
        duplicates = []
        seen: set[str] = set()
        for book in self._books:
            pdf_path = Path(book.pdf_path)
            if not pdf_path.exists():
                missing.append(book.book_id)
            page_count = self.mapper.page_count(pdf_path) if pdf_path.exists() else None
            for mapping in book.chapter_mappings:
                valid, errors = self.mapper.validate_mapping(mapping, page_count)
                if not valid:
                    invalid_ranges.append({"book_id": book.book_id, "chapter_id": mapping.chapter_id, "errors": errors})
                key = f"{book.grade}:{self._key(book.subject)}:{self._key(book.language)}:{self._key(mapping.chapter_id)}"
                if key in seen:
                    duplicates.append(mapping.chapter_id)
                seen.add(key)
        return {
            "books": len(self._books),
            "chapters": sum(len(book.chapter_mappings) for book in self._books),
            "missing_pdfs": missing,
            "invalid_page_ranges": invalid_ranges,
            "duplicate_chapter_mappings": sorted(set(duplicates)),
            "languages": sorted({book.language for book in self._books}),
        }

    def _reference(self, book: PdfBook, mapping: PdfChapterMapping) -> PdfReference:
        return PdfReference(
            grade=book.grade,
            subject=book.subject,
            language=book.language,
            chapter_id=mapping.chapter_id,
            chapter_title=mapping.chapter_title,
            pdf_path=book.pdf_path,
            pdf_file=book.pdf_file,
            book_id=book.book_id,
            start_page=mapping.start_page,
            end_page=mapping.end_page,
            metadata={**book.metadata, **mapping.metadata},
        )

    @classmethod
    def _subject_key(cls, value: Any) -> str:
        key = cls._key(value)
        return {
            "mathematics": "maths",
            "math": "maths",
            "social": "social_science",
            "social science": "social_science",
        }.get(key, key)

    @staticmethod
    def _key(value: Any) -> str:
        return ChapterPageMapper.slug(normalize_curriculum_name(str(value or "")))

    @staticmethod
    def _dump(model: PdfBook) -> dict[str, Any]:
        if hasattr(model, "model_dump"):
            return model.model_dump(mode="json")
        return model.dict()
