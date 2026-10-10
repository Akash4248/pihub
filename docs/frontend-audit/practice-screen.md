# Practice Screen — Deep Audit

**Date:** 2026-09-13  
**Scope:** `Mentora/lib/features/assessment/presentation/mentora_practice_screen.dart:331` + `Mentora/lib/features/home/presentation/mentora_home_screen.dart:440` + `Mentora/lib/features/course/presentation/subject_chapters_screen.dart:309` + `Mentora/lib/features/tutor/presentation/chapter_learning_workspace_screen.dart:1276` + `Mentora/lib/features/network/services/mentora_backend_client.dart:1030` + `Mentora/lib/features/assessment/presentation/chapter_flashcards_screen.dart` + `Mentora/lib/features/assessment/data/local/*` + `Mentora/lib/main.dart:124` + `pihub/backend/gateway/app/main.py:1943-1983`  
**Method:** Full `lib` read + `grep -rn` backend routes (`gateway:8000` `content-pipeline:8001` `pack-service:8030`) + `flutter analyze` slice + live `gateway.log` on `GET /quizzes/{chapter} 404`  
**Verdict:** **HIGH** — `Practice` tab is isolated mock; does not show `active grade -> chapters -> flashcards/quizzes` hierarchy. 1-file fix + backend wire required. Duplicate of `ChapterWorkspace Tab3/Tab6`.

---

## 1. User Spec vs Current

| Expected (user) | Current (`mentora_practice_screen.dart:10-331`) | Match |
|---|---|---|
| `Practice` shows **all chapters in active grade** (grade from `SharedPreferences selected_grade`) | Hard `String _selectedSubject='Physics'` `mentora_practice_screen.dart:19`, `_activeChapterId` switch `Physics->Motion` / `Chemistry->ch_chem_1` etc. `27-39`. No `SharedPreferences.getInt('selected_grade')` read (compare `mentora_home_screen.dart:37-47` does). `grade` never sent to backend. | **No** |
| Grouped by subject, tapping chapter shows **subject context** | 4 `ChoiceChip`s fixed `Physics/Chemistry/Biology/Mathematics` `112-137` horizontal scroll. No subject list from `getSubjectsForGrade()`; no chapter list. | **No** |
| Selecting chapter -> **quizzes + flashcards** for that chapter | Direct single question card `140-184` + options `188-232` + `Check Answer` `236-275` + `Step-by-Step Solution` `277-313`. One chapter at a time, no navigation, no flashcards deck. Calls `MentoraBackendClient.getQuizForChapter(_activeChapterId):49` which is **offline stub** `mentora_backend_client.dart:523-606` (10 templated `What is core principle in $cleanTitle?`); same file `494-517` `getVideoLecturesForChapter` and `475-488` `getSimulationForChapter` also stubs to silence `GET /videos/{chapter} 404` / `GET /api/v1/simulations/{chapter} 404` per `frontend-backend-route-audit.md:44-54`. | **No** — mock, not live `GET /quizzes?chapter=&subject=&grade` + `GET /flashcards?chapter=&subject=&grade` `gateway/app/main.py:1943-1982` |
| Offline-first, respects packs/lessons installed | `getQuizForChapter` no pack check, no `CourseRepository` / `ContentPackRepository` lookup. `ChapterWorkspace` already does `VideoResourceRepository.getVideosForChapter` + `ChatSessionRepository` per-chapter. | **No** |

**Result:** `Practice` is unreachable from chapter hierarchy; `Home -> SubjectChapters -> ChapterWorkspace` already implements correct `grade->subject->chapter->Flashcards( Tab3)/Quiz(Tab6)` but `Practice` duplicates Quiz in isolation.

---

## 2. Current Implementation Detail

### 2.1 File Map

