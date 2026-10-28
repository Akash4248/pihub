from __future__ import annotations

import re
from typing import Any

from .educational_concept_validator import EducationalConceptValidator


class LearningObjectiveExtractor:
    def __init__(self) -> None:
        self.validator = EducationalConceptValidator()

    def extract(self, structures: dict[str, list[dict[str, Any]]], concept_names: list[str]) -> list[dict[str, Any]]:
        objectives = []
        for item in structures.get("learning_objectives", []):
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if not text:
                continue
            related = self._related_concepts(text, concept_names)
            if not related:
                related = self._terms_from_objective(text)
            objectives.append(
                {
                    "objective": text,
                    "related_concepts": related[:8],
                    "chunk_id": item.get("chunk_id", ""),
                }
            )
        return objectives

    def _related_concepts(self, objective: str, concept_names: list[str]) -> list[str]:
        lowered = objective.lower()
        return [name for name in concept_names if name.lower() in lowered or self._token_overlap(name, objective) >= 0.65]

    def _terms_from_objective(self, objective: str) -> list[str]:
        terms = []
        for match in re.finditer(r"\b(?:understand|explain|identify|describe|compare|calculate|observe|explore)\s+([^.;!?]{4,70})", objective, re.I):
            candidate = re.sub(r"\b(?:and|or|the|a|an|to|with|in|of)\b.*$", "", match.group(1), flags=re.I).strip()
            validation = self.validator.validate(candidate, {"frequency": 2, "text": objective})
            if validation.valid:
                terms.append(candidate.title())
        return list(dict.fromkeys(terms))

    @staticmethod
    def _token_overlap(left: str, right: str) -> float:
        left_tokens = {token for token in re.findall(r"[a-z0-9]+", left.lower()) if len(token) >= 4}
        right_tokens = {token for token in re.findall(r"[a-z0-9]+", right.lower()) if len(token) >= 4}
        if not left_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens)
