from __future__ import annotations

import json
import logging
import os
import struct
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SqliteVecExporter:
    """Exports content chunks and 384-dim bge-small sentence embeddings to portable SQLite vector databases.
    
    The resulting SQLite files can be read directly by both native sqlite-vec C++ extensions 
    and standard SQLite binary blob vector queries in mobile clients.
    """

    @staticmethod
    def pack_float_vector(vector: list[float]) -> bytes:
        """Packs a list of 384 floats into binary IEEE 754 little-endian 32-bit float bytes."""
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def unpack_float_vector(blob: bytes) -> list[float]:
        """Unpacks binary IEEE 754 little-endian float bytes into a list of floats."""
        count = len(blob) // 4
        return list(struct.unpack(f"<{count}f", blob))

    def export_to_sqlite(self, db_path: Path, chunks: list[dict[str, Any]]) -> int:
        """Exports a list of chunk dictionaries containing 'chunk_id', 'text', 'vector', and metadata to SQLite."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = db_path.with_name(f"{db_path.name}.tmp")
        if temp_path.exists():
            temp_path.unlink()

        conn = sqlite3.connect(str(temp_path))
        try:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    chapter_id TEXT,
                    subject TEXT,
                    grade INTEGER,
                    chunk_type TEXT,
                    text TEXT,
                    indexed_text TEXT,
                    metadata_json TEXT,
                    vector_blob BLOB
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON chunks(chapter_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_subject_grade ON chunks(subject, grade)
            """)

            inserted_count = 0
            seen_chunk_ids: set[str] = set()
            skipped_duplicates = 0
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")
                if not chunk_id:
                    continue
                if chunk_id in seen_chunk_ids:
                    skipped_duplicates += 1
                    continue
                seen_chunk_ids.add(chunk_id)

                vector = chunk.get("vector") or []
                vector_blob = self.pack_float_vector(vector) if vector else None

                metadata = chunk.get("metadata") or {}
                chapter_id = str(chunk.get("chapter_id") or metadata.get("chapter_id") or "")
                subject = str(chunk.get("subject") or metadata.get("subject") or "")
                grade = int(chunk.get("grade") or metadata.get("grade") or 0)
                chunk_type = str(chunk.get("chunk_type") or metadata.get("chunk_type") or "text")
                text = str(chunk.get("text") or "")
                indexed_text = str(chunk.get("indexed_text") or text)

                cursor.execute(
                    """
                    INSERT INTO chunks (
                        chunk_id, chapter_id, subject, grade, chunk_type, text, indexed_text, metadata_json, vector_blob
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        chapter_id,
                        subject,
                        grade,
                        chunk_type,
                        text,
                        indexed_text,
                        json.dumps(metadata, ensure_ascii=False),
                        vector_blob,
                    ),
                )
                inserted_count += 1

            if skipped_duplicates:
                logger.warning("Skipped %d duplicate chunk_id values during SQLite vector export", skipped_duplicates)
            conn.commit()
        except Exception:
            conn.rollback()
            if temp_path.exists():
                temp_path.unlink()
            raise
        finally:
            conn.close()

        os.replace(temp_path, db_path)
        return inserted_count
