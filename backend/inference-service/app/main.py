from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.context import ExperimentContextProvider, PackContextProvider
from app.evaluation import evaluate_answer
from app.language import LanguageAdapter
from app.orchestration import TutorOrchestrator
from app.sessions import SessionManager
from shared.config import get_settings as get_shared_settings
from shared.text_normalization import language_display_name, normalize_language_code
from shared.topic_normalization import normalize_subject, normalize_topic


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


_default_model_path = os.getenv("LLAMA_MODEL_PATH", "/models/model.gguf")



class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    inference_service_url: str = Field(default="http://0.0.0.0:8010", alias="INFERENCE_SERVICE_URL")
    content_pipeline_url: str = Field(default="http://content-pipeline:8001", alias="CONTENT_PIPELINE_URL")
    llama_server_host: str = Field(default="127.0.0.1", alias="LLAMA_SERVER_HOST")
    llama_server_port: int = Field(default=8081, alias="LLAMA_SERVER_PORT")
    llama_model_path: str = Field(default=_default_model_path, alias="LLAMA_MODEL_PATH")

    llama_context_size: int = Field(default=2048, alias="LLAMA_CONTEXT_SIZE")
    llama_max_tokens: int = Field(default=256, alias="LLAMA_MAX_TOKENS")
    llama_temperature: float = Field(default=0.4, alias="LLAMA_TEMPERATURE")
    llama_top_p: float = Field(default=0.9, alias="LLAMA_TOP_P")
    llama_prompt_cache_size: int = Field(default=128, alias="LLAMA_PROMPT_CACHE_SIZE")
    content_generation_backend: str = Field(default="llama", alias="CONTENT_GENERATION_BACKEND")
    ollama_base_url: str = Field(default="http://ollama:11434", alias="OLLAMA_BASE_URL")
    gemma_content_model: str = Field(default="gemma4", alias="GEMMA_CONTENT_MODEL")
    content_generation_temperature: float = Field(default=0.2, alias="CONTENT_GENERATION_TEMPERATURE")
    content_generation_top_p: float = Field(default=0.9, alias="CONTENT_GENERATION_TOP_P")
    content_generation_retries: int = Field(default=3, alias="CONTENT_GENERATION_RETRIES")
    content_generation_allow_fallback: bool = Field(default=True, alias="CONTENT_GENERATION_ALLOW_FALLBACK")
    stream_batch_chars: int = Field(default=120, alias="STREAM_BATCH_CHARS")
    prompt_context_limit: int = Field(default=1800, alias="PROMPT_CONTEXT_LIMIT")
    max_concurrent_inference: int = Field(default=2, alias="MAX_CONCURRENT_INFERENCE")
    inference_queue_timeout_seconds: float = Field(default=180.0, alias="INFERENCE_QUEUE_TIMEOUT_SECONDS")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    question: str
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    topic: str | None = None
    language: str | None = None
    limit: int = Field(default=5, ge=1, le=20)
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    intent: str | None = None
    confidence: float | None = None
    conversationHistory: list[Any] = Field(default_factory=list)
    conversation_history: list[Any] = Field(default_factory=list)
    sessionState: dict[str, Any] = Field(default_factory=dict)
    session_state: dict[str, Any] = Field(default_factory=dict)
    executionMode: str | None = None
    execution_mode: str | None = None
    normalized_topic: str | None = None
    original_topic: str | None = None
    topic_alias: str | None = None
    asset_context: list[dict[str, Any]] = Field(default_factory=list)
    asset_search: dict[str, Any] = Field(default_factory=dict)
    simulation_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_frontend_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if "conversation_history" in normalized and "conversationHistory" not in normalized:
            normalized["conversationHistory"] = normalized["conversation_history"]
        if "session_state" in normalized and "sessionState" not in normalized:
            normalized["sessionState"] = normalized["session_state"]
        if "execution_mode" in normalized and "executionMode" not in normalized:
            normalized["executionMode"] = normalized["execution_mode"]

        if not normalized.get("question"):
            for key in ("query", "prompt", "message", "text", "userQuestion"):
                value = normalized.get(key)
                if value:
                    normalized["question"] = value
                    break

        for key in (
            "question",
            "subject",
            "chapter",
            "topic",
            "language",
            "intent",
            "executionMode",
            "execution_mode",
            "normalized_topic",
            "original_topic",
            "topic_alias",
        ):
            value = normalized.get(key)
            if value is not None and not isinstance(value, str):
                normalized[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

        limit = normalized.get("limit")
        if limit is not None:
            try:
                coerced_limit = int(limit)
                if coerced_limit < 1 or coerced_limit > 20:
                    normalized["limit"] = 5
            except (TypeError, ValueError):
                normalized["limit"] = 5

        return normalized

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question is required")
        return value.strip()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return normalize_language_code(value) or None


class TutorRequest(ChatRequest):
    hint_style: str = Field(default="guided")


class ContextResult(BaseModel):
    id: str
    score: float | None = None
    text: str
    metadata: dict[str, Any]


class InferenceResponse(BaseModel):
    answer: str
    model: str
    language: str | None = None
    context: list[ContextResult] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    service: str
    checks: dict[str, Any]


class ContentGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    section_id: str | None = None
    title: str
    content: str
    concepts: list[str] = Field(default_factory=list)
    grade: int | None = None
    subject: str | None = None
    chapter: str | None = None
    language: str | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content is required")
        return value

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return normalize_language_code(value) or None


class SummarySchema(BaseModel):
    title: str
    summary: str
    keyPoints: list[str] = Field(default_factory=list)
    importantFacts: list[str] = Field(default_factory=list)


class ChapterNotesSchema(BaseModel):
    chapter_title: str
    one_sentence_summary: str
    core_points: list[str] = Field(default_factory=list)
    important_formulas: list[str] = Field(default_factory=list)
    experiments: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    real_world_applications: list[str] = Field(default_factory=list)
    quiz_focus: list[str] = Field(default_factory=list)


class FlashcardSchema(BaseModel):
    question: str
    answer: str
    difficulty: str = "medium"


class FlashcardsSchema(BaseModel):
    items: list[FlashcardSchema] = Field(default_factory=list)


class QuizItemSchema(BaseModel):
    question: str
    options: list[str] = Field(min_length=4, max_length=4)
    answer: str
    explanation: str


class QuizSchema(BaseModel):
    items: list[QuizItemSchema] = Field(default_factory=list)


class GlossaryItemSchema(BaseModel):
    term: str
    definition: str


class GlossarySchema(BaseModel):
    items: list[GlossaryItemSchema] = Field(default_factory=list)


class LearningObjectiveSchema(BaseModel):
    objective: str


class LearningObjectivesSchema(BaseModel):
    items: list[LearningObjectiveSchema] = Field(default_factory=list)


class MisconceptionItemSchema(BaseModel):
    misconception: str
    correction: str
    why_students_confuse_it: str


class MisconceptionsSchema(BaseModel):
    items: list[MisconceptionItemSchema] = Field(default_factory=list)


class ApplicationItemSchema(BaseModel):
    concept: str
    real_world_use: str
    explanation: str


class ApplicationsSchema(BaseModel):
    items: list[ApplicationItemSchema] = Field(default_factory=list)


class PromptCache:
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self._store: OrderedDict[str, str] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            value = self._store.get(key)
            if value is not None:
                self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)


class ModelManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_path = Path(settings.llama_model_path)
        self.server_process: subprocess.Popen[str] | None = None
        self.http = httpx.AsyncClient(timeout=180.0)
        self.chat_prompt_cache = PromptCache(settings.llama_prompt_cache_size)
        self.artifact_prompt_cache = PromptCache(settings.llama_prompt_cache_size)
        self.active_model = self.model_path.name
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_inference)

    @property
    def prompt_cache(self) -> PromptCache:
        """Deprecated alias for chat_prompt_cache."""
        return self.chat_prompt_cache

    def is_ready(self) -> bool:
        return self.server_process is not None and self.server_process.poll() is None

    def start_server(self) -> None:
        if self.is_ready() or not self.model_path.exists():
            return

        import shutil
        server_bin = shutil.which("llama-server") or "llama-server"

        command = [
            server_bin,
            "--host",
            self.settings.llama_server_host,
            "--port",
            str(self.settings.llama_server_port),
            "--model",
            str(self.model_path),
            "--ctx-size",
            str(self.settings.llama_context_size),
        ]
        try:
            self.server_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        except Exception as exc:
            logger.warning("[MODEL_MANAGER] FAILED_TO_START_SERVER error=%s", exc)


    async def close(self) -> None:
        if self.server_process and self.server_process.poll() is None:
            self.server_process.terminate()
            await asyncio.to_thread(self.server_process.wait, 10)
        await self.http.aclose()

    async def health(self) -> dict[str, Any]:
        checks: dict[str, Any] = {"model_path": str(self.model_path)}
        if not self.model_path.exists():
            checks["server"] = {"status": "missing_model"}
            return {"status": "degraded", "checks": checks}

        self.start_server()
        if not self.is_ready():
            checks["server"] = {"status": "starting"}
            return {"status": "degraded", "checks": checks}

        try:
            response = await self.http.get(f"http://{self.settings.llama_server_host}:{self.settings.llama_server_port}/v1/models")
            checks["server"] = {"status_code": response.status_code, "body": response.text[:200]}
            return {"status": "ok" if response.is_success else "degraded", "checks": checks}
        except Exception as exc:
            checks["server"] = {"status": "error", "detail": str(exc)}
            return {"status": "degraded", "checks": checks}


settings = Settings()
shared_settings = get_shared_settings()
manager = ModelManager(settings)
tutor_metrics: deque[dict[str, Any]] = deque(maxlen=100)
retrieval_metrics: deque[dict[str, Any]] = deque(maxlen=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start_server()
    try:
        yield
    finally:
        await manager.close()


app = FastAPI(title="inference-service", lifespan=lifespan)


@app.middleware("http")
async def log_request_body(request: Request, call_next):
    if request.url.path == "/ai/tutor":
        body = await request.body()
        logger.info("[TUTOR] RAW_REQUEST=%s", body.decode("utf-8", errors="ignore"))

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive)

    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.error("[TUTOR] VALIDATION_ERROR=%s", exc.errors())
    return JSONResponse(
        status_code=400,
        content={"detail": exc.errors()},
    )


def _record_tutor_metric(metric: dict[str, Any]) -> None:
    tutor_metrics.appendleft({
        "recorded_at": datetime_now_utc(),
        **metric,
    })


def _record_retrieval_metric(metric: dict[str, Any]) -> None:
    retrieval_metrics.appendleft({
        "recorded_at": datetime_now_utc(),
        **metric,
    })


def datetime_now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


def _metrics_summary() -> dict[str, Any]:
    metrics = list(tutor_metrics)
    if not metrics:
        return {
            "count": 0,
            "averages": {},
            "recent": [],
        }
    numeric_keys = ("retrieval_ms", "rerank_ms", "llm_ms", "total_ms", "chunks_retrieved", "chunks_used")
    averages = {
        key: round(sum(float(metric.get(key, 0.0)) for metric in metrics) / len(metrics), 2)
        for key in numeric_keys
    }
    return {
        "count": len(metrics),
        "averages": averages,
        "recent": metrics[:20],
    }


