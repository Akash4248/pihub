from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, List, Optional

from shared.config import get_settings

logger = logging.getLogger(__name__)


class HashFallbackEmbeddingModel:
    """Lightweight hash fallback vectorizer used ONLY when EMBEDDING_FALLBACK=hash in CI/tests."""

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def encode(
        self,
        sentences: Any,
        batch_size: int = 32,
        show_progress_bar: bool = False,
        normalize_embeddings: bool = False,
    ) -> List[List[float]]:
        if isinstance(sentences, str):
            input_list = [sentences]
        else:
            input_list = list(sentences)

        vectors: List[List[float]] = []
        for text in input_list:
            v = [0.0] * self.dimension
            tokens = str(text or "").split()
            if tokens:
                for token in tokens:
                    idx = int(hashlib.blake2b(token.encode("utf-8"), digest_size=4).hexdigest(), 16) % self.dimension
                    v[idx] += 1.0
                norm = sum(x * x for x in v) ** 0.5
                if norm > 0:
                    v = [x / norm for x in v]
            vectors.append(v)
        return vectors

    def get_embedding_dimension(self) -> int:
        return self.dimension


class LlamaServerEmbeddingModel:
    """BGE-M3 (or compatible) embeddings via llama.cpp `/v1/embeddings`.

    No torch/transformers required — works anywhere llama-server runs
    (laptop, server, Pi). Same `encode` interface as SentenceTransformer.
    """

    def __init__(
        self,
        base_url: str,
        model: str = "bge-m3",
        timeout_seconds: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self._client: Any = None
        self._dimension: Optional[int] = None

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        payload: dict[str, Any] = {"input": texts}
        if self.model:
            payload["model"] = self.model
        try:
            response = self._get_client().post("/v1/embeddings", json=payload)
        except Exception as exc:
            raise RuntimeError(f"llama-server embeddings unreachable at {self.base_url}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"llama-server embeddings failed ({response.status_code}): {response.text[:300]}"
            )
        try:
            items = sorted(response.json()["data"], key=lambda d: d["index"])
            vectors = [list(map(float, item["embedding"])) for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Malformed embeddings response from {self.base_url}: {exc}") from exc
        if not vectors or any(len(v) != len(vectors[0]) for v in vectors):
            raise RuntimeError(f"Inconsistent embedding dims from {self.base_url}")
        if self._dimension is None:
            self._dimension = len(vectors[0])
        elif len(vectors[0]) != self._dimension:
            raise RuntimeError(
                f"Embedding dim changed mid-run ({self._dimension} -> {len(vectors[0])})"
            )
        return vectors

    def encode(
        self,
        sentences: Any,
        batch_size: int = 32,
        show_progress_bar: bool = False,
        normalize_embeddings: bool = False,
    ) -> List[List[float]]:
        if isinstance(sentences, str):
            input_list = [sentences]
        else:
            input_list = [str(t or "") for t in sentences]
        step = max(1, batch_size or self.batch_size)
        vectors: List[List[float]] = []
        for start in range(0, len(input_list), step):
            vectors.extend(self._embed_batch(input_list[start:start + step]))
        if normalize_embeddings:
            normalized: List[List[float]] = []
            for v in vectors:
                norm = sum(x * x for x in v) ** 0.5
                normalized.append([x / norm for x in v] if norm > 0 else v)
            return normalized
        return vectors

    def get_sentence_embedding_dimension(self) -> int:
        if self._dimension is None:
            self.encode(["dimension probe"])
        assert self._dimension is not None
        return self._dimension

    def get_embedding_dimension(self) -> int:
        return self.get_sentence_embedding_dimension()


class EmbeddingModelManager:
    """Thread-safe lazy singleton for embeddings.

    Backend selection via EMBEDDING_BACKEND ("auto"|"llama_server"|"sentence_transformers"):
    - auto: llama-server first, sentence-transformers second (zero-downtime cutover).
    - Either explicit value pins that backend (failure raises, or hashes iff EMBEDDING_FALLBACK=hash).

    Features:
    - Double-checked locking via threading.Lock() to load weights exactly once.
    - Dynamic vector dimension detection via get_embedding_dimension().
    - Configurable fallback (EMBEDDING_FALLBACK="none"|"hash").
    """

    _instance: Optional[EmbeddingModelManager] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model: Any = None
        self._model_name: Optional[str] = None
        self._dimension: Optional[int] = None
        self._is_degraded: bool = False
        self._load_error: Optional[str] = None

    @classmethod
    def get_instance(cls) -> EmbeddingModelManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = EmbeddingModelManager()
        return cls._instance

    def get_model(self) -> Any:
        if self._model is None and not self._is_degraded:
            with self._lock:
                if self._model is None and not self._is_degraded:
                    self._load_model()
        if self._is_degraded and self._model is None:
            settings = get_settings()
            fallback_mode = getattr(settings, "embedding_fallback", "none").lower()
            if fallback_mode == "hash":
                logger.warning("Using HashFallbackEmbeddingModel (EMBEDDING_FALLBACK=hash)")
                self._model = HashFallbackEmbeddingModel(dimension=384)
                self._dimension = 384
            else:
                raise RuntimeError(f"Embedding model load failed: {self._load_error}")
        return self._model

    def _load_model(self) -> None:
        settings = get_settings()
        backend = getattr(settings, "embedding_backend", "auto").lower()
        if backend == "llama_server":
            self._load_llama_server(settings)
        elif backend == "sentence_transformers":
            self._load_sentence_transformers(settings)
        else:
            try:
                self._load_llama_server(settings)
            except Exception as exc:
                logger.warning("llama-server embeddings unavailable (%s); trying sentence-transformers", exc)
                self._load_sentence_transformers(settings)

    def _load_llama_server(self, settings: Any) -> None:
        base_url = getattr(settings, "llama_server_url", "http://127.0.0.1:8081")
        model_name = getattr(settings, "embedding_llama_model", "bge-m3")
        logger.info("Using llama-server embeddings at %s (model=%s)...", base_url, model_name)
        model = LlamaServerEmbeddingModel(base_url=base_url, model=model_name)
        self._dimension = model.get_sentence_embedding_dimension()
        self._model = model
        self._model_name = f"llama-server:{model_name}"
        self._is_degraded = False
        logger.info("llama-server embeddings ready (dim=%d)", self._dimension)

    def _load_sentence_transformers(self, settings: Any) -> None:
        model_name = settings.embedding_model_name
        cache_dir = settings.embedding_cache_dir

        logger.info("Loading embedding model %s from cache %s...", model_name, cache_dir)
        try:
            from sentence_transformers import SentenceTransformer
            from huggingface_hub import snapshot_download

            local_path = cache_dir
            if os.path.exists(cache_dir) and os.listdir(cache_dir):
                local_path = cache_dir
            else:
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    local_path = snapshot_download(
                        repo_id=model_name,
                        local_dir=cache_dir,
                        local_dir_use_symlinks=False,
                    )
                except Exception as dl_err:
                    logger.warning("Snapshot download failed for %s: %s", model_name, dl_err)
                    local_path = model_name

            model = SentenceTransformer(local_path, device=settings.embedding_device)
            dim_fn = getattr(model, "get_embedding_dimension", None) or getattr(model, "get_sentence_embedding_dimension", None)
            self._dimension = dim_fn() if dim_fn else 1024
            self._model = model
            self._model_name = model_name
            self._is_degraded = False
            logger.info("Successfully loaded embedding model %s (dim=%d)", model_name, self._dimension)
        except Exception as exc:
            self._is_degraded = True
            self._load_error = str(exc)
            logger.error("Failed to load embedding model %s: %s", model_name, exc)
            fallback_mode = getattr(settings, "embedding_fallback", "none").lower()
            if fallback_mode == "hash":
                logger.warning("Falling back to HashFallbackEmbeddingModel (EMBEDDING_FALLBACK=hash)")
                self._model = HashFallbackEmbeddingModel(dimension=384)
                self._dimension = 384
            else:
                self._model = None

    def get_dimension(self) -> int:
        if self._dimension is not None:
            return self._dimension
        model = self.get_model()
        if hasattr(model, "get_embedding_dimension"):
            return model.get_embedding_dimension()
        return 1024

    @property
    def is_degraded(self) -> bool:
        return self._is_degraded
