# Content-Pipeline Service Audit — `pihub/backend/content-pipeline`

**Scope:** `app/main.py` (1723 lines) + `app/textbook_ingest.py` (637) + `app/content_pipeline/*` (7 modules) + `app/educational_intelligence/*` (12) + `app/curriculum_graph/*` (5) + `app/retrieval_engine/*` (2) + `Dockerfile` + `shared/config.py`  
**Date:** 2026-09-12 — `develop` + agent delta (chonkie wrapper, `artifact_schemas.py`, `Dockerfile.pi`)  
**Method:** `git diff HEAD` + `pytest` 31/31 + live `run_backend.sh` `:8001/health` ok

---

## 1. Duplicate Logic — Chunking (4 chunkers, 3 heading detectors)

| Location | Issue | Fix |
|---|---|---|
| `app/main.py:735` `_split_into_sections` (isupper+endswith`:`) | Naive heading split duplicates `SectionParser` | Delete, keep `SectionParser`. |
| `app/main.py:763` `_chunk_text` (fixed `chunk_size/overlap` char window, `self.chunk_size` never set) | Dead code — shadowed by `enable_semantic_educational_chunking` at `textbook_ingest.py:572` | Delete. |
| `app/textbook_ingest.py:324` `EducationalChunker` (100/1200 char buffer) | Legacy paragraph chunker | Delete, keep `EducationalChunkerV2`. |
| `app/content_pipeline/educational_chunker.py:25` `EducationalChunkerV2` (SectionParser+ParagraphMerger+FormulaPreserver+ConceptBoundaryDetector+chonkie) | **Canonical** — now `Clean→Chonkie→Preserve→Repair` | Keep single entrypoint. |
| `section_parser.py:13` vs `textbook_ingest.py:196` vs `main.py:735` | 3× `chapter/unit/lesson/section \d+` regex thresholds 8/10/12 words | Keep `section_parser.py` only. |
| `extraction_cleaner.py:85` `clean_raw_text`/`repair_chunks:174` vs `educational_chunker.py:69` `_NOISE_RE` vs `artifact_cleaning.py:71` `is_noisy_text` | 3× noise filters divergent regex | Unify in `extraction_cleaner.py`. |

---

## 2. Duplicate Logic — Embedding (hash fallback + double load)

| Location | Severity | Bug | Fix |
|---|---|---|---|
| `app/main.py:82` `SimpleEmbeddingModel.blake2b(token)%384` | CRITICAL — silent semantic collapse; unrelated text cosine >0; BGE-M3 1024d replaced by hash 384d without error, `ensure_collection` creates 384-dim collection then 1024 upsert fails. | `raise` + `health degraded` instead of fallback. |
| `app/main.py:110` `SentenceTransformerEmbeddingModel` duplicated in `retrieval_engine/hybrid_chunk_store.py:290` | MEDIUM — identical `snapshot_download` + `_resolve_local_model_path` (157-180 vs 307-330), 2 caches for same `BAAI/bge-m3`. | Share helper in `shared/`. |
| `app/main.py:249` `_load_model` vs `258` `_load_local_retrieval_model` | HIGH — double load same `BAAI/bge-m3` (config `embedding_model_name==local_retrieval_model_name`), 2×2.2GB RAM + 2× `snapshot_download`. `_load_local:265` eager `_ensure_loaded` race. | Single singleton injected into `HybridChunkStore(model=)`. |
| `app/main.py:118` fallback `return 384` | MEDIUM — hardcoded 384 mismatches bge-m3 1024. | Read from model. |
| `app/main.py:601` `_store_chunks encode` vs `hybrid_chunk_store.py:109` re-encode same `indexed_text` | HIGH — 2× encode per chunk | Embed once, pass vector to `hybrid_store.upsert(vectors=)`. |
| `app/enrichment_store.py:82` 3rd encode via `asyncio.to_thread` at `enriched_content_agent.py:225` | MEDIUM — sync block on loop | Reuse vector. |

---

## 3. Duplicate Logic — Retrieval (3 rerankers)

