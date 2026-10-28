from __future__ import annotations

import hashlib
import re
from collections import Counter
from math import ceil
from typing import Any


WORD_RE = re.compile(r"[A-Za-z0-9]+")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
RAG_TYPES = {"concept", "example", "worked_example", "summary", "glossary", "formula_explanation", "tutor_context", "concept_context"}
PREFERRED_MIN = 250
PREFERRED_MAX = 350
ALLOWED_MIN = 200
ALLOWED_MAX = 400
SPLIT_AT = 450


class ChunkNormalizer:
    """Normalize educational chunks into the publication gate's word-count band."""

    def normalize(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ordered_rows = [self._with_order(row, index) for index, row in enumerate(rows)]
        non_rag = [row for row in ordered_rows if not self._rag_eligible(row)]
        rag = [row for row in ordered_rows if self._rag_eligible(row)]
        grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
        for row in rag:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            key = (metadata.get("grade"), metadata.get("subject"), metadata.get("chapter"))
            grouped.setdefault(key, []).append(row)

        normalized: list[dict[str, Any]] = []
        merge_count = 0
        split_count = 0
        for group in grouped.values():
            merged, group_merges = self._merge_short_rows(group)
            merge_count += group_merges
            for row in merged:
                pieces = self._split_large_row(row)
                if len(pieces) > 1:
                    split_count += 1
                normalized.extend(pieces)

        normalized, forced_report = self._force_allowed_range(normalized)

        output = sorted([*non_rag, *normalized], key=self._order)
        lengths = [word_count(row.get("text")) for row in normalized]
        class_counts = Counter(str(row.get("metadata", {}).get("quality_class") or "") for row in output)
        report = {
            "total_chunks": len(output),
            "rag_chunks": len(normalized),
            "input_rag_chunks": len(rag),
            "output_rag_chunks": len(normalized),
            "chunks_merged": merge_count,
            "chunks_split": split_count,
            "forced_chunks_merged": forced_report["forced_chunks_merged"],
            "forced_chunks_split": forced_report["forced_chunks_split"],
            "contextual_expansions": forced_report["contextual_expansions"],
            "short_chunks_removed": forced_report["short_chunks_removed"],
            "chunks_below_200": sum(1 for value in lengths if value < ALLOWED_MIN),
            "chunks_above_400": sum(1 for value in lengths if value > ALLOWED_MAX),
            "chunks_in_preferred_250_350": sum(1 for value in lengths if PREFERRED_MIN <= value <= PREFERRED_MAX),
            "average_chunk_length": round(sum(lengths) / max(1, len(lengths)), 2),
            "minimum_chunk_length": min(lengths) if lengths else 0,
            "maximum_chunk_length": max(lengths) if lengths else 0,
            "quality_class_counts": dict(sorted(class_counts.items())),
            "content_type_counts": dict(sorted(Counter(str(row.get("metadata", {}).get("content_type")) for row in normalized).items())),
        }
        return [self._strip_order(row) for row in output], report

    def _force_allowed_range(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        split_rows: list[dict[str, Any]] = []
        forced_splits = 0
        for row in rows:
            if word_count(row.get("text")) > ALLOWED_MAX:
                pieces = self._split_by_word_window(row)
                forced_splits += max(0, len(pieces) - 1)
                split_rows.extend(pieces)
            else:
                split_rows.append(row)

        rows = sorted(split_rows, key=self._order)
        output: list[dict[str, Any]] = []
        forced_merges = 0
        for row in rows:
            if word_count(row.get("text")) >= ALLOWED_MIN:
                output.append(row)
                continue
            if output and self._compatible(output[-1], row):
                candidate_words = word_count("\n\n".join([str(output[-1].get("text") or ""), str(row.get("text") or "")]))
                if candidate_words <= ALLOWED_MAX:
                    output[-1] = self._merge_rows([output[-1], row])
                    forced_merges += 1
                    continue
            output.append(row)

        contextual_expansions = 0
        for index, row in enumerate(output):
            if word_count(row.get("text")) >= ALLOWED_MIN:
                continue
            expanded = self._expand_with_context(row)
            if expanded is not row:
                output[index] = expanded
                contextual_expansions += 1

        output, late_merges = self._merge_remaining_short_rows(output)
        forced_merges += late_merges

        final_rows: list[dict[str, Any]] = []
        for row in output:
            if word_count(row.get("text")) > ALLOWED_MAX:
                pieces = self._split_by_word_window(row)
                forced_splits += max(0, len(pieces) - 1)
                final_rows.extend(pieces)
            else:
                final_rows.append(row)
        length_filtered_rows = [row for row in final_rows if word_count(row.get("text")) >= ALLOWED_MIN]
        return length_filtered_rows, {
            "forced_chunks_merged": forced_merges,
            "forced_chunks_split": forced_splits,
            "contextual_expansions": contextual_expansions,
            "short_chunks_removed": len(final_rows) - len(length_filtered_rows),
        }

    def _merge_short_rows(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        output: list[dict[str, Any]] = []
        buffer: list[dict[str, Any]] = []
        merge_count = 0

        for row in rows:
            text_words = word_count(row.get("text"))
            if not buffer:
                buffer = [row]
            else:
                candidate_words = word_count("\n\n".join(str(item.get("text") or "") for item in [*buffer, row]))
                if self._compatible(buffer[-1], row) and candidate_words <= ALLOWED_MAX:
                    buffer.append(row)
                    merge_count += 1
                else:
                    output.extend(self._flush_buffer(buffer))
                    buffer = [row]
            if text_words >= ALLOWED_MIN and len(buffer) == 1:
                output.extend(self._flush_buffer(buffer))
                buffer = []
            elif word_count("\n\n".join(str(item.get("text") or "") for item in buffer)) >= PREFERRED_MIN:
                output.extend(self._flush_buffer(buffer))
                buffer = []

        if buffer:
            if output and self._compatible(output[-1], buffer[0]):
                candidate_words = word_count("\n\n".join([str(output[-1].get("text") or ""), *[str(item.get("text") or "") for item in buffer]]))
                if candidate_words <= ALLOWED_MAX:
                    output[-1] = self._merge_rows([output[-1], *buffer])
                    merge_count += len(buffer)
                else:
                    output.extend(self._flush_buffer(buffer))
            else:
                output.extend(self._flush_buffer(buffer))
        return output, merge_count

    def _flush_buffer(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        if len(rows) == 1:
            return [rows[0]]
        return [self._merge_rows(rows)]

    def _split_large_row(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        text = str(row.get("text") or "")
        if word_count(text) <= SPLIT_AT:
            return [row]
        sentences = [sentence.strip() for sentence in SENTENCE_RE.split(text) if sentence.strip()]
        if len(sentences) <= 1:
            return [row]
        pieces: list[list[str]] = []
        buffer: list[str] = []
        for sentence in sentences:
            candidate = " ".join([*buffer, sentence])
            if buffer and word_count(candidate) > PREFERRED_MAX:
                pieces.append(buffer)
                buffer = []
            buffer.append(sentence)
        if buffer:
            if pieces and word_count(" ".join(buffer)) < ALLOWED_MIN and word_count(" ".join([*pieces[-1], *buffer])) <= ALLOWED_MAX:
                pieces[-1].extend(buffer)
            else:
                pieces.append(buffer)
        if len(pieces) <= 1:
            return [row]
        output = []
        for index, piece in enumerate(pieces, start=1):
            text_piece = " ".join(piece).strip()
            if not text_piece:
                continue
            output.append(self._copy_row(row, text_piece, f"part_{index}"))
        return output or [row]

    def _split_by_word_window(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        words = WORD_RE.findall(str(row.get("text") or ""))
        if len(words) <= ALLOWED_MAX:
            return [row]
        chunk_count = max(2, ceil(len(words) / PREFERRED_MAX))
        size = ceil(len(words) / chunk_count)
        chunks = []
        for index in range(chunk_count):
            start = index * size
            end = min(len(words), start + size)
            if start >= len(words):
                break
            text = " ".join(words[start:end])
            chunks.append(self._copy_row(row, text, f"window_{len(chunks) + 1}"))
        return chunks

    def _expand_with_context(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        package = metadata.get("tutor_context_package") if isinstance(metadata.get("tutor_context_package"), dict) else {}
        additions: list[str] = []
        if package:
            additions.extend(
                str(package.get(field) or "")
                for field in ("definition", "explanation", "why_it_matters", "example")
                if package.get(field)
            )
            for field, label in (
                ("prerequisites", "Prerequisites"),
                ("related_concepts", "Related concepts"),
                ("common_misconceptions", "Common misconceptions"),
                ("real_world_applications", "Real world applications"),
            ):
                values = package.get(field) or []
                if values:
                    additions.append(f"{label}: " + "; ".join(str(value) for value in values[:8]))
        if not additions:
            if metadata.get("explanation"):
                additions.append(str(metadata["explanation"]))
            if metadata.get("why_it_matters"):
                additions.append(str(metadata["why_it_matters"]))
            for field, label in (
                ("learning_objective", "Learning objective"),
                ("prerequisites", "Prerequisites"),
                ("related_concepts", "Related concepts"),
                ("common_misconceptions", "Common misconceptions"),
                ("real_world_applications", "Real world applications"),
                ("key_terms", "Key terms"),
            ):
                values = metadata.get(field)
                if isinstance(values, list) and values:
                    additions.append(f"{label}: " + "; ".join(str(value) for value in values[:10]))
                elif isinstance(values, str) and values:
                    additions.append(f"{label}: {values}")
        if not additions:
            return row
        text = str(row.get("text") or "").strip()
        for addition in additions:
            candidate = f"{text}\n\n{addition}".strip()
            if word_count(candidate) > ALLOWED_MAX:
                continue
            text = candidate
            if word_count(text) >= ALLOWED_MIN:
                break
        if word_count(text) < ALLOWED_MIN:
            return row
        metadata = dict(metadata)
        metadata["normalized_chunk"] = True
        metadata["normalization_action"] = "contextual_expansion"
        return {**row, "chunk_id": f"{row.get('chunk_id')}_expanded_{digest(text)}", "text": text, "metadata": metadata}

    def _merge_remaining_short_rows(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(rows, key=self._order)
        merged = 0
        changed = True
        while changed:
            changed = False
            output: list[dict[str, Any]] = []
            skip_next = False
            for index, row in enumerate(rows):
                if skip_next:
                    skip_next = False
                    continue
                if word_count(row.get("text")) >= ALLOWED_MIN:
                    output.append(row)
                    continue
                next_row = rows[index + 1] if index + 1 < len(rows) else None
                if next_row is not None and self._same_chapter(row, next_row):
                    candidate_words = word_count("\n\n".join([str(row.get("text") or ""), str(next_row.get("text") or "")]))
                    if candidate_words <= ALLOWED_MAX:
                        output.append(self._merge_rows([row, next_row]))
                        skip_next = True
                        merged += 1
                        changed = True
                        continue
                if output and self._same_chapter(output[-1], row):
                    candidate_words = word_count("\n\n".join([str(output[-1].get("text") or ""), str(row.get("text") or "")]))
                    if candidate_words <= ALLOWED_MAX:
                        output[-1] = self._merge_rows([output[-1], row])
                        merged += 1
                        changed = True
                        continue
                output.append(row)
            rows = output
        return rows, merged

    @staticmethod
    def _merge_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        first = rows[0]
        metadata = dict(first.get("metadata") or {})
        source_ids = []
        for row in rows:
            row_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            values = row_meta.get("source_chunk_ids") if isinstance(row_meta.get("source_chunk_ids"), list) else []
            source_ids.extend(values or [row.get("chunk_id")])
        texts = [str(row.get("text") or "").strip() for row in rows if str(row.get("text") or "").strip()]
        merged_text = "\n\n".join(texts)
        metadata["source_chunk_ids"] = [item for item in source_ids if item]
        metadata["normalized_chunk"] = True
        metadata["normalization_action"] = "merge"
        metadata["_normalization_order"] = min(
            (row.get("metadata") or {}).get("_normalization_order", 0)
            for row in rows
            if isinstance(row.get("metadata"), dict)
        )
        return {
            **first,
            "chunk_id": f"normalized_merge_{digest(merged_text)}",
            "text": merged_text,
            "metadata": metadata,
        }

    @staticmethod
    def _copy_row(row: dict[str, Any], text: str, suffix: str) -> dict[str, Any]:
        metadata = dict(row.get("metadata") or {})
        values = metadata.get("source_chunk_ids") if isinstance(metadata.get("source_chunk_ids"), list) else []
        metadata["source_chunk_ids"] = values or [row.get("chunk_id")]
        metadata["normalized_chunk"] = True
        metadata["normalization_action"] = "split"
        return {
            **row,
            "chunk_id": f"{row.get('chunk_id')}_{suffix}_{digest(text)}",
            "text": text,
            "metadata": metadata,
        }

    @staticmethod
    def _compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_meta = left.get("metadata") if isinstance(left.get("metadata"), dict) else {}
        right_meta = right.get("metadata") if isinstance(right.get("metadata"), dict) else {}
        if left_meta.get("chapter") != right_meta.get("chapter"):
            return False
        left_topic = left_meta.get("topic") or left_meta.get("section")
        right_topic = right_meta.get("topic") or right_meta.get("section")
        if left_topic and right_topic and left_topic != right_topic:
            return False
        left_type = str(left_meta.get("content_type") or "")
        right_type = str(right_meta.get("content_type") or "")
        compatible_pairs = {
            ("concept", "example"),
            ("example", "concept"),
            ("concept", "formula_explanation"),
            ("formula_explanation", "concept"),
            ("concept", "tutor_context"),
            ("tutor_context", "concept"),
            ("example", "tutor_context"),
            ("tutor_context", "example"),
            ("formula_explanation", "tutor_context"),
            ("tutor_context", "formula_explanation"),
            ("worked_example", "tutor_context"),
            ("tutor_context", "worked_example"),
            ("worked_example", "example"),
            ("example", "worked_example"),
        }
        return left_type == right_type or (left_type, right_type) in compatible_pairs

    @staticmethod
    def _same_chapter(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_meta = left.get("metadata") if isinstance(left.get("metadata"), dict) else {}
        right_meta = right.get("metadata") if isinstance(right.get("metadata"), dict) else {}
        return (
            left_meta.get("grade"),
            left_meta.get("subject"),
            left_meta.get("chapter"),
        ) == (
            right_meta.get("grade"),
            right_meta.get("subject"),
            right_meta.get("chapter"),
        )

    @staticmethod
    def _rag_eligible(row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return bool(metadata.get("rag_eligible")) and str(metadata.get("content_type") or "") in RAG_TYPES

    @staticmethod
    def _with_order(row: dict[str, Any], index: int) -> dict[str, Any]:
        metadata = dict(row.get("metadata") or {})
        metadata["_normalization_order"] = index
        return {**row, "metadata": metadata}

    @staticmethod
    def _order(row: dict[str, Any]) -> int:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        try:
            return int(metadata.get("_normalization_order") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _strip_order(row: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(row.get("metadata") or {})
        metadata.pop("_normalization_order", None)
        return {**row, "metadata": metadata}


def word_count(text: Any) -> int:
    return len(WORD_RE.findall(str(text or "")))


def digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.lower()).encode("utf-8")).hexdigest()[:12]
