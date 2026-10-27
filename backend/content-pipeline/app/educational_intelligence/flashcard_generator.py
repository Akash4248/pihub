from __future__ import annotations

from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, is_meaningful_term
from app.educational_intelligence.glossary_extractor import GlossaryExtractor
from app.educational_intelligence.inference_client import InferenceClient


class FlashcardGenerator:
    """Create revision flashcards from glossary and chapter content.

    Uses inference-service AI endpoint as the primary method for
    generating higher-quality front/back flashcards with varied question
    patterns. Falls back to template-based cards from glossary terms.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()
        self.glossary_extractor = GlossaryExtractor(inference_client=inference_client)

    async def generate(self, chunks: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
        metadata = chunks[0].get("metadata", {}) if chunks else {}
        text_parts: list[str] = []
        concepts: list[str] = []
        for chunk in chunks[:10]:
            t = clean_text(str(chunk.get("text", "")))
            if t:
                text_parts.append(t)
            for c in (chunk.get("metadata", {}).get("concepts") or chunk.get("metadata", {}).get("topics") or []):
                if c not in concepts:
                    concepts.append(c)

        content = "\n\n".join(text_parts)[:8000]
        title = metadata.get("chapter") or "Untitled"

        ai_result = await self.inference.generate_flashcards(
            title=title,
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        items_list = (ai_result.get("items") if ai_result else None) or (ai_result.get("cards") if ai_result else None)
        if items_list:
            return [
                {
                    "front": clean_text(str(item.get("front") or item.get("question", ""))),
                    "back": clean_text(str(item.get("back") or item.get("answer", ""))),
                    "difficulty": item.get("difficulty", "medium"),
                    "chapter": metadata.get("chapter"),
                    "subject": metadata.get("subject"),
                    "generated_by": "inference-service",
                }
                for item in items_list
                if clean_text(str(item.get("front") or item.get("question", "")))
                and clean_text(str(item.get("back") or item.get("answer", "")))
            ][:limit]

        return self._heuristic_generate(chunks, limit)

    def _heuristic_generate(self, chunks: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
        glossary = self.glossary_extractor._heuristic_extract(chunks)
        cards: list[dict[str, Any]] = []
        prompt_templates = [
            "Define {term} and state its key characteristics.",
            "What is the significance of {term} in this chapter?",
            "Explain how {term} is understood and applied.",
            "What is {term}?",
        ]
        for idx, entry in enumerate(glossary):
            term = clean_text(str(entry.get("term") or ""))
            definition = clean_text(str(entry.get("definition") or ""))
            if not term or not definition or not is_meaningful_term(term):
                continue
            template = prompt_templates[idx % len(prompt_templates)]
            cards.append({
                "front": template.format(term=term),
                "back": definition,
                "chapter": entry.get("chapter"),
                "subject": entry.get("subject"),
                "generated_by": "heuristic",
            })
            if len(cards) >= limit:
                break
        return cards
