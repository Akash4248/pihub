from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..pack_system.manifest_validator import ManifestValidator
from .educational_quality_validator import EducationalQualityValidator
from .glossary_validator import GlossaryValidator
from .quiz_validator import QuizValidator
from .retrieval_validator import RetrievalValidator


@dataclass
class PackValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PackValidator:
    def __init__(self) -> None:
        self.manifest_validator = ManifestValidator()
        self.retrieval_validator = RetrievalValidator()
        self.glossary_validator = GlossaryValidator()
        self.quiz_validator = QuizValidator()
        self.quality_validator = EducationalQualityValidator()

    def validate(self, manifest: dict[str, Any], artifacts: dict[str, Any], quality_scores: dict[str, float] | None = None) -> PackValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        manifest_valid, manifest_errors = self.manifest_validator.validate(manifest)
        if not manifest_valid:
            errors.extend(manifest_errors)

        retrieval_valid, retrieval_errors, retrieval_warnings = self.retrieval_validator.validate(manifest, artifacts)
        if not retrieval_valid:
            errors.extend(retrieval_errors)
        warnings.extend(retrieval_warnings)

        glossary_valid, glossary_errors = self.glossary_validator.validate(artifacts.get("glossary", []))
        if not glossary_valid:
            errors.extend(glossary_errors)

        quiz_valid, quiz_errors = self.quiz_validator.validate(artifacts.get("quizzes", []))
        if not quiz_valid:
            errors.extend(quiz_errors)

        completeness_valid, completeness_errors = self.quality_validator.validate_completeness(manifest, artifacts)
        if not completeness_valid:
            errors.extend(completeness_errors)

        if quality_scores:
            score_valid, score_errors = self.quality_validator.validate_scores(quality_scores)
            if not score_valid:
                errors.extend(score_errors)

        return PackValidationResult(valid=not errors, errors=errors, warnings=warnings)
