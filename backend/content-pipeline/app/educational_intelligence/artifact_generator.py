from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from shared.security import get_internal_service_key

from .structured_models import StructuredSection


logger = logging.getLogger(__name__)


class GemmaArtifactGenerator:
    """Generate notebook-style pack artifacts from bounded sections through inference-service."""

    def __init__(self, inference_url: str | None = None, timeout: float = 180.0) -> None:
        self.inference_url = (inference_url or os.getenv("INFERENCE_SERVICE_URL", "http://inference-service:8010")).rstrip("/")
        self.timeout = timeout
        self._cache: dict[tuple[str, str], Any] = {}

    def generate(self, sections: list[StructuredSection], metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx
        except Exception as exc:
            raise RuntimeError(f"httpx is required for content generation: {exc}") from exc

        chapter_notes: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        flashcards: list[dict[str, Any]] = []
        quizzes: list[dict[str, Any]] = []
        glossary_by_term: dict[str, dict[str, Any]] = {}
        misconceptions: list[dict[str, Any]] = []
        applications: list[dict[str, Any]] = []
        objectives: list[dict[str, Any]] = []

        with httpx.Client(timeout=self.timeout) as client:
            for section in sections:
                concepts = self._concept_candidates(section)
                notes = self._normalize_notes(
                    section,
                    self._request_or_raise(client, "chapter-notes", section, metadata, concepts),
                )
                chapter_notes.append(notes)

                summary = self._normalize_summary(
                    section,
                    self._request_or_raise(client, "summary", section, metadata, concepts),
                )
                summaries.append(summary)

                flashcards.extend(self._normalize_flashcards(self._request_or_raise(client, "flashcards", section, metadata, concepts)))
                quizzes.extend(self._normalize_quizzes(self._request_or_raise(client, "quiz", section, metadata, concepts)))
                for item in self._normalize_glossary(self._request_or_raise(client, "glossary", section, metadata, concepts)):
                    term = str(item.get("term") or "").strip().lower()
                    if term and term not in glossary_by_term:
                        glossary_by_term[term] = item
                misconceptions.extend(self._normalize_misconceptions(self._request_or_raise(client, "misconceptions", section, metadata, concepts)))
                applications.extend(self._normalize_applications(self._request_or_raise(client, "applications", section, metadata, concepts)))
                objectives.extend(self._normalize_learning_objectives(self._request_or_raise(client, "learning-objectives", section, metadata, concepts)))

        key_points = [
            {
                "chapter_title": str(item.get("title") or item.get("chapter_title") or "Section"),
                "key_points": [str(value) for value in item.get("keyPoints", [])[:5]],
            }
            for item in summaries
            if isinstance(item, dict)
        ]

        return {
            "chapter_notes": chapter_notes[:80],
            "key_points": key_points[:80],
            "summaries": self._to_existing_summaries(summaries),
            "flashcards": self._to_existing_flashcards(flashcards),
            "quizzes": self._to_existing_quizzes(quizzes),
            "glossary": list(glossary_by_term.values())[:80],
            "misconceptions": misconceptions[:80],
            "applications": applications[:80],
            "learning_objectives": objectives[:80],
            "reports": {
                "gemma_generation": {
                    "section_count": len(sections),
                    "chapter_notes_count": len(chapter_notes),
                    "summary_count": len(summaries),
                    "flashcard_count": len(flashcards),
                    "quiz_count": len(quizzes),
                    "glossary_count": len(glossary_by_term),
                    "misconception_count": len(misconceptions),
                    "application_count": len(applications),
                    "learning_objective_count": len(objectives),
                    "inference_url": self.inference_url,
                }
            },
        }

    def _request_or_raise(
        self,
        client: Any,
        artifact: str,
        section: StructuredSection,
        metadata: dict[str, Any],
        concepts: list[str],
    ) -> Any:
        section_hash = str(section.metadata.get("section_hash") or hashlib.sha256(section.content.encode("utf-8")).hexdigest())
        cache_key = (artifact, section_hash)
        if cache_key in self._cache:
            return self._cache[cache_key]
        payload = {
            "section_id": section.section_id,
            "title": section.title,
            "content": section.content,
            "concepts": concepts,
            "grade": metadata.get("grade"),
            "subject": metadata.get("subject"),
            "chapter": metadata.get("chapter"),
            "language": metadata.get("language"),
        }
        endpoint = f"{self.inference_url}/ai/content/{artifact}"
        headers = {"X-Internal-Service-Key": get_internal_service_key()}
        response = client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        result = data.get("items") if isinstance(data, dict) and "items" in data else data
        self._cache[cache_key] = result
        return result

    @staticmethod
    def _normalize_notes(section: StructuredSection, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("chapter-notes endpoint must return an object")
        chapter_title = str(data.get("chapter_title") or section.title or "Section")
        return {
            "chapter_title": chapter_title,
            "one_sentence_summary": str(data.get("one_sentence_summary") or ""),
            "core_points": [str(value) for value in data.get("core_points", []) if str(value).strip()],
            "important_formulas": [str(value) for value in data.get("important_formulas", []) if str(value).strip()],
            "experiments": [str(value) for value in data.get("experiments", []) if str(value).strip()],
            "key_terms": [str(value) for value in data.get("key_terms", []) if str(value).strip()],
            "misconceptions": [str(value) for value in data.get("misconceptions", []) if str(value).strip()],
            "real_world_applications": [str(value) for value in data.get("real_world_applications", []) if str(value).strip()],
            "quiz_focus": [str(value) for value in data.get("quiz_focus", []) if str(value).strip()],
        }

    @staticmethod
    def _normalize_summary(section: StructuredSection, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("summary endpoint must return an object")
        title = str(data.get("title") or section.title or "Section Summary")
        summary = str(data.get("summary") or "").strip()
        if not summary:
            raise ValueError("summary content is empty")
        return {
            "title": title,
            "summary": summary,
            "keyPoints": [str(value) for value in data.get("keyPoints", []) if str(value).strip()],
            "importantFacts": [str(value) for value in data.get("importantFacts", []) if str(value).strip()],
        }

    @staticmethod
    def _normalize_flashcards(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        flashcards: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question or not answer:
                continue
            key = question.lower()
            if key in seen:
                continue
            seen.add(key)
            flashcards.append({"question": question, "answer": answer, "difficulty": str(item.get("difficulty") or "medium")})
        return flashcards

    @staticmethod
    def _normalize_quizzes(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        quizzes: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            options = [str(value) for value in item.get("options", []) if str(value).strip()]
            answer = str(item.get("answer") or "").strip()
            question = str(item.get("question") or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            if question and answer and len(options) >= 4:
                quizzes.append(
                    {
                        "question": question,
                        "options": options[:4],
                        "answer": answer,
                        "explanation": explanation,
                        "difficulty": str(item.get("difficulty") or "medium"),
                    }
                )
        return quizzes

    @staticmethod
    def _normalize_glossary(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        glossary: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            definition = str(item.get("definition") or "").strip()
            if not term or not definition:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            glossary.append({"term": term, "definition": definition})
        return glossary

    @staticmethod
    def _normalize_misconceptions(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            misconception = str(item.get("misconception") or "").strip()
            correction = str(item.get("correction") or "").strip()
            why = str(item.get("why_students_confuse_it") or "").strip()
            if misconception and correction and why:
                result.append(
                    {
                        "misconception": misconception,
                        "correction": correction,
                        "why_students_confuse_it": why,
                    }
                )
        return result

    @staticmethod
    def _normalize_applications(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            concept = str(item.get("concept") or "").strip()
            real_world_use = str(item.get("real_world_use") or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            if concept and real_world_use and explanation:
                result.append({"concept": concept, "real_world_use": real_world_use, "explanation": explanation})
        return result

    @staticmethod
    def _normalize_learning_objectives(data: Any) -> list[dict[str, Any]]:
        items = GemmaArtifactGenerator._items_from_response(data)
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            objective = str(item.get("objective") or "").strip()
            if objective:
                result.append({"objective": objective})
        return result

    @staticmethod
    def _items_from_response(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [data]
        return []

    @staticmethod
    def _to_existing_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "title": str(item.get("title") or "Section Summary"),
                "text": str(item.get("summary") or ""),
                "key_terms": [str(value) for value in item.get("keyPoints", [])[:12]],
                "important_facts": item.get("importantFacts", []),
            }
            for item in items
            if isinstance(item, dict) and str(item.get("summary") or "").strip()
        ]

    @staticmethod
    def _to_existing_flashcards(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        cards = []
        seen = set()
        for item in items:
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if question and answer and question.lower() not in seen:
                cards.append({"front": question, "back": answer, "difficulty": str(item.get("difficulty") or "medium")})
                seen.add(question.lower())
        return cards[:120]

    @staticmethod
    def _to_existing_quizzes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        quizzes = []
        for item in items:
            options = [str(value) for value in item.get("options", []) if str(value).strip()]
            answer = str(item.get("answer") or "").strip()
            if len(options) >= 4 and answer:
                quizzes.append(
                    {
                        "question": str(item.get("question") or "").strip(),
                        "options": [{"label": chr(ord("A") + idx), "text": text} for idx, text in enumerate(options[:4])],
                        "correct_answer": answer,
                        "answer_label": "A" if options and options[0] == answer else "",
                        "explanation": str(item.get("explanation") or ""),
                        "difficulty": str(item.get("difficulty") or "medium"),
                        "source": "gemma_section_generation",
                    }
                )
        return quizzes[:80]

    @staticmethod
    def _concept_candidates(section: StructuredSection) -> list[str]:
        text = section.content
        candidates = []
        for match in re_find_terms(text):
            lowered = match.lower()
            if lowered not in {item.lower() for item in candidates}:
                candidates.append(match)
            if len(candidates) >= 20:
                break
        return candidates


def re_find_terms(text: str) -> list[str]:
    import re

    phrases = re.findall(r"\b[A-Z][A-Za-z]*(?:\s+[A-Za-z][A-Za-z]*){0,3}\b", text)
    glossary_like = re.findall(r"\b([a-z][a-z]+(?:\s+[a-z][a-z]+){0,2})\s+(?:is|are|means|refers to|defined as)\b", text, re.I)
    values = [*phrases, *glossary_like]
    stop = {"The", "This", "That", "For", "When", "Where", "Activity", "Exercise", "Chapter"}
    return [value.strip() for value in values if value.strip() and value.strip() not in stop and len(value.split()) <= 4]
