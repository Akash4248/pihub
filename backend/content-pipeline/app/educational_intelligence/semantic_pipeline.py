from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from .section_builder import SectionBuilder
from .structured_models import StructuredBlock, StructuredDocument, StructuredSection
from .artifact_generator import GemmaArtifactGenerator
from .inference_client import InferenceClient
from .artifact_cleaning import clean_text as util_clean_text
from .chunk_normalizer import ChunkNormalizer
from .concept_extractor import EducationalConceptExtractor
from .concept_graph import ConceptGraphBuilder
from .concept_models import EducationalConcept
from .concept_validator import ConceptCoverageValidator
from .explanation_recovery import ExplanationRecovery
from .formula_intelligence import FormulaIntelligence
from .textbook_builder import TextbookBuilder
from .toc_cleanup import TocCleanup
from .tutor_context_builder import TutorContextBuilder
from .worked_example_builder import WorkedExampleBuilder
from shared.text_normalization import normalize_language_code


WORD_RE = re.compile(r"[\w\u0900-\u097F\u0C80-\u0CFF]+")
SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

CONTENT_TYPES = {
    "concept",
    "example",
    "worked_example",
    "activity",
    "exercise",
    "assessment",
    "glossary",
    "summary",
    "table",
    "table_of_contents",
    "index_page",
    "metadata",
}

RAG_ELIGIBLE_TYPES = {"concept", "example", "worked_example", "summary", "glossary"}

SUPPORTED_QUERIES = [
    "What is photosynthesis?",
    "Explain Newton's first law.",
    "What is a quadrilateral?",
    "Define democracy.",
    "Explain fractions.",
]


def normalize_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def word_count(text: Any) -> int:
    return len(WORD_RE.findall(str(text or "")))


