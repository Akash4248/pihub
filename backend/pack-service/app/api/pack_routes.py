from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .pack_download_service import PackDownloadService
from .pack_response_models import (
    EnrichmentResponse,
    Flashcard,
    GlossaryEntry,
    ManifestResponse,
    PackListItem,
    PackPreviewResponse,
    PackValidationReport,
    QuizQuestion,
    PackSummaryItem,
)


router = APIRouter(tags=["Pack API"])


def _repository(request: Request):
    return request.app.state.pack_repository


def _validator(request: Request):
    return request.app.state.pack_validator


def _scorer(request: Request):
    return request.app.state.quality_scorer


def _benchmark_runner(request: Request):
    return request.app.state.retrieval_benchmark


def _eval_runner(request: Request):
    return request.app.state.educational_eval_runner


def _load_json(path: Path) -> Any:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_artifacts(record: dict[str, Any]) -> dict[str, Any]:
    pack_dir = Path(record["pack_dir"])
    return {
        "content": _load_json(pack_dir / "content.json"),
        "chapter_knowledge": _load_json(pack_dir / "chapter_knowledge.json") or {},
        "glossary": _load_json(pack_dir / "glossary.json"),
        "quizzes": _load_json(pack_dir / "quizzes.json"),
        "flashcards": _load_json(pack_dir / "flashcards.json"),
        "summaries": _load_json(pack_dir / "summaries.json"),
        "enrichment": _load_json(pack_dir / "enrichment.json") or {},
        "retrieval_index": _load_json(pack_dir / "retrieval_index" / "index.json") or {},
    }


@router.get("/packs/list")
def list_packs(request: Request) -> dict[str, Any]:
    packs = _repository(request).list_packs()
    return {"packs": [PackListItem(**pack).model_dump() for pack in packs], "total_count": len(packs)}


@router.get("/packs")
def list_packs_legacy(request: Request) -> dict[str, Any]:
    return list_packs(request)


@router.get("/packs/search")
def search_packs(
    request: Request,
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    language: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    packs = _repository(request).search(grade=grade, subject=subject, chapter=chapter, language=language, version=version)
    return {"packs": [PackListItem(**pack).model_dump() for pack in packs], "total_count": len(packs)}


@router.get("/packs/{pack_id}")
def get_pack_metadata(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    record = _repository(request).get_pack(pack_id, version)
    if record is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    return PackListItem(**record).model_dump()


@router.get("/packs/{pack_id}/manifest")
def get_pack_manifest(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    manifest = _repository(request).load_manifest(pack_id, version)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Pack manifest not found")
    return ManifestResponse(**manifest).model_dump()


@router.get("/packs/{pack_id}/preview")
def get_pack_preview(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    record = _repository(request).get_pack(pack_id, version)
    if record is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    manifest = _repository(request).load_manifest(pack_id, version)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Pack manifest not found")

    artifacts = _load_artifacts(record)
    quality = _scorer(request).score(manifest, artifacts)
    preview = PackPreviewResponse(
        manifest=ManifestResponse(**manifest),
        summaries=[PackSummaryItem(**item) for item in artifacts.get("summaries", [])],
        glossary=[GlossaryEntry(**item) for item in artifacts.get("glossary", [])],
        quizzes=[QuizQuestion(**item) for item in artifacts.get("quizzes", [])],
        flashcards=[Flashcard(**item) for item in artifacts.get("flashcards", [])],
        chapter_knowledge=artifacts.get("chapter_knowledge", {}),
        enrichment=EnrichmentResponse(**artifacts.get("enrichment", {})),
        quality_scores=quality.model_dump(),
    )
    return preview.model_dump()


@router.get("/packs/{pack_id}/download")
def download_pack(pack_id: str, request: Request, version: str | None = Query(default=None)):
    record = _repository(request).get_pack(pack_id, version)
    if record is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    return PackDownloadService().download(record)


@router.post("/packs/{pack_id}/validate")
def validate_pack(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    manifest = _repository(request).load_manifest(pack_id, version)
    record = _repository(request).get_pack(pack_id, version)
    if manifest is None or record is None:
        raise HTTPException(status_code=404, detail="Pack not found")

    artifacts = _load_artifacts(record)
    result = _validator(request).validate(manifest, artifacts, _scorer(request).score(manifest, artifacts).model_dump())
    return PackValidationReport(pack_id=pack_id, version=str(manifest.get("version", "1.0.0")), valid=result.valid, errors=result.errors, warnings=result.warnings).model_dump()


@router.post("/sync/manifest")
def build_sync_manifest(request: Request, host_version: str = Query(default="1.0.0")) -> dict[str, Any]:
    records = _repository(request).list_packs()
    return request.app.state.sync_manifest_generator.generate(host_version, records)


@router.post("/sync/delta")
def build_delta(request: Request, current_versions: dict[str, str]) -> dict[str, Any]:
    return request.app.state.delta_builder.build(_repository(request).list_packs(), current_versions)


@router.get("/packs/{pack_id}/benchmark")
def benchmark_pack(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    record = _repository(request).get_pack(pack_id, version)
    if record is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    # Scope to the requested pack: benchmarking the whole library here made
    # pack_id a mere existence check and hid per-pack regressions.
    return _benchmark_runner(request).run([record]).model_dump()


@router.get("/packs/{pack_id}/evaluation")
def evaluate_pack(pack_id: str, request: Request, version: str | None = Query(default=None)) -> dict[str, Any]:
    manifest = _repository(request).load_manifest(pack_id, version)
    record = _repository(request).get_pack(pack_id, version)
    if manifest is None or record is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    artifacts = _load_artifacts(record)
    return _eval_runner(request).run(manifest, artifacts, _repository(request).list_packs())
