from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Any

import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from dataclasses import dataclass

from fastapi import FastAPI, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from qdrant_client import QdrantClient

from shared.config import get_settings
from shared.security import sanitize_safe_filename, verify_internal_api_key, sanitize_llm_prompt, read_file_chunks_safely, validate_pdf_upload, InternalAuthMiddleware, require_admin
from shared.curriculum_graph import CurriculumGraph, create_sample_curriculum
from shared.schemas import (
    ChunkPreview,
    DebugRetrievalRequest,
    DirectoryIngestRequest,
    DirectoryIngestResponse,
    DirectoryIngestStatus,
    EducationalResource,
    HealthResponse,
    IngestResponse,
    Metadata,
    PackManifest,
    PackMetadata,
    SearchRequest,
    SearchResponse,
)
from shared.text_normalization import normalize_curriculum_name, normalize_language_code
from shared.vector_store import build_filter, ensure_collection, make_qdrant_client, upsert_chunks

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# In-process RAG observability. Records one entry per retrieval request so the
# console log stream and /rag/metrics can report latency + accuracy proxies.
rag_metrics: deque[dict[str, Any]] = deque(maxlen=500)
_rag_metrics_lock = threading.Lock()


def _record_rag_metric(metric: dict[str, Any]) -> None:
    with _rag_metrics_lock:
        rag_metrics.appendleft({"recorded_at": datetime.now(timezone.utc).isoformat(), **metric})


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = int(round((p / 100.0) * (len(ordered) - 1)))
    return round(ordered[k], 2)


def _rag_metrics_summary() -> dict[str, Any]:
    with _rag_metrics_lock:
        metrics = list(rag_metrics)
    if not metrics:
        return {"count": 0, "latency_ms": {}, "quality": {}, "recent": []}
    lat = [float(m.get("total_ms", 0.0)) for m in metrics]
    embed = [float(m.get("embed_ms", 0.0)) for m in metrics]
    retrieve = [float(m.get("retrieve_ms", 0.0)) for m in metrics]
    rerank = [float(m.get("rerank_ms", 0.0)) for m in metrics]
    non_empty = [m for m in metrics if int(m.get("results_returned", 0)) > 0]
    return {
        "count": len(metrics),
        "latency_ms": {
            "avg": round(sum(lat) / len(lat), 2),
            "p50": _percentile(lat, 50),
            "p95": _percentile(lat, 95),
            "p99": _percentile(lat, 99),
            "embed_avg": round(sum(embed) / len(embed), 2),
            "retrieve_avg": round(sum(retrieve) / len(retrieve), 2),
            "rerank_avg": round(sum(rerank) / len(rerank), 2),
        },
        "quality": {
            "non_empty_rate": round(100.0 * len(non_empty) / len(metrics), 2),
            "avg_candidates": round(sum(float(m.get("candidates", 0)) for m in metrics) / len(metrics), 2),
            "avg_results": round(sum(float(m.get("results_returned", 0)) for m in metrics) / len(metrics), 2),
            "avg_top_score": round(sum(float(m.get("top_score", 0.0)) for m in metrics) / len(metrics), 3),
        },
        "recent": metrics[:40],
    }


def _sanitize_pack_id(s: str) -> str:
    """Create a filesystem-safe, slug-like pack id from arbitrary input."""
    if s is None:
        return ""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", normalize_curriculum_name(str(s))).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    return slug or "pack"

from app.auto_ingest import AutoIngestionService
from app.curriculum_graph.curriculum_router import CurriculumRouter
from app.curriculum_graph.graph_builder import GraphBuilder
from app.curriculum_graph.graph_storage import GraphStorage
from app.curriculum_graph.concept_index import ConceptIndex
from app.educational_intelligence import (
    ApplicationsGenerator,
    ArtifactAgent,
    ChapterNotesGenerator,
    EnrichmentRouter,
    EnrichmentStore,
    EnrichedContentAgent,
    FlashcardGenerator,
    GlossaryExtractor,
    InferenceClient,
    LearningObjectivesGenerator,
    MisconceptionsGenerator,
    PackCompiler,
    QualityEvaluator,
    QuizGenerator,
    ReportRenderer,
    SummaryGenerator,
)
from app.retrieval_engine.hybrid_chunk_store import HybridChunkStore, RetrievalHit
from app.retrieval_engine.educational_retrieval_engine import EducationalRetrievalEngine
from app.textbook_ingest import StructuredTextbookIngest
from app.generated_pack_ingestor import GeneratedPackIngestor


settings = get_settings()
app = FastAPI(title="content-pipeline")
app.add_middleware(InternalAuthMiddleware)


from shared.embedding_service import EmbeddingModelManager, HashFallbackEmbeddingModel


class SentenceTransformerEmbeddingModel:
    """Proxy embedding model routing calls to shared EmbeddingModelManager singleton."""

    def __init__(self, model_name: str, device: str = "cpu", cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self.device = device
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._manager = EmbeddingModelManager.get_instance()

    def get_sentence_embedding_dimension(self) -> int:
        return self._manager.get_dimension()

    def encode(self, texts: list[str] | str, normalize_embeddings: bool = True) -> list[list[float]]:
        model = self._manager.get_model()
        if isinstance(texts, str):
            texts = [texts]
        embeddings = model.encode(
            texts,
            normalize_embeddings=normalize_embeddings,
            show_progress_bar=False,
        )
        if hasattr(embeddings, "tolist"):
            embeddings = embeddings.tolist()
        return [[float(value) for value in row] for row in embeddings]


    def _resolve_local_model_path(self) -> Path | None:
        model_path = Path(self.model_name)
        if model_path.exists():
            return model_path
        if self.cache_dir is None:
            return None
        local_dir = self.cache_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", self.model_name)
        if local_dir.exists() and any(local_dir.iterdir()):
            return local_dir
        try:
            from huggingface_hub import snapshot_download
        except Exception:
            return None
        try:
            local_dir.parent.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=self.model_name,
                local_dir=str(local_dir),
                local_dir_use_symlinks=False,
                resume_download=True,
            )
            return local_dir
        except Exception:
            return None