| Location | Duplication | Fix |
|---|---|---|
| `app/main.py:503` `_tokenize_text` + `511` `_lexical_relevance` (max overlap + 0.65/0.8/0.7 subject/topic boost) | Overlaps | |
| `retrieval_engine/educational_retrieval_engine.py:18` `_tokenize`/`_lexical_score`/`_chunk_type_score:51` + `162` `educational 0.4/0.2/0.15/0.25` → `rank:169` `0.35*semantic+0.35*lexical+0.30*educational` | Duplicates `_lexical_relevance` + diverges score (gateway `max` vs `rank` weighted) | Dedupe scorer. |
| `retrieval_engine/hybrid_chunk_store.py:229` `_fuse_scores 0.55*dense+0.35*lexical` + `_bm25` | 3rd scorer, called then rescored by `rank` → double lexical. | Fuse once. |
| `app/main.py:876` `_retrieve_candidate_hits` + `988` `_search` (3× `client.search` at 893,949,967 + `CurriculumRouter` + `ConceptIndex`) | Mirrors `pack-service/pack_generator:216` `_search_chunks_by_metadata` + `404` `_build_qdrant_filter` — `SUBJECT_QUERY_ALIASES:37` diverges from `shared/text_normalization`. Also `pack-service` loads `with_vectors=True` wastefully. | Single `shared/vector_store.build_filter`, package `hybrid_chunk_index.json`/`sqlite_vec` not live Qdrant for Pi. |

---

## 4. Duplicate Pack Compilation (40% LOC)

| A (content-pipeline) | B (pack-service) | Divergence | Fix |
|---|---|---|---|
| `educational_intelligence/pack_compiler.py:18` 10 JSONs + ZIP `version 2.0.0` | `pack-service/pack_generator.py:662` `SemanticContentPipeline` + `PackRepository.save_pack` tar.gz `2.0.0` | Two manifests, validators, quality gates (`quality_evaluator.py:56` vs `pack_generator:686`). | Delete pack-compiler from pipeline, POST chunks to pack-service. |
| `app/main.py:1171` `build_pack_preview` (0.1.0) + `1218` `build_learning_pack_preview` + `1514` `/build/pack` | `pack-service/api/pack_routes.py:66` `/packs/{id}/preview|manifest|download` | Same JSON two services, `pack_id` slug divergence `slug(pack)-slug(grade)-slug(subject)` vs `class_7_science_en`. | Single authority = pack-service. |
| `app/concept_graph_exporter.py:15` + `app/sync_and_import_all_packs.py:1` (`/home/akash/Documents/...` hardcoded) | `pack-service/pack_storage/` | SQLite exporter duplicated; `sync_and_import...` synthetic `"Conservation of Energy"` fallback fake quizzes. | Delete or gate. |

---

## 5. Duplicate Artifact Generation (8 generators + 2 agents)

| Location | Duplication | Fix |
|---|---|---|
| `app/main.py:635` `_generate_artifacts` 8× sequential `await summary/glossary/quiz…` | `educational_intelligence/agentic_orchestrator.py:347` `ArtifactAgent` same 8 via `GenerationPlanner`+critique | `_generate_artifacts` should delegate to `ArtifactAgent`. |
| `educational_intelligence/enriched_content_agent.py:65` wraps `ArtifactAgent` + `WebResourceFinder` | `app/main.py:233` instantiates both `ArtifactAgent` *and* `EnrichedContentAgent` (2× `InferenceClient`, 2× `ContentAnalyzer`) | Single agent. |
| `summary_generator:37` + `glossary_extractor:107` + `quiz_generator:67` + `flashcard_generator:62` 4× `_heuristic_*` | All `clean_text` + `metadata topics/concepts` + language detection; `quiz:68` even calls `glossary.extract` recursively | Shared heuristic helper. |
| `inference_client.py:58` 8× `generate_*` via `/ai/content/*` vs `agentic_orchestrator.py:198` `_generate_text` via `/ai/chat` | Two LLM paths for same model | Single `generate_structured` with `Instructor`. |

*Agent delta status:* `artifact_schemas.py` (new) + `agentic_orchestrator:_parse_artifact` now Pydantic-validates 6/8 types; `InferenceClient.generate_structured` added but **not yet wired** into `_execute_task` (so `max_retries` dead) — next PR to wire.

---

## 6. Bugs — Silent Fallbacks

