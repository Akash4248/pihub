#!/usr/bin/env python3
"""
Full Textbook Ingestion and Master Educational Database Rebuild Script.
Ingests all textbooks in TEXTBOOKS/ using the modern hybrid content pipeline,
rebuilding backend/.local_run/artifacts/educational_master.db with authentic
extracted concepts, sections, formulas, and definitions.

Optimized with strict memory bounds:
- Skips high-RAM image extraction during master db rebuild
- Constant O(1) memory footprint via per-book database transactions and explicit garbage collection
- Real-time RSS memory tracking to prevent system overload
"""
from __future__ import annotations

import gc
import json
import logging
import os
import resource
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# Ensure backend modules are on sys.path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir / "content-pipeline"))

os.environ["ALLOW_ANONYMOUS_DEV"] = "true"
os.environ["EMBEDDING_FALLBACK"] = "hash"
os.environ["INGEST_SKIP_IMAGES"] = "true"
# Restrict background BLAS / OpenMP worker threads to 1 to prevent CPU starvation
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

# Drop CPU scheduling priority so user apps (VS Code, GUI) remain 100% responsive
try:
    os.nice(15)
except Exception:
    pass

from app.textbook_ingest import StructuredTextbookIngest
from shared.chapter_naming import canonical_chapter_title

LOG_FILE = backend_dir / ".local_run" / "ingest_all_resources.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("FullIngestion")


