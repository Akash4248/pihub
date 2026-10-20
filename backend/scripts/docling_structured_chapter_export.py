from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from shared.chapter_naming import chapter_title_for_pdf  # noqa: E402


WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?")
CLASS_RE = re.compile(r"\b(?:class|grade)[\s_-]*([1-9]|1[0-2])\b", re.I)
FORMULA_RE = re.compile(
    r"("
    r"[A-Za-z][A-Za-z0-9_ ()]{0,40}\s*(?:=|∝|≤|≥|<|>)\s*[^.;\n]{1,120}"
    r"|[A-Z][a-z]?\s*=\s*[^.;\n]{1,120}"
    r"|[A-Za-z0-9]+\s*/\s*[A-Za-z0-9]+"
    r")"
)
HEADING_RE = re.compile(
    r"^(chapter\s+\d+|unit\s+\d+|\d+(?:\.\d+)*\s+.+|[A-Z][A-Z0-9 ,:;()'’–-]{5,})$",
    re.I,
)
OCR_NOISE_RE = re.compile(r"(cid:\d+|�|[|_~]{4,}|[A-Za-z]\s+[A-Za-z]\s+[A-Za-z]\s+[A-Za-z]\s+[A-Za-z])")


@dataclass
class RawBlock:
    kind: str
    text: str
    level: int = 0
    table: dict[str, Any] | None = None
    figure: dict[str, Any] | None = None
    equation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def slugify(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_") or "untitled"


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))


def estimated_tokens(text: str) -> int:
    return max(1, int(len(text or "") / 4))


def dump_model(value: Any) -> dict[str, Any]:
    if hasattr(value, "export_to_dict"):
        return value.export_to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {}


