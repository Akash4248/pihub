import os
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
import logging
import json
import time
import asyncio
import base64
from services.tutor_adapter import InferenceTutorAdapter
from stt.base import Transcript

from models import STTRequest, STTResponse, TTSRequest, TTSResponse, VoiceQueryRequest, VoiceQueryResponse

logger = logging.getLogger(__name__)
seen_sessions = set()

router = APIRouter()


def check_voice_enabled():
    val = os.getenv("VOICE_SERVICE_ENABLED", "true").strip().lower()
    if val in {"0", "false", "no", "off"}:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VOICE_SERVICE_DISABLED",
                "message": "Voice service is disabled in system configuration.",
            },
        )


def _normalize_language_code(language: str | None) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    if value.startswith("kn") or value in {"kan", "kannada"}:
        return "kn"
    if value.startswith("hi") or value in {"hin", "hindi"}:
        return "hi"
    if value.startswith("en") or value in {"eng", "english"}:
        return "en"
    return value or "en"


def _chunk_text(text: str, max_chars: int = 80) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for sentence in text.replace("?", ".").split("."):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence = sentence + "."
        if len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                chunks.append(current)
            if len(sentence) <= max_chars:
                current = sentence
            else:
                words = sentence.split()
                buffer = ""
                for word in words:
                    candidate = f"{buffer} {word}".strip()
                    if len(candidate) > max_chars and buffer:
                        chunks.append(buffer)
                        buffer = word
                    else:
                        buffer = candidate
                current = buffer
    if current:
        chunks.append(current)
    return chunks


def _normalize_transcript(result: Transcript | dict[str, object], language: str | None = None) -> Transcript:
    if isinstance(result, Transcript):
        return result
    return Transcript(
        text=str(result.get("transcript") or result.get("text") or ""),
        language=_normalize_language_code(str(result.get("language") or language or "en")),
        confidence=result.get("confidence"),
        latency_ms=float(result.get("latency_ms") or 0.0),
        partial_transcripts=[str(item) for item in list(result.get("partial_transcripts") or [])],
        timestamps=[dict(item) for item in list(result.get("timestamps") or []) if isinstance(item, dict)],
        metadata=dict(result.get("metadata") or {}),
    )


@router.post("/voice/query", response_model=VoiceQueryResponse, tags=["voice"], dependencies=[Depends(check_voice_enabled)])
async def voice_query(request: Request, payload: VoiceQueryRequest) -> VoiceQueryResponse:
    gateway = request.app.state.voice_gateway
    return await gateway.query(payload)


@router.post("/voice/tts", response_model=TTSResponse, tags=["voice"], dependencies=[Depends(check_voice_enabled)])
async def voice_tts(request: Request, payload: TTSRequest) -> TTSResponse | StreamingResponse:
    gateway = request.app.state.voice_gateway
    if payload.stream:
        streamer = request.app.state.voice_streamer
        return StreamingResponse(
            streamer.tts.stream(payload.text, payload.voice, payload.language, payload.format),
            media_type="audio/L16",
            headers={
                "X-Audio-Sample-Rate": "24000",
                "X-Audio-Channels": "1",
            },
        )
    return await gateway.tts_only(payload)


@router.post("/voice/stt", response_model=STTResponse, tags=["voice"], dependencies=[Depends(check_voice_enabled)])
async def voice_stt(
    request: Request,
    language: str | None = None,
    enable_partial_transcripts: bool = False,
    file: UploadFile = File(...),
) -> STTResponse:
    request.app.state.voice_metrics.increment("stt_requests")
    audio = await file.read()
    result = _normalize_transcript(await request.app.state.stt_engine.transcribe(audio, language), language)
    return STTResponse(
        transcript=result.text,
        language=result.language,
        partial_transcripts=list(result.partial_transcripts) if enable_partial_transcripts else [],
        confidence=result.confidence,
        metrics={"latency_ms": result.latency_ms, **dict(result.metadata)},
    )


@router.get("/voice/audio/{asset_id}", tags=["voice", "audio"])
@router.get("/audio/{asset_id}", tags=["audio"])
async def get_audio_asset(request: Request, asset_id: str, range_header: str | None = Header(default=None, alias="Range")) -> Response:
    storage = request.app.state.audio_storage
    audio = await storage.get(asset_id)
    if not audio:
        raise HTTPException(status_code=404, detail={"success": False, "error": {"code": "AUDIO_NOT_FOUND", "message": asset_id}})
    content = audio.content
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
        "ETag": audio.checksum or "",
    }
    if range_header:
        start, end = _parse_range(range_header, len(content))
        headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
        return Response(content[start : end + 1], status_code=206, media_type=audio.content_type, headers=headers)
    headers["Content-Length"] = str(len(content))
    return Response(content, media_type=audio.content_type, headers=headers)


@router.get("/voice/metrics", tags=["voice", "analytics"])
async def voice_metrics(request: Request) -> dict[str, object]:
    return request.app.state.voice_metrics.snapshot()