class Pipeline:
    LEGACY_COLLECTION_NAMES = ("educational_chunks",)

    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self.client = make_qdrant_client(settings.qdrant_url)
        self.embedding_model: SimpleEmbeddingModel | None = None
        self.collection_name = settings.qdrant_collection
        self.upload_dir = Path(settings.upload_dir)
        self.work_dir = Path(settings.work_dir)
        self.content_dir = Path(settings.content_dir)
        self.retrieval_snapshot_path = self.work_dir / "hybrid_chunk_index.json"
        self.curriculum_graph_path = Path(settings.curriculum_graph_path)
        self.curriculum_relation_graph_path = Path(settings.curriculum_relation_graph_path)
        self.textbook_ingestor = StructuredTextbookIngest()
        self.curriculum_graph = CurriculumGraph.load(self.curriculum_graph_path)
        if not self.curriculum_graph.grades:
            self.curriculum_graph = create_sample_curriculum()
            self.curriculum_graph.save(self.curriculum_graph_path)
        self.concept_index = ConceptIndex()
        self.concept_manifest_paths = self._discover_concept_manifest_paths()
        self._rebuild_concept_index()
        self.ingestion_log: list[dict[str, Any]] = []
        self.auto_ingestion_service = AutoIngestionService(self.content_dir, self._auto_ingest_path) if settings.enable_auto_ingestion else None
        self.enable_curriculum_graph_engine = settings.enable_curriculum_graph_engine
        self.graph_storage = GraphStorage(self.curriculum_relation_graph_path)
        self.graph_builder = GraphBuilder()
        self.relation_graph = self.graph_storage.load() if self.enable_curriculum_graph_engine else {}
        self.enable_educational_retrieval_engine = settings.enable_educational_retrieval_engine
        self.retrieval_engine = EducationalRetrievalEngine()
        self.hybrid_store = HybridChunkStore(
            snapshot_path=self.retrieval_snapshot_path,
            model_name=settings.local_retrieval_model_name,
            device=settings.local_retrieval_device,
            cache_dir=settings.local_retrieval_cache_dir,
        )
        self.enable_ai_artifact_generation = settings.enable_ai_artifact_generation
        self.inference_client = InferenceClient() if self.enable_ai_artifact_generation else None
        self.summary_generator = SummaryGenerator(inference_client=self.inference_client)
        self.glossary_extractor = GlossaryExtractor(inference_client=self.inference_client)
        self.quiz_generator = QuizGenerator(inference_client=self.inference_client)
        self.flashcard_generator = FlashcardGenerator(inference_client=self.inference_client)
        self.chapter_notes_generator = ChapterNotesGenerator(inference_client=self.inference_client)
        self.learning_objectives_generator = LearningObjectivesGenerator(inference_client=self.inference_client)
        self.misconceptions_generator = MisconceptionsGenerator(inference_client=self.inference_client)
        self.applications_generator = ApplicationsGenerator(inference_client=self.inference_client)
        self.artifact_agent = ArtifactAgent(inference_client=self.inference_client)
        from app.educational_intelligence.media_store import MediaStore
        self.media_store = MediaStore()
        self.enrichment_store = EnrichmentStore(embedding_model=self._load_model())
        self.enriched_content_agent = EnrichedContentAgent(
            inference_client=self.inference_client,
            artifact_agent=self.artifact_agent,
            media_store=self.media_store,
            enrichment_store=self.enrichment_store,
        )
        self.enrichment_router = EnrichmentRouter()
        self.pack_compiler = PackCompiler()
        self.quality_evaluator = QualityEvaluator()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.curriculum_graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.curriculum_relation_graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.retrieval_snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_model(self) -> SimpleEmbeddingModel:
        if self.embedding_model is None:
            self.embedding_model = SentenceTransformerEmbeddingModel(
                settings.embedding_model_name,
                settings.embedding_device,
                settings.embedding_cache_dir,
            )
        return self.embedding_model

    def _vector_size(self) -> int:
        return self._load_model().get_sentence_embedding_dimension()

    def _discover_concept_manifest_paths(self) -> list[Path]:
        current_file = Path(__file__).resolve()
        repo_root = current_file.parents[3] if len(current_file.parents) > 3 else current_file.parents[-1]
        candidate_paths = [
            Path(settings.curriculum_manifest_path),
            Path(settings.pack_registry_path),
            repo_root / "backend" / "curriculum-builder" / "curriculum_build" / "curriculum_manifest.json",
            repo_root / "backend" / "curriculum-builder" / "complete_build" / "curriculum_manifest.json",
            repo_root / "backend" / "curriculum-builder" / "complete_build" / "pack_registry.json",
        ]
        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in candidate_paths:
            resolved = str(path.resolve()) if path.exists() else str(path)
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            unique_paths.append(path)
        return unique_paths

    def _rebuild_concept_index(
        self,
        chunks: list[dict[str, Any]] | None = None,
        glossary_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.concept_index.build_from_curriculum(
            self.curriculum_graph,
            manifest_paths=self.concept_manifest_paths,
            chunks=chunks,
            glossary_entries=glossary_entries,
        )

    def ensure_ready(self) -> None:
        try:
            ensure_collection(self.client, self.collection_name, self._vector_size(), settings.qdrant_url)
        except Exception:
            logger.warning("Qdrant unavailable for ensure_collection — running in degraded mode")
        try:
            if not self.hybrid_store.is_ready():
                self.hybrid_store.load_snapshot()
            if not self.hybrid_store.is_ready():
                self._hydrate_hybrid_store()
        except Exception as exc:
            logger.warning("Hybrid store hydration failed — search degraded: %s", str(exc)[:300])

    def reset_rag_state(self) -> dict[str, Any]:
        with self._write_lock:
            logger.info("Resetting RAG collection %s", self.collection_name)
            try:
                self.client.delete_collection(collection_name=self.collection_name)
            except Exception as exc:
                logger.warning("Could not delete collection %s cleanly: %s", self.collection_name, str(exc)[:300])
            ensure_collection(self.client, self.collection_name, self._vector_size(), settings.qdrant_url)
            self.hybrid_store.clear()
            if self.enable_curriculum_graph_engine:
                self.relation_graph = {}
                self.graph_storage.save(self.relation_graph)
            self.ingestion_log.clear()
            return {
                "collection": self.collection_name,
                "hybrid_snapshot": str(self.retrieval_snapshot_path),
                "status": "reset",
            }

    def _collection_point_count(self, collection_name: str) -> int:
        try:
            response = self.client.count(collection_name=collection_name, exact=True)
            return int(getattr(response, "count", 0) or 0)
        except Exception:
            try:
                points, _ = self.client.scroll(
                    collection_name=collection_name,
                    limit=1,
                    with_payload=False,
                    with_vectors=False,
                )
                return len(points)
            except Exception:
                return 0

    def _migrate_legacy_collection_if_needed(self) -> None:
        current_count = self._collection_point_count(self.collection_name)
        if current_count > 0:
            return
        for legacy_collection in self.LEGACY_COLLECTION_NAMES:
            if legacy_collection == self.collection_name:
                continue
            legacy_count = self._collection_point_count(legacy_collection)
            if legacy_count <= 0:
                continue
            logger.info(
                "Migrating Qdrant collection %s -> %s (%s points)",
                legacy_collection,
                self.collection_name,
                legacy_count,
            )
            migrated_chunks: list[dict[str, Any]] = []
            offset = None
            while True:
                points, offset = self.client.scroll(
                    collection_name=legacy_collection,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    migrated_chunks.append(
                        {
                            "chunk_id": str(point.id),
                            "text": str(payload.get("text", "")),
                            "metadata": {k: v for k, v in payload.items() if k != "text"},
                        }
                    )
                if offset is None:
                    break
            if migrated_chunks:
                texts = [chunk["text"] for chunk in migrated_chunks]
                embedding_texts = []
                for chunk in migrated_chunks:
                    metadata = chunk.get("metadata", {})
                    prefix_parts = [
                        str(metadata.get("grade") or ""),
                        str(metadata.get("subject") or ""),
                        str(metadata.get("chapter") or ""),
                        str(metadata.get("section") or ""),
                        " ".join(metadata.get("topics", [])),
                        " ".join(metadata.get("concepts", [])),
                    ]
                    prefix = " ".join(part for part in prefix_parts if part).strip()
                    embedding_texts.append(f"{prefix}\n\n{chunk['text']}" if prefix else chunk["text"])

                embeddings = self._load_model().encode(embedding_texts, normalize_embeddings=True)
                metadatas = [chunk["metadata"] for chunk in migrated_chunks]
                point_ids = upsert_chunks(self.client, self.collection_name, embeddings, texts, metadatas)
                try:
                    self.hybrid_store.upsert(
                        [
                            {
                                "chunk_id": str(point_ids[idx]),
                                "text": chunk["text"],
                                "metadata": {**chunk["metadata"], "retrieval_source": "legacy_migration"},
                            }
                            for idx, chunk in enumerate(migrated_chunks)
                        ]
                    )
                except Exception as exc:
                    logger.warning("Hybrid retrieval store migration update failed: %s", str(exc)[:300])
                self.curriculum_graph.build_from_chunks(migrated_chunks)
                self.curriculum_graph.save(self.curriculum_graph_path)
                glossary_entries = self.glossary_extractor.extract(migrated_chunks)
                try:
                    self._rebuild_concept_index(chunks=migrated_chunks, glossary_entries=glossary_entries)
                except Exception:
                    pass
                if self.enable_curriculum_graph_engine:
                    self.relation_graph = self.graph_builder.build(migrated_chunks, existing=self.relation_graph)
                    self.graph_storage.save(self.relation_graph)
            break

    def _hydrate_hybrid_store(self) -> None:
        try:
            documents: list[dict[str, Any]] = []
            offset = None
            while True:
                points, offset = self.client.scroll(
                    collection_name=self.collection_name,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    text = str(payload.get("text", ""))
                    metadata = {k: v for k, v in payload.items() if k != "text"}
                    documents.append(
                        {
                            "chunk_id": str(point.id),
                            "text": text,
                            "metadata": metadata,
                        }
                    )
                if offset is None:
                    break
            if documents:
                self.hybrid_store.upsert(documents)
        except Exception as exc:
            logger.warning("Hybrid retrieval store hydration failed: %s", str(exc)[:300])

    def _resolve_content_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.content_dir / candidate
        resolved = candidate.resolve()
        content_root = self.content_dir.resolve()
        if resolved != content_root and content_root not in resolved.parents:
            raise HTTPException(status_code=400, detail="Path must be inside the configured content directory")
        return resolved

    def _merge_metadata(
        self,
        file_path: Path,
        metadata: Metadata | None = None,
        raw_text: str = "",
        source: str | None = None,
    ) -> dict[str, Any]:
        merged = self.textbook_ingestor.metadata_extractor.extract_from_path(file_path)
        if metadata is not None:
            merged.update(metadata.model_dump(exclude_none=True))
        merged = self.textbook_ingestor.metadata_extractor.merge_text_metadata(merged, raw_text)
        if merged.get("chapter"):
            merged["chapter"] = normalize_curriculum_name(str(merged["chapter"]))
        if merged.get("subject"):
            merged["subject"] = normalize_curriculum_name(str(merged["subject"]))
        if merged.get("language"):
            merged["language"] = normalize_language_code(str(merged["language"])) or normalize_curriculum_name(str(merged["language"]))
        if source:
            merged["source"] = source
        return merged

    def _ensure_topics(self, chunk: dict[str, Any]) -> list[str]:
        metadata = chunk.get("metadata", {})
        topics = list(metadata.get("topics") or [])
        if topics:
            return topics
        topics = self.curriculum_graph.infer_topics_for_query(chunk.get("text", ""))
        if topics:
            return topics
        return self.curriculum_graph.infer_concepts_for_text(chunk.get("text", ""), limit=4)

    @staticmethod
    def _tokenize_text(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF']+", text.lower())
            if token
        }

    def _lexical_relevance(self, query: str, hit: Any) -> float:
        payload = hit.payload or {}
        query_tokens = self._tokenize_text(query)
        payload_text = " ".join(
            [
                str(payload.get("text", "")),
                str(payload.get("subject", "")),
                str(payload.get("chapter", "")),
                str(payload.get("section", "")),
                " ".join(payload.get("topics", [])) if isinstance(payload.get("topics"), list) else str(payload.get("topics", "")),
                " ".join(payload.get("concepts", [])) if isinstance(payload.get("concepts"), list) else str(payload.get("concepts", "")),
            ]
        )
        payload_tokens = self._tokenize_text(payload_text)
        if not query_tokens or not payload_tokens:
            return float(hit.score or 0.0)

        overlap = len(query_tokens & payload_tokens) / max(len(query_tokens), 1)
        score = max(float(hit.score or 0.0), overlap)

        inferred_subject = self.curriculum_graph.infer_subject_for_query(query)
        if inferred_subject and str(payload.get("subject", "")).lower() == inferred_subject.lower():
            score = max(score, 0.65)

        inferred_topics = self.curriculum_graph.infer_topics_for_query(query)
        payload_topics = {str(topic).lower() for topic in (payload.get("topics") or [])}
        if any(topic.lower() in payload_topics for topic in inferred_topics):
            score = max(score, 0.8)

        chapter = str(payload.get("chapter", "")).lower()
        if chapter and any(token in chapter for token in query_tokens):
            score = max(score, 0.7)

        return score

    def _enrich_chunks(self, chunks: list[dict[str, Any]], base_metadata: dict[str, Any]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for chunk in chunks:
            metadata = {**base_metadata, **chunk.get("metadata", {})}
            metadata.setdefault("source", base_metadata.get("source", "upload"))
            metadata.setdefault("language", base_metadata.get("language", "en"))
            inferred_subject = metadata.get("subject") or self.curriculum_graph.infer_subject_for_query(chunk.get("text", ""))
            if inferred_subject:
                metadata["subject"] = normalize_curriculum_name(str(inferred_subject))
            topics = self._ensure_topics(chunk)
            metadata["topics"] = [normalize_curriculum_name(str(topic)) for topic in topics if topic]
            concepts = self.curriculum_graph.infer_concepts_for_text(chunk.get("text", ""), limit=6)
            if concepts:
                metadata["concepts"] = concepts
            if not metadata.get("chapter") and base_metadata.get("chapter"):
                metadata["chapter"] = normalize_curriculum_name(str(base_metadata["chapter"]))
            if metadata.get("chapter"):
                metadata["chapter"] = normalize_curriculum_name(str(metadata["chapter"]))
            if metadata.get("section"):
                metadata["section"] = normalize_curriculum_name(str(metadata["section"]))
            if metadata.get("language"):
                metadata["language"] = normalize_language_code(str(metadata["language"])) or normalize_curriculum_name(str(metadata["language"]))
            enriched.append({"text": chunk["text"], "metadata": metadata})
        return enriched

    def _prepare_response_metadata(self, metadata: dict[str, Any]) -> Metadata:
        return Metadata(
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            topic=(metadata.get("topics") or [metadata.get("topic") or None])[0] if (metadata.get("topics") or metadata.get("topic")) else None,
            language=metadata.get("language"),
        )

    def _store_chunks(self, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            return
        with self._write_lock:
            logger.info("Storing %d enriched chunks to vector store", len(chunks))
            print(f"[store] storing_chunks count={len(chunks)}")

            texts = [chunk["text"] for chunk in chunks]
            embedding_texts = []
            for chunk in chunks:
                metadata = chunk.get("metadata", {})
                prefix_parts = [
                    str(metadata.get("grade") or ""),
                    str(metadata.get("subject") or ""),
                    str(metadata.get("chapter") or ""),
                    str(metadata.get("section") or ""),
                    " ".join(metadata.get("topics", [])),
                    " ".join(metadata.get("concepts", [])),
                ]
                prefix = " ".join(part for part in prefix_parts if part).strip()
                embedding_texts.append(f"{prefix}\n\n{chunk['text']}" if prefix else chunk["text"])

            embeddings = self._load_model().encode(embedding_texts, normalize_embeddings=True)
            logger.info("Generated %d embeddings for %d chunks", len(embeddings), len(chunks))
            print(f"[store] embeddings_generated count={len(embeddings)}")
            metadatas = [chunk["metadata"] for chunk in chunks]
            point_ids = upsert_chunks(self.client, self.collection_name, embeddings, texts, metadatas)
            logger.info("Upserted %d vectors into collection %s", len(point_ids), self.collection_name)
            print(f"[store] upserted_vectors count={len(point_ids)} collection={self.collection_name}")
            try:
                self.hybrid_store.upsert(
                    [
                        {
                            "chunk_id": str(point_ids[idx]),
                            "text": chunk["text"],
                            "metadata": {
                                **chunk["metadata"],
                                "retrieval_source": "local_hybrid",
                            },
                        }
                        for idx, chunk in enumerate(chunks)
                    ],
                    vectors=embeddings,
                )

            except Exception as exc:
                logger.warning("Hybrid retrieval store update failed: %s", str(exc)[:300])
            self.curriculum_graph.build_from_chunks(chunks)
            self.curriculum_graph.save(self.curriculum_graph_path)
            glossary_entries = self.glossary_extractor._heuristic_extract(chunks) if hasattr(self.glossary_extractor, '_heuristic_extract') else []
            try:
                self._rebuild_concept_index(chunks=chunks, glossary_entries=glossary_entries)
            except Exception:
                pass
        if self.enable_curriculum_graph_engine:
            self.relation_graph = self.graph_builder.build(chunks, existing=self.relation_graph)
            self.graph_storage.save(self.relation_graph)

    async def _generate_artifacts(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        """Generate all educational artifacts from chunks using AI generators.

        Returns a dict with summaries, glossary, quizzes, flashcards,
        chapter notes, learning objectives, misconceptions, and applications.
        """
        if not chunks:
            return {}

        if not self.enable_ai_artifact_generation:
            return {}

        metadata = chunks[0].get("metadata", {}) if chunks else {}
        chapter = metadata.get("chapter")
        logger.info("Generating artifacts for chapter: %s", chapter)

        summary = await self.summary_generator.generate(chunks, chapter=chapter)
        glossary = await self.glossary_extractor.extract(chunks) if hasattr(self.glossary_extractor, 'extract') and callable(getattr(self.glossary_extractor, 'extract')) else []
        quizzes = await self.quiz_generator.generate(chunks)
        flashcards = await self.flashcard_generator.generate(chunks)
        chapter_notes = await self.chapter_notes_generator.generate(chunks)
        learning_objectives = await self.learning_objectives_generator.generate(chunks)
        misconceptions = await self.misconceptions_generator.generate(chunks)
        applications = await self.applications_generator.generate(chunks)

        enrichment = self.enrichment_router.route(
            topic=str(metadata.get("topics", [None])[0] or chapter or ""),
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
        )

        artifacts = {
            "summaries": [summary] if summary else [],
            "glossary": glossary,
            "quizzes": quizzes,
            "flashcards": flashcards,
            "chapter_notes": [chapter_notes] if chapter_notes else [],
            "learning_objectives": learning_objectives,
            "misconceptions": misconceptions,
            "applications": applications,
            "enrichment": [enrichment] if enrichment else [],
        }

        logger.info(
            "Generated artifacts: %d summaries, %d glossary, %d quizzes, "
            "%d flashcards, %d chapter_notes, %d objectives, %d misconceptions, %d applications",
            len(artifacts["summaries"]),
            len(artifacts["glossary"]),
            len(artifacts["quizzes"]),
            len(artifacts["flashcards"]),
            len(artifacts["chapter_notes"]),
            len(artifacts["learning_objectives"]),
            len(artifacts["misconceptions"]),
            len(artifacts["applications"]),
        )

        return artifacts

    async def _ingest_and_generate_artifacts(
        self, file_path: Path, metadata: Metadata | None = None, source: str | None = None
    ) -> dict[str, Any]:
        """Ingest a PDF and generate AI-powered educational artifacts."""
        result = await asyncio.to_thread(self._ingest_textbook_path, file_path, metadata, source)
        chunks = result.get("chunks", [])
        if chunks:
            artifacts = await self._generate_artifacts(chunks)
            result["artifacts"] = artifacts
        return result

    async def _auto_ingest_path(self, file_path: Path) -> None:
        await asyncio.to_thread(self._ingest_textbook_path, file_path, None, "watcher")

    def _ingest_textbook_path(self, file_path: Path, metadata: Metadata | None = None, source: str | None = None) -> dict[str, Any]:
        self.ensure_ready()
        raw_text = self.textbook_ingestor.extract_text_from_pdf(file_path)
        logger.info("Extracted text length for %s: %d", file_path.name, len(raw_text or ""))
        base_metadata = self._merge_metadata(file_path, metadata, raw_text, source=source or "textbook")
        chunks = self.textbook_ingestor.ingest_from_path(file_path, raw_text)
        logger.info("Detected %d raw chunks from chunker for %s", len(chunks), file_path.name)
        enriched_chunks = self._enrich_chunks(chunks, base_metadata)
        self._store_chunks(enriched_chunks)

        with self._write_lock:
            self.ingestion_log.append(
                {
                    "file_name": file_path.name,
                    "source_path": str(file_path),
                    "chunks_created": len(enriched_chunks),
                    "metadata": base_metadata,
                }
            )

        return {
            "file_name": file_path.name,
            "source_path": str(file_path),
            "chunks_created": len(enriched_chunks),
            "collection": self.collection_name,
            "metadata": self._prepare_response_metadata(base_metadata),
            "chunks": enriched_chunks,
        }

    def ingest_pdf(self, file_name: str, content: bytes, metadata: Metadata, source: str | None = None) -> IngestResponse:
        self.ensure_ready()
        safe_name = sanitize_safe_filename(file_name)
        target_path = self.upload_dir / safe_name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)

        result = self._ingest_textbook_path(target_path, metadata=metadata, source=source or "upload")
        return IngestResponse(
            file_name=file_name,
            chunks_created=result["chunks_created"],
            collection=self.collection_name,
            metadata=result["metadata"],
        )

    def _directory_checkpoint_path(self, directory: Path, recursive: bool) -> Path:
        key = hashlib.sha1(
            f"{self.collection_name}|{directory}|{recursive}".encode("utf-8")
        ).hexdigest()[:16]
        return self.work_dir / "ingest_checkpoints" / f"{key}.json"

    def _load_ingest_checkpoint(self, directory: Path, recursive: bool) -> dict[str, Any]:
        checkpoint = self._directory_checkpoint_path(directory, recursive)
        if not checkpoint.exists():
            return {"checkpoint": checkpoint, "completed": set(), "updated_at": None}
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            if payload.get("collection") == self.collection_name and payload.get("directory") == str(directory):
                return {
                    "checkpoint": checkpoint,
                    "completed": set(payload.get("completed", [])),
                    "updated_at": payload.get("updated_at"),
                }
        except Exception as exc:
            logger.warning("Ignoring unreadable ingest checkpoint %s: %s", checkpoint, exc)
        return {"checkpoint": checkpoint, "completed": set(), "updated_at": None}

    def _save_ingest_checkpoint(
        self, checkpoint: Path, directory: Path, recursive: bool, files: list[Path], completed: set[str]
    ) -> None:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "collection": self.collection_name,
            "directory": str(directory),
            "recursive": recursive,
            "total_files": len(files),
            "completed": sorted(completed),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = checkpoint.with_name(checkpoint.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, checkpoint)

    def ingest_textbook_directory(
        self,
        directory: Path,
        recursive: bool = True,
        source: str | None = None,
        restart: bool = False,
    ) -> dict[str, Any]:
        self.ensure_ready()
        directory = self._resolve_content_path(str(directory))
        pattern = "**/*.pdf" if recursive else "*.pdf"
        files = sorted(directory.glob(pattern))

        state = self._load_ingest_checkpoint(directory, recursive)
        checkpoint = state["checkpoint"]
        completed: set[str] = set() if restart else set(state["completed"])
        resumed = bool(completed)

        def _persist() -> None:
            self._save_ingest_checkpoint(checkpoint, directory, recursive, files, completed)

        results: list[dict[str, Any]] = []
        total_chunks = 0
        skipped = 0
        remaining = sum(1 for f in files if str(f) not in completed)
        logger.info(
            "Directory ingest: dir=%s total=%d already_done=%d remaining=%d resume=%s restart=%s",
            directory, len(files), len(completed), remaining, resumed, restart,
        )

        for file_path in files:
            source_path = str(file_path)
            if source_path in completed:
                skipped += 1
                continue
            try:
                result = self._ingest_textbook_path(file_path, metadata=None, source=source or "directory")
                results.append({
                    "file_name": result["file_name"],
                    "source_path": result["source_path"],
                    "chunks_created": result["chunks_created"],
                    "metadata": result["metadata"].model_dump(),
                })
                total_chunks += result["chunks_created"]
                completed.add(source_path)
                _persist()
                logger.info(
                    "Ingested %s (%d chunks) — %d/%d files complete",
                    file_path.name, result["chunks_created"], len(completed), len(files),
                )
            except Exception as exc:
                logger.exception("Failed to ingest %s", file_path)
                results.append({"file_name": file_path.name, "source_path": source_path, "error": str(exc)})

        _persist()
        return {
            "directory": str(directory),
            "recursive": recursive,
            "files_processed": len(results),
            "files_skipped": skipped,
            "total_files": len(files),
            "chunks_created": total_chunks,
            "collection": self.collection_name,
            "resumed": resumed,
            "restart": restart,
            "results": results,
        }

    def _query_boost_terms(self, query: str, inferred_topics: list[str], prerequisite_topics: list[str], related_topics: list[str]) -> list[str]:
        terms: list[str] = []
        for bucket in (inferred_topics, prerequisite_topics, related_topics):
            for term in bucket:
                normalized = normalize_curriculum_name(str(term))
                if normalized and normalized not in terms:
                    terms.append(normalized)
        for token in self._tokenize_text(query):
            if token not in terms:
                terms.append(token)
        return terms[:24]

    def _merge_hits(self, hits: list[Any]) -> list[RetrievalHit]:
        merged: dict[str, RetrievalHit] = {}
        for hit in hits:
            if isinstance(hit, dict):
                hit_id = str(hit.get("id") or "")
                score = float(hit.get("score") or 0.0)
                payload = dict(hit.get("payload") or hit.get("metadata") or {})
                payload_text = str(hit.get("text") or payload.get("text") or "")
                if payload_text:
                    payload["text"] = payload_text
                source = str(hit.get("source") or payload.get("retrieval_source") or "hybrid")
            else:
                hit_id = str(getattr(hit, "id", ""))
                score = float(getattr(hit, "score", 0.0) or 0.0)
                payload = dict(getattr(hit, "payload", {}) or {})
                source = str(getattr(hit, "source", payload.get("retrieval_source") or "hybrid"))
            if not hit_id:
                continue
            payload.setdefault("retrieval_source", source)
            payload.setdefault("text", str(payload.get("text") or ""))
            payload["retrieval_source"] = source
            current = merged.get(hit_id)
            if current is None or score > current.score:
                merged[hit_id] = RetrievalHit(id=hit_id, score=score, payload=payload, source=source)
            else:
                current.payload.update({k: v for k, v in payload.items() if k not in current.payload})
                current.score = max(current.score, score)
                if source not in current.source:
                    current.source = f"{current.source}|{source}"
        return sorted(merged.values(), key=lambda item: item.score, reverse=True)

    def _retrieve_candidate_hits(
        self,
        query: str,
        limit: int,
        requested_filters: dict[str, Any],
        routed_filters: dict[str, Any],
        inferred_subject: str | None,
        inferred_topics: list[str],
        prerequisite_topics: list[str],
        related_topics: list[str],
    ) -> tuple[list[RetrievalHit], dict[str, Any]]:
        search_limit = max(limit * 10, 30)
        boost_terms = self._query_boost_terms(query, inferred_topics, prerequisite_topics, related_topics)
        qfilter = build_filter(requested_filters)
        expanded_query = " ".join(part for part in [query, " ".join(boost_terms)] if part).strip()
        _embed_start = time.perf_counter()
        query_vector = self._load_model().encode([expanded_query or query], normalize_embeddings=True)[0]
        embed_ms = (time.perf_counter() - _embed_start) * 1000

        _dense_start = time.perf_counter()
        dense_hits = self.client.search(
            collection_name=self.collection_name,
            query_vector=query_vector,
            limit=search_limit,
            query_filter=qfilter,
            with_payload=True,
        )
        dense_filter_fallback_used = False
        if not dense_hits and qfilter is not None:
            dense_hits = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=search_limit,
                query_filter=None,
                with_payload=True,
            )
            dense_filter_fallback_used = True
        dense_ms = (time.perf_counter() - _dense_start) * 1000
        candidate_hits: list[Any] = [
            {
                "id": str(hit.id),
                "score": float(hit.score) if hit.score is not None else 0.0,
                "text": str((hit.payload or {}).get("text", "")),
                "payload": {**(hit.payload or {}), "retrieval_source": "qdrant_dense"},
                "source": "qdrant_dense",
            }
            for hit in dense_hits
        ]

        _lexical_start = time.perf_counter()
        lexical_hits = self.hybrid_store.search(
            query=query,
            limit=search_limit,
            filters=requested_filters or None,
            boost_terms=boost_terms,
        )
        lexical_filter_fallback_used = False
        if not lexical_hits and requested_filters:
            lexical_hits = self.hybrid_store.search(
                query=query,
                limit=search_limit,
                filters=None,
                boost_terms=boost_terms,
            )
            lexical_filter_fallback_used = True
        lexical_ms = (time.perf_counter() - _lexical_start) * 1000
        candidate_hits.extend(lexical_hits)

        chapter_candidates: list[str] = []
        confidence = 0.0
        if self.enable_curriculum_graph_engine:
            chapter_candidates, confidence, _ = self.concept_index.route_query_to_chapters(query, self.curriculum_graph)

        chapter_hit_count = 0
        if chapter_candidates and confidence >= 0.35:
            for chapter in chapter_candidates[:3]:
                chapter_filters = {"chapter": chapter}
                if inferred_subject:
                    chapter_filters["subject"] = inferred_subject
                chapter_dense_hits = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=query_vector,
                    limit=max(6, limit * 3),
                    query_filter=build_filter(chapter_filters),
                    with_payload=True,
                )
                chapter_hit_count += len(chapter_dense_hits)
                candidate_hits.extend(
                    {
                        "id": str(hit.id),
                        "score": float(hit.score) if hit.score is not None else 0.0,
                        "text": str((hit.payload or {}).get("text", "")),
                        "payload": {**(hit.payload or {}), "retrieval_source": "chapter_dense"},
                        "source": "chapter_dense",
                    }
                    for hit in chapter_dense_hits
                )
                chapter_lexical_hits = self.hybrid_store.search(
                    query=query,
                    limit=max(6, limit * 3),
                    filters=chapter_filters,
                    boost_terms=boost_terms,
                )
                candidate_hits.extend(chapter_lexical_hits)

        merged = self._merge_hits(candidate_hits)
        diagnostics = {
            "dense_hits": len(dense_hits),
            "lexical_hits": len(lexical_hits),
            "dense_filter_fallback_used": dense_filter_fallback_used,
            "lexical_filter_fallback_used": lexical_filter_fallback_used,
            "chapter_candidates": chapter_candidates[:5],
            "chapter_candidate_confidence": round(confidence, 3),
            "chapter_hits": chapter_hit_count,
            "boost_terms": boost_terms[:12],
            "embed_ms": round(embed_ms, 2),
            "dense_ms": round(dense_ms, 2),
            "lexical_ms": round(lexical_ms, 2),
        }
        return merged, diagnostics

    def _search(self, query: str, limit: int, filters: dict[str, Any] | None = None) -> SearchResponse:
        _request_start = time.perf_counter()
        self.ensure_ready()
        requested_filters = dict(filters or {})
        routed_filters = dict(requested_filters)
        inferred_subject = self.curriculum_graph.infer_subject_for_query(query)
        inferred_topics = self.curriculum_graph.infer_topics_for_query(query)
        prerequisite_topics: list[str] = []
        related_topics: list[str] = []
        if self.enable_curriculum_graph_engine:
            route = CurriculumRouter(self.curriculum_graph, self.relation_graph).route(query, routed_filters)
            routed_filters = route.filters
            inferred_topics = route.expanded_topics or inferred_topics
            prerequisite_topics = route.prerequisite_topics
            related_topics = route.related_topics
        else:
            if inferred_subject and "subject" not in routed_filters:
                routed_filters["subject"] = inferred_subject
            if inferred_topics and "topic" not in routed_filters:
                routed_filters["topic"] = inferred_topics[0]

        # If topic was inferred (not explicit), avoid strict topic filter and rerank by educational priority.
        if self.enable_educational_retrieval_engine and "topic" in routed_filters and "topic" not in requested_filters:
            routed_filters.pop("topic", None)

        _retrieve_start = time.perf_counter()
        hits, _retrieval_debug = self._retrieve_candidate_hits(
            query=query,
            limit=limit,
            requested_filters=requested_filters,
            routed_filters=routed_filters,
            inferred_subject=inferred_subject,
            inferred_topics=inferred_topics,
            prerequisite_topics=prerequisite_topics,
            related_topics=related_topics,
        )
        retrieve_ms = (time.perf_counter() - _retrieve_start) * 1000

        _rerank_start = time.perf_counter()
        if self.enable_educational_retrieval_engine:
            results = self.retrieval_engine.rank(
                query=query,
                hits=hits,
                limit=limit,
                routed_filters=routed_filters,
                inferred_subject=inferred_subject,
                inferred_topics=inferred_topics,
                prerequisite_topics=prerequisite_topics,
                related_topics=related_topics,
            )
            # Keep response shape backward-compatible.
            for item in results:
                item.pop("ranking_debug", None)
                item.pop("vector_score", None)
        else:
            score_threshold = 0.3
            results = [
                {
                    "id": str(hit.id),
                    "score": self._lexical_relevance(query, hit),
                    "text": str((hit.payload or {}).get("text", "")),
                    "metadata": {k: v for k, v in (hit.payload or {}).items() if k != "text"},
                }
                for hit in hits
                if self._lexical_relevance(query, hit) >= score_threshold
            ]
        rerank_ms = (time.perf_counter() - _rerank_start) * 1000
        results = results[:limit]

        total_ms = (time.perf_counter() - _request_start) * 1000
        top_score = float(results[0].get("score", 0.0)) if results else 0.0
        metric = {
            "query": query,
            "limit": limit,
            "filters": requested_filters,
            "routed_filters": routed_filters,
            "candidates": len(hits),
            "results_returned": len(results),
            "top_score": round(top_score, 4),
            "embed_ms": round(float(_retrieval_debug.get("embed_ms", 0.0)), 2),
            "retrieve_ms": round(retrieve_ms, 2),
            "rerank_ms": round(rerank_ms, 2),
            "total_ms": round(total_ms, 2),
            "dense_hits": int(_retrieval_debug.get("dense_hits", 0)),
            "lexical_hits": int(_retrieval_debug.get("lexical_hits", 0)),
            "chapter_candidates": _retrieval_debug.get("chapter_candidates", []),
        }
        _record_rag_metric(metric)
        logger.info(
            "[RAG] query=%r limit=%d filters=%s candidates=%d returned=%d top_score=%.3f "
            "embed_ms=%.1f retrieve_ms=%.1f rerank_ms=%.1f total_ms=%.1f top_ids=%s",
            query,
            limit,
            requested_filters or {},
            metric["candidates"],
            metric["results_returned"],
            metric["top_score"],
            metric["embed_ms"],
            metric["retrieve_ms"],
            metric["rerank_ms"],
            metric["total_ms"],
            [str(r.get("id"))[:8] for r in results[:5]],
        )
        return SearchResponse(query=query, results=results)

    def debug_retrieval(self, query: str, limit: int, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        routed_filters = dict(metadata or {})
        inferred_subject = self.curriculum_graph.infer_subject_for_query(query)
        inferred_topics = self.curriculum_graph.infer_topics_for_query(query)
        expanded_topics: list[str] = []
        prerequisite_topics: list[str] = []
        related_topics: list[str] = []
        if self.enable_curriculum_graph_engine:
            route = CurriculumRouter(self.curriculum_graph, self.relation_graph).route(query, routed_filters)
            routed_filters = route.filters
            expanded_topics = route.expanded_topics
            prerequisite_topics = route.prerequisite_topics
            related_topics = route.related_topics
        elif inferred_subject and "subject" not in routed_filters:
            routed_filters["subject"] = inferred_subject
            if inferred_topics and "topic" not in routed_filters:
                routed_filters["topic"] = inferred_topics[0]

        hits, retrieval_debug = self._retrieve_candidate_hits(
            query=query,
            limit=limit,
            requested_filters=metadata or {},
            routed_filters=routed_filters,
            inferred_subject=inferred_subject,
            inferred_topics=expanded_topics or inferred_topics,
            prerequisite_topics=prerequisite_topics,
            related_topics=related_topics,
        )
        if self.enable_educational_retrieval_engine:
            results = self.retrieval_engine.rank(
                query=query,
                hits=hits,
                limit=limit,
                routed_filters=routed_filters,
                inferred_subject=inferred_subject,
                inferred_topics=expanded_topics or inferred_topics,
                prerequisite_topics=prerequisite_topics,
                related_topics=related_topics,
            )
            for item in results:
                item["text"] = item["text"][:500]
        else:
            results = [
                {
                    "id": str(hit.id),
                    "score": self._lexical_relevance(query, hit),
                    "vector_score": float(hit.score) if hit.score is not None else None,
                    "text": str((hit.payload or {}).get("text", ""))[:500],
                    "metadata": {k: v for k, v in (hit.payload or {}).items() if k != "text"},
                }
                for hit in hits
            ]
        return {
            "query": query,
            "limit": limit,
            "inferred_subject": inferred_subject,
            "inferred_topics": inferred_topics,
            "expanded_topics": expanded_topics,
            "prerequisite_topics": prerequisite_topics,
            "related_topics": related_topics,
            "applied_filters": routed_filters,
            "retrieval_debug": retrieval_debug,
            "results": results,
        }

    def debug_metadata(self, file_path: Path) -> dict[str, Any]:
        raw_text = self.textbook_ingestor.extract_text_from_pdf(file_path)
        base_metadata = self._merge_metadata(file_path, None, raw_text, source="debug")
        chunks = self.textbook_ingestor.ingest_from_path(file_path, raw_text)
        return {
            "file_name": file_path.name,
            "source_path": str(file_path),
            "metadata": base_metadata,
            "chunk_count": len(chunks),
            "preview_topics": self.curriculum_graph.infer_topics_for_query(raw_text),
            "preview_concepts": self.curriculum_graph.infer_concepts_for_text(raw_text),
        }

    def preview_chunks(self, file_path: Path) -> list[dict[str, Any]]:
        raw_text = self.textbook_ingestor.extract_text_from_pdf(file_path)
        base_metadata = self._merge_metadata(file_path, None, raw_text, source="preview")
        chunks = self.textbook_ingestor.ingest_from_path(file_path, raw_text)
        enriched = self._enrich_chunks(chunks, base_metadata)
        return [{"text": chunk["text"][:500], "metadata": chunk["metadata"]} for chunk in enriched]

    def similarity_score(self, left: str, right: str) -> float:
        left_vector = self._load_model().encode([left], normalize_embeddings=True)[0]
        right_vector = self._load_model().encode([right], normalize_embeddings=True)[0]
        return sum(a * b for a, b in zip(left_vector, right_vector, strict=True))

    def curriculum_search(self, query: str, limit: int = 1) -> dict[str, Any]:
        """Run previous (fallback) and curriculum-aware retrieval and return both.

        Returns a dict containing 'previous' and 'curriculum' results plus
        routing metadata (candidate_chapters, confidence).
        """
        previous = self._search(query, limit, None)

        candidates, confidence, inferred_subject = self.concept_index.route_query_to_chapters(query, self.curriculum_graph)

        if candidates and confidence >= 0.6:
            # restrict to the top candidate chapter
            chapter = candidates[0]
            filters: dict[str, Any] = {"chapter": chapter}
            if inferred_subject:
                filters["subject"] = inferred_subject
            curriculum_results = self._search(query, limit, filters)
        else:
            curriculum_results = previous

        return {
            "query": query,
            "candidate_chapters": candidates,
            "confidence": float(confidence),
            "inferred_subject": inferred_subject,
            "previous": previous.model_dump() if hasattr(previous, "model_dump") else previous,
            "curriculum": curriculum_results.model_dump() if hasattr(curriculum_results, "model_dump") else curriculum_results,
        }

    def build_pack_preview(self, chunks: list[dict[str, Any]], pack_name: str = "curriculum_pack") -> dict[str, Any]:
        if not chunks:
            manifest = PackManifest(pack_id=f"{pack_name}-empty", pack_name=pack_name, version="0.1.0")
            metadata = PackMetadata()
            return {"manifest": manifest.model_dump(), "metadata": metadata.model_dump(), "resources": []}

        first = chunks[0]["metadata"]
        topics: list[str] = []
        resource_types = ["experiment", "simulation", "animation", "diagram", "virtual_lab", "quiz", "html_interactive"]
        for chunk in chunks:
            for topic in chunk["metadata"].get("topics", []):
                if topic not in topics:
                    topics.append(topic)

        manifest = PackManifest(
            pack_id=f"{_sanitize_pack_id(pack_name)}-{_sanitize_pack_id(first.get('grade'))}-{_sanitize_pack_id(first.get('subject'))}",
            pack_name=pack_name,
            version="0.1.0",
            grade=first.get("grade"),
            subject=normalize_curriculum_name(str(first.get("subject"))) if first.get("subject") is not None else None,
            chapter=normalize_curriculum_name(str(first.get("chapter"))) if first.get("chapter") is not None else None,
            language=normalize_language_code(str(first.get("language"))) if first.get("language") is not None else None,
            file_count=1,
            chunk_count=len(chunks),
        )
        metadata = PackMetadata(
            grade=first.get("grade"),
            subject=normalize_curriculum_name(str(first.get("subject"))) if first.get("subject") is not None else None,
            chapter=normalize_curriculum_name(str(first.get("chapter"))) if first.get("chapter") is not None else None,
            language=normalize_language_code(str(first.get("language"))) if first.get("language") is not None else None,
            source=first.get("source"),
            curriculum_topics=topics,
            resource_types=resource_types,
        )
        resources = [
            EducationalResource(
                resource_type="diagram",
                topic=topic,
                grade_range=[first.get("grade")] if first.get("grade") is not None else [],
                offline_supported=True,
                interactive=False,
                source=first.get("source"),
            ).model_dump()
            for topic in topics[:5]
        ]
        return {"manifest": manifest.model_dump(), "metadata": metadata.model_dump(), "resources": resources}

    def build_learning_pack_preview(self, chunks: list[dict[str, Any]], pack_name: str = "curriculum_pack") -> dict[str, Any]:
        pack_preview = self.build_pack_preview(chunks, pack_name)
        if not chunks:
            empty_summary = self.summary_generator.generate([], chapter=None, topic=None)
            empty_glossary: list[dict[str, Any]] = []
            empty_quizzes: list[dict[str, Any]] = []
            empty_flashcards: list[dict[str, Any]] = []
            empty_enrichment: list[dict[str, Any]] = []
            empty_quality = self.quality_evaluator.evaluate([], empty_quizzes, empty_glossary)
            pack = self.pack_compiler.compile(pack_name, [], [empty_summary], empty_glossary, empty_quizzes, empty_flashcards, empty_enrichment)
            return {
                **pack_preview,
                "summary": empty_summary,
                "glossary": empty_glossary,
                "quizzes": empty_quizzes,
                "flashcards": empty_flashcards,
                "enrichment": empty_enrichment,
                "quality": empty_quality,
                "compiled_pack": pack,
            }

        first_metadata = chunks[0].get("metadata", {})
        chapter = first_metadata.get("chapter") or pack_name
        topic = (first_metadata.get("topics") or [first_metadata.get("topic") or None])[0]
        summary = self.summary_generator.generate(chunks, chapter=chapter, topic=topic)
        glossary = self.glossary_extractor.extract(chunks)
        quizzes = self.quiz_generator.generate(chunks)
        flashcards = self.flashcard_generator.generate(chunks)
        enrichment_context = self.enrichment_router.route(
            topic=topic or chapter,
            grade=first_metadata.get("grade"),
            subject=first_metadata.get("subject"),
        )
        quality = self.quality_evaluator.evaluate(chunks, quizzes, glossary)
        compiled = self.pack_compiler.compile(
            pack_name=pack_name,
            chunks=chunks,
            summaries=[summary],
            glossary=glossary,
            quizzes=quizzes,
            flashcards=flashcards,
            enrichment=enrichment_context["resources"],
        )

        return {
            **pack_preview,
            "summary": summary,
            "glossary": glossary,
            "quizzes": quizzes,
            "flashcards": flashcards,
            "enrichment": enrichment_context,
            "quality": quality,
            "compiled_pack": compiled,
        }


pipeline = Pipeline()
report_renderer = ReportRenderer()


@app.on_event("startup")
async def startup() -> None:
    try:
        pipeline.ensure_ready()
    except Exception as exc:
        logger.warning("Qdrant unavailable during startup: %s - running in degraded mode", str(exc)[:100])
    if pipeline.auto_ingestion_service is not None:
        await pipeline.auto_ingestion_service.startup()


@app.on_event("shutdown")
async def shutdown() -> None:
    if pipeline.auto_ingestion_service is not None:
        await pipeline.auto_ingestion_service.shutdown()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    checks: dict[str, Any] = {"service": "ok"}
    try:
        pipeline.ensure_ready()
        checks["qdrant"] = {"status": "ok", "collection": pipeline.collection_name}
        checks["curriculum_graph"] = {
            "status": "ok",
            "grades": len(pipeline.curriculum_graph.grades),
            "path": str(pipeline.curriculum_graph_path),
        }
        return HealthResponse(status="ok", service="content-pipeline", checks=checks)
    except Exception as exc:
        checks["qdrant"] = {"status": "error", "detail": str(exc)}
        return HealthResponse(status="degraded", service="content-pipeline", checks=checks)


@app.post("/ingest/pdf", response_model=IngestResponse, dependencies=[Depends(require_admin)])
async def ingest_pdf(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default=None),
) -> IngestResponse:
    try:
        metadata_model = Metadata.model_validate_json(metadata)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid metadata: {exc}") from exc

    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    safe_name = validate_pdf_upload(file.filename, content)
    return await asyncio.to_thread(pipeline.ingest_pdf, safe_name, content, metadata_model, source)


@app.post("/ingest/textbook", response_model=IngestResponse, dependencies=[Depends(require_admin)])
async def ingest_textbook(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default="textbook"),
) -> IngestResponse:
    try:
        metadata_model = Metadata.model_validate_json(metadata)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid metadata: {exc}") from exc
    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    safe_name = validate_pdf_upload(file.filename, content)
    return await asyncio.to_thread(pipeline.ingest_pdf, safe_name, content, metadata_model, source)

try:
    from pydantic import BaseModel
except Exception:
    pass

class IngestGeneratedPackRequest(BaseModel):
    pack_id: str

@app.post("/ingest/generated-pack", dependencies=[Depends(require_admin)])
async def ingest_generated_pack(request: IngestGeneratedPackRequest) -> dict:
    try:
        ingestor = GeneratedPackIngestor(pipeline)
        return await asyncio.to_thread(ingestor.ingest_pack, request.pack_id)
    except Exception as e:
        logger.exception("Generated pack ingestion failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest/directory", response_model=DirectoryIngestResponse, dependencies=[Depends(require_admin)])
async def ingest_directory(request: DirectoryIngestRequest) -> dict[str, Any]:
    directory = pipeline._resolve_content_path(request.directory)
    return await asyncio.to_thread(
        pipeline.ingest_textbook_directory,
        directory,
        request.recursive,
        request.source,
        request.restart,
    )


@app.get("/ingest/status", response_model=DirectoryIngestStatus)
async def ingest_status(directory: str, recursive: bool = True) -> dict[str, Any]:
    def _status() -> dict[str, Any]:
        resolved = pipeline._resolve_content_path(directory)
        pattern = "**/*.pdf" if recursive else "*.pdf"
        files = sorted(resolved.glob(pattern))
        state = pipeline._load_ingest_checkpoint(resolved, recursive)
        completed = state["completed"]
        done = sum(1 for f in files if str(f) in completed)
        return {
            "directory": str(resolved),
            "collection": pipeline.collection_name,
            "total_files": len(files),
            "completed_files": done,
            "remaining_files": len(files) - done,
            "complete": len(files) > 0 and done >= len(files),
            "updated_at": state.get("updated_at"),
        }

    return await asyncio.to_thread(_status)


@app.post("/admin/reset-rag", dependencies=[Depends(require_admin)])
async def reset_rag() -> dict[str, Any]:
    return await asyncio.to_thread(pipeline.reset_rag_state)


@app.post("/rag/search", response_model=SearchResponse)
async def rag_search(request: SearchRequest) -> SearchResponse:
    filters = request.metadata.model_dump(exclude_none=True) if request.metadata else None
    limit = max(1, min(request.limit, 50))
    return await asyncio.to_thread(pipeline._search, request.query, limit, filters)


@app.post("/rag/curriculum_search")
async def rag_curriculum_search(request: SearchRequest) -> dict[str, Any]:
    # Expose curriculum-aware retrieval that compares previous vs new results
    limit = max(1, min(request.limit, 10))
    return await asyncio.to_thread(pipeline.curriculum_search, request.query, limit)


@app.get("/rag/chapter", response_model=SearchResponse)
async def rag_chapter(chapter: str, limit: int = 5) -> SearchResponse:
    return await asyncio.to_thread(pipeline._search, chapter, limit, {"chapter": chapter})


@app.get("/rag/subject", response_model=SearchResponse)
async def rag_subject(subject: str, limit: int = 5) -> SearchResponse:
    return await asyncio.to_thread(pipeline._search, subject, limit, {"subject": subject})


@app.get("/rag/metrics")
async def rag_metrics_endpoint() -> dict[str, Any]:
    return _rag_metrics_summary()


@app.get("/debug/curriculum")
async def debug_curriculum() -> dict[str, Any]:
    return {"curriculum": pipeline.curriculum_graph.to_dict(), "path": str(pipeline.curriculum_graph_path)}


@app.get("/debug/curriculum-relations")
async def debug_curriculum_relations() -> dict[str, Any]:
    return {
        "enabled": pipeline.enable_curriculum_graph_engine,
        "path": str(pipeline.curriculum_relation_graph_path),
        "relations": pipeline.relation_graph,
    }


@app.get("/debug/metadata")
async def debug_metadata(path: str) -> dict[str, Any]:
    file_path = pipeline._resolve_content_path(path)
    return await asyncio.to_thread(pipeline.debug_metadata, file_path)


@app.get("/debug/chunks")
async def debug_chunks(path: str) -> dict[str, Any]:
    file_path = pipeline._resolve_content_path(path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)
    return {"path": str(file_path), "chunk_count": len(chunks), "chunks": chunks}


@app.post("/debug/retrieval")
async def debug_retrieval(request: DebugRetrievalRequest) -> dict[str, Any]:
    metadata = request.metadata.model_dump(exclude_none=True) if request.metadata else None
    return await asyncio.to_thread(pipeline.debug_retrieval, request.query, request.limit, metadata)


@app.get("/debug/similarity")
async def debug_similarity(left: str, right: str) -> dict[str, Any]:
    score = await asyncio.to_thread(pipeline.similarity_score, left, right)
    return {"left": left, "right": right, "score": score}


@app.get("/debug/pack-preview")
async def debug_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    file_path = pipeline._resolve_content_path(path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)
    return await asyncio.to_thread(pipeline.build_pack_preview, chunks, pack_name)


@app.get("/debug/learning-pack-preview")
async def debug_learning_pack_preview(path: str, pack_name: str = "curriculum_pack") -> dict[str, Any]:
    file_path = pipeline._resolve_content_path(path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)
    return await asyncio.to_thread(pipeline.build_learning_pack_preview, chunks, pack_name)


# ── AI-Powered Artifact Generation Endpoints ──────────────────────────────


class GenerateArtifactsRequest(BaseModel):
    path: str
    chapter: str | None = None
    subject: str | None = None
    grade: int | None = None
    language: str | None = None


@app.post("/build/artifacts")
async def build_artifacts(request: GenerateArtifactsRequest) -> dict[str, Any]:
    """Generate AI-powered educational artifacts from ingested PDF chunks."""
    file_path = pipeline._resolve_content_path(request.path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)

    if not chunks:
        raise HTTPException(status_code=404, detail="No chunks found for the given path")

    if request.chapter:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["chapter"] = request.chapter
    if request.subject:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["subject"] = request.subject
    if request.grade is not None:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["grade"] = request.grade
    if request.language:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["language"] = request.language

    artifacts = await pipeline._generate_artifacts(chunks)
    return {
        "path": str(file_path),
        "chunk_count": len(chunks),
        "artifacts": artifacts,
    }


class BuildPackRequest(BaseModel):
    path: str
    pack_name: str = "curriculum_pack"
    chapter: str | None = None
    subject: str | None = None
    grade: int | None = None
    language: str | None = None


@app.post("/build/pack")
async def build_pack(request: BuildPackRequest) -> dict[str, Any]:
    """Ingest a PDF, generate AI artifacts, and compile into an offline pack."""
    file_path = pipeline._resolve_content_path(request.path)

    metadata = Metadata(
        grade=request.grade,
        subject=request.subject,
        chapter=request.chapter,
        language=request.language,
    )

    result = await pipeline._ingest_and_generate_artifacts(
        file_path, metadata=metadata, source="build"
    )
    chunks = result.get("chunks", [])
    artifacts = result.get("artifacts", {})

    pack_manifest = pipeline.pack_compiler.compile(
        pack_name=request.pack_name,
        chunks=chunks,
        summaries=artifacts.get("summaries", []),
        glossary=artifacts.get("glossary", []),
        quizzes=artifacts.get("quizzes", []),
        flashcards=artifacts.get("flashcards", []),
        enrichment=artifacts.get("enrichment", []),
        chapter_notes=artifacts.get("chapter_notes", []),
        learning_objectives=artifacts.get("learning_objectives", []),
        misconceptions=artifacts.get("misconceptions", []),
        applications=artifacts.get("applications", []),
    )

    return {
        "file_name": result.get("file_name"),
        "chunks_created": result.get("chunks_created"),
        "collection": result.get("collection"),
        "artifacts": {
            k: len(v) for k, v in artifacts.items() if isinstance(v, list)
        },
        "pack": pack_manifest,
    }


@app.get("/build/quality/{path:path}")
async def build_quality(path: str) -> dict[str, Any]:
    """Evaluate quality of ingested chunks for a given PDF path."""
    file_path = pipeline._resolve_content_path(path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)
    if not chunks:
        raise HTTPException(status_code=404, detail="No chunks found")

    heuristic_glossary = pipeline.glossary_extractor._heuristic_extract(chunks)
    heuristic_quizzes = await pipeline.quiz_generator._heuristic_generate(chunks, limit=4)
    quality = pipeline.quality_evaluator.evaluate(chunks, heuristic_quizzes, heuristic_glossary)

    return {
        "path": str(file_path),
        "chunk_count": len(chunks),
        "quality": quality,
    }


class AgenticGenerateRequest(BaseModel):
    path: str
    chapter: str | None = None
    subject: str | None = None
    grade: int | None = None
    language: str | None = None
    skip_critique: bool = False
    max_iterations: int | None = None


@app.post("/build/agentic-artifacts")
async def build_agentic_artifacts(request: AgenticGenerateRequest) -> dict[str, Any]:
    """Generate educational artifacts using the agentic orchestrator with
    self-critique and iterative improvement cycles.

    The agent:
    1. Analyzes content (category, density, features)
    2. Plans artifact generation order based on subject
    3. Generates each artifact with subject-aware prompts
    4. Self-critiques output (accuracy, educational value, completeness)
    5. Iteratively improves until quality threshold is met
    """
    file_path = pipeline._resolve_content_path(request.path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)

    if not chunks:
        raise HTTPException(status_code=404, detail="No chunks found")

    if request.chapter:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["chapter"] = request.chapter
    if request.subject:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["subject"] = request.subject
    if request.grade is not None:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["grade"] = request.grade
    if request.language:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["language"] = request.language

    result = await pipeline.artifact_agent.generate_artifacts(
        chunks=chunks,
        max_iterations=request.max_iterations,
        skip_critique=request.skip_critique,
        pdf_path=str(file_path),
    )

    return {
        "path": str(file_path),
        "chunk_count": len(chunks),
        "analysis": result["analysis"],
        "plan": result["plan"],
        "artifacts": result["artifacts"],
        "images": result.get("images", []),
        "formulas": result.get("formulas", []),
        "latex_block": result.get("latex_block", ""),
        "image_dir": result.get("image_dir", ""),
    }


@dataclass
class ReportRequest:
    job_id: str
    duration: int = 0


class EnrichedGenerateRequest(BaseModel):
    path: str
    chapter: str | None = None
    subject: str | None = None
    grade: int | None = None
    language: str | None = None
    skip_critique: bool = False
    max_iterations: int | None = None
    max_topics: int = 8
    include_web: bool = True
    dedupe_across_subject: bool = True
    download_media: bool = True
    persist: bool = True
    fetch_languages: list[str] | None = None


@app.post("/build/enriched-artifacts")
async def build_enriched_artifacts(request: EnrichedGenerateRequest) -> dict[str, Any]:
    """Generate textbook artifacts PLUS per-topic enrichment.

    Enrichment features added on top of the base agentic artifacts:
      1. Related videos (web search: YouTube + open web) per topic.
      2. Real-world examples with explanations (concept-grounded).
      3. Interactive simulations discovered from the OPEN WEB (PhET, OLabs,
         GeoGebra, JavaLab, etc.).

    Topics are the important topics of the chapter, de-duplicated within the
    chapter and across previously processed chapters of the same subject.

    Supports English and Kannada (and other Indian-script) textbooks — the
    source language is auto-detected and threaded through generation and
    resource lookups. This is a NEW agent; ``/build/agentic-artifacts`` and the
    local PhET catalog / EnrichmentRouter endpoint are unchanged.
    """
    file_path = pipeline._resolve_content_path(request.path)
    chunks = await asyncio.to_thread(pipeline.preview_chunks, file_path)

    if not chunks:
        raise HTTPException(status_code=404, detail="No chunks found")

    if request.chapter:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["chapter"] = request.chapter
    if request.subject:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["subject"] = request.subject
    if request.grade is not None:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["grade"] = request.grade
    if request.language:
        for chunk in chunks:
            chunk.setdefault("metadata", {})["language"] = request.language

    result = await pipeline.enriched_content_agent.generate(
        chunks=chunks,
        max_iterations=request.max_iterations,
        skip_critique=request.skip_critique,
        pdf_path=str(file_path),
        max_topics=request.max_topics,
        include_web=request.include_web,
        dedupe_across_subject=request.dedupe_across_subject,
        download_media=request.download_media,
        persist=request.persist,
        fetch_languages=request.fetch_languages,
    )

    return {
        "path": str(file_path),
        "chunk_count": len(chunks),
        "language": result.get("language"),
        "fetch_languages": result.get("fetch_languages", []),
        "analysis": result["analysis"],
        "plan": result["plan"],
        "artifacts": result["artifacts"],
        "images": result.get("images", []),
        "formulas": result.get("formulas", []),
        "latex_block": result.get("latex_block", ""),
        "image_dir": result.get("image_dir", ""),
        "topics": result.get("topics", []),
        "real_world_examples": result.get("real_world_examples", []),
        "topic_enrichment": result.get("topic_enrichment", []),
        "enrichment_summary": result.get("enrichment_summary", {}),
    }


@app.get("/build/report/{job_id}")
async def build_report(job_id: str, duration: int = 0) -> HTMLResponse:
    """Render HTML report for a completed pipeline job."""
    try:
        html = report_renderer.render_job(job_id, duration=duration)
        return HTMLResponse(content=html, status_code=200)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


@app.get("/reports")
async def list_reports() -> list[dict]:
    """List all completed pipeline jobs with their status."""
    return report_renderer.list_jobs()
