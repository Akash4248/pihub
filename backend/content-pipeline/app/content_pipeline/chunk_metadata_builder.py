from __future__ import annotations

import re
from collections import Counter
from typing import Any

from shared.text_normalization import normalize_curriculum_name, normalize_language_code


class ChunkMetadataBuilder:
    """Build normalized chunk metadata with educational fields."""

    EDUCATIONAL_STOPWORDS = {
        # Core grammar
        "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "and", "or", "for", "with", "by",
        "this", "that", "these", "those", "it", "as", "at", "be", "from", "its", "has", "have", "had", "not",
        "but", "if", "so", "no", "we", "you", "they", "them", "their", "there", "our", "us", "he", "she", "him",
        # Textbook meta-filler
        "what", "how", "why", "when", "where", "which", "who", "whose", "let", "give", "given", "find",
        "take", "use", "using", "used", "other", "also", "one", "two", "three", "first", "second",
        "shown", "show", "shows", "table", "fig", "figure", "following", "called", "know", "see",
        "can", "will", "would", "could", "should", "may", "might", "such", "each", "all", "some",
        "many", "well", "make", "made", "then", "than", "about", "into", "over", "after", "before",
        "reprint", "chapter", "page", "section", "activity", "exercise", "question", "answer",
    }

    def __init__(self) -> None:
        self._yake_extractor = None
        try:
            import yake  # type: ignore
            self._yake_extractor = yake.KeywordExtractor(
                lan="en",
                n=2,
                dedupLim=0.8,
                windowsSize=2,
                top=10,
                features=None,
            )
        except Exception:
            self._yake_extractor = None

    def build(
        self,
        text: str,
        base_metadata: dict[str, Any],
        section_title: str,
        chunk_type: str,
        topic_hint: str | None = None,
    ) -> dict[str, Any]:
        metadata = dict(base_metadata)
        metadata["section"] = normalize_curriculum_name(section_title)
        metadata["chunk_type"] = chunk_type
        metadata.setdefault("topic", normalize_curriculum_name(topic_hint or section_title))
        metadata.setdefault("language", self._infer_language(base_metadata, text))
        if metadata.get("subject"):
            metadata["subject"] = normalize_curriculum_name(str(metadata["subject"]))
        if metadata.get("chapter"):
            metadata["chapter"] = normalize_curriculum_name(str(metadata["chapter"]))
        if metadata.get("language"):
            metadata["language"] = normalize_language_code(str(metadata["language"])) or normalize_curriculum_name(str(metadata["language"]))
        metadata.setdefault("difficulty", self._infer_difficulty(base_metadata))
        metadata.setdefault("keywords", self._extract_keywords(text))
        if "grade" in metadata and "difficulty" not in metadata:
            metadata["difficulty"] = f"grade_{metadata['grade']}"
        return metadata

    def _infer_language(self, base_metadata: dict[str, Any], text: str) -> str:
        explicit = normalize_language_code(str(base_metadata.get("language") or ""))
        if explicit:
            return explicit
        if re.search(r"[\u0C80-\u0CFF]", text):
            return "kn"
        if re.search(r"[\u0900-\u097F]", text):
            return "hi"
        if re.search(r"[\u0B80-\u0BFF]", text):
            return "ta"
        if re.search(r"[\u0C00-\u0C7F]", text):
            return "te"
        if re.search(r"[\u0D00-\u0D7F]", text):
            return "ml"
        return "en"

    def _extract_keywords(self, text: str, limit: int = 8) -> list[str]:
        # Strip code blocks or excessive symbols before keyword extraction
        clean_sample = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        clean_sample = re.sub(r"\$\$.*?\$\$", " ", clean_sample, flags=re.DOTALL)

        # 1. Try statistical keyphrase extraction via YAKE if available
        if self._yake_extractor and len(clean_sample.split()) >= 8:
            try:
                raw_kw = self._yake_extractor.extract_keywords(clean_sample)
                extracted: list[str] = []
                for kw, _ in raw_kw:
                    candidate = kw.strip().lower()
                    if (
                        len(candidate) >= 3
                        and candidate not in self.EDUCATIONAL_STOPWORDS
                        and not candidate.isdigit()
                    ):
                        extracted.append(candidate)
                    if len(extracted) >= limit:
                        break
                if extracted:
                    return extracted
            except Exception:
                pass

        # 2. Multilingual-safe tokenization fallback (supports English, Kannada, Hindi, Tamil, Telugu)
        words = re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF\u0B80-\u0BFF\u0C00-\u0C7F\u0D00-\u0D7F][\w\u0900-\u097F\u0C80-\u0CFF\u0B80-\u0BFF\u0C00-\u0C7F\u0D00-\u0D7F_\-]{2,}", clean_sample.lower())
        filtered = [word for word in words if word not in self.EDUCATIONAL_STOPWORDS and not word.isdigit()]
        counts = Counter(filtered)
        return [word for word, _ in counts.most_common(limit)]

    def _infer_difficulty(self, metadata: dict[str, Any]) -> str:
        grade = metadata.get("grade")
        if grade is None:
            return "grade_unknown"
        return f"grade_{grade}"