@router.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    val = os.getenv("VOICE_SERVICE_ENABLED", "true").strip().lower()
    if val in {"0", "false", "no", "off"}:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": "Voice service is disabled in system configuration."
        })
        try:
            await websocket.close(code=1008)
        except TypeError:
            await websocket.close()
        return

    await websocket.accept()
    
    metrics = websocket.app.state.voice_metrics
    metrics.increment("active_connections")
    metrics.increment("voice_sessions")
    
    stt_engine = websocket.app.state.stt_engine
    tutor_adapter = InferenceTutorAdapter()
    
    session_id = "unknown"
    session_audio_chunks = bytearray()
    language_state = "en"

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError as e:
                logger.error(f"Malformed JSON in WS: {e}")
                await websocket.send_json({"type": "error", "message": "Invalid JSON frame"})
                continue
                
            msg_type = data.get("type")
            
            # session_start or audio_start (accepted for backward compat)
            if msg_type in ("session_start", "audio_start"):
                session_id = data.get("session_id") or data.get("sessionId") or "unknown"
                language_state = _normalize_language_code(data.get("language"))
                session_audio_chunks.clear()
                if session_id in seen_sessions:
                    metrics.increment("reconnects")
                else:
                    seen_sessions.add(session_id)
                # Send session_acknowledged for new protocol, or final_transcript for old
                if msg_type == "audio_start":
                    # Old protocol compat
                    await websocket.send_json({"type": "session_acknowledged", "session_id": session_id})
                else:
                    await websocket.send_json({"type": "session_acknowledged", "session_id": session_id})
                
            elif msg_type == "audio_chunk":
                if session_id == "unknown":
                    await websocket.send_json({"type": "error", "message": "Session not initialized"})
                    await websocket.close()
                    break
                
                chunk_b64 = data.get("data")
                if chunk_b64:
                    try:
                        session_audio_chunks.extend(base64.b64decode(chunk_b64))
                    except Exception as e:
                        logger.error(f"Failed to decode audio chunk: {e}")

            elif msg_type == "audio_complete":
                if session_id == "unknown":
                    await websocket.send_json({"type": "error", "message": "Session not initialized"})
                    await websocket.close()
                    break
                    
                language_state = _normalize_language_code(data.get("language")) or language_state
                roundtrip_start = time.perf_counter()
                
                # STT stage
                await websocket.send_json({"type": "transcribing"})
                
                try:
                    stt_started = time.perf_counter()
                    stt_res: Transcript | None = None
                    async for event in stt_engine.transcribe_stream(bytes(session_audio_chunks), language=language_state):
                        if event.type == "partial_transcript":
                            await websocket.send_json({
                                "type": "partial_transcript",
                                "text": event.text,
                                "language": event.language or language_state,
                            })
                        elif event.type == "final_transcript":
                            stt_res = Transcript(
                                text=event.text,
                                language=event.language or language_state,
                                confidence=event.confidence,
                                latency_ms=0.0,
                                metadata=dict(event.metadata),
                            )
                            await websocket.send_json({
                                "type": "final_transcript",
                                "text": stt_res.text,
                                "language": stt_res.language,
                                "confidence": stt_res.confidence,
                            })
                    if stt_res is None:
                        stt_res = _normalize_transcript(
                            await stt_engine.transcribe(bytes(session_audio_chunks), language=language_state),
                            language_state,
                        )
                    if not stt_res.latency_ms:
                        stt_res.latency_ms = (time.perf_counter() - stt_started) * 1000
                    transcript = stt_res.text or "unknown"
                    language = stt_res.language or language_state
                    metrics.observe("stt_latency_ms", stt_res.latency_ms)
                except Exception as e:
                    logger.error(f"STT transcription failed: {e}")
                    metrics.increment("stt_failures")
                    transcript = "unknown"
                    language = language_state
                    await websocket.send_json({"type": "error", "message": f"STT failed: {e}"})
                    continue
                    
                # Tutor stage
                await websocket.send_json({"type": "thinking"})
                sim_context = data.get("simulation_context", {})
                
                tutor_start = time.perf_counter()
                try:
                    answer = await tutor_adapter.get_answer(
                        question=transcript,
                        language=language,
                        session_id=session_id,
                        simulation_context=sim_context
                    )
                except Exception as e:
                    logger.error(f"Tutor call failed: {e}")
                    answer = "I'm sorry, I couldn't reach the tutor service right now."
                
                tutor_latency = (time.perf_counter() - tutor_start) * 1000
                metrics.observe("tutor_latency_ms", tutor_latency)

                answer_chunks = _chunk_text(answer, max_chars=96)
                for chunk in answer_chunks:
                    await websocket.send_json({"type": "response_chunk", "text": chunk})
                await websocket.send_json({"type": "response_complete"})
                
                # TTS stage
                await websocket.send_json({"type": "generating_audio"})
                tts_engine = websocket.app.state.tts_engine
                
                try:
                    seq = 1
                    async for audio_chunk in tts_engine.stream(answer, "default", language, "wav"):
                        await websocket.send_json({
                            "type": "audio_chunk",
                            "sequence": seq,
                            "data": base64.b64encode(audio_chunk).decode("utf-8")
                        })
                        seq += 1
                except Exception as e:
                    logger.error(f"TTS streaming failed: {e}")
                    
                await websocket.send_json({
                    "type": "audio_complete",
                    "session_id": session_id,
                    "language": language
                })
                
                roundtrip = (time.perf_counter() - roundtrip_start) * 1000
                metrics.observe("voice_roundtrip_ms", roundtrip)
                
            else:
                # Ignore unknown messages or log them if needed
                pass
                
    except WebSocketDisconnect:
        metrics.increment("disconnects")
    except Exception as e:
        logger.error(f"Error in voice stream: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        metrics.increment("active_connections", -1)


def _parse_range(header: str, size: int) -> tuple[int, int]:
    if not header.startswith("bytes="):
        raise HTTPException(status_code=416, detail="Only bytes ranges are supported")
    raw_start, _, raw_end = header.removeprefix("bytes=").partition("-")
    start = int(raw_start or 0)
    end = int(raw_end) if raw_end else size - 1
    if start < 0 or end < start or start >= size:
        raise HTTPException(status_code=416, detail="Invalid range")
    return start, min(end, size - 1)
