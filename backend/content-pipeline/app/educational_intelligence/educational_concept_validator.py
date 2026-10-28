from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .concept_models import ConceptType


STOPWORD_PHRASES = {
    "a b",
    "above",
    "about",
    "after",
    "activity",
    "again",
    "along",
    "also",
    "another",
    "based",
    "below",
    "between",
    "called",
    "chapter",
    "class",
    "curiosity",
    "during",
    "each",
    "example",
    "exercise",
    "every",
    "fig",
    "figure",
    "find",
    "following",
    "from",
    "ganita",
    "general",
    "grade",
    "have",
    "image",
    "images",
    "introduction",
    "let us",
    "lets explore",
    "like",
    "more",
    "number",
    "page",
    "prakash",
    "question",
    "questions",
    "same",
    "science",
    "some",
    "table",
    "textbook",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "three",
    "what",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}


@dataclass(frozen=True)
class ConceptValidationResult:
    valid: bool
    reason: str
    classification: str
    concept_type: ConceptType


class EducationalConceptValidator:
    def validate(self, term: Any, evidence: dict[str, Any] | None = None) -> ConceptValidationResult:
        evidence = evidence or {}
        raw = str(term or "").strip()
        normalized = self._normalize(raw)
        if not normalized:
            return self._invalid("empty", "malformed phrase")
        if len(normalized) < 4:
            return self._invalid("shorter than threshold", "malformed phrase")
        if len(normalized) > 70:
            return self._invalid("too long", "malformed phrase")
        if normalized in STOPWORD_PHRASES:
            return self._invalid("stopword phrase", "glossary noise")
        if re.fullmatch(r"(chapter|unit|lesson|class|grade)\s*\d+.*", normalized):
            return self._invalid("chapter label", "chapter title")
        if re.fullmatch(r"(fig|figure|table)\s*\.?\s*\d+.*", normalized):
            return self._invalid("page artifact", "page artifact")
        if re.fullmatch(r"\d+(\.\d+)*", normalized):
            return self._invalid("page number", "page artifact")
        if any(marker in normalized for marker in ("isbn", "copyright", "ncert", "textbook of", "published by")):
            return self._invalid("publisher text", "publisher metadata")
        if "\ufffd" in raw or "\x08" in raw or "\x07" in raw or re.search(r"(?:[a-z]\s){4,}[a-z]", normalized):
            return self._invalid("ocr fragment", "OCR artifact")
        alpha_count = sum(1 for char in normalized if char.isalpha())
        if alpha_count / max(1, len(normalized)) < 0.45:
            return self._invalid("low alphabetic density", "malformed phrase")
        tokens = normalized.split()
        if tokens[0] in {"and", "but", "if", "when", "while", "where", "which", "that", "this", "these", "those", "there"}:
            return self._invalid("clause opener", "malformed phrase")
        if len(tokens) > 5 and not evidence.get("has_formula"):
            return self._invalid("long phrase", "malformed phrase")
        if len(tokens) == 1 and evidence.get("frequency", 0) < 2 and not evidence.get("has_definition"):
            return self._invalid("weak single-token evidence", "glossary noise")
        if len(set(tokens)) < len(tokens) / 2:
            return self._invalid("repeated tokens", "malformed phrase")
        return ConceptValidationResult(True, "valid", "valid concept", self.classify_type(raw, evidence))

    def classify_type(self, term: str, evidence: dict[str, Any] | None = None) -> ConceptType:
        evidence = evidence or {}
        text = f"{term} {evidence.get('text', '')}".lower()
        if evidence.get("has_formula") or re.search(r"[=<>≤≥]", text):
            return ConceptType.FORMULA
        if "theorem" in text:
            return ConceptType.THEOREM
        if re.search(r"\blaw\b", text):
            return ConceptType.LAW
        if any(word in text for word in ("principle", "rule", "property")):
            return ConceptType.PRINCIPLE
        if any(word in text for word in ("process", "cycle", "formation", "method")):
            return ConceptType.PROCESS
        if evidence.get("has_example"):
            return ConceptType.EXAMPLE
        if evidence.get("has_definition"):
            return ConceptType.DEFINITION
        return ConceptType.CONCEPT

    @staticmethod
    def _normalize(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9\s-]", " ", value.lower())
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _invalid(reason: str, classification: str) -> ConceptValidationResult:
        return ConceptValidationResult(False, reason, classification, ConceptType.CONCEPT)
