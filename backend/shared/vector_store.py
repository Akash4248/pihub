from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from shared.text_normalization import language_filter_values, normalize_curriculum_name, normalize_language_code


@dataclass(slots=True)
class StoredChunk:
    chunk_id: str
    text: str
    metadata: dict[str, Any]
    score: float | None = None


def make_qdrant_client(url: str) -> QdrantClient:
    if url == ":memory:":
        return QdrantClient(":memory:")
    return QdrantClient(url=url)


def ensure_collection(client: QdrantClient, collection_name: str, vector_size: int, qdrant_url: str | None = None) -> None:
    if qdrant_url:
        try:
            response = httpx.get(f"{qdrant_url.rstrip('/')}/collections/{collection_name}", timeout=10.0)
            if response.status_code == 200:
                payload = response.json()
                existing_size = (
                    payload.get("result", {})
                    .get("config", {})
                    .get("params", {})
                    .get("vectors", {})
                    .get("size")
                )
                if existing_size is not None and int(existing_size) != int(vector_size):
                    raise RuntimeError(
                        f"Qdrant collection {collection_name} already exists with vector size {existing_size}, expected {vector_size}."
                    )
                return
            if response.status_code != 404:
                response.raise_for_status()
        except Exception:
            pass

    try:
        collections = {collection.name for collection in client.get_collections().collections}
        if collection_name in collections:
            return
    except Exception:
        pass

    client.create_collection(
        collection_name=collection_name,
        vectors_config=qmodels.VectorParams(size=vector_size, distance=qmodels.Distance.COSINE),
    )


def upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    embeddings: list[list[float]],
    texts: list[str],
    metadatas: list[dict[str, Any]],
) -> list[str]:
    # Deterministic ids so re-ingesting the same content overwrites instead of
    # duplicating it (random uuid4 previously created duplicate points).
    def _point_id(text: str, metadata: dict[str, Any]) -> str:
        basis = "|".join(str(metadata.get(k, "")) for k in ("grade", "subject", "chapter", "section", "topic"))
        digest = hashlib.sha1(f"{basis}|{text}".encode("utf-8")).hexdigest()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, digest))

    def _coerce_grade(value: Any) -> Any:
        # grade occasionally arrives as a numeric string ("7"); store it as an
        # int so numeric Qdrant filters (grade == 7) work correctly.
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    point_ids = [
        _point_id(text, metadata)
        for text, metadata in zip(texts, metadatas)
    ]
    points = [
            qmodels.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": text,
                    **{
                        key: (
                            normalize_language_code(value)
                            if key == "language" and isinstance(value, str)
                            else _coerce_grade(value)
                            if key == "grade"
                            else normalize_curriculum_name(value)
                            if key in {"subject", "chapter", "topic", "section", "textbook_name"} and isinstance(value, str)
                            else value
                        )
                        for key, value in metadata.items()
                    },
                },
            )
        for point_id, vector, text, metadata in zip(point_ids, embeddings, texts, metadatas, strict=True)
    ]
    client.upsert(collection_name=collection_name, points=points)
    return point_ids


def build_filter(metadata: dict[str, Any] | None = None) -> qmodels.Filter | None:
    if not metadata:
        return None

    conditions: list[qmodels.FieldCondition] = []
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str):
            if key == "language":
                values = language_filter_values(value)
                if len(values) > 1:
                    conditions.append(qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=values)))
                    continue
                value = normalize_language_code(value)
            else:
                value = normalize_curriculum_name(value)
        conditions.append(qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value)))

    if not conditions:
        return None

    return qmodels.Filter(must=conditions)
