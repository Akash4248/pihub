from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from shared.curriculum_graph import build_chapter_enrichment
from shared.text_normalization import normalize_curriculum_name


class ConceptIndex:
    """Source-aware concept -> chapter index.

    The index is populated from curriculum graph content, glossary entries,
    generated concepts, keywords, topics, pack manifests, and chapter metadata.
    Chapter-title matching is only used when concept-based lookup does not yield
    a confident chapter candidate.
    """

    SOURCE_WEIGHTS: dict[str, float] = {
        "glossary_term": 1.0,
        "glossary_definition": 0.95,
        "generated_concept": 0.92,
        "keyword": 0.88,
        "topic": 0.9,
        "manifest_topic": 0.86,
        "manifest_keyword": 0.82,
        "chapter_metadata": 0.05,
        "chapter_description": 0.05,
        "chapter_title": 0.0,
    }

    QUERY_FAMILY_ALIASES: dict[str, list[str]] = {
        "common difference": ["arithmetic progression", "arithmetic progressions"],
        "nth term": ["arithmetic progression", "arithmetic progressions"],
        "mean": ["statistics"],
        "median": ["statistics"],
        "mode": ["statistics"],
        "average": ["statistics"],
        "dataset": ["statistics"],
        "data": ["statistics"],
        "statistic": ["statistics"],
        "similar triangles": ["triangles"],
        "cylinder": ["surface areas and volumes"],
    }

    SUBJECT_ALIASES: dict[str, set[str]] = {
        "maths": {"math", "mathematics"},
        "math": {"maths", "mathematics"},
        "mathematics": {"math", "maths"},
        "science": {"sciences"},
        "social": {"social science", "social studies"},
        "social science": {"social", "social studies"},
        "social studies": {"social", "social science"},
    }

    STOPWORDS: set[str] = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "the",
        "their",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }

    def __init__(self) -> None:
        self._concept_to_chapters: dict[str, dict[str, float]] = defaultdict(dict)
        self._chapter_profiles: dict[str, dict[str, Any]] = {}
        self._chapter_order: dict[str, int] = {}

    def clear(self) -> None:
        self._concept_to_chapters = defaultdict(dict)
        self._chapter_profiles = {}
        self._chapter_order = {}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF']+", text.lower())
            if token
        ]

    @classmethod
    def _normalize_term(cls, term: str) -> str:
        return normalize_curriculum_name(str(term)).strip().lower()

    @classmethod
    def _extract_terms(cls, text: str, limit: int = 24) -> list[str]:
        normalized = cls._normalize_term(text)
        if not normalized:
            return []

        tokens = [token for token in cls._tokenize(normalized) if token not in cls.STOPWORDS]
        if not tokens:
            return []

        terms: list[str] = []

        def add_term(value: str) -> None:
            cleaned = cls._normalize_term(value)
            if not cleaned or cleaned in terms or len(cleaned) <= 2:
                return
            terms.append(cleaned)

        add_term(normalized)

        for size in (3, 2):
            for start in range(0, max(len(tokens) - size + 1, 0)):
                phrase = " ".join(tokens[start : start + size]).strip()
                if len(phrase.split()) == size:
                    add_term(phrase)
                    if len(terms) >= limit:
                        return terms[:limit]

        for token in tokens:
            add_term(token)
            if len(terms) >= limit:
                break

        return terms[:limit]

    def _ensure_profile(
        self,
        chapter_name: str,
        subject_name: str | None = None,
        grade: int | None = None,
        language: str | None = None,
        chapter_order: int | None = None,
    ) -> dict[str, Any]:
        chapter_key = self._normalize_term(chapter_name)
        profile = self._chapter_profiles.get(chapter_key)
        if profile is None:
            profile = {
                "chapter": chapter_key,
                "display_name": chapter_name,
                "subject": self._normalize_term(subject_name or "") if subject_name else None,
                "grade": grade,
                "language": self._normalize_term(language or "") if language else None,
                "sources": defaultdict(dict),
                "order": chapter_order if chapter_order is not None else len(self._chapter_profiles),
            }
            self._chapter_profiles[chapter_key] = profile
        else:
            if subject_name and not profile.get("subject"):
                profile["subject"] = self._normalize_term(subject_name)
            if grade is not None and profile.get("grade") is None:
                profile["grade"] = grade
            if language and not profile.get("language"):
                profile["language"] = self._normalize_term(language)
            if chapter_order is not None and profile.get("order") is None:
                profile["order"] = chapter_order

        if chapter_order is not None:
            self._chapter_order[chapter_key] = chapter_order
        elif chapter_key not in self._chapter_order:
            self._chapter_order[chapter_key] = profile.get("order", len(self._chapter_order))

        return profile

    def _index_term(self, chapter_key: str, term: str, source: str) -> None:
        normalized = self._normalize_term(term)
        if not normalized:
            return

        source_weight = self.SOURCE_WEIGHTS.get(source, 0.5)
        current = self._concept_to_chapters[normalized].get(chapter_key, 0.0)
        if source_weight > current:
            self._concept_to_chapters[normalized][chapter_key] = source_weight

        profile = self._chapter_profiles.get(chapter_key)
        if profile is None:
            return
        source_terms = profile["sources"].setdefault(source, {})
        if source_weight > source_terms.get(normalized, 0.0):
            source_terms[normalized] = source_weight

    def _index_text(self, chapter_key: str, text: str, source: str, limit: int = 24) -> None:
        for term in self._extract_terms(text, limit=limit):
            self._index_term(chapter_key, term, source)

    def _index_values(self, chapter_key: str, values: Iterable[Any], source: str) -> None:
        for value in values:
            if value is None:
                continue
            if isinstance(value, dict):
                self._index_values(chapter_key, value.values(), source)
                continue
            if isinstance(value, (list, tuple, set)):
                self._index_values(chapter_key, value, source)
                continue
            self._index_text(chapter_key, str(value), source)

    def _register_chapter_metadata(
        self,
        chapter_name: str,
        *,
        subject_name: str | None = None,
        grade: int | None = None,
        language: str | None = None,
        chapter_order: int | None = None,
        topic_sources: Iterable[Any] = (),
        concept_sources: Iterable[Any] = (),
        keyword_sources: Iterable[Any] = (),
        enrichment_sources: Iterable[Any] = (),
        metadata_sources: Iterable[Any] = (),
        description_sources: Iterable[Any] = (),
        title_sources: Iterable[Any] = (),
    ) -> None:
        profile = self._ensure_profile(chapter_name, subject_name, grade, language, chapter_order)
        chapter_key = profile["chapter"]

        self._index_values(chapter_key, enrichment_sources, "manifest_topic")
        self._index_values(chapter_key, topic_sources, "topic")
        self._index_values(chapter_key, concept_sources, "generated_concept")
        self._index_values(chapter_key, keyword_sources, "keyword")
        self._index_values(chapter_key, metadata_sources, "chapter_metadata")
        self._index_values(chapter_key, description_sources, "chapter_description")
        self._apply_family_aliases(chapter_key)

    def _chapter_enrichment_for(self, chapter_name: str, subject_name: str | None = None, description: str = "", topics: list[str] | None = None, concepts: list[str] | None = None, learning_outcomes: list[str] | None = None) -> dict[str, list[str]]:
        return build_chapter_enrichment(
            chapter_name,
            subject_name=subject_name,
            description=description,
            topics=topics or [],
            concepts=concepts or [],
            learning_outcomes=learning_outcomes or [],
        )

    @classmethod
    def _subjects_match(cls, left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right:
            return True
        return right in cls.SUBJECT_ALIASES.get(left, set()) or left in cls.SUBJECT_ALIASES.get(right, set())

    def _apply_family_aliases(self, chapter_key: str) -> None:
        profile = self._chapter_profiles.get(chapter_key, {})
        source_terms = profile.get("sources") or {}
        all_terms = set().union(*(set(values.keys()) for values in source_terms.values() if values))

        if all_terms & {"mean", "median", "mode"}:
            self._index_term(chapter_key, "statistics", "keyword")

        if all_terms & {"common difference", "nth term"}:
            self._index_term(chapter_key, "arithmetic progression", "keyword")
            self._index_term(chapter_key, "arithmetic progressions", "keyword")

        if "similar triangles" in all_terms:
            self._index_term(chapter_key, "triangles", "keyword")

        if "cylinder" in all_terms:
            self._index_term(chapter_key, "surface areas and volumes", "keyword")

    def build_from_curriculum(
        self,
        curriculum_graph: Any,
        manifest_paths: Iterable[str | Path] | None = None,
        chunks: list[dict[str, Any]] | None = None,
        glossary_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.clear()

        grades = curriculum_graph.grades or {}
        for grade_key, grade in sorted(grades.items(), key=lambda item: int(getattr(item[1], "level", item[0]))):
            grade_level = getattr(grade, "level", None)
            for subject_index, subject in enumerate(getattr(grade, "subjects", []) or []):
                subject_name = normalize_curriculum_name(getattr(subject, "name", "") or "")
                for chapter_index, chapter in enumerate(getattr(subject, "chapters", []) or []):
                    chapter_name = normalize_curriculum_name(getattr(chapter, "name", "") or "")
                    enrichment = getattr(chapter, "enrichment", None) or self._chapter_enrichment_for(
                        chapter_name,
                        subject_name=subject_name,
                        description=getattr(chapter, "description", "") or getattr(subject, "description", ""),
                        topics=[getattr(topic, "name", "") for topic in getattr(chapter, "topics", []) or []],
                        concepts=[
                            getattr(concept, "name", "")
                            for topic in getattr(chapter, "topics", []) or []
                            for concept in getattr(topic, "concepts", []) or []
                        ],
                        learning_outcomes=list(getattr(chapter, "learning_outcomes", []) or []),
                    )
                    self._register_chapter_metadata(
                        chapter_name,
                        subject_name=subject_name,
                        grade=grade_level,
                        chapter_order=chapter_index + (subject_index * 100),
                        topic_sources=[getattr(topic, "name", "") for topic in getattr(chapter, "topics", []) or []],
                        concept_sources=[
                            getattr(concept, "name", "")
                            for topic in getattr(chapter, "topics", []) or []
                            for concept in getattr(topic, "concepts", []) or []
                        ],
                        enrichment_sources=[enrichment],
                        description_sources=[getattr(chapter, "description", ""), getattr(subject, "description", "")],
                        metadata_sources=[getattr(chapter, "learning_outcomes", []), getattr(subject, "total_hours", None)],
                    )

                    for topic in getattr(chapter, "topics", []) or []:
                        self._index_values(chapter_name, [getattr(topic, "description", "")], "chapter_description")
                        self._index_values(chapter_name, [getattr(topic, "learning_objectives", [])], "chapter_metadata")
                        for concept in getattr(topic, "concepts", []) or []:
                            self._index_values(chapter_name, [getattr(concept, "description", "")], "glossary_definition")
                            self._index_values(chapter_name, [getattr(concept, "prerequisites", [])], "topic")
                            self._index_values(chapter_name, [getattr(concept, "related_topics", [])], "topic")

        for manifest_path in manifest_paths or []:
            self.build_from_manifest(manifest_path)

        if chunks:
            self.build_from_chunks(chunks)
        if glossary_entries:
            self.build_from_glossary_entries(glossary_entries)

    def build_from_manifest(self, manifest_source: str | Path | dict[str, Any]) -> None:
        if isinstance(manifest_source, (str, Path)):
            path = Path(manifest_source)
            if not path.exists():
                return
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return
        else:
            manifest = manifest_source

        curriculum_index = manifest.get("curriculum_index") or {}
        for curriculum_key, curriculum in curriculum_index.items():
            grade = curriculum.get("grade")
            subject = curriculum.get("subject")
            language = curriculum.get("language")
            for chapter_entry in curriculum.get("chapters", []) or []:
                chapter_name = chapter_entry.get("chapter_name") or chapter_entry.get("chapter") or ""
                if not chapter_name:
                    continue
                metadata = chapter_entry.get("metadata") or {}
                enrichment = metadata.get("enrichment") or self._chapter_enrichment_for(
                    chapter_name,
                    subject_name=subject,
                    description=metadata.get("description", ""),
                    topics=list(metadata.get("curriculum_topics") or metadata.get("topics") or []),
                    concepts=list(metadata.get("generated_concepts") or metadata.get("concepts") or []),
                    learning_outcomes=list(metadata.get("learning_objectives") or metadata.get("learning_outcomes") or []),
                )
                self._register_chapter_metadata(
                    chapter_name,
                    subject_name=subject,
                    grade=grade if isinstance(grade, int) or grade is None else None,
                    language=language,
                    chapter_order=chapter_entry.get("sequence"),
                    topic_sources=metadata.get("curriculum_topics") or metadata.get("topics") or [],
                    concept_sources=metadata.get("generated_concepts") or metadata.get("concepts") or [],
                    keyword_sources=metadata.get("keywords") or metadata.get("glossary_terms") or [],
                    enrichment_sources=[enrichment],
                    metadata_sources=[metadata.get("learning_objectives"), metadata.get("learning_outcomes"), metadata.get("summary")],
                    description_sources=[metadata.get("description"), curriculum_key],
                )

        for pack_id, pack_entry in (manifest.get("packs") or {}).items():
            chapter_name = pack_entry.get("chapter") or pack_entry.get("chapter_name") or ""
            if not chapter_name:
                continue
            metadata = pack_entry.get("metadata") or {}
            enrichment = metadata.get("enrichment") or self._chapter_enrichment_for(
                chapter_name,
                subject_name=pack_entry.get("subject"),
                description=metadata.get("description", ""),
                topics=list(metadata.get("curriculum_topics") or metadata.get("topics") or []),
                concepts=list(metadata.get("generated_concepts") or metadata.get("concepts") or []),
                learning_outcomes=list(metadata.get("learning_objectives") or metadata.get("learning_outcomes") or []),
            )
            self._register_chapter_metadata(
                chapter_name,
                subject_name=pack_entry.get("subject"),
                grade=pack_entry.get("grade"),
                language=pack_entry.get("language"),
                topic_sources=metadata.get("curriculum_topics") or metadata.get("topics") or [],
                concept_sources=metadata.get("generated_concepts") or metadata.get("concepts") or [],
                keyword_sources=metadata.get("keywords") or metadata.get("glossary_terms") or [],
                enrichment_sources=[enrichment],
                metadata_sources=[metadata.get("learning_objectives"), metadata.get("learning_outcomes"), metadata.get("summary")],
                description_sources=[metadata.get("description")],
            )

    def build_from_chunks(self, chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            metadata = chunk.get("metadata", {}) or {}
            chapter_name = str(metadata.get("chapter") or "").strip()
            if not chapter_name:
                continue
            enrichment = metadata.get("enrichment") or self._chapter_enrichment_for(
                chapter_name,
                subject_name=str(metadata.get("subject") or "") or None,
                description=str(metadata.get("section") or ""),
                topics=list(metadata.get("topics") or [metadata.get("topic")]),
                concepts=list(metadata.get("concepts") or []),
                learning_outcomes=list(metadata.get("learning_outcomes") or []),
            )
            self._register_chapter_metadata(
                chapter_name,
                subject_name=str(metadata.get("subject") or "") or None,
                grade=metadata.get("grade"),
                language=str(metadata.get("language") or "") or None,
                topic_sources=metadata.get("topics") or [metadata.get("topic")],
                concept_sources=metadata.get("concepts") or [],
                keyword_sources=metadata.get("keywords") or [],
                enrichment_sources=[enrichment],
                metadata_sources=[metadata.get("learning_objectives"), metadata.get("learning_outcomes"), metadata.get("section"), metadata.get("chunk_type")],
                description_sources=[chunk.get("text", "")],
            )

    def build_from_glossary_entries(self, glossary_entries: list[dict[str, Any]]) -> None:
        for entry in glossary_entries:
            chapter_name = str(entry.get("chapter") or "").strip()
            if not chapter_name:
                continue
            enrichment = entry.get("enrichment") or self._chapter_enrichment_for(
                chapter_name,
                subject_name=str(entry.get("subject") or "") or None,
                description=str(entry.get("definition") or ""),
                topics=[entry.get("term")],
                concepts=[entry.get("term")],
                learning_outcomes=[],
            )
            self._register_chapter_metadata(
                chapter_name,
                subject_name=str(entry.get("subject") or "") or None,
                topic_sources=[entry.get("term")],
                concept_sources=[entry.get("term")],
                keyword_sources=[entry.get("term")],
                enrichment_sources=[enrichment],
                metadata_sources=[entry.get("source")],
                description_sources=[entry.get("definition")],
            )

    def route_query_to_chapters(self, query: str, curriculum_graph: Any) -> Tuple[List[str], float, str | None]:
        inferred_subject = curriculum_graph.infer_subject_for_query(query)
        query_terms = self._extract_terms(query, limit=16)
        query_terms.extend([self._normalize_term(topic) for topic in (curriculum_graph.infer_topics_for_query(query) or [])])
        query_terms.extend([self._normalize_term(concept) for concept in (curriculum_graph.infer_concepts_for_text(query, limit=6) or [])])

        # Snapshot the profile maps so a concurrent rebuild (e.g. during
        # ingestion) cannot mutate the dicts while we iterate them.
        chapter_profiles = self._chapter_profiles.copy()
        concept_to_chapters = self._concept_to_chapters.copy()

        search_terms: list[str] = []
        for term in query_terms:
            normalized = self._normalize_term(term)
            if normalized and normalized not in search_terms:
                search_terms.append(normalized)

        chapter_scores: dict[str, float] = defaultdict(float)
        matched_sources: dict[str, set[str]] = defaultdict(set)
        matched_terms: dict[str, set[str]] = defaultdict(set)

        for term in search_terms:
            for alias in self.QUERY_FAMILY_ALIASES.get(term, []) or []:
                alias_term = self._normalize_term(alias)
                if alias_term and alias_term not in search_terms:
                    search_terms.append(alias_term)

        for term in search_terms:
            for chapter_key, source_weight in concept_to_chapters.get(term, {}).items():
                chapter_scores[chapter_key] += source_weight
                matched_terms[chapter_key].add(term)
                profile = chapter_profiles.get(chapter_key, {})
                for source_name, source_terms in (profile.get("sources") or {}).items():
                    if term in source_terms:
                        matched_sources[chapter_key].add(source_name)

        inferred_subject_key = self._normalize_term(inferred_subject or "")
        ranking_scores: dict[str, float] = {}
        for chapter_key, profile in chapter_profiles.items():
            subject_key = self._normalize_term(profile.get("subject") or "")
            subject_bonus = 0.0
            if inferred_subject_key and subject_key:
                if self._subjects_match(subject_key, inferred_subject_key):
                    subject_bonus = 0.35
                else:
                    subject_bonus = -0.12
            ranking_scores[chapter_key] = chapter_scores.get(chapter_key, 0.0) + subject_bonus

        ranked_keys = sorted(
            chapter_profiles.keys(),
            key=lambda chapter_key: (
                -ranking_scores.get(chapter_key, 0.0),
                self._chapter_order.get(chapter_key, 10**6),
                chapter_profiles.get(chapter_key, {}).get("display_name", ""),
            ),
        )

        candidates = [chapter_profiles[key]["display_name"] for key in ranked_keys if key in chapter_profiles and ranking_scores.get(key, 0.0) > 0.0]
        if candidates:
            top_key = ranked_keys[0]
            top_score = ranking_scores.get(top_key, 0.0)
            source_set = matched_sources.get(top_key, set())
            coverage = len(matched_terms.get(top_key, set())) / max(len(search_terms), 1)
            source_bonus = 0.18 if source_set & {"glossary_term", "glossary_definition"} else 0.08 if source_set & {"topic", "keyword", "generated_concept"} else 0.0
            confidence = min(0.98, 0.35 + min(top_score / 4.0, 0.45) + source_bonus + (coverage * 0.25))
            if top_score >= 0.45 or coverage >= 0.3:
                return candidates[:5], confidence, inferred_subject

        query_text = self._normalize_term(query)
        fallback_candidates: list[str] = []
        for chapter_key in ranked_keys:
            title = chapter_profiles.get(chapter_key, {}).get("display_name", "")
            normalized_title = self._normalize_term(title)
            if normalized_title and (normalized_title in query_text or any(token in normalized_title for token in self._tokenize(query_text))):
                fallback_candidates.append(title)

        if fallback_candidates:
            return fallback_candidates[:3], 0.35, inferred_subject

        return [], 0.0, inferred_subject

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept_to_chapters": {term: dict(chapters) for term, chapters in self._concept_to_chapters.items()},
            "chapters": self._chapter_profiles,
        }