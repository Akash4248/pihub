# Pack Sync / Curriculum / RAG — Detailed Audit

**Scope:** `Mentora/lib/features/educational`, `content_packs`, `course`, `rag`, `network` vs `IDP/offline_tutor_app` vs `pihub/backend` 321 routes  
**Date:** 2026-09-12 — `flutter analyze` 799 issues, `diff -rq` 83

---

## 1. Pack Sync — 4 Parallel Implementations (CRITICAL)

Mentora compiles **4 pack managers** that all do catalog→version→download→progress with different clients/schemas — only 1 is reached from UI.

| Impl | File:Line | Class | Base URL / Endpoint | Model Written | Status |
|---|---|---|---|---|---|
| 1 | `educational/application/pack_manager.dart:16` `PackManager` | `IDP/TEXTBOOKS` hardcoded dir, `GradeModel`→`educational.db` | Legacy-only | **Dead** — no UI calls it, but still compiled |
| 2 | `educational/application/pack_sync_service.dart:20` `PackSyncService` | `EndpointBuilder.packsCatalog` `GET /packs/catalog` 8s, `PackInstaller` | `GradeModel/SubjectModel` | Dead — `pack_manager:309` `SyncManager.checkForPackUpdates` body truncated (no `http.get`), falls through to offline fallback |
| 3 | `educational/application/sync_manager.dart:30` `SyncManager`+`SyncQueue`+`PackDownloadOperation:204` `GET /packs` `POST /packs/{id}` `GET /sync/delta/{packId}` `POST /progress/update` | `EducationalPackModel` → `educational.db` + `RagRepository` | Live for grade onboarding (`grade_sync_screen:54`) but `checkForPackUpdates:60` gutted and `syncPackWithBackend:194` `POST /packs/{id}` 404 (should be `/packs/sync`) |
| 4 | `content_packs/application/content_pack_sync_service.dart:173` `ContentPackSyncService` | mDNS `school-content.local:641` + `GET /api/v1/pdf/catalog` via `BackendApiService.listPacks` | `ContentPackManifest`→`material_packs` | **Canonical** — `ContentPackInstallerScreen` uses this; probes `192.168.50.1` etc. |

Installers: `pack_installer.dart:17` (`ZipDecoder` → `educational_*`) vs `content_pack_archive_service.dart:61` (GZip+Tar → `material_packs/rag_chunks`) — two DBs.  
Models: `EducationalPackModel` vs `ContentPackManifest` vs `RemoteContentPack:14` vs `PackSyncEntry` vs `CurriculumChapter:23` — `version String vs int`, `localPath/rootPath/archiveUrl/downloadUrl` divergent, `packId==chapter_id` conflation via `sync_manager:162`.

### Top Bugs (Pack)

| ID | File:Line | Sev | Bug | Fix |
|---|---|---|---|---|
| PS-01 | `pack_installer.dart:69-102` | CRITICAL | **Zip-Slip** `path.join(targetDir, file.name)` no `..` check — `../../etc/cron` escapes. | `canon = normalize(join(target, file.name)); if (!isWithin(target, canon)) throw`. |
| PS-02 | `308:312` | HIGH | `_readFileContent` discards non-`.txt/.md` bytes → `''` → empty DB + zero-token RAG. Double truncation `500` then `200`. | `PdfExtractionService.extractText` for RAG, excerpt for UI. |
| PS-10 | `sync_manager:60-90` | CRITICAL | `checkForPackUpdates` body missing `http.get` — always returns offline fallback. | Restore from `ContentPackSyncService.fetchCatalog`. |
| PS-11 | `92:119` | HIGH | Offline fallback `SELECT chapter_id,subject,grade FROM chapters WHERE grade=?` — columns **not exist** in `AppDatabase:146` (`id, subject_id, title`) → `no such column: chapter_id` → always `[]`. | `SELECT id,title FROM chapters JOIN subjects ... WHERE course_id` via `CurriculumRepository`. |
| PS-14 | `194:224` | HIGH | `POST /packs/$packId` 404 — should be `POST /packs/sync`/`/sync/delta`. | `EndpointBuilder.syncDelta`. |
| PS-16 | `265:291` | MEDIUM | `POST /progress/update` `chapterId int` vs `ContentPack` `String phet_simulations_v1` type mismatch. | `String chapterId`. |
| PS-21 | `504:654` | HIGH | `http.get.bodyBytes` loads 150MB pack in RAM → OOM, 5-min timeout, no `sha256` verify, logs `print DOWNLOAD_URL` with token. | Stream `HttpClient` chunked + `sha256` vs `entry.hash`. |
| PS-25 | `content_pack_sync_service:188` + `ContentPackInstallerScreen:55` `http://192.168.50.1:8080/catalog.json` | MEDIUM | Stale hardcoded IPs override `AppEnvironment.backendBaseUrl`. | `RuntimeBackendUrl`. |

---

## 2. Curriculum — 3 Hierarchies + 4 Repos

