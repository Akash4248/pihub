# Video Player Feature — Detailed Audit

**Scope:** `home/data/local/video_resource_repository:207`, `home/presentation/video_player_screen:728`, `tutor/presentation/chapter_learning_workspace:762-925` Watch Tab, `home/presentation/chapter_dashboard:471-718` inline, `course/data/local/app_database:552`, `mentora_backend_client:470`, `assets/chapter_video_resources.json` ~1300 entries, `learning_materials_screen:359-495` filesystem scan  
**Date:** 2026-09-12

---

## 1. Discovery — 3 Sources, 2 Tables, Tier-3 Wildcard

### 1.1 Central YouTube `video_resource_repository:74-207`

| Line | Finding | Sev |
|---|---|---|
| 81-96 | Seed-guard `COUNT(*) WHERE grade=?` bails if `count>0 && !hasInvalidUrls` → prevents re-seed. | ✅ |
| 89-92 | Blacklist `contains('MAG_BR')` string-match deletes **entire grade** if 1 bad sample | ⚠️ → URL validation |
| 102-108 | `rootBundle.loadString('assets/chapter_video_resources.json')` sync on main, 900 entries, re-parsed per grade, no cache, `where grade==grade` in-memory | Perf — split per-grade asset |
| 110-134 | `batch.insert ConflictAlgorithm.replace` **without UNIQUE `(chapter_id,grade,rank)`** → `id` PK auto-inc duplicates accumulate | 🐛 `UNIQUE(chapter_id, video_id)` |
| 175-206 | 3-tier fallback `chapter_id=?` → `LIKE %sanitized%` → `grade=?` returns **entire grade 45 videos** when title mismatch → irrelevant videos shown | 🐛 Return `[]` + warning, not grade dump |
| 187 | `chapterId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'),'%')` → `Light – Reflection` → `Light%%%Reflection` overly broad `LIKE %...%` | 🐛 |
| 144 | `saveGradeVideos` 0 callers — dead API for pack installer, flexible `v['chapter_id']??v['chapter']` anticipation never used | Dead |

### 1.2 Filesystem Pack Scan `learning_materials_screen:359-404`

| Line | Finding |
|---|---|
| 31-38 | Second source `_repoVideosPath=/home/akash/Desktop/IDP/VIDEOS` hard dev path + `/storage/emulated/0/VIDEOS` → probe wasted I/O on device. |
| 126-139 | `listItemsByKind('video')` → `MediaResource` for Videos tab — **disjoint** from `chapter_video_resources` — Workspace Watch vs Videos tab different catalogs. |
| 38-40 | Fake seed `assets/demo_video.mp4` `sizeBytes:1024` not in `pubspec` → phantom `videoCount` metric `buildCatalogEntry:127` |

### 1.3 Backend `mentora_backend_client:470` `GET /videos/$chapter`

Undocumented, no `grade/subject` params, `DioException` swallowed → hard fallback 3-item `tBmavvMwu68` playlist; likely always 404. Duplicates catalog `content_pack_sync_service:372` `${RuntimeBackendUrl}/packs` vs `/packs/catalog` — hosts can diverge.

### 1.4 Asset `chapter_video_resources.json`

~1300 entries, **3 IDs rotating** `M7lc1UVf-VE, aircAruvnKk, fNk_zzaMoSs, WUvTyaaNkzM` cyclic — not curated. All `grade=10`; **Grade 9 (default) 0 rows** → fallback tier-3 all-grade bug triggered.

---

## 2. Players — 4 Controllers, 3 Helpers, Split State

| File:Line | Finding |
|---|---|
| `video_player_screen:5` `youtube_player_flutter 10.0.1` + `video_player 2.8.2` dual import | Correct separation but duplicate WebView engines with `flutter_inappwebview`. |
| `37-41` `_isYouTube` `contains('youtube.com')\|youtu.be\|^[11-char]$` | Misses `m.youtube.com`, `youtube-nocookie`, `/embed` without full URL. |
| `42-67` `_extractYouTubeId` **triplicated** `video_player:42`, `chapter_dashboard:494`, `workspace:738` — `?v=`, `youtu.be/`, `/embed/`, bare ID → fallback `M7lc1UVf-VE` demo **masks broken URL**. | Extract `core/utils/youtube_utils.dart` |
| `75-139` `_initializeVideo()` forks YouTube `YoutubePlayerController(autoPlay:true)` else `VideoPlayerController.networkUrl/file` | `print` logs, no `try/catch`, `networkUrl` **not cached** (`cached_video_player` missing) → re-buffer on replay; offline YouTube fails with no connectivity banner. |
| `126-130` listener `setState(){}` per position tick ~30fps | Jank — use `ValueListenableBuilder`. |
| `148-161` `_changePlaybackSpeed` only native, **no-op for YouTube** but UI still shows control. | Guard. |
| `535-552` Share/CC `SnackBar('Coming soon')` dead FAB. | Dead. |
| `chapter_dashboard:490` `+video_player_screen:5` **3rd inline player** each `YoutubePlayerController` — N controllers if play sequentially without close → leak. | Close on another start. |
| `workspace:738-783` **4th player** `YoutubePlayerController _youtubeController` + `TabController` double listener `54+302` recreates controller inside `build() 772` side-effect → flicker/leak; `print [VIDEO_DEBUG] 56+307` prod; `autoPlay:false` vs other `true` inconsistent. | Move to `didUpdateWidget` `setState`. |
| `workspace:517` desktop `useHybridComposition:true` fallback `Open in Browser` without `canLaunchUrl` | Check. |
| `workspace:877` ISL FilterChip toggles `_islEnabled` with **zero effect** (no overlay, `islAvailable` unused) | Dead stub. |

