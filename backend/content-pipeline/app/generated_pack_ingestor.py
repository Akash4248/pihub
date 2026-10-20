import json
import logging
import re
from pathlib import Path
from typing import Any

from app.educational_intelligence.artifact_cleaning import clean_text, is_meaningful_term, is_noisy_text, pick_anchor_sentence
from app.educational_intelligence.glossary_extractor import GlossaryExtractor
from shared.text_normalization import normalize_language_code

from shared.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# Map long subject names to canonical curriculum subjects
SUBJECT_NORMALIZATION = {
    "mathematics": "maths",
    "maths": "maths",
    "science": "science",
}


def _normalize_subject(raw: str) -> str:
    """Map raw subject strings to canonical curriculum subjects."""
    raw_lower = raw.lower().strip()
    if raw_lower in SUBJECT_NORMALIZATION:
        return SUBJECT_NORMALIZATION[raw_lower]
    # social_science variants
    if "social" in raw_lower:
        return "social_science"
    # science keyword
    if "science" in raw_lower:
        return "science"
    # maths keyword
    if "math" in raw_lower:
        return "maths"
    # fallback: use snake_case
    return re.sub(r"[^a-z0-9]+", "_", raw_lower).strip("_")


def _parse_grade(raw: Any) -> int:
    """Convert grade to int, defaulting to 0 for unknowns."""
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return 0


