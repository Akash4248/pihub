from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, sentence_split
from app.educational_intelligence.image_extractor import ImageExtractor
from app.educational_intelligence.formula_converter import FormulaConverter, format_latex_block
from app.educational_intelligence.inference_client import InferenceClient

logger = logging.getLogger(__name__)


class ContentCategory(Enum):
    SCIENCE = "science"
    MATHEMATICS = "mathematics"
    SOCIAL_SCIENCE = "social_science"
    LANGUAGE = "language"
    GENERIC = "generic"


class ContentDensity(Enum):
    SPARSE = "sparse"
    MODERATE = "moderate"
    DENSE = "dense"


@dataclass
class ContentAnalysis:
    category: ContentCategory = ContentCategory.GENERIC
    density: ContentDensity = ContentDensity.MODERATE
    language: str = "en"
    chunk_count: int = 0
    has_formulas: bool = False
    has_tables: bool = False
    has_definitions: bool = False
    has_examples: bool = False
    has_experiments: bool = False
    key_concepts: list[str] = field(default_factory=list)
    estimated_grade: int | None = None

    def summary(self) -> str:
        return (
            f"Category={self.category.value}, Density={self.density.value}, "
            f"Language={self.language}, Chunks={self.chunk_count}, "
            f"Formulas={self.has_formulas}, Tables={self.has_tables}, "
            f"Definitions={self.has_definitions}, Examples={self.has_examples}, "
            f"Experiments={self.has_experiments}, Concepts={self.key_concepts[:5]}"
        )


@dataclass
class ArtifactCritique:
    artifact_type: str
    score: float
    issues: list[str]
    suggestions: list[str]
    passed: bool


ARTIFACT_CRITIQUE_PROMPT = """You are an educational quality reviewer. Evaluate the following {artifact_type} generated from educational content.

SOURCE CONTENT (first 1500 chars):
{source_context}

GENERATED {artifact_type_upper}:
{artifact_content}

GRADE LEVEL: {grade}

Evaluate on these criteria (score each 0-10):
1. ACCURACY: Does it correctly reflect the source material without hallucination?
2. EDUCATIONAL_VALUE: Does it actually help a student learn?
3. AGE_APPROPRIATENESS: Is it suitable for grade {grade}?
4. COMPLETENESS: Does it cover the key points from the source?
5. CLARITY: Is it clear and well-written?

Return ONLY a JSON object:
{{
  "scores": {{"accuracy": 8, "educational_value": 7, "age_appropriateness": 9, "completeness": 6, "clarity": 8}},
  "issues": ["Issue 1", "Issue 2"],
  "suggestions": ["Suggestion 1", "Suggestion 2"],
  "overall_score": 7.6,
  "passed": true
}}
Threshold for passing: overall_score >= 7.0
"""

ARTIFACT_IMPROVEMENT_PROMPT = """You are generating an improved {artifact_type} for educational content.

SOURCE CONTENT:
{source_context}

GRADE LEVEL: {grade}

PREVIOUS VERSION:
{previous_content}

CRITIQUE OF PREVIOUS VERSION:
{critique_text}

Generate an improved version addressing all the issues above. Return valid JSON matching the expected schema for {artifact_type}. Be more accurate, more educational, and age-appropriate."""


