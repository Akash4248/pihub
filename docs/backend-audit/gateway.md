# Gateway Service Audit — `pihub/backend/gateway`

**Scope:** `gateway/app/main.py` (2186 lines) + `services/auth_service.py` + `services/experiment_service_client.py` + `middleware/structlog_middleware.py` + `shared/config.py` + `shared/security.py`  
**Date:** 2026-09-12 — `develop` HEAD `8465bb9` + diff  
**Method:** `rg @(app|router).(get|post)` (321 routes total) vs `Mentora/lib` callers (18 live) + manual read + `ss -tlnp` live probe.

---

## 1. Duplicate Route Handlers

| ID | File:Line | Severity | Finding | Fix |
|---|---|---|---|---|
| D1 | `gateway/app/main.py:1066-1067` | MEDIUM | `@app.post("/content/upload")` + `@app.post("/upload")` same `upload_content()` handler — alias duplication. | Keep `/upload` canonical, 301 other. |
| D2 | `gateway/app/main.py:1300-1301,1308-1309,1316-1317,1336-1337,1353-1354,1363-1365` | MEDIUM | Voice 5 handlers each dual-decorated (`/voice/*` + `/api/voice/*` + `/api/v1/voice/stream`) = 11 registrations for 5 logics. | Single prefix `/api/voice` canonical. |
| D3 | `gateway/app/main.py:1945-1982` | MEDIUM | `flashcards(1945) / quizzes(1955) / glossary(1965) / summaries(1975)` identical except `asset_name` — copy-paste 4× `return await _asset_response`. | `def asset(kind): return _asset_response(kind, ...)` |
| D4 | `gateway/app/main.py:1419-1429` | LOW | `sync_get()` vs `sync_post()` both proxy `pihub_url/sync` via `http.get` vs `_proxy_to`. | Unified `_proxy(pihub, "/sync", method)`. |
| D5 | `gateway/app/main.py:2112-2122,2130-2140` | MEDIUM | `classroom_get/post` + `devices_get/post` identical direct proxy pattern vs `_proxy_to` elsewhere. | Consolidate. |
| D6 | `gateway/app/main.py:2143-2186` | HIGH | 9 debug proxies `/debug/curriculum` … `/debug/learning-pack-preview` all `await _proxy_json("/debug/...")` — should be loop-generated and not exposed publicly. | Loop + auth gate or remove from public gateway. |

---

## 2. Duplicate Logic

| ID | File:Line | Severity | Finding | Fix |
|---|---|---|---|---|
| L1 | `gateway/app/main.py:92-112` + `114-156` + `middleware/structlog_middleware.py:11-32` | HIGH | Double logging: `StructuredLoggingMiddleware` *and* `@app.middleware("http") structured_logging` — every request logs twice (JSON + `[TAG] REQUEST_START`). | Keep one middleware, delete the other. |
| L2 | `gateway/app/main.py:168-222` vs `788-889` | HIGH | 7 proxy helpers overlapping: `_proxy_json(168)`, `_proxy_get(175)`, `_get_json(182)`, `_post_json(192)`, `_post_multipart(202)`, `_proxy_stream(788)`, `_proxy_to(828)`, `_send_with_retry(861)`. `_proxy_get` vs `_get_json` differ only in `RequestError→503` handling. | Single `_request(base_url, method, path, ...)`. |
| L3 | `gateway/app/main.py:597-614` + `626-651` + `654-686` + `723-786` | MEDIUM | `_asset_metadata(597)` + `_asset_response(626)` + `_chapter_knowledge_assets(654)` + `_asset_search_bundle(723)` duplicate pack→preview loops. | Cache `pack→preview` map once per request. |
| L4 | `gateway/app/main.py:234-332` | MEDIUM | Pack ID normalization: `_normalize_pack_public_id(247)`, `_pack_id_suffix(255)`, `_public_pack_id(263)`, `_canonical_pack_entry(276)` recomputed 5× via `_canonical_pack_entries(403)`. | Compute once per `/packs` call. |
| L5 | `gateway/app/main.py:944-1063` vs `services/experiment_service_client.py:74-83` | MEDIUM | Health check inline 7× `http.get(.../health)` vs `ExperimentServiceClient.health()` hitting wrong `GET /experiments`. | Use client for health. |
| L6 | `gateway/app/main.py:1985-2023` vs `1157-1282` | MEDIUM | `planner_lesson(1985)` and `ai_tutor(1157)` both build 4× `_asset_response` bundles — 80% overlap. | Share `build_lesson_context()`. |

---

## 3. Bugs — Silent Fallbacks

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| S1 | `gateway/app/main.py:617-623` | HIGH | `_pack_service_packs()` swallows *all* `HTTPException` → `[]` — empty catalog indistinguishable from outage. |
| S2 | `gateway/app/main.py:638-639,669-670` | HIGH | `except HTTPException: continue` in `_asset_response` drops preview failures silently — partial results no warning. |
| S3 | `gateway/app/main.py:436-437` | MEDIUM | `logger.warning("[PACK] PIHUB_PACK_LIST_UNAVAILABLE")` then `pihub_count=0` — fallback silent. |
| S4 | `gateway/app/main.py:1452-1457` | MEDIUM | `known_hashes` JSON decode fail → `{}` → returns full pack list (massive transfer). |
| S5 | `gateway/app/main.py:1471-1472` | HIGH | `if not packs and all_packs: packs = all_packs` undoes grade/subject filter — client asks `grade=12` gets all grades, violates contract. |
| S6 | `services/auth_service.py:70-71,102-127` | MEDIUM | `verify_password`/`decode_access_token` bare `except Exception: return False/None` — no log, brute-force invisible. |

