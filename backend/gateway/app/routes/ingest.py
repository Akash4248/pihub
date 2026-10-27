from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
import httpx

from shared.config import get_settings
from shared.schemas import IngestResponse, Metadata
from shared.security import read_file_chunks_safely, require_admin, sanitize_safe_filename

from app.context import http_client
from app.services.upstream import INGEST_PROXY_TIMEOUT, proxy_request, proxy_to


router = APIRouter(tags=["Ingest"])

settings = get_settings()


@router.post("/content/upload", status_code=301, dependencies=[Depends(require_admin)])
async def content_upload_alias() -> RedirectResponse:
    return RedirectResponse(url="/upload", status_code=301)


@router.post("/upload", response_model=IngestResponse, dependencies=[Depends(require_admin)])
async def upload_content(
    file: UploadFile = File(...),
    grade: int | None = Form(default=None),
    subject: str | None = Form(default=None),
    chapter: str | None = Form(default=None),
    topic: str | None = Form(default=None),
    language: str | None = Form(default=None),
) -> dict[str, Any]:
    safe_name = sanitize_safe_filename(file.filename or "upload.pdf")
    metadata = Metadata(grade=grade, subject=subject, chapter=chapter, topic=topic, language=language)
    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)

    try:
        response = await http_client().post(
            f"{settings.content_pipeline_url}/ingest/pdf",
            data={"metadata": metadata.model_dump_json()},
            files={"file": (safe_name, content, file.content_type or "application/pdf")},
            timeout=INGEST_PROXY_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {exc}") from exc
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2000])
    return response.json()


@router.post("/ingest/pdf", response_model=IngestResponse, dependencies=[Depends(require_admin)])
async def ingest_pdf(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default=None),
) -> dict[str, Any]:
    safe_name = sanitize_safe_filename(file.filename or "upload.pdf")
    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        response = await http_client().post(
            f"{settings.content_pipeline_url}/ingest/pdf",
            data={"metadata": metadata, "source": source},
            files={"file": (safe_name, content, file.content_type or "application/pdf")},
            timeout=INGEST_PROXY_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {exc}") from exc
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@router.post("/ingest/textbook", response_model=IngestResponse, dependencies=[Depends(require_admin)])
async def ingest_textbook(
    file: UploadFile = File(...),
    metadata: str = Form(default="{}"),
    source: str | None = Form(default="textbook"),
) -> dict[str, Any]:
    safe_name = sanitize_safe_filename(file.filename or "textbook.pdf")
    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        response = await http_client().post(
            f"{settings.content_pipeline_url}/ingest/textbook",
            data={"metadata": metadata, "source": source or "textbook"},
            files={"file": (safe_name, content, file.content_type or "application/pdf")},
            timeout=INGEST_PROXY_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Service unavailable: {exc}") from exc
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@router.post("/ingest/generated-pack", dependencies=[Depends(require_admin)])
async def ingest_generated_pack(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/ingest/generated-pack", method="POST", payload=payload)


@router.post("/admin/reset-rag", dependencies=[Depends(require_admin)])
async def reset_rag() -> dict[str, Any]:
    return await proxy_request(settings.content_pipeline_url, "/admin/reset-rag", method="POST", payload={})


@router.post("/ingest/directory", dependencies=[Depends(require_admin)])
async def ingest_directory(payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_to(settings.content_pipeline_url, "POST", "/ingest/directory", payload)
