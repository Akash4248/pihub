from __future__ import annotations

import unittest
from pathlib import Path

from shared.chapter_naming import (
    CHAPTER,
    FRONT_MATTER,
    FULL_TEXTBOOK,
    REFERENCE,
    canonical_chapter_title,
    chapter_number_for_pdf,
    chapter_title_for_pdf,
    classify_pdf_path,
    extract_chapter_number,
)


class ChapterNamingTests(unittest.TestCase):
    def test_strips_chapter_prefix(self) -> None:
        self.assertEqual(canonical_chapter_title("Chapter 3 Poverty as a Challenge"), "Poverty as a Challenge")
        self.assertEqual(canonical_chapter_title("Unit 2 Motion in a Plane"), "Motion in a Plane")

    def test_filename_title_case_and_possessives(self) -> None:
        self.assertEqual(canonical_chapter_title("the_other_side_of_zero.pdf"), "The Other Side of Zero")
        self.assertEqual(
            canonical_chapter_title("introduction_to_euclid_s_geometry.pdf"),
            "Introduction to Euclid's Geometry",
        )
        self.assertEqual(canonical_chapter_title("we_the_travellers_ii.pdf"), "We the Travellers II")
        self.assertEqual(canonical_chapter_title("indias_cultural_roots.pdf"), "India's Cultural Roots")
        self.assertEqual(
            canonical_chapter_title("fun_with_numbers_numbers_21_to_99.pdf"),
            "Fun with Numbers Numbers 21 to 99",
        )
        self.assertEqual(canonical_chapter_title("proportional_reasoning_1.pdf"), "Proportional Reasoning 1")
        self.assertEqual(canonical_chapter_title("Proportional Reasoning-1"), "Proportional Reasoning-1")
        self.assertEqual(canonical_chapter_title("Proportional Reasoning–2"), "Proportional Reasoning-2")

    def test_extracts_chapter_number(self) -> None:
        self.assertEqual(extract_chapter_number("Chapter 12 Sound"), 12)
        self.assertIsNone(extract_chapter_number("Real Numbers"))

    def test_class_11_code_override(self) -> None:
        path = "class_11/biology/kebo101.pdf"
        self.assertEqual(chapter_title_for_pdf(path), "The Living World")
        self.assertEqual(chapter_number_for_pdf(path), 1)
        self.assertEqual(classify_pdf_path(path), CHAPTER)

    def test_non_chapter_classification(self) -> None:
        self.assertEqual(classify_pdf_path("mathematics/class 9/introduction.pdf"), FRONT_MATTER)
        self.assertEqual(classify_pdf_path("social/class 6th social science/glossary.pdf"), REFERENCE)
        self.assertEqual(
            classify_pdf_path("science/class 6/class_6_science_textbook.pdf"),
            FULL_TEXTBOOK,
        )
        self.assertEqual(
            classify_pdf_path("mathematics/class 9/introduction_to_euclid_s_geometry.pdf"),
            CHAPTER,
        )
        self.assertEqual(
            classify_pdf_path("social/class 10 th social science part 4/democratic_politics_ii.pdf"),
            FULL_TEXTBOOK,
        )
        self.assertEqual(
            classify_pdf_path("social/class 9th social science part 3/economics.pdf"),
            FULL_TEXTBOOK,
        )

    def test_scanned_relative_paths(self) -> None:
        root = Path("/home/akash/Desktop/NOTIMP/TEXTBOOKS")
        if not root.exists():
            self.skipTest("TEXTBOOKS corpus is not available")
        prefixed = [
            path
            for path in root.rglob("*.pdf")
            if classify_pdf_path(path.relative_to(root).as_posix()) == CHAPTER
            and canonical_chapter_title(path.stem, relative_path=path.relative_to(root).as_posix()).lower().startswith(
                ("chapter ", "unit ", "lesson ")
            )
        ]
        self.assertEqual(prefixed, [])


if __name__ == "__main__":
    unittest.main()
