from __future__ import annotations

from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class QuizItem(BaseModel):
    id: str = Field(description="Unique ID for the quiz item")
    question: str = Field(description="Question stem")
    options: List[str] = Field(description="4 multiple-choice options (A, B, C, D)")
    correct_answer: str = Field(description="Exact string matching one of the options")
    explanation: str = Field(description="Explanation of why the correct answer is right")
    difficulty: Literal["easy", "medium", "hard"] = Field(default="medium", description="easy, medium, or hard")
    bloom_level: Literal["remembering", "understanding", "applying", "analyzing", "evaluating", "creating"] = Field(
        default="understanding", description="remembering, understanding, applying, analyzing, evaluating, creating"
    )



class QuizSet(BaseModel):
    title: str = Field(description="Quiz title")
    items: List[QuizItem] = Field(description="List of quiz items")


class GlossaryEntry(BaseModel):
    term: str = Field(description="Key concept or term")
    definition: str = Field(description="Clear educational definition")
    context_example: Optional[str] = Field(default=None, description="Example usage or context from textbook")


class GlossarySet(BaseModel):
    terms: List[GlossaryEntry] = Field(description="List of glossary terms")


class FlashcardItem(BaseModel):
    front: str = Field(description="Question or key concept prompt")
    back: str = Field(description="Answer or detailed explanation")
    category: str = Field(default="General", description="Topic or sub-category")


class FlashcardDeck(BaseModel):
    title: str = Field(description="Deck title")
    cards: List[FlashcardItem] = Field(description="List of flashcards")


class ChapterSummary(BaseModel):
    overview: str = Field(description="High-level overview of the chapter")
    key_points: List[str] = Field(description="Bullet points of core concepts")
    takeaways: List[str] = Field(description="Key takeaways for students")


class MisconceptionItem(BaseModel):
    common_misconception: str = Field(description="Common student misconception")
    correction: str = Field(description="Scientifically accurate explanation")
    why_it_occurs: str = Field(description="Why students often fall for this misconception")


class MisconceptionGuide(BaseModel):
    misconceptions: List[MisconceptionItem] = Field(description="List of misconceptions")


class LearningObjectives(BaseModel):
    objectives: List[str] = Field(description="Clear measurable learning outcomes for the chapter")
