"""Parse folder hierarchy to extract curriculum metadata."""

import re
from pathlib import Path
from typing import Optional

from language_detector import LanguageDetector
from subject_mapper import SubjectMapper
from shared.chapter_naming import (
    canonical_chapter_title,
    chapter_number_for_pdf,
    chapter_title_for_pdf,
    classify_pdf_path,
)


class FolderParser:
    """Parse TEXTBOOKS folder hierarchy to extract curriculum structure."""

    @staticmethod
    def extract_grade_from_folder_name(folder_name: str) -> Optional[int]:
        """
        Extract grade from folder name.

        Examples:
            "class 1" -> 1
            "class 8 part 1" -> 8
            "class 10" -> 10

        Args:
            folder_name: Folder name

        Returns:
            Grade number or None
        """
        # Match "class N" or "class N part M"
        match = re.search(r"class[\s_-]+(\d+)", folder_name, re.IGNORECASE)
        if match:
            return int(match.group(1))

        # Try standalone digits
        match = re.search(r"^(\d+)(?:\s|$)", folder_name)
        if match:
            return int(match.group(1))

        return None

    @staticmethod
    def extract_part_from_folder_name(folder_name: str) -> Optional[int]:
        """
        Extract part number from folder name.

        Examples:
            "class 7 part 1" -> 1
            "class 8 part 2" -> 2

        Args:
            folder_name: Folder name

        Returns:
            Part number or None
        """
        match = re.search(r"part[\s_-]*(\d+)", folder_name, re.IGNORECASE)
        if match:
            return int(match.group(1))

        return None

    @classmethod
    def extract_chapter_from_filename(cls, filename: str) -> str:
        """
        Extract chapter name from PDF filename.

        Examples:
            "Proportional Reasoning – 1.pdf" -> "Proportional Reasoning"
            "A Square and A Cube.pdf" -> "A Square and A Cube"
            "So Many Toys (Data Handling).pdf" -> "So Many Toys"

        Args:
            filename: PDF filename

        Returns:
            Chapter name
        """
        return canonical_chapter_title(filename)

    @classmethod
    def is_textbook_file(cls, filename: str) -> bool:
        """
        Check if filename is a textbook PDF.

        Args:
            filename: Filename to check

        Returns:
            True if it's likely a PDF textbook file
        """
        if not filename.lower().endswith(".pdf"):
            return False

        # Exclude temporary/upload files
        if filename.startswith("media_to_upload"):
            return False

        return True

    @classmethod
    def parse_curriculum_path(cls, file_path: Path, textbooks_root: Path) -> dict:
        """
        Parse a complete curriculum path.

        Args:
            file_path: Path to PDF file
            textbooks_root: Root TEXTBOOKS directory

        Returns:
            Dictionary with parsed curriculum metadata
        """
        relative_path = file_path.relative_to(textbooks_root)
        parts = relative_path.parts

        # Detect language from path
        language = LanguageDetector.detect_from_path(file_path)

        # Support both layouts:
        #   mathematics/class 9/chapter.pdf
        #   class_11/biology/chapter.pdf
        subject = None
        grade = None
        part = None
        for folder_name in parts[:-1]:
            if grade is None:
                grade = cls.extract_grade_from_folder_name(folder_name)
            if subject is None:
                subject = SubjectMapper.extract_subject_from_directory(Path(folder_name))
            if part is None:
                part = cls.extract_part_from_folder_name(folder_name)

        relative_key = relative_path.as_posix()

        return {
            "language": language,
            "subject": subject,
            "grade": grade,
            "part": part,
            "chapter": chapter_title_for_pdf(relative_key, file_path.name),
            "chapter_number": chapter_number_for_pdf(relative_key, file_path.name),
            "kind": classify_pdf_path(relative_key),
            "filename": file_path.name,
            "relative_path": str(relative_path),
            "full_path": str(file_path),
        }
