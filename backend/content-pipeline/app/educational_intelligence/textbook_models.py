from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TextbookBlockType(str, Enum):
    HEADING = "heading"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"
    DEFINITION = "definition"
    EXAMPLE = "example"
    WORKED_EXAMPLE = "worked_example"
    FORMULA = "formula"
    NOTE = "note"
    IMPORTANT = "important"
    SUMMARY = "summary"
    ACTIVITY = "activity"
    EXERCISE = "exercise"
    TABLE = "table"
    IMAGE_REFERENCE = "image_reference"


class TextbookBlock(BaseModel):
    block_id: str
    type: TextbookBlockType
    text: str = ""
    title: str = ""
    formula: str = ""
    variables: list[dict[str, str]] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextbookSection(BaseModel):
    section_id: str
    title: str
    level: int = 1
    blocks: list[TextbookBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextbookChapter(BaseModel):
    pack_id: str
    title: str
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    language: str | None = None
    sections: list[TextbookSection] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
