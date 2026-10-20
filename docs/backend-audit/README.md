# Backend Audit Index — 2026-09-12

All backend services audited for **duplication + bugs** (file:line, severity, fix). `pihub/backend` `develop` + agent delta (`8465bb9` + `git diff HEAD` 20 files).

| Service | File | Status | Critical | High | Key Finding |
|---|---|---|---|---|---|
| **Gateway** | [`gateway.md`](gateway.md) | `2186` lines, 45 issues | 5 | 15 | Unauthenticated 60+ routes + dead `auth_service` 209L, unbounded `file.read()` OOM, N+1 `asyncio.gather` missing (80 fetches sequential), `900s` timeout, aliased voice routes. 31/31 tests pass but `GET /catalog/subjects` 404 → Mentora silent mock. |
| **Content-Pipeline** | [`content-pipeline.md`](content-pipeline.md) | `1723` + 7 modules | 2 | 8 | 4 chunkers vs 1 `EducationalChunkerV2` (+ agent `SentenceChunker` not `Semantic`), hash `blake2b%384` silent collapse 384 vs 1024 dim, double BGE-M3 load 4.4GB, `PackCompiler` vs pack-service 40% LOC, 5 sync engines, `auto_ingest` race, `/shared` perms outside Docker. |
| **Inference-Service** | [`inference-service.md`](inference-service.md) | `1337` | 3 | 9 | 3 prompt builders duplicate, 2 LLM plumbing paths, semaphore bypass on streaming (unbounded `Queue`, bypass `MAX_CONCURRENT=2`), shared `PromptCache(128)` collision (chat+artifacts), hardcoded `/home/akash/.../model.gguf`, `_fallback_content_artifact` silent fake quizzes. |
| **Pack-Service** | [`pack-service.md`](pack-service.md) | `788` + 10 modules | 2 | 8 | 4 slug impls, 3 filter builders (subject aliases drift), 4 quality gates contradictory (`v2` vs `semantic-v1`), 50k `SequenceMatcher` fallback `chapter_fuzzy_match` RAM, `lang` pack `language "english" vs "en"` always invalid, `delta` not wired to Flutter, `benchmark` ignores `pack_id`. |
| **PiHub** | [`pihub.md`](pihub.md) | `864` + 7 subdirs | 3 | 8 | 3 caches vs 5 sync engines vs 2 device managers — TOCTOU `evict_if_needed`, `cache_index.json` torn write, `host_url 192.168.1.100` vs hotspot `192.168.42.0/24`, classroom name collision (`ClassroomSession` physical vs `AssignmentSession`), hardcoded secrets. |
| **Experiment-Service** | [`experiment-service.md`](experiment-service.md) | `233` + builder/runtime | 2 | 5 | `ClassroomSession` collision with pihub, IDOR `x-user-id` bypass if header omitted, `StaticFiles` traversal `/simulations/../storage/`, `POST /ai/generate-experiment` vs Dart `POST /ai/experiment/generate` 404, `Query(default=[])` mutable, builder `dict` bypasses validation. |
| **Voice + Shared** | [`voice-service+shared.md`](voice-service+shared.md) | — | 2 | 4 | `voice` 5 REST routes orphaned (app uses WS), `Gemma4Audio` 12B no quant, `shared/config:25` default JWT `change-me`, `ALLOW_ANONYMOUS_DEV` bypass, `/shared` host paths. |

**Cross-cutting TOP 6** (see `voice-service+shared.md`): `(1)` `/shared` vs `.local_run` path split → `PermissionError` outside Docker, `(2)` unauth gateway + dead `auth_service`, `(3)` unbounded uploads, `(4)` 5 sync + 3 cache, `(5)` 6 Flutter RAG + 3 server rerankers, `(6)` silent fallback chains (wrong packs / fake quizzes).

**Agent delta re-audit:** `docs/content-pipeline-re-audit-2026-09-12.md` — 31/31 green, `:8001/health` ok post-restart (391 weights), but `SentenceChunker` should be `SemanticChunker`, `generate_structured` dead until wired, `Dockerfile.pi` `COPY requirements.txt` unused, `MediaStore` lazy `mkdir` still needed. Live `Mentora` vs `backend` 303/321 routes unwired, `EndpointBuilder` 25 dead getters.

**Prioritized cross-service fix order:**

1. Gate secrets (`shared/config:25` JWT fail-closed, `gateway 900s→15s`, `pihub admin_token timing-safe`).
2. Enforce streaming uploads (`shared/security.read_file_chunks_safely` + `sanitize_safe_filename`) in all 3 gateways (`gateway`, `content-pipeline`, `pihub create_pack`) + `FileResponse` path `resolve` check.
3. Unify sync (1 `SyncEngine` DB-backed + `PersistentSyncQueue` merge) + unify cache (1 `PackManager` + `store.pack_cache`) + `temp+rename+fcntl` atomic writes.
4. Wire `auth_service` via `Depends(require_admin)` on gateway + experiment classroom + IDOR header mandatory, validate `model` with try.
5. Dedupe prompt builders (`inference-service`) + single embedding singleton (`content-pipeline`) + delete dead chunkers/pack-compilers.
6. Ship gateway aliases (`/catalog/subjects`, `/rag/retrieve`, `/ai/chat/stream`) then delete Dart `OfflineSocraticEngine` mocks — the plan in the previous message is the correct Phase 1.

*Evidence: `git diff HEAD --stat` 20 files `+364/-845`, `.venv/bin/python -m pytest backend/content-pipeline/tests -v` 31 passed 5.54s, `ss -tlnp` 8001 `LISTEN` post-restart, `curl :8001/health` `ok`, `:8000/health` `degraded` (llama-server `503 Loading model`). Full per-service `file:line` tables in linked docs.*
