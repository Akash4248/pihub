from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any


class PackCompiler:
    """Compile generated educational artifacts into an offline pack manifest/archive.

    Supports all artifact types: chunks, summaries, glossary, quizzes, flashcards,
    chapter notes, learning objectives, misconceptions, real-world applications,
    figures, and enrichment resources.
    """

    def compile(
        self,
        pack_name: str,
        chunks: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        glossary: list[dict[str, Any]],
        quizzes: list[dict[str, Any]],
        flashcards: list[dict[str, Any]],
        enrichment: list[dict[str, Any]],
        chapter_notes: list[dict[str, Any]] | None = None,
        learning_objectives: list[dict[str, Any]] | None = None,
        misconceptions: list[dict[str, Any]] | None = None,
        applications: list[dict[str, Any]] | None = None,
        image_dir: Path | str | None = None,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        output_root = output_dir or Path(tempfile.gettempdir()) / "pihub_packs"
        output_root.mkdir(parents=True, exist_ok=True)
        pack_dir = output_root / self._slug(pack_name)
        pack_dir.mkdir(parents=True, exist_ok=True)

        # Copy figure images if available
        image_count = 0
        if image_dir:
            img_path = Path(image_dir)
            if img_path.exists():
                pack_img_dir = pack_dir / "images"
                pack_img_dir.mkdir(parents=True, exist_ok=True)
                for f in img_path.iterdir():
                    if f.is_file():
                        shutil.copy2(f, pack_img_dir / f.name)
                        image_count += 1

        manifest = {
            "pack_id": self._slug(pack_name),
            "pack_name": pack_name,
            "version": "2.0.0",
            "chunk_count": len(chunks),
            "summary_count": len(summaries),
            "glossary_count": len(glossary),
            "quiz_count": len(quizzes),
            "flashcard_count": len(flashcards),
            "enrichment_count": len(enrichment),
            "chapter_notes_count": len(chapter_notes or []),
            "learning_objectives_count": len(learning_objectives or []),
            "misconceptions_count": len(misconceptions or []),
            "applications_count": len(applications or []),
            "figure_count": image_count,
            "artifact_types": [
                "content", "summaries", "glossary", "quizzes", "flashcards",
                "enrichment", "chapter_notes", "learning_objectives",
                "misconceptions", "applications",
            ],
        }

        artifacts = {
            "content.json": chunks,
            "summaries.json": summaries,
            "glossary.json": glossary,
            "quizzes.json": quizzes,
            "flashcards.json": flashcards,
            "enrichment.json": enrichment,
            "chapter_notes.json": chapter_notes or [],
            "learning_objectives.json": learning_objectives or [],
            "misconceptions.json": misconceptions or [],
            "applications.json": applications or [],
            "metadata.json": manifest,
        }
        for filename, payload in artifacts.items():
            (pack_dir / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        archive_path = pack_dir.with_suffix(".zip")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in pack_dir.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, arcname=file_path.relative_to(pack_dir))

        manifest["archive_path"] = str(archive_path)
        manifest["pack_dir"] = str(pack_dir)
        return manifest

    def _slug(self, text: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", text.strip()).strip("_").lower()
        return clean or "pack"