def _retrieval_metrics_summary() -> dict[str, Any]:
    metrics = list(retrieval_metrics)
    if not metrics:
        return {
            "count": 0,
            "averages": {},
            "recent": [],
        }
    numeric_keys = ("retrieval_ms", "chunks_retrieved", "chunks_used")
    averages = {
        key: round(sum(float(metric.get(key, 0.0)) for metric in metrics) / len(metrics), 2)
        for key in numeric_keys
    }
    fallback_counts: dict[str, int] = {}
    for metric in metrics:
        reason = str(metric.get("fallback_reason") or "none")
        fallback_counts[reason] = fallback_counts.get(reason, 0) + 1
    return {
        "count": len(metrics),
        "averages": averages,
        "fallback_counts": fallback_counts,
        "recent": metrics[:40],
    }


def _grade_instructions(grade: int | None) -> str:
    if grade is None:
        return "Use clear educational language."
    if grade <= 4:
        return "Use very simple words, short sentences, and concrete examples."
    if grade <= 8:
        return "Explain step by step with balanced detail and a helpful example."
    return "Provide structured but concise explanations with key concepts and reasoning."


def _language_label(language: str | None) -> str:
    code = normalize_language_code(language)
    if not code:
        return "English"
    return language_display_name(code)


def _language_instruction(language: str | None) -> str:
    code = normalize_language_code(language)
    if code == "kn":
        return "Respond entirely in Kannada script and language. Translate all technical terms into Kannada, do not use English words."
    if code == "hi":
        return "Respond entirely in Hindi in Devanagari script. Translate all technical terms into Hindi, do not use English words."
    return "Use clear natural English."


def _sanitize_prompt_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""

    normalized = unicodedata.normalize("NFKC", text)
    cleaned: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if char in {"\n", "\r", "\t"}:
            cleaned.append("\n" if char != "\t" else " ")
            continue
        if category.startswith("C"):
            continue
        cleaned.append(char)

    sanitized = "".join(cleaned)
    # Strip dangerous LLM control tokens and injection headers to prevent prompt injection
    injection_tokens = [
        "<|im_start|>", "<|im_end|>", "<|endoftext|>",
        "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>",
        "### Instruction:", "### System:", "### Response:",
        "SYSTEM:", "USER:", "ASSISTANT:"
    ]
    for token in injection_tokens:
        sanitized = re.sub(re.escape(token), "", sanitized, flags=re.IGNORECASE)

    sanitized_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in sanitized.splitlines()]
    return "\n".join(sanitized_lines).strip()



def _build_system_prompt(request: ChatRequest, style: str) -> str:
    language = normalize_language_code(request.language)
    parts = [
        _sanitize_prompt_text("You are an expert educational tutoring assistant for distributed classroom platform."),
        _sanitize_prompt_text("Provide clear, accurate educational explanations aligned with curriculum standards."),
        _sanitize_prompt_text("Prioritize learning value and relevance over verbosity."),
        _grade_instructions(request.grade),
        _sanitize_prompt_text(f"Subject: {request.subject or 'General Studies'}"),
        _sanitize_prompt_text(f"Chapter: {request.chapter or 'General Topics'}"),
        _sanitize_prompt_text(f"Topic: {request.topic or 'General'}"),
        _sanitize_prompt_text(f"Language: {_language_label(language or request.language)} ({language or 'en'})"),
        _sanitize_prompt_text(f"Teaching style: {style}"),
        _sanitize_prompt_text(_language_instruction(language)),
        _sanitize_prompt_text("IMPORTANT: Use provided context only if it is relevant and accurate."),
        _sanitize_prompt_text("If context is not helpful, answer directly based on educational knowledge."),
    ]
    return "\n".join(parts)


async def _search_context(query: str, limit: int, metadata: dict[str, Any]) -> tuple[list[ContextResult], bool]:
    payload: dict[str, Any] = {"query": query, "limit": limit}
    if metadata:
        payload["metadata"] = metadata
    try:
        response = await manager.http.post(f"{shared_settings.content_pipeline_url}/rag/search", json=payload)
        if response.is_error:
            logger.error(f"[RAG] content-pipeline error: {response.text}")
            return [], True
        data = response.json().get("results", [])
        logger.info(f"[RAG] content-pipeline returned {len(data)} results for payload {payload}")
        return [ContextResult.model_validate(item) for item in data], False
    except Exception as exc:
        logger.error(f"[RAG] content-pipeline exception: {exc}")
        return [], True


async def _retrieve_context_with_observability(request: ChatRequest) -> tuple[list[ContextResult], dict[str, Any]]:
    normalization = normalize_topic(request.normalized_topic or request.topic, request.question)
    normalized_topic = normalization.normalized_topic
    query = _sanitize_prompt_text(request.question)
    if normalized_topic and normalized_topic not in request.question.lower():
        query = f"{query} {_sanitize_prompt_text(normalized_topic)}"

    normalized_lang = normalize_language_code(request.language) or None

    metadata = {key: value for key, value in {
        "grade": request.grade,
        "subject": normalize_subject(request.subject),
        "chapter": request.chapter,
        "topic": normalized_topic,
        "language": normalized_lang,
        "source": "generated_pack",
    }.items() if value is not None}

    started = time.perf_counter()
    chunks, fallback_error = await _search_context(query, request.limit, metadata)
    fallback_reason: str | None = "content_pipeline_error" if fallback_error else None

    if not chunks and metadata and not fallback_error:
        fallback_reason = "metadata_relaxed"
        chunks, _ = await _search_context(query, request.limit, {})

    if not chunks and not fallback_reason:
        fallback_reason = "no_matching_chunks"

    rag_ms = (time.perf_counter() - started) * 1000
    retrieval_meta = {
        "query": request.question,
        "intent": request.intent,
        "topic": request.topic,
        "normalized_topic": normalized_topic,
        "matched_alias": normalization.matched_alias,
        "response_source": "rag" if chunks else "direct_llm",
        "fallback_reason": fallback_reason,
        "rag_fallback": fallback_error,
        "asset_matches": len(request.asset_context),
        "chunks_retrieved": len(chunks),
        "chunks_used": len(chunks),
        "asset_ms": 0.0,
        "rag_ms": round(rag_ms, 2),
        "total_ms": round(rag_ms, 2),
    }
    _record_retrieval_metric(retrieval_meta)
    logger.info(
        "[RAG] RETRIEVAL query=%s normalized_topic=%s intent=%s chunks_retrieved=%s chunks_used=%s fallback_reason=%s",
        retrieval_meta["query"],
        retrieval_meta["normalized_topic"],
        retrieval_meta["intent"],
        retrieval_meta["chunks_retrieved"],
        retrieval_meta["chunks_used"],
        retrieval_meta["fallback_reason"],
    )
    return chunks, retrieval_meta



