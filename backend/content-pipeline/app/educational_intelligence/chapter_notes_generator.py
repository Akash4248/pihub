from __future__ import annotations

from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, is_noisy_text, pick_anchor_sentence, sentence_split
from app.educational_intelligence.formula_converter import FormulaConverter
from app.educational_intelligence.inference_client import InferenceClient


class ChapterNotesGenerator:
    """Generate comprehensive chapter notes including core points, formulas,
    experiments, misconceptions, and real-world applications.

    Uses inference-service AI endpoint as the primary method.
    Falls back to extracting key sentences from the content.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()

    async def generate(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        metadata = chunks[0].get("metadata", {}) if chunks else {}
        text_parts: list[str] = []
        concepts: list[str] = []
        for chunk in chunks[:15]:
            t = clean_text(str(chunk.get("text", "")))
            if t:
                text_parts.append(t)
            for c in (chunk.get("metadata", {}).get("concepts") or chunk.get("metadata", {}).get("topics") or []):
                if c not in concepts:
                    concepts.append(c)

        content = "\n\n".join(text_parts)[:10000]
        title = metadata.get("chapter") or "Untitled"

        ai_result = await self.inference.generate_chapter_notes(
            title=title,
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        if ai_result:
            return {
                "chapter_title": ai_result.get("chapter_title", title),
                "one_sentence_summary": ai_result.get("one_sentence_summary", ""),
                "core_points": ai_result.get("core_points", []),
                "important_formulas": ai_result.get("important_formulas", []),
                "experiments": ai_result.get("experiments", []),
                "key_terms": ai_result.get("key_terms", concepts[:8]),
                "misconceptions": ai_result.get("misconceptions", []),
                "real_world_applications": ai_result.get("real_world_applications", []),
                "quiz_focus": ai_result.get("quiz_focus", []),
                "language": metadata.get("language", "en"),
                "generated_by": "inference-service",
            }

        return self._heuristic_generate(text_parts, concepts, metadata)

    def _heuristic_generate(
        self, text_parts: list[str], concepts: list[str], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        all_text = " ".join(text_parts)
        sentences = [s.strip() for s in sentence_split(all_text) if s.strip() and not is_noisy_text(s.strip())]
        core = sentences[:5]
        formulas = [f.latex for f in FormulaConverter().convert_text_formulas(all_text) if f.latex][:5]
        return {
            "chapter_title": metadata.get("chapter") or "Untitled",
            "one_sentence_summary": pick_anchor_sentence(all_text) if sentences else "",
            "core_points": core,
            "important_formulas": formulas,
            "experiments": [s for s in sentences if any(k in s.lower() for k in ("experiment", "activity", "lab", "practical", "observation"))][:3],
            "key_terms": concepts[:8],
            "misconceptions": [],
            "real_world_applications": [s for s in sentences if any(k in s.lower() for k in ("application", "real-world", "example", "used in", "daily life"))][:3],
            "quiz_focus": core[:4],
            "language": metadata.get("language", "en"),
            "generated_by": "heuristic",
        }