| File:Line | Role |
|---|---|
| `lib/main.dart:19,100` | Registers `'/practice': MentoraPracticeScreen()` but `home: AppShell->MentoraHomeScreen IndexedStack[1]` `mentora_home_screen.dart:111` actually renders it as tab. `routes['/practice']` dead (never `pushNamed`). |
| `features/assessment/presentation/mentora_practice_screen.dart:16-55` | State: `_client MentoraBackendClient`, `_selectedSubject`, `_selectedDifficulty='Medium'` (unused display only `172` `Difficulty: Medium`), `_quizData`, `_isLoading`, `_currentQuestionIndex`, `_selectedOption`, `_showExplanation`. `initState:43` -> `_loadPracticeQuestions:47-54` (`setState isLoading`, `await _client.getQuizForChapter(_activeChapterId)`). |
| `mentora_practice_screen.dart:27-39` | `_activeChapterId` getter - maps 4 chips to single chapter id. `Physics` default `Motion`. Never grade-qualified. |
| `mentora_practice_screen.dart:57-331` | `build`: `Scaffold slateBg F8FAFC`, `AppBar` `Practice & Assessment Studio | NCERT Class 9-12 Adaptive Question Bank` `71-75`, filter chips `112-137`, question card `140-184` (`Question X/Y` badge + difficulty chip), options `188-232` (selected=indigo `EEF2FF`, correct=green `ECFDF5`, wrong=red `FEF2F2`), `Check Answer` `236-275` saves `QuizResultRepository.saveResult:247` + `ProgressRepository.recordQuestionAsked:262`, explanation `277-313`, `Next Question` `295-312` wraps modulo. `_buildFilterChip:321-330` `ChoiceChip selectedColor indigo`. |
| `features/network/services/mentora_backend_client.dart:523-606` | `getQuizForChapter(chapter)` now returns local `Map` 10 Qs (no HTTP). Comment `519-527` explains `GET /quizzes/{chapter} 404` silence. Same for `getSimulationForChapter:475` and `getVideoLecturesForChapter:494`. Live probe if needed is `GET /quizzes?chapter=...` via `EndpointBuilder:134` `GET /quizzes` `gateway:1953`. |
| `features/home/presentation/mentora_home_screen.dart:37-61` | Reference impl for grade-aware fetch: `_loadUserData` reads `selected_grade`, `_fetchBackendSubjects` calls `MentoraBackendClient.getSubjectsForGrade(_selectedGrade):54` which does `_subjectsCache` -> `_loadSubjectsFromLocalDb:301` (`CourseRepository.getSubjects('course_$grade')`) -> `_fetchSubjectsFromNetwork:280` (`GET /packs/catalog` `mentora_backend_client.dart:280-298`). |
| `features/course/presentation/subject_chapters_screen.dart:48-66` | Reference chapter fetch: `_loadChapters` -> `getChaptersForSubject(subject,grade:9):327` (`_loadChaptersFromLocalDb:373` + `_fetchChaptersFromNetwork:350` `GET /packs/catalog` -> `_saveBackendChaptersToLocalDb:405`). Renders `ListView` cards `141-270` with `onTap:154` `pushNamed /chapter_workspace {chapterTitle,subjectName,grade}` + pills `231-258` (now `InkWell`? dead pre-fix). |
| `features/tutor/presentation/chapter_learning_workspace_screen.dart:41-608` | Canonical per-chapter Flashcards+Quiz: `TabController 6` `Chat/Textbook/Flashcards/Simulation/Watch/Quiz`, `Flashcards Tab3:589` `ChapterFlashcardsScreen(chapterId:1...)` (hard `1` bug `navigation-ui-ux.md:43`), `Quiz Tab6:604` `_buildQuizTab:983-1142` + Chat/Sim/Watch. `_loadTabResources:177-252` loads `_client.getQuizForChapter` + flashcard local. |
| `features/assessment/presentation/chapter_flashcards_screen.dart:??` | Dedicated flashcards deck (flip, `320x380` fixed `navigation-ui-ux.md:41`). Used by workspace Tab3. Not used by `Practice`. |
| `features/assessment/data/local/quiz_result_repository.dart` + `progress/data/local/progress_repository.dart` | Persist per-question results in both `mentora_practice_screen:247` and `workspace:1083`. Duplicate logic. |
| `pihub/backend/gateway/app/main.py:1943-1982` | Live learning artifact APIs: `GET /flashcards`, `/quizzes`, `/glossary`, `/summaries` `1943+` all `-> _asset_response(asset_name, {grade,subject,chapter,topic})` `588-616` (parallel preview fanout `594-615`). Also `POST /planner/lesson:1983`. These are the correct endpoints `Practice` should call. |
| `docs/frontend-audit/frontend-backend-route-audit.md:50-54` | Confirms `GET /quizzes/{chapter}` `GET /videos/{chapter}` `GET /api/v1/simulations/{chapter}` are **404** live; `GET /quizzes?chapter=` query-param is live. |

### 2.2 Data Flow (Current)

```
IndexedStack tap Practice (index 1)
  -> MentoraPracticeScreen.initState
    -> _activeChapterId = 'Motion' (Physics default)
    -> MentoraBackendClient.getQuizForChapter('Motion')
       -> returns local Map{10 templated Qs} (no http, no grade/subject filter)
  -> build: 4 chips -> 1 question card -> Check Answer -> Next
  (no SharedPreferences grade, no subjects RPC, no chapter list, no flashcards)
```

Compare desired:
```
Home: selected_grade (SharedPrefs) -> getSubjectsForGrade(grade) grid
SubjectChapters: getChaptersForSubject(subject,grade) list -> tap chapter
  -> ChapterWorkspace(grade,subject,chapterTitle) 6 tabs -> Flashcards + Quiz
Practice (spec): same as above but entry is Practice tab, listing ALL chapters of grade grouped by subject
```