class ContentAnalyzer:
    """Analyze chunks to determine content category, density, and pedagogical features."""

    SCIENCE_KEYWORDS = {
        "experiment", "observation", "hypothesis", "chemical", "reaction",
        "force", "energy", "cell", "organism", "photosynthesis", "respiration",
        "electric", "magnet", "wave", "light", "sound", "heat", "temperature",
        "gravity", "motion", "velocity", "acceleration", "element", "compound",
        "acid", "base", "salt", "metal", "non-metal", "plant", "animal",
        "habitat", "ecosystem", "food chain", "nutrition", "digestion",
        "circulation", "excretion", "reproduction", "evolution",
    }

    MATHS_KEYWORDS = {
        "number", "digit", "place value", "addition", "subtraction",
        "multiplication", "division", "fraction", "decimal", "percentage",
        "ratio", "proportion", "algebra", "equation", "geometry", "shape",
        "angle", "triangle", "circle", "square", "rectangle", "area",
        "perimeter", "volume", "data", "graph", "probability", "statistics",
        "measurement", "length", "weight", "capacity", "time", "money",
        "pattern", "symmetry", "coordinate", "integer",
    }

    SOCIAL_KEYWORDS = {
        "history", "geography", "civics", "political", "economy",
        "constitution", "democracy", "government", "parliament",
        "agriculture", "industry", "transport", "communication",
        "environment", "resource", "population", "settlement",
        "culture", "heritage", "movement", "revolution", "empire",
        "civilization", "kingdom", "republic", "independence",
        "freedom", "rights", "duty", "citizen",
    }

    EXPERIMENT_KEYWORDS = {
        "experiment", "procedure", "observation", "apparatus",
        "material required", "aim", "precaution", "result",
        "inference", "conclusion", "record your observation",
    }

    FORMULA_PATTERN = re.compile(r"[A-Za-z0-9]+\s*[=∝×÷]\s*[A-Za-z0-9\s+\-*/^().]+")
    DEFINITION_PATTERN = re.compile(r"(is called|is known as|refers to|is defined as|means|may be defined as)", re.IGNORECASE)

    def analyze(self, chunks: list[dict[str, Any]]) -> ContentAnalysis:
        all_text = " ".join(
            clean_text(str(c.get("text", ""))) for c in chunks if c.get("text")
        )
        lower_text = all_text.lower()
        metadata = chunks[0].get("metadata", {}) if chunks else {}

        category = self._classify_content(lower_text, metadata)
        density = self._estimate_density(chunks)
        language = metadata.get("language", "en")
        has_formulas = bool(self.FORMULA_PATTERN.findall(all_text))
        has_tables = any(
            chunk.get("metadata", {}).get("chunk_type") == "table"
            or "table" in str(chunk.get("metadata", {}))
            for chunk in chunks
        )
        has_definitions = bool(self.DEFINITION_PATTERN.findall(lower_text))
        has_examples = "example" in lower_text or "for example" in lower_text or "e.g." in lower_text
        has_experiments = any(kw in lower_text for kw in self.EXPERIMENT_KEYWORDS)

        sentences = sentence_split(all_text)
        word_freq: dict[str, int] = {}
        for s in sentences:
            for w in s.lower().split():
                w = w.strip(".,;:!?()[]")
                if len(w) > 3:
                    word_freq[w] = word_freq.get(w, 0) + 1

        concepts = list(metadata.get("concepts") or metadata.get("topics") or [])
        if not concepts:
            top_terms = sorted(word_freq.items(), key=lambda x: -x[1])[:10]
            concepts = [t for t, _ in top_terms if len(t) > 4][:8]

        return ContentAnalysis(
            category=category,
            density=density,
            language=language,
            chunk_count=len(chunks),
            has_formulas=has_formulas,
            has_tables=has_tables,
            has_definitions=has_definitions,
            has_examples=has_examples,
            has_experiments=has_experiments,
            key_concepts=concepts,
            estimated_grade=metadata.get("grade"),
        )

    def _classify_content(self, lower_text: str, metadata: dict) -> ContentCategory:
        subject = (metadata.get("subject") or "").lower()
        if "math" in subject or "mathematics" in subject:
            return ContentCategory.MATHEMATICS
        if "social" in subject or "history" in subject or "geography" in subject or "civics" in subject or "political" in subject:
            return ContentCategory.SOCIAL_SCIENCE
        if "science" in subject or "physics" in subject or "chemistry" in subject or "biology" in subject:
            return ContentCategory.SCIENCE
        if "english" in subject or "hindi" in subject or "kannada" in subject or "language" in subject:
            return ContentCategory.LANGUAGE

        science_score = sum(1 for kw in self.SCIENCE_KEYWORDS if kw in lower_text)
        maths_score = sum(1 for kw in self.MATHS_KEYWORDS if kw in lower_text)
        social_score = sum(1 for kw in self.SOCIAL_KEYWORDS if kw in lower_text)

        if science_score >= maths_score and science_score >= social_score and science_score >= 3:
            return ContentCategory.SCIENCE
        if maths_score >= science_score and maths_score >= social_score and maths_score >= 3:
            return ContentCategory.MATHEMATICS
        if social_score >= science_score and social_score >= maths_score and social_score >= 3:
            return ContentCategory.SOCIAL_SCIENCE
        return ContentCategory.GENERIC

    def _estimate_density(self, chunks: list[dict[str, Any]]) -> ContentDensity:
        total_chars = sum(len(str(c.get("text", ""))) for c in chunks)
        if total_chars < 1000:
            return ContentDensity.SPARSE
        if total_chars < 5000:
            return ContentDensity.MODERATE
        return ContentDensity.DENSE


