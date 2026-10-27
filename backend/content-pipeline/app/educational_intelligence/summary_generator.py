from __future__ import annotations

import re
from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, is_noisy_text, pick_anchor_sentence
from app.educational_intelligence.inference_client import InferenceClient
from app.educational_intelligence.multilingual_support import MultilingualSupport


class SummaryGenerator:
    """Generate chapter/topic summaries.

    Uses the inference-service AI endpoint as the primary method.
    Falls back to heuristic sentence extraction when AI is unavailable.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()
        self.multilingual = MultilingualSupport()

    def _sentences(self, text: str) -> list[str]:
        return [segment.strip() for segment in re.split(r"(?<=[.!?।])\s+", text.strip()) if segment.strip()]

    def _collect_focus_text(self, chunks: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for chunk in chunks:
            text = clean_text(str(chunk.get("text", ""))).strip()
            if not text:
                continue
            if chunk.get("metadata", {}).get("chunk_type") in {"definition", "formula", "example", "experiment", "qa", "summary"}:
                parts.append(text)
            elif len(parts) < 6:
                parts.append(text)
        return "\n".join(parts)

    def _heuristic_summary(self, chunks: list[dict[str, Any]], chapter: str | None, topic: str | None) -> dict[str, Any]:
        text = self._collect_focus_text(chunks)
        sentences = [s for s in self._sentences(text) if s and not is_noisy_text(s)]
        summary_sentences = sentences[:4]
        summary = " ".join(summary_sentences).strip()
        if not summary and chunks:
            summary = clean_text(str(chunks[0].get("text", "")))[:240]

        focus_terms: list[str] = []
        for chunk in chunks:
            for term in (chunk.get("metadata", {}).get("topics") or []):
                tc = clean_text(str(term))
                if tc and tc not in focus_terms and not is_noisy_text(tc):
                    focus_terms.append(tc)
            for term in (chunk.get("metadata", {}).get("concepts") or []):
                tc = clean_text(str(term))
                if tc and tc not in focus_terms and not is_noisy_text(tc):
                    focus_terms.append(term)

        revision_notes = [f"Remember: {s}" for s in summary_sentences[:3]]
        profile = self.multilingual.detect_language(text)
        if profile.language == "kn":
            revision_notes = [n.replace("Remember: ", "ನೆನಪಿಡಿ: ") for n in revision_notes]
        elif profile.language == "hi":
            revision_notes = [n.replace("Remember: ", "याद रखें: ") for n in revision_notes]

        return {
            "chapter": chapter or (chunks[0].get("metadata", {}).get("chapter") if chunks else None),
            "topic": topic or (focus_terms[0] if focus_terms else None),
            "language": profile.language,
            "summary": summary,
            "key_points": focus_terms[:8],
            "revision_notes": revision_notes,
            "chunk_count": len(chunks),
            "generated_by": "heuristic",
        }

    async def generate(
        self, chunks: list[dict[str, Any]], chapter: str | None = None, topic: str | None = None
    ) -> dict[str, Any]:
        text = self._collect_focus_text(chunks)
        metadata = chunks[0].get("metadata", {}) if chunks else {}

        title = chapter or metadata.get("chapter") or topic or "Untitled"
        concepts = list(metadata.get("concepts") or metadata.get("topics") or [])
        grade = metadata.get("grade")
        subject = metadata.get("subject")
        language = metadata.get("language")

        ai_result = await self.inference.generate_summary(
            title=title,
            content=text[:8000],
            concepts=concepts[:20],
            grade=grade,
            subject=subject,
            chapter=chapter or metadata.get("chapter"),
            language=language,
        )
        if ai_result:
            key_pts = ai_result.get("keyPoints") or ai_result.get("key_points") or concepts
            return {
                "chapter": chapter or metadata.get("chapter"),
                "topic": topic or (key_pts[:1] if key_pts else None),
                "language": language or "en",
                "summary": ai_result.get("summary") or ai_result.get("overview", ""),
                "key_points": key_pts[:8],
                "important_facts": ai_result.get("importantFacts") or ai_result.get("takeaways", []),
                "revision_notes": [f"Remember: {p}" for p in key_pts[:3]],
                "chunk_count": len(chunks),
                "generated_by": "inference-service",
            }

        return self._heuristic_summary(chunks, chapter, topic)

    def quick_review(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        text = self._collect_focus_text(chunks)
        sentences = [s for s in self._sentences(text) if s and not is_noisy_text(s)]
        summary_sentences = [pick_anchor_sentence(s) for s in sentences[:3] if s]
        summary = " ".join(summary_sentences).strip() if summary_sentences else ""
        if not summary and chunks:
            summary = clean_text(str(chunks[0].get("text", "")))[:200]

        profile = self.multilingual.detect_language(text)
        title = "Quick Review"
        if profile.language == "kn":
            title = "ತ್ವರಿತ ವಿಮರ್ಶೆ"
        elif profile.language == "hi":
            title = "त्वरित समीक्षा"

        return {
            "title": title,
            "summary": summary,
            "bullets": summary_sentences[:3],
        }
