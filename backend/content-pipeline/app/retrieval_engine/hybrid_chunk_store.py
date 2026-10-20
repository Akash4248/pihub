from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from shared.text_normalization import normalize_curriculum_name


TOKEN_RE = re.compile(r"[\w\u0900-\u097F\u0C80-\u0CFF']+")


@dataclass(slots=True)
class RetrievalHit:
    id: str
    score: float
    payload: dict[str, Any]
    source: str = "hybrid"


class HybridChunkStore:
    """Local retrieval cache with dense + lexical scoring.

    The store keeps an in-memory index and a JSON snapshot on disk so retrieval
    stays fast across restarts without depending on the vector DB for candidate
    generation.
    """

    def __init__(self, snapshot_path: Path, model_name: str, device: str, cache_dir: str | None = None) -> None:
        self.snapshot_path = snapshot_path
        self.model_name = model_name
        self.device = device
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._model: Any | None = None
        self._model_ready = False
        self._documents: dict[str, dict[str, Any]] = {}
        self._postings: dict[str, set[str]] = defaultdict(set)
        self._doc_freq: Counter[str] = Counter()
        self._avg_doc_len = 0.0
        self._loaded_snapshot = False

    def is_ready(self) -> bool:
        return self._loaded_snapshot or bool(self._documents)

    def load_snapshot(self) -> bool:
        if self._loaded_snapshot:
            return True
        if not self.snapshot_path.exists():
            self._loaded_snapshot = True
            return False
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            self._loaded_snapshot = True
            return False

        self._documents.clear()
        for item in payload.get("documents", []) or []:
            chunk_id = str(item.get("chunk_id") or "")
            if not chunk_id:
                continue
            self._documents[chunk_id] = item

        self._rebuild_statistics()
        self._loaded_snapshot = True
        return bool(self._documents)

    def save_snapshot(self) -> None:
        import os
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_name": self.model_name,
            "device": self.device,
            "documents": list(self._documents.values()),
        }
        tmp_path = self.snapshot_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.snapshot_path)
        try:
            dir_fd = os.open(str(self.snapshot_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    def export_sqlite_vec(self, sqlite_path: Path) -> int:
        from .sqlite_vec_exporter import SqliteVecExporter
        exporter = SqliteVecExporter()
        return exporter.export_to_sqlite(sqlite_path, list(self._documents.values()))

    def clear(self) -> None:
        self._documents.clear()
        self._postings = defaultdict(set)
        self._doc_freq = Counter()
        self._avg_doc_len = 0.0
        self._loaded_snapshot = True
        if self.snapshot_path.exists():
            self.snapshot_path.unlink()

    def upsert(self, chunks: list[dict[str, Any]], vectors: list[list[float]] | None = None) -> None:
        if not chunks:
            return

        if vectors is not None and len(vectors) != len(chunks):
            raise ValueError(f"Precomputed vectors length ({len(vectors)}) does not match chunks length ({len(chunks)})")

        documents: list[dict[str, Any]] = []
        indexed_texts: list[str] = []
        for chunk in chunks:
            document = self._document_for_chunk(chunk, vector=False)
            if not document["chunk_id"]:
                continue
            documents.append(document)
            indexed_texts.append(document["indexed_text"])

        if vectors is None:
            model = self._load_model()
            if model is not None and indexed_texts:
                encoded = model.encode(indexed_texts, normalize_embeddings=True)
                if hasattr(encoded, "tolist"):
                    encoded = encoded.tolist()
                computed_vectors = [[float(value) for value in row] for row in encoded]
            else:
                computed_vectors = []
        else:
            computed_vectors = vectors

        for index, document in enumerate(documents):
            if index < len(computed_vectors):
                document["vector"] = computed_vectors[index]
            self._documents[document["chunk_id"]] = document
        self._rebuild_statistics()
        self.save_snapshot()


    def search(
        self,
        query: str,
        limit: int,
        filters: dict[str, Any] | None = None,
        boost_terms: Iterable[str] | None = None,
    ) -> list[RetrievalHit]:
        if not self._documents:
            self.load_snapshot()
        if not self._documents:
            return []

        query_terms = self._tokenize(" ".join([query, *list(boost_terms or [])]))
        candidate_ids: set[str] = set()
        for term in query_terms:
            candidate_ids.update(self._postings.get(term, set()))

        if not candidate_ids:
            candidate_ids = set(self._documents.keys())

        query_vector = self._encode(f"{query} {' '.join(boost_terms or [])}".strip())
        if not query_terms and not query_vector:
            return []
        scores: list[RetrievalHit] = []
        for chunk_id in candidate_ids:
            document = self._documents.get(chunk_id)
            if not document:
                continue
            if filters and not self._matches_filters(document.get("metadata", {}), filters):
                continue
            lexical = self._bm25(query_terms, document)
            dense = self._cosine_similarity(query_vector, document.get("vector") or [])
            score = self._fuse_scores(dense, lexical, document.get("metadata", {}), filters)
            if score <= 0:
                continue
            payload = {"text": document["text"], **(document.get("metadata") or {})}
            scores.append(RetrievalHit(id=chunk_id, score=score, payload=payload, source="local_hybrid"))

        scores.sort(key=lambda item: item.score, reverse=True)
        return scores[:limit]

    def _document_for_chunk(self, chunk: dict[str, Any], vector: bool = True) -> dict[str, Any]:
        metadata = dict(chunk.get("metadata") or {})
        text = str(chunk.get("text") or "")
        chunk_id = str(chunk.get("chunk_id") or metadata.get("chunk_id") or self._stable_chunk_id(text, metadata))
        indexed_text = self._indexed_text(text, metadata)
        tokens = self._tokenize(indexed_text)
        return {
            "chunk_id": chunk_id,
            "text": text,
            "metadata": metadata,
            "tokens": tokens,
            "term_counts": dict(Counter(tokens)),
            "length": len(tokens),
            "indexed_text": indexed_text,
            "vector": self._encode(indexed_text) if vector else [],
        }

    def _rebuild_statistics(self) -> None:
        self._postings = defaultdict(set)
        self._doc_freq = Counter()
        total_length = 0
        for document in self._documents.values():
            tokens = list(document.get("tokens") or [])
            total_length += len(tokens)
            seen = set(tokens)
            for token in seen:
                self._doc_freq[token] += 1
                self._postings[token].add(str(document["chunk_id"]))
        self._avg_doc_len = total_length / max(1, len(self._documents))

    def _indexed_text(self, text: str, metadata: dict[str, Any]) -> str:
        parts = [
            str(metadata.get("grade") or ""),
            str(metadata.get("subject") or ""),
            str(metadata.get("chapter") or ""),
            str(metadata.get("section") or ""),
            str(metadata.get("topic") or ""),
            " ".join(metadata.get("topics", [])) if isinstance(metadata.get("topics"), list) else str(metadata.get("topics") or ""),
            " ".join(metadata.get("concepts", [])) if isinstance(metadata.get("concepts"), list) else str(metadata.get("concepts") or ""),
            " ".join(metadata.get("keywords", [])) if isinstance(metadata.get("keywords"), list) else str(metadata.get("keywords") or ""),
            text,
        ]
        return " ".join(part for part in parts if part).strip()

    def _matches_filters(self, metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, expected in filters.items():
            if expected in (None, "", []):
                continue
            value = metadata.get(key)
            if key in {"chapter", "subject", "language"}:
                if normalize_curriculum_name(str(value)) != normalize_curriculum_name(str(expected)):
                    return False
                continue
            if key == "grade":
                if str(value) != str(expected):
                    return False
                continue
            if key in {"topic", "topics", "concept", "concepts", "key_terms", "keywords"}:
                haystack = value if isinstance(value, list) else [value]
                normalized = {normalize_curriculum_name(str(item)) for item in haystack if item}
                needles = expected if isinstance(expected, list) else [expected]
                if not any(normalize_curriculum_name(str(item)) in normalized for item in needles if item):
                    return False
                continue
        return True

    def _fuse_scores(
        self,
        dense: float,
        lexical: float,
        metadata: dict[str, Any],
        filters: dict[str, Any] | None,
    ) -> float:
        score = 0.55 * dense + 0.35 * lexical
        if filters:
            for key in ("chapter", "subject", "grade", "language"):
                expected = filters.get(key)
                if expected in (None, ""):
                    continue
                if key == "grade" and str(metadata.get(key)) == str(expected):
                    score += 0.10
                elif key != "grade" and normalize_curriculum_name(str(metadata.get(key))) == normalize_curriculum_name(str(expected)):
                    score += 0.10
        if metadata.get("chunk_type") in {"definition", "formula", "example", "qa", "explanation"}:
            score += 0.05
        return round(score, 6)

    def _bm25(self, query_terms: list[str], document: dict[str, Any]) -> float:
        if not query_terms:
            return 0.0
        term_counts = document.get("term_counts") or {}
        length = max(1, int(document.get("length") or 0))
        k1 = 1.5
        b = 0.75
        score = 0.0
        for term in query_terms:
            tf = int(term_counts.get(term, 0))
            if tf <= 0:
                continue
            df = self._doc_freq.get(term, 0)
            if df <= 0:
                continue
            idf = math.log(1.0 + ((len(self._documents) - df + 0.5) / (df + 0.5)))
            denom = tf + k1 * (1.0 - b + b * (length / max(self._avg_doc_len, 1e-6)))
            score += idf * ((tf * (k1 + 1.0)) / denom)
        return score

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True))

    def _tokenize(self, text: str) -> list[str]:
        return [token.lower() for token in TOKEN_RE.findall(text.lower()) if token]

    def _encode(self, text: str) -> list[float]:
        if not text:
            return []
        model = self._load_model()
        if model is None:
            return []
        embeddings = model.encode([text], normalize_embeddings=True)
        if hasattr(embeddings, "tolist"):
            embeddings = embeddings.tolist()
        vector = embeddings[0] if len(embeddings) > 0 else []
        return [float(value) for value in vector]

    def _load_model(self) -> Any | None:
        if self._model_ready:
            return self._model
        self._model_ready = True
        try:
            from shared.embedding_service import EmbeddingModelManager
            self._model = EmbeddingModelManager.get_instance().get_model()
        except Exception:
            self._model = None
        return self._model


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

    @staticmethod
    def _stable_chunk_id(text: str, metadata: dict[str, Any]) -> str:
        payload = json.dumps({"text": text[:4000], "metadata": metadata}, ensure_ascii=False, sort_keys=True)
        import hashlib

        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
