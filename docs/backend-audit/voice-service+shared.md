# Voice-Service + Shared + Remaining Audit

**Scope:** `pihub/backend/voice-service` (stt/gemma4_audio, streaming) + `pihub/backend/shared` (config, security) + `pihub/backend/curriculum-builder` + cross-cutting  
**Date:** 2026-09-12

---

## Voice-Service (`voice-service/app.py` + `api/routes.py`)

| ID | File:Line | Severity | Finding |
|---|---|---|---|
| | `voice-service/Dockerfile:34-36` copies `/opt/llama.cpp` bin but `app.py` not using `which`. | LOW | Path mismatch `"/home/akash/Desktop/IDP/llama.cpp/..."` hardcoded. |
| | `api/routes.py:78-140` `POST /voice/query|/tts|/stt` + `WS /voice/stream` | MEDIUM | Mentora `VoiceProvider` uses local `LlamaEngine` + WS, not REST — 5 REST routes orphaned; `EndpointBuilder` voice getters never called, `discovery.capabilities.voice` never checked. |
| | `stt/gemma4_audio.py:77` `Gemma4AudioBackend` vs `voice-service/stt/__init__.py:7` re-export `Gemma4AudioSTTEngine` | LOW | Aliased `Gemma4AudioSTTEngine = Gemma4AudioBackend` duplication, 12B model heavy for Pi — no quantization. |
| | `requirements.txt` vs `gateway` voice proxy `shared: voice_service_url` | MEDIUM | Gateway `voice_service_enabled=false` default but `pihub` never probes it. |

*Fix:* Gate voice routes behind `voice_service_enabled`, wire `VoiceProvider` WS to `POST /voice/stt` multipart (already `api/routes.py:78` exists) or drop REST.

---

## Shared (`shared/config.py` + `shared/security.py` + `shared/pack_schemas.py`)

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| | `shared/config.py:25` `jwt_secret_key="change-me..."` | CRITICAL | Fail-closed if env missing (gateway + inference + pihub all share). |
| | `shared/config.py:30-34` `minioadmin/minioadmin`, `minio:9000` | MEDIUM | Insecure defaults. |
| | `shared/config.py:24` `gateway_http_timeout_seconds=900` | HIGH | 15min hold. |
| | `shared/security.py:20-29` JWT random `token_hex(32)` per restart → invalidation | HIGH | Persist or use env. |
| | `shared/security.py:96-97` `ALLOW_ANONYMOUS_DEV=true` bypass | HIGH | Single env disables all inter-service auth. |
| | `shared/security.py:104-155` 5 helpers dead in gateway (see `gateway.md` N2) | HIGH | `read_file_chunks_safely` never called vs `await file.read()` OOM. |

---

## Curriculum-Builder (`curriculum-builder/`)

| ID | File:Line | Finding |
|---|---|---|
| | `curriculum-builder/` `pack_validation_report.json` committed | Should be `gitignored` artifact. |
| | `curriculum-builder/complete_build/pack_registry.json` vs `pack-service/pack_storage` registry | Two registries — builder vs runtime drift. |
| | Hardcoded `TEXTBOOKS_ROOT="/home/akash/Desktop/PIHUB/TEXTBOOKS"` at `shared/config.py:80` | Host path, fails in Docker. |

---

## Cross-Cutting — Most Critical Across Services

| Rank | Issue | Services | Impact |
|---|---|---|---|
| 1 | `/shared` host path + `PermissionError` outside Docker (content-pipeline `MediaStore`, pack-service `LOCAL_RUN_BASE` vs `/shared` split) | content-pipeline, pack-service, pihub | Live `Pipeline()` throws outside `run_backend.sh`. |
| 2 | Unauthenticated gateway + dead `auth_service` | gateway, pihub, experiment-service classroom | All 60+ gateway routes 0 auth; experiment classroom no JWT. |
| 3 | Unbounded `await file.read()` file upload | gateway, content-pipeline, pihub `create_pack` | OOM on 2GB zip. |
| 4 | 5 sync engines + 3 caches | pihub | Data loss, queue divergence. |
| 5 | 6 RAG engines in Flutter + 3 server rerankers | mentora + content-pipeline | Divergent search, 3 vector stores. |
| 6 | Silent fallback chains (`_search 50k fuzzy`, `fallback_content_artifact`) | pack-service, inference-service, gateway | Wrong-content packs / fake quizzes 200. |

---

**Priority across services:** env-gate secrets (CRITICAL), streaming upload limits, unify sync + cache (1 each), wire `auth_service` via `Depends`, dedupe prompt builders + single embedding singleton.

*Evidence: `shared/config:25` default JWT, `gateway: audit/gateway.md` dead auth 209L, `content-pipeline: re-audit` `/shared/media` perm, `pack-service: audit/pack-service.md` 50k fallback.*
