from __future__ import annotations

from typing import Any

from .concept_extractor import normalize
from .concept_models import ConceptCoverageReport, EducationalConcept


class ConceptCoverageValidator:
    def validate(self, source_concepts: list[EducationalConcept], published_artifacts: dict[str, Any]) -> ConceptCoverageReport:
        published_text = self._published_text(published_artifacts)
        published_terms = normalize(published_text)

        source_names = [concept.name for concept in source_concepts]
        retained_names = [name for name in source_names if normalize(name) in published_terms]

        definitions = [concept.name for concept in source_concepts if concept.definition]
        retained_definitions = [concept.name for concept in source_concepts if concept.definition and self._meaningful_overlap(concept.definition, published_text)]

        examples = [concept.name for concept in source_concepts if concept.examples or concept.worked_examples]
        retained_examples = [
            concept.name
            for concept in source_concepts
            if (concept.examples or concept.worked_examples)
            and any(self._meaningful_overlap(example, published_text) for example in [*concept.examples, *concept.worked_examples])
        ]

        formulas = [formula for concept in source_concepts for formula in concept.formulas if self._is_formula_like(formula)]
        retained_formulas = [formula for formula in formulas if normalize(formula) in published_terms or self._formula_overlap(formula, published_text)]

        objectives = [objective for concept in source_concepts for objective in concept.learning_objectives]
        retained_objectives = [objective for objective in objectives if self._meaningful_overlap(objective, published_text)]

        return ConceptCoverageReport(
            source_concepts=len(source_names),
            published_concepts=len(set(retained_names)),
            retained_concepts=len(set(retained_names)),
            coverage_percent=self._percent(len(set(retained_names)), len(set(source_names))),
            source_definitions=len(definitions),
            published_definitions=len(set(retained_definitions)),
            retained_definitions=len(set(retained_definitions)),
            definition_coverage_percent=self._percent(len(set(retained_definitions)), len(set(definitions))),
            source_examples=len(examples),
            published_examples=len(set(retained_examples)),
            retained_examples=len(set(retained_examples)),
            example_coverage_percent=self._percent(len(set(retained_examples)), len(set(examples))),
            source_formulas=len(formulas),
            published_formulas=len(retained_formulas),
            retained_formulas=len(retained_formulas),
            formula_coverage_percent=self._percent(len(retained_formulas), len(formulas)),
            source_learning_objectives=len(objectives),
            published_learning_objectives=len(retained_objectives),
            retained_learning_objectives=len(retained_objectives),
            learning_objective_coverage_percent=self._percent(len(retained_objectives), len(objectives)),
            missing_concepts=sorted(set(source_names) - set(retained_names))[:40],
            missing_definitions=sorted(set(definitions) - set(retained_definitions))[:40],
            missing_examples=sorted(set(examples) - set(retained_examples))[:40],
            missing_formulas=sorted(set(formulas) - set(retained_formulas))[:40],
            missing_learning_objectives=sorted(set(objectives) - set(retained_objectives))[:40],
        )

    def _published_text(self, artifacts: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("content", "concepts", "examples", "worked_examples", "formulas", "tutor_contexts", "summaries", "glossary", "quizzes", "flashcards"):
            value = artifacts.get(key, [])
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        parts.extend(str(item.get(field) or "") for field in ("text", "definition", "question", "correct_answer", "explanation", "front", "back"))
                        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                        package = metadata.get("tutor_context_package")
                        if isinstance(package, dict):
                            parts.extend(
                                str(package.get(field) or "")
                                for field in ("concept", "definition", "explanation", "example", "why_it_matters")
                            )
                            for field in (
                                "worked_examples",
                                "prerequisites",
                                "related_concepts",
                                "common_misconceptions",
                                "real_world_applications",
                                "learning_objectives",
                            ):
                                values = package.get(field) or []
                                if isinstance(values, list):
                                    parts.extend(str(value) for value in values)
                        for formula_info in metadata.get("formula_intelligence") or []:
                            if isinstance(formula_info, dict):
                                parts.extend(str(formula_info.get(field) or "") for field in ("formula", "meaning", "explanation", "example"))
                        for formula_info in metadata.get("formula_context") or []:
                            if isinstance(formula_info, dict):
                                parts.extend(str(formula_info.get(field) or "") for field in ("formula", "meaning", "explanation", "example"))
                    else:
                        parts.append(str(item))
        return " ".join(parts)

    @staticmethod
    def _meaningful_overlap(source: str, published: str) -> bool:
        source_terms = {term for term in normalize(source).split() if len(term) >= 5}
        if not source_terms:
            return True
        published_terms = set(normalize(published).split())
        return len(source_terms & published_terms) / max(1, min(len(source_terms), 12)) >= 0.35

    @staticmethod
    def _formula_overlap(source: str, published: str) -> bool:
        source_norm = normalize(source)
        published_norm = normalize(published)
        if source_norm in published_norm:
            return True
        source_terms = {term for term in source_norm.split() if len(term) >= 2}
        if not source_terms:
            return True
        published_terms = set(published_norm.split())
        symbolic = any(symbol in str(source) for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝", "+", "-", "×", "÷", "/"))
        threshold = 0.3 if symbolic else 0.6
        return len(source_terms & published_terms) / max(1, min(len(source_terms), 10)) >= threshold

    @staticmethod
    def _is_formula_like(value: str) -> bool:
        text = str(value or "")
        if not any(symbol in text for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝", "+", "-", "×", "÷", "/", "^")):
            return False
        return True

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if total == 0:
            return 100.0
        return round(100.0 * value / total, 2)
