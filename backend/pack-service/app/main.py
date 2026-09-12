from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

from app.analytics.routes import router as analytics_router
from app.api.pack_routes import router as pack_router
from app.api.pdf_routes import router as pdf_router
from app.api.preview_routes import router as preview_router
from app.evaluation.educational_eval_runner import EducationalEvalRunner
from app.evaluation.quality_scoring import QualityScorer
from app.evaluation.retrieval_benchmark import RetrievalBenchmark
from app.pack_generator import PackGenerationNoContentError, PackGenerator, PackQualityGateError
from app.pack_storage.pack_repository import PackRepository
from app.pdf_reader import PdfRegistrationService, PdfRepository
from app.sync.delta_builder import DeltaBuilder
from app.sync.sync_manifest_generator import SyncManifestGenerator
from app.validation.pack_validator import PackValidator
from app.generated_pack_importer import GeneratedPackImporter
from shared.schemas import HealthResponse
from shared.text_normalization import normalize_curriculum_name, normalize_language_code

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "educational_chunks_bge_m3")
LOCAL_RUN_BASE = Path(__file__).resolve().parents[2] / ".local_run"
SHARED_STORAGE_PATH = os.getenv("SHARED_STORAGE_PATH", str(LOCAL_RUN_BASE / "shared"))
PACK_STORAGE_PATH = os.getenv("PACK_STORAGE_PATH") or os.getenv("SHARED_STORAGE_PATH") or str(LOCAL_RUN_BASE / "packs")
CURRICULUM_GRAPH_PATH = os.getenv("CURRICULUM_GRAPH_PATH") or str(LOCAL_RUN_BASE / "work" / "curriculum_graph.json")
PDF_LIBRARY_PATH = os.getenv("PDF_LIBRARY_PATH") or str(LOCAL_RUN_BASE / "textbooks")
PDF_MANIFEST_PATH = os.getenv("PDF_MANIFEST_PATH") or str(Path(PACK_STORAGE_PATH) / "pdf_manifests" / "pdf_manifest.json")

app = FastAPI(title="Pack Management Service", version="2.0.0")


def _pack_generator() -> PackGenerator:
    return PackGenerator(
        qdrant_url=QDRANT_URL,
        qdrant_collection=QDRANT_COLLECTION,
        pack_storage_path=PACK_STORAGE_PATH,
        curriculum_graph_path=CURRICULUM_GRAPH_PATH,
    )


@app.on_event("startup")
async def startup_event() -> None:
    storage_root = Path(PACK_STORAGE_PATH)
    storage_root.mkdir(parents=True, exist_ok=True)

    app.state.pack_repository = PackRepository(storage_root)
    pdf_repository = PdfRepository(Path(PDF_MANIFEST_PATH), library_root=Path(PDF_LIBRARY_PATH))
    app.state.pdf_repository = pdf_repository
    app.state.pdf_registration_service = PdfRegistrationService(pdf_repository, Path(PDF_LIBRARY_PATH))
    if Path(PDF_LIBRARY_PATH).exists():
        scan_report = app.state.pdf_registration_service.rebuild_catalog()
        logger.info("PDF library catalog rebuilt: %s", scan_report)
    app.state.pack_validator = PackValidator()
    app.state.quality_scorer = QualityScorer()
    app.state.retrieval_benchmark = RetrievalBenchmark()
    app.state.educational_eval_runner = EducationalEvalRunner()
    app.state.sync_manifest_generator = SyncManifestGenerator()
    app.state.delta_builder = DeltaBuilder()
    app.state.pack_generator = _pack_generator()

    logger.info("Pack service storage ready: %s", storage_root)
    logger.info("PDF reader manifest ready: %s", PDF_MANIFEST_PATH)


@app.get("/health", tags=["Health"])
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="pack-management", checks={"storage_ready": True, "version": "2.0.0"})


try:
    from shared.pack_schemas import PackGenerationRequest, PackGenerationResponse
except ImportError:  # pragma: no cover - fallback for alternate import paths
    from pydantic import BaseModel

    class PackGenerationRequest(BaseModel):
        pack_type: str
        grade: Optional[int] = None
        subject: Optional[str] = None
        chapter: Optional[str] = None
        language: Optional[str] = None
        include_media: bool = False
        compression: str = "gzip"
        quantize_embeddings: bool = False

    class PackGenerationResponse(BaseModel):
        pack_id: str
        version: str
        status: str
        chunk_count: int
        media_count: int
        estimated_size_mb: float
        manifest_url: Optional[str] = None
        download_url: Optional[str] = None


def _expected_pack_id(request: PackGenerationRequest) -> str | None:
    generator = app.state.pack_generator
    normalized_subject = normalize_curriculum_name(request.subject) if request.subject else None
    normalized_chapter = normalize_curriculum_name(request.chapter) if request.chapter else None
    normalized_language = normalize_language_code(request.language) or (normalize_curriculum_name(request.language) if request.language else None)
    if request.pack_type == "class" and request.grade is not None and normalized_subject:
        language = normalized_language or "english"
        return f"class{request.grade}_{generator._pack_id_part(normalized_subject)}_{generator._pack_id_part(language)}"
    if request.pack_type == "chapter" and request.grade is not None and normalized_subject and normalized_chapter:
        language = normalized_language or "english"
        return (
            f"chapter_{request.grade}_{generator._pack_id_part(normalized_subject)}_"
            f"{generator._pack_id_part(normalized_chapter)}_{generator._pack_id_part(language)}"
        )
    if request.pack_type == "language" and normalized_language:
        subject_str = f"_{generator._pack_id_part(normalized_subject)}" if normalized_subject else ""
        grade_str = f"_{request.grade}" if request.grade else ""
        return f"lang_{generator._pack_id_part(normalized_language)}{grade_str}{subject_str}"
    return None


