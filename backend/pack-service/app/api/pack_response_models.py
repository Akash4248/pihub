from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PackSummaryItem(BaseModel):
    title: str
    text: str
    topic: str | None = None
    chunk_ids: list[str] = Field(default_factory=list)


class GlossaryEntry(BaseModel):
    term: str
    definition: str
    example: str | None = None
    language: str | None = None


class QuizOption(BaseModel):
    label: str
    text: str


class QuizQuestion(BaseModel):
    question: str
    options: list[QuizOption] = Field(default_factory=list)
    correct_answer: str
    explanation: str | None = None
    difficulty: str | None = None


class Flashcard(BaseModel):
    front: str
    back: str
    topic: str | None = None


class EnrichmentResponse(BaseModel):
    related_topics: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    recommended_resources: list[dict[str, Any]] = Field(default_factory=list)


class ManifestResponse(BaseModel):
    pack_id: str
    version: str
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    language: str | None = None
    generated_at: datetime
    checksum: str
    content_checksum: str | None = None
    retrieval_index_version: str
    artifact_counts: dict[str, int] = Field(default_factory=dict)
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    quality_scores: dict[str, float] = Field(default_factory=dict)
    archive_path: str | None = None
    compressed_size_mb: float | None = None
    archive_exists: bool = False



class PackListItem(BaseModel):
    pack_id: str
    version: str
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    language: str | None = None
    checksum: str | None = None
    content_checksum: str | None = None
    artifact_counts: dict[str, int] = Field(default_factory=dict)
    quality_scores: dict[str, float] = Field(default_factory=dict)
    archive_path: str | None = None
    size_bytes: int | None = None
    compressed_size_mb: float | None = None
    archive_exists: bool = False
    manifest_exists: bool = False
    source_manifest_pack_id: str | None = None


class PackPreviewResponse(BaseModel):
    manifest: ManifestResponse
    summaries: list[PackSummaryItem] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    quizzes: list[QuizQuestion] = Field(default_factory=list)
    flashcards: list[Flashcard] = Field(default_factory=list)
    chapter_knowledge: dict[str, Any] = Field(default_factory=dict)
    enrichment: EnrichmentResponse = Field(default_factory=EnrichmentResponse)
    quality_scores: dict[str, float] = Field(default_factory=dict)


class PackValidationReport(BaseModel):
    pack_id: str
    version: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class QualityScoreResponse(BaseModel):
    retrieval_score: float = 0.0
    coverage_score: float = 0.0
    quiz_quality: float = 0.0
    glossary_quality: float = 0.0
    flashcard_quality: float = 0.0
    overall_score: float = 0.0


class BenchmarkResult(BaseModel):
    benchmark_name: str
    total_questions: int
    exact_matches: int
    average_score: float
    report: list[dict[str, Any]] = Field(default_factory=list)