### 2.3 UI/UX Bugs Specific to Practice

| File:Line | Bug | Severity |
|---|---|---|
| `mentora_practice_screen.dart:20` `_selectedDifficulty='Medium'` never changes; badge `172` hard `Difficulty: Medium` | MEDIUM | Mismatch adaptive bank (`gateway /quizzes?difficulty=`) unused |
| `27-39` switch ignores grade | HIGH | Grade 9 vs 12 same `Motion` |
| `49` `getQuizForChapter` mock discards `subject/grade/topic` | HIGH | Always generic `System Properties` 4-option |
| `300` `_currentQuestionIndex = (_currentQuestionIndex+1)%length` loops forever | LOW | No progress/completion, vs `workspace:1122-1137` `Restart Quiz` resets |
| `242-266` `QuizResult` saves per-question `score: isCorrect?1:0 totalQuestions:1` | MEDIUM | `progress_dashboard` expects per-session `totalQuestions:10`; `chapter_flashcards` not recorded |
| `112-137` 4 chips hard Physics/Chem/Bio/Math | MEDIUM | Misses `English`, `Social Science`, `Kannada` present in `course_repository.dart` seed |
| `object` no `RefreshIndicator`, no empty state | LOW | `subject_chapters_screen:131` has `RefreshIndicator`; practice has spinner only |
| `321-330` `ChoiceChip` `onSelected:_=>onTap()` ignores deselect | LOW | Double tap no-op |
| Duplicate `workspace:983` quiz UI - 2 quiz players | MEDIUM | 4 quiz UIs total `mentora_practice` + `quiz_assessment_screen` + `quiz_player_screen` + `workspace Tab6` (`navigation-ui-ux.md:22`) |

---

## 3. Proposed `Practice` Spec (To Match User Intent)

```
Practice tab
 ├─ Header: "Grade {activeGrade} Practice" + subject filter (All | Maths | Science | Social Science | English) from getSubjectsForGrade
 ├─ Body: Grouped chapter list
 │    ├─ For each subject in grade: SectionHeader(subject, pack_count)
 │    └─ For each chapter in subject (from getChaptersForSubject(subject,grade)):
 │         Card: ch.number, title, status badge (completed/in_progress/not_started), duration, mastery
 │         Pills: Flashcards(🔖 count) · Quiz(📝 count) · Textbook · Chat
 ├─ On tap chapter card -> ChapterPracticeDetail OR push ChapterWorkspace with initialTab = Flashcards/Quiz
 │    ├─ Flashcards deck (GET /flashcards?grade=&subject=&chapter=) + local fallback ChapterFlashcardsScreen
 │    └─ Quiz player (GET /quizzes?grade=&subject=&chapter=) with Check/Explain/Next + ProgressRepository
 └─ Empty states: "No chapters for Grade X, sync packs in Settings -> Manage Content"
```

Alternative A (minimal): Keep chips but replace body with chapter list instead of single question.
Alternative B (recommended): Reuse `SubjectChaptersScreen` grouping logic; `Practice` becomes `GradeChaptersScreen` (all subjects).

**Why grouped, not flat:** `CourseRepository: ensureSeedDataForGrade` seeds `sub_{slug}_{grade}` each with 10-12 chapters; `Home` already shows `totalChapters` per subject. Flat 40-chapter scroll loses subject context.

---

## 4. Backend Contract to Wire

| Endpoint | Gateway `app/main.py:line` | Params | Response | Used by |
|---|---|---|---|---|
| `GET /packs/catalog` | `1517` | - | `{grades:[{grade,subjects:[{subject,pack_count,chunk_count}]}]}` | `getSubjectsForGrade` `_fetchSubjectsFromNetwork:280` |
| `GET /flashcards` | `1943` | `grade,subject,chapter,topic` | `{items:[{front,back,term,definition,...}], total}` | New practice flashcards tab |
| `GET /quizzes` | `1953` | `grade,subject,chapter,topic` | `{items:[{question,options,correctIndex,explanation}], total}` | New practice quiz tab |
| `GET /glossary` | `1963` | `...` | `{items:[{term,definition}]}` | Optional glossary pill |
| `GET /summaries` | `1973` | `...` | `{items:[{text,title}]}` | Optional summary |
| `POST /planner/lesson` | `1983` | `{question,grade,subject,chapter,topic}` | `{summary,flashcards,practice_questions}` | Fallback when asset empty |

Current mock path `mentora_backend_client.dart:523` must be replaced by `await _client.get('/quizzes', queryParams: {grade,subject,chapter})` etc., with offline fallback to `getQuizForChapter` templated only if `BackendAvailabilityCache.isOffline` or `items.isEmpty`.

