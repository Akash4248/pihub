from __future__ import annotations

from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, sentence_split
from app.educational_intelligence.inference_client import InferenceClient


class LearningObjectivesGenerator:
    """Generate learning objectives / outcomes for a chapter or topic.

    Uses inference-service AI endpoint as the primary method.
    Falls back to deriving objectives from key sentences.
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

        ai_result = await self.inference.generate_learning_objectives(
            title=title,
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        items_list = (ai_result.get("items") if ai_result else None) or (ai_result.get("objectives") if ai_result else None)
        if items_list:
            return [
                {
                    "objective": clean_text(str(item.get("objective") if isinstance(item, dict) else item)),
                    "language": metadata.get("language", "en"),
                    "generated_by": "inference-service",
                }
                for item in items_list
                if clean_text(str(item.get("objective") if isinstance(item, dict) else item))
            ]

        return self._heuristic_generate(text_parts, concepts)

    def _heuristic_generate(self, text_parts: list[str], concepts: list[str]) -> list[dict[str, Any]]:
        all_text = " ".join(text_parts)
        sentences = sentence_split(all_text)
        objectives = []

        understand_terms = ["understand", "explain", "describe", "define", "identify", "recognize"]
        for sentence in sentences[:6]:
            lower = sentence.lower()
            for trigger in understand_terms:
                if trigger in lower:
                    objectives.append({"objective": sentence.strip()[:200], "generated_by": "heuristic"})
                    break

        for concept in concepts[:6]:
            obj = f"Understand and explain the concept of {concept}"
            if not any(obj in o.get("objective", "") for o in objectives):
                objectives.append({"objective": obj, "generated_by": "heuristic"})

        if not objectives and sentences:
            for s in sentences[:4]:
                objectives.append({"objective": s.strip()[:200], "generated_by": "heuristic"})

        return objectives[:8]
