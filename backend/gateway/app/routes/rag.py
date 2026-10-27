from __future__ import annotations

from shared.schemas import SearchRequest, SearchResponse
from fastapi import APIRouter

from app.services.tutor import forward_rag


router = APIRouter(tags=["RAG"])


@router.post("/rag/search", response_model=SearchResponse)
async def rag_search(request: SearchRequest) -> dict:
    return await forward_rag(
        kind="search", query=request.query, path="/rag/search", method="POST",
        limit=request.limit, payload=request.model_dump(),
    )


@router.post("/rag/curriculum_search")
async def rag_curriculum_search(request: SearchRequest) -> dict:
    return await forward_rag(
        kind="curriculum_search", query=request.query, path="/rag/curriculum_search", method="POST",
        limit=request.limit, payload=request.model_dump(),
    )


@router.get("/rag/chapter", response_model=SearchResponse)
async def rag_chapter(chapter: str, limit: int = 5) -> dict:
    return await forward_rag(
        kind="chapter", query=chapter, path="/rag/chapter", method="GET",
        limit=limit, params={"chapter": chapter, "limit": limit},
    )


@router.get("/rag/subject", response_model=SearchResponse)
async def rag_subject(subject: str, limit: int = 5) -> dict:
    return await forward_rag(
        kind="subject", query=subject, path="/rag/subject", method="GET",
        limit=limit, params={"subject": subject, "limit": limit},
    )
