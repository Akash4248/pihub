from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ConceptType(str, Enum):
    CONCEPT = "CONCEPT"
    DEFINITION = "DEFINITION"
    FORMULA = "FORMULA"
    PRINCIPLE = "PRINCIPLE"
    PROCESS = "PROCESS"
    LAW = "LAW"
    THEOREM = "THEOREM"
    EXAMPLE = "EXAMPLE"


class EducationalConcept(BaseModel):
    concept_id: str
    name: str
    concept_type: ConceptType = ConceptType.CONCEPT
    source_type: str = "frequency_extractor"
    definition: str = ""
    examples: list[str] = Field(default_factory=list)
    worked_examples: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    learning_objectives: list[str] = Field(default_factory=list)
    common_misconceptions: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    related_concepts: list[str] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConceptCoverageReport(BaseModel):
    source_concepts: int
    published_concepts: int
    retained_concepts: int
    coverage_percent: float
    source_definitions: int
    published_definitions: int
    retained_definitions: int
    definition_coverage_percent: float
    source_examples: int
    published_examples: int
    retained_examples: int
    example_coverage_percent: float
    source_formulas: int
    published_formulas: int
    retained_formulas: int
    formula_coverage_percent: float
    source_learning_objectives: int
    published_learning_objectives: int
    retained_learning_objectives: int
    learning_objective_coverage_percent: float
    missing_concepts: list[str] = Field(default_factory=list)
    missing_definitions: list[str] = Field(default_factory=list)
    missing_examples: list[str] = Field(default_factory=list)
    missing_formulas: list[str] = Field(default_factory=list)
    missing_learning_objectives: list[str] = Field(default_factory=list)
