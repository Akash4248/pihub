from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

try:
    import soundfile as sf
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    sf = None

from .base import Transcript, TranscriptEvent, VoiceBackend

logger = logging.getLogger(__name__)

_LANGUAGE_LABELS = {
    "en": "English",
    "eng": "English",
    "hi": "Hindi",
    "hin": "Hindi",
    "kn": "Kannada",
    "kan": "Kannada",
    "mixed": "the original spoken language(s), preserving code-switching",
    "mix": "the original spoken language(s), preserving code-switching",
    "auto": "the original spoken language(s), preserving code-switching",
}


def _normalize_language(language: str | None) -> str:
    if not language:
        return "auto"
    return language.strip().lower()


def _language_prompt(language: str | None) -> str:
    normalized = _normalize_language(language)
    label = _LANGUAGE_LABELS.get(normalized, normalized.title())
    return f"Transcribe the following speech segment in {label}. Preserve the spoken language and do not translate."


def _split_tokens(text: str) -> list[str]:
    return [token for token in text.strip().split() if token]


def _merge_transcripts(previous: str, current: str) -> str:
    previous = " ".join(previous.split())
    current = " ".join(current.split())
    if not previous:
        return current
    if not current:
        return previous
    if current.startswith(previous):
        return current
    if previous.startswith(current):
        return previous

    prev_tokens = _split_tokens(previous)
    curr_tokens = _split_tokens(current)
    max_overlap = min(len(prev_tokens), len(curr_tokens))
    for size in range(max_overlap, 0, -1):
        if prev_tokens[-size:] == curr_tokens[:size]:
            merged = prev_tokens + curr_tokens[size:]
            return " ".join(merged).strip()
    return f"{previous} {current}".strip()


