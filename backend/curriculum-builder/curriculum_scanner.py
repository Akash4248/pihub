"""Master curriculum scanner for extracting curriculum structure from TEXTBOOKS."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

from folder_parser import FolderParser
from language_detector import LanguageDetector
from subject_mapper import SubjectMapper
from shared.chapter_naming import CHAPTER

logger = logging.getLogger(__name__)


class CurriculumScanner:
    """Scan TEXTBOOKS directory and extract curriculum structure."""

    def __init__(self, textbooks_root: Path):
        """
        Initialize scanner.

        Args:
            textbooks_root: Path to TEXTBOOKS directory
        """
        self.textbooks_root = Path(textbooks_root)
        if not self.textbooks_root.exists():
            raise ValueError(f"Textbooks directory not found: {self.textbooks_root}")

        self.curriculum_data: Dict = {
            "metadata": {
                "scanned_at": None,
                "textbooks_root": str(self.textbooks_root),
                "total_pdfs": 0,
                "grades": [],
                "subjects": [],
                "languages": [],
            },
            "curriculum": {},
        }

    def scan(self) -> Dict:
        """
        Scan entire TEXTBOOKS directory.

        Returns:
            Dictionary with complete curriculum structure
        """
        logger.info(f"Starting curriculum scan: {self.textbooks_root}")

        pdf_files = sorted(self.textbooks_root.rglob("*.pdf"))
        logger.info(f"Found {len(pdf_files)} PDF files")

        for pdf_path in pdf_files:
            if FolderParser.is_textbook_file(pdf_path.name):
                self._process_pdf(pdf_path)

        self._finalize_metadata()
        logger.info(f"Curriculum scan complete. Found {len(self.curriculum_data['curriculum'])} curriculum entries")

        return self.curriculum_data

    def _process_pdf(self, pdf_path: Path) -> None:
        """
        Process a single PDF file.

        Args:
            pdf_path: Path to PDF file
        """
        try:
            parsed = FolderParser.parse_curriculum_path(pdf_path, self.textbooks_root)
            if parsed.get("kind", CHAPTER) != CHAPTER:
                logger.info("Skipping non-chapter PDF (%s): %s", parsed.get("kind"), parsed["relative_path"])
                return

            # Create curriculum key
            curriculum_key = self._create_curriculum_key(parsed)

            # Add or update curriculum entry
            if curriculum_key not in self.curriculum_data["curriculum"]:
                self.curriculum_data["curriculum"][curriculum_key] = {
                    "grade": parsed["grade"],
                    "subject": parsed["subject"],
                    "language": parsed["language"],
                    "part": parsed.get("part"),
                    "chapters": [],
                }

            # Add chapter
            chapter_entry = {
                "chapter": parsed["chapter"],
                "chapter_number": parsed.get("chapter_number"),
                "kind": parsed.get("kind", CHAPTER),
                "filename": parsed["filename"],
                "relative_path": parsed["relative_path"],
                "part": parsed["part"],
            }

            self.curriculum_data["curriculum"][curriculum_key]["chapters"].append(chapter_entry)

            # Track unique values
            self._track_metadata(parsed)

        except Exception as e:
            logger.error(f"Error processing {pdf_path}: {e}")

    @staticmethod
    def _create_curriculum_key(parsed: dict) -> str:
        """
        Create a unique key for curriculum entry.

        Args:
            parsed: Parsed curriculum data

        Returns:
            Unique curriculum key
        """
        grade = parsed["grade"] or "unknown"
        subject = parsed["subject"] or "unknown"
        language = parsed["language"] or "english"
        part = f"_part{parsed['part']}" if parsed["part"] else ""

        return f"grade_{grade}_{subject}_{language}{part}".lower()

    def _track_metadata(self, parsed: dict) -> None:
        """Track unique metadata values."""
        if parsed["grade"] and parsed["grade"] not in self.curriculum_data["metadata"]["grades"]:
            self.curriculum_data["metadata"]["grades"].append(parsed["grade"])

        if parsed["subject"] and parsed["subject"] not in self.curriculum_data["metadata"]["subjects"]:
            self.curriculum_data["metadata"]["subjects"].append(parsed["subject"])

        if parsed["language"] not in self.curriculum_data["metadata"]["languages"]:
            self.curriculum_data["metadata"]["languages"].append(parsed["language"])

    def _finalize_metadata(self) -> None:
        """Finalize metadata after scan."""
        from datetime import datetime

        self.curriculum_data["metadata"]["scanned_at"] = datetime.utcnow().isoformat()
        self.curriculum_data["metadata"]["total_pdfs"] = sum(
            len(entry["chapters"]) for entry in self.curriculum_data["curriculum"].values()
        )

        # Sort grades and subjects
        self.curriculum_data["metadata"]["grades"].sort()
        self.curriculum_data["metadata"]["subjects"].sort()
        self.curriculum_data["metadata"]["languages"].sort()

    def get_grades(self) -> List[int]:
        """Get all grades in curriculum."""
        return self.curriculum_data["metadata"]["grades"]

    def get_subjects(self) -> List[str]:
        """Get all subjects in curriculum."""
        return self.curriculum_data["metadata"]["subjects"]

    def get_languages(self) -> List[str]:
        """Get all languages in curriculum."""
        return self.curriculum_data["metadata"]["languages"]

    def get_chapters_for_grade_subject(self, grade: int, subject: str) -> List[dict]:
        """
        Get chapters for a specific grade and subject.

        Args:
            grade: Grade number
            subject: Subject code

        Returns:
            List of chapter entries
        """
        key = f"grade_{grade}_{subject}_english".lower()
        if key in self.curriculum_data["curriculum"]:
            return self.curriculum_data["curriculum"][key]["chapters"]

        return []

    def save_scan_result(self, output_path: Path) -> None:
        """
        Save scan results to JSON.

        Args:
            output_path: Path to save JSON file
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.curriculum_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Curriculum scan results saved to {output_path}")

    def print_summary(self) -> None:
        """Print curriculum scan summary."""
        meta = self.curriculum_data["metadata"]
        print("\n" + "=" * 60)
        print("CURRICULUM SCAN SUMMARY")
        print("=" * 60)
        print(f"Total PDFs: {meta['total_pdfs']}")
        print(f"Grades: {meta['grades']}")
        print(f"Subjects: {meta['subjects']}")
        print(f"Languages: {meta['languages']}")
        print(f"Total Curriculum Entries: {len(self.curriculum_data['curriculum'])}")
        print("\nCURRICULUM STRUCTURE:")
        print("-" * 60)

        for key, entry in sorted(self.curriculum_data["curriculum"].items()):
            print(f"\n{key.upper()}")
            print(f"  Grade: {entry['grade']}")
            print(f"  Subject: {entry['subject']}")
            print(f"  Language: {entry['language']}")
            print(f"  Chapters: {len(entry['chapters'])}")
            for chapter in entry["chapters"]:
                print(f"    - {chapter['chapter']}")

        print("\n" + "=" * 60)
