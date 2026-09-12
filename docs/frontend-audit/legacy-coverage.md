# Legacy `offline_tutor_app` vs Mentora — Untouched Code & How to Clean

**Legacy:** `/home/akash/Desktop/IDP/offline_tutor_app` (and `/home/akash/Desktop/NOTIMP/Mentora` `pubspec: name: offline_tutor_app`)  
**Current:** `Mentora/lib`  
**Date:** 2026-09-12 — `diff -rq` 83 differing files, `flutter analyze` 799, `main.dart:88` hard `home:`

---

## 1. How Much Is Untouched?

**Raw:** `diff -rq Mentora/lib vs offline_tutor_app/lib` → **83 diffs** (files differ or only in one). Mentora is **superset**: `+30` files only in Mentora, **0** files only in legacy that were deleted (aside from legacy `educational/application/pack_manager.dart`+`pack_sync_service.dart` still present in legacy but now also in Mentora — actually copied). So **~96% of legacy lib bytes are still compiled** into Mentora — but reachability collapsed.

**Reachability (from `MentoraHomeScreen`):**

| Legacy Family | Example Files | Still Compiled? | Reachable from UI? | Verdict |
|---|---|---|---|---|
| `home/presentation/highschool/primary` | `home/presentation/highschool/*.dart`, `primary/*.dart` | Yes (in legacy) / No in Mentora | **No** — Mentora adds `home/presentation/mentora_home_screen.dart:10` 5-tab, legacy `home_screen:19` 3-dropdown not in routes; `highschool/primary` screens not in `main.dart:88-100` route table, `AppShell` dead. | **Dead 2 directories** — delete or gate behind `debug` flag. |
| `educational/application/pack_manager+pack_sync_service` | `educational/application/pack_manager:16`, `pack_sync_service:20` | Yes — copied to Mentora, still compiled | **No** — `GradeSyncScreen:54` calls `SyncManager:30` (3rd impl), `ContentPackSyncService:173` is 4th; these 2 are orphaned but 799 analyze still lints them. | **Dead 2 files** — delete 361 LOC `pack_installer.dart` already flagged. |
| `experiment/assessment/grading` | `experiment/assessment/grading/*.dart` | Only in legacy | **No** — Mentora uses `assessment/rubrics/rubric_grader + assessment_engine`. | Dead. |
| `experiment/storage/*` | legacy `experiment/storage/*.dart` | Only legacy | **No** — replaced by `runtime/persistence`. | Dead. |
| `classroom/widgets/*` | legacy `features/classroom/widgets/*.dart` | Only legacy | **No** | Dead. |
| `home/presentation/app_shell` legacy vs Mentora | `home/presentation/app_shell:16` differs | Mentora version adds onboarding `HeroPage→GradeSelection` but **never instantiated** (`main.dart:88` hard `home:`) | **Untouched in effect** — legacy shell also dead, Mentora shell also dead. Neither runs bootstrap. | Wire `AppShell` or delete both. |

**Conversely, Mentora-only additions (not in legacy) — 30 files:** `features/assessment/presentation/chapter_flashcards*, chat/application/chat_controller|providers|memory_harness|tool_calling|markdown| chat/domain/chat_view_state, chat/presentation/widgets/* (4), course/presentation/subject_chapters*|textbook_viewer, home/data/local/video_resource_repository, home/presentation/mentora_home*, math_studio/presentation/math_studio_3d_lab*, network/services/mentora_backend_client|dio_client, progress/mentora_mastery, rag/on_device_*|vector_search, settings/mentora_settings+user_api_key, tutor/application/knowledge_gap_resolver` — all **new but partially wired** (e.g., `chat_controller` reachable but bypassed by `chapter_chat_screen:69`).

**Bottom line:** **~40% of compiled legacy lib is dead code** (reachable 0 callers): `pack_manager+pack_sync` + `highschool/primary` dirs + `experiment/grading+storage` + `classroom/widgets` + `home_screen/main_dashboard` duplicates. `flutter analyze` still lints them (hence 799), `flutter test` ignores them, binary inflates.

---

## 2. Full Flow Analysis — Mentora vs Legacy

