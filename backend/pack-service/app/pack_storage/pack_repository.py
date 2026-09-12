from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from ..pack_system.manifest_builder import ManifestBuilder
from ..pack_system.manifest_validator import ManifestValidator
from .pack_locator import PackLocator
from .pack_registry import PackRegistry


class PackRepository:
    def __init__(self, storage_root: Path, retrieval_index_version: str = "v2") -> None:
        self.storage_root = storage_root
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.locator = PackLocator(storage_root)
        self.registry = PackRegistry(storage_root)
        self.manifest_builder = ManifestBuilder(retrieval_index_version=retrieval_index_version)
        self.manifest_validator = ManifestValidator()

    def save_pack(self, pack_data: dict[str, Any]) -> dict[str, Any]:
        pack_id = pack_data["pack_id"]
        grade = pack_data.get("grade")
        subject = pack_data.get("subject")
        chapter = pack_data.get("chapter")
        language = pack_data.get("language")
        version = pack_data.get("version", "1.0.0")
        artifacts = pack_data.get("artifacts", {})
        generation_metadata = pack_data.get("generation_metadata", {})
        quality_scores = pack_data.get("quality_scores", {})
        artifact_counts = pack_data.get("artifact_counts") or self._infer_artifact_counts(artifacts)

        pack_dir = self.locator.pack_dir(pack_id, grade=grade, subject=subject, chapter=chapter)
        pack_dir.mkdir(parents=True, exist_ok=True)

        manifest = self.manifest_builder.build(
            pack_id=pack_id,
            grade=grade,
            subject=subject,
            chapter=chapter,
            language=language,
            version=version,
            artifact_counts=artifact_counts,
            generation_metadata=generation_metadata,
            content_checksum_source={"pack_id": pack_id, "artifacts": artifacts, "artifact_counts": artifact_counts, "generation_metadata": generation_metadata},
            quality_scores=quality_scores,
        )

        self._write_artifacts(pack_dir, artifacts)
        manifest_path = pack_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        archive_path = self._archive_pack(pack_dir)
        record = self.registry.register(manifest, str(pack_dir), str(archive_path))
        record["manifest_path"] = str(manifest_path)
        record["valid"] = self.manifest_validator.validate(manifest)[0]
        return record

    def list_packs(self) -> list[dict[str, Any]]:
        return [self._hydrate_record(record) for record in self.registry.list()]

    def get_pack(self, pack_id: str, version: str | None = None) -> dict[str, Any] | None:
        record = self.registry.get(pack_id, version)
        return self._hydrate_record(record) if record else None

    def remove_pack(self, pack_id: str, version: str | None = None) -> None:
        record = self.registry.get(pack_id, version)
        if record:
            pack_dir = Path(str(record.get("pack_dir") or ""))
            archive_path = Path(str(record.get("archive_path") or ""))
            try:
                if pack_dir.exists():
                    shutil.rmtree(pack_dir)
            except Exception as exc:
                logger.warning("Failed to remove pack directory %s: %s", pack_dir, exc)
            try:
                if archive_path.exists():
                    archive_path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove archive %s: %s", archive_path, exc)
        self.registry.remove(pack_id, version)

    def load_manifest(self, pack_id: str, version: str | None = None) -> dict[str, Any] | None:
        record = self.get_pack(pack_id, version)
        if not record:
            return None
        manifest_path = Path(record["pack_dir"]) / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Corrupt manifest at %s: %s", manifest_path, exc)
            return None

    def validate_pack(self, pack_id: str, version: str | None = None) -> tuple[bool, list[str]]:
        manifest = self.load_manifest(pack_id, version)
        if not manifest:
            return False, ["manifest:not-found"]
        return self.manifest_validator.validate(manifest)

    def search(self, **criteria: Any) -> list[dict[str, Any]]:
        return [self._hydrate_record(record) for record in self.registry.search(**criteria)]

    def _hydrate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        hydrated = dict(record)
        pack_dir = Path(str(hydrated.get("pack_dir") or ""))
        manifest_path = pack_dir / "manifest.json"
        hydrated["manifest_exists"] = manifest_path.exists()
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_pack_id = manifest.get("pack_id")
                if manifest_pack_id and manifest_pack_id != hydrated.get("pack_id"):
                    hydrated["source_manifest_pack_id"] = manifest_pack_id
                for key in (
                    "version",
                    "grade",
                    "subject",
                    "chapter",
                    "language",
                    "checksum",
                    "content_checksum",
                    "artifact_counts",
                    "retrieval_index_version",
                    "generated_at",
                    "quality_scores",
                    "generation_metadata",
                ):
                    if key in manifest:
                        hydrated[key] = manifest[key]
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Corrupt manifest at %s: %s", manifest_path, exc)
                hydrated["valid"] = False

        archive_path = Path(str(hydrated.get("archive_path") or ""))
        hydrated["archive_exists"] = archive_path.exists()
        if archive_path.exists():
            size_bytes = archive_path.stat().st_size
            hydrated["size_bytes"] = size_bytes
            hydrated["compressed_size_mb"] = round(size_bytes / (1024 * 1024), 4)
        hydrated["manifest_path"] = str(manifest_path)
        return hydrated

    def _write_artifacts(self, pack_dir: Path, artifacts: dict[str, Any]) -> None:
        default_artifacts = {
            "textbook.json": artifacts.get("textbook", {}),
            "content.json": artifacts.get("content", []),
            "chapter_notes.json": artifacts.get("chapter_notes", []),
            "key_points.json": artifacts.get("key_points", []),
            "chapter_knowledge.json": artifacts.get("chapter_knowledge", self._build_chapter_knowledge(artifacts)),
            "concepts.json": artifacts.get("concepts", []),
            "examples.json": artifacts.get("examples", []),
            "worked_examples.json": artifacts.get("worked_examples", []),
            "formulas.json": artifacts.get("formulas", []),
            "tutor_contexts.json": artifacts.get("tutor_contexts", []),
            "activities.json": artifacts.get("activities", []),
            "questions.json": artifacts.get("questions", []),
            "glossary.json": artifacts.get("glossary", []),
            "misconceptions.json": artifacts.get("misconceptions", []),
            "applications.json": artifacts.get("applications", []),
            "quizzes.json": artifacts.get("quizzes", []),
            "flashcards.json": artifacts.get("flashcards", []),
            "summaries.json": artifacts.get("summaries", []),
            "reports/content_classification.json": artifacts.get("reports", {}).get("content_classification", {}),
            "reports/content_cleanup_report.json": artifacts.get("reports", {}).get("content_cleanup", {}),
            "reports/toc_cleanup_report.json": artifacts.get("reports", {}).get("toc_cleanup", {}),
            "reports/deduplication_report.json": artifacts.get("reports", {}).get("deduplication", {}),
            "reports/chunk_normalization_report.json": artifacts.get("reports", {}).get("chunk_normalization", {}),
            "reports/final_chunk_normalization_report.json": artifacts.get("reports", {}).get("final_chunk_normalization", {}),
            "reports/chunk_quality_report.json": artifacts.get("reports", {}).get("chunk_quality", {}),
            "reports/explanation_recovery_report.json": artifacts.get("reports", {}).get("explanation_recovery", {}),
            "reports/worked_example_builder_report.json": artifacts.get("reports", {}).get("worked_example_builder", {}),
            "reports/formula_validation_report.json": artifacts.get("reports", {}).get("formula_validation", {}),
            "reports/tutor_context_report.json": artifacts.get("reports", {}).get("tutor_context_enrichment", {}),
            "reports/textbook_publication_report.json": artifacts.get("reports", {}).get("textbook_publication", {}),
            "reports/rag_validation_report.json": artifacts.get("reports", {}).get("rag_validation", {}),
            "reports/concept_audit.json": artifacts.get("reports", {}).get("concept_audit", {}),
            "reports/concept_graph.json": artifacts.get("reports", {}).get("concept_graph", {}),
            "reports/concept_coverage_report.json": artifacts.get("reports", {}).get("concept_coverage", {}),
            "reports/summary_quality_v2.json": artifacts.get("reports", {}).get("summary_quality_v2", {}),
            "reports/quiz_alignment_report.json": artifacts.get("reports", {}).get("quiz_alignment_report", {}),
            "reports/tutor_context_quality.json": artifacts.get("reports", {}).get("tutor_context_quality", {}),
            "reports/quality_gate.json": artifacts.get("quality_gate", {}),
            "enrichment.json": artifacts.get("enrichment", {}),
            "retrieval_index/index.json": artifacts.get("retrieval_index", {}),
        }
        for relative_path, payload in default_artifacts.items():
            target = pack_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, (dict, list)):
                target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            elif isinstance(payload, bytes):
                target.write_bytes(payload)
            else:
                target.write_text(str(payload), encoding="utf-8")
                
        # Support custom generated pack artifacts
        for key, payload in artifacts.items():
            file_name = f"{key}.json"
            if file_name not in default_artifacts:
                target = pack_dir / file_name
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(payload, (dict, list)):
                    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
                else:
                    target.write_text(str(payload), encoding="utf-8")
                    
        # Support static assets copying
        if "static_dir" in artifacts and artifacts["static_dir"]:
            import shutil
            static_dir = Path(artifacts["static_dir"])
            if static_dir.exists() and static_dir.is_dir():
                static_target = pack_dir / "simulations"
                if static_target.exists():
                    shutil.rmtree(static_target)
                shutil.copytree(static_dir, static_target)

    def _archive_pack(self, pack_dir: Path) -> Path:
        archive_path = self.locator.archive_path(pack_dir)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(pack_dir, arcname=pack_dir.name)
        return archive_path

    @staticmethod
    def _infer_artifact_counts(artifacts: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key in (
            "content",
            "chapter_notes",
            "key_points",
            "concepts",
            "examples",
            "worked_examples",
            "formulas",
            "tutor_contexts",
            "activities",
            "questions",
            "glossary",
            "misconceptions",
            "applications",
            "quizzes",
            "flashcards",
            "summaries",
        ):
            value = artifacts.get(key, [])
            counts[key] = len(value) if isinstance(value, list) else (1 if value else 0)
        enrichment_value = artifacts.get("enrichment", {})
        retrieval_index_value = artifacts.get("retrieval_index", {})
        textbook_value = artifacts.get("textbook", {})
        counts["enrichment"] = len(enrichment_value) if isinstance(enrichment_value, dict) else 0
        counts["retrieval_index"] = len(retrieval_index_value) if isinstance(retrieval_index_value, dict) else 0
        counts["textbook"] = len(textbook_value.get("sections", [])) if isinstance(textbook_value, dict) else 0
        chapter_knowledge = artifacts.get("chapter_knowledge") or PackRepository._build_chapter_knowledge(artifacts)
        counts["chapter_knowledge"] = len(chapter_knowledge) if isinstance(chapter_knowledge, dict) else 0
        return counts

    @staticmethod
    def _build_chapter_knowledge(artifacts: dict[str, Any]) -> dict[str, Any]:
        glossary = artifacts.get("glossary") if isinstance(artifacts.get("glossary"), list) else []
        enrichment = artifacts.get("enrichment") if isinstance(artifacts.get("enrichment"), dict) else {}
        return {
            "concepts": artifacts.get("concepts", []),
            "definitions": [
                {
                    "term": item.get("term") or item.get("front") or "",
                    "definition": item.get("definition") or item.get("back") or "",
                    "example": item.get("example") or "",
                }
                for item in glossary
                if isinstance(item, dict)
            ],
            "misconceptions": enrichment.get("common_misconceptions", []),
            "relationships": enrichment.get("related_concepts", []) or enrichment.get("concept_relationships", []),
            "examples": artifacts.get("examples", []),
            "worked_examples": artifacts.get("worked_examples", []),
            "formulas": artifacts.get("formulas", []),
        }
