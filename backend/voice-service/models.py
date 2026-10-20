from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class CacheStatus(str, Enum):
    hit = "hit"
    miss = "miss"
    bypass = "bypass"


class AudioManifest(BaseModel):
    chapter_id: str
    summary: Optional[str] = None
    glossary: Optional[str] = None
    concepts: list[str] = Field(default_factory=list)


class STTRequest(BaseModel):
    language: Optional[str] = "en"
    enable_partial_transcripts: bool = False


class STTResponse(BaseModel):
    success: bool = True
    transcript: str = ""
    language: str = "en"
    partial_transcripts: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class TTSRequest(BaseModel):
    text: str
    voice: str = "default"
    language: str = "en"
    stream: bool = False
    format: str = "wav"
    cache: bool = True


class TTSResponse(BaseModel):
    success: bool = True
    audio_id: Optional[str] = None
    audio_url: Optional[str] = None
    cache_status: CacheStatus = CacheStatus.miss
    format: str = "wav"
    duration_ms: Optional[int] = None


class VoiceQueryRequest(BaseModel):
    question: Optional[str] = None
    query: Optional[str] = None
    grade: Optional[int] = None
    subject: Optional[str] = None
    chapter_id: Optional[str] = None
    topic: Optional[str] = None
    language: Optional[str] = "en"
    prefer_cached_audio: bool = True
    require_curriculum_context: bool = True


class VoiceQueryResponse(BaseModel):
    success: bool = True
    answer_text: str = ""
    audio_id: Optional[str] = None
    audio_url: Optional[str] = None
    cache_status: CacheStatus = CacheStatus.miss
    response_source: str = "rag_tutor"
    context_used: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