def stable_hash(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def token_set(text: str) -> set[str]:
    stop = {
        "what",
        "explain",
        "define",
        "state",
        "describe",
        "with",
        "from",
        "this",
        "that",
        "first",
        "second",
        "third",
        "law",
    }
    return {token.lower() for token in WORD_RE.findall(text or "") if len(token) > 2 and token.lower() not in stop}


def contains_kannada(text: Any) -> bool:
    return bool(re.search(r"[\u0C80-\u0CFF]", str(text or "")))


def localized_text(text_en: str, text_kn: str, language: str | None) -> str:
    return text_kn if normalize_language_code(language) == "kn" else text_en


def localized_question(language: str | None, concept: str) -> str:
    if normalize_language_code(language) == "kn":
        return f"{concept} ಎಂದರೇನು, ಮತ್ತು ಅದು ಏಕೆ ಮುಖ್ಯ?"
    if normalize_language_code(language) == "hi":
        return f"{concept} क्या है, और यह क्यों महत्वपूर्ण है?"
    return f"What is {concept}, and why is it important?"


def is_toc_or_index(text: str) -> bool:
    lowered = normalize_text(text)
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if re.search(r"\b(contents|table of contents|index|answers|answer key)\b", lowered):
        page_like = sum(1 for line in lines if re.search(r"\.{2,}\s*\d+$|\b\d{1,3}$", line))
        if page_like >= 2 or len(lines) <= 30:
            return True
    if len(lines) >= 4:
        page_listing = sum(1 for line in lines if re.search(r"\.{2,}\s*\d+$|^\d+(\.\d+)*\s+.+\s+\d+$", line))
        if page_listing / len(lines) > 0.35:
            return True
    return False


def is_header_footer_or_metadata(text: str) -> bool:
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    if not stripped:
        return True
    if re.fullmatch(r"(page\s*)?\d{1,4}", stripped, re.I):
        return True
    if re.fullmatch(r"(chapter|unit|lesson)\s+\d+(\s*[:.-].*)?", stripped, re.I):
        return True
    if re.fullmatch(r"(mathematics|maths|science|social science|class\s+\d+|grade\s+\d+)", stripped, re.I):
        return True
    if re.search(r"\b(ganita prakash|curiosity|exploring society|textbook of science|grade\s+\d+\s*\|\s*part)\b", lowered):
        return True
    return bool(
        re.search(
            r"\b(isbn|copyright|all rights reserved|published by|printed at|reprint|ncert|"
            r"national council of educational research|publisher|edition)\b",
            lowered,
        )
    )


def is_ocr_noise(text: str) -> bool:
    stripped = str(text or "").strip()
    if "\ufffd" in stripped or "\x08" in stripped:
        return True
    if re.search(r"(?:[A-Za-z]\s){5,}[A-Za-z]", stripped):
        return True
    if len(stripped) >= 30:
        printable = sum(1 for char in stripped if char.isprintable())
        alpha_numeric_space = sum(1 for char in stripped if char.isalnum() or char.isspace())
        if printable and (printable - alpha_numeric_space) / printable > 0.45:
            return True
    return False


def is_formula_only(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) > 260:
        return False
    symbols = sum(1 for char in stripped if char in "=+-×÷*/^√∠∆π≤≥<>°")
    return symbols >= 2 and word_count(stripped) <= 22


def is_table_fragment(text: str) -> bool:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    short_lines = sum(1 for line in lines if len(line.strip()) <= 34)
    numeric_lines = sum(1 for line in lines if re.search(r"\d", line))
    return short_lines / len(lines) > 0.58 and numeric_lines / len(lines) > 0.25


def classify_content(text: str) -> str:
    lowered = normalize_text(text)
    words = word_count(text)
    question_marks = str(text or "").count("?")
    numbered_questions = len(re.findall(r"(^|\n)\s*\d{1,2}\s*[.)]\s+", str(text or "")))
    if is_toc_or_index(text):
        return "table_of_contents"
    if is_header_footer_or_metadata(text) or is_ocr_noise(text):
        return "metadata"
    if re.search(r"\b(glossary|key terms|terms to know)\b", lowered):
        return "glossary"
    if re.search(r"\b(summary|recap|in brief|points to remember|what you have learnt)\b", lowered):
        return "summary"
    if re.search(r"\b(activity|try this|do this|project work|observe and record)\b", lowered):
        return "activity"
    if re.search(r"\b(exercise|questions?|problems?|multiple choice|choose the correct|fill in the blanks)\b", lowered):
        return "exercise"
    if question_marks >= 2 or (question_marks >= 1 and numbered_questions >= 1):
        return "exercise"
    if re.search(r"\b(test|assessment|worksheet|exam|marks)\b", lowered):
        return "assessment"
    if re.search(r"\b(example|for example|illustration)\b", lowered):
        if re.search(r"\b(solution|solved|steps?|therefore|hence)\b", lowered):
            return "worked_example"
        return "example"
    if is_table_fragment(text):
        return "table"
    if is_formula_only(text) or words < 40:
        return "metadata"
    return "concept"


def chunk_quality_class(text: str, seen_hashes: set[str] | None = None) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return "EMPTY"
    digest = stable_hash(normalized)
    if seen_hashes is not None:
        if digest in seen_hashes:
            return "DUPLICATE"
        seen_hashes.add(digest)
    if is_toc_or_index(text):
        return "TABLE_OF_CONTENTS"
    if is_header_footer_or_metadata(text):
        return "HEADER_FOOTER"
    if is_ocr_noise(text):
        return "OCR_NOISE"
    if is_formula_only(text):
        return "FORMULA_ONLY"
    if is_table_fragment(text):
        return "TABLE_FRAGMENT"
    if word_count(text) < 100:
        return "SHORT_FRAGMENT"
    return "GOOD"


@dataclass
class SemanticPipelineResult:
    artifacts: dict[str, Any]
    reports: dict[str, Any]
    quality_gate: dict[str, Any]


class SemanticContentPipeline:
    """Build clean educational pack artifacts from retrieved curriculum chunks."""

    def __init__(
        self,
        min_words: int = 200,
        max_words: int = 400,
        near_duplicate_threshold: float = 0.95,
    ) -> None:
        self.min_words = min_words
        self.max_words = max_words
        self.near_duplicate_threshold = near_duplicate_threshold

    def build(self, chunks: list[dict[str, Any]], pack_id: str, metadata: dict[str, Any]) -> SemanticPipelineResult:
        classified, classification_report, cleanup_report = self._classify_and_clean(chunks)
        deduped, dedupe_report = self._deduplicate(classified)
        deduped, toc_cleanup_report = TocCleanup().clean(deduped)
        concept_extractor = EducationalConceptExtractor()
        educational_concepts = concept_extractor.extract(deduped, metadata)
        concept_audit = concept_extractor.audit(educational_concepts, chapter=metadata.get("chapter"), subject=metadata.get("subject"))
        concept_graph = ConceptGraphBuilder().build(educational_concepts)
        semantic_chunks, chunk_report = self._semantic_chunk(deduped)
        semantic_chunks, chunk_normalization_report = ChunkNormalizer().normalize(semantic_chunks)
        chunk_report = chunk_normalization_report
        semantic_chunks, explanation_recovery_report = ExplanationRecovery().recover(semantic_chunks)
        semantic_chunks, worked_example_report = WorkedExampleBuilder().build(semantic_chunks)
        semantic_chunks, formula_validation_report = FormulaIntelligence().enhance(semantic_chunks, educational_concepts)
        semantic_chunks, educational_concepts, tutor_context_report = TutorContextBuilder().build(semantic_chunks, educational_concepts, concept_graph)
        semantic_chunks, final_chunk_normalization_report = ChunkNormalizer().normalize(semantic_chunks)
        chunk_report = final_chunk_normalization_report
        artifacts = self._build_artifacts(semantic_chunks, pack_id, metadata, educational_concepts, concept_graph)
        textbook_publication_report = artifacts.pop("textbook_publication_report", {})
        gemma_generation_report = artifacts.pop("gemma_generation_report", {})
        coverage_model = ConceptCoverageValidator().validate(educational_concepts, artifacts)
        concept_coverage = coverage_model.model_dump() if hasattr(coverage_model, "model_dump") else coverage_model.dict()
        rag_report = self._validate_rag(artifacts["content"], metadata)
        summary_quality_v2 = self._summary_quality_v2(artifacts, educational_concepts)
        quiz_alignment_report = self._quiz_alignment_report(artifacts, educational_concepts)
        tutor_context_quality = self._tutor_context_quality(artifacts, educational_concepts)
        quality_gate = self._quality_gate(chunk_report, cleanup_report, toc_cleanup_report, dedupe_report, rag_report, concept_coverage)

        reports = {
            "content_classification": classification_report,
            "content_cleanup": cleanup_report,
            "toc_cleanup": toc_cleanup_report,
            "deduplication": dedupe_report,
            "chunk_quality": chunk_report,
            "chunk_normalization": chunk_normalization_report,
            "final_chunk_normalization": final_chunk_normalization_report,
            "explanation_recovery": explanation_recovery_report,
            "worked_example_builder": worked_example_report,
            "formula_validation": formula_validation_report,
            "tutor_context_enrichment": tutor_context_report,
            "textbook_publication": textbook_publication_report,
            "gemma_generation": gemma_generation_report,
            "concept_audit": concept_audit,
            "concept_graph": concept_graph,
            "concept_coverage": concept_coverage,
            "summary_quality_v2": summary_quality_v2,
            "quiz_alignment_report": quiz_alignment_report,
            "tutor_context_quality": tutor_context_quality,
            "rag_validation": rag_report,
        }
        return SemanticPipelineResult(artifacts=artifacts, reports=reports, quality_gate=quality_gate)

    def _classify_and_clean(self, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        type_counts: Counter[str] = Counter()
        removal_counts: Counter[str] = Counter()
        removed_pages: set[Any] = set()
        removed_chunks: list[dict[str, Any]] = []

        for chunk in chunks:
            text = self._clean_text(str(chunk.get("text") or ""))
            content_type = classify_content(text)
            quality_class = chunk_quality_class(text)
            type_counts[content_type] += 1
            metadata = dict(chunk.get("metadata") or {})
            row = {
                **chunk,
                "text": text,
                "metadata": {
                    **metadata,
                    "content_type": content_type,
                    "quality_class": quality_class,
                    "rag_eligible": content_type in RAG_ELIGIBLE_TYPES,
                },
            }

            removal_reason = self._removal_reason(row)
            if removal_reason:
                removal_counts[removal_reason] += 1
                if metadata.get("page") is not None:
                    removed_pages.add(metadata.get("page"))
                removed_chunks.append({"chunk_id": chunk.get("chunk_id"), "reason": removal_reason})
                continue
            rows.append(row)

        classification_report = {
            "total_chunks": len(chunks),
            "kept_chunks": len(rows),
            "content_type_counts": {key: int(type_counts.get(key, 0)) for key in sorted(CONTENT_TYPES)},
        }
        cleanup_report = {
            "removed_pages": len(removed_pages),
            "removed_chunks": len(chunks) - len(rows),
            "removal_reason": dict(sorted(removal_counts.items())),
            "sample_removed_chunks": removed_chunks[:50],
        }
        return rows, classification_report, cleanup_report

    def _deduplicate(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        exact_seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        near_buckets: dict[tuple[Any, Any, Any, str], list[str]] = defaultdict(list)
        duplicates_removed = 0
        duplicates_kept = 0
        cross_grade_preserved = 0

        for row in rows:
            text = str(row.get("text") or "")
            digest = stable_hash(text)
            if digest in exact_seen:
                duplicates_removed += 1
                continue
            exact_seen.add(digest)

            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            key = (
                metadata.get("grade"),
                metadata.get("subject"),
                metadata.get("chapter"),
                metadata.get("content_type"),
            )
            normalized = normalize_text(text)
            is_near_duplicate = any(
                SequenceMatcher(None, normalized, previous).ratio() >= self.near_duplicate_threshold
                for previous in near_buckets[key][-40:]
            )
            if is_near_duplicate:
                duplicates_removed += 1
                continue

            concept_key = (metadata.get("subject"), token_fingerprint(text))
            if metadata.get("grade") is not None:
                cross_grade_preserved += sum(
                    1
                    for existing in kept[-200:]
                    if isinstance(existing.get("metadata"), dict)
                    and existing["metadata"].get("grade") != metadata.get("grade")
                    and existing["metadata"].get("subject") == concept_key[0]
                    and token_fingerprint(str(existing.get("text") or "")) == concept_key[1]
                )

            near_buckets[key].append(normalized)
            duplicates_kept += 1
            kept.append(row)

        return kept, {
            "duplicates_removed": duplicates_removed,
            "duplicates_kept": duplicates_kept,
            "cross_grade_duplicates_preserved": cross_grade_preserved,
            "near_duplicate_threshold": self.near_duplicate_threshold,
        }

    def _semantic_chunk(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        grouped: dict[tuple[Any, Any, Any, Any, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            grouped[
                (
                    metadata.get("grade"),
                    metadata.get("subject"),
                    metadata.get("chapter"),
                    metadata.get("section") or metadata.get("topic"),
                    metadata.get("content_type", "concept"),
                )
            ].append(row)

        output: list[dict[str, Any]] = []
        for _, group in grouped.items():
            buffer: list[dict[str, Any]] = []
            for row in group:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                candidate_words = word_count("\n\n".join([str(item.get("text") or "") for item in buffer] + [text]))
                if buffer and candidate_words > self.max_words:
                    output.append(self._merge_rows(buffer))
                    buffer = []
                buffer.append(row)
                if candidate_words >= self.min_words and SENTENCE_END_RE.search(text):
                    output.append(self._merge_rows(buffer))
                    buffer = []
            if buffer:
                if output and self._compatible_for_merge(output[-1], buffer[0]):
                    output[-1] = self._merge_rows([output[-1], *buffer])
                else:
                    output.append(self._merge_rows(buffer))

        seen: set[str] = set()
        final_rows: list[dict[str, Any]] = []
        class_counts: Counter[str] = Counter()
        for row in output:
            text = str(row.get("text") or "")
            quality = chunk_quality_class(text, seen)
            class_counts[quality] += 1
            if quality == "DUPLICATE":
                continue
            metadata = dict(row.get("metadata") or {})
            if metadata.get("content_type") in RAG_ELIGIBLE_TYPES and quality in {
                "HEADER_FOOTER",
                "OCR_NOISE",
                "FORMULA_ONLY",
                "TABLE_FRAGMENT",
                "TABLE_OF_CONTENTS",
            }:
                continue
            if metadata.get("content_type") in RAG_ELIGIBLE_TYPES and word_count(text) < 60:
                class_counts["SHORT_RAG_DROPPED"] += 1
                continue
            metadata["quality_class"] = quality
            row["metadata"] = metadata
            final_rows.append(row)

        rag_rows = [row for row in final_rows if row.get("metadata", {}).get("content_type") in RAG_ELIGIBLE_TYPES]
        lengths = [word_count(row.get("text")) for row in rag_rows]
        return final_rows, {
            "total_chunks": len(final_rows),
            "rag_chunks": len(rag_rows),
            "average_chunk_length": round(sum(lengths) / max(1, len(lengths)), 2),
            "short_chunks": sum(1 for value in lengths if value < self.min_words),
            "long_chunks": sum(1 for value in lengths if value > self.max_words),
            "formula_only_chunks": int(class_counts.get("FORMULA_ONLY", 0)),
            "quality_class_counts": dict(sorted(class_counts.items())),
        }

    def _build_artifacts(
        self,
        rows: list[dict[str, Any]],
        pack_id: str,
        metadata: dict[str, Any],
        educational_concepts: list[EducationalConcept],
        concept_graph: dict[str, Any],
    ) -> dict[str, Any]:
        concepts = [self._enrich(row, metadata) for row in rows if row["metadata"].get("content_type") == "concept"]
        examples = [self._enrich(row, metadata) for row in rows if row["metadata"].get("content_type") == "example"]
        worked = [self._enrich(row, metadata) for row in rows if row["metadata"].get("content_type") == "worked_example"]
        formulas = [self._enrich(row, metadata) for row in rows if row["metadata"].get("content_type") == "formula_explanation"]
        tutor_contexts = [self._enrich(row, metadata) for row in rows if row["metadata"].get("content_type") == "tutor_context"]
        activities = [self._artifact(row) for row in rows if row["metadata"].get("content_type") == "activity"]
        questions = [self._artifact(row) for row in rows if row["metadata"].get("content_type") in {"exercise", "assessment"}]
        generated_artifacts = self._generate_section_artifacts(rows, metadata, educational_concepts=educational_concepts)
        glossary = generated_artifacts.get("glossary", [])
        summaries = generated_artifacts.get("summaries", [])
        quizzes = generated_artifacts.get("quizzes", [])
        flashcards = generated_artifacts.get("flashcards", [])
        concept_context = self._concept_context_chunks(educational_concepts, metadata)
        textbook, textbook_report = TextbookBuilder().build(rows, pack_id, metadata, educational_concepts)

        rag_content = [*concept_context, *tutor_contexts, *concepts, *examples, *worked, *formulas]
        rag_content.extend(
            {
                "chunk_id": f"summary_{idx}",
                "text": item["text"],
                "metadata": {
                    **metadata,
                    "content_type": "summary",
                    "rag_eligible": True,
                    "learning_objective": f"Review {metadata.get('chapter') or metadata.get('subject') or 'the lesson'}",
                    "difficulty": infer_difficulty(metadata),
                    "key_terms": item.get("key_terms", []),
                    "prerequisites": [],
                    "common_misconceptions": [],
                    "related_concepts": item.get("key_terms", []),
                },
                "embedding": [],
            }
            for idx, item in enumerate(summaries[:3], start=1)
        )
        rag_content.extend(
            {
                "chunk_id": f"glossary_{idx}_{stable_hash(item['term'])[:8]}",
                "text": f"{item['term']}: {item['definition']}",
                "metadata": {**metadata, "content_type": "glossary", "rag_eligible": True, "key_terms": [item["term"]]},
                "embedding": [],
            }
            for idx, item in enumerate(glossary[:20], start=1)
        )

        return {
            "textbook": textbook,
            "content": rag_content,
            "chapter_notes": generated_artifacts.get("chapter_notes", []),
            "key_points": generated_artifacts.get("key_points", []),
            "concepts": concepts,
            "examples": examples,
            "worked_examples": worked,
            "formulas": formulas,
            "tutor_contexts": tutor_contexts,
            "activities": activities,
            "questions": questions,
            "glossary": glossary,
            "misconceptions": generated_artifacts.get("misconceptions", []),
            "applications": generated_artifacts.get("applications", []),
            "quizzes": quizzes,
            "flashcards": flashcards,
            "summaries": summaries,
            "enrichment": {
                "related_topics": sorted({term for item in rag_content for term in item.get("metadata", {}).get("related_concepts", [])})[:60],
                "prerequisites": sorted({term for item in rag_content for term in item.get("metadata", {}).get("prerequisites", [])})[:40],
                "common_misconceptions": sorted({term for item in rag_content for term in item.get("metadata", {}).get("common_misconceptions", [])})[:80],
                "real_world_applications": sorted({term for item in rag_content for term in item.get("metadata", {}).get("real_world_applications", [])})[:80],
                "tutor_context_packages": [
                    item["metadata"]["tutor_context_package"]
                    for item in tutor_contexts
                    if isinstance(item.get("metadata"), dict) and item["metadata"].get("tutor_context_package")
                ][:80],
                "pipeline": "semantic_educational_knowledge_pipeline_v1",
                "concept_graph": concept_graph,
                "learning_objectives": generated_artifacts.get("learning_objectives", []),
            },
            "retrieval_index": {
                "vector_count": len(rag_content),
                "indexed_content_types": sorted([*RAG_ELIGIBLE_TYPES, "concept_context", "definition", "tutor_context", "worked_example_context"]),
                "version": "semantic-v1",
            },
            "textbook_publication_report": textbook_report,
            "gemma_generation_report": generated_artifacts.get("reports", {}).get("gemma_generation", {}),
        }

    def _generate_section_artifacts(
        self,
        rows: list[dict[str, Any]],
        metadata: dict[str, Any],
        educational_concepts: list[EducationalConcept] | None = None,
    ) -> dict[str, Any]:
        enabled = os.getenv("ENABLE_GEMMA_CONTENT_GENERATION", "true").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return self._heuristic_fallback_artifacts(rows, metadata, educational_concepts=educational_concepts, error_msg="Gemma disabled")
        timeout = float(os.getenv("CONTENT_GENERATION_TIMEOUT_SECONDS", "20"))
        blocks = []
        for index, row in enumerate(rows, start=1):
            row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            content_type = str(row_metadata.get("content_type") or "paragraph")
            block_type = "paragraph"
            if content_type in {"table"}:
                block_type = "table"
            elif content_type in {"formula", "formula_explanation"} or row_metadata.get("formula_intelligence"):
                block_type = "formula"
            elif content_type in {"example", "worked_example"}:
                block_type = "example"
            blocks.append(
                StructuredBlock(
                    block_id=str(row.get("chunk_id") or f"row_{index}"),
                    type=block_type,  # type: ignore[arg-type]
                    text=str(row.get("text") or ""),
                    metadata=row_metadata,
                )
            )
        document = StructuredDocument(
            source_path="semantic_content_pipeline",
            title=str(metadata.get("chapter") or metadata.get("subject") or "Textbook Section"),
            blocks=blocks,
            metadata=metadata,
        )
        sections = SectionBuilder(min_words=500, max_words=1500).build(document)
        if not sections:
            return self._heuristic_fallback_artifacts(rows, metadata, educational_concepts=educational_concepts, error_msg="No sections built")
        try:
            generated = GemmaArtifactGenerator(timeout=timeout).generate(sections, metadata)
            if generated and any(generated.get(k) for k in ("summaries", "quizzes", "flashcards", "chapter_notes")):
                return generated
        except Exception as exc:
            logger.warning("GemmaArtifactGenerator failed, falling back to heuristic generators: %s", exc)
            return self._heuristic_fallback_artifacts(rows, metadata, educational_concepts=educational_concepts, error_msg=str(exc)[:300])

        return self._heuristic_fallback_artifacts(rows, metadata, educational_concepts=educational_concepts, error_msg="Gemma returned empty artifacts")

    def _heuristic_fallback_artifacts(
        self,
        rows: list[dict[str, Any]],
        metadata: dict[str, Any],
        educational_concepts: list[EducationalConcept] | None = None,
        error_msg: str = "",
    ) -> dict[str, Any]:
        chapter_title = str(metadata.get("chapter") or metadata.get("chapter_title") or "Chapter")
        subject = str(metadata.get("subject") or "General")
        grade = metadata.get("grade") or 0
        language = metadata.get("language") or "en"

        from .summary_generator import SummaryGenerator
        from .glossary_extractor import GlossaryExtractor
        from .chapter_notes_generator import ChapterNotesGenerator
        from .misconceptions_generator import MisconceptionsGenerator
        from .applications_generator import ApplicationsGenerator
        from .learning_objectives_generator import LearningObjectivesGenerator

        # 1. Summary
        sg = SummaryGenerator()
        summary_data = sg._heuristic_summary(rows, chapter=chapter_title, topic=None)
        summaries = [
            {
                "chapter_title": chapter_title,
                "text": summary_data.get("summary", ""),
                "summary": summary_data.get("summary", ""),
                "key_terms": summary_data.get("key_points", []),
                "keyPoints": summary_data.get("key_points", []),
            }
        ]

        # 2. Glossary
        ge = GlossaryExtractor()
        glossary = ge._heuristic_extract(rows)
        existing_terms = {str(g.get("term", "")).lower() for g in glossary}
        if educational_concepts:
            for ec in educational_concepts:
                term = str(ec.name or "").strip()
                defn = str(ec.definition or "").strip()
                if term and term.lower() not in existing_terms:
                    glossary.append({
                        "term": term,
                        "definition": defn or f"{term} is an important concept studied in {chapter_title}.",
                        "chapter": chapter_title,
                        "subject": subject,
                        "language": language,
                        "source": "concept_extractor",
                    })
                    existing_terms.add(term.lower())

        # 3. Chapter Notes
        cng = ChapterNotesGenerator()
        text_parts = [str(r.get("text", "")) for r in rows if r.get("text")]
        concepts_list = [g.get("term", "") for g in glossary if g.get("term")]
        notes_data = cng._heuristic_generate(text_parts, concepts_list, metadata)
        chapter_notes = [notes_data] if notes_data else []

        # 4. Quizzes
        quizzes: list[dict[str, Any]] = []
        definitions = [g.get("definition", "") for g in glossary if g.get("definition")]
        for g in glossary[:8]:
            term = g.get("term", "")
            defn = g.get("definition", "")
            if term and defn:
                distractors = [d for d in definitions if d != defn][:3]
                while len(distractors) < 3:
                    distractors.append(f"Alternate concept variation for {term} {len(distractors) + 1}")
                quizzes.append({
                    "question_type": "mcq",
                    "question": f"What is {term}?",
                    "options": [defn] + distractors,
                    "answer": defn,
                    "correct_answer": defn,
                    "explanation": f"{term} is defined as: {defn}",
                    "chapter": chapter_title,
                    "subject": subject,
                    "language": language,
                    "generated_by": "heuristic",
                })
                quizzes.append({
                    "question_type": "true_false",
                    "question": f"True or False: {defn}",
                    "answer": True,
                    "correct_answer": "True",
                    "explanation": f"This is a correct definition of {term}.",
                    "chapter": chapter_title,
                    "subject": subject,
                    "language": language,
                    "generated_by": "heuristic",
                })

        # 5. Flashcards
        flashcards: list[dict[str, Any]] = []
        for g in glossary[:12]:
            term = g.get("term", "")
            defn = g.get("definition", "")
            if term and defn:
                flashcards.append({
                    "front": f"Define {term} and state its key characteristics.",
                    "back": defn,
                    "term": term,
                    "definition": defn,
                    "chapter": chapter_title,
                    "subject": subject,
                    "language": language,
                    "generated_by": "heuristic",
                })

        # 6. Misconceptions, Applications, Objectives
        import asyncio
        def _safe_run(coro):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        return pool.submit(asyncio.run, coro).result()
                return loop.run_until_complete(coro)
            except Exception:
                try:
                    return asyncio.run(coro)
                except Exception:
                    return []

        misconceptions = _safe_run(MisconceptionsGenerator().generate(rows))
        applications = _safe_run(ApplicationsGenerator().generate(rows))
        objectives = _safe_run(LearningObjectivesGenerator().generate(rows))

        key_points = [
            {"chapter_title": chapter_title, "key_points": summary_data.get("key_points", [])[:5]}
        ]

        return {
            "chapter_notes": chapter_notes,
            "summaries": summaries,
            "flashcards": flashcards,
            "quizzes": quizzes,
            "glossary": glossary,
            "misconceptions": misconceptions,
            "applications": applications,
            "learning_objectives": objectives,
            "key_points": key_points,
            "reports": {
                "gemma_generation": {
                    "enabled": True,
                    "fallback": "heuristic",
                    "error": error_msg,
                    "section_count": len(rows),
                }
            },
        }

    def _validate_rag(self, content: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
        rows = []
        duplicate_results = 0
        empty_results = 0
        precision_scores = []
        recall_scores = []
        corpus_terms = token_set(" ".join(str(item.get("text") or "") for item in content))

        for query in SUPPORTED_QUERIES:
            query_terms = token_set(query)
            applicable = bool(query_terms & corpus_terms)
            results = self._search_content(query, content, limit=5)
            if not results:
                empty_results += 1
            seen = set()
            duplicates = 0
            relevant = 0
            for result in results:
                digest = stable_hash(result.get("text", ""))
                if digest in seen:
                    duplicates += 1
                seen.add(digest)
                if token_set(result.get("text", "")) & query_terms:
                    relevant += 1
            duplicate_results += duplicates
            if applicable:
                precision_scores.append(relevant / max(1, len(results)))
                recall_scores.append(1.0 if relevant else 0.0)
            rows.append(
                {
                    "query": query,
                    "applicable": applicable,
                    "results": len(results),
                    "relevant_results": relevant,
                    "duplicate_results": duplicates,
                }
            )

        return {
            "queries": rows,
            "retrieval_precision": round(sum(precision_scores) / max(1, len(precision_scores)), 4),
            "retrieval_recall": round(sum(recall_scores) / max(1, len(recall_scores)), 4),
            "duplicate_results": duplicate_results,
            "empty_results": empty_results,
            "applicable_queries": len(precision_scores),
        }

    def _quality_gate(
        self,
        chunk_report: dict[str, Any],
        cleanup_report: dict[str, Any],
        toc_cleanup_report: dict[str, Any],
        dedupe_report: dict[str, Any],
        rag_report: dict[str, Any],
        concept_coverage: dict[str, Any],
    ) -> dict[str, Any]:
        total_chunks = max(1, int(chunk_report.get("total_chunks", 0)))
        output_duplicate_ratio = float(chunk_report.get("quality_class_counts", {}).get("DUPLICATE", 0)) / total_chunks
        source_duplicate_ratio = dedupe_report["duplicates_removed"] / max(1, dedupe_report["duplicates_removed"] + dedupe_report["duplicates_kept"])
        header_footer_ratio = cleanup_report["removal_reason"].get("header_footer", 0) / max(1, cleanup_report["removed_chunks"] + total_chunks)
        toc_chunks = int(toc_cleanup_report.get("toc_chunks_remaining", 0))
        average = float(chunk_report.get("average_chunk_length", 0.0))
        failures = []
        if output_duplicate_ratio >= 0.10:
            failures.append("duplicate_ratio>=10%")
        if not self.min_words <= average <= self.max_words:
            failures.append(f"average_chunk_length_outside_{self.min_words}_{self.max_words}")
        if int(chunk_report.get("chunks_below_200", 0)) != 0:
            failures.append("chunks_below_200_present")
        if int(chunk_report.get("chunks_above_400", 0)) != 0:
            failures.append("chunks_above_400_present")
        if int(chunk_report.get("quality_class_counts", {}).get("EMPTY", 0)) != 0:
            failures.append("empty_chunks_present")
        if toc_chunks != 0:
            failures.append("toc_chunks_present")
        if header_footer_ratio >= 0.01:
            failures.append("header_footer_chunks>=1%")
        if rag_report.get("applicable_queries", 0) and float(rag_report.get("retrieval_precision", 0.0)) < 0.90:
            failures.append("retrieval_precision<=90%")
        if int(chunk_report.get("rag_chunks", 0)) == 0:
            failures.append("no_rag_eligible_content")
        if float(concept_coverage.get("coverage_percent", 0.0)) < 90.0:
            failures.append("concept_coverage<90%")
        if float(concept_coverage.get("definition_coverage_percent", 0.0)) < 90.0:
            failures.append("definition_coverage<90%")
        if float(concept_coverage.get("example_coverage_percent", 0.0)) < 85.0:
            failures.append("example_coverage<85%")
        if float(concept_coverage.get("formula_coverage_percent", 0.0)) < 95.0:
            failures.append("formula_coverage<95%")
        if float(concept_coverage.get("learning_objective_coverage_percent", 0.0)) < 90.0:
            failures.append("learning_objective_coverage<90%")
        return {
            "passed": not failures,
            "failures": failures,
            "metrics": {
                "duplicate_ratio": round(output_duplicate_ratio, 4),
                "source_duplicate_ratio": round(source_duplicate_ratio, 4),
                "average_chunk_length": average,
                "chunks_below_200": int(chunk_report.get("chunks_below_200", 0)),
                "chunks_above_400": int(chunk_report.get("chunks_above_400", 0)),
                "empty_chunks": int(chunk_report.get("quality_class_counts", {}).get("EMPTY", 0)),
                "toc_chunks": toc_chunks,
                "header_footer_ratio": round(header_footer_ratio, 4),
                "retrieval_precision": rag_report.get("retrieval_precision"),
                "concept_coverage": concept_coverage.get("coverage_percent"),
                "definition_coverage": concept_coverage.get("definition_coverage_percent"),
                "example_coverage": concept_coverage.get("example_coverage_percent"),
                "formula_coverage": concept_coverage.get("formula_coverage_percent"),
                "learning_objective_coverage": concept_coverage.get("learning_objective_coverage_percent"),
            },
        }

    def _clean_text(self, text: str) -> str:
        text = CONTROL_RE.sub(" ", str(text or ""))
        lines = [re.sub(r"[ \t]+", " ", line.replace("\u00a0", " ")).strip() for line in text.splitlines()]
        counts = Counter(normalize_text(line) for line in lines if line.strip())
        cleaned: list[str] = []
        for line in lines:
            if not line:
                cleaned.append("")
                continue
            normalized = normalize_text(line)
            if is_header_footer_or_metadata(line) or is_toc_or_index(line):
                continue
            if re.fullmatch(r"\d{1,4}", line):
                continue
            if re.match(r"^(fig|table)\.?\s*\d+", line, re.I):
                continue
            if re.search(r"\b(ganita prakash|curiosity|exploring society|textbook of science)\b", line, re.I):
                continue
            if counts[normalized] >= 3 and word_count(line) <= 8:
                continue
            cleaned.append(line)
        value = "\n".join(cleaned)
        value = re.sub(r"(\w)-\n(\w)", r"\1\2", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _removal_reason(self, row: dict[str, Any]) -> str | None:
        text = str(row.get("text") or "")
        content_type = row.get("metadata", {}).get("content_type")
        if not text:
            return "empty"
        if content_type in {"table_of_contents", "index_page"}:
            return "table_of_contents"
        if content_type == "metadata":
            if is_ocr_noise(text):
                return "ocr_noise"
            if is_header_footer_or_metadata(text):
                return "header_footer"
            if is_formula_only(text):
                return "formula_only"
            if is_table_fragment(text):
                return "table_fragment"
            return "metadata"
        if content_type in {"exercise", "assessment", "table"}:
            return None
        if word_count(text) < 40:
            return "short_fragment"
        return None

    def _merge_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        first = dict(rows[0])
        texts = [str(row.get("text") or "").strip() for row in rows if str(row.get("text") or "").strip()]
        metadata = dict(first.get("metadata") or {})
        metadata["source_chunk_ids"] = [row.get("chunk_id") for row in rows if row.get("chunk_id")]
        metadata["merged_chunk_count"] = len(rows)
        merged = {
            **first,
            "chunk_id": f"semantic_{stable_hash(texts)[:16]}",
            "text": "\n\n".join(texts),
            "metadata": metadata,
        }
        return merged

    @staticmethod
    def _compatible_for_merge(existing: dict[str, Any], new_row: dict[str, Any]) -> bool:
        existing_meta = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        new_meta = new_row.get("metadata") if isinstance(new_row.get("metadata"), dict) else {}
        return (
            existing_meta.get("grade"),
            existing_meta.get("subject"),
            existing_meta.get("chapter"),
            existing_meta.get("content_type"),
        ) == (
            new_meta.get("grade"),
            new_meta.get("subject"),
            new_meta.get("chapter"),
            new_meta.get("content_type"),
        )

    def _enrich(self, row: dict[str, Any], pack_metadata: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(row.get("metadata") or {})
        text = str(row.get("text") or "")
        key_terms = extract_key_terms(text)
        enriched_metadata = {
            **metadata,
            "learning_objective": metadata.get("learning_objective") or learning_objective(metadata, pack_metadata, text),
            "difficulty": infer_difficulty({**pack_metadata, **metadata}),
            "grade": metadata.get("grade", pack_metadata.get("grade")),
            "subject": metadata.get("subject", pack_metadata.get("subject")),
            "chapter": metadata.get("chapter", pack_metadata.get("chapter")),
            "key_terms": key_terms,
            "prerequisites": metadata.get("prerequisites") or infer_prerequisites(key_terms, pack_metadata, metadata),
            "common_misconceptions": metadata.get("common_misconceptions") or infer_misconceptions(key_terms, pack_metadata, metadata),
            "related_concepts": metadata.get("related_concepts") or key_terms[:8],
            "rag_eligible": True,
        }
        if metadata.get("explanation"):
            enriched_metadata["explanation"] = metadata.get("explanation")
        if metadata.get("example"):
            enriched_metadata["example"] = metadata.get("example")
        if metadata.get("recovery_score") is not None:
            enriched_metadata["recovery_score"] = metadata.get("recovery_score")
        if metadata.get("tutor_context"):
            enriched_metadata["tutor_context"] = metadata.get("tutor_context")
        if metadata.get("recovered_from_chunk_ids"):
            enriched_metadata["recovered_from_chunk_ids"] = metadata.get("recovered_from_chunk_ids")
        if metadata.get("formula_intelligence"):
            enriched_metadata["formula_intelligence"] = metadata.get("formula_intelligence")
            enriched_metadata["formula_count"] = metadata.get("formula_count", len(metadata.get("formula_intelligence") or []))
        for field in ("tutor_context_package", "why_it_matters", "real_world_applications", "formula_context"):
            if metadata.get(field):
                enriched_metadata[field] = metadata.get(field)
        return {"chunk_id": row.get("chunk_id"), "text": text, "metadata": enriched_metadata, "embedding": row.get("embedding", [])}

    @staticmethod
    def _artifact(row: dict[str, Any]) -> dict[str, Any]:
        return {"chunk_id": row.get("chunk_id"), "text": row.get("text", ""), "metadata": row.get("metadata", {})}

    def _concept_context_chunks(self, concepts: list[EducationalConcept], metadata: dict[str, Any]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for concept in concepts[:60]:
            pieces = [
                f"Concept: {concept.name}.",
                f"Definition: {concept.definition}" if concept.definition else "",
                f"Learning objective: {concept.learning_objectives[0]}" if concept.learning_objectives else "",
            ]
            if concept.formulas:
                pieces.append("Formulae: " + "; ".join(concept.formulas[:3]))
            if concept.worked_examples:
                pieces.append("Worked example: " + concept.worked_examples[0])
            elif concept.examples:
                pieces.append("Example: " + concept.examples[0])
            package = concept.metadata.get("tutor_context_package") if isinstance(concept.metadata, dict) else None
            if isinstance(package, dict):
                if package.get("why_it_matters"):
                    pieces.append("Why it matters: " + str(package["why_it_matters"]))
                if package.get("prerequisites"):
                    pieces.append("Prerequisites: " + "; ".join(str(item) for item in package["prerequisites"][:6]))
                if package.get("common_misconceptions"):
                    pieces.append("Common misconceptions: " + "; ".join(str(item) for item in package["common_misconceptions"][:5]))
                if package.get("real_world_applications"):
                    pieces.append("Real world applications: " + "; ".join(str(item) for item in package["real_world_applications"][:6]))
            text = " ".join(piece for piece in pieces if piece)
            chunks.append(
                {
                    "chunk_id": f"context_{concept.concept_id}",
                    "text": text,
                    "metadata": {
                        **metadata,
                        "content_type": "concept_context",
                        "concept_id": concept.concept_id,
                        "concept_name": concept.name,
                        "rag_eligible": True,
                        "learning_objective": concept.learning_objectives[0] if concept.learning_objectives else f"Understand {concept.name}.",
                        "difficulty": infer_difficulty(metadata),
                        "key_terms": [concept.name, *concept.related_concepts[:6]],
                        "prerequisites": concept.prerequisites,
                        "common_misconceptions": concept.common_misconceptions,
                        "related_concepts": concept.related_concepts,
                        "why_it_matters": package.get("why_it_matters") if isinstance(package, dict) else "",
                        "real_world_applications": package.get("real_world_applications", []) if isinstance(package, dict) else [],
                        "tutor_context_package": package if isinstance(package, dict) else None,
                    },
                    "embedding": [],
                }
            )
        return chunks

    def _generate_glossary(self, rows: list[dict[str, Any]], pack_metadata: dict[str, Any], concepts: list[EducationalConcept]) -> list[dict[str, Any]]:
        terms: dict[str, str] = {}
        language = normalize_language_code(pack_metadata.get("language"))
        for concept in concepts:
            if is_good_flashcard_term(concept.name) and concept.name.lower() not in terms:
                fallback = localized_text(
                    f"Important concept in {pack_metadata.get('chapter') or 'this lesson'}.",
                    f"{pack_metadata.get('chapter') or 'ಈ ಪಾಠ'} ಯಲ್ಲಿನ ಪ್ರಮುಖ ಪರಿಕಲ್ಪನೆ.",
                    language,
                )
                terms[concept.name.lower()] = concept.definition or fallback
        for row in rows:
            text = str(row.get("text") or "")
            for term in [value for value in extract_key_terms(text) if is_good_flashcard_term(value)][:6]:
                if term.lower() not in terms:
                    fallback = localized_text(
                        f"Important term in {pack_metadata.get('chapter') or 'this lesson'}.",
                        f"{pack_metadata.get('chapter') or 'ಈ ಪಾಠ'} ಯಲ್ಲಿನ ಪ್ರಮುಖ ಪದ.",
                        language,
                    )
                    terms[term.lower()] = first_sentence_containing(text, term)[:280] or fallback
        return [{"term": key, "definition": value, "language": language} for key, value in list(terms.items())[:30]]

    def _generate_summaries(
        self,
        rows: list[dict[str, Any]],
        pack_metadata: dict[str, Any],
        pack_id: str,
        concepts: list[EducationalConcept],
    ) -> list[dict[str, Any]]:
        language = normalize_language_code(pack_metadata.get("language"))
        built_worked_examples = [
            str(row.get("text") or "")
            for row in rows
            if isinstance(row.get("metadata"), dict) and row["metadata"].get("worked_example")
        ][:5]
        if concepts:
            key_concepts = concepts[:30]
            sections = [
                localized_text(
                    "Key Concepts: ",
                    "ಪ್ರಮುಖ ಪರಿಕಲ್ಪನೆಗಳು: ",
                    language,
                )
                + "; ".join(f"{concept.name}: {concept.definition}" for concept in key_concepts if concept.definition),
                localized_text(
                    "Definitions: ",
                    "ವ್ಯಾಖ್ಯಾನಗಳು: ",
                    language,
                )
                + "; ".join(f"{concept.name} means {concept.definition}" for concept in key_concepts if concept.definition),
                localized_text(
                    "Worked Examples: ",
                    "ಪರಿಹರಿಸಿದ ಉದಾಹರಣೆಗಳು: ",
                    language,
                )
                + "; ".join([*(example for concept in key_concepts for example in concept.worked_examples[:1]), *built_worked_examples])[:900],
                localized_text("Formulae: ", "ಸೂತ್ರಗಳು: ", language)
                + "; ".join(formula for concept in key_concepts for formula in concept.formulas[:3]),
                localized_text("Common Mistakes: ", "ಸಾಮಾನ್ಯ ತಪ್ಪುಗಳು: ", language)
                + "; ".join(misconception for concept in key_concepts for misconception in concept.common_misconceptions[:1])[:1200],
                localized_text("Key Takeaways: ", "ಮುಖ್ಯ ಅಂಶಗಳು: ", language)
                + "; ".join(objective for concept in key_concepts for objective in concept.learning_objectives[:1])[:1600],
            ]
            text = "\n".join(section for section in sections if len(section.split(": ", 1)[-1].strip()) > 0)
            if word_count(text) >= 50:
                return [
                    {
                        "title": pack_metadata.get("chapter") or pack_metadata.get("subject") or pack_id,
                        "text": text[:6000],
                        "key_terms": [concept.name for concept in key_concepts],
                    }
                ]

        chunks = [row for row in rows if row.get("metadata", {}).get("content_type") in {"concept", "example", "worked_example", "summary"}]
        if not chunks:
            chunks = rows[:3]
        joined = " ".join(str(row.get("text") or "") for row in chunks[:8])
        sentences = split_sentences(joined)
        key_terms = extract_key_terms(joined)
        selected: list[str] = []
        covered: set[str] = set()
        for sentence in sentences:
            terms = set(extract_key_terms(sentence, limit=8))
            if not terms:
                continue
            if terms - covered or len(selected) < 4:
                selected.append(sentence)
                covered.update(terms)
            if word_count(" ".join(selected)) >= 120 or len(selected) >= 10:
                break
        if word_count(" ".join(selected)) < 60:
            selected = sentences[:8]
        return [
            {
                "title": pack_metadata.get("chapter") or pack_metadata.get("subject") or pack_id,
                "text": " ".join(selected)[:1800],
                "key_terms": key_terms[:12],
                "language": language,
            }
        ]

    def _generate_quizzes(
        self,
        questions: list[dict[str, Any]],
        rag_content: list[dict[str, Any]],
        pack_metadata: dict[str, Any],
        concepts: list[EducationalConcept],
    ) -> list[dict[str, Any]]:
        quizzes: list[dict[str, Any]] = []
        language = normalize_language_code(pack_metadata.get("language"))
        distractor_pool = extract_key_terms(
            " ".join(str(item.get("text") or "") for item in rag_content),
            limit=40,
        )
        for concept in concepts[:10]:
            objective = concept.learning_objectives[0] if concept.learning_objectives else f"Understand {concept.name}."
            question = self._question_from_objective(objective, concept.name, language)
            answer = concept.definition or first_sentence(concept.examples[0] if concept.examples else concept.worked_examples[0] if concept.worked_examples else objective)
            quizzes.append(self._quiz_item(question, answer, [*concept.related_concepts, *distractor_pool], source="learning_objective", language=language))
            if len(quizzes) >= 10:
                return quizzes
        for question in questions[:10]:
            text = str(question.get("text") or "")
            if "?" in text:
                prompt = text.split("?")[0][:180] + "?"
            else:
                prompt = localized_text(
                    f"Explain one key idea from {pack_metadata.get('chapter') or 'this lesson'}.",
                    f"{pack_metadata.get('chapter') or 'ಈ ಪಾಠ'} ಯಿಂದ ಒಂದು ಪ್ರಮುಖ ಕಲ್ಪನೆಯನ್ನು ವಿವರಿಸಿ.",
                    language,
                )
            answer = first_sentence(text) or localized_text("Use the lesson explanation.", "ಪಾಠದ ವಿವರಣೆಯನ್ನು ಬಳಸಿ.", language)
            quizzes.append(self._quiz_item(prompt, answer, distractor_pool, source="questions", language=language))
        for item in rag_content[: max(0, 10 - len(quizzes))]:
            terms = item.get("metadata", {}).get("key_terms", [])
            term = terms[0] if terms else pack_metadata.get("chapter") or localized_text("the concept", "ಆ ಪರಿಕಲ್ಪನೆ", language)
            answer = first_sentence(str(item.get("text") or ""))[:360]
            quizzes.append(self._quiz_item(localized_question(language, term), answer, distractor_pool, source="concept", language=language))
        return quizzes

    @staticmethod
    def _question_from_objective(objective: str, concept_name: str, language: str | None) -> str:
        lowered = objective.lower()
        if "calculate" in lowered:
            return localized_text(
                f"How do you calculate or use {concept_name}?",
                f"{concept_name} ಅನ್ನು ಹೇಗೆ ಲೆಕ್ಕ ಹಾಕುವುದು ಅಥವಾ ಬಳಸುವುದು?",
                language,
            )
        if "compare" in lowered:
            return localized_text(
                f"How would you compare {concept_name} with a related concept?",
                f"{concept_name} ಅನ್ನು ಸಂಬಂಧಿತ ಪರಿಕಲ್ಪನೆಯೊಂದಿಗೆ ಹೇಗೆ ಹೋಲಿಸುತ್ತೀರಿ?",
                language,
            )
        if "identify" in lowered:
            return localized_text(
                f"How can you identify {concept_name} in a lesson or example?",
                f"ಪಾಠ ಅಥವಾ ಉದಾಹರಣೆಯಲ್ಲಿ {concept_name} ಅನ್ನು ಹೇಗೆ ಗುರುತಿಸುತ್ತೀರಿ?",
                language,
            )
        return localized_question(language, concept_name)

    @staticmethod
    def _quiz_item(question: str, answer: str, distractor_pool: list[str], source: str, language: str | None = None) -> dict[str, Any]:
        language = normalize_language_code(language) or ("kn" if contains_kannada(question) or contains_kannada(answer) else "en")
        answer = answer or localized_text("Review the lesson explanation.", "ಪಾಠದ ವಿವರಣೆಯನ್ನು ಪರಿಶೀಲಿಸಿ.", language)
        answer_label = "A"
        distractors = [term.title() for term in distractor_pool if term.lower() not in answer.lower() and term.lower() not in question.lower()]
        option_texts = [answer, *[localized_text(f"Only related to {term}", f"{term} ಗೆ ಮಾತ್ರ ಸಂಬಂಧಿತ", language) for term in distractors[:3]]]
        while len(option_texts) < 4:
            option_texts.append(localized_text("A related but incomplete idea from the chapter", "ಅಧ್ಯಾಯಕ್ಕೆ ಸಂಬಂಧಿಸಿದ ಆದರೆ ಅಪೂರ್ಣ ಕಲ್ಪನೆ", language))
        return {
            "question": question,
            "options": [{"label": chr(ord("A") + idx), "text": text[:220]} for idx, text in enumerate(option_texts[:4])],
            "correct_answer": answer,
            "explanation": localized_text(
                f"The correct answer is supported by the chapter text: {answer[:240]}",
                f"ಸರಿಯಾದ ಉತ್ತರವನ್ನು ಅಧ್ಯಾಯದ ಪಠ್ಯ ಬೆಂಬಲಿಸುತ್ತದೆ: {answer[:240]}",
                language,
            ),
            "difficulty": "medium",
            "source": source,
            "answer_label": answer_label,
        }

    def _generate_flashcards(
        self,
        rag_content: list[dict[str, Any]],
        glossary: list[dict[str, Any]],
        pack_metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        cards = [{"front": item["term"], "back": item["definition"]} for item in glossary[:20]]
        language = normalize_language_code(pack_metadata.get("language"))
        seen = {normalize_text(card["front"]) for card in cards}
        for item in rag_content:
            worked_example = item.get("metadata", {}).get("worked_example")
            if worked_example:
                concepts = item.get("metadata", {}).get("concepts_used", [])
                front = localized_text(
                    f"How do you solve a {item.get('metadata', {}).get('problem_type', 'worked example')} problem?",
                    f"{item.get('metadata', {}).get('problem_type', 'worked example')} ಸಮಸ್ಯೆಯನ್ನು ಹೇಗೆ ಪರಿಹರಿಸುತ್ತೀರಿ?",
                    language,
                )
                if concepts:
                    front = localized_text(
                        f"How do you use {str(concepts[0]).title()} in a worked example?",
                        f"ಕಾರ್ಯಗತ ಉದಾಹರಣೆಯಲ್ಲಿ {str(concepts[0]).title()} ಅನ್ನು ಹೇಗೆ ಬಳಸುತ್ತೀರಿ?",
                        language,
                    )
                if normalize_text(front) not in seen:
                    cards.append({"front": front, "back": first_sentence(str(item.get("text") or ""))[:320], "language": language})
                    seen.add(normalize_text(front))
                    if len(cards) >= 30:
                        break
            terms = [term for term in item.get("metadata", {}).get("key_terms", []) if is_good_flashcard_term(term)]
            if not terms:
                continue
            front = terms[0].title()
            if normalize_text(front) in seen:
                continue
            back = first_sentence_containing(str(item.get("text") or ""), terms[0]) or first_sentence(str(item.get("text") or ""))
            cards.append({"front": front, "back": back[:320], "language": language})
            seen.add(normalize_text(front))
            if len(cards) >= 30:
                break
        return cards

    def _summary_quality_v2(self, artifacts: dict[str, Any], concepts: list[EducationalConcept]) -> dict[str, Any]:
        summary_text = " ".join(str(item.get("text") or "") for item in artifacts.get("summaries", []))
        concept_names = [concept.name for concept in concepts]
        retained = [name for name in concept_names if normalize_text(name) in normalize_text(summary_text)]
        definitions = [concept.name for concept in concepts if concept.definition and normalize_text(concept.definition)[:60] in normalize_text(summary_text)]
        examples = [concept.name for concept in concepts if (concept.examples or concept.worked_examples) and concept.name in retained]
        formulas = [formula for concept in concepts for formula in concept.formulas if normalize_text(formula) in normalize_text(summary_text)]
        return {
            "concept_coverage": percent_count(len(retained), len(concept_names)),
            "definition_coverage": percent_count(len(definitions), sum(1 for concept in concepts if concept.definition)),
            "example_coverage": percent_count(len(examples), sum(1 for concept in concepts if concept.examples or concept.worked_examples)),
            "formula_coverage": percent_count(len(formulas), sum(len(concept.formulas) for concept in concepts)),
            "summary_count": len(artifacts.get("summaries", [])),
        }

    def _quiz_alignment_report(self, artifacts: dict[str, Any], concepts: list[EducationalConcept]) -> dict[str, Any]:
        concept_terms = {normalize_text(concept.name) for concept in concepts}
        rows = []
        for quiz in artifacts.get("quizzes", []):
            text = normalize_text(" ".join(str(quiz.get(key) or "") for key in ("question", "correct_answer", "explanation")))
            aligned = any(term and term in text for term in concept_terms)
            rows.append(
                {
                    "question": quiz.get("question"),
                    "aligned_to_concept": aligned,
                    "has_explanation": word_count(quiz.get("explanation")) >= 8,
                    "has_distractors": len(quiz.get("options") or []) >= 4,
                    "difficulty": quiz.get("difficulty"),
                }
            )
        return {
            "quiz_count": len(rows),
            "alignment_percent": percent_count(sum(1 for row in rows if row["aligned_to_concept"]), len(rows)),
            "explanation_percent": percent_count(sum(1 for row in rows if row["has_explanation"]), len(rows)),
            "distractor_percent": percent_count(sum(1 for row in rows if row["has_distractors"]), len(rows)),
            "rows": rows[:40],
        }

    def _tutor_context_quality(self, artifacts: dict[str, Any], concepts: list[EducationalConcept]) -> dict[str, Any]:
        content = artifacts.get("content", [])
        context_types = Counter(str(item.get("metadata", {}).get("content_type")) for item in content if isinstance(item.get("metadata"), dict))
        concept_context_count = context_types.get("concept_context", 0)
        definition_count = sum(1 for item in content if "definition:" in normalize_text(item.get("text")))
        worked_count = sum(1 for item in content if "worked example:" in normalize_text(item.get("text")))
        return {
            "content_count": len(content),
            "concept_context_count": concept_context_count,
            "definition_context_count": definition_count,
            "worked_example_context_count": worked_count,
            "concept_context_coverage": percent_count(concept_context_count, len(concepts)),
            "context_types": dict(context_types),
        }

    @staticmethod
    def _search_content(query: str, content: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        q_terms = token_set(query)
        scored = []
        for item in content:
            terms = token_set(str(item.get("text") or ""))
            if not terms:
                continue
            overlap = len(q_terms & terms)
            score = overlap / math.sqrt(max(1, len(terms)))
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for score, item in scored[:limit] if score > 0]


def token_fingerprint(text: str) -> str:
    terms = sorted(token_set(text))
    return "|".join(terms[:30])


def extract_key_terms(text: str, limit: int = 16) -> list[str]:
    stop = {
        "this",
        "that",
        "with",
        "from",
        "have",
        "which",
        "their",
        "there",
        "about",
        "because",
        "these",
        "those",
        "when",
        "where",
        "what",
        "will",
        "they",
        "were",
        "been",
        "into",
        "chapter",
        "exercise",
        "activity",
        "image",
        "images",
        "figure",
        "table",
        "page",
        "textbook",
        "ganita",
        "prakash",
        "curiosity",
        "class",
        "part",
    }
    counts = Counter(token.lower() for token in WORD_RE.findall(text or "") if len(token) >= 2 and token.lower() not in stop and not token.isdigit())
    return [term for term, _ in counts.most_common(limit)]


def is_good_flashcard_term(term: str) -> bool:
    normalized = normalize_text(term)
    if not normalized:
        return False
    if contains_kannada(normalized) and len(normalized) < 2:
        return False
    if not contains_kannada(normalized) and len(normalized) < 4:
        return False
    bad = {
        "image",
        "images",
        "figure",
        "table",
        "chapter",
        "activity",
        "exercise",
        "question",
        "ganita",
        "prakash",
        "curiosity",
        "textbook",
        "grade",
        "page",
    }
    return normalized not in bad and not normalized.isdigit()


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?।॥])\s+", text or "") if item.strip()]


def first_sentence(text: str) -> str:
    sentences = split_sentences(text)
    return sentences[0] if sentences else str(text or "").strip()


def first_sentence_containing(text: str, term: str) -> str:
    for sentence in split_sentences(text):
        if term.lower() in sentence.lower():
            return sentence.strip()
    return first_sentence(text)


def infer_difficulty(metadata: dict[str, Any]) -> str:
    try:
        grade = int(metadata.get("grade") or 0)
    except (TypeError, ValueError):
        grade = 0
    if grade <= 5:
        return "easy"
    if grade <= 8:
        return "medium"
    return "hard"


def learning_objective(metadata: dict[str, Any], pack_metadata: dict[str, Any], text: str) -> str:
    topic = metadata.get("topic") or metadata.get("section") or pack_metadata.get("chapter") or pack_metadata.get("subject") or "the concept"
    content_type = metadata.get("content_type", "concept")
    language = normalize_language_code(metadata.get("language") or pack_metadata.get("language"))
    if content_type == "worked_example":
        return localized_text(
            f"Solve and explain worked examples related to {topic}.",
            f"{topic} ಗೆ ಸಂಬಂಧಿಸಿದ ಕಾರ್ಯಗತ ಉದಾಹರಣೆಗಳನ್ನು ಪರಿಹರಿಸಿ ಮತ್ತು ವಿವರಿಸಿ.",
            language,
        )
    if content_type == "example":
        return localized_text(
            f"Connect examples to the core idea of {topic}.",
            f"ಉದಾಹರಣೆಗಳನ್ನು {topic} ನ ಪ್ರಮುಖ ಕಲ್ಪನೆಯೊಂದಿಗೆ ಸಂಪರ್ಕಿಸಿ.",
            language,
        )
    return localized_text(
        f"Understand and explain {topic}.",
        f"{topic} ಅನ್ನು ಅರ್ಥಮಾಡಿಕೊಂಡು ವಿವರಿಸಿ.",
        language,
    )


def infer_prerequisites(key_terms: list[str], metadata: dict[str, Any], row_metadata: dict[str, Any] | None = None) -> list[str]:
    subject = normalize_text(metadata.get("subject"))
    language = normalize_language_code((row_metadata or {}).get("language") or metadata.get("language"))
    prerequisites: list[str] = []
    if subject in {"math", "maths", "mathematics"}:
        prerequisites.extend(
            [
                localized_text("number sense", "ಸಂಖ್ಯಾ ಅರಿವು", language),
                localized_text("basic operations", "ಮೂಲ ಗಣಿತ ಕ್ರಿಯೆಗಳು", language),
            ]
        )
    if "photosynthesis" in key_terms:
        prerequisites.append(localized_text("parts of a plant", "ಸಸ್ಯದ ಭಾಗಗಳು", language))
    if "democracy" in key_terms:
        prerequisites.append(localized_text("government", "ಸರ್ಕಾರ", language))
    return prerequisites[:6]


def infer_misconceptions(key_terms: list[str], metadata: dict[str, Any] | None = None, row_metadata: dict[str, Any] | None = None) -> list[str]:
    misconceptions: list[str] = []
    terms = {term.lower() for term in key_terms}
    language = normalize_language_code((row_metadata or {}).get("language") or (metadata or {}).get("language"))
    if "photosynthesis" in terms:
        misconceptions.append(localized_text("Plants get all their food directly from soil.", "ಸಸ್ಯಗಳು ತಮ್ಮ ಆಹಾರವನ್ನು ನೇರವಾಗಿ ಮಣ್ಣಿನಿಂದ ಪಡೆಯುತ್ತವೆ.", language))
    if "force" in terms:
        misconceptions.append(localized_text("Motion always requires a continuous force.", "ಚಲನೆಗೆ ಯಾವಾಗಲೂ ನಿರಂತರ ಬಲ ಬೇಕು.", language))
    if "fraction" in terms or "fractions" in terms:
        misconceptions.append(localized_text("A larger denominator always means a larger fraction.", "ಹೆಚ್ಚಿನ ಹರವು ಯಾವಾಗಲೂ ದೊಡ್ಡ ಭಿನ್ನರಾಶಿಯನ್ನು ಸೂಚಿಸುತ್ತದೆ.", language))
    return misconceptions[:5]


def percent_count(value: int, total: int) -> float:
    if total == 0:
        return 100.0
    return round(100.0 * value / total, 2)


def write_pipeline_reports(reports: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_map = {
        "content_classification": "content_classification.json",
        "content_cleanup": "content_cleanup_report.json",
        "toc_cleanup": "toc_cleanup_report.json",
        "deduplication": "deduplication_report.json",
        "chunk_normalization": "chunk_normalization_report.json",
        "final_chunk_normalization": "final_chunk_normalization_report.json",
        "explanation_recovery": "explanation_recovery_report.json",
        "formula_validation": "formula_validation_report.json",
        "tutor_context_enrichment": "tutor_context_report.json",
        "textbook_publication": "textbook_publication_report.json",
        "chunk_quality": "chunk_quality_report.json",
        "rag_validation": "rag_validation_report.json",
    }
    for key, filename in file_map.items():
        (output_dir / filename).write_text(json.dumps(reports.get(key, {}), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
