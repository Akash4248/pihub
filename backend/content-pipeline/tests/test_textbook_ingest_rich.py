from __future__ import annotations

import unittest
from pathlib import Path

from app.content_pipeline.chunk_metadata_builder import ChunkMetadataBuilder
from app.content_pipeline.educational_classifier import EducationalClassifier
from app.content_pipeline.educational_chunker import EducationalChunkerV2
from app.content_pipeline.formula_preserver import FormulaPreserver
from app.content_pipeline.section_parser import SectionParser
from app.educational_intelligence.formula_converter import FormulaConverter


class RichExtractionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section_parser = SectionParser()
        self.classifier = EducationalClassifier()
        self.formula_preserver = FormulaPreserver()
        self.metadata_builder = ChunkMetadataBuilder()
        self.formula_converter = FormulaConverter()
        self.chunker = EducationalChunkerV2()

    def test_markdown_heading_hierarchy_prevents_math_as_heading(self) -> None:
        markdown_text = (
            "## 4.1 Rectangles and Squares\n\n"
            "We know what rectangles are.\n\n"
            "AD = CB\n"
            "∠BAD = ∠CDA = 90°\n\n"
            "Therefore, diagonals are equal.\n\n"
            "## 4.2 Rhombus\n\n"
            "A rhombus has all sides equal."
        )
        sections = self.section_parser.parse(markdown_text)
        titles = [s.title for s in sections]
        self.assertIn("4.1 Rectangles and Squares", titles)
        self.assertIn("4.2 Rhombus", titles)
        # Math equations must never be section headings
        self.assertNotIn("AD = CB", titles)
        self.assertNotIn("∠BAD = ∠CDA = 90°", titles)

    def test_educational_classifier_ignores_plain_hyphenated_words(self) -> None:
        prose_text = "Students should follow a step-by-step well-known procedure to observe the phenomenon."
        self.assertNotEqual(self.classifier.classify(prose_text), "formula")

    def test_educational_classifier_detects_real_equations(self) -> None:
        formula_text = "In ∆ABC, the relation is: c^2 = a^2 + b^2\nE = mc^2"
        self.assertEqual(self.classifier.classify(formula_text), "formula")

    def test_educational_classifier_detects_ncert_definitions(self) -> None:
        def_text = "The process of taking in food and its utilisation by the body is called nutrition."
        self.assertEqual(self.classifier.classify(def_text), "definition")

    def test_formula_converter_extracts_markdown_latex(self) -> None:
        text_with_latex = "The energy is given by $$E = mc^2$$ and force by $F = ma$."
        formulas = self.formula_converter.convert_text_formulas(text_with_latex)
        latex_strings = [f.latex for f in formulas]
        self.assertIn("E = mc^2", latex_strings)
        self.assertIn("F = ma", latex_strings)

    def test_metadata_builder_yake_keywords(self) -> None:
        sample_text = (
            "Photosynthesis is the process by which autotrophic plants synthesize glucose "
            "using chlorophyll pigments, carbon dioxide, water, and sunlight."
        )
        base = {"grade": 7, "subject": "Science", "chapter": "Nutrition in Plants"}
        meta = self.metadata_builder.build(sample_text, base, "Photosynthesis", "definition")
        keywords = meta["keywords"]
        self.assertTrue(any("photosynthesis" in kw or "chlorophyll" in kw or "glucose" in kw for kw in keywords))
        # Ensure filler words are not top keywords
        for filler in ["what", "let", "other", "using", "which"]:
            self.assertNotIn(filler, keywords[:3])


if __name__ == "__main__":
    unittest.main()
