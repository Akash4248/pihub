from __future__ import annotations

import re
from collections import Counter
from typing import Any


WINDOW_SIZE = 2
REJECT_TYPES = {"metadata", "table_of_contents", "index_page", "activity", "assessment"}
SIGNAL_RE = re.compile(r"\b(example|worked example|solved problem|solution|illustration|exercise|problem)\b", re.I)
QUESTION_RE = re.compile(r"\?\s*$|^\s*(?:find|calculate|show|prove|solve|what|why|how|explain|determine)\b", re.I)
ANSWER_RE = re.compile(r"\b(?:answer|solution|therefore|hence|so|thus|=)\b", re.I)
STEP_RE = re.compile(r"(?:(?:step\s*\d+|first|next|then|finally)[,:]?\s*)?([^.!?]{12,220}[.!?])", re.I)
WORD_RE = re.compile(r"[A-Za-z0-9]+")


class WorkedExampleBuilder:
    """Build cohesive worked examples from neighboring problem, method, and answer chunks."""

    def build(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        output = list(rows)
        examples = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            if self._reject(row) or not self._candidate(row):
                continue
            window = self._window(rows, index)
            built = self._build_example(row, window)
            if not built:
                continue
            digest = self._digest(built["problem"], built["final_answer"])
            if digest in seen:
                continue
            seen.add(digest)
            output.append(self._row_from_example(row, built, len(examples) + 1))
            examples.append(built)

        steps_counts = [len(example["steps"]) for example in examples]
        concepts = {concept for example in examples for concept in example["concepts_used"]}
        return output, {
            "worked_examples_created": len(examples),
            "average_steps_per_example": round(sum(steps_counts) / max(1, len(steps_counts)), 2),
            "concepts_with_examples": len(concepts),
            "example_coverage": min(100.0, self._percent(len(concepts), self._concept_pool(rows))),
            "examples": examples[:50],
        }

    def _candidate(self, row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content_type = str(metadata.get("content_type") or "")
        text = str(row.get("text") or "")
        return content_type in {"worked_example", "example", "exercise"} or bool(SIGNAL_RE.search(text))

    def _reject(self, row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content_type = str(metadata.get("content_type") or "")
        quality_class = str(metadata.get("quality_class") or "")
        return content_type in REJECT_TYPES or quality_class in {"OCR_NOISE", "TABLE_OF_CONTENTS", "HEADER_FOOTER", "EMPTY"}

    def _window(self, rows: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
        current = rows[index]
        return [
            row
            for row in rows[max(0, index - WINDOW_SIZE) : min(len(rows), index + WINDOW_SIZE + 1)]
            if not self._reject(row) and self._same_context(current, row)
        ]

    def _build_example(self, row: dict[str, Any], window: list[dict[str, Any]]) -> dict[str, Any] | None:
        text = "\n\n".join(str(item.get("text") or "") for item in window if item.get("text"))
        if len(WORD_RE.findall(text)) < 35:
            return None
        problem = self._problem(text) or self._problem(str(row.get("text") or ""))
        if not problem:
            return None
        steps = self._steps(text)
        final_answer = self._final_answer(text)
        if not steps and not final_answer:
            return None
        concepts = self._concepts(text)
        explanation = self._explanation(text, steps, final_answer)
        if not explanation:
            return None
        return {
            "problem": problem[:500],
            "approach": self._approach(text, concepts),
            "steps": steps[:6],
            "final_answer": final_answer[:320],
            "explanation": explanation[:900],
            "problem_type": self._problem_type(text),
            "difficulty": self._difficulty(text),
            "concepts_used": concepts[:10],
            "steps_count": len(steps[:6]),
        }

    def _row_from_example(self, source: dict[str, Any], example: dict[str, Any], sequence: int) -> dict[str, Any]:
        metadata = dict(source.get("metadata") or {})
        source_ids = metadata.get("source_chunk_ids") if isinstance(metadata.get("source_chunk_ids"), list) else []
        metadata.update(
            {
                "content_type": "worked_example",
                "worked_example": True,
                "problem_type": example["problem_type"],
                "difficulty": example["difficulty"],
                "concepts_used": example["concepts_used"],
                "steps_count": example["steps_count"],
                "rag_eligible": True,
                "quality_class": "GOOD",
            }
        )
        text = "\n".join(
            [
                f"Problem: {example['problem']}",
                f"Approach: {example['approach']}",
                *[f"Step {idx + 1}: {step}" for idx, step in enumerate(example["steps"])],
                f"Final answer: {example['final_answer']}",
                f"Explanation: {example['explanation']}",
            ]
        )
        return {
            **source,
            "chunk_id": f"worked_example_{sequence}_{self._digest(example['problem'], example['final_answer'])[:12]}",
            "text": text,
            "metadata": {**metadata, "source_chunk_ids": source_ids or [source.get("chunk_id")]},
            "embedding": source.get("embedding", []),
        }

    @staticmethod
    def _same_context(current: dict[str, Any], row: dict[str, Any]) -> bool:
        current_meta = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        row_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if current_meta.get("chapter") != row_meta.get("chapter"):
            return False
        current_topic = current_meta.get("topic") or current_meta.get("section")
        row_topic = row_meta.get("topic") or row_meta.get("section")
        return not current_topic or not row_topic or current_topic == row_topic

    @staticmethod
    def _problem(text: str) -> str:
        for sentence in split_sentences(text):
            if QUESTION_RE.search(sentence) or "?" in sentence:
                return sentence.strip()
        for sentence in split_sentences(text):
            if SIGNAL_RE.search(sentence):
                return sentence.strip()
        return ""

    @staticmethod
    def _steps(text: str) -> list[str]:
        sentences = split_sentences(text)
        steps = []
        for sentence in sentences:
            lowered = sentence.lower()
            if ANSWER_RE.search(sentence) or any(marker in lowered for marker in ("first", "next", "then", "finally", "we get", "we find", "substitute")):
                steps.append(sentence.strip())
        if not steps:
            steps = [match.group(1).strip() for match in STEP_RE.finditer(text)][:4]
        return list(dict.fromkeys(steps))

    @staticmethod
    def _final_answer(text: str) -> str:
        sentences = split_sentences(text)
        for sentence in reversed(sentences):
            lowered = sentence.lower()
            if any(marker in lowered for marker in ("therefore", "hence", "answer", "final", "we get", "we find")) or "=" in sentence:
                return sentence.strip()
        return sentences[-1].strip() if sentences else ""

    @staticmethod
    def _approach(text: str, concepts: list[str]) -> str:
        if concepts:
            return f"Use {', '.join(concepts[:3])} and follow the relation shown in the problem."
        if "=" in text:
            return "Identify the known quantities, write the relation, substitute the values, and simplify."
        return "Read the problem, identify the concept, and use the explanation to reason step by step."

    @staticmethod
    def _explanation(text: str, steps: list[str], final_answer: str) -> str:
        explanatory = [
            sentence
            for sentence in split_sentences(text)
            if any(marker in sentence.lower() for marker in ("because", "therefore", "hence", "means", "reason", "relationship", "for example"))
        ]
        selected = explanatory[:3] or steps[:3]
        if final_answer and final_answer not in selected:
            selected.append(final_answer)
        return " ".join(selected)

    @staticmethod
    def _problem_type(text: str) -> str:
        lowered = text.lower()
        if "=" in text or any(term in lowered for term in ("calculate", "find", "value", "area", "ratio", "proportion")):
            return "calculation"
        if any(term in lowered for term in ("why", "explain", "reason")):
            return "explanation"
        return "concept_application"

    @staticmethod
    def _difficulty(text: str) -> str:
        words = len(WORD_RE.findall(text))
        if words < 80:
            return "easy"
        if words < 220:
            return "medium"
        return "hard"

    @staticmethod
    def _concepts(text: str) -> list[str]:
        stop = {
            "this",
            "that",
            "with",
            "from",
            "what",
            "when",
            "where",
            "which",
            "there",
            "their",
            "example",
            "problem",
            "solution",
            "answer",
        }
        counts = Counter(token.lower() for token in WORD_RE.findall(text) if len(token) >= 4 and token.lower() not in stop and not token.isdigit())
        return [term.title() for term, _count in counts.most_common(8)]

    @staticmethod
    def _concept_pool(rows: list[dict[str, Any]]) -> int:
        concepts = set()
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if metadata.get("content_type") in {"concept", "example", "worked_example"}:
                concepts.update(WorkedExampleBuilder._concepts(str(row.get("text") or ""))[:4])
        return len(concepts)

    @staticmethod
    def _digest(left: str, right: str) -> str:
        import hashlib

        return hashlib.sha256(f"{left}|{right}".lower().encode("utf-8")).hexdigest()

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if not total:
            return 100.0
        return round(100.0 * value / total, 2)


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]
