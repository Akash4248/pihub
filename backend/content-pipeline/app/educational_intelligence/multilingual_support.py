from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from shared.text_normalization import normalize_language_code


@dataclass(frozen=True)
class LanguageProfile:
    language: str
    script: str
    confidence: float


class MultilingualSupport:
    """Small multilingual helpers for educational content."""

    def detect_language(self, text: str) -> LanguageProfile:
        if not text:
            return LanguageProfile(language="en", script="latin", confidence=0.0)

        # Count characters per script
        kn_count = len(re.findall(r"[\u0C80-\u0CFF]", text))
        hi_count = len(re.findall(r"[\u0900-\u097F]", text))
        ta_count = len(re.findall(r"[\u0B80-\u0BFF]", text))
        te_count = len(re.findall(r"[\u0C00-\u0C7F]", text))
        ml_count = len(re.findall(r"[\u0D00-\u0D7F]", text))
        total_letters = len(re.findall(r"\w", text)) or 1

        # Only classify as an Indic language if that script represents a meaningful fraction (>= 15%)
        # or has at least 5 characters when the text is short.
        if kn_count / total_letters >= 0.15 or (total_letters < 50 and kn_count >= 5):
            return LanguageProfile(language="kn", script="kannada", confidence=0.98)
        if hi_count / total_letters >= 0.15 or (total_letters < 50 and hi_count >= 5):
            return LanguageProfile(language="hi", script="devanagari", confidence=0.98)
        if ta_count / total_letters >= 0.15 or (total_letters < 50 and ta_count >= 5):
            return LanguageProfile(language="ta", script="tamil", confidence=0.98)
        if te_count / total_letters >= 0.15 or (total_letters < 50 and te_count >= 5):
            return LanguageProfile(language="te", script="telugu", confidence=0.98)
        if ml_count / total_letters >= 0.15 or (total_letters < 50 and ml_count >= 5):
            return LanguageProfile(language="ml", script="malayalam", confidence=0.98)

        lower = text.lower()
        if any(token in lower for token in [" and ", " the ", " is ", " are "]):
            return LanguageProfile(language="en", script="latin", confidence=0.88)
        return LanguageProfile(language="en", script="latin", confidence=0.5)

    def normalize(self, text: str) -> str:
        return " ".join(text.split())

    def prefer_original_language(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", ""))
        profile = self.detect_language(text)
        enriched = dict(payload)
        enriched["language"] = normalize_language_code(enriched.get("language") or profile.language) or profile.language
        enriched["language_confidence"] = profile.confidence
        return enriched
