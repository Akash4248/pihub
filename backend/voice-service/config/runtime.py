from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class VoiceRuntimeSettings:
    voice_tts_enabled: bool
    svara_model_path: Path
    svara_snac_decoder_path: Path
    svara_sample_rate: int
    svara_max_tokens: int
    svara_threads: int
    svara_context: int
    svara_gpu_layers: int
    svara_batch_size: int
    svara_timeout_seconds: int
    svara_metrics_path: Path
    svara_warmup_enabled: bool

    @property
    def model_name(self) -> str:
        return self.svara_model_path.name


def _path_env(name: str, default: str, fallback_rel: str) -> Path:
    val = os.getenv(name)
    if val:
        return Path(val)
    target = Path(default)
    try:
        if target.exists():
            return target
        parent = target.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return target
    except Exception:
        pass
    return Path(".local_run") / fallback_rel


@lru_cache(maxsize=1)
def get_runtime_settings() -> VoiceRuntimeSettings:
    return VoiceRuntimeSettings(
        voice_tts_enabled=_bool_env("VOICE_TTS_ENABLED", True),
        svara_model_path=_path_env("SVARA_MODEL_PATH", "/models/svara/svara-tts-v1.Q4_K_M.gguf", "models/svara/svara-tts-v1.Q4_K_M.gguf"),
        svara_snac_decoder_path=_path_env("SVARA_SNAC_DECODER_PATH", "/models/svara/snac_24khz-ONNX/onnx/decoder_model.onnx", "models/svara/snac_24khz-ONNX/onnx/decoder_model.onnx"),
        svara_sample_rate=_int_env("SVARA_SAMPLE_RATE", 24000),
        svara_max_tokens=_int_env("SVARA_MAX_TOKENS", 768),
        svara_threads=_int_env("SVARA_THREADS", 8),
        svara_context=_int_env("SVARA_CONTEXT", 4096),
        svara_gpu_layers=_int_env("SVARA_GPU_LAYERS", 99),
        svara_batch_size=_int_env("SVARA_BATCH_SIZE", 512),
        svara_timeout_seconds=_int_env("SVARA_TIMEOUT_SECONDS", 300),
        svara_metrics_path=Path(os.getenv("SVARA_RUNTIME_METRICS_PATH", "/tmp/idp_voice_runtime_metrics.json")),
        svara_warmup_enabled=_bool_env("SVARA_WARMUP_ENABLED", True),
    )
