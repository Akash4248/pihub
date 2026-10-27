"""Gateway-side retrieval metrics registry."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from shared.topic_normalization import topic_aliases


RETRIEVAL_METRICS: deque[dict[str, Any]] = deque(maxlen=200)


def record_retrieval_metric(metric: dict[str, Any]) -> None:
    RETRIEVAL_METRICS.appendleft({
        "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
        **metric,
    })


def retrieval_summary() -> dict[str, Any]:
    metrics = list(RETRIEVAL_METRICS)
    if not metrics:
        return {
            "count": 0,
            "averages": {},
            "recent": [],
            "topic_aliases": topic_aliases(),
        }
    numeric_keys = ("asset_ms", "rag_ms", "total_ms", "asset_matches", "chunks_retrieved", "chunks_used")
    averages = {
        key: round(sum(float(metric.get(key, 0.0)) for metric in metrics) / len(metrics), 2)
        for key in numeric_keys
    }
    return {
        "count": len(metrics),
        "averages": averages,
        "recent": metrics[:40],
        "topic_aliases": topic_aliases(),
    }
