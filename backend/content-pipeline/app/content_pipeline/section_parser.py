from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedSection:
    title: str
    content: str


class SectionParser:
    """Parse educational text into chapter/section-level blocks."""

    HEADING_PATTERNS = [
        re.compile(r"^chapter\s+\d+", re.IGNORECASE),
        re.compile(r"^unit\s+\d+", re.IGNORECASE),
        re.compile(r"^lesson\s+\d+", re.IGNORECASE),
        re.compile(r"^section\s+\d+", re.IGNORECASE),
        re.compile(r"^\d+(\.\d+)+\s+"),
    ]

    def parse(self, text: str) -> list[ParsedSection]:
        lines = [line.rstrip() for line in text.splitlines()]
        sections: list[ParsedSection] = []
        current_title = "Introduction"
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_lines
            content = "\n".join(current_lines).strip()
            if content:
                sections.append(ParsedSection(title=current_title, content=content))
            current_lines = []


        for line in lines:
            stripped = line.strip()
            if not stripped:
                current_lines.append("")
                continue
            if self._is_heading(stripped):
                if current_lines:
                    flush()
                current_title = stripped.lstrip("# ").strip()
            else:
                current_lines.append(stripped)

        if current_lines:
            flush()

        if not sections:
            sections.append(ParsedSection(title="Document", content=text.strip()))
        return sections

    def _is_heading(self, line: str) -> bool:
        if line.startswith("#"):
            return True
        if line.endswith(":") and len(line.split()) <= 12:
            return True
        if line.isupper() and len(line.split()) <= 10:
            return True
        return any(pattern.match(line) for pattern in self.HEADING_PATTERNS)
