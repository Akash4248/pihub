from __future__ import annotations

import hashlib
import re
from typing import Any

from .structured_models import StructuredBlock, StructuredDocument, StructuredSection


WORD_RE = re.compile(r"[A-Za-z0-9]+")
FORMULA_RE = re.compile(r"([A-Za-z][A-Za-z0-9_ ]{0,24}\s*[=∝]\s*[^.;\n]{1,80}|[A-Za-z]\s*=\s*[^.;\n]{1,80})")


class SectionBuilder:
    """Convert structured document blocks into bounded educational sections."""

    def __init__(self, min_words: int = 500, max_words: int = 1500) -> None:
        self.min_words = min_words
        self.max_words = max_words

    def build(self, document: StructuredDocument) -> list[StructuredSection]:
        sections: list[StructuredSection] = []
        current_title = document.title or "Main Section"
        buffer: list[StructuredBlock] = []

        for block in document.blocks:
            if block.type in {"heading", "subheading"} and buffer:
                sections.extend(self._flush(current_title, buffer, document.metadata))
                buffer = []
            if block.type in {"heading", "subheading"}:
                current_title = block.text or current_title
                buffer.append(block)
                continue
            buffer.append(block)
            if self._word_count(buffer) >= self.max_words:
                sections.extend(self._flush(current_title, buffer, document.metadata))
                buffer = []
        if buffer:
            sections.extend(self._flush(current_title, buffer, document.metadata))
        return self._merge_short_sections(sections)

    def _flush(self, title: str, blocks: list[StructuredBlock], metadata: dict[str, Any]) -> list[StructuredSection]:
        chunks: list[list[StructuredBlock]] = []
        current: list[StructuredBlock] = []
        for block in blocks:
            current.append(block)
            if self._word_count(current) >= self.max_words:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)
        return [self._section(title, chunk, metadata, index) for index, chunk in enumerate(chunks, start=1)]

    def _merge_short_sections(self, sections: list[StructuredSection]) -> list[StructuredSection]:
        merged: list[StructuredSection] = []
        for section in sections:
            if merged and section.word_count < self.min_words and merged[-1].word_count + section.word_count <= self.max_words:
                previous = merged[-1]
                blocks = [*previous.blocks, *section.blocks]
                merged[-1] = self._section(previous.title, blocks, {**previous.metadata, **section.metadata}, len(merged))
            else:
                merged.append(section)
        return merged

    def _section(self, title: str, blocks: list[StructuredBlock], metadata: dict[str, Any], index: int) -> StructuredSection:
        content = "\n\n".join(block.text for block in blocks if block.text).strip()
        formulas = [match.group(0).strip() for match in FORMULA_RE.finditer(content)]
        examples = [
            block.text
            for block in blocks
            if re.search(r"\b(example|for example|worked example|solved)\b", block.text, re.I)
        ][:5]
        tables = [block.rows for block in blocks if block.type == "table" and block.rows]
        digest = hashlib.sha256(f"{title}:{index}:{content}".encode("utf-8")).hexdigest()[:12]
        return StructuredSection(
            section_id=f"section_{index}_{digest}",
            title=title.strip() or "Main Section",
            content=content,
            formulas=formulas[:20],
            tables=tables[:10],
            examples=examples,
            blocks=blocks,
            metadata={**metadata, "section_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()},
        )

    @staticmethod
    def _word_count(blocks: list[StructuredBlock]) -> int:
        return sum(len(WORD_RE.findall(block.text or "")) for block in blocks)
