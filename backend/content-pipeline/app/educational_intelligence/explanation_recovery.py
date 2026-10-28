from __future__ import annotations

import re
from collections import Counter
from typing import Any


EDUCATIONAL_SIGNALS = (
    "because",
    "therefore",
    "hence",
    "for example",
    "for instance",
    "means that",
    "defined as",
    "can be explained as",
)
EXAMPLE_SIGNALS = ("example", "consider", "suppose", "let us", "if", "when")
EXPLANATION_SIGNALS = ("why", "how", "reason", "process", "relationship")
REJECT_TYPES = {"exercise", "assessment", "activity", "table_of_contents", "index_page", "metadata"}
TARGET_TYPES = {"concept", "glossary", "summary"}
FORMULA_RE = re.compile(r"(?:[A-Za-z][A-Za-z\s]{0,35}|[A-Za-z])\s*(?:=|<|>|≤|≥|≈|∝)\s*[A-Za-z0-9πθ°%+\-*/×÷^().,\s]{1,110}")
WORD_RE = re.compile(r"[A-Za-z0-9]+")


class ExplanationRecovery:
    """Recover explanation context from neighboring chunks during enrichment."""

    def recover(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        chunks_examined = 0
        chunks_recovered = 0
        explanation_lengths: list[int] = []
        definition_targets = definition_recovered = 0
        formula_targets = formula_recovered = 0
        recovered_scores: list[float] = []
        recovered_by_type: Counter[str] = Counter()

        for index, row in enumerate(rows):
            copied = {**row, "metadata": dict(row.get("metadata") or {})}
            content_type = str(copied["metadata"].get("content_type") or "")
            text = str(copied.get("text") or "")
            is_formula = bool(FORMULA_RE.search(text))
            if not self._is_target(copied):
                recovered.append(copied)
                continue

            chunks_examined += 1
            if content_type == "glossary" or self._looks_like_definition(text):
                definition_targets += 1
            if is_formula:
                formula_targets += 1

            candidate = self._best_candidate(rows, index)
            if candidate:
                explanation = candidate["explanation"]
                example = candidate["example"]
                score = candidate["score"]
                copied["metadata"]["explanation"] = explanation
                copied["metadata"]["example"] = example
                copied["metadata"]["recovery_score"] = score
                copied["metadata"]["recovered_from_chunk_ids"] = candidate["source_chunk_ids"]
                copied["metadata"]["tutor_context"] = self._tutor_context(text, explanation, example)
                copied["text"] = self._enhance_text(text, explanation, example)
                chunks_recovered += 1
                recovered_scores.append(score)
                explanation_lengths.append(len(WORD_RE.findall(explanation)))
                recovered_by_type[content_type or "unknown"] += 1
                if content_type == "glossary" or self._looks_like_definition(text):
                    definition_recovered += 1
                if is_formula:
                    formula_recovered += 1
            recovered.append(copied)

        missing_explanation_rate = 100.0 - self._percent(chunks_recovered, chunks_examined)
        return recovered, {
            "chunks_examined": chunks_examined,
            "chunks_recovered": chunks_recovered,
            "missing_explanation_rate": round(missing_explanation_rate, 2),
            "average_explanation_length": round(sum(explanation_lengths) / max(1, len(explanation_lengths)), 2),
            "average_recovery_score": round(sum(recovered_scores) / max(1, len(recovered_scores)), 4),
            "definition_with_explanation_rate": self._percent(definition_recovered, definition_targets),
            "formula_with_explanation_rate": self._percent(formula_recovered, formula_targets),
            "definition_targets": definition_targets,
            "formula_targets": formula_targets,
            "recovered_by_type": dict(recovered_by_type),
        }

    def _best_candidate(self, rows: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
        current = rows[index]
        candidates = []
        for neighbor_index in range(max(0, index - 2), min(len(rows), index + 3)):
            neighbor = rows[neighbor_index]
            if neighbor_index == index:
                neighbor = current
            if self._reject(neighbor):
                continue
            if not self._same_context(current, neighbor):
                continue
            text = str(neighbor.get("text") or "")
            score = self._score(text, current)
            if score <= 0:
                continue
            candidates.append((score, neighbor_index, neighbor))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], -abs(item[1] - index)), reverse=True)
        selected = candidates[:3]
        explanation_parts = []
        example_parts = []
        source_ids = []
        for _score, _neighbor_index, neighbor in selected:
            text = str(neighbor.get("text") or "")
            source_ids.append(str(neighbor.get("chunk_id") or ""))
            explanation_parts.extend(self._explanation_sentences(text))
            example_parts.extend(self._example_sentences(text))
        explanation = " ".join(dict.fromkeys(explanation_parts))[:1200]
        example = " ".join(dict.fromkeys(example_parts))[:800]
        if not explanation and not example:
            return None
        score = round(sum(item[0] for item in selected) / max(1, len(selected)), 4)
        return {
            "explanation": explanation or example,
            "example": example,
            "score": score,
            "source_chunk_ids": [item for item in source_ids if item],
        }

    def _is_target(self, row: dict[str, Any]) -> bool:
        if self._reject(row):
            return False
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content_type = str(metadata.get("content_type") or "")
        text = str(row.get("text") or "")
        return content_type in TARGET_TYPES or bool(FORMULA_RE.search(text)) or self._looks_like_definition(text)

    def _reject(self, row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content_type = str(metadata.get("content_type") or "")
        quality_class = str(metadata.get("quality_class") or "")
        if content_type in REJECT_TYPES:
            return True
        return quality_class in {"OCR_NOISE", "TABLE_OF_CONTENTS", "HEADER_FOOTER", "EMPTY"}

    def _same_context(self, current: dict[str, Any], neighbor: dict[str, Any]) -> bool:
        current_meta = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        neighbor_meta = neighbor.get("metadata") if isinstance(neighbor.get("metadata"), dict) else {}
        if current_meta.get("chapter") != neighbor_meta.get("chapter"):
            return False
        current_section = current_meta.get("section") or current_meta.get("topic")
        neighbor_section = neighbor_meta.get("section") or neighbor_meta.get("topic")
        if current_section and neighbor_section and current_section == neighbor_section:
            return True
        return bool(self._concept_family(str(current.get("text") or "")) & self._concept_family(str(neighbor.get("text") or "")))

    def _score(self, text: str, current: dict[str, Any]) -> float:
        lowered = text.lower()
        score = 0.0
        score += 2.0 * sum(1 for signal in EDUCATIONAL_SIGNALS if signal in lowered)
        score += 1.4 * sum(1 for signal in EXAMPLE_SIGNALS if re.search(rf"\b{re.escape(signal)}\b", lowered))
        score += 1.2 * sum(1 for signal in EXPLANATION_SIGNALS if signal in lowered)
        if FORMULA_RE.search(text):
            score += 1.4
        overlap = len(self._concept_family(text) & self._concept_family(str(current.get("text") or "")))
        score += min(3.0, overlap * 0.7)
        words = len(WORD_RE.findall(text))
        if 45 <= words <= 260:
            score += 1.0
        elif words > 360:
            score -= 0.8
        return max(0.0, score)

    @staticmethod
    def _concept_family(text: str) -> set[str]:
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
            "question",
            "example",
        }
        return {token.lower() for token in WORD_RE.findall(text or "") if len(token) >= 4 and token.lower() not in stop and not token.isdigit()}

    @staticmethod
    def _looks_like_definition(text: str) -> bool:
        return bool(re.search(r"\b(is|are|means|refers to|is called|are called|defined as)\b", text, re.I))

    @staticmethod
    def _explanation_sentences(text: str) -> list[str]:
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]
        selected = []
        for sentence in sentences:
            lowered = sentence.lower()
            if any(signal in lowered for signal in (*EDUCATIONAL_SIGNALS, *EXPLANATION_SIGNALS)) or (len(WORD_RE.findall(sentence)) >= 18 and not sentence.endswith("?")):
                selected.append(sentence)
            if len(selected) >= 4:
                break
        return selected

    @staticmethod
    def _example_sentences(text: str) -> list[str]:
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]
        return [sentence for sentence in sentences if any(signal in sentence.lower() for signal in EXAMPLE_SIGNALS)][:3]

    @staticmethod
    def _enhance_text(text: str, explanation: str, example: str) -> str:
        pieces = [text]
        if explanation and explanation not in text:
            pieces.append(f"Explanation: {explanation}")
        if example and example not in text:
            pieces.append(f"Example: {example}")
        return "\n\n".join(piece for piece in pieces if piece).strip()

    @staticmethod
    def _tutor_context(text: str, explanation: str, example: str) -> str:
        return " ".join(piece for piece in (text, explanation, example) if piece)[:1800]

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if not total:
            return 100.0
        return round(100.0 * value / total, 2)