async def _retrieve_context(request: ChatRequest) -> list[ContextResult]:
    context, _ = await _retrieve_context_with_observability(request)
    return context


def _build_user_prompt(request: ChatRequest, context: list[ContextResult]) -> str:
    # Build context block with better formatting
    context_lines: list[str] = []
    if request.asset_context:
        context_lines.append(_sanitize_prompt_text("CURATED EDUCATIONAL ASSETS:"))
        context_lines.append("-" * 40)
        for index, item in enumerate(request.asset_context[:8], start=1):
            source_type = _sanitize_prompt_text(item.get("source_type") or "asset")
            title = _sanitize_prompt_text(item.get("title") or "")
            text = _sanitize_prompt_text(item.get("text") or "")[:250]
            context_lines.append(_sanitize_prompt_text(f"[Asset {index}: {source_type}] {title}\n{text}"))
        context_lines.append("-" * 40)

    if context:
        context_lines.append(_sanitize_prompt_text("EDUCATIONAL CONTEXT:"))
        context_lines.append("-" * 40)
        for index, item in enumerate(context, start=1):
            snippet = _sanitize_prompt_text(item.text)[:350]
            score_info = f"(relevance: {item.score:.2f})" if item.score is not None else "(relevance: unknown)"
            context_lines.append(_sanitize_prompt_text(f"[Source {index}] {score_info}\n{snippet}"))
        context_lines.append("-" * 40)

    context_block = "\n".join(context_lines)
    if len(context_block) > settings.prompt_context_limit:
        context_block = context_block[: settings.prompt_context_limit] + "\n[context truncated]"

    # Build cleaner prompt format for Phi-2
    prompt_parts = []
    if context_block:
        prompt_parts.append(context_block)
    
    prompt_parts.append(_sanitize_prompt_text(f"QUESTION: {request.question}"))
    prompt_parts.append(_sanitize_prompt_text("ANSWER (clear and educational):"))
    
    return _sanitize_prompt_text("\n".join(prompt_parts))


def _prompt_cache_key(system_prompt: str, user_prompt: str, model: str, params: dict[str, Any]) -> str:
    payload = json.dumps({"system": system_prompt, "user": user_prompt, "model": model, "params": params}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_model_output(answer: str) -> str:
    """Remove ChatML and other model special tokens from output."""
    # Strip any text after ChatML end-of-turn / role-start tokens
    # (model should stop at these but strip as safety net)
    for stop_token in ("<|im_end|>", "<|im_start|>", "<|endoftext|>"):
        idx = answer.find(stop_token)
        if idx >= 0:
            answer = answer[:idx]

    # Strip common BOS/EOS tokens
    answer = answer.replace("<s>", "").replace("</s>", "")

    # Strip Gemma/ChatML role prefixes that leaked through (e.g. "system\n", "user\n")
    answer = re.sub(r'^(system|user|assistant)\s*\n', '', answer, flags=re.IGNORECASE)

    # Strip instruction-style echo prefixes (e.g. "Instruct:", "Output:")
    answer = re.sub(r'^(instruct|output|response|answer)[:\s]+', '', answer, flags=re.IGNORECASE)

    # Clean up multiple spaces and leading/trailing whitespace
    answer = " ".join(answer.split())
    return answer.strip()


def _strip_stream_labels(answer: str) -> str:
    text = str(answer or "").strip()
    if not text:
        return text
    text = re.sub(r"^\s*\[Answer:\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\[Source\s+\d+\][^\n]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\(relevance:\s*[^)]*\)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[Source\s+\d+\][^\n]*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _looks_like_prompt_echo(answer: str) -> bool:
    text = str(answer or "").lower()
    markers = (
        "educational context:",
        "curated educational assets:",
        "question:",
        "answer (clear and educational):",
        "[source ",
        "[answer:",
        "final answer language:",
        "teaching style:",
    )
    return any(marker in text for marker in markers)


def _chapter_intro_fallback(request: ChatRequest) -> str:
    chapter = _sanitize_prompt_text(getattr(request, "chapter", None) or getattr(request, "topic", None) or "this chapter")
    code = normalize_language_code(getattr(request, "language", None))
    if code == "kn":
        return f"ಈ ಅಧ್ಯಾಯವು {chapter} ಬಗ್ಗೆ ಇದೆ. ನಾನು ಅದನ್ನು ಹಂತ ಹಂತವಾಗಿ ವಿವರಿಸಬಹುದು."
    if code == "hi":
        return f"यह अध्याय {chapter} के बारे में है। मैं इसे चरण-दर-चरण समझा सकता हूँ।"
    return f"This chapter is about {chapter}. I can explain it step by step if you want."


def _finalize_tutor_answer(answer: str, request: ChatRequest) -> str:
    cleaned = _strip_stream_labels(_clean_model_output(answer))
    if not cleaned or _looks_like_prompt_echo(cleaned):
        return _chapter_intro_fallback(request)
    return cleaned


def _extract_json_object(text: str) -> Any:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?", "", value.strip(), flags=re.I).strip()
        value = re.sub(r"```$", "", value.strip()).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start_positions = [pos for pos in (value.find("{"), value.find("[")) if pos >= 0]
        if not start_positions:
            raise
        start = min(start_positions)
        end = max(value.rfind("}"), value.rfind("]"))
        if end <= start:
            raise
        return json.loads(value[start : end + 1])


def _split_source_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।\n])\s+", " ".join(str(text or "").split()))
    return [part.strip() for part in parts if part.strip()]


