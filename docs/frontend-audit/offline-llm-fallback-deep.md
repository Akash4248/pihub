# Offline LLM Fallback — Deep Dive

**Scope:** Offline detection + GGUF LLM loading on Android/Linux/Desktop + chat fallback path `HybridInferenceService` vs `ChapterChatScreen`  
**Date:** 2026-09-12 — live `run_backend.sh` 5/6 RUNNING, `flutter analyze` 799, `Mentora` vs `offline_tutor_app` `diff 83`

---

## 1. How Offline Fallback Should Work (Intended)

`HybridInferenceService:191` `streamTutorAnswer(backendAvailable, hasRelevantLocalContent, localCurriculumContext, question, grade, subject, chapter)` — 3 routes:

1. **backendRag** — `if backendAvailable && network.quality != offline && hasRelevantLocalContent?` → curriculum RAG then language fallback.
2. **`curriculumRag`** — `else if hasRelevantLocalContent` → `localCurriculumContext` (RagRepositoryV2 chunks) injected.
3. **`knowledgeFallback`** — else → pure knowledge prompt.

`NetworkStateService` polls `connectivity_service:80` (`InternetAddress.lookup('google.com')`) + `Connectivity().onConnectivityChanged` + `BackendAvailabilityCache` (`backend_api_service:55` `isBackendAvailable()`), `PlatformTutorInferenceGateway` for Linux GGUF, `llm_admin_channel_service:62` `MethodChannel('offline_tutor/llm')` for Android `LlamaEngine.kt`.

`ChapterChatScreen` should call `HybridInferenceService` with `backendAvailable = await BackendAvailabilityCache.isBackendAvailable()` + `hasRelevant = await RagRepository.searchChunksForChapter(chapter.id).isNotEmpty` + `localContext = chunks.map(content)`.

---

## 2. What Actually Happens (3 Breaks)

### Break 1 — Chat bypasses HybridInference entirely

`chat/presentation/chapter_chat_screen:69,99` does **not** call `HybridInferenceService`:

```dart
final gateway = ref.read(tutorInferenceGatewayProvider);
await for (final token in gateway.streamResponse(prompt: contextualPrompt))
```

It builds its own `ConversationMemoryHarness` `localRagPreCheck(chapterId:'')` (empty → 0 chunks) and streams via `PlatformTutorInferenceGateway` (which on Linux spawns `llama-cli` at `/home/akash/Desktop/IDP/llama.cpp/build/bin/llama-cli`). The `246-262` offline branching + `intentDetector` + `assetResolver` in `HybridInferenceService` is **dead for visible chat**.

### Break 2 — Offline detection is racy + wrong signal

| File:Line | Bug |
|---|---|
| `network/services/network_state_service:46-50` | Before first `_refresh():69` returns `offline` when `_lastSnapshot==null` — startup race forces cache-only even on Wi-Fi. Should return `unknown` + shimmer. |
| `services/connectivity_service:80-88` | `InternetAddress.lookup('google.com:443')` blocked on classroom LAN / Pi AP (no WAN) → false offline; contradicts `ConnectivityService.probeBackend:101` `Socket.connect(RuntimeBackendUrl.host, port)` which says online when Pi reachable. |
| `network/data/backend_api_service:55-61` | `cachedStatus != null → return` no TTL — stale `true` after Pi shutdown keeps trying backend. |
| `chapter_chat_screen:166-171` | Hardcodes `isOfflineMode:true` `activeModel:'On-Device GGUF Llama-3'` — online branch untestable. |

### Break 3 — Local LLM `isReady` is stale + GGUF discovery hard dev paths

* `hybrid_inference:247-253` empty `catch(_){}` on `ensureModelLoaded()` swallows failure, then `372-376` shows "model not ready" only after streaming fails.
* `platform_inference_adapter:47-72` stale import `package:offline_tutor_app/...` + optimistic `_ready=true` on `MissingPluginException` masks missing exe → `LinuxTutorInferenceGateway:39` later throws.
* `linux_llm_config_service:47-51` defaults `/home/akash/Desktop/IDP/llama.cpp/build/bin/llama-cli` dev path; `autoDetectModelPath:118-184` scans 9 hardcoded `~/PIHUB`, `~/.cache/lm-studio`, returns first `.gguf length>0` no `GGUF` magic check; `copyModelToAppStorage:189` blocking multi-GB `File.copy` on UI isolate, Linux `Stream.value(100)` hides.