def get_memory_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> None:
    repo_root = backend_dir.parents[1]
    textbooks_dir = repo_root / "TEXTBOOKS"
    master_db_path = backend_dir / ".local_run" / "artifacts" / "educational_master.db"
    backup_db_path = master_db_path.with_suffix(".db.bak")
    master_db_path.parent.mkdir(parents=True, exist_ok=True)

    if not textbooks_dir.exists():
        logger.error("Textbooks directory not found at %s", textbooks_dir)
        sys.exit(1)

    all_pdfs = sorted(list(textbooks_dir.rglob("*.pdf")))
    logger.info("Found %d textbook PDFs in %s (Current RSS: %.1f MB)", len(all_pdfs), textbooks_dir, get_memory_mb())

    # Backup existing database once if not already backed up
    if master_db_path.exists() and not backup_db_path.exists():
        logger.info("Creating backup of existing database")
        shutil.copy2(str(master_db_path), str(backup_db_path))

    # Initialize master database connection with required schema
    conn = sqlite3.connect(str(master_db_path), timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chapters (
            chapter_id TEXT PRIMARY KEY,
            grade INTEGER,
            subject TEXT,
            chapter_title TEXT,
            description TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            concept_id TEXT PRIMARY KEY,
            chapter_id TEXT,
            name TEXT,
            summary TEXT,
            difficulty TEXT,
            FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_questions (
            id TEXT PRIMARY KEY,
            chapter_id TEXT,
            question TEXT,
            options TEXT,
            correct_index INTEGER,
            explanation TEXT,
            difficulty TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS flashcards (
            id TEXT PRIMARY KEY,
            chapter_id TEXT,
            front TEXT,
            back TEXT,
            difficulty TEXT
        )
    """)
    conn.commit()

    ingestor = StructuredTextbookIngest()

    total_chunks_extracted = 0
    total_concepts_stored = 0
    successful_pdfs = 0
    failed_pdfs = 0

    start_time = time.time()

    for idx, pdf_path in enumerate(all_pdfs, start=1):
        rel_path = pdf_path.relative_to(textbooks_dir)
        
        # Check if already processed in this database with authentic concepts
        first_meta = ingestor.metadata_extractor.extract_from_path(pdf_path)
        grade = int(first_meta.get("grade") or 0)
        raw_subject = str(first_meta.get("subject") or "General").title()
        chapter_title = canonical_chapter_title(str(first_meta.get("chapter") or pdf_path.stem))
        chapter_id = f"grade_{grade}_{raw_subject.lower()}_{chapter_title.lower().replace(' ', '_')}"

        cursor.execute("SELECT COUNT(*) FROM concepts WHERE chapter_id = ? AND name NOT LIKE '%Core Principle%'", (chapter_id,))
        existing_concept_count = cursor.fetchone()[0]
        if existing_concept_count > 0:
            logger.info("[%d/%d] Skipping already ingested chapter (%d concepts): %s", idx, len(all_pdfs), existing_concept_count, rel_path)
            successful_pdfs += 1
            total_concepts_stored += existing_concept_count
            continue

        logger.info("[%d/%d] Ingesting: %s (RSS: %.1f MB)", idx, len(all_pdfs), rel_path, get_memory_mb())

        try:
            chunks = ingestor.ingest_pdf(pdf_path)
            if not chunks:
                logger.warning("No chunks extracted from %s", rel_path)
                continue

            total_chunks_extracted += len(chunks)

            # Re-read refined metadata from first chunk
            first_meta = chunks[0].get("metadata", {})
            grade = int(first_meta.get("grade") or grade)
            raw_subject = str(first_meta.get("subject") or raw_subject).title()
            chapter_title = canonical_chapter_title(str(first_meta.get("chapter") or chapter_title))
            chapter_id = f"grade_{grade}_{raw_subject.lower()}_{chapter_title.lower().replace(' ', '_')}"

            # Upsert chapter entry
            cursor.execute("""
                INSERT INTO chapters (chapter_id, grade, subject, chapter_title, description)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chapter_id) DO UPDATE SET
                    chapter_title = excluded.chapter_title,
                    grade = excluded.grade,
                    subject = excluded.subject,
                    description = excluded.description
            """, (
                chapter_id,
                grade,
                raw_subject,
                chapter_title,
                f"Curriculum chapter on {chapter_title} ({raw_subject}, Grade {grade})"
            ))

            # Delete old placeholder concepts for this chapter
            cursor.execute("DELETE FROM concepts WHERE chapter_id = ?", (chapter_id,))

            # Extract authentic concepts from chunks
            seen_concepts: set[str] = set()
            concept_idx = 0

            for chunk in chunks:
                meta = chunk.get("metadata", {})
                chunk_type = meta.get("chunk_type", "explanation")
                text = chunk.get("text", "").strip()
                section = meta.get("section", "").strip()
                keywords = meta.get("keywords") or []

                # Candidate concept names from definitions, sections, and top keywords
                candidates = []
                if chunk_type == "definition":
                    first_line = text.split("\n")[0].strip()
                    if ":" in first_line:
                        candidates.append(first_line.split(":")[0].strip(" *#_-"))
                    elif " is " in first_line:
                        candidates.append(first_line.split(" is ")[0].strip(" *#_-"))

                if section and section.lower() not in ("introduction", "document"):
                    candidates.append(section.title())

                for kw in keywords[:2]:
                    if len(kw) > 3 and kw.lower() not in ("chapter", "section", "figure", "table"):
                        candidates.append(kw.title())

                for cand in candidates:
                    clean_cand = cand.strip(" *#_-")
                    cand_lower = clean_cand.lower()
                    if not clean_cand or len(clean_cand) < 3 or cand_lower in seen_concepts:
                        continue
                    seen_concepts.add(cand_lower)
                    concept_idx += 1

                    summary_snippet = text[:280].strip()
                    if len(text) > 280:
                        summary_snippet += "..."

                    concept_id = f"{chapter_id}_c_{concept_idx}"
                    cursor.execute("""
                        INSERT INTO concepts (concept_id, chapter_id, name, summary, difficulty)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(concept_id) DO UPDATE SET
                            name = excluded.name,
                            summary = excluded.summary
                    """, (
                        concept_id,
                        chapter_id,
                        clean_cand,
                        summary_snippet,
                        "medium" if chunk_type == "formula" else "easy" if chunk_type == "definition" else "intermediate"
                    ))
                    total_concepts_stored += 1

            conn.commit()
            successful_pdfs += 1

            # Free memory immediately
            del chunks
            del seen_concepts
            gc.collect()
            # Brief pause to yield CPU time slices to host editor / desktop
            time.sleep(0.05)

        except Exception as exc:
            failed_pdfs += 1
            logger.error("Error processing %s: %s", rel_path, exc, exc_info=True)
            gc.collect()
            time.sleep(0.05)

    conn.close()

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("FULL INGESTION COMPLETE")
    logger.info("Elapsed Time: %.2f seconds", elapsed)
    logger.info("Total PDFs Processed: %d (Success: %d, Failed: %d)", len(all_pdfs), successful_pdfs, failed_pdfs)
    logger.info("Total Chunks Extracted: %d", total_chunks_extracted)
    logger.info("Total Real Concepts Stored: %d", total_concepts_stored)
    logger.info("Master database updated")
    logger.info("Final Memory RSS: %.1f MB", get_memory_mb())
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