class GeneratedPackIngestor:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline

    def ingest_pack(self, pack_id: str) -> dict[str, Any]:
        """Ingest a generated_pack's artifacts into Qdrant via the pipeline."""
        # Primary path: pack was saved via pack-service into shared packs dir
        # Try to find it; fall back to the raw generated_pack directory in work_dir
        candidate_paths = [
            Path(settings.pack_storage_path) / pack_id,
            Path(settings.pack_storage_path) / "grade_0" / "mixed" / "generated_pack" / pack_id,
            Path(settings.work_dir) / "generated_pack",
            Path(settings.work_dir) / pack_id,
        ]
        pack_dir = None
        for p in candidate_paths:
            if p.exists() and p.is_dir():
                pack_dir = p
                break

        if pack_dir is None:
            raise ValueError(f"Pack directory not found for pack_id={pack_id}. Tried: {candidate_paths}")

        logger.info("Ingesting generated_pack from: %s", pack_dir)
        chunks: list[dict[str, Any]] = []

        # ── concepts.json ─────────────────────────────────────────────────────
        self._ingest_concepts(pack_dir, chunks)

        # ── detailed_explanation.json ─────────────────────────────────────────
        self._ingest_explanations(pack_dir, chunks)

        # ── summary.json ─────────────────────────────────────────────────────
        self._ingest_summaries(pack_dir, chunks)

        # ── glossary.json ─────────────────────────────────────────────────────
        self._ingest_glossary(pack_dir, chunks)

        # ── flashcards.json ───────────────────────────────────────────────────
        self._ingest_flashcards(pack_dir, chunks)

        # Enrich & store
        enriched_chunks = self.pipeline._enrich_chunks(
            chunks, base_metadata={"source": "generated_pack"}
        )

        if enriched_chunks:
            self.pipeline._store_chunks(enriched_chunks)
            logger.info("Ingested %d chunks for pack %s", len(enriched_chunks), pack_id)

        return {
            "pack_id": pack_id,
            "chunks_created": len(enriched_chunks),
            "collection": self.pipeline.collection_name,
            "pack_dir": str(pack_dir),
        }

    # ── Artifact parsers ──────────────────────────────────────────────────────

    def _base_meta(self, item: dict) -> dict:
        """Extract normalized grade/subject/chapter from a top-level artifact item."""
        language = normalize_language_code(
            str(
                item.get("language")
                or item.get("payload", {}).get("language")
                or ""
            )
        )
        return {
            "grade": _parse_grade(item.get("grade", 0)),
            "subject": _normalize_subject(str(item.get("subject", "mixed"))),
            "chapter": clean_text(str(item.get("chapter_title", "Unknown Chapter"))) or "Unknown Chapter",
            "source": "generated_pack",
            **({"language": language} if language else {}),
        }

    def _ingest_concepts(self, pack_dir: Path, chunks: list) -> None:
        f = pack_dir / "concepts.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data:
                meta = self._base_meta(item)
                for concept in item.get("payload", {}).get("concepts", []):
                    name = clean_text(str(concept.get("name", "")))
                    definition = clean_text(str(concept.get("definition", "")))
                    if not name or not definition or not is_meaningful_term(name) or is_noisy_text(definition):
                        continue
                    text = f"Concept: {name}\nDefinition: {definition}"
                    if concept.get("importance"):
                        text += f"\nImportance: {concept['importance']}"
                    chunks.append({
                        "text": text,
                        "metadata": {
                            **meta,
                            "section": "Concepts",
                            "chunk_type": "concept",
                            "topics": concept.get("keywords", []),
                            "concepts": [name],
                        },
                    })
        except Exception as e:
            logger.error("Error parsing concepts.json: %s", e)

    def _ingest_explanations(self, pack_dir: Path, chunks: list) -> None:
        f = pack_dir / "detailed_explanation.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data:
                meta = self._base_meta(item)
                explanation = clean_text(str(item.get("payload", {}).get("explanation", "")))
                if explanation:
                    chunks.append({
                        "text": pick_anchor_sentence(explanation),
                        "metadata": {
                            **meta,
                            "section": "Detailed Explanation",
                            "chunk_type": "explanation",
                            "topics": [meta["chapter"]],
                        },
                    })
        except Exception as e:
            logger.error("Error parsing detailed_explanation.json: %s", e)

    def _ingest_summaries(self, pack_dir: Path, chunks: list) -> None:
        f = pack_dir / "summary.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data:
                meta = self._base_meta(item)
                # Kaggle output has summary_detailed and summary_short
                summary = clean_text(str(item.get("payload", {}).get("summary_detailed", "")))
                if not summary:
                    summary = clean_text(str(item.get("payload", {}).get("summary_short", "")))
                if not summary:
                    summary = clean_text(str(item.get("payload", {}).get("summary", "")))
                
                if summary and not is_noisy_text(summary):
                    chunks.append({
                        "text": f"Summary: {pick_anchor_sentence(summary)}",
                        "metadata": {
                            **meta,
                            "section": "Summary",
                            "chunk_type": "summary",
                            "topics": [meta["chapter"]],
                        },
                    })
        except Exception as e:
            logger.error("Error parsing summary.json: %s", e)

    def _ingest_glossary(self, pack_dir: Path, chunks: list) -> None:
        f = pack_dir / "glossary.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data:
                meta = self._base_meta(item)
                payload = item.get("payload", {})
                
                # Kaggle output format: payload contains term and definition directly
                term = clean_text(str(payload.get("term", "")))
                definition = clean_text(str(payload.get("definition", "")))
                
                if not term or not definition or not is_meaningful_term(term) or is_noisy_text(definition):
                    # Fallback to the old list format if it exists
                    for entry in payload.get("glossary", []):
                        t = clean_text(str(entry.get("term", "")))
                        d = clean_text(str(entry.get("definition", "")))
                        if t and d and is_meaningful_term(t) and not is_noisy_text(d):
                            chunks.append({
                                "text": f"Term: {t}\nDefinition: {pick_anchor_sentence(d, t)}",
                                "metadata": {
                                    **meta,
                                    "section": "Glossary",
                                    "chunk_type": "glossary",
                                    "topics": [t],
                                    "concepts": [t],
                                },
                            })
                    continue
                    continue

                chunks.append({
                    "text": f"Term: {term}\nDefinition: {pick_anchor_sentence(definition, term)}",
                    "metadata": {
                        **meta,
                        "section": "Glossary",
                        "chunk_type": "glossary",
                        "topics": [term],
                        "concepts": [term],
                    },
                })
        except Exception as e:
            logger.error("Error parsing glossary.json: %s", e)

    def _ingest_flashcards(self, pack_dir: Path, chunks: list) -> None:
        f = pack_dir / "flashcards.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data:
                meta = self._base_meta(item)
                payload = item.get("payload", {})
                
                # Kaggle output format: payload contains front and back directly
                question = clean_text(str(payload.get("front", "")))
                answer = clean_text(str(payload.get("back", "")))
                
                if not question or not answer or is_noisy_text(question) or is_noisy_text(answer):
                    # Fallback to the old list format if it exists
                    for card in payload.get("flashcards", []):
                        q = clean_text(str(card.get("question", "")))
                        a = clean_text(str(card.get("answer", "")))
                        if q and a and not is_noisy_text(q) and not is_noisy_text(a):
                            chunks.append({
                                "text": f"Q: {q}\nA: {a}",
                                "metadata": {
                                    **meta,
                                    "section": "Flashcards",
                                    "chunk_type": "flashcard",
                                    "topics": [meta["chapter"]],
                                },
                            })
                    continue
                    continue

                chunks.append({
                    "text": f"Q: {question}\nA: {answer}",
                    "metadata": {
                        **meta,
                        "section": "Flashcards",
                        "chunk_type": "flashcard",
                        "topics": [meta["chapter"]],
                    },
                })
        except Exception as e:
            logger.error("Error parsing flashcards.json: %s", e)