class GenerationPlanner:
    """Plan artifact generation based on content analysis."""

    SCIENCE_PRIORITY = [
        "summary", "glossary", "chapter_notes", "learning_objectives",
        "misconceptions", "flashcards", "quiz", "applications",
    ]
    MATHS_PRIORITY = [
        "summary", "chapter_notes", "glossary", "flashcards",
        "quiz", "learning_objectives", "misconceptions", "applications",
    ]
    SOCIAL_PRIORITY = [
        "summary", "chapter_notes", "glossary", "learning_objectives",
        "misconceptions", "flashcards", "applications", "quiz",
    ]
    GENERIC_PRIORITY = [
        "summary", "glossary", "chapter_notes", "flashcards",
        "quiz", "learning_objectives", "misconceptions", "applications",
    ]

    @dataclass
    class ArtifactTask:
        artifact_type: str
        priority: int
        depends_on: list[str] = field(default_factory=list)
        max_iterations: int = 2

    def plan(self, analysis: ContentAnalysis) -> list[ArtifactTask]:
        if analysis.category == ContentCategory.SCIENCE:
            order = self.SCIENCE_PRIORITY
            iterations = 3 if analysis.has_experiments else 2
        elif analysis.category == ContentCategory.MATHEMATICS:
            order = self.MATHS_PRIORITY
            iterations = 3 if analysis.has_formulas else 2
        elif analysis.category == ContentCategory.SOCIAL_SCIENCE:
            order = self.SOCIAL_PRIORITY
            iterations = 2
        else:
            order = self.GENERIC_PRIORITY
            iterations = 2

        if analysis.density == ContentDensity.SPARSE:
            order = [a for a in order if a not in ("chapter_notes", "misconceptions")]
            iterations = 1

        tasks = []
        for i, artifact_type in enumerate(order):
            deps = []
            if artifact_type in ("quiz", "flashcards") and "glossary" in order[:i]:
                deps.append("glossary")
            if artifact_type == "applications" and "chapter_notes" in order[:i]:
                deps.append("chapter_notes")
            tasks.append(self.ArtifactTask(
                artifact_type=artifact_type,
                priority=i,
                depends_on=deps,
                max_iterations=iterations,
            ))
        return tasks


