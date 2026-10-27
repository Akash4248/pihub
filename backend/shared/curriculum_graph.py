"""
Curriculum graph structure for educational metadata.

Supports intelligent retrieval via hierarchical curriculum relationships:

Grade → Subject → Chapter → Topic → Concept

This enables:
- Curriculum-aware filtering
- Enrichment guidance
- Offline pack organization
- Adaptive tutoring support
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.text_normalization import normalize_curriculum_name


_ENRICHMENT_FAMILIES: dict[str, dict[str, list[str]]] = {
    "statistics": {
        "concepts": ["mean", "median", "mode", "average", "frequency", "dataset", "graph"],
        "glossary_terms": ["mean", "median", "mode", "average", "frequency", "dataset", "data handling", "graph"],
        "synonyms": ["data handling and presentation", "data handling", "data presentation", "statistics"],
        "topic_aliases": ["statistics", "data handling", "data handling and presentation", "data presentation"],
        "educational_keywords": ["mean", "median", "mode", "average", "dataset", "frequency", "data", "graph"],
    },
    "arithmetic progressions": {
        "concepts": ["common difference", "nth term", "sequence", "ap", "progression"],
        "glossary_terms": ["common difference", "nth term", "sequence", "arithmetic progression", "ap"],
        "synonyms": ["arithmetic progression", "arithmetic progressions", "ap", "sequence"],
        "topic_aliases": ["arithmetic progression", "arithmetic progressions", "sequence"],
        "educational_keywords": ["common difference", "nth term", "sequence", "ap", "progression"],
    },
    "triangles": {
        "concepts": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "glossary_terms": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "synonyms": ["triangles", "similarity", "congruence"],
        "topic_aliases": ["similar triangles", "congruent triangles"],
        "educational_keywords": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
    },
    "surface areas and volumes": {
        "concepts": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
        "glossary_terms": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
        "synonyms": ["surface areas and volumes", "solid shapes", "mensuration"],
        "topic_aliases": ["surface area", "volume", "solid shapes"],
        "educational_keywords": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
    },
}


def _normalize_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = normalize_curriculum_name(str(value)).strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _extract_term_fragments(*parts: str, limit: int = 16) -> list[str]:
    fragments: list[str] = []
    for part in parts:
        normalized = normalize_curriculum_name(str(part)).strip().lower()
        if not normalized:
            continue
        tokens = [token for token in re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF']+", normalized) if token]
        for size in (3, 2, 1):
            for index in range(0, max(len(tokens) - size + 1, 0)):
                term = " ".join(tokens[index : index + size]).strip()
                if term and term not in fragments:
                    fragments.append(term)
                if len(fragments) >= limit:
                    return fragments[:limit]
    return fragments[:limit]


def build_chapter_enrichment(
    chapter_name: str,
    subject_name: str | None = None,
    description: str = "",
    topics: list[str] | None = None,
    concepts: list[str] | None = None,
    learning_outcomes: list[str] | None = None,
) -> dict[str, list[str]]:
    """Build a canonical enrichment bundle for a chapter."""
    topics = topics or []
    concepts = concepts or []
    learning_outcomes = learning_outcomes or []

    base_text = " ".join([chapter_name, subject_name or "", description, " ".join(topics), " ".join(concepts), " ".join(learning_outcomes)])
    normalized_base = normalize_curriculum_name(base_text).lower()

    family: str | None = None
    family_aliases = {
        "statistics": ["statistics", "data handling", "data handling and presentation", "data presentation", "frequency", "mean", "median", "mode", "dataset"],
        "arithmetic progressions": ["arithmetic progression", "arithmetic progressions", "common difference", "nth term", "sequence", "ap"],
        "triangles": ["triangles", "similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "surface areas and volumes": ["surface areas and volumes", "cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
    }
    for name, markers in family_aliases.items():
        if any(marker in normalized_base for marker in markers):
            family = name
            break

    enrichment: dict[str, list[str]] = {
        "concepts": [],
        "glossary_terms": [],
        "synonyms": [],
        "topic_aliases": [],
        "educational_keywords": [],
    }

    if family and family in _ENRICHMENT_FAMILIES:
        for key, values in _ENRICHMENT_FAMILIES[family].items():
            enrichment[key].extend(values)

    enrichment["concepts"].extend(concepts)
    enrichment["glossary_terms"].extend(concepts)
    enrichment["topic_aliases"].extend(topics)
    enrichment["educational_keywords"].extend(_extract_term_fragments(chapter_name, description, " ".join(topics), " ".join(concepts), " ".join(learning_outcomes)))
    enrichment["synonyms"].extend(_extract_term_fragments(chapter_name, subject_name or "", description))

    if not enrichment["concepts"]:
        enrichment["concepts"].extend(_extract_term_fragments(chapter_name, description, " ".join(topics), " ".join(concepts)))
    if not enrichment["glossary_terms"]:
        enrichment["glossary_terms"].extend(enrichment["concepts"])
    if not enrichment["topic_aliases"]:
        enrichment["topic_aliases"].extend(_extract_term_fragments(chapter_name, description))

    for key, values in enrichment.items():
        enrichment[key] = _normalize_terms(values)
    return enrichment


@dataclass
class Concept:
    """Atomic educational concept."""
    
    name: str
    description: str = ""
    level: int = 1  # Complexity level 1-10
    prerequisites: list[str] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "level": self.level,
            "prerequisites": self.prerequisites,
            "related_topics": self.related_topics,
        }


@dataclass
class Topic:
    """Educational topic within a chapter."""
    
    name: str
    description: str = ""
    concepts: list[Concept] = field(default_factory=list)
    duration_minutes: int = 30
    learning_objectives: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "concepts": [c.to_dict() for c in self.concepts],
            "duration_minutes": self.duration_minutes,
            "learning_objectives": self.learning_objectives,
        }


@dataclass
class Chapter:
    """Chapter within a curriculum subject."""
    
    name: str
    number: int
    description: str = ""
    topics: list[Topic] = field(default_factory=list)
    learning_outcomes: list[str] = field(default_factory=list)
    enrichment: dict[str, list[str]] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "number": self.number,
            "description": self.description,
            "topics": [t.to_dict() for t in self.topics],
            "learning_outcomes": self.learning_outcomes,
            "enrichment": self.enrichment,
        }


@dataclass
class Subject:
    """Subject within a grade."""
    
    name: str
    description: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    total_hours: float = 0.0
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "chapters": [c.to_dict() for c in self.chapters],
            "total_hours": self.total_hours,
        }


@dataclass
class Grade:
    """Grade level with multiple subjects."""
    
    level: int
    name: str = ""
    subjects: list[Subject] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.name:
            self.name = f"Class {self.level}" if self.level <= 12 else f"Grade {self.level}"
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "name": self.name,
            "subjects": [s.to_dict() for s in self.subjects],
        }


class CurriculumGraph:
    """
    Main curriculum hierarchy manager.
    
    Supports operations like:
    - Query by grade/subject/chapter
    - Find enrichment recommendations
    - Generate offline packs
    - Track learning progression
    """
    
    def __init__(self):
        self.grades: dict[int, Grade] = {}

    @staticmethod
    def _normalize(text: str) -> str:
        return normalize_curriculum_name(text)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF']+", text.lower())
            if token
        }

    def infer_subject_for_query(self, query: str) -> str | None:
        query_text = self._normalize(query)
        subject_keywords = {
            "maths": ["algebra", "geometry", "arithmetic", "equation", "fraction", "ratio", "percent"],
            "science": ["photosynthesis", "respiration", "friction", "force", "motion", "cell", "plant", "animal"],
            "social": ["democracy", "history", "geography", "civics", "government", "constitution"],
            "english": ["grammar", "poem", "story", "reading", "writing", "language"],
        }
        for subject, keywords in subject_keywords.items():
            if any(keyword in query_text for keyword in keywords):
                return subject
        return None

    def infer_topics_for_query(self, query: str) -> list[str]:
        query_text = self._normalize(query)
        topic_keywords = {
            "photosynthesis": ["photosynthesis", "chlorophyll", "plants"],
            "respiration": ["respiration", "breathing"],
            "algebra": ["algebra", "equation"],
            "friction": ["friction", "motion"],
            "democracy": ["democracy", "election", "government"],
        }
        return [topic for topic, keywords in topic_keywords.items() if any(keyword in query_text for keyword in keywords)]

    def infer_concepts_for_text(self, text: str, limit: int = 6) -> list[str]:
        stop_words = {
            "about", "their", "there", "which", "these", "those", "through", "where", "because", "using",
            "chapter", "exercise", "lesson", "topic", "section", "question", "answer", "plant", "plants",
            "what", "when", "with", "would", "could", "should", "some", "many", "much", "more", "most",
            "this", "that", "then", "than", "them", "they", "have", "were", "been", "being", "does",
            "must", "will", "from", "into", "also", "only", "such", "since", "above", "below", "other",
            "each", "both", "very", "just", "even", "like", "well", "strip", "strips", "together",
            "give", "given", "take", "make", "made", "know", "said", "find", "call", "called", "look",
        }
        concepts: list[str] = []
        for token in self._tokenize(text):
            tok_low = token.lower()
            if len(tok_low) <= 3 or tok_low in stop_words or tok_low.isdigit():
                continue
            if tok_low not in concepts:
                concepts.append(tok_low)
            if len(concepts) >= limit:
                break
        return concepts

    def build_from_chunks(self, chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            metadata = chunk.get("metadata", {})
            grade_value = metadata.get("grade")
            subject_value = metadata.get("subject")
            chapter_value = metadata.get("chapter")
            if grade_value is None or subject_value is None or chapter_value is None:
                continue

            try:
                grade_level = int(grade_value)
            except Exception:
                continue

            grade = self.grades.setdefault(grade_level, Grade(level=grade_level))
            subject_name = str(subject_value)
            subject = next((item for item in grade.subjects if self._normalize(item.name) == self._normalize(subject_name)), None)
            if subject is None:
                subject = Subject(name=subject_name)
                grade.subjects.append(subject)

            chapter_name = str(chapter_value)
            chapter = next((item for item in subject.chapters if self._normalize(item.name) == self._normalize(chapter_name)), None)
            if chapter is None:
                chapter = Chapter(name=chapter_name, number=len(subject.chapters) + 1)
                subject.chapters.append(chapter)

            topic_candidates = list(metadata.get("topics") or [])
            if not topic_candidates:
                topic_candidates = self.infer_topics_for_query(chunk.get("text", ""))
            if not topic_candidates:
                topic_candidates = self.infer_concepts_for_text(chunk.get("text", ""), limit=3)

            concepts = [Concept(name=concept.title()) for concept in self.infer_concepts_for_text(chunk.get("text", ""))]
            for topic_name in topic_candidates:
                topic = next((item for item in chapter.topics if self._normalize(item.name) == self._normalize(str(topic_name))), None)
                if topic is None:
                    topic = Topic(name=str(topic_name), concepts=list(concepts))
                    chapter.topics.append(topic)
                elif not topic.concepts:
                    topic.concepts.extend(concepts)

            chapter.enrichment = build_chapter_enrichment(
                chapter.name,
                subject_name=subject.name,
                description=chapter.description or subject.description,
                topics=[topic.name for topic in chapter.topics],
                concepts=[concept.name for topic in chapter.topics for concept in topic.concepts],
                learning_outcomes=chapter.learning_outcomes,
            )
    
    def add_grade(self, grade: Grade) -> None:
        """Add grade to curriculum."""
        self.grades[grade.level] = grade
    
    def get_grade(self, level: int) -> Grade | None:
        """Get grade by level."""
        return self.grades.get(level)
    
    def get_subject(self, grade: int, subject_name: str) -> Subject | None:
        """Get subject by grade and name."""
        grade_obj = self.grades.get(grade)
        if grade_obj:
            for subject in grade_obj.subjects:
                if self._normalize(subject.name) == self._normalize(subject_name):
                    return subject
        return None
    
    def get_chapter(self, grade: int, subject: str, chapter_num: int) -> Chapter | None:
        """Get chapter by grade, subject, and chapter number."""
        subject_obj = self.get_subject(grade, subject)
        if subject_obj:
            for chapter in subject_obj.chapters:
                if chapter.number == chapter_num:
                    return chapter
        return None
    
    def find_topics(self, grade: int, subject: str) -> list[str]:
        """Find all topics for a grade/subject."""
        subject_obj = self.get_subject(grade, subject)
        if not subject_obj:
            return []
        
        topics = []
        for chapter in subject_obj.chapters:
            for topic in chapter.topics:
                topics.append(topic.name)
        return topics
    
    def find_concepts(self, grade: int, subject: str, topic_name: str) -> list[str]:
        """Find all concepts for a specific topic."""
        subject_obj = self.get_subject(grade, subject)
        if not subject_obj:
            return []
        
        concepts = []
        for chapter in subject_obj.chapters:
            for topic in chapter.topics:
                if self._normalize(topic.name) == self._normalize(topic_name):
                    concepts.extend([c.name for c in topic.concepts])
        return concepts
    
    def to_dict(self) -> dict[int, Any]:
        """Export entire curriculum as dict."""
        return {level: grade.to_dict() for level, grade in self.grades.items()}

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CurriculumGraph":
        target = Path(path)
        if not target.exists():
            return create_sample_curriculum()

        raw = json.loads(target.read_text(encoding="utf-8"))
        graph = cls()
        for level_key, grade_value in raw.items():
            grade_level = int(level_key)
            grade = Grade(level=grade_level, name=grade_value.get("name", ""), subjects=[])
            for subject_value in grade_value.get("subjects", []):
                subject = Subject(
                    name=subject_value.get("name", ""),
                    description=subject_value.get("description", ""),
                    total_hours=float(subject_value.get("total_hours", 0.0)),
                    chapters=[],
                )
                for chapter_value in subject_value.get("chapters", []):
                    chapter = Chapter(
                        name=chapter_value.get("name", ""),
                        number=int(chapter_value.get("number", 0)),
                        description=chapter_value.get("description", ""),
                        learning_outcomes=list(chapter_value.get("learning_outcomes", [])),
                        enrichment=dict(chapter_value.get("enrichment", {}) or {}),
                        topics=[],
                    )
                    for topic_value in chapter_value.get("topics", []):
                        topic = Topic(
                            name=topic_value.get("name", ""),
                            description=topic_value.get("description", ""),
                            duration_minutes=int(topic_value.get("duration_minutes", 30)),
                            learning_objectives=list(topic_value.get("learning_objectives", [])),
                            concepts=[],
                        )
                        for concept_value in topic_value.get("concepts", []):
                            topic.concepts.append(
                                Concept(
                                    name=concept_value.get("name", ""),
                                    description=concept_value.get("description", ""),
                                    level=int(concept_value.get("level", 1)),
                                    prerequisites=list(concept_value.get("prerequisites", [])),
                                    related_topics=list(concept_value.get("related_topics", [])),
                                )
                            )
                        chapter.topics.append(topic)
                    subject.chapters.append(chapter)
                grade.subjects.append(subject)
            graph.add_grade(grade)
        return graph


# Pre-built curriculum helpers

def create_sample_curriculum() -> CurriculumGraph:
    """
    Create sample curriculum for Indian CBSE/ICSE standards.
    
    This is a starter template; real curriculum would be loaded from files.
    """
    graph = CurriculumGraph()
    
    # Grade 7 Science example
    science_topics = [
        Topic(
            name="Nutrition in Plants",
            description="Understanding how plants obtain and process nutrients",
            concepts=[
                Concept(name="Photosynthesis", level=3),
                Concept(name="Chlorophyll", level=2),
                Concept(name="Leaf structure", level=2),
            ],
            learning_objectives=[
                "Understand photosynthesis process",
                "Identify parts of a leaf",
                "Explain role of chlorophyll",
            ]
        ),
        Topic(
            name="Nutrition in Animals",
            description="How animals obtain and digest food",
            concepts=[
                Concept(name="Digestive system", level=3),
                Concept(name="Enzymes", level=4),
            ]
        ),
    ]
    
    chapter = Chapter(
        name="Nutrition",
        number=1,
        topics=science_topics,
    )
    
    subject = Subject(
        name="Science",
        chapters=[chapter],
    )
    
    grade = Grade(level=7, subjects=[subject])
    graph.add_grade(grade)

    chapter.enrichment = build_chapter_enrichment(
        chapter.name,
        subject_name=subject.name,
        description=chapter.description,
        topics=[topic.name for topic in chapter.topics],
        concepts=[concept.name for topic in chapter.topics for concept in topic.concepts],
        learning_outcomes=chapter.learning_outcomes,
    )
    
    return graph


if __name__ == "__main__":
    # Test curriculum graph
    graph = create_sample_curriculum()
    
    print("=== Curriculum Graph ===\n")
    
    # Query grade 7
    g7 = graph.get_grade(7)
    print(f"Grade: {g7.name}")
    
    # Query subject
    science = graph.get_subject(7, "Science")
    print(f"Subject: {science.name}")
    print(f"Chapters: {[ch.name for ch in science.chapters]}")
    
    # Query topics
    topics = graph.find_topics(7, "Science")
    print(f"Topics in Grade 7 Science: {topics}")
    
    # Query concepts
    concepts = graph.find_concepts(7, "Science", "Nutrition in Plants")
    print(f"Concepts in 'Nutrition in Plants': {concepts}")
    
    # Export to dict
    print("\n=== Full Curriculum ===")
    import json
    print(json.dumps(graph.to_dict(), indent=2))