class Gemma4AudioBackend(VoiceBackend):
    """Official Gemma 4 audio backend using Hugging Face Transformers.

    The model is loaded from the official Google repository and run directly
    through the Transformers multimodal runtime. Streaming is emulated at the
    application level by chunking audio into overlapping windows and emitting
    incremental transcript updates.
    """

    def __init__(self) -> None:
        self.model_id = os.getenv("GEMMA_MODEL_ID", "google/gemma-4-E4B-it")
        self.cache_dir = Path(os.getenv("GEMMA_MODEL_CACHE_DIR", "/models/gemma4_audio"))
        self.device = os.getenv("GEMMA_DEVICE", "cpu").strip().lower()
        self.dtype = os.getenv("GEMMA_DTYPE", "auto").strip().lower()
        self.max_audio_seconds = float(os.getenv("GEMMA_MAX_AUDIO_SECONDS", "30"))
        self.chunk_seconds = float(os.getenv("GEMMA_CHUNK_SECONDS", "8"))
        self.chunk_overlap_seconds = float(os.getenv("GEMMA_CHUNK_OVERLAP_SECONDS", "1"))
        self.max_new_tokens = int(os.getenv("GEMMA_MAX_NEW_TOKENS", "256"))
        self.batch_size = int(os.getenv("GEMMA_BATCH_SIZE", "1"))
        self.language = os.getenv("GEMMA_LANGUAGE", "auto")
        self.input_sample_rate = int(os.getenv("GEMMA_INPUT_SAMPLE_RATE", "24000"))
        self.streaming = os.getenv("GEMMA_STREAMING", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.last_error: str | None = None
        self.loaded = False
        self.load_time_ms: float | None = None
        self.model_path: str | None = None
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self._processor: Any = None
        self._model: Any = None

    async def initialize(self) -> None:
        await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        if self.loaded:
            return
        async with self._load_lock:
            if self.loaded:
                return
            started = time.perf_counter()
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._load_runtime)
                self.load_time_ms = round((time.perf_counter() - started) * 1000, 2)
                self.loaded = True
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.loaded = False
                logger.warning("Gemma 4 audio backend unavailable: %s", exc)

    def _load_runtime(self) -> None:
        try:
            import torch
            from transformers import AutoProcessor
            try:
                from transformers import AutoModelForImageTextToText as GemmaModelClass
            except Exception:  # pragma: no cover - older transformers builds
                from transformers import AutoModelForMultimodalLM as GemmaModelClass
        except Exception as exc:  # pragma: no cover - dependency failure path
            raise RuntimeError(f"Gemma runtime dependencies unavailable: {exc}") from exc

        load_kwargs: dict[str, Any] = {
            "cache_dir": str(self.cache_dir),
            "trust_remote_code": True,
            "low_cpu_mem_usage": True,
        }
        if self.dtype != "auto":
            torch_dtype = getattr(torch, self.dtype, None)
            if torch_dtype is None:
                raise RuntimeError(f"Unsupported GEMMA_DTYPE={self.dtype}")
            load_kwargs["torch_dtype"] = torch_dtype

        if self.device == "auto":
            load_kwargs["device_map"] = "auto"
        else:
            load_kwargs["device_map"] = None

        self._processor = AutoProcessor.from_pretrained(self.model_id, cache_dir=str(self.cache_dir), trust_remote_code=True)
        self._model = GemmaModelClass.from_pretrained(self.model_id, **load_kwargs)

        if self.device != "auto":
            self._model = self._model.to(self.device)

        self.model_path = str(self.cache_dir)
        logger.info(
            "Gemma 4 audio backend loaded model_id=%s cache_dir=%s device=%s dtype=%s",
            self.model_id,
            self.cache_dir,
            self.device,
            self.dtype,
        )

    def _read_audio(self, audio: bytes) -> tuple[list[float], int]:
        if sf is None:
            pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            return pcm.tolist(), self.input_sample_rate
        try:
            with io.BytesIO(audio) as buffer:
                data, sample_rate = sf.read(buffer, dtype="float32", always_2d=False)
            if hasattr(data, "tolist"):
                waveform = data.tolist()
            else:
                waveform = list(data)
            if isinstance(waveform, list) and waveform and isinstance(waveform[0], list):
                waveform = [float(item) for item in waveform[0]]
            return waveform, int(sample_rate)
        except Exception:
            pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            return pcm.tolist(), self.input_sample_rate

    def _wave_to_bytes(self, waveform: list[float], sample_rate: int) -> bytes:
        if sf is None:
            pcm = (np.asarray(waveform, dtype=np.float32) * 32767.0).clip(-32768, 32767).astype(np.int16)
            return pcm.tobytes()
        with io.BytesIO() as buffer:
            sf.write(buffer, waveform, sample_rate, format="WAV")
            return buffer.getvalue()

    def _chunk_audio(self, waveform: list[float], sample_rate: int) -> list[list[float]]:
        total_seconds = len(waveform) / float(sample_rate)
        if total_seconds <= self.max_audio_seconds:
            return [waveform]

        chunk_size = max(1, int(self.chunk_seconds * sample_rate))
        overlap = max(0, int(self.chunk_overlap_seconds * sample_rate))
        step = max(1, chunk_size - overlap)
        chunks: list[list[float]] = []
        start = 0
        while start < len(waveform):
            end = min(len(waveform), start + chunk_size)
            chunks.append(waveform[start:end])
            if end >= len(waveform):
                break
            start += step
        return chunks

    def _transcribe_sync(self, audio: bytes, language: str | None) -> Transcript:
        if not audio:
            return Transcript(
                text="",
                language=language or self.language or "en",
                confidence=None,
                latency_ms=0.0,
                metadata={"backend": "gemma4_audio", "empty_audio": True},
            )

        if not self.loaded:
            raise RuntimeError("Gemma runtime is not loaded")

        waveform, sample_rate = self._read_audio(audio)
        chunks = self._chunk_audio(waveform, sample_rate)
        cumulative = ""
        partials: list[str] = []
        timestamps: list[dict[str, Any]] = []
        current_start = 0.0

        for index, chunk in enumerate(chunks, start=1):
            chunk_bytes = self._wave_to_bytes(chunk, sample_rate)
            chunk_text, confidence = self._infer_chunk_sync(chunk_bytes, language)
            cumulative = _merge_transcripts(cumulative, chunk_text)
            partials.append(cumulative)
            timestamps.append(
                {
                    "chunk_index": index,
                    "start_seconds": round(current_start, 3),
                    "duration_seconds": round(len(chunk) / float(sample_rate), 3),
                    "confidence": confidence,
                    "text": chunk_text,
                }
            )
            current_start += max(0.0, (len(chunk) - int(self.chunk_overlap_seconds * sample_rate)) / float(sample_rate))

        final_text = " ".join(cumulative.split())
        return Transcript(
            text=final_text,
            language=language or self.language or "en",
            confidence=timestamps[-1]["confidence"] if timestamps else None,
            latency_ms=0.0,
            partial_transcripts=partials,
            timestamps=timestamps,
            metadata={
                "backend": "gemma4_audio",
                "model_id": self.model_id,
                "device": self.device,
                "dtype": self.dtype,
                "chunk_seconds": self.chunk_seconds,
                "overlap_seconds": self.chunk_overlap_seconds,
            },
        )

    def _infer_chunk_sync(self, audio_bytes: bytes, language: str | None) -> tuple[str, float | None]:
        prompt = _language_prompt(language or self.language)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()

            try:
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "audio", "audio": tmp.name},
                        ],
                    }
                ]
                inputs = self._processor.apply_chat_template(  # type: ignore[union-attr]
                    messages,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    add_generation_prompt=True,
                )
                if hasattr(inputs, "to"):
                    inputs = inputs.to(self._model.device)  # type: ignore[union-attr]

                outputs = self._model.generate(  # type: ignore[union-attr]
                    **inputs,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,
                )

                input_len = inputs["input_ids"].shape[-1] if "input_ids" in inputs else 0
                decoded = self._processor.decode(outputs[0][input_len:], skip_special_tokens=False)  # type: ignore[union-attr]
                response = decoded.strip()
                if hasattr(self._processor, "parse_response"):
                    parsed = self._processor.parse_response(response)  # type: ignore[union-attr]
                else:
                    parsed = response
                text = self._extract_text(parsed, response)
                confidence = self._extract_confidence(parsed)
                return text, confidence
            except Exception as exc:
                self.last_error = str(exc)
                raise

    def _extract_text(self, parsed: Any, fallback: str) -> str:
        if isinstance(parsed, str):
            return parsed.strip() or fallback
        if isinstance(parsed, dict):
            for key in ("transcript", "text", "answer", "output"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if parsed:
                return json.dumps(parsed, ensure_ascii=False)
        if parsed is None:
            return fallback
        return str(parsed).strip() or fallback

    def _extract_confidence(self, parsed: Any) -> float | None:
        if isinstance(parsed, dict):
            confidence = parsed.get("confidence")
            if isinstance(confidence, (int, float)):
                return float(confidence)
        return None

    async def transcribe(self, audio: bytes, language: str | None = None) -> Transcript:
        await self._ensure_loaded()
        started = time.perf_counter()
        transcript = await asyncio.to_thread(self._transcribe_sync, audio, language)
        transcript.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        transcript.metadata.update(
            {
                "model_path": self.model_path,
                "load_time_ms": self.load_time_ms,
                "streaming": self.streaming,
            }
        )
        return transcript

    async def transcribe_stream(self, audio: bytes, language: str | None = None) -> AsyncIterator[TranscriptEvent]:
        transcript = await self.transcribe(audio, language)
        for partial in transcript.partial_transcripts:
            yield TranscriptEvent(
                type="partial_transcript",
                text=partial,
                language=transcript.language,
                confidence=transcript.confidence,
                metadata={"backend": "gemma4_audio"},
            )
        yield TranscriptEvent(
            type="final_transcript",
            text=transcript.text,
            language=transcript.language,
            confidence=transcript.confidence,
            metadata={"backend": "gemma4_audio"},
        )

    async def health(self) -> dict[str, object]:
        return {
            "loaded": self.loaded,
            "status": "ready" if self.loaded else "unavailable",
            "model": self.model_id,
            "model_name": self.model_id,
            "model_path": self.model_path,
            "device": self.device,
            "dtype": self.dtype,
            "streaming_supported": True,
            "backend_latency": self.load_time_ms,
            "last_error": self.last_error,
        }

    async def metrics(self) -> dict[str, object]:
        return {
            "voice_backend": "gemma4_audio",
            "backend_loaded": self.loaded,
            "streaming_supported": True,
            "fallback_active": False,
            "model_name": self.model_id,
            "backend_latency": self.load_time_ms,
            "last_error": self.last_error,
        }

    async def shutdown(self) -> None:
        self._processor = None
        self._model = None


Gemma4AudioSTTEngine = Gemma4AudioBackend