---

## 4. Bugs — Hardcoded Values

| ID | File:Line | Severity | Finding | Fix |
|---|---|---|---|---|
| H1 | `gateway/app/main.py:585,921,925` | MEDIUM | UDP `47890` hardcoded 3 places. | `settings.discovery_port` |
| H4 | `services/auth_service.py:22` | HIGH | `ACCESS_TOKEN_EXPIRE_SECONDS=2592000` (30 days) no rotation. | 1h + refresh. |
| H6 | `shared/config.py:24` | HIGH | `gateway_http_timeout_seconds=900.0` (15min) — gateway holds 900s on hung downstream. | Per-route 15s/45s. |
| H9 | `shared/config.py:25` | CRITICAL | Default `jwt_secret_key="change-me-super-secret-jwt-key-min32chars"` — all deployments share secret if env missing. | Fail-closed if not set. |

---

## 5. Bugs — Security (5 CRITICAL)

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| SEC1 | `gateway/app/main.py:25,944-2186` | CRITICAL | **No auth enforcement** — `auth_service.get_current_user_required` never imported, 60+ routes unauthenticated. |
| SEC2 | `gateway/app/main.py:1068-1116,1320` | CRITICAL | Unbounded `await file.read()` — 2GB upload → OOM, 2× memory (read + `files={"file": content}`), `shared/security.read_file_chunks_safely` exists but dead. Also `file.filename` unsanitized. |
| SEC3 | `gateway/app/main.py:120-127` | HIGH | `logger.info("[TUTOR] RAW_REQUEST=%s", body.decode())` logs PII/prompt injection plaintext. |
| SEC4 | `shared/security.py:96-97` | HIGH | `ALLOW_ANONYMOUS_DEV=true` disables all `verify_internal_api_key`. |
| SEC7 | `gateway/app/main.py:1068-1086` | MEDIUM | No `sanitize_llm_prompt` on `ai_tutor/ai_chat/planner_lesson` — prompt injection passthrough. |

---

## 6. Bugs — Error Handling

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| E1 | `gateway/app/main.py:168-172` vs `182-189` | HIGH | `_proxy_json` doesn't handle `RequestError→503` but `_get_json` does — inconsistent 500 vs 503. |
| E2 | `gateway/app/main.py:955-982` | HIGH | `health()` calls `.json()` before `is_error` check — non-JSON HTML → `JSONDecodeError` masked. |
| E3 | `gateway/app/main.py:1157-1282` | HIGH | `ai_tutor(payload: dict)` zero Pydantic validation — empty `question` still triggers 4 asset searches + LLM proxy. |
| E5 | `gateway/app/main.py:828-859` | MEDIUM | `_proxy_to` converts any `429/503` to `502 Unexpected response type` — loses status. |
| E6 | `gateway/app/main.py:861-889` | MEDIUM | `_send_with_retry` sends `json=payload` for `GET` — non-standard. |

---

## 7. Not-Wired / Dead Code

| ID | File:Line | Severity | Finding |
|---|---|---|---|
| N1 | `services/auth_service.py:1-209` | CRITICAL | Entire module dead — 0 imports in `main.py`, no `/auth/*` routes, `mentora_users.db` created at import but never queried. |
| N2 | `shared/security.py:104-155` | HIGH | `sanitize_safe_filename`, `read_file_chunks_safely`, `sanitize_llm_prompt`, `verify_jwt_token` — all dead in gateway. |
| N3 | `gateway/requirements.txt:1-6` | HIGH | Missing `websockets`, `PyJWT`, `structlog` — `import websockets:1361` mid-file breaks if not installed, even when `voice_enabled=False`. |

---

## 8. Performance

| ID | File:Line | Severity | Finding |
|---|---|---|---|
| P1 | `gateway/app/main.py:723-741,626-652` | HIGH | **N+1 HTTP storm**: `_asset_search_bundle` 4× sequential `_asset_response` × `packs` loop sequential `GET /packs/{id}/preview` — 20 packs → 80 sequential fetches. Should `asyncio.gather`. |
| P2 | `gateway/app/main.py:944-1063` | HIGH | `GET /health` 7 sequential `await http.get` — should `gather`. |
| P4 | `gateway/app/main.py:1077-1116` | HIGH | `await file.read()` double memory — use streaming. |

---

**Priority fix order:** Wire `auth_service` (SEC1+N1), enforce streaming upload (SEC2), parallelize `asyncio.gather` (P1+P2), dedupe proxy helpers (L2), merge logging middleware (L1), fix `packs/sync` filter fallback (S5), fail-closed JWT (H9).

*Evidence: 2186-line read, `Mentora/lib` 18/321 routes live, `ss -tlnp` gateway `8000 LISTEN`, `curl :8000/health` `degraded` (inference 503).*
