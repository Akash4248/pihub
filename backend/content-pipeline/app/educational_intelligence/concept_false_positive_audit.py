from __future__ import annotations

from typing import Any

from .educational_concept_validator import EducationalConceptValidator


class ConceptFalsePositiveAudit:
    def __init__(self) -> None:
        self.validator = EducationalConceptValidator()

    def audit(self, terms: list[str], evidence_by_term: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        evidence_by_term = evidence_by_term or {}
        rows = []
        seen: set[str] = set()
        for term in terms:
            key = term.lower().strip()
            result = self.validator.validate(term, evidence_by_term.get(key, {}))
            duplicate = key in seen
            seen.add(key)
            rows.append(
                {
                    "term": term,
                    "classification": "false_positive" if duplicate or not result.valid else "valid",
                    "reason": "duplicate concept" if duplicate else result.classification if not result.valid else "valid concept",
                    "validator_reason": result.reason,
                    "concept_type": result.concept_type.value,
                }
            )
        return rows
