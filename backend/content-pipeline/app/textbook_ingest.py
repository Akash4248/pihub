"""
Structured textbook ingestion for curriculum-aware pipeline.

Supports:
- Directory-based curriculum hierarchy (grade/subject/chapter structure)
- Automatic metadata extraction from file paths
- Chapter detection from PDF content
- Educational semantic chunking
- Multi-language support

Directory structure:
content/
  class_6/
    science/
      photosynthesis.pdf
      respiration.pdf
    maths/
    social/
  class_7/
  ...
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from shared.config import get_settings
from shared.text_normalization import normalize_curriculum_name, normalize_language_code

import logging

from app.content_pipeline.educational_chunker import EducationalChunkerV2

logger = logging.getLogger(__name__)


class TextbookMetadataExtractor:
    """Extract curriculum metadata from file paths and content."""
    
    GRADE_MAP = {
        "class_6": 6, "class_7": 7, "class_8": 8, "class_9": 9, "class_10": 10,
        "grade_6": 6, "grade_7": 7, "grade_8": 8, "grade_9": 9, "grade_10": 10,
    }
    
    LANGUAGES = ["english", "kannada", "hindi", "marathi", "tamil", "telugu"]

    @classmethod
    def detect_language_from_text(cls, text: str) -> str:
        lower_text = text.lower()
        if any("\u0c80" <= char <= "\u0cff" for char in text):
            return "kn"
        if any("\u0900" <= char <= "\u097f" for char in text):
            return "hi"
        for language in cls.LANGUAGES:
            if language in lower_text:
                return normalize_language_code(language) or language
        return "en"
    
    @classmethod
    def extract_from_path(cls, file_path: Path) -> dict[str, Any]:
        """
        Extract metadata from file path structure.
        
        Example:
        content/class_7/science/photosynthesis.pdf
        →
        {
            "grade": 7,
            "subject": "science",
            "language": "english",  # default
            "textbook_name": "photosynthesis"
        }
        """
        parts = [p for p in file_path.parts]
        metadata: dict[str, Any] = {}
        subject_candidate: str | None = None
        filename = normalize_curriculum_name(file_path.stem)
        filename_tokens = set(re.findall(r"[a-z0-9]+", filename.lower()))

        language_aliases = {
            "en": {"english", "eng", "en"},
            "kn": {"kannada", "kan", "kannada_medium", "kn"},
            "hi": {"hindi", "hin", "hi"},
            "marathi": {"marathi", "mar"},
            "tamil": {"tamil", "tam"},
            "telugu": {"telugu", "tel"},
        }

        # Flexible grade extraction: handle 'class 8', 'grade 8', 'class_8', 'class8 part 1'
        for i, part in enumerate(parts):
            p = part.lower()
            # direct map
            if p in cls.GRADE_MAP:
                metadata["grade"] = cls.GRADE_MAP[p]
                # attempt to get subject from next part
                if i + 1 < len(parts):
                    next_part = parts[i + 1]
                    if ".pdf" not in next_part.lower():
                        metadata["subject"] = normalize_curriculum_name(next_part)
                    else:
                        subject_candidate = normalize_curriculum_name(next_part)
                break
            # regex match like 'class 8' or 'grade 8' or 'class8'
            m = re.search(r"(?:class|grade)\s*[_-]?(\d{1,2})", p)
            if m:
                try:
                    metadata["grade"] = int(m.group(1))
                except Exception:
                    pass
                if i + 1 < len(parts):
                    next_part = parts[i + 1]
                    if ".pdf" not in next_part.lower():
                        metadata["subject"] = normalize_curriculum_name(next_part)
                    else:
                        subject_candidate = normalize_curriculum_name(next_part)
                break

        # If subject still not found, prefer filename tokens and then path tokens.
        if "subject" not in metadata:
            SUBJECT_TOKENS = ["math", "maths", "mathematics", "science", "social", "social_science", "english", "kannada"]
            for token in SUBJECT_TOKENS:
                if token in filename_tokens:
                    if "math" in token:
                        metadata["subject"] = "mathematics"
                    elif "social" in token:
                        metadata["subject"] = "social_science"
                    else:
                        metadata["subject"] = normalize_curriculum_name(token)
                    break
            if "subject" not in metadata:
                for part in parts:
                    low = normalize_curriculum_name(part).lower()
                    if not low or "medium" in low:
                        continue
                    for token in SUBJECT_TOKENS:
                        if low == token or low.endswith(f"_{token}") or low.startswith(f"{token}_"):
                            if "math" in token:
                                metadata["subject"] = "mathematics"
                            elif "social" in token:
                                metadata["subject"] = "social_science"
                            else:
                                metadata["subject"] = normalize_curriculum_name(token)
                            break
                    if "subject" in metadata:
                        break

        if "subject" not in metadata and subject_candidate:
            metadata["subject"] = subject_candidate
        
        # Extract language if specified in path
        for lang in cls.LANGUAGES:
            if lang in str(file_path).lower():
                metadata["language"] = normalize_language_code(lang) or lang
                break

        # Default to English if not specified
        if "language" not in metadata:
            metadata["language"] = "en"
        
        # Extract textbook name / chapter from filename and sanitize
        # cleanup trailing dashes, separators, and trailing numbers
        name = re.sub(r"[\(\)\[\]]", "", filename)
        name = normalize_curriculum_name(name)
        metadata["textbook_name"] = name
        # Also expose a cleaned 'chapter' field for downstream use
        metadata["chapter"] = name

        for language, aliases in language_aliases.items():
            if any(alias in str(file_path).lower() for alias in aliases):
                metadata["language"] = normalize_language_code(language) or language
                break
        
        return metadata

    @classmethod
    def merge_text_metadata(cls, metadata: dict[str, Any], text: str) -> dict[str, Any]:
        merged = dict(metadata)
        language = merged.get("language")
        if not language or normalize_language_code(language) == "en":
            merged["language"] = cls.detect_language_from_text(text)
        # Clean chapter trailing characters
        if merged.get("chapter"):
            merged["chapter"] = normalize_curriculum_name(str(merged.get("chapter")))
        if merged.get("subject"):
            merged["subject"] = normalize_curriculum_name(str(merged.get("subject")))
        if merged.get("language"):
            merged["language"] = normalize_language_code(str(merged.get("language"))) or normalize_curriculum_name(str(merged.get("language")))
        if merged.get("textbook_name"):
            merged["textbook_name"] = normalize_curriculum_name(str(merged.get("textbook_name")))
        return merged


class EducationalChunkDetector:
    """Detect educational boundaries for semantic chunking."""
    
    # Patterns to detect chapter/section boundaries
    CHAPTER_PATTERNS = [
        r"^chapter\s+\d+",
        r"^unit\s+\d+",
        r"^lesson\s+\d+",
        r"^section\s+\d+",
        r"^topic\s+\d+",
        r"^ಅಧ್ಯಾಯ\s+\d+",
        r"^ಪಾಠ\s+\d+",
        r"^exercise",
        r"^questions",
    ]
    
    SECTION_PATTERNS = [
        r"^[a-z0-9\.\s]+\n(?=[A-Z])",  # Uppercase after newline
        r"^[0-9]+\.[0-9]+\s+",  # Numbered sections
    ]

    CHAPTER_KEYWORDS = ("chapter", "unit", "lesson", "topic")
    SECTION_KEYWORDS = ("exercise", "example", "activity", "question", "questions", "solve", "practice")
    MULTILINGUAL_SECTION_KEYWORDS = ("ಅಭ್ಯಾಸ", "ಉದಾಹರಣೆ", "ಚಟುವಟಿಕೆ", "ಪ್ರಶ್ನೆ", "ಪರಿಹಾರ", "ಅಭ್ಯಾಸಗಳು")
    
    @classmethod
    def detect_chapters(cls, text: str) -> list[dict[str, Any]]:
        """
        Detect chapter boundaries in text.
        
        Returns:
        [
            {
                "title": "Chapter 1: Photosynthesis",
                "start": 0,
                "end": 500,
                "topics": ["photosynthesis", "chlorophyll", ...]
            }
        ]
        """
        chapters: list[dict[str, Any]] = []
        lines = text.split("\n")
        
        current_chapter: dict[str, Any] | None = None
        current_pos = 0
        
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            
            if not line_stripped:
                current_pos += len(line) + 1
                continue

            # Check if line matches chapter pattern
            if cls._is_chapter_heading(line_stripped):
                # Save previous chapter
                if current_chapter:
                    current_chapter["end"] = current_pos
                    chapters.append(current_chapter)
                
                # Start new chapter
                current_chapter = {
                    "title": line_stripped,
                    "start": current_pos,
                    "topics": cls._extract_topics(line_stripped),
                    "sections": [],
                }
            elif current_chapter and cls._is_section_heading(line_stripped):
                current_chapter.setdefault("sections", []).append({
                    "title": line_stripped,
                    "start": current_pos,
                    "topics": cls._extract_topics(line_stripped),
                })
            
            current_pos += len(line) + 1  # +1 for newline
        
        # Save last chapter
        if current_chapter:
            current_chapter["end"] = current_pos
            chapters.append(current_chapter)

        if not chapters:
            chapters.append({
                "title": "Document",
                "start": 0,
                "end": len(text),
                "topics": cls._extract_topics(text[:120]),
                "sections": [],
            })
        
        return chapters
    
    @classmethod
    def _is_chapter_heading(cls, text: str) -> bool:
        """Check if text matches chapter heading patterns."""
        for pattern in cls.CHAPTER_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return True
        if len(text.split()) <= 8 and text and text[0].isupper() and text.endswith(":"):
            return True
        return False

    @classmethod
    def _is_section_heading(cls, text: str) -> bool:
        lowered = text.lower()
        if lowered.startswith(cls.SECTION_KEYWORDS):
            return True
        if text.startswith(cls.MULTILINGUAL_SECTION_KEYWORDS):
            return True
        if re.match(r"^[0-9]+(\.[0-9]+)*\s+", text):
            return True
        if len(text.split()) <= 10 and text and text[0].isupper() and text.endswith(":"):
            return True
        return False
    
    @classmethod
    def _extract_topics(cls, heading: str) -> list[str]:
        """Extract potential topics from heading."""
        # Remove numbers and common prefixes
        cleaned = re.sub(r"^(chapter|unit|lesson|section|exercise|question|topic|unit)\s+\d+:?\s*", "", heading, flags=re.IGNORECASE)
        
        # Split on common delimiters
        topics = re.split(r"[;,/\-]", cleaned)
        
        # Clean up and filter
        return [t.strip().lower() for t in topics if t.strip() and len(t.strip()) > 3]


class StructuredTextbookIngest:
    """Main API for structured textbook ingestion."""
    
    def __init__(self):
        settings = get_settings()
        self.metadata_extractor = TextbookMetadataExtractor()
        self.chunker = EducationalChunkerV2()
        self.semantic_chunker = self.chunker
        self.enable_semantic_educational_chunking = True


    def extract_text_from_pdf(self, file_path: Path) -> str:
        """Extract text from PDF with Docling first, then OCR-capable fallback."""
        docling_text = self._extract_text_with_docling(file_path)
        if docling_text and len(docling_text) > 40:
            logger.debug("Docling extracted text length from %s: %d", file_path.name, len(docling_text))
            print(f"[ingest] docling_extracted_text_length {file_path.name} {len(docling_text)}")
            return docling_text
        try:
            import fitz

            document = fitz.open(str(file_path))
            pages: list[str] = []
            for page in document:
                page_text = page.get_text("text").strip()
                if page_text:
                    pages.append(page_text)

            extracted = "\n".join(pages).strip()
            logger.debug("Extracted text length from %s: %d", file_path.name, len(extracted))
            print(f"[ingest] extracted_text_length {file_path.name} {len(extracted)}")
            if extracted and len(extracted) > 40:
                return extracted

            try:
                import pytesseract
                from PIL import Image
            except Exception:
                return extracted

            ocr_pages: list[str] = []
            for page in document:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_text = pytesseract.image_to_string(image).strip()
                if ocr_text:
                    ocr_pages.append(ocr_text)

            ocr_text = "\n".join(ocr_pages).strip()
            logger.debug("OCR extracted text length from %s: %d", file_path.name, len(ocr_text))
            print(f"[ingest] ocr_extracted_text_length {file_path.name} {len(ocr_text)}")
            return ocr_text or extracted
        except Exception as exc:
            raise RuntimeError(f"Unable to extract text from {file_path.name}: {exc}") from exc

    def extract_images(
        self,
        file_path: Path,
        *,
        min_width: int = 200,
        min_height: int = 200,
        min_area: int = 40_000,
    ) -> list[dict[str, Any]]:
        """Extract 'important' raster images from a PDF and persist them.

        Only images above the size thresholds are kept (skips tiny icons,
        rule lines and decorative glyphs). Each image is rendered to PNG
        under ``<upload_dir>/images/<book>/`` and a manifest entry is
        returned with page number, pixel size and any nearby caption.

        The manifest can be attached to chunk/section metadata so retrieval
        can surface the relevant figure alongside text.
        """
        try:
            import fitz
        except Exception as exc:  # pragma: no cover - fitz optional at import
            logger.debug("fitz unavailable, skipping image extraction: %s", exc)
            return []

        settings = get_settings()
        book = file_path.stem
        image_dir = Path(settings.upload_dir) / "images" / book
        image_dir.mkdir(parents=True, exist_ok=True)

        # Pre-scan pages for caption text to associate with images.
        doc = fitz.open(str(file_path))
        page_texts = [page.get_text("text") for page in doc]

        manifest: list[dict[str, Any]] = []
        seen_xrefs: set[int] = set()
        order = 0
        for page_index, page in enumerate(doc):
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.width < min_width or pix.height < min_height:
                        continue
                    if pix.width * pix.height < min_area:
                        continue
                    if pix.alpha:
                        pix = fitz.Pixmap(pix, alpha=False)
                    caption = self._nearest_caption(page_texts, page_index)
                    order += 1
                    fname = f"p{page_index + 1}_{order}.png"
                    pix.save(str(image_dir / fname))
                    manifest.append({
                        "filename": fname,
                        "path": str(image_dir / fname),
                        "page": page_index + 1,
                        "width": pix.width,
                        "height": pix.height,
                        "caption": caption,
                        "book": book,
                    })
                except Exception as exc:  # skip unreadable image xrefs
                    logger.debug("Skipping image xref %s in %s: %s", xref, file_path.name, exc)
                    continue
        doc.close()
        print(f"[ingest] images_extracted {file_path.name} count={len(manifest)}")
        return manifest

    @staticmethod
    def _nearest_caption(page_texts: list[str], page_index: int) -> str:
        """Find a figure caption on this/next page near the bottom/top."""
        window = [t for t in page_texts[max(0, page_index - 1): page_index + 2] if t]
        joined = "\n".join(window)
        m = re.search(r"(figure\s+\d+[\.:]?\s*[^\n]{0,120})", joined, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_text_with_docling(self, file_path: Path) -> str:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore

            result = DocumentConverter().convert(str(file_path))
            document = result.document
            if hasattr(document, "export_to_markdown"):
                return str(document.export_to_markdown()).strip()
            if hasattr(document, "export_to_text"):
                return str(document.export_to_text()).strip()
        except Exception as exc:
            logger.debug("Docling extraction unavailable for %s: %s", file_path.name, exc)
        return ""

    def ingest_pdf(self, file_path: Path) -> list[dict[str, Any]]:
        """Ingest a PDF file directly from disk."""
        raw_text = self.extract_text_from_pdf(file_path)
        return self.ingest_from_path(file_path, raw_text)

    def ingest_directory(self, directory: Path, recursive: bool = True) -> list[dict[str, Any]]:
        """Ingest all PDFs from a directory tree."""
        pattern = "**/*.pdf" if recursive else "*.pdf"
        chunks: list[dict[str, Any]] = []
        for file_path in sorted(directory.glob(pattern)):
            chunks.extend(self.ingest_pdf(file_path))
        return chunks
    
    def ingest_from_path(self, file_path: Path, text_content: str) -> list[dict[str, Any]]:
        """
        Full ingestion pipeline:
        1. Extract metadata from path
        2. Detect chapters
        3. Create semantic chunks
        """
        # Extract metadata from path
        metadata = self.metadata_extractor.merge_text_metadata(self.metadata_extractor.extract_from_path(file_path), text_content)

        # Chunk educational content. Keep legacy chunker available for compatibility.
        if self.enable_semantic_educational_chunking:
            chunks = self.semantic_chunker.chunk_educational(text_content, metadata)
        else:
            chunks = self.chunker.chunk_educational(text_content, metadata)

        # Extract and persist important figures; tag chunks whose text
        # references a figure with that image (caption-based association,
        # since the section chunker does not track page numbers).
        image_manifest = self.extract_images(file_path)
        if image_manifest:
            metadata.setdefault("images", [m["filename"] for m in image_manifest])
            for chunk in chunks:
                ctext = chunk.get("text", "")
                linked = [
                    img["filename"]
                    for img in image_manifest
                    if img["caption"] and img["caption"][:25].lower() in ctext.lower()
                ]
                if linked:
                    chunk.setdefault("metadata", {})["images"] = linked

        logger.debug("Ingested %s -> extracted_text_len=%d, chunks=%d", file_path.name, len(text_content or ""), len(chunks))
        print(f"[ingest] chunking_result {file_path.name} extracted_len={len(text_content or '')} chunk_count={len(chunks)}")
        if chunks:
            # log first chunk sizes for diagnostics
            first_len = len(chunks[0].get("text", ""))
            logger.debug("First chunk length: %d", first_len)
            print(f"[ingest] first_chunk_length {file_path.name} {first_len}")

        return chunks


# Example usage
if __name__ == "__main__":
    # Test metadata extraction
    test_path = Path("content/class_7/science/photosynthesis.pdf")
    extractor = TextbookMetadataExtractor()
    print("Extracted metadata:", extractor.extract_from_path(test_path))
    
    # Test chapter detection
    sample_text = """
    Chapter 1: Photosynthesis
    
    Photosynthesis is the process by which plants make food.
    
    Section 1.1: What is Photosynthesis
    
    It occurs in the chloroplasts of plant cells.
    
    Chapter 2: Respiration
    
    Respiration is the opposite of photosynthesis.
    """
    
    detector = EducationalChunkDetector()
    chapters = detector.detect_chapters(sample_text)
    print("\nDetected chapters:", chapters)
    
    # Test chunking
    ingest = StructuredTextbookIngest()
    chunks = ingest.ingest_from_path(test_path, sample_text)
    print(f"\nCreated {len(chunks)} chunks")
    for i, chunk in enumerate(chunks, 1):
        print(f"\nChunk {i}:")
        print(f"  Text: {chunk['text'][:100]}...")
        print(f"  Metadata: {chunk['metadata']}")
