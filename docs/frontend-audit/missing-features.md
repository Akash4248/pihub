# Missing / Unwired Features (All Reachable vs Dead)

**Scope:** Full `Mentora/lib` 853 Dart files vs `lib/main.dart:88` `MentoraHomeScreen` reachable subgraph (~35 files).  
**Date:** 2026-09-12

---

## 1. Reachable Graph

`main.dart:88` `home: MentoraHomeScreen` (`IndexedStack` 5: Home/Practice/Mastery/Studio/Settings) + routes `/subject_chapters→SubjectChapters`, `/chapter_workspace→ChapterLearningWorkspace`, `/voice_overlay→VoiceDebug`.

Transitive extras: `MathStudioHome:53-419` 10 workspaces, `MentoraSettings:244→ManageContentScreen→GradeSelection`, `SubjectChapters:149→ChapterWorkspace` + `227→TextbookViewer`/`247→ChapterFlashcards` + `ChapterWorkspace:762→VideoPlayer`. **Everything else dead.**

---

## 2. Bootstrap — 80% Dead

| File:Line | Finding |
|---|---|
| `bootstrap/app_shell:16`  `AppShell` | **FULLY DEAD** — `main.dart:88` bypasses, never instantiated. |
| `bootstrap/background_bootstrap:15` `start()` warms `EducationalDatabase`, `DatabaseAutoRepair:43`, `RagRepository.ensureSeedChunks:52`, `InvertedIndex:61`, `LlmAdminChannelService:69`, `ContentPackBootstrapService:99` | Dead — only via `AppShell:52`. Never runs under `MentoraHomeScreen`. |
| `bootstrap/optional_bootstrap:11` `DistributedServiceComposer:41`, `NetworkResilienceCoordinator:49` | Dead — via `AppShell:46`. |
| `bootstrap/startup_coordinator:5` instantiated `main:43` but never read except `AppShell:94` | Dead observer, `runtimeBanner()` never shown. |
| `bootstrap/critical_bootstrap:10` `configureDesktopSqlite` | **PARTIAL** — this one wired `main:32`. |

**Fix:** `home: AppShell(...)` or in `main:47` `BackgroundBootstrap(...).start(); OptionalBootstrap(...).start()` before `runApp`.

---

## 3. Assessment / Progress / Tools — Unwired

| Feature | File | Status |
|---|---|---|
| `assessment/domain/quiz_result:1`, `data/local/quiz_result_repository:7` | Present but `MentoraPracticeScreen:28` `getQuizForChapter('Motion')` hard-coded, never `saveResult` | Dead persist, never feeds `MentoraMastery`. |
| Rubrics `experiment/assessment/rubrics/*` 0 imports from `features/assessment/**` | Isolated to experiment engine, no quiz UI. | Wire `RubricGrader` to `ReportPanel`. |
| `progress/dashboard `progress_dashboard:11` 715 lines (leaderboard, trends) vs `mentora_mastery:7` duplicate | `ProgressDashboard` 3 tabs not reachable (only via dead `HomeScreen`), `MentoraMastery` reachable but progress `recordQuestionAsked:38` never called from chat. | Merge — expose leaderboard via Mastery overflow. |
| `features/tools/` | **Does not exist** — closest `experiment/runtime/tools/*` 5 files dead (only `ToolOverlay` in dead `ExperimentTheatre`). | Make `features/tools` or expose via `ChapterWorkspace` sim tab. |
| `features/experiment` 400 files (`builder`, `investigation`, `phet` catalog) | Only toy inline HTML canvas `workspace:547` live; `ExperimentHub`, `ExperimentPlayer` (real), `BuilderScreen`, `LabReports`, `TemplateGallery` all dead. | `ExperimentHubScreen` needs route; `RubricGrader` never called. |
| `features/home` 12 dead screens (`home_screen`, `main_dashboard`, `my_learning`, `chapter_dashboard`, `learning_materials`, `quiz_*`, `pdf_*`, `subject_screen`, `video_player`) | All only via dead `HomeScreen`/`MainDashboard`. | Delete or route via `MentoraHome` FAB. |
| `features/network` 80 files `application/*` 60+ coordinators (`heartbeat_recovery`, `classroom_presence` etc.) | Only `MentoraBackendClient` used; `BackendDiscoveryService` 55 managers overlap, no single owner. | Keep `BackendApiService` via `RuntimeBackendUrl`, delete rest. |
| `features/p2p` 7 files `P2PScreen` etc. | Dead — not in `MentoraHome` IndexedStack, `P2PChannelService` unreachable. | Expose via Settings or delete. |
| `features/rag` 20 files `RagRepository`, `EmbeddingIndex`, `PdfStructureExtraction` etc. | All dead except seed via dead bootstrap. `DocumentRagIngestionScreen` dead. | Keep `RagRepositoryV2` only. |
| `core/widgets/idp_skeleton_loader:1` | 0 callers | Dead. |
| `features/analytics/offline_learning_report_screen:1` | 0 callers | Dead. |
| `features/translation/*` 5, `tutor/widgets/*` 5, `voice/debug/*` | Dead except chat. | — |
| `features/language/LanguageSelector` | Only via dead `MainDashboard`, not `MentoraSettings` (which lacks picker). | Wire. |

---

## 4. Duplicate Screens — Twins

| Group | Keep | Dead Dupes |
|---|---|---|
| Home hub `mentora_home` vs `home_screen` vs `main_dashboard` vs `my_learning` vs `hero_page` vs `app_shell` | `MentoraHomeScreen` | 5 dead (~2000 lines) |
| Progress `mentora_mastery` vs `progress_dashboard` | `MentoraMastery` | `ProgressDashboard` 715 lines hidden |
| Quiz `mentora_practice` vs `quiz_assessment` vs `quiz_player` + experiment `report_panel` | Dedupe to one adaptive bank | 3 dead |
| Textbook 4 viewers | `textbook_viewer_screen` | 3 dead |
| Subject `course/subject_chapters` vs `home/subject_screen` vs `educational/subject_screen` | Course variant | 2 dupes |

---

## 5. Route Table — Unused

`main.dart:89-97` `routes` hash correct but **no navigation pushes** to `/home`, `/practice`, `/mastery`, `/settings` elsewhere — all via `IndexedStack` or `MaterialPageRoute(builder: MentoraHomeScreen(initialTabIndex:))`. `onUnknownRoute:98` fallback to `MentoraHomeScreen` masks 404s for `/learning_materials`, `/progress_dashboard`, `/content_packs/installer`, `/p2p`, `/experiment/*`.

---

## 6. Fix List

* Wire `AppShell` as `main:88 home: AppShell(...)` or call bootstraps in `main:47`.
* Expose `ContentPackInstallerScreen` via `ManageContentScreen` or `MentoraSettings:244`.
* Expose `ProgressDashboard` via `MentoraMastery` overflow.
* Merge `QuizResultRepository.saveResult` from `MentoraPractice:221` + `ChapterWorkspace:1018`.
* Surface rubrics `rubric_grader:7` into `AssessmentReportScreen`.
* Delete `HomeScreen/MainDashboard/LearningMaterials/Quiz*/Pdf*/Educational/Chat/Classroom/P2P` or route via FAB/unified nav.
* Remove dead `routes` map or make `pushNamed` to `/practice` etc. used.

*Evidence: `lib/main.dart:88` routes, `lib/bootstrap/background_bootstrap:15` dead, `lib/features/p2p/presentation/p2p_screen:55` TabController never used, 853 vs ~35 reachable.*
