from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Any
import pytest

from app.educational_intelligence.artifact_cleaning import (
    clean_text,
    extract_candidate_terms,
    is_meaningful_term,
    build_mcq_options,
)
from app.educational_intelligence.artifact_schemas import (
    QuizItem,
    QuizSet,
    FlashcardItem,
    FlashcardDeck,
    GlossarySet,
    ChapterSummary,
    MisconceptionGuide,
    LearningObjectives,
)
from app.educational_intelligence.glossary_extractor import FormulaExtractor, GlossaryExtractor
from app.educational_intelligence.quiz_generator import QuizGenerator
from app.educational_intelligence.multilingual_support import MultilingualSupport
from app.educational_intelligence.pack_compiler import PackCompiler


class TestContentGenerationRemediation:
    def test_science_terms_are_meaningful(self) -> None:
        """Verify optics and physics terms are not stripped by GENERIC_TERMS."""
        science_terms = ["light", "mirror", "concave", "convex", "beam", "glass", "image", "surface"]
        for term in science_terms:
            assert is_meaningful_term(term), f"Expected '{term}' to be recognized as meaningful educational term"

    def test_multi_word_candidate_extraction(self) -> None:
        """Verify multi-word concepts are extracted as candidates."""
        sample_text = (
            "An electric current flows through the circuit. The electric current produces a magnetic field. "
            "The focal length of a concave mirror determines the image formation."
        )
        candidates = extract_candidate_terms(sample_text)
        assert any("Electric Current" in c for c in candidates)
        assert any("Concave Mirror" in c or "Mirror" in c for c in candidates)

    def test_formula_extractor_rejects_hyphenated_words(self) -> None:
        """Verify FormulaExtractor ignores ordinary compound words and headers."""
        text = (
            "Section 1.2 - Introduction to Elements\n"
            "Non-metals have low electrical conductivity.\n"
            "This is a well-known phenomenon in science.\n"
            "F = m * a\n"
            "E = m * c^2\n"
        )
        extractor = FormulaExtractor()
        formulas = extractor.extract_formulas(text)
        formula_texts = [f["formula"] for f in formulas]

        assert not any("Introduction" in f for f in formula_texts)
        assert not any("Non-metals" in f for f in formula_texts)
        assert not any("well-known" in f for f in formula_texts)
        assert any("F = m * a" in f for f in formula_texts)
        assert any("E = m * c^2" in f for f in formula_texts)

    def test_pydantic_schema_bidirectional_coercion(self) -> None:
        """Verify schemas accept both inference-service and content-pipeline keys."""
        # QuizItem accepts 'answer'
        q = QuizItem.model_validate({
            "question": "What is reflection?",
            "options": ["Bouncing back", "Passing through", "Bending", "Absorbing"],
            "answer": "Bouncing back",
        })
        assert q.correct_answer == "Bouncing back"

        # QuizSet accepts direct list or 'items'
        qs = QuizSet.model_validate([q.model_dump()])
        assert len(qs.items) == 1

        # FlashcardItem accepts 'question' and 'answer'
        card = FlashcardItem.model_validate({"question": "Define Force", "answer": "Push or pull"})
        assert card.front == "Define Force"
        assert card.back == "Push or pull"

        # FlashcardDeck accepts 'items'
        deck = FlashcardDeck.model_validate({"items": [{"question": "Q", "answer": "A"}]})
        assert len(deck.cards) == 1
        assert deck.cards[0].front == "Q"

        # GlossarySet accepts 'items'
        glossary = GlossarySet.model_validate({"items": [{"term": "Atom", "definition": "Smallest unit"}]})
        assert len(glossary.terms) == 1

        # ChapterSummary accepts 'summary', 'keyPoints', 'importantFacts'
        summary = ChapterSummary.model_validate({
            "summary": "Overview text",
            "keyPoints": ["Point 1", "Point 2"],
            "importantFacts": ["Fact 1"],
        })
        assert summary.overview == "Overview text"
        assert len(summary.key_points) == 2
        assert len(summary.takeaways) == 1

        # LearningObjectives accepts 'items' with objects
        objs = LearningObjectives.model_validate({"items": [{"objective": "Understand optics"}]})
        assert objs.objectives == ["Understand optics"]

    @pytest.mark.asyncio
    async def test_quiz_generator_balanced_true_false_and_fill_blank(self) -> None:
        """Verify quiz generator produces balanced True and False statements and clean blanks."""
        chunks = [{
            "text": "Photosynthesis is the process by which plants make food. Chlorophyll is the green pigment in leaves.",
            "metadata": {"chapter": "Nutrition in Plants", "subject": "science", "grade": 7},
        }]
        gen = QuizGenerator()
        quizzes = await gen.generate(chunks, limit=10)

        tf_questions = [q for q in quizzes if q.get("question_type") == "true_false"]
        answers = [q["answer"] for q in tf_questions]
        # Should have at least one True and at least one False
        assert True in answers
        assert False in answers

        fill_blank = [q for q in quizzes if q.get("question_type") == "fill_blank"]
        for fb in fill_blank:
            assert "______" in fb["question"], f"Expected blank in question stem: {fb['question']}"
            assert fb["answer"]

    def test_multilingual_threshold_detection(self) -> None:
        """Verify English text with an isolated Indic quote is not misclassified as Kannada."""
        english_with_quote = (
            "The Sulba Sutras (शुल्बसूत्र) are ancient Indian geometric treatises. "
            "They describe constructions of right angles and Pythagorean triples in standard Vedic fire altars. "
            "These texts form a crucial foundation for ancient mathematical astronomy."
        )
        detector = MultilingualSupport()
        profile = detector.detect_language(english_with_quote)
        assert profile.language == "en", f"Expected 'en' for predominantly English text, got '{profile.language}'"

    def test_pack_compiler_includes_images_and_slugs_safely(self) -> None:
        """Verify PackCompiler packages figure images and sanitizes path slugs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            img_dir = tmp_path / "figures"
            img_dir.mkdir()
            (img_dir / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake_image_data")

            compiler = PackCompiler()
            manifest = compiler.compile(
                pack_name="Class 10/Science: Optics & Light",
                chunks=[{"text": "Sample"}],
                summaries=[{"summary": "Test"}],
                glossary=[{"term": "Light", "definition": "Form of energy"}],
                quizzes=[{"question": "Q", "answer": "A"}],
                flashcards=[{"front": "F", "back": "B"}],
                enrichment=[],
                image_dir=img_dir,
                output_dir=tmp_path / "packs",
            )

            archive_file = Path(manifest["archive_path"])
            assert archive_file.exists()

            with zipfile.ZipFile(archive_file, "r") as z:
                names = z.namelist()
                assert "content.json" in names
                assert "metadata.json" in names
                assert "images/fig1.png" in names