**Package:** `pubspec:58` `youtube_player_flutter 10.0.1` WebView-based, no API key, embedded IFrame; no `youtube_thumbnail` (`img.youtube.com/vi/$id/0.jpg`) fetched — gradients only. No `onError` wired.

---

## 3. Native / Pack Video

| Aspect | Finding |
|---|---|
| Native path `video_player:98-115` `File.exists` correct, error verbatim leaks path. | — |
| Pack `learning_materials:_VideosTab:1374` `MediaResource` `duration '--:--'` hardcoded, views `'Local file'` | No `ffprobe` duration extraction. |
| Caching | **None** — no `cached_video_player`, `hive`, watch progress `learner_progress` tracks quiz/chat only (`app_database:268`). `MentoraBackendClient` in-memory subject cache no video bulk `getVideosForGrade`. |
| Offline | YouTube requires internet — no `ConnectivityService` guard, no offline banner; local `media_resources` offline but `chapter_video_resources` bundled offline metadata still tries YouTube playback. |

---

## 4. Caching / Duplication / Dead Code

| File:Line | Dead |
|---|---|
| `video_resource_repository:144` `saveGradeVideos` 0 callers | Dead |
| `mentora_backend_client:470` `GET /videos/$chapter` | Always fallback, unverifiable |
| `video_player:219` `Ask AI` FAB | Stub |
| `workspace:877` ISL | Stub |
| `content_packs/bootstrap:38` `demo_video.mp4` phantom | Remove |
| `app_database:48` `media_resources` vs `chapter_video_resources` forked storage | Merge into single `VideoRepository` |

**Duplication:** 3× `extractYoutubeId` + 3× `isYouTube` + 3× controller init → `youtube_utils.dart`; 2 tables vs `media_resources` + `material_pack_items`.

---

## 5. UI/UX

| File:Line | Bug |
|---|---|
| `workspace:344` 6-tab `isScrollable` no `AutomaticKeepAlive` → video resets on tab switch (mitigated `pause` but state lost). | KeepAlive. |
| `workspace:344` vs `dashboard:404` dual Watch — dashboard vertical cards vs workspace hero+playlist no deep-link sync; tap dashboard card doesn't open workspace `Watch` tab. | Deep link `initialTabIndex:4`. |
| `workspace:165` duration `${(sec/60).round()}:00` loses seconds → `305→5:00` vs `5:05`; `5400→90:00` invalid `mm:ss` → `1:30:00`. | `Duration` format. |
| `video_player:325` app bar `OfflineTutor` stale branding vs `Mentora` | Rename. |
| `workspace:785` `SingleChildScrollView` wrapping `YoutubePlayer` gesture conflict scroll vs drag | `NestedScrollView` fixed player. |

---

## 6. Recommendations

1. **Unify discovery** `VideoRepository.getVideos(chapterId,grade)` queries `chapter_video_resources` + `material_pack_items(kind=video)` de-dupe by `videoId`, `source` flag. Remove `saveGradeVideos` or wire.
2. **Deduplicate** `youtube_utils.dart` `extractYoutubeId/isYouTubeUrl/buildYoutubeUrl`.
3. **Controller lifecycle** — move `workspace:772` out of `build` to `initState/didUpdateWidget/setState`; single source of video state.
4. **Fallback** — tier-3 `grade=?` → `[]` + "No videos for this chapter" banner, not unrelated grade dump. Add `UNIQUE(chapter_id, video_id)` + index `grade, chapter_id`.
5. **Offline** — `ConnectivityService` guard before `YoutubePlayer`, show local pack videos alternative + `cached_video_player` for http.
6. **Remove fakes** — delete `M7lc1UVf-VE` fallback + `aircAruvnKk` rotation vs empty state ` _initError`.
7. **Wire `VideoResourceRepository.ensureSeed`** lazily with `await` + cache, split per-grade JSON.

*Inventory: 728-line `video_player_screen`, 1197-line `workspace` Watch `762-925`, 719-line `chapter_dashboard`, 577-line `app_database` table `552`, `youtube_player_flutter 10.0.1`, `flutter_inappwebview 6.1.5`.*
