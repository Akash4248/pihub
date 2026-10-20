from __future__ import annotations

import re
from typing import Any

from app.educational_intelligence.artifact_cleaning import (
    clean_text,
    dedupe_by_key,
    is_meaningful_term,
    is_noisy_text,
    pick_anchor_sentence,
)
from app.educational_intelligence.inference_client import InferenceClient


class DefinitionExtractor:
    def extract_definitions(self, text: str) -> list[dict[str, str]]:
        matches: list[dict[str, str]] = []
        for line in [segment.strip() for segment in text.splitlines() if segment.strip()]:
            line = clean_text(line)
            patterns = [
                r"^(?P<term>[\w\u0900-\u097F\u0C80-\u0CFF][\w\u0900-\u097F\u0C80-\u0CFF\- ]{1,60})\s*[:\-]\s*(?P<definition>.{10,220})$",
                r"^(?P<term>[\w\u0900-\u097F\u0C80-\u0CFF][\w\u0900-\u097F\u0C80-\u0CFF\- ]{1,60})\s+is\s+(?P<definition>.{10,220})$",
                r"^(?P<term>[\w\u0900-\u097F\u0C80-\u0CFF][\w\u0900-\u097F\u0C80-\u0CFF\- ]{1,60})\s+(?:means|refers to|describes|explains|shows)\s+(?P<definition>.{10,220})$",
            ]
            for pattern in patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    term = clean_text(match.group("term"))
                    definition = clean_text(match.group("definition")).rstrip(".")
                    if term and definition and is_meaningful_term(term) and not is_noisy_text(definition):
                        matches.append({"term": term, "definition": definition})
                    break
        return matches


class FormulaExtractor:
    def extract_formulas(self, text: str) -> list[dict[str, str]]:
        formulas: list[dict[str, str]] = []
        for line in [segment.strip() for segment in text.splitlines() if segment.strip()]:
            line = clean_text(line)
            if re.search(r"[=+\-*/^]", line) and len(line) < 180 and not is_noisy_text(line):
                formulas.append({"formula": line, "definition": "Formula referenced in chapter text"})
        return formulas


class GlossaryExtractor:
    """Extract glossary terms from educational chunks.

    Uses inference-service AI endpoint as the primary method for
    high-quality term-definition extraction. Falls back to regex-based
    heuristic extraction when AI is unavailable.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()
        self.definition_extractor = DefinitionExtractor()
        self.formula_extractor = FormulaExtractor()

    async def _ai_extract(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        text_parts: list[str] = []
        concepts: list[str] = []
        metadata = chunks[0].get("metadata", {}) if chunks else {}
        for chunk in chunks[:10]:
            text = clean_text(str(chunk.get("text", "")))
            if text:
                text_parts.append(text)
            for c in (chunk.get("metadata", {}).get("concepts") or chunk.get("metadata", {}).get("topics") or []):
                if c not in concepts:
                    concepts.append(c)

        content = "\n\n".join(text_parts)[:8000]
        if not content:
            return None

        ai_result = await self.inference.generate_glossary(
            title=metadata.get("chapter") or "Untitled",
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        if ai_result and ai_result.get("items"):
            extracted = [
                {
                    "term": clean_text(str(item.get("term", ""))),
                    "definition": clean_text(str(item.get("definition", ""))),
                    "chapter": metadata.get("chapter"),
                    "subject": metadata.get("subject"),
                    "language": metadata.get("language"),
                    "source": "inference-service",
                }
                for item in ai_result["items"]
                if clean_text(str(item.get("term", "")))
                and clean_text(str(item.get("definition", "")))
                and is_meaningful_term(clean_text(str(item.get("term", ""))))
                and not is_noisy_text(clean_text(str(item.get("term", ""))))
            ]
            if extracted:
                return extracted
        return None

    async def extract(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ai_terms = await self._ai_extract(chunks)
        if ai_terms:
            return ai_terms

        return self._heuristic_extract(chunks)

    def _heuristic_extract(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        terms: dict[str, dict[str, Any]] = {}
        for chunk in chunks:
            text = clean_text(str(chunk.get("text", "")))
            metadata = chunk.get("metadata", {})
            for item in self.definition_extractor.extract_definitions(text):
                term = item["term"]
                terms[term.lower()] = {
                    "term": term,
                    "definition": item["definition"],
                    "chapter": metadata.get("chapter"),
                    "subject": metadata.get("subject"),
                    "language": metadata.get("language"),
                    "source": "definition",
                }
            for item in self.formula_extractor.extract_formulas(text):
                term = item["formula"]
                terms[term.lower()] = {
                    "term": term,
                    "definition": item["definition"],
                    "chapter": metadata.get("chapter"),
                    "subject": metadata.get("subject"),
                    "language": metadata.get("language"),
                    "source": "formula",
                }

            for topic in metadata.get("topics", []):
                key = clean_text(str(topic)).lower()
                if key and key not in terms and is_meaningful_term(key):
                    terms[key] = {
                        "term": clean_text(str(topic)),
                        "definition": self._term_hint(topic, text),
                        "chapter": metadata.get("chapter"),
                        "subject": metadata.get("subject"),
                        "language": metadata.get("language"),
                        "source": "topic_hint",
                    }

        filtered = [
            entry for entry in terms.values()
            if is_meaningful_term(entry.get("term", "")) and not is_noisy_text(entry.get("definition", ""))
        ]
        return dedupe_by_key(filtered, "term")

    def _term_hint(self, term: str, text: str) -> str:
        sentences = [segment.strip() for segment in re.split(r"(?<=[.!?।])\s+", text) if segment.strip()]
        for sentence in sentences:
            if str(term).lower() in sentence.lower():
                return pick_anchor_sentence(sentence, term)
        if re.search(r"[\u0C80-\u0CFF]", f"{term} {text}"):
            return f"{clean_text(str(term))} ಗೆ ಸಂಬಂಧಿಸಿದ ಪ್ರಮುಖ ಪರಿಕಲ್ಪನೆ"
        return f"Key concept related to {clean_text(str(term))}"
