import os
from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_storage_path(env_var_name: str, default_path: str, fallback_relative_path: str) -> str:
    val = os.getenv(env_var_name)
    if val:
        return val
    target = Path(default_path)
    try:
        if target.exists():
            return default_path
        parent = target.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return default_path
    except Exception:
        pass

    local_dir = Path(".local_run") / fallback_relative_path
    return str(local_dir.resolve())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="distributed-educational-ai-ecosystem", alias="APP_NAME")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    gateway_host: str = Field(default="0.0.0.0", alias="GATEWAY_HOST")
    gateway_port: int = Field(default=8000, alias="GATEWAY_PORT")

    content_pipeline_url: str = Field(default="http://content-pipeline:8001", alias="CONTENT_PIPELINE_URL")
    inference_service_url: str = Field(default="http://inference-service:8010", alias="INFERENCE_SERVICE_URL")
    pihub_url: str = Field(default="http://pihub:8020", alias="PIHUB_URL")
    experiment_service_url: str = Field(default="http://experiment-service:8040", alias="EXPERIMENT_SERVICE_URL")
    experiment_service_required: bool = Field(default=False, alias="EXPERIMENT_SERVICE_REQUIRED")
    voice_service_url: str = Field(default="http://voice-service:8050", alias="VOICE_SERVICE_URL")
    voice_service_required: bool = Field(default=False, alias="VOICE_SERVICE_REQUIRED")
    voice_service_enabled: bool = Field(default=False, alias="VOICE_SERVICE_ENABLED")
    gateway_http_timeout_seconds: float = Field(default=120.0, alias="GATEWAY_HTTP_TIMEOUT_SECONDS")
    discovery_port: int = Field(default=47890, alias="DISCOVERY_PORT")
    jwt_secret_key: str = Field(default="change-me-super-secret-jwt-key-min32chars", alias="JWT_SECRET_KEY")
    # Fail-closed by default: production denies anonymous service calls unless
    # explicitly opted into dev mode. Local `run_backend.sh` exports
    # ALLOW_ANONYMOUS_DEV=true so laptop dev keeps working.
    allow_anonymous_dev: bool = Field(default=False, alias="ALLOW_ANONYMOUS_DEV")
    database_url: str = Field(default="postgresql+asyncpg://postgres:postgres@db:5432/pihub", alias="DATABASE_URL")


    redis_url: str = Field(default="", alias="REDIS_URL")
    
    # MinIO Object Storage
    minio_endpoint: str = Field(default="minio:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="minioadmin", alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="educational-packs", alias="MINIO_BUCKET")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")

    qdrant_url: str = Field(default="http://qdrant:6333", alias="QDRANT_URL")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")
    qdrant_collection: str = Field(default="educational_chunks_bge_m3", alias="QDRANT_COLLECTION")

    embedding_model_name: str = Field(default="BAAI/bge-m3", alias="EMBEDDING_MODEL_NAME")
    embedding_device: str = Field(default="cpu", alias="EMBEDDING_DEVICE")
    embedding_cache_dir: str = Field(default_factory=lambda: _resolve_storage_path("EMBEDDING_CACHE_DIR", "/shared/models/sentence-transformers/embedding", "models/sentence-transformers/embedding"), alias="EMBEDDING_CACHE_DIR")
    local_retrieval_model_name: str = Field(default="BAAI/bge-m3", alias="LOCAL_RETRIEVAL_MODEL_NAME")
    local_retrieval_device: str = Field(default="cpu", alias="LOCAL_RETRIEVAL_DEVICE")
    local_retrieval_cache_dir: str = Field(default_factory=lambda: _resolve_storage_path("LOCAL_RETRIEVAL_CACHE_DIR", "/shared/models/sentence-transformers/retrieval", "models/sentence-transformers/retrieval"), alias="LOCAL_RETRIEVAL_CACHE_DIR")
    embedding_fallback: str = Field(default="none", alias="EMBEDDING_FALLBACK") # none|hash
    # Embedding backend: "auto" tries llama-server first, then sentence-transformers.
    # "llama_server" runs BGE-M3 GGUF via llama.cpp /v1/embeddings (no torch);
    # "sentence_transformers" is the legacy torch path.
    embedding_backend: str = Field(default="auto", alias="EMBEDDING_BACKEND") # auto|llama_server|sentence_transformers
    llama_server_url: str = Field(default="http://127.0.0.1:8081", alias="LLAMA_SERVER_URL")
    embedding_llama_model: str = Field(default="bge-m3", alias="EMBEDDING_LLAMA_MODEL")


    upload_dir: str = Field(default_factory=lambda: _resolve_storage_path("UPLOAD_DIR", "/shared/uploads", "uploads"), alias="UPLOAD_DIR")
    work_dir: str = Field(default_factory=lambda: _resolve_storage_path("WORK_DIR", "/shared/work", "work"), alias="WORK_DIR")
    content_dir: str = Field(default_factory=lambda: _resolve_storage_path("CONTENT_DIR", "/shared/content", "content"), alias="CONTENT_DIR")
    curriculum_graph_path: str = Field(default_factory=lambda: _resolve_storage_path("CURRICULUM_GRAPH_PATH", "/shared/work/curriculum_graph.json", "work/curriculum_graph.json"), alias="CURRICULUM_GRAPH_PATH")
    curriculum_relation_graph_path: str = Field(default_factory=lambda: _resolve_storage_path("CURRICULUM_RELATION_GRAPH_PATH", "/shared/work/curriculum_relation_graph.json", "work/curriculum_relation_graph.json"), alias="CURRICULUM_RELATION_GRAPH_PATH")
    enable_auto_ingestion: bool = Field(default=False, alias="ENABLE_AUTO_INGESTION")
    enable_ai_artifact_generation: bool = Field(default=True, alias="ENABLE_AI_ARTIFACT_GENERATION")
    enable_semantic_educational_chunking: bool = Field(default=True, alias="ENABLE_SEMANTIC_EDUCATIONAL_CHUNKING")
    enable_curriculum_graph_engine: bool = Field(default=True, alias="ENABLE_CURRICULUM_GRAPH_ENGINE")
    enable_educational_retrieval_engine: bool = Field(default=True, alias="ENABLE_EDUCATIONAL_RETRIEVAL_ENGINE")
    
    # Pack Management
    pack_storage_path: str = Field(default_factory=lambda: _resolve_storage_path("PACK_STORAGE_PATH", "/shared/packs", "packs"), alias="PACK_STORAGE_PATH")
    pack_service_url: str = Field(default="http://pack-service:8030", alias="PACK_SERVICE_URL")
    
    # Pi Cache Settings
    cache_path: str = Field(default="/cache", alias="CACHE_PATH")
    pi_cache_max_mb: int = Field(default=500, alias="PI_CACHE_MAX_MB")
    pi_cache_db_path: str = Field(default="/cache/cache_index.db", alias="PI_CACHE_DB_PATH")
    
    # Sync Engine Settings
    sync_interval_minutes: int = Field(default=60, alias="SYNC_INTERVAL_MINUTES")
    # Classroom hotspot gateway (see pihub/deployment/hotspot.py subnet
    # 192.168.42.0/24). Override with HOST_URL for laptop/docker setups.
    host_url: str = Field(default="http://192.168.42.1:8000", alias="HOST_URL")
    
    # Failover Settings
    heartbeat_interval_seconds: int = Field(default=30, alias="HEARTBEAT_INTERVAL_SECONDS")
    heartbeat_failure_threshold: int = Field(default=3, alias="HEARTBEAT_FAILURE_THRESHOLD")
    
    # Classroom Settings
    classroom_name: str = Field(default="Classroom A", alias="CLASSROOM_NAME")
    classroom_id: str = Field(default="class_001", alias="CLASSROOM_ID")
    
    # Curriculum Builder Settings
    textbooks_root: str = Field(default_factory=lambda: _resolve_storage_path("TEXTBOOKS_ROOT", "/home/akash/Desktop/PIHUB/TEXTBOOKS", "textbooks"), alias="TEXTBOOKS_ROOT")
    curriculum_build_dir: str = Field(default_factory=lambda: _resolve_storage_path("CURRICULUM_BUILD_DIR", "/shared/curriculum", "curriculum"), alias="CURRICULUM_BUILD_DIR")
    curriculum_manifest_path: str = Field(default_factory=lambda: _resolve_storage_path("CURRICULUM_MANIFEST_PATH", "/shared/curriculum/curriculum_manifest.json", "curriculum/curriculum_manifest.json"), alias="CURRICULUM_MANIFEST_PATH")
    pack_registry_path: str = Field(default_factory=lambda: _resolve_storage_path("PACK_REGISTRY_PATH", "/shared/curriculum/pack_registry.json", "curriculum/pack_registry.json"), alias="PACK_REGISTRY_PATH")
    enrichment_registry_path: str = Field(default_factory=lambda: _resolve_storage_path("ENRICHMENT_REGISTRY_PATH", "/shared/curriculum/enrichment_registry.json", "curriculum/enrichment_registry.json"), alias="ENRICHMENT_REGISTRY_PATH")
    curriculum_version: str = Field(default="1.0.0", alias="CURRICULUM_VERSION")
    max_concurrent_compilation_tasks: int = Field(default=2, alias="MAX_CONCURRENT_COMPILATION_TASKS")
    enable_gemma_content_generation: bool = Field(default=True, alias="ENABLE_GEMMA_CONTENT_GENERATION")
    content_generation_timeout_seconds: float = Field(default=20.0, alias="CONTENT_GENERATION_TIMEOUT_SECONDS")

    # Enriched content agent (videos / real-world / simulations)
    enable_web_enrichment: bool = Field(default=True, alias="ENABLE_WEB_ENRICHMENT")
    web_enrichment_timeout_seconds: float = Field(default=8.0, alias="WEB_ENRICHMENT_TIMEOUT_SECONDS")
    web_enrichment_max_per_topic: int = Field(default=4, alias="WEB_ENRICHMENT_MAX_PER_TOPIC")
    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")
    enrichment_user_agent: str = Field(
        default="Mozilla/5.0 (compatible; PIHUB-EnrichmentBot/1.0)",
        alias="ENRICHMENT_USER_AGENT",
    )
    # Languages to fetch enrichment for (comma-separated). Both are stored so the
    # offline app can serve Kannada and English side by side.
    enrichment_languages: str = Field(default="en,kn", alias="ENRICHMENT_LANGUAGES")

    # Offline media store (downloaded + compressed videos/images)
    enable_media_download: bool = Field(default=True, alias="ENABLE_MEDIA_DOWNLOAD")
    media_storage_path: str = Field(default_factory=lambda: _resolve_storage_path("MEDIA_STORAGE_PATH", "/shared/media", "media"), alias="MEDIA_STORAGE_PATH")
    media_download_timeout_seconds: float = Field(default=180.0, alias="MEDIA_DOWNLOAD_TIMEOUT_SECONDS")
    media_max_video_height: int = Field(default=480, alias="MEDIA_MAX_VIDEO_HEIGHT")
    media_video_crf: int = Field(default=28, alias="MEDIA_VIDEO_CRF")  # H.264 CRF: visually lossless-ish, small
    media_video_preset: str = Field(default="slow", alias="MEDIA_VIDEO_PRESET")
    media_max_videos_per_topic: int = Field(default=1, alias="MEDIA_MAX_VIDEOS_PER_TOPIC")
    media_max_images_per_topic: int = Field(default=2, alias="MEDIA_MAX_IMAGES_PER_TOPIC")

    # Cobalt API (self-hosted media downloader — replaces yt-dlp)
    cobalt_api_url: str = Field(default="http://cobalt:9000", alias="COBALT_API_URL")

    # Web search provider for enrichment. "auto" (default) tries SearXNG if
    # SEARXNG_URL is set, otherwise falls back to keyless DuckDuckGo HTML.
    # "searxng" forces SearXNG (and warns if not configured); "duckduckgo" forces DDG.
    web_search_provider: str = Field(default="auto", alias="WEB_SEARCH_PROVIDER")
    searxng_url: str = Field(default="", alias="SEARXNG_URL")
    # Hard cap on the local media volume (videos+images), GB. 0 = unbounded.
    media_max_total_gb: float = Field(default=20.0, alias="MEDIA_MAX_TOTAL_GB")

    # MongoDB — stores media + enrichment metadata for offline access
    mongo_url: str = Field(default="mongodb://mongo:27017", alias="MONGO_URL")
    mongo_db: str = Field(default="pihub", alias="MONGO_DB")
    enable_mongo_storage: bool = Field(default=True, alias="ENABLE_MONGO_STORAGE")

    # Qdrant collection for enrichment records (videos/articles/sims/examples)
    enrichment_collection: str = Field(default="enrichment_resources", alias="ENRICHMENT_COLLECTION")

    def validate_security_config(self) -> None:
        insecure_keys = {"change-me", "change-me-super-secret-jwt-key-min32chars", "default-secret-key-change-in-production"}
        if not self.allow_anonymous_dev:
            if self.jwt_secret_key in insecure_keys:
                raise RuntimeError(
                    "Insecure JWT_SECRET_KEY set in production environment. "
                    "Set a secure JWT_SECRET_KEY environment variable or set ALLOW_ANONYMOUS_DEV=true for local dev."
                )
            if self.minio_access_key in {"minioadmin", "minio"} or self.minio_secret_key in {"minioadmin", "minio"}:
                raise RuntimeError(
                    "Insecure MINIO credentials set in production environment. "
                    "Set MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables or set ALLOW_ANONYMOUS_DEV=true for local dev."
                )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_security_config()
    return settings

