from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


WORD_RE = re.compile(r"[A-Za-z0-9]+")


HEADER_FOOTER_RE = (
    re.compile(r"^(chapter|unit|lesson)\s+\d+(\s*[:.-].*)?$", re.I),
    re.compile(r"^(mathematics|maths|science|social science|social_science|environmental studies|evs)\s*$", re.I),
    re.compile(r"^(class|grade)\s+\d+\s*$", re.I),
    re.compile(r"^reprint\s+\d{4}(-\d{2})?\s*$", re.I),
    re.compile(r"^fig\.?\s*\d+(\.\d+)?\s*$", re.I),
    re.compile(r"^table\s+\d+(\.\d+)?\s*$", re.I),
)

ISBN_COPYRIGHT_RE = re.compile(
    r"\b(isbn|copyright|all rights reserved|published by|national council of educational research|"
    r"ncert|republication|no part of this publication|printed at)\b",
    re.I,
)


@dataclass(frozen=True)
class ExtractionRepairMetrics:
    input_chunks: int
    output_chunks: int
    chunks_removed: int
    chunks_merged: int
    duplicates_removed: int
    header_footer_removed: int
    scan_artifacts_removed: int
    empty_removed: int


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def is_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"(page\s*(no\.?)?\s*)?\d{1,4}", str(text).strip(), re.I))


def is_header_footer(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    return any(pattern.fullmatch(stripped) for pattern in HEADER_FOOTER_RE)


def is_scan_artifact(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if "\ufffd" in stripped or "\x08" in stripped:
        return True
    if re.search(r"(?:[A-Za-z]\s){5,}[A-Za-z]", stripped):
        return True
    printable = sum(1 for char in stripped if char.isprintable())
    alpha_numeric_space = sum(1 for char in stripped if char.isalnum() or char.isspace())
    return printable > 20 and (printable - alpha_numeric_space) / max(printable, 1) > 0.42


def is_formula_only(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) > 220:
        return False
    symbols = sum(1 for char in stripped if char in "=+-×÷*/^√∠∆π≤≥<>°")
    return symbols >= 2 and word_count(stripped) <= 18


def clean_raw_text(text: str) -> str:
    lines = [line.replace("\u00a0", " ").strip() for line in str(text or "").splitlines()]
    counts = Counter(normalize_text(line) for line in lines if line.strip())
    cleaned_lines: list[str] = []
    for line in lines:
        stripped = re.sub(r"[ \t]+", " ", line).strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        normalized = normalize_text(stripped)
        if is_page_number(stripped) or ISBN_COPYRIGHT_RE.search(stripped):
            continue
        if is_header_footer(stripped) and counts[normalized] > 1:
            continue
        if counts[normalized] >= 3 and word_count(stripped) <= 8:
            continue
        cleaned_lines.append(stripped)
    value = "\n".join(cleaned_lines)
    # Strip unprintable ASCII control characters (such as bell \x07, formfeed \x0c)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    value = re.sub(r"(\w)-\n(\w)", r"\1\2", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def classify_extraction_chunk(text: str, seen_hashes: set[str] | None = None) -> str:
    cleaned = normalize_text(text)
    if not cleaned:
        return "EMPTY"
    digest = content_hash(cleaned)
    if seen_hashes is not None:
        if digest in seen_hashes:
            return "DUPLICATE"
        seen_hashes.add(digest)
    if is_page_number(text) or is_header_footer(text) or ISBN_COPYRIGHT_RE.search(str(text or "")):
        return "HEADER_FOOTER"
    if is_scan_artifact(text):
        return "OCR_NOISE"
    if is_formula_only(text):
        return "FORMULA_ONLY"
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) >= 4:
        short_lines = sum(1 for line in lines if len(line.strip()) <= 28)
        numeric_lines = sum(1 for line in lines if re.search(r"\d", line))
        if short_lines / len(lines) > 0.62 and numeric_lines / len(lines) > 0.30:
            return "TABLE_FRAGMENT"
    if word_count(text) < 100:
        return "SHORT_FRAGMENT"
    return "GOOD"


def _merge_chunk_group(chunks: list[dict[str, Any]], min_words: int, target_max_words: int) -> tuple[list[dict[str, Any]], int]:
    merged: list[dict[str, Any]] = []
    buffer: dict[str, Any] | None = None
    buffer_texts: list[str] = []
    merged_count = 0

    def flush() -> None:
        nonlocal buffer, buffer_texts
        if buffer is None:
            return
        item = dict(buffer)
        item["text"] = "\n\n".join(buffer_texts).strip()
        metadata = dict(item.get("metadata") or {})
        metadata["cleaned"] = True
        metadata["merged_chunk_count"] = len(buffer_texts)
        item["metadata"] = metadata
        merged.append(item)
        buffer = None
        buffer_texts = []

    for chunk in chunks:
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        if buffer is None:
            buffer = chunk
            buffer_texts = [text]
            continue
        candidate = "\n\n".join([*buffer_texts, text])
        if word_count("\n\n".join(buffer_texts)) < min_words or word_count(candidate) <= target_max_words:
            buffer_texts.append(text)
            merged_count += 1
        else:
            flush()
            buffer = chunk
            buffer_texts = [text]
    flush()
    return merged, merged_count


def repair_chunks(
    chunks: list[dict[str, Any]],
    min_words: int = 100,
    target_min_words: int = 150,
    target_max_words: int = 400,
) -> tuple[list[dict[str, Any]], ExtractionRepairMetrics]:
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    removed = Counter()
    input_word_total = sum(word_count(str(chunk.get("text") or "")) for chunk in chunks)

    for chunk in chunks:
        text = clean_raw_text(str(chunk.get("text") or ""))
        category = classify_extraction_chunk(text, seen_hashes=seen)
        if category in {"EMPTY", "DUPLICATE", "HEADER_FOOTER", "OCR_NOISE"}:
            removed[category] += 1
            continue
        item = dict(chunk)
        item["text"] = text
        kept.append(item)

    grouped: list[dict[str, Any]] = []
    current_key: tuple[Any, Any, Any, Any] | None = None
    current_group: list[dict[str, Any]] = []
    merged_count = 0

    def flush_group() -> None:
        nonlocal current_group, merged_count
        if not current_group:
            return
        merged, count = _merge_chunk_group(current_group, target_min_words, target_max_words)
        grouped.extend(merged)
        merged_count += count
        current_group = []

    for chunk in kept:
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        key = (metadata.get("grade"), metadata.get("subject"), metadata.get("chapter"), metadata.get("section"))
        if current_key is not None and key != current_key:
            flush_group()
        current_key = key
        current_group.append(chunk)
    flush_group()

    if input_word_total < min_words:
        final_chunks = grouped
    else:
        final_chunks = []
        for chunk in grouped:
            text = str(chunk.get("text") or "")
            category = classify_extraction_chunk(text)
            if category in {"GOOD"}:
                final_chunks.append(chunk)
            else:
                removed[category] += 1

    return final_chunks, ExtractionRepairMetrics(
        input_chunks=len(chunks),
        output_chunks=len(final_chunks),
        chunks_removed=len(chunks) - len(final_chunks),
        chunks_merged=merged_count,
        duplicates_removed=removed["DUPLICATE"],
        header_footer_removed=removed["HEADER_FOOTER"],
        scan_artifacts_removed=removed["OCR_NOISE"],
        empty_removed=removed["EMPTY"],
    )
