from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}\s+)?([A-Z][A-Za-z0-9,:'’\-\s]{3,90})\s*$")
DEFINITION_RE = re.compile(
    r"\b(?P<term>[A-Z]?[A-Za-z][A-Za-z\s-]{2,55}?)\s+(?:is|are|means|refers to|is called|are called|can be defined as)\s+(?P<definition>[^.?!]{20,320}[.?!])",
    re.I,
)
GLOSSARY_RE = re.compile(r"^\s*(?P<term>[A-Za-z][A-Za-z\s-]{2,50})\s*[:\-]\s*(?P<definition>.{20,260})$", re.M)
OBJECTIVE_RE = re.compile(
    r"\b(?:you will|we will|students will|learn to|understand|explain|identify|describe|compare|calculate|observe|explore)\b[^.?!]{18,220}[.?!]",
    re.I,
)
FORMULA_RE = re.compile(
    r"(?P<formula>(?:[A-Za-z][A-Za-z\s]{0,30}|[A-Za-z])\s*(?:=|<|>|≤|≥|≈|∝)\s*[A-Za-z0-9πθ°%+\-*/×÷^().,\s]{1,110})"
)
INLINE_FORMULA_RE = re.compile(
    r"\b(?P<formula>(?:speed|velocity|acceleration|force|pressure|density|work|power|current|voltage|resistance|area|volume|perimeter)\s*=\s*[^.?!,;]{3,90})",
    re.I,
)


@dataclass
class StructureItem:
    text: str
    source_type: str
    chunk_id: str = ""
    term: str = ""
    definition: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_type": self.source_type,
            "chunk_id": self.chunk_id,
            "term": self.term,
            "definition": self.definition,
            "metadata": self.metadata,
        }


class EducationalStructureParser:
    """Parse textbook-like chunks into educational structures before concept extraction."""

    def parse(self, rows: list[dict[str, Any]], pack_metadata: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        pack_metadata = pack_metadata or {}
        structures: dict[str, list[StructureItem]] = {
            "headings": [],
            "definitions": [],
            "examples": [],
            "worked_examples": [],
            "formulas": [],
            "learning_objectives": [],
            "glossary": [],
            "summary_sections": [],
        }
        for row in rows:
            text = str(row.get("text") or "")
            if not text.strip():
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            chunk_id = str(row.get("chunk_id") or "")
            merged_metadata = {
                "grade": metadata.get("grade", pack_metadata.get("grade")),
                "subject": metadata.get("subject", pack_metadata.get("subject")),
                "chapter": metadata.get("chapter", pack_metadata.get("chapter")),
                "topic": metadata.get("topic", pack_metadata.get("topic")),
            }
            for heading in self._headings(text, metadata, pack_metadata):
                structures["headings"].append(StructureItem(heading, "heading", chunk_id, term=heading, metadata=merged_metadata))
            for match in DEFINITION_RE.finditer(text):
                term = self._clean_term(match.group("term"))
                if term:
                    structures["definitions"].append(
                        StructureItem(match.group(0).strip(), "definition", chunk_id, term=term, definition=match.group("definition").strip(), metadata=merged_metadata)
                    )
            for match in GLOSSARY_RE.finditer(text):
                term = self._clean_term(match.group("term"))
                if term:
                    structures["glossary"].append(
                        StructureItem(match.group(0).strip(), "glossary", chunk_id, term=term, definition=match.group("definition").strip(), metadata=merged_metadata)
                    )
            for formula in self._formulas(text):
                structures["formulas"].append(StructureItem(formula, "formula", chunk_id, term=self._formula_name(formula), metadata=merged_metadata))
            lowered = text.lower()
            if "summary" in lowered[:120] or "what we have learnt" in lowered[:160]:
                structures["summary_sections"].append(StructureItem(text[:1200], "summary", chunk_id, metadata=merged_metadata))
            if "example" in lowered or "for example" in lowered:
                structures["examples"].append(StructureItem(text[:1000], "example", chunk_id, metadata=merged_metadata))
            if any(marker in lowered for marker in ("worked example", "solution", "solved", "therefore", "hence")):
                structures["worked_examples"].append(StructureItem(text[:1200], "worked_example", chunk_id, metadata=merged_metadata))
            for objective in OBJECTIVE_RE.findall(text):
                structures["learning_objectives"].append(StructureItem(objective.strip(), "learning_objective", chunk_id, metadata=merged_metadata))
        return {key: [item.to_dict() for item in values] for key, values in structures.items()}

    def _headings(self, text: str, metadata: dict[str, Any], pack_metadata: dict[str, Any]) -> list[str]:
        headings: list[str] = []
        for key in ("topic", "section"):
            value = metadata.get(key) or pack_metadata.get(key)
            if isinstance(value, str):
                cleaned = self._clean_heading(value)
                if cleaned:
                    headings.append(cleaned)
        for line in text.splitlines()[:8]:
            line = re.sub(r"\s+", " ", line).strip()
            if not line or len(line) > 90:
                continue
            match = HEADING_RE.match(line)
            if match and self._looks_like_heading(line):
                cleaned = self._clean_heading(match.group(1))
                if cleaned:
                    headings.append(cleaned)
        return list(dict.fromkeys(headings))

    def _formulas(self, text: str) -> list[str]:
        formulas = [match.group("formula") for match in FORMULA_RE.finditer(text)]
        formulas.extend(match.group("formula") for match in INLINE_FORMULA_RE.finditer(text))
        cleaned = []
        for formula in formulas:
            value = re.sub(r"\s+", " ", formula).strip(" .,:;")
            if 4 <= len(value) <= 130 and any(symbol in value for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝")):
                cleaned.append(value)
        return list(dict.fromkeys(cleaned))

    @staticmethod
    def _looks_like_heading(line: str) -> bool:
        lower = line.lower()
        if any(marker in lower for marker in ("figure", "table", "page", "exercise", "question", "isbn", "copyright")):
            return False
        words = line.split()
        if len(words) > 9:
            return False
        uppercase_ratio = sum(1 for word in words if word[:1].isupper()) / max(1, len(words))
        return uppercase_ratio >= 0.5

    @staticmethod
    def _clean_heading(value: str) -> str:
        value = re.sub(r"chapter\s+\d+\s*", "", str(value), flags=re.I)
        value = re.sub(r"^\d+(?:\.\d+)*\s*", "", value)
        value = re.sub(r"[^A-Za-z0-9\s,'’:-]", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" :-")
        return value if 4 <= len(value) <= 80 else ""

    @staticmethod
    def _clean_term(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9\s-]", " ", str(value or ""))
        value = re.sub(r"\s+", " ", value).strip()
        return value[:70]

    @staticmethod
    def _formula_name(formula: str) -> str:
        left = re.split(r"=|<|>|≤|≥|≈|∝", formula, maxsplit=1)[0]
        left = re.sub(r"[^A-Za-z0-9\s-]", " ", left)
        left = re.sub(r"\s+", " ", left).strip()
        return left[:50] or "Formula"
