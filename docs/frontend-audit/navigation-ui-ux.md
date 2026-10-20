# Navigation / Bootstrap / UI/UX Fixes

**Scope:** `Mentora/lib/main.dart` (105), `features/home/*`, `bootstrap/*`, `core/theme/*`, `features/course/*`, `features/settings/*`  
**Date:** 2026-09-12 — `flutter analyze` 799 issues, `diff 83` vs legacy

---

## 1. Navigation — Dead Shell + 4 Homes + 2 Chapter Workspaces

| File:Line | Finding | Fix |
|---|---|---|
| `lib/main.dart:88-100` `home: MentoraHomeScreen()`, `onUnknownRoute` returns home | `AppShell:16-107` intended shell (resolves `app_intro_seen_v1` + `selected_grade`, starts `BackgroundBootstrap`/`OptionalBootstrap` in `initState:42`) **never instantiated** — `main.dart:43` creates `StartupCoordinator` but never passes to `AppShell`. Onboarding bypassed if grade cleared. `onUnknownRoute` masks 404. | Either `home: AppShell(...)` or call `BackgroundBootstrap(startupCoordinator,courseRepository).start()` + `OptionalBootstrap(...).start()` in `main:32-46` after `BehaviorRegistry`. Make `onUnknownRoute` log + 404. |
| `home/presentation/home_screen:19` legacy Home (3 Dropdowns) | Compiled but not routed, from `offline_tutor_app`. | Delete or route only for diagnostics. |
| `home/presentation/main_dashboard_screen:46` legacy Dashboard `NavigationBar` (Home/Tools/Settings), glassmorphism | Not routed, `SELECT COUNT(*) FROM material_packs` diagnostics. | Delete. |
| `home/presentation/mentora_home_screen:10` current 5-tab `IndexedStack` (Home/Practice/Mastery/Studio/Settings) | **Current** `home` — but home tab duplicates `MyLearningScreen:18` `MyLearningScreen` "Continue Learning" concepts. | Unify `mentora_home` home tab + `my_learning_screen` logic. |
| `course/presentation/subject_chapters_screen:7` new grade+subject→chapter via `MentoraBackendClient` | Registered `/subject_chapters` 6 pills `Textbook/Cards/Chat/Sims/Watch/Quiz`. Pills `Chat/Sims/Watch/Quiz:259-265` dead (no `InkWell`), whole card only goes Tab 0. | Wrap pills in `InkWell(initialTabIndex)`. |
| `home/presentation/subject_screen:16` legacy accordion `CurriculumSubject`→`CurriculumRepository` | Not in routes, pushed `MyLearningScreen:442`. | Choose one stack; delete legacy. |
| `tutor/presentation/chapter_learning_workspace_screen:18` 6-tab (`TabController 6`, `isScrollable`) Tab 1 Chat /2 Textbook /3 Flashcards /4 Simulation /5 Watch /6 Quiz | Registered `/chapter_workspace` — Tab pills duplicate dashboard. TabBarView nested `ListView` + `InAppWebView` → `didChangeDependencies:114` `setState` unbounded height on fast switch. | Keep workspace, delete `chapter_dashboard_screen:23` grid or make it the single entry. |
| `home/presentation/chapter_dashboard_screen:23` card-grid dashboard for same chapter + YouTube inline | Pushed `SubjectScreen:248` — 2 entry points to same chapter don't link. | One entry. |
| `textbook_viewer_screen:9` + `pdf_chapter_reader` + `chapter_reader` + `pdf_viewer` | 4 PDF viewers, hardcoded `/storage/.../packs/${ch['title']}/source.pdf` → iOS/desktop 404. | Keep `TextbookViewerScreen`, use `ContentPackRepository.absolutePath`. |
| `video_player_screen:12` + workspace Tab5 `762` + `chapter_dashboard:471` | 3 players, `YoutubePlayerController` recreated without guard → leak. | Single `VideoPlayerHelper.extractYouTubeId`. |
| `quiz_assessment_screen` + `quiz_player_screen` + `mentora_practice_screen:5` + workspace Tab6 | 4 quiz UIs | One `QuizPlayer`. |

---

## 2. Theme + Responsiveness

* `flutter_build_responsive_layout` 0 hits — not used.
* `IDPTheme.lightTheme` seed `0xFF3F51B5` but 70% screens hardcode `0xFF4F46E5` (`mentora_home:106`, `mentora_mastery:95`, `subject_chapters:65`, etc.) vs `main_dashboard:57` `0xFF0B6E4F` — 3 primaries. No `darkTheme`.
* Responsive only in `hero_page:132-178` + `my_learning_screen:536-693` (`LayoutBuilder >600`), `learning_materials:1072` (`<780` bottom sheet vs two-pane). Else fixed: `mentora_home:333` `crossAxisCount:2`, `chapter_flashcards:247` `320×380`, `grade_selection:67` `maxWidth:480` correct but most not. Fix: `LayoutBuilder` `SliverGridDelegateWithMaxCrossAxisExtent(280)`, `ConstrainedBox(600-900)`.

---

## 3. Layout Bugs