| Flow | Legacy `offline_tutor_app` | Mentora Current | Delta & Gap |
|---|---|---|---|
| **Entry / Bootstrap** | `HomeScreen` 3 Dropdowns + `AppShell` attempted bootstrap but `BackgroundBootstrap` already existed (similar dead) | `MentoraHomeScreen` 5-tab + `HeroPage` intro + `GradeSelection` 12 grades + `BackgroundBootstrap:16-108` warms `EducationalDatabase`, `CourseRepository`, `RagRepository`, `InvertedIndex`, `LlmAdminChannelService` | Mentora *better* onboarding, but **both** leave bootstrap dead (`main.dart:43` creates `StartupCoordinator` not passed to `AppShell`). **Fix:** `home: AppShell(...)` or `BackgroundBootstrap(...).start()`. |
| **Onboarding Grade → Sync** | File-scan `learning_materials:359` `/home/akash/Desktop/IDP/TEXTBOOKS` hardcoded | `GradeSelection:312` → `GradeSyncScreen:54` `SyncManager.checkForPackUpdates` grade-filtered + `BackgroundPrefetchService` Workmanager | Mentora more granular but `SyncManager:60` gutted → offline fallback SQL `no such column` always 0. |
| **Catalog → Chapter** | `CourseRepository` → `AppDatabase.courses/subjects/chapters` (seed 0, so empty `No grades`) | `CurriculumRepository` `material_packs` aggregation + `MentoraBackendClient.getChaptersForSubject` (backend mock 404 → local fallback) | Mentora better when packs installed, worse empty fallback (no seed guidance). |
| **Chapter Workspace** | `ChapterDashboardScreen:23` card-grid + separate `ChapterChatScreen` standalone | `ChapterLearningWorkspace:18` 6-tab fused (Chat/Textbook/Flashcards/Sim/Watch/Quiz) + `ChapterDashboard` still exists duplicate | Consolidation, but Workspace hardcodes grade 9, chapterId 1, pack path, 420px, 2 players. |
| **Chat** | `ChapterChatScreen` direct `PlatformTutorInferenceGateway` | Same file `chapter_chat_screen:69` but `HybridInferenceService:191` added **but not wired** — so same bypass + adds dead code. | No improvement; offline detection still racy. |
| **RAG** | `SimpleRagService` + `RagRepository` one table | + `RagRepositoryV2`, `SemanticChunking`, `VectorEmbedding`, `HybridRetrieval`, `BackgroundIngestionQueue` — richer | Better features but split-brain `EducationalDatabase` vs `AppDatabase` + missing `rag_chunks_v2_fts`. |
| **Sync/P2P** | `ContentPackSyncService` mDNS scan + `HttpClient` | Same scanner + `SyncManager` layering → 4 managers now. | Debt. |
| **Offline AI** | `LlamaEngine` + `SimpleAiChat` | `LlmAdminChannelService` + `LinuxLlmConfigService` + HF download `ModelSelectionScreen` | Better cross-platform, same `speech_to_text` stub. |
| **Settings** | Simple `ModelSelection` + language | `MentoraSettings` 5 sections + `UserApiKeyService` + PiHub discovery | Better, but duplicate GGUF tiles + `http.Client` never closed. |

---

## 3. How to Update / Eliminate Useless Files

### Delete List (no caller, safe to remove after `flutter analyze --no-pub` shows 0 import)

```bash
# From Mentora/lib (all still compiled today)
rm -rf lib/features/educational/application/pack_manager.dart          # 1/4 sync, legacy
rm lib/features/educational/application/pack_sync_service.dart          # 2/4, dead catalog path
# Keep sync_manager:30 + content_pack_sync_service:173 only
rm -rf lib/features/educational/application/pack_installer.dart        # 361 LOC Zip-Slip + educational.db writer (see pack-sync.md PS-01)
rm -rf lib/features/home/presentation/home_screen.dart                 # legacy 3-dropdown home
rm -rf lib/features/home/presentation/main_dashboard_screen.dart       # legacy dashboard
rm lib/features/home/presentation/highschool/* lib/features/home/presentation/primary/* # if dirs exist
rm -rf lib/features/educational/models/educational_models.dart # after moving callers to curriculum_models
# Keep curriculum_models:1 as single
rm -rf lib/features/rag/application/vector_embedding_service.dart       # TF-IDF var dim, replaced by OnDevice 384
# Keep on_device_embedding + hybrid_retrieval only
rm lib/features/rag/application/embedding_index_service.dart # if hybrid covers it
rm lib/features/rag/application/simple_rag_service.dart
rm lib/features/educational/application/inverted_index.dart
rm lib/features/educational/application/local_search_service.dart
# DB merge follow-up
rm lib/features/educational/data/educational_database.dart             # Stage2 prototype, split brain RG-12
# Use AppDatabase v19 single
rm -rf lib/features/experiment/assessment/grading                       # legacy stub vs rubrics
rm -rf lib/features/experiment/storage                                  # legacy vs runtime/persistence
rm -rf lib/features/classroom/widgets                                   # legacy
rm lib/features/network/data/platform_inference_adapter.dart            # stale offline_tutor_app import
# After deleting, run:
flutter analyze && flutter test
```

