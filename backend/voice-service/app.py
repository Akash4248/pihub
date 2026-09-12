from __future__ import annotations

import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from analytics import VoiceMetrics
from api import router
from audio import AudioManifestRegistry, FileSystemAudioStorage
from cache import InMemoryVoiceCache
from services.tutor_engine import RagTutorEngine
from services.voice_gateway import VoiceGateway
from stt import get_stt_engine
from streaming import VoiceStreamer
from tts import get_tts_engine


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tts_engine = getattr(app.state, "tts_engine", None)
        stt_engine = getattr(app.state, "stt_engine", None)
        warmup_task = None
        if hasattr(tts_engine, "warmup"):
            warmup_task = asyncio.create_task(tts_engine.warmup())
            app.state.tts_warmup_task = warmup_task
        if hasattr(stt_engine, "initialize"):
            try:
                await stt_engine.initialize()
            except Exception:
                pass
        try:
            yield
        finally:
            if warmup_task is not None:
                try:
                    await warmup_task
                except Exception:
                    pass
            tts_engine = getattr(app.state, "tts_engine", None)
            if hasattr(tts_engine, "close"):
                await tts_engine.close()
            stt_engine = getattr(app.state, "stt_engine", None)
            if hasattr(stt_engine, "shutdown"):
                try:
                    await stt_engine.shutdown()
                except Exception:
                    pass

    app = FastAPI(
        title="IDP Voice Service",
        version="1.0.0",
        description="Voice questions, voice tutor, STT, TTS, pre-generated audio, streaming audio, and analytics.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    audio_root = Path(os.getenv("VOICE_AUDIO_ROOT", "/tmp/idp_voice_audio"))
    manifest_path = os.getenv("VOICE_AUDIO_MANIFEST", "")
    metrics = VoiceMetrics()
    cache = InMemoryVoiceCache()
    storage = FileSystemAudioStorage(audio_root)
    manifests = AudioManifestRegistry(manifest_path or None)
    tutor = RagTutorEngine()
    tts = get_tts_engine()
    stt = get_stt_engine()

    app.state.voice_metrics = metrics
    app.state.voice_cache = cache
    app.state.audio_storage = storage
    app.state.audio_manifest_registry = manifests
    app.state.tutor_engine = tutor
    app.state.tts_engine = tts
    app.state.stt_engine = stt
    app.state.voice_gateway = VoiceGateway(cache, storage, manifests, tutor, tts, metrics)
    app.state.voice_streamer = VoiceStreamer(tutor, tts)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, object]:
        voice_enabled = os.getenv("VOICE_SERVICE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
        tts_health: dict[str, Any] = {"loaded": False, "status": "unknown"}
        tts_engine = getattr(app.state, "tts_engine", None)
        if hasattr(tts_engine, "health_check"):
            try:
                tts_health = await tts_engine.health_check()
            except Exception as exc:
                tts_health = {"loaded": False, "status": "error", "last_error": str(exc)}

        stt_health: dict[str, Any] = {"loaded": False, "status": "unknown"}
        stt_engine = getattr(app.state, "stt_engine", None)
        if hasattr(stt_engine, "health"):
            try:
                stt_health = await stt_engine.health()
            except Exception as exc:
                stt_health = {"loaded": False, "status": "error", "last_error": str(exc)}

        return {
            "status": "healthy" if voice_enabled else "disabled",
            "voice_service_enabled": voice_enabled,
            "service": "voice-service",
            "voice_service": {
                "tts": tts_health,
                "stt": stt_health,
            },
            "capabilities": {
                "voice_query": voice_enabled,
                "stt": voice_enabled,
                "tts": voice_enabled,
                "streaming_tts": voice_enabled,
                "pre_generated_audio": True,
                "range_requests": True,
            },
            "voice_backend": stt_health.get("voice_backend") or getattr(stt_engine, "active_backend_name", "faster_whisper"),
            "backend_loaded": bool(stt_health.get("loaded", False)),
            "streaming_supported": bool(stt_health.get("streaming_supported", True)),
            "fallback_active": bool(stt_health.get("fallback_active", False)),
            "model_name": stt_health.get("model_name") or stt_health.get("model"),
            "backend_latency": stt_health.get("backend_latency"),
            "last_error": stt_health.get("last_error"),
        }

    app.include_router(router)
    return app


app = create_app()
