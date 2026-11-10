from __future__ import annotations

import logging
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any

from shared.config import get_settings

logger = logging.getLogger(__name__)


class ServiceDomain(str, Enum):
    PIHUB = "pihub"
    PACK_SERVICE = "pack-service"
    CONTENT_PIPELINE = "content-pipeline"
    INFERENCE_SERVICE = "inference-service"
    GATEWAY = "gateway"


# Explicit Data Ownership Registry
DATA_OWNERSHIP_CATALOG: dict[ServiceDomain, dict[str, Any]] = {
    ServiceDomain.PIHUB: {
        "db_filename": "pihub.sqlite3",
        "owned_entities": ["classrooms", "devices", "students", "student_progress", "quiz_sessions", "sync_events"],
        "description": "Owns classroom rosters, student state, quiz evaluation records, and offline sync journals.",
    },
    ServiceDomain.PACK_SERVICE: {
        "db_filename": "pack_service.sqlite3",
        "owned_entities": ["packs", "pack_manifests", "pack_versions", "pack_compilations", "export_jobs"],
        "description": "Owns educational pack compilation status, versioning, chapter manifests, and bundle archives.",
    },
    ServiceDomain.CONTENT_PIPELINE: {
        "db_filename": "content_pipeline.sqlite3",
        "owned_entities": ["ingestion_jobs", "document_metadata", "chunk_provenance", "enrichment_records"],
        "description": "Owns document parsing states, chunk indexes, enriched media references, and vector embeddings.",
    },
    ServiceDomain.INFERENCE_SERVICE: {
        "db_filename": "inference_service.sqlite3",
        "owned_entities": ["model_configs", "model_benchmarks", "inference_metrics", "prompt_cache_stats"],
        "description": "Owns LLM model metadata, runtime parameter configurations, and inference telemetry.",
    },
    ServiceDomain.GATEWAY: {
        "db_filename": "gateway_audit.sqlite3",
        "owned_entities": ["audit_logs", "api_keys", "rate_limit_state", "service_registry_cache"],
        "description": "Owns public API routing metadata, client access logs, and persistent rate limit tracking.",
    },
}


def get_service_db_path(domain: ServiceDomain | str) -> Path:
    """Resolve the isolated database file path owned strictly by the specified service domain."""
    domain_enum = ServiceDomain(domain) if isinstance(domain, str) else domain
    catalog = DATA_OWNERSHIP_CATALOG[domain_enum]
    filename = catalog["db_filename"]

    backend_root = Path(__file__).resolve().parents[2]
    storage_dir = backend_root / ".local_run" / "databases" / domain_enum.value
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir / filename


def get_service_db_connection(domain: ServiceDomain | str) -> sqlite3.Connection:
    """Returns an isolated SQLite connection configured with WAL mode and foreign key integrity."""
    db_path = get_service_db_path(domain)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_all_service_databases() -> None:
    """Initialize isolated schemas for each microservice following Database-per-Service principles."""
    # 1. Pack Service DB
    with get_service_db_connection(ServiceDomain.PACK_SERVICE) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS pack_records (
            pack_id TEXT PRIMARY KEY,
            grade INTEGER NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT,
            version TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            manifest_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pack_grade_subject ON pack_records(grade, subject);
        """)

    # 2. Content Pipeline DB
    with get_service_db_connection(ServiceDomain.CONTENT_PIPELINE) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            job_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            status TEXT NOT NULL,
            chunks_count INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        """)

    # 3. Inference Service DB
    with get_service_db_connection(ServiceDomain.INFERENCE_SERVICE) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS inference_telemetry (
            request_id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            duration_ms REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """)

    # 4. Gateway Audit DB
    with get_service_db_connection(ServiceDomain.GATEWAY) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS gateway_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            client_ip TEXT,
            endpoint TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            latency_ms REAL NOT NULL,
            timestamp TEXT NOT NULL
        );
        """)

    logger.info("[DATABASE_PER_SERVICE] Isolated schemas verified for all microservices.")