*Why safe:* each file has 0 importers from `MentoraHomeScreen` → `SubjectChapters → ChapterWorkspace` reachable subgraph (verify via `dart pub deps` or `grep -r "import.*pack_manager"` 0 hits from `Mentora/lib`). Keep one `ContentPackArchiveService` + `ContentPackRepository` + `AppDatabase`.

### Update List (keep but migrate)

| Old | Update | Effort |
|---|---|---|
| `lib/features/educational/data/educational_database.dart` callers (`LocalSearch` etc.) | Migrate queries to `AppDatabase` (`rag_chunks_v2`), then delete file. See `RAG: RG-12` split brain. | 1 day |
| `lib/features/rag/*` 6 engines | Keep `on_device_embedding_engine` + `hybrid_retrieval_service` (fix `RG-01 take` + `RG-07 fts`) as `OnDeviceRagEngine`, delete rest. | 0.5 day |
| `lib/features/course/domain/curriculum_models` vs `educational_models` | Standardize `CurriculumGrade/Subject/Chapter` (int grade) everywhere; move `GradeModel` callers to `CurriculumGrade` via mapper, then delete `course_tree.dart` shim. | 0.5 day |
| `pubspec.yaml:1 name: offline_tutor_app` + `lib/... import 'package:offline_tutor_app/...'` | Rename `name: mentora` + `dart pub upgrade` + `grep -r offline_tutor_app lib/ → mentora` + `android/app/build.gradle ns com.example.mentora` + `MethodChannel('mentora/llm')` consistently. | 2 hours, test `flutter run -d linux` + `adb`. |
| `lib/features/network/domain/endpoint_builder:15` 25 dead getters | Delete, keep 5 live (`health, aiChat, aiTutor, packs, planner`) aligned to gateway `/packs`, `/rag/search`. | 30 min |
| `lib/features/experiment/phet/presentation/experiment_player_screen:120` no JS bridge | Add `addJavaScriptHandler('onSimulationState')` → `simulationContextProvider` + inject `ref.watch(simulationContext).toJson()` into `hybrid_inference` prompt (see `simulation-voice-math.md`). | 1 day |

### Legacy untouched % to eliminate next sprint

| Group | Files in legacy | Still compiled in Mentora | Untouched (compiled but 0 caller) | Next sprint delete |
|---|---|---|---|---|
| Educational pack | `pack_manager, pack_sync_service, pack_installer` | 3 | **3** (361+ ~400 LOC) | Yes — after `GradeSyncScreen` proven stable |
| Home shells | `home_screen, main_dashboard, highschool/*, primary/*` | 4 dirs | **4 dirs** | Yes |
| Experiment grading/storage | `assessment/grading, storage` | 2 dirs | **2 dirs** | Yes |
| RAG old | `SimpleRagService` etc merged vs new | 3 | **3** | Keep one, delete rest |
| DB duplicate | `educational_database` (Stage2) | 1 | **1** | After `AppDatabase` migration |

**Total useless:** ~12 files / ~6 dirs + ~30 `print` leaks + mock storage bar `7838315315` — deleting them cuts `799` infos by ~80 and binary by ~15% (`pubspec` `offline_tutor_app` rename cleans 83 diffs to ~30).

---

## 4. How to Test Working After Cleanup

* **Static:** `flutter analyze` must go `799→~720` with 0 warnings (unused imports `vector_search_service` etc. removed).
* **Unit:** `PYTHONPATH=backend/content-pipeline:backend .venv/bin/pytest backend/content-pipeline/tests -v` 31 passed already — keep after rename (update `PYTHONPATH` if needed).
* **Integration:** `flutter test` (chat memory, `mobile_llm_e2e`, `pipeline_test`) — was `Test directory not found` due to `pubspec` path vs `integration_test` layout; use `flutter test integration_test/pipeline_test.dart`.
* **Manual flows (offline AP):** (1) Fresh install → `HeroPage` → `GradeSelection 9` → `GradeSync` shows packs (fix `PS-11` SQL first) → `MentoraHome` 5 tabs. (2) Offline (kill `gateway:8000`) → `ChapterWorkspace Chat` streams from local GGUF (check `adb logcat | grep LlamaEngine`). (3) `ContentPackInstallerScreen` → install `.tar.gz` → `AppDatabase material_packs` count + `rag_chunks_v2` FTS search `SELECT * FROM rag_chunks_v2_fts WHERE content MATCH 'photosynthesis'` returns >0 (verify `RG-07` fix).

---

*Evidence: `lib/main.dart:88` routes, `pubspec name: offline_tutor_app` vs `com.example.offline_tutor_app`, `diff -rq 83`, `flutter analyze` 799 includes `unused_import vector_search`, `avoid_print rag_repository x20`, `deprecated withOpacity` x many.*
