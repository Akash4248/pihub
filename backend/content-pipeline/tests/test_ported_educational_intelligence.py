from __future__ import annotations

import unittest
from app.educational_intelligence.textbook_builder import TextbookBuilder
from app.educational_intelligence.section_builder import SectionBuilder
from app.educational_intelligence.structured_models import StructuredBlock, StructuredDocument
from app.educational_intelligence.concept_models import EducationalConcept, ConceptType
from app.educational_intelligence.concept_graph import ConceptGraphBuilder
from app.educational_intelligence.worked_example_builder import WorkedExampleBuilder
from app.educational_intelligence.tutor_context_builder import TutorContextBuilder


class PortedEducationalIntelligenceTests(unittest.TestCase):
    def test_textbook_builder_tables_and_formulas(self) -> None:
        rows = [
            {
                "chunk_id": "table_1",
                "text": "Quantity    Cost\n1 notebook    20\n2 notebooks    40\n4 notebooks    80",
                "metadata": {"content_type": "table", "grade": 8, "subject": "maths", "chapter": "proportion", "topic": "direct proportion"},
            },
            {
                "chunk_id": "formula_1",
                "text": "Formula: y = kx. This means y changes in the same ratio as x.",
                "metadata": {
                    "content_type": "formula_explanation",
                    "grade": 8,
                    "subject": "maths",
                    "chapter": "proportion",
                    "topic": "direct proportion",
                    "formula_intelligence": [
                        {
                            "formula": "y = kx",
                            "formula_type": "equation",
                            "meaning": "y changes in the same ratio as x.",
                            "variables": {"x": "input quantity", "y": "output quantity", "k": "constant"},
                            "units": {},
                        }
                    ],
                },
            },
        ]

        textbook, report = TextbookBuilder().build(
            rows,
            pack_id="grade8_maths_proportion",
            metadata={"grade": 8, "subject": "maths", "chapter": "proportion", "language": "english"},
            concepts=[],
        )

        blocks = [block for section in textbook["sections"] for block in section["blocks"]]
        table_blocks = [block for block in blocks if block["type"] == "table"]
        formula_blocks = [block for block in blocks if block["type"] == "formula"]

        self.assertEqual(report["table_blocks"], 1)
        self.assertEqual(report["formula_blocks"], 1)
        self.assertEqual(table_blocks[0]["rows"][0], ["Quantity", "Cost"])
        self.assertEqual(formula_blocks[0]["formula"], "y = kx")
        self.assertEqual(formula_blocks[0]["variables"][0]["symbol"], "k")

    def test_section_builder_bounds_blocks_into_sections(self) -> None:
        paragraphs = ["Word " * 150 for _ in range(6)]
        blocks = [
            StructuredBlock(block_id="b0", type="heading", text="Introduction to Physics"),
            StructuredBlock(block_id="b1", type="paragraph", text=paragraphs[0]),
            StructuredBlock(block_id="b2", type="paragraph", text=paragraphs[1]),
            StructuredBlock(block_id="b3", type="paragraph", text=paragraphs[2]),
            StructuredBlock(block_id="b4", type="heading", text="Kinematics and Motion"),
            StructuredBlock(block_id="b5", type="paragraph", text=paragraphs[3]),
            StructuredBlock(block_id="b6", type="paragraph", text=paragraphs[4]),
            StructuredBlock(block_id="b7", type="paragraph", text=paragraphs[5]),
        ]
        doc = StructuredDocument(
            source_path="test_doc",
            title="Physics Grade 9",
            blocks=blocks,
            metadata={"grade": 9, "subject": "Physics"},
        )
        builder = SectionBuilder(min_words=200, max_words=600)
        sections = builder.build(doc)
        self.assertGreaterEqual(len(sections), 2)
        for sec in sections:
            self.assertGreaterEqual(sec.word_count, 150)
            self.assertLessEqual(sec.word_count, 700)

    def test_concept_graph_builder(self) -> None:
        c1 = EducationalConcept(
            concept_id="c_energy",
            name="Energy",
            definition="The ability to do work.",
            prerequisites=[],
            related_concepts=["Work", "Power"],
            learning_objectives=["Understand energy conservation."],
            source_chunk_ids=["chunk_1"],
            concept_type=ConceptType.CONCEPT,
        )
        c2 = EducationalConcept(
            concept_id="c_work",
            name="Work",
            definition="Work is force times displacement.",
            prerequisites=["Energy"],
            related_concepts=["Energy"],
            learning_objectives=["Calculate work done."],
            source_chunk_ids=["chunk_1"],
            concept_type=ConceptType.CONCEPT,
        )
        graph = ConceptGraphBuilder().build([c1, c2])
        self.assertEqual(graph["concept_count"], 2)
        self.assertGreaterEqual(graph["edge_count"], 1)

    def test_worked_example_builder(self) -> None:
        rows = [
            {
                "chunk_id": "we_1",
                "text": (
                    "Example 1. A moving motor car travels a distance of one hundred meters in five seconds along a straight level road. "
                    "Find the speed and velocity of the car during this uniform motion.\n"
                    "Solution: We are given distance d = 100 meters and time taken t = 5 seconds. "
                    "Step 1: Write down the formula for speed: Speed = Distance / Time.\n"
                    "Step 2: Substitute the numerical values: Speed = 100 / 5 = 20 m/s.\n"
                    "Therefore, the final answer for the speed of the motor car is 20 meters per second."
                ),
                "metadata": {"grade": 9, "subject": "Physics", "chapter": "Motion"},
            }
        ]
        enhanced_rows, report = WorkedExampleBuilder().build(rows)
        self.assertGreaterEqual(report.get("worked_examples_created", 0), 1)


if __name__ == "__main__":
    unittest.main()
