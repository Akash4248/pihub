from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import websockets
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from shared.config import get_settings

from app.services.upstream import get_json, post_multipart, proxy_stream, proxy_to


router = APIRouter(tags=["Voice"])

logger = logging.getLogger(__name__)

settings = get_settings()


def _require_voice_enabled() -> None:
    if not settings.voice_service_enabled:
        raise HTTPException(status_code=503, detail="Voice service disabled")


@router.post("/voice/query")
@router.post("/api/voice/query")
async def voice_query(payload: dict[str, Any]) -> Any:
    _require_voice_enabled()
    return await proxy_to(settings.voice_service_url, "POST", "/voice/query", payload)


@router.post("/voice/tts")
@router.post("/api/voice/tts")
async def voice_tts(payload: dict[str, Any]) -> Any:
    _require_voice_enabled()
    return await proxy_to(settings.voice_service_url, "POST", "/voice/tts", payload)


@router.post("/voice/stt")
@router.post("/api/voice/stt")
async def voice_stt(
    file: UploadFile = File(...),
    language: str | None = Query(default=None),
    enable_partial_transcripts: bool = Query(default=False),
) -> dict[str, Any]:
    _require_voice_enabled()
    return await post_multipart(
        settings.voice_service_url,
        "/voice/stt",
        file,
        {
            "language": language,
            "enable_partial_transcripts": enable_partial_transcripts,
        },
    )


@router.get("/voice/audio/{asset_id:path}")
@router.get("/api/voice/audio/{asset_id:path}")
async def voice_audio(asset_id: str, request: Request) -> StreamingResponse:
    _require_voice_enabled()
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    return await proxy_stream(
        settings.voice_service_url,
        "GET",
        f"/voice/audio/{quote(asset_id, safe='')}",
        headers=headers,
    )


@router.get("/voice/metrics")
@router.get("/api/voice/metrics")
async def voice_metrics() -> dict[str, Any]:
    _require_voice_enabled()
    return await get_json(settings.voice_service_url, "/voice/metrics")


@router.websocket("/voice/stream")
@router.websocket("/api/voice/stream")
@router.websocket("/api/v1/voice/stream")
async def voice_stream_proxy(websocket: WebSocket) -> None:
    await websocket.accept()
    if not settings.voice_service_enabled:
        await websocket.send_json({"type": "error", "message": "Voice service disabled"})
        await websocket.close()
        return
    target_url = settings.voice_service_url.replace("http://", "ws://").replace("https://", "wss://") + "/voice/stream"
    logger.info(f"[GATEWAY] Proxying WebSocket connection to: {target_url}")

    session_id = "unknown"
    start_time = time.time()

    try:
        async with websockets.connect(target_url) as voice_ws:
            async def client_to_service():
                nonlocal session_id
                try:
                    while True:
                        msg = await websocket.receive_text()
                        try:
                            data = json.loads(msg)
                            if data.get("type") == "audio_start":
                                session_id = data.get("session_id") or "unknown"
                                logger.info(f"[GATEWAY] Session ID identified: {session_id}")
                        except Exception:
                            pass
                        await voice_ws.send(msg)
                except WebSocketDisconnect:
                    logger.info(f"[GATEWAY] Client disconnected from session {session_id}")
                except Exception as e:
                    logger.error(f"[GATEWAY] Error forwarding client to service: {e}")

            async def service_to_client():
                try:
                    while True:
                        msg = await voice_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as e:
                    logger.debug(f"[GATEWAY] Voice service connection closed or errored: {e}")

            await asyncio.gather(client_to_service(), service_to_client())
    except Exception as e:
        logger.error(f"[GATEWAY] Fail to connect to voice service: {e}")
        try:
            await websocket.send_json({"type": "error", "message": f"Voice service unavailable: {e}"})
            await websocket.close()
        except Exception:
            pass