def _fallback_content_artifact(request: ContentGenerationRequest, artifact: str) -> dict[str, Any]:
    sentences = _split_source_sentences(request.content)
    first_sentence = sentences[0] if sentences else request.content.strip()
    key_sentences = sentences[:4] or ([request.content.strip()] if request.content.strip() else [])
    concepts = [str(concept).strip() for concept in request.concepts if str(concept).strip()]
    chapter_title = request.title.strip() or (request.chapter or artifact.replace("-", " ").title())

    if artifact == "summary":
        return {
            "title": chapter_title,
            "summary": first_sentence[:400],
            "keyPoints": key_sentences[:4],
            "importantFacts": concepts[:4] or key_sentences[:2],
        }
    if artifact == "chapter-notes":
        return {
            "chapter_title": chapter_title,
            "one_sentence_summary": first_sentence[:300],
            "core_points": key_sentences[:5],
            "important_formulas": [item for item in concepts if any(ch.isdigit() or ch in "=/××*" for ch in item)][:5],
            "experiments": [item for item in key_sentences if any(word in item.lower() for word in ("experiment", "practical", "activity", "lab"))][:3],
            "key_terms": concepts[:8],
            "misconceptions": [first_sentence[:200]] if first_sentence else [],
            "real_world_applications": key_sentences[1:4],
            "quiz_focus": key_sentences[:4],
        }
    if artifact == "flashcards":
        items = []
        source_items = concepts or key_sentences[:4]
        for idx, item in enumerate(source_items[:6], start=1):
            items.append({
                "question": f"What is {item}?" if request.language != "kn" else f"{item} ಎಂದರೇನು?",
                "answer": key_sentences[min(idx - 1, max(0, len(key_sentences) - 1))] if key_sentences else first_sentence[:240],
                "difficulty": "medium",
            })
        return {"items": items}
    if artifact == "quiz":
        source_term = concepts[0] if concepts else (key_sentences[0] if key_sentences else chapter_title)
        return {
            "items": [
                {
                    "question": f"What best describes {source_term}?" if request.language != "kn" else f"{source_term} ಅನ್ನು ಉತ್ತಮವಾಗಿ ವಿವರಿಸುವುದು ಯಾವುದು?",
                    "options": [
                        source_term,
                        key_sentences[0] if key_sentences else "Option A",
                        key_sentences[1] if len(key_sentences) > 1 else "Option B",
                        key_sentences[2] if len(key_sentences) > 2 else "Option C",
                    ],
                    "answer": source_term,
                    "explanation": first_sentence[:240] or source_term,
                }
            ]
        }
    if artifact == "glossary":
        items = [{"term": term, "definition": first_sentence[:240] or term} for term in concepts[:8]]
        if not items and key_sentences:
            items = [{"term": chapter_title, "definition": first_sentence[:240]}]
        return {"items": items}
    if artifact == "learning-objectives":
        items = [{"objective": sentence[:240]} for sentence in key_sentences[:4]]
        if not items:
            items = [{"objective": f"Understand {chapter_title}"}]
        return {"items": items}
    if artifact == "misconceptions":
        items = [{
            "misconception": first_sentence[:220] or chapter_title,
            "correction": key_sentences[1][:220] if len(key_sentences) > 1 else first_sentence[:220],
            "why_students_confuse_it": key_sentences[2][:220] if len(key_sentences) > 2 else "It sounds similar to the main idea.",
        }]
        return {"items": items}
    if artifact == "applications":
        items = []
        for idx, sentence in enumerate(key_sentences[:3], start=1):
            items.append({
                "concept": concepts[idx - 1] if idx - 1 < len(concepts) else f"Application {idx}",
                "real_world_use": sentence[:220],
                "explanation": sentence[:220],
            })
        if not items:
            items = [{
                "concept": chapter_title,
                "real_world_use": first_sentence[:220] or chapter_title,
                "explanation": first_sentence[:220] or chapter_title,
            }]
        return {"items": items}

    return {}


def _content_section_hash(request: ContentGenerationRequest, artifact: str) -> str:
    payload = {
        "artifact": artifact,
        "title": request.title,
        "content": request.content,
        "concepts": request.concepts,
        "grade": request.grade,
        "subject": request.subject,
        "chapter": request.chapter,
        "language": request.language,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _content_system_prompt(schema_hint: str, language: str | None = None) -> str:
    language_code = normalize_language_code(language)
    language_label = _language_label(language_code)
    return (
        _sanitize_prompt_text("You generate school textbook learning artifacts from one bounded source section. ")
        + _sanitize_prompt_text("Use only facts present in the source. Do not invent concepts, dates, formulas, or names. ")
        + _sanitize_prompt_text("Return valid JSON only. No markdown. ")
        + _sanitize_prompt_text(f"Write the artifact in {language_label} ({language_code or 'en'}) when the request language is set. ")
        + _sanitize_prompt_text(f"{_language_instruction(language_code)} ")
        + _sanitize_prompt_text(f"Required schema: {schema_hint}")
    )


def _content_user_prompt(request: ContentGenerationRequest, artifact: str) -> str:
    language_code = normalize_language_code(request.language)
    concepts = ", ".join(request.concepts[:20])
    metadata = (
        f"Grade: {request.grade or ''}\n"
        f"Subject: {request.subject or ''}\n"
        f"Chapter: {request.chapter or ''}\n"
        f"Section: {request.title}\n"
        f"Language: {_language_label(language_code)} ({language_code or 'en'})\n"
        f"Known concepts: {concepts}\n"
    )
    return _sanitize_prompt_text(f"{metadata}\nGenerate {artifact} from this source section only:\n\n{request.content[:12000]}")


async def _ollama_generate(prompt: str, system_prompt: str) -> str:
    payload = {
        "model": settings.gemma_content_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "temperature": settings.content_generation_temperature,
            "top_p": settings.content_generation_top_p,
        },
    }
    response = await manager.http.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload, timeout=180.0)
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    body = response.json()
    message = body.get("message") or {}
    return str(message.get("content") or "")