| File:Line | Severity | Bug |
|---|---|---|
| `app/main.py:304-308,313-315` `ensure_ready:306` `except: warning` + `314: pass` | HIGH — swallows Qdrant failure, search returns 0 hits with 200. |
| `app/main.py:124-128,143-155` model load `except: self._model=None; 384` | HIGH — swallows `sentence_transformers` import/model error with no log. |
| `app/textbook_ingest.py:435-439` `except: return extracted` after `pytesseract` missing | MEDIUM — scanned PDF returns truncated fitz text silently. |
| `app/retrieval_engine/hybrid_chunk_store.py:56,292-303` snapshot/model load `except: return False/None` | HIGH — corrupted `hybrid_chunk_index.json` → `search:142` `[]` silently. |
| `app/educational_intelligence/inference_client.py:50-55` `except ConnectError: return None` | HIGH — every `generate_*` → `None` → heuristic fake MCQ `quiz_generator:168` `"What best describes X?"` + fake distractors — 200 with low quality. |
| `app/auto_ingest.py:114-117` `asyncio.create_task` from watchdog thread without `call_soon_threadsafe` | HIGH — race, task may never run; `processed_files` set not threadsafe, `on_modified:57` noop so updated PDF never re-ingested. |

---

## 7. Bugs — Hardcoded /shared Paths

| File:Line | Path | Fix |
|---|---|---|
| `app/generated_pack_ingestor.py:58-60` `/shared/packs`, `/shared/packs/grade_0/mixed/generated_pack`, `/shared/generated_pack` | Hardcoded | `settings.pack_storage_path` |
| `app/educational_intelligence/reports/report_renderer.py:10` `/shared/work/reports` | Hardcoded | `settings.work_dir` |
| `app/textbook_ingest.py:481` `Path(settings.upload_dir)/images` | Unbounded growth + duplicate root with `media_store.py:76` `/shared/media/videos` | Centralize. |

Bare `Pipeline()` outside Docker throws `PermissionError: /shared` (verified `re-audit`) — `run_backend.sh` exports `MEDIA_STORAGE_PATH=.local_run/media` masks it, but code needs lazy `mkdir` only if `enabled`.

---

## 8. Bugs — Validation & Race

| File:Line | Bug |
|---|---|
| `app/main.py:789` `ingest_pdf:791` `sanitize_safe_filename` added ✓ but no size/MIME/PDF magic check, `content bytes` full in mem → OOM. |
| `app/main.py:804` `ingest_textbook_directory` `glob("**/*.pdf")` no limit, serial `to_thread` but 3 writes per file (`Qdrant+hybrid+graph.save`) no batch → N rebuilds O(N²). |
| `app/main.py:317` `reset_rag_state` `delete_collection`→`ensure_collection` no lock → concurrent `ingest_pdf:790` may write to deleted collection. `ingestion_log:717` list append not threadsafe. |
| `app/retrieval_engine/hybrid_chunk_store.py:79` snapshot `write_text(json.dumps)` not atomic rename → torn write corrupts JSON. |
| `app/media_store.py:235` `_budget_ok` `rglob("*").stat` per video (8 topics → 8 full scans) | O(n) per topic. |
| `app/main.py:1368` `rag/search limit=1` hardcoded vs `curriculum_search:1379` `min(limit,10)` | Inconsistent. |

---

## 9. Performance

* Double model load 2× BGE-M3 ≈4.4GB RAM — share singleton.
* Sync `encode()` on loop: `_store_chunks:601` + `_retrieve_candidate_hits:893` 3× sequential `client.search` + `hybrid.search` — should `gather` dense|lexical|chapter.
* 8× sequential `await summary/glossary…:651-658` + `ArtifactAgent:503` `for task: await _execute` — could `gather` independent (`summary+glossary`).

---

**Priority fix order:** Replace hash fallback with `raise` (82-107), single embedding singleton, delete dead chunkers (735-787), remove `PackCompiler` from pipeline (1171,1218,1514), parametrize `/shared` (58-60), atomic snapshot + batch ingest, wire `InferredClient.generate_structured` into `ArtifactAgent`.

*Evidence: 31/31 pytest passed (`PYTHONPATH=backend/content-pipeline:backend`), `:8001/health` `ok` post-restart (391 weights).*
