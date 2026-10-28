from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from .concept_models import ConceptType, EducationalConcept
from .educational_concept_validator import EducationalConceptValidator


WORD_RE = re.compile(r"[A-Za-z0-9]+")
FORMULA_RE = re.compile(r"([A-Za-z0-9πθ°%,\s]{1,50}(?:=|<|>|≤|≥|≈|∝)[A-Za-z0-9πθ°%+\-*/×÷^().,\s]{1,100})")
DEFINITION_RE = re.compile(
    r"\b(?P<term>[A-Z]?[A-Za-z][A-Za-z\s-]{2,50}?)\s+(?:is|are|means|refers to|is called|are called)\s+(?P<definition>[^.?!]{20,260}[.?!])",
    re.I,
)
OBJECTIVE_RE = re.compile(r"\b(?:learn|understand|explain|identify|describe|compare|calculate|observe|explore)\b[^.?!]{20,180}[.?!]", re.I)

STOP_TERMS = {
    "activity",
    "chapter",
    "class",
    "curiosity",
    "example",
    "exercise",
    "figure",
    "ganita",
    "grade",
    "image",
    "images",
    "prakash",
    "question",
    "science",
    "table",
    "textbook",
}


def normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def sentence_split(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]


def words(text: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(text or "") if len(token) >= 4 and token.lower() not in STOP_TERMS and not token.isdigit()]


def stable_id(name: str) -> str:
    return hashlib.sha256(normalize(name).encode("utf-8")).hexdigest()[:16]