class ArtifactCritiquer:
    """Self-critique generated artifacts using the inference service."""

    def __init__(self, inference: InferenceClient):
        self.inference = inference

    async def critique(
        self, artifact_type: str, artifact_content: Any, source_context: str, grade: int | None = None
    ) -> ArtifactCritique:
        content_str = json.dumps(artifact_content, indent=2, ensure_ascii=False)[:2000]
        source_str = source_context[:1500]

        prompt = ARTIFACT_CRITIQUE_PROMPT.format(
            artifact_type=artifact_type.replace("_", " "),
            artifact_type_upper=artifact_type.replace("_", " ").upper(),
            source_context=source_str,
            artifact_content=content_str,
            grade=grade or 6,
        )

        result = await self.inference._generate_text(prompt)
        if not result:
            return ArtifactCritique(
                artifact_type=artifact_type,
                score=7.0,
                issues=[],
                suggestions=[],
                passed=True,
            )

        try:
            data = json.loads(result)
            scores = data.get("scores", {})
            avg_score = (
                sum(scores.values()) / len(scores)
                if scores
                else data.get("overall_score", 7.0)
            )
            return ArtifactCritique(
                artifact_type=artifact_type,
                score=avg_score,
                issues=data.get("issues", []),
                suggestions=data.get("suggestions", []),
                passed=data.get("passed", avg_score >= 7.0),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse critique for %s: %s", artifact_type, result[:100])
            return ArtifactCritique(
                artifact_type=artifact_type,
                score=7.0,
                issues=[],
                suggestions=[],
                passed=True,
            )


class ArtifactAgent:
    """Agentic orchestrator that generates educational artifacts with
    self-critique and improvement cycles.

    Flow for each artifact type:
    1. Generate initial version using the inference service (with subject-aware prompt)
    2. Self-critique the output (accuracy, educational value, completeness)
    3. If score < threshold, regenerate with critique context
    4. Repeat up to max_iterations
    """

    PASS_THRESHOLD = 7.0
    MAX_ITERATIONS = 3

    SUBJECT_SYSTEM_PROMPTS = {
        ContentCategory.SCIENCE: (
            "You are a science textbook expert. Generate accurate, curriculum-aligned "
            "content that explains concepts with precision. Include relevant examples "
            "and real-world connections. Avoid oversimplification of scientific terms."
        ),
        ContentCategory.MATHEMATICS: (
            "You are a mathematics textbook expert. Generate clear, step-by-step "
            "explanations with precise definitions and formulas. Include worked examples "
            "and emphasize understanding over memorization."
        ),
        ContentCategory.SOCIAL_SCIENCE: (
            "You are a social studies textbook expert. Generate content that connects "
            "historical/geographical/political concepts to students' daily lives. "
            "Emphasize cause-effect relationships and civic awareness."
        ),
        ContentCategory.LANGUAGE: (
            "You are a language textbook expert. Generate content that builds "
            "vocabulary, grammar understanding, and communication skills. "
            "Include contextual examples and usage notes."
        ),
        ContentCategory.GENERIC: (
            "You are an educational content expert. Generate clear, grade-appropriate "
            "learning content that builds understanding step by step."
        ),
    }

    MUTLI_TURN_ARTIFACT_PROMPTS: dict[str, str] = {
        "summary": (
            "Generate a {subject_type} summary for grade {grade} students studying {chapter}. "
            "Return JSON matching schema: {{\"overview\": string, \"key_points\": [string], \"takeaways\": [string]}}."
        ),
        "glossary": (
            "Extract 8-15 key terms and definitions for grade {grade} {subject_type} on {chapter}. "
            "Return JSON matching schema: {{\"terms\": [{{\"term\": string, \"definition\": string, \"context_example\": string}}]}}."
        ),
        "chapter_notes": (
            "Generate comprehensive chapter notes for grade {grade} {subject_type} on {chapter}. "
            "Return JSON matching schema: {{\"chapter_title\": string, \"one_sentence_summary\": string, \"core_points\": [string], \"important_formulas\": [string], \"experiments\": [string], \"key_terms\": [string], \"misconceptions\": [string], \"real_world_applications\": [string], \"quiz_focus\": [string]}}."
        ),
        "learning_objectives": (
            "Generate 4-6 measurable learning objectives for grade {grade} {subject_type} on {chapter}. "
            "Return JSON matching schema: {{\"objectives\": [string]}}."
        ),
        "misconceptions": (
            "Identify 3-5 common student misconceptions about {chapter} in {subject_type}. "
            "Return JSON matching schema: {{\"misconceptions\": [{{\"common_misconception\": string, \"correction\": string, \"why_it_occurs\": string}}]}}."
        ),
        "flashcards": (
            "Create 6-10 revision flashcards for grade {grade} {subject_type} on {chapter}. "
            "Return JSON matching schema: {{\"title\": string, \"cards\": [{{\"front\": string, \"back\": string, \"category\": string, \"difficulty\": \"easy|medium|hard\"}}]}}."
        ),
        "quiz": (
            "Create 5-8 quiz questions for grade {grade} {subject_type} on {chapter}. "
            "Return JSON matching schema: {{\"title\": string, \"items\": [{{\"id\": string, \"question\": string, \"options\": [string, string, string, string], \"correct_answer\": string, \"explanation\": string, \"difficulty\": \"easy|medium|hard\", \"bloom_level\": \"understanding|applying\"}}]}}."
        ),
        "applications": (
            "Generate 3-5 real-world applications for {subject_type} concepts on {chapter}. "
            "Return JSON matching schema: {{\"items\": [{{\"concept\": string, \"real_world_use\": string, \"explanation\": string}}]}}."
        ),
    }

    def __init__(self, inference_client: InferenceClient | None = None):
        self.inference = inference_client or InferenceClient()
        self.analyzer = ContentAnalyzer()
        self.planner = GenerationPlanner()
        self.critiquer = ArtifactCritiquer(self.inference)
        self.image_extractor = ImageExtractor()
        self.formula_converter = FormulaConverter()

    async def generate_artifacts(
        self,
        chunks: list[dict[str, Any]],
        max_iterations: int | None = None,
        skip_critique: bool = False,
        pdf_path: str | None = None,
    ) -> dict[str, Any]:
        """Generate all educational artifacts using the agentic loop.

        Args:
            chunks: Educational chunks to generate artifacts from.
            max_iterations: Override max iterations per artifact.
            skip_critique: Skip the critique loop (just generate once).
            pdf_path: Path to the source PDF (for image extraction).

        Returns:
            Dict with all generated artifacts and metadata.
        """
        analysis = self.analyzer.analyze(chunks)
        logger.info("Content analysis: %s", analysis.summary())

        tasks = self.planner.plan(analysis)
        logger.info("Generation plan: %s", [t.artifact_type for t in tasks])

        all_text = " ".join(
            clean_text(str(c.get("text", ""))) for c in chunks if c.get("text")
        )
        source_context = all_text[:12000]
        metadata = chunks[0].get("metadata", {}) if chunks else {}
        chapter = metadata.get("chapter") or "Untitled"

        generated: dict[str, Any] = {
            "summaries": [],
            "glossary": [],
            "chapter_notes": [],
            "learning_objectives": [],
            "misconceptions": [],
            "flashcards": [],
            "quizzes": [],
            "applications": [],
        }

        images_data: dict[str, Any] = {"images": [], "formulas": []}

        if pdf_path:
            images_data = await self._extract_media(pdf_path, all_text)
            analysis.has_formulas = analysis.has_formulas or bool(images_data.get("formulas"))

        completed_types: set[str] = set()

        for task in tasks:
            deps_met = all(d in completed_types for d in task.depends_on)
            if not deps_met:
                logger.info("Skipping %s (unmet deps: %s)", task.artifact_type, task.depends_on)
                continue

            iterations = max_iterations or task.max_iterations
            result = await self._execute_task(
                task=task,
                source_context=source_context,
                analysis=analysis,
                metadata=metadata,
                chapter=chapter,
                max_iterations=iterations,
                skip_critique=skip_critique,
                existing_artifacts=generated,
            )

            collection_key = self._collection_key(task.artifact_type)
            if result:
                if isinstance(result, list):
                    generated[collection_key] = result
                else:
                    generated[collection_key] = [result]
            completed_types.add(task.artifact_type)

        return {
            "artifacts": generated,
            "images": images_data.get("images", []),
            "formulas": images_data.get("formulas", []),
            "latex_block": format_latex_block(images_data.get("formulas", [])),
            "image_dir": images_data.get("image_dir", ""),
            "analysis": {
                "category": analysis.category.value,
                "density": analysis.density.value,
                "language": analysis.language,
                "chunk_count": analysis.chunk_count,
                "key_concepts": analysis.key_concepts[:8],
                "has_images": len(images_data.get("images", [])) > 0,
                "has_formulas": analysis.has_formulas,
            },
            "plan": [t.artifact_type for t in tasks],
        }

    async def _extract_media(
        self, pdf_path: str, all_text: str
    ) -> dict[str, Any]:
        """Extract images from PDF and convert formulas to LaTeX."""
        import asyncio

        images = await asyncio.to_thread(self.image_extractor.extract, pdf_path)
        image_dir = images.get("image_dir", "")

        formulas: list[dict[str, Any]] = []

        text_formulas = self.formula_converter.convert_text_formulas(all_text)
        for f in text_formulas:
            formulas.append({
                "latex": f.latex,
                "confidence": f.confidence,
                "method": f.method,
                "original_text": f.original_text,
                "source_image": "",
            })

        for img in images.get("images", []):
            img_path = img.get("path", "")
            if not img_path:
                continue
            result = self.formula_converter.convert_image(
                img_path, page_number=img.get("page", 0)
            )
            if result.latex:
                formulas.append({
                    "latex": result.latex,
                    "confidence": result.confidence,
                    "method": result.method,
                    "source_image": img.get("filename", ""),
                    "page": img.get("page", 0),
                })

        logger.info(
            "Extracted %d images, %d formulas from %s",
            len(images.get("images", [])), len(formulas), pdf_path,
        )

        return {
            "images": images.get("images", []),
            "formulas": formulas,
            "image_dir": image_dir,
        }

    async def _execute_task(
        self,
        task: GenerationPlanner.ArtifactTask,
        source_context: str,
        analysis: ContentAnalysis,
        metadata: dict[str, Any],
        chapter: str,
        max_iterations: int,
        skip_critique: bool,
        existing_artifacts: dict[str, Any],
    ) -> Any:
        prompt = self.MUTLI_TURN_ARTIFACT_PROMPTS.get(task.artifact_type)
        if not prompt:
            return None

        system_prompt = self.SUBJECT_SYSTEM_PROMPTS.get(analysis.category, self.SUBJECT_SYSTEM_PROMPTS[ContentCategory.GENERIC])
        grade = analysis.estimated_grade or 6
        subject_label = analysis.category.value.replace("_", " ").title()

        user_prompt = prompt.format(
            subject_type=subject_label,
            grade=grade,
            chapter=chapter,
        )

        full_prompt = (
            f"{system_prompt}\n\n"
            f"SOURCE CONTENT:\n{source_context}\n\n"
            f"{user_prompt}\n\n"
            f"Return valid JSON only."
        )

        from app.educational_intelligence.artifact_schemas import (
            QuizSet, GlossarySet, FlashcardDeck, ChapterSummary, MisconceptionGuide, LearningObjectives
        )

        schema_map = {
            "quiz": QuizSet,
            "glossary": GlossarySet,
            "flashcards": FlashcardDeck,
            "summary": ChapterSummary,
            "misconceptions": MisconceptionGuide,
            "learning_objectives": LearningObjectives,
        }

        best_result = None
        best_score = 0.0
        critique = None

        schema_cls = schema_map.get(task.artifact_type)

        # Fast path: check if inference-service has direct endpoint for this artifact
        direct_result = await self._call_endpoint_for_task(
            task.artifact_type, chapter, source_context, analysis.key_concepts, metadata
        )
        if direct_result:
            if schema_cls:
                try:
                    return schema_cls.model_validate(direct_result).model_dump()
                except Exception as exc:
                    logger.debug("Validating direct endpoint result for %s: %s", task.artifact_type, exc)
            return direct_result

        for attempt in range(max_iterations):
            logger.info("Generating %s (attempt %d/%d)", task.artifact_type, attempt + 1, max_iterations)

            if attempt == 0:
                target_prompt = full_prompt
            else:
                critique_text = json.dumps({
                    "issues": critique.issues if critique is not None else [],
                    "suggestions": critique.suggestions if critique is not None else [],
                    "scores": {"overall": critique.score} if critique is not None else {},
                }, indent=2)
                target_prompt = ARTIFACT_IMPROVEMENT_PROMPT.format(
                    artifact_type=task.artifact_type.replace("_", " "),
                    source_context=source_context,
                    grade=grade,
                    previous_content=json.dumps(best_result, indent=2, ensure_ascii=False)[:2000],
                    critique_text=critique_text,
                )

            if schema_cls:
                structured_model = await self.inference.generate_structured(target_prompt, schema_cls, max_retries=2)
                if structured_model is not None:
                    result = structured_model.model_dump()
                else:
                    raw = await self.inference._generate_text(target_prompt)
                    result = self._parse_artifact(raw, task.artifact_type) if raw else None
            else:
                raw = await self.inference._generate_text(target_prompt)
                result = self._parse_artifact(raw, task.artifact_type) if raw else None

            if not result:
                continue

            if skip_critique:
                return result

            critique = await self.critiquer.critique(
                artifact_type=task.artifact_type,
                artifact_content=result,
                source_context=source_context,
                grade=grade,
            )

            logger.info(
                "%s critique (attempt %d): score=%.1f, passed=%s, issues=%d",
                task.artifact_type, attempt + 1, critique.score, critique.passed, len(critique.issues),
            )

            if critique.score > best_score:
                best_score = critique.score
                best_result = result

            if critique.passed:
                return result

        if not best_result:
            best_result = await self._generate_heuristic(
                task.artifact_type, source_context, metadata, analysis.key_concepts
            )

        return best_result

    async def _call_endpoint_for_task(
        self, artifact_type: str, title: str, content: str, concepts: list[str], metadata: dict[str, Any]
    ) -> Any | None:
        grade = metadata.get("grade")
        subject = metadata.get("subject")
        chapter = metadata.get("chapter") or title
        language = metadata.get("language")

        try:
            if artifact_type == "summary":
                return await self.inference.generate_summary(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "glossary":
                return await self.inference.generate_glossary(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "chapter_notes":
                return await self.inference.generate_chapter_notes(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "flashcards":
                return await self.inference.generate_flashcards(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "quiz":
                return await self.inference.generate_quiz(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "learning_objectives":
                return await self.inference.generate_learning_objectives(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "misconceptions":
                return await self.inference.generate_misconceptions(title, content, concepts, grade, subject, chapter, language)
            elif artifact_type == "applications":
                return await self.inference.generate_applications(title, content, concepts, grade, subject, chapter, language)
        except Exception as exc:
            logger.debug("Error in _call_endpoint_for_task for %s: %s", artifact_type, exc)
        return None

    async def _generate_heuristic(
        self, artifact_type: str, source_context: str, metadata: dict[str, Any], concepts: list[str]
    ) -> Any | None:
        chunks = [{"text": source_context, "metadata": metadata}]
        try:
            if artifact_type == "summary":
                from app.educational_intelligence.summary_generator import SummaryGenerator
                return await SummaryGenerator(self.inference).generate(chunks)
            elif artifact_type == "glossary":
                from app.educational_intelligence.glossary_extractor import GlossaryExtractor
                return await GlossaryExtractor(self.inference).extract(chunks)
            elif artifact_type == "chapter_notes":
                from app.educational_intelligence.chapter_notes_generator import ChapterNotesGenerator
                return await ChapterNotesGenerator(self.inference).generate(chunks)
            elif artifact_type == "flashcards":
                from app.educational_intelligence.flashcard_generator import FlashcardGenerator
                return await FlashcardGenerator(self.inference).generate(chunks)
            elif artifact_type == "quiz":
                from app.educational_intelligence.quiz_generator import QuizGenerator
                return await QuizGenerator(self.inference).generate(chunks)
            elif artifact_type == "learning_objectives":
                from app.educational_intelligence.learning_objectives_generator import LearningObjectivesGenerator
                return await LearningObjectivesGenerator(self.inference).generate(chunks)
            elif artifact_type == "misconceptions":
                from app.educational_intelligence.misconceptions_generator import MisconceptionsGenerator
                return await MisconceptionsGenerator(self.inference).generate(chunks)
            elif artifact_type == "applications":
                from app.educational_intelligence.applications_generator import ApplicationsGenerator
                return await ApplicationsGenerator(self.inference).generate(chunks)
        except Exception as exc:
            logger.warning("Heuristic generation fallback failed for %s: %s", artifact_type, exc)
        return None

    def _parse_artifact(self, raw: str, artifact_type: str) -> Any:
        from app.educational_intelligence.artifact_schemas import (
            QuizSet, GlossarySet, FlashcardDeck, ChapterSummary, MisconceptionGuide, LearningObjectives
        )

        schema_map = {
            "quiz": QuizSet,
            "glossary": GlossarySet,
            "flashcards": FlashcardDeck,
            "summary": ChapterSummary,
            "misconceptions": MisconceptionGuide,
            "learning_objectives": LearningObjectives,
        }

        parsed_json = None
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*\n?", "", text)
                text = re.sub(r"\n?```\s*$", "", text)
            parsed_json = json.loads(text)
        except json.JSONDecodeError:
            try:
                start = min(
                    pos for pos in (raw.find("{"), raw.find("["))
                    if pos >= 0
                )
                end = max(raw.rfind("}"), raw.rfind("]"))
                if end > start:
                    parsed_json = json.loads(raw[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                pass

        if parsed_json is not None:
            schema_cls = schema_map.get(artifact_type)
            if schema_cls and isinstance(parsed_json, dict):
                try:
                    model_inst = schema_cls.model_validate(parsed_json)
                    return model_inst.model_dump()
                except Exception as exc:
                    logger.warning("Pydantic validation failed for %s: %s", artifact_type, exc)
                    return parsed_json
            return parsed_json

        logger.warning("Failed to parse %s output as JSON", artifact_type)
        return None


    def _collection_key(self, artifact_type: str) -> str:
        mapping = {
            "summary": "summaries",
            "glossary": "glossary",
            "chapter_notes": "chapter_notes",
            "learning_objectives": "learning_objectives",
            "misconceptions": "misconceptions",
            "flashcards": "flashcards",
            "quiz": "quizzes",
            "applications": "applications",
        }
        return mapping.get(artifact_type, artifact_type)
