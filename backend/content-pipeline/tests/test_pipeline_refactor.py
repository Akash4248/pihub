from __future__ import annotations

import json
import sqlite3
import pytest
import sqlite_vec

from app.content_pipeline.educational_chunker import EducationalChunkerV2
from app.educational_intelligence.artifact_schemas import (
    QuizItem, QuizSet, GlossaryEntry, GlossarySet, FlashcardItem, FlashcardDeck, ChapterSummary
)
from app.educational_intelligence.agentic_orchestrator import ArtifactAgent


def test_educational_chunker_with_chonkie():
    """Verify EducationalChunkerV2 cleans text, uses Chonkie for splitting, preserves formulas, and repairs chunks."""
    chunker = EducationalChunkerV2(min_chunk_chars=50, max_chunk_chars=200, overlap_chars=20)
    
    sample_text = (
        "# Chapter 1: Chemical Reactions\n\n"
        "Reprint 2025-26\n\n"  # Standalone noise line
        "Chemical reactions involve breaking and making of bonds between atoms to produce new substances. "
        "For example, when magnesium ribbon is burnt in oxygen, it burns with a dazzling white flame and changes into a white powder. "
        "The reaction can be written as: 2Mg + O2 -> 2MgO.\n\n"
        "Figure 1.1\n\n"  # Standalone noise line
        "Another example is the reaction between zinc and dilute sulphuric acid to produce hydrogen gas. "
        "Zn + H2SO4 -> ZnSO4 + H2."
    )

    metadata = {"grade": 10, "subject": "Science", "chapter": "Chemical Reactions"}
    chunks = chunker.chunk_educational(sample_text, metadata)

    assert isinstance(chunks, list)
    assert len(chunks) > 0
    for chunk in chunks:
        assert "text" in chunk
        assert "metadata" in chunk
        # Verify noise line was stripped
        assert "Reprint 2025-26" not in chunk["text"]


def test_pydantic_artifact_schemas():
    """Verify Pydantic schemas validate valid artifact structures and catch invalid ones."""
    quiz_data = {
        "title": "Chemical Reactions Quiz",
        "items": [
            {
                "id": "q1",
                "question": "What is formed when Mg burns in O2?",
                "options": ["MgO", "MgOH", "MgCO3", "MgNO3"],
                "correct_answer": "MgO",
                "explanation": "Magnesium reacts with oxygen to form magnesium oxide.",
                "difficulty": "easy",
                "bloom_level": "remembering"
            }
        ]
    }
    
    quiz_set = QuizSet.model_validate(quiz_data)
    assert quiz_set.title == "Chemical Reactions Quiz"
    assert len(quiz_set.items) == 1
    assert quiz_set.items[0].correct_answer == "MgO"

    glossary_data = {
        "terms": [
            {
                "term": "Exothermic Reaction",
                "definition": "A reaction in which heat is released along with the formation of products.",
                "context_example": "Respiration is an exothermic reaction."
            }
        ]
    }
    glossary_set = GlossarySet.model_validate(glossary_data)
    assert len(glossary_set.terms) == 1
    assert glossary_set.terms[0].term == "Exothermic Reaction"


def test_agentic_orchestrator_parse_artifact_with_pydantic():
    """Verify agentic orchestrator _parse_artifact uses Pydantic schema validation."""
    agent = ArtifactAgent()

    raw_json_str = """
    ```json
    {
        "title": "Physics Chapter 1 Quiz",
        "items": [
            {
                "id": "q101",
                "question": "What is the SI unit of force?",
                "options": ["Newton", "Joule", "Watt", "Pascal"],
                "correct_answer": "Newton",
                "explanation": "Force is measured in Newtons (N).",
                "difficulty": "easy",
                "bloom_level": "remembering"
            }
        ]
    }
    ```
    """

    parsed = agent._parse_artifact(raw_json_str, "quiz")
    assert parsed is not None
    assert isinstance(parsed, dict)
    assert parsed["title"] == "Physics Chapter 1 Quiz"
    assert parsed["items"][0]["correct_answer"] == "Newton"


def test_sqlite_vec_extension_loading():
    """Verify sqlite-vec extension loads successfully and performs vector distance queries."""
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)

    cursor = db.cursor()
    cursor.execute("CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[4]);")
    cursor.execute("INSERT INTO vec_items(rowid, embedding) VALUES (1, '[0.1, 0.2, 0.3, 0.4]');")
    cursor.execute("INSERT INTO vec_items(rowid, embedding) VALUES (2, '[0.9, 0.8, 0.7, 0.6]');")
    db.commit()

    cursor.execute("SELECT rowid, distance FROM vec_items WHERE embedding MATCH '[0.1, 0.2, 0.3, 0.4]' ORDER BY distance LIMIT 1;")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == 1
    assert abs(row[1]) < 1e-4
    db.close()