---

## 3. All Features Touched by Offline Path

| Feature | Offline-Correct | Bug |
|---|---|---|
| **Chat** | No | Bypass as above; `OfflineTutorService` never used from chat. |
| **RAG** | Partial | `PromptBudgetManager:1500` clips before `BackendApiService:2400` backend clips; `rag_chunks_v2_fts` **missing** (`app_database:247` fallback to LIKE) → hybrid 0 rows → `LIKE` O(n) slower. |
| **Voice** | No | `offline_speech_service:21` stub `"Explain chapter..."` — user Hindi/Kannada lost; `voice/models/voice_state` diverges from `tutor/models/conversation_state`. |
| **Pack installer** | Partial | `DocumentRagIngestionScreen:48` default `/home/akash/Desktop/IDP/TEXTBOOKS` desktop leak → Android permission denied. |
| **Progress/Quiz** | Yes | `ProgressRepository` local SQLite — works offline (no backend sync, which is also broken `sync_manager:265` `/progress/update` 404). |

---

## 4. How Offline Fallback Behaves Today (Tested)

* With backend up (`:8000/health` `content_pipeline: ok`, `:8001 ok`, `llama-server:8081` 5s probe), `ChapterChatScreen` still forces offline mode — prompt shows `Offline Socratic Tutor`, LLM is `PlatformTutorInferenceGateway` direct, not `HybridInferenceService`. So with Pi up, chat **does not use RAG** `rag_chunks_v2` even when pack installed (RG-12 split brain: `LocalSearch` empty vs `RagRepositoryV2` populated).
* With backend down (kill `:8000`), fallback **does** go to `PlatformTutorInferenceGateway` → Linux `LlamaEngine` or `LlmAdminChannelService` on Android — but `isReady` race can show "Thinking..." forever because `stderr` tokens not yielded into `merged` (`linux_tutor_inference_gateway:77-124` only `stdout`).
* `flutter analyze` 799 has no `offline` error, but `git log` shows `fix(tutor): enable FormattedTextWidget`, `fix(chat): pre-flight gateway discovery` — history of offline toggle churn.

---

## 5. Fix — Single Offline Path (P0)

1. **Fix detection:** `network_state_service:46` return `NetworkQuality.unknown` pre-refresh, gate UI with shimmer; replace `google.com:443` check with `RuntimeBackendUrl.current` reachability as primary (keep WAN as secondary); add `BackendAvailabilityCache` TTL 30s + `onChange` notifier.
2. **Wire chat:** `chapter_chat_screen:64-118` replace direct `gateway.streamResponse` with `HybridInferenceService.streamTutorAnswer(backendAvailable: await BackendAvailabilityCache.isBackendAvailable(), hasRelevant: (await RagRepositoryV2.searchChunks(...)).isNotEmpty, localContext: chunks, question: ...)` — then `OfflineTutorService` via `HybridInference` fallback is the **only** LLM path.
3. **Fix GGUF:** `linux_llm_config_service:177` read 4 bytes `== 'GGUF'`, hard paths → `getApplicationSupportDirectory()/models` only, `Isolate.run(() => File.copy)` + progress + space check, stale `offline_tutor_app` import → `Mentora` `package:mentora/...` (rename `pubspec` `offline_tutor_app`→`mentora`).
4. **Replace stub STT:** `offline_speech_service:33` remove hard string, use `speech_to_text` (online) / `vosk`/`whisper.cpp` (offline via `MethodChannel`) or return error "Voice not available offline — type question" (honest vs fake).
5. **Budget:** `PromptBudgetManager.maxPromptChars 1500→~4000` (GGUF 2048-4096), thread `linuxConfig.maxTokens`.

*Verification:* kill `gateway:8000` → `ChapterChatScreen` shows `offline` banner with real `RagRepositoryV2` chunk count, streams from local GGUF; `adb logcat | grep LlamaEngine` shows model path not `/home/akash/...`.*

---

*Evidence: `hybrid_inference:191-349` dead for `chapter_chat:69`, `network_state:46` pre-refresh offline, `connectivity:80` google.com, `platform_inference_adapter:2` stale import, `linux_llm_config:48` dev path.*
