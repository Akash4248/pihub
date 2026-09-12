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


class EmbeddingModelManager:
    """Thread-safe lazy singleton for BGE-M3 sentence transformers.

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
