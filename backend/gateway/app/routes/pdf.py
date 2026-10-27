from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from shared.config import get_settings

from app.services.upstream import get_json, proxy_stream


router = APIRouter(tags=["PDF"])

settings = get_settings()


def _pdf_path(*parts: Any) -> str:
    encoded = "/".join(quote(str(part), safe="") for part in parts if part is not None)
    return f"/api/v1/pdf/{encoded}" if encoded else "/api/v1/pdf"


@router.get("/api/v1/pdf/catalog")
async def pdf_catalog() -> Any:
    return await get_json(settings.pack_service_url, "/api/v1/pdf/catalog")


@router.get("/api/v1/pdf/resolve")
async def pdf_resolve(
    grade: int = Query(...),
    subject: str = Query(...),
    chapter: str = Query(...),
    language: str = Query(default="english"),
) -> Any:
    return await get_json(
        settings.pack_service_url,
        "/api/v1/pdf/resolve",
        params={"grade": grade, "subject": subject, "chapter": chapter, "language": language},
    )


@router.get("/api/v1/pdf/book/{grade}/{subject}")
async def pdf_book(grade: int, subject: str, language: str = Query(default="english")) -> Any:
    return await get_json(
        settings.pack_service_url,
        _pdf_path("book", grade, subject),
        params={"language": language},
    )


@router.get("/api/v1/pdf/chapter/{chapter_id}/metadata")
async def pdf_chapter_metadata(chapter_id: str) -> Any:
    return await get_json(settings.pack_service_url, _pdf_path("chapter", chapter_id, "metadata"))


@router.get("/api/v1/pdf/chapter/{chapter_id}")
async def pdf_chapter(chapter_id: str) -> Any:
    return await get_json(settings.pack_service_url, _pdf_path("chapter", chapter_id))


@router.get("/api/v1/pdf/file/{book_id}")
async def pdf_file(book_id: str) -> StreamingResponse:
    return await proxy_stream(settings.pack_service_url, "GET", _pdf_path("file", book_id))