| File:Line | Bug | Fix |
|---|---|---|
| `subject_chapters_screen:219-269` `Row→Expanded(SingleChildScrollView horizontal reverse:true)` | Pills truncate on SE, `reverse:true` a11y order, deprecated `withOpacity`. | Remove `reverse`, use `ListView` horizontal. |
| `chapter_learning_workspace:360-363` `TabBarView` + `didChangeDependencies:114-149` `setState` before layout → unbounded height flash | Defer `setState` to post-frame. |
| `chapter_learning_workspace:762-782` Youtube controller per build, no `WidgetsBinding` guard | Cache, `dispose` old before create. |
| `chapter_flashcards:141-185` `320×380` card overflows 320px width, `rotationY` no perspective | `MediaQuery` responsive, `perspective` 0.002. |
| `_isLoading` `subject_chapters:123` only set false on success → spinner forever on `null` data | Set false in `finally`. |
| `chapter_learning_workspace:497` `chapterId:1` hardcoded | Real `packId`. |
| `view All` `mentora_home:318` + `my_learning:328` `Resume Lesson` `onPressed:{}` no-ops | Wire or remove. |
| `mentora_practice:29` `getQuizForChapter('Motion')` hard `Motion` | `_selectedSubject` param. |

---

## 4. Flows

### Onboarding: `HeroPage → GradeSelection (AnimatedContainer check_circle:67)` → `GradeSyncScreen:54-93` `checkForPackUpdates(grade)` → stats → `processPackUpdates([pack])` `while(syncQueue.isProcessing) delay 500ms` blocks UI, no cancel, `queuedCount==0` silent skip → `MentoraHomeScreen`. Grade change via `mentora_settings:226` writes `SharedPreferences` without re-sync → stale packs.

### Catalog → Workspace: `mentora_home:287` `pushNamed('/subject_chapters')` → `subject_chapters:149` `pushNamed('/chapter_workspace', {chapterTitle,subject,grade,initialTabIndex})` → 6-tab. Pill affordance dead (see §1).

### Inside Workspace: Tab1 Chat hard grade 9 (`ConversationMemoryHarness:29`), `hasAudio:true` but `Read Aloud:414` `onPressed:{}` no-op; Tab2 Textbook fixed 420px `199-235` `pop`; Tab3 Flashcards `chapterId:1`; Tab4 Sim inline HTML `550-623` slivers vs native race; Tab5 Watch 2 players; Tab6 Quiz `userMastery` static.

### Progress: `progress_dashboard:11` 3 tabs (`CourseRepository+ProgressRepository+QuizResult`) skeleton OK vs `mentora_mastery:7` duplicate circular mastery — both 5 BottomNav, no link to next chapter except `MyLearning:212`.

### Settings: 5 sections. Grade dropdown `209-237` no validation; `Manage Packs 244-260` ok; Hybrid switch `305-314`; Provider `331-393` saves on `onChanged` no debounce; PiHub `472-482` `LinearProgressIndicator` stale if `checkBackendHealth` throws; Offline GGUF duplicate tiles `415` + `611` pushing `ModelSelectionScreen:15` (`.gguf/.bin` `file_picker:113` allows `.bin` + substring `gguf` false positive, `http.Client.send:388` never `close()`, 20min no timeout, no resume/cancel, `openWrite:405` no `finally close`, progress `setState` per chunk jank, iOS `NSFileProtection`).

### Bootstrap (dead): `background_bootstrap:16-108` warms `EducationalDatabase`, `DatabaseAutoRepair`, `CourseRepository.ensureSeedData`, `RagRepository.ensureSeedChunks`, `ContentPackBootstrapService.bootstrapLegacyMediaIntoPacks`, `InvertedIndex` if FTS missing, `LlmAdminChannelService.preloadModel()` Android else ready — all `try/catch` correct but **never started** (`main.dart` never creates `BackgroundBootstrap`). Same `optional_bootstrap:12-62` (`DistributedServiceComposer` + `NetworkResilienceCoordinator`). `startup_coordinator:5-88` `runtimeBanner()` ignored by `MentoraHomeScreen`.

**Fix:** `home: AppShell(..., startupCoordinator, ...)` or in `main:32` `BackgroundBootstrap(startupCoordinator,courseRepository).start(); OptionalBootstrap(startupCoordinator).start();`

---

## 5. UI/UX Fixes (Actionable)

* Wire `AppShell` or bootstrap, remove 404 mask `onUnknownRoute` → log+404.
* Unify 4 homes: keep `MentoraHomeScreen`, delete `home_screen` + `main_dashboard_screen` or keep for diagnostics flag.
* Pill taps: wrap `259-266` in `InkWell(initialTabIndex)`.
* Hard paths: replace `subject_chapters:231` + `workspace:488` absolute `/storage/...` with `ContentPackRepository`.
* Theme: replace hardcoded `0xFF4F46E5` with `IDPColors.primary`/`Theme.primaryColor`, add `darkTheme`.
* Responsive: `LayoutBuilder` grid `maxWidth~/280`, flashcard `MediaQuery` responsive, `ConstrainedBox(480)` wherever needed.
* HF download: `http.Client` `close()`, cancel token, `contentLength` resume check, `try/finally sink.close()`, delete partial on failure.
* Grade change: after `setInt` navigate to `GradeSyncScreen` if packs missing.

---

*Evidence: `lib/main.dart:88-100` routes, `app_shell:16-107` unused, `flutter analyze` 799 includes `deprecated withOpacity`, `allowMultiple`, `avoid_print` in `rag_repository` x15, `mentora_home 5-tab` `IndexedStack`.*