async def _content_completion(system_prompt: str, user_prompt: str) -> str:
    if settings.content_generation_backend.lower() == "ollama":
        return await _ollama_generate(user_prompt, system_prompt)
    request = ChatRequest(
        question=user_prompt,
        temperature=settings.content_generation_temperature,
        top_p=settings.content_generation_top_p,
        max_tokens=1200,
    )
    return await _chat_completion(
        system_prompt,
        user_prompt,
        request,
        stream=False,
        response_format={"type": "json_object"},
    )


async def _generate_content_json(
    artifact: str,
    request: ContentGenerationRequest,
    schema_model: type[BaseModel],
    schema_hint: str,
) -> Any:
    cache_key = _content_section_hash(request, artifact)
    cached = await manager.artifact_prompt_cache.get(cache_key)
    if cached is not None:
        return schema_model.model_validate_json(cached)

    system_prompt = _content_system_prompt(schema_hint, request.language)
    user_prompt = _content_user_prompt(request, artifact)
    last_error = ""
    for attempt in range(max(1, settings.content_generation_retries)):
        try:
            prompt = user_prompt
            if attempt:
                prompt += f"\n\nPrevious JSON was invalid: {last_error}. Return corrected JSON only."
            raw = await _content_completion(system_prompt, prompt)
            parsed = _extract_json_object(raw)
            model = schema_model.model_validate(parsed)
            await manager.artifact_prompt_cache.set(cache_key, model.model_dump_json())
            return model
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[CONTENT_GENERATION_RETRY] artifact=%s attempt=%d error=%s", artifact, attempt + 1, exc)

    if settings.content_generation_allow_fallback:
        logger.warning("[CONTENT_GENERATION_FALLBACK] artifact=%s reason=generation_retries_exhausted error=%s", artifact, last_error)
        fallback = _fallback_content_artifact(request, artifact)
        if isinstance(fallback, dict):
            fallback["fallback"] = True
        model = schema_model.model_validate(fallback)
        await manager.artifact_prompt_cache.set(cache_key, model.model_dump_json())
        return model

    raise HTTPException(status_code=502, detail=f"Content generation failed for {artifact}: {last_error}")


async def _chat_completion(

    system_prompt: str,
    user_prompt: str,
    request: ChatRequest,
    stream: bool = False,
    response_format: dict[str, Any] | None = None,
) -> Any:
    # When the Ollama backend is selected, generation is served by Ollama and
    # no local GGUF model / llama-server is required. Skip the guard + server
    # launch in that case so the service runs without docker (Ollama only).
    if settings.content_generation_backend.lower() != "ollama":
        if not manager.model_path.exists():
            raise HTTPException(status_code=503, detail="No GGUF model mounted at LLAMA_MODEL_PATH")
        manager.start_server()

    base_url = f"http://{settings.llama_server_host}:{settings.llama_server_port}"
    payload = {
        "model": manager.active_model,
        "messages": [
            {"role": "system", "content": _sanitize_prompt_text(system_prompt)},
            {"role": "user", "content": _sanitize_prompt_text(user_prompt)},
        ],
        "temperature": request.temperature if request.temperature is not None else settings.llama_temperature,
        "top_p": request.top_p if request.top_p is not None else settings.llama_top_p,
        "max_tokens": request.max_tokens if request.max_tokens is not None else settings.llama_max_tokens,
        "stream": stream,
        # Stop at ChatML end-of-turn token so the model doesn't generate fake user/assistant turns
        "stop": ["<|im_end|>", "<|im_start|>", "<|endoftext|>"],
    }
    if response_format:
        payload["response_format"] = response_format

    cache_key = _prompt_cache_key(system_prompt, user_prompt, manager.active_model, payload)
    if not stream:
        cached = await manager.chat_prompt_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        async with asyncio.timeout(settings.inference_queue_timeout_seconds):
            async with manager.semaphore:
                if not stream:
                    if settings.content_generation_backend.lower() == "ollama":
                        raw = await _ollama_generate(user_prompt, system_prompt)
                        if not raw:
                            raise HTTPException(status_code=502, detail="Ollama returned empty response")
                        await manager.chat_prompt_cache.set(cache_key, raw)
                        return raw
                    response = await manager.http.post(f"{base_url}/v1/chat/completions", json=payload, timeout=180.0)
                    if response.is_error:
                        raise HTTPException(status_code=response.status_code, detail=response.text)
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    content = _finalize_tutor_answer(content, request)
                    await manager.chat_prompt_cache.set(cache_key, content)
                    return content
    except TimeoutError:
        raise HTTPException(
            status_code=530,
            detail="Inference server busy: max concurrent request limit reached or queue timeout exceeded",
        )

    async def event_stream() -> AsyncIterator[str]:
        # Accumulates the full raw response for post-processing
        full_response = ""
        # Tracks what has been emitted to compute incremental deltas
        emitted = ""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_closed = asyncio.Event()

        async def read_upstream() -> None:
            try:
                async with asyncio.timeout(settings.inference_queue_timeout_seconds):
                    async with manager.semaphore:
                        async with manager.http.stream("POST", f"{base_url}/v1/chat/completions", json=payload, timeout=180.0) as streamed:
                            async for line in streamed.aiter_lines():
                                await queue.put(line)
            except Exception as exc:
                logger.error("[STREAM] UPSTREAM_ERROR=%s", exc)
            finally:
                stream_closed.set()
                await queue.put(None)


        reader_task = asyncio.create_task(read_upstream())
        last_heartbeat = time.monotonic()

        try:
            yield f"data: {json.dumps({'chunk': '', 'done': False, 'started': True})}\n\n"
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if stream_closed.is_set():
                        break
                    if time.monotonic() - last_heartbeat >= 3.0:
                        yield f"data: {json.dumps({'chunk': '', 'done': False, 'heartbeat': True})}\n\n"
                        last_heartbeat = time.monotonic()
                    continue

                if line is None:
                    break
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue

                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break

                try:
                    payload_data = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # Stop if finish_reason is set (e.g. 'stop', 'length')
                finish_reason = payload_data.get("choices", [{}])[0].get("finish_reason")
                chunk = payload_data.get("choices", [{}])[0].get("delta", {}).get("content") or ""

                if chunk:
                    full_response += chunk
                    # Stream immediately without batching — clean only stop tokens inline
                    safe_chunk = chunk
                    for stop_tok in ("<|im_end|>", "<|im_start|>", "<|endoftext|>"):
                        if stop_tok in safe_chunk:
                            safe_chunk = safe_chunk[:safe_chunk.index(stop_tok)]
                    if safe_chunk:
                        emitted += safe_chunk
                        yield f"data: {json.dumps({'chunk': safe_chunk, 'done': False})}\n\n"
                        last_heartbeat = time.monotonic()

                if finish_reason and finish_reason != "null":
                    break

            # Post-process the complete accumulated response
            cleaned = _finalize_tutor_answer(full_response, request)
            # If the cleaned version differs significantly from what was emitted
            # (e.g. fallback triggered), send a correction chunk
            if cleaned != emitted and cleaned:
                yield f"data: {json.dumps({'chunk': cleaned, 'done': True, 'corrected': True})}\n\n"
            else:
                yield f"data: {json.dumps({'chunk': '', 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task

    return event_stream()




