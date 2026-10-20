from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from domain.enums import ExperimentCategory, ExperimentDifficulty, ExperimentExecutionMode
from domain.models import (
    ExperimentManifest,
    ExperimentStep,
    ExperimentVariable,
    ExperimentVisualization,
)


class ExperimentDefinition(BaseModel):
    manifest: ExperimentManifest
    category: ExperimentCategory
    version: str = "1.0.0"
    steps: list[ExperimentStep] = Field(default_factory=list)
    variables: list[ExperimentVariable] = Field(default_factory=list)
    visualizations: list[ExperimentVisualization] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentSummary(BaseModel):
    id: str
    title: str
    subject: str
    category: ExperimentCategory | None = None
    chapter: str | None = None
    topic: str | None = None
    difficulty: ExperimentDifficulty
    required_sensors: list[str] = Field(default_factory=list)
    supported_modes: list[ExperimentExecutionMode] = Field(default_factory=list)
    estimated_duration: int | None = None
    description: str


class ExperimentSearchFilters(BaseModel):
    q: str | None = None
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    topic: str | None = None
    difficulty: ExperimentDifficulty | None = None
    required_sensors: list[str] = Field(default_factory=list)
    execution_modes: list[ExperimentExecutionMode] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ExperimentRegistryMetadata(BaseModel):
    total: int
    subjects: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    updated_at: datetime
