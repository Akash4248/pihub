from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


BlockType = Literal["heading", "subheading", "paragraph", "table", "formula", "list", "example"]


class StructuredBlock(BaseModel):
    block_id: str
    type: BlockType
    text: str = ""
    level: int = 0
    rows: list[list[str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructuredDocument(BaseModel):
    source_path: str
    title: str = ""
    blocks: list[StructuredBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructuredSection(BaseModel):
    section_id: str
    title: str
    content: str
    formulas: list[str] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    blocks: list[StructuredBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.content.split())