---

## 5. Implementation Plan (Single PR, 3 Files)

### Step 1 — `mentora_backend_client.dart` (no new file)
- Add `Future<Map<String,dynamic>> getPracticeBundle({required int grade, required String subject, required String chapter})` that parallel `GET /flashcards` + `GET /quizzes` (use `Future.wait`, `Dio get` with `queryParameters: {grade,subject,chapter}`) else fallback to current templated.
- Deprecate `getQuizForChapter` path-param; keep as offline generator but rename `generateOfflineQuiz(chapter)`.

### Step 2 — `mentora_practice_screen.dart` (rewrite `build`)
```dart
// _loadGradeAndChapters()
prefs.getInt('selected_grade') ?? 9 -> _activeGrade
subjects = await _client.getSubjectsForGrade(_activeGrade)   // reuse cache local+network
for (s in subjects) chaptersBySubject[s.name] = await _client.getChaptersForSubject(s.name, grade:_activeGrade)
setState
// build -> if _isLoading spinner else ListView grouped ExpansionTile per subject
// onTap chapter -> Navigator.pushNamed('/chapter_workspace', arguments:{chapterTitle,subjectName,grade,initialTabIndex:2}) // 2=Flashcards or 5=Quiz
// or inline BottomSheet with TabBar(Flashcards, Quiz)
```
- Remove `_selectedSubject` switch, `_selectedDifficulty` badge dynamic, single-question card.
- Keep `QuizResultRepository` + `ProgressRepository` but route via workspace so not duplicated.
- Add empty/error states + `RefreshIndicator`.

### Step 3 — `chapter_learning_workspace_screen.dart` (1-line fix)
- `ChapterFlashcardsScreen(chapterId:1)` `589` -> real `chapterId` from `CourseRepository` id or `pack_id`. Align with `practice` passing correct chapter.

### Step 4 — Tests
- Widget test: `practice loads grade 9 -> 4 subjects -> 32 chapters -> tap Motion -> workspace flashcards/quiz`.
- Integration: mock `Dio` `/flashcards` `/quizzes` 200 with 3 items; offline mode falls back.

---

## 6. Risk & Rollback

| Risk | Mitigation |
|---|---|
| `getChaptersForSubject` for 4 subjects sequential slow | `Future.wait` parallel; cache `_chaptersCache` already keyed `grade_subject` `327` |
| Empty packs (current `GET /packs 200 []`) -> empty chapters | Fallback `_loadSubjectsFromLocalDb:301` seeds 12 chapters/subject; show `Sync packs` CTA |
| Duplicate quiz persistence | Unify via `QuizResultRepository` single save; remove per-question `totalQuestions:1` in practice mock |
| Hard `Motion` query preserves 404 | Delete path-param stubs; use query-param `GET /quizzes?chapter=Motion&grade=9&subject=Science` |

---

## 7. Checklist

- [ ] `Practice` reads `selected_grade` from `SharedPreferences`
- [ ] `Practice` shows **all chapters in active grade grouped by subject** (via `getSubjectsForGrade` + `getChaptersForSubject`)
- [ ] Tapping chapter navigates to `ChapterWorkspace` flashcards/quiz (or inline deck)
- [ ] `Practice` calls live `GET /flashcards` + `GET /quizzes` with `grade,subject,chapter`
- [ ] Offline fallback still works (`BackendAvailabilityCache.isOffline`)
- [ ] Remove hard `Physics/Chem/Bio/Math` 4-chip single-quiz mock
- [ ] Fix `chapterId:1` hardcode in workspace
- [ ] De-dupe 4 quiz UIs to single `QuizPlayer`

---

## 8. References

- `Mentora/lib/features/assessment/presentation/mentora_practice_screen.dart:27-54,112-331`
- `Mentora/lib/features/network/services/mentora_backend_client.dart:253-403,523-606`
- `Mentora/lib/features/home/presentation/mentora_home_screen.dart:37-61,111-116`
- `Mentora/lib/features/course/presentation/subject_chapters_screen.dart:48-270`
- `Mentora/lib/features/tutor/presentation/chapter_learning_workspace_screen.dart:589-604,983-1142`
- `pihub/backend/gateway/app/main.py:1943-1983` `GET /flashcards|/quizzes|/glossary|/summaries`
- `docs/frontend-audit/navigation-ui-ux.md:22,45` duplicate quiz UIs, hard `Motion`
- `docs/frontend-audit/frontend-backend-route-audit.md:44-54` `GET /quizzes/{chapter} 404`
- `docs/frontend-audit/README.md:32` flow map `Home->SubjectChapters->ChapterWorkspace`

*Generated via live read of `Mentora/lib` 105 files + `pihub/backend` 316 routes on 2026-09-13.*
