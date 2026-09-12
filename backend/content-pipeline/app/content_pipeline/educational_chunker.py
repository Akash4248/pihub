from __future__ import annotations

import re
from typing import Any

from app.content_pipeline.chunk_metadata_builder import ChunkMetadataBuilder
from app.content_pipeline.concept_boundary_detector import ConceptBoundaryDetector
from app.content_pipeline.educational_classifier import EducationalClassifier
from app.content_pipeline.extraction_cleaner import clean_raw_text, repair_chunks
from app.content_pipeline.formula_preserver import FormulaPreserver
from app.content_pipeline.paragraph_merger import ParagraphMerger
from app.content_pipeline.section_parser import ParsedSection, SectionParser


# Pure caption/label fragments that carry no teachable content on their own
# (e.g. the repeated "Figure 1.2" / "Activity 1.2" / "Reprint 2025-26" lines
# that the PDF emits next to the real figure/activity text).
_NOISE_RE = re.compile(
    r"^(figure\s+\d+(\.\d+)?|fig\.?\s*\d+(\.\d+)?|activity\s+\d+(\.\d+)?|"
    r"reprint\s+\d+[-\s]*\d*|^\d+$)$",
    re.IGNORECASE,
)


class EducationalChunkerV2:
    """Semantic educational chunker that preserves formulas and pedagogical blocks.

    Enhancements over the base behaviour:
    * Sentence-aware hard split so a single paragraph longer than
      ``max_chunk_chars`` is broken on sentence boundaries instead of
      overflowing the cap.
    * Overlap of the trailing sentence(s) carried into the next chunk for
      retrieval continuity (avoids cutting a concept at the seam).
    * Light noise strip of standalone caption/label fragments.
    """

    def __init__(
        self,
        min_chunk_chars: int = 180,
        max_chunk_chars: int = 1100,
        overlap_chars: int = 140,
    ) -> None:
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        self.overlap_chars = overlap_chars
        self.section_parser = SectionParser()
        self.paragraph_merger = ParagraphMerger(min_chars=120)
        self.classifier = EducationalClassifier()
        self.formula_preserver = FormulaPreserver()
        self.boundary_detector = ConceptBoundaryDetector()
        self.metadata_builder = ChunkMetadataBuilder()
        try:
            from chonkie import SemanticChunker, SentenceChunker
            try:
                self._chonkie_chunker = SemanticChunker(
                    max_chunk_size=max_chunk_chars,
                    similarity_threshold=0.5,
                )
            except Exception:
                self._chonkie_chunker = SentenceChunker(
                    chunk_size=max_chunk_chars,
                    chunk_overlap=overlap_chars,
                )
        except Exception:
            self._chonkie_chunker = None


    @staticmethod
    def _is_noise(paragraph: str) -> bool:
        p = paragraph.strip()
        return bool(p) and len(p) < 80 and bool(_NOISE_RE.match(p))

    def _split_long_paragraph(self, paragraph: str) -> list[str]:
        """Break an overlong paragraph on sentence boundaries using Chonkie if available.

        Sentences are kept intact; only when a single sentence itself exceeds
        the cap do we fall back to a hard character split.
        """
        if len(paragraph) <= self.max_chunk_chars:
            return [paragraph]

        if self._chonkie_chunker:
            try:
                chunks = self._chonkie_chunker.chunk(paragraph)
                extracted = [c.text.strip() for c in chunks if c.text and c.text.strip()]
                if extracted:
                    return extracted
            except Exception:
                pass

        sentences = re.split(r"(?<=[.!?])\s+", paragraph)

        pieces: list[str] = []
        buf = ""
        for sent in sentences:
            if buf and len(buf) + len(sent) + 1 > self.max_chunk_chars:
                pieces.append(buf.strip())
                buf = sent
            else:
                buf = f"{buf} {sent}".strip() if buf else sent
        if buf:
            pieces.append(buf.strip())

        # Hard split any residual monster sentence.
        out: list[str] = []
        for piece in pieces:
            if len(piece) <= self.max_chunk_chars:
                out.append(piece)
                continue
            for i in range(0, len(piece), self.max_chunk_chars):
                out.append(piece[i : i + self.max_chunk_chars].strip())
        return out

    def _preprocess_paragraphs(self, paragraphs: list[str]) -> list[str]:
        """Drop pure noise fragments and split overlong paragraphs."""
        out: list[str] = []
        for p in paragraphs:
            if self._is_noise(p):
                continue
            out.extend(self._split_long_paragraph(p))
        return out

    def chunk_educational(self, text: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        cleaned_text = clean_raw_text(text)
        sections = self.section_parser.parse(cleaned_text)
        chunks: list[dict[str, Any]] = []

        for section in sections:
            section_chunks = self._chunk_section(section, metadata)
            chunks.extend(section_chunks)

        repaired, _ = repair_chunks(chunks, target_max_words=150)
        return repaired

    def _chunk_section(self, section: ParsedSection, base_metadata: dict[str, Any]) -> list[dict[str, Any]]:
        raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section.content) if p.strip()]
        filtered_raw = [p for p in raw_paragraphs if not self._is_noise(p)]
        merged = self.paragraph_merger.merge(filtered_raw)
        paragraphs = self._preprocess_paragraphs(merged)

        section_chunks: list[dict[str, Any]] = []
        current: list[str] = []
        current_len = 0
        overlap_tail = ""

        def flush() -> None:
            nonlocal current, current_len, overlap_tail
            if not current:
                return
            text = "\n\n".join(current).strip()
            if not text:
                current, current_len = [], 0
                return
            # Carry the trailing sentence(s) forward as overlap.
            overlap_tail = text[-self.overlap_chars :] if self.overlap_chars else ""
            chunk_type = self.classifier.classify(text)
            metadata = self.metadata_builder.build(
                text=text,
                base_metadata=base_metadata,
                section_title=section.title,
                chunk_type=chunk_type,
                topic_hint=section.title,
            )
            section_chunks.append({"text": text, "metadata": metadata})
            current, current_len = [], 0

        for paragraph in paragraphs:
            # A merged paragraph may still exceed the cap; split it hard here
            # so it never overflows a chunk.
            if len(paragraph) > self.max_chunk_chars:
                sub = self._split_long_paragraph(paragraph)
            else:
                sub = [paragraph]

            for piece in sub:
                paragraph_len = len(piece)
                force_atomic = self.formula_preserver.is_formula_block(piece) or self.boundary_detector.is_boundary_start(piece)

                if force_atomic:
                    flush()
                    # A formula/boundary block that is still overlong must be
                    # split too (it may be a long narrative containing one
                    # equation); otherwise it would overflow the cap.
                    atomic_pieces = (
                        self._split_long_paragraph(piece)
                        if len(piece) > self.max_chunk_chars
                        else [piece]
                    )
                    for ap in atomic_pieces:
                        atomic_type = self.boundary_detector.boundary_label(ap) or self.classifier.classify(ap)
                        atomic_meta = self.metadata_builder.build(
                            text=ap,
                            base_metadata=base_metadata,
                            section_title=section.title,
                            chunk_type=atomic_type,
                            topic_hint=section.title,
                        )
                        section_chunks.append({"text": ap, "metadata": atomic_meta})
                    continue

                if current and (current_len + paragraph_len + 2) > self.max_chunk_chars:
                    flush()
                    if overlap_tail:
                        current = [overlap_tail]
                        current_len = len(overlap_tail) + 2

                current.append(piece)
                current_len += paragraph_len + 2

                if current_len >= self.min_chunk_chars and self.boundary_detector.is_boundary_start(piece):
                    flush()

        flush()
        return section_chunks