tutor_orchestrator = TutorOrchestrator(
    session_manager=SessionManager(),
    pack_context_provider=PackContextProvider(_retrieve_context_with_observability),
    experiment_context_provider=ExperimentContextProvider(),
    language_adapter=LanguageAdapter(),
    build_system_prompt=_build_system_prompt,
    build_user_prompt=_build_user_prompt,
    chat_completion=_chat_completion,
    active_model=lambda: manager.active_model,
)


@app.get("/metrics/tutor")
async def tutor_metrics_endpoint() -> dict[str, Any]:
    return _metrics_summary()


@app.get("/metrics/retrieval")
async def retrieval_metrics_endpoint() -> dict[str, Any]:
    return _retrieval_metrics_summary()


@app.post("/ai/content/summary", response_model=SummarySchema)
async def generate_summary(request: ContentGenerationRequest) -> SummarySchema:
    return await _generate_content_json(
        "summary",
        request,
        SummarySchema,
        '{"title": string, "summary": string, "keyPoints": string[], "importantFacts": string[]}',
    )


@app.post("/ai/content/chapter-notes", response_model=ChapterNotesSchema)
async def generate_chapter_notes(request: ContentGenerationRequest) -> ChapterNotesSchema:
    return await _generate_content_json(
        "chapter notes",
        request,
        ChapterNotesSchema,
        '{"chapter_title": string, "one_sentence_summary": string, "core_points": string[], "important_formulas": string[], "experiments": string[], "key_terms": string[], "misconceptions": string[], "real_world_applications": string[], "quiz_focus": string[]}',
    )


@app.post("/ai/content/flashcards", response_model=FlashcardsSchema)
async def generate_flashcards(request: ContentGenerationRequest) -> FlashcardsSchema:
    return await _generate_content_json(
        "flashcards",
        request,
        FlashcardsSchema,
        '{"items": [{"question": string, "answer": string, "difficulty": "easy|medium|hard"}]}',
    )


@app.post("/ai/content/quiz", response_model=QuizSchema)
async def generate_quiz(request: ContentGenerationRequest) -> QuizSchema:
    return await _generate_content_json(
        "quiz",
        request,
        QuizSchema,
        '{"items": [{"question": string, "options": [string, string, string, string], "answer": string, "explanation": string}]}',
    )


@app.post("/ai/content/glossary", response_model=GlossarySchema)
async def generate_glossary(request: ContentGenerationRequest) -> GlossarySchema:
    return await _generate_content_json(
        "glossary",
        request,
        GlossarySchema,
        '{"items": [{"term": string, "definition": string}]}',
    )


@app.post("/ai/content/learning-objectives", response_model=LearningObjectivesSchema)
async def generate_learning_objectives(request: ContentGenerationRequest) -> LearningObjectivesSchema:
    return await _generate_content_json(
        "learning-objectives",
        request,
        LearningObjectivesSchema,
        '{"items": [{"objective": string}]}',
    )


@app.post("/ai/content/misconceptions", response_model=MisconceptionsSchema)
async def generate_misconceptions(request: ContentGenerationRequest) -> MisconceptionsSchema:
    return await _generate_content_json(
        "misconceptions",
        request,
        MisconceptionsSchema,
        '{"items": [{"misconception": string, "correction": string, "why_students_confuse_it": string}]}',
    )


