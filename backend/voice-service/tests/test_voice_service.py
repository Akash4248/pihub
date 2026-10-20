from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from shared.security import get_internal_service_key  # noqa: E402

AUTH_HEADERS = {"X-Internal-Service-Key": get_internal_service_key()}


class FakeTTS:
    async def synthesize(self, text: str, voice: str, language: str, audio_format: str) -> bytes:
        return f"AUDIO:{language}:{voice}:{text}".encode("utf-8")

    async def stream(self, text: str, voice: str, language: str, audio_format: str) -> AsyncIterator[bytes]:
        yield b"AUDIO_CHUNK_1"
        yield b"AUDIO_CHUNK_2"


class FakeSTT:
    async def transcribe(self, audio: bytes, language: str | None = None) -> dict[str, Any]:
        return {"transcript": "what is photosynthesis", "language": language or "en", "confidence": 0.9, "partial_transcripts": ["what is"]}


class FakeTutor:
    async def answer_with_context(self, question: str, filters: dict[str, Any]) -> dict[str, Any]:
        return {"answer": "Photosynthesis helps plants make food.", "context": [{"chapter_id": filters.get("chapter_id")}]}

    async def stream_answer_with_context(self, question: str, filters: dict[str, Any]) -> AsyncIterator[str]:
        yield "Photosynthesis "
        yield "makes food."


def app():
    app = create_app()
    app.state.tts_engine = FakeTTS()
    app.state.stt_engine = FakeSTT()
    app.state.tutor_engine = FakeTutor()
    app.state.voice_gateway.tts = app.state.tts_engine
    app.state.voice_gateway.tutor = app.state.tutor_engine
    app.state.voice_streamer.tts = app.state.tts_engine
    app.state.voice_streamer.tutor = app.state.tutor_engine
    return app


@pytest.mark.anyio
async def test_tts_cache_miss_then_hit() -> None:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {"text": "Hello", "language": "en", "cache": True}
        first = await client.post("/voice/tts", json=payload, headers=AUTH_HEADERS)
        second = await client.post("/voice/tts", json=payload, headers=AUTH_HEADERS)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["cache_status"] == "miss"
        assert second.json()["cache_status"] == "hit"


@pytest.mark.anyio
async def test_audio_retrieval_and_range_request() -> None:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = (await client.post("/voice/tts", json={"text": "Range me"}, headers=AUTH_HEADERS)).json()
        full = await client.get(created["audio_url"])
        partial = await client.get(created["audio_url"], headers={"Range": "bytes=0-4"})
        assert full.status_code == 200
        assert full.headers["accept-ranges"] == "bytes"
        assert partial.status_code == 206
        assert partial.content == full.content[:5]


@pytest.mark.anyio
async def test_voice_query_cache_miss() -> None:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/query", json={"question": "What is photosynthesis?", "chapter_id": "plants"}, headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["response_source"] == "rag_tutor"
        assert body["audio_url"].startswith("/voice/audio/")


@pytest.mark.anyio
async def test_stt_request() -> None:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/stt?language=en&enable_partial_transcripts=true", files={"file": ("q.wav", b"fake", "audio/wav")}, headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["transcript"] == "what is photosynthesis"


@pytest.mark.anyio
async def test_streaming_tts() -> None:
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/tts", json={"text": "stream", "stream": True}, headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert b"AUDIO_CHUNK" in response.content
