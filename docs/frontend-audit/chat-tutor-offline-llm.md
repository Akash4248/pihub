# Chat / Tutor / RAG Wiring + Offline LLM Fallback

**Scope:** `Mentora/lib/features/chat/*`, `tutor/*`, `network/application/hybrid_inference_service.dart`, `network/data/backend_api_service.dart`, `rag/*`, `voice/*`  
**Date:** 2026-09-12 — `flutter analyze` 799, `diff 83` vs `offline_tutor_app`

---

## 1. Chat Flow — `online → RAG → LLM → fallback` is Bypassed

`ChapterChatScreen:69,99-101`

```dart
final gateway = ref.read(tutorInferenceGatewayProvider);
await for (final token in gateway.streamResponse(prompt: contextualPrompt))
```

* **Bypasses** `HybridInferenceService:191-557` (`backendRag → curriculumRag → knowledgeFallback`, `intentDetector`, `assetResolver`, `confidenceEvaluator`, RAG chunk counts) — that entire `246-262` offline detection + `391-430` budget logic is **dead for visible chat**.
* Builds `ConversationMemoryHarness:83-89` prompt directly; `ConversationMemoryHarness:68` calls `RagRepository.localRagPreCheck(chapterId:'')` with **empty `chapterId`** → FTS `WHERE fts.chapter_id=? ''` → 0 chunks → semantic recall always empty.
* Parallel RAG divergence: `RagRepository:41` seed `rag_chunks:chap_linear_eq` (one-table FTS only) vs `HybridRetrieval:133` `rag_chunks_v2` BM25+TF-IDF — chat uses the former, production hybrid uses latter; token budgets `PromptBudgetManager:1500` vs `HybridInference:420` unenforced.

`catch (e) { persistCompletedAssistantMessage('I faced an issue: $e') } :111-113` persists exceptions as normal assistant bubble — no `state.error`, no retry, `isGenerating=false` only in `finally:115`. `HybridInference:167` bare `catch (_)` discards stack.

---

## 2. Offline LLM Fallback — Detection Racy + Stub STT + GGUF Hard Paths

### 2.1 Offline detection multi-sourced, contradictory

| File:Line | Bug |
|---|---|
| `network/services/network_state_service:46-50` | Returns `offline` when `_lastSnapshot==null` — before first `_refresh():69-78` every consumer sees offline even on Wi-Fi (cache-only path chosen). Should return `unknown` + shimmer. |
| `services/connectivity_service:80-88` | `InternetAddress.lookup('google.com:443')` — blocked on classroom LAN / Pi AP (no WAN) → reports offline when Pi `RuntimeBackendUrl` is reachable (probe `Socket.connect(uri.host,uri.port):101` says online). Two signals diverge. |
| `network/data/backend_api_service:55-61` `BackendAvailabilityCache` | No observable TTL — stale `true` after Pi shutdown keeps trying backend minutes. |
| `chapter_chat_screen:166-171` | Hardcodes `isOfflineMode:true`, `activeModel:'On-Device GGUF Llama-3'` — online branch untestable. |

### 2.2 Local LLM loading — two engines, stale `isReady`

`HybridInference:247-253` empty `catch(_){}` on `ensureModelLoaded()` swallows failure; `247` vs `PlatformTutorInferenceGateway:15-42` direct `LinuxTutorInferenceGateway` on `Platform.isLinux` without `ensureModelLoaded()` — cold `File.exists` check only, not loaded-in-memory. `platform_inference_adapter:47` stale import `package:offline_tutor_app/...` + optimistic `_ready=true` on `MissingPluginException:64-68` masks missing exe.

### 2.3 GGUF discovery — dev-machine absolute paths

`chat/data/local/linux_llm_config_service:47-51` defaults `executablePath:'/home/akash/Desktop/IDP/llama.cpp/build/bin/llama-cli'` (dev), `autoDetectModelPath:118-184` scans 9 hardcoded `~/.../PIHUB`, `~/.cache/lm-studio`, `~/.ollama` then first `.gguf` `length>0` — no GGUF magic `GGUF` header check, no quant/mtime sanity. `copyModelToAppStorage:189-201` blocking `File.copy` of multi-GB on UI isolate, no progress/space check, Linux `Stream.value(100)` hides.

### 2.4 Linux process — merge/stream bugs