class EducationalConceptExtractor:
    """Offline heuristic extractor for concepts, definitions, formulas, examples, and objectives."""

    def __init__(self) -> None:
        self.validator = EducationalConceptValidator()

    def extract(self, rows: list[dict[str, Any]], pack_metadata: dict[str, Any] | None = None) -> list[EducationalConcept]:
        pack_metadata = pack_metadata or {}
        buckets: dict[str, dict[str, Any]] = {}
        cooccurrence: dict[str, Counter[str]] = defaultdict(Counter)
        global_counts = Counter()
        for row in rows:
            global_counts.update(words(str(row.get("text") or "")))
        priority_terms = [term for term, _count in global_counts.most_common(35)]
        priority_keys = {normalize(term) for term in priority_terms}

        for row in rows:
            text = str(row.get("text") or "")
            if not text.strip():
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            chunk_id = str(row.get("chunk_id") or "")
            terms = self._candidate_terms(text, metadata, pack_metadata, priority_terms, priority_keys)
            for term in terms:
                bucket = buckets.setdefault(
                    normalize(term),
                    {
                        "name": term.strip().title(),
                        "definitions": [],
                        "examples": [],
                        "worked_examples": [],
                        "formulas": [],
                        "learning_objectives": [],
                        "source_chunk_ids": [],
                        "frequency": 0,
                        "text": "",
                        "metadata": {
                            "grade": metadata.get("grade", pack_metadata.get("grade")),
                            "subject": metadata.get("subject", pack_metadata.get("subject")),
                            "chapter": metadata.get("chapter", pack_metadata.get("chapter")),
                        },
                    },
                )
                bucket["frequency"] += 1
                if len(bucket["text"]) < 2000:
                    bucket["text"] += " " + text[:600]
                if chunk_id and chunk_id not in bucket["source_chunk_ids"]:
                    bucket["source_chunk_ids"].append(chunk_id)
                for other in terms:
                    if normalize(other) != normalize(term):
                        cooccurrence[normalize(term)][other.strip().title()] += 1

            for match in DEFINITION_RE.finditer(text):
                term = self._clean_term(match.group("term"))
                if not term:
                    continue
                bucket = buckets.setdefault(normalize(term), self._empty_bucket(term, row, pack_metadata))
                bucket["frequency"] += 2
                if len(bucket["text"]) < 2000:
                    bucket["text"] += " " + text[:600]
                self._append_unique(bucket["definitions"], match.group(0))

            formulas = [self._clean_formula(item) for item in FORMULA_RE.findall(text)]
            objectives = [item.strip() for item in OBJECTIVE_RE.findall(text)]
            for term in terms[:5]:
                bucket = buckets.setdefault(normalize(term), self._empty_bucket(term, row, pack_metadata))
                bucket["frequency"] += 1
                if len(bucket["text"]) < 2000:
                    bucket["text"] += " " + text[:600]
                for formula in formulas:
                    if formula:
                        self._append_unique(bucket["formulas"], formula)
                for objective in objectives:
                    self._append_unique(bucket["learning_objectives"], objective)
                content_type = str(metadata.get("content_type") or "")
                if content_type == "worked_example" or re.search(r"\b(solution|solved|therefore|hence)\b", text, re.I):
                    self._append_unique(bucket["worked_examples"], text[:1200])
                elif content_type == "example" or re.search(r"\b(example|for example)\b", text, re.I):
                    self._append_unique(bucket["examples"], text[:1000])

        concepts: list[EducationalConcept] = []
        for key, bucket in buckets.items():
            evidence = self._bucket_evidence(bucket)
            validation = self.validator.validate(bucket["name"], evidence)
            if not validation.valid:
                continue
            related = [term for term, _ in cooccurrence[key].most_common(8)]
            definition = bucket["definitions"][0] if bucket["definitions"] else self._definition_from_name(bucket["name"], pack_metadata)
            objectives = bucket["learning_objectives"] or [f"Understand and explain {bucket['name']} in {pack_metadata.get('chapter') or 'this chapter'}."]
            concepts.append(
                EducationalConcept(
                    concept_id=f"concept_{stable_id(bucket['name'])}",
                    name=bucket["name"],
                    concept_type=validation.concept_type,
                    definition=definition,
                    examples=bucket["examples"][:5],
                    worked_examples=bucket["worked_examples"][:5],
                    formulas=bucket["formulas"][:5],
                    learning_objectives=objectives[:5],
                    common_misconceptions=self._misconceptions(bucket["name"]),
                    prerequisites=self._prerequisites(bucket["name"], pack_metadata),
                    related_concepts=related,
                    source_chunk_ids=bucket["source_chunk_ids"][:30],
                    metadata=bucket["metadata"],
                )
            )
        concepts = self._dedupe_concepts(concepts)
        concepts.sort(
            key=lambda concept: (
                3 if concept.concept_type in {ConceptType.DEFINITION, ConceptType.FORMULA, ConceptType.LAW, ConceptType.THEOREM, ConceptType.PROCESS} else 1,
                len(concept.examples) + len(concept.worked_examples) + len(concept.formulas),
                len(concept.source_chunk_ids),
            ),
            reverse=True,
        )
        return concepts[:35]

    def audit(self, concepts: list[EducationalConcept], chapter: str | None = None, subject: str | None = None) -> dict[str, Any]:
        return {
            "chapter": chapter,
            "subject": subject,
            "concept_count": len(concepts),
            "definition_count": sum(1 for concept in concepts if concept.definition),
            "example_count": sum(len(concept.examples) for concept in concepts),
            "worked_example_count": sum(len(concept.worked_examples) for concept in concepts),
            "formula_count": sum(len(concept.formulas) for concept in concepts),
            "learning_objective_count": sum(len(concept.learning_objectives) for concept in concepts),
            "concepts": [concept.model_dump() if hasattr(concept, "model_dump") else concept.dict() for concept in concepts],
        }

    def _candidate_terms(
        self,
        text: str,
        metadata: dict[str, Any],
        pack_metadata: dict[str, Any],
        priority_terms: list[str],
        priority_keys: set[str],
    ) -> list[str]:
        candidates: list[str] = []
        candidates.extend(priority_terms)
        counts = Counter(words(text))
        candidates.extend(term for term, _ in counts.most_common(12))
        cleaned = []
        seen = set()
        for candidate in candidates:
            term = self._clean_term(candidate)
            key = normalize(term)
            if not term or key in seen:
                continue
            # Single-word candidates need corpus-level support. Multiword candidates
            # are kept only when anchored to a high-frequency educational term.
            if len(term.split()) == 1 and key not in priority_keys:
                continue
            if len(term.split()) > 1 and not any(normalize(token) in priority_keys for token in words(term)):
                continue
            if term:
                seen.add(normalize(term))
                cleaned.append(term)
        return cleaned[:20]

    def _nounish_phrases(self, text: str) -> list[str]:
        phrases: list[str] = []
        for match in re.finditer(r"\b([A-Za-z]{4,}(?:\s+(?:of|and|in|to|with|[A-Za-z]{4,})){1,4})\b", text):
            phrase = self._clean_term(match.group(1))
            if phrase and not phrase.lower().startswith(("which ", "there ", "these ", "those ")):
                phrases.append(phrase)
        return phrases[:20]

    def _split_term_phrase(self, value: str) -> list[str]:
        value = re.sub(r"chapter\s+\d+\s*", "", value, flags=re.I)
        parts = re.split(r"[:|,/()-]+", value)
        return [part.strip() for part in parts if 3 < len(part.strip()) <= 60]

    def _clean_term(self, value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9\s-]", " ", str(value or ""))
        value = re.sub(r"\s+", " ", value).strip()
        if not value or value.lower() in STOP_TERMS or value.isdigit():
            return ""
        return value[:60]

    def _clean_formula(self, value: str) -> str:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        return value if 3 <= len(value) <= 120 else ""

    def _empty_bucket(self, term: str, row: dict[str, Any], pack_metadata: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return {
            "name": term.strip().title(),
            "definitions": [],
            "examples": [],
            "worked_examples": [],
            "formulas": [],
            "learning_objectives": [],
            "source_chunk_ids": [str(row.get("chunk_id"))] if row.get("chunk_id") else [],
            "frequency": 1,
            "text": str(row.get("text") or "")[:600],
            "metadata": {
                "grade": metadata.get("grade", pack_metadata.get("grade")),
                "subject": metadata.get("subject", pack_metadata.get("subject")),
                "chapter": metadata.get("chapter", pack_metadata.get("chapter")),
            },
        }

    def _bucket_evidence(self, bucket: dict[str, Any]) -> dict[str, Any]:
        return {
            "frequency": bucket.get("frequency", 0),
            "has_definition": bool(bucket.get("definitions")),
            "has_formula": bool(bucket.get("formulas")),
            "has_example": bool(bucket.get("examples") or bucket.get("worked_examples")),
            "text": bucket.get("text", ""),
        }

    def _dedupe_concepts(self, concepts: list[EducationalConcept]) -> list[EducationalConcept]:
        result: list[EducationalConcept] = []
        seen: set[str] = set()
        for concept in concepts:
            key = normalize(concept.name)
            if key in seen:
                continue
            if any(key != normalize(existing.name) and (key in normalize(existing.name) or normalize(existing.name) in key) for existing in result):
                # Prefer the shorter canonical term unless the longer one has stronger evidence.
                stronger = len(concept.source_chunk_ids) + len(concept.formulas) + len(concept.examples) + len(concept.worked_examples)
                existing_scores = [
                    len(existing.source_chunk_ids) + len(existing.formulas) + len(existing.examples) + len(existing.worked_examples)
                    for existing in result
                    if key in normalize(existing.name) or normalize(existing.name) in key
                ]
                if existing_scores and stronger <= max(existing_scores):
                    continue
            seen.add(key)
            result.append(concept)
        return result

    def _definition_from_name(self, name: str, pack_metadata: dict[str, Any]) -> str:
        chapter = pack_metadata.get("chapter") or "this chapter"
        return f"{name} is an important idea studied in {chapter}."

    def _misconceptions(self, name: str) -> list[str]:
        lowered = name.lower()
        if "force" in lowered:
            return ["Motion does not always require a continuous force."]
        if "proportion" in lowered or "ratio" in lowered:
            return ["Equal differences do not always mean proportional relationships."]
        if "fraction" in lowered:
            return ["A larger denominator does not always mean a larger fraction."]
        return []

    def _prerequisites(self, name: str, metadata: dict[str, Any]) -> list[str]:
        subject = normalize(metadata.get("subject"))
        if "math" in subject:
            return ["number sense", "basic operations"]
        if "science" in subject:
            return ["observation", "measurement"]
        return []

    @staticmethod
    def _append_unique(items: list[str], value: str) -> None:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value not in items:
            items.append(value)
