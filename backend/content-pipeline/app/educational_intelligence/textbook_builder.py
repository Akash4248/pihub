from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from .concept_models import EducationalConcept
from .textbook_models import TextbookBlock, TextbookBlockType, TextbookChapter, TextbookSection


SECTION_FALLBACK = "Main Lesson"
TABLE_ROW_RE = re.compile(r"\s{2,}|\|")
DEFINITION_RE = re.compile(r"\b(is|are|means|refers to|defined as)\b", re.I)
IMPORTANT_RE = re.compile(r"\b(important|remember|note that|key point)\b", re.I)
REJECT_TYPES = {"metadata", "table_of_contents", "index_page"}


class TextbookBuilder:
    """Build reader-first textbook artifacts from semantic educational rows."""

    def build(
        self,
        rows: list[dict[str, Any]],
        pack_id: str,
        metadata: dict[str, Any],
        concepts: list[EducationalConcept],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        sections: dict[str, TextbookSection] = {}
        ordered_keys: list[str] = []
        block_counts: Counter[str] = Counter()

        for row in rows:
            row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if self._reject(row_metadata):
                continue
            section_title = self._section_title(row_metadata, metadata)
            section_key = self._stable_id(section_title)
            if section_key not in sections:
                sections[section_key] = TextbookSection(
                    section_id=f"section_{len(sections) + 1}_{section_key}",
                    title=section_title,
                    level=1,
                    blocks=[
                        TextbookBlock(
                            block_id=f"heading_{len(sections) + 1}_{section_key}",
                            type=TextbookBlockType.HEADING,
                            text=section_title,
                            title=section_title,
                            metadata={"source": "section_reconstruction"},
                        )
                    ],
                    metadata={
                        "grade": row_metadata.get("grade", metadata.get("grade")),
                        "subject": row_metadata.get("subject", metadata.get("subject")),
                        "chapter": row_metadata.get("chapter", metadata.get("chapter")),
                    },
                )
                ordered_keys.append(section_key)
                block_counts[TextbookBlockType.HEADING.value] += 1

            blocks = self._blocks_for_row(row, len(sections[section_key].blocks) + 1)
            sections[section_key].blocks.extend(blocks)
            for block in blocks:
                block_counts[block.type.value] += 1

        self._add_concept_definitions(sections, ordered_keys, concepts, metadata, block_counts)
        chapter = TextbookChapter(
            pack_id=pack_id,
            title=self._chapter_title(metadata),
            grade=self._as_int(metadata.get("grade")),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
            sections=[sections[key] for key in ordered_keys],
            metadata={
                "artifact_type": "structured_textbook",
                "version": "1.0.0",
                "source": "semantic_content_pipeline",
                "reader_optimized": True,
                "rag_artifact": False,
            },
        )
        payload = self._dump(chapter)
        report = {
            "sections_reconstructed": len(chapter.sections),
            "total_blocks": sum(block_counts.values()),
            "block_counts": dict(sorted(block_counts.items())),
            "formula_blocks": block_counts[TextbookBlockType.FORMULA.value],
            "definition_blocks": block_counts[TextbookBlockType.DEFINITION.value],
            "example_blocks": block_counts[TextbookBlockType.EXAMPLE.value],
            "worked_example_blocks": block_counts[TextbookBlockType.WORKED_EXAMPLE.value],
            "table_blocks": block_counts[TextbookBlockType.TABLE.value],
            "publication_ready": bool(chapter.sections and sum(block_counts.values()) > 0),
        }
        return payload, report

    def _blocks_for_row(self, row: dict[str, Any], sequence: int) -> list[TextbookBlock]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        text = self._clean_text(row.get("text"))
        if not text:
            return []
        blocks: list[TextbookBlock] = []
        formulas = metadata.get("formula_intelligence") if isinstance(metadata.get("formula_intelligence"), list) else []
        if formulas:
            for formula in formulas:
                if isinstance(formula, dict) and self._valid_formula_block(str(formula.get("formula") or "")):
                    blocks.append(self._formula_block(formula, metadata, sequence + len(blocks)))
            if str(metadata.get("content_type") or "") == "formula_explanation":
                return blocks

        table_rows = self._table_rows(text)
        if table_rows:
            blocks.append(
                TextbookBlock(
                    block_id=self._block_id("table", text, sequence + len(blocks)),
                    type=TextbookBlockType.TABLE,
                    rows=table_rows,
                    text=text,
                    metadata=self._block_metadata(metadata),
                )
            )
            return blocks

        block_type = self._block_type(metadata, text)
        blocks.append(
            TextbookBlock(
                block_id=self._block_id(block_type.value, text, sequence + len(blocks)),
                type=block_type,
                text=text,
                title=self._block_title(metadata, text, block_type),
                metadata=self._block_metadata(metadata),
            )
        )
        return blocks

    def _formula_block(self, formula: dict[str, Any], metadata: dict[str, Any], sequence: int) -> TextbookBlock:
        variables = [
            {"symbol": str(symbol), "meaning": str(meaning)}
            for symbol, meaning in sorted((formula.get("variables") or {}).items())
        ]
        text = " ".join(
            piece
            for piece in (
                str(formula.get("meaning") or ""),
                str(formula.get("explanation") or ""),
                str(formula.get("example") or ""),
            )
            if piece
        )
        return TextbookBlock(
            block_id=self._block_id("formula", str(formula.get("formula")), sequence),
            type=TextbookBlockType.FORMULA,
            text=text,
            formula=str(formula.get("formula") or ""),
            variables=variables,
            metadata={**self._block_metadata(metadata), "formula_type": formula.get("formula_type"), "units": formula.get("units", {})},
        )

    @staticmethod
    def _valid_formula_block(formula: str) -> bool:
        value = re.sub(r"\s+", " ", formula).strip()
        if not value or not any(symbol in value for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝")):
            return False
        lowered = value.lower()
        if lowered.startswith(("if ", "then ", "because ", "therefore ", "hence ", "for example ")):
            return False
        left = re.split(r"=|<|>|≤|≥|≈|∝", value, maxsplit=1)[0]
        if len(left.split()) > 4:
            return False
        return True

    def _add_concept_definitions(
        self,
        sections: dict[str, TextbookSection],
        ordered_keys: list[str],
        concepts: list[EducationalConcept],
        metadata: dict[str, Any],
        block_counts: Counter[str],
    ) -> None:
        if not concepts:
            return
        section_key = "key_concepts"
        if section_key not in sections:
            sections[section_key] = TextbookSection(
                section_id="section_key_concepts",
                title="Key Concepts",
                level=1,
                blocks=[
                    TextbookBlock(
                        block_id="heading_key_concepts",
                        type=TextbookBlockType.HEADING,
                        text="Key Concepts",
                        title="Key Concepts",
                        metadata={"source": "concept_extractor"},
                    )
                ],
                metadata={"grade": metadata.get("grade"), "subject": metadata.get("subject"), "chapter": metadata.get("chapter")},
            )
            ordered_keys.append(section_key)
            block_counts[TextbookBlockType.HEADING.value] += 1
        existing_text = "\n".join(block.text.lower() for section in sections.values() for block in section.blocks)
        for concept in concepts[:40]:
            if not concept.definition or concept.name.lower() in existing_text:
                continue
            block = TextbookBlock(
                block_id=f"definition_{concept.concept_id}",
                type=TextbookBlockType.DEFINITION,
                title=concept.name,
                text=concept.definition,
                metadata={
                    "concept_id": concept.concept_id,
                    "source": concept.source_type,
                    "key_terms": [concept.name, *concept.related_concepts[:5]],
                },
            )
            sections[section_key].blocks.append(block)
            block_counts[TextbookBlockType.DEFINITION.value] += 1

    @staticmethod
    def _reject(metadata: dict[str, Any]) -> bool:
        return str(metadata.get("content_type") or "") in REJECT_TYPES

    @staticmethod
    def _section_title(row_metadata: dict[str, Any], pack_metadata: dict[str, Any]) -> str:
        for key in ("section", "topic", "concept_name"):
            value = row_metadata.get(key)
            if value:
                return str(value).strip().title()
        return str(pack_metadata.get("chapter") or pack_metadata.get("subject") or SECTION_FALLBACK).strip().title()

    @staticmethod
    def _chapter_title(metadata: dict[str, Any]) -> str:
        chapter = metadata.get("chapter")
        subject = metadata.get("subject")
        grade = metadata.get("grade")
        pieces = [f"Grade {grade}" if grade else "", str(subject or "").replace("_", " ").title(), str(chapter or "").title()]
        return " - ".join(piece for piece in pieces if piece)

    @staticmethod
    def _block_type(metadata: dict[str, Any], text: str) -> TextbookBlockType:
        content_type = str(metadata.get("content_type") or "")
        if content_type == "worked_example" or metadata.get("worked_example"):
            return TextbookBlockType.WORKED_EXAMPLE
        if content_type == "example":
            return TextbookBlockType.EXAMPLE
        if content_type == "activity":
            return TextbookBlockType.ACTIVITY
        if content_type in {"exercise", "assessment"}:
            return TextbookBlockType.EXERCISE
        if content_type == "summary":
            return TextbookBlockType.SUMMARY
        if content_type == "table":
            return TextbookBlockType.TABLE
        if content_type == "glossary":
            return TextbookBlockType.DEFINITION
        if content_type in {"formula_explanation", "formula"}:
            return TextbookBlockType.FORMULA
        if metadata.get("definition") or DEFINITION_RE.search(text[:220]):
            return TextbookBlockType.DEFINITION
        if IMPORTANT_RE.search(text[:180]):
            return TextbookBlockType.IMPORTANT
        return TextbookBlockType.PARAGRAPH

    @staticmethod
    def _block_title(metadata: dict[str, Any], text: str, block_type: TextbookBlockType) -> str:
        if block_type == TextbookBlockType.DEFINITION:
            terms = metadata.get("key_terms") if isinstance(metadata.get("key_terms"), list) else []
            if terms:
                return str(terms[0]).title()
        if block_type == TextbookBlockType.WORKED_EXAMPLE:
            return str(metadata.get("problem_type") or "Worked Example").replace("_", " ").title()
        if block_type == TextbookBlockType.EXAMPLE:
            return "Example"
        if block_type == TextbookBlockType.ACTIVITY:
            return "Activity"
        if block_type == TextbookBlockType.EXERCISE:
            return "Exercise"
        return ""

    @staticmethod
    def _block_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "content_type",
            "grade",
            "subject",
            "chapter",
            "topic",
            "learning_objective",
            "difficulty",
            "key_terms",
            "source_chunk_ids",
            "recovery_score",
            "concepts_used",
            "steps_count",
        )
        return {key: metadata[key] for key in keys if key in metadata}

    @staticmethod
    def _table_rows(text: str) -> list[list[str]]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if len(lines) < 2:
            return []
        rows = []
        table_like = 0
        for line in lines[:20]:
            cells = [cell.strip(" |") for cell in TABLE_ROW_RE.split(line) if cell.strip(" |")]
            if len(cells) >= 2:
                table_like += 1
                rows.append(cells[:8])
        return rows if table_like >= 2 else []

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\n{3,}", "\n\n", str(value or "").strip())

    @staticmethod
    def _stable_id(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
        return slug[:48] or hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]

    def _block_id(self, prefix: str, text: str, sequence: int) -> str:
        return f"{prefix}_{sequence}_{hashlib.sha256(str(text).encode('utf-8')).hexdigest()[:12]}"

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dump(model: TextbookChapter) -> dict[str, Any]:
        if hasattr(model, "model_dump"):
            return model.model_dump(mode="json")
        return model.dict()