`linux_tutor_inference_gateway:66-124` `Process.start` no timeout/`ulimit`; `stdout` filtered via `_isLlamaLogChunk` but `stderr` never yielded into `merged` (`process.stderr` only to `stderrBuffer`) — on builds that emit tokens to `stderr` UI hangs "Thinking…". `stdoutDone && stderrDone && !closed → close` (83-87) can close prematurely if `stdout` ends before process exit; `stopGeneration:293` `SIGINT`→600ms→`SIGKILL` drops pending chunks; `_detectCapabilities:163` `Process.run('--help')` per cold start, no negative-result cache TTL; `--temp 0.3 --top-k 30` `208` ignores `GenerationConfig`.

---

## 3. Duplicate Chat States / Models

| A | `TutorMessage{text,isUser,timestamp}` `chat/domain/tutor_message:1` | `ConversationMessage{id,role,text,language,timestamp}` `tutor/models/conversation_message:16` — histories can't unify |
| B | `ChatViewState{messages,isGenerating,chatMode,EngineStatus}` `chat/domain/chat_view_state:6` | `ConversationProviderState` + `VoiceProviderState` — 3 `StateNotifier`s for one chat, `isGenerating` vs `thinking/generatingAudio/speaking` diverge |
| C | `ConversationMemoryService:16` vs `ConversationContextBuilder:22` vs `ConversationMemoryHarness:7` — 3 memory builders, 3 token budgets, 3 storage (`SharedPreferences` vs mem); chat uses `Harness` only |

No canonical `ChatMessage`/`ChatState`; adding a feature edits 4 providers.

---

## 4. Silent Fallbacks + Hardcoded Prompts + Races

| ID | File:Line | Bug |
|---|---|---|
|  | `voice/services/offline_speech_service:21-36` `transcribeAudioFile` | Stub: `size<100→''` else hard `"Explain chapter concept..."` — user Kannada/Hindi discarded. |
|  | `rag_repository:332,443` `catch(_)` swallow FTS errors → `-1.0` score silently. | Fix: surface FTS missing. |
|  | `backend_api_service:201` `catch(_){ continue }` skips malformed `data:` JSON → truncated answer. | Log. |
|  | `ConversationMemoryHarness:29-34` grade `9` hard-coded, fictitious `search_chapter_knowledge` tool never exists. | Param grade, delete tool. |
|  | `HybridInference:390-415` vs `TutorPromptBuilder:26` vs `SimpleAiChatComponent:104` 3 socratic prompts compete. | One builder. |
|  | `ChatController:102-115` `appendMessage` fire-and-forget (no `await`), `updateLastMessage:117` updates UI not DB — crash mid-stream loses answer, `persistCompletedAssistantMessage:129` only DB write. | `await` + transactional streaming. |
|  | `ChatController:143` `Provider.family` `sessionId='session_${course.id}_${subject.id}_${chapter.id}'` reconstruct each build — double `initializeSession` race, welcome message overwrites fetched history. | Guard `isBootstrapping`. |
|  | `_scrollToBottom:120` per-token `animateTo` without cancel → unbounded animation queue jank. | Debounce. |

---

## 5. UI/UX Chat

* `chat_input_bar:34-44` mic pushes `VoiceTutorScreen` even when `isGenerating==true` (text disabled but mic not) → voice + LLM pipelines overlap; `onSubmitted:53` fires `onSend` even when generating → duplicate question.
* `chat_message_list:54-72` double spinner (bubble `Thinking...` place-holder filtered at `ChatSessionRepository:136` on read but still in `state.messages`) + no `state.error` render, `Container maxWidth 0.82` fixed not tablet-responsive.
* `chat_memory_policy_sheet:58-73` exposes only `shortTermWindow 4/8/12/16` + `semanticRecallEnabled`, hides `semanticTopK/resetPolicy/inactivityMinutes` from `chat_memory_policy_repository:13`.
* `ChapterChatScreen:168` subtitle `'${course.name} NCERT • Offline Socratic Tutor'` always offline, green dot `74-81` always connected.

---

**Priority:** Wire chat to `HybridInferenceService` single path (pass real `chapter.id` not `''`), fix offline `unknown` vs `offline` startup, replace stub STT with `speech_to_text`/`whisper.cpp`, `await` DB writes + `mounted` guards, dedupe `TutorMessage`/`ConversationMessage` to one, surface `backendConnected/engineLoaded` + `state.error` in UI.

*Evidence: `hybrid_inference_service:191` dead for `chapter_chat_screen:69`, `offline_speech:33` stub, `linux_llm_config:48` dev path.*