| Hierarchy | File | Fields | Issue |
|---|---|---|---|
| 1 | `educational/models/educational_models.dart:10` `GradeModel→SubjectModel→ChapterModel` | `gradeId String "grade_6"` | Drift vs int grade. |
| 2 | `course/domain/curriculum_models.dart:1` `CurriculumGrade→Subject→Chapter` | `int grade`, `rootPath` | **Standard** — `CurriculumRepository` aggregates `material_packs`. |
| 3 | `content_packs/domain/content_pack_models.dart:69` `ContentPackItem` slug `"class_6_science"` | range `gradeMin/Max` | 3rd slug. |
| Repos | `CourseRepository`/`CurriculumRepository`/`ContentPackRepository`/`TextbookRepository` | `AppDatabase v19` vs `EducationalDatabase` duplicate | `CurriculumRepository:19` groups by `gradeMin` only → combined `6-9` pack invisible for 7-9. |

| ID | File:Line | Sev | Bug |
|---|---|---|---|
| CU-02 | `curriculum_repository:48-53` | HIGH | Only first `summaries.json` item → 5-section pack shows 1. Concatenate. |
| CU-05/06 | `grade_screen:27`, `subject_screen:27`, `chapter_screen:33` `widget.grade.id!` | HIGH | Force-unwrap null `id` crash on unsaved grade from `PackInstaller` race `UNIQUE gradeNumber`. |
| CU-09 | `subject_chapters_screen:231` + `chapter_learning_workspace:488` `"/storage/emulated/0/Android/.../packs/${ch['title']}/source.pdf"` | MEDIUM | Hard package + unsanitized `title` → iOS/desktop 404. Use `ContentPackRepository.packRootDirectory`. |

---

## 3. RAG — 6 Engines, 3 Stores, Split Brain

| Engine | File | Store | Strategy |
|---|---|---|---|
| 1 OnDevice | `rag/on_device_embedding_engine:4` FNV 384-d hash | `embedding_index` | `Float32List` blob |
| 2 VectorEmbedding | `rag/vector_embedding_service:8` TF-IDF (`minTermFreq=2`) | vocab var | `initializeVocabulary` required; empty vocab for 3-chunk pack → `StateError` → `hybrid:211 []` silent |
| 3 EmbeddingIndex | `rag/embedding_index_service:19` | `Rag→Embedding` bridge | OnDevice only |
| 4 VectorSearch | `rag/vector_search_service:22` 60% cosine+40% Jaccard | both | — |
| 5 Hybrid | `rag/hybrid_retrieval_service:9` BM25 FTS5 + TF-IDF `rag_chunks_v2` | `rag_chunks_v2` | `semanticWeight 0.4` |
| 6 Simple | `rag/simple_rag_service:20` TF + FTS position + Jaccard 0.85 | `rag_chunks` | budget 900-2600 chars |
| + | `educational/inverted_index:8` + `local_search:33` + `retrieval_router:40` | `EducationalDatabase` | Old stack, `escalateToBackend` TODO `[]` |

| ID | File:Line | Sev | Bug |
|---|---|---|---|
| RG-01 | `hybrid_retrieval:100-103` `..sort; ..take(limit); ..toList` discards `take` | HIGH | `limit=10` returns 40 rows → token budget blow. Fix `(...toList()..sort()).take(limit).toList()`. |
| RG-05 | `rag_repository:133-157` insert `rag_chunks` + `rag_chunks_fts` **no transaction** | HIGH | Second insert fail → orphan; `ConflictAlgorithm.replace` only on main → duplicate FTS rows. Use `content='rag_chunks'` trigger. |
| RG-07 | `app_database:184-247` v1 `fts5(unicode61)` vs v2 **no FTS** comment "`FTS4 not supported - using LIKE`" | HIGH | `hybrid_retrieval:134` `JOIN rag_chunks_fts` on v2 → 0 rows → LIKE O(n) fallback slower. Create `rag_chunks_v2_fts USING fts5(content)`. |
| RG-12 | `educational/local_search:54-90` vs `RagRepository` | HIGH | **Split brain** — `LocalSearch`/`InvertedIndex` build from `EducationalDatabase.concepts` (empty when pack via `ContentPackArchiveService` writes only `AppDatabase`) → `OfflineTutorService` says "Download packs" despite packs installed. |

*Evidence: `flutter analyze` 799 includes `unused_import vector_search`, `print` in `rag_repository:114` x20, `AppDatabase v19` vs `EducationalDatabase` dual file.*

---

**Fix order:** Unify to single `AppDatabase` + `rag_chunks_v2` + `rag_chunks_v2_fts`, single `HybridRetrieval` (fix `RG-01`+`RG-07`), wire `OfflineTutorService→RagRepositoryV2`, delete `pack_installer.dart` (361 LOC) + `EducationalDatabase`, fix `sync_manager:92` fallback SQL + `rag/retrieve→/rag/search` alias, stream packs with hash.
