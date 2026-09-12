# Inference-Service Audit — `pihub/backend/inference-service`

**Scope:** `app/main.py` (1337) + `context/` (Pack/Experiment) + `language/` + `orchestration/` + `sessions/` + `evaluation/` + `tutor/`  
**Date:** 2026-09-12  
**Method:** read + `rg` + live `POST /ai/tutor` probe

---

## 1. Duplicate Logic

| ID | File:Line | Severity | Duplication | Fix |
|---|---|---|---|---|
| D1 | `main.py:520-536` `_build_system_prompt` vs `845-855` `_content_system_prompt` vs `language/language_adapter.py:42-49` + `tutor/prompting.py:7` | HIGH | 3 system-prompt builders (grade/subject + language) → double `Final answer language:` + conflicting hints (`translate all` vs `preserve code-switched`). | Single builder. |
| D2 | `main.py:611-645` `_build_user_prompt` vs `858-869` `_content_user_prompt` vs `tutor/prompting.py:11` | HIGH | 2 user-prompt assemblies (`request.content[:12000]` vs `item.text[:350]`) — `/ai/chat` vs orchestrator diverge. | One formatter. |
| D3 | `main.py:1209-1220` `/ai/chat` vs `orchestration/tutor_orchestrator:79-86` vs `main.py:1223-1295` `/ai/tutor` | HIGH | 3 chat orchestration copies (retrieve→system→user→`_chat_completion`) — `/ai/chat` misses experiment context. | `/ai/chat` delegates to orchestrator. |
| D4 | `main.py:872-908` `_ollama_generate` + `_content_completion` vs `952-1013` `_chat_completion` | HIGH | 2 LLM plumbing paths (ollama `/api/chat` vs llama `/v1/chat/completions`), `ChatRequest` ephemeral wrapper redundant, `content_generation_backend` string compare 3× (893,922,994). | Enum + single caller. |
| D5 | `main.py:1123-1200` 8× `/ai/content/*` | MEDIUM | 8 handlers copy-paste (`artifact`,`schema`,`hint`). | Loop `/ai/content/{artifact}` table. |
| D6 | `main.py:424-441` vs `444-467` `_metrics_summary` / `_retrieval_metrics_summary` | MEDIUM | Duplicated averaging + `recent` slicing. | Helper. |

---

## 2. Bugs — Silent Fallbacks

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| B1 | `main.py:539-549,552-603` `_search_context` / `_retrieve_context_with_observability` | CRITICAL | `response.is_error` → `log` + `return []` — 5xx indistinguishable from "no chunk", LLM hallucinates. Second relaxed `topic` search also swallowed. |
| B2 | `main.py:911-948` `_generate_content_json` | CRITICAL | Default `content_generation_backend=llama` + `content_generation_allow_fallback=True` → **never calls LLM**, straight heuristic ` _fallback_content_artifact(742)` (`"An unrelated idea…"`) with 200. After 3 JSON retries also silent fallback. |
| B3 | `main.py:712-716` `_finalize_tutor_answer` + `702-709` | HIGH | Empty/prompt-echo → generic `"This chapter is about X."` silently. |
| B4 | `main.py:131-139` `ChatRequest.normalize` | HIGH | `limit` out-of-range coerced to 5 (should 400), `question` alias over 5 keys + `json.dumps` for objects lets injection as string. |

---

## 3. Bugs — Race / Concurrency

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| B9 | `main.py:990-1013` vs `1014-1096` | CRITICAL | Semaphore wraps only non-stream branch (992); streaming `event_stream` spawns `http.stream` **without** semaphore, bypasses `MAX_CONCURRENT_INFERENCE=2`, unbounded `Queue[str\|None](1019)` → OOM on fanout. |
| B10 | `main.py:991` `asyncio.timeout(180)` | HIGH | Timeout guards enqueue only, not stream lifetime — stream can hold forever. Fixed 180s duplicated. |
| B11 | `sessions/session_manager.py:10-18` `_sessions: dict` no lock, no TTL | HIGH | Concurrent `get_or_create` races (duplicate session), no eviction → leak, multi-worker separate dicts. |
| B12 | `main.py:282-300` `PromptCache(128)` shared for chat *and* artifacts (418 hex key, 6-char `hash[:24]` session alias) — heuristic caches evict real LLM; LRU thrash. | HIGH | Split caches. |
| B13 | `main.py:316-334` `ModelManager.start_server` | MEDIUM | No async lock — two `health()` race `Popen` double launch; `DEVNULL` hides errors. |

---

## 4. Bugs — Security

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| B15 | `main.py:495-517` `_sanitize_prompt_text` | HIGH | Strips only 7 ChatML tokens, missing `### Instruction:`, `SYSTEM:`, base64/unicode bypass; `request.question` also used unsanitized in `_retrieve_context` query. |
| B17 | `main.py:45,321` `llama_model_path="/home/akash/Desktop/..."` + fallback `/home/akash/Desktop/IDP/llama.cpp/...` | CRITICAL | Host absolute paths committed — fails in Docker (`/models/model.gguf`), leaks username. |
| B14 | `main.py:381-392` `log_request_body` | MEDIUM | `await request.body()` then rebuilds `Request`, logs PII unbounded at INFO. |

---

## 5. Dead / Overlap

| ID | File:Line | Finding |
|---|---|---|
| B21 | `context/pack_context_provider.py:12-36` vs `experiment_context_provider:9-51` vs `context/models.py:8-19` `TutorContext` | Thin façade — `TutorContext.chapter/concepts/glossary/faq` alias same list, three fields always `[]` because `asset_context` never populated. Two providers duplicate `sessionState` merge logic already in `SessionManager`. |
| B23 | `main.py:534-536` + `orchestration:80-81` | Double `IMPORTANT: Use context only if relevant` + `[Note: No relevant...]` contradictory signals. |
| B24 | `evaluation/answer_evaluator.py:51-94` + `monitoring/tutor_monitor.py:6` | Unused except manual `/ai/tutor/evaluate`, `TutorLatencyBreakdown` streaming path omits `tutor_latency` → 0.0. |

---

**Priority:** Fix semaphore to wrap streaming (B9), remove silent RAG/artifact fallbacks or surface `fallback:true` (B1/B2), dedupe prompt builders (D1-D3), `asyncio.Lock`+TTL for `SessionManager` (B11), split caches (B12), replace host paths with `/models/model.gguf` + `which` (B17), expand sanitizer / hierarchy isolation (B15).

*Evidence: live `:8010` `GET /ai/health` `degraded` (`llama-server:8081 /v1/models 503 Loading model`), `:8000/health` `inference_service` degraded.*
