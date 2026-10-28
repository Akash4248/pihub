from __future__ import annotations

import re
from collections import Counter
from typing import Any


TOC_HEAD_RE = re.compile(r"\b(contents?|table of contents|index|chapter index|answer key|answers)\b", re.I)
PAGE_LISTING_RE = re.compile(
    r"^\s*(?:chapter|unit|lesson|section)?\s*\d+(?:\.\d+)*\s+.{2,90}(?:\.{2,}|\s{2,})\s*\d{1,4}\s*$",
    re.I,
)
DOT_LEADER_RE = re.compile(r"\.{2,}\s*\d{1,4}\s*$")
NAV_LINE_RE = re.compile(r"^\s*(?:chapter|unit|lesson|section)\s+\d+(?:\.\d+)*\s*$", re.I)


class TocCleanup:
    """Remove navigation-style pages and section listings before semantic chunking."""

    def clean(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        reasons: Counter[str] = Counter()

        for row in rows:
            reason = self._reason(str(row.get("text") or ""), row.get("metadata") if isinstance(row.get("metadata"), dict) else {})
            if reason:
                reasons[reason] += 1
                removed.append(
                    {
                        "chunk_id": row.get("chunk_id"),
                        "reason": reason,
                        "preview": str(row.get("text") or "")[:240],
                    }
                )
                continue
            kept.append(row)

        return kept, {
            "chunks_examined": len(rows),
            "chunks_removed": len(removed),
            "toc_chunks_remaining": sum(1 for row in kept if self._reason(str(row.get("text") or ""), row.get("metadata") if isinstance(row.get("metadata"), dict) else {})),
            "removal_reasons": dict(sorted(reasons.items())),
            "removed_samples": removed[:100],
        }

    def _reason(self, text: str, metadata: dict[str, Any]) -> str | None:
        normalized = normalize(text)
        if not normalized:
            return "empty_navigation"
        if metadata.get("content_type") in {"table_of_contents", "index_page"}:
            return "classified_toc_or_index"
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            return "empty_navigation"
        page_listing_count = sum(1 for line in lines if PAGE_LISTING_RE.search(line) or DOT_LEADER_RE.search(line))
        nav_line_count = sum(1 for line in lines if NAV_LINE_RE.search(line))
        short_nav_lines = sum(1 for line in lines if len(line) <= 80 and re.search(r"\b(chapter|unit|lesson|section|page)\b", line, re.I))

        if TOC_HEAD_RE.search(text):
            if page_listing_count >= 1 or len(lines) <= 45:
                return "toc_heading"
        if len(lines) >= 3 and page_listing_count / len(lines) >= 0.25:
            return "page_listing"
        if len(lines) >= 5 and (page_listing_count + nav_line_count + short_nav_lines) / len(lines) >= 0.5:
            return "navigation_listing"
        if re.search(r"\b(chapter|section)\s+\d+(?:\.\d+)*\b.{0,80}\bpage\s+\d+", normalized) and word_count(text) < 120:
            return "section_page_listing"
        return None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", str(text or "")))
