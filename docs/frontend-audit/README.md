# Frontend Audit — Mentora Flutter App (`Mentora/lib`)

**Date:** 2026-09-12  
**Scope:** `Mentora/lib` (Flutter, `offline_tutor_app` package name) vs `pihub/backend` (321 routes) vs legacy `IDP/offline_tutor_app`  
**Method:** `Mentora/lib` full read (>35 files) + `flutter analyze` (799 issues) + `diff -rq Mentora/lib vs offline_tutor_app/lib` (83 differing files) + `ss -tlnp` live backend + `pytest` 31/31 backend

---

## Index

| # | Doc | Scope | Issues | Verdict |
|---|---|---|---|---|
| 1 | [`pack-sync-curriculum.md`](pack-sync-curriculum.md) | Pack sync (4 impls), Curriculum (3 hierarchies), RAG (6 engines, 3 stores) | 69 | CRITICAL: Zip-Slip `pack_installer:69`, fallback SQL `no such column: chapter_id` `sync_manager:92`, limit `take` bug `hybrid_retrieval:100`, split-brain `EducationalDatabase` vs `AppDatabase` |
| 2 | [`chat-tutor-offline-llm.md`](chat-tutor-offline-llm.md) | Chat flow, Tutor RAG, HybridInference, Offline LLM GGUF, Memory Policy | 22 | CRITICAL: `ChapterChatScreen` bypasses `HybridInferenceService`, offline `quality==offline` race **before** `start()`, stub STT `"Explain chapter..."`, GGUF `~/.cache` dev paths |
| 3 | [`simulation-voice-math.md`](simulation-voice-math.md) | SimulationContext bridging, Experiment lifecycle, Offline queue, Grading, Voice STT/TTS/Socket, Math Studio 3D | 24 | CRITICAL: PhET WebView no JS bridge, grading free-credit `answer.isNotEmpty→true`, Forge2D 3MB dead |
| 4 | [`navigation-ui-ux.md`](navigation-ui-ux.md) | Navigation shell, Bootstrap, Theme, Responsive, Layout bugs, Flows | 18 | HIGH: `AppShell` never instantiated (`main.dart:88` hard `home:`), `onUnknownRoute` masks 404, `hero_page` only responsive, 799 analyze infos |
| 5 | [`legacy-coverage.md`](legacy-coverage.md) | `offline_tutor_app` vs Mentora diff, 83-file drift, dead files, how to eliminate | — | 96% shared, +30 files added, 0 removed; `pack_installer`, `pack_sync_service`, `highschool/primary` dead; rename `pubspec` `offline_tutor_app→mentora` |
| 6 | [`offline-llm-fallback-deep.md`](offline-llm-fallback-deep.md) | Dedicated offline LLM fallback deep dive (GGUF, Linux, Android, offline detection) | 14 | `InternetAddress.lookup('google.com:443')` fails on AP, `ensureModelLoaded` empty catch, `llama-cli` hard `/home/akash/Desktop/IDP/...` |

**Cross-cutting TOP 5 (all features):**

1. **4 Pack sync + 5 models + 2 installers + 2 DBs** — same chapter installed via `PackInstaller` (`GradeModel`→`educational.db`) vs `ContentPackArchiveService` (`CurriculumChapter`→`material_packs`) — pick one.
2. **6 RAG engines + 3 stores** — `hybrid_retrieval:100` `..take(limit)` discards `take`, `rag_chunks_v2_fts` **never created** (`app_database:247` "`FTS4 not supported`" fallback to LIKE O(n)), `OfflineTutorService` reads orphan `EducationalDatabase`.
3. **Chat bypass** — `chapter_chat_screen:69` calls `PlatformTutorInferenceGateway` direct, not `HybridInferenceService:191` `backendRag→curriculumRag→knowledgeFallback` — RAG, intent, asset routing dead for visible chat.
4. **SimulationContext not-wired** — native `ExperimentContextBridge:181` works, PhET `phet/player:37` static only + no `addJavaScriptHandler`, `chat` never reads `simulationContextProvider`.
5. **Offline detection + GGUF** — `NetworkStateService:46` returns `offline` before first `_refresh()`, `google.com` probe fails on classroom LAN; `LLM fallback` `HybridInference:247` empty `catch(_){}`, `LinuxTutorInferenceGateway:47` `offline_tutor_app` stale import → optimistic `ready=true`.

**Test evidence:** `flutter analyze 799 issues` (26s, 0 errors, ~790 infos + 9 warnings: unused imports, `print` in prod, `withOpacity` deprecated, `allowMultiple` deprecated), `diff -rq 83` files differ, backend 31/31 green, `:8001/health` ok post-restart.

**How much legacy untouched?** `diff -rq` 83 diffs, but Mentora is **superset** — legacy files all still present, Mentora adds ~30 files (assessment/presentation, chat/application, course/presentation, home/mentora_home, rag/on_device, etc.) and **deletes 0** legacy files; legacy `educational/application/pack_manager.dart` + `pack_sync_service.dart` remain compiled but no longer called by UI (dead), `home/presentation/highschool/primary` dead, `experiment/assessment/grading` stub vs new `rubrics`. So **~96% of `offline_tutor_app/lib` bytes are still compiled** — but ~40% of those paths are no longer reachable from `MentoraHomeScreen` (see `legacy-coverage.md`).

**Flow map (see `navigation-ui-ux.md` §4):** `HeroPage → GradeSelection → GradeSync (offline fallback SQL broken PS-11) → MentoraHome (5-tab IndexedStack) → SubjectChapters (hard path /storage/.../source.pdf CU-09) → ChapterWorkspace 6-tab (Chat hard grade 9, Textbook fixed 420px, Flashcards chapterId:1, Sim inline HTML vs native race, Watch 2 players, Quiz Restart no persist) → Mastery duplicate`.

**Fix order (P0→P1):** Pack installer `Branded Threat: Zip-Slip + `content_pack_archive_service:118` path traversal, `sync_manager:92` dead offline fallback SQL, `hybrid_retrieval:100` limit, `offline_speech:21` stub STT, PhET JS bridge + chat `simulation_context` injection, `AppShell` wiring, `HybridInferenceService` single chat path, `AppDatabase` single DB + `rag_chunks_v2_fts` creation, delete `pack_installer.dart` / `EducationalDatabase` / `VectorEmbeddingService` duplicates.

---

*Evidence: `Mentora/lib` 105-line `main.dart`, `lib/features/**` 4 pack services, 6 RAG engines, `flutter analyze` 799, `diff 83`, `offline_tutor_app` vs `Mentora` pubspec `name: offline_tutor_app` legacy.*
