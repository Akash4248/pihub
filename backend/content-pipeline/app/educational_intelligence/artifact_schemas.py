from __future__ import annotations

import uuid
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuizItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8], description="Unique ID for the quiz item")
    question: str = Field(description="Question stem")
    options: List[str] = Field(description="4 multiple-choice options (A, B, C, D)")
    correct_answer: str = Field(alias="answer", description="Exact string matching one of the options")
    explanation: str = Field(default="", description="Explanation of why the correct answer is right")
    difficulty: Literal["easy", "medium", "hard"] = Field(default="medium", description="easy, medium, or hard")
    bloom_level: Literal["remembering", "understanding", "applying", "analyzing", "evaluating", "creating"] = Field(
        default="understanding", description="Bloom's taxonomy level"
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Fallback for answer / correct_answer
            if "correct_answer" not in data and "answer" in data:
                data["correct_answer"] = str(data["answer"])
            elif "answer" not in data and "correct_answer" in data:
                data["answer"] = str(data["correct_answer"])
            if not data.get("id"):
                data["id"] = str(uuid.uuid4())[:8]
        return data


class QuizSet(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str = Field(default="Chapter Quiz", description="Quiz title")
    items: List[QuizItem] = Field(default_factory=list, description="List of quiz items")

    @model_validator(mode="before")
    @classmethod
    def _coerce_items(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "items" not in data and "quizzes" in data:
                data["items"] = data["quizzes"]
            elif "items" not in data and "questions" in data:
                data["items"] = data["questions"]
        elif isinstance(data, list):
            data = {"title": "Chapter Quiz", "items": data}
        return data


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    term: str = Field(description="Key concept or term")
    definition: str = Field(description="Clear educational definition")
    context_example: Optional[str] = Field(default=None, description="Example usage or context from textbook")


class GlossarySet(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    terms: List[GlossaryEntry] = Field(default_factory=list, description="List of glossary terms")

    @model_validator(mode="before")
    @classmethod
    def _coerce_terms(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "terms" not in data and "items" in data:
                data["terms"] = data["items"]
        elif isinstance(data, list):
            data = {"terms": data}
        return data


class FlashcardItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    front: str = Field(description="Question or key concept prompt")
    back: str = Field(description="Answer or detailed explanation")
    category: str = Field(default="General", description="Topic or sub-category")
    difficulty: str = Field(default="medium", description="Difficulty level")

    @model_validator(mode="before")
    @classmethod
    def _coerce_front_back(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "front" not in data and "question" in data:
                data["front"] = str(data["question"])
            if "back" not in data and "answer" in data:
                data["back"] = str(data["answer"])
        return data


class FlashcardDeck(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str = Field(default="Revision Cards", description="Deck title")
    cards: List[FlashcardItem] = Field(default_factory=list, description="List of flashcards")

    @model_validator(mode="before")
    @classmethod
    def _coerce_cards(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "cards" not in data and "items" in data:
                data["cards"] = data["items"]
            elif "cards" not in data and "flashcards" in data:
                data["cards"] = data["flashcards"]
        elif isinstance(data, list):
            data = {"title": "Revision Cards", "cards": data}
        return data


class ChapterSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    overview: str = Field(description="High-level overview of the chapter")
    key_points: List[str] = Field(default_factory=list, description="Bullet points of core concepts")
    takeaways: List[str] = Field(default_factory=list, description="Key takeaways or important facts for students")

    @model_validator(mode="before")
    @classmethod
    def _coerce_summary_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "overview" not in data and "summary" in data:
                data["overview"] = str(data["summary"])
            if "key_points" not in data and "keyPoints" in data:
                data["key_points"] = list(data["keyPoints"])
            if "takeaways" not in data:
                if "importantFacts" in data:
                    data["takeaways"] = list(data["importantFacts"])
                elif "revision_notes" in data:
                    data["takeaways"] = list(data["revision_notes"])
        return data


class MisconceptionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    common_misconception: str = Field(description="Common student misconception")
    correction: str = Field(description="Scientifically accurate explanation")
    why_it_occurs: str = Field(default="", description="Why students often fall for this misconception")

    @model_validator(mode="before")
    @classmethod
    def _coerce_misconception(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "common_misconception" not in data and "misconception" in data:
                data["common_misconception"] = str(data["misconception"])
            if "why_it_occurs" not in data and "why_students_confuse_it" in data:
                data["why_it_occurs"] = str(data["why_students_confuse_it"])
        return data


class MisconceptionGuide(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    misconceptions: List[MisconceptionItem] = Field(default_factory=list, description="List of misconceptions")

    @model_validator(mode="before")
    @classmethod
    def _coerce_misconceptions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "misconceptions" not in data and "items" in data:
                data["misconceptions"] = data["items"]
        elif isinstance(data, list):
            data = {"misconceptions": data}
        return data


class LearningObjectives(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    objectives: List[str] = Field(default_factory=list, description="Clear measurable learning outcomes for the chapter")

    @model_validator(mode="before")
    @classmethod
    def _coerce_objectives(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "objectives" not in data and "items" in data:
                data["objectives"] = [
                    item.get("objective") if isinstance(item, dict) else str(item)
                    for item in data["items"]
                    if item
                ]
        elif isinstance(data, list):
            data = {
                "objectives": [
                    item.get("objective") if isinstance(item, dict) else str(item)
                    for item in data
                    if item
                ]
            }
        return data