def text_from_item(item: dict[str, Any]) -> str:
    for key in ("text", "orig", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_space(value)
    prov = item.get("prov")
    if isinstance(prov, list):
        parts = [str(part.get("text") or "").strip() for part in prov if isinstance(part, dict)]
        return normalize_space(" ".join(part for part in parts if part))
    return ""


def normalize_space(text: str) -> str:
    text = str(text or "").replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def infer_grade(path: Path) -> str:
    for part in path.parts:
        match = CLASS_RE.search(part)
        if match:
            return match.group(1)
    return ""


SUBJECT_ALIASES = {
    "mathematics": "mathematics",
    "maths": "mathematics",
    "math": "mathematics",
    "science": "science",
    "social": "social_science",
    "social science": "social_science",
    "social_science": "social_science",
    "biology": "biology",
    "chemistry": "chemistry",
    "physics": "physics",
}


def infer_subject(path: Path, textbooks_root: Path) -> str:
    try:
        relative = path.relative_to(textbooks_root)
    except ValueError:
        relative = path
    for part in reversed(relative.parts[:-1]):
        normalized = re.sub(r"[^a-z]+", " ", part.lower())
        normalized = re.sub(r"\b(?:class|grade|part)\b", " ", normalized)
        normalized = " ".join(normalized.split())
        if normalized in SUBJECT_ALIASES:
            return SUBJECT_ALIASES[normalized]
    return relative.parent.name


def looks_like_heading(text: str, label: str) -> bool:
    if "title" in label or "section_header" in label:
        return True
    if len(text) > 120:
        return False
    return bool(HEADING_RE.match(text)) or (word_count(text) <= 9 and text[:1].isupper())


def classify_text_block(text: str, label: str, seen_heading: bool) -> tuple[str, int]:
    lowered = label.lower()
    if looks_like_heading(text, lowered):
        if "title" in lowered and not seen_heading:
            return "heading", 1
        if "section_header" in lowered:
            return "subheading", 2
        return ("heading", 1) if not seen_heading else ("subheading", 2)
    if "caption" in lowered:
        return "caption", 0
    if "list" in lowered:
        return "list", 0
    if FORMULA_RE.search(text):
        return "formula", 0
    return "paragraph", 0


def table_rows(table: dict[str, Any]) -> list[list[str]]:
    data = table.get("data") if isinstance(table.get("data"), dict) else table
    cells = data.get("table_cells") or data.get("cells") or []
    if not isinstance(cells, list):
        return []
    by_row: dict[int, list[tuple[int, str]]] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row = int(cell.get("start_row_offset_idx") or cell.get("row") or cell.get("row_span", 0) or 0)
        col = int(cell.get("start_col_offset_idx") or cell.get("col") or cell.get("col_span", 0) or 0)
        text = normalize_space(str(cell.get("text") or ""))
        by_row.setdefault(row, []).append((col, text))
    return [[text for _, text in sorted(values)] for _, values in sorted(by_row.items()) if values]


def make_docling_converter() -> Any:
    from docling.datamodel.base_models import InputFormat  # type: ignore
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # type: ignore
    from docling.document_converter import DocumentConverter, PdfFormatOption  # type: ignore

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = False
    pipeline_options.do_formula_enrichment = False
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def extract_blocks_with_docling(pdf_path: Path, converter: Any) -> tuple[dict[str, Any], list[RawBlock]]:
    result = converter.convert(str(pdf_path))
    document = dump_model(result.document)
    blocks: list[RawBlock] = []
    seen_heading = False

    texts = document.get("texts") if isinstance(document.get("texts"), list) else []
    for item in texts:
        if not isinstance(item, dict):
            continue
        text = text_from_item(item)
        if not text:
            continue
        label = str(item.get("label") or item.get("type") or "").lower()
        kind, level = classify_text_block(text, label, seen_heading)
        if kind in {"heading", "subheading"}:
            seen_heading = True
        metadata = {"docling_label": label}
        blocks.append(RawBlock(kind=kind, text=text, level=level, metadata=metadata))
        if kind == "formula":
            for match in FORMULA_RE.finditer(text):
                equation = normalize_space(match.group(0))
                if equation and len(equation) <= 160:
                    blocks.append(RawBlock(kind="equation", text=equation, equation=equation, metadata=metadata))

    tables = document.get("tables") if isinstance(document.get("tables"), list) else []
    for index, table in enumerate(tables, start=1):
        if not isinstance(table, dict):
            continue
        rows = table_rows(table)
        if not rows:
            continue
        headers = rows[0] if rows else []
        table_obj = {
            "title": normalize_space(str(table.get("caption") or f"Table {index}")),
            "headers": headers,
            "rows": rows[1:] if len(rows) > 1 else [],
        }
        blocks.append(RawBlock(kind="table", text="", table=table_obj, metadata={"docling_label": "table"}))

    pictures = document.get("pictures") if isinstance(document.get("pictures"), list) else []
    for index, picture in enumerate(pictures, start=1):
        if not isinstance(picture, dict):
            continue
        caption = normalize_space(str(picture.get("caption") or picture.get("text") or ""))
        blocks.append(
            RawBlock(
                kind="figure",
                text=caption,
                figure={"figure_id": f"figure_{index}", "caption": caption},
                metadata={"docling_label": "picture"},
            )
        )
    return document, blocks


def extract_blocks_with_pymupdf(pdf_path: Path) -> tuple[dict[str, Any], list[RawBlock]]:
    import fitz  # type: ignore

    document = {"name": pdf_path.stem, "fallback": "pymupdf"}
    blocks: list[RawBlock] = []
    with fitz.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            text = page.get_text("text")
            parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
            for part in parts:
                part = normalize_space(part)
                kind, level = classify_text_block(part, "", any(block.kind in {"heading", "subheading"} for block in blocks))
                blocks.append(RawBlock(kind=kind, text=part, level=level, metadata={"page": page_index, "fallback": "pymupdf"}))
    return document, blocks


def new_section(title: str, level: int, index: int) -> dict[str, Any]:
    digest = hashlib.sha256(f"{index}:{title}:{level}".encode("utf-8")).hexdigest()[:12]
    return {
        "section_id": f"section_{index}_{digest}",
        "title": title.strip() or f"Section {index}",
        "level": level or 1,
        "content": "",
        "tables": [],
        "figures": [],
        "equations": [],
        "subsections": [],
        "metadata": {"word_count": 0, "character_count": 0, "estimated_tokens": 0},
    }


def append_to_section(section: dict[str, Any], block: RawBlock) -> None:
    if block.kind == "table" and block.table:
        section["tables"].append(block.table)
        return
    if block.kind == "figure" and block.figure:
        section["figures"].append(block.figure)
        return
    if block.kind == "equation" and block.equation:
        if block.equation not in section["equations"]:
            section["equations"].append(block.equation)
        return
    text = normalize_space(block.text)
    if not text:
        return
    if block.kind == "formula":
        for match in FORMULA_RE.finditer(text):
            equation = normalize_space(match.group(0))
            if equation and equation not in section["equations"]:
                section["equations"].append(equation)
    if section["content"]:
        section["content"] += "\n\n" + text
    else:
        section["content"] = text


def finalize_section(section: dict[str, Any]) -> dict[str, Any]:
    content = section.get("content") or ""
    section["metadata"] = {
        **dict(section.get("metadata") or {}),
        "word_count": word_count(content),
        "character_count": len(content),
        "estimated_tokens": estimated_tokens(content),
    }
    section["tables"] = section.get("tables", [])
    section["figures"] = section.get("figures", [])
    section["equations"] = section.get("equations", [])
    section["subsections"] = [finalize_section(item) for item in section.get("subsections", [])]
    return section


def split_large_section(section: dict[str, Any], max_words: int = 2000) -> list[dict[str, Any]]:
    words = section["metadata"]["word_count"]
    if words <= max_words:
        return [section]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section.get("content", "")) if part.strip()]
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    for paragraph in paragraphs:
        current.append(paragraph)
        if word_count("\n\n".join(current)) >= max_words:
            chunks.append({**section, "content": "\n\n".join(current), "subsections": []})
            current = []
    if current:
        chunks.append({**section, "content": "\n\n".join(current), "subsections": []})
    output: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk["section_id"] = f"{section['section_id']}_part_{index}"
        chunk["title"] = section["title"] if len(chunks) == 1 else f"{section['title']} - Part {index}"
        output.append(finalize_section(chunk))
    return output


