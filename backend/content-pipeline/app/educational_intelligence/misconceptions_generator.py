from __future__ import annotations

from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, sentence_split
from app.educational_intelligence.inference_client import InferenceClient


class MisconceptionsGenerator:
    """Generate common misconceptions and their corrections for a topic.

    Uses inference-service AI endpoint as the primary method.
    Falls back to heuristic extraction from content patterns.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()

    async def generate(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

        ai_result = await self.inference.generate_misconceptions(
            title=title,
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        items_list = (ai_result.get("items") if ai_result else None) or (ai_result.get("misconceptions") if ai_result else None)
        if items_list:
            return [
                {
                    "misconception": clean_text(str(item.get("common_misconception") or item.get("misconception", ""))),
                    "correction": clean_text(str(item.get("correction", ""))),
                    "why_students_confuse_it": clean_text(str(item.get("why_it_occurs") or item.get("why_students_confuse_it", ""))),
                    "language": metadata.get("language", "en"),
                    "generated_by": "inference-service",
                }
                for item in items_list
                if clean_text(str(item.get("common_misconception") or item.get("misconception", "")))
            ]

        return self._heuristic_generate(text_parts, concepts)

    def _heuristic_generate(self, text_parts: list[str], concepts: list[str]) -> list[dict[str, Any]]:
        all_text = " ".join(text_parts)
        sentences = sentence_split(all_text)
        misconceptions = []

        misconception_markers = ["commonly mistaken", "common misconception", "many students think",
                                  "mistaken belief", "incorrectly believe", "often confused"]
        for sentence in sentences:
            lower = sentence.lower()
            for marker in misconception_markers:
                if marker in lower:
                    misconceptions.append({
                        "misconception": sentence.strip()[:200],
                        "correction": "Refer to the chapter explanation for the correct understanding.",
                        "why_students_confuse_it": "The concept sounds similar to everyday experience.",
                        "generated_by": "heuristic",
                    })
                    break

        if not misconceptions and concepts:
            for concept in concepts[:4]:
                misconceptions.append({
                    "misconception": f"Students may think {concept} is the same as related concepts",
                    "correction": f"{concept} has specific meaning in this context",
                    "why_students_confuse_it": "Terms sound similar or overlap with everyday language",
                    "generated_by": "heuristic",
                })

        return misconceptions[:6]