def _remove_stale_failed_pack(request: PackGenerationRequest) -> None:
    pack_id = _expected_pack_id(request)
    if not pack_id:
        return
    app.state.pack_repository.remove_pack(pack_id)
    logger.warning("Removed stale pack after failed publication: %s", pack_id)


@app.post("/packs/generate", response_model=PackGenerationResponse, tags=["Pack Generation"])
async def generate_pack(request: PackGenerationRequest) -> PackGenerationResponse:
    try:
        generator = app.state.pack_generator
        normalized_subject = normalize_curriculum_name(request.subject) if request.subject else None
        normalized_chapter = normalize_curriculum_name(request.chapter) if request.chapter else None
        normalized_language = normalize_language_code(request.language) or request.language

        if request.pack_type == "class":
            if request.grade is None or not request.subject:
                raise HTTPException(status_code=400, detail="grade and subject required for class packs")
            pack_id = await generator.generate_class_pack(
                grade=request.grade,
                subject=normalized_subject or request.subject,
                language=normalized_language or request.language or "english",
                include_media=request.include_media,
                compression=request.compression,
                quantize_embeddings=request.quantize_embeddings,
            )
        elif request.pack_type == "chapter":
            if request.grade is None or not request.subject or not request.chapter:
                raise HTTPException(status_code=400, detail="grade, subject, and chapter required for chapter packs")
            pack_id = await generator.generate_chapter_pack(
                grade=request.grade,
                subject=normalized_subject or request.subject,
                chapter=normalized_chapter or request.chapter,
                language=normalized_language or request.language or "english",
                compression=request.compression,
                quantize_embeddings=request.quantize_embeddings,
            )
        elif request.pack_type == "language":
            if not request.language:
                raise HTTPException(status_code=400, detail="language required for language packs")
            pack_id = await generator.generate_language_pack(
                language=normalized_language or request.language,
                grade=request.grade,
                subject=normalized_subject,
                compression=request.compression,
                quantize_embeddings=request.quantize_embeddings,
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown pack type: {request.pack_type}")

        manifest = generator.get_pack_manifest(pack_id)
        if not manifest:
            raise HTTPException(status_code=500, detail="Failed to generate pack")

        pack_record = app.state.pack_repository.get_pack(pack_id)
        size_mb = float(pack_record.get("compressed_size_mb", 0.0)) if pack_record else 0.0

        return PackGenerationResponse(
            pack_id=pack_id,
            version=manifest.get("version", "1.0.0"),
            status="completed",
            chunk_count=int(manifest.get("artifact_counts", {}).get("content", 0)),
            media_count=0,
            estimated_size_mb=size_mb,
            manifest_url=f"/packs/{pack_id}/manifest",
            download_url=f"/packs/{pack_id}/download",
        )

    except HTTPException:
        raise
    except PackGenerationNoContentError as exc:
        _remove_stale_failed_pack(request)
        logger.warning("Pack generation blocked: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except PackQualityGateError as exc:
        _remove_stale_failed_pack(request)
        logger.warning("Pack generation failed quality gate: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # pragma: no cover - surface runtime errors as HTTP errors
        logger.exception("Pack generation error")
        raise HTTPException(status_code=500, detail=str(exc))

from pydantic import BaseModel, Field
class ImportGeneratedPackRequest(BaseModel):
    source_dir: str = Field(default_factory=lambda: os.getenv("SHARED_STORAGE_PATH", str(Path(PACK_STORAGE_PATH).parent / "shared" / "generated_pack")))

@app.post("/packs/import/generated", tags=["Pack Import"])
async def import_generated_pack(request: ImportGeneratedPackRequest) -> dict:
    importer = GeneratedPackImporter(
        pack_repository=app.state.pack_repository,
        content_pipeline_url=os.getenv("CONTENT_PIPELINE_URL", "http://content-pipeline:8001")
    )
    try:
        record = await importer.import_pack(Path(request.source_dir))
        return {"status": "success", "pack": record}
    except Exception as e:
        logger.exception("Import failed")
        raise HTTPException(status_code=500, detail=str(e))


class ImportPhetRequest(BaseModel):
    source_dir: str = "/shared/phet_downloads"

@app.post("/packs/import/phet", tags=["Pack Import"])
async def import_phet_simulations(request: ImportPhetRequest) -> dict:
    from app.importers.phet_importer import PhetImporter
    importer = PhetImporter(pack_repository=app.state.pack_repository)
    try:
        record = await importer.import_simulations(Path(request.source_dir))
        return {"status": "success", "pack": record}
    except Exception as e:
        logger.exception("PHET Import failed")
        raise HTTPException(status_code=500, detail=str(e))


app.include_router(pack_router)
app.include_router(pdf_router)
app.include_router(preview_router)
app.include_router(analytics_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8030)
