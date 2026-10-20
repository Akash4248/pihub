from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.pdf_reader import PdfRegistrationService, PdfRepository


PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n%%EOF"


class PdfReaderTests(unittest.TestCase):
    def test_scan_library_generates_manifest_and_resolves_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_dir = root / "TEXTBOOKS" / "mathematics" / "class 8"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "Proportional Reasoning.pdf"
            pdf_path.write_bytes(PDF_BYTES)

            manifest_path = root / "pdf_manifest.json"
            repository = PdfRepository(manifest_path, library_root=root / "TEXTBOOKS")
            service = PdfRegistrationService(repository, root / "TEXTBOOKS")

            report = service.scan_library()
            self.assertEqual(report["pdfs_registered"], 1)
            self.assertEqual(report["books_discovered"], 1)
            self.assertEqual(report["chapters_mapped"], 1)
            self.assertTrue(manifest_path.exists())

            reference = repository.resolve_pdf_reference(8, "maths", "Proportional Reasoning", "english")
            self.assertIsNotNone(reference)
            assert reference is not None
            self.assertEqual(reference.start_page, 1)
            self.assertEqual(reference.end_page, 2)
            self.assertEqual(reference.subject, "maths")
            self.assertTrue(Path(reference.pdf_path).exists())
            self.assertEqual(repository.get_chapter_page_range(reference.chapter_id), (1, 2))
            self.assertEqual(len(repository.get_books(8, "maths", "english")), 1)
            catalog = service.catalog_payload()
            self.assertEqual(catalog["total_entries"], 1)
            self.assertEqual(catalog["entries"][0]["chapter"], "Proportional Reasoning")

    def test_rebuild_catalog_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "science" / "class 6" / "Chapter 1 Science.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(PDF_BYTES)

            repository = PdfRepository(root / "pdf_manifest.json", library_root=root)
            service = PdfRegistrationService(repository, root)
            first = service.scan_library()
            second = service.scan_library()

            self.assertEqual(first["books_discovered"], 1)
            self.assertEqual(second["books_discovered"], 1)
            self.assertEqual(second["duplicate_registrations"], 0)
            self.assertEqual(len(repository.list_books()), 1)

    def test_ordinal_grade_social_science_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "social" / "class 6th social science" / "economic_activities_around_us.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(PDF_BYTES)

            repository = PdfRepository(root / "pdf_manifest.json", library_root=root)
            service = PdfRegistrationService(repository, root)

            report = service.scan_library()
            self.assertEqual(report["pdfs_registered"], 1)
            self.assertEqual(report["grades"], [6])
            self.assertEqual(report["subjects"], ["social_science"])
            reference = repository.resolve_pdf_reference(6, "social science", "Economic Activities Around Us")
            self.assertIsNotNone(reference)
            assert reference is not None
            self.assertEqual(reference.subject, "social_science")

    def test_curriculum_manifest_preserves_canonical_chapter_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative_path = "social/class 6th social science/economic_activities_around_us.pdf"
            pdf_path = root / relative_path
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(PDF_BYTES)
            manifest_path = root / "curriculum_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "curriculum_index": {
                            "grade_6_social_science_english": {
                                "chapters": [
                                    {
                                        "chapter_id": "6_social_science_english_ch000",
                                        "chapter_name": "Economic Activities Around Us",
                                        "relative_path": relative_path,
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            repository = PdfRepository(root / "pdf_manifest.json", library_root=root)
            service = PdfRegistrationService(repository, root, curriculum_manifest_path=manifest_path)
            service.scan_library()

            reference = repository.get_chapter_pdf("6_social_science_english_ch000")
            self.assertIsNotNone(reference)
            assert reference is not None
            self.assertEqual(reference.chapter_title, "Economic Activities Around Us")
            self.assertEqual(repository.get_chapter_page_range("6_social_science_english_ch000"), (1, 2))
            self.assertIsNotNone(
                repository.resolve_pdf_reference(6, "social science", "Economic Activities Around Us")
            )


if __name__ == "__main__":
    unittest.main()