def merge_short_sections(sections: list[dict[str, Any]], min_words: int = 500, max_words: int = 2000) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for section in sections:
        if (
            merged
            and section["metadata"]["word_count"] < min_words
            and merged[-1]["metadata"]["word_count"] + section["metadata"]["word_count"] <= max_words
            and not section["tables"]
        ):
            previous = merged[-1]
            if section.get("content"):
                previous["content"] = (previous.get("content", "") + "\n\n" + section["content"]).strip()
            previous["tables"].extend(section.get("tables", []))
            previous["figures"].extend(section.get("figures", []))
            for equation in section.get("equations", []):
                if equation not in previous["equations"]:
                    previous["equations"].append(equation)
            previous["subsections"].append(section)
            merged[-1] = finalize_section(previous)
        else:
            merged.append(section)
    return merged


def build_structured_chapter(
    pdf_path: Path,
    textbooks_root: Path,
    allow_fallback: bool,
    converter: Any | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = {
        "grade": infer_grade(pdf_path),
        "subject": infer_subject(pdf_path, textbooks_root),
        "chapter_title": chapter_title_for_pdf(pdf_path, pdf_path.name),
        "pdf_path": str(pdf_path),
    }
    started = time.perf_counter()
    extractor = "docling"
    error = ""
    try:
        if converter is None:
            converter = make_docling_converter()
        raw_document, blocks = extract_blocks_with_docling(pdf_path, converter)
    except Exception as exc:
        if not allow_fallback:
            raise
        extractor = "pymupdf_fallback"
        error = f"{type(exc).__name__}: {exc}"
        raw_document, blocks = extract_blocks_with_pymupdf(pdf_path)

    chapter_title = metadata["chapter_title"]
    title = normalize_space(str(raw_document.get("name") or chapter_title))
    sections: list[dict[str, Any]] = []
    current = new_section(chapter_title, 1, 1)
    section_index = 1

    for block in blocks:
        if block.kind in {"heading", "subheading"}:
            if word_count(current.get("content", "")) or current["tables"] or current["figures"] or current["equations"]:
                sections.append(finalize_section(current))
                section_index += 1
            current = new_section(block.text, block.level or 1, section_index)
            continue
        append_to_section(current, block)

    if word_count(current.get("content", "")) or current["tables"] or current["figures"] or current["equations"] or not sections:
        sections.append(finalize_section(current))

    expanded: list[dict[str, Any]] = []
    for section in sections:
        expanded.extend(split_large_section(section))
    sections = merge_short_sections(expanded)

    document_id = slugify(f"grade_{metadata['grade']}_{metadata['subject']}_{pdf_path.stem}")
    chapter = {
        "document_id": document_id,
        "document_title": title,
        "chapter_title": chapter_title,
        "grade": metadata["grade"],
        "subject": metadata["subject"],
        "language": "english",
        "source_pdf": str(pdf_path),
        "extractor": extractor,
        "sections": sections,
    }
    metrics = {
        **metadata,
        "document_id": document_id,
        "document_title": title,
        "extractor": extractor,
        "extractor_error": error,
        "section_count": len(sections),
        "table_count": sum(len(section["tables"]) for section in sections),
        "figure_count": sum(len(section["figures"]) for section in sections),
        "equation_count": sum(len(section["equations"]) for section in sections),
        "total_words": sum(section["metadata"]["word_count"] for section in sections),
        "largest_section_words": max((section["metadata"]["word_count"] for section in sections), default=0),
        "smallest_section_words": min((section["metadata"]["word_count"] for section in sections), default=0),
        "duplicate_sections": count_duplicate_sections(sections),
        "ocr_noise_sections": count_ocr_noise_sections(sections),
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    return chapter, metrics


def count_duplicate_sections(sections: list[dict[str, Any]]) -> int:
    seen: set[str] = set()
    duplicates = 0
    for section in sections:
        digest = hashlib.sha256((section.get("content") or "").strip().lower().encode("utf-8")).hexdigest()
        if digest in seen:
            duplicates += 1
        seen.add(digest)
    return duplicates


def count_ocr_noise_sections(sections: list[dict[str, Any]]) -> int:
    return sum(1 for section in sections if OCR_NOISE_RE.search(section.get("content") or ""))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def chapter_output_path(output_dir: Path, chapter: dict[str, Any]) -> Path:
    grade = f"grade_{chapter.get('grade') or 'unknown'}"
    subject = slugify(str(chapter.get("subject") or "unknown"))
    chapter_id = slugify(str(chapter.get("chapter_title") or chapter.get("document_id") or "chapter"))
    return output_dir / grade / subject / chapter_id / "structured_chapter.json"


def chapter_output_path_from_pdf(output_dir: Path, pdf_path: Path, textbooks_root: Path) -> Path:
    grade = f"grade_{infer_grade(pdf_path) or 'unknown'}"
    subject = slugify(infer_subject(pdf_path, textbooks_root) or "unknown")
    chapter_id = slugify(chapter_title_for_pdf(pdf_path, pdf_path.name))
    return output_dir / grade / subject / chapter_id / "structured_chapter.json"


def metrics_from_chapter(chapter: dict[str, Any], output_path: Path) -> dict[str, Any]:
    sections = chapter.get("sections") if isinstance(chapter.get("sections"), list) else []
    return {
        "pdf_path": str(chapter.get("source_pdf") or ""),
        "chapter_title": str(chapter.get("chapter_title") or ""),
        "grade": str(chapter.get("grade") or ""),
        "subject": str(chapter.get("subject") or ""),
        "document_id": str(chapter.get("document_id") or ""),
        "document_title": str(chapter.get("document_title") or ""),
        "extractor": str(chapter.get("extractor") or ""),
        "extractor_error": "",
        "section_count": len(sections),
        "table_count": sum(len(section.get("tables", [])) for section in sections if isinstance(section, dict)),
        "figure_count": sum(len(section.get("figures", [])) for section in sections if isinstance(section, dict)),
        "equation_count": sum(len(section.get("equations", [])) for section in sections if isinstance(section, dict)),
        "total_words": sum(int((section.get("metadata") or {}).get("word_count", 0)) for section in sections if isinstance(section, dict)),
        "largest_section_words": max(
            (int((section.get("metadata") or {}).get("word_count", 0)) for section in sections if isinstance(section, dict)),
            default=0,
        ),
        "smallest_section_words": min(
            (int((section.get("metadata") or {}).get("word_count", 0)) for section in sections if isinstance(section, dict)),
            default=0,
        ),
        "duplicate_sections": count_duplicate_sections([section for section in sections if isinstance(section, dict)]),
        "ocr_noise_sections": count_ocr_noise_sections([section for section in sections if isinstance(section, dict)]),
        "duration_seconds": 0,
        "output_path": str(output_path),
        "resumed": True,
    }


def report_validation(metrics: list[dict[str, Any]], output_dir: Path) -> None:
    total = len(metrics)
    docling_count = sum(1 for item in metrics if item["extractor"] == "docling")
    failed = [item for item in metrics if item.get("failed")]
    lines = [
        "# DOCLING EXTRACTION VALIDATION",
        "",
        f"- Textbooks targeted: {total}",
        f"- Docling extractions: {docling_count}",
        f"- Failed extractions: {len(failed)}",
        f"- Total sections: {sum(item.get('section_count', 0) for item in metrics)}",
        f"- Total tables: {sum(item.get('table_count', 0) for item in metrics)}",
        f"- Total figures: {sum(item.get('figure_count', 0) for item in metrics)}",
        f"- Total equations: {sum(item.get('equation_count', 0) for item in metrics)}",
        f"- Total words: {sum(item.get('total_words', 0) for item in metrics)}",
        "",
        "| Chapter name | Grade | Subject | Sections | Tables | Figures | Equations | Total words | Largest | Smallest | Extractor |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in metrics:
        lines.append(
            "| {chapter} | {grade} | {subject} | {sections} | {tables} | {figures} | {equations} | {words} | {largest} | {smallest} | {extractor} |".format(
                chapter=str(item.get("chapter_title", "")).replace("|", "\\|"),
                grade=item.get("grade", ""),
                subject=str(item.get("subject", "")).replace("|", "\\|"),
                sections=item.get("section_count", 0),
                tables=item.get("table_count", 0),
                figures=item.get("figure_count", 0),
                equations=item.get("equation_count", 0),
                words=item.get("total_words", 0),
                largest=item.get("largest_section_words", 0),
                smallest=item.get("smallest_section_words", 0),
                extractor=item.get("extractor", ""),
            )
        )
    (output_dir / "DOCLING_EXTRACTION_VALIDATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_audit(metrics: list[dict[str, Any]], output_dir: Path) -> None:
    missing_sections = [item for item in metrics if item.get("section_count", 0) == 0 or item.get("total_words", 0) == 0]
    duplicated = [item for item in metrics if item.get("duplicate_sections", 0) > 0]
    noisy = [item for item in metrics if item.get("ocr_noise_sections", 0) > 0]
    no_tables = [item for item in metrics if item.get("table_count", 0) == 0]
    no_equations = [item for item in metrics if item.get("equation_count", 0) == 0]
    fallback = [item for item in metrics if item.get("extractor") != "docling"]
    lines = [
        "# DOCLING STRUCTURE AUDIT",
        "",
        "## Summary",
        "",
        f"- Missing or empty sections: {len(missing_sections)}",
        f"- Duplicate section content findings: {sum(item.get('duplicate_sections', 0) for item in duplicated)}",
        f"- OCR corruption findings: {sum(item.get('ocr_noise_sections', 0) for item in noisy)}",
        f"- Chapters with tables preserved: {len(metrics) - len(no_tables)}",
        f"- Chapters with equations preserved: {len(metrics) - len(no_equations)}",
        f"- Non-Docling fallback extractions: {len(fallback)}",
        "",
        "## Findings",
        "",
    ]
    for heading, items, field in [
        ("Missing Sections", missing_sections, "total_words"),
        ("Duplicate Sections", duplicated, "duplicate_sections"),
        ("OCR Noise", noisy, "ocr_noise_sections"),
        ("Fallback Extractions", fallback, "extractor_error"),
    ]:
        lines.extend([f"### {heading}", ""])
        if not items:
            lines.extend(["None.", ""])
            continue
        for item in items[:100]:
            lines.append(f"- {item.get('chapter_title')} ({item.get('subject')} grade {item.get('grade')}): {field}={item.get(field)}")
        lines.append("")
    lines.extend(
        [
            "## Audit Verdict",
            "",
            "This report is structural only. It does not certify educational quality or generate AI artifacts.",
            "",
        ]
    )
    (output_dir / "DOCLING_STRUCTURE_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")


def discover_pdfs(textbooks_root: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in textbooks_root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf" and not path.name.startswith(".")
        ],
        key=lambda path: str(path).lower(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Docling structured chapter JSON for every textbook PDF.")
    parser.add_argument("--textbooks-dir", default="TEXTBOOKS", help="Path to textbook PDF library.")
    parser.add_argument("--out-dir", default="docling_structured_chapters", help="Output directory.")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit for smoke tests.")
    parser.add_argument("--allow-fallback", action="store_true", help="Use PyMuPDF fallback when Docling is unavailable or fails.")
    parser.add_argument("--no-resume", action="store_true", help="Regenerate chapters even if structured_chapter.json already exists.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    textbooks_root = Path(args.textbooks_dir)
    output_dir = Path(args.out_dir)
    if not textbooks_root.exists():
        print(f"Textbooks directory not found: {textbooks_root}", file=sys.stderr)
        return 2
    pdfs = discover_pdfs(textbooks_root)
    if args.limit:
        pdfs = pdfs[: args.limit]
    metrics: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    converter: Any | None = None
    if not args.allow_fallback or pdfs:
        try:
            converter = make_docling_converter()
        except Exception as exc:
            if not args.allow_fallback:
                print(f"Failed to initialize Docling converter: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            print(f"Docling converter unavailable, fallback enabled: {type(exc).__name__}: {exc}", file=sys.stderr)
    for index, pdf_path in enumerate(pdfs, start=1):
        try:
            out_path = chapter_output_path_from_pdf(output_dir, pdf_path, textbooks_root)
            if out_path.exists() and not args.no_resume:
                chapter = json.loads(out_path.read_text(encoding="utf-8"))
                item_metrics = metrics_from_chapter(chapter, out_path)
                metrics.append(item_metrics)
                print(f"[{index}/{len(pdfs)}] reused {out_path}", flush=True)
                continue
            chapter, item_metrics = build_structured_chapter(
                pdf_path,
                textbooks_root,
                allow_fallback=args.allow_fallback,
                converter=converter,
            )
            out_path = chapter_output_path(output_dir, chapter)
            write_json(out_path, chapter)
            item_metrics["output_path"] = str(out_path)
            metrics.append(item_metrics)
            print(f"[{index}/{len(pdfs)}] exported {pdf_path} -> {out_path}", flush=True)
        except Exception as exc:
            failed = {
                "pdf_path": str(pdf_path),
                "chapter_title": chapter_title_for_pdf(pdf_path, pdf_path.name),
                "grade": infer_grade(pdf_path),
                "subject": infer_subject(pdf_path, textbooks_root),
                "failed": True,
                "extractor": "docling",
                "extractor_error": f"{type(exc).__name__}: {exc}",
                "section_count": 0,
                "table_count": 0,
                "figure_count": 0,
                "equation_count": 0,
                "total_words": 0,
                "largest_section_words": 0,
                "smallest_section_words": 0,
                "duplicate_sections": 0,
                "ocr_noise_sections": 0,
            }
            metrics.append(failed)
            print(f"[{index}/{len(pdfs)}] FAILED {pdf_path}: {failed['extractor_error']}", file=sys.stderr, flush=True)
            if not args.allow_fallback:
                break
    write_json(output_dir / "docling_export_manifest.json", {"textbooks_dir": str(textbooks_root), "chapters": metrics})
    report_validation(metrics, output_dir)
    report_audit(metrics, output_dir)
    failed_count = sum(1 for item in metrics if item.get("failed"))
    return 1 if failed_count and not args.allow_fallback else 0


if __name__ == "__main__":
    raise SystemExit(main())