@app.post("/ai/content/applications", response_model=ApplicationsSchema)
async def generate_applications(request: ContentGenerationRequest) -> ApplicationsSchema:
    return await _generate_content_json(
        "applications",
        request,
        ApplicationsSchema,
        '{"items": [{"concept": string, "real_world_use": string, "explanation": string}]}',
    )


@app.get("/ai/health", response_model=HealthResponse)
async def ai_health() -> HealthResponse:
    data = await manager.health()
    return HealthResponse(status=data["status"], service="inference-service", checks=data["checks"])


@app.post("/ai/chat", response_model=InferenceResponse)
async def ai_chat(request: ChatRequest) -> InferenceResponse:
    context = await _retrieve_context(request)
    
    # If no relevant context found, indicate this in system prompt
    system_prompt = _build_system_prompt(request, "direct tutoring")
    if not context:
        system_prompt += "\n[Note: No relevant educational context was found. Answer based on your knowledge.]"
    
    user_prompt = _build_user_prompt(request, context)
    answer = await _chat_completion(system_prompt, user_prompt, request, stream=False)
    return InferenceResponse(answer=answer, model=manager.active_model, context=context)


@app.post("/ai/tutor")
async def ai_tutor(request: TutorRequest) -> Any:
    result = await tutor_orchestrator.run(request)
    retrieval_diagnostics = result.retrieval_diagnostics
    retrieval_ms = float(retrieval_diagnostics.get("retrieval_ms", 0.0))
    chunks_retrieved = result.chunks_retrieved
    chunks_used = result.chunks_used
    rerank_ms = 0.0
    logger.info(
        "[TUTOR] intent=%s topic=%s normalized_topic=%s retrieved_chunks=%s asset_used=%s response_source=%s fallback_reason=%s language=%s session_id=%s",
        request.intent,
        request.topic,
        retrieval_diagnostics.get("normalized_topic"),
        chunks_retrieved,
        bool(request.asset_context),
        "inference_service",
        retrieval_diagnostics.get("fallback_reason"),
        result.language,
        result.session_id,
    )
    if request.stream:
        llm_start = time.perf_counter()

        async def measured_stream() -> AsyncIterator[str]:
            try:
                if result.stream is None:
                    return
                async for chunk in result.stream:
                    yield chunk
            finally:
                llm_ms = (time.perf_counter() - llm_start) * 1000
                total_ms = result.metrics.context_latency_ms + llm_ms
                _record_tutor_metric({
                    "retrieval_ms": round(retrieval_ms, 2),
                    "rerank_ms": round(rerank_ms, 2),
                    "llm_ms": round(llm_ms, 2),
                    "total_ms": round(total_ms, 2),
                    "context_latency_ms": result.metrics.context_latency_ms,
                    "language_adapter_latency_ms": result.metrics.language_adapter_latency_ms,
                    "chunks_retrieved": chunks_retrieved,
                    "chunks_used": chunks_used,
                    "stream": True,
                    "language": result.language,
                    "session_id": result.session_id,
                    "intent": request.intent,
                    "topic": request.topic,
                    "normalized_topic": retrieval_diagnostics.get("normalized_topic"),
                    "fallback_reason": retrieval_diagnostics.get("fallback_reason"),
                    "asset_used": bool(request.asset_context),
                    "response_source": "inference_service",
                })

        return StreamingResponse(measured_stream(), media_type="text/event-stream")
    _record_tutor_metric({
        "retrieval_ms": round(retrieval_ms, 2),
        "rerank_ms": round(rerank_ms, 2),
        "llm_ms": result.metrics.tutor_latency_ms,
        "total_ms": result.metrics.total_response_latency_ms,
        "context_latency_ms": result.metrics.context_latency_ms,
        "language_adapter_latency_ms": result.metrics.language_adapter_latency_ms,
        "chunks_retrieved": chunks_retrieved,
        "chunks_used": chunks_used,
        "stream": False,
        "language": result.language,
        "session_id": result.session_id,
        "intent": request.intent,
        "topic": request.topic,
        "normalized_topic": retrieval_diagnostics.get("normalized_topic"),
        "fallback_reason": retrieval_diagnostics.get("fallback_reason"),
        "asset_used": bool(request.asset_context),
        "response_source": "inference_service",
    })
    return InferenceResponse(answer=result.answer, model=result.model, language=result.language, context=result.context)


@app.post("/ai/tutor/debug")
async def ai_tutor_debug(request: TutorRequest) -> dict[str, Any]:
    debug_request = request.model_copy(update={"stream": False})
    result = await tutor_orchestrator.run(debug_request)

    def dump_context_item(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump()
        if isinstance(item, dict):
            return item
        return {
            "text": str(item),
        }

    return {
        "question": debug_request.question,
        "language": result.language,
        "model": result.model,
        "retrieved_chunks": [dump_context_item(item) for item in result.context],
        "retrieval_diagnostics": result.retrieval_diagnostics,
        "experiment_context": result.experiment_context,
        "session_context": result.session_context,
        "system_prompt": result.system_prompt,
        "user_prompt": result.user_prompt,
        "final_prompt": result.final_prompt,
        "answer": result.answer,
        "metrics": result.metrics.model_dump(),
    }


@app.post("/ai/tutor/evaluate")
async def ai_tutor_evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    evaluation = evaluate_answer(
        question=str(payload.get("question") or ""),
        answer=str(payload.get("answer") or ""),
        context=payload.get("context") if isinstance(payload.get("context"), list) else [],
        language=str(payload.get("language") or "en"),
        grade=payload.get("grade") if isinstance(payload.get("grade"), int) else None,
    )
    return evaluation.model_dump()
